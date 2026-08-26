"""Structured ffprobe metadata for the production export boundary.

This module is deliberately independent from the current analyzer/export
implementation.  It provides a strict, immutable metadata snapshot so a
future exporter can make an explicit decision about PTS, VFR and audio rather
than parsing human-readable FFmpeg output.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, replace

from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


_BUNDLE_BIN_DIR = (
    Path(__file__).resolve().parent
    / "tools"
    / "ffmpeg-7.1.0"
    / "bundle"
    / "ffmpeg-7.1-essentials_build"
    / "bin"
)


class MediaInfoError(RuntimeError):
    """A media probe could not produce an auditable metadata snapshot."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _fraction_text(value: Fraction | None) -> str | None:
    return None if value is None else f"{value.numerator}/{value.denominator}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bundled_tool_path(name: str) -> Path | None:
    candidate = _BUNDLE_BIN_DIR / name
    return candidate.resolve() if candidate.is_file() else None


def _tool_info(path: Path, *, version_line: str | None = None, verified: bool = False) -> "ToolInfo":
    path = path.expanduser().resolve()
    if not path.is_file():
        raise MediaInfoError("TOOL_NOT_FOUND", f"tool not found: {path}", details={"path": str(path)})
    return ToolInfo(path=path, sha256=_sha256_file(path), version_line=version_line, verified=verified)


@dataclass(frozen=True)
class ToolInfo:
    path: Path
    sha256: str
    version_line: str | None = None
    verified: bool = False

    def is_current(self) -> bool:
        """Return whether the executable still matches its registered hash."""
        try:
            return self.path.is_file() and _sha256_file(self.path).lower() == self.sha256.lower()
        except OSError:
            return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "version_line": self.version_line,
            "verified": self.verified,
        }


_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")
_FRAME_PTS_EVIDENCE_SCHEMA_VERSION = 1
_FRAME_PTS_EVIDENCE_KIND = "production_frame_pts_certification"


