#!/usr/bin/env python3
"""Bind legacy full-oracle timing to an existing full checksum decode.

This is an offline evidence adapter. It reads three immutable JSON artifacts
and never opens the source media or starts FFmpeg:

* the legacy full PTS oracle;
* the source-decode evidence created while building a normalized proxy; and
* that proxy's generation manifest, which reverse-binds the source decode.

The output deliberately has its own kind. It is diagnostic frame-order,
checksum, and PTS-conflict evidence, not a normal frame oracle, media-time
authority, Gate PASS, or production input.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence


SCHEMA_VERSION = 1
LEGACY_ORACLE_KIND = "mpv_phase0_frame_pts_oracle"
SOURCE_DECODE_KIND = "mpv_phase0_proxy_source_decode_evidence"
GENERATION_MANIFEST_KIND = "mpv_phase0_pts_normalized_proxy"
ADAPTER_KIND = "mpv_phase0_legacy_full_oracle_reconciliation"
EVIDENCE_ROLE = "diagnostic_evidence_only"
EXIT_BLOCKED = 10
EXIT_FAILED = 20
_CHECKSUM_RE = re.compile(r"^[0-9A-Fa-f]{8}$")
_SHA256_RE = re.compile(r"^[0-9A-Fa-f]{64}$")
_REQUIRED_CONFLICT_REASONS = frozenset(
    {"PTS_DUPLICATE", "PTS_NON_MONOTONIC"}
)


class LegacyOracleAdapterError(ValueError):
    """The existing artifacts cannot be reconciled without guessing."""


class EvidenceSnapshot:
    def __init__(
        self,
        *,
        path: Path,
        value: dict[str, Any],
        sha256: str,
        size: int,
    ) -> None:
        self.path = path
        self.value = value
        self.sha256 = sha256
        self.size = size

    def record(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "size": self.size,
        }

    def assert_unchanged(self) -> None:
        try:
            payload = self.path.read_bytes()
        except OSError as exc:
            raise LegacyOracleAdapterError(
                f"input evidence changed or disappeared: {self.path}: {exc}"
            ) from exc
        if len(payload) != self.size or hashlib.sha256(payload).hexdigest() != self.sha256:
            raise LegacyOracleAdapterError(
                f"input evidence changed during reconciliation: {self.path}"
            )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _same_path(left: Any, right: Path) -> bool:
    if not isinstance(left, str):
        return False
    return os.path.normcase(str(Path(left).expanduser().resolve())) == os.path.normcase(
        str(right.expanduser().resolve())
    )


def _load_snapshot(path: Path, name: str) -> EvidenceSnapshot:
    path = path.expanduser().resolve()
    try:
        before = path.stat()
        payload = path.read_bytes()
        after = path.stat()
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LegacyOracleAdapterError(f"cannot read {name}: {path}: {exc}") from exc
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or len(payload) != after.st_size
    ):
        raise LegacyOracleAdapterError(f"{name} changed while it was being read: {path}")
    if not isinstance(value, dict):
        raise LegacyOracleAdapterError(f"{name} must be a JSON object: {path}")
    return EvidenceSnapshot(
        path=path,
        value=value,
        sha256=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
    )


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LegacyOracleAdapterError(f"{name} must be an object")
    return value


def _require_int(value: Any, name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LegacyOracleAdapterError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise LegacyOracleAdapterError(f"{name} must be at least {minimum}")
    return value


def _require_finite(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LegacyOracleAdapterError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0.0):
        qualifier = "finite and positive" if positive else "finite"
        raise LegacyOracleAdapterError(f"{name} must be {qualifier}")
    return result


def _require_sha256(value: Any, name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise LegacyOracleAdapterError(f"{name} is not a SHA-256 value")
    return value.lower()


def _require_reason_codes(report: dict[str, Any], name: str) -> list[str]:
    values = report.get("reason_codes")
    if not isinstance(values, list) or not all(
        isinstance(value, str) and value for value in values
    ):
        raise LegacyOracleAdapterError(f"{name} reason_codes must be a string list")
    missing = _REQUIRED_CONFLICT_REASONS.difference(values)
    if missing:
        raise LegacyOracleAdapterError(
            f"{name} lacks required PTS conflict reasons: {sorted(missing)}"
        )
    return values


def _require_file_binding(
    value: Any,
    name: str,
    snapshot: EvidenceSnapshot,
) -> dict[str, Any]:
    record = _require_mapping(value, name)
    if not _same_path(record.get("path"), snapshot.path):
        raise LegacyOracleAdapterError(f"{name} path is stale")
    if _require_sha256(record.get("sha256"), f"{name} SHA-256") != snapshot.sha256:
        raise LegacyOracleAdapterError(f"{name} SHA-256 is stale")
    if "size" in record and record.get("size") != snapshot.size:
        raise LegacyOracleAdapterError(f"{name} size is stale")
    return record


def _require_rows(
    report: dict[str, Any],
    name: str,
    *,
    require_checksums: bool,
) -> list[dict[str, Any]]:
    rows = report.get("pts_table")
    if not isinstance(rows, list) or not rows:
        raise LegacyOracleAdapterError(f"{name} has no pts_table")
    normalized: list[dict[str, Any]] = []
    for index, value in enumerate(rows):
        row = _require_mapping(value, f"{name} pts_table[{index}]")
        if row.get("n") != index:
            raise LegacyOracleAdapterError(f"{name} frame indices are not contiguous")
        _require_int(row.get("pts"), f"{name} pts_table[{index}].pts")
        _require_int(
            row.get("duration"),
            f"{name} pts_table[{index}].duration",
            minimum=1,
        )
        _require_finite(row.get("pts_time"), f"{name} pts_table[{index}].pts_time")
        _require_finite(
            row.get("duration_time"),
            f"{name} pts_table[{index}].duration_time",
            positive=True,
        )
        if require_checksums:
            checksum = row.get("checksum")
            if not isinstance(checksum, str) or _CHECKSUM_RE.fullmatch(checksum) is None:
                raise LegacyOracleAdapterError(
                    f"{name} pts_table[{index}].checksum is not an 8-digit showinfo checksum"
                )
            frame_type = row.get("frame_type")
            if not isinstance(frame_type, str) or not frame_type:
                raise LegacyOracleAdapterError(
                    f"{name} pts_table[{index}].frame_type is missing"
                )
            if row.get("is_keyframe") not in (0, 1):
                raise LegacyOracleAdapterError(
                    f"{name} pts_table[{index}].is_keyframe is invalid"
                )
        normalized.append(row)
    return normalized


def _compare_timeline(
    legacy_rows: Sequence[dict[str, Any]],
    decode_rows: Sequence[dict[str, Any]],
) -> None:
    if len(legacy_rows) != len(decode_rows):
        raise LegacyOracleAdapterError(
            "legacy oracle and source decode frame counts differ"
        )
    fields = ("n", "pts", "duration", "pts_time", "duration_time")
    for index, (legacy, decoded) in enumerate(zip(legacy_rows, decode_rows)):
        for field in fields:
            if legacy.get(field) != decoded.get(field):
                raise LegacyOracleAdapterError(
                    f"timeline mismatch at frame {index}: {field} differs"
                )


def _derive_conflicts(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    first_by_pts: dict[int, tuple[int, str]] = {}
    duplicate_pairs: list[dict[str, Any]] = []
    non_monotonic_pairs: list[dict[str, Any]] = []
    conflict_indices: set[int] = set()
    event_indices: list[int] = []
    previous: tuple[int, int] | None = None
    for index, row in enumerate(rows):
        pts = int(row["pts"])
        checksum = str(row["checksum"]).upper()
        first = first_by_pts.get(pts)
        if first is None:
            first_by_pts[pts] = (index, checksum)
        else:
            first_index, first_checksum = first
            relation = "same" if first_checksum == checksum else "distinct"
            duplicate_pairs.append(
                {
                    "pts": pts,
                    "first_n": first_index,
                    "n": index,
                    "first_checksum": first_checksum,
                    "checksum": checksum,
                    "checksum_relation": relation,
                }
            )
            conflict_indices.update((first_index, index))
            event_indices.append(index)
        if previous is not None and pts < previous[1]:
            non_monotonic_pairs.append(
                {
                    "previous_n": previous[0],
                    "previous_pts": previous[1],
                    "n": index,
                    "pts": pts,
                }
            )
            conflict_indices.update((previous[0], index))
            event_indices.append(index)
        previous = (index, pts)
    distinct = sum(
        pair["checksum_relation"] == "distinct" for pair in duplicate_pairs
    )
    same = len(duplicate_pairs) - distinct
    return {
        "duplicate_count": len(duplicate_pairs),
        "duplicate_distinct_checksum_count": distinct,
        "duplicate_same_checksum_count": same,
        "duplicate_unknown_checksum_count": 0,
        "non_monotonic_count": len(non_monotonic_pairs),
        "unique_pts_count": len(first_by_pts),
        "conflict_indices": sorted(conflict_indices),
        "first_conflict_event_n": min(event_indices) if event_indices else None,
        "last_conflict_event_n": max(event_indices) if event_indices else None,
        "duplicate_pairs": duplicate_pairs,
        "non_monotonic_pairs": non_monotonic_pairs,
    }


def _require_summary_value(
    summary: dict[str, Any], field: str, expected: Any, name: str
) -> None:
    if summary.get(field) != expected:
        raise LegacyOracleAdapterError(
            f"{name}.{field} is stale: expected {expected!r}, got {summary.get(field)!r}"
        )


def _validate_showinfo_and_duration(
    report: dict[str, Any],
    rows: Sequence[dict[str, Any]],
    conflicts: dict[str, Any],
    name: str,
    *,
    require_checksum_summary: bool,
) -> None:
    frame_count = len(rows)
    showinfo = _require_mapping(report.get("showinfo"), f"{name} showinfo")
    for field in ("parsed_frames", "frame_lines"):
        _require_summary_value(showinfo, field, frame_count, f"{name} showinfo")
    for field in ("malformed_frame_lines", "frames_before_time_base"):
        _require_summary_value(showinfo, field, 0, f"{name} showinfo")
    frame_index = _require_mapping(
        showinfo.get("frame_index"), f"{name} showinfo frame_index"
    )
    for field, expected in (
        ("first", 0),
        ("last", frame_count - 1),
        ("unique_count", frame_count),
        ("discontinuity_count", 0),
        ("duplicate_count", 0),
        ("contiguous_from_zero", True),
    ):
        _require_summary_value(frame_index, field, expected, f"{name} frame_index")

    time_base = _require_mapping(showinfo.get("time_base"), f"{name} time_base")
    numerator = _require_int(time_base.get("numerator"), f"{name} time_base numerator", minimum=1)
    denominator = _require_int(
        time_base.get("denominator"), f"{name} time_base denominator", minimum=1
    )
    expected_text = f"{numerator}/{denominator}"
    if time_base.get("text") != expected_text:
        raise LegacyOracleAdapterError(f"{name} time_base text is stale")
    if showinfo.get("observed_time_bases") != [expected_text]:
        raise LegacyOracleAdapterError(f"{name} observed time bases are incomplete")
    seconds_per_tick = numerator / denominator
    for index, row in enumerate(rows):
        if not math.isclose(
            float(row["pts_time"]),
            int(row["pts"]) * seconds_per_tick,
            rel_tol=0.0,
            abs_tol=5e-7,
        ):
            raise LegacyOracleAdapterError(f"{name} frame {index} PTS time is stale")
        if not math.isclose(
            float(row["duration_time"]),
            int(row["duration"]) * seconds_per_tick,
            rel_tol=0.0,
            abs_tol=5e-7,
        ):
            raise LegacyOracleAdapterError(
                f"{name} frame {index} duration time is stale"
            )

    pts = _require_mapping(showinfo.get("pts"), f"{name} PTS summary")
    for field, expected in (
        ("present_count", frame_count),
        ("missing_count", 0),
        ("duplicate_count", conflicts["duplicate_count"]),
        ("non_monotonic_count", conflicts["non_monotonic_count"]),
        ("unique_count", conflicts["unique_pts_count"]),
        ("strictly_increasing", False),
    ):
        _require_summary_value(pts, field, expected, f"{name} PTS summary")
    if require_checksum_summary:
        for field in (
            "duplicate_distinct_checksum_count",
            "duplicate_same_checksum_count",
            "duplicate_unknown_checksum_count",
        ):
            _require_summary_value(
                pts, field, conflicts[field], f"{name} PTS summary"
            )
    pts_time = _require_mapping(
        showinfo.get("pts_time"), f"{name} PTS-time summary"
    )
    for field, expected in (
        ("present_count", frame_count),
        ("missing_count", 0),
        ("non_monotonic_count", conflicts["non_monotonic_count"]),
        ("time_base_mismatch_count", 0),
    ):
        _require_summary_value(pts_time, field, expected, f"{name} PTS-time summary")

    for field, row in (("first_frame", rows[0]), ("last_frame", rows[-1])):
        summary = _require_mapping(showinfo.get(field), f"{name} {field}")
        for key in ("n", "pts", "pts_time", "duration", "duration_time"):
            _require_summary_value(summary, key, row[key], f"{name} {field}")
        if require_checksum_summary:
            _require_summary_value(
                summary, "checksum", row["checksum"], f"{name} {field}"
            )

    duration = _require_mapping(report.get("duration"), f"{name} duration")
    for field, expected in (
        ("frame_duration_present_count", frame_count),
        ("frame_duration_missing_count", 0),
        ("frame_duration_time_present_count", frame_count),
        ("frame_duration_time_missing_count", 0),
        ("non_positive_count", 0),
        ("time_base_mismatch_count", 0),
    ):
        _require_summary_value(duration, field, expected, f"{name} duration")


def _validate_conflict_contract(
    legacy: dict[str, Any],
    decoded: dict[str, Any],
    conflicts: dict[str, Any],
) -> None:
    if conflicts["duplicate_count"] < 1 or conflicts["non_monotonic_count"] < 1:
        raise LegacyOracleAdapterError(
            "reconciled evidence must contain both duplicate and non-monotonic PTS"
        )
    if conflicts["duplicate_distinct_checksum_count"] != conflicts["duplicate_count"]:
        raise LegacyOracleAdapterError(
            "every duplicate PTS must bind distinct decoded-picture checksums"
        )
    if conflicts["duplicate_same_checksum_count"] or conflicts[
        "duplicate_unknown_checksum_count"
    ]:
        raise LegacyOracleAdapterError(
            "duplicate PTS checksum relation is not the required distinct relation"
        )
    _require_reason_codes(legacy, "legacy oracle")
    _require_reason_codes(decoded, "source decode")
    assessment = _require_mapping(
        decoded.get("pts_conflict_assessment"),
        "source decode PTS conflict assessment",
    )
    for field, expected in (
        ("status", "conflict"),
        ("scan_scope", "complete"),
        ("automatic_repair_safe", False),
        ("automatic_sort_or_deduplicate_allowed", False),
        ("first_conflict_frame_n", conflicts["first_conflict_event_n"]),
        ("last_conflict_frame_n", conflicts["last_conflict_event_n"]),
    ):
        _require_summary_value(assessment, field, expected, "conflict assessment")


def _validate_report_identity(
    report: dict[str, Any], name: str, expected_kind: str
) -> None:
    if report.get("schema_version") != SCHEMA_VERSION or report.get("kind") != expected_kind:
        raise LegacyOracleAdapterError(f"{name} schema or kind is invalid")
    if report.get("status") != "BLOCKED":
        raise LegacyOracleAdapterError(f"{name} must remain BLOCKED")
    if report.get("authoritative_frame_timeline") is not False:
        raise LegacyOracleAdapterError(
            f"{name} authoritative_frame_timeline must be false"
        )
    ffmpeg = _require_mapping(report.get("ffmpeg"), f"{name} FFmpeg result")
    if ffmpeg.get("returncode") != 0:
        raise LegacyOracleAdapterError(f"{name} FFmpeg run did not complete")


def _validate_inputs(
    legacy_snapshot: EvidenceSnapshot,
    decode_snapshot: EvidenceSnapshot,
    manifest_snapshot: EvidenceSnapshot,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    legacy = legacy_snapshot.value
    decoded = decode_snapshot.value
    manifest = manifest_snapshot.value
    _validate_report_identity(legacy, "legacy oracle", LEGACY_ORACLE_KIND)
    _validate_report_identity(decoded, "source decode", SOURCE_DECODE_KIND)
    if decoded.get("decode_diagnostic_error_count") != 0:
        raise LegacyOracleAdapterError("source decode contains diagnostic errors")

    legacy_video = _require_mapping(legacy.get("video"), "legacy oracle video")
    decoded_video = _require_mapping(decoded.get("video"), "source decode video")
    if not _same_path(legacy_video.get("path"), Path(str(decoded_video.get("path")))):
        raise LegacyOracleAdapterError("legacy and source-decode media paths differ")
    for field in ("size", "mtime_ns"):
        if legacy_video.get(field) != decoded_video.get(field):
            raise LegacyOracleAdapterError(f"media {field} differs between reports")
    source_sha = _require_sha256(
        decoded_video.get("sha256"), "source decode media SHA-256"
    )
    legacy_sha = legacy_video.get("sha256")
    if legacy_sha is not None and _require_sha256(
        legacy_sha, "legacy oracle media SHA-256"
    ) != source_sha:
        raise LegacyOracleAdapterError(
            "legacy oracle media SHA-256 differs from source decode"
        )

    reference = _require_mapping(
        decoded.get("reference_oracle"), "source decode reference oracle"
    )
    _require_file_binding(reference, "source decode reference oracle", legacy_snapshot)
    if reference.get("scope") != "full":
        raise LegacyOracleAdapterError("source decode reference scope is not full")

    legacy_rows = _require_rows(
        legacy, "legacy full oracle", require_checksums=False
    )
    decoded_rows = _require_rows(
        decoded, "source decode evidence", require_checksums=True
    )
    _compare_timeline(legacy_rows, decoded_rows)
    frame_count = len(decoded_rows)
    if reference.get("reference_frame_count") != frame_count or reference.get(
        "business_frame_count"
    ) != frame_count:
        raise LegacyOracleAdapterError(
            "source decode reference frame domain is incomplete or stale"
        )

    conflicts = _derive_conflicts(decoded_rows)
    _validate_showinfo_and_duration(
        legacy,
        legacy_rows,
        conflicts,
        "legacy oracle",
        require_checksum_summary=False,
    )
    _validate_showinfo_and_duration(
        decoded,
        decoded_rows,
        conflicts,
        "source decode",
        require_checksum_summary=True,
    )
    _validate_conflict_contract(legacy, decoded, conflicts)

    if (
        manifest.get("schema_version") != SCHEMA_VERSION
        or manifest.get("kind") != GENERATION_MANIFEST_KIND
        or manifest.get("status") != "BUILT_UNVERIFIED"
    ):
        raise LegacyOracleAdapterError(
            "generation manifest schema, kind, or status is invalid"
        )
    source = _require_mapping(manifest.get("source"), "generation manifest source")
    if (
        not _same_path(source.get("path"), Path(str(decoded_video.get("path"))))
        or _require_sha256(source.get("sha256"), "manifest source SHA-256")
        != source_sha
        or source.get("size") != decoded_video.get("size")
        or source.get("mtime_ns") != decoded_video.get("mtime_ns")
        or source.get("scope") != "full"
        or source.get("business_frame_start") != 0
        or source.get("business_frame_count") != frame_count
        or source.get("identity_unchanged_during_generation") is not True
    ):
        raise LegacyOracleAdapterError(
            "generation manifest source identity or full frame domain is stale"
        )
    _require_file_binding(
        source.get("oracle"), "generation manifest source oracle", legacy_snapshot
    )
    oracle_binding = _require_mapping(
        source.get("oracle"), "generation manifest source oracle"
    )
    if (
        oracle_binding.get("scope") != "full"
        or oracle_binding.get("parsed_frames") != frame_count
        or not _REQUIRED_CONFLICT_REASONS.issubset(
            set(oracle_binding.get("reason_codes") or [])
        )
    ):
        raise LegacyOracleAdapterError(
            "generation manifest source-oracle scope or conflict binding is stale"
        )
    decode_binding = _require_file_binding(
        source.get("generation_source_decode"),
        "generation manifest source decode",
        decode_snapshot,
    )
    if (
        decode_binding.get("observed_frames") != frame_count
        or decode_binding.get("business_checksum_frames") != frame_count
        or str(source.get("first_checksum", "")).upper()
        != str(decoded_rows[0]["checksum"]).upper()
        or str(source.get("last_checksum", "")).upper()
        != str(decoded_rows[-1]["checksum"]).upper()
    ):
        raise LegacyOracleAdapterError(
            "generation manifest source checksum sequence is stale"
        )

    generation = _require_mapping(
        manifest.get("generation"), "generation manifest generation"
    )
    executed_command = generation.get("executed_command")
    decoded_ffmpeg = _require_mapping(decoded.get("ffmpeg"), "source decode FFmpeg")
    decoded_command = decoded_ffmpeg.get("command")
    if (
        not isinstance(executed_command, list)
        or not executed_command
        or executed_command != decoded_command
    ):
        raise LegacyOracleAdapterError(
            "source decode command is not reverse-bound by the generation manifest"
        )
    generation_ffmpeg = _require_mapping(
        generation.get("ffmpeg"), "generation manifest FFmpeg"
    )
    if not _same_path(
        decoded_ffmpeg.get("path"), Path(str(generation_ffmpeg.get("path")))
    ):
        raise LegacyOracleAdapterError(
            "source decode FFmpeg path differs from the generation manifest"
        )
    return decoded_rows, conflicts, {
        "path": str(Path(str(decoded_video["path"])).expanduser().resolve()),
        "sha256": source_sha,
        "size": decoded_video["size"],
        "mtime_ns": decoded_video["mtime_ns"],
        "current_file_verified": False,
    }


def _expected_reason_codes(
    legacy: dict[str, Any], decoded: dict[str, Any]
) -> list[str]:
    return list(
        dict.fromkeys(
            [
                *legacy["reason_codes"],
                *decoded["reason_codes"],
                "LEGACY_FULL_ORACLE_RECONCILED_DIAGNOSTIC_ONLY",
                "NO_MEDIA_TIME_AUTHORITY",
            ]
        )
    )


def _expected_capabilities() -> dict[str, bool]:
    return {
        "source_media_identity_from_bound_evidence": True,
        "decoded_picture_order": True,
        "complete_checksum_sequence": True,
        "pts_conflict_structure": True,
        "media_time_authority": False,
        "audio_timeline": False,
        "av_sync": False,
        "edl_timing": False,
    }


def _expected_reconciliation(frame_count: int) -> dict[str, Any]:
    return {
        "scope": "full",
        "source_frame_domain": [0, frame_count],
        "frame_count": frame_count,
        "timeline_fields_compared": [
            "n",
            "pts",
            "duration",
            "pts_time",
            "duration_time",
        ],
        "media_decode_performed": False,
        "source_media_opened": False,
        "sort_or_deduplicate_performed": False,
        "all_business_durations_positive": True,
        "all_checksums_valid": True,
        "input_identity_rechecked_before_publish": True,
    }


def _load_bound_snapshot(value: Any, name: str) -> EvidenceSnapshot:
    record = _require_mapping(value, name)
    declared_path = record.get("path")
    if not isinstance(declared_path, str):
        raise LegacyOracleAdapterError(f"{name} path is missing")
    snapshot = _load_snapshot(Path(declared_path), name)
    _require_file_binding(record, name, snapshot)
    return snapshot


def verify_reconciliation_evidence(path: Path) -> dict[str, Any]:
    """Recompute an adapter report from all three immutable source artifacts."""
    report_snapshot = _load_snapshot(path, "reconciliation evidence")
    report = report_snapshot.value
    if (
        report.get("schema_version") != SCHEMA_VERSION
        or report.get("kind") != ADAPTER_KIND
        or report.get("status") != "BLOCKED"
        or report.get("evidence_role") != EVIDENCE_ROLE
        or report.get("authoritative_frame_timeline") is not False
        or report.get("time_authority") != "none"
        or report.get("gate_approval") is not False
        or report.get("production_consumer_allowed") is not False
        or report.get("exit_code") != EXIT_BLOCKED
        or not isinstance(report.get("created_utc"), str)
        or not report.get("created_utc")
    ):
        raise LegacyOracleAdapterError(
            "reconciliation evidence header or isolation policy is invalid"
        )
    bindings = _require_mapping(
        report.get("bindings"), "reconciliation evidence bindings"
    )
    legacy = _load_bound_snapshot(
        bindings.get("legacy_full_oracle"), "bound legacy full oracle"
    )
    decoded = _load_bound_snapshot(
        bindings.get("source_decode"), "bound source decode"
    )
    manifest = _load_bound_snapshot(
        bindings.get("generation_manifest"), "bound generation manifest"
    )
    rows, conflicts, source_media = _validate_inputs(legacy, decoded, manifest)
    expected_bindings = {
        "legacy_full_oracle": legacy.record(),
        "source_decode": decoded.record(),
        "generation_manifest": manifest.record(),
    }
    expected_fields: tuple[tuple[str, Any], ...] = (
        ("bindings", expected_bindings),
        ("source_media", source_media),
        ("reason_codes", _expected_reason_codes(legacy.value, decoded.value)),
        ("capabilities", _expected_capabilities()),
        ("reconciliation", _expected_reconciliation(len(rows))),
        ("pts_conflicts", conflicts),
        ("showinfo", decoded.value["showinfo"]),
        ("duration", decoded.value["duration"]),
        ("pts_table", rows),
    )
    for field, expected in expected_fields:
        if report.get(field) != expected:
            raise LegacyOracleAdapterError(
                f"reconciliation evidence {field} differs from recomputed inputs"
            )
    for snapshot in (report_snapshot, legacy, decoded, manifest):
        snapshot.assert_unchanged()
    return {
        "record": report_snapshot.record(),
        "report": report,
        "source_media": source_media,
        "frame_count": len(rows),
        "conflict_indices": conflicts["conflict_indices"],
        "bound_evidence": expected_bindings,
        "time_authority": "none",
        "gate_approval": False,
    }


def _write_json_new_atomic(
    output_path: Path,
    value: dict[str, Any],
    *,
    before_publish: Callable[[], None],
) -> None:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    temporary = output_path.with_name(
        f".{output_path.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        before_publish()
        try:
            os.link(temporary, output_path)
        except FileExistsError as exc:
            raise FileExistsError(
                f"reconciliation evidence is write-once: {output_path}"
            ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def adapt_legacy_full_oracle(
    legacy_oracle_path: Path,
    source_decode_path: Path,
    generation_manifest_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    """Publish isolated, non-authoritative reconciliation evidence once."""
    paths = [
        Path(path).expanduser().resolve()
        for path in (
            legacy_oracle_path,
            source_decode_path,
            generation_manifest_path,
            output_path,
        )
    ]
    legacy_oracle_path, source_decode_path, generation_manifest_path, output_path = paths
    if len(set(paths)) != len(paths):
        raise ValueError("input and output evidence paths must be distinct")
    if output_path.exists():
        raise FileExistsError(
            f"reconciliation evidence is write-once: {output_path}"
        )
    legacy = _load_snapshot(legacy_oracle_path, "legacy full oracle")
    decoded = _load_snapshot(source_decode_path, "source decode evidence")
    manifest = _load_snapshot(generation_manifest_path, "generation manifest")
    rows, conflicts, source_media = _validate_inputs(legacy, decoded, manifest)
    reason_codes = _expected_reason_codes(legacy.value, decoded.value)
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": ADAPTER_KIND,
        "created_utc": _utc_now(),
        "status": "BLOCKED",
        "evidence_role": EVIDENCE_ROLE,
        "authoritative_frame_timeline": False,
        "time_authority": "none",
        "gate_approval": False,
        "production_consumer_allowed": False,
        "reason_codes": reason_codes,
        "source_media": source_media,
        "bindings": {
            "legacy_full_oracle": legacy.record(),
            "source_decode": decoded.record(),
            "generation_manifest": manifest.record(),
        },
        "capabilities": _expected_capabilities(),
        "reconciliation": _expected_reconciliation(len(rows)),
        "pts_conflicts": conflicts,
        "showinfo": decoded.value["showinfo"],
        "duration": decoded.value["duration"],
        "pts_table": rows,
        "exit_code": EXIT_BLOCKED,
    }

    def assert_inputs_unchanged() -> None:
        for snapshot in (legacy, decoded, manifest):
            snapshot.assert_unchanged()

    _write_json_new_atomic(
        output_path,
        report,
        before_publish=assert_inputs_unchanged,
    )
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Bind existing full PTS/checksum evidence without decoding media; "
            "the result remains diagnostic and BLOCKED"
        )
    )
    parser.add_argument("legacy_oracle", type=Path)
    parser.add_argument("source_decode", type=Path)
    parser.add_argument("--generation-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = adapt_legacy_full_oracle(
            args.legacy_oracle,
            args.source_decode,
            args.generation_manifest,
            args.output,
        )
    except (
        LegacyOracleAdapterError,
        FileExistsError,
        FileNotFoundError,
        OSError,
        ValueError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAILED
    print(
        json.dumps(
            {
                "status": report["status"],
                "kind": report["kind"],
                "time_authority": report["time_authority"],
                "frame_count": report["reconciliation"]["frame_count"],
                "output": str(args.output.expanduser().resolve()),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return EXIT_BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())
