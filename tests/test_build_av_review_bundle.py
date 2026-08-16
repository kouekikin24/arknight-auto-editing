from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "scripts" / "build_av_review_bundle.py"
SPEC = importlib.util.spec_from_file_location("build_av_review_bundle", MODULE_PATH)
assert SPEC and SPEC.loader
review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review)


class ReviewBundleUnitTests(unittest.TestCase):
    def test_candidate_groups_merge_nearby_events(self) -> None:
        candidates = [
            {"requested_video_position_seconds": 316.1},
            {"requested_video_position_seconds": 316.2},
            {"requested_video_position_seconds": 325.4},
        ]
        windows = review._window_groups(candidates, 8.0)
        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0]["candidate_indices"], [0, 1])
        self.assertEqual(windows[1]["candidate_indices"], [2])
        self.assertAlmostEqual(windows[0]["start_seconds"], 312.15)
        self.assertAlmostEqual(windows[1]["start_seconds"], 321.4)

    def test_signal_summary_never_promotes_sample_grid_to_anchor(self) -> None:
        summary = review._signal_summary(
            {"visual_change_events": [{"offset_seconds": 1.0, "score": 2.0}]},
            {"audio_events": [{"offset_seconds": 1.02, "peak": 100, "rms": 20.0}]},
        )
        self.assertEqual(summary["status"], review.REVIEW_STATUS)
        self.assertTrue(summary["requires_external_truth"])
        self.assertTrue(summary["human_observation_required"])
        self.assertNotIn("source_anchor", summary)

    def test_candidate_report_requires_no_media_time_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "source.mp4"
            media.write_bytes(b"source")
            report_path = root / "candidate.json"
            report = {
                "schema_version": 3,
                "kind": "mpv_phase0_av_event_candidate_scan",
                "status": "CANDIDATES_FOUND",
                "reason_codes": [],
                "scope": "bounded_source_seek_request",
                "time_basis": {
                    "requested_seek_seconds": 0.0,
                    "seek_basis": "requested_ffmpeg_output_seek_after_input",
                    "video_sample_grid": "requested_seek_seconds + frame_sample_index / requested_sample_fps",
                    "audio_sample_grid": "requested_seek_seconds + start_sample / decoded_sample_rate",
                    "candidate_match_delta": "requested_audio_position_seconds - requested_video_position_seconds",
                    "media_pts_authority": "none",
                    "can_register_source_anchor_directly": False,
                },
                "window": {
                    "requested_start_seconds": 0.0,
                    "requested_duration_seconds": 1.0,
                    "requested_end_seconds_exclusive": 1.0,
                    "max_allowed_seconds": 10.0,
                },
                "source": {
                    "path": str(media.resolve()),
                    "size": media.stat().st_size,
                    "sha256": review._sha256(media),
                },
                "source_oracle": {
                    "provided": False,
                    "file": None,
                    "binding_purpose": "none",
                    "provides_requested_time_mapping": False,
                    "time_authority": "none",
                },
                "video_sampling": {
                    "width": 320,
                    "height": 180,
                    "fps": 10.0,
                    "frame_count": 1,
                    "visual_change_scores": [0.0],
                },
                "audio_sampling": {
                    "status": "NO_AUDIO_STREAM",
                    "stream_present": False,
                    "decoded_samples_present": False,
                    "sample_rate": 48000,
                    "window_samples": 960,
                    "window_count": 0,
                    "clock_basis": "decoded_window_local_diagnostic_only",
                    "windows": [],
                    "decode": {"returncode": 1, "stderr": ""},
                },
                "candidates": [],
                "artifacts": {"contact_sheet": None, "contact_sheet_fps": None},
                "tools": {
                    "scanner": {"path": "scanner.py", "sha256": "0" * 64, "size": 1},
                    "ffmpeg": {"path": "ffmpeg.exe", "sha256": "1" * 64, "size": 1, "version_line": "test", "library_lines": []},
                    "commands": {"video": [], "audio": [], "contact_sheet": None},
                },
                "publication_identity_check": {"status": "PASSED", "checked": []},
            }
            report_path.write_text(json.dumps(report), encoding="utf-8")
            loaded, record = review._load_candidate_report(report_path)
            self.assertEqual(loaded["status"], "CANDIDATES_FOUND")
            self.assertEqual(record["sha256"], review._sha256(report_path))

    def test_manifest_promotion_contract_is_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "review_bundle.json"
            path.write_text(
                json.dumps(
                    {
                        "status": review.REVIEW_STATUS,
                        "scope": "review_only",
                        "promotion_contract": {
                            "production_consumer_allowed": False,
                            "gate_approval": False,
                            "source_observation_created": False,
                            "source_anchor_created": False,
                            "proxy_created": False,
                            "requires_external_truth": True,
                        },
                    }
                ),
                encoding="utf-8",
            )
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["status"], "REQUIRES_EXTERNAL_TRUTH")
            self.assertFalse(loaded["promotion_contract"]["production_consumer_allowed"])
            self.assertFalse(loaded["promotion_contract"]["gate_approval"])


if __name__ == "__main__":
    unittest.main()