def _canonical_pts_table_sha256(rows: Sequence[Mapping[str, Any]]) -> str:
    payload = json.dumps(
        list(rows),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _tool_binding_from_evidence(
    payload: Mapping[str, Any],
    name: str,
) -> Mapping[str, Any]:
    tools = payload.get("tools")
    if not isinstance(tools, Mapping):
        raise MediaInfoError(
            "FRAME_PTS_EVIDENCE_INVALID",
            "frame PTS evidence has no tool binding",
            details={"tool": name},
        )
    value = tools.get(name)
    if not isinstance(value, Mapping):
        raise MediaInfoError(
            "FRAME_PTS_EVIDENCE_INVALID",
            f"frame PTS evidence has no {name} binding",
            details={"tool": name},
        )
    path = value.get("path")
    sha256 = value.get("sha256")
    version_line = value.get("version_line")
    if (
        not isinstance(path, str)
        or not path
        or not isinstance(sha256, str)
        or _SHA256_RE.fullmatch(sha256) is None
        or not isinstance(version_line, str)
        or not version_line
        or value.get("verified") is not True
    ):
        raise MediaInfoError(
            "FRAME_PTS_EVIDENCE_INVALID",
            f"frame PTS evidence has an invalid {name} binding",
            details={"tool": name},
        )
    return value


HEAD_ANOMALY_FRAME_LIMIT = 32
"""B' targeted-adjudication ceiling (2026-08-16 owner ruling).

Recording-start artifacts may produce duplicate or non-monotonic PTS confined
to the first frames of a stream.  Evidence may adjudicate them only when every
involved decode index stays below this limit; anything beyond it, or any other
anomaly shape, keeps the certification BLOCKED.
"""

ADJUDICATED_EVIDENCE_STATUS = "PASS_WITH_HEAD_ANOMALIES"


def _evidence_head_anomaly_limit(payload: Mapping[str, Any], path: Path) -> int:
    """Return the adjudicated head window, or 0 when evidence is strictly clean."""
    adjudication = payload.get("anomaly_adjudication")
    if adjudication is None:
        if payload.get("status") == ADJUDICATED_EVIDENCE_STATUS:
            raise MediaInfoError(
                "FRAME_PTS_EVIDENCE_INVALID",
                "adjudicated frame PTS evidence has no anomaly_adjudication block",
                details={"path": str(path)},
            )
        return 0
    if not isinstance(adjudication, Mapping) or payload.get("status") != ADJUDICATED_EVIDENCE_STATUS:
        raise MediaInfoError(
            "FRAME_PTS_EVIDENCE_INVALID",
            "anomaly_adjudication requires the adjudicated evidence status",
            details={"path": str(path), "status": payload.get("status")},
        )
    raw_limit = adjudication.get("head_frame_limit")
    if (
        isinstance(raw_limit, bool)
        or not isinstance(raw_limit, int)
        or raw_limit <= 0
        or raw_limit > HEAD_ANOMALY_FRAME_LIMIT
    ):
        raise MediaInfoError(
            "FRAME_PTS_EVIDENCE_INVALID",
            "anomaly_adjudication head_frame_limit exceeds the registered policy ceiling",
            details={"path": str(path), "head_frame_limit": raw_limit},
        )
    return raw_limit


def _check_pts_monotonic_head_tolerant(
    pairs,
    *,
    head_anomaly_limit: int,
    path: Path | None = None,
) -> None:
    """Require strictly increasing PTS outside the adjudicated head window."""
    previous_pts: int | None = None
    for expected, (number, pts) in enumerate(pairs):
        if previous_pts is not None and pts <= previous_pts:
            if head_anomaly_limit <= 0 or expected >= head_anomaly_limit:
                raise MediaInfoError(
                    "FRAME_PTS_EVIDENCE_NON_MONOTONIC",
                    "frame PTS table must be strictly increasing",
                    details={
                        "path": str(path) if path is not None else None,
                        "row": expected,
                        "previous_pts": previous_pts,
                        "pts": pts,
                    },
                )
        previous_pts = pts


def head_anomaly_facts(pairs, *, head_frame_limit: int) -> dict[str, Any] | None:
    """Recompute duplicate/non-monotonic facts for adjudication decisions.

    Returns ``None`` when the table is strictly increasing.  Raises
    ``MediaInfoError`` when any anomaly involves a frame at or beyond
    ``head_frame_limit`` (not adjudicable).  Callers receive the exact
    monotonic breaks and duplicate tick groups confined to the head window.
    """
    breaks: list[dict[str, int]] = []
    ticks: dict[int, list[int]] = {}
    previous_pts: int | None = None
    for number, pts in pairs:
        ticks.setdefault(pts, []).append(number)
        if previous_pts is not None and pts <= previous_pts:
            breaks.append(
                {"n": number, "previous_pts": previous_pts, "pts": pts}
            )
        previous_pts = pts
    duplicates = [
        {"pts": pts, "frames": sorted(numbers)}
        for pts, numbers in sorted(ticks.items())
        if len(numbers) > 1
    ]
    if not breaks and not duplicates:
        return None
    involved = sorted(
        {
            index
            for break_ in breaks
            for index in (break_["n"], break_["n"] - 1)
        }
        | {index for group in duplicates for index in group["frames"]}
    )
    if any(index >= head_frame_limit for index in involved):
        raise MediaInfoError(
            "FRAME_PTS_ANOMALY_BEYOND_HEAD",
            "PTS anomalies extend beyond the adjudicable head window",
            details={"involved_frames": involved, "head_frame_limit": head_frame_limit},
        )
    return {
        "head_frame_limit": head_frame_limit,
        "monotonic_breaks": breaks,
        "duplicate_ticks": duplicates,
        "involved_frames": involved,
    }


def _load_certification_evidence(
    certification: "FramePtsCertification",
    *,
    expected_source_path: Path | None = None,
    expected_source_size: int | None = None,
    expected_ffmpeg: "ToolInfo | None" = None,
    expected_ffprobe: "ToolInfo | None" = None,
) -> tuple[list[Mapping[str, Any]], int]:
    path = certification.evidence_path
    if not path.is_file():
        raise MediaInfoError(
            "FRAME_PTS_EVIDENCE_MISSING",
            "frame PTS certification evidence does not exist",
            details={"path": str(path)},
        )
    if _sha256_file(path).lower() != certification.evidence_sha256.lower():
        raise MediaInfoError(
            "FRAME_PTS_EVIDENCE_CHANGED",
            "frame PTS certification evidence changed after registration",
            details={"path": str(path)},
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MediaInfoError(
            "FRAME_PTS_EVIDENCE_INVALID",
            "frame PTS certification evidence is not valid JSON",
            details={"path": str(path)},
        ) from exc
    if not isinstance(payload, Mapping):
        raise MediaInfoError("FRAME_PTS_EVIDENCE_INVALID", "frame PTS evidence root must be an object")
    if (
        payload.get("schema_version") != _FRAME_PTS_EVIDENCE_SCHEMA_VERSION
        or payload.get("kind") != _FRAME_PTS_EVIDENCE_KIND
    ):
        raise MediaInfoError(
            "FRAME_PTS_EVIDENCE_INVALID",
            "frame PTS evidence kind or schema is not production certification",
            details={"path": str(path)},
        )
    head_anomaly_limit = _evidence_head_anomaly_limit(payload, path)
    allowed_status = "PASS_WITH_HEAD_ANOMALIES" if head_anomaly_limit else "PASS"
    if payload.get("status") != allowed_status or payload.get("authoritative_frame_timeline") is not True:
        raise MediaInfoError(
            "FRAME_PTS_EVIDENCE_NOT_AUTHORITATIVE",
            "frame PTS evidence is not an authoritative PASS",
            details={"path": str(path), "status": payload.get("status")},
        )
    reason_codes = payload.get("reason_codes", [])
    if reason_codes != [] or payload.get("scope") != "full":
        raise MediaInfoError(
            "FRAME_PTS_EVIDENCE_NOT_AUTHORITATIVE",
            "frame PTS evidence contains blocking reasons or is not full-scope",
            details={"path": str(path), "reason_codes": reason_codes, "scope": payload.get("scope")},
        )
    source = payload.get("source")
    if (
        not isinstance(source, Mapping)
        or not isinstance(source.get("path"), str)
        or not source.get("path")
        or source.get("sha256") != certification.source_sha256
    ):
        raise MediaInfoError(
            "FRAME_PTS_EVIDENCE_SOURCE_MISMATCH",
            "frame PTS evidence source binding differs from the certification",
            details={"path": str(path)},
        )
    if expected_source_path is not None:
        if Path(str(source["path"])).expanduser().resolve() != expected_source_path.resolve():
            raise MediaInfoError(
                "FRAME_PTS_EVIDENCE_SOURCE_MISMATCH",
                "frame PTS evidence path differs from MediaInfo",
                details={"path": str(path)},
            )
    if expected_source_size is not None and source.get("size") != expected_source_size:
        raise MediaInfoError(
            "FRAME_PTS_EVIDENCE_SOURCE_MISMATCH",
            "frame PTS evidence size differs from MediaInfo",
            details={"path": str(path)},
        )
    time_base = payload.get("time_base")
    expected_time_base = _fraction_text(certification.time_base)
    if (
        not isinstance(time_base, Mapping)
        or time_base.get("numerator") != certification.time_base.numerator
        or time_base.get("denominator") != certification.time_base.denominator
        or time_base.get("text") != expected_time_base
    ):
        raise MediaInfoError(
            "FRAME_PTS_EVIDENCE_TIME_BASE_MISMATCH",
            "frame PTS evidence time_base differs from the certification",
            details={"path": str(path), "expected": expected_time_base},
        )
    frame_status = payload.get("frame_pts_status")
    if frame_status != certification.status:
        raise MediaInfoError(
            "FRAME_PTS_EVIDENCE_STATUS_MISMATCH",
            "frame PTS evidence status differs from the certification",
            details={"path": str(path)},
        )
    ffmpeg_binding = _tool_binding_from_evidence(payload, "ffmpeg")
    ffprobe_binding = _tool_binding_from_evidence(payload, "ffprobe")
    for binding, expected, name in (
        (ffmpeg_binding, expected_ffmpeg, "ffmpeg"),
        (ffprobe_binding, expected_ffprobe, "ffprobe"),
    ):
        if expected is None:
            continue
        if (
            Path(str(binding["path"])).expanduser().resolve() != expected.path
            or str(binding["sha256"]).lower() != expected.sha256.lower()
            or binding["version_line"] != expected.version_line
            or binding.get("verified") is not True
        ):
            raise MediaInfoError(
                "FRAME_PTS_EVIDENCE_TOOL_MISMATCH",
                f"frame PTS evidence {name} binding differs from MediaInfo",
                details={"path": str(path), "tool": name},
            )
    rows = payload.get("pts_table")
    if not isinstance(rows, list) or not rows:
        raise MediaInfoError(
            "FRAME_PTS_EVIDENCE_INCOMPLETE",
            "frame PTS evidence has no complete pts_table",
            details={"path": str(path)},
        )
    if payload.get("frame_count") != len(rows):
        raise MediaInfoError(
            "FRAME_PTS_EVIDENCE_INCOMPLETE",
            "frame PTS evidence frame_count does not match pts_table",
            details={"path": str(path)},
        )
    for expected, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise MediaInfoError("FRAME_PTS_EVIDENCE_INVALID", "frame PTS table row must be an object")
        number = row.get("n")
        pts = row.get("pts")
        duration = row.get("duration")
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
            raise MediaInfoError(
                "FRAME_PTS_EVIDENCE_INVALID",
                "frame PTS table must contain contiguous integer n/pts and positive duration ticks",
                details={"path": str(path), "row": expected},
            )
    _check_pts_monotonic_head_tolerant(
        ((row["n"], row["pts"]) for row in rows),
        head_anomaly_limit=head_anomaly_limit,
        path=path,
    )
    if _canonical_pts_table_sha256(rows) != certification.pts_table_sha256.lower():
        raise MediaInfoError(
            "FRAME_PTS_EVIDENCE_CHANGED",
            "frame PTS table digest does not match the certification",
            details={"path": str(path)},
        )
    declared_table_sha = payload.get("pts_table_sha256")
    if declared_table_sha != certification.pts_table_sha256:
        raise MediaInfoError(
            "FRAME_PTS_EVIDENCE_CHANGED",
            "frame PTS evidence declares a stale table digest",
            details={"path": str(path)},
        )
    if len(rows) != certification.frame_count:
        raise MediaInfoError(
            "FRAME_PTS_EVIDENCE_INCOMPLETE",
            "frame PTS table length does not match the certification",
            details={"expected": certification.frame_count, "actual": len(rows)},
        )
    return rows, head_anomaly_limit


@dataclass(frozen=True)
class FramePtsCertification:
    """Immutable binding for an authoritative decoded-frame PTS table."""

    status: str
    source_sha256: str
    frame_count: int
    time_base: Fraction
    pts_table_sha256: str
    evidence_path: Path
    evidence_sha256: str

    def __post_init__(self) -> None:
        if self.status not in {"cfr", "vfr"}:
            raise MediaInfoError("FRAME_PTS_STATUS_INVALID", "frame PTS status must be cfr or vfr")
        source_sha = str(self.source_sha256).lower()
        evidence_sha = str(self.evidence_sha256).lower()
        table_sha = str(self.pts_table_sha256).lower()
        if not _SHA256_RE.fullmatch(source_sha):
            raise MediaInfoError("FRAME_PTS_CERTIFICATION_INVALID", "source SHA-256 is invalid")
        if not _SHA256_RE.fullmatch(evidence_sha) or not _SHA256_RE.fullmatch(table_sha):
            raise MediaInfoError("FRAME_PTS_CERTIFICATION_INVALID", "certification SHA-256 is invalid")
        if isinstance(self.frame_count, bool) or not isinstance(self.frame_count, int) or self.frame_count <= 0:
            raise MediaInfoError("FRAME_PTS_CERTIFICATION_INVALID", "frame_count must be a positive integer")
        if not isinstance(self.time_base, Fraction) or self.time_base <= 0:
            raise MediaInfoError("FRAME_PTS_CERTIFICATION_INVALID", "time_base must be a positive Fraction")
        object.__setattr__(self, "source_sha256", source_sha)
        object.__setattr__(self, "evidence_sha256", evidence_sha)
        object.__setattr__(self, "pts_table_sha256", table_sha)
        object.__setattr__(self, "evidence_path", Path(self.evidence_path).expanduser().resolve())

    @classmethod
    def from_evidence(
        cls,
        evidence_path: str | os.PathLike[str],
        *,
        status: str,
        source_sha256: str,
        time_base: Fraction,
    ) -> "FramePtsCertification":
        path = Path(evidence_path).expanduser().resolve()
        if not path.is_file():
            raise MediaInfoError("FRAME_PTS_EVIDENCE_MISSING", "frame PTS certification evidence does not exist")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MediaInfoError("FRAME_PTS_EVIDENCE_INVALID", "frame PTS certification evidence is not valid JSON") from exc
        rows = payload.get("pts_table") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list) or not rows:
            raise MediaInfoError("FRAME_PTS_EVIDENCE_INCOMPLETE", "frame PTS evidence has no complete pts_table")
        certification = cls(
            status=status,
            source_sha256=source_sha256,
            frame_count=len(rows),
            time_base=time_base,
            pts_table_sha256=_canonical_pts_table_sha256(rows),
            evidence_path=path,
            evidence_sha256=_sha256_file(path),
        )
        _load_certification_evidence(certification)
        return certification

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source_sha256": self.source_sha256,
            "frame_count": self.frame_count,
            "time_base": _fraction_text(self.time_base),
            "pts_table_sha256": self.pts_table_sha256,
            "evidence_path": str(self.evidence_path),
            "evidence_sha256": self.evidence_sha256,
        }


