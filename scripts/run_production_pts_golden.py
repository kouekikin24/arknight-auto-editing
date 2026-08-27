"""Run a bounded production PTS/A-V export golden.

The source must be supplied explicitly.  This command is intentionally a
short-fixture harness; it refuses to infer a source from the four real MPV
samples and publishes every result once into a new output directory.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

# Keep both direct-file and module entry points anchored at the repository root.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from frame_pts_certifier import produce_frame_pts_certification
from media_exporter import ExportRequest, MediaExporter
from media_info import MediaInfo, probe_media
from timeline_plan import TimelinePlan

from scripts.record_gate_decision import file_record, write_once


def _version_line(path: Path) -> str:
    completed = subprocess.run(
        [str(path), "-version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise RuntimeError(f"cannot read tool version: {path}")
    return completed.stdout.splitlines()[0]


def _probe_json(source: Path) -> dict[str, Any]:
    """Read container/stream metadata via PyAV, ffprobe-shaped (ffprobe.exe retired)."""
    from media_info import _pyav_probe_payload

    return _pyav_probe_payload(source.expanduser().resolve())


def run_golden(
    source: Path,
    output_dir: Path,
    *,
    ffmpeg: Path,
) -> dict[str, Any]:
    source = source.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    ffmpeg = ffmpeg.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if not ffmpeg.is_file():
        raise FileNotFoundError("explicit FFmpeg executable is incomplete")
    if output_dir.exists():
        raise FileExistsError(f"golden output is write-once: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=False)

    media = probe_media(source, ffmpeg_path=ffmpeg)
    certification = produce_frame_pts_certification(
        media,
        cache_root=output_dir / "frame_pts_evidence",
    )
    if not certification.certified:
        raise RuntimeError(
            "short production golden source is not certifiable: "
            + ",".join(certification.reason_codes)
        )

    certified_media: MediaInfo = certification.media_info
    certified = certified_media.frame_pts_certification
    assert certified is not None
    frame_count = certified.frame_count
    if frame_count < 6:
        raise RuntimeError("production golden requires at least six certified frames")
    delete_range = (frame_count // 3, frame_count // 3 + 2)
    plan = TimelinePlan.from_deleted_ranges(frame_count, [delete_range])
    output_path = output_dir / "export.mp4"
    request = ExportRequest.full(
        source,
        output_path,
        plan,
        fps=30,
        quality=8,
        media_info=certified_media,
        ffmpeg_path=str(ffmpeg),
        include_audio=True,
    )
    result = MediaExporter().export(request)

    from scripts import verify_mpv_frames

    output_oracle, output_exit = verify_mpv_frames.probe_video(
        output_path,
        verify_mpv_frames.FfmpegExecutable(ffmpeg, "production_pts_golden"),
        threads=1,
        max_frames=None,
    )
    decoded_frames = output_oracle.get("showinfo", {}).get("parsed_frames")
    if (
        output_exit != verify_mpv_frames.EXIT_PASS
        or output_oracle.get("status") != "PASS"
        or decoded_frames != result.written_frames
    ):
        raise RuntimeError("exported output PTS oracle did not pass")
    probe_output = _probe_json(output_path)

    write_once(output_dir / "output_pts_oracle.json", output_oracle)
    write_once(output_dir / "output_probe.json", probe_output)
    manifest = {
        "schema_version": 1,
        "kind": "production_pts_golden",
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": file_record(source, repo_root=Path(__file__).resolve().parents[1]),
        "tools": {
            "ffmpeg": {
                **file_record(ffmpeg, repo_root=Path(__file__).resolve().parents[1]),
                "version_line": _version_line(ffmpeg),
            },
        },
        "media_info": certified_media.as_dict(),
        "frame_pts_certification": certified.as_dict(),
        "timeline": {
            "total_frames": plan.total_frames,
            "delete_ranges": [list(delete_range)],
            "kept_ranges": [list(value) for value in plan.kept_ranges],
            "fingerprint": plan.fingerprint,
        },
        "export": {
            "output": file_record(output_path, repo_root=output_dir),
            "written_frames": result.written_frames,
            "total_frames": result.total_frames,
            "metadata": dict(result.metadata),
        },
        "output_evidence": {
            "pts_oracle": file_record(output_dir / "output_pts_oracle.json", repo_root=output_dir),
            "probe": file_record(output_dir / "output_probe.json", repo_root=output_dir),
        },
        "status": "PASS",
        "pts_table_consumed": result.metadata.get("pts_table_consumed") is True,
        "audio_mode": result.metadata.get("audio_mode"),
    }
    if not manifest["pts_table_consumed"]:
        raise RuntimeError("production golden did not consume the certified PTS table")
    write_once(output_dir / "manifest.json", manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    args = parser.parse_args(argv)
    manifest = run_golden(
        args.source,
        args.output_dir,
        ffmpeg=args.ffmpeg,
    )
    print(json.dumps({"status": manifest["status"], "output_dir": str(args.output_dir.resolve())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
