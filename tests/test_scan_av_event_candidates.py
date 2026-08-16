from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "scripts" / "scan_av_event_candidates.py"
SPEC = importlib.util.spec_from_file_location("scan_av_event_candidates", MODULE_PATH)
assert SPEC and SPEC.loader
scan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scan)


def valid_oracle(source: Path, *, scope: str = "partial") -> dict:
    command = ["ffmpeg", "-i", str(source), "-f", "null", "-"]
    if scope == "partial":
        command[-3:-3] = ["-frames:v", "2"]
    return {
        "schema_version": 1,
        "kind": "mpv_phase0_frame_pts_oracle",
        "video": {
            "path": str(source.resolve()),
            "size": source.stat().st_size,
            "sha256": scan.sha256_file(source),
        },
        "ffmpeg": {"returncode": 0, "command": command},
        "showinfo": {"parsed_frames": 2},
        "pts_table": [{"n": 0}, {"n": 1}],
        "pts_conflict_assessment": {"scan_scope": scope},
    }


def fake_ffmpeg_record(path: Path) -> dict:
    return {
        **scan.file_record(path),
        "version_line": "ffmpeg version test",
        "library_lines": [],
    }


class EventCandidateUnitTests(unittest.TestCase):
    def test_visual_change_is_zero_for_identical_frames(self) -> None:
        frame = bytes(range(64))
        self.assertEqual(scan._visual_change_scores([frame, frame]), [0.0, 0.0])

    def test_audio_metrics_use_the_sample_clock(self) -> None:
        samples = [0] * scan.AUDIO_WINDOW_SAMPLES + [3000] * scan.AUDIO_WINDOW_SAMPLES
        metrics = scan._audio_window_metrics(struct.pack(f"<{len(samples)}h", *samples))
        self.assertEqual(len(metrics), 2)
        self.assertEqual(metrics[1]["start_sample"], scan.AUDIO_WINDOW_SAMPLES)
        self.assertAlmostEqual(metrics[1]["start_seconds"], 0.02)
        self.assertEqual(metrics[1]["peak"], 3000)

    def test_candidate_requires_both_visual_and_audio_events(self) -> None:
        audio = [
            {
                "start_sample": index * scan.AUDIO_WINDOW_SAMPLES,
                "end_sample_exclusive": (index + 1) * scan.AUDIO_WINDOW_SAMPLES,
                "start_seconds": index * 0.02,
                "end_seconds": (index + 1) * 0.02,
                "peak": 5000 if index == 3 else 10,
                "rms": 3000.0 if index == 3 else 5.0,
            }
            for index in range(5)
        ]
        candidates = scan._candidate_events(
            [0.0, 0.0, 0.0, 100.0, 0.0],
            audio,
            fps=50.0,
            window_start_seconds=174.0,
        )
        self.assertEqual([item["frame_sample_index"] for item in candidates], [3])
        self.assertAlmostEqual(candidates[0]["video_window_offset_seconds"], 0.06)
        self.assertAlmostEqual(
            candidates[0]["requested_video_position_seconds"], 174.06
        )
        self.assertAlmostEqual(candidates[0]["audio_window_offset_seconds"], 0.06)
        self.assertAlmostEqual(
            candidates[0]["requested_audio_position_seconds"], 174.06
        )
        self.assertAlmostEqual(candidates[0]["sample_grid_delta_seconds"], 0.0)
        self.assertNotIn("source_time_seconds", candidates[0])
        self.assertTrue(candidates[0]["requires_human_observation"])

    def test_all_commands_bind_the_same_nonzero_window_start(self) -> None:
        ffmpeg = Path("ffmpeg.exe")
        media = Path("source.mp4")
        commands = (
            scan.video_sample_command(ffmpeg, media, 174.0, 10.0, 10.0),
            scan.audio_sample_command(ffmpeg, media, 174.0, 10.0),
            scan.contact_sheet_command(
                ffmpeg, media, 174.0, 10.0, Path("contact.png")
            ),
        )
        for command in commands:
            self.assertEqual(command[command.index("-ss") + 1], "174.000000")
            self.assertEqual(command[command.index("-t") + 1], "10.000000")

    def test_ffmpeg_temporary_output_is_not_a_hidden_windows_name(self) -> None:
        temporary = scan._temporary_path(Path("contact.png"), ".png")

        self.assertFalse(temporary.name.startswith("."))
        self.assertTrue(temporary.name.endswith(".tmp.png"))


