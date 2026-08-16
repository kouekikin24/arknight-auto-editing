from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "scripts" / "verify_mpv_frames.py"
SPEC = importlib.util.spec_from_file_location("verify_mpv_frames", MODULE_PATH)
assert SPEC and SPEC.loader
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


def frame_line(
    n: int,
    pts: str,
    pts_time: str,
    *,
    duration: str = "2",
    duration_time: str = "0.02",
    checksum: str = "ABCDEF01",
    frame_type: str = "P",
) -> str:
    return (
        "[Parsed_showinfo_0 @ abc] "
        f"n: {n} pts: {pts} pts_time:{pts_time} "
        f"duration:{duration} duration_time:{duration_time} fmt:yuv420p "
        f"iskey:0 type:{frame_type} checksum:{checksum}\n"
    )


class ShowinfoAnalyzerTests(unittest.TestCase):
    def test_clean_timeline_is_authoritative_without_fps_inference(self) -> None:
        analyzer = verify.ShowinfoAnalyzer()
        analyzer.feed("  Duration: 00:00:00.06, start: 0.000000, bitrate: 1 kb/s\n")
        analyzer.feed(
            "[Parsed_showinfo_0 @ abc] config in time_base: 1/100, frame_rate: 50/1\n"
        )
        analyzer.feed(frame_line(0, "0", "0"))
        analyzer.feed(frame_line(1, "2", "0.02"))
        analyzer.feed(frame_line(2, "4", "0.04"))

        report = analyzer.finish(ffmpeg_returncode=0)

        self.assertEqual(report["status"], "PASS")
        self.assertTrue(report["authoritative_frame_timeline"])
        self.assertEqual(report["reason_codes"], [])
        self.assertEqual(report["showinfo"]["parsed_frames"], 3)
        self.assertTrue(report["showinfo"]["pts"]["strictly_increasing"])
        self.assertEqual(report["showinfo"]["time_base"]["text"], "1/100")
        self.assertAlmostEqual(report["duration"]["format_seconds"], 0.06)
        self.assertAlmostEqual(
            report["duration"]["decoded_presentation_extent_seconds"], 0.06
        )

    def test_anomalies_block_authority_and_are_reported_separately(self) -> None:
        analyzer = verify.ShowinfoAnalyzer()
        analyzer.feed(
            "[Parsed_showinfo_0 @ abc] config in time_base: 1/100, frame_rate: 50/1\n"
        )
        analyzer.feed(frame_line(0, "0", "0"))
        analyzer.feed(frame_line(1, "2", "0.02"))
        analyzer.feed(frame_line(2, "2", "0.02"))
        analyzer.feed(frame_line(4, "1", "0.01"))
        analyzer.feed(frame_line(5, "NOPTS", "N/A"))

        report = analyzer.finish(ffmpeg_returncode=0)

        self.assertEqual(report["status"], "BLOCKED")
        self.assertFalse(report["authoritative_frame_timeline"])
        self.assertIn("FRAME_INDEX_MISSING_OR_OUT_OF_ORDER", report["reason_codes"])
        self.assertIn("PTS_MISSING", report["reason_codes"])
        self.assertIn("PTS_TIME_MISSING", report["reason_codes"])
        self.assertIn("PTS_DUPLICATE", report["reason_codes"])
        self.assertIn("PTS_NON_MONOTONIC", report["reason_codes"])
        self.assertEqual(report["showinfo"]["pts"]["duplicate_count"], 1)
        self.assertEqual(report["showinfo"]["pts"]["non_monotonic_count"], 1)
        self.assertEqual(report["showinfo"]["pts"]["missing_count"], 1)

    def test_missing_or_invalid_duration_blocks_without_fps_fallback(self) -> None:
        analyzer = verify.ShowinfoAnalyzer()
        analyzer.feed("[Parsed_showinfo_0 @ abc] config in time_base: 1/10\n")
        analyzer.feed(
            frame_line(0, "0", "0", duration="N/A", duration_time="N/A")
        )

        report = analyzer.finish(ffmpeg_returncode=0)

        self.assertEqual(report["status"], "BLOCKED")
        self.assertIn("FRAME_DURATION_MISSING", report["reason_codes"])
        self.assertIsNone(report["duration"]["decoded_presentation_extent_seconds"])

    def test_ffmpeg_failure_is_error_not_timestamp_block(self) -> None:
        analyzer = verify.ShowinfoAnalyzer()
        report = analyzer.finish(ffmpeg_returncode=7)

        self.assertEqual(report["status"], "ERROR")
        self.assertIn("FFMPEG_DECODE_FAILED", report["reason_codes"])

    def test_partial_scan_is_always_blocked(self) -> None:
        analyzer = verify.ShowinfoAnalyzer()
        analyzer.feed("[Parsed_showinfo_0 @ abc] config in time_base: 1/100\n")
        analyzer.feed(frame_line(0, "0", "0"))

        report = analyzer.finish(ffmpeg_returncode=0, partial_scan=True)

        self.assertEqual(report["status"], "BLOCKED")
        self.assertFalse(report["authoritative_frame_timeline"])
        self.assertIn("PARTIAL_SCAN", report["reason_codes"])
        self.assertEqual(report["pts_conflict_assessment"]["scan_scope"], "partial")

    def test_duplicate_pts_with_distinct_pixels_forbids_sort_or_deduplicate(self) -> None:
        analyzer = verify.ShowinfoAnalyzer()
        analyzer.feed("[Parsed_showinfo_0 @ abc] config in time_base: 1/100\n")
        analyzer.feed(frame_line(0, "0", "0", checksum="AAAA0001"))
        analyzer.feed(frame_line(1, "0", "0", checksum="BBBB0002"))

        report = analyzer.finish(ffmpeg_returncode=0)

        self.assertEqual(report["showinfo"]["pts"]["duplicate_distinct_checksum_count"], 1)
        self.assertEqual(report["examples"]["duplicate_pts"][0]["checksum_relation"], "distinct")
        self.assertFalse(
            report["pts_conflict_assessment"]["automatic_sort_or_deduplicate_allowed"]
        )


