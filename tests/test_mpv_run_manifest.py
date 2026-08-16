from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "scripts" / "spike_mpv.py"
SPEC = importlib.util.spec_from_file_location("spike_mpv_run_manifest", MODULE_PATH)
assert SPEC and SPEC.loader
spike_mpv = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(spike_mpv)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_baseline(root: Path, *, full_video_hash: bool = True) -> Path:
    path = root / "baseline.json"
    video_path = root / "sample.mp4"
    video_path.write_bytes(b"synthetic video bytes")
    meta_path = root / "meta.json"
    meta_path.write_text(
        json.dumps({"video": str(video_path)}), encoding="utf-8"
    )
    video = {
        "path": str(video_path),
        "exists": True,
        "size": video_path.stat().st_size,
        "mtime_ns": video_path.stat().st_mtime_ns,
    }
    if full_video_hash:
        video["sha256"] = _sha256(video_path)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "mpv_phase0_baseline",
                "samples": [
                    {
                        "stem": "sample",
                        "video": video,
                        "artifacts": [
                            {
                                "path": str(meta_path),
                                "exists": True,
                                "sha256": _sha256(meta_path),
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_thresholds(root: Path, command: str) -> Path:
    path = root / f"{command}_thresholds.json"
    values = {
        "env": {
            "require_runtime_status": "pass",
            "require_supply_chain_status": "pass",
        },
        "stress": {
            "seek_failures_max": 0,
            "duration_error_frames_max": 1.0,
            "load_seconds_max": 10.0,
            "seek_p95_ms_max": 300.0,
            "seek_max_ms_max": 1000.0,
        },
        "lifecycle": {
            "repeat_min": 50,
            "failures_max": 0,
            "handle_growth_max": 0,
            "thread_growth_max": 0,
        },
        "wid": {
            "switch_loads_min": 20,
            "load_failures_max": 0,
            "load_p95_ms_max": 300.0,
            "load_max_ms_max": 1000.0,
            "keyboard_events_min": 1,
            "queued_errors_max": 0,
        },
    }[command]
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "mpv_phase0_thresholds",
                "command": command,
                "thresholds": values,
            }
        ),
        encoding="utf-8",
    )
    return path


def _args(
    root: Path,
    command: str,
    *,
    baseline: Path | None,
    thresholds: Path | None,
) -> argparse.Namespace:
    meta = root / "meta.json"
    if not meta.exists():
        video = root / "sample.mp4"
        video.write_bytes(b"synthetic video bytes")
        meta.write_text(json.dumps({"video": str(video)}), encoding="utf-8")
    return argparse.Namespace(
        command=command,
        baseline_manifest=baseline,
        threshold_manifest=thresholds,
        run_manifest=root / "run.json",
        meta=meta,
        provenance_manifest=None,
        output=root / "result.json",
        timeout=10.0,
        func=lambda _args: 0,
    )


class RunManifestTests(unittest.TestCase):
    def test_incremental_baseline_reuses_bound_video_hash_without_reading_video(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            prior = _write_baseline(root)
            meta = root / "meta.json"
            output = root / "incremental.json"
            expected_sha = json.loads(prior.read_text(encoding="utf-8"))["samples"][0]["video"]["sha256"]

            with mock.patch.object(
                spike_mpv,
                "_sha256_file",
                wraps=spike_mpv._sha256_file,
            ) as hash_file:
                manifest = spike_mpv.capture_baseline(
                    [meta],
                    output,
                    full_video_hash=False,
                    reuse_video_hashes_from=prior,
                )

            video_path = (root / "sample.mp4").resolve()
            hashed_paths = [call.args[0].resolve() for call in hash_file.call_args_list]
            self.assertNotIn(video_path, hashed_paths)
            self.assertEqual(manifest["samples"][0]["video"]["sha256"], expected_sha)
            self.assertEqual(
                manifest["samples"][0]["video"]["sha256_source"]["kind"],
                "reused_verified_baseline",
            )
            self.assertEqual(manifest["baseline_completeness"]["status"], "BLOCKED")

    def test_incremental_baseline_rejects_changed_video_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            prior = _write_baseline(root)
            meta = root / "meta.json"
            video = root / "sample.mp4"
            video.write_bytes(b"changed length")

            with self.assertRaisesRegex(ValueError, "video size changed"):
                spike_mpv.capture_baseline(
                    [meta],
                    root / "incremental.json",
                    full_video_hash=False,
                    reuse_video_hashes_from=prior,
                )

    def test_write_once_manifest_binds_inputs_verifiers_command_and_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            baseline = _write_baseline(root)
            thresholds = _write_thresholds(root, "stress")
            artifact = root / "sample.edl"
            artifact.write_text("# mpv EDL v0\n", encoding="utf-8")
            args = _args(
                root,
                "stress",
                baseline=baseline,
                thresholds=thresholds,
            )

            path, manifest = spike_mpv.create_run_manifest(
                "stress", args, args.output, artifacts=(artifact,)
            )

            self.assertEqual(path, args.run_manifest.resolve())
            self.assertEqual(manifest["status"], "pass")
            self.assertEqual(manifest["reason_codes"], [])
            self.assertEqual(manifest["command"]["name"], "stress")
            self.assertNotIn("func", manifest["command"]["parameters"])
            self.assertNotIn("run_manifest", manifest["command"]["parameters"])
            self.assertEqual(
                manifest["expected_result"]["path"],
                str(args.output.resolve()),
            )
            self.assertEqual(
                manifest["inputs"]["baseline_manifest"]["sha256"],
                _sha256(baseline),
            )
            self.assertEqual(
                manifest["inputs"]["threshold_manifest"]["sha256"],
                _sha256(thresholds),
            )
            self.assertEqual(
                manifest["inputs"]["artifacts"][0]["sha256"],
                _sha256(artifact),
            )
            self.assertTrue(all(item.get("sha256") for item in manifest["verifiers"]))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), manifest)

            with self.assertRaisesRegex(FileExistsError, "write-once"):
                spike_mpv.create_run_manifest(
                    "stress", args, args.output, artifacts=(artifact,)
                )

            args.run_manifest = args.output
            with self.assertRaisesRegex(ValueError, "must differ"):
                spike_mpv.create_run_manifest("stress", args, args.output)

    def test_missing_or_incomplete_evidence_keeps_binding_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            incomplete = _write_baseline(root, full_video_hash=False)
            wrong_thresholds = _write_thresholds(root, "wid")
            args = _args(
                root,
                "stress",
                baseline=incomplete,
                thresholds=wrong_thresholds,
            )

            _, manifest = spike_mpv.create_run_manifest(
                "stress", args, args.output
            )

            self.assertEqual(manifest["status"], "blocked")
            self.assertIn("BASELINE_VIDEO_HASH_MISSING", manifest["reason_codes"])
            self.assertIn("THRESHOLD_COMMAND_MISMATCH", manifest["reason_codes"])

    def test_baseline_rejects_artifact_or_video_changed_after_capture(self) -> None:
        for changed, expected in (
            ("meta", "BASELINE_ARTIFACT_HASH_MISMATCH"),
            ("video", "BASELINE_VIDEO_HASH_MISMATCH"),
        ):
            with self.subTest(changed=changed):
                with tempfile.TemporaryDirectory() as temp_value:
                    root = Path(temp_value)
                    baseline = _write_baseline(root)
                    thresholds = _write_thresholds(root, "stress")
                    args = _args(
                        root,
                        "stress",
                        baseline=baseline,
                        thresholds=thresholds,
                    )
                    target = root / (
                        "meta.json" if changed == "meta" else "sample.mp4"
                    )
                    if changed == "meta":
                        target.write_text(
                            json.dumps({"video": str(root / "sample.mp4"), "changed": True}),
                            encoding="utf-8",
                        )
                    else:
                        target.write_bytes(b"changed video bytes")

                    _, manifest = spike_mpv.create_run_manifest(
                        "stress", args, args.output
                    )

                    self.assertEqual(manifest["status"], "blocked")
                    self.assertIn(expected, manifest["reason_codes"])

    def test_report_references_manifest_hash_and_cannot_pass_incomplete_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            args = _args(root, "env", baseline=None, thresholds=None)
            path, manifest = spike_mpv.create_run_manifest("env", args, args.output)
            report = {"status": "pass", "reason_codes": []}

            spike_mpv._attach_run_manifest(report, path, manifest)

            self.assertEqual(report["status"], "blocked")
            self.assertEqual(report["run_binding_status"], "blocked")
            self.assertEqual(report["run_manifest"]["sha256"], _sha256(path))
            self.assertIn("RUN_MANIFEST_INCOMPLETE", report["reason_codes"])

    def test_runtime_parsers_accept_all_binding_arguments(self) -> None:
        parser = spike_mpv._parser()
        common = [
            "--baseline-manifest", "baseline.json",
            "--threshold-manifest", "thresholds.json",
            "--run-manifest", "run.json",
        ]
        cases = (
            ["env", *common],
            ["stress", "--meta", "meta.json", "--mpv-dir", "mpv", *common],
            ["lifecycle", "--meta", "meta.json", "--mpv-dir", "mpv", *common],
            ["wid", "--meta", "meta.json", "--mpv-dir", "mpv", *common],
        )

        for argv in cases:
            with self.subTest(command=argv[0]):
                parsed = parser.parse_args(argv)
                self.assertEqual(parsed.baseline_manifest, Path("baseline.json"))
                self.assertEqual(parsed.threshold_manifest, Path("thresholds.json"))
                self.assertEqual(parsed.run_manifest, Path("run.json"))

    def test_registered_thresholds_are_applied_to_the_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            baseline = _write_baseline(root)
            thresholds = _write_thresholds(root, "stress")
            args = _args(
                root,
                "stress",
                baseline=baseline,
                thresholds=thresholds,
            )
            path, manifest = spike_mpv.create_run_manifest(
                "stress", args, args.output
            )
            report = {
                "status": "pass",
                "reason_codes": [],
                "seek_failures": [],
                "duration_error_frames": 0.1,
                "load_seconds": 0.2,
                "seek_latency": {"p95_ms": 250.0, "max_ms": 1200.0},
            }

            spike_mpv._attach_run_manifest(report, path, manifest)

            evaluation = report["preregistered_threshold_evaluation"]
            self.assertEqual(evaluation["status"], "fail")
            self.assertEqual(evaluation["checks"]["seek_max_ms_max"]["status"], "fail")
            self.assertEqual(report["status"], "fail")
            self.assertIn(
                "PREREGISTERED_THRESHOLD_EXCEEDED", report["reason_codes"]
            )

    def test_env_report_is_cryptographically_bound_before_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            baseline = _write_baseline(root)
            thresholds = _write_thresholds(root, "env")
            args = _args(root, "env", baseline=baseline, thresholds=thresholds)
            args.mpv_dir = None
            args.provenance_manifest = None
            args.source_url = None
            args.license_note = None

            with mock.patch.object(
                spike_mpv,
                "probe_mpv",
                return_value=(
                    {
                        "status": "pass",
                        "runtime_status": "pass",
                        "supply_chain_status": "pass",
                        "reason_codes": [],
                    },
                    object(),
                ),
            ):
                exit_code = spike_mpv._cmd_env(args)

            report = json.loads(args.output.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 0)
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["run_binding_status"], "pass")
            self.assertEqual(
                report["preregistered_threshold_evaluation"]["status"], "pass"
            )
            self.assertEqual(report["run_manifest"]["sha256"], _sha256(args.run_manifest))


if __name__ == "__main__":
    unittest.main()
