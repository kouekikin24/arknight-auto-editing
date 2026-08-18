"""G3 gate: EDL cut-point frame-exactness with real libmpv on a clean source.

G3 asks whether the edited (cut) playback shows exactly the kept frames with no
deleted-frame leakage at the cut boundaries.  It collects three layers of
evidence:

- A. Real-window EDL render: the EDL is loaded and rendered by real libmpv
  (vo=gpu) into a real on-screen Tk WID, and the client area is non-blank.
  This proves the EDL route renders through the same real window G1 validated.
- B. Real-window absolute-exact stepping: each kept frame is seeked by its
  certified absolute time and the presented position is read back, proving
  frame-accurate landing in the real window.
- C. Headless frame-exact replay (primary): the same EDL is rendered with
  vo=image, every emitted frame's machine-readable pixel ID is decoded, and the
  observed sequence is compared exactly against the expected kept sequence.

The synthetic cfr fixture encodes each frame's ID as 16 pixel blocks, so the
pixel comparison is exact, not a blur/DPI approximation.  Real-source cut-point
lag (head-PTS anomaly) is a separate, already-documented measurement.
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

CFR_FIXTURE = REPO / ".cache" / "mpv_spike" / "pts_fixtures" / "cfr.mp4"
CFR_TRUTH = REPO / ".cache" / "mpv_spike" / "pts_fixtures" / "cfr.truth.json"
DEFAULT_DLL_DIR = REPO / "tools" / "libmpv" / "dll"
OUT_ROOT = REPO / ".cache" / "mpv_spike" / "g3_cutpoint"

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


def _non_blank(image) -> dict:
    import numpy as np

    arr = np.asarray(image.convert("RGB"), dtype=np.uint8)
    flat = arr.reshape(-1, 3)
    h, w = arr.shape[:2]
    cx, cy = w // 2, h // 2
    half = min(16, w // 2, h // 2)
    center = arr[cy - half : cy + half, cx - half : cx + half].reshape(-1, 3)
    return {
        "center_distinct": int(len(np.unique(center, axis=0))),
        "mean_luma": float((0.299 * flat[:, 0] + 0.587 * flat[:, 1] + 0.114 * flat[:, 2]).mean()),
    }


def _prepare_binding(dll_dir: Path):
    os.environ["PATH"] = str(dll_dir) + os.pathsep + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(str(dll_dir))
        except OSError:
            pass
    import mpv  # noqa: E402

    return mpv


def _build_edl(fixture: Path, truth: dict, deleted: list[tuple[int, int]], edl_path: Path) -> list[list[dict]]:
    frames = truth["frames"]
    tb_num = truth["time_base"]["numerator"]
    tb_den = truth["time_base"]["denominator"]
    kept: list[list[dict]] = []
    cursor = 0
    for start, end in sorted(deleted):
        if cursor < start:
            kept.append(frames[cursor:start])
        cursor = max(cursor, end)
    if cursor < len(frames):
        kept.append(frames[cursor:])
    edl_path.parent.mkdir(parents=True, exist_ok=True)
    fixture_posix = fixture.as_posix()
    escaped = f"%{len(fixture_posix.encode('utf-8'))}%{fixture_posix}"
    lines = ["# mpv EDL v0"]
    for segment in kept:
        start_s = segment[0]["pts_ticks"] * tb_num / tb_den
        dur_s = (segment[-1]["pts_ticks"] + segment[-1]["duration_ticks"] - segment[0]["pts_ticks"]) * tb_num / tb_den
        lines.append(f"{escaped},{start_s:.9f},{dur_s:.9f}")
    edl_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return kept


def run_g3(fixture: Path, truth_path: Path, dll_dir: Path, out_dir: Path, deleted: list[tuple[int, int]]) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    edl_path = out_dir / "cut.edl"
    kept = _build_edl(fixture, truth, deleted, edl_path)
    expected_ids = [f["frame_id"] for seg in kept for f in seg]
    deleted_ids = sorted({f["frame_id"] for s, e in deleted for f in truth["frames"][s:e]})
    tb_den = truth["time_base"]["denominator"]
    tb_num = truth["time_base"]["numerator"]

    result = {
        "schema_version": 1,
        "kind": "mpv_phase0_g3_cutpoint",
        "gate": "G3",
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fixture": str(fixture),
        "edl": str(edl_path),
        "deleted_ranges": deleted,
        "expected_ids": expected_ids,
        "layers": {},
        "status": "fail",
        "reason_codes": [],
    }

    mpv = _prepare_binding(dll_dir)

    # ---- Layer A: real-window EDL render ---------------------------------
    import tkinter as tk
    from PIL import ImageGrab

    root = tk.Tk()
    root.title("G3 cut-point")
    root.geometry("640x360+140+140")
    host = tk.Frame(root, bg="black")
    host.pack(fill=tk.BOTH, expand=True)
    root.update_idletasks()
    wid = host.winfo_id()
    full = ImageGrab.grab()
    scale = full.size[0] / root.winfo_screenwidth()

    def grab_host():
        root.update_idletasks()
        root.update()
        x, y, w, h = host.winfo_rootx(), host.winfo_rooty(), host.winfo_width(), host.winfo_height()
        return ImageGrab.grab(bbox=(int(x * scale), int(y * scale), int((x + w) * scale), int((y + h) * scale)))

    player = mpv.MPV(
        vo="gpu", wid=str(wid), audio="no", keep_open="yes", idle="yes",
        terminal=False, input_default_bindings=False, hr_seek="yes",
    )
    try:
        player.play(str(edl_path))
        deadline = time.time() + 8.0
        loaded = False
        while time.time() < deadline:
            root.update()
            try:
                if player.duration:
                    loaded = True
                    break
            except Exception:
                pass
            time.sleep(0.05)
        player.pause = True
        # paint a few frames
        settle = time.time() + 1.5
        while time.time() < settle:
            root.update()
            time.sleep(0.05)
        stats = _non_blank(grab_host())
        result["layers"]["A_real_window_render"] = {
            "loaded": loaded,
            "non_blank_center_distinct": stats["center_distinct"],
            "non_blank_mean_luma": stats["mean_luma"],
            "pass": bool(loaded and stats["center_distinct"] > 1 and stats["mean_luma"] > 8.0),
        }

        # ---- Layer B: real-window absolute-exact stepping ----------------
        # Kept virtual times are contiguous in EDL time; step to each kept
        # frame's virtual start and read back the presented time-pos.
        virtual_starts = []
        cursor = 0.0
        for seg in kept:
            for f in seg:
                virtual_starts.append(cursor)
                cursor += (f["duration_ticks"] * tb_num / tb_den)
        landings = []
        for idx, vt in enumerate(virtual_starts):
            player.command("seek", f"{vt:.9f}", "absolute+exact")
            time.sleep(0.12)
            root.update()
            try:
                pos = player.time_pos
            except Exception:
                pos = None
            landings.append({"index": idx, "target": round(vt, 6), "pos": (round(pos, 6) if isinstance(pos, (int, float)) else None)})
        # A landing is frame-accurate if the presented position falls inside the
        # target frame's 40ms window (ms-quantized readback).
        frame_dur = 40 * tb_num / tb_den
        accurate = sum(
            1
            for l in landings
            if l["pos"] is not None and abs(l["pos"] - l["target"]) < frame_dur
        )
        result["layers"]["B_real_window_stepping"] = {
            "landings": landings,
            "accurate": accurate,
            "total": len(landings),
            "pass": accurate == len(landings),
        }
    finally:
        try:
            player.terminate()
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass

    # ---- Layer C: headless frame-exact replay (primary) ------------------
    image_dir = out_dir / "frames"
    if image_dir.exists():
        for old in image_dir.glob("*.png"):
            old.unlink()
    image_dir.mkdir(parents=True, exist_ok=True)
    player2 = mpv.MPV(
        vo="image", audio="no", untimed=True, framedrop="no", keep_open="yes",
        idle="yes", terminal=False, input_default_bindings=False,
        **{"vo-image-format": "png", "vo-image-png-compression": "1", "vo-image-outdir": str(image_dir)},
    )
    started = time.perf_counter()
    try:
        with player2.prepare_and_wait_for_event("file-loaded", timeout=30):
            player2.play(str(edl_path))
        deadline = time.perf_counter() + 120
        while time.perf_counter() < deadline:
            try:
                if player2.eof_reached:
                    break
            except Exception:
                break
            time.sleep(0.1)
    finally:
        player2.terminate()
    observed_ids = [decode_frame_id(p) for p in sorted(image_dir.glob("*.png"))]
    deleted_shown = sorted(set(observed_ids) & set(deleted_ids))
    result["layers"]["C_headless_exact_replay"] = {
        "observed_ids": observed_ids,
        "expected_ids": expected_ids,
        "sequence_exact": observed_ids == expected_ids,
        "deleted_ids_shown": deleted_shown,
        "wall_seconds": round(time.perf_counter() - started, 2),
        "pass": observed_ids == expected_ids and not deleted_shown,
    }

    # ---- verdict ----------------------------------------------------------
    codes = []
    if not result["layers"]["A_real_window_render"]["pass"]:
        codes.append("REAL_WINDOW_RENDER_FAILED")
    if not result["layers"]["B_real_window_stepping"]["pass"]:
        codes.append("REAL_WINDOW_STEPPING_INEXACT")
    if not result["layers"]["C_headless_exact_replay"]["pass"]:
        codes.append("CUT_POINT_NOT_FRAME_EXACT")
    result["reason_codes"] = codes
    result["status"] = "pass" if not codes else "fail"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=CFR_FIXTURE)
    parser.add_argument("--truth", type=Path, default=CFR_TRUTH)
    parser.add_argument("--dll-dir", type=Path, default=DEFAULT_DLL_DIR)
    parser.add_argument("--out-dir", type=Path, default=OUT_ROOT)
    parser.add_argument("--delete", default="4-6,8-9", help="comma-separated half-open ranges")
    args = parser.parse_args(argv)

    deleted = []
    for part in args.delete.split(","):
        s, e = (int(x) for x in part.split("-"))
        deleted.append((s, e))

    if not args.fixture.is_file() or not args.truth.is_file():
        print("fixture/truth missing", file=sys.stderr)
        return 2
    if not (args.dll_dir / "libmpv-2.dll").is_file():
        print(f"libmpv-2.dll missing under {args.dll_dir}", file=sys.stderr)
        return 2

    out_dir = args.out_dir / datetime.now().strftime("%Y%m%d-%H%M%S")
    result = run_g3(args.fixture, args.truth, args.dll_dir, out_dir, deleted)
    report = out_dir / "g3_cutpoint.json"
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "reason_codes": result["reason_codes"],
                      "observed": result["layers"]["C_headless_exact_replay"]["observed_ids"],
                      "report": str(report)}, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
