#!/usr/bin/env python3
"""Build an independent decoded-frame/PTS oracle for the libmpv spike.

The oracle intentionally does not import ``spike_mpv.py`` or production modules.
It asks FFmpeg's ``showinfo`` filter for every decoded frame and never invents a
timestamp from ``frame / fps``. Any missing, duplicate, or non-monotonic PTS
makes the result BLOCKED and therefore non-authoritative.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, NamedTuple, Sequence


SCHEMA_VERSION = 1
EXIT_PASS = 0
EXIT_USAGE = 2
EXIT_BLOCKED = 10
EXIT_FFMPEG_UNAVAILABLE = 20
EXIT_FFMPEG_FAILED = 21
EXIT_OUTPUT_FAILED = 22

_FRAME_LINE_RE = re.compile(r"showinfo[^]]*\].*?\bn:\s*", re.IGNORECASE)
_TIME_BASE_RE = re.compile(
    r"\bconfig\s+in\s+time_base:\s*(?P<num>-?\d+)\s*/\s*(?P<den>-?\d+)",
    re.IGNORECASE,
)
_FORMAT_DURATION_RE = re.compile(
    r"^\s*Duration:\s*(?P<hours>\d+):(?P<minutes>\d+):"
    r"(?P<seconds>\d+(?:\.\d+)?)\s*,\s*start:\s*(?P<start>[-+0-9.eE]+)",
    re.IGNORECASE,
)
_DECODE_ERROR_RE = re.compile(
    r"(?:error while decoding|corrupt decoded frame|invalid nal|"
    r"decode_slice_header error|concealing \d+ .* errors)",
    re.IGNORECASE,
)
_MISSING_TOKENS = frozenset({"n/a", "na", "nopts", "av_nopts_value", "unknown"})
_TRUTH_SCHEMA_VERSION = 1
_TRUTH_KIND = "mpv_phase0_pts_truth"
_PIXEL_FRAME_SIZE_LIMIT = 64 * 1024 * 1024


class TruthManifestError(ValueError):
    """The optional synthetic-fixture truth manifest is not trustworthy."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _field(line: str, name: str) -> str | None:
    match = re.search(rf"(?:^|\s){re.escape(name)}:\s*([^\s]+)", line)
    return match.group(1) if match else None


def _parse_int(value: str | None) -> int | None:
    if value is None or value.strip().lower() in _MISSING_TOKENS:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_float(value: str | None) -> float | None:
    if value is None or value.strip().lower() in _MISSING_TOKENS:
        return None
    try:
        result = float(value)
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def _append_bounded(values: list[dict[str, Any]], value: dict[str, Any], limit: int) -> None:
    if len(values) < limit:
        values.append(value)


class FfmpegExecutable(NamedTuple):
    path: Path
    source: str


