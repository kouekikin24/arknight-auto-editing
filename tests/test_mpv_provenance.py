from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "scripts" / "spike_mpv.py"
SPEC = importlib.util.spec_from_file_location("spike_mpv_provenance", MODULE_PATH)
assert SPEC and SPEC.loader
spike_mpv = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(spike_mpv)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_evidence(root: Path) -> tuple[Path, dict[str, object]]:
    files = {
        "archive": root / "mpv-source.7z",
        "dll": root / "libmpv-2.dll",
        "build": root / "build-record.json",
        "license": root / "LICENSE.txt",
        "redistribution": root / "REDISTRIBUTION.txt",
    }
    for name, path in files.items():
        path.write_bytes(f"verified {name} evidence\n".encode("ascii"))
    manifest: dict[str, object] = {
        "schema_version": 1,
        "kind": "mpv_phase0_provenance",
        "source_url": "https://example.invalid/releases/mpv-source.7z",
        **{
            name: {"path": path.name, "sha256": _sha256(path)}
            for name, path in files.items()
        },
    }
    manifest_path = root / "provenance.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path, manifest


class _FakePlayer:
    mpv_version = "v-test"
    mpv_configuration = "test build"
    ffmpeg_version = "test ffmpeg"

    def __init__(self, **_options: object) -> None:
        pass

    def terminate(self) -> None:
        pass


_FAKE_MPV = SimpleNamespace(MPV=_FakePlayer, MPV_VERSION="2.5")


class ProvenanceManifestTests(unittest.TestCase):
    def test_all_local_evidence_files_are_required_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            manifest_path, _ = _write_evidence(root)

            manifest, loaded_path = spike_mpv._load_provenance_manifest(manifest_path)
            assert manifest is not None

            self.assertEqual(loaded_path, manifest_path.resolve())
            self.assertEqual(
                set(spike_mpv.verify_provenance_files(manifest)["checks"]),
                set(spike_mpv.PROVENANCE_FILE_SECTIONS),
            )
            self.assertEqual(
                spike_mpv.verify_provenance_files(manifest)["status"], "pass"
            )
            for name in spike_mpv.PROVENANCE_FILE_SECTIONS:
                self.assertTrue(Path(manifest[name]["path"]).is_absolute())

    def test_truthy_descriptions_cannot_replace_evidence_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            _, raw = _write_evidence(root)

            invalid_cases = []
            for missing in ("archive", "license", "redistribution"):
                value = dict(raw)
                value.pop(missing)
                invalid_cases.append((missing, value))

            build_string = dict(raw)
            build_string["build"] = {"configuration": "release x64"}
            invalid_cases.append(("build", build_string))

            for field, value in invalid_cases:
                with self.subTest(field=field):
                    with self.assertRaisesRegex(ValueError, rf"{field}\."):
                        spike_mpv.validate_provenance_manifest(value, base_dir=root)

    def test_missing_or_hash_mismatched_file_fails_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            manifest_path, _ = _write_evidence(root)
            manifest, _ = spike_mpv._load_provenance_manifest(manifest_path)
            assert manifest is not None

            Path(manifest["license"]["path"]).unlink()
            Path(manifest["build"]["path"]).write_text("changed", encoding="utf-8")
            verification = spike_mpv.verify_provenance_files(manifest)

            self.assertEqual(verification["status"], "fail")
            self.assertEqual(verification["checks"]["license"]["status"], "missing")
            self.assertEqual(verification["checks"]["build"]["status"], "fail")


