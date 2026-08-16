# video_io.py — 视频 IO 线程
# 整个程序唯一持有 cv2.VideoCapture 的地方，
# 通过命令队列接收指令，把解码好的帧放入 frame_q。
#
# 主线程  ──cmd_q──▶  _VideoIOThread（唯一cap）──frame_q──▶  渲染循环

from __future__ import annotations

import cv2
import numpy as np
import threading
import time
from collections import deque
from queue import Queue, Empty

from frame_types import FRAME_TYPE_1X, FRAME_TYPE_0_2X

# ---------- 计时时钟 ----------
# 必须用 perf_counter，不能用 monotonic。
# 实测本机（Win + CPython 3.11）：
#   monotonic    = GetTickCount64()，resolution 15.625ms（实测步长只有 15/16ms 两种）
#   perf_counter = QueryPerformanceCounter()，resolution 0.1us
# 用 monotonic 时 lag = now - (t0 + slots*fd) 的两个端点各带 [0,15.625) 量化误差，
# 合成误差 ±15.625ms ≈ ±0.94 个 frame_dur(16.67ms)，而追帧判定阈值恰好是 1*frame_dur
# ⇒ 真实只落后 2ms 的拍会被测成落后 18ms，触发追帧并真的丢帧（late1 与 drop 一起被抬高）。
# 同时所有 present_ms 都会被量化到 15.625ms 网格上（15/31/47/62/78/93...），
# 制造出「便宜拍 2-5ms / 贵拍 31ms+」的假双峰。改用 perf_counter 后 read() 实测 p50=2.3ms。
_now = time.perf_counter

# ---------- 命令类型常量（导出供外部使用）----------
CMD_SEEK        = 'seek'         # 精确 seek，播放时应用裁剪区跳过
CMD_SEEK_LATEST = 'seek_latest'  # 节流 seek：丢弃积压旧命令，允许停在裁剪区内
CMD_PLAY        = 'play'
CMD_STOP        = 'stop'
CMD_QUIT        = 'quit'
CMD_SET_PACE_MODE = 'set_pace_mode'  # params: mode 'opt'|'base'


class _VideoIOQuit(Exception):
    """Internal control-flow signal used to stop the IO worker cleanly.

    ``SystemExit`` technically terminates a thread, but pytest (and any
    embedding application that installs thread exception hooks) reports it as
    an unhandled thread exception.  A private sentinel keeps the shutdown
    path explicit while allowing :meth:`run` to consume it before releasing
    the capture.
    """


