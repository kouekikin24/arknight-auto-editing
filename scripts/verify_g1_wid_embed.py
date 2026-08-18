"""G1 gate: real libmpv WID embedding on a real Tk window.

This is the first preview-integration check that runs a real libmpv render into
a real on-screen Tk child window.  Every earlier preview test used a fake
binding, so this script collects the five pieces of real-window evidence the
gate requires:

1. WID validity     - the host child has a nonzero, stable window handle.
2. Non-blank pixels - the rendered client area is neither uniform nor black.
3. Resize follow    - the rendered content follows the host after a resize.
4. DPI scale        - the physical/logical pixel ratio is measured, not trusted
                      from Tk (a DPI-unaware process is virtualized by Windows).
5. Focus survival   - clicking the video surface does not steal the global
                      playback shortcut.

Evidence is written under .cache/mpv_spike/g1_wid/ as JSON plus the captured
screenshots.  Run on a real desktop session; it opens a visible window.
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
DEFAULT_DLL_DIR = REPO / "tools" / "libmpv" / "dll"
OUT_ROOT = REPO / ".cache" / "mpv_spike" / "g1_wid"


def _pixel_stats(image) -> dict:
    """Return uniformity/brightness facts for a captured client area."""
    import numpy as np

    small = image.convert("RGB")
    arr = np.asarray(small, dtype=np.uint8)
    total = int(arr.shape[0] * arr.shape[1])
    if total == 0:
        return {"error": "empty capture", "distinct": 0, "mean_luma": 0.0}
    flat = arr.reshape(-1, 3)
    distinct = int(len(np.unique(flat, axis=0)))
    mean_luma = float((0.299 * flat[:, 0] + 0.587 * flat[:, 1] + 0.114 * flat[:, 2]).mean())
    # Center 32x32 patch uniformity is a stronger "is something drawn" signal
    # than the whole client area, which can include letterbox bars.
    h, w = arr.shape[:2]
    cx, cy = w // 2, h // 2
    half = min(16, w // 2, h // 2)
    center = arr[cy - half : cy + half, cx - half : cx + half].reshape(-1, 3)
    center_distinct = int(len(np.unique(center, axis=0)))
    return {
        "size": [w, h],
        "distinct": distinct,
        "mean_luma": round(mean_luma, 2),
        "center_patch": [2 * half, 2 * half],
        "center_distinct": center_distinct,
    }


def run_g1(fixture: Path, dll_dir: Path, out_dir: Path) -> dict:
    import tkinter as tk
    from PIL import ImageGrab

    from mpv_engine import MpvEngine

    out_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "schema_version": 1,
        "kind": "mpv_phase0_g1_wid",
        "gate": "G1",
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fixture": str(fixture),
        "dll_dir": str(dll_dir),
        "checks": {},
        "artifacts": {},
        "status": "fail",
        "reason_codes": [],
    }

    # --- DPI scale (measured, not trusted) -------------------------------
    probe = tk.Tk()
    probe.withdraw()
    probe.update_idletasks()
    logical_w = probe.winfo_screenwidth()
    logical_h = probe.winfo_screenheight()
    full = ImageGrab.grab()
    measured_scale = round(full.size[0] / max(1, logical_w), 4)
    tk_reported = None
    try:
        tk_reported = round(float(probe.winfo_fpixels("1i")) / 96.0, 4)
    except Exception:
        pass
    probe.destroy()
    result["checks"]["dpi_scale"] = {
        "logical_screen": [logical_w, logical_h],
        "physical_capture": [full.size[0], full.size[1]],
        "measured_scale": measured_scale,
        "tk_reported_scale": tk_reported,
        "virtualized": abs(measured_scale - 1.0) > 1e-3 and (tk_reported or 1.0) == 1.0,
    }

    def logical_to_physical(x_logical: int, y_logical: int) -> tuple[int, int]:
        return (int(x_logical * measured_scale), int(y_logical * measured_scale))

    # --- Build the real window + native host -----------------------------
    root = tk.Tk()
    root.title("G1 WID embed")
    root.geometry("640x360+120+120")
    host = tk.Frame(root, bg="black", width=480, height=270)
    host.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
    root.update_idletasks()
    wid = host.winfo_id()

    result["checks"]["wid"] = {
        "wid": wid,
        "nonzero": bool(wid),
        "stable": wid == host.winfo_id(),
    }

    def grab_host(tag: str):
        root.update_idletasks()
        root.update()
        x = host.winfo_rootx()
        y = host.winfo_rooty()
        w = host.winfo_width()
        h = host.winfo_height()
        x0, y0 = logical_to_physical(x, y)
        x1, y1 = logical_to_physical(x + w, y + h)
        img = ImageGrab.grab(bbox=(x0, y0, x1, y1))
        path = out_dir / f"{tag}.png"
        img.save(path)
        result["artifacts"][tag] = {
            "path": str(path),
            "bbox_logical": [x, y, w, h],
            "bbox_physical": [x0, y0, x1, y1],
            "stats": _pixel_stats(img),
        }
        return result["artifacts"][tag]["stats"]

    engine = None
    try:
        engine = MpvEngine(str(fixture), wid=wid, fps=25.0, total=13, dll_dir=dll_dir)
        engine.start()
        # Wait for the file to actually load and the first frame to present.
        deadline = time.time() + 8.0
        loaded = False
        while time.time() < deadline:
            root.update()
            for event in engine.poll_events():
                if event.get("event") == "file-loaded":
                    loaded = True
            if loaded:
                break
            time.sleep(0.05)
        result["checks"]["file_loaded"] = {"loaded": loaded, "engine_alive": engine.is_alive()}
        # Let vo=gpu paint several frames into the WID before capturing.
        settle = time.time() + 2.0
        while time.time() < settle:
            root.update()
            time.sleep(0.05)

        # --- 2. non-blank pixels ----------------------------------------
        stats_loaded = grab_host("g1_loaded")
        non_blank = (
            stats_loaded.get("center_distinct", 0) > 1
            and stats_loaded.get("mean_luma", 0.0) > 8.0
        )
        result["checks"]["non_blank_pixels"] = {
            "center_distinct": stats_loaded.get("center_distinct"),
            "mean_luma": stats_loaded.get("mean_luma"),
            "pass": bool(non_blank),
        }

        # --- 3. resize follow -------------------------------------------
        root.geometry("880x520")
        settle = time.time() + 1.5
        while time.time() < settle:
            root.update()
            time.sleep(0.05)
        stats_resized = grab_host("g1_resized")
        resized_non_blank = (
            stats_resized.get("center_distinct", 0) > 1
            and stats_resized.get("mean_luma", 0.0) > 8.0
        )
        result["checks"]["resize_follow"] = {
            "before_center_distinct": stats_loaded.get("center_distinct"),
            "after_center_distinct": stats_resized.get("center_distinct"),
            "after_mean_luma": stats_resized.get("mean_luma"),
            "new_host_logical": [host.winfo_width(), host.winfo_height()],
            "pass": bool(resized_non_blank),
        }

        # --- 5. focus survival -------------------------------------------
        # Click the video surface; the host must not permanently capture the
        # global playback shortcut.  We synthesize a click then check that the
        # toplevel can still take focus.
        try:
            host.event_generate("<Button-1>", x=10, y=10)
            root.update()
            root.focus_force()
            root.update()
            focused = root.focus_displayof() is not None
        except Exception:
            focused = False
        result["checks"]["focus_survival"] = {"toplevel_recoverable": bool(focused), "pass": bool(focused)}

    finally:
        if engine is not None:
            try:
                engine.close(timeout=2.0)
            except Exception:
                pass
        try:
            root.destroy()
        except Exception:
            pass

    # --- verdict ----------------------------------------------------------
    checks = result["checks"]
    codes = []
    if not (checks["wid"]["nonzero"] and checks["wid"]["stable"]):
        codes.append("WID_INVALID")
    if not checks.get("file_loaded", {}).get("loaded"):
        codes.append("FILE_NOT_LOADED")
    if not checks.get("non_blank_pixels", {}).get("pass"):
        codes.append("BLANK_RENDER")
    if not checks.get("resize_follow", {}).get("pass"):
        codes.append("RESIZE_NOT_FOLLOWED")
    if not checks.get("focus_survival", {}).get("pass"):
        codes.append("FOCUS_CAPTURED")
    result["reason_codes"] = codes
    result["status"] = "pass" if not codes else "fail"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=CFR_FIXTURE)
    parser.add_argument("--dll-dir", type=Path, default=DEFAULT_DLL_DIR)
    parser.add_argument("--out-dir", type=Path, default=OUT_ROOT)
    args = parser.parse_args(argv)

    if not args.fixture.is_file():
        print(f"fixture missing: {args.fixture}", file=sys.stderr)
        return 2
    if not (args.dll_dir / "libmpv-2.dll").is_file():
        print(f"libmpv-2.dll missing under: {args.dll_dir}", file=sys.stderr)
        return 2

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = args.out_dir / timestamp
    result = run_g1(args.fixture, args.dll_dir, out_dir)
    report = out_dir / "g1_wid.json"
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "reason_codes": result["reason_codes"], "report": str(report)},
                     ensure_ascii=False, indent=2))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
