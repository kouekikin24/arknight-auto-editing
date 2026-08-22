#!/usr/bin/env python3
"""Screenshot the export tab to verify the 输出路径/视频质量 row alignment."""
from __future__ import annotations

import sys
import time
import tkinter as tk
from tkinter import ttk
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from PIL import ImageGrab  # noqa: E402

from settings_panel import SettingsPanel  # noqa: E402


def main() -> int:
    root = tk.Tk()
    root.title("export tab layout probe")
    root.geometry("420x520+120+80")
    panel = SettingsPanel(root)
    panel.pack(fill=tk.BOTH, expand=True)
    panel.output_var.set(r"D:\out\sample.mp4")
    # Switch the notebook to the export tab (index 3).
    for child in panel.winfo_children():
        if isinstance(child, ttk.Notebook):
            child.select(3)
            break
    end = time.time() + 1.5
    while time.time() < end:
        root.update()
        time.sleep(0.02)
    root.update_idletasks()
    x, y = root.winfo_rootx(), root.winfo_rooty()
    w, h = root.winfo_width(), root.winfo_height()
    full = ImageGrab.grab()
    scale = full.size[0] / root.winfo_screenwidth()
    img = ImageGrab.grab(
        bbox=(int(x * scale), int(y * scale), int((x + w) * scale), int((y + h) * scale))
    )
    out = _REPO / ".cache" / "export_tab_layout.png"
    img.save(out)
    print(out)
    root.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
