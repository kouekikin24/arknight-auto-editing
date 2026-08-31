# analyzer.py —— 模板加载 + 帧状态识别（无 GUI 依赖）

import ctypes
import cv2
import numpy as np
import os
import shutil
import subprocess
import concurrent.futures
import multiprocessing
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Sequence
import media_info
from frame_types import (FRAME_TYPE_NORMAL, FRAME_TYPE_PAUSE,
                         FRAME_TYPE_1X, FRAME_TYPE_2X, FRAME_TYPE_0_2X)
from task_manager import TaskCancelled
from timeline_plan import TimelinePlan

_NO_WINDOW = 0x08000000 if sys.platform == 'win32' else 0
_GPU_PROBE_CACHE: dict[str, bool] = {}
_GPU_PROBE_LOCK = threading.Lock()

# 快速滤镜路径：段过多时 filter_complex 会极慢且进度长期停在 ~1%
# 超过阈值则跳过，改走有帧进度的稳妥逐帧路径（不改变删帧语义）
_MAX_FILTER_EXPORT_RANGES = 120
_MAX_FILTER_EXPORT_RANGES_AUDIO = 80
# The CFR/legacy branch of the certified PTS exporter keeps one trim chain
# per segment; graph cost grows superlinearly with segment count (measured
# ~quadratic in RESEARCH_PHASE3_20260831 §B), so this ceiling stays a hard
# error there.  The VFR branch fails differently: one flat select/setpts
# expression pair per pass overflows FFmpeg's expression evaluator stack
# somewhere between 4000 and 13619 terms (observed 2026-09-01: exit code
# 0xC00000FD on a 13619-range schedule), so larger VFR schedules switch to
# batched-seek export (_export_pts_video_batched) instead of raising.
_MAX_PTS_EXPORT_RANGES = 4000
# Segments per video batch on the batched VFR route; each batch builds its
# own bounded select/setpts expressions.  A 4000-term single pass is
# probe-verified and 2623 is e2e-validated; 2000 keeps 2x margin under both.
_PTS_VIDEO_BATCH_RANGES = 2000

# The video side of the VFR branch (one flat select + gap-sum setpts) stays
# cheap into the thousands of segments; the AUDIO side does not: a single
# atrim/concat graph grows superlinearly with segment count (measured: 400
# segments ≈ 120 s, 800 segments > 600 s).  Beyond this threshold the audio
# therefore leaves the shared graph and is built by batched passes instead
# (see _export_audio_pts_batched).
_MAX_PTS_SINGLE_GRAPH_AUDIO_RANGES = 400
_PTS_AUDIO_BATCH_SIZE = 100


@dataclass
class _FFmpegPipe:
    process: subprocess.Popen
    stderr_file: Any

    @property
    def stdin(self):
        if self.process.stdin is None:
            raise RuntimeError("FFmpeg stdin 不可用")
        return self.process.stdin

# ---------------------------------------------------------------
#  模板加载（带预缩放缓存）
# ---------------------------------------------------------------

TEMPLATE_DIRS = {
    'pause': {'ref_dir': 'templates_pause', 'source_dir': 'source_images_pause'},
    'speed_1x': {'ref_dir': 'templates_1x', 'source_dir': 'source_images_1x'},
    'speed_2x': {'ref_dir': 'templates_2x', 'source_dir': 'source_images_2x'},
    'speed_0_2x': {'ref_dir': 'templates_play', 'source_dir': 'source_images_play'},
}

IMG_EXTS = ('.png', '.jpg', '.bmp', '.jpeg')


def load_templates(proc_res: tuple = (400, 225)) -> tuple[dict, int]:
    configs: dict[str, list] = {k: [] for k in TEMPLATE_DIRS}
    total = 0

    for ctype, dirs in TEMPLATE_DIRS.items():
        src_dir, ref_dir = dirs['source_dir'], dirs['ref_dir']
        if not os.path.exists(src_dir) or not os.path.exists(ref_dir): continue

        src_files = [f for f in os.listdir(src_dir) if f.lower().endswith(IMG_EXTS)]
        ref_files = [f for f in os.listdir(ref_dir) if f.lower().endswith(IMG_EXTS)]
        if not src_files or not ref_files: continue

        src_img = cv2.imread(os.path.join(src_dir, src_files[0]), cv2.IMREAD_GRAYSCALE)
        if src_img is None: continue
        sh, sw = src_img.shape

        for rf in ref_files:
            ref_img = cv2.imread(os.path.join(ref_dir, rf), cv2.IMREAD_GRAYSCALE)
            if ref_img is None: continue
            rh, rw = ref_img.shape

            res = cv2.matchTemplate(src_img, ref_img, cv2.TM_CCOEFF_NORMED)
            _, _, _, max_loc = cv2.minMaxLoc(res)
            rx, ry = max_loc
            _, mask = cv2.threshold(ref_img, 10, 255, cv2.THRESH_BINARY)

            scale_x, scale_y = proc_res[0] / sw, proc_res[1] / sh
            ext = 2.0
            erx = max(0, int(rx * scale_x - rw * scale_x * (ext - 1) / 2))
            ery = max(0, int(ry * scale_y - rh * scale_y * (ext - 1) / 2))
            tw, th = max(1, int(rw * scale_x)), max(1, int(rh * scale_y))

            configs[ctype].append({
                'roi_orig': (rx, ry, rw, rh),
                'source_res': (sw, sh),
                'cached_proc_res': proc_res,
                'cached_roi': (erx, ery, int(rw * scale_x * ext), int(rh * scale_y * ext)),
                'cached_t': cv2.resize(ref_img, (tw, th), interpolation=cv2.INTER_AREA),
                'cached_m': cv2.resize(mask, (tw, th), interpolation=cv2.INTER_NEAREST),
            })
            total += 1
    return configs, total


# ---------------------------------------------------------------
#  单帧匹配
# ---------------------------------------------------------------

def _get_best_score(gray_frame: np.ndarray, templates: list, proc_res: tuple) -> float:
    max_score = -1.0
    fh, fw = gray_frame.shape
    for t in templates:
        erx, ery, erw, erh = t['cached_roi']
        t_r, m_r = t['cached_t'], t['cached_m']
        erw, erh = min(fw - erx, erw), min(fh - ery, erh)

        if erw <= 0 or erh <= 0: continue
        roi = gray_frame[ery:ery + erh, erx:erx + erw]
        if roi.shape[0] < t_r.shape[0] or roi.shape[1] < t_r.shape[1]: continue

        res = cv2.matchTemplate(roi, t_r, cv2.TM_CCOEFF_NORMED, mask=m_r)
        _, score, _, _ = cv2.minMaxLoc(res)
        if np.isfinite(score): max_score = max(max_score, score)
    return max_score


def _classify_gray(gray: np.ndarray, configs: dict,
                   thresholds: dict, proc_res: tuple) -> int:
    if configs['pause'] and _get_best_score(gray, configs['pause'], proc_res) >= thresholds['pause']:
        return FRAME_TYPE_PAUSE
    x1s = _get_best_score(gray, configs['speed_1x'], proc_res) if configs['speed_1x'] else -1.0
    x2s = _get_best_score(gray, configs['speed_2x'], proc_res) if configs['speed_2x'] else -1.0
    if x1s >= thresholds['speed_1x'] and x1s > x2s: return FRAME_TYPE_1X
    if x2s >= thresholds['speed_2x'] and x2s > x1s: return FRAME_TYPE_2X
    if configs['speed_0_2x'] and _get_best_score(gray, configs['speed_0_2x'], proc_res) >= thresholds['speed_0_2x']:
        return FRAME_TYPE_0_2X
    return FRAME_TYPE_NORMAL


# ---------------------------------------------------------------
#  子进程全局状态
# ---------------------------------------------------------------

_worker_configs: dict = {}
_worker_thresholds: dict = {}
_worker_proc_res: tuple = (400, 225)


def _worker_init(configs: dict, thresholds: dict, proc_res: tuple):
    global _worker_configs, _worker_thresholds, _worker_proc_res
    _worker_configs = configs
    _worker_thresholds = thresholds
    _worker_proc_res = proc_res


def _worker_classify_gray(gray: np.ndarray) -> int:
    return _classify_gray(gray, _worker_configs, _worker_thresholds, _worker_proc_res)


def _worker_classify_gray_scored(gray: np.ndarray) -> tuple[int, float]:
    """Diagnostic variant: also return the raw pause-template score.

    Only used when the caller enables diagnostics; the production path keeps
    using _worker_classify_gray so the judgment logic is byte-for-byte
    unchanged.  Returns (state, pause_score).  pause_score is -1.0 when no
    pause templates are configured.
    """
    state = _classify_gray(gray, _worker_configs, _worker_thresholds, _worker_proc_res)
    pause_templates = _worker_configs.get('pause') or []
    score = (
        _get_best_score(gray, pause_templates, _worker_proc_res)
        if pause_templates
        else -1.0
    )
    return state, float(score)


# ---------------------------------------------------------------
#  First-pass pause-boundary context (skip second VideoCapture scan)
# ---------------------------------------------------------------

ANALYSIS_CONTEXT_VERSION = 1


class _BoundaryTracker:
    """Collect one scalar boundary record per pause run during ordered commit.

    Keeps at most two small gray frames: prev_gray (also used for diffs) and
    the open pause run's before-gray. Never grows with video duration.

    Logical length is always the *decoded* frame count L, not container
    metadata M. finish_with_last(decoded=L) must be used for EOF.
    """

    def __init__(self):
        self.records: list[dict] = []
        self._open_start: int | None = None
        self._before_gray: np.ndarray | None = None
        self._skipped_records = 0

    def observe(
        self,
        idx: int,
        gray: np.ndarray,
        state: int,
        prev_gray: np.ndarray | None,
    ) -> None:
        is_pause = int(state) == FRAME_TYPE_PAUSE
        if self._open_start is None:
            if is_pause:
                self._open_start = idx
                # before = max(0, start-1): previous gray, or this frame if start==0
                src = gray if idx == 0 else prev_gray
                self._before_gray = None if src is None else np.ascontiguousarray(src).copy()
        elif not is_pause:
            self._close_run(end=idx - 1, after_idx=idx, after_gray=gray)

    def finish_with_last(self, decoded: int, last_gray: np.ndarray | None) -> None:
        """Close a run still open at end-of-stream using logical length L=decoded."""
        if self._open_start is None:
            return
        if decoded <= 0:
            self._open_start = None
            self._before_gray = None
            self._skipped_records += 1
            return
        end = int(decoded) - 1
        # Logical total L = decoded. Same formula as build_segments:
        # after_idx = min(L - 1, end + 1). When pause reaches last frame,
        # after_idx == end and we close with last_gray.
        after_idx = min(int(decoded) - 1, end + 1)
        if after_idx <= end:
            if last_gray is not None:
                self._close_run(end=end, after_idx=after_idx, after_gray=last_gray)
                return
        # Missing after frame → context incomplete
        self._open_start = None
        self._before_gray = None
        self._skipped_records += 1

    def _close_run(self, end: int, after_idx: int, after_gray: np.ndarray) -> None:
        start = int(self._open_start)
        before_idx = max(0, start - 1)
        if self._before_gray is None:
            self._open_start = None
            self._before_gray = None
            self._skipped_records += 1
            return
        diff = float(cv2.mean(cv2.absdiff(self._before_gray, after_gray))[0])
        self.records.append(
            {
                "start": start,
                "end": int(end),
                "before_index": int(before_idx),
                "after_index": int(after_idx),
                "diff": diff,
            }
        )
        self._open_start = None
        self._before_gray = None

    @property
    def skipped(self) -> int:
        return self._skipped_records


def _make_analysis_context(
    logical_len: int, records: list[dict], complete: bool
) -> dict:
    """logical_len L = len(states) after trim (= decoded). Not container metadata."""
    L = int(logical_len)
    return {
        "version": ANALYSIS_CONTEXT_VERSION,
        "complete": bool(complete),
        "frame_count": L,
        "decoded_frame_count": L,
        "pause_boundary_diffs": list(records),
    }


def context_records_for_pauses(analysis_context, pauses: list, total: int):
    """Return records if the whole context is usable; else None.

    All-or-nothing: never mix cached and rescanned boundaries.
    `total` must be len(states) (logical length L).
    """
    if not isinstance(analysis_context, dict):
        return None
    try:
        if analysis_context.get("version") != ANALYSIS_CONTEXT_VERSION:
            return None
        if analysis_context.get("complete") is not True:
            return None
        L = int(total)
        if int(analysis_context.get("frame_count", -1)) != L:
            return None
        if int(analysis_context.get("decoded_frame_count", -1)) != L:
            return None
        records = analysis_context.get("pause_boundary_diffs")
        if not isinstance(records, list) or len(records) != len(pauses):
            return None
        for rec, p in zip(records, pauses):
            if not isinstance(rec, dict):
                return None
            start, end = int(rec["start"]), int(rec["end"])
            before, after = int(rec["before_index"]), int(rec["after_index"])
            diff = float(rec["diff"])
            if start != int(p["start"]) or end != int(p["end"]):
                return None
            if before != max(0, start - 1) or after != min(L - 1, end + 1):
                return None
            if not np.isfinite(diff):
                return None
    except (KeyError, TypeError, ValueError):
        return None
    return records


def analysis_context_skips_second_scan(analysis_context, pauses: list, total: int) -> bool:
    """True when build_segments would skip the second VideoCapture scan."""
    if not pauses:
        return True
    return context_records_for_pauses(analysis_context, pauses, total) is not None


def _finalize_analysis_arrays(
    states: np.ndarray,
    diffs: np.ndarray,
    allocated_total: int,
    decoded: int,
    tracker: _BoundaryTracker | None,
    last_gray: np.ndarray | None,
) -> tuple[np.ndarray, np.ndarray, dict]:
    """Trim to decoded length L; complete does NOT require L == metadata M."""
    if decoded < len(states):
        states = states[:decoded]
        diffs = diffs[:decoded]
    L = int(decoded)
    if allocated_total > 0 and L < int(allocated_total):
        print(
            f"[analyze] logical length L={L} < container metadata M={int(allocated_total)} "
            f"(using L for context completeness)",
            flush=True,
        )
    if tracker is not None:
        # Finish against logical length L, not container metadata.
        tracker.finish_with_last(L, last_gray)
        complete = tracker.skipped == 0 and L > 0
        context = _make_analysis_context(L, tracker.records, complete)
    else:
        context = _make_analysis_context(L, [], False)
    return states, diffs, context


# ---------------------------------------------------------------
#  分析解码后端（生产可选 A_PT）
# ---------------------------------------------------------------

