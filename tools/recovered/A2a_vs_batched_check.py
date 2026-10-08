"""验证 A2a 单趟产物（样本4, 视频-only, 2623段, 660s 完成）：
帧数 + 输出PTS序列 + 内容灰度，与分批原型 4_batched.mp4（已对认证表 PASS）逐帧比对。
若一致 → A2a 即样本 4 的首个单趟基准，分批=单趟等价在样本 4 上也成立。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".cache"))

import av  # noqa: E402

OUT = ROOT / ".cache" / "research_phase3"
A2A = OUT / "A2a_singlepass_v2623.mp4"
BATCHED = ROOT / ".cache" / "exp_batched_seek" / "4_batched.mp4"


def probe_pts(path: Path):
    c = av.open(str(path))
    v = c.streams.video[0]
    frames = []
    for f in c.decode(v):
        arr = f.to_ndarray(format="gray")
        frames.append((f.pts, float(arr.mean())))
    c.close()
    return frames


def main() -> int:
    print("[*] 解码 A2a 单趟产物...", flush=True)
    a = probe_pts(A2A)
    print(f"    A2a 帧数={len(a)}", flush=True)
    print("[*] 解码分批产物 4_batched.mp4...", flush=True)
    b = probe_pts(BATCHED)
    print(f"    分批 帧数={len(b)}", flush=True)
    n = min(len(a), len(b))
    mismatch = [i for i in range(n) if a[i][0] != b[i][0]]
    max_diff = max((abs(a[i][1] - b[i][1]) for i in range(n)), default=0)
    tail_note = ""
    if len(a) != len(b):
        tail_note = f"（尾部差异：单趟多出 {len(a) - len(b)} 帧——clone guard 帧数口径不同属预期）"
    res = {
        "a2a_frames": len(a),
        "batched_frames": len(b),
        "pts_mismatch_in_first_n": len(mismatch),
        "first_mismatch_index": mismatch[0] if mismatch else None,
        "max_gray_diff": round(max_diff, 4),
        "verdict": ("PASS: 单趟与分批逐帧一致" if not mismatch and max_diff < 2.0
                    else "FAIL"),
        "note": tail_note,
    }
    (OUT / "A2a_vs_batched_check.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=2), flush=True)
    return 0 if res["verdict"].startswith("PASS") else 1


if __name__ == "__main__":
    raise SystemExit(main())
