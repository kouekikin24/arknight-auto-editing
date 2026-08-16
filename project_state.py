"""Owner-side project edit state and revision tracking.

The current UI still renders legacy inclusive segment dictionaries.  This
module owns copies of those dictionaries and applies immutable EditCommands;
consumers receive snapshots, so widgets and workers cannot mutate shared
project state behind the revision counter.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import operator
from typing import Iterable, Mapping

import numpy as np

from edit_commands import EditCommand, SetClipBounds, SetPauseMaskRun, SetPauseMode


_PAUSE_MODES = frozenset({"auto", "all", "keep"})
_MASK_VALUES = frozenset({0, 1, 2, 3})


def _integer(value: object, name: str) -> int:
    try:
        return operator.index(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _copy_segments(values: Iterable[Mapping[str, object]]) -> list[dict]:
    result: list[dict] = []
    for raw in values:
        segment = deepcopy(dict(raw))
        mask = segment.get("local_del_mask")
        if mask is not None:
            segment["local_del_mask"] = np.array(mask, copy=True)
        result.append(segment)
    return result


def _validate_ids(segments: list[dict], label: str, *, required: bool) -> None:
    seen: set[int] = set()
    for index, segment in enumerate(segments):
        if "id" not in segment:
            if required:
                raise ValueError(f"{label} segment {index} has no id")
            continue
        segment_id = _integer(segment["id"], f"{label} segment {index} id")
        if segment_id in seen:
            raise ValueError(f"duplicate {label} segment id: {segment_id}")
        seen.add(segment_id)


def _validate_range(segment: dict, label: str, index: int) -> tuple[int, int]:
    try:
        start_raw = segment["start"]
        end_raw = segment["end"]
    except KeyError as exc:
        raise ValueError(f"{label} segment {index} has no {exc.args[0]}") from exc
    start = _integer(start_raw, f"{label} segment {index} start")
    end = _integer(end_raw, f"{label} segment {index} end")
    if start < 0 or end < start:
        raise ValueError(
            f"invalid {label} segment {index} range: [{start}, {end}]"
        )
    return start, end


def _validated_segments(
    pause_values: Iterable[Mapping[str, object]],
    speed_values: Iterable[Mapping[str, object]],
    clip_values: Iterable[Mapping[str, object]],
) -> tuple[list[dict], list[dict], list[dict]]:
    pauses = _copy_segments(pause_values)
    speeds = _copy_segments(speed_values)
    clips = _copy_segments(clip_values)
    _validate_ids(pauses, "pause", required=True)
    _validate_ids(speeds, "speed", required=False)
    _validate_ids(clips, "clip", required=True)

    for index, segment in enumerate(pauses):
        start, end = _validate_range(segment, "pause", index)
        mode = str(segment.get("mode", "auto")).strip().lower()
        if mode not in _PAUSE_MODES:
            raise ValueError(f"unsupported pause mode: {segment.get('mode')!r}")
        segment["mode"] = mode
        mask = segment.get("local_del_mask")
        if mask is not None:
            values = np.asarray(mask)
            expected = end - start + 1
            if values.ndim != 1 or len(values) != expected:
                raise ValueError(
                    f"pause segment {index} mask length must be {expected}"
                )
            if not bool(np.isin(values, tuple(_MASK_VALUES)).all()):
                raise ValueError(
                    f"pause segment {index} mask contains unsupported values"
                )

    for index, segment in enumerate(speeds):
        _validate_range(segment, "speed", index)

    for index, segment in enumerate(clips):
        start, end = _validate_range(segment, "clip", index)
        try:
            keep_start = _integer(
                segment["keep_in"], f"clip segment {index} keep_in"
            )
            keep_end = _integer(
                segment["keep_out"], f"clip segment {index} keep_out"
            ) + 1
        except KeyError as exc:
            raise ValueError(f"clip segment {index} has no {exc.args[0]}") from exc
        if not (start <= keep_start <= keep_end <= end + 1):
            raise ValueError(
                f"clip segment {index} keep range [{keep_start}, {keep_end}) "
                f"is outside [{start}, {end + 1})"
            )
    return pauses, speeds, clips


@dataclass(frozen=True, slots=True)
class ProjectSnapshot:
    project_generation: int
    timeline_revision: int
    pause_segments: tuple[dict, ...]
    speed_segments: tuple[dict, ...]
    clip_segments: tuple[dict, ...]

    def mutable_segments(self) -> tuple[list[dict], list[dict], list[dict]]:
        return (
            _copy_segments(self.pause_segments),
            _copy_segments(self.speed_segments),
            _copy_segments(self.clip_segments),
        )


class ProjectState:
    """Single owner for edits associated with the currently loaded source."""

    def __init__(self) -> None:
        self._project_generation = 0
        self._timeline_revision = 0
        self._pause_segments: list[dict] = []
        self._speed_segments: list[dict] = []
        self._clip_segments: list[dict] = []

    @property
    def project_generation(self) -> int:
        return self._project_generation

    @property
    def timeline_revision(self) -> int:
        return self._timeline_revision

    def replace_project(
        self,
        pause_segments: Iterable[Mapping[str, object]] = (),
        speed_segments: Iterable[Mapping[str, object]] = (),
        clip_segments: Iterable[Mapping[str, object]] = (),
    ) -> ProjectSnapshot:
        replacement = _validated_segments(
            pause_segments, speed_segments, clip_segments
        )
        self._project_generation += 1
        self._timeline_revision = 0
        self._replace_segments(*replacement)
        return self.snapshot()

    def replace_timeline(
        self,
        pause_segments: Iterable[Mapping[str, object]],
        speed_segments: Iterable[Mapping[str, object]],
        clip_segments: Iterable[Mapping[str, object]],
    ) -> ProjectSnapshot:
        replacement = _validated_segments(
            pause_segments, speed_segments, clip_segments
        )
        self._replace_segments(*replacement)
        self._timeline_revision += 1
        return self.snapshot()

    def apply(self, command: EditCommand) -> ProjectSnapshot:
        return self.apply_many((command,))

    def apply_many(self, commands: Iterable[EditCommand]) -> ProjectSnapshot:
        """Apply one owner-side edit atomically and publish one revision."""
        pending = tuple(commands)
        if not pending:
            return self.snapshot()

        original = (
            self._pause_segments,
            self._speed_segments,
            self._clip_segments,
        )
        self._pause_segments = _copy_segments(original[0])
        self._speed_segments = _copy_segments(original[1])
        self._clip_segments = _copy_segments(original[2])
        try:
            for command in pending:
                self._apply_one(command)
        except BaseException:
            (
                self._pause_segments,
                self._speed_segments,
                self._clip_segments,
            ) = original
            raise
        self._timeline_revision += 1
        return self.snapshot()

    def _apply_one(self, command: EditCommand) -> None:
        if isinstance(command, SetPauseMode):
            mode = str(command.mode).strip().lower()
            if mode not in _PAUSE_MODES:
                raise ValueError(f"unsupported pause mode: {command.mode!r}")
            segment = self._find(self._pause_segments, command.segment_id, "pause")
            segment["mode"] = mode
        elif isinstance(command, SetPauseMaskRun):
            segment = self._find(self._pause_segments, command.segment_id, "pause")
            mask = segment.get("local_del_mask")
            if mask is None:
                raise ValueError("pause segment has no local_del_mask")
            copied = np.array(mask, copy=True)
            start = _integer(command.start_offset, "start_offset")
            end = _integer(command.end_offset, "end_offset")
            value = _integer(command.value, "value")
            if value not in _MASK_VALUES:
                raise ValueError(f"unsupported pause mask value: {value}")
            if start < 0 or end <= start or end > len(copied):
                raise ValueError(
                    f"pause mask range [{start}, {end}) is outside [0, {len(copied)})"
                )
            copied[start:end] = value
            segment["local_del_mask"] = copied
        elif isinstance(command, SetClipBounds):
            segment = self._find(self._clip_segments, command.segment_id, "clip")
            keep_start = _integer(command.keep_start, "keep_start")
            keep_end = _integer(command.keep_end, "keep_end")
            start = _integer(segment["start"], "clip start")
            end_inclusive = _integer(segment["end"], "clip end")
            if not (start <= keep_start <= keep_end <= end_inclusive + 1):
                raise ValueError(
                    f"clip keep range [{keep_start}, {keep_end}) is outside "
                    f"[{start}, {end_inclusive + 1})"
                )
            segment["keep_in"] = keep_start
            segment["keep_out"] = keep_end - 1
        else:
            raise TypeError(f"unsupported edit command: {type(command).__name__}")

    def snapshot(self) -> ProjectSnapshot:
        return ProjectSnapshot(
            project_generation=self._project_generation,
            timeline_revision=self._timeline_revision,
            pause_segments=tuple(_copy_segments(self._pause_segments)),
            speed_segments=tuple(_copy_segments(self._speed_segments)),
            clip_segments=tuple(_copy_segments(self._clip_segments)),
        )

    def _replace_segments(self, pause_segments, speed_segments, clip_segments) -> None:
        self._pause_segments = _copy_segments(pause_segments)
        self._speed_segments = _copy_segments(speed_segments)
        self._clip_segments = _copy_segments(clip_segments)

    @staticmethod
    def _find(segments: list[dict], segment_id: int, label: str) -> dict:
        wanted = _integer(segment_id, "segment_id")
        for segment in segments:
            if _integer(segment.get("id"), f"{label} id") == wanted:
                return segment
        raise KeyError(f"unknown {label} segment id: {wanted}")