class OutputAndCliTests(unittest.TestCase):
    def test_atomic_json_replaces_destination_and_leaves_no_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            output = Path(temp_value) / "nested" / "report.json"
            verify.write_json_atomic(output, {"old": True})
            verify.write_json_atomic(output, {"status": "BLOCKED", "value": "中文"})

            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")),
                {"status": "BLOCKED", "value": "中文"},
            )
            self.assertEqual(list(output.parent.glob("*.tmp")), [])

    def test_main_writes_blocked_report_and_returns_documented_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            video = temp / "sample.mp4"
            video.write_bytes(b"sample")
            output = temp / "report.json"
            ffmpeg = verify.FfmpegExecutable(temp / "ffmpeg.exe", "test")
            report = {
                "status": "BLOCKED",
                "authoritative_frame_timeline": False,
                "reason_codes": ["PTS_DUPLICATE"],
                "showinfo": {"parsed_frames": 2},
            }

            with mock.patch.object(verify, "resolve_ffmpeg", return_value=ffmpeg), mock.patch.object(
                verify, "probe_video", return_value=(report, verify.EXIT_BLOCKED)
            ):
                exit_code = verify.main(
                    [str(video), "--output", str(output), "--example-limit", "3"]
                )

            self.assertEqual(exit_code, verify.EXIT_BLOCKED)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), report)

    def test_command_preserves_input_timestamps_and_uses_passthrough(self) -> None:
        command = verify.build_ffmpeg_command(
            Path("ffmpeg"), Path("video with spaces.mp4")
        )
        self.assertIn("-copyts", command)
        self.assertEqual(command[command.index("-fps_mode") + 1], "passthrough")
        self.assertEqual(command[command.index("-vf") + 1], "showinfo")
        self.assertNotIn("-r", command)

    def test_command_can_record_a_non_default_decode_thread_count(self) -> None:
        command = verify.build_ffmpeg_command(
            Path("ffmpeg"), Path("video.mp4"), threads=4
        )
        self.assertEqual(command[command.index("-threads") + 1], "4")
        with self.assertRaises(ValueError):
            verify.build_ffmpeg_command(Path("ffmpeg"), Path("video.mp4"), threads=0)

    def test_prefix_diagnostic_command_records_frame_limit(self) -> None:
        command = verify.build_ffmpeg_command(
            Path("ffmpeg"), Path("video.mp4"), max_frames=20
        )
        self.assertEqual(command[command.index("-frames:v") + 1], "20")
        with self.assertRaises(ValueError):
            verify.build_ffmpeg_command(
                Path("ffmpeg"), Path("video.mp4"), max_frames=0
            )


if __name__ == "__main__":
    unittest.main()