def _version_token(version_line: str | None) -> str | None:
    if not version_line:
        return None
    match = re.search(r"\bversion\s+(\S+)", version_line, flags=re.IGNORECASE)
    return match.group(1) if match else None


def _tool_pair_verified(ffprobe: ToolInfo | None, ffmpeg: ToolInfo | None) -> bool:
    """Require a single immutable FFmpeg build for both CLI tools."""
    if ffprobe is None or ffmpeg is None:
        return False
    if not ffprobe.verified or not ffmpeg.verified:
        return False
    if not ffprobe.version_line or not ffmpeg.version_line:
        return False
    if ffprobe.path.resolve().parent != ffmpeg.path.resolve().parent:
        return False
    if ffprobe.path.resolve() == ffmpeg.path.resolve():
        return False
    if not re.fullmatch(r"[0-9a-f]{64}", ffprobe.sha256.lower()):
        return False
    if not re.fullmatch(r"[0-9a-f]{64}", ffmpeg.sha256.lower()):
        return False
    if ffprobe.sha256.lower() == ffmpeg.sha256.lower():
        return False
    probe_token = _version_token(ffprobe.version_line)
    ffmpeg_token = _version_token(ffmpeg.version_line)
    if probe_token is None or ffmpeg_token is None:
        return False
    return probe_token == ffmpeg_token


