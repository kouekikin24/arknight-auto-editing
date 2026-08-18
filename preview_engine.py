"""Shared preview-engine contracts.

The preview layer deliberately uses source-frame coordinates for editing and
keeps certified media time as a separate dependency.  Implementations may
render decoded RGB frames (OpenCV) or render directly into a native window
(libmpv), but callers use the same small command surface.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from queue import Queue
from typing import Any, Mapping, Protocol, runtime_checkable

from media_info import MediaInfo
from timeline_plan import TimelinePlan


class PreviewEngineError(RuntimeError):
    """Fail-closed preview error with a stable machine-readable code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.details = dict(details or {})


@dataclass(frozen=True, slots=True)
class SourceSeekRequest:
    """Seek to one source frame without silently snapping deleted frames."""

    source_frame: int
    canvas_size: tuple[int, int]
    timeline_revision: int
    exact: bool = True
    latest_only: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.source_frame, bool) or not isinstance(self.source_frame, int):
            raise TypeError("source_frame must be an integer")
        if self.source_frame < 0:
            raise ValueError("source_frame must be non-negative")
        if (
            not isinstance(self.canvas_size, tuple)
            or len(self.canvas_size) != 2
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 1
                for value in self.canvas_size
            )
        ):
            raise ValueError("canvas_size must contain two positive integers")
        if (
            isinstance(self.timeline_revision, bool)
            or not isinstance(self.timeline_revision, int)
            or self.timeline_revision < 0
        ):
            raise ValueError("timeline_revision must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class PreviewPlayRequest:
    """Immutable playback snapshot captured by the Tk owner thread."""

    start_frame: int
    playback_rate: float
    preview_step: int
    speed_multiplier: float
    skip_trimmed: bool
    speedup_1x: bool
    speedup_02: bool
    speedup_02_factor: int
    timeline_plan: TimelinePlan
    speed_segments: tuple[tuple[int, int, int], ...]
    canvas_size: tuple[int, int]
    project_generation: int
    timeline_revision: int
    preview_step_cap: int = 3
    skip_trim_min_span: int = 0

    def __post_init__(self) -> None:
        for name in (
            "start_frame",
            "preview_step",
            "speedup_02_factor",
            "project_generation",
            "timeline_revision",
            "preview_step_cap",
            "skip_trim_min_span",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
        if self.start_frame < 0 or self.start_frame >= self.timeline_plan.total_frames:
            raise ValueError("start_frame is outside the timeline plan")
        if self.preview_step < 1 or self.speedup_02_factor < 1:
            raise ValueError("preview step values must be positive")
        if self.project_generation < 0 or self.timeline_revision < 0:
            raise ValueError("project scope values must be non-negative")
        if self.preview_step_cap < 1 or self.skip_trim_min_span < 0:
            raise ValueError("preview tuning values are invalid")
        if (
            not math.isfinite(float(self.playback_rate))
            or not math.isfinite(float(self.speed_multiplier))
            or float(self.playback_rate) <= 0
            or float(self.speed_multiplier) <= 0
        ):
            raise ValueError("playback rates must be positive")
        if (
            not isinstance(self.canvas_size, tuple)
            or len(self.canvas_size) != 2
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 1
                for value in self.canvas_size
            )
        ):
            raise ValueError("canvas_size must contain two positive integers")


@dataclass(frozen=True, slots=True)
class CertifiedEdlRequest:
    """Source-bound request for an EDL built only from certified PTS ticks."""

    media_info: MediaInfo
    timeline_plan: TimelinePlan
    project_generation: int
    timeline_revision: int

    def __post_init__(self) -> None:
        if not isinstance(self.media_info, MediaInfo):
            raise TypeError("media_info must be a MediaInfo snapshot")
        if not isinstance(self.timeline_plan, TimelinePlan):
            raise TypeError("timeline_plan must be a TimelinePlan")
        if (
            isinstance(self.project_generation, bool)
            or not isinstance(self.project_generation, int)
            or self.project_generation < 0
        ):
            raise ValueError("project_generation must be a non-negative integer")
        if (
            isinstance(self.timeline_revision, bool)
            or not isinstance(self.timeline_revision, int)
            or self.timeline_revision < 0
        ):
            raise ValueError("timeline_revision must be a non-negative integer")


@runtime_checkable
class PreviewEngine(Protocol):
    """Minimum playback boundary used by :class:`VideoPreviewPlayer`.

    Implementations reject new control or metrics-mutating calls with
    ``PreviewEngineError(code="ENGINE_CLOSED", ...)`` once ``close`` begins.
    Inspection methods and ``close`` itself remain safe to call while a
    bounded native/decoder shutdown is being retried.
    """

    engine_name: str
    path: str
    fps: float
    total: int
    mode: str
    frame_queue: Queue | None
    native_rendering: bool

    def start(self) -> None: ...

    def bind_media_info(self, media: MediaInfo | None) -> None: ...

    def seek_source(self, request: SourceSeekRequest) -> bool: ...

    def play(self, request: PreviewPlayRequest) -> bool: ...

    def play_edl(
        self,
        request: CertifiedEdlRequest,
        *,
        start_frame: int,
        playback_rate: float = 1.0,
    ) -> bool: ...

    def stop(self) -> bool: ...

    def set_pace_mode(self, mode: str) -> None: ...

    def set_viewport(self, width: int, height: int, dpi_scale: float = 1.0) -> None: ...

    def get_pace_mode(self) -> str: ...

    def snapshot_perf(self) -> dict[str, Any]: ...

    def snapshot(self) -> dict[str, Any]: ...

    def reset_perf_stats(self) -> None: ...

    def begin_perf_segment(self) -> None: ...

    def is_playback_active(self) -> bool: ...

    def is_alive(self) -> bool: ...

    def poll_events(self, limit: int = 64) -> tuple[dict[str, Any], ...]: ...

    def close(self, timeout: float = 1.0) -> bool: ...


def normalized_source_path(value: str | Path) -> Path:
    """Return the canonical path used by source-bound engine checks."""

    return Path(value).expanduser().resolve()
