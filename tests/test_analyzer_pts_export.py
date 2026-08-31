from fractions import Fraction
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import analyzer


class AnalyzerPtsExportTests(unittest.TestCase):
    def test_fraction_filter_seconds_preserves_integer_and_signed_boundaries(self) -> None:
        cases = (
            (Fraction(0), "0"),
            (Fraction(1), "1"),
            (Fraction(10), "10"),
            (Fraction(20), "20"),
            (Fraction(100), "100"),
            (Fraction(-10), "-10"),
            (Fraction(10, 3), "3.333333333333333333333333333333333333333"),
            (Fraction(1, 10), "0.1"),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(analyzer._fraction_filter_seconds(value), expected)

    def test_consumer_builds_tick_trim_filter_and_vfr_passthrough(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            ffmpeg = root / "ffmpeg.exe"
            output = root / "output.mp4"
            source.write_bytes(b"source")
            ffmpeg.write_bytes(b"ffmpeg")
            captured: dict[str, object] = {}

            def fake_run(command, *, timeout, cancel_cb):
                captured["command"] = list(command)
                filter_path = Path(command[command.index("-filter_complex_script") + 1])
                captured["filter"] = filter_path.read_text(encoding="utf-8")
                output_path = Path(command[-1])
                output_path.write_bytes(b"encoded")

            intervals = (
                {
                    "source_frame_range": [0, 2],
                    "pts_tick_range": [-20, 30],
                },
                {
                    "source_frame_range": [3, 4],
                    "pts_tick_range": [80, 100],
                },
            )
            with mock.patch.object(analyzer, "_run_ffmpeg_interruptible", side_effect=fake_run):
                written, total, metadata = analyzer.export_pts_schedule(
                    str(source),
                    str(output),
                    intervals,
                    time_base=Fraction(1, 1000),
                    source_frame_count=4,
                    reported_total_frames=4,
                    quality=8,
                    ffmpeg_path=str(ffmpeg),
                )

            self.assertEqual((written, total), (3, 4))
            self.assertTrue(output.is_file())
            self.assertTrue(metadata["pts_table_consumed"])
            command = captured["command"]
            assert isinstance(command, list)
            self.assertIn("-copyts", command)
            self.assertEqual(command[command.index("-fps_mode:v") + 1], "passthrough")
            filter_text = captured["filter"]
            assert isinstance(filter_text, str)
            self.assertIn("trim=start_pts=-20:end_pts=30", filter_text)
            self.assertIn("trim=start_pts=80:end_pts=100", filter_text)
            self.assertNotIn("start_frame", filter_text)
            self.assertNotIn("/30", filter_text)

    def test_consumer_rejects_overlapping_or_float_tick_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            ffmpeg = root / "ffmpeg.exe"
            source.write_bytes(b"source")
            ffmpeg.write_bytes(b"ffmpeg")
            with self.assertRaises(ValueError):
                analyzer.export_pts_schedule(
                    str(source),
                    str(root / "out.mp4"),
                    ({
                        "source_frame_range": [0, 1],
                        "pts_tick_range": [0.0, 10],
                    },),
                    time_base=Fraction(1, 1000),
                    source_frame_count=1,
                    reported_total_frames=1,
                    quality=8,
                    ffmpeg_path=str(ffmpeg),
                )

    def test_certified_cfr_selects_cfr_muxing_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            ffmpeg = root / "ffmpeg.exe"
            output = root / "output.mp4"
            source.write_bytes(b"source")
            ffmpeg.write_bytes(b"ffmpeg")
            captured: dict[str, object] = {}

            def fake_run(command, *, timeout, cancel_cb):
                captured["command"] = list(command)
                Path(command[-1]).write_bytes(b"encoded")

            with mock.patch.object(
                analyzer, "_run_ffmpeg_interruptible", side_effect=fake_run
            ):
                _written, _total, metadata = analyzer.export_pts_schedule(
                    str(source),
                    str(output),
                    ({
                        "source_frame_range": [0, 2],
                        "pts_tick_range": [0, 80],
                    },),
                    time_base=Fraction(1, 1000),
                    source_frame_count=2,
                    reported_total_frames=2,
                    quality=8,
                    ffmpeg_path=str(ffmpeg),
                    frame_pts_status="cfr",
                )

            command = captured["command"]
            assert isinstance(command, list)
            self.assertEqual(command[command.index("-fps_mode:v") + 1], "cfr")
            self.assertEqual(metadata["pts_output_fps_mode"], "cfr")

    def test_certified_vfr_preserves_ticks_and_adds_terminal_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            ffmpeg = root / "ffmpeg.exe"
            output = root / "output.mp4"
            source.write_bytes(b"source")
            ffmpeg.write_bytes(b"ffmpeg")
            captured: dict[str, object] = {}

            def fake_run(command, *, timeout, cancel_cb):
                captured["command"] = list(command)
                filter_path = Path(command[command.index("-filter_complex_script") + 1])
                captured["filter"] = filter_path.read_text(encoding="utf-8")
                Path(command[-1]).write_bytes(b"encoded")

            intervals = (
                {
                    "source_frame_range": [0, 2],
                    "pts_tick_range": [0, 100],
                },
                {
                    "source_frame_range": [4, 5],
                    "pts_tick_range": [180, 260],
                },
            )
            with mock.patch.object(
                analyzer, "_run_ffmpeg_interruptible", side_effect=fake_run
            ), mock.patch.object(
                analyzer, "_verify_pts_export_container"
            ) as verify_mock:
                written, _total, metadata = analyzer.export_pts_schedule(
                    str(source),
                    str(output),
                    intervals,
                    time_base=Fraction(1, 1000),
                    source_frame_count=5,
                    reported_total_frames=5,
                    quality=8,
                    ffmpeg_path=str(ffmpeg),
                    frame_pts_status="vfr",
                )

            self.assertEqual(written, 3)
            command = captured["command"]
            assert isinstance(command, list)
            self.assertEqual(command[command.index("-fps_mode:v") + 1], "passthrough")
            self.assertIn("-bf", command)
            self.assertEqual(command[command.index("-frames:v") + 1], "4")
            filter_text = captured["filter"]
            assert isinstance(filter_text, str)
            # One flat select (OR of between ranges) plus a flat gap-sum
            # setpts: both depth-1 formulations scale to thousands of ranges,
            # unlike per-range trim chains (O(ranges x frames)) or nested
            # piecewise if-chains (parser depth limit ~100).  The terminal
            # clone guard keeps the last real frame's muxed duration positive.
            self.assertIn(
                "select='between(pts,0,99)+between(pts,180,259)'", filter_text
            )
            self.assertIn(
                "setpts='PTS-0-if(gte(PTS,180),80,0)'", filter_text
            )
            self.assertIn("tpad=stop_mode=clone:stop=1", filter_text)
            self.assertTrue(metadata["pts_terminal_guard"])
            # The terminal clone is pinned to its analytic position so a
            # pathological source duration cannot become a phantom packet.
            self.assertEqual(
                command[command.index("-bsf:v") + 1],
                "setts=pts='if(gt(PTS,179),180,PTS)':duration='if(gt(PTS,179),80,DURATION)'",
            )
            verify_mock.assert_called_once()
            self.assertEqual(
                metadata["pts_consumer"], "ffmpeg_select_pts_setpts_flat"
            )

    def test_sentinel_clone_position_accounts_for_removed_gaps(self) -> None:
        schedule = [(0, 2, 0, 100), (4, 5, 180, 260)]
        scheduled, clone_pts, step = analyzer._pts_sentinel_clone_position(
            schedule
        )
        self.assertEqual(scheduled, 3)
        # 260 - first(0) - gap(180-100=80)
        self.assertEqual(clone_pts, 180)
        self.assertEqual(step, 80)

    def test_sentinel_clone_position_single_segment(self) -> None:
        scheduled, clone_pts, step = analyzer._pts_sentinel_clone_position(
            [(10, 16, 1000, 2536)]
        )
        self.assertEqual(scheduled, 6)
        self.assertEqual(clone_pts, 1536)
        self.assertEqual(step, 256)

    def test_sentinel_fix_uses_position_threshold_not_packet_index(self) -> None:
        args = analyzer._pts_sentinel_fix_args([(0, 2, 0, 100), (4, 5, 180, 260)])
        self.assertEqual(args[0], "-bsf:v")
        self.assertEqual(
            args[1],
            "setts=pts='if(gt(PTS,179),180,PTS)'"
            ":duration='if(gt(PTS,179),80,DURATION)'",
        )

    @staticmethod
    def _fake_run(captured: list) -> object:
        def run(command, *, timeout, cancel_cb):
            captured.append(list(command))
            Path(command[-1]).write_bytes(b"encoded")
        return run

    def _big_intervals(self, count: int) -> tuple:
        return tuple(
            {
                "source_frame_range": [index, index + 1],
                "pts_tick_range": [index * 256, index * 256 + 256],
            }
            for index in range(count)
        )

    def test_large_schedule_with_audio_leaves_shared_graph(self) -> None:
        intervals = self._big_intervals(401)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            output = root / "output.mp4"
            source.write_bytes(b"source")
            (root / "ffmpeg.exe").write_bytes(b"ffmpeg")
            captured: list = []
            with mock.patch.object(
                analyzer, "_run_ffmpeg_interruptible",
                side_effect=self._fake_run(captured),
            ), mock.patch.object(analyzer, "_verify_pts_export_container"):
                _w, _t, metadata = analyzer.export_pts_schedule(
                    str(source),
                    str(output),
                    intervals,
                    time_base=Fraction(1, 1000),
                    source_frame_count=401,
                    reported_total_frames=401,
                    quality=8,
                    ffmpeg_path=str(root / "ffmpeg.exe"),
                    include_audio=True,
                    source_has_audio=True,
                    frame_pts_status="vfr",
                )
            # 视频趟（无音轨）+ 5 个音频批 + 1 次混流
            self.assertEqual(metadata["pts_audio_consumer"], "batched_pcm")
            self.assertEqual(metadata["audio_mode"], "muxed")
            self.assertNotIn("[outa]", captured[0])
            self.assertIn("-an", captured[0])
            batch_runs = [
                c for c in captured if "audio-batch" in str(c[-1])
            ]
            self.assertEqual(len(batch_runs), 5)
            for batch_cmd in batch_runs:
                self.assertIn("pcm_s16le", batch_cmd)
            mux_cmd = captured[-1]
            self.assertEqual(mux_cmd[mux_cmd.index("-c:v") + 1], "copy")
            self.assertEqual(mux_cmd[mux_cmd.index("-c:a") + 1], "aac")

    def test_audio_batch_failure_degrades_to_silent_video(self) -> None:
        intervals = self._big_intervals(401)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            output = root / "output.mp4"
            source.write_bytes(b"source")
            (root / "ffmpeg.exe").write_bytes(b"ffmpeg")
            captured: list = []

            def flaky(command, *, timeout, cancel_cb):
                captured.append(list(command))
                if "audio-batch" in str(command[-1]):
                    raise RuntimeError("boom")
                Path(command[-1]).write_bytes(b"encoded")

            with mock.patch.object(
                analyzer, "_run_ffmpeg_interruptible", side_effect=flaky
            ), mock.patch.object(analyzer, "_verify_pts_export_container"):
                _w, _t, metadata = analyzer.export_pts_schedule(
                    str(source),
                    str(output),
                    intervals,
                    time_base=Fraction(1, 1000),
                    source_frame_count=401,
                    reported_total_frames=401,
                    quality=8,
                    ffmpeg_path=str(root / "ffmpeg.exe"),
                    include_audio=True,
                    source_has_audio=True,
                    frame_pts_status="vfr",
                )
            self.assertEqual(metadata["audio_mode"], "failed_video_only")
            self.assertTrue(output.exists())

    def test_small_schedule_with_audio_stays_single_graph(self) -> None:
        intervals = self._big_intervals(3)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            output = root / "output.mp4"
            source.write_bytes(b"source")
            (root / "ffmpeg.exe").write_bytes(b"ffmpeg")
            captured: list = []
            with mock.patch.object(
                analyzer, "_run_ffmpeg_interruptible",
                side_effect=self._fake_run(captured),
            ), mock.patch.object(analyzer, "_verify_pts_export_container"):
                _w, _t, metadata = analyzer.export_pts_schedule(
                    str(source),
                    str(output),
                    intervals,
                    time_base=Fraction(1, 1000),
                    source_frame_count=3,
                    reported_total_frames=3,
                    quality=8,
                    ffmpeg_path=str(root / "ffmpeg.exe"),
                    include_audio=True,
                    source_has_audio=True,
                    frame_pts_status="vfr",
                )
            self.assertEqual(metadata["pts_audio_consumer"], "single_graph")
            self.assertIn("[outa]", captured[0])
            self.assertEqual(len(captured), 1)

    def test_audio_batching_split_math(self) -> None:
        schedule = analyzer._normalize_pts_schedule(
            self._big_intervals(250), time_base=Fraction(1, 1000)
        )
        with tempfile.TemporaryDirectory() as temporary:
            captured: list = []
            with mock.patch.object(
                analyzer, "_run_ffmpeg_interruptible",
                side_effect=self._fake_run(captured),
            ):
                list_path = analyzer._export_audio_pts_batched(
                    "source.mp4",
                    schedule,
                    time_base=Fraction(1, 1000),
                    ffmpeg="ffmpeg",
                    tmpdir=temporary,
                )
            # 250 段 → 3 批（100+100+50）
            self.assertEqual(len(captured), 3)
            content = Path(list_path).read_text(encoding="utf-8")
            # 每批 100 段 × 0.256s = 25.6s；末批 50 段不写 duration
            self.assertEqual(content.count("duration 25.6"), 2)
            self.assertEqual(content.count(".nut"), 3)


if __name__ == "__main__":
    unittest.main()