class EventCandidateEvidenceTests(unittest.TestCase):
    def _reconciliation_fixture(self, root: Path) -> dict[str, object]:
        media = root / "source.mp4"
        evidence = root / "reconciliation.json"
        media.write_bytes(b"source")
        evidence.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": scan.RECONCILIATION_KIND,
                }
            ),
            encoding="utf-8",
        )
        bound_paths = {}
        bound_records = {}
        for name, payload in (
            ("legacy_full_oracle", b"legacy"),
            ("source_decode", b"decode"),
            ("generation_manifest", b"generation"),
        ):
            path = root / f"{name}.json"
            path.write_bytes(payload)
            bound_paths[name] = path
            bound_records[name] = scan.file_record(path)
        source_record = scan.file_record(media)
        return {
            "media": media,
            "evidence": evidence,
            "bound_paths": bound_paths,
            "verified": {
                "record": scan.file_record(evidence),
                "source_media": {
                    "path": str(media.resolve()),
                    "sha256": source_record["sha256"],
                    "size": source_record["size"],
                    "mtime_ns": media.stat().st_mtime_ns,
                    "current_file_verified": False,
                },
                "frame_count": 29804,
                "conflict_indices": [3, 4, 5, 7],
                "bound_evidence": bound_records,
                "time_authority": "none",
                "gate_approval": False,
            },
        }

    def test_legacy_report_schemas_v1_and_v2_are_rejected(self) -> None:
        for legacy_version in (1, 2):
            with self.subTest(schema_version=legacy_version), self.assertRaisesRegex(
                scan.EventScanError, "schema_version"
            ):
                scan.validate_scan_report_schema(
                    {
                        "schema_version": legacy_version,
                        "kind": scan.MANIFEST_KIND,
                        "time_basis": {"media_pts_authority": "none"},
                    }
                )

    def test_v3_header_cannot_relabel_legacy_time_fields(self) -> None:
        report = {
            "schema_version": scan.SCHEMA_VERSION,
            "kind": scan.MANIFEST_KIND,
            "scope": "bounded_source_seek_request",
            "time_basis": {
                "media_pts_authority": "none",
                "can_register_source_anchor_directly": False,
            },
            "window": {
                "requested_start_seconds": 174.0,
                "requested_duration_seconds": 10.0,
                "requested_end_seconds_exclusive": 184.0,
            },
            "source_oracle": {
                "provides_requested_time_mapping": False,
                "time_authority": "none",
            },
            "candidates": [
                {
                    "window_time_seconds": 0.1,
                    "source_time_seconds": 174.1,
                }
            ],
        }
        with self.assertRaisesRegex(scan.EventScanError, "legacy time field"):
            scan.validate_scan_report_schema(report)

    def test_negative_window_start_is_rejected_before_decode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "source.mp4"
            ffmpeg = root / "ffmpeg.exe"
            media.write_bytes(b"source")
            ffmpeg.write_bytes(b"ffmpeg")
            with mock.patch.object(scan, "_run_bytes") as run:
                with self.assertRaises(scan.EventScanError):
                    scan.scan_window(
                        media,
                        root / "scan.json",
                        ffmpeg=ffmpeg,
                        start_seconds=-0.001,
                    )
            run.assert_not_called()

    def test_duration_limit_is_enforced_before_decode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "source.mp4"
            ffmpeg = root / "ffmpeg.exe"
            media.write_bytes(b"source")
            ffmpeg.write_bytes(b"ffmpeg")
            with mock.patch.object(scan, "_run_bytes") as run:
                with self.assertRaises(scan.EventScanError):
                    scan.scan_window(
                        media,
                        root / "scan.json",
                        ffmpeg=ffmpeg,
                        duration_seconds=scan.MAX_SCAN_SECONDS + 0.001,
                    )
            run.assert_not_called()

    def test_unbound_oracle_is_rejected_before_decode_or_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "source.mp4"
            ffmpeg = root / "ffmpeg.exe"
            oracle = root / "source.oracle.json"
            media.write_bytes(b"source")
            ffmpeg.write_bytes(b"ffmpeg")
            oracle.write_text(
                json.dumps(
                    {
                        **valid_oracle(media),
                        "video": {
                            "size": media.stat().st_size,
                            "sha256": "0" * 64,
                        },
                    }
                ),
                encoding="utf-8",
            )
            output = root / "scan.json"
            contact = root / "contact.png"
            with mock.patch.object(scan, "_run_bytes") as run:
                with self.assertRaises(scan.EventScanError):
                    scan.scan_window(
                        media,
                        output,
                        ffmpeg=ffmpeg,
                        source_oracle=oracle,
                        contact_sheet=contact,
                    )
            run.assert_not_called()
            self.assertFalse(output.exists())
            self.assertFalse(contact.exists())

    def test_oracle_schema_and_scope_are_strict_identity_only_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "source.mp4"
            media.write_bytes(b"source")
            source_record = scan.file_record(media)

            accepted_path = root / "accepted.json"
            accepted_path.write_text(
                json.dumps(valid_oracle(media)), encoding="utf-8"
            )
            _, binding = scan._load_source_oracle_binding(
                accepted_path, source_record=source_record
            )
            self.assertEqual(binding["scan_scope"], "partial")
            self.assertFalse(binding["full_scan"])
            self.assertEqual(
                binding["binding_purpose"],
                "source_media_identity_and_oracle_provenance_only",
            )
            self.assertFalse(binding["provides_requested_time_mapping"])
            self.assertEqual(binding["time_authority"], "none")

            complete_path = root / "complete.json"
            complete_path.write_text(
                json.dumps(valid_oracle(media, scope="complete")),
                encoding="utf-8",
            )
            _, complete_binding = scan._load_source_oracle_binding(
                complete_path, source_record=source_record
            )
            self.assertEqual(complete_binding["scan_scope"], "complete")
            self.assertTrue(complete_binding["full_scan"])

            cases = {}
            wrong_schema = copy.deepcopy(valid_oracle(media))
            wrong_schema["schema_version"] = 2
            cases["schema_version"] = wrong_schema
            wrong_kind = copy.deepcopy(valid_oracle(media))
            wrong_kind["kind"] = "other"
            cases["kind"] = wrong_kind
            wrong_size = copy.deepcopy(valid_oracle(media))
            wrong_size["video"]["size"] += 1
            cases["hash and size"] = wrong_size
            failed_decode = copy.deepcopy(valid_oracle(media))
            failed_decode["ffmpeg"]["returncode"] = 1
            cases["returncode"] = failed_decode
            boolean_returncode = copy.deepcopy(valid_oracle(media))
            boolean_returncode["ffmpeg"]["returncode"] = False
            cases["ffmpeg.returncode"] = boolean_returncode
            bad_table = copy.deepcopy(valid_oracle(media))
            bad_table["pts_table"] = "not-a-list"
            cases["pts_table"] = bad_table
            inconsistent_count = copy.deepcopy(valid_oracle(media))
            inconsistent_count["showinfo"]["parsed_frames"] = 1
            cases["parsed_frames"] = inconsistent_count
            complete_with_limit = copy.deepcopy(valid_oracle(media))
            complete_with_limit["pts_conflict_assessment"]["scan_scope"] = "complete"
            cases["complete"] = complete_with_limit
            partial_without_limit = valid_oracle(media, scope="complete")
            partial_without_limit["pts_conflict_assessment"]["scan_scope"] = "partial"
            cases["partial"] = partial_without_limit

            for index, (message, payload) in enumerate(cases.items()):
                with self.subTest(message=message):
                    path = root / f"invalid-{index}.json"
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaisesRegex(scan.EventScanError, message):
                        scan._load_source_oracle_binding(
                            path, source_record=source_record
                        )

    def test_reconciliation_is_accepted_only_as_recomputed_identity_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self._reconciliation_fixture(root)
            media = fixture["media"]
            evidence = fixture["evidence"]
            source_record = scan.file_record(media)
            with mock.patch.object(
                scan.reconciliation_verifier,
                "verify_reconciliation_evidence",
                return_value=fixture["verified"],
            ) as verifier:
                _, binding = scan._load_source_oracle_binding(
                    evidence, source_record=source_record
                )
            verifier.assert_called_once_with(evidence.resolve())
            self.assertEqual(binding["kind"], scan.RECONCILIATION_KIND)
            self.assertEqual(binding["scan_scope"], "complete")
            self.assertTrue(binding["full_scan"])
            self.assertEqual(binding["parsed_frames"], 29804)
            self.assertEqual(binding["conflict_indices"], [3, 4, 5, 7])
            self.assertFalse(binding["provides_requested_time_mapping"])
            self.assertEqual(binding["time_authority"], "none")
            self.assertFalse(binding["gate_approval"])

    def test_reconciliation_record_change_is_rejected_before_decode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self._reconciliation_fixture(root)
            verified = copy.deepcopy(fixture["verified"])
            verified["record"]["sha256"] = "0" * 64
            with mock.patch.object(
                scan.reconciliation_verifier,
                "verify_reconciliation_evidence",
                return_value=verified,
            ), mock.patch.object(scan, "_run_bytes") as run:
                with self.assertRaises(scan.EventScanError):
                    scan._load_source_oracle_binding(
                        fixture["evidence"],
                        source_record=scan.file_record(fixture["media"]),
                    )
            run.assert_not_called()

    def test_reconciliation_bound_evidence_mutation_blocks_publication(self) -> None:
        for name in (
            "legacy_full_oracle",
            "source_decode",
            "generation_manifest",
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixture = self._reconciliation_fixture(root)
                ffmpeg = root / "ffmpeg.exe"
                output = root / "scan.json"
                ffmpeg.write_bytes(b"ffmpeg")
                frame_bytes = scan.CONTACT_WIDTH * scan.CONTACT_HEIGHT * 3

                def run_audio(command, **_kwargs):
                    fixture["bound_paths"][name].write_bytes(b"changed")
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout=struct.pack(
                            f"<{scan.AUDIO_WINDOW_SAMPLES}h",
                            *([0] * scan.AUDIO_WINDOW_SAMPLES),
                        ),
                        stderr=b"",
                    )

                with mock.patch.object(
                    scan.reconciliation_verifier,
                    "verify_reconciliation_evidence",
                    return_value=fixture["verified"],
                ), mock.patch.object(
                    scan, "ffmpeg_record", side_effect=fake_ffmpeg_record
                ), mock.patch.object(
                    scan, "_run_bytes", return_value=bytes(frame_bytes)
                ), mock.patch.object(
                    scan.subprocess, "run", side_effect=run_audio
                ):
                    with self.assertRaisesRegex(
                        scan.EventScanError, f"reconciliation.{name} identity changed"
                    ):
                        scan.scan_window(
                            fixture["media"],
                            output,
                            ffmpeg=ffmpeg,
                            source_oracle=fixture["evidence"],
                        )
                self.assertFalse(output.exists())

    def test_reconciliation_checked_list_includes_transitive_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self._reconciliation_fixture(root)
            ffmpeg = root / "ffmpeg.exe"
            output = root / "scan.json"
            ffmpeg.write_bytes(b"ffmpeg")
            frame_bytes = scan.CONTACT_WIDTH * scan.CONTACT_HEIGHT * 3
            audio = subprocess.CompletedProcess(
                [],
                0,
                stdout=struct.pack(
                    f"<{scan.AUDIO_WINDOW_SAMPLES}h",
                    *([0] * scan.AUDIO_WINDOW_SAMPLES),
                ),
                stderr=b"",
            )
            with mock.patch.object(
                scan.reconciliation_verifier,
                "verify_reconciliation_evidence",
                return_value=fixture["verified"],
            ), mock.patch.object(
                scan, "ffmpeg_record", side_effect=fake_ffmpeg_record
            ), mock.patch.object(
                scan, "_run_bytes", return_value=bytes(frame_bytes)
            ), mock.patch.object(scan.subprocess, "run", return_value=audio):
                result = scan.scan_window(
                    fixture["media"],
                    output,
                    ffmpeg=ffmpeg,
                    source_oracle=fixture["evidence"],
                )
            self.assertEqual(
                result["publication_identity_check"]["checked"],
                [
                    "source",
                    "ffmpeg",
                    "scanner",
                    "source_oracle",
                    "reconciliation.legacy_full_oracle",
                    "reconciliation.source_decode",
                    "reconciliation.generation_manifest",
                ],
            )

    def test_contact_publish_rechecks_reconciliation_bound_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self._reconciliation_fixture(root)
            ffmpeg = root / "ffmpeg.exe"
            output = root / "scan.json"
            contact = root / "contact.png"
            ffmpeg.write_bytes(b"ffmpeg")
            frame_bytes = scan.CONTACT_WIDTH * scan.CONTACT_HEIGHT * 3

            def run(command, **_kwargs):
                if "image2" in command:
                    Path(command[-1]).write_bytes(b"png")
                    fixture["bound_paths"]["source_decode"].write_bytes(b"changed")
                    stdout = b""
                elif "rawvideo" in command:
                    stdout = bytes(frame_bytes)
                else:
                    stdout = struct.pack(
                        f"<{scan.AUDIO_WINDOW_SAMPLES}h",
                        *([0] * scan.AUDIO_WINDOW_SAMPLES),
                    )
                return subprocess.CompletedProcess(command, 0, stdout, b"")

            with mock.patch.object(
                scan.reconciliation_verifier,
                "verify_reconciliation_evidence",
                return_value=fixture["verified"],
            ), mock.patch.object(
                scan, "ffmpeg_record", side_effect=fake_ffmpeg_record
            ), mock.patch.object(scan.subprocess, "run", side_effect=run):
                with self.assertRaisesRegex(
                    scan.EventScanError,
                    "reconciliation.source_decode identity changed",
                ):
                    scan.scan_window(
                        fixture["media"],
                        output,
                        ffmpeg=ffmpeg,
                        source_oracle=fixture["evidence"],
                        contact_sheet=contact,
                    )
            self.assertFalse(contact.exists())
            self.assertFalse(output.exists())

    def test_manifest_publish_failure_removes_its_contact_sheet(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self._reconciliation_fixture(root)
            ffmpeg = root / "ffmpeg.exe"
            output = root / "scan.json"
            contact = root / "contact.png"
            ffmpeg.write_bytes(b"ffmpeg")
            frame_bytes = scan.CONTACT_WIDTH * scan.CONTACT_HEIGHT * 3

            def run(command, **_kwargs):
                if "image2" in command:
                    Path(command[-1]).write_bytes(b"png")
                    stdout = b""
                elif "rawvideo" in command:
                    stdout = bytes(frame_bytes)
                else:
                    stdout = struct.pack(
                        f"<{scan.AUDIO_WINDOW_SAMPLES}h",
                        *([0] * scan.AUDIO_WINDOW_SAMPLES),
                    )
                return subprocess.CompletedProcess(command, 0, stdout, b"")

            original_write = scan.write_json_new

            def fail_before_publish(path, value, *, before_publish=None):
                def mutate_then_check():
                    fixture["bound_paths"]["generation_manifest"].write_bytes(
                        b"changed"
                    )
                    if before_publish is not None:
                        before_publish()

                return original_write(
                    path,
                    value,
                    before_publish=mutate_then_check,
                )

            with mock.patch.object(
                scan.reconciliation_verifier,
                "verify_reconciliation_evidence",
                return_value=fixture["verified"],
            ), mock.patch.object(
                scan, "ffmpeg_record", side_effect=fake_ffmpeg_record
            ), mock.patch.object(
                scan.subprocess, "run", side_effect=run
            ), mock.patch.object(
                scan, "write_json_new", side_effect=fail_before_publish
            ):
                with self.assertRaisesRegex(
                    scan.EventScanError,
                    "reconciliation.generation_manifest identity changed",
                ):
                    scan.scan_window(
                        fixture["media"],
                        output,
                        ffmpeg=ffmpeg,
                        source_oracle=fixture["evidence"],
                        contact_sheet=contact,
                    )
            self.assertFalse(contact.exists())
            self.assertFalse(output.exists())

    def test_reconciliation_verification_failure_blocks_before_decode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "source.mp4"
            evidence = root / "reconciliation.json"
            media.write_bytes(b"source")
            evidence.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "kind": scan.RECONCILIATION_KIND,
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(
                scan.reconciliation_verifier,
                "verify_reconciliation_evidence",
                side_effect=scan.reconciliation_verifier.LegacyOracleAdapterError(
                    "tampered"
                ),
            ), self.assertRaisesRegex(scan.EventScanError, "verification failed"):
                scan._load_source_oracle_binding(
                    evidence, source_record=scan.file_record(media)
                )

    def test_scan_window_records_all_commands_and_requested_time_basis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "source.mp4"
            ffmpeg = root / "ffmpeg.exe"
            output = root / "scan.json"
            contact = root / "contact.png"
            media.write_bytes(b"source")
            ffmpeg.write_bytes(b"ffmpeg")
            oracle = root / "source-oracle.json"
            oracle.write_text(json.dumps(valid_oracle(media)), encoding="utf-8")
            frame_bytes = scan.CONTACT_WIDTH * scan.CONTACT_HEIGHT * 3

            def run(command, **_kwargs):
                if "rawvideo" in command:
                    stdout = bytes(frame_bytes)
                elif "s16le" in command:
                    stdout = struct.pack(
                        f"<{scan.AUDIO_WINDOW_SAMPLES}h",
                        *([0] * scan.AUDIO_WINDOW_SAMPLES),
                    )
                elif "image2" in command:
                    Path(command[-1]).write_bytes(b"png")
                    stdout = b""
                else:
                    self.fail(f"unexpected command: {command}")
                return subprocess.CompletedProcess(
                    command, 0, stdout=stdout, stderr=b""
                )

            with mock.patch.object(
                scan, "ffmpeg_record", side_effect=fake_ffmpeg_record
            ), mock.patch.object(scan.subprocess, "run", side_effect=run) as runner:
                result = scan.scan_window(
                    media,
                    output,
                    ffmpeg=ffmpeg,
                    start_seconds=174.0,
                    duration_seconds=1.0,
                    sample_fps=1.0,
                    contact_sheet=contact,
                    source_oracle=oracle,
                )

            actual_commands = [call.args[0] for call in runner.call_args_list]
            self.assertEqual(len(actual_commands), 3)
            self.assertEqual(result["tools"]["commands"]["video"], actual_commands[0])
            self.assertEqual(result["tools"]["commands"]["audio"], actual_commands[1])
            self.assertEqual(
                result["tools"]["commands"]["contact_sheet"], actual_commands[2]
            )
            for command in actual_commands:
                self.assertEqual(command[command.index("-ss") + 1], "174.000000")
                self.assertEqual(command[command.index("-t") + 1], "1.000000")
            self.assertEqual(result["time_basis"]["media_pts_authority"], "none")
            self.assertFalse(
                result["time_basis"]["can_register_source_anchor_directly"]
            )
            self.assertEqual(result["time_basis"]["requested_seek_seconds"], 174.0)
            self.assertFalse(
                result["source_oracle"]["provides_requested_time_mapping"]
            )
            self.assertEqual(
                result["source_oracle"]["binding_purpose"],
                "source_media_identity_and_oracle_provenance_only",
            )
            self.assertEqual(
                result["publication_identity_check"]["checked"],
                ["source", "ffmpeg", "scanner", "source_oracle"],
            )
            self.assertEqual(
                result["publication_identity_check"]["status"], "PASSED"
            )
            self.assertTrue(output.is_file())
            self.assertTrue(contact.is_file())
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), result)

    def test_only_known_missing_stream_is_classified_as_no_audio(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "source.mp4"
            ffmpeg = root / "ffmpeg.exe"
            output = root / "scan.json"
            media.write_bytes(b"source")
            ffmpeg.write_bytes(b"ffmpeg")
            frame_bytes = scan.CONTACT_WIDTH * scan.CONTACT_HEIGHT * 3
            missing = subprocess.CompletedProcess(
                [],
                1,
                stdout=b"",
                stderr=b"Stream map '0:a:0' matches no streams.",
            )
            with mock.patch.object(
                scan, "ffmpeg_record", side_effect=fake_ffmpeg_record
            ), mock.patch.object(
                scan, "_run_bytes", return_value=bytes(frame_bytes)
            ), mock.patch.object(scan.subprocess, "run", return_value=missing):
                result = scan.scan_window(media, output, ffmpeg=ffmpeg)

            self.assertEqual(result["audio_sampling"]["status"], "NO_AUDIO_STREAM")
            self.assertFalse(result["audio_sampling"]["stream_present"])
            self.assertIn("NO_AUDIO_STREAM", result["reason_codes"])

    def test_audio_decode_failure_blocks_without_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "source.mp4"
            ffmpeg = root / "ffmpeg.exe"
            output = root / "scan.json"
            media.write_bytes(b"source")
            ffmpeg.write_bytes(b"ffmpeg")
            frame_bytes = scan.CONTACT_WIDTH * scan.CONTACT_HEIGHT * 3
            failed = subprocess.CompletedProcess(
                [],
                1,
                stdout=b"",
                stderr=b"Error while decoding stream #0:1",
            )
            with mock.patch.object(
                scan, "ffmpeg_record", side_effect=fake_ffmpeg_record
            ), mock.patch.object(
                scan, "_run_bytes", return_value=bytes(frame_bytes)
            ), mock.patch.object(scan.subprocess, "run", return_value=failed):
                with self.assertRaisesRegex(
                    scan.EventScanError, "bounded audio scan failed"
                ):
                    scan.scan_window(media, output, ffmpeg=ffmpeg)
            self.assertFalse(output.exists())

    def test_input_mutation_blocks_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "source.mp4"
            ffmpeg = root / "ffmpeg.exe"
            output = root / "scan.json"
            media.write_bytes(b"source")
            ffmpeg.write_bytes(b"ffmpeg")
            frame_bytes = scan.CONTACT_WIDTH * scan.CONTACT_HEIGHT * 3

            def run_audio(command, **_kwargs):
                media.write_bytes(b"changed source")
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=struct.pack(
                        f"<{scan.AUDIO_WINDOW_SAMPLES}h",
                        *([0] * scan.AUDIO_WINDOW_SAMPLES),
                    ),
                    stderr=b"",
                )

            with mock.patch.object(
                scan, "ffmpeg_record", side_effect=fake_ffmpeg_record
            ), mock.patch.object(
                scan, "_run_bytes", return_value=bytes(frame_bytes)
            ), mock.patch.object(scan.subprocess, "run", side_effect=run_audio):
                with self.assertRaisesRegex(
                    scan.EventScanError, "source identity changed"
                ):
                    scan.scan_window(media, output, ffmpeg=ffmpeg)
            self.assertFalse(output.exists())

    def test_write_once_result_rejects_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "scan.json"
            scan.write_json_new(output, {"value": 1})
            with self.assertRaises(FileExistsError):
                scan.write_json_new(output, {"value": 2})
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), {"value": 1})

    def test_write_once_rechecks_immediately_before_publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "scan.json"
            with self.assertRaisesRegex(RuntimeError, "changed"):
                scan.write_json_new(
                    output,
                    {"value": 1},
                    before_publish=lambda: (_ for _ in ()).throw(
                        RuntimeError("changed")
                    ),
                )
            self.assertFalse(output.exists())
            self.assertEqual(list(output.parent.glob("*.tmp.json.tmp")), [])


if __name__ == "__main__":
    unittest.main()
