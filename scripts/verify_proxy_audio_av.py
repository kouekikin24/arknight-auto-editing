#!/usr/bin/env python3
"""Verify audio timelines and A/V sync for a normalized Phase 0 proxy.

The video proxy is deliberately generated without an audio stream.  This tool
keeps that fact explicit: it decodes the source and proxy audio streams with
FFmpeg's ``ashowinfo`` filter, checks sample-domain continuity, and refuses to
call a video-only proxy ready.  A future audio-carrying route can reuse the
same evidence format and must pass the exact audio and A/V checks first.
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
import tempfile
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import verify_mpv_frames as frame_oracle
import pts_normalized_proxy as proxy_verifier
import adapt_legacy_full_oracle as reconciliation_verifier


SCHEMA_VERSION = 2
RUN_KIND = "mpv_phase0_audio_av_run_manifest"
REPORT_KIND = "mpv_phase0_audio_av_report"
CONTENT_ANCHOR_SCHEMA_VERSION = 2
CONTENT_ANCHOR_MANIFEST_KIND = "mpv_phase0_content_anchor_manifest"
CONTENT_ANCHOR_OBSERVATION_KIND = "mpv_phase0_content_anchor_observations"
CONTENT_ANCHOR_WINDOW_KIND = "mpv_phase0_content_anchor_window"
# A source anchor is registered before a proxy exists.  Keep its kind
# deliberately separate from the paired source/proxy manifest so a proxy
# observation cannot be mistaken for the source of truth.
SOURCE_CONTENT_ANCHOR_SCHEMA_VERSION = 2
SOURCE_CONTENT_ANCHOR_MANIFEST_KIND = "mpv_phase0_source_content_anchor_manifest"
SOURCE_CONTENT_ANCHOR_OBSERVATION_KIND = "mpv_phase0_source_content_anchor_observations"
EVIDENCE_BUNDLE_SCHEMA_VERSION = 1
EVIDENCE_BUNDLE_KIND = "mpv_phase0_split_window_evidence_bundle"
PTS_CONFLICT_EVIDENCE_KIND = "mpv_phase0_pts_conflict_evidence"
RECONCILIATION_KIND = "mpv_phase0_legacy_full_oracle_reconciliation"
AV_EVENT_EVIDENCE_KIND = "mpv_phase0_av_event_evidence"
EVENT_SCAN_MANIFEST_KIND = "mpv_phase0_av_event_candidate_scan"
# A split-window bundle is deliberately a provenance precondition.  It is not
# an A/V Gate result and must not be consumed as one by later callers.
SPLIT_WINDOW_EVIDENCE_ROLE = "structural_precondition_only"
CONTENT_ANCHOR_CAPTURE_TOOL = SCRIPT_DIR / "capture_av_content_anchors.py"
EXIT_PASS = 0
EXIT_USAGE = 2
EXIT_BLOCKED = 10
EXIT_FAILED = 20
DEFAULT_MAX_AV_ANCHOR_ERROR_SECONDS = 0.010
DEFAULT_MAX_IDENTITY_OFFSET_SPAN_SECONDS = 0.010
CONTENT_ANCHOR_REEXTRACT_TIMEOUT_SECONDS = 300
REQUIRED_CONTENT_ANCHOR_ZONES = ("start", "pts_conflict", "middle", "end")
_AUDIO_PRESERVATION_REASON_CODES = frozenset(
    {
        "PROXY_AUDIO_MISSING",
        "UNEXPECTED_PROXY_AUDIO",
        "AUDIO_FORMAT_MISMATCH",
        "AUDIO_FRAME_COUNT_MISMATCH",
        "AUDIO_SAMPLE_COUNT_MISMATCH",
        "AUDIO_PCM_CHECKSUM_MISMATCH",
        "NO_AUDIO_POLICY_NOT_DECLARED",
    }
)
_MISSING_TOKENS = frozenset({"n/a", "na", "nopts", "av_nopts_value", "unknown"})
_AUDIO_FRAME_RE = re.compile(r"ashowinfo[^]]*\].*?\bn\s*:", re.IGNORECASE)
_AUDIO_STREAM_RE = re.compile(r"^\s*Stream\s+#.*\bAudio:\s*([^,\s]+)", re.IGNORECASE)
_MAP_MISSING_RE = re.compile(
    r"(?:matches no streams|failed to set value ['\"]?0:a:0|invalid argument)",
    re.IGNORECASE,
)


class AudioEvidenceError(ValueError):
    """An audio/A/V evidence file is malformed or not safely bound."""


class SourceAnchorEvidenceError(AudioEvidenceError):
    """A source-first anchor manifest cannot be used as gate evidence."""

    def __init__(self, reason_code: str, message: str) -> None:
        self.reason_code = reason_code
        super().__init__(message)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _file_record(path: Path, *, include_size: bool = False) -> dict[str, Any]:
    path = path.expanduser().resolve()
    record: dict[str, Any] = {"path": str(path), "sha256": sha256_file(path)}
    if include_size:
        record["size"] = path.stat().st_size
    return record


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AudioEvidenceError(f"cannot read JSON evidence {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AudioEvidenceError(f"JSON evidence must be an object: {path}")
    return value


def write_json_new(
    path: Path,
    value: Any,
    *,
    validate_temporary: Callable[[Path], None] | None = None,
    before_publish: Callable[[], None] | None = None,
) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if validate_temporary is not None:
            validate_temporary(temporary)
        if before_publish is not None:
            before_publish()
        os.link(temporary, path)
    except FileExistsError as exc:
        raise FileExistsError(
            f"evidence already exists; choose a new path: {path}"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _bundle_blocked(reason_code: str, *, error: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "BLOCKED",
        "evidence_role": SPLIT_WINDOW_EVIDENCE_ROLE,
        "ready_for_source_anchor": False,
        "time_authority": "none",
        "production_consumer_allowed": False,
        "gate_approval": False,
        "reason_codes": [reason_code],
        "conflict_evidence": None,
        "av_event_evidence": None,
    }
    if error:
        result["error"] = error
    return result


def _validate_source_media_record(
    record: dict[str, Any],
    source_media_path: Path,
    name: str,
) -> dict[str, Any]:
    bound_path = _validate_exact_file_binding(
        record, name, source_media_path, require_size=True
    )
    return _file_record(bound_path, include_size=True)


def _validate_bundle_tool_binding(bundle: dict[str, Any]) -> None:
    tools = _require_mapping(bundle.get("tools"), "split-window bundle tools")
    _validate_exact_file_binding(
        _require_mapping(tools.get("creator"), "split-window bundle creator"),
        "split-window bundle creator",
        Path(__file__),
    )


def _load_pts_conflict_evidence(
    evidence_path: Path,
    *,
    source_media_path: Path,
    source_oracle_path: Path,
) -> dict[str, Any]:
    evidence_path = evidence_path.expanduser().resolve()
    evidence = _load_json(evidence_path)
    if (
        evidence.get("schema_version") != EVIDENCE_BUNDLE_SCHEMA_VERSION
        or evidence.get("kind") != PTS_CONFLICT_EVIDENCE_KIND
        or evidence.get("status") != "BLOCKED"
        or evidence.get("evidence_role")
        != "decoded_picture_order_and_pts_conflict_only"
        or evidence.get("authoritative_frame_timeline") is not False
        or evidence.get("time_authority") != "none"
        or evidence.get("production_consumer_allowed") is not False
        or evidence.get("gate_approval") is not False
        or evidence.get("exit_code") != EXIT_BLOCKED
    ):
        raise AudioEvidenceError("PTS conflict evidence header or isolation policy is invalid")
    tools = _require_mapping(evidence.get("tools"), "PTS conflict evidence tools")
    _validate_exact_file_binding(
        _require_mapping(tools.get("creator"), "PTS conflict evidence creator"),
        "PTS conflict evidence creator",
        Path(__file__),
    )
    bindings = _require_mapping(
        evidence.get("bindings"), "PTS conflict evidence bindings"
    )
    source_record = _require_mapping(
        bindings.get("source_media"), "PTS conflict source media"
    )
    _validate_source_media_record(
        source_record, source_media_path, "PTS conflict source media"
    )
    source_oracle_record = _require_mapping(
        bindings.get("source_oracle"), "PTS conflict source oracle"
    )
    _validate_exact_file_binding(
        source_oracle_record,
        "PTS conflict source oracle",
        source_oracle_path,
    )
    oracle_record = _file_record(source_oracle_path)
    oracle_header = _load_json(source_oracle_path)
    if oracle_header.get("kind") == RECONCILIATION_KIND:
        reconciliation = _verify_reconciliation_conflict_source(
            source_oracle_path,
            source_media_path=source_media_path,
        )
        oracle = reconciliation["report"]
        expected_provenance = reconciliation["provenance"]
        if evidence.get("source_provenance") != expected_provenance:
            raise AudioEvidenceError(
                "PTS conflict reconciliation provenance is stale"
            )
    else:
        _, oracle = _load_bound_oracle(
            oracle_record,
            "PTS conflict source oracle",
            source_media_path,
            str(source_record.get("sha256")),
        )
        expected_provenance = {
            "kind": "frame_oracle",
            "time_authority": "none",
        }
        if evidence.get("source_provenance") != expected_provenance:
            raise AudioEvidenceError("PTS conflict oracle provenance is invalid")
    assessment = _require_mapping(
        oracle.get("pts_conflict_assessment", oracle.get("reconciliation")),
        "PTS conflict oracle assessment",
    )
    scan_scope = assessment.get("scan_scope", assessment.get("scope"))
    if scan_scope not in ("complete", "full"):
        raise AudioEvidenceError("PTS conflict evidence requires a complete source oracle scope")
    automatic_sort_allowed = assessment.get(
        "automatic_sort_or_deduplicate_allowed",
        False if oracle.get("kind") == RECONCILIATION_KIND else None,
    )
    if automatic_sort_allowed is not False:
        raise AudioEvidenceError("PTS conflict oracle does not explicitly forbid sort/deduplicate")
    rows = oracle.get("pts_table")
    if not isinstance(rows, list) or not rows:
        raise AudioEvidenceError("PTS conflict oracle has no frame table")
    if evidence.get("scan_scope") not in ("complete", "full"):
        raise AudioEvidenceError("PTS conflict evidence scan scope is not complete")
    if evidence.get("source_frame_domain") != [0, len(rows)]:
        raise AudioEvidenceError("PTS conflict evidence source frame domain is not complete")
    actual_conflicts = _pts_conflict_indices(rows)
    frames = evidence.get("frames")
    if not isinstance(frames, list) or not frames:
        raise AudioEvidenceError("PTS conflict evidence has no frames")
    normalized_frames = [
        {
            "source_frame_index": index,
            "pts": rows[index].get("pts"),
            "checksum": str(rows[index].get("checksum", "")).upper(),
        }
        for index in sorted(actual_conflicts)
    ]
    if any(not frame["checksum"] for frame in normalized_frames):
        raise AudioEvidenceError("PTS conflict frame checksum is missing")
    if frames != normalized_frames:
        raise AudioEvidenceError("PTS conflict frame sequence is stale")
    if evidence.get("conflict_indices") != sorted(actual_conflicts):
        raise AudioEvidenceError("PTS conflict index summary is stale")
    return {
        "record": _file_record(evidence_path),
        "source_media": _file_record(source_media_path, include_size=True),
        "source_oracle": oracle_record,
        "conflict_indices": sorted(actual_conflicts),
        "frames": normalized_frames,
        "source_provenance": expected_provenance,
    }


def _verify_reconciliation_conflict_source(
    reconciliation_path: Path,
    *,
    source_media_path: Path,
) -> dict[str, Any]:
    reconciliation_path = reconciliation_path.expanduser().resolve()
    try:
        verified = reconciliation_verifier.verify_reconciliation_evidence(
            reconciliation_path
        )
    except (
        reconciliation_verifier.LegacyOracleAdapterError,
        FileNotFoundError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        raise AudioEvidenceError(
            f"PTS conflict reconciliation verification failed: {exc}"
        ) from exc
    source = _require_mapping(
        verified.get("source_media"), "PTS conflict reconciliation source media"
    )
    actual_source = _file_record(source_media_path, include_size=True)
    if (
        not _same_path(Path(str(source.get("path"))), source_media_path)
        or str(source.get("sha256", "")).lower()
        != str(actual_source["sha256"]).lower()
        or source.get("size") != actual_source["size"]
    ):
        raise AudioEvidenceError(
            "PTS conflict reconciliation is bound to another source media"
        )
    report = _require_mapping(
        verified.get("report"), "verified PTS conflict reconciliation report"
    )
    if (
        report.get("status") != "BLOCKED"
        or report.get("time_authority") != "none"
        or report.get("authoritative_frame_timeline") is not False
        or report.get("production_consumer_allowed") is not False
        or report.get("gate_approval") is not False
    ):
        raise AudioEvidenceError(
            "PTS conflict reconciliation isolation policy is invalid"
        )
    return {
        "report": report,
        "provenance": {
            "kind": "legacy_full_oracle_reconciliation",
            "reconciliation": verified["record"],
            "bound_evidence": verified["bound_evidence"],
            "time_authority": "none",
            "production_consumer_allowed": False,
            "gate_approval": False,
        },
    }


def _assert_record_unchanged(record: dict[str, Any], name: str) -> None:
    path = _validate_bound_file(record, name)
    if "size" in record and record.get("size") != path.stat().st_size:
        raise AudioEvidenceError(f"{name} size binding is stale")


def _assert_reconciliation_conflict_inputs_unchanged(
    source_media_path: Path,
    reconciliation_path: Path,
    expected: dict[str, Any],
) -> None:
    source_record = expected["source_media"]
    _validate_source_media_record(
        source_record, source_media_path, "PTS conflict source media"
    )
    _assert_record_unchanged(
        expected["provenance"]["reconciliation"],
        "PTS conflict reconciliation",
    )
    for name, record in expected["provenance"]["bound_evidence"].items():
        _assert_record_unchanged(record, f"PTS conflict reconciliation {name}")
    current = _verify_reconciliation_conflict_source(
        reconciliation_path,
        source_media_path=source_media_path,
    )
    if current["provenance"] != expected["provenance"]:
        raise AudioEvidenceError("PTS conflict reconciliation identity changed")


def create_pts_conflict_evidence_from_reconciliation(
    source_media_path: Path,
    reconciliation_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    """Publish write-once conflict evidence without granting media time authority."""
    source_media_path = source_media_path.expanduser().resolve()
    reconciliation_path = reconciliation_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    if output_path.exists():
        raise FileExistsError("PTS conflict evidence is write-once")
    verified = _verify_reconciliation_conflict_source(
        reconciliation_path,
        source_media_path=source_media_path,
    )
    report = verified["report"]
    rows = report.get("pts_table")
    if not isinstance(rows, list) or not rows:
        raise AudioEvidenceError("PTS conflict reconciliation has no frame table")
    conflict_indices = sorted(_pts_conflict_indices(rows))
    declared_conflicts = _require_mapping(
        report.get("pts_conflicts"), "PTS conflict reconciliation summary"
    ).get("conflict_indices")
    if declared_conflicts != conflict_indices or not conflict_indices:
        raise AudioEvidenceError("PTS conflict reconciliation summary is stale")
    frames = []
    for index in conflict_indices:
        row = _require_mapping(rows[index], f"PTS conflict frame {index}")
        checksum = row.get("checksum")
        if not isinstance(checksum, str) or not checksum.strip():
            raise AudioEvidenceError("PTS conflict frame checksum is missing")
        frames.append(
            {
                "source_frame_index": index,
                "pts": row.get("pts"),
                "checksum": checksum.upper(),
            }
        )
    evidence = {
        "schema_version": EVIDENCE_BUNDLE_SCHEMA_VERSION,
        "kind": PTS_CONFLICT_EVIDENCE_KIND,
        "created_utc": _utc_now(),
        "status": "BLOCKED",
        "evidence_role": "decoded_picture_order_and_pts_conflict_only",
        "authoritative_frame_timeline": False,
        "time_authority": "none",
        "production_consumer_allowed": False,
        "gate_approval": False,
        "exit_code": EXIT_BLOCKED,
        "bindings": {
            "source_media": _file_record(source_media_path, include_size=True),
            "source_oracle": _file_record(reconciliation_path),
        },
        "source_provenance": verified["provenance"],
        "scan_scope": "complete",
        "source_frame_domain": [0, len(rows)],
        "conflict_indices": conflict_indices,
        "frames": frames,
        "tools": {"creator": _file_record(Path(__file__))},
    }
    publish_snapshot = {
        "source_media": evidence["bindings"]["source_media"],
        "provenance": verified["provenance"],
    }
    write_json_new(
        output_path,
        evidence,
        validate_temporary=lambda temporary: _load_pts_conflict_evidence(
            temporary,
            source_media_path=source_media_path,
            source_oracle_path=reconciliation_path,
        ),
        before_publish=lambda: _assert_reconciliation_conflict_inputs_unchanged(
            source_media_path,
            reconciliation_path,
            publish_snapshot,
        )
        or _assert_record_unchanged(
            evidence["tools"]["creator"], "PTS conflict evidence creator"
        ),
    )
    return evidence


def _load_av_event_evidence(
    evidence_path: Path,
    *,
    source_media_path: Path,
    source_oracle_path: Path,
) -> dict[str, Any]:
    evidence_path = evidence_path.expanduser().resolve()
    evidence = _load_json(evidence_path)
    if (
        evidence.get("schema_version") != EVIDENCE_BUNDLE_SCHEMA_VERSION
        or evidence.get("kind") != AV_EVENT_EVIDENCE_KIND
    ):
        raise AudioEvidenceError("A/V event evidence kind or version is invalid")
    if evidence.get("status") != "OBSERVED":
        raise AudioEvidenceError("A/V event evidence is not an observed event")
    bindings = _require_mapping(evidence.get("bindings"), "A/V event bindings")
    source_record = _require_mapping(
        bindings.get("source_media"), "A/V event source media"
    )
    _validate_source_media_record(
        source_record, source_media_path, "A/V event source media"
    )
    _validate_exact_file_binding(
        _require_mapping(
            bindings.get("source_oracle"), "A/V event source oracle"
        ),
        "A/V event source oracle",
        source_oracle_path,
    )
    observation_record = _require_mapping(
        bindings.get("observation"), "A/V event observation"
    )
    observation_path = _validate_bound_file(
        observation_record, "A/V event observation"
    )
    observation = _load_json(observation_path)
    if observation.get("kind") == EVENT_SCAN_MANIFEST_KIND or observation.get(
        "status"
    ) == "CANDIDATES_FOUND":
        raise AudioEvidenceError(
            "scanner candidates cannot be used as source observations"
        )
    if observation.get("kind") != SOURCE_CONTENT_ANCHOR_OBSERVATION_KIND:
        raise AudioEvidenceError("A/V event observation kind is invalid")
    if observation.get("schema_version") != SOURCE_CONTENT_ANCHOR_SCHEMA_VERSION:
        raise AudioEvidenceError("A/V event observation schema version is invalid")
    _assert_source_only_payload(observation)
    _validate_exact_file_binding(
        _require_mapping(observation.get("observer"), "A/V event observer tool"),
        "A/V event observer tool",
        CONTENT_ANCHOR_CAPTURE_TOOL,
    )
    if not _nonempty_text(observation.get("method")):
        raise AudioEvidenceError("A/V event observation method is missing")
    audio_clock = _require_mapping(
        observation.get("audio_clock"), "A/V event observation audio clock"
    )
    sample_rate = audio_clock.get("source_sample_rate")
    if (
        audio_clock.get("source_stream", "0:a:0") != "0:a:0"
        or audio_clock.get("sample_index_basis")
        not in ("ashowinfo_pts", "decoded_ashowinfo_pts")
        or isinstance(sample_rate, bool)
        or not isinstance(sample_rate, int)
        or sample_rate <= 0
    ):
        raise AudioEvidenceError("A/V event observation audio clock is invalid")
    _, source_oracle = _load_bound_oracle(
        _file_record(source_oracle_path),
        "A/V event source oracle",
        source_media_path,
        str(source_record.get("sha256")),
    )
    source_rows = source_oracle.get("pts_table")
    if not isinstance(source_rows, list) or not source_rows:
        raise AudioEvidenceError("A/V event source oracle has no frame table")
    if observation.get("source_frame_domain") != [0, len(source_rows)]:
        raise AudioEvidenceError("A/V event observation must bind the complete source frame domain")
    anchors = observation.get("anchors")
    if not isinstance(anchors, list) or not anchors:
        raise AudioEvidenceError("A/V event observation must contain at least one observed anchor")
    for position, anchor in enumerate(anchors):
        if not isinstance(anchor, dict):
            raise AudioEvidenceError(f"A/V event anchor {position} is invalid")
        try:
            _source_event_validation(anchor, position)
        except SourceAnchorEvidenceError as exc:
            raise AudioEvidenceError(str(exc)) from exc
        source_index = anchor.get("source_frame_index")
        if (
            isinstance(source_index, bool)
            or not isinstance(source_index, int)
            or source_index < 0
            or source_index >= len(source_rows)
        ):
            raise AudioEvidenceError(f"A/V event anchor {position} is outside the source oracle")
        checksum = str(source_rows[source_index].get("checksum", "")).upper()
        if not checksum or str(anchor.get("source_frame_checksum", "")).upper() != checksum:
            raise AudioEvidenceError(f"A/V event anchor {position} checksum is stale")
        if not _nonempty_text(anchor.get("evidence_id")):
            raise AudioEvidenceError(f"A/V event anchor {position} evidence id is missing")
        sample = anchor.get("source_audio_sample")
        if isinstance(sample, bool) or not isinstance(sample, int) or sample < 0:
            raise AudioEvidenceError(f"A/V event anchor {position} audio sample is invalid")
    event = _require_mapping(evidence.get("event"), "A/V event")
    if (
        event.get("observed") is not True
        or event.get("requires_human_observation") is not False
        or not _nonempty_text(event.get("description"))
    ):
        raise AudioEvidenceError("A/V event lacks independent observation")
    video_event = _require_mapping(event.get("video"), "A/V video event")
    audio_event = _require_mapping(event.get("audio"), "A/V audio event")
    if (
        video_event.get("observed") is not True
        or audio_event.get("observed") is not True
        or not _nonempty_text(video_event.get("description"))
        or not _nonempty_text(audio_event.get("description"))
    ):
        raise AudioEvidenceError("A/V event must contain observed video and audio")
    window = _require_mapping(evidence.get("window"), "A/V event window")
    start = window.get("requested_start_seconds")
    duration = window.get("requested_duration_seconds")
    if (
        isinstance(start, bool)
        or not isinstance(start, (int, float))
        or not math.isfinite(float(start))
        or float(start) < 0.0
        or isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(float(duration))
        or float(duration) <= 0.0
        or float(duration) > 10.0
    ):
        raise AudioEvidenceError("A/V event window is not bounded to 10 seconds")
    return {
        "record": _file_record(evidence_path),
        "source_media": _file_record(source_media_path, include_size=True),
        "source_oracle": _file_record(source_oracle_path),
        "observation": _file_record(observation_path),
        "window": {
            "requested_start_seconds": float(start),
            "requested_duration_seconds": float(duration),
        },
        "event": event,
    }


def evaluate_split_window_evidence_bundle(
    bundle_path: Path | None,
    *,
    source_media_path: Path,
    av_event_source_oracle_path: Path,
    pts_conflict_source_path: Path | None = None,
) -> dict[str, Any]:
    """Verify separate PTS-conflict and real A/V event evidence as one bundle."""
    if bundle_path is None:
        return _bundle_blocked("AV_EVIDENCE_BUNDLE_NOT_PROVIDED")
    bundle_path = bundle_path.expanduser().resolve()
    try:
        bundle = _load_json(bundle_path)
        if (
            bundle.get("schema_version") != EVIDENCE_BUNDLE_SCHEMA_VERSION
            or bundle.get("kind") != EVIDENCE_BUNDLE_KIND
            or bundle.get("evidence_role") != SPLIT_WINDOW_EVIDENCE_ROLE
            or bundle.get("ready_for_source_anchor") is not False
            or bundle.get("time_authority") != "none"
            or bundle.get("production_consumer_allowed") is not False
            or bundle.get("gate_approval") is not False
        ):
            raise AudioEvidenceError("split-window evidence bundle isolation policy is invalid")
        expected_separation = {
            "pts_conflict_evidence": "decoded_picture_order_and_pts_conflict",
            "av_event_evidence": "independently_observed_audio_visual_content",
            "same_window_required": False,
            "evidence_role": SPLIT_WINDOW_EVIDENCE_ROLE,
            "ready_for_source_anchor": False,
        }
        if bundle.get("separation") != expected_separation:
            raise AudioEvidenceError("split-window evidence separation contract is invalid")
        _validate_bundle_tool_binding(bundle)
        bindings = _require_mapping(
            bundle.get("bindings"), "split-window evidence bundle bindings"
        )
        _validate_source_media_record(
            _require_mapping(bindings.get("source_media"), "bundle source media"),
            source_media_path,
            "bundle source media",
        )
        conflict_source_record = _require_mapping(
            bindings.get("pts_conflict_source"), "bundle PTS conflict source"
        )
        event_oracle_record = _require_mapping(
            bindings.get("av_event_source_oracle"), "bundle A/V event source oracle"
        )
        bound_conflict_source = _validate_bound_file(
            conflict_source_record, "bundle PTS conflict source"
        )
        bound_event_oracle = _validate_exact_file_binding(
            event_oracle_record,
            "bundle A/V event source oracle",
            av_event_source_oracle_path,
        )
        if pts_conflict_source_path is not None:
            _validate_exact_file_binding(
                conflict_source_record,
                "bundle PTS conflict source",
                pts_conflict_source_path,
            )
        conflict_path = _validate_bound_file(
            _require_mapping(
                bindings.get("pts_conflict_evidence"),
                "bundle PTS conflict evidence",
            ),
            "bundle PTS conflict evidence",
        )
        event_path = _validate_bound_file(
            _require_mapping(
                bindings.get("av_event_evidence"), "bundle A/V event evidence"
            ),
            "bundle A/V event evidence",
        )
        conflict = _load_pts_conflict_evidence(
            conflict_path,
            source_media_path=source_media_path,
            source_oracle_path=bound_conflict_source,
        )
        event = _load_av_event_evidence(
            event_path,
            source_media_path=source_media_path,
            source_oracle_path=bound_event_oracle,
        )
        return {
            "status": "PASS",
            "evidence_role": SPLIT_WINDOW_EVIDENCE_ROLE,
            "ready_for_source_anchor": False,
            "time_authority": "none",
            "production_consumer_allowed": False,
            "gate_approval": False,
            "reason_codes": [],
            "bundle": _file_record(bundle_path),
            "source_media": _file_record(source_media_path, include_size=True),
            "pts_conflict_source": _file_record(bound_conflict_source),
            "av_event_source_oracle": _file_record(bound_event_oracle),
            "conflict_evidence": conflict,
            "av_event_evidence": event,
        }
    except (AudioEvidenceError, KeyError, OSError, TypeError, ValueError) as exc:
        return _bundle_blocked("AV_EVIDENCE_BUNDLE_INVALID", error=str(exc))


def create_split_window_evidence_bundle(
    source_media_path: Path,
    av_event_source_oracle_path: Path,
    pts_conflict_evidence_path: Path,
    av_event_evidence_path: Path,
    output_path: Path,
    *,
    pts_conflict_source_path: Path | None = None,
) -> dict[str, Any]:
    """Bind two already-created evidence manifests without weakening either."""
    source_media_path = source_media_path.expanduser().resolve()
    av_event_source_oracle_path = av_event_source_oracle_path.expanduser().resolve()
    pts_conflict_evidence_path = pts_conflict_evidence_path.expanduser().resolve()
    av_event_evidence_path = av_event_evidence_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    if output_path.exists():
        raise FileExistsError("split-window evidence bundle is write-once")
    if pts_conflict_source_path is None:
        conflict_payload = _load_json(pts_conflict_evidence_path)
        conflict_binding = _require_mapping(
            conflict_payload.get("bindings"), "PTS conflict evidence bindings"
        )
        pts_conflict_source_path = _validate_bound_file(
            _require_mapping(
                conflict_binding.get("source_oracle"),
                "PTS conflict source oracle",
            ),
            "PTS conflict source oracle",
        )
    else:
        pts_conflict_source_path = pts_conflict_source_path.expanduser().resolve()
    conflict = _load_pts_conflict_evidence(
        pts_conflict_evidence_path,
        source_media_path=source_media_path,
        source_oracle_path=pts_conflict_source_path,
    )
    event = _load_av_event_evidence(
        av_event_evidence_path,
        source_media_path=source_media_path,
        source_oracle_path=av_event_source_oracle_path,
    )
    manifest = {
        "schema_version": EVIDENCE_BUNDLE_SCHEMA_VERSION,
        "kind": EVIDENCE_BUNDLE_KIND,
        "created_utc": _utc_now(),
        "evidence_role": SPLIT_WINDOW_EVIDENCE_ROLE,
        "ready_for_source_anchor": False,
        "time_authority": "none",
        "production_consumer_allowed": False,
        "gate_approval": False,
        "bindings": {
            "source_media": _file_record(source_media_path, include_size=True),
            "pts_conflict_source": _file_record(pts_conflict_source_path),
            "av_event_source_oracle": _file_record(av_event_source_oracle_path),
            "pts_conflict_evidence": conflict["record"],
            "av_event_evidence": event["record"],
        },
        "separation": {
            "pts_conflict_evidence": "decoded_picture_order_and_pts_conflict",
            "av_event_evidence": "independently_observed_audio_visual_content",
            "same_window_required": False,
            "evidence_role": SPLIT_WINDOW_EVIDENCE_ROLE,
            "ready_for_source_anchor": False,
        },
        "tools": {"creator": _file_record(Path(__file__))},
    }
    def validate_bundle(path: Path) -> None:
        result = evaluate_split_window_evidence_bundle(
            path,
            source_media_path=source_media_path,
            av_event_source_oracle_path=av_event_source_oracle_path,
            pts_conflict_source_path=pts_conflict_source_path,
        )
        if result.get("status") != "PASS":
            raise AudioEvidenceError(
                "split-window bundle validation failed: "
                + str(result.get("error") or result.get("reason_codes"))
            )

    input_records = {
        "source_media": _file_record(source_media_path, include_size=True),
        "pts_conflict_source": _file_record(pts_conflict_source_path),
        "av_event_source_oracle": _file_record(av_event_source_oracle_path),
        "pts_conflict_evidence": conflict["record"],
        "av_event_evidence": event["record"],
        "creator": manifest["tools"]["creator"],
    }

    def recheck_inputs() -> None:
        for name, record in input_records.items():
            _assert_record_unchanged(record, f"split-window {name}")
        _load_pts_conflict_evidence(
            pts_conflict_evidence_path,
            source_media_path=source_media_path,
            source_oracle_path=pts_conflict_source_path,
        )
        _load_av_event_evidence(
            av_event_evidence_path,
            source_media_path=source_media_path,
            source_oracle_path=av_event_source_oracle_path,
        )

    write_json_new(
        output_path,
        manifest,
        validate_temporary=validate_bundle,
        before_publish=recheck_inputs,
    )
    return manifest


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AudioEvidenceError(f"{name} must be an object")
    return value


def _require_int(value: Any, name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AudioEvidenceError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise AudioEvidenceError(f"{name} must be at least {minimum}")
    return value


def _same_path(left: Path, right: Path) -> bool:
    return left.expanduser().resolve().as_posix().lower() == right.expanduser().resolve().as_posix().lower()


def _token(line: str, name: str) -> str | None:
    match = re.search(rf"(?:^|\s){re.escape(name)}\s*:\s*([^\s]+)", line)
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


def _valid_positive_threshold(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) > 0.0
    )


def _valid_content_anchor_threshold(value: Any) -> bool:
    return _valid_positive_threshold(value) and float(value) <= float(
        DEFAULT_MAX_AV_ANCHOR_ERROR_SECONDS
    )


def _threshold_reason_codes(
    max_anchor_error_seconds: Any,
    max_identity_offset_span_seconds: Any,
) -> list[str]:
    reasons: list[str] = []
    if not _valid_content_anchor_threshold(max_anchor_error_seconds):
        reasons.append("MAX_AV_ANCHOR_THRESHOLD_INVALID")
    if not _valid_positive_threshold(max_identity_offset_span_seconds):
        reasons.append("MAX_IDENTITY_OFFSET_THRESHOLD_INVALID")
    return reasons


class AudioAnalyzer:
    """Parse FFmpeg ashowinfo lines without retaining unbounded stderr."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.malformed_lines: list[str] = []
        self.diagnostic_tail: deque[str] = deque(maxlen=200)
        self.audio_stream_declared = False
        self.audio_codec: str | None = None

    def feed(self, line: str) -> None:
        stripped = line.strip()
        if stripped:
            self.diagnostic_tail.append(stripped)
        stream_match = _AUDIO_STREAM_RE.search(line)
        if stream_match:
            self.audio_stream_declared = True
            if self.audio_codec is None:
                self.audio_codec = stream_match.group(1)
        if not _AUDIO_FRAME_RE.search(line):
            return
        values: dict[str, Any] = {
            "n": _parse_int(_token(line, "n")),
            "pts": _parse_int(_token(line, "pts")),
            "pts_time": _parse_float(_token(line, "pts_time")),
            "nb_samples": _parse_int(_token(line, "nb_samples")),
            "rate": _parse_int(_token(line, "rate")),
            "channels": _parse_int(_token(line, "channels")),
            "chlayout": _token(line, "chlayout"),
            "checksum": _token(line, "checksum"),
        }
        required = ("n", "pts", "pts_time", "nb_samples", "rate", "channels", "chlayout")
        if any(values[key] is None for key in required):
            if len(self.malformed_lines) < 20:
                self.malformed_lines.append(stripped)
            return
        self.audio_stream_declared = True
        values["checksum"] = (values["checksum"] or "").upper()
        self.rows.append(values)

    def finish(self, *, returncode: int) -> dict[str, Any]:
        reasons: list[str] = []
        rows = self.rows
        missing_map = any(_MAP_MISSING_RE.search(line) for line in self.diagnostic_tail)
        stream_present = self.audio_stream_declared or bool(rows)
        if not rows:
            reasons.append("AUDIO_STREAM_MISSING" if missing_map or not stream_present else "AUDIO_DECODE_EMPTY")
        if self.malformed_lines:
            reasons.append("AUDIO_FRAME_MALFORMED")
        if any(not row.get("checksum") for row in rows):
            reasons.append("AUDIO_CHECKSUM_MISSING")
        if returncode != 0 and "AUDIO_STREAM_MISSING" not in reasons:
            reasons.append("AUDIO_DECODE_FAILED")

        continuity_errors: list[dict[str, Any]] = []
        format_errors: list[dict[str, Any]] = []
        expected_format: tuple[int, int, str] | None = None
        total_samples = 0
        for index, row in enumerate(rows):
            if row["n"] != index:
                continuity_errors.append({"kind": "frame_index", "expected": index, "actual": row["n"]})
            if row["nb_samples"] <= 0:
                continuity_errors.append({"kind": "non_positive_samples", "n": row["n"], "value": row["nb_samples"]})
            current_format = (row["rate"], row["channels"], row["chlayout"])
            if expected_format is None:
                expected_format = current_format
            elif current_format != expected_format:
                format_errors.append({"n": row["n"], "expected": expected_format, "actual": current_format})
            if index:
                previous = rows[index - 1]
                expected_pts = previous["pts"] + previous["nb_samples"]
                if row["pts"] != expected_pts:
                    continuity_errors.append(
                        {
                            "kind": "pts_gap_or_overlap",
                            "n": row["n"],
                            "expected_pts": expected_pts,
                            "actual_pts": row["pts"],
                            "delta_samples": row["pts"] - expected_pts,
                        }
                    )
            expected_time = row["pts"] / row["rate"]
            if not math.isclose(row["pts_time"], expected_time, rel_tol=5e-6, abs_tol=5e-7):
                continuity_errors.append(
                    {
                        "kind": "pts_time_mismatch",
                        "n": row["n"],
                        "expected": expected_time,
                        "actual": row["pts_time"],
                    }
                )
            total_samples += max(row["nb_samples"], 0)
        if continuity_errors:
            reasons.append("AUDIO_TIMELINE_NON_CONTIGUOUS")
        if format_errors:
            reasons.append("AUDIO_FORMAT_CHANGED")
        reasons = list(dict.fromkeys(reasons))

        first = rows[0] if rows else None
        last = rows[-1] if rows else None
        end_pts = (last["pts"] + last["nb_samples"]) if last else None
        sample_rate = first["rate"] if first else None
        return {
            "status": "PASS" if returncode == 0 and rows and not reasons else "BLOCKED",
            "reason_codes": reasons,
            "stream_present": stream_present,
            "codec": self.audio_codec,
            "ffmpeg_returncode": returncode,
            "frames": {
                "count": len(rows),
                "pts_table": rows,
                "first": first,
                "last": last,
                "total_samples": total_samples,
                "start_pts": first["pts"] if first else None,
                "end_pts_exclusive": end_pts,
                "start_time": first["pts_time"] if first else None,
                "end_time": (end_pts / sample_rate) if end_pts is not None and sample_rate else None,
            },
            "format": {
                "sample_rate": expected_format[0] if expected_format else None,
                "channels": expected_format[1] if expected_format else None,
                "channel_layout": expected_format[2] if expected_format else None,
            },
            "continuity": {
                "status": "PASS" if rows and not continuity_errors else "BLOCKED",
                "error_count": len(continuity_errors),
                "examples": continuity_errors[:20],
            },
            "format_validation": {
                "status": "PASS" if rows and not format_errors else "BLOCKED",
                "error_count": len(format_errors),
                "examples": format_errors[:20],
            },
            "malformed_lines": self.malformed_lines,
            "diagnostic_tail": list(self.diagnostic_tail),
        }


