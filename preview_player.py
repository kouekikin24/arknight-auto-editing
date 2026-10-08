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
    CertifiedEdlRequest,
    PreviewEngine,
    PreviewEngineError,
    PreviewPlayRequest,
    SourceSeekRequest,
)
from dthrough_ledger import derive_spans

# 解码预算限速（09-13 真机首测 T0）：直通档解码账单=rate×60 帧/秒，0.2x
# rate 封顶=解码预算：帽×60 须 ≤ 真实环境吞吐（含 GUI/管道/mpv 渲染竞争）。
# 09-13 午 live T0：帽 32 时 owner 真机 3.mp4 呈现 43.2fps（需求 1920fps，
# dsvc 冲到 770% 核心跳）→ 有效吞吐 ≈1380fps，裸机 bench 2029 未含消费者
# 开销不可作依据 → 帽回撤 20（mixedrate 验证平滑）。
# 09-13 深夜 1e 非参考 B 帧丢弃落地（dsvc skip_frame=NONREF，丢弃 75% 喂入
# 包）→ 帽曾提至 24。**09-16 真机 T0 回撤至 20**：owner 4.mp4 4x 手检报
# 「卡顿 + 部分片段卡住半秒以上」，实测持续高倍率段呈现 43.2fps、dsvc 冲
# 过 770% 核心跳 → 有效吞吐 ≈1380fps（含 GUI/管道/mpv 渲染竞争），帽 24
# 的 1440fps 需求超出。帽 20=需求 1200fps，留 ~13% 余量。0.2x 段=导出该段
# 2 倍速；理想 40×（2400fps）超本机 CPU 物理墙，待 GPU 立项。
# DSVC_DISCARD_NONREF=0 可关丢弃。
_DTHROUGH_RATE_CAP = 20
# 碎段守卫已移除（09-13 真机 T0）：旧口径「任一 span 呈现帧数≥2」被 0.2x 岛
# 触发——11 源帧岛在 cap32 下呈现 0.34 帧 → 整单打回 Tier1+skim（糊+12x），
# 直通在真实项目上永远无法接管。改造后守卫的立论（每段固定换段成本）已
# 不成立：连续边界零成本、restart 税被缓冲吸收（probe_dthrough_frag B 相
# Δ17 restart≈1ms/次）、且 ceil(n_src/rate)≥1 保证每段至少呈现 1 帧。
from timeline_widget import TimelineWidget
from timeline_plan import TimelinePlan
from media_exporter import ExportRequest, ExportResult, MediaExporter
from media_info import resolve_ffmpeg_path
from task_manager import TaskCancelled, TaskContext, TaskHandle, TaskManager
from project_state import ProjectState
from edit_commands import SetClipBounds, SetPauseMaskRun, SetPauseMode
import skim_cache


_ANALYSIS_TASK = "player.analysis"
_MEDIA_INFO_TASK = "player.media_info"
_FRAME_PTS_TASK = "player.frame_pts_certification"
_EXPORT_TASK = "player.export.full"
_SEGMENT_EXPORT_TASK = "player.export.segments"
_SKIM_TASK = "player.skim_build"


def _run_analysis_task(context: TaskContext, snapshot: dict) -> tuple:
    """Run video analysis without touching Tk or mutable player state."""
    import analyzer

    video_path = snapshot["video_path"]
    proc_res = list(snapshot["proc_res"])
    # 取证锚点（flush）：入口即打一条——worker 任何阶段崩溃时，
    # 日志能分辨「没进分析」还是「分析中段死」（Errno 22 现场教训：
    # 无 flush 的 [analyze] 行会死在块缓冲里，弹窗又不带堆栈）。
    print(f"[analyze] worker start video={video_path}", flush=True)
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
        f"proc_res={proc_res} video={video_path}",
        flush=True,
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