# Default remains OpenCV for compatibility. Optional:
#   ffmpeg_sw_passthrough == verified A_PT path
#   (software decode + scale=area + gray + -fps_mode passthrough)
DECODE_BACKEND_OPENCV = "opencv"
DECODE_BACKEND_FFMPEG_SW_PASSTHROUGH = "ffmpeg_sw_passthrough"
_DECODE_BACKEND_ALIASES = {
    "opencv": DECODE_BACKEND_OPENCV,
    "cv2": DECODE_BACKEND_OPENCV,
    "default": DECODE_BACKEND_OPENCV,
    "ffmpeg_sw_passthrough": DECODE_BACKEND_FFMPEG_SW_PASSTHROUGH,
    "ffmpeg_sw_gray_passthrough": DECODE_BACKEND_FFMPEG_SW_PASSTHROUGH,
    "a_pt": DECODE_BACKEND_FFMPEG_SW_PASSTHROUGH,
}


def normalize_decode_backend(decode_backend: str | None) -> str:
    key = (decode_backend or DECODE_BACKEND_OPENCV).strip().lower()
    if key not in _DECODE_BACKEND_ALIASES:
        raise ValueError(
            f"unknown decode_backend={decode_backend!r}; "
            f"allowed={sorted(set(_DECODE_BACKEND_ALIASES.values()))}"
        )
    return _DECODE_BACKEND_ALIASES[key]


def _a_pt_impl() -> str:
    """Select the A_PT implementation: "pyav" (default) or "ffmpeg" CLI.

    PyAV is the consolidated in-process path and is bit-identical to the
    ffmpeg.exe 7.1 CLI when it bundles the same FFmpeg 7.x generation (av 13.x).
    Set ARKNIGHT_A_PT_IMPL=ffmpeg to force the legacy CLI subprocess path. If
    PyAV is requested but unavailable, fall back to the CLI with a notice.
    """
    impl = os.environ.get("ARKNIGHT_A_PT_IMPL", "pyav").strip().lower()
    if impl not in ("pyav", "ffmpeg"):
        raise ValueError(
            f"unknown ARKNIGHT_A_PT_IMPL={impl!r}; allowed=['pyav', 'ffmpeg']"
        )
    if impl == "pyav":
        try:
            import av  # noqa: F401
        except Exception:
            print(
                "[analyze] PyAV unavailable; A_PT falling back to FFmpeg CLI",
                flush=True,
            )
            return "ffmpeg"
    return impl


def resolve_ffmpeg_path(ffmpeg_path: str | None = None) -> str:
    try:
        return str(media_info.resolve_ffmpeg_path(ffmpeg_path))
    except media_info.MediaInfoError as exc:
        raise FileNotFoundError(str(exc)) from exc


def _read_exact(stream, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def _ffmpeg_sw_passthrough_cmd(
    ffmpeg: str, video_path: str, frames: int, proc_res: tuple[int, int]
) -> list[str]:
    """Verified A_PT command graph (do not add extra timestamp/sync flags)."""
    pw, ph = int(proc_res[0]), int(proc_res[1])
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        video_path,
        "-an",
        "-frames:v",
        str(int(frames)),
        "-vf",
        f"scale={pw}:{ph}:flags=area,format=gray",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "-fps_mode",
        "passthrough",
        "pipe:1",
    ]