def audio_command(ffmpeg: Path, media: Path) -> list[str]:
    return [
        str(ffmpeg),
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "info",
        "-copyts",
        "-i",
        str(media),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-af",
        "ashowinfo",
        "-f",
        "null",
        "-",
    ]


def content_anchor_video_capture_command(
    ffmpeg: Path,
    media: Path,
    frame_index: int,
    output: Path,
) -> list[str]:
    return [
        str(ffmpeg.expanduser().resolve()),
        "-n",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        str(media.expanduser().resolve()),
        "-vf",
        f"select=eq(n\\,{frame_index})",
        "-vsync",
        "0",
        "-frames:v",
        "1",
        str(output.expanduser().resolve()),
    ]


def content_anchor_audio_capture_command(
    ffmpeg: Path,
    media: Path,
    *,
    decoded_start_sample: int,
    decoded_end_sample_exclusive: int,
    output: Path,
) -> list[str]:
    return [
        str(ffmpeg.expanduser().resolve()),
        "-n",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        str(media.expanduser().resolve()),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-af",
        (
            f"atrim=start_sample={decoded_start_sample}:"
            f"end_sample={decoded_end_sample_exclusive},asetpts=N/SR/TB"
        ),
        "-c:a",
        "pcm_s16le",
        str(output.expanduser().resolve()),
    ]


