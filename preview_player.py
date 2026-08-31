# preview_player.py — 视频预览播放器

import tkinter as tk
from tkinter import ttk
import cv2
import inspect
import numpy as np
import PIL.Image
import PIL.ImageTk
import os
import subprocess
import tempfile
import threading
import time
import ctypes
import hashlib
from collections import deque
from pathlib import Path
from queue import Queue, Empty

from frame_types import (FRAME_TYPE_NORMAL, FRAME_TYPE_PAUSE,
                         FRAME_TYPE_1X, FRAME_TYPE_2X, FRAME_TYPE_0_2X)
from video_io import VideoIOThread, CMD_SEEK, CMD_SEEK_LATEST, CMD_PLAY, CMD_STOP
from cv_engine import CvEngine
from mpv_engine import MpvEngine
from preview_engine import (
    PreviewEngine,
    PreviewEngineError,
    PreviewPlayRequest,
    SourceSeekRequest,
)
from timeline_widget import TimelineWidget
from timeline_plan import TimelinePlan
from media_exporter import ExportRequest, ExportResult, MediaExporter
from media_info import resolve_ffmpeg_path
from task_manager import TaskCancelled, TaskContext, TaskHandle, TaskManager
from project_state import ProjectState
from edit_commands import SetClipBounds, SetPauseMaskRun, SetPauseMode


_ANALYSIS_TASK = "player.analysis"
_MEDIA_INFO_TASK = "player.media_info"
_FRAME_PTS_TASK = "player.frame_pts_certification"
_EXPORT_TASK = "player.export.full"
_SEGMENT_EXPORT_TASK = "player.export.segments"


def _run_analysis_task(context: TaskContext, snapshot: dict) -> tuple:
    """Run video analysis without touching Tk or mutable player state."""
    import analyzer

    video_path = snapshot["video_path"]
    proc_res = list(snapshot["proc_res"])
    context.checkpoint()

    cap_tmp = cv2.VideoCapture(video_path)
    try:
        ret, first_frame = cap_tmp.read()
    finally:
        cap_tmp.release()
    if ret and proc_res[1] == 225:
        height, width = first_frame.shape[:2]
        proc_res[1] = int(proc_res[0] * height / width)
    proc_res = tuple(proc_res)

    context.checkpoint()
    configs, loaded = analyzer.load_templates(proc_res)
    context.checkpoint()

    backend_key = snapshot["backend_key"]
    backend_label = snapshot["backend_label"]
    ffmpeg_path = snapshot.get("ffmpeg_path")

    def progress(ratio):
        context.checkpoint()
        context.report((backend_label, float(ratio)))

    print(
        f"[analyze] decode_backend={backend_key} "
        f"ffmpeg_path={ffmpeg_path or 'auto'} "
        f"proc_res={proc_res} video={video_path}"
    )
    backend_note = snapshot.get("backend_note") or ""
    if backend_note:
        print(f"[analyze] {backend_note}", flush=True)

    states, diffs, analysis_context = analyzer.analyze_video_with_context(
        video_path,
        configs,
        snapshot["thresholds"],
        proc_res,
        snapshot["batch"],
        snapshot["threads"],
        progress,
        decode_backend=backend_key,
        ffmpeg_path=ffmpeg_path,
    )
    context.checkpoint()
    pauses, speeds = analyzer.build_segments(
        states,
        diffs,
        video_path,
        proc_res,
        snapshot["compare"],
        snapshot["fps"],
        progress,
        analysis_context=analysis_context,
    )
    context.checkpoint()
    used_context = analyzer.analysis_context_skips_second_scan(
        analysis_context, pauses, len(states)
    )
    print(
        f"[analyze] context complete="
        f"{analysis_context.get('complete') if isinstance(analysis_context, dict) else None} "
        f"L={len(states)} pause_boundary_records="
        f"{len(analysis_context.get('pause_boundary_diffs') or []) if isinstance(analysis_context, dict) else 0} "
        f"skip_second_scan={used_context}",
        flush=True,
    )
    return (
        states,
        diffs,
        pauses,
        speeds,
        backend_label,
        used_context,
        loaded == 0,
    )


def _run_media_info_task(context: TaskContext, snapshot: dict):
    """Probe one immutable source snapshot without touching Tk state."""
    import media_info

    context.checkpoint()
    result = media_info.probe_media(
        snapshot["video_path"],
        ffmpeg_path=snapshot.get("ffmpeg_path"),
    )
    context.checkpoint()
    return result


def _run_frame_pts_certification_task(context: TaskContext, snapshot: dict):
    """Build a source-scoped certification without touching Tk state."""
    import frame_pts_certifier

    context.checkpoint()
    result = frame_pts_certifier.produce_frame_pts_certification(
        snapshot["media_info"],
        evidence_path=snapshot.get("evidence_path"),
        cache_root=snapshot.get("cache_root"),
        checkpoint=context.checkpoint,
        publish_commit=context.commit,
    )
    context.checkpoint()
    return result


