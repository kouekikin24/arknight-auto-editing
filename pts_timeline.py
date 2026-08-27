"""Exact mapping from certified source frames to media timestamp intervals.

This module deliberately performs no FPS arithmetic.  It accepts only a
current :class:`~media_info.FramePtsCertification`, preserves its integer PTS
ticks, and maps half-open source-frame ranges to half-open tick intervals.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import operator
import re
from typing import Sequence

from media_info import (
    FramePtsCertification,
    MediaInfo,
    MediaInfoError,
    ToolInfo,
    _check_pts_monotonic_head_tolerant,
    _load_certification_evidence,
)
from timeline_plan import TimelinePlan


def _strict_index(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise MediaInfoError(
            "FRAME_RANGE_INVALID",
            f"{name} must be an integer, not bool",
            details={"field": name},
        )
    try:
        return operator.index(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MediaInfoError(
            "FRAME_RANGE_INVALID",
            f"{name} must be an integer",
            details={"field": name},
        ) from exc


@dataclass(frozen=True, slots=True)
class FramePtsRow:
    """The timestamp fields required to address one decoded source frame."""

    n: int
    pts: int
    duration: int

    def __post_init__(self) -> None:
        n = _strict_index(self.n, "n")
        pts = _strict_index(self.pts, "pts")
        duration = _strict_index(self.duration, "duration")
        if n < 0:
            raise MediaInfoError("FRAME_PTS_ROW_INVALID", "frame row n must be non-negative")
        if duration <= 0:
            raise MediaInfoError("FRAME_PTS_ROW_INVALID", "frame row duration must be positive")
        object.__setattr__(self, "n", n)
        object.__setattr__(self, "pts", pts)
        object.__setattr__(self, "duration", duration)

    @property
    def end_tick(self) -> int:
        return self.pts + self.duration


@dataclass(frozen=True, slots=True)
class PtsTickInterval:
    """One half-open source-frame range and its exact media-time interval."""

    start_frame: int
    end_frame: int
    start_tick: int
    end_tick: int
    time_base: Fraction

    def __post_init__(self) -> None:
        start_frame = _strict_index(self.start_frame, "start_frame")
        end_frame = _strict_index(self.end_frame, "end_frame")
        start_tick = _strict_index(self.start_tick, "start_tick")
        end_tick = _strict_index(self.end_tick, "end_tick")
        object.__setattr__(self, "start_frame", start_frame)
        object.__setattr__(self, "end_frame", end_frame)
        object.__setattr__(self, "start_tick", start_tick)
        object.__setattr__(self, "end_tick", end_tick)
        if self.start_frame < 0 or self.end_frame <= self.start_frame:
            raise MediaInfoError(
                "FRAME_RANGE_INVALID",
                "PTS intervals require a non-empty half-open frame range",
            )
        if self.end_tick <= self.start_tick:
            raise MediaInfoError(
                "FRAME_PTS_INTERVAL_INVALID",
                "PTS interval end must be greater than its start",
                details={
                    "start_tick": self.start_tick,
                    "end_tick": self.end_tick,
                },
            )
        if not isinstance(self.time_base, Fraction) or self.time_base <= 0:
            raise MediaInfoError(
                "FRAME_PTS_TIME_BASE_INVALID",
                "PTS interval time_base must be a positive Fraction",
            )

    @property
    def duration_ticks(self) -> int:
        return self.end_tick - self.start_tick

    @property
    def start_time(self) -> Fraction:
        return self.start_tick * self.time_base

    @property
    def end_time(self) -> Fraction:
        return self.end_tick * self.time_base

    @property
    def duration_time(self) -> Fraction:
        return self.duration_ticks * self.time_base

    def as_dict(self) -> dict[str, object]:
        return {
            "source_frame_range": [self.start_frame, self.end_frame],
            "pts_tick_range": [self.start_tick, self.end_tick],
            "duration_ticks": self.duration_ticks,
            "time_base": f"{self.time_base.numerator}/{self.time_base.denominator}",
            "interval_semantics": "half-open [start, end)",
        }


@dataclass(frozen=True, slots=True)
class CertifiedPtsTimeline:
    """Immutable, source-bound view of a certified decoded-frame PTS table."""

    source_sha256: str
    status: str
    time_base: Fraction
    pts_table_sha256: str
    evidence_sha256: str
    rows: tuple[FramePtsRow, ...]
    head_anomaly_limit: int = 0

    def __post_init__(self) -> None:
        if self.status not in {"cfr", "vfr"}:
            raise MediaInfoError("FRAME_PTS_STATUS_INVALID", "timeline status must be cfr or vfr")
        if not isinstance(self.time_base, Fraction) or self.time_base <= 0:
            raise MediaInfoError(
                "FRAME_PTS_TIME_BASE_INVALID",
                "timeline time_base must be a positive Fraction",
            )
        limit = self.head_anomaly_limit
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise MediaInfoError(
                "FRAME_PTS_EVIDENCE_INVALID",
                "head_anomaly_limit must be a non-negative integer",
            )
        object.__setattr__(self, "head_anomaly_limit", int(limit))
        for field_name in ("source_sha256", "pts_table_sha256", "evidence_sha256"):
            value = str(getattr(self, field_name)).lower()
            if not re.fullmatch(r"[0-9a-f]{64}", value):
                raise MediaInfoError(
                    "FRAME_PTS_CERTIFICATION_INVALID",
                    f"{field_name} must be a SHA-256 hex string",
                    details={"field": field_name},
                )
            object.__setattr__(self, field_name, value)
        if not isinstance(self.rows, tuple) or not self.rows:
            raise MediaInfoError(
                "FRAME_PTS_EVIDENCE_INCOMPLETE",
                "certified PTS timeline requires at least one frame row",
            )
        for expected, row in enumerate(self.rows):
            if not isinstance(row, FramePtsRow) or row.n != expected:
                raise MediaInfoError(
                    "FRAME_PTS_EVIDENCE_INVALID",
                    "certified PTS timeline rows must be contiguous FramePtsRow values",
                    details={"row": expected},
                )
        _check_pts_monotonic_head_tolerant(
            ((row.n, row.pts) for row in self.rows),
            head_anomaly_limit=self.head_anomaly_limit,
        )

    @classmethod
    def from_certification(
        cls,
        certification: FramePtsCertification,
        *,
        expected_source_sha256: str | None = None,
        expected_ffmpeg: ToolInfo | None = None,
    ) -> "CertifiedPtsTimeline":
        if not isinstance(certification, FramePtsCertification):
            raise MediaInfoError(
                "FRAME_PTS_CERTIFICATION_REQUIRED",
                "a complete FramePtsCertification is required",
            )
        if (
            expected_source_sha256 is not None
            and certification.source_sha256 != str(expected_source_sha256).lower()
        ):
            raise MediaInfoError(
                "FRAME_PTS_SOURCE_MISMATCH",
                "frame PTS certification belongs to a different source",
                details={
                    "expected": str(expected_source_sha256).lower(),
                    "actual": certification.source_sha256,
                },
            )

        raw_rows, head_anomaly_limit = _load_certification_evidence(
            certification,
            expected_ffmpeg=expected_ffmpeg,
        )
        rows = tuple(
            FramePtsRow(
                n=int(row["n"]),
                pts=int(row["pts"]),
                duration=int(row["duration"]),
            )
            for row in raw_rows
        )
        if len(rows) != certification.frame_count:
            raise MediaInfoError(
                "FRAME_PTS_FRAME_COUNT_MISMATCH",
                "certified PTS row count changed while building the timeline",
                details={
                    "certified": certification.frame_count,
                    "actual": len(rows),
                },
            )
        return cls(
            source_sha256=certification.source_sha256,
            status=certification.status,
            time_base=certification.time_base,
            pts_table_sha256=certification.pts_table_sha256,
            evidence_sha256=certification.evidence_sha256,
            rows=rows,
            head_anomaly_limit=head_anomaly_limit,
        )

    @classmethod
    def from_media_info(cls, media: MediaInfo) -> "CertifiedPtsTimeline":
        """Require the full production boundary before exposing PTS rows."""
        if not isinstance(media, MediaInfo):
            raise MediaInfoError(
                "MEDIA_INFO_REQUIRED",
                "a MediaInfo snapshot is required for a certified PTS timeline",
            )
        if not media.complete_for_export or media.frame_pts_certification is None:
            raise MediaInfoError(
                "MEDIA_INFO_NOT_EXPORT_READY",
                "MediaInfo is not bound to a current complete FramePtsCertification",
            )
        media.assert_source_current()
        return cls.from_validated_media_info(media)

    @classmethod
    def from_validated_media_info(cls, media: MediaInfo) -> "CertifiedPtsTimeline":
        """Build a timeline after the caller has completed the MediaInfo gate.

        This narrow entry point is for a validation snapshot that has already
        evaluated ``MediaInfo.complete_for_export``.  It still revalidates and
        loads the immutable evidence binding, but does not repeat the full
        source/tool readiness check.
        """
        if not isinstance(media, MediaInfo):
            raise MediaInfoError(
                "MEDIA_INFO_REQUIRED",
                "a MediaInfo snapshot is required for a certified PTS timeline",
            )
        certification = media.frame_pts_certification
        if certification is None or not media.frame_pts_authoritative:
            raise MediaInfoError(
                "MEDIA_INFO_NOT_EXPORT_READY",
                "MediaInfo is not bound to a current complete FramePtsCertification",
            )
        # The exporter calls this only after its one-time readiness check.  A
        # second source/tool readiness pass here would defeat the purpose of
        # the snapshot.
        return cls.from_certification(
            certification,
            expected_source_sha256=media.source_sha256,
            expected_ffmpeg=media.ffmpeg,
        )

    @property
    def frame_count(self) -> int:
        return len(self.rows)

    def interval_for_range(self, frame_range: Sequence[int]) -> PtsTickInterval:
        try:
            start_raw, end_raw = frame_range
        except (TypeError, ValueError) as exc:
            raise MediaInfoError(
                "FRAME_RANGE_INVALID",
                "frame range must contain exactly two values",
            ) from exc
        start = _strict_index(start_raw, "start_frame")
        end = _strict_index(end_raw, "end_frame")
        if start < 0 or end > self.frame_count or end <= start:
            raise MediaInfoError(
                "FRAME_RANGE_OUT_OF_BOUNDS",
                f"frame range [{start}, {end}) is outside [0, {self.frame_count})",
                details={
                    "start": start,
                    "end": end,
                    "frame_count": self.frame_count,
                },
            )

        first = self.rows[start]
        last = self.rows[end - 1]
        return PtsTickInterval(
            start_frame=start,
            end_frame=end,
            start_tick=first.pts,
            end_tick=last.end_tick,
            time_base=self.time_base,
        )

    def intervals_for_plan(self, plan: TimelinePlan) -> tuple[PtsTickInterval, ...]:
        if not isinstance(plan, TimelinePlan):
            raise MediaInfoError(
                "TIMELINE_PLAN_REQUIRED",
                "PTS mapping requires a TimelinePlan",
            )
        if plan.total_frames != self.frame_count:
            raise MediaInfoError(
                "FRAME_PTS_FRAME_COUNT_MISMATCH",
                "certified frame count does not match the timeline plan",
                details={
                    "certified": self.frame_count,
                    "timeline": plan.total_frames,
                },
            )
        return tuple(self.interval_for_range(value) for value in plan.kept_ranges)