class ProvenanceProbeTests(unittest.TestCase):
    def _probe(
        self,
        dll: Path,
        *,
        provenance: object = None,
        loaded: Path | None = None,
        source_url: str | None = None,
        license_note: str | None = None,
    ) -> tuple[dict[str, object], object | None]:
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(spike_mpv, "_wrapper_record", return_value={}))
            stack.enter_context(
                mock.patch.object(spike_mpv.importlib, "import_module", return_value=_FAKE_MPV)
            )
            stack.enter_context(
                mock.patch.object(spike_mpv, "_loaded_dll_path", return_value=loaded or dll)
            )
            stack.enter_context(mock.patch.dict(os.environ, {"PATH": "test-path"}))
            if hasattr(spike_mpv.os, "add_dll_directory"):
                stack.enter_context(
                    mock.patch.object(spike_mpv.os, "add_dll_directory", return_value=object())
                )
            return spike_mpv.probe_mpv(
                dll,
                provenance=provenance,
                source_url=source_url,
                license_note=license_note,
            )

    def test_truthy_legacy_strings_stay_blocked_but_runtime_still_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            dll = Path(temp_value) / "libmpv-2.dll"
            dll.write_bytes(b"runtime dll")

            report, module = self._probe(
                dll,
                source_url="https://example.invalid/mpv.7z",
                license_note="redistribution allowed",
            )

            self.assertIs(module, _FAKE_MPV)
            self.assertEqual(report["runtime_status"], "pass")
            self.assertEqual(report["supply_chain_status"], "blocked")
            self.assertEqual(report["status"], "blocked")
            self.assertIn("PROVENANCE_MANIFEST_MISSING", report["reason_codes"])

    def test_exact_loaded_dll_path_and_hash_can_pass_g0(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            manifest_path, _ = _write_evidence(root)
            dll = root / "libmpv-2.dll"

            report, module = self._probe(dll, provenance=manifest_path)

            self.assertIs(module, _FAKE_MPV)
            self.assertTrue(report["loaded_dll_matches_provenance_path"])
            self.assertTrue(report["loaded_dll_matches_provenance_sha256"])
            self.assertEqual(report["provenance_files"]["status"], "pass")
            self.assertEqual(report["supply_chain_status"], "pass")
            self.assertEqual(report["status"], "pass")

    def test_loaded_dll_path_mismatch_blocks_g0(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            manifest_path, _ = _write_evidence(root)
            dll = root / "libmpv-2.dll"
            other = root / "mpv-2.dll"
            other.write_bytes(dll.read_bytes())

            report, module = self._probe(
                dll, provenance=manifest_path, loaded=other
            )

            self.assertIs(module, _FAKE_MPV)
            self.assertFalse(report["loaded_dll_matches_provenance_path"])
            self.assertTrue(report["loaded_dll_matches_provenance_sha256"])
            self.assertEqual(report["status"], "blocked")
            self.assertIn("LOADED_DLL_PATH_MISMATCH", report["reason_codes"])

    def test_loaded_dll_hash_mismatch_blocks_g0(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            manifest_path, raw = _write_evidence(root)
            dll = root / "libmpv-2.dll"
            raw["dll"]["sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(raw), encoding="utf-8")

            report, module = self._probe(dll, provenance=manifest_path)

            self.assertIs(module, _FAKE_MPV)
            self.assertTrue(report["loaded_dll_matches_provenance_path"])
            self.assertFalse(report["loaded_dll_matches_provenance_sha256"])
            self.assertEqual(report["status"], "blocked")
            self.assertIn("LOADED_DLL_HASH_MISMATCH", report["reason_codes"])

    def test_missing_evidence_blocks_g0_without_suppressing_runtime_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            manifest_path, _ = _write_evidence(root)
            dll = root / "libmpv-2.dll"
            (root / "REDISTRIBUTION.txt").unlink()

            report, module = self._probe(dll, provenance=manifest_path)

            self.assertIs(module, _FAKE_MPV)
            self.assertEqual(report["runtime_status"], "pass")
            self.assertEqual(report["supply_chain_status"], "blocked")
            self.assertEqual(report["status"], "blocked")
            self.assertIn(
                "PROVENANCE_FILE_VERIFICATION_FAILED", report["reason_codes"]
            )


class ProvenanceCommandTests(unittest.TestCase):
    def test_all_runtime_commands_accept_manifest_argument(self) -> None:
        manifest = Path("evidence/provenance.json")
        cases = {
            "env": ["env"],
            "stress": ["stress", "--meta", "meta.json", "--mpv-dir", "mpv"],
            "lifecycle": [
                "lifecycle",
                "--meta",
                "meta.json",
                "--mpv-dir",
                "mpv",
            ],
            "wid": ["wid", "--meta", "meta.json", "--mpv-dir", "mpv"],
        }
        parser = spike_mpv._parser()

        for command, argv in cases.items():
            with self.subTest(command=command):
                args = parser.parse_args(
                    [*argv, "--provenance-manifest", str(manifest)]
                )
                self.assertEqual(args.provenance_manifest, manifest)

    def test_all_runtime_commands_propagate_manifest_to_probe(self) -> None:
        manifest = Path("evidence/provenance.json")
        blocked_environment = {"status": "blocked", "reason_codes": ["TEST"]}
        commands = (
            spike_mpv._cmd_env,
            spike_mpv._cmd_stress,
            spike_mpv._cmd_lifecycle,
            spike_mpv._cmd_wid,
        )

        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            for command in commands:
                with self.subTest(command=command.__name__):
                    args = argparse.Namespace(
                        output=root / f"{command.__name__}.json",
                        meta=root / "meta.json",
                        mpv_dir=root,
                        provenance_manifest=manifest,
                        source_url=None,
                        license_note=None,
                        repeat=1,
                        timeout=0.1,
                        geometry="320x240",
                        auto_seconds=0.0,
                        exercise_resize=False,
                        switch_cycles=0,
                        switch_interval_ms=100,
                    )
                    with (
                        mock.patch.object(
                            spike_mpv,
                            "build_edl",
                            return_value=(root / "test.edl", {"status": "blocked"}),
                        ),
                        mock.patch.object(
                            spike_mpv,
                            "probe_mpv",
                            return_value=(blocked_environment, None),
                        ) as probe,
                        mock.patch.object(spike_mpv, "_write_json"),
                        mock.patch("builtins.print"),
                    ):
                        self.assertEqual(command(args), 2)
                    probe.assert_called_once_with(
                        root,
                        provenance=manifest,
                        source_url=None,
                        license_note=None,
                    )


if __name__ == "__main__":
    unittest.main()
