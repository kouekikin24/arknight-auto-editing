#!/usr/bin/env python3
"""Scan a short source window for independently observable A/V events.

This is a diagnostic tool, not a gate.  It deliberately has a hard duration
limit and reports candidate visual changes and audio transients without
claiming that they are aligned.  A human (or a separately reviewed manifest
creator) must promote an observed event to a source-anchor registration.

Report schema v3 treats every reported time as a requested FFmpeg seek plus a
decoded sample-grid offset.  Those values are useful for locating an event,
but they are not source-media PTS and must never be consumed as authoritative
timeline mapping.  An optional frame oracle or isolated reconciliation report
binds source identity and records its scan scope only; neither upgrades
candidate times into media time.
Schema v1 and v2 reports predate this contract and must be rejected.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import statistics
import struct
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import verify_mpv_frames as frame_oracle
import adapt_legacy_full_oracle as reconciliation_verifier


SCHEMA_VERSION = 3
MANIFEST_KIND = "mpv_phase0_av_event_candidate_scan"
FRAME_ORACLE_KIND = "mpv_phase0_frame_pts_oracle"
RECONCILIATION_KIND = "mpv_phase0_legacy_full_oracle_reconciliation"
MAX_SCAN_SECONDS = 10.0
DEFAULT_SCAN_SECONDS = 10.0
DEFAULT_SAMPLE_FPS = 10.0
CONTACT_SHEET_FPS = 2.0
CONTACT_WIDTH = 320
CONTACT_HEIGHT = 180
CONTACT_COLUMNS = 5
CONTACT_ROWS = 4
AUDIO_RATE = 48_000
AUDIO_WINDOW_SAMPLES = 960  # 20 ms at 48 kHz
EXIT_PASS = 0
EXIT_BLOCKED = 10
EXIT_FAILED = 20
_NO_AUDIO_STREAM_RE = re.compile(
    r"Stream map\s+['\"]?0:a:0['\"]?\s+matches no streams",
    re.IGNORECASE,
)


class EventScanError(ValueError):
    """The bounded event scan could not produce auditable evidence."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_record(path: Path, *, include_size: bool = True) -> dict[str, Any]:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    result: dict[str, Any] = {"path": str(path), "sha256": sha256_file(path)}
    if include_size:
        result["size"] = path.stat().st_size
    return result


