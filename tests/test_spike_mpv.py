from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "scripts" / "spike_mpv.py"
SPEC = importlib.util.spec_from_file_location("spike_mpv", MODULE_PATH)
assert SPEC and SPEC.loader
spike_mpv = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(spike_mpv)


class RangeTests(unittest.TestCase):
    def test_normalize_clamps_sorts_and_unions(self) -> None:
        actual = spike_mpv.normalize_ranges(
            [(8, 15), (-3, 2), (2, 4), (6, 9), (5, 5), (20, 30)],
            total=20,
        )
        self.assertEqual(actual, [(0, 4), (6, 15)])

    def test_complement_uses_half_open_intervals(self) -> None:
        deleted = [(0, 2), (4, 6), (9, 10)]
        self.assertEqual(
            spike_mpv.complement_ranges(deleted, total=10),
            [(2, 4), (6, 9)],
        )

    def test_frame_map_roundtrip_and_deleted_frames(self) -> None:
        mapping = spike_mpv.FrameMap([(2, 4), (6, 9)])
        self.assertEqual(mapping.virtual_total, 5)
        self.assertEqual(mapping.source_to_virtual(2), 0)
        self.assertEqual(mapping.source_to_virtual(3), 1)
        self.assertIsNone(mapping.source_to_virtual(4))
        self.assertEqual(mapping.source_to_virtual(6), 2)
        self.assertEqual(mapping.virtual_to_source(4), 8)
        with self.assertRaises(IndexError):
            mapping.virtual_to_source(5)

    def test_edl_escape_counts_utf8_bytes(self) -> None:
        value = "D:/视频/a,b%.mp4"
        escaped = spike_mpv._edl_escape(value)
        self.assertEqual(escaped, f"%{len(value.encode('utf-8'))}%{value}")


