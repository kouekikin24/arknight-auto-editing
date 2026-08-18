#!/usr/bin/env python3
"""Capture one auditable video/audio window for a Phase 0 content anchor."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import verify_mpv_frames as frame_oracle
import verify_proxy_audio_av as verifier


EXIT_PASS = 0
EXIT_FAILED = 20


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise verifier.AudioEvidenceError(f"{name} must be an object")
    return value


def _run_capture(command: list[str], output: Path, name: str) -> None:
    completed = subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"{name} capture failed: {detail or completed.returncode}")


def _temporary_capture_path(output: Path) -> Path:
    return output.with_name(
        f".{output.stem}.{uuid.uuid4().hex}.capture{output.suffix}"
    )


def _unlink_owned_publication(temporary: Path, output: Path) -> None:
    try:
        if temporary.is_file() and output.is_file() and temporary.samefile(output):
            output.unlink()
    except OSError:
        pass


def capture_window(
    media_path: Path,
    oracle_path: Path,
    video_output: Path,
    audio_output: Path,
    manifest_output: Path,
    ffmpeg: frame_oracle.FfmpegExecutable,
    *,
    side: str,
    scope: str,
    frame_index: int,
    audio_sample: int,
    window_radius_samples: int,
) -> dict[str, Any]:
    media_path = media_path.expanduser().resolve()
    oracle_path = oracle_path.expanduser().resolve()
    video_output = video_output.expanduser().resolve()
    audio_output = audio_output.expanduser().resolve()
    manifest_output = manifest_output.expanduser().resolve()
    if side not in ("source", "proxy") or scope not in ("prefix", "full"):
        raise verifier.AudioEvidenceError("capture side or scope is invalid")
    if (
        isinstance(frame_index, bool)
        or not isinstance(frame_index, int)
        or frame_index < 0
        or isinstance(audio_sample, bool)
        or not isinstance(audio_sample, int)
        or audio_sample < 0
        or isinstance(window_radius_samples, bool)
        or not isinstance(window_radius_samples, int)
        or window_radius_samples <= 0
    ):
        raise verifier.AudioEvidenceError("capture frame/sample/window values are invalid")
    for output in (video_output, audio_output, manifest_output):
        if output.exists():
            raise FileExistsError(f"capture evidence is write-once: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
    if not media_path.is_file() or not oracle_path.is_file():
        raise FileNotFoundError("capture media or oracle is missing")

    media_record = verifier._file_record(media_path, include_size=True)
    oracle_record = verifier._file_record(oracle_path)
    _, oracle = verifier._load_bound_oracle(
        oracle_record,
        f"{side} capture oracle",
        media_path,
        media_record["sha256"],
    )
    rows = oracle.get("pts_table")
    if not isinstance(rows, list) or frame_index >= len(rows):
        raise verifier.AudioEvidenceError("capture video frame is absent from the oracle")
    video_row = _require_mapping(rows[frame_index], "capture video frame")
    checksum = video_row.get("checksum")
    if not isinstance(checksum, str) or not checksum:
        raise verifier.AudioEvidenceError("capture video frame checksum is missing")

    audio = verifier.decode_audio(media_path, ffmpeg.path)
    if audio.get("status") != "PASS":
        raise verifier.AudioEvidenceError(
            "capture audio evidence is not decodable: "
            + ", ".join(audio.get("reason_codes", []))
        )
    audio_frame = verifier._audio_frame_for_sample(audio, audio_sample)
    if audio_frame is None:
        raise verifier.AudioEvidenceError("capture sample has no decoded audio frame")
    frames = _require_mapping(audio.get("frames"), "capture audio frames")
    decoded_start = frames.get("start_pts")
    decoded_end = frames.get("end_pts_exclusive")
    if not isinstance(decoded_start, int) or not isinstance(decoded_end, int):
        raise verifier.AudioEvidenceError("capture decoded audio range is missing")
    start_sample = max(decoded_start, audio_sample - window_radius_samples)
    end_sample = min(decoded_end, audio_sample + window_radius_samples + 1)
    if start_sample > audio_sample or audio_sample >= end_sample:
        raise verifier.AudioEvidenceError("capture audio window does not contain its anchor")

    temporary_video = _temporary_capture_path(video_output)
    temporary_audio = _temporary_capture_path(audio_output)
    video_command = verifier.content_anchor_video_capture_command(
        ffmpeg.path, media_path, frame_index, temporary_video
    )
    audio_command = verifier.content_anchor_audio_capture_command(
        ffmpeg.path,
        media_path,
        decoded_start_sample=start_sample - decoded_start,
        decoded_end_sample_exclusive=end_sample - decoded_start,
        output=temporary_audio,
    )
    published: list[tuple[Path, Path]] = []
    try:
        _run_capture(video_command, temporary_video, "video frame")
        _run_capture(audio_command, temporary_audio, "audio window")
        verifier._validate_window_artifact_file(
            {
                **verifier._file_record(temporary_video, include_size=True),
                "media_type": "image/png",
            },
            "captured video frame",
            media_type="image/png",
        )
        verifier._validate_window_artifact_file(
            {
                **verifier._file_record(temporary_audio, include_size=True),
                "media_type": "audio/wav",
            },
            "captured audio window",
            media_type="audio/wav",
        )
        os.link(temporary_video, video_output)
        published.append((temporary_video, video_output))
        os.link(temporary_audio, audio_output)
        published.append((temporary_audio, audio_output))
        manifest = {
            "schema_version": verifier.CONTENT_ANCHOR_SCHEMA_VERSION,
            "kind": verifier.CONTENT_ANCHOR_WINDOW_KIND,
            "created_utc": verifier._utc_now(),
            "side": side,
            "scope": scope,
            "media": media_record,
            "oracle": oracle_record,
            "video_frame": {"index": frame_index, "checksum": checksum.upper()},
            "audio_frame": {
                field: audio_frame.get(field)
                for field in ("n", "pts", "nb_samples", "checksum")
            },
            "audio_window": {
                "stream": "0:a:0",
                "sample_rate": (audio.get("format") or {}).get("sample_rate"),
                "anchor_sample": audio_sample,
                "start_sample": start_sample,
                "end_sample_exclusive": end_sample,
            },
            "visual_artifact": {
                **verifier._file_record(video_output, include_size=True),
                "media_type": "image/png",
            },
            "audio_artifact": {
                **verifier._file_record(audio_output, include_size=True),
                "media_type": "audio/wav",
            },
            "capture": {
                "tool": verifier._file_record(Path(__file__)),
                "ffmpeg": verifier._file_record(ffmpeg.path),
                "commands": {"video": video_command, "audio": audio_command},
                "publication": {
                    "method": "hardlink_create_new",
                    "visual_target": str(video_output),
                    "audio_target": str(audio_output),
                },
            },
        }
        verifier.write_json_new(manifest_output, manifest)
        return manifest
    except BaseException:
        for temporary, output in reversed(published):
            _unlink_owned_publication(temporary, output)
        raise
    finally:
        temporary_video.unlink(missing_ok=True)
        temporary_audio.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("media", type=Path)
    parser.add_argument("--oracle", type=Path, required=True)
    parser.add_argument("--side", choices=("source", "proxy"), required=True)
    parser.add_argument("--scope", choices=("prefix", "full"), required=True)
    parser.add_argument("--frame-index", type=int, required=True)
    parser.add_argument("--audio-sample", type=int, required=True)
    parser.add_argument("--window-radius-samples", type=int, default=2048)
    parser.add_argument("--video-output", type=Path, required=True)
    parser.add_argument("--audio-output", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        ffmpeg = frame_oracle.resolve_ffmpeg(args.ffmpeg)
        manifest = capture_window(
            args.media,
            args.oracle,
            args.video_output,
            args.audio_output,
            args.manifest_output,
            ffmpeg,
            side=args.side,
            scope=args.scope,
            frame_index=args.frame_index,
            audio_sample=args.audio_sample,
            window_radius_samples=args.window_radius_samples,
        )
    except (
        FileExistsError,
        FileNotFoundError,
        OSError,
        RuntimeError,
        ValueError,
        verifier.AudioEvidenceError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAILED
    print(
        json.dumps(
            {
                "status": "CAPTURED",
                "side": manifest["side"],
                "video_frame": manifest["video_frame"],
                "manifest": str(args.manifest_output.expanduser().resolve()),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return EXIT_PASS


if __name__ == "__main__":
    raise SystemExit(main())