def _analyze_video_opencv(
    video_path: str,
    configs: dict,
    thresholds: dict,
    proc_res: tuple,
    batch_size: int,
    n_threads: int,
    progress_cb=None,
    want_context: bool = False,
    want_diagnostics: bool = False,
) -> tuple[np.ndarray, np.ndarray, dict | None]:
    """FROZEN decode backend — kept as a fallback, no longer maintained.

    The production decode path is A_PT, now implemented in-process by PyAV
    (_analyze_video_pyav_filter), with the FFmpeg CLI retained behind
    ARKNIGHT_A_PT_IMPL=ffmpeg. This OpenCV decode backend exists only as a
    last-resort fallback and must not receive new feature work. Scope note:
    this freeze covers the *decode backend* only — cv2.matchTemplate (the
    pause/speed recognition algorithm itself), imaging and preview are
    unchanged and remain in active use.
    """
    cap = cv2.VideoCapture(video_path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    states = np.zeros(max(0, total), dtype=np.int8)
    diffs = np.zeros(max(0, total), dtype=np.float32)
    n_workers = min(n_threads, multiprocessing.cpu_count())
    pw, ph = proc_res
    tracker = _BoundaryTracker() if want_context else None
    # Diagnostic collectors (only when explicitly requested; judgment untouched)
    diag_scores: list[float] | None = [] if want_diagnostics else None
    diag_luma: list[float] | None = [] if want_diagnostics else None
    # If total unknown/0, still allow reading until EOF with growable lists.
    use_dynamic = total <= 0
    if use_dynamic:
        states_list: list[int] = []
        diffs_list: list[float] = []
        tracker = _BoundaryTracker() if want_context else None

    with concurrent.futures.ProcessPoolExecutor(
            max_workers=n_workers,
            initializer=_worker_init,
            initargs=(configs, thresholds, proc_res)) as ex:

        idx = 0
        prev_gray = None
        last_gray = None
        allocated_total = total

        while True:
            batch_grays = []
            batch_indices = []
            batch_prev = []

            for _ in range(batch_size):
                ret, frame = cap.read()
                if not ret:
                    break

                gray = cv2.cvtColor(
                    cv2.resize(frame, (pw, ph), interpolation=cv2.INTER_AREA),
                    cv2.COLOR_BGR2GRAY,
                )
                batch_grays.append(gray)
                batch_indices.append(idx)
                batch_prev.append(prev_gray)
                if diag_luma is not None:
                    diag_luma.append(float(gray.mean()))

                if not use_dynamic:
                    if prev_gray is not None and idx < len(diffs):
                        diffs[idx] = float(cv2.mean(cv2.absdiff(gray, prev_gray))[0])
                else:
                    diffs_list.append(
                        0.0
                        if prev_gray is None
                        else float(cv2.mean(cv2.absdiff(gray, prev_gray))[0])
                    )
                prev_gray = gray
                last_gray = gray
                idx += 1

            if not batch_grays:
                break

            chunk = max(4, len(batch_grays) // (n_workers * 2))
            if diag_scores is not None:
                scored = list(ex.map(_worker_classify_gray_scored, batch_grays, chunksize=chunk))
                results = [s for s, _ in scored]
                diag_scores.extend(sc for _, sc in scored)
            else:
                results = list(ex.map(_worker_classify_gray, batch_grays, chunksize=chunk))

            for i, s, g, pg in zip(batch_indices, results, batch_grays, batch_prev):
                if use_dynamic:
                    states_list.append(int(s))
                else:
                    states[i] = s
                if tracker is not None:
                    tracker.observe(i, g, int(s), pg)

            if progress_cb:
                denom = max(1, allocated_total if allocated_total > 0 else idx)
                progress_cb((idx / denom) * 0.5)

    cap.release()
    if use_dynamic:
        states = np.asarray(states_list, dtype=np.int8)
        diffs = np.asarray(diffs_list, dtype=np.float32)
        allocated_total = idx
        decoded = idx
    else:
        decoded = idx
        # allocated_total from metadata; decoded may be smaller
    diag = None
    if diag_scores is not None or diag_luma is not None:
        diag = {
            "pause_score": np.asarray(diag_scores[:decoded], dtype=np.float32),
            "luma": np.asarray(diag_luma[:decoded], dtype=np.float32),
        }
    if want_context:
        states, diffs, context = _finalize_analysis_arrays(
            states, diffs, max(allocated_total, decoded), decoded, tracker, last_gray
        )
        if diag is not None:
            context = dict(context or {})
            context["_diagnostics"] = diag
        return states, diffs, context
    if decoded < len(states):
        states = states[:decoded]
        diffs = diffs[:decoded]
    if diag is not None:
        return states, diffs, {"_diagnostics": diag}
    return states, diffs, None


def _analyze_video_ffmpeg_sw_passthrough(
    video_path: str,
    configs: dict,
    thresholds: dict,
    proc_res: tuple,
    batch_size: int,
    n_threads: int,
    progress_cb=None,
    ffmpeg_path: str | None = None,
    want_context: bool = False,
    want_diagnostics: bool = False,
) -> tuple[np.ndarray, np.ndarray, dict | None]:
    """Production A_PT: FFmpeg software gray@proc_res with output fps_mode=passthrough."""
    ffmpeg = resolve_ffmpeg_path(ffmpeg_path)
    pw, ph = int(proc_res[0]), int(proc_res[1])
    if pw <= 0 or ph <= 0:
        raise ValueError(f"invalid proc_res={proc_res}")

    # Frame count oracle matches existing OpenCV path (same CAP_PROP_FRAME_COUNT).
    cap = cv2.VideoCapture(video_path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    cap.release()
    if total <= 0:
        raise RuntimeError(f"cannot determine frame count for {video_path}")

    bpf = pw * ph
    cmd = _ffmpeg_sw_passthrough_cmd(ffmpeg, video_path, total, (pw, ph))
    # Wall budget scales with length; 180s is only enough for short clips.
    timeout_s = max(180.0, 120.0 + float(total) * 0.12)

    states = np.zeros(total, dtype=np.int8)
    diffs = np.zeros(total, dtype=np.float32)
    n_workers = min(n_threads, multiprocessing.cpu_count())
    tracker = _BoundaryTracker() if want_context else None
    diag_scores: list[float] | None = [] if want_diagnostics else None
    diag_luma: list[float] | None = [] if want_diagnostics else None

    stderr_file = tempfile.TemporaryFile()
    proc: subprocess.Popen | None = None
    got = 0
    prev_gray = None
    last_gray = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            creationflags=_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if proc.stdout is None:
            raise RuntimeError("FFmpeg stdout pipe unavailable")

        deadline = time.perf_counter() + timeout_s

        def _stderr_tail() -> str:
            try:
                stderr_file.flush()
                stderr_file.seek(0, os.SEEK_END)
                size = stderr_file.tell()
                stderr_file.seek(max(0, size - 4096), os.SEEK_SET)
                return stderr_file.read().decode("utf-8", errors="replace")
            except Exception:
                return ""

        with concurrent.futures.ProcessPoolExecutor(
            max_workers=n_workers,
            initializer=_worker_init,
            initargs=(configs, thresholds, proc_res),
        ) as ex:
            while got < total:
                if time.perf_counter() > deadline:
                    raise TimeoutError(
                        f"FFmpeg A_PT timed out after {timeout_s:.1f}s at frame {got}/{total}"
                    )

                batch_n = min(int(batch_size), total - got)
                batch_grays = []
                batch_indices = []
                batch_prev = []
                eof = False
                for _ in range(batch_n):
                    raw = _read_exact(proc.stdout, bpf)
                    if len(raw) == 0:
                        # Clean EOF: CAP_PROP_FRAME_COUNT is often slightly high.
                        eof = True
                        break
                    if len(raw) != bpf:
                        raise RuntimeError(
                            f"FFmpeg A_PT partial frame at index {got}/{total}; "
                            f"got_bytes={len(raw)} expected={bpf}; "
                            f"stderr={_stderr_tail()[:500]}"
                        )
                    gray = np.frombuffer(raw, dtype=np.uint8).reshape((ph, pw)).copy()
                    batch_grays.append(gray)
                    batch_indices.append(got)
                    batch_prev.append(prev_gray)
                    if diag_luma is not None:
                        diag_luma.append(float(gray.mean()))
                    if prev_gray is not None:
                        diffs[got] = float(cv2.mean(cv2.absdiff(gray, prev_gray))[0])
                    prev_gray = gray
                    last_gray = gray
                    got += 1

                if batch_grays:
                    chunk = max(4, len(batch_grays) // (n_workers * 2))
                    if diag_scores is not None:
                        scored = list(
                            ex.map(_worker_classify_gray_scored, batch_grays, chunksize=chunk)
                        )
                        results = [s for s, _ in scored]
                        diag_scores.extend(sc for _, sc in scored)
                    else:
                        results = list(
                            ex.map(_worker_classify_gray, batch_grays, chunksize=chunk)
                        )
                    for i, s, g, pg in zip(
                        batch_indices, results, batch_grays, batch_prev
                    ):
                        states[i] = s
                        if tracker is not None:
                            tracker.observe(i, g, int(s), pg)
                    if progress_cb:
                        progress_cb((got / total) * 0.5)

                if eof:
                    break

        # Drain/wait FFmpeg
        try:
            proc.stdout.close()
        except Exception:
            pass
        try:
            rc = proc.wait(timeout=max(30.0, min(120.0, timeout_s * 0.1)))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise TimeoutError("FFmpeg A_PT did not exit after stdout closed")
        if got == 0:
            raise RuntimeError(
                f"FFmpeg A_PT produced 0 frames; exit={rc}; stderr={_stderr_tail()[:500]}"
            )
        if got < total:
            # Metadata overstated frame count (common). Accept actual stream length.
            print(
                f"[analyze] A_PT decoded {got}/{total} frames "
                f"(container metadata may overstate FRAME_COUNT); exit={rc}",
                flush=True,
            )
        elif rc not in (0, None) and got != total:
            raise RuntimeError(
                f"FFmpeg A_PT exit={rc} after {got}/{total} frames; stderr={_stderr_tail()[:500]}"
            )
    finally:
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)
            except Exception:
                pass
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass
        try:
            stderr_file.close()
        except Exception:
            pass

    if want_context:
        states, diffs, context = _finalize_analysis_arrays(
            states, diffs, total, got, tracker, last_gray
        )
        if diag_scores is not None or diag_luma is not None:
            context = dict(context or {})
            context["_diagnostics"] = {
                "pause_score": np.asarray((diag_scores or [])[:got], dtype=np.float32),
                "luma": np.asarray((diag_luma or [])[:got], dtype=np.float32),
            }
        return states, diffs, context
    if got != total:
        states = states[:got]
        diffs = diffs[:got]
    if diag_scores is not None or diag_luma is not None:
        return states, diffs, {"_diagnostics": {
            "pause_score": np.asarray((diag_scores or [])[:got], dtype=np.float32),
            "luma": np.asarray((diag_luma or [])[:got], dtype=np.float32),
        }}
    return states, diffs, None


def _analyze_video_pyav_filter(
    video_path: str,
    configs: dict,
    thresholds: dict,
    proc_res: tuple,
    batch_size: int,
    n_threads: int,
    progress_cb=None,
    ffmpeg_path: str | None = None,
    want_context: bool = False,
    want_diagnostics: bool = False,
) -> tuple[np.ndarray, np.ndarray, dict | None]:
    """Production A_PT implemented with PyAV: the CLI filter graph
    (scale={pw}:{ph}:flags=area → format=gray, one frame per decoded frame)
    run in-process via libavfilter.

    Bit-identical to the ffmpeg.exe 7.1 CLI output when PyAV bundles the same
    FFmpeg 7.x generation (av 13.x); verified frame-for-frame on the production
    samples. ARKNIGHT_ANALYZE_DECODE=ffmpeg restores the CLI implementation.
    """
    try:
        import av
        import av.filter
    except Exception as exc:
        raise RuntimeError(f"PyAV is required for the pyav A_PT backend: {exc}") from exc

    pw, ph = int(proc_res[0]), int(proc_res[1])
    if pw <= 0 or ph <= 0:
        raise ValueError(f"invalid proc_res={proc_res}")

    # Frame count oracle matches the CLI path (same CAP_PROP_FRAME_COUNT).
    cap = cv2.VideoCapture(video_path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    cap.release()
    if total <= 0:
        raise RuntimeError(f"cannot determine frame count for {video_path}")

    # Wall budget scales with length; identical to the CLI path.
    timeout_s = max(180.0, 120.0 + float(total) * 0.12)

    states = np.zeros(total, dtype=np.int8)
    diffs = np.zeros(total, dtype=np.float32)
    n_workers = min(n_threads, multiprocessing.cpu_count())
    tracker = _BoundaryTracker() if want_context else None
    diag_scores: list[float] | None = [] if want_diagnostics else None
    diag_luma: list[float] | None = [] if want_diagnostics else None

    container = None
    got = 0
    prev_gray = None
    last_gray = None
    deadline = time.perf_counter() + timeout_s
    try:
        container = av.open(str(video_path))
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        graph = av.filter.Graph()
        src = graph.add_buffer(template=stream)
        scale = graph.add("scale", f"{pw}:{ph}:flags=area")
        fmt = graph.add("format", "gray")
        sink = graph.add("buffersink")
        src.link_to(scale)
        scale.link_to(fmt)
        fmt.link_to(sink)
        graph.configure()

        from av.error import BlockingIOError as _Again, EOFError as _Done  # type: ignore

        batch_grays: list[np.ndarray] = []
        batch_indices: list[int] = []
        batch_prev: list[np.ndarray | None] = []
        stop = False

        def _consume(out) -> None:
            nonlocal got, prev_gray, last_gray, stop
            if got >= total:
                stop = True
                return
            p = out.planes[0]
            raw = (ctypes.c_ubyte * p.buffer_size).from_address(p.buffer_ptr)
            gray = np.frombuffer(
                raw, dtype=np.uint8
            ).reshape(out.height, p.line_size)[:, : out.width].copy()
            batch_grays.append(gray)
            batch_indices.append(got)
            batch_prev.append(prev_gray)
            if diag_luma is not None:
                diag_luma.append(float(gray.mean()))
            if prev_gray is not None:
                diffs[got] = float(cv2.mean(cv2.absdiff(gray, prev_gray))[0])
            prev_gray = gray
            last_gray = gray
            got += 1
            if got >= total:
                stop = True

        with concurrent.futures.ProcessPoolExecutor(
            max_workers=n_workers,
            initializer=_worker_init,
            initargs=(configs, thresholds, proc_res),
        ) as ex:

            def _flush_batch() -> None:
                if not batch_grays:
                    return
                if time.perf_counter() > deadline:
                    raise TimeoutError(
                        f"PyAV A_PT timed out after {timeout_s:.1f}s at frame {got}/{total}"
                    )
                chunk = max(4, len(batch_grays) // (n_workers * 2))
                if diag_scores is not None:
                    scored = list(
                        ex.map(_worker_classify_gray_scored, batch_grays, chunksize=chunk)
                    )
                    results = [s for s, _ in scored]
                    diag_scores.extend(sc for _, sc in scored)
                else:
                    results = list(
                        ex.map(_worker_classify_gray, batch_grays, chunksize=chunk)
                    )
                for i, s, g, pg in zip(
                    batch_indices, results, batch_grays, batch_prev
                ):
                    states[i] = s
                    if tracker is not None:
                        tracker.observe(i, g, int(s), pg)
                if progress_cb:
                    progress_cb((got / total) * 0.5)
                batch_grays.clear()
                batch_indices.clear()
                batch_prev.clear()

            def _drain(eof: bool) -> None:
                while not stop:
                    try:
                        _consume(graph.pull())
                    except _Again:
                        return
                    except _Done:
                        return

            for frame in container.decode(stream):
                if stop:
                    break
                graph.push(frame)
                _drain(eof=False)
                if stop:
                    break
                if len(batch_grays) >= int(batch_size):
                    _flush_batch()
            if not stop:
                graph.push(None)
                _drain(eof=True)
            _flush_batch()
    finally:
        if container is not None:
            try:
                container.close()
            except Exception:
                pass

    if got == 0:
        raise RuntimeError(f"PyAV A_PT produced 0 frames for {video_path}")
    if got < total:
        # Metadata overstated frame count (common). Accept actual stream length.
        print(
            f"[analyze] A_PT(PyAV) decoded {got}/{total} frames "
            f"(container metadata may overstate FRAME_COUNT)",
            flush=True,
        )

    if want_context:
        states, diffs, context = _finalize_analysis_arrays(
            states, diffs, total, got, tracker, last_gray
        )
        if diag_scores is not None or diag_luma is not None:
            context = dict(context or {})
            context["_diagnostics"] = {
                "pause_score": np.asarray((diag_scores or [])[:got], dtype=np.float32),
                "luma": np.asarray((diag_luma or [])[:got], dtype=np.float32),
            }
        return states, diffs, context
    if got != total:
        states = states[:got]
        diffs = diffs[:got]
    if diag_scores is not None or diag_luma is not None:
        return states, diffs, {"_diagnostics": {
            "pause_score": np.asarray((diag_scores or [])[:got], dtype=np.float32),
            "luma": np.asarray((diag_luma or [])[:got], dtype=np.float32),
        }}
    return states, diffs, None


# ---------------------------------------------------------------
#  批量分析整段视频
# ---------------------------------------------------------------

def analyze_video(video_path: str, configs: dict, thresholds: dict,
                  proc_res: tuple, batch_size: int, n_threads: int,
                  progress_cb=None,
                  decode_backend: str = DECODE_BACKEND_OPENCV,
                  ffmpeg_path: str | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Analyze full video into states/diffs (compatible two-item API).

    decode_backend:
      - "opencv" (default): production baseline BGR→INTER_AREA→GRAY
      - "ffmpeg_sw_passthrough" / "a_pt": verified A_PT software gray path

    For skipping the second boundary VideoCapture scan, prefer
    analyze_video_with_context(...) and pass the context to build_segments.
    """
    states, diffs, _ = analyze_video_with_context(
        video_path,
        configs,
        thresholds,
        proc_res,
        batch_size,
        n_threads,
        progress_cb,
        decode_backend=decode_backend,
        ffmpeg_path=ffmpeg_path,
    )
    return states, diffs


def analyze_video_with_context(
    video_path: str,
    configs: dict,
    thresholds: dict,
    proc_res: tuple,
    batch_size: int,
    n_threads: int,
    progress_cb=None,
    decode_backend: str = DECODE_BACKEND_OPENCV,
    ffmpeg_path: str | None = None,
    want_diagnostics: bool = False,
) -> tuple[np.ndarray, np.ndarray, dict]:
    """Three-item API: (states, diffs, analysis_context).

    analysis_context is JSON-safe (no frame pixels). When complete, pass it to
    build_segments(..., analysis_context=context) to skip the second boundary
    VideoCapture scan. Incomplete/mismatched context is rejected wholesale.

    want_diagnostics=True additionally records the raw pause-template score and
    per-frame luma into context["_diagnostics"] as numpy arrays.  This is a
    pure side-channel for offline boundary analysis; the classification logic
    and the JSON-safe context validation are unchanged.
    """
    backend = normalize_decode_backend(decode_backend)
    if backend == DECODE_BACKEND_OPENCV:
        states, diffs, context = _analyze_video_opencv(
            video_path,
            configs,
            thresholds,
            proc_res,
            batch_size,
            n_threads,
            progress_cb,
            want_context=True,
            want_diagnostics=want_diagnostics,
        )
        return states, diffs, context or _make_analysis_context(len(states), [], False)
    if backend == DECODE_BACKEND_FFMPEG_SW_PASSTHROUGH:
        impl = _a_pt_impl()
        if impl == "pyav":
            states, diffs, context = _analyze_video_pyav_filter(
                video_path,
                configs,
                thresholds,
                proc_res,
                batch_size,
                n_threads,
                progress_cb,
                ffmpeg_path=ffmpeg_path,
                want_context=True,
                want_diagnostics=want_diagnostics,
            )
        else:
            states, diffs, context = _analyze_video_ffmpeg_sw_passthrough(
                video_path,
                configs,
                thresholds,
                proc_res,
                batch_size,
                n_threads,
                progress_cb,
                ffmpeg_path=ffmpeg_path,
                want_context=True,
                want_diagnostics=want_diagnostics,
            )
        return states, diffs, context or _make_analysis_context(len(states), [], False)
    raise ValueError(f"unsupported decode_backend={backend!r}")


# ---------------------------------------------------------------
#  段落提取 + 内部操作细粒度帧差分析 + 外部边界差分
# ---------------------------------------------------------------

def _analyze_pause_mask(s_i: int, e_i: int, diffs: np.ndarray, still_frames: int, motion_thresh: float):
    seg_len = e_i - s_i + 1
    if seg_len <= 0:
        return np.zeros(0, dtype=np.uint8), 'all'

    active_mask = np.zeros(seg_len, dtype=bool)
    active_mask[0] = False

    for k in range(1, seg_len):
        idx = s_i + k
        if diffs[idx] > motion_thresh:
            active_mask[k] = True
            active_mask[k - 1] = True

    del_mask = np.zeros(seg_len, dtype=np.uint8)

    if seg_len > 0:
        runs = []
        curr_val = active_mask[0]
        start = 0
        for i in range(1, seg_len):
            if active_mask[i] != curr_val:
                runs.append((curr_val, start, i - 1))
                curr_val = active_mask[i]
                start = i
        runs.append((curr_val, start, seg_len - 1))

        has_active = any(val for val, s, e in runs)

        if not has_active:
            if seg_len > 2 * still_frames:
                del_mask[still_frames: seg_len - still_frames] = 1
            return del_mask, 'auto'

        for val, s, e in runs:
            if not val:
                run_len = e - s + 1
                if run_len > still_frames:
                    if s == 0:
                        keep_start = e - still_frames + 1
                        del_mask[s:keep_start] = 1
                    elif e == seg_len - 1:
                        keep_end = s + still_frames - 1
                        del_mask[keep_end + 1:e + 1] = 1
                    else:
                        half = still_frames // 2
                        other_half = still_frames - half
                        del_mask[s + half: e - other_half + 1] = 1

    return del_mask, 'auto'


def build_segments(states: np.ndarray, diffs: np.ndarray, video_path: str, proc_res: tuple,
                   compare_cfg: dict, fps: float, progress_cb=None, *,
                   analysis_context=None) -> tuple[list, list]:
    total = len(states)
    pauses = []
    speeds = []

    still_time = compare_cfg.get('still_time_thresh', 0.1)
    motion_thresh = compare_cfg.get('motion_thresh', 2.0)
    boundary_thresh = compare_cfg.get('boundary_thresh', 5.0)
    still_frames = max(2, int(fps * still_time))

    # 1. 基础分段
    i = 0
    while i < total:
        curr = int(states[i])
        s_i = i
        while i < total and int(states[i]) == curr:
            i += 1
        e_i = i - 1

        if curr == FRAME_TYPE_PAUSE:
            del_mask, mode = _analyze_pause_mask(s_i, e_i, diffs, still_frames, motion_thresh)
            pauses.append({
                'id': len(pauses),
                'start': s_i,
                'end': e_i,
                'mode': mode,
                'local_del_mask': del_mask,
                'boundary_diff': 0.0  # 预占位，稍后计算
            })
            if progress_cb: progress_cb(0.5 + (e_i / max(1, total)) * 0.25)

        elif curr in (FRAME_TYPE_1X, FRAME_TYPE_2X, FRAME_TYPE_0_2X):
            speeds.append({'type': curr, 'start': s_i, 'end': e_i})

    # 2. 暂停边界差分
    #    完整的第一遍 analysis_context 可直接提供每个暂停段的 boundary_diff，
    #    此时不再打开第二个 VideoCapture。上下文不可用时整体拒绝并回退到
    #    原始全量顺序 grab 扫描；绝不混用缓存与重扫结果。
    context_records = None
    if pauses and analysis_context is not None:
        context_records = context_records_for_pauses(analysis_context, pauses, total)

    if pauses and context_records is not None:
        for p, rec in zip(pauses, context_records):
            diff = float(rec['diff'])
            p['boundary_diff'] = diff
            # 严格小于：等于阈值不强制全删
            if diff < boundary_thresh:
                p['mode'] = 'all'
        if progress_cb:
            progress_cb(1.0)
        return pauses, speeds

    if pauses:
        cap = cv2.VideoCapture(video_path)
        # 获取所有目标帧索引，去重并排序
        target_indices = sorted(list(set([max(0, p['start'] - 1) for p in pauses] +
                                         [min(total - 1, p['end'] + 1) for p in pauses])))
        target_frames = {}
        curr_idx = 0
        for target in target_indices:
            # 顺序 grab 直到目标帧，这是最稳定精准读取特定帧的方法
            while curr_idx < target:
                cap.grab()
                curr_idx += 1
            ret, frame = cap.read()
            if ret:
                target_frames[target] = cv2.cvtColor(cv2.resize(frame, proc_res, interpolation=cv2.INTER_AREA),
                                                     cv2.COLOR_BGR2GRAY)
            curr_idx += 1
        cap.release()

        # 根据边界差分改写判定
        for p in pauses:
            b_idx = max(0, p['start'] - 1)
            a_idx = min(total - 1, p['end'] + 1)
            if b_idx in target_frames and a_idx in target_frames:
                diff = float(cv2.mean(cv2.absdiff(target_frames[b_idx], target_frames[a_idx]))[0])
                p['boundary_diff'] = diff
                # 核心机制：一旦前后差距过小，不管之前算出来动作多大，一律强制“全删”
                if diff < boundary_thresh:
                    p['mode'] = 'all'
        if progress_cb:
            progress_cb(1.0)

    return pauses, speeds


# ---------------------------------------------------------------
#  导出辅助
# ---------------------------------------------------------------

def _speedup_mask(states: np.ndarray, frame_type: int, factor: int,
                  exclude_mask: np.ndarray) -> np.ndarray:
    total = len(states)
    if factor < 1:
        raise ValueError("speedup factor must be at least 1")
    type_mask = (states == frame_type) & ~exclude_mask

    if not type_mask.any(): return np.zeros(total, dtype=bool)

    # Compute the one-based position inside each contiguous type run in one
    # linear pass.  The previous implementation rewrote ``offsets[s:]`` for
    # every run, which made many short speed segments quadratic in total
    # frame count.
    indices = np.arange(total, dtype=np.int64)
    starts = type_mask & ~np.r_[False, type_mask[:-1]]
    run_start = np.where(starts, indices, 0)
    last_start = np.maximum.accumulate(run_start)
    local_cnt = np.where(type_mask, indices - last_start + 1, 0)

    if factor == 2:
        return type_mask & (local_cnt % 2 == 0)
    return type_mask & (local_cnt % factor != 1)


def _build_delete_mask(total: int, states: np.ndarray,
                       pause_segments: list, speed_segments: list,
                       clip_segments: list,
                       speedup_1x: bool, speedup_02: bool,
                       speedup_02_factor: int) -> np.ndarray:
    del_mask = np.zeros(total, dtype=bool)

    for seg in pause_segments:
        s, e = seg['start'], seg['end']
        mode = seg.get('mode', 'auto')
        if mode == 'all':
            del_mask[s:e + 1] = True
        elif mode == 'auto' and 'local_del_mask' in seg:
            m = seg['local_del_mask']
            # 1 为自动删除，2 为人工强制删除
            del_mask[s:e + 1] = (m == 1) | (m == 2)

    for seg in clip_segments:
        s, e = seg['start'], seg['end']
        ki, ko = seg['keep_in'], seg['keep_out']
        if ki > ko:
            del_mask[s:e + 1] = True
        else:
            if ki > s:   del_mask[s:ki] = True
            if ko < e:   del_mask[ko + 1:e + 1] = True

    if speedup_1x:
        del_mask |= _speedup_mask(states, FRAME_TYPE_1X, 2, del_mask)

    if speedup_02 and speedup_02_factor > 1:
        del_mask |= _speedup_mask(states, FRAME_TYPE_0_2X, speedup_02_factor, del_mask)

    return del_mask


def _plan_delete_mask(plan: TimelinePlan) -> np.ndarray:
    mask = np.zeros(plan.total_frames, dtype=bool)
    for start, end in plan.deleted_ranges:
        mask[start:end] = True
    return mask


def build_timeline_plan(total: int, states: np.ndarray,
                        pause_segments: list, speed_segments: list,
                        clip_segments: list,
                        speedup_1x: bool, speedup_02: bool,
                        speedup_02_factor: int) -> TimelinePlan:
    """Resolve legacy edit dictionaries into one validated frame timeline."""
    mask = _build_delete_mask(
        total,
        states,
        pause_segments,
        speed_segments,
        clip_segments,
        speedup_1x,
        speedup_02,
        speedup_02_factor,
    )
    return TimelinePlan.from_delete_mask(mask)


def build_delete_set(total: int, states: np.ndarray,
                     pause_segments: list, speed_segments: list,
                     clip_segments: list,
                     speedup_1x: bool, speedup_02: bool,
                     speedup_02_factor: int) -> np.ndarray:
    """Compatibility wrapper; new consumers should keep the TimelinePlan."""
    return _plan_delete_mask(
        build_timeline_plan(
            total,
            states,
            pause_segments,
            speed_segments,
            clip_segments,
            speedup_1x,
            speedup_02,
            speedup_02_factor,
        )
    )


def _kept_frame_ranges(to_del: np.ndarray) -> list[tuple[int, int]]:
    """将删除掩码转换为左闭右开的保留帧区间。"""
    return list(TimelinePlan.from_delete_mask(to_del).kept_ranges)


def inspect_export_plan(to_del, include_audio: bool = True, *,
                        video_path: str | None = None,
                        ffmpeg_path: str | None = None,
                        pts_certified: bool = False) -> dict:
    """Return a preflight plan without conflating probe failure with no audio.

    pts_certified marks the certified PTS export route: its audio is batched
    beyond _MAX_PTS_SINGLE_GRAPH_AUDIO_RANGES, so segment count alone cannot
    overflow the audio graph and the legacy range ceiling does not apply.
    """
    if isinstance(to_del, TimelinePlan):
        plan = to_del
    else:
        mask = np.asarray(to_del, dtype=bool)
        if mask.ndim != 1:
            raise ValueError("to_del must be a one-dimensional frame mask")
        plan = TimelinePlan.from_delete_mask(mask)
    n_ranges = len(plan.kept_ranges)
    audio_limit = _MAX_FILTER_EXPORT_RANGES_AUDIO
    drop_reasons = []
    block_reasons = []
    audio_probe = None
    resolved_ffmpeg = None
    if video_path is not None:
        try:
            resolved_ffmpeg = resolve_ffmpeg_path(ffmpeg_path)
        except FileNotFoundError:
            block_reasons.append("ffmpeg_unavailable")
    # The 80-range ceiling only protects the legacy in-graph audio mixer
    # (_export_video_impl); the certified PTS route batches audio instead.
    if include_audio and n_ranges > audio_limit and not pts_certified:
        drop_reasons.append("too_many_ranges")
    if include_audio and video_path is not None:
        if resolved_ffmpeg is None:
            audio_probe = {
                "status": "unavailable",
                "present": None,
                "method": None,
                "errors": ["FFmpeg unavailable"],
            }
        else:
            audio_probe = _probe_audio_stream(
                video_path,
                ffmpeg_path=resolved_ffmpeg,
            )
            if audio_probe["present"] is None:
                drop_reasons.append("audio_probe_inconclusive")
    return {
        "n_ranges": n_ranges,
        "kept_frames": plan.kept_frames,
        "timeline_fingerprint": plan.fingerprint,
        "include_audio": bool(include_audio),
        "video_path": (
            os.path.normcase(os.path.abspath(video_path))
            if video_path is not None
            else None
        ),
        "audio_limit": audio_limit,
        "ffmpeg_path": resolved_ffmpeg,
        "audio_probe": audio_probe,
        "export_block_reasons": list(dict.fromkeys(block_reasons)),
        "export_blocked": bool(block_reasons),
        "audio_drop_reasons": list(dict.fromkeys(drop_reasons)),
        "audio_drop_requires_confirmation": bool(drop_reasons),
    }


def _probe_audio_stream(video_path: str,
                        ffmpeg_path: str | None = None) -> dict:
    errors = []
    # Primary: PyAV reads container metadata in-process (ffprobe.exe retired).
    try:
        import av

        container = av.open(str(video_path))
        try:
            present = len(container.streams.audio) > 0
        finally:
            container.close()
        return {
            "status": "pass",
            "present": present,
            "method": "pyav",
            "errors": errors,
        }
    except Exception as exc:
        errors.append(f"pyav: {type(exc).__name__}: {exc}")

    try:
        ffmpeg = resolve_ffmpeg_path(ffmpeg_path)
    except FileNotFoundError as exc:
        errors.append(str(exc))
        return {
            "status": "unavailable",
            "present": None,
            "method": None,
            "errors": errors,
        }
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", video_path,
             "-map", "0:a:0", "-frames:a", "1", "-f", "null", "-"],
            check=False, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=15,
            creationflags=_NO_WINDOW)
        if result.returncode == 0:
            return {
                "status": "pass",
                "present": True,
                "method": "ffmpeg_decode",
                "errors": errors,
            }
        stderr = (result.stderr or "").strip()
        no_stream_markers = (
            "matches no streams",
            "does not contain any stream",
            "stream map '0:a:0' matches no streams",
        )
        if any(marker in stderr.lower() for marker in no_stream_markers):
            return {
                "status": "pass",
                "present": False,
                "method": "ffmpeg_decode",
                "errors": errors,
            }
        errors.append(f"ffmpeg rc={result.returncode}: {stderr[:500]}")
        return {
            "status": "error",
            "present": None,
            "method": "ffmpeg_decode",
            "errors": errors,
        }
    except Exception as exc:
        errors.append(f"ffmpeg: {type(exc).__name__}: {exc}")
        return {
            "status": "error",
            "present": None,
            "method": "ffmpeg_decode",
            "errors": errors,
        }


def _has_audio_stream(video_path: str, ffmpeg_path: str | None = None) -> bool:
    """Compatibility helper for callers that only need a conservative bool."""
    return _probe_audio_stream(video_path, ffmpeg_path=ffmpeg_path)["present"] is True


def _gpu_encoder_probe_args(enc: str) -> list[str]:
    q = "24"
    if enc == "h264_nvenc":
        return ["-c:v", enc, "-preset", "p4", "-cq", q]
    if enc == "h264_qsv":
        return ["-c:v", enc, "-global_quality", q]
    if enc == "h264_amf":
        return ["-c:v", enc, "-usage", "transcoding", "-quality", "speed",
                "-rc", "cqp", "-qp_i", q, "-qp_p", q]
    return ["-c:v", enc]


def _gpu_encoder_works(enc: str, timeout: float = 5, ffmpeg_path: str | None = None) -> bool:
    # 单飞探测：探测全程持锁，避免多线程并发启动多个 ffmpeg 探测同一编码器，
    # 进而在 GPU 资源紧张时因并发抢占产生假阴性。
    with _GPU_PROBE_LOCK:
        cache_key = f"{enc}|{ffmpeg_path or ''}"
        cached = _GPU_PROBE_CACHE.get(cache_key)
        if cached is not None:
            return cached

        try:
            ffmpeg = resolve_ffmpeg_path(ffmpeg_path)
        except FileNotFoundError:
            _GPU_PROBE_CACHE[cache_key] = False
            return False

        try:
            subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=0.1",
                 *_gpu_encoder_probe_args(enc), "-f", "null", "-"],
                check=True, capture_output=True, timeout=timeout,
                creationflags=_NO_WINDOW)
        except FileNotFoundError:
            # FFmpeg 不在 PATH：确定不可用，缓存 False。
            _GPU_PROBE_CACHE[cache_key] = False
            return False
        except subprocess.CalledProcessError:
            # 编码器确实初始化失败（ffmpeg 正常退出且给出错误）：缓存 False。
            # 但超时（TimeoutExpired）等瞬时失败不在此分支，不缓存。
            _GPU_PROBE_CACHE[cache_key] = False
            return False
        except Exception:
            # 瞬时失败（超时、GPU 被占用、驱动初始化等）：不缓存，下次重新探测，
            # 避免一次偶发失败永久禁用该编码器。
            return False

        _GPU_PROBE_CACHE[cache_key] = True
        return True


def list_ffmpeg_gpu_encoders(ffmpeg_path: str | None = None) -> list[str]:
    try:
        ffmpeg = resolve_ffmpeg_path(ffmpeg_path)
        output = subprocess.check_output(
            [ffmpeg, "-hide_banner", "-encoders"],
            text=True, stderr=subprocess.STDOUT, timeout=10,
            creationflags=_NO_WINDOW)
    except Exception:
        return []

    # 顺序即优先级：nvenc（NVIDIA）通常最快最稳，其次是 qsv（Intel）、amf（AMD）。
    # 纯 AMD 机型误选不可用 nvenc 的问题（issue #3）已由 _gpu_encoder_works 的实测探测解决，
    # 不依赖把 amf 提前；探测通过的编码器按此序取首个即可。
    candidates = ["h264_nvenc", "h264_qsv", "h264_amf", "h264_videotoolbox"]
    return [enc for enc in candidates if enc in output]


def list_working_gpu_encoders(ffmpeg_path: str | None = None) -> list[str]:
    return [
        enc
        for enc in list_ffmpeg_gpu_encoders(ffmpeg_path)
        if _gpu_encoder_works(enc, ffmpeg_path=ffmpeg_path)
    ]


def _pick_gpu_encoder(ffmpeg_path: str | None = None) -> str | None:
    working = list_working_gpu_encoders(ffmpeg_path)
    return working[0] if working else None


def _resolve_gpu_encoder(
    gpu_encoder: str | None, ffmpeg_path: str | None = None
) -> str | None:
    selected = (gpu_encoder or "").strip()
    if selected:
        return selected if _gpu_encoder_works(selected, ffmpeg_path=ffmpeg_path) else None
    return _pick_gpu_encoder(ffmpeg_path)


def _encoder_cmd_args(enc: str, quality: int) -> list[str]:
    q = max(0, min(10, int(quality)))
    qp = str(18 + (10 - q))
    if enc == "h264_nvenc":
        return ["-c:v", enc, "-preset", "p4", "-cq", qp]
    if enc == "h264_qsv":
        return ["-c:v", enc, "-global_quality", qp]
    if enc == "h264_amf":
        return ["-c:v", enc, "-usage", "transcoding", "-quality", "speed",
                "-rc", "cqp", "-qp_i", qp, "-qp_p", qp]
    return ["-c:v", enc, "-q:v", qp]


def _video_encoder_args(
    quality: int,
    use_gpu: bool,
    gpu_encoder: str,
    ffmpeg_path: str | None = None,
) -> list[str]:
    q = max(0, min(10, int(quality)))
    if use_gpu:
        enc = _resolve_gpu_encoder(gpu_encoder, ffmpeg_path=ffmpeg_path)
        if enc:
            return _encoder_cmd_args(enc, q) + ["-pix_fmt", "yuv420p", "-threads", "0"]
    crf = int(round(28 - q))
    return ["-c:v", "libx264", "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-threads", "0"]



def _export_progress(progress_cb, ratio, written=0, status: str | None = None) -> None:
    """Call UI progress callback; support optional status string (3rd arg)."""
    if not progress_cb:
        return
    try:
        if status is not None:
            progress_cb(float(ratio), int(written), status)
        else:
            progress_cb(float(ratio), int(written))
    except TypeError:
        # older callback: (ratio, written) only
        progress_cb(float(ratio), int(written))


def _check_export_cancel(cancel_cb) -> None:
    if cancel_cb is not None:
        cancel_cb()


def _terminate_process(process: subprocess.Popen) -> None:
    """Best-effort bounded termination used by cancellable FFmpeg paths."""
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.wait(timeout=2.0)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        pass


def _run_ffmpeg_interruptible(
    cmd: list[str],
    *,
    timeout: float,
    cancel_cb=None,
) -> None:
    """Run FFmpeg while polling cooperative cancellation and a hard deadline."""
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
            _check_export_cancel(cancel_cb)
            try:
                returncode = process.wait(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() - started >= timeout:
                    raise subprocess.TimeoutExpired(cmd, timeout)
        # Cancellation may arrive after the last polling checkpoint but just
        # as the child exits.  Do not report success without one final check.
        _check_export_cancel(cancel_cb)
        stderr_file.seek(0)
        stderr = stderr_file.read()
        if returncode != 0:
            raise subprocess.CalledProcessError(returncode, cmd, stderr=stderr)
    except BaseException:
        if process is not None:
            _terminate_process(process)
        raise
    finally:
        stderr_file.close()


def _export_ranges_with_ffmpeg_filters(
        video_path: str, output_path: str, ranges: list[tuple[int, int]],
        fps: float, quality: int, use_gpu: bool, gpu_encoder: str,
        include_audio: bool, progress_cb=None,
        ffmpeg_path: str | None = None,
        cancel_cb=None) -> bool:
    """使用 FFmpeg trim/concat 快速导出左闭右开的帧区间。

    滤镜图通过 -filter_complex_script 写入文件（不走命令行），ffmpeg concat
    可处理上千段，因此不再对段数设上限；空 ranges 时直接回退逐帧路径。
    """
    if not ranges:
        return False
    _check_export_cancel(cancel_cb)

    try:
        ffmpeg = resolve_ffmpeg_path(ffmpeg_path)
    except FileNotFoundError:
        return False

    # include_audio is already the result of export preflight.  Do not probe a
    # second time and silently turn a transient probe error into a silent file.
    has_audio = bool(include_audio)
    limit = (
        _MAX_FILTER_EXPORT_RANGES_AUDIO if has_audio else _MAX_FILTER_EXPORT_RANGES
    )
    n_ranges = len(ranges)
    if n_ranges > limit:
        # 不建巨型滤镜；由 export_video 改走稳妥路径
        print(
            f"[analyzer] 保留段 {n_ranges} > {limit}，跳过快速滤镜，改用稳妥逐帧导出"
        )
        _export_progress(
            progress_cb,
            0.02,
            0,
            f"保留段过多({n_ranges}>{limit})，改用稳妥逐帧…",
        )
        return False

    with tempfile.TemporaryDirectory() as tmpdir:
        filter_file = os.path.join(tmpdir, "filter.txt")
        lines = []
        concat_inputs = []
        for idx, (start, end) in enumerate(ranges):
            _check_export_cancel(cancel_cb)
            lines.append(
                f"[0:v]trim=start_frame={start}:end_frame={end},"
                f"setpts=PTS-STARTPTS[v{idx}]")
            concat_inputs.append(f"[v{idx}]")
            if has_audio:
                lines.append(
                    f"[0:a]atrim=start={start / fps:.9f}:end={end / fps:.9f},"
                    f"asetpts=PTS-STARTPTS[a{idx}]")
                concat_inputs.append(f"[a{idx}]")

        if has_audio:
            lines.append(
                "".join(concat_inputs)
                + f"concat=n={len(ranges)}:v=1:a=1[outv][outa]")
        else:
            lines.append(
                "".join(concat_inputs)
                + f"concat=n={len(ranges)}:v=1:a=0[outv]")

        with open(filter_file, "w", encoding="utf-8") as handle:
            handle.write(";\n".join(lines))

        cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-nostats",
            "-i", video_path, "-filter_complex_script", filter_file,
            "-map", "[outv]",
        ]
        if has_audio:
            cmd += ["-map", "[outa]"]
        cmd += _video_encoder_args(quality, use_gpu, gpu_encoder, ffmpeg_path=ffmpeg)
        cmd += ["-c:a", "aac"] if has_audio else ["-an"]
        cmd.append(output_path)

        try:
            _export_progress(
                progress_cb,
                0.01,
                0,
                f"FFmpeg 快速导出中（保留段 {n_ranges}，可能较久）…",
            )
            _run_ffmpeg_interruptible(
                cmd, timeout=1800.0, cancel_cb=cancel_cb
            )
            kept = sum(end - start for start, end in ranges)
            _export_progress(progress_cb, 1.0, kept, "快速导出完成")
            return True
        except TaskCancelled:
            # Cancellation is a control-flow result, not a reason to fall
            # back to the slower OpenCV path.  The caller owns the atomic
            # staging policy, but remove this path's partial output now so a
            # direct low-level caller cannot mistake it for a valid file.
            if os.path.isfile(output_path):
                try:
                    os.remove(output_path)
                except OSError:
                    pass
            raise
        except subprocess.CalledProcessError as exc:
            # 快速滤镜路径失败：删除 ffmpeg 写了一半的输出，回退到逐帧路径。
            # 同时打印 ffmpeg 的真实 stderr，避免“静默变慢 + 无诊断”。
            err = (exc.stderr or b"").decode("utf-8", errors="ignore").strip()
            if err:
                print(f"[analyzer] 快速滤镜导出失败，回退逐帧路径。ffmpeg stderr: {err}")
            if os.path.isfile(output_path):
                os.remove(output_path)
            return False
        except Exception as exc:
            if cancel_cb is not None:
                _check_export_cancel(cancel_cb)
            # 超时/其它异常同样回退，但打印原因，不再完全静默。
            print(f"[analyzer] 快速滤镜导出异常，回退逐帧路径: {exc}")
            if os.path.isfile(output_path):
                os.remove(output_path)
            return False


def _fraction_filter_seconds(value: Fraction) -> str:
    """Render an exact Fraction as a non-exponential FFmpeg duration."""
    if not isinstance(value, Fraction) or value.denominator <= 0:
        raise ValueError("filter duration must be a Fraction")
    with localcontext() as context:
        context.prec = 40
        rendered = format(
            Decimal(value.numerator) / Decimal(value.denominator), "f"
        )
    # Only trim fractional trailing zeroes.  Stripping an integer such as
    # ``10`` would silently turn a valid FFmpeg boundary into ``1``.
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _normalize_pts_schedule(
    intervals: Sequence[Mapping[str, Any]],
    *,
    time_base: Fraction,
) -> list[tuple[int, int, int, int]]:
    """Validate the immutable frame->tick schedule used by the PTS consumer."""
    if not isinstance(time_base, Fraction) or time_base <= 0:
        raise ValueError("PTS export requires a positive Fraction time_base")
    normalized: list[tuple[int, int, int, int]] = []
    previous_end_tick: int | None = None
    for index, value in enumerate(intervals):
        if not isinstance(value, Mapping):
            raise ValueError(f"PTS interval {index} must be an object")
        frame_range = value.get("source_frame_range")
        tick_range = value.get("pts_tick_range")
        if (
            not isinstance(frame_range, (list, tuple))
            or len(frame_range) != 2
            or not isinstance(tick_range, (list, tuple))
            or len(tick_range) != 2
        ):
            raise ValueError(f"PTS interval {index} has invalid ranges")
        start_frame, end_frame = frame_range
        start_tick, end_tick = tick_range
        values = (start_frame, end_frame, start_tick, end_tick)
        if any(isinstance(item, bool) or not isinstance(item, int) for item in values):
            raise ValueError(f"PTS interval {index} requires integer frame/tick values")
        if start_frame < 0 or end_frame <= start_frame or end_tick <= start_tick:
            raise ValueError(f"PTS interval {index} is empty or reversed")
        if previous_end_tick is not None and start_tick < previous_end_tick:
            raise ValueError("PTS intervals must be ordered and non-overlapping")
        normalized.append((start_frame, end_frame, start_tick, end_tick))
        previous_end_tick = end_tick
    if not normalized:
        raise ValueError("PTS export requires at least one interval")
    return normalized


def _pts_select_setpts_video_filter(
    schedule: Sequence[tuple[int, int, int, int]],
    *,
    clone_guard: bool = True,
) -> str:
    """Single-pass, tick-exact selection and renumbering for large schedules.

    ``select`` is one flat OR of ``between`` ranges.  ``setpts`` subtracts
    every removed gap that ends at or before the frame via a flat sum of
    depth-1 ``if`` terms: ``out = PTS - start_0 - sum(gap_j if PTS >=
    gap_end_j)``.  Both alternatives fail at thousands of ranges — per-range
    ``trim`` chains cost O(ranges x frames) because every chain scans the
    whole source, and a nested piecewise ``if`` chain exceeds FFmpeg's
    expression parser depth around 100 segments.
    """
    conditions = "+".join(
        f"between(pts,{start_tick},{end_tick - 1})"
        for _start_frame, _end_frame, start_tick, end_tick in schedule
    )
    expression_parts = [f"PTS-{schedule[0][2]}"]
    for index in range(len(schedule) - 1):
        gap_start = schedule[index][3]
        gap_end = schedule[index + 1][2]
        if gap_end > gap_start:
            expression_parts.append(
                f"-if(gte(PTS,{gap_end}),{gap_end - gap_start},0)"
            )
    # The terminal clone guard gives the last real frame a positive muxed
    # duration; without it the final frame is lost at EOF.  Batched exports
    # only enable it on the final batch — a mid-stream clone would surface
    # as a duplicated frame after concatenation.
    tail = ",tpad=stop_mode=clone:stop=1" if clone_guard else ""
    return (
        f"[0:v:0]select='{conditions}',"
        f"setpts='{''.join(expression_parts)}'"
        f"{tail}[outv]"
    )


def _pts_sentinel_clone_position(
    schedule: Sequence[tuple[int, int, int, int]],
) -> tuple[int, int, int]:
    """Compute where the terminal tpad clone MUST sit on the output timeline.

    Returns ``(scheduled, clone_pts, step)``: the real frame count, the clone's
    output-timeline position (right after the last real frame), and the last
    segment's average frame step.  ``tpad`` computes the clone position from
    the *source* frame's duration metadata, which on bad-timestamp VFR sources
    can be pathological (observed 41,743,872 ticks ≈ 44 min on sample 2),
    producing a far-future phantom packet and a garbage container duration.
    The encode therefore pins the clone with an absolute ``setts`` bitstream
    filter override built from these values.
    """
    scheduled = sum(end - start for start, end, _st, _et in schedule)
    first_tick = schedule[0][2]
    clone_pts = schedule[-1][3] - first_tick
    for index in range(len(schedule) - 1):
        gap_start = schedule[index][3]
        gap_end = schedule[index + 1][2]
        if gap_end > gap_start:
            clone_pts -= gap_end - gap_start
    last_sf, last_ef, last_st, last_et = schedule[-1]
    step = max(1, round((last_et - last_st) / (last_ef - last_sf)))
    return scheduled, clone_pts, step


def _pts_sentinel_fix_args(
    schedule: Sequence[tuple[int, int, int, int]],
) -> list[str]:
    """``-bsf:v setts`` args pinning the terminal clone to its analytic slot.

    The clone is identified by *position* (the only packet beyond
    ``clone_pts - 1``), not by packet index: real output frames never exceed
    ``clone_pts - step``, so the rewrite is a no-op on healthy sources and
    stays correct even if a pathological source shifts the packet count.
    """
    _scheduled, clone_pts, step = _pts_sentinel_clone_position(schedule)
    threshold = clone_pts - 1
    expression = (
        f"setts=pts='if(gt(PTS,{threshold}),{clone_pts},PTS)'"
        f":duration='if(gt(PTS,{threshold}),{step},DURATION)'"
    )
    return ["-bsf:v", expression]


def _verify_pts_export_container(
    output_path: str,
    schedule: Sequence[tuple[int, int, int, int]],
    *,
    time_base: Fraction,
) -> None:
    """Container-level post-check for the VFR PTS export.

    Decode-level checks cannot see the phantom packet the tpad clone becomes
    on bad-timestamp sources (it is skipped by decoders in a standalone file
    but turns into a real frame after concat).  Verify the packet table
    directly: clone position, monotonic tail, and a sane declared stream
    duration.

    Packet count is intentionally a soft check: on badly non-monotonic
    sources the muxer clamps/drops duplicate-PTS frames (observed on sample
    2, pre-existing and content-identical to the accepted baseline), so the
    count may differ from ``scheduled + 1`` by a small bounded amount.  The
    anti-phantom invariants (clone position, declared duration) stay exact.
    """
    import av

    scheduled, clone_pts, step = _pts_sentinel_clone_position(schedule)
    container = av.open(output_path)
    try:
        video = container.streams.video[0]
        declared = video.duration
        count = 0
        prev_pts = None
        last_pts = None
        for packet in container.demux(video):
            if packet.pts is None:
                continue
            count += 1
            prev_pts, last_pts = last_pts, packet.pts
    finally:
        container.close()
    problems: list[str] = []
    if abs(count - (scheduled + 1)) > 2:
        problems.append(f"包数 {count} 与预期 {scheduled + 1} 偏差超过 2")
    elif count != scheduled + 1:
        print(
            f"[analyzer] 容器后检提示：包数 {count}，预期 {scheduled + 1}，"
            "差值在坏时间戳源的已知 muxer 行为范围内"
        )
    if last_pts != clone_pts:
        problems.append(f"末包 pts {last_pts} != 预期克隆位 {clone_pts}")
    if prev_pts is not None and last_pts is not None and last_pts <= prev_pts:
        problems.append(f"PTS 非单调: {prev_pts} -> {last_pts}")
    if declared is not None and abs(declared - (clone_pts + step)) > max(step, 1):
        problems.append(
            f"容器声明时长 {declared} 与预期 {clone_pts + step} 不符"
            f"（time_base={time_base}）"
        )
    if problems:
        raise RuntimeError("PTS 导出容器后检失败: " + "; ".join(problems))


def _export_audio_pts_batched(
    video_path: str,
    schedule: Sequence[tuple[int, int, int, int]],
    *,
    time_base: Fraction,
    ffmpeg: str,
    tmpdir: str,
    progress_cb=None,
    cancel_cb=None,
    ffmpeg_timeout: float = 1800.0,
) -> str:
    """Build the audio track for a large PTS schedule in bounded batches.

    A single atrim/concat graph grows superlinearly with segment count
    (measured: 400 segments ≈ 120 s, 800 segments > 600 s), while the video
    side stays cheap — so audio leaves the shared graph beyond
    ``_MAX_PTS_SINGLE_GRAPH_AUDIO_RANGES`` and is built here instead.

    Each batch holds at most ``_PTS_AUDIO_BATCH_SIZE`` segments, addressed by
    tick-exact ``Fraction`` seconds (never frame/fps arithmetic), concatenated
    inside the batch and stored as lossless PCM in a .nut container.  Per-batch
    AAC would drift at every join (measured +1280 samples per boundary from
    priming); PCM intermediates keep the joins sample-exact, and the single
    final AAC encode happens at mux time.  Returns the concat list file whose
    entries carry precise ``duration`` directives (without them the demuxer
    derives batch lengths from file metadata and boundaries drift).
    """
    batch_files: list[str] = []
    durations: list[Fraction] = []
    total_batches = (
        len(schedule) + _PTS_AUDIO_BATCH_SIZE - 1
    ) // _PTS_AUDIO_BATCH_SIZE
    for start in range(0, len(schedule), _PTS_AUDIO_BATCH_SIZE):
        _check_export_cancel(cancel_cb)
        sub = schedule[start:start + _PTS_AUDIO_BATCH_SIZE]
        batch_index = start // _PTS_AUDIO_BATCH_SIZE
        lines: list[str] = []
        labels: list[str] = []
        for index, (_sf, _ef, start_tick, end_tick) in enumerate(sub):
            start_seconds = _fraction_filter_seconds(start_tick * time_base)
            end_seconds = _fraction_filter_seconds(end_tick * time_base)
            lines.append(
                f"[0:a:0]atrim=start={start_seconds}:end={end_seconds},"
                f"asetpts=PTS-STARTPTS[a{index}]"
            )
            labels.append(f"[a{index}]")
        lines.append(
            "".join(labels) + f"concat=n={len(sub)}:v=0:a=1[outa]"
        )
        filter_file = os.path.join(tmpdir, f"audio-batch-{batch_index}.txt")
        Path(filter_file).write_text(";\n".join(lines), encoding="utf-8")
        batch_path = os.path.join(tmpdir, f"audio-batch-{batch_index}.nut")
        seek_seconds = max(0.0, float(sub[0][2] * time_base) - 1.0)
        # Input-side -t stops the decode at the batch tail; without it every
        # batch decodes to EOF (staircase waste, observed in the prototype).
        read_seconds = float(sub[-1][3] * time_base) - seek_seconds + 0.5
        cmd = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostats",
            "-ss",
            f"{seek_seconds:.9f}",
            "-t",
            f"{read_seconds:.9f}",
            "-copyts",
            "-i",
            video_path,
            "-filter_complex_script",
            filter_file,
            "-map",
            "[outa]",
            "-c:a",
            "pcm_s16le",
            "-f",
            "nut",
            batch_path,
        ]
        _run_ffmpeg_interruptible(
            cmd, timeout=ffmpeg_timeout, cancel_cb=cancel_cb
        )
        batch_files.append(batch_path)
        durations.append(
            sum(
                (end_tick - start_tick) * time_base
                for _sf, _ef, start_tick, end_tick in sub
            )
        )
        _export_progress(
            progress_cb,
            0.90 + 0.08 * (batch_index + 1) / total_batches,
            0,
            f"音频分批导出中…（{batch_index + 1}/{total_batches} 批）",
        )
    list_path = os.path.join(tmpdir, "audio-concat.txt")
    lines = []
    for index, (batch_path, duration) in enumerate(zip(batch_files, durations)):
        lines.append(f"file '{Path(batch_path).as_posix()}'")
        if index < len(batch_files) - 1:
            lines.append(f"duration {_fraction_filter_seconds(duration)}")
    Path(list_path).write_text("\n".join(lines), encoding="utf-8")
    return list_path


def _export_pts_video_single_pass(
    video_path: str,
    schedule: Sequence[tuple[int, int, int, int]],
    *,
    time_base: Fraction,
    output_fps_mode: str,
    graph_audio: bool,
    quality: int,
    use_gpu: bool,
    gpu_encoder: str,
    ffmpeg: str,
    tmpdir: str,
    output_path: str,
    scheduled_written: int,
    frame_pts_status: str | None,
    progress_cb=None,
    cancel_cb=None,
    ffmpeg_timeout: float,
) -> None:
    """Encode the whole PTS schedule in one ffmpeg pass (the filter graph
    fits within FFmpeg's expression evaluator limits)."""
    filter_file = os.path.join(tmpdir, "pts-filter.txt")
    # VFR schedules use one flat select/setpts pass over the source; the
    # cfr branch keeps the proven per-segment trim/setpts/concat chains.
    # Interval end ticks are by construction the last kept frame's natural
    # end (pts + duration), so nothing shortens a segment tail either way.
    lines: list[str] = []
    concat_inputs: list[str] = []
    if frame_pts_status == "vfr":
        lines.append(_pts_select_setpts_video_filter(schedule))
        if graph_audio:
            audio_inputs: list[str] = []
            for index, (_start_frame, _end_frame, start_tick, end_tick) in enumerate(schedule):
                start_seconds = _fraction_filter_seconds(start_tick * time_base)
                end_seconds = _fraction_filter_seconds(end_tick * time_base)
                lines.append(
                    f"[0:a:0]atrim=start={start_seconds}:end={end_seconds},"
                    f"asetpts=PTS-STARTPTS[a{index}]"
                )
                audio_inputs.append(f"[a{index}]")
            lines.append(
                "".join(audio_inputs)
                + f"concat=n={len(schedule)}:v=0:a=1[outa]"
            )
    else:
        for index, (_start_frame, _end_frame, start_tick, end_tick) in enumerate(schedule):
            lines.append(
                f"[0:v:0]trim=start_pts={start_tick}:end_pts={end_tick},"
                f"setpts=PTS-STARTPTS[v{index}]"
            )
            concat_inputs.append(f"[v{index}]")
            if graph_audio:
                start_seconds = _fraction_filter_seconds(start_tick * time_base)
                end_seconds = _fraction_filter_seconds(end_tick * time_base)
                lines.append(
                    f"[0:a:0]atrim=start={start_seconds}:end={end_seconds},"
                    f"asetpts=PTS-STARTPTS[a{index}]"
                )
                concat_inputs.append(f"[a{index}]")
        if graph_audio:
            lines.append(
                "".join(concat_inputs)
                + f"concat=n={len(schedule)}:v=1:a=1[outv][outa]"
            )
        else:
            lines.append(
                "".join(concat_inputs)
                + f"concat=n={len(schedule)}:v=1:a=0[outv]"
            )
    Path(filter_file).write_text(";\n".join(lines), encoding="utf-8")

    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostats",
        "-copyts",
        "-i",
        video_path,
        "-filter_complex_script",
        filter_file,
        "-map",
        "[outv]",
    ]
    if graph_audio:
        cmd += ["-map", "[outa]"]
    cmd += ["-fps_mode:v", output_fps_mode]
    cmd += _video_encoder_args(quality, use_gpu, gpu_encoder, ffmpeg_path=ffmpeg)
    if frame_pts_status == "vfr":
        cmd += [
            "-bf",
            "0",
            "-enc_time_base:v",
            f"{time_base.numerator}/{time_base.denominator}",
            "-video_track_timescale",
            str(time_base.denominator),
            # One extra slot for the terminal clone guard.
            "-frames:v",
            str(scheduled_written + 1),
        ]
        # Pin the terminal tpad clone to its analytic position: tpad
        # inherits the source frame's duration, which is pathological on
        # bad-timestamp VFR sources and would otherwise create a
        # far-future phantom packet and a garbage declared duration.
        cmd += _pts_sentinel_fix_args(schedule)
    cmd += ["-c:a", "aac"] if graph_audio else ["-an"]
    cmd += ["-movflags", "+faststart", output_path]

    _export_progress(
        progress_cb,
        0.01,
        0,
        f"按认证 PTS 时间表导出（{len(schedule)} 段）…",
    )
    _run_ffmpeg_interruptible(cmd, timeout=ffmpeg_timeout, cancel_cb=cancel_cb)


def _export_pts_video_batched(
    video_path: str,
    schedule: Sequence[tuple[int, int, int, int]],
    *,
    time_base: Fraction,
    quality: int,
    use_gpu: bool,
    gpu_encoder: str,
    ffmpeg: str,
    tmpdir: str,
    output_path: str,
    progress_cb=None,
    cancel_cb=None,
    ffmpeg_timeout: float,
) -> None:
    """VFR video pass for schedules beyond the single-pass expression ceiling.

    Each batch reuses the single-pass flat select/setpts filter on its own
    sub-schedule (bounded expression size), seeks with ``-ss`` + ``-copyts``
    so only the batch span is decoded, and stops at a ``-frames:v`` cap.
    Batches are stream-copied together by the concat demuxer with exact
    duration directives (batch span = sum of kept tick lengths, so the
    concatenated timeline equals the global compressed tick axis).  Only the
    final batch carries the terminal clone guard and its setts fix; a
    mid-stream clone would surface as a duplicated frame.
    """
    batch_size = _PTS_VIDEO_BATCH_RANGES
    batches = [
        schedule[index : index + batch_size]
        for index in range(0, len(schedule), batch_size)
    ]
    tb_text = f"{time_base.numerator}/{time_base.denominator}"
    tb_float = float(time_base)
    batch_files: list[str] = []
    batch_span_ticks: list[int] = []
    frames_done = 0
    for batch_index, sub in enumerate(batches):
        last_batch = batch_index == len(batches) - 1
        batch_frames = sum(end - start for start, end, _st, _et in sub)
        batch_span = sum(end_tick - start_tick for _sf, _ef, start_tick, end_tick in sub)
        filter_file = os.path.join(tmpdir, f"pts-filter-{batch_index:04d}.txt")
        Path(filter_file).write_text(
            _pts_select_setpts_video_filter(sub, clone_guard=last_batch),
            encoding="utf-8",
        )
        batch_path = os.path.join(tmpdir, f"vbatch-{batch_index:04d}.mp4")
        seek_seconds = max(0.0, sub[0][2] * tb_float - 1.0)
        cmd = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostats",
            "-ss",
            f"{seek_seconds:.9f}",
            # -copyts keeps certified source ticks so the batch filter still
            # addresses frames in global tick coordinates.
            "-copyts",
            "-i",
            video_path,
            "-filter_complex_script",
            filter_file,
            "-map",
            "[outv]",
            "-fps_mode:v",
            "passthrough",
        ]
        cmd += _video_encoder_args(quality, use_gpu, gpu_encoder, ffmpeg_path=ffmpeg)
        cmd += [
            "-bf",
            "0",
            "-enc_time_base:v",
            tb_text,
            "-video_track_timescale",
            str(time_base.denominator),
            # The output cap stops the batch right after its last kept frame
            # instead of decoding to EOF; the final batch gets one extra slot
            # for the terminal clone guard.
            "-frames:v",
            str(batch_frames + (1 if last_batch else 0)),
        ]
        if last_batch:
            cmd += _pts_sentinel_fix_args(sub)
        cmd += ["-an", "-movflags", "+faststart", batch_path]
        _export_progress(
            progress_cb,
            0.01 + 0.84 * (batch_index / len(batches)),
            frames_done,
            f"视频分批导出 {batch_index + 1}/{len(batches)}（每批 ≤{batch_size} 段）…",
        )
        _run_ffmpeg_interruptible(cmd, timeout=ffmpeg_timeout, cancel_cb=cancel_cb)
        batch_files.append(batch_path)
        batch_span_ticks.append(batch_span)
        frames_done += batch_frames
        _check_export_cancel(cancel_cb)

    concat_list = os.path.join(tmpdir, "video-concat.txt")
    list_lines: list[str] = []
    for batch_index, batch_path in enumerate(batch_files):
        list_lines.append(f"file '{Path(batch_path).as_posix()}'")
        if batch_index < len(batch_files) - 1:
            seconds = batch_span_ticks[batch_index] * tb_float
            list_lines.append(f"duration {seconds:.9f}")
    Path(concat_list).write_text("\n".join(list_lines), encoding="utf-8")
    _export_progress(progress_cb, 0.86, frames_done, "拼接视频分批…")
    concat_cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostats",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        concat_list,
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        output_path,
    ]
    _run_ffmpeg_interruptible(concat_cmd, timeout=ffmpeg_timeout, cancel_cb=cancel_cb)


def export_pts_schedule(
    video_path: str,
    output_path: str,
    intervals: Sequence[Mapping[str, Any]],
    *,
    time_base: Fraction,
    source_frame_count: int,
    reported_total_frames: int,
    quality: int,
    use_gpu: bool = False,
    gpu_encoder: str = "",
    ffmpeg_path: str | None = None,
    include_audio: bool = False,
    source_has_audio: bool = False,
    frame_pts_status: str | None = None,
    progress_cb=None,
    cancel_cb=None,
    ffmpeg_timeout: float = 1800.0,
) -> tuple[int, int, dict[str, Any]]:
    """Export an immutable PTS schedule without falling back to FPS arithmetic.

    ``trim=start_pts/end_pts`` addresses video in the certified stream time
    base.  Audio boundaries are derived from the same integer ticks as exact
    ``Fraction`` seconds.  Every segment is reset before concat.  A certified
    CFR source uses FFmpeg's CFR muxing mode so encoded packets retain positive
    durations; a VFR source keeps passthrough mode so no frame clock is
    invented.  The default remains passthrough for legacy direct callers.
    """
    if (
        isinstance(source_frame_count, bool)
        or not isinstance(source_frame_count, int)
        or source_frame_count <= 0
    ):
        raise ValueError("source_frame_count must be a positive integer")
    if (
        isinstance(reported_total_frames, bool)
        or not isinstance(reported_total_frames, int)
        or reported_total_frames <= 0
    ):
        raise ValueError("reported_total_frames must be a positive integer")
    schedule = _normalize_pts_schedule(intervals, time_base=time_base)
    if schedule[-1][1] > source_frame_count:
        raise ValueError("PTS interval exceeds the certified source frame count")
    if frame_pts_status not in {None, "cfr", "vfr"}:
        raise ValueError("frame_pts_status must be cfr, vfr, or None")
    if frame_pts_status != "vfr" and len(schedule) > _MAX_PTS_EXPORT_RANGES:
        raise RuntimeError(
            f"认证 PTS 时间表段数超过 FFmpeg 滤镜安全上限（{_MAX_PTS_EXPORT_RANGES}），"
            "拒绝回退到 FPS 导出"
        )
    output_fps_mode = "cfr" if frame_pts_status == "cfr" else "passthrough"
    scheduled_written = sum(
        end - start for start, end, _start_tick, _end_tick in schedule
    )
    ffmpeg = resolve_ffmpeg_path(ffmpeg_path)
    has_audio = bool(include_audio and source_has_audio)
    # Beyond the single-graph audio ceiling the shared atrim/concat graph
    # grows superlinearly with segment count and would stall the whole
    # export, so large schedules keep the video pass audio-free and build
    # the audio track in bounded batches afterwards (PCM intermediates, one
    # final AAC encode at mux time).
    batch_audio = (
        has_audio and len(schedule) > _MAX_PTS_SINGLE_GRAPH_AUDIO_RANGES
    )
    graph_audio = has_audio and not batch_audio
    # One flat select/setpts expression pair per pass overflows FFmpeg's
    # expression evaluator stack somewhere between 4000 and 13619 terms
    # (observed: exit code 0xC00000FD), so beyond the single-pass ceiling
    # the VFR video pass is encoded in bounded -ss-seeked batches and
    # stitched with the concat demuxer.  Audio batching is independent of
    # this and stays unchanged.
    batched_video = (
        frame_pts_status == "vfr" and len(schedule) > _MAX_PTS_EXPORT_RANGES
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        video_target = output_path
        if batch_audio:
            video_target = os.path.join(tmpdir, "video-only.mp4")
        _check_export_cancel(cancel_cb)
        try:
            if batched_video:
                _export_pts_video_batched(
                    video_path,
                    schedule,
                    time_base=time_base,
                    quality=quality,
                    use_gpu=use_gpu,
                    gpu_encoder=gpu_encoder,
                    ffmpeg=ffmpeg,
                    tmpdir=tmpdir,
                    output_path=video_target,
                    progress_cb=progress_cb,
                    cancel_cb=cancel_cb,
                    ffmpeg_timeout=ffmpeg_timeout,
                )
            else:
                _export_pts_video_single_pass(
                    video_path,
                    schedule,
                    time_base=time_base,
                    output_fps_mode=output_fps_mode,
                    graph_audio=graph_audio,
                    quality=quality,
                    use_gpu=use_gpu,
                    gpu_encoder=gpu_encoder,
                    ffmpeg=ffmpeg,
                    tmpdir=tmpdir,
                    output_path=video_target,
                    scheduled_written=scheduled_written,
                    frame_pts_status=frame_pts_status,
                    progress_cb=progress_cb,
                    cancel_cb=cancel_cb,
                    ffmpeg_timeout=ffmpeg_timeout,
                )
        except BaseException:
            for stale in (output_path, video_target):
                if os.path.isfile(stale):
                    try:
                        os.remove(stale)
                    except OSError:
                        pass
            raise

        audio_mode = "disabled"
        if batch_audio:
            # Audio is built only after the video pass succeeded; an audio
            # failure degrades to the (already complete) silent video instead
            # of deleting the whole export.
            try:
                audio_list = _export_audio_pts_batched(
                    video_path,
                    schedule,
                    time_base=time_base,
                    ffmpeg=ffmpeg,
                    tmpdir=tmpdir,
                    progress_cb=progress_cb,
                    cancel_cb=cancel_cb,
                    ffmpeg_timeout=ffmpeg_timeout,
                )
                mux_cmd = [
                    ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostats",
                    "-i",
                    video_target,
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    audio_list,
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-c:v",
                    "copy",
                    "-c:a",
                    "aac",
                    "-movflags",
                    "+faststart",
                    output_path,
                ]
                _run_ffmpeg_interruptible(
                    mux_cmd, timeout=ffmpeg_timeout, cancel_cb=cancel_cb
                )
                audio_mode = "muxed"
            except TaskCancelled:
                for stale in (output_path, video_target):
                    if os.path.isfile(stale):
                        try:
                            os.remove(stale)
                        except OSError:
                            pass
                raise
            except BaseException as exc:
                print(f"[analyzer] 音频分批导出失败，降级为无声成片: {exc}")
                audio_mode = "failed_video_only"
            if audio_mode == "failed_video_only" and os.path.isfile(video_target):
                os.replace(video_target, output_path)
            elif os.path.isfile(video_target):
                os.remove(video_target)
        elif graph_audio:
            audio_mode = "muxed"
        elif include_audio:
            audio_mode = "no_stream"

    if not os.path.isfile(output_path) or os.path.getsize(output_path) <= 0:
        raise RuntimeError("PTS 导出器未生成有效输出文件")
    if frame_pts_status == "vfr":
        # Decode-level checks are blind to phantom packets; verify the packet
        # table itself (count, clone position, monotonicity, declared duration).
        _verify_pts_export_container(
            output_path, schedule, time_base=time_base
        )
    written = scheduled_written
    _export_progress(progress_cb, 1.0, written, "认证 PTS 导出完成")
    return written, reported_total_frames, {
        "audio_mode": audio_mode,
        "pts_audio_consumer": (
            "batched_pcm" if batch_audio
            else "single_graph" if graph_audio
            else "none"
        ),
        "pts_table_consumed": True,
        "pts_video_consumer": (
            "batched_seek_concat" if batched_video else "single_pass"
        ),
        "pts_consumer": (
            "ffmpeg_select_pts_setpts_flat"
            if frame_pts_status == "vfr"
            else "ffmpeg_trim_pts_concat"
        ),
        "pts_consumer_command_scope": (
            "video_select_flat_setpts_gap_sum_vfr"
            if frame_pts_status == "vfr"
            else f"video_start_pts_end_pts_{output_fps_mode}"
        ),
        "pts_output_fps_mode": output_fps_mode,
        "pts_terminal_guard": frame_pts_status == "vfr",
        "pts_schedule_segment_count": len(schedule),
        "pts_schedule_frame_count": written,
        "pts_time_base": f"{time_base.numerator}/{time_base.denominator}",
        "source_frame_count": source_frame_count,
    }
def _mux_audio_for_ranges(video_path: str, video_only_path: str,
                          output_path: str, ranges: list[tuple[int, int]],
                          fps: float, ffmpeg_path: str | None = None,
                          progress_cb=None,
                          source_has_audio: bool | None = None,
                          cancel_cb=None) -> str:
    """Mux range-trimmed audio onto video-only file.

    Returns audio_mode: 'muxed' | 'no_stream'.
    Caller must decide segment-limit / user-disable before calling.
    """
    if source_has_audio is None:
        _check_export_cancel(cancel_cb)
        probe = _probe_audio_stream(video_path, ffmpeg_path=ffmpeg_path)
        source_has_audio = probe["present"]
        if source_has_audio is None:
            raise RuntimeError(
                "无法确认源片音轨，拒绝把探测失败当作无音轨: "
                + "; ".join(probe.get("errors") or [])
            )
    if source_has_audio is False:
        os.replace(video_only_path, output_path)
        return "no_stream"

    ffmpeg = resolve_ffmpeg_path(ffmpeg_path)
    with tempfile.TemporaryDirectory() as tmpdir:
        filter_file = os.path.join(tmpdir, "audio-filter.txt")
        lines = []
        labels = []
        for idx, (start, end) in enumerate(ranges):
            _check_export_cancel(cancel_cb)
            lines.append(
                f"[0:a]atrim=start={start / fps:.9f}:end={end / fps:.9f},"
                f"asetpts=PTS-STARTPTS[a{idx}]")
            labels.append(f"[a{idx}]")
        lines.append("".join(labels) + f"concat=n={len(ranges)}:v=0:a=1[outa]")
        with open(filter_file, "w", encoding="utf-8") as handle:
            handle.write(";\n".join(lines))

        try:
            _export_progress(
                progress_cb, 0.98, 0,
                f"混音编码中…（共 {len(ranges)} 段）",
            )
            _run_ffmpeg_interruptible(
                [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-nostats",
                 "-i", video_path, "-i", video_only_path,
                 "-filter_complex_script", filter_file,
                 "-map", "1:v:0", "-map", "[outa]",
                 "-c:v", "copy", "-c:a", "aac", "-shortest", output_path],
                timeout=1800.0,
                cancel_cb=cancel_cb,
            )
            return "muxed"
        except TaskCancelled:
            if os.path.isfile(output_path):
                try:
                    os.remove(output_path)
                except OSError:
                    pass
            raise
        except Exception as exc:
            # 混流失败：删除可能被 ffmpeg 截断/写半的损坏输出，避免残留假成品。
            if os.path.isfile(output_path):
                try:
                    os.remove(output_path)
                except OSError:
                    pass
            stderr = b""
            if isinstance(exc, subprocess.CalledProcessError):
                stderr = exc.stderr or b""
            if stderr:
                raise RuntimeError(
                    f"音频混流失败: {stderr.decode('utf-8', errors='ignore')}"
                ) from exc
            if cancel_cb is not None:
                _check_export_cancel(cancel_cb)
            raise


def _export_video_impl(video_path: str, output_path: str, to_del,
                 fps: float, quality: int, progress_cb=None,
                  use_gpu: bool = False, gpu_encoder: str = "",
                  ffmpeg_path: str | None = None,
                  include_audio: bool = True,
                 allow_audio_drop: bool = False,
                 cancel_cb=None,
                 preflight: dict | None = None):
    """导出整段剪辑。

    优先 FFmpeg trim/concat 快速路径；段数过多或失败时回退到
    OpenCV 解码 + FFmpeg pipe 编码的稳妥逐帧路径。
    快速路径成功时不打开 OpenCV VideoCapture。
    include_audio=False 时不混音、快速路径也不映射音轨。
    保留段过多时跳过「精密切音」(atrim×N)，直接输出无音画面（M2a）。

    返回 (written, total, meta)，meta['audio_mode'] 为
    muxed | no_stream | disabled | skipped_segments | skipped_unavailable |
    skipped_probe。
    progress_cb 可为 (ratio, written) 或 (ratio, written, status)。
    """
    _check_export_cancel(cancel_cb)
    if isinstance(to_del, TimelinePlan):
        timeline_plan = to_del
        total = timeline_plan.total_frames
        to_del = _plan_delete_mask(timeline_plan)
    elif isinstance(to_del, set):
        cap0 = cv2.VideoCapture(video_path)
        try:
            total = int(cap0.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        finally:
            cap0.release()
        mask = np.zeros(total, dtype=bool)
        for idx in to_del:
            if 0 <= idx < total:
                mask[idx] = True
        to_del = mask
        timeline_plan = TimelinePlan.from_delete_mask(to_del)
    else:
        to_del = np.asarray(to_del, dtype=bool)
        if to_del.ndim != 1:
            raise ValueError("to_del must be a one-dimensional frame mask")
        total = int(to_del.shape[0])
        timeline_plan = TimelinePlan.from_delete_mask(to_del)

    if total <= 0:
        raise RuntimeError("没有可导出的帧")

    ranges = list(timeline_plan.kept_ranges)
    written_fast = timeline_plan.kept_frames
    if not ranges:
        raise RuntimeError("没有可导出的帧")
    _check_export_cancel(cancel_cb)

    if preflight is None:
        plan = inspect_export_plan(
            timeline_plan,
            include_audio=include_audio,
            video_path=video_path,
            ffmpeg_path=ffmpeg_path,
        )
    else:
        plan = dict(preflight)
        expected_source = os.path.normcase(os.path.abspath(video_path))
        if plan.get("timeline_fingerprint") != timeline_plan.fingerprint:
            raise ValueError("preflight timeline does not match export timeline")
        if int(plan.get("n_ranges", -1)) != len(timeline_plan.kept_ranges):
            raise ValueError("preflight range count does not match export timeline")
        if bool(plan.get("include_audio")) != bool(include_audio):
            raise ValueError("preflight audio policy does not match export request")
        if plan.get("video_path") != expected_source:
            raise ValueError("preflight source path does not match export source")
    n_ranges = int(plan["n_ranges"])
    audio_limit = int(plan["audio_limit"])
    want_audio = bool(include_audio)
    audio_probe = plan.get("audio_probe") or {}
    source_has_audio = audio_probe.get("present")
    block_reasons = list(plan.get("export_block_reasons") or [])
    drop_reasons = list(plan.get("audio_drop_reasons") or [])
    if block_reasons:
        labels = {
            "ffmpeg_unavailable": "FFmpeg 不可用",
        }
        reason_text = "；".join(
            labels.get(value, value) for value in block_reasons
        )
        raise RuntimeError(
            f"当前导出不可用（{reason_text}）。"
            "请在设置中填写 FFmpeg 路径或安装 imageio-ffmpeg。"
        )
    # 精混音仅在段数可控时进行（与带音快速滤镜门槛一致）
    precise_audio_ok = bool(
        want_audio
        and n_ranges <= audio_limit
        and source_has_audio is True
        and plan.get("ffmpeg_path")
    )
    if plan["audio_drop_requires_confirmation"] and not allow_audio_drop:
        labels = {
            "too_many_ranges": f"保留段 {n_ranges} 超过安全上限 {audio_limit}",
            "ffmpeg_unavailable": "FFmpeg 不可用",
            "audio_probe_inconclusive": "无法确认源片音轨",
        }
        reason_text = "；".join(labels.get(value, value) for value in drop_reasons)
        raise RuntimeError(
            f"当前导出不能保证保留音频（{reason_text}），只能生成无声视频。"
            "请关闭“保留音频”，"
            "或在明确确认后允许无声导出。"
        )

    _export_progress(
        progress_cb, 0.0, 0,
        f"准备导出… 保留 {written_fast}/{total} 帧，{n_ranges} 段"
        + ("" if want_audio else "，按设置无音"),
    )

    ffmpeg_bin = plan.get("ffmpeg_path")
    if not ffmpeg_bin:
        try:
            ffmpeg_bin = resolve_ffmpeg_path(ffmpeg_path)
        except FileNotFoundError as exc:
            raise RuntimeError(
                "导出需要 FFmpeg；请在设置中填写 FFmpeg 路径或安装 imageio-ffmpeg。"
            ) from exc

    def _audio_mode_without_mux() -> str:
        if not want_audio:
            return "disabled"
        if source_has_audio is False:
            return "no_stream"
        if "too_many_ranges" in drop_reasons:
            return "skipped_segments"
        if "ffmpeg_unavailable" in drop_reasons:
            return "skipped_unavailable"
        return "skipped_probe"

    def _meta(mode: str) -> dict:
        return {
            "audio_mode": mode,
            "n_ranges": n_ranges,
            "audio_limit": audio_limit,
            "timeline_fingerprint": timeline_plan.fingerprint,
            "audio_drop_reasons": drop_reasons,
            "audio_probe_status": audio_probe.get("status"),
            "ffmpeg_path": plan.get("ffmpeg_path"),
        }

    # ---- 路径 A：FFmpeg 滤镜（不占 OpenCV cap）----
    # 要精混音才带音进 filter；段过多或用户关音频 → 仅视频滤镜 / 或不走 filter
    if ffmpeg_bin:
        filter_with_audio = precise_audio_ok
        reason = (
            f"尝试 FFmpeg 快速导出（{n_ranges} 段"
            + ("，含音" if filter_with_audio else "，无精混音")
            + "）…"
        )
        _export_progress(progress_cb, 0.01, 0, reason)
        if _export_ranges_with_ffmpeg_filters(
                video_path, output_path, ranges, fps, quality,
                use_gpu, gpu_encoder, filter_with_audio, progress_cb,
                ffmpeg_path=ffmpeg_bin, cancel_cb=cancel_cb):
            mode = "muxed" if precise_audio_ok else _audio_mode_without_mux()
            return written_fast, total, _meta(mode)
        _export_progress(
            progress_cb, 0.05, 0,
            f"快速路径未用，稳妥逐帧导出中（{n_ranges} 段）…",
        )
    # ---- 路径 B：OpenCV 解码 + FFmpeg pipe 编码 ----
    cap = cv2.VideoCapture(video_path)
    ret, sample = cap.read()
    if not ret:
        cap.release()
        raise RuntimeError("无法读取视频帧")
    h, w = sample.shape[:2]
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

    video_only_path = output_path + ".video-only.tmp.mp4"
    writer_kind = "ffmpeg"
    ffmpeg_proc = None

    try:
        _export_progress(progress_cb, 0.06, 0, "启动 FFmpeg 编码器（逐帧）…")
        ffmpeg_proc = _open_ffmpeg_pipe_writer(
            video_only_path, fps, w, h, quality,
            use_gpu=use_gpu, gpu_encoder=gpu_encoder, ffmpeg_path=ffmpeg_bin)
    except BaseException:
        _cleanup_failed_writer_start(
            cap, video_only_path, writer_kind, None, ffmpeg_proc
        )
        raise

    written = 0
    video_writer_completed = False
    try:
        idx = 0
        while idx < total:
            _check_export_cancel(cancel_cb)
            if to_del[idx]:
                next_keep = idx + 1
                while next_keep < total and to_del[next_keep]:
                    next_keep += 1
                gap = next_keep - idx
                if gap > 30:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, next_keep)
                else:
                    for _ in range(gap):
                        cap.read()
                idx = next_keep
                continue

            ret, frame = cap.read()
            if not ret:
                raise RuntimeError(
                    f"视频在源帧 {idx} 处提前结束，"
                    f"时间线仍需读取到源帧 {total - 1}"
                )
            if ffmpeg_proc is None:
                raise RuntimeError("FFmpeg 写入器未初始化")
            ffmpeg_proc.stdin.write(frame.tobytes())
            written += 1
            idx += 1
            if progress_cb and written % 60 == 0:
                _export_progress(
                    progress_cb,
                    min(0.95, idx / max(1, total)),
                    written,
                    f"逐帧写入 {written} 帧（源进度 {int(100 * idx / max(1, total))}%）…",
                )
        _close_video_writer(
            writer_kind, None, ffmpeg_proc, cancel_cb=cancel_cb
        )
        video_writer_completed = True
    finally:
        cap.release()
        if not video_writer_completed:
            try:
                _abort_video_writer(writer_kind, None, ffmpeg_proc)
            except BaseException:
                # Preserve the export/cancellation error; cleanup is best effort.
                pass
            if os.path.isfile(video_only_path):
                try:
                    os.remove(video_only_path)
                except OSError:
                    pass

    audio_mode = "none"
    try:
        if not want_audio:
            _export_progress(
                progress_cb, 0.97, written,
                "按设置跳过音频，封装无音视频…",
            )
            os.replace(video_only_path, output_path)
            audio_mode = "disabled"
        elif not precise_audio_ok:
            audio_mode = _audio_mode_without_mux()
            status = {
                "no_stream": "源片无音轨，封装无音视频…",
                "skipped_segments": (
                    f"保留段过多({n_ranges}>{audio_limit})，跳过精混音…"
                ),
                "skipped_probe": "音轨探测未通过，按确认结果导出无音视频…",
            }.get(audio_mode, "按确认结果导出无音视频…")
            _export_progress(progress_cb, 0.97, written, status)
            print(
                f"[analyzer] skip precise audio mux: mode={audio_mode}; "
                f"reasons={drop_reasons}",
                flush=True,
            )
            _check_export_cancel(cancel_cb)
            os.replace(video_only_path, output_path)
        else:
            _export_progress(
                progress_cb, 0.97, written,
                f"混音中…（共 {n_ranges} 段）",
            )
            audio_mode = _mux_audio_for_ranges(
                video_path, video_only_path, output_path, ranges, fps,
                ffmpeg_path=ffmpeg_bin, progress_cb=progress_cb,
                source_has_audio=True, cancel_cb=cancel_cb,
            )
    finally:
        if os.path.isfile(video_only_path):
            try:
                os.remove(video_only_path)
            except OSError:
                pass

    _export_progress(progress_cb, 1.0, written, f"完成 {written}/{total} 帧")
    return written, total, _meta(audio_mode)


def export_video(video_path: str, output_path: str, to_del,
                 fps: float, quality: int, progress_cb=None,
                 use_gpu: bool = False, gpu_encoder: str = "",
                 ffmpeg_path: str | None = None,
                 include_audio: bool = True,
                 allow_audio_drop: bool = False,
                 cancel_cb=None,
                 commit_cb=None,
                 preflight: dict | None = None):
    """Export atomically, preserving an existing destination on failure."""
    if os.path.normcase(os.path.realpath(video_path)) == os.path.normcase(
        os.path.realpath(output_path)
    ):
        raise ValueError("export destination must not replace the source video")
    if preflight is not None and preflight.get("ffmpeg_path"):
        ffmpeg_bin = str(preflight["ffmpeg_path"])
    else:
        try:
            ffmpeg_bin = resolve_ffmpeg_path(ffmpeg_path)
        except FileNotFoundError as exc:
            raise RuntimeError(
                "导出需要 FFmpeg；请在设置中填写 FFmpeg 路径或安装 imageio-ffmpeg。"
            ) from exc
    output_dir = os.path.dirname(os.path.abspath(output_path))
    prefix = f".{os.path.basename(output_path)}."
    output_ext = os.path.splitext(output_path)[1] or ".mp4"
    fd, staging_path = tempfile.mkstemp(
        prefix=prefix,
        suffix=f".partial{output_ext}",
        dir=output_dir,
    )
    os.close(fd)
    video_only_staging = staging_path + ".video-only.tmp.mp4"
    try:
        try:
            os.remove(staging_path)
        except OSError:
            pass
        result = _export_video_impl(
            video_path,
            staging_path,
            to_del,
            fps,
            quality,
            progress_cb,
            use_gpu=use_gpu,
            gpu_encoder=gpu_encoder,
            ffmpeg_path=ffmpeg_bin,
            include_audio=include_audio,
            allow_audio_drop=allow_audio_drop,
            cancel_cb=cancel_cb,
            preflight=preflight,
        )
        _check_export_cancel(cancel_cb)
        if not os.path.isfile(staging_path) or os.path.getsize(staging_path) <= 0:
            raise RuntimeError("导出器未生成有效的临时输出文件")
        if commit_cb is None:
            os.replace(staging_path, output_path)
        else:
            commit_cb(staging_path, output_path)
        return result
    finally:
        for candidate in (staging_path, video_only_staging):
            if os.path.isfile(candidate):
                try:
                    os.remove(candidate)
                except OSError:
                    pass


def export_ranges(video_path: str, output_path: str, ranges: list,
                  fps: float, quality: int, progress_cb=None,
                   use_gpu: bool = False, gpu_encoder: str = "",
                   ffmpeg_path: str | None = None,
                   cancel_cb=None):
    """Export source-frame ranges using strict half-open intervals.

    ``ranges`` is normalized through :class:`TimelinePlan` so callers cannot
    accidentally mix the historical inclusive ``end`` convention with the
    rest of the application.  Adjacent/overlapping ranges are equivalent to
    their union and are emitted once.
    """
    if not ranges:
        return 0, 0
    _check_export_cancel(cancel_cb)

    try:
        max_end = max(int(pair[1]) for pair in ranges)
    except (TypeError, ValueError, IndexError) as exc:
        raise ValueError("ranges must contain half-open (start, end) pairs") from exc
    if max_end <= 0:
        return 0, 0
    range_plan = TimelinePlan.from_kept_ranges(max_end, ranges)
    exclusive_ranges = list(range_plan.kept_ranges)
    total_frames_to_export = range_plan.kept_frames
    if not exclusive_ranges or total_frames_to_export <= 0:
        return 0, 0
    try:
        ffmpeg_bin = resolve_ffmpeg_path(ffmpeg_path)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "分段导出需要 FFmpeg；请在设置中填写 FFmpeg 路径或安装 imageio-ffmpeg。"
        ) from exc
    if _export_ranges_with_ffmpeg_filters(
            video_path, output_path, exclusive_ranges, fps, quality,
            use_gpu, gpu_encoder, False, progress_cb,
            ffmpeg_path=ffmpeg_bin, cancel_cb=cancel_cb):
        return total_frames_to_export, total_frames_to_export

    cap = cv2.VideoCapture(video_path)
    first_start = exclusive_ranges[0][0]
    cap.set(cv2.CAP_PROP_POS_FRAMES, first_start)
    ret, sample = cap.read()
    if not ret:
        cap.release()
        raise RuntimeError("无法读取视频帧")
    h, w = sample.shape[:2]
    cap.set(cv2.CAP_PROP_POS_FRAMES, first_start)

    writer_kind = "ffmpeg"
    ffmpeg_proc = None
    try:
        ffmpeg_proc = _open_ffmpeg_pipe_writer(
            output_path, fps, w, h, quality,
            use_gpu=use_gpu, gpu_encoder=gpu_encoder, ffmpeg_path=ffmpeg_bin)
    except BaseException:
        _cleanup_failed_writer_start(
            cap, output_path, writer_kind, None, ffmpeg_proc
        )
        raise

    written = 0
    completed = False
    try:
        for start, end in exclusive_ranges:
            _check_export_cancel(cancel_cb)
            if int(cap.get(cv2.CAP_PROP_POS_FRAMES)) != start:
                cap.set(cv2.CAP_PROP_POS_FRAMES, start)
            for offset in range(end - start):
                _check_export_cancel(cancel_cb)
                ret, frame = cap.read()
                if not ret:
                    raise RuntimeError(
                        f"视频在源帧 {start + offset} 处提前结束，"
                        f"需要读取区间 [{start}, {end})"
                    )
                if ffmpeg_proc is None:
                    raise RuntimeError("FFmpeg 写入器未初始化")
                ffmpeg_proc.stdin.write(frame.tobytes())
                written += 1
                if progress_cb and written % 30 == 0:
                    progress_cb(written / total_frames_to_export, written)
                _check_export_cancel(cancel_cb)
        _close_video_writer(
            writer_kind, None, ffmpeg_proc, cancel_cb=cancel_cb
        )
        completed = True
    finally:
        cap.release()
        if not completed:
            try:
                _abort_video_writer(writer_kind, None, ffmpeg_proc)
            except BaseException:
                # Preserve the export/cancellation error; cleanup is best effort.
                pass
            if os.path.isfile(output_path):
                try:
                    os.remove(output_path)
                except OSError:
                    pass
    return written, total_frames_to_export


def _open_ffmpeg_pipe_writer(output_path: str, fps: float, w: int, h: int, quality: int, use_gpu: bool,
                             gpu_encoder: str = "", ffmpeg_path: str | None = None):
    q = max(0, min(10, int(quality)))
    crf = int(round(28 - q))
    ffmpeg = resolve_ffmpeg_path(ffmpeg_path)
    base_cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-nostats",
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{w}x{h}", "-r", f"{fps}", "-i", "-", "-an",
    ]

    if use_gpu:
        enc = _resolve_gpu_encoder(gpu_encoder, ffmpeg_path=ffmpeg)
        if enc:
            cmd = base_cmd + _encoder_cmd_args(enc, q) + ["-pix_fmt", "yuv420p", output_path]
            return _spawn_ffmpeg_pipe(cmd)

    cmd = base_cmd + ["-c:v", "libx264", "-crf", str(crf), "-pix_fmt", "yuv420p", output_path]
    return _spawn_ffmpeg_pipe(cmd)


def _spawn_ffmpeg_pipe(cmd: list[str]):
    stderr_file = tempfile.TemporaryFile()
    try:
        process = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stderr=stderr_file,
            creationflags=_NO_WINDOW)
    except Exception:
        stderr_file.close()
        raise
    return _FFmpegPipe(process, stderr_file)


def _abort_video_writer(writer_kind, writer, ffmpeg_proc) -> None:
    """Abort a partially initialized/active writer without blocking on flush."""
    if writer_kind == "ffmpeg" and ffmpeg_proc:
        # A buffered stdin.close() may flush and block while FFmpeg is still
        # alive.  Stop the consumer first, then discard the pipe resources.
        _terminate_process(ffmpeg_proc.process)
        try:
            ffmpeg_proc.stdin.close()
        except (OSError, RuntimeError, ValueError):
            pass
        try:
            ffmpeg_proc.stderr_file.close()
        except (OSError, ValueError):
            pass


def _cleanup_failed_writer_start(
    cap, output_path: str, writer_kind, writer, ffmpeg_proc
) -> None:
    """Release startup resources and discard an encoder's partial artifact."""
    try:
        _abort_video_writer(writer_kind, writer, ffmpeg_proc)
    except BaseException:
        # Preserve the writer-start exception; cleanup is best effort.
        pass
    try:
        cap.release()
    except BaseException:
        pass
    if os.path.isfile(output_path):
        try:
            os.remove(output_path)
        except OSError:
            pass


def _close_video_writer(writer_kind, writer, ffmpeg_proc, cancel_cb=None):
    if writer_kind == "ffmpeg" and ffmpeg_proc:
        stderr_file = ffmpeg_proc.stderr_file
        try:
            _check_export_cancel(cancel_cb)
        except BaseException:
            _abort_video_writer(writer_kind, writer, ffmpeg_proc)
            raise
        try:
            ffmpeg_proc.stdin.close()
        except OSError:
            pass
        timed_out = False
        try:
            deadline = time.monotonic() + 1800.0
            while True:
                try:
                    _check_export_cancel(cancel_cb)
                except BaseException:
                    _abort_video_writer(writer_kind, writer, ffmpeg_proc)
                    raise
                try:
                    returncode = ffmpeg_proc.process.wait(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        _terminate_process(ffmpeg_proc.process)
                        returncode = -1
                        timed_out = True
                        break
            stderr_file.seek(0)
            err = stderr_file.read()
        finally:
            stderr_file.close()
        if timed_out:
            raise RuntimeError("ffmpeg 编码超时")
        if returncode != 0:
            raise RuntimeError(f"ffmpeg 编码失败: {err.decode('utf-8', errors='ignore')}")
