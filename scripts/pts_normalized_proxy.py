#!/usr/bin/env python3
"""Build and verify a versioned video-only PTS-normalized proxy.

This Phase 0 tool never treats frame/fps as source media truth. It derives the
proxy cadence from the positive decoded-frame duration recorded by an existing
source oracle, preserves decoded picture order, and appends one cloned terminal
guard outside the business frame domain. The generic frame oracle remains
strict: a decoded zero-duration guard still makes that report BLOCKED. This
tool may qualify the video frame domain only after proving that every real frame
is present, checksum-aligned, and has a positive duration.

Audio and A/V synchronization are intentionally not implemented here. A video
PASS therefore still produces an overall BLOCKED result.
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
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import verify_mpv_frames as frame_oracle


SCHEMA_VERSION = 1
MANIFEST_KIND = "mpv_phase0_pts_normalized_proxy"
REPORT_KIND = "mpv_phase0_pts_normalized_proxy_verification"
EXIT_PASS = 0
EXIT_USAGE = 2
EXIT_BLOCKED = 10
EXIT_FAILED = 20
DEFAULT_PRESET = "veryfast"
_CHECKSUM_RE = re.compile(r"^[0-9A-Fa-f]{8}$")
SOURCE_CONTENT_ANCHOR_SCHEMA_VERSION = 2
SOURCE_CONTENT_ANCHOR_MANIFEST_KIND = "mpv_phase0_source_content_anchor_manifest"


class ProxyEvidenceError(ValueError):
    """The source oracle, proxy manifest, or bound evidence is invalid."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProxyEvidenceError(f"cannot read JSON evidence {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProxyEvidenceError(f"JSON evidence must be an object: {path}")
    return value


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.expanduser().resolve())) == os.path.normcase(
        str(right.expanduser().resolve())
    )