def decode_audio(media: Path, ffmpeg: Path) -> dict[str, Any]:
    command = audio_command(ffmpeg, media)
    analyzer = AudioAnalyzer()
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
            analyzer.feed(line)
    finally:
        process.stderr.close()
    returncode = process.wait()
    result = analyzer.finish(returncode=returncode)
    result["media"] = {"path": str(media), "sha256": sha256_file(media), "size": media.stat().st_size}
    result["command"] = command
    return result


def _validate_bound_file(record: dict[str, Any], name: str) -> Path:
    path_value = record.get("path")
    sha_value = record.get("sha256")
    if not isinstance(path_value, str) or not isinstance(sha_value, str):
        raise AudioEvidenceError(f"{name} path/sha256 binding is invalid")
    path = Path(path_value).expanduser().resolve()
    if not path.is_file():
        raise AudioEvidenceError(f"{name} file is missing: {path}")
    if sha256_file(path).lower() != sha_value.lower():
        raise AudioEvidenceError(f"{name} SHA-256 does not match the bound evidence")
    return path


def _validate_exact_file_binding(
    record: dict[str, Any],
    name: str,
    expected_path: Path,
    *,
    require_size: bool = False,
) -> Path:
    path = _validate_bound_file(record, name)
    expected_path = expected_path.expanduser().resolve()
    if not _same_path(path, expected_path):
        raise AudioEvidenceError(f"{name} is bound to another path")
    if require_size and record.get("size") != expected_path.stat().st_size:
        raise AudioEvidenceError(f"{name} size binding is stale")
    return path


def _stream_summary(audio: dict[str, Any]) -> dict[str, Any]:
    frames = dict(audio.get("frames") or {})
    frames.pop("pts_table", None)
    return {
        "status": audio.get("status"),
        "reason_codes": audio.get("reason_codes", []),
        "stream_present": audio.get("stream_present"),
        "codec": audio.get("codec"),
        "frames": frames,
        "format": audio.get("format"),
        "continuity": audio.get("continuity"),
        "format_validation": audio.get("format_validation"),
    }


def _canonical_audio_evidence(audio: dict[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for value in (audio.get("frames") or {}).get("pts_table", []):
        if not isinstance(value, dict):
            rows.append({"invalid": repr(value)})
            continue
        rows.append(
            {
                field: value.get(field)
                for field in (
                    "n",
                    "pts",
                    "pts_time",
                    "nb_samples",
                    "rate",
                    "channels",
                    "chlayout",
                    "checksum",
                )
            }
        )
    return {
        "status": audio.get("status"),
        "reason_codes": audio.get("reason_codes", []),
        "stream_present": audio.get("stream_present"),
        "codec": audio.get("codec"),
        "format": audio.get("format"),
        "frames": {
            "count": (audio.get("frames") or {}).get("count"),
            "total_samples": (audio.get("frames") or {}).get("total_samples"),
            "start_pts": (audio.get("frames") or {}).get("start_pts"),
            "end_pts_exclusive": (audio.get("frames") or {}).get(
                "end_pts_exclusive"
            ),
            "pts_table": rows,
        },
        "continuity": audio.get("continuity"),
        "format_validation": audio.get("format_validation"),
    }


def audio_evidence_binding(audio: dict[str, Any]) -> dict[str, Any]:
    evidence = _canonical_audio_evidence(audio)
    frames = evidence["frames"]
    return {
        "sha256": sha256_json(evidence),
        "stream_present": evidence["stream_present"],
        "sample_rate": (evidence.get("format") or {}).get("sample_rate"),
        "frame_count": frames.get("count"),
        "total_samples": frames.get("total_samples"),
        "start_pts": frames.get("start_pts"),
        "end_pts_exclusive": frames.get("end_pts_exclusive"),
    }


def evaluate_audio_preservation(
    source_audio: dict[str, Any],
    proxy_audio: dict[str, Any],
    *,
    allow_no_audio: bool = False,
) -> dict[str, Any]:
    reasons: list[str] = []
    source_present = bool(source_audio.get("stream_present"))
    proxy_present = bool(proxy_audio.get("stream_present"))
    no_audio_pair = not source_present and not proxy_present
    if source_audio.get("status") != "PASS" and not (no_audio_pair and allow_no_audio):
        reasons.extend(
            f"SOURCE_{reason}" for reason in source_audio.get("reason_codes", [])
        )
    if proxy_audio.get("status") != "PASS" and not (no_audio_pair and allow_no_audio):
        reasons.extend(
            f"PROXY_{reason}" for reason in proxy_audio.get("reason_codes", [])
        )
    if source_present and not proxy_present:
        reasons.append("PROXY_AUDIO_MISSING")
    elif not source_present and proxy_present:
        reasons.append("UNEXPECTED_PROXY_AUDIO")
    elif source_present and proxy_present:
        source_format = source_audio.get("format") or {}
        proxy_format = proxy_audio.get("format") or {}
        if source_format != proxy_format:
            reasons.append("AUDIO_FORMAT_MISMATCH")
        source_frames = source_audio.get("frames") or {}
        proxy_frames = proxy_audio.get("frames") or {}
        if source_frames.get("count") != proxy_frames.get("count"):
            reasons.append("AUDIO_FRAME_COUNT_MISMATCH")
        if source_frames.get("total_samples") != proxy_frames.get("total_samples"):
            reasons.append("AUDIO_SAMPLE_COUNT_MISMATCH")
        source_rows = source_frames.get("pts_table") or []
        proxy_rows = proxy_frames.get("pts_table") or []
        source_checksums = [row.get("checksum") for row in source_rows]
        proxy_checksums = [row.get("checksum") for row in proxy_rows]
        if source_checksums != proxy_checksums:
            reasons.append("AUDIO_PCM_CHECKSUM_MISMATCH")
        timing_fields = ("pts", "nb_samples", "rate", "channels", "chlayout")
        source_timing = [
            {field: row.get(field) for field in timing_fields} for row in source_rows
        ]
        proxy_timing = [
            {field: row.get(field) for field in timing_fields} for row in proxy_rows
        ]
        if source_timing != proxy_timing:
            reasons.append("AUDIO_FRAME_TIMELINE_MISMATCH")
    elif no_audio_pair and not allow_no_audio:
        reasons.append("NO_AUDIO_POLICY_NOT_DECLARED")
    reasons = list(dict.fromkeys(reasons))
    if no_audio_pair and allow_no_audio and not reasons:
        status = "NOT_APPLICABLE_PASS"
    else:
        status = "PASS" if not reasons else "BLOCKED"
    return {
        "status": status,
        "reason_codes": reasons,
        "source": _stream_summary(source_audio),
        "proxy": _stream_summary(proxy_audio),
        "mapping": {
            "kind": "identity_full",
            "exact_pcm_and_sample_timeline_required": True,
        },
    }


def _load_bound_oracle(
    record: dict[str, Any],
    name: str,
    media_path: Path,
    expected_sha256: str,
) -> tuple[Path, dict[str, Any]]:
    oracle_path = _validate_bound_file(record, name)
    report = _load_json(oracle_path)
    if (
        report.get("schema_version") != frame_oracle.SCHEMA_VERSION
        or report.get("kind") != "mpv_phase0_frame_pts_oracle"
    ):
        raise AudioEvidenceError(f"{name} schema or kind is invalid")
    ffmpeg = _require_mapping(report.get("ffmpeg"), f"{name} ffmpeg result")
    if ffmpeg.get("returncode") != 0:
        raise AudioEvidenceError(f"{name} FFmpeg decode did not complete successfully")
    video = _require_mapping(report.get("video"), f"{name} video")
    declared_path = video.get("path")
    if not isinstance(declared_path, str) or not _same_path(
        Path(declared_path), media_path
    ):
        raise AudioEvidenceError(f"{name} is bound to another media path")
    declared_sha = video.get("sha256")
    if (
        not isinstance(declared_sha, str)
        or declared_sha.lower() != expected_sha256.lower()
        or declared_sha.lower() != sha256_file(media_path).lower()
    ):
        raise AudioEvidenceError(f"{name} does not bind the current media SHA-256")
    if video.get("size") != media_path.stat().st_size:
        raise AudioEvidenceError(f"{name} media size binding is stale")
    rows = report.get("pts_table")
    if not isinstance(rows, list) or not rows:
        raise AudioEvidenceError(f"{name} has no frame table")
    for index, value in enumerate(rows):
        row = _require_mapping(value, f"{name} pts_table[{index}]")
        if row.get("n") != index:
            raise AudioEvidenceError(f"{name} frame indices are not contiguous")
        if not isinstance(row.get("pts"), int) or isinstance(row.get("pts"), bool):
            raise AudioEvidenceError(f"{name} frame {index} PTS is missing")
        checksum = row.get("checksum")
        if not isinstance(checksum, str) or not checksum.strip():
            raise AudioEvidenceError(f"{name} frame {index} checksum is missing")
    return oracle_path, report


def _single_pixel_format(report: dict[str, Any], name: str) -> str:
    showinfo = _require_mapping(report.get("showinfo"), f"{name} showinfo")
    formats = showinfo.get("pixel_formats")
    if (
        not isinstance(formats, list)
        or len(formats) != 1
        or not isinstance(formats[0], str)
        or not formats[0]
    ):
        raise AudioEvidenceError(
            f"{name} must declare exactly one decoded pixel format"
        )
    return formats[0]


def _validate_video_evidence(
    video_manifest: dict[str, Any], video_report: dict[str, Any]
) -> dict[str, Any]:
    """Re-check the video proof instead of trusting its claimed PASS status."""
    try:
        if (
            video_manifest.get("schema_version") != proxy_verifier.SCHEMA_VERSION
            or video_manifest.get("kind") != proxy_verifier.MANIFEST_KIND
        ):
            raise AudioEvidenceError("video proxy manifest schema or kind is invalid")
        if (
            video_report.get("schema_version") != proxy_verifier.SCHEMA_VERSION
            or video_report.get("kind") != proxy_verifier.REPORT_KIND
        ):
            raise AudioEvidenceError("video proxy report schema or kind is invalid")
        source = _require_mapping(video_manifest.get("source"), "video manifest source")
        proxy = _require_mapping(video_manifest.get("proxy"), "video manifest proxy")
        source_path = _validate_bound_file(source, "source video")
        proxy_path = _validate_bound_file(proxy, "proxy video")
        source_sha256 = str(source.get("sha256"))
        proxy_sha256 = str(proxy.get("sha256"))
        source_oracle_record = _require_mapping(source.get("oracle"), "source video oracle")
        source_oracle_path, source_oracle = _load_bound_oracle(
            source_oracle_record,
            "source video oracle",
            source_path,
            source_sha256,
        )
        decoded_record = _require_mapping(
            video_report.get("decoded_proxy_oracle"),
            "decoded proxy oracle",
        )
        decoded_oracle_path, decoded_oracle = _load_bound_oracle(
            decoded_record,
            "decoded proxy oracle",
            proxy_path,
            proxy_sha256,
        )

        claimed = _require_mapping(
            video_report.get("video_validation"), "video report video_validation"
        )
        claimed_reasons: list[str] = []
        if claimed.get("status") != "PASS":
            claimed_reasons.append("VIDEO_VALIDATION_NOT_PASS")
        if claimed.get("authoritative_for_full_source") is not True:
            claimed_reasons.append("VIDEO_VALIDATION_NOT_AUTHORITATIVE")
        if claimed.get("scope") != "full":
            claimed_reasons.append("VIDEO_VALIDATION_SCOPE_NOT_FULL")
        source_scope = source.get("scope")
        if source_scope != "full" or video_report.get("scope") != source_scope:
            claimed_reasons.append("VIDEO_SCOPE_NOT_FULL")

        count = _require_int(
            source.get("business_frame_count"),
            "video manifest business_frame_count",
            minimum=1,
        )
        if claimed.get("business_frame_count") != count:
            claimed_reasons.append("VIDEO_BUSINESS_FRAME_COUNT_MISMATCH")
        if claimed.get("business_frame_domain") != [0, count]:
            claimed_reasons.append("VIDEO_BUSINESS_FRAME_DOMAIN_INVALID")
        for field in (
            "checksum_alignment",
            "pts_alignment",
            "positive_business_durations",
        ):
            section = _require_mapping(claimed.get(field), f"video validation {field}")
            if section.get("status") != "PASS" or section.get("mismatch_count") not in (0, None):
                claimed_reasons.append(f"VIDEO_{field.upper()}_NOT_PASS")
        guard = _require_mapping(claimed.get("terminal_guard"), "video validation guard")
        if (
            guard.get("observed") is not True
            or guard.get("included_in_business_domain") is not False
            or guard.get("business_frame_domain") != [0, count]
            or guard.get("checksum_matches") is not True
            or guard.get("pts_matches") is not True
        ):
            claimed_reasons.append("VIDEO_TERMINAL_GUARD_INVALID")

        source_pixel_format = _single_pixel_format(source_oracle, "source video oracle")
        proxy_pixel_format = _single_pixel_format(decoded_oracle, "decoded proxy oracle")
        if source_pixel_format != proxy_pixel_format:
            claimed_reasons.append("VIDEO_PIXEL_FORMAT_MISMATCH")

        source_rows = source_oracle.get("pts_table")
        if not isinstance(source_rows, list) or not source_rows:
            raise AudioEvidenceError("source video oracle has no pts_table")
        recomputed = proxy_verifier.evaluate_video_proxy(
            video_manifest, source_rows, decoded_oracle
        )
        if recomputed.get("status") != "PASS":
            claimed_reasons.extend(
                f"VIDEO_RECHECK_{reason}"
                for reason in recomputed.get("reason_codes", [])
            )
        claimed_reasons = list(dict.fromkeys(claimed_reasons))
        return {
            "status": "PASS" if not claimed_reasons else "BLOCKED",
            "reason_codes": claimed_reasons,
            "source_path": str(source_path),
            "proxy_path": str(proxy_path),
            "source_oracle": {"path": str(source_oracle_path), "sha256": sha256_file(source_oracle_path)},
            "decoded_proxy_oracle": {"path": str(decoded_oracle_path), "sha256": sha256_file(decoded_oracle_path)},
            "source_pixel_format": source_pixel_format,
            "proxy_pixel_format": proxy_pixel_format,
            "recomputed": recomputed,
        }
    except (AudioEvidenceError, proxy_verifier.ProxyEvidenceError, KeyError, TypeError, ValueError) as exc:
        return {
            "status": "BLOCKED",
            "reason_codes": ["VIDEO_EVIDENCE_INVALID"],
            "error": str(exc),
        }


def _source_video_rows(video_manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source = _require_mapping(video_manifest.get("source"), "video manifest source")
    oracle = _require_mapping(source.get("oracle"), "video manifest source oracle")
    source_path = _validate_bound_file(source, "source video")
    source_sha256 = str(source.get("sha256"))
    oracle_path, report = _load_bound_oracle(
        oracle, "source video oracle", source_path, source_sha256
    )
    rows = report.get("pts_table")
    if not isinstance(rows, list) or not rows:
        raise AudioEvidenceError("source video oracle has no pts_table")
    for index, value in enumerate(rows):
        row = _require_mapping(value, f"source video oracle pts_table[{index}]")
        if row.get("n") != index:
            raise AudioEvidenceError("source video oracle frame indices are not contiguous")
        _require_int(row.get("pts"), f"source video oracle pts_table[{index}].pts")
        _require_int(
            row.get("duration"),
            f"source video oracle pts_table[{index}].duration",
            minimum=1,
        )
    timeline = _require_mapping(video_manifest.get("normalized_timeline"), "video normalized timeline")
    return rows, timeline


def _timeline_snapshot(timeline_manifest: dict[str, Any]) -> dict[str, Any]:
    source = _require_mapping(timeline_manifest.get("source"), "timeline manifest source")
    timeline = _require_mapping(
        timeline_manifest.get("normalized_timeline"), "normalized timeline"
    )
    time_base = _require_mapping(timeline.get("time_base"), "normalized time base")
    frame_count = _require_int(
        source.get("business_frame_count"), "business frame count", minimum=1
    )
    source_start = _require_int(
        source.get("business_frame_start", 0),
        "business frame start",
        minimum=0,
    )
    source_end = source_start + frame_count
    expected_mapping = proxy_verifier._frame_mapping(source_start, frame_count)
    duration_ticks = _require_int(
        timeline.get("duration_ticks"), "normalized duration ticks", minimum=1
    )
    numerator = _require_int(
        time_base.get("numerator"), "normalized time base numerator", minimum=1
    )
    denominator = _require_int(
        time_base.get("denominator"), "normalized time base denominator", minimum=1
    )
    if timeline.get("business_pts_start") != 0:
        raise AudioEvidenceError("normalized business timeline must start at zero")
    if timeline.get("business_pts_end_exclusive") != frame_count * duration_ticks:
        raise AudioEvidenceError("normalized business timeline end is inconsistent")
    scope = source.get("scope")
    if scope not in ("prefix", "full"):
        raise AudioEvidenceError("timeline scope must be prefix or full")
    if scope == "full" and source_start != 0:
        raise AudioEvidenceError("full timeline scope cannot start at a non-zero source frame")
    require_explicit_mapping = source_start != 0
    for owner_name, owner in (("timeline source", source), ("normalized timeline", timeline)):
        for field, expected in (
            ("source_frame_domain", expected_mapping["source_frame_domain"]),
            ("proxy_frame_domain", expected_mapping["proxy_frame_domain"]),
            ("frame_mapping", expected_mapping),
        ):
            value = owner.get(field)
            if value is None and not require_explicit_mapping:
                continue
            if value != expected:
                raise AudioEvidenceError(
                    f"{owner_name} {field} differs from the declared frame window"
                )
    timeline_source_start = timeline.get("source_frame_start")
    timeline_source_end = timeline.get("source_frame_end_exclusive")
    if require_explicit_mapping and (
        timeline_source_start != source_start or timeline_source_end != source_end
    ):
        raise AudioEvidenceError(
            "normalized timeline source frame bounds are missing or inconsistent"
        )
    if timeline_source_start is not None and timeline_source_start != source_start:
        raise AudioEvidenceError("normalized timeline source frame start is inconsistent")
    if timeline_source_end is not None and timeline_source_end != source_end:
        raise AudioEvidenceError("normalized timeline source frame end is inconsistent")
    return {
        "scope": scope,
        "business_frame_start": source_start,
        "business_frame_count": frame_count,
        "source_frame_domain": expected_mapping["source_frame_domain"],
        "business_frame_domain": [0, frame_count],
        "proxy_frame_domain": expected_mapping["proxy_frame_domain"],
        "frame_mapping": expected_mapping,
        "time_base": {"numerator": numerator, "denominator": denominator},
        "duration_ticks": duration_ticks,
    }


def _pts_conflict_indices(rows: Sequence[dict[str, Any]]) -> set[int]:
    by_pts: dict[int, list[int]] = {}
    conflicts: set[int] = set()
    previous_pts: int | None = None
    previous_index: int | None = None
    for index, row in enumerate(rows):
        pts = row.get("pts")
        if not isinstance(pts, int) or isinstance(pts, bool):
            continue
        by_pts.setdefault(pts, []).append(index)
        if previous_pts is not None and pts <= previous_pts:
            conflicts.add(index)
            if previous_index is not None:
                conflicts.add(previous_index)
        previous_pts = pts
        previous_index = index
    for indices in by_pts.values():
        if len(indices) > 1:
            conflicts.update(indices)
    return conflicts


def _zone_contains_frame(
    zone: str,
    index: int,
    frame_count: int,
    conflict_indices: set[int],
    *,
    frame_start: int = 0,
) -> bool:
    if zone == "pts_conflict":
        return index in conflict_indices
    local_index = index - frame_start
    if local_index < 0 or local_index >= frame_count:
        return False
    if zone == "start":
        return local_index < max(1, math.ceil(frame_count * 0.10))
    if zone == "middle":
        return (
            math.floor(frame_count * 0.40)
            <= local_index
            < max(math.floor(frame_count * 0.40) + 1, math.ceil(frame_count * 0.60))
        )
    if zone == "end":
        return local_index >= min(frame_count - 1, math.floor(frame_count * 0.90))
    return False


def _audio_frame_for_sample(
    audio: dict[str, Any], sample: int
) -> dict[str, Any] | None:
    rows = (audio.get("frames") or {}).get("pts_table") or []
    for row in rows:
        if not isinstance(row, dict):
            continue
        pts = row.get("pts")
        count = row.get("nb_samples")
        if (
            isinstance(pts, int)
            and not isinstance(pts, bool)
            and isinstance(count, int)
            and not isinstance(count, bool)
            and count > 0
            and pts <= sample < pts + count
        ):
            return row
    return None


def _validate_window_artifact_file(
    record: dict[str, Any],
    name: str,
    *,
    media_type: str,
) -> Path:
    if record.get("media_type") != media_type:
        raise AudioEvidenceError(f"{name} media type is invalid")
    path = _validate_bound_file(record, name)
    if record.get("size") != path.stat().st_size or path.stat().st_size <= 8:
        raise AudioEvidenceError(f"{name} size binding is stale or empty")
    with path.open("rb") as stream:
        header = stream.read(12)
    if media_type == "image/png" and not header.startswith(b"\x89PNG\r\n\x1a\n"):
        raise AudioEvidenceError(f"{name} is not a PNG capture")
    if media_type == "audio/wav" and not (
        header.startswith(b"RIFF") and header[8:12] == b"WAVE"
    ):
        raise AudioEvidenceError(f"{name} is not a WAV capture")
    return path


def _run_content_anchor_reextraction(
    command: list[str],
    output: Path,
    name: str,
) -> None:
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
            timeout=CONTENT_ANCHOR_REEXTRACT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioEvidenceError(f"{name} independent re-extraction timed out") from exc
    except OSError as exc:
        raise AudioEvidenceError(
            f"{name} independent re-extraction could not start: {exc}"
        ) from exc
    if completed.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise AudioEvidenceError(
            f"{name} independent re-extraction failed: "
            f"{detail or completed.returncode}"
        )


def _verify_content_anchor_artifact_reextraction(
    *,
    side: str,
    ffmpeg_path: Path,
    media_path: Path,
    frame_index: int,
    decoded_start_sample: int,
    decoded_end_sample_exclusive: int,
    visual_path: Path,
    audio_path: Path,
) -> dict[str, Any]:
    """Reproduce both captures from bound inputs instead of trusting their headers."""
    with tempfile.TemporaryDirectory(prefix=f"mpv-anchor-{side}-") as temp_value:
        temp = Path(temp_value)
        reproduced_visual = temp / "frame.png"
        reproduced_audio = temp / "audio.wav"
        video_command = content_anchor_video_capture_command(
            ffmpeg_path, media_path, frame_index, reproduced_visual
        )
        audio_command = content_anchor_audio_capture_command(
            ffmpeg_path,
            media_path,
            decoded_start_sample=decoded_start_sample,
            decoded_end_sample_exclusive=decoded_end_sample_exclusive,
            output=reproduced_audio,
        )
        _run_content_anchor_reextraction(
            video_command, reproduced_visual, f"{side} visual artifact"
        )
        _run_content_anchor_reextraction(
            audio_command, reproduced_audio, f"{side} audio artifact"
        )
        _validate_window_artifact_file(
            {
                **_file_record(reproduced_visual, include_size=True),
                "media_type": "image/png",
            },
            f"{side} independently extracted visual artifact",
            media_type="image/png",
        )
        _validate_window_artifact_file(
            {
                **_file_record(reproduced_audio, include_size=True),
                "media_type": "audio/wav",
            },
            f"{side} independently extracted audio artifact",
            media_type="audio/wav",
        )
        expected_visual_sha256 = sha256_file(visual_path)
        expected_audio_sha256 = sha256_file(audio_path)
        reproduced_visual_sha256 = sha256_file(reproduced_visual)
        reproduced_audio_sha256 = sha256_file(reproduced_audio)
        if reproduced_visual_sha256.lower() != expected_visual_sha256.lower():
            raise AudioEvidenceError(
                f"{side} visual artifact differs from independent re-extraction"
            )
        if reproduced_audio_sha256.lower() != expected_audio_sha256.lower():
            raise AudioEvidenceError(
                f"{side} audio artifact differs from independent re-extraction"
            )
        return {
            "status": "PASS",
            "visual_sha256": reproduced_visual_sha256,
            "audio_sha256": reproduced_audio_sha256,
        }


def _capture_temporary_output_from_command(
    command: Any,
    final_output: Path,
    name: str,
) -> Path:
    if not isinstance(command, list) or not command or not isinstance(command[-1], str):
        raise AudioEvidenceError(f"{name} capture command is invalid")
    temporary = Path(command[-1]).expanduser().resolve()
    final_output = final_output.expanduser().resolve()
    expected_name = re.compile(
        rf"\.{re.escape(final_output.stem)}\.[0-9a-f]{{32}}"
        rf"\.capture{re.escape(final_output.suffix)}"
    )
    if temporary.parent != final_output.parent or not expected_name.fullmatch(
        temporary.name
    ):
        raise AudioEvidenceError(f"{name} capture temporary output is invalid")
    return temporary


_FORMULA_EVENT_TOKENS = frozenset(
    {
        "formula",
        "derived",
        "derived_from_formula",
        "normalized_clock",
        "frame_clock",
        "pts_formula",
    }
)


def _assert_source_only_payload(value: Any, *, path: str = "source-anchor") -> None:
    """Reject proxy-bearing fields anywhere in a source-only evidence file.

    Values may contain ordinary path text (for example ``verify_proxy_audio_av``);
    only object keys are part of the schema and therefore subject to this check.
    """
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise SourceAnchorEvidenceError(
                    "AV_SOURCE_ANCHOR_SOURCE_ONLY_VIOLATION",
                    f"{path} contains a non-string field name",
                )
            if "proxy" in key.casefold():
                raise SourceAnchorEvidenceError(
                    "AV_SOURCE_ANCHOR_SOURCE_ONLY_VIOLATION",
                    f"{path}.{key} is not permitted in a source-only manifest",
                )
            _assert_source_only_payload(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_source_only_payload(child, path=f"{path}[{index}]")


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _source_event_validation(anchor: dict[str, Any], position: int) -> None:
    """Require an independently observed visual *and* audio event.

    A frame/sample pair selected from ``n * duration`` is a clock mapping, not
    content evidence.  Requiring explicit observations in both domains keeps
    the source registration useful even when the proxy later changes its PTS.
    """
    if anchor.get("derived_from_formula") is True or anchor.get("formula_derived") is True:
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_FORMULA_DERIVED",
            f"source anchor {position} is marked as formula-derived",
        )
    event = anchor.get("event")
    if not isinstance(event, dict):
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_EVENT_MISSING",
            f"source anchor {position} has no observed event description",
        )
    method = event.get("method")
    if not _nonempty_text(method):
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_EVENT_METHOD_MISSING",
            f"source anchor {position} event method is missing",
        )
    method_tokens = {
        token
        for token in re.split(r"[^a-z0-9_]+", str(method).lower())
        if token
    }
    if method_tokens & _FORMULA_EVENT_TOKENS or "formula" in str(method).lower():
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_FORMULA_DERIVED",
            f"source anchor {position} event method is formula-derived",
        )
    if event.get("observed") is not True:
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_EVENT_NOT_OBSERVED",
            f"source anchor {position} is not marked as actually observed",
        )
    description = event.get("description")
    if not _nonempty_text(description):
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_EVENT_DESCRIPTION_MISSING",
            f"source anchor {position} event description is missing",
        )

    # Accept the explicit schema and a few readable aliases used by manual
    # observation tooling, but never infer either event from frame arithmetic.
    video = event.get("video")
    audio = event.get("audio")
    video_observed = (
        isinstance(video, dict)
        and video.get("observed") is True
        and _nonempty_text(video.get("description"))
    ) or (
        event.get("video_observed") is True
        and _nonempty_text(event.get("video_description"))
    ) or (
        event.get("visual_change_observed") is True
        and _nonempty_text(event.get("visual_description"))
    )
    audio_observed = (
        isinstance(audio, dict)
        and audio.get("observed") is True
        and _nonempty_text(audio.get("description"))
    ) or (
        event.get("audio_observed") is True
        and _nonempty_text(event.get("audio_description"))
    ) or (
        event.get("audio_transient_observed") is True
        and _nonempty_text(event.get("audio_description"))
    )
    if not video_observed or not audio_observed:
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_REAL_EVENT_REQUIRED",
            f"source anchor {position} lacks independently observed video/audio events",
        )


