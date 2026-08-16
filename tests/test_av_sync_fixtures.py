from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


generate = load_script(
    "generate_av_sync_fixtures_test",
    REPO / "scripts" / "generate_av_sync_fixtures.py",
)
verify = load_script(
    "verify_av_sync_fixtures_test",
    REPO / "scripts" / "verify_av_sync_fixtures.py",
)


class SyntheticAvFixtureTests(unittest.TestCase):
    def test_known_video_pts_and_audio_sample_clock_align(self) -> None:
        try:
            ffmpeg = generate.video_fixture.resolve_ffmpeg()
        except FileNotFoundError as exc:
            self.skipTest(str(exc))
        with tempfile.TemporaryDirectory() as temp_value:
            result = generate.generate_fixture(Path(temp_value), ffmpeg=ffmpeg)
            report = verify.verify_fixture(
                Path(result["video"]),
                Path(result["truth_manifest"]),
                ffmpeg,
            )
            self.assertEqual(report["status"], "PASS")
            self.assertEqual(report["reason_codes"], [])
            self.assertEqual(report["audio"]["pulse_ranges"], [[3840, 4320], [13440, 13920]])
            self.assertTrue(all(anchor["error_seconds"] <= 0.010 for anchor in report["anchors"]))

    def test_shifted_audio_truth_is_blocked_by_anchor_threshold(self) -> None:
        try:
            ffmpeg = generate.video_fixture.resolve_ffmpeg()
        except FileNotFoundError as exc:
            self.skipTest(str(exc))
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            result = generate.generate_fixture(temp, ffmpeg=ffmpeg)
            truth_path = Path(result["truth_manifest"])
            truth = json.loads(truth_path.read_text(encoding="utf-8"))
            truth["anchors"][0]["audio_sample"] += 960
            shifted = temp / "shifted.truth.json"
            shifted.write_text(json.dumps(truth), encoding="utf-8")
            report = verify.verify_fixture(Path(result["video"]), shifted, ffmpeg)
            self.assertEqual(report["status"], "BLOCKED")
            self.assertIn("AV_ANCHOR_ERROR_EXCEEDED", report["reason_codes"])

    def test_nonfinite_fixture_threshold_is_blocked(self) -> None:
        try:
            ffmpeg = generate.video_fixture.resolve_ffmpeg()
        except FileNotFoundError as exc:
            self.skipTest(str(exc))
        with tempfile.TemporaryDirectory() as temp_value:
            result = generate.generate_fixture(Path(temp_value), ffmpeg=ffmpeg)
            report = verify.verify_fixture(
                Path(result["video"]),
                Path(result["truth_manifest"]),
                ffmpeg,
                max_anchor_error_seconds=float("nan"),
            )
            self.assertEqual(report["status"], "BLOCKED")
            self.assertEqual(report["reason_codes"], ["THRESHOLD_INVALID"])