def _require_tool_pair(ffprobe: ToolInfo, ffmpeg: ToolInfo) -> None:
    if _tool_pair_verified(ffprobe, ffmpeg):
        return
    raise MediaInfoError(
        "TOOL_PAIR_MISMATCH",
        "ffprobe and ffmpeg must be verified tools from the same bundle and version",
        details={
            "ffprobe_path": str(ffprobe.path),
            "ffmpeg_path": str(ffmpeg.path),
            "ffprobe_version": ffprobe.version_line,
            "ffmpeg_version": ffmpeg.version_line,
        },
    )


@dataclass(frozen=True)
class VideoStreamInfo:
    index: int
    codec_name: str
    codec_long_name: str | None
    width: int
    height: int
    pixel_format: str
    time_base: Fraction | None
    start_time: Fraction | None
    duration: Fraction | None
    avg_frame_rate: Fraction | None
    r_frame_rate: Fraction | None
    frame_count: int | None
    start_pts: int | None = None
    duration_ts: int | None = None
    validation_errors: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "codec_name": self.codec_name,
            "codec_long_name": self.codec_long_name,
            "width": self.width,
            "height": self.height,
            "pixel_format": self.pixel_format,
            "time_base": _fraction_text(self.time_base),
            "start_time": _fraction_text(self.start_time),
            "duration": _fraction_text(self.duration),
            "avg_frame_rate": _fraction_text(self.avg_frame_rate),
            "r_frame_rate": _fraction_text(self.r_frame_rate),
            "frame_count": self.frame_count,
            "start_pts": self.start_pts,
            "duration_ts": self.duration_ts,
            "validation_errors": list(self.validation_errors),
        }


@dataclass(frozen=True)
class AudioStreamInfo:
    index: int
    codec_name: str
    codec_long_name: str | None
    sample_rate: int | None
    channels: int | None
    channel_layout: str | None
    sample_format: str | None
    time_base: Fraction | None
    start_time: Fraction | None
    duration: Fraction | None
    bit_rate: int | None
    start_pts: int | None = None
    duration_ts: int | None = None
    validation_errors: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "codec_name": self.codec_name,
            "codec_long_name": self.codec_long_name,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "channel_layout": self.channel_layout,
            "sample_format": self.sample_format,
            "time_base": _fraction_text(self.time_base),
            "start_time": _fraction_text(self.start_time),
            "duration": _fraction_text(self.duration),
            "bit_rate": self.bit_rate,
            "start_pts": self.start_pts,
            "duration_ts": self.duration_ts,
            "validation_errors": list(self.validation_errors),
        }


@dataclass(frozen=True)
class MediaInfo:
    source_path: Path
    source_sha256: str
    source_size: int
    source_mtime_ns: int
    format_name: str
    format_long_name: str | None
    duration: Fraction | None
    start_time: Fraction | None
    video_streams: tuple[VideoStreamInfo, ...]
    audio_streams: tuple[AudioStreamInfo, ...]
    vfr_status: str
    frame_pts_authoritative: bool = False
    frame_pts_certification: FramePtsCertification | None = None
    validation_errors: tuple[str, ...] = ()
    ffprobe: ToolInfo | None = None
    ffmpeg: ToolInfo | None = None

    @property
    def has_audio(self) -> bool:
        return bool(self.audio_streams)

    @property
    def tool_pair_verified(self) -> bool:
        return _tool_pair_verified(self.ffprobe, self.ffmpeg)

    def source_is_current(self) -> bool:
        """Return whether the probed source still has the registered identity."""
        try:
            return self.source_path.is_file() and _sha256_file(self.source_path) == self.source_sha256
        except OSError:
            return False

    def assert_source_current(self) -> None:
        if not self.source_is_current():
            raise MediaInfoError(
                "SOURCE_CHANGED_AFTER_PROBE",
                "source media no longer matches the registered probe identity",
                details={"path": str(self.source_path), "expected_sha256": self.source_sha256},
            )

    @property
    def complete_for_export(self) -> bool:
        tool_ok = (
            self.ffprobe is not None
            and self.ffmpeg is not None
            and self.ffprobe.verified
            and self.ffmpeg.verified
            and bool(self.ffprobe.version_line)
            and bool(self.ffmpeg.version_line)
            and self.tool_pair_verified
            and self.ffprobe.is_current()
            and self.ffmpeg.is_current()
        )
        ticks_ok = all(
            stream.start_pts is not None and stream.duration_ts is not None
            for stream in (*self.video_streams, *self.audio_streams)
        )
        certification_ok = self._frame_pts_certification_is_current()
        return (
            bool(self.video_streams)
            and self.frame_pts_authoritative
            and certification_ok
            and ticks_ok
            and not self.validation_errors
            and bool(re.fullmatch(r"[0-9a-f]{64}", self.source_sha256))
            and self.source_is_current()
            and tool_ok
        )

    def _frame_pts_certification_is_current(self) -> bool:
        certification = self.frame_pts_certification
        if not self.frame_pts_authoritative or certification is None:
            return False
        try:
            if certification.source_sha256 != self.source_sha256:
                return False
            primary = self.video_streams[0] if self.video_streams else None
            if primary is None or primary.time_base != certification.time_base:
                return False
            _load_certification_evidence(
                certification,
                expected_source_path=self.source_path,
                expected_source_size=self.source_size,
                expected_ffmpeg=self.ffmpeg,
                expected_ffprobe=self.ffprobe,
            )
        except (MediaInfoError, OSError, TypeError, ValueError):
            return False
        return True

    def certify_frame_pts(self, certification: FramePtsCertification) -> "MediaInfo":
        """Attach a complete, source-bound certification for decoded frame PTS."""
        if not isinstance(certification, FramePtsCertification):
            raise MediaInfoError(
                "FRAME_PTS_CERTIFICATION_REQUIRED",
                "complete FramePtsCertification is required; a status string is insufficient",
            )
        if not all(
            stream.start_pts is not None and stream.duration_ts is not None
            for stream in (*self.video_streams, *self.audio_streams)
        ):
            raise MediaInfoError("FRAME_PTS_TICKS_MISSING", "frame PTS certification requires stream timestamp ticks")
        if certification.source_sha256 != self.source_sha256:
            raise MediaInfoError("FRAME_PTS_SOURCE_MISMATCH", "frame PTS certification belongs to a different source")
        primary = self.video_streams[0]
        if primary.time_base != certification.time_base:
            raise MediaInfoError("FRAME_PTS_TIME_BASE_MISMATCH", "frame PTS certification time_base differs from video stream")
        _load_certification_evidence(
            certification,
            expected_source_path=self.source_path,
            expected_source_size=self.source_size,
            expected_ffmpeg=self.ffmpeg,
            expected_ffprobe=self.ffprobe,
        )
        return replace(
            self,
            vfr_status=certification.status,
            frame_pts_authoritative=True,
            frame_pts_certification=certification,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": {
                "path": str(self.source_path),
                "sha256": self.source_sha256,
                "size": self.source_size,
                "mtime_ns": self.source_mtime_ns,
            },
            "format": {
                "name": self.format_name,
                "long_name": self.format_long_name,
                "duration": _fraction_text(self.duration),
                "start_time": _fraction_text(self.start_time),
            },
            "video_streams": [stream.as_dict() for stream in self.video_streams],
            "audio_streams": [stream.as_dict() for stream in self.audio_streams],
            "has_audio": self.has_audio,
            "vfr_status": self.vfr_status,
            "frame_pts_authoritative": self.frame_pts_authoritative,
            "frame_pts_certification": (
                self.frame_pts_certification.as_dict()
                if self.frame_pts_certification is not None
                else None
            ),
            "validation_errors": list(self.validation_errors),
            "complete_for_export": self.complete_for_export,
            "tool_pair_verified": self.tool_pair_verified,
            "ffprobe": self.ffprobe.as_dict() if self.ffprobe else None,
            "ffmpeg": self.ffmpeg.as_dict() if self.ffmpeg else None,
        }