def _parse_evidence_utc(value: Any, name: str) -> datetime:
    if not _nonempty_text(value):
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_CHRONOLOGY_UNPROVEN",
            f"{name} timestamp is missing",
        )
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_CHRONOLOGY_UNPROVEN",
            f"{name} timestamp is invalid",
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _load_source_content_anchor_observations(
    observation_record: dict[str, Any],
    *,
    scope: str,
    source_start_frame: int,
    frame_count: int,
    source_rows: Sequence[dict[str, Any]],
    source_audio: dict[str, Any],
    source_media_path: Path,
    source_oracle_path: Path,
    expected_ffmpeg_path: Path,
) -> dict[str, Any]:
    """Load and validate a source-only observation input.

    This intentionally does not accept ``proxy_observation`` or any proxy
    frame/sample fields.  It is the only input from which a source anchor
    manifest can be registered.
    """
    observation_path = _validate_bound_file(
        observation_record, "source content-anchor observation input"
    )
    observation = _load_json(observation_path)
    if (
        observation.get("schema_version") != SOURCE_CONTENT_ANCHOR_SCHEMA_VERSION
        or observation.get("kind")
        not in (SOURCE_CONTENT_ANCHOR_OBSERVATION_KIND, CONTENT_ANCHOR_OBSERVATION_KIND)
        or observation.get("scope") != scope
    ):
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_OBSERVATION_INVALID",
            "source content-anchor observation kind, version, or scope is invalid",
        )
    _assert_source_only_payload(observation)
    observer = _require_mapping(
        observation.get("observer"), "source content-anchor observer tool"
    )
    observer_path = _validate_exact_file_binding(
        observer,
        "source content-anchor observer tool",
        CONTENT_ANCHOR_CAPTURE_TOOL,
    )
    method = observation.get("method")
    if not _nonempty_text(method):
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_OBSERVATION_METHOD_MISSING",
            "source content-anchor observation method is missing",
        )
    audio_clock = _require_mapping(
        observation.get("audio_clock"), "source content-anchor audio clock"
    )
    source_rate = (source_audio.get("format") or {}).get("sample_rate")
    if (
        audio_clock.get("source_stream", "0:a:0") != "0:a:0"
        or audio_clock.get("sample_index_basis")
        not in ("ashowinfo_pts", "decoded_ashowinfo_pts")
        or audio_clock.get("source_sample_rate") != source_rate
    ):
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_AUDIO_CLOCK_INVALID",
            "source content-anchor audio clock binding is invalid",
        )
    anchors = observation.get("anchors")
    if not isinstance(anchors, list):
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_OBSERVATION_INVALID",
            "source content-anchor observations must contain an anchor list",
        )
    zones = [value.get("zone") if isinstance(value, dict) else None for value in anchors]
    semantic_reasons: list[str] = []
    if zones != list(REQUIRED_CONTENT_ANCHOR_ZONES):
        semantic_reasons.append("AV_CONTENT_ANCHOR_ZONES_INCOMPLETE")
    evidence_ids: set[str] = set()
    source_windows: list[dict[str, Any]] = []
    source_end_frame = source_start_frame + frame_count
    if observation.get("source_frame_domain") != [
        source_start_frame,
        source_end_frame,
    ]:
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_FRAME_DOMAIN_INVALID",
            "source content-anchor observation frame domain is missing or stale",
        )
    conflict_indices = _pts_conflict_indices(source_rows)
    normalized_anchors: list[dict[str, Any]] = []
    for position, value in enumerate(anchors):
        if not isinstance(value, dict):
            semantic_reasons.append("AV_SOURCE_ANCHOR_INVALID")
            continue
        try:
            _source_event_validation(value, position)
        except SourceAnchorEvidenceError as exc:
            semantic_reasons.append(exc.reason_code)
        source_index = value.get("source_frame_index")
        if (
            not isinstance(source_index, int)
            or isinstance(source_index, bool)
            or source_index < source_start_frame
            or source_index >= source_end_frame
            or source_index >= len(source_rows)
        ):
            semantic_reasons.append("AV_SOURCE_ANCHOR_FRAME_OUTSIDE_BUSINESS_DOMAIN")
            continue
        evidence_id = value.get("evidence_id")
        if not _nonempty_text(evidence_id):
            semantic_reasons.append("AV_SOURCE_ANCHOR_EVIDENCE_ID_MISSING")
        elif evidence_id in evidence_ids:
            semantic_reasons.append("AV_SOURCE_ANCHOR_EVIDENCE_ID_DUPLICATE")
        else:
            evidence_ids.add(str(evidence_id))
        zone = value.get("zone")
        if not isinstance(zone, str) or not _zone_contains_frame(
            zone,
            source_index,
            frame_count,
            conflict_indices,
            frame_start=source_start_frame,
        ):
            semantic_reasons.append("AV_CONTENT_ANCHOR_ZONE_POSITION_INVALID")
        checksum = str(source_rows[source_index].get("checksum", "")).upper()
        if not checksum or str(value.get("source_frame_checksum", "")).upper() != checksum:
            semantic_reasons.append("AV_SOURCE_ANCHOR_FRAME_CHECKSUM_MISMATCH")
        source_sample = value.get("source_audio_sample")
        if (
            not isinstance(source_sample, int)
            or isinstance(source_sample, bool)
            or source_sample < 0
        ):
            semantic_reasons.append("AV_SOURCE_ANCHOR_AUDIO_SAMPLE_INVALID")
            continue
        try:
            source_window = _load_content_anchor_window(
                _require_mapping(
                    value.get("source_observation"),
                    f"source anchor {position} observation window",
                ),
                side="source",
                scope=scope,
                media_path=source_media_path,
                oracle_path=source_oracle_path,
                frame_index=source_index,
                frame_checksum=checksum,
                audio_sample=source_sample,
                audio=source_audio,
                observer_tool_path=observer_path,
                expected_ffmpeg_path=expected_ffmpeg_path,
            )
        except (AudioEvidenceError, KeyError, OSError, TypeError, ValueError) as exc:
            semantic_reasons.append("AV_SOURCE_ANCHOR_WINDOW_INVALID")
            source_window = None
        if source_window is not None:
            source_windows.append({"position": position, "source": source_window})
        # Keep only source-domain fields in the normalized copy.  This copy is
        # what later proxy manifests must match byte-for-byte semantically.
        normalized_anchors.append(
            {
                "zone": zone,
                "evidence_id": evidence_id,
                "source_frame_index": source_index,
                "source_audio_sample": source_sample,
                "source_frame_checksum": checksum,
                "event": value.get("event"),
                "source_observation": value.get("source_observation"),
            }
        )
    if semantic_reasons:
        unique_reasons = list(dict.fromkeys(semantic_reasons))
        primary_reason = next(
            (
                reason
                for reason in unique_reasons
                if reason.startswith("AV_SOURCE_ANCHOR_")
            ),
            "AV_SOURCE_ANCHOR_OBSERVATION_INVALID",
        )
        raise SourceAnchorEvidenceError(
            primary_reason,
            "; ".join(unique_reasons),
        )
    return {
        "path": observation_path,
        "record": _file_record(observation_path),
        "payload": observation,
        "anchors": normalized_anchors,
        "windows": source_windows,
    }


def _load_content_anchor_window(
    window_record: dict[str, Any],
    *,
    side: str,
    scope: str,
    media_path: Path,
    oracle_path: Path,
    frame_index: int,
    frame_checksum: str,
    audio_sample: int,
    audio: dict[str, Any],
    observer_tool_path: Path,
    expected_ffmpeg_path: Path,
) -> dict[str, Any]:
    window_path = _validate_bound_file(window_record, f"{side} observation window")
    window = _load_json(window_path)
    if (
        window.get("schema_version") != CONTENT_ANCHOR_SCHEMA_VERSION
        or window.get("kind") != CONTENT_ANCHOR_WINDOW_KIND
        or window.get("side") != side
        or window.get("scope") != scope
    ):
        raise AudioEvidenceError(f"{side} observation window kind, version, or scope is invalid")
    _validate_exact_file_binding(
        _require_mapping(window.get("media"), f"{side} window media"),
        f"{side} window media",
        media_path,
        require_size=True,
    )
    _validate_exact_file_binding(
        _require_mapping(window.get("oracle"), f"{side} window oracle"),
        f"{side} window oracle",
        oracle_path,
    )
    video_frame = _require_mapping(
        window.get("video_frame"), f"{side} window video frame"
    )
    if (
        video_frame.get("index") != frame_index
        or str(video_frame.get("checksum", "")).upper() != frame_checksum.upper()
    ):
        raise AudioEvidenceError(f"{side} observation video frame binding is stale")

    sample_rate = (audio.get("format") or {}).get("sample_rate")
    audio_frame = _audio_frame_for_sample(audio, audio_sample)
    if audio_frame is None:
        raise AudioEvidenceError(f"{side} observation sample has no decoded audio frame")
    bound_audio_frame = _require_mapping(
        window.get("audio_frame"), f"{side} window audio frame"
    )
    for field in ("n", "pts", "nb_samples", "checksum"):
        if bound_audio_frame.get(field) != audio_frame.get(field):
            raise AudioEvidenceError(f"{side} observation audio frame binding is stale")
    audio_window = _require_mapping(
        window.get("audio_window"), f"{side} audio window"
    )
    start_sample = audio_window.get("start_sample")
    end_sample = audio_window.get("end_sample_exclusive")
    if (
        audio_window.get("stream") != "0:a:0"
        or audio_window.get("sample_rate") != sample_rate
        or audio_window.get("anchor_sample") != audio_sample
        or not isinstance(start_sample, int)
        or isinstance(start_sample, bool)
        or not isinstance(end_sample, int)
        or isinstance(end_sample, bool)
        or start_sample > audio_sample
        or audio_sample >= end_sample
        or end_sample - start_sample > int(sample_rate or 0) * 2
    ):
        raise AudioEvidenceError(f"{side} audio observation window is invalid")
    frames = audio.get("frames") or {}
    decoded_start = frames.get("start_pts")
    decoded_end = frames.get("end_pts_exclusive")
    if (
        not isinstance(decoded_start, int)
        or not isinstance(decoded_end, int)
        or start_sample < decoded_start
        or end_sample > decoded_end
    ):
        raise AudioEvidenceError(f"{side} audio observation window is out of range")

    visual_path = _validate_window_artifact_file(
        _require_mapping(window.get("visual_artifact"), f"{side} visual artifact"),
        f"{side} visual artifact",
        media_type="image/png",
    )
    audio_path = _validate_window_artifact_file(
        _require_mapping(window.get("audio_artifact"), f"{side} audio artifact"),
        f"{side} audio artifact",
        media_type="audio/wav",
    )
    capture = _require_mapping(window.get("capture"), f"{side} capture provenance")
    _validate_exact_file_binding(
        _require_mapping(capture.get("tool"), f"{side} capture tool"),
        f"{side} capture tool",
        observer_tool_path,
    )
    _validate_exact_file_binding(
        _require_mapping(capture.get("ffmpeg"), f"{side} capture FFmpeg"),
        f"{side} capture FFmpeg",
        expected_ffmpeg_path,
    )
    commands = _require_mapping(capture.get("commands"), f"{side} capture commands")
    publication = _require_mapping(
        capture.get("publication"), f"{side} capture publication"
    )
    if (
        publication.get("method") != "hardlink_create_new"
        or publication.get("visual_target") != str(visual_path)
        or publication.get("audio_target") != str(audio_path)
    ):
        raise AudioEvidenceError(f"{side} capture publication binding is stale")
    video_command = commands.get("video")
    audio_command = commands.get("audio")
    temporary_visual = _capture_temporary_output_from_command(
        video_command, visual_path, f"{side} visual"
    )
    temporary_audio = _capture_temporary_output_from_command(
        audio_command, audio_path, f"{side} audio"
    )
    expected_video_command = content_anchor_video_capture_command(
        expected_ffmpeg_path, media_path, frame_index, temporary_visual
    )
    expected_audio_command = content_anchor_audio_capture_command(
        expected_ffmpeg_path,
        media_path,
        decoded_start_sample=start_sample - decoded_start,
        decoded_end_sample_exclusive=end_sample - decoded_start,
        output=temporary_audio,
    )
    if (
        video_command != expected_video_command
        or audio_command != expected_audio_command
    ):
        raise AudioEvidenceError(f"{side} capture command binding is stale")
    reextraction = _verify_content_anchor_artifact_reextraction(
        side=side,
        ffmpeg_path=expected_ffmpeg_path,
        media_path=media_path,
        frame_index=frame_index,
        decoded_start_sample=start_sample - decoded_start,
        decoded_end_sample_exclusive=end_sample - decoded_start,
        visual_path=visual_path,
        audio_path=audio_path,
    )
    return {
        "path": str(window_path),
        "sha256": sha256_file(window_path),
        "visual_artifact": _file_record(visual_path, include_size=True),
        "audio_artifact": _file_record(audio_path, include_size=True),
        "independent_reextraction": reextraction,
    }


