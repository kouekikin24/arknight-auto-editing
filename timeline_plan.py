"""Authoritative source-frame timeline primitives.

The rest of the application may use different playback or export engines, but
they must agree on this representation: frame ranges are half-open
``[start, end)`` intervals in source-frame coordinates.  This module is kept
free of OpenCV, Tk and FFmpeg so it can be tested independently.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
import hashlib
import operator
from typing import Iterable, Sequence


Range = tuple[int, int]


def _as_int(value: object, name: str) -> int:
    try:
        result = operator.index(value)  # supports NumPy integer scalars
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    return result


def _canonical_ranges(total_frames: int, ranges: Iterable[Sequence[int]]) -> tuple[Range, ...]:
    """Validate, sort and union half-open ranges.

    Empty ranges are ignored.  Out-of-bounds ranges are rejected instead of
    silently clamped; a malformed edit must not change the user's timeline.
    """
    total = _as_int(total_frames, "total_frames")
    if total < 0:
        raise ValueError("total_frames must be non-negative")

    pending: list[Range] = []
    for index, value in enumerate(ranges):
        try:
            start_raw, end_raw = value
        except (TypeError, ValueError) as exc:
            raise ValueError(f"range {index} must contain exactly two values") from exc
        start = _as_int(start_raw, f"range {index} start")
        end = _as_int(end_raw, f"range {index} end")
        if start < 0 or end < 0 or start > total or end > total:
            raise ValueError(f"range {index} [{start}, {end}) is outside [0, {total})")
        if end < start:
            raise ValueError(f"range {index} has end before start: [{start}, {end})")
        if start != end:
            pending.append((start, end))

    if not pending:
        return ()
    pending.sort()
    merged: list[Range] = [pending[0]]
    for start, end in pending[1:]:
        old_start, old_end = merged[-1]
        if start <= old_end:  # overlap or adjacency: one canonical run
            merged[-1] = (old_start, max(old_end, end))
        else:
            merged.append((start, end))
    return tuple(merged)


def _complement(total_frames: int, deleted: Sequence[Range]) -> tuple[Range, ...]:
    cursor = 0
    kept: list[Range] = []
    for start, end in deleted:
        if cursor < start:
            kept.append((cursor, start))
        cursor = end
    if cursor < total_frames:
        kept.append((cursor, total_frames))
    return tuple(kept)


@dataclass(frozen=True, slots=True)
class TimelinePlan:
    """A validated mapping between source frames and a virtual keep timeline."""

    total_frames: int
    deleted_ranges: tuple[Range, ...]
    kept_ranges: tuple[Range, ...] = field(init=False)
    virtual_prefix: tuple[int, ...] = field(init=False)
    fingerprint: str = field(init=False)
    _keep_starts: tuple[int, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        total = _as_int(self.total_frames, "total_frames")
        if total < 0:
            raise ValueError("total_frames must be non-negative")
        deleted = _canonical_ranges(total, self.deleted_ranges)
        kept = _complement(total, deleted)
        expected_prefix = [0]
        for start, end in kept:
            expected_prefix.append(expected_prefix[-1] + end - start)
        prefix = tuple(expected_prefix)
        starts = tuple(start for start, _ in kept)
        object.__setattr__(self, "total_frames", total)
        object.__setattr__(self, "deleted_ranges", deleted)
        object.__setattr__(self, "kept_ranges", kept)
        object.__setattr__(self, "virtual_prefix", prefix)
        object.__setattr__(self, "_keep_starts", starts)
        object.__setattr__(self, "fingerprint", self._make_fingerprint(total, deleted))

    @classmethod
    def from_deleted_ranges(
        cls, total_frames: int, deleted_ranges: Iterable[Sequence[int]]
    ) -> "TimelinePlan":
        total = _as_int(total_frames, "total_frames")
        deleted = _canonical_ranges(total, deleted_ranges)
        return cls(total, deleted)

    @classmethod
    def from_kept_ranges(
        cls, total_frames: int, kept_ranges: Iterable[Sequence[int]]
    ) -> "TimelinePlan":
        """Build a plan from source ranges that should be retained.

        This is the companion to :meth:`from_deleted_ranges` for exporters
        and segment writers.  The input uses the same strict half-open
        contract and is canonicalized before its complement is calculated.
        """
        total = _as_int(total_frames, "total_frames")
        kept = _canonical_ranges(total, kept_ranges)
        deleted = _complement(total, kept)
        return cls(total, deleted)

    @classmethod
    def from_delete_mask(cls, mask: Iterable[object]) -> "TimelinePlan":
        values = tuple(bool(value) for value in mask)
        ranges: list[Range] = []
        index = 0
        while index < len(values):
            if not values[index]:
                index += 1
                continue
            start = index
            index += 1
            while index < len(values) and values[index]:
                index += 1
            ranges.append((start, index))
        return cls.from_deleted_ranges(len(values), ranges)

    @staticmethod
    def _make_fingerprint(total_frames: int, deleted: Sequence[Range]) -> str:
        payload = ",".join(
            [str(total_frames), *(f"{start}:{end}" for start, end in deleted)]
        ).encode("ascii")
        return hashlib.sha256(payload).hexdigest()

    @property
    def deleted_frames(self) -> int:
        return sum(end - start for start, end in self.deleted_ranges)

    @property
    def kept_frames(self) -> int:
        return self.virtual_prefix[-1] if self.virtual_prefix else 0

    @property
    def virtual_duration_frames(self) -> int:
        """Alias used by EDL consumers; this is frames, not seconds or PTS."""
        return self.kept_frames

    def to_delete_mask(self) -> tuple[bool, ...]:
        values = [False] * self.total_frames
        for start, end in self.deleted_ranges:
            values[start:end] = [True] * (end - start)
        return tuple(values)

    def is_deleted(self, source_frame: int) -> bool:
        self._check_source(source_frame)
        index = bisect_right(self._keep_starts, source_frame) - 1
        if index >= 0:
            start, end = self.kept_ranges[index]
            if start <= source_frame < end:
                return False
        return True

    def source_to_virtual(self, source_frame: int) -> int | None:
        """Map a kept source frame; deleted frames deliberately return None."""
        self._check_source(source_frame)
        index = bisect_right(self._keep_starts, source_frame) - 1
        if index < 0:
            return None
        start, end = self.kept_ranges[index]
        if source_frame >= end:
            return None
        return self.virtual_prefix[index] + source_frame - start

    def virtual_to_source(self, virtual_frame: int) -> int:
        if isinstance(virtual_frame, bool):
            raise ValueError("virtual_frame must be an integer, not bool")
        virtual = _as_int(virtual_frame, "virtual_frame")
        if virtual < 0 or virtual >= self.kept_frames:
            raise IndexError(
                f"virtual frame {virtual} outside [0, {self.kept_frames})"
            )
        index = bisect_right(self.virtual_prefix, virtual) - 1
        start, _ = self.kept_ranges[index]
        return start + virtual - self.virtual_prefix[index]

    def snap_source(self, source_frame: int, direction: str = "next") -> int | None:
        """Return a kept frame at/near ``source_frame`` without changing it otherwise."""
        self._check_source(source_frame)
        if not self.is_deleted(source_frame):
            return source_frame
        if direction == "next":
            for start, _ in self.kept_ranges:
                if start > source_frame:
                    return start
            return None
        if direction == "previous":
            for start, end in reversed(self.kept_ranges):
                if end - 1 < source_frame:
                    return end - 1
            return None
        if direction == "nearest":
            nxt = self.snap_source(source_frame, "next")
            prev = self.snap_source(source_frame, "previous")
            if nxt is None:
                return prev
            if prev is None:
                return nxt
            return nxt if nxt - source_frame <= source_frame - prev else prev
        raise ValueError("direction must be next, previous or nearest")

    def _check_source(self, source_frame: int) -> int:
        source = _as_int(source_frame, "source_frame")
        if source < 0 or source >= self.total_frames:
            raise IndexError(f"source frame {source} outside [0, {self.total_frames})")
        return source

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "total_frames": self.total_frames,
            "deleted_ranges": [list(value) for value in self.deleted_ranges],
            "kept_ranges": [list(value) for value in self.kept_ranges],
            "virtual_frames": self.kept_frames,
            "fingerprint": self.fingerprint,
            "interval_semantics": "half-open [start, end)",
        }
