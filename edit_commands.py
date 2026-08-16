"""Immutable edit commands emitted by UI controls.

Commands describe intent in source-frame coordinates.  They do not retain a
reference to the mutable dictionaries used by the legacy Tk widgets.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias


@dataclass(frozen=True, slots=True)
class SetPauseMode:
    segment_id: int
    mode: str


@dataclass(frozen=True, slots=True)
class SetPauseMaskRun:
    segment_id: int
    start_offset: int
    end_offset: int
    value: int


@dataclass(frozen=True, slots=True)
class SetClipBounds:
    segment_id: int
    keep_start: int
    keep_end: int


EditCommand: TypeAlias = SetPauseMode | SetPauseMaskRun | SetClipBounds