class EdlBuildTests(unittest.TestCase):
    def test_build_edl_writes_full_keep_timeline_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            video = temp / "视频,a%.mp4"
            video.write_bytes(b"not-a-real-video")
            skips = temp / "skip.json"
            skips.write_text(json.dumps([[2, 4], [6, 8]]), encoding="utf-8")
            meta = temp / "sample_meta.json"
            meta.write_text(
                json.dumps(
                    {
                        "video": str(video),
                        "stem": "sample",
                        "fps": 2.0,
                        "analyzed_frames": 10,
                        "paths": {"skip_segs": str(skips)},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            edl, report = spike_mpv.build_edl(meta, temp / "out")

            lines = edl.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], "# mpv EDL v0")
            self.assertEqual(len(lines), 4)
            self.assertIn(",0.000000000,1.000000000", lines[1])
            self.assertIn(",2.000000000,1.000000000", lines[2])
            self.assertIn(",4.000000000,1.000000000", lines[3])
            self.assertEqual(report["mapping"]["virtual_frames"], 6)
            self.assertEqual(report["mapping"]["keep_ranges"], 3)
            self.assertEqual(report["checks"]["frame_interval_mapping"], "pass")
            self.assertFalse(report["authoritative_pts_mapping"])


class ProxyPtsEdlBuildTests(unittest.TestCase):
    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _write_json(path: Path, value: dict) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _fixture(
        self,
        temp: Path,
        *,
        source_start: int = 10,
        count: int = 6,
        total_frames: int = 20,
        scope: str = "prefix",
    ) -> dict[str, Path | dict]:
        source = temp / "source.mp4"
        source.write_bytes(b"source-media")
        proxy = temp / "proxy.mp4"
        proxy.write_bytes(b"normalized-proxy")
        skips = temp / "skip.json"
        skips.write_text(
            json.dumps([[0, 10], [11, 13], [15, 20]]), encoding="utf-8"
        )
        meta = temp / "sample_meta.json"
        self._write_json(
            meta,
            {
                "video": str(source),
                "stem": "sample",
                # Deliberately wrong for the proxy tick table.  An accidental
                # frame/fps fallback would produce 0.5-second frame lengths.
                "fps": 2.0,
                "analyzed_frames": total_frames,
                "paths": {"skip_segs": str(skips)},
            },
        )

        duration_ticks = 7
        mapping = spike_mpv._proxy_frame_mapping(source_start, count)
        checksums = [f"{index + 1:08X}" for index in range(count)]
        source_oracle = temp / "source.oracle.json"
        self._write_json(
            source_oracle,
            {
                "schema_version": 1,
                "kind": "mpv_phase0_frame_pts_oracle",
                "status": "BLOCKED",
                "video": {
                    "path": str(source),
                    "sha256": self._sha256(source),
                },
                "pts_table": [
                    {
                        "n": index,
                        "pts": index * duration_ticks,
                        "duration": duration_ticks,
                        "checksum": checksums[index],
                    }
                    for index in range(count)
                ],
            },
        )
        manifest = temp / "proxy.manifest.json"
        manifest_value = {
            "schema_version": 1,
            "kind": "mpv_phase0_pts_normalized_proxy",
            "status": "BUILT_UNVERIFIED",
            "source": {
                "path": str(source),
                "sha256": self._sha256(source),
                "business_frame_start": source_start,
                "business_frame_count": count,
                "source_frame_domain": [source_start, source_start + count],
                "proxy_frame_domain": [0, count],
                "frame_mapping": mapping,
                "scope": scope,
                "oracle": {
                    "path": str(source_oracle),
                    "sha256": self._sha256(source_oracle),
                },
            },
            "proxy": {
                "path": str(proxy),
                "sha256": self._sha256(proxy),
            },
            "normalized_timeline": {
                "time_base": {"numerator": 1, "denominator": 100},
                "duration_ticks": duration_ticks,
                "business_pts_start": 0,
                "business_pts_end_exclusive": count * duration_ticks,
                "normalized_end_ticks": count * duration_ticks,
                "source_frame_start": source_start,
                "source_frame_end_exclusive": source_start + count,
                "source_frame_domain": [source_start, source_start + count],
                "proxy_frame_domain": [0, count],
                "frame_mapping": mapping,
                "frame_fps_fallback_used": False,
            },
            "terminal_guard": {
                "proxy_frame_index": count,
                "source_frame_index": source_start + count - 1,
                "pts_ticks": count * duration_ticks,
                "expected_checksum": checksums[-1],
                "business_frame_domain": [0, count],
                "source_frame_domain": [source_start, source_start + count],
                "proxy_frame_domain": [0, count],
                "included_in_business_domain": False,
            },
        }
        self._write_json(manifest, manifest_value)

        oracle = temp / "proxy.oracle.json"
        rows = [
            {
                "n": index,
                "pts": index * duration_ticks,
                "duration": duration_ticks,
                "checksum": checksums[index],
            }
            for index in range(count)
        ]
        rows.append(
            {
                "n": count,
                "pts": count * duration_ticks,
                "duration": 0,
                "checksum": checksums[-1],
            }
        )
        oracle_value = {
            "schema_version": 1,
            "kind": "mpv_phase0_frame_pts_oracle",
            "status": "BLOCKED",
            "authoritative_frame_timeline": False,
            "video": {
                "path": str(proxy),
                "sha256": self._sha256(proxy),
            },
            "ffmpeg": {"returncode": 0},
            "showinfo": {
                "parsed_frames": count + 1,
                "time_base": {"numerator": 1, "denominator": 100},
            },
            "pts_table": rows,
        }
        self._write_json(oracle, oracle_value)

        video_report = temp / "proxy.video.verify.json"
        report_value = {
            "schema_version": 1,
            "kind": "mpv_phase0_pts_normalized_proxy_verification",
            # A video-only schema cannot grant full G2 authority, even if a
            # fixture claims these two fields are PASS/true.
            "status": "PASS",
            "proxy_ready_for_gate": True,
            "reason_codes": [],
            "scope": scope,
            "source_frame_domain": [source_start, source_start + count],
            "proxy_frame_domain": [0, count],
            "frame_mapping": mapping,
            "manifest": {
                "path": str(manifest),
                "sha256": self._sha256(manifest),
            },
            "decoded_proxy_oracle": {
                "path": str(oracle),
                "sha256": self._sha256(oracle),
            },
            "binding": {"status": "PASS", "reason_codes": []},
            "video_validation": {
                "status": "PASS",
                "reason_codes": [],
                "business_frame_count": count,
                "business_frame_domain": [0, count],
                "decoded_frame_count": count + 1,
                "source_frame_domain": [source_start, source_start + count],
                "proxy_frame_domain": [0, count],
                "frame_mapping": mapping,
                "checksum_alignment": {
                    "status": "PASS",
                    "mismatch_count": 0,
                },
                "pts_alignment": {"status": "PASS", "mismatch_count": 0},
                "positive_business_durations": {
                    "status": "PASS",
                    "mismatch_count": 0,
                },
                "terminal_guard": {
                    "observed": True,
                    "included_in_business_domain": False,
                    "business_frame_domain": [0, count],
                    "checksum_matches": True,
                    "pts_matches": True,
                },
            },
        }
        self._write_json(video_report, report_value)
        return {
            "source": source,
            "source_oracle": source_oracle,
            "proxy": proxy,
            "meta": meta,
            "manifest": manifest,
            "manifest_value": manifest_value,
            "oracle": oracle,
            "oracle_value": oracle_value,
            "video_report": video_report,
            "video_report_value": report_value,
        }

    def _full_gate_report(self, temp: Path, fixture: dict) -> Path:
        source = fixture["source"]
        proxy = fixture["proxy"]
        manifest = fixture["manifest"]
        oracle = fixture["oracle"]
        source_oracle = fixture["source_oracle"]
        video_report = fixture["video_report"]
        video_evidence = {
            "status": "PASS",
            "reason_codes": [],
            "source_oracle": {
                "path": str(source_oracle),
                "sha256": self._sha256(source_oracle),
            },
            "decoded_proxy_oracle": {
                "path": str(oracle),
                "sha256": self._sha256(oracle),
            },
            "source_pixel_format": "yuv420p",
            "proxy_pixel_format": "yuv420p",
        }
        audio_preservation = {"status": "PASS", "reason_codes": []}
        av_sync = {
            "status": "PASS",
            "reason_codes": [],
            "content_anchors": {"status": "NOT_APPLICABLE_PASS"},
        }
        run = temp / "proxy.audio_av.run.json"
        self._write_json(
            run,
            {
                "schema_version": 2,
                "kind": "mpv_phase0_audio_av_run_manifest",
                "status": "PASS",
                "scope": "full",
                "video_proxy_manifest": {
                    "path": str(manifest),
                    "sha256": self._sha256(manifest),
                },
                "video_report": {
                    "path": str(video_report),
                    "sha256": self._sha256(video_report),
                },
                "source": {
                    "path": str(source),
                    "sha256": self._sha256(source),
                },
                "proxy": {
                    "path": str(proxy),
                    "sha256": self._sha256(proxy),
                },
                "video_evidence": video_evidence,
                "audio_timeline": {"status": "PASS", "reason_codes": []},
                "audio_preservation": audio_preservation,
                "av_sync": av_sync,
            },
        )
        gate_report = temp / "proxy.audio_av.verify.json"
        self._write_json(
            gate_report,
            {
                "schema_version": 2,
                "kind": "mpv_phase0_audio_av_report",
                "status": "PASS",
                "reason_codes": [],
                "proxy_ready_for_gate": True,
                "scope": "full",
                "video_validation": video_evidence,
                "audio_timeline": {"status": "PASS", "reason_codes": []},
                "audio_preservation": audio_preservation,
                "av_sync": av_sync,
                "run_manifest": {
                    "path": str(run),
                    "sha256": self._sha256(run),
                },
            },
        )
        return gate_report

    def test_nonzero_window_uses_proxy_pts_ticks_and_excludes_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            fixture = self._fixture(temp)

            edl, report = spike_mpv.build_proxy_edl(
                fixture["meta"],
                fixture["manifest"],
                fixture["oracle"],
                fixture["video_report"],
                temp / "out",
            )

            lines = edl.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], "# mpv EDL v0")
            self.assertEqual(len(lines), 3)
            self.assertIn(",0.000000000000,0.070000000000", lines[1])
            self.assertIn(",0.210000000000,0.140000000000", lines[2])
            self.assertEqual(
                report["mapping"]["segments"][0]["source_frame_range"],
                [10, 11],
            )
            self.assertEqual(
                report["mapping"]["segments"][1]["proxy_frame_range"],
                [3, 5],
            )
            self.assertEqual(
                report["mapping"]["segments"][1]["proxy_pts_range"],
                [21, 35],
            )
            self.assertFalse(report["mapping"]["terminal_guard_included"])
            self.assertTrue(report["pts_table_consumed"])
            self.assertFalse(report["frame_fps_fallback_used"])
            self.assertFalse(report["authoritative_pts_mapping"])
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(report["checks"]["mpv_boundary_playback"], "not_run")
            self.assertIn("PROXY_GATE_NOT_FULLY_VERIFIED", report["reason_codes"])

    def test_tampered_nonzero_mapping_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            fixture = self._fixture(temp)
            manifest_value = fixture["manifest_value"]
            self.assertIsInstance(manifest_value, dict)
            manifest_value["normalized_timeline"]["frame_mapping"] = (
                spike_mpv._proxy_frame_mapping(9, 6)
            )
            self._write_json(fixture["manifest"], manifest_value)
            report_value = fixture["video_report_value"]
            self.assertIsInstance(report_value, dict)
            report_value["manifest"]["sha256"] = self._sha256(fixture["manifest"])
            self._write_json(fixture["video_report"], report_value)

            with self.assertRaisesRegex(
                spike_mpv.ProxyEdlEvidenceError, "frame_mapping is invalid"
            ):
                spike_mpv.build_proxy_edl(
                    fixture["meta"],
                    fixture["manifest"],
                    fixture["oracle"],
                    fixture["video_report"],
                    temp / "out",
                )

    def test_only_fully_bound_audio_av_gate_can_grant_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            fixture = self._fixture(
                temp,
                source_start=0,
                count=20,
                total_frames=20,
                scope="full",
            )
            gate_report = self._full_gate_report(temp, fixture)

            _, report = spike_mpv.build_proxy_edl(
                fixture["meta"],
                fixture["manifest"],
                fixture["oracle"],
                fixture["video_report"],
                temp / "out",
                proxy_gate_report_path=gate_report,
            )

            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["reason_codes"], [])
            self.assertTrue(report["authoritative_pts_mapping"])
            self.assertEqual(report["checks"]["proxy_gate"], "pass")

    def test_non_positive_business_duration_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            fixture = self._fixture(temp)
            oracle_value = fixture["oracle_value"]
            self.assertIsInstance(oracle_value, dict)
            oracle_value["pts_table"][2]["duration"] = 0
            self._write_json(fixture["oracle"], oracle_value)
            report_value = fixture["video_report_value"]
            self.assertIsInstance(report_value, dict)
            report_value["decoded_proxy_oracle"]["sha256"] = self._sha256(
                fixture["oracle"]
            )
            self._write_json(fixture["video_report"], report_value)

            with self.assertRaisesRegex(
                spike_mpv.ProxyEdlEvidenceError, "duration must be >= 1"
            ):
                spike_mpv.build_proxy_edl(
                    fixture["meta"],
                    fixture["manifest"],
                    fixture["oracle"],
                    fixture["video_report"],
                    temp / "out",
                )

    def test_source_oracle_cannot_be_substituted_for_proxy_oracle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            fixture = self._fixture(temp)
            oracle_value = fixture["oracle_value"]
            self.assertIsInstance(oracle_value, dict)
            oracle_value["video"] = {
                "path": str(fixture["source"]),
                "sha256": self._sha256(fixture["source"]),
            }
            self._write_json(fixture["oracle"], oracle_value)
            report_value = fixture["video_report_value"]
            self.assertIsInstance(report_value, dict)
            report_value["decoded_proxy_oracle"]["sha256"] = self._sha256(
                fixture["oracle"]
            )
            self._write_json(fixture["video_report"], report_value)

            with self.assertRaisesRegex(
                spike_mpv.ProxyEdlEvidenceError, "binds another proxy"
            ):
                spike_mpv.build_proxy_edl(
                    fixture["meta"],
                    fixture["manifest"],
                    fixture["oracle"],
                    fixture["video_report"],
                    temp / "out",
                )


if __name__ == "__main__":
    unittest.main()
