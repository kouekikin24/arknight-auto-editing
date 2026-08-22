#!/usr/bin/env python3
"""Probe: does libmpv render ``osd-overlay`` ASS events top-right on this build?

Loads the real source paused at a kept frame, screenshots the preview host
before/after sending an ``osd-overlay`` command, and reports bright-text
pixel counts in each corner.  Also verifies ``show_osd_text`` (top-left
frame number) still works alongside the corner overlay.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import tkinter as tk
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

os.environ.setdefault("ARKNIGHT_PREVIEW_ENGINE", "mpv")

from PIL import ImageGrab  # noqa: E402

import preview_player  # noqa: E402
from settings_panel import SettingsPanel  # noqa: E402


def _corner_stats(img, corner: str, frac: float = 0.18):
    w, h = img.size
    cw, ch = int(w * frac), int(h * 0.12)
    if corner == "tl":
        box = (0, 0, cw, ch)
    else:
        box = (w - cw, 0, w, ch)
    crop = img.crop(box).convert("L")
    px = list(crop.getdata())
    bright = sum(1 for v in px if v > 140)
    return {"bright": bright, "total": len(px)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", type=Path, default=Path(r"D:\qq下载\920\2.mp4"))
    ap.add_argument("--seek-frame", type=int, default=1000)
    args = ap.parse_args()

    result: dict = {"video": str(args.video)}
    root = tk.Tk()
    root.title("osd-overlay probe")
    root.geometry("900x620+80+60")
    settings = SettingsPanel(root)
    player = preview_player.VideoPreviewPlayer(
        root, settings, str(args.video), preview_engine="mpv"
    )
    player.pack(fill=tk.BOTH, expand=True)

    def pump(seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            root.update()
            time.sleep(0.01)

    def grab_host():
        root.update_idletasks()
        root.update()
        x = player.video_surface.winfo_rootx()
        y = player.video_surface.winfo_rooty()
        w = player.video_surface.winfo_width()
        h = player.video_surface.winfo_height()
        full = ImageGrab.grab()
        scale = full.size[0] / root.winfo_screenwidth()
        return ImageGrab.grab(
            bbox=(int(x * scale), int(y * scale), int((x + w) * scale), int((y + h) * scale))
        )

    try:
        engine = player._io
        result["engine"] = type(engine).__name__
        if type(engine).__name__ != "MpvEngine":
            result["reason_codes"] = ["MPV_ENGINE_NOT_SELECTED"]
            return result
        pump(1.5)
        player._seek(args.seek_frame)
        pump(1.5)

        base = grab_host()
        result["baseline"] = {"tl": _corner_stats(base, "tl"), "tr": _corner_stats(base, "tr")}

        ok_corner = engine.show_osd_corner_text("59.9 FPS")
        result["show_osd_corner_text_accepted"] = ok_corner
        pump(0.8)
        after = grab_host()
        result["corner_on"] = {"tl": _corner_stats(after, "tl"), "tr": _corner_stats(after, "tr")}

        # Persistence: overlay should still be visible 2s later without resend.
        pump(2.0)
        persist = grab_host()
        result["corner_persist"] = {"tr": _corner_stats(persist, "tr")}

        # Frame-number OSD (top-left, show-text) must coexist.
        ok_frame = engine.show_osd_text("帧 1,000 / 423,999", 1200)
        result["show_osd_text_accepted"] = ok_frame
        pump(0.5)
        both = grab_host()
        both.save(_REPO / ".cache" / "osd_overlay_probe.png")
        result["both_on"] = {"tl": _corner_stats(both, "tl"), "tr": _corner_stats(both, "tr")}

        # Clearing must remove the corner overlay.
        engine.show_osd_corner_text("")
        pump(0.6)
        cleared = grab_host()
        result["corner_cleared"] = {"tr": _corner_stats(cleared, "tr")}

        tr_delta = result["corner_on"]["tr"]["bright"] - result["baseline"]["tr"]["bright"]
        persist_delta = result["corner_persist"]["tr"]["bright"] - result["baseline"]["tr"]["bright"]
        tl_delta = result["both_on"]["tl"]["bright"] - result["baseline"]["tl"]["bright"]
        result["verdict"] = {
            "corner_renders": ok_corner and tr_delta > 40,
            "corner_persists": persist_delta > 40,
            "frame_osd_coexists": ok_frame and tl_delta > 40,
            "tr_delta": tr_delta,
            "tl_delta": tl_delta,
        }
    finally:
        try:
            player.close()
        except Exception:
            pass
        root.destroy()
    return result


if __name__ == "__main__":
    out = main()
    print(json.dumps(out, ensure_ascii=False, indent=2) if isinstance(out, dict) else out)
