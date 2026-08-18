#!/usr/bin/env python3
"""Verify the independent synthetic A/V sample-clock fixture."""
from __future__ import annotations

import argparse
from array import array
import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import generate_av_sync_fixtures as fixture_generator
import verify_mpv_frames as frame_oracle


EXIT_PASS = 0
EXIT_BLOCKED = 10
EXIT_FAILED = 20


class FixtureEvidenceError(ValueError):
    pass


def _load_truth(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FixtureEvidenceError(f"cannot read fixture truth: {exc}") from exc
    if not isinstance(value, dict) or value.get("kind") != fixture_generator.FIXTURE_KIND:
        raise FixtureEvidenceError("fixture truth kind is invalid")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decode_audio(ffmpeg: Path, video: Path, sample_rate: int, channels: int) -> list[int]:
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        str(video),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-f",
        "s16le",
        "-ar",
        str(sample_rate),
        "-ac",
        str(channels),
        "-acodec",
        "pcm_s16le",
        "pipe:1",
    ]
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise FixtureEvidenceError(
            "audio decode failed: "
            + completed.stderr.decode("utf-8", errors="replace")
        )
    if len(completed.stdout) % 2:
        raise FixtureEvidenceError("decoded PCM has an odd byte count")
    values = array("h")
    values.frombytes(completed.stdout)
    if sys.byteorder != "little":
        values.byteswap()
    if channels != 1:
        if len(values) % channels:
            raise FixtureEvidenceError("decoded PCM is not channel aligned")
        return [
            sum(values[index : index + channels]) // channels
            for index in range(0, len(values), channels)
        ]
    return list(values)


def _active_ranges(samples: Sequence[int], threshold: int) -> list[list[int]]:
    ranges: list[list[int]] = []
    start: int | None = None
    for index, value in enumerate(samples):
        active = abs(value) >= threshold
        if active and start is None:
            start = index
        elif not active and start is not None:
            ranges.append([start, index])
            start = None
    if start is not None:
        ranges.append([start, len(samples)])
    return ranges


def verify_fixture(
    video: Path,
    truth_path: Path,
    ffmpeg: Path,
    *,
    max_anchor_error_seconds: float = 0.010,
) -> dict[str, Any]:
    video = video.expanduser().resolve()
    truth_path = truth_path.expanduser().resolve()
    truth = _load_truth(truth_path)
    video_record = truth.get("video")
    if not isinstance(video_record, dict):
        raise FixtureEvidenceError("fixture truth video is invalid")
    if _sha256(video) != video_record.get("sha256"):
        raise FixtureEvidenceError("fixture video SHA-256 does not match truth")
    if not isinstance(max_anchor_error_seconds, (int, float)) or isinstance(
        max_anchor_error_seconds, bool
    ) or not math.isfinite(float(max_anchor_error_seconds)) or max_anchor_error_seconds <= 0:
        return {"status": "BLOCKED", "reason_codes": ["THRESHOLD_INVALID"]}

    video_truth_path = truth_path.with_name(f".{truth_path.name}.video.truth.json")
    video_truth = {
        "schema_version": 1,
        "kind": "mpv_phase0_pts_truth",
        "fixture_name": "av_sync_video",
        "video": {
            "path": video.name,
            "sha256": video_record["sha256"],
            "width": video_record["width"],
            "height": video_record["height"],
            "pixel_format": "gray",
        },
        "time_base": truth["video_time_base"],
        "id_encoding": fixture_generator.video_fixture.id_encoding(),
        "frames": [
            {
                "order": row["index"],
                "frame_id": row["frame_id"],
                "pts_ticks": row["pts_ticks"],
                "duration_ticks": row["duration_ticks"],
            }
            for row in truth["video_frames"]
        ],
        "terminal_guard": {
            "frame_id": fixture_generator.video_fixture.TERMINAL_GUARD_ID,
            "pts_ticks": truth["video_frames"][-1]["pts_ticks"]
            + truth["video_frames"][-1]["duration_ticks"],
            "may_be_present": True,
        },
    }
    video_truth_path.write_text(
        json.dumps(video_truth, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        video_report, video_exit = frame_oracle.probe_video(
            video,
            frame_oracle.FfmpegExecutable(ffmpeg.resolve(), "fixture"),
            truth_manifest=video_truth_path,
        )
    finally:
        video_truth_path.unlink(missing_ok=True)

    reasons: list[str] = []
    if video_exit != frame_oracle.EXIT_PASS or video_report.get("status") != "PASS":
        reasons.append("VIDEO_TRUTH_NOT_PASS")
    audio = truth.get("audio")
    if not isinstance(audio, dict):
        raise FixtureEvidenceError("fixture truth audio is invalid")
    samples = _decode_audio(
        ffmpeg.resolve(),
        video,
        int(audio["sample_rate"]),
        int(audio["channels"]),
    )
    if len(samples) != audio["total_samples"]:
        reasons.append("AUDIO_SAMPLE_COUNT_MISMATCH")
    actual_ranges = _active_ranges(samples, int(audio["pulse_amplitude"]) // 2)
    if actual_ranges != audio["pulse_ranges_samples"]:
        reasons.append("AUDIO_PULSE_RANGE_MISMATCH")

    tb = truth["video_time_base"]
    anchors: list[dict[str, Any]] = []
    pts_table = video_report.get("pts_table") or []
    for anchor in truth.get("anchors", []):
        frame_index = anchor["video_frame_index"]
        sample_index = anchor["audio_sample"]
        if frame_index >= len(pts_table):
            reasons.append("AV_ANCHOR_FRAME_MISSING")
            continue
        video_seconds = pts_table[frame_index]["pts"] * tb["numerator"] / tb["denominator"]
        audio_seconds = sample_index / audio["sample_rate"]
        error = abs(video_seconds - audio_seconds)
        anchors.append(
            {
                "video_frame_index": frame_index,
                "audio_sample": sample_index,
                "video_seconds": video_seconds,
                "audio_seconds": audio_seconds,
                "error_seconds": error,
            }
        )
        if error > max_anchor_error_seconds:
            reasons.append("AV_ANCHOR_ERROR_EXCEEDED")
    reasons = list(dict.fromkeys(reasons))
    return {
        "status": "PASS" if not reasons else "BLOCKED",
        "reason_codes": reasons,
        "video": {
            "status": video_report.get("status"),
            "exit_code": video_exit,
            "pts_table_count": len(pts_table),
        },
        "audio": {
            "sample_count": len(samples),
            "expected_sample_count": audio["total_samples"],
            "pulse_ranges": actual_ranges,
            "expected_pulse_ranges": audio["pulse_ranges_samples"],
        },
        "anchors": anchors,
        "thresholds": {"max_anchor_error_seconds": max_anchor_error_seconds},
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify a synthetic A/V sample-clock fixture")
    parser.add_argument("video", type=Path)
    parser.add_argument("truth_manifest", type=Path)
    parser.add_argument("--ffmpeg", type=Path, default=None)
    parser.add_argument("--max-anchor-error-seconds", type=float, default=0.010)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        ffmpeg = fixture_generator.video_fixture.resolve_ffmpeg(args.ffmpeg)
        report = verify_fixture(
            args.video,
            args.truth_manifest,
            ffmpeg,
            max_anchor_error_seconds=args.max_anchor_error_seconds,
        )
    except (FileNotFoundError, FixtureEvidenceError, OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAILED
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return EXIT_PASS if report["status"] == "PASS" else EXIT_BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())
