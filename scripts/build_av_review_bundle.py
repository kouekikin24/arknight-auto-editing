#!/usr/bin/env python3
"""Build bounded, review-only A/V diagnostics from an existing candidate report.

This tool deliberately does not run the candidate scanner and cannot create a
source observation, source anchor, proxy, or media-time authority.  It reuses
the already published schema-v3 candidate report only to locate a short set of
review windows.  Every result is marked as requiring external truth.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import scan_av_event_candidates as scan


SCHEMA_VERSION = 1
MANIFEST_KIND = "mpv_phase0_av_review_bundle"
REVIEW_STATUS = "REQUIRES_EXTERNAL_TRUTH"
DEFAULT_CONTEXT_SECONDS = 8.0
DEFAULT_SAMPLE_FPS = 10.0
CONTACT_SHEET_FPS = 2.0


class ReviewBundleError(ValueError):
    """The bounded review bundle cannot be published safely."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_record(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    return {"path": str(path), "size": path.stat().st_size, "sha256": _sha256(path)}


def _temporary_path(path: Path, suffix: str) -> Path:
    return path.with_name(f"{path.stem}.{uuid.uuid4().hex}.tmp{suffix}")


def _publish_file(temporary: Path, destination: Path) -> dict[str, Any]:
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"review artifact is write-once: {destination}")
    if not temporary.is_file() or temporary.stat().st_size == 0:
        raise ReviewBundleError(f"empty review artifact: {temporary}")
    os.link(temporary, destination)
    temporary.unlink(missing_ok=True)
    return _file_record(destination)


