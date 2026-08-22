"""Build preview EDL files from an immutable certified tick timeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from bisect import bisect_left
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid

from media_info import MediaInfo, MediaInfoError
from preview_engine import CertifiedEdlRequest, PreviewEngineError
from pts_timeline import CertifiedPtsTimeline, FramePtsRow


# B' is deliberately narrow.  A single kept head frame can be
# unrepresentable when it shares a tick with the first deleted frame; retain
# that fact as preview metadata rather than pretending the EDL is pixel exact.
_PREVIEW_HEAD_TICK_COLLISION_MAX = 1


def _fraction_seconds(value: Fraction) -> str:
    if not isinstance(value, Fraction) or value.denominator <= 0:
        raise ValueError("EDL time must be a positive-denominator Fraction")
    # Decimal rendering avoids float rounding while remaining accepted by mpv.
    from decimal import Decimal, localcontext

    with localcontext() as context:
        context.prec = 50
        rendered = format(
            Decimal(value.numerator) / Decimal(value.denominator),
            "f",
        )
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def edl_escape(path: str | os.PathLike[str]) -> str:
    """Encode an EDL path using mpv's UTF-8 byte-count form."""

    value = Path(path).expanduser().resolve().as_posix()
    return f"%{len(value.encode('utf-8'))}%{value}"


@dataclass(frozen=True, slots=True)
class CertifiedEdlSegment:
    source_frame_range: tuple[int, int]
    pts_tick_range: tuple[int, int]
    virtual_time_range: tuple[Fraction, Fraction]

    @property
    def start_time(self) -> Fraction:
        return self.virtual_time_range[0]

    @property
    def duration(self) -> Fraction:
        return self.virtual_time_range[1] - self.virtual_time_range[0]


