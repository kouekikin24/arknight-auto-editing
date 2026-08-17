"""EDL frame-exactness proof on a clean synthetic source.

Plays an EDL built from the cfr fixture's truth-manifest ticks with vo=image,
decodes the unique pixel frame IDs from every rendered frame, and compares
the observed ID sequence against the expected kept sequence exactly.

A clean source must reproduce the kept sequence frame-perfectly; that is the
mechanism-level precondition for any EDL preview route.  Real-source gaps
(head-PTS-anomaly lag) are measured separately on the real samples.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

MPV_DIR = REPO / "tools" / "libmpv" / "dll"

BIT_COUNT = 16
BIT_X, BIT_Y, BIT_WIDTH, BIT_HEIGHT = 64, 8, 8, 16
PIXEL_THRESHOLD = 128


def decode_frame_id(image_path: Path) -> int:
    from PIL import Image
    import numpy as np

    with Image.open(image_path) as img:
        gray = np.asarray(img.convert("L"), dtype=np.uint8)
    value = 0
    for index in range(BIT_COUNT):
        x = BIT_X + index * BIT_WIDTH
        block = gray[BIT_Y : BIT_Y + BIT_HEIGHT, x : x + BIT_WIDTH]
        if float(block.mean()) > PIXEL_THRESHOLD:
            value |= 1 << index
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture",
        type=Path,
        default=REPO / ".cache" / "mpv_spike" / "pts_fixtures" / "cfr.mp4",
    )
    parser.add_argument(
        "--truth",
        type=Path,
        default=REPO / ".cache" / "mpv_spike" / "pts_fixtures" / "cfr.truth.json",
    )
    parser.add_argument(
        "--delete",
        default="4-6,8-9",
        help="comma-separated half-open frame ranges, e.g. 4-6,8-9",
    )
    args = parser.parse_args()

    truth = json.loads(args.truth.read_text(encoding="utf-8"))
    frames = truth["frames"]
    time_base = truth["time_base"]
    tb_num, tb_den = time_base["numerator"], time_base["denominator"]

    deleted: list[tuple[int, int]] = []
    for part in args.delete.split(","):
        start, end = (int(x) for x in part.split("-"))
        deleted.append((start, end))

    kept: list[list[dict]] = []
    cursor = 0
    for start, end in sorted(deleted):
        if cursor < start:
            kept.append(frames[cursor:start])
        cursor = max(cursor, end)
    if cursor < len(frames):
        kept.append(frames[cursor:])

    edl_dir = REPO / ".cache" / "mpv_spike" / "certified_edl" / "synthetic"
    edl_dir.mkdir(parents=True, exist_ok=True)
    edl_path = edl_dir / "synthetic.edl"
    fixture_posix = args.fixture.as_posix()
    escaped = f"%{len(fixture_posix.encode('utf-8'))}%{fixture_posix}"
    lines = ["# mpv EDL v0"]
    for segment in kept:
        start_s = segment[0]["pts_ticks"] * tb_num / tb_den
        dur_s = (segment[-1]["pts_ticks"] + segment[-1]["duration_ticks"]
                 - segment[0]["pts_ticks"]) * tb_num / tb_den
        lines.append(f"{escaped},{start_s:.9f},{dur_s:.9f}")
    edl_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    expected_ids = [frame["frame_id"] for segment in kept for frame in segment]
    deleted_ids = sorted(
        {frame["frame_id"] for s, e in deleted for frame in frames[s:e]}
    )

    os.environ["PATH"] = str(MPV_DIR) + os.pathsep + os.environ.get("PATH", "")
    import mpv  # noqa: E402

    image_dir = edl_dir / "frames"
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
        **{
            "vo-image-format": "png",
            "vo-image-png-compression": "1",
            "vo-image-outdir": str(image_dir),
        },
    )
    started = time.perf_counter()
    try:
        with player.prepare_and_wait_for_event("file-loaded", timeout=30):
            player.play(str(edl_path))
        deadline = time.perf_counter() + 120
        while time.perf_counter() < deadline:
            try:
                if player.eof_reached:
                    break
            except Exception:
                break
            time.sleep(0.1)
    finally:
        player.terminate()
    wall = time.perf_counter() - started

    played = sorted(image_dir.glob("*.png"))
    observed_ids = [decode_frame_id(path) for path in played]

    deleted_shown = sorted(set(observed_ids) & set(deleted_ids))
    verdict = (
        "PASS"
        if observed_ids == expected_ids and not deleted_shown
        else "FAIL"
    )
    report = {
        "kind": "certified_edl_exactness_probe",
        "schema_version": 1,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "fixture": str(args.fixture),
        "edl": str(edl_path),
        "deleted_ranges": deleted,
        "expected_ids": expected_ids,
        "observed_ids": observed_ids,
        "deleted_ids_shown": deleted_shown,
        "wall_seconds": round(wall, 2),
        "verdict": verdict,
    }
    report_path = edl_dir / "exactness.verify.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"report: {report_path}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
