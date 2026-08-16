"""Shared, immutable export requests and atomic export orchestration.

The existing analyzer owns FFmpeg/OpenCV encoding details.  This module keeps
the application-facing contract small and makes full and ranged exports share
the same request validation, cancellation and final-file checks.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import tempfile
from types import MappingProxyType
from typing import Any, Callable, Literal, Mapping, Sequence

from media_info import (
    FramePtsCertification,
    HEAD_ANOMALY_FRAME_LIMIT,
    MediaInfo,
    MediaInfoError,
    ToolInfo,
    _sha256_file,
)
from pts_timeline import CertifiedPtsTimeline, PtsTickInterval
from timeline_plan import Range, TimelinePlan


ExportMode = Literal["full", "ranges"]
ProgressCallback = Callable[..., Any]
CancelCallback = Callable[[], Any]
CommitCallback = Callable[[str, str], Any]

HEAD_TICK_COLLISION_MAX_DROPS = 1


def _head_tick_collision_drops(
    pts_timeline: CertifiedPtsTimeline,
    effective_ranges: Sequence[Range],
    pts_intervals: Sequence[PtsTickInterval],
) -> tuple[Mapping[str, int], ...]:
    """Kept frames that pure tick-based trimming provably cannot represent.

    A kept frame whose pts falls outside its own interval's tick span shares
    that tick with content on the other side of the cut, so FFmpeg
    ``trim``/``select`` addressing by PTS cannot keep it while dropping the
    neighbour.  The B' ruling tolerates this only inside the registered head
    window and at most ``HEAD_TICK_COLLISION_MAX_DROPS`` times per export;
    anything else fails closed.
    """
    drops: list[dict[str, int]] = []
    for (start, end), interval in zip(effective_ranges, pts_intervals):
        for frame in range(start, end):
            row = pts_timeline.rows[frame]
            if row.pts < interval.start_tick or row.pts >= interval.end_tick:
                drops.append(
                    {
                        "n": row.n,
                        "pts": row.pts,
                        "interval_start_frame": start,
                        "interval_end_frame": end,
                        "interval_start_tick": interval.start_tick,
                        "interval_end_tick": interval.end_tick,
                    }
                )
    for drop in drops:
        if drop["n"] >= HEAD_ANOMALY_FRAME_LIMIT:
            raise MediaInfoError(
                "FRAME_PTS_TICK_COLLISION_UNADJUDICATED",
                "a kept frame outside the head window is unrepresentable by tick trimming",
                details=dict(drop),
            )
    if len(drops) > HEAD_TICK_COLLISION_MAX_DROPS:
        raise MediaInfoError(
            "FRAME_PTS_TICK_COLLISION_UNADJUDICATED",
            "more kept frames than the adjudication policy allows are "
            "unrepresentable by tick trimming",
            details={"drops": [dict(drop) for drop in drops]},
        )
    return tuple(drops)


@dataclass(frozen=True, slots=True)
class ExportRequest:
    source_path: Path
    output_path: Path
    timeline_plan: TimelinePlan
    fps: float
    quality: int
    mode: ExportMode = "full"
    ranges: tuple[Range, ...] = ()
    use_gpu: bool = False
    gpu_encoder: str = ""
    ffmpeg_path: str | None = None
    ffprobe_path: str | None = None
    include_audio: bool = True
    allow_audio_drop: bool = False
    media_info: MediaInfo | None = None
    enforce_media_certification: bool = True

    def __post_init__(self) -> None:
        source = Path(self.source_path).expanduser().resolve()
        output = Path(self.output_path).expanduser().resolve()
        if source == output:
            raise ValueError("export destination must not replace the source video")
        if not math.isfinite(float(self.fps)) or float(self.fps) <= 0:
            raise ValueError("fps must be finite and positive")
        if isinstance(self.quality, bool) or not isinstance(self.quality, int):
            raise ValueError("quality must be an integer")
        if self.mode not in {"full", "ranges"}:
            raise ValueError("mode must be full or ranges")
        canonical = TimelinePlan.from_kept_ranges(
            self.timeline_plan.total_frames, self.ranges
        ).kept_ranges if self.mode == "ranges" else ()
        if self.mode == "ranges" and not canonical:
            raise ValueError("ranges export requires at least one non-empty range")
        if self.mode == "full" and self.ranges:
            raise ValueError("full export cannot carry ranges")
        object.__setattr__(self, "source_path", source)
        object.__setattr__(self, "output_path", output)
        object.__setattr__(self, "fps", float(self.fps))
        object.__setattr__(self, "ranges", tuple(canonical))

    @classmethod
    def full(
        cls,
        source_path: str | os.PathLike[str],
        output_path: str | os.PathLike[str],
        timeline_plan: TimelinePlan,
        *,
        fps: float,
        quality: int,
        **options: Any,
    ) -> "ExportRequest":
        return cls(source_path, output_path, timeline_plan, fps, quality, **options)

    @classmethod
    def ranges_export(
        cls,
        source_path: str | os.PathLike[str],
        output_path: str | os.PathLike[str],
        timeline_plan: TimelinePlan,
        ranges: Sequence[Sequence[int]],
        *,
        fps: float,
        quality: int,
        **options: Any,
    ) -> "ExportRequest":
        return cls(
            source_path,
            output_path,
            timeline_plan,
            fps,
            quality,
            mode="ranges",
            ranges=tuple((start, end) for start, end in ranges),
            **options,
        )


@dataclass(frozen=True, slots=True)
class ExportResult:
    output_path: Path
    written_frames: int
    total_frames: int
    mode: ExportMode
    metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "output_path", Path(self.output_path).resolve())
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class ExportValidationSnapshot:
    """One immutable validation result shared by an authenticated export.

    The snapshot owns the loaded PTS timeline and the exact ranges used by
    the request.  A final identity check is still required immediately before
    publishing output, so this is a bounded I/O reduction rather than a
    permanent cache of mutable media files.
    """

    source_path: Path
    source_sha256: str
    source_size: int
    source_mtime_ns: int
    ffmpeg: ToolInfo
    ffprobe: ToolInfo
    certification: FramePtsCertification
    pts_timeline: CertifiedPtsTimeline
    effective_ranges: tuple[Range, ...]
    pts_intervals: tuple[PtsTickInterval, ...]
    expected_written: int
    tick_collision_drops: tuple[Mapping[str, int], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_path", Path(self.source_path).resolve())
        object.__setattr__(self, "effective_ranges", tuple(self.effective_ranges))
        object.__setattr__(self, "pts_intervals", tuple(self.pts_intervals))
        object.__setattr__(self, "tick_collision_drops", tuple(self.tick_collision_drops))
        if self.expected_written <= 0:
            raise ValueError("an export validation snapshot requires kept frames")
        if self.expected_written != sum(
            end - start for start, end in self.effective_ranges
        ):
            raise ValueError("snapshot frame count does not match its ranges")

    def assert_current(self) -> None:
        """Fail closed if any bound source, tool, or evidence file changed."""

        try:
            source_stat = self.source_path.stat()
        except OSError as exc:
            raise MediaInfoError(
                "SOURCE_CHANGED_DURING_EXPORT",
                "source media is no longer available during export",
                details={"path": str(self.source_path)},
            ) from exc
        try:
            source_sha256 = _sha256_file(self.source_path)
        except OSError as exc:
            raise MediaInfoError(
                "SOURCE_CHANGED_DURING_EXPORT",
                "source media is no longer readable during export",
                details={"path": str(self.source_path)},
            ) from exc
        if (
            source_stat.st_size != self.source_size
            or source_stat.st_mtime_ns != self.source_mtime_ns
            or source_sha256.lower() != self.source_sha256.lower()
        ):
            raise MediaInfoError(
                "SOURCE_CHANGED_DURING_EXPORT",
                "source media changed after export validation",
                details={"path": str(self.source_path)},
            )
        for tool, name in ((self.ffmpeg, "ffmpeg"), (self.ffprobe, "ffprobe")):
            try:
                tool_current = (
                    tool.path.is_file()
                    and _sha256_file(tool.path).lower() == tool.sha256.lower()
                )
            except OSError:
                tool_current = False
            if not tool_current:
                raise MediaInfoError(
                    "TOOL_CHANGED_DURING_EXPORT",
                    f"registered {name} executable changed after export validation",
                    details={"path": str(tool.path)},
                )
        try:
            evidence_sha256 = _sha256_file(self.certification.evidence_path)
        except OSError as exc:
            raise MediaInfoError(
                "FRAME_PTS_EVIDENCE_CHANGED",
                "frame PTS evidence is no longer available during export",
                details={"path": str(self.certification.evidence_path)},
            ) from exc
        if evidence_sha256.lower() != self.certification.evidence_sha256.lower():
            raise MediaInfoError(
                "FRAME_PTS_EVIDENCE_CHANGED",
                "frame PTS evidence changed after export validation",
                details={"path": str(self.certification.evidence_path)},
            )

    def metadata(self) -> dict[str, Any]:
        return {
            "media_authority": "certified_source_pts",
            "pts_table_loaded": True,
            "pts_table_consumed": False,
            "pts_consumer": "ffmpeg_trim_pts_concat",
            "pts_tick_intervals": tuple(
                interval.as_dict() for interval in self.pts_intervals
            ),
            "media_source_sha256": self.source_sha256,
            "frame_pts_evidence_sha256": self.certification.evidence_sha256,
            "pts_table_sha256": self.certification.pts_table_sha256,
            "frame_pts_time_base": (
                f"{self.certification.time_base.numerator}/"
                f"{self.certification.time_base.denominator}"
            ),
            "certified_frame_count": self.certification.frame_count,
            "ffmpeg_path": str(self.ffmpeg.path),
            "ffprobe_path": str(self.ffprobe.path),
            "ffmpeg_sha256": self.ffmpeg.sha256,
            "ffprobe_sha256": self.ffprobe.sha256,
            "head_anomaly_adjudication": {
                "policy": "head-restricted-2026-08-16",
                "head_frame_limit": HEAD_ANOMALY_FRAME_LIMIT,
                "head_anomaly_limit_ticks": self.pts_timeline.head_anomaly_limit,
                "tick_collision_drops": [dict(drop) for drop in self.tick_collision_drops],
                "expected_on_disk_frames": self.expected_written
                - len(self.tick_collision_drops),
            },
        }


class MediaExporter:
    """Execute a validated request through the existing analyzer primitives."""

    @staticmethod
    def _validate_certified_request(
        request: ExportRequest,
        *,
        source_path_override: str | None,
    ) -> ExportValidationSnapshot | None:
        if not request.enforce_media_certification:
            return None
        media = request.media_info
        if not isinstance(media, MediaInfo):
            raise MediaInfoError(
                "MEDIA_INFO_REQUIRED",
                "production export requires a MediaInfo snapshot with FramePtsCertification",
            )
        if not media.complete_for_export:
            raise MediaInfoError(
                "MEDIA_INFO_NOT_EXPORT_READY",
                "production export requires complete FramePtsCertification and current source evidence",
            )
        if media.source_path != request.source_path:
            raise MediaInfoError(
                "MEDIA_INFO_SOURCE_MISMATCH",
                "MediaInfo source does not match the export request",
            )
        source_path = Path(source_path_override).expanduser().resolve() if source_path_override else request.source_path
        if source_path != request.source_path:
            raise MediaInfoError(
                "EXPORT_SOURCE_OVERRIDE_MISMATCH",
                "source_path_override cannot select a different source",
            )
        certification = media.frame_pts_certification
        if certification is None:
            raise MediaInfoError(
                "FRAME_PTS_CERTIFICATION_REQUIRED",
                "production export requires a bound FramePtsCertification",
            )
        if certification.frame_count != request.timeline_plan.total_frames:
            raise MediaInfoError(
                "FRAME_PTS_FRAME_COUNT_MISMATCH",
                "certified frame count does not match the export timeline",
                details={
                    "certified": certification.frame_count,
                    "timeline": request.timeline_plan.total_frames,
                },
            )
        for requested, bound, label in (
            (request.ffmpeg_path, media.ffmpeg, "ffmpeg"),
            (request.ffprobe_path, media.ffprobe, "ffprobe"),
        ):
            if bound is None:
                raise MediaInfoError(
                    "EXPORT_TOOL_MISMATCH",
                    f"export requires a bound {label} executable",
                )
            if requested is not None and Path(requested).expanduser().resolve() != bound.path:
                raise MediaInfoError(
                    "EXPORT_TOOL_MISMATCH",
                    f"export {label} path differs from the MediaInfo snapshot",
                )
        # This is the only timeline construction for the whole export.  The
        # export body consumes the immutable object below instead of reparsing
        # the evidence and rebuilding all intervals.
        pts_timeline = CertifiedPtsTimeline.from_validated_media_info(media)
        effective_ranges = (
            request.timeline_plan.kept_ranges
            if request.mode == "full"
            else request.ranges
        )
        pts_intervals = tuple(
            pts_timeline.interval_for_range(value) for value in effective_ranges
        )
        expected_written = sum(end - start for start, end in effective_ranges)
        if expected_written <= 0:
            raise MediaInfoError(
                "EXPORT_EMPTY_TIMELINE",
                "certified export requires at least one kept frame",
            )
        tick_collision_drops = _head_tick_collision_drops(
            pts_timeline, effective_ranges, pts_intervals
        )
        assert media.ffmpeg is not None and media.ffprobe is not None
        return ExportValidationSnapshot(
            source_path=media.source_path,
            source_sha256=media.source_sha256,
            source_size=media.source_size,
            source_mtime_ns=media.source_mtime_ns,
            ffmpeg=media.ffmpeg,
            ffprobe=media.ffprobe,
            certification=certification,
            pts_timeline=pts_timeline,
            effective_ranges=tuple(effective_ranges),
            pts_intervals=pts_intervals,
            expected_written=expected_written,
            tick_collision_drops=tick_collision_drops,
        )

    def export(
        self,
        request: ExportRequest,
        *,
        progress_cb: ProgressCallback | None = None,
        cancel_cb: CancelCallback | None = None,
        commit_cb: CommitCallback | None = None,
        preflight: dict[str, Any] | None = None,
        source_path_override: str | None = None,
    ) -> ExportResult:
        import analyzer

        validation_snapshot = self._validate_certified_request(
            request,
            source_path_override=source_path_override,
        )
        authority_metadata = (
            {"media_authority": "legacy_unverified"}
            if validation_snapshot is None
            else validation_snapshot.metadata()
        )
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        source_path = source_path_override or str(request.source_path)

        if validation_snapshot is not None:
            media = request.media_info
            assert isinstance(media, MediaInfo)
            certification = validation_snapshot.certification
            for key, bound in (
                ("ffmpeg_path", validation_snapshot.ffmpeg.path),
                ("ffprobe_path", validation_snapshot.ffprobe.path),
            ):
                if preflight is not None and preflight.get(key):
                    if Path(preflight[key]).expanduser().resolve() != bound:
                        raise MediaInfoError(
                            "EXPORT_TOOL_MISMATCH",
                            f"preflight {key} differs from the certified MediaInfo tool",
                        )

            # Recheck the immutable identities immediately before command
            # construction.  The legacy frame/FPS exporter is unreachable for
            # a certified request.
            validation_snapshot.assert_current()
            pts_timeline = validation_snapshot.pts_timeline
            pts_intervals = tuple(
                interval.as_dict() for interval in validation_snapshot.pts_intervals
            )
            expected_written = validation_snapshot.expected_written

            staging_fd, staging_name = tempfile.mkstemp(
                prefix=f".{request.output_path.stem}.",
                suffix=f".partial{request.output_path.suffix or '.mp4'}",
                dir=str(request.output_path.parent),
            )
            os.close(staging_fd)
            try:
                os.remove(staging_name)
            except OSError:
                pass
            try:
                written, total, metadata = analyzer.export_pts_schedule(
                    source_path,
                    staging_name,
                    pts_intervals,
                    time_base=pts_timeline.time_base,
                    source_frame_count=pts_timeline.frame_count,
                    reported_total_frames=(
                        pts_timeline.frame_count
                        if request.mode == "full"
                        else expected_written
                    ),
                    quality=request.quality,
                    use_gpu=request.use_gpu,
                    gpu_encoder=request.gpu_encoder,
                    ffmpeg_path=str(validation_snapshot.ffmpeg.path),
                    include_audio=request.include_audio,
                    source_has_audio=media.has_audio,
                    frame_pts_status=certification.status,
                    progress_cb=progress_cb,
                    cancel_cb=cancel_cb,
                )
                if cancel_cb is not None:
                    cancel_cb()
                if (
                    written != expected_written
                    or written <= 0
                    or not os.path.isfile(staging_name)
                    or os.path.getsize(staging_name) <= 0
                ):
                    raise RuntimeError(
                        "认证 PTS 导出器生成的帧数或临时文件无效"
                    )
                validation_snapshot.assert_current()
                if commit_cb is None:
                    os.replace(staging_name, request.output_path)
                else:
                    commit_cb(staging_name, str(request.output_path))
                return ExportResult(
                    request.output_path,
                    int(written),
                    int(total),
                    request.mode,
                    {
                        **authority_metadata,
                        **(metadata or {}),
                        "pts_tick_intervals": pts_intervals,
                        "pts_table_consumed": True,
                        "timeline_fingerprint": request.timeline_plan.fingerprint,
                        "ffmpeg_path": str(validation_snapshot.ffmpeg.path),
                        "ffprobe_path": str(validation_snapshot.ffprobe.path),
                    },
                )
            finally:
                if os.path.isfile(staging_name):
                    try:
                        os.remove(staging_name)
                    except OSError:
                        pass

        if request.mode == "full":
            written, total, metadata = analyzer.export_video(
                source_path,
                str(request.output_path),
                request.timeline_plan,
                request.fps,
                request.quality,
                progress_cb,
                use_gpu=request.use_gpu,
                gpu_encoder=request.gpu_encoder,
                ffmpeg_path=request.ffmpeg_path,
                ffprobe_path=request.ffprobe_path,
                include_audio=request.include_audio,
                allow_audio_drop=request.allow_audio_drop,
                cancel_cb=cancel_cb,
                commit_cb=commit_cb,
                preflight=preflight,
            )
            return ExportResult(
                request.output_path,
                int(written),
                int(total),
                request.mode,
                {**authority_metadata, **(metadata or {})},
            )

        staging_fd, staging_name = tempfile.mkstemp(
            prefix=f".{request.output_path.stem}.",
            suffix=f".partial{request.output_path.suffix or '.mp4'}",
            dir=str(request.output_path.parent),
        )
        os.close(staging_fd)
        try:
            os.remove(staging_name)
        except OSError:
            pass
        try:
            range_kwargs = {
                "use_gpu": request.use_gpu,
                "gpu_encoder": request.gpu_encoder,
                "ffmpeg_path": request.ffmpeg_path,
                "ffprobe_path": request.ffprobe_path,
                "cancel_cb": cancel_cb,
            }
            if progress_cb is not None:
                range_kwargs["progress_cb"] = progress_cb
            written, total = analyzer.export_ranges(
                source_path,
                staging_name,
                list(request.ranges),
                request.fps,
                request.quality,
                **range_kwargs,
            )
            if cancel_cb is not None:
                cancel_cb()
            if written <= 0 or not os.path.isfile(staging_name) or os.path.getsize(staging_name) <= 0:
                raise RuntimeError("exporter did not produce a valid ranged output")
            if commit_cb is None:
                os.replace(staging_name, request.output_path)
            else:
                commit_cb(staging_name, str(request.output_path))
            return ExportResult(
                request.output_path,
                int(written),
                int(total),
                request.mode,
                {
                    **authority_metadata,
                    "audio_mode": "disabled",
                    "ffprobe_path": request.ffprobe_path,
                    "timeline_fingerprint": request.timeline_plan.fingerprint,
                },
            )
        finally:
            if os.path.isfile(staging_name):
                try:
                    os.remove(staging_name)
                except OSError:
                    pass
