# main.py —— 程序入口（PanedWindow 实现可拖动左右分隔）

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import os
import multiprocessing   # ProcessPoolExecutor 需要在入口处 freeze_support

from settings_panel import SettingsPanel
from preview_player import VideoPreviewPlayer
from task_manager import TaskManager


def main():
    root = tk.Tk()
    root.title("明日方舟剪辑工具")
    root.geometry("1380x920")
    root.minsize(900, 600)
    tasks = TaskManager(root)
    closing = False

    # 顶部工具栏
    top = ttk.Frame(root)
    top.pack(fill=tk.X, padx=10, pady=6)
    ttk.Label(top, text="视频:").pack(side=tk.LEFT)
    input_var = tk.StringVar()
    ttk.Entry(top, textvariable=input_var, width=60).pack(side=tk.LEFT, padx=5)

    # ---- 可拖动左右面板 ----
    paned = tk.PanedWindow(root, orient=tk.HORIZONTAL,
                           sashwidth=6,          # 分隔条宽度（px）
                           sashrelief=tk.RAISED,
                           bg="#555555")
    paned.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)

    # 左：播放器
    left_frame = ttk.Frame(paned)
    paned.add(left_frame, stretch="always", minsize=500)

    # 右：设置面板（默认宽度 360，可拖到更宽）
    right_frame = ttk.Frame(paned)
    paned.add(right_frame, stretch="never", minsize=240, width=360)

    settings = SettingsPanel(right_frame, task_manager=tasks)
    settings.pack(fill=tk.BOTH, expand=True)

    player = VideoPreviewPlayer(left_frame, settings=settings, task_manager=tasks)
    player.pack(fill=tk.BOTH, expand=True)

    # 绑定导出
    settings.export_callback = player.export_video
    settings.segment_export_callback = player.export_segments
    # 绑定批量暂停模式按钮
    settings.apply_pause_callback = player.apply_pause_mode

    def open_file():
        if closing:
            return
        path = filedialog.askopenfilename(
            filetypes=[("视频文件", "*.mp4 *.avi *.mov *.mkv"),
                       ("所有文件",  "*.*")])
        if not path:
            return
        previous_path = input_var.get()
        previous_output = settings.output_var.get()
        input_var.set(path)
        if not settings.output_var.get():
            name, _ = os.path.splitext(path)
            settings.output_var.set(f"{name}_clipped.mp4")
        if player.load_video(path) is False:
            input_var.set(previous_path)
            settings.output_var.set(previous_output)
            messagebox.showerror(
                "无法切换视频",
                "旧视频的解码线程未能及时退出；为避免同时打开两个解码器，本次切换已取消。",
            )

    open_btn = ttk.Button(top, text="打开视频", command=open_file)
    open_btn.pack(side=tk.LEFT, padx=5)

    def close_app():
        nonlocal closing
        if closing:
            return
        closing = True
        try:
            open_btn.config(state=tk.DISABLED)
            root.config(cursor="watch")
            root.update_idletasks()
        except Exception:
            pass

        io_closed = True
        survivors = []
        try:
            tasks.cancel_all()
            try:
                io_closed = player.close(timeout=1.5)
            except Exception as exc:
                io_closed = False
                print(f"[shutdown] player close failed: {exc}", flush=True)
            try:
                settings.close()
            except Exception as exc:
                print(f"[shutdown] settings close failed: {exc}", flush=True)
            try:
                survivors = tasks.close(timeout=2.0)
            except Exception as exc:
                print(f"[shutdown] task close failed: {exc}", flush=True)
        finally:
            if not io_closed:
                print("[shutdown] video IO thread did not exit before deadline", flush=True)
            if survivors:
                print(f"[shutdown] task survivors: {survivors}", flush=True)
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", close_app)

    root.mainloop()


if __name__ == "__main__":
    # Windows 下用 PyInstaller/cx_Freeze 打包时必须调用，
    # 否则 ProcessPoolExecutor 会递归启动子进程崩溃
    multiprocessing.freeze_support()
    main()