class ShowinfoAnalyzer:
    """Streaming accumulator for FFmpeg showinfo evidence."""

    def __init__(self, *, example_limit: int = 20) -> None:
        if example_limit < 1:
            raise ValueError("example_limit must be at least 1")
        self.example_limit = example_limit
        self.frame_lines = 0
        self.parsed_frames = 0
        self.malformed_frame_lines = 0
        self.first_frame: dict[str, Any] | None = None
        self.last_frame: dict[str, Any] | None = None
        self.pts_table: list[dict[str, Any]] = []

        self.time_bases: list[tuple[int, int]] = []
        self._time_base_set: set[tuple[int, int]] = set()
        self.frames_before_time_base = 0
        self.pixel_formats: set[str] = set()
        self.pixel_format_missing = 0
        self.format_duration_seconds: float | None = None
        self.format_start_seconds: float | None = None

        self.expected_n = 0
        self.first_n: int | None = None
        self.last_n: int | None = None
        self._seen_n: set[int] = set()
        self.frame_index_discontinuities = 0
        self.frame_index_duplicates = 0

        self.pts_present = 0
        self.pts_missing = 0
        self.pts_time_present = 0
        self.pts_time_missing = 0
        self._seen_pts: dict[int, int] = {}
        self.duplicate_pts = 0
        self.duplicate_pts_distinct_checksum = 0
        self.duplicate_pts_same_checksum = 0
        self.duplicate_pts_unknown_checksum = 0
        self.non_monotonic_pts = 0
        self.non_monotonic_pts_time = 0
        self.pts_time_mismatches = 0
        self._previous_pts: tuple[int, int] | None = None
        self._previous_pts_time: tuple[int, float] | None = None
        self.first_pts: int | None = None
        self.last_pts: int | None = None
        self.first_pts_time: float | None = None
        self.last_pts_time: float | None = None
        self.min_pts_time: float | None = None
        self.max_pts_time: float | None = None
        self.pts_steps: Counter[int] = Counter()
        self.first_pts_conflict_n: int | None = None
        self.last_pts_conflict_n: int | None = None
        self._seen_pts_checksum: dict[int, str | None] = {}

        self.duration_present = 0
        self.duration_missing = 0
        self.duration_time_present = 0
        self.duration_time_missing = 0
        self.non_positive_durations = 0
        self.duration_time_mismatches = 0
        self.duration_time_sum = 0.0
        self.min_duration_time: float | None = None
        self.max_duration_time: float | None = None
        self.min_frame_start: float | None = None
        self.max_frame_end: float | None = None

        self.decode_error_lines = 0
        self.examples: dict[str, list[dict[str, Any]]] = {
            "malformed_frames": [],
            "frame_index_discontinuities": [],
            "duplicate_pts": [],
            "non_monotonic_pts": [],
            "missing_pts": [],
            "pts_time_mismatches": [],
            "duration_problems": [],
            "decode_errors": [],
        }

    @property
    def time_base(self) -> tuple[int, int] | None:
        if len(self._time_base_set) != 1:
            return None
        numerator, denominator = next(iter(self._time_base_set))
        if numerator <= 0 or denominator <= 0:
            return None
        return numerator, denominator

    def feed(self, raw_line: str) -> None:
        line = raw_line.rstrip("\r\n")

        time_base_match = _TIME_BASE_RE.search(line)
        if time_base_match:
            value = (
                int(time_base_match.group("num")),
                int(time_base_match.group("den")),
            )
            self.time_bases.append(value)
            self._time_base_set.add(value)

        if self.format_duration_seconds is None:
            duration_match = _FORMAT_DURATION_RE.match(line)
            if duration_match:
                self.format_duration_seconds = (
                    int(duration_match.group("hours")) * 3600.0
                    + int(duration_match.group("minutes")) * 60.0
                    + float(duration_match.group("seconds"))
                )
                self.format_start_seconds = float(duration_match.group("start"))

        if _DECODE_ERROR_RE.search(line):
            self.decode_error_lines += 1
            _append_bounded(
                self.examples["decode_errors"],
                {"line": line},
                self.example_limit,
            )

        if not _FRAME_LINE_RE.search(line):
            return

        self.frame_lines += 1
        n_token = _field(line, "n")
        n = _parse_int(n_token)
        if n is None:
            self.malformed_frame_lines += 1
            _append_bounded(
                self.examples["malformed_frames"],
                {"line": line},
                self.example_limit,
            )
            return

        self.parsed_frames += 1
        if not self._time_base_set:
            self.frames_before_time_base += 1
        if self.first_n is None:
            self.first_n = n
        self.last_n = n

        if n != self.expected_n:
            self.frame_index_discontinuities += 1
            _append_bounded(
                self.examples["frame_index_discontinuities"],
                {"expected": self.expected_n, "actual": n},
                self.example_limit,
            )
        self.expected_n = n + 1
        if n in self._seen_n:
            self.frame_index_duplicates += 1
        self._seen_n.add(n)

        pts_token = _field(line, "pts")
        pts_time_token = _field(line, "pts_time")
        duration_token = _field(line, "duration")
        duration_time_token = _field(line, "duration_time")
        pts = _parse_int(pts_token)
        pts_time = _parse_float(pts_time_token)
        duration = _parse_int(duration_token)
        duration_time = _parse_float(duration_time_token)
        checksum = _field(line, "checksum")
        pixel_format = _field(line, "fmt")
        if pixel_format:
            self.pixel_formats.add(pixel_format)
        else:
            self.pixel_format_missing += 1
        frame_type = _field(line, "type")
        is_keyframe = _parse_int(_field(line, "iskey"))
        row = {
            "n": n,
            "pts": pts,
            "pts_time": pts_time,
            "duration": duration,
            "duration_time": duration_time,
            "checksum": checksum,
            "pixel_format": pixel_format,
            "frame_type": frame_type,
            "is_keyframe": is_keyframe,
        }
        self.pts_table.append(dict(row))
        if self.first_frame is None:
            self.first_frame = dict(row)
        self.last_frame = dict(row)

        self._consume_pts(n, pts, pts_time, checksum)
        self._consume_duration(n, pts_time, duration, duration_time)

    def _note_pts_conflict(self, n: int) -> None:
        if self.first_pts_conflict_n is None:
            self.first_pts_conflict_n = n
        self.last_pts_conflict_n = n

    def _consume_pts(
        self,
        n: int,
        pts: int | None,
        pts_time: float | None,
        checksum: str | None,
    ) -> None:
        if pts is None:
            self.pts_missing += 1
            _append_bounded(
                self.examples["missing_pts"],
                {"n": n, "field": "pts"},
                self.example_limit,
            )
        else:
            self.pts_present += 1
            if self.first_pts is None:
                self.first_pts = pts
            self.last_pts = pts
            first_seen_n = self._seen_pts.get(pts)
            if first_seen_n is not None:
                self.duplicate_pts += 1
                self._note_pts_conflict(n)
                first_checksum = self._seen_pts_checksum.get(pts)
                if checksum is None or first_checksum is None:
                    checksum_relation = "unknown"
                    self.duplicate_pts_unknown_checksum += 1
                elif checksum == first_checksum:
                    checksum_relation = "same"
                    self.duplicate_pts_same_checksum += 1
                else:
                    checksum_relation = "distinct"
                    self.duplicate_pts_distinct_checksum += 1
                _append_bounded(
                    self.examples["duplicate_pts"],
                    {
                        "pts": pts,
                        "first_n": first_seen_n,
                        "n": n,
                        "first_checksum": first_checksum,
                        "checksum": checksum,
                        "checksum_relation": checksum_relation,
                    },
                    self.example_limit,
                )
            else:
                self._seen_pts[pts] = n
                self._seen_pts_checksum[pts] = checksum
            if self._previous_pts is not None:
                previous_n, previous_pts = self._previous_pts
                delta = pts - previous_pts
                self.pts_steps[delta] += 1
                if delta < 0:
                    self.non_monotonic_pts += 1
                    self._note_pts_conflict(n)
                    _append_bounded(
                        self.examples["non_monotonic_pts"],
                        {
                            "previous_n": previous_n,
                            "previous_pts": previous_pts,
                            "n": n,
                            "pts": pts,
                        },
                        self.example_limit,
                    )
            self._previous_pts = (n, pts)

        if pts_time is None:
            self.pts_time_missing += 1
            _append_bounded(
                self.examples["missing_pts"],
                {"n": n, "field": "pts_time"},
                self.example_limit,
            )
        else:
            self.pts_time_present += 1
            if self.first_pts_time is None:
                self.first_pts_time = pts_time
            self.last_pts_time = pts_time
            self.min_pts_time = (
                pts_time if self.min_pts_time is None else min(self.min_pts_time, pts_time)
            )
            self.max_pts_time = (
                pts_time if self.max_pts_time is None else max(self.max_pts_time, pts_time)
            )
            if self._previous_pts_time is not None:
                previous_n, previous_time = self._previous_pts_time
                if pts_time < previous_time:
                    self.non_monotonic_pts_time += 1
                    _append_bounded(
                        self.examples["non_monotonic_pts"],
                        {
                            "previous_n": previous_n,
                            "previous_pts_time": previous_time,
                            "n": n,
                            "pts_time": pts_time,
                        },
                        self.example_limit,
                    )
            self._previous_pts_time = (n, pts_time)

        time_base = self.time_base
        if pts is not None and pts_time is not None and time_base is not None:
            expected = pts * time_base[0] / time_base[1]
            if not math.isclose(pts_time, expected, rel_tol=5e-6, abs_tol=5e-7):
                self.pts_time_mismatches += 1
                _append_bounded(
                    self.examples["pts_time_mismatches"],
                    {"n": n, "pts_time": pts_time, "expected": expected},
                    self.example_limit,
                )

    def _consume_duration(
        self,
        n: int,
        pts_time: float | None,
        duration: int | None,
        duration_time: float | None,
    ) -> None:
        if duration is None:
            self.duration_missing += 1
            _append_bounded(
                self.examples["duration_problems"],
                {"n": n, "field": "duration", "problem": "missing"},
                self.example_limit,
            )
        else:
            self.duration_present += 1
            if duration <= 0:
                self.non_positive_durations += 1
                _append_bounded(
                    self.examples["duration_problems"],
                    {"n": n, "field": "duration", "value": duration},
                    self.example_limit,
                )

        if duration_time is None:
            self.duration_time_missing += 1
            _append_bounded(
                self.examples["duration_problems"],
                {"n": n, "field": "duration_time", "problem": "missing"},
                self.example_limit,
            )
        else:
            self.duration_time_present += 1
            self.duration_time_sum += duration_time
            self.min_duration_time = (
                duration_time
                if self.min_duration_time is None
                else min(self.min_duration_time, duration_time)
            )
            self.max_duration_time = (
                duration_time
                if self.max_duration_time is None
                else max(self.max_duration_time, duration_time)
            )
            if duration_time <= 0:
                self.non_positive_durations += 1
                _append_bounded(
                    self.examples["duration_problems"],
                    {"n": n, "field": "duration_time", "value": duration_time},
                    self.example_limit,
                )
            if pts_time is not None:
                end = pts_time + duration_time
                self.min_frame_start = (
                    pts_time
                    if self.min_frame_start is None
                    else min(self.min_frame_start, pts_time)
                )
                self.max_frame_end = (
                    end if self.max_frame_end is None else max(self.max_frame_end, end)
                )

        time_base = self.time_base
        if duration is not None and duration_time is not None and time_base is not None:
            expected = duration * time_base[0] / time_base[1]
            if not math.isclose(duration_time, expected, rel_tol=5e-6, abs_tol=5e-7):
                self.duration_time_mismatches += 1
                _append_bounded(
                    self.examples["duration_problems"],
                    {
                        "n": n,
                        "field": "duration_time",
                        "value": duration_time,
                        "expected": expected,
                    },
                    self.example_limit,
                )

    def finish(
        self,
        *,
        ffmpeg_returncode: int,
        partial_scan: bool = False,
    ) -> dict[str, Any]:
        reasons: list[str] = []
        if ffmpeg_returncode != 0:
            reasons.append("FFMPEG_DECODE_FAILED")
        if partial_scan:
            reasons.append("PARTIAL_SCAN")
        if self.frame_lines == 0 or self.parsed_frames == 0:
            reasons.append("NO_DECODED_FRAMES")
        if self.malformed_frame_lines:
            reasons.append("MALFORMED_SHOWINFO_FRAME")
        if (
            self.first_n != 0
            or self.frame_index_discontinuities
            or self.frame_index_duplicates
            or len(self._seen_n) != self.parsed_frames
        ):
            reasons.append("FRAME_INDEX_MISSING_OR_OUT_OF_ORDER")
        if self.pts_missing:
            reasons.append("PTS_MISSING")
        if self.pts_time_missing:
            reasons.append("PTS_TIME_MISSING")
        if self.duplicate_pts:
            reasons.append("PTS_DUPLICATE")
        if self.non_monotonic_pts or self.non_monotonic_pts_time:
            reasons.append("PTS_NON_MONOTONIC")
        if not self._time_base_set:
            reasons.append("TIME_BASE_MISSING")
        elif self.time_base is None:
            reasons.append("TIME_BASE_INVALID_OR_CHANGED")
        if self.frames_before_time_base:
            reasons.append("TIME_BASE_AFTER_FRAMES")
        if self.pts_time_mismatches:
            reasons.append("PTS_TIME_MISMATCH")
        if self.duration_missing or self.duration_time_missing:
            reasons.append("FRAME_DURATION_MISSING")
        if self.non_positive_durations:
            reasons.append("FRAME_DURATION_NON_POSITIVE")
        if self.duration_time_mismatches:
            reasons.append("DURATION_TIME_MISMATCH")
        if self.pixel_format_missing:
            reasons.append("PIXEL_FORMAT_MISSING")
        if len(self.pixel_formats) != 1:
            reasons.append("PIXEL_FORMAT_INVALID_OR_CHANGED")
        if self.decode_error_lines:
            reasons.append("DECODE_DIAGNOSTIC_ERROR")

        reasons = list(dict.fromkeys(reasons))
        process_ok = ffmpeg_returncode == 0
        authoritative = process_ok and not reasons
        if not process_ok:
            status = "ERROR"
        elif authoritative:
            status = "PASS"
        else:
            status = "BLOCKED"

        time_base = self.time_base
        time_base_record = None
        if time_base is not None:
            time_base_record = {
                "numerator": time_base[0],
                "denominator": time_base[1],
                "text": f"{time_base[0]}/{time_base[1]}",
            }

        ordered_span = None
        if self.first_pts_time is not None and self.last_pts_time is not None:
            ordered_span = self.last_pts_time - self.first_pts_time
        decoded_extent = None
        if self.min_frame_start is not None and self.max_frame_end is not None:
            decoded_extent = self.max_frame_end - self.min_frame_start

        if self.duplicate_pts or self.non_monotonic_pts or self.non_monotonic_pts_time:
            conflict_status = "conflict"
            automatic_repair_safe: bool | None = False
            recommended_action = "normalize_to_versioned_proxy_or_reject_source"
        elif self.pts_missing or self.pts_time_missing or not self._time_base_set:
            conflict_status = "indeterminate"
            automatic_repair_safe = None
            recommended_action = "collect_complete_pts_evidence"
        else:
            conflict_status = "clean_in_observed_scope" if partial_scan else "clean"
            automatic_repair_safe = None
            recommended_action = "no_pts_repair_needed"

        return {
            "status": status,
            "authoritative_frame_timeline": authoritative,
            "reason_codes": reasons,
            "showinfo": {
                "frame_lines": self.frame_lines,
                "parsed_frames": self.parsed_frames,
                "malformed_frame_lines": self.malformed_frame_lines,
                "first_frame": self.first_frame,
                "last_frame": self.last_frame,
                "pixel_formats": sorted(self.pixel_formats),
                "pixel_format_missing_count": self.pixel_format_missing,
                "frame_index": {
                    "first": self.first_n,
                    "last": self.last_n,
                    "unique_count": len(self._seen_n),
                    "discontinuity_count": self.frame_index_discontinuities,
                    "duplicate_count": self.frame_index_duplicates,
                    "contiguous_from_zero": (
                        self.parsed_frames > 0
                        and self.first_n == 0
                        and self.frame_index_discontinuities == 0
                        and self.frame_index_duplicates == 0
                    ),
                },
                "time_base": time_base_record,
                "observed_time_bases": [f"{n}/{d}" for n, d in self.time_bases],
                "frames_before_time_base": self.frames_before_time_base,
                "pts": {
                    "present_count": self.pts_present,
                    "missing_count": self.pts_missing,
                    "unique_count": len(self._seen_pts),
                    "duplicate_count": self.duplicate_pts,
                    "duplicate_distinct_checksum_count": self.duplicate_pts_distinct_checksum,
                    "duplicate_same_checksum_count": self.duplicate_pts_same_checksum,
                    "duplicate_unknown_checksum_count": self.duplicate_pts_unknown_checksum,
                    "non_monotonic_count": self.non_monotonic_pts,
                    "strictly_increasing": (
                        self.pts_present == self.parsed_frames
                        and self.duplicate_pts == 0
                        and self.non_monotonic_pts == 0
                    ),
                    "first": self.first_pts,
                    "last": self.last_pts,
                    "common_steps": [
                        {"pts_delta": delta, "count": count}
                        for delta, count in self.pts_steps.most_common(20)
                    ],
                },
                "pts_time": {
                    "present_count": self.pts_time_present,
                    "missing_count": self.pts_time_missing,
                    "non_monotonic_count": self.non_monotonic_pts_time,
                    "time_base_mismatch_count": self.pts_time_mismatches,
                    "first_seconds": self.first_pts_time,
                    "last_seconds": self.last_pts_time,
                    "minimum_seconds": self.min_pts_time,
                    "maximum_seconds": self.max_pts_time,
                },
            },
            "pts_table": list(self.pts_table),
            "pts_conflict_assessment": {
                "status": conflict_status,
                "scan_scope": "partial" if partial_scan else "complete",
                "first_conflict_frame_n": self.first_pts_conflict_n,
                "last_conflict_frame_n": self.last_pts_conflict_n,
                "automatic_repair_safe": automatic_repair_safe,
                "automatic_sort_or_deduplicate_allowed": False,
                "recommended_action": recommended_action,
            },
            "duration": {
                "format_seconds": self.format_duration_seconds,
                "format_start_seconds": self.format_start_seconds,
                "frame_duration_present_count": self.duration_present,
                "frame_duration_missing_count": self.duration_missing,
                "frame_duration_time_present_count": self.duration_time_present,
                "frame_duration_time_missing_count": self.duration_time_missing,
                "non_positive_count": self.non_positive_durations,
                "time_base_mismatch_count": self.duration_time_mismatches,
                "sum_frame_duration_seconds": self.duration_time_sum,
                "minimum_frame_duration_seconds": self.min_duration_time,
                "maximum_frame_duration_seconds": self.max_duration_time,
                "ordered_first_to_last_pts_seconds": ordered_span,
                "decoded_presentation_extent_seconds": decoded_extent,
            },
            "decode_diagnostic_error_count": self.decode_error_lines,
            "examples": self.examples,
        }


