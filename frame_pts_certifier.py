"""Produce immutable, source-bound decoded-frame PTS certifications."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Literal, Mapping
import uuid

import media_info


EVIDENCE_SCHEMA_VERSION = 2
EVIDENCE_KIND = "production_frame_pts_certification"
CertificationStatus = Literal["PASS", "PASS_WITH_HEAD_ANOMALIES", "BLOCKED"]
Checkpoint = Callable[[], Any]
OracleProbe = Callable[..., tuple[dict[str, Any], int]]
PublishCommit = Callable[..., Any]

ADJUDICATION_POLICY_VERSION = "head-restricted-2026-08-16"
ADJUDICABLE_ORACLE_REASONS = frozenset({"PTS_DUPLICATE", "PTS_NON_MONOTONIC"})


@dataclass(frozen=True, slots=True)
class FramePtsCertificationOutcome:
    """Result of one cache-first, full-source PTS certification attempt."""

    status: CertificationStatus
    media_info: media_info.MediaInfo
    evidence_path: Path
    reason_codes: tuple[str, ...]
    generated: bool

    @property
    def certified(self) -> bool:
        return (
            self.status
            in ("PASS", media_info.ADJUDICATED_EVIDENCE_STATUS)
            and self.media_info.complete_for_export
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_json_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _require_media_prerequisites(value: media_info.MediaInfo) -> None:
    if not isinstance(value, media_info.MediaInfo):
        raise media_info.MediaInfoError(
            "MEDIA_INFO_REQUIRED",
            "frame PTS certification requires a MediaInfo snapshot",
        )
    value.assert_source_current()
    if not value.video_streams or value.video_streams[0].time_base is None:
        raise media_info.MediaInfoError(
            "FRAME_PTS_TIME_BASE_MISSING",
            "frame PTS certification requires the primary video time_base",
        )
    if value.ffmpeg is None or not value.ffmpeg_verified:
        raise media_info.MediaInfoError(
            "FFMPEG_TOOL_MISMATCH",
            "frame PTS certification requires the verified MediaInfo ffmpeg tool",
        )
    if not value.ffmpeg.is_current():
        raise media_info.MediaInfoError(
            "TOOL_CHANGED_AFTER_PROBE",
            "registered ffmpeg executable changed after MediaInfo probing",
            details={"path": str(value.ffmpeg.path)},
        )


def default_evidence_path(
    value: media_info.MediaInfo,
    *,
    cache_root: str | os.PathLike[str] | None = None,
) -> Path:
    _require_media_prerequisites(value)
    assert value.ffmpeg is not None
    producer_sha256 = media_info._sha256_file(Path(__file__).resolve())
    root = (
        Path(cache_root).expanduser().resolve()
        if cache_root is not None
        else Path(__file__).resolve().parent / ".cache" / "media_info" / "frame_pts"
    )
    return (
        root
        / value.source_sha256
        / (
            f"v{EVIDENCE_SCHEMA_VERSION}-{producer_sha256[:16]}-"
            f"{value.ffmpeg.sha256[:16]}.json"
        )
    )


def _tool_record(tool: media_info.ToolInfo) -> dict[str, Any]:
    return tool.as_dict()


def _same_path(left: str | os.PathLike[str], right: Path) -> bool:
    return Path(left).expanduser().resolve() == right.resolve()


def _validate_oracle_identity(
    report: Mapping[str, Any],
    value: media_info.MediaInfo,
) -> None:
    if report.get("schema_version") != 1 or report.get("kind") != "mpv_phase0_frame_pts_oracle":
        raise media_info.MediaInfoError(
            "FRAME_PTS_ORACLE_INVALID",
            "frame PTS oracle schema or kind is not recognized",
        )
    video = report.get("video")
    if not isinstance(video, Mapping):
        raise media_info.MediaInfoError(
            "FRAME_PTS_ORACLE_INVALID",
            "frame PTS oracle has no video identity",
        )
    if (
        not isinstance(video.get("path"), str)
        or not _same_path(video["path"], value.source_path)
        or str(video.get("sha256", "")).lower() != value.source_sha256.lower()
        or video.get("size") != value.source_size
        or video.get("mtime_ns") != value.source_mtime_ns
    ):
        raise media_info.MediaInfoError(
            "FRAME_PTS_ORACLE_SOURCE_MISMATCH",
            "frame PTS oracle belongs to a different source",
        )
    ffmpeg = report.get("ffmpeg")
    if not isinstance(ffmpeg, Mapping) or value.ffmpeg is None:
        raise media_info.MediaInfoError(
            "FRAME_PTS_ORACLE_INVALID",
            "frame PTS oracle has no FFmpeg execution record",
        )
    command = ffmpeg.get("command")
    if (
        not isinstance(ffmpeg.get("path"), str)
        or not _same_path(ffmpeg["path"], value.ffmpeg.path)
        or ffmpeg.get("resolution_source") != "media_info"
        or str(ffmpeg.get("sha256", "")).lower() != value.ffmpeg.sha256.lower()
        or ffmpeg.get("version_line") != value.ffmpeg.version_line
        or ffmpeg.get("verified") is not True
        or isinstance(ffmpeg.get("returncode"), bool)
        or not isinstance(ffmpeg.get("returncode"), int)
        or not isinstance(command, list)
        or not command
        or any(not isinstance(item, str) for item in command)
    ):
        raise media_info.MediaInfoError(
            "FRAME_PTS_ORACLE_TOOL_MISMATCH",
            "frame PTS oracle tool identity is not the registered MediaInfo FFmpeg",
        )

    assessment = report.get("pts_conflict_assessment")
    if not isinstance(assessment, Mapping) or assessment.get("scan_scope") != "complete":
        raise media_info.MediaInfoError(
            "FRAME_PTS_ORACLE_SCOPE_INVALID",
            "frame PTS oracle is not a complete full-source scan",
        )

    # Match the complete production command exactly. Checking only for the
    # absence of ``-frames:v`` allows a truncated command to masquerade as a
    # full scan, so every token, including the source path and null sink, is
    # part of the identity contract. Thread count is allowed to vary, but it
    # must be a single positive integer in the canonical command shape.
    thread_positions = [index for index, item in enumerate(command) if item == "-threads"]
    if len(thread_positions) != 1:
        raise media_info.MediaInfoError(
            "FRAME_PTS_ORACLE_SCOPE_INVALID",
            "frame PTS oracle command must contain exactly one -threads value",
        )
    thread_index = thread_positions[0]
    if thread_index + 1 >= len(command):
        raise media_info.MediaInfoError(
            "FRAME_PTS_ORACLE_SCOPE_INVALID",
            "frame PTS oracle command has a truncated -threads option",
        )
    try:
        threads = int(command[thread_index + 1])
    except (TypeError, ValueError) as exc:
        raise media_info.MediaInfoError(
            "FRAME_PTS_ORACLE_SCOPE_INVALID",
            "frame PTS oracle command has an invalid -threads value",
        ) from exc
    if threads < 1:
        raise media_info.MediaInfoError(
            "FRAME_PTS_ORACLE_SCOPE_INVALID",
            "frame PTS oracle command requires positive decode threads",
        )
    try:
        from scripts import verify_mpv_frames

        expected_command = verify_mpv_frames.build_ffmpeg_command(
            value.ffmpeg.path,
            value.source_path,
            threads=threads,
            max_frames=None,
        )
    except (ImportError, OSError, TypeError, ValueError) as exc:
        raise media_info.MediaInfoError(
            "FRAME_PTS_ORACLE_INVALID",
            "could not construct the canonical frame PTS oracle command",
        ) from exc
    if command != expected_command:
        raise media_info.MediaInfoError(
            "FRAME_PTS_ORACLE_SCOPE_INVALID",
            "frame PTS oracle command is truncated or not a complete source decode",
        )


def _validate_authoritative_rows(rows: Any) -> list[dict[str, int]]:
    """Structural row validation: contiguous n, integer ticks, duration > 0."""
    if not isinstance(rows, list) or not rows:
        raise media_info.MediaInfoError(
            "FRAME_PTS_EVIDENCE_INCOMPLETE",
            "frame PTS oracle has no complete pts_table",
        )
    normalized: list[dict[str, int]] = []
    for expected, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise media_info.MediaInfoError(
                "FRAME_PTS_EVIDENCE_INVALID",
                "frame PTS table row must be an object",
                details={"row": expected},
            )
        number = raw.get("n")
        pts = raw.get("pts")
        duration = raw.get("duration")
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number != expected
            or isinstance(pts, bool)
            or not isinstance(pts, int)
            or isinstance(duration, bool)
            or not isinstance(duration, int)
            or duration <= 0
        ):
            raise media_info.MediaInfoError(
                "FRAME_PTS_EVIDENCE_INVALID",
                "frame PTS rows require contiguous integer n/pts and positive duration",
                details={"row": expected},
            )
        normalized.append({"n": number, "pts": pts, "duration": duration})
    return normalized


def _classify_frame_pts(rows: list[dict[str, int]]) -> Literal["cfr", "vfr"]:
    duration = rows[0]["duration"]
    constant_duration = all(row["duration"] == duration for row in rows)
    contiguous_ticks = all(
        rows[index + 1]["pts"] - row["pts"] == row["duration"]
        for index, row in enumerate(rows[:-1])
    )
    return "cfr" if constant_duration and contiguous_ticks else "vfr"


def _oracle_time_base(report: Mapping[str, Any]) -> dict[str, Any] | None:
    showinfo = report.get("showinfo")
    value = showinfo.get("time_base") if isinstance(showinfo, Mapping) else None
    if not isinstance(value, Mapping):
        return None
    numerator = value.get("numerator")
    denominator = value.get("denominator")
    if (
        isinstance(numerator, bool)
        or not isinstance(numerator, int)
        or numerator <= 0
        or isinstance(denominator, bool)
        or not isinstance(denominator, int)
        or denominator <= 0
    ):
        return None
    return {
        "numerator": numerator,
        "denominator": denominator,
        "text": f"{numerator}/{denominator}",
    }


def _build_evidence(
    report: Mapping[str, Any],
    exit_code: int,
    value: media_info.MediaInfo,
    *,
    retry_of: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    _validate_oracle_identity(report, value)
    assert value.ffmpeg is not None
    primary_time_base = value.video_streams[0].time_base
    assert primary_time_base is not None

    oracle_reasons = report.get("reason_codes")
    if not isinstance(oracle_reasons, list) or any(
        not isinstance(reason, str) or not reason for reason in oracle_reasons
    ):
        raise media_info.MediaInfoError(
            "FRAME_PTS_ORACLE_INVALID",
            "frame PTS oracle reason_codes must be a list of strings",
        )
    reason_codes = list(dict.fromkeys(oracle_reasons))
    internal_reasons: list[str] = []

    def _internal(code: str) -> None:
        internal_reasons.append(code)
        reason_codes.append(code)

    if report.get("status") == "BLOCKED" and not reason_codes:
        _internal("FRAME_PTS_ORACLE_BLOCKED")
    assessment = report.get("pts_conflict_assessment")
    if not isinstance(assessment, Mapping) or assessment.get("scan_scope") != "complete":
        _internal("FRAME_PTS_SCOPE_NOT_FULL")

    rows_value = report.get("pts_table")
    normalized_rows: list[dict[str, int]] | None = None
    try:
        normalized_rows = _validate_authoritative_rows(rows_value)
    except media_info.MediaInfoError as exc:
        _internal(exc.code)

    showinfo = report.get("showinfo")
    parsed_frames = showinfo.get("parsed_frames") if isinstance(showinfo, Mapping) else None
    ffmpeg_record = report.get("ffmpeg")
    if isinstance(ffmpeg_record, Mapping) and ffmpeg_record.get("returncode") != 0:
        _internal("FFMPEG_DECODE_FAILED")
    observed_time_bases = (
        showinfo.get("observed_time_bases")
        if isinstance(showinfo, Mapping)
        else None
    )
    if isinstance(observed_time_bases, list):
        if any(not isinstance(item, str) for item in observed_time_bases):
            _internal("FRAME_PTS_TIME_BASE_INVALID")
        elif len(set(observed_time_bases)) > 1:
            _internal("FRAME_PTS_TIME_BASE_MULTIPLE")
    if normalized_rows is None or parsed_frames != len(normalized_rows):
        _internal("FRAME_PTS_FRAME_COUNT_MISMATCH")

    time_base = _oracle_time_base(report)
    expected_time_base = {
        "numerator": primary_time_base.numerator,
        "denominator": primary_time_base.denominator,
        "text": f"{primary_time_base.numerator}/{primary_time_base.denominator}",
    }
    if time_base != expected_time_base:
        _internal("FRAME_PTS_TIME_BASE_MISMATCH")

    if report.get("status") not in {"PASS", "BLOCKED"}:
        raise media_info.MediaInfoError(
            "FRAME_PTS_ORACLE_FAILED",
            "frame PTS oracle did not complete as PASS or BLOCKED",
            details={"status": report.get("status"), "exit_code": exit_code},
        )
    if report.get("status") == "PASS" and exit_code != 0:
        raise media_info.MediaInfoError(
            "FRAME_PTS_ORACLE_FAILED",
            "frame PTS oracle reported PASS with a non-zero exit code",
            details={"exit_code": exit_code},
        )

    oracle_pass = (
        exit_code == 0
        and report.get("status") == "PASS"
        and report.get("authoritative_frame_timeline") is True
        and not reason_codes
    )

    # B' targeted adjudication (2026-08-16): a decode that succeeded and is
    # clean in every other respect may carry duplicate/non-monotonic PTS only
    # inside the registered head window.  Anything beyond it keeps BLOCKED.
    adjudication: dict[str, Any] | None = None
    if not oracle_pass and normalized_rows is not None:
        try:
            facts = media_info.head_anomaly_facts(
                ((row["n"], row["pts"]) for row in normalized_rows),
                head_frame_limit=media_info.HEAD_ANOMALY_FRAME_LIMIT,
            )
        except media_info.MediaInfoError:
            facts = None
            _internal("FRAME_PTS_EVIDENCE_NON_MONOTONIC")
        if (
            facts is not None
            and report.get("status") == "BLOCKED"
            and oracle_reasons
            and set(oracle_reasons) <= ADJUDICABLE_ORACLE_REASONS
            and isinstance(ffmpeg_record, Mapping)
            and ffmpeg_record.get("returncode") == 0
            and not internal_reasons
        ):
            adjudication = {
                "policy_version": ADJUDICATION_POLICY_VERSION,
                "head_frame_limit": facts["head_frame_limit"],
                "oracle_reason_codes": list(oracle_reasons),
                "facts": facts,
                "ruling": (
                    "duplicate/non-monotonic PTS confined to the recording-start "
                    "head window; authoritative outside it"
                ),
            }

    status: CertificationStatus = (
        "PASS"
        if oracle_pass
        else (
            media_info.ADJUDICATED_EVIDENCE_STATUS
            if adjudication is not None
            else "BLOCKED"
        )
    )
    authoritative = status != "BLOCKED"
    authoritative_rows = normalized_rows if normalized_rows is not None else []
    frame_status = _classify_frame_pts(authoritative_rows) if authoritative else None
    canonical_rows: Any = authoritative_rows if authoritative else rows_value
    table_sha = (
        media_info._canonical_pts_table_sha256(authoritative_rows)
        if authoritative
        else None
    )
    oracle_record = dict(report)
    oracle_record.pop("pts_table", None)
    producer_path = Path(__file__).resolve()
    evidence = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "kind": EVIDENCE_KIND,
        "created_utc": _utc_now(),
        "status": status,
        "authoritative_frame_timeline": authoritative,
        "scope": "full",
        "reason_codes": [] if authoritative else list(dict.fromkeys(reason_codes)),
        "frame_pts_status": frame_status,
        "frame_count": len(authoritative_rows) if authoritative else parsed_frames,
        "time_base": time_base,
        "pts_table_sha256": table_sha,
        "pts_table": canonical_rows,
        "source": {
            "path": str(value.source_path),
            "sha256": value.source_sha256,
            "size": value.source_size,
            "mtime_ns": value.source_mtime_ns,
        },
        "tools": {
            "ffmpeg": _tool_record(value.ffmpeg),
            "ffmpeg_verified": value.ffmpeg_verified,
        },
        "producer": {
            "path": str(producer_path),
            "sha256": media_info._sha256_file(producer_path),
            "schema_version": EVIDENCE_SCHEMA_VERSION,
        },
        "oracle": {
            "report_sha256": _canonical_json_sha256(report),
            "report": oracle_record,
        },
    }
    if adjudication is not None:
        evidence["anomaly_adjudication"] = adjudication
    if retry_of is not None:
        evidence["retry"] = dict(retry_of)
    return evidence


def _retry_evidence_path(path: Path) -> Path:
    """Return a unique sibling path without replacing the prior evidence."""
    token = uuid.uuid4().hex[:12]
    return path.with_name(f"{path.stem}.retry-{token}{path.suffix}")


def _publish_json_new(path: Path, value: Mapping[str, Any]) -> bool:
    """Publish one evidence file without replacing an existing target."""
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
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
            temporary_name = stream.name
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_name, path)
        except FileExistsError:
            return False
        return True
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def _load_evidence(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise media_info.MediaInfoError(
            "FRAME_PTS_EVIDENCE_INVALID",
            "frame PTS evidence is not readable JSON",
            details={"path": str(path)},
        ) from exc
    if not isinstance(value, Mapping):
        raise media_info.MediaInfoError(
            "FRAME_PTS_EVIDENCE_INVALID",
            "frame PTS evidence root must be an object",
            details={"path": str(path)},
        )
    return value


def _require_bound_evidence(
    payload: Mapping[str, Any],
    value: media_info.MediaInfo,
) -> None:
    if (
        payload.get("schema_version") != EVIDENCE_SCHEMA_VERSION
        or payload.get("kind") != EVIDENCE_KIND
        or payload.get("scope") != "full"
    ):
        raise media_info.MediaInfoError(
            "FRAME_PTS_EVIDENCE_INVALID",
            "cached frame PTS evidence has the wrong kind, schema or scope",
        )
    producer = payload.get("producer")
    producer_path = Path(__file__).resolve()
    producer_sha256 = media_info._sha256_file(producer_path)
    if (
        not isinstance(producer, Mapping)
        or producer.get("schema_version") != EVIDENCE_SCHEMA_VERSION
        or not isinstance(producer.get("path"), str)
        or not _same_path(producer["path"], producer_path)
        or str(producer.get("sha256", "")).lower() != producer_sha256.lower()
    ):
        raise media_info.MediaInfoError(
            "FRAME_PTS_EVIDENCE_PRODUCER_MISMATCH",
            "cached frame PTS evidence was produced by another algorithm version",
        )
    source = payload.get("source")
    if (
        not isinstance(source, Mapping)
        or source.get("sha256") != value.source_sha256
        or source.get("size") != value.source_size
        or not isinstance(source.get("path"), str)
        or not _same_path(source["path"], value.source_path)
    ):
        raise media_info.MediaInfoError(
            "FRAME_PTS_EVIDENCE_SOURCE_MISMATCH",
            "cached frame PTS evidence belongs to a different source",
        )
    tools = payload.get("tools")
    if not isinstance(tools, Mapping) or value.ffmpeg is None:
        raise media_info.MediaInfoError(
            "FRAME_PTS_EVIDENCE_TOOL_MISMATCH",
            "cached frame PTS evidence has no ffmpeg tool binding",
        )
    for name, expected in (("ffmpeg", value.ffmpeg),):
        record = tools.get(name)
        if not isinstance(record, Mapping) or dict(record) != expected.as_dict():
            raise media_info.MediaInfoError(
                "FRAME_PTS_EVIDENCE_TOOL_MISMATCH",
                f"cached frame PTS evidence {name} binding differs from MediaInfo",
            )


def load_frame_pts_certification(
    value: media_info.MediaInfo,
    evidence_path: str | os.PathLike[str],
    *,
    generated: bool = False,
) -> FramePtsCertificationOutcome:
    _require_media_prerequisites(value)
    path = Path(evidence_path).expanduser().resolve()
    payload = _load_evidence(path)
    _require_bound_evidence(payload, value)
    status = payload.get("status")
    reasons = payload.get("reason_codes")
    if not isinstance(reasons, list) or any(
        not isinstance(reason, str) or not reason for reason in reasons
    ):
        raise media_info.MediaInfoError(
            "FRAME_PTS_EVIDENCE_INVALID",
            "frame PTS evidence reason_codes must be a list of strings",
        )
    if status == "BLOCKED":
        if not reasons or payload.get("authoritative_frame_timeline") is not False:
            raise media_info.MediaInfoError(
                "FRAME_PTS_EVIDENCE_INVALID",
                "blocked frame PTS evidence must remain non-authoritative",
            )
        return FramePtsCertificationOutcome(
            "BLOCKED",
            value,
            path,
            tuple(reasons),
            generated,
        )
    head_anomaly_limit = media_info._evidence_head_anomaly_limit(payload, path)
    allowed_status = (
        media_info.ADJUDICATED_EVIDENCE_STATUS if head_anomaly_limit else "PASS"
    )
    if status != allowed_status or reasons:
        raise media_info.MediaInfoError(
            "FRAME_PTS_EVIDENCE_INVALID",
            "frame PTS evidence status is neither a clean PASS nor BLOCKED",
        )
    frame_status = payload.get("frame_pts_status")
    if frame_status not in {"cfr", "vfr"}:
        raise media_info.MediaInfoError(
            "FRAME_PTS_EVIDENCE_INVALID",
            "frame PTS evidence has no derived cfr/vfr status",
        )
    primary_time_base = value.video_streams[0].time_base
    assert primary_time_base is not None
    certification = media_info.FramePtsCertification.from_evidence(
        path,
        status=frame_status,
        source_sha256=value.source_sha256,
        time_base=primary_time_base,
    )
    certified_media = value.certify_frame_pts(certification)
    return FramePtsCertificationOutcome(
        allowed_status,
        certified_media,
        path,
        (),
        generated,
    )


def _default_oracle_probe(
    source: Path,
    ffmpeg: media_info.ToolInfo,
    *,
    checkpoint: Checkpoint | None,
    threads: int = 1,
) -> tuple[dict[str, Any], int]:
    from scripts import verify_mpv_frames

    report, exit_code = verify_mpv_frames.probe_video(
        source,
        verify_mpv_frames.FfmpegExecutable(ffmpeg.path, "media_info"),
        threads=threads,
        max_frames=None,
        cancel_check=checkpoint,
    )
    ffmpeg_record = report.get("ffmpeg")
    if not isinstance(ffmpeg_record, Mapping):
        raise media_info.MediaInfoError(
            "FRAME_PTS_ORACLE_INVALID",
            "frame PTS oracle has no FFmpeg execution record",
        )
    enriched_ffmpeg = dict(ffmpeg_record)
    enriched_ffmpeg.update(_tool_record(ffmpeg))
    enriched_ffmpeg["resolution_source"] = "media_info"
    report["ffmpeg"] = enriched_ffmpeg
    return report, exit_code


def produce_frame_pts_certification(
    value: media_info.MediaInfo,
    *,
    evidence_path: str | os.PathLike[str] | None = None,
    cache_root: str | os.PathLike[str] | None = None,
    checkpoint: Checkpoint | None = None,
    publish_commit: PublishCommit | None = None,
    oracle_probe: OracleProbe | None = None,
    retry_blocked: bool = False,
    decode_threads: int = 1,
) -> FramePtsCertificationOutcome:
    """Generate or reuse one full, immutable PTS certification evidence file.

    Existing evidence remains authoritative for the default cache-first path.
    A caller must explicitly request ``retry_blocked`` to run a new attempt
    after a cached BLOCKED result; the retry is published beside the old file.
    ``decode_threads`` only speeds up the full decode; the oracle command
    identity accepts any single positive thread count.
    """
    _require_media_prerequisites(value)
    if (
        isinstance(decode_threads, bool)
        or not isinstance(decode_threads, int)
        or decode_threads < 1
    ):
        raise media_info.MediaInfoError(
            "FRAME_PTS_THREADS_INVALID",
            "decode_threads must be a positive integer",
        )
    path = (
        Path(evidence_path).expanduser().resolve()
        if evidence_path is not None
        else default_evidence_path(value, cache_root=cache_root)
    )
    retry_of: dict[str, Any] | None = None
    if path.is_file():
        cached = load_frame_pts_certification(value, path)
        if cached.status != "BLOCKED" or not retry_blocked:
            return cached
        retry_of = {
            "previous_evidence_path": str(path),
            "previous_evidence_sha256": media_info._sha256_file(path),
            "reason": "explicit_retry_blocked",
        }
        path = _retry_evidence_path(path)
    if checkpoint is not None:
        checkpoint()
    assert value.ffmpeg is not None
    source_hash_before = media_info._sha256_file(value.source_path)
    ffmpeg_hash_before = media_info._sha256_file(value.ffmpeg.path)
    probe = oracle_probe
    if probe is None:
        report, exit_code = _default_oracle_probe(
            value.source_path,
            value.ffmpeg,
            checkpoint=checkpoint,
            threads=decode_threads,
        )
    else:
        report, exit_code = probe(
            value.source_path,
            value.ffmpeg.path,
            checkpoint=checkpoint,
        )
    if checkpoint is not None:
        checkpoint()
    if (
        media_info._sha256_file(value.source_path) != source_hash_before
        or source_hash_before != value.source_sha256
    ):
        raise media_info.MediaInfoError(
            "SOURCE_CHANGED_DURING_FRAME_PTS_PROBE",
            "source media changed while frame PTS evidence was generated",
        )
    if (
        media_info._sha256_file(value.ffmpeg.path) != ffmpeg_hash_before
        or ffmpeg_hash_before != value.ffmpeg.sha256
    ):
        raise media_info.MediaInfoError(
            "TOOL_CHANGED_DURING_FRAME_PTS_PROBE",
            "ffmpeg executable changed while frame PTS evidence was generated",
        )
    evidence = _build_evidence(
        report,
        int(exit_code),
        value,
        retry_of=retry_of,
    )
    generated = (
        publish_commit(_publish_json_new, path, evidence)
        if publish_commit is not None
        else _publish_json_new(path, evidence)
    )
    if checkpoint is not None:
        checkpoint()
    return load_frame_pts_certification(value, path, generated=generated)
