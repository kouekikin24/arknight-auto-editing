from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from task_manager import TaskCancelled


REPO = Path(__file__).resolve().parents[1]


def load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


generate = load_script("generate_pts_oracle_fixtures", REPO / "scripts" / "generate_pts_oracle_fixtures.py")
verify = load_script("verify_mpv_frames_fixture_test", REPO / "scripts" / "verify_mpv_frames.py")


class FixtureEncodingTests(unittest.TestCase):
    def test_binary_frame_id_round_trip_and_complements(self) -> None:
        for frame_id in (0, 1, 1000, 32767, 65535):
            decoded, problems = verify.decode_truth_frame_id(
                generate.frame_bytes(frame_id),
                width=generate.WIDTH,
                height=generate.HEIGHT,
                encoding=generate.id_encoding(),
            )
            self.assertEqual(problems, [])
            self.assertEqual(decoded, frame_id)

    def test_corrupted_complement_is_rejected(self) -> None:
        frame = bytearray(generate.frame_bytes(1234))
        x = generate.BIT_X
        y = generate.COMPLEMENT_Y
        corrupt_value = generate.PIXEL_WHITE if ((1234 >> 0) & 1) else generate.PIXEL_BLACK
        for row in range(y, y + generate.BIT_HEIGHT):
            start = row * generate.WIDTH + x
            frame[start : start + generate.BIT_WIDTH] = bytes([corrupt_value]) * generate.BIT_WIDTH
        decoded, problems = verify.decode_truth_frame_id(
            bytes(frame),
            width=generate.WIDTH,
            height=generate.HEIGHT,
            encoding=generate.id_encoding(),
        )
        self.assertIsNone(decoded)
        self.assertTrue(any(value.startswith("complement[") for value in problems))


class FixtureEndToEndTests(unittest.TestCase):
    def test_probe_cancellation_does_not_wait_for_stderr_eof(self) -> None:
        try:
            ffmpeg = generate.resolve_ffmpeg()
        except FileNotFoundError as exc:
            self.skipTest(str(exc))

        with tempfile.TemporaryDirectory() as temp_value:
            result = generate.generate_fixture(
                Path(temp_value),
                name="cancel",
                pts_ticks=generate.CFR_PTS,
                id_start=1000,
                ffmpeg=ffmpeg,
                force=False,
            )
            calls = 0

            def cancel_check() -> None:
                nonlocal calls
                calls += 1
                if calls >= 2:
                    raise TaskCancelled("cancelled")

            with self.assertRaises(TaskCancelled):
                verify.probe_video(
                    Path(result["video"]),
                    verify.FfmpegExecutable(ffmpeg, "test"),
                    cancel_check=cancel_check,
                )

    def test_cfr_and_vfr_have_authoritative_id_pts_alignment(self) -> None:
        try:
            ffmpeg = generate.resolve_ffmpeg()
        except FileNotFoundError as exc:
            self.skipTest(str(exc))

        with tempfile.TemporaryDirectory() as temp_value:
            output_dir = Path(temp_value)
            cases = (
                ("cfr", generate.CFR_PTS, 1000),
                ("vfr", generate.VFR_PTS, 2000),
            )
            for name, pts_ticks, id_start in cases:
                result = generate.generate_fixture(
                    output_dir,
                    name=name,
                    pts_ticks=pts_ticks,
                    id_start=id_start,
                    ffmpeg=ffmpeg,
                    force=False,
                )
                video = Path(result["video"])
                truth_path = Path(result["truth_manifest"])
                report, exit_code = verify.probe_video(
                    video,
                    verify.FfmpegExecutable(ffmpeg, "test"),
                    truth_manifest=truth_path,
                )
                self.assertEqual(exit_code, verify.EXIT_PASS)
                self.assertEqual(report["status"], "PASS")
                self.assertTrue(report["authoritative_frame_timeline"])
                self.assertEqual(report["frame_id_alignment"]["reason_codes"], [])
                self.assertEqual(
                    report["frame_id_alignment"]["decoded_frame_ids"],
                    [id_start + index for index in range(len(pts_ticks))],
                )
                self.assertEqual(len(report["pts_table"]), len(pts_ticks))
                if name == "vfr":
                    durations = [row["duration"] for row in report["pts_table"]]
                    self.assertGreater(len(set(durations)), 1)
                    self.assertEqual(
                        [row["pts"] for row in report["pts_table"]],
                        pts_ticks,
                    )

    def test_truth_manifest_hash_mismatch_blocks_before_decode(self) -> None:
        try:
            ffmpeg = generate.resolve_ffmpeg()
        except FileNotFoundError as exc:
            self.skipTest(str(exc))

        with tempfile.TemporaryDirectory() as temp_value:
            output_dir = Path(temp_value)
            result = generate.generate_fixture(
                output_dir,
                name="cfr",
                pts_ticks=generate.CFR_PTS,
                id_start=1000,
                ffmpeg=ffmpeg,
                force=False,
            )
            truth_path = Path(result["truth_manifest"])
            truth = json.loads(truth_path.read_text(encoding="utf-8"))
            truth["video"]["sha256"] = "0" * 64
            truth_path.write_text(json.dumps(truth), encoding="utf-8")
            report, exit_code = verify.probe_video(
                Path(result["video"]),
                verify.FfmpegExecutable(ffmpeg, "test"),
                truth_manifest=truth_path,
            )
            self.assertEqual(exit_code, verify.EXIT_BLOCKED)
            self.assertEqual(report["status"], "BLOCKED")
            self.assertEqual(report["reason_codes"], ["TRUTH_MANIFEST_INVALID"])


if __name__ == "__main__":
    unittest.main()
