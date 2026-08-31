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
            # B-frames enabled (libx264 default count); packet-order-free
            # post-check makes reordering safe.
            self.assertEqual(command[command.index("-bf") + 1], "3")
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

    def test_pts_bframe_args_are_encoder_dependent(self) -> None:
        # NVENC follows the OBS default (2); libx264 its own default (3).
        self.assertEqual(
            analyzer._pts_bframe_args(["-c:v", "h264_nvenc"]), ["-bf", "2"]
        )
        self.assertEqual(
            analyzer._pts_bframe_args(["-c:v", "libx264"]), ["-bf", "3"]
        )

    def test_container_postcheck_tolerates_bframe_packet_reorder(self) -> None:
        # With B-frames the packet order is the decode order, so packet-level
        # PTS is legitimately non-monotonic; order-free invariants must pass.
        schedule = [(0, 2, 0, 100), (4, 5, 180, 260)]  # clone=180, step=80
        problems = analyzer._evaluate_pts_export_packets(
            [0, 100, 60, 180], 260, schedule, time_base=Fraction(1, 1000)
        )
        self.assertEqual(problems, [])

    def test_container_postcheck_hard_failures(self) -> None:
        schedule = [(0, 2, 0, 100), (4, 5, 180, 260)]
        tb = Fraction(1, 1000)
        # Clone sentinel not at its analytic position
        self.assertTrue(
            analyzer._evaluate_pts_export_packets(
                [0, 100, 60, 179], 260, schedule, time_base=tb
            )
        )
        # Timeline not anchored at zero
        self.assertTrue(
            analyzer._evaluate_pts_export_packets(
                [5, 100, 60, 180], 260, schedule, time_base=tb
            )
        )
        # Garbage declared duration (phantom-packet landmine)
        self.assertTrue(
            analyzer._evaluate_pts_export_packets(
                [0, 100, 60, 180], 9999, schedule, time_base=tb
            )
        )
        # Gross packet loss beyond muxer-dedup tolerance
        self.assertTrue(
            analyzer._evaluate_pts_export_packets([0, 180], 260, schedule, time_base=tb)
            == []
        )
        self.assertTrue(
            analyzer._evaluate_pts_export_packets([0, 180, 180], 260, schedule, time_base=tb)
            == []
        )
        self.assertTrue(
            analyzer._evaluate_pts_export_packets([180], 260, schedule, time_base=tb)
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

    def test_vfr_beyond_expression_ceiling_routes_to_batched_video(self) -> None:
        intervals = self._big_intervals(analyzer._MAX_PTS_EXPORT_RANGES + 1)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            output = root / "output.mp4"
            source.write_bytes(b"source")
            (root / "ffmpeg.exe").write_bytes(b"ffmpeg")

            def fake_batched(_path, _sched, **kwargs):
                Path(kwargs["output_path"]).write_bytes(b"batched")

            with mock.patch.object(
                analyzer, "_export_pts_video_batched", side_effect=fake_batched
            ) as batched_mock, mock.patch.object(
                analyzer, "_export_pts_video_single_pass"
            ) as single_mock, mock.patch.object(
                analyzer, "_verify_pts_export_container"
            ):
                written, _total, metadata = analyzer.export_pts_schedule(
                    str(source),
                    str(output),
                    intervals,
                    time_base=Fraction(1, 1000),
                    source_frame_count=len(intervals),
                    reported_total_frames=len(intervals),
                    quality=8,
                    ffmpeg_path=str(root / "ffmpeg.exe"),
                    frame_pts_status="vfr",
                )

            batched_mock.assert_called_once()
            single_mock.assert_not_called()
            self.assertEqual(metadata["pts_video_consumer"], "batched_seek_concat")
            self.assertEqual(written, len(intervals))

    def test_vfr_within_ceiling_stays_single_pass(self) -> None:
        intervals = self._big_intervals(3)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            output = root / "output.mp4"
            source.write_bytes(b"source")
            (root / "ffmpeg.exe").write_bytes(b"ffmpeg")

            def fake_single(_path, _sched, **kwargs):
                Path(kwargs["output_path"]).write_bytes(b"single")

            with mock.patch.object(
                analyzer, "_export_pts_video_single_pass", side_effect=fake_single
            ) as single_mock, mock.patch.object(
                analyzer, "_export_pts_video_batched"
            ) as batched_mock, mock.patch.object(
                analyzer, "_verify_pts_export_container"
            ):
                _written, _total, metadata = analyzer.export_pts_schedule(
                    str(source),
                    str(output),
                    intervals,
                    time_base=Fraction(1, 1000),
                    source_frame_count=len(intervals),
                    reported_total_frames=len(intervals),
                    quality=8,
                    ffmpeg_path=str(root / "ffmpeg.exe"),
                    frame_pts_status="vfr",
                )

            single_mock.assert_called_once()
            batched_mock.assert_not_called()
            self.assertEqual(metadata["pts_video_consumer"], "single_pass")

    def test_batched_video_split_math(self) -> None:
        # 4500 one-frame segments, 256-tick frames with a 356-tick gap each
        intervals = tuple(
            {
                "source_frame_range": [index * 2, index * 2 + 1],
                "pts_tick_range": [index * 612, index * 612 + 256],
            }
            for index in range(4500)
        )
        schedule = analyzer._normalize_pts_schedule(
            intervals, time_base=Fraction(1, 1000)
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            captured: list = []

            def fake_run(command, *, timeout, cancel_cb):
                captured.append(list(command))
                Path(command[-1]).write_bytes(b"encoded")

            with mock.patch.object(
                analyzer, "_run_ffmpeg_interruptible", side_effect=fake_run
            ):
                analyzer._export_pts_video_batched(
                    "source.mp4",
                    schedule,
                    time_base=Fraction(1, 1000),
                    quality=8,
                    use_gpu=False,
                    gpu_encoder="",
                    ffmpeg="ffmpeg",
                    tmpdir=temporary,
                    output_path=str(root / "video.mp4"),
                    ffmpeg_timeout=1800.0,
                )

            # 4500 segments -> 3 batches (2000+2000+500) + 1 concat
            self.assertEqual(len(captured), 4)
            batch_cmds, concat_cmd = captured[:3], captured[3]
            self.assertEqual(concat_cmd[concat_cmd.index("-f") + 1], "concat")
            self.assertEqual(concat_cmd[concat_cmd.index("-c") + 1], "copy")
            for command in batch_cmds:
                self.assertEqual(
                    command[command.index("-fps_mode:v") + 1], "passthrough"
                )
                self.assertIn("-an", command)
            # Per-batch expressions stay bounded; clone guard only on the last.
            filters = [
                (root / f"pts-filter-{index:04d}.txt").read_text(encoding="utf-8")
                for index in range(3)
            ]
            self.assertNotIn("tpad", filters[0])
            self.assertNotIn("tpad", filters[1])
            self.assertIn("tpad=stop_mode=clone:stop=1", filters[2])
            self.assertEqual(filters[0].count("between(pts,"), 2000)
            self.assertEqual(filters[2].count("between(pts,"), 500)
            # -frames:v caps each batch; only the last gets the clone slot.
            self.assertEqual(batch_cmds[0][batch_cmds[0].index("-frames:v") + 1], "2000")
            self.assertEqual(batch_cmds[1][batch_cmds[1].index("-frames:v") + 1], "2000")
            self.assertEqual(batch_cmds[2][batch_cmds[2].index("-frames:v") + 1], "501")
            # -ss seeks to each batch's first segment start tick minus 1s.
            self.assertEqual(batch_cmds[0][batch_cmds[0].index("-ss") + 1], "0.000000000")
            self.assertEqual(batch_cmds[1][batch_cmds[1].index("-ss") + 1], "1223.000000000")
            self.assertEqual(batch_cmds[2][batch_cmds[2].index("-ss") + 1], "2447.000000000")
            # Only the last batch pins the terminal clone via setts.
            self.assertNotIn("-bsf:v", batch_cmds[0])
            self.assertNotIn("-bsf:v", batch_cmds[1])
            self.assertIn("-bsf:v", batch_cmds[2])
            # Concat list carries exact per-batch durations (span ticks / 1000).
            concat_list = (root / "video-concat.txt").read_text(encoding="utf-8")
            self.assertEqual(concat_list.count("duration 512.000000000"), 2)
            self.assertEqual(concat_list.count("vbatch-"), 3)


if __name__ == "__main__":
    unittest.main()
