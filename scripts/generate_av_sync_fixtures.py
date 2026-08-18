#!/usr/bin/env python3
"""Generate a small, lossless audio/video clock fixture.

The video uses the existing unique-pixel-frame fixture encoding.  The audio is
Uncompressed PCM in MP4 so its sample clock and pulse ranges are lossless and
can be checked independently from the video PTS clock.
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from typing import Any, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import generate_pts_oracle_fixtures as video_fixture


SCHEMA_VERSION = 1
FIXTURE_KIND = "mpv_phase0_av_sync_truth"
TIME_BASE_NUMERATOR = 1
TIME_BASE_DENOMINATOR = 1000
VIDEO_PTS_TICKS = [index * 40 for index in range(12)]
AUDIO_SAMPLE_RATE = 48000
AUDIO_CHANNELS = 1
AUDIO_TOTAL_SAMPLES = 23040
PULSE_RANGES_SAMPLES = [[3840, 4320], [13440, 13920]]
PULSE_AMPLITUDE = 20000


def _setpts_expression(pts_ticks: Sequence[int]) -> str:
    expression = str(pts_ticks[-1])
    for index in range(len(pts_ticks) - 2, -1, -1):
        expression = f"if(eq(N,{index}),{pts_ticks[index]},{expression})"
    return expression


def _audio_samples() -> list[int]:
    samples = [0] * AUDIO_TOTAL_SAMPLES
    for start, end in PULSE_RANGES_SAMPLES:
        for index in range(start, end):
            samples[index] = PULSE_AMPLITUDE
    return samples


def _write_wav(path: Path) -> None:
    samples = _audio_samples()
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(AUDIO_CHANNELS)
        stream.setsampwidth(2)
        stream.setframerate(AUDIO_SAMPLE_RATE)
        stream.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def build_ffmpeg_command(ffmpeg: Path, audio_path: Path, output: Path) -> list[str]:
    guard_pts = VIDEO_PTS_TICKS[-1] + (VIDEO_PTS_TICKS[-1] - VIDEO_PTS_TICKS[-2])
    all_pts = [*VIDEO_PTS_TICKS, guard_pts]
    return [
        str(ffmpeg),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pixel_format",
        "gray",
        "-video_size",
        f"{video_fixture.WIDTH}x{video_fixture.HEIGHT}",
        "-framerate",
        "1000",
        "-i",
        "pipe:0",
        "-i",
        str(audio_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-vf",
        f"settb=1/1000,setpts='{_setpts_expression(all_pts)}'",
        "-fps_mode",
        "passthrough",
        "-c:v",
        "libx264",
        "-qp",
        "0",
        "-bf",
        "0",
        "-pix_fmt",
        "yuv444p",
        "-enc_time_base",
        "1/1000",
        "-video_track_timescale",
        "1000",
        "-c:a",
        "pcm_s16le",
        "-ar",
        str(AUDIO_SAMPLE_RATE),
        "-ac",
        str(AUDIO_CHANNELS),
        "-shortest",
        str(output),
    ]


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def generate_fixture(
    output_dir: Path,
    *,
    ffmpeg: Path,
    force: bool = False,
) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    video = output_dir / "av_sync.mp4"
    truth_path = output_dir / "av_sync.truth.json"
    if not force and (video.exists() or truth_path.exists()):
        raise FileExistsError(f"fixture exists; pass --force: {video}")
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix="av_sync_audio_", suffix=".wav", dir=output_dir, delete=False
    ) as temporary_audio:
        audio_path = Path(temporary_audio.name)
    try:
        _write_wav(audio_path)
        command = build_ffmpeg_command(ffmpeg, audio_path, video)
        payload = b"".join(
            video_fixture.frame_bytes(frame_id)
            for frame_id in [*range(1000, 1012), video_fixture.TERMINAL_GUARD_ID]
        )
        completed = subprocess.run(
            command,
            input=payload,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "FFmpeg A/V fixture generation failed: "
                + completed.stderr.decode("utf-8", errors="replace")
            )
        if not video.is_file() or video.stat().st_size == 0:
            raise RuntimeError("FFmpeg did not create the A/V fixture")
    finally:
        audio_path.unlink(missing_ok=True)

    guard_pts = VIDEO_PTS_TICKS[-1] + (VIDEO_PTS_TICKS[-1] - VIDEO_PTS_TICKS[-2])
    truth = {
        "schema_version": SCHEMA_VERSION,
        "kind": FIXTURE_KIND,
        "video": {
            "path": video.name,
            "sha256": video_fixture.sha256_file(video),
            "pixel_format": "yuv444p",
            "width": video_fixture.WIDTH,
            "height": video_fixture.HEIGHT,
        },
        "video_time_base": {
            "numerator": TIME_BASE_NUMERATOR,
            "denominator": TIME_BASE_DENOMINATOR,
        },
        "video_frames": [
            {
                "index": index,
                "frame_id": 1000 + index,
                "pts_ticks": pts,
                "duration_ticks": (
                    VIDEO_PTS_TICKS[index + 1] - pts
                    if index + 1 < len(VIDEO_PTS_TICKS)
                    else guard_pts - pts
                ),
            }
            for index, pts in enumerate(VIDEO_PTS_TICKS)
        ],
        "audio": {
            "codec": "pcm_s16le",
            "sample_rate": AUDIO_SAMPLE_RATE,
            "channels": AUDIO_CHANNELS,
            "total_samples": AUDIO_TOTAL_SAMPLES,
            "pulse_ranges_samples": PULSE_RANGES_SAMPLES,
            "pulse_amplitude": PULSE_AMPLITUDE,
        },
        "anchors": [
            {"video_frame_index": 2, "audio_sample": PULSE_RANGES_SAMPLES[0][0]},
            {"video_frame_index": 7, "audio_sample": PULSE_RANGES_SAMPLES[1][0]},
        ],
        "generation": {"ffmpeg_command": command},
    }
    _write_json(truth_path, truth)
    return {"video": str(video), "truth_manifest": str(truth_path)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a lossless A/V clock fixture")
    parser.add_argument("--output-dir", type=Path, default=Path(".cache/mpv_spike/av_fixtures"))
    parser.add_argument("--ffmpeg", type=Path, default=None)
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        ffmpeg = video_fixture.resolve_ffmpeg(args.ffmpeg)
        result = generate_fixture(args.output_dir, ffmpeg=ffmpeg, force=args.force)
    except (FileNotFoundError, FileExistsError, OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"status": "PASS", **result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
