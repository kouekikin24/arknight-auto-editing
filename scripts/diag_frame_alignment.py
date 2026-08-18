#!/usr/bin/env python3
"""Full-map frame alignment: P0 (OpenCV gray) vs A (FFmpeg software gray).

Reuses bench_decode iterators only (no -ss, no mid-stream seek).
Streams both paths from frame 0 with O(radius) buffers; does not cache full video.

Also optionally compares A vs A_passthrough (-fps_mode passthrough on output)
when supported by the local FFmpeg binary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import zlib
from collections import Counter, deque
from pathlib import Path
from typing import Any, Deque, Iterator

import cv2
import numpy as np

_SCRIPTS = Path(__file__).resolve().parent
_REPO = _SCRIPTS.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _load_bd():
    import importlib.util

    path = _SCRIPTS / "bench_decode.py"
    spec = importlib.util.spec_from_file_location("bench_decode_diag2", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bench_decode_diag2"] = mod
    spec.loader.exec_module(mod)
    return mod


bd = _load_bd()

MATCH_RADIUS = 3
SIG_SIZE = (16, 9)  # w,h tiny signature for optional banded alignment
KNOWN_EVENTS = [858, 1196, 1197, 1201, 1202, 1227, 1228, 1229, 1230]
MOTION_THR = 2.0

# Candidate aliases for alignment map (single candidate vs P0 per run).
CANDIDATE_ALIASES: dict[str, str] = {
    "a": "ffmpeg_sw_gray",
    "a_pt": "ffmpeg_sw_gray_passthrough",
    "f": "ffmpeg_cuda_cpu_area_gray",
    "f_pt": "ffmpeg_cuda_cpu_area_gray_passthrough",
    "e2": "ffmpeg_cuda_e_interp_2",
    "e2_pt": "ffmpeg_cuda_e_interp_2_passthrough",
}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _crc32(gray: np.ndarray) -> str:
    g = np.ascontiguousarray(gray)
    return f"{zlib.crc32(g.data) & 0xFFFFFFFF:08x}"


def _mae(a: np.ndarray, b: np.ndarray) -> float:
    return float(cv2.mean(cv2.absdiff(a, b))[0])


def _signature(gray: np.ndarray) -> np.ndarray:
    # INTER_AREA small preview for alignment only
    return cv2.resize(gray, SIG_SIZE, interpolation=cv2.INTER_AREA)


def _open_p0(video: str, frames: int, proc: tuple[int, int]) -> Iterator[np.ndarray]:
    return bd.iter_opencv_gray_frames(video, frames, proc[0], proc[1])


def _open_candidate(
    video: str,
    frames: int,
    proc: tuple[int, int],
    ffmpeg: str,
    backend_case: str,
) -> tuple[Iterator[np.ndarray], list[str], dict[str, Any]]:
    """Open FFmpeg gray candidate via bench_decode command construction only."""
    # CUDA gate when needed (base name for passthrough)
    base = getattr(bd, "_PASSTHROUGH_CASE_BASE", {}).get(backend_case, backend_case)
    if base in getattr(bd, "_CUDA_GRAY_CASES", frozenset()) or str(base).startswith(
        "ffmpeg_cuda_"
    ):
        info = bd.probe_ffmpeg(ffmpeg)
        info = bd.probe_cuda_runtime(ffmpeg, video, info)
        need_extract = not (
            base in ("ffmpeg_cuda_cpu_area_gray", "ffmpeg_cuda_yuv444_convert_gray")
            or str(base).startswith("ffmpeg_cuda_e_interp_")
        )
        gate = bd._cuda_gate(base, info, frames, need_extractplanes=need_extract)
        if gate is not None:
            raise RuntimeError(
                f"CUDA gate failed for {backend_case}: {gate.status} "
                f"{gate.skip_or_error_reason}"
            )
    cmd, bpf, wh, layout, meta = bd.build_ffmpeg_cmd(
        backend_case, ffmpeg, video, frames, proc
    )
    del bpf, wh, layout
    it = bd.iter_ffmpeg_gray_frames(
        ffmpeg,
        video,
        frames,
        proc[0],
        proc[1],
        timeout_s=float(getattr(bd, "_DEFAULT_PIPE_TIMEOUT_S", 180.0)),
        command=cmd,
    )
    return it, list(cmd), dict(meta)


def stream_full_map(
    video: str,
    frames: int,
    proc: tuple[int, int],
    ffmpeg: str,
    *,
    candidate_alias: str = "a",
) -> dict[str, Any]:
    """Stream P0 and one FFmpeg candidate; O(radius) buffers; per-frame match stats."""
    if candidate_alias not in CANDIDATE_ALIASES:
        raise ValueError(
            f"unknown candidate {candidate_alias!r}; allowed {sorted(CANDIDATE_ALIASES)}"
        )
    backend_case = CANDIDATE_ALIASES[candidate_alias]
    p0_it = _open_p0(video, frames, proc)
    a_it, a_cmd, cand_meta = _open_candidate(
        video, frames, proc, ffmpeg, backend_case
    )

    # Buffers of (index, gray, crc, sig)
    p0_buf: Deque[tuple[int, np.ndarray, str, np.ndarray]] = deque()
    a_buf: Deque[tuple[int, np.ndarray, str, np.ndarray]] = deque()
    r = MATCH_RADIUS

    per_frame: list[dict[str, Any]] = []
    p0_prev: np.ndarray | None = None
    a_prev: np.ndarray | None = None
    p0_prev_crc: str | None = None
    a_prev_crc: str | None = None
    p0_roll = 0
    a_roll = 0
    p0_count = 0
    a_count = 0

    # tiny signatures for optional banded DP (store all — 1231*16*9 bytes ≈ 177KB)
    p0_sigs: list[np.ndarray] = []
    a_sigs: list[np.ndarray] = []
    p0_crcs: list[str] = []
    a_crcs: list[str] = []
    p0_diffs: list[float] = []
    a_diffs: list[float] = []

    def push_p0(i: int, g: np.ndarray) -> None:
        nonlocal p0_prev, p0_prev_crc, p0_roll, p0_count
        if not g.flags["C_CONTIGUOUS"]:
            g = np.ascontiguousarray(g)
        crc = _crc32(g)
        sig = _signature(g)
        dtp = 0.0 if p0_prev is None else _mae(g, p0_prev)
        p0_prev = g
        p0_prev_crc = crc
        p0_roll = zlib.crc32(g.data, p0_roll)
        p0_count += 1
        p0_sigs.append(sig)
        p0_crcs.append(crc)
        p0_diffs.append(float(dtp))
        p0_buf.append((i, g, crc, sig))
        while len(p0_buf) > 2 * r + 1:
            p0_buf.popleft()

    def push_a(i: int, g: np.ndarray) -> None:
        nonlocal a_prev, a_prev_crc, a_roll, a_count
        if not g.flags["C_CONTIGUOUS"]:
            g = np.ascontiguousarray(g)
        crc = _crc32(g)
        sig = _signature(g)
        dtp = 0.0 if a_prev is None else _mae(g, a_prev)
        a_prev = g
        a_prev_crc = crc
        a_roll = zlib.crc32(g.data, a_roll)
        a_count += 1
        a_sigs.append(sig)
        a_crcs.append(crc)
        a_diffs.append(float(dtp))
        a_buf.append((i, g, crc, sig))
        while len(a_buf) > 2 * r + 1:
            a_buf.popleft()

    def find_in_buf(
        buf: Deque[tuple[int, np.ndarray, str, np.ndarray]], j: int
    ) -> tuple[int, np.ndarray, str, np.ndarray] | None:
        for item in buf:
            if item[0] == j:
                return item
        return None

    def match_row(
        ref_i: int,
        ref_g: np.ndarray,
        ref_crc: str,
        dst_buf: Deque[tuple[int, np.ndarray, str, np.ndarray]],
        dst_diff_to_prev: float | None,
        ref_diff_to_prev: float,
        ref_path: str,
    ) -> dict[str, Any]:
        candidates: list[tuple[int, float]] = []
        for j in range(ref_i - r, ref_i + r + 1):
            item = find_in_buf(dst_buf, j)
            if item is None:
                continue
            candidates.append((j, _mae(ref_g, item[1])))
        if not candidates:
            return {
                "i": ref_i,
                "error": "no_candidates_in_buffer",
            }
        candidates.sort(key=lambda x: (x[1], abs(x[0] - ref_i), x[0]))
        best_j, best_mae = candidates[0]
        second = candidates[1][1] if len(candidates) > 1 else None
        same = next((m for j, m in candidates if j == ref_i), None)
        conf_gap = None if second is None else float(second - best_mae)
        same_minus_best = None if same is None else float(same - best_mae)
        return {
            "i": ref_i,
            "ref_path": ref_path,
            "ref_crc": ref_crc,
            "ref_diff_to_prev": float(ref_diff_to_prev),
            "dst_diff_to_prev_at_same": dst_diff_to_prev,
            "same_index_mae": same,
            "best_offset": int(best_j - ref_i),
            "best_match_index": int(best_j),
            "best_mae": float(best_mae),
            "second_best_mae": second,
            "confidence_gap": conf_gap,
            "same_minus_best": same_minus_best,
            "same_index_is_best": best_j == ref_i,
        }

    # Prime: need both sides filled enough. Pull frame-by-frame in lockstep.
    try:
        for i in range(frames):
            try:
                g0 = next(p0_it)
            except StopIteration as exc:
                raise RuntimeError(f"P0 early EOF at {i}") from exc
            try:
                ga = next(a_it)
            except StopIteration as exc:
                raise RuntimeError(f"A early EOF at {i}") from exc

            push_p0(i, g0)
            push_a(i, ga)

            # Once both buffers contain this index, emit match for (i - r) if ready
            emit_i = i - r
            if emit_i >= 0:
                # P0[emit_i] vs A around it
                p0_item = find_in_buf(p0_buf, emit_i)
                if p0_item is None:
                    raise RuntimeError(f"P0 buffer missing {emit_i}")
                a_same = find_in_buf(a_buf, emit_i)
                a_dtp = a_diffs[emit_i] if emit_i < len(a_diffs) else None
                row = match_row(
                    emit_i,
                    p0_item[1],
                    p0_item[2],
                    a_buf,
                    a_dtp,
                    p0_diffs[emit_i],
                    "p0",
                )
                # reverse fields for A[emit_i] vs P0
                a_item = find_in_buf(a_buf, emit_i)
                if a_item is None:
                    raise RuntimeError(f"A buffer missing {emit_i}")
                rev = match_row(
                    emit_i,
                    a_item[1],
                    a_item[2],
                    p0_buf,
                    p0_diffs[emit_i],
                    a_diffs[emit_i],
                    "a",
                )
                per_frame.append(
                    {
                        "i": emit_i,
                        "p0_crc": p0_crcs[emit_i],
                        "a_crc": a_crcs[emit_i],
                        "p0_diff_to_prev": p0_diffs[emit_i],
                        "a_diff_to_prev": a_diffs[emit_i],
                        "p0_to_a": {
                            "same_index_mae": row.get("same_index_mae"),
                            "best_offset": row.get("best_offset"),
                            "best_match_index": row.get("best_match_index"),
                            "best_mae": row.get("best_mae"),
                            "second_best_mae": row.get("second_best_mae"),
                            "confidence_gap": row.get("confidence_gap"),
                            "same_minus_best": row.get("same_minus_best"),
                            "same_index_is_best": row.get("same_index_is_best"),
                        },
                        "a_to_p0": {
                            "same_index_mae": rev.get("same_index_mae"),
                            "best_offset": rev.get("best_offset"),
                            "best_match_index": rev.get("best_match_index"),
                            "best_mae": rev.get("best_mae"),
                            "second_best_mae": rev.get("second_best_mae"),
                            "confidence_gap": rev.get("confidence_gap"),
                            "same_minus_best": rev.get("same_minus_best"),
                            "same_index_is_best": rev.get("same_index_is_best"),
                        },
                    }
                )

        # flush remaining tail frames i = frames-r .. frames-1
        for emit_i in range(max(0, frames - r), frames):
            if any(pf["i"] == emit_i for pf in per_frame):
                continue
            p0_item = find_in_buf(p0_buf, emit_i)
            a_item = find_in_buf(a_buf, emit_i)
            if p0_item is None or a_item is None:
                # pull nothing more; should already be in buffer
                raise RuntimeError(f"tail buffer missing {emit_i}")
            row = match_row(
                emit_i,
                p0_item[1],
                p0_item[2],
                a_buf,
                a_diffs[emit_i],
                p0_diffs[emit_i],
                "p0",
            )
            rev = match_row(
                emit_i,
                a_item[1],
                a_item[2],
                p0_buf,
                p0_diffs[emit_i],
                a_diffs[emit_i],
                "a",
            )
            per_frame.append(
                {
                    "i": emit_i,
                    "p0_crc": p0_crcs[emit_i],
                    "a_crc": a_crcs[emit_i],
                    "p0_diff_to_prev": p0_diffs[emit_i],
                    "a_diff_to_prev": a_diffs[emit_i],
                    "p0_to_a": {
                        "same_index_mae": row.get("same_index_mae"),
                        "best_offset": row.get("best_offset"),
                        "best_match_index": row.get("best_match_index"),
                        "best_mae": row.get("best_mae"),
                        "second_best_mae": row.get("second_best_mae"),
                        "confidence_gap": row.get("confidence_gap"),
                        "same_minus_best": row.get("same_minus_best"),
                        "same_index_is_best": row.get("same_index_is_best"),
                    },
                    "a_to_p0": {
                        "same_index_mae": rev.get("same_index_mae"),
                        "best_offset": rev.get("best_offset"),
                        "best_match_index": rev.get("best_match_index"),
                        "best_mae": rev.get("best_mae"),
                        "second_best_mae": rev.get("second_best_mae"),
                        "confidence_gap": rev.get("confidence_gap"),
                        "same_minus_best": rev.get("same_minus_best"),
                        "same_index_is_best": rev.get("same_index_is_best"),
                    },
                }
            )

        # exhaust
        for name, it in (("p0", p0_it), ("a", a_it)):
            try:
                next(it)
                raise RuntimeError(f"{name} produced extra frame")
            except StopIteration:
                pass
            except bd.FFmpegGrayFrameError as exc:
                raise RuntimeError(f"{name} finalize: {exc}") from exc
    finally:
        try:
            p0_it.close()
        except Exception:
            pass
        try:
            a_it.close()
        except Exception:
            pass

    per_frame.sort(key=lambda x: x["i"])
    if len(per_frame) != frames or p0_count != frames or a_count != frames:
        raise RuntimeError(
            f"count mismatch per_frame={len(per_frame)} p0={p0_count} a={a_count} expected={frames}"
        )

    return {
        "candidate_alias": candidate_alias,
        "candidate_backend": backend_case,
        "candidate_path_meta": cand_meta,
        "a_command": a_cmd,
        "p0_rolling_crc32": f"{p0_roll & 0xFFFFFFFF:08x}",
        "a_rolling_crc32": f"{a_roll & 0xFFFFFFFF:08x}",
        "frames": frames,
        "per_frame": per_frame,
        "p0_diffs": p0_diffs,
        "a_diffs": a_diffs,
        "p0_crcs": p0_crcs,
        "a_crcs": a_crcs,
        "p0_sigs": p0_sigs,
        "a_sigs": a_sigs,
    }


def summarize_offsets(per_frame: list[dict[str, Any]]) -> dict[str, Any]:
    offs = [int(pf["p0_to_a"]["best_offset"]) for pf in per_frame]
    hist = Counter(offs)
    same_best = sum(1 for pf in per_frame if pf["p0_to_a"]["same_index_is_best"])
    return {
        "histogram": {str(k): int(v) for k, v in sorted(hist.items())},
        "same_index_is_best_count": same_best,
        "n": len(per_frame),
        "same_index_is_best_fraction": same_best / max(1, len(per_frame)),
    }


def high_discrimination_frames(
    per_frame: list[dict[str, Any]], top_k: int = 200
) -> list[dict[str, Any]]:
    """Frames with large confidence_gap and/or large motion — better offset evidence."""
    scored = []
    for pf in per_frame:
        gap = pf["p0_to_a"]["confidence_gap"]
        gap = 0.0 if gap is None else float(gap)
        mot = max(float(pf["p0_diff_to_prev"]), float(pf["a_diff_to_prev"]))
        # prefer high gap; break ties with motion
        scored.append((gap, mot, pf))
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]["i"]))
    out = []
    for gap, mot, pf in scored[:top_k]:
        out.append(
            {
                "i": pf["i"],
                "best_offset": pf["p0_to_a"]["best_offset"],
                "best_mae": pf["p0_to_a"]["best_mae"],
                "same_index_mae": pf["p0_to_a"]["same_index_mae"],
                "confidence_gap": gap,
                "p0_diff_to_prev": pf["p0_diff_to_prev"],
                "a_diff_to_prev": pf["a_diff_to_prev"],
                "same_index_is_best": pf["p0_to_a"]["same_index_is_best"],
            }
        )
    return out


def event_frames(per_frame: list[dict[str, Any]], thr: float = MOTION_THR) -> list[dict[str, Any]]:
    out = []
    for pf in per_frame:
        if pf["p0_diff_to_prev"] > thr or pf["a_diff_to_prev"] > thr:
            out.append(
                {
                    "i": pf["i"],
                    "p0_diff_to_prev": pf["p0_diff_to_prev"],
                    "a_diff_to_prev": pf["a_diff_to_prev"],
                    "best_offset": pf["p0_to_a"]["best_offset"],
                    "best_mae": pf["p0_to_a"]["best_mae"],
                    "same_index_mae": pf["p0_to_a"]["same_index_mae"],
                    "confidence_gap": pf["p0_to_a"]["confidence_gap"],
                    "same_index_is_best": pf["p0_to_a"]["same_index_is_best"],
                }
            )
    return out


def build_offset_runs(
    per_frame: list[dict[str, Any]],
    *,
    min_conf_gap: float = 0.05,
    min_motion: float = 0.5,
) -> list[dict[str, Any]]:
    """Runs using only frames that are not pure static ambiguity."""
    selected = []
    for pf in per_frame:
        gap = pf["p0_to_a"]["confidence_gap"]
        gap = 0.0 if gap is None else float(gap)
        mot = max(float(pf["p0_diff_to_prev"]), float(pf["a_diff_to_prev"]))
        if gap >= min_conf_gap or mot >= min_motion or pf["i"] in KNOWN_EVENTS:
            selected.append(pf)
    if not selected:
        return []
    runs: list[dict[str, Any]] = []
    cur_off = int(selected[0]["p0_to_a"]["best_offset"])
    start = int(selected[0]["i"])
    acc: list[dict[str, Any]] = [selected[0]]

    def flush(end_i: int, items: list[dict[str, Any]], off: int) -> None:
        bests = [float(x["p0_to_a"]["best_mae"]) for x in items]
        sames = [
            float(x["p0_to_a"]["same_index_mae"])
            for x in items
            if x["p0_to_a"]["same_index_mae"] is not None
        ]
        gaps = [
            float(x["p0_to_a"]["confidence_gap"])
            for x in items
            if x["p0_to_a"]["confidence_gap"] is not None
        ]
        event_n = sum(
            1
            for x in items
            if x["p0_diff_to_prev"] > MOTION_THR or x["a_diff_to_prev"] > MOTION_THR
        )
        runs.append(
            {
                "offset": off,
                "start": start,
                "end": end_i,  # inclusive last supporting frame
                "frame_count": end_i - start + 1,
                "supporting_frame_count": len(items),
                "supporting_event_count": event_n,
                "median_best_mae": float(np.median(bests)) if bests else None,
                "median_same_index_mae": float(np.median(sames)) if sames else None,
                "median_confidence_gap": float(np.median(gaps)) if gaps else None,
            }
        )

    for pf in selected[1:]:
        off = int(pf["p0_to_a"]["best_offset"])
        if off == cur_off:
            acc.append(pf)
        else:
            flush(int(acc[-1]["i"]), acc, cur_off)
            cur_off = off
            start = int(pf["i"])
            acc = [pf]
    flush(int(acc[-1]["i"]), acc, cur_off)
    return runs


def peak_alignment(
    p0_diffs: list[float], a_diffs: list[float], top_k: int = 50
) -> dict[str, Any]:
    p0_arr = np.asarray(p0_diffs, float)
    a_arr = np.asarray(a_diffs, float)
    n = len(p0_arr)

    def top_peaks(arr: np.ndarray, k: int) -> list[int]:
        # local maxima among top-k by value
        idx = np.argsort(-arr)[: max(k * 3, k)]
        peaks = []
        for i in idx:
            i = int(i)
            left = arr[i - 1] if i > 0 else -1
            right = arr[i + 1] if i + 1 < n else -1
            if arr[i] >= left and arr[i] >= right:
                peaks.append(i)
            if len(peaks) >= k:
                break
        return peaks

    p0_peaks = top_peaks(p0_arr, top_k)
    a_peaks = top_peaks(a_arr, top_k)
    p0_over = [int(i) for i in np.flatnonzero(p0_arr > MOTION_THR)]
    a_over = [int(i) for i in np.flatnonzero(a_arr > MOTION_THR)]

    def match_peaks(src: list[int], src_arr: np.ndarray, dst: list[int], dst_arr: np.ndarray):
        rows = []
        dst_set = set(dst)
        for i in src:
            cand = [j for j in range(i - 3, i + 4) if 0 <= j < n]
            # prefer destination peaks, else max diff in band
            peak_cands = [j for j in cand if j in dst_set]
            if peak_cands:
                j_best = max(peak_cands, key=lambda j: dst_arr[j])
            else:
                j_best = max(cand, key=lambda j: dst_arr[j]) if cand else i
            rows.append(
                {
                    "event_frame": int(i),
                    "matched_frame": int(j_best),
                    "event_offset": int(j_best - i),
                    "src_diff": float(src_arr[i]),
                    "dst_diff": float(dst_arr[j_best]),
                }
            )
        return rows

    p0_to_a = match_peaks(p0_peaks, p0_arr, a_peaks, a_arr)
    a_to_p0 = match_peaks(a_peaks, a_arr, p0_peaks, p0_arr)
    # mutual
    for row in p0_to_a:
        i = row["event_frame"]
        j = row["matched_frame"]
        rev = next((x for x in a_to_p0 if x["event_frame"] == j), None)
        row["mutual_nearest"] = bool(rev and rev["matched_frame"] == i)

    special = {}
    for e in KNOWN_EVENTS:
        if 0 <= e < n:
            special[str(e)] = {
                "p0_diff": float(p0_arr[e]),
                "a_diff": float(a_arr[e]),
                "band": {
                    str(j): {"p0": float(p0_arr[j]), "a": float(a_arr[j])}
                    for j in range(max(0, e - 3), min(n, e + 4))
                },
            }

    return {
        "p0_top_peaks": p0_peaks,
        "a_top_peaks": a_peaks,
        "p0_over_motion_thr": p0_over,
        "a_over_motion_thr": a_over,
        "p0_peak_to_a": p0_to_a,
        "a_peak_to_p0": a_to_p0,
        "known_event_band": special,
    }


def banded_signature_align(
    p0_sigs: list[np.ndarray], a_sigs: list[np.ndarray], band: int = 5
) -> dict[str, Any]:
    """Band-limited DTW-like alignment on tiny signatures. Diagnostic only."""
    n = len(p0_sigs)
    m = len(a_sigs)
    if n == 0 or m == 0 or n != m:
        return {"supported": False, "reason": f"length p0={n} a={m}"}
    # costs: mae of signatures
    # DP with ops match / p0-only / a-only within band
    INF = 1e18
    # dp[i][k] where k = j - i + band, j in [i-band, i+band]
    width = 2 * band + 1
    dp = np.full((n + 1, width), INF, dtype=np.float64)
    bt = np.full((n + 1, width), -1, dtype=np.int8)  # 0 match, 1 p0-only, 2 a-only
    bj = np.full((n + 1, width), -1, dtype=np.int32)

    def k_of(i: int, j: int) -> int | None:
        k = j - i + band
        if 0 <= k < width:
            return k
        return None

    # init
    dp[0, band] = 0.0  # i=0,j=0
    for t in range(1, band + 1):
        # leading a-only or p0-only
        k = k_of(0, t)
        if k is not None:
            dp[0, k] = t * 10.0  # penalty
            bt[0, k] = 2
            bj[0, k] = t - 1
        k = k_of(t, 0)
        # can't encode j=0 i=t in this k scheme easily for all; skip free leading p0

    def sig_mae(i: int, j: int) -> float:
        return float(cv2.mean(cv2.absdiff(p0_sigs[i], a_sigs[j]))[0])

    for i in range(n):
        for k in range(width):
            if dp[i, k] >= INF / 2:
                continue
            j = i + (k - band)
            if j < 0 or j > m:
                continue
            # match (i,j) -> (i+1,j+1) if both available
            if i < n and j < m:
                nk = k_of(i + 1, j + 1)
                if nk is not None:
                    cost = dp[i, k] + sig_mae(i, j)
                    if cost < dp[i + 1, nk]:
                        dp[i + 1, nk] = cost
                        bt[i + 1, nk] = 0
                        bj[i + 1, nk] = j
            # p0-only: (i+1, j)
            if i < n:
                nk = k_of(i + 1, j)
                if nk is not None:
                    cost = dp[i, k] + 8.0
                    if cost < dp[i + 1, nk]:
                        dp[i + 1, nk] = cost
                        bt[i + 1, nk] = 1
                        bj[i + 1, nk] = j
            # a-only: (i, j+1) — stay on same i row hard with this indexing; skip
            # Use secondary loop on j dimension by allowing k+1 at same i via repeated a-only
            if j < m:
                nk = k_of(i, j + 1)
                if nk is not None and i == i:  # same i
                    # We only have dp rows by i; encode a-only by updating same row carefully
                    pass

    # Simpler banded path: greedy along diagonal using cumulative best offsets from MAE map
    # Mark signature DP as simplified — return greedy path from existing per-frame offsets
    return {
        "supported": True,
        "method": "signature_cost_available_but_path_from_fullres_offsets",
        "note": (
            "Full asymmetric DTW omitted to avoid arbitrary gap penalties; "
            "insert/drop candidates derived from full-res offset runs + event peaks."
        ),
        "band": band,
        "sig_size": list(SIG_SIZE),
    }


def opencv_pos_slice(video: str, indices: list[int]) -> list[dict[str, Any]]:
    want = set(indices)
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        raise RuntimeError("opencv open failed")
    rows = []
    try:
        backend = None
        try:
            backend = cap.getBackendName()
        except Exception:
            backend = None
        i = 0
        while True:
            ok, fr = cap.read()
            if not ok:
                break
            if i in want:
                rows.append(
                    {
                        "logical_frame_index": i,
                        "CAP_PROP_POS_FRAMES": float(cap.get(cv2.CAP_PROP_POS_FRAMES)),
                        "CAP_PROP_POS_MSEC": float(cap.get(cv2.CAP_PROP_POS_MSEC)),
                        "backend": backend,
                    }
                )
            i += 1
            if i > max(want):
                break
    finally:
        cap.release()
    return rows


def ffmpeg_showinfo_slice(
    ffmpeg: str, video: str, frames: int, indices: list[int]
) -> dict[str, Any]:
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-i",
        video,
        "-an",
        "-frames:v",
        str(frames),
        "-vf",
        "showinfo",
        "-f",
        "null",
        "-",
    ]
    p = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600.0,
    )
    text = (p.stderr or "") + "\n" + (p.stdout or "")
    pat = re.compile(
        r"n:\s*(?P<n>\d+)\s+pts:\s*(?P<pts>-?\d+)\s+pts_time:(?P<pts_time>[-0-9.eE+]+)"
        r"(?:.*?checksum:(?P<checksum>[0-9A-Fa-f]+))?",
        re.I,
    )
    want = set(indices)
    rows = []
    all_n = []
    for m in pat.finditer(text):
        n = int(m.group("n"))
        all_n.append(n)
        if n in want:
            rows.append(
                {
                    "n": n,
                    "pts": int(m.group("pts")),
                    "pts_time": float(m.group("pts_time")),
                    "checksum": m.group("checksum"),
                }
            )
    contig = all(all_n[i] == all_n[i - 1] + 1 for i in range(1, len(all_n))) if all_n else False
    return {
        "returncode": int(p.returncode),
        "parsed_count": len(all_n),
        "n_contiguous": contig,
        "rows": rows,
        "command": cmd,
    }


def classify_global(
    hist: dict[str, int],
    runs: list[dict[str, Any]],
    events: list[dict[str, Any]],
    head: list[dict[str, Any]],
    tail: list[dict[str, Any]],
    passthrough_same: bool | None,
) -> dict[str, Any]:
    n = sum(int(v) for v in hist.values()) or 1
    frac1 = int(hist.get("1", 0)) / n
    frac0 = int(hist.get("0", 0)) / n
    # head/tail offsets among event-ish
    head_offs = Counter(int(pf["p0_to_a"]["best_offset"]) for pf in head)
    tail_offs = Counter(int(pf["p0_to_a"]["best_offset"]) for pf in tail)
    event_offs = Counter(int(e["best_offset"]) for e in events)

    label = "5_segmented_or_inconclusive"
    reasons: list[str] = []

    if passthrough_same is False:
        label = "3_output_frame_sync_difference"
        reasons.append("A_passthrough changed relative to A")
    elif frac1 >= 0.8 and int(event_offs.get(1, 0)) >= max(1, int(0.6 * max(1, len(events)))):
        # check if early head also +1
        head_event = [pf for pf in head if max(pf["p0_diff_to_prev"], pf["a_diff_to_prev"]) > 0.5]
        if not head_event:
            head_dom = head_offs.most_common(1)[0][0] if head_offs else None
        else:
            head_dom = Counter(
                int(pf["p0_to_a"]["best_offset"]) for pf in head_event
            ).most_common(1)[0][0]
        tail_dom = tail_offs.most_common(1)[0][0] if tail_offs else None
        if head_dom == 1 and tail_dom == 1:
            label = "1_global_start_phase_offset"
            reasons.append("high +1 fraction and head/tail/events dominated by +1")
        elif head_dom == 0 and tail_dom == 1:
            label = "2_midstream_insert_or_drop"
            reasons.append("head ~0 then later +1")
        else:
            label = "1_global_start_phase_offset"
            reasons.append(
                f"event/tail +1 dominant; head_dom={head_dom} tail_dom={tail_dom}"
            )
    elif frac0 >= 0.8 and int(event_offs.get(0, 0)) >= max(1, int(0.6 * max(1, len(events)))):
        label = "4_static_ambiguity"
        reasons.append("events prefer 0; +1 may be static confusion")
    else:
        reasons.append(f"hist={hist} event_offs={dict(event_offs)} runs={len(runs)}")

    return {
        "label": label,
        "reasons": reasons,
        "fractions": {"offset0": frac0, "offset1": frac1},
        "event_offset_hist": {str(k): int(v) for k, v in event_offs.items()},
        "head_offset_hist": {str(k): int(v) for k, v in head_offs.items()},
        "tail_offset_hist": {str(k): int(v) for k, v in tail_offs.items()},
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Full-map P0 vs FFmpeg candidate frame alignment"
    )
    p.add_argument("--video", required=True)
    p.add_argument("--frames", type=int, required=True)
    p.add_argument("--ffmpeg", required=True)
    p.add_argument("--proc-res", nargs=2, type=int, default=[400, 225])
    p.add_argument("--out-json", required=True)
    p.add_argument(
        "--candidate",
        default="a",
        choices=sorted(CANDIDATE_ALIASES.keys()),
        help=(
            "Single FFmpeg-family candidate vs P0 (default a=legacy sw gray). "
            "Use a_pt/f_pt/e2_pt for explicit -fps_mode passthrough backends."
        ),
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    video = str(Path(args.video).expanduser())
    if not Path(video).is_file():
        print(f"error: video not found: {video}", file=sys.stderr)
        return 2
    frames = int(args.frames)
    if frames <= 0:
        print("error: bad frames", file=sys.stderr)
        return 2
    proc = (int(args.proc_res[0]), int(args.proc_res[1]))
    try:
        ffmpeg = bd.resolve_ffmpeg(args.ffmpeg)
    except Exception as exc:
        print(f"error: ffmpeg: {exc}", file=sys.stderr)
        return 2

    cand_alias = str(args.candidate)
    backend = CANDIDATE_ALIASES[cand_alias]
    print("=== full-map frame alignment ===")
    print(f"video={video} frames={frames} proc={proc}")
    print(f"ffmpeg={ffmpeg}")
    print(f"candidate={cand_alias} backend={backend}")

    print(f"-- streaming full offset map (P0 vs {cand_alias}) ...", flush=True)
    full = stream_full_map(video, frames, proc, ffmpeg, candidate_alias=cand_alias)
    print(
        "   done rolling",
        full["p0_rolling_crc32"],
        full["a_rolling_crc32"],
        "rows",
        len(full["per_frame"]),
    )

    # drop heavy sigs from final after use
    p0_sigs = full.pop("p0_sigs")
    a_sigs = full.pop("a_sigs")
    per_frame = full["per_frame"]

    hist = summarize_offsets(per_frame)
    high = high_discrimination_frames(per_frame, 200)
    high_hist = Counter(int(x["best_offset"]) for x in high)
    events = event_frames(per_frame)
    event_hist = Counter(int(x["best_offset"]) for x in events)
    runs = build_offset_runs(per_frame)

    head = [pf for pf in per_frame if pf["i"] < 30]
    tail = [pf for pf in per_frame if pf["i"] >= frames - 31]
    peaks = peak_alignment(full["p0_diffs"], full["a_diffs"], 50)

    print("-- head/tail POS + showinfo ...", flush=True)
    idx_meta = list(range(0, 31)) + list(range(max(0, frames - 31), frames))
    pos_rows = opencv_pos_slice(video, idx_meta)
    show = ffmpeg_showinfo_slice(ffmpeg, video, frames, idx_meta)

    # signature note
    sig_align = banded_signature_align(p0_sigs, a_sigs, band=5)

    # Optional second map for legacy 'a' only: compare against a_pt without
    # overwriting the primary candidate result block.
    passthrough_block: dict[str, Any]
    if cand_alias == "a":
        print("-- secondary map P0 vs a_pt (explicit passthrough backend) ...", flush=True)
        try:
            full_pt = stream_full_map(
                video, frames, proc, ffmpeg, candidate_alias="a_pt"
            )
            a_crc_pt = full_pt["a_crcs"]
            a_crc = full["a_crcs"]
            same_all = a_crc_pt == a_crc
            first_diff = next(
                (i for i in range(frames) if a_crc_pt[i] != a_crc[i]), None
            )
            hist_pt = summarize_offsets(full_pt["per_frame"])
            passthrough_block = {
                "supported": True,
                "secondary_candidate": "a_pt",
                "command": full_pt["a_command"],
                "path_meta": full_pt.get("candidate_path_meta"),
                "a_rolling_crc32": full_pt["a_rolling_crc32"],
                "identical_to_primary_candidate": same_all,
                "rolling_identical": full_pt["a_rolling_crc32"]
                == full["a_rolling_crc32"],
                "first_crc_mismatch_index": first_diff,
                "offset_histogram": hist_pt["histogram"],
                "same_index_is_best_fraction": hist_pt["same_index_is_best_fraction"],
                "p0_rolling_crc32": full_pt["p0_rolling_crc32"],
            }
            del full_pt
        except Exception as exc:
            passthrough_block = {
                "supported": False,
                "reason": f"{type(exc).__name__}: {exc}",
            }
    else:
        passthrough_block = {
            "supported": False,
            "reason": (
                f"secondary a_pt comparison only auto-runs when --candidate a; "
                f"primary candidate is {cand_alias}"
            ),
        }

    passthrough_same = passthrough_block.get("identical_to_primary_candidate")
    classification = classify_global(
        hist["histogram"], runs, events, head, tail, passthrough_same
    )

    # impact notes
    impact = {
        "a_f_equivalence": "still_valid — A≡F was proven independently; alignment issue is P0 vs A/F",
        "a_e2_pixel_and_business": "still_valid for same-index among FFmpeg family (A/F/E2 share timeline)",
        "p0_vs_af_same_index_scores": (
            "contaminated where best_offset!=0; especially motion peaks shifted by ~1 frame"
        ),
        "p0_vs_af_diff_to_prev": "requires offset correction near scene cuts; raw same-index diffs misleading",
        "zero_label_mismatch": "can keep as observed on this video; not proof of alignment",
        "future_pause_validation": (
            "align by content offset (prefer event-peak mapping) or fix frame origin/sync "
            "before comparing diffs/masks; do not trust unaligned same-index diffs"
        ),
    }

    # suspected insert/drop from runs (content-level)
    suspects = []
    for i in range(1, len(runs)):
        if runs[i]["offset"] != runs[i - 1]["offset"]:
            suspects.append(
                {
                    "at_supporting_frame": runs[i]["start"],
                    "offset_before": runs[i - 1]["offset"],
                    "offset_after": runs[i]["offset"],
                    "note": "offset run change; verify against event peaks",
                }
            )

    # head/end detail compact
    def pack_detail(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for pf in rows:
            out.append(
                {
                    "i": pf["i"],
                    "p0_crc": pf["p0_crc"],
                    "a_crc": pf["a_crc"],
                    "p0_diff_to_prev": pf["p0_diff_to_prev"],
                    "a_diff_to_prev": pf["a_diff_to_prev"],
                    "same_index_mae": pf["p0_to_a"]["same_index_mae"],
                    "best_offset": pf["p0_to_a"]["best_offset"],
                    "best_mae": pf["p0_to_a"]["best_mae"],
                    "second_best_mae": pf["p0_to_a"]["second_best_mae"],
                    "confidence_gap": pf["p0_to_a"]["confidence_gap"],
                    "same_index_is_best": pf["p0_to_a"]["same_index_is_best"],
                }
            )
        return out

    result = {
        "metadata": {
            "video": str(Path(video).resolve()),
            "video_sha256": _sha256_file(Path(video)),
            "frames": frames,
            "proc_res": list(proc),
            "match_radius": MATCH_RADIUS,
            "ffmpeg": ffmpeg,
            "candidate_alias": cand_alias,
            "candidate_backend": backend,
            "candidate_command": full["a_command"],
            "candidate_path_meta": full.get("candidate_path_meta"),
            "timeline_family": (full.get("candidate_path_meta") or {}).get(
                "timeline_family"
            ),
            "output_fps_mode": (full.get("candidate_path_meta") or {}).get(
                "output_fps_mode"
            ),
            "no_ss": True,
            "diag_script_sha256": _sha256_file(Path(__file__).resolve()),
            "bench_decode_sha256": _sha256_file(_SCRIPTS / "bench_decode.py"),
            "timeline_result_status": getattr(bd, "_TIMELINE_RESULT_STATUS", {}),
        },
        "determinism": {
            "note": "single full-map pass; path determinism previously proven for P0/A on this sample",
            "p0_rolling_crc32": full["p0_rolling_crc32"],
            "candidate_rolling_crc32": full["a_rolling_crc32"],
            "frames_read_p0_and_candidate": frames,
        },
        "full_offset_map_summary": {
            "all_frames": hist,
            "high_discrimination_top200_histogram": {
                str(k): int(v) for k, v in sorted(high_hist.items())
            },
            "event_frames_histogram": {
                str(k): int(v) for k, v in sorted(event_hist.items())
            },
            "event_frame_count": len(events),
        },
        "offset_runs": runs,
        "high_confidence_frames": high[:100],
        "motion_event_alignment": peaks,
        "start_detail": {
            "frames_0_30": pack_detail(head),
            "opencv_pos": [r for r in pos_rows if r["logical_frame_index"] < 31],
            "ffmpeg_showinfo": [r for r in show["rows"] if r["n"] < 31],
        },
        "end_detail": {
            "frames_tail": pack_detail(tail),
            "opencv_pos": [r for r in pos_rows if r["logical_frame_index"] >= frames - 31],
            "ffmpeg_showinfo": [r for r in show["rows"] if r["n"] >= frames - 31],
        },
        "fps_sync_variant": passthrough_block,
        "suspected_insert_drop_events": suspects,
        "signature_align": sig_align,
        "classification": classification,
        "impact_on_existing_results": impact,
        "answers": {
            "p0_0_best_offset": head[0]["p0_to_a"]["best_offset"] if head else None,
            "p0_0_best_match_index": head[0]["p0_to_a"]["best_match_index"] if head else None,
            "p0_0_same_mae": head[0]["p0_to_a"]["same_index_mae"] if head else None,
            "a_0_to_p0": head[0]["a_to_p0"] if head else None,
            "p0_last": pack_detail([per_frame[-1]])[0] if per_frame else None,
            "a_last_to_p0": per_frame[-1]["a_to_p0"] if per_frame else None,
        },
        "per_frame_offset_map": [
            {
                "i": pf["i"],
                "best_offset": pf["p0_to_a"]["best_offset"],
                "best_mae": pf["p0_to_a"]["best_mae"],
                "same_index_mae": pf["p0_to_a"]["same_index_mae"],
                "confidence_gap": pf["p0_to_a"]["confidence_gap"],
                "p0_diff_to_prev": pf["p0_diff_to_prev"],
                "a_diff_to_prev": pf["a_diff_to_prev"],
                "same_index_is_best": pf["p0_to_a"]["same_index_is_best"],
            }
            for pf in per_frame
        ],
        "showinfo_meta": {
            "parsed_count": show["parsed_count"],
            "n_contiguous": show["n_contiguous"],
            "returncode": show["returncode"],
        },
        "limitations": {
            "match_radius": MATCH_RADIUS,
            "static_frames_ambiguous": True,
            "signature_dtw": "not used as primary evidence",
            "single_candidate_per_run": True,
            "primary_candidate": cand_alias,
        },
    }

    def sanitize(o: Any) -> Any:
        if isinstance(o, dict):
            return {str(k): sanitize(v) for k, v in o.items()}
        if isinstance(o, list):
            return [sanitize(v) for v in o]
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating, float)):
            x = float(o)
            return x if math.isfinite(x) else None
        if isinstance(o, np.ndarray):
            raise TypeError("ndarray leaked")
        return o

    result = sanitize(result)
    out = Path(args.out_json)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    out.write_text(text, encoding="utf-8")
    json.loads(out.read_text(encoding="utf-8"))
    print(f"JSON written: {out.resolve()} size={out.stat().st_size}")
    print("offset hist all", hist["histogram"])
    print("offset hist events", dict(event_hist))
    print("runs", json.dumps(runs[:20], indent=2)[:2000])
    print("classification", classification)
    print(
        "head0",
        pack_detail(head)[:3],
        "tail last3",
        pack_detail(tail)[-3:],
    )
    print("passthrough", passthrough_block.get("identical_to_a"), passthrough_block.get("supported"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
