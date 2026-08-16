from __future__ import annotations

import importlib.util
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "scripts" / "verify_proxy_audio_av.py"
SPEC = importlib.util.spec_from_file_location("verify_proxy_audio_av_bundle", MODULE_PATH)
assert SPEC and SPEC.loader
audio = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audio)

ADAPTER_PATH = REPO / "scripts" / "adapt_legacy_full_oracle.py"
ADAPTER_SPEC = importlib.util.spec_from_file_location(
    "adapt_legacy_for_bundle", ADAPTER_PATH
)
assert ADAPTER_SPEC and ADAPTER_SPEC.loader
adapter = importlib.util.module_from_spec(ADAPTER_SPEC)
ADAPTER_SPEC.loader.exec_module(adapter)


class SplitWindowEvidenceBundleTests(unittest.TestCase):
    def _write(self, path: Path, value: object) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _fixture(self, root: Path) -> dict[str, Path]:
        source = root / "source.mp4"
        source.write_bytes(b"source-media")
        rows = [
            {"n": 0, "pts": 0, "duration": 1, "checksum": "A"},
            {"n": 1, "pts": 1, "duration": 1, "checksum": "B"},
            {"n": 2, "pts": 1, "duration": 1, "checksum": "C"},
            {"n": 3, "pts": 3, "duration": 1, "checksum": "D"},
        ]
        oracle = root / "source.oracle.json"
        self._write(
            oracle,
            {
                "schema_version": audio.frame_oracle.SCHEMA_VERSION,
                "kind": "mpv_phase0_frame_pts_oracle",
                "video": audio._file_record(source, include_size=True),
                "ffmpeg": {"returncode": 0},
                "authoritative_frame_timeline": False,
                "pts_conflict_assessment": {
                    "scan_scope": "complete",
                    "automatic_sort_or_deduplicate_allowed": False,
                },
                "pts_table": rows,
            },
        )
        conflict = root / "conflict.json"
        self._write(
            conflict,
            {
                "schema_version": audio.EVIDENCE_BUNDLE_SCHEMA_VERSION,
                "kind": audio.PTS_CONFLICT_EVIDENCE_KIND,
                "status": "BLOCKED",
                "evidence_role": "decoded_picture_order_and_pts_conflict_only",
                "authoritative_frame_timeline": False,
                "time_authority": "none",
                "production_consumer_allowed": False,
                "gate_approval": False,
                "source_provenance": {
                    "kind": "frame_oracle",
                    "time_authority": "none",
                },
                "exit_code": audio.EXIT_BLOCKED,
                "bindings": {
                    "source_media": audio._file_record(source, include_size=True),
                    "source_oracle": audio._file_record(oracle),
                },
                "scan_scope": "complete",
                "source_frame_domain": [0, 4],
                "conflict_indices": [1, 2],
                "frames": [
                    {"source_frame_index": 1, "pts": 1, "checksum": "B"},
                    {"source_frame_index": 2, "pts": 1, "checksum": "C"},
                ],
                "tools": {"creator": audio._file_record(audio.Path(__file__).parents[1] / "scripts" / "verify_proxy_audio_av.py")},
            },
        )
        observation = root / "event.observation.json"
        self._write(
            observation,
            {
                "schema_version": audio.SOURCE_CONTENT_ANCHOR_SCHEMA_VERSION,
                "kind": audio.SOURCE_CONTENT_ANCHOR_OBSERVATION_KIND,
                "scope": "prefix",
                "source_frame_domain": [0, 4],
                "observer": audio._file_record(audio.CONTENT_ANCHOR_CAPTURE_TOOL),
                "method": "manual source-only event observation",
                "audio_clock": {
                    "source_stream": "0:a:0",
                    "sample_index_basis": "decoded_ashowinfo_pts",
                    "source_sample_rate": 48000,
                },
                "anchors": [
                    {
                        "evidence_id": "event-1",
                        "source_frame_index": 1,
                        "source_frame_checksum": "B",
                        "source_audio_sample": 48000,
                        "event": {
                            "method": "manual independent observation",
                            "observed": True,
                            "description": "visible flash and audible transient",
                            "video": {
                                "observed": True,
                                "description": "visible flash",
                            },
                            "audio": {
                                "observed": True,
                                "description": "audible transient",
                            },
                        },
                    }
                ],
            },
        )
        event = root / "event.json"
        self._write(
            event,
            {
                "schema_version": audio.EVIDENCE_BUNDLE_SCHEMA_VERSION,
                "kind": audio.AV_EVENT_EVIDENCE_KIND,
                "status": "OBSERVED",
                "bindings": {
                    "source_media": audio._file_record(source, include_size=True),
                    "source_oracle": audio._file_record(oracle),
                    "observation": audio._file_record(observation),
                },
                "window": {
                    "requested_start_seconds": 316.0,
                    "requested_duration_seconds": 10.0,
                },
                "event": {
                    "observed": True,
                    "requires_human_observation": False,
                    "description": "manual visible flash and audible transient",
                    "video": {"observed": True, "description": "visible flash"},
                    "audio": {"observed": True, "description": "audible transient"},
                },
            },
        )
        return {
            "source": source,
            "oracle": oracle,
            "conflict": conflict,
            "observation": observation,
            "event": event,
            "bundle": root / "bundle.json",
        }

    def _reconciliation_fixture(self, root: Path) -> dict[str, Path]:
        source = root / "source.mp4"
        source.write_bytes(b"source-media")
        source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        pts = [0, 256, 512, 768, 1280, 768, 1024, 1280]
        legacy_rows = [
            {
                "n": index,
                "pts": value,
                "duration": 256,
                "pts_time": value / 15360,
                "duration_time": 1 / 60,
            }
            for index, value in enumerate(pts)
        ]
        decoded_rows = [
            {
                **row,
                "checksum": f"{0xA0 + index:08X}",
                "frame_type": "I" if index == 0 else "P",
                "is_keyframe": 1 if index == 0 else 0,
            }
            for index, row in enumerate(legacy_rows)
        ]

        def showinfo(rows, *, checksums):
            pts_summary = {
                "present_count": 8,
                "missing_count": 0,
                "duplicate_count": 2,
                "non_monotonic_count": 1,
                "unique_count": 6,
                "strictly_increasing": False,
            }
            if checksums:
                pts_summary.update(
                    duplicate_distinct_checksum_count=2,
                    duplicate_same_checksum_count=0,
                    duplicate_unknown_checksum_count=0,
                )
            return {
                "parsed_frames": 8,
                "frame_lines": 8,
                "malformed_frame_lines": 0,
                "frames_before_time_base": 0,
                "observed_time_bases": ["1/15360"],
                "time_base": {
                    "numerator": 1,
                    "denominator": 15360,
                    "text": "1/15360",
                },
                "frame_index": {
                    "first": 0,
                    "last": 7,
                    "unique_count": 8,
                    "discontinuity_count": 0,
                    "duplicate_count": 0,
                    "contiguous_from_zero": True,
                },
                "pts": pts_summary,
                "pts_time": {
                    "present_count": 8,
                    "missing_count": 0,
                    "non_monotonic_count": 1,
                    "time_base_mismatch_count": 0,
                },
                "first_frame": rows[0],
                "last_frame": rows[-1],
            }

        duration = {
            "frame_duration_present_count": 8,
            "frame_duration_missing_count": 0,
            "frame_duration_time_present_count": 8,
            "frame_duration_time_missing_count": 0,
            "non_positive_count": 0,
            "time_base_mismatch_count": 0,
        }
        legacy = root / "legacy.json"
        self._write(
            legacy,
            {
                "schema_version": 1,
                "kind": adapter.LEGACY_ORACLE_KIND,
                "status": "BLOCKED",
                "authoritative_frame_timeline": False,
                "reason_codes": ["PTS_DUPLICATE", "PTS_NON_MONOTONIC"],
                "video": {
                    "path": str(source),
                    "size": source.stat().st_size,
                    "mtime_ns": source.stat().st_mtime_ns,
                },
                "ffmpeg": {"returncode": 0},
                "showinfo": showinfo(legacy_rows, checksums=False),
                "pts_table": legacy_rows,
                "duration": duration,
            },
        )
        source_decode = root / "source_decode.json"
        command = [
            str(root / "ffmpeg.exe"),
            "-i",
            str(source),
            "-vf",
            "showinfo@source",
            "-frames:v",
            "9",
            str(root / "proxy.tmp.mp4"),
        ]
        self._write(
            source_decode,
            {
                "schema_version": 1,
                "kind": adapter.SOURCE_DECODE_KIND,
                "status": "BLOCKED",
                "authoritative_frame_timeline": False,
                "reason_codes": ["PTS_DUPLICATE", "PTS_NON_MONOTONIC"],
                "decode_diagnostic_error_count": 0,
                "video": {
                    "path": str(source),
                    "sha256": source_sha,
                    "size": source.stat().st_size,
                    "mtime_ns": source.stat().st_mtime_ns,
                },
                "ffmpeg": {"path": command[0], "command": command, "returncode": 0},
                "showinfo": showinfo(decoded_rows, checksums=True),
                "pts_table": decoded_rows,
                "reference_oracle": {
                    "path": str(legacy),
                    "sha256": adapter._load_snapshot(legacy, "legacy").sha256,
                    "reference_frame_count": 8,
                    "business_frame_count": 8,
                    "scope": "full",
                },
                "pts_conflict_assessment": {
                    "scan_scope": "complete",
                    "automatic_repair_safe": False,
                    "automatic_sort_or_deduplicate_allowed": False,
                    "status": "conflict",
                    "first_conflict_frame_n": 5,
                    "last_conflict_frame_n": 7,
                },
                "duration": duration,
            },
        )
        generation = root / "generation.json"
        self._write(
            generation,
            {
                "schema_version": 1,
                "kind": adapter.GENERATION_MANIFEST_KIND,
                "status": "BUILT_UNVERIFIED",
                "source": {
                    "path": str(source),
                    "sha256": source_sha,
                    "size": source.stat().st_size,
                    "mtime_ns": source.stat().st_mtime_ns,
                    "scope": "full",
                    "business_frame_start": 0,
                    "business_frame_count": 8,
                    "identity_unchanged_during_generation": True,
                    "first_checksum": decoded_rows[0]["checksum"],
                    "last_checksum": decoded_rows[-1]["checksum"],
                    "oracle": {
                        "path": str(legacy),
                        "sha256": adapter._load_snapshot(legacy, "legacy").sha256,
                        "scope": "full",
                        "parsed_frames": 8,
                        "reason_codes": ["PTS_DUPLICATE", "PTS_NON_MONOTONIC"],
                    },
                    "generation_source_decode": {
                        "path": str(source_decode),
                        "sha256": adapter._load_snapshot(
                            source_decode, "source decode"
                        ).sha256,
                        "observed_frames": 8,
                        "business_checksum_frames": 8,
                    },
                },
                "generation": {
                    "executed_command": command,
                    "ffmpeg": {"path": command[0]},
                },
            },
        )
        reconciliation = root / "reconciliation.json"
        adapter.adapt_legacy_full_oracle(
            legacy, source_decode, generation, reconciliation
        )
        return {
            "source": source,
            "legacy": legacy,
            "source_decode": source_decode,
            "generation": generation,
            "reconciliation": reconciliation,
            "conflict": root / "conflict.json",
        }

    def _create(self, fixture: dict[str, Path]) -> dict:
        return audio.create_split_window_evidence_bundle(
            fixture["source"],
            fixture["oracle"],
            fixture["conflict"],
            fixture["event"],
            fixture["bundle"],
        )

    def _evaluate(self, fixture: dict[str, Path]) -> dict:
        return audio.evaluate_split_window_evidence_bundle(
            fixture["bundle"],
            source_media_path=fixture["source"],
            av_event_source_oracle_path=fixture["oracle"],
        )

    def test_separate_windows_pass_as_one_bound_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            manifest = self._create(fixture)
            result = self._evaluate(fixture)

            self.assertFalse(manifest["separation"]["same_window_required"])
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["time_authority"], "none")
            self.assertFalse(result["production_consumer_allowed"])
            self.assertFalse(result["gate_approval"])
            self.assertFalse(result["ready_for_source_anchor"])
            self.assertEqual(result["conflict_evidence"]["conflict_indices"], [1, 2])
            self.assertEqual(
                result["av_event_evidence"]["window"]["requested_start_seconds"],
                316.0,
            )

    def test_bundle_publish_rechecks_each_bound_input(self) -> None:
        targets = ("source", "oracle", "conflict", "event")
        for target in targets:
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temp_value:
                fixture = self._fixture(Path(temp_value))

                def fake_write_json(
                    path: Path,
                    value: object,
                    *,
                    validate_temporary=None,
                    before_publish=None,
                ) -> None:
                    self.assertIsNotNone(validate_temporary)
                    self.assertIsNotNone(before_publish)
                    target_path = fixture[target]
                    target_path.write_bytes(target_path.read_bytes() + b"tamper")
                    before_publish()

                with mock.patch.object(audio, "write_json_new", side_effect=fake_write_json):
                    with self.assertRaisesRegex(
                        audio.AudioEvidenceError, "split-window"
                    ):
                        self._create(fixture)
                self.assertFalse(fixture["bundle"].exists())

    def test_missing_half_of_bundle_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            self._create(fixture)
            bundle = json.loads(fixture["bundle"].read_text(encoding="utf-8"))
            bundle["bindings"].pop("av_event_evidence")
            self._write(fixture["bundle"], bundle)

            result = self._evaluate(fixture)
            self.assertEqual(result["status"], "BLOCKED")
            self.assertIn("AV_EVIDENCE_BUNDLE_INVALID", result["reason_codes"])

    def test_tampering_either_manifest_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            self._create(fixture)
            fixture["conflict"].write_text("tampered\n", encoding="utf-8")
            self.assertEqual(self._evaluate(fixture)["status"], "BLOCKED")

    def test_bundle_tool_binding_tamper_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            self._create(fixture)
            bundle = json.loads(fixture["bundle"].read_text(encoding="utf-8"))
            bundle["tools"]["creator"]["sha256"] = "0" * 64
            self._write(fixture["bundle"], bundle)

            self.assertEqual(self._evaluate(fixture)["status"], "BLOCKED")

    def test_conflict_frame_must_be_real_and_checksum_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            conflict = json.loads(fixture["conflict"].read_text(encoding="utf-8"))
            conflict["frames"][0] = {
                "source_frame_index": 0,
                "pts": 0,
                "checksum": "A",
            }
            self._write(fixture["conflict"], conflict)

            with self.assertRaises(audio.AudioEvidenceError):
                self._create(fixture)
            self.assertFalse(fixture["bundle"].exists())

    def test_scanner_candidates_cannot_masquerade_as_observation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            self._write(
                fixture["observation"],
                {
                    "schema_version": 3,
                    "kind": audio.EVENT_SCAN_MANIFEST_KIND,
                    "status": "CANDIDATES_FOUND",
                    "candidates": [{"requires_human_observation": True}],
                },
            )
            event = json.loads(fixture["event"].read_text(encoding="utf-8"))
            event["bindings"]["observation"] = audio._file_record(
                fixture["observation"]
            )
            self._write(fixture["event"], event)

            with self.assertRaisesRegex(audio.AudioEvidenceError, "scanner candidates"):
                self._create(fixture)
            self.assertFalse(fixture["bundle"].exists())

    def test_bundle_is_write_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            self._create(fixture)
            with self.assertRaises(FileExistsError):
                self._create(fixture)

    def test_empty_event_observation_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            observation = json.loads(
                fixture["observation"].read_text(encoding="utf-8")
            )
            observation["anchors"] = []
            self._write(fixture["observation"], observation)
            event = json.loads(fixture["event"].read_text(encoding="utf-8"))
            event["bindings"]["observation"] = audio._file_record(
                fixture["observation"]
            )
            self._write(fixture["event"], event)
            with self.assertRaisesRegex(
                audio.AudioEvidenceError, "at least one observed anchor"
            ):
                self._create(fixture)
            self.assertFalse(fixture["bundle"].exists())

    def test_reconciliation_builds_isolated_write_once_conflict_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._reconciliation_fixture(Path(temp_value))
            evidence = audio.create_pts_conflict_evidence_from_reconciliation(
                fixture["source"],
                fixture["reconciliation"],
                fixture["conflict"],
            )
            self.assertEqual(evidence["conflict_indices"], [3, 4, 5, 7])
            self.assertEqual(evidence["time_authority"], "none")
            self.assertFalse(evidence["production_consumer_allowed"])
            self.assertFalse(evidence["gate_approval"])
            loaded = audio._load_pts_conflict_evidence(
                fixture["conflict"],
                source_media_path=fixture["source"],
                source_oracle_path=fixture["reconciliation"],
            )
            self.assertEqual(loaded["conflict_indices"], [3, 4, 5, 7])
            self.assertEqual(
                loaded["source_provenance"]["kind"],
                "legacy_full_oracle_reconciliation",
            )
            with self.assertRaises(FileExistsError):
                audio.create_pts_conflict_evidence_from_reconciliation(
                    fixture["source"],
                    fixture["reconciliation"],
                    fixture["conflict"],
                )

    def test_reconciliation_bound_input_tamper_blocks_conflict_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._reconciliation_fixture(Path(temp_value))
            audio.create_pts_conflict_evidence_from_reconciliation(
                fixture["source"],
                fixture["reconciliation"],
                fixture["conflict"],
            )
            fixture["source_decode"].write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(
                audio.AudioEvidenceError, "reconciliation verification failed"
            ):
                audio._load_pts_conflict_evidence(
                    fixture["conflict"],
                    source_media_path=fixture["source"],
                    source_oracle_path=fixture["reconciliation"],
                )

    def test_reconciliation_cannot_enter_normal_oracle_loader(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._reconciliation_fixture(Path(temp_value))
            with self.assertRaisesRegex(audio.AudioEvidenceError, "schema or kind"):
                audio._load_bound_oracle(
                    audio._file_record(fixture["reconciliation"]),
                    "source oracle",
                    fixture["source"],
                    audio.sha256_file(fixture["source"]),
                )


if __name__ == "__main__":
    unittest.main()
