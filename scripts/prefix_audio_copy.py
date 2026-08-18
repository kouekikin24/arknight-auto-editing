#!/usr/bin/env python3
"""Build and verify a bounded prefix proxy with packet-copied AAC audio.

This is a Phase 0 experiment only.  A prefix result is never Gate PASS: it is
useful for proving audio-stream preservation and sample-domain binding before a
full source run is considered.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import pts_normalized_proxy as proxy_verifier
import verify_mpv_frames as frame_oracle
import verify_proxy_audio_av as audio_verifier


MANIFEST_SCHEMA_VERSION = 1
REPORT_SCHEMA_VERSION = 2
SCHEMA_VERSION = MANIFEST_SCHEMA_VERSION
MANIFEST_KIND = "mpv_phase0_prefix_audio_copy_manifest"
REPORT_KIND = "mpv_phase0_prefix_audio_copy_report"
EXIT_PASS = 0
EXIT_BLOCKED = 10
EXIT_FAILED = 20
DEFAULT_MAX_ANCHOR_ERROR_SECONDS = 0.010
DEFAULT_MAX_IDENTITY_OFFSET_SPAN_SECONDS = 0.010


class PrefixEvidenceError(ValueError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise PrefixEvidenceError(f"JSON evidence must be an object: {path}")
    return value


def _write_json_new(path: Path, value: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _bound_file(record: dict[str, Any], name: str) -> Path:
    path_value = record.get("path")
    sha_value = record.get("sha256")
    if not isinstance(path_value, str) or not isinstance(sha_value, str):
        raise PrefixEvidenceError(f"{name} path/sha256 binding is invalid")
    path = Path(path_value).expanduser().resolve()
    if not path.is_file() or _sha256(path).lower() != sha_value.lower():
        raise PrefixEvidenceError(f"{name} SHA-256 binding is invalid")
    return path


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.expanduser().resolve())) == os.path.normcase(
        str(right.expanduser().resolve())
    )


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PrefixEvidenceError(f"{name} must be an object")
    return value


def _mux_window(timeline: dict[str, Any]) -> dict[str, Any]:
    time_base = _require_mapping(timeline.get("time_base"), "normalized time base")
    numerator = time_base.get("numerator")
    denominator = time_base.get("denominator")
    business_end_ticks = timeline.get("business_pts_end_exclusive")
    duration_ticks = timeline.get("duration_ticks")
    for value, name in (
        (numerator, "time base numerator"),
        (denominator, "time base denominator"),
        (business_end_ticks, "business end"),
        (duration_ticks, "frame duration"),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise PrefixEvidenceError(f"{name} must be a positive integer")
    if timeline.get("business_pts_start") != 0:
        raise PrefixEvidenceError("normalized business timeline must start at zero")
    # The guard packet starts exactly at business_end_ticks.  FFmpeg's -t is
    # exclusive, so one media tick keeps that packet without reaching the next
    # AAC packet boundary.
    mux_end_exclusive_ticks = business_end_ticks + 1
    return {
        "business_end_ticks": business_end_ticks,
        "business_end_seconds": business_end_ticks * numerator / denominator,
        "mux_end_exclusive_ticks": mux_end_exclusive_ticks,
        "mux_end_exclusive_seconds": mux_end_exclusive_ticks
        * numerator
        / denominator,
    }


def _build_mux_command(
    ffmpeg: Path,
    video_path: Path,
    source_path: Path,
    output_path: Path,
    *,
    mux_end_exclusive_seconds: float,
) -> list[str]:
    return [
        str(ffmpeg.resolve()),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-copyts",
        "-i",
        str(video_path),
        "-i",
        str(source_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "copy",
        "-avoid_negative_ts",
        "disabled",
        "-t",
        f"{mux_end_exclusive_seconds:.12f}",
        str(output_path),
    ]


def build_prefix(
    video_manifest_path: Path,
    output_path: Path,
    manifest_path: Path,
    ffmpeg: Path,
) -> dict[str, Any]:
    video_manifest_path = video_manifest_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    manifest_path = manifest_path.expanduser().resolve()
    if output_path.exists() or manifest_path.exists():
        raise FileExistsError("prefix audio-copy evidence is write-once")
    video_manifest = _load_json(video_manifest_path)
    if (
        video_manifest.get("schema_version") != proxy_verifier.SCHEMA_VERSION
        or video_manifest.get("kind") != proxy_verifier.MANIFEST_KIND
    ):
        raise PrefixEvidenceError("video manifest schema or kind is invalid")
    source = video_manifest.get("source")
    video_record = video_manifest.get("proxy")
    if not isinstance(source, dict) or not isinstance(video_record, dict):
        raise PrefixEvidenceError("video manifest source/proxy is invalid")
    source_path = _bound_file(source, "source video")
    video_path = _bound_file(video_record, "video-only proxy")
    if not isinstance(source.get("scope"), str) or source.get("scope") not in ("prefix", "full"):
        raise PrefixEvidenceError("video source scope is invalid")
    timeline = _require_mapping(
        video_manifest.get("normalized_timeline"), "normalized timeline"
    )
    mux_window = _mux_window(timeline)
    guard = _require_mapping(video_manifest.get("terminal_guard"), "terminal guard")
    if (
        guard.get("pts_ticks") != mux_window["business_end_ticks"]
        or guard.get("included_in_business_domain") is not False
    ):
        raise PrefixEvidenceError("terminal guard is not outside the business domain")
    temporary = output_path.with_name(f".{output_path.stem}.{uuid.uuid4().hex}.tmp{output_path.suffix}")
    command = _build_mux_command(
        ffmpeg,
        video_path,
        source_path,
        temporary,
        mux_end_exclusive_seconds=mux_window["mux_end_exclusive_seconds"],
    )
    completed = subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(
            "AAC packet-copy mux failed: "
            + completed.stderr.decode("utf-8", errors="replace")
        )
    if not temporary.is_file() or temporary.stat().st_size == 0:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("AAC packet-copy mux produced no output")
    try:
        os.link(temporary, output_path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    temporary.unlink()
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "kind": MANIFEST_KIND,
        "created_utc": _utc_now(),
        "status": "BUILT_UNVERIFIED",
        "scope": source["scope"],
        "source": {
            "path": str(source_path),
            "sha256": _sha256(source_path),
            "size": source_path.stat().st_size,
            "business_frame_count": source.get("business_frame_count"),
            "scope": source.get("scope"),
            "oracle": source.get("oracle"),
        },
        "video_manifest": {
            "path": str(video_manifest_path),
            "sha256": _sha256(video_manifest_path),
        },
        "video_proxy": video_record,
        "proxy": {
            "path": str(output_path),
            "sha256": _sha256(output_path),
            "size": output_path.stat().st_size,
        },
        "normalized_timeline": copy.deepcopy(timeline),
        "audio_route": {
            "name": "packet_copy",
            "source_stream": "1:a:0",
            "codec_copy": True,
            "command": command,
            **mux_window,
        },
        "tools": {
            "ffmpeg": {
                "path": str(ffmpeg.resolve()),
                "sha256": _sha256(ffmpeg),
            },
            "prefix_audio_copy": {
                "path": str(Path(__file__).resolve()),
                "sha256": _sha256(Path(__file__)),
            },
        },
    }
    _write_json_new(manifest_path, manifest)
    return manifest


def _audio_prefix_compare(source: dict[str, Any], proxy: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    if source.get("status") != "PASS":
        reasons.extend(f"SOURCE_{value}" for value in source.get("reason_codes", []))
    if proxy.get("status") != "PASS":
        reasons.extend(f"PROXY_{value}" for value in proxy.get("reason_codes", []))
    source_format = source.get("format") or {}
    proxy_format = proxy.get("format") or {}
    if source_format != proxy_format:
        reasons.append("AUDIO_FORMAT_MISMATCH")
    source_rows = (source.get("frames") or {}).get("pts_table", [])
    proxy_rows = (proxy.get("frames") or {}).get("pts_table", [])
    if not proxy_rows or len(proxy_rows) >= len(source_rows):
        reasons.append("AUDIO_PREFIX_SCOPE_INVALID")
    if len(proxy_rows) <= len(source_rows):
        for index, row in enumerate(proxy_rows):
            source_row = source_rows[index]
            for field in ("pts", "nb_samples", "rate", "channels", "chlayout", "checksum"):
                if row.get(field) != source_row.get(field):
                    reasons.append("AUDIO_PREFIX_PCM_OR_PTS_MISMATCH")
                    break
    reasons = list(dict.fromkeys(reasons))
    return {
        "status": "PASS" if not reasons else "BLOCKED",
        "reason_codes": reasons,
        "source_frames": len(source_rows),
        "proxy_frames": len(proxy_rows),
        "source_samples": (source.get("frames") or {}).get("total_samples"),
        "proxy_samples": (proxy.get("frames") or {}).get("total_samples"),
        "source_start_time": (source.get("frames") or {}).get("start_time"),
        "proxy_start_time": (proxy.get("frames") or {}).get("start_time"),
        "source_end_time": (source.get("frames") or {}).get("end_time"),
        "proxy_end_time": (proxy.get("frames") or {}).get("end_time"),
    }


def _identity_offset(video_manifest: dict[str, Any], source_rows: list[dict[str, Any]]) -> dict[str, Any]:
    timeline = video_manifest["normalized_timeline"]
    time_base = timeline["time_base"]
    duration_ticks = int(timeline["duration_ticks"])
    numerator = int(time_base["numerator"])
    denominator = int(time_base["denominator"])
    return audio_verifier.normalization_offset_summary(
        source_rows,
        duration_ticks=duration_ticks,
        numerator=numerator,
        denominator=denominator,
    )


def verify_prefix(
    manifest_path: Path,
    oracle_output: Path | None,
    output_path: Path,
    ffmpeg: Path,
    *,
    proxy_oracle_path: Path | None = None,
    content_anchor_manifest_path: Path | None = None,
    max_anchor_error_seconds: float = DEFAULT_MAX_ANCHOR_ERROR_SECONDS,
    max_identity_offset_span_seconds: float = DEFAULT_MAX_IDENTITY_OFFSET_SPAN_SECONDS,
) -> dict[str, Any]:
    manifest_path = manifest_path.expanduser().resolve()
    oracle_output = oracle_output.expanduser().resolve() if oracle_output is not None else None
    proxy_oracle_path = (
        proxy_oracle_path.expanduser().resolve()
        if proxy_oracle_path is not None
        else None
    )
    output_path = output_path.expanduser().resolve()
    threshold_reasons = audio_verifier._threshold_reason_codes(
        max_anchor_error_seconds,
        max_identity_offset_span_seconds,
    )
    if threshold_reasons:
        raise PrefixEvidenceError(
            "invalid A/V thresholds: " + ", ".join(threshold_reasons)
        )
    if (oracle_output is None) == (proxy_oracle_path is None):
        raise PrefixEvidenceError(
            "choose exactly one of a new oracle output or an existing proxy oracle"
        )
    if content_anchor_manifest_path is not None and proxy_oracle_path is None:
        raise PrefixEvidenceError(
            "content-anchor verification requires an existing bound proxy oracle"
        )
    if (oracle_output is not None and oracle_output.exists()) or output_path.exists():
        raise FileExistsError("prefix audio-copy verification is write-once")
    manifest = _load_json(manifest_path)
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("kind") != MANIFEST_KIND:
        raise PrefixEvidenceError("prefix manifest kind is invalid")
    source = manifest.get("source")
    proxy = manifest.get("proxy")
    if not isinstance(source, dict) or not isinstance(proxy, dict):
        raise PrefixEvidenceError("prefix source/proxy binding is invalid")
    source_path = _bound_file(source, "source video")
    proxy_path = _bound_file(proxy, "audio proxy")
    video_manifest_record = manifest.get("video_manifest")
    if not isinstance(video_manifest_record, dict):
        raise PrefixEvidenceError("video manifest binding is missing")
    video_manifest_path = _bound_file(video_manifest_record, "video manifest")
    video_manifest = _load_json(video_manifest_path)
    if (
        video_manifest.get("schema_version") != proxy_verifier.SCHEMA_VERSION
        or video_manifest.get("kind") != proxy_verifier.MANIFEST_KIND
    ):
        raise PrefixEvidenceError("bound video manifest schema or kind is invalid")
    bound_source = video_manifest.get("source")
    if not isinstance(bound_source, dict):
        raise PrefixEvidenceError("bound video manifest source is invalid")
    if (
        manifest.get("scope") != bound_source.get("scope")
        or source.get("scope") != bound_source.get("scope")
    ):
        raise PrefixEvidenceError("prefix scope differs from bound video manifest")
    if not _same_path(Path(str(bound_source.get("path", ""))), source_path):
        raise PrefixEvidenceError("source path differs from bound video manifest")
    if bound_source.get("sha256") != source.get("sha256"):
        raise PrefixEvidenceError("source SHA differs from bound video manifest")
    bound_video_proxy = video_manifest.get("proxy")
    if not isinstance(bound_video_proxy, dict):
        raise PrefixEvidenceError("bound video manifest proxy is invalid")
    bound_video_proxy_path = _bound_file(bound_video_proxy, "video-only proxy")
    if manifest.get("video_proxy") != bound_video_proxy:
        raise PrefixEvidenceError("video proxy binding differs from bound video manifest")
    if manifest.get("normalized_timeline") != video_manifest.get("normalized_timeline"):
        raise PrefixEvidenceError("normalized timeline differs from bound video manifest")
    tools = _require_mapping(manifest.get("tools"), "prefix tools")
    bound_ffmpeg = _bound_file(
        _require_mapping(tools.get("ffmpeg"), "FFmpeg tool"), "FFmpeg tool"
    )
    if not _same_path(bound_ffmpeg, ffmpeg):
        raise PrefixEvidenceError("FFmpeg path differs from bound tool")
    bound_builder = _bound_file(
        _require_mapping(tools.get("prefix_audio_copy"), "prefix audio-copy tool"),
        "prefix audio-copy tool",
    )
    if not _same_path(bound_builder, Path(__file__)):
        raise PrefixEvidenceError("prefix audio-copy tool path differs from verifier")
    route = _require_mapping(manifest.get("audio_route"), "audio route")
    if route.get("name") != "packet_copy" or route.get("codec_copy") is not True:
        raise PrefixEvidenceError("audio route is not packet-copy")
    mux_window = _mux_window(
        _require_mapping(video_manifest.get("normalized_timeline"), "normalized timeline")
    )
    if (
        route.get("business_end_ticks") != mux_window["business_end_ticks"]
        or route.get("mux_end_exclusive_ticks") != mux_window["mux_end_exclusive_ticks"]
    ):
        raise PrefixEvidenceError("audio route end is not bound to the video timeline")
    command = route.get("command")
    expected_prefix = _build_mux_command(
        ffmpeg,
        bound_video_proxy_path,
        source_path,
        proxy_path,
        mux_end_exclusive_seconds=mux_window["mux_end_exclusive_seconds"],
    )
    if not isinstance(command, list) or command[:-1] != expected_prefix[:-1]:
        raise PrefixEvidenceError("audio route command differs from bound inputs")
    if not isinstance(command[-1], str):
        raise PrefixEvidenceError("audio route temporary output is invalid")
    temporary_output = Path(command[-1]).expanduser().resolve()
    if (
        temporary_output.parent != proxy_path.parent
        or not temporary_output.name.startswith(f".{proxy_path.stem}.")
        or not temporary_output.name.endswith(f".tmp{proxy_path.suffix}")
    ):
        raise PrefixEvidenceError("audio route temporary output is outside the bound target")
    source_oracle_record = source.get("oracle")
    if not isinstance(source_oracle_record, dict):
        raise PrefixEvidenceError("source oracle binding is missing")
    source_oracle_path, source_oracle = audio_verifier._load_bound_oracle(
        source_oracle_record,
        "source video oracle",
        source_path,
        str(source["sha256"]),
    )
    source_rows = source_oracle.get("pts_table")
    if not isinstance(source_rows, list) or not source_rows:
        raise PrefixEvidenceError("source oracle has no pts_table")

    ffmpeg = ffmpeg.expanduser().resolve()
    if proxy_oracle_path is not None:
        proxy_oracle_record = {
            "path": str(proxy_oracle_path),
            "sha256": _sha256(proxy_oracle_path),
        }
        try:
            bound_proxy_oracle_path, oracle_report = audio_verifier._load_bound_oracle(
                proxy_oracle_record,
                "existing proxy oracle",
                proxy_path,
                str(proxy["sha256"]),
            )
        except audio_verifier.AudioEvidenceError as exc:
            raise PrefixEvidenceError(str(exc)) from exc
    else:
        assert oracle_output is not None
        oracle_report, _oracle_exit = frame_oracle.probe_video(
            proxy_path, frame_oracle.FfmpegExecutable(ffmpeg, "--ffmpeg")
        )
        _write_json_new(oracle_output, oracle_report)
        bound_proxy_oracle_path = oracle_output
    evaluation_manifest = copy.deepcopy(video_manifest)
    evaluation_manifest["proxy"] = proxy
    video_validation = proxy_verifier.evaluate_video_proxy(
        evaluation_manifest, source_rows, oracle_report
    )
    source_audio = audio_verifier.decode_audio(source_path, ffmpeg)
    proxy_audio = audio_verifier.decode_audio(proxy_path, ffmpeg)
    audio_prefix = _audio_prefix_compare(source_audio, proxy_audio)
    content_anchor_evidence = audio_verifier.evaluate_bound_content_anchor_manifest(
        content_anchor_manifest_path,
        scope=str(manifest.get("scope")),
        subject_manifest_path=manifest_path,
        timeline_manifest_path=video_manifest_path,
        source_media_path=source_path,
        proxy_media_path=proxy_path,
        source_oracle_path=source_oracle_path,
        proxy_oracle_path=bound_proxy_oracle_path,
        source_oracle=source_oracle,
        proxy_oracle=oracle_report,
        source_audio=source_audio,
        proxy_audio=proxy_audio,
        max_error_seconds=max_anchor_error_seconds,
        expected_ffmpeg_path=ffmpeg,
    )
    identity = _identity_offset(evaluation_manifest, source_rows)
    source_pts_identity = audio_verifier.evaluate_source_pts_identity(
        identity,
        max_offset_span_seconds=max_identity_offset_span_seconds,
    )
    av_sync_reasons: list[str] = []
    # The generic oracle intentionally returns BLOCKED for the terminal
    # zero-duration guard.  The dedicated proxy evaluator decides whether
    # that violation is confined to the guard outside the business domain.
    if video_validation.get("status") != "PASS":
        av_sync_reasons.append("VIDEO_VALIDATION_NOT_PASS")
    if audio_prefix.get("status") != "PASS":
        av_sync_reasons.extend(
            audio_prefix.get("reason_codes") or ["AUDIO_PREFIX_NOT_PASS"]
        )
    # Thresholds were validated before any evidence or media was opened.
    timeline = evaluation_manifest["normalized_timeline"]
    end_seconds = int(timeline["business_pts_end_exclusive"]) * int(timeline["time_base"]["numerator"]) / int(timeline["time_base"]["denominator"])
    proxy_audio_end = audio_prefix.get("proxy_end_time")
    proxy_audio_start = audio_prefix.get("proxy_start_time")
    boundary_anchors = {
        "start_error_seconds": abs(proxy_audio_start) if isinstance(proxy_audio_start, (int, float)) else None,
        "end_error_seconds": abs(proxy_audio_end - end_seconds) if isinstance(proxy_audio_end, (int, float)) else None,
        "normalized_video_start_seconds": 0.0,
        "normalized_video_end_seconds": end_seconds,
        "proxy_audio_start_seconds": proxy_audio_start,
        "proxy_audio_end_seconds": proxy_audio_end,
        "max_error_seconds": max_anchor_error_seconds,
    }
    boundary_reasons: list[str] = []
    if boundary_anchors["start_error_seconds"] is None or boundary_anchors["start_error_seconds"] > max_anchor_error_seconds:
        boundary_reasons.append("AV_START_ANCHOR_ERROR_EXCEEDED")
    if boundary_anchors["end_error_seconds"] is None or boundary_anchors["end_error_seconds"] > max_anchor_error_seconds:
        boundary_reasons.append("AV_END_ANCHOR_ERROR_EXCEEDED")
    boundary_anchors["status"] = "PRESERVED" if not boundary_reasons else "CHANGED"
    boundary_anchors["gate_effect"] = "diagnostic_only"
    boundary_anchors["reason_codes"] = boundary_reasons
    if content_anchor_evidence.get("status") not in ("PASS", "NOT_APPLICABLE_PASS"):
        av_sync_reasons.extend(
            content_anchor_evidence.get("reason_codes")
            or ["AV_CONTENT_ANCHORS_NOT_PASS"]
        )
    av_sync_reasons = list(dict.fromkeys(av_sync_reasons))
    reasons = list(av_sync_reasons)
    if manifest.get("scope") != "full":
        reasons.append("SOURCE_PREFIX_ONLY")
    if manifest.get("scope") != "prefix":
        reasons.append("AUDIO_PREFIX_SCOPE_NOT_PREFIX")
    reasons = list(dict.fromkeys(reasons))
    diagnostic_codes = list(
        dict.fromkeys(source_pts_identity.get("reason_codes", []) + boundary_reasons)
    )
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "kind": REPORT_KIND,
        "created_utc": _utc_now(),
        "status": "PASS" if not reasons else "BLOCKED",
        "scope": manifest.get("scope"),
        "reason_codes": reasons,
        "diagnostic_codes": diagnostic_codes,
        "video_validation": video_validation,
        "audio_timeline": audio_prefix,
        "normalization_offset": identity,
        "source_pts_identity": source_pts_identity,
        "av_sync": {
            "status": "PASS" if not av_sync_reasons else "BLOCKED",
            "reason_codes": av_sync_reasons,
            "diagnostic_codes": diagnostic_codes,
            "components": {
                "video_validation": video_validation.get("status"),
                "audio_prefix": audio_prefix.get("status"),
                "content_anchors": content_anchor_evidence.get("status"),
                "thresholds": "PASS" if not threshold_reasons else "BLOCKED",
            },
            "timestamp_boundaries": boundary_anchors,
            "content_anchors": content_anchor_evidence,
            "thresholds": {
                "max_anchor_error_seconds": max_anchor_error_seconds,
                "max_identity_offset_span_seconds": max_identity_offset_span_seconds,
            },
        },
        "bindings": {
            "manifest": {"path": str(manifest_path), "sha256": _sha256(manifest_path)},
            "source": {"path": str(source_path), "sha256": _sha256(source_path)},
            "proxy": {"path": str(proxy_path), "sha256": _sha256(proxy_path)},
            "source_oracle": {"path": str(source_oracle_path), "sha256": _sha256(source_oracle_path)},
            "proxy_oracle": {
                "path": str(bound_proxy_oracle_path),
                "sha256": _sha256(bound_proxy_oracle_path),
            },
            "content_anchor_manifest": content_anchor_evidence.get("manifest"),
            "content_anchor_observation": content_anchor_evidence.get("observation"),
            "ffmpeg": {"path": str(ffmpeg), "sha256": _sha256(ffmpeg)},
            "verifier": {"path": str(Path(__file__).resolve()), "sha256": _sha256(Path(__file__))},
        },
        "exit_code": EXIT_PASS if not reasons else EXIT_BLOCKED,
    }
    _write_json_new(output_path, report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build or verify a prefix AAC packet-copy proxy")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("video_manifest", type=Path)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--manifest", type=Path, required=True)
    build.add_argument("--ffmpeg", type=Path, default=None)
    verify = sub.add_parser("verify")
    verify.add_argument("manifest", type=Path)
    oracle = verify.add_mutually_exclusive_group(required=True)
    oracle.add_argument("--oracle-output", type=Path)
    oracle.add_argument("--proxy-oracle", type=Path)
    verify.add_argument("--content-anchor-manifest", type=Path)
    verify.add_argument("--output", type=Path, required=True)
    verify.add_argument("--ffmpeg", type=Path, default=None)
    verify.add_argument("--max-anchor-error-seconds", type=float, default=DEFAULT_MAX_ANCHOR_ERROR_SECONDS)
    verify.add_argument("--max-identity-offset-span-seconds", type=float, default=DEFAULT_MAX_IDENTITY_OFFSET_SPAN_SECONDS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        ffmpeg = frame_oracle.resolve_ffmpeg(args.ffmpeg).path
        if args.command == "build":
            result = build_prefix(args.video_manifest, args.output, args.manifest, ffmpeg)
        else:
            result = verify_prefix(
                args.manifest,
                args.oracle_output,
                args.output,
                ffmpeg,
                proxy_oracle_path=args.proxy_oracle,
                content_anchor_manifest_path=args.content_anchor_manifest,
                max_anchor_error_seconds=args.max_anchor_error_seconds,
                max_identity_offset_span_seconds=args.max_identity_offset_span_seconds,
            )
    except (FileNotFoundError, FileExistsError, OSError, PrefixEvidenceError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAILED
    print(json.dumps({"status": result["status"], "reason_codes": result.get("reason_codes", [])}, ensure_ascii=False, sort_keys=True))
    return result.get("exit_code", EXIT_PASS)


if __name__ == "__main__":
    raise SystemExit(main())
