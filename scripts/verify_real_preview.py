"""Real-footage preview verification: the production sample-4 source on screen.

This is the operator-facing check that closes the preview-only libmpv route:
it renders the real 424176-frame recording in a real on-screen Tk window via
the real :class:`MpvEngine`, builds a certified EDL from adjudicated B'
evidence, and proves the SOURCE and EDL views both locate and render frames
correctly at multiple points across the two-hour timeline.

Checks, in order:

1. SOURCE mode: seek to five spread-out frames; after each seek the player
   time-pos must sit near the certified time for that frame and the rendered
   window must be non-blank.
2. EDL mode: play_edl() must load the certified EDL, then seek to three
   spread source frames through the virtual-time mapping; rendered window
   must be non-blank.
3. Paced SOURCE<->EDL switching (human edit cadence): every transition must
   deliver file-loaded, report the right engine mode, and render non-blank.

The gate criterion is the paced pattern; sub-second switching is a known
mpv-on-Windows GPU-context stress case and is out of scope here.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DEFAULT_DLL_DIR = REPO / "tools" / "libmpv" / "dll"
OUT_ROOT = REPO / ".cache" / "mpv_spike" / "real_preview"


def _non_blank(image) -> dict:
    import numpy as np

    arr = np.asarray(image.convert("RGB"), dtype=np.uint8)
    flat = arr.reshape(-1, 3)
    h, w = arr.shape[:2]
    cx, cy = w // 2, h // 2
    half = min(16, w // 2, h // 2)
    center = arr[cy - half : cy + half, cx - half : cx + half].reshape(-1, 3)
    return {
        "distinct_colors": int(len(np.unique(flat[:: max(1, len(flat) // 50_000)], axis=0))),
        "center_distinct": int(len(np.unique(center, axis=0))),
        "mean_luma": float(
            (0.299 * flat[:, 0] + 0.587 * flat[:, 1] + 0.114 * flat[:, 2]).mean()
        ),
    }


def _is_non_blank(stats: dict) -> bool:
    # A real recording can sit on a flat color block at the exact window
    # center, so the whole-window palette is the reliable blank signal.
    return stats["distinct_colors"] > 8 and stats["mean_luma"] > 3.0


def _deleted_ranges() -> list[tuple[int, int]]:
    """Spread deletions across the whole two-hour recording.

    Mixing very short and longer spans exercises both the EDL cut-point
    handling and the collision scan against the full 424176-row timeline.
    Ranges are half-open source-frame spans well outside the adjudicated
    head window (0..32).
    """
    return [
        (60, 150),
        (1_000, 1_300),
        (6_000, 6_090),
        (20_000, 20_450),
        (50_000, 50_120),
        (90_000, 90_600),
        (140_000, 140_200),
        (200_000, 200_800),
        (260_000, 260_090),
        (330_000, 330_500),
        (400_000, 400_300),
    ]


def run(source: Path, dll_dir: Path, out_dir: Path) -> dict:
    import tkinter as tk
    from PIL import ImageGrab

    from mpv_engine import MpvEngine
    from preview_engine import CertifiedEdlRequest, SourceSeekRequest
    from timeline_plan import TimelinePlan
    from media_info import probe_media
    from frame_pts_certifier import produce_frame_pts_certification

    out_dir.mkdir(parents=True, exist_ok=True)
    deleted = _deleted_ranges()

    result = {
        "schema_version": 1,
        "kind": "mpv_real_preview_verification",
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": str(source),
        "deleted_ranges": deleted,
        "checks": {},
        "status": "fail",
        "reason_codes": [],
    }

    t_probe = time.perf_counter()
    media = probe_media(source)
    outcome = produce_frame_pts_certification(media, decode_threads=4)
    if not outcome.certified:
        result["reason_codes"] = ["CERTIFICATION_FAILED"]
        return result
    certified_media = outcome.media_info
    certification = certified_media.frame_pts_certification
    total = int(certification.frame_count)
    plan = TimelinePlan.from_deleted_ranges(total, deleted)
    result["certification"] = {
        "frame_count": total,
        "time_base": str(certification.time_base),
        "head_anomaly_limit": int(getattr(certification, "head_anomaly_limit", 0)),
        "probe_and_load_seconds": round(time.perf_counter() - t_probe, 2),
    }
    result["plan"] = {
        "total_frames": plan.total_frames,
        "deleted_frames": plan.deleted_frames,
        "kept_frames": plan.kept_frames,
        "deleted_range_count": len(deleted),
    }

    root = tk.Tk()
    root.title("Real preview verification")
    root.geometry("960x540+120+120")
    host = tk.Frame(root, bg="black")
    host.pack(fill=tk.BOTH, expand=True)
    root.update_idletasks()
    wid = host.winfo_id()
    full = ImageGrab.grab()
    scale = full.size[0] / root.winfo_screenwidth()
    result["display"] = {
        "tk_screen_wh": [root.winfo_screenwidth(), root.winfo_screenheight()],
        "physical_wh": list(full.size),
        "measured_scale": scale,
    }

    def grab_host():
        root.update_idletasks()
        root.update()
        x = host.winfo_rootx()
        y = host.winfo_rooty()
        w = host.winfo_width()
        h = host.winfo_height()
        return ImageGrab.grab(
            bbox=(int(x * scale), int(y * scale), int((x + w) * scale), int((y + h) * scale))
        )

    engine = MpvEngine(
        str(source),
        wid=wid,
        fps=float(media.video_streams[0].avg_frame_rate),
        total=total,
        dll_dir=dll_dir,
        edl_dir=out_dir / "edl",
    )
    engine.bind_media_info(certified_media)
    engine.start()

    def pump(seconds: float) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            root.update()
            time.sleep(0.02)

    def wait_loaded(timeout: float = 20.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            root.update()
            for event in engine.poll_events():
                if event.get("event") == "file-loaded":
                    return True, event.get("path")
            time.sleep(0.03)
        return False, None

    def wait_time_pos(target: float, tolerance: float = 0.5, timeout: float = 8.0):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            root.update()
            player = engine._player
            if player is not None:
                try:
                    value = player.time_pos
                    if value is not None:
                        last = float(value)
                        if abs(last - target) <= tolerance:
                            return True, last
                except Exception:
                    pass
            time.sleep(0.03)
        return False, last

    # The certified source-time table maps a source frame to its certified
    # PTS seconds; the same table drives both seek_source and the EDL build.
    timeline = engine._timeline
    source_times = engine._timeline_times

    def source_seconds(frame: int) -> float:
        return float(source_times[frame])

    failures: list[dict] = []
    try:
        ok, _ = wait_loaded()
        if not ok:
            result["reason_codes"] = ["INITIAL_LOAD_FAILED"]
            return result
        pump(0.5)

        # -- Check 1: SOURCE mode seeks -------------------------------------
        source_frames = [0, 90_000, 180_000, 300_000, total - 1]
        source_rows = []
        for frame in source_frames:
            target = source_seconds(frame)
            engine.seek_source(
                SourceSeekRequest(frame, (960, 540), 0, exact=True, latest_only=True)
            )
            located, observed = wait_time_pos(target, tolerance=0.5)
            pump(0.35)
            stats = _non_blank(grab_host())
            row = {
                "frame": frame,
                "target_s": round(target, 3),
                "observed_s": round(observed, 3) if observed is not None else None,
                "located": bool(located),
                "mode": engine.mode,
                "distinct_colors": stats["distinct_colors"],
                "non_blank": bool(_is_non_blank(stats)),
            }
            source_rows.append(row)
            if not located or engine.mode != "source" or not _is_non_blank(stats):
                failures.append({"check": "source_seek", **row})
        result["checks"]["source_seeks"] = source_rows

        # -- Check 2: EDL mode loads and locates ----------------------------
        engine.play_edl(
            CertifiedEdlRequest(certified_media, plan, 0, 0),
            start_frame=0,
            playback_rate=1.0,
        )
        ok, edl_path = wait_loaded()
        edl_rows = [{"file_loaded": bool(ok), "path_tail": (str(edl_path)[-32:] if edl_path else None), "mode": engine.mode}]
        if not ok or engine.mode != "edl":
            failures.append({"check": "edl_load", "file_loaded": bool(ok), "mode": engine.mode})
        else:
            pump(0.5)
            player = engine._player
            for frame in (90_000, 200_000, 300_000):
                # In EDL mode the virtual timeline excludes deleted spans, so
                # the same source frame lands earlier than in SOURCE mode.
                # Seek inside the EDL via the raw player command so the engine
                # stays in "edl" mode (seek_source would switch it back).
                virtual_s = float(engine._edl.virtual_time_for_source(frame, snap=True))
                player.command("seek", virtual_s, "absolute", "exact")
                located, observed = wait_time_pos(virtual_s, tolerance=0.75)
                pump(0.4)
                stats = _non_blank(grab_host())
                row = {
                    "frame": frame,
                    "virtual_target_s": round(virtual_s, 3),
                    "observed_s": round(observed, 3) if observed is not None else None,
                    "located": bool(located),
                    "mode": engine.mode,
                    "distinct_colors": stats["distinct_colors"],
                    "non_blank": bool(_is_non_blank(stats)),
                }
                edl_rows.append(row)
                if not located or engine.mode != "edl" or not _is_non_blank(stats):
                    failures.append({"check": "edl_seek", **row})
        result["checks"]["edl_seeks"] = edl_rows

        # -- Check 3: paced SOURCE<->EDL switching --------------------------
        transitions = []
        for cycle in range(6):
            for target in ("source", "edl"):
                if target == "edl":
                    engine.play_edl(
                        CertifiedEdlRequest(certified_media, plan, 0, 0),
                        start_frame=0,
                        playback_rate=1.0,
                    )
                else:
                    engine.seek_source(
                        SourceSeekRequest(0, (960, 540), 0, exact=True, latest_only=True)
                    )
                ok, path = wait_loaded()
                pump(1.2)
                stats = _non_blank(grab_host())
                row = {
                    "cycle": cycle,
                    "target": target,
                    "file_loaded": bool(ok),
                    "mode": engine.mode,
                    "distinct_colors": stats["distinct_colors"],
                    "non_blank": bool(_is_non_blank(stats)),
                }
                transitions.append(row)
                if not ok or engine.mode != target or not _is_non_blank(stats):
                    failures.append({"check": "switch", **row})
        result["checks"]["switches"] = transitions
    finally:
        try:
            engine.close(timeout=3.0)
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass

    codes: list[str] = []
    if any(f["check"] == "source_seek" for f in failures):
        codes.append("SOURCE_SEEK_FAILED")
    if any(f["check"] == "edl_load" for f in failures):
        codes.append("EDL_LOAD_FAILED")
    if any(f["check"] == "edl_seek" for f in failures):
        codes.append("EDL_SEEK_FAILED")
    if any(f["check"] == "switch" for f in failures):
        codes.append("SWITCH_FAILED")
    result["reason_codes"] = codes
    result["status"] = "pass" if not codes else "fail"
    result["failures"] = failures
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--dll-dir", type=Path, default=DEFAULT_DLL_DIR)
    parser.add_argument("--out-dir", type=Path, default=OUT_ROOT)
    args = parser.parse_args(argv)

    if not args.source.is_file():
        print(f"source missing: {args.source}", file=sys.stderr)
        return 2
    if not (args.dll_dir / "libmpv-2.dll").is_file():
        print(f"libmpv-2.dll missing under {args.dll_dir}", file=sys.stderr)
        return 2

    out_dir = args.out_dir / datetime.now().strftime("%Y%m%d-%H%M%S")
    result = run(args.source, args.dll_dir, out_dir)
    report = out_dir / "real_preview.json"
    report.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary = {
        "status": result["status"],
        "reason_codes": result["reason_codes"],
        "frames": result.get("certification", {}).get("frame_count"),
        "deleted_frames": result.get("plan", {}).get("deleted_frames"),
        "checks": {k: len(v) for k, v in result.get("checks", {}).items()},
        "report": str(report),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