class VideoPreviewPlayer(tk.Frame):
    def __init__(
        self,
        parent,
        settings,
        video_path=None,
        width=800,
        height=450,
        *,
        task_manager: TaskManager | None = None,
        preview_engine: str | None = None,
        engine_factory=None,
    ):
        super().__init__(parent)
        self.settings = settings
        inherited_tasks = getattr(settings, "task_manager", None)
        self._owns_task_manager = task_manager is None and inherited_tasks is None
        self.task_manager = task_manager or inherited_tasks or TaskManager(self)
        self._analysis_handle: TaskHandle | None = None
        self._media_info_handle: TaskHandle | None = None
        self._frame_pts_handle: TaskHandle | None = None
        self._export_handle: TaskHandle | None = None
        self._segment_export_handle: TaskHandle | None = None
        self._closing = False
        requested_engine = (
            preview_engine
            or os.environ.get("ARKNIGHT_PREVIEW_ENGINE", "cv")
        ).strip().lower()
        self._preview_engine_kind = requested_engine if requested_engine in {"cv", "mpv"} else "cv"
        self._engine_factory = engine_factory
        self.video_path = video_path
        self.media_info = None
        self.media_info_error: Exception | None = None
        self.frame_pts_error: Exception | None = None
        self.frame_pts_status: str | None = None

        self.total_frames: int = 0
        self.fps: float = 30.0
        self.current_frame_idx: int = 0

        self.canvas_w = width
        self.canvas_h = height

        self.pause_segments: list = []
        self.speed_segments: list = []
        self.clip_segments: list = []
        self.project_state = ProjectState()
        self.task_manager.invalidate_scope(
            project_generation=self.project_state.project_generation,
            timeline_revision=self.project_state.timeline_revision,
        )
        self.states_array = None
        self.diffs_array = None  # 新增：持久化保存帧差异，用于随时根据新参数重算裁剪区

        # Edits are mutable dictionaries for legacy UI compatibility, so keep
        # an explicit revision and cache only the immutable cut-only plan used
        # by frequent seek/play commands.  Export plans with speed policies
        # are built separately from their explicit settings snapshot.
        self._timeline_revision: int = self.project_state.timeline_revision
        self._cut_plan_cache: tuple[int, TimelinePlan] | None = None

        self.is_playing = False
        self._io: PreviewEngine | None = None
        # 略大于 2：解码偶发尖峰时少丢帧；渲染侧仍只取最新一帧
        self._frame_q: Queue = Queue(maxsize=4)
        self._canvas_img_id = None
        # T1-3：复用同一个 PhotoImage，避免每帧 Tcl 分配/释放
        self._photo = None
        self._photo_size: tuple[int, int] = (0, 0)

        self._key_held: str | None = None
        self._key_after_id: str | None = None
        self._key_hold_fired: bool = False
        self._key_preview_id: str | None = None
        self._render_after_id: str | None = None
        self._bind_after_id: str | None = None
        self._perf_after_id: str | None = None
        self._is_dragging: bool = False
        # 预览流畅度量化：最近一次完整播放段的快照（停播时冻结，便于抄数对比）
        self._perf_last: dict | None = None
        self._perf_ui_tick: int = 0
        self._last_display_mono: float | None = None
        self._ui_gap_ms_max: float = 0.0
        self._ui_frames: int = 0
        self._ui_gap_ms_sum: float = 0.0
        self._ui_stutter_n: int = 0  # 显示间隔 > 1.8×理想间隔
        # 倍率标定 V1/V2
        self._calib_after_id: str | None = None
        self._calib_active: bool = False
        self._calib_t0: float | None = None
        self._calib_f0: int = 0
        self._calib_ignore: bool = False
        self._calib_mode: str = ""  # single | pair_a | pair_b
        self._calib_pair_anchor: int = 0
        self._calib_last_line: str = ""
        self._calib_pair_lines: list[str] = []
        # scheme B: auto rate on normal stop
        self._auto_rate_t0: float | None = None
        self._auto_rate_f0: int = 0
        self._auto_rate_ignore: bool = False
        self._AUTO_RATE_MIN_S = 3.0
        # 原生渲染启动遮盖：mpv 首帧画出前盖一层黑底，避免露出白色子窗口
        self._native_cover: tk.Frame | None = None
        self._native_cover_after_id: str | None = None
        # 帧号屏显（mpv OSD）：节流计数 + 寻址后立即刷新一次
        self._osd_tick: int = 0
        self._osd_pending: bool = False
        # 原生 mpv 子窗口点击后会夺走键盘焦点（详见
        # _reclaim_focus_from_native_child）；连续两拍确认才拉回
        self._focus_reclaim_streak: int = 0
        # 暂停中步进寻址的采信保护：mpv 寻址是异步的，旧 time-pos 事件会经
        # snapshot_perf 把 UI 帧号拉回寻址前的值（左右键看似失灵）。
        # 保护窗口内渲染循环不回写 current_frame_idx。
        self._paused_seek_guard_until: float = 0.0
        # 暂停静帧覆盖层（MLT/Shotcut 模式）：暂停态的画面由独立解码管线
        # 出静帧贴到覆盖层，帧精确；mpv 只负责播放。gen 丢弃过期解码。
        self._overlay_gen: int = 0
        self._still_overlay = None
        self._still_photo = None
        self._still_last_gray = None
        self._still_pending = None
        self._still_decoding: bool = False
        self._was_playing: bool = False
        # PyAV 静帧解码（API 层主选）：关键帧索引表后台构建（npz 缓存），
        # 未就绪/未安装时自动回退 ffmpeg CLI 两段式，再回退 OpenCV
        self._pyav_kf = None
        self._pyav_state = "idle"
        self._pyav_container = None
        # (monotonic, source_frame, mpv_drop_count) 采样，用于右上角
        # 瞬时播放帧率（≈1s 窗口，丢帧不计入“看到的”帧率）
        self._fps_samples: deque = deque(maxlen=64)

        self._setup_ui()
        if video_path: self.load_video(video_path)

        # 绑定单段与批量事件
        self.settings.apply_pause_callback = self.apply_pause_mode
        self.settings.single_pause_callback = self.set_single_pause_mode
        self.timeline.on_pause_select_cb = self._on_timeline_pause_select

    # ==========================================================
    #  UI 构建
    # ==========================================================
    def _setup_ui(self):
        # Native mpv rendering receives this dedicated child Frame WID.  The
        # Canvas remains the sole RGB target for CvEngine.
        self.video_surface = tk.Frame(self, bg="black")
        self.video_surface.pack(pady=5, fill=tk.BOTH, expand=True)
        self.video_canvas = tk.Canvas(
            self.video_surface, width=self.canvas_w, height=self.canvas_h, bg="black"
        )
        self.video_canvas.pack(fill=tk.BOTH, expand=True)
        self.mpv_host = tk.Frame(self.video_surface, bg="black")
        self.video_surface.bind("<Configure>", self._on_video_surface_configure, add="+")
        self.video_surface.bind("<Button-1>", lambda _event: self._focus_preview(), add="+")
        self.mpv_host.bind("<Button-1>", lambda _event: self._focus_preview(), add="+")
        self.video_canvas.bind("<Button-1>", lambda _event: self._focus_preview())
        # 右键：复制帧号/帧率到剪贴板（OSD 画在视频上没法选中复制）
        for _w in (self.video_surface, self.mpv_host, self.video_canvas):
            _w.bind("<Button-3>", self._on_video_right_click, add="+")

        self.timeline = TimelineWidget(self)
        self.timeline.pack(fill=tk.X, padx=10)
        self.timeline.on_seek_cb = self._on_tl_seek
        self.timeline.on_handle_end_cb = self._on_tl_drag_end
        self.timeline.on_edit_cb = self._on_timeline_edit
        self.timeline.canvas.bind("<Button-1>", lambda _event: self._focus_preview(), add='+')

        ctrl = ttk.Frame(self)
        ctrl.pack(fill=tk.X, pady=5)

        self.btn_play = ttk.Button(ctrl, text="▶ 播放", command=self.toggle_play)
        self.btn_play.pack(side=tk.LEFT, padx=5)

        ttk.Label(ctrl, text="倍速:").pack(side=tk.LEFT, padx=(10, 2))
        self.preview_speed_var = tk.StringVar(value="1x")
        speed_combo = ttk.Combobox(
            ctrl, textvariable=self.preview_speed_var,
            values=["0.1x", "0.25x", "0.5x", "1x", "2x", "4x"], width=6, state="readonly")
        speed_combo.pack(side=tk.LEFT, padx=2)
        speed_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_preview_option_change())

        self.btn_analyze = ttk.Button(ctrl, text="自动模板分析", command=self._start_analysis)
        self.btn_analyze.pack(side=tk.LEFT, padx=10)

        self.skip_trimmed = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            ctrl, text="预览时跳过裁剪区", variable=self.skip_trimmed,
            command=self._on_preview_option_change,
        ).pack(side=tk.LEFT, padx=5)

        # 关闭后预览仍按设置对 1x/0.2x 区抽帧加速；勾选后预览只跟上方「倍速」，便于排查卡顿
        self.preview_ignore_speedup_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            ctrl, text="预览忽略业务加速", variable=self.preview_ignore_speedup_var,
            command=self._on_preview_option_change,
        ).pack(side=tk.LEFT, padx=5)

        # mpv 原生渲染时用 mpv OSD 在画面上叠加当前源帧号（编辑时直接读屏）
        self.show_frame_osd_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            ctrl, text="屏显帧号", variable=self.show_frame_osd_var,
        ).pack(side=tk.LEFT, padx=5)

        # mpv 原生窗口会吞掉视频区右键，复制帧号/帧率的入口放在控制条上
        ttk.Button(ctrl, text="复制帧号", width=8,
                   command=lambda: self._copy_osd_text("frame")).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(ctrl, text="复制帧率", width=8,
                   command=lambda: self._copy_osd_text("fps")).pack(side=tk.LEFT, padx=(4, 0))

        # 默认开优化；取消勾选 / 按 O = #9 式基线（只统计，不追帧软锚）
        self.preview_opt_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            ctrl, text="预览优化", variable=self.preview_opt_var,
            command=self._on_preview_opt_change,
        ).pack(side=tk.LEFT, padx=5)

        self.lbl_time = ttk.Label(ctrl, text="00:00 / 00:00")
        self.lbl_time.pack(side=tk.RIGHT, padx=10)

        self.lbl_info = ttk.Label(self, text="就绪", foreground="#00CED1", font=("Consolas", 10))
        self.lbl_info.pack(fill=tk.X, padx=10, pady=2)

        perf_row = ttk.Frame(self)
        perf_row.pack(fill=tk.X, padx=10, pady=(0, 2))
        self.lbl_perf = ttk.Label(
            perf_row,
            text="流畅度: 播放后显示（迟到率 / 追帧 / 单帧耗时）",
            foreground="#888888",
            font=("Consolas", 9),
        )
        self.lbl_perf.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(perf_row, text="复制流畅度", width=10, command=self._copy_perf_stats).pack(
            side=tk.RIGHT, padx=(6, 0)
        )
        ttk.Button(perf_row, text="清零统计", width=8, command=self._reset_perf_stats_ui).pack(
            side=tk.RIGHT
        )
        ttk.Button(perf_row, text="标定10s", width=8, command=self._calib_start_single).pack(
            side=tk.RIGHT, padx=(6, 0)
        )
        ttk.Button(perf_row, text="对比标定", width=8, command=self._calib_start_pair).pack(
            side=tk.RIGHT, padx=(6, 0)
        )
        ttk.Button(perf_row, text="复制标定", width=8, command=self._calib_copy).pack(
            side=tk.RIGHT, padx=(6, 0)
        )

        calib_row = ttk.Frame(self)
        calib_row.pack(fill=tk.X, padx=10, pady=(0, 2))
        self.lbl_calib = ttk.Label(
            calib_row,
            text="倍率: 播≥3秒停即显示(跳裁剪不虚高)；也可标定10s/对比",
            foreground="#888888",
            font=("Consolas", 9),
        )
        self.lbl_calib.pack(side=tk.LEFT, fill=tk.X, expand=True)

        hint = "← → 逐帧  |  空格 播放  |  O 切换预览优化  |  时间轴：右键黄块 / 中键平移"
        ttk.Label(self, text=hint, foreground="#555555", font=("Consolas", 8)).pack(fill=tk.X, padx=10, pady=(0, 2))

        self._render_loop()
        self._bind_after_id = self.after_idle(self._bind_keys)

    def _show_engine_surface(self, native: bool, engine: PreviewEngine | None = None) -> None:
        if native:
            self.video_canvas.pack_forget()
            self.mpv_host.pack(fill=tk.BOTH, expand=True)
            self._raise_native_cover()
        else:
            self.mpv_host.pack_forget()
            self._drop_native_cover()
            self.video_canvas.pack(fill=tk.BOTH, expand=True)
        try:
            self.video_surface.update_idletasks()
            target = engine or self._io
            if target is not None:
                width = max(1, self.video_surface.winfo_width() or self.canvas_w)
                height = max(1, self.video_surface.winfo_height() or self.canvas_h)
                target.set_viewport(width, height, self._preview_dpi_scale())
        except Exception:
            pass

    def _osd_texts(self) -> tuple[str, str]:
        getter = getattr(self._io, "get_osd_texts", None)
        if not callable(getter):
            return "", ""
        try:
            return getter()
        except Exception:
            return "", ""

    def _copy_osd_text(self, kind: str) -> None:
        """复制帧号（纯数字，方便直接贴进排查工具）或帧率读数到剪贴板。"""
        if kind == "frame":
            self._copy_to_clipboard(str(int(self.current_frame_idx)))
            return
        _frame_text, fps_text = self._osd_texts()
        if fps_text:
            self._copy_to_clipboard(fps_text)
        else:
            try:
                self.lbl_info.config(text="暂无可复制的帧率读数")
            except Exception:
                pass

    def _on_video_right_click(self, event) -> None:
        """视频区右键：复制帧号/帧率菜单。

        原生 mpv 子窗口会吞掉右键事件，此菜单实际只对 CV 引擎触发；
        mpv 下用控制条上的「复制帧号/复制帧率」按钮。
        """
        if self._closing:
            return
        _frame_text, fps_text = self._osd_texts()
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(
            label=f"复制帧号  ({int(self.current_frame_idx)})",
            command=lambda: self._copy_osd_text("frame"),
        )
        menu.add_command(
            label=f"复制帧率  ({fps_text or '无'})",
            command=lambda: self._copy_osd_text("fps"),
            state=(tk.NORMAL if fps_text else tk.DISABLED),
        )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _copy_to_clipboard(self, text: str) -> None:
        if not text:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.lbl_info.config(text=f"已复制: {text}")
        except Exception:
            pass

    def _focus_preview(self) -> None:
        """Keep global playback shortcuts active after a native WID click."""
        try:
            self.focus_set()
            self.winfo_toplevel().focus_force()
        except Exception:
            pass

    def _preview_dpi_scale(self) -> float:
        try:
            # Tk reports physical pixels per inch; 96 is the Windows logical
            # baseline used by libmpv's WID viewport.
            return max(0.25, float(self.winfo_fpixels("1i")) / 96.0)
        except Exception:
            return 1.0

    def _on_video_surface_configure(self, event) -> None:
        if self._closing or self._io is None:
            return
        try:
            self._io.set_viewport(
                max(1, int(event.width)),
                max(1, int(event.height)),
                self._preview_dpi_scale(),
            )
        except Exception:
            pass

    def _make_preview_engine(self, path: str) -> PreviewEngine:
        # CvEngine owns the only long-lived OpenCV capture.  Avoid opening a
        # second probe capture on that path; mpv/factory engines need the small
        # container-shape hint before their first request.
        if self._engine_factory is None and self._preview_engine_kind == "cv":
            engine = CvEngine(path, self._frame_q)
            engine.start()
            self._show_engine_surface(bool(getattr(engine, "native_rendering", False)), engine)
            return engine

        cap = cv2.VideoCapture(path)
        try:
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        finally:
            cap.release()
        if self._engine_factory is not None:
            factory_kwargs = {
                "frame_queue": self._frame_q,
                "wid": self.mpv_host.winfo_id(),
                "fps": fps,
                "total": total,
            }
            try:
                parameters = inspect.signature(self._engine_factory).parameters
                accepts_kwargs = any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters.values()
                )
                if not accepts_kwargs:
                    factory_kwargs = {
                        key: value
                        for key, value in factory_kwargs.items()
                        if key in parameters
                    }
            except (TypeError, ValueError):
                # Some extension callables do not expose a Python signature;
                # pass the full documented factory contract in that case.
                pass
            engine = self._engine_factory(path, **factory_kwargs)
        elif self._preview_engine_kind == "mpv":
            try:
                engine = MpvEngine(
                    path,
                    wid=self.mpv_host.winfo_id(),
                    fps=fps,
                    total=total,
                    dll_dir=os.environ.get("MPV_DLL_DIR"),
                    edl_dir=Path(__file__).resolve().parent / ".cache" / "preview_edl",
                )
                engine.start()
            except PreviewEngineError as exc:
                if exc.code not in {"MPV_DLL_MISSING", "MPV_IMPORT_FAILED"}:
                    raise
                # Keep the established OpenCV path usable when the optional
                # project-owned mpv runtime is absent or cannot import.
                self._preview_engine_kind = "cv"
                engine = CvEngine(path, self._frame_q)
        else:
            engine = CvEngine(path, self._frame_q)
        if not getattr(engine, "_started", False):
            engine.start()
        self._show_engine_surface(bool(getattr(engine, "native_rendering", False)), engine)
        return engine

    # ==========================================================
    #  视频加载
    # ==========================================================
    def close(self, timeout: float = 1.0) -> bool:
        """Stop UI timers, invalidate analysis, and close the decoder."""
        if self._closing:
            io_closed = True
            if self._io is not None:
                io_closed = self._io.close(timeout=timeout)
                if io_closed:
                    self._io = None
            survivors = (
                self.task_manager.close(timeout=timeout)
                if getattr(self, "_owns_task_manager", False)
                else []
            )
            return io_closed and not survivors
        self._closing = True
        self._pyav_reset()
        self.task_manager.invalidate(_ANALYSIS_TASK)
        self.task_manager.invalidate(_MEDIA_INFO_TASK)
        self.task_manager.invalidate(_FRAME_PTS_TASK)
        self.task_manager.invalidate(_EXPORT_TASK)
        self.task_manager.invalidate(_SEGMENT_EXPORT_TASK)
        self._analysis_handle = None
        self._media_info_handle = None
        self._frame_pts_handle = None
        self._export_handle = None
        self._segment_export_handle = None
        self.is_playing = False
        self._key_held = None
        self._calib_active = False

        for attr in (
            "_render_after_id",
            "_bind_after_id",
            "_key_after_id",
            "_key_preview_id",
            "_calib_after_id",
            "_perf_after_id",
            "_native_cover_after_id",
        ):
            after_id = getattr(self, attr, None)
            if after_id is not None:
                try:
                    self.after_cancel(after_id)
                except Exception:
                    pass
                setattr(self, attr, None)

        io_thread = self._io
        if io_thread is None:
            io_closed = True
        else:
            io_closed = io_thread.close(timeout=timeout)
            if io_closed:
                self._io = None
        while True:
            try:
                self._frame_q.get_nowait()
            except Empty:
                break

        if getattr(self, "_owns_task_manager", False):
            survivors = self.task_manager.close(timeout=timeout)
            return io_closed and not survivors
        return io_closed

    def destroy(self):
        self.close(timeout=1.0)
        super().destroy()

    def _invalidate_analysis_for_reload(self) -> None:
        """Make every result from the previous source video stale."""
        self.task_manager.invalidate(_ANALYSIS_TASK)
        self.task_manager.invalidate(_MEDIA_INFO_TASK)
        self.task_manager.invalidate(_FRAME_PTS_TASK)
        self.task_manager.invalidate(_EXPORT_TASK)
        self.task_manager.invalidate(_SEGMENT_EXPORT_TASK)
        self._analysis_handle = None
        self._media_info_handle = None
        self._frame_pts_handle = None
        self._export_handle = None
        self._segment_export_handle = None
        try:
            self.btn_analyze.config(state=tk.NORMAL, text="自动模板分析")
        except Exception:
            pass
        for attr in ("export_btn", "segment_export_btn"):
            button = getattr(self.settings, attr, None)
            if button is not None:
                try:
                    button.config(state=tk.NORMAL)
                except Exception:
                    pass

    def load_video(self, path: str):
        if self._closing:
            return
        self._invalidate_analysis_for_reload()
        if self._io is not None:
            old_io = self._io
            self._io = None
            if not old_io.close(timeout=1.0):
                self._io = old_io
                print(
                    f"[video-io] close timeout while replacing {old_io.path!r}",
                    flush=True,
                )
                return False
        while True:
            try:
                self._frame_q.get_nowait()
            except Empty:
                break

        self.video_path = path
        self._drop_still_overlay()
        self._pyav_reset()
        self.media_info = None
        self.media_info_error = None
        self.frame_pts_error = None
        self.frame_pts_status = None
        self.total_frames = 0
        self.fps = 30.0
        self.current_frame_idx = 0
        # A new source starts a new project generation.  Publish fresh lists
        # instead of clearing the lists still referenced by the old timeline.
        with self.task_manager.scope_transition():
            project_snapshot = self.project_state.replace_project()
            self.task_manager.invalidate_scope(
                project_generation=project_snapshot.project_generation,
                timeline_revision=project_snapshot.timeline_revision,
            )
        self._publish_project_snapshot(project_snapshot)
        self.states_array = None
        self.diffs_array = None
        self._cut_plan_cache = None
        self.is_playing = False
        self.btn_play.config(text="▶ 播放")
        self._canvas_img_id = None
        self._photo = None
        self._photo_size = (0, 0)
        self.timeline.selected_pause_id = None
        self.settings.set_selected_pause(None, "")

        self._io = self._make_preview_engine(path)
        self.fps = self._io.fps
        self.total_frames = self._io.total
        if self._preview_engine_kind == "mpv":
            self._start_pyav_table_build()

        self.timeline.total_frames = self.total_frames
        self.timeline.fps = self.fps
        self.timeline.zoom_level = 1.0
        self.timeline.scroll_offset = 0.0
        self.timeline.current_frame_idx = 0
        self.timeline.mark_dirty()

        # 移除原先的 if not 判断，强制更新导出路径
        name, _ = os.path.splitext(path)
        self.settings.output_var.set(f"{name}_clipped.mp4")

        self._seek(0)
        self.timeline.redraw()
        self._start_media_info_probe()
        return True

    def _start_media_info_probe(self) -> None:
        """Probe the current source; missing PTS certification remains blocking."""
        if self._closing or not self.video_path:
            return

        try:
            params = self.settings.get_params()
        except Exception:
            params = {}
        source_path = str(Path(self.video_path).expanduser().resolve())
        project_generation, _timeline_revision = self._task_scope()
        snapshot = {
            "video_path": source_path,
            "ffmpeg_path": params.get("ffmpeg_path"),
            "frame_pts_evidence_path": params.get("frame_pts_evidence_path"),
            "frame_pts_cache_root": params.get("frame_pts_cache_root"),
        }

        def on_success(result) -> None:
            if self._closing:
                return
            if Path(result.source_path).resolve() != Path(source_path):
                return
            source_is_current = getattr(result, "source_is_current", None)
            if callable(source_is_current) and not source_is_current():
                return
            self.media_info = result
            self.media_info_error = None
            self.frame_pts_error = None
            self.frame_pts_status = "PENDING"
            self._start_frame_pts_certification(result, snapshot)

        def on_error(exc: BaseException) -> None:
            if self._closing:
                return
            self.media_info = None
            self.media_info_error = exc
            self.frame_pts_error = None
            self.frame_pts_status = None
            print(
                f"[media-info] probe blocked for {source_path}: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )

        def on_done(_status) -> None:
            self._media_info_handle = None

        try:
            self._media_info_handle = self.task_manager.submit(
                _MEDIA_INFO_TASK,
                lambda context: _run_media_info_task(context, snapshot),
                on_success=on_success,
                on_error=on_error,
                on_done=on_done,
                project_generation=project_generation,
                timeline_revision=None,
                replace=True,
            )
        except RuntimeError as exc:
            self.media_info_error = exc
            self._media_info_handle = None

    def _start_frame_pts_certification(
        self,
        media_snapshot,
        probe_snapshot: dict | None = None,
    ) -> None:
        """Certify the current source; timeline edits do not cancel this task."""
        if self._closing or not self.video_path:
            return
        source_path = str(Path(self.video_path).expanduser().resolve())
        if Path(getattr(media_snapshot, "source_path", "")).resolve() != Path(source_path):
            return
        project_generation, _timeline_revision = self._task_scope()
        params = probe_snapshot or {}
        snapshot = {
            "media_info": media_snapshot,
            "evidence_path": params.get("frame_pts_evidence_path"),
            "cache_root": params.get("frame_pts_cache_root"),
        }

        def on_success(result) -> None:
            if self._closing or Path(self.video_path).resolve() != Path(source_path):
                return
            self.media_info = result.media_info
            self.frame_pts_status = result.status
            certified = bool(getattr(result, "certified", False))
            if not certified:
                self.frame_pts_error = RuntimeError(
                    "frame PTS certification blocked: "
                    + ", ".join(result.reason_codes)
                )
            else:
                self.frame_pts_error = None
                if self._io is not None:
                    # Binding re-derives the certified timeline; run it off
                    # the Tk owner thread so certification completion cannot
                    # freeze the UI.  A play command racing the bind fails
                    # closed with the existing CERTIFICATION_REQUIRED notice.
                    io_ref = self._io
                    media_ref = self.media_info

                    def _bind_certification() -> None:
                        try:
                            io_ref.bind_media_info(media_ref)
                        except Exception as exc:
                            self.frame_pts_error = exc

                    threading.Thread(
                        target=_bind_certification,
                        name="arknight-preview-cert-bind",
                        daemon=True,
                    ).start()

        def on_error(exc: BaseException) -> None:
            if self._closing or Path(self.video_path).resolve() != Path(source_path):
                return
            self.frame_pts_status = "ERROR"
            self.frame_pts_error = exc

        def on_done(_status) -> None:
            self._frame_pts_handle = None

        try:
            self._frame_pts_handle = self.task_manager.submit(
                _FRAME_PTS_TASK,
                lambda context: _run_frame_pts_certification_task(context, snapshot),
                on_success=on_success,
                on_error=on_error,
                on_done=on_done,
                project_generation=project_generation,
                timeline_revision=None,
                replace=True,
            )
        except RuntimeError as exc:
            self.frame_pts_status = "ERROR"
            self.frame_pts_error = exc

    # ==========================================================
    #  IO 线程命令封装
    # ==========================================================
    def _canvas_wh(self) -> tuple:
        cw = self.video_surface.winfo_width() or self.canvas_w
        ch = self.video_surface.winfo_height() or self.canvas_h
        return (max(1, cw), max(1, ch))

    def _speed_segs_snap(self) -> list:
        return [(s['start'], s['end'], s['type']) for s in self.speed_segments]

    def _task_scope(self) -> tuple[int, int]:
        state = getattr(self, "project_state", None)
        if state is None:
            # Some isolated integration tests construct a legacy player stub.
            # Real player instances always initialize ProjectState in __init__.
            return 0, int(getattr(self, "_timeline_revision", 0))
        snapshot = state.snapshot()
        return snapshot.project_generation, snapshot.timeline_revision

    def _invalidate_derived_timeline_plan(self) -> None:
        """Discard derived plan data without changing the edit revision."""
        self._cut_plan_cache = None

    def _publish_project_snapshot(self, snapshot) -> None:
        """Publish detached compatibility views from the authoritative owner."""
        self.pause_segments, self.speed_segments, self.clip_segments = (
            snapshot.mutable_segments()
        )
        self._timeline_revision = snapshot.timeline_revision
        self._cut_plan_cache = None
        timeline = getattr(self, "timeline", None)
        if timeline is not None:
            (
                timeline.pause_segments,
                timeline.speed_segments,
                timeline.clip_segments,
            ) = snapshot.mutable_segments()

    def _apply_project_command(self, command) -> None:
        """Apply an immutable edit command and refresh all legacy render views."""
        self._apply_project_commands((command,))

    def _apply_project_commands(self, commands) -> None:
        """Apply a batch of owner-side commands, then publish one snapshot."""
        pending = tuple(commands)
        if not pending:
            return
        with self.task_manager.scope_transition():
            snapshot = self.project_state.apply_many(pending)
            self.task_manager.invalidate_scope(
                project_generation=snapshot.project_generation,
                timeline_revision=snapshot.timeline_revision,
            )
        self._publish_project_snapshot(snapshot)
        self.timeline.mark_dirty()
        self.timeline.redraw()

    def _on_timeline_edit(self, command) -> None:
        """Apply the immutable command emitted by TimelineWidget."""
        if not isinstance(command, (SetPauseMaskRun, SetClipBounds, SetPauseMode)):
            raise TypeError("timeline edits must be immutable EditCommand values")
        self._apply_project_command(command)
        # Clip drag completion already routes through _on_tl_drag_end, while
        # right-click mask edits have no separate mouse-up command.
        if isinstance(command, SetPauseMaskRun) and self.is_playing:
            self._send_play(self.current_frame_idx)

    def _build_timeline_plan(self, *, states=None,
                             speedup_1x: bool = False,
                             speedup_02: bool = False,
                             speedup_02_factor: int = 1):
        """Snapshot all mutable edit dictionaries into a validated plan."""
        import analyzer

        cacheable = (
            states is None
            and not speedup_1x
            and not speedup_02
            and speedup_02_factor == 1
        )
        if cacheable and self._cut_plan_cache is not None:
            cached_revision, cached_plan = self._cut_plan_cache
            if cached_revision == self._timeline_revision:
                return cached_plan

        state = getattr(self, "project_state", None)
        if state is None:
            pause_segments = self.pause_segments
            speed_segments = self.speed_segments
            clip_segments = self.clip_segments
        else:
            owner_snapshot = state.snapshot()
            pause_segments, speed_segments, clip_segments = (
                owner_snapshot.mutable_segments()
            )
            self._timeline_revision = owner_snapshot.timeline_revision

        if states is None:
            states = (
                self.states_array
                if self.states_array is not None
                else np.zeros(self.total_frames, dtype=np.int8)
            )
        plan = analyzer.build_timeline_plan(
            self.total_frames,
            states,
            pause_segments,
            speed_segments,
            clip_segments,
            speedup_1x,
            speedup_02,
            speedup_02_factor,
        )
        if cacheable:
            self._cut_plan_cache = (self._timeline_revision, plan)
        return plan

    def _all_skip_segs_snap(self) -> list:
        # VideoIO consumes half-open, globally sorted delete ranges.  Speed
        # policy stays separate in the current CV engine and is not duplicated
        # into this cut-only plan.
        return list(self._build_timeline_plan().deleted_ranges)

    def _preview_pts_ready(self) -> bool:
        media = self.media_info
        if media is None:
            return False
        # ``complete_for_export`` re-hashes the source and revalidates both
        # tools.  That is appropriate at the certification boundary, but this
        # helper runs on every drag/step seek.  The certification callback has
        # already performed that gate and binds the immutable evidence here.
        return bool(
            getattr(media, "frame_pts_authoritative", False)
            and getattr(media, "frame_pts_certification", None) is not None
            and self.frame_pts_error is None
        )

    def _seek_frame(self, frame_idx: int) -> None:
        """Mode-aware engine seek shared by arrow keys and timeline drags.

        With 跳过裁剪区 active on the native engine, playback runs the EDL
        view; a plain ``seek_source`` would force the engine back to the raw
        source stream (a reload plus a jump into deleted footage, which made
        ←/→ look dead).  Route through the EDL artifact instead when one has
        been built; otherwise the engine is in source mode already and a
        source seek is the consistent fallback.
        """
        if not self._io:
            return
        if not self.is_playing:
            # 暂停中寻址：引擎回读的旧帧号会经渲染循环把 UI 拉回原值，
            # 保护窗口内不采信引擎帧号（详见 _apply_native_perf）。
            # 保留已挂起的步进目标：寻址就是朝它去的，清掉会让渲染循环
            # 在保护窗过期后采信旧位置、把 UI 拉回去。
            self._paused_seek_guard_until = time.monotonic() + 0.25
        if (
            bool(getattr(self._io, "native_rendering", False))
            and bool(self.skip_trimmed.get())
            and bool(self._all_skip_segs_snap())
        ):
            seek_edl = getattr(self._io, "seek_edl", None)
            if callable(seek_edl):
                try:
                    seek_edl(int(frame_idx))
                    self._show_paused_still(int(frame_idx))
                    return
                except PreviewEngineError as exc:
                    if exc.code != "EDL_NOT_READY":
                        raise
                    # EDL 尚未建立（还没播放过）：此时引擎本就在源模式，
                    # 落到下面的源寻址即可，步进不该被卡住
        _project_generation, timeline_revision = self._task_scope()
        self._timeline_revision = timeline_revision
        try:
            self._io.seek_source(
                SourceSeekRequest(
                    source_frame=int(frame_idx),
                    canvas_size=self._canvas_wh(),
                    timeline_revision=timeline_revision,
                    # Before certification, only frame zero can be addressed
                    # by the mpv engine.  Once bound, all source seeks use the
                    # certified PTS table and absolute+exact mode.
                    exact=self._preview_pts_ready(),
                    latest_only=True,
                )
            )
        except PreviewEngineError as exc:
            if exc.code != "CERTIFICATION_REQUIRED":
                raise
            try:
                self.lbl_info.config(text="认证时间表尚未就绪，暂不能精确定位")
            except Exception:
                pass
        self._show_paused_still(int(frame_idx))

    def _show_paused_still(self, frame_idx: int) -> None:
        """暂停态静帧覆盖：独立解码管线出帧，帧精确（MLT/Shotcut 模式）。

        mpv 的暂停精确寻址在其源码语义里就不承诺帧级精度（普通 exact
        seek 有容差且寻址期丢帧，frame-step 才走 VERY_EXACT 路径），所以
        暂停画面不由 mpv 出：OpenCV 随机定位解码目标帧贴到覆盖层，mpv
        只负责播放。播放恢复时覆盖层撤下（渲染循环边缘检测）。
        """
        if self._closing or self.is_playing:
            return
        io = getattr(self, "_io", None)
        if io is None or not getattr(io, "native_rendering", False):
            return
        if not hasattr(self, "tk") or not self.video_path:
            return
        try:
            w = max(1, self.video_surface.winfo_width())
            h = max(1, self.video_surface.winfo_height())
        except Exception:
            return
        # 节流：已有解码在飞时跳过（渲染循环的追帧逻辑会在落地后补齐最新目标），
        # 避免拖时间轴时每个 tick 都 spawn 一个 ffmpeg 子进程
        if self._still_decoding:
            return
        # Tk/settings 只能在主线程读（Tcl 非线程安全）：这里取好全部参数，
        # 工作线程只拿纯值
        t = None
        fn = getattr(io, "source_time_for_frame", None)
        if callable(fn):
            t = fn(int(frame_idx))
        if t is None:
            fps = float(getattr(io, "fps", 0) or 0) or 60.0
            t = int(frame_idx) / fps
        try:
            ffmpeg_path = self.settings.get_params().get("ffmpeg_path")
        except Exception:
            ffmpeg_path = None
        # 碰撞刻度消歧依据：N 在暂停段内=暗帧，否则亮帧（主线程读段落）
        expected_bright = self._expected_bright(int(frame_idx))
        self._overlay_gen += 1
        gen = self._overlay_gen
        self._still_decoding = True
        threading.Thread(
            target=self._decode_still_work,
            args=(int(frame_idx), gen, w, h, float(t), ffmpeg_path,
                  expected_bright),
            daemon=True,
        ).start()

    def _expected_bright(self, frame_idx: int) -> bool:
        """N 的分析器语境亮度类：暂停段内=暗帧，其余=亮帧。

        原始容器存在刻度碰撞（两帧共用一个 pts，实测本源 24576 个共享
        刻度、683 个保留岛起点中 83 个踩中）：时间寻址在碰撞 tick 上拿到
        的第一帧可能是前一帧。抓 2 帧后按此语境挑对的那帧。
        """
        for seg in getattr(self, "pause_segments", []) or []:
            try:
                if int(seg.get("start", -1)) <= frame_idx <= int(seg.get("end", -1)):
                    return False
            except Exception:
                continue
        return True

    def _decode_still_work(self, frame_idx: int, gen: int,
                           w: int, h: int, t: float, ffmpeg_path,
                           expected_bright: bool) -> None:
        try:
            # 解码后端优先级：PyAV（API 层计数定帧，表就绪时）→
            # ffmpeg CLI 两段式（A_PT 工具链）→ OpenCV
            img = None
            if self._pyav_kf is not None:
                img = self._still_decode_pyav(frame_idx, w, h, expected_bright)
            if img is None:
                img = self._still_decode_ffmpeg(frame_idx, w, h, t, ffmpeg_path,
                                                expected_bright)
            if img is None:
                img = self._still_decode_opencv(frame_idx, w, h)
            if img is None:
                return
            arr = np.asarray(img)
            gray = cv2.cvtColor(cv2.resize(arr, (400, 225)),
                                cv2.COLOR_RGB2GRAY).astype(np.float32)
            # 工作线程不得调用 Tcl（非线程安全）：只写 pending 槽，
            # 由渲染循环主线程排空贴图
            self._still_pending = (img, gray, frame_idx, gen)
        except Exception:
            pass
        finally:
            self._still_decoding = False

    def _still_decode_ffmpeg(self, frame_idx: int, w: int, h: int,
                             t: float, ffmpeg_path, expected_bright: bool):
        try:
            ff = resolve_ffmpeg_path(ffmpeg_path)
            # 两段式精确取帧：输入 -ss 在本仓源上实测会落在目标前几帧
            # （解封装 seek 不精确，5409 落进前一段 PAUSE），所以退 0.5s
            # 粗寻址 + -copyts 保留原始时间戳 + select 按 PTS 截取目标帧。
            # 抓 2 帧：碰撞 tick 上两帧共用同一 pts，第一帧可能是前一段；
            # 用分析器语境（该帧应为暗/亮）挑对的那帧。
            back = max(0.0, float(t) - 0.5)
            cmd = [str(ff), "-y", "-loglevel", "error", "-copyts",
                   "-ss", f"{back:.6f}", "-i", str(self.video_path),
                   "-vf", f"select=gte(t\\,{float(t):.6f}),scale={w}:{h}",
                   "-vsync", "0", "-frames:v", "2",
                   "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
            out = subprocess.run(cmd, capture_output=True, timeout=10).stdout
            need = w * h * 3
            if len(out) < need:
                return None
            imgs = [PIL.Image.frombytes("RGB", (w, h), bytes(out[i * need:(i + 1) * need]))
                    for i in range(len(out) // need)]
            if len(imgs) == 1:
                return imgs[0]

            def luma(im):
                return float(np.asarray(im.convert("L")).mean())

            want = 62.0 if expected_bright else 40.0
            return min(imgs, key=lambda im: abs(luma(im) - want))
        except Exception:
            return None

    def _still_decode_opencv(self, frame_idx: int, w: int, h: int):
        try:
            cap = cv2.VideoCapture(str(self.video_path))
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ok, frame = cap.read()
            cap.release()
            if not ok or frame is None:
                return None
            rgb = cv2.cvtColor(cv2.resize(frame, (w, h)), cv2.COLOR_BGR2RGB)
            return PIL.Image.fromarray(rgb)
        except Exception:
            return None

    def _blit_still(self, img, gray, frame_idx: int, gen: int) -> None:
        if gen != self._overlay_gen or self._closing or self.is_playing:
            return
        if self._still_overlay is None:
            self._still_overlay = tk.Frame(self.video_surface, bg="black")
            self._still_label = tk.Label(self._still_overlay, bg="black")
            self._still_label.pack(fill=tk.BOTH, expand=True)
            # mpv 的帧号 OSD 画在 mpv 窗口内，会被本覆盖层盖住；
            # 暂停态在覆盖层上自绘同样的左上角帧号
            self._still_osd = tk.Label(
                self._still_overlay, bg="black", fg="white", anchor="w")
            self._still_osd.place(x=8, y=6)
            self._still_overlay.pack(fill=tk.BOTH, expand=True)
        self._still_photo = PIL.ImageTk.PhotoImage(img)
        self._still_label.config(image=self._still_photo)
        self._still_overlay.lift()
        try:
            if bool(self.show_frame_osd_var.get()):
                self._still_osd.config(
                    text=f"帧 {frame_idx:,} / {max(0, self.total_frames - 1):,}")
            else:
                self._still_osd.config(text="")
        except Exception:
            pass
        self._still_last_gray = gray
        self._still_frame_idx = frame_idx

    def _drain_still_pending(self) -> None:
        item = self._still_pending
        if item is None:
            return
        self._still_pending = None
        self._blit_still(*item)

    def _drop_still_overlay(self) -> None:
        self._overlay_gen += 1
        self._still_pending = None
        self._still_last_gray = None
        ov = self._still_overlay
        self._still_overlay = None
        if ov is not None:
            try:
                ov.destroy()
            except Exception:
                pass

    # ---- PyAV 静帧解码（API 层主选，MLT 计数式定帧 + 指纹验戳） ----

    @staticmethod
    def _pyav_frame_hash64(frame):
        """帧指纹：Y 平面 [::16,::16] 子采样的 blake2b-64（零拷贝直读平面）。

        建表（全片顺序解码）与查帧（seek 后计数）必须走同一函数，指纹
        才可用于把计数结果与账本逐位对账。实测单帧开销 ~0.02ms。
        """
        try:
            p = frame.planes[0]
            w, ls, hgt = p.width, p.line_size, frame.height
            buf = (ctypes.c_ubyte * p.buffer_size).from_address(p.buffer_ptr)
            y = np.frombuffer(buf, dtype=np.uint8).reshape(hgt, ls)[:, :w]
            small = np.ascontiguousarray(y[::16, ::16])
            return int.from_bytes(
                hashlib.blake2b(small.tobytes(), digest_size=8).digest(),
                "little")
        except Exception:
            return None

    def _pyav_table_path(self):
        if not self.video_path:
            return None
        base = Path(__file__).resolve().parent / ".cache" / "pyav_kf"
        try:
            base.mkdir(parents=True, exist_ok=True)
            st = os.stat(self.video_path)
            key = f"{Path(self.video_path).stem}_{st.st_size}_{int(st.st_mtime)}.npz"
            return base / key
        except OSError:
            return None

    def _pyav_reset(self) -> None:
        self._pyav_kf = None
        self._pyav_state = "idle"
        c = self._pyav_container
        self._pyav_container = None
        if c is not None:
            try:
                c.close()
            except Exception:
                pass

    def _start_pyav_table_build(self) -> None:
        """后台构建关键帧索引表（一次全片解码，npz 缓存按 大小+mtime 失效）。"""
        if self._pyav_state != "idle" or self._pyav_kf is not None:
            return
        try:
            import av  # noqa: F401
        except Exception:
            return  # 未安装 PyAV：静帧走 CLI/OpenCV 回退
        path = self._pyav_table_path()
        if path is None:
            return
        if path.exists():
            try:
                d = np.load(path)
                # v2 表必须带逐帧指纹；旧 v1 缓存缺 key 会在此抛错，
                # 落到下方重建（同路径覆盖升级）
                self._pyav_kf = (d["kf_indices"], d["kf_pts"],
                                 d["frame_hashes"])
                self._pyav_state = "ready"
                return
            except Exception:
                pass
        self._pyav_state = "building"
        video_path = str(self.video_path)
        threading.Thread(target=self._pyav_table_build_work,
                         args=(video_path, path), daemon=True).start()

    def _pyav_table_build_work(self, video_path: str, path) -> None:
        try:
            import av
            container = av.open(video_path)
            stream = container.streams.video[0]
            stream.thread_type = "AUTO"
            kf_idx, kf_pts, hashes = [], [], []
            i = 0
            for frame in container.decode(stream):
                if frame.pict_type == 1 and frame.pts is not None:
                    kf_idx.append(i)
                    kf_pts.append(int(frame.pts))
                h64 = self._pyav_frame_hash64(frame)
                hashes.append(0 if h64 is None else h64)
                i += 1
            container.close()
            if i < 2 or not kf_idx or self._closing \
                    or video_path != str(self.video_path):
                self._pyav_state = "idle"
                return
            st = os.stat(video_path)
            np.savez(path,
                     kf_indices=np.asarray(kf_idx, dtype=np.int64),
                     kf_pts=np.asarray(kf_pts, dtype=np.int64),
                     frame_hashes=np.asarray(hashes, dtype=np.uint64),
                     src_size=np.int64(st.st_size),
                     src_mtime=np.float64(st.st_mtime))
            if video_path == str(self.video_path) and not self._closing:
                self._pyav_kf = (np.asarray(kf_idx, dtype=np.int64),
                                 np.asarray(kf_pts, dtype=np.int64),
                                 np.asarray(hashes, dtype=np.uint64))
                self._pyav_state = "ready"
        except Exception:
            self._pyav_state = "idle"

    def _still_decode_pyav(self, frame_idx: int, w: int, h: int,
                           expected_bright: bool):
        """MLT 计数式定帧 + 指纹验戳：计数给唯一答案，指纹只做裁判。

        PyAV(FFmpeg 8) 的 pts 标签与认证账本在碰撞处不一致（实测 0~2 帧
        浮动），所以不按 pts 匹配目标：关键帧表只用于定位 seek 起点，
        之后纯计数到第 N 帧。计数结果与建表时的逐帧指纹对账——相等即
        精确命中；不等（关键帧踩碰撞刻度导致计数基准 ±1）则在 ±2 邻域
        找指纹相等者校正。指纹缺失（旧缓存过渡期）才退回邻域亮度语境
        挑选。seek 落点有内部错位（可能落上一 GOP）：越过 K 时退一个
        GOP 重试。
        """
        import bisect
        if self._pyav_kf is None:
            return None
        kf_idx, kf_pts = self._pyav_kf[0], self._pyav_kf[1]
        hashes = self._pyav_kf[2] if len(self._pyav_kf) > 2 else None
        j = bisect.bisect_right(kf_idx, frame_idx) - 1
        if j < 0:
            return None
        K, kpts = int(kf_idx[j]), int(kf_pts[j])
        container = self._pyav_container
        if container is None:
            try:
                import av
                container = av.open(str(self.video_path))
            except Exception:
                return None
            self._pyav_container = container
        stream = container.streams.video[0]
        offset = kpts
        cand, count = [], 0
        aligned, overshoot = False, False
        for _ in range(2):
            container.seek(offset, stream=stream, backward=True)
            cand, count = [], 0
            aligned, overshoot = False, False
            for f in container.decode(stream):
                if f.pts is None:
                    continue
                if not aligned:
                    if int(f.pts) < kpts:
                        continue
                    if int(f.pts) > kpts + 512:
                        overshoot = True
                        break
                    aligned = True
                cand.append(f)
                if count >= (frame_idx - K) + 2:
                    break
                count += 1
            if not overshoot:
                break
            offset = kpts - 3072  # 越过 K：退一个 GOP 重来
        rel = frame_idx - K
        if not aligned or len(cand) < rel + 1:
            return None
        best = None
        if hashes is not None and 0 <= frame_idx < len(hashes):
            want_h = int(hashes[frame_idx])
            if want_h:
                for o in (rel, rel - 1, rel + 1, rel - 2, rel + 2):
                    if 0 <= o < len(cand):
                        fh = self._pyav_frame_hash64(cand[o])
                        if fh is not None and fh == want_h:
                            best = cand[o]
                            break
        if best is None:
            # 指纹缺失/不可用的过渡期兜底：邻域 3 帧按暗/亮语境挑
            lo = max(0, rel - 1)
            group = cand[lo:rel + 2]
            want = 62.0 if expected_bright else 40.0

            def fluma(f):
                g = cv2.cvtColor(cv2.resize(f.to_ndarray(format="rgb24"),
                                            (400, 225)),
                                 cv2.COLOR_RGB2GRAY)
                return float(g.mean())

            best = min(group, key=lambda f: abs(fluma(f) - want))
        rgb = best.to_ndarray(format="rgb24")
        return PIL.Image.fromarray(rgb).resize((w, h))

    def _seek(self, frame_idx: int, skip_trim: bool = False):
        self._auto_rate_clear()
        if not self._io: return
        frame_idx = max(0, min(int(frame_idx), max(0, self.total_frames - 1)))
        self.current_frame_idx = frame_idx
        self.timeline.current_frame_idx = frame_idx
        self._osd_pending = True
        # 寻址会让瞬时帧率窗口出现跳变，清掉重新累积
        if self._fps_samples:
            self._fps_samples.clear()
        self._seek_frame(frame_idx)

    def _reject_play_request(self, message: str) -> None:
        """Return the UI to a stopped state when an engine rejects playback."""

        self.is_playing = False
        try:
            self._auto_rate_clear()
        except Exception:
            pass
        if getattr(self, "_calib_active", False):
            try:
                self._calib_cancel_timer()
            except Exception:
                pass
            self._calib_active = False
        try:
            self.btn_play.config(text="▶ 播放")
        except Exception:
            pass
        try:
            self.lbl_info.config(text=message)
        except Exception:
            pass

    def _send_play(self, start: int) -> bool:
        if not self._io:
            self._reject_play_request("预览引擎尚未就绪")
            return False
        self._apply_pace_mode_to_io()
        p = self.settings.get_params()

        speed_str = self.preview_speed_var.get().rstrip('x')
        try:
            speed = float(speed_str)
        except ValueError:
            speed = 1.0
        if speed >= 1.0:
            # ≥1x：用跳帧实现快进（2x→隔帧，4x→每 4 帧一显）
            preview_step = max(1, int(speed))
            speed_multiplier = 1.0
        else:
            preview_step = 1
            speed_multiplier = 1.0 / max(speed, 0.01)

        # mpv 原生引擎不支持业务变速段（MPV_SPEED_POLICY_UNSUPPORTED 会直接拒播），
        # 日用路径自动按「忽略业务加速」处理并提示，而不是让播放失败。
        native_engine = bool(getattr(self._io, "native_rendering", False))
        biz_active = (
            bool(p.get('speedup_1x')) or bool(p.get('speedup_02'))
            or bool(self._speed_segs_snap())
        )
        ignore_biz = bool(self.preview_ignore_speedup_var.get()) or native_engine
        if native_engine and biz_active and not bool(self.preview_ignore_speedup_var.get()):
            try:
                self.lbl_info.config(text="mpv 预览：业务变速段按原速播放（如需业务加速请切回 OpenCV 引擎）")
            except Exception:
                pass
        if self.total_frames <= 0:
            self._reject_play_request("当前源没有可播放帧")
            return False
        start = max(0, min(int(start), self.total_frames - 1))
        try:
            plan = self._build_timeline_plan()
            project_generation, timeline_revision = self._task_scope()
            request = PreviewPlayRequest(
                start_frame=start,
                playback_rate=speed,
                preview_step=preview_step,
                speed_multiplier=speed_multiplier,
                skip_trimmed=bool(self.skip_trimmed.get()),
                speedup_1x=False if ignore_biz else bool(p['speedup_1x']),
                speedup_02=False if ignore_biz else bool(p['speedup_02']),
                speedup_02_factor=int(p['speedup_02_factor']),
                timeline_plan=plan,
                speed_segments=() if ignore_biz else tuple(self._speed_segs_snap()),
                canvas_size=self._canvas_wh(),
                project_generation=project_generation,
                timeline_revision=timeline_revision,
                preview_step_cap=3,
                skip_trim_min_span=0,
            )
            accepted = bool(self._io.play(request))
            if not accepted:
                self._reject_play_request("预览引擎拒绝了播放请求")
                return False
            # 播放重新启动意味着位置跳变；瞬时帧率窗口从新的起点累积
            if self._fps_samples:
                self._fps_samples.clear()
            return True
        except PreviewEngineError as exc:
            message = (
                "认证时间表尚未就绪，无法播放剪辑预览"
                if exc.code == "CERTIFICATION_REQUIRED"
                else f"预览启动失败: {exc.code}"
            )
            self._reject_play_request(message)
            return False
        except (OSError, ValueError, TypeError) as exc:
            self._reject_play_request(f"预览启动失败: {exc}")
            return False
    def _on_preview_option_change(self):
        """倍速 / 跳裁剪 / 忽略业务加速 变更时，播放中立刻按新参数续播。"""
        if self.is_playing:
            self._auto_rate_mark_start()
            self._send_play(self.current_frame_idx)
        self._update_labels()

    def _pace_mode_str(self) -> str:
        return "opt" if bool(self.preview_opt_var.get()) else "base"

    def _apply_pace_mode_to_io(self) -> None:
        if not self._io:
            return
        try:
            self._io.set_pace_mode(self._pace_mode_str())
        except Exception:
            pass

    def _on_preview_opt_change(self):
        """勾选「预览优化」：opt=当前优化；关=base #9 式（仍统计）。"""
        if self._closing:
            return
        self._apply_pace_mode_to_io()
        if self.is_playing:
            # 换模式后重发 PLAY（内部 begin_perf_segment 会清零本段统计）
            self._reset_ui_gap_stats()
            self._send_play(self.current_frame_idx)
        self._refresh_perf_label(force=True)
        mode = "优化ON" if self.preview_opt_var.get() else "基线#9"
        try:
            self.lbl_info.config(text=f"预览节奏: {mode}（O 键切换）")
        except Exception:
            pass

    def _toggle_preview_opt(self, _event=None):
        if self._closing:
            return "break"
        focused = None
        try:
            focused = self.focus_get()
        except Exception:
            pass
        if isinstance(focused, (ttk.Entry, tk.Entry, ttk.Combobox)):
            return  # 输入框打字时不要切换 A/B
        self.preview_opt_var.set(not bool(self.preview_opt_var.get()))
        self._on_preview_opt_change()
        return "break"

    def _stop_playback_ui(self, from_user: bool = False) -> None:
        """UI 侧停播；from_user 时向 IO 发 STOP。片尾自动停时 IO 已 halt。"""
        if self._calib_active:
            if from_user:
                self._calib_cancel_timer()
                self._calib_active = False
                self._calib_set_label("倍率标定: 已取消（播放被手动停止）", "#cc4444")
        else:
            try:
                self._auto_rate_on_stop()
            except Exception:
                pass
        self.is_playing = False
        try:
            self.btn_play.config(text="▶ 播放")
        except Exception:
            pass
        if from_user:
            self._send_stop()
        if self._io:
            try:
                self._perf_last = self._io.snapshot_perf()
            except Exception:
                pass
        self._refresh_perf_label(force=True)

    def _sync_eof_stop(self) -> None:
        """片尾 IO 已停但按钮仍显示暂停时，自动对齐 UI 并冻结统计展示。"""
        if not self.is_playing or not self._io or self._is_dragging:
            return
        try:
            snap = self._io.snapshot_perf()
        except Exception:
            return
        # 仅片尾自动停；seek/用户暂停不要误触（拖进度条时 IO 也会短暂 inactive）
        if snap.get("playback_active"):
            return
        if snap.get("play_end_reason") != "eof":
            return
        self._stop_playback_ui(from_user=False)

    def _ideal_display_gap_ms(self) -> float:
        """当前预览倍速下，理想的显示间隔（ms）。≥1x 抽帧时仍按 1/fps 一拍。"""
        fps = float(self.fps) if self.fps else 30.0
        speed_str = self.preview_speed_var.get().rstrip('x')
        try:
            speed = float(speed_str)
        except ValueError:
            speed = 1.0
        if speed >= 1.0:
            return 1000.0 / max(fps, 1e-3)
        return 1000.0 / max(fps * max(speed, 0.01), 1e-3)

    def _reset_ui_gap_stats(self) -> None:
        self._last_display_mono = None
        self._ui_gap_ms_max = 0.0
        self._ui_frames = 0
        self._ui_gap_ms_sum = 0.0
        self._ui_stutter_n = 0

    def _note_ui_display(self) -> None:
        now = time.monotonic()
        if self._last_display_mono is not None:
            gap = (now - self._last_display_mono) * 1000.0
            self._ui_frames += 1
            self._ui_gap_ms_sum += gap
            if gap > self._ui_gap_ms_max:
                self._ui_gap_ms_max = gap
            if gap > self._ideal_display_gap_ms() * 1.8:
                self._ui_stutter_n += 1
        self._last_display_mono = now

    def _compose_perf_text(self) -> str:
        snap = None
        if self._io and self.is_playing:
            try:
                snap = self._io.snapshot_perf()
            except Exception:
                snap = self._perf_last
        elif self._perf_last is not None:
            snap = self._perf_last
        elif self._io:
            try:
                snap = self._io.snapshot_perf()
            except Exception:
                snap = None

        if not snap or int(snap.get("presented", 0)) <= 0:
            return "流畅度: 播放≥3秒 | 看 >1帧迟到% / 追帧 / p95解码 / seek / 尖峰"

        late_pct = float(snap.get("late_pct", 0.0))
        late1_pct = float(snap.get("late1_pct", 0.0))
        late2_pct = float(snap.get("late2_pct", 0.0))
        drop_pct = float(snap.get("drop_pct", 0.0))
        presented = int(snap.get("presented", 0))
        discarded = int(snap.get("discarded", 0))
        catchups = int(snap.get("catchup_events", 0))
        soft_r = int(snap.get("pace_resets", 0))
        hard_r = int(snap.get("hard_resets", 0))
        seeks = int(snap.get("seek_count", 0))
        q_drop = int(snap.get("q_drop", 0))
        absorbed = int(snap.get("skip_trim_absorbed", 0) or 0)
        cap_threads = int(snap.get("cap_threads", 0) or 0)
        avg_ms = float(snap.get("present_ms_avg", 0.0))
        max_ms = float(snap.get("present_ms_max", 0.0))
        p95_ms = float(snap.get("present_ms_p95", 0.0))
        lag_max = float(snap.get("lag_ms_max", 0.0))
        lag_p95 = float(snap.get("lag_ms_p95", 0.0))
        wall_s = float(snap.get("wall_s", 0.0))
        io_active = bool(snap.get("playback_active", False))
        spikes = snap.get("spikes") or []

        ideal_gap = self._ideal_display_gap_ms() / 1000.0
        media_s = presented * ideal_gap if ideal_gap > 0 else 0.0
        media_s += discarded * ideal_gap
        rt = (wall_s / media_s) if media_s > 1e-3 else 0.0

        ui_avg = (self._ui_gap_ms_sum / self._ui_frames) if self._ui_frames else 0.0
        ui_st = self._ui_stutter_n
        # 分档以 >1帧迟到为主（微抖 late 仅作参考）
        grade = self._grade_smoothness(late1_pct, drop_pct, avg_ms, rt)

        if self.is_playing and io_active:
            state = "播"
        elif self.is_playing and not io_active:
            state = "片尾"
        else:
            state = "停"

        pace_mode = str(snap.get("pace_mode") or self._pace_mode_str())
        mode_tag = "优化" if pace_mode == "opt" else "基线"

        spike_hint = ""
        if spikes:
            last = spikes[-1]
            rs = ",".join(last.get("reasons") or [])
            spike_hint = f" | 末尖峰f{last.get('frame')} {last.get('present_ms')}ms[{rs}]"

        text = (
            f"流畅度[{state}/{mode_tag}]{grade} | "
            f"微抖{late_pct:.0f}% >1帧{late1_pct:.0f}% >2帧{late2_pct:.0f}% | "
            f"追帧{discarded}拍/{catchups}次 | "
            f"seek{seeks} 软锚{soft_r} 硬重置{hard_r} | "
            f"解码{avg_ms:.0f}/p95 {p95_ms:.0f}/max{max_ms:.0f}ms | "
            f"落后p95 {lag_p95:.0f}/max{lag_max:.0f}ms | "
            f"实时比{rt:.2f} | "
            f"UI{ui_avg:.0f}/{self._ui_gap_ms_max:.0f}ms 顿{ui_st} q丢{q_drop} | "
            f"吸收{absorbed} 线程{cap_threads} | "
            f"{wall_s:.1f}s"
            f"{spike_hint}"
        )
        if str(snap.get("engine")) == "mpv":
            # mpv 原生渲染没有 per-frame 解码/落后统计；用 mpv 自己的丢帧
            # 计数和 time-pos 实测实时比作为权威读数。
            drops = snap.get("mpv_frame_drops")
            ratio = snap.get("rt_ratio")
            parts = []
            if drops is not None:
                parts.append(f"mpv丢帧{int(drops)}")
            if ratio is not None:
                parts.append(f"实测比{ratio:.2f}")
            if parts:
                text += " | " + " ".join(parts)
        return text

    def export_video(self):
        from tkinter import messagebox
        import analyzer

        if self._closing:
            return
        if (
            getattr(self, "_export_handle", None) is not None
            or getattr(self, "_segment_export_handle", None) is not None
            or getattr(self, "_analysis_handle", None) is not None
        ):
            return messagebox.showwarning("导出进行中", "请等待当前导出任务结束。")
        if not self.video_path:
            return messagebox.showerror("错误", "请先加载视频")

        video_path = str(self.video_path)
        fps = float(self.fps or 30.0)
        project_generation, timeline_revision = self._task_scope()
        p = self.settings.get_params()
        output_path = str(p["output"])
        quality = int(p["quality"])
        use_gpu = bool(p.get("export_use_gpu", False))
        gpu_encoder = str(p.get("gpu_encoder", ""))
        ffmpeg_path = p.get("ffmpeg_path")
        include_audio = bool(p.get("export_keep_audio", True))
        enforce_media_certification = bool(
            p.get("enforce_media_certification", False)
        )
        if not p["output"]:
            return messagebox.showerror("错误", "请先设置输出路径")
        if enforce_media_certification and (
            self.media_info is None or not self.media_info.complete_for_export
        ):
            return messagebox.showerror(
                "无法导出",
                "当前源文件尚未绑定完整的 FramePtsCertification，"
                "请先完成帧时间戳认证。",
            )

        try:
            timeline_plan = self._build_timeline_plan(
                speedup_1x=p["speedup_1x"],
                speedup_02=p["speedup_02"],
                speedup_02_factor=p["speedup_02_factor"],
            )
            export_preflight = analyzer.inspect_export_plan(
                timeline_plan,
                include_audio=include_audio,
                video_path=video_path,
                ffmpeg_path=ffmpeg_path,
            )
        except Exception as exc:
            return messagebox.showerror("导出准备失败", str(exc))

        if export_preflight.get("export_blocked"):
            reason_labels = {
                "ffmpeg_unavailable": (
                    "没有可用的 FFmpeg。请在设置中填写 FFmpeg 路径，"
                    "或安装 imageio-ffmpeg。"
                ),
            }
            reasons = "\n".join(
                "- " + reason_labels.get(value, value)
                for value in export_preflight.get("export_block_reasons", [])
            )
            return messagebox.showerror(
                "无法导出",
                reasons or "导出依赖不可用。",
            )

        allow_audio_drop = False
        if export_preflight["audio_drop_requires_confirmation"]:
            reason_labels = {
                "too_many_ranges": (
                    f"保留段 {export_preflight['n_ranges']} 个，超过音频安全上限 "
                    f"{export_preflight['audio_limit']}"
                ),
                "audio_probe_inconclusive": "无法确认源片音轨",
            }
            reasons = "\n".join(
                "- " + reason_labels.get(value, value)
                for value in export_preflight.get("audio_drop_reasons", [])
            )
            allow_audio_drop = messagebox.askyesno(
                "确认导出无声视频",
                f"当前无法保证保留音频：\n{reasons}\n\n继续会生成无声视频，是否继续？",
            )
            if not allow_audio_drop:
                self.settings.export_status_var.set(
                    "已取消：当前剪辑无法安全保留音频"
                )
                return

        export_request = ExportRequest.full(
            video_path,
            output_path,
            timeline_plan,
            fps=fps,
            quality=quality,
            use_gpu=use_gpu,
            gpu_encoder=gpu_encoder,
            ffmpeg_path=ffmpeg_path,
            include_audio=include_audio,
            allow_audio_drop=allow_audio_drop,
            media_info=getattr(self, "media_info", None),
            enforce_media_certification=enforce_media_certification,
        )

        self.settings.export_btn.config(state=tk.DISABLED)
        self.settings.export_progress_var.set(0)
        self.settings.export_status_var.set("导出中：正在关闭预览解码器…")
        if not self._pause_preview_for_export():
            self.settings.export_btn.config(state=tk.NORMAL)
            self.settings.export_status_var.set("无法安全关闭预览，已取消导出")
            return

        def work(context):
            def progress(ratio, written, status=None):
                context.checkpoint()
                context.report(
                    (
                        float(ratio),
                        int(written),
                        status or f"写入 {int(float(ratio) * 100)}%",
                    )
                )

            context.checkpoint()
            return MediaExporter().export(
                export_request,
                progress_cb=progress,
                cancel_cb=context.checkpoint,
                source_path_override=video_path,
                commit_cb=lambda source, target: context.commit(
                    os.replace, source, target, final=True
                ),
                preflight=export_preflight,
            )

        def on_progress(value):
            if self._closing:
                return
            ratio, _written, status = value
            self.settings.export_progress_var.set(
                max(0.0, min(100.0, float(ratio) * 100.0))
            )
            self.settings.export_status_var.set(status)

        def on_success(result):
            if self._closing:
                return
            if isinstance(result, ExportResult):
                written = result.written_frames
                total = result.total_frames
                meta = result.metadata
            elif isinstance(result, tuple) and len(result) >= 3:
                written, total, meta = result[0], result[1], result[2] or {}
            else:
                written, total = result[0], result[1]
                meta = {}
            audio_mode = meta.get("audio_mode", "")
            audio_line = {
                "muxed": "音频：已按保留段混音",
                "failed_video_only": "音频：混音失败，已降级为无声视频（视频完整）",
                "skipped_segments": "音频：保留段过多，已按确认导出无声视频",
                "skipped_unavailable": "音频：FFmpeg 不可用，已按确认导出无声视频",
                "skipped_probe": "音频：音轨探测失败，已按确认导出无声视频",
                "disabled": "音频：已按设置关闭",
                "no_stream": "音频：源片无音轨",
            }.get(audio_mode, "音频：未混音或本地导出")
            self.settings.export_status_var.set(f"完成：{written}/{total} 帧")
            self.settings.export_progress_var.set(100)
            messagebox.showinfo(
                "导出完成",
                f"输出：{output_path}\n总帧：{total}，保留：{written}\n{audio_line}",
            )

        def on_error(exc):
            if self._closing:
                return
            self.settings.export_status_var.set(f"失败：{str(exc)[:80]}")
            messagebox.showerror("导出失败", str(exc))

        def on_cancelled():
            if not self._closing:
                self.settings.export_status_var.set("导出已取消，未覆盖原有输出")

        def on_done(_status):
            self._export_handle = None
            if self._closing:
                return
            self.settings.export_btn.config(state=tk.NORMAL)
            self._resume_preview_after_export()

        try:
            self._export_handle = self.task_manager.submit(
                _EXPORT_TASK,
                work,
                on_success=on_success,
                on_error=on_error,
                on_progress=on_progress,
                on_cancelled=on_cancelled,
                on_done=on_done,
                project_generation=project_generation,
                timeline_revision=timeline_revision,
                replace=True,
            )
        except RuntimeError as exc:
            self.settings.export_btn.config(state=tk.NORMAL)
            self.settings.export_status_var.set(str(exc))
            self._resume_preview_after_export()

    @staticmethod
    def _grade_smoothness(late1_pct: float, drop_pct: float, avg_ms: float, rt: float) -> str:
        """粗分档：用 >1 帧迟到%，比 0.25 帧微抖更贴体感。"""
        if late1_pct <= 3 and drop_pct <= 1 and avg_ms <= 25 and (rt == 0 or rt <= 1.05):
            return "优"
        if late1_pct <= 10 and drop_pct <= 5 and avg_ms <= 40 and (rt == 0 or rt <= 1.15):
            return "良"
        if late1_pct <= 25 and drop_pct <= 15:
            return "中"
        return "差"

    def _refresh_perf_label(self, force: bool = False) -> None:
        if not hasattr(self, "lbl_perf"):
            return
        try:
            self.lbl_perf.config(text=self._compose_perf_text())
        except Exception:
            pass

    def _reset_perf_stats_ui(self) -> None:
        if self._io:
            try:
                self._io.reset_perf_stats()
            except Exception:
                pass
        self._perf_last = None
        self._reset_ui_gap_stats()
        if self.is_playing and self._io:
            # 播放中清零：从当前重新记墙钟
            try:
                self._io.begin_perf_segment()
            except Exception:
                pass
        self._refresh_perf_label(force=True)

    def _copy_perf_stats(self) -> None:
        text = self._compose_perf_text()
        detail = text
        if self._io:
            try:
                snap = self._io.snapshot_perf()
                # spikes 单独多列几条，便于归因 188ms 级尖峰
                spikes = snap.get("spikes") or []
                spike_lines = []
                for s in spikes[-12:]:
                    spike_lines.append(
                        f"  f{s.get('frame')} present={s.get('present_ms')}ms "
                        f"lag={s.get('lag_ms')}ms reasons={s.get('reasons')}"
                    )
                detail = text + "\n" + repr({k: v for k, v in snap.items() if k != "spikes"})
                if spike_lines:
                    detail += "\nspikes:\n" + "\n".join(spike_lines)
            except Exception:
                pass
        try:
            root = self.winfo_toplevel()
            root.clipboard_clear()
            root.clipboard_append(detail)
            root.update_idletasks()
            self.lbl_perf.config(foreground="#2e8b57")
            if self._perf_after_id is not None:
                try:
                    self.after_cancel(self._perf_after_id)
                except Exception:
                    pass
            self._perf_after_id = self.after(800, self._restore_perf_label)
        except Exception:
            pass

    def _restore_perf_label(self):
        self._perf_after_id = None
        if not self._closing:
            self.lbl_perf.config(foreground="#888888")


    def _send_stop(self):
        if self._io:
            self._io.stop()

    # ==========================================================
    #  键盘快捷键
    # ==========================================================

    _KEY_PREVIEW_MS = 150

    def _bind_keys(self):
        self._bind_after_id = None
        if self._closing:
            return
        root = self.winfo_toplevel()
        root.bind('<Left>', self._on_key_press_left, add='+')
        root.bind('<Right>', self._on_key_press_right, add='+')
        root.bind('<KeyRelease-Left>', self._on_key_release, add='+')
        root.bind('<KeyRelease-Right>', self._on_key_release, add='+')
        root.bind('<space>', self._on_key_space)
        root.bind('<o>', self._toggle_preview_opt, add='+')
        root.bind('<O>', self._toggle_preview_opt, add='+')
        for cls in ('TButton', 'Button', 'TCheckbutton', 'TRadiobutton', 'TCombobox', 'TNotebook'):
            root.bind_class(cls, '<space>', lambda e: 'break')

    def _on_key_press_left(self, event):
        if self._closing:
            return
        if self._key_held == 'Left': return
        self._key_held = 'Left'
        self._key_hold_fired = False
        self._step_frame(-1, seek=True)
        self._key_after_id = self.after(400, self._start_repeat, 'Left')

    def _on_key_press_right(self, event):
        if self._closing:
            return
        if self._key_held == 'Right': return
        self._key_held = 'Right'
        self._key_hold_fired = False
        self._step_frame(+1, seek=True)
        self._key_after_id = self.after(400, self._start_repeat, 'Right')

    def _on_key_release(self, event):
        if self._closing:
            return
        direction = event.keysym
        if self._key_held != direction: return
        self._key_held = None
        if self._key_after_id:
            self.after_cancel(self._key_after_id)
            self._key_after_id = None
        if self._key_preview_id:
            self.after_cancel(self._key_preview_id)
            self._key_preview_id = None
        if self._key_hold_fired:
            while True:
                try:
                    self._frame_q.get_nowait()
                except Empty:
                    break
            self._do_preview_seek()
        self._key_hold_fired = False

    def _on_key_space(self, event):
        if self._closing:
            return "break"
        focused = self.focus_get()
        if isinstance(focused, (ttk.Entry, tk.Entry, ttk.Combobox)): return
        self.toggle_play();
        return 'break'

    def _start_repeat(self, direction: str):
        if self._closing:
            return
        self._key_hold_fired = True
        self._schedule_preview()
        self._repeat_frame(direction)

    def _repeat_frame(self, direction: str):
        if self._closing or self._key_held != direction: return
        delta = -1 if direction == 'Left' else +1
        self._step_frame(delta, seek=False)
        speed = self.settings.key_repeat_speed_var.get()
        interval = max(16, int(1000 / speed))
        self._key_after_id = self.after(interval, self._repeat_frame, direction)

    def _schedule_preview(self):
        if self._closing:
            return
        self._key_preview_id = self.after(self._KEY_PREVIEW_MS, self._preview_tick)

    def _preview_tick(self):
        if self._closing or not self._key_held: return
        self._do_preview_seek()
        self._key_preview_id = self.after(self._KEY_PREVIEW_MS, self._preview_tick)

    def _do_preview_seek(self):
        if not self._io or self.total_frames <= 0: return
        self._seek_frame(max(0, min(self.current_frame_idx, self.total_frames - 1)))

    def _step_frame(self, delta: int, seek: bool = True):
        if self.total_frames <= 0: return
        # 原始 ±1 步进。删除段/保留段的落点交给引擎（seek_edl 对删除段
        # 向前 snap）；不要替用户"跨段"，否则步进距离不可预期。
        new_idx = max(0, min(self.total_frames - 1, self.current_frame_idx + delta))
        if new_idx == self.current_frame_idx: return
        self.current_frame_idx = new_idx
        self.timeline.current_frame_idx = new_idx
        self.timeline._ensure_pointer_visible()
        self.timeline.update_pointer()
        self._update_labels()
        if seek: self._seek(new_idx, skip_trim=False)

    # ==========================================================
    #  播放控制
    # ==========================================================

    # ==========================================================
    #  倍率标定 V1（10s）/ V2（忽略 on/off 对比）
    # ==========================================================
    def _calib_cancel_timer(self) -> None:
        if self._calib_after_id is not None:
            try:
                self.after_cancel(self._calib_after_id)
            except Exception:
                pass
            self._calib_after_id = None

    def _calib_zone_label(self, frame_idx: int) -> str:
        """Rough zone for expectation display."""
        for seg in self.speed_segments:
            if seg["start"] <= frame_idx <= seg["end"]:
                t = seg["type"]
                name = {
                    FRAME_TYPE_1X: "1x",
                    FRAME_TYPE_2X: "2x",
                    FRAME_TYPE_0_2X: "0.2x",
                }.get(t, "?")
                return f"变速{name}"
        return "普通/其它"

    def _calib_expected_rate(self, frame_idx: int, ignore_biz: bool) -> tuple[float, int, int, str]:
        """Return (M_exp, raw_step, capped_step, note).

        Matches current preview policy: S1 cap only (no S2 clock scale).
        Slow UI speeds stretch the display clock, while speeds >=1x advance
        multiple source frames per display tick.
        """
        speed_str = self.preview_speed_var.get().rstrip("x")
        try:
            ui_speed = float(speed_str)
        except ValueError:
            ui_speed = 1.0
        if ui_speed >= 1.0:
            preview_step = max(1, int(ui_speed))
            clock_rate = 1.0  # >=1x uses frame skip, not a faster clock
        else:
            preview_step = 1
            clock_rate = max(ui_speed, 0.01)

        p = self.settings.get_params()
        if ignore_biz:
            biz = 1
        else:
            biz = 1
            for seg in self.speed_segments:
                if seg["start"] <= frame_idx <= seg["end"]:
                    t = seg["type"]
                    if t == FRAME_TYPE_1X and p.get("speedup_1x"):
                        biz = 2
                    elif t == FRAME_TYPE_0_2X and p.get("speedup_02"):
                        biz = max(2, int(p.get("speedup_02_factor", 10) or 10))
                    break
        raw = max(1, preview_step * biz)
        cap = 3
        capped = min(raw, cap)
        # wall-clock media advance rate under S1-only policy
        m_exp = clock_rate * float(capped)
        note = f"raw_step={raw} cap={cap} → 期望按步进{capped}"
        if raw > capped:
            note += f"（业务理想约{raw}x，预览封顶后约{capped}x）"
        return m_exp, raw, capped, note

    def _playback_rate_measurement(
        self,
        started_at: float,
        start_frame: int,
    ) -> tuple[float, int, int, str, int] | None:
        """Measure media advance using IO play steps, excluding trim jumps."""
        current_frame = int(self.current_frame_idx)
        wall_s = max(1e-6, time.monotonic() - float(started_at))
        play_frames: int | None = None
        trim_frames = 0
        source = "ui-frames"
        if self._io:
            try:
                snapshot = self._io.snapshot_perf()
                io_wall_s = float(snapshot.get("wall_s") or 0.0)
                if io_wall_s > 0.0:
                    wall_s = max(io_wall_s, 1e-6)
                if snapshot.get("rate_play_frames") is not None:
                    play_frames = int(snapshot.get("rate_play_frames") or 0)
                    source = "play-steps"
                trim_frames = int(snapshot.get("rate_trim_frames") or 0)
            except Exception:
                pass

        if play_frames is None:
            play_frames = current_frame - int(start_frame)
            if play_frames < 0:
                return None
        return wall_s, play_frames, trim_frames, source, current_frame

    def _calib_set_label(self, text: str, color: str = "#888888") -> None:
        self._calib_last_line = text
        if hasattr(self, "lbl_calib"):
            try:
                self.lbl_calib.config(text=text, foreground=color)
            except Exception:
                pass

    def _calib_copy(self) -> None:
        text = self._calib_last_line or ""
        if self._calib_pair_lines:
            text = "\n".join(self._calib_pair_lines)
        if not text:
            text = "尚无标定结果"
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self._calib_set_label(text + "  [已复制]", "#2e8b57")
        except Exception:
            pass

    def _calib_stop_play_for_measure(self) -> None:
        if self.is_playing:
            self._stop_playback_ui(from_user=True)

    def _calib_begin_run(self, mode: str, ignore_biz: bool, anchor_frame: int | None = None) -> None:
        if self._closing:
            return
        if not self._io or self.total_frames <= 0:
            self._calib_set_label("倍率标定: 请先加载视频", "#cc4444")
            return
        self._calib_cancel_timer()
        self._calib_stop_play_for_measure()

        if anchor_frame is not None:
            self.current_frame_idx = int(max(0, min(anchor_frame, self.total_frames - 1)))
            self.timeline.current_frame_idx = self.current_frame_idx
            try:
                self._seek(self.current_frame_idx, skip_trim=False)
            except Exception:
                pass

        # apply ignore flag for this run
        self.preview_ignore_speedup_var.set(bool(ignore_biz))
        self._calib_ignore = bool(ignore_biz)
        self._calib_mode = mode
        self._calib_active = True
        self._calib_f0 = int(self.current_frame_idx)
        self._calib_t0 = time.monotonic()

        zone = self._calib_zone_label(self._calib_f0)
        m_exp, raw, capped, note = self._calib_expected_rate(self._calib_f0, ignore_biz)
        tag = "忽略ON" if ignore_biz else "忽略OFF"
        self._calib_set_label(
            f"标定中[{tag}] 10s… f0={self._calib_f0} 区={zone} 期望≈{m_exp:.2f}x ({note})",
            "#daa520",
        )

        self.is_playing = True
        try:
            self.btn_play.config(text="⏸ 暂停")
        except Exception:
            pass
        self._reset_ui_gap_stats()
        if not self._send_play(self.current_frame_idx):
            self._calib_active = False
            return
        self._calib_after_id = self.after(10000, self._calib_finish_run)

    def _calib_finish_run(self) -> None:
        self._calib_after_id = None
        if self._closing:
            return
        if not self._calib_active or self._calib_t0 is None:
            return
        f0 = int(self._calib_f0)
        measurement = self._playback_rate_measurement(self._calib_t0, f0)
        if measurement is None:
            self._calib_active = False
            self._calib_stop_play_for_measure()
            self._calib_set_label(
                "倍率标定: 本次含回退/seek，结果已忽略",
                "#cc4444",
            )
            return
        tw, play_frames, trim_frames, source, f1 = measurement
        fps = float(self.fps) if self.fps else 30.0
        ts = play_frames / max(fps, 1e-6)
        m = ts / tw
        ignore_biz = bool(self._calib_ignore)
        m_exp, raw, capped, note = self._calib_expected_rate(f0, ignore_biz)
        err = (m - m_exp) / m_exp if abs(m_exp) > 1e-6 else 0.0
        ok = abs(err) <= 0.10 if raw <= capped else abs(err) <= 0.15
        tag = "忽略ON" if ignore_biz else "忽略OFF"
        zone = self._calib_zone_label(f0)
        verdict = "达标" if ok else "未达标"
        line = (
            f"标定[{tag}] {verdict} | 墙钟{tw:.2f}s 片源{ts:.2f}s | "
            f"实测{m:.3f}x 期望{m_exp:.3f}x 误差{err*100:+.1f}% | "
            f"计{play_frames}帧[{source}] 指针{f0}→{f1} @ {fps:.3g}fps | "
            f"区={zone} | {note}"
            + (
                f" | 跳过裁剪≈{trim_frames}帧(不计入倍率)"
                if trim_frames > 0
                else ""
            )
        )
        self._calib_active = False
        self._calib_stop_play_for_measure()

        color = "#2e8b57" if ok else "#cc4444"
        if self._calib_mode == "single":
            self._calib_pair_lines = [line]
            self._calib_set_label(line, color)
            return

        if self._calib_mode == "pair_a":
            self._calib_pair_lines = [line]
            self._calib_set_label(line + " → 接着测忽略OFF…", "#daa520")
            # V2 second leg: ignore OFF, same anchor
            self._calib_after_id = self.after(
                400,
                lambda: self._calib_begin_run(
                    "pair_b", ignore_biz=False, anchor_frame=self._calib_pair_anchor
                ),
            )
            return

        if self._calib_mode == "pair_b":
            self._calib_pair_lines.append(line)
            # summary
            summary = "对比标定完成:\n" + "\n".join(self._calib_pair_lines)
            self._calib_last_line = summary
            self._calib_set_label(
                " | ".join(self._calib_pair_lines),
                "#2e8b57" if all("达标" in x for x in self._calib_pair_lines) else "#cc4444",
            )
            return


    def _auto_rate_mark_start(self) -> None:
        """Mark t0/f0 when user starts normal playback (not V1/V2 timed calib)."""
        if self._calib_active:
            return
        if not self._io or self.total_frames <= 0:
            self._auto_rate_t0 = None
            return
        self._auto_rate_t0 = time.monotonic()
        self._auto_rate_f0 = int(self.current_frame_idx)
        self._auto_rate_ignore = bool(self.preview_ignore_speedup_var.get())

    def _auto_rate_clear(self) -> None:
        self._auto_rate_t0 = None

    def _auto_rate_on_stop(self) -> None:
        """Stop auto rate: use play-step frame count (excludes skip_trim jumps)."""
        if self._calib_active:
            return
        if self._auto_rate_t0 is None:
            return
        t0 = float(self._auto_rate_t0)
        f0 = int(self._auto_rate_f0)
        ignore_biz = bool(self._auto_rate_ignore)
        self._auto_rate_t0 = None

        min_s = float(getattr(self, "_AUTO_RATE_MIN_S", 3.0) or 3.0)
        measurement = self._playback_rate_measurement(t0, f0)
        if measurement is None:
            self._calib_set_label(
                "倍率: 本次含回退/seek，已忽略（请向前连续播一段）",
                "#cc4444",
            )
            return
        wall_s, play_frames, trim_frames, src, f1 = measurement

        if wall_s < min_s:
            self._calib_set_label(
                f"倍率: 样本过短 {wall_s:.1f}s（请连续播放≥{min_s:.0f}s 再停）",
                "#888888",
            )
            return

        fps = float(self.fps) if self.fps else 30.0
        ts = play_frames / max(fps, 1e-6)
        meas = ts / wall_s
        m_exp, raw, capped, note = self._calib_expected_rate(f0, ignore_biz)
        err = (meas - m_exp) / m_exp if abs(m_exp) > 1e-6 else 0.0
        ok = abs(err) <= 0.10 if raw <= capped else abs(err) <= 0.15
        tag = "忽略ON" if ignore_biz else "忽略OFF"
        zone = self._calib_zone_label(f0)
        verdict = "达标" if ok else "未达标"
        trim_note = ""
        if trim_frames > 0:
            trim_note = f" | 跳过裁剪≈{trim_frames}帧(不计入倍率)"
        line = (
            f"自动[{tag}] {verdict} | 墙钟{wall_s:.2f}s 播放推进{ts:.2f}s | "
            f"实测{meas:.3f}x 期望{m_exp:.3f}x 误差{err*100:+.1f}% | "
            f"计{play_frames}帧[{src}] 指针{f0}→{f1} @ {fps:.3g}fps | "
            f"区={zone} | {note}{trim_note}"
        )
        self._calib_pair_lines = [line]
        self._calib_set_label(line, "#2e8b57" if ok else "#cc4444")


    def _calib_start_single(self) -> None:
        """V1: 10s calibration with current ignore checkbox."""
        if self._calib_active:
            self._calib_set_label("标定进行中…", "#daa520")
            return
        self._calib_pair_lines = []
        ignore = bool(self.preview_ignore_speedup_var.get())
        self._calib_begin_run("single", ignore_biz=ignore, anchor_frame=None)

    def _calib_start_pair(self) -> None:
        """V2: same start frame — ignore ON 10s, then ignore OFF 10s."""
        if self._calib_active:
            self._calib_set_label("标定进行中…", "#daa520")
            return
        if not self._io or self.total_frames <= 0:
            self._calib_set_label("倍率标定: 请先加载视频", "#cc4444")
            return
        self._calib_pair_lines = []
        self._calib_pair_anchor = int(self.current_frame_idx)
        self._calib_set_label(
            f"对比标定: 锚点帧 {self._calib_pair_anchor}，先忽略ON 10s…",
            "#daa520",
        )
        self._calib_begin_run(
            "pair_a", ignore_biz=True, anchor_frame=self._calib_pair_anchor
        )


    def toggle_play(self):
        if self._closing:
            return
        if self.is_playing:
            self._stop_playback_ui(from_user=True)
        else:
            self.is_playing = True
            try:
                self.btn_play.config(text="⏸ 暂停")
            except Exception:
                pass
            self._reset_ui_gap_stats()
            self._auto_rate_mark_start()
            self._send_play(self.current_frame_idx)

    def _on_tl_seek(self, frame_idx: int):
        self._is_dragging = True
        self._seek(frame_idx, skip_trim=False)

    def _on_tl_drag_end(self):
        self._is_dragging = False
        while True:
            try:
                self._frame_q.get_nowait()
            except Empty:
                break
        if self.is_playing: self._send_play(self.current_frame_idx)

    def _on_timeline_pause_select(self, seg_id: int):
        for seg in self.pause_segments:
            if seg['id'] == seg_id:
                self.settings.set_selected_pause(seg_id, seg.get('mode', 'auto'))
                break

    def _preview_speed_factor(self) -> float:
        """当前预览倍速（解析失败按 1x）；用于 FPS 读数的上限钳制。"""
        var = getattr(self, "preview_speed_var", None)
        try:
            return max(0.01, float(str(var.get()).rstrip("xX")))
        except Exception:
            return 1.0

    def _note_fps_sample(self, time_pos, drops) -> None:
        """喂采样并节流刷新右上角播放帧率 OSD。

        口径：媒体时间推进速率 × 源帧率 = 实际内容帧率。用 time-pos（EDL
        里也连续、不会因跳过删除段而大跳）而不是源帧号，所以不会被 EDL
        跳变污染成 129/175 那种虚高值。上限钳到 源帧率×当前倍速（mpv 的
        2x/4x 是 playback_rate 原生变速，内容帧率随之放大到 ~120/~240），
        超出上限必是测量噪声。暂停 / 寻址瞬间清窗重新累积。
        """
        now = time.monotonic()
        if isinstance(time_pos, (int, float)) and self.is_playing:
            self._fps_samples.append((now, float(time_pos), drops if isinstance(drops, int) else None))
        else:
            if self._fps_samples:
                self._fps_samples.clear()
        shower = getattr(self._io, "show_osd_corner_text", None)
        if not callable(shower) or not bool(self.show_frame_osd_var.get()):
            return
        if not self.is_playing:
            return
        if self._osd_tick % 15 != 0:
            return
        text = None
        samples = self._fps_samples
        src_fps = float(getattr(self._io, "fps", 0.0) or 0.0)
        if len(samples) >= 8 and src_fps > 0:
            t0, p0, d0 = samples[0]
            t1, p1, d1 = samples[-1]
            dt = t1 - t0
            if dt >= 0.5:
                # 求逆序对：寻址/模式切换后 time-pos 可能倒退，那段窗口不可信
                ordered = all(
                    samples[i][1] <= samples[i + 1][1]
                    for i in range(len(samples) - 1)
                )
                if ordered:
                    media_rate = (p1 - p0) / dt  # 媒体秒 / 墙钟秒
                    fps = media_rate * src_fps
                    if d0 is not None and d1 is not None:
                        fps -= (d1 - d0) / dt  # 扣掉丢帧速率
                    fps = max(0.0, min(fps, src_fps * self._preview_speed_factor()))
                    text = f"{fps:.1f} FPS"
        try:
            shower(text or "… FPS")
        except Exception:
            pass

    def _maybe_show_frame_osd(self, frame: int) -> None:
        """原生渲染时用持久 overlay 在左上角显示当前源帧号；暂停也常驻。"""
        shower = getattr(self._io, "show_osd_topleft_text", None)
        if not callable(shower) or not bool(self.show_frame_osd_var.get()):
            return
        # 持久 overlay 不会自己消失；_osd_pending 让寻址/暂停时也立即刷新一次
        self._osd_tick += 1
        if not self._osd_pending and self._osd_tick % 15 != 0:
            return
        self._osd_pending = False
        try:
            shower(f"帧 {frame:,} / {max(0, self.total_frames - 1):,}")
        except Exception:
            pass

    def _raise_native_cover(self) -> None:
        """mpv 首帧前盖黑底，避免白色原生子窗口露脸；带兜底超时。"""
        if self._native_cover is not None:
            return
        self._native_cover = tk.Frame(self.video_surface, bg="black")
        self._native_cover.pack(fill=tk.BOTH, expand=True)
        self._native_cover.lift()
        self._schedule_drop_native_cover(delay_ms=3000)

    def _schedule_drop_native_cover(self, delay_ms: int) -> None:
        if self._native_cover is None or self._native_cover_after_id is not None:
            return
        self._native_cover_after_id = self.after(delay_ms, self._drop_native_cover)

    def _drop_native_cover(self) -> None:
        self._native_cover_after_id = None
        cover = self._native_cover
        self._native_cover = None
        if cover is not None:
            try:
                cover.destroy()
            except Exception:
                pass

    def _reclaim_focus_from_native_child(self) -> None:
        """把被原生 mpv 子窗口夺走的键盘焦点拉回 Tk。

        点击画面后 Windows 焦点落在 mpv 的视频子窗口上：此后 ←/→/空格 全部
        进不了 Tk 的事件循环，快捷键看似失灵（与右键菜单被吞同源）。
        GetFocus 只报本线程的窗口，对外线程子窗口恒为 0，所以用
        GetGUIThreadInfo 拿全局焦点窗口：本窗口在前台、全局焦点落在本窗口
        内部、但本线程 GetFocus 为空 = 焦点被外线程子窗口持有。连续两拍
        成立才 focus_set，避免与文件对话框等瞬时状态打架。
        ARKNIGHT_FOCUS_WATCHDOG=0 可关闭。
        """
        if self._closing or os.environ.get("ARKNIGHT_FOCUS_WATCHDOG", "1") == "0":
            self._focus_reclaim_streak = 0
            return
        if not bool(getattr(self._io, "native_rendering", False)):
            self._focus_reclaim_streak = 0
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            foreground = user32.GetForegroundWindow()
            top_hwnd = int(self.winfo_toplevel().wm_frame(), 16)
            ga_root = 2
            if (
                not foreground
                or user32.GetAncestor(foreground, ga_root)
                != user32.GetAncestor(top_hwnd, ga_root)
            ):
                self._focus_reclaim_streak = 0
                return
            if user32.GetFocus():
                # 焦点在本线程的 Tk 控件上（输入框等），不动
                self._focus_reclaim_streak = 0
                return

            class _GTI(ctypes.Structure):
                _fields_ = [
                    ("cbSize", ctypes.c_ulong),
                    ("flags", ctypes.c_ulong),
                    ("hwndActive", ctypes.c_void_p),
                    ("hwndFocus", ctypes.c_void_p),
                    ("hwndCapture", ctypes.c_void_p),
                    ("hwndMenuOwner", ctypes.c_void_p),
                    ("hwndMoveSize", ctypes.c_void_p),
                    ("hwndCaret", ctypes.c_void_p),
                    ("rcCaret", ctypes.c_long * 4),
                ]

            gti = _GTI()
            gti.cbSize = ctypes.sizeof(_GTI)
            if not user32.GetGUIThreadInfo(0, ctypes.byref(gti)):
                self._focus_reclaim_streak = 0
                return
            focus_hwnd = gti.hwndFocus or gti.hwndActive
            if not focus_hwnd:
                self._focus_reclaim_streak = 0
                return
            # 全局焦点不在本窗口内（别的应用/桌面），与我们无关
            if user32.GetAncestor(focus_hwnd, ga_root) != user32.GetAncestor(top_hwnd, ga_root):
                self._focus_reclaim_streak = 0
                return
            self._focus_reclaim_streak += 1
            if self._focus_reclaim_streak < 2:
                return
            self._focus_reclaim_streak = 0
            self.focus_set()
        except Exception:
            self._focus_reclaim_streak = 0

    def _apply_native_perf(self, perf: dict) -> None:
        """原生引擎渲染分支：采信引擎帧号、刷新帧号 OSD、喂 FPS 采样。

        暂停中用户刚用 ←/→ 或拖时间轴发了寻址时（保护窗口内），不用引擎
        回读的帧号回写 UI：mpv 寻址是异步的，旧的 time-pos 事件先把
        source_frame 拉回寻址前的值，UI 跟着回退，表现为步进失灵。
        播放中维持引擎为权威源不动。
        """
        native_frame = perf.get("source_frame")
        guard_until = getattr(self, "_paused_seek_guard_until", 0.0)
        paused_guard = not self.is_playing and time.monotonic() < guard_until
        if isinstance(native_frame, int) and 0 <= native_frame < max(1, self.total_frames):
            if paused_guard:
                # 保护窗内：寻址还没落地，旧 time-pos 回读不得回写 UI。
                pass
            elif not self.is_playing:
                # 暂停中：UI 帧号是权威（用户刚步进/拖动过）。引擎回读与
                # UI 一致是寻址落地；不一致是 VFR/EDL 的 ±1 映射偏差，
                # 采信只会把 UI 从用户的目标弹回旧值，所以一律不回写。
                pass
            else:
                self.current_frame_idx = native_frame
                self.timeline.current_frame_idx = native_frame
            self._maybe_show_frame_osd(self.current_frame_idx)
        self._note_fps_sample(perf.get("time_pos"), perf.get("mpv_frame_drops"))

    def _render_loop(self):
        if self._closing:
            self._render_after_id = None
            return
        self._reclaim_focus_from_native_child()
        # 暂停静帧覆盖层生命周期：播放上升沿撤覆盖层，下降沿（含片尾
        # 自动停播）贴当前帧静帧
        if bool(getattr(self._io, "native_rendering", False)):
            if self.is_playing and not self._was_playing:
                self._drop_still_overlay()
            elif not self.is_playing and self._was_playing:
                self._show_paused_still(self.current_frame_idx)
            self._was_playing = self.is_playing
            self._drain_still_pending()
            # 追帧：节流跳过的寻址在这里补齐——暂停态覆盖层始终收敛到当前帧号
            if (not self.is_playing and not self._still_decoding
                    and self._still_pending is None
                    and getattr(self, "_still_frame_idx", None) != self.current_frame_idx):
                self._show_paused_still(self.current_frame_idx)
        if self._io is not None:
            events = ()
            try:
                events = self._io.poll_events()
            except Exception:
                pass
            if (
                self._native_cover is not None
                and any(event.get("event") == "file-loaded" for event in events)
            ):
                # mpv 已经画好首帧附近的内容，稍等一拍再撤黑底遮盖，
                # 避免启动瞬间露出未渲染的原生子窗口（白屏观感）。
                self._schedule_drop_native_cover(delay_ms=150)
            if bool(getattr(self._io, "native_rendering", False)) and not self._is_dragging:
                try:
                    self._apply_native_perf(self._io.snapshot_perf())
                except Exception:
                    pass
        # 只显示队列里最新一帧，避免积压时「补放旧帧」造成拖影/顿挫
        latest = None
        drained = 0
        try:
            while True:
                latest = self._frame_q.get_nowait()
                drained += 1
        except Empty:
            pass
        if latest is not None:
            idx, rgb = latest
            if drained > 1:
                # 主线程一次丢掉的中间帧 ≈ UI 侧积压
                pass
            if not self._key_hold_fired and not self._is_dragging:
                self.current_frame_idx = idx
                self.timeline.current_frame_idx = idx
                self.timeline._ensure_pointer_visible()
            self._note_ui_display()
            self._display_rgb(rgb)
        self.timeline.update_pointer()
        self._perf_ui_tick += 1
        # ~4 次/秒：刷新流畅度；并检测片尾自动停播（避免时长一直涨、按钮仍显示暂停）
        if self._perf_ui_tick % 15 == 0:
            self._sync_eof_stop()
            self._refresh_perf_label()
        self._render_after_id = self.after(16, self._render_loop)

    def _display_rgb(self, rgb: np.ndarray):
        img = PIL.Image.fromarray(rgb)
        cw, ch = self._canvas_wh()
        size = img.size  # (w, h)

        # T1-3：尺寸不变时就地 paste 像素，省掉每帧 PhotoImage 分配 +
        # 旧对象析构触发的 Tcl image delete（UI 顿挫与 GC 尖峰的主要来源）。
        reused = False
        if self._photo is not None and size == self._photo_size:
            try:
                self._photo.paste(img)
                reused = True
            except Exception:
                # paste 失败（Tcl image 已失效等）→ 回退整体重建
                self._photo = None
                self._photo_size = (0, 0)

        if not reused:
            self._photo = PIL.ImageTk.PhotoImage(image=img)
            self._photo_size = size
            if self._canvas_img_id is None:
                self.video_canvas.delete("all")
                self._canvas_img_id = self.video_canvas.create_image(
                    cw // 2, ch // 2, image=self._photo)
            else:
                self.video_canvas.itemconfig(self._canvas_img_id, image=self._photo)

        if self._canvas_img_id is not None:
            self.video_canvas.coords(self._canvas_img_id, cw // 2, ch // 2)
        self._update_labels()

    # ==========================================================
    #  标签更新（增加显示差异值功能）
    # ==========================================================
    def _update_labels(self):
        cur = self.current_frame_idx
        cur_s = cur / self.fps if self.fps else 0
        tot_s = self.total_frames / self.fps if self.fps else 0
        self.lbl_time.config(text=f"{self._fmt(cur_s)} / {self._fmt(tot_s)}")

        info = "普通区域"
        for seg in self.pause_segments:
            if seg['start'] <= cur <= seg['end']:
                mode_str = {'all': '全删', 'keep': '全保留', 'auto': '按设置裁剪'}.get(seg.get('mode', 'auto'), '')
                bd_diff = seg.get('boundary_diff', 0.0)
                # 底部界面展示，供用户参考去调参
                info = f"暂停 | ID: {seg['id']} | 模式: {mode_str} | 边界差异: {bd_diff:.1f}"
                break
        else:
            p = self.settings.get_params()
            ignore_biz = bool(self.preview_ignore_speedup_var.get())
            for seg in self.speed_segments:
                if seg['start'] <= cur <= seg['end']:
                    t = seg['type']
                    name = {FRAME_TYPE_1X: '1x', FRAME_TYPE_2X: '2x', FRAME_TYPE_0_2X: '0.2x'}.get(t, '?')
                    eff = 1
                    if not ignore_biz:
                        if t == FRAME_TYPE_1X and p.get('speedup_1x'):
                            eff = 2
                        if t == FRAME_TYPE_0_2X and p.get('speedup_02'):
                            eff = p.get('speedup_02_factor', 10)
                    speed_str = self.preview_speed_var.get().rstrip('x')
                    try:
                        pspeed = float(speed_str)
                    except ValueError:
                        pspeed = 1.0
                    total_eff = eff * pspeed
                    extra = ""
                    if total_eff != 1:
                        extra = f"（预览 {total_eff:g}x）"
                    elif ignore_biz and t in (FRAME_TYPE_1X, FRAME_TYPE_0_2X):
                        extra = "（已忽略业务加速）"
                    info = f"变速 {name}" + extra
                    break
        self.lbl_info.config(text=info)

    @staticmethod
    def _fmt(sec: float) -> str:
        m, s = divmod(int(sec), 60);
        return f"{m:02d}:{s:02d}"

    # ==========================================================
    #  单段与批量暂停模式控制（带有掩码动态重算）
    # ==========================================================
    def apply_pause_mode(self, mode: str):
        import analyzer
        p = self.settings.get_params()
        boundary_thresh = p['compare'].get('boundary_thresh', 5.0)
        motion_thresh = p['compare'].get('motion_thresh', 2.0)
        still_time = p['compare'].get('still_time_thresh', 0.1)
        still_frames = max(2, int(self.fps * still_time))

        commands = []
        for seg in self.pause_segments:
            seg_id = int(seg['id'])
            if mode == 'auto':
                # 只要点击了智能裁剪，就利用保存好的 diffs 取出最新参数重算一次内部掩码
                if self.diffs_array is not None:
                    new_mask, _ = analyzer._analyze_pause_mask(
                        seg['start'], seg['end'], self.diffs_array, still_frames, motion_thresh)
                    old_mask = np.asarray(seg.get('local_del_mask'), dtype=np.uint8)
                    commands.extend(
                        self._mask_delta_commands(seg_id, old_mask, new_mask)
                    )

                if seg.get('boundary_diff', 0.0) < boundary_thresh:
                    next_mode = 'all'
                else:
                    next_mode = 'auto'
            else:
                next_mode = mode
            if seg.get('mode', 'auto') != next_mode:
                commands.append(SetPauseMode(seg_id, next_mode))

        self._apply_project_commands(commands)

        if self.settings.selected_pause_id is not None:
            for seg in self.pause_segments:
                if seg['id'] == self.settings.selected_pause_id:
                    self.settings.set_selected_pause(self.settings.selected_pause_id, seg['mode'])
                    break

        if self.is_playing: self._send_play(self.current_frame_idx)

    def set_single_pause_mode(self, seg_id: int, mode: str):
        import analyzer
        p = self.settings.get_params()
        motion_thresh = p['compare'].get('motion_thresh', 2.0)
        still_time = p['compare'].get('still_time_thresh', 0.1)
        still_frames = max(2, int(self.fps * still_time))

        commands = []
        for seg in self.pause_segments:
            if seg['id'] == seg_id:
                seg_id = int(seg_id)
                if mode == 'auto':
                    # 针对单段重算内部裁剪掩码（如果用户调了灵敏度参数）
                    if self.diffs_array is not None:
                        new_mask, _ = analyzer._analyze_pause_mask(
                            seg['start'], seg['end'], self.diffs_array, still_frames, motion_thresh)
                        old_mask = np.asarray(seg.get('local_del_mask'), dtype=np.uint8)
                        commands.extend(
                            self._mask_delta_commands(seg_id, old_mask, new_mask)
                        )

                if seg.get('mode', 'auto') != mode:
                    commands.append(SetPauseMode(seg_id, mode))

                self._apply_project_commands(commands)
                self.settings.set_selected_pause(seg_id, mode)
                if self.is_playing:
                    self._send_play(self.current_frame_idx)
                break

    @staticmethod
    def _mask_delta_commands(seg_id: int, old_mask, new_mask) -> list:
        """Encode changed mask runs as immutable half-open edit commands."""
        old = np.asarray(old_mask, dtype=np.uint8).reshape(-1)
        new = np.asarray(new_mask, dtype=np.uint8).reshape(-1)
        if old.shape != new.shape:
            raise ValueError("pause mask length changed during edit")
        commands = []
        start = None
        value = None
        for index, (before, after) in enumerate(zip(old.tolist(), new.tolist())):
            if before == after:
                if start is not None:
                    commands.append(SetPauseMaskRun(seg_id, start, index, value))
                    start = None
                continue
            if start is None:
                start, value = index, int(after)
            elif int(after) != value:
                commands.append(SetPauseMaskRun(seg_id, start, index, value))
                start, value = index, int(after)
        if start is not None:
            commands.append(SetPauseMaskRun(seg_id, start, len(new), value))
        return commands

    # ==========================================================
    #  模板分析
    # ==========================================================
    def _start_analysis(self):
        """Submit analysis from an owner-thread snapshot."""
        if self._closing or not self.video_path:
            return
        from tkinter import messagebox
        import analyzer

        if (
            getattr(self, "_export_handle", None) is not None
            or getattr(self, "_segment_export_handle", None) is not None
        ):
            return messagebox.showwarning(
                "任务进行中", "导出任务运行时不能同时开始分析。"
            )

        p = self.settings.get_params()
        video_path = str(self.video_path)
        fps = float(self.fps or 30.0)
        project_generation, timeline_revision = self._task_scope()
        decode_backend = p.get("decode_backend", "opencv")
        ffmpeg_path = p.get("ffmpeg_path")
        ffmpeg_path = p.get("ffmpeg_path")
        backend_note = ""
        try:
            backend_key = analyzer.normalize_decode_backend(decode_backend)
        except Exception as norm_exc:
            backend_key = "opencv"
            backend_note = f"(invalid setting; using OpenCV: {norm_exc})"
        backend_label = {
            "opencv": "OpenCV",
            "ffmpeg_sw_passthrough": "FFmpeg A_PT",
        }.get(backend_key, backend_key) + backend_note

        snapshot = {
            "video_path": video_path,
            "fps": fps,
            "proc_res": tuple(p["proc_res"]),
            "thresholds": dict(p["thresholds"]),
            "compare": dict(p["compare"]),
            "batch": p["batch"],
            "threads": p["threads"],
            "backend_key": backend_key,
            "backend_label": backend_label,
            "backend_note": backend_note,
            "ffmpeg_path": ffmpeg_path,
        }

        self.btn_analyze.config(state=tk.DISABLED, text="Analyzing...")

        def on_progress(value):
            if self._closing:
                return
            try:
                label, ratio = value
                percent = int(float(ratio) * 100)
            except (TypeError, ValueError):
                return
            self.btn_analyze.config(text=f"{label.split('(')[0].strip()} {percent}%")

        def on_success(result):
            if self._closing:
                return
            states, diffs, pauses, speeds, label, used_context, missing_templates = result
            if missing_templates:
                messagebox.showwarning(
                    "Templates missing",
                    "No usable templates were found; all frames will be treated as normal.",
                )
            self._finish_analysis(
                states,
                diffs,
                pauses,
                speeds,
                backend_label=label,
                context_used=used_context,
            )

        def on_error(exc):
            if self._closing:
                return
            self.btn_analyze.config(state=tk.NORMAL, text="Automatic analysis")
            messagebox.showerror(
                "Analysis failed",
                f"Backend {backend_label} failed for {video_path}:\n"
                f"{type(exc).__name__}: {exc}\n\n"
                "Try OpenCV (the default decoder) and run it again.",
            )

        def on_cancelled():
            if not self._closing:
                self.btn_analyze.config(state=tk.NORMAL, text="Automatic analysis")

        def on_done(_status):
            self._analysis_handle = None

        try:
            self._analysis_handle = self.task_manager.submit(
                _ANALYSIS_TASK,
                lambda context: _run_analysis_task(context, snapshot),
                on_success=on_success,
                on_error=on_error,
                on_progress=on_progress,
                on_cancelled=on_cancelled,
                on_done=on_done,
                project_generation=project_generation,
                timeline_revision=timeline_revision,
                replace=True,
            )
        except RuntimeError:
            self.btn_analyze.config(state=tk.NORMAL, text="Automatic analysis")
            raise

    def _finish_analysis(
        self,
        states,
        diffs,
        pauses,
        speeds,
        backend_label: str = "OpenCV",
        context_used: bool = False,
    ):
        from tkinter import messagebox
        clips = self._build_clip_segments(pauses, self.total_frames)
        with self.task_manager.scope_transition():
            snapshot = self.project_state.replace_timeline(pauses, speeds, clips)
            self.task_manager.invalidate_scope(
                project_generation=snapshot.project_generation,
                timeline_revision=snapshot.timeline_revision,
            )
        self._publish_project_snapshot(snapshot)
        self.states_array = states
        self.diffs_array = diffs  # 储存 diffs

        self.timeline.selected_pause_id = None
        self.settings.set_selected_pause(None, "")

        self.timeline.mark_dirty()
        self.btn_analyze.config(state=tk.NORMAL, text="自动模板分析")
        self.timeline.redraw()
        boundary_note = (
            "边界: 第一遍上下文（已跳过二次扫片）"
            if context_used
            else "边界: 回退二次扫片或无暂停"
        )
        messagebox.showinfo(
            "分析完成",
            f"解码后端: {backend_label}\n"
            f"{boundary_note}\n"
            f"识别到 {len(pauses)} 处暂停，{len(speeds)} 个变速区间。",
        )

    @staticmethod
    def _build_clip_segments(pauses: list, total_frames: int) -> list:
        if total_frames <= 0: return []
        occupied = sorted([(seg['start'], seg['end']) for seg in pauses])
        clips = [];
        clip_id = 0;
        prev_end = -1
        for ps, pe in occupied:
            gap_start, gap_end = prev_end + 1, ps - 1
            if gap_end >= gap_start:
                clips.append(
                    {'id': clip_id, 'start': gap_start, 'end': gap_end, 'keep_in': gap_start, 'keep_out': gap_end})
                clip_id += 1
            prev_end = pe
        tail_start, tail_end = prev_end + 1, total_frames - 1
        if tail_end >= tail_start:
            clips.append(
                {'id': clip_id, 'start': tail_start, 'end': tail_end, 'keep_in': tail_start, 'keep_out': tail_end})
        return clips

    # ==========================================================
    #  导出
    # ==========================================================
    def _pause_preview_for_export(self) -> bool:
        """导出前停预览并尽量释放 VideoCapture，避免与导出双开同一文件。"""
        try:
            if self.is_playing:
                self._stop_playback_ui(from_user=True)
            elif self._io:
                self._send_stop()
        except Exception:
            pass
        # 释放预览 IO 占用的 cap；导完后按当前路径重建
        old_io = self._io
        self._io = None
        if old_io is not None and not old_io.close(timeout=1.0):
            self._io = old_io
            return False
        while True:
            try:
                self._frame_q.get_nowait()
            except Empty:
                break
        return True

    def _resume_preview_after_export(self) -> None:
        """导出结束后重建预览 IO，停在导出前附近的帧。"""
        if self._closing or not self.video_path:
            return
        try:
            if self._io and self._io.is_alive():
                return
            self._io = self._make_preview_engine(self.video_path)
            # Rebind the immutable certification to the replacement preview
            # engine; otherwise a resumed mpv instance would silently lose
            # its exact source-time seek table after export.
            if self.media_info is not None:
                try:
                    self._io.bind_media_info(self.media_info)
                except Exception as exc:
                    self.frame_pts_error = exc
            # 容器帧数可能与分析长度略有出入，以 IO 为准更新
            if self._io.total:
                self.total_frames = int(self._io.total)
                self.timeline.total_frames = self.total_frames
            if self._io.fps:
                self.fps = float(self._io.fps)
                self.timeline.fps = self.fps
            # The reopened container may report a different frame count; in
            # either case discard a plan derived from the pre-export IO.
            self._invalidate_derived_timeline_plan()
            idx = int(max(0, min(self.current_frame_idx, max(0, self.total_frames - 1))))
            self._seek(idx, skip_trim=False)
        except Exception:
            pass

    @staticmethod
    def _speed_label(state: int) -> str:
        return {FRAME_TYPE_2X: '2x', FRAME_TYPE_1X: '1x', FRAME_TYPE_0_2X: '0.2x', FRAME_TYPE_NORMAL: 'other'}.get(
            state, 'other')

    def _build_valid_segments_for_export(self, states: np.ndarray, split_by_speed: bool, merge_pause: bool) -> list:
        total_frames = int(self.total_frames)
        timeline_plan = self._build_timeline_plan(states=states)
        to_del = np.asarray(timeline_plan.to_delete_mask(), dtype=bool)
        valid = ~to_del

        segs = []
        i = 0
        while i < total_frames:
            if not valid[i]:
                i += 1
                continue

            cur_state = int(states[i])
            is_pause = (cur_state == FRAME_TYPE_PAUSE)
            speed_label = self._speed_label(cur_state)

            if is_pause and merge_pause:
                p_end = total_frames - 1
                for pseg in self.pause_segments:
                    if pseg['start'] <= i <= pseg['end']:
                        p_end = pseg['end']
                        break

                ranges = []
                j = i
                while j <= p_end and j < total_frames:
                    if valid[j] and int(states[j]) == FRAME_TYPE_PAUSE:
                        rs = j
                        while j <= p_end and j < total_frames and valid[j] and int(states[j]) == FRAME_TYPE_PAUSE:
                            j += 1
                        ranges.append((rs, j))
                    else:
                        j += 1

                segs.append({'ranges': ranges, 'label': 'pause_merged'})
                i = p_end + 1
            else:
                s = i
                while i < total_frames and valid[i]:
                    st = int(states[i])
                    if is_pause:
                        if st != FRAME_TYPE_PAUSE: break
                    else:
                        if st == FRAME_TYPE_PAUSE: break
                        if split_by_speed and self._speed_label(st) != speed_label: break
                    i += 1
                label = 'pause' if is_pause else (speed_label if split_by_speed else 'normal')
                if s < i:
                    segs.append({'ranges': [(s, i)], 'label': label})

        # 新增：合并因为中间“全删”而导致的连续同类型片段
        merged_segs = []
        for seg in segs:
            if not merged_segs:
                merged_segs.append(seg)
            else:
                last_seg = merged_segs[-1]
                # 当标签完全一致，且不是独立的暂停区时（避免两次分别的人工有效暂停被误合），进行跨区合并
                if last_seg['label'] == seg['label'] and 'pause' not in seg['label']:
                    last_seg['ranges'].extend(seg['ranges'])
                else:
                    merged_segs.append(seg)

        return merged_segs

    def export_segments(self):
        from tkinter import messagebox

        if self._closing:
            return
        if (
            getattr(self, "_export_handle", None) is not None
            or getattr(self, "_segment_export_handle", None) is not None
            or getattr(self, "_analysis_handle", None) is not None
        ):
            return messagebox.showwarning("导出进行中", "请等待当前导出任务结束。")
        if not self.video_path:
            return messagebox.showerror("错误", "请先加载视频")

        video_path = str(self.video_path)
        fps = float(self.fps or 30.0)
        project_generation, timeline_revision = self._task_scope()
        p = self.settings.get_params()
        total_frames = int(self.total_frames)
        quality = int(p["quality"])
        use_gpu = bool(p.get("export_use_gpu", False))
        gpu_encoder = str(p.get("gpu_encoder", ""))
        ffmpeg_path = p.get("ffmpeg_path")
        enforce_media_certification = bool(
            p.get("enforce_media_certification", False)
        )
        out_path = p.get("output") or ""
        if not out_path:
            return messagebox.showerror(
                "错误", "请先设置导出路径（用于确定分段输出目录）"
            )
        if enforce_media_certification and (
            self.media_info is None or not self.media_info.complete_for_export
        ):
            return messagebox.showerror(
                "无法导出",
                "当前源文件尚未绑定完整的 FramePtsCertification，"
                "请先完成帧时间戳认证。",
            )

        out_root = os.path.dirname(out_path) or os.getcwd()
        base = os.path.splitext(os.path.basename(out_path))[0] or "segments"
        out_dir_root = os.path.join(out_root, f"{base}_segments")
        states = (
            np.array(self.states_array, copy=True)
            if self.states_array is not None
            else np.zeros(total_frames, dtype=np.int8)
        )
        split = self.settings.segment_split_by_speed_var.get()
        merge_pause = self.settings.merge_pause_ops_var.get()
        segs = self._build_valid_segments_for_export(states, split, merge_pause)
        timeline_plan = TimelinePlan.from_kept_ranges(
            total_frames,
            [item for seg in segs for item in seg["ranges"]],
        )
        if not segs:
            return messagebox.showwarning(
                "提示", "当前时间轴没有可导出的有效片段。"
            )

        if not self._pause_preview_for_export():
            self.settings.segment_export_status_var.set(
                "无法安全关闭预览，已取消分段导出"
            )
            return
        self.settings.segment_export_btn.config(state=tk.DISABLED)
        self.settings.segment_export_progress_var.set(0)
        self.settings.segment_export_status_var.set("准备分段导出…")

        def work(context):
            context.checkpoint()
            os.makedirs(out_dir_root, exist_ok=True)
            out_dir = tempfile.mkdtemp(
                prefix=f"run-{time.strftime('%Y%m%d-%H%M%S')}-",
                dir=out_dir_root,
            )
            total = len(segs)
            pad = max(1, len(str(total)))
            completed = 0
            succeeded = 0
            failures = []

            # Each FFmpeg encoder already uses its own internal parallelism.
            # Serial files give deterministic cancellation and avoid a nested
            # non-daemon executor that could outlive the Tk application.
            for idx, seg in enumerate(segs, start=1):
                context.checkpoint()
                stem = f"{idx:0{pad}d}_{seg['label']}"
                final_path = os.path.join(out_dir, f"{stem}.mp4")
                try:
                    request = ExportRequest.ranges_export(
                        video_path,
                        final_path,
                        timeline_plan,
                        list(seg["ranges"]),
                        fps=fps,
                        quality=quality,
                        use_gpu=use_gpu,
                        gpu_encoder=gpu_encoder,
                        ffmpeg_path=ffmpeg_path,
                        include_audio=False,
                        media_info=getattr(self, "media_info", None),
                        enforce_media_certification=enforce_media_certification,
                    )
                    result = MediaExporter().export(
                        request,
                        cancel_cb=context.checkpoint,
                        source_path_override=video_path,
                        commit_cb=lambda source, target: context.commit(
                            os.replace, source, target
                        ),
                    )
                    written = result.written_frames
                    succeeded += 1
                except TaskCancelled:
                    raise
                except Exception as exc:
                    failures.append((stem, str(exc)))

                completed += 1
                context.report(
                    {
                        "ratio": completed / total,
                        "completed": completed,
                        "succeeded": succeeded,
                        "total": total,
                    }
                )

            # All per-file publishes are complete.  Mark the batch final while
            # it still owns this project scope so a later edit cannot relabel
            # already committed output as a cancelled export.
            context.commit(lambda: None, final=True)
            return {
                "out_dir": out_dir,
                "total": total,
                "succeeded": succeeded,
                "failures": failures,
            }

        def on_progress(value):
            if self._closing:
                return
            completed = value["completed"]
            succeeded = value["succeeded"]
            total = value["total"]
            self.settings.segment_export_progress_var.set(value["ratio"] * 100)
            self.settings.segment_export_status_var.set(
                f"处理 {completed}/{total}，成功 {succeeded}，"
                f"失败 {completed - succeeded}"
            )

        def on_success(result):
            if self._closing:
                return
            total = result["total"]
            succeeded = result["succeeded"]
            failures = result["failures"]
            failed = len(failures)
            self.settings.segment_export_progress_var.set(100)
            self.settings.segment_export_status_var.set(
                f"完成：成功 {succeeded}/{total}，失败 {failed}"
                "（分段默认不保留音频）"
            )
            details = "\n".join(
                f"- {name}: {error[:500]}" for name, error in failures[:3]
            )
            if failed:
                messagebox.showwarning(
                    "分段导出完成",
                    f"输出目录：{result['out_dir']}\n"
                    f"成功：{succeeded}/{total}\n失败：{failed}/{total}"
                    + (f"\n\n部分错误：\n{details}" if details else ""),
                )
            else:
                messagebox.showinfo(
                    "分段导出完成",
                    f"输出目录：{result['out_dir']}\n"
                    f"成功：{succeeded}/{total}\n"
                    "说明：分段导出默认不保留音频。",
                )

        def on_error(exc):
            if self._closing:
                return
            self.settings.segment_export_status_var.set(f"失败：{str(exc)[:80]}")
            messagebox.showerror("分段导出失败", str(exc))

        def on_cancelled():
            if not self._closing:
                self.settings.segment_export_status_var.set(
                    "分段导出已取消；已完成文件保留，未完成临时文件已清理"
                )

        def on_done(_status):
            self._segment_export_handle = None
            if self._closing:
                return
            self.settings.segment_export_btn.config(state=tk.NORMAL)
            self._resume_preview_after_export()

        try:
            self._segment_export_handle = self.task_manager.submit(
                _SEGMENT_EXPORT_TASK,
                work,
                on_success=on_success,
                on_error=on_error,
                on_progress=on_progress,
                on_cancelled=on_cancelled,
                on_done=on_done,
                project_generation=project_generation,
                timeline_revision=timeline_revision,
                replace=True,
            )
        except RuntimeError as exc:
            self.settings.segment_export_btn.config(state=tk.NORMAL)
            self.settings.segment_export_status_var.set(str(exc))
            self._resume_preview_after_export()
