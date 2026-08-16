from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "scripts" / "adapt_legacy_full_oracle.py"
SPEC = importlib.util.spec_from_file_location("adapt_legacy_full_oracle", MODULE_PATH)
assert SPEC and SPEC.loader
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)


class LegacyFullOracleAdapterTests(unittest.TestCase):
    def _write(self, path: Path, value: object) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _rows(self, *, checksums: bool) -> list[dict[str, object]]:
        pts_values = [0, 256, 512, 768, 1280, 768, 1024, 1280]
        pts_times = [
            0.0,
            0.0166667,
            0.0333333,
            0.05,
            0.0833333,
            0.05,
            0.0666667,
            0.0833333,
        ]
        frame_types = ["I", "B", "B", "B", "B", "P", "B", "B"]
        rows: list[dict[str, object]] = []
        for index, (pts, pts_time, frame_type) in enumerate(
            zip(pts_values, pts_times, frame_types)
        ):
            row: dict[str, object] = {
                "n": index,
                "pts": pts,
                "duration": 256,
                "pts_time": pts_time,
                "duration_time": 0.0166667,
            }
            if checksums:
                row.update(
                    checksum=f"{0xA0 + index:08X}",
                    frame_type=frame_type,
                    is_keyframe=1 if index == 0 else 0,
                )
            rows.append(row)
        return rows

    def _showinfo(
        self, rows: list[dict[str, object]], *, checksums: bool
    ) -> dict[str, object]:
        first = dict(rows[0])
        last = dict(rows[-1])
        pts_summary: dict[str, object] = {
            "present_count": len(rows),
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
            "parsed_frames": len(rows),
            "frame_lines": len(rows),
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
                "last": len(rows) - 1,
                "unique_count": len(rows),
                "discontinuity_count": 0,
                "duplicate_count": 0,
                "contiguous_from_zero": True,
            },
            "pts": pts_summary,
            "pts_time": {
                "present_count": len(rows),
                "missing_count": 0,
                "non_monotonic_count": 1,
                "time_base_mismatch_count": 0,
            },
            "first_frame": first,
            "last_frame": last,
        }

    def _duration(self, count: int) -> dict[str, int]:
        return {
            "frame_duration_present_count": count,
            "frame_duration_missing_count": 0,
            "frame_duration_time_present_count": count,
            "frame_duration_time_missing_count": 0,
            "non_positive_count": 0,
            "time_base_mismatch_count": 0,
        }

    def _fixture(self, root: Path) -> dict[str, Path]:
        source = root / "source.mp4"
        source.write_bytes(b"source-media")
        source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        legacy_rows = self._rows(checksums=False)
        decoded_rows = self._rows(checksums=True)
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
                "showinfo": self._showinfo(legacy_rows, checksums=False),
                "pts_table": legacy_rows,
                "duration": self._duration(len(legacy_rows)),
            },
        )
        source_decode = root / "source_decode.json"
        ffmpeg_command = [
            str(root / "ffmpeg.exe"),
            "-i",
            str(source),
            "-vf",
            "showinfo@source",
            "-frames:v",
            str(len(decoded_rows) + 1),
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
                "ffmpeg": {
                    "path": str(root / "ffmpeg.exe"),
                    "command": ffmpeg_command,
                    "returncode": 0,
                },
                "showinfo": self._showinfo(decoded_rows, checksums=True),
                "pts_table": decoded_rows,
                "reference_oracle": {
                    "path": str(legacy),
                    "sha256": adapter._load_snapshot(legacy, "legacy").sha256,
                    "reference_frame_count": len(legacy_rows),
                    "business_frame_count": len(decoded_rows),
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
                "duration": self._duration(len(decoded_rows)),
            },
        )
        manifest = root / "manifest.json"
        self._write(
            manifest,
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
                    "business_frame_count": len(decoded_rows),
                    "identity_unchanged_during_generation": True,
                    "first_checksum": decoded_rows[0]["checksum"],
                    "last_checksum": decoded_rows[-1]["checksum"],
                    "oracle": {
                        "path": str(legacy),
                        "sha256": adapter._load_snapshot(legacy, "legacy").sha256,
                        "scope": "full",
                        "parsed_frames": len(legacy_rows),
                        "reason_codes": [
                            "PTS_DUPLICATE",
                            "PTS_NON_MONOTONIC",
                        ],
                    },
                    "generation_source_decode": {
                        "path": str(source_decode),
                        "sha256": adapter._load_snapshot(
                            source_decode, "source decode"
                        ).sha256,
                        "observed_frames": len(decoded_rows),
                        "business_checksum_frames": len(decoded_rows),
                    },
                },
                "generation": {
                    "executed_command": ffmpeg_command,
                    "ffmpeg": {"path": str(root / "ffmpeg.exe")},
                },
            },
        )
        return {
            "legacy": legacy,
            "source_decode": source_decode,
            "manifest": manifest,
            "output": root / "reconciled.json",
        }

    def _adapt(self, fixture: dict[str, Path]) -> dict:
        return adapter.adapt_legacy_full_oracle(
            fixture["legacy"],
            fixture["source_decode"],
            fixture["manifest"],
            fixture["output"],
        )

    def _mutate_decode_and_rebind(self, fixture: dict[str, Path], mutator) -> None:
        decoded = json.loads(fixture["source_decode"].read_text(encoding="utf-8"))
        mutator(decoded)
        self._write(fixture["source_decode"], decoded)
        manifest = json.loads(fixture["manifest"].read_text(encoding="utf-8"))
        manifest["source"]["generation_source_decode"]["sha256"] = (
            adapter._load_snapshot(fixture["source_decode"], "source decode").sha256
        )
        self._write(fixture["manifest"], manifest)

    def test_reconciles_as_isolated_blocked_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            report = self._adapt(fixture)
            self.assertEqual(report["kind"], adapter.ADAPTER_KIND)
            self.assertNotEqual(report["kind"], adapter.LEGACY_ORACLE_KIND)
            self.assertEqual(report["status"], "BLOCKED")
            self.assertFalse(report["authoritative_frame_timeline"])
            self.assertEqual(report["time_authority"], "none")
            self.assertFalse(report["gate_approval"])
            self.assertFalse(report["production_consumer_allowed"])
            self.assertEqual(report["exit_code"], adapter.EXIT_BLOCKED)
            self.assertEqual(
                report["reconciliation"]["source_frame_domain"], [0, 8]
            )
            self.assertEqual(
                report["pts_conflicts"]["conflict_indices"], [3, 4, 5, 7]
            )
            self.assertEqual(
                report["pts_conflicts"]["duplicate_distinct_checksum_count"], 2
            )
            self.assertEqual(report["pts_conflicts"]["non_monotonic_count"], 1)
            verified = adapter.verify_reconciliation_evidence(fixture["output"])
            self.assertEqual(verified["frame_count"], 8)
            self.assertEqual(verified["conflict_indices"], [3, 4, 5, 7])
            self.assertEqual(verified["time_authority"], "none")
            self.assertFalse(verified["gate_approval"])

    def test_output_is_write_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            self._adapt(fixture)
            with self.assertRaises(FileExistsError):
                self._adapt(fixture)

    def test_timeline_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            self._mutate_decode_and_rebind(
                fixture, lambda value: value["pts_table"][1].update(pts=999)
            )
            with self.assertRaisesRegex(
                adapter.LegacyOracleAdapterError, "timeline mismatch"
            ):
                self._adapt(fixture)
            self.assertFalse(fixture["output"].exists())

    def test_invalid_checksum_is_rejected_when_outer_hash_is_updated(self) -> None:
        for checksum in (None, "", "C0", "GGGGGGGG", "0" * 9):
            with self.subTest(checksum=checksum), tempfile.TemporaryDirectory() as temp_value:
                fixture = self._fixture(Path(temp_value))
                self._mutate_decode_and_rebind(
                    fixture,
                    lambda value, checksum=checksum: value["pts_table"][3].update(
                        checksum=checksum
                    ),
                )
                with self.assertRaisesRegex(
                    adapter.LegacyOracleAdapterError, "showinfo checksum"
                ):
                    self._adapt(fixture)
                self.assertFalse(fixture["output"].exists())

    def test_missing_conflict_reason_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            self._mutate_decode_and_rebind(
                fixture,
                lambda value: value.update(reason_codes=["PTS_DUPLICATE"]),
            )
            with self.assertRaisesRegex(
                adapter.LegacyOracleAdapterError, "lacks required PTS conflict"
            ):
                self._adapt(fixture)

    def test_partial_reference_scope_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            self._mutate_decode_and_rebind(
                fixture,
                lambda value: value["reference_oracle"].update(scope="prefix"),
            )
            with self.assertRaisesRegex(adapter.LegacyOracleAdapterError, "not full"):
                self._adapt(fixture)

    def test_nonzero_ffmpeg_returncode_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            self._mutate_decode_and_rebind(
                fixture,
                lambda value: value["ffmpeg"].update(returncode=1),
            )
            with self.assertRaisesRegex(
                adapter.LegacyOracleAdapterError, "FFmpeg run did not complete"
            ):
                self._adapt(fixture)

    def test_stale_reverse_binding_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            decoded = json.loads(
                fixture["source_decode"].read_text(encoding="utf-8")
            )
            decoded["pts_table"][3]["checksum"] = "DEADBEEF"
            self._write(fixture["source_decode"], decoded)
            with self.assertRaisesRegex(
                adapter.LegacyOracleAdapterError, "SHA-256 is stale"
            ):
                self._adapt(fixture)

    def test_same_checksum_duplicate_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))

            def mutate(value: dict) -> None:
                value["pts_table"][5]["checksum"] = value["pts_table"][3][
                    "checksum"
                ]
                value["showinfo"]["pts"].update(
                    duplicate_distinct_checksum_count=1,
                    duplicate_same_checksum_count=1,
                )

            self._mutate_decode_and_rebind(fixture, mutate)
            with self.assertRaisesRegex(
                adapter.LegacyOracleAdapterError, "distinct decoded-picture"
            ):
                self._adapt(fixture)

    def test_published_report_tampering_is_recomputed_and_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            self._adapt(fixture)
            report = json.loads(fixture["output"].read_text(encoding="utf-8"))
            report["pts_table"][3]["checksum"] = "DEADBEEF"
            self._write(fixture["output"], report)
            with self.assertRaisesRegex(
                adapter.LegacyOracleAdapterError, "differs from recomputed inputs"
            ):
                adapter.verify_reconciliation_evidence(fixture["output"])

    def test_bound_input_tampering_invalidates_published_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            self._adapt(fixture)
            fixture["source_decode"].write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(
                adapter.LegacyOracleAdapterError, "cannot read|SHA-256 is stale"
            ):
                adapter.verify_reconciliation_evidence(fixture["output"])

    def test_publish_failure_leaves_no_partial_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            with mock.patch.object(adapter.os, "link", side_effect=OSError("link failed")):
                with self.assertRaisesRegex(OSError, "link failed"):
                    self._adapt(fixture)
            self.assertFalse(fixture["output"].exists())
            self.assertEqual(
                list(
                    fixture["output"].parent.glob(
                        f".{fixture['output'].name}.*.tmp"
                    )
                ),
                [],
            )


if __name__ == "__main__":
    unittest.main()