def _missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip().upper() in {"", "N/A", "NA"})


def _required_text(stream: Mapping[str, Any], key: str, *, field: str) -> str:
    value = stream.get(key)
    if _missing(value):
        raise MediaInfoError("PROBE_FIELD_MISSING", f"missing required field: {field}", details={"field": field})
    if not isinstance(value, str):
        raise MediaInfoError("PROBE_FIELD_INVALID", f"field must be text: {field}", details={"field": field})
    return value.strip()


def _optional_text(stream: Mapping[str, Any], key: str) -> str | None:
    value = stream.get(key)
    if _missing(value):
        return None
    if not isinstance(value, str):
        raise MediaInfoError("PROBE_FIELD_INVALID", f"field must be text: {key}", details={"field": key})
    return value.strip()


def _optional_int(value: Any, *, field: str, minimum: int | None = 0) -> int | None:
    if _missing(value):
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise MediaInfoError("PROBE_FIELD_INVALID", f"field must be an integer: {field}", details={"field": field})
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        result = int(value.strip())
    else:
        raise MediaInfoError("PROBE_FIELD_INVALID", f"field must be an integer: {field}", details={"field": field})
    if minimum is not None and result < minimum:
        raise MediaInfoError("PROBE_FIELD_INVALID", f"field is below minimum: {field}", details={"field": field})
    return result


def _required_int(stream: Mapping[str, Any], key: str, *, field: str, minimum: int = 0) -> int:
    value = _optional_int(stream.get(key), field=field, minimum=minimum)
    if value is None:
        raise MediaInfoError("PROBE_FIELD_MISSING", f"missing required field: {field}", details={"field": field})
    return value


def _optional_fraction(value: Any, *, field: str, positive: bool = False, zero_is_none: bool = False) -> Fraction | None:
    if _missing(value):
        return None
    if isinstance(value, bool):
        raise MediaInfoError("PROBE_FIELD_INVALID", f"field must be rational: {field}", details={"field": field})
    try:
        if isinstance(value, Fraction):
            result = value
        elif isinstance(value, int):
            result = Fraction(value, 1)
        elif isinstance(value, float):
            raise ValueError("float input is not an exact rational")
        else:
            text = str(value).strip()
            if text == "0/0" or (zero_is_none and text in {"0", "0/1"}):
                return None
            result = Fraction(text)
    except (ValueError, ZeroDivisionError) as exc:
        raise MediaInfoError("PROBE_FIELD_INVALID", f"field must be rational: {field}", details={"field": field}) from exc
    if result.denominator <= 0 or (positive and result <= 0):
        raise MediaInfoError("PROBE_FIELD_INVALID", f"field must be positive: {field}", details={"field": field})
    return result


def _optional_frame_rate(value: Any, *, field: str) -> Fraction | None:
    return _optional_fraction(value, field=field, positive=True, zero_is_none=True)


def _parse_video(stream: Mapping[str, Any], ordinal: int) -> VideoStreamInfo:
    prefix = f"video[{ordinal}]"
    index = _required_int(stream, "index", field=f"{prefix}.index")
    codec_name = _required_text(stream, "codec_name", field=f"{prefix}.codec_name")
    width = _required_int(stream, "width", field=f"{prefix}.width", minimum=1)
    height = _required_int(stream, "height", field=f"{prefix}.height", minimum=1)
    pixel_format = _required_text(stream, "pix_fmt", field=f"{prefix}.pix_fmt")
    time_base = _optional_fraction(stream.get("time_base"), field=f"{prefix}.time_base", positive=True)
    if time_base is None:
        raise MediaInfoError("PROBE_FIELD_MISSING", f"missing required field: {prefix}.time_base", details={"field": f"{prefix}.time_base"})
    errors: list[str] = []
    avg = _optional_frame_rate(stream.get("avg_frame_rate"), field=f"{prefix}.avg_frame_rate")
    rate = _optional_frame_rate(stream.get("r_frame_rate"), field=f"{prefix}.r_frame_rate")
    if avg is None:
        errors.append(f"{prefix}.avg_frame_rate_missing")
    if rate is None:
        errors.append(f"{prefix}.r_frame_rate_missing")
    start = _optional_fraction(stream.get("start_time"), field=f"{prefix}.start_time")
    duration = _optional_fraction(stream.get("duration"), field=f"{prefix}.duration", positive=True)
    if duration is None:
        errors.append(f"{prefix}.duration_missing")
    frame_count = _optional_int(stream.get("nb_frames"), field=f"{prefix}.nb_frames")
    start_pts = _optional_int(stream.get("start_pts"), field=f"{prefix}.start_pts", minimum=None)
    duration_ts = _optional_int(stream.get("duration_ts"), field=f"{prefix}.duration_ts", minimum=1)
    return VideoStreamInfo(
        index=index,
        codec_name=codec_name,
        codec_long_name=_optional_text(stream, "codec_long_name"),
        width=width,
        height=height,
        pixel_format=pixel_format,
        time_base=time_base,
        start_time=start,
        duration=duration,
        avg_frame_rate=avg,
        r_frame_rate=rate,
        frame_count=frame_count,
        start_pts=start_pts,
        duration_ts=duration_ts,
        validation_errors=tuple(errors),
    )


