from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "scripts" / "pts_normalized_proxy.py"
SPEC = importlib.util.spec_from_file_location("pts_normalized_proxy", MODULE_PATH)
assert SPEC and SPEC.loader
proxy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proxy)


ANCHOR_CREATED_UTC = "2000-01-01T00:00:00+00:00"
GENERATION_STARTED_UTC = "2000-01-01T00:00:01+00:00"
MANIFEST_CREATED_UTC = "2000-01-01T00:00:02+00:00"


def write_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def source_anchor_payload(
    source: Path,
    source_oracle: Path,
    *,
    scope: str,
    business_frame_count: int,
    source_start_frame: int = 0,
) -> dict:
    return {
        "schema_version": proxy.SOURCE_CONTENT_ANCHOR_SCHEMA_VERSION,
        "kind": proxy.SOURCE_CONTENT_ANCHOR_MANIFEST_KIND,
        "created_utc": ANCHOR_CREATED_UTC,
        "source_only": True,
        "scope": scope,
        "bindings": {
            "source_media": proxy._file_record(source, include_size=True),
            "source_oracle": proxy._file_record(
                source_oracle, include_size=False
            ),
        },
        "source": {
            "business_frame_start": source_start_frame,
            "business_frame_count": business_frame_count,
            "source_frame_domain": [
                source_start_frame,
                source_start_frame + business_frame_count,
            ],
            "frame_sequence_basis": "decoded_source_picture_order",
        },
    }


def create_source_anchor(
    source: Path,
    source_oracle: Path,
    output: Path,
    *,
    scope: str,
    business_frame_count: int,
    required_source_start: int = 0,
    required_source_end: int,
) -> dict:
    write_json(
        output,
        source_anchor_payload(
            source,
            source_oracle,
            scope=scope,
            business_frame_count=business_frame_count,
            source_start_frame=required_source_start,
        ),
    )
    return proxy._validate_source_anchor_manifest(
        output,
        source,
        source_oracle,
        expected_scope=scope,
        required_source_start=required_source_start,
        required_source_end=required_source_end,
    )


def create_source_fixture(
    temp: Path,
    checksums: list[str],
    *,
    scope: str,
    anchor_frame_count: int | None = None,
    anchor_start_frame: int = 0,
) -> tuple[Path, Path, Path, dict]:
    source = temp / "source.mp4"
    source.write_bytes(b"video")
    source_oracle = temp / "source.oracle.json"
    write_json(source_oracle, source_report(source, checksums))
    source_anchor = temp / "source.anchor.json"
    count = len(checksums) if anchor_frame_count is None else anchor_frame_count
    anchor_record = create_source_anchor(
        source,
        source_oracle,
        source_anchor,
        scope=scope,
        business_frame_count=count,
        required_source_start=anchor_start_frame,
        required_source_end=anchor_start_frame + count,
    )
    return source, source_oracle, source_anchor, anchor_record


def source_report(source: Path, checksums: list[str]) -> dict:
    rows = [
        {
            "n": index,
            "pts": index * 2,
            "pts_time": index * 0.02,
            "duration": 2,
            "duration_time": 0.02,
            "checksum": checksum,
        }
        for index, checksum in enumerate(checksums)
    ]
    stat = source.stat()
    return {
        "kind": "mpv_phase0_frame_pts_oracle",
        "video": {
            "path": str(source),
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        },
        "ffmpeg": {"returncode": 0},
        "showinfo": {
            "parsed_frames": len(rows),
            "time_base": {"numerator": 1, "denominator": 100},
            "pixel_formats": ["yuv420p"],
        },
        "pts_table": rows,
    }


def manifest(checksums: list[str]) -> dict:
    count = len(checksums)
    return {
        "source": {
            "business_frame_count": count,
            "scope": "full",
            "pixel_format": "yuv420p",
        },
        "normalized_timeline": {
            "time_base": {"numerator": 1, "denominator": 100},
            "duration_ticks": 2,
        },
        "generation": {"encoder": {"pixel_format": "yuv420p"}},
        "terminal_guard": {
            "proxy_frame_index": count,
            "expected_checksum": checksums[-1],
            "business_frame_domain": [0, count],
            "included_in_business_domain": False,
        },
    }


