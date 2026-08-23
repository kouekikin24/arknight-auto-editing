#!/usr/bin/env python3
"""真实窗口（1380x920，mpv 引擎）逐项验证本轮四项修复。

1. 导出面板：输出路径 Entry 与视频质量 Spinbox 左缘对齐；两个「就绪」标签
   左对齐且面板右侧无裁剪（浏览按钮完整可见）。
2. 左右键步进：暂停态（从未播放 / 播放后暂停）按 → 键，UI 帧号不被渲染
   循环回写打回，画面也确实落到目标帧。
3. 2x 播放：右上角 FPS 读数允许超过 60（上限 = 源帧率×倍速）。
4. 控制条「复制帧号」按钮把当前帧号写进剪贴板。

截图存 .cache/real_window_fixes.png，结果以 PASS/FAIL 行打印。
"""
from __future__ import annotations

import os
import sys
import time
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace

os.environ["ARKNIGHT_PREVIEW_ENGINE"] = "mpv"

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from PIL import ImageGrab  # noqa: E402

import main as app_main  # noqa: E402
from preview_player import VideoPreviewPlayer  # noqa: E402
from settings_panel import SettingsPanel  # noqa: E402

VIDEO = r"D:\qq下载\920\2.mp4"
RESULTS: list[tuple[str, bool, str]] = []