def resolve_ffmpeg(requested: Path | None = None) -> FfmpegExecutable:
    if requested is not None:
        path = requested.expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"FFmpeg executable not found: {path}")
        return FfmpegExecutable(path=path, source="--ffmpeg")

    try:
        import imageio_ffmpeg  # type: ignore

        path = Path(imageio_ffmpeg.get_ffmpeg_exe()).resolve()
        if path.is_file():
            return FfmpegExecutable(path=path, source="imageio_ffmpeg")
    except Exception:
        pass

    discovered = shutil.which("ffmpeg")
    if discovered:
        return FfmpegExecutable(path=Path(discovered).resolve(), source="PATH")
    raise FileNotFoundError(
        "FFmpeg not found via imageio_ffmpeg or PATH; pass --ffmpeg explicitly"
    )


def _validate_threads(threads: int) -> int:
    if isinstance(threads, bool) or not isinstance(threads, int) or threads < 1:
        raise ValueError("threads must be a positive integer")
    return threads


def _validate_max_frames(max_frames: int | None) -> int | None:
    if max_frames is None:
        return None
    if isinstance(max_frames, bool) or not isinstance(max_frames, int) or max_frames < 1:
        raise ValueError("max_frames must be a positive integer")
    return max_frames


def build_ffmpeg_command(
    ffmpeg: Path,
    video: Path,
    *,
    threads: int = 1,
    max_frames: int | None = None,
) -> list[str]:
    threads = _validate_threads(threads)
    max_frames = _validate_max_frames(max_frames)
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "info",
        "-copyts",
        "-threads",
        str(threads),
        "-i",
        str(video),
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
        "-vf",
        "showinfo",
        "-fps_mode",
        "passthrough",
    ]
    if max_frames is not None:
        command.extend(["-frames:v", str(max_frames)])
    command.extend(["-f", "null", "-"])
    return command