def _load_content_anchor_observations(
    observation_record: dict[str, Any],
    *,
    scope: str,
    source_start_frame: int,
    frame_count: int,
    source_rows: Sequence[dict[str, Any]],
    proxy_rows: Sequence[dict[str, Any]],
    source_audio: dict[str, Any],
    proxy_audio: dict[str, Any],
    source_media_path: Path,
    proxy_media_path: Path,
    source_oracle_path: Path,
    proxy_oracle_path: Path,
    expected_ffmpeg_path: Path,
    registered_source_anchors: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    observation_path = _validate_bound_file(
        observation_record, "content-anchor observation input"
    )
    observation = _load_json(observation_path)
    if (
        observation.get("schema_version") != CONTENT_ANCHOR_SCHEMA_VERSION
        or observation.get("kind") != CONTENT_ANCHOR_OBSERVATION_KIND
        or observation.get("scope") != scope
    ):
        raise AudioEvidenceError("content-anchor observation kind, version, or scope is invalid")
    observer = _require_mapping(
        observation.get("observer"), "content-anchor observer tool"
    )
    observer_path = _validate_exact_file_binding(
        observer,
        "content-anchor observer tool",
        CONTENT_ANCHOR_CAPTURE_TOOL,
    )
    method = observation.get("method")
    if not isinstance(method, str) or not method.strip():
        raise AudioEvidenceError("content-anchor observation method is missing")
    audio_clock = _require_mapping(
        observation.get("audio_clock"), "content-anchor audio clock"
    )
    source_rate = (source_audio.get("format") or {}).get("sample_rate")
    proxy_rate = (proxy_audio.get("format") or {}).get("sample_rate")
    sample_basis = audio_clock.get("sample_index_basis")
    declared_source_rate = audio_clock.get("source_sample_rate")
    declared_proxy_rate = audio_clock.get("proxy_sample_rate")
    if (
        audio_clock.get("source_stream", "0:a:0") != "0:a:0"
        or audio_clock.get("proxy_stream", "0:a:0") != "0:a:0"
        or sample_basis not in ("ashowinfo_pts", "decoded_ashowinfo_pts")
        or declared_source_rate != source_rate
        or declared_proxy_rate != proxy_rate
    ):
        raise AudioEvidenceError("content-anchor audio clock binding is invalid")

    anchors = observation.get("anchors")
    if not isinstance(anchors, list):
        raise AudioEvidenceError("content-anchor observations must contain an anchor list")
    semantic_reasons: list[str] = []
    zones = [value.get("zone") if isinstance(value, dict) else None for value in anchors]
    if zones != list(REQUIRED_CONTENT_ANCHOR_ZONES):
        semantic_reasons.append("AV_CONTENT_ANCHOR_ZONES_INCOMPLETE")
    if len(anchors) != len(registered_source_anchors):
        semantic_reasons.append("AV_SOURCE_ANCHOR_REGISTRATION_MISMATCH")
    evidence_ids: set[str] = set()
    window_records: list[dict[str, Any]] = []
    source_end_frame = source_start_frame + frame_count
    conflict_indices = _pts_conflict_indices(source_rows)
    for position, value in enumerate(anchors):
        if not isinstance(value, dict):
            semantic_reasons.append("AV_CONTENT_ANCHOR_INVALID")
            continue
        evidence_id = value.get("evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            semantic_reasons.append("AV_CONTENT_ANCHOR_EVIDENCE_ID_MISSING")
        elif evidence_id in evidence_ids:
            semantic_reasons.append("AV_CONTENT_ANCHOR_EVIDENCE_ID_DUPLICATE")
        else:
            evidence_ids.add(evidence_id)
        if position >= len(registered_source_anchors) or _source_anchor_projection(
            value
        ) != _source_anchor_projection(registered_source_anchors[position]):
            semantic_reasons.append("AV_SOURCE_ANCHOR_REGISTRATION_MISMATCH")
        source_index = value.get("source_frame_index")
        proxy_index = value.get("proxy_frame_index")
        if (
            not isinstance(source_index, int)
            or isinstance(source_index, bool)
            or not isinstance(proxy_index, int)
            or isinstance(proxy_index, bool)
            or source_index < source_start_frame
            or proxy_index < 0
            or source_index >= source_end_frame
            or proxy_index >= frame_count
        ):
            semantic_reasons.append("AV_CONTENT_ANCHOR_FRAME_OUTSIDE_BUSINESS_DOMAIN")
            continue
        if proxy_index != source_index - source_start_frame:
            semantic_reasons.append("AV_CONTENT_ANCHOR_FRAME_MAPPING_INVALID")
        if source_index >= len(source_rows) or proxy_index >= len(proxy_rows):
            semantic_reasons.append("AV_CONTENT_ANCHOR_FRAME_MISSING")
            continue
        zone = value.get("zone")
        if not isinstance(zone, str) or not _zone_contains_frame(
            zone,
            source_index,
            frame_count,
            conflict_indices,
            frame_start=source_start_frame,
        ):
            semantic_reasons.append("AV_CONTENT_ANCHOR_ZONE_POSITION_INVALID")
        source_checksum = str(source_rows[source_index].get("checksum", "")).upper()
        proxy_checksum = str(proxy_rows[proxy_index].get("checksum", "")).upper()
        observed_source = str(value.get("source_frame_checksum", "")).upper()
        observed_proxy = str(value.get("proxy_frame_checksum", "")).upper()
        if (
            not source_checksum
            or source_checksum != proxy_checksum
            or observed_source != source_checksum
            or observed_proxy != proxy_checksum
        ):
            semantic_reasons.append("AV_CONTENT_ANCHOR_OBSERVED_CHECKSUM_MISMATCH")
        landmark = value.get("landmark")
        if not isinstance(landmark, str) or not landmark.strip():
            semantic_reasons.append("AV_CONTENT_ANCHOR_LANDMARK_MISSING")
        source_sample = value.get("source_audio_sample")
        proxy_sample = value.get("proxy_audio_sample")
        if any(
            not isinstance(sample, int) or isinstance(sample, bool) or sample < 0
            for sample in (source_sample, proxy_sample)
        ):
            semantic_reasons.append("AV_CONTENT_ANCHOR_FIELDS_INVALID")
            continue
        source_window = _load_content_anchor_window(
            _require_mapping(
                value.get("source_observation"),
                f"content-anchor {position} source observation",
            ),
            side="source",
            scope=scope,
            media_path=source_media_path,
            oracle_path=source_oracle_path,
            frame_index=source_index,
            frame_checksum=source_checksum,
            audio_sample=source_sample,
            audio=source_audio,
            observer_tool_path=observer_path,
            expected_ffmpeg_path=expected_ffmpeg_path,
        )
        proxy_window = _load_content_anchor_window(
            _require_mapping(
                value.get("proxy_observation"),
                f"content-anchor {position} proxy observation",
            ),
            side="proxy",
            scope=scope,
            media_path=proxy_media_path,
            oracle_path=proxy_oracle_path,
            frame_index=proxy_index,
            frame_checksum=proxy_checksum,
            audio_sample=proxy_sample,
            audio=proxy_audio,
            observer_tool_path=observer_path,
            expected_ffmpeg_path=expected_ffmpeg_path,
        )
        window_records.append(
            {"position": position, "source": source_window, "proxy": proxy_window}
        )
    return {
        "path": observation_path,
        "record": _file_record(observation_path),
        "payload": observation,
        "anchors": anchors,
        "windows": window_records,
        "semantic_reason_codes": list(dict.fromkeys(semantic_reasons)),
    }


def _content_anchor_blocked(
    reason_code: str,
    *,
    error: str | None = None,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "BLOCKED",
        "reason_codes": [reason_code],
        "binding_checks": {"status": "BLOCKED"},
        "manifest": None,
        "observation": None,
        "anchors": [],
        "required_zones": list(REQUIRED_CONTENT_ANCHOR_ZONES),
        "observed_zones": [],
    }
    if error:
        result["binding_checks"]["error"] = error
    if manifest_path is not None:
        result["manifest"] = {"path": str(manifest_path.expanduser().resolve())}
    return result


def _source_anchor_projection(anchor: dict[str, Any]) -> dict[str, Any]:
    return {
        "zone": anchor.get("zone"),
        "evidence_id": anchor.get("evidence_id"),
        "source_frame_index": anchor.get("source_frame_index"),
        "source_audio_sample": anchor.get("source_audio_sample"),
        "source_frame_checksum": str(
            anchor.get("source_frame_checksum", "")
        ).upper(),
        "event": anchor.get("event"),
        "source_observation": anchor.get("source_observation"),
    }


def _source_anchor_frame_sequence(
    anchors: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "position": position,
            "zone": anchor.get("zone"),
            "source_frame_index": anchor.get("source_frame_index"),
            "source_frame_checksum": str(
                anchor.get("source_frame_checksum", "")
            ).upper(),
        }
        for position, anchor in enumerate(anchors)
    ]


def create_source_content_anchor_manifest(
    source_media_path: Path,
    source_oracle_path: Path,
    observation_path: Path,
    output_path: Path,
    ffmpeg: frame_oracle.FfmpegExecutable | Path,
    *,
    scope: str,
    business_frame_count: int | None = None,
    source_start_frame: int = 0,
) -> dict[str, Any]:
    """Register source content events before any normalized proxy is built.

    The observation input and output manifest are source-only.  The resulting
    file is write-once and is later bound by SHA-256 from the paired proxy
    content-anchor manifest.
    """
    if scope not in ("prefix", "full"):
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_MANIFEST_INVALID",
            "source content-anchor scope must be prefix or full",
        )
    if (
        isinstance(source_start_frame, bool)
        or not isinstance(source_start_frame, int)
        or source_start_frame < 0
    ):
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_FRAME_DOMAIN_INVALID",
            "source anchor frame start is invalid",
        )
    if source_start_frame != 0 and business_frame_count is None:
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_FRAME_DOMAIN_INVALID",
            "a non-zero source frame start requires an explicit frame count",
        )
    source_media_path = source_media_path.expanduser().resolve()
    source_oracle_path = source_oracle_path.expanduser().resolve()
    observation_path = observation_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    if output_path.exists():
        raise FileExistsError(
            "source content-anchor manifest is write-once; choose a new path"
        )
    if not source_media_path.is_file() or not source_oracle_path.is_file():
        raise FileNotFoundError("source content-anchor media or oracle is missing")
    ffmpeg_path = (
        ffmpeg.path
        if isinstance(ffmpeg, frame_oracle.FfmpegExecutable)
        else Path(ffmpeg)
    )
    ffmpeg_path = ffmpeg_path.expanduser().resolve()
    registered_utc = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    source_media_record = _file_record(source_media_path, include_size=True)
    source_oracle_record = _file_record(source_oracle_path)
    _, source_oracle = _load_bound_oracle(
        source_oracle_record,
        "source anchor oracle",
        source_media_path,
        str(source_media_record["sha256"]),
    )
    source_rows = source_oracle.get("pts_table")
    if not isinstance(source_rows, list) or not source_rows:
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_ORACLE_INVALID",
            "source anchor oracle has no decoded frame sequence",
        )
    if business_frame_count is None:
        business_frame_count = len(source_rows) - source_start_frame
    if (
        isinstance(business_frame_count, bool)
        or not isinstance(business_frame_count, int)
        or business_frame_count < 1
    ):
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_FRAME_DOMAIN_INVALID",
            "source anchor business frame count is invalid",
        )
    source_end_frame = source_start_frame + business_frame_count
    if source_end_frame > len(source_rows):
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_FRAME_DOMAIN_INVALID",
            "source anchor frame window exceeds the source oracle",
        )
    if scope == "full" and (
        source_start_frame != 0 or source_end_frame != len(source_rows)
    ):
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_FRAME_DOMAIN_INVALID",
            "full source-anchor scope must cover the complete source oracle",
        )
    source_audio = decode_audio(source_media_path, ffmpeg_path)
    if source_audio.get("status") != "PASS" or not source_audio.get(
        "stream_present"
    ):
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_AUDIO_INVALID",
            "source anchor registration requires a decodable source audio stream",
        )
    observation_record = _file_record(observation_path)
    observation = _load_source_content_anchor_observations(
        observation_record,
        scope=scope,
        source_start_frame=source_start_frame,
        frame_count=business_frame_count,
        source_rows=source_rows,
        source_audio=source_audio,
        source_media_path=source_media_path,
        source_oracle_path=source_oracle_path,
        expected_ffmpeg_path=ffmpeg_path,
    )
    anchors = observation["anchors"]
    manifest = {
        "schema_version": SOURCE_CONTENT_ANCHOR_SCHEMA_VERSION,
        "kind": SOURCE_CONTENT_ANCHOR_MANIFEST_KIND,
        "created_utc": registered_utc,
        "source_only": True,
        "scope": scope,
        "bindings": {
            "source_media": source_media_record,
            "source_oracle": source_oracle_record,
            "source_observation_input": observation_record,
        },
        "source": {
            "business_frame_start": source_start_frame,
            "business_frame_count": business_frame_count,
            "source_frame_domain": [source_start_frame, source_end_frame],
            "frame_sequence_basis": "decoded_source_picture_order",
            "frame_sequence": _source_anchor_frame_sequence(anchors),
            "audio_window_sequence": [
                {
                    "position": position,
                    "zone": anchor.get("zone"),
                    "source_audio_sample": anchor.get("source_audio_sample"),
                    "source_observation": anchor.get("source_observation"),
                }
                for position, anchor in enumerate(anchors)
            ],
        },
        "anchors": anchors,
        "audio_evidence": audio_evidence_binding(source_audio),
        "registration": {
            "mode": "source_before_media_generation",
            "event_selection": "independent_real_content_observation",
            "write_once": True,
        },
        "tools": {
            "creator": _file_record(Path(__file__)),
            "verifier": _file_record(Path(__file__)),
            "capture": _file_record(CONTENT_ANCHOR_CAPTURE_TOOL),
            "ffmpeg": ffmpeg_version_record(ffmpeg_path),
        },
        "commands": {"source_audio": source_audio.get("command")},
    }
    _assert_source_only_payload(manifest)
    write_json_new(output_path, manifest)
    return manifest


register_source_content_anchor_manifest = create_source_content_anchor_manifest
# Short aliases keep the API discoverable for experiment scripts while the
# long name remains the canonical schema-facing entry point.
create_source_anchor_manifest = create_source_content_anchor_manifest
register_source_anchor_manifest = create_source_content_anchor_manifest


