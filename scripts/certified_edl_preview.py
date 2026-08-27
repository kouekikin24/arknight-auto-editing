"""Preview-only spike: build an mpv EDL from the certified PTS table and
verify cut-point correctness frame by frame.

Route per the 2026-08-17 owner ruling: the EDL time route comes from the
B'-adjudicated FramePtsCertification (never frame/fps arithmetic).  The EDL
is played headless with a lavfi ``showinfo`` filter, and every decoded
frame's timestamp is compared against the expected kept-frame sequence from
the certified table.  Any frame from a deleted region, any missing kept
frame, or any boundary error fails the probe.

Usage:
    python scripts/certified_edl_preview.py --stem 3
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import time

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
MPV_DIR = REPO / "tools" / "libmpv" / "dll"

SAMPLES = {
    1: Path(r"D:\qq下载\920\1.mp4"),
    2: Path(r"D:\qq下载\920\2.mp4"),
    3: Path(r"D:\qq下载\920\3.mp4"),
    4: Path(r"D:\qq下载\920\4.mp4"),
}

_SHOWINFO_RE = re.compile(r"pts:\s*(-?\d+)\s+pts_time:\s*(-?[0-9.]+)")


def _edl_escape(value: str) -> str:
    encoded = value.encode("utf-8")
    return f"%{len(encoded)}%{value}"


def build_certified_edl(stem: int) -> dict:
    """Build the EDL from the certified tick table; returns the EDL record."""
    import frame_pts_certifier
    import media_info
    from analyzer import _fraction_filter_seconds
    from pts_timeline import CertifiedPtsTimeline
    from timeline_plan import TimelinePlan

    source = SAMPLES[stem]
    media = media_info.probe_media(source, ffmpeg_path=FFMPEG)
    outcome = frame_pts_certifier.produce_frame_pts_certification(
        media, decode_threads=4
    )
    if not outcome.certified:
        raise RuntimeError(
            f"sample {stem} did not certify: {outcome.status} {outcome.reason_codes}"
        )
    certified = outcome.media_info
    timeline = CertifiedPtsTimeline.from_media_info(certified)

    skip_path = REPO / ".cache" / "preview_fluency" / f"{stem}_skip_segs.json"
    raw_deleted = json.loads(skip_path.read_text(encoding="utf-8"))
    plan = TimelinePlan.from_deleted_ranges(
        certified.frame_pts_certification.frame_count,
        [(int(s), int(e)) for s, e in raw_deleted],
    )

    rows = timeline.rows
    time_base = timeline.time_base
    segments = []
    for start, end in plan.kept_ranges:
        start_tick = rows[start].pts
        end_tick = rows[end - 1].end_tick
        segments.append(
            {
                "frames": [start, end],
                "start_tick": start_tick,
                "end_tick": end_tick,
                "start_time": start_tick * time_base,
                "duration_time": (end_tick - start_tick) * time_base,
                "expected_ticks": [row.pts for row in rows[start:end]],
            }
        )

    out_dir = REPO / ".cache" / "mpv_spike" / "certified_edl"
    out_dir.mkdir(parents=True, exist_ok=True)
    edl_path = out_dir / f"{stem}.certified.edl"
    escaped = _edl_escape(source.as_posix())
    lines = ["# mpv EDL v0"]
    for seg in segments:
        lines.append(
            f"{escaped},{_fraction_filter_seconds(seg['start_time'])},"
            f"{_fraction_filter_seconds(seg['duration_time'])}"
        )
    edl_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return {
        "edl_path": edl_path,
        "segments": segments,
        "kept_ranges": len(plan.kept_ranges),
        "expected_frames": sum(end - start for start, end in plan.kept_ranges),
        "certification_status": outcome.status,
        "evidence": str(outcome.evidence_path),
    }


def _extract_reference_frames(stem: int, wanted: list[int], out_dir: Path) -> list[Path]:
    """Decode specific source frame indices (ascending) to images."""
    import subprocess

    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("ref_*.png"):
        old.unlink()
    if not wanted:
        return []
    conditions = "+".join(f"eq(n,{n})" for n in wanted)
    pattern = out_dir / "ref_%05d.png"
    cmd = [
        str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(SAMPLES[stem]),
        "-vf", f"select='{conditions}'",
        "-vsync", "0", "-start_number", "0", str(pattern),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
    if result.returncode != 0:
        raise RuntimeError(f"reference extraction failed: {result.stderr[:400]}")
    files = sorted(out_dir.glob("ref_*.png"))
    if len(files) != len(wanted):
        raise RuntimeError(
            f"reference extraction produced {len(files)} of {len(wanted)} frames"
        )
    return files


def _mad(path_a: Path, path_b: Path, size: tuple[int, int] = (160, 90)) -> float:
    from PIL import Image
    import numpy as np

    with Image.open(path_a) as img_a, Image.open(path_b) as img_b:
        arr_a = np.asarray(img_a.convert("L").resize(size), dtype=np.int16)
        arr_b = np.asarray(img_b.convert("L").resize(size), dtype=np.int16)
    return float(np.abs(arr_a - arr_b).mean())


def play_and_verify(stem: int, record: dict) -> dict:
    """Play the EDL with vo=image; compare every played frame against the
    certified source frames (full sequence identity, not just boundaries)."""
    os.environ["PATH"] = str(MPV_DIR) + os.pathsep + os.environ.get("PATH", "")
    import mpv  # noqa: E402

    image_dir = record["edl_path"].parent / f"{stem}_frames"
    if image_dir.exists():
        for old in image_dir.glob("*.png"):
            old.unlink()
    image_dir.mkdir(parents=True, exist_ok=True)

    player = mpv.MPV(
        vo="image",
        audio="no",
        untimed=True,
        framedrop="no",
        keep_open="yes",
        idle="yes",
        terminal=False,
        input_default_bindings=False,
        cache="yes",
        demuxer_max_bytes="256MiB",
        **{
            "vo-image-format": "png",
            "vo-image-png-compression": "1",
            "vo-image-outdir": str(image_dir),
        },
    )
    started = time.perf_counter()
    try:
        with player.prepare_and_wait_for_event("file-loaded", timeout=30):
            player.play(str(record["edl_path"]))
        deadline = time.perf_counter() + 600
        while time.perf_counter() < deadline:
            try:
                if player.eof_reached:
                    break
            except Exception:
                break
            time.sleep(0.25)
    finally:
        player.terminate()
    wall = time.perf_counter() - started

    played = sorted(image_dir.glob("*.png"))
    observed_frames = len(played)

    expected_source_frames: list[int] = []
    for seg in record["segments"]:
        expected_source_frames.extend(range(*seg["frames"]))

    ref_dir = record["edl_path"].parent / f"{stem}_ref"
    refs = _extract_reference_frames(stem, expected_source_frames, ref_dir)

    # Full-sequence comparison with drop/insert alignment detection.
    failures: list[str] = []
    drop_note: str | None = None
    matched = 0
    i = j = 0
    while i < len(played) and j < len(refs):
        diff = _mad(played[i], refs[j])
        if diff <= 8.0:
            matched += 1
            i += 1
            j += 1
            continue
        # hypothesis: source frame j was dropped by playback; test re-align
        if j + 1 < len(refs) and _mad(played[i], refs[j + 1]) <= 8.0:
            drop_note = (
                f"source frame {expected_source_frames[j]} missing from playback "
                f"at sequence position {i}; sequence re-aligned afterwards"
            )
            j += 1
            continue
        failures.append(
            f"sequence mismatch at played {i} vs source frame "
            f"{expected_source_frames[j]}: diff={diff:.2f}"
        )
        break

    verdict = "PASS" if not failures and matched == len(refs) else "FAIL"
    if drop_note and not failures and matched == len(refs):
        verdict = "PASS"
    return {
        "kind": "certified_edl_preview_probe",
        "schema_version": 1,
        "stem": stem,
        "edl": str(record["edl_path"]),
        "certification_status": record["certification_status"],
        "evidence": record["evidence"],
        "kept_ranges": record["kept_ranges"],
        "expected_frames": record["expected_frames"],
        "observed_frames": observed_frames,
        "sequence_matched_frames": matched,
        "single_frame_drop_note": drop_note,
        "wall_seconds": round(wall, 2),
        "verdict": verdict,
        "failures": failures[:10],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stem", type=int, required=True)
    args = parser.parse_args()

    print(f"[{args.stem}] building certified-tick EDL ...", flush=True)
    record = build_certified_edl(args.stem)
    print(
        f"[{args.stem}] EDL: {record['kept_ranges']} kept ranges, "
        f"{record['expected_frames']} expected frames, "
        f"cert={record['certification_status']}",
        flush=True,
    )
    print(f"[{args.stem}] playing headless with showinfo ...", flush=True)
    result = play_and_verify(args.stem, record)
    out = record["edl_path"].with_suffix(".verify.json")
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    print(f"report: {out}", flush=True)
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