def build_truth_ffmpeg_command(
    ffmpeg: Path,
    video: Path,
    raw_output: Path,
    *,
    threads: int = 1,
    max_frames: int | None = None,
) -> list[str]:
    """Decode once, keeping showinfo and the corresponding gray pixels."""
    threads = _validate_threads(threads)
    max_frames = _validate_max_frames(max_frames)
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "info",
        "-copyts",
        "-threads",
        str(threads),
        "-i",
        str(video),
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
        "-vf",
        "showinfo,format=gray",
        "-fps_mode",
        "passthrough",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
    ]
    if max_frames is not None:
        command.extend(["-frames:v", str(max_frames)])
    command.append(str(raw_output))
    return command


def _require_truth_int(value: Any, name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TruthManifestError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise TruthManifestError(f"{name} must be >= {minimum}")
    return value


def _require_truth_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TruthManifestError(f"{name} must be an object")
    return value


def load_truth_manifest(path: Path, video: Path) -> dict[str, Any]:
    """Validate a fixture truth file before any frame evidence is accepted."""
    path = path.expanduser().resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TruthManifestError(f"cannot read truth manifest {path}: {exc}") from exc
    root = _require_truth_mapping(raw, "truth manifest")
    if root.get("schema_version") != _TRUTH_SCHEMA_VERSION:
        raise TruthManifestError("unsupported truth manifest schema_version")
    if root.get("kind") != _TRUTH_KIND:
        raise TruthManifestError("unexpected truth manifest kind")

    video_record = _require_truth_mapping(root.get("video"), "video")
    declared_path = video_record.get("path")
    if not isinstance(declared_path, str) or not declared_path:
        raise TruthManifestError("video.path must be a non-empty string")
    declared = Path(declared_path).expanduser()
    if not declared.is_absolute():
        declared = path.parent / declared
    if declared.resolve() != video.expanduser().resolve():
        raise TruthManifestError("truth manifest video.path does not match the CLI video")
    expected_hash = video_record.get("sha256")
    if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash):
        raise TruthManifestError("video.sha256 must be a SHA-256 hex digest")
    actual_hash = _sha256_file(video)
    if actual_hash.lower() != expected_hash.lower():
        raise TruthManifestError("truth manifest video.sha256 does not match the video")
    width = _require_truth_int(video_record.get("width"), "video.width", minimum=1)
    height = _require_truth_int(video_record.get("height"), "video.height", minimum=1)
    if video_record.get("pixel_format") != "gray":
        raise TruthManifestError("truth fixtures must declare gray pixel_format")
    if width * height > _PIXEL_FRAME_SIZE_LIMIT:
        raise TruthManifestError("truth fixture frame is too large")

    time_base = _require_truth_mapping(root.get("time_base"), "time_base")
    tb_num = _require_truth_int(time_base.get("numerator"), "time_base.numerator", minimum=1)
    tb_den = _require_truth_int(time_base.get("denominator"), "time_base.denominator", minimum=1)

    encoding = _require_truth_mapping(root.get("id_encoding"), "id_encoding")
    bit_count = _require_truth_int(encoding.get("bit_count"), "id_encoding.bit_count", minimum=1)
    threshold = encoding.get("threshold", 128)
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise TruthManifestError("id_encoding.threshold must be numeric")
    if not 0 <= float(threshold) <= 255:
        raise TruthManifestError("id_encoding.threshold must be in [0, 255]")
    marker_bits = encoding.get("marker_bits")
    if not isinstance(marker_bits, list) or not marker_bits:
        raise TruthManifestError("id_encoding.marker_bits must be a non-empty list")
    if any(bit not in (0, 1) for bit in marker_bits):
        raise TruthManifestError("id_encoding.marker_bits must contain only 0 or 1")
    for key in ("marker_x", "marker_y", "marker_block_width", "marker_block_height", "bit_x", "bit_y", "bit_width", "bit_height", "complement_y"):
        _require_truth_int(encoding.get(key), f"id_encoding.{key}", minimum=0)
    if encoding["bit_width"] == 0 or encoding["bit_height"] == 0:
        raise TruthManifestError("id_encoding bit blocks must be non-empty")
    if encoding["marker_block_width"] == 0 or encoding["marker_block_height"] == 0:
        raise TruthManifestError("id_encoding marker blocks must be non-empty")
    marker_right = encoding["marker_x"] + len(marker_bits) * encoding["marker_block_width"]
    bit_right = encoding["bit_x"] + bit_count * encoding["bit_width"]
    if marker_right > width or encoding["marker_y"] + encoding["marker_block_height"] > height:
        raise TruthManifestError("marker layout exceeds video dimensions")
    if bit_right > width or encoding["bit_y"] + encoding["bit_height"] > height:
        raise TruthManifestError("bit layout exceeds video dimensions")
    if bit_right > width or encoding["complement_y"] + encoding["bit_height"] > height:
        raise TruthManifestError("complement layout exceeds video dimensions")

    frames = root.get("frames")
    if not isinstance(frames, list) or not frames:
        raise TruthManifestError("frames must be a non-empty list")
    normalized_frames: list[dict[str, int]] = []
    seen_ids: set[int] = set()
    seen_orders: set[int] = set()
    for index, record_value in enumerate(frames):
        record = _require_truth_mapping(record_value, f"frames[{index}]")
        order = _require_truth_int(record.get("order"), f"frames[{index}].order", minimum=0)
        frame_id = _require_truth_int(record.get("frame_id"), f"frames[{index}].frame_id", minimum=0)
        pts_ticks = _require_truth_int(record.get("pts_ticks"), f"frames[{index}].pts_ticks")
        duration_ticks = _require_truth_int(record.get("duration_ticks"), f"frames[{index}].duration_ticks", minimum=1)
        if order != index or order in seen_orders:
            raise TruthManifestError("frames.order must be contiguous and unique")
        if frame_id in seen_ids:
            raise TruthManifestError("frames.frame_id must be unique")
        if index and pts_ticks <= normalized_frames[-1]["pts_ticks"]:
            raise TruthManifestError("frames.pts_ticks must be strictly increasing")
        seen_orders.add(order)
        seen_ids.add(frame_id)
        normalized_frames.append(
            {
                "order": order,
                "frame_id": frame_id,
                "pts_ticks": pts_ticks,
                "duration_ticks": duration_ticks,
            }
        )

    guard_value = root.get("terminal_guard")
    guard: dict[str, Any] | None = None
    if guard_value is not None:
        guard_record = _require_truth_mapping(guard_value, "terminal_guard")
        guard_id = _require_truth_int(guard_record.get("frame_id"), "terminal_guard.frame_id", minimum=0)
        guard_pts = _require_truth_int(guard_record.get("pts_ticks"), "terminal_guard.pts_ticks")
        if guard_id in seen_ids:
            raise TruthManifestError("terminal_guard.frame_id duplicates a truth frame")
        if guard_pts <= normalized_frames[-1]["pts_ticks"]:
            raise TruthManifestError("terminal_guard.pts_ticks must follow truth frames")
        guard = {
            "frame_id": guard_id,
            "pts_ticks": guard_pts,
            "may_be_present": bool(guard_record.get("may_be_present", True)),
        }

    return {
        "path": str(path),
        "manifest_sha256": _sha256_file(path),
        "video": {
            "path": str(video.expanduser().resolve()),
            "sha256": actual_hash,
            "width": width,
            "height": height,
            "pixel_format": "gray",
        },
        "time_base": {"numerator": tb_num, "denominator": tb_den},
        "id_encoding": dict(encoding),
        "frames": normalized_frames,
        "terminal_guard": guard,
    }


