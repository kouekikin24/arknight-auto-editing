"""End-to-end certified export for the real sample recordings.

Chain per sample: repo FFmpeg pair -> MediaInfo -> full FramePtsCertification
(with B' head-anomaly adjudication) -> TimelinePlan from the real business
skip segments -> MediaExporter full export -> ffprobe frame-count verdict
against the adjudicated expectation.

Only reads existing skip-segment evidence (.cache/preview_fluency); the
oracle decode itself is the certification scan and is expected to take
minutes for the large samples.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

FFMPEG = (
    REPO
    / "tools"
    / "ffmpeg-7.1.0"
    / "bundle"
    / "ffmpeg-7.1-essentials_build"
    / "bin"
    / "ffmpeg.exe"
)
FFPROBE = FFMPEG.with_name("ffprobe.exe")

SAMPLES = {
    1: Path(r"D:\qq下载\920\1.mp4"),
    2: Path(r"D:\qq下载\920\2.mp4"),
    3: Path(r"D:\qq下载\920\3.mp4"),
    4: Path(r"D:\qq下载\920\4.mp4"),
}


def _count_output_frames(output: Path) -> int:
    result = subprocess.run(
        [
            str(FFPROBE),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-count_frames",
            "-show_entries",
            "stream=nb_read_frames",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(output),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr[:500]}")
    return int(result.stdout.strip())


def run(stem: int, output_root: Path, decode_threads: int, quality: int, *, include_audio: bool = True) -> dict:
    import frame_pts_certifier
    import media_info
    from media_exporter import ExportRequest, MediaExporter
    from timeline_plan import TimelinePlan

    source = SAMPLES[stem]
    if not source.is_file():
        raise FileNotFoundError(source)
    skip_segs = json.loads(
        (REPO / ".cache" / "preview_fluency" / f"{stem}_skip_segs.json").read_text(
            encoding="utf-8"
        )
    )
    meta = json.loads(
        (REPO / ".cache" / "preview_fluency" / f"{stem}_meta.json").read_text(
            encoding="utf-8"
        )
    )
    fps = float(meta["fps"])

    print(f"[{stem}] probe_media ...", flush=True)
    media = media_info.probe_media(
        source, ffprobe_path=FFPROBE, ffmpeg_path=FFMPEG
    )
    print(
        f"[{stem}] probed: {len(media.video_streams)} video, "
        f"{len(media.audio_streams)} audio streams",
        flush=True,
    )

    print(f"[{stem}] full-decode PTS certification (threads={decode_threads}) ...", flush=True)
    outcome = frame_pts_certifier.produce_frame_pts_certification(
        media, decode_threads=decode_threads
    )
    print(
        f"[{stem}] certification status={outcome.status} "
        f"reasons={outcome.reason_codes} evidence={outcome.evidence_path}",
        flush=True,
    )
    if not outcome.certified:
        raise RuntimeError(
            f"sample {stem} did not certify: {outcome.status} {outcome.reason_codes}"
        )
    certified = outcome.media_info

    # Business skip segments are already half-open [start, end):
    # sum(end - start) == skip_frames_sum for every baseline sample.
    deleted = [(int(s), int(e)) for s, e in skip_segs]
    plan = TimelinePlan.from_deleted_ranges(certified.frame_pts_certification.frame_count, deleted)
    expected_written = sum(end - start for start, end in plan.kept_ranges)

    output_dir = output_root / f"{stem}"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{stem}_certified_export.mp4"
    request = ExportRequest.full(
        source,
        output,
        plan,
        fps=fps,
        quality=quality,
        media_info=certified,
        include_audio=bool(certified.has_audio and include_audio),
        ffmpeg_path=str(FFMPEG),
        ffprobe_path=str(FFPROBE),
    )
    print(f"[{stem}] exporting {len(plan.kept_ranges)} kept ranges "
          f"({expected_written} frames) ...", flush=True)
    try:
        result = MediaExporter().export(request)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        print(f"[{stem}] FFMPEG FAILED rc={exc.returncode}\n{stderr}", flush=True)
        raise

    adjudication = result.metadata.get("head_anomaly_adjudication", {})
    drops = adjudication.get("tick_collision_drops", [])
    expected_on_disk = adjudication.get(
        "expected_on_disk_frames", expected_written
    )
    print(f"[{stem}] export done: written={result.written_frames} "
          f"expected={expected_written} drops={drops}", flush=True)

    print(f"[{stem}] counting output frames with ffprobe ...", flush=True)
    actual_frames = _count_output_frames(output)
    verdict = "PASS" if actual_frames == expected_on_disk else "FAIL"

    manifest = {
        "kind": "real_sample_certified_export",
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "stem": stem,
        "source": str(source),
        "source_sha256": certified.source_sha256,
        "certification_status": outcome.status,
        "certification_evidence": str(outcome.evidence_path),
        "kept_ranges": len(plan.kept_ranges),
        "expected_written": expected_written,
        "head_anomaly_adjudication": adjudication,
        "expected_on_disk_frames": expected_on_disk,
        "actual_on_disk_frames": actual_frames,
        "verdict": verdict,
        "output": str(output),
        "output_size": output.stat().st_size,
        "audio_mode": result.metadata.get("audio_mode"),
        "pts_table_consumed": result.metadata.get("pts_table_consumed"),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[{stem}] verdict={verdict} frames={actual_frames}/"
          f"{expected_on_disk} manifest={manifest_path}", flush=True)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stems", default="3", help="comma-separated sample stems")
    parser.add_argument(
        "--output-root",
        default=str(REPO / "PRODUCTION_REAL_SAMPLE_20260816"),
    )
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--quality", type=int, default=17)
    parser.add_argument("--no-audio", action="store_true")
    args = parser.parse_args()

    output_root = Path(args.output_root)
    stems = [int(item) for item in args.stems.split(",") if item.strip()]
    manifests = {}
    for stem in stems:
        manifests[stem] = run(stem, output_root, args.threads, args.quality, include_audio=not args.no_audio)
    failed = [stem for stem, m in manifests.items() if m["verdict"] != "PASS"]
    print(json.dumps({k: v["verdict"] for k, v in manifests.items()}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