def _run_skim_build_task(context: TaskContext, snapshot: dict):
    """后台构建 P1-RAM 掠过代理（Tier2），不触碰 Tk 状态。

    阶梯自动降档（480p→360p→240p）由 build_skim_blob_auto 负责；
    SkimBuildError 由 on_error 接住 → 回落 Tier1 活 EDL + cap，
    播放链永不因掠过代理失败而中断。
    """
    context.checkpoint()
    blob = skim_cache.build_skim_blob_auto(
        snapshot["media_info"],
        snapshot["plan"],
        project_generation=snapshot["project_generation"],
        timeline_revision=snapshot["timeline_revision"],
        # TaskContext 只暴露 cancelled 属性（无 Event 本体）→ 传 callable
        cancel_event=lambda: context.cancelled,
    )
    context.checkpoint()
    return blob


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
        # P1-RAM 掠过代理（Tier2）构建任务：head = 在飞构建的代数键前 4 元
        # （源 sha × 段表指纹 × 作用域），防同代重复提交。
        self._skim_build_handle: TaskHandle | None = None
        self._skim_build_head: tuple | None = None
        self._skim_rebuild_after = None
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
        # 片尾回绕判据：引擎报 EOF 后置 True；下一次 _send_play 消费后清。
        # 用于区分「播完重播」（该回绕）与「暂停恢复/定位」（不该回绕）。
        self._play_ended_at_eof: bool = False
        # 分析缓存首次提示（调研结论：默认复用但不静默，提示一次）
        self._analysis_cache_prompted: bool = False
        # 用户拒绝过的缓存键（拒绝=要亲手重算；重导入/换源时复位）
        self._analysis_cache_declined_key: str | None = None
        self._last_analysis_snapshot: dict | None = None
        # 媒体探测在飞时挂起的分析请求（源路径）。导入后数秒内即点分析时，
        # 缓存键所需的源 sha256 尚未就绪——挂起而非静默跳过（09-23 修复）。
        self._pending_analysis_source: str | None = None
        # 选项2 闭环验证定位：飞行中的验证目标帧与看门狗截止（引擎
        # seek-verified 事件缺失时的双保险；成功=主窗原生精确帧不贴
        # 静帧，失败=事件驱动回退 C4）。
        self._verify_pending_frame: int | None = None
        self._verify_pending_until: float = 0.0
        # 暂停静帧覆盖层（MLT/Shotcut 模式）：暂停态的画面由独立解码管线
        # 出静帧贴到覆盖层，帧精确；mpv 只负责播放。gen 丢弃过期解码。
        self._overlay_gen: int = 0
        self._still_overlay = None
        self._still_photo = None
        self._still_last_gray = None
        self._still_pending = None
        self._still_decoding: bool = False
        self._still_resize_after = None
        self._video_dims: tuple[int, int] | None = None
        self._still_matrix = None
        self._was_playing: bool = False
        # PyAV 静帧解码（API 层主选）：关键帧索引表后台构建（npz 缓存），
        # 未就绪/未安装时自动回退 ffmpeg CLI 两段式，再回退 OpenCV
        self._pyav_kf = None
        self._pyav_state = "idle"
        self._pyav_container = None
        # 方案 C：暂停静帧由隐藏 mpv 实例自渲染（单帧 Y4M 喂入），与播放
        # 同一渲染管线 → 逐像素一致。主线程建实例，工作线程只发命令。
        self._still_mpv = None
        self._still_mpv_top = None
        self._still_mpv_size = (0, 0)
        self._still_mpv_settle_until = 0.0
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
        # 右键：复制帧号到剪贴板（OSD 画在视频上没法选中复制）
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

        # mpv 原生渲染时用 mpv OSD 在画面上叠加当前源帧号（编辑时直接读屏）
        self.show_frame_osd_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            ctrl, text="屏显帧号", variable=self.show_frame_osd_var,
            command=self._on_frame_osd_toggle,
        ).pack(side=tk.LEFT, padx=5)

        # 右上角播放帧率 OSD 的独立开关（与帧号分开控制）
        self.show_fps_osd_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            ctrl, text="屏显帧率", variable=self.show_fps_osd_var,
            command=self._on_fps_osd_change,
        ).pack(side=tk.LEFT, padx=5)

        # mpv 原生窗口会吞掉视频区右键，复制帧号的入口放在控制条上
        ttk.Button(ctrl, text="复制帧号", width=8,
                   command=self._copy_frame_no).pack(side=tk.LEFT, padx=(8, 0))

        # 默认开优化；取消勾选 / 按 O = #9 式基线（只统计，不追帧软锚）
        # 注：pace 对照只对 cv 引擎生效；mpv 引擎下隐藏（见 ARCHIVE_PREVIEW_PACE.md）
        self.preview_opt_var = tk.BooleanVar(value=True)
        self.chk_preview_opt = ttk.Checkbutton(
            ctrl, text="预览优化", variable=self.preview_opt_var,
            command=self._on_preview_opt_change,
        )
        self.chk_preview_opt.pack(side=tk.LEFT, padx=5)
        if self._preview_engine_kind == "mpv":
            # 视频未加载时尚无引擎切换事件，启动即按目标引擎隐藏
            self.chk_preview_opt.pack_forget()

        self.lbl_time = ttk.Label(ctrl, text="00:00 / 00:00")
        self.lbl_time.pack(side=tk.RIGHT, padx=10)

        self.lbl_info = ttk.Label(self, text="就绪", foreground="#00CED1", font=("Consolas", 10))
        self.lbl_info.pack(fill=tk.X, padx=10, pady=2)

        hint = "← → 逐帧移动  |  空格 播放  |  时间轴：右键点击黄色块手动删除操作，鼠标中键平移"
        ttk.Label(self, text=hint, foreground="#555555", font=("Consolas", 8)).pack(fill=tk.X, padx=10, pady=(0, 2))

        self._render_loop()
        self._bind_after_id = self.after_idle(self._bind_keys)

    def _show_engine_surface(self, native: bool, engine: PreviewEngine | None = None) -> None:
        if native:
            self.video_canvas.pack_forget()
            self.mpv_host.pack(fill=tk.BOTH, expand=True)
            self._raise_native_cover()
            # pace 对照开关只对 cv 引擎有真实效果；mpv 下隐藏以免误导
            self.chk_preview_opt.pack_forget()
        else:
            self.mpv_host.pack_forget()
            self._drop_native_cover()
            self.video_canvas.pack(fill=tk.BOTH, expand=True)
            self.chk_preview_opt.pack(side=tk.LEFT, padx=5)
        try:
            self.video_surface.update_idletasks()
            target = engine or self._io
            if target is not None:
                width = max(1, self.video_surface.winfo_width() or self.canvas_w)
                height = max(1, self.video_surface.winfo_height() or self.canvas_h)
                target.set_viewport(width, height, self._preview_dpi_scale())
        except Exception:
            pass

    def _copy_frame_no(self) -> None:
        """复制当前帧号（纯数字，方便直接贴进排查工具）到剪贴板。"""
        self._copy_to_clipboard(str(int(self.current_frame_idx)))

    def _on_video_right_click(self, event) -> None:
        """视频区右键：复制帧号菜单。

        原生 mpv 子窗口会吞掉右键事件，此菜单实际只对 CV 引擎触发；
        mpv 下用控制条上的「复制帧号」按钮。
        """
        if self._closing:
            return
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(
            label=f"复制帧号  ({int(self.current_frame_idx)})",
            command=self._copy_frame_no,
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
        # 静帧的黑边和帧号已按窗口尺寸烧进位图：尺寸变了按新尺寸重出
        if not self.is_playing and self._still_overlay is not None:
            if self._still_resize_after is not None:
                try:
                    self.after_cancel(self._still_resize_after)
                except Exception:
                    pass
            self._still_resize_after = self.after(180, self._reblit_resized_still)

    def _reblit_resized_still(self) -> None:
        self._still_resize_after = None
        if self._closing or self.is_playing or self._still_overlay is None:
            return
        if self._still_decoding:
            # 在飞的解码出的是旧尺寸：落地后再按新尺寸重出一帧
            self._still_resize_after = self.after(180, self._reblit_resized_still)
            return
        self._show_paused_still(int(self.current_frame_idx))

    def _on_frame_osd_toggle(self) -> None:
        """帧号开关：播放中立即显隐 mpv overlay；暂停中重出静帧（烧字与否）。

        播放中取消勾选必须主动清——持久 overlay 不会自己消失，
        不清会把旧帧号定格在画面上。
        """
        try:
            on = bool(self.show_frame_osd_var.get())
        except Exception:
            return
        if self.is_playing:
            if on:
                self._osd_pending = True
                self._maybe_show_frame_osd(self.current_frame_idx)
            else:
                shower = getattr(self._io, "show_osd_topleft_text", None)
                if callable(shower):
                    try:
                        shower("")
                    except Exception:
                        pass
        elif self._still_overlay is not None:
            self._show_paused_still(int(self.current_frame_idx))

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
            vw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            vh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        finally:
            cap.release()
        # 静帧化妆链按 mpv 的 floor 取整规则预计算内容尺寸，需要源尺寸
        self._video_dims = (vw, vh) if vw > 0 and vh > 0 else None
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
        self._teardown_still_mpv()
        self.task_manager.invalidate(_ANALYSIS_TASK)
        self.task_manager.invalidate(_MEDIA_INFO_TASK)
        self.task_manager.invalidate(_FRAME_PTS_TASK)
        self.task_manager.invalidate(_EXPORT_TASK)
        self.task_manager.invalidate(_SEGMENT_EXPORT_TASK)
        self.task_manager.invalidate(_SKIM_TASK)
        self._analysis_handle = None
        self._media_info_handle = None
        self._frame_pts_handle = None
        self._export_handle = None
        self._segment_export_handle = None
        self._pending_analysis_source = None
        self._skim_build_handle = None
        self._skim_build_head = None
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
            "_skim_rebuild_after",
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
        self._pending_analysis_source = None
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
        # 换源 → 缓存交互状态复位（否则第二个视频的"首次提示"不再出现）
        self._analysis_cache_prompted = False
        self._analysis_cache_declined_key = None
        self._last_analysis_snapshot = None
        self._pending_analysis_source = None
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
            # 导入后立即点分析而被挂起的请求：源身份就绪 → 续跑
            self._maybe_resume_pending_analysis()

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
            # 探测失败=源身份不可用=缓存键无法构造：如实告知而非静默冷跑
            if self._cancel_pending_analysis("媒体探测失败"):
                from tkinter import messagebox
                messagebox.showwarning(
                    "媒体探测失败",
                    "无法读取源媒体信息，分析缓存不可用。\n"
                    "请排查后重试「自动模板分析」。",
                )

        def on_done(_status) -> None:
            self._media_info_handle = None
            # 探测落定但仍无源身份（源被改动/删除、或 on_success 因源过期
            # 早退）→ 挂起请求不得悬死（09-23：on_error 已自行作废并提示，
            # 此处只兜住未提示的剩余路径）
            if (
                self._pending_analysis_source is not None
                and not self._closing
                and getattr(self, "media_info", None) is None
            ):
                if self._cancel_pending_analysis("媒体探测未产出可用结果"):
                    from tkinter import messagebox
                    messagebox.showwarning(
                        "媒体探测失败",
                        "无法读取源媒体信息，分析缓存不可用。\n"
                        "请重新导入视频后重试「自动模板分析」。",
                    )

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

    def _maybe_schedule_skim_build(self) -> None:
        """P1-RAM Tier2：段表/作用域变化时后台构建内存掠过代理。

        触发点：分析落定（_finish_analysis 预热）与每次播放请求
        （_send_play 兜底，覆盖编辑与开关切换）。守卫：mpv 原生引擎 +
        ram:// 协议已注册 + 账本已认证 + 跳裁剪开 + 存在删除段；引擎
        skim_key 前 4 元已匹配当前代数或在飞构建同代时跳过。
        replace=True：新编辑直接顶替旧构建（scope 失效同向兜底）。
        任何失败只打日志回落 Tier1（活 EDL + preview_step_cap 封顶），
        播放链不受影响。守卫全部 getattr 防御：隔离测试的 legacy 桩
        没有完整 __init__，任何缺件都静默 no-op。
        """
        if getattr(self, "_closing", True):
            return
        engine = getattr(self, "_io", None)
        media = getattr(self, "media_info", None)
        if engine is None or media is None:
            return
        if not getattr(engine, "skim_ready", False):
            return
        if not getattr(media, "frame_pts_authoritative", False):
            return
        skip_var = getattr(self, "skip_trimmed", None)
        try:
            if skip_var is None or not bool(skip_var.get()):
                return
        except Exception:
            return
        settings = getattr(self, "settings", None)
        try:
            params = settings.get_params() if settings is not None else {}
        except Exception:
            params = {}
        if not params.get("skim_preview", True):
            return  # 设置面板关闭了快速掠过代理
        try:
            plan = self._build_timeline_plan()
        except Exception:
            return
        if not plan.deleted_ranges:
            return
        project_generation, timeline_revision = self._task_scope()
        head = (
            str(media.source_sha256),
            str(plan.fingerprint),
            int(project_generation),
            int(timeline_revision),
        )
        current_key = getattr(engine, "skim_key", None)
        if current_key is not None and tuple(current_key[:4]) == head:
            return  # 引擎已持有当前代 blob
        if getattr(self, "_skim_build_head", None) == head:
            return  # 同代构建在飞
        snapshot = {
            "media_info": media,
            "plan": plan,
            "project_generation": project_generation,
            "timeline_revision": timeline_revision,
        }
        source_path = str(Path(self.video_path).expanduser().resolve())

        def on_success(blob) -> None:
            self._skim_build_head = None
            if self._closing or self._io is not engine:
                return
            try:
                if Path(self.video_path).resolve() != Path(source_path):
                    return
            except OSError:
                return
            try:
                installed = bool(engine.set_skim_blob(blob.data, blob.key))
            except Exception as exc:
                print(f"[skim] blob 安装失败: {type(exc).__name__}: {exc}",
                      flush=True)
                return
            if installed:
                print(f"[skim] 掠过代理就绪: {blob.mb:.1f}MB / "
                      f"{blob.kept_frames} 帧 / 构建 {blob.build_s:.1f}s",
                      flush=True)

        def on_error(exc: BaseException) -> None:
            self._skim_build_head = None
            if isinstance(exc, (skim_cache.SkimBuildCancelled, TaskCancelled)):
                return  # 被新构建顶替/scope 失效，静默
            print(f"[skim] 构建失败（回落 Tier1 活 EDL）: "
                  f"{type(exc).__name__}: {exc}", flush=True)

        def on_cancelled() -> None:
            self._skim_build_head = None

        def on_done(_status) -> None:
            self._skim_build_handle = None

        self._skim_build_head = head
        try:
            self._skim_build_handle = self.task_manager.submit(
                _SKIM_TASK,
                lambda context: _run_skim_build_task(context, snapshot),
                on_success=on_success,
                on_error=on_error,
                on_cancelled=on_cancelled,
                on_done=on_done,
                project_generation=project_generation,
                timeline_revision=timeline_revision,
                replace=True,
            )
        except RuntimeError as exc:
            self._skim_build_head = None
            print(f"[skim] 任务提交被拒: {exc}", flush=True)

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
        # 段表=活文档：任何变更（编辑命令/分析落定/加载恢复）都让引擎里
        # 的 skim blob 代数过期（key 守卫自动回落 Tier1）。重建不在变更时
        # 预热——只在 >3x 播放且直通失格时懒触发（09-13 T0：预热=任何编辑
        # 后白烧 5 分钟+294MB，且 blob 在库即用令 ≤cap 播放也糊）。

    _SKIM_REBUILD_DEBOUNCE_MS = 1200

    def _arm_skim_rebuild(self) -> None:
        """编辑落定 debounce：拖动/刷掩码高频发快照，静默 1.2s 才排重建。

        窗口内重复 arm 顺延；调度与播放互不干扰——引擎换代是整体换
        引用，已打开的流继续读旧快照，新 blob 下一次 loadfile 生效。
        """
        if getattr(self, "_closing", True):
            return
        after = getattr(self, "after", None)
        if not callable(after):
            return  # legacy 桩没有 Tk after
        pending = getattr(self, "_skim_rebuild_after", None)
        if pending is not None:
            try:
                self.after_cancel(pending)
            except Exception:
                pass
        try:
            self._skim_rebuild_after = self.after(
                self._SKIM_REBUILD_DEBOUNCE_MS, self._on_skim_rebuild_due
            )
        except Exception:
            self._skim_rebuild_after = None

    def _on_skim_rebuild_due(self) -> None:
        self._skim_rebuild_after = None
        if getattr(self, "_closing", True):
            return
        try:
            self._maybe_schedule_skim_build()
        except Exception as exc:
            print(f"[skim] debounce 重建调度失败: {type(exc).__name__}: {exc}",
                  flush=True)

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
        # 直通档定位：源帧 → 台账名义 tick → dsvc 重定位。必须先于 EDL/
        # verified/源寻址拦截——那些路径按 EDL 虚拟秒或源秒换算字节地址，
        # 对 rawpush CFR 轴全是错位坐标。
        if bool(getattr(self._io, "dthrough_active", False)):
            self._io.seek_dthrough_source_frame(int(frame_idx))
            if not self.is_playing:
                # 静帧贴请求帧（C4 独立解码）；恢复播放后引擎回读的
                # 网格落点帧号（≤请求帧，±1 抽帧 tick）经 perf 自校正
                self._show_paused_still(int(frame_idx))
            return
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
        # 选项2：暂停+原生引擎+非拖动 → 闭环验证定位（无修正 seek →
        # 等落定 → 读 tp 对账 → frame-step 补齐 → 复核）。飞行中不贴
        # 静帧（追帧逻辑被 _verify_pending_frame 抑制）；成功=主窗原生
        # 呈现精确帧，失败=seek-verified 事件驱动回退 C4。重复 PTS 段
        # 目标/EDL/未认证由引擎拒收（False/异常），落到下面的旧路径。
        verified_fn = getattr(self._io, "seek_source_verified", None)
        if (not self.is_playing and not self._is_dragging
                and bool(getattr(self._io, "native_rendering", False))
                and callable(verified_fn)):
            try:
                started = bool(verified_fn(SourceSeekRequest(
                    source_frame=int(frame_idx),
                    canvas_size=self._canvas_wh(),
                    timeline_revision=timeline_revision,
                    exact=True,
                    latest_only=True,
                )))
            except PreviewEngineError as exc:
                if exc.code != "CERTIFICATION_REQUIRED":
                    raise
                started = False
            if started:
                self._verify_pending_frame = int(frame_idx)
                # 看门狗比引擎超时（1.2s）宽一档：事件丢失也能兜底
                self._verify_pending_until = time.monotonic() + 1.6
                return
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

    def _on_seek_verified(self, event: dict) -> None:
        """闭环验证定位的结果事件（引擎 seek_source_verified 出队）。

        成功：主窗口原生呈现的已经是账本复核过的精确帧——撤掉任何旧
        静帧覆盖层、对齐收敛标记（挡追帧重贴）、刷帧号 OSD。
        失败（超时/过冲/超预算/异常）：回退 C4 静帧管线，精确性由
        构造保证（独立解码+计数+指纹），永不显示错帧。
        """
        if self._closing:
            return
        frame = event.get("frame")
        if frame is None:
            return
        frame = int(frame)
        if self._verify_pending_frame != frame:
            return  # 过期结果：已被更新的动作取代（引擎侧也已静默作废）
        self._verify_pending_frame = None
        if event.get("ok"):
            self._drop_still_overlay()
            self._still_frame_idx = frame
            self._osd_pending = True
        else:
            self._show_paused_still(frame)

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
        # 帧号文本在主线程拼好（与播放态 mpv OSD 同格式）；开关关=空串=不烧字
        try:
            show_osd = bool(self.show_frame_osd_var.get())
        except Exception:
            show_osd = True
        osd_text = (self._still_frame_text(int(frame_idx)) if show_osd else "")
        # 方案 C：主线程建/复用隐藏渲染实例（工作线程不可碰 Tk）
        self._ensure_still_mpv(w, h)
        self._overlay_gen += 1
        gen = self._overlay_gen
        self._still_decoding = True
        threading.Thread(
            target=self._decode_still_work,
            args=(int(frame_idx), gen, w, h, float(t), ffmpeg_path,
                  expected_bright, osd_text),
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

    def _still_frame_text(self, frame_idx: int) -> str:
        """与播放态 mpv OSD 完全一致的帧号文本。"""
        return f"帧 {frame_idx:,} / {max(0, self.total_frames - 1):,}"

    def _build_still_ass(self, w: int, h: int, text: str):
        """生成与 mpv OSD 逐像素对齐的临时 ASS，返回路径（失败 None）。

        常数来自 .cache/v3ass 标定（三尺寸 bbox 零差）：两侧同为 libass +
        Microsoft YaHei 是一致的前提；PlayResX=W*720/H 使横纵缩放同为
        H/720，与 mpv 的窗高缩放定律一致。
        """
        try:
            import tempfile
            px = max(1, round(w * 720 / h))
            ass = (Path(tempfile.gettempdir())
                   / f"arknight_still_osd_{os.getpid()}.ass")
            ass.write_text(
                "[Script Info]\nScriptType: v4.00+\n"
                f"PlayResX: {px}\nPlayResY: 720\n"
                "ScaledBorderAndShadow: yes\n\n"
                "[V4+ Styles]\n"
                "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, "
                "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, "
                "Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
                "MarginL, MarginR, MarginV, Encoding\n"
                "Style: OSD,Microsoft YaHei,30,&H00FFFFFF,&H00000000,&H80000000,"
                "0,0,0,0,100,100,0,0,1,1.65,0,7,15,15,15,1\n\n"
                "[Events]\n"
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
                "MarginV, Effect, Text\n"
                f"Dialogue: 0,0:00:00.00,0:00:01.00,OSD,,0,0,0,,{text}\n",
                encoding="utf-8")
            return ass
        except Exception:
            return None

    def _still_color_matrix(self) -> str:
        """静帧链的 yuv→rgb 矩阵，与 mpv 对未标记流的启发式对齐。

        源带颜色标记则尊重标记（bt709/601 系）；未标记则按 mpv 规则：
        高 ≥720 用 bt709，否则 bt601。实测本仓 OBS 源全部未标记，mpv 按
        709 渲染——静帧若用 swscale 默认的 601，饱和区会产生 R/G 通道
        1.5+ 灰阶的系统性偏差（用户可察觉的"颜色有点不一样"）。
        """
        m = self._still_matrix
        if m is not None:
            return m
        tag = None
        try:
            import av
            c = av.open(str(self.video_path))
            raw = getattr(c.streams.video[0].codec_context, "colorspace", None)
            c.close()
            tag = int(getattr(raw, "value", raw)) if raw is not None else None
        except Exception:
            tag = None
        if tag == 1:             # AVCOL_SPC_BT709
            m = "bt709"
        elif tag in (5, 6):      # BT470BG / SMPTE170M（601 系）
            m = "smpte170m"
        else:                    # 未标记：跟随 mpv 的高清启发式
            dims = self._video_dims
            m = "bt709" if (dims and dims[1] >= 720) else "smpte170m"
        self._still_matrix = m
        return m

    def _still_filter_chain(self, w: int, h: int, ass_path,
                            select_expr: str = "", native_dims=None,
                            matrix=None) -> str:
        """静帧化妆链：[select] → 保比例缩放 → 补黑边 → [矩阵打标] → 烧帧号。

        内容尺寸按 mpv 的 floor 取整规则在 Python 侧算死：实测同一
        1920x1080 源在 1000x483 目标下，mpv 取 858 宽放 x=71，scale 滤镜
        的自动比例取 859 放 x=70——暂停/播放切换会有 1px 横移。native_dims
        未知时退回自动比例（接受 ≤1px 差）。ass= 的 Windows 路径必须
        正斜杠 + 冒号转义 + 单引号包裹。ass 滤镜按帧 PTS 匹配 Dialogue
        时间窗：两段式寻址带 -copyts 时帧带着源片原始大 PTS，必须 setpts
        归零，否则文本静默不渲染。matrix 只能由 scale 滤镜的
        in_color_matrix 亲自执行（实测 setparams 打标不驱动自动转换器）；
        设了矩阵后 scale 直接输出 rgb，pad/ass 在 RGB 域工作。
        """
        chain = select_expr
        cw = ch = 0
        if native_dims:
            nw, nh = native_dims
            if nw > 0 and nh > 0:
                if w * nh >= nw * h:    # 高度绑定（目标比源更宽）
                    ch = h
                    cw = max(1, (h * nw) // nh)
                else:                   # 宽度绑定
                    cw = w
                    ch = max(1, (w * nh) // nw)
        cm = f":in_color_matrix={matrix}" if matrix else ""
        if cw:
            chain += (f"scale={cw}:{ch}:flags=area{cm},"
                      f"pad={w}:{h}:{(w - cw) // 2}:{(h - ch) // 2}:color=black")
        else:
            chain += (f"scale={w}:{h}:force_original_aspect_ratio=decrease"
                      f":flags=area{cm},"
                      f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black")
        if ass_path is not None:
            esc = str(ass_path).replace("\\", "/").replace(":", "\\:")
            chain += f",setpts=PTS-STARTPTS,ass='{esc}'"
        return chain

    def _still_burn_ffmpeg(self, data: bytes, nw: int, nh: int, pix_fmt: str,
                           w: int, h: int, ass_path, ffmpeg_path):
        """原生帧字节 → ffmpeg CLI 化妆链 → 窗口尺寸成品图。

        pix_fmt 为 yuv420p 时颜色矩阵在链里按 mpv 启发式打标（消除
        601/709 分歧）；rgb24 输入的矩阵转换已在上游发生，不再干预。
        """
        try:
            ff = resolve_ffmpeg_path(ffmpeg_path)
            matrix = self._still_color_matrix() if pix_fmt == "yuv420p" else None
            cmd = [str(ff), "-y", "-loglevel", "error",
                   "-f", "rawvideo", "-pix_fmt", pix_fmt, "-s", f"{nw}x{nh}",
                   "-i", "-",
                   "-vf", self._still_filter_chain(w, h, ass_path,
                                                   native_dims=(nw, nh),
                                                   matrix=matrix),
                   "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
            out = subprocess.run(cmd, input=data,
                                 capture_output=True, timeout=15).stdout
            if len(out) < w * h * 3:
                return None
            return PIL.Image.frombytes("RGB", (w, h), bytes(out[: w * h * 3]))
        except Exception:
            return None

    def _burn_av_frame(self, frame, w: int, h: int, ass_path, ffmpeg_path):
        """PyAV 帧的化妆入口：优先零转换地喂 yuv420p 平面（矩阵在链里
        对齐 mpv），非 yuv420p 源退回 rgb24。"""
        nw, nh = frame.width, frame.height
        yuv = self._pyav_frame_yuv420p_bytes(frame)
        if yuv is not None:
            img = self._still_burn_ffmpeg(yuv, nw, nh, "yuv420p",
                                          w, h, ass_path, ffmpeg_path)
            if img is not None:
                return img
        rgb = frame.to_ndarray(format="rgb24")
        pil = PIL.Image.fromarray(rgb)
        return (self._still_burn_ffmpeg(rgb.tobytes(), nw, nh, "rgb24",
                                        w, h, ass_path, ffmpeg_path)
                or pil.resize((w, h)))

    # ---- 方案 C：隐藏 mpv 实例自渲染静帧（与播放同管线，逐像素一致） ----

    def _ensure_still_mpv(self, w: int, h: int):
        """主线程建/复用离屏隐藏 mpv 实例（wid 嵌入无任务栏按钮的
        Toplevel，运行时可改尺寸免重建）。任何失败返回 None → 走兜底链。
        """
        if self._still_mpv is not None:
            if (w, h) != self._still_mpv_size and self._still_mpv_top is not None:
                try:
                    self._still_mpv_top.geometry(f"{w}x{h}+20000+20000")
                    self._still_mpv_top.update_idletasks()
                    self._still_mpv_size = (w, h)
                    # 窗口 resize 触发 mpv vo 重配（其内部线程异步进行）；
                    # 工作线程的命令必须等重配完成，否则 d3d11 交换链竞态
                    self._still_mpv_settle_until = time.monotonic() + 0.4
                except Exception:
                    pass
            return self._still_mpv
        try:
            import mpv
            top = tk.Toplevel(self)
            top.overrideredirect(True)
            top.geometry(f"{w}x{h}+20000+20000")
            top.configure(bg="black")
            top.update_idletasks()
            p = mpv.MPV(
                vo="gpu", wid=str(top.winfo_id()), idle="yes",
                keep_open="yes", pause="yes", hr_seek="yes", osc="no",
                audio="no", terminal="no", input_default_bindings="no",
                input_vo_keyboard="no", osd_font="Microsoft YaHei",
                keepaspect_window="no", image_display_duration="inf",
                osd_duration=1)
            self._still_mpv = p
            self._still_mpv_top = top
            self._still_mpv_size = (w, h)
            return p
        except Exception:
            self._still_mpv = None
            self._still_mpv_top = None
            return None

    def _teardown_still_mpv(self) -> None:
        # 等工作线程离开 libmpv 命令窗口（identity 检查之外的最后保险）
        end = time.time() + 1.0
        while time.time() < end and self._still_decoding:
            time.sleep(0.05)
        p = self._still_mpv
        top = self._still_mpv_top
        self._still_mpv = None
        self._still_mpv_top = None
        self._still_mpv_size = (0, 0)
        if p is not None:
            try:
                p.terminate()
            except Exception:
                pass
        if top is not None:
            try:
                top.destroy()
            except Exception:
                pass

    @staticmethod
    def _write_still_y4m(path, yuv: bytes, w: int, h: int) -> None:
        header = (f"YUV4MPEG2 W{w} H{h} F60:1 Ip A1:1 C420mpeg2 "
                  f"XYSCSS=420MPEG2\nFRAME\n")
        with open(path, "wb") as f:
            f.write(header.encode("ascii"))
            f.write(yuv)

    def _still_render_mpv(self, frame, w: int, h: int, osd_text: str):
        """工作线程：单帧 Y4M → 隐藏实例 loadfile → window 截图 → PIL。

        帧精确由构造保证（无时间寻址）；Y4M=未标记 YUV → mpv 走与真实
        视频相同的色彩启发式；window 截图含上屏转换 → 与播放逐像素一致。
        任何环节失败返回 None，调用方回退 ffmpeg 化妆链。

        C+（2026-09-05）：两处固定等待换成事件驱动——loadfile 用
        file-loaded 事件替代 path 轮询+0.25s 干等；截图用内存直返的
        screenshot-raw（实测 3.7ms）替代落盘 PNG+文件轮询+PIL 读回
        （实测 81.6ms）。事件等待超时（5s）兜底回退老路径的行为不变。
        """
        p = self._still_mpv
        if p is None:
            return None
        yuv = self._pyav_frame_yuv420p_bytes(frame)
        if yuv is None:
            return None
        try:
            # resize 后等 mpv vo 重配完成（主线程记的稳定截止点）
            wait = self._still_mpv_settle_until - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            if self._still_mpv is not p:  # 期间被 teardown/重建
                return None
            import tempfile
            y4m = Path(tempfile.gettempdir()) / \
                f"arknight_still_{os.getpid()}.y4m"
            self._write_still_y4m(y4m, yuv, frame.width, frame.height)
            if osd_text:
                safe = (osd_text.replace("\\", "\\\\").replace("{", "\\{")
                        .replace("}", "\\}"))
                p.command("osd-overlay", 2, "ass-events",
                          "{\\an7}" + safe, 0, 0, 0)
            else:
                p.command("osd-overlay", 2, "none", "", 0, 0, 0)
            # 事件等待窗口内发 loadfile：先注册回调再发命令，免得
            # file-loaded 先于注册到达（python-mpv prepare_and_wait 语义）
            loadfile = getattr(p, "loadfile", None)
            if (callable(loadfile) or hasattr(p, "prepare_and_wait_for_event")):
                try:
                    with p.prepare_and_wait_for_event(
                            "file-loaded", timeout=5):
                        p.command("loadfile", str(y4m))
                except Exception:
                    # 事件路径不可用/超时：回退老的 path 轮询判定
                    p.command("loadfile", str(y4m))
                    end = time.time() + 5
                    while time.time() < end:
                        if (getattr(p, "path", None) or "").endswith(".y4m"):
                            break
                        time.sleep(0.03)
                    else:
                        return None
            else:
                p.command("loadfile", str(y4m))
                end = time.time() + 5
                while time.time() < end:
                    if (getattr(p, "path", None) or "").endswith(".y4m"):
                        break
                    time.sleep(0.03)
                else:
                    return None
            # 渲染仍需一拍（vo 上屏异步）：留 60ms 而非固定 250ms，
            # screenshot-raw 读的是当前呈现内容，不足时由位图尺寸
            # 校验+调用方追帧逻辑兜底
            time.sleep(0.06)
            if self._still_mpv is not p:  # teardown 竞态保护
                return None
            raw = None
            try:
                raw = p.command("screenshot-raw", "window")
            except Exception:
                raw = None
            if (isinstance(raw, dict) and raw.get("w") == w
                    and raw.get("h") == h and raw.get("data")):
                # bgr0（BGRA 字节序）→ RGB；stride 对齐按 w*4 校验
                stride = int(raw.get("stride") or (w * 4))
                if stride == w * 4:
                    buf = raw["data"]
                    if isinstance(buf, memoryview):
                        buf = buf.tobytes()
                    arr = np.frombuffer(buf, dtype=np.uint8)
                    if arr.size == h * stride:
                        arr = arr.reshape(h, w, 4)
                        img = PIL.Image.fromarray(
                            arr[:, :, :3][:, :, ::-1].copy(), "RGB")
                        return img
            # screenshot-raw 不可用/尺寸不符/stride 不齐：回退老落盘路径
            shot = Path(tempfile.gettempdir()) / \
                f"arknight_still_shot_{os.getpid()}.png"
            if shot.exists():
                shot.unlink()
            if self._still_mpv is not p:  # teardown 竞态保护
                return None
            p.command("screenshot-to-file", str(shot), "window")
            end = time.time() + 5
            last = -1
            while time.time() < end:
                if shot.is_file():
                    sz = shot.stat().st_size
                    if sz > 0 and sz == last:
                        break
                    last = sz
                time.sleep(0.03)
            else:
                return None
            img = PIL.Image.open(shot).convert("RGB")
            if img.size != (w, h):
                return None
            return img
        except Exception:
            return None

    def _decode_still_work(self, frame_idx: int, gen: int,
                           w: int, h: int, t: float, ffmpeg_path,
                           expected_bright: bool, osd_text: str) -> None:
        try:
            # 解码后端只负责出"对的那帧"（PyAV 计数定帧 → ffmpeg 两段式 →
            # OpenCV）；保比例+黑边+帧号的化妆统一走 ffmpeg CLI 一条链，
            # 与播放态 mpv OSD 同为 libass 渲染。子进程不可用时退回
            # 硬拉伸——宁可变形，不让暂停画面空白。
            ass_path = None
            if osd_text:
                ass_path = self._build_still_ass(w, h, osd_text)
            img = None
            if self._pyav_kf is not None:
                frame = self._still_decode_pyav(frame_idx, expected_bright)
                if frame is not None:
                    # 方案 C 主路：隐藏 mpv 自渲染（与播放同管线）；
                    # 失败回退 ffmpeg 化妆链（烧帧号走 ASS）
                    img = self._still_render_mpv(frame, w, h, osd_text)
                    if img is None:
                        img = self._burn_av_frame(frame, w, h, ass_path,
                                                  ffmpeg_path)
            if img is None:
                img = self._still_decode_ffmpeg(frame_idx, w, h, t, ffmpeg_path,
                                                expected_bright, ass_path)
            if img is None:
                native = self._still_decode_opencv(frame_idx)
                if native is not None:
                    # OpenCV 的 BGR→RGB 转换已按 601 发生，矩阵无法再修；
                    # 兜底路径接受这一已知偏差
                    img = (self._still_burn_ffmpeg(native.tobytes(),
                                                   native.size[0],
                                                   native.size[1],
                                                   "rgb24", w, h, ass_path,
                                                   ffmpeg_path)
                           or native.resize((w, h)))
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
                             t: float, ffmpeg_path, expected_bright: bool,
                             ass_path):
        try:
            ff = resolve_ffmpeg_path(ffmpeg_path)
            # 两段式精确取帧：输入 -ss 在本仓源上实测会落在目标前几帧
            # （解封装 seek 不精确，5409 落进前一段 PAUSE），所以退 0.5s
            # 粗寻址 + -copyts 保留原始时间戳 + select 按 PTS 截取目标帧。
            # 抓 2 帧：碰撞 tick 上两帧共用同一 pts，第一帧可能是前一段；
            # 用分析器语境（该帧应为暗/亮）挑对的那帧。
            back = max(0.0, float(t) - 0.5)
            chain = self._still_filter_chain(
                w, h, ass_path,
                select_expr=f"select=gte(t\\,{float(t):.6f}),",
                native_dims=self._video_dims,
                matrix=self._still_color_matrix())
            cmd = [str(ff), "-y", "-loglevel", "error", "-copyts",
                   "-ss", f"{back:.6f}", "-i", str(self.video_path),
                   "-vf", chain,
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
                # 黑边和烧字都在边缘：只量中央 50% 内容区，避免阈值被污染
                cw, ch = im.size
                crop = im.crop((cw // 4, ch // 4, cw * 3 // 4, ch * 3 // 4))
                return float(np.asarray(crop.convert("L")).mean())

            want = 62.0 if expected_bright else 40.0
            return min(imgs, key=lambda im: abs(luma(im) - want))
        except Exception:
            return None

    def _still_decode_opencv(self, frame_idx: int):
        try:
            cap = cv2.VideoCapture(str(self.video_path))
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ok, frame = cap.read()
            cap.release()
            if not ok or frame is None:
                return None
            # 原生分辨率返回；缩放/黑边/帧号统一走 ffmpeg 化妆链
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            return PIL.Image.fromarray(rgb)
        except Exception:
            return None

    def _blit_still(self, img, gray, frame_idx: int, gen: int) -> None:
        if gen != self._overlay_gen or self._closing or self.is_playing:
            return
        if self._still_overlay is None:
            self._still_overlay = tk.Frame(self.video_surface, bg="black")
            # bd/highlightthickness 必须钉 0：Label 默认 bd=2，会把位图内嵌 2px
            self._still_label = tk.Label(
                self._still_overlay, bg="black", bd=0, highlightthickness=0)
            # 居中不用 relx/rely+anchor=center：place 对奇数尺寸取整不对称
            # （round(m*0.5) - c//2 = +1），位图会下移 1px——默认窗口高度
            # 为奇数时"暂停画面下移 1px"的根因（4K 偶数高度不发作）。
            # 显式整数 floor 居中，与 mpv/化妆链的取整规则一致；
            # 窗口缩放期间由 Configure 重新居中（位图重出前的过渡态）。
            self._still_overlay.bind(
                "<Configure>", lambda _e: self._recenter_still_label(), add="+")
        # 帧号已按 mpv OSD 同款参数烧进位图，无需再叠 Tk 文本
        self._still_photo = PIL.ImageTk.PhotoImage(img)
        self._still_label.config(image=self._still_photo)
        self._recenter_still_label()
        # 覆盖层恒等于 video_surface 全幅，与位图尺寸无关
        self._still_overlay.place(x=0, y=0, relwidth=1, relheight=1)
        self._still_overlay.lift()
        # 自愈：位图与面板尺寸不符（启动竞态/漏收的 Configure）→ 重出一帧
        try:
            sw = self.video_surface.winfo_width()
            sh = self.video_surface.winfo_height()
            if (sw > 1 and sh > 1 and (sw, sh) != img.size
                    and self._still_resize_after is None):
                self._still_resize_after = self.after(
                    200, self._reblit_resized_still)
        except Exception:
            pass
        self._still_last_gray = gray
        self._still_frame_idx = frame_idx

    def _recenter_still_label(self) -> None:
        """显式整数居中静帧位图（floor，与 mpv/化妆链取整规则一致）。"""
        ov = self._still_overlay
        lbl = getattr(self, "_still_label", None)
        photo = self._still_photo
        if ov is None or lbl is None or photo is None:
            return
        try:
            ow, oh = ov.winfo_width(), ov.winfo_height()
            if ow <= 1 or oh <= 1:
                return
            lbl.place(x=(ow - photo.width()) // 2,
                      y=(oh - photo.height()) // 2)
        except Exception:
            pass

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
        if self._still_resize_after is not None:
            try:
                self.after_cancel(self._still_resize_after)
            except Exception:
                pass
            self._still_resize_after = None
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

    @staticmethod
    def _pyav_frame_yuv420p_bytes(frame):
        """零转换取出 yuv420p 紧密打包字节（YUV 三平面按行宽裁剪拼接）。

        颜色矩阵转换刻意留给化妆链（对齐 mpv 的启发式）；非 yuv420p
        源返回 None，调用方退回 rgb24 路径。与 _pyav_frame_hash64 同款
        零拷贝直读平面手法。
        """
        try:
            if getattr(frame.format, "name", None) != "yuv420p":
                return None
            w, h = frame.width, frame.height
            cw, ch = (w + 1) // 2, (h + 1) // 2
            parts = []
            for i, (pw, ph) in enumerate(((w, h), (cw, ch), (cw, ch))):
                p = frame.planes[i]
                buf = (ctypes.c_ubyte * p.buffer_size).from_address(p.buffer_ptr)
                a = np.frombuffer(buf, dtype=np.uint8).reshape(ph, p.line_size)
                parts.append(np.ascontiguousarray(a[:, :pw]).tobytes())
            return b"".join(parts)
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
        self._still_matrix = None  # 新视频要重新判定颜色矩阵
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

    def _still_decode_pyav(self, frame_idx: int, expected_bright: bool):
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
        # 返回原生帧（不做任何颜色/尺寸转换）：yuv→rgb 矩阵由化妆链
        # 按 mpv 启发式打标执行，若此处先转 rgb 矩阵就定型成 601 了
        return best

    def _seek(self, frame_idx: int, skip_trim: bool = False):
        self._play_ended_at_eof = False   # 用户主动定位 → 下次播放不回绕
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

        # 开关语义（review G 轮补漏）：取消「快速掠过代理」= 永不走 Tier2。
        # 光停止调度新构建不够——已装入引擎的 blob 必须同步清掉，否则
        # 下一次播放仍按代数命中旧代理（手检 e 项的预期是永远保底 3x）。
        if not p.get('skim_preview', True):
            clear_skim = getattr(self._io, "clear_skim_blob", None)
            if callable(clear_skim):
                try:
                    clear_skim()
                except Exception:
                    pass

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

        # mpv 原生引擎已在 time-pos 里实现业务变速段（与上游同语义），
        # 不再按「忽略业务加速」截流；cv 引擎本就不剥，mpv 现在也不剥。
        # 播放中跨段时 mpv 的 speed 属性在段边界切换，与 OpenCV 逐帧 step 对齐。
        native_engine = bool(getattr(self._io, "native_rendering", False))
        ignore_biz = False
        if self.total_frames <= 0:
            self._reject_play_request("当前源没有可播放帧")
            return False
        start = max(0, min(int(start), self.total_frames - 1))
        try:
            plan = self._build_timeline_plan()
            project_generation, timeline_revision = self._task_scope()
            # 片尾回绕（09-16 真机 T0）：播完后 current_frame_idx 停在末段最后
            # 呈现帧，再点播放会从「无可推之帧」起播=空流假播放（引擎挂牌
            # "播放中"但 tp/源帧永不推进，红条冻在末尾、时间戳错乱）。
            #
            # 精确条件（09-16 复查修正 v2）：**只有自然播完（引擎报 EOF）之后**
            # 再启动播放才算「重播」，回绕到 0。
            #
            # 演进：v1 用 `start >= 末段末帧`→ owner 实测「暂停后点播放，位置
            # 被强制拉回开头」（停在片尾附近即误触发）；v2 曾加 `not is_playing`
            # → 但 toggle_play 的播放分支**先**把 is_playing 置 True 再调本函数，
            # 该条件恒假 → 回绕永久失效（自相矛盾，已纠正）。
            # 现用 EOF 标志：暂停/恢复、手动 seek 到末尾都不回绕（用户意图是
            # 「继续/定位」），仅「播完 → 再播」回绕（用��意图是「重播」）。
            if (plan.kept_ranges
                    and getattr(self, "_play_ended_at_eof", False)):
                _k0, _k1 = plan.kept_ranges[-1]
                _last_presentable = max(_k0, _k1 - 1)
                if start >= _last_presentable:
                    start = 0
            self._play_ended_at_eof = False
            # 1c-② 解码直通分档：生效速率超 Tier1 cap(3x)、直通就绪、且
            # 「快速掠过」开关开启时，高速播放改走 dsvc 包级跳过活流
            # （原画质真倍速、零物化，mpv 恒 speed=1）；其余情形回落
            # Tier1 活 EDL，行为分毫不动。UI<1x 不走直通（抽帧只会加速，
            # 减速语义仍需源路径）。
            dthrough_spans = None
            gate_reason = ""
            if not native_engine:
                gate_reason = "非原生引擎"
            elif speed < 1.0:
                gate_reason = "UI<1x"
            elif not bool(self.skip_trimmed.get()):
                gate_reason = "跳过裁剪区关"
            elif not plan.deleted_ranges:
                gate_reason = "无删段"
            elif not p.get('skim_preview', True):
                gate_reason = "快速掠过开关关"
            elif not bool(getattr(self._io, "dthrough_ready", False)):
                gate_reason = "dthrough_ready=False"
            elif self.media_info is None:
                gate_reason = "media_info 缺失"
            else:
                try:
                    dthrough_spans = derive_spans(
                        plan.kept_ranges,
                        self._speed_segs_snap(),
                        speedup_1x=False if ignore_biz else bool(p['speedup_1x']),
                        speedup_02=False if ignore_biz else bool(p['speedup_02']),
                        factor_02=int(p['speedup_02_factor']),
                        ui_rate=speed,
                        fps=float(self._io.fps or 0.0),
                        rate_cap=_DTHROUGH_RATE_CAP,
                    )
                except (TypeError, ValueError) as exc:
                    gate_reason = f"derive_spans 异常: {exc}"
                    dthrough_spans = None
                # （09-13 午废除「UI≤3x 一律 Tier1」旧规：它立于碎段税时代，
                # 税已被小间隙连续解码根除；且 owner 目标=预览节奏与导出一致
                # ——1x 走直通后 0.2x 段 10×=真导出节奏，Tier1 旧语义封顶 3×
                # 恰是「节奏不一致」的最后一环。1x 解码账 600fps≪2029。
                # 现仅 <1x 走 Tier1——外层已挡：抽帧只会加速，减速语义需源路径。）
                if dthrough_spans is None and not gate_reason:
                    gate_reason = "spans 为空"
            if dthrough_spans is None:
                # 观测（09-13 晨 owner 请求全程监测）：分档决策+原因落 stdout
                if speed > 3.0:
                    print(f"[dthrough] 未接管（{gate_reason}）→ Tier1+skim 兜底",
                          flush=True)
                    # Tier2 兜底触发：仅限 >3x 且直通未接管（dsvc 不可用/意外失格
                    # /未跳裁剪）——那才是 Tier1 需要代理补位的场景。≤3x Tier1 活
                    # EDL 原生够用，不烧 5 分钟构建+294MB（09-13 晨 owner 实测
                    # 高占用 T0：1x 手检也会白建一次）。直通档接管时同样跳过
                    # ——skim 已退役休眠，只在直通失格的高速场景续命。
                    self._maybe_schedule_skim_build()
                else:
                    print(f"[dthrough] 未接管（{gate_reason}）→ Tier1（不建代理）",
                          flush=True)
            accepted = False
            if dthrough_spans is not None:
                print(f"[dthrough] 接管 spans={len(dthrough_spans)} "
                      f"rate=[{min(s.rate for s in dthrough_spans)}~"
                      f"{max(s.rate for s in dthrough_spans)}] start={start}",
                      flush=True)
                try:
                    accepted = bool(self._io.play_dthrough(
                        CertifiedEdlRequest(
                            media_info=self.media_info,
                            timeline_plan=plan,
                            project_generation=project_generation,
                            timeline_revision=timeline_revision,
                        ),
                        spans=dthrough_spans,
                        fps=float(self._io.fps or 60.0),
                        start_source_frame=start,
                    ))
                    if accepted:
                        # 直通接管=原画质 rawpush，skim 代理永无用武之地；
                        # 清掉已装 blob（294MB 级内存）并杜绝代理路径混淆
                        clear_skim = getattr(self._io, "clear_skim_blob", None)
                        if callable(clear_skim):
                            try:
                                clear_skim()
                            except Exception:
                                pass
                except PreviewEngineError:
                    dthrough_spans = None  # 直通意外失格：回落 Tier1 原路径
                    print("[dthrough] play_dthrough 异常 → Tier1 回落", flush=True)
            if not accepted and dthrough_spans is None:
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

    def _on_fps_osd_change(self):
        """屏显帧率开关：关闭时立即清掉右上角持久 OSD（开着则由播放心跳刷新）。"""
        shower = getattr(self._io, "show_osd_corner_text", None)
        if not self.show_fps_osd_var.get() and callable(shower):
            try:
                shower("")
            except Exception:
                pass

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
        self._play_ended_at_eof = True   # 标记「已自然播完」供下次播放判重播
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
                # enforce_media_certification guarantees a complete MediaInfo
                # at this point (checked above), i.e. the certified PTS route
                # whose batched audio has no segment-count ceiling.
                pts_certified=enforce_media_certification,
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
            # 原生步进松手时引擎已停在最后一拍的目标帧，再寻址只会
            # 触发慢速贴帧；旧路径仍需末拍寻址保证落点
            if not self._native_step_enabled():
                self._do_preview_seek()
        self._key_hold_fired = False

    def _on_key_space(self, event):
        if self._closing:
            return "break"
        focused = self.focus_get()
        if isinstance(focused, (ttk.Entry, tk.Entry, ttk.Combobox)): return
        # P1-E（09-16 真机 T0）：点过画面后键盘焦点可能仍在原生 mpv 子窗口
        # （它只吞「第一次」按键，渲染循环要连续两拍才拉回）——表现为空格
        # 「有时不生效」。此处即时抢一次焦点，让本次按键生效。
        if bool(getattr(self._io, "native_rendering", False)):
            try:
                self._focus_reclaim_streak = 2  # 直通：本次即视为连续两拍
                self._reclaim_focus_from_native_child()
            except Exception:
                pass
        self.toggle_play();
        return 'break'

    def _start_repeat(self, direction: str):
        if self._closing:
            return
        self._key_hold_fired = True
        # 原生步进每拍都直达引擎，无需周期预览寻址（寻址反而触发慢速
        # C4 贴帧覆盖层，盖住原生帧）
        if not self._native_step_enabled():
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
        # 原生引擎暂停中：mpv 直发毫秒级位级精确帧步进（探针D T0），
        # 替代慢速 seek+C4 贴帧；seek 标志只在旧路径上有意义。
        if self._native_step_enabled() and self._native_step(delta):
            return
        if seek: self._seek(new_idx, skip_trim=False)

    def _native_step_enabled(self) -> bool:
        """原生步进可用：原生引擎 + 暂停中 + 引擎暴露 step_frame。"""
        io = getattr(self, "_io", None)
        return (
            io is not None
            and not self.is_playing
            and not self._closing
            and bool(getattr(io, "native_rendering", False))
            and callable(getattr(io, "step_frame", None))
        )

    def _native_step(self, delta: int) -> bool:
        """mpv 原生帧步进（frame-step/frame-back-step，VERY_EXACT 路径）。

        主窗直接出帧：已有静帧覆盖层立即变陈旧必须撤下，并把静帧收敛
        标记对齐到当前帧号，否则渲染循环的追帧逻辑会重新贴 C4 覆盖层
        盖住原生帧。失败返回 False，由调用方回退旧 seek+贴帧路径。
        """
        try:
            stepped = bool(self._io.step_frame(+1 if delta > 0 else -1))
        except Exception:
            return False
        if not stepped:
            return False
        # 用户步进取代飞行中的验证定位（引擎侧已静默作废验证任务）
        self._verify_pending_frame = None
        try:
            self._auto_rate_clear()
        except Exception:
            pass
        if self._fps_samples:
            self._fps_samples.clear()
        self._drop_still_overlay()
        self._still_frame_idx = self.current_frame_idx
        self._osd_pending = True
        return True

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

        # apply ignore flag for this run（勾选框已随 UI 精简删除，
        # 引擎读取点固定为 False；此处只保留 calib 流程自身的状态记录）
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
        self._auto_rate_ignore = False

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
        ignore = False
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

    def _on_timeline_pause_select(self, seg_id: int | None):
        # None=清选（P1-C：裸单击已选段 → 清选并跳转，不再吞掉点击）
        if seg_id is None:
            try:
                self.settings.set_selected_pause(None, "")
            except Exception:
                pass
            return
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

    def _note_fps_sample(self, time_pos, drops, perf=None) -> None:
        """喂采样并节流刷新右上角播放帧率 OSD。

        口径：媒体时间推进速率 × 源帧率 = 实际内容帧率。用 time-pos（EDL
        里也连续、不会因跳过删除段而大跳）而不是源帧号，所以不会被 EDL
        跳变污染成 129/175 那种虚高值。上限钳到 源帧率×当前倍速（mpv 的
        2x/4x 是 playback_rate 原生变速，内容帧率随之放大到 ~120/~240），
        超出上限必是测量噪声。暂停 / 寻址瞬间清窗重新累积。

        档位标签（P1-RAM）：「掠过Nx」=Tier2 内存代理播放中、N=引擎当前
        实际速率（0.2x 区域加速生效时会看到它跳到 10x，出段回 1x）；
        「保底Nx」=Tier1 活 EDL（封顶 3x）；「源」=完整源文件；
        「直通Nx」=解码直通档（1c：dsvc 活流，N=台账实测档位抽帧率=
        该段生效倍速，mpv 恒 speed=1、呈现恒 60fps 虚拟轴）。回答
        「怎么判断业务加速真的生效」——直接看这个数字随段切换。
        """
        now = time.monotonic()
        if isinstance(time_pos, (int, float)) and self.is_playing:
            self._fps_samples.append((now, float(time_pos), drops if isinstance(drops, int) else None))
        else:
            if self._fps_samples:
                self._fps_samples.clear()
        shower = getattr(self._io, "show_osd_corner_text", None)
        if not callable(shower) or not bool(self.show_fps_osd_var.get()):
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
        tag = None
        if isinstance(perf, dict):
            mode = str(perf.get("mode") or "")
            speed = perf.get("speed")
            try:
                speed_text = f"{float(speed):.1f}x" if speed is not None else ""
            except (TypeError, ValueError):
                speed_text = ""
            if mode == "dthrough":
                tag = f"直通{speed_text}"
            elif mode == "skim":
                tag = f"掠过{speed_text}"
            elif mode == "edl":
                tag = f"保底{speed_text}"
            elif mode == "source":
                tag = f"源{speed_text}" if speed_text else "源"
        if tag:
            text = f"{tag} | {text}" if text else f"{tag} | … FPS"
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

        换载窗口的红条闪 0 由**引擎侧**解决（`snapshot_perf` 在未读回时
        报 `source_frame=None`，见 `_ensure_loaded_locked`），此处保持
        「播放中一律采信」的既有契约不变。
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
        self._note_fps_sample(perf.get("time_pos"), perf.get("mpv_frame_drops"),
                              perf=perf)

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
            # 追帧：节流跳过的寻址在这里补齐——暂停态覆盖层始终收敛到当前帧号。
            # 闭环验证飞行中抑制：落点由验证环负责（成功=原生帧，失败=事件
            # 驱动回退 C4），此处抢跑会让慢速贴帧盖住即将到来的原生精确帧。
            if (not self.is_playing and not self._still_decoding
                    and self._still_pending is None
                    and self._verify_pending_frame is None
                    and getattr(self, "_still_frame_idx", None) != self.current_frame_idx):
                self._show_paused_still(self.current_frame_idx)
        if self._io is not None:
            events = ()
            try:
                events = self._io.poll_events()
            except Exception:
                pass
            for event in events:
                if event.get("event") == "seek-verified":
                    self._on_seek_verified(event)
            # 看门狗：引擎结果事件缺失（理论不该发生，poll 的超时判定
            # 应已出队 fail 事件）——过截止按失败处理，回退 C4 兜底
            if (self._verify_pending_frame is not None
                    and time.monotonic() > self._verify_pending_until):
                frame = self._verify_pending_frame
                self._verify_pending_frame = None
                self._show_paused_still(frame)
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
            ignore_biz = False
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

        # 模板身份冻结进快照（09-16 复查）：`make_key` 默认会**现算**模板指纹，
        # 若 store 与 load 各自重算，两次之间模板文件被改动就会得到不同的键
        # （存入的键对应"改动后"的模板，语义错位且白算两遍 9.6MB 的 sha256）。
        # 在分析请求发起时算一次、冻结进快照，两边共用同一值。
        try:
            import analysis_cache as _ac
            snapshot["templates_fingerprint"] = _ac.templates_fingerprint()
        except Exception as exc:
            print(f"[cache] 模板指纹不可用（将用 unknown）: {exc}", flush=True)

        # 保存本次分析参数快照（缓存键来源）；并尝试命中缓存
        self._last_analysis_snapshot = dict(snapshot)

        # 媒体探测在飞（源 sha256 未就绪）→ 挂起本次请求，探测落定后续跑。
        # 09-23 修复：原实现在 media_info 缺失时静默跳过缓存查询，导入后
        # 立即点击会白跑数分钟完整分析（owner 实测：本可秒开却冷跑 ~6min）。
        if (
            getattr(self, "media_info", None) is None
            and getattr(self, "_media_info_handle", None) is not None
        ):
            self._pending_analysis_source = str(self.video_path)
            self.btn_analyze.config(state=tk.DISABLED, text="等待媒体探测…")
            print(f"[cache] 媒体探测未就绪：挂起分析请求，探测落定后自动续跑 "
                  f"({self.video_path})", flush=True)
            return

        self.btn_analyze.config(state=tk.DISABLED, text="Analyzing...")
        if self._try_load_analysis_cache(snapshot):
            return

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
            # 模板缺失警告统一在 _finish_analysis 里弹（缓存命中路径同样要弹）
            self._finish_analysis(
                states,
                diffs,
                pauses,
                speeds,
                backend_label=label,
                context_used=used_context,
                templates_missing=missing_templates,
            )

        def on_error(exc):
            if self._closing:
                return
            # 取证：弹窗之外把完整堆栈落控制台+文件——Errno 22 现场只给
            # 了一行消息，拿不到崩溃位置（2026-09-08 教训）。
            import traceback as _traceback
            tb_text = "".join(_traceback.format_exception(
                type(exc), exc, exc.__traceback__))
            print(f"[analyze] FAILED {video_path}\n{tb_text}", flush=True)
            try:
                log_dir = Path(".cache")
                log_dir.mkdir(exist_ok=True)
                with open(log_dir / "analysis_error.log", "a",
                          encoding="utf-8") as fh:
                    fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} "
                             f"{video_path} backend={backend_label}\n{tb_text}")
            except Exception:
                pass
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

    # ------------------------------------------------------------------
    #  分析结果磁盘缓存（09-16）：键=源sha×参数×后端×ffmpeg×格式版本
    # ------------------------------------------------------------------

    def _analysis_cache_key(self) -> "object | None":
        """当前分析请求的缓存键（media_info/snapshot 缺件时返回 None）。"""
        try:
            import analysis_cache as ac
            media = getattr(self, "media_info", None)
            snap = getattr(self, "_last_analysis_snapshot", None)
            if media is None or not snap:
                return None
            return ac.make_key(media, snap)
        except Exception:
            return None

    def _store_analysis_cache(self, states, diffs, pauses, speeds,
                              backend_label: str,
                              context_used: bool = True,
                              templates_missing: bool = False) -> None:
        """分析完成后落盘。失败**如实上报**（不再谎报"已缓存"）。

        缓存仍是纯加速层：写失败不影响分析结果，但必须留痕——09-16 教训
        （v1 无条件打印"已缓存"，把"真实产物从未写成功"掩盖了整轮）。
        `context_used`/`templates_missing` 入档：命中时提示框复述当时路径。
        """
        key = self._analysis_cache_key()
        if key is None:
            print("[cache] 跳过写入：缺少 media_info/参数快照（无缓存键）",
                  flush=True)
            return
        try:
            import analysis_cache as ac
            media = getattr(self, "media_info", None)
            written = ac.AnalysisCache().store(
                key, states=states, diffs=diffs, pauses=pauses, speeds=speeds,
                backend_label=backend_label,
                source_size=int(getattr(media, "source_size", 0) or 0),
                source_mtime_ns=int(getattr(media, "source_mtime_ns", 0) or 0),
                context_used=bool(context_used),
                templates_missing=bool(templates_missing),
            )
            try:
                size_mb = written.stat().st_size / 1e6
            except OSError:
                size_mb = 0.0
            print(f"[cache] 分析结果已缓存 key={key.digest()[:12]} "
                  f"（{size_mb:.1f} MB → {written.name}）", flush=True)
        except Exception as exc:
            code = getattr(exc, "code", type(exc).__name__)
            print(f"[cache] 写入失败（分析结果不受影响）: {code}: {exc}",
                  flush=True)

    def _maybe_resume_pending_analysis(self) -> None:
        """媒体探测落定后，续跑被挂起的分析请求（09-23 时序修复）。

        只在「同一源 + 仍挂起 + 未关闭」时续跑；源身份已就绪，故此时
        缓存查询可正常进行（命中则秒开，未命中才冷跑）。
        """
        source = getattr(self, "_pending_analysis_source", None)
        if source is None or self._closing:
            return
        self._pending_analysis_source = None
        if not self.video_path or str(self.video_path) != source:
            # 挂起期间换了源：请求作废（新源的分析由用户重新发起）
            return
        if getattr(self, "media_info", None) is None:
            return
        # 复位按钮：若续跑在守卫处提前返回（如导出进行中），按钮不能停在
        # "等待媒体探测…"；正常续跑会在下一步立即改写为 "Analyzing..."。
        try:
            self.btn_analyze.config(state=tk.NORMAL, text="自动模板分析")
        except Exception:
            pass
        print(f"[cache] 媒体探测就绪：续跑挂起的分析请求 ({source})", flush=True)
        self._start_analysis()

    def _cancel_pending_analysis(self, reason: str) -> bool:
        """作废挂起的分析请求并复位按钮；返回是否确有挂起请求。"""
        source = getattr(self, "_pending_analysis_source", None)
        if source is None:
            return False
        self._pending_analysis_source = None
        print(f"[cache] 挂起的分析请求作废（{reason}）", flush=True)
        try:
            self.btn_analyze.config(state=tk.NORMAL, text="自动模板分析")
        except Exception:
            pass
        return True

    def _try_load_analysis_cache(self, snapshot: dict) -> bool:
        """尝试载入缓存；命中则直接完成分析流程。返回是否命中。

        交互（调研结论 + 主会话 review 修正）：默认复用，但**首次提示一次**
        ——分析结果直接决定剪辑决策，用户应知情；此后不再打扰。
        """
        try:
            import analysis_cache as ac
            media = getattr(self, "media_info", None)
            if media is None:
                # 09-23 取证补强：此处曾静默 return，导致"为什么没弹缓存"
                # 只能靠时间线反推。留痕，便于下次一眼定位。
                print("[cache] 跳过缓存查询：源身份未就绪（media_info 缺失）",
                      flush=True)
                return False
            cache = ac.AnalysisCache()
            cache.cleanup_orphans()
            key = ac.make_key(media, snapshot)
            got = cache.load(key)            # 只读一次（实测 3.8ms@2.1MB）
            if got is None:
                print(f"[cache] 未命中（键={key.digest()[:12]}），走完整分析",
                      flush=True)
                return False
            meta = got.get("meta") or {}
            _pauses = got.get("pauses")
            _speeds = got.get("speeds")
            # 口径统一（09-16 修复）：提示框的 MB 曾用 len(states)=帧数，
            # 与 describe() 的文件字节口径打架；一律取真实文件大小。
            _size_mb = round(int(got.get("file_bytes") or 0) / 1e6, 1)
            info = {
                "created_wall": meta.get("created_wall"),
                "backend_label": meta.get("backend_label"),
                "pause_n": len(_pauses) if _pauses is not None else 0,
                "speed_n": len(_speeds) if _speeds is not None else 0,
                "mb": _size_mb,
            }
            # 用户已拒绝过这个键（要亲手重算）→ 直接走完整分析，不打扰
            if getattr(self, "_analysis_cache_declined_key", None) == key.digest():
                return False
            if not self._analysis_cache_prompted:
                from tkinter import messagebox
                created = info.get("created_wall") or "未知时间"
                mb = info.get("mb")
                ans = messagebox.askyesno(
                    "发现分析缓存",
                    f"该视频的分析结果已缓存（{created}，{mb} MB，"
                    f"{info.get('pause_n', 0)} 个暂停段）。"
                    "载入缓存可立即使用（<1 秒）；选「否」将重新完整分析"
                    "（约数分钟）。",
                    default="yes",
                )
                if not ans:
                    # 拒绝 = "我要亲手重算"：记住该键，此后不再询问、也不
                    # 自动载入（09-16 review：原实现把 prompted 置位在问答
                    # 之前，拒绝后再次点分析会静默吃缓存，用户选择被推翻）。
                    # 一旦重算出新结果（_finish_analysis 非缓存路径），该记录
                    # 由 _finish_analysis 清除——届时缓存内容=用户刚算的那份。
                    self._analysis_cache_declined_key = key.digest()
                    return False
                self._analysis_cache_prompted = True
            self._finish_analysis(
                got["states"], got["diffs"], got["pauses"], got["speeds"],
                backend_label=str(info.get("backend_label")
                                  or snapshot.get("backend_label") or "缓存"),
                # 复述**当时**的真实分析路径（写死 True 会让提示框谎称
                # "已跳过二次扫片"，与原分析不符）
                context_used=bool(meta.get("context_used", True)),
                templates_missing=bool(meta.get("templates_missing", False)),
                # 来自缓存 → 跳过回写（显式参数，不用实例状态：实例状态在
                # 异常/中断路径会残留，导致下一次真实分析的结果永不落盘）
                from_cache=True,
            )
            print(f"[cache] 已从缓存载入 key={key.digest()[:12]} "
                  f"（{info.get('mb')} MB）", flush=True)
            return True
        except Exception as exc:
            print(f"[cache] 载入失败（回落完整分析）: {type(exc).__name__}: {exc}",
                  flush=True)
            return False

    def _finish_analysis(
        self,
        states,
        diffs,
        pauses,
        speeds,
        backend_label: str = "OpenCV",
        context_used: bool = False,
        templates_missing: bool = False,
        from_cache: bool = False,
    ):
        from tkinter import messagebox
        # 模板缺失警告（原在 on_success；移入此处以便缓存命中路径同样提示，
        # 顺序保持"警告先于完成信息"）
        if templates_missing:
            messagebox.showwarning(
                "Templates missing",
                "No usable templates were found; all frames will be treated as normal.",
            )
        # 分析产物落盘（09-16 新增）：4min → 下次秒级载入。失败只打日志，
        # 绝不影响分析结果本身（缓存是纯加速层）——但日志必须如实
        # （v1 曾无条件打印"已缓存"，掩盖了"真实产物从未写成功"）。
        # from_cache=True（本次结果来自缓存）→ 跳过回写：同内容重复落盘纯属
        # 浪费且会刷新 mtime 干扰 LRU。**用显式参数而非实例状态**：09-16
        # review 发现实例标志会在异常/中断路径残留 True，令下一次真实分析的
        # 结果永不落盘（P1，同属"静默不写"）。
        if not from_cache:
            self._store_analysis_cache(states, diffs, pauses, speeds,
                                       backend_label,
                                       context_used=context_used,
                                       templates_missing=templates_missing)
            # 刚算完的这份已落盘（内容=用户亲手要的）→ 解除"拒绝"记录，
            # 之后再点分析=复用刚算的结果，符合用户预期。
            self._analysis_cache_declined_key = None
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
        # （09-13 移除 Tier2 预热：分析落定就建代理=直通档时代纯浪费；
        # 代理只在 >3x 播放且直通失格时按需懒构建。）
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
