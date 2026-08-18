#!/usr/bin/env python3
"""Decode / preprocess micro-benchmark for arknight-auto-editing.

Standalone tool: does NOT import or modify analyzer.py / video_io.py.
No new dependencies beyond the app baseline (opencv-python, numpy) and
the Python standard library. Optional: psutil / nvidia-smi when present.

Typical smoke:
  python scripts/bench_decode.py \\
    --video D:/ArknightsPathFinding/tmp/input.mp4 \\
    --ffmpeg "C:/Users/AAA/AppData/Local/Programs/Python/Python311/Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe" \\
    --start 0 --frames 30 --warmup 0 --repeat 1
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_PROC_RES = (400, 225)
# Default runnable set (excludes experimental direct NV12).
CASE_ORDER = (
    "opencv_read",
    "opencv_resize",
    "opencv_gray",
    "ffmpeg_sw_bgr",
    "ffmpeg_sw_gray",
    "ffmpeg_cuda_yuv444_gray",
    "ffmpeg_cuda_nv12_416_gray",
)
# Known cases including experimental (not in default list).
EXPERIMENTAL_CASES = (
    "ffmpeg_cuda_nv12_direct",  # former ffmpeg_cuda_nv12; odd size skipped
)
# Diagnostic-only verify cases (not in performance CASE_ORDER).
DIAGNOSTIC_CASES = (
    "ffmpeg_sw_y_extract",  # D: SW scale→yuv444p→extractplanes=y
    "ffmpeg_cuda_yuv444_convert_gray",  # E: CUDA yuv444p→hwdownload→format=gray
    # E + explicit assumed input matrix/range via CPU scale (colorspace filter
    # rejects odd 400x225 on this binary; scale in_* works).
    "ffmpeg_cuda_diag_bt709_tv",
    "ffmpeg_cuda_diag_bt709_pc",
    "ffmpeg_cuda_diag_smpte170m_tv",
    "ffmpeg_cuda_diag_smpte170m_pc",
    # F: CUDA decode + CPU area scale + format=gray (no scale_cuda).
    "ffmpeg_cuda_cpu_area_gray",
    # E with explicit scale_cuda interp_algo=N (0..4).
    "ffmpeg_cuda_e_interp_0",
    "ffmpeg_cuda_e_interp_1",
    "ffmpeg_cuda_e_interp_2",
    "ffmpeg_cuda_e_interp_3",
    "ffmpeg_cuda_e_interp_4",
    # Explicit output fps_mode=passthrough variants (aligned-timeline diagnostics).
    # Commands match A/F/E2 except injecting -fps_mode passthrough before pipe:1.
    "ffmpeg_sw_gray_passthrough",
    "ffmpeg_cuda_cpu_area_gray_passthrough",
    "ffmpeg_cuda_e_interp_2_passthrough",
)
ALL_CASES = CASE_ORDER + EXPERIMENTAL_CASES + DIAGNOSTIC_CASES
_STDOUT_CHUNK = 1024 * 1024
_STDERR_TAIL_BYTES = 128 * 1024
_DEFAULT_PIPE_TIMEOUT_S = 180.0
_P95_MIN_SAMPLES = 20
# Map passthrough diagnostic names → base case (identical graph; only output sync differs).
_PASSTHROUGH_CASE_BASE: dict[str, str] = {
    "ffmpeg_sw_gray_passthrough": "ffmpeg_sw_gray",
    "ffmpeg_cuda_cpu_area_gray_passthrough": "ffmpeg_cuda_cpu_area_gray",
    "ffmpeg_cuda_e_interp_2_passthrough": "ffmpeg_cuda_e_interp_2",
}
_CUDA_GRAY_CASES = frozenset({
    "ffmpeg_cuda_yuv444_gray",
    "ffmpeg_cuda_nv12_416_gray",
    "ffmpeg_cuda_nv12_direct",
    "ffmpeg_cuda_yuv444_convert_gray",
    "ffmpeg_cuda_diag_bt709_tv",
    "ffmpeg_cuda_diag_bt709_pc",
    "ffmpeg_cuda_diag_smpte170m_tv",
    "ffmpeg_cuda_diag_smpte170m_pc",
    "ffmpeg_cuda_cpu_area_gray",
    "ffmpeg_cuda_e_interp_0",
    "ffmpeg_cuda_e_interp_1",
    "ffmpeg_cuda_e_interp_2",
    "ffmpeg_cuda_e_interp_3",
    "ffmpeg_cuda_e_interp_4",
    "ffmpeg_cuda_cpu_area_gray_passthrough",
    "ffmpeg_cuda_e_interp_2_passthrough",
})
# Correctness-verify mode: final gray @ proc-res only (no BGR / raw / experimental).
_VERIFY_REFERENCE_ALLOWED = frozenset({
    "opencv_gray",
    # Pairwise diagnostic refs (FFmpeg gray producers used as reference side).
    "ffmpeg_sw_y_extract",
    "ffmpeg_cuda_yuv444_gray",
    "ffmpeg_sw_gray",
    "ffmpeg_cuda_cpu_area_gray",
    "ffmpeg_cuda_yuv444_convert_gray",
    "ffmpeg_sw_gray_passthrough",
    "ffmpeg_cuda_cpu_area_gray_passthrough",
    "ffmpeg_cuda_e_interp_2_passthrough",
})
_VERIFY_CASE_ALLOWED = frozenset({
    "ffmpeg_sw_gray",
    "ffmpeg_cuda_yuv444_gray",
    "ffmpeg_cuda_nv12_416_gray",
    "ffmpeg_sw_y_extract",
    "ffmpeg_cuda_yuv444_convert_gray",
    "ffmpeg_cuda_diag_bt709_tv",
    "ffmpeg_cuda_diag_bt709_pc",
    "ffmpeg_cuda_diag_smpte170m_tv",
    "ffmpeg_cuda_diag_smpte170m_pc",
    "ffmpeg_cuda_cpu_area_gray",
    "ffmpeg_cuda_e_interp_0",
    "ffmpeg_cuda_e_interp_1",
    "ffmpeg_cuda_e_interp_2",
    "ffmpeg_cuda_e_interp_3",
    "ffmpeg_cuda_e_interp_4",
    "ffmpeg_sw_gray_passthrough",
    "ffmpeg_cuda_cpu_area_gray_passthrough",
    "ffmpeg_cuda_e_interp_2_passthrough",
})
# Historical comparison status (do not rewrite old JSON artifacts).
_TIMELINE_RESULT_STATUS: dict[str, dict[str, str]] = {
    "P0_vs_legacy_ffmpeg_same_index": {
        "status": "deprecated_for_semantic_comparison",
        "reason": "known_output_frame_sync_phase_shift",
    },
    "legacy_A_equals_F": {
        "status": "verified_within_ffmpeg_default_timeline",
        "reason": "byte_and_business_identity_on_default_sync",
    },
    "legacy_A_vs_E2": {
        "status": "valid_within_ffmpeg_default_timeline",
        "reason": "same_ffmpeg_default_sync_family",
    },
    "P0_vs_passthrough_candidates": {
        "status": "pending_validation",
        "reason": "explicit_fps_mode_passthrough_not_yet_full_validated",
    },
}
# From this binary: scale_cuda interp_algo default 0 (unnamed); nearest=1..lanczos=4.
_SCALE_CUDA_INTERP_LABELS: dict[int, str] = {
    0: "0_default_unnamed",
    1: "nearest",
    2: "bilinear",
    3: "bicubic",
    4: "lanczos",
}
# Reference intensity bins for per-luma diagnostics (inclusive edges).
_REF_INTENSITY_BINS: tuple[tuple[int, int], ...] = (
    (0, 15),
    (16, 31),
    (32, 63),
    (64, 95),
    (96, 127),
    (128, 159),
    (160, 191),
    (192, 223),
    (224, 239),
    (240, 255),
)
# Gradient-strength bins on reference (max of |dx|,|dy|), inclusive.
_GRADIENT_BINS: tuple[tuple[int, int], ...] = (
    (0, 0),
    (1, 3),
    (4, 7),
    (8, 15),
    (16, 31),
    (32, 63),
    (64, 127),
    (128, 255),
)
_VERIFY_CASE_FORBIDDEN = frozenset({
    "ffmpeg_sw_bgr",
    "ffmpeg_cuda_nv12_direct",
    "opencv_read",
    "opencv_resize",
    "opencv_gray",  # reference only; never a candidate
})
_DEFAULT_VERIFY_REFERENCE = "opencv_gray"
_DEFAULT_VERIFY_CASES = (
    "ffmpeg_sw_gray",
    "ffmpeg_cuda_yuv444_gray",
    "ffmpeg_cuda_nv12_416_gray",
)
# Color fields reported as unspecified when FFmpeg does not declare them.
_COLOR_UNSPEC = "unspecified"
# From `ffmpeg -h filter=scale_cuda` on the project binary (imageio-ffmpeg v7.1):
#   interp_algo <int> from 0 to 4 (default 0)
#   named: nearest=1, bilinear=2, bicubic=3, lanczos=4  (no name for 0)
# Pin default as integer 0 so the filter has no hidden interp setting.
_SCALE_CUDA_INTERP_ALGO_DEFAULT = 0
_SCALE_CUDA_INTERP_ALGO_LABEL = "0"  # explicit int form of the binary default

# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass
class FFmpegInfo:
    path: str
    version_line: str
    hwaccels: list[str]
    has_h264_cuvid: bool
    has_hevc_cuvid: bool
    has_h264_qsv: bool
    has_scale_cuda: bool
    has_scale_npp: bool
    has_hwdownload: bool
    has_extractplanes: bool
    has_d3d11va: bool
    has_qsv_hwaccel: bool
    cuda_runtime_ok: bool | None = None
    cuda_runtime_reason: str | None = None


@dataclass
class RunMetrics:
    wall_s: float
    frames_actual: int
    fps: float
    ms_per_frame: float
    peak_rss_bytes: int | None = None
    cpu_percent: float | None = None
    gpu_decoder_util: float | None = None
    out_width: int | None = None
    out_height: int | None = None
    y_plane_bytes: int | None = None
    # Explicit FFmpeg execution accounting (not inferred from status alone).
    process_returncode: int | None = None
    bytes_received: int | None = None
    expected_bytes: int | None = None
    complete_frames: int | None = None
    trailing_bytes: int | None = None
    error: str | None = None


@dataclass
class CaseResult:
    case: str
    status: str  # ok | skipped | failed
    skip_or_error_reason: str | None
    frames_expected: int
    frames_actual: int | None
    median_fps: float | None
    median_ms_per_frame: float | None
    min_fps: float | None
    max_fps: float | None
    min_ms_per_frame: float | None
    max_ms_per_frame: float | None
    # Only populated when successful timed runs >= _P95_MIN_SAMPLES; never a
    # renamed max-of-5-runs value.
    p95_ms_per_frame: float | None
    runs: list[dict[str, Any]] = field(default_factory=list)
    command: list[str] | None = None
    notes: str | None = None
    # Path metadata for FFmpeg cases (decode/scale/download/post).
    path_meta: dict[str, Any] | None = None
    # Last timed-run explicit accounting (FFmpeg); not inferred from status.
    process_returncode: int | None = None
    bytes_received: int | None = None
    expected_bytes: int | None = None
    complete_frames: int | None = None
    trailing_bytes: int | None = None
    # Pixel verification summary (verify mode only).
    verify: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _which_like(name: str) -> str | None:
    return shutil.which(name)


def _run_capture(cmd: list[str], timeout: float | None = 60.0) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return p.returncode, p.stdout or "", p.stderr or ""
    except FileNotFoundError as exc:
        return 127, "", f"FileNotFoundError: {exc}"
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"") if isinstance(exc.stdout, (bytes, bytearray)) else (exc.stdout or "")
        err = (exc.stderr or b"") if isinstance(exc.stderr, (bytes, bytearray)) else (exc.stderr or "")
        if isinstance(out, (bytes, bytearray)):
            out = out.decode("utf-8", errors="replace")
        if isinstance(err, (bytes, bytearray)):
            err = err.decode("utf-8", errors="replace")
        return 124, out, f"TimeoutExpired: {err}"


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _p95(values: list[float]) -> float:
    if not values:
        return float("nan")
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    # nearest-rank style for small N
    k = max(0, min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1)))))
    return ordered[k]


def _median(values: list[float]) -> float:
    return float(statistics.median(values)) if values else float("nan")


def _p95_from_abs_error_hist(hist: np.ndarray) -> float:
    """Exact pixel-level P95 of absolute errors from a length-256 histogram.

    Standard nearest-rank: 0-based index = ceil(0.95 * n) - 1 on the virtual
    sorted list of all pixel abs-errors. This is NOT the mean of per-frame P95s.
    """
    total = int(hist.sum())
    if total <= 0:
        return float("nan")
    # 0-based index into the ordered multiset of size n.
    target_index = int(math.ceil(0.95 * total)) - 1
    if target_index < 0:
        target_index = 0
    if target_index >= total:
        target_index = total - 1
    cum = 0
    for d in range(int(hist.shape[0])):
        cum += int(hist[d])
        if cum > target_index:
            return float(d)
    return float(hist.shape[0] - 1)


def _signed_percentile_from_hist(hist: np.ndarray, p: float) -> float | None:
    """Nearest-rank percentile for signed-error hist indexed by (err+255).

    hist length must be 511 covering signed errors -255..+255.
    Returns Python float or None if empty.
    """
    total = int(hist.sum())
    if total <= 0:
        return None
    # index = ceil(p * n) - 1, p in (0,1]
    target_index = int(math.ceil(float(p) * total)) - 1
    if target_index < 0:
        target_index = 0
    if target_index >= total:
        target_index = total - 1
    cum = 0
    for i in range(int(hist.shape[0])):
        cum += int(hist[i])
        if cum > target_index:
            return float(i - 255)
    return float(int(hist.shape[0]) - 1 - 255)


def _new_signed_accum() -> dict[str, Any]:
    """Streaming accumulators for signed-error / linear-fit / intensity bins."""
    return {
        "signed_hist": np.zeros(511, dtype=np.uint64),  # index = signed + 255
        "sum_signed": 0,  # int64 via Python int
        "sum_ref": 0,
        "sum_cand": 0,
        "sum_ref2": 0,
        "sum_cand2": 0,
        "sum_ref_cand": 0,
        "sum_abs": 0,
        "n": 0,
        "ref_zero": 0,
        "ref_255": 0,
        "cand_zero": 0,
        "cand_255": 0,
        # per-bin: count, sum_ref, sum_cand, sum_signed, sum_abs, abs_hist[256]
        "bins": [
            {
                "lo": lo,
                "hi": hi,
                "count": 0,
                "sum_ref": 0,
                "sum_cand": 0,
                "sum_signed": 0,
                "sum_abs": 0,
                "abs_hist": np.zeros(256, dtype=np.uint64),
            }
            for lo, hi in _REF_INTENSITY_BINS
        ],
        # gradient-strength bins on reference (max |dx|,|dy|)
        "grad_bins": [
            {
                "lo": lo,
                "hi": hi,
                "count": 0,
                "sum_signed": 0,
                "sum_abs": 0,
                "count_gt_8": 0,
                "abs_hist": np.zeros(256, dtype=np.uint64),
            }
            for lo, hi in _GRADIENT_BINS
        ],
    }


def _update_signed_accum(
    accum: dict[str, Any],
    reference: np.ndarray,
    candidate: np.ndarray,
) -> None:
    """Update streaming signed/linear/bin stats from one gray pair (uint8)."""
    ref = reference.astype(np.int16, copy=False)
    cand = candidate.astype(np.int16, copy=False)
    signed = cand - ref  # int16, range -255..255
    abs_err = np.abs(signed)
    flat_s = signed.ravel()
    flat_a = abs_err.ravel()
    flat_r = ref.ravel()
    flat_c = cand.ravel()
    n = int(flat_s.size)
    if n <= 0:
        return

    # Signed hist: shift by +255 → bins 0..510
    sh = np.bincount((flat_s.astype(np.int32) + 255), minlength=511).astype(
        np.uint64, copy=False
    )
    if sh.shape[0] < 511:
        pad = np.zeros(511, dtype=np.uint64)
        pad[: sh.shape[0]] = sh
        sh = pad
    else:
        sh = sh[:511]
    accum["signed_hist"] += sh

    # Moments (Python int avoids overflow)
    accum["sum_signed"] += int(flat_s.astype(np.int64, copy=False).sum())
    accum["sum_ref"] += int(flat_r.astype(np.int64, copy=False).sum())
    accum["sum_cand"] += int(flat_c.astype(np.int64, copy=False).sum())
    accum["sum_ref2"] += int(
        (flat_r.astype(np.int64, copy=False) * flat_r.astype(np.int64, copy=False)).sum()
    )
    accum["sum_cand2"] += int(
        (flat_c.astype(np.int64, copy=False) * flat_c.astype(np.int64, copy=False)).sum()
    )
    accum["sum_ref_cand"] += int(
        (flat_r.astype(np.int64, copy=False) * flat_c.astype(np.int64, copy=False)).sum()
    )
    accum["sum_abs"] += int(flat_a.astype(np.int64, copy=False).sum())
    accum["n"] += n
    accum["ref_zero"] += int((flat_r == 0).sum())
    accum["ref_255"] += int((flat_r == 255).sum())
    accum["cand_zero"] += int((flat_c == 0).sum())
    accum["cand_255"] += int((flat_c == 255).sum())

    # Intensity bins by reference value.
    for b in accum["bins"]:
        lo = int(b["lo"])
        hi = int(b["hi"])
        mask = (flat_r >= lo) & (flat_r <= hi)
        cnt = int(mask.sum())
        if cnt <= 0:
            continue
        rs = flat_r[mask]
        cs = flat_c[mask]
        ss = flat_s[mask]
        aa = flat_a[mask]
        b["count"] += cnt
        b["sum_ref"] += int(rs.astype(np.int64, copy=False).sum())
        b["sum_cand"] += int(cs.astype(np.int64, copy=False).sum())
        b["sum_signed"] += int(ss.astype(np.int64, copy=False).sum())
        b["sum_abs"] += int(aa.astype(np.int64, copy=False).sum())
        ah = np.bincount(aa.astype(np.int32), minlength=256).astype(
            np.uint64, copy=False
        )
        if ah.shape[0] < 256:
            pad = np.zeros(256, dtype=np.uint64)
            pad[: ah.shape[0]] = ah
            ah = pad
        else:
            ah = ah[:256]
        b["abs_hist"] += ah

    # Gradient strength on reference: max(|dx|, |dy|); edge pixels use available side.
    # Shape (H, W); compute full maps then bin without retaining the maps.
    h, w = int(reference.shape[0]), int(reference.shape[1])
    if h >= 1 and w >= 1:
        ref_i = ref  # int16
        dx = np.zeros((h, w), dtype=np.int16)
        dy = np.zeros((h, w), dtype=np.int16)
        if w >= 2:
            d = np.abs(ref_i[:, 1:] - ref_i[:, :-1])
            dx[:, 1:] = d
            dx[:, 0] = d[:, 0]
        if h >= 2:
            d = np.abs(ref_i[1:, :] - ref_i[:-1, :])
            dy[1:, :] = d
            dy[0, :] = d[0, :]
        grad = np.maximum(dx, dy).ravel()
        # re-use signed/abs flats (same order as ravel)
        for gb in accum["grad_bins"]:
            lo = int(gb["lo"])
            hi = int(gb["hi"])
            mask = (grad >= lo) & (grad <= hi)
            cnt = int(mask.sum())
            if cnt <= 0:
                continue
            ss = flat_s[mask]
            aa = flat_a[mask]
            gb["count"] += cnt
            gb["sum_signed"] += int(ss.astype(np.int64, copy=False).sum())
            gb["sum_abs"] += int(aa.astype(np.int64, copy=False).sum())
            gb["count_gt_8"] += int((aa > 8).sum())
            ah = np.bincount(aa.astype(np.int32), minlength=256).astype(
                np.uint64, copy=False
            )
            if ah.shape[0] < 256:
                pad = np.zeros(256, dtype=np.uint64)
                pad[: ah.shape[0]] = ah
                ah = pad
            else:
                ah = ah[:256]
            gb["abs_hist"] += ah


def _finalize_signed_accum(accum: dict[str, Any]) -> dict[str, Any]:
    """Produce JSON-safe signed/linear/bin summary from streaming accumulators."""
    n = int(accum["n"])
    sh: np.ndarray = accum["signed_hist"]
    if n <= 0:
        return {
            "mean_signed_error": None,
            "median_signed_error": None,
            "signed_error_p05": None,
            "signed_error_p25": None,
            "signed_error_p75": None,
            "signed_error_p95": None,
            "negative_error_ratio": None,
            "positive_error_ratio": None,
            "zero_error_ratio": None,
            "reference_zero_count": 0,
            "reference_255_count": 0,
            "candidate_zero_count": 0,
            "candidate_255_count": 0,
            "pearson_r": None,
            "linear_fit_slope": None,
            "linear_fit_intercept": None,
            "linear_fit_r2": None,
            "intensity_bins": [],
            "gradient_bins": [],
            "sample_pixels": 0,
        }

    mean_s = float(accum["sum_signed"]) / float(n)
    med = _signed_percentile_from_hist(sh, 0.50)
    p05 = _signed_percentile_from_hist(sh, 0.05)
    p25 = _signed_percentile_from_hist(sh, 0.25)
    p75 = _signed_percentile_from_hist(sh, 0.75)
    p95 = _signed_percentile_from_hist(sh, 0.95)

    # signed hist: index 0 = -255, index 255 = 0, index 510 = +255
    neg = int(sh[:255].sum())
    zero = int(sh[255])
    pos = int(sh[256:].sum())

    sum_r = float(accum["sum_ref"])
    sum_c = float(accum["sum_cand"])
    sum_r2 = float(accum["sum_ref2"])
    sum_c2 = float(accum["sum_cand2"])
    sum_rc = float(accum["sum_ref_cand"])
    nf = float(n)

    # Pearson r and least-squares cand ≈ slope * ref + intercept
    cov = sum_rc - (sum_r * sum_c) / nf
    var_r = sum_r2 - (sum_r * sum_r) / nf
    var_c = sum_c2 - (sum_c * sum_c) / nf
    pearson: float | None
    slope: float | None
    intercept: float | None
    r2: float | None
    if var_r > 0.0 and var_c > 0.0:
        denom = math.sqrt(var_r * var_c)
        pearson = float(cov / denom) if denom > 0.0 else None
    else:
        pearson = None
    if var_r > 0.0:
        slope = float(cov / var_r)
        intercept = float((sum_c / nf) - slope * (sum_r / nf))
        # R² for the linear fit: 1 - SS_res/SS_tot on candidate
        # SS_res = sum (c - (s*r+i))^2 expanded via moments
        # = sum_c2 - 2*s*sum_rc - 2*i*sum_c + s^2*sum_r2 + 2*s*i*sum_r + i^2*n
        ss_res = (
            sum_c2
            - 2.0 * slope * sum_rc
            - 2.0 * intercept * sum_c
            + (slope * slope) * sum_r2
            + 2.0 * slope * intercept * sum_r
            + (intercept * intercept) * nf
        )
        ss_tot = var_c
        if ss_tot > 0.0 and math.isfinite(ss_res):
            r2 = float(1.0 - (ss_res / ss_tot))
            if not math.isfinite(r2):
                r2 = None
        else:
            r2 = None
    else:
        slope = None
        intercept = None
        r2 = None

    def _finite_or_none(x: float | None) -> float | None:
        if x is None:
            return None
        try:
            xf = float(x)
        except (TypeError, ValueError):
            return None
        return xf if math.isfinite(xf) else None

    bins_out: list[dict[str, Any]] = []
    for b in accum["bins"]:
        cnt = int(b["count"])
        if cnt <= 0:
            bins_out.append(
                {
                    "lo": int(b["lo"]),
                    "hi": int(b["hi"]),
                    "pixel_count": 0,
                    "mean_reference": None,
                    "mean_candidate": None,
                    "mean_signed_error": None,
                    "mean_abs_error": None,
                    "p95_abs_error": None,
                }
            )
            continue
        bins_out.append(
            {
                "lo": int(b["lo"]),
                "hi": int(b["hi"]),
                "pixel_count": cnt,
                "mean_reference": float(b["sum_ref"]) / float(cnt),
                "mean_candidate": float(b["sum_cand"]) / float(cnt),
                "mean_signed_error": float(b["sum_signed"]) / float(cnt),
                "mean_abs_error": float(b["sum_abs"]) / float(cnt),
                "p95_abs_error": float(_p95_from_abs_error_hist(b["abs_hist"])),
            }
        )

    grad_out: list[dict[str, Any]] = []
    for gb in accum.get("grad_bins", []):
        cnt = int(gb["count"])
        if cnt <= 0:
            grad_out.append(
                {
                    "lo": int(gb["lo"]),
                    "hi": int(gb["hi"]),
                    "pixel_count": 0,
                    "mean_abs_error": None,
                    "p95_abs_error": None,
                    "ratio_gt_8": None,
                    "mean_signed_error": None,
                }
            )
            continue
        grad_out.append(
            {
                "lo": int(gb["lo"]),
                "hi": int(gb["hi"]),
                "pixel_count": cnt,
                "mean_abs_error": float(gb["sum_abs"]) / float(cnt),
                "p95_abs_error": float(_p95_from_abs_error_hist(gb["abs_hist"])),
                "ratio_gt_8": float(gb["count_gt_8"]) / float(cnt),
                "mean_signed_error": float(gb["sum_signed"]) / float(cnt),
            }
        )

    return {
        "mean_signed_error": _finite_or_none(mean_s),
        "median_signed_error": _finite_or_none(med),
        "signed_error_p05": _finite_or_none(p05),
        "signed_error_p25": _finite_or_none(p25),
        "signed_error_p75": _finite_or_none(p75),
        "signed_error_p95": _finite_or_none(p95),
        "negative_error_ratio": float(neg) / nf,
        "positive_error_ratio": float(pos) / nf,
        "zero_error_ratio": float(zero) / nf,
        "reference_zero_count": int(accum["ref_zero"]),
        "reference_255_count": int(accum["ref_255"]),
        "candidate_zero_count": int(accum["cand_zero"]),
        "candidate_255_count": int(accum["cand_255"]),
        "pearson_r": _finite_or_none(pearson),
        "linear_fit_slope": _finite_or_none(slope),
        "linear_fit_intercept": _finite_or_none(intercept),
        "linear_fit_r2": _finite_or_none(r2),
        "intensity_bins": bins_out,
        "gradient_bins": grad_out,
        "sample_pixels": n,
    }


def _compare_gray_pair(
    reference: np.ndarray,
    candidate: np.ndarray,
    hist: np.ndarray,
    frame_index: int,
    top_k_pixels: int = 20,
) -> dict[str, Any]:
    """Compare one gray pair; update global abs-error histogram in-place.

    diff = abs(ref.astype(int16) - cand.astype(int16))  # no uint8 wrap
    Returns per-frame scalar metrics, top-k error pixels for this frame, and
    bbox of abs_error>8 pixels. Does not retain the full diff after return.
    """
    diff = np.abs(
        reference.astype(np.int16, copy=False) - candidate.astype(np.int16, copy=False)
    )
    flat = diff.ravel()
    n = int(flat.size)
    frame_hist = np.bincount(flat, minlength=256).astype(np.uint64, copy=False)
    if frame_hist.shape[0] < 256:
        padded = np.zeros(256, dtype=np.uint64)
        padded[: frame_hist.shape[0]] = frame_hist
        frame_hist = padded
    else:
        frame_hist = frame_hist[:256]
    hist += frame_hist

    total_abs = int(flat.astype(np.int64, copy=False).sum())
    mae = float(total_abs) / float(n) if n else 0.0
    max_abs = int(flat.max()) if n else 0
    p95 = _p95_from_abs_error_hist(frame_hist)

    def _gt(t: int) -> int:
        # abs > t → bins (t+1)..255
        return int(frame_hist[t + 1 :].sum()) if t + 1 < 256 else 0

    gt1 = _gt(1)
    gt2 = _gt(2)
    gt4 = _gt(4)
    gt8 = _gt(8)
    gt16 = _gt(16)
    gt32 = _gt(32)
    gt64 = _gt(64)
    gt128 = _gt(128)

    # Top-k pixels of this frame (by abs_error desc, then y, x).
    k = min(int(top_k_pixels), n) if n else 0
    top_pixels: list[dict[str, int]] = []
    if k > 0:
        if n <= k:
            order = np.argsort(-flat, kind="stable")
        else:
            part = np.argpartition(flat, -k)[-k:]
            order = part[np.argsort(-flat[part], kind="stable")]
        h, w = int(diff.shape[0]), int(diff.shape[1])
        for idx in order:
            ii = int(idx)
            y, x = divmod(ii, w)
            ae = int(flat[ii])
            top_pixels.append(
                {
                    "frame_index": int(frame_index),
                    "x": int(x),
                    "y": int(y),
                    "reference_value": int(reference[y, x]),
                    "candidate_value": int(candidate[y, x]),
                    "abs_error": ae,
                }
            )
        # Stable secondary keys: abs desc, frame, y, x (frame already fixed).
        top_pixels.sort(
            key=lambda p: (-p["abs_error"], p["frame_index"], p["y"], p["x"])
        )

    # Bounding box of abs_error > 8 (for worst-frame diagnostics).
    bbox_gt_8: dict[str, int] | None = None
    if gt8 > 0:
        ys, xs = np.where(diff > 8)
        bbox_gt_8 = {
            "min_x": int(xs.min()),
            "max_x": int(xs.max()),
            "min_y": int(ys.min()),
            "max_y": int(ys.max()),
            "count_gt_8": int(gt8),
        }

    return {
        "mae": mae,
        "max_abs_error": max_abs,
        "pixel_p95_abs_error": float(p95),
        "pixels": n,
        "total_abs_error": total_abs,
        "count_gt_1": gt1,
        "count_gt_2": gt2,
        "count_gt_4": gt4,
        "count_gt_8": gt8,
        "count_gt_16": gt16,
        "count_gt_32": gt32,
        "count_gt_64": gt64,
        "count_gt_128": gt128,
        "ratio_gt_1": float(gt1) / float(n) if n else 0.0,
        "ratio_gt_2": float(gt2) / float(n) if n else 0.0,
        "ratio_gt_4": float(gt4) / float(n) if n else 0.0,
        "ratio_gt_8": float(gt8) / float(n) if n else 0.0,
        "ratio_gt_16": float(gt16) / float(n) if n else 0.0,
        "ratio_gt_32": float(gt32) / float(n) if n else 0.0,
        "ratio_gt_64": float(gt64) / float(n) if n else 0.0,
        "ratio_gt_128": float(gt128) / float(n) if n else 0.0,
        "top_pixels": top_pixels,
        "bbox_gt_8": bbox_gt_8,
    }


def _merge_top_error_pixels(
    global_top: list[dict[str, int]],
    frame_top: list[dict[str, int]],
    k: int = 20,
) -> list[dict[str, int]]:
    """Merge frame top pixels into global top-k; keep Python ints only."""
    merged = list(global_top) + list(frame_top)
    merged.sort(
        key=lambda p: (-int(p["abs_error"]), int(p["frame_index"]), int(p["y"]), int(p["x"]))
    )
    out: list[dict[str, int]] = []
    for p in merged[:k]:
        out.append(
            {
                "frame_index": int(p["frame_index"]),
                "x": int(p["x"]),
                "y": int(p["y"]),
                "reference_value": int(p["reference_value"]),
                "candidate_value": int(p["candidate_value"]),
                "abs_error": int(p["abs_error"]),
            }
        )
    return out


def _verify_consistency_checks(
    *,
    hist: np.ndarray,
    total_pixels: int,
    total_abs_error: int,
    frames_compared: int,
    frames_requested: int,
    width: int,
    height: int,
    global_max_abs_error: int,
    counts: dict[str, int],
    ratios: dict[str, float],
) -> list[str]:
    """Return list of consistency failure messages (empty => ok). No assert."""
    failures: list[str] = []
    hist_sum = int(hist.sum())
    if hist_sum != int(total_pixels):
        failures.append(
            f"hist.sum()={hist_sum} != total_pixels={total_pixels}"
        )
    expected_pixels = int(frames_compared) * int(width) * int(height)
    if int(total_pixels) != expected_pixels:
        failures.append(
            f"total_pixels={total_pixels} != frames*W*H={expected_pixels}"
        )
    recon_abs = 0
    for d in range(256):
        recon_abs += int(d) * int(hist[d])
    if recon_abs != int(total_abs_error):
        failures.append(
            f"sum(d*hist[d])={recon_abs} != total_abs_error={total_abs_error}"
        )
    order = (
        "count_gt_128",
        "count_gt_64",
        "count_gt_32",
        "count_gt_16",
        "count_gt_8",
        "count_gt_4",
        "count_gt_2",
        "count_gt_1",
    )
    for a, b in zip(order, order[1:]):
        if int(counts[a]) > int(counts[b]):
            failures.append(f"threshold order violated: {a}={counts[a]} > {b}={counts[b]}")
    for name, c in counts.items():
        if c < 0 or c > int(total_pixels):
            failures.append(f"{name}={c} out of [0, total_pixels={total_pixels}]")
    for name, r in ratios.items():
        if not (0.0 <= float(r) <= 1.0):
            failures.append(f"{name}={r} out of [0.0, 1.0]")
    # global_max must equal last non-zero histogram bin (or 0 if empty).
    last_nz = 0
    for d in range(255, -1, -1):
        if int(hist[d]) > 0:
            last_nz = d
            break
    if int(global_max_abs_error) != last_nz:
        failures.append(
            f"global_max_abs_error={global_max_abs_error} != last_nonzero_bin={last_nz}"
        )
    if int(frames_compared) != int(frames_requested):
        failures.append(
            f"frames_compared={frames_compared} != frames_requested={frames_requested}"
        )
    return failures


# ---------------------------------------------------------------------------
# Resource sampling (best-effort, no required deps)
# ---------------------------------------------------------------------------


class ResourceSampler:
    """Sample peak RSS of a target PID and optional nvidia decoder util."""

    def __init__(self, pid: int | None, sample_gpu: bool):
        self.pid = pid
        self.sample_gpu = sample_gpu
        self.peak_rss: int | None = None
        self.gpu_samples: list[float] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._psutil_proc = None
        if pid is not None:
            try:
                import psutil  # type: ignore

                self._psutil_proc = psutil.Process(pid)
            except Exception:
                self._psutil_proc = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._psutil_proc is not None:
                try:
                    rss = int(self._psutil_proc.memory_info().rss)
                    self.peak_rss = rss if self.peak_rss is None else max(self.peak_rss, rss)
                except Exception:
                    pass
            if self.sample_gpu:
                util = _nvidia_decoder_util()
                if util is not None:
                    self.gpu_samples.append(util)
            self._stop.wait(0.2)

    def gpu_decoder_util_mean(self) -> float | None:
        if not self.gpu_samples:
            return None
        return float(sum(self.gpu_samples) / len(self.gpu_samples))


def _nvidia_decoder_util() -> float | None:
    smi = _which_like("nvidia-smi")
    if not smi:
        return None
    rc, out, _ = _run_capture(
        [smi, "--query-gpu=utilization.decoder", "--format=csv,noheader,nounits"],
        timeout=5.0,
    )
    if rc != 0 or not out.strip():
        return None
    try:
        return float(out.strip().splitlines()[0].strip())
    except ValueError:
        return None


def _self_rss_bytes() -> int | None:
    try:
        import psutil  # type: ignore

        return int(psutil.Process(os.getpid()).memory_info().rss)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# FFmpeg resolution / probing
# ---------------------------------------------------------------------------


def resolve_ffmpeg(spec: str) -> str:
    """Resolve --ffmpeg value to an absolute existing path.

    No implicit default binary. 'auto' searches known candidates only.
    """
    spec = spec.strip()
    if not spec:
        raise SystemExit("error: --ffmpeg is required (path or 'auto')")

    if spec.lower() != "auto":
        path = Path(spec).expanduser()
        if not path.is_file():
            raise SystemExit(f"error: --ffmpeg path not found: {path}")
        return str(path.resolve())

    candidates: list[Path] = []
    which = _which_like("ffmpeg")
    if which:
        candidates.append(Path(which))

    # Known local builds observed during recovery (no disk-wide scan).
    home = Path.home()
    candidates.extend(
        [
            home
            / "AppData/Local/Programs/Python/Python311/Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe",
            home
            / "AppData/Local/Programs/Python/Python312/Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe",
            Path(r"D:/BiliTools/ffmpeg.exe"),
        ]
    )

    for c in candidates:
        try:
            if c.is_file():
                return str(c.resolve())
        except OSError:
            continue
    raise SystemExit(
        "error: --ffmpeg auto could not resolve a usable binary "
        "(PATH ffmpeg / known imageio_ffmpeg / D:/BiliTools/ffmpeg.exe)"
    )


def probe_ffmpeg(ffmpeg: str) -> FFmpegInfo:
    rc, out, err = _run_capture([ffmpeg, "-hide_banner", "-version"], timeout=15.0)
    version_line = _first_line(out or err) if (rc == 0 or out or err) else f"version probe failed rc={rc}"

    rc_h, out_h, err_h = _run_capture([ffmpeg, "-hide_banner", "-hwaccels"], timeout=15.0)
    hw_text = out_h or err_h
    hwaccels: list[str] = []
    if rc_h == 0:
        for line in hw_text.splitlines():
            s = line.strip()
            if not s or s.lower().startswith("hardware"):
                continue
            hwaccels.append(s.split()[0])

    rc_d, out_d, err_d = _run_capture([ffmpeg, "-hide_banner", "-decoders"], timeout=20.0)
    dec_text = (out_d or "") + "\n" + (err_d or "")
    has_h264_cuvid = bool(re.search(r"\bh264_cuvid\b", dec_text))
    has_hevc_cuvid = bool(re.search(r"\bhevc_cuvid\b", dec_text))
    has_h264_qsv = bool(re.search(r"\bh264_qsv\b", dec_text))

    rc_f, out_f, err_f = _run_capture([ffmpeg, "-hide_banner", "-filters"], timeout=20.0)
    filt_text = (out_f or "") + "\n" + (err_f or "")
    has_scale_cuda = bool(re.search(r"\bscale_cuda\b", filt_text))
    has_scale_npp = bool(re.search(r"\bscale_npp\b", filt_text))
    has_hwdownload = bool(re.search(r"\bhwdownload\b", filt_text))
    has_extractplanes = bool(re.search(r"\bextractplanes\b", filt_text))

    has_d3d11va = any(h.lower() == "d3d11va" for h in hwaccels)
    has_qsv_hwaccel = any(h.lower() == "qsv" for h in hwaccels)

    return FFmpegInfo(
        path=ffmpeg,
        version_line=version_line,
        hwaccels=hwaccels,
        has_h264_cuvid=has_h264_cuvid,
        has_hevc_cuvid=has_hevc_cuvid,
        has_h264_qsv=has_h264_qsv,
        has_scale_cuda=has_scale_cuda,
        has_scale_npp=has_scale_npp,
        has_hwdownload=has_hwdownload,
        has_extractplanes=has_extractplanes,
        has_d3d11va=has_d3d11va,
        has_qsv_hwaccel=has_qsv_hwaccel,
    )


def _cuda_component_gaps(info: FFmpegInfo, *, need_extractplanes: bool) -> list[str]:
    missing: list[str] = []
    if "cuda" not in [h.lower() for h in info.hwaccels]:
        missing.append("hwaccel:cuda")
    if not info.has_h264_cuvid:
        missing.append("decoder:h264_cuvid")
    if not info.has_scale_cuda:
        missing.append("filter:scale_cuda")
    if not info.has_hwdownload:
        missing.append("filter:hwdownload")
    if need_extractplanes and not info.has_extractplanes:
        missing.append("filter:extractplanes")
    return missing


def even_16x9_intermediate(out_w: int, out_h: int) -> tuple[int, int] | None:
    """Next even 16:9 size >= proc-res, strictly preserving 16:9.

    Integer 16:9 sizes are exactly (16*k, 9*k). Both dimensions are even iff k is
    even (16k always even; 9k even only when k even). For default 400x225 (k=25)
    this yields 416x234.
    """
    if out_w <= 0 or out_h <= 0:
        return None
    if out_w * 9 != out_h * 16:
        return None
    if out_w % 16 != 0 or out_h % 9 != 0 or (out_w // 16) != (out_h // 9):
        return None
    k = out_w // 16
    if k % 2 == 1:
        k += 1
    return (16 * k, 9 * k)


def probe_cuda_runtime(ffmpeg: str, video: str, info: FFmpegInfo) -> FFmpegInfo:
    """Short real CUDA init probe (1 frame). Component presence is not enough."""
    missing = _cuda_component_gaps(info, need_extractplanes=False)
    if missing:
        info.cuda_runtime_ok = False
        info.cuda_runtime_reason = "missing components: " + ", ".join(missing)
        return info

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-hwaccel",
        "cuda",
        "-hwaccel_output_format",
        "cuda",
        "-i",
        video,
        "-an",
        "-frames:v",
        "1",
        "-vf",
        "scale_cuda=64:36,hwdownload,format=nv12",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "nv12",
        "pipe:1",
    ]
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            timeout=30.0,
        )
    except subprocess.TimeoutExpired:
        info.cuda_runtime_ok = False
        info.cuda_runtime_reason = "cuda runtime probe timed out"
        return info
    except OSError as exc:
        info.cuda_runtime_ok = False
        info.cuda_runtime_reason = f"cuda runtime probe OSError: {exc}"
        return info

    stderr = (p.stderr or b"").decode("utf-8", errors="replace").strip()
    # NV12 64x36 => Y=64*36, UV=64*36/2 => 3456 bytes
    expected = 64 * 36 + (64 * 36) // 2
    if p.returncode != 0:
        info.cuda_runtime_ok = False
        info.cuda_runtime_reason = (
            f"cuda runtime probe ffmpeg rc={p.returncode}: {stderr[:500] or '(empty stderr)'}"
        )
        return info
    if len(p.stdout or b"") < expected:
        info.cuda_runtime_ok = False
        info.cuda_runtime_reason = (
            f"cuda runtime probe short output: got {len(p.stdout or b'')} < {expected}; "
            f"stderr={stderr[:300] or '(empty)'}"
        )
        return info
    info.cuda_runtime_ok = True
    info.cuda_runtime_reason = None
    return info


# ---------------------------------------------------------------------------
# Video metadata
# ---------------------------------------------------------------------------


def open_video_meta(path: str) -> dict[str, Any]:
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise SystemExit(f"error: OpenCV cannot open video: {path}")
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fourcc_i = int(cap.get(cv2.CAP_PROP_FOURCC) or 0)
    fourcc = "".join(chr((fourcc_i >> (8 * i)) & 0xFF) for i in range(4))
    cap.release()
    return {
        "path": str(Path(path).resolve()),
        "width": w,
        "height": h,
        "fps": fps,
        "frame_count": n,
        "fourcc": fourcc,
    }


def probe_video_color_meta(ffmpeg_path: str, video_path: str) -> dict[str, Any]:
    """Read-only color / stream metadata from FFmpeg -i banner (no ffprobe required).

    Fields not declared by this FFmpeg binary's demuxer output are recorded as
    'unspecified' — never guessed.
    """
    out: dict[str, Any] = {
        "codec_name": _COLOR_UNSPEC,
        "pix_fmt": _COLOR_UNSPEC,
        "width": None,
        "height": None,
        "avg_frame_rate": _COLOR_UNSPEC,
        "color_range": _COLOR_UNSPEC,
        "color_space": _COLOR_UNSPEC,
        "color_primaries": _COLOR_UNSPEC,
        "color_transfer": _COLOR_UNSPEC,
        "field_order": _COLOR_UNSPEC,
        "raw_stream_line": "",
        "probe_tool": "ffmpeg -i",
    }
    try:
        p = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-i", video_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30.0,
        )
        # FFmpeg prints probe info to stderr and exits non-zero without -f null.
        text = (p.stderr or "") + "\n" + (p.stdout or "")
    except Exception as exc:
        out["probe_error"] = f"{type(exc).__name__}: {exc}"
        return out

    # Stream line example:
    # Stream #0:0(...): Video: h264 (Main) (...), yuv420p(progressive), 2560x1440 ...
    stream_re = re.compile(
        r"Stream\s+#0:0[^\n]*?Video:\s*"
        r"(?P<codec>[a-zA-Z0-9_]+)"
        r"[^\n]*?,\s*"
        r"(?P<pixfmt>[a-zA-Z0-9_]+)"
        r"(?:\((?P<pixattrs>[^)]*)\))?"
        r"[^\n]*?,\s*"
        r"(?P<w>\d+)x(?P<h>\d+)"
        r"[^\n]*?"
        r"(?P<fps>[\d.]+)\s*fps",
        re.IGNORECASE,
    )
    m = stream_re.search(text)
    if m:
        out["codec_name"] = m.group("codec")
        out["pix_fmt"] = m.group("pixfmt")
        out["width"] = int(m.group("w"))
        out["height"] = int(m.group("h"))
        out["avg_frame_rate"] = m.group("fps")
        out["raw_stream_line"] = m.group(0).strip()
        attrs = (m.group("pixattrs") or "").lower()
        if "progressive" in attrs:
            out["field_order"] = "progressive"
        elif "interlaced" in attrs or "top first" in attrs or "bottom first" in attrs:
            out["field_order"] = attrs
        # color tags only if explicitly present in attrs or nearby text.
        for key, patterns in (
            ("color_range", (r"\btv\b", r"\bpc\b", r"\bfull\b", r"\blimited\b", r"color_range[=:](\w+)")),
            ("color_space", (r"bt709", r"bt470", r"smpte170m", r"bt2020", r"color_space[=:](\w+)")),
            ("color_primaries", (r"primaries[=:](\w+)", r"bt709", r"bt2020")),
            ("color_transfer", (r"transfer[=:](\w+)", r"smpte2084", r"bt709", r"iec61966")),
        ):
            found = None
            for pat in patterns:
                mm = re.search(pat, attrs + " " + text.lower())
                if mm:
                    found = mm.group(1) if mm.lastindex else mm.group(0)
                    break
            # Only accept if the tag appears as an explicit color_* field near stream.
            # For this binary, yuv420p(progressive) alone does NOT declare matrix/range.
            if found and ("color_" in text.lower() or "=" in (found or "")):
                out[key] = found
    else:
        # Fallback: capture any Stream #0:0 video line raw.
        for line in text.splitlines():
            if "Stream #0:0" in line and "Video:" in line:
                out["raw_stream_line"] = line.strip()
                break
    return out


# ---------------------------------------------------------------------------
# OpenCV cases
# ---------------------------------------------------------------------------


def run_opencv(
    case: str,
    video: str,
    frames: int,
    proc_res: tuple[int, int],
) -> RunMetrics:
    pw, ph = proc_res
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        return RunMetrics(0.0, 0, 0.0, 0.0, error=f"OpenCV open failed: {video}")

    # Enforce start=0 path: reopen from beginning (no seek API used).
    got = 0
    out_w = out_h = None
    rss0 = _self_rss_bytes()
    t0 = time.perf_counter()
    try:
        while got < frames:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            if case == "opencv_read":
                out_w, out_h = int(frame.shape[1]), int(frame.shape[0])
            elif case == "opencv_resize":
                small = cv2.resize(frame, (pw, ph), interpolation=cv2.INTER_AREA)
                out_w, out_h = int(small.shape[1]), int(small.shape[0])
            elif case == "opencv_gray":
                gray = cv2.cvtColor(
                    cv2.resize(frame, (pw, ph), interpolation=cv2.INTER_AREA),
                    cv2.COLOR_BGR2GRAY,
                )
                out_w, out_h = int(gray.shape[1]), int(gray.shape[0])
            else:
                return RunMetrics(0.0, 0, 0.0, 0.0, error=f"unknown opencv case {case}")
            got += 1
    finally:
        cap.release()
    wall = time.perf_counter() - t0
    rss1 = _self_rss_bytes()
    peak = None
    if rss0 is not None and rss1 is not None:
        peak = max(rss0, rss1)
    elif rss1 is not None:
        peak = rss1
    fps = (got / wall) if wall > 0 else 0.0
    mspf = (wall * 1000.0 / got) if got else 0.0
    # OpenCV path has no subprocess; still report explicit frame accounting.
    exp_bytes = None
    if case in ("opencv_resize", "opencv_gray") and out_w and out_h:
        exp_bytes = got * out_w * out_h * (1 if case == "opencv_gray" else 3)
    elif case == "opencv_read" and out_w and out_h:
        exp_bytes = got * out_w * out_h * 3
    return RunMetrics(
        wall_s=wall,
        frames_actual=got,
        fps=fps,
        ms_per_frame=mspf,
        peak_rss_bytes=peak,
        out_width=out_w,
        out_height=out_h,
        process_returncode=None,
        bytes_received=exp_bytes,
        expected_bytes=exp_bytes,
        complete_frames=got,
        trailing_bytes=0,
    )


def iter_opencv_gray_frames(
    video_path: str,
    frame_count: int,
    proc_width: int,
    proc_height: int,
) -> Iterator[np.ndarray]:
    """Yield gray uint8 frames matching the opencv_gray case pipeline.

    Processing order (must match run_opencv opencv_gray exactly):
      1. cap.read() -> BGR
      2. cv2.resize(..., INTER_AREA) to (proc_width, proc_height)
      3. cv2.cvtColor(..., COLOR_BGR2GRAY)

    Streams one frame at a time (O(1) frames retained). No seek: starts at frame 0.
    On early EOF raises RuntimeError with requested_frames / actual_frames /
    last_good_frame_index. Always releases VideoCapture in finally.
    """
    if frame_count <= 0:
        raise RuntimeError(
            f"iter_opencv_gray_frames: frame_count must be > 0 "
            f"(got {frame_count})"
        )
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"OpenCV open failed: {video_path}")

    got = 0
    last_good = -1
    try:
        while got < frame_count:
            ok, frame = cap.read()
            if not ok or frame is None:
                raise RuntimeError(
                    f"early EOF while reading reference frames: "
                    f"requested_frames={frame_count} actual_frames={got} "
                    f"last_good_frame_index={last_good}"
                )
            # Identical order to run_opencv opencv_gray: resize BGR then BGR2GRAY.
            gray = cv2.cvtColor(
                cv2.resize(
                    frame,
                    (proc_width, proc_height),
                    interpolation=cv2.INTER_AREA,
                ),
                cv2.COLOR_BGR2GRAY,
            )
            if gray.dtype != np.uint8:
                gray = np.ascontiguousarray(gray, dtype=np.uint8)
            elif not gray.flags["C_CONTIGUOUS"]:
                gray = np.ascontiguousarray(gray)
            if gray.shape != (proc_height, proc_width):
                raise RuntimeError(
                    f"unexpected gray shape {gray.shape} "
                    f"(expected {(proc_height, proc_width)}) at frame {got}"
                )
            last_good = got
            got += 1
            yield gray
    finally:
        cap.release()


class FFmpegGrayFrameError(RuntimeError):
    """Controlled failure while streaming gray frames from an FFmpeg pipe."""

    def __init__(
        self,
        message: str,
        *,
        requested_frames: int,
        actual_frames: int,
        current_frame_index: int,
        bytes_in_partial_frame: int,
        returncode: int | None,
        stderr_tail: str,
    ) -> None:
        detail = (
            f"{message}; requested_frames={requested_frames} "
            f"actual_frames={actual_frames} "
            f"current_frame_index={current_frame_index} "
            f"bytes_in_partial_frame={bytes_in_partial_frame} "
            f"returncode={returncode} "
            f"stderr_tail={stderr_tail[:800] or '(empty)'}"
        )
        super().__init__(detail)
        self.requested_frames = requested_frames
        self.actual_frames = actual_frames
        self.current_frame_index = current_frame_index
        self.bytes_in_partial_frame = bytes_in_partial_frame
        self.returncode = returncode
        self.stderr_tail = stderr_tail


def iter_ffmpeg_gray_frames(
    ffmpeg_path: str,
    video_path: str,
    frame_count: int,
    proc_width: int,
    proc_height: int,
    timeout_s: float,
    command: list[str] | None = None,
) -> Iterator[np.ndarray]:
    """Yield gray uint8 frames from an FFmpeg rawvideo pipe (one frame at a time).

    Default command matches the existing ffmpeg_sw_gray case exactly:
      scale=W:H:flags=area,format=gray + rawvideo gray pipe:1

    stdout is read via read_exact (loop until one full frame). stderr goes to a
    TemporaryFile (never an unconsumed PIPE). A single wall-clock deadline covers
    Popen, per-frame reads, FFmpeg exit, and cleanup. Generator close / exception
    paths always kill→wait→close so no FFmpeg process is left behind.
    """
    del ffmpeg_path  # path is already baked into `command` when provided
    if frame_count <= 0:
        raise FFmpegGrayFrameError(
            "frame_count must be > 0",
            requested_frames=frame_count,
            actual_frames=0,
            current_frame_index=-1,
            bytes_in_partial_frame=0,
            returncode=None,
            stderr_tail="",
        )
    bpf = int(proc_width) * int(proc_height)
    if bpf <= 0:
        raise FFmpegGrayFrameError(
            "invalid frame byte size",
            requested_frames=frame_count,
            actual_frames=0,
            current_frame_index=-1,
            bytes_in_partial_frame=0,
            returncode=None,
            stderr_tail="",
        )

    if command is None:
        command, _, _, _, _ = build_ffmpeg_cmd(
            "ffmpeg_sw_gray",
            # build_ffmpeg_cmd expects the ffmpeg binary as first arg of common_head;
            # caller should pass an absolute path via command= for real runs.
            "ffmpeg",
            video_path,
            frame_count,
            (proc_width, proc_height),
        )

    deadline = time.perf_counter() + max(1.0, float(timeout_s))

    def remaining() -> float:
        return deadline - time.perf_counter()

    stderr_file = tempfile.TemporaryFile()
    proc: subprocess.Popen | None = None
    got = 0
    partial = 0
    finished_cleanly = False

    def _stderr() -> str:
        return _stderr_tail_text(stderr_file)

    def _rc() -> int | None:
        if proc is None:
            return None
        return proc.returncode if proc.poll() is not None else proc.returncode

    def _fail(message: str, *, partial_bytes: int = 0) -> None:
        # Best-effort stop before raising so callers never inherit a live process.
        _force_stop_proc(proc)
        raise FFmpegGrayFrameError(
            message,
            requested_frames=frame_count,
            actual_frames=got,
            current_frame_index=got,
            bytes_in_partial_frame=partial_bytes,
            returncode=_rc() if proc is None else (
                proc.returncode if proc.poll() is not None else proc.returncode
            ),
            stderr_tail=_stderr(),
        )

    def read_exact(stdout, n: int) -> bytes:
        """Read exactly n bytes; empty EOF with no data → b''; partial EOF → error."""
        nonlocal partial
        buf = bytearray()
        while len(buf) < n:
            if remaining() <= 0:
                partial = len(buf)
                _fail(
                    f"timeout after {timeout_s:.1f}s while reading frame",
                    partial_bytes=len(buf),
                )
            # Bound each OS read so a stalled pipe cannot hang past the deadline.
            # Use a modest chunk; loop until full frame or EOF.
            to_read = min(n - len(buf), _STDOUT_CHUNK)
            try:
                chunk = stdout.read(to_read)
            except Exception as exc:
                partial = len(buf)
                _fail(f"stdout read failed: {exc!r}", partial_bytes=len(buf))
            if not chunk:
                if not buf:
                    return b""
                partial = len(buf)
                _fail(
                    "EOF with partial frame",
                    partial_bytes=len(buf),
                )
            buf.extend(chunk)
        partial = 0
        return bytes(buf)

    try:
        if remaining() <= 0:
            _fail(f"timeout before Popen after {timeout_s:.1f}s")
        try:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                stdin=subprocess.DEVNULL,
            )
        except OSError as exc:
            _fail(f"Popen failed: {exc}")

        assert proc.stdout is not None

        while got < frame_count:
            if remaining() <= 0:
                _fail(
                    f"timeout after {timeout_s:.1f}s before frame {got}",
                    partial_bytes=0,
                )
            raw = read_exact(proc.stdout, bpf)
            if not raw:
                _fail(
                    "short output: EOF before all frames",
                    partial_bytes=0,
                )
            frame = np.frombuffer(raw, dtype=np.uint8)
            if frame.size != bpf:
                _fail(
                    f"unexpected frame byte count {frame.size}",
                    partial_bytes=int(frame.size),
                )
            gray = np.ascontiguousarray(frame.reshape((proc_height, proc_width)))
            got += 1
            yield gray

        # Drain one more byte probe: any leftover partial data is an error.
        if remaining() <= 0:
            _fail(f"timeout after {timeout_s:.1f}s after last frame")
        extra = proc.stdout.read(1)
        if extra:
            # Keep reading a little to size the partial trailing payload for the error.
            rest = bytearray(extra)
            try:
                more = proc.stdout.read(min(_STDOUT_CHUNK, bpf))
                if more:
                    rest.extend(more)
            except Exception:
                pass
            _fail(
                "trailing bytes after expected frame count",
                partial_bytes=len(rest),
            )

        # Wait for clean exit within remaining deadline.
        rem = remaining()
        try:
            if rem <= 0:
                raise subprocess.TimeoutExpired(command, timeout_s)
            rc = proc.wait(timeout=rem)
        except subprocess.TimeoutExpired:
            _fail(f"ffmpeg wait timed out after {timeout_s:.1f}s")

        if rc != 0:
            _fail(f"ffmpeg non-zero exit rc={rc}")

        # Close stdout now that drain is complete.
        try:
            proc.stdout.close()
        except Exception:
            pass
        finished_cleanly = True
    finally:
        if not finished_cleanly:
            _force_stop_proc(proc)
        elif proc is not None:
            # Clean path: ensure process is reaped and handles closed.
            if proc.poll() is None:
                _force_stop_proc(proc)
            elif proc.stdout is not None:
                try:
                    proc.stdout.close()
                except Exception:
                    pass
        try:
            stderr_file.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# FFmpeg pipe cases
# ---------------------------------------------------------------------------


def build_ffmpeg_cmd(
    case: str,
    ffmpeg: str,
    video: str,
    frames: int,
    proc_res: tuple[int, int],
) -> tuple[list[str], int, tuple[int, int], str, dict[str, Any]]:
    """Return (cmd, bytes_per_frame, final_wh, pix_layout, path_meta).

    pix_layout: 'bgr24' | 'gray' | 'nv12'

    Passthrough diagnostic cases (*_passthrough) reuse the legacy base graph and
    inject output option ``-fps_mode passthrough`` immediately before ``pipe:1``
    (same placement validated in diag_frame_alignment).
    """
    if case in _PASSTHROUGH_CASE_BASE:
        base = _PASSTHROUGH_CASE_BASE[case]
        cmd, bpf, wh, layout, meta = build_ffmpeg_cmd(
            base, ffmpeg, video, frames, proc_res
        )
        if not cmd or cmd[-1] != "pipe:1":
            raise RuntimeError(
                f"passthrough inject expects cmd ending with pipe:1, got tail={cmd[-4:]!r}"
            )
        cmd = cmd[:-1] + ["-fps_mode", "passthrough", "pipe:1"]
        meta = dict(meta)
        meta["output_fps_mode"] = "passthrough"
        meta["frame_sync_policy"] = "explicit_passthrough"
        meta["timeline_family"] = "ffmpeg_passthrough"
        meta["alignment_status"] = "pending_full_validation"
        meta["legacy_base_case"] = base
        return cmd, bpf, wh, layout, meta

    cmd, bpf, wh, layout, meta = _build_ffmpeg_cmd_body(
        case, ffmpeg, video, frames, proc_res
    )
    meta = dict(meta)
    meta.setdefault("output_fps_mode", None)
    meta.setdefault("frame_sync_policy", "ffmpeg_auto")
    meta.setdefault("timeline_family", "ffmpeg_default_sync")
    meta.setdefault(
        "alignment_status",
        "known_one_frame_phase_shift_on_current_sample",
    )
    return cmd, bpf, wh, layout, meta


def _build_ffmpeg_cmd_body(
    case: str,
    ffmpeg: str,
    video: str,
    frames: int,
    proc_res: tuple[int, int],
) -> tuple[list[str], int, tuple[int, int], str, dict[str, Any]]:
    """Internal command builder without passthrough wrapping."""
    pw, ph = proc_res
    common_head = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
    ]
    # start=0 only in formal path: no -ss
    if case == "ffmpeg_sw_bgr":
        raise RuntimeError("ffmpeg_sw_bgr requires meta size; use build_ffmpeg_cmd_with_meta")
    if case == "ffmpeg_sw_gray":
        cmd = common_head + [
            "-i",
            video,
            "-an",
            "-frames:v",
            str(frames),
            "-vf",
            f"scale={pw}:{ph}:flags=area,format=gray",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "pipe:1",
        ]
        meta = {
            "decode_backend": "ffmpeg_software",
            "final_width": pw,
            "final_height": ph,
            "final_pix_fmt": "gray",
            "gpu_scale_width": None,
            "gpu_scale_height": None,
            "gpu_scale_pix_fmt": None,
            "gpu_scale_interp_algo": None,
            "hwdownload_pix_fmt": None,
            "cpu_post_scale": True,
            "cpu_post_scale_algo": "area",
            "cpu_scale": True,
            "cpu_scale_algo": "area",
            "cpu_gray_conversion": True,
            "plane_extraction": None,
            "intermediate_width": None,
            "intermediate_height": None,
            "intermediate_pix_fmt": None,
            "expected_bytes_per_frame": pw * ph,
            "source_color_range": _COLOR_UNSPEC,
            "source_color_space": _COLOR_UNSPEC,
            "source_color_primaries": _COLOR_UNSPEC,
            "source_color_transfer": _COLOR_UNSPEC,
        }
        return cmd, pw * ph, (pw, ph), "gray", meta
    if case == "ffmpeg_cuda_nv12_direct":
        # Experimental: direct NV12 at proc-res (even dims required by caller).
        cmd = common_head + [
            "-hwaccel",
            "cuda",
            "-hwaccel_output_format",
            "cuda",
            "-c:v",
            "h264_cuvid",
            "-i",
            video,
            "-an",
            "-frames:v",
            str(frames),
            "-vf",
            f"scale_cuda={pw}:{ph},hwdownload,format=nv12",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "nv12",
            "pipe:1",
        ]
        bpf = pw * ph + (pw * ph) // 2
        meta = {
            "decode_backend": "cuda_nvdec_h264_cuvid",
            "final_width": pw,
            "final_height": ph,
            "final_pix_fmt": "nv12",
            "gpu_scale_width": pw,
            "gpu_scale_height": ph,
            "gpu_scale_pix_fmt": "nv12",
            "hwdownload_pix_fmt": "nv12",
            "cpu_post_scale": False,
            "expected_bytes_per_frame": bpf,
            "experimental": True,
        }
        return cmd, bpf, (pw, ph), "nv12", meta
    if case == "ffmpeg_cuda_yuv444_gray":
        # Path B: GPU scale to proc-res as yuv444p, download, extract Y → gray.
        # Odd dimensions allowed (final transport is gray, not NV12).
        # interp_algo pinned to binary default 0 (see _SCALE_CUDA_INTERP_ALGO_DEFAULT).
        vf = (
            f"scale_cuda={pw}:{ph}:format=yuv444p:"
            f"interp_algo={_SCALE_CUDA_INTERP_ALGO_DEFAULT},"
            f"hwdownload,format=yuv444p,extractplanes=y"
        )
        cmd = common_head + [
            "-hwaccel",
            "cuda",
            "-hwaccel_output_format",
            "cuda",
            "-c:v",
            "h264_cuvid",
            "-i",
            video,
            "-an",
            "-frames:v",
            str(frames),
            "-vf",
            vf,
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "pipe:1",
        ]
        bpf = pw * ph
        meta = {
            "decode_backend": "cuda_nvdec_h264_cuvid",
            "final_width": pw,
            "final_height": ph,
            "final_pix_fmt": "gray",
            "gpu_scale_width": pw,
            "gpu_scale_height": ph,
            "gpu_scale_pix_fmt": "yuv444p",
            "gpu_scale_interp_algo": _SCALE_CUDA_INTERP_ALGO_DEFAULT,
            "gpu_scale_interp_algo_label": _SCALE_CUDA_INTERP_ALGO_LABEL,
            "hwdownload_pix_fmt": "yuv444p",
            "cpu_post_scale": False,
            "cpu_post_scale_algo": None,
            "cpu_scale": False,
            "cpu_scale_algo": None,
            "cpu_gray_conversion": False,
            "plane_extraction": "y",
            "intermediate_width": None,
            "intermediate_height": None,
            "intermediate_pix_fmt": None,
            "expected_bytes_per_frame": bpf,
            "source_color_range": _COLOR_UNSPEC,
            "source_color_space": _COLOR_UNSPEC,
            "source_color_primaries": _COLOR_UNSPEC,
            "source_color_transfer": _COLOR_UNSPEC,
        }
        return cmd, bpf, (pw, ph), "gray", meta
    if case == "ffmpeg_cuda_nv12_416_gray":
        # Path C: even 16:9 intermediate NV12 on GPU, then CPU area scale to proc-res gray.
        mid = even_16x9_intermediate(pw, ph)
        if mid is None:
            raise ValueError(
                f"cannot derive even 16:9 intermediate from proc_res={pw}x{ph}"
            )
        mw, mh = mid
        vf = (
            f"scale_cuda={mw}:{mh}:format=nv12:"
            f"interp_algo={_SCALE_CUDA_INTERP_ALGO_DEFAULT},"
            f"hwdownload,format=nv12,extractplanes=y,"
            f"scale={pw}:{ph}:flags=area"
        )
        cmd = common_head + [
            "-hwaccel",
            "cuda",
            "-hwaccel_output_format",
            "cuda",
            "-c:v",
            "h264_cuvid",
            "-i",
            video,
            "-an",
            "-frames:v",
            str(frames),
            "-vf",
            vf,
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "pipe:1",
        ]
        bpf = pw * ph
        meta = {
            "decode_backend": "cuda_nvdec_h264_cuvid",
            "final_width": pw,
            "final_height": ph,
            "final_pix_fmt": "gray",
            "gpu_scale_width": mw,
            "gpu_scale_height": mh,
            "gpu_scale_pix_fmt": "nv12",
            "gpu_scale_interp_algo": _SCALE_CUDA_INTERP_ALGO_DEFAULT,
            "gpu_scale_interp_algo_label": _SCALE_CUDA_INTERP_ALGO_LABEL,
            "hwdownload_pix_fmt": "nv12",
            "cpu_post_scale": True,
            "cpu_post_scale_algo": "area",
            "intermediate_width": mw,
            "intermediate_height": mh,
            "intermediate_pix_fmt": "nv12",
            "expected_bytes_per_frame": bpf,
            "cpu_scale": True,
            "cpu_scale_algo": "area",
            "cpu_gray_conversion": False,
            "plane_extraction": "y",
            "source_color_range": _COLOR_UNSPEC,
            "source_color_space": _COLOR_UNSPEC,
            "source_color_primaries": _COLOR_UNSPEC,
            "source_color_transfer": _COLOR_UNSPEC,
        }
        return cmd, bpf, (pw, ph), "gray", meta
    if case == "ffmpeg_sw_y_extract":
        # Diagnostic D: SW scale→yuv444p→extractplanes=y (isolate Y-plane vs BGR2GRAY).
        vf = f"scale={pw}:{ph}:flags=area,format=yuv444p,extractplanes=y"
        cmd = common_head + [
            "-i",
            video,
            "-an",
            "-frames:v",
            str(frames),
            "-vf",
            vf,
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "pipe:1",
        ]
        bpf = pw * ph
        meta = {
            "decode_backend": "ffmpeg_software",
            "final_width": pw,
            "final_height": ph,
            "final_pix_fmt": "gray",
            "gpu_scale_width": None,
            "gpu_scale_height": None,
            "gpu_scale_pix_fmt": None,
            "gpu_scale_interp_algo": None,
            "gpu_scale_interp_algo_label": None,
            "hwdownload_pix_fmt": None,
            "cpu_post_scale": True,
            "cpu_post_scale_algo": "area",
            "cpu_scale": True,
            "cpu_scale_algo": "area",
            "cpu_gray_conversion": False,
            "plane_extraction": "y",
            "intermediate_width": pw,
            "intermediate_height": ph,
            "intermediate_pix_fmt": "yuv444p",
            "expected_bytes_per_frame": bpf,
            "source_color_range": _COLOR_UNSPEC,
            "source_color_space": _COLOR_UNSPEC,
            "source_color_primaries": _COLOR_UNSPEC,
            "source_color_transfer": _COLOR_UNSPEC,
            "diagnostic": True,
        }
        return cmd, bpf, (pw, ph), "gray", meta
    if case == "ffmpeg_cuda_yuv444_convert_gray":
        # Diagnostic E: same as B through hwdownload, then explicit format=gray
        # instead of extractplanes=y.
        vf = (
            f"scale_cuda={pw}:{ph}:format=yuv444p:"
            f"interp_algo={_SCALE_CUDA_INTERP_ALGO_DEFAULT},"
            f"hwdownload,format=yuv444p,format=gray"
        )
        cmd = common_head + [
            "-hwaccel",
            "cuda",
            "-hwaccel_output_format",
            "cuda",
            "-c:v",
            "h264_cuvid",
            "-i",
            video,
            "-an",
            "-frames:v",
            str(frames),
            "-vf",
            vf,
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "pipe:1",
        ]
        bpf = pw * ph
        meta = {
            "decode_backend": "cuda_nvdec_h264_cuvid",
            "final_width": pw,
            "final_height": ph,
            "final_pix_fmt": "gray",
            "gpu_scale_width": pw,
            "gpu_scale_height": ph,
            "gpu_scale_pix_fmt": "yuv444p",
            "gpu_scale_interp_algo": _SCALE_CUDA_INTERP_ALGO_DEFAULT,
            "gpu_scale_interp_algo_label": _SCALE_CUDA_INTERP_ALGO_LABEL,
            "hwdownload_pix_fmt": "yuv444p",
            "cpu_post_scale": False,
            "cpu_post_scale_algo": None,
            "cpu_scale": False,
            "cpu_scale_algo": None,
            "cpu_gray_conversion": True,
            "plane_extraction": None,
            "intermediate_width": pw,
            "intermediate_height": ph,
            "intermediate_pix_fmt": "yuv444p",
            "expected_bytes_per_frame": bpf,
            "source_color_range": _COLOR_UNSPEC,
            "source_color_space": _COLOR_UNSPEC,
            "source_color_primaries": _COLOR_UNSPEC,
            "source_color_transfer": _COLOR_UNSPEC,
            "diagnostic": True,
        }
        return cmd, bpf, (pw, ph), "gray", meta
    if case.startswith("ffmpeg_cuda_diag_"):
        # Diagnostic: E path + explicit assumed input matrix/range via CPU scale.
        # colorspace filter rejects odd 400x225 on this FFmpeg build
        # ("Invalid odd size"); scale in_color_matrix/in_range is supported.
        # Mapping: ffmpeg_cuda_diag_{matrix}_{range}
        #   matrix: bt709 | smpte170m
        #   range:  tv | pc   (assumed *input* range; output forced pc/full)
        rest = case[len("ffmpeg_cuda_diag_") :]
        if rest.endswith("_tv"):
            assumed_range = "tv"
            matrix = rest[: -len("_tv")]
        elif rest.endswith("_pc"):
            assumed_range = "pc"
            matrix = rest[: -len("_pc")]
        else:
            raise ValueError(case)
        if matrix not in ("bt709", "smpte170m"):
            raise ValueError(case)
        # Keep output full/pc gray semantic (explicit).
        scale_color = (
            f"scale=in_color_matrix={matrix}:in_range={assumed_range}:"
            f"out_color_matrix={matrix}:out_range=pc"
        )
        vf = (
            f"scale_cuda={pw}:{ph}:format=yuv444p:"
            f"interp_algo={_SCALE_CUDA_INTERP_ALGO_DEFAULT},"
            f"hwdownload,format=yuv444p,"
            f"{scale_color},"
            f"format=gray"
        )
        cmd = common_head + [
            "-hwaccel",
            "cuda",
            "-hwaccel_output_format",
            "cuda",
            "-c:v",
            "h264_cuvid",
            "-i",
            video,
            "-an",
            "-frames:v",
            str(frames),
            "-vf",
            vf,
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "pipe:1",
        ]
        bpf = pw * ph
        meta = {
            "decode_backend": "cuda_nvdec_h264_cuvid",
            "final_width": pw,
            "final_height": ph,
            "final_pix_fmt": "gray",
            "gpu_scale_width": pw,
            "gpu_scale_height": ph,
            "gpu_scale_pix_fmt": "yuv444p",
            "gpu_scale_interp_algo": _SCALE_CUDA_INTERP_ALGO_DEFAULT,
            "gpu_scale_interp_algo_label": _SCALE_CUDA_INTERP_ALGO_LABEL,
            "hwdownload_pix_fmt": "yuv444p",
            "cpu_post_scale": False,
            "cpu_post_scale_algo": None,
            "cpu_scale": True,
            "cpu_scale_algo": "default_sws_with_explicit_matrix_range",
            "cpu_gray_conversion": True,
            "plane_extraction": None,
            "intermediate_width": pw,
            "intermediate_height": ph,
            "intermediate_pix_fmt": "yuv444p",
            "expected_bytes_per_frame": bpf,
            "source_color_range": _COLOR_UNSPEC,
            "source_color_space": _COLOR_UNSPEC,
            "source_color_primaries": _COLOR_UNSPEC,
            "source_color_transfer": _COLOR_UNSPEC,
            "assumed_input_matrix": matrix,
            "assumed_input_range": assumed_range,
            "output_matrix": matrix,
            "output_range": "pc",
            "conversion_filter": "scale",
            "conversion_filter_args": scale_color,
            "diagnostic": True,
            "note": (
                "assumed_* are diagnostic hypotheses; source stream color tags "
                "are unspecified. colorspace filter unsupported at odd 400x225 "
                "on this binary; using scale in_color_matrix/in_range instead."
            ),
        }
        return cmd, bpf, (pw, ph), "gray", meta
    if case == "ffmpeg_cuda_cpu_area_gray":
        # Diagnostic F: CUDA decode + hwdownload nv12 + CPU area scale + format=gray.
        # Isolates CUVID/NVDEC backend from scale_cuda (no GPU scale, no extractplanes).
        vf = "hwdownload,format=nv12,scale=%d:%d:flags=area,format=gray" % (pw, ph)
        cmd = common_head + [
            "-hwaccel",
            "cuda",
            "-hwaccel_output_format",
            "cuda",
            "-c:v",
            "h264_cuvid",
            "-i",
            video,
            "-an",
            "-frames:v",
            str(frames),
            "-vf",
            vf,
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "pipe:1",
        ]
        bpf = pw * ph
        meta = {
            "decode_backend": "cuda_nvdec_h264_cuvid",
            "final_width": pw,
            "final_height": ph,
            "final_pix_fmt": "gray",
            "gpu_scale_width": None,
            "gpu_scale_height": None,
            "gpu_scale_pix_fmt": None,
            "gpu_scale_interp_algo": None,
            "gpu_scale_interp_algo_label": None,
            "hwdownload_pix_fmt": "nv12",
            "scale_backend": "cpu_swscale",
            "cpu_post_scale": True,
            "cpu_post_scale_algo": "area",
            "cpu_scale": True,
            "cpu_scale_algo": "area",
            "cpu_gray_conversion": True,
            "plane_extraction": None,
            "input_range_metadata": "tv_from_cuda_showinfo_when_probed",
            "intermediate_width": None,
            "intermediate_height": None,
            "intermediate_pix_fmt": "nv12",
            "expected_bytes_per_frame": bpf,
            "source_color_range": _COLOR_UNSPEC,
            "source_color_space": _COLOR_UNSPEC,
            "source_color_primaries": _COLOR_UNSPEC,
            "source_color_transfer": _COLOR_UNSPEC,
            "diagnostic": True,
        }
        return cmd, bpf, (pw, ph), "gray", meta
    if case.startswith("ffmpeg_cuda_e_interp_"):
        # Diagnostic E variants: scale_cuda interp_algo=N then format=gray.
        # Mapping on this binary (ffmpeg -h filter=scale_cuda): default 0 (unnamed),
        # nearest=1, bilinear=2, bicubic=3, lanczos=4.
        suffix = case[len("ffmpeg_cuda_e_interp_") :]
        try:
            n_algo = int(suffix)
        except ValueError as exc:
            raise ValueError(case) from exc
        if n_algo not in _SCALE_CUDA_INTERP_LABELS:
            raise ValueError(case)
        label = _SCALE_CUDA_INTERP_LABELS[n_algo]
        vf = (
            f"scale_cuda={pw}:{ph}:format=yuv444p:interp_algo={n_algo},"
            f"hwdownload,format=yuv444p,format=gray"
        )
        cmd = common_head + [
            "-hwaccel",
            "cuda",
            "-hwaccel_output_format",
            "cuda",
            "-c:v",
            "h264_cuvid",
            "-i",
            video,
            "-an",
            "-frames:v",
            str(frames),
            "-vf",
            vf,
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "pipe:1",
        ]
        bpf = pw * ph
        meta = {
            "decode_backend": "cuda_nvdec_h264_cuvid",
            "final_width": pw,
            "final_height": ph,
            "final_pix_fmt": "gray",
            "gpu_scale_width": pw,
            "gpu_scale_height": ph,
            "gpu_scale_pix_fmt": "yuv444p",
            "gpu_scale_interp_algo": n_algo,
            "gpu_scale_interp_algo_label": label,
            "hwdownload_pix_fmt": "yuv444p",
            "scale_backend": "scale_cuda",
            "cpu_post_scale": False,
            "cpu_post_scale_algo": None,
            "cpu_scale": False,
            "cpu_scale_algo": None,
            "cpu_gray_conversion": True,
            "plane_extraction": None,
            "intermediate_width": pw,
            "intermediate_height": ph,
            "intermediate_pix_fmt": "yuv444p",
            "expected_bytes_per_frame": bpf,
            "source_color_range": _COLOR_UNSPEC,
            "source_color_space": _COLOR_UNSPEC,
            "source_color_primaries": _COLOR_UNSPEC,
            "source_color_transfer": _COLOR_UNSPEC,
            "diagnostic": True,
            "note": (
                "interp_algo labels from this binary's scale_cuda help; "
                "0 has no named alias on this build."
            ),
        }
        return cmd, bpf, (pw, ph), "gray", meta
    raise ValueError(case)


def build_ffmpeg_cmd_with_meta(
    case: str,
    ffmpeg: str,
    video: str,
    frames: int,
    proc_res: tuple[int, int],
    src_wh: tuple[int, int],
) -> tuple[list[str], int, tuple[int, int], str, dict[str, Any]]:
    if case == "ffmpeg_sw_bgr":
        sw, sh = src_wh
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            video,
            "-an",
            "-frames:v",
            str(frames),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "pipe:1",
        ]
        meta = {
            "decode_backend": "ffmpeg_software",
            "final_width": sw,
            "final_height": sh,
            "final_pix_fmt": "bgr24",
            "gpu_scale_width": None,
            "gpu_scale_height": None,
            "gpu_scale_pix_fmt": None,
            "hwdownload_pix_fmt": None,
            "cpu_post_scale": False,
            "expected_bytes_per_frame": sw * sh * 3,
        }
        return cmd, sw * sh * 3, (sw, sh), "bgr24", meta
    return build_ffmpeg_cmd(case, ffmpeg, video, frames, proc_res)


def _stderr_tail_text(stderr_file, limit: int = _STDERR_TAIL_BYTES) -> str:
    """Read at most the last `limit` bytes from a TemporaryFile-like object."""
    try:
        stderr_file.seek(0, os.SEEK_END)
        size = stderr_file.tell()
        start = max(0, size - limit)
        stderr_file.seek(start)
        data = stderr_file.read()
    except Exception as exc:
        return f"(stderr read failed: {exc})"
    if isinstance(data, str):
        text = data
    else:
        text = data.decode("utf-8", errors="replace")
    return text.strip()


def _force_stop_proc(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    if proc.poll() is None:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=10)
    except Exception:
        pass
    if proc.stdout is not None:
        try:
            proc.stdout.close()
        except Exception:
            pass


def run_ffmpeg_pipe(
    cmd: list[str],
    frames: int,
    bytes_per_frame: int,
    out_wh: tuple[int, int],
    pix_layout: str,
    sample_gpu: bool,
    timeout_s: float = _DEFAULT_PIPE_TIMEOUT_S,
) -> RunMetrics:
    """Stream FFmpeg rawvideo stdout without retaining frame payloads.

    Memory is O(chunk_size). stderr goes to a TemporaryFile (no pipe deadlock).
    A single wall-clock deadline covers Popen, stdout drain, process exit, and
    reader-thread join.

    Always populates process_returncode / bytes_received / expected_bytes /
    complete_frames / trailing_bytes explicitly (never leave them to status inference).
    """
    y_plane = out_wh[0] * out_wh[1] if pix_layout in ("gray", "nv12") else None
    expected_total = frames * bytes_per_frame if bytes_per_frame > 0 else 0

    def _acct(
        wall: float,
        frames_actual: int,
        fps: float,
        mspf: float,
        *,
        rc: int | None,
        bytes_received: int,
        complete: int,
        trailing: int,
        peak=None,
        gpu=None,
        error: str | None = None,
    ) -> RunMetrics:
        return RunMetrics(
            wall_s=wall,
            frames_actual=frames_actual,
            fps=fps,
            ms_per_frame=mspf,
            peak_rss_bytes=peak,
            gpu_decoder_util=gpu,
            out_width=out_wh[0],
            out_height=out_wh[1],
            y_plane_bytes=y_plane,
            process_returncode=rc,
            bytes_received=bytes_received,
            expected_bytes=expected_total,
            complete_frames=complete,
            trailing_bytes=trailing,
            error=error,
        )

    if bytes_per_frame <= 0:
        return _acct(
            0.0, 0, 0.0, 0.0,
            rc=None, bytes_received=0, complete=0, trailing=0,
            error="bytes_per_frame invalid",
        )

    deadline = time.perf_counter() + max(1.0, float(timeout_s))

    def remaining() -> float:
        return deadline - time.perf_counter()

    stderr_file = tempfile.TemporaryFile()
    proc: subprocess.Popen | None = None
    reader: threading.Thread | None = None
    sampler: ResourceSampler | None = None
    bytes_received = 0
    reader_error: list[BaseException] = []
    t0 = time.perf_counter()
    rc: int | None = None

    def read_stdout(stdout) -> None:
        nonlocal bytes_received
        try:
            while True:
                if remaining() <= 0:
                    return
                chunk = stdout.read(_STDOUT_CHUNK)
                if not chunk:
                    break
                bytes_received += len(chunk)
        except BaseException as exc:
            reader_error.append(exc)

    try:
        if remaining() <= 0:
            return _acct(
                0.0, 0, 0.0, 0.0,
                rc=None, bytes_received=0, complete=0, trailing=0,
                error="timeout before Popen",
            )

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                stdin=subprocess.DEVNULL,
            )
        except OSError as exc:
            return _acct(
                0.0, 0, 0.0, 0.0,
                rc=None, bytes_received=0, complete=0, trailing=0,
                error=f"Popen failed: {exc}",
            )

        assert proc.stdout is not None
        sampler = ResourceSampler(proc.pid, sample_gpu=sample_gpu)
        sampler.start()
        t0 = time.perf_counter()

        reader = threading.Thread(
            target=read_stdout,
            args=(proc.stdout,),
            name="ffmpeg-stdout-reader",
            daemon=True,
        )
        reader.start()

        timed_out = False
        while reader.is_alive():
            rem = remaining()
            if rem <= 0:
                timed_out = True
                break
            reader.join(timeout=min(0.2, rem))

        if timed_out or remaining() <= 0:
            _force_stop_proc(proc)
            if reader is not None:
                reader.join(timeout=5.0)
            if sampler is not None:
                sampler.stop()
            rc = proc.returncode if proc is not None else None
            stderr = _stderr_tail_text(stderr_file)
            complete = bytes_received // bytes_per_frame
            trailing = bytes_received % bytes_per_frame
            return _acct(
                time.perf_counter() - t0, complete, 0.0, 0.0,
                rc=rc, bytes_received=bytes_received, complete=complete, trailing=trailing,
                peak=sampler.peak_rss if sampler else None,
                gpu=sampler.gpu_decoder_util_mean() if sampler else None,
                error=(
                    f"timeout after {timeout_s:.1f}s "
                    f"(bytes_received={bytes_received} complete_frames={complete} "
                    f"trailing_bytes={trailing}); stderr={stderr[:800] or '(empty)'}"
                ),
            )

        if reader_error:
            _force_stop_proc(proc)
            if reader is not None:
                reader.join(timeout=5.0)
            if sampler is not None:
                sampler.stop()
            rc = proc.returncode if proc is not None else None
            stderr = _stderr_tail_text(stderr_file)
            complete = bytes_received // bytes_per_frame
            trailing = bytes_received % bytes_per_frame
            return _acct(
                time.perf_counter() - t0, complete, 0.0, 0.0,
                rc=rc, bytes_received=bytes_received, complete=complete, trailing=trailing,
                peak=sampler.peak_rss if sampler else None,
                error=(
                    f"stdout reader failed: {reader_error[0]!r}; "
                    f"stderr={stderr[:400] or '(empty)'}"
                ),
            )

        rem = remaining()
        try:
            if rem <= 0:
                raise subprocess.TimeoutExpired(cmd, timeout_s)
            rc = proc.wait(timeout=rem)
        except subprocess.TimeoutExpired:
            _force_stop_proc(proc)
            if reader is not None:
                reader.join(timeout=5.0)
            if sampler is not None:
                sampler.stop()
            rc = proc.returncode if proc is not None else None
            stderr = _stderr_tail_text(stderr_file)
            complete = bytes_received // bytes_per_frame
            trailing = bytes_received % bytes_per_frame
            return _acct(
                time.perf_counter() - t0, complete, 0.0, 0.0,
                rc=rc, bytes_received=bytes_received, complete=complete, trailing=trailing,
                peak=sampler.peak_rss if sampler else None,
                gpu=sampler.gpu_decoder_util_mean() if sampler else None,
                error=(
                    f"ffmpeg wait timed out after {timeout_s:.1f}s "
                    f"(bytes_received={bytes_received} complete_frames={complete} "
                    f"trailing_bytes={trailing}); stderr={stderr[:800] or '(empty)'}"
                ),
            )

        if reader is not None and reader.is_alive():
            reader.join(timeout=max(0.01, remaining()))
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except Exception:
                pass

        wall = time.perf_counter() - t0
        if sampler is not None:
            sampler.stop()
        stderr = _stderr_tail_text(stderr_file)
        complete = bytes_received // bytes_per_frame
        trailing = bytes_received % bytes_per_frame
        peak = sampler.peak_rss if sampler else None
        gpu = sampler.gpu_decoder_util_mean() if sampler else None

        if rc != 0:
            return _acct(
                wall, complete, 0.0, 0.0,
                rc=rc, bytes_received=bytes_received, complete=complete, trailing=trailing,
                peak=peak, gpu=gpu,
                error=f"ffmpeg rc={rc}: {stderr[:800] or '(empty stderr)'}",
            )

        if complete != frames or trailing != 0:
            return _acct(
                wall, complete, 0.0, 0.0,
                rc=rc, bytes_received=bytes_received, complete=complete, trailing=trailing,
                peak=peak, gpu=gpu,
                error=(
                    f"frame/byte mismatch: bytes_received={bytes_received} "
                    f"bpf={bytes_per_frame} complete_frames={complete} "
                    f"expected={frames} trailing_bytes={trailing}; "
                    f"stderr={stderr[:400] or '(empty)'}"
                ),
            )

        fps = (complete / wall) if wall > 0 else 0.0
        mspf = (wall * 1000.0 / complete) if complete else 0.0
        return _acct(
            wall, complete, fps, mspf,
            rc=rc, bytes_received=bytes_received, complete=complete, trailing=trailing,
            peak=peak, gpu=gpu,
        )
    except Exception as exc:
        _force_stop_proc(proc)
        if reader is not None:
            reader.join(timeout=5.0)
        if sampler is not None:
            sampler.stop()
        complete = bytes_received // bytes_per_frame if bytes_per_frame else 0
        trailing = bytes_received % bytes_per_frame if bytes_per_frame else 0
        rc = proc.returncode if proc is not None else None
        return _acct(
            time.perf_counter() - t0, complete, 0.0, 0.0,
            rc=rc, bytes_received=bytes_received, complete=complete, trailing=trailing,
            error=f"pipe run failed: {exc!r}",
        )
    finally:
        try:
            stderr_file.close()
        except Exception:
            pass



# ---------------------------------------------------------------------------
# Case orchestration
# ---------------------------------------------------------------------------


def summarize_runs(
    case: str,
    status: str,
    reason: str | None,
    frames_expected: int,
    metrics: list[RunMetrics],
    command: list[str] | None,
    notes: str | None = None,
    path_meta: dict[str, Any] | None = None,
) -> CaseResult:
    ok_metrics = [m for m in metrics if not m.error]
    if status == "ok" and not ok_metrics:
        status = "failed"
        reason = reason or (metrics[-1].error if metrics else "no successful runs")

    def _fin(xs: list[float]) -> float | None:
        return None if not xs else float(xs[0] if len(xs) == 1 else _median(xs))

    fps_list = [m.fps for m in ok_metrics]
    mspf_list = [m.ms_per_frame for m in ok_metrics]
    frames_actual = ok_metrics[-1].frames_actual if ok_metrics else (
        metrics[-1].frames_actual if metrics else None
    )

    # Small-N policy: with fewer than _P95_MIN_SAMPLES successful runs, report
    # median/min/max only. Never label max-of-5 as a stable P95.
    p95_val: float | None = None
    if len(mspf_list) >= _P95_MIN_SAMPLES:
        p95_val = float(_p95(mspf_list))

    last = None
    if ok_metrics:
        last = ok_metrics[-1]
    elif metrics:
        last = metrics[-1]

    return CaseResult(
        case=case,
        status=status,
        skip_or_error_reason=reason,
        frames_expected=frames_expected,
        frames_actual=frames_actual,
        median_fps=_fin(fps_list) if fps_list else None,
        median_ms_per_frame=_fin(mspf_list) if mspf_list else None,
        min_fps=float(min(fps_list)) if fps_list else None,
        max_fps=float(max(fps_list)) if fps_list else None,
        min_ms_per_frame=float(min(mspf_list)) if mspf_list else None,
        max_ms_per_frame=float(max(mspf_list)) if mspf_list else None,
        p95_ms_per_frame=p95_val,
        runs=[asdict(m) for m in metrics],
        command=command,
        notes=notes,
        path_meta=path_meta,
        process_returncode=last.process_returncode if last else None,
        bytes_received=last.bytes_received if last else None,
        expected_bytes=last.expected_bytes if last else None,
        complete_frames=last.complete_frames if last else None,
        trailing_bytes=last.trailing_bytes if last else None,
    )


def _cuda_gate(
    case: str,
    ffinfo: FFmpegInfo,
    frames: int,
    *,
    need_extractplanes: bool,
) -> CaseResult | None:
    """Return a skipped/failed CaseResult if CUDA path cannot run; else None."""
    missing = _cuda_component_gaps(ffinfo, need_extractplanes=need_extractplanes)
    if missing:
        return summarize_runs(
            case,
            "skipped",
            "missing components: " + ", ".join(missing),
            frames,
            [],
            None,
            notes="hardware case not available on this ffmpeg build",
        )
    if ffinfo.cuda_runtime_ok is False and ffinfo.cuda_runtime_reason and (
        ffinfo.cuda_runtime_reason.startswith("missing components")
    ):
        return summarize_runs(
            case,
            "skipped",
            ffinfo.cuda_runtime_reason,
            frames,
            [],
            None,
            notes="hardware case not available on this ffmpeg build",
        )
    if ffinfo.cuda_runtime_ok is not True:
        reason = ffinfo.cuda_runtime_reason or "cuda runtime probe not ok"
        return summarize_runs(
            case,
            "failed",
            reason,
            frames,
            [],
            None,
            notes="components may be listed but runtime init failed",
        )
    return None


def execute_case(
    case: str,
    video: str,
    meta: dict[str, Any],
    ffmpeg: str,
    ffinfo: FFmpegInfo,
    frames: int,
    proc_res: tuple[int, int],
    warmup: int,
    repeat: int,
) -> CaseResult:
    notes = None
    command: list[str] | None = None
    path_meta: dict[str, Any] | None = None

    if case.startswith("opencv_"):
        def once() -> RunMetrics:
            return run_opencv(case, video, frames, proc_res)

        for _ in range(max(0, warmup)):
            m = once()
            if m.error:
                return summarize_runs(case, "failed", m.error, frames, [m], None)
        runs = []
        for _ in range(repeat):
            runs.append(once())
        # size check on last good
        for m in runs:
            if m.error:
                return summarize_runs(case, "failed", m.error, frames, runs, None)
            if m.frames_actual != frames:
                return summarize_runs(
                    case,
                    "failed",
                    f"frames_actual={m.frames_actual} != expected={frames}",
                    frames,
                    runs,
                    None,
                )
            if case != "opencv_read":
                if m.out_width != proc_res[0] or m.out_height != proc_res[1]:
                    return summarize_runs(
                        case,
                        "failed",
                        f"unexpected size {m.out_width}x{m.out_height}",
                        frames,
                        runs,
                        None,
                    )
        return summarize_runs(case, "ok", None, frames, runs, None)

    # FFmpeg software / hardware
    if case == "ffmpeg_sw_bgr":
        cmd, bpf, wh, layout, path_meta = build_ffmpeg_cmd_with_meta(
            case, ffmpeg, video, frames, proc_res, (meta["width"], meta["height"])
        )
        command = cmd
        notes = "software decode to full-resolution BGR24 rawvideo"
    elif case == "ffmpeg_sw_gray":
        cmd, bpf, wh, layout, path_meta = build_ffmpeg_cmd_with_meta(
            case, ffmpeg, video, frames, proc_res, (meta["width"], meta["height"])
        )
        command = cmd
        notes = (
            "software decode + scale=proc_res flags=area + gray; "
            "not claimed pixel-identical to OpenCV INTER_AREA+BGR2GRAY"
        )
    elif case == "ffmpeg_cuda_yuv444_gray":
        gate = _cuda_gate(case, ffinfo, frames, need_extractplanes=True)
        if gate is not None:
            return gate
        cmd, bpf, wh, layout, path_meta = build_ffmpeg_cmd_with_meta(
            case, ffmpeg, video, frames, proc_res, (meta["width"], meta["height"])
        )
        command = cmd
        notes = (
            "CUDA/NVDEC + scale_cuda yuv444p @ proc-res + hwdownload + extractplanes=y; "
            "final gray; no CPU resize; not claimed equal to OpenCV gray"
        )
    elif case == "ffmpeg_cuda_nv12_416_gray":
        gate = _cuda_gate(case, ffinfo, frames, need_extractplanes=True)
        if gate is not None:
            return gate
        mid = even_16x9_intermediate(proc_res[0], proc_res[1])
        if mid is None:
            return summarize_runs(
                case,
                "skipped",
                (
                    f"cannot derive even strict-16:9 intermediate from "
                    f"proc_res={proc_res[0]}x{proc_res[1]} "
                    f"(require integer 16:9; no silent stretch)"
                ),
                frames,
                [],
                None,
                notes="intermediate size derivation failed; final size not altered",
            )
        cmd, bpf, wh, layout, path_meta = build_ffmpeg_cmd_with_meta(
            case, ffmpeg, video, frames, proc_res, (meta["width"], meta["height"])
        )
        command = cmd
        notes = (
            f"CUDA/NVDEC + scale_cuda nv12 @ {mid[0]}x{mid[1]} + hwdownload + "
            f"extractplanes=y + CPU scale area to {proc_res[0]}x{proc_res[1]} gray; "
            "not claimed equal to OpenCV gray"
        )
    elif case == "ffmpeg_cuda_nv12_direct":
        pw, ph = proc_res
        # Experimental: direct NV12 at proc-res; odd dims skipped.
        if (pw % 2) != 0 or (ph % 2) != 0:
            return summarize_runs(
                case,
                "skipped",
                (
                    f"NV12 requires even width and height; "
                    f"requested proc_res={pw}x{ph}"
                ),
                frames,
                [],
                None,
                notes="experimental direct NV12; odd proc-res not auto-fixed",
                path_meta={"experimental": True, "final_pix_fmt": "nv12"},
            )
        gate = _cuda_gate(case, ffinfo, frames, need_extractplanes=False)
        if gate is not None:
            return gate
        cmd, bpf, wh, layout, path_meta = build_ffmpeg_cmd_with_meta(
            case, ffmpeg, video, frames, proc_res, (meta["width"], meta["height"])
        )
        command = cmd
        notes = (
            "EXPERIMENTAL: CUDA/NVDEC + scale_cuda + hwdownload + raw nv12 @ proc-res; "
            "not in default case list"
        )
    elif case in _PASSTHROUGH_CASE_BASE:
        # A_PT / F_PT / E2_PT: same graphs as base cases + output -fps_mode passthrough.
        base = _PASSTHROUGH_CASE_BASE[case]
        need_extract = False
        if base.startswith("ffmpeg_cuda_"):
            gate = _cuda_gate(case, ffinfo, frames, need_extractplanes=need_extract)
            if gate is not None:
                return gate
        cmd, bpf, wh, layout, path_meta = build_ffmpeg_cmd_with_meta(
            case, ffmpeg, video, frames, proc_res, (meta["width"], meta["height"])
        )
        command = cmd
        notes = (
            f"passthrough timeline variant of {base}; "
            "output -fps_mode passthrough only; graph otherwise identical"
        )
    else:
        return summarize_runs(case, "skipped", f"unknown case {case}", frames, [], None)

    if command is not None and path_meta is not None:
        path_meta = dict(path_meta)
        path_meta["command"] = command

    sample_gpu = case in _CUDA_GRAY_CASES
    # Scale total pipe wall budget with frame count (default 180s is smoke-only).
    pipe_timeout_s = max(_DEFAULT_PIPE_TIMEOUT_S, 120.0 + float(frames) * 0.12)

    def once() -> RunMetrics:
        return run_ffmpeg_pipe(
            cmd,
            frames,
            bpf,
            wh,
            layout,
            sample_gpu=sample_gpu,
            timeout_s=pipe_timeout_s,
        )

    for _ in range(max(0, warmup)):
        m = once()
        if m.error:
            return summarize_runs(
                case, "failed", m.error, frames, [m], command, notes, path_meta
            )

    runs: list[RunMetrics] = []
    for _ in range(repeat):
        m = once()
        runs.append(m)
        if m.error:
            return summarize_runs(
                case, "failed", m.error, frames, runs, command, notes, path_meta
            )
        if m.frames_actual != frames:
            return summarize_runs(
                case,
                "failed",
                f"frames_actual={m.frames_actual} != expected={frames}",
                frames,
                runs,
                command,
                notes,
                path_meta,
            )
    return summarize_runs(case, "ok", None, frames, runs, command, notes, path_meta)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_table(results: list[CaseResult]) -> None:
    headers = [
        "case",
        "status",
        "median_fps",
        "min_fps",
        "max_fps",
        "median_ms/f",
        "min_ms/f",
        "max_ms/f",
        "p95_ms/f",
        "frames_exp",
        "frames_act",
        "reason",
    ]
    rows = []
    for r in results:
        rows.append(
            [
                r.case,
                r.status,
                f"{r.median_fps:.2f}" if r.median_fps is not None else "-",
                f"{r.min_fps:.2f}" if r.min_fps is not None else "-",
                f"{r.max_fps:.2f}" if r.max_fps is not None else "-",
                f"{r.median_ms_per_frame:.3f}" if r.median_ms_per_frame is not None else "-",
                f"{r.min_ms_per_frame:.3f}" if r.min_ms_per_frame is not None else "-",
                f"{r.max_ms_per_frame:.3f}" if r.max_ms_per_frame is not None else "-",
                f"{r.p95_ms_per_frame:.3f}" if r.p95_ms_per_frame is not None else "-",
                str(r.frames_expected),
                str(r.frames_actual) if r.frames_actual is not None else "-",
                (r.skip_or_error_reason or "")[:60],
            ]
        )
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    fmt = "  ".join(f"{{:{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*["-" * w for w in widths]))
    for row in rows:
        print(fmt.format(*row))
    # Path metadata and explicit FFmpeg accounting (when present).
    for r in results:
        if r.path_meta:
            pm = {k: v for k, v in r.path_meta.items() if k != "command"}
            print(f"path_meta[{r.case}]: {pm}")
        if any(
            x is not None
            for x in (
                r.process_returncode,
                r.bytes_received,
                r.expected_bytes,
                r.complete_frames,
                r.trailing_bytes,
            )
        ):
            print(
                f"exec[{r.case}]: returncode={r.process_returncode} "
                f"bytes_received={r.bytes_received} expected_bytes={r.expected_bytes} "
                f"complete_frames={r.complete_frames} trailing_bytes={r.trailing_bytes}"
            )
        if r.verify:
            print(f"verify[{r.case}]: {r.verify}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_csv_cases(raw: str) -> list[str]:
    """Split comma-separated case names; drop empties; preserve order; de-dupe."""
    seen: set[str] = set()
    out: list[str] = []
    for part in raw.split(","):
        name = part.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _verify_one_candidate(
    *,
    reference: str,
    candidate: str,
    video_path: str,
    ffmpeg_path: str,
    verify_frames: int,
    proc_res: tuple[int, int],
    cmd: list[str],
    bpf: int,
    path_meta: dict[str, Any],
    timeout_s: float,
    ref_cmd: list[str] | None = None,
) -> dict[str, Any]:
    """Lockstep reference vs one candidate; return JSON-serializable verify dict.

    reference == 'opencv_gray' uses OpenCV iterator; any other allowed reference
    is an FFmpeg gray producer opened via ref_cmd (must be provided).
    Always closes both generators before returning or raising.
    """
    if reference == "opencv_gray":
        ref_iter = iter_opencv_gray_frames(
            video_path, verify_frames, proc_res[0], proc_res[1]
        )
    else:
        if not ref_cmd:
            raise RuntimeError(
                f"ref_cmd required for FFmpeg reference {reference!r}"
            )
        ref_iter = iter_ffmpeg_gray_frames(
            ffmpeg_path,
            video_path,
            verify_frames,
            proc_res[0],
            proc_res[1],
            timeout_s=timeout_s,
            command=ref_cmd,
        )
    cand_iter = iter_ffmpeg_gray_frames(
        ffmpeg_path,
        video_path,
        verify_frames,
        proc_res[0],
        proc_res[1],
        timeout_s=timeout_s,
        command=cmd,
    )

    ref_frames = 0
    cand_frames = 0
    ref_crc = 0
    cand_crc = 0
    total_candidate_bytes = 0
    partial_frame_bytes = 0
    ffmpeg_returncode: int | None = None
    stderr_tail = ""
    frame_sync_ok = False

    hist = np.zeros(256, dtype=np.uint64)
    total_abs_error = 0
    total_pixels = 0
    global_max_abs_error = 0
    count_gt_1 = 0
    count_gt_2 = 0
    count_gt_4 = 0
    count_gt_8 = 0
    count_gt_16 = 0
    count_gt_32 = 0
    count_gt_64 = 0
    count_gt_128 = 0
    signed_accum = _new_signed_accum()

    frame_stats: list[dict[str, Any]] = []
    highest_mae_frame_index = -1
    highest_mae_value = -1.0
    highest_max_error_frame_index = -1
    highest_max_error_value = -1
    highest_max_error_bbox_gt_8: dict[str, int] | None = None
    top_error_pixels: list[dict[str, int]] = []
    fail_msg: str | None = None

    try:
        for frame_index in range(verify_frames):
            try:
                reference_frame = next(ref_iter)
            except StopIteration:
                fail_msg = (
                    f"reference exhausted early at frame_index={frame_index}; "
                    f"requested_frames={verify_frames} actual_frames={ref_frames}"
                )
                break
            except RuntimeError as exc:
                fail_msg = f"reference read failed: {exc}"
                break

            try:
                candidate_frame = next(cand_iter)
            except StopIteration:
                fail_msg = (
                    f"candidate exhausted early at frame_index={frame_index}; "
                    f"requested_frames={verify_frames} actual_frames={cand_frames}"
                )
                break
            except FFmpegGrayFrameError as exc:
                partial_frame_bytes = exc.bytes_in_partial_frame
                ffmpeg_returncode = exc.returncode
                stderr_tail = exc.stderr_tail
                fail_msg = f"candidate read failed: {exc}"
                break
            except RuntimeError as exc:
                fail_msg = f"candidate read failed: {exc}"
                break

            if reference_frame.shape != (proc_res[1], proc_res[0]):
                fail_msg = (
                    f"reference shape {reference_frame.shape} != "
                    f"expected {(proc_res[1], proc_res[0])} at frame_index={frame_index}"
                )
                break
            if candidate_frame.shape != (proc_res[1], proc_res[0]):
                fail_msg = (
                    f"candidate shape {candidate_frame.shape} != "
                    f"expected {(proc_res[1], proc_res[0])} at frame_index={frame_index}"
                )
                break
            if reference_frame.dtype != np.uint8 or candidate_frame.dtype != np.uint8:
                fail_msg = (
                    f"dtype mismatch at frame_index={frame_index}: "
                    f"ref={reference_frame.dtype} cand={candidate_frame.dtype}"
                )
                break
            if (
                not reference_frame.flags["C_CONTIGUOUS"]
                or not candidate_frame.flags["C_CONTIGUOUS"]
            ):
                fail_msg = f"non-C-contiguous frame at frame_index={frame_index}"
                break
            if reference_frame.shape != candidate_frame.shape:
                fail_msg = (
                    f"ref/cand shape diverge at frame_index={frame_index}: "
                    f"ref={reference_frame.shape} cand={candidate_frame.shape}"
                )
                break

            try:
                m = _compare_gray_pair(
                    reference_frame,
                    candidate_frame,
                    hist,
                    frame_index=frame_index,
                    top_k_pixels=20,
                )
                _update_signed_accum(signed_accum, reference_frame, candidate_frame)
            except Exception as exc:
                fail_msg = (
                    f"pixel compare failed at frame_index={frame_index}: "
                    f"{type(exc).__name__}: {exc}"
                )
                break

            total_abs_error += int(m["total_abs_error"])
            total_pixels += int(m["pixels"])
            max_abs = int(m["max_abs_error"])
            if max_abs > global_max_abs_error:
                global_max_abs_error = max_abs
            count_gt_1 += int(m["count_gt_1"])
            count_gt_2 += int(m["count_gt_2"])
            count_gt_4 += int(m["count_gt_4"])
            count_gt_8 += int(m["count_gt_8"])
            count_gt_16 += int(m["count_gt_16"])
            count_gt_32 += int(m["count_gt_32"])
            count_gt_64 += int(m["count_gt_64"])
            count_gt_128 += int(m["count_gt_128"])

            mae = float(m["mae"])
            p95 = float(m["pixel_p95_abs_error"])
            frame_stats.append(
                {
                    "frame_index": int(frame_index),
                    "mae": mae,
                    "max_abs_error": max_abs,
                    "pixel_p95_abs_error": p95,
                    "count_gt_8": int(m["count_gt_8"]),
                    "ratio_gt_8": float(m["ratio_gt_8"]),
                    "count_gt_16": int(m["count_gt_16"]),
                    "count_gt_32": int(m["count_gt_32"]),
                    "count_gt_64": int(m["count_gt_64"]),
                    "count_gt_128": int(m["count_gt_128"]),
                    "bbox_gt_8": m["bbox_gt_8"],
                }
            )
            if mae > highest_mae_value:
                highest_mae_value = mae
                highest_mae_frame_index = frame_index
            if max_abs > highest_max_error_value:
                highest_max_error_value = max_abs
                highest_max_error_frame_index = frame_index
                highest_max_error_bbox_gt_8 = m["bbox_gt_8"]

            top_error_pixels = _merge_top_error_pixels(
                top_error_pixels, m["top_pixels"], k=20
            )

            ref_crc = zlib.crc32(reference_frame.data, ref_crc)
            cand_crc = zlib.crc32(candidate_frame.data, cand_crc)
            total_candidate_bytes += int(candidate_frame.nbytes)
            ref_frames += 1
            cand_frames += 1
            del reference_frame, candidate_frame, m

        if fail_msg is None:
            try:
                next(cand_iter)
                fail_msg = "candidate produced extra frame beyond verify_frames"
            except StopIteration:
                ffmpeg_returncode = 0
                partial_frame_bytes = 0
                stderr_tail = ""
            except FFmpegGrayFrameError as exc:
                partial_frame_bytes = exc.bytes_in_partial_frame
                ffmpeg_returncode = exc.returncode
                stderr_tail = exc.stderr_tail
                fail_msg = f"candidate finalize failed: {exc}"

        if fail_msg is None:
            try:
                next(ref_iter)
                fail_msg = "reference produced extra frame beyond verify_frames"
            except StopIteration:
                pass
            except RuntimeError as exc:
                fail_msg = f"reference finalize failed: {exc}"

        if fail_msg is None:
            frame_sync_ok = True
    finally:
        try:
            ref_iter.close()
        except Exception:
            pass
        try:
            cand_iter.close()
        except Exception:
            pass

    if fail_msg is not None:
        raise RuntimeError(fail_msg)
    if not frame_sync_ok:
        raise RuntimeError("frame_sync_ok is False after lockstep")
    if ref_frames != verify_frames or cand_frames != verify_frames:
        raise RuntimeError(
            f"frame count mismatch: requested={verify_frames} "
            f"ref={ref_frames} cand={cand_frames}"
        )
    if total_pixels <= 0:
        raise RuntimeError("no pixels compared")

    width, height = int(proc_res[0]), int(proc_res[1])
    global_mae = float(total_abs_error) / float(total_pixels)
    global_pixel_p95 = _p95_from_abs_error_hist(hist)
    counts = {
        "count_gt_1": int(count_gt_1),
        "count_gt_2": int(count_gt_2),
        "count_gt_4": int(count_gt_4),
        "count_gt_8": int(count_gt_8),
        "count_gt_16": int(count_gt_16),
        "count_gt_32": int(count_gt_32),
        "count_gt_64": int(count_gt_64),
        "count_gt_128": int(count_gt_128),
    }
    ratios = {
        f"ratio_gt_{t}": float(counts[f"count_gt_{t}"]) / float(total_pixels)
        for t in (1, 2, 4, 8, 16, 32, 64, 128)
    }
    cons_failures = _verify_consistency_checks(
        hist=hist,
        total_pixels=int(total_pixels),
        total_abs_error=int(total_abs_error),
        frames_compared=int(ref_frames),
        frames_requested=int(verify_frames),
        width=width,
        height=height,
        global_max_abs_error=int(global_max_abs_error),
        counts=counts,
        ratios=ratios,
    )
    if cons_failures:
        raise RuntimeError(
            "consistency check failed: " + "; ".join(cons_failures)
        )

    frame_maes = [float(s["mae"]) for s in frame_stats]
    frame_p95s = [float(s["pixel_p95_abs_error"]) for s in frame_stats]
    top5_mae = sorted(
        frame_stats, key=lambda s: (-float(s["mae"]), int(s["frame_index"]))
    )[:5]
    top5_gt8 = sorted(
        frame_stats,
        key=lambda s: (-int(s["count_gt_8"]), int(s["frame_index"])),
    )[:5]

    def _frame_summary(s: dict[str, Any]) -> dict[str, Any]:
        return {
            "frame_index": int(s["frame_index"]),
            "mae": float(s["mae"]),
            "max_abs_error": int(s["max_abs_error"]),
            "pixel_p95_abs_error": float(s["pixel_p95_abs_error"]),
            "count_gt_8": int(s["count_gt_8"]),
            "ratio_gt_8": float(s["ratio_gt_8"]),
            "count_gt_16": int(s["count_gt_16"]),
            "count_gt_32": int(s["count_gt_32"]),
            "count_gt_64": int(s["count_gt_64"]),
            "count_gt_128": int(s["count_gt_128"]),
        }

    if highest_max_error_bbox_gt_8 is None and highest_max_error_frame_index >= 0:
        for s in frame_stats:
            if int(s["frame_index"]) == int(highest_max_error_frame_index):
                highest_max_error_bbox_gt_8 = s.get("bbox_gt_8")
                break

    ref_crc_hex = f"{ref_crc & 0xFFFFFFFF:08x}"
    cand_crc_hex = f"{cand_crc & 0xFFFFFFFF:08x}"

    # path_meta: JSON-safe copy without embedding huge command twice if present.
    pm: dict[str, Any] = {}
    for k, v in (path_meta or {}).items():
        if k == "command":
            continue
        if isinstance(v, (str, int, float, bool)) or v is None:
            pm[k] = v
        elif isinstance(v, (list, tuple)):
            pm[k] = list(v)
        else:
            pm[k] = str(v)

    signed_stats = _finalize_signed_accum(signed_accum)

    return {
        "status": "pixel_verification_ok",
        "reference_case": reference,
        "candidate_case": candidate,
        "frames_compared": int(verify_frames),
        "width": int(width),
        "height": int(height),
        "command": list(cmd),
        "path_meta": pm,
        "global_mae": float(global_mae),
        "global_max_abs_error": int(global_max_abs_error),
        "global_pixel_p95_abs_error": float(global_pixel_p95),
        "ratio_gt_1": float(ratios["ratio_gt_1"]),
        "ratio_gt_2": float(ratios["ratio_gt_2"]),
        "ratio_gt_4": float(ratios["ratio_gt_4"]),
        "ratio_gt_8": float(ratios["ratio_gt_8"]),
        "ratio_gt_16": float(ratios["ratio_gt_16"]),
        "ratio_gt_32": float(ratios["ratio_gt_32"]),
        "ratio_gt_64": float(ratios["ratio_gt_64"]),
        "ratio_gt_128": float(ratios["ratio_gt_128"]),
        "count_gt_1": int(counts["count_gt_1"]),
        "count_gt_2": int(counts["count_gt_2"]),
        "count_gt_4": int(counts["count_gt_4"]),
        "count_gt_8": int(counts["count_gt_8"]),
        "count_gt_16": int(counts["count_gt_16"]),
        "count_gt_32": int(counts["count_gt_32"]),
        "count_gt_64": int(counts["count_gt_64"]),
        "count_gt_128": int(counts["count_gt_128"]),
        "total_pixels": int(total_pixels),
        "total_abs_error": int(total_abs_error),
        "frame_mae_median": float(_median(frame_maes)),
        "frame_mae_min": float(min(frame_maes)),
        "frame_mae_max": float(max(frame_maes)),
        "frame_pixel_p95_median": float(_median(frame_p95s)),
        "frame_pixel_p95_max": float(max(frame_p95s)),
        "highest_mae_frame_index": int(highest_mae_frame_index),
        "highest_mae_value": float(highest_mae_value),
        "highest_max_error_frame_index": int(highest_max_error_frame_index),
        "highest_max_error_value": int(highest_max_error_value),
        "highest_max_error_frame_bbox_gt_8": (
            {k: int(v) for k, v in highest_max_error_bbox_gt_8.items()}
            if highest_max_error_bbox_gt_8 is not None
            else None
        ),
        "top_error_pixels": top_error_pixels,
        "top5_mae_frames": [_frame_summary(s) for s in top5_mae],
        "top5_count_gt_8_frames": [_frame_summary(s) for s in top5_gt8],
        "mean_signed_error": signed_stats["mean_signed_error"],
        "median_signed_error": signed_stats["median_signed_error"],
        "signed_error_p05": signed_stats["signed_error_p05"],
        "signed_error_p25": signed_stats["signed_error_p25"],
        "signed_error_p75": signed_stats["signed_error_p75"],
        "signed_error_p95": signed_stats["signed_error_p95"],
        "negative_error_ratio": signed_stats["negative_error_ratio"],
        "positive_error_ratio": signed_stats["positive_error_ratio"],
        "zero_error_ratio": signed_stats["zero_error_ratio"],
        "reference_zero_count": signed_stats["reference_zero_count"],
        "reference_255_count": signed_stats["reference_255_count"],
        "candidate_zero_count": signed_stats["candidate_zero_count"],
        "candidate_255_count": signed_stats["candidate_255_count"],
        "pearson_r": signed_stats["pearson_r"],
        "linear_fit_slope": signed_stats["linear_fit_slope"],
        "linear_fit_intercept": signed_stats["linear_fit_intercept"],
        "linear_fit_r2": signed_stats["linear_fit_r2"],
        "intensity_bins": signed_stats["intensity_bins"],
        "gradient_bins": signed_stats.get("gradient_bins", []),
        "reference_crc32": ref_crc_hex,
        "candidate_crc32": cand_crc_hex,
        "frame_sync_ok": True,
        "partial_frame_bytes": int(partial_frame_bytes),
        "process_returncode": (
            int(ffmpeg_returncode) if ffmpeg_returncode is not None else None
        ),
        "bytes_received": int(total_candidate_bytes),
        "expected_bytes": int(verify_frames * bpf),
        "total_candidate_bytes": int(total_candidate_bytes),
        "stderr_tail": str(stderr_tail[:800] if stderr_tail else ""),
        "consistency_checks_ok": True,
    }


def run_verify_mode(
    args: argparse.Namespace,
    ffmpeg_path: str,
    ffmpeg_info: FFmpegInfo | None,
    video_meta: dict[str, Any],
) -> int:
    """Correctness-verify: reference vs each candidate in order.

    Default: P0=opencv_gray vs A/B/C(/D/E). Also supports FFmpeg-gray references
    for pairwise diagnostics (e.g. D vs B, B vs C) via --verify-reference.

    Sequential only: reopen reference for every candidate; never hold more than
    two frame streams. Fails fast on first candidate error after printing prior
    successes. When reference is opencv_gray, its CRC must be identical across
    candidates (re-read each round).
    """
    reference = (args.verify_reference or "").strip()
    candidates = _parse_csv_cases(args.verify_cases or "")
    proc_res = (int(args.proc_res[0]), int(args.proc_res[1]))
    verify_frames = (
        int(args.verify_frames) if args.verify_frames is not None else int(args.frames)
    )
    video_path = str(
        (video_meta or {}).get("path")
        or Path(args.video).expanduser()
    )
    color_meta = (video_meta or {}).get("color") if isinstance(video_meta, dict) else None
    if not isinstance(color_meta, dict):
        color_meta = probe_video_color_meta(ffmpeg_path, video_path)

    errors: list[str] = []

    if verify_frames <= 0:
        errors.append(f"--verify-frames must be > 0 (got {verify_frames})")
    if verify_frames > int(args.frames):
        errors.append(
            f"--verify-frames ({verify_frames}) must be <= --frames ({args.frames})"
        )

    if reference not in _VERIFY_REFERENCE_ALLOWED:
        errors.append(
            f"--verify-reference {reference!r} not allowed "
            f"(allowed: {sorted(_VERIFY_REFERENCE_ALLOWED)})"
        )

    if not candidates:
        errors.append("--verify-cases is empty after parsing")

    for c in candidates:
        if c == reference:
            errors.append(
                f"verify candidate {c!r} must not equal --verify-reference"
            )
        if c in _VERIFY_CASE_FORBIDDEN:
            errors.append(
                f"verify candidate {c!r} is forbidden "
                f"(not final gray @ proc-res, or is reference-only)"
            )
        elif c not in _VERIFY_CASE_ALLOWED:
            if c in ALL_CASES:
                errors.append(
                    f"verify candidate {c!r} is not allowed in verify mode "
                    f"(allowed: {sorted(_VERIFY_CASE_ALLOWED)})"
                )
            else:
                errors.append(f"unknown verify candidate case: {c!r}")

    if errors:
        for msg in errors:
            print(f"error: {msg}", file=sys.stderr)
        return 2

    # CUDA gate / runtime probe for any CUDA candidate or CUDA reference.
    need_cuda = any(c in _CUDA_GRAY_CASES for c in candidates) or (
        reference in _CUDA_GRAY_CASES
    )
    ffinfo = ffmpeg_info
    if need_cuda:
        if ffinfo is None:
            ffinfo = probe_ffmpeg(ffmpeg_path)
        if ffinfo.cuda_runtime_ok is None:
            ffinfo = probe_cuda_runtime(ffmpeg_path, video_path, ffinfo)

    print("=== bench_decode verify mode (multi-candidate) ===")
    print(f"video: {video_path}")
    print(f"reference: {reference}")
    print(f"candidates: {', '.join(candidates)}")
    print(f"verify_frames: {verify_frames} (cap from --frames={args.frames})")
    print(f"proc_res: {proc_res[0]}x{proc_res[1]}")
    print(f"require_hw: {bool(args.require_hw)}")
    print(
        f"scale_cuda_interp_algo_pinned: {_SCALE_CUDA_INTERP_ALGO_DEFAULT} "
        f"(binary default 0; no named alias)"
    )
    print(
        "source_color_meta: "
        f"codec={color_meta.get('codec_name')!r} "
        f"pix_fmt={color_meta.get('pix_fmt')!r} "
        f"size={color_meta.get('width')}x{color_meta.get('height')} "
        f"fps={color_meta.get('avg_frame_rate')!r} "
        f"range={color_meta.get('color_range')!r} "
        f"space={color_meta.get('color_space')!r} "
        f"primaries={color_meta.get('color_primaries')!r} "
        f"transfer={color_meta.get('color_transfer')!r} "
        f"field_order={color_meta.get('field_order')!r}"
    )
    if color_meta.get("raw_stream_line"):
        print(f"source_stream_line: {color_meta.get('raw_stream_line')}")
    if ffinfo is not None:
        print(
            f"cuda_runtime_ok: {ffinfo.cuda_runtime_ok} "
            f"reason={ffinfo.cuda_runtime_reason!r}"
        )

    def _inject_color(pm: dict[str, Any]) -> dict[str, Any]:
        out = dict(pm)
        out["source_color_range"] = color_meta.get("color_range", _COLOR_UNSPEC)
        out["source_color_space"] = color_meta.get("color_space", _COLOR_UNSPEC)
        out["source_color_primaries"] = color_meta.get(
            "color_primaries", _COLOR_UNSPEC
        )
        out["source_color_transfer"] = color_meta.get(
            "color_transfer", _COLOR_UNSPEC
        )
        out["source_codec_name"] = color_meta.get("codec_name", _COLOR_UNSPEC)
        out["source_pix_fmt"] = color_meta.get("pix_fmt", _COLOR_UNSPEC)
        out["source_field_order"] = color_meta.get("field_order", _COLOR_UNSPEC)
        return out

    def _gate_cuda_case(case_name: str) -> int | None:
        """Return exit code on gate failure, else None."""
        if case_name not in _CUDA_GRAY_CASES:
            return None
        if ffinfo is None:
            print(
                f"error: [{case_name}] missing FFmpegInfo for CUDA gate",
                file=sys.stderr,
            )
            return 1
        need_extract = case_name not in (
            "ffmpeg_cuda_nv12_direct",
            "ffmpeg_cuda_yuv444_convert_gray",
            "ffmpeg_cuda_cpu_area_gray",
        ) and not str(case_name).startswith("ffmpeg_cuda_diag_") and not str(
            case_name
        ).startswith("ffmpeg_cuda_e_interp_")
        # E / F / diag / e_interp use format=gray or CPU scale, not extractplanes.
        if (
            case_name
            in (
                "ffmpeg_cuda_yuv444_convert_gray",
                "ffmpeg_cuda_cpu_area_gray",
            )
            or str(case_name).startswith("ffmpeg_cuda_diag_")
            or str(case_name).startswith("ffmpeg_cuda_e_interp_")
        ):
            need_extract = False
        gate = _cuda_gate(
            case_name, ffinfo, verify_frames, need_extractplanes=need_extract
        )
        if gate is not None:
            print(
                f"error: [{case_name}] CUDA gate: status={gate.status} "
                f"reason={gate.skip_or_error_reason!r}",
                file=sys.stderr,
            )
            return 1
        if case_name == "ffmpeg_cuda_nv12_416_gray":
            mid = even_16x9_intermediate(proc_res[0], proc_res[1])
            if mid is None:
                print(
                    f"error: [{case_name}] cannot derive even 16:9 intermediate "
                    f"from proc_res={proc_res[0]}x{proc_res[1]}",
                    file=sys.stderr,
                )
                return 1
        return None

    # Gate CUDA reference once up front.
    if reference in _CUDA_GRAY_CASES:
        rc_gate = _gate_cuda_case(reference)
        if rc_gate is not None:
            return rc_gate

    # Pre-build FFmpeg reference command when reference is not OpenCV.
    ref_cmd: list[str] | None = None
    if reference != "opencv_gray":
        try:
            ref_cmd, _, _, _, _ = build_ffmpeg_cmd(
                reference, ffmpeg_path, video_path, verify_frames, proc_res
            )
        except Exception as exc:
            print(
                f"error: build reference command for {reference!r} failed: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 1
        print(f"reference_cmd: {subprocess.list2cmdline(ref_cmd)}")

    results: list[dict[str, Any]] = []
    reference_crc32_expected: str | None = None

    for cand in candidates:
        print(f"\n-- verify candidate {cand} ...", flush=True)

        rc_gate = _gate_cuda_case(cand)
        if rc_gate is not None:
            if results:
                print(
                    f"(kept {len(results)} prior successful candidate result(s))",
                    file=sys.stderr,
                )
                for r in results:
                    print(
                        f"  ok: {r['candidate_case']} "
                        f"mae={r['global_mae']:.6f} "
                        f"crc_ref={r['reference_crc32']} "
                        f"crc_c={r['candidate_crc32']}"
                    )
            return rc_gate

        try:
            cmd, bpf, wh, layout, path_meta = build_ffmpeg_cmd(
                cand, ffmpeg_path, video_path, verify_frames, proc_res
            )
        except Exception as exc:
            print(
                f"error: [{cand}] build command failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 1
        del layout, wh
        path_meta = _inject_color(path_meta)

        print(f"candidate_cmd: {subprocess.list2cmdline(cmd)}")
        print(f"path_meta: { {k: v for k, v in path_meta.items() if k != 'command'} }")

        try:
            one = _verify_one_candidate(
                reference=reference,
                candidate=cand,
                video_path=video_path,
                ffmpeg_path=ffmpeg_path,
                verify_frames=verify_frames,
                proc_res=proc_res,
                cmd=cmd,
                bpf=bpf,
                path_meta=path_meta,
                timeout_s=_DEFAULT_PIPE_TIMEOUT_S,
                ref_cmd=ref_cmd,
            )
        except Exception as exc:
            print(
                f"error: [{cand}] verify failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            if results:
                print(
                    f"(kept {len(results)} prior successful candidate result(s))",
                    file=sys.stderr,
                )
                for r in results:
                    print(
                        f"  ok: {r['candidate_case']} "
                        f"mae={r['global_mae']:.6f} "
                        f"crc_p0={r['reference_crc32']} "
                        f"crc_c={r['candidate_crc32']}"
                    )
            return 1

        # Reference CRC stability across candidates (re-read each round).
        if reference_crc32_expected is None:
            reference_crc32_expected = str(one["reference_crc32"])
            one["reference_crc32_expected"] = reference_crc32_expected
            one["reference_crc32_match"] = True
        else:
            one["reference_crc32_expected"] = reference_crc32_expected
            if str(one["reference_crc32"]) != reference_crc32_expected:
                print(
                    f"error: reference CRC unstable for {cand}: "
                    f"got {one['reference_crc32']} "
                    f"expected {reference_crc32_expected}",
                    file=sys.stderr,
                )
                one["reference_crc32_match"] = False
                results.append(one)
                return 1
            one["reference_crc32_match"] = True

        results.append(one)
        print(
            f"   ok: frames={one['frames_compared']} "
            f"mae={one['global_mae']:.6f} "
            f"max={one['global_max_abs_error']} "
            f"p95={one['global_pixel_p95_abs_error']} "
            f"p0_crc={one['reference_crc32']} "
            f"c_crc={one['candidate_crc32']} "
            f"rc={one['process_returncode']}"
        )

    print("\n=== multi-candidate verify summary ===")
    print(f"reference_crc32_expected: {reference_crc32_expected}")
    for one in results:
        print(f"--- {one['candidate_case']} ---")
        print(f"status: {one['status']}")
        print(f"frames_compared: {one['frames_compared']}")
        print(
            f"global_mae: {one['global_mae']:.6f} "
            f"max={one['global_max_abs_error']} "
            f"p95={one['global_pixel_p95_abs_error']}"
        )
        print(
            f"ratios: gt1={one['ratio_gt_1']:.6f} gt2={one['ratio_gt_2']:.6f} "
            f"gt4={one['ratio_gt_4']:.6f} gt8={one['ratio_gt_8']:.6f} "
            f"gt16={one['ratio_gt_16']:.6f} gt32={one['ratio_gt_32']:.6f} "
            f"gt64={one['ratio_gt_64']:.6f} gt128={one['ratio_gt_128']:.6f}"
        )
        print(
            f"counts: gt1={one['count_gt_1']} gt2={one['count_gt_2']} "
            f"gt4={one['count_gt_4']} gt8={one['count_gt_8']} "
            f"gt16={one['count_gt_16']} gt32={one['count_gt_32']} "
            f"gt64={one['count_gt_64']} gt128={one['count_gt_128']}"
        )
        print(
            f"frame_mae: min={one['frame_mae_min']:.6f} "
            f"median={one['frame_mae_median']:.6f} "
            f"max={one['frame_mae_max']:.6f}"
        )
        print(
            f"frame_pixel_p95: median={one['frame_pixel_p95_median']} "
            f"max={one['frame_pixel_p95_max']}"
        )
        print(
            f"worst_mae_frame: index={one['highest_mae_frame_index']} "
            f"mae={one['highest_mae_value']:.6f}"
        )
        print(
            f"worst_max_error_frame: index={one['highest_max_error_frame_index']} "
            f"max={one['highest_max_error_value']} "
            f"bbox_gt_8={one['highest_max_error_frame_bbox_gt_8']}"
        )
        print(f"reference_crc32: {one['reference_crc32']} match={one['reference_crc32_match']}")
        print(f"candidate_crc32: {one['candidate_crc32']}")
        print(
            f"bytes_received={one['bytes_received']} "
            f"expected_bytes={one['expected_bytes']} "
            f"partial_frame_bytes={one['partial_frame_bytes']} "
            f"process_returncode={one['process_returncode']}"
        )
        print(f"stderr_tail: {one['stderr_tail'] or '(empty)'}")
        print(f"path_meta: {one['path_meta']}")
        print(f"consistency_checks_ok: {one['consistency_checks_ok']}")
        print(
            f"signed: mean={one.get('mean_signed_error')} "
            f"median={one.get('median_signed_error')} "
            f"p05={one.get('signed_error_p05')} p25={one.get('signed_error_p25')} "
            f"p75={one.get('signed_error_p75')} p95={one.get('signed_error_p95')}"
        )
        print(
            f"signed_ratios: neg={one.get('negative_error_ratio')} "
            f"zero={one.get('zero_error_ratio')} pos={one.get('positive_error_ratio')}"
        )
        print(
            f"linear_fit: slope={one.get('linear_fit_slope')} "
            f"intercept={one.get('linear_fit_intercept')} "
            f"r2={one.get('linear_fit_r2')} pearson={one.get('pearson_r')}"
        )
        print(
            f"clip_counts: ref0={one.get('reference_zero_count')} "
            f"ref255={one.get('reference_255_count')} "
            f"cand0={one.get('candidate_zero_count')} "
            f"cand255={one.get('candidate_255_count')}"
        )
        print(f"intensity_bins: {one.get('intensity_bins')}")
        print(f"gradient_bins: {one.get('gradient_bins')}")
        print(f"top_error_pixels: {one['top_error_pixels']}")
        print(f"top5_mae_frames: {one['top5_mae_frames']}")
        print(f"top5_count_gt_8_frames: {one['top5_count_gt_8_frames']}")

    # JSON serializability of the full result list.
    try:
        json.dumps(results)
    except TypeError as exc:
        print(f"error: verify results not JSON-serializable: {exc}", file=sys.stderr)
        return 1

    print(f"\nall_candidates_ok: {len(results)}/{len(candidates)}")
    return 0

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Decode/preprocess benchmark (no production imports)")
    p.add_argument("--video", required=True, help="Input video path")
    p.add_argument(
        "--ffmpeg",
        required=True,
        help="Absolute path to ffmpeg, or 'auto' (must print resolved path)",
    )
    p.add_argument("--start", type=int, default=0, help="Start frame index (formal default 0)")
    p.add_argument("--frames", type=int, required=True, help="Number of frames to process")
    p.add_argument("--warmup", type=int, default=0)
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--proc-res", nargs=2, type=int, default=list(DEFAULT_PROC_RES), metavar=("W", "H"))
    p.add_argument(
        "--cases",
        default=",".join(CASE_ORDER),
        help=(
            f"Comma-separated cases (default: non-experimental). "
            f"Known: {','.join(ALL_CASES)}"
        ),
    )
    p.add_argument(
        "--require-hw",
        action="store_true",
        help="Exit non-zero if any hardware case is skipped or failed",
    )
    p.add_argument("--out-json", default=None, help="Optional JSON report path")
    # Correctness-verify mode (independent of performance benchmark path).
    p.add_argument(
        "--verify",
        action="store_true",
        help=(
            "Enable correctness-verify mode (reference vs each --verify-cases "
            "candidate in order; supports diagnostic D/E and FFmpeg-side refs)"
        ),
    )
    p.add_argument(
        "--verify-reference",
        default=_DEFAULT_VERIFY_REFERENCE,
        help=(
            f"Reference case for --verify (default: {_DEFAULT_VERIFY_REFERENCE}; "
            f"allowed: {', '.join(sorted(_VERIFY_REFERENCE_ALLOWED))})"
        ),
    )
    p.add_argument(
        "--verify-cases",
        default=",".join(_DEFAULT_VERIFY_CASES),
        help=(
            "Comma-separated candidate cases for --verify "
            f"(default: {','.join(_DEFAULT_VERIFY_CASES)}; "
            "must be final gray @ proc-res; reference excluded)"
        ),
    )
    p.add_argument(
        "--verify-frames",
        type=int,
        default=None,
        help=(
            "Frame count for --verify (default: inherit --frames; "
            "must be > 0 and <= --frames)"
        ),
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.frames <= 0:
        print("error: --frames must be > 0", file=sys.stderr)
        return 2
    if args.repeat <= 0:
        print("error: --repeat must be > 0", file=sys.stderr)
        return 2
    if args.warmup < 0:
        print("error: --warmup must be >= 0", file=sys.stderr)
        return 2

    seek_mode = "start_zero_only"
    if args.start != 0:
        # First formal version: allow flag but do not implement non-zero seek.
        print(
            "error: non-zero --start is not supported in this version "
            "(formal comparisons require --start 0; decode-and-discard seek is future work)",
            file=sys.stderr,
        )
        return 2

    video = str(Path(args.video).expanduser())
    if not Path(video).is_file():
        print(f"error: video not found: {video}", file=sys.stderr)
        return 2

    # Correctness-verify mode: independent of the performance benchmark path.
    # Resolve ffmpeg (and probe capabilities if any CUDA candidate is requested).
    if args.verify:
        try:
            ffmpeg = resolve_ffmpeg(args.ffmpeg)
        except Exception as exc:
            print(f"error: resolve ffmpeg failed: {exc}", file=sys.stderr)
            return 2
        verify_cases = _parse_csv_cases(getattr(args, "verify_cases", "") or "")
        verify_ref = (getattr(args, "verify_reference", "") or "").strip()
        ffinfo: FFmpegInfo | None = None
        need_cuda = any(c in _CUDA_GRAY_CASES for c in verify_cases) or (
            verify_ref in _CUDA_GRAY_CASES
        )
        if need_cuda:
            ffinfo = probe_ffmpeg(ffmpeg)
        color_meta = probe_video_color_meta(ffmpeg, video)
        return run_verify_mode(
            args,
            ffmpeg,
            ffinfo,
            {"path": video, "color": color_meta},
        )

    ffmpeg = resolve_ffmpeg(args.ffmpeg)
    ffinfo = probe_ffmpeg(ffmpeg)
    meta = open_video_meta(video)
    if meta["frame_count"] and args.frames > meta["frame_count"]:
        print(
            f"error: --frames {args.frames} exceeds reported frame_count {meta['frame_count']}",
            file=sys.stderr,
        )
        return 2

    proc_res = (int(args.proc_res[0]), int(args.proc_res[1]))
    cases = [c.strip() for c in args.cases.split(",") if c.strip()]
    unknown = [c for c in cases if c not in ALL_CASES]
    if unknown:
        print(f"error: unknown cases: {unknown}", file=sys.stderr)
        return 2

    # CUDA runtime probe only if a CUDA case is requested
    if any(c in _CUDA_GRAY_CASES for c in cases):
        ffinfo = probe_cuda_runtime(ffmpeg, video, ffinfo)

    print("=== bench_decode ===")
    print(f"video: {meta['path']}")
    print(
        f"meta: {meta['width']}x{meta['height']} fourcc={meta['fourcc']!r} "
        f"fps={meta['fps']} frames={meta['frame_count']}"
    )
    print(f"range: start={args.start} frames={args.frames} seek_mode={seek_mode}")
    print(f"proc_res: {proc_res[0]}x{proc_res[1]}")
    print(f"warmup={args.warmup} repeat={args.repeat}")
    print(f"ffmpeg: {ffmpeg}")
    print(f"ffmpeg_version: {ffinfo.version_line}")
    print(f"hwaccels: {', '.join(ffinfo.hwaccels) if ffinfo.hwaccels else '(none)'}")
    print(
        "components: "
        f"h264_cuvid={ffinfo.has_h264_cuvid} scale_cuda={ffinfo.has_scale_cuda} "
        f"hwdownload={ffinfo.has_hwdownload} extractplanes={ffinfo.has_extractplanes} "
        f"scale_npp={ffinfo.has_scale_npp} "
        f"d3d11va={ffinfo.has_d3d11va} qsv_hwaccel={ffinfo.has_qsv_hwaccel}"
    )
    print(
        f"cuda_runtime_ok: {ffinfo.cuda_runtime_ok} "
        f"reason={ffinfo.cuda_runtime_reason!r}"
    )
    print()

    results: list[CaseResult] = []
    for case in cases:
        print(f"-- running {case} ...", flush=True)
        result = execute_case(
            case=case,
            video=video,
            meta=meta,
            ffmpeg=ffmpeg,
            ffinfo=ffinfo,
            frames=args.frames,
            proc_res=proc_res,
            warmup=args.warmup,
            repeat=args.repeat,
        )
        results.append(result)
        if result.command:
            print(f"   cmd: {subprocess.list2cmdline(result.command)}")
        if result.path_meta:
            pm = {k: v for k, v in result.path_meta.items() if k != "command"}
            print(f"   path_meta: {pm}")
        print(
            f"   status={result.status} reason={result.skip_or_error_reason!r} "
            f"median_fps={result.median_fps}"
        )

    print()
    print_table(results)

    report = {
        "video": meta,
        "ffmpeg": asdict(ffinfo),
        "seek_mode": seek_mode,
        "start": args.start,
        "frames": args.frames,
        "warmup": args.warmup,
        "repeat": args.repeat,
        "proc_res": list(proc_res),
        "results": [asdict(r) for r in results],
    }
    if args.out_json:
        out_path = Path(args.out_json)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON written: {out_path.resolve()}")

    # Exit code policy
    hw_cases = [r for r in results if r.case in _CUDA_GRAY_CASES]
    if args.require_hw:
        for r in hw_cases:
            if r.status in ("skipped", "failed"):
                return 1
    # Soft default: non-zero only if any non-skipped case failed
    if any(r.status == "failed" for r in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
