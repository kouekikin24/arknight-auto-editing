#!/usr/bin/env python3
"""proxy_cache.py — P1 物化代理：把「导出节奏」烘入 1080p 连续文件（磁盘缓存）。

为什么存在（HANDOFF §23/§24）：40x = UI 4x × 业务 10x = 2400 源帧/秒解码，
直解源零余量、硬解更慢、并行预取物理不可达。唯一路径=物化——把业务速度
（pause 删除、1x→2、0.2x→10）提前烘进代理文件，播放端只需按 UI 倍速顺序
解码（UI4x 下恒 240fps 需求，实测 1080p 都有 2 倍余量、零停顿）。

与 skim_cache.py（RAM TS blob，退役）的关系：那个服务「真 10x 掠过」且
易失；本模块服务「任意 UI 倍速全片播放」且落盘跨会话复用。复用同一批
已验证骨架（分批/验证门/取消/原子写），滤镜与编码直接调 analyzer 生产
函数——与导出同一份代码，保证「代理=导出副产品」帧级一致。

核心不变量（干跑 4.mp4 实证，probe_p1_fullbuild）：
- 输出帧数 == 导出帧数（业务速度烘入后，20184/20184 硬对账相等）；
- 代理时长 == 导出虚拟时长（tick 语义，setpts 全局轴）；
- 代理帧 k ↔ 源帧 source_frames[k] 双射（单调、精确，红条/帧号零改动）；
- 播放端 mpv 恒「单文件 + speed=UI 倍速」，无 EDL/无 dsvc/无拼接边界。

磁盘布局（默认 .cache/proxy/）：
  <key>.mp4     代理文件（faststart，播放器可流式读）
  <key>.npz     source_frames 映射数组 + 元数据（JSON 字符串）
  <key>.part   构建中的临时目录前缀（原子写：完成才改名）
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

import analyzer

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ProxyBuildError(RuntimeError):
    """代理构建失败（带机器可读 code；调用方回落既有播放路径）。"""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class ProxyBuildCancelled(ProxyBuildError):
    def __init__(self, message: str = "proxy build cancelled"):
        super().__init__("CANCELLED", message)


# ---------------------------------------------------------------------------
# 计划（导出节奏）与键
# ---------------------------------------------------------------------------

def export_del_mask(
    total_frames: int,
    states: np.ndarray,
    pause_segments: Sequence[dict],
    speed_segments: Sequence[dict],
    clip_segments: Sequence[dict],
    *,
    speedup_1x: bool,
    speedup_02: bool,
    speedup_02_factor: int,
) -> np.ndarray:
    """业务速度烘入的删除掩码——与导出（analyzer._build_delete_mask）同源。

    pause 段删除 + clip 段裁剪 + 1x→2 + 0.2x→factor 全部烘进代理：代理
    文件本身就是「导出帧序列」，播放端只按 UI 倍速播即可复现导出节奏。
    """
    mask = analyzer._build_delete_mask(
        int(total_frames),
        states,
        list(pause_segments),
        list(speed_segments),
        list(clip_segments),
        bool(speedup_1x),
        bool(speedup_02),
        int(speedup_02_factor),
    )
    if not mask.any():
        raise ProxyBuildError(
            "NO_DELETED_RANGES",
            "proxy materialization requires a trimmed timeline (nothing to bake)",
        )
    return mask


def export_schedule_from_mask(
    mask: np.ndarray,
) -> tuple[list[tuple[int, int]], np.ndarray]:
    """删除掩码 → (保留区间 [start,end)，源帧映射数组)。

    ``source_frames[k]`` = 代理第 k 帧对应的源帧号。数组单调递增，
    播放端 seek/读回用它双向换算（searchsorted）。
    """
    kept = np.flatnonzero(~mask).astype(np.int64)
    if kept.size == 0:
        raise ProxyBuildError("EMPTY_SCHEDULE", "timeline keeps zero frames")
    # 连续段分组
    breaks = np.flatnonzero(np.diff(kept) != 1)
    starts = np.concatenate(([0], breaks + 1))
    ends = np.concatenate((breaks + 1, [kept.size]))
    ranges = [
        (int(kept[s]), int(kept[e - 1]) + 1) for s, e in zip(starts, ends)
    ]
    return ranges, kept


def biz_steps_for_frames(
    states: np.ndarray,
    *,
    speedup_1x: bool,
    speedup_02: bool,
    speedup_02_factor: int,
) -> np.ndarray:
    """每个源帧的业务倍率（角标显示用；与 _speedup_mask 同一份语义）。"""
    from frame_types import FRAME_TYPE_0_2X, FRAME_TYPE_1X

    steps = np.ones(states.size, dtype=np.int32)
    if speedup_1x:
        steps[states == FRAME_TYPE_1X] = 2
    if speedup_02:
        steps[states == FRAME_TYPE_0_2X] = max(2, int(speedup_02_factor))
    return steps


def proxy_key(
    source_sha256: str,
    plan_fingerprint: str,
    spec: "ProxySpec",
    *,
    speedup_1x: bool,
    speedup_02: bool,
    speedup_02_factor: int,
    fps: float,
) -> str:
    """缓存键：源身份 × 导出计划 × 业务设置 × 编码规格 × 帧率。

    任一变化（编辑/设置/源替换/规格升级）→ 新键 → 旧代理不可达（LRU 清）。
    """
    import hashlib

    payload = json.dumps(
        {
            "source_sha256": str(source_sha256),
            "plan_fingerprint": str(plan_fingerprint),
            "speedup_1x": bool(speedup_1x),
            "speedup_02": bool(speedup_02),
            "speedup_02_factor": int(speedup_02_factor),
            "fps": round(float(fps), 6),
            **spec.as_key_fields(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class ProxySpec:
    """构建档位（owner 定案 1080p，ASSET §八 10-11）。"""

    target_h: int = 1080          # 输出高度；0=保持源分辨率
    quality: int = 6              # analyzer._video_encoder_args 0..10 档（cq22）
    gop: int = 30                 # 短 GOP（业界代理标准，seek/scrub 友好）
    max_size_mb: float = 4096.0   # 单代理容量守卫（1080p 实测 1.2GB/片）
    format_version: int = 1

    def __post_init__(self) -> None:
        if self.target_h < 0:
            raise ValueError("target_h must be >= 0")
        if not 0 <= int(self.quality) <= 10:
            raise ValueError("quality must be within 0..10")
        if int(self.gop) < 2:
            raise ValueError("gop must be >= 2")
        if self.max_size_mb <= 0:
            raise ValueError("max_size_mb must be positive")

    def as_key_fields(self) -> dict[str, Any]:
        return {
            "target_h": int(self.target_h),
            "quality": int(self.quality),
            "gop": int(self.gop),
            "format_version": int(self.format_version),
        }


@dataclass
class ProxyArtifact:
    """一次成功构建的不可变结果。"""

    path: Path
    key: str
    source_frames: np.ndarray   # (N,) int64，代理帧 k → 源帧
    frames: int
    duration_s: float
    mb: float
    build_s: float
    spec: ProxySpec
    meta: dict = field(default_factory=dict)
    ticks: np.ndarray | None = None  # (N,) int64，代理帧 k 的实际呈现 tick（认证 time_base）
    biz_steps: np.ndarray | None = None  # (N,) int32，代理帧 k 处烘入的业务倍率（角标用）
    time_base: Fraction = Fraction(1, 15360)  # ticks 的时间基（认证轴）
    fps: float = 60.0  # 源平均帧率（均匀近似回退用）

    def source_frame_for_proxy_frame(self, k: int) -> int | None:
        if not (0 <= int(k) < self.frames):
            return None
        return int(self.source_frames[int(k)])

    def proxy_frame_for_source_frame(self, source_frame: int) -> int | None:
        """源帧 → 代理帧（searchsorted；删除段按「下一个保留帧」钳位）。"""
        arr = self.source_frames
        k = int(np.searchsorted(arr, int(source_frame), side="left"))
        if k >= arr.size:
            k = arr.size - 1
        return int(k)


# ---------------------------------------------------------------------------
# 构建
# ---------------------------------------------------------------------------

_DEFAULT_BATCH_SOURCE_SPAN = 4000  # 批窗口（源帧）：约 66 秒@60fps
# 10-11 实测校准（owner「20s 太慢」驱动）：
#   W=120k → 编辑窗口含 4900 段 > 2000 上限被切 3 批、重编 3 批 88s；
#   W=20k  → 每窗 1 批但批跨度 2 万帧，重编要解码穿过整窗（10.7s）；
#   W=4k   → 每窗 1 批（最密窗 531 段 < 2000 上限）且跨度 ≤4k 帧，
#            重编实测 2.0s。编辑只改「所在窗口」，其余窗签名逐位不变
#            （窗口边界固定于源帧空间 [0,W),[W,2W),…，不随编辑漂移）。
# 代价：批数 22→95，最终重封装 +~1s；净收益 10.7s→2.0s 重建。
_DEFAULT_BATCH_RANGES = analyzer._PTS_VIDEO_BATCH_RANGES  # 2000 段/批


def _batch_ranges(
    ranges: Sequence[tuple[int, int]],
    *,
    max_source_span: int = _DEFAULT_BATCH_SOURCE_SPAN,
    max_ranges: int = _DEFAULT_BATCH_RANGES,
) -> list[list[tuple[int, int]]]:
    """按**固定源窗口**切批 + 窗口内段数上限切分。

    关键不变量：窗口边界在源帧空间固定（[0,W),[W,2W),…），不随编辑
    变化——否则前面的编辑会让后面所有批的边界漂移、批级缓存全部失效
    （每次编辑=全量重编）。固定窗口下，编辑只改「所在窗口」的批内容，
    其余批的 (ticks 表, span, 克隆旗标) 逐字节不变 → 直接 concat 复用
    （probe_proxy_skim 实测删段重拼 297ms 零重编码）。

    段数上限=生产导出同款（2000）：单批 select 表达式是 O(段数) 扁平
    OR，实测超限触发 FFmpeg 表达式解析栈溢出（rc=0xC00000FD，13619 段
    塞 2 批实测）。窗口内超限时按顺序切分（同样确定性的）。
    """
    batches: list[list[tuple[int, int]]] = []
    current: list[tuple[int, int]] = []
    current_window: int | None = None
    for r in ranges:
        window = r[0] // max(1, max_source_span)
        if current and (
            window != current_window or len(current) >= max_ranges
        ):
            batches.append(current)
            current = []
        current.append(r)
        current_window = window
    if current:
        batches.append(current)
    return batches


def _batch_signature(
    source_sha256: str,
    spec: ProxySpec,
    batch_ticks: Sequence[tuple[int, int, int, int]],
    *,
    clone_guard: bool,
) -> str:
    """批次内容签名：源身份 × 规格 × 该批 tick 表 × 尾克隆旗标。

    编辑后只有「帧/ticks 表实际变化」的批次签名会变——其余批次直接
    concat 复用（probe_proxy_skim 实测删段重拼 297ms 零重编码）。
    """
    import hashlib

    h = hashlib.sha256()
    h.update(str(source_sha256).encode())
    h.update(json.dumps(spec.as_key_fields(), sort_keys=True).encode())
    h.update(b"|clone" if clone_guard else b"|mid")
    for st, et, sf, ef in (
        (t[2], t[3], t[0], t[1]) for t in batch_ticks
    ):
        h.update(f"{st},{et},{sf},{ef};".encode())
    return h.hexdigest()[:24]


class _BatchCache:
    """批级缓存：<root>/batches/<sig>.mp4。命中=直接参与 concat。

    容量双约束（文件数 + 总字节）：单批 1080p 可达 200MB 级，仅按文件数
    封顶最坏可积压几十 GB；按 LRU mtime 同时压两者（默认 ≤64 文件且
    ≤8GB——约等于 1-2 个完整代理的批次集，够「编辑后只重编一批」复用）。
    """

    def __init__(self, root: Path, *, max_files: int = 256, max_total_mb: float = 8192.0):
        self.root = root / "batches"
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_files = int(max_files)
        self.max_total_mb = float(max_total_mb)

    def get(self, sig: str) -> Path | None:
        p = self.root / f"{sig}.mp4"
        if p.is_file() and p.stat().st_size > 0:
            try:
                os.utime(p, None)
            except OSError:
                pass
            return p
        return None

    def put(self, sig: str, path: Path) -> Path:
        dest = self.root / f"{sig}.mp4"
        tmp = dest.with_name(dest.name + ".tmp")
        shutil.copyfile(path, tmp)
        os.replace(tmp, dest)
        return dest

    def evict(self, keep: set[str]) -> None:
        entries: list[tuple[float, float, Path]] = []
        for p in self.root.glob("*.mp4"):
            if p.stem in keep:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            entries.append((st.st_mtime, st.st_size / 1e6, p))
        # 保留集也要计入总量（避免 keep 集自身无界增长）
        kept_mb = 0.0
        for sig in keep:
            p = self.root / f"{sig}.mp4"
            try:
                kept_mb += p.stat().st_size / 1e6
            except OSError:
                pass
        entries.sort()
        total_mb = kept_mb + sum(size for _m, size, _p in entries)
        count = len(keep) + len(entries)
        for _m, size, p in entries:
            if count <= self.max_files and total_mb <= self.max_total_mb:
                break
            try:
                p.unlink()
            except OSError:
                continue
            total_mb -= size
            count -= 1


def _tick_schedule(
    ranges: Sequence[tuple[int, int]],
    ticks_per_frame: int,
) -> list[tuple[int, int, int, int]]:
    """帧区间 → analyzer 生产滤镜所需的 tick 区间（全局 tick 轴）。

    ticks_per_frame=time_base 分母/fps 取整（4.mp4=256@1/15360）。VFR 源
    由调用方给每帧 tick；此处为统一 CFR 轴的近似，生产路径应优先用
    认证账本（intervals_for_plan）的精确 tick。
    """
    return [
        (s, e, s * ticks_per_frame, e * ticks_per_frame) for s, e in ranges
    ]


_TIMELINE_CACHE: dict[str, Any] = {}


def _certified_tick_schedule(
    media_info,
    ranges: Sequence[tuple[int, int]],
    time_base: Fraction,
) -> list[tuple[int, int, int, int]]:
    """认证账本精确 tick（生产路径）：intervals_for_plan 同一套语义。

    认证时间轴按源 sha 缓存：424k 行构建实测 ~1.5s，编辑重建每次都付
    太贵；认证不可变（源 sha 相同即同一账本）→ 同 sha 复用安全。
    ranges→plan→intervals 仍每次现算（~0.1s）。
    """
    from pts_timeline import CertifiedPtsTimeline
    from timeline_plan import TimelinePlan

    sha = str(getattr(media_info, "source_sha256", "") or "")
    timeline = _TIMELINE_CACHE.get(sha) if sha else None
    if timeline is None:
        timeline = CertifiedPtsTimeline.from_validated_media_info(media_info)
        if sha:
            _TIMELINE_CACHE.clear()  # 只留当前源（换源即换 sha）
            _TIMELINE_CACHE[sha] = timeline
    total = timeline.frame_count
    mask = np.ones(total, dtype=bool)
    for s, e in ranges:
        mask[s:e] = False
    plan = TimelinePlan.from_delete_mask(mask)
    intervals = timeline.intervals_for_plan(plan)
    return [
        (iv.start_frame, iv.end_frame, iv.start_tick, iv.end_tick)
        for iv in intervals
    ]


def output_ticks_for_schedule(
    media_info,
    schedule_ticks: Sequence[tuple[int, int, int, int]],
    source_frames: np.ndarray,
) -> np.ndarray | None:
    """代理输出帧 k 的实际呈现 tick（与 setpts 滤镜同款算术，精确）。

    setpts: out = PTS - start_0 - Σ_{j: PTS ≥ gap_end_j} (gap_end_j - gap_start_j)。
    对第 i 个保留区间的帧：所有更早间隙都已扣除 → out = src_tick(f) - base_i，
    base_i = start_0 + Σ_{j<i} gap_j。逐帧 src_tick 取自认证 PTS 表（VFR 精确）。
    失败返回 None（引擎回落均匀 tick 近似）。
    """
    try:
        timeline = _TIMELINE_CACHE.get(str(getattr(media_info, "source_sha256", "") or ""))
        if timeline is None:
            from pts_timeline import CertifiedPtsTimeline

            timeline = CertifiedPtsTimeline.from_validated_media_info(media_info)
            sha = str(getattr(media_info, "source_sha256", "") or "")
            if sha:
                _TIMELINE_CACHE.clear()
                _TIMELINE_CACHE[sha] = timeline
        rows = timeline.rows
        n = len(rows)
        # 向量化：逐行 int() 转换 42 万次实测 ~0.4s（Python 循环）
        src_tick = np.fromiter(
            (int(r.pts) for r in rows), dtype=np.int64, count=n
        )
        source_frames = np.asarray(source_frames, dtype=np.int64)
        ticks = np.empty(source_frames.size, dtype=np.int64)
        base = int(schedule_ticks[0][2])
        cursor = 0
        for idx, (sf, ef, st, et) in enumerate(schedule_ticks):
            if idx > 0:
                prev = schedule_ticks[idx - 1]
                gap_start = int(prev[3])
                gap_end = int(st)
                if gap_end > gap_start:
                    base += gap_end - gap_start
            count = int(ef) - int(sf)
            seg = slice(cursor, cursor + count)
            ticks[seg] = src_tick[int(sf):int(ef)] - base
            cursor += count
        if cursor != source_frames.size:
            return None
        if ticks.size and ticks[0] != 0:
            return None
        if np.any(np.diff(ticks) <= 0):
            return None
        return ticks
    except Exception:
        return None


def build_proxy(
    media_info,
    ranges: Sequence[tuple[int, int]],
    source_frames: np.ndarray,
    *,
    output_path: str | os.PathLike[str],
    spec: ProxySpec = ProxySpec(),
    batch_source_span: int = _DEFAULT_BATCH_SOURCE_SPAN,
    timeout: float = 3600.0,
    cancel_event: Any = None,
    progress_cb: Callable[[float, str], None] | None = None,
    use_certified_ticks: bool = True,
    biz_steps: np.ndarray | None = None,
    source_sha256: str = "",
) -> ProxyArtifact:
    """构建 + 验证 + 原子落盘（后台工作线程入口）。

    复用 analyzer 生产导出管线：分批 select/setpts 滤镜（-filter_complex_script
    防 Windows 命令行超限）+ NVENC + concat（stream copy）。输出帧数硬对账
    == len(source_frames)；容器后检复用导出同款不变量（克隆位/时间轴锚点）。
    """
    if cancel_event is None:
        is_cancelled: Callable[[], bool] = lambda: False
    elif callable(getattr(cancel_event, "is_set", None)):
        is_cancelled = cancel_event.is_set
    elif callable(cancel_event):
        is_cancelled = cancel_event
    else:
        raise TypeError("cancel_event must be an Event or a zero-arg callable")

    ranges = [(int(s), int(e)) for s, e in ranges]
    source_frames = np.asarray(source_frames, dtype=np.int64)
    expected_frames = int(sum(e - s for s, e in ranges))
    if not ranges or expected_frames <= 0 or source_frames.size == 0:
        raise ProxyBuildError(
            "EMPTY_SCHEDULE", "proxy build requires a non-empty kept schedule"
        )
    if expected_frames != int(source_frames.size):
        raise ProxyBuildError(
            "SCHEDULE_MISMATCH",
            f"ranges keep {expected_frames} frames but mapping has {source_frames.size}",
        )
    if not getattr(media_info, "frame_pts_authoritative", False):
        raise ProxyBuildError(
            "CERTIFICATION_REQUIRED",
            "proxy build requires certified frame PTS",
        )
    # 新鲜度守卫（同 skim_cache）：stat 快照防「认证后源被替换」错位构建。
    try:
        stat_result = Path(media_info.source_path).stat()
        if (
            int(stat_result.st_size) != int(media_info.source_size)
            or int(stat_result.st_mtime_ns) != int(media_info.source_mtime_ns)
        ):
            raise ProxyBuildError(
                "SOURCE_CHANGED",
                "source file changed after certification; proxy build refused",
            )
    except OSError as exc:
        raise ProxyBuildError("SOURCE_CHANGED", f"source not stat-able: {exc}") from exc

    ffmpeg = str(media_info.ffmpeg.path)
    source_path = str(media_info.source_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # tick 轴：认证路径优先（VFR 精确），失败回落统一 CFR 近似。
    # time_base 必须来自认证账本（各源不同：4.mp4=1/15360），不能写死。
    time_base = Fraction(1, 15360)
    fps = 60.0
    try:
        vstream = media_info.video_streams[0]
        if vstream.time_base:
            time_base = Fraction(vstream.time_base)
        if vstream.avg_frame_rate:
            fps = float(vstream.avg_frame_rate)
    except Exception:
        pass
    use_ticks = None
    if use_certified_ticks:
        try:
            use_ticks = _certified_tick_schedule(media_info, ranges, time_base)
        except Exception:
            use_ticks = None
    if use_ticks is None:
        ticks_per_frame = max(1, int(round(float(time_base) ** -1 / fps)))
        use_ticks = _tick_schedule(ranges, ticks_per_frame)
    # 输出帧 k 的实际呈现 tick（VFR 精确；失败则 None → 引擎用均匀近似）
    output_ticks = output_ticks_for_schedule(media_info, use_ticks, source_frames)
    # 代理帧 k 处的业务倍率（角标用；调用方给全片 steps 时切片）
    output_steps = None
    if biz_steps is not None:
        try:
            steps_arr = np.asarray(biz_steps, dtype=np.int32)
            if steps_arr.size == int(source_frames[-1]) + 1 or steps_arr.size >= int(source_frames.max()) + 1:
                output_steps = steps_arr[source_frames]
        except Exception:
            output_steps = None

    gpu = analyzer._pick_gpu_encoder(ffmpeg_path=ffmpeg)
    enc = analyzer._video_encoder_args(
        spec.quality, bool(gpu), gpu or "", ffmpeg_path=ffmpeg
    )

    tmpdir = tempfile.mkdtemp(prefix="ark-proxy-", dir=str(output_path.parent))
    batch_files: list[str] = []
    batch_spans: list[int] = []
    frames_done = 0
    t0 = time.perf_counter()
    # 批级缓存：编辑后只有内容变化的批次重编（其余直接 concat 复用）。
    batch_cache = _BatchCache(output_path.parent) if source_sha256 else None
    keep_sigs: set[str] = set()
    try:
        batches = _batch_ranges(ranges, max_source_span=batch_source_span)
        tick_iter = iter(use_ticks)
        for bi, sub in enumerate(batches):
            if is_cancelled():
                raise ProxyBuildCancelled()
            bframes = sum(e - s for s, e in sub)
            sub_ticks = [next(tick_iter) for _ in sub]
            bspan = sum(et - st for _s, _e, st, et in sub_ticks)
            last_batch = bi == len(batches) - 1
            # 批级复用：签名=源身份×规格×该批 ticks×克隆旗标
            sig = None
            if batch_cache is not None:
                sig = _batch_signature(
                    source_sha256, spec, sub_ticks, clone_guard=last_batch
                )
                keep_sigs.add(sig)
                cached = batch_cache.get(sig)
                if cached is not None:
                    batch_files.append(str(cached))
                    batch_spans.append(bspan)
                    frames_done += bframes
                    if progress_cb is not None:
                        progress_cb(
                            0.02 + 0.86 * (bi / max(1, len(batches))),
                            f"物化代理 {bi + 1}/{len(batches)}（复用批次）…",
                        )
                    continue
            graph = analyzer._pts_select_setpts_video_filter(
                sub_ticks, clone_guard=last_batch
            )
            if spec.target_h:
                graph += f";[outv]scale=-2:{int(spec.target_h)}[outv2]"
                outlabel = "[outv2]"
            else:
                outlabel = "[outv]"
            graph_file = os.path.join(tmpdir, f"pts-filter-{bi:04d}.txt")
            Path(graph_file).write_text(graph, encoding="utf-8")
            batch_path = os.path.join(tmpdir, f"vbatch-{bi:04d}.mp4")
            seek_s = max(0.0, sub_ticks[0][2] * float(time_base) - 1.0)
            cmd = [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-nostats",
                "-ss", f"{seek_s:.9f}", "-copyts", "-i", source_path,
                "-filter_complex_script", graph_file,
                "-map", outlabel, "-fps_mode:v", "passthrough",
            ]
            cmd += enc
            cmd += ["-g", str(int(spec.gop)), "-bf", "0"]
            cmd += [
                "-enc_time_base:v",
                f"{time_base.numerator}/{time_base.denominator}",
                "-video_track_timescale", str(time_base.denominator),
                "-frames:v", str(bframes + (1 if last_batch else 0)),
            ]
            if last_batch:
                cmd += analyzer._pts_sentinel_fix_args(sub_ticks)
            cmd += ["-an", batch_path]
            if progress_cb is not None:
                progress_cb(
                    0.02 + 0.86 * (bi / max(1, len(batches))),
                    f"物化代理 {bi + 1}/{len(batches)}（{bframes} 帧）…",
                )
            _run_ffmpeg(cmd, timeout=timeout, is_cancelled=is_cancelled)
            if batch_cache is not None and sig is not None:
                try:
                    batch_cache.put(sig, Path(batch_path))
                except OSError:
                    pass  # 缓存写失败不影响本次构建（纯加速层）
            batch_files.append(batch_path)
            batch_spans.append(bspan)
            frames_done += bframes
        if batch_cache is not None:
            batch_cache.evict(keep_sigs)
        if is_cancelled():
            raise ProxyBuildCancelled()
        # concat（stream copy，零重编码）
        concat_list = os.path.join(tmpdir, "concat.txt")
        lines: list[str] = []
        for i, bp in enumerate(batch_files):
            # concat demuxer 相对路径按列表文件所在目录解析 → 用绝对路径。
            # duration=tick 跨度（该批在虚拟时间轴上的长度）：这是唯一与
            # ticks 数组自洽的值——10-11 实测，改用「文件真实时长」会让
            # 每批多算 1 帧克隆时长、95 批累计漂移 1.5s（merge 336.05 vs
            # 期望 337.60 对账失败）；tick 跨度版本 22 批实测 336.42 精确。
            lines.append(f"file '{Path(bp).resolve().as_posix()}'")
            if i < len(batch_files) - 1:
                lines.append(f"duration {batch_spans[i] * float(time_base):.9f}")
        Path(concat_list).write_text("\n".join(lines), encoding="utf-8")
        if progress_cb is not None:
            progress_cb(0.90, "拼接代理批次…")
        tmp_out = os.path.join(tmpdir, "proxy.mp4")
        _run_ffmpeg(
            [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", concat_list,
                "-c", "copy", tmp_out,
            ],
            timeout=timeout,
            is_cancelled=is_cancelled,
        )
        build_s = time.perf_counter() - t0
        # ---- 验证门 ----
        if progress_cb is not None:
            progress_cb(0.94, "验证代理（帧数/时长对账）…")
        meta = verify_proxy(
            tmp_out,
            expected_frames=expected_frames,
            expected_duration_s=sum(batch_spans) * float(time_base),
        )
        mb = os.path.getsize(tmp_out) / 1e6
        if mb > spec.max_size_mb:
            raise ProxyBuildError(
                "BUDGET_EXCEEDED",
                f"proxy {mb:.0f}MB exceeds budget {spec.max_size_mb:.0f}MB",
                details={"mb": mb, "budget_mb": spec.max_size_mb},
            )
        # ---- 原子落盘 ----
        if is_cancelled():
            raise ProxyBuildCancelled()
        os.replace(tmp_out, output_path)
        if progress_cb is not None:
            progress_cb(1.0, f"代理就绪（{expected_frames} 帧 / {mb:.0f}MB）")
        return ProxyArtifact(
            path=output_path,
            key="",
            source_frames=source_frames,
            frames=expected_frames,
            duration_s=float(meta["duration_s"]),
            mb=float(mb),
            build_s=float(build_s),
            spec=spec,
            meta=meta,
            ticks=output_ticks,
            biz_steps=output_steps,
            time_base=time_base,
            fps=fps,
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _run_ffmpeg(
    cmd: list[str],
    *,
    timeout: float,
    is_cancelled: Callable[[], bool],
) -> None:
    """跑一条 ffmpeg 命令，轮询取消与硬超时（模式对齐 analyzer）。"""
    stderr_file = tempfile.TemporaryFile()
    process: subprocess.Popen | None = None
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            stderr=stderr_file,
            creationflags=_NO_WINDOW,
        )
        while True:
            if is_cancelled():
                raise ProxyBuildCancelled()
            try:
                returncode = process.wait(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() - started >= timeout:
                    raise ProxyBuildError(
                        "FFMPEG_TIMEOUT", f"ffmpeg exceeded {timeout:.0f}s"
                    )
        if is_cancelled():
            raise ProxyBuildCancelled()
        stderr_file.seek(0)
        stderr = stderr_file.read()
        if returncode != 0:
            raise ProxyBuildError(
                "FFMPEG_FAILED",
                f"ffmpeg rc={returncode}: {stderr[-800:].decode('utf-8', 'replace')}",
            )
    except BaseException:
        if process is not None:
            try:
                process.kill()
            except Exception:
                pass
        raise
    finally:
        stderr_file.close()


def verify_proxy(
    path: str | os.PathLike[str],
    *,
    expected_frames: int,
    expected_duration_s: float,
    duration_tolerance_s: float = 0.5,
    deep: bool = False,
) -> dict[str, Any]:
    """验证门：帧数硬对账 + 时长对账（默认快速路径，deep=全解码）。

    10-11 性能实测（20227 帧 1080p 代理）：全解码 **111.0s** / 解复用计数
    **0.4s** / 容器元数据 **0.02s**——三者帧数逐位一致（本管线 `-bf 0`
    纯视频流一包恰一帧）。构建验证跑在**每次编辑重建**上，故默认：
    ①容器元数据（nb_frames/duration，20ms 级）为主判；
    ②元数据缺失/为 0 时回落解复用计数（0.4s）；
    ③抽样解码首 30 帧兜底「包可数但流不可解码」的极端损坏；
    ④`deep=True` 保留全解码口径（诊断/取证用）。
    帧数容差 ±1（tpad 末端克隆，PyAV 视角的已知哨兵语义——实际播放
    时长不受影响，probe_proxy_skim 同款口径）。
    """
    try:
        import av
    except ImportError as exc:  # pragma: no cover
        raise ProxyBuildError("PYAV_MISSING", str(exc)) from exc
    frames = 0
    duration = None
    method = "metadata"
    try:
        container = av.open(str(path))
        stream = container.streams.video[0]
        duration = (
            float(container.duration) / av.time_base if container.duration else None
        )
        if deep:
            for _frame in container.decode(stream):
                frames += 1
            method = "decode"
        else:
            declared = stream.frames or 0
            if declared > 0:
                frames = int(declared)
            else:
                # 元数据缺失 → 解复用计数回落
                frames = sum(1 for packet in container.demux(stream) if packet.size)
                method = "packets"
        container.close()
    except Exception as exc:
        raise ProxyBuildError("VERIFY_DECODE_FAILED", str(exc)) from exc
    if not deep:
        # 抽样解码首 30 帧（兜底「包可数但流不可解码」的极端损坏）
        try:
            container = av.open(str(path))
            stream = container.streams.video[0]
            decoded = 0
            for _frame in container.decode(stream):
                decoded += 1
                if decoded >= 30:
                    break
            container.close()
        except Exception as exc:
            raise ProxyBuildError("VERIFY_DECODE_FAILED", str(exc)) from exc
    if abs(frames - expected_frames) > 1:
        raise ProxyBuildError(
            "FRAME_COUNT_MISMATCH",
            f"proxy has {frames} frames, expected {expected_frames}",
            details={"frames": frames, "expected": expected_frames,
                     "method": method},
        )
    if duration is None or abs(duration - expected_duration_s) > duration_tolerance_s:
        raise ProxyBuildError(
            "DURATION_MISMATCH",
            f"proxy duration {duration} != expected {expected_duration_s:.3f}s",
            details={"duration": duration, "expected_s": expected_duration_s},
        )
    return {"frames": frames, "duration_s": duration, "method": method}


# ---------------------------------------------------------------------------
# 磁盘缓存（LRU）
# ---------------------------------------------------------------------------

class ProxyCache:
    """.cache/proxy/ 下的代理缓存：load/store/evict。

    原子写（同目录临时文件 + os.replace）；LRU 按访问 mtime；容量上限
    max_total_mb。损坏/版本不符的条目 load 返回 None（调用方重建）。
    """

    META_SUFFIX = ".npz"

    def __init__(self, root: str | os.PathLike[str], *, max_total_mb: float = 20480.0):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_total_mb = float(max_total_mb)
        self._lock = threading.RLock()

    def _paths(self, key: str) -> tuple[Path, Path]:
        return self.root / f"{key}.mp4", self.root / f"{key}{self.META_SUFFIX}"

    def load(self, key: str) -> ProxyArtifact | None:
        mp4, meta_path = self._paths(key)
        if not mp4.is_file() or not meta_path.is_file():
            return None
        try:
            with np.load(meta_path, allow_pickle=False) as data:
                source_frames = np.asarray(data["source_frames"], dtype=np.int64)
                meta = json.loads(str(data["meta_json"]))
                ticks = (
                    np.asarray(data["ticks"], dtype=np.int64)
                    if "ticks" in data.files
                    else None
                )
                biz_steps = (
                    np.asarray(data["biz_steps"], dtype=np.int32)
                    if "biz_steps" in data.files
                    else None
                )
            if int(meta.get("format_version", -1)) != ProxySpec().format_version:
                return None
            if source_frames.ndim != 1 or source_frames.size == 0:
                return None
            if ticks is not None and ticks.size != source_frames.size:
                ticks = None
            if biz_steps is not None and biz_steps.size != source_frames.size:
                biz_steps = None
            spec = ProxySpec(**meta["spec"])
            tb_text = str(meta.get("time_base") or "1/15360")
            try:
                num, den = tb_text.split("/")
                time_base = Fraction(int(num), int(den))
            except (TypeError, ValueError):
                time_base = Fraction(1, 15360)
            try:
                fps = float(meta.get("fps") or 60.0)
            except (TypeError, ValueError):
                fps = 60.0
        except Exception:
            return None
        try:
            os.utime(mp4, None)  # LRU 触达
        except OSError:
            pass
        return ProxyArtifact(
            path=mp4,
            key=key,
            source_frames=source_frames,
            frames=int(meta.get("frames", source_frames.size)),
            duration_s=float(meta.get("duration_s", 0.0)),
            mb=float(meta.get("mb", 0.0)),
            build_s=float(meta.get("build_s", 0.0)),
            spec=spec,
            meta=dict(meta.get("verify", {})),
            ticks=ticks,
            biz_steps=biz_steps,
            time_base=time_base,
            fps=fps,
        )

    def store(self, key: str, artifact: ProxyArtifact) -> ProxyArtifact:
        """把构建产物登记进缓存（原子：先写 meta，mp4 已就位）。

        不修改入参（返回新 artifact）；调用方可安全复用于多次 store
        （测试/多键场景）。源 mp4 与目标同文件时跳过拷贝。
        """
        mp4, meta_path = self._paths(key)
        with self._lock:
            try:
                same_file = artifact.path.resolve() == mp4.resolve()
            except OSError:
                same_file = False
            if not same_file:
                if not artifact.path.is_file():
                    raise ProxyBuildError(
                        "STORE_SOURCE_MISSING",
                        f"artifact source {artifact.path} does not exist",
                    )
                tmp = mp4.with_name(mp4.name + ".tmp")
                shutil.copyfile(artifact.path, tmp)
                os.replace(tmp, mp4)
            meta = {
                "format_version": artifact.spec.format_version,
                "spec": artifact.spec.as_key_fields(),
                "frames": int(artifact.frames),
                "duration_s": float(artifact.duration_s),
                "mb": float(artifact.mb),
                "build_s": float(artifact.build_s),
                "verify": dict(artifact.meta),
                "source_frames_sha": _array_sha(artifact.source_frames),
                "time_base": f"{artifact.time_base.numerator}/{artifact.time_base.denominator}",
                "fps": float(artifact.fps),
            }
            payload = {
                "source_frames": artifact.source_frames,
                "meta_json": np.asarray(json.dumps(meta, ensure_ascii=False)),
            }
            if artifact.ticks is not None:
                payload["ticks"] = np.asarray(artifact.ticks, dtype=np.int64)
            if artifact.biz_steps is not None:
                payload["biz_steps"] = np.asarray(artifact.biz_steps, dtype=np.int32)
            # np.savez 会给不以 .npz 结尾的名字补后缀 → 临时名也以 .npz 结尾
            tmp_meta = meta_path.with_name(meta_path.name + ".tmp.npz")
            np.savez(tmp_meta, **payload)
            os.replace(tmp_meta, meta_path)
            self._evict_locked(protect=key)
        return ProxyArtifact(
            path=mp4,
            key=key,
            source_frames=artifact.source_frames,
            frames=artifact.frames,
            duration_s=artifact.duration_s,
            mb=artifact.mb,
            build_s=artifact.build_s,
            spec=artifact.spec,
            meta=dict(artifact.meta),
            ticks=artifact.ticks,
            biz_steps=artifact.biz_steps,
        )

    def _evict_locked(self, *, protect: str | None = None) -> None:
        """LRU 清理；protect=本次刚入库的键（即便超预算也不自删，
        否则单文件超限时 store 返回的 artifact 指向已删文件）。"""
        entries: list[tuple[float, float, str]] = []
        for mp4 in self.root.glob("*.mp4"):
            if protect is not None and mp4.stem == protect:
                continue
            try:
                st = mp4.stat()
            except OSError:
                continue
            entries.append((st.st_mtime, st.st_size / 1e6, mp4.stem))
        protected_mb = 0.0
        if protect is not None:
            pmp4, _pm = self._paths(protect)
            try:
                protected_mb = pmp4.stat().st_size / 1e6
            except OSError:
                protected_mb = 0.0
        total = sum(size for _m, size, _k in entries) + protected_mb
        if total <= self.max_total_mb:
            return
        entries.sort()  # 旧 mtime 先删
        for _m, size, key in entries:
            if total <= self.max_total_mb:
                break
            mp4, meta_path = self._paths(key)
            for p in (mp4, meta_path):
                try:
                    p.unlink()
                except OSError:
                    pass
            total -= size

    def contains(self, key: str) -> bool:
        mp4, meta_path = self._paths(key)
        return mp4.is_file() and meta_path.is_file()


def _array_sha(arr: np.ndarray) -> str:
    import hashlib

    return hashlib.sha256(np.ascontiguousarray(arr, dtype=np.int64).tobytes()).hexdigest()[:16]


def expected_proxy_key(
    media_info,
    states: np.ndarray,
    pause_segments: Sequence[dict],
    speed_segments: Sequence[dict],
    clip_segments: Sequence[dict],
    *,
    speedup_1x: bool,
    speedup_02: bool,
    speedup_02_factor: int,
    fps: float,
    spec: ProxySpec | None = None,
) -> str:
    """构建端将使用的键（**单一权威**：GUI 门/调度器必须用这个）。

    背景（10-11 真机 E2E 抓出）：调度器/播放门曾用「剪辑计划」指纹
    （_build_timeline_plan，不含业务速度）算键，而构建端用「导出节奏」
    掩码（含 1x→2、0.2x→10）算键——两者不同 → 引擎装的是构建键、
    GUI 比的是剪辑键 → 永不匹配、代理装了却走不了。现在三处（构建、
    调度、播放门）统一走本函数。
    """
    from timeline_plan import TimelinePlan

    if spec is None:
        spec = ProxySpec()
    mask = export_del_mask(
        int(states.size),
        states,
        pause_segments,
        speed_segments,
        clip_segments,
        speedup_1x=speedup_1x,
        speedup_02=speedup_02,
        speedup_02_factor=speedup_02_factor,
    )
    plan = TimelinePlan.from_delete_mask(mask)
    return proxy_key(
        str(media_info.source_sha256),
        str(plan.fingerprint),
        spec,
        speedup_1x=speedup_1x,
        speedup_02=speedup_02,
        speedup_02_factor=speedup_02_factor,
        fps=fps,
    )


def build_proxy_for_plan(
    media_info,
    states: np.ndarray,
    pause_segments: Sequence[dict],
    speed_segments: Sequence[dict],
    clip_segments: Sequence[dict],
    *,
    speedup_1x: bool,
    speedup_02: bool,
    speedup_02_factor: int,
    fps: float,
    spec: ProxySpec = ProxySpec(),
    cache_root: str | os.PathLike[str] | None = None,
    timeout: float = 3600.0,
    cancel_event: Any = None,
    progress_cb: Callable[[float, str], None] | None = None,
) -> ProxyArtifact:
    """全链入口：计划 → 键 → 缓存命中/构建 → 入库。

    命中：直接返回（跨会话复用）。未命中：构建（分批+验证）→ 原子入库。
    """
    mask = export_del_mask(
        int(states.size),
        states,
        pause_segments,
        speed_segments,
        clip_segments,
        speedup_1x=speedup_1x,
        speedup_02=speedup_02,
        speedup_02_factor=speedup_02_factor,
    )
    ranges, source_frames = export_schedule_from_mask(mask)
    key = expected_proxy_key(
        media_info,
        states,
        pause_segments,
        speed_segments,
        clip_segments,
        speedup_1x=speedup_1x,
        speedup_02=speedup_02,
        speedup_02_factor=speedup_02_factor,
        fps=fps,
        spec=spec,
    )
    if cache_root is None:
        cache_root = Path(__file__).parent / ".cache" / "proxy"
    cache = ProxyCache(cache_root)
    hit = cache.load(key)
    if hit is not None:
        if progress_cb is not None:
            progress_cb(1.0, f"代理缓存命中（{hit.frames} 帧）")
        return hit
    mp4_path, _meta = cache._paths(key)
    steps = biz_steps_for_frames(
        np.asarray(states),
        speedup_1x=speedup_1x,
        speedup_02=speedup_02,
        speedup_02_factor=speedup_02_factor,
    )
    artifact = build_proxy(
        media_info,
        ranges,
        source_frames,
        output_path=mp4_path,
        spec=spec,
        timeout=timeout,
        cancel_event=cancel_event,
        progress_cb=progress_cb,
        biz_steps=steps,
        source_sha256=str(media_info.source_sha256),
    )
    return cache.store(key, artifact)