def _load_bound_source_content_anchor_manifest(
    manifest_record: dict[str, Any],
    *,
    scope: str,
    subject_manifest_path: Path,
    source_media_path: Path,
    source_oracle_path: Path,
    source_oracle: dict[str, Any],
    source_audio: dict[str, Any],
    expected_ffmpeg_path: Path,
) -> dict[str, Any]:
    source_manifest_path = _validate_bound_file(
        manifest_record, "source content-anchor manifest"
    )
    source_manifest = _load_json(source_manifest_path)
    if (
        source_manifest.get("schema_version")
        != SOURCE_CONTENT_ANCHOR_SCHEMA_VERSION
        or source_manifest.get("kind") != SOURCE_CONTENT_ANCHOR_MANIFEST_KIND
        or source_manifest.get("scope") != scope
        or source_manifest.get("source_only") is not True
    ):
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_MANIFEST_INVALID",
            "source content-anchor manifest kind, version, scope, or mode is invalid",
        )
    _assert_source_only_payload(source_manifest)
    bindings = _require_mapping(
        source_manifest.get("bindings"), "source content-anchor bindings"
    )
    source_media_record = _require_mapping(
        bindings.get("source_media"), "source anchor media binding"
    )
    _validate_exact_file_binding(
        source_media_record,
        "source anchor media",
        source_media_path,
        require_size=True,
    )
    source_oracle_record = _require_mapping(
        bindings.get("source_oracle"), "source anchor oracle binding"
    )
    _validate_exact_file_binding(
        source_oracle_record,
        "source anchor oracle",
        source_oracle_path,
    )
    _, bound_source_oracle = _load_bound_oracle(
        source_oracle_record,
        "source anchor oracle",
        source_media_path,
        str(source_media_record.get("sha256")),
    )
    if bound_source_oracle != source_oracle:
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_ORACLE_STALE",
            "source anchor in-memory oracle differs from bound evidence",
        )
    if source_manifest.get("audio_evidence") != audio_evidence_binding(source_audio):
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_AUDIO_STALE",
            "source anchor decoded audio evidence is stale",
        )
    tools = _require_mapping(source_manifest.get("tools"), "source anchor tools")
    for role in ("creator", "verifier"):
        _validate_exact_file_binding(
            _require_mapping(tools.get(role), f"source anchor {role} tool"),
            f"source anchor {role} tool",
            Path(__file__),
        )
    _validate_exact_file_binding(
        _require_mapping(tools.get("capture"), "source anchor capture tool"),
        "source anchor capture tool",
        CONTENT_ANCHOR_CAPTURE_TOOL,
    )
    _validate_exact_file_binding(
        _require_mapping(tools.get("ffmpeg"), "source anchor FFmpeg"),
        "source anchor FFmpeg",
        expected_ffmpeg_path,
    )
    source_section = _require_mapping(
        source_manifest.get("source"), "source anchor source section"
    )
    frame_start = _require_int(
        source_section.get("business_frame_start"),
        "source anchor business frame start",
        minimum=0,
    )
    frame_count = _require_int(
        source_section.get("business_frame_count"),
        "source anchor business frame count",
        minimum=1,
    )
    frame_end = frame_start + frame_count
    source_rows = source_oracle.get("pts_table")
    if (
        not isinstance(source_rows, list)
        or frame_end > len(source_rows)
        or source_section.get("source_frame_domain") != [frame_start, frame_end]
    ):
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_FRAME_DOMAIN_INVALID",
            "source anchor frame domain differs from the bound oracle",
        )
    subject_manifest_path = subject_manifest_path.expanduser().resolve()
    subject = _load_json(subject_manifest_path)
    subject_timeline = _timeline_snapshot(subject)
    if (
        subject_timeline["scope"] != scope
        or subject_timeline["source_frame_domain"] != [frame_start, frame_end]
    ):
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_FRAME_DOMAIN_INVALID",
            "source anchor frame domain differs from the proxy subject",
        )
    observation = _load_source_content_anchor_observations(
        _require_mapping(
            bindings.get("source_observation_input"),
            "source anchor observation binding",
        ),
        scope=scope,
        source_start_frame=frame_start,
        frame_count=frame_count,
        source_rows=source_rows,
        source_audio=source_audio,
        source_media_path=source_media_path,
        source_oracle_path=source_oracle_path,
        expected_ffmpeg_path=expected_ffmpeg_path,
    )
    anchors = source_manifest.get("anchors")
    if anchors != observation["anchors"]:
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_OBSERVATION_STALE",
            "source anchor manifest differs from its registered observations",
        )
    if source_section.get("frame_sequence_basis") != "decoded_source_picture_order":
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_FRAME_SEQUENCE_INVALID",
            "source anchor frame sequence basis is invalid",
        )
    if source_section.get("frame_sequence") != _source_anchor_frame_sequence(anchors):
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_FRAME_SEQUENCE_INVALID",
            "source anchor frame sequence is stale",
        )
    expected_audio_windows = [
        {
            "position": position,
            "zone": anchor.get("zone"),
            "source_audio_sample": anchor.get("source_audio_sample"),
            "source_observation": anchor.get("source_observation"),
        }
        for position, anchor in enumerate(anchors)
    ]
    if source_section.get("audio_window_sequence") != expected_audio_windows:
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_AUDIO_WINDOWS_INVALID",
            "source anchor audio window sequence is stale",
        )

    subject_source_anchor = subject.get("source_anchor_manifest")
    if not isinstance(subject_source_anchor, dict):
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_NOT_BOUND_AT_PROXY_GENERATION",
            "proxy subject did not bind the source anchor registration at generation",
        )
    try:
        _validate_exact_file_binding(
            subject_source_anchor,
            "proxy subject source anchor registration",
            source_manifest_path,
        )
    except AudioEvidenceError as exc:
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_NOT_BOUND_AT_PROXY_GENERATION",
            str(exc),
        ) from exc
    if (
        str(subject_source_anchor.get("sha256", "")).lower()
        != str(manifest_record.get("sha256", "")).lower()
    ):
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_NOT_BOUND_AT_PROXY_GENERATION",
            "proxy subject source anchor SHA-256 differs from paired evidence",
        )
    source_created = _parse_evidence_utc(
        source_manifest.get("created_utc"), "source anchor registration"
    )
    subject_created = _parse_evidence_utc(
        subject.get("created_utc"), "proxy subject creation"
    )
    if source_created > subject_created:
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_CREATED_AFTER_PROXY",
            "source anchor registration was created after the proxy subject",
        )
    try:
        source_mtime = source_manifest_path.stat().st_mtime_ns
        subject_mtime = subject_manifest_path.stat().st_mtime_ns
    except OSError as exc:
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_CHRONOLOGY_UNPROVEN",
            "cannot read source/proxy evidence file timestamps",
        ) from exc
    if source_mtime > subject_mtime:
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_CREATED_AFTER_PROXY",
            "source anchor evidence file appeared after the proxy subject",
        )
    return {
        "path": source_manifest_path,
        "record": _file_record(source_manifest_path),
        "payload": source_manifest,
        "anchors": anchors,
        "observation": observation["record"],
        "registered_utc": source_manifest.get("created_utc"),
        "timeline": subject_timeline,
    }


def evaluate_bound_content_anchor_manifest(
    manifest_path: Path | None,
    *,
    scope: str | None = None,
    expected_scope: str | None = None,
    subject_manifest_path: Path,
    timeline_manifest_path: Path,
    source_media_path: Path,
    proxy_media_path: Path,
    source_oracle_path: Path,
    proxy_oracle_path: Path,
    source_oracle: dict[str, Any],
    proxy_oracle: dict[str, Any],
    source_audio: dict[str, Any],
    proxy_audio: dict[str, Any],
    max_error_seconds: float,
    allow_no_audio: bool = False,
    expected_ffmpeg_path: Path | None = None,
    source_anchor_manifest_path: Path | None = None,
) -> dict[str, Any]:
    if scope is None:
        scope = expected_scope
    if scope not in ("prefix", "full"):
        return _content_anchor_blocked(
            "AV_CONTENT_ANCHOR_MANIFEST_INVALID",
            error="content-anchor scope must be prefix or full",
            manifest_path=manifest_path,
        )
    no_audio_pair = not source_audio.get("stream_present") and not proxy_audio.get(
        "stream_present"
    )
    if no_audio_pair and allow_no_audio:
        return {
            "status": "NOT_APPLICABLE_PASS",
            "reason_codes": [],
            "binding_checks": {"status": "NOT_APPLICABLE"},
            "manifest": None,
            "observation": None,
            "anchors": [],
            "required_zones": list(REQUIRED_CONTENT_ANCHOR_ZONES),
            "observed_zones": [],
        }
    if manifest_path is None:
        return _content_anchor_blocked("AV_CONTENT_ANCHORS_NOT_PROVIDED")
    manifest_path = manifest_path.expanduser().resolve()
    try:
        manifest = _load_json(manifest_path)
        if (
            manifest.get("schema_version") != CONTENT_ANCHOR_SCHEMA_VERSION
            or manifest.get("kind") != CONTENT_ANCHOR_MANIFEST_KIND
            or manifest.get("scope") != scope
        ):
            raise AudioEvidenceError("content-anchor manifest kind, version, or scope is invalid")
        bindings = _require_mapping(
            manifest.get("bindings"), "content-anchor manifest bindings"
        )
        source_anchor_binding = bindings.get("source_anchor_manifest")
        if not isinstance(source_anchor_binding, dict):
            raise SourceAnchorEvidenceError(
                "AV_SOURCE_ANCHOR_MANIFEST_NOT_PROVIDED",
                "paired content-anchor manifest has no source-first registration",
            )
        if source_anchor_manifest_path is not None:
            expected_source_anchor_path = source_anchor_manifest_path.expanduser().resolve()
            declared_source_anchor_path = source_anchor_binding.get("path")
            if not isinstance(declared_source_anchor_path, str) or not _same_path(
                Path(declared_source_anchor_path), expected_source_anchor_path
            ):
                raise SourceAnchorEvidenceError(
                    "AV_SOURCE_ANCHOR_MANIFEST_INVALID",
                    "paired manifest is bound to another source anchor registration",
                )
        _validate_exact_file_binding(
            _require_mapping(bindings.get("subject_manifest"), "subject manifest binding"),
            "subject manifest",
            subject_manifest_path,
        )
        _validate_exact_file_binding(
            _require_mapping(bindings.get("timeline_manifest"), "timeline manifest binding"),
            "timeline manifest",
            timeline_manifest_path,
        )
        _validate_exact_file_binding(
            _require_mapping(bindings.get("source_media"), "source media binding"),
            "source media",
            source_media_path,
            require_size=True,
        )
        _validate_exact_file_binding(
            _require_mapping(bindings.get("proxy_media"), "proxy media binding"),
            "proxy media",
            proxy_media_path,
            require_size=True,
        )
        _validate_exact_file_binding(
            _require_mapping(bindings.get("source_oracle"), "source oracle binding"),
            "source oracle",
            source_oracle_path,
        )
        _validate_exact_file_binding(
            _require_mapping(bindings.get("proxy_oracle"), "proxy oracle binding"),
            "proxy oracle",
            proxy_oracle_path,
        )
        source_record = _require_mapping(
            bindings.get("source_media"), "source media binding"
        )
        proxy_record = _require_mapping(
            bindings.get("proxy_media"), "proxy media binding"
        )
        _, bound_source_oracle = _load_bound_oracle(
            _require_mapping(bindings.get("source_oracle"), "source oracle binding"),
            "content-anchor source oracle",
            source_media_path,
            str(source_record.get("sha256")),
        )
        _, bound_proxy_oracle = _load_bound_oracle(
            _require_mapping(bindings.get("proxy_oracle"), "proxy oracle binding"),
            "content-anchor proxy oracle",
            proxy_media_path,
            str(proxy_record.get("sha256")),
        )
        if source_oracle != bound_source_oracle or proxy_oracle != bound_proxy_oracle:
            raise AudioEvidenceError("content-anchor in-memory oracle differs from bound evidence")

        timeline_manifest = _load_json(timeline_manifest_path)
        expected_timeline = _timeline_snapshot(timeline_manifest)
        if manifest.get("timeline") != expected_timeline or expected_timeline["scope"] != scope:
            raise AudioEvidenceError("content-anchor timeline binding is stale")
        threshold = _require_mapping(
            manifest.get("thresholds"), "content-anchor thresholds"
        ).get("max_content_anchor_error_seconds")
        if (
            not _valid_content_anchor_threshold(threshold)
            or float(threshold) != float(max_error_seconds)
        ):
            raise AudioEvidenceError("content-anchor threshold differs from the registered verifier threshold")
        audio_bindings = _require_mapping(
            manifest.get("audio_evidence"), "content-anchor audio evidence"
        )
        if audio_bindings.get("source") != audio_evidence_binding(source_audio):
            raise AudioEvidenceError("content-anchor source audio evidence is stale")
        if audio_bindings.get("proxy") != audio_evidence_binding(proxy_audio):
            raise AudioEvidenceError("content-anchor proxy audio evidence is stale")
        tools = _require_mapping(manifest.get("tools"), "content-anchor tools")
        for role in ("creator", "verifier"):
            tool_path = _validate_exact_file_binding(
                _require_mapping(tools.get(role), f"content-anchor {role} tool"),
                f"content-anchor {role} tool",
                Path(__file__),
            )
            if not _same_path(tool_path, Path(__file__)):
                raise AudioEvidenceError(f"content-anchor {role} tool path is invalid")
        if expected_ffmpeg_path is None:
            raise AudioEvidenceError("current FFmpeg path is required for content-anchor verification")
        _validate_exact_file_binding(
            _require_mapping(tools.get("ffmpeg"), "content-anchor FFmpeg tool"),
            "content-anchor FFmpeg tool",
            expected_ffmpeg_path,
        )

        try:
            source_anchor = _load_bound_source_content_anchor_manifest(
                source_anchor_binding,
                scope=scope,
                subject_manifest_path=subject_manifest_path,
                source_media_path=source_media_path,
                source_oracle_path=source_oracle_path,
                source_oracle=source_oracle,
                source_audio=source_audio,
                expected_ffmpeg_path=expected_ffmpeg_path,
            )
        except SourceAnchorEvidenceError:
            raise
        except (AudioEvidenceError, KeyError, OSError, TypeError, ValueError) as exc:
            raise SourceAnchorEvidenceError(
                "AV_SOURCE_ANCHOR_MANIFEST_INVALID", str(exc)
            ) from exc
        if source_anchor.get("timeline") != expected_timeline:
            raise SourceAnchorEvidenceError(
                "AV_SOURCE_ANCHOR_FRAME_DOMAIN_INVALID",
                "subject and timeline manifests declare different frame mappings",
            )
        source_registration = _require_mapping(
            manifest.get("source_anchor_registration"),
            "paired source anchor registration snapshot",
        )
        expected_source_registration = {
            "path": str(source_anchor["path"]),
            "sha256": source_anchor["record"]["sha256"],
            "created_utc": source_anchor.get("registered_utc"),
            "frame_sequence_sha256": sha256_json(
                source_anchor["payload"].get("source", {}).get("frame_sequence")
            ),
        }
        if source_registration != expected_source_registration:
            raise SourceAnchorEvidenceError(
                "AV_SOURCE_ANCHOR_MANIFEST_INVALID",
                "paired source anchor registration snapshot is stale",
            )

        source_rows = source_oracle.get("pts_table")
        proxy_rows = proxy_oracle.get("pts_table")
        if not isinstance(source_rows, list) or not isinstance(proxy_rows, list):
            raise AudioEvidenceError("content-anchor oracle PTS tables are missing")
        observation = _load_content_anchor_observations(
            _require_mapping(
                bindings.get("observation_input"), "content-anchor observation binding"
            ),
            scope=scope,
            source_start_frame=expected_timeline["business_frame_start"],
            frame_count=expected_timeline["business_frame_domain"][1],
            source_rows=source_rows,
            proxy_rows=proxy_rows,
            source_audio=source_audio,
            proxy_audio=proxy_audio,
            source_media_path=source_media_path,
            proxy_media_path=proxy_media_path,
            source_oracle_path=source_oracle_path,
            proxy_oracle_path=proxy_oracle_path,
            expected_ffmpeg_path=expected_ffmpeg_path,
            registered_source_anchors=source_anchor["anchors"],
        )
        time_base = expected_timeline["time_base"]
        source_frames = source_audio.get("frames") or {}
        proxy_frames = proxy_audio.get("frames") or {}
        source_sample_rate = (source_audio.get("format") or {}).get("sample_rate")
        proxy_sample_rate = (proxy_audio.get("format") or {}).get("sample_rate")
        evaluated = evaluate_content_anchor_evidence(
            observation["anchors"],
            source_rows,
            proxy_rows,
            business_frame_count=expected_timeline["business_frame_domain"][1],
            source_start_frame=expected_timeline["business_frame_start"],
            duration_ticks=expected_timeline["duration_ticks"],
            time_base_numerator=time_base["numerator"],
            time_base_denominator=time_base["denominator"],
            audio_sample_rate=None,
            source_audio_start_sample=source_frames.get("start_pts"),
            source_audio_end_sample_exclusive=source_frames.get("end_pts_exclusive"),
            proxy_audio_start_sample=proxy_frames.get("start_pts"),
            proxy_audio_end_sample_exclusive=proxy_frames.get("end_pts_exclusive"),
            max_error_seconds=max_error_seconds,
            source_audio_sample_rate=(
                source_sample_rate if isinstance(source_sample_rate, int) else 0
            ),
            proxy_audio_sample_rate=(
                proxy_sample_rate if isinstance(proxy_sample_rate, int) else 0
            ),
        )
        reasons = list(
            dict.fromkeys(
                observation["semantic_reason_codes"] + evaluated["reason_codes"]
            )
        )
        result = dict(evaluated)
        result.update(
            {
                "status": "PASS" if not reasons else "BLOCKED",
                "reason_codes": reasons,
                "binding_checks": {"status": "PASS"},
                "manifest": _file_record(manifest_path),
                "source_anchor_manifest": source_anchor["record"],
                "observation": observation["record"],
            }
        )
        return result
    except SourceAnchorEvidenceError as exc:
        blocked = _content_anchor_blocked(
            exc.reason_code,
            error=str(exc),
            manifest_path=manifest_path,
        )
        # Preserve the historical high-level code while exposing the precise
        # source-first failure for callers that want to distinguish chronology,
        # tampering, and missing real events.
        if "AV_CONTENT_ANCHOR_MANIFEST_INVALID" not in blocked["reason_codes"]:
            blocked["reason_codes"].append("AV_CONTENT_ANCHOR_MANIFEST_INVALID")
        return blocked
    except (AudioEvidenceError, KeyError, OSError, TypeError, ValueError) as exc:
        return _content_anchor_blocked(
            "AV_CONTENT_ANCHOR_MANIFEST_INVALID",
            error=str(exc),
            manifest_path=manifest_path,
        )