def _parse_audio(stream: Mapping[str, Any], ordinal: int) -> AudioStreamInfo:
    prefix = f"audio[{ordinal}]"
    index = _required_int(stream, "index", field=f"{prefix}.index")
    codec_name = _required_text(stream, "codec_name", field=f"{prefix}.codec_name")
    sample_rate = _optional_int(stream.get("sample_rate"), field=f"{prefix}.sample_rate", minimum=1)
    channels = _optional_int(stream.get("channels"), field=f"{prefix}.channels", minimum=1)
    time_base = _optional_fraction(stream.get("time_base"), field=f"{prefix}.time_base", positive=True)
    errors: list[str] = []
    for value, name in ((sample_rate, "sample_rate"), (channels, "channels"), (time_base, "time_base")):
        if value is None:
            errors.append(f"{prefix}.{name}_missing")
    duration = _optional_fraction(stream.get("duration"), field=f"{prefix}.duration", positive=True)
    if duration is None:
        errors.append(f"{prefix}.duration_missing")
    start_pts = _optional_int(stream.get("start_pts"), field=f"{prefix}.start_pts", minimum=None)
    duration_ts = _optional_int(stream.get("duration_ts"), field=f"{prefix}.duration_ts", minimum=1)
    return AudioStreamInfo(
        index=index,
        codec_name=codec_name,
        codec_long_name=_optional_text(stream, "codec_long_name"),
        sample_rate=sample_rate,
        channels=channels,
        channel_layout=_optional_text(stream, "channel_layout"),
        sample_format=_optional_text(stream, "sample_fmt"),
        time_base=time_base,
        start_time=_optional_fraction(stream.get("start_time"), field=f"{prefix}.start_time"),
        duration=duration,
        bit_rate=_optional_int(stream.get("bit_rate"), field=f"{prefix}.bit_rate"),
        start_pts=start_pts,
        duration_ts=duration_ts,
        validation_errors=tuple(errors),
    )


def _parse_payload(payload: Mapping[str, Any] | str | bytes, source_path: Path) -> tuple[tuple[VideoStreamInfo, ...], tuple[AudioStreamInfo, ...], Mapping[str, Any]]:
    if isinstance(payload, (str, bytes)):
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MediaInfoError("PROBE_JSON_INVALID", "ffprobe output is not valid JSON") from exc
    else:
        decoded = payload
    if not isinstance(decoded, Mapping):
        raise MediaInfoError("PROBE_JSON_INVALID", "ffprobe JSON root must be an object")
    streams = decoded.get("streams")
    fmt = decoded.get("format")
    if not isinstance(streams, list):
        raise MediaInfoError("PROBE_FIELD_MISSING", "ffprobe JSON has no streams array", details={"field": "streams"})
    if not isinstance(fmt, Mapping):
        raise MediaInfoError("PROBE_FIELD_MISSING", "ffprobe JSON has no format object", details={"field": "format"})
    videos: list[VideoStreamInfo] = []
    audios: list[AudioStreamInfo] = []
    for stream in streams:
        if not isinstance(stream, Mapping):
            raise MediaInfoError("PROBE_FIELD_INVALID", "ffprobe stream entry must be an object")
        stream_type = stream.get("codec_type")
        if stream_type == "video":
            videos.append(_parse_video(stream, len(videos)))
        elif stream_type == "audio":
            audios.append(_parse_audio(stream, len(audios)))
    if not videos:
        raise MediaInfoError("VIDEO_STREAM_MISSING", f"no video stream in {source_path}")
    format_name = fmt.get("format_name")
    if _missing(format_name) or not isinstance(format_name, str):
        raise MediaInfoError("PROBE_FIELD_MISSING", "missing required field: format.format_name", details={"field": "format.format_name"})
    return tuple(videos), tuple(audios), fmt


def parse_ffprobe_json(
    payload: Mapping[str, Any] | str | bytes,
    source_path: str | os.PathLike[str],
    *,
    ffprobe: ToolInfo | None = None,
    ffmpeg: ToolInfo | None = None,
    source_sha256_before: str | None = None,
) -> MediaInfo:
    source = Path(source_path).expanduser().resolve()
    if not source.is_file():
        raise MediaInfoError("SOURCE_NOT_FOUND", f"source media not found: {source}")
    videos, audios, fmt = _parse_payload(payload, source)
    errors: list[str] = []
    duration = _optional_fraction(fmt.get("duration"), field="format.duration", positive=True)
    start_time = _optional_fraction(fmt.get("start_time"), field="format.start_time")
    if duration is None:
        errors.append("format.duration_missing")
    if start_time is None:
        errors.append("format.start_time_missing")
    for stream in videos:
        errors.extend(stream.validation_errors)
    for stream in audios:
        errors.extend(stream.validation_errors)
    primary = videos[0]
    if primary.avg_frame_rate is None or primary.r_frame_rate is None:
        vfr_status = "unknown"
    elif primary.avg_frame_rate == primary.r_frame_rate:
        vfr_status = "rate_match"
    else:
        vfr_status = "rate_mismatch"
    if vfr_status == "unknown":
        errors.append("video.vfr_status_unknown")
    stat = source.stat()
    source_sha256 = _sha256_file(source)
    if source_sha256_before is not None and source_sha256 != source_sha256_before:
        raise MediaInfoError(
            "SOURCE_CHANGED_DURING_PROBE",
            "source media changed while probe was running",
            details={"before_sha256": source_sha256_before, "after_sha256": source_sha256},
        )
    return MediaInfo(
        source_path=source,
        source_sha256=source_sha256,
        source_size=stat.st_size,
        source_mtime_ns=stat.st_mtime_ns,
        format_name=str(fmt["format_name"]),
        format_long_name=_optional_text(fmt, "format_long_name"),
        duration=duration,
        start_time=start_time,
        video_streams=videos,
        audio_streams=audios,
        vfr_status=vfr_status,
        validation_errors=tuple(dict.fromkeys(errors)),
        ffprobe=ffprobe,
        ffmpeg=ffmpeg,
    )


