#!/usr/bin/env python3
"""Isolated libmpv Phase 0 utilities.

This script deliberately does not import production playback modules. Commands that
do not need libmpv (baseline and build-edl) remain usable when the DLL is absent.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import importlib
import importlib.metadata
import json
import math
import os
import platform
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from timeline_plan import TimelinePlan


CACHE_ROOT = REPO / ".cache" / "mpv_spike"
DEFAULT_META_PATHS = tuple(
    REPO / ".cache" / "preview_fluency" / f"{stem}_meta.json"
    for stem in ("1", "2", "3", "4")
)
DLL_NAMES = ("mpv-1.dll", "mpv-2.dll", "libmpv-2.dll")
PTS_PROXY_MANIFEST_KINDS = {
    "mpv_phase0_pts_normalized_proxy",
    "mpv_phase0_prefix_audio_copy_manifest",
}
PTS_PROXY_VIDEO_REPORT_KINDS = {
    "mpv_phase0_pts_normalized_proxy_verification",
    "mpv_phase0_prefix_audio_copy_report",
}
PROVENANCE_SCHEMA_VERSION = 1
PROVENANCE_KINDS = {"mpv_phase0_provenance", "mpv_provenance"}
PROVENANCE_FILE_SECTIONS = ("archive", "dll", "build", "license", "redistribution")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
CORE_FILES = (
    "analyzer.py",
    "frame_types.py",
    "main.py",
    "preview_player.py",
    "settings_panel.py",
    "timeline_plan.py",
    "timeline_widget.py",
    "video_io.py",
    "pyproject.toml",
    "requirements.txt",
    "uv.lock",
    "edit_commands.py",
    "project_state.py",
    "task_manager.py",
)
_DLL_HANDLES: list[Any] = []


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def _write_json_new(path: Path, value: Any) -> None:
    """Create a JSON artifact once; never replace an existing run binding."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        value, ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise FileExistsError(
            f"run manifest already exists and is write-once: {path}"
        ) from exc


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _path_for_manifest(value: str | Path, *, base_dir: Path) -> Path:
    """Resolve a manifest path without consulting PATH or the current directory."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve(strict=False)


def _same_path(left: str | Path, right: str | Path) -> bool:
    """Compare absolute paths using the host's case rules (not just a directory)."""
    left_value = os.path.normcase(os.path.normpath(str(Path(left).resolve(strict=False))))
    right_value = os.path.normcase(os.path.normpath(str(Path(right).resolve(strict=False))))
    return left_value == right_value


def _sha256_value(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value.strip()):
        raise ValueError(f"{field} must be a 64-character SHA-256 hex string")
    return value.strip().lower()


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _section(raw: Mapping[str, Any], name: str, *, aliases: Sequence[str] = ()) -> dict[str, Any]:
    value: Any = raw.get(name)
    if value is None:
        for alias in aliases:
            if alias in raw:
                value = raw[alias]
                break
    if not isinstance(value, Mapping):
        raise ValueError(f"provenance manifest field {name!r} must be an object")
    return dict(value)


def validate_provenance_manifest(
    value: Mapping[str, Any],
    *,
    base_dir: Path = REPO,
) -> dict[str, Any]:
    """Validate and canonicalize the libmpv provenance contract.

    The manifest is intentionally stricter than the old ``source_url`` and
    ``license_note`` strings.  A usable G0 record must identify the exact DLL
    and four *local evidence files* (source archive, build record, license,
    and redistribution record), each with a declared SHA-256.  A descriptive
    build configuration string or license note is metadata only; neither can
    substitute for its evidence file.  Unknown fields are retained for future
    evidence. Relative paths are resolved relative to the manifest file's
    directory.
    """
    if not isinstance(value, Mapping):
        raise ValueError("provenance manifest must be a JSON object")
    raw = dict(value)
    try:
        schema_version = int(raw.get("schema_version"))
    except (TypeError, ValueError):
        raise ValueError("provenance manifest schema_version must be 1") from None
    if schema_version != PROVENANCE_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported provenance manifest schema_version: {schema_version}"
        )
    kind = _required_text(raw.get("kind"), "provenance manifest kind")
    if kind not in PROVENANCE_KINDS:
        raise ValueError(f"unsupported provenance manifest kind: {kind}")

    source_url = raw.get("source_url")
    source = raw.get("source")
    if source_url is None and isinstance(source, Mapping):
        source_url = source.get("url")
    source_url = _required_text(source_url, "source_url")
    if "://" not in source_url:
        raise ValueError("source_url must be an absolute URL")

    if raw.get("dll") is None and (
        "dll_path" in raw or "dll_sha256" in raw
    ):
        dll_raw = {
            "path": raw.get("dll_path"),
            "sha256": raw.get("dll_sha256"),
        }
    else:
        dll_raw = _section(raw, "dll", aliases=("dll_record",))
    dll_path = dll_raw.get("path", raw.get("dll_path"))
    dll_sha = dll_raw.get("sha256", raw.get("dll_sha256"))
    dll_path = _path_for_manifest(
        _required_text(dll_path, "dll.path"), base_dir=base_dir
    )
    if dll_path.name.lower() not in DLL_NAMES:
        raise ValueError(
            f"dll.path must name one of {', '.join(DLL_NAMES)}: {dll_path.name}"
        )
    dll_sha = _sha256_value(dll_sha, "dll.sha256")

    archive_value: Any = raw.get("archive")
    if archive_value is None and isinstance(source, Mapping):
        archive_value = source.get("archive")
    if archive_value is None:
        archive_value = {
            "path": raw.get("archive_path"),
            "sha256": raw.get("archive_sha256"),
        }
    if not isinstance(archive_value, Mapping):
        raise ValueError("provenance manifest archive must be an object")
    archive_raw = dict(archive_value)
    archive_sha = _sha256_value(
        archive_raw.get("sha256", raw.get("archive_sha256")),
        "archive.sha256",
    )
    archive_path = _path_for_manifest(
        _required_text(
            archive_raw.get("path", raw.get("archive_path")), "archive.path"
        ),
        base_dir=base_dir,
    )

    build_value: Any = raw.get("build")
    if build_value is None:
        # Keep the legacy alias only as a way to locate the evidence file. A
        # configuration string by itself is deliberately not accepted.
        build_value = {
            "path": raw.get("build_path"),
            "sha256": raw.get("build_sha256"),
            "configuration": raw.get("build_configuration"),
        }
    if not isinstance(build_value, Mapping):
        raise ValueError("provenance manifest build must be an object")
    build = dict(build_value)
    build_path = _path_for_manifest(
        _required_text(build.get("path", raw.get("build_path")), "build.path"),
        base_dir=base_dir,
    )
    build_sha = _sha256_value(
        build.get("sha256", raw.get("build_sha256")), "build.sha256"
    )
    configuration = build.get("configuration", build.get("config"))
    if configuration is not None:
        if isinstance(configuration, str):
            configuration = _required_text(configuration, "build.configuration")
        elif not isinstance(configuration, Mapping) or not configuration:
            raise ValueError(
                "build.configuration must be a non-empty string or object"
            )

    license_value: Any = raw.get("license")
    if license_value is None:
        license_value = {
            "path": raw.get("license_path"),
            "sha256": raw.get("license_sha256"),
            "note": raw.get("license_note"),
        }
    if not isinstance(license_value, Mapping):
        raise ValueError("provenance manifest license must be an object")
    license_raw = dict(license_value)
    license_path_value = license_raw.get("path", raw.get("license_path"))
    license_path = _path_for_manifest(
        _required_text(license_path_value, "license.path"), base_dir=base_dir
    )
    license_sha = _sha256_value(
        license_raw.get("sha256", raw.get("license_sha256")),
        "license.sha256",
    )
    license_note_value = license_raw.get("note", raw.get("license_note"))
    license_note = (
        _required_text(license_note_value, "license.note")
        if license_note_value is not None
        else None
    )

    redistribution_value: Any = raw.get("redistribution")
    if redistribution_value is None:
        redistribution_value = raw.get("redistribution_evidence")
    if redistribution_value is None:
        redistribution_value = {
            "path": raw.get("redistribution_path"),
            "sha256": raw.get("redistribution_sha256"),
        }
    if not isinstance(redistribution_value, Mapping):
        raise ValueError("provenance manifest redistribution must be an object")
    redistribution_raw = dict(redistribution_value)
    redistribution_path = _path_for_manifest(
        _required_text(
            redistribution_raw.get("path", raw.get("redistribution_path")),
            "redistribution.path",
        ),
        base_dir=base_dir,
    )
    redistribution_sha = _sha256_value(
        redistribution_raw.get(
            "sha256", raw.get("redistribution_sha256")
        ),
        "redistribution.sha256",
    )

    normalized: dict[str, Any] = {
        "schema_version": schema_version,
        "kind": kind,
        "source_url": source_url,
        "archive": {
            "sha256": archive_sha,
            "path": str(archive_path),
        },
        "dll": {"path": str(dll_path), "sha256": dll_sha},
        "build": {
            **build,
            "path": str(build_path),
            "sha256": build_sha,
            **({"configuration": configuration} if configuration is not None else {}),
        },
        "license": {
            "path": str(license_path),
            "sha256": license_sha,
            **({"note": license_note} if license_note is not None else {}),
        },
        "redistribution": {
            "path": str(redistribution_path),
            "sha256": redistribution_sha,
        },
    }
    # Keep non-contract evidence (for example compiler and dependency data), but
    # do not let aliases overwrite the canonical values above.
    for key, item in raw.items():
        if key not in normalized:
            normalized[key] = item
    return normalized


def _load_provenance_manifest(
    value: Path | str | Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, Path | None]:
    if value is None:
        return None, None
    manifest_path: Path | None = None
    if isinstance(value, (str, Path)):
        manifest_path = Path(value).expanduser().resolve()
        if not manifest_path.is_file():
            raise FileNotFoundError(manifest_path)
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read provenance manifest {manifest_path}: {exc}") from exc
        base_dir = manifest_path.parent
    else:
        raw = value
        base_dir = REPO
    return validate_provenance_manifest(raw, base_dir=base_dir), manifest_path