def report(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


def pump(root: tk.Misc, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        try:
            root.update()
        except tk.TclError:
            return
        time.sleep(0.01)


def find_player(root: tk.Misc) -> VideoPreviewPlayer:
    stack = [root]
    while stack:
        w = stack.pop()
        if isinstance(w, VideoPreviewPlayer):
            return w
        stack.extend(w.winfo_children())
    raise RuntimeError("VideoPreviewPlayer not found")


def find_settings(root: tk.Misc) -> SettingsPanel:
    stack = [root]
    while stack:
        w = stack.pop()
        if isinstance(w, SettingsPanel):
            return w
        stack.extend(w.winfo_children())
    raise RuntimeError("SettingsPanel not found")


def press_right(player: VideoPreviewPlayer, root: tk.Misc, times: int) -> None:
    for _ in range(times):
        player._on_key_press_right(None)
        pump(root, 0.05)
        player._on_key_release(SimpleNamespace(keysym="Right"))
        pump(root, 0.05)


def main() -> int:
    root = tk.Tk()
    root.title("real window fixes probe")
    root.geometry("1380x920+30+10")

    # 复制 main() 的装配（不动 main.py 本体）
    from tkinter import ttk
    from task_manager import TaskManager

    tasks = TaskManager(root)
    paned = tk.PanedWindow(root, orient=tk.HORIZONTAL, sashwidth=6,
                           sashrelief=tk.RAISED, bg="#555555")
    paned.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)
    left_frame = ttk.Frame(paned)
    paned.add(left_frame, stretch="always", minsize=500)
    right_frame = ttk.Frame(paned)
    paned.add(right_frame, stretch="never", minsize=240, width=360)
    settings = SettingsPanel(right_frame, task_manager=tasks)
    settings.pack(fill=tk.BOTH, expand=True)
    player = VideoPreviewPlayer(left_frame, settings=settings, task_manager=tasks)
    player.pack(fill=tk.BOTH, expand=True)

    pump(root, 1.0)

    # ---------- 1. 导出面板布局 ----------
    from tkinter import ttk as _ttk
    notebook = None
    for child in settings.winfo_children():
        if isinstance(child, _ttk.Notebook):
            notebook = child
            break
    assert notebook is not None
    notebook.select(3)  # 导出
    pump(root, 0.6)
    root.update_idletasks()

    def widget_by_text(container: tk.Misc, text: str, cls) -> tk.Misc | None:
        stack = [container]
        while stack:
            w = stack.pop()
            try:
                if isinstance(w, cls) and w.cget("text") == text:
                    return w
            except tk.TclError:
                pass
            stack.extend(w.winfo_children())
        return None

    tab_export = notebook.winfo_children()[3]
    browse = widget_by_text(tab_export, "浏览", _ttk.Button)
    # 精确锁定：输出路径 Entry 挂在 output_var 上、质量 Spinbox 挂在 quality_var
    # 上（GPU 编码器下拉是 ttk.Combobox——它继承 ttk.Entry，会被类名检查误中）
    entry = None
    spinbox = None
    stack = [tab_export]
    while stack:
        w = stack.pop()
        try:
            var = str(w.cget("textvariable"))
        except tk.TclError:
            var = ""
        if entry is None and isinstance(w, _ttk.Entry) and not isinstance(w, _ttk.Combobox) \
                and var == str(settings.output_var):
            entry = w
        if spinbox is None and w.winfo_class() == "TSpinbox" and var == str(settings.quality_var):
            spinbox = w
        stack.extend(w.winfo_children())
    report("导出:找到输出路径 Entry", entry is not None)
    report("导出:找到视频质量 Spinbox", spinbox is not None)
    if entry is not None and spinbox is not None:
        # 同格同 padx 的兄弟控件左缘必须一致；ttk 缩进差 1-2px 视为对齐
        dx = abs(entry.winfo_rootx() - spinbox.winfo_rootx())
        report("导出:Entry/Spinbox 左缘对齐", dx <= 3, f"|dx|={dx}")
        report("导出:输出路径输入框不被挤成细条", entry.winfo_width() >= 100,
               f"w={entry.winfo_width()}")
    if browse is not None:
        right_edge = browse.winfo_rootx() + browse.winfo_width()
        panel_right = right_frame.winfo_rootx() + right_frame.winfo_width()
        report("导出:浏览按钮未被裁掉", right_edge <= panel_right + 2,
               f"按钮右缘={right_edge} 面板右缘={panel_right}")
    status_labels = []
    stack = [tab_export]
    while stack:
        w = stack.pop()
        try:
            if isinstance(w, _ttk.Label) and w.cget("text") == "就绪":
                status_labels.append(w)
        except tk.TclError:
            pass
        stack.extend(w.winfo_children())
    tab_left = tab_export.winfo_rootx()
    for i, lbl in enumerate(status_labels):
        off = lbl.winfo_rootx() - tab_left
        report(f"导出:就绪标签{i + 1} 左对齐", off <= 14, f"距面板左缘 {off}px")

    # ---------- 加载视频，等认证 ----------
    player.load_video(VIDEO)
    t0 = time.time()
    while time.time() - t0 < 90:
        pump(root, 0.3)
        if player._preview_pts_ready():
            break
    report("认证就绪", player._preview_pts_ready(),
           f"等待 {time.time() - t0:.1f}s total_frames={player.total_frames}")

    # 注入一个全删暂停段，让「跳过裁剪区」有内容（镜像 _publish_project_snapshot 的形状）
    if player.total_frames > 4000:
        player.pause_segments = [
            {"id": 1, "start": 2000, "end": 2600, "mode": "all", "boundary_diff": 99.0}
        ]
        player.timeline.pause_segments = player.pause_segments
        player.timeline.mark_dirty()
    pump(root, 0.3)

    # ---------- 2. 暂停态步进（从未播放） ----------
    start = player.current_frame_idx
    press_right(player, root, 5)
    pump(root, 0.6)  # 等保护窗口过期 + mpv 寻址落地
    after = player.current_frame_idx
    report("步进:未播放暂停态 →×5 UI 帧号前进", after - start == 5,
           f"{start} -> {after}")
    pump(root, 0.8)
    report("步进:引擎落地后 UI 帧号不回退", player.current_frame_idx == after,
           f"now={player.current_frame_idx}")

    # ---------- 播放建立 EDL，再暂停步进 ----------
    player.toggle_play()
    pump(root, 2.5)
    playing_ok = player.is_playing
    report("播放启动", playing_ok)
    player.toggle_play()  # 暂停
    pump(root, 0.4)
    paused_at = player.current_frame_idx
    press_right(player, root, 5)
    pump(root, 0.6)
    step2 = player.current_frame_idx - paused_at
    # 步进落入全删段会被 EDL snap 到段尾，允许 >=5
    report("步进:EDL 模式暂停态 →×5 有前进", step2 >= 5,
           f"{paused_at} -> {player.current_frame_idx} (+{step2})")
    pump(root, 0.8)
    report("步进:EDL 模式落地后不回退",
           player.current_frame_idx >= paused_at + 5,
           f"now={player.current_frame_idx}")

    # ---------- 3. 2x FPS 上限 ----------
    player.preview_speed_var.set("2x")
    if not player.is_playing:
        player.toggle_play()
    pump(root, 4.0)
    fps_text = ""
    getter = getattr(player._io, "get_osd_texts", None)
    if callable(getter):
        _f, fps_text = getter()
    fps_val = None
    try:
        fps_val = float(fps_text.split()[0])
    except (ValueError, IndexError):
        pass
    src_fps = float(getattr(player._io, "fps", 0.0) or 0.0)
    report("2x:FPS 读数有效", fps_val is not None, f"text={fps_text!r}")
    if fps_val is not None:
        report("2x:FPS 不再被钳在源帧率", fps_val > src_fps + 3,
               f"{fps_val:.1f} vs 源 {src_fps:.1f}")
        report("2x:FPS 不超过 源×2", fps_val <= src_fps * 2 + 1,
               f"{fps_val:.1f} vs 上限 {src_fps * 2:.1f}")
    player.toggle_play()  # 停播
    pump(root, 0.3)

    # ---------- 4. 复制帧号按钮 ----------
    copy_btn = None
    stack = [root]
    while stack:
        w = stack.pop()
        try:
            if isinstance(w, _ttk.Button) and w.cget("text") == "复制帧号":
                copy_btn = w
                break
        except tk.TclError:
            pass
        stack.extend(w.winfo_children())
    report("复制:控制条存在「复制帧号」按钮", copy_btn is not None)
    if copy_btn is not None:
        try:
            root.clipboard_clear()
            copy_btn.invoke()
            pump(root, 0.2)
            clip = root.clipboard_get()
            report("复制:剪贴板内容 = 当前帧号",
                   clip == str(int(player.current_frame_idx)),
                   f"clip={clip!r} frame={player.current_frame_idx}")
        except tk.TclError as exc:
            report("复制:剪贴板内容 = 当前帧号", False, str(exc))

    # ---------- 截图 ----------
    root.update_idletasks()
    x, y = root.winfo_rootx(), root.winfo_rooty()
    w, h = root.winfo_width(), root.winfo_height()
    full = ImageGrab.grab()
    scale = full.size[0] / root.winfo_screenwidth()
    img = ImageGrab.grab(
        bbox=(int(x * scale), int(y * scale), int((x + w) * scale), int((y + h) * scale))
    )
    out = _REPO / ".cache" / "real_window_fixes.png"
    img.save(out)
    print(f"screenshot: {out}", flush=True)

    # ---------- 清理 ----------
    try:
        tasks.cancel_all()
    except Exception:
        pass
    try:
        player.close(timeout=2.0)
    except Exception:
        pass
    try:
        tasks.close(timeout=2.0)
    except Exception:
        pass
    root.destroy()

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    if failed:
        print("FAILED: " + "; ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