def resolve_ffprobe_path(ffprobe_path: str | os.PathLike[str] | None = None, *, ffmpeg_path: str | os.PathLike[str] | None = None) -> Path:
    if ffprobe_path:
        requested = os.path.expanduser(str(ffprobe_path))
        candidate = Path(requested)
        if candidate.is_file():
            return candidate.resolve()
        raise MediaInfoError("FFPROBE_UNAVAILABLE", "requested ffprobe executable was not found", details={"requested": requested})
    if ffmpeg_path:
        ffmpeg = Path(os.path.expanduser(str(ffmpeg_path)))
        sibling = ffmpeg.with_name("ffprobe.exe" if ffmpeg.suffix.lower() == ".exe" else "ffprobe")
        if sibling.is_file():
            return sibling.resolve()
        raise MediaInfoError(
            "FFPROBE_UNAVAILABLE",
            "ffprobe was not found beside the explicitly selected ffmpeg",
            details={"ffmpeg_path": str(ffmpeg), "expected_ffprobe": str(sibling)},
        )
    bundled = _bundled_tool_path("ffprobe.exe")
    if bundled is not None:
        return bundled
    found = shutil.which("ffprobe")
    if found:
        candidate = Path(found)
        if candidate.is_file():
            return candidate.resolve()
    raise MediaInfoError("FFPROBE_UNAVAILABLE", "ffprobe executable was not found", details={"requested": str(ffprobe_path) if ffprobe_path else None})


def resolve_ffmpeg_path(ffmpeg_path: str | os.PathLike[str] | None = None) -> Path:
    if ffmpeg_path:
        requested = os.path.expanduser(str(ffmpeg_path))
        candidate = Path(requested)
        if candidate.is_file():
            return candidate.resolve()
        raise MediaInfoError("FFMPEG_UNAVAILABLE", f"requested ffmpeg executable was not found: {ffmpeg_path}")
    bundled = _bundled_tool_path("ffmpeg.exe")
    if bundled is not None:
        return bundled
    found = shutil.which("ffmpeg")
    if found:
        return Path(found).resolve()
    try:
        import imageio_ffmpeg  # type: ignore

        candidate = Path(imageio_ffmpeg.get_ffmpeg_exe())
        if candidate.is_file():
            return candidate.resolve()
    except Exception:
        pass
    raise MediaInfoError("FFMPEG_UNAVAILABLE", "ffmpeg executable was not found")


