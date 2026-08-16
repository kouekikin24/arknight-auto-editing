from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "scripts" / "verify_proxy_audio_av.py"
SPEC = importlib.util.spec_from_file_location("verify_proxy_audio_av", MODULE_PATH)
assert SPEC and SPEC.loader
audio = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audio)


def audio_line(
    n: int,
    pts: int,
    *,
    pts_time: str | None = None,
    nb_samples: int = 1024,
    rate: int = 48000,
    channels: int = 2,
    layout: str = "stereo",
    checksum: str = "ABCDEF01",
) -> str:
    if pts_time is None:
        pts_time = f"{pts / rate:.7f}"
    return (
        "[Parsed_ashowinfo_0 @ abc] "
        f"n:{n} pts:{pts} pts_time:{pts_time} fmt:fltp "
        f"channels:{channels} chlayout:{layout} rate:{rate} "
        f"nb_samples:{nb_samples} checksum:{checksum}\n"
    )


def audio_report(*, start_pts: int = 0, checksums: list[str] | None = None) -> dict:
    checksums = checksums or ["AAAA0001", "BBBB0002"]
    rows = [
        {
            "n": index,
            "pts": start_pts + index * 2,
            "pts_time": (start_pts + index * 2) / 100,
            "nb_samples": 2,
            "rate": 100,
            "channels": 2,
            "chlayout": "stereo",
            "checksum": checksum,
        }
        for index, checksum in enumerate(checksums)
    ]
    return {
        "status": "PASS",
        "reason_codes": [],
        "stream_present": True,
        "format": {"sample_rate": 100, "channels": 2, "channel_layout": "stereo"},
        "frames": {
            "count": len(rows),
            "first": rows[0],
            "last": rows[-1],
            "pts_table": rows,
            "total_samples": len(rows) * 2,
            "start_pts": rows[0]["pts"],
            "end_pts_exclusive": rows[-1]["pts"] + 2,
            "start_time": rows[0]["pts"] / 100,
            "end_time": (rows[-1]["pts"] + 2) / 100,
        },
    }


class AudioAnalyzerTests(unittest.TestCase):
    def test_first_audio_pts_may_include_priming_but_samples_must_be_contiguous(self) -> None:
        analyzer = audio.AudioAnalyzer()
        analyzer.feed("Stream #0:1: Audio: aac (LC), 48000 Hz, stereo\n")
        analyzer.feed(audio_line(0, 480))
        analyzer.feed(audio_line(1, 1504, checksum="BBBB0002"))
        report = analyzer.finish(returncode=0)

        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["codec"], "aac")
        self.assertEqual(report["frames"]["start_pts"], 480)
        self.assertEqual(report["frames"]["end_pts_exclusive"], 2528)
        self.assertEqual(report["continuity"]["status"], "PASS")

    def test_audio_gap_or_overlap_blocks_timeline(self) -> None:
        analyzer = audio.AudioAnalyzer()
        analyzer.feed(audio_line(0, 0))
        analyzer.feed(audio_line(1, 1500))
        report = analyzer.finish(returncode=0)

        self.assertEqual(report["status"], "BLOCKED")
        self.assertIn("AUDIO_TIMELINE_NON_CONTIGUOUS", report["reason_codes"])
        self.assertEqual(report["continuity"]["error_count"], 1)

    def test_missing_audio_stream_is_explicit(self) -> None:
        analyzer = audio.AudioAnalyzer()
        analyzer.feed("Stream #0:0: Video: h264 (High), 60 fps\n")
        analyzer.feed("Stream map '0:a:0' matches no streams.\n")
        report = analyzer.finish(returncode=1)

        self.assertFalse(report["stream_present"])
        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual(report["reason_codes"], ["AUDIO_STREAM_MISSING"])

    def test_missing_pcm_checksum_blocks_audio_evidence(self) -> None:
        analyzer = audio.AudioAnalyzer()
        analyzer.feed(audio_line(0, 0).replace(" checksum:ABCDEF01", ""))
        report = analyzer.finish(returncode=0)

        self.assertEqual(report["status"], "BLOCKED")
        self.assertIn("AUDIO_CHECKSUM_MISSING", report["reason_codes"])