def create_content_anchor_manifest(
    subject_manifest_path: Path,
    timeline_manifest_path: Path,
    source_media_path: Path,
    proxy_media_path: Path,
    source_oracle_path: Path,
    proxy_oracle_path: Path,
    observation_path: Path,
    output_path: Path,
    ffmpeg: frame_oracle.FfmpegExecutable | Path,
    *,
    scope: str,
    max_error_seconds: float,
    source_anchor_manifest_path: Path | None = None,
) -> dict[str, Any]:
    if scope not in ("prefix", "full"):
        raise AudioEvidenceError("content-anchor scope must be prefix or full")
    if not _valid_content_anchor_threshold(max_error_seconds):
        raise AudioEvidenceError(
            "content-anchor threshold must be finite, positive, and no greater than 10 ms"
        )
    if source_anchor_manifest_path is None:
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_MANIFEST_NOT_PROVIDED",
            "proxy content-anchor creation requires a source-first registration",
        )
    subject_manifest_path = subject_manifest_path.expanduser().resolve()
    timeline_manifest_path = timeline_manifest_path.expanduser().resolve()
    source_oracle_path = source_oracle_path.expanduser().resolve()
    proxy_oracle_path = proxy_oracle_path.expanduser().resolve()
    source_anchor_manifest_path = source_anchor_manifest_path.expanduser().resolve()
    observation_path = observation_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    if output_path.exists():
        raise FileExistsError("content-anchor manifest is write-once; choose a new path")
    subject = _load_json(subject_manifest_path)
    timeline_manifest = _load_json(timeline_manifest_path)
    timeline_snapshot = _timeline_snapshot(timeline_manifest)
    if timeline_snapshot["scope"] != scope:
        raise AudioEvidenceError("content-anchor scope differs from timeline manifest")
    source_record = _require_mapping(subject.get("source"), "subject source")
    proxy_record = _require_mapping(subject.get("proxy"), "subject proxy")
    source_media_path = _validate_exact_file_binding(
        source_record, "subject source media", source_media_path, require_size=True
    )
    proxy_media_path = _validate_exact_file_binding(
        proxy_record, "subject proxy media", proxy_media_path, require_size=True
    )
    source_oracle_record = _file_record(source_oracle_path)
    proxy_oracle_record = _file_record(proxy_oracle_path)
    _, source_oracle = _load_bound_oracle(
        source_oracle_record,
        "content-anchor source oracle",
        source_media_path,
        str(source_record.get("sha256")),
    )
    _, proxy_oracle = _load_bound_oracle(
        proxy_oracle_record,
        "content-anchor proxy oracle",
        proxy_media_path,
        str(proxy_record.get("sha256")),
    )
    ffmpeg_path = ffmpeg.path if isinstance(ffmpeg, frame_oracle.FfmpegExecutable) else Path(ffmpeg)
    ffmpeg_path = ffmpeg_path.expanduser().resolve()
    source_audio = decode_audio(source_media_path, ffmpeg_path)
    proxy_audio = decode_audio(proxy_media_path, ffmpeg_path)
    source_anchor = _load_bound_source_content_anchor_manifest(
        _file_record(source_anchor_manifest_path),
        scope=scope,
        subject_manifest_path=subject_manifest_path,
        source_media_path=source_media_path,
        source_oracle_path=source_oracle_path,
        source_oracle=source_oracle,
        source_audio=source_audio,
        expected_ffmpeg_path=ffmpeg_path,
    )
    if source_anchor.get("timeline") != timeline_snapshot:
        raise SourceAnchorEvidenceError(
            "AV_SOURCE_ANCHOR_FRAME_DOMAIN_INVALID",
            "subject and timeline manifests declare different frame mappings",
        )
    observation_record = _file_record(observation_path)
    source_rows = source_oracle.get("pts_table")
    proxy_rows = proxy_oracle.get("pts_table")
    if not isinstance(source_rows, list) or not isinstance(proxy_rows, list):
        raise AudioEvidenceError("content-anchor oracle PTS tables are missing")
    observation = _load_content_anchor_observations(
        observation_record,
        scope=scope,
        source_start_frame=timeline_snapshot["business_frame_start"],
        frame_count=timeline_snapshot["business_frame_domain"][1],
        source_rows=source_rows,
        proxy_rows=proxy_rows,
        source_audio=source_audio,
        proxy_audio=proxy_audio,
        source_media_path=source_media_path,
        proxy_media_path=proxy_media_path,
        source_oracle_path=source_oracle_path,
        proxy_oracle_path=proxy_oracle_path,
        expected_ffmpeg_path=ffmpeg_path,
        registered_source_anchors=source_anchor["anchors"],
    )
    time_base = timeline_snapshot["time_base"]
    source_frames = source_audio.get("frames") or {}
    proxy_frames = proxy_audio.get("frames") or {}
    source_sample_rate = (source_audio.get("format") or {}).get("sample_rate")
    proxy_sample_rate = (proxy_audio.get("format") or {}).get("sample_rate")
    evaluated = evaluate_content_anchor_evidence(
        observation["anchors"],
        source_rows,
        proxy_rows,
        business_frame_count=timeline_snapshot["business_frame_domain"][1],
        source_start_frame=timeline_snapshot["business_frame_start"],
        duration_ticks=timeline_snapshot["duration_ticks"],
        time_base_numerator=time_base["numerator"],
        time_base_denominator=time_base["denominator"],
        audio_sample_rate=None,
        source_audio_start_sample=source_frames.get("start_pts"),
        source_audio_end_sample_exclusive=source_frames.get("end_pts_exclusive"),
        proxy_audio_start_sample=proxy_frames.get("start_pts"),
        proxy_audio_end_sample_exclusive=proxy_frames.get("end_pts_exclusive"),
        max_error_seconds=max_error_seconds,
        source_audio_sample_rate=(
            source_sample_rate if isinstance(source_sample_rate, int) else 0
        ),
        proxy_audio_sample_rate=(
            proxy_sample_rate if isinstance(proxy_sample_rate, int) else 0
        ),
    )
    observation_reasons = list(
        dict.fromkeys(
            observation["semantic_reason_codes"] + evaluated["reason_codes"]
        )
    )
    if observation_reasons:
        raise AudioEvidenceError(
            "content-anchor observations are not valid: "
            + ", ".join(observation_reasons)
        )
    manifest = {
        "schema_version": CONTENT_ANCHOR_SCHEMA_VERSION,
        "kind": CONTENT_ANCHOR_MANIFEST_KIND,
        "created_utc": _utc_now(),
        "scope": scope,
        "bindings": {
            "subject_manifest": _file_record(subject_manifest_path),
            "timeline_manifest": _file_record(timeline_manifest_path),
            "source_media": _file_record(source_media_path, include_size=True),
            "proxy_media": _file_record(proxy_media_path, include_size=True),
            "source_oracle": source_oracle_record,
            "proxy_oracle": proxy_oracle_record,
            "source_anchor_manifest": source_anchor["record"],
            "observation_input": observation_record,
        },
        "timeline": timeline_snapshot,
        "thresholds": {
            "max_content_anchor_error_seconds": max_error_seconds,
        },
        "audio_evidence": {
            "source": audio_evidence_binding(source_audio),
            "proxy": audio_evidence_binding(proxy_audio),
        },
        "observation_validation": {
            "status": "PASS",
            "reason_codes": [],
            "observed_zones": evaluated.get("observed_zones", []),
        },
        "source_anchor_registration": {
            "path": str(source_anchor["path"]),
            "sha256": source_anchor["record"]["sha256"],
            "created_utc": source_anchor.get("registered_utc"),
            "frame_sequence_sha256": sha256_json(
                source_anchor["payload"].get("source", {}).get("frame_sequence")
            ),
        },
        "tools": {
            "creator": _file_record(Path(__file__)),
            "verifier": _file_record(Path(__file__)),
            "ffmpeg": ffmpeg_version_record(ffmpeg_path),
        },
        "commands": {
            "source_audio": source_audio.get("command"),
            "proxy_audio": proxy_audio.get("command"),
        },
    }
    write_json_new(output_path, manifest)
    return manifest


bind_content_anchor_manifest = create_content_anchor_manifest


def normalization_offset_summary(
    rows: Sequence[dict[str, Any]],
    *,
    duration_ticks: int,
    numerator: int,
    denominator: int,
) -> dict[str, Any]:
    """Return the identity-offset range forced by a normalized frame clock.

    ``rows`` stays in decoded source order.  This deliberately does not sort or
    deduplicate PTS values: a repeated or inverted source PTS is evidence that
    the source clock cannot be treated as the normalized business clock.
    """
    offsets: list[float] = []
    for index, row in enumerate(rows):
        pts = row.get("pts")
        if not isinstance(pts, int) or isinstance(pts, bool):
            continue
        offsets.append(
            (index * duration_ticks - pts) * numerator / denominator
        )
    return {
        "count": len(offsets),
        "min_seconds": min(offsets) if offsets else None,
        "max_seconds": max(offsets) if offsets else None,
        "span_seconds": max(offsets) - min(offsets) if offsets else None,
    }


def evaluate_source_pts_identity(
    offset_summary: dict[str, Any],
    *,
    max_offset_span_seconds: float,
) -> dict[str, Any]:
    """Classify source-PTS identity without treating it as content sync."""
    if not _valid_positive_threshold(max_offset_span_seconds):
        return {
            "status": "BLOCKED",
            "gate_effect": "validator_configuration_error",
            "reason_codes": ["MAX_IDENTITY_OFFSET_THRESHOLD_INVALID"],
            "offset": offset_summary,
            "threshold_seconds": max_offset_span_seconds,
        }
    count = offset_summary.get("count")
    span = offset_summary.get("span_seconds")
    if not isinstance(count, int) or count <= 0 or not isinstance(span, (int, float)):
        return {
            "status": "BLOCKED",
            "gate_effect": "invalid_evidence",
            "reason_codes": ["SOURCE_PTS_IDENTITY_EVIDENCE_INVALID"],
            "offset": offset_summary,
            "threshold_seconds": max_offset_span_seconds,
        }
    preserved = float(span) <= max_offset_span_seconds
    return {
        "status": "PRESERVED" if preserved else "NOT_PRESERVED",
        "gate_effect": "diagnostic_only",
        "reason_codes": [] if preserved else ["SOURCE_PTS_IDENTITY_NOT_PRESERVED"],
        "offset": offset_summary,
        "threshold_seconds": max_offset_span_seconds,
    }


def evaluate_content_anchor_evidence(
    anchors: Any,
    source_rows: Sequence[dict[str, Any]],
    proxy_rows: Sequence[dict[str, Any]],
    *,
    business_frame_count: int,
    source_start_frame: int = 0,
    duration_ticks: int,
    time_base_numerator: int,
    time_base_denominator: int,
    audio_sample_rate: int,
    source_audio_start_sample: int | None,
    source_audio_end_sample_exclusive: int | None,
    proxy_audio_start_sample: int | None,
    proxy_audio_end_sample_exclusive: int | None,
    max_error_seconds: float = DEFAULT_MAX_AV_ANCHOR_ERROR_SECONDS,
    source_audio_sample_rate: int | None = None,
    proxy_audio_sample_rate: int | None = None,
) -> dict[str, Any]:
    """Validate real content anchors against the proxy sample clock.

    This is intentionally separate from packet timestamp boundary checks.  An
    anchor manifest records an observed source/proxy frame identity and the
    corresponding source/proxy audio sample for four required regions.  Without
    those observations, a numerically plausible boundary cannot claim content
    synchronization.
    """
    reasons: list[str] = []
    if not _valid_content_anchor_threshold(max_error_seconds):
        return {
            "status": "BLOCKED",
            "reason_codes": ["AV_CONTENT_ANCHOR_THRESHOLD_INVALID"],
            "anchors": [],
            "required_zones": list(REQUIRED_CONTENT_ANCHOR_ZONES),
            "observed_zones": [],
            "thresholds": {"max_error_seconds": max_error_seconds},
        }
    if anchors is None:
        return {
            "status": "BLOCKED",
            "reason_codes": ["AV_CONTENT_ANCHORS_NOT_PROVIDED"],
            "anchors": [],
            "required_zones": list(REQUIRED_CONTENT_ANCHOR_ZONES),
            "observed_zones": [],
            "thresholds": {"max_error_seconds": max_error_seconds},
        }
    if not isinstance(anchors, list):
        return {
            "status": "BLOCKED",
            "reason_codes": ["AV_CONTENT_ANCHORS_INVALID"],
            "anchors": [],
            "required_zones": list(REQUIRED_CONTENT_ANCHOR_ZONES),
            "observed_zones": [],
            "thresholds": {"max_error_seconds": max_error_seconds},
        }
    if source_audio_sample_rate is None:
        source_audio_sample_rate = audio_sample_rate
    if proxy_audio_sample_rate is None:
        proxy_audio_sample_rate = audio_sample_rate
    if (
        not isinstance(business_frame_count, int)
        or isinstance(business_frame_count, bool)
        or business_frame_count <= 0
        or not isinstance(source_start_frame, int)
        or isinstance(source_start_frame, bool)
        or source_start_frame < 0
        or not isinstance(duration_ticks, int)
        or isinstance(duration_ticks, bool)
        or duration_ticks <= 0
        or not isinstance(time_base_numerator, int)
        or isinstance(time_base_numerator, bool)
        or time_base_numerator <= 0
        or not isinstance(time_base_denominator, int)
        or isinstance(time_base_denominator, bool)
        or time_base_denominator <= 0
        or not isinstance(source_audio_sample_rate, int)
        or isinstance(source_audio_sample_rate, bool)
        or source_audio_sample_rate <= 0
        or not isinstance(proxy_audio_sample_rate, int)
        or isinstance(proxy_audio_sample_rate, bool)
        or proxy_audio_sample_rate <= 0
    ):
        return {
            "status": "BLOCKED",
            "reason_codes": ["AV_CONTENT_ANCHOR_CLOCK_INVALID"],
            "anchors": [],
            "required_zones": list(REQUIRED_CONTENT_ANCHOR_ZONES),
            "observed_zones": [],
            "thresholds": {"max_error_seconds": max_error_seconds},
        }

    reports: list[dict[str, Any]] = []
    observed_zones: list[str] = []
    source_end_frame = source_start_frame + business_frame_count
    for position, value in enumerate(anchors):
        if not isinstance(value, dict):
            reasons.append("AV_CONTENT_ANCHOR_INVALID")
            continue
        zone = value.get("zone")
        evidence_id = value.get("evidence_id")
        source_index = value.get("source_frame_index")
        proxy_index = value.get("proxy_frame_index")
        source_sample = value.get("source_audio_sample")
        proxy_sample = value.get("proxy_audio_sample")
        if zone not in REQUIRED_CONTENT_ANCHOR_ZONES or zone in observed_zones:
            reasons.append("AV_CONTENT_ANCHOR_ZONE_INVALID")
        elif isinstance(zone, str):
            observed_zones.append(zone)
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            reasons.append("AV_CONTENT_ANCHOR_EVIDENCE_ID_MISSING")
        if any(
            not isinstance(item, int) or isinstance(item, bool) or item < 0
            for item in (source_index, proxy_index, source_sample, proxy_sample)
        ):
            reasons.append("AV_CONTENT_ANCHOR_FIELDS_INVALID")
            continue
        if (
            source_index < source_start_frame
            or source_index >= source_end_frame
            or source_index >= len(source_rows)
        ):
            reasons.append("AV_CONTENT_ANCHOR_SOURCE_FRAME_MISSING")
            continue
        if proxy_index >= len(proxy_rows) or proxy_index >= business_frame_count:
            reasons.append("AV_CONTENT_ANCHOR_PROXY_FRAME_MISSING")
            continue
        expected_proxy_index = source_index - source_start_frame
        if proxy_index != expected_proxy_index:
            reasons.append("AV_CONTENT_ANCHOR_FRAME_MAPPING_INVALID")
            continue
        source_row = source_rows[source_index]
        proxy_row = proxy_rows[proxy_index]
        source_checksum = str(source_row.get("checksum", "")).upper()
        proxy_checksum = str(proxy_row.get("checksum", "")).upper()
        if not source_checksum or source_checksum != proxy_checksum:
            reasons.append("AV_CONTENT_ANCHOR_FRAME_CHECKSUM_MISMATCH")
        if (
            source_audio_start_sample is not None
            and source_sample < source_audio_start_sample
        ) or (
            source_audio_end_sample_exclusive is not None
            and source_sample >= source_audio_end_sample_exclusive
        ):
            reasons.append("AV_CONTENT_ANCHOR_SOURCE_SAMPLE_MISSING")
        if (
            proxy_audio_start_sample is not None
            and proxy_sample < proxy_audio_start_sample
        ) or (
            proxy_audio_end_sample_exclusive is not None
            and proxy_sample >= proxy_audio_end_sample_exclusive
        ):
            reasons.append("AV_CONTENT_ANCHOR_PROXY_SAMPLE_MISSING")
        proxy_pts = proxy_row.get("pts")
        if not isinstance(proxy_pts, int) or isinstance(proxy_pts, bool):
            reasons.append("AV_CONTENT_ANCHOR_PROXY_PTS_MISSING")
            continue
        source_video_seconds = (
            source_index * duration_ticks * time_base_numerator / time_base_denominator
        )
        proxy_video_seconds = proxy_pts * time_base_numerator / time_base_denominator
        source_audio_seconds = source_sample / source_audio_sample_rate
        proxy_audio_seconds = proxy_sample / proxy_audio_sample_rate
        source_offset_seconds = source_audio_seconds - source_video_seconds
        proxy_offset_seconds = proxy_audio_seconds - proxy_video_seconds
        error_seconds = abs(proxy_offset_seconds - source_offset_seconds)
        if error_seconds > max_error_seconds:
            reasons.append("AV_CONTENT_ANCHOR_ERROR_EXCEEDED")
        reports.append(
            {
                "position": position,
                "zone": zone,
                "evidence_id": evidence_id,
                "source_frame_index": source_index,
                "proxy_frame_index": proxy_index,
                "expected_proxy_frame_index": expected_proxy_index,
                "source_audio_sample": source_sample,
                "proxy_audio_sample": proxy_sample,
                "sample_delta": proxy_sample - source_sample,
                "source_checksum": source_checksum,
                "proxy_checksum": proxy_checksum,
                "source_video_order_seconds": source_video_seconds,
                "proxy_video_seconds": proxy_video_seconds,
                "source_audio_seconds": source_audio_seconds,
                "proxy_audio_seconds": proxy_audio_seconds,
                "source_offset_seconds": source_offset_seconds,
                "proxy_offset_seconds": proxy_offset_seconds,
                "error_seconds": error_seconds,
            }
        )
    missing_zones = [zone for zone in REQUIRED_CONTENT_ANCHOR_ZONES if zone not in observed_zones]
    if missing_zones:
        reasons.append("AV_CONTENT_ANCHOR_ZONES_INCOMPLETE")
    reasons = list(dict.fromkeys(reasons))
    return {
        "status": "PASS" if not reasons else "BLOCKED",
        "reason_codes": reasons,
        "anchors": reports,
        "frame_mapping": proxy_verifier._frame_mapping(
            source_start_frame, business_frame_count
        ),
        "required_zones": list(REQUIRED_CONTENT_ANCHOR_ZONES),
        "observed_zones": observed_zones,
        "missing_zones": missing_zones,
        "thresholds": {"max_error_seconds": max_error_seconds},
    }


def evaluate_av_sync(
    video_manifest: dict[str, Any],
    source_audio: dict[str, Any],
    proxy_audio: dict[str, Any],
    *,
    max_anchor_error_seconds: float = DEFAULT_MAX_AV_ANCHOR_ERROR_SECONDS,
    max_identity_offset_span_seconds: float = DEFAULT_MAX_IDENTITY_OFFSET_SPAN_SECONDS,
    allow_no_audio: bool = False,
) -> dict[str, Any]:
    threshold_reasons = _threshold_reason_codes(
        max_anchor_error_seconds, max_identity_offset_span_seconds
    )
    if threshold_reasons:
        return {
            "status": "BLOCKED",
            "reason_codes": threshold_reasons,
            "normalization_offset": {
                "count": 0,
                "min_seconds": None,
                "max_seconds": None,
                "span_seconds": None,
            },
            "source_pts_identity": {
                "status": "BLOCKED",
                "gate_effect": "validator_configuration_error",
                "reason_codes": threshold_reasons,
                "offset": {
                    "count": 0,
                    "min_seconds": None,
                    "max_seconds": None,
                    "span_seconds": None,
                },
                "threshold_seconds": max_identity_offset_span_seconds,
            },
            "anchors": {
                "source_video_start_seconds": None,
                "normalized_video_start_seconds": None,
                "normalized_video_end_seconds": None,
                "source_audio_start_seconds": None,
                "source_audio_end_seconds": None,
                "proxy_audio_start_seconds": None,
                "proxy_audio_end_seconds": None,
                "max_error_seconds": max_anchor_error_seconds,
                "start_error_seconds": None,
                "end_error_seconds": None,
                "max_observed_error_seconds": None,
            },
            "thresholds": {
                "max_anchor_error_seconds": max_anchor_error_seconds,
                "max_identity_offset_span_seconds": max_identity_offset_span_seconds,
            },
        }
    source_present = bool(source_audio.get("stream_present"))
    proxy_present = bool(proxy_audio.get("stream_present"))
    audio_preservation = evaluate_audio_preservation(
        source_audio, proxy_audio, allow_no_audio=allow_no_audio
    )
    reasons = list(audio_preservation["reason_codes"])

    rows, timeline = _source_video_rows(video_manifest)
    numerator = _require_int(timeline.get("time_base", {}).get("numerator"), "video time base numerator", minimum=1)
    denominator = _require_int(timeline.get("time_base", {}).get("denominator"), "video time base denominator", minimum=1)
    duration_ticks = _require_int(timeline.get("duration_ticks"), "video duration ticks", minimum=1)
    normalized_start = timeline.get("business_pts_start")
    normalized_end = timeline.get("business_pts_end_exclusive")
    if normalized_start != 0 or not isinstance(normalized_end, int):
        raise AudioEvidenceError("video normalized business domain is invalid")
    normalized_start_seconds = normalized_start * numerator / denominator
    normalized_end_seconds = normalized_end * numerator / denominator
    source_start_seconds = rows[0].get("pts", 0) * numerator / denominator
    if any(
        not isinstance(row.get("pts"), int) or isinstance(row.get("pts"), bool)
        for row in rows
    ):
        reasons.append("SOURCE_VIDEO_PTS_MISSING")
    offset_summary = normalization_offset_summary(
        rows,
        duration_ticks=duration_ticks,
        numerator=numerator,
        denominator=denominator,
    )
    source_pts_identity = evaluate_source_pts_identity(
        offset_summary,
        max_offset_span_seconds=max_identity_offset_span_seconds,
    )

    anchors: dict[str, Any] = {
        "source_video_start_seconds": source_start_seconds,
        "normalized_video_start_seconds": normalized_start_seconds,
        "normalized_video_end_seconds": normalized_end_seconds,
        "source_audio_start_seconds": (source_audio.get("frames") or {}).get("start_time"),
        "source_audio_end_seconds": (source_audio.get("frames") or {}).get("end_time"),
        "proxy_audio_start_seconds": (proxy_audio.get("frames") or {}).get("start_time"),
        "proxy_audio_end_seconds": (proxy_audio.get("frames") or {}).get("end_time"),
        "max_error_seconds": max_anchor_error_seconds,
    }
    if source_present and proxy_present:
        source_start_error = abs(
            (anchors["source_audio_start_seconds"] - source_start_seconds)
            - (anchors["proxy_audio_start_seconds"] - normalized_start_seconds)
        )
        source_end_error = abs(
            (anchors["source_audio_end_seconds"] - (max(row["pts"] + row.get("duration", 0) for row in rows) * numerator / denominator))
            - (anchors["proxy_audio_end_seconds"] - normalized_end_seconds)
        )
        anchors["start_error_seconds"] = source_start_error
        anchors["end_error_seconds"] = source_end_error
        anchors["max_observed_error_seconds"] = max(source_start_error, source_end_error)
        boundary_changed = (
            source_start_error > max_anchor_error_seconds
            or source_end_error > max_anchor_error_seconds
        )
        anchors["source_pts_relationship_status"] = (
            "CHANGED" if boundary_changed else "PRESERVED"
        )
        anchors["gate_effect"] = "diagnostic_only"
        anchors["reason_codes"] = (
            ["SOURCE_PTS_RELATIVE_AV_BOUNDARY_CHANGED"] if boundary_changed else []
        )
    else:
        anchors["start_error_seconds"] = None
        anchors["end_error_seconds"] = None
        anchors["max_observed_error_seconds"] = None
        anchors["source_pts_relationship_status"] = "NOT_EVALUATED"
        anchors["gate_effect"] = "diagnostic_only"
        anchors["reason_codes"] = []

    reasons = list(dict.fromkeys(reasons))
    status = audio_preservation["status"] if not reasons else "BLOCKED"
    return {
        "status": status,
        "reason_codes": reasons,
        "audio_preservation": audio_preservation,
        "normalization_offset": offset_summary,
        "source_pts_identity": source_pts_identity,
        "anchors": anchors,
        "thresholds": {
            "max_anchor_error_seconds": max_anchor_error_seconds,
            "max_identity_offset_span_seconds": max_identity_offset_span_seconds,
        },
    }


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


