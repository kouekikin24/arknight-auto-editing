#!/usr/bin/env python3
"""Business-correctness harness (phase B1) for arknight-auto-editing.

B1 scope (authoritative for candidate purity):
  per-frame scores → classify labels → diffs → pause core (start/end/local_del_mask)
  → speed segments

Explicitly NOT validated as candidate-pure in B1:
  boundary_diff / mode (build_segments re-reads source via OpenCV)
  build_delete_set / final delete ranges

Reuses production:
  analyzer.load_templates, analyzer._get_best_score, analyzer._classify_gray,
  analyzer.build_segments  (private helpers used diagnostically — recorded in metadata)

Reuses bench_decode frame sources (no duplicated FFmpeg graphs):
  iter_opencv_gray_frames, iter_ffmpeg_gray_frames, build_ffmpeg_cmd,
  resolve_ffmpeg, probe_ffmpeg, probe_cuda_runtime, _cuda_gate, open_video_meta

Does NOT import main / preview_player / settings_panel (no Tk / export).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import zlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# Path bootstrap: repo root on sys.path for analyzer / frame_types;
# load scripts/bench_decode.py as a free module (scripts is not a package).
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import analyzer  # noqa: E402
import frame_types  # noqa: E402


def _load_bench_decode():
    import importlib.util

    path = _SCRIPTS_DIR / "bench_decode.py"
    spec = importlib.util.spec_from_file_location("bench_decode", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load bench_decode from {path}")
    mod = importlib.util.module_from_spec(spec)
    # Required for @dataclass under importlib
    sys.modules["bench_decode"] = mod
    spec.loader.exec_module(mod)
    return mod


bd = _load_bench_decode()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CASE_ALIASES: dict[str, str] = {
    "p0": "opencv_gray",
    # Legacy FFmpeg default output sync (known +1 phase vs P0 on current sample).
    "a": "ffmpeg_sw_gray",
    "f": "ffmpeg_cuda_cpu_area_gray",
    "e2": "ffmpeg_cuda_e_interp_2",
    # Explicit -fps_mode passthrough (aligned-timeline candidates; pending full validation).
    "a_pt": "ffmpeg_sw_gray_passthrough",
    "f_pt": "ffmpeg_cuda_cpu_area_gray_passthrough",
    "e2_pt": "ffmpeg_cuda_e_interp_2_passthrough",
}
CASE_ORDER_DEFAULT = ("p0", "a_pt", "f_pt", "e2_pt")
LEGACY_UNALIGNED_ALIASES = frozenset({"a", "f", "e2"})
PASSTHROUGH_ALIASES = frozenset({"a_pt", "f_pt", "e2_pt"})
LABEL_NAMES: dict[int, str] = {
    frame_types.FRAME_TYPE_NORMAL: "normal",
    frame_types.FRAME_TYPE_PAUSE: "pause",
    frame_types.FRAME_TYPE_1X: "speed_1x",
    frame_types.FRAME_TYPE_2X: "speed_2x",
    frame_types.FRAME_TYPE_0_2X: "speed_0_2x",
}
SCORE_KEYS = ("pause", "speed_1x", "speed_2x", "speed_0_2x")
CONFIG_KEYS_FOR_SCORE = {
    "pause": "pause",
    "speed_1x": "speed_1x",
    "speed_2x": "speed_2x",
    "speed_0_2x": "speed_0_2x",
}


# ---------------------------------------------------------------------------
# Small helpers (JSON-safe aggregations for unit tests + reporting)
# ---------------------------------------------------------------------------


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_csv_preserve(raw: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for part in raw.split(","):
        name = part.strip().lower()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _p95_abs(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(abs(float(v)) for v in values)
    n = len(ordered)
    idx = int(math.ceil(0.95 * n)) - 1
    idx = max(0, min(n - 1, idx))
    return float(ordered[idx])


def score_diff_summary(ref: np.ndarray, cand: np.ndarray) -> dict[str, Any]:
    """Per-class absolute score difference stats (ref/cand length-N float arrays)."""
    if ref.shape != cand.shape:
        raise ValueError(f"score shape mismatch {ref.shape} vs {cand.shape}")
    d = np.abs(ref.astype(np.float64) - cand.astype(np.float64))
    flat = d.ravel()
    n = int(flat.size)
    if n == 0:
        return {
            "mean_abs": None,
            "median_abs": None,
            "p95_abs": None,
            "max_abs": None,
            "argmax_frame": None,
            "pearson_r": None,
            "n": 0,
        }
    max_i = int(np.argmax(flat))
    # pearson
    r = ref.astype(np.float64).ravel()
    c = cand.astype(np.float64).ravel()
    r_m = float(r.mean())
    c_m = float(c.mean())
    vr = float(((r - r_m) ** 2).sum())
    vc = float(((c - c_m) ** 2).sum())
    if vr > 0.0 and vc > 0.0:
        pearson = float(((r - r_m) * (c - c_m)).sum() / math.sqrt(vr * vc))
        if not math.isfinite(pearson):
            pearson = None
    else:
        pearson = None
    return {
        "mean_abs": float(flat.mean()),
        "median_abs": float(np.median(flat)),
        "p95_abs": float(_p95_abs(flat.tolist()) or 0.0),
        "max_abs": float(flat.max()),
        "argmax_frame": max_i,
        "pearson_r": pearson,
        "n": n,
    }


def label_confusion(ref: np.ndarray, cand: np.ndarray) -> dict[str, Any]:
    if ref.shape != cand.shape:
        raise ValueError("label length mismatch")
    n = int(ref.size)
    mismatch = ref != cand
    count = int(mismatch.sum())
    idxs = [int(i) for i in np.flatnonzero(mismatch)]
    conf: dict[str, int] = {}
    for a, b in zip(ref.tolist(), cand.tolist()):
        key = f"{int(a)}->{int(b)}"
        conf[key] = conf.get(key, 0) + 1
    return {
        "mismatch_count": count,
        "mismatch_ratio": float(count) / float(n) if n else 0.0,
        "mismatch_frames": idxs,
        "confusion_counts": conf,
        "ref_counts": {str(k): int(v) for k, v in Counter(ref.tolist()).items()},
        "cand_counts": {str(k): int(v) for k, v in Counter(cand.tolist()).items()},
        "n": n,
    }


def threshold_crossings(
    ref_scores: np.ndarray, cand_scores: np.ndarray, thr: float
) -> dict[str, Any]:
    """Frames where ref and cand sit on opposite sides of threshold."""
    r_hit = ref_scores >= thr
    c_hit = cand_scores >= thr
    cross = r_hit != c_hit
    idxs = [int(i) for i in np.flatnonzero(cross)]
    return {
        "crossing_count": int(cross.sum()),
        "crossing_frames": idxs,
        "threshold": float(thr),
    }


def diff_series_summary(ref: np.ndarray, cand: np.ndarray, motion_thr: float) -> dict[str, Any]:
    d = np.abs(ref.astype(np.float64) - cand.astype(np.float64))
    n = int(d.size)
    if n == 0:
        return {
            "mae": None,
            "median_abs": None,
            "p95_abs": None,
            "max_abs": None,
            "argmax_frame": None,
            "motion_thr_crossing_count": 0,
            "motion_thr_crossing_frames": [],
        }
    r_m = ref >= motion_thr
    c_m = cand >= motion_thr
    cross = r_m != c_m
    return {
        "mae": float(d.mean()),
        "median_abs": float(np.median(d)),
        "p95_abs": float(_p95_abs(d.tolist()) or 0.0),
        "max_abs": float(d.max()),
        "argmax_frame": int(np.argmax(d)),
        "motion_thr_crossing_count": int(cross.sum()),
        "motion_thr_crossing_frames": [int(i) for i in np.flatnonzero(cross)],
    }


def local_mask_hamming(a: np.ndarray, b: np.ndarray) -> dict[str, Any]:
    if a.shape != b.shape:
        return {
            "length_match": False,
            "hamming": None,
            "symmetric_diff_frames": None,
            "a_crc32": None,
            "b_crc32": None,
            "a_true_count": int(np.count_nonzero(a)),
            "b_true_count": int(np.count_nonzero(b)),
        }
    aa = a.astype(np.uint8).ravel()
    bb = b.astype(np.uint8).ravel()
    diff = aa != bb
    return {
        "length_match": True,
        "hamming": int(diff.sum()),
        "symmetric_diff_frames": int(diff.sum()),
        "a_crc32": f"{zlib.crc32(np.ascontiguousarray(aa).data) & 0xFFFFFFFF:08x}",
        "b_crc32": f"{zlib.crc32(np.ascontiguousarray(bb).data) & 0xFFFFFFFF:08x}",
        "a_true_count": int(np.count_nonzero(aa)),
        "b_true_count": int(np.count_nonzero(bb)),
    }


def mask_crc32(mask: np.ndarray) -> str:
    m = np.ascontiguousarray(mask.astype(np.uint8))
    return f"{zlib.crc32(m.data) & 0xFFFFFFFF:08x}"


# ---------------------------------------------------------------------------
# Frame source adapters
# ---------------------------------------------------------------------------


@dataclass
class FrameSourceResult:
    case_alias: str
    backend_case: str
    frames: int
    command: list[str] | None
    path_meta: dict[str, Any]
    process_returncode: int | None
    bytes_received: int | None
    expected_bytes: int | None
    partial_frame_bytes: int
    stderr_tail: str
    cleanup_ok: bool
    gray_crc32: str
    per_frame_crc32: list[str]
    states: np.ndarray
    diffs: np.ndarray
    scores: dict[str, np.ndarray]
    margins: dict[str, np.ndarray]
    top1_scores: np.ndarray
    top2_scores: np.ndarray
    top1_top2_gap: np.ndarray
    pause_hit: np.ndarray
    labels_names: list[str]
    pause_core: list[dict[str, Any]]
    speed_segments: list[dict[str, Any]]
    boundary_diagnostic: list[dict[str, Any]]


def _iter_case_frames(
    alias: str,
    video: str,
    frames: int,
    proc_res: tuple[int, int],
    ffmpeg_path: str | None,
    ffinfo: Any | None,
) -> tuple[Iterator[np.ndarray], list[str] | None, dict[str, Any], str]:
    """Return (iterator, command_or_None, path_meta, backend_case_name)."""
    backend = CASE_ALIASES[alias]
    pw, ph = proc_res
    if backend == "opencv_gray":
        it = bd.iter_opencv_gray_frames(video, frames, pw, ph)
        meta = {
            "decode_backend": "opencv",
            "final_width": pw,
            "final_height": ph,
            "final_pix_fmt": "gray",
            "pipeline": "BGR_read→INTER_AREA→BGR2GRAY",
            "output_fps_mode": None,
            "frame_sync_policy": "opencv_native",
            "timeline_family": "opencv_reference",
            "alignment_status": "reference",
        }
        return it, None, meta, backend

    if ffmpeg_path is None:
        raise RuntimeError(f"case {alias} requires --ffmpeg")

    # CUDA gate for CUDA backends (gate on base name for *_passthrough)
    gate_case = getattr(bd, "_PASSTHROUGH_CASE_BASE", {}).get(backend, backend)
    if gate_case in getattr(bd, "_CUDA_GRAY_CASES", frozenset()) or str(gate_case).startswith(
        "ffmpeg_cuda_"
    ):
        info = ffinfo
        if info is None:
            info = bd.probe_ffmpeg(ffmpeg_path)
            info = bd.probe_cuda_runtime(ffmpeg_path, video, info)
        need_extract = gate_case not in (
            "ffmpeg_cuda_cpu_area_gray",
            "ffmpeg_cuda_yuv444_convert_gray",
        ) and not str(gate_case).startswith("ffmpeg_cuda_e_interp_")
        if str(gate_case).startswith("ffmpeg_cuda_e_interp_") or gate_case in (
            "ffmpeg_cuda_cpu_area_gray",
            "ffmpeg_cuda_yuv444_convert_gray",
        ):
            need_extract = False
        gate = bd._cuda_gate(gate_case, info, frames, need_extractplanes=need_extract)
        if gate is not None:
            raise RuntimeError(
                f"CUDA gate failed for {backend}: {gate.status} {gate.skip_or_error_reason}"
            )

    cmd, bpf, wh, layout, path_meta = bd.build_ffmpeg_cmd(
        backend, ffmpeg_path, video, frames, proc_res
    )
    del bpf, wh, layout
    # Total pipe wall budget must scale with frame count. The decode-module default
    # (180s) is only enough for short smoke clips; 50k+ frames need many minutes.
    base_to = float(getattr(bd, "_DEFAULT_PIPE_TIMEOUT_S", 180.0))
    timeout_s = max(base_to, 120.0 + float(frames) * 0.12)
    it = bd.iter_ffmpeg_gray_frames(
        ffmpeg_path,
        video,
        frames,
        pw,
        ph,
        timeout_s=timeout_s,
        command=cmd,
    )
    return it, list(cmd), dict(path_meta), backend


def _scores_for_frame(
    gray: np.ndarray, configs: dict, proc_res: tuple[int, int]
) -> dict[str, float]:
    out: dict[str, float] = {}
    for sk, ck in CONFIG_KEYS_FOR_SCORE.items():
        tlist = configs.get(ck) or []
        if tlist:
            out[sk] = float(analyzer._get_best_score(gray, tlist, proc_res))
        else:
            out[sk] = -1.0
    return out


def run_candidate(
    alias: str,
    video: str,
    frames: int,
    proc_res: tuple[int, int],
    thresholds: dict[str, float],
    compare_cfg: dict[str, float],
    configs: dict,
    fps: float,
    ffmpeg_path: str | None,
    ffinfo: Any | None,
) -> FrameSourceResult:
    pw, ph = proc_res
    it, command, path_meta, backend = _iter_case_frames(
        alias, video, frames, proc_res, ffmpeg_path, ffinfo
    )

    states = np.zeros(frames, dtype=np.int8)
    diffs = np.zeros(frames, dtype=np.float32)
    scores = {k: np.zeros(frames, dtype=np.float64) for k in SCORE_KEYS}
    margins = {k: np.zeros(frames, dtype=np.float64) for k in SCORE_KEYS}
    top1 = np.zeros(frames, dtype=np.float64)
    top2 = np.zeros(frames, dtype=np.float64)
    gaps = np.zeros(frames, dtype=np.float64)
    pause_hit = np.zeros(frames, dtype=np.bool_)
    label_names: list[str] = []
    per_crc: list[str] = []
    rolling = 0
    prev: np.ndarray | None = None
    got = 0
    process_returncode: int | None = 0 if backend != "opencv_gray" else None
    bytes_received: int | None = 0 if backend != "opencv_gray" else None
    expected_bytes: int | None = (
        frames * pw * ph if backend != "opencv_gray" else None
    )
    partial = 0
    stderr_tail = ""
    cleanup_ok = False

    try:
        for gray in it:
            if got >= frames:
                raise RuntimeError(f"{alias}: extra frame beyond frames={frames}")
            if gray.dtype != np.uint8:
                raise RuntimeError(f"{alias}: dtype {gray.dtype} at frame {got}")
            if gray.shape != (ph, pw):
                raise RuntimeError(
                    f"{alias}: shape {gray.shape} != {(ph, pw)} at frame {got}"
                )
            if not gray.flags["C_CONTIGUOUS"]:
                gray = np.ascontiguousarray(gray)

            crc = zlib.crc32(gray.data) & 0xFFFFFFFF
            per_crc.append(f"{crc:08x}")
            rolling = zlib.crc32(gray.data, rolling)

            sc = _scores_for_frame(gray, configs, proc_res)
            thr_map = {
                "pause": thresholds["pause"],
                "speed_1x": thresholds["speed_1x"],
                "speed_2x": thresholds["speed_2x"],
                "speed_0_2x": thresholds["speed_0_2x"],
            }
            for k in SCORE_KEYS:
                scores[k][got] = sc[k]
                margins[k][got] = float(sc[k] - thr_map[k])

            ordered = sorted(((sc[k], k) for k in SCORE_KEYS), reverse=True)
            top1[got] = float(ordered[0][0])
            top2[got] = float(ordered[1][0]) if len(ordered) > 1 else float("nan")
            gaps[got] = float(top1[got] - top2[got]) if len(ordered) > 1 else float("nan")

            label = int(
                analyzer._classify_gray(gray, configs, thresholds, proc_res)
            )
            states[got] = label
            label_names.append(LABEL_NAMES.get(label, str(label)))
            pause_hit[got] = bool(sc["pause"] >= thr_map["pause"])

            if prev is None:
                diffs[got] = 0.0
            else:
                diffs[got] = float(cv2.mean(cv2.absdiff(gray, prev))[0])
            prev = gray
            got += 1
            if backend != "opencv_gray":
                bytes_received = (bytes_received or 0) + int(gray.nbytes)

        # Exhaust FFmpeg generator post-checks when applicable
        if backend != "opencv_gray":
            try:
                next(it)
                raise RuntimeError(f"{alias}: frame source produced extra frame")
            except StopIteration:
                process_returncode = 0
            except bd.FFmpegGrayFrameError as exc:
                process_returncode = exc.returncode
                partial = exc.bytes_in_partial_frame
                stderr_tail = exc.stderr_tail
                raise RuntimeError(f"{alias}: finalize failed: {exc}") from exc
        else:
            try:
                next(it)
                raise RuntimeError(f"{alias}: opencv source produced extra frame")
            except StopIteration:
                pass
    finally:
        try:
            it.close()
            cleanup_ok = True
        except Exception:
            cleanup_ok = False

    if got != frames:
        raise RuntimeError(f"{alias}: frames_got={got} != expected={frames}")

    # build_segments: production; boundary fields marked non-authoritative
    pauses, speeds = analyzer.build_segments(
        states, diffs, video, proc_res, compare_cfg, fps, progress_cb=None
    )

    pause_core: list[dict[str, Any]] = []
    boundary_diag: list[dict[str, Any]] = []
    for p in pauses:
        mask = np.asarray(p.get("local_del_mask", np.zeros(0, dtype=np.uint8)))
        pause_core.append(
            {
                "id": int(p.get("id", -1)),
                "start": int(p["start"]),
                "end": int(p["end"]),
                "local_del_mask_len": int(mask.size),
                "local_del_mask_true_count": int(np.count_nonzero(mask)),
                "local_del_mask_crc32": mask_crc32(mask) if mask.size else None,
                # raw mask bytes as list for JSON equality (length = seg_len, small)
                "local_del_mask": [int(x) for x in mask.tolist()],
            }
        )
        boundary_diag.append(
            {
                "id": int(p.get("id", -1)),
                "start": int(p["start"]),
                "end": int(p["end"]),
                "boundary_diff": float(p.get("boundary_diff", 0.0)),
                "mode": str(p.get("mode", "")),
                "boundary_source": "p0_opencv_reread",
                "candidate_pure": False,
                "authoritative_for_candidate": False,
            }
        )

    speed_segments = [
        {
            "type": int(s["type"]),
            "type_name": LABEL_NAMES.get(int(s["type"]), str(s["type"])),
            "start": int(s["start"]),
            "end": int(s["end"]),
        }
        for s in speeds
    ]

    return FrameSourceResult(
        case_alias=alias,
        backend_case=backend,
        frames=frames,
        command=command,
        path_meta=path_meta,
        process_returncode=process_returncode,
        bytes_received=bytes_received,
        expected_bytes=expected_bytes,
        partial_frame_bytes=partial,
        stderr_tail=stderr_tail,
        cleanup_ok=cleanup_ok,
        gray_crc32=f"{rolling & 0xFFFFFFFF:08x}",
        per_frame_crc32=per_crc,
        states=states,
        diffs=diffs,
        scores=scores,
        margins=margins,
        top1_scores=top1,
        top2_scores=top2,
        top1_top2_gap=gaps,
        pause_hit=pause_hit,
        labels_names=label_names,
        pause_core=pause_core,
        speed_segments=speed_segments,
        boundary_diagnostic=boundary_diag,
    )


# ---------------------------------------------------------------------------
# Comparisons
# ---------------------------------------------------------------------------


def compare_to_p0(
    p0: FrameSourceResult, cand: FrameSourceResult, thresholds: dict[str, float], motion_thr: float
) -> dict[str, Any]:
    score_cmp = {
        k: score_diff_summary(p0.scores[k], cand.scores[k]) for k in SCORE_KEYS
    }
    lab = label_confusion(p0.states, cand.states)
    crossings = {
        k: threshold_crossings(p0.scores[k], cand.scores[k], float(thresholds[k]))
        for k in SCORE_KEYS
    }
    dsum = diff_series_summary(p0.diffs, cand.diffs, motion_thr)

    # pause core
    p0_bounds = [(p["start"], p["end"]) for p in p0.pause_core]
    c_bounds = [(p["start"], p["end"]) for p in cand.pause_core]
    pause_bounds_equal = p0_bounds == c_bounds
    mask_cmps = []
    if pause_bounds_equal and len(p0.pause_core) == len(cand.pause_core):
        for a, b in zip(p0.pause_core, cand.pause_core):
            ma = np.asarray(a["local_del_mask"], dtype=np.uint8)
            mb = np.asarray(b["local_del_mask"], dtype=np.uint8)
            mask_cmps.append(local_mask_hamming(ma, mb))
    speed_equal = p0.speed_segments == cand.speed_segments

    # mismatch frame diagnostics (cap detail list)
    detail = []
    for fi in lab["mismatch_frames"][:50]:
        detail.append(
            {
                "frame_index": fi,
                "p0_label": int(p0.states[fi]),
                "p0_label_name": p0.labels_names[fi],
                "cand_label": int(cand.states[fi]),
                "cand_label_name": cand.labels_names[fi],
                "p0_scores": {k: float(p0.scores[k][fi]) for k in SCORE_KEYS},
                "cand_scores": {k: float(cand.scores[k][fi]) for k in SCORE_KEYS},
                "p0_margins": {k: float(p0.margins[k][fi]) for k in SCORE_KEYS},
                "cand_margins": {k: float(cand.margins[k][fi]) for k in SCORE_KEYS},
                "p0_top1_top2_gap": float(p0.top1_top2_gap[fi]),
                "cand_top1_top2_gap": float(cand.top1_top2_gap[fi]),
                "p0_crc32": p0.per_frame_crc32[fi],
                "cand_crc32": cand.per_frame_crc32[fi],
                "p0_diff_to_prev": float(p0.diffs[fi]),
                "cand_diff_to_prev": float(cand.diffs[fi]),
            }
        )

    return {
        "candidate": cand.case_alias,
        "score_diff_by_class": score_cmp,
        "labels": lab,
        "threshold_crossings": crossings,
        "diffs": dsum,
        "pause_core": {
            "bounds_equal": pause_bounds_equal,
            "p0_count": len(p0.pause_core),
            "cand_count": len(cand.pause_core),
            "p0_bounds": p0_bounds,
            "cand_bounds": c_bounds,
            "local_mask_comparisons": mask_cmps,
            "all_masks_identical": bool(
                pause_bounds_equal
                and mask_cmps
                and all(m.get("hamming") == 0 for m in mask_cmps)
            )
            if mask_cmps
            else pause_bounds_equal and len(p0.pause_core) == 0,
        },
        "speed_segments_equal": speed_equal,
        "mismatch_frame_details": detail,
        "note": (
            "boundary_diff/mode from build_segments are NOT part of candidate-core "
            "comparison (OpenCV reread)."
        ),
    }


# ---------------------------------------------------------------------------
# B1 acceptance policy (execution vs business acceptance)
# ---------------------------------------------------------------------------

B1_ACCEPTANCE_POLICY_VERSION = "b1_acceptance_v2_pause_consumed_motion"


def _as_dict_result(obj: Any) -> dict[str, Any]:
    """Normalize FrameSourceResult or result_to_jsonable dict."""
    if isinstance(obj, FrameSourceResult):
        return result_to_jsonable(obj)
    if isinstance(obj, dict):
        return obj
    raise TypeError(f"unsupported result type: {type(obj)}")


def _pause_consumed_diff_indices(start: int, end: int) -> list[int]:
    """Indices actually read by analyzer._analyze_pause_mask for segment [start, end].

    Production loop: for k in range(1, seg_len): idx = s_i + k
    => consumed = s_i+1 ... e_i inclusive. diffs[s_i] is never tested.
    """
    if end < start:
        return []
    return list(range(int(start) + 1, int(end) + 1))


def _motion_crossings(diffs: list[float] | np.ndarray, thr: float) -> list[int]:
    d = np.asarray(diffs, dtype=float)
    return sorted(int(i) for i in np.flatnonzero(d > float(thr)))


def _crossing_set_in_indices(diffs: list[float] | np.ndarray, thr: float, indices: list[int]) -> list[int]:
    d = np.asarray(diffs, dtype=float)
    out = []
    for i in indices:
        if 0 <= i < len(d) and float(d[i]) > float(thr):
            out.append(int(i))
    return sorted(out)


def _unique_sorted(xs: list[int]) -> list[int]:
    return sorted(set(int(x) for x in xs))


def evaluate_b1_acceptance(
    *,
    candidate_results: dict[str, Any],
    comparisons_to_p0: dict[str, Any],
    identity_invariants: dict[str, Any] | None = None,
    motion_thr: float = 2.0,
    frames_expected: int | None = None,
    execution_ok: bool = True,
    cleanup_ok: bool = True,
    serialization_ok: bool = True,
    candidates_completed: list[str] | None = None,
) -> dict[str, Any]:
    """Pure B1 acceptance evaluation (no IO, no video, no reclassification).

    Separates harness execution success from business acceptance and sample coverage.
    """
    identity_invariants = identity_invariants or {}
    results = {k: _as_dict_result(v) for k, v in candidate_results.items()}
    if "p0" not in results:
        return {
            "policy_version": B1_ACCEPTANCE_POLICY_VERSION,
            "execution_status": {
                "result": "execution_failed",
                "candidates_completed": list(results.keys()),
                "cleanup_ok": bool(cleanup_ok),
                "serialization_ok": bool(serialization_ok),
            },
            "acceptance": {
                "policy_version": B1_ACCEPTANCE_POLICY_VERSION,
                "result": "execution_failed",
                "hard_failures": [
                    {
                        "code": "missing_p0",
                        "message": "p0 reference missing from candidate_results",
                        "frames": [],
                    }
                ],
                "hard_checks": {},
                "warning_checks": {},
            },
            "sample_coverage": {
                "pause_core_activated": False,
                "pause_segment_count": 0,
                "label_types_observed": [],
                "label_transition_count": 0,
                "boundary_activated": False,
                "delete_set_evaluated": False,
                "production_coverage_complete": False,
                "coverage_status": ["coverage_incomplete_pause_not_activated"],
            },
            "legacy_gate": {
                "global_motion_crossing_equal": None,
                "result_under_previous_policy": "not_evaluated",
                "symmetric_difference_frames": [],
            },
            "warnings": [],
        }

    p0 = results["p0"]
    cand_aliases = [k for k in results.keys() if k != "p0"]
    if candidates_completed is None:
        candidates_completed = list(results.keys())

    hard_failures: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    def hard(code: str, message: str, frames: list[int] | None = None, **extra: Any) -> None:
        hard_failures.append(
            {
                "code": code,
                "message": message,
                "frames": _unique_sorted(frames or []),
                **extra,
            }
        )

    def warn(code: str, message: str, frames: list[int] | None = None, **extra: Any) -> None:
        warnings.append(
            {
                "code": code,
                "message": message,
                "frames": _unique_sorted(frames or []),
                **extra,
            }
        )

    # --- execution ---
    exec_result = "ok"
    if not execution_ok or not cleanup_ok or not serialization_ok:
        exec_result = "execution_failed"
        hard(
            "execution_incomplete",
            f"execution_ok={execution_ok} cleanup_ok={cleanup_ok} serialization_ok={serialization_ok}",
        )

    n0 = int(p0.get("frames_processed") or len(p0.get("states") or []))
    if frames_expected is not None and n0 != int(frames_expected):
        hard(
            "frame_count_mismatch_p0",
            f"p0 frames_processed={n0} expected={frames_expected}",
        )
        exec_result = "execution_failed"

    for alias in cand_aliases:
        r = results[alias]
        n = int(r.get("frames_processed") or len(r.get("states") or []))
        if n != n0:
            hard(
                "frame_count_mismatch",
                f"{alias} frames={n} != p0 frames={n0}",
                candidate=alias,
            )
            exec_result = "execution_failed"
        if r.get("cleanup_ok") is False:
            hard("cleanup_failed", f"{alias} cleanup_ok=false", candidate=alias)
            exec_result = "execution_failed"
        if r.get("partial_frame_bytes"):
            hard(
                "partial_frame_bytes",
                f"{alias} partial_frame_bytes={r.get('partial_frame_bytes')}",
                candidate=alias,
            )
            exec_result = "execution_failed"

    # --- harness identity invariants ---
    invariant_failed = False
    for key, inv in identity_invariants.items():
        if not isinstance(inv, dict):
            continue
        st = inv.get("status")
        if st == "harness_invariant_failed" or inv.get("ok") is False:
            invariant_failed = True
            hard(
                "harness_identity_invariant",
                f"{key}: {inv.get('failures') or st}",
                pair=inv.get("pair") or key,
            )

    # --- sample coverage from p0 ---
    p0_states = [int(x) for x in p0.get("states") or []]
    label_types = sorted(set(p0_states))
    transitions = sum(
        1 for i in range(1, len(p0_states)) if p0_states[i] != p0_states[i - 1]
    )
    p0_pauses = list(p0.get("pause_core") or [])
    pause_activated = len(p0_pauses) > 0
    boundary_activated = any(
        bool(x.get("boundary_diff") not in (None, 0, 0.0))
        or str(x.get("mode") or "") not in ("", "auto")
        for r in results.values()
        for x in (r.get("boundary_diagnostic") or [])
    )
    # boundary_diagnostic always lists modes; for empty pauses it's inactive
    if not pause_activated:
        boundary_activated = False

    coverage_status = []
    if not pause_activated:
        coverage_status.append("coverage_incomplete_pause_not_activated")
    if transitions == 0:
        coverage_status.append("coverage_incomplete_no_label_transitions")
    coverage_status.append("coverage_incomplete_b2_not_evaluated")
    production_complete = (
        pause_activated and transitions > 0 and False
    )  # B2 never evaluated here
    if production_complete:
        coverage_status = ["coverage_complete"]

    sample_coverage = {
        "pause_core_activated": pause_activated,
        "pause_segment_count": len(p0_pauses),
        "label_types_observed": label_types,
        "label_transition_count": int(transitions),
        "boundary_activated": boundary_activated,
        "delete_set_evaluated": False,
        "production_coverage_complete": False,
        "coverage_status": coverage_status,
    }

    # --- hard business checks vs p0 ---
    hard_checks: dict[str, Any] = {
        "labels_equal": {},
        "pause_bounds_equal": {},
        "pause_local_masks_equal": {},
        "speed_segments_equal": {},
        "pause_consumed_motion_crossings_equal": {},
        "pause_presence_agreement": {},
    }

    # Aggregate legacy global motion symmetric difference across cand vs p0
    legacy_sym: set[int] = set()

    for alias in cand_aliases:
        r = results[alias]
        cmpd = comparisons_to_p0.get(alias) or {}
        # labels
        lab = cmpd.get("labels") or {}
        mm = int(lab.get("mismatch_count") or 0)
        hard_checks["labels_equal"][alias] = mm == 0
        if mm:
            hard(
                "label_sequence_mismatch",
                f"{alias}: label mismatch_count={mm}",
                frames=list(lab.get("mismatch_frames") or []),
                candidate=alias,
            )

        # pause bounds
        pc = cmpd.get("pause_core") or {}
        bounds_eq = bool(pc.get("bounds_equal"))
        hard_checks["pause_bounds_equal"][alias] = bounds_eq
        if not bounds_eq:
            hard(
                "pause_bounds_mismatch",
                f"{alias}: pause start/end mismatch p0={pc.get('p0_bounds')} cand={pc.get('cand_bounds')}",
                candidate=alias,
                p0_bounds=pc.get("p0_bounds"),
                cand_bounds=pc.get("cand_bounds"),
            )

        # local masks
        mask_ok = bool(pc.get("all_masks_identical"))
        # if counts differ already failed bounds; still check hamming
        for i, mc in enumerate(pc.get("local_mask_comparisons") or []):
            if mc.get("length_match") is False or int(mc.get("hamming") or 0) != 0:
                mask_ok = False
                hard(
                    "pause_local_mask_mismatch",
                    f"{alias}: local_del_mask mismatch at pause index {i}",
                    candidate=alias,
                    pause_index=i,
                    hamming=mc.get("hamming"),
                )
        hard_checks["pause_local_masks_equal"][alias] = mask_ok and bounds_eq

        # speed segments
        sp_eq = bool(cmpd.get("speed_segments_equal"))
        if not sp_eq:
            # also direct compare
            sp_eq = list(p0.get("speed_segments") or []) == list(
                r.get("speed_segments") or []
            )
        hard_checks["speed_segments_equal"][alias] = sp_eq
        if not sp_eq:
            hard(
                "speed_segments_mismatch",
                f"{alias}: speed segments differ",
                candidate=alias,
                p0_speeds=p0.get("speed_segments"),
                cand_speeds=r.get("speed_segments"),
            )

        # pause presence agreement (one has pause segments, other doesn't)
        p0_n = len(p0.get("pause_core") or [])
        c_n = len(r.get("pause_core") or [])
        presence_ok = (p0_n == 0 and c_n == 0) or (p0_n > 0 and c_n > 0 and bounds_eq)
        # stronger: if p0_n != c_n
        if p0_n != c_n:
            presence_ok = False
            hard(
                "pause_presence_disagreement",
                f"{alias}: pause segment count p0={p0_n} cand={c_n}",
                candidate=alias,
            )
        hard_checks["pause_presence_agreement"][alias] = presence_ok

        # PAUSE-consumed motion crossings (production-accurate indices)
        p0_diffs = p0.get("diffs") or []
        c_diffs = r.get("diffs") or []
        p0_pause_cross: set[int] = set()
        c_pause_cross: set[int] = set()
        # Use p0 pause geometry as the production segment map for "same positions"
        # If bounds differ, already hard-failed; still compute on each side's segments.
        for seg in p0.get("pause_core") or []:
            idxs = _pause_consumed_diff_indices(int(seg["start"]), int(seg["end"]))
            p0_pause_cross.update(
                _crossing_set_in_indices(p0_diffs, motion_thr, idxs)
            )
        for seg in r.get("pause_core") or []:
            idxs = _pause_consumed_diff_indices(int(seg["start"]), int(seg["end"]))
            c_pause_cross.update(_crossing_set_in_indices(c_diffs, motion_thr, idxs))
        # When bounds equal, compare sets; when both empty, equal
        pause_cross_eq = sorted(p0_pause_cross) == sorted(c_pause_cross)
        hard_checks["pause_consumed_motion_crossings_equal"][alias] = pause_cross_eq
        if not pause_cross_eq:
            sym = sorted(p0_pause_cross.symmetric_difference(c_pause_cross))
            hard(
                "pause_consumed_motion_crossing_mismatch",
                f"{alias}: motion crossings differ inside PAUSE consumed indices s_i+1..e_i",
                frames=sym,
                candidate=alias,
                p0_crossings=sorted(p0_pause_cross),
                cand_crossings=sorted(c_pause_cross),
                consumed_index_rule="s_i+1..e_i inclusive; diffs[s_i] excluded",
            )

        # --- legacy global motion gate (diagnostic only) ---
        p0_g = set(_motion_crossings(p0_diffs, motion_thr))
        c_g = set(_motion_crossings(c_diffs, motion_thr))
        sym_g = sorted(p0_g.symmetric_difference(c_g))
        legacy_sym.update(sym_g)

        # --- warnings ---
        # 1) non-PAUSE global motion crossings
        # frames that are in sym_g and not in any pause consumed range on p0
        all_pause_consumed: set[int] = set()
        for seg in p0.get("pause_core") or []:
            all_pause_consumed.update(
                _pause_consumed_diff_indices(int(seg["start"]), int(seg["end"]))
            )
        non_pause_sym = [i for i in sym_g if i not in all_pause_consumed]
        if non_pause_sym:
            warn(
                "global_motion_crossing",
                f"{alias}: global motion thr crossings differ outside PAUSE consumed indices",
                frames=non_pause_sym,
                candidate=alias,
                threshold=float(motion_thr),
                reference_diffs={str(i): float(p0_diffs[i]) for i in non_pause_sym if i < len(p0_diffs)},
                candidate_diffs={str(i): float(c_diffs[i]) for i in non_pause_sym if i < len(c_diffs)},
                in_pause_segment=False,
                consumed_by_analyze_pause_mask=False,
                downstream_output_difference=False,
            )

        # 2) score thr crossings without label change
        thr_x = cmpd.get("threshold_crossings") or {}
        for sk, info in thr_x.items():
            frames_x = list(info.get("crossing_frames") or [])
            if not frames_x:
                continue
            # filter those where labels still match
            lab_ok_frames = []
            for fi in frames_x:
                if fi < len(p0_states) and fi < len(r.get("states") or []):
                    if int(p0["states"][fi]) == int(r["states"][fi]):
                        lab_ok_frames.append(int(fi))
            if lab_ok_frames and mm == 0:
                warn(
                    "score_threshold_crossing_without_label_change",
                    f"{alias}: score class {sk} thr crossings without label change",
                    frames=lab_ok_frames,
                    candidate=alias,
                    score_key=sk,
                    threshold=float(info.get("threshold") or thr_x.get(sk, {}).get("threshold") or 0.7),
                    in_pause_segment=False,
                    consumed_by_analyze_pause_mask=False,
                    downstream_output_difference=False,
                )

        # 3) top2 order change without top1/label change (scan all frames lightly)
        p0_scores = p0.get("scores") or {}
        c_scores = r.get("scores") or {}
        top2_frames = []
        for i in range(min(len(p0_states), len(r.get("states") or []))):
            if int(p0["states"][i]) != int(r["states"][i]):
                continue
            def rank(scores_map: dict, idx: int) -> list[str]:
                items = sorted(
                    ((float(scores_map[k][idx]), k) for k in SCORE_KEYS if k in scores_map),
                    reverse=True,
                )
                return [k for _, k in items]
            rp = rank(p0_scores, i)
            rc = rank(c_scores, i)
            if rp and rc and rp[0] == rc[0] and len(rp) > 1 and len(rc) > 1 and rp[1] != rc[1]:
                top2_frames.append(i)
        if top2_frames:
            warn(
                "top2_order_change",
                f"{alias}: top2 class order changed while top1/label unchanged",
                frames=top2_frames[:50],
                candidate=alias,
                frame_count=len(top2_frames),
                downstream_output_difference=False,
            )

        # 4) threshold margin reduction for 0_2x (example class) — if min margin shrinks
        if "speed_0_2x" in p0_scores and "speed_0_2x" in c_scores:
            thr0 = 0.7
            # use configuration if present in margins already relative
            p0_m = np.asarray(p0_scores["speed_0_2x"], float) - thr0
            c_m = np.asarray(c_scores["speed_0_2x"], float) - thr0
            if float(c_m.min()) + 1e-12 < float(p0_m.min()):
                warn(
                    "threshold_margin_reduction",
                    f"{alias}: min speed_0_2x margin reduced vs p0",
                    candidate=alias,
                    p0_min_margin=float(p0_m.min()),
                    cand_min_margin=float(c_m.min()),
                    shrink=float(p0_m.min() - c_m.min()),
                    downstream_output_difference=False,
                )

        # 5) raw score differences without label change already implied; optional light note
        # skip flooding warnings for every score MAE

    legacy_equal = len(legacy_sym) == 0
    legacy_result = "pass" if legacy_equal else "stop"
    legacy_gate = {
        "global_motion_crossing_equal": legacy_equal,
        "result_under_previous_policy": legacy_result,
        "symmetric_difference_frames": _unique_sorted(list(legacy_sym)),
        "policy": "previous_hard_gate_required_full_timeline_motion_crossing_set_equality",
    }

    # --- acceptance result enum ---
    if invariant_failed:
        acc_result = "harness_invariant_failed"
    elif exec_result != "ok" or any(
        hf["code"]
        in (
            "execution_incomplete",
            "frame_count_mismatch",
            "frame_count_mismatch_p0",
            "cleanup_failed",
            "partial_frame_bytes",
            "missing_p0",
        )
        for hf in hard_failures
    ):
        # if only business hard fails, still hard_fail; if execution issues dominate
        if any(
            hf["code"].startswith("execution")
            or hf["code"].startswith("frame_count")
            or hf["code"] in ("cleanup_failed", "partial_frame_bytes", "missing_p0")
            for hf in hard_failures
        ):
            acc_result = "execution_failed"
        else:
            acc_result = "hard_fail"
    elif hard_failures:
        acc_result = "hard_fail"
    elif warnings:
        acc_result = "match_with_warning"
    else:
        acc_result = "match_no_warning"

    # hard_checks summary
    hard_checks_summary = {
        "all_passed": acc_result in ("match_with_warning", "match_no_warning"),
        "details": hard_checks,
    }
    warning_checks = {
        "count": len(warnings),
        "codes": sorted({w["code"] for w in warnings}),
    }

    return {
        "policy_version": B1_ACCEPTANCE_POLICY_VERSION,
        "execution_status": {
            "result": exec_result if exec_result == "ok" and not any(
                hf["code"] in (
                    "execution_incomplete",
                    "frame_count_mismatch",
                    "frame_count_mismatch_p0",
                    "cleanup_failed",
                    "partial_frame_bytes",
                    "missing_p0",
                )
                for hf in hard_failures
            )
            else ("execution_failed" if exec_result != "ok" or any(
                hf["code"] in (
                    "execution_incomplete",
                    "frame_count_mismatch",
                    "frame_count_mismatch_p0",
                    "cleanup_failed",
                    "partial_frame_bytes",
                    "missing_p0",
                )
                for hf in hard_failures
            ) else exec_result),
            "candidates_completed": list(candidates_completed),
            "cleanup_ok": bool(cleanup_ok),
            "serialization_ok": bool(serialization_ok),
        },
        "acceptance": {
            "policy_version": B1_ACCEPTANCE_POLICY_VERSION,
            "result": acc_result,
            "hard_failures": hard_failures,
            "hard_checks": hard_checks_summary,
            "warning_checks": warning_checks,
        },
        "sample_coverage": sample_coverage,
        "legacy_gate": legacy_gate,
        "warnings": warnings,
    }


def _pyify(o: Any) -> Any:
    if isinstance(o, dict):
        return {str(k): _pyify(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_pyify(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        x = float(o)
        return x if math.isfinite(x) else None
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    return o


def check_pair_invariant(
    left: FrameSourceResult,
    right: FrameSourceResult,
    *,
    left_name: str,
    right_name: str,
) -> dict[str, Any]:
    """Two candidates on the same timeline family must be business-identical."""
    failures: list[str] = []
    first_frame: int | None = None

    def fail(msg: str, fi: int | None = None) -> None:
        nonlocal first_frame
        failures.append(msg)
        if first_frame is None and fi is not None:
            first_frame = fi

    if left.frames != right.frames:
        fail(f"frames {left.frames} != {right.frames}")
    if left.gray_crc32 != right.gray_crc32:
        fail(f"gray_crc32 {left.gray_crc32} != {right.gray_crc32}")
    n = min(left.frames, right.frames)
    for i in range(n):
        if left.per_frame_crc32[i] != right.per_frame_crc32[i]:
            fail("per_frame_crc32 mismatch", i)
            break
    for k in SCORE_KEYS:
        if not np.array_equal(left.scores[k], right.scores[k]):
            d = np.where(left.scores[k] != right.scores[k])[0]
            fi = int(d[0]) if len(d) else None
            fail(f"scores[{k}] mismatch", fi)
    if not np.array_equal(left.states, right.states):
        d = np.where(left.states != right.states)[0]
        fail("states mismatch", int(d[0]) if len(d) else None)
    if not np.array_equal(left.diffs, right.diffs):
        d = np.where(left.diffs != right.diffs)[0]
        fail("diffs mismatch", int(d[0]) if len(d) else None)
    left_bounds = [(p["start"], p["end"]) for p in left.pause_core]
    right_bounds = [(p["start"], p["end"]) for p in right.pause_core]
    if left_bounds != right_bounds:
        fail(f"pause bounds {left_bounds} != {right_bounds}")
    if len(left.pause_core) == len(right.pause_core):
        for i, (pa, pb) in enumerate(zip(left.pause_core, right.pause_core)):
            if pa["local_del_mask"] != pb["local_del_mask"]:
                fail(f"local_del_mask seg {i} mismatch")
    elif left.pause_core or right.pause_core:
        fail("pause_core length mismatch")
    if left.speed_segments != right.speed_segments:
        fail("speed_segments mismatch")

    ok = not failures
    detail = None
    if not ok and first_frame is not None and 0 <= first_frame < n:
        i = first_frame
        detail = {
            "frame_index": i,
            f"{left_name}_crc": left.per_frame_crc32[i],
            f"{right_name}_crc": right.per_frame_crc32[i],
            f"{left_name}_scores": {k: float(left.scores[k][i]) for k in SCORE_KEYS},
            f"{right_name}_scores": {k: float(right.scores[k][i]) for k in SCORE_KEYS},
            f"{left_name}_label": int(left.states[i]),
            f"{right_name}_label": int(right.states[i]),
            f"{left_name}_diff": float(left.diffs[i]),
            f"{right_name}_diff": float(right.diffs[i]),
            f"{left_name}_command": left.command,
            f"{right_name}_command": right.command,
            f"{left_name}_path_meta": left.path_meta,
            f"{right_name}_path_meta": right.path_meta,
        }
    return {
        "ok": ok,
        "status": "ok" if ok else "harness_invariant_failed",
        "pair": f"{left_name}_vs_{right_name}",
        "failures": failures,
        "first_mismatch_detail": detail,
    }


def check_a_f_invariant(a: FrameSourceResult, f: FrameSourceResult) -> dict[str, Any]:
    """Legacy A/F identity (ffmpeg_default_sync timeline)."""
    return check_pair_invariant(a, f, left_name="a", right_name="f")


def result_to_jsonable(r: FrameSourceResult) -> dict[str, Any]:
    timeline = "opencv_reference"
    if r.case_alias in LEGACY_UNALIGNED_ALIASES:
        timeline = "legacy_default_sync"
    elif r.case_alias in PASSTHROUGH_ALIASES:
        timeline = "ffmpeg_passthrough"
    elif r.backend_case != "opencv_gray":
        timeline = str((r.path_meta or {}).get("timeline_family") or "unknown")
    return {
        "case": r.case_alias,
        "backend_case": r.backend_case,
        "timeline_family": timeline,
        "command": r.command,
        "path_meta": r.path_meta,
        "frames_processed": r.frames,
        "gray_crc32": r.gray_crc32,
        "per_frame_crc32": r.per_frame_crc32,
        "state_counts": {str(k): int(v) for k, v in Counter(r.states.tolist()).items()},
        "scores_summary": {
            k: {
                "mean": float(r.scores[k].mean()),
                "min": float(r.scores[k].min()),
                "max": float(r.scores[k].max()),
            }
            for k in SCORE_KEYS
        },
        "states": [int(x) for x in r.states.tolist()],
        "label_names": r.labels_names,
        "diffs": [float(x) for x in r.diffs.tolist()],
        "scores": {k: [float(x) for x in r.scores[k].tolist()] for k in SCORE_KEYS},
        "margins": {k: [float(x) for x in r.margins[k].tolist()] for k in SCORE_KEYS},
        "top1_scores": [float(x) for x in r.top1_scores.tolist()],
        "top2_scores": [float(x) for x in r.top2_scores.tolist()],
        "top1_top2_gap": [float(x) for x in r.top1_top2_gap.tolist()],
        "pause_hit": [bool(x) for x in r.pause_hit.tolist()],
        "pause_core": r.pause_core,
        "speed_segments": r.speed_segments,
        "boundary_diagnostic": r.boundary_diagnostic,
        "process_returncode": r.process_returncode,
        "bytes_received": r.bytes_received,
        "expected_bytes": r.expected_bytes,
        "partial_frame_bytes": r.partial_frame_bytes,
        "stderr_tail": r.stderr_tail,
        "cleanup_ok": r.cleanup_ok,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Business verify B1: per-frame scores/labels/diffs + pause core "
            "(no delete_set; boundary mode not candidate-pure). "
            "Default requires aligned timeline (a_pt/f_pt/e2_pt)."
        )
    )
    p.add_argument("--video", required=True, help="Input video path")
    p.add_argument("--frames", type=int, required=True, help="Frame count from index 0")
    p.add_argument(
        "--proc-res",
        nargs=2,
        type=int,
        default=[400, 225],
        metavar=("W", "H"),
        help="Locked processing resolution (default 400 225; no UI aspect rewrite)",
    )
    p.add_argument(
        "--cases",
        default=",".join(CASE_ORDER_DEFAULT),
        help=(
            "Comma list of p0,a_pt,f_pt,e2_pt (aligned; default). "
            "Legacy a,f,e2 require --allow-legacy-unaligned. P0 required first."
        ),
    )
    p.add_argument(
        "--ffmpeg",
        default="auto",
        help="FFmpeg path or 'auto' (required for non-p0 cases)",
    )
    p.add_argument("--threshold-pause", type=float, default=0.7)
    p.add_argument("--threshold-1x", type=float, default=0.7)
    p.add_argument("--threshold-2x", type=float, default=0.7)
    p.add_argument("--threshold-0-2x", type=float, default=0.7)
    p.add_argument("--still-time-thresh", type=float, default=0.1)
    p.add_argument("--motion-thresh", type=float, default=2.0)
    p.add_argument("--boundary-thresh", type=float, default=5.0)
    p.add_argument("--out-json", default=None, help="Optional JSON output path")
    p.add_argument(
        "--start",
        type=int,
        default=0,
        help="Must be 0 in B1 (non-zero start not supported)",
    )
    p.add_argument(
        "--require-aligned-timeline",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "When enabled (default), reject legacy a/f/e2 in the same run as P0 "
            "because default FFmpeg output sync has a known +1 phase shift vs OpenCV. "
            "Use a_pt/f_pt/e2_pt instead."
        ),
    )
    p.add_argument(
        "--allow-legacy-unaligned",
        action="store_true",
        default=False,
        help=(
            "Allow legacy a/f/e2 (ffmpeg_auto sync) with P0 for historical "
            "reproduction only. Implies not requiring aligned timeline for those cases."
        ),
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.frames <= 0:
        print("error: --frames must be > 0", file=sys.stderr)
        return 2
    if args.start != 0:
        print(
            "error: B1 forbids non-zero --start (decode-and-discard seek not in scope)",
            file=sys.stderr,
        )
        return 2

    proc_res = (int(args.proc_res[0]), int(args.proc_res[1]))
    if proc_res[0] <= 0 or proc_res[1] <= 0:
        print("error: invalid --proc-res", file=sys.stderr)
        return 2

    cases = _parse_csv_preserve(args.cases)
    if not cases:
        print("error: --cases empty", file=sys.stderr)
        return 2
    if cases[0] != "p0":
        print("error: P0 must be first in --cases", file=sys.stderr)
        return 2
    if "p0" not in cases:
        print("error: P0 required", file=sys.stderr)
        return 2
    unknown = [c for c in cases if c not in CASE_ALIASES]
    if unknown:
        print(
            f"error: unknown cases {unknown}; allowed {sorted(CASE_ALIASES)}",
            file=sys.stderr,
        )
        return 2

    require_aligned = bool(args.require_aligned_timeline) and not bool(
        args.allow_legacy_unaligned
    )
    legacy_requested = [c for c in cases if c in LEGACY_UNALIGNED_ALIASES]
    if require_aligned and legacy_requested and "p0" in cases:
        print(
            "error: --require-aligned-timeline rejects legacy cases "
            f"{legacy_requested} together with P0. "
            "Default FFmpeg output sync has a known one-frame phase shift vs OpenCV "
            "on the current sample. Use a_pt,f_pt,e2_pt "
            "(explicit -fps_mode passthrough), or pass --allow-legacy-unaligned "
            "only for historical reproduction.",
            file=sys.stderr,
        )
        return 2

    video = str(Path(args.video).expanduser())
    if not Path(video).is_file():
        print(f"error: video not found: {video}", file=sys.stderr)
        return 2

    thresholds = {
        "pause": float(args.threshold_pause),
        "speed_1x": float(args.threshold_1x),
        "speed_2x": float(args.threshold_2x),
        "speed_0_2x": float(args.threshold_0_2x),
    }
    compare_cfg = {
        "still_time_thresh": float(args.still_time_thresh),
        "motion_thresh": float(args.motion_thresh),
        "boundary_thresh": float(args.boundary_thresh),
    }

    need_ffmpeg = any(c != "p0" for c in cases)
    ffmpeg_path: str | None = None
    ffinfo = None
    if need_ffmpeg:
        try:
            ffmpeg_path = bd.resolve_ffmpeg(args.ffmpeg)
        except Exception as exc:
            print(f"error: resolve ffmpeg: {exc}", file=sys.stderr)
            return 2
        if any(CASE_ALIASES[c].startswith("ffmpeg_cuda_") for c in cases):
            ffinfo = bd.probe_ffmpeg(ffmpeg_path)
            ffinfo = bd.probe_cuda_runtime(ffmpeg_path, video, ffinfo)

    # Video meta / fps via OpenCV (production uses CAP_PROP_FPS in GUI; here open_video_meta)
    try:
        vmeta = bd.open_video_meta(video)
        fps = float(vmeta.get("fps") or 0.0)
    except Exception:
        cap = cv2.VideoCapture(video)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        cap.release()
        vmeta = {"path": video, "fps": fps}
    if fps <= 0:
        fps = 30.0  # last resort for still_frames; recorded in metadata

    # Templates
    configs, n_templates = analyzer.load_templates(proc_res)
    template_summary: dict[str, Any] = {"total": int(n_templates), "per_class": {}}
    for k, lst in configs.items():
        template_summary["per_class"][k] = {
            "count": len(lst),
            "rois": [t.get("cached_roi") for t in lst],
        }

    # File hashes
    analyzer_path = Path(analyzer.__file__).resolve()
    bench_path = _SCRIPTS_DIR / "bench_decode.py"
    self_path = Path(__file__).resolve()
    metadata = {
        "phase": "B1",
        "production_reference": True,
        "proc_res_locked": True,
        "ui_aspect_ratio_rewrite": False,
        "source_aspect_rewrite_disabled": True,
        "valid_when_runtime_proc_res_matches": True,
        "reused_production": [
            "analyzer.load_templates",
            "analyzer._get_best_score",
            "analyzer._classify_gray",
            "analyzer.build_segments",
        ],
        "private_api_note": (
            "_get_best_score and _classify_gray are private; used diagnostically "
            "to reuse production matching without reimplementation."
        ),
        "reused_bench": [
            "iter_opencv_gray_frames",
            "iter_ffmpeg_gray_frames",
            "build_ffmpeg_cmd",
            "resolve_ffmpeg",
            "probe_ffmpeg",
            "probe_cuda_runtime",
            "_cuda_gate",
            "open_video_meta",
        ],
        "not_called": ["analyzer.build_delete_set", "export_video", "export_ranges"],
        "boundary_policy": {
            "boundary_diff": "diagnostic_only",
            "mode": "diagnostic_only",
            "boundary_source": "p0_opencv_reread",
            "candidate_pure": False,
        },
        "video": str(Path(video).resolve()),
        "video_sha256": _file_sha256(Path(video)),
        "analyzer_sha256": _file_sha256(analyzer_path),
        "bench_decode_sha256": _file_sha256(bench_path),
        "business_verify_sha256": _file_sha256(self_path),
        "fps_used": float(fps),
        "video_meta": {k: vmeta[k] for k in vmeta if k != "path"}
        if isinstance(vmeta, dict)
        else {},
    }

    configuration = {
        "frames": int(args.frames),
        "proc_res": list(proc_res),
        "cases": cases,
        "thresholds": thresholds,
        "compare": compare_cfg,
        "ffmpeg": ffmpeg_path,
        "require_aligned_timeline": require_aligned,
        "allow_legacy_unaligned": bool(args.allow_legacy_unaligned),
    }

    print("=== bench_business_verify B1 ===")
    print(f"video: {metadata['video']}")
    print(f"frames: {args.frames} proc_res: {proc_res[0]}x{proc_res[1]} locked")
    print(f"cases: {cases}")
    print(
        f"timeline_guard: require_aligned={require_aligned} "
        f"allow_legacy_unaligned={bool(args.allow_legacy_unaligned)}"
    )
    print(f"templates_loaded: {n_templates} summary={template_summary['per_class']}")
    print(f"thresholds: {thresholds}")
    print(f"compare: {compare_cfg}")
    print("ui_aspect_ratio_rewrite=false proc_res_locked=true")
    print("build_delete_set: NOT CALLED (B1)")

    results: dict[str, FrameSourceResult] = {}
    for alias in cases:
        print(f"\n-- candidate {alias} ({CASE_ALIASES[alias]}) ...", flush=True)
        try:
            results[alias] = run_candidate(
                alias=alias,
                video=video,
                frames=int(args.frames),
                proc_res=proc_res,
                thresholds=thresholds,
                compare_cfg=compare_cfg,
                configs=configs,
                fps=fps,
                ffmpeg_path=ffmpeg_path,
                ffinfo=ffinfo,
            )
        except Exception as exc:
            print(f"error: candidate {alias} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            # emit partial
            partial_out = {
                "metadata": metadata,
                "configuration": configuration,
                "template_summary": template_summary,
                "candidate_results": {
                    k: result_to_jsonable(v) for k, v in results.items()
                },
                "status": "failed",
                "failed_candidate": alias,
                "error": f"{type(exc).__name__}: {exc}",
                "limitations": _limitations_text(),
            }
            _emit(partial_out, args.out_json)
            return 1
        r = results[alias]
        print(
            f"   ok frames={r.frames} gray_crc={r.gray_crc32} "
            f"states={dict(Counter(r.states.tolist()))} "
            f"pauses={len(r.pause_core)} speeds={len(r.speed_segments)} "
            f"cleanup_ok={r.cleanup_ok}"
        )

    # Timeline-family identity invariants (only when both members requested).
    identity_checks: dict[str, Any] = {}
    if "a" in results and "f" in results:
        identity_checks["legacy_a_f"] = check_pair_invariant(
            results["a"], results["f"], left_name="a", right_name="f"
        )
        print(f"\nlegacy A/F invariant: {identity_checks['legacy_a_f']['status']}")
        if not identity_checks["legacy_a_f"]["ok"]:
            print(
                f"error: legacy A/F harness invariant failed: "
                f"{identity_checks['legacy_a_f']['failures']}",
                file=sys.stderr,
            )
            out = {
                "metadata": metadata,
                "configuration": configuration,
                "template_summary": template_summary,
                "candidate_results": {
                    k: result_to_jsonable(v) for k, v in results.items()
                },
                "identity_invariants": identity_checks,
                "a_f_invariant": identity_checks["legacy_a_f"],
                "status": "harness_invariant_failed",
                "limitations": _limitations_text(),
            }
            _emit(out, args.out_json)
            return 1
    else:
        identity_checks["legacy_a_f"] = {
            "ok": True,
            "status": "skipped_missing_a_or_f",
            "pair": "a_vs_f",
        }

    if "a_pt" in results and "f_pt" in results:
        identity_checks["passthrough_a_pt_f_pt"] = check_pair_invariant(
            results["a_pt"], results["f_pt"], left_name="a_pt", right_name="f_pt"
        )
        print(
            f"\npassthrough A_PT/F_PT invariant: "
            f"{identity_checks['passthrough_a_pt_f_pt']['status']}"
        )
        if not identity_checks["passthrough_a_pt_f_pt"]["ok"]:
            print(
                f"error: passthrough A_PT/F_PT harness invariant failed: "
                f"{identity_checks['passthrough_a_pt_f_pt']['failures']}",
                file=sys.stderr,
            )
            out = {
                "metadata": metadata,
                "configuration": configuration,
                "template_summary": template_summary,
                "candidate_results": {
                    k: result_to_jsonable(v) for k, v in results.items()
                },
                "identity_invariants": identity_checks,
                "a_f_invariant": identity_checks.get("legacy_a_f"),
                "status": "harness_invariant_failed",
                "limitations": _limitations_text(),
            }
            _emit(out, args.out_json)
            return 1
    else:
        identity_checks["passthrough_a_pt_f_pt"] = {
            "ok": True,
            "status": "skipped_missing_a_pt_or_f_pt",
            "pair": "a_pt_vs_f_pt",
        }

    # Back-compat field name for older consumers.
    a_f = identity_checks["legacy_a_f"]

    # Comparisons to P0
    comparisons = {}
    p0 = results["p0"]
    for alias, r in results.items():
        if alias == "p0":
            continue
        comparisons[alias] = compare_to_p0(p0, r, thresholds, compare_cfg["motion_thresh"])
        c = comparisons[alias]
        print(
            f"compare {alias} vs p0: label_mismatch={c['labels']['mismatch_count']} "
            f"pause_bounds_equal={c['pause_core']['bounds_equal']} "
            f"speed_equal={c['speed_segments_equal']}"
        )

    cand_jsonable = {k: result_to_jsonable(v) for k, v in results.items()}
    acceptance_bundle = _pyify(
        evaluate_b1_acceptance(
            candidate_results=cand_jsonable,
            comparisons_to_p0=comparisons,
            identity_invariants=identity_checks,
            motion_thr=float(compare_cfg["motion_thresh"]),
            frames_expected=int(args.frames),
            execution_ok=True,
            cleanup_ok=all(v.cleanup_ok for v in results.values()),
            serialization_ok=True,
            candidates_completed=list(results.keys()),
        )
    )

    out = {
        "metadata": metadata,
        "configuration": configuration,
        "template_summary": template_summary,
        "candidate_results": cand_jsonable,
        "comparisons_to_p0": comparisons,
        "identity_invariants": identity_checks,
        "a_f_invariant": a_f,
        "pause_core_results": {
            k: {
                "pause_core": v.pause_core,
                "speed_segments": v.speed_segments,
                "boundary_diagnostic": v.boundary_diagnostic,
            }
            for k, v in results.items()
        },
        "execution_status": acceptance_bundle["execution_status"],
        "acceptance": acceptance_bundle["acceptance"],
        "sample_coverage": acceptance_bundle["sample_coverage"],
        "legacy_gate": acceptance_bundle["legacy_gate"],
        "warnings": acceptance_bundle["warnings"],
        "limitations": _limitations_text(),
        # Compatibility only: harness finished the requested B1 pipeline.
        # Does NOT mean business acceptance or production coverage.
        "status": "b1_ok",
        "status_meaning": (
            "harness_execution_completed_only; see acceptance.result and sample_coverage"
        ),
    }
    try:
        json.dumps(out)
    except TypeError as exc:
        print(f"error: result not JSON-serializable: {exc}", file=sys.stderr)
        return 1
    _emit(out, args.out_json)
    acc = acceptance_bundle["acceptance"]["result"]
    print(f"\nstatus: b1_ok (execution complete; acceptance.result={acc})")
    print(f"acceptance: {acc}")
    print(f"legacy_gate: {acceptance_bundle['legacy_gate']['result_under_previous_policy']}")
    print(f"sample_coverage: {acceptance_bundle['sample_coverage']['coverage_status']}")
    if acceptance_bundle["warnings"]:
        print(f"warnings: {len(acceptance_bundle['warnings'])}")
        for w in acceptance_bundle["warnings"][:10]:
            print(f"  - {w.get('code')}: frames={w.get('frames')}")
    if acceptance_bundle["acceptance"]["hard_failures"]:
        print("hard_failures:")
        for hf in acceptance_bundle["acceptance"]["hard_failures"]:
            print(f"  - {hf.get('code')}: {hf.get('message')}")
    return 0


def _limitations_text() -> dict[str, Any]:
    return {
        "phase": "B1",
        "candidate_core_fields": [
            "per-frame scores",
            "labels from _classify_gray",
            "diffs",
            "pause start/end",
            "local_del_mask",
            "speed segments",
        ],
        "opencv_contaminated_fields": [
            "boundary_diff",
            "mode (after boundary rewrite)",
            "build_delete_set / final delete ranges",
        ],
        "why_no_build_delete_set": (
            "build_delete_set consumes pause mode which may be forced to 'all' by "
            "boundary_diff computed from OpenCV re-read frames in build_segments; "
            "that path is not candidate-pure until a gray_by_index provider exists."
        ),
        "timeline_status": {
            "P0_vs_legacy_ffmpeg_same_index": {
                "status": "deprecated_for_semantic_comparison",
                "reason": "known_output_frame_sync_phase_shift",
            },
            "legacy_A_equals_F": {
                "status": "verified_within_ffmpeg_default_timeline",
            },
            "legacy_A_vs_E2": {
                "status": "valid_within_ffmpeg_default_timeline",
            },
            "P0_vs_passthrough_candidates": {
                "status": "pending_validation",
            },
        },
        "a_f_pixel_context": (
            "Pixel diagnosis showed legacy A and F byte-identical on the sample "
            "under default FFmpeg sync; B1 enforces identity within each timeline "
            "family (legacy a/f and passthrough a_pt/f_pt)."
        ),
        "aligned_timeline_default": (
            "Default --require-aligned-timeline rejects legacy a/f/e2 with P0; "
            "use a_pt/f_pt/e2_pt (explicit -fps_mode passthrough)."
        ),
        "acceptance_policy": {
            "version": B1_ACCEPTANCE_POLICY_VERSION,
            "status_field": "status=b1_ok means harness execution completed only",
            "business_field": "acceptance.result",
            "coverage_field": "sample_coverage",
            "legacy_global_motion_gate": "legacy_gate (warning under v2; was hard under previous policy)",
        },
    }


def _emit(obj: dict[str, Any], out_json: str | None) -> None:
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    if out_json:
        Path(out_json).write_text(text, encoding="utf-8")
        print(f"JSON written: {Path(out_json).resolve()}")
    else:
        # Summary already printed; full JSON only if no path? Spec: print JSON to stdout
        # Avoid dumping huge per-frame arrays twice — write compact notice + path optional.
        # Spec says: 未提供时只打印 JSON 到 stdout — honor that.
        print(text)


if __name__ == "__main__":
    sys.exit(main())