class VideoIOThread(threading.Thread):
    """
    单一 cap 的视频 IO 线程。
    所有 cv2.VideoCapture 操作都在本线程内串行执行，
    彻底避免 FFmpeg async_lock 崩溃。
    """

    # 小跨度：只 grab（用户拖动附近 / 普通步进）
    # 实测（1080p60 H.264）：grab ≈ 0.53ms/帧，而 cap.set() 精准 seek 无论跨度
    # 多大都是固定 ~60ms。因此损益平衡点在 ~100-120 帧，而非原来的 30。
    # 阈值设为 100：100 帧以内改用 grab（≈53ms 且无尖峰），超过才 seek。
    # 这样消除裁剪边界 seek 尖峰，且不会像「吸收」那样把裁剪帧显示出来。
    _GRAB_SEEK_THRESHOLD = 100
    # 追帧路径的 seek 阈值。追帧只 grab 不解码显示，单帧更便宜，
    # 因此可略高于显示路径；两者都基于同一实测损益平衡点(~100-120帧)。
    _CATCHUP_SEEK_THRESHOLD = 120
    # 预览显示边长上限
    _MAX_PREVIEW_EDGE = 1280
    # 单次追帧上限。大裁剪尖峰后会直接软锚；普通落后也只小步追，
    # 避免一次 discard 连环叠出 drop%（进「优」主闸是 drop≤1%）。
    _MAX_CATCHUP_SLOTS = 4
    # S1: max source frames advanced per display tick under biz speedup
    _PREVIEW_STEP_CAP = 3
    # 微抖迟到（偏严，兼容旧 late%）
    _LATE_LAG_RATIO = 0.25
    # _interruptible_sleep 的分片长度 = 命令响应延迟上限。
    # 2ms 远小于一拍(16.67ms)，对节奏无影响；见 _interruptible_sleep 注释。
    _SLEEP_SLICE = 0.002
    # present 耗时超过该 ms 记尖峰样本。
    # 旧值 40.0 是在 monotonic(15.625ms 粒度)下定的，等于「>=3 ticks」，
    # 把 16-46ms 区间的尖峰全部挡在门外，spikes 环里只剩 seek，
    # 于是得出「残留尖峰全部是跳裁剪」的错误结论。
    # 换 perf_counter 后按真实预算定：8ms ≈ 半拍，read() p50 仅 2.3ms。
    _SPIKE_PRESENT_MS = 8.0
    # lag 超过 1 拍也记尖峰
    _SPIKE_RING = 40
    _SAMPLE_RING = 512  # present_ms / lag_ms 样本，用于 p95
    # 预览吸收小裁剪段：跨度 ≤ 该值的裁剪区「正常播过」而不跳。
    # 注意：被吸收的帧会真的显示出来，那正是暂停闪屏的来源，因此默认关闭(0)。
    # 保留该开关仅为调试；消除 seek 尖峰请用下面的 _GRAB_SEEK_THRESHOLD。
    _SKIP_TRIM_MIN_SPAN = 0

    def __init__(self, path: str, frame_q: Queue, *, legacy_timing: bool = False):
        super().__init__(daemon=True)
        self.path    = path
        self.frame_q = frame_q
        self.cmd_q: Queue = Queue()
        self._quit_lock = threading.Lock()
        self._quit_requested = threading.Event()
        # ``close()`` can release a capture before ``start()``.  Keep release
        # ownership explicit so a later ``run()`` cannot release it twice.
        self._cap_release_lock = threading.Lock()
        self._cap_released = False

        # A/B 计时层。legacy_timing=True 复现修复前行为：
        #   时钟 = monotonic(15.625ms 粒度)，等待 = cmd_q.get(timeout=)（超睡 14ms）
        # 仅用于对照测量，正常运行务必保持 False。
        self._legacy_timing = bool(legacy_timing)
        self._now = time.monotonic if self._legacy_timing else time.perf_counter

        self._cap  = cv2.VideoCapture(path)
        # 解码线程数：仅读取用于诊断，不强设。
        # 实测本机 FFmpeg 后端已默认取满核心（16），且 set() 在 open 之后无效；
        # 用 open 参数强设 4 反而把 decode max 从 3.4ms 抬到 11.9ms，故不干预。
        try:
            self._cap_threads = int(self._cap.get(cv2.CAP_PROP_N_THREADS) or 0)
        except Exception:
            self._cap_threads = 0
        self.fps   = self._cap.get(cv2.CAP_PROP_FPS) or 30.0
        self.total = int(self._cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self._cur  = 0

        self._playing     = False
        self._play_params: dict = {}
        self._play_t0: float | None = None
        self._play_slots: int = 0
        # opt=当前优化；base=#9 式基线（sleep 计时 + 旧阈值 seek，仍记统计）
        self._pace_mode: str = 'opt'
        self._stats_lock = threading.Lock()
        # 本拍归因（present 前写入）
        self._slot_meta: dict = {
            "skip_trim_n": 0,
            "step_skip_n": 0,
            "used_seek": False,
            "seek_n": 0,
            "path": "present",
        }
        self._reset_perf_stats_unlocked()

    # ------------------------------------------------------------------
    # cap 操作（仅本线程调用）
    # ------------------------------------------------------------------

    def _seek_cap(self, frame_idx: int, note: bool = True):
        """用户拖动 / 起播：直接定位（允许任意方向）。"""
        frame_idx = int(max(0, min(frame_idx, max(0, self.total - 1))))
        self._cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        self._cur = frame_idx
        if note:
            self._slot_meta["used_seek"] = True
            self._slot_meta["seek_n"] = self._slot_meta.get("seek_n", 0) + 1
            self._note_seek()

    def _advance_to(
        self,
        target: int,
        *,
        allow_seek: bool = True,
        prefer_grab: bool = False,
    ) -> str:
        """
        仅向前推进到 target（播放/追帧/跳裁剪）。
        opt：大跨度可向前 seek（阈值 _CATCHUP_SEEK_THRESHOLD）。
        base：#9 行为 — 跨度 > _GRAB_SEEK_THRESHOLD 即 POS_FRAMES（不读 report 纠偏）。
        返回：'grab' | 'seek' | 'none'
        """
        if self.total <= 0:
            return "none"
        target = int(max(0, min(target, self.total - 1)))
        if target <= self._cur:
            return "none"
        n = target - self._cur
        base = self._pace_mode == "base"
        if base:
            thr = self._GRAB_SEEK_THRESHOLD
        else:
            thr = self._CATCHUP_SEEK_THRESHOLD if prefer_grab else self._GRAB_SEEK_THRESHOLD
        if allow_seek and n > thr:
            if base:
                # #9：直接 set 到目标并信任逻辑帧
                self._seek_cap(target, note=True)
                return "seek"
            prev = self._cur
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, target)
            self._slot_meta["used_seek"] = True
            self._slot_meta["seek_n"] = self._slot_meta.get("seek_n", 0) + 1
            self._note_seek()
            reported = int(round(self._cap.get(cv2.CAP_PROP_POS_FRAMES)))
            if prev < reported <= target:
                self._cur = reported
            else:
                self._cur = target
            while self._cur < target:
                if not self._cap.grab():
                    break
                self._cur += 1
            return "seek"
        for _ in range(n):
            if not self._cap.grab():
                break
            self._cur += 1
        return "grab"

    def _grab_n(
        self,
        n: int,
        prefer_grab: bool = False,
        allow_seek: bool = True,
    ) -> str:
        """兼容旧调用：向前跳过 n 帧。"""
        if n <= 0:
            return "none"
        return self._advance_to(
            self._cur + n,
            allow_seek=allow_seek,
            prefer_grab=prefer_grab,
        )

    def _read_one(self):
        ret, frame = self._cap.read()
        if ret:
            self._cur += 1
        return ret, frame

    # ------------------------------------------------------------------
    # 静态辅助
    # ------------------------------------------------------------------

    @staticmethod
    def jump_pause(cur: int, pause_segs: list) -> int:
        while True:
            jumped = cur
            for ti, to in pause_segs:
                if to > ti and ti <= cur < to:
                    jumped = to
                    break
            if jumped == cur:
                return cur
            cur = jumped

    @staticmethod
    def speed_step(cur: int, speed_segs: list,
                   speedup_1x: bool, speedup_02: bool, factor_02: int) -> int:
        for s, e, t in speed_segs:
            if s <= cur <= e:
                if t == FRAME_TYPE_1X and speedup_1x:
                    return 2
                if t == FRAME_TYPE_0_2X and speedup_02:
                    return max(2, factor_02)
        return 1

    # ------------------------------------------------------------------
    # 队列 / 统计
    # ------------------------------------------------------------------

    def _flush_frame_q(self):
        while True:
            try:
                self.frame_q.get_nowait()
            except Empty:
                break

    def _reset_perf_stats_unlocked(self) -> None:
        self._perf = {
            "presented": 0,
            "discarded": 0,
            "late": 0,            # lag > 0.25 * frame_dur（微抖，兼容旧字段）
            "late1": 0,           # lag > 1 * frame_dur
            "late2": 0,           # lag > 2 * frame_dur
            "catchup_events": 0,
            "pace_resets": 0,     # 软重锚次数（旧名保留）
            "hard_resets": 0,     # 真正 slots 清零（应极少）
            "seek_count": 0,
            "q_drop": 0,
            "present_ms_sum": 0.0,
            "present_ms_max": 0.0,
            "lag_ms_max": 0.0,
            "play_wall_t0": None,
            "play_wall_end": None,
            "playback_active": False,
            "play_end_reason": None,
            "pace_mode": "opt",
            "rate_play_frames": 0,
            "skip_trim_absorbed": 0,
            "rate_trim_frames": 0,
            "spikes": deque(maxlen=self._SPIKE_RING),
            "present_ms_samples": deque(maxlen=self._SAMPLE_RING),
            "lag_ms_samples": deque(maxlen=self._SAMPLE_RING),
        }

    def reset_perf_stats(self) -> None:
        with self._stats_lock:
            self._reset_perf_stats_unlocked()

    def begin_perf_segment(self) -> None:
        self._note_play_start_stats()

    def is_playback_active(self) -> bool:
        with self._stats_lock:
            return bool(self._perf.get("playback_active"))

    @staticmethod
    def _percentile(samples: list[float], p: float) -> float:
        if not samples:
            return 0.0
        xs = sorted(samples)
        if len(xs) == 1:
            return float(xs[0])
        k = (len(xs) - 1) * (p / 100.0)
        f = int(k)
        c = min(f + 1, len(xs) - 1)
        if f == c:
            return float(xs[f])
        return float(xs[f] + (xs[c] - xs[f]) * (k - f))

    def snapshot_perf(self) -> dict:
        with self._stats_lock:
            p = dict(self._perf)
            spikes = list(p.get("spikes") or [])
            present_samples = list(p.get("present_ms_samples") or [])
            lag_samples = list(p.get("lag_ms_samples") or [])

        presented = int(p["presented"])
        discarded = int(p["discarded"])
        late = int(p["late"])
        late1 = int(p.get("late1", 0))
        late2 = int(p.get("late2", 0))
        wall_s = 0.0
        t0 = p.get("play_wall_t0")
        t1 = p.get("play_wall_end")
        if t0 is not None:
            end = float(t1) if t1 is not None else self._now()
            wall_s = max(0.0, end - float(t0))
        avg_ms = (p["present_ms_sum"] / presented) if presented else 0.0
        late_pct = (100.0 * late / presented) if presented else 0.0
        late1_pct = (100.0 * late1 / presented) if presented else 0.0
        late2_pct = (100.0 * late2 / presented) if presented else 0.0
        drop_pct = (
            100.0 * discarded / (presented + discarded)
            if (presented + discarded) else 0.0
        )
        return {
            "presented": presented,
            "discarded": discarded,
            "late": late,
            "late_pct": late_pct,
            "late1": late1,
            "late1_pct": late1_pct,
            "late2": late2,
            "late2_pct": late2_pct,
            "drop_pct": drop_pct,
            "catchup_events": int(p["catchup_events"]),
            "pace_resets": int(p["pace_resets"]),
            "hard_resets": int(p.get("hard_resets", 0)),
            "seek_count": int(p.get("seek_count", 0)),
            "q_drop": int(p["q_drop"]),
            "present_ms_avg": avg_ms,
            "present_ms_max": float(p["present_ms_max"]),
            "present_ms_p95": self._percentile(present_samples, 95),
            "lag_ms_max": float(p["lag_ms_max"]),
            "lag_ms_p95": self._percentile(lag_samples, 95),
            "wall_s": wall_s,
            "playback_active": bool(p.get("playback_active")),
            "pace_mode": str(p.get("pace_mode") or "opt"),
            "play_end_reason": p.get("play_end_reason"),
            "rate_play_frames": int(p.get("rate_play_frames", 0) or 0),
            "skip_trim_absorbed": int(p.get("skip_trim_absorbed", 0) or 0),
            "cap_threads": int(getattr(self, "_cap_threads", 0) or 0),
            "rate_trim_frames": int(p.get("rate_trim_frames", 0) or 0),
            "spikes": spikes[-12:],
        }

    def _note_play_start_stats(self) -> None:
        with self._stats_lock:
            self._reset_perf_stats_unlocked()
            now = self._now()
            self._perf["play_wall_t0"] = now
            self._perf["play_wall_end"] = None
            self._perf["playback_active"] = True
            self._perf["play_end_reason"] = "playing"
            self._perf["pace_mode"] = self._pace_mode

    def _freeze_perf_wall(self, reason: str = "stop") -> None:
        with self._stats_lock:
            if self._perf.get("play_wall_t0") is not None and self._perf.get("play_wall_end") is None:
                self._perf["play_wall_end"] = self._now()
            self._perf["playback_active"] = False
            self._perf["play_end_reason"] = reason

    def _halt_playback(self, flush_frames: bool = False, reason: str = "stop") -> None:
        self._playing = False
        self._play_t0 = None
        self._play_slots = 0
        self._freeze_perf_wall(reason=reason)
        if flush_frames:
            self._flush_frame_q()

    def _note_present(self, present_s: float, lag_s: float, frame_dur: float,
                      frame_idx: int, meta: dict) -> None:
        ms = present_s * 1000.0
        lag_ms = max(0.0, lag_s) * 1000.0
        with self._stats_lock:
            self._perf["presented"] += 1
            self._perf["present_ms_sum"] += ms
            if ms > self._perf["present_ms_max"]:
                self._perf["present_ms_max"] = ms
            if lag_ms > self._perf["lag_ms_max"]:
                self._perf["lag_ms_max"] = lag_ms
            self._perf["present_ms_samples"].append(ms)
            self._perf["lag_ms_samples"].append(lag_ms)
            if lag_s > frame_dur * self._LATE_LAG_RATIO:
                self._perf["late"] += 1
            if lag_s > frame_dur:
                self._perf["late1"] += 1
            if lag_s > 2 * frame_dur:
                self._perf["late2"] += 1
            # 尖峰归因
            if ms >= self._SPIKE_PRESENT_MS or lag_s > frame_dur:
                reasons = []
                if meta.get("used_seek"):
                    reasons.append("seek")
                if meta.get("skip_trim_n", 0) > 0:
                    reasons.append(f"skip_trim:{meta['skip_trim_n']}")
                if meta.get("step_skip_n", 0) > 0:
                    reasons.append(f"step:{meta['step_skip_n']}")
                if meta.get("step_raw") is not None and meta.get("step_capped") is not None:
                    try:
                        if int(meta.get("step_raw") or 0) > int(meta.get("step_capped") or 0):
                            reasons.append(
                                f"cap:{meta['step_capped']}/{meta['step_raw']}"
                            )
                    except (TypeError, ValueError):
                        pass
                if meta.get("path"):
                    reasons.append(str(meta["path"]))
                if not reasons:
                    reasons.append("decode_or_ui")
                self._perf["spikes"].append({
                    "frame": int(frame_idx),
                    "present_ms": round(ms, 1),
                    "lag_ms": round(lag_ms, 1),
                    "reasons": reasons,
                })

    def _note_discarded(self, n: int = 1) -> None:
        if n <= 0:
            return
        with self._stats_lock:
            self._perf["discarded"] += int(n)

    def _note_catchup_event(self) -> None:
        with self._stats_lock:
            self._perf["catchup_events"] += 1

    def _note_soft_reanchor(self) -> None:
        with self._stats_lock:
            self._perf["pace_resets"] += 1

    def _note_hard_reset(self) -> None:
        with self._stats_lock:
            self._perf["hard_resets"] += 1
            self._perf["pace_resets"] += 1

    def _note_seek(self) -> None:
        with self._stats_lock:
            self._perf["seek_count"] += 1

    def _skip_trim_min_span(self) -> int:
        """T1-1：预览吸收小裁剪段的阈值（帧）。0 表示关闭。"""
        pp = self._play_params or {}
        raw = pp.get('skip_trim_min_span')
        if raw is None:
            raw = getattr(self, '_SKIP_TRIM_MIN_SPAN', 0)
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 0

    def _note_skip_trim_absorbed(self) -> None:
        with self._stats_lock:
            self._perf["skip_trim_absorbed"] = (
                int(self._perf.get("skip_trim_absorbed", 0)) + 1
            )

    def _note_rate_play_frames(self, n: int) -> None:
        if n <= 0:
            return
        with self._stats_lock:
            self._perf["rate_play_frames"] = int(self._perf.get("rate_play_frames", 0)) + int(n)

    def _note_rate_trim_frames(self, n: int) -> None:
        if n <= 0:
            return
        with self._stats_lock:
            self._perf["rate_trim_frames"] = int(self._perf.get("rate_trim_frames", 0)) + int(n)

    def _note_q_drop(self) -> None:
        with self._stats_lock:
            self._perf["q_drop"] += 1

    def _clear_slot_meta(self, path: str = "present") -> None:
        self._slot_meta = {
            "skip_trim_n": 0,
            "step_skip_n": 0,
            "used_seek": False,
            "seek_n": 0,
            "path": path,
            "frame_idx": int(self._cur),
        }

    @classmethod
    def _clamp_canvas(cls, canvas_wh: tuple) -> tuple[int, int]:
        cw, ch = int(canvas_wh[0]), int(canvas_wh[1])
        cw = max(1, cw)
        ch = max(1, ch)
        m = max(cw, ch)
        if m > cls._MAX_PREVIEW_EDGE:
            s = cls._MAX_PREVIEW_EDGE / m
            cw = max(1, int(cw * s))
            ch = max(1, int(ch * s))
        return cw, ch

    def _push_frame(self, cur_idx: int, frame: np.ndarray, canvas_wh: tuple):
        cw, ch = self._clamp_canvas(canvas_wh)
        fh, fw = frame.shape[:2]
        scale  = min(cw / fw, ch / fh)
        nw     = max(1, int(fw * scale))
        nh     = max(1, int(fh * scale))
        interp = cv2.INTER_AREA if scale < 0.5 else cv2.INTER_LINEAR
        small  = cv2.resize(frame, (nw, nh), interpolation=interp)
        rgb    = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
        if self.frame_q.full():
            try:
                self.frame_q.get_nowait()
                self._note_q_drop()
            except Empty:
                pass
        self.frame_q.put((cur_idx, rgb))

    # ------------------------------------------------------------------
    # 播放：推进 / 显示一拍
    # ------------------------------------------------------------------

    def _raw_total_step(self) -> int:
        """Ideal source-frame step from UI preview_step x business speed_step."""
        pp = self._play_params
        s_step = self.speed_step(
            self._cur, pp['speed_segs'],
            pp['speedup_1x'], pp['speedup_02'], pp['speedup_02_factor'],
        )
        return max(1, int(pp.get('preview_step', 1) or 1) * int(s_step))

    def _preview_step_plan(self) -> tuple[int, float, int]:
        """S1: (capped_step, unused_scale, raw_step).

        capped_step = min(raw, CAP) so we never skip ~9 frames per tick.
        scale is kept for diagnostics only; do NOT multiply into frame_dur
        (that caused preview stutter storms).
        """
        raw = self._raw_total_step()
        cap = max(1, int(getattr(self, '_PREVIEW_STEP_CAP', 3) or 3))
        pp = self._play_params or {}
        if pp.get('preview_step_cap') is not None:
            try:
                cap = max(1, int(pp['preview_step_cap']))
            except (TypeError, ValueError):
                pass
        capped = min(raw, cap)
        scale = float(capped) / float(raw) if raw > 0 else 1.0
        return max(1, int(capped)), scale, max(1, int(raw))

    def _current_total_step(self) -> int:
        """Source frames to advance per display tick (S1-capped)."""
        capped, _scale, _raw = self._preview_step_plan()
        return capped

    def _skip_trim_if_needed(self, prefer_grab: bool = False) -> bool:
        pp = self._play_params
        if not pp.get('skip_trimmed'):
            return self._cur < self.total
        jumped = self.jump_pause(self._cur, pp['pause_segs'])
        if jumped != self._cur:
            n = jumped - self._cur
            self._slot_meta["skip_trim_n"] = self._slot_meta.get("skip_trim_n", 0) + n
            # T1-1：极短裁剪段直接播过。seek 固定成本 30-80ms，而播 n 帧只需
            # n×解码；小 n 时不 seek 反而更快且无尖峰。仅预览，导出不受影响。
            min_span = self._skip_trim_min_span()
            if 0 < n <= min_span:
                self._slot_meta["skip_trim_absorbed"] = (
                    self._slot_meta.get("skip_trim_absorbed", 0) + n
                )
                self._note_skip_trim_absorbed()
                return self._cur < self.total
            self._note_rate_trim_frames(n)
            # 大裁剪必须允许向前 seek；禁止向后（_advance_to 只接受 target>_cur）
            self._advance_to(jumped, allow_seek=True, prefer_grab=prefer_grab)
        return self._cur < self.total

    def _present_one_slot(self) -> tuple[bool, float, int, dict]:
        """
        解码并推送一帧显示拍。
        返回 (ok, present_seconds, frame_idx, meta)。
        """
        t0 = self._now()
        self._clear_slot_meta("present")
        pp = self._play_params
        if not self._skip_trim_if_needed(prefer_grab=False):
            self._halt_playback(flush_frames=False, reason="eof")
            return False, 0.0, int(self._cur), dict(self._slot_meta)

        capped_step, _dur_scale, raw_step = self._preview_step_plan()
        total_step = capped_step
        frame_idx = self._cur
        self._slot_meta["frame_idx"] = frame_idx
        self._slot_meta["step_raw"] = int(raw_step)
        self._slot_meta["step_capped"] = int(capped_step)
        ret, frame = self._read_one()
        if not ret:
            self._halt_playback(flush_frames=False, reason="eof")
            return False, 0.0, frame_idx, dict(self._slot_meta)

        skip = total_step - 1
        if skip > 0:
            self._slot_meta["step_skip_n"] = skip
            if pp.get('skip_trimmed'):
                landed = self.jump_pause(self._cur + skip, pp['pause_segs'])
                # 步进通常很小→grab；若落点因裁剪区变远→可向前 seek
                self._advance_to(landed, allow_seek=True, prefer_grab=True)
            else:
                self._advance_to(self._cur + skip, allow_seek=True, prefer_grab=True)

        self._push_frame(frame_idx, frame, pp['canvas_wh'])
        self._note_rate_play_frames(int(total_step))
        meta = dict(self._slot_meta)
        return True, self._now() - t0, frame_idx, meta

    def _discard_one_slot(self) -> bool:
        """落后追赶：只推进时间轴（可向前 seek 大裁剪）。"""
        self._clear_slot_meta("catchup")
        pp = self._play_params
        if not self._skip_trim_if_needed(prefer_grab=True):
            self._halt_playback(flush_frames=False, reason="eof")
            return False
        total_step = self._current_total_step()
        if pp.get('skip_trimmed'):
            landed = self.jump_pause(self._cur + total_step, pp['pause_segs'])
            self._advance_to(landed, allow_seek=True, prefer_grab=True)
        else:
            self._advance_to(self._cur + total_step, allow_seek=True, prefer_grab=True)
        if self._cur >= self.total:
            self._halt_playback(flush_frames=False, reason="eof")
            return False
        self._note_discarded(1)
        self._note_rate_play_frames(int(total_step))
        return True

    def _interruptible_sleep(self, seconds: float) -> bool:
        """精确睡到 seconds，期间可被命令打断。返回 True = 已处理命令。

        不要用 cmd_q.get(timeout=)：它走 Condition.wait，本机粒度 15.625ms
        且带拖尾，实测请求 2ms 实睡 p50=14.9/p95=29.0ms，请求 16.6ms 实睡
        p50=30.0ms（整整两拍）。正常一拍 present≈3-5ms、需睡 12-14ms，
        因此每拍被无谓超睡约 14ms ≈ 0.84 帧 ⇒ 凭空记 late1 并触发追帧。
        time.sleep 在 3.11 走高精度 waitable timer，实测请求 2ms 实睡
        p50=2.11/p95=2.52ms。改为 sleep 分片 + get_nowait 轮询：
        睡眠精度 <1ms，命令响应延迟上限 = _SLEEP_SLICE。
        """
        if seconds <= 0.0005:
            return False
        end = self._now() + seconds
        if self._legacy_timing:
            # 对照组：复现修复前行为（Condition.wait 粒度 + 拖尾）。
            while True:
                rem = end - self._now()
                if rem <= 0.0005:
                    return False
                try:
                    cmd = self.cmd_q.get(timeout=min(rem, 0.05))
                    self._handle_cmd(cmd)
                    return True
                except Empty:
                    continue
        while True:
            rem = end - self._now()
            if rem <= 0.0005:
                return False
            try:
                self._handle_cmd(self.cmd_q.get_nowait())
                return True
            except Empty:
                pass
            time.sleep(min(rem, self._SLEEP_SLICE))

    def _soft_reanchor(self, frame_dur: float) -> None:
        """
        软重锚：保持 slots 连续，把 t0 调到仅落后约 1 拍。
        比 hard reset（slots=0）更不伤节奏。
        """
        now = self._now()
        if self._play_slots <= 0:
            self._play_t0 = now
            self._play_slots = 0
            self._note_hard_reset()
            return
        # target = t0 + slots*fd  →  希望 lag ≈ 1*fd  ⇒  now - target = fd
        # t0 = now - slots*fd - fd
        self._play_t0 = now - self._play_slots * frame_dur - frame_dur
        self._note_soft_reanchor()

    def _pace_after_slot(self, frame_dur: float, present_s: float,
                         frame_idx: int, meta: dict) -> bool:
        """
        返回 True = 本轮已处理命令，主循环应 continue。
        opt：墙钟 + 追帧 + 软锚。
        base：#9 式 — 仅 sleep(frame_dur - present)，落后不追不锚（仍记 late 统计）。
        """
        now = self._now()
        if self._play_t0 is None:
            self._play_t0 = now
            self._play_slots = 0
        self._play_slots += 1
        target = self._play_t0 + self._play_slots * frame_dur
        lag = now - target
        self._note_present(present_s, max(0.0, lag), frame_dur, frame_idx, meta)

        if self._pace_mode == "base":
            # #9：只按本拍耗时 sleep，无追帧 / 软锚
            rem = frame_dur - present_s
            if rem > 0.0005:
                if self._interruptible_sleep(rem):
                    return True
            return False

        if lag < -0.0005:
            if self._interruptible_sleep(-lag):
                return True
            return False

        if lag > frame_dur:
            self._note_catchup_event()
            # 本拍已很慢 / 裁剪定位成本：直接软锚，避免连环 discard 叠尖峰。
            # seek+skip_trim 的 lag 是墙钟定位成本，不是内容债；再 discard 只会
            # 跳过想看的内容，并可能再触发一次 seek。
            # 大跨度 grab 跳裁剪（未走 set）但本拍已超过 1 帧时长时同样软锚。
            if (
                present_s > 3.0 * frame_dur
                or lag > 8.0 * frame_dur
                or (meta.get("used_seek") and int(meta.get("skip_trim_n", 0) or 0) > 0)
                or (int(meta.get("skip_trim_n", 0) or 0) > 0 and present_s > frame_dur)
            ):
                self._soft_reanchor(frame_dur)
                return False
            extra = min(int(lag / frame_dur), self._MAX_CATCHUP_SLOTS)
            for _ in range(extra):
                if not self._playing:
                    break
                try:
                    cmd = self.cmd_q.get_nowait()
                    self._handle_cmd(cmd)
                    return True
                except Empty:
                    pass
                if not self._discard_one_slot():
                    break
                self._play_slots += 1
                # A1：追帧中途若自己又 seek / 大跳，立刻停手软锚，勿连环。
                dmeta = self._slot_meta or {}
                if dmeta.get("used_seek") or int(dmeta.get("skip_trim_n", 0) or 0) > 0:
                    self._soft_reanchor(frame_dur)
                    return False
                # A1：每丢一拍重测 lag，已回到 ≤1 帧则提前结束。
                now_mid = self._now()
                target_mid = self._play_t0 + self._play_slots * frame_dur
                if now_mid - target_mid <= frame_dur:
                    break
            now2 = self._now()
            target2 = self._play_t0 + self._play_slots * frame_dur
            if now2 - target2 > 2 * frame_dur:
                if self._play_slots > 100000:
                    self._play_t0 = now2
                    self._play_slots = 0
                    self._note_hard_reset()
                else:
                    self._soft_reanchor(frame_dur)
        return False

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def _release_cap_once(self) -> bool:
        """Release ``VideoCapture`` at most once across shutdown paths."""
        with self._cap_release_lock:
            if self._cap_released:
                return False
            self._cap_released = True
        self._cap.release()
        return True

    def run(self):
        try:
            self._run_loop()
        except _VideoIOQuit:
            # Normal shutdown requested by CMD_QUIT; do not leak a thread
            # exception to Tk/pytest's global exception hook.
            pass
        finally:
            # All exit paths, including CMD_QUIT, release the cap
            # exactly once from the IO thread.
            self._release_cap_once()

    def _run_loop(self):
        base_frame_dur = 1.0 / max(float(self.fps), 1e-3)

        while True:
            if not self._playing:
                with self._stats_lock:
                    if self._perf.get("play_wall_t0") is not None and self._perf.get("play_wall_end") is None:
                        self._perf["play_wall_end"] = self._now()
                    if self._perf.get("playback_active"):
                        self._perf["playback_active"] = False
                        if not self._perf.get("play_end_reason"):
                            self._perf["play_end_reason"] = "stop"
                self._play_t0 = None
                self._play_slots = 0
                cmd = self.cmd_q.get()
                self._handle_cmd(cmd)
                continue

            try:
                cmd = self.cmd_q.get_nowait()
                self._handle_cmd(cmd)
                continue
            except Empty:
                pass

            if not self._playing:
                continue

            pp = self._play_params
            speed_mult = float(pp.get('speed_multiplier', 1.0) or 1.0)
            # 注意：不要用 S2「capped/raw」去缩 frame_dur。
            # raw=10,cap=3 时 scale=0.3 → 间隔约 10ms，而单拍解码常 30ms+，
            # 会几乎每拍 late，引发追帧雪崩（严重卡顿）。加速感仅靠 S1 步幅封顶。
            frame_dur = max(0.001, base_frame_dur * speed_mult)

            ok, present_s, frame_idx, meta = self._present_one_slot()
            if not ok:
                continue

            if self._pace_after_slot(frame_dur, present_s, frame_idx, meta):
                continue

    # ------------------------------------------------------------------
    # 命令处理
    # ------------------------------------------------------------------

    def _handle_cmd(self, cmd: dict):
        t = cmd['type']

        if t == CMD_QUIT:
            self._halt_playback(flush_frames=False, reason="stop")
            raise _VideoIOQuit

        elif t == CMD_STOP:
            self._halt_playback(flush_frames=True, reason="stop")

        elif t in (CMD_SEEK, CMD_SEEK_LATEST):
            self._halt_playback(flush_frames=True, reason="seek")
            frame_idx  = cmd['frame']
            canvas_wh  = cmd['canvas_wh']
            pause_segs = cmd.get('pause_segs', [])
            skip_trim  = cmd.get('skip_trimmed', True)

            if skip_trim and t == CMD_SEEK:
                frame_idx = self.jump_pause(frame_idx, pause_segs)

            self._seek_cap(frame_idx, note=True)
            ret, frame = self._read_one()
            if ret:
                self._push_frame(frame_idx, frame, canvas_wh)

        elif t == CMD_SET_PACE_MODE:
            m = cmd.get("mode", "opt")
            self._pace_mode = "base" if str(m).lower() == "base" else "opt"
            with self._stats_lock:
                self._perf["pace_mode"] = self._pace_mode

        elif t == CMD_PLAY:
            self._flush_frame_q()
            new_params = cmd['params']
            start = int(new_params.get('start_frame', 0))
            was_playing = self._playing
            self._play_params = new_params

            # opt：播放中重发 PLAY 不向后 seek（防 UI 落后导致回跳）
            # base：#9 行为 — 每次 PLAY 都 seek 到 start
            if self._pace_mode == "base" or not was_playing:
                self._seek_cap(start, note=True)
            elif start > self._cur:
                self._grab_n(start - self._cur, allow_seek=False)

            self._playing = True
            self._play_t0 = None
            self._play_slots = 0
            self.begin_perf_segment()

    # ------------------------------------------------------------------
    # 外部接口（线程安全）
    # ------------------------------------------------------------------

    def set_pace_mode(self, mode: str) -> None:
        """线程安全：切换 opt/base。播放中下一拍起生效。"""
        m = "base" if str(mode).lower() in ("base", "baseline", "off", "0", "false") else "opt"
        self.send({"type": CMD_SET_PACE_MODE, "mode": m})

    def get_pace_mode(self) -> str:
        with self._stats_lock:
            return str(self._perf.get("pace_mode") or self._pace_mode)

    def send(self, cmd: dict):
        # Once shutdown starts, no new playback work may be queued behind the
        # quit sentinel.  This also prevents a late Tk callback from reviving
        # an IO thread while the window is closing.
        with self._quit_lock:
            if self._quit_requested.is_set():
                return False
            if cmd['type'] == CMD_SEEK_LATEST:
                kept = []
                while True:
                    try:
                        old = self.cmd_q.get_nowait()
                        if old['type'] != CMD_SEEK_LATEST:
                            kept.append(old)
                    except Empty:
                        break
                for c in kept:
                    self.cmd_q.put(c)
            self.cmd_q.put(cmd)
            return True

    def stop_and_quit(self):
        """Request shutdown without waiting (legacy-compatible operation)."""
        with self._quit_lock:
            if self._quit_requested.is_set():
                return
            self._quit_requested.set()
            # Discard stale seeks/plays so QUIT is handled promptly even when
            # the UI generated a burst of commands immediately beforehand.
            while True:
                try:
                    self.cmd_q.get_nowait()
                except Empty:
                    break
            try:
                self.cmd_q.put({'type': CMD_QUIT})
            except Exception:
                pass

    def close(self, timeout: float = 1.0) -> bool:
        """Request shutdown and join this thread up to ``timeout`` seconds.

        Returns ``True`` only when the thread has exited.  A bounded join is
        important for Tk shutdown: callers can report a non-cooperative
        decoder instead of pretending that the capture was released.
        """
        self.stop_and_quit()
        if threading.current_thread() is not self and self.is_alive():
            self.join(max(0.0, float(timeout)))
        # A thread that was never started has no owner to release the capture;
        # releasing it here is safe.  For a running thread, leave release to
        # its command handler so cv2 is never touched concurrently.
        if not self.is_alive() and self.ident is None:
            try:
                self._release_cap_once()
            except Exception:
                pass
        return not self.is_alive()
