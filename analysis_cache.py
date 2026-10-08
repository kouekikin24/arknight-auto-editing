#!/usr/bin/env python3
"""分析结果磁盘缓存（R8 同源先例：npz + 内容键；对齐 skim_cache/skim_key 语义）。

**目的**：4.mp4（42 万帧）一次分析 ~4 分钟；产物仅 ~2.1MB
（states int8 0.42MB + diffs float32 1.7MB + context JSON 0.2MB），
落盘后重导入可秒级载入。

**键设计（调研结论 §8.1，T0）**：`sha256(源) × 参数指纹 × 后端 × ffmpeg 身份
× 产物格式版本`。源 sha256 复用 `MediaInfo.source_sha256`（probe_media 已算，
零额外成本）；参数指纹来自 `_start_analysis` 的 snapshot（阈值/参数直接决定
states，漏项会导致**静默错命中**——本模块的头号风险，故全字段入键）。

**存储（§8.2，T0）**：npz 单文件（ZIP 容器天然每成员 CRC-32，实测单字节
损坏抛 BadZipFile），元数据作为独立 entry 内嵌同文件（自证，不做 sidecar）。
原子写：同目录 mkstemp → write → fsync → os.replace；**Windows 上目标被
句柄占用会抛 WinError 5（实测）**，故带重试退避。

**v2 布局（09-16 修复）**：pauses 里的 `local_del_mask` 是 **numpy 数组**
（`analyzer._analyze_pause_mask` 产物），JSON 序列化会抛 TypeError——v1 因此
**从未写成功过**（异常被"缓存是纯加速层"的兜底吞掉，日志还谎报"已缓存"）。
v2 把 mask 抽成 npz 独立成员 `masks_concat`(uint8) + `mask_lengths`(int64)，
meta 里留占位 `{"__npz_mask__": i}`，载入时按长度切片还原为 ndarray。

**失效（§8.3，T0）**：版本号进键 → 旧版自然不可达；启动清理孤儿 .tmp；
容量上限 + 近似 LRU（按 mtime，命中时 touch）。

**交互（§8.4 + 主会话 review 修正）**：默认复用但**首次提示一次**
（分析结果直接影响剪辑决策，用户应知情；参数变化则强制重算）。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import numpy as np

# 产物格式版本：读写结构/产物语义变更时递增 → 旧缓存自然不可达（§8.3 方案 A）
# v2：mask 抽为 npz 独立成员（v1 因 JSON 序列化 ndarray 从未写成功）
# v3：0.2x 判据修复（须三类最高）→ states 语义变化，v2 缓存不可复用
ANALYSIS_CACHE_VERSION = 3

# 缓存目录（与既有 .cache/pyav_kf 同域）
DEFAULT_CACHE_DIR = Path(__file__).resolve().parent / ".cache" / "analysis"

# 容量上限：单产物 ~2.1MB；4096 条 ≈ 8.6GB（对照本机 .cache 已 16GB）
DEFAULT_MAX_ENTRIES = 4096

# Windows os.replace 重试（杀毒/索引/同步盘会瞬时持句柄）
_REPLACE_RETRIES = 5
_REPLACE_BACKOFF_S = 0.05


class AnalysisCacheError(RuntimeError):
    """缓存读写失败（带机器可读 code；调用方一律降级为「跑分析」不炸）。"""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True, slots=True)
class AnalysisCacheKey:
    """缓存身份：决定「复用还是重算」的全部输入（§8.1）。

    任一字段变化 → 键变化 → 视为未命中（all-or-nothing，绝不混用）。
    """

    source_sha256: str
    params_fingerprint: str
    decode_backend: str
    ffmpeg_identity: str
    templates_fingerprint: str = "unknown"
    format_version: int = ANALYSIS_CACHE_VERSION

    def digest(self) -> str:
        payload = "|".join((
            str(self.source_sha256).lower(),
            str(self.params_fingerprint),
            str(self.decode_backend),
            str(self.ffmpeg_identity),
            str(self.templates_fingerprint),
            str(int(self.format_version)),
        ))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def params_fingerprint(snapshot: Mapping[str, Any]) -> str:
    """分析参数指纹（§8.1 风险 #1：漏项=静默错命中）。

    取 snapshot 中**全部影响 states 的字段**：阈值/比较参数/处理分辨率/
    批大小/线程数/fps。用 sort_keys 保证同参不同构造顺序得到同指纹。
    """
    payload = {
        "thresholds": dict(snapshot.get("thresholds") or {}),
        "compare": dict(snapshot.get("compare") or {}),
        # 注意：不可写 `snapshot.get(...) or ()`——若调用方传 numpy 数组会触发
        # 真值歧义（ValueError: truth value of an array is ambiguous）。
        # 09-16 review 同类缺陷已在前一处修复，此处一并加固。
        "proc_res": [int(v) for v in (snapshot.get("proc_res") if
                                      snapshot.get("proc_res") is not None
                                      else ())],
        "batch": int(snapshot.get("batch") or 0),
        "threads": int(snapshot.get("threads") or 0),
        "fps": round(float(snapshot.get("fps") or 0.0), 6),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:32]


def ffmpeg_identity(media_info: Any) -> str:
    """ffmpeg 工具身份（§8.1/§7.2：版本差异会让同源同参结果不同）。

    先例：scripts/pts_normalized_proxy.py 用 sha256+version_line 硬校验。
    """
    try:
        ff = getattr(media_info, "ffmpeg", None)
        if ff is None:
            return "none"
        sha = str(getattr(ff, "sha256", "") or "")
        ver = str(getattr(ff, "version_line", "") or "")
        if sha or ver:
            return hashlib.sha256(f"{sha}|{ver}".encode("utf-8")).hexdigest()[:16]
    except Exception:
        pass
    return "unknown"


def templates_fingerprint(base_dir: Path | str | None = None) -> str:
    """模板身份（§8.1 风险 #1 补强，09-16 review 发现）。

    `analyzer._classify_gray` **完全**依赖模板匹配（templates_pause/1x/2x/play
    里的图片 + source_images_*）——换一张模板图、增删一个模板文件，同源同参
    也会得到不同的 states。不进键 = 静默错命中（最坏的一类：用户以为复用的
    是"刚算的"，实际是"按旧模板算的"）。

    **口径对齐**（09-16 review 修正）：`load_templates` 用的是 **CWD 相对**
    路径（analyzer.py:90 `os.path.exists(src_dir)`），故这里默认同样按 CWD
    解析——两边必须读同一批文件，否则指纹与真实分类输入脱钩。目录名/扩展名
    与 analyzer.TEMPLATE_DIRS/IMG_EXTS 保持一致。

    指纹 = 各目录下图片 文件名+内容 sha256 的排序摘要；目录/文件缺失以占位
    参与（结构变化同样换键）。
    """
    root = Path(base_dir) if base_dir else Path.cwd()
    dirs = ("templates_pause", "templates_1x", "templates_2x", "templates_play",
            "source_images_pause", "source_images_1x", "source_images_2x",
            "source_images_play")
    exts = (".png", ".jpg", ".jpeg", ".bmp")
    entries: list[str] = [f"cwd:{root}"]
    for name in dirs:
        d = root / name
        try:
            files = sorted(f for f in os.listdir(d)
                           if f.lower().endswith(exts))
        except OSError:
            files = []
        for fn in files:
            try:
                digest = hashlib.sha256(
                    (d / fn).read_bytes()).hexdigest()[:16]
            except OSError:
                digest = "unreadable"
            entries.append(f"{name}/{fn}:{digest}")
    if len(entries) == 1:
        return "none"
    blob = "\n".join(entries).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def make_key(media_info: Any, snapshot: Mapping[str, Any]) -> AnalysisCacheKey:
    """从 MediaInfo + 分析 snapshot 构造缓存键。

    **硬防护（09-16 review）**：源身份必须是一个真实的 sha256。若 media_info
    缺失或 sha 为空/非法，抛 AnalysisCacheError 而非构造「空身份键」——否则
    所有视频会撞同一键，造成**缓存投毒**（A 视频结果被 B 视频命中）。
    调用方应把该异常视为「无缓存可用」，回落完整分析。
    """
    import re as _re
    sha = str(getattr(media_info, "source_sha256", "") or "").strip().lower()
    if not _re.fullmatch(r"[0-9a-f]{64}", sha):
        raise AnalysisCacheError(
            "KEY_SOURCE_UNIDENTIFIED",
            "source sha256 unavailable; refusing to build a cache key",
            details={"sha_len": len(sha)},
        )
    return AnalysisCacheKey(
        source_sha256=sha,
        params_fingerprint=params_fingerprint(snapshot),
        decode_backend=str(snapshot.get("backend_key") or "opencv"),
        ffmpeg_identity=ffmpeg_identity(media_info),
        # 模板身份默认按仓库根目录算（可被 snapshot 显式覆盖，便于测试/迁移）
        templates_fingerprint=str(
            snapshot.get("templates_fingerprint")
            or templates_fingerprint()),
    )


def _lossless_cast(values, dtype: Any, label: str):
    """按目标 dtype 转换数组，**转换必须无损**，否则抛 AnalysisCacheError。

    与 mask 的同类缺陷一致（09-16 review）：`np.asarray(x, dtype=np.int8)` 会
    把 300 静默变成 44——"看起来存好了、读出来是错的"是最坏一类。真实
    analyzer 产物本就是 int8/float32（analyzer.py:561-562），故此检查对生产
    零成本，只挡异常调用方与未来的静默损坏。

    **无损的判据分整数/浮点两类**（09-16 复查修正——原实现对两者都用
    `array_equal`，对 float64 的 0.1→float32 会误判为"有损"而拒真数据）：
      · 整数：逐元素必须相等（可精确往返）；
      · 浮点：只要求"有限值仍有限"（转出 inf 才算有损）。float64→float32
        本就存在精度舍入，那是**目标格式的定义**（缓存里存的就是 float32），
        不是数据损坏；逐位相等会把合法的 float64 输入全部拒掉。
    NaN：NaN != NaN，用 `equal_nan` 语义——NaN 进 NaN 出视为无损。
    """
    arr = np.asarray(values)
    with np.errstate(over="ignore", invalid="ignore"):
        out = arr.astype(dtype)
    if np.issubdtype(np.dtype(dtype), np.integer):
        lossless = bool(np.array_equal(out, arr))
    else:
        # 有限输入不得溢出为 inf（inf→inf 合法；NaN→NaN 合法）
        finite_in = np.isfinite(arr)
        lossless = bool(np.all(np.isfinite(out[finite_in])))
    if not lossless:
        raise AnalysisCacheError(
            f"STORE_{label.upper()}_VALUE_LOSS",
            f"{label} does not survive cast to {np.dtype(dtype).name}")
    return out


def _stat_size(path: Path) -> int:
    """文件字节数；失败（并发删除/占用）返回 0——仅用于展示口径。"""
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


# ---- pauses 的 local_del_mask 与 JSON 的桥（v2 修复） ----
#
# 背景（T0，09-16 实测）：真实 analyzer 产物里每个暂停段带 `local_del_mask`
# （np.uint8 数组），JSON 序列化直接抛 TypeError。v1 因此从未写成功过。
# 方案：mask 抽成 npz 独立成员（二进制、体积小），meta 里只留索引占位。
# 注意「mask 缺失」与「mask 为 None」是两种语义，必须区分——前者是 speed
# 段/老产物形态，后者是显式空值；用「键是否存在」区分，不引入哨兵值。

_MASK_PLACEHOLDER_KEY = "__npz_mask__"


def _json_default(value: Any) -> Any:
    """numpy 标量 → Python 标量（便宜、无语义变化）；**数组一律拒绝**。

    数组必须走显式布局（如 mask 的 npz 独立成员）——不能悄悄展开成 JSON
    列表：体积/解析成本高，且会让"载入后是 list 还是 ndarray"变得含糊，
    下游按数组语义写的代码（如 `(m == 1) | (m == 2)`）会静默失效。
    """
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    raise TypeError(f"not JSON-serializable: {type(value).__name__}")


def _pack_pauses(pauses: list) -> tuple[list, list]:
    """把 pauses 里所有 ndarray mask 抽出为 (干净 records, mask 列表)。

    返回的 records 可安全 json.dumps；mask 列表按出现顺序拼接进 npz。

    两道硬校验（与本次修复同类：**静默数据损坏比崩溃更坏**）：
      ① 必须一维——二维 mask 会让 `concat[slice]` 按行切片，还原出错误形状；
      ② uint8 转换必须无损——`np.uint8(300)` 静默变 44。
    违规抛 AnalysisCacheError（调用方如实上报，绝不落盘错数据）。
    """
    records: list = []
    masks: list = []
    for raw in pauses or ():
        record = dict(raw)
        mask = record.get("local_del_mask")
        if mask is not None:
            orig = np.asarray(mask)
            if orig.ndim != 1:
                raise AnalysisCacheError(
                    "STORE_MASK_INVALID_SHAPE",
                    f"local_del_mask must be 1-D, got shape {orig.shape}")
            arr = orig.astype(np.uint8)
            if not np.array_equal(arr, orig):
                raise AnalysisCacheError(
                    "STORE_MASK_VALUE_LOSS",
                    "local_del_mask values do not survive uint8 cast "
                    f"(min={orig.min()}, max={orig.max()})")
            # 必须**移除**原 ndarray：只加占位符的话 json.dumps 仍会撞上它
            record.pop("local_del_mask", None)
            record[_MASK_PLACEHOLDER_KEY] = len(masks)
            masks.append(arr)
        records.append(record)
    return records, masks


def _unpack_pauses(records: list, concat, lengths) -> list:
    """还原 _pack_pauses 的产物：占位符 → ndarray（copy 保独立）。

    内部不一致**一律抛 ValueError 令整次载入失败**（由 load 吞成未命中→回落
    完整分析），绝不返回"少了几段 mask / mask 被错切"的错结果（R6 最内层
    安全：静默错数据比崩溃更坏）。检查项（09-16 review 补齐）：
      ① 长度和非负且等于拼接体大小；② 每个索引在范围内；③ 索引不重复
      （重复=两段共用一张 mask，属损坏）。
    """
    lengths = [int(v) for v in (lengths if lengths is not None else ())]
    concat_arr = np.asarray(concat)
    total = int(concat_arr.size)
    # 维度自证（与 _pack_pauses 的二维拒绝对称）：二维 concat 会让切片按行
    # 取，还原出形状错误的 mask（09-16 review 发现）
    if concat_arr.ndim != 1:
        raise ValueError(f"masks_concat must be 1-D, got {concat_arr.shape}")
    if any(n < 0 for n in lengths):
        raise ValueError(f"negative mask length in {lengths}")
    if sum(lengths) != total:
        raise ValueError(
            f"mask lengths mismatch: sum={sum(lengths)} concat={total}")
    offsets = []
    cursor = 0
    for n in lengths:
        offsets.append(cursor)
        cursor += n
    out: list = []
    used: set[int] = set()
    for raw in records or ():
        record = dict(raw)
        index = record.pop(_MASK_PLACEHOLDER_KEY, None)
        if index is not None:
            i = int(index)
            if not (0 <= i < len(lengths)):
                raise ValueError(f"mask index {i} out of range")
            if i in used:
                raise ValueError(f"duplicate mask index {i}")
            used.add(i)
            start = offsets[i]
            record["local_del_mask"] = np.array(
                concat[start:start + lengths[i]], dtype=np.uint8)
        out.append(record)
    return out


class AnalysisCache:
    """分析结果缓存（磁盘 npz）。线程安全：调用方在任务线程使用。"""

    def __init__(self, cache_dir: Path | str | None = None,
                 max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        self.dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
        self.max_entries = int(max_entries)

    # ---- 路径 ----

    def _path_for(self, key: AnalysisCacheKey) -> Path:
        return self.dir / f"{key.digest()}.npz"

    # ---- 读 ----

    @staticmethod
    def _validate_key(key: AnalysisCacheKey) -> None:
        """拒绝「未识别源」的键（防投毒；与 make_key 硬防护双保险）。"""
        import re as _re
        sha = str(getattr(key, "source_sha256", "") or "")
        if not _re.fullmatch(r"[0-9a-f]{64}", sha.lower()):
            raise AnalysisCacheError(
                "KEY_SOURCE_UNIDENTIFIED",
                "cache key has no valid source sha256",
            )

    def load(self, key: AnalysisCacheKey) -> dict[str, Any] | None:
        """命中返回 {'states','diffs','pauses','speeds','meta'}；否则 None。

        任何异常（损坏/版本不符/元数据自证失败）一律视为未命中——调用方
        回落完整分析，绝不抛给上层。
        """
        self._validate_key(key)
        path = self._path_for(key)
        if not path.is_file():
            return None
        try:
            # with 块：NpzFile 惰性持有 zip 句柄，显式关闭避免句柄滞留
            with np.load(path, allow_pickle=False) as d:
                # 自证：内嵌键必须与请求键一致（防"文件被换/键算法变更"）
                if int(d["format_version"]) != int(key.format_version):
                    return None
                if str(d["key_digest"]) != key.digest():
                    return None
                states = np.array(d["states"])
                diffs = np.array(d["diffs"])
                meta = json.loads(str(d["meta_json"]))
                if len(states) != len(diffs):
                    return None
                # v2：mask 在独立成员里，按 lengths 切片还原（缺成员=异常产物）
                records = meta.get("pauses") or []
                if any(_MASK_PLACEHOLDER_KEY in r for r in records):
                    if "masks_concat" not in d or "mask_lengths" not in d:
                        return None
                    records = _unpack_pauses(records, d["masks_concat"],
                                             d["mask_lengths"])
                    # 语义自证（09-16 review）：mask 长度必须与其段长一致。
                    # 真实产物由 _analyze_pause_mask 保证 len == end-start+1；
                    # 不一致=损坏/篡改 → 当未命中，避免下游 replace_timeline
                    # 在校验时才炸（那是"载入成功后失败"，体验更差）。
                    for rec in records:
                        mask = rec.get("local_del_mask")
                        if mask is None:
                            continue
                        if "start" in rec and "end" in rec:
                            expected = int(rec["end"]) - int(rec["start"]) + 1
                            if expected >= 0 and len(mask) != expected:
                                return None
                    # meta 里也替换为还原后的 records：否则调用方若把
                    # meta["pauses"]（还带占位符）再交给 store()，占位符会被
                    # 重写成新索引而旧 mask 仍在 concat 里 = 静默错配
                    meta = {**meta, "pauses": records}
            result = {
                "states": states,
                "diffs": diffs,
                "pauses": records,
                "speeds": meta.get("speeds") or [],
                "meta": meta,
                # 真实文件字节（GUI 提示框口径；调用方免于私有路径访问）
                # stat 失败（并发删除）不致命：退化为 0，不影响命中本身
                "file_bytes": _stat_size(path),
            }
            # 命中 → touch（近似 LRU）
            try:
                os.utime(path, None)
            except OSError:
                pass
            return result
        except Exception:
            # 损坏/不可读 → 未命中（不删，留待清理，避免与并发读竞争）
            return None

    # ---- 写 ----

    def store(self, key: AnalysisCacheKey, *, states, diffs,
              pauses: list, speeds: list,
              backend_label: str = "", source_size: int = 0,
              source_mtime_ns: int = 0, context_used: bool = True,
              templates_missing: bool = False) -> Path:
        """原子写缓存；**失败抛 AnalysisCacheError（带 code）**。

        09-16 修复：v1 把一切异常吞成 None，调用方又无条件打印"已缓存"，
        导致「真实产物写不进」被掩盖整整一轮。现在失败必须带原因抛出，
        由调用方（把缓存当纯加速层）捕获并**如实**上报。

        `context_used`/`templates_missing` 一并入档：缓存命中时完成提示框
        要用它们复述**当时**的真实分析路径——写死 True 会让提示框谎称
        "已跳过二次扫片"（与原分析不符，属"显示信息说谎"同类）。
        """
        self._validate_key(key)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AnalysisCacheError("STORE_MKDIR_FAILED",
                                     f"cannot create cache dir: {exc}") from exc
        target = self._path_for(key)
        # mask（ndarray）走 npz 独立成员，meta 里只留占位索引。
        # 整个组装段包在契约里：任何异常都以 AnalysisCacheError 抛出
        # （不对外泄漏裸 ValueError/TypeError——调用方按 code 上报）。
        try:
            records, masks = _pack_pauses(pauses)
            states_arr = _lossless_cast(states, np.int8, "states")
            diffs_arr = _lossless_cast(diffs, np.float32, "diffs")
            concat = (np.concatenate(masks) if masks
                      else np.zeros(0, dtype=np.uint8))
            lengths = np.array([int(m.size) for m in masks], dtype=np.int64)
        except AnalysisCacheError:
            raise
        except Exception as exc:
            raise AnalysisCacheError(
                "STORE_PAYLOAD_INVALID",
                f"{type(exc).__name__}: {exc}",
            ) from exc
        meta = {
            "format_version": int(key.format_version),
            "source_sha256": key.source_sha256,
            "params_fingerprint": key.params_fingerprint,
            "decode_backend": key.decode_backend,
            "ffmpeg_identity": key.ffmpeg_identity,
            "backend_label": str(backend_label),
            "source_size": int(source_size),
            "source_mtime_ns": int(source_mtime_ns),
            "created_wall": time.strftime("%Y-%m-%d %H:%M:%S"),
            "created_epoch": int(time.time()),
            "context_used": bool(context_used),
            "templates_missing": bool(templates_missing),
            "pauses": records,
            "speeds": list(speeds),
        }
        try:
            meta_json = json.dumps(meta, ensure_ascii=False,
                                   default=_json_default)
        except (TypeError, ValueError) as exc:
            raise AnalysisCacheError(
                "STORE_META_NOT_SERIALIZABLE",
                f"meta not JSON-serializable: {type(exc).__name__}: {exc}",
            ) from exc
        tmp: str | None = None
        try:
            # 唯一临时名（同目录 → 保证 os.replace 同卷原子）
            tmp = str(target.with_name(
                f".tmp_analysis_{os.getpid()}_{time.monotonic_ns()}.npz"))
            np.savez(tmp,
                     states=states_arr,
                     diffs=diffs_arr,
                     masks_concat=concat,
                     mask_lengths=lengths,
                     format_version=np.int64(key.format_version),
                     key_digest=np.array(key.digest()),
                     meta_json=np.array(meta_json))
            # fsync 后再 replace：断电/崩溃不产生半文件
            # （必须以写模式重开：savez 关闭了自己的句柄）
            with open(tmp, "rb+") as fh:
                fh.flush()
                os.fsync(fh.fileno())
            self._atomic_replace(tmp, target)
            tmp = None
        except AnalysisCacheError:
            raise
        except Exception as exc:
            raise AnalysisCacheError(
                "STORE_WRITE_FAILED",
                f"{type(exc).__name__}: {exc}",
            ) from exc
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        self._evict_if_needed()
        return target

    @staticmethod
    def _atomic_replace(src: str, dst: Path) -> None:
        """os.replace + Windows 共享冲突重试（WinError 5 实测复现）。"""
        last: Exception | None = None
        for attempt in range(_REPLACE_RETRIES):
            try:
                os.replace(src, dst)
                return
            except PermissionError as exc:       # Windows: 句柄占用
                last = exc
                time.sleep(_REPLACE_BACKOFF_S * (attempt + 1))
        if last is not None:
            raise last

    # ---- 维护 ----

    def evict(self) -> int:
        """容量上限 + 近似 LRU（按 mtime 最旧先删）。返回删除数。"""
        try:
            entries = [p for p in self.dir.glob("*.npz") if p.is_file()]
        except OSError:
            return 0
        delete_n = len(entries) - self.max_entries
        if delete_n <= 0:
            return 0
        try:
            entries.sort(key=lambda p: p.stat().st_mtime)
        except OSError:
            return 0
        removed = 0
        for p in entries[:delete_n]:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
        return removed

    def _evict_if_needed(self) -> None:
        try:
            self.evict()
        except Exception:
            pass

    def cleanup_orphans(self) -> int:
        """清理原子写的孤儿 .tmp（进程崩溃遗留）。返回删除数。"""
        removed = 0
        try:
            for p in self.dir.glob(".tmp_analysis_*.npz"):
                try:
                    p.unlink()
                    removed += 1
                except OSError:
                    pass
        except OSError:
            pass
        return removed

    def describe(self, key: AnalysisCacheKey) -> dict[str, Any] | None:
        """命中时返回人类可读信息（供 GUI 提示"何时生成"）。"""
        path = self._path_for(key)
        if not path.is_file():
            return None
        try:
            with np.load(path, allow_pickle=False) as d:
                meta = json.loads(str(d["meta_json"]))
            return {
                "created_wall": meta.get("created_wall"),
                "backend_label": meta.get("backend_label"),
                "pause_n": len(meta.get("pauses") or []),
                "speed_n": len(meta.get("speeds") or []),
                # 口径：真实文件字节（GUI 提示框曾误用 len(states)=帧数）
                "mb": round(path.stat().st_size / 1e6, 2),
            }
        except Exception:
            return None