@dataclass(frozen=True, slots=True)
class CertifiedEdl:
    """Immutable EDL plus the mapping needed to display source positions."""

    path: Path
    source_path: Path
    source_sha256: str
    pts_table_sha256: str
    evidence_sha256: str
    plan_fingerprint: str
    time_base: Fraction
    segments: tuple[CertifiedEdlSegment, ...]
    content_sha256: str
    _rows: tuple[FramePtsRow, ...] = field(repr=False, compare=False)
    _segment_pts: tuple[tuple[int, ...], ...] = field(repr=False, compare=False)
    # B' accepts a bounded, source-head PTS anomaly for preview only.  Keep
    # the adjudication visible on the immutable object so callers cannot
    # mistake this EDL for a pixel-exact export timeline.
    preview_bias_frames: int = 0
    preview_bias_reason: str | None = None
    preview_tick_collision_frames: tuple[int, ...] = ()

    @property
    def frame_count(self) -> int:
        return len(self._rows)

    @property
    def expected_frames(self) -> int:
        return sum(end - start for start, end in (s.source_frame_range for s in self.segments))

    @property
    def duration(self) -> Fraction:
        if not self.segments:
            return Fraction(0, 1)
        return self.segments[-1].virtual_time_range[1]

    def virtual_time_for_source(self, source_frame: int, *, snap: bool = False) -> Fraction:
        """Map a source frame into EDL time, optionally snapping deleted frames."""

        if isinstance(source_frame, bool) or not isinstance(source_frame, int):
            raise TypeError("source_frame must be an integer")
        if source_frame < 0 or source_frame >= len(self._rows):
            raise PreviewEngineError(
                "SOURCE_FRAME_OUT_OF_RANGE",
                "source frame is outside the certified timeline",
                details={"source_frame": source_frame, "frame_count": len(self._rows)},
            )
        if source_frame in self.preview_tick_collision_frames:
            if not snap:
                raise PreviewEngineError(
                    "SOURCE_FRAME_TICK_AMBIGUOUS",
                    "source frame shares an unrepresentable EDL tick with a cut boundary",
                    details={"source_frame": source_frame},
                )
            for segment in self.segments:
                start, end = segment.source_frame_range
                if start <= source_frame < end:
                    # The ambiguous row lies at or outside this segment's
                    # tick boundary.  Snapping forward therefore means the
                    # following EDL boundary, never a later decode row whose
                    # anomalous PTS happens to be earlier in virtual time.
                    return segment.virtual_time_range[1]
            return self.duration
        for segment in self.segments:
            start, end = segment.source_frame_range
            if start <= source_frame < end:
                return segment.start_time + self._offset_in_segment(segment, source_frame)
        if not snap:
            raise PreviewEngineError(
                "SOURCE_FRAME_DELETED",
                "deleted source frame has no EDL time; request an explicit snap",
                details={"source_frame": source_frame},
            )
        for segment in self.segments:
            if source_frame < segment.source_frame_range[0]:
                return segment.start_time
        if self.segments:
            last = self.segments[-1]
            return last.virtual_time_range[1]
        raise PreviewEngineError("EDL_EMPTY", "the EDL contains no kept frames")

    def source_frame_for_virtual_time(self, value: Fraction | int) -> int:
        """Resolve an EDL time using certified row PTS, never FPS arithmetic."""

        if not self.segments:
            raise PreviewEngineError("EDL_EMPTY", "the EDL contains no kept ranges")
        if isinstance(value, bool) or not isinstance(value, (Fraction, int)):
            raise TypeError("virtual EDL time must be an integer or Fraction")
        if not isinstance(value, Fraction):
            value = Fraction(value, 1)
        if value < 0:
            value = Fraction(0, 1)
        for segment_index, segment in enumerate(self.segments):
            virtual_start, virtual_end = segment.virtual_time_range
            if value < virtual_end or segment is self.segments[-1]:
                target_tick = segment.pts_tick_range[0] + int(
                    (value - virtual_start) / self.time_base
                )
                return self._nearest_row_in_segment(segment_index, segment, target_tick)
        return self.segments[-1].source_frame_range[1] - 1

    def _offset_in_segment(self, segment: CertifiedEdlSegment, frame: int) -> Fraction:
        # The row offset is derived from the certified PTS table encoded in the
        # segment's tick range.  Segment boundaries are exact; individual row
        # lookup is reconstructed from the cached timeline rows below.
        start, end = segment.source_frame_range
        if frame == start:
            return Fraction(0, 1)
        # Rows are not stored twice in the public record.  The segment tick
        # range has enough information for the start, while this helper is
        # replaced by the builder's private row map on instances.
        rows = self._rows
        return (rows[frame].pts - segment.pts_tick_range[0]) * self.time_base

    def _nearest_row_in_segment(
        self,
        segment_index: int,
        segment: CertifiedEdlSegment,
        target_tick: int,
    ) -> int:
        rows = self._rows
        start, end = segment.source_frame_range
        values = self._segment_pts[segment_index]
        if all(left <= right for left, right in zip(values, values[1:])):
            offset = bisect_left(values, target_tick)
            if offset < len(values) and values[offset] == target_tick:
                return start + offset
            # Frame ownership follows a half-open PTS interval: use the last
            # frame whose start tick is not after the requested time.  A
            # nearest-PTS choice jumps to the next VFR frame halfway through a
            # long duration and makes the editing pointer lie about the frame
            # currently visible in mpv.
            return start + max(0, min(len(values) - 1, offset - 1))
        # B' only permits a short anomalous head.  Keep the fallback explicit
        # rather than sorting or deduplicating the certified rows.
        return min(range(start, end), key=lambda index: abs(rows[index].pts - target_tick))


