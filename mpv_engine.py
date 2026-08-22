"""Preview-only libmpv engine.

The engine is intentionally independent from the export path.  It renders to
an already-created Tk child window when a WID is supplied and keeps all mpv
callbacks off the Tk thread by publishing bounded event records.
"""
from __future__ import annotations

from fractions import Fraction
from bisect import bisect_left
import math
import os
from pathlib import Path
from queue import Empty, Full, Queue
import re
import threading
from typing import Any, Callable

from certified_edl import CertifiedEdl, build_certified_edl
from media_info import MediaInfo, MediaInfoError
from preview_engine import (
    CertifiedEdlRequest,
    PreviewEngineError,
    PreviewPlayRequest,
    SourceSeekRequest,
    normalized_source_path,
)
from pts_timeline import CertifiedPtsTimeline
from timeline_plan import TimelinePlan


def _fraction_seconds(value: Fraction) -> str:
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


class MpvEngine:
    """A source/EDL preview engine with injectable python-mpv binding."""

    engine_name = "mpv"
    native_rendering = True

    @property
    def mode(self) -> str:
        with self._lock:
            return self._mode

    def __init__(
        self,
        path: str,
        *,
        wid: int | None = None,
        fps: float = 0.0,
        total: int = 0,
        dll_dir: str | os.PathLike[str] | None = None,
        edl_dir: str | os.PathLike[str] | None = None,
        mpv_factory: Callable[..., Any] | None = None,
        event_queue_size: int = 256,
    ) -> None:
        if event_queue_size < 1:
            raise ValueError("event_queue_size must be positive")
        self.path = str(normalized_source_path(path))
        self.wid = int(wid) if wid is not None else None
        self.fps = float(fps or 0.0)
        self.total = int(total or 0)
        if self.fps < 0 or not math.isfinite(self.fps):
            raise ValueError("fps must be finite and non-negative")
        if self.total < 0:
            raise ValueError("total must be non-negative")
        self.frame_queue = None
        self._dll_dir = (
            normalized_source_path(dll_dir)
            if dll_dir is not None
            else None
        )
        self._edl_dir = (
            normalized_source_path(edl_dir)
            if edl_dir is not None
            else Path(self.path).parent / ".arknight-preview-edl"
        )
        self._factory = mpv_factory
        self._dll_handle: Any | None = None
        self._player: Any | None = None
        self._event_callback: Callable[[Any], None] | None = None
        self._property_callbacks: list[tuple[str, Callable[..., None]]] = []
        self._events: Queue[dict[str, Any]] = Queue(maxsize=event_queue_size)
        self._event_drop_count = 0
        self._lock = threading.RLock()
        self._closed = False
        self._started = False
        self._terminate_thread: threading.Thread | None = None
        self._terminate_done: threading.Event | None = None
        self._terminate_error: BaseException | None = None
        self._callbacks_detached = False
        self._mode = "source"
        self._load_generation = 0
        self._project_generation = 0
        self._timeline_revision = 0
        self._media_info: MediaInfo | None = None
        # A cheap source identity snapshot is used on every exact seek.  Full
        # SHA-256 validation happens when certification is bound and when an
        # EDL is built; hashing a multi-gigabyte source for every arrow-key
        # seek would make the editing surface unusable.
        self._source_stat: tuple[int, int] | None = None
        self._timeline: CertifiedPtsTimeline | None = None
        self._timeline_times: tuple[Fraction, ...] = ()
        self._timeline_clean_start = 0
        self._edl: CertifiedEdl | None = None
        self._edl_cache_key: tuple[Any, ...] | None = None
        self._last_time_pos: float | None = None
        self._duration = 0.0
        self._source_frame = 0
        self._playing = False
        self._loaded_ready = False
        self._current_path: str | None = None
        self._pending_seek: tuple[Fraction, bool] | None = None
        self._pending_resume = False
        self._pending_speed: float | None = None
        self._play_end_reason: str | None = None
        self._pace_mode = "opt"
        self._viewport: tuple[int, int, float] | None = None
        self._stats = self._new_stats()

    @staticmethod
    def _new_stats() -> dict[str, Any]:
        return {
            "presented": 0,
            "discarded": 0,
            "late": 0,
            "late1": 0,
            "late2": 0,
            "catchup_events": 0,
            "pace_resets": 0,
            "hard_resets": 0,
            "seek_count": 0,
            "q_drop": 0,
            "present_ms_sum": 0.0,
            "present_ms_max": 0.0,
            "present_ms_p95": 0.0,
            "lag_ms_max": 0.0,
            "lag_ms_p95": 0.0,
            "wall_s": 0.0,
            "playback_active": False,
            "play_end_reason": None,
            "pace_mode": "opt",
            "rate_play_frames": 0,
            "skip_trim_absorbed": 0,
            "rate_trim_frames": 0,
            "spikes": [],
            "engine": "mpv",
        }

    # ------------------------------------------------------------------
    # Binding/bootstrap
    # ------------------------------------------------------------------
    def _prepare_binding(self) -> Any:
        if self._factory is not None:
            return self._factory
        dll_dir = self._dll_dir
        if dll_dir is None:
            raw = os.environ.get("MPV_DLL_DIR")
            if raw:
                dll_dir = normalized_source_path(raw)
            else:
                dll_dir = Path(__file__).resolve().parent / "tools" / "libmpv" / "dll"
        dll = dll_dir / "libmpv-2.dll"
        if not dll.is_file():
            raise PreviewEngineError(
                "MPV_DLL_MISSING",
                "project-owned libmpv-2.dll was not found",
                details={"dll_dir": str(dll_dir), "path": str(dll)},
            )
        # python-mpv resolves the DLL at import time.  Make the selected
        # directory explicit instead of relying on a system mpv/PATH entry.
        os.environ["PATH"] = str(dll_dir) + os.pathsep + os.environ.get("PATH", "")
        add_dll_directory = getattr(os, "add_dll_directory", None)
        if add_dll_directory is not None:
            try:
                # Keep the handle alive for the lifetime of the engine; on
                # Windows dropping it immediately can undo the DLL search
                # path before python-mpv imports its dependent libraries.
                self._dll_handle = add_dll_directory(str(dll_dir))
            except OSError:
                pass
        try:
            import mpv
        except Exception as exc:  # pragma: no cover - depends on local DLLs
            raise PreviewEngineError(
                "MPV_IMPORT_FAILED",
                "python-mpv could not load the selected libmpv binding",
                details={"dll": str(dll)},
            ) from exc
        return mpv.MPV

    def _make_player(self) -> Any:
        factory = self._prepare_binding()
        options: dict[str, Any] = {
            # The preview is a silent editing surface; never decode or output
            # audio even when the source has an audio stream.
            "audio": "no",
            "idle": "yes",
            "keep_open": "yes",
            "terminal": False,
            "input_default_bindings": False,
            "input_vo_keyboard": False,
            "osc": "no",
            "framedrop": "vo",
            "hr_seek": "yes",
        }
        if self.wid is None:
            options["vo"] = "null"
        else:
            options["vo"] = "gpu"
            options["wid"] = str(self.wid)
        try:
            player = factory(**options)
        except TypeError:
            # Small fake bindings and older wrappers may not accept all mpv
            # options; the real python-mpv path receives the full set above.
            reduced = {key: value for key, value in options.items() if key in {"vo", "wid", "idle", "terminal"}}
            player = factory(**reduced)
        self._event_callback = self._on_event
        register = getattr(player, "register_event_callback", None)
        if callable(register):
            register(self._event_callback)
        for name in ("time-pos", "pause", "duration", "eof-reached", "path"):
            observe = getattr(player, "observe_property", None)
            if not callable(observe):
                break
            callback = self._make_property_callback(name)
            observe(name, callback)
            self._property_callbacks.append((name, callback))
        return player

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")
            if self._started:
                return
            self._player = self._make_player()
            self._started = True
            self._ensure_loaded_locked(self.path, "source")

    # ------------------------------------------------------------------
    # State/events
    # ------------------------------------------------------------------
    @staticmethod
    def _event_name(event: Any) -> str:
        if isinstance(event, dict):
            value = event.get("event") or event.get("name") or event.get("event_id")
        else:
            value = getattr(event, "event_id", None)
            value = getattr(value, "name", value)
        text = str(value or "").lower().replace("_", "-")
        for prefix in ("mpveventid.", "eventid."):
            if text.startswith(prefix):
                text = text[len(prefix):]
                break
        if "." in text:
            text = text.rsplit(".", 1)[-1]
        match = re.search(r"\s([a-z][a-z0-9-]*)>?$", text)
        if match:
            text = match.group(1)
        return text

    def _enqueue_event(self, value: dict[str, Any]) -> None:
        try:
            self._events.put_nowait(value)
        except Full:
            # Preserve lifecycle/load markers when a native callback bursts a
            # large number of property updates.  The queue remains bounded,
            # but low-value telemetry is evicted before a critical marker.
            buffered: list[dict[str, Any]] = []
            while True:
                try:
                    buffered.append(self._events.get_nowait())
                except Empty:
                    break
            critical = value.get("event") in {
                "file-loaded",
                "start-file",
                "end-file",
                "shutdown",
            }
            if critical:
                removed = False
                kept: list[dict[str, Any]] = []
                for event in buffered:
                    if not removed and event.get("event") in {
                        "property-change",
                        "viewport",
                    }:
                        removed = True
                        continue
                    kept.append(event)
                buffered = kept
            elif buffered:
                buffered.pop(0)
            for event in buffered:
                try:
                    self._events.put_nowait(event)
                except Full:
                    break
            try:
                self._events.put_nowait(value)
            except Full:
                pass
            self._event_drop_count += 1

    def _on_event(self, event: Any) -> None:
        name = self._event_name(event)
        with self._lock:
            if self._closed and name != "shutdown":
                return
            if isinstance(event, dict) and event.get("generation") is not None:
                if int(event["generation"]) != self._load_generation:
                    return
            # Test/fake bindings can carry the owner scope on an event.  Real
            # libmpv callbacks do not, but accepting the fields here makes the
            # stale-result boundary explicit and keeps adapters deterministic.
            if isinstance(event, dict) and event.get("project_generation") is not None:
                if int(event["project_generation"]) != self._project_generation:
                    return
            if isinstance(event, dict) and event.get("timeline_revision") is not None:
                if int(event["timeline_revision"]) != self._timeline_revision:
                    return
            event_path = None
            if isinstance(event, dict):
                raw_path = event.get("path") or event.get("filename")
                if raw_path:
                    event_path = str(normalized_source_path(raw_path))
            if name in {"file-loaded", "start-file", "end-file"}:
                # Real libmpv callbacks do not carry our Python generation
                # token.  The observed ``path`` property is the second guard
                # against an old load completion unlocking a newer SOURCE/EDL
                # request while a replacement file is still loading.
                # python-mpv usually exposes the new ``path`` property before
                # (or at the same time as) MPV_EVENT_FILE_LOADED, but the
                # property callback can lag behind the native event.  Query
                # the binding synchronously as a third source of truth so a
                # valid replacement load is not discarded merely because the
                # old observed property is still cached.
                reported_path = event_path or self._binding_path_locked()
                if reported_path is not None and reported_path != getattr(self, "_loaded_path", None):
                    return
                if (
                    reported_path is None
                    and self._current_path is not None
                    and getattr(self, "_loaded_path", None) is not None
                    and self._current_path != self._loaded_path
                ):
                    return
            generation = self._load_generation
            if name in {"file-loaded", "start-file"}:
                if name == "file-loaded":
                    self._loaded_ready = True
                else:
                    self._loaded_ready = False
                self._playing = False
                self._play_end_reason = None
                if name == "file-loaded":
                    # python-mpv delivers this callback on its own event
                    # thread, not the Tk owner's pump, so release the queued
                    # seek/speed/resume here.  Playback then starts even
                    # while the owner thread is busy with a long redraw or
                    # export teardown; poll_events() stays a read-only
                    # fallback for bindings without a live event thread.
                    self._flush_pending_locked()
            elif name in {"end-file", "eof-reached"}:
                self._playing = False
                self._play_end_reason = "eof"
            elif name == "shutdown":
                self._closed = True
                self._playing = False
                self._play_end_reason = "shutdown"
            payload = {
                "event": name,
                "generation": generation,
                "project_generation": self._project_generation,
                "timeline_revision": self._timeline_revision,
                "mode": self._mode,
                "source_frame": self._source_frame,
                "path": self._current_path or getattr(self, "_loaded_path", None),
            }
        self._enqueue_event(payload)

    def _binding_path_locked(self) -> str | None:
        """Read the native path property without trusting a stale callback."""

        player = self._player
        if player is None:
            return None
        for name in ("path", "filename", "stream_open_filename"):
            try:
                value = getattr(player, name)
            except Exception:
                continue
            if not value:
                continue
            try:
                return str(normalized_source_path(value))
            except (TypeError, ValueError, OSError):
                return str(value)
        getter = getattr(player, "get_property", None)
        if callable(getter):
            try:
                value = getter("path")
            except Exception:
                value = None
            if value:
                try:
                    return str(normalized_source_path(value))
                except (TypeError, ValueError, OSError):
                    return str(value)
        return None

    def _make_property_callback(self, property_name: str) -> Callable[..., None]:
        def callback(_name: str, value: Any) -> None:
            self._on_property(property_name, value)

        return callback

    def _on_property(self, property_name: str, value: Any) -> None:
        with self._lock:
            if self._closed:
                return
            if property_name == "time-pos":
                try:
                    position = float(value)
                except (TypeError, ValueError):
                    position = None
                if position is not None and math.isfinite(position):
                    if self._last_time_pos is not None and abs(position - self._last_time_pos) > 1e-9:
                        self._stats["presented"] += 1
                    self._last_time_pos = position
                    self._source_frame = self._frame_for_position_locked(position)
            elif property_name == "pause":
                if value is not None:
                    self._playing = not bool(value)
            elif property_name == "eof-reached" and bool(value):
                self._playing = False
                self._play_end_reason = "eof"
            elif property_name == "duration":
                try:
                    self._duration = float(value)
                except (TypeError, ValueError):
                    self._duration = 0.0
            elif property_name == "path":
                if value:
                    try:
                        self._current_path = str(normalized_source_path(value))
                    except (TypeError, ValueError, OSError):
                        self._current_path = str(value)
                else:
                    self._current_path = None
            self._enqueue_event(
                {
                    "event": "property-change",
                    "property": property_name,
                    "value": value,
                    "generation": self._load_generation,
                    "project_generation": self._project_generation,
                    "timeline_revision": self._timeline_revision,
                    "mode": self._mode,
                    "source_frame": self._source_frame,
                    "path": self._current_path,
                }
            )

    def poll_events(self, limit: int = 64) -> tuple[dict[str, Any], ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        result = []
        for _ in range(limit):
            try:
                result.append(self._events.get_nowait())
            except Empty:
                break
        with self._lock:
            self._flush_pending_locked()
        return tuple(result)

    # ------------------------------------------------------------------
    # Source/EDL commands
    # ------------------------------------------------------------------
    def bind_media_info(self, media: MediaInfo | None) -> None:
        with self._lock:
            if self._closed:
                raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")
            if media is None:
                self._media_info = None
                self._timeline = None
                self._timeline_times = ()
                self._timeline_clean_start = 0
                self._source_stat = None
                self._edl = None
                self._edl_cache_key = None
                return
            if Path(media.source_path).resolve() != Path(self.path).resolve():
                raise PreviewEngineError(
                    "SOURCE_MISMATCH",
                    "MediaInfo belongs to another source",
                    details={"engine": self.path, "media": str(media.source_path)},
                )
            try:
                # Bind only a current source.  This validation is deliberately
                # transactional: a failed foreign/stale bind must not replace
                # a previously valid certification on the engine.
                media.assert_source_current()
                candidate_timeline = None
                if media.complete_for_export:
                    candidate_timeline = CertifiedPtsTimeline.from_media_info(media)
                stat = Path(self.path).stat()
            except MediaInfoError as exc:
                raise PreviewEngineError(
                    exc.code,
                    str(exc),
                    details=getattr(exc, "details", {}),
                ) from exc
            except (OSError, ValueError, TypeError) as exc:
                raise PreviewEngineError(
                    "CERTIFICATION_INVALID",
                    "certified media could not produce a current PTS timeline",
                ) from exc
            self._media_info = media
            self._timeline = candidate_timeline
            self._timeline_times = (
                tuple(row.pts * candidate_timeline.time_base for row in candidate_timeline.rows)
                if candidate_timeline is not None
                else ()
            )
            self._timeline_clean_start = (
                min(candidate_timeline.head_anomaly_limit, max(0, candidate_timeline.frame_count - 1))
                if candidate_timeline is not None
                else 0
            )
            self._source_stat = (int(stat.st_size), int(stat.st_mtime_ns))

    def _assert_bound_source_current_locked(self) -> None:
        """Reject obvious source replacement without hashing on every seek."""

        media = self._media_info
        if media is None or self._source_stat is None:
            return
        try:
            stat = Path(self.path).stat()
            current = (int(stat.st_size), int(stat.st_mtime_ns))
        except OSError as exc:
            raise PreviewEngineError(
                "SOURCE_CHANGED_AFTER_PROBE",
                "bound source is no longer available",
                details={"path": self.path},
            ) from exc
        if current != self._source_stat:
            raise PreviewEngineError(
                "SOURCE_CHANGED_AFTER_PROBE",
                "bound source metadata changed after certification",
                details={
                    "path": self.path,
                    "expected_size": self._source_stat[0],
                    "actual_size": current[0],
                    "expected_mtime_ns": self._source_stat[1],
                    "actual_mtime_ns": current[1],
                },
            )

    def _validate_request_source_locked(self, request: CertifiedEdlRequest) -> None:
        media = request.media_info
        if Path(media.source_path).resolve() != Path(self.path).resolve():
            raise PreviewEngineError(
                "SOURCE_MISMATCH",
                "certified EDL request belongs to another source",
                details={"engine": self.path, "media": str(media.source_path)},
            )
        if self._media_info is not None and media.source_sha256 != self._media_info.source_sha256:
            raise PreviewEngineError(
                "CERTIFICATION_MISMATCH",
                "certified EDL request does not match the bound source certification",
            )

    def _require_player_locked(self) -> Any:
        if self._closed:
            raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")
        if not self._started or self._player is None:
            self.start()
        assert self._player is not None
        return self._player

    def _ensure_loaded_locked(self, path: str | os.PathLike[str], mode: str) -> None:
        player = self._require_player_locked()
        source = str(normalized_source_path(path))
        if self._mode == mode and getattr(self, "_loaded_path", None) == source:
            return
        self._load_generation += 1
        self._mode = mode
        self._loaded_path = source
        self._loaded_ready = False
        self._pending_seek = None
        self._pending_resume = False
        self._pending_speed = None
        self._last_time_pos = None
        self._source_frame = 0
        self._play_end_reason = None
        # Hold the player paused across the load: pause is a global property
        # that persists into the next file, and the pending seek must land
        # before anything is presented.
        try:
            player.pause = True
        except Exception:
            try:
                player.command("set", "pause", "yes")
            except Exception:
                pass
        # A synchronous loadfile blocks the caller for the whole demuxer
        # open; a real EDL with thousands of segments takes seconds and
        # freezes the Tk owner thread ("not responding").  Issue the load
        # fire-and-forget when the binding supports async commands; the
        # event thread completes the sequence via file-loaded.  Fake and
        # reduced bindings keep the synchronous play() route.
        loader = getattr(player, "command_async", None)
        if callable(loader):
            try:
                loader("loadfile", source, "replace")
                return
            except Exception:
                pass
        player.play(source)

    def _command_seek_locked(self, seconds: Fraction, *, exact: bool) -> None:
        player = self._require_player_locked()
        mode = "absolute+exact" if exact else "absolute"
        player.command("seek", _fraction_seconds(seconds), mode)
        self._stats["seek_count"] += 1

    def _flush_pending_locked(self) -> None:
        if self._closed or not self._loaded_ready or self._player is None:
            return
        try:
            if self._pending_speed is not None:
                self._set_speed_locked(self._pending_speed)
            if self._pending_seek is not None:
                seconds, exact = self._pending_seek
                self._command_seek_locked(seconds, exact=exact)
                self._pending_seek = None
            if self._pending_resume:
                self._pending_resume = False
                self._resume_locked()
            self._pending_speed = None
        except Exception:
            # Keep the action pending for the next owner-thread poll.  This
            # handles libmpv's short transition window after file-loaded.
            return

    def _source_time_locked(self, frame: int, *, exact: bool) -> Fraction:
        timeline = self._timeline
        if timeline is None:
            if exact:
                raise PreviewEngineError(
                    "CERTIFICATION_REQUIRED",
                    "absolute+exact source seek requires certified PTS rows",
                )
            if frame != 0:
                raise PreviewEngineError(
                    "CERTIFICATION_REQUIRED",
                    "non-zero source seek requires certified PTS rows",
                )
            return Fraction(0, 1)
        self._assert_bound_source_current_locked()
        if frame < 0 or frame >= timeline.frame_count:
            raise PreviewEngineError(
                "SOURCE_FRAME_OUT_OF_RANGE",
                "source frame is outside the certified PTS table",
                details={"frame": frame, "count": timeline.frame_count},
            )
        return timeline.rows[frame].pts * timeline.time_base

    def seek_source(self, request: SourceSeekRequest) -> bool:
        with self._lock:
            if self._closed:
                raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")
            seconds = self._source_time_locked(request.source_frame, exact=request.exact)
            self._timeline_revision = request.timeline_revision
            self._ensure_loaded_locked(self.path, "source")
            if self._loaded_ready:
                self._command_seek_locked(seconds, exact=request.exact)
            else:
                self._pending_seek = (seconds, request.exact)
            self._source_frame = request.source_frame
            return True

    def play(self, request: PreviewPlayRequest) -> bool:
        with self._lock:
            if self._closed:
                raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")
            if request.speedup_1x or request.speedup_02 or request.speed_segments:
                # These are source-frame business policies owned by
                # VideoIOThread.  Applying only part of them in native mpv
                # would silently change the edit preview, so the native
                # skeleton fails closed and leaves the caller free to select
                # CvEngine explicitly.
                raise PreviewEngineError(
                    "MPV_SPEED_POLICY_UNSUPPORTED",
                    "native mpv preview does not yet implement frame-speed policies",
                )
        if request.skip_trimmed and request.timeline_plan.deleted_ranges:
            if self._media_info is None or not self._media_info.complete_for_export:
                raise PreviewEngineError(
                    "CERTIFICATION_REQUIRED",
                    "EDL preview requires current certified media",
                )
            return self.play_edl(
                CertifiedEdlRequest(
                    media_info=self._media_info,
                    timeline_plan=request.timeline_plan,
                    project_generation=request.project_generation,
                    timeline_revision=request.timeline_revision,
                ),
                start_frame=request.start_frame,
                playback_rate=request.playback_rate,
            )
        with self._lock:
            if self._closed:
                raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")
            self._project_generation = request.project_generation
            self._timeline_revision = request.timeline_revision
            self._ensure_loaded_locked(self.path, "source")
            exact = self._timeline is not None
            seconds = self._source_time_locked(request.start_frame, exact=exact)
            if self._loaded_ready:
                self._set_speed_locked(request.playback_rate)
                self._command_seek_locked(seconds, exact=exact)
                self._resume_locked()
            else:
                self._pending_speed = request.playback_rate
                self._pending_seek = (seconds, exact)
                self._pending_resume = True
            return True

    def play_edl(
        self,
        request: CertifiedEdlRequest,
        *,
        start_frame: int,
        playback_rate: float = 1.0,
    ) -> bool:
        with self._lock:
            if self._closed:
                raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")
            rate = float(playback_rate)
            if rate <= 0 or not math.isfinite(rate):
                raise ValueError("playback_rate must be a finite positive number")
            if isinstance(start_frame, bool) or not isinstance(start_frame, int):
                raise TypeError("start_frame must be an integer")
            if start_frame < 0 or start_frame >= request.timeline_plan.total_frames:
                raise PreviewEngineError(
                    "SOURCE_FRAME_OUT_OF_RANGE",
                    "start frame is outside the timeline plan",
                    details={"start_frame": start_frame},
                )
            self._validate_request_source_locked(request)
            # Serialize EDL publication with close().  Once close begins, no
            # command may publish a new preview artifact or reach libmpv.
            # Rebuilding the EDL re-derives the certified timeline; repeated
            # toggles of the same plan reuse the built artifact instead, and
            # a fresh build passes the engine's already-bound timeline so
            # the multi-gigabyte source is stat-checked, not re-hashed.
            cache_key = (
                request.media_info.source_sha256,
                request.timeline_plan.fingerprint,
                request.project_generation,
                request.timeline_revision,
            )
            if (
                self._edl is not None
                and self._edl_cache_key == cache_key
                and self._edl.path.is_file()
            ):
                edl = self._edl
            else:
                self._assert_bound_source_current_locked()
                edl = build_certified_edl(
                    request, self._edl_dir, timeline=self._timeline
                )
                self._edl_cache_key = cache_key
            seconds = edl.virtual_time_for_source(start_frame, snap=True)
            self._project_generation = request.project_generation
            self._timeline_revision = request.timeline_revision
            self._edl = edl
            self._ensure_loaded_locked(edl.path, "edl")
            if self._loaded_ready:
                self._command_seek_locked(seconds, exact=True)
                self._set_speed_locked(rate)
                self._resume_locked()
            else:
                self._pending_seek = (seconds, True)
                self._pending_speed = rate
                self._pending_resume = True
            return True

    def _set_speed_locked(self, value: float) -> None:
        player = self._require_player_locked()
        rate = float(value)
        if rate <= 0 or not math.isfinite(rate):
            raise ValueError("playback rate must be positive")
        try:
            player.speed = rate
        except Exception:
            player.command("set", "speed", str(rate))

    def _resume_locked(self) -> None:
        player = self._require_player_locked()
        try:
            player.pause = False
        except Exception:
            player.command("set", "pause", "no")
        self._playing = True
        self._play_end_reason = "playing"
        self._stats["playback_active"] = True
        self._stats["play_end_reason"] = "playing"

    def stop(self) -> bool:
        with self._lock:
            if self._closed:
                raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")
            if self._player is None:
                return False
            # A stop issued during an asynchronous load must cancel the
            # queued seek/resume pair; otherwise the later file-loaded event
            # would unexpectedly restart playback.
            self._pending_seek = None
            self._pending_resume = False
            self._pending_speed = None
            try:
                self._player.pause = True
            except Exception:
                try:
                    self._player.command("set", "pause", "yes")
                except Exception:
                    return False
            self._playing = False
            self._play_end_reason = "stop"
            self._stats["playback_active"] = False
            self._stats["play_end_reason"] = "stop"
            return True

    def send(self, command: dict[str, Any]) -> bool:
        """Translate the existing VideoIOThread command shape during migration."""

        if not isinstance(command, dict):
            raise TypeError("preview command must be a mapping")
        kind = command.get("type")
        if kind == "quit":
            return self.close()
        with self._lock:
            if self._closed:
                raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")
        if kind in {"seek", "seek_latest"}:
            request = SourceSeekRequest(
                source_frame=int(command.get("frame", 0)),
                canvas_size=tuple(command.get("canvas_wh") or (1, 1)),
                timeline_revision=int(command.get("timeline_revision", self._timeline_revision)),
                exact=self._timeline is not None,
                latest_only=kind == "seek_latest",
            )
            return self.seek_source(request)
        if kind == "stop":
            return self.stop()
        if kind == "set_pace_mode":
            self.set_pace_mode(str(command.get("mode", "opt")))
            return True
        if kind == "play":
            params = dict(command.get("params") or {})
            total = self.total or int(params.get("total_frames", 0) or 0)
            if total <= 0:
                raise PreviewEngineError("FRAME_COUNT_REQUIRED", "mpv preview needs a source frame count")
            plan = TimelinePlan.from_deleted_ranges(
                total,
                params.get("pause_segs") or (),
            )
            speed_multiplier = float(params.get("speed_multiplier", 1.0) or 1.0)
            preview_step = max(1, int(params.get("preview_step", 1) or 1))
            playback_rate = preview_step if abs(speed_multiplier - 1.0) < 1e-9 else 1.0 / speed_multiplier
            request = PreviewPlayRequest(
                start_frame=int(params.get("start_frame", 0) or 0),
                playback_rate=playback_rate,
                preview_step=preview_step,
                speed_multiplier=speed_multiplier,
                skip_trimmed=bool(params.get("skip_trimmed", False)),
                speedup_1x=bool(params.get("speedup_1x", False)),
                speedup_02=bool(params.get("speedup_02", False)),
                speedup_02_factor=max(1, int(params.get("speedup_02_factor", 1) or 1)),
                timeline_plan=plan,
                speed_segments=tuple(tuple(value) for value in params.get("speed_segs") or ()),
                canvas_size=tuple(params.get("canvas_wh") or (1, 1)),
                project_generation=int(params.get("project_generation", self._project_generation)),
                timeline_revision=int(params.get("timeline_revision", self._timeline_revision)),
                preview_step_cap=max(1, int(params.get("preview_step_cap", 3) or 3)),
                skip_trim_min_span=max(0, int(params.get("skip_trim_min_span", 0) or 0)),
            )
            return self.play(request)
        raise PreviewEngineError("COMMAND_UNSUPPORTED", f"unsupported mpv preview command: {kind!r}")

    def set_viewport(self, width: int, height: int, dpi_scale: float = 1.0) -> None:
        if width < 1 or height < 1 or dpi_scale <= 0:
            raise ValueError("viewport dimensions and DPI scale must be positive")
        # The WID child follows the host HWND automatically.  Keep this method
        # explicit so the Tk owner can call it after resize/DPI transitions.
        with self._lock:
            if self._closed:
                raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")
            self._viewport = (int(width), int(height), float(dpi_scale))
            self._enqueue_event(
                {
                    "event": "viewport",
                    "width": int(width),
                    "height": int(height),
                    "dpi_scale": float(dpi_scale),
                    "generation": self._load_generation,
                    "mode": self._mode,
                    "project_generation": self._project_generation,
                    "timeline_revision": self._timeline_revision,
                }
            )

    # ------------------------------------------------------------------
    # Stats/lifecycle
    # ------------------------------------------------------------------
    def _frame_for_position_locked(self, position: float) -> int:
        if self._mode == "edl" and self._edl is not None:
            return self._edl.source_frame_for_virtual_time(Fraction(str(max(0.0, position))))
        if self._timeline is None:
            return self._source_frame
        target = Fraction(str(max(0.0, position)))
        times = self._timeline_times
        clean_start = min(self._timeline_clean_start, len(times))
        if clean_start < len(times):
            if clean_start and target < times[clean_start]:
                # The target is still inside B' anomalous head territory.
                # The clean monotonic binary search cannot describe it.
                return min(
                    range(clean_start),
                    key=lambda index: abs(times[index] - target),
                )
            # For the monotonic portion of a VFR timeline, a displayed time
            # belongs to the most recent frame whose PTS is not after it.
            # Nearest-PTS lookup advances the source pointer halfway through
            # a long frame, which is wrong for cut editing and especially
            # visible on variable-duration material.
            offset = bisect_left(times[clean_start:], target)
            index = clean_start + offset
            if index < len(times) and times[index] == target:
                return index
            return max(clean_start, min(len(times) - 1, index - 1))
        # B' permits only an anomalous source head.  Sorting or deduplicating
        # those rows would falsify the certification, so retain the explicit
        # nearest-row fallback inside that bounded window.
        return min(range(len(times)), key=lambda index: abs(times[index] - target))

    def set_pace_mode(self, mode: str) -> None:
        with self._lock:
            if self._closed:
                raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")
            self._pace_mode = "base" if str(mode).lower() in {"base", "off", "0", "false"} else "opt"
            self._stats["pace_mode"] = self._pace_mode

    def get_pace_mode(self) -> str:
        return self._pace_mode

    def snapshot_perf(self) -> dict[str, Any]:
        with self._lock:
            value = dict(self._stats)
            value.update(
                {
                    "engine": self.engine_name,
                    "mode": self._mode,
                    "generation": self._load_generation,
                    "project_generation": self._project_generation,
                    "timeline_revision": self._timeline_revision,
                    "source_frame": self._source_frame,
                    "playing": self._playing,
                    "playback_active": self._playing,
                    "play_end_reason": self._play_end_reason,
                    "event_queue_drops": self._event_drop_count,
                    "duration": self._duration,
                    "viewport": self._viewport,
                    "preview_bias_frames": (
                        self._edl.preview_bias_frames
                        if self._mode == "edl" and self._edl is not None
                        else 0
                    ),
                    "preview_bias_reason": (
                        self._edl.preview_bias_reason
                        if self._mode == "edl" and self._edl is not None
                        else None
                    ),
                    "preview_tick_collision_frames": (
                        self._edl.preview_tick_collision_frames
                        if self._mode == "edl" and self._edl is not None
                        else ()
                    ),
                }
            )
            return value

    def snapshot(self) -> dict[str, Any]:
        return self.snapshot_perf()

    def reset_perf_stats(self) -> None:
        with self._lock:
            if self._closed:
                raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")
            self._stats = self._new_stats()
            self._stats["pace_mode"] = self._pace_mode

    def begin_perf_segment(self) -> None:
        with self._lock:
            if self._closed:
                raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")
            self.reset_perf_stats()
            self._stats["playback_active"] = self._playing

    def is_playback_active(self) -> bool:
        with self._lock:
            return bool(self._playing)

    def is_alive(self) -> bool:
        with self._lock:
            # A timed-out close keeps the native player owned by this engine so
            # the caller can retry termination.  Report that resource as alive
            # until the terminate worker has actually finished, even though
            # ``_closed`` rejects all new playback commands.
            return self._player is not None

    def close(self, timeout: float = 1.0) -> bool:
        wait_s = max(0.0, float(timeout))
        with self._lock:
            if self._closed and self._player is None:
                return True
            player = self._player
            self._closed = True
            self._playing = False
            self._play_end_reason = "shutdown"
            self._loaded_ready = False
            self._pending_seek = None
            self._pending_resume = False
            self._pending_speed = None
            self._stats["playback_active"] = False
            self._stats["play_end_reason"] = "shutdown"
            if player is None:
                handle = self._dll_handle
                self._dll_handle = None
                # There is no native player to terminate.  Release any DLL
                # search-path handle acquired before a factory/bootstrap error.
                if handle is not None:
                    try:
                        handle.close()
                    except Exception:
                        pass
                return True

            if not self._callbacks_detached:
                for name, callback in tuple(self._property_callbacks):
                    try:
                        player.unobserve_property(callback)
                    except TypeError:
                        try:
                            player.unobserve_property(name, callback)
                        except Exception:
                            pass
                    except Exception:
                        pass
                self._property_callbacks.clear()
                if self._event_callback is not None:
                    try:
                        player.unregister_event_callback(self._event_callback)
                    except Exception:
                        pass
                self._callbacks_detached = True

            done = self._terminate_done
            thread = self._terminate_thread
            if done is None or thread is None:
                done = threading.Event()
                self._terminate_done = done
                self._terminate_error = None

                def terminate() -> None:
                    try:
                        player.terminate()
                    except BaseException as exc:
                        with self._lock:
                            self._terminate_error = exc
                    finally:
                        done.set()

                thread = threading.Thread(
                    target=terminate,
                    name="arknight-mpv-close",
                    daemon=True,
                )
                self._terminate_thread = thread
                thread.start()

        thread.join(wait_s)
        if not done.is_set():
            # Keep the player and termination state so a later close() can
            # retry the same native instance instead of falsely reporting a
            # successful close after a timed-out first attempt.
            return False
        with self._lock:
            terminate_error = self._terminate_error
            if terminate_error is not None:
                # Keep ownership and permit a later close retry.  Native
                # bindings occasionally surface a transient shutdown error.
                self._terminate_thread = None
                self._terminate_done = None
                self._terminate_error = None
                return False
        with self._lock:
            if self._terminate_done is done:
                self._player = None
                self._started = False
                self._terminate_thread = None
                self._terminate_done = None
                self._terminate_error = None
                handle = self._dll_handle
                self._dll_handle = None
            else:
                handle = None
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
        return True


__all__ = ["MpvEngine"]