def _block_mean(raw: bytes, width: int, x: int, y: int, block_width: int, block_height: int) -> float:
    total = 0
    count = block_width * block_height
    for row in range(y, y + block_height):
        start = row * width + x
        total += sum(raw[start : start + block_width])
    return total / count


def decode_truth_frame_id(
    raw: bytes,
    *,
    width: int,
    height: int,
    encoding: dict[str, Any],
) -> tuple[int | None, list[str]]:
    if len(raw) != width * height:
        return None, ["TRUTH_RAW_FRAME_SIZE_MISMATCH"]
    threshold = float(encoding.get("threshold", 128))
    marker_bits = encoding["marker_bits"]
    problems: list[str] = []
    for index, expected in enumerate(marker_bits):
        x = encoding["marker_x"] + index * encoding["marker_block_width"]
        mean = _block_mean(
            raw,
            width,
            x,
            encoding["marker_y"],
            encoding["marker_block_width"],
            encoding["marker_block_height"],
        )
        if int(mean >= threshold) != expected:
            problems.append(f"marker[{index}]")

    frame_id = 0
    for index in range(encoding["bit_count"]):
        x = encoding["bit_x"] + index * encoding["bit_width"]
        top = _block_mean(raw, width, x, encoding["bit_y"], encoding["bit_width"], encoding["bit_height"])
        complement = _block_mean(
            raw,
            width,
            x,
            encoding["complement_y"],
            encoding["bit_width"],
            encoding["bit_height"],
        )
        bit = int(top >= threshold)
        if int(complement >= threshold) == bit:
            problems.append(f"complement[{index}]")
        frame_id |= bit << index
    return (None if problems else frame_id), problems