def decoded_proxy_report(checksums: list[str]) -> dict:
    count = len(checksums)
    rows = [
        {
            "n": index,
            "pts": index * 2,
            "pts_time": index * 0.02,
            "duration": 2,
            "duration_time": 0.02,
            "checksum": checksum,
        }
        for index, checksum in enumerate(checksums)
    ]
    rows.append(
        {
            "n": count,
            "pts": count * 2,
            "pts_time": count * 0.02,
            "duration": 0,
            "duration_time": 0.0,
            "checksum": checksums[-1],
        }
    )
    return {
        "status": "BLOCKED",
        "reason_codes": ["FRAME_DURATION_NON_POSITIVE"],
        "ffmpeg": {"returncode": 0},
        "showinfo": {
            "time_base": {"numerator": 1, "denominator": 100},
            "pixel_formats": ["yuv420p"],
        },
        "pts_table": rows,
        "examples": {
            "duration_problems": [
                {"n": count, "field": "duration", "value": 0},
                {"n": count, "field": "duration_time", "value": 0.0},
            ]
        },
    }


class SourceOracleTests(unittest.TestCase):
    def test_source_oracle_requires_checksums_and_positive_constant_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            source = Path(temp_value) / "source.mp4"
            source.write_bytes(b"video")
            report = source_report(source, ["AAAA0001", "BBBB0002"])

            evidence = proxy.analyze_source_oracle(
                report, source, business_frames=2
            )
            self.assertEqual(evidence["duration_ticks"], 2)
            self.assertEqual(evidence["business_frame_count"], 2)

            report["pts_table"][1].pop("checksum")
            with self.assertRaises(proxy.ProxyEvidenceError):
                proxy.analyze_source_oracle(report, source, business_frames=2)

    def test_generation_decode_can_supply_checksums_for_existing_timing_table(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            source = Path(temp_value) / "source.mp4"
            source.write_bytes(b"video")
            report = source_report(source, ["AAAA0001", "BBBB0002"])
            for row in report["pts_table"]:
                row.pop("checksum")
            checksum_report = {
                "kind": "mpv_phase0_proxy_source_decode_evidence",
                "video": {
                    "path": str(source),
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                },
                "ffmpeg": {"returncode": 0},
                "showinfo": {"pixel_formats": ["yuv420p"]},
                "pts_table": source_report(
                    source, ["AAAA0001", "BBBB0002"]
                )["pts_table"],
            }

            evidence = proxy.analyze_source_oracle(
                report,
                source,
                business_frames=2,
                checksum_report=checksum_report,
            )
            self.assertEqual(
                [row["checksum"] for row in evidence["rows"]],
                ["AAAA0001", "BBBB0002"],
            )
            checksum_report["pts_table"].append(
                {
                    "n": 2,
                    "pts": 4,
                    "pts_time": 0.04,
                    "duration": 2,
                    "duration_time": 0.02,
                    "checksum": "CCCC0003",
                }
            )
            with self.assertRaises(proxy.ProxyEvidenceError):
                proxy.analyze_source_oracle(
                    report,
                    source,
                    business_frames=2,
                    checksum_report=checksum_report,
                )
            checksum_report["pts_table"].pop()
            checksum_report["pts_table"][1]["duration"] = 3
            with self.assertRaises(proxy.ProxyEvidenceError):
                proxy.analyze_source_oracle(
                    report,
                    source,
                    business_frames=2,
                    checksum_report=checksum_report,
                )

    def test_complete_reference_with_generation_checksums_stays_full_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            source = Path(temp_value) / "source.mp4"
            source.write_bytes(b"video")
            report = source_report(source, ["AAAA0001", "BBBB0002"])
            checksum_report = {
                "kind": "mpv_phase0_proxy_source_decode_evidence",
                "video": {
                    "path": str(source),
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                },
                "ffmpeg": {"returncode": 0},
                "showinfo": {"pixel_formats": ["yuv420p"]},
                "pts_table": [dict(row) for row in report["pts_table"]],
            }
            for row in report["pts_table"]:
                row.pop("checksum")

            evidence = proxy.analyze_source_oracle(
                report,
                source,
                business_frames=None,
                checksum_report=checksum_report,
            )
            self.assertEqual(evidence["scope"], "full")
            self.assertEqual(evidence["business_frame_count"], 2)

    def test_variable_duration_is_blocked_without_frame_fps_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            source = Path(temp_value) / "source.mp4"
            source.write_bytes(b"video")
            report = source_report(source, ["AAAA0001", "BBBB0002"])
            report["pts_table"][1]["duration"] = 3
            report["pts_table"][1]["duration_time"] = 0.03
            with self.assertRaisesRegex(proxy.ProxyEvidenceError, "frame/fps"):
                proxy.analyze_source_oracle(report, source, business_frames=2)

    def test_partial_oracle_requires_an_explicit_prefix_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            source = Path(temp_value) / "source.mp4"
            source.write_bytes(b"video")
            report = source_report(source, ["AAAA0001", "BBBB0002"])
            report["reason_codes"] = ["PARTIAL_SCAN"]
            with self.assertRaises(proxy.ProxyEvidenceError):
                proxy.analyze_source_oracle(report, source, business_frames=None)
            evidence = proxy.analyze_source_oracle(
                report, source, business_frames=2
            )
            self.assertEqual(evidence["scope"], "prefix")

    def test_nonzero_source_window_maps_to_proxy_local_frame_indices(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            source = Path(temp_value) / "source.mp4"
            source.write_bytes(b"video")
            report = source_report(
                source,
                ["AAAA0001", "BBBB0002", "CCCC0003", "DDDD0004"],
            )

            evidence = proxy.analyze_source_oracle(
                report,
                source,
                source_start_frame=2,
                business_frames=2,
            )

            self.assertEqual(evidence["source_frame_domain"], [2, 4])
            self.assertEqual(evidence["proxy_frame_domain"], [0, 2])
            self.assertEqual(
                [row["n"] for row in evidence["rows"]], [0, 1]
            )
            self.assertEqual(
                [row["source_frame_index"] for row in evidence["rows"]],
                [2, 3],
            )
            self.assertEqual(
                [row["checksum"] for row in evidence["rows"]],
                ["CCCC0003", "DDDD0004"],
            )
            self.assertEqual(evidence["scope"], "prefix")
            self.assertEqual(
                evidence["frame_mapping"]["source_to_proxy_offset"], -2
            )

    def test_nonzero_source_window_requires_explicit_valid_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            source = Path(temp_value) / "source.mp4"
            source.write_bytes(b"video")
            report = source_report(
                source, ["AAAA0001", "BBBB0002", "CCCC0003"]
            )

            with self.assertRaisesRegex(
                proxy.ProxyEvidenceError, "business_frames is required"
            ):
                proxy.analyze_source_oracle(
                    report,
                    source,
                    source_start_frame=1,
                    business_frames=None,
                )
            with self.assertRaisesRegex(
                proxy.ProxyEvidenceError, "window exceeds"
            ):
                proxy.analyze_source_oracle(
                    report,
                    source,
                    source_start_frame=2,
                    business_frames=2,
                )

    def test_generation_checksum_evidence_binds_nonzero_source_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            source = Path(temp_value) / "source.mp4"
            source.write_bytes(b"video")
            report = source_report(
                source,
                ["AAAA0001", "BBBB0002", "CCCC0003", "DDDD0004"],
            )
            checksum_rows = [dict(row) for row in report["pts_table"][2:4]]
            for local_index, row in enumerate(checksum_rows):
                row["n"] = local_index
                row["source_frame_index"] = 2 + local_index
            checksum_report = {
                "kind": "mpv_phase0_proxy_source_decode_evidence",
                "video": {
                    "path": str(source),
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                },
                "ffmpeg": {"returncode": 0},
                "source_frame_start": 2,
                "source_frame_count": 2,
                "source_frame_domain": [2, 4],
                "pts_table": checksum_rows,
            }

            evidence = proxy.analyze_source_oracle(
                report,
                source,
                source_start_frame=2,
                business_frames=2,
                checksum_report=checksum_report,
            )
            self.assertEqual(evidence["source_frame_domain"], [2, 4])

            checksum_report["source_frame_start"] = 1
            with self.assertRaisesRegex(
                proxy.ProxyEvidenceError, "frame start differs"
            ):
                proxy.analyze_source_oracle(
                    report,
                    source,
                    source_start_frame=2,
                    business_frames=2,
                    checksum_report=checksum_report,
                )


class ProxyCommandTests(unittest.TestCase):
    def test_command_clones_one_guard_and_uses_source_duration_ticks(self) -> None:
        command = proxy.build_ffmpeg_command(
            Path("ffmpeg"),
            Path("source.mp4"),
            Path("proxy.mp4"),
            business_frame_count=40,
            time_base_numerator=1,
            time_base_denominator=15360,
            duration_ticks=256,
        )
        video_filter = command[command.index("-vf") + 1]
        self.assertTrue(
            video_filter.startswith("trim=end_frame=40,showinfo@source,")
        )
        self.assertIn("tpad=stop_mode=clone:stop=1", video_filter)
        self.assertIn("setpts=N*256", video_filter)
        self.assertEqual(command[command.index("-frames:v") + 1], "41")
        self.assertIn("-an", command)
        self.assertNotIn("-r", command)

    def test_command_trims_explicit_nonzero_source_window(self) -> None:
        command = proxy.build_ffmpeg_command(
            Path("ffmpeg"),
            Path("source.mp4"),
            Path("proxy.mp4"),
            source_start_frame=17,
            business_frame_count=4,
            time_base_numerator=1,
            time_base_denominator=100,
            duration_ticks=2,
        )

        video_filter = command[command.index("-vf") + 1]
        self.assertTrue(
            video_filter.startswith(
                "trim=start_frame=17:end_frame=21,showinfo@source,"
            )
        )
        self.assertEqual(command[command.index("-frames:v") + 1], "5")

    def test_manifest_command_and_timeline_are_rederived_from_source_evidence(self) -> None:
        checksums = ["AAAA0001", "BBBB0002"]
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        temp = Path(temporary.name)
        source, source_oracle, _, anchor_record = create_source_fixture(
            temp, checksums, scope="full"
        )
        output = temp / "proxy.mp4"
        ffmpeg = temp / "ffmpeg.exe"
        ffmpeg.write_bytes(b"ffmpeg")
        evidence = {
            "business_frame_count": 2,
            "duration_ticks": 2,
            "time_base": {"numerator": 1, "denominator": 100},
            "scope": "full",
            "pixel_format": "yuv420p",
            "source_ordered_last_end_ticks": 4,
            "source_presentation_end_ticks": 4,
            "rows": [
                {"n": 0, "duration": 2, "duration_time": 0.02, "checksum": checksums[0]},
                {"n": 1, "duration": 2, "duration_time": 0.02, "checksum": checksums[1]},
            ],
        }
        command = proxy.build_ffmpeg_command(
            ffmpeg,
            source,
            output,
            business_frame_count=2,
            time_base_numerator=1,
            time_base_denominator=100,
            duration_ticks=2,
        )
        value = {
            "created_utc": MANIFEST_CREATED_UTC,
            "source_anchor_manifest": anchor_record,
            "source": {
                "business_frame_start": 0,
                "business_frame_count": 2,
                "scope": "full",
                "oracle": {
                    "path": str(source_oracle),
                    "sha256": proxy.sha256_file(source_oracle),
                },
                "first_checksum": checksums[0],
                "last_checksum": checksums[1],
                "pixel_format": "yuv420p",
            },
            "normalized_timeline": {
                "time_base": {"numerator": 1, "denominator": 100},
                "duration_ticks": 2,
                "business_pts_start": 0,
                "business_pts_end_exclusive": 4,
                "source_ordered_last_end_ticks": 4,
                "source_presentation_end_ticks": 4,
                "normalized_end_ticks": 4,
                "normalized_minus_source_end_ticks": 0,
                "normalized_minus_source_end_seconds": 0.0,
                "frame_fps_fallback_used": False,
            },
            "terminal_guard": {
                "method": "clone_last_business_frame",
                "proxy_frame_index": 2,
                "pts_ticks": 4,
                "expected_checksum": checksums[1],
                "business_frame_domain": [0, 2],
                "included_in_business_domain": False,
            },
            "generation": {
                "started_utc": GENERATION_STARTED_UTC,
                "source_anchor_manifest": anchor_record,
                "canonical_command": command,
                "executed_command": command[:-1] + [str(Path("temporary.mp4").resolve())],
                "encoder": {
                    "name": "libx264",
                    "preset": proxy.DEFAULT_PRESET,
                    "lossless_qp": 0,
                    "b_frames": 0,
                    "pixel_format": "yuv420p",
                    "identification_lines": ["264 - core test"],
                },
            },
            "audio_timeline": {"status": "NOT_RUN"},
            "av_sync": {"status": "NOT_RUN"},
        }

        proxy.validate_manifest_semantics(value, evidence, source, output, ffmpeg)
        value["normalized_timeline"]["duration_ticks"] = 3
        with self.assertRaises(proxy.ProxyEvidenceError):
            proxy.validate_manifest_semantics(value, evidence, source, output, ffmpeg)

    def test_manifest_rederives_nonzero_source_window_and_guard_mapping(self) -> None:
        checksums = ["CCCC0003", "DDDD0004"]
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        temp = Path(temporary.name)
        source, source_oracle, _, anchor_record = create_source_fixture(
            temp,
            ["AAAA0001", "BBBB0002", *checksums],
            scope="prefix",
            anchor_frame_count=2,
            anchor_start_frame=2,
        )
        output = temp / "proxy.mp4"
        ffmpeg = temp / "ffmpeg.exe"
        ffmpeg.write_bytes(b"ffmpeg")
        mapping = proxy._frame_mapping(2, 2)
        evidence = {
            "business_frame_count": 2,
            "source_frame_start": 2,
            "source_frame_end_exclusive": 4,
            "source_frame_domain": [2, 4],
            "proxy_frame_domain": [0, 2],
            "frame_mapping": mapping,
            "duration_ticks": 2,
            "time_base": {"numerator": 1, "denominator": 100},
            "scope": "prefix",
            "pixel_format": "yuv420p",
            "source_ordered_first_pts_ticks": 4,
            "source_presentation_start_ticks": 4,
            "source_ordered_last_end_ticks": 8,
            "source_presentation_end_ticks": 8,
            "rows": [
                {
                    "n": 0,
                    "source_frame_index": 2,
                    "duration": 2,
                    "duration_time": 0.02,
                    "checksum": checksums[0],
                },
                {
                    "n": 1,
                    "source_frame_index": 3,
                    "duration": 2,
                    "duration_time": 0.02,
                    "checksum": checksums[1],
                },
            ],
        }
        command = proxy.build_ffmpeg_command(
            ffmpeg,
            source,
            output,
            source_start_frame=2,
            business_frame_count=2,
            time_base_numerator=1,
            time_base_denominator=100,
            duration_ticks=2,
        )
        value = {
            "created_utc": MANIFEST_CREATED_UTC,
            "source_anchor_manifest": anchor_record,
            "source": {
                "business_frame_start": 2,
                "business_frame_count": 2,
                "source_frame_domain": [2, 4],
                "proxy_frame_domain": [0, 2],
                "frame_mapping": mapping,
                "scope": "prefix",
                "oracle": {
                    "path": str(source_oracle),
                    "sha256": proxy.sha256_file(source_oracle),
                    "source_frame_start": 2,
                    "source_frame_end_exclusive": 4,
                    "source_frame_domain": [2, 4],
                },
                "generation_source_decode": {
                    "source_frame_start": 2,
                    "source_frame_end_exclusive": 4,
                    "source_frame_domain": [2, 4],
                },
                "first_checksum": checksums[0],
                "last_checksum": checksums[1],
                "pixel_format": "yuv420p",
            },
            "normalized_timeline": {
                "time_base": {"numerator": 1, "denominator": 100},
                "duration_ticks": 2,
                "business_pts_start": 0,
                "business_pts_end_exclusive": 4,
                "source_ordered_first_pts_ticks": 4,
                "source_presentation_start_ticks": 4,
                "source_ordered_last_end_ticks": 8,
                "source_presentation_end_ticks": 8,
                "normalized_end_ticks": 4,
                "normalized_minus_source_end_ticks": -4,
                "normalized_minus_source_end_seconds": -0.04,
                "normalized_minus_source_start_ticks": -4,
                "normalized_minus_source_start_seconds": -0.04,
                "source_frame_start": 2,
                "source_frame_end_exclusive": 4,
                "source_frame_domain": [2, 4],
                "proxy_frame_domain": [0, 2],
                "frame_mapping": mapping,
                "frame_fps_fallback_used": False,
            },
            "terminal_guard": {
                "method": "clone_last_business_frame",
                "proxy_frame_index": 2,
                "source_frame_index": 3,
                "pts_ticks": 4,
                "expected_checksum": checksums[1],
                "business_frame_domain": [0, 2],
                "source_frame_domain": [2, 4],
                "proxy_frame_domain": [0, 2],
                "included_in_business_domain": False,
            },
            "generation": {
                "started_utc": GENERATION_STARTED_UTC,
                "source_anchor_manifest": anchor_record,
                "canonical_command": command,
                "executed_command": command[:-1]
                + [str(Path("temporary.mp4").resolve())],
                "source_frame_start": 2,
                "source_frame_end_exclusive": 4,
                "source_frame_domain": [2, 4],
                "proxy_frame_domain": [0, 2],
                "frame_mapping": mapping,
                "encoder": {
                    "name": "libx264",
                    "preset": proxy.DEFAULT_PRESET,
                    "lossless_qp": 0,
                    "b_frames": 0,
                    "pixel_format": "yuv420p",
                    "identification_lines": ["264 - core test"],
                },
            },
            "audio_timeline": {"status": "NOT_RUN"},
            "av_sync": {"status": "NOT_RUN"},
        }

        proxy.validate_manifest_semantics(value, evidence, source, output, ffmpeg)

        value["source"]["business_frame_start"] = 1
        with self.assertRaises(proxy.ProxyEvidenceError):
            proxy.validate_manifest_semantics(value, evidence, source, output, ffmpeg)


class SourceAnchorBindingTests(unittest.TestCase):
    def test_source_only_key_check_rejects_camel_case_proxy_fields(self) -> None:
        proxy._assert_source_only(
            {"tools": {"verifier": {"path": "verify_proxy_audio_av.py"}}}
        )
        with self.assertRaises(proxy.ProxyEvidenceError):
            proxy._assert_source_only({"proxyFrameIndex": 0})

    @staticmethod
    def _build(
        temp: Path,
        source: Path,
        source_oracle: Path,
        source_anchor: Path,
    ) -> dict:
        ffmpeg_path = temp / "ffmpeg.exe"
        ffmpeg_path.write_bytes(b"ffmpeg")
        return proxy.build_proxy(
            source,
            source_oracle,
            temp / "source.decode.json",
            temp / "proxy.mp4",
            temp / "proxy.manifest.json",
            proxy.frame_oracle.FfmpegExecutable(ffmpeg_path, "test"),
            source_anchor_manifest_path=source_anchor,
            business_frames=None,
            preset=proxy.DEFAULT_PRESET,
        )

    def test_builder_rejects_missing_source_anchor_before_starting_ffmpeg(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            source = temp / "source.mp4"
            source.write_bytes(b"video")
            source_oracle = temp / "source.oracle.json"
            write_json(
                source_oracle,
                source_report(source, ["AAAA0001", "BBBB0002"]),
            )

            with mock.patch.object(proxy.subprocess, "Popen") as popen:
                with self.assertRaises(FileNotFoundError):
                    self._build(
                        temp,
                        source,
                        source_oracle,
                        temp / "missing.source.anchor.json",
                    )

            popen.assert_not_called()
            self.assertFalse((temp / "proxy.mp4").exists())

    def test_builder_rejects_mismatched_source_anchor_before_starting_ffmpeg(self) -> None:
        cases = (
            "source_sha256",
            "source_oracle",
            "scope",
            "frame_domain",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_value:
                temp = Path(temp_value)
                anchor_scope = "prefix" if case == "scope" else "full"
                anchor_count = 1 if case == "frame_domain" else 2
                source, source_oracle, source_anchor, _ = create_source_fixture(
                    temp,
                    ["AAAA0001", "BBBB0002"],
                    scope=anchor_scope,
                    anchor_frame_count=anchor_count,
                )
                payload = json.loads(source_anchor.read_text(encoding="utf-8"))
                if case == "source_sha256":
                    payload["bindings"]["source_media"]["sha256"] = "0" * 64
                    write_json(source_anchor, payload)
                elif case == "source_oracle":
                    payload["bindings"]["source_oracle"]["sha256"] = "0" * 64
                    write_json(source_anchor, payload)

                with mock.patch.object(proxy.subprocess, "Popen") as popen:
                    with self.assertRaises(proxy.ProxyEvidenceError):
                        self._build(
                            temp,
                            source,
                            source_oracle,
                            source_anchor,
                        )

                popen.assert_not_called()
                self.assertFalse((temp / "proxy.mp4").exists())

    def test_anchor_changed_during_generation_does_not_publish_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            checksums = ["AAAA0001", "BBBB0002"]
            source, source_oracle, source_anchor, _ = create_source_fixture(
                temp, checksums, scope="full"
            )
            reference_evidence = proxy.analyze_source_oracle(
                json.loads(source_oracle.read_text(encoding="utf-8")),
                source,
                business_frames=None,
                require_checksums=False,
            )

            class CompletedProcess:
                def __init__(self) -> None:
                    self.stderr = io.StringIO("")

                @staticmethod
                def wait() -> int:
                    return 0

                @staticmethod
                def poll() -> int:
                    return 0

                @staticmethod
                def terminate() -> None:
                    return None

            def start_generation(command: list[str], **_: object) -> CompletedProcess:
                Path(command[-1]).write_bytes(b"generated proxy")
                payload = json.loads(source_anchor.read_text(encoding="utf-8"))
                payload["tamper_marker"] = "changed after FFmpeg start"
                write_json(source_anchor, payload)
                return CompletedProcess()

            with (
                mock.patch.object(
                    proxy,
                    "analyze_source_oracle",
                    return_value=reference_evidence,
                ),
                mock.patch.object(
                    proxy.subprocess,
                    "Popen",
                    side_effect=start_generation,
                ),
            ):
                with self.assertRaisesRegex(
                    proxy.ProxyEvidenceError,
                    "changed during proxy generation",
                ):
                    self._build(
                        temp,
                        source,
                        source_oracle,
                        source_anchor,
                    )

            self.assertFalse((temp / "proxy.mp4").exists())
            self.assertFalse((temp / "source.decode.json").exists())
            self.assertFalse((temp / "proxy.manifest.json").exists())
            self.assertEqual(list(temp.glob(".proxy.*.tmp.mp4")), [])

    def test_verify_detects_source_anchor_replaced_after_manifest_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            checksums = ["AAAA0001", "BBBB0002"]
            source, source_oracle, source_anchor, anchor_record = (
                create_source_fixture(temp, checksums, scope="full")
            )
            proxy_media = temp / "proxy.mp4"
            proxy_media.write_bytes(b"proxy")
            source_decode = temp / "source.decode.json"
            write_json(
                source_decode,
                {"source_anchor_manifest": anchor_record},
            )
            ffmpeg_path = temp / "ffmpeg.exe"
            ffmpeg_path.write_bytes(b"ffmpeg")
            ffmpeg_record = {
                "path": str(ffmpeg_path),
                "sha256": proxy.sha256_file(ffmpeg_path),
                "version_line": "ffmpeg test",
                "library_lines": [],
            }
            manifest_path = temp / "proxy.manifest.json"
            write_json(
                manifest_path,
                {
                    "schema_version": proxy.SCHEMA_VERSION,
                    "kind": proxy.MANIFEST_KIND,
                    "created_utc": MANIFEST_CREATED_UTC,
                    "source_anchor_manifest": anchor_record,
                    "source": {
                        **proxy._file_record(source, include_size=True),
                        "scope": "full",
                        "business_frame_start": 0,
                        "business_frame_count": len(checksums),
                        "oracle": proxy._file_record(
                            source_oracle, include_size=False
                        ),
                        "generation_source_decode": proxy._file_record(
                            source_decode, include_size=False
                        ),
                    },
                    "proxy": proxy._file_record(proxy_media, include_size=True),
                    "normalized_timeline": {},
                    "terminal_guard": {},
                    "generation": {
                        "started_utc": GENERATION_STARTED_UTC,
                        "source_anchor_manifest": anchor_record,
                        "tools": proxy._tool_bindings(),
                        "ffmpeg": ffmpeg_record,
                        "encoder": {},
                    },
                },
            )
            evidence = proxy.analyze_source_oracle(
                json.loads(source_oracle.read_text(encoding="utf-8")),
                source,
                business_frames=None,
            )

            replacement = json.loads(source_anchor.read_text(encoding="utf-8"))
            replacement["replacement_marker"] = "new file contents"
            write_json(source_anchor, replacement)
            replacement_record = proxy._validate_source_anchor_manifest(
                source_anchor,
                source,
                source_oracle,
                expected_scope="full",
                required_source_start=0,
                required_source_end=len(checksums),
            )
            self.assertNotEqual(anchor_record, replacement_record)

            with (
                mock.patch.object(
                    proxy,
                    "analyze_source_oracle",
                    return_value=evidence,
                ),
                mock.patch.object(
                    proxy,
                    "ffmpeg_version_record",
                    return_value=ffmpeg_record,
                ),
            ):
                report, oracle_report = proxy.verify_proxy(
                    manifest_path,
                    proxy.frame_oracle.FfmpegExecutable(ffmpeg_path, "test"),
                    threads=1,
                )

            self.assertIsNone(oracle_report)
            self.assertEqual(report["binding"]["status"], "BLOCKED")
            self.assertTrue(
                any(
                    "source anchor registration binding is stale" in reason
                    for reason in report["binding"]["reason_codes"]
                ),
                report["binding"]["reason_codes"],
            )

    def test_build_cli_requires_source_anchor_manifest(self) -> None:
        arguments = [
            "build",
            "source.mp4",
            "--source-oracle",
            "source.oracle.json",
            "--source-decode-output",
            "source.decode.json",
            "--output",
            "proxy.mp4",
            "--manifest",
            "proxy.manifest.json",
        ]
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr), self.assertRaises(SystemExit) as raised:
            proxy.main(arguments)

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--source-anchor-manifest", stderr.getvalue())


class ProxyEvaluationTests(unittest.TestCase):
    def test_zero_duration_is_allowed_only_for_guard_outside_business_domain(self) -> None:
        checksums = ["AAAA0001", "BBBB0002", "CCCC0003"]
        source_rows = source_report_rows = [
            {"n": index, "duration": 2, "duration_time": 0.02, "checksum": value}
            for index, value in enumerate(checksums)
        ]
        result = proxy.evaluate_video_proxy(
            manifest(checksums),
            source_report_rows,
            decoded_proxy_report(checksums),
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["checksum_alignment"]["mismatch_count"], 0)
        self.assertEqual(result["positive_business_durations"]["status"], "PASS")
        self.assertTrue(result["terminal_guard"]["observed"])
        self.assertFalse(result["terminal_guard"]["included_in_business_domain"])
        self.assertEqual(
            result["generic_oracle"]["allowed_guard_only_reason_codes"],
            ["FRAME_DURATION_NON_POSITIVE"],
        )

    def test_business_checksum_mismatch_blocks_video(self) -> None:
        checksums = ["AAAA0001", "BBBB0002"]
        report = decoded_proxy_report(checksums)
        report["pts_table"][1]["checksum"] = "DDDD0004"
        source_rows = [
            {"n": index, "duration": 2, "duration_time": 0.02, "checksum": value}
            for index, value in enumerate(checksums)
        ]
        result = proxy.evaluate_video_proxy(
            manifest(checksums), source_rows, report
        )
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("BUSINESS_FRAME_CHECKSUM_MISMATCH", result["reason_codes"])

    def test_prefix_video_pass_is_not_authoritative_for_full_source(self) -> None:
        checksums = ["AAAA0001", "BBBB0002"]
        value = manifest(checksums)
        value["source"]["scope"] = "prefix"
        source_rows = [
            {"n": index, "duration": 2, "duration_time": 0.02, "checksum": checksum}
            for index, checksum in enumerate(checksums)
        ]
        result = proxy.evaluate_video_proxy(
            value, source_rows, decoded_proxy_report(checksums)
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["scope"], "prefix")
        self.assertFalse(result["authoritative_for_full_source"])

    def test_nonpositive_business_duration_cannot_be_excused_as_guard(self) -> None:
        checksums = ["AAAA0001", "BBBB0002"]
        report = decoded_proxy_report(checksums)
        report["pts_table"][1]["duration"] = 0
        report["pts_table"][1]["duration_time"] = 0.0
        report["examples"]["duration_problems"].extend(
            [
                {"n": 1, "field": "duration", "value": 0},
                {"n": 1, "field": "duration_time", "value": 0.0},
            ]
        )
        source_rows = [
            {"n": index, "duration": 2, "duration_time": 0.02, "checksum": value}
            for index, value in enumerate(checksums)
        ]
        result = proxy.evaluate_video_proxy(
            manifest(checksums), source_rows, report
        )
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("BUSINESS_FRAME_DURATION_INVALID", result["reason_codes"])
        self.assertIn(
            "GENERIC_ORACLE_FRAME_DURATION_NON_POSITIVE", result["reason_codes"]
        )

    def test_nonzero_source_window_evaluation_uses_explicit_mapping(self) -> None:
        checksums = ["CCCC0003", "DDDD0004"]
        value = manifest(checksums)
        value["source"].update(
            {
                "business_frame_start": 2,
                "source_frame_domain": [2, 4],
                "proxy_frame_domain": [0, 2],
                "frame_mapping": proxy._frame_mapping(2, 2),
                "scope": "prefix",
            }
        )
        value["normalized_timeline"].update(
            {
                "source_frame_start": 2,
                "source_frame_end_exclusive": 4,
                "source_frame_domain": [2, 4],
                "proxy_frame_domain": [0, 2],
                "frame_mapping": proxy._frame_mapping(2, 2),
            }
        )
        value["terminal_guard"].update(
            {
                "source_frame_index": 3,
                "source_frame_domain": [2, 4],
                "proxy_frame_domain": [0, 2],
            }
        )
        source_rows = [
            {
                "n": index,
                "duration": 2,
                "duration_time": 0.02,
                "checksum": checksum,
            }
            for index, checksum in enumerate(
                ["AAAA0001", "BBBB0002", "CCCC0003", "DDDD0004"]
            )
        ]

        result = proxy.evaluate_video_proxy(
            value,
            source_rows,
            decoded_proxy_report(checksums),
        )

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["source_frame_domain"], [2, 4])
        self.assertEqual(result["proxy_frame_domain"], [0, 2])
        self.assertEqual(result["frame_mapping"]["proxy_to_source_offset"], 2)

        value["terminal_guard"]["source_frame_index"] = 2
        blocked = proxy.evaluate_video_proxy(
            value,
            source_rows,
            decoded_proxy_report(checksums),
        )
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertIn("TERMINAL_GUARD_DOMAIN_INVALID", blocked["reason_codes"])


if __name__ == "__main__":
    unittest.main()
