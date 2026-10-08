#!/usr/bin/env python3
"""P1-RAM skim 代理：把压缩保留段构建成内存 MPEG-TS blob，供真 10x 掠过。

两级预览模型的 Tier2：blob 就绪且代数匹配时，高速播放走引擎内
``ram://`` stream_cb 协议（零磁盘、mpv 侧 cache=no）；未就绪/过期回落
Tier1 活 EDL（速率受 ``preview_step_cap`` 封顶）。暂停/拖动/逐帧永远
回源文件路径，静帧像素精度与本模块无关。

构建命令形态（T0 实证，probe_b0_2mp4_build + probe_b0_tlimit）：
- 单命令全局 PTS 轴：生产滤镜图（select 扁平 OR + setpts 扁平 gap 减法
  + tpad 尾克隆 + setts 哨兵修正）→ ``-f mpegts pipe:1`` → bytearray。
- 输入侧 ``-ss``(首段-1s) + ``-t``(尾段余量截断) + ``-copyts``：
  tpad 克隆要等输入 EOF，无 ``-t`` 时窗口结束后仍解码到文件尾
  （2.mp4 逐段 42-82s 病理的真正机理；截断后同窗口 11.5s）。
- ``-frames:v kept+1`` 提前停机；``-enc_time_base`` 钉时基；
  ``-fps_mode:v passthrough`` 保 PTS 忠实（blob 时间轴 ≡ EDL 虚拟时间轴，
  引擎的虚拟时间↔源帧映射零改动，probe_skim_ram_streamcb tp_end 实证）。
- NVENC 拒绝 GOP=1（strict GOP>B+1）→ GOP 下限 2；``-bf 0`` 保流内
  seek 落点（GOP30 blob 暂停态 seek 精确实证 5.0→5.0）。

足迹与天花板（T0，probe_ram_footprint）：480p GOP30 ≈ 0.45-0.68 MB/保留秒
（内容相关 ±2x）；1080p GOP30 ≈ 2.4；blob 上 10x 干净、14.3x 软解饱和 →
引擎侧 factor ≤12 守卫。2.mp4 规模（51min 源、64 稀疏段）单命令全量
构建 85.3s、帧数/时长帧精确。
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Sequence

from pts_timeline import CertifiedPtsTimeline

import analyzer

# 实测 MB/保留秒 的上界余量（3.mp4/2.mp4 内容差 ±2x）：构建前的
# 预算粗筛用，防止注定超预算的源白跑一次全量构建；实测值仍以
# 构建后的字节数为准。360/240 档按像素比例折算（阶梯降档必须能过
# 预筛，否则降档形同虚设——review G3）。
_EST_MB_PER_KEPT_S = {
    (480, 30): 1.5,
    (480, 2): 3.0,
    (360, 30): 0.9,
    (360, 2): 1.8,
    (240, 30): 0.5,
    (240, 2): 1.0,
    (1080, 30): 4.5,
    (1080, 2): 7.0,
}
_EST_MB_PER_KEPT_S_DEFAULT = 4.5


class SkimBuildError(RuntimeError):
    """skim blob 构建失败（带机器可读 code，GUI 回落 Tier1 不炸播放链）。"""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class SkimBuildCancelled(SkimBuildError):
    def __init__(self, message: str = "skim build cancelled"):
        super().__init__("CANCELLED", message)


@dataclass(frozen=True)
class SkimSpec:
    """构建档位。默认 480p GOP30 = 足迹/天花板推荐档（T0 实测）。

    max_blob_mb=1200：预估闸带 2-3x 内容余量（实测 0.45-0.68MB/保留秒），
    800 预算下 480p 只覆盖 ~8.9 分钟保留内容；owner 4.mp4 现场实测
    预估 895MB 被拒、三档全挂 skim 永不就绪 → 提至 1200（480p≈13.3min /
    360p≈22.2min / 240p≈40min 覆盖）。communicate 双缓冲瞬态峰值≈2x 实际
    blob，典型几十 MB 级；极端大项目最坏 ~2.4GB 瞬态后释放。
    """

    target_h: int = 480        # 缩放目标高度；0 = 保持源分辨率
    gop: int = 30              # NVENC 拒绝 GOP=1 → 下限 2
    quality: int = 6           # analyzer._video_encoder_args 的 0..10 档
    max_blob_mb: float = 1200.0  # B3 预算守卫：构建后超限拒绝入库

    def __post_init__(self) -> None:
        if self.gop < 2:
            raise ValueError("skim GOP must be >= 2 (NVENC rejects GOP=1)")
        if not 0 <= int(self.quality) <= 10:
            raise ValueError("skim quality must be within 0..10")
        if self.target_h < 0:
            raise ValueError("skim target_h must be >= 0")
        if self.max_blob_mb <= 0:
            raise ValueError("skim max_blob_mb must be positive")

    def as_key_tail(self) -> tuple[int, int, int]:
        return (int(self.target_h), int(self.gop), int(self.quality))


@dataclass(frozen=True)
class SkimBlob:
    """一次成功构建的不可变结果。data 入库后禁止原地修改（引擎的
    stream_cb 在 mpv 解复用线程上无锁读引用，换代=整体换引用）。"""

    data: bytearray
    key: tuple
    frames: int           # 实测输出帧数（= kept + 1 尾克隆）
    kept_frames: int
    virtual_s: float      # 压缩时间轴时长（≡ EDL 虚拟时长）
    mb: float
    build_s: float
    spec: SkimSpec


def skim_key(
    source_sha256: str,
    plan_fingerprint: str,
    project_generation: int,
    timeline_revision: int,
    spec: SkimSpec,
) -> tuple:
    """blob 代数键：前 4 元 = 引擎 EDL cache_key 同构（源身份 × 段表 ×
    作用域），尾 3 元 = 构建档位。引擎按前 4 元匹配播放请求。"""
    return (
        str(source_sha256),
        str(plan_fingerprint),
        int(project_generation),
        int(timeline_revision),
        *spec.as_key_tail(),
    )


# 预算降档阶梯：默认 480p（0.45-0.68MB/保留秒 实测），超预算逐级降
# 分辨率重试（GOP/quality 不动——降分辨率对足迹杠杆最大，T0 实测
# 1080p→480p ≈ 3.5x）。全部超预算 → 抛最后的 SkimBuildError 回落 Tier1。
SKIM_SPEC_LADDER: tuple[SkimSpec, ...] = (
    SkimSpec(target_h=480),
    SkimSpec(target_h=360),
    SkimSpec(target_h=240),
)
_BUDGET_CODES = ("BUDGET_EXCEEDED", "BUDGET_ESTIMATE_EXCEEDED")


def _schedule_for(media_info, plan) -> tuple[list[tuple[int, int, int, int]], Fraction]:
    """生产路径段表：认证账本 intervals_for_plan → 归一化（与磁盘导出
    export_pts_schedule 同一套 tick 语义；probe_b0 实证账本 tick ≡ 原始流
    pts，Δ=0）。异常统一包装为 SkimBuildError（GUI 回落 Tier1 不炸链）。"""
    try:
        timeline = CertifiedPtsTimeline.from_validated_media_info(media_info)
        intervals = timeline.intervals_for_plan(plan)
    except SkimBuildError:
        raise
    except Exception as exc:
        raise SkimBuildError(
            "PLAN_MISMATCH",
            f"certified timeline rejected the plan: {type(exc).__name__}: {exc}",
        ) from exc
    dicts = [
        {
            "source_frame_range": (iv.start_frame, iv.end_frame),
            "pts_tick_range": (iv.start_tick, iv.end_tick),
        }
        for iv in intervals
    ]
    try:
        schedule = analyzer._normalize_pts_schedule(
            dicts, time_base=timeline.time_base
        )
    except ValueError as exc:
        raise SkimBuildError("EMPTY_SCHEDULE", str(exc)) from exc
    return schedule, timeline.time_base


def build_filter_graph(schedule: Sequence[tuple[int, int, int, int]],
                       spec: SkimSpec) -> tuple[str, str]:
    """(filter graph, 输出标签)。缩放挂尾，标签随档位切换。"""
    graph = analyzer._pts_select_setpts_video_filter(schedule, clone_guard=True)
    if spec.target_h:
        graph = graph + f";[outv]scale=-2:{int(spec.target_h)}[outv2]"
        return graph, "[outv2]"
    return graph, "[outv]"


def build_command(
    ffmpeg_path: str,
    source_path: str,
    schedule: Sequence[tuple[int, int, int, int]],
    time_base: Fraction,
    spec: SkimSpec,
    *,
    graph_file: str | None = None,
) -> tuple[list[str], int, float]:
    """纯函数：产出 (argv, kept_frames, virtual_s)。测试可直接断言命令形态。

    graph_file 给定时滤镜经 ``-filter_complex_script`` 读文件（调用方负责
    已写好）：Windows CreateProcess 命令行上限 32767 字符，千段级段表的
    select/setpts 表达式必超（生产分批导出同款方案；几 KB 临时文本，
    不是媒体写盘）。
    """
    if not schedule:
        raise SkimBuildError("EMPTY_SCHEDULE", "skim build requires kept spans")
    kept = sum(end - start for start, end, _st, _et in schedule)
    if kept <= 0:
        raise SkimBuildError("EMPTY_SCHEDULE", "skim schedule keeps zero frames")
    virtual_ticks = sum(et - st for _sf, _ef, st, et in schedule)
    virtual_s = float(Fraction(virtual_ticks) * time_base)

    gpu = analyzer._pick_gpu_encoder(ffmpeg_path=ffmpeg_path)
    enc = analyzer._video_encoder_args(
        spec.quality, bool(gpu), gpu or "", ffmpeg_path=ffmpeg_path
    )
    graph, outlabel = build_filter_graph(schedule, spec)

    tb_float = float(time_base)
    seek_s = max(0.0, schedule[0][2] * tb_float - 1.0)
    # 输入侧截断：解码到最后保留段尾 + 余量即停。没有它，tpad 克隆
    # 会拖着解码到真实 EOF（B0 实证 87.5s vs 11.5s）。
    limit_s = (schedule[-1][3] * tb_float - seek_s) + 1.5
    cmd = [
        ffmpeg_path,
        "-hide_banner", "-loglevel", "error", "-nostats",
        "-ss", f"{seek_s:.9f}",
        "-t", f"{limit_s:.9f}",
        "-copyts", "-i", source_path,
    ]
    if graph_file is not None:
        cmd += ["-filter_complex_script", graph_file]
    else:
        cmd += ["-filter_complex", graph]
    cmd += ["-map", outlabel, "-fps_mode:v", "passthrough"]
    cmd += enc
    cmd += ["-g", str(int(spec.gop)), "-bf", "0"]
    cmd += ["-enc_time_base:v", f"{time_base.numerator}/{time_base.denominator}"]
    cmd += ["-frames:v", str(kept + 1)]
    cmd += analyzer._pts_sentinel_fix_args(schedule)
    cmd += ["-an", "-f", "mpegts", "pipe:1"]
    return cmd, kept, virtual_s


def _verify_blob(
    blob: bytearray, kept: int, virtual_s: float, spec: SkimSpec
) -> dict[str, Any]:
    """验证门：帧数 == kept+1（含尾克隆；PyAV 侧 +1 为已知哨兵语义）、
    时长 ≡ 压缩时间轴、TS 同步字节、预算守卫。"""
    if not blob or blob[0] != 0x47:
        raise SkimBuildError(
            "NOT_MPEGTS", "skim build output is not an MPEG-TS stream"
        )
    mb = len(blob) / 1e6
    if mb > spec.max_blob_mb:
        raise SkimBuildError(
            "BUDGET_EXCEEDED",
            f"skim blob {mb:.1f}MB exceeds budget {spec.max_blob_mb:.1f}MB",
            details={"mb": mb, "budget_mb": spec.max_blob_mb},
        )
    try:
        import io

        import av
    except ImportError as exc:  # pragma: no cover - av 是全仓硬依赖
        raise SkimBuildError("PYAV_MISSING", str(exc)) from exc
    try:
        container = av.open(io.BytesIO(bytes(blob)), format="mpegts")
        stream = container.streams.video[0]
        frames = 0
        first_pts = None
        last_pts = None
        for frame in container.decode(stream):
            if first_pts is None:
                first_pts = frame.pts
            last_pts = frame.pts
            frames += 1
        duration = (
            float(container.duration) / av.time_base if container.duration else None
        )
        container.close()
    except Exception as exc:
        raise SkimBuildError("VERIFY_DECODE_FAILED", str(exc)) from exc
    if frames != kept + 1:
        raise SkimBuildError(
            "FRAME_COUNT_MISMATCH",
            f"skim blob has {frames} frames, expected {kept + 1}",
            details={"frames": frames, "expected": kept + 1},
        )
    if duration is None or abs(duration - virtual_s) > max(0.5, virtual_s * 0.02):
        raise SkimBuildError(
            "DURATION_MISMATCH",
            f"skim blob duration {duration} != virtual {virtual_s:.3f}s",
            details={"duration": duration, "virtual_s": virtual_s},
        )
    return {"frames": frames, "duration_s": duration,
            "first_pts90k": first_pts, "last_pts90k": last_pts, "mb": mb}


def build_skim_blob(
    media_info,
    plan,
    *,
    spec: SkimSpec = SkimSpec(),
    project_generation: int = 0,
    timeline_revision: int = 0,
    timeout: float = 1800.0,
    cancel_event: threading.Event | None = None,
    progress_cb: Callable[[float, str], None] | None = None,
) -> SkimBlob:
    """后台工作线程入口：构建 + 验证 + 打包。任何失败抛 SkimBuildError
    （调用方回落 Tier1，不影响播放链）；cancel_event 置位抛
    SkimBuildCancelled 并杀掉 ffmpeg 子进程。"""
    if not getattr(media_info, "frame_pts_authoritative", False):
        raise SkimBuildError(
            "CERTIFICATION_REQUIRED", "skim build requires certified frame PTS"
        )
    if not plan.deleted_ranges:
        raise SkimBuildError(
            "NO_DELETED_RANGES", "skim proxy only serves trimmed timelines"
        )
    # 新鲜度守卫：认证后源文件被替换（同路径不同内容）时，认证 tick 与
    # 实际流不再对应，构建会静默产出错位内容。用引擎同款廉价 stat 快照
    # （size+mtime_ns）而非 source_is_current 的全量 sha 重哈希——多 GB
    # 源哈希要数秒，引擎的 bind/exact-seek 守卫也是 stat 语义，风险面一致。
    try:
        stat_result = Path(media_info.source_path).stat()
        if (
            int(stat_result.st_size) != int(media_info.source_size)
            or int(stat_result.st_mtime_ns) != int(media_info.source_mtime_ns)
        ):
            raise SkimBuildError(
                "SOURCE_CHANGED",
                "source file changed after certification; skim build refused",
            )
    except OSError as exc:
        raise SkimBuildError(
            "SOURCE_CHANGED", f"source not stat-able: {exc}"
        ) from exc
    ffmpeg_path = str(media_info.ffmpeg.path)
    source_path = str(media_info.source_path)

    schedule, time_base = _schedule_for(media_info, plan)
    # 预算粗筛用账本 tick 精确计算保留秒（不假设 60fps 源），
    # MB/保留秒 取实测上界余量；构建后仍以实际字节数为准。
    virtual_est_s = sum(
        end_tick - start_tick for _sf, _ef, start_tick, end_tick in schedule
    ) * float(time_base)
    est_mb = (
        virtual_est_s
        * _EST_MB_PER_KEPT_S.get(
            (int(spec.target_h) if spec.target_h else 1080, int(spec.gop)),
            _EST_MB_PER_KEPT_S_DEFAULT,
        )
    )
    if est_mb > spec.max_blob_mb:
        raise SkimBuildError(
            "BUDGET_ESTIMATE_EXCEEDED",
            f"estimated skim blob {est_mb:.0f}MB exceeds budget "
            f"{spec.max_blob_mb:.0f}MB before build",
            details={"est_mb": est_mb, "budget_mb": spec.max_blob_mb},
        )

    # cancel 接受 threading.Event 或零参 callable：TaskContext 只暴露
    # cancelled 属性不给 Event 本体（G1 现场崩溃教训——AttributeError
    # 让构建秒败，owner 全程停在 Tier1）。
    if cancel_event is None:
        is_cancelled: Callable[[], bool] = lambda: False
    elif callable(getattr(cancel_event, "is_set", None)):
        is_cancelled = cancel_event.is_set
    elif callable(cancel_event):
        is_cancelled = cancel_event
    else:
        raise TypeError("cancel_event must be an Event or a zero-arg callable")

    # 滤镜一律走 script 临时文件（G2）：Windows CreateProcess 命令行
    # 上限 32767 字符，千段级段表的表达式必超（Popen 直接 OSError）。
    graph, _outlabel = build_filter_graph(schedule, spec)
    graph_fd, graph_file = tempfile.mkstemp(prefix="ark-skim-filter-",
                                            suffix=".txt")
    try:
        with os.fdopen(graph_fd, "w", encoding="utf-8") as handle:
            handle.write(graph)
        cmd, kept, virtual_s = build_command(
            ffmpeg_path, source_path, schedule, time_base, spec,
            graph_file=graph_file,
        )
        if progress_cb is not None:
            progress_cb(0.0, f"构建掠过代理（{kept} 帧 / {virtual_s:.1f}s）…")
        t0 = time.perf_counter()
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        # 取消/超时看门狗：ffmpeg 在长间隙解码阶段可能长时间零输出，
        # 阻塞式读会拖住取消响应；communicate 负责无死锁收干双管道，
        # 看门狗 0.3s 粒度检查取消并 kill，兼发进度心跳。
        stop_watch = threading.Event()
        cancelled: list[bool] = []

        def _watchdog() -> None:
            next_beat = 2.0
            while not stop_watch.wait(0.3):
                if proc.poll() is not None:
                    return
                if is_cancelled():
                    cancelled.append(True)
                    proc.kill()
                    return
                elapsed = time.perf_counter() - t0
                if elapsed > timeout:
                    proc.kill()
                    return
                if progress_cb is not None and elapsed >= next_beat:
                    next_beat = elapsed + 2.0
                    progress_cb(min(0.95, elapsed / 60.0), "构建掠过代理…")

        watcher = threading.Thread(target=_watchdog, daemon=True)
        watcher.start()
        try:
            try:
                out, err = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate()
                raise SkimBuildError(
                    "TIMEOUT", f"skim build exceeded {timeout:.0f}s"
                ) from None
        finally:
            stop_watch.set()
            watcher.join(timeout=2.0)
        if cancelled:
            raise SkimBuildCancelled()
        if time.perf_counter() - t0 > timeout and proc.returncode != 0:
            raise SkimBuildError(
                "TIMEOUT", f"skim build exceeded {timeout:.0f}s")
        if proc.returncode != 0:
            raise SkimBuildError(
                "FFMPEG_FAILED",
                f"skim build ffmpeg rc={proc.returncode}: "
                f"{(err or b'')[-300:].decode('utf-8', 'replace')!r}",
            )
        blob = bytearray(out or b"")
        build_s = time.perf_counter() - t0
    finally:
        try:
            os.unlink(graph_file)
        except OSError:
            pass
    verified = _verify_blob(blob, kept, virtual_s, spec)
    key = skim_key(
        media_info.source_sha256,
        plan.fingerprint,
        project_generation,
        timeline_revision,
        spec,
    )
    if progress_cb is not None:
        progress_cb(1.0, "掠过代理就绪")
    return SkimBlob(
        data=blob,
        key=key,
        frames=int(verified["frames"]),
        kept_frames=kept,
        virtual_s=virtual_s,
        mb=float(verified["mb"]),
        build_s=round(build_s, 2),
        spec=spec,
    )


def build_skim_blob_auto(
    media_info,
    plan,
    *,
    ladder: Sequence[SkimSpec] = SKIM_SPEC_LADDER,
    project_generation: int = 0,
    timeline_revision: int = 0,
    timeout: float = 1800.0,
    cancel_event: threading.Event | None = None,
    progress_cb: Callable[[float, str], None] | None = None,
) -> SkimBlob:
    """GUI 工作线程入口：按阶梯自动降档的构建。

    只有预算类失败（BUDGET_*）触发降档重试；取消原样上抛（顶替语义
    依赖它快速退出）；其他失败直接上抛回落 Tier1。
    """
    specs = tuple(ladder) or (SkimSpec(),)
    last_budget_error: SkimBuildError | None = None
    for index, spec in enumerate(specs):
        try:
            return build_skim_blob(
                media_info,
                plan,
                spec=spec,
                project_generation=project_generation,
                timeline_revision=timeline_revision,
                timeout=timeout,
                cancel_event=cancel_event,
                progress_cb=progress_cb,
            )
        except SkimBuildCancelled:
            raise
        except SkimBuildError as exc:
            if exc.code in _BUDGET_CODES and index < len(specs) - 1:
                last_budget_error = exc
                if progress_cb is not None:
                    progress_cb(
                        0.0,
                        f"内存预算超限，降档重试（{spec.target_h}p → "
                        f"{specs[index + 1].target_h}p）…",
                    )
                continue
            raise
    raise last_budget_error or SkimBuildError("EMPTY_LADDER", "no skim spec")