def _run(command: Sequence[str], *, label: str) -> None:
    completed = subprocess.run(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ReviewBundleError(f"{label} failed ({completed.returncode}): {detail}")


def _ffmpeg_record(ffmpeg: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [str(ffmpeg), "-version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if completed.returncode != 0 or not lines:
        raise ReviewBundleError("FFmpeg version probe failed")
    return {
        **_file_record(ffmpeg),
        "version_line": lines[0],
        "library_lines": [line for line in lines if line.startswith("lib")],
    }


def _load_candidate_report(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewBundleError(f"cannot read candidate report: {path}") from exc
    try:
        scan.validate_scan_report_schema(report)
    except Exception as exc:
        raise ReviewBundleError(f"candidate report validation failed: {exc}") from exc
    time_basis = report.get("time_basis", {})
    if time_basis.get("media_pts_authority") != "none":
        raise ReviewBundleError("candidate report unexpectedly grants media PTS authority")
    if time_basis.get("can_register_source_anchor_directly") is not False:
        raise ReviewBundleError("candidate report can register a source anchor directly")
    source = report.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("path"), str):
        raise ReviewBundleError("candidate report has no bound source")
    source_path = Path(source["path"]).expanduser().resolve()
    current_source = _file_record(source_path)
    if current_source["sha256"] != source.get("sha256") or current_source["size"] != source.get("size"):
        raise ReviewBundleError("candidate report source identity no longer matches")
    if not isinstance(report.get("window"), dict):
        raise ReviewBundleError("candidate report has no bounded window")
    return report, _file_record(path)


def _require_finite(value: Any, *, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ReviewBundleError(f"{name} must be numeric") from exc
    if not math.isfinite(result):
        raise ReviewBundleError(f"{name} must be finite")
    return result


def _window_groups(candidates: list[dict[str, Any]], context_seconds: float) -> list[dict[str, Any]]:
    if not candidates:
        return []
    ordered = sorted(
        enumerate(candidates),
        key=lambda item: _require_finite(
            item[1].get("requested_video_position_seconds"),
            name="candidate requested video position",
        ),
    )
    groups: list[list[tuple[int, dict[str, Any]]]] = []
    for item in ordered:
        if not groups:
            groups.append([item])
            continue
        previous_time = _require_finite(
            groups[-1][-1][1].get("requested_video_position_seconds"),
            name="candidate requested video position",
        )
        current_time = _require_finite(
            item[1].get("requested_video_position_seconds"),
            name="candidate requested video position",
        )
        if current_time - previous_time <= context_seconds / 2.0:
            groups[-1].append(item)
        else:
            groups.append([item])
    windows: list[dict[str, Any]] = []
    for index, group in enumerate(groups, start=1):
        times = [
            _require_finite(item[1]["requested_video_position_seconds"], name="candidate time")
            for item in group
        ]
        center = (min(times) + max(times)) / 2.0
        start = max(0.0, center - context_seconds / 2.0)
        windows.append(
            {
                "id": f"context_{index:02d}",
                "start_seconds": start,
                "duration_seconds": context_seconds,
                "candidate_indices": [item[0] for item in group],
                "candidate_requested_positions": times,
            }
        )
    return windows


def _clip_command(ffmpeg: Path, media: Path, start: float, duration: float, output: Path) -> list[str]:
    return [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-ss", f"{start:.6f}",
        "-i", str(media), "-t", f"{duration:.6f}", "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", "-y", str(output),
    ]


def _wav_command(ffmpeg: Path, media: Path, start: float, duration: float, output: Path) -> list[str]:
    return [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-ss", f"{start:.6f}",
        "-i", str(media), "-t", f"{duration:.6f}", "-map", "0:a:0", "-vn", "-sn", "-dn",
        "-ac", "1", "-ar", str(scan.AUDIO_RATE), "-c:a", "pcm_s16le", "-y", str(output),
    ]


def _waveform_command(ffmpeg: Path, wav: Path, output: Path) -> list[str]:
    return [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-i", str(wav),
        "-filter_complex", "showwavespic=s=1600x320:colors=0x2f80ed", "-frames:v", "1",
        "-y", str(output),
    ]


def _frame_summary(ffmpeg: Path, media: Path, start: float, duration: float, sample_fps: float) -> dict[str, Any]:
    command = scan.video_sample_command(ffmpeg, media, start, duration, sample_fps)
    completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ReviewBundleError(f"review video sampling failed ({completed.returncode}): {detail}")
    frame_bytes = scan.CONTACT_WIDTH * scan.CONTACT_HEIGHT * 3
    if len(completed.stdout) % frame_bytes:
        raise ReviewBundleError("review video sample is not frame aligned")
    frames = [
        completed.stdout[offset : offset + frame_bytes]
        for offset in range(0, len(completed.stdout), frame_bytes)
    ]
    scores = scan._visual_change_scores(frames)
    ranked = sorted(
        enumerate(scores), key=lambda item: float(item[1]), reverse=True
    )[:8]
    return {
        "frame_count": len(frames),
        "sample_fps": sample_fps,
        "thumbnail_size": [scan.CONTACT_WIDTH, scan.CONTACT_HEIGHT],
        "visual_change_max": max(scores, default=0.0),
        "visual_change_events": [
            {"offset_seconds": index / sample_fps, "score": score}
            for index, score in ranked
            if index > 0
        ],
        "command": command,
    }


def _audio_summary(ffmpeg: Path, media: Path, start: float, duration: float) -> dict[str, Any]:
    command = scan.audio_sample_command(ffmpeg, media, start, duration)
    completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    stderr = completed.stderr.decode("utf-8", errors="replace").strip()
    if completed.returncode != 0:
        return {
            "status": "NO_USABLE_AUDIO",
            "sample_rate": scan.AUDIO_RATE,
            "window_count": 0,
            "audio_peak_max": None,
            "audio_events": [],
            "stderr": stderr,
            "command": command,
        }
    metrics = scan._audio_window_metrics(completed.stdout)
    ranked = sorted(metrics, key=lambda item: float(item["peak"]), reverse=True)[:8]
    return {
        "status": "DECODED",
        "sample_rate": scan.AUDIO_RATE,
        "window_samples": scan.AUDIO_WINDOW_SAMPLES,
        "window_count": len(metrics),
        "audio_peak_max": max((int(item["peak"]) for item in metrics), default=0),
        "audio_rms_max": max((float(item["rms"]) for item in metrics), default=0.0),
        "audio_events": [
            {
                "offset_seconds": float(item["start_seconds"]),
                "peak": int(item["peak"]),
                "rms": float(item["rms"]),
                "start_sample": int(item["start_sample"]),
            }
            for item in ranked
        ],
        "command": command,
    }


def _signal_summary(video: dict[str, Any], audio: dict[str, Any]) -> dict[str, Any]:
    visual_events = video.get("visual_change_events", [])
    audio_events = audio.get("audio_events", [])
    pairs: list[dict[str, Any]] = []
    for visual in visual_events[:8]:
        if not audio_events:
            continue
        nearest = min(
            audio_events,
            key=lambda item: abs(float(item["offset_seconds"]) - float(visual["offset_seconds"])),
        )
        pairs.append(
            {
                "visual_offset_seconds": float(visual["offset_seconds"]),
                "audio_offset_seconds": float(nearest["offset_seconds"]),
                "sample_grid_delta_seconds": float(nearest["offset_seconds"])
                - float(visual["offset_seconds"]),
            }
        )
    return {
        "status": REVIEW_STATUS,
        "visual_signal_present": bool(visual_events),
        "audio_signal_present": bool(audio_events),
        "nearest_sample_grid_pairs": pairs,
        "requires_external_truth": True,
        "human_observation_required": True,
        "interpretation": "Signals are diagnostic only; no pair is a source anchor or media PTS.",
    }


def _build_window(
    *,
    ffmpeg: Path,
    media: Path,
    output_dir: Path,
    window: dict[str, Any],
    sample_fps: float,
) -> dict[str, Any]:
    start = float(window["start_seconds"])
    duration = float(window["duration_seconds"])
    name = str(window["id"])
    artifacts: dict[str, Any] = {}
    for extension, command_factory in (
        ("mp4", lambda path: _clip_command(ffmpeg, media, start, duration, path)),
        ("wav", lambda path: _wav_command(ffmpeg, media, start, duration, path)),
    ):
        destination = output_dir / f"{name}.{extension}"
        temporary = _temporary_path(destination, f".{extension}")
        try:
            _run(command_factory(temporary), label=f"{name} {extension} generation")
            artifacts[extension] = _publish_file(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    waveform_destination = output_dir / f"{name}_waveform.png"
    waveform_temporary = _temporary_path(waveform_destination, ".png")
    try:
        _run(
            _waveform_command(ffmpeg, Path(artifacts["wav"]["path"]), waveform_temporary),
            label=f"{name} waveform generation",
        )
        artifacts["waveform"] = _publish_file(waveform_temporary, waveform_destination)
    finally:
        waveform_temporary.unlink(missing_ok=True)
    contact_destination = output_dir / f"{name}_contact.png"
    contact_temporary = _temporary_path(contact_destination, ".png")
    try:
        _run(
            scan.contact_sheet_command(ffmpeg, media, start, duration, contact_temporary),
            label=f"{name} contact sheet generation",
        )
        artifacts["contact_sheet"] = _publish_file(contact_temporary, contact_destination)
    finally:
        contact_temporary.unlink(missing_ok=True)
    video = _frame_summary(ffmpeg, media, start, duration, sample_fps)
    audio = _audio_summary(ffmpeg, media, start, duration)
    return {
        "id": name,
        "scope": "review_only_source_window",
        "start_seconds": start,
        "duration_seconds": duration,
        "end_seconds_exclusive": start + duration,
        "candidate_indices": list(window.get("candidate_indices", [])),
        "candidate_requested_positions": list(window.get("candidate_requested_positions", [])),
        "artifacts": artifacts,
        "video_sampling": video,
        "audio_sampling": audio,
        "signal_summary": _signal_summary(video, audio),
    }


def build_review_bundle(
    candidate_report_path: Path,
    output_dir: Path,
    *,
    ffmpeg: Path,
    context_seconds: float = DEFAULT_CONTEXT_SECONDS,
    sample_fps: float = DEFAULT_SAMPLE_FPS,
) -> dict[str, Any]:
    if not math.isfinite(float(context_seconds)) or not (4.0 <= float(context_seconds) <= 10.0):
        raise ReviewBundleError("context_seconds must be between 4 and 10")
    if not math.isfinite(float(sample_fps)) or not (1.0 <= float(sample_fps) <= 10.0):
        raise ReviewBundleError("sample_fps must be between 1 and 10")
    output_dir = output_dir.expanduser().resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"review bundle directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    report, report_record = _load_candidate_report(candidate_report_path)
    ffmpeg = ffmpeg.expanduser().resolve()
    if not ffmpeg.is_file():
        raise FileNotFoundError(ffmpeg)
    media = Path(report["source"]["path"]).expanduser().resolve()
    ffmpeg_record = _ffmpeg_record(ffmpeg)
    source_record_before = _file_record(media)
    candidates = report.get("candidates", [])
    if not isinstance(candidates, list):
        raise ReviewBundleError("candidate report candidates must be a list")
    source_window = report["window"]
    full = {
        "id": "full_window",
        "start_seconds": _require_finite(source_window["requested_start_seconds"], name="full start"),
        "duration_seconds": _require_finite(source_window["requested_duration_seconds"], name="full duration"),
        "candidate_indices": list(range(len(candidates))),
        "candidate_requested_positions": [
            _require_finite(item["requested_video_position_seconds"], name="candidate time")
            for item in candidates
        ],
    }
    windows = [full] + _window_groups(candidates, float(context_seconds))
    built_windows = [
        _build_window(
            ffmpeg=ffmpeg,
            media=media,
            output_dir=output_dir,
            window=window,
            sample_fps=float(sample_fps),
        )
        for window in windows
    ]
    source_record_after = _file_record(media)
    report_record_after = _file_record(Path(report_record["path"]))
    if source_record_before != source_record_after or report_record != report_record_after:
        raise ReviewBundleError("source or candidate report changed during review generation")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "kind": MANIFEST_KIND,
        "status": REVIEW_STATUS,
        "scope": "review_only",
        "source": source_record_after,
        "candidate_report": report_record_after,
        "time_basis": {
            "media_pts_authority": "none",
            "can_register_source_anchor_directly": False,
            "requested_seek_and_sample_grid_only": True,
        },
        "promotion_contract": {
            "production_consumer_allowed": False,
            "gate_approval": False,
            "source_observation_created": False,
            "source_anchor_created": False,
            "proxy_created": False,
            "requires_external_truth": True,
        },
        "windows": built_windows,
        "tools": {
            "review_builder": _file_record(Path(__file__).resolve()),
            "ffmpeg": ffmpeg_record,
            "scanner_report_reused_without_rerun": True,
        },
        "parameters": {
            "context_seconds": float(context_seconds),
            "sample_fps": float(sample_fps),
            "review_transcode": "libx264+aac for human review only",
        },
        "publication_identity_check": {
            "status": "PASSED",
            "checked": ["source", "candidate_report"],
        },
    }
    manifest_path = output_dir / "review_bundle.json"
    scan.write_json_new(manifest_path, manifest)
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--context-seconds", type=float, default=DEFAULT_CONTEXT_SECONDS)
    parser.add_argument("--sample-fps", type=float, default=DEFAULT_SAMPLE_FPS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = build_review_bundle(
            args.candidate_report,
            args.output_dir,
            ffmpeg=args.ffmpeg,
            context_seconds=args.context_seconds,
            sample_fps=args.sample_fps,
        )
    except (ReviewBundleError, FileExistsError, FileNotFoundError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 20
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "scope": manifest["scope"],
                "windows": [window["id"] for window in manifest["windows"]],
                "output_dir": str(Path(args.output_dir).expanduser().resolve()),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 10


if __name__ == "__main__":
    raise SystemExit(main())