def _require_int(value: Any, name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProxyEvidenceError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ProxyEvidenceError(f"{name} must be at least {minimum}")
    return value


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProxyEvidenceError(f"{name} must be an object")
    return value


def _require_checksum(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _CHECKSUM_RE.fullmatch(value):
        raise ProxyEvidenceError(f"{name} must be an 8-digit showinfo checksum")
    return value.upper()


def _file_record(
    path: Path,
    *,
    include_size: bool = True,
    include_mtime: bool = False,
) -> dict[str, Any]:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    result: dict[str, Any] = {
        "path": str(path),
        "sha256": sha256_file(path),
    }
    if include_size:
        result["size"] = path.stat().st_size
    if include_mtime:
        result["mtime_ns"] = path.stat().st_mtime_ns
    return result


def _parse_utc(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ProxyEvidenceError(f"{name} timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProxyEvidenceError(f"{name} timestamp is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _assert_source_only(value: Any, *, path: str = "source_anchor") -> None:
    """Reject proxy-bearing keys before a source-only manifest is bound."""
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ProxyEvidenceError(f"{path} contains a non-string field name")
            if "proxy" in key.casefold():
                raise ProxyEvidenceError(
                    f"source anchor is not source-only: {path}.{key}"
                )
            _assert_source_only(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_source_only(child, path=f"{path}[{index}]")


def _validate_source_anchor_manifest(
    manifest_path: Path,
    source_path: Path,
    source_oracle_path: Path,
    *,
    expected_scope: str,
    required_source_start: int,
    required_source_end: int,
) -> dict[str, Any]:
    """Validate and snapshot the source-first registration before generation."""
    manifest_path = manifest_path.expanduser().resolve()
    source_path = source_path.expanduser().resolve()
    source_oracle_path = source_oracle_path.expanduser().resolve()
    record = _file_record(
        manifest_path,
        include_size=True,
        include_mtime=True,
    )
    payload = _load_json(manifest_path)
    if (
        payload.get("schema_version") != SOURCE_CONTENT_ANCHOR_SCHEMA_VERSION
        or payload.get("kind") != SOURCE_CONTENT_ANCHOR_MANIFEST_KIND
        or payload.get("source_only") is not True
        or payload.get("scope") != expected_scope
    ):
        raise ProxyEvidenceError(
            "source anchor manifest schema, source-only mode, or scope is invalid"
        )
    _assert_source_only(payload)
    bindings = _require_mapping(payload.get("bindings"), "source anchor bindings")
    media_binding = _require_mapping(
        bindings.get("source_media"), "source anchor source media"
    )
    oracle_binding = _require_mapping(
        bindings.get("source_oracle"), "source anchor source oracle"
    )
    actual_source = _file_record(source_path, include_size=True)
    if (
        not isinstance(media_binding.get("path"), str)
        or not _same_path(Path(media_binding["path"]), source_path)
        or str(media_binding.get("sha256", "")).lower()
        != actual_source["sha256"].lower()
        or media_binding.get("size") != actual_source["size"]
    ):
        raise ProxyEvidenceError("source anchor media binding differs from source")
    actual_oracle = _file_record(source_oracle_path, include_size=False)
    if (
        not isinstance(oracle_binding.get("path"), str)
        or not _same_path(Path(oracle_binding["path"]), source_oracle_path)
        or str(oracle_binding.get("sha256", "")).lower()
        != actual_oracle["sha256"].lower()
    ):
        raise ProxyEvidenceError("source anchor oracle binding differs from source oracle")
    source_section = _require_mapping(payload.get("source"), "source anchor source")
    anchor_start = _require_int(
        source_section.get("business_frame_start"),
        "source anchor business_frame_start",
        minimum=0,
    )
    anchor_count = _require_int(
        source_section.get("business_frame_count"),
        "source anchor business_frame_count",
        minimum=1,
    )
    if (
        anchor_start != required_source_start
        or anchor_start + anchor_count != required_source_end
        or source_section.get("source_frame_domain")
        != [required_source_start, required_source_end]
    ):
        raise ProxyEvidenceError(
            "source anchor frame domain differs from the proxy source window"
        )
    created_utc = _parse_utc(
        payload.get("created_utc"), "source anchor created_utc"
    )
    if created_utc > datetime.now(timezone.utc):
        raise ProxyEvidenceError("source anchor created_utc is in the future")
    return {
        **record,
        "created_utc": payload["created_utc"],
        "kind": payload["kind"],
        "schema_version": payload["schema_version"],
        "scope": payload["scope"],
    }


def _source_window(
    parsed_frames: int,
    *,
    source_start_frame: int = 0,
    business_frames: int | None,
) -> tuple[int, int, int]:
    """Return the bounded source frame window as ``(start, count, end)``.

    ``source_start_frame`` is deliberately independent from the proxy-local
    frame numbering.  A missing ``business_frames`` remains the historical
    whole-source request only when the start is zero; a non-zero start must
    carry an explicit finite window so it cannot silently consume an unknown
    suffix of a source oracle.
    """
    start = _require_int(source_start_frame, "source_start_frame", minimum=0)
    if business_frames is None:
        if start != 0:
            raise ProxyEvidenceError(
                "business_frames is required when source_start_frame is non-zero"
            )
        count = parsed_frames
    else:
        count = _require_int(business_frames, "business_frames", minimum=1)
    end = start + count
    if end > parsed_frames:
        raise ProxyEvidenceError(
            "source frame window exceeds the source oracle frame count"
        )
    return start, count, end


def _frame_mapping(source_start_frame: int, count: int) -> dict[str, Any]:
    source_end = source_start_frame + count
    return {
        "kind": "bounded_affine_frame_index",
        "source_start_frame": source_start_frame,
        "proxy_start_frame": 0,
        "frame_count": count,
        "source_frame_domain": [source_start_frame, source_end],
        "proxy_frame_domain": [0, count],
        "source_to_proxy_offset": -source_start_frame,
        "proxy_to_source_offset": source_start_frame,
        "source_to_proxy": (
            "proxy_frame_index = source_frame_index - source_frame_start"
        ),
        "proxy_to_source": (
            "source_frame_index = proxy_frame_index + source_frame_start"
        ),
    }


def write_json_new(path: Path, value: Any) -> None:
    """Write new immutable evidence; refuse to replace an existing file."""
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        raise FileExistsError(f"evidence already exists; choose a new path: {path}")


def ffmpeg_version_record(ffmpeg: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [str(ffmpeg), "-version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"FFmpeg version probe failed with {completed.returncode}")
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("FFmpeg version probe returned no output")
    return {
        "path": str(ffmpeg.resolve()),
        "sha256": sha256_file(ffmpeg),
        "version_line": lines[0],
        "library_lines": [line for line in lines if line.startswith("lib")],
    }


def analyze_source_oracle(
    report: dict[str, Any],
    source: Path,
    *,
    business_frames: int | None,
    source_start_frame: int = 0,
    checksum_report: dict[str, Any] | None = None,
    require_checksums: bool = True,
) -> dict[str, Any]:
    """Validate source rows used to construct the proxy without repairing PTS."""
    if report.get("kind") != "mpv_phase0_frame_pts_oracle":
        raise ProxyEvidenceError("source oracle kind is invalid")
    video = _require_mapping(report.get("video"), "source oracle video")
    report_path = video.get("path")
    if not isinstance(report_path, str) or not _same_path(Path(report_path), source):
        raise ProxyEvidenceError("source oracle video.path does not match the source")
    ffmpeg = _require_mapping(report.get("ffmpeg"), "source oracle ffmpeg")
    if ffmpeg.get("returncode") != 0:
        raise ProxyEvidenceError("source oracle FFmpeg decode did not return 0")
    rows = report.get("pts_table")
    if not isinstance(rows, list) or not rows:
        raise ProxyEvidenceError("source oracle has no pts_table")
    showinfo = _require_mapping(report.get("showinfo"), "source oracle showinfo")
    parsed_frames = _require_int(
        showinfo.get("parsed_frames"),
        "source oracle parsed_frames",
        minimum=1,
    )
    if parsed_frames != len(rows):
        raise ProxyEvidenceError("source oracle parsed_frames does not match pts_table")
    pixel_formats = showinfo.get("pixel_formats")
    if (
        not isinstance(pixel_formats, list)
        or len(pixel_formats) != 1
        or not isinstance(pixel_formats[0], str)
        or not pixel_formats[0]
    ):
        raise ProxyEvidenceError(
            "source oracle must declare exactly one decoded pixel format"
        )
    source_sha256 = sha256_file(source)
    video_sha256 = video.get("sha256")
    if not isinstance(video_sha256, str) or video_sha256.lower() != source_sha256.lower():
        raise ProxyEvidenceError(
            "source oracle video.sha256 must match the source file"
        )
    start, count, end = _source_window(
        parsed_frames,
        source_start_frame=source_start_frame,
        business_frames=business_frames,
    )
    reason_codes = report.get("reason_codes", [])
    if not isinstance(reason_codes, list):
        raise ProxyEvidenceError("source oracle reason_codes must be a list")
    partial_scan = "PARTIAL_SCAN" in reason_codes
    conflict = report.get("pts_conflict_assessment")
    if isinstance(conflict, dict) and conflict.get("scan_scope") == "partial":
        partial_scan = True
    if partial_scan and (
        business_frames is None
        or (start == 0 and count != parsed_frames)
        or end > parsed_frames
    ):
        raise ProxyEvidenceError(
            "a partial source oracle requires an explicit observed frame window"
        )
    scope = (
        "prefix"
        if partial_scan
        or start != 0
        or business_frames is not None
        or count < parsed_frames
        else "full"
    )

    checksum_rows: list[Any] | None = None
    if checksum_report is not None:
        if checksum_report.get("kind") != "mpv_phase0_proxy_source_decode_evidence":
            raise ProxyEvidenceError("source checksum evidence kind is invalid")
        checksum_video = _require_mapping(
            checksum_report.get("video"), "source checksum evidence video"
        )
        checksum_path = checksum_video.get("path")
        if not isinstance(checksum_path, str) or not _same_path(
            Path(checksum_path), source
        ):
            raise ProxyEvidenceError(
                "source checksum evidence video.path does not match the source"
            )
        checksum_sha256 = checksum_video.get("sha256")
        if not isinstance(checksum_sha256, str) or checksum_sha256.lower() != source_sha256.lower():
            raise ProxyEvidenceError(
                "source checksum evidence video.sha256 must match the source"
            )
        checksum_ffmpeg = _require_mapping(
            checksum_report.get("ffmpeg"), "source checksum evidence ffmpeg"
        )
        if checksum_ffmpeg.get("returncode") != 0:
            raise ProxyEvidenceError("source checksum decode did not return 0")
        checksum_start = checksum_report.get("source_frame_start", 0)
        checksum_count = checksum_report.get("source_frame_count")
        checksum_domain = checksum_report.get("source_frame_domain")
        if checksum_start != start:
            raise ProxyEvidenceError(
                "source checksum evidence frame start differs from the source window"
            )
        if (start != 0 and checksum_count != count) or (
            checksum_count is not None and checksum_count != count
        ):
            raise ProxyEvidenceError(
                "source checksum evidence frame count differs from the source window"
            )
        if (start != 0 and checksum_domain != [start, end]) or (
            checksum_domain is not None and checksum_domain != [start, end]
        ):
            raise ProxyEvidenceError(
                "source checksum evidence frame domain differs from the source window"
            )
        checksum_rows_value = checksum_report.get("pts_table")
        if not isinstance(checksum_rows_value, list) or len(checksum_rows_value) != count:
            raise ProxyEvidenceError(
                "source checksum evidence must exactly cover the business frame domain"
            )
        checksum_rows = checksum_rows_value

    time_base = _require_mapping(
        showinfo.get("time_base"),
        "source oracle time_base",
    )
    numerator = _require_int(time_base.get("numerator"), "time_base.numerator", minimum=1)
    denominator = _require_int(
        time_base.get("denominator"), "time_base.denominator", minimum=1
    )
    if numerator != 1:
        raise ProxyEvidenceError("Phase 0 MP4 proxy requires a 1/N source time base")

    normalized_rows: list[dict[str, Any]] = []
    durations: set[int] = set()
    source_pts_ticks: list[int] = []
    source_end_ticks: list[int] = []
    for index, raw_row in enumerate(rows[start:end]):
        row = _require_mapping(raw_row, f"pts_table[{start + index}]")
        n = _require_int(row.get("n"), f"pts_table[{index}].n", minimum=0)
        if n != start + index:
            raise ProxyEvidenceError(
                "source oracle frame indices are not contiguous in the selected window"
            )
        duration = _require_int(
            row.get("duration"), f"pts_table[{index}].duration", minimum=1
        )
        pts = _require_int(row.get("pts"), f"pts_table[{index}].pts")
        pts_time = row.get("pts_time")
        if not isinstance(pts_time, (int, float)) or isinstance(pts_time, bool):
            raise ProxyEvidenceError(f"pts_table[{index}].pts_time must be numeric")
        if not math.isfinite(float(pts_time)):
            raise ProxyEvidenceError(f"pts_table[{index}].pts_time must be finite")
        duration_time = row.get("duration_time")
        if not isinstance(duration_time, (int, float)) or isinstance(duration_time, bool):
            raise ProxyEvidenceError(f"pts_table[{index}].duration_time must be numeric")
        if not math.isfinite(float(duration_time)) or float(duration_time) <= 0:
            raise ProxyEvidenceError(f"pts_table[{index}].duration_time must be positive")
        expected_duration_time = duration * numerator / denominator
        if not math.isclose(
            float(duration_time), expected_duration_time, rel_tol=5e-6, abs_tol=5e-7
        ):
            raise ProxyEvidenceError(
                f"pts_table[{index}].duration_time does not match the source time base"
            )
        checksum_row = row
        if checksum_rows is not None:
            checksum_row = _require_mapping(
                checksum_rows[index], f"checksum pts_table[{index}]"
            )
            if checksum_row.get("n") != index:
                raise ProxyEvidenceError(
                    f"source checksum evidence local frame index differs at frame {index}"
                )
            source_frame_index = checksum_row.get("source_frame_index")
            if (start != 0 and source_frame_index != n) or (
                source_frame_index is not None and source_frame_index != n
            ):
                raise ProxyEvidenceError(
                    f"source checksum evidence source frame index differs at frame {index}"
                )
            for key in ("pts", "duration"):
                if checksum_row.get(key) != row.get(key):
                    raise ProxyEvidenceError(
                        f"source checksum evidence {key} differs at frame {index}"
                    )
            for key in ("pts_time", "duration_time"):
                left = checksum_row.get(key)
                right = row.get(key)
                if not isinstance(left, (int, float)) or not isinstance(
                    right, (int, float)
                ) or not math.isclose(
                    float(left), float(right), rel_tol=5e-6, abs_tol=5e-7
                ):
                    raise ProxyEvidenceError(
                        f"source checksum evidence {key} differs at frame {index}"
                    )
        checksum_value = checksum_row.get("checksum")
        checksum = None
        if checksum_value is not None or require_checksums:
            checksum = _require_checksum(
                checksum_value, f"pts_table[{index}].checksum"
            )
        durations.add(duration)
        source_pts_ticks.append(pts)
        source_end_ticks.append(pts + duration)
        normalized_rows.append(
            {
                "n": index,
                "source_frame_index": n,
                "pts": pts,
                "pts_time": float(pts_time),
                "duration": duration,
                "duration_time": float(duration_time),
                "checksum": checksum,
            }
        )
    if len(durations) != 1:
        raise ProxyEvidenceError(
            "Phase 0 proxy v1 requires one positive decoded duration; no frame/fps fallback is allowed"
        )
    duration_ticks = next(iter(durations))
    mapping = _frame_mapping(start, count)
    return {
        "rows": normalized_rows,
        "source_frame_start": start,
        "source_frame_end_exclusive": end,
        "source_frame_domain": mapping["source_frame_domain"],
        "proxy_frame_domain": mapping["proxy_frame_domain"],
        "frame_mapping": mapping,
        "business_frame_count": count,
        "time_base": {"numerator": numerator, "denominator": denominator},
        "pixel_format": pixel_formats[0],
        "duration_ticks": duration_ticks,
        "reference_frame_count": parsed_frames,
        "scope": scope,
        "reference_reason_codes": reason_codes,
        "source_ordered_first_pts_ticks": source_pts_ticks[0],
        "source_presentation_start_ticks": min(source_pts_ticks),
        "source_ordered_last_end_ticks": source_end_ticks[-1],
        "source_presentation_end_ticks": max(source_end_ticks),
    }


def build_ffmpeg_command(
    ffmpeg: Path,
    source: Path,
    output: Path,
    *,
    business_frame_count: int,
    time_base_numerator: int,
    time_base_denominator: int,
    duration_ticks: int,
    source_start_frame: int = 0,
    preset: str = DEFAULT_PRESET,
) -> list[str]:
    business_frame_count = _require_int(
        business_frame_count, "business_frame_count", minimum=1
    )
    duration_ticks = _require_int(duration_ticks, "duration_ticks", minimum=1)
    source_start_frame = _require_int(
        source_start_frame, "source_start_frame", minimum=0
    )
    if time_base_numerator != 1 or time_base_denominator < 1:
        raise ValueError("MP4 proxy time base must be 1/N")
    if not preset or not isinstance(preset, str):
        raise ValueError("preset must be a non-empty string")
    trim_filter = (
        f"trim=end_frame={business_frame_count}"
        if source_start_frame == 0
        else f"trim=start_frame={source_start_frame}:end_frame={source_start_frame + business_frame_count}"
    )
    video_filter = (
        f"{trim_filter},"
        "showinfo@source,"
        "tpad=stop_mode=clone:stop=1,"
        f"settb={time_base_numerator}/{time_base_denominator},"
        f"setpts=N*{duration_ticks}"
    )
    return [
        str(ffmpeg),
        "-y",
        "-hide_banner",
        "-loglevel",
        "info",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
        "-vf",
        video_filter,
        "-fps_mode",
        "passthrough",
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-qp",
        "0",
        "-bf",
        "0",
        "-pix_fmt",
        "yuv420p",
        "-enc_time_base",
        f"{time_base_numerator}/{time_base_denominator}",
        "-video_track_timescale",
        str(time_base_denominator),
        "-frames:v",
        str(business_frame_count + 1),
        str(output),
    ]


def _tool_bindings() -> dict[str, Any]:
    this_script = Path(__file__).resolve()
    oracle_script = Path(frame_oracle.__file__).resolve()
    return {
        "pts_normalized_proxy": {
            "path": str(this_script),
            "sha256": sha256_file(this_script),
        },
        "verify_mpv_frames": {
            "path": str(oracle_script),
            "sha256": sha256_file(oracle_script),
        },
        "python_version": sys.version,
    }


def build_proxy(
    source: Path,
    source_oracle_path: Path,
    source_decode_path: Path,
    output: Path,
    manifest_path: Path,
    ffmpeg: frame_oracle.FfmpegExecutable,
    *,
    source_anchor_manifest_path: Path,
    business_frames: int | None,
    source_start_frame: int = 0,
    preset: str,
) -> dict[str, Any]:
    source = source.expanduser().resolve()
    source_oracle_path = source_oracle_path.expanduser().resolve()
    source_anchor_manifest_path = source_anchor_manifest_path.expanduser().resolve()
    source_decode_path = source_decode_path.expanduser().resolve()
    output = output.expanduser().resolve()
    manifest_path = manifest_path.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"source video not found: {source}")
    if output.suffix.lower() != ".mp4":
        raise ValueError("Phase 0 normalized proxy output must use .mp4")
    if len(
        {
            source_anchor_manifest_path,
            source_decode_path,
            output,
            manifest_path,
        }
    ) != 4:
        raise ValueError(
            "source anchor, source decode, proxy, and manifest paths must be distinct"
        )
    if output.exists() or manifest_path.exists() or source_decode_path.exists():
        raise FileExistsError(
            "source decode, proxy output, and manifest are write-once; choose new paths"
        )
    source_report = _load_json(source_oracle_path)
    reference_evidence = analyze_source_oracle(
        source_report,
        source,
        business_frames=business_frames,
        source_start_frame=source_start_frame,
        require_checksums=False,
    )
    source_anchor_record = _validate_source_anchor_manifest(
        source_anchor_manifest_path,
        source,
        source_oracle_path,
        expected_scope=reference_evidence["scope"],
        required_source_start=reference_evidence["source_frame_start"],
        required_source_end=reference_evidence["source_frame_end_exclusive"],
    )
    generation_started_utc = _utc_now()
    source_stat = source.stat()
    source_sha256_before = sha256_file(source)
    report_video = _require_mapping(source_report.get("video"), "source oracle video")
    if report_video.get("size") != source_stat.st_size:
        raise ProxyEvidenceError("source size changed since the source oracle was generated")
    if report_video.get("mtime_ns") != source_stat.st_mtime_ns:
        raise ProxyEvidenceError("source mtime changed since the source oracle was generated")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(
        f".{output.stem}.{uuid.uuid4().hex}.tmp{output.suffix}"
    )
    command = build_ffmpeg_command(
        ffmpeg.path,
        source,
        temporary,
        business_frame_count=reference_evidence["business_frame_count"],
        time_base_numerator=reference_evidence["time_base"]["numerator"],
        time_base_denominator=reference_evidence["time_base"]["denominator"],
        duration_ticks=reference_evidence["duration_ticks"],
        source_start_frame=reference_evidence["source_frame_start"],
        preset=preset,
    )
    source_analyzer = frame_oracle.ShowinfoAnalyzer()
    encoder_lines: list[str] = []
    diagnostic_tail: deque[str] = deque(maxlen=200)
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stderr is not None
        try:
            for line in process.stderr:
                source_analyzer.feed(line)
                stripped = line.strip()
                if "264 - core" in stripped and stripped not in encoder_lines:
                    encoder_lines.append(stripped)
                if stripped:
                    diagnostic_tail.append(stripped)
        finally:
            process.stderr.close()
        returncode = process.wait()
        if returncode != 0:
            raise RuntimeError(
                f"FFmpeg proxy generation failed ({returncode}): "
                + "\n".join(diagnostic_tail)
            )
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise RuntimeError("FFmpeg did not create a non-empty proxy")
        source_stat_after = source.stat()
        source_sha256_after = sha256_file(source)
        if (
            source_stat_after.st_size != source_stat.st_size
            or source_stat_after.st_mtime_ns != source_stat.st_mtime_ns
            or source_sha256_after != source_sha256_before
        ):
            raise ProxyEvidenceError("source identity changed during proxy generation")
        source_decode_evidence = source_analyzer.finish(
            ffmpeg_returncode=returncode,
            partial_scan=reference_evidence["scope"] == "prefix",
        )
        source_decode_report = {
            "schema_version": SCHEMA_VERSION,
            "kind": "mpv_phase0_proxy_source_decode_evidence",
            "created_utc": _utc_now(),
            "method": (
                "source showinfo captured in the same decode that generated the proxy; "
                "only the registered source frame window is consumed"
            ),
            "source_anchor_manifest": source_anchor_record,
            "video": {
                "path": str(source),
                "sha256": source_sha256_after,
                "size": source_stat.st_size,
                "mtime_ns": source_stat.st_mtime_ns,
            },
            "reference_oracle": {
                "path": str(source_oracle_path),
                "sha256": sha256_file(source_oracle_path),
                "reference_frame_count": reference_evidence["reference_frame_count"],
                "business_frame_count": reference_evidence["business_frame_count"],
                "source_frame_start": reference_evidence["source_frame_start"],
                "source_frame_end_exclusive": reference_evidence[
                    "source_frame_end_exclusive"
                ],
                "source_frame_domain": reference_evidence["source_frame_domain"],
                "scope": reference_evidence["scope"],
            },
            "ffmpeg": {
                "path": str(ffmpeg.path.resolve()),
                "command": command,
                "returncode": returncode,
                "diagnostic_tail": list(diagnostic_tail),
            },
        }
        source_decode_report.update(source_decode_evidence)
        source_decode_report["source_frame_start"] = reference_evidence[
            "source_frame_start"
        ]
        source_decode_report["source_frame_end_exclusive"] = reference_evidence[
            "source_frame_end_exclusive"
        ]
        source_decode_report["source_frame_count"] = reference_evidence[
            "business_frame_count"
        ]
        source_decode_report["source_frame_domain"] = reference_evidence[
            "source_frame_domain"
        ]
        source_decode_report["proxy_frame_domain"] = reference_evidence[
            "proxy_frame_domain"
        ]
        source_decode_report["frame_mapping"] = reference_evidence[
            "frame_mapping"
        ]
        for row in source_decode_report.get("pts_table", []):
            if isinstance(row, dict) and isinstance(row.get("n"), int):
                row["source_frame_index"] = (
                    reference_evidence["source_frame_start"] + row["n"]
                )
        evidence = analyze_source_oracle(
            source_report,
            source,
            business_frames=business_frames,
            source_start_frame=source_start_frame,
            checksum_report=source_decode_report,
        )
        if (
            _validate_source_anchor_manifest(
                source_anchor_manifest_path,
                source,
                source_oracle_path,
                expected_scope=evidence["scope"],
                required_source_start=evidence["source_frame_start"],
                required_source_end=evidence["source_frame_end_exclusive"],
            )
            != source_anchor_record
        ):
            raise ProxyEvidenceError(
                "source anchor manifest changed during proxy generation"
            )
        if output.exists():
            raise FileExistsError(f"proxy output appeared during generation: {output}")
        os.rename(temporary, output)
    except BaseException:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait()
        raise
    finally:
        if temporary.exists():
            temporary.unlink()
    count = evidence["business_frame_count"]
    duration_ticks = evidence["duration_ticks"]
    canonical_command = list(command)
    canonical_command[-1] = str(output)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "kind": MANIFEST_KIND,
        "created_utc": _utc_now(),
        "source_anchor_manifest": source_anchor_record,
        "status": "BUILT_UNVERIFIED",
        "method": (
            "decode in source picture order, assign cumulative positive decoded durations, "
            "append one cloned terminal guard outside the business frame domain"
        ),
        "source": {
            "path": str(source),
            "sha256": source_sha256_before,
            "size": source_stat.st_size,
            "mtime_ns": source_stat.st_mtime_ns,
            "identity_unchanged_during_generation": True,
            "oracle": {
                "path": str(source_oracle_path),
                "sha256": sha256_file(source_oracle_path),
                "parsed_frames": len(source_report["pts_table"]),
                "source_frame_start": evidence["source_frame_start"],
                "source_frame_end_exclusive": evidence[
                    "source_frame_end_exclusive"
                ],
                "source_frame_domain": evidence["source_frame_domain"],
                "scope": evidence["scope"],
                "reason_codes": evidence["reference_reason_codes"],
            },
            "generation_source_decode": {
                "path": str(source_decode_path),
                "sha256": None,
                "observed_frames": source_decode_report["showinfo"]["parsed_frames"],
                "business_checksum_frames": count,
                "source_frame_start": evidence["source_frame_start"],
                "source_frame_end_exclusive": evidence[
                    "source_frame_end_exclusive"
                ],
                "source_frame_domain": evidence["source_frame_domain"],
            },
            "business_frame_start": evidence["source_frame_start"],
            "business_frame_count": count,
            "source_frame_domain": evidence["source_frame_domain"],
            "proxy_frame_domain": evidence["proxy_frame_domain"],
            "frame_mapping": evidence["frame_mapping"],
            "scope": evidence["scope"],
            "first_checksum": evidence["rows"][0]["checksum"],
            "last_checksum": evidence["rows"][-1]["checksum"],
            "pixel_format": evidence["pixel_format"],
        },
        "proxy": {
            "path": str(output),
            "sha256": sha256_file(output),
            "size": output.stat().st_size,
        },
        "normalized_timeline": {
            "time_base": evidence["time_base"],
            "duration_ticks": duration_ticks,
            "business_pts_start": 0,
            "business_pts_end_exclusive": count * duration_ticks,
            "source_ordered_first_pts_ticks": evidence[
                "source_ordered_first_pts_ticks"
            ],
            "source_presentation_start_ticks": evidence[
                "source_presentation_start_ticks"
            ],
            "source_ordered_last_end_ticks": evidence[
                "source_ordered_last_end_ticks"
            ],
            "source_presentation_end_ticks": evidence[
                "source_presentation_end_ticks"
            ],
            "normalized_end_ticks": count * duration_ticks,
            "normalized_minus_source_end_ticks": (
                count * duration_ticks - evidence["source_presentation_end_ticks"]
            ),
            "normalized_minus_source_end_seconds": (
                (count * duration_ticks - evidence["source_presentation_end_ticks"])
                * evidence["time_base"]["numerator"]
                / evidence["time_base"]["denominator"]
            ),
            "normalized_minus_source_start_ticks": -evidence[
                "source_presentation_start_ticks"
            ],
            "normalized_minus_source_start_seconds": (
                -evidence["source_presentation_start_ticks"]
                * evidence["time_base"]["numerator"]
                / evidence["time_base"]["denominator"]
            ),
            "construction": "pts[n] = n * source_decoded_duration_ticks",
            "frame_fps_fallback_used": False,
            "source_frame_start": evidence["source_frame_start"],
            "source_frame_end_exclusive": evidence["source_frame_end_exclusive"],
            "source_frame_domain": evidence["source_frame_domain"],
            "proxy_frame_domain": evidence["proxy_frame_domain"],
            "frame_mapping": evidence["frame_mapping"],
        },
        "terminal_guard": {
            "method": "clone_last_business_frame",
            "proxy_frame_index": count,
            "pts_ticks": count * duration_ticks,
            "expected_checksum": evidence["rows"][-1]["checksum"],
            "business_frame_domain": [0, count],
            "included_in_business_domain": False,
            "zero_duration_allowed_for_guard_only": True,
            "source_frame_index": evidence["source_frame_end_exclusive"] - 1,
            "source_frame_domain": evidence["source_frame_domain"],
            "proxy_frame_domain": evidence["proxy_frame_domain"],
        },
        "generation": {
            "started_utc": generation_started_utc,
            "source_anchor_manifest": source_anchor_record,
            "executed_command": command,
            "canonical_command": canonical_command,
            "source_frame_start": evidence["source_frame_start"],
            "source_frame_end_exclusive": evidence["source_frame_end_exclusive"],
            "source_frame_domain": evidence["source_frame_domain"],
            "proxy_frame_domain": evidence["proxy_frame_domain"],
            "frame_mapping": evidence["frame_mapping"],
            "ffmpeg": ffmpeg_version_record(ffmpeg.path),
            "encoder": {
                "name": "libx264",
                "preset": preset,
                "lossless_qp": 0,
                "b_frames": 0,
                "pixel_format": "yuv420p",
                "identification_lines": encoder_lines,
            },
            "tools": _tool_bindings(),
        },
        "audio_timeline": {
            "mode": "omitted_for_video_phase0",
            "status": "NOT_RUN",
        },
        "av_sync": {"status": "NOT_RUN"},
    }
    try:
        if (
            _validate_source_anchor_manifest(
                source_anchor_manifest_path,
                source,
                source_oracle_path,
                expected_scope=evidence["scope"],
                required_source_start=evidence["source_frame_start"],
                required_source_end=evidence["source_frame_end_exclusive"],
            )
            != source_anchor_record
        ):
            raise ProxyEvidenceError(
                "source anchor manifest changed before evidence publication"
            )
        write_json_new(source_decode_path, source_decode_report)
        manifest["source"]["generation_source_decode"]["sha256"] = sha256_file(
            source_decode_path
        )
        write_json_new(manifest_path, manifest)
    except BaseException:
        # The output path was required to be absent before this run, so this
        # cleanup can only remove the unbound proxy created above.
        output.unlink(missing_ok=True)
        source_decode_path.unlink(missing_ok=True)
        raise
    return manifest


def evaluate_video_proxy(
    manifest: dict[str, Any],
    source_rows: Sequence[dict[str, Any]],
    oracle_report: dict[str, Any],
) -> dict[str, Any]:
    reasons: list[str] = []
    source = _require_mapping(manifest.get("source"), "manifest source")
    timeline = _require_mapping(
        manifest.get("normalized_timeline"), "manifest normalized_timeline"
    )
    guard_record = _require_mapping(manifest.get("terminal_guard"), "manifest guard")
    count = _require_int(source.get("business_frame_count"), "business_frame_count", minimum=1)
    source_start = _require_int(
        source.get("business_frame_start", 0), "business_frame_start", minimum=0
    )
    source_frame_end = source_start + count
    scope = source.get("scope")
    if scope not in ("prefix", "full"):
        raise ProxyEvidenceError("manifest source scope must be prefix or full")
    duration_ticks = _require_int(timeline.get("duration_ticks"), "duration_ticks", minimum=1)
    time_base = _require_mapping(timeline.get("time_base"), "normalized time_base")
    numerator = _require_int(time_base.get("numerator"), "time_base.numerator", minimum=1)
    denominator = _require_int(time_base.get("denominator"), "time_base.denominator", minimum=1)
    if len(source_rows) == count:
        business_source_rows = list(source_rows)
    elif len(source_rows) >= source_frame_end:
        business_source_rows = list(source_rows[source_start:source_frame_end])
    else:
        business_source_rows = list(source_rows)
        reasons.append("SOURCE_BUSINESS_FRAME_COUNT_MISMATCH")
    declared_source_domain = source.get("source_frame_domain")
    expected_mapping = _frame_mapping(source_start, count)
    if (source_start != 0 or declared_source_domain is not None) and (
        declared_source_domain != expected_mapping["source_frame_domain"]
    ):
        reasons.append("SOURCE_FRAME_DOMAIN_INVALID")
    declared_proxy_domain = source.get("proxy_frame_domain")
    if (source_start != 0 or declared_proxy_domain is not None) and (
        declared_proxy_domain != expected_mapping["proxy_frame_domain"]
    ):
        reasons.append("PROXY_FRAME_DOMAIN_INVALID")
    declared_mapping = source.get("frame_mapping")
    if (source_start != 0 or declared_mapping is not None) and (
        declared_mapping != expected_mapping
    ):
        reasons.append("FRAME_MAPPING_INVALID")
    for field, expected in (
        ("source_frame_start", source_start),
        ("source_frame_end_exclusive", source_frame_end),
        ("source_frame_domain", expected_mapping["source_frame_domain"]),
        ("proxy_frame_domain", expected_mapping["proxy_frame_domain"]),
        ("frame_mapping", expected_mapping),
    ):
        if source_start != 0 or field in timeline:
            if timeline.get(field) != expected:
                reasons.append("TIMELINE_FRAME_MAPPING_INVALID")
                break
    for index, source_row in enumerate(business_source_rows[:count]):
        source_index = source_row.get("source_frame_index")
        oracle_index = source_row.get("n")
        if source_index is None and oracle_index == source_start + index:
            source_index = oracle_index
        if source_start != 0 and source_index != source_start + index:
            reasons.append("SOURCE_FRAME_MAPPING_INVALID")
        elif source_index is not None and source_index != source_start + index:
            reasons.append("SOURCE_FRAME_MAPPING_INVALID")

    rows = oracle_report.get("pts_table")
    if not isinstance(rows, list):
        rows = []
        reasons.append("PROXY_PTS_TABLE_MISSING")
    decoded_count = len(rows)
    guard_observed = decoded_count == count + 1
    if decoded_count not in (count, count + 1):
        reasons.append("PROXY_FRAME_COUNT_MISMATCH")
    business_rows = rows[:count]
    checksum_mismatches: list[dict[str, Any]] = []
    pts_mismatches: list[dict[str, Any]] = []
    duration_mismatches: list[dict[str, Any]] = []

    compare_count = min(count, len(business_source_rows), len(business_rows))
    for index in range(compare_count):
        source_row = business_source_rows[index]
        proxy_row = _require_mapping(business_rows[index], f"proxy pts_table[{index}]")
        if proxy_row.get("n") != index:
            reasons.append("PROXY_FRAME_INDEX_MISMATCH")
        source_checksum = str(source_row.get("checksum", "")).upper()
        proxy_checksum = str(proxy_row.get("checksum", "")).upper()
        if source_checksum != proxy_checksum:
            checksum_mismatches.append(
                {"n": index, "source": source_checksum, "proxy": proxy_checksum}
            )
        expected_pts = index * duration_ticks
        if proxy_row.get("pts") != expected_pts:
            pts_mismatches.append(
                {"n": index, "expected": expected_pts, "actual": proxy_row.get("pts")}
            )
        expected_pts_time = expected_pts * numerator / denominator
        actual_pts_time = proxy_row.get("pts_time")
        if not isinstance(actual_pts_time, (int, float)) or isinstance(actual_pts_time, bool) or not math.isclose(
            float(actual_pts_time), expected_pts_time, rel_tol=5e-6, abs_tol=5e-7
        ):
            pts_mismatches.append(
                {
                    "n": index,
                    "field": "pts_time",
                    "expected": expected_pts_time,
                    "actual": actual_pts_time,
                }
            )
        actual_duration = proxy_row.get("duration")
        actual_duration_time = proxy_row.get("duration_time")
        expected_duration_time = duration_ticks * numerator / denominator
        if actual_duration != duration_ticks:
            duration_mismatches.append(
                {
                    "n": index,
                    "field": "duration",
                    "expected": duration_ticks,
                    "actual": actual_duration,
                }
            )
        if not isinstance(actual_duration_time, (int, float)) or isinstance(actual_duration_time, bool) or float(actual_duration_time) <= 0 or not math.isclose(
            float(actual_duration_time), expected_duration_time, rel_tol=5e-6, abs_tol=5e-7
        ):
            duration_mismatches.append(
                {
                    "n": index,
                    "field": "duration_time",
                    "expected": expected_duration_time,
                    "actual": actual_duration_time,
                }
            )

    if checksum_mismatches:
        reasons.append("BUSINESS_FRAME_CHECKSUM_MISMATCH")
    if pts_mismatches:
        reasons.append("BUSINESS_FRAME_PTS_MISMATCH")
    if duration_mismatches:
        reasons.append("BUSINESS_FRAME_DURATION_INVALID")

    guard_evaluation: dict[str, Any] = {
        "observed": guard_observed,
        "included_in_business_domain": False,
        "business_frame_domain": [0, count],
        "source_frame_domain": [source_start, source_frame_end],
    }
    if not guard_observed:
        reasons.append("TERMINAL_GUARD_MISSING")
    guard_index = _require_int(
        guard_record.get("proxy_frame_index"), "guard.proxy_frame_index", minimum=1
    )
    if (
        guard_index != count
        or guard_record.get("business_frame_domain") != [0, count]
        or guard_record.get("included_in_business_domain") is not False
        or (
            source_start != 0
            and guard_record.get("source_frame_index") != source_frame_end - 1
        )
        or (
            guard_record.get("source_frame_index") is not None
            and guard_record.get("source_frame_index") != source_frame_end - 1
        )
        or (
            (
                source_start != 0
                or guard_record.get("source_frame_domain") is not None
            )
            and guard_record.get("source_frame_domain")
            != [source_start, source_frame_end]
        )
        or (
            (
                source_start != 0
                or guard_record.get("proxy_frame_domain") is not None
            )
            and guard_record.get("proxy_frame_domain") != [0, count]
        )
    ):
        reasons.append("TERMINAL_GUARD_DOMAIN_INVALID")
    if guard_observed:
        guard = _require_mapping(rows[count], "decoded terminal guard")
        expected_guard_checksum = str(guard_record.get("expected_checksum", "")).upper()
        guard_evaluation.update(
            {
                "frame": guard,
                "checksum_matches": str(guard.get("checksum", "")).upper()
                == expected_guard_checksum,
                "pts_matches": guard.get("pts") == count * duration_ticks,
                "source_frame_index": source_frame_end - 1,
            }
        )
        if guard.get("n") != count:
            reasons.append("TERMINAL_GUARD_INDEX_MISMATCH")
        if not guard_evaluation["checksum_matches"]:
            reasons.append("TERMINAL_GUARD_CHECKSUM_MISMATCH")
        if not guard_evaluation["pts_matches"]:
            reasons.append("TERMINAL_GUARD_PTS_MISMATCH")

    generic_reasons = oracle_report.get("reason_codes")
    if not isinstance(generic_reasons, list):
        generic_reasons = ["GENERIC_ORACLE_REASON_CODES_INVALID"]
    duration_problems = _require_mapping(
        oracle_report.get("examples", {}), "generic oracle examples"
    ).get("duration_problems", [])
    guard_only_duration_problem = (
        guard_observed
        and isinstance(duration_problems, list)
        and bool(duration_problems)
        and all(
            isinstance(problem, dict) and problem.get("n") == count
            for problem in duration_problems
        )
    )
    allowed_generic_reasons: list[str] = []
    for reason in generic_reasons:
        if reason == "FRAME_DURATION_NON_POSITIVE" and guard_only_duration_problem:
            allowed_generic_reasons.append(reason)
        else:
            reasons.append(f"GENERIC_ORACLE_{reason}")
    ffmpeg_record = _require_mapping(oracle_report.get("ffmpeg"), "generic oracle ffmpeg")
    if ffmpeg_record.get("returncode") != 0:
        reasons.append("PROXY_FFMPEG_DECODE_FAILED")

    report_time_base = _require_mapping(
        _require_mapping(oracle_report.get("showinfo"), "generic oracle showinfo").get(
            "time_base"
        ),
        "generic oracle time_base",
    )
    if (
        report_time_base.get("numerator") != numerator
        or report_time_base.get("denominator") != denominator
    ):
        reasons.append("PROXY_TIME_BASE_MISMATCH")
    showinfo = _require_mapping(oracle_report.get("showinfo"), "generic oracle showinfo")
    pixel_formats = showinfo.get("pixel_formats")
    encoder = _require_mapping(
        _require_mapping(manifest.get("generation"), "manifest generation").get("encoder"),
        "manifest encoder",
    )
    expected_pixel_format = encoder.get("pixel_format")
    if (
        not isinstance(pixel_formats, list)
        or len(pixel_formats) != 1
        or not isinstance(pixel_formats[0], str)
        or not pixel_formats[0]
    ):
        reasons.append("PROXY_PIXEL_FORMAT_MISSING_OR_CHANGED")
    elif expected_pixel_format != pixel_formats[0]:
        reasons.append("PROXY_PIXEL_FORMAT_MISMATCH")

    reasons = list(dict.fromkeys(reasons))
    return {
        "status": "PASS" if not reasons else "BLOCKED",
        "reason_codes": reasons,
        "scope": scope,
        "source_frame_start": source_start,
        "source_frame_end_exclusive": source_frame_end,
        "source_frame_domain": [source_start, source_frame_end],
        "proxy_frame_domain": [0, count],
        "frame_mapping": _frame_mapping(source_start, count),
        "authoritative_for_full_source": (
            scope == "full" and source_start == 0 and not reasons
        ),
        "business_frame_domain": [0, count],
        "business_frame_count": count,
        "decoded_frame_count": decoded_count,
        "checksum_alignment": {
            "status": "PASS" if not checksum_mismatches and compare_count == count else "BLOCKED",
            "compared": compare_count,
            "mismatch_count": len(checksum_mismatches),
            "examples": checksum_mismatches[:20],
        },
        "pts_alignment": {
            "status": "PASS" if not pts_mismatches and compare_count == count else "BLOCKED",
            "mismatch_count": len(pts_mismatches),
            "examples": pts_mismatches[:20],
        },
        "positive_business_durations": {
            "status": "PASS" if not duration_mismatches and compare_count == count else "BLOCKED",
            "mismatch_count": len(duration_mismatches),
            "examples": duration_mismatches[:20],
        },
        "terminal_guard": guard_evaluation,
        "generic_oracle": {
            "status": oracle_report.get("status"),
            "reason_codes": generic_reasons,
            "allowed_guard_only_reason_codes": allowed_generic_reasons,
        },
    }


def _validate_bound_file(record: dict[str, Any], name: str) -> Path:
    path_value = record.get("path")
    sha_value = record.get("sha256")
    if not isinstance(path_value, str) or not isinstance(sha_value, str):
        raise ProxyEvidenceError(f"{name} path/sha256 binding is invalid")
    path = Path(path_value).expanduser().resolve()
    if not path.is_file():
        raise ProxyEvidenceError(f"{name} file is missing: {path}")
    if sha256_file(path).lower() != sha_value.lower():
        raise ProxyEvidenceError(f"{name} SHA-256 does not match the manifest")
    return path


def validate_manifest_semantics(
    manifest: dict[str, Any],
    source_evidence: dict[str, Any],
    source_path: Path,
    proxy_path: Path,
    ffmpeg_path: Path,
) -> None:
    """Re-derive the timeline, guard, and command from bound source evidence."""
    source = _require_mapping(manifest.get("source"), "manifest source")
    timeline = _require_mapping(
        manifest.get("normalized_timeline"), "manifest normalized_timeline"
    )
    guard = _require_mapping(manifest.get("terminal_guard"), "manifest guard")
    generation = _require_mapping(manifest.get("generation"), "manifest generation")
    encoder = _require_mapping(generation.get("encoder"), "manifest encoder")
    count = source_evidence["business_frame_count"]
    duration_ticks = source_evidence["duration_ticks"]
    time_base = source_evidence["time_base"]
    rows = source_evidence["rows"]
    source_start = _require_int(
        source.get("business_frame_start", 0), "business_frame_start", minimum=0
    )
    evidence_start = _require_int(
        source_evidence.get("source_frame_start", 0),
        "source evidence frame start",
        minimum=0,
    )
    source_frame_end = source_start + count
    if source_start != evidence_start:
        raise ProxyEvidenceError(
            "manifest source frame start differs from source evidence"
        )
    source_oracle_binding = _require_mapping(
        source.get("oracle"), "manifest source oracle"
    )
    source_oracle_value = source_oracle_binding.get("path")
    if not isinstance(source_oracle_value, str):
        raise ProxyEvidenceError("manifest source oracle path is invalid")
    source_anchor_binding = _require_mapping(
        manifest.get("source_anchor_manifest"),
        "manifest source anchor registration",
    )
    source_anchor_value = source_anchor_binding.get("path")
    if not isinstance(source_anchor_value, str):
        raise ProxyEvidenceError("manifest source anchor path is invalid")
    actual_source_anchor = _validate_source_anchor_manifest(
        Path(source_anchor_value),
        source_path,
        Path(source_oracle_value),
        expected_scope=source_evidence["scope"],
        required_source_start=source_start,
        required_source_end=source_frame_end,
    )
    if source_anchor_binding != actual_source_anchor:
        raise ProxyEvidenceError(
            "manifest source anchor registration binding is stale"
        )
    generation_anchor = _require_mapping(
        generation.get("source_anchor_manifest"),
        "generation source anchor registration",
    )
    if generation_anchor != source_anchor_binding:
        raise ProxyEvidenceError(
            "generation source anchor registration differs from manifest"
        )
    source_anchor_created = _parse_utc(
        actual_source_anchor.get("created_utc"),
        "source anchor created_utc",
    )
    generation_started = _parse_utc(
        generation.get("started_utc"),
        "proxy generation started_utc",
    )
    manifest_created = _parse_utc(
        manifest.get("created_utc"),
        "proxy manifest created_utc",
    )
    if not source_anchor_created <= generation_started <= manifest_created:
        raise ProxyEvidenceError(
            "source anchor was not registered before proxy generation"
        )

    if source.get("business_frame_count") != count:
        raise ProxyEvidenceError("manifest business frame domain differs from source evidence")
    expected_mapping = _frame_mapping(source_start, count)
    for owner, record in (
        ("manifest source", source),
        ("source evidence", source_evidence),
    ):
        if source_start != 0 or "source_frame_domain" in record:
            if record.get("source_frame_domain") != expected_mapping["source_frame_domain"]:
                raise ProxyEvidenceError(f"{owner} source frame domain is invalid")
            if record.get("proxy_frame_domain") != expected_mapping["proxy_frame_domain"]:
                raise ProxyEvidenceError(f"{owner} proxy frame domain is invalid")
            if record.get("frame_mapping") != expected_mapping:
                raise ProxyEvidenceError(f"{owner} frame mapping is invalid")
    for binding_name in ("oracle", "generation_source_decode"):
        binding_value = source.get(binding_name)
        has_window_binding = isinstance(binding_value, dict) and any(
            field in binding_value
            for field in (
                "source_frame_start",
                "source_frame_end_exclusive",
                "source_frame_domain",
            )
        )
        if source_start != 0 or has_window_binding:
            binding = _require_mapping(
                binding_value, f"manifest source {binding_name}"
            )
            if (
                binding.get("source_frame_start") != source_start
                or binding.get("source_frame_end_exclusive")
                != source_frame_end
                or binding.get("source_frame_domain")
                != expected_mapping["source_frame_domain"]
            ):
                raise ProxyEvidenceError(
                    f"manifest source {binding_name} is not bound to the source window"
                )
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ProxyEvidenceError("source evidence rows must be objects")
        mapped_source_index = row.get("source_frame_index")
        if source_start != 0 or mapped_source_index is not None:
            if mapped_source_index != source_start + index:
                raise ProxyEvidenceError(
                    "source evidence row mapping differs from the source window"
                )
    if source.get("scope") != source_evidence["scope"]:
        raise ProxyEvidenceError("manifest source scope differs from source evidence")
    if source.get("first_checksum") != rows[0]["checksum"] or source.get("last_checksum") != rows[-1]["checksum"]:
        raise ProxyEvidenceError("manifest source checksum summary differs from source evidence")
    if source.get("pixel_format") != source_evidence.get("pixel_format"):
        raise ProxyEvidenceError("manifest source pixel format differs from source evidence")
    if timeline.get("time_base") != time_base or timeline.get("duration_ticks") != duration_ticks:
        raise ProxyEvidenceError("normalized timeline differs from source decoded durations")
    if timeline.get("business_pts_start") != 0 or timeline.get("business_pts_end_exclusive") != count * duration_ticks:
        raise ProxyEvidenceError("normalized business PTS domain is invalid")
    normalized_end = count * duration_ticks
    source_ordered_start = source_evidence.get("source_ordered_first_pts_ticks")
    source_presentation_start = source_evidence.get(
        "source_presentation_start_ticks"
    )
    source_pts_end = source_evidence["source_presentation_end_ticks"]
    expected_delta = normalized_end - source_pts_end
    if (
        timeline.get("source_ordered_last_end_ticks")
        != source_evidence["source_ordered_last_end_ticks"]
        or timeline.get("source_presentation_end_ticks") != source_pts_end
        or timeline.get("normalized_end_ticks") != normalized_end
        or timeline.get("normalized_minus_source_end_ticks") != expected_delta
    ):
        raise ProxyEvidenceError("normalized/source duration delta cannot be re-derived")
    delta_seconds = timeline.get("normalized_minus_source_end_seconds")
    expected_delta_seconds = (
        expected_delta * time_base["numerator"] / time_base["denominator"]
    )
    if not isinstance(delta_seconds, (int, float)) or isinstance(delta_seconds, bool) or not math.isclose(
        float(delta_seconds), expected_delta_seconds, rel_tol=5e-9, abs_tol=5e-12
    ):
        raise ProxyEvidenceError("normalized/source duration delta seconds are invalid")
    if source_start != 0 or "source_ordered_first_pts_ticks" in timeline:
        source_ordered_start = _require_int(
            source_ordered_start, "source evidence ordered first PTS"
        )
        source_presentation_start = _require_int(
            source_presentation_start, "source evidence presentation start PTS"
        )
        if (
            timeline.get("source_ordered_first_pts_ticks")
            != source_ordered_start
            or timeline.get("source_presentation_start_ticks")
            != source_presentation_start
            or timeline.get("normalized_minus_source_start_ticks")
            != -source_presentation_start
        ):
            raise ProxyEvidenceError(
                "normalized/source start offset cannot be re-derived"
            )
        start_delta_seconds = timeline.get(
            "normalized_minus_source_start_seconds"
        )
        expected_start_delta_seconds = (
            -source_presentation_start
            * time_base["numerator"]
            / time_base["denominator"]
        )
        if (
            not isinstance(start_delta_seconds, (int, float))
            or isinstance(start_delta_seconds, bool)
            or not math.isclose(
                float(start_delta_seconds),
                expected_start_delta_seconds,
                rel_tol=5e-9,
                abs_tol=5e-12,
            )
        ):
            raise ProxyEvidenceError(
                "normalized/source start offset seconds are invalid"
            )
    if timeline.get("frame_fps_fallback_used") is not False:
        raise ProxyEvidenceError("frame/fps fallback must be explicitly false")
    for field, expected in (
        ("source_frame_start", source_start),
        ("source_frame_end_exclusive", source_frame_end),
        ("source_frame_domain", expected_mapping["source_frame_domain"]),
        ("proxy_frame_domain", expected_mapping["proxy_frame_domain"]),
        ("frame_mapping", expected_mapping),
    ):
        if source_start != 0 or field in timeline:
            if timeline.get(field) != expected:
                raise ProxyEvidenceError(
                    f"normalized timeline {field} is not bound to the source window"
                )
    if (
        guard.get("method") != "clone_last_business_frame"
        or guard.get("proxy_frame_index") != count
        or guard.get("pts_ticks") != count * duration_ticks
        or guard.get("expected_checksum") != rows[-1]["checksum"]
        or guard.get("business_frame_domain") != [0, count]
        or guard.get("included_in_business_domain") is not False
        or (
            source_start != 0
            and guard.get("source_frame_index") != source_frame_end - 1
        )
        or (
            guard.get("source_frame_index") is not None
            and guard.get("source_frame_index") != source_frame_end - 1
        )
        or (
            guard.get("source_frame_domain") is not None
            and guard.get("source_frame_domain")
            != expected_mapping["source_frame_domain"]
        )
        or (
            guard.get("proxy_frame_domain") is not None
            and guard.get("proxy_frame_domain")
            != expected_mapping["proxy_frame_domain"]
        )
    ):
        raise ProxyEvidenceError("terminal guard semantics differ from source evidence")
    if (
        encoder.get("name") != "libx264"
        or encoder.get("lossless_qp") != 0
        or encoder.get("b_frames") != 0
        or encoder.get("pixel_format") != "yuv420p"
    ):
        raise ProxyEvidenceError("encoder settings are not the registered lossless no-B-frame route")
    identification_lines = encoder.get("identification_lines")
    if not isinstance(identification_lines, list) or not identification_lines:
        raise ProxyEvidenceError("libx264 identification is missing")
    preset = encoder.get("preset")
    if not isinstance(preset, str) or not preset:
        raise ProxyEvidenceError("encoder preset is invalid")

    expected_command = build_ffmpeg_command(
        ffmpeg_path,
        source_path,
        proxy_path,
        business_frame_count=count,
        time_base_numerator=time_base["numerator"],
        time_base_denominator=time_base["denominator"],
        duration_ticks=duration_ticks,
        source_start_frame=source_start,
        preset=preset,
    )
    canonical_command = generation.get("canonical_command")
    executed_command = generation.get("executed_command")
    for field, expected in (
        ("source_frame_start", source_start),
        ("source_frame_end_exclusive", source_frame_end),
        ("source_frame_domain", expected_mapping["source_frame_domain"]),
        ("proxy_frame_domain", expected_mapping["proxy_frame_domain"]),
        ("frame_mapping", expected_mapping),
    ):
        if source_start != 0 or field in generation:
            if generation.get(field) != expected:
                raise ProxyEvidenceError(
                    f"generation {field} is not bound to the source window"
                )
    if canonical_command != expected_command:
        raise ProxyEvidenceError("canonical generation command cannot be re-derived")
    if not isinstance(executed_command, list) or executed_command[:-1] != expected_command[:-1]:
        raise ProxyEvidenceError("executed generation command differs from the registered route")
    if not isinstance(executed_command[-1], str) or Path(executed_command[-1]).suffix.lower() != ".mp4":
        raise ProxyEvidenceError("executed generation command has an invalid temporary output")
    audio = _require_mapping(manifest.get("audio_timeline"), "manifest audio_timeline")
    av_sync = _require_mapping(manifest.get("av_sync"), "manifest av_sync")
    if audio.get("status") != "NOT_RUN" or av_sync.get("status") != "NOT_RUN":
        raise ProxyEvidenceError("Phase 0 video-only manifest must not claim audio or A/V validation")


def verify_proxy(
    manifest_path: Path,
    ffmpeg: frame_oracle.FfmpegExecutable,
    *,
    threads: int,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    manifest_path = manifest_path.expanduser().resolve()
    manifest = _load_json(manifest_path)
    binding_reasons: list[str] = []
    source_evidence: dict[str, Any] | None = None
    proxy_path: Path | None = None
    try:
        if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("kind") != MANIFEST_KIND:
            raise ProxyEvidenceError("proxy manifest schema/kind is invalid")
        source_record = _require_mapping(manifest.get("source"), "manifest source")
        proxy_record = _require_mapping(manifest.get("proxy"), "manifest proxy")
        source_path = _validate_bound_file(source_record, "source")
        proxy_path = _validate_bound_file(proxy_record, "proxy")
        oracle_record = _require_mapping(source_record.get("oracle"), "source oracle binding")
        source_oracle_path = _validate_bound_file(oracle_record, "source oracle")
        source_decode_record = _require_mapping(
            source_record.get("generation_source_decode"),
            "generation source decode binding",
        )
        source_decode_path = _validate_bound_file(
            source_decode_record, "generation source decode"
        )
        source_report = _load_json(source_oracle_path)
        source_decode_report = _load_json(source_decode_path)
        manifest_source_anchor = _require_mapping(
            manifest.get("source_anchor_manifest"),
            "manifest source anchor registration",
        )
        source_decode_anchor = _require_mapping(
            source_decode_report.get("source_anchor_manifest"),
            "source decode source anchor registration",
        )
        if source_decode_anchor != manifest_source_anchor:
            raise ProxyEvidenceError(
                "source decode evidence used another source anchor registration"
            )
        manifest_scope = source_record.get("scope")
        if manifest_scope not in ("prefix", "full"):
            raise ProxyEvidenceError("manifest source scope must be prefix or full")
        manifest_source_start = _require_int(
            source_record.get("business_frame_start", 0),
            "source.business_frame_start",
            minimum=0,
        )
        source_evidence = analyze_source_oracle(
            source_report,
            source_path,
            business_frames=(
                _require_int(
                    source_record.get("business_frame_count"),
                    "source.business_frame_count",
                    minimum=1,
                )
                if manifest_scope == "prefix" or manifest_source_start != 0
                else None
            ),
            source_start_frame=manifest_source_start,
            checksum_report=source_decode_report,
        )
        generation = _require_mapping(manifest.get("generation"), "manifest generation")
        tools = _require_mapping(generation.get("tools"), "generation tools")
        _validate_bound_file(
            _require_mapping(tools.get("pts_normalized_proxy"), "proxy tool binding"),
            "pts_normalized_proxy tool",
        )
        _validate_bound_file(
            _require_mapping(tools.get("verify_mpv_frames"), "oracle tool binding"),
            "verify_mpv_frames tool",
        )
        generated_ffmpeg = _require_mapping(generation.get("ffmpeg"), "generation ffmpeg")
        if not _same_path(Path(str(generated_ffmpeg.get("path", ""))), ffmpeg.path):
            raise ProxyEvidenceError("verification FFmpeg path differs from the generator")
        if sha256_file(ffmpeg.path).lower() != str(generated_ffmpeg.get("sha256", "")).lower():
            raise ProxyEvidenceError("verification FFmpeg SHA-256 differs from the generator")
        current_ffmpeg_version = ffmpeg_version_record(ffmpeg.path)
        if current_ffmpeg_version["version_line"] != generated_ffmpeg.get("version_line"):
            raise ProxyEvidenceError("verification FFmpeg version differs from the generator")
        validate_manifest_semantics(
            manifest,
            source_evidence,
            source_path,
            proxy_path,
            ffmpeg.path,
        )
    except (FileNotFoundError, OSError, ProxyEvidenceError, RuntimeError, ValueError) as exc:
        binding_reasons.append(f"PROXY_BINDING_INVALID:{exc}")

    oracle_report: dict[str, Any] | None = None
    if not binding_reasons and source_evidence is not None and proxy_path is not None:
        oracle_report, _ = frame_oracle.probe_video(
            proxy_path,
            ffmpeg,
            threads=threads,
        )
        video_validation = evaluate_video_proxy(
            manifest, source_evidence["rows"], oracle_report
        )
    else:
        video_validation = {
            "status": "BLOCKED",
            "reason_codes": ["PROXY_BINDING_INVALID"],
        }

    overall_reasons = list(binding_reasons)
    overall_reasons.extend(video_validation.get("reason_codes", []))
    if source_evidence is not None and source_evidence.get("scope") == "prefix":
        overall_reasons.append("SOURCE_PREFIX_ONLY")
    overall_reasons.extend(["AUDIO_TIMELINE_NOT_VERIFIED", "AV_SYNC_NOT_VERIFIED"])
    overall_reasons = list(dict.fromkeys(overall_reasons))
    report = {
        "schema_version": SCHEMA_VERSION,
        "kind": REPORT_KIND,
        "created_utc": _utc_now(),
        "status": "BLOCKED",
        "reason_codes": overall_reasons,
        "proxy_ready_for_gate": False,
        "scope": source_evidence.get("scope") if source_evidence else "unknown",
        "source_frame_domain": (
            source_evidence.get("source_frame_domain") if source_evidence else None
        ),
        "proxy_frame_domain": (
            source_evidence.get("proxy_frame_domain") if source_evidence else None
        ),
        "frame_mapping": (
            source_evidence.get("frame_mapping") if source_evidence else None
        ),
        "authoritative_for_full_source": False,
        "manifest": {
            "path": str(manifest_path),
            "sha256": sha256_file(manifest_path),
        },
        "binding": {
            "status": "PASS" if not binding_reasons else "BLOCKED",
            "reason_codes": binding_reasons,
        },
        "video_validation": video_validation,
        "audio_timeline": {"status": "NOT_RUN"},
        "av_sync": {"status": "NOT_RUN"},
        "exit_code": EXIT_BLOCKED,
    }
    return report, oracle_report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build or verify a versioned PTS-normalized video proxy"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build")
    build.add_argument("source", type=Path)
    build.add_argument("--source-oracle", type=Path, required=True)
    build.add_argument("--source-anchor-manifest", type=Path, required=True)
    build.add_argument("--source-decode-output", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--manifest", type=Path, required=True)
    build.add_argument("--business-frames", type=int, default=None)
    build.add_argument("--source-start-frame", type=int, default=0)
    build.add_argument("--ffmpeg", type=Path, default=None)
    build.add_argument("--preset", default=DEFAULT_PRESET)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("manifest", type=Path)
    verify_parser.add_argument("--output", type=Path, required=True)
    verify_parser.add_argument("--oracle-output", type=Path, required=True)
    verify_parser.add_argument("--ffmpeg", type=Path, default=None)
    verify_parser.add_argument("--threads", type=int, default=1)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if getattr(args, "business_frames", None) is not None and args.business_frames < 1:
        parser.error("--business-frames must be at least 1")
    if getattr(args, "source_start_frame", 0) < 0:
        parser.error("--source-start-frame must be at least 0")
    if (
        getattr(args, "source_start_frame", 0) != 0
        and getattr(args, "business_frames", None) is None
    ):
        parser.error(
            "--business-frames is required when --source-start-frame is non-zero"
        )
    if getattr(args, "threads", 1) < 1:
        parser.error("--threads must be at least 1")
    try:
        ffmpeg = frame_oracle.resolve_ffmpeg(args.ffmpeg)
        if args.command == "build":
            manifest = build_proxy(
                args.source,
                args.source_oracle,
                args.source_decode_output,
                args.output,
                args.manifest,
                ffmpeg,
                source_anchor_manifest_path=args.source_anchor_manifest,
                business_frames=args.business_frames,
                source_start_frame=args.source_start_frame,
                preset=args.preset,
            )
            print(
                json.dumps(
                    {
                        "status": manifest["status"],
                        "proxy": manifest["proxy"]["path"],
                        "manifest": str(args.manifest.expanduser().resolve()),
                        "business_frame_count": manifest["source"]["business_frame_count"],
                        "source_start_frame": manifest["source"][
                            "business_frame_start"
                        ],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return EXIT_PASS

        output = args.output.expanduser().resolve()
        oracle_output = args.oracle_output.expanduser().resolve()
        if output.exists() or oracle_output.exists():
            raise FileExistsError("verification outputs are write-once; choose new paths")
        report, oracle_report = verify_proxy(
            args.manifest,
            ffmpeg,
            threads=args.threads,
        )
        if oracle_report is not None:
            write_json_new(oracle_output, oracle_report)
            report["decoded_proxy_oracle"] = {
                "path": str(oracle_output),
                "sha256": sha256_file(oracle_output),
                "status": oracle_report.get("status"),
            }
        write_json_new(output, report)
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "video_status": report["video_validation"]["status"],
                    "reason_codes": report["reason_codes"],
                    "report": str(output),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return EXIT_BLOCKED
    except (FileNotFoundError, FileExistsError, OSError, ProxyEvidenceError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