def _truth_alignment(
    truth: dict[str, Any],
    pts_table: Sequence[dict[str, Any]],
    decoded_frame_ids: Sequence[int | None],
) -> tuple[dict[str, Any], list[str]]:
    expected = truth["frames"]
    expected_ids = [row["frame_id"] for row in expected]
    decoded_ids = list(decoded_frame_ids)
    guard = truth.get("terminal_guard")
    guard_id = guard["frame_id"] if guard else None
    guard_observed = bool(guard and decoded_ids == expected_ids + [guard_id])
    ids_without_guard = decoded_ids[:-1] if guard_observed else decoded_ids
    id_order_ok = ids_without_guard == expected_ids and (
        not guard or not guard["may_be_present"] or guard_observed or guard_id not in decoded_ids
    )
    duplicate_ids = sorted({value for value in decoded_ids if value is not None and decoded_ids.count(value) > 1})
    missing_ids = [value for value in expected_ids if value not in decoded_ids]
    unexpected_ids = [value for value in decoded_ids if value not in expected_ids and value != guard_id]
    reasons: list[str] = []
    if any(value is None for value in decoded_ids):
        reasons.append("TRUTH_FRAME_ID_DECODE_FAILED")
    if len(decoded_ids) not in (len(expected_ids), len(expected_ids) + (1 if guard and guard["may_be_present"] else 0)):
        reasons.append("TRUTH_RAW_FRAME_COUNT_MISMATCH")
    if duplicate_ids:
        reasons.append("TRUTH_FRAME_ID_DUPLICATE")
    if missing_ids or unexpected_ids or not id_order_ok:
        reasons.append("TRUTH_FRAME_ID_MISSING_OR_OUT_OF_ORDER")

    pts_mismatches: list[dict[str, Any]] = []
    duration_mismatches: list[dict[str, Any]] = []
    tb_num = truth["time_base"]["numerator"]
    tb_den = truth["time_base"]["denominator"]
    compare_count = min(len(expected), len(pts_table), len(ids_without_guard))
    for index in range(compare_count):
        actual = pts_table[index]
        target = expected[index]
        if actual.get("pts") != target["pts_ticks"]:
            pts_mismatches.append({"order": index, "expected": target["pts_ticks"], "actual": actual.get("pts")})
        if actual.get("duration") != target["duration_ticks"]:
            duration_mismatches.append(
                {"order": index, "expected": target["duration_ticks"], "actual": actual.get("duration")}
            )
        expected_pts_time = target["pts_ticks"] * tb_num / tb_den
        expected_duration_time = target["duration_ticks"] * tb_num / tb_den
        if actual.get("pts_time") is None or not math.isclose(
            actual["pts_time"], expected_pts_time, rel_tol=5e-6, abs_tol=5e-7
        ):
            pts_mismatches.append(
                {"order": index, "field": "pts_time", "expected": expected_pts_time, "actual": actual.get("pts_time")}
            )
        if actual.get("duration_time") is None or not math.isclose(
            actual["duration_time"], expected_duration_time, rel_tol=5e-6, abs_tol=5e-7
        ):
            duration_mismatches.append(
                {
                    "order": index,
                    "field": "duration_time",
                    "expected": expected_duration_time,
                    "actual": actual.get("duration_time"),
                }
            )
    if pts_mismatches:
        reasons.append("TRUTH_PTS_MISMATCH")
    if duration_mismatches:
        reasons.append("TRUTH_DURATION_MISMATCH")
    reasons = list(dict.fromkeys(reasons))
    return (
        {
            "status": "PASS" if not reasons else "BLOCKED",
            "expected_frame_count": len(expected_ids),
            "observed_frame_count": len(decoded_ids),
            "expected_frame_ids": expected_ids,
            "decoded_frame_ids": decoded_ids,
            "terminal_guard_frame_id": guard_id,
            "terminal_guard_observed": guard_observed,
            "missing_frame_ids": missing_ids,
            "unexpected_frame_ids": unexpected_ids,
            "duplicate_frame_ids": duplicate_ids,
            "order_ok": id_order_ok,
            "pts_mismatches": pts_mismatches,
            "duration_mismatches": duration_mismatches,
            "reason_codes": reasons,
        },
        reasons,
    )