def _verify_tool(path: Path, *, execute: Callable[..., Any], timeout_seconds: float) -> ToolInfo:
    """Capture version and hash as one immutable executable provenance record."""
    before = _sha256_file(path)
    try:
        completed = execute(
            [str(path), "-version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise MediaInfoError("TOOL_VERSION_FAILED", f"could not verify tool version: {path}", details={"path": str(path)}) from exc
    if completed.returncode != 0:
        raise MediaInfoError("TOOL_VERSION_FAILED", f"tool -version failed: {path}", details={"returncode": completed.returncode})
    after = _sha256_file(path)
    if before != after:
        raise MediaInfoError("TOOL_CHANGED_DURING_PROBE", f"tool changed while being verified: {path}", details={"path": str(path)})
    version_output = str(getattr(completed, "stdout", "") or "")
    version_line = next((line.strip() for line in version_output.splitlines() if line.strip()), None)
    if not version_line:
        raise MediaInfoError("TOOL_VERSION_MISSING", f"tool returned no version text: {path}", details={"path": str(path)})
    return _tool_info(path, version_line=version_line, verified=True)


def _active_probe_backend() -> str:
    """Metadata probe backend selector.

    Defaults to the in-process PyAV reader (field-identical to ffprobe on all
    production samples); the ffprobe/ffmpeg tool pair is still verified for
    frame-PTS oracle binding. Set ARKNIGHT_MEDIA_PROBE=ffprobe to restore the
    CLI metadata spawn as a rollback.
    """
    return os.environ.get("ARKNIGHT_MEDIA_PROBE", "pyav").strip().lower()


def _round_to_microsecond(value: Fraction | None) -> Fraction | None:
    """Round a seconds value onto the microsecond grid.

    ffprobe prints stream/format times as %.6f decimals, so reproducing its
    MediaInfo field-for-field requires the same rounding rather than the exact
    duration_ts*time_base fraction PyAV exposes.
    """
    if value is None:
        return None
    scaled = value * 1_000_000
    nearest = int(scaled + Fraction(1, 2)) if scaled >= 0 else int(scaled - Fraction(1, 2))
    return Fraction(nearest, 1_000_000)


def _pyav_probe_payload(source: Path) -> dict:
    """Read container/stream metadata via PyAV and shape it like ffprobe JSON.

    Feeding the result to parse_ffprobe_json reuses every validation rule, so a
    PyAV probe yields a MediaInfo field-identical to the ffprobe path.
    """
    try:
        import av
    except Exception as exc:  # pragma: no cover - environment guard
        raise MediaInfoError("PYAV_UNAVAILABLE", f"PyAV is required for the pyav probe backend: {exc}") from exc

    container = av.open(str(source))
    try:
        streams: list[dict] = []
        for s in container.streams.video:
            cc = s.codec_context
            time_base = s.time_base
            start_s = None if (s.start_time is None or time_base is None) else s.start_time * time_base
            dur_s = None if (s.duration is None or time_base is None) else s.duration * time_base
            r_rate = getattr(s, "base_rate", None)
            if r_rate in (None, 0) or r_rate == Fraction(0, 1):
                r_rate = s.guessed_rate
            streams.append({
                "index": s.index,
                "codec_type": "video",
                "codec_name": cc.name,
                "codec_long_name": getattr(getattr(cc, "codec", None), "long_name", None),
                "width": cc.width,
                "height": cc.height,
                "pix_fmt": getattr(cc.format, "name", None),
                "time_base": time_base,
                "avg_frame_rate": s.average_rate,
                "r_frame_rate": r_rate,
                "nb_frames": s.frames if s.frames else None,
                "start_pts": s.start_time,
                "duration_ts": s.duration,
                "start_time": _round_to_microsecond(start_s),
                "duration": _round_to_microsecond(dur_s),
            })
        for s in container.streams.audio:
            cc = s.codec_context
            time_base = s.time_base
            start_s = None if (s.start_time is None or time_base is None) else s.start_time * time_base
            dur_s = None if (s.duration is None or time_base is None) else s.duration * time_base
            try:
                channels = cc.channels
            except Exception:
                channels = None
            if not channels:
                channels = getattr(getattr(cc, "layout", None), "nb_channels", None)
            layout = getattr(cc, "layout", None)
            streams.append({
                "index": s.index,
                "codec_type": "audio",
                "codec_name": cc.name,
                "codec_long_name": getattr(getattr(cc, "codec", None), "long_name", None),
                "sample_rate": getattr(cc, "rate", None),
                "channels": channels,
                "channel_layout": getattr(layout, "name", None) if layout is not None else None,
                "sample_fmt": getattr(cc.format, "name", None),
                "time_base": time_base,
                "bit_rate": cc.bit_rate,
                "start_pts": s.start_time,
                "duration_ts": s.duration,
                "start_time": _round_to_microsecond(start_s),
                "duration": _round_to_microsecond(dur_s),
            })
        fmt = container.format
        payload = {
            "streams": streams,
            "format": {
                "format_name": fmt.name,
                "format_long_name": fmt.long_name,
                "duration": None if container.duration is None else Fraction(container.duration, 1_000_000),
                "start_time": None if container.start_time is None else Fraction(container.start_time, 1_000_000),
            },
        }
        return payload
    finally:
        container.close()


def probe_media(
    source_path: str | os.PathLike[str],
    *,
    ffprobe_path: str | os.PathLike[str] | None = None,
    ffmpeg_path: str | os.PathLike[str] | None = None,
    timeout_seconds: float = 15.0,
    runner: Callable[..., Any] | None = None,
) -> MediaInfo:
    if _active_probe_backend() == "pyav":
        return probe_media_pyav(
            source_path,
            ffprobe_path=ffprobe_path,
            ffmpeg_path=ffmpeg_path,
            timeout_seconds=timeout_seconds,
            runner=runner,
        )
    source = Path(source_path).expanduser().resolve()
    if not source.is_file():
        raise MediaInfoError("SOURCE_NOT_FOUND", f"source media not found: {source}")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise MediaInfoError("PROBE_ARGUMENT_INVALID", "timeout_seconds must be finite and positive")
    ffmpeg = resolve_ffmpeg_path(ffmpeg_path)
    ffprobe = resolve_ffprobe_path(ffprobe_path, ffmpeg_path=ffmpeg)
    execute = runner or subprocess.run
    source_sha256_before = _sha256_file(source)
    tool_error: MediaInfoError | None = None
    try:
        ffprobe_tool = _verify_tool(ffprobe, execute=execute, timeout_seconds=timeout_seconds)
        ffmpeg_tool = _verify_tool(ffmpeg, execute=execute, timeout_seconds=timeout_seconds)
        _require_tool_pair(ffprobe_tool, ffmpeg_tool)
    except MediaInfoError as exc:
        # Still run the requested probe so media failures retain their specific
        # FFPROBE_* diagnostic instead of being masked by tool provenance.
        tool_error = exc
        ffprobe_tool = None
        ffmpeg_tool = None
    try:
        completed = execute(
            [str(ffprobe), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(source)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaInfoError("FFPROBE_TIMEOUT", "ffprobe did not finish before the timeout", details={"timeout_seconds": timeout_seconds, "path": str(ffprobe)}) from exc
    except OSError as exc:
        raise MediaInfoError("FFPROBE_EXECUTION_FAILED", f"ffprobe could not be started: {ffprobe}", details={"path": str(ffprobe), "os_error": str(exc)}) from exc
    if completed.returncode != 0:
        raise MediaInfoError(
            "FFPROBE_FAILED",
            f"ffprobe failed with return code {completed.returncode}",
            details={"stderr": (completed.stderr or "")[-2000:]},
        )
    if tool_error is not None:
        raise tool_error
    for tool in (ffprobe_tool, ffmpeg_tool):
        assert tool is not None
        if _sha256_file(tool.path) != tool.sha256:
            raise MediaInfoError(
                "TOOL_CHANGED_DURING_PROBE",
                f"tool changed after version verification: {tool.path}",
                details={"path": str(tool.path)},
            )
    output = completed.stdout
    if not output:
        raise MediaInfoError("PROBE_JSON_INVALID", "ffprobe returned empty JSON")
    return parse_ffprobe_json(
        output,
        source,
        ffprobe=ffprobe_tool,
        ffmpeg=ffmpeg_tool,
        source_sha256_before=source_sha256_before,
    )


def probe_media_pyav(
    source_path: str | os.PathLike[str],
    *,
    ffprobe_path: str | os.PathLike[str] | None = None,
    ffmpeg_path: str | os.PathLike[str] | None = None,
    timeout_seconds: float = 15.0,
    runner: Callable[..., Any] | None = None,
) -> MediaInfo:
    """Build the same MediaInfo as probe_media, reading metadata via PyAV.

    The ffprobe/ffmpeg tool pair is still resolved, hashed and verified exactly
    as in the CLI path, because frame-PTS certification binds both executables.
    Only the metadata read itself moves from spawning ffprobe to PyAV.
    """
    source = Path(source_path).expanduser().resolve()
    if not source.is_file():
        raise MediaInfoError("SOURCE_NOT_FOUND", f"source media not found: {source}")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise MediaInfoError("PROBE_ARGUMENT_INVALID", "timeout_seconds must be finite and positive")
    ffmpeg = resolve_ffmpeg_path(ffmpeg_path)
    ffprobe = resolve_ffprobe_path(ffprobe_path, ffmpeg_path=ffmpeg)
    execute = runner or subprocess.run
    source_sha256_before = _sha256_file(source)
    tool_error: MediaInfoError | None = None
    try:
        ffprobe_tool = _verify_tool(ffprobe, execute=execute, timeout_seconds=timeout_seconds)
        ffmpeg_tool = _verify_tool(ffmpeg, execute=execute, timeout_seconds=timeout_seconds)
        _require_tool_pair(ffprobe_tool, ffmpeg_tool)
    except MediaInfoError as exc:
        tool_error = exc
        ffprobe_tool = None
        ffmpeg_tool = None
    try:
        payload = _pyav_probe_payload(source)
    except MediaInfoError:
        raise
    except Exception as exc:
        raise MediaInfoError(
            "PYAV_PROBE_FAILED",
            f"PyAV metadata probe failed: {source}",
            details={"error": str(exc)},
        ) from exc
    if tool_error is not None:
        raise tool_error
    for tool in (ffprobe_tool, ffmpeg_tool):
        assert tool is not None
        if _sha256_file(tool.path) != tool.sha256:
            raise MediaInfoError(
                "TOOL_CHANGED_DURING_PROBE",
                f"tool changed after version verification: {tool.path}",
                details={"path": str(tool.path)},
            )
    return parse_ffprobe_json(
        payload,
        source,
        ffprobe=ffprobe_tool,
        ffmpeg=ffmpeg_tool,
        source_sha256_before=source_sha256_before,
    )