def verify_audio_av(
    video_manifest_path: Path,
    video_report_path: Path,
    run_manifest_path: Path,
    output_path: Path,
    ffmpeg: frame_oracle.FfmpegExecutable,
    *,
    max_anchor_error_seconds: float,
    max_identity_offset_span_seconds: float,
    allow_no_audio: bool,
    content_anchor_manifest_path: Path | None = None,
) -> dict[str, Any]:
    video_manifest_path = video_manifest_path.expanduser().resolve()
    video_report_path = video_report_path.expanduser().resolve()
    run_manifest_path = run_manifest_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    if run_manifest_path.exists() or output_path.exists():
        raise FileExistsError("audio/A/V evidence is write-once; choose new paths")
    video_manifest = _load_json(video_manifest_path)
    video_report = _load_json(video_report_path)
    threshold_reasons = _threshold_reason_codes(
        max_anchor_error_seconds, max_identity_offset_span_seconds
    )
    if threshold_reasons:
        raise AudioEvidenceError(
            "invalid A/V thresholds: " + ", ".join(threshold_reasons)
        )
    if video_manifest.get("kind") != "mpv_phase0_pts_normalized_proxy":
        raise AudioEvidenceError("video manifest kind is invalid")
    manifest_record = _require_mapping(video_report.get("manifest"), "video report manifest")
    if not _same_path(Path(str(manifest_record.get("path", ""))), video_manifest_path):
        raise AudioEvidenceError("video report is bound to another manifest")
    if manifest_record.get("sha256", "").lower() != sha256_file(video_manifest_path).lower():
        raise AudioEvidenceError("video report manifest SHA-256 is stale")
    source_record = _require_mapping(video_manifest.get("source"), "video manifest source")
    proxy_record = _require_mapping(video_manifest.get("proxy"), "video manifest proxy")
    source_path = _validate_bound_file(source_record, "source video")
    proxy_path = _validate_bound_file(proxy_record, "proxy video")
    video_evidence = _validate_video_evidence(video_manifest, video_report)
    source_audio = decode_audio(source_path, ffmpeg.path)
    proxy_audio = decode_audio(proxy_path, ffmpeg.path)
    audio_preservation = evaluate_audio_preservation(
        source_audio, proxy_audio, allow_no_audio=allow_no_audio
    )
    audio_status = audio_preservation["status"]
    audio_reasons = list(audio_preservation["reason_codes"])
    if video_evidence["status"] == "PASS":
        timestamp_evaluation = evaluate_av_sync(
            video_manifest,
            source_audio,
            proxy_audio,
            max_anchor_error_seconds=max_anchor_error_seconds,
            max_identity_offset_span_seconds=max_identity_offset_span_seconds,
            allow_no_audio=allow_no_audio,
        )
        source_oracle_path = Path(video_evidence["source_oracle"]["path"])
        proxy_oracle_path = Path(video_evidence["decoded_proxy_oracle"]["path"])
        content_anchors = evaluate_bound_content_anchor_manifest(
            content_anchor_manifest_path,
            scope=str(_require_mapping(video_manifest.get("source"), "video manifest source").get("scope")),
            subject_manifest_path=video_manifest_path,
            timeline_manifest_path=video_manifest_path,
            source_media_path=source_path,
            proxy_media_path=proxy_path,
            source_oracle_path=source_oracle_path,
            proxy_oracle_path=proxy_oracle_path,
            source_oracle=_load_json(source_oracle_path),
            proxy_oracle=_load_json(proxy_oracle_path),
            source_audio=source_audio,
            proxy_audio=proxy_audio,
            max_error_seconds=max_anchor_error_seconds,
            allow_no_audio=allow_no_audio,
            expected_ffmpeg_path=ffmpeg.path,
        )
        timestamp_gate_reasons = [
            reason
            for reason in timestamp_evaluation.get("reason_codes", [])
            if reason not in audio_reasons
        ]
        av_reasons = list(
            dict.fromkeys(
                timestamp_gate_reasons + content_anchors.get("reason_codes", [])
            )
        )
        source_identity = timestamp_evaluation.get("source_pts_identity") or {}
        timestamp_boundaries = timestamp_evaluation.get("anchors") or {}
        diagnostic_codes = list(
            dict.fromkeys(
                source_identity.get("reason_codes", [])
                + timestamp_boundaries.get("reason_codes", [])
            )
        )
        av_sync = {
            "status": (
                "PASS"
                if not timestamp_gate_reasons
                and content_anchors.get("status") in ("PASS", "NOT_APPLICABLE_PASS")
                else "BLOCKED"
            ),
            "reason_codes": av_reasons,
            "diagnostic_codes": diagnostic_codes,
            "source_pts_identity": source_identity,
            "timestamp_boundaries": timestamp_boundaries,
            "content_anchors": content_anchors,
            "normalization_offset": timestamp_evaluation.get("normalization_offset"),
            "thresholds": timestamp_evaluation.get("thresholds"),
        }
    else:
        av_sync = {
            "status": "BLOCKED",
            "reason_codes": ["VIDEO_EVIDENCE_INVALID"],
            "diagnostic_codes": [],
            "normalization_offset": {
                "count": 0,
                "min_seconds": None,
                "max_seconds": None,
                "span_seconds": None,
            },
            "source_pts_identity": {},
            "timestamp_boundaries": {},
            "content_anchors": _content_anchor_blocked("VIDEO_EVIDENCE_INVALID"),
            "thresholds": {
                "max_anchor_error_seconds": max_anchor_error_seconds,
                "max_identity_offset_span_seconds": max_identity_offset_span_seconds,
            },
        }
    video_status = video_evidence["status"]
    overall_reasons: list[str] = []
    if video_status != "PASS":
        overall_reasons.extend(video_evidence.get("reason_codes", []))
    overall_reasons.extend(audio_reasons)
    overall_reasons.extend(av_sync["reason_codes"])
    scope = _require_mapping(video_manifest.get("source"), "video manifest source").get("scope")
    if scope != "full":
        overall_reasons.append("SOURCE_SCOPE_NOT_FULL")
    overall_reasons = list(dict.fromkeys(overall_reasons))
    overall_status = "PASS" if not overall_reasons and scope == "full" else "BLOCKED"
    run_manifest = {
        "schema_version": SCHEMA_VERSION,
        "kind": RUN_KIND,
        "created_utc": _utc_now(),
        "status": overall_status,
        "scope": scope,
        "video_proxy_manifest": {"path": str(video_manifest_path), "sha256": sha256_file(video_manifest_path)},
        "video_report": {"path": str(video_report_path), "sha256": sha256_file(video_report_path)},
        "source": {"path": str(source_path), "sha256": sha256_file(source_path), "size": source_path.stat().st_size},
        "proxy": {"path": str(proxy_path), "sha256": sha256_file(proxy_path), "size": proxy_path.stat().st_size},
        "video_evidence": {
            key: value
            for key, value in video_evidence.items()
            if key not in {"source_path", "proxy_path", "recomputed"}
        },
        "stream_policy": {
            "source_audio_stream": "0:a:0",
            "proxy_audio_stream": "0:a:0",
            "route": "packet_copy_expected",
            "exact_pcm_match_required": True,
            "allow_no_audio": allow_no_audio,
        },
        "thresholds": {
            "max_av_anchor_error_seconds": max_anchor_error_seconds,
            "max_identity_offset_span_seconds": max_identity_offset_span_seconds,
            "decoded_continuity_tolerance_samples": 0,
        },
        "tools": {
            "ffmpeg": ffmpeg_version_record(ffmpeg.path),
            "verify_proxy_audio_av": {"path": str(Path(__file__).resolve()), "sha256": sha256_file(Path(__file__))},
            "python_version": sys.version,
        },
        "commands": {"source_audio": source_audio["command"], "proxy_audio": proxy_audio["command"]},
        "source_audio": _stream_summary(source_audio),
        "proxy_audio": _stream_summary(proxy_audio),
        "audio_timeline": {"status": audio_status, "reason_codes": audio_reasons},
        "audio_preservation": audio_preservation,
        "av_sync": av_sync,
        "content_anchor_evidence": {
            "manifest": av_sync.get("content_anchors", {}).get("manifest"),
            "observation": av_sync.get("content_anchors", {}).get("observation"),
        },
    }
    write_json_new(run_manifest_path, run_manifest)
    report = {
        "schema_version": SCHEMA_VERSION,
        "kind": REPORT_KIND,
        "created_utc": _utc_now(),
        "status": overall_status,
        "reason_codes": overall_reasons,
        "proxy_ready_for_gate": overall_status == "PASS",
        "scope": scope,
        "video_validation": video_evidence,
        "audio_timeline": {
            "status": audio_status,
            "reason_codes": audio_reasons,
            "source": source_audio,
            "proxy": proxy_audio,
        },
        "audio_preservation": audio_preservation,
        "av_sync": av_sync,
        "source_pts_identity": av_sync.get("source_pts_identity", {}),
        "diagnostic_codes": av_sync.get("diagnostic_codes", []),
        "run_manifest": {"path": str(run_manifest_path), "sha256": sha256_file(run_manifest_path)},
        "exit_code": EXIT_PASS if overall_status == "PASS" else EXIT_BLOCKED,
    }
    write_json_new(output_path, report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify audio timeline and A/V sync for a Phase 0 proxy")
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("video_manifest", type=Path)
    verify.add_argument("--video-report", type=Path, required=True)
    verify.add_argument("--run-manifest", type=Path, required=True)
    verify.add_argument("--output", type=Path, required=True)
    verify.add_argument("--ffmpeg", type=Path, default=None)
    verify.add_argument("--max-av-anchor-error-seconds", type=float, default=DEFAULT_MAX_AV_ANCHOR_ERROR_SECONDS)
    verify.add_argument("--max-identity-offset-span-seconds", type=float, default=DEFAULT_MAX_IDENTITY_OFFSET_SPAN_SECONDS)
    verify.add_argument("--allow-no-audio", action="store_true")
    verify.add_argument("--content-anchor-manifest", type=Path)

    register_source = subparsers.add_parser(
        "register-source-anchors",
        aliases=("register-source-content-anchors", "register-source-anchor-manifest"),
    )
    register_source.add_argument("source_media", type=Path)
    register_source.add_argument("--source-oracle", type=Path, required=True)
    register_source.add_argument("--observations", type=Path, required=True)
    register_source.add_argument("--output", type=Path, required=True)
    register_source.add_argument("--scope", choices=("prefix", "full"), required=True)
    register_source.add_argument("--source-start-frame", type=int, default=0)
    register_source.add_argument("--business-frame-count", type=int)
    register_source.add_argument("--ffmpeg", type=Path, default=None)

    bind = subparsers.add_parser("bind-content-anchors")
    bind.add_argument("subject_manifest", type=Path)
    bind.add_argument("--timeline-manifest", type=Path, required=True)
    bind.add_argument("--source-media", type=Path, required=True)
    bind.add_argument("--proxy-media", type=Path, required=True)
    bind.add_argument("--source-oracle", type=Path, required=True)
    bind.add_argument("--proxy-oracle", type=Path, required=True)
    bind.add_argument("--source-anchor-manifest", type=Path, required=True)
    bind.add_argument("--observations", type=Path, required=True)
    bind.add_argument("--output", type=Path, required=True)
    bind.add_argument("--scope", choices=("prefix", "full"), required=True)
    bind.add_argument("--ffmpeg", type=Path, default=None)
    bind.add_argument(
        "--max-content-anchor-error-seconds",
        type=float,
        default=DEFAULT_MAX_AV_ANCHOR_ERROR_SECONDS,
    )
    bundle = subparsers.add_parser("bind-split-window-evidence")
    bundle.add_argument("source_media", type=Path)
    bundle.add_argument("--av-event-source-oracle", type=Path, required=True)
    bundle.add_argument("--pts-conflict-source", type=Path)
    bundle.add_argument("--pts-conflict-evidence", type=Path, required=True)
    bundle.add_argument("--av-event-evidence", type=Path, required=True)
    bundle.add_argument("--output", type=Path, required=True)
    verify_bundle = subparsers.add_parser("verify-split-window-evidence")
    verify_bundle.add_argument("bundle", type=Path)
    verify_bundle.add_argument("--source-media", type=Path, required=True)
    verify_bundle.add_argument("--av-event-source-oracle", type=Path, required=True)
    verify_bundle.add_argument("--pts-conflict-source", type=Path)
    conflict = subparsers.add_parser("build-pts-conflict-evidence")
    conflict.add_argument("source_media", type=Path)
    conflict.add_argument("--reconciliation", type=Path, required=True)
    conflict.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "verify":
        threshold_reasons = _threshold_reason_codes(
            args.max_av_anchor_error_seconds,
            args.max_identity_offset_span_seconds,
        )
        if threshold_reasons:
            parser.error("A/V thresholds must be finite positive numbers")
    elif args.command == "bind-content-anchors" and not _valid_content_anchor_threshold(
        args.max_content_anchor_error_seconds
    ):
        parser.error(
            "content-anchor threshold must be finite, positive, and no greater than 10 ms"
        )
    elif args.command in {
            "register-source-anchors",
            "register-source-content-anchors",
            "register-source-anchor-manifest",
        }:
        if args.source_start_frame < 0:
            parser.error("--source-start-frame must be at least 0")
        if args.business_frame_count is not None and args.business_frame_count < 1:
            parser.error("--business-frame-count must be at least 1")
        if args.source_start_frame != 0 and args.business_frame_count is None:
            parser.error(
                "--business-frame-count is required when --source-start-frame is non-zero"
            )
    try:
        if args.command == "build-pts-conflict-evidence":
            evidence = create_pts_conflict_evidence_from_reconciliation(
                args.source_media,
                args.reconciliation,
                args.output,
            )
            print(
                json.dumps(
                    {
                        "status": "BLOCKED",
                        "kind": evidence["kind"],
                        "time_authority": evidence["time_authority"],
                        "conflict_indices": evidence["conflict_indices"],
                        "evidence": str(args.output.expanduser().resolve()),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return EXIT_BLOCKED
        if args.command == "bind-split-window-evidence":
            manifest = create_split_window_evidence_bundle(
                args.source_media,
                args.av_event_source_oracle,
                args.pts_conflict_evidence,
                args.av_event_evidence,
                args.output,
                pts_conflict_source_path=args.pts_conflict_source,
            )
            print(
                json.dumps(
                    {
                        "status": "BOUND",
                        "kind": manifest["kind"],
                        "evidence_role": manifest["evidence_role"],
                        "ready_for_source_anchor": manifest[
                            "ready_for_source_anchor"
                        ],
                        "manifest": str(args.output.expanduser().resolve()),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return EXIT_PASS
        if args.command == "verify-split-window-evidence":
            result = evaluate_split_window_evidence_bundle(
                args.bundle,
                source_media_path=args.source_media,
                av_event_source_oracle_path=args.av_event_source_oracle,
                pts_conflict_source_path=args.pts_conflict_source,
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return EXIT_PASS if result["status"] == "PASS" else EXIT_BLOCKED
        ffmpeg = frame_oracle.resolve_ffmpeg(args.ffmpeg)
        if args.command in {
            "register-source-anchors",
            "register-source-content-anchors",
            "register-source-anchor-manifest",
        }:
            manifest = create_source_content_anchor_manifest(
                args.source_media,
                args.source_oracle,
                args.observations,
                args.output,
                ffmpeg,
                scope=args.scope,
                business_frame_count=args.business_frame_count,
                source_start_frame=args.source_start_frame,
            )
            print(
                json.dumps(
                    {
                        "status": "REGISTERED",
                        "kind": manifest["kind"],
                        "scope": manifest["scope"],
                        "source_frame_domain": manifest["source"][
                            "source_frame_domain"
                        ],
                        "manifest": str(args.output.expanduser().resolve()),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return EXIT_PASS
        if args.command == "bind-content-anchors":
            manifest = create_content_anchor_manifest(
                args.subject_manifest,
                args.timeline_manifest,
                args.source_media,
                args.proxy_media,
                args.source_oracle,
                args.proxy_oracle,
                args.observations,
                args.output,
                ffmpeg,
                scope=args.scope,
                max_error_seconds=args.max_content_anchor_error_seconds,
                source_anchor_manifest_path=args.source_anchor_manifest,
            )
            print(
                json.dumps(
                    {
                        "status": "BOUND",
                        "kind": manifest["kind"],
                        "scope": manifest["scope"],
                        "manifest": str(args.output.expanduser().resolve()),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return EXIT_PASS
        report = verify_audio_av(
            args.video_manifest,
            args.video_report,
            args.run_manifest,
            args.output,
            ffmpeg,
            max_anchor_error_seconds=args.max_av_anchor_error_seconds,
            max_identity_offset_span_seconds=args.max_identity_offset_span_seconds,
            allow_no_audio=args.allow_no_audio,
            content_anchor_manifest_path=args.content_anchor_manifest,
        )
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "audio_status": report["audio_timeline"]["status"],
                    "av_sync_status": report["av_sync"]["status"],
                    "reason_codes": report["reason_codes"],
                    "report": str(args.output.expanduser().resolve()),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return report["exit_code"]
    except SourceAnchorEvidenceError as exc:
        print(f"{exc.reason_code}: {exc}", file=sys.stderr)
        return EXIT_BLOCKED
    except (AudioEvidenceError, FileExistsError, FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
