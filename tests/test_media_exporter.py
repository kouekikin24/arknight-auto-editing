from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import media_info
import media_exporter
from media_exporter import ExportRequest, ExportResult, MediaExporter
from timeline_plan import TimelinePlan


class MediaExporterTests(unittest.TestCase):
    @staticmethod
    def _certified_media(root: Path, frame_count: int) -> media_info.MediaInfo:
        source = root / "source.mp4"
        ffprobe = root / "ffprobe.exe"
        ffmpeg = root / "ffmpeg.exe"
        evidence = root / "frame-pts.json"
        source.write_bytes(b"source")
        ffprobe.write_bytes(b"ffprobe-tool")
        ffmpeg.write_bytes(b"ffmpeg-tool")
        source_sha256 = media_info._sha256_file(source)
        ffprobe_info = media_info.ToolInfo(
            ffprobe,
            media_info._sha256_file(ffprobe),
            "ffprobe version 7.1-essentials_build-www.gyan.dev",
            True,
        )
        ffmpeg_info = media_info.ToolInfo(
            ffmpeg,
            media_info._sha256_file(ffmpeg),
            "ffmpeg version 7.1-essentials_build-www.gyan.dev",
            True,
        )
        rows = [
            {"n": n, "pts": n * 100, "duration": 100}
            for n in range(frame_count)
        ]
        evidence.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "production_frame_pts_certification",
                    "status": "PASS",
                    "authoritative_frame_timeline": True,
                    "reason_codes": [],
                    "scope": "full",
                    "source": {
                        "path": str(source.resolve()),
                        "sha256": source_sha256,
                        "size": source.stat().st_size,
                    },
                    "time_base": {
                        "numerator": 1,
                        "denominator": 1000,
                        "text": "1/1000",
                    },
                    "frame_pts_status": "cfr",
                    "frame_count": len(rows),
                    "tools": {
                        "ffprobe": ffprobe_info.as_dict(),
                        "ffmpeg": ffmpeg_info.as_dict(),
                    },
                    "pts_table_sha256": media_info._canonical_pts_table_sha256(rows),
                    "pts_table": rows,
                }
            ),
            encoding="utf-8",
        )
        video = media_info.VideoStreamInfo(
            index=0,
            codec_name="h264",
            codec_long_name="H.264",
            width=192,
            height=96,
            pixel_format="yuv420p",
            time_base=Fraction(1, 1000),
            start_time=Fraction(0, 1),
            duration=Fraction(frame_count, 25),
            avg_frame_rate=Fraction(25, 1),
            r_frame_rate=Fraction(25, 1),
            frame_count=frame_count,
            start_pts=0,
            duration_ts=frame_count * 40,
        )
        info = media_info.MediaInfo(
            source_path=source.resolve(),
            source_sha256=source_sha256,
            source_size=source.stat().st_size,
            source_mtime_ns=source.stat().st_mtime_ns,
            format_name="mp4",
            format_long_name="MPEG-4",
            duration=Fraction(frame_count, 25),
            start_time=Fraction(0, 1),
            video_streams=(video,),
            audio_streams=(),
            vfr_status="rate_match",
            ffprobe=ffprobe_info,
            ffmpeg=ffmpeg_info,
        )
        certification = media_info.FramePtsCertification.from_evidence(
            evidence,
            status="cfr",
            source_sha256=source_sha256,
            time_base=video.time_base,
        )
        return info.certify_frame_pts(certification)

    def test_production_export_requires_certified_media_before_creating_output(self) -> None:
        plan = TimelinePlan.from_deleted_ranges(4, [])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            output = root / "nested" / "output.mp4"
            source.write_bytes(b"source")
            request = ExportRequest.full(
                source,
                output,
                plan,
                fps=30,
                quality=8,
            )
            with mock.patch("analyzer.export_video") as export:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "requires a MediaInfo snapshot",
                ):
                    MediaExporter().export(request)
            export.assert_not_called()
            self.assertFalse(output.parent.exists())

    def test_production_ranges_share_the_same_certification_gate(self) -> None:
        plan = TimelinePlan.from_deleted_ranges(4, [])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            output = root / "nested" / "output.mp4"
            source.write_bytes(b"source")
            request = ExportRequest.ranges_export(
                source,
                output,
                plan,
                [(0, 2)],
                fps=30,
                quality=8,
            )
            with mock.patch("analyzer.export_ranges") as export:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "requires a MediaInfo snapshot",
                ):
                    MediaExporter().export(request)
            export.assert_not_called()
            self.assertFalse(output.parent.exists())

    def test_request_canonicalizes_ranges_and_is_immutable(self) -> None:
        plan = TimelinePlan.from_deleted_ranges(10, [(8, 9)])
        request = ExportRequest.ranges_export(
            "source.mp4",
            "output.mp4",
            plan,
            [(4, 7), (1, 3), (3, 5)],
            fps=30,
            quality=8,
            enforce_media_certification=False,
        )
        self.assertEqual(request.ranges, ((1, 7),))
        with self.assertRaises(AttributeError):
            request.fps = 60  # type: ignore[misc]

    def test_certified_full_export_passes_gate_and_binds_provenance(self) -> None:
        plan = TimelinePlan.from_deleted_ranges(4, [])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = self._certified_media(root, 4)
            output = root / "output.mp4"
            request = ExportRequest.full(
                media.source_path,
                output,
                plan,
                fps=30,
                quality=8,
                media_info=media,
                ffmpeg_path=str(media.ffmpeg.path),
                ffprobe_path=str(media.ffprobe.path),
            )
            def fake_pts_export(_source, staging, _intervals, **_kwargs):
                Path(staging).write_bytes(b"pts-output")
                return 4, 4, {
                    "audio_mode": "disabled",
                    "pts_table_consumed": True,
                    "pts_consumer": "ffmpeg_trim_pts_concat",
                }

            with mock.patch("analyzer.export_pts_schedule", side_effect=fake_pts_export):
                result = MediaExporter().export(request)
            self.assertEqual(result.metadata["media_authority"], "certified_source_pts")
            self.assertTrue(result.metadata["pts_table_loaded"])
            self.assertTrue(result.metadata["pts_table_consumed"])
            self.assertEqual(result.metadata["pts_consumer"], "ffmpeg_trim_pts_concat")
            self.assertEqual(
                result.metadata["pts_tick_intervals"][0]["pts_tick_range"],
                [0, 400],
            )
            self.assertEqual(result.metadata["certified_frame_count"], 4)

    def test_certified_export_binds_tools_when_request_is_auto(self) -> None:
        plan = TimelinePlan.from_deleted_ranges(4, [])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = self._certified_media(root, 4)
            output = root / "output.mp4"
            request = ExportRequest.full(
                media.source_path,
                output,
                plan,
                fps=30,
                quality=8,
                media_info=media,
            )

            def fake_pts_export(_source, staging, _intervals, **kwargs):
                self.assertEqual(kwargs["ffmpeg_path"], str(media.ffmpeg.path))
                Path(staging).write_bytes(b"pts-output")
                return 4, 4, {"pts_table_consumed": True}

            with mock.patch("analyzer.export_pts_schedule", side_effect=fake_pts_export):
                result = MediaExporter().export(request)
            self.assertEqual(result.metadata["ffmpeg_path"], str(media.ffmpeg.path))
            self.assertEqual(result.metadata["ffprobe_path"], str(media.ffprobe.path))

    def test_certified_export_builds_one_snapshot_for_full_and_ranges(self) -> None:
        plan = TimelinePlan.from_deleted_ranges(4, [])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = self._certified_media(root, 4)

            def fake_pts_export(_source, staging, intervals, **_kwargs):
                Path(staging).write_bytes(b"pts-output")
                self.assertEqual(intervals[0]["pts_tick_range"], [100, 300])
                return 2, 2, {"pts_table_consumed": True}

            ranged_request = ExportRequest.ranges_export(
                media.source_path,
                root / "ranged-output.mp4",
                plan,
                [(1, 3)],
                fps=30,
                quality=8,
                media_info=media,
            )
            with mock.patch.object(
                media_exporter.CertifiedPtsTimeline,
                "from_validated_media_info",
                wraps=media_exporter.CertifiedPtsTimeline.from_validated_media_info,
            ) as build_timeline:
                with mock.patch(
                    "analyzer.export_pts_schedule",
                    side_effect=fake_pts_export,
                ):
                    result = MediaExporter().export(ranged_request)
            self.assertEqual(result.written_frames, 2)
            self.assertEqual(build_timeline.call_count, 1)
            self.assertTrue(result.metadata["pts_table_consumed"])

    def test_certified_export_rejects_source_change_before_publish(self) -> None:
        plan = TimelinePlan.from_deleted_ranges(4, [])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = self._certified_media(root, 4)
            output = root / "output.mp4"
            output.write_bytes(b"old")
            request = ExportRequest.full(
                media.source_path,
                output,
                plan,
                fps=30,
                quality=8,
                media_info=media,
            )

            def fake_pts_export(_source, staging, _intervals, **_kwargs):
                Path(staging).write_bytes(b"new")
                media.source_path.write_bytes(b"changed-source")
                return 4, 4, {"pts_table_consumed": True}

            with mock.patch("analyzer.export_pts_schedule", side_effect=fake_pts_export):
                with self.assertRaisesRegex(
                    media_info.MediaInfoError,
                    "source media changed after export validation",
                ):
                    MediaExporter().export(request)
            self.assertEqual(output.read_bytes(), b"old")
            self.assertEqual(list(root.glob("*.partial*")), [])

    def test_certified_export_rejects_tool_change_before_publish(self) -> None:
        plan = TimelinePlan.from_deleted_ranges(4, [])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = self._certified_media(root, 4)
            output = root / "output.mp4"
            output.write_bytes(b"old")
            request = ExportRequest.full(
                media.source_path,
                output,
                plan,
                fps=30,
                quality=8,
                media_info=media,
            )

            def fake_pts_export(_source, staging, _intervals, **_kwargs):
                Path(staging).write_bytes(b"new")
                assert media.ffmpeg is not None
                media.ffmpeg.path.write_bytes(b"changed-tool")
                return 4, 4, {"pts_table_consumed": True}

            with mock.patch("analyzer.export_pts_schedule", side_effect=fake_pts_export):
                with self.assertRaisesRegex(
                    media_info.MediaInfoError,
                    "registered ffmpeg executable changed after export validation",
                ):
                    MediaExporter().export(request)
            self.assertEqual(output.read_bytes(), b"old")
            self.assertEqual(list(root.glob("*.partial*")), [])

    def test_full_export_wraps_existing_atomic_analyzer(self) -> None:
        plan = TimelinePlan.from_deleted_ranges(4, [])
        request = ExportRequest.full(
            "source.mp4",
            "output.mp4",
            plan,
            fps=30,
            quality=8,
            enforce_media_certification=False,
        )
        with mock.patch(
            "analyzer.export_video", return_value=(4, 4, {"audio_mode": "muxed"})
        ) as export:
            result = MediaExporter().export(request)
        self.assertIsInstance(result, ExportResult)
        self.assertEqual((result.written_frames, result.total_frames), (4, 4))
        self.assertEqual(result.metadata["audio_mode"], "muxed")
        self.assertEqual(
            export.call_args.args[:5],
            (str(Path("source.mp4").resolve()), str(Path("output.mp4").resolve()), plan, 30.0, 8),
        )

    def test_ranged_export_commits_staging_and_preserves_old_on_failure(self) -> None:
        plan = TimelinePlan.from_deleted_ranges(6, [])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            output = root / "output.mp4"
            source.write_bytes(b"source")
            output.write_bytes(b"old")
            request = ExportRequest.ranges_export(
                source,
                output,
                plan,
                [(1, 3)],
                fps=30,
                quality=8,
                enforce_media_certification=False,
            )

            def fake_export(_source, staging, *_args, **_kwargs):
                Path(staging).write_bytes(b"new")
                return 2, 2

            with mock.patch("analyzer.export_ranges", side_effect=fake_export):
                result = MediaExporter().export(request)
            self.assertEqual(output.read_bytes(), b"new")
            self.assertEqual(result.written_frames, 2)
            self.assertEqual(list(root.glob("*.partial*")), [])

            output.write_bytes(b"old-again")
            with mock.patch("analyzer.export_ranges", side_effect=RuntimeError("boom")):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    MediaExporter().export(request)
            self.assertEqual(output.read_bytes(), b"old-again")
            self.assertEqual(list(root.glob("*.partial*")), [])

    def test_ranged_export_cancel_after_write_removes_staging(self) -> None:
        plan = TimelinePlan.from_deleted_ranges(4, [])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = ExportRequest.ranges_export(
                root / "source.mp4",
                root / "output.mp4",
                plan,
                [(0, 2)],
                fps=30,
                quality=8,
                enforce_media_certification=False,
            )

            def fake_export(_source, staging, *_args, **_kwargs):
                Path(staging).write_bytes(b"new")
                return 2, 2

            def cancel():
                raise RuntimeError("cancelled")

            with mock.patch("analyzer.export_ranges", side_effect=fake_export):
                with self.assertRaisesRegex(RuntimeError, "cancelled"):
                    MediaExporter().export(request, cancel_cb=cancel)
            self.assertFalse(request.output_path.exists())
            self.assertEqual(list(root.glob("*.partial*")), [])


if __name__ == "__main__":
    unittest.main()