def verify_provenance_files(
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify hashes for files named by a canonical provenance manifest."""
    checks: dict[str, Any] = {}
    all_pass = True
    for name in PROVENANCE_FILE_SECTIONS:
        section = manifest.get(name)
        if not isinstance(section, Mapping):
            checks[name] = {"status": "invalid", "reason": "missing section"}
            all_pass = False
            continue
        path = Path(str(section["path"])).resolve(strict=False)
        expected = str(section["sha256"]).lower()
        record: dict[str, Any] = {
            "path": str(path),
            "expected_sha256": expected,
            "exists": path.is_file(),
        }
        if path.is_file():
            actual = _sha256_file(path)
            record["actual_sha256"] = actual
            record["sha256_match"] = secrets.compare_digest(actual, expected)
            record["status"] = "pass" if record["sha256_match"] else "fail"
        else:
            record["sha256_match"] = False
            record["status"] = "missing"
        checks[name] = record
        all_pass = all_pass and record["status"] == "pass"

    return {"status": "pass" if all_pass else "fail", "checks": checks}


def _file_record(path: Path, *, hash_file: bool = True) -> dict[str, Any]:
    path = path.resolve()
    record: dict[str, Any] = {"path": str(path), "exists": path.is_file()}
    if not path.is_file():
        return record
    stat = path.stat()
    record.update({"size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
    if hash_file:
        record["sha256"] = _sha256_file(path)
    return record


def _repo_path(value: str | Path, *, base: Path = REPO) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base / path


def _run(command: Sequence[str], *, cwd: Path = REPO) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        return {"command": list(command), "error": repr(exc)}
    return {
        "command": list(command),
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _package_version(distribution: str) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def _git_value(*args: str) -> str | None:
    result = _run(("git", *args))
    if result.get("returncode") != 0:
        return None
    return str(result.get("stdout", "")).strip()


def _git_snapshot() -> dict[str, Any]:
    diff = subprocess.run(
        ["git", "diff", "--binary", "--no-ext-diff"],
        cwd=REPO,
        capture_output=True,
        check=False,
    )
    status = _git_value("status", "--short") or ""
    return {
        "head": _git_value("rev-parse", "HEAD"),
        "branch": _git_value("branch", "--show-current"),
        "status_short": status.splitlines(),
        "tracked_diff_sha256": _sha256_bytes(diff.stdout),
        "tracked_diff_bytes": len(diff.stdout),
    }


def _relevant_repo_files() -> list[Path]:
    files = [REPO / name for name in CORE_FILES]
    files.extend(sorted((REPO / "scripts").glob("*.py")))
    files.extend(sorted(REPO.glob("HANDOFF*.md")))
    files.extend(sorted((REPO / "tests").glob("test_*.py")))
    seen: set[Path] = set()
    return [p for p in files if p.is_file() and not (p.resolve() in seen or seen.add(p.resolve()))]


def _template_records() -> list[dict[str, Any]]:
    records = []
    for pattern in ("source_images_*/*", "templates_*/*"):
        for path in sorted(REPO.glob(pattern)):
            if path.is_file():
                records.append(_file_record(path))
    return records


def _load_meta(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _meta_artifact_paths(meta_path: Path, meta: dict[str, Any]) -> list[Path]:
    paths = [meta_path]
    for value in (meta.get("paths") or {}).values():
        paths.append(_repo_path(value))
    return paths


def _ffmpeg_snapshot() -> dict[str, Any]:
    record: dict[str, Any] = {"path": None, "version": None}
    try:
        import imageio_ffmpeg  # type: ignore

        executable = Path(imageio_ffmpeg.get_ffmpeg_exe()).resolve()
        record["path"] = str(executable)
        record["file"] = _file_record(executable)
        version = _run((str(executable), "-version"))
        record["version"] = (version.get("stdout") or "").splitlines()[:1]
    except Exception as exc:
        record["error"] = repr(exc)
    return record


def capture_baseline(
    meta_paths: Sequence[Path],
    output: Path,
    *,
    full_video_hash: bool,
    reuse_video_hashes_from: Path | None = None,
) -> dict[str, Any]:
    reused_hashes: list[dict[str, Any]] = []
    reuse_manifest_record = None
    reuse_samples: list[Mapping[str, Any]] = []
    if reuse_video_hashes_from is not None:
        reuse_path = reuse_video_hashes_from.expanduser().resolve()
        reuse_manifest = json.loads(reuse_path.read_text(encoding="utf-8"))
        if reuse_manifest.get("kind") != "mpv_phase0_baseline":
            raise ValueError("reused video hashes must come from an mpv_phase0_baseline")
        raw_samples = reuse_manifest.get("samples")
        if not isinstance(raw_samples, list):
            raise ValueError("reused baseline has no samples list")
        reuse_samples = [item for item in raw_samples if isinstance(item, Mapping)]
        reuse_manifest_record = _file_record(reuse_path)

    samples = []
    for meta_path in meta_paths:
        meta_path = meta_path.resolve()
        meta = _load_meta(meta_path)
        video_path = Path(meta["video"]).resolve()
        artifacts = [_file_record(path) for path in _meta_artifact_paths(meta_path, meta)]
        video_record = _file_record(video_path, hash_file=full_video_hash)
        if not full_video_hash and reuse_video_hashes_from is not None:
            matching = next(
                (
                    item
                    for item in reuse_samples
                    if isinstance(item.get("video"), Mapping)
                    and item["video"].get("path")
                    and _same_path(item["video"]["path"], video_path)
                ),
                None,
            )
            if matching is None:
                raise ValueError(f"video is missing from reused baseline: {video_path}")
            prior_video = matching["video"]
            prior_sha = str(prior_video.get("sha256") or "").lower()
            if not _SHA256_RE.fullmatch(prior_sha):
                raise ValueError(f"reused baseline has no complete video hash: {video_path}")
            for field in ("size", "mtime_ns"):
                if int(prior_video.get(field, -1)) != int(video_record.get(field, -2)):
                    raise ValueError(
                        f"video {field} changed since reused baseline: {video_path}"
                    )
            video_record["sha256"] = prior_sha
            video_record["sha256_source"] = {
                "kind": "reused_verified_baseline",
                "baseline_path": str(reuse_video_hashes_from.expanduser().resolve()),
                "baseline_sha256": reuse_manifest_record["sha256"],
            }
            reused_hashes.append(
                {"path": str(video_path), "sha256": prior_sha}
            )

        samples.append(
            {
                "stem": str(meta.get("stem") or video_path.stem),
                "video": video_record,
                "meta_summary": {
                    key: meta.get(key)
                    for key in (
                        "backend",
                        "fps",
                        "meta_frames",
                        "analyzed_frames",
                        "proc_res",
                        "templates_loaded",
                        "n_pause_segments",
                        "n_speed_segments",
                        "n_skip_segs",
                        "skip_frames_sum",
                        "speedup_defaults",
                    )
                },
                "artifacts": artifacts,
            }
        )

    manifest = {
        "schema_version": 1,
        "kind": "mpv_phase0_baseline",
        "created_utc": _utc_now(),
        "repo": str(REPO),
        "git": _git_snapshot(),
        "runtime": {
            "platform": platform.platform(),
            "python": sys.version,
            "python_executable": sys.executable,
            "packages": {
                name: _package_version(name)
                for name in (
                    "numpy",
                    "opencv-python",
                    "Pillow",
                    "imageio",
                    "imageio-ffmpeg",
                    "python-mpv",
                )
            },
            "ffmpeg": _ffmpeg_snapshot(),
        },
        "worktree_files": [_file_record(path) for path in _relevant_repo_files()],
        "templates": _template_records(),
        "samples": samples,
        "baseline_completeness": {
            "status": "BLOCKED",
            "reason_codes": [
                "PREVIEW_GOLDEN_RESULT_MISSING",
                "EXPORT_GOLDEN_RESULT_MISSING",
                "CLIP_PERSISTENCE_BASELINE_MISSING",
            ],
            "analysis_artifacts": "captured",
            "project_state_contract": {
                "project_generation": "runtime-owned; initial value 0",
                "timeline_revision": "runtime-owned; initial value 0",
                "edit_commands": [
                    "SetPauseMode",
                    "SetPauseMaskRun",
                    "SetClipBounds",
                ],
            },
        },
        "video_hash_reuse": {
            "source_manifest": reuse_manifest_record,
            "reused": reused_hashes,
        },
        "notes": [
            "Existing preview cache predates this manifest and is not a golden result.",
            "A missing video sha256 means baseline was captured without --full-video-hash.",
            "BLOCKED completeness fields are explicit missing evidence, not Gate failures.",
        ],
    }
    _write_json(output, manifest)
    return manifest


def _jsonable_parameter(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value.expanduser().resolve(strict=False))
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable_parameter(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable_parameter(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _manifest_input(
    value: Path | str | None,
    *,
    expected_kind: str,
    missing_code: str,
    invalid_code: str,
) -> tuple[dict[str, Any] | None, dict[str, Any], list[str]]:
    if value is None:
        return None, {"path": None, "exists": False}, [missing_code]
    path = Path(value).expanduser().resolve(strict=False)
    record = _file_record(path)
    if not path.is_file():
        return None, record, [missing_code]
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("manifest root must be an object")
        if int(loaded.get("schema_version")) != 1:
            raise ValueError("schema_version must be 1")
        if loaded.get("kind") != expected_kind:
            raise ValueError(f"kind must be {expected_kind!r}")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        record["validation_error"] = str(exc)
        return None, record, [invalid_code]
    return loaded, record, []


_RUN_THRESHOLD_KEYS: dict[str, tuple[str, ...]] = {
    "env": (
        "require_runtime_status",
        "require_supply_chain_status",
    ),
    "stress": (
        "seek_failures_max",
        "duration_error_frames_max",
        "load_seconds_max",
        "seek_p95_ms_max",
        "seek_max_ms_max",
    ),
    "lifecycle": (
        "repeat_min",
        "failures_max",
        "handle_growth_max",
        "thread_growth_max",
    ),
    "wid": (
        "switch_loads_min",
        "load_failures_max",
        "load_p95_ms_max",
        "load_max_ms_max",
        "keyboard_events_min",
        "queued_errors_max",
    ),
    "stepspeed": (
        "frame_step_max_deviation_ms",
        "back_step_p95_ms_max",
        "back_step_max_ms_max",
        "speed_rate_min_ratio",
        "speed_rate_max_ratio",
    ),
}


def _validate_run_thresholds(command: str, values: Any) -> list[str]:
    if not isinstance(values, dict):
        return ["THRESHOLD_VALUES_MISSING"]
    required = _RUN_THRESHOLD_KEYS.get(command)
    if required is None:
        return ["THRESHOLD_COMMAND_UNSUPPORTED"]
    if any(key not in values for key in required):
        return ["THRESHOLD_REQUIRED_KEYS_MISSING"]
    if set(values) != set(required):
        return ["THRESHOLD_UNSUPPORTED_KEYS"]
    if command == "env":
        if any(values[key] != "pass" for key in required):
            return ["THRESHOLD_STATUS_REQUIREMENT_INVALID"]
        return []
    for key in required:
        value = values[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return ["THRESHOLD_NUMERIC_VALUE_INVALID"]
        if not math.isfinite(float(value)) or not float(value) >= 0.0:
            return ["THRESHOLD_NUMERIC_VALUE_INVALID"]
    return []


def _threshold_evaluation(
    command: str,
    report: Mapping[str, Any],
    values: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Evaluate only metrics named by the pre-run threshold contract."""
    if values is None:
        return {"status": "blocked", "checks": {}}

    checks: dict[str, dict[str, Any]] = {}

    def maximum(name: str, actual: Any) -> None:
        expected = values[name]
        status = "blocked" if actual is None else (
            "pass" if float(actual) <= float(expected) else "fail"
        )
        checks[name] = {"operator": "<=", "expected": expected, "actual": actual, "status": status}

    def minimum(name: str, actual: Any) -> None:
        expected = values[name]
        status = "blocked" if actual is None else (
            "pass" if float(actual) >= float(expected) else "fail"
        )
        checks[name] = {"operator": ">=", "expected": expected, "actual": actual, "status": status}

    def required_status(name: str, actual: Any) -> None:
        expected = values[name]
        if actual is None or actual == "blocked":
            status = "blocked"
        else:
            status = "pass" if actual == expected else "fail"
        checks[name] = {"operator": "==", "expected": expected, "actual": actual, "status": status}

    if command == "env":
        required_status("require_runtime_status", report.get("runtime_status"))
        required_status(
            "require_supply_chain_status", report.get("supply_chain_status")
        )
    elif command == "stress":
        failures = report.get("seek_failures")
        maximum(
            "seek_failures_max",
            len(failures) if isinstance(failures, list) else None,
        )
        maximum("duration_error_frames_max", report.get("duration_error_frames"))
        maximum("load_seconds_max", report.get("load_seconds"))
        latency = report.get("seek_latency") or {}
        maximum("seek_p95_ms_max", latency.get("p95_ms"))
        maximum("seek_max_ms_max", latency.get("max_ms"))
    elif command == "lifecycle":
        failures = report.get("failures")
        minimum("repeat_min", report.get("repeat"))
        maximum(
            "failures_max",
            len(failures) if isinstance(failures, list) else None,
        )
        initial_handles = report.get("initial_handles")
        final_handles = report.get("final_handles")
        maximum(
            "handle_growth_max",
            (
                final_handles - initial_handles
                if isinstance(initial_handles, int) and isinstance(final_handles, int)
                else None
            ),
        )
        initial_threads = report.get("initial_threads")
        final_threads = report.get("final_threads")
        maximum(
            "thread_growth_max",
            (
                final_threads - initial_threads
                if isinstance(initial_threads, int) and isinstance(final_threads, int)
                else None
            ),
        )
    elif command == "wid":
        history = report.get("load_history")
        failures = (
            sum(1 for item in history if not item.get("ok"))
            if isinstance(history, list)
            else None
        )
        minimum(
            "switch_loads_min", len(history) if isinstance(history, list) else None
        )
        maximum("load_failures_max", failures)
        latency = report.get("load_latency") or {}
        maximum("load_p95_ms_max", latency.get("p95_ms"))
        maximum("load_max_ms_max", latency.get("max_ms"))
        minimum("keyboard_events_min", report.get("keyboard_events"))
        queued = report.get("queued_events")
        maximum(
            "queued_errors_max",
            (
                sum(1 for item in queued if isinstance(item, dict) and item.get("error"))
                if isinstance(queued, list)
                else None
            ),
        )
    elif command == "stepspeed":
        exactness = report.get("frame_step_exactness") or {}
        maximum(
            "frame_step_max_deviation_ms",
            exactness.get("max_deviation_ms"),
        )
        timing = report.get("back_step_timing") or {}
        maximum("back_step_p95_ms_max", timing.get("p95_ms"))
        maximum("back_step_max_ms_max", timing.get("max_ms"))
        rates = report.get("speed_rates") or {}
        ratios = [
            entry.get("ratio")
            for entry in rates.values()
            if isinstance(entry, dict)
        ]
        ratios = [r for r in ratios if isinstance(r, (int, float))]
        minimum("speed_rate_min_ratio", min(ratios) if ratios else None)
        maximum("speed_rate_max_ratio", max(ratios) if ratios else None)

    statuses = {check["status"] for check in checks.values()}
    status = "fail" if "fail" in statuses else (
        "blocked" if "blocked" in statuses or not checks else "pass"
    )
    return {"status": status, "checks": checks}


def create_run_manifest(
    command: str,
    args: argparse.Namespace,
    expected_output: Path,
    *,
    artifacts: Sequence[Path] = (),
) -> tuple[Path, dict[str, Any]]:
    """Pre-register one Gate run before libmpv or Tk work starts.

    The manifest is write-once and the eventual result report records its
    SHA-256.  The manifest records the expected result path in return, giving
    the two files a bidirectional binding without mutating this pre-run file.
    Missing baseline/threshold evidence keeps the run usable for diagnostics,
    but marks the binding ``blocked`` rather than formal Gate evidence.
    """
    expected_output = expected_output.expanduser().resolve(strict=False)
    requested = getattr(args, "run_manifest", None)
    if requested is None:
        token = f"{_run_id()}-{time.time_ns()}-{secrets.token_hex(3)}"
        manifest_path = expected_output.with_name(
            f"{expected_output.stem}.run-{token}.json"
        )
    else:
        manifest_path = Path(requested).expanduser().resolve(strict=False)
    if _same_path(manifest_path, expected_output):
        raise ValueError("run manifest path must differ from the result report")

    baseline, baseline_record, reason_codes = _manifest_input(
        getattr(args, "baseline_manifest", None),
        expected_kind="mpv_phase0_baseline",
        missing_code="BASELINE_MANIFEST_MISSING",
        invalid_code="BASELINE_MANIFEST_INVALID",
    )
    thresholds, threshold_record, threshold_reasons = _manifest_input(
        getattr(args, "threshold_manifest", None),
        expected_kind="mpv_phase0_thresholds",
        missing_code="THRESHOLD_MANIFEST_MISSING",
        invalid_code="THRESHOLD_MANIFEST_INVALID",
    )
    reason_codes.extend(threshold_reasons)

    meta_path = (
        Path(args.meta).expanduser().resolve(strict=False)
        if getattr(args, "meta", None) is not None
        else None
    )
    meta_record = _file_record(meta_path) if meta_path is not None else None

    if baseline is not None:
        samples = baseline.get("samples")
        if not isinstance(samples, list) or not samples:
            reason_codes.append("BASELINE_SAMPLES_MISSING")
        elif any(
            not isinstance(sample, dict)
            or not isinstance(sample.get("video"), dict)
            or not sample["video"].get("sha256")
            for sample in samples
        ):
            reason_codes.append("BASELINE_VIDEO_HASH_MISSING")
        elif command in {"stress", "lifecycle", "wid"} and meta_path is not None:
            matching_sample = None
            for sample in samples:
                for artifact in sample.get("artifacts") or []:
                    if artifact.get("path") and _same_path(
                        artifact["path"], meta_path
                    ):
                        matching_sample = sample
                        break
                if matching_sample is not None:
                    break
            if matching_sample is None:
                reason_codes.append("BASELINE_META_NOT_BOUND")
            else:
                for artifact in matching_sample.get("artifacts") or []:
                    artifact_path = artifact.get("path")
                    expected_sha = artifact.get("sha256")
                    if not artifact_path or not expected_sha:
                        reason_codes.append("BASELINE_ARTIFACT_HASH_MISSING")
                        continue
                    current = _file_record(Path(artifact_path))
                    if not current.get("exists"):
                        reason_codes.append("BASELINE_ARTIFACT_MISSING")
                    elif not secrets.compare_digest(
                        str(current.get("sha256", "")).lower(),
                        str(expected_sha).lower(),
                    ):
                        reason_codes.append("BASELINE_ARTIFACT_HASH_MISMATCH")

                try:
                    current_meta = _load_meta(meta_path)
                    current_video = Path(current_meta["video"]).resolve()
                    baseline_video = matching_sample["video"]
                    if not _same_path(baseline_video["path"], current_video):
                        reason_codes.append("BASELINE_VIDEO_PATH_MISMATCH")
                    else:
                        current_video_record = _file_record(current_video)
                        if not secrets.compare_digest(
                            str(current_video_record.get("sha256", "")).lower(),
                            str(baseline_video.get("sha256", "")).lower(),
                        ):
                            reason_codes.append("BASELINE_VIDEO_HASH_MISMATCH")
                except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                    reason_codes.append("BASELINE_VIDEO_BINDING_INVALID")

    threshold_values: dict[str, Any] | None = None
    if thresholds is not None:
        if thresholds.get("command") != command:
            reason_codes.append("THRESHOLD_COMMAND_MISMATCH")
        values = thresholds.get("thresholds")
        if not isinstance(values, dict) or not values:
            reason_codes.append("THRESHOLD_VALUES_MISSING")
        else:
            threshold_values = dict(values)
            reason_codes.extend(
                _validate_run_thresholds(command, threshold_values)
            )

    verifier_paths = (
        Path(__file__).resolve(),
        (REPO / "scripts" / "verify_mpv_frames.py").resolve(),
        (REPO / "timeline_plan.py").resolve(),
    )
    verifier_records = [_file_record(path) for path in verifier_paths]
    if any(not record.get("exists") or not record.get("sha256") for record in verifier_records):
        reason_codes.append("VERIFIER_FILE_MISSING")

    parameters = {
        key: _jsonable_parameter(value)
        for key, value in sorted(vars(args).items())
        if key not in {"func", "run_manifest"}
    }
    input_records: dict[str, Any] = {
        "baseline_manifest": baseline_record,
        "threshold_manifest": threshold_record,
        "meta": meta_record,
        "provenance_manifest": (
            _file_record(
                Path(args.provenance_manifest).expanduser().resolve(strict=False)
            )
            if getattr(args, "provenance_manifest", None) is not None
            else None
        ),
        "artifacts": [_file_record(Path(path)) for path in artifacts],
    }
    reason_codes = list(dict.fromkeys(reason_codes))
    manifest = {
        "schema_version": 1,
        "kind": "mpv_phase0_run_manifest",
        "created_utc": _utc_now(),
        "status": "pass" if not reason_codes else "blocked",
        "reason_codes": reason_codes,
        "command": {"name": command, "parameters": parameters},
        "expected_result": {"path": str(expected_output)},
        "inputs": input_records,
        "verifiers": verifier_records,
        "preregistered_thresholds": threshold_values,
        "notes": [
            "This file is created before runtime probing and is never replaced.",
            "A blocked binding may be used for diagnostics but is not formal Gate evidence.",
        ],
    }
    _write_json_new(manifest_path, manifest)
    return manifest_path, manifest


def _attach_run_manifest(
    report: dict[str, Any],
    manifest_path: Path,
    manifest: Mapping[str, Any],
) -> None:
    record = _file_record(manifest_path)
    report["run_manifest"] = record
    report["run_binding_status"] = manifest.get("status")
    report["run_binding_reason_codes"] = list(
        manifest.get("reason_codes") or []
    )
    command = (manifest.get("command") or {}).get("name")
    evaluation = _threshold_evaluation(
        str(command or ""),
        report,
        (
            manifest.get("preregistered_thresholds")
            if manifest.get("status") == "pass"
            else None
        ),
    )
    report["preregistered_threshold_evaluation"] = evaluation
    reasons = report.setdefault("reason_codes", [])
    if evaluation["status"] == "fail":
        if "PREREGISTERED_THRESHOLD_EXCEEDED" not in reasons:
            reasons.append("PREREGISTERED_THRESHOLD_EXCEEDED")
        if report.get("status") != "error":
            report["status"] = "fail"
    elif evaluation["status"] == "blocked":
        if "PREREGISTERED_THRESHOLD_NOT_EVALUATED" not in reasons:
            reasons.append("PREREGISTERED_THRESHOLD_NOT_EVALUATED")
        if report.get("status") == "pass":
            report["status"] = "blocked"
    if manifest.get("status") != "pass":
        if "RUN_MANIFEST_INCOMPLETE" not in reasons:
            reasons.append("RUN_MANIFEST_INCOMPLETE")
        if report.get("status") == "pass":
            report["status"] = "blocked"


def normalize_ranges(ranges: Iterable[Sequence[int]], total: int) -> list[tuple[int, int]]:
    """Clamp, sort, and union half-open frame ranges."""
    total = max(0, int(total))
    normalized = []
    for item in ranges:
        if len(item) != 2:
            raise ValueError(f"range must have exactly two values: {item!r}")
        start = min(total, max(0, int(item[0])))
        end = min(total, max(0, int(item[1])))
        if end > start:
            normalized.append((start, end))
    normalized.sort()

    merged: list[tuple[int, int]] = []
    for start, end in normalized:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def complement_ranges(deleted: Sequence[tuple[int, int]], total: int) -> list[tuple[int, int]]:
    """Return the complement of normalized half-open ranges."""
    keep: list[tuple[int, int]] = []
    cursor = 0
    for start, end in deleted:
        if start > cursor:
            keep.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < total:
        keep.append((cursor, total))
    return keep


class FrameMap:
    def __init__(self, keep_ranges: Sequence[tuple[int, int]]):
        self.keep = list(keep_ranges)
        total = max((end for _, end in self.keep), default=0)
        deleted = complement_ranges(normalize_ranges(self.keep, total), total)
        self._plan = TimelinePlan.from_deleted_ranges(total, deleted)
        self.starts = [start for start, _ in self._plan.kept_ranges]
        self.prefix = list(self._plan.virtual_prefix[:-1])
        self.virtual_total = self._plan.kept_frames

    def source_to_virtual(self, frame: int) -> int | None:
        if frame < 0 or frame >= self._plan.total_frames:
            return None
        return self._plan.source_to_virtual(frame)

    def virtual_to_source(self, frame: int) -> int:
        try:
            return self._plan.virtual_to_source(frame)
        except IndexError:
            raise IndexError(frame) from None


def _mapping_report(
    deleted: Sequence[tuple[int, int]],
    keep: Sequence[tuple[int, int]],
    total: int,
) -> dict[str, Any]:
    mapping = FrameMap(keep)
    checked = 0
    for start, end in keep:
        for source in {start, end - 1, start + (end - start) // 2}:
            virtual = mapping.source_to_virtual(source)
            if virtual is None or mapping.virtual_to_source(virtual) != source:
                raise AssertionError(f"roundtrip failed for source frame {source}")
            checked += 1
    deleted_checked = 0
    for start, end in deleted:
        for source in {start, end - 1, start + (end - start) // 2}:
            if mapping.source_to_virtual(source) is not None:
                raise AssertionError(f"deleted source frame mapped into EDL: {source}")
            deleted_checked += 1
    if sum(end - start for start, end in deleted) + mapping.virtual_total != total:
        raise AssertionError("delete and keep ranges do not cover the source timeline")
    return {
        "source_frames": total,
        "virtual_frames": mapping.virtual_total,
        "keep_ranges": len(keep),
        "delete_ranges": len(deleted),
        "roundtrip_points_checked": checked,
        "deleted_points_checked": deleted_checked,
    }


def _edl_escape(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("EDL values cannot contain newlines")
    return f"%{len(value.encode('utf-8'))}%{value}"


class ProxyEdlEvidenceError(ValueError):
    """A proxy timeline cannot be safely consumed by the Phase 0 EDL writer."""


def _load_evidence_json(path: Path, name: str) -> dict[str, Any]:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProxyEdlEvidenceError(f"cannot read {name} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProxyEdlEvidenceError(f"{name} must be a JSON object")
    return value


def _edl_evidence_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProxyEdlEvidenceError(f"{name} must be an object")
    return dict(value)


def _edl_evidence_int(value: Any, name: str, *, minimum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProxyEdlEvidenceError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ProxyEdlEvidenceError(f"{name} must be >= {minimum}")
    return value


def _bound_evidence_file(
    record: Mapping[str, Any],
    name: str,
    *,
    base_dir: Path,
) -> Path:
    path_value = record.get("path")
    if not isinstance(path_value, str) or not path_value:
        raise ProxyEdlEvidenceError(f"{name}.path must be a non-empty string")
    path = _path_for_manifest(path_value, base_dir=base_dir)
    if not path.is_file():
        raise ProxyEdlEvidenceError(f"{name} file is missing: {path}")
    expected = record.get("sha256")
    if not isinstance(expected, str) or not _SHA256_RE.fullmatch(expected):
        raise ProxyEdlEvidenceError(f"{name}.sha256 must be a SHA-256 hex string")
    if not secrets.compare_digest(_sha256_file(path), expected.lower()):
        raise ProxyEdlEvidenceError(f"{name} SHA-256 does not match")
    return path


def _proxy_frame_mapping(source_start: int, count: int) -> dict[str, Any]:
    source_end = source_start + count
    return {
        "kind": "bounded_affine_frame_index",
        "source_start_frame": source_start,
        "proxy_start_frame": 0,
        "frame_count": count,
        "source_frame_domain": [source_start, source_end],
        "proxy_frame_domain": [0, count],
        "source_to_proxy_offset": -source_start,
        "proxy_to_source_offset": source_start,
        "source_to_proxy": (
            "proxy_frame_index = source_frame_index - source_frame_start"
        ),
        "proxy_to_source": (
            "source_frame_index = proxy_frame_index + source_frame_start"
        ),
    }


def _validate_proxy_video_report(
    report_path: Path,
    *,
    manifest_path: Path,
    oracle_path: Path,
    proxy_path: Path,
    source_start: int,
    count: int,
    mapping: Mapping[str, Any],
) -> dict[str, Any]:
    report_path = report_path.expanduser().resolve()
    report = _load_evidence_json(report_path, "proxy video report")
    kind = report.get("kind")
    if report.get("schema_version") not in (1, 2) or kind not in PTS_PROXY_VIDEO_REPORT_KINDS:
        raise ProxyEdlEvidenceError("proxy video report schema or kind is invalid")

    if kind == "mpv_phase0_pts_normalized_proxy_verification":
        manifest_record = _edl_evidence_object(
            report.get("manifest"), "proxy video report manifest binding"
        )
        oracle_record = _edl_evidence_object(
            report.get("decoded_proxy_oracle"),
            "proxy video report oracle binding",
        )
        binding = _edl_evidence_object(
            report.get("binding"), "proxy video report binding result"
        )
        if binding.get("status") != "PASS" or binding.get("reason_codes") not in ([], None):
            raise ProxyEdlEvidenceError("proxy video report binding did not pass")
        proxy_record = None
    else:
        bindings = _edl_evidence_object(
            report.get("bindings"), "proxy video report bindings"
        )
        manifest_record = _edl_evidence_object(
            bindings.get("manifest"), "proxy video report manifest binding"
        )
        oracle_record = _edl_evidence_object(
            bindings.get("proxy_oracle"), "proxy video report oracle binding"
        )
        proxy_record = _edl_evidence_object(
            bindings.get("proxy"), "proxy video report proxy binding"
        )

    bound_manifest = _bound_evidence_file(
        manifest_record, "proxy video report manifest", base_dir=report_path.parent
    )
    bound_oracle = _bound_evidence_file(
        oracle_record, "proxy video report oracle", base_dir=report_path.parent
    )
    if not _same_path(bound_manifest, manifest_path):
        raise ProxyEdlEvidenceError("proxy video report binds another manifest")
    if not _same_path(bound_oracle, oracle_path):
        raise ProxyEdlEvidenceError("proxy video report binds another oracle")
    if proxy_record is not None:
        bound_proxy = _bound_evidence_file(
            proxy_record, "proxy video report proxy", base_dir=report_path.parent
        )
        if not _same_path(bound_proxy, proxy_path):
            raise ProxyEdlEvidenceError("proxy video report binds another proxy")

    video = _edl_evidence_object(
        report.get("video_validation"), "proxy video validation"
    )
    if video.get("status") != "PASS" or video.get("reason_codes") not in ([], None):
        raise ProxyEdlEvidenceError("proxy video validation did not pass")
    if video.get("business_frame_count") != count:
        raise ProxyEdlEvidenceError("proxy video validation frame count differs")
    if video.get("business_frame_domain") != [0, count]:
        raise ProxyEdlEvidenceError("proxy video validation business domain differs")
    if video.get("decoded_frame_count") != count + 1:
        raise ProxyEdlEvidenceError("proxy video validation did not observe one guard")
    for field in (
        "checksum_alignment",
        "pts_alignment",
        "positive_business_durations",
    ):
        section = _edl_evidence_object(
            video.get(field), f"proxy video validation {field}"
        )
        if section.get("status") != "PASS" or section.get("mismatch_count") not in (0, None):
            raise ProxyEdlEvidenceError(f"proxy video validation {field} did not pass")
    guard = _edl_evidence_object(
        video.get("terminal_guard"), "proxy video validation guard"
    )
    if (
        guard.get("observed") is not True
        or guard.get("included_in_business_domain") is not False
        or guard.get("business_frame_domain") != [0, count]
        or guard.get("checksum_matches") is not True
        or guard.get("pts_matches") is not True
    ):
        raise ProxyEdlEvidenceError("proxy video validation guard is invalid")
    expected_source_domain = [source_start, source_start + count]
    for owner in (report, video):
        if owner.get("source_frame_domain") is not None and owner.get(
            "source_frame_domain"
        ) != expected_source_domain:
            raise ProxyEdlEvidenceError("proxy video report source domain differs")
        if owner.get("proxy_frame_domain") is not None and owner.get(
            "proxy_frame_domain"
        ) != [0, count]:
            raise ProxyEdlEvidenceError("proxy video report proxy domain differs")
        if owner.get("frame_mapping") is not None and owner.get("frame_mapping") != mapping:
            raise ProxyEdlEvidenceError("proxy video report frame mapping differs")

    return {
        "path": report_path,
        "report": report,
        # These reports prove the video leg only.  Neither accepted schema is
        # the complete audio/A-V/libmpv gate report, even if hand-edited to
        # claim PASS, so it cannot grant EDL authority by itself.
        "gate_ready": False,
    }


def _validate_proxy_gate_report(
    gate_report_path: Path,
    *,
    manifest_path: Path,
    oracle_path: Path,
    video_report_path: Path,
    source_path: Path,
    proxy_path: Path,
) -> dict[str, Any]:
    """Validate the final audio/A-V gate and its immutable run bindings."""
    gate_report_path = gate_report_path.expanduser().resolve()
    report = _load_evidence_json(gate_report_path, "proxy gate report")
    if (
        report.get("schema_version") != 2
        or report.get("kind") != "mpv_phase0_audio_av_report"
    ):
        raise ProxyEdlEvidenceError(
            "only a schema-v2 audio/A-V report can grant proxy EDL authority"
        )
    if (
        report.get("status") != "PASS"
        or report.get("proxy_ready_for_gate") is not True
        or report.get("scope") != "full"
        or report.get("reason_codes") != []
    ):
        raise ProxyEdlEvidenceError("proxy audio/A-V gate report did not fully pass")

    run_record = _edl_evidence_object(
        report.get("run_manifest"), "proxy gate run manifest binding"
    )
    run_path = _bound_evidence_file(
        run_record, "proxy gate run manifest", base_dir=gate_report_path.parent
    )
    run = _load_evidence_json(run_path, "proxy gate run manifest")
    if (
        run.get("schema_version") != 2
        or run.get("kind") != "mpv_phase0_audio_av_run_manifest"
        or run.get("status") != "PASS"
        or run.get("scope") != "full"
    ):
        raise ProxyEdlEvidenceError("proxy gate run manifest did not fully pass")

    bound_manifest = _bound_evidence_file(
        _edl_evidence_object(
            run.get("video_proxy_manifest"), "gate run proxy manifest binding"
        ),
        "gate run proxy manifest",
        base_dir=run_path.parent,
    )
    bound_video_report = _bound_evidence_file(
        _edl_evidence_object(
            run.get("video_report"), "gate run video report binding"
        ),
        "gate run video report",
        base_dir=run_path.parent,
    )
    bound_source = _bound_evidence_file(
        _edl_evidence_object(run.get("source"), "gate run source binding"),
        "gate run source",
        base_dir=run_path.parent,
    )
    bound_proxy = _bound_evidence_file(
        _edl_evidence_object(run.get("proxy"), "gate run proxy binding"),
        "gate run proxy",
        base_dir=run_path.parent,
    )
    if not _same_path(bound_manifest, manifest_path):
        raise ProxyEdlEvidenceError("proxy gate binds another proxy manifest")
    if not _same_path(bound_video_report, video_report_path):
        raise ProxyEdlEvidenceError("proxy gate binds another video report")
    if not _same_path(bound_source, source_path):
        raise ProxyEdlEvidenceError("proxy gate binds another source")
    if not _same_path(bound_proxy, proxy_path):
        raise ProxyEdlEvidenceError("proxy gate binds another proxy")

    timeline_manifest = _load_evidence_json(
        manifest_path, "proxy gate bound proxy manifest"
    )
    timeline_source = _edl_evidence_object(
        timeline_manifest.get("source"), "proxy gate manifest source"
    )
    source_oracle = _bound_evidence_file(
        _edl_evidence_object(
            timeline_source.get("oracle"), "proxy gate manifest source oracle"
        ),
        "proxy gate manifest source oracle",
        base_dir=manifest_path.parent,
    )
    video = _edl_evidence_object(
        report.get("video_validation"), "proxy gate video validation"
    )
    run_video = _edl_evidence_object(
        run.get("video_evidence"), "proxy gate run video evidence"
    )
    if video != run_video or video.get("status") != "PASS" or video.get(
        "reason_codes"
    ) not in ([], None):
        raise ProxyEdlEvidenceError("proxy gate video evidence is not a bound PASS")
    for owner_name, owner, key in (
        ("proxy gate video", video, "decoded_proxy_oracle"),
        ("proxy gate run video", run_video, "decoded_proxy_oracle"),
    ):
        bound_oracle = _bound_evidence_file(
            _edl_evidence_object(owner.get(key), f"{owner_name} oracle binding"),
            f"{owner_name} oracle",
            base_dir=run_path.parent,
        )
        if not _same_path(bound_oracle, oracle_path):
            raise ProxyEdlEvidenceError("proxy gate binds another PTS oracle")
    gate_source_oracle = _bound_evidence_file(
        _edl_evidence_object(
            video.get("source_oracle"), "proxy gate source oracle binding"
        ),
        "proxy gate source oracle",
        base_dir=run_path.parent,
    )
    if not _same_path(gate_source_oracle, source_oracle):
        raise ProxyEdlEvidenceError("proxy gate binds another source oracle")
    source_oracle_value = _load_evidence_json(
        source_oracle, "proxy gate source oracle"
    )
    if (
        source_oracle_value.get("schema_version") != 1
        or source_oracle_value.get("kind") != "mpv_phase0_frame_pts_oracle"
    ):
        raise ProxyEdlEvidenceError("proxy gate source oracle schema or kind is invalid")
    source_oracle_video = _bound_evidence_file(
        _edl_evidence_object(
            source_oracle_value.get("video"), "proxy gate source oracle video"
        ),
        "proxy gate source oracle video",
        base_dir=source_oracle.parent,
    )
    if not _same_path(source_oracle_video, source_path):
        raise ProxyEdlEvidenceError("proxy gate source oracle binds another source")
    if (
        not isinstance(video.get("source_pixel_format"), str)
        or video.get("source_pixel_format") != video.get("proxy_pixel_format")
    ):
        raise ProxyEdlEvidenceError("proxy gate pixel formats are not preserved")

    audio_timeline = _edl_evidence_object(
        report.get("audio_timeline"), "proxy gate audio timeline"
    )
    audio_preservation = _edl_evidence_object(
        report.get("audio_preservation"), "proxy gate audio preservation"
    )
    av_sync = _edl_evidence_object(report.get("av_sync"), "proxy gate A/V sync")
    if (
        audio_timeline.get("status") != "PASS"
        or audio_timeline.get("reason_codes") not in ([], None)
        or audio_preservation.get("status") != "PASS"
        or audio_preservation.get("reason_codes") not in ([], None)
        or av_sync.get("status") != "PASS"
        or av_sync.get("reason_codes") not in ([], None)
    ):
        raise ProxyEdlEvidenceError("proxy gate audio or A/V evidence did not pass")
    anchors = _edl_evidence_object(
        av_sync.get("content_anchors"), "proxy gate content anchors"
    )
    if anchors.get("status") not in ("PASS", "NOT_APPLICABLE_PASS"):
        raise ProxyEdlEvidenceError("proxy gate content anchors did not pass")
    if anchors.get("status") == "PASS":
        anchor_binding = _edl_evidence_object(
            anchors.get("binding_checks"), "proxy gate anchor binding checks"
        )
        if anchor_binding.get("status") != "PASS":
            raise ProxyEdlEvidenceError("proxy gate content anchor binding did not pass")
        for key in ("manifest", "observation"):
            _bound_evidence_file(
                _edl_evidence_object(
                    anchors.get(key), f"proxy gate content anchor {key}"
                ),
                f"proxy gate content anchor {key}",
                base_dir=run_path.parent,
            )
    if run.get("video_evidence") != video:
        raise ProxyEdlEvidenceError("proxy gate run/report video evidence differs")
    if run.get("audio_preservation") != audio_preservation:
        raise ProxyEdlEvidenceError("proxy gate run/report audio evidence differs")
    if run.get("av_sync") != av_sync:
        raise ProxyEdlEvidenceError("proxy gate run/report A/V evidence differs")
    run_audio = _edl_evidence_object(
        run.get("audio_timeline"), "proxy gate run audio timeline"
    )
    if (
        run_audio.get("status") != audio_timeline.get("status")
        or run_audio.get("reason_codes") != audio_timeline.get("reason_codes")
    ):
        raise ProxyEdlEvidenceError("proxy gate run/report audio timeline differs")
    return {"path": gate_report_path, "run_path": run_path, "report": report}


def _load_proxy_edl_contract(
    manifest_path: Path,
    oracle_path: Path,
    video_report_path: Path,
    gate_report_path: Path | None,
    *,
    source_video_path: Path,
    total_frames: int,
) -> dict[str, Any]:
    manifest_path = manifest_path.expanduser().resolve()
    oracle_path = oracle_path.expanduser().resolve()
    manifest = _load_evidence_json(manifest_path, "proxy manifest")
    kind = manifest.get("kind")
    if manifest.get("schema_version") != 1 or kind not in PTS_PROXY_MANIFEST_KINDS:
        raise ProxyEdlEvidenceError("proxy manifest schema or kind is invalid")

    timeline_manifest = manifest
    timeline_manifest_path = manifest_path
    if kind == "mpv_phase0_prefix_audio_copy_manifest":
        video_manifest_record = _edl_evidence_object(
            manifest.get("video_manifest"), "bound video manifest"
        )
        timeline_manifest_path = _bound_evidence_file(
            video_manifest_record,
            "bound video manifest",
            base_dir=manifest_path.parent,
        )
        timeline_manifest = _load_evidence_json(
            timeline_manifest_path, "bound video manifest"
        )
        if (
            timeline_manifest.get("schema_version") != 1
            or timeline_manifest.get("kind")
            != "mpv_phase0_pts_normalized_proxy"
        ):
            raise ProxyEdlEvidenceError("bound video manifest schema or kind is invalid")
        if manifest.get("normalized_timeline") != timeline_manifest.get(
            "normalized_timeline"
        ):
            raise ProxyEdlEvidenceError(
                "playable proxy timeline differs from its bound video manifest"
            )

    source = _edl_evidence_object(manifest.get("source"), "proxy source")
    timeline_source = _edl_evidence_object(
        timeline_manifest.get("source"), "timeline manifest source"
    )
    timeline = _edl_evidence_object(
        timeline_manifest.get("normalized_timeline"), "normalized timeline"
    )
    playable_timeline = _edl_evidence_object(
        manifest.get("normalized_timeline"), "playable normalized timeline"
    )
    if playable_timeline != timeline:
        raise ProxyEdlEvidenceError("playable proxy timeline binding differs")

    bound_source = _bound_evidence_file(
        source, "proxy source", base_dir=manifest_path.parent
    )
    if not _same_path(bound_source, source_video_path):
        raise ProxyEdlEvidenceError("proxy source differs from the EDL source metadata")
    timeline_bound_source = _bound_evidence_file(
        timeline_source,
        "timeline manifest source",
        base_dir=timeline_manifest_path.parent,
    )
    if not _same_path(timeline_bound_source, bound_source):
        raise ProxyEdlEvidenceError("proxy manifests bind different source media")

    count = _edl_evidence_int(
        timeline_source.get("business_frame_count"),
        "business_frame_count",
        minimum=1,
    )
    if source.get("business_frame_count") != count:
        raise ProxyEdlEvidenceError("playable proxy business frame count differs")
    source_start = _edl_evidence_int(
        timeline.get("source_frame_start"), "source_frame_start", minimum=0
    )
    source_end = source_start + count
    if source_end > total_frames:
        raise ProxyEdlEvidenceError("proxy source frame domain exceeds metadata")
    expected_mapping = _proxy_frame_mapping(source_start, count)
    if timeline.get("source_frame_end_exclusive") != source_end:
        raise ProxyEdlEvidenceError("normalized source frame end is invalid")
    for field, expected in (
        ("source_frame_domain", [source_start, source_end]),
        ("proxy_frame_domain", [0, count]),
        ("frame_mapping", expected_mapping),
    ):
        if timeline.get(field) != expected:
            raise ProxyEdlEvidenceError(f"normalized timeline {field} is invalid")
        if timeline_source.get(field) != expected:
            raise ProxyEdlEvidenceError(f"timeline manifest source {field} is invalid")
    if timeline_source.get("business_frame_start") != source_start:
        raise ProxyEdlEvidenceError("timeline manifest source start is invalid")
    if timeline.get("frame_fps_fallback_used") is not False:
        raise ProxyEdlEvidenceError("proxy timeline must forbid frame/fps fallback")

    scope = manifest.get("scope", source.get("scope"))
    if scope not in ("prefix", "full") or timeline_source.get("scope") != scope:
        raise ProxyEdlEvidenceError("proxy source scope is invalid")
    if scope == "full" and (source_start != 0 or count != total_frames):
        raise ProxyEdlEvidenceError("full proxy does not cover the full source domain")

    time_base = _edl_evidence_object(timeline.get("time_base"), "proxy time base")
    numerator = _edl_evidence_int(
        time_base.get("numerator"), "time_base.numerator", minimum=1
    )
    denominator = _edl_evidence_int(
        time_base.get("denominator"), "time_base.denominator", minimum=1
    )
    duration_ticks = _edl_evidence_int(
        timeline.get("duration_ticks"), "duration_ticks", minimum=1
    )
    business_end_ticks = count * duration_ticks
    if (
        timeline.get("business_pts_start") != 0
        or timeline.get("business_pts_end_exclusive") != business_end_ticks
        or timeline.get("normalized_end_ticks") != business_end_ticks
    ):
        raise ProxyEdlEvidenceError("normalized business PTS domain is invalid")

    proxy_record = _edl_evidence_object(manifest.get("proxy"), "playable proxy")
    proxy_path = _bound_evidence_file(
        proxy_record, "playable proxy", base_dir=manifest_path.parent
    )
    oracle = _load_evidence_json(oracle_path, "proxy PTS oracle")
    if oracle.get("schema_version") != 1 or oracle.get("kind") != "mpv_phase0_frame_pts_oracle":
        raise ProxyEdlEvidenceError("proxy PTS oracle schema or kind is invalid")
    oracle_video = _edl_evidence_object(oracle.get("video"), "proxy oracle video")
    oracle_proxy = _bound_evidence_file(
        oracle_video, "proxy oracle video", base_dir=oracle_path.parent
    )
    if not _same_path(oracle_proxy, proxy_path):
        raise ProxyEdlEvidenceError("proxy PTS oracle binds another proxy")
    ffmpeg = _edl_evidence_object(oracle.get("ffmpeg"), "proxy oracle FFmpeg result")
    if ffmpeg.get("returncode") != 0:
        raise ProxyEdlEvidenceError("proxy PTS oracle decode did not complete")
    showinfo = _edl_evidence_object(oracle.get("showinfo"), "proxy oracle showinfo")
    if showinfo.get("parsed_frames") != count + 1:
        raise ProxyEdlEvidenceError("proxy PTS oracle did not decode one terminal guard")
    oracle_time_base = _edl_evidence_object(
        showinfo.get("time_base"), "proxy oracle time base"
    )
    if (
        oracle_time_base.get("numerator") != numerator
        or oracle_time_base.get("denominator") != denominator
    ):
        raise ProxyEdlEvidenceError("proxy oracle time base differs from the manifest")
    rows_value = oracle.get("pts_table")
    if not isinstance(rows_value, list) or len(rows_value) != count + 1:
        raise ProxyEdlEvidenceError("proxy PTS table must include every business frame and one guard")
    rows: list[dict[str, Any]] = []
    for index, value in enumerate(rows_value):
        row = _edl_evidence_object(value, f"proxy pts_table[{index}]")
        if row.get("n") != index:
            raise ProxyEdlEvidenceError("proxy frame indices are not contiguous")
        pts = _edl_evidence_int(row.get("pts"), f"proxy pts_table[{index}].pts")
        if pts != index * duration_ticks:
            raise ProxyEdlEvidenceError("proxy PTS table differs from the normalized timeline")
        if index < count:
            duration = _edl_evidence_int(
                row.get("duration"),
                f"proxy pts_table[{index}].duration",
                minimum=1,
            )
            if duration != duration_ticks:
                raise ProxyEdlEvidenceError(
                    "proxy business frame duration differs from the normalized timeline"
                )
        rows.append(row)

    guard = _edl_evidence_object(
        timeline_manifest.get("terminal_guard"), "terminal guard"
    )
    if (
        guard.get("proxy_frame_index") != count
        or guard.get("pts_ticks") != business_end_ticks
        or guard.get("business_frame_domain") != [0, count]
        or guard.get("included_in_business_domain") is not False
        or guard.get("source_frame_index") != source_end - 1
        or guard.get("source_frame_domain") != [source_start, source_end]
        or guard.get("proxy_frame_domain") != [0, count]
    ):
        raise ProxyEdlEvidenceError("terminal guard mapping is invalid")
    if rows[count].get("checksum") != guard.get("expected_checksum"):
        raise ProxyEdlEvidenceError("terminal guard checksum differs from the manifest")

    verification = _validate_proxy_video_report(
        video_report_path,
        manifest_path=manifest_path,
        oracle_path=oracle_path,
        proxy_path=proxy_path,
        source_start=source_start,
        count=count,
        mapping=expected_mapping,
    )
    gate_verification = None
    if gate_report_path is not None:
        gate_verification = _validate_proxy_gate_report(
            gate_report_path,
            manifest_path=manifest_path,
            oracle_path=oracle_path,
            video_report_path=verification["path"],
            source_path=bound_source,
            proxy_path=proxy_path,
        )
    return {
        "manifest_path": manifest_path,
        "oracle_path": oracle_path,
        "video_report_path": verification["path"],
        "video_report": verification["report"],
        "gate_report_path": (
            gate_verification["path"] if gate_verification is not None else None
        ),
        "gate_run_manifest_path": (
            gate_verification["run_path"]
            if gate_verification is not None
            else None
        ),
        "gate_ready": gate_verification is not None,
        "proxy_path": proxy_path,
        "scope": scope,
        "source_start": source_start,
        "source_end": source_end,
        "count": count,
        "mapping": expected_mapping,
        "time_base": {"numerator": numerator, "denominator": denominator},
        "duration_ticks": duration_ticks,
        "business_end_ticks": business_end_ticks,
        "business_rows": rows[:count],
        "guard_row": rows[count],
    }


def _edl_seconds(ticks: int, time_base: Mapping[str, int]) -> str:
    seconds = (
        Decimal(ticks)
        * Decimal(time_base["numerator"])
        / Decimal(time_base["denominator"])
    )
    return format(seconds, ".12f")


def build_proxy_edl(
    meta_path: Path,
    proxy_manifest_path: Path,
    proxy_oracle_path: Path,
    proxy_video_report_path: Path,
    output_dir: Path,
    *,
    proxy_gate_report_path: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Write an experimental EDL from a verified proxy frame/PTS mapping.

    This is deliberately separate from ``build_edl``.  It may prove that the
    writer consumed the proxy PTS table while the overall libmpv gate remains
    blocked on full-scope audio, A/V, and runtime evidence.
    """
    meta_path = meta_path.expanduser().resolve()
    meta = _load_meta(meta_path)
    total = int(meta["analyzed_frames"])
    if total < 1:
        raise ProxyEdlEvidenceError("metadata must contain at least one frame")
    source_video = Path(meta["video"]).expanduser().resolve()
    if not source_video.is_file():
        raise FileNotFoundError(source_video)
    skip_path = _repo_path(meta["paths"]["skip_segs"])
    raw_deleted = json.loads(skip_path.read_text(encoding="utf-8"))
    deleted = normalize_ranges(raw_deleted, total)
    timeline_plan = TimelinePlan.from_deleted_ranges(total, deleted)
    contract = _load_proxy_edl_contract(
        proxy_manifest_path,
        proxy_oracle_path,
        proxy_video_report_path,
        proxy_gate_report_path,
        source_video_path=source_video,
        total_frames=total,
    )

    source_start = contract["source_start"]
    source_end = contract["source_end"]
    rows = contract["business_rows"]
    time_base = contract["time_base"]
    kept_in_proxy: list[tuple[int, int]] = []
    for start, end in timeline_plan.kept_ranges:
        clipped_start = max(start, source_start)
        clipped_end = min(end, source_end)
        if clipped_start < clipped_end:
            kept_in_proxy.append((clipped_start, clipped_end))

    escaped_path = _edl_escape(contract["proxy_path"].as_posix())
    lines = ["# mpv EDL v0"]
    segments: list[dict[str, Any]] = []
    for source_segment_start, source_segment_end in kept_in_proxy:
        proxy_start = source_segment_start - source_start
        proxy_end = source_segment_end - source_start
        start_ticks = rows[proxy_start]["pts"]
        last_row = rows[proxy_end - 1]
        end_ticks = last_row["pts"] + last_row["duration"]
        if end_ticks > contract["business_end_ticks"]:
            raise ProxyEdlEvidenceError("EDL segment enters the terminal guard domain")
        length_ticks = end_ticks - start_ticks
        start_seconds = _edl_seconds(start_ticks, time_base)
        length_seconds = _edl_seconds(length_ticks, time_base)
        lines.append(f"{escaped_path},{start_seconds},{length_seconds}")
        segments.append(
            {
                "source_frame_range": [source_segment_start, source_segment_end],
                "proxy_frame_range": [proxy_start, proxy_end],
                "proxy_pts_range": [start_ticks, end_ticks],
                "start_seconds": start_seconds,
                "length_seconds": length_seconds,
            }
        )

    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = str(meta.get("stem") or source_video.stem)
    edl_path = output_dir / f"{stem}_proxy.edl"
    edl_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    video_report = contract["video_report"]
    reason_codes: list[str] = []
    if not contract["gate_ready"]:
        reason_codes.append("PROXY_GATE_NOT_FULLY_VERIFIED")
    if contract["scope"] != "full":
        reason_codes.append("SOURCE_SCOPE_NOT_FULL")
    for reason in video_report.get("reason_codes", []):
        if isinstance(reason, str):
            reason_codes.append(reason)
    reason_codes = list(dict.fromkeys(reason_codes))
    status = "pass" if not reason_codes else "blocked"
    report = {
        "schema_version": 2,
        "kind": "mpv_phase0_proxy_pts_edl",
        "created_utc": _utc_now(),
        "gate": "G2",
        "status": status,
        "reason_codes": reason_codes,
        "meta": _file_record(meta_path),
        "skip_segments": _file_record(skip_path),
        "source_video": _file_record(source_video),
        "proxy_manifest": _file_record(contract["manifest_path"]),
        "proxy_oracle": _file_record(contract["oracle_path"]),
        "proxy_video_report": _file_record(contract["video_report_path"]),
        "proxy_gate_report": (
            _file_record(contract["gate_report_path"])
            if contract["gate_report_path"] is not None
            else {"path": None, "exists": False}
        ),
        "proxy_gate_run_manifest": (
            _file_record(contract["gate_run_manifest_path"])
            if contract["gate_run_manifest_path"] is not None
            else {"path": None, "exists": False}
        ),
        "proxy_video": _file_record(contract["proxy_path"]),
        "edl": _file_record(edl_path),
        "interval_semantics": "half-open [start, end)",
        "timing_model": "decoded proxy PTS/duration ticks",
        # Consuming the table proves the writer contract, not the whole G2
        # route.  Authority additionally requires a fully bound gate PASS.
        "authoritative_pts_mapping": contract["gate_ready"],
        "pts_table_consumed": True,
        "frame_fps_fallback_used": False,
        "metadata_fps_diagnostic_only": meta.get("fps"),
        "mapping": {
            **contract["mapping"],
            "kept_source_ranges": [list(value) for value in kept_in_proxy],
            "segments": segments,
            "time_base": time_base,
            "business_pts_domain": [0, contract["business_end_ticks"]],
            "terminal_guard_proxy_frame": contract["count"],
            "terminal_guard_pts_ticks": contract["business_end_ticks"],
            "terminal_guard_included": False,
        },
        "checks": {
            "evidence_binding": "pass",
            "source_to_proxy_mapping": "pass",
            "proxy_pts_table": "pass",
            "positive_business_durations": "pass",
            "terminal_guard_outside_edl_intervals": "pass",
            "mpv_boundary_playback": "not_run",
            "edl_file_written": "pass",
            "proxy_gate": "pass" if contract["gate_ready"] else "blocked",
        },
    }
    report_path = output_dir / f"{stem}_proxy_edl_manifest.json"
    _write_json(report_path, report)
    return edl_path, report


def build_edl(meta_path: Path, output_dir: Path) -> tuple[Path, dict[str, Any]]:
    meta_path = meta_path.resolve()
    meta = _load_meta(meta_path)
    total = int(meta["analyzed_frames"])
    fps = float(meta["fps"])
    if fps <= 0:
        raise ValueError(f"invalid fps: {fps}")
    video_path = Path(meta["video"]).resolve()
    if not video_path.is_file():
        raise FileNotFoundError(video_path)
    skip_path = _repo_path(meta["paths"]["skip_segs"])
    raw_deleted = json.loads(skip_path.read_text(encoding="utf-8"))
    deleted = normalize_ranges(raw_deleted, total)
    timeline = TimelinePlan.from_deleted_ranges(total, deleted)
    keep = list(timeline.kept_ranges)
    mapping = _mapping_report(deleted, keep, total)

    output_dir.mkdir(parents=True, exist_ok=True)
    stem = str(meta.get("stem") or video_path.stem)
    edl_path = output_dir / f"{stem}.edl"
    escaped_path = _edl_escape(video_path.as_posix())
    lines = ["# mpv EDL v0"]
    for start, end in keep:
        lines.append(f"{escaped_path},{start / fps:.9f},{(end - start) / fps:.9f}")
    edl_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    # A decoder executable being present is not evidence that its frame PTS
    # are authoritative.  The independent oracle must produce a PASS report
    # and a per-frame table before a PTS-based EDL can be enabled.  Current
    # exploratory EDLs intentionally remain CFR approximations.
    oracle_path = CACHE_ROOT / "frame_oracle" / f"{stem}.json"
    oracle = None
    if oracle_path.is_file():
        try:
            oracle = json.loads(oracle_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            oracle = None
    oracle_has_table = bool(
        oracle
        and oracle.get("status") == "PASS"
        and oracle.get("authoritative_frame_timeline") is True
        and oracle.get("pts_table")
    )
    # The current writer below still emits start/length from frame/fps.  Keep
    # the gate blocked even if a future oracle table appears until the writer
    # actually consumes that table.
    oracle_authoritative = False
    pts_reason = [
        "PTS_EDL_WRITER_NOT_IMPLEMENTED"
        if oracle_has_table
        else "PTS_ORACLE_NOT_AUTHORITATIVE"
    ]
    report = {
        "schema_version": 1,
        "kind": "mpv_phase0_edl",
        "created_utc": _utc_now(),
        "gate": "G2",
        "status": "pass" if oracle_authoritative else "blocked",
        "reason_codes": pts_reason,
        "meta": _file_record(meta_path),
        "skip_segments": _file_record(skip_path),
        "video": _file_record(video_path, hash_file=False),
        "fps": fps,
        "mapping": mapping,
        "edl": _file_record(edl_path),
        "interval_semantics": "half-open [start, end)",
        "timing_model": (
            "per-frame PTS oracle" if oracle_authoritative
            else "CFR frame/fps exploratory approximation"
        ),
        "authoritative_pts_mapping": oracle_authoritative,
        "pts_oracle": {
            "path": str(oracle_path.resolve()),
            "exists": oracle_path.is_file(),
            "status": oracle.get("status") if oracle else None,
            "has_pts_table": bool(oracle and oracle.get("pts_table")),
        },
        "checks": {
            "frame_interval_mapping": "pass",
            "edl_file_written": "pass",
            "pts_mapping": "pass" if oracle_authoritative else "blocked",
        },
    }
    report_path = output_dir / f"{stem}_edl_manifest.json"
    _write_json(report_path, report)
    return edl_path, report


def _wrapper_record() -> dict[str, Any]:
    record: dict[str, Any] = {"distribution_version": _package_version("python-mpv")}
    try:
        distribution = importlib.metadata.distribution("python-mpv")
        wrapper = Path(distribution.locate_file("mpv.py")).resolve()
        record["module"] = _file_record(wrapper)
    except Exception as exc:
        record["error"] = repr(exc)
    return record


def _candidate_dlls(mpv_dir: Path | None) -> list[Path]:
    candidates: list[Path] = []
    values: list[Path] = []
    if mpv_dir is not None:
        values.append(mpv_dir)
    if os.environ.get("MPV_DLL_DIR"):
        values.append(Path(os.environ["MPV_DLL_DIR"]))
    values.extend((REPO / "vendor" / "mpv", Path(sys.executable).resolve().parent))
    for value in values:
        value = value.expanduser().resolve()
        if value.is_file() and value.name.lower() in DLL_NAMES:
            candidates.append(value)
        elif value.is_dir():
            candidates.extend(value / name for name in DLL_NAMES if (value / name).is_file())
    unique = []
    seen: set[Path] = set()
    for path in candidates:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _loaded_dll_path() -> Path | None:
    if os.name != "nt":
        return None
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    kernel32.GetModuleFileNameW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_uint,
    ]
    kernel32.GetModuleFileNameW.restype = ctypes.c_uint
    for name in DLL_NAMES:
        handle = kernel32.GetModuleHandleW(name)
        if not handle:
            continue
        buffer = ctypes.create_unicode_buffer(32768)
        if kernel32.GetModuleFileNameW(handle, buffer, len(buffer)):
            return Path(buffer.value).resolve()
    return None


def _pe_machine(path: Path) -> str | None:
    try:
        with path.open("rb") as stream:
            if stream.read(2) != b"MZ":
                return None
            stream.seek(0x3C)
            pe_offset = int.from_bytes(stream.read(4), "little")
            stream.seek(pe_offset)
            if stream.read(4) != b"PE\x00\x00":
                return None
            machine = int.from_bytes(stream.read(2), "little")
    except OSError:
        return None
    return {0x8664: "x86_64", 0x014C: "x86", 0xAA64: "arm64"}.get(machine, hex(machine))


def probe_mpv(
    mpv_dir: Path | None,
    *,
    provenance: Path | str | Mapping[str, Any] | None = None,
    source_url: str | None = None,
    license_note: str | None = None,
) -> tuple[dict[str, Any], Any | None]:
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "mpv_phase0_environment",
        "created_utc": _utc_now(),
        "gate": "G0",
        "wrapper": _wrapper_record(),
        "requested_mpv_dir": str(mpv_dir.resolve()) if mpv_dir else None,
        "source_url": source_url,
        "license_note": license_note,
        "reason_codes": [],
    }

    provenance_data: dict[str, Any] | None = None
    provenance_path: Path | None = None
    try:
        provenance_data, provenance_path = _load_provenance_manifest(provenance)
        if provenance_data is not None:
            if source_url is not None and source_url.strip() != provenance_data["source_url"]:
                raise ValueError("--source-url does not match provenance source_url")
            manifest_license_note = provenance_data["license"].get("note")
            if (
                license_note is not None
                and license_note.strip() != manifest_license_note
            ):
                raise ValueError("--license-note does not match provenance license.note")
            report["source_url"] = provenance_data["source_url"]
            report["license_note"] = manifest_license_note
            report["provenance_manifest"] = (
                _file_record(provenance_path)
                if provenance_path is not None
                else {"inline": True}
            )
            report["provenance"] = provenance_data
            report["provenance_files"] = verify_provenance_files(provenance_data)
        else:
            report["provenance_manifest"] = None
            report["provenance"] = None
            report["provenance_files"] = {
                "status": "missing",
                "checks": {},
            }
            report["reason_codes"].append("PROVENANCE_MANIFEST_MISSING")
    except (FileNotFoundError, ValueError, TypeError, KeyError) as exc:
        # Provenance is a hard G0 condition, not a prerequisite for collecting
        # local runtime diagnostics. Keep probing the explicitly requested DLL
        # while making it impossible for this run to pass the supply-chain Gate.
        provenance_data = None
        report.update(
            {
                "supply_chain_status": "blocked",
                "status": "blocked",
                "provenance_error": str(exc),
                "provenance_manifest": (
                    _file_record(Path(provenance).expanduser())
                    if isinstance(provenance, (str, Path))
                    else {"inline": provenance is not None}
                ),
                "provenance": None,
                "provenance_files": {"status": "invalid", "checks": {}},
            }
        )
        report["reason_codes"].append("PROVENANCE_MANIFEST_INVALID")

    expected_dll = (
        Path(provenance_data["dll"]["path"]).resolve(strict=False)
        if provenance_data is not None
        else None
    )
    requested_value = mpv_dir
    if requested_value is None and os.environ.get("MPV_DLL_DIR"):
        requested_value = Path(os.environ["MPV_DLL_DIR"])
    if requested_value is None and expected_dll is not None:
        requested_value = expected_dll
    candidates = _candidate_dlls(requested_value)
    report["candidate_dlls"] = [_file_record(path) for path in candidates]

    requested_directory = None
    requested_dll = None
    if requested_value is not None:
        requested_value = requested_value.expanduser().resolve(strict=False)
        requested_dll = requested_value if requested_value.is_file() else None
        directory = requested_value if requested_value.is_dir() else requested_value.parent
        requested_directory = directory.resolve()
        if expected_dll is not None:
            requested_matches_manifest = (
                _same_path(requested_dll, expected_dll)
                if requested_dll is not None
                else _same_path(requested_directory, expected_dll.parent)
            )
            report["requested_path_matches_provenance"] = requested_matches_manifest
            if not requested_matches_manifest:
                report["reason_codes"].append("REQUESTED_DLL_PATH_MISMATCH")
                report["provenance_blocker"] = (
                    "the requested mpv path does not contain the exact DLL "
                    "declared by the provenance manifest"
                )

        if (
            provenance_data is not None
            and report["provenance_files"]["status"] != "pass"
        ):
            report["reason_codes"].append("PROVENANCE_FILE_VERIFICATION_FAILED")
            report["provenance_blocker"] = (
                "one or more provenance file hashes could not be verified"
            )

        if os.name == "nt" and directory.is_dir():
            _DLL_HANDLES.append(os.add_dll_directory(str(requested_directory)))
        os.environ["PATH"] = str(requested_directory) + os.pathsep + os.environ.get("PATH", "")

    try:
        module = importlib.import_module("mpv")
        player = module.MPV(vo="null", audio="no", terminal=False, idle="yes")
        try:
            report["libmpv_version"] = player.mpv_version
            report["client_api_version"] = getattr(module, "MPV_VERSION", None)
            report["mpv_configuration"] = player.mpv_configuration
            report["ffmpeg_version"] = player.ffmpeg_version
        finally:
            player.terminate()
        loaded = _loaded_dll_path()
        loaded_record = _file_record(loaded) if loaded else None
        if loaded_record is not None and loaded is not None:
            loaded_record["pe_machine"] = _pe_machine(loaded)
        report["loaded_dll"] = loaded_record
        requested_path_matches = bool(
            loaded
            and requested_value
            and (
                _same_path(loaded, requested_dll)
                if requested_dll is not None
                else _same_path(loaded.parent, requested_directory)
            )
        )
        report["loaded_from_requested_directory"] = requested_path_matches

        provenance_path_matches = bool(
            loaded and expected_dll and _same_path(loaded, expected_dll)
        )
        provenance_hash_matches = bool(
            loaded_record
            and provenance_data
            and loaded_record.get("sha256")
            and secrets.compare_digest(
                str(loaded_record["sha256"]).lower(),
                str(provenance_data["dll"]["sha256"]).lower(),
            )
        )
        report["loaded_dll_matches_provenance_path"] = provenance_path_matches
        report["loaded_dll_matches_provenance_sha256"] = provenance_hash_matches
        supply_chain_complete = bool(
            provenance_data
            and report["provenance_files"]["status"] == "pass"
            and loaded
            and requested_path_matches
            and provenance_path_matches
            and provenance_hash_matches
        )
        report["runtime_status"] = "pass"
        report["supply_chain_status"] = "pass" if supply_chain_complete else "blocked"
        report["status"] = "pass" if supply_chain_complete else "blocked"
        if not supply_chain_complete:
            if provenance_data is not None and not provenance_path_matches:
                report["reason_codes"].append("LOADED_DLL_PATH_MISMATCH")
            if provenance_data is not None and not provenance_hash_matches:
                report["reason_codes"].append("LOADED_DLL_HASH_MISMATCH")
            if not requested_path_matches:
                report["reason_codes"].append("LOADED_DLL_REQUEST_MISMATCH")
            report["blocker"] = report.get(
                "provenance_blocker",
                report.get(
                    "provenance_error",
                    "loaded libmpv did not satisfy every provenance check",
                ),
            )
        return report, module
    except Exception as exc:
        report.update(
            {
                "runtime_status": "blocked",
                "supply_chain_status": "blocked",
                "status": "blocked",
                "blocker": repr(exc),
            }
        )
        report["reason_codes"].append("MPV_RUNTIME_UNAVAILABLE")
        return report, None


def _create_player(module: Any, *, wid: int | None = None, log: list[dict[str, str]] | None = None, hwdec: str | None = None) -> Any:
    def on_log(level: str, prefix: str, text: str) -> None:
        if log is not None and level in {"fatal", "error", "warn"} and len(log) < 500:
            log.append({"level": level, "prefix": prefix, "text": text.rstrip()})

    options: dict[str, Any] = {
        "audio": "no",
        "osc": False,
        "osd_bar": False,
        "osd_level": 0,
        "input_default_bindings": False,
        "input_vo_keyboard": False,
        "keep_open": "yes",
        "idle": "yes",
        "hr_seek": "yes",
        "framedrop": "vo",
        "cache": "yes",
        "demuxer_max_bytes": "256MiB",
        "demuxer_readahead_secs": 2,
        "terminal": False,
    }
    if hwdec:
        options["hwdec"] = hwdec
    framedrop = None
    if isinstance(getattr(_create_player, "_framedrop", None), str):
        framedrop = _create_player._framedrop
    if framedrop:
        options["framedrop"] = framedrop
    if wid is None:
        options["vo"] = "null"
    else:
        options["wid"] = str(wid)
        options["force_window"] = "yes"
        options["hwdec"] = "auto-safe"
    return module.MPV(log_handler=on_log, loglevel="warn", **options)


def _load_and_wait(player: Any, path: Path, timeout: float) -> float:
    started = time.perf_counter()
    with player.prepare_and_wait_for_event("file-loaded", timeout=timeout):
        player.play(str(path.resolve()))
    return time.perf_counter() - started


def _timeline_from_meta(meta_path: Path) -> tuple[dict[str, Any], list[tuple[int, int]], list[tuple[int, int]]]:
    meta = _load_meta(meta_path.resolve())
    total = int(meta["analyzed_frames"])
    skip_path = _repo_path(meta["paths"]["skip_segs"])
    raw_deleted = json.loads(skip_path.read_text(encoding="utf-8"))
    deleted = normalize_ranges(raw_deleted, total)
    keep = complement_ranges(deleted, total)
    return meta, deleted, keep


def _sample_values(values: Sequence[int], count: int) -> list[int]:
    if count <= 0 or not values:
        return []
    if len(values) <= count:
        return list(values)
    if count == 1:
        return [values[len(values) // 2]]
    indices = {round(i * (len(values) - 1) / (count - 1)) for i in range(count)}
    return [values[index] for index in sorted(indices)]


def _latency_summary(samples: Sequence[float]) -> dict[str, float | int | None]:
    if not samples:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "p99_ms": None, "max_ms": None}
    ordered = sorted(samples)

    def percentile(p: float) -> float:
        index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * p)))
        return ordered[index] * 1000.0

    return {
        "count": len(ordered),
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "p99_ms": percentile(0.99),
        "max_ms": max(ordered) * 1000.0,
    }


def _wait_for_seek(player: Any, target: float, fps: float, timeout: float) -> tuple[bool, float | None]:
    deadline = time.perf_counter() + timeout
    tolerance = max(2.0 / max(fps, 1e-6), 0.04)
    last_position = None
    while time.perf_counter() < deadline:
        try:
            last_position = player.time_pos
            seeking = bool(player.seeking)
        except Exception:
            seeking = True
        if last_position is not None and not seeking and abs(float(last_position) - target) <= tolerance:
            return True, float(last_position)
        time.sleep(0.005)
    return False, float(last_position) if last_position is not None else None


def _windows_handle_count() -> int | None:
    if os.name != "nt":
        return None
    import ctypes

    count = ctypes.c_ulong()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.GetProcessHandleCount.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.GetProcessHandleCount.restype = ctypes.c_int
    process = kernel32.GetCurrentProcess()
    if not kernel32.GetProcessHandleCount(process, ctypes.byref(count)):
        return None
    return int(count.value)


def _configure_tk_environment() -> dict[str, Any]:
    result = {"tcl_library": os.environ.get("TCL_LIBRARY"), "tk_library": os.environ.get("TK_LIBRARY")}
    tcl_root = Path(sys.base_prefix) / "tcl"
    candidates = {
        "TCL_LIBRARY": tcl_root / "tcl8.6",
        "TK_LIBRARY": tcl_root / "tk8.6",
    }
    for name, path in candidates.items():
        if not os.environ.get(name) and (path / ("init.tcl" if name == "TCL_LIBRARY" else "tk.tcl")).is_file():
            os.environ[name] = str(path)
            result[name.lower()] = str(path)
            result["auto_configured"] = True
    return result


def _hwnd_class(hwnd: int) -> str | None:
    if os.name != "nt":
        return None
    import ctypes

    buffer = ctypes.create_unicode_buffer(256)
    if ctypes.windll.user32.GetClassNameW(hwnd, buffer, len(buffer)):
        return buffer.value
    return None


def _cmd_stress(args: argparse.Namespace) -> int:
    output = args.output or (_default_run_dir() / "stress.json")
    edl_path, edl_report = build_edl(args.meta, output.parent / "edl")
    run_path, run_manifest = create_run_manifest(
        "stress", args, output, artifacts=(edl_path,)
    )
    environment, module = probe_mpv(
        args.mpv_dir,
        provenance=getattr(args, "provenance_manifest", None),
        source_url=getattr(args, "source_url", None),
        license_note=getattr(args, "license_note", None),
    )
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "mpv_phase0_headless_stress",
        "created_utc": _utc_now(),
        "environment": environment,
        "edl": edl_report,
        "status": "blocked",
        "reason_codes": [],
    }
    if module is None:
        report["reason_codes"].extend(
            environment.get("reason_codes") or ["MPV_DLL_NOT_FOUND"]
        )
        _attach_run_manifest(report, run_path, run_manifest)
        _write_json(output, report)
        print(f"report: {output.resolve()}")
        return 2

    meta, _, keep = _timeline_from_meta(args.meta)
    fps = float(meta["fps"])
    mapping = FrameMap(keep)
    boundary_frames = [value for value in mapping.prefix[1:] if value < mapping.virtual_total]
    targets = [(value + 0.5) / fps for value in _sample_values(boundary_frames, args.samples)]
    logs: list[dict[str, str]] = []
    player = _create_player(module, log=logs)
    failures = []
    seek_latencies = []
    observed_positions = []
    try:
        player.pause = True
        load_seconds = _load_and_wait(player, edl_path, args.timeout)
        duration = float(player.duration or 0.0)
        for target in targets:
            started = time.perf_counter()
            player.command("seek", target, "absolute+exact")
            ok, observed = _wait_for_seek(player, target, fps, args.timeout)
            elapsed = time.perf_counter() - started
            if ok:
                seek_latencies.append(elapsed)
            else:
                failures.append({"target": target, "observed": observed})
            observed_positions.append({"target": target, "observed": observed, "seconds": elapsed})
        expected_duration = mapping.virtual_total / fps
        report.update(
            {
                "local_runtime_status": "pass" if not failures else "fail",
                "load_seconds": load_seconds,
                "duration_seconds": duration,
                "expected_cfr_duration_seconds": expected_duration,
                "duration_error_frames": abs(duration - expected_duration) * fps,
                "seek_latency": _latency_summary(seek_latencies),
                "seek_failures": failures,
                "seek_observations": observed_positions,
                "mpv_logs": logs,
            }
        )
    except Exception as exc:
        report.update({"local_runtime_status": "error", "error": repr(exc)})
    finally:
        try:
            player.stop()
        except Exception:
            pass
        player.terminate()

    if report.get("local_runtime_status") == "fail":
        report["status"] = "fail"
        report["reason_codes"].append("ASSERTION_FAILED")
        exit_code = 1
    elif report.get("local_runtime_status") == "error":
        report["status"] = "error"
        report["reason_codes"].append("HARNESS_ERROR")
        exit_code = 3
    else:
        report["status"] = "blocked"
        if environment.get("supply_chain_status") != "pass":
            report["reason_codes"].append("UNVERIFIED_DLL_PROVENANCE")
        report["reason_codes"].append("AUTHORITATIVE_PTS_MISSING")
        exit_code = 2
    _attach_run_manifest(report, run_path, run_manifest)
    _write_json(output, report)
    print(json.dumps({key: report.get(key) for key in ("status", "local_runtime_status", "load_seconds", "duration_error_frames", "seek_latency", "seek_failures")}, indent=2))
    print(f"report: {output.resolve()}")
    return exit_code


def _cmd_lifecycle(args: argparse.Namespace) -> int:
    output = args.output or (_default_run_dir() / "lifecycle.json")
    edl_path, edl_report = build_edl(args.meta, output.parent / "edl")
    run_path, run_manifest = create_run_manifest(
        "lifecycle", args, output, artifacts=(edl_path,)
    )
    environment, module = probe_mpv(
        args.mpv_dir,
        provenance=getattr(args, "provenance_manifest", None),
        source_url=getattr(args, "source_url", None),
        license_note=getattr(args, "license_note", None),
    )
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "mpv_phase0_lifecycle",
        "created_utc": _utc_now(),
        "environment": environment,
        "edl": edl_report,
        "repeat": args.repeat,
        "iterations": [],
    }
    if module is None:
        report.update(
            {
                "status": "blocked",
                "reason_codes": environment.get("reason_codes")
                or ["MPV_DLL_NOT_FOUND"],
            }
        )
        _attach_run_manifest(report, run_path, run_manifest)
        _write_json(output, report)
        return 2

    failures = []
    initial_handles = _windows_handle_count()
    initial_threads = threading.active_count()
    for index in range(args.repeat):
        before_handles = _windows_handle_count()
        started = time.perf_counter()
        player = None
        error = None
        try:
            player = _create_player(module)
            player.pause = True
            _load_and_wait(player, edl_path, args.timeout)
            player.stop()
        except Exception as exc:
            error = repr(exc)
            failures.append({"iteration": index, "error": error})
        finally:
            if player is not None:
                try:
                    player.terminate()
                except Exception as exc:
                    error = error or repr(exc)
                    failures.append({"iteration": index, "error": repr(exc)})
            gc.collect()
        report["iterations"].append(
            {
                "iteration": index,
                "seconds": time.perf_counter() - started,
                "handles_before": before_handles,
                "handles_after": _windows_handle_count(),
                "threads_after": threading.active_count(),
                "error": error,
            }
        )
    report.update(
        {
            "initial_handles": initial_handles,
            "final_handles": _windows_handle_count(),
            "initial_threads": initial_threads,
            "final_threads": threading.active_count(),
            "failures": failures,
            "local_runtime_status": "pass" if not failures else "fail",
        }
    )
    if failures:
        report.update({"status": "fail", "reason_codes": ["LIFECYCLE_FAILURE"]})
        exit_code = 1
    else:
        reason_codes = ["WID_LIFECYCLE_NOT_COVERED"]
        if environment.get("supply_chain_status") != "pass":
            reason_codes.insert(0, "UNVERIFIED_DLL_PROVENANCE")
        report.update(
            {
                "status": "blocked",
                "reason_codes": reason_codes,
            }
        )
        exit_code = 2
    _attach_run_manifest(report, run_path, run_manifest)
    _write_json(output, report)
    print(json.dumps({key: report.get(key) for key in ("status", "local_runtime_status", "initial_handles", "final_handles", "initial_threads", "final_threads", "failures")}, indent=2))
    print(f"report: {output.resolve()}")
    return exit_code


def _wait_for_time_change(
    player: Any,
    previous: float | None,
    *,
    timeout: float,
    minimum_change: float,
) -> tuple[float | None, float]:
    """Poll time-pos until it moves by at least ``minimum_change`` seconds."""
    started = time.perf_counter()
    while time.perf_counter() - started < timeout:
        value = player.time_pos if player.time_pos is not None else None
        if (
            previous is not None
            and value is not None
            and abs(value - previous) >= minimum_change
        ):
            return value, time.perf_counter() - started
        time.sleep(0.002)
    value = player.time_pos if player.time_pos is not None else None
    return value, time.perf_counter() - started


def _cmd_stepspeed(args: argparse.Namespace) -> int:
    """G5: source-mode frame-step exactness, back-step latency and speeds."""
    output = args.output or (_default_run_dir() / "stepspeed.json")
    video = Path(args.video).expanduser().resolve()
    if not video.is_file():
        raise FileNotFoundError(video)
    run_path, run_manifest = create_run_manifest(
        "stepspeed", args, output, artifacts=(video,)
    )
    environment, module = probe_mpv(
        args.mpv_dir,
        provenance=getattr(args, "provenance_manifest", None),
        source_url=getattr(args, "source_url", None),
        license_note=getattr(args, "license_note", None),
    )
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "mpv_phase0_stepspeed",
        "gate": "G5",
        "created_utc": _utc_now(),
        "environment": environment,
        "video": _file_record(video),
        "scope": "source_mode_full_file",
        "scope_note": (
            "EDL cut-point overshoot at speed requires an approved time route "
            "(G2/G3) and is recorded there, not here."
        ),
        "start_pos": args.start_pos,
        "hwdec": getattr(args, "hwdec", None) or "no",
        "framedrop": getattr(args, "framedrop", None) or "vo(default)",
        "speeds": [float(item) for item in str(args.speeds).split(",") if item.strip()],
        "repeats": args.repeats,
        "cold_repeats": args.cold_repeats,
    }
    if module is None:
        report.update(
            {
                "status": "blocked",
                "reason_codes": environment.get("reason_codes")
                or ["MPV_DLL_NOT_FOUND"],
            }
        )
        _attach_run_manifest(report, run_path, run_manifest)
        _write_json(output, report)
        return 2

    try:
        import psutil  # type: ignore

        process = psutil.Process()
        cpu_probe = True
    except Exception:
        process = None
        cpu_probe = False
    report["cpu_probe"] = "psutil" if cpu_probe else "unavailable"

    def new_player() -> tuple[Any, float]:
        _create_player._framedrop = getattr(args, "framedrop", None)
        player = _create_player(module, hwdec=getattr(args, "hwdec", None))
        player.pause = True
        load_seconds = _load_and_wait(player, video, args.timeout)
        return player, load_seconds

    failures: list[dict[str, Any]] = []

    # --- Phase 1: frame-step exactness on a hot player ---------------------
    player, _load = new_player()
    frame_duration = None
    container_fps = player.container_fps if hasattr(player, "container_fps") else None
    if container_fps and float(container_fps) > 0:
        frame_duration = 1.0 / float(container_fps)
    report["container_fps"] = container_fps
    report["frame_duration_seconds"] = frame_duration

    player.seek(args.start_pos, reference="absolute+exact")
    time.sleep(0.2)
    initial_pos = player.time_pos
    forward_dev_ms: list[float] = []
    for _index in range(args.step_repeats):
        before = player.time_pos
        player.frame_step()
        after, _wait = _wait_for_time_change(
            player, before, timeout=args.timeout, minimum_change=1e-4
        )
        if after is None or before is None or frame_duration is None:
            failures.append({"phase": "forward_step", "index": _index})
            continue
        forward_dev_ms.append(abs(after - before - frame_duration) * 1000.0)
    back_dev_ms: list[float] = []
    for _index in range(args.step_repeats):
        before = player.time_pos
        player.frame_back_step()
        after, _wait = _wait_for_time_change(
            player, before, timeout=args.timeout, minimum_change=1e-4
        )
        if after is None or before is None or frame_duration is None:
            failures.append({"phase": "back_step_exactness", "index": _index})
            continue
        back_dev_ms.append(abs(before - after - frame_duration) * 1000.0)
    all_dev = forward_dev_ms + back_dev_ms
    final_pos = player.time_pos
    report["step_position_drift"] = {
        "initial_pos": initial_pos,
        "final_pos": final_pos,
        "drift_frames": (
            (final_pos - initial_pos) / frame_duration
            if initial_pos is not None and final_pos is not None and frame_duration
            else None
        ),
    }
    report["frame_step_exactness"] = {
        "forward_samples": len(forward_dev_ms),
        "back_samples": len(back_dev_ms),
        "max_deviation_ms": max(all_dev) if all_dev else None,
        "mean_deviation_ms": (
            sum(all_dev) / len(all_dev) if all_dev else None
        ),
        "forward_deviation_ms": [round(v, 3) for v in forward_dev_ms],
        "back_deviation_ms": [round(v, 3) for v in back_dev_ms],
    }
    player.terminate()
    gc.collect()

    # --- Phase 2: back-step latency, cold (fresh load) and hot -------------
    cold_s: list[float] = []
    for index in range(args.cold_repeats):
        player = None
        try:
            player, _load = new_player()
            player.seek(args.start_pos + 5.0, reference="absolute+exact")
            time.sleep(0.1)
            previous = player.time_pos
            if previous is None:
                failures.append(
                    {"phase": "cold_back_step", "index": index, "error": "time-pos unavailable"}
                )
                continue
            started = time.perf_counter()
            player.frame_back_step()
            _after, _waited = _wait_for_time_change(
                player,
                previous,
                timeout=args.timeout,
                minimum_change=1e-4,
            )
            cold_s.append(time.perf_counter() - started)
        except Exception as exc:
            failures.append({"phase": "cold_back_step", "index": index, "error": repr(exc)})
        finally:
            if player is not None:
                player.terminate()
        gc.collect()
    hot_s: list[float] = []
    player, _load = new_player()
    try:
        for index in range(args.repeats):
            player.seek(args.start_pos + 5.0 + (index % 7), reference="absolute+exact")
            time.sleep(0.05)
            previous = player.time_pos
            if previous is None:
                failures.append(
                    {"phase": "hot_back_step", "index": index, "error": "time-pos unavailable"}
                )
                continue
            started = time.perf_counter()
            player.frame_back_step()
            _after, _waited = _wait_for_time_change(
                player,
                previous,
                timeout=args.timeout,
                minimum_change=1e-4,
            )
            hot_s.append(time.perf_counter() - started)
    except Exception as exc:
        failures.append({"phase": "hot_back_step", "index": index, "error": repr(exc)})
    finally:
        player.terminate()
        gc.collect()
    report["back_step_timing"] = _latency_summary(cold_s + hot_s)
    report["back_step_timing_cold"] = _latency_summary(cold_s)
    report["back_step_timing_hot"] = _latency_summary(hot_s)

    # --- Phase 3: measured advance rates at each speed ----------------------
    speed_rates: dict[str, dict[str, Any]] = {}
    for speed in report["speeds"]:
        player, _load = new_player()
        entry: dict[str, Any] = {"speed": speed}
        try:
            player.seek(args.start_pos, reference="absolute+exact")
            time.sleep(0.2)
            if cpu_probe and process is not None:
                process.cpu_percent(interval=None)
            player.speed = speed
            player.pause = False
            time.sleep(args.speed_ramp)
            media_before = player.time_pos
            wall_before = time.perf_counter()
            time.sleep(args.speed_window)
            media_after = player.time_pos
            wall_after = time.perf_counter()
            player.pause = True
            wall = wall_after - wall_before
            media = (
                media_after - media_before
                if media_after is not None and media_before is not None
                else None
            )
            entry["wall_seconds"] = wall
            entry["media_seconds"] = media
            entry["measured_rate"] = media / wall if media is not None and wall > 0 else None
            entry["ratio"] = (
                (media / wall) / speed if media is not None and wall > 0 else None
            )
            if cpu_probe and process is not None:
                entry["process_cpu_percent"] = process.cpu_percent(interval=None)
        except Exception as exc:
            entry["error"] = repr(exc)
            failures.append({"phase": "speed", "speed": speed, "error": repr(exc)})
        finally:
            player.terminate()
        gc.collect()
        speed_rates[f"x{speed:g}"] = entry
    report["speed_rates"] = speed_rates

    report["failures"] = failures
    if failures:
        report["status"] = "fail"
        report["reason_codes"] = ["STEPSPEED_FAILURES"]
    else:
        report["status"] = "pass"
        report["reason_codes"] = []
    _attach_run_manifest(report, run_path, run_manifest)
    _write_json(output, report)
    print(f"report: {output}")
    return 0 if report["status"] == "pass" else 2


def _cmd_wid(args: argparse.Namespace) -> int:
    output = args.output or (_default_run_dir() / "wid.json")
    edl_path, edl_report = build_edl(args.meta, output.parent / "edl")
    run_path, run_manifest = create_run_manifest(
        "wid", args, output, artifacts=(edl_path,)
    )
    environment, module = probe_mpv(
        args.mpv_dir,
        provenance=getattr(args, "provenance_manifest", None),
        source_url=getattr(args, "source_url", None),
        license_note=getattr(args, "license_note", None),
    )
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "mpv_phase0_tk_wid",
        "created_utc": _utc_now(),
        "environment": environment,
        "edl": edl_report,
        "tk_environment": _configure_tk_environment(),
    }
    if module is None:
        report.update(
            {
                "status": "blocked",
                "reason_codes": environment.get("reason_codes")
                or ["MPV_DLL_NOT_FOUND"],
            }
        )
        _attach_run_manifest(report, run_path, run_manifest)
        _write_json(output, report)
        return 2

    import tkinter as tk
    from tkinter import ttk

    meta = _load_meta(args.meta.resolve())
    source_path = Path(meta["video"]).resolve()
    events: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=256)
    logs: list[dict[str, str]] = []
    root = tk.Tk()
    root.title("libmpv Phase 0 WID")
    root.geometry(args.geometry)

    toolbar = ttk.Frame(root)
    toolbar.pack(fill=tk.X)
    status_var = tk.StringVar(value="creating WID")
    ttk.Label(toolbar, textvariable=status_var).pack(side=tk.LEFT, padx=6)
    host = tk.Frame(root, background="black")
    host.pack(fill=tk.BOTH, expand=True)
    root.update_idletasks()
    hwnd = int(host.winfo_id())
    hwnd_class = _hwnd_class(hwnd)
    player = _create_player(module, wid=hwnd, log=logs)
    closing = False
    load_history: list[dict[str, Any]] = []
    keyboard_events = 0
    final_properties: dict[str, Any] = {}

    def load(path: Path, mode: str) -> None:
        try:
            seconds = _load_and_wait(player, path, args.timeout)
            player.pause = True
            load_history.append({"mode": mode, "path": str(path), "seconds": seconds, "ok": True})
            status_var.set(f"{mode} loaded in {seconds * 1000:.1f}ms")
        except Exception as exc:
            load_history.append({"mode": mode, "path": str(path), "ok": False, "error": repr(exc)})
            status_var.set(f"load failed: {exc}")

    def toggle_pause(_event: Any = None) -> str:
        nonlocal keyboard_events
        keyboard_events += 1
        player.pause = not bool(player.pause)
        return "break"

    def close() -> None:
        nonlocal closing
        if closing:
            return
        closing = True
        for name in ("vo_configured", "current_vo", "video_params", "duration", "time_pos"):
            try:
                final_properties[name] = getattr(player, name)
            except Exception as exc:
                final_properties[name] = {"error": repr(exc)}
        try:
            player.stop()
        except Exception:
            pass
        player.terminate()
        root.destroy()

    ttk.Button(toolbar, text="Source", command=lambda: load(source_path, "source")).pack(side=tk.RIGHT)
    ttk.Button(toolbar, text="EDL", command=lambda: load(edl_path, "edl")).pack(side=tk.RIGHT)
    ttk.Button(toolbar, text="Play/Pause", command=toggle_pause).pack(side=tk.RIGHT)
    ttk.Button(toolbar, text="Back", command=lambda: player.command("frame-back-step")).pack(side=tk.RIGHT)
    ttk.Button(toolbar, text="Step", command=lambda: player.command("frame-step")).pack(side=tk.RIGHT)
    root.bind("<space>", toggle_pause)
    root.protocol("WM_DELETE_WINDOW", close)

    def poll() -> None:
        if closing:
            return
        try:
            position = player.time_pos
            status_var.set(
                f"pos={float(position or 0):.3f}s vo={player.current_vo} configured={player.vo_configured}"
            )
        except Exception as exc:
            try:
                events.put_nowait({"error": repr(exc)})
            except queue.Full:
                pass
        root.after(100, poll)

    def auto_switch(index: int = 0) -> None:
        total_loads = max(0, int(args.switch_cycles)) * 2
        if total_loads <= 0:
            load(edl_path, "edl")
            return
        path, mode = (source_path, "source") if index % 2 == 0 else (edl_path, "edl")
        load(path, mode)
        if index + 1 < total_loads:
            root.after(args.switch_interval_ms, lambda: auto_switch(index + 1))
        elif args.auto_seconds <= 0:
            root.after(300, close)

    root.after(100, auto_switch)
    root.after(150, poll)
    if args.exercise_resize:
        root.after(800, lambda: root.geometry("800x500"))
        root.after(1400, lambda: root.geometry("1100x700"))
    if args.auto_seconds > 0:
        root.after(int(args.auto_seconds * 1000), close)
    try:
        root.mainloop()
        report.update(
            {
                "hwnd": hwnd,
                "hwnd_class": hwnd_class,
                "load_history": load_history,
                "keyboard_events": keyboard_events,
                "final_properties": final_properties,
                "mpv_logs": logs,
                "queued_events": list(events.queue),
                "load_latency": _latency_summary(
                    [float(item["seconds"]) for item in load_history if item.get("ok")]
                ),
                "local_runtime_status": "pass" if load_history and load_history[-1].get("ok") else "fail",
            }
        )
    except Exception as exc:
        report.update({"local_runtime_status": "error", "error": repr(exc)})
        try:
            close()
        except Exception:
            pass

    local_status = report.get("local_runtime_status")
    if local_status == "fail":
        report.update({"status": "fail", "reason_codes": ["WID_LOAD_FAILED"]})
        exit_code = 1
    elif local_status == "error":
        report.update({"status": "error", "reason_codes": ["HARNESS_ERROR"]})
        exit_code = 3
    else:
        reason_codes = ["MANUAL_RESIZE_DPI_FOCUS_ATTESTATION_MISSING"]
        if environment.get("supply_chain_status") != "pass":
            reason_codes.insert(0, "UNVERIFIED_DLL_PROVENANCE")
        report.update(
            {
                "status": "blocked",
                "reason_codes": reason_codes,
            }
        )
        exit_code = 2
    _attach_run_manifest(report, run_path, run_manifest)
    _write_json(output, report)
    print(json.dumps({key: report.get(key) for key in ("status", "local_runtime_status", "hwnd", "hwnd_class", "load_history", "reason_codes")}, ensure_ascii=False, indent=2))
    print(f"report: {output.resolve()}")
    return exit_code


def _default_run_dir() -> Path:
    return CACHE_ROOT / _run_id()


def _cmd_baseline(args: argparse.Namespace) -> int:
    output = args.output or (_default_run_dir() / "baseline_manifest.json")
    manifest = capture_baseline(
        args.meta,
        output,
        full_video_hash=args.full_video_hash,
        reuse_video_hashes_from=args.reuse_video_hashes_from,
    )
    print(f"baseline: {output.resolve()}")
    print(f"samples: {len(manifest['samples'])}")
    return 0


def _cmd_build_edl(args: argparse.Namespace) -> int:
    output_dir = args.output_dir or (CACHE_ROOT / "generated")
    edl_path, report = build_edl(args.meta, output_dir)
    print(f"edl: {edl_path.resolve()}")
    print(json.dumps(report["mapping"], ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "pass" else 2


def _cmd_build_proxy_edl(args: argparse.Namespace) -> int:
    output_dir = args.output_dir or (CACHE_ROOT / "generated")
    edl_path, report = build_proxy_edl(
        args.meta,
        args.proxy_manifest,
        args.proxy_oracle,
        args.proxy_video_report,
        output_dir,
        proxy_gate_report_path=args.proxy_gate_report,
    )
    print(f"edl: {edl_path.resolve()}")
    print(json.dumps(report["mapping"], ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "pass" else 2


def _cmd_env(args: argparse.Namespace) -> int:
    output = args.output or (_default_run_dir() / "environment.json")
    run_path, run_manifest = create_run_manifest("env", args, output)
    report, _ = probe_mpv(
        args.mpv_dir,
        provenance=getattr(args, "provenance_manifest", None),
        source_url=getattr(args, "source_url", None),
        license_note=getattr(args, "license_note", None),
    )
    _attach_run_manifest(report, run_path, run_manifest)
    _write_json(output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    print(f"report: {output.resolve()}")
    return 0 if report["status"] == "pass" else 2


def _add_provenance_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--provenance-manifest",
        "--provenance",
        dest="provenance_manifest",
        type=Path,
        default=None,
        help=(
            "JSON provenance manifest binding the source archive, exact DLL, "
            "build record, license, and redistribution evidence files"
        ),
    )
    parser.add_argument(
        "--source-url",
        default=None,
        help="optional metadata cross-check; cannot replace --provenance-manifest",
    )
    parser.add_argument(
        "--license-note",
        default=None,
        help="optional metadata cross-check; cannot replace license evidence",
    )


def _add_run_binding_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--baseline-manifest",
        type=Path,
        default=None,
        help=(
            "baseline_manifest.json captured before the Gate run; missing or "
            "incomplete baselines keep the result exploratory"
        ),
    )
    parser.add_argument(
        "--threshold-manifest",
        type=Path,
        default=None,
        help=(
            "pre-registered mpv_phase0_thresholds JSON for this command"
        ),
    )
    parser.add_argument(
        "--run-manifest",
        type=Path,
        default=None,
        help=(
            "write-once run manifest path; defaults to a unique file beside "
            "the result report"
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Isolated libmpv Phase 0 utilities")
    sub = parser.add_subparsers(dest="command", required=True)

    baseline = sub.add_parser("baseline", help="capture a reproducible workspace baseline")
    baseline.add_argument(
        "--meta",
        type=Path,
        action="append",
        default=None,
        help="cache meta JSON; repeat for multiple samples",
    )
    baseline.add_argument("--output", type=Path, default=None)
    baseline.add_argument("--full-video-hash", action="store_true")
    baseline.add_argument(
        "--reuse-video-hashes-from",
        type=Path,
        default=None,
        help=(
            "reuse complete source-video SHA-256 values from an earlier baseline "
            "only when path, size, and mtime_ns are unchanged"
        ),
    )
    baseline.set_defaults(func=_cmd_baseline)

    edl = sub.add_parser("build-edl", help="normalize skip ranges and write a full EDL")
    edl.add_argument("--meta", type=Path, required=True)
    edl.add_argument("--output-dir", type=Path, default=None)
    edl.set_defaults(func=_cmd_build_edl)

    proxy_edl = sub.add_parser(
        "build-proxy-edl",
        help="write an experimental EDL from bound proxy PTS evidence",
    )
    proxy_edl.add_argument("--meta", type=Path, required=True)
    proxy_edl.add_argument("--proxy-manifest", type=Path, required=True)
    proxy_edl.add_argument("--proxy-oracle", type=Path, required=True)
    proxy_edl.add_argument("--proxy-video-report", type=Path, required=True)
    proxy_edl.add_argument("--proxy-gate-report", type=Path, default=None)
    proxy_edl.add_argument("--output-dir", type=Path, default=None)
    proxy_edl.set_defaults(func=_cmd_build_proxy_edl)

    env = sub.add_parser("env", help="probe python-mpv and an explicitly supplied DLL")
    env.add_argument("--mpv-dir", type=Path, default=None)
    _add_provenance_arguments(env)
    _add_run_binding_arguments(env)
    env.add_argument("--output", type=Path, default=None)
    env.set_defaults(func=_cmd_env)

    stress = sub.add_parser("stress", help="load a full EDL and seek across sampled boundaries")
    stress.add_argument("--meta", type=Path, required=True)
    stress.add_argument("--mpv-dir", type=Path, required=True)
    stress.add_argument("--samples", type=int, default=100)
    stress.add_argument("--timeout", type=float, default=10.0)
    _add_provenance_arguments(stress)
    _add_run_binding_arguments(stress)
    stress.add_argument("--output", type=Path, default=None)
    stress.set_defaults(func=_cmd_stress)

    lifecycle = sub.add_parser("lifecycle", help="repeat headless create/load/stop/terminate")
    lifecycle.add_argument("--meta", type=Path, required=True)
    lifecycle.add_argument("--mpv-dir", type=Path, required=True)
    lifecycle.add_argument("--repeat", type=int, default=3)
    lifecycle.add_argument("--timeout", type=float, default=10.0)
    _add_provenance_arguments(lifecycle)
    _add_run_binding_arguments(lifecycle)
    lifecycle.add_argument("--output", type=Path, default=None)
    lifecycle.set_defaults(func=_cmd_lifecycle)

    stepspeed = sub.add_parser(
        "stepspeed", help="G5: frame-step exactness, back-step latency, speeds"
    )
    stepspeed.add_argument("--video", type=Path, required=True)
    stepspeed.add_argument("--mpv-dir", type=Path, required=True)
    stepspeed.add_argument("--timeout", type=float, default=10.0)
    stepspeed.add_argument("--start-pos", type=float, default=60.0)
    stepspeed.add_argument("--step-repeats", type=int, default=20)
    stepspeed.add_argument("--cold-repeats", type=int, default=100)
    stepspeed.add_argument("--repeats", type=int, default=100)
    stepspeed.add_argument("--speeds", default="2,10,20,80")
    stepspeed.add_argument("--speed-ramp", type=float, default=0.5)
    stepspeed.add_argument("--speed-window", type=float, default=2.0)
    stepspeed.add_argument("--hwdec", default=None, help="e.g. auto / d3d11va / no")
    stepspeed.add_argument("--framedrop", default=None, help="e.g. decoder / vo / decoder+vo")
    _add_provenance_arguments(stepspeed)
    _add_run_binding_arguments(stepspeed)
    stepspeed.add_argument("--output", type=Path, default=None)
    stepspeed.set_defaults(func=_cmd_stepspeed)

    wid = sub.add_parser("wid", help="open an isolated Tk Frame with embedded libmpv")
    wid.add_argument("--meta", type=Path, required=True)
    wid.add_argument("--mpv-dir", type=Path, required=True)
    wid.add_argument("--geometry", default="960x600")
    wid.add_argument("--timeout", type=float, default=10.0)
    wid.add_argument("--auto-seconds", type=float, default=0.0)
    wid.add_argument("--exercise-resize", action="store_true")
    wid.add_argument("--switch-cycles", type=int, default=0)
    wid.add_argument("--switch-interval-ms", type=int, default=100)
    _add_provenance_arguments(wid)
    _add_run_binding_arguments(wid)
    wid.add_argument("--output", type=Path, default=None)
    wid.set_defaults(func=_cmd_wid)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "baseline" and args.meta is None:
        args.meta = list(DEFAULT_META_PATHS)
    try:
        return int(args.func(args))
    except (
        FileExistsError,
        FileNotFoundError,
        ValueError,
        AssertionError,
        json.JSONDecodeError,
    ) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
