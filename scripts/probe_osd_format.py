#!/usr/bin/env python3
"""一次性定论 osd-overlay ass-events 的正确 data 格式。

分别用 3 种格式发同一段文字，截图看哪种只显示干净的文本：
  A) 完整 Dialogue 行（当前实现，疑似错误）
  B) 纯文本
  C) 只带 {\\an9} 标签 + 文本
"""
from __future__ import annotations

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

VARIANTS = {
    "A_dialogue_line": "Dialogue: 0,0:00:00.00,9:00:00.00,Default,,0,0,0,,{\\an9}TEST 60 FPS",
    "B_plain_text": "TEST 60 FPS",
    "C_tag_plus_text": "{\\an9}TEST 60 FPS",
}


def main() -> int:
    video = Path(r"D:\qq下载\920\2.mp4")
    root = tk.Tk()
    root.geometry("900x620+80+40")
    root.title("osd-overlay format probe")
    settings = SettingsPanel(root)
    player = preview_player.VideoPreviewPlayer(root, settings, str(video), preview_engine="mpv")
    player.pack(fill=tk.BOTH, expand=True)

    def pump(s):
        end = time.time() + s
        while time.time() < end:
            root.update()
            time.sleep(0.01)

    def grab():
        root.update_idletasks(); root.update()
        x = player.video_surface.winfo_rootx(); y = player.video_surface.winfo_rooty()
        w = player.video_surface.winfo_width(); h = player.video_surface.winfo_height()
        full = ImageGrab.grab(); sc = full.size[0] / root.winfo_screenwidth()
        return ImageGrab.grab(bbox=(int(x*sc), int(y*sc), int((x+w)*sc), int((y+h)*sc)))

    outdir = _REPO / ".cache" / "osd_format_probe"
    outdir.mkdir(parents=True, exist_ok=True)
    try:
        engine = player._io
        pump(1.5)
        player._seek(1000)
        pump(1.5)
        raw = engine._player  # 直接发原始命令，绕过 show_osd_corner_text 的封装
        for name, data in VARIANTS.items():
            raw.command("osd-overlay", 1, "ass-events", data, 0, 0, 0)
            pump(0.7)
            img = grab()
            top = img.crop((0, 0, img.size[0], int(img.size[1]*0.15)))
            top.save(outdir / f"{name}.png")
            print(f"saved {name}.png  data={data!r}")
            raw.command("osd-overlay", 1, "none", "", 0, 0, 0)
            pump(0.4)
    finally:
        try: player.close()
        except Exception: pass
        root.destroy()
    print(outdir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
