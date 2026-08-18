"""OpenCV preview-engine adapter.

This module does not replace or modify :class:`video_io.VideoIOThread`; it
only translates the shared preview protocol to the existing command queue.
"""
from __future__ import annotations

import math
from queue import Empty, Queue
import threading
from typing import Any, Callable

from media_info import MediaInfo, MediaInfoError
from preview_engine import (
    CertifiedEdlRequest,
    PreviewEngineError,
    PreviewPlayRequest,
    SourceSeekRequest,
    normalized_source_path,
)
from video_io import (
    CMD_PLAY,
    CMD_QUIT,
    CMD_SEEK,
    CMD_SEEK_LATEST,
    CMD_STOP,
    VideoIOThread,
)


class CvEngine:
    """Compatibility adapter preserving the current OpenCV playback path."""

    engine_name = "cv"
    native_rendering = False

    @property
    def mode(self) -> str:
        return self._mode

    def __init__(
        self,
        path: str,
        frame_queue: Queue,
        *,
        io_factory: Callable[..., VideoIOThread] = VideoIOThread,
    ) -> None:
        self.path = str(normalized_source_path(path))
        self.frame_queue = frame_queue
        self._io = io_factory(self.path, frame_queue)
        self.fps = float(self._io.fps)
        self.total = int(self._io.total)
        self._lock = threading.RLock()
        self._started = False
        self._closed = False
        self._close_complete = False
        self._media_info: MediaInfo | None = None
        self._events: Queue[dict[str, Any]] = Queue(maxsize=64)
        self._project_generation = 0
        self._timeline_revision = 0
        self._mode = "source"
        self._viewport: tuple[int, int, float] | None = None

    def _require_open_locked(self) -> None:
        if self._closed:
            raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")

    def _ensure_started_locked(self) -> None:
        self._require_open_locked()
        if not self._started:
            self.start()

    def _validate_media_source_locked(self, media: MediaInfo) -> None:
        if normalized_source_path(media.source_path) != normalized_source_path(self.path):
            raise PreviewEngineError(
                "SOURCE_MISMATCH",
                "MediaInfo belongs to another source",
                details={"engine": self.path, "media": str(media.source_path)},
            )
        try:
            media.assert_source_current()
        except MediaInfoError as exc:
            raise PreviewEngineError(
                exc.code,
                str(exc),
                details=getattr(exc, "details", {}),
            ) from exc

    def _validate_source_frame_locked(self, frame: int) -> None:
        if self.total <= 0 or frame >= self.total:
            raise PreviewEngineError(
                "SOURCE_FRAME_OUT_OF_RANGE",
                "source frame is outside the preview source",
                details={"frame": frame, "count": self.total},
            )

    def _publish(self, event: dict[str, Any]) -> None:
        try:
            self._events.put_nowait(event)
        except Exception:
            try:
                self._events.get_nowait()
            except Empty:
                pass
            try:
                self._events.put_nowait(event)
            except Exception:
                pass

    def start(self) -> None:
        with self._lock:
            self._require_open_locked()
            if self._started:
                return
            self._io.start()
            self._started = True
            self._publish(
                {
                    "event": "started",
                    "mode": "source",
                    "project_generation": self._project_generation,
                    "timeline_revision": self._timeline_revision,
                }
            )

    def bind_media_info(self, media: MediaInfo | None) -> None:
        # CvEngine addresses frames through OpenCV and intentionally remains
        # available when certification is absent or blocked.
        with self._lock:
            self._require_open_locked()
            if media is not None:
                self._validate_media_source_locked(media)
            self._media_info = media

    def seek_source(self, request: SourceSeekRequest) -> bool:
        with self._lock:
            self._ensure_started_locked()
            self._validate_source_frame_locked(request.source_frame)
            command_type = CMD_SEEK_LATEST if request.latest_only else CMD_SEEK
            accepted = bool(
                self._io.send(
                    {
                        "type": command_type,
                        "frame": request.source_frame,
                        "timeline_revision": request.timeline_revision,
                        "canvas_wh": request.canvas_size,
                        "pause_segs": [],
                        "skip_trimmed": False,
                    }
                )
            )
            if accepted:
                self._timeline_revision = request.timeline_revision
                self._mode = "source"
                self._publish(
                    {
                        "event": "seek-request",
                        "mode": "source",
                        "source_frame": request.source_frame,
                        "timeline_revision": request.timeline_revision,
                    }
                )
            return accepted

    def play(self, request: PreviewPlayRequest) -> bool:
        with self._lock:
            self._ensure_started_locked()
            mode = (
                "edl"
                if request.skip_trimmed and request.timeline_plan.deleted_ranges
                else "source"
            )
            accepted = bool(
                self._io.send(
                    {
                        "type": CMD_PLAY,
                        "params": {
                            "start_frame": request.start_frame,
                            "preview_step": request.preview_step,
                            "speed_multiplier": request.speed_multiplier,
                            "skip_trimmed": request.skip_trimmed,
                            "speedup_1x": request.speedup_1x,
                            "speedup_02": request.speedup_02,
                            "speedup_02_factor": request.speedup_02_factor,
                            "pause_segs": list(request.timeline_plan.deleted_ranges),
                            "timeline_revision": request.timeline_revision,
                            "speed_segs": list(request.speed_segments),
                            "canvas_wh": request.canvas_size,
                            "preview_step_cap": request.preview_step_cap,
                            "skip_trim_min_span": request.skip_trim_min_span,
                        },
                    },
                )
            )
            if accepted:
                self._project_generation = request.project_generation
                self._timeline_revision = request.timeline_revision
                self._mode = mode
                self._publish(
                    {
                        "event": "play-request",
                        "mode": self._mode,
                        "source_frame": request.start_frame,
                        "project_generation": request.project_generation,
                        "timeline_revision": request.timeline_revision,
                    }
                )
            return accepted

    def play_edl(
        self,
        request: CertifiedEdlRequest,
        *,
        start_frame: int,
        playback_rate: float = 1.0,
    ) -> bool:
        """Play the certified plan through the existing CV skip policy.

        CvEngine is the deliberate fallback when mpv is unavailable.  It does
        not consume the EDL file and therefore does not become an exporter or
        timestamp authority; it simply preserves the long-standing
        source-frame skip behavior of ``VideoIOThread``.
        """
        with self._lock:
            self._ensure_started_locked()
            self._validate_media_source_locked(request.media_info)
            if (
                self._media_info is not None
                and request.media_info.source_sha256 != self._media_info.source_sha256
            ):
                raise PreviewEngineError(
                    "CERTIFICATION_MISMATCH",
                    "certified EDL request does not match the bound source certification",
                )
            if isinstance(start_frame, bool) or not isinstance(start_frame, int):
                raise TypeError("start_frame must be an integer")
            if start_frame < 0 or start_frame >= request.timeline_plan.total_frames:
                raise PreviewEngineError(
                    "SOURCE_FRAME_OUT_OF_RANGE",
                    "start frame is outside the timeline plan",
                    details={"start_frame": start_frame},
                )
            rate = float(playback_rate)
            if rate <= 0 or not math.isfinite(rate):
                raise ValueError("playback_rate must be a finite positive number")
            accepted = bool(
                self._io.send(
                    {
                        "type": CMD_PLAY,
                        "params": {
                            "start_frame": start_frame,
                            "preview_step": 1,
                            "speed_multiplier": 1.0 / rate,
                            "skip_trimmed": True,
                            "speedup_1x": False,
                            "speedup_02": False,
                            "speedup_02_factor": 1,
                            "pause_segs": list(request.timeline_plan.deleted_ranges),
                            "timeline_revision": request.timeline_revision,
                            "speed_segs": [],
                            "canvas_wh": (
                                self._viewport[:2]
                                if self._viewport is not None
                                else (1, 1)
                            ),
                            "preview_step_cap": 3,
                            "skip_trim_min_span": 0,
                        },
                    },
                )
            )
            if accepted:
                self._project_generation = request.project_generation
                self._timeline_revision = request.timeline_revision
                self._mode = "edl"
                self._publish(
                    {
                        "event": "play-request",
                        "mode": "edl",
                        "source_frame": start_frame,
                        "project_generation": request.project_generation,
                        "timeline_revision": request.timeline_revision,
                    }
                )
            return accepted

    def stop(self) -> bool:
        with self._lock:
            self._ensure_started_locked()
            accepted = bool(self._io.send({"type": CMD_STOP}))
            if accepted:
                self._publish(
                    {
                        "event": "stop-request",
                        "mode": self._mode,
                        "project_generation": self._project_generation,
                        "timeline_revision": self._timeline_revision,
                    }
                )
            return accepted

    def send(self, command: dict[str, Any]) -> bool:
        """Legacy command bridge used while the Tk player is being migrated."""
        if not isinstance(command, dict):
            raise TypeError("preview command must be a mapping")
        if command.get("type") == CMD_QUIT:
            return self.close()
        with self._lock:
            self._ensure_started_locked()
            return bool(self._io.send(command))

    def set_viewport(self, width: int, height: int, dpi_scale: float = 1.0) -> None:
        if width < 1 or height < 1 or dpi_scale <= 0:
            raise ValueError("viewport dimensions and DPI scale must be positive")
        with self._lock:
            self._ensure_started_locked()
            self._viewport = (int(width), int(height), float(dpi_scale))
            self._publish(
                {
                    "event": "viewport",
                    "width": int(width),
                    "height": int(height),
                    "dpi_scale": float(dpi_scale),
                }
            )

    def set_pace_mode(self, mode: str) -> None:
        with self._lock:
            self._require_open_locked()
            self._io.set_pace_mode(mode)

    def get_pace_mode(self) -> str:
        return self._io.get_pace_mode()

    def snapshot_perf(self) -> dict[str, Any]:
        value = self._io.snapshot_perf()
        value["engine"] = self.engine_name
        value.setdefault("mode", self._mode)
        value.setdefault("project_generation", self._project_generation)
        value.setdefault("timeline_revision", self._timeline_revision)
        return value

    def snapshot(self) -> dict[str, Any]:
        return self.snapshot_perf()

    def reset_perf_stats(self) -> None:
        with self._lock:
            self._require_open_locked()
            self._io.reset_perf_stats()

    def begin_perf_segment(self) -> None:
        with self._lock:
            self._require_open_locked()
            self._io.begin_perf_segment()

    def is_playback_active(self) -> bool:
        return self._io.is_playback_active()

    def is_alive(self) -> bool:
        return self._io.is_alive()

    def poll_events(self, limit: int = 64) -> tuple[dict[str, Any], ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        result = []
        for _ in range(limit):
            try:
                result.append(self._events.get_nowait())
            except Empty:
                break
        return tuple(result)

    def close(self, timeout: float = 1.0) -> bool:
        with self._lock:
            if self._close_complete:
                return True
            # Close is a one-way lifecycle transition even when the bounded
            # join times out.  The wrapped thread remains owned and retryable,
            # but no command may be queued behind its quit sentinel.
            self._closed = True
            closed = bool(self._io.close(timeout=timeout))
            if closed:
                self._close_complete = True
                self._started = False
                self._publish(
                    {
                        "event": "closed",
                        "project_generation": self._project_generation,
                        "timeline_revision": self._timeline_revision,
                    }
                )
            return closed