def _read_truth_pixels(
    raw_path: Path,
    *,
    truth: dict[str, Any],
    expected_frame_count: int,
) -> tuple[list[int | None], list[str]]:
    width = truth["video"]["width"]
    height = truth["video"]["height"]
    frame_size = width * height
    raw = raw_path.read_bytes()
    if len(raw) % frame_size:
        return [], ["TRUTH_RAW_FRAME_SIZE_MISMATCH"]
    count = len(raw) // frame_size
    if count < expected_frame_count:
        return [], ["TRUTH_RAW_FRAME_COUNT_MISMATCH"]
    frame_ids: list[int | None] = []
    decode_problems: list[str] = []
    encoding = truth["id_encoding"]
    for index in range(count):
        start = index * frame_size
        frame_id, problems = decode_truth_frame_id(
            raw[start : start + frame_size],
            width=width,
            height=height,
            encoding=encoding,
        )
        frame_ids.append(frame_id)
        if problems:
            decode_problems.append(f"frame[{index}]:" + ",".join(problems))
    return frame_ids, decode_problems


def probe_video(
    video: Path,
    ffmpeg: FfmpegExecutable,
    *,
    example_limit: int = 20,
    truth_manifest: Path | None = None,
    threads: int = 1,
    max_frames: int | None = None,
    cancel_check: Callable[[], Any] | None = None,
) -> tuple[dict[str, Any], int]:
    threads = _validate_threads(threads)
    max_frames = _validate_max_frames(max_frames)
    video = video.expanduser().resolve()
    if not video.is_file():
        raise FileNotFoundError(f"video not found: {video}")
    truth: dict[str, Any] | None = None
    if truth_manifest is not None:
        try:
            truth = load_truth_manifest(truth_manifest, video)
        except TruthManifestError as exc:
            command = build_ffmpeg_command(
                ffmpeg.path, video, threads=threads, max_frames=max_frames
            )
            report = _base_report(video, ffmpeg, command)
            report.update(
                {
                    "status": "BLOCKED",
                    "authoritative_frame_timeline": False,
                    "reason_codes": ["TRUTH_MANIFEST_INVALID"],
                    "truth_manifest": {
                        "path": str(truth_manifest.expanduser().resolve()),
                        "status": "INVALID",
                        "error": str(exc),
                    },
                    "exit_code": EXIT_BLOCKED,
                }
            )
            return report, EXIT_BLOCKED

    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    raw_output: Path | None = None
    if truth is not None:
        temp_dir = tempfile.TemporaryDirectory(prefix="mpv_pts_oracle_")
        raw_output = Path(temp_dir.name) / "decoded.gray"
        command = build_truth_ffmpeg_command(
            ffmpeg.path,
            video,
            raw_output,
            threads=threads,
            max_frames=max_frames,
        )
    else:
        command = build_ffmpeg_command(
            ffmpeg.path, video, threads=threads, max_frames=max_frames
        )
    analyzer = ShowinfoAnalyzer(example_limit=example_limit)
    if cancel_check is not None:
        cancel_check()

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        report = _base_report(video, ffmpeg, command)
        report.update(
            {
                "status": "ERROR",
                "authoritative_frame_timeline": False,
                "reason_codes": ["FFMPEG_START_FAILED"],
                "error": repr(exc),
                "exit_code": EXIT_FFMPEG_FAILED,
            }
        )
        if temp_dir is not None:
            temp_dir.cleanup()
        return report, EXIT_FFMPEG_FAILED

    assert process.stderr is not None
    # Reading ``for line in process.stderr`` directly makes cancellation
    # hostage to FFmpeg producing another line.  A daemon reader isolates the
    # blocking pipe; the worker polls a queue and can terminate a silent or
    # half-closed child within the normal checkpoint interval.
    stderr_queue: queue.Queue[str] = queue.Queue()
    stderr_done = threading.Event()

    def _read_stderr() -> None:
        try:
            assert process.stderr is not None
            for line in process.stderr:
                stderr_queue.put(line)
        finally:
            stderr_done.set()

    stderr_reader = threading.Thread(
        target=_read_stderr,
        name="mpv-pts-oracle-stderr",
        daemon=True,
    )
    stderr_reader.start()
    try:
        while True:
            if cancel_check is not None:
                cancel_check()
            try:
                line = stderr_queue.get(timeout=0.1)
            except queue.Empty:
                if stderr_done.is_set() and process.poll() is not None and stderr_queue.empty():
                    break
                continue
            analyzer.feed(line)
        while process.poll() is None:
            if cancel_check is not None:
                cancel_check()
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                continue
        returncode = process.wait(timeout=0.1)
        if cancel_check is not None:
            cancel_check()
    except BaseException:
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5.0)
        finally:
            try:
                process.stderr.close()
            except OSError:
                pass
            stderr_reader.join(timeout=1.0)
        raise
    finally:
        try:
            process.stderr.close()
        except OSError:
            pass
        stderr_reader.join(timeout=1.0)

    evidence = analyzer.finish(
        ffmpeg_returncode=returncode,
        partial_scan=max_frames is not None,
    )
    if truth is not None and raw_output is not None:
        truth_record = {
            "path": truth["path"],
            "manifest_sha256": truth["manifest_sha256"],
            "video_sha256": truth["video"]["sha256"],
            "expected_frame_count": len(truth["frames"]),
        }
        evidence["truth_manifest"] = truth_record
        try:
            decoded_ids, pixel_problems = _read_truth_pixels(
                raw_output,
                truth=truth,
                expected_frame_count=len(truth["frames"]),
            )
        except OSError as exc:
            decoded_ids = []
            pixel_problems = [f"TRUTH_RAW_READ_FAILED:{exc}"]
        alignment, alignment_reasons = _truth_alignment(
            truth,
            evidence.get("pts_table", []),
            decoded_ids,
        )
        if pixel_problems:
            alignment["pixel_decode_problems"] = pixel_problems[:20]
            alignment["reason_codes"] = list(
                dict.fromkeys(alignment.get("reason_codes", []) + ["TRUTH_FRAME_ID_DECODE_FAILED"])
            )
            alignment["status"] = "BLOCKED"
            alignment_reasons = list(
                dict.fromkeys(alignment_reasons + ["TRUTH_FRAME_ID_DECODE_FAILED"])
            )
        evidence["frame_id_alignment"] = alignment
        if alignment_reasons:
            evidence["reason_codes"] = list(
                dict.fromkeys(evidence.get("reason_codes", []) + alignment_reasons)
            )
            evidence["status"] = "BLOCKED" if returncode == 0 else evidence["status"]
            evidence["authoritative_frame_timeline"] = False

    if returncode != 0:
        exit_code = EXIT_FFMPEG_FAILED
    elif evidence["status"] == "BLOCKED":
        exit_code = EXIT_BLOCKED
    else:
        exit_code = EXIT_PASS

    report = _base_report(video, ffmpeg, command)
    report.update(evidence)
    report["ffmpeg"]["returncode"] = returncode
    report["exit_code"] = exit_code
    if temp_dir is not None:
        temp_dir.cleanup()
    return report, exit_code


