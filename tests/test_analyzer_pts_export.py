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
            ):
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
            self.assertIn("select='between(pts,0,99)+between(pts,180,259)'", filter_text)
            self.assertIn("tpad=stop_mode=clone:stop=1", filter_text)
            self.assertNotIn("concat=n=2:v=1:a=0", filter_text)
            self.assertTrue(metadata["pts_terminal_guard"])


if __name__ == "__main__":
    unittest.main()