class AvSyncEvaluationTests(unittest.TestCase):
    def _manifest(self, temp: Path, pts: list[int]) -> dict:
        source = temp / "source.mp4"
        oracle_path = temp / "source.oracle.json"
        source.write_bytes(b"source")
        oracle = {
            "schema_version": audio.frame_oracle.SCHEMA_VERSION,
            "kind": "mpv_phase0_frame_pts_oracle",
            "ffmpeg": {"returncode": 0},
            "video": {
                "path": str(source),
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "size": source.stat().st_size,
            },
            "showinfo": {"pixel_formats": ["yuv420p"]},
            "pts_table": [
                {
                    "n": n,
                    "pts": value,
                    "duration": 2,
                    "checksum": f"{n + 1:08X}",
                }
                for n, value in enumerate(pts)
            ],
        }
        oracle_path.write_text(json.dumps(oracle), encoding="utf-8")
        return {
            "source": {
                "path": str(source),
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "oracle": {
                    "path": str(oracle_path),
                    "sha256": hashlib.sha256(oracle_path.read_bytes()).hexdigest(),
                },
            },
            "normalized_timeline": {
                "time_base": {"numerator": 1, "denominator": 100},
                "duration_ticks": 2,
                "business_pts_start": 0,
                "business_pts_end_exclusive": len(pts) * 2,
            },
        }

    def test_source_audio_with_video_only_proxy_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            manifest = self._manifest(Path(temp_value), [0, 2, 4])
            source_audio = audio_report()
            proxy_audio = {
                "status": "BLOCKED",
                "reason_codes": ["AUDIO_STREAM_MISSING"],
                "stream_present": False,
                "frames": {},
                "format": {},
            }
            result = audio.evaluate_av_sync(manifest, source_audio, proxy_audio)

            self.assertEqual(result["status"], "BLOCKED")
            self.assertIn("PROXY_AUDIO_MISSING", result["reason_codes"])

    def test_normalization_offset_is_diagnostic_not_content_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            manifest = self._manifest(Path(temp_value), [0, 4, 4])
            result = audio.evaluate_av_sync(
                manifest,
                audio_report(),
                audio_report(),
                max_identity_offset_span_seconds=0.01,
            )

            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["source_pts_identity"]["status"], "NOT_PRESERVED")
            self.assertEqual(result["source_pts_identity"]["gate_effect"], "diagnostic_only")
            self.assertIn(
                "SOURCE_PTS_IDENTITY_NOT_PRESERVED",
                result["source_pts_identity"]["reason_codes"],
            )
            self.assertAlmostEqual(result["normalization_offset"]["span_seconds"], 0.02)

    def test_audio_format_difference_blocks_identity_route(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            manifest = self._manifest(Path(temp_value), [0, 2])
            source_audio = audio_report()
            proxy_audio = audio_report()
            proxy_audio["format"]["sample_rate"] = 48000
            result = audio.evaluate_av_sync(manifest, source_audio, proxy_audio)

            self.assertEqual(result["status"], "BLOCKED")
            self.assertIn("AUDIO_FORMAT_MISMATCH", result["reason_codes"])

    def test_audio_checksum_difference_blocks_packet_copy_route(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            manifest = self._manifest(Path(temp_value), [0, 2])
            source_audio = audio_report(checksums=["AAAA0001", "BBBB0002"])
            proxy_audio = audio_report(checksums=["AAAA0001", "CCCC0003"])
            source_audio["frames"]["pts_table"] = [
                dict(source_audio["frames"]["first"]),
                dict(source_audio["frames"]["last"]),
            ]
            proxy_audio["frames"]["pts_table"] = [
                dict(proxy_audio["frames"]["first"]),
                dict(proxy_audio["frames"]["last"]),
            ]
            result = audio.evaluate_av_sync(manifest, source_audio, proxy_audio)

            self.assertEqual(result["status"], "BLOCKED")
            self.assertIn("AUDIO_PCM_CHECKSUM_MISMATCH", result["reason_codes"])

    def test_audio_sample_timeline_difference_blocks_preservation(self) -> None:
        source_audio = audio_report()
        proxy_audio = audio_report()
        source_audio["frames"]["pts_table"] = [
            dict(source_audio["frames"]["first"]),
            dict(source_audio["frames"]["last"]),
        ]
        proxy_audio["frames"]["pts_table"] = [
            dict(proxy_audio["frames"]["first"]),
            dict(proxy_audio["frames"]["last"]),
        ]
        proxy_audio["frames"]["pts_table"][1]["pts"] += 1

        result = audio.evaluate_audio_preservation(source_audio, proxy_audio)

        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("AUDIO_FRAME_TIMELINE_MISMATCH", result["reason_codes"])

    def test_no_audio_requires_explicit_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            manifest = self._manifest(Path(temp_value), [0, 2])
            no_audio = {"stream_present": False, "frames": {}}
            blocked = audio.evaluate_av_sync(manifest, no_audio, no_audio)
            allowed = audio.evaluate_av_sync(manifest, no_audio, no_audio, allow_no_audio=True)

            self.assertEqual(blocked["status"], "BLOCKED")
            self.assertIn("NO_AUDIO_POLICY_NOT_DECLARED", blocked["reason_codes"])
            self.assertEqual(allowed["status"], "NOT_APPLICABLE_PASS")

    def test_nonfinite_thresholds_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            manifest = self._manifest(Path(temp_value), [0, 2])
            result = audio.evaluate_av_sync(
                manifest,
                audio_report(),
                audio_report(),
                max_anchor_error_seconds=math.nan,
            )
            self.assertEqual(result["status"], "BLOCKED")
            self.assertIn("MAX_AV_ANCHOR_THRESHOLD_INVALID", result["reason_codes"])

            result = audio.evaluate_av_sync(
                manifest,
                audio_report(),
                audio_report(),
                max_identity_offset_span_seconds=math.inf,
            )
            self.assertEqual(result["status"], "BLOCKED")
            self.assertIn("MAX_IDENTITY_OFFSET_THRESHOLD_INVALID", result["reason_codes"])

    def test_content_anchor_threshold_cannot_exceed_registered_ten_ms(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            manifest = self._manifest(Path(temp_value), [0, 2])
            result = audio.evaluate_av_sync(
                manifest,
                audio_report(),
                audio_report(),
                max_anchor_error_seconds=0.010001,
            )

            self.assertEqual(result["status"], "BLOCKED")
            self.assertIn("MAX_AV_ANCHOR_THRESHOLD_INVALID", result["reason_codes"])

    def test_allow_no_audio_keeps_source_pts_identity_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            manifest = self._manifest(Path(temp_value), [0, 4, 4])
            no_audio = {"stream_present": False, "frames": {}}
            result = audio.evaluate_av_sync(
                manifest,
                no_audio,
                no_audio,
                allow_no_audio=True,
            )
            self.assertEqual(result["status"], "NOT_APPLICABLE_PASS")
            self.assertEqual(result["source_pts_identity"]["status"], "NOT_PRESERVED")
            self.assertNotIn(
                "SOURCE_PTS_IDENTITY_NOT_PRESERVED", result["reason_codes"]
            )
            self.assertIn(
                "SOURCE_PTS_IDENTITY_NOT_PRESERVED",
                result["source_pts_identity"]["reason_codes"],
            )


class ContentAnchorEvidenceTests(unittest.TestCase):
    def _rows(self, *, proxy_checksums: list[str] | None = None) -> tuple[list[dict], list[dict]]:
        checksums = proxy_checksums or ["A", "B", "C", "D"]
        source = [
            {"n": index, "pts": index * 100, "checksum": checksum}
            for index, checksum in enumerate(["A", "B", "C", "D"])
        ]
        proxy = [
            {"n": index, "pts": index * 100, "checksum": checksum}
            for index, checksum in enumerate(checksums)
        ]
        return source, proxy

    def _anchors(
        self,
        *,
        source_shift_samples: int = 0,
        proxy_shift_samples: int = 0,
    ) -> list[dict]:
        return [
            {
                "zone": zone,
                "evidence_id": f"sample3-{zone}",
                "source_frame_index": index,
                "proxy_frame_index": index,
                "source_audio_sample": index * 1000 + source_shift_samples,
                "proxy_audio_sample": index * 1000 + proxy_shift_samples,
            }
            for zone, index in zip(audio.REQUIRED_CONTENT_ANCHOR_ZONES, range(4))
        ]

    def test_missing_content_anchors_are_blocked(self) -> None:
        source, proxy = self._rows()
        result = audio.evaluate_content_anchor_evidence(
            None,
            source,
            proxy,
            business_frame_count=4,
            duration_ticks=100,
            time_base_numerator=1,
            time_base_denominator=100,
            audio_sample_rate=1000,
            source_audio_start_sample=0,
            source_audio_end_sample_exclusive=4000,
            proxy_audio_start_sample=0,
            proxy_audio_end_sample_exclusive=4000,
        )

        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("AV_CONTENT_ANCHORS_NOT_PROVIDED", result["reason_codes"])

    def test_four_clock_anchors_pass(self) -> None:
        source, proxy = self._rows()
        result = audio.evaluate_content_anchor_evidence(
            self._anchors(),
            source,
            proxy,
            business_frame_count=4,
            duration_ticks=100,
            time_base_numerator=1,
            time_base_denominator=100,
            audio_sample_rate=1000,
            source_audio_start_sample=0,
            source_audio_end_sample_exclusive=4000,
            proxy_audio_start_sample=0,
            proxy_audio_end_sample_exclusive=4000,
        )

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["observed_zones"], list(audio.REQUIRED_CONTENT_ANCHOR_ZONES))
        self.assertTrue(all(anchor["error_seconds"] == 0 for anchor in result["anchors"]))

    def test_shifted_anchor_is_blocked_without_relaxing_threshold(self) -> None:
        source, proxy = self._rows()
        result = audio.evaluate_content_anchor_evidence(
            self._anchors(proxy_shift_samples=20),
            source,
            proxy,
            business_frame_count=4,
            duration_ticks=100,
            time_base_numerator=1,
            time_base_denominator=100,
            audio_sample_rate=1000,
            source_audio_start_sample=0,
            source_audio_end_sample_exclusive=4000,
            proxy_audio_start_sample=0,
            proxy_audio_end_sample_exclusive=4000,
        )

        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("AV_CONTENT_ANCHOR_ERROR_EXCEEDED", result["reason_codes"])

    def test_matching_fixed_audio_priming_offset_passes(self) -> None:
        source, proxy = self._rows()
        result = audio.evaluate_content_anchor_evidence(
            self._anchors(source_shift_samples=10, proxy_shift_samples=10),
            source,
            proxy,
            business_frame_count=4,
            duration_ticks=100,
            time_base_numerator=1,
            time_base_denominator=100,
            audio_sample_rate=1000,
            source_audio_start_sample=0,
            source_audio_end_sample_exclusive=4000,
            proxy_audio_start_sample=0,
            proxy_audio_end_sample_exclusive=4000,
        )

        self.assertEqual(result["status"], "PASS")
        for anchor in result["anchors"]:
            self.assertAlmostEqual(anchor["source_offset_seconds"], 0.01)
        self.assertTrue(all(anchor["error_seconds"] == 0 for anchor in result["anchors"]))

    def test_source_and_proxy_audio_use_independent_sample_clocks(self) -> None:
        source, proxy = self._rows()
        anchors = self._anchors()
        for index, anchor in enumerate(anchors):
            anchor["source_audio_sample"] = index * 1000
            anchor["proxy_audio_sample"] = index * 2000
        result = audio.evaluate_content_anchor_evidence(
            anchors,
            source,
            proxy,
            business_frame_count=4,
            duration_ticks=100,
            time_base_numerator=1,
            time_base_denominator=100,
            audio_sample_rate=None,
            source_audio_start_sample=0,
            source_audio_end_sample_exclusive=4000,
            proxy_audio_start_sample=0,
            proxy_audio_end_sample_exclusive=8000,
            source_audio_sample_rate=1000,
            proxy_audio_sample_rate=2000,
        )

        self.assertEqual(result["status"], "PASS")
        self.assertTrue(all(anchor["error_seconds"] == 0 for anchor in result["anchors"]))

    def test_source_av_delay_preserved_by_proxy_passes(self) -> None:
        source, proxy = self._rows()
        result = audio.evaluate_content_anchor_evidence(
            self._anchors(source_shift_samples=30, proxy_shift_samples=30),
            source,
            proxy,
            business_frame_count=4,
            duration_ticks=100,
            time_base_numerator=1,
            time_base_denominator=100,
            audio_sample_rate=1000,
            source_audio_start_sample=0,
            source_audio_end_sample_exclusive=4000,
            proxy_audio_start_sample=0,
            proxy_audio_end_sample_exclusive=4000,
        )

        self.assertEqual(result["status"], "PASS")
        self.assertTrue(all(anchor["error_seconds"] == 0 for anchor in result["anchors"]))

        changed_source = audio.evaluate_content_anchor_evidence(
            self._anchors(source_shift_samples=10, proxy_shift_samples=30),
            source,
            proxy,
            business_frame_count=4,
            duration_ticks=100,
            time_base_numerator=1,
            time_base_denominator=100,
            audio_sample_rate=1000,
            source_audio_start_sample=0,
            source_audio_end_sample_exclusive=4000,
            proxy_audio_start_sample=0,
            proxy_audio_end_sample_exclusive=4000,
        )
        self.assertEqual(changed_source["status"], "BLOCKED")
        self.assertIn(
            "AV_CONTENT_ANCHOR_ERROR_EXCEEDED", changed_source["reason_codes"]
        )

    def test_frame_checksum_mismatch_is_blocked(self) -> None:
        source, proxy = self._rows(proxy_checksums=["A", "X", "C", "D"])
        result = audio.evaluate_content_anchor_evidence(
            self._anchors(),
            source,
            proxy,
            business_frame_count=4,
            duration_ticks=100,
            time_base_numerator=1,
            time_base_denominator=100,
            audio_sample_rate=1000,
            source_audio_start_sample=0,
            source_audio_end_sample_exclusive=4000,
            proxy_audio_start_sample=0,
            proxy_audio_end_sample_exclusive=4000,
        )

        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("AV_CONTENT_ANCHOR_FRAME_CHECKSUM_MISMATCH", result["reason_codes"])

    def test_incomplete_anchor_zones_are_blocked(self) -> None:
        source, proxy = self._rows()
        result = audio.evaluate_content_anchor_evidence(
            self._anchors()[:3],
            source,
            proxy,
            business_frame_count=4,
            duration_ticks=100,
            time_base_numerator=1,
            time_base_denominator=100,
            audio_sample_rate=1000,
            source_audio_start_sample=0,
            source_audio_end_sample_exclusive=4000,
            proxy_audio_start_sample=0,
            proxy_audio_end_sample_exclusive=4000,
        )

        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("AV_CONTENT_ANCHOR_ZONES_INCOMPLETE", result["reason_codes"])


class VideoEvidenceRecheckTests(unittest.TestCase):
    def _fixture(
        self,
        temp: Path,
        *,
        source_pixel_format: str = "yuv420p",
        proxy_pixel_format: str = "yuv420p",
    ) -> tuple[dict, dict, Path, Path, Path, Path]:
        source = temp / "source.mp4"
        proxy = temp / "proxy.mp4"
        source.write_bytes(b"source-media")
        proxy.write_bytes(b"proxy-media")
        source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        proxy_sha = hashlib.sha256(proxy.read_bytes()).hexdigest()
        source_rows = [
            {"n": 0, "pts": 0, "pts_time": 0.0, "duration": 2, "duration_time": 0.02, "checksum": "AAAA0001"},
            {"n": 1, "pts": 2, "pts_time": 0.02, "duration": 2, "duration_time": 0.02, "checksum": "BBBB0002"},
        ]
        proxy_rows = [
            dict(row) for row in source_rows
        ] + [
            {"n": 2, "pts": 4, "pts_time": 0.04, "duration": 0, "duration_time": 0.0, "checksum": "BBBB0002"}
        ]
        source_oracle = {
            "schema_version": audio.frame_oracle.SCHEMA_VERSION,
            "kind": "mpv_phase0_frame_pts_oracle",
            "ffmpeg": {"returncode": 0},
            "video": {"path": str(source), "sha256": source_sha, "size": source.stat().st_size},
            "showinfo": {"pixel_formats": [source_pixel_format]},
            "pts_table": source_rows,
        }
        proxy_oracle = {
            "schema_version": audio.frame_oracle.SCHEMA_VERSION,
            "kind": "mpv_phase0_frame_pts_oracle",
            "video": {"path": str(proxy), "sha256": proxy_sha, "size": proxy.stat().st_size},
            "showinfo": {
                "time_base": {"numerator": 1, "denominator": 100},
                "pixel_formats": [proxy_pixel_format],
            },
            "ffmpeg": {"returncode": 0},
            "reason_codes": ["FRAME_DURATION_NON_POSITIVE"],
            "examples": {"duration_problems": [{"n": 2, "field": "duration", "value": 0}]},
            "pts_table": proxy_rows,
        }
        source_oracle_path = temp / "source.oracle.json"
        proxy_oracle_path = temp / "proxy.oracle.json"
        source_oracle_path.write_text(json.dumps(source_oracle), encoding="utf-8")
        proxy_oracle_path.write_text(json.dumps(proxy_oracle), encoding="utf-8")
        manifest = {
            "schema_version": audio.proxy_verifier.SCHEMA_VERSION,
            "kind": audio.proxy_verifier.MANIFEST_KIND,
            "source": {
                "path": str(source),
                "sha256": source_sha,
                "size": source.stat().st_size,
                "scope": "full",
                "business_frame_count": 2,
                "pixel_format": source_pixel_format,
                "oracle": {"path": str(source_oracle_path), "sha256": hashlib.sha256(source_oracle_path.read_bytes()).hexdigest()},
            },
            "proxy": {
                "path": str(proxy),
                "sha256": proxy_sha,
                "size": proxy.stat().st_size,
            },
            "normalized_timeline": {
                "time_base": {"numerator": 1, "denominator": 100},
                "duration_ticks": 2,
                "business_pts_start": 0,
                "business_pts_end_exclusive": 4,
                "source_ordered_last_end_ticks": 4,
                "source_presentation_end_ticks": 4,
                "normalized_end_ticks": 4,
            },
            "terminal_guard": {
                "method": "clone_last_business_frame",
                "proxy_frame_index": 2,
                "pts_ticks": 4,
                "expected_checksum": "BBBB0002",
                "business_frame_domain": [0, 2],
                "included_in_business_domain": False,
            },
            "generation": {"encoder": {"pixel_format": "yuv420p"}},
        }
        claimed = audio.proxy_verifier.evaluate_video_proxy(manifest, source_rows, proxy_oracle)
        report = {
            "schema_version": audio.proxy_verifier.SCHEMA_VERSION,
            "kind": audio.proxy_verifier.REPORT_KIND,
            "manifest": {"path": str(temp / "manifest.json"), "sha256": ""},
            "scope": "full",
            "decoded_proxy_oracle": {"path": str(proxy_oracle_path), "sha256": hashlib.sha256(proxy_oracle_path.read_bytes()).hexdigest()},
            "video_validation": claimed,
        }
        manifest_path = temp / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        report["manifest"] = {"path": str(manifest_path), "sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest()}
        return manifest, report, source_oracle_path, proxy_oracle_path, source, proxy

    def test_forged_pass_report_is_rechecked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            manifest, report, source_oracle_path, proxy_oracle_path, _, _ = self._fixture(temp)
            report["video_validation"] = {"status": "PASS"}
            result = audio._validate_video_evidence(manifest, report)
            self.assertEqual(result["status"], "BLOCKED")
            self.assertIn("VIDEO_EVIDENCE_INVALID", result["reason_codes"])

    def test_checksum_evidence_tamper_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            manifest, report, source_oracle_path, _, _, _ = self._fixture(temp)
            source_oracle = json.loads(source_oracle_path.read_text(encoding="utf-8"))
            source_oracle["pts_table"][1]["checksum"] = "CCCC0003"
            source_oracle_path.write_text(json.dumps(source_oracle), encoding="utf-8")
            manifest["source"]["oracle"]["sha256"] = hashlib.sha256(source_oracle_path.read_bytes()).hexdigest()
            result = audio._validate_video_evidence(manifest, report)
            self.assertEqual(result["status"], "BLOCKED")
            self.assertTrue(any(code.startswith("VIDEO_RECHECK_") for code in result["reason_codes"]))

    def test_missing_guard_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            manifest, report, _, proxy_oracle_path, _, _ = self._fixture(temp)
            proxy_oracle = json.loads(proxy_oracle_path.read_text(encoding="utf-8"))
            proxy_oracle["pts_table"].pop()
            proxy_oracle_path.write_text(json.dumps(proxy_oracle), encoding="utf-8")
            report["decoded_proxy_oracle"]["sha256"] = hashlib.sha256(proxy_oracle_path.read_bytes()).hexdigest()
            result = audio._validate_video_evidence(manifest, report)
            self.assertEqual(result["status"], "BLOCKED")
            self.assertTrue(any(code.startswith("VIDEO_RECHECK_") for code in result["reason_codes"]))

    def test_pixel_format_conversion_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            manifest, report, _, _, _, _ = self._fixture(
                temp,
                source_pixel_format="yuv420p10le",
                proxy_pixel_format="yuv420p",
            )
            result = audio._validate_video_evidence(manifest, report)
            self.assertEqual(result["status"], "BLOCKED")
            self.assertIn("VIDEO_PIXEL_FORMAT_MISMATCH", result["reason_codes"])

    def test_validated_video_evidence_is_json_serializable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            manifest, report, _, _, _, _ = self._fixture(temp)

            result = audio._validate_video_evidence(manifest, report)

            self.assertEqual(result["status"], "PASS")
            json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