def _base_report(
    video: Path,
    ffmpeg: FfmpegExecutable,
    command: Sequence[str],
) -> dict[str, Any]:
    stat = video.stat()
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "mpv_phase0_frame_pts_oracle",
        "created_utc": _utc_now(),
        "method": (
            "FFmpeg software decode with showinfo; no frame/fps timestamp fallback"
        ),
        "video": {
            "path": str(video),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": _sha256_file(video),
        },
        "ffmpeg": {
            "path": str(ffmpeg.path),
            "resolution_source": ffmpeg.source,
            "command": list(command),
            "returncode": None,
        },
    }


def write_json_atomic(path: Path, value: Any) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temp_name = stream.name
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if temp_name is not None:
            try:
                Path(temp_name).unlink()
            except FileNotFoundError:
                pass


def _default_output(video: Path) -> Path:
    repo = Path(__file__).resolve().parents[1]
    return repo / ".cache" / "mpv_spike" / "frame_oracle" / f"{video.stem}.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Decode one video with FFmpeg showinfo and verify its frame/PTS timeline. "
            "No frame/fps fallback is permitted."
        ),
        epilog=(
            "Exit codes: 0=PASS, 10=BLOCKED timestamp evidence, "
            "20=FFmpeg unavailable, 21=FFmpeg failed, 22=report write failed."
        ),
    )
    parser.add_argument("video", type=Path)
    parser.add_argument("--ffmpeg", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--truth-manifest",
        type=Path,
        default=None,
        help="optional synthetic-fixture manifest for pixel ID and exact PTS alignment",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=1,
        help="FFmpeg decode threads; default 1 for the reproducible baseline",
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        default=None,
        help=(
            "diagnostic prefix length; any use forces PARTIAL_SCAN/BLOCKED and "
            "cannot satisfy the authoritative Gate"
        ),
    )
    parser.add_argument("--example-limit", type=int, default=20)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.example_limit < 1:
        parser.error("--example-limit must be at least 1")
    if args.threads < 1:
        parser.error("--threads must be at least 1")
    if args.max_frames is not None and args.max_frames < 1:
        parser.error("--max-frames must be at least 1")

    video = args.video.expanduser().resolve()
    output = (args.output or _default_output(video)).expanduser().resolve()
    if not video.is_file():
        parser.error(f"video not found: {video}")

    try:
        ffmpeg = resolve_ffmpeg(args.ffmpeg)
    except FileNotFoundError as exc:
        report = {
            "schema_version": SCHEMA_VERSION,
            "kind": "mpv_phase0_frame_pts_oracle",
            "created_utc": _utc_now(),
            "status": "ERROR",
            "authoritative_frame_timeline": False,
            "reason_codes": ["FFMPEG_UNAVAILABLE"],
            "video": {"path": str(video)},
            "error": str(exc),
            "exit_code": EXIT_FFMPEG_UNAVAILABLE,
        }
        try:
            write_json_atomic(output, report)
        except OSError as write_exc:
            print(f"cannot write report {output}: {write_exc}", file=sys.stderr)
            return EXIT_OUTPUT_FAILED
        print(str(exc), file=sys.stderr)
        print(f"report: {output}")
        return EXIT_FFMPEG_UNAVAILABLE

    report, exit_code = probe_video(
        video,
        ffmpeg,
        example_limit=args.example_limit,
        truth_manifest=args.truth_manifest,
        threads=args.threads,
        max_frames=args.max_frames,
    )
    try:
        write_json_atomic(output, report)
    except OSError as exc:
        print(f"cannot write report {output}: {exc}", file=sys.stderr)
        return EXIT_OUTPUT_FAILED

    print(
        json.dumps(
            {
                "status": report["status"],
                "authoritative_frame_timeline": report["authoritative_frame_timeline"],
                "reason_codes": report["reason_codes"],
                "parsed_frames": report.get("showinfo", {}).get("parsed_frames", 0),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    print(f"report: {output}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
