#!/usr/bin/env python3
"""Generate tiny CFR/VFR videos with lossless, machine-readable frame IDs.

The fixtures are deliberately independent of the production player. Each source
frame is unique, encoded as a binary ID in a gray pixel grid, and assigned an
explicit PTS in a 1/1000 time base. A final terminal guard frame gives MP4 a
known end timestamp; the guard is allowed to be absent from decoded output.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence


SCHEMA_VERSION = 1
TRUTH_KIND = "mpv_phase0_pts_truth"
WIDTH = 192
HEIGHT = 96
TIME_BASE_NUMERATOR = 1
TIME_BASE_DENOMINATOR = 1000
BIT_COUNT = 16
MARKER_BITS = [1, 0, 1, 0, 1, 0, 1, 0]
MARKER_X = 0
MARKER_Y = 0
MARKER_BLOCK_WIDTH = 8
MARKER_BLOCK_HEIGHT = 8
BIT_X = 64
BIT_Y = 8
BIT_WIDTH = 8
BIT_HEIGHT = 16
COMPLEMENT_Y = 32
PIXEL_BLACK = 16
PIXEL_WHITE = 235
PIXEL_THRESHOLD = 128
TERMINAL_GUARD_ID = 65535

CFR_PTS = [index * 40 for index in range(12)]
# Eleven gaps describe twelve real frames; the terminal guard repeats the
# final gap so the last real frame receives a positive duration in MP4.
VFR_DURATIONS = [40, 60, 20, 80, 50, 30, 70, 40, 90, 20, 60]
VFR_PTS = [0]
for _duration in VFR_DURATIONS:
    VFR_PTS.append(VFR_PTS[-1] + _duration)


def resolve_ffmpeg(requested: Path | None = None) -> Path:
    if requested is not None:
        path = requested.expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"FFmpeg executable not found: {path}")
        return path
    try:
        import imageio_ffmpeg  # type: ignore

        path = Path(imageio_ffmpeg.get_ffmpeg_exe()).resolve()
        if path.is_file():
            return path
    except Exception:
        pass
    discovered = shutil.which("ffmpeg")
    if discovered:
        return Path(discovered).resolve()
    raise FileNotFoundError("FFmpeg not found via imageio_ffmpeg or PATH")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fill_block(buffer: bytearray, x: int, y: int, width: int, height: int, value: int) -> None:
    for row in range(y, y + height):
        start = row * WIDTH + x
        buffer[start : start + width] = bytes([value]) * width


def frame_bytes(frame_id: int) -> bytes:
    if not 0 <= frame_id < 1 << BIT_COUNT:
        raise ValueError("frame_id does not fit the fixture bit width")
    buffer = bytearray([PIXEL_BLACK]) * (WIDTH * HEIGHT)
    for index, bit in enumerate(MARKER_BITS):
        _fill_block(
            buffer,
            MARKER_X + index * MARKER_BLOCK_WIDTH,
            MARKER_Y,
            MARKER_BLOCK_WIDTH,
            MARKER_BLOCK_HEIGHT,
            PIXEL_WHITE if bit else PIXEL_BLACK,
        )
    for index in range(BIT_COUNT):
        bit = (frame_id >> index) & 1
        value = PIXEL_WHITE if bit else PIXEL_BLACK
        complement = PIXEL_BLACK if bit else PIXEL_WHITE
        x = BIT_X + index * BIT_WIDTH
        _fill_block(buffer, x, BIT_Y, BIT_WIDTH, BIT_HEIGHT, value)
        _fill_block(buffer, x, COMPLEMENT_Y, BIT_WIDTH, BIT_HEIGHT, complement)
    return bytes(buffer)


def id_encoding() -> dict[str, Any]:
    return {
        "bit_count": BIT_COUNT,
        "threshold": PIXEL_THRESHOLD,
        "marker_bits": MARKER_BITS,
        "marker_x": MARKER_X,
        "marker_y": MARKER_Y,
        "marker_block_width": MARKER_BLOCK_WIDTH,
        "marker_block_height": MARKER_BLOCK_HEIGHT,
        "bit_x": BIT_X,
        "bit_y": BIT_Y,
        "bit_width": BIT_WIDTH,
        "bit_height": BIT_HEIGHT,
        "complement_y": COMPLEMENT_Y,
    }


def _setpts_expression(pts_ticks: Sequence[int]) -> str:
    expression = str(pts_ticks[-1])
    for index in range(len(pts_ticks) - 2, -1, -1):
        expression = f"if(eq(N,{index}),{pts_ticks[index]},{expression})"
    return expression


def build_ffmpeg_command(ffmpeg: Path, output: Path, pts_ticks: Sequence[int]) -> list[str]:
    # The last input frame is a terminal guard. Its timestamp supplies the
    # duration of the final real frame and may be omitted by the MP4 demuxer.
    source_pts = list(pts_ticks)
    if not source_pts:
        raise ValueError("at least one PTS is required")
    guard_pts = source_pts[-1] + (source_pts[-1] - source_pts[-2] if len(source_pts) > 1 else 40)
    all_pts = source_pts + [guard_pts]
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
        f"{WIDTH}x{HEIGHT}",
        "-framerate",
        "1000",
        "-i",
        "pipe:0",
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
        str(output),
    ]


def _write_video(ffmpeg: Path, output: Path, pts_ticks: Sequence[int], frame_ids: Sequence[int]) -> list[str]:
    command = build_ffmpeg_command(ffmpeg, output, pts_ticks)
    payload = b"".join(frame_bytes(frame_id) for frame_id in frame_ids)
    completed = subprocess.run(
        command,
        input=payload,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        diagnostic = completed.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"FFmpeg fixture generation failed ({completed.returncode}): {diagnostic}")
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError(f"FFmpeg did not create a non-empty fixture: {output}")
    return command


def _truth_manifest(
    *,
    name: str,
    video: Path,
    ffmpeg: Path,
    command: Sequence[str],
    pts_ticks: Sequence[int],
    frame_ids: Sequence[int],
) -> dict[str, Any]:
    guard_pts = pts_ticks[-1] + (pts_ticks[-1] - pts_ticks[-2] if len(pts_ticks) > 1 else 40)
    frames = []
    for index, (frame_id, pts) in enumerate(zip(frame_ids, pts_ticks)):
        next_pts = pts_ticks[index + 1] if index + 1 < len(pts_ticks) else guard_pts
        frames.append(
            {
                "order": index,
                "frame_id": frame_id,
                "pts_ticks": pts,
                "duration_ticks": next_pts - pts,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": TRUTH_KIND,
        "fixture_name": name,
        "video": {
            "path": video.name,
            "sha256": sha256_file(video),
            "width": WIDTH,
            "height": HEIGHT,
            "pixel_format": "gray",
        },
        "time_base": {"numerator": TIME_BASE_NUMERATOR, "denominator": TIME_BASE_DENOMINATOR},
        "id_encoding": id_encoding(),
        "frames": frames,
        "terminal_guard": {
            "frame_id": TERMINAL_GUARD_ID,
            "pts_ticks": guard_pts,
            "may_be_present": True,
        },
        "generation": {
            "ffmpeg_path": str(ffmpeg),
            "command": list(command),
            "source_frame_count": len(frame_ids) + 1,
            "source_frame_ids": list(frame_ids) + [TERMINAL_GUARD_ID],
        },
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def generate_fixture(
    output_dir: Path,
    *,
    name: str,
    pts_ticks: Sequence[int],
    id_start: int,
    ffmpeg: Path,
    force: bool,
) -> dict[str, Any]:
    video = output_dir / f"{name}.mp4"
    truth_path = output_dir / f"{name}.truth.json"
    if not force and (video.exists() or truth_path.exists()):
        raise FileExistsError(f"fixture exists; pass --force to replace: {video}")
    frame_ids = [id_start + index for index in range(len(pts_ticks))]
    output_dir.mkdir(parents=True, exist_ok=True)
    command = _write_video(ffmpeg, video, pts_ticks, frame_ids + [TERMINAL_GUARD_ID])
    truth = _truth_manifest(
        name=name,
        video=video,
        ffmpeg=ffmpeg,
        command=command,
        pts_ticks=pts_ticks,
        frame_ids=frame_ids,
    )
    write_json(truth_path, truth)
    return {"video": str(video), "truth_manifest": str(truth_path), "frame_count": len(frame_ids)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate CFR/VFR PTS-oracle fixtures")
    parser.add_argument("--output-dir", type=Path, default=Path(".cache/mpv_spike/pts_fixtures"))
    parser.add_argument("--ffmpeg", type=Path, default=None)
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        ffmpeg = resolve_ffmpeg(args.ffmpeg)
        output_dir = args.output_dir.expanduser().resolve()
        results = [
            generate_fixture(
                output_dir,
                name="cfr",
                pts_ticks=CFR_PTS,
                id_start=1000,
                ffmpeg=ffmpeg,
                force=args.force,
            ),
            generate_fixture(
                output_dir,
                name="vfr",
                pts_ticks=VFR_PTS,
                id_start=2000,
                ffmpeg=ffmpeg,
                force=args.force,
            ),
        ]
    except (FileNotFoundError, FileExistsError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"status": "PASS", "fixtures": results}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