def ffmpeg_record(path: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [str(path), "-version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if completed.returncode != 0 or not lines:
        raise EventScanError("FFmpeg version probe failed")
    return {
        **file_record(path),
        "version_line": lines[0],
        "library_lines": [line for line in lines if line.startswith("lib")],
    }


def write_json_new(
    path: Path,
    value: dict[str, Any],
    *,
    before_publish: Callable[[], None] | None = None,
) -> None:
    """Crash-safely publish immutable JSON without replacing prior evidence."""
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    temporary = _temporary_path(path, ".json.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if before_publish is not None:
            before_publish()
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _temporary_path(path: Path, suffix: str | None = None) -> Path:
    return path.with_name(
        f"{path.stem}.{uuid.uuid4().hex}.tmp{suffix or path.suffix}"
    )


def _run_bytes(command: list[str], *, name: str) -> bytes:
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise EventScanError(f"{name} failed ({completed.returncode}): {detail}")
    return completed.stdout


def _require_plain_int(value: Any, *, name: str, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise EventScanError(f"{name} must be an integer >= {minimum}")
    return value


def _oracle_max_frames(command: Sequence[Any]) -> int | None:
    if not all(isinstance(item, str) for item in command):
        raise EventScanError("source oracle ffmpeg.command must contain only strings")
    positions = [index for index, item in enumerate(command) if item == "-frames:v"]
    if not positions:
        return None
    if len(positions) != 1 or positions[0] + 1 >= len(command):
        raise EventScanError("source oracle has malformed -frames:v scope")
    try:
        value = int(command[positions[0] + 1])
    except ValueError as exc:
        raise EventScanError("source oracle -frames:v must be a positive integer") from exc
    if value <= 0:
        raise EventScanError("source oracle -frames:v must be a positive integer")
    return value


def _validate_exact_file_binding(value: Any, *, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EventScanError(f"{name} must be an object")
    declared_path = value.get("path")
    declared_sha = value.get("sha256")
    declared_size = value.get("size")
    if not isinstance(declared_path, str) or not isinstance(declared_sha, str):
        raise EventScanError(f"{name} path or SHA-256 is missing")
    actual = file_record(Path(declared_path))
    if (
        actual["sha256"].lower() != declared_sha.lower()
        or actual["size"] != _require_plain_int(declared_size, name=f"{name}.size")
    ):
        raise EventScanError(f"{name} binding is stale")
    return actual


def _load_reconciliation_binding(
    oracle_path: Path,
    oracle_record: dict[str, Any],
    *,
    source_record: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        verified = reconciliation_verifier.verify_reconciliation_evidence(
            oracle_path
        )
    except (
        reconciliation_verifier.LegacyOracleAdapterError,
        FileNotFoundError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        raise EventScanError(f"reconciliation evidence verification failed: {exc}") from exc
    verified_record = _validate_exact_file_binding(
        verified.get("record"), name="reconciliation evidence"
    )
    if verified_record != oracle_record:
        raise EventScanError("reconciliation evidence changed while it was loaded")
    source = verified["source_media"]
    if (
        os.path.normcase(str(Path(source["path"]).expanduser().resolve()))
        != os.path.normcase(str(Path(source_record["path"]).resolve()))
        or source["sha256"].lower() != source_record["sha256"].lower()
        or source["size"] != source_record["size"]
        or source["mtime_ns"] != Path(source_record["path"]).stat().st_mtime_ns
    ):
        raise EventScanError("reconciliation is not bound to the scanned source identity")
    bound_evidence = {
        name: _validate_exact_file_binding(
            record, name=f"reconciliation {name}"
        )
        for name, record in verified["bound_evidence"].items()
    }
    parsed_frames = verified["frame_count"]
    return verified_record, {
        "provided": True,
        "file": verified_record,
        "schema_version": 1,
        "kind": RECONCILIATION_KIND,
        "scan_scope": "complete",
        "full_scan": True,
        "parsed_frames": parsed_frames,
        "binding_purpose": (
            "source_media_identity_frame_order_checksums_and_pts_conflict_provenance_only"
        ),
        "provides_requested_time_mapping": False,
        "time_authority": "none",
        "gate_approval": False,
        "conflict_indices": verified["conflict_indices"],
        "bound_evidence": bound_evidence,
    }


def _load_source_oracle_binding(
    path: Path,
    *,
    source_record: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate identity evidence without granting any media-time authority."""
    oracle_path = path.expanduser().resolve()
    oracle_record = file_record(oracle_path)
    try:
        oracle = json.loads(oracle_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EventScanError(f"cannot read source oracle: {exc}") from exc
    if not isinstance(oracle, dict):
        raise EventScanError("source oracle must be an object")
    oracle_schema_version = _require_plain_int(
        oracle.get("schema_version"),
        name="source oracle schema_version",
        minimum=1,
    )
    if oracle_schema_version != 1:
        raise EventScanError("source oracle schema_version must be 1")
    kind = oracle.get("kind")
    if kind == RECONCILIATION_KIND:
        return _load_reconciliation_binding(
            oracle_path,
            oracle_record,
            source_record=source_record,
        )
    if kind != FRAME_ORACLE_KIND:
        raise EventScanError("source oracle kind is not an accepted identity evidence kind")

    video = oracle.get("video")
    if not isinstance(video, dict):
        raise EventScanError("source oracle video must be an object")
    oracle_sha256 = video.get("sha256")
    oracle_size = video.get("size")
    if (
        not isinstance(oracle_sha256, str)
        or oracle_sha256.lower() != source_record["sha256"]
        or _require_plain_int(oracle_size, name="source oracle video.size")
        != source_record["size"]
    ):
        raise EventScanError("source oracle is not bound to the scanned media hash and size")

    ffmpeg = oracle.get("ffmpeg")
    if not isinstance(ffmpeg, dict):
        raise EventScanError("source oracle ffmpeg must be an object")
    ffmpeg_returncode = ffmpeg.get("returncode")
    if (
        not isinstance(ffmpeg_returncode, int)
        or isinstance(ffmpeg_returncode, bool)
        or ffmpeg_returncode != 0
    ):
        raise EventScanError("source oracle ffmpeg.returncode must be 0")
    command = ffmpeg.get("command")
    if not isinstance(command, list):
        raise EventScanError("source oracle ffmpeg.command must be a list")
    max_frames = _oracle_max_frames(command)

    pts_table = oracle.get("pts_table")
    if not isinstance(pts_table, list) or not all(
        isinstance(row, dict) for row in pts_table
    ):
        raise EventScanError("source oracle pts_table must be a list of objects")
    showinfo = oracle.get("showinfo")
    if not isinstance(showinfo, dict):
        raise EventScanError("source oracle showinfo must be an object")
    parsed_frames = _require_plain_int(
        showinfo.get("parsed_frames"),
        name="source oracle showinfo.parsed_frames",
        minimum=1,
    )
    if parsed_frames != len(pts_table):
        raise EventScanError("source oracle parsed_frames does not match pts_table length")

    assessment = oracle.get("pts_conflict_assessment")
    if not isinstance(assessment, dict):
        raise EventScanError("source oracle pts_conflict_assessment must be an object")
    scan_scope = assessment.get("scan_scope")
    if scan_scope not in {"complete", "partial"}:
        raise EventScanError("source oracle scan_scope must be complete or partial")
    full_scan = scan_scope == "complete"
    if full_scan and max_frames is not None:
        raise EventScanError("complete source oracle must not use -frames:v")
    if not full_scan and max_frames is None:
        raise EventScanError("partial source oracle must declare -frames:v")
    if max_frames is not None and parsed_frames > max_frames:
        raise EventScanError("partial source oracle parsed more than its -frames:v limit")

    binding = {
        "provided": True,
        "file": oracle_record,
        "schema_version": 1,
        "kind": FRAME_ORACLE_KIND,
        "scan_scope": scan_scope,
        "full_scan": full_scan,
        "parsed_frames": parsed_frames,
        "binding_purpose": "source_media_identity_and_oracle_provenance_only",
        "provides_requested_time_mapping": False,
        "time_authority": "none",
    }
    return oracle_record, binding


def _assert_input_identities_unchanged(
    snapshots: dict[str, tuple[Path, dict[str, Any]]],
) -> None:
    for label, (path, expected) in snapshots.items():
        try:
            actual = file_record(path)
        except (FileNotFoundError, OSError) as exc:
            raise EventScanError(f"{label} identity changed before publication") from exc
        for key in ("path", "sha256", "size"):
            if actual.get(key) != expected.get(key):
                raise EventScanError(f"{label} identity changed before publication")


def validate_scan_report_schema(report: dict[str, Any]) -> None:
    """Reject legacy candidate reports before any future consumer uses them."""
    if not isinstance(report, dict):
        raise EventScanError("candidate scan report must be an object")
    if report.get("schema_version") != SCHEMA_VERSION:
        raise EventScanError(f"candidate scan report schema_version must be {SCHEMA_VERSION}")
    if report.get("kind") != MANIFEST_KIND:
        raise EventScanError(f"candidate scan report kind must be {MANIFEST_KIND}")
    if report.get("scope") != "bounded_source_seek_request":
        raise EventScanError("candidate scan report scope is not a bounded seek request")
    time_basis = report.get("time_basis")
    if (
        not isinstance(time_basis, dict)
        or time_basis.get("media_pts_authority") != "none"
        or time_basis.get("can_register_source_anchor_directly") is not False
    ):
        raise EventScanError("candidate scan report must deny media PTS authority")
    window = report.get("window")
    if not isinstance(window, dict):
        raise EventScanError("candidate scan report window must be an object")
    for key in (
        "requested_start_seconds",
        "requested_duration_seconds",
        "requested_end_seconds_exclusive",
    ):
        value = window.get(key)
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
        ):
            raise EventScanError(f"candidate scan report window.{key} must be finite")
    oracle_binding = report.get("source_oracle")
    if (
        not isinstance(oracle_binding, dict)
        or oracle_binding.get("provides_requested_time_mapping") is not False
        or oracle_binding.get("time_authority") != "none"
    ):
        raise EventScanError("candidate scan source oracle must be identity-only")
    candidates = report.get("candidates")
    if not isinstance(candidates, list):
        raise EventScanError("candidate scan report candidates must be a list")
    required_candidate_times = (
        "video_window_offset_seconds",
        "requested_video_position_seconds",
        "audio_window_offset_seconds",
        "requested_audio_position_seconds",
        "sample_grid_delta_seconds",
    )
    forbidden_legacy_times = {
        "window_time_seconds",
        "source_time_seconds",
        "audio_window_time_seconds",
        "requested_source_time_seconds",
        "requested_audio_source_time_seconds",
        "audio_video_match_delta_seconds",
    }
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise EventScanError("candidate scan entries must be objects")
        if forbidden_legacy_times.intersection(candidate):
            raise EventScanError("candidate scan entry uses a legacy time field")
        for key in required_candidate_times:
            value = candidate.get(key)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
            ):
                raise EventScanError(f"candidate scan entry {key} must be finite")
        expected_delta = (
            float(candidate["requested_audio_position_seconds"])
            - float(candidate["requested_video_position_seconds"])
        )
        if not math.isclose(
            float(candidate["sample_grid_delta_seconds"]),
            expected_delta,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise EventScanError("candidate scan sample-grid delta is inconsistent")


def _scaled_filter(*, fps: float) -> str:
    return (
        f"fps={fps:g},scale={CONTACT_WIDTH}:{CONTACT_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={CONTACT_WIDTH}:{CONTACT_HEIGHT}:(ow-iw)/2:(oh-ih)/2,format=rgb24"
    )


def video_sample_command(
    ffmpeg: Path,
    media: Path,
    start: float,
    duration: float,
    fps: float,
) -> list[str]:
    return [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(media),
        "-ss",
        f"{start:.6f}",
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
        "-t",
        f"{duration:.6f}",
        "-vf",
        _scaled_filter(fps=fps),
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "pipe:1",
    ]


def contact_sheet_command(
    ffmpeg: Path,
    media: Path,
    start: float,
    duration: float,
    output: Path,
) -> list[str]:
    return [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(media),
        "-ss",
        f"{start:.6f}",
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
        "-t",
        f"{duration:.6f}",
        "-vf",
        _scaled_filter(fps=CONTACT_SHEET_FPS)
        + f",tile={CONTACT_COLUMNS}x{CONTACT_ROWS}:padding=2:margin=2",
        "-frames:v",
        "1",
        "-f",
        "image2",
        "-y",
        str(output),
    ]


def audio_sample_command(
    ffmpeg: Path,
    media: Path,
    start: float,
    duration: float,
) -> list[str]:
    return [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(media),
        "-ss",
        f"{start:.6f}",
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-t",
        f"{duration:.6f}",
        "-ac",
        "1",
        "-ar",
        str(AUDIO_RATE),
        "-f",
        "s16le",
        "pipe:1",
    ]


def _visual_change_scores(frames: Sequence[bytes]) -> list[float]:
    if not frames:
        return []
    scores = [0.0]
    # Sampling every 16th byte is enough for a candidate list and keeps this
    # diagnostic bounded even when a caller raises the thumbnail rate.
    for previous, current in zip(frames, frames[1:]):
        if len(previous) != len(current) or not current:
            scores.append(float("inf"))
            continue
        total = sum(abs(current[index] - previous[index]) for index in range(0, len(current), 16))
        scores.append(total / max(1, len(range(0, len(current), 16))))
    return scores


def _audio_window_metrics(raw: bytes) -> list[dict[str, float | int]]:
    usable = len(raw) - (len(raw) % 2)
    if usable <= 0:
        return []
    samples = struct.unpack(f"<{usable // 2}h", raw[:usable])
    metrics: list[dict[str, float | int]] = []
    for start in range(0, len(samples), AUDIO_WINDOW_SAMPLES):
        window = samples[start : start + AUDIO_WINDOW_SAMPLES]
        if not window:
            continue
        peak = max(abs(value) for value in window)
        rms = math.sqrt(sum(value * value for value in window) / len(window))
        metrics.append(
            {
                "start_sample": start,
                "end_sample_exclusive": start + len(window),
                "start_seconds": start / AUDIO_RATE,
                "end_seconds": (start + len(window)) / AUDIO_RATE,
                "peak": int(peak),
                "rms": rms,
            }
        )
    return metrics


def _robust_cutoff(values: Sequence[float], multiplier: float = 3.0) -> float:
    finite = [value for value in values if math.isfinite(value)]
    if not finite:
        return float("inf")
    baseline = statistics.median(finite)
    deviations = [abs(value - baseline) for value in finite]
    mad = statistics.median(deviations)
    return max(baseline * multiplier, baseline + max(1.0, mad * 6.0))


def _candidate_events(
    visual_scores: Sequence[float],
    audio_metrics: Sequence[dict[str, float | int]],
    *,
    fps: float,
    window_start_seconds: float = 0.0,
) -> list[dict[str, Any]]:
    visual_cutoff = _robust_cutoff(visual_scores[1:])
    rms_cutoff = _robust_cutoff([float(item["rms"]) for item in audio_metrics])
    peak_cutoff = max(
        1000.0,
        _robust_cutoff([float(item["peak"]) for item in audio_metrics]),
    )
    candidates: list[dict[str, Any]] = []
    for frame_index, visual_score in enumerate(visual_scores):
        frame_time = frame_index / fps
        audio_index = min(
            range(len(audio_metrics)),
            key=lambda index: abs(float(audio_metrics[index]["start_seconds"]) - frame_time),
            default=None,
        )
        if audio_index is None:
            continue
        audio = audio_metrics[audio_index]
        visual_hit = math.isfinite(visual_score) and visual_score >= visual_cutoff
        audio_hit = float(audio["peak"]) >= peak_cutoff and float(audio["rms"]) >= rms_cutoff
        if visual_hit and audio_hit:
            audio_window_time = float(audio["start_seconds"])
            requested_video_source_time = window_start_seconds + frame_time
            requested_audio_source_time = window_start_seconds + audio_window_time
            candidates.append(
                {
                    "frame_sample_index": frame_index,
                    "video_window_offset_seconds": frame_time,
                    "requested_video_position_seconds": requested_video_source_time,
                    "visual_change_score": visual_score,
                    "audio_window_index": audio_index,
                    "audio_window_offset_seconds": audio_window_time,
                    "requested_audio_position_seconds": requested_audio_source_time,
                    "sample_grid_delta_seconds": (
                        requested_audio_source_time - requested_video_source_time
                    ),
                    "audio_peak": audio["peak"],
                    "audio_rms": audio["rms"],
                    "requires_human_observation": True,
                }
            )
    return candidates


def scan_window(
    media: Path,
    output_manifest: Path,
    *,
    ffmpeg: frame_oracle.FfmpegExecutable | Path,
    start_seconds: float = 0.0,
    duration_seconds: float = DEFAULT_SCAN_SECONDS,
    sample_fps: float = DEFAULT_SAMPLE_FPS,
    contact_sheet: Path | None = None,
    source_oracle: Path | None = None,
) -> dict[str, Any]:
    media = media.expanduser().resolve()
    output_manifest = output_manifest.expanduser().resolve()
    if contact_sheet is not None:
        contact_sheet = contact_sheet.expanduser().resolve()
    if not media.is_file():
        raise FileNotFoundError(media)
    if output_manifest.exists():
        raise FileExistsError(f"event scan is write-once: {output_manifest}")
    if contact_sheet is not None and contact_sheet.exists():
        raise FileExistsError(f"contact sheet is write-once: {contact_sheet}")
    if contact_sheet is not None and contact_sheet == output_manifest:
        raise EventScanError(
            "output manifest and contact sheet must use distinct paths"
        )
    if not isinstance(start_seconds, (int, float)) or isinstance(start_seconds, bool):
        raise EventScanError("start_seconds must be numeric")
    if not math.isfinite(float(start_seconds)) or float(start_seconds) < 0.0:
        raise EventScanError("start_seconds must be finite and non-negative")
    if not isinstance(duration_seconds, (int, float)) or isinstance(duration_seconds, bool):
        raise EventScanError("duration_seconds must be numeric")
    if not math.isfinite(float(duration_seconds)) or not (0.0 < float(duration_seconds) <= MAX_SCAN_SECONDS):
        raise EventScanError(f"duration_seconds must be in (0, {MAX_SCAN_SECONDS}]")
    if not isinstance(sample_fps, (int, float)) or isinstance(sample_fps, bool):
        raise EventScanError("sample_fps must be numeric")
    if not math.isfinite(float(sample_fps)) or not (0.1 <= float(sample_fps) <= 10.0):
        raise EventScanError("sample_fps must be between 0.1 and 10")
    ffmpeg_path = ffmpeg.path if isinstance(ffmpeg, frame_oracle.FfmpegExecutable) else Path(ffmpeg)
    ffmpeg_path = ffmpeg_path.expanduser().resolve()
    if not ffmpeg_path.is_file():
        raise FileNotFoundError(ffmpeg_path)
    source_record = file_record(media)
    scanner_path = Path(__file__).resolve()
    scanner_record = file_record(scanner_path)
    source_oracle_binding: dict[str, Any] = {
        "provided": False,
        "file": None,
        "binding_purpose": "none",
        "provides_requested_time_mapping": False,
        "time_authority": "none",
    }
    if source_oracle is not None:
        source_oracle_record, source_oracle_binding = _load_source_oracle_binding(
            source_oracle,
            source_record=source_record,
        )
    ffmpeg_tool_record = ffmpeg_record(ffmpeg_path)
    identity_snapshots: dict[str, tuple[Path, dict[str, Any]]] = {
        "source": (media, source_record),
        "ffmpeg": (ffmpeg_path, ffmpeg_tool_record),
        "scanner": (scanner_path, scanner_record),
    }
    if source_oracle is not None:
        identity_snapshots["source_oracle"] = (
            Path(source_oracle_binding["file"]["path"]),
            source_oracle_binding["file"],
        )
        for name, record in source_oracle_binding.get("bound_evidence", {}).items():
            identity_snapshots[f"reconciliation.{name}"] = (
                Path(record["path"]),
                record,
            )

    raw_video = _run_bytes(
        video_sample_command(
            ffmpeg_path,
            media,
            float(start_seconds),
            float(duration_seconds),
            float(sample_fps),
        ),
        name="bounded video scan",
    )
    frame_bytes = CONTACT_WIDTH * CONTACT_HEIGHT * 3
    if len(raw_video) % frame_bytes:
        raise EventScanError("bounded video output is not frame aligned")
    frames = [raw_video[offset : offset + frame_bytes] for offset in range(0, len(raw_video), frame_bytes)]
    visual_scores = _visual_change_scores(frames)

    audio_command = audio_sample_command(
        ffmpeg_path, media, float(start_seconds), float(duration_seconds)
    )
    audio_completed = subprocess.run(
        audio_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    audio_stderr = audio_completed.stderr.decode("utf-8", errors="replace").strip()
    if audio_completed.returncode != 0:
        if _NO_AUDIO_STREAM_RE.search(audio_stderr):
            audio_status = "NO_AUDIO_STREAM"
            audio_stream_present = False
            raw_audio = b""
        else:
            raise EventScanError(
                f"bounded audio scan failed ({audio_completed.returncode}): {audio_stderr}"
            )
    else:
        audio_stream_present = True
        raw_audio = audio_completed.stdout
        audio_status = "DECODED" if raw_audio else "NO_AUDIO_SAMPLES_IN_WINDOW"
    if len(raw_audio) % 2:
        raise EventScanError("bounded audio output is not s16le sample aligned")
    audio_metrics = _audio_window_metrics(raw_audio)
    candidates = _candidate_events(
        visual_scores,
        audio_metrics,
        fps=float(sample_fps),
        window_start_seconds=float(start_seconds),
    )

    contact_record: dict[str, Any] | None = None
    contact_command_used: list[str] | None = None
    contact_published_stat: os.stat_result | None = None
    if contact_sheet is not None:
        contact_sheet.parent.mkdir(parents=True, exist_ok=True)
        temporary = _temporary_path(contact_sheet, ".png")
        contact_command_used = contact_sheet_command(
            ffmpeg_path,
            media,
            float(start_seconds),
            float(duration_seconds),
            temporary,
        )
        try:
            _run_bytes(
                contact_command_used,
                name="contact sheet capture",
            )
            if not temporary.is_file() or temporary.stat().st_size == 0:
                raise EventScanError("FFmpeg did not create a contact sheet")
            _assert_input_identities_unchanged(identity_snapshots)
            os.link(temporary, contact_sheet)
            contact_published_stat = contact_sheet.stat()
            contact_record = file_record(contact_sheet)
        finally:
            temporary.unlink(missing_ok=True)

    status = "CANDIDATES_FOUND" if candidates else "NO_USABLE_AV_EVENT"
    reasons = [] if candidates else ["NO_USABLE_AV_EVENT"]
    if audio_status != "DECODED":
        reasons.append(audio_status)
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": MANIFEST_KIND,
        "status": status,
        "reason_codes": list(dict.fromkeys(reasons)),
        "scope": "bounded_source_seek_request",
        "time_basis": {
            "requested_seek_seconds": float(start_seconds),
            "seek_basis": "requested_ffmpeg_output_seek_after_input",
            "video_sample_grid": (
                "requested_seek_seconds + frame_sample_index / requested_sample_fps"
            ),
            "audio_sample_grid": (
                "requested_seek_seconds + start_sample / decoded_sample_rate"
            ),
            "candidate_match_delta": (
                "requested_audio_position_seconds - requested_video_position_seconds"
            ),
            "media_pts_authority": "none",
            "can_register_source_anchor_directly": False,
        },
        "window": {
            "requested_start_seconds": float(start_seconds),
            "requested_duration_seconds": float(duration_seconds),
            "requested_end_seconds_exclusive": (
                float(start_seconds) + float(duration_seconds)
            ),
            "max_allowed_seconds": MAX_SCAN_SECONDS,
        },
        "source": source_record,
        "source_oracle": source_oracle_binding,
        "video_sampling": {
            "width": CONTACT_WIDTH,
            "height": CONTACT_HEIGHT,
            "fps": float(sample_fps),
            "frame_count": len(frames),
            "visual_change_scores": visual_scores,
        },
        "audio_sampling": {
            "status": audio_status,
            "stream_present": audio_stream_present,
            "decoded_samples_present": bool(raw_audio),
            "sample_rate": AUDIO_RATE,
            "window_samples": AUDIO_WINDOW_SAMPLES,
            "window_count": len(audio_metrics),
            "clock_basis": "decoded_window_local_diagnostic_only",
            "windows": audio_metrics,
            "decode": {
                "returncode": audio_completed.returncode,
                "stderr": audio_stderr,
            },
        },
        "candidates": candidates,
        "artifacts": {
            "contact_sheet": contact_record,
            "contact_sheet_fps": CONTACT_SHEET_FPS if contact_record else None,
        },
        "tools": {
            "scanner": scanner_record,
            "ffmpeg": ffmpeg_tool_record,
            "commands": {
                "video": video_sample_command(
                    ffmpeg_path,
                    media,
                    float(start_seconds),
                    float(duration_seconds),
                    float(sample_fps),
                ),
                "audio": audio_command,
                "contact_sheet": contact_command_used,
            },
        },
        "publication_identity_check": {
            "status": "PASSED",
            "checked": list(identity_snapshots),
        },
    }
    try:
        validate_scan_report_schema(result)
        _assert_input_identities_unchanged(identity_snapshots)
        write_json_new(
            output_manifest,
            result,
            before_publish=lambda: _assert_input_identities_unchanged(
                identity_snapshots
            ),
        )
    except BaseException:
        if contact_published_stat is not None and contact_sheet is not None:
            try:
                if os.path.samestat(contact_published_stat, contact_sheet.stat()):
                    contact_sheet.unlink()
            except FileNotFoundError:
                pass
        raise
    return result
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("media", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--contact-sheet", type=Path)
    parser.add_argument("--source-oracle", type=Path)
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--duration", type=float, default=DEFAULT_SCAN_SECONDS)
    parser.add_argument("--sample-fps", type=float, default=DEFAULT_SAMPLE_FPS)
    parser.add_argument("--ffmpeg", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        ffmpeg = frame_oracle.resolve_ffmpeg(args.ffmpeg)
        result = scan_window(
            args.media,
            args.output,
            ffmpeg=ffmpeg,
            start_seconds=args.start,
            duration_seconds=args.duration,
            sample_fps=args.sample_fps,
            contact_sheet=args.contact_sheet,
            source_oracle=args.source_oracle,
        )
    except (EventScanError, FileExistsError, FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAILED
    print(json.dumps({"status": result["status"], "reason_codes": result["reason_codes"], "output": str(args.output.resolve())}, ensure_ascii=False, sort_keys=True))
    return EXIT_PASS if result["status"] == "CANDIDATES_FOUND" else EXIT_BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())