def _write_once(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise PreviewEngineError(
                "EDL_PATH_COLLISION",
                "an existing EDL path contains different content",
                details={"path": str(path)},
            )
        return
    temporary: Path | None = None
    try:
        fd, name = tempfile.mkstemp(
            prefix=f".{path.name}.{uuid.uuid4().hex}.",
            suffix=".tmp",
            dir=str(path.parent),
            text=True,
        )
        temporary = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_text(encoding="utf-8") != content:
                raise PreviewEngineError(
                    "EDL_PATH_COLLISION",
                    "an EDL publish raced with different content",
                    details={"path": str(path)},
                )
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _head_tick_collision_frames(
    timeline: CertifiedPtsTimeline,
    intervals,
) -> tuple[int, ...]:
    """Return source rows a tick-only EDL cannot address independently.

    The EDL line operates on a half-open tick span, while the business plan
    operates on source-frame spans.  A kept row outside its own tick span, or a
    deleted row inside a kept tick span, shares a boundary tick with content
    on the other side of a cut. B' allows one such recording-head row only
    when it lies inside the certified head window; all other cases are rejected
    before a preview artifact is written.
    """

    collisions: list[int] = []
    kept = [False] * timeline.frame_count
    for interval in intervals:
        for frame in range(interval.start_frame, interval.end_frame):
            kept[frame] = True
            pts = timeline.rows[frame].pts
            if pts < interval.start_tick or pts >= interval.end_tick:
                collisions.append(frame)
    # A deleted row at a later segment's start tick can be pulled into that
    # segment.  This adjacent equal-boundary ambiguity is invisible to a
    # strict ``start < previous_end`` check alone.  Scanning every row for
    # every interval is O(intervals x frames), which is unusable on real
    # sources (thousands of kept ranges over hundreds of thousands of frames).
    # The certification guarantees strictly increasing PTS outside the
    # adjudicated head window, so one ascending merge over the rows reaches
    # exactly the same rows.
    limit = timeline.head_anomaly_limit
    starts = [interval.start_tick for interval in intervals]
    ends = [interval.end_tick for interval in intervals]
    opened: list[int] = []
    next_interval = 0
    for frame, row in enumerate(timeline.rows):
        pts = row.pts
        # The certification boundary already enforces strictly increasing PTS
        # outside the adjudicated head window, so once the ascending walk
        # passes an interval's end_tick that interval can never match a later
        # row again.  Each opened interval is therefore closed independently
        # by the single ``pts < ends[index]`` test.
        while next_interval < len(intervals) and starts[next_interval] <= pts:
            opened.append(next_interval)
            next_interval += 1
        kept_row = kept[frame]
        remaining: list[int] = []
        for index in opened:
            if pts < ends[index]:
                remaining.append(index)
                if not kept_row:
                    collisions.append(frame)
        opened = remaining
    collisions = sorted(set(collisions))
    if not collisions:
        return ()
    limit = timeline.head_anomaly_limit
    if (
        limit <= 0
        or len(collisions) > _PREVIEW_HEAD_TICK_COLLISION_MAX
        or any(frame >= limit for frame in collisions)
    ):
        raise PreviewEngineError(
            "FRAME_PTS_TICK_COLLISION_UNADJUDICATED",
            "a tick-only preview EDL cannot represent the requested cut exactly",
            details={
                "frames": collisions,
                "head_anomaly_limit": limit,
                "allowed_collisions": _PREVIEW_HEAD_TICK_COLLISION_MAX,
            },
        )
    return tuple(collisions)


def build_certified_edl(
    request: CertifiedEdlRequest,
    output_dir: str | os.PathLike[str],
    *,
    timeline: CertifiedPtsTimeline | None = None,
) -> CertifiedEdl:
    """Build or reuse one EDL whose intervals come solely from certified PTS.

    ``timeline`` lets a caller that has already bound and validated the
    certified timeline (the preview engine, which stat-checks the source on
    every command) skip the per-call multi-gigabyte source hash and the
    evidence re-derivation.  The default path re-validates everything and
    remains the fail-closed route for exporters.
    """

    media = request.media_info
    certification = media.frame_pts_certification
    if timeline is None:
        try:
            media.assert_source_current()
        except MediaInfoError as exc:
            raise PreviewEngineError(exc.code, str(exc), details=exc.details) from exc
        if not media.complete_for_export or certification is None:
            raise PreviewEngineError(
                "CERTIFICATION_REQUIRED",
                "certified PTS media is required before building an EDL",
            )
    else:
        # complete_for_export re-hashes the source; the caller vouched for
        # the full gate when it bound this timeline, so only the cheap
        # certification identity is re-checked here.
        if certification is None:
            raise PreviewEngineError(
                "CERTIFICATION_REQUIRED",
                "certified PTS media is required before building an EDL",
            )
        if (
            timeline.frame_count != certification.frame_count
            or timeline.time_base != certification.time_base
        ):
            raise PreviewEngineError(
                "TIMELINE_MISMATCH",
                "caller-provided certified timeline does not match the request certification",
                details={
                    "timeline_frames": timeline.frame_count,
                    "certification_frames": certification.frame_count,
                    "timeline_time_base": str(timeline.time_base),
                    "certification_time_base": str(certification.time_base),
                },
            )
    plan = request.timeline_plan
    if plan.total_frames != certification.frame_count:
        raise PreviewEngineError(
            "FRAME_COUNT_MISMATCH",
            "timeline plan and certified PTS frame count differ",
            details={"plan": plan.total_frames, "certified": certification.frame_count},
        )
    if timeline is None:
        try:
            timeline = CertifiedPtsTimeline.from_media_info(media)
        except MediaInfoError as exc:
            raise PreviewEngineError(exc.code, str(exc), details=exc.details) from exc
    try:
        intervals = timeline.intervals_for_plan(plan)
    except MediaInfoError as exc:
        raise PreviewEngineError(exc.code, str(exc), details=exc.details) from exc
    if not intervals:
        raise PreviewEngineError("EDL_EMPTY", "cannot build an EDL with no kept ranges")
    previous_end_tick: int | None = None
    previous_end_frame: int | None = None
    for interval in intervals:
        if (
            interval.start_frame < 0
            or interval.end_frame > timeline.frame_count
            or interval.end_frame <= interval.start_frame
        ):
            raise PreviewEngineError(
                "FRAME_INTERVAL_OUT_OF_BOUNDS",
                "certified EDL interval is outside the source frame table",
                details={
                    "start_frame": interval.start_frame,
                    "end_frame": interval.end_frame,
                    "frame_count": timeline.frame_count,
                },
            )
        if interval.time_base != timeline.time_base:
            raise PreviewEngineError(
                "PTS_TIME_BASE_MISMATCH",
                "certified EDL interval uses a different time base",
                details={
                    "interval_time_base": str(interval.time_base),
                    "timeline_time_base": str(timeline.time_base),
                },
            )
        if previous_end_frame is not None and interval.start_frame < previous_end_frame:
            raise PreviewEngineError(
                "FRAME_INTERVAL_OVERLAP",
                "kept EDL frame intervals overlap",
                details={
                    "previous_end_frame": previous_end_frame,
                    "start_frame": interval.start_frame,
                },
            )
        if previous_end_tick is not None and interval.start_tick < previous_end_tick:
            raise PreviewEngineError(
                "PTS_INTERVAL_OVERLAP",
                "kept EDL intervals overlap in certified tick space",
                details={
                    "previous_end_tick": previous_end_tick,
                    "start_tick": interval.start_tick,
                },
            )
        previous_end_tick = interval.end_tick
        previous_end_frame = interval.end_frame

    collision_frames = _head_tick_collision_frames(timeline, intervals)

    segments: list[CertifiedEdlSegment] = []
    virtual_cursor = Fraction(0, 1)
    bias_frames = int(getattr(timeline, "head_anomaly_limit", 0) or 0)
    bias_reasons: list[str] = []
    if bias_frames:
        bias_reasons.append(
            "B' certified head PTS anomalies; preview may lag at content transitions"
        )
    if collision_frames:
        bias_reasons.append(
            "tick-only EDL cannot independently address source frame(s) "
            + ", ".join(str(frame) for frame in collision_frames)
        )
    bias_reason = "; ".join(bias_reasons) or None
    lines = ["# mpv EDL v0"]
    if bias_frames:
        lines.append(f"# arknight-preview-bias-frames: {bias_frames}")
        lines.append(f"# arknight-preview-bias-reason: {bias_reason}")
    if collision_frames:
        lines.append(
            "# arknight-preview-tick-collision-frames: "
            + ",".join(str(frame) for frame in collision_frames)
        )
    escaped = edl_escape(media.source_path)
    for interval in intervals:
        virtual_start = virtual_cursor
        virtual_cursor += interval.duration_time
        segment = CertifiedEdlSegment(
            source_frame_range=(interval.start_frame, interval.end_frame),
            pts_tick_range=(interval.start_tick, interval.end_tick),
            virtual_time_range=(virtual_start, virtual_cursor),
        )
        segments.append(segment)
        lines.append(
            f"{escaped},{_fraction_seconds(interval.start_time)},"
            f"{_fraction_seconds(interval.duration_time)}"
        )
    content = "\n".join(lines) + "\n"
    identity = hashlib.sha256(
        json.dumps(
            {
                "preview_edl_schema": 2,
                "source_sha256": media.source_sha256,
                "pts_table_sha256": certification.pts_table_sha256,
                "evidence_sha256": certification.evidence_sha256,
                "time_base": f"{timeline.time_base.numerator}/{timeline.time_base.denominator}",
                "frame_count": timeline.frame_count,
                "plan_fingerprint": plan.fingerprint,
                "tick_collision_frames": collision_frames,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:24]
    root = Path(output_dir).expanduser().resolve()
    path = root / f"preview-{identity}.edl"
    _write_once(path, content)
    result = CertifiedEdl(
        path=path,
        source_path=media.source_path,
        source_sha256=media.source_sha256,
        pts_table_sha256=certification.pts_table_sha256,
        evidence_sha256=certification.evidence_sha256,
        plan_fingerprint=plan.fingerprint,
        time_base=timeline.time_base,
        segments=tuple(segments),
        content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        _rows=timeline.rows,
        _segment_pts=tuple(
            tuple(timeline.rows[start].pts for start in range(*segment.source_frame_range))
            for segment in segments
        ),
        preview_bias_frames=bias_frames,
        preview_bias_reason=bias_reason,
        preview_tick_collision_frames=collision_frames,
    )
    return result


__all__ = [
    "CertifiedEdl",
    "CertifiedEdlSegment",
    "build_certified_edl",
    "edl_escape",
]
