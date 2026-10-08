"""MP4 ctts 微手术：按认证时刻表原地改写呈现偏移，修复重复 PTS。
原理：仅改视频轨 ctts 盒（组合时间偏移），mdat/stco/音频一律不动；
moov 在文件尾，重建 moov 不影响任何块偏移。
用法: python ctts_patch.py <in.mp4> <out.mp4> <orig.npz>"""
import struct, sys, os
import numpy as np

SRC, DST, NPZ = sys.argv[1], sys.argv[2], sys.argv[3]

def box_iter(buf, start, end):
    pos = start
    while pos < end:
        size, typ = struct.unpack_from(">I4s", buf, pos)
        hdr = 8
        if size == 1:
            size = struct.unpack_from(">Q", buf, pos + 8)[0]; hdr = 16
        elif size == 0:
            size = end - pos
        yield pos, size, hdr, typ
        pos += size

src = open(SRC, "rb").read()
fsize = len(src)

# ---- 定位 moov（应在末尾）----
top = list(box_iter(src, 0, fsize))
moov = [b for b in top if b[3] == b"moov"][0]
assert moov[0] + moov[1] == fsize, "moov 不在文件尾，需处理 stco 平移（本脚本不覆盖该情形）"

def children(buf, box):
    start = box[0] + box[2]
    return list(box_iter(buf, start, box[0] + box[1]))

def find1(boxes, typ):
    return [b for b in boxes if b[3] == typ][0]

moov_boxes = children(src, moov)
# 视频轨：hdlr=vide
video_stbl = None
for trak in [b for b in moov_boxes if b[3] == b"trak"]:
    mdia = find1(children(src, trak), b"mdia")
    hdlr = find1(children(src, mdia), b"hdlr")
    handler = src[hdlr[0] + hdlr[2] + 8: hdlr[0] + hdlr[2] + 12]
    if handler == b"vide":
        minf = find1(children(src, mdia), b"minf")
        video_stbl = find1(children(src, minf), b"stbl")
        break
assert video_stbl is not None

stbl_boxes = children(src, video_stbl)
stts_box = find1(stbl_boxes, b"stts")
ctts_box = find1(stbl_boxes, b"ctts")

# ---- 展开 stts → dts ----
p = stts_box[0] + stbl_boxes and stts_box[0] + stts_box[2]
ver_flags, count = struct.unpack_from(">II", src, p)
p += 8
dts_list = []
t = 0
for _ in range(count):
    n_samp, delta = struct.unpack_from(">II", src, p); p += 8
    dts_list.extend(range(t, t + n_samp * delta, delta))
    t += n_samp * delta
dts = np.array(dts_list, dtype=np.int64)

# ---- 展开 ctts ----
p = ctts_box[0] + ctts_box[2]
ver_flags, count = struct.unpack_from(">II", src, p)
ctts_ver = ver_flags >> 24
p += 8
fmt = ">ii" if ctts_ver == 1 else ">II"
ctts_list = []
for _ in range(count):
    n_samp, off = struct.unpack_from(fmt, src, p); p += 8
    ctts_list.extend([off] * n_samp)
ctts = np.array(ctts_list, dtype=np.int64)
print(f"ctts version={ctts_ver} samples={len(ctts)} stts_samples={len(dts)}")

# ---- 与 PyAV 解封装值自校对 ----
d = np.load(NPZ)
pts_ref = d["pts_dec"]
mine = dts + ctts
assert len(mine) == len(pts_ref), f"样本数不符 {len(mine)} vs {len(pts_ref)}"
assert (mine == pts_ref).all(), "自研解析与 PyAV 解封装不一致！"
print("自校对 PASS：stts+ctts 展开 == PyAV 解封装 PTS（184293 帧全对）")

# ---- 计算每样本 bump ----
pres_order = np.argsort(pts_ref, kind="stable")
repaired_pres = pts_ref[pres_order].astype(np.int64).copy()
for k in range(1, len(repaired_pres)):
    if repaired_pres[k] <= repaired_pres[k - 1]:
        repaired_pres[k] = repaired_pres[k - 1] + 1
repaired_dec = np.empty(len(pts_ref), dtype=np.int64)
repaired_dec[pres_order] = repaired_pres
bump = repaired_dec - pts_ref
print(f"bump 分布: 0 tick={int((bump==0).sum())}, 1 tick={int((bump==1).sum())}, 2 tick={int((bump==2).sum())}, max={int(bump.max())}")

new_ctts = ctts + bump
if ctts_ver == 0:
    assert (new_ctts >= 0).all(), "v0 ctts 出现负偏移"
assert new_ctts.max() < 2**31, "偏移溢出"

# ---- 重压缩 run-length + 重建 ctts 盒 ----
runs = []
cur, n_run = int(new_ctts[0]), 1
for v in new_ctts[1:]:
    v = int(v)
    if v == cur:
        n_run += 1
    else:
        runs.append((n_run, cur)); cur, n_run = v, 1
runs.append((n_run, cur))
fmt_entry = ">iI" if False else None
body = struct.pack(">II", ver_flags, len(runs))
for n_run, off in runs:
    body += struct.pack(">Ii" if ctts_ver == 1 else ">II", n_run, off)
new_ctts_box = struct.pack(">I4s", 8 + len(body), b"ctts") + body
print(f"ctts 盒: {ctts_box[1]} -> {len(new_ctts_box)} 字节（条目 {count} -> {len(runs)}）")

# ---- 重建 stbl → minf → mdia → trak → moov ----
def rebuild(container_box, child_old, child_new_bytes):
    start, size, hdr = container_box[0], container_box[1], container_box[2]
    cstart, csize = child_old[0], child_old[1]
    inner = src[start + hdr: cstart] + child_new_bytes + src[cstart + csize: start + size]
    return struct.pack(">I4s", hdr + len(inner), src[start + 4:start + 8]) + inner

# 逐层向上重建（拿到各层盒对象）
for trak in [b for b in moov_boxes if b[3] == b"trak"]:
    mdia = find1(children(src, trak), b"mdia")
    hdlr = find1(children(src, mdia), b"hdlr")
    if src[hdlr[0] + hdlr[2] + 8: hdlr[0] + hdlr[2] + 12] == b"vide":
        video_trak = trak
        video_mdia = mdia
        video_minf = find1(children(src, mdia), b"minf")
        break

new_stbl = rebuild(video_stbl, ctts_box, new_ctts_box)
new_minf = rebuild(video_minf, video_stbl, new_stbl)
new_mdia = rebuild(video_mdia, video_minf, new_minf)
new_trak = rebuild(video_trak, video_mdia, new_mdia)
new_moov = rebuild(moov, video_trak, new_trak)
print(f"moov: {moov[1]} -> {len(new_moov)} 字节")

with open(DST, "wb") as f:
    f.write(src[: moov[0]])
    f.write(new_moov)
print("写出:", DST, os.path.getsize(DST), "字节")
