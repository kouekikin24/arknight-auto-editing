from __future__ import annotations

import unittest
from unittest import mock
from pathlib import Path
import tempfile

import numpy as np

import analyzer
from timeline_plan import TimelinePlan


class ExportPreflightTests(unittest.TestCase):
    def test_export_video_rejects_source_as_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source.mp4"
            source.write_bytes(b"source")
            with self.assertRaisesRegex(ValueError, "source video"):
                analyzer.export_video(
                    str(source), str(source), np.zeros(1, dtype=bool), 30.0, 8
                )
            self.assertEqual(source.read_bytes(), b"source")

    def test_export_ranges_passes_canonical_half_open_ranges(self) -> None:
        with mock.patch.object(
            analyzer, "resolve_ffmpeg_path", return_value="ffmpeg"
        ), mock.patch.object(
            analyzer, "_export_ranges_with_ffmpeg_filters", return_value=True
        ) as fast_export:
            written, total = analyzer.export_ranges(
                "source.mp4",
                "output.mp4",
                [(3, 5), (1, 3), (4, 8)],
                fps=30.0,
                quality=8,
            )

        self.assertEqual((written, total), (7, 7))
        self.assertEqual(fast_export.call_args.args[2], [(1, 8)])

    def test_audio_risk_is_false_for_small_range_count(self) -> None:
        mask = np.zeros(100, dtype=bool)
        plan = analyzer.inspect_export_plan(mask, include_audio=True)
        self.assertEqual(plan["n_ranges"], 1)
        self.assertFalse(plan["audio_drop_requires_confirmation"])

    def test_audio_risk_is_true_above_filter_limit(self) -> None:
        mask = np.ones(161, dtype=bool)
        mask[::2] = False
        plan = analyzer.inspect_export_plan(mask, include_audio=True)
        self.assertEqual(plan["n_ranges"], 81)
        self.assertEqual(plan["audio_limit"], 80)
        self.assertTrue(plan["audio_drop_requires_confirmation"])

    def test_pts_certified_route_never_drops_audio_for_range_count(self) -> None:
        # The certified PTS exporter batches audio beyond
        # _MAX_PTS_SINGLE_GRAPH_AUDIO_RANGES, so segment count alone must not
        # trigger the legacy audio-drop confirmation (real cuts reach 13619
        # kept ranges on sample 4).
        mask = np.ones(161, dtype=bool)
        mask[::2] = False
        plan = analyzer.inspect_export_plan(
            mask, include_audio=True, pts_certified=True
        )
        self.assertEqual(plan["n_ranges"], 81)
        self.assertFalse(plan["audio_drop_requires_confirmation"])
        self.assertNotIn("too_many_ranges", plan["audio_drop_reasons"])

    @staticmethod
    def _overshoot_intervals(count: int) -> list[dict]:
        return [
            {
                "source_frame_range": (index * 2, index * 2 + 1),
                "pts_tick_range": (index * 20, index * 20 + 10),
            }
            for index in range(count)
        ]

    def test_vfr_schedule_beyond_ceiling_is_accepted_for_batching(self) -> None:
        # VFR schedules beyond _MAX_PTS_EXPORT_RANGES are no longer rejected:
        # they route to batched-seek export (the single-pass expression
        # overflows FFmpeg's evaluator stack at five-digit term counts).
        from fractions import Fraction

        intervals = self._overshoot_intervals(analyzer._MAX_PTS_EXPORT_RANGES + 1)
        with mock.patch.object(
            analyzer,
            "resolve_ffmpeg_path",
            side_effect=RuntimeError("sentinel: reached ffmpeg resolution"),
        ):
            with self.assertRaisesRegex(RuntimeError, "sentinel"):
                analyzer.export_pts_schedule(
                    "src.mp4",
                    "out.mp4",
                    intervals,
                    time_base=Fraction(1, 15360),
                    source_frame_count=2 * len(intervals),
                    reported_total_frames=2 * len(intervals),
                    quality=6,
                    frame_pts_status="vfr",
                )

    def test_cfr_schedule_cap_unchanged(self) -> None:
        # The CFR per-segment trim/concat branch keeps its hard ceiling:
        # graph cost grows superlinearly with segment count there.
        from fractions import Fraction

        intervals = self._overshoot_intervals(analyzer._MAX_PTS_EXPORT_RANGES + 1)
        with self.assertRaisesRegex(RuntimeError, "滤镜安全上限"):
            analyzer.export_pts_schedule(
                "src.mp4",
                "out.mp4",
                intervals,
                time_base=Fraction(1, 15360),
                source_frame_count=2 * len(intervals),
                reported_total_frames=2 * len(intervals),
                quality=6,
                frame_pts_status="cfr",
            )

    def test_disabling_audio_never_requires_confirmation(self) -> None:
        mask = np.ones(161, dtype=bool)
        mask[::2] = False
        plan = analyzer.inspect_export_plan(mask, include_audio=False)
        self.assertFalse(plan["audio_drop_requires_confirmation"])

    def test_source_without_audio_is_not_reported_as_dropped_audio(self) -> None:
        mask = np.zeros(10, dtype=bool)
        with mock.patch.object(
            analyzer, "resolve_ffmpeg_path", return_value="ffmpeg"
        ), mock.patch.object(
            analyzer,
            "_probe_audio_stream",
            return_value={
                "status": "pass",
                "present": False,
                "method": "ffmpeg_decode",
                "errors": [],
            },
        ):
            plan = analyzer.inspect_export_plan(
                mask, include_audio=True, video_path="source.mp4"
            )
        self.assertFalse(plan["audio_drop_requires_confirmation"])
        self.assertEqual(plan["audio_probe"]["present"], False)

    def test_audio_probe_reads_stream_metadata_via_pyav(self) -> None:
        fake_stream = mock.Mock()
        fake_container = mock.Mock()
        fake_container.streams.audio = [fake_stream]
        with mock.patch("av.open", return_value=fake_container):
            result = analyzer._probe_audio_stream("source.mp4")
        self.assertEqual(result["method"], "pyav")
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["present"])

    def test_audio_probe_reports_absent_audio_stream_via_pyav(self) -> None:
        fake_container = mock.Mock()
        fake_container.streams.audio = []
        with mock.patch("av.open", return_value=fake_container):
            result = analyzer._probe_audio_stream("source.mp4")
        self.assertEqual(result["method"], "pyav")
        self.assertEqual(result["status"], "pass")
        self.assertFalse(result["present"])

    def test_missing_ffmpeg_blocks_export_when_audio_is_requested(self) -> None:
        mask = np.zeros(10, dtype=bool)
        with mock.patch.object(
            analyzer, "resolve_ffmpeg_path", side_effect=FileNotFoundError
        ), mock.patch.object(
            analyzer,
            "_probe_audio_stream",
        ) as probe_audio:
            plan = analyzer.inspect_export_plan(
                mask, include_audio=True, video_path="source.mp4"
            )
        self.assertTrue(plan["export_blocked"])
        self.assertIn("ffmpeg_unavailable", plan["export_block_reasons"])
        self.assertFalse(plan["audio_drop_requires_confirmation"])
        probe_audio.assert_not_called()

    def test_probe_failure_is_not_treated_as_no_audio_stream(self) -> None:
        mask = np.zeros(10, dtype=bool)
        with mock.patch.object(
            analyzer, "resolve_ffmpeg_path", return_value="ffmpeg"
        ), mock.patch.object(
            analyzer,
            "_probe_audio_stream",
            return_value={
                "status": "error",
                "present": None,
                "method": "ffmpeg_decode",
                "errors": ["decode failed"],
            },
        ):
            plan = analyzer.inspect_export_plan(
                mask, include_audio=True, video_path="source.mp4"
            )
        self.assertTrue(plan["audio_drop_requires_confirmation"])
        self.assertIn("audio_probe_inconclusive", plan["audio_drop_reasons"])

    def test_export_rejects_stale_or_cross_source_preflight(self) -> None:
        plan = TimelinePlan.from_deleted_ranges(8, [(2, 4)])
        preflight = analyzer.inspect_export_plan(
            plan,
            include_audio=False,
            video_path="source.mp4",
        )
        cases = {
            "preflight timeline": {
                "timeline_fingerprint": "stale",
            },
            "preflight range count": {
                "n_ranges": preflight["n_ranges"] + 1,
            },
            "preflight audio policy": {
                "include_audio": True,
            },
            "preflight source path": {
                "video_path": preflight["video_path"] + ".other",
            },
        }

        for expected, changes in cases.items():
            with self.subTest(expected=expected):
                stale = dict(preflight)
                stale.update(changes)
                with self.assertRaisesRegex(ValueError, expected):
                    analyzer._export_video_impl(
                        "source.mp4",
                        "output.mp4",
                        plan,
                        fps=30.0,
                        quality=8,
                        include_audio=False,
                        preflight=stale,
                    )

    def test_export_rejects_unconfirmed_audio_drop_before_opening_media(self) -> None:
        mask = np.ones(161, dtype=bool)
        mask[::2] = False
        with self.assertRaisesRegex(RuntimeError, "无声视频"):
            analyzer.export_video(
                "missing-input.mp4",
                "missing-output.mp4",
                mask,
                fps=60.0,
                quality=8,
                include_audio=True,
            )

    def test_export_without_audio_requires_ffmpeg_before_opening_media(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "result.mp4"

            with (
                mock.patch.object(
                    analyzer, "resolve_ffmpeg_path", side_effect=FileNotFoundError
                ),
                mock.patch.object(analyzer.cv2, "VideoCapture") as video_capture,
            ):
                with self.assertRaisesRegex(RuntimeError, "导出需要 FFmpeg"):
                    analyzer.export_video(
                        "source.mp4",
                        str(output),
                        np.zeros(1, dtype=bool),
                        fps=30.0,
                        quality=8,
                        include_audio=False,
                    )

            video_capture.assert_not_called()
            self.assertFalse(output.exists())
            self.assertEqual(list(Path(tmpdir).glob(".*.partial.mp4*")), [])

    def test_export_video_success_atomically_replaces_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "result.mp4"
            output.write_bytes(b"old")
            staging_paths = []

            def impl(_video, staging, *_args, **_kwargs):
                staging_paths.append(Path(staging))
                Path(staging).write_bytes(b"new")
                return 3, 4, {"audio_mode": "disabled"}

            with mock.patch.object(analyzer, "_export_video_impl", side_effect=impl):
                result = analyzer.export_video(
                    "source.mp4", str(output), np.zeros(4, dtype=bool), 30.0, 8
                )

            self.assertEqual(result[:2], (3, 4))
            self.assertEqual(output.read_bytes(), b"new")
            self.assertEqual(len(staging_paths), 1)
            self.assertNotEqual(staging_paths[0], output)
            self.assertFalse(staging_paths[0].exists())

    def test_export_video_preserves_destination_container_suffix_in_staging(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "result.avi"
            staging_paths = []

            def impl(_video, staging, *_args, **_kwargs):
                staging_paths.append(Path(staging))
                Path(staging).write_bytes(b"new")
                return 1, 1, {}

            with mock.patch.object(analyzer, "_export_video_impl", side_effect=impl):
                analyzer.export_video(
                    "source.mp4", str(output), np.zeros(1, dtype=bool), 30.0, 8
                )

            self.assertTrue(staging_paths[0].name.endswith(".partial.avi"))
            self.assertEqual(output.read_bytes(), b"new")

    def test_export_video_failure_preserves_destination_and_cleans_staging(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "result.mp4"
            output.write_bytes(b"old")

            def impl(_video, staging, *_args, **_kwargs):
                Path(staging).write_bytes(b"partial")
                Path(staging + ".video-only.tmp.mp4").write_bytes(b"partial-video")
                raise RuntimeError("boom")

            with mock.patch.object(analyzer, "_export_video_impl", side_effect=impl):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    analyzer.export_video(
                        "source.mp4", str(output), np.zeros(4, dtype=bool), 30.0, 8
                    )

            self.assertEqual(output.read_bytes(), b"old")
            leftovers = [
                path for path in Path(tmpdir).iterdir()
                if "partial.mp4" in path.name
            ]
            self.assertEqual(leftovers, [])

    def test_cancel_before_atomic_commit_preserves_destination(self) -> None:
        class Cancelled(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "result.mp4"
            output.write_bytes(b"old")

            def impl(_video, staging, *_args, **_kwargs):
                Path(staging).write_bytes(b"new")
                return 1, 1, {}

            with mock.patch.object(analyzer, "_export_video_impl", side_effect=impl):
                with self.assertRaises(Cancelled):
                    analyzer.export_video(
                        "source.mp4",
                        str(output),
                        np.zeros(1, dtype=bool),
                        30.0,
                        8,
                        cancel_cb=lambda: (_ for _ in ()).throw(Cancelled()),
                    )

            self.assertEqual(output.read_bytes(), b"old")
            self.assertEqual(list(Path(tmpdir).glob(".*.partial.mp4*")), [])


if __name__ == "__main__":
    unittest.main()
