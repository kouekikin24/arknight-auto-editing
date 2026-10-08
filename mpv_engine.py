"""Preview-only libmpv engine.

The engine is intentionally independent from the export path.  It renders to
an already-created Tk child window when a WID is supplied and keeps all mpv
callbacks off the Tk thread by publishing bounded event records.
"""
from __future__ import annotations

from fractions import Fraction
from bisect import bisect_left
import ctypes
from ctypes import (CFUNCTYPE, POINTER, Structure, byref, c_char_p, c_int,
                    c_int64, c_uint64, c_void_p)
import math
import os
from pathlib import Path
from queue import Empty, Full, Queue
import re
import threading
import time
from typing import Any, Callable, Sequence

from certified_edl import CertifiedEdl, build_certified_edl
from dthrough_ledger import DThroughLedger, LedgerSpan, coerce_span
from dthrough_service import DThroughService, DThroughServiceError
from frame_types import FRAME_TYPE_0_2X, FRAME_TYPE_1X
from media_info import MediaInfo, MediaInfoError
from preview_engine import (
    CertifiedEdlRequest,
    PreviewEngineError,
    PreviewPlayRequest,
    SourceSeekRequest,
    normalized_source_path,
)
from pts_timeline import CertifiedPtsTimeline
from timeline_plan import TimelinePlan


def canonical_load_target(value: Any) -> str:
    """加载目标的进程内比较标识。

    带 scheme 的 URL（P1-RAM 的 ``ram://skim<id>``）原样保留——
    ``Path.resolve()`` 会把它揉成文件系统路径，导致 file-loaded 事件
    的路径校验永远失配、pending seek/resume 卡死；文件系统路径仍走
    ``normalized_source_path``（与所有源绑定检查同一语义）。
    """
    text = str(value)
    if "://" in text:
        return text
    return str(normalized_source_path(text))


# ----------------------------------------------------------------------
# P1-RAM skim：libmpv client API ``mpv_stream_cb_add_ro`` 的 ctypes 绑定
# （字段序对齐 mpv/stream_cb.h；T0 实证 probe_skim_ram_streamcb rc=0、
# 10x 有效 9.11x、OSD 8/8=60、tp_end≡EDL 虚拟时长）。
# 回调运行在 mpv 解复用线程：只读 blob 快照与进程内存，禁止回调任何
# mpv API、禁止拿引擎锁（死锁守卫）。
# ----------------------------------------------------------------------
_SKIM_READ_FN = CFUNCTYPE(c_int64, c_void_p, c_void_p, c_uint64)
_SKIM_SEEK_FN = CFUNCTYPE(c_int64, c_void_p, c_int64)
_SKIM_SIZE_FN = CFUNCTYPE(c_int64, c_void_p)
_SKIM_CLOSE_FN = CFUNCTYPE(None, c_void_p)
_SKIM_CANCEL_FN = CFUNCTYPE(None, c_void_p)


class _SkimStreamCbInfo(Structure):
    _fields_ = [
        ("cookie", c_void_p),
        ("read_func", _SKIM_READ_FN),
        ("seek_func", _SKIM_SEEK_FN),
        ("size_func", _SKIM_SIZE_FN),
        ("close_func", _SKIM_CLOSE_FN),
        ("cancel_cb", _SKIM_CANCEL_FN),
    ]


_SKIM_OPEN_FN = CFUNCTYPE(c_int, c_void_p, c_char_p, POINTER(_SkimStreamCbInfo))


class _SkimStreamState(Structure):
    _fields_ = [("pos", c_int64)]


# cookie=id(engine) → 强引用注册表：对象存活期间 id 不复用；close() 摘除。
_SKIM_ENGINES: dict[int, Any] = {}


def _skim_open_callback(user_data, uri, info):
    engine = _SKIM_ENGINES.get(int(user_data or 0))
    blob = getattr(engine, "_skim_blob", None) if engine is not None else None
    if blob is None:
        return -1  # MPV_ERROR_GENERIC：无 blob 时加载失败，调用侧回落
    # open 时快照 blob 引用：换代后已打开的流继续读旧快照（引用保活，
    # 旧 blob 在流关闭后由 GC 释放）；新 blob 随下一次 loadfile 生效
    # （URL 带自增代号强制 _ensure_loaded 重载）。
    state = _SkimStreamState(0)
    blob_len = len(blob)

    def read_fn(cookie, buf, nbytes):
        contents = ctypes.cast(cookie, POINTER(_SkimStreamState)).contents
        position = contents.pos
        count = min(int(nbytes), blob_len - position)
        if count <= 0:
            return 0
        ctypes.memmove(buf, (ctypes.c_char * count).from_buffer(blob, position),
                       count)
        contents.pos = position + count
        return count

    def seek_fn(cookie, offset):
        contents = ctypes.cast(cookie, POINTER(_SkimStreamState)).contents
        if offset < 0 or offset > blob_len:
            return -1
        contents.pos = int(offset)
        return contents.pos

    def size_fn(_cookie):
        return blob_len

    def close_fn(_cookie):
        pass

    def cancel_fn(_cookie):
        pass

    functions = (
        _SKIM_READ_FN(read_fn),
        _SKIM_SEEK_FN(seek_fn),
        _SKIM_SIZE_FN(size_fn),
        _SKIM_CLOSE_FN(close_fn),
        _SKIM_CANCEL_FN(cancel_fn),
    )
    info.contents.cookie = ctypes.cast(byref(state), c_void_p)
    info.contents.read_func = functions[0]
    info.contents.seek_func = functions[1]
    info.contents.size_func = functions[2]
    info.contents.close_func = functions[3]
    info.contents.cancel_cb = functions[4]
    if engine is not None:
        # ctypes 包装对象必须保活到流关闭；list.append 在 GIL 下原子，
        # 不需要（也不允许）拿引擎锁。
        engine._skim_keepalive.append([state, *functions])
    return 0


_SKIM_OPEN_CALLBACK = _SKIM_OPEN_FN(_skim_open_callback)


# ---------------------------------------------------------------------------
# 解码直通档（一期 1b）：rawpush:// 活流 stream_cb
# 与 ram:// skim 的差别：blob=静态字节序列；rawpush=dsvc 子进程实时产出的
# CFR rawvideo 流。read 回调必须阻塞式等待（官方语义：无数据不得返 0）；
# EOF=read 返 0 → end-file(EOF)。seek=流内帧级字节寻址（帧号×帧大小），
# 由 seek_fn 翻译成 dsvc 的虚拟帧重定位。
# ---------------------------------------------------------------------------
_DTHROUGH_PROTOCOL = "rawpush"  # 纯 scheme 名（注册名不含 ://；URL=rawpush://dt<N>）
_DTHROUGH_ENGINES: dict[int, Any] = {}


def _dthrough_open_callback(user_data, uri, info):
    engine = _DTHROUGH_ENGINES.get(int(user_data or 0))
    svc = getattr(engine, "_dthrough_svc", None) if engine is not None else None
    if svc is None:
        return -1
    if engine is not None:
        engine._dthrough_opens += 1  # 诊断：demuxer 是否真的来开流
    state = _SkimStreamState(0)
    cancelled = {"flag": False}

    def read_fn(cookie, buf, nbytes):
        want = int(nbytes)
        cancelled["flag"] = False  # cancel 只作用于当次读操作
        while True:
            if engine is not None:
                engine._dthrough_reads += 1
            c = svc.pop_chunk(min(want, 1 << 20), 0.05)
            if c is None:
                if cancelled["flag"]:
                    return -1  # 被 seek/中断打断：返错误码（返 0=EOF 会杀死流）
                continue  # 暂无数据：阻塞式等待，绝不能返 0（那是 EOF）
            if c == b"":
                return 0  # 本代流推完 → end-file(EOF)
            ctypes.memmove(buf, c, len(c))
            return len(c)

    def seek_fn(cookie, offset):
        contents = ctypes.cast(cookie, POINTER(_SkimStreamState)).contents
        ctx = getattr(engine, "_dthrough_ctx", None)
        if ctx is None or offset < 0:
            return -1
        frame = int(offset) // ctx["frame_size"]
        contents.pos = int(offset)
        if offset == 0 and frame == 0:
            return 0  # open 时的 seek(0) 探测：流本来就从 0 起，免重定位
        try:
            svc.flush_chunks()
            svc.seek(frame, ctx["src"], ctx["spans"], ctx["fps"])
        except DThroughServiceError:
            return -1
        return contents.pos

    def size_fn(_cookie):
        return -1  # 活流无固定大小；seekable 由 seek_fn(0)>=0 表达

    def close_fn(_cookie):
        pass

    def cancel_fn(_cookie):
        cancelled["flag"] = True

    functions = (
        _SKIM_READ_FN(read_fn),
        _SKIM_SEEK_FN(seek_fn),
        _SKIM_SIZE_FN(size_fn),
        _SKIM_CLOSE_FN(close_fn),
        _SKIM_CANCEL_FN(cancel_fn),
    )
    info.contents.cookie = ctypes.cast(byref(state), c_void_p)
    info.contents.read_func = functions[0]
    info.contents.seek_func = functions[1]
    info.contents.size_func = functions[2]
    info.contents.close_func = functions[3]
    info.contents.cancel_cb = functions[4]
    if engine is not None:
        engine._dthrough_keepalive.append([state, *functions])
    return 0


_DTHROUGH_OPEN_CALLBACK = _SKIM_OPEN_FN(_dthrough_open_callback)


def _fraction_seconds(value: Fraction) -> str:
    from decimal import Decimal, localcontext

    with localcontext() as context:
        context.prec = 50
        rendered = format(
            Decimal(value.numerator) / Decimal(value.denominator),
            "f",
        )
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


class MpvEngine:
    """A source/EDL preview engine with injectable python-mpv binding."""

    engine_name = "mpv"
    native_rendering = True

    @property
    def mode(self) -> str:
        with self._lock:
            return self._mode

    def __init__(
        self,
        path: str,
        *,
        wid: int | None = None,
        fps: float = 0.0,
        total: int = 0,
        dll_dir: str | os.PathLike[str] | None = None,
        edl_dir: str | os.PathLike[str] | None = None,
        mpv_factory: Callable[..., Any] | None = None,
        event_queue_size: int = 256,
    ) -> None:
        if event_queue_size < 1:
            raise ValueError("event_queue_size must be positive")
        self.path = str(normalized_source_path(path))
        self.wid = int(wid) if wid is not None else None
        self.fps = float(fps or 0.0)
        self.total = int(total or 0)
        if self.fps < 0 or not math.isfinite(self.fps):
            raise ValueError("fps must be finite and non-negative")
        if self.total < 0:
            raise ValueError("total must be non-negative")
        self.frame_queue = None
        self._dll_dir = (
            normalized_source_path(dll_dir)
            if dll_dir is not None
            else None
        )
        self._edl_dir = (
            normalized_source_path(edl_dir)
            if edl_dir is not None
            else Path(self.path).parent / ".arknight-preview-edl"
        )
        self._factory = mpv_factory
        self._dll_handle: Any | None = None
        self._player: Any | None = None
        self._event_callback: Callable[[Any], None] | None = None
        self._property_callbacks: list[tuple[str, Callable[..., None]]] = []
        self._events: Queue[dict[str, Any]] = Queue(maxsize=event_queue_size)
        self._event_drop_count = 0
        self._lock = threading.RLock()
        self._closed = False
        self._started = False
        self._terminate_thread: threading.Thread | None = None
        self._terminate_done: threading.Event | None = None
        self._terminate_error: BaseException | None = None
        self._callbacks_detached = False
        self._mode = "source"
        self._load_generation = 0
        self._project_generation = 0
        self._timeline_revision = 0
        self._media_info: MediaInfo | None = None
        # A cheap source identity snapshot is used on every exact seek.  Full
        # SHA-256 validation happens when certification is bound and when an
        # EDL is built; hashing a multi-gigabyte source for every arrow-key
        # seek would make the editing surface unusable.
        self._source_stat: tuple[int, int] | None = None
        self._timeline: CertifiedPtsTimeline | None = None
        self._timeline_times: tuple[Fraction, ...] = ()
        self._timeline_clean_start = 0
        self._edl: CertifiedEdl | None = None
        self._edl_cache_key: tuple[Any, ...] | None = None
        self._skip_next_advance = False
        self._last_time_pos: float | None = None
        self._duration = 0.0
        self._source_frame = 0
        self._playing = False
        self._loaded_ready = False
        self._current_path: str | None = None
        self._pending_seek: tuple[Fraction, bool] | None = None
        self._pending_resume = False
        self._pending_speed: float | None = None
        # 选项2 闭环验证定位：飞行中的验证任务（seek_source_verified）与
        # 重复 PTS 段名册（帧→段首帧号，-1=唯一PTS，绑定时构建）。
        self._verify: dict[str, Any] | None = None
        self._dup_run: tuple[int, ...] = ()
        # 业务变速段（上游 VideoIOThread.speed_step 语义）：收到 play 请求
        # 时把段表与开关钉进 _biz_speed，每次 time-pos 更新判定当前段并
        # 用 mpv speed 属性做边界切换，与 OpenCV 引擎的逐帧 step 加速对齐。
        self._biz_speed: tuple[tuple[tuple[int, int, int], ...], bool, bool, int] | None = None
        self._biz_base_rate = 1.0
        self._biz_applied_step = 0  # 首拍必钉一次基准速率，避免换载间旧速残留
        # 速率封顶（上游 _preview_step_plan 的 min(raw, cap) parity）：
        # Tier1 活 EDL/source = request.preview_step_cap（UI×业务乘积同封）；
        # Tier2 skim = _SKIM_SPEED_CAP（T0：blob 上 14.3x 软解饱和、OSD 崩，
        # 12 为守卫上限）；None = 内部路径不封顶。
        self._biz_speed_cap: float | None = None
        # P1-RAM skim（Tier2）：内存 TS blob + ram:// stream_cb 协议。
        # blob 代数匹配（源 sha × 段表指纹 × 作用域）时 play() 的 EDL
        # 分支改载 ram://；否则回落 Tier1 活 EDL（cap 封顶）。
        self._skim_blob: bytearray | None = None
        self._skim_key: tuple | None = None
        self._skim_id = 0
        self._skim_ready = False
        self._skim_keepalive: list = []
        # 解码直通档（Tier2 改判后的新 Tier2）：dsvc 子进程活流 + rawpush://。
        # 业务倍速编进 spans 的 rate（推流端节流），引擎恒 speed=1（规避
        # #13694/#13513 speed 切换 bug 族）；音频无（双钟失配 T0）。
        self._dthrough_svc: DThroughService | None = None
        self._dthrough_ctx: dict[str, Any] | None = None
        self._dthrough_id = 0
        self._dthrough_ready = False
        # 双轴台账（1c-①）：名义轴编码 seek 命令、实测轴解码 time-pos；
        # _dthrough_rate=当前段抽帧率（=生效倍速，perf speed/角标用）。
        self._dthrough_ledger: DThroughLedger | None = None
        self._dthrough_rate = 0.0
        self._dthrough_keepalive: list = []
        self._dthrough_opens = 0
        self._dthrough_reads = 0
        self._osd_topleft = ""
        self._osd_corner = ""
        self._play_end_reason: str | None = None
        self._pace_mode = "opt"
        self._viewport: tuple[int, int, float] | None = None
        self._stats = self._new_stats()

    @staticmethod
    def _new_stats() -> dict[str, Any]:
        return {
            "presented": 0,
            "discarded": 0,
            "late": 0,
            "late1": 0,
            "late2": 0,
            "catchup_events": 0,
            "pace_resets": 0,
            "hard_resets": 0,
            "seek_count": 0,
            "step_count": 0,
            "verify_count": 0,
            "verify_ok": 0,
            "verify_fail": 0,
            "verify_steps": 0,
            "verify_ms_max": 0.0,
            "q_drop": 0,
            "present_ms_sum": 0.0,
            "present_ms_max": 0.0,
            "present_ms_p95": 0.0,
            "lag_ms_max": 0.0,
            "lag_ms_p95": 0.0,
            "wall_s": 0.0,
            "playback_active": False,
            "play_end_reason": None,
            "pace_mode": "opt",
            "rate_play_frames": 0,
            "skip_trim_absorbed": 0,
            "rate_trim_frames": 0,
            "spikes": [],
            "engine": "mpv",
            # Native-render fluency sampling: the per-frame present/lag
            # pipeline belongs to the OpenCV engine; for native rendering the
            # trustworthy signals are mpv's own drop counter and the wall-clock
            # realtime ratio accumulated from time-pos advances.
            "media_advanced_s": 0.0,
            "play_wall_t0": None,
            "play_wall_end": None,
        }

    # ------------------------------------------------------------------
    # Binding/bootstrap
    # ------------------------------------------------------------------
    def _prepare_binding(self) -> Any:
        if self._factory is not None:
            return self._factory
        dll_dir = self._dll_dir
        if dll_dir is None:
            raw = os.environ.get("MPV_DLL_DIR")
            if raw:
                dll_dir = normalized_source_path(raw)
            else:
                dll_dir = Path(__file__).resolve().parent / "tools" / "libmpv" / "dll"
        dll = dll_dir / "libmpv-2.dll"
        if not dll.is_file():
            raise PreviewEngineError(
                "MPV_DLL_MISSING",
                "project-owned libmpv-2.dll was not found",
                details={"dll_dir": str(dll_dir), "path": str(dll)},
            )
        # python-mpv resolves the DLL at import time.  Make the selected
        # directory explicit instead of relying on a system mpv/PATH entry.
        os.environ["PATH"] = str(dll_dir) + os.pathsep + os.environ.get("PATH", "")
        add_dll_directory = getattr(os, "add_dll_directory", None)
        if add_dll_directory is not None:
            try:
                # Keep the handle alive for the lifetime of the engine; on
                # Windows dropping it immediately can undo the DLL search
                # path before python-mpv imports its dependent libraries.
                self._dll_handle = add_dll_directory(str(dll_dir))
            except OSError:
                pass
        try:
            import mpv
        except Exception as exc:  # pragma: no cover - depends on local DLLs
            raise PreviewEngineError(
                "MPV_IMPORT_FAILED",
                "python-mpv could not load the selected libmpv binding",
                details={"dll": str(dll)},
            ) from exc
        return mpv.MPV

    def _make_player(self) -> Any:
        factory = self._prepare_binding()
        options: dict[str, Any] = {
            # The preview is a silent editing surface; never decode or output
            # audio even when the source has an audio stream.
            "audio": "no",
            "idle": "yes",
            "keep_open": "yes",
            "terminal": False,
            "input_default_bindings": False,
            "input_vo_keyboard": False,
            "osc": "no",
            "framedrop": "vo",
            "hr_seek": "yes",
            # 钉死 OSD 字体：暂停静帧的帧号由 ffmpeg/libass 按同字体同参数
            # 烧录，两侧必须解析到同一字体文件才能逐像素一致
            "osd-font": "Microsoft YaHei",
        }
        if self.wid is None:
            options["vo"] = "null"
        else:
            options["vo"] = "gpu"
            options["wid"] = str(self.wid)
        try:
            player = factory(**options)
        except TypeError:
            # Small fake bindings and older wrappers may not accept all mpv
            # options; the real python-mpv path receives the full set above.
            reduced = {key: value for key, value in options.items() if key in {"vo", "wid", "idle", "terminal"}}
            player = factory(**reduced)
        self._event_callback = self._on_event
        register = getattr(player, "register_event_callback", None)
        if callable(register):
            register(self._event_callback)
        for name in ("time-pos", "pause", "duration", "eof-reached", "path"):
            observe = getattr(player, "observe_property", None)
            if not callable(observe):
                break
            callback = self._make_property_callback(name)
            observe(name, callback)
            self._property_callbacks.append((name, callback))
        return player

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")
            if self._started:
                return
            self._player = self._make_player()
            self._started = True
            self._register_skim_protocol_locked()
            self._ensure_loaded_locked(self.path, "source")

    _SKIM_PROTOCOL = "ram"
    _SKIM_SPEED_CAP = 12.0
    # Tier1（EDL 拼接流）默认速率上限 = PreviewPlayRequest.preview_step_cap
    # 默认值（上游 _preview_step_plan 的 cap=3 parity）。非 play 流程切入
    # edl 模式（seek_edl 防御回落）时用它复位，防止继承 Tier2 的 12x。
    _TIER1_DEFAULT_CAP = 3.0

    def _register_skim_protocol_locked(self) -> None:
        """注册 ram:// stream_cb 协议（libmpv 官方 client API）。

        任何失败只降级不炸：_skim_ready=False 时 play() 永远走 Tier1
        活 EDL，播放链行为与本功能合入前完全一致。测试假绑定没有
        handle 属性 → 自动跳过。
        """
        try:
            if self._factory is not None:
                return
            handle = getattr(self._player, "handle", None)
            if not handle:
                return
            dll_dir = self._dll_dir
            if dll_dir is None:
                raw = os.environ.get("MPV_DLL_DIR")
                dll_dir = (
                    normalized_source_path(raw)
                    if raw
                    else Path(__file__).resolve().parent / "tools" / "libmpv" / "dll"
                )
            lib = ctypes.CDLL(str(Path(dll_dir) / "libmpv-2.dll"))
            lib.mpv_stream_cb_add_ro.restype = ctypes.c_int
            lib.mpv_stream_cb_add_ro.argtypes = [
                ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p, _SKIM_OPEN_FN,
            ]
            _SKIM_ENGINES[id(self)] = self
            rc = lib.mpv_stream_cb_add_ro(
                handle,
                self._SKIM_PROTOCOL.encode("ascii"),
                ctypes.c_void_p(id(self)),
                _SKIM_OPEN_CALLBACK,
            )
            self._skim_ready = rc == 0
            # 解码直通协议同场注册（与 ram:// 同一 open 回调类型/生命周期）
            _DTHROUGH_ENGINES[id(self)] = self
            rc2 = lib.mpv_stream_cb_add_ro(
                handle,
                _DTHROUGH_PROTOCOL.encode("ascii"),
                ctypes.c_void_p(id(self)),
                _DTHROUGH_OPEN_CALLBACK,
            )
            self._dthrough_ready = rc2 == 0
        except Exception:
            self._skim_ready = False

    # ------------------------------------------------------------------
    # State/events
    # ------------------------------------------------------------------
    @staticmethod
    def _event_name(event: Any) -> str:
        if isinstance(event, dict):
            value = event.get("event") or event.get("name") or event.get("event_id")
        else:
            value = getattr(event, "event_id", None)
            value = getattr(value, "name", value)
        text = str(value or "").lower().replace("_", "-")
        for prefix in ("mpveventid.", "eventid."):
            if text.startswith(prefix):
                text = text[len(prefix):]
                break
        if "." in text:
            text = text.rsplit(".", 1)[-1]
        match = re.search(r"\s([a-z][a-z0-9-]*)>?$", text)
        if match:
            text = match.group(1)
        return text

    def _enqueue_event(self, value: dict[str, Any]) -> None:
        try:
            self._events.put_nowait(value)
        except Full:
            # Preserve lifecycle/load markers when a native callback bursts a
            # large number of property updates.  The queue remains bounded,
            # but low-value telemetry is evicted before a critical marker.
            buffered: list[dict[str, Any]] = []
            while True:
                try:
                    buffered.append(self._events.get_nowait())
                except Empty:
                    break
            critical = value.get("event") in {
                "file-loaded",
                "start-file",
                "end-file",
                "shutdown",
            }
            if critical:
                removed = False
                kept: list[dict[str, Any]] = []
                for event in buffered:
                    if not removed and event.get("event") in {
                        "property-change",
                        "viewport",
                    }:
                        removed = True
                        continue
                    kept.append(event)
                buffered = kept
            elif buffered:
                buffered.pop(0)
            for event in buffered:
                try:
                    self._events.put_nowait(event)
                except Full:
                    break
            try:
                self._events.put_nowait(value)
            except Full:
                pass
            self._event_drop_count += 1

    def _on_event(self, event: Any) -> None:
        name = self._event_name(event)
        with self._lock:
            if self._closed and name != "shutdown":
                return
            if isinstance(event, dict) and event.get("generation") is not None:
                if int(event["generation"]) != self._load_generation:
                    return
            # Test/fake bindings can carry the owner scope on an event.  Real
            # libmpv callbacks do not, but accepting the fields here makes the
            # stale-result boundary explicit and keeps adapters deterministic.
            if isinstance(event, dict) and event.get("project_generation") is not None:
                if int(event["project_generation"]) != self._project_generation:
                    return
            if isinstance(event, dict) and event.get("timeline_revision") is not None:
                if int(event["timeline_revision"]) != self._timeline_revision:
                    return
            event_path = None
            if isinstance(event, dict):
                raw_path = event.get("path") or event.get("filename")
                if raw_path:
                    event_path = canonical_load_target(raw_path)
            if name in {"file-loaded", "start-file", "end-file"}:
                # Real libmpv callbacks do not carry our Python generation
                # token.  The observed ``path`` property is the second guard
                # against an old load completion unlocking a newer SOURCE/EDL
                # request while a replacement file is still loading.
                # python-mpv usually exposes the new ``path`` property before
                # (or at the same time as) MPV_EVENT_FILE_LOADED, but the
                # property callback can lag behind the native event.  Query
                # the binding synchronously as a third source of truth so a
                # valid replacement load is not discarded merely because the
                # old observed property is still cached.
                reported_path = event_path or self._binding_path_locked()
                if reported_path is not None and reported_path != getattr(self, "_loaded_path", None):
                    return
                if (
                    reported_path is None
                    and self._current_path is not None
                    and getattr(self, "_loaded_path", None) is not None
                    and self._current_path != self._loaded_path
                ):
                    return
            generation = self._load_generation
            if name in {"file-loaded", "start-file"}:
                if name == "file-loaded":
                    self._loaded_ready = True
                else:
                    self._loaded_ready = False
                self._playing = False
                self._play_end_reason = None
                if name == "file-loaded":
                    # python-mpv delivers this callback on its own event
                    # thread, not the Tk owner's pump, so release the queued
                    # seek/speed/resume here.  Playback then starts even
                    # while the owner thread is busy with a long redraw or
                    # export teardown; poll_events() stays a read-only
                    # fallback for bindings without a live event thread.
                    self._flush_pending_locked()
            elif name in {"end-file", "eof-reached"}:
                self._playing = False
                self._play_end_reason = "eof"
                self._freeze_play_wall_locked("eof")
            elif name == "playback-restart":
                # 闭环验证的落定门：restart=寻址后首帧已呈现，此前 tp
                # 可能仍是瞬态目标值（微探针 T0），门后才允许评估落点
                self._verify_on_restart_locked()
            elif name == "shutdown":
                self._closed = True
                self._playing = False
                self._play_end_reason = "shutdown"
            payload = {
                "event": name,
                "generation": generation,
                "project_generation": self._project_generation,
                "timeline_revision": self._timeline_revision,
                "mode": self._mode,
                "source_frame": self._source_frame,
                "path": self._current_path or getattr(self, "_loaded_path", None),
            }
        self._enqueue_event(payload)

    def _binding_path_locked(self) -> str | None:
        """Read the native path property without trusting a stale callback."""

        player = self._player
        if player is None:
            return None
        for name in ("path", "filename", "stream_open_filename"):
            try:
                value = getattr(player, name)
            except Exception:
                continue
            if not value:
                continue
            try:
                return canonical_load_target(value)
            except (TypeError, ValueError, OSError):
                return str(value)
        getter = getattr(player, "get_property", None)
        if callable(getter):
            try:
                value = getter("path")
            except Exception:
                value = None
            if value:
                try:
                    return canonical_load_target(value)
                except (TypeError, ValueError, OSError):
                    return str(value)
        return None

    @staticmethod
    def _biz_speed_step(source_frame: int, speed_segs: tuple,
                        speedup_1x: bool, speedup_02: bool, factor_02: int) -> int:
        """上游 VideoIOThread.speed_step 的同语义实现（区间闭包，首个命中段优先）。"""
        for seg in speed_segs:
            try:
                s, e, t = int(seg[0]), int(seg[1]), int(seg[2])
            except (TypeError, ValueError, IndexError):
                continue
            if s <= source_frame <= e:
                if t == FRAME_TYPE_1X and speedup_1x:
                    return 2
                if t == FRAME_TYPE_0_2X and speedup_02:
                    return max(2, factor_02)
        return 1

    def _apply_biz_speed_locked(self, source_frame: int) -> None:
        """time-pos 更新后按当前源帧切换 mpv speed；无业务段时保持基准速率。"""
        biz = self._biz_speed
        if biz is None or self._player is None:
            return
        segs, speedup_1x, speedup_02, factor_02 = biz
        step = self._biz_speed_step(source_frame, segs, speedup_1x, speedup_02, factor_02)
        if step == self._biz_applied_step:
            return
        self._biz_applied_step = step
        try:
            self._set_speed_locked(
                self._capped_speed_locked(self._biz_base_rate * step)
            )
        except Exception:
            # 过渡窗口（EDL 换载等）里 speed 写入偶发失败：下一轮 time-pos
            # 会重试，不能为预览的段边界把整条播放链炸掉。
            self._biz_applied_step = 0

    def _make_property_callback(self, property_name: str) -> Callable[..., None]:
        def callback(_name: str, value: Any) -> None:
            self._on_property(property_name, value)

        return callback

    def _on_property(self, property_name: str, value: Any) -> None:
        with self._lock:
            if self._closed:
                return
            if property_name == "time-pos":
                try:
                    position = float(value)
                except (TypeError, ValueError):
                    position = None
                if position is not None and math.isfinite(position):
                    if self._last_time_pos is not None and abs(position - self._last_time_pos) > 1e-9:
                        self._stats["presented"] += 1
                        if self._skip_next_advance:
                            self._skip_next_advance = False
                        else:
                            advance = position - self._last_time_pos
                            # Clamp to one presentation interval's worth: an
                            # EDL relaunch or an unflagged jump must not
                            # inflate the realtime ratio.
                            if 0.0 < advance <= 0.5:
                                self._stats["media_advanced_s"] += advance
                    self._last_time_pos = position
                    self._source_frame = self._frame_for_position_locked(position)
                    # 业务变速段的边界切换只依赖 time-pos（引擎位置唯一权威）；
                    # source/EDL 两种模式经同一映射得到源帧，与上游语义一致。
                    self._apply_biz_speed_locked(self._source_frame)
                    # 闭环验证飞行中：每次 tp 落定都推进一轮评估
                    #（精确匹配不上账本=瞬态，自动等下一拍）
                    if self._verify is not None:
                        self._verify_evaluate_locked()
            elif property_name == "pause":
                if value is not None:
                    self._playing = not bool(value)
            elif property_name == "eof-reached" and bool(value):
                self._playing = False
                self._play_end_reason = "eof"
                self._freeze_play_wall_locked("eof")
            elif property_name == "duration":
                try:
                    self._duration = float(value)
                except (TypeError, ValueError):
                    self._duration = 0.0
            elif property_name == "path":
                if value:
                    try:
                        self._current_path = canonical_load_target(value)
                    except (TypeError, ValueError, OSError):
                        self._current_path = str(value)
                else:
                    self._current_path = None
            self._enqueue_event(
                {
                    "event": "property-change",
                    "property": property_name,
                    "value": value,
                    "generation": self._load_generation,
                    "project_generation": self._project_generation,
                    "timeline_revision": self._timeline_revision,
                    "mode": self._mode,
                    "source_frame": self._source_frame,
                    "path": self._current_path,
                }
            )

    def poll_events(self, limit: int = 64) -> tuple[dict[str, Any], ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        result = []
        for _ in range(limit):
            try:
                result.append(self._events.get_nowait())
            except Empty:
                break
        with self._lock:
            if (self._verify is not None and not self._closed
                    and time.monotonic() > self._verify["deadline"]):
                # 验证超时看门狗：owner 线程的 poll 就是闭环的时钟
                self._verify_finish_locked(False, "timeout")
            self._flush_pending_locked()
        return tuple(result)

    # ------------------------------------------------------------------
    # Source/EDL commands
    # ------------------------------------------------------------------
    def bind_media_info(self, media: MediaInfo | None) -> None:
        """Bind certified media; **heavy derivation happens outside the lock**.

        2026-09-24 review T0：原实现在 `with self._lock` 内构建 424k 行认证
        时间轴 + 浮点秒表 + 重复段名册（实测 **~2.4s**），期间渲染循环
        （`snapshot_perf` 也要同一把锁）整段卡住——播放中认证完成时表现为
        一次 2s+ 的画面冻结。现将纯函数式的派生（只依赖 media，不碰引擎
        状态）移到锁外，锁内只做事务性发布（校验 + 换引用）。
        并发安全：派生期间引擎仍按旧状态工作；发布时用**同一把锁**原子
        替换，且发布前复核源 stat 未变（派生耗时窗口内被换源则拒收）。
        """
        # ---- 锁外：纯派生（不读/写任何引擎字段） ----
        prepared = None
        if media is not None:
            if Path(media.source_path).resolve() != Path(self.path).resolve():
                raise PreviewEngineError(
                    "SOURCE_MISMATCH",
                    "MediaInfo belongs to another source",
                    details={"engine": self.path, "media": str(media.source_path)},
                )
            stat = Path(self.path).stat()
            try:
                # Bind only a current source.  This validation is deliberately
                # transactional: a failed foreign/stale bind must not replace
                # a previously valid certification on the engine.  Source
                # identity is checked with the same cheap stat snapshot used
                # on every exact seek: the certification was produced or
                # cache-loaded through the fail-closed certifier in this
                # process (which verified the source hash), and re-hashing a
                # multi-gigabyte source here froze the Tk owner thread for
                # seconds on certification completion.
                candidate_timeline = None
                if (
                    media.frame_pts_authoritative
                    and media.frame_pts_certification is not None
                ):
                    candidate_timeline = (
                        CertifiedPtsTimeline.from_validated_media_info(media)
                    )
            except MediaInfoError as exc:
                raise PreviewEngineError(
                    exc.code,
                    str(exc),
                    details=getattr(exc, "details", {}),
                ) from exc
            except (OSError, ValueError, TypeError) as exc:
                raise PreviewEngineError(
                    "CERTIFICATION_INVALID",
                    "certified media could not produce a current PTS timeline",
                ) from exc
            prepared = self._prepare_timeline_derivations(candidate_timeline)
        # ---- 锁内：事务性发布 ----
        with self._lock:
            if self._closed:
                raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")
            if media is None:
                self._media_info = None
                self._timeline = None
                self._timeline_times = ()
                self._timeline_clean_start = 0
                self._source_stat = None
                self._edl = None
                self._edl_cache_key = None
                self._dup_run = ()
                return
            assert prepared is not None
            timeline, timeline_times, clean_start, dup_run, stat_sig = prepared
            if self._source_stat is not None and self._source_stat != stat_sig:
                # 派生期间源被替换：拒收（与原先锁内校验同语义）
                raise PreviewEngineError(
                    "SOURCE_CHANGED_AFTER_PROBE",
                    "source file was replaced while the engine was bound",
                    details={"path": self.path},
                )
            self._media_info = media
            self._timeline = timeline
            self._timeline_times = timeline_times
            self._timeline_clean_start = clean_start
            self._dup_run = dup_run
            self._source_stat = stat_sig

    def _prepare_timeline_derivations(self, candidate_timeline):
        """锁外派生：浮点秒表 / 清洁起点 / 重复 PTS 名册 + 源 stat 签名。

        纯函数（只读入参），供 `bind_media_info` 在锁外调用——见该方法的
        并发说明。返回 (timeline, times, clean_start, dup_run, stat_sig)。
        """
        stat = Path(self.path).stat()
        stat_sig = (int(stat.st_size), int(stat.st_mtime_ns))
        # float seconds: 424k Fraction multiplications cost half a second
        # per bind; float products stay exact far below one source tick
        # at these magnitudes and only feed bisect lookups.
        times = (
            tuple(
                float(row.pts) * float(candidate_timeline.time_base)
                for row in candidate_timeline.rows
            )
            if candidate_timeline is not None
            else ()
        )
        clean_start = (
            min(candidate_timeline.head_anomaly_limit,
                max(0, candidate_timeline.frame_count - 1))
            if candidate_timeline is not None
            else 0
        )
        # 重复 PTS 段名册：认证表强制 PTS 严格递增，建表时真实重复
        # PTS（本源 26.7% 帧卷入）被单调化（+1 tick，~65µs），因此
        # 不能用「相邻相等」识别——按「相邻间隙 < 2ms = 准重复段」：
        # 真实帧间隔 ≥8ms（120fps 上限；ms 时基源单调化 ε=1ms 也在
        # 判据内），两数量级干净分隔。段内帧（段首之后）tp 无法
        # 区分 → 闭环拒收走 C4；段首放行（全片扫描 300/300 delta=0
        # + Tier2 像素 34/34 必落段首，T0）。
        dup = [-1] * len(times)
        k = 0
        while k < len(times):
            j = k + 1
            while (j < len(times)
                   and (times[j] - times[j - 1]) < self._DUP_GAP_EPS_S):
                j += 1
            if j - k > 1:
                for m in range(k + 1, j):
                    dup[m] = k
            k = j
        return (candidate_timeline, times, clean_start, tuple(dup), stat_sig)

    def _assert_bound_source_current_locked(self) -> None:
        """Reject obvious source replacement without hashing on every seek."""

        media = self._media_info
        if media is None or self._source_stat is None:
            return
        try:
            stat = Path(self.path).stat()
            current = (int(stat.st_size), int(stat.st_mtime_ns))
        except OSError as exc:
            raise PreviewEngineError(
                "SOURCE_CHANGED_AFTER_PROBE",
                "bound source is no longer available",
                details={"path": self.path},
            ) from exc
        if current != self._source_stat:
            raise PreviewEngineError(
                "SOURCE_CHANGED_AFTER_PROBE",
                "bound source metadata changed after certification",
                details={
                    "path": self.path,
                    "expected_size": self._source_stat[0],
                    "actual_size": current[0],
                    "expected_mtime_ns": self._source_stat[1],
                    "actual_mtime_ns": current[1],
                },
            )

    def _validate_request_source_locked(self, request: CertifiedEdlRequest) -> None:
        media = request.media_info
        if Path(media.source_path).resolve() != Path(self.path).resolve():
            raise PreviewEngineError(
                "SOURCE_MISMATCH",
                "certified EDL request belongs to another source",
                details={"engine": self.path, "media": str(media.source_path)},
            )
        if self._media_info is not None and media.source_sha256 != self._media_info.source_sha256:
            raise PreviewEngineError(
                "CERTIFICATION_MISMATCH",
                "certified EDL request does not match the bound source certification",
            )

    def _require_player_locked(self) -> Any:
        if self._closed:
            raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")
        if not self._started or self._player is None:
            self.start()
        assert self._player is not None
        return self._player

    def _ensure_loaded_locked(
        self,
        path: str | os.PathLike[str],
        mode: str,
        file_options: str | None = None,
    ) -> None:
        player = self._require_player_locked()
        source = canonical_load_target(path)
        if self._mode == mode and getattr(self, "_loaded_path", None) == source:
            return
        self._load_generation += 1
        self._mode = mode
        self._loaded_path = source
        self._loaded_ready = False
        self._pending_seek = None
        self._pending_resume = False
        self._pending_speed = None
        # `_source_frame` 复位 0 的同时清空 `_last_time_pos`：快照里
        # `source_frame` 的唯一可信条件是「本代已有 time-pos 读回」。
        # 消费者（GUI 播放中无条件采信引擎帧号）据此可分辨「真在第 0 帧」
        # 与「本代还没读回」——后者若采信，红条会闪回开头（跨段起播/
        # 编辑后重播实测 2 拍 ~20ms 闪 0，09-24 review T0）。
        self._last_time_pos = None
        self._source_frame = 0
        self._play_end_reason = None
        self._biz_applied_step = 0  # 换载后首个 time-pos 重新钉一次速率
        self._verify = None  # 换载作废飞行中的验证任务（generation 双保险）
        # Hold the player paused across the load: pause is a global property
        # that persists into the next file, and the pending seek must land
        # before anything is presented.
        try:
            player.pause = True
        except Exception:
            try:
                player.command("set", "pause", "yes")
            except Exception:
                pass
        # A synchronous loadfile blocks the caller for the whole demuxer
        # open; a real EDL with thousands of segments takes seconds and
        # freezes the Tk owner thread ("not responding").  Issue the load
        # fire-and-forget when the binding supports async commands; the
        # event thread completes the sequence via file-loaded.  Fake and
        # reduced bindings keep the synchronous play() route.
        loader = getattr(player, "command_async", None)
        if callable(loader):
            try:
                if file_options:
                    # loadfile <url> <flags> <index> <options>（0.41 签名：
                    # 第 4 参=index(int)、第 5 参=options——4 参字符串形态会
                    # 把 options 落进 index 槽报 -4，且被 except 吞掉静默
                    # 丢失全部 per-file 选项，diag_rawload 实证）。
                    loader("loadfile", source, "replace", "0", file_options)
                else:
                    loader("loadfile", source, "replace")
                return
            except Exception:
                pass
        player.play(source)

    @staticmethod
    def _is_paused_locked(player) -> bool:
        try:
            return bool(getattr(player, "pause", False))
        except Exception:
            return False

    def _command_seek_locked(self, seconds: Fraction, *, exact: bool) -> None:
        player = self._require_player_locked()
        mode = "absolute+exact" if exact else "absolute"
        if (
            exact
            and self._is_paused_locked(player)
            and self._verify is None
            and self._mode not in ("skim", "dthrough")
        ):
            # A paused exact seek presents a stale frame a couple of source
            # ticks before the target at PTS/segment boundaries: the user
            # sees the previous span's picture (e.g. a deleted PAUSE overlay
            # at a kept-island start).  Measured landing curve on real data:
            # +1.5 ticks still lands in the previous span at some boundaries,
            # +2.5 ticks lands inside [target, target+1] at every boundary
            # and mid-segment point (±1 within identical-looking content,
            # counter stays authoritative).  Playback seeks are untouched.
            # 闭环验证任务飞行中跳过修正：验证环自己负责精确落位，
            # 带修正反而制造 +1~+2 过冲（全片扫描 T0）。
            # skim blob 同样跳过：均匀压缩时间轴没有源 PTS 边界病，
            # 暂停态流内 seek T0 实测 5.0→5.0 精确落位，带修正反而
            # 过冲 ~2.5 帧（41.7ms=2.5 个 16.7ms 帧）。直通档 rawpush
            # CFR 虚拟轴同理，1c 起一并豁免（带修正=名义 tick 整体偏 2.5）。
            seconds = seconds + Fraction(5, 120)
        player.command("seek", _fraction_seconds(seconds), mode)
        self._stats["seek_count"] += 1
        # The next time-pos update lands at the seek target; its delta from
        # the pre-seek position is a jump, not playback, and must not count
        # toward the realtime ratio.
        self._skip_next_advance = True

    def _flush_pending_locked(self) -> None:
        if self._closed or not self._loaded_ready or self._player is None:
            return
        try:
            if self._pending_speed is not None:
                self._set_speed_locked(self._pending_speed)
            if self._pending_seek is not None:
                seconds, exact = self._pending_seek
                self._command_seek_locked(seconds, exact=exact)
                self._pending_seek = None
            if self._pending_resume:
                self._pending_resume = False
                self._resume_locked()
            self._pending_speed = None
        except Exception:
            # Keep the action pending for the next owner-thread poll.  This
            # handles libmpv's short transition window after file-loaded.
            return

    def _source_time_locked(self, frame: int, *, exact: bool) -> Fraction:
        timeline = self._timeline
        if timeline is None:
            if exact:
                raise PreviewEngineError(
                    "CERTIFICATION_REQUIRED",
                    "absolute+exact source seek requires certified PTS rows",
                )
            if frame != 0:
                raise PreviewEngineError(
                    "CERTIFICATION_REQUIRED",
                    "non-zero source seek requires certified PTS rows",
                )
            return Fraction(0, 1)
        self._assert_bound_source_current_locked()
        if frame < 0 or frame >= timeline.frame_count:
            raise PreviewEngineError(
                "SOURCE_FRAME_OUT_OF_RANGE",
                "source frame is outside the certified PTS table",
                details={"frame": frame, "count": timeline.frame_count},
            )
        return timeline.rows[frame].pts * timeline.time_base

    def source_time_for_frame(self, frame: int) -> float | None:
        """Certified PTS seconds for a source frame, or None pre-certification.

        Lets owners address the source with the same PTS semantics the engine
        uses for exact seeks (e.g. the paused-still overlay decode).
        """
        with self._lock:
            try:
                return float(self._source_time_locked(int(frame), exact=True))
            except Exception:
                return None

    def seek_source(self, request: SourceSeekRequest) -> bool:
        with self._lock:
            if self._closed:
                raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")
            self._verify = None  # 旧式寻址取代飞行中的验证任务（静默）
            seconds = self._source_time_locked(request.source_frame, exact=request.exact)
            self._timeline_revision = request.timeline_revision
            # 模式不变量：source 模式无拼接边界风暴，速率不封顶（与
            # play() 源分支一致）；清掉 skim/EDL 播放可能残留的旧 cap。
            self._biz_speed_cap = None
            self._ensure_loaded_locked(self.path, "source")
            if self._loaded_ready:
                self._command_seek_locked(seconds, exact=request.exact)
            else:
                self._pending_seek = (seconds, request.exact)
            self._source_frame = request.source_frame
            return True

    # 闭环验证预算：全片扫描（probe_seek_sweep，2341点，T0）实证无修正
    # exact seek 偏差∈[0,2]帧（只停早、零过冲），预算4帧+总时限1.2s 宽裕；
    # 超限/超时/异常一律判失败 → 调用方回退 C4 静帧管线，永不显示错帧。
    _VERIFY_STEP_BUDGET = 4
    _VERIFY_TIMEOUT_S = 1.2
    # 准重复段判据：认证表相邻间隙小于此值视为「真实重复 PTS 的单调化
    # 产物」。真实帧间隔 ≥8ms（120fps 上限），ms 时基源的单调化 ε=1ms，
    # 2ms 阈值对两边都留出数量级余量。
    _DUP_GAP_EPS_S = 0.002

    def seek_source_verified(self, request: SourceSeekRequest) -> bool:
        """带闭环验证的暂停精确定位（选项2）。

        流程：无修正 exact seek → playback-restart 门（瞬态期 tp 报目标值，
        微探针 T0）→ time-pos 与账本 float 精确匹配读回落帧 → 停早则
        frame-step 前进补齐（探针D：位级精确）→ 复核落账。结果以
        ``seek-verified`` 事件出队（ok/reason/frame/steps/ms）。

        返回 False = 不适用且未发寻址（EDL 模式 / 目标在重复 PTS 段内
        ——段首除外，段首有 34/34 必落段首的 T0 背书 / 未认证账本时抛
        CERTIFICATION_REQUIRED 与 seek_source 一致），调用方直接走旧
        seek+C4 路径。

        证据链（T0）：探针E=落定后 tp 报实际呈现帧；全片扫描=2341 点
        零过冲、偏差≤2帧、restart 门控下精确匹配零假阳性、唯一PTS点
        tp 失配=0；探针D=frame-step 逐帧位级精确。
        """
        with self._lock:
            if self._closed:
                raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")
            if self._mode in ("edl", "skim"):
                return False
            frame = int(request.source_frame)
            try:
                head = self._dup_run[frame]
            except IndexError:
                head = -1  # 越界目标由 _source_time_locked 统一报错
            if head >= 0:
                return False
            seconds = self._source_time_locked(frame, exact=True)
            self._timeline_revision = request.timeline_revision
            self._ensure_loaded_locked(self.path, "source")
            self._verify = {
                "target": frame,
                "phase": "await_restart",
                "steps_sent": 0,
                "t0": time.monotonic(),
                "deadline": time.monotonic() + self._VERIFY_TIMEOUT_S,
                "generation": self._load_generation,
            }
            self._stats["verify_count"] += 1
            if self._loaded_ready:
                # _command_seek_locked 在验证任务飞行中自动跳过 +5/120
                # 修正：修正只服务旧路径的「恢复播放不停早」，闭环自己
                # 负责精确落位（无修正实测只停早、从不过冲，单向好补）。
                self._command_seek_locked(seconds, exact=True)
            else:
                self._pending_seek = (seconds, True)
            self._source_frame = frame
            return True

    def _verify_on_restart_locked(self) -> None:
        v = self._verify
        if v is None or v["generation"] != self._load_generation:
            return
        if v["phase"] != "await_restart":
            return
        v["phase"] = "correcting"
        self._verify_evaluate_locked()

    def _verify_frame_for_tp_locked(self, pos: float, around: int) -> int | None:
        """time-pos → 账本帧号（float 精确匹配，窗口 ±4，容差 1e-7）。

        刻意不复用 _frame_for_position_locked：那条路 Fraction(str(float))
        的 ε 方向性只适合 UI 映射；验证环要「匹配不上就等下一拍」的
        严格语义。mpv tp 与账本 float(pts×tb) 同为 IEEE double（探针E
        实测差 0.0），1e-7 容差远小于最小帧差（~8ms）。
        """
        times = self._timeline_times
        if not times:
            return None
        lo = max(0, around - 4)
        hi = min(len(times), around + 5)
        best, best_d = None, None
        for k in range(lo, hi):
            d = abs(times[k] - pos)
            if best_d is None or d < best_d:
                best, best_d = k, d
        if best is None or best_d > 1e-7:
            return None
        return best

    def _verify_evaluate_locked(self) -> None:
        v = self._verify
        if v is None or v["phase"] != "correcting" or self._player is None:
            return
        if time.monotonic() > v["deadline"]:
            self._verify_finish_locked(False, "timeout")
            return
        pos = self._last_time_pos
        if pos is None:
            return
        landed = self._verify_frame_for_tp_locked(pos, v["target"])
        if landed is None:
            return  # 瞬态/未落定（tp 对不上账本任何邻帧）：等下一次观察
        delta = v["target"] - landed
        if delta == 0:
            self._verify_finish_locked(
                True, "exact" if v["steps_sent"] == 0 else "corrected")
            return
        if delta < 0:
            # 无修正 seek 全片 2341 点零过冲（T0）；出现过冲=账本外
            # 行为，不猜原因直接判失败走 C4 兜底
            self._verify_finish_locked(False, "overshoot")
            return
        if v["steps_sent"] + delta > self._VERIFY_STEP_BUDGET:
            self._verify_finish_locked(False, "budget")
            return
        for _ in range(delta):
            try:
                self._player.command("frame-step")
            except Exception:
                self._verify_finish_locked(False, "step_error")
                return
            v["steps_sent"] += 1
            self._stats["verify_steps"] += 1
        # 补步后唯一 PTS 目标的 tp 必变（不同帧不同 PTS）→ time-pos
        # 观察回调驱动下一轮评估直至 delta==0

    def _verify_finish_locked(self, ok: bool, reason: str) -> None:
        v = self._verify
        self._verify = None
        if v is None:
            return
        self._stats["verify_ok" if ok else "verify_fail"] += 1
        elapsed_ms = (time.monotonic() - v["t0"]) * 1000.0
        if elapsed_ms > self._stats.get("verify_ms_max", 0.0):
            self._stats["verify_ms_max"] = elapsed_ms
        self._enqueue_event({
            "event": "seek-verified",
            "ok": ok,
            "reason": reason,
            "frame": v["target"],
            "steps": v["steps_sent"],
            "ms": round(elapsed_ms, 1),
            "generation": v["generation"],
            "project_generation": self._project_generation,
            "timeline_revision": self._timeline_revision,
            "mode": self._mode,
            "source_frame": v["target"] if ok else self._source_frame,
        })

    def seek_edl(self, source_frame: int) -> bool:
        """Seek inside the current EDL view by source frame, staying in EDL mode.

        ``seek_source`` forces the engine back to the raw source stream, so
        frame stepping through the skip-trimmed preview must not use it.
        Frames inside deleted ranges snap to the containing span end, the
        same mapping playback uses.  Pause state is untouched: a paused
        player steps paused, a playing one keeps rolling after the seek.
        Raises ``EDL_NOT_READY`` when no EDL artifact has been built yet
        (caller falls back to a plain source seek; the engine is in source
        mode in that case anyway).
        """
        with self._lock:
            if self._closed:
                raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")
            self._verify = None  # EDL 寻址取代飞行中的验证任务（静默）
            edl = self._edl
            if edl is None:
                raise PreviewEngineError(
                    "EDL_NOT_READY",
                    "no EDL artifact has been built for this engine yet",
                )
            if isinstance(source_frame, bool) or not isinstance(source_frame, int):
                raise TypeError("source_frame must be an integer")
            frame = max(0, min(int(source_frame), edl.frame_count - 1))
            virtual = edl.virtual_time_for_source(frame, snap=True)
            if self._mode == "skim" and self._skim_blob is not None:
                # Tier2 保持：blob 时间轴 ≡ EDL 虚拟时间轴，直接在 ram://
                # 流内寻址。播放中流内 seek 落点不可靠（TS 无索引，T0 实测
                # 2.0→7.78），暂停态精确（5.0→5.0）→ 播放中先落暂停、
                # 落位后恢复。绝不切回 EDL 文件：那会让 Tier1 活流继承
                # skim 的 12x 上限（cap 只在 play 请求时钉），高速拼接
                # 边界风暴会原样复活。
                player = self._require_player_locked()
                resume_after = self._playing
                if resume_after:
                    try:
                        player.pause = True
                    except Exception:
                        try:
                            player.command("set", "pause", "yes")
                        except Exception:
                            pass
                self._command_seek_locked(virtual, exact=True)
                if resume_after:
                    self._resume_locked()
                self._source_frame = edl.source_frame_for_virtual_time(virtual)
                return True
            if self._mode == "skim":
                # 防御：skim 模式但 blob 已被清（档位变更等竞态）→ 回落
                # EDL 活流，速率上限必须落回 Tier1 默认，绝不带 12x 进
                # 拼接流（模式不变量：edl 模式 cap ≤ Tier1 默认）。
                self._biz_speed_cap = self._TIER1_DEFAULT_CAP
            self._ensure_loaded_locked(edl.path, "edl")
            if self._loaded_ready:
                self._command_seek_locked(virtual, exact=True)
            else:
                self._pending_seek = (virtual, True)
            self._source_frame = edl.source_frame_for_virtual_time(virtual)
            return True

    def step_frame(self, direction: int) -> bool:
        """暂停步进：mpv 原生 frame-step/frame-back-step（VERY_EXACT 路径）。

        探针D（probe_frame_step，T0）实证裸命令位级精确且 0.3-17.2ms；
        ``input-command`` 元命令在此 libmpv(v0.41.0-926) 上不可用，故直发。
        步进作用于当前显示的时序（source 或 EDL 虚拟时间轴，EDL 天然
        跳过删除段），落帧经 time-pos 观察事件回读；调用方无需传目标
        帧号。播放中调用先落暂停，与上游「暂停才步进」语义一致。
        """
        with self._lock:
            if self._closed:
                raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")
            player = self._require_player_locked()
            self._verify = None  # 用户步进取代飞行中的验证定位（静默）
            if not self._is_paused_locked(player):
                try:
                    player.pause = True
                except Exception:
                    pass
            cmd = "frame-step" if direction >= 0 else "frame-back-step"
            try:
                player.command(cmd)
            except Exception:
                return False
            self._stats["step_count"] += 1
            return True

    def play(self, request: PreviewPlayRequest) -> bool:
        with self._lock:
            if self._closed:
                raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")
            self._verify = None  # 播放请求取代飞行中的验证定位（静默）
            # 业务变速段不再拒播：段表与开关钉进 _biz_speed，time-pos 更新时
            # 用 mpv speed 属性切换（与上游 VideoIOThread.speed_step 同语义）。
            # EDL 模式下 time-pos 是虚拟时间，但经 _frame_for_position_locked
            # 折回源帧，同一份段表照常生效。
            biz = (
                tuple(
                    (int(seg[0]), int(seg[1]), int(seg[2]))
                    for seg in (request.speed_segments or ())
                ),
                bool(request.speedup_1x),
                bool(request.speedup_02),
                max(1, int(request.speedup_02_factor or 1)),
            )
            self._biz_speed = biz if (biz[0] or biz[1] or biz[2]) else None
            self._biz_base_rate = float(request.playback_rate)
            self._biz_applied_step = 0
        if request.skip_trimmed and request.timeline_plan.deleted_ranges:
            # complete_for_export re-hashes the multi-gigabyte source; the
            # engine's bound certification is the authoritative readiness
            # signal here (the certifier performed the full gate and the
            # engine stat-checks the source on every command).
            if self._media_info is None or not (
                self._media_info.frame_pts_authoritative
                and self._media_info.frame_pts_certification is not None
            ):
                raise PreviewEngineError(
                    "CERTIFICATION_REQUIRED",
                    "EDL preview requires current certified media",
                )
            return self.play_edl(
                CertifiedEdlRequest(
                    media_info=self._media_info,
                    timeline_plan=request.timeline_plan,
                    project_generation=request.project_generation,
                    timeline_revision=request.timeline_revision,
                ),
                start_frame=request.start_frame,
                playback_rate=request.playback_rate,
                speed_cap=max(1.0, float(request.preview_step_cap)),
            )
        with self._lock:
            if self._closed:
                raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")
            self._project_generation = request.project_generation
            self._timeline_revision = request.timeline_revision
            # 源模式不封顶：连续文件没有拼接边界风暴（T0：连续文件 10x
            # rt9.978 干净；probe_biz_osd_engine 源模式全场景 OSD≥57.5），
            # UI 4x 与业务高速是 f53d6dc 既有能力。cap parity 只治 EDL
            # 拼接播放（Tier1）；显式清掉上一次播放可能留下的旧 cap。
            self._biz_speed_cap = None
            self._ensure_loaded_locked(self.path, "source")
            exact = self._timeline is not None
            seconds = self._source_time_locked(request.start_frame, exact=exact)
            if self._loaded_ready:
                self._set_speed_locked(request.playback_rate)
                self._command_seek_locked(seconds, exact=exact)
                self._resume_locked()
            else:
                self._pending_speed = request.playback_rate
                self._pending_seek = (seconds, exact)
                self._pending_resume = True
            return True

    def play_edl(
        self,
        request: CertifiedEdlRequest,
        *,
        start_frame: int,
        playback_rate: float = 1.0,
        skim: bool | None = None,
        speed_cap: float | None = None,
    ) -> bool:
        """EDL 虚拟时间轴播放；skim blob 代数匹配时自动改载 ram://（Tier2）。

        ``skim``：None/True = 代数匹配即启用，False = 强制 Tier1 活 EDL。
        ``speed_cap``：Tier1 速率封顶（上游 preview_step_cap parity）；
        skim 启用时改用 ``_SKIM_SPEED_CAP``（blob 天花板守卫）。
        """
        with self._lock:
            if self._closed:
                raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")
            self._verify = None  # EDL 播放取代飞行中的验证任务（静默）
            rate = float(playback_rate)
            if rate <= 0 or not math.isfinite(rate):
                raise ValueError("playback_rate must be a finite positive number")
            if isinstance(start_frame, bool) or not isinstance(start_frame, int):
                raise TypeError("start_frame must be an integer")
            if start_frame < 0 or start_frame >= request.timeline_plan.total_frames:
                raise PreviewEngineError(
                    "SOURCE_FRAME_OUT_OF_RANGE",
                    "start frame is outside the timeline plan",
                    details={"start_frame": start_frame},
                )
            self._validate_request_source_locked(request)
            use_skim = (
                self._skim_eligible_locked(request) if skim is not False else False
            )
            # 09-13：skim 只服务「Tier1 cap 扛不住」的速率——≤cap 的播放走
            # Tier1 活 EDL 原画质（旧语义 blob 在库即用，1x 播放也糊=owner
            # 真机 T0：分析预热建好 blob 后 1x 手检全程代理）。speed_cap=
            # None 的直调方保持旧语义（契约测试与既有调用方依赖）。
            if use_skim and speed_cap is not None and rate <= float(speed_cap):
                use_skim = False
            self._biz_speed_cap = (
                self._SKIM_SPEED_CAP
                if use_skim
                else float(speed_cap) if speed_cap is not None
                else None
            )
            # Serialize EDL publication with close().  Once close begins, no
            # command may publish a new preview artifact or reach libmpv.
            # Rebuilding the EDL re-derives the certified timeline; repeated
            # toggles of the same plan reuse the built artifact instead, and
            # a fresh build passes the engine's already-bound timeline so
            # the multi-gigabyte source is stat-checked, not re-hashed.
            cache_key = (
                request.media_info.source_sha256,
                request.timeline_plan.fingerprint,
                request.project_generation,
                request.timeline_revision,
            )
            if (
                self._edl is not None
                and self._edl_cache_key == cache_key
                and self._edl.path.is_file()
            ):
                edl = self._edl
            else:
                self._assert_bound_source_current_locked()
                edl = build_certified_edl(
                    request, self._edl_dir, timeline=self._timeline
                )
                self._edl_cache_key = cache_key
            seconds = edl.virtual_time_for_source(start_frame, snap=True)
            self._project_generation = request.project_generation
            self._timeline_revision = request.timeline_revision
            self._edl = edl
            if use_skim:
                # Tier2：blob 时间轴 ≡ EDL 虚拟时间轴（构建滤镜与磁盘导出
                # 同一套 tick 语义，T0 tp_end 实证），映射对象照常是 _edl。
                # cache=no 只作本文件的 per-file 选项：blob 已在 RAM，mpv
                # demuxer 二次缓存会再复制至多 150MB（足迹×2，T0 实证）。
                self._ensure_loaded_locked(
                    f"{self._SKIM_PROTOCOL}://skim{self._skim_id}",
                    "skim",
                    file_options="cache=no",
                )
            else:
                self._ensure_loaded_locked(edl.path, "edl")
            rate = self._capped_speed_locked(rate)
            if self._loaded_ready:
                self._command_seek_locked(seconds, exact=True)
                self._set_speed_locked(rate)
                self._resume_locked()
            else:
                self._pending_seek = (seconds, True)
                self._pending_speed = rate
                self._pending_resume = True
            return True

    def play_dthrough(
        self,
        request: CertifiedEdlRequest,
        *,
        spans: Sequence[Sequence[float] | LedgerSpan],
        fps: float = 60.0,
        start_virtual_frame: int = 0,
        start_source_frame: int | None = None,
    ) -> bool:
        """解码直通档播放（新 Tier2）：dsvc 活流 → rawpush:// CFR 虚拟轴。

        ``spans``=[start,end,rate]（源秒）或 LedgerSpan（源帧区间+源秒）；
        rate=业务×UI 倍速（抽帧在 dsvc 推流端完成）→ 引擎恒 speed=1，无
        mpv speed 切换面。``start_source_frame``（源帧，GUI 入口）优先于
        ``start_virtual_frame``（名义 tick，E2E/底层入口）——起点定位走
        per-file start= 选项（名义轴坐标，dsvc.locate 语言）。双轴台账
        随播放建立：名义轴编码后续 seek 命令，实测轴（dsvc per_span）解码
        time-pos 读回。EDL 台账照常建立供时间轴/映射使用，但播放数据不经
        EDL 文件。
        """
        with self._lock:
            if self._closed:
                raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")
            if not self._dthrough_ready:
                raise PreviewEngineError(
                    "DTHROUGH_NOT_READY",
                    "rawpush:// stream_cb protocol is not available",
                )
            fps = float(fps)
            norm_spans = [coerce_span(s, fps) for s in spans]
            if not norm_spans:
                raise PreviewEngineError(
                    "DTHROUGH_EMPTY_SPANS", "dthrough span table is empty"
                )
            ledger = DThroughLedger(norm_spans, fps=fps)
            if start_source_frame is not None:
                if isinstance(start_source_frame, bool) or not isinstance(
                    start_source_frame, int
                ):
                    raise TypeError("start_source_frame must be an integer")
                if start_source_frame < 0:
                    raise PreviewEngineError(
                        "SOURCE_FRAME_OUT_OF_RANGE",
                        "start_source_frame is negative",
                        details={"start_source_frame": start_source_frame},
                    )
                start_virtual_frame = ledger.nominal_tick_for_source_frame(
                    start_source_frame
                )
            if not isinstance(start_virtual_frame, int) or isinstance(
                start_virtual_frame, bool
            ):
                raise TypeError("start_virtual_frame must be an integer")
            if start_virtual_frame < 0:
                raise PreviewEngineError(
                    "SOURCE_FRAME_OUT_OF_RANGE",
                    "start_virtual_frame is negative",
                    details={"start_virtual_frame": start_virtual_frame},
                )
            self._verify = None
            self._validate_request_source_locked(request)
            # 暂停→播放快速路径（09-23 真机 T0，症状 B）：每次都整路重载会让
            # 恢复播放的头 1~2s 跑不满 60fps（探针实测重载首窗 0/18.6/23.5fps
            # vs 纯 resume 首窗 58.9~60.8fps；深定位更差）。流未失效时直接
            # 取消暂停——语义等价（同一段表、同一位置），代价差两个数量级。
            if self._dthrough_resume_ok_locked(
                request, norm_spans, fps, start_source_frame
            ):
                # 作用域字段照常刷新（GUI 的代数/修订校验依赖它们与最新
                # 请求一致；重载路径在下方同样赋值）。
                self._project_generation = request.project_generation
                self._timeline_revision = request.timeline_revision
                self._resume_locked()
                return True
            # EDL 台账照常（虚拟秒↔源帧映射与时间轴展示用）；构建失败不阻播
            cache_key = (
                request.media_info.source_sha256,
                request.timeline_plan.fingerprint,
                request.project_generation,
                request.timeline_revision,
            )
            try:
                if (
                    self._edl is not None
                    and self._edl_cache_key == cache_key
                    and self._edl.path.is_file()
                ):
                    self._edl = self._edl
                else:
                    self._assert_bound_source_current_locked()
                    self._edl = build_certified_edl(
                        request, self._edl_dir, timeline=self._timeline
                    )
                    self._edl_cache_key = cache_key
            except PreviewEngineError:
                self._edl = None  # 直通档不强制依赖 EDL 文件
            self._project_generation = request.project_generation
            self._timeline_revision = request.timeline_revision
            vstream = request.media_info.video_streams[0]
            if self._dthrough_svc is None:
                self._dthrough_svc = DThroughService(
                    width=vstream.width, height=vstream.height
                )
            self._dthrough_ctx = {
                "src": str(request.media_info.source_path),
                "spans": [[s.a, s.b, s.rate] for s in norm_spans],
                "fps": fps,
                "frame_size": vstream.width * vstream.height * 3 // 2,
            }
            self._dthrough_id += 1
            self._dthrough_svc.load(
                self._dthrough_ctx["src"], self._dthrough_ctx["spans"], fps
            )
            # 双轴台账随播放建立（1c-①）：名义轴编码 seek 命令，实测轴由
            # dsvc span/eof 事件逐段精化（禁名义值）
            self._dthrough_ledger = ledger
            # 中途起播=dsvc **seek 代**：demuxer 的 per-file ``start=`` 会走
            # stream_cb seek_fn → ``svc.seek()``（probe_redbar_jump3 的
            # svc_calls 实证：load 之后紧跟 seek virtual_frame=11902），本代
            # **只从目标段起推**，目标段之前的 per_span 全 0。实测轴必须同步
            # 重定基，否则那些 0 会把 tick→源帧映射整体拽偏——探针实测
            # start=12000 读回 23999（红条横跳的直接来源）。语义与
            # ``_seek_dthrough_tick_locked`` 的重定基完全一致。
            start_origin_frame: int | None = None
            if start_virtual_frame > 0:
                start_origin_frame = ledger.source_frame_for_nominal_tick(
                    start_virtual_frame
                )
                if start_origin_frame is not None:
                    ledger.rebase(start_virtual_frame, start_origin_frame)
            try:
                start_hint = (
                    start_source_frame
                    if start_source_frame is not None
                    else start_origin_frame
                )
                self._dthrough_rate = float(
                    ledger.rate_for_source_frame(int(start_hint or 0))
                )
            except (TypeError, ValueError):
                self._dthrough_rate = float(norm_spans[0].rate)
            self._biz_speed = None  # 直通档：无 mpv 段切速（速率在推流端）
            self._biz_speed_cap = None
            # 起播定位走 per-file start= 选项（demuxer 内部初始 seek，无命
            # 令竞态）；起播瞬间 stream_cb 尚未 open 完，seek 命令会 -12。
            seconds = float(start_virtual_frame) / max(float(fps), 1e-6)
            # rawvideo demuxer 四件套必须 per-file（全局设会毒死 EDL/源文件）
            # framedrop=no：全局 framedrop=vo 与 keep_open=yes 组合在
            # rawvideo CFR 流上致死（合成流 5.5s 冻结复现，diag_rawload H/I
            # 二分实证：单去 framedrop 或单去 keep_open 均可播）。
            file_options = (
                "cache=no,framedrop=no,demuxer=rawvideo,"
                f"demuxer-rawvideo-fps={float(fps):.6f},"
                f"demuxer-rawvideo-w={vstream.width},"
                f"demuxer-rawvideo-h={vstream.height},"
                "demuxer-rawvideo-format=I420"
            )
            if start_virtual_frame > 0:
                file_options += f",start={seconds:.6f}"
            self._ensure_loaded_locked(
                f"{_DTHROUGH_PROTOCOL}://dt{self._dthrough_id}",
                "dthrough",
                file_options=file_options,
            )
            if self._loaded_ready:
                self._set_speed_locked(1.0)  # 恒速：业务倍速已在推流端抽帧
                self._resume_locked()
            else:
                self._pending_speed = 1.0
                self._pending_resume = True
            return True

    def _dthrough_resume_ok_locked(
        self,
        request: CertifiedEdlRequest,
        norm_spans: Sequence[LedgerSpan],
        fps: float,
        start_source_frame: int | None,
    ) -> bool:
        """当前已载 dthrough 流能否直接 resume（免整路重载）？

        判据（全部满足才走快速路径，否则回落完整重载=行为与从前一致）：
          · 已在 dthrough 模式、流已就绪、播放器未暂停态外异常；
          · 段表指纹与本次请求**逐段一致**（同段数、同区间、同 rate）；
          · 源相同；
          · 当前呈现位置与请求起点**落在同一段**（同段内 resume 不改变
            语义；跨段/跳段必须重载，否则推流相位与 UI 位置不一致）。
        实测收益（probe_resume_stall）：恢复播放首 0.5s 窗 23.5fps → 58.9fps。
        """
        if self._mode != "dthrough" or not self._loaded_ready:
            return False
        if self._dthrough_svc is None or self._dthrough_ledger is None:
            return False
        # 本代流已推完（EOF）：resume 只会立刻再报 EOF（read 返 0），
        # 必须重载起新代——否则「播完→同段再播」变成空转假播放。
        if self._dthrough_svc.eof_seen():
            return False
        ctx = self._dthrough_ctx
        if not ctx:
            return False
        if str(ctx.get("src")) != str(request.media_info.source_path):
            return False
        if abs(float(ctx.get("fps") or 0.0) - float(fps)) > 1e-9:
            return False
        loaded = ctx.get("spans") or ()
        if len(loaded) != len(norm_spans):
            return False
        for a, b in zip(loaded, norm_spans):
            if (
                float(a[0]) != float(b.a)
                or float(a[1]) != float(b.b)
                or int(a[2]) != int(b.rate)
            ):
                return False
        # 同段判定：请求起点 vs 当前呈现帧（名义轴，与 rebase 无关）
        if start_source_frame is None:
            return False
        current = int(self._source_frame)
        target = int(start_source_frame)
        ledger = self._dthrough_ledger
        return (
            ledger.span_index_for_source_frame(current)
            == ledger.span_index_for_source_frame(target)
        )

    @property
    def dthrough_ready(self) -> bool:
        """rawpush:// 协议注册成功（引擎级能力，与当前是否在播无关）。"""
        return bool(self._dthrough_ready)

    @property
    def dthrough_active(self) -> bool:
        """当前装载流=解码直通档（GUI seek/角标路由用）。"""
        return self._mode == "dthrough" and self._dthrough_svc is not None

    def seek_dthrough_frame(self, virtual_frame: int) -> bool:
        """直通档名义 tick 级定位：mpv 流内 seek → seek_fn → dsvc 重定位。"""
        with self._lock:
            self._assert_dthrough_locked()
            if isinstance(virtual_frame, bool) or not isinstance(virtual_frame, int):
                raise TypeError("virtual_frame must be an integer")
            if virtual_frame < 0:
                raise PreviewEngineError(
                    "SOURCE_FRAME_OUT_OF_RANGE",
                    "virtual_frame is negative",
                    details={"virtual_frame": virtual_frame},
                )
            self._seek_dthrough_tick_locked(virtual_frame)
            return True

    def seek_dthrough_source_frame(self, source_frame: int) -> bool:
        """直通档源帧级定位（GUI 拖轴/步进入口）：台账换算名义 tick。"""
        with self._lock:
            self._assert_dthrough_locked()
            if isinstance(source_frame, bool) or not isinstance(source_frame, int):
                raise TypeError("source_frame must be an integer")
            ledger = self._dthrough_ledger
            if ledger is None:
                raise PreviewEngineError(
                    "DTHROUGH_NOT_ACTIVE", "dthrough ledger is not ready"
                )
            tick = ledger.nominal_tick_for_source_frame(int(source_frame))
            self._seek_dthrough_tick_locked(tick)
            return True

    def _assert_dthrough_locked(self) -> None:
        if self._closed:
            raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")
        if self._mode != "dthrough" or self._dthrough_svc is None:
            raise PreviewEngineError(
                "DTHROUGH_NOT_ACTIVE", "dthrough mode is not active"
            )

    def _seek_dthrough_tick_locked(self, tick: int) -> None:
        player = self._require_player_locked()
        resume_after = self._playing
        if resume_after:
            try:
                player.pause = True
            except Exception:
                try:
                    player.command("set", "pause", "yes")
                except Exception:
                    pass
        fps = self._dthrough_ctx["fps"] if self._dthrough_ctx else 60.0
        seconds = Fraction(tick, 1) / Fraction(max(fps, 1e-6))
        self._command_seek_locked(seconds, exact=True)
        # 台账重定基：新代流从名义 tick 落点起推，实测轴原点=tick
        #（落点源帧=locate 镜像；dsvc.locate 永远全表分割，名义轴不动）
        ledger = self._dthrough_ledger
        if ledger is not None:
            origin_frame = ledger.source_frame_for_nominal_tick(tick)
            if origin_frame is not None:
                ledger.rebase(tick, origin_frame)
                self._source_frame = origin_frame
        if resume_after:
            self._resume_locked()

    def _skim_eligible_locked(self, request: CertifiedEdlRequest) -> bool:
        """Tier2 资格：协议注册成功 + blob 在库 + 代数键前 4 元与本请求
        一致（源 sha × 段表指纹 × 作用域；尾 3 元是构建档位，档位变更由
        GUI 侧清库重 build，不参与播放匹配）。"""
        if not self._skim_ready or self._skim_blob is None or self._skim_key is None:
            return False
        if len(self._skim_key) < 4:
            return False
        expected = (
            str(request.media_info.source_sha256),
            str(request.timeline_plan.fingerprint),
            int(request.project_generation),
            int(request.timeline_revision),
        )
        return tuple(self._skim_key[:4]) == expected

    def _capped_speed_locked(self, rate: float) -> float:
        cap = self._biz_speed_cap
        if cap is None:
            return float(rate)
        return min(float(rate), float(cap))

    def set_skim_blob(self, data: bytearray, key: tuple) -> bool:
        """安装构建完成的 skim blob（后台工作线程调用安全）。

        换代 = 整体换引用（data 入库后不得原地修改）：已打开的流继续读
        open 时快照的旧 blob（引用保活，流关闭后 GC），新 blob 随下一次
        loadfile 生效——_skim_id 自增使 URL 变化，_ensure_loaded 不会误判
        「同一路径已加载」而跳过重载。
        """
        with self._lock:
            if self._closed or not self._skim_ready:
                return False
            if not isinstance(data, (bytearray, memoryview)) or not data:
                raise TypeError("skim blob data must be a non-empty bytearray")
            self._skim_blob = bytearray(data) if isinstance(data, memoryview) else data
            self._skim_key = tuple(key)
            self._skim_id += 1
            return True

    def clear_skim_blob(self) -> None:
        """主动释放 blob（源切换/档位变更/预算超限回落 Tier1）。"""
        with self._lock:
            self._skim_blob = None
            self._skim_key = None
            self._skim_id += 1

    @property
    def skim_key(self) -> tuple | None:
        return self._skim_key

    @property
    def skim_ready(self) -> bool:
        """ram:// 协议注册成功（真绑定 + stream_cb rc=0）。blob 在库与否
        由 skim_key 判断；两者同时成立才有 Tier2 播放资格。"""
        return self._skim_ready

    def _set_speed_locked(self, value: float) -> None:
        player = self._require_player_locked()
        rate = float(value)
        if rate <= 0 or not math.isfinite(rate):
            raise ValueError("playback rate must be positive")
        try:
            player.speed = rate
        except Exception:
            player.command("set", "speed", str(rate))

    def _resume_locked(self) -> None:
        player = self._require_player_locked()
        try:
            player.pause = False
        except Exception:
            player.command("set", "pause", "no")
        self._playing = True
        self._play_end_reason = "playing"
        self._stats["playback_active"] = True
        self._stats["play_end_reason"] = "playing"
        if self._stats.get("play_wall_t0") is None:
            self._stats["play_wall_t0"] = time.perf_counter()
            self._stats["play_wall_end"] = None

    def _freeze_play_wall_locked(self, reason: str) -> None:
        stats = self._stats
        if stats.get("play_wall_t0") is not None and stats.get("play_wall_end") is None:
            stats["play_wall_end"] = time.perf_counter()
        stats["playback_active"] = False
        stats["play_end_reason"] = reason

    def stop(self) -> bool:
        with self._lock:
            if self._closed:
                raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")
            if self._player is None:
                return False
            # A stop issued during an asynchronous load must cancel the
            # queued seek/resume pair; otherwise the later file-loaded event
            # would unexpectedly restart playback.
            self._pending_seek = None
            self._pending_resume = False
            self._pending_speed = None
            try:
                self._player.pause = True
            except Exception:
                try:
                    self._player.command("set", "pause", "yes")
                except Exception:
                    return False
            self._playing = False
            self._play_end_reason = "stop"
            self._freeze_play_wall_locked("stop")
            return True

    def send(self, command: dict[str, Any]) -> bool:
        """Translate the existing VideoIOThread command shape during migration."""

        if not isinstance(command, dict):
            raise TypeError("preview command must be a mapping")
        kind = command.get("type")
        if kind == "quit":
            return self.close()
        with self._lock:
            if self._closed:
                raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")
        if kind in {"seek", "seek_latest"}:
            request = SourceSeekRequest(
                source_frame=int(command.get("frame", 0)),
                canvas_size=tuple(command.get("canvas_wh") or (1, 1)),
                timeline_revision=int(command.get("timeline_revision", self._timeline_revision)),
                exact=self._timeline is not None,
                latest_only=kind == "seek_latest",
            )
            return self.seek_source(request)
        if kind == "stop":
            return self.stop()
        if kind == "set_pace_mode":
            self.set_pace_mode(str(command.get("mode", "opt")))
            return True
        if kind == "play":
            params = dict(command.get("params") or {})
            total = self.total or int(params.get("total_frames", 0) or 0)
            if total <= 0:
                raise PreviewEngineError("FRAME_COUNT_REQUIRED", "mpv preview needs a source frame count")
            plan = TimelinePlan.from_deleted_ranges(
                total,
                params.get("pause_segs") or (),
            )
            speed_multiplier = float(params.get("speed_multiplier", 1.0) or 1.0)
            preview_step = max(1, int(params.get("preview_step", 1) or 1))
            playback_rate = preview_step if abs(speed_multiplier - 1.0) < 1e-9 else 1.0 / speed_multiplier
            request = PreviewPlayRequest(
                start_frame=int(params.get("start_frame", 0) or 0),
                playback_rate=playback_rate,
                preview_step=preview_step,
                speed_multiplier=speed_multiplier,
                skip_trimmed=bool(params.get("skip_trimmed", False)),
                speedup_1x=bool(params.get("speedup_1x", False)),
                speedup_02=bool(params.get("speedup_02", False)),
                speedup_02_factor=max(1, int(params.get("speedup_02_factor", 1) or 1)),
                timeline_plan=plan,
                speed_segments=tuple(tuple(value) for value in params.get("speed_segs") or ()),
                canvas_size=tuple(params.get("canvas_wh") or (1, 1)),
                project_generation=int(params.get("project_generation", self._project_generation)),
                timeline_revision=int(params.get("timeline_revision", self._timeline_revision)),
                preview_step_cap=max(1, int(params.get("preview_step_cap", 3) or 3)),
                skip_trim_min_span=max(0, int(params.get("skip_trim_min_span", 0) or 0)),
            )
            return self.play(request)
        raise PreviewEngineError("COMMAND_UNSUPPORTED", f"unsupported mpv preview command: {kind!r}")

    def set_viewport(self, width: int, height: int, dpi_scale: float = 1.0) -> None:
        if width < 1 or height < 1 or dpi_scale <= 0:
            raise ValueError("viewport dimensions and DPI scale must be positive")
        # The WID child follows the host HWND automatically.  Keep this method
        # explicit so the Tk owner can call it after resize/DPI transitions.
        with self._lock:
            if self._closed:
                raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")
            self._viewport = (int(width), int(height), float(dpi_scale))
            self._enqueue_event(
                {
                    "event": "viewport",
                    "width": int(width),
                    "height": int(height),
                    "dpi_scale": float(dpi_scale),
                    "generation": self._load_generation,
                    "mode": self._mode,
                    "project_generation": self._project_generation,
                    "timeline_revision": self._timeline_revision,
                }
            )

    # ------------------------------------------------------------------
    # Stats/lifecycle
    # ------------------------------------------------------------------
    def _frame_for_position_locked(self, position: float) -> int:
        if self._mode == "dthrough":
            return self._dthrough_frame_for_position_locked(position)
        if self._mode in ("edl", "skim") and self._edl is not None:
            return self._edl.source_frame_for_virtual_time(Fraction(str(max(0.0, position))))
        if self._timeline is None:
            return self._source_frame
        target = Fraction(str(max(0.0, position)))
        times = self._timeline_times
        clean_start = min(self._timeline_clean_start, len(times))
        if clean_start < len(times):
            if clean_start and target < times[clean_start]:
                # The target is still inside B' anomalous head territory.
                # The clean monotonic binary search cannot describe it.
                return min(
                    range(clean_start),
                    key=lambda index: abs(times[index] - target),
                )
            # For the monotonic portion of a VFR timeline, a displayed time
            # belongs to the most recent frame whose PTS is not after it.
            # Nearest-PTS lookup advances the source pointer halfway through
            # a long frame, which is wrong for cut editing and especially
            # visible on variable-duration material.
            # lo= bounds the search without copying the (potentially
            # hundreds of thousands of entries) tail of the PTS table on
            # every time-pos update.
            index = bisect_left(times, target, clean_start)
            if index < len(times) and times[index] == target:
                return index
            return max(clean_start, min(len(times) - 1, index - 1))
        # B' permits only an anomalous source head.  Sorting or deduplicating
        # those rows would falsify the certification, so retain the explicit
        # nearest-row fallback inside that bounded window.
        return min(range(len(times)), key=lambda index: abs(times[index] - target))

    def _dthrough_frame_for_position_locked(self, position: float) -> int:
        """直通档 tp 读回：tick=tp×fps（实测轴）→ 台账 → 源帧。

        每拍顺手排水 dsvc span/eof 实测（禁名义值）并刷新档位速率。
        台账缺失/换载过渡窗回退上一帧号（与源模式自守卫同形）。
        """
        ledger = self._dthrough_ledger
        ctx = self._dthrough_ctx
        if ledger is None or ctx is None:
            return self._source_frame
        try:
            fps = float(ctx.get("fps") or 60.0)
            tick = int(round(max(0.0, position) * fps))
            if self._dthrough_svc is not None:
                actuals = self._dthrough_svc.drain_actuals()
                if actuals is not None:
                    ledger.set_actuals(actuals[0], complete=actuals[1])
            frame = ledger.source_frame_for_tick(tick)
        except (TypeError, ValueError):
            return self._source_frame
        if frame is None:
            return self._source_frame
        try:
            self._dthrough_rate = float(ledger.rate_for_source_frame(frame))
        except (TypeError, ValueError):
            pass
        return frame

    def set_pace_mode(self, mode: str) -> None:
        with self._lock:
            if self._closed:
                raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")
            self._pace_mode = "base" if str(mode).lower() in {"base", "off", "0", "false"} else "opt"
            self._stats["pace_mode"] = self._pace_mode

    def get_pace_mode(self) -> str:
        return self._pace_mode

    def _read_frame_drop_count_locked(self) -> int | None:
        """Read mpv's native drop counter; None when the binding lacks it."""

        player = self._player
        if player is None:
            return None
        for reader in (
            lambda: int(getattr(player, "frame_drop_count")),
            lambda: int(player.command("get_property", "frame-drop-count")),
        ):
            try:
                value = reader()
            except Exception:
                continue
            if value >= 0:
                return value
        return None

    def show_osd_text(self, text: str, duration_ms: int = 600) -> bool:
        """Overlay a short text on the native render surface."""

        if not isinstance(text, str) or not text:
            return False
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
            raise ValueError("duration_ms must be a non-negative integer")
        with self._lock:
            if self._closed:
                raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")
            player = self._player
            if player is None:
                return False
            try:
                player.command("show-text", text, duration_ms)
                return True
            except Exception:
                return False

    def show_osd_corner_text(self, text: str) -> bool:
        """持久右上角 OSD（播放帧率）。见 _osd_overlay。"""
        return self._osd_overlay(1, text, "an9")

    def show_osd_topleft_text(self, text: str) -> bool:
        """持久左上角 OSD（帧号）。暂停时也常驻，不随 show-text 超时消失。"""
        return self._osd_overlay(2, text, "an7")

    def get_osd_texts(self) -> tuple[str, str]:
        """Return (frame_osd_text, fps_osd_text) for copy-to-clipboard."""
        with self._lock:
            return (self._osd_topleft, self._osd_corner)

    def _osd_overlay(self, overlay_id: int, text: str, align: str) -> bool:
        """Persistent OSD overlay on the native render surface.

        ``show-text`` owns a single OSD slot and auto-expires, so both the
        frame number and the FPS readout go through libmpv's ``osd-overlay``
        command with distinct ids.  ``data`` is ASS-override tag + plain text
        (no ``Dialogue:`` prefix — that gets rendered literally).  Pass an
        empty string to clear.
        """

        if not isinstance(text, str):
            return False
        with self._lock:
            if self._closed:
                raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")
            player = self._player
            if player is None:
                return False
            try:
                if not text:
                    player.command("osd-overlay", overlay_id, "none", "", 0, 0, 0)
                    shown = ""
                else:
                    safe = text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
                    player.command(
                        "osd-overlay", overlay_id, "ass-events", f"{{\\{align}}}{safe}", 0, 0, 0
                    )
                    shown = text
                if overlay_id == 1:
                    self._osd_corner = shown
                elif overlay_id == 2:
                    self._osd_topleft = shown
                return True
            except Exception:
                return False

    def snapshot_perf(self) -> dict[str, Any]:
        with self._lock:
            value = dict(self._stats)
            # Native-render fluency: mpv's own drop counter plus the realtime
            # ratio accumulated from clamped time-pos advances.  These feed
            # the same grading the OpenCV engine feeds from its present/lag
            # pipeline; the per-frame ms fields stay zero by design.
            t0 = value.get("play_wall_t0")
            if t0 is not None:
                end = value.get("play_wall_end")
                value["wall_s"] = max(
                    0.0, (end if end is not None else time.perf_counter()) - t0
                )
            advanced = float(value.get("media_advanced_s", 0.0))
            wall = float(value.get("wall_s", 0.0))
            value["rt_ratio"] = (advanced / wall) if wall > 0.5 else None
            drops = self._read_frame_drop_count_locked()
            value["mpv_frame_drops"] = drops
            # 当前 mpv 速率（UI×业务、已封顶后的实际值）：GUI 角标用它
            # 直观展示「0.2x 区域加速此刻是几倍」。直通档 mpv 恒 speed=1
            # （速率在推流端抽帧），报台账实测档位速率（=该段抽帧率）。
            try:
                value["speed"] = float(self._player.speed) if self._player is not None else None
            except Exception:
                value["speed"] = None
            if self._mode == "dthrough" and self._dthrough_rate > 0:
                value["speed"] = float(self._dthrough_rate)
            presented = int(value.get("presented", 0))
            if drops is None:
                value["mpv_drop_pct"] = None
            else:
                value["mpv_drop_pct"] = (
                    100.0 * drops / (presented + drops) if (presented + drops) else 0.0
                )
            value.setdefault("late_pct", 0.0)
            value.setdefault("late1_pct", 0.0)
            value.setdefault("late2_pct", 0.0)
            value.setdefault("drop_pct", value["mpv_drop_pct"] or 0.0)
            value.setdefault("present_ms_avg", 0.0)
            value.update(
                {
                    "engine": self.engine_name,
                    "mode": self._mode,
                    "generation": self._load_generation,
                    "project_generation": self._project_generation,
                    "timeline_revision": self._timeline_revision,
                    # 本代尚未读回位置时报告 None（而非复位值 0）：消费者
                    # （GUI 播放中无条件采信）据此避免把「未读回」误当
                    # 「真的在第 0 帧」→ 红条闪回开头（09-24 review T0）。
                    "source_frame": (
                        self._source_frame
                        if self._last_time_pos is not None
                        else None
                    ),
                    "time_pos": self._last_time_pos,
                    "playing": self._playing,
                    "playback_active": self._playing,
                    "play_end_reason": self._play_end_reason,
                    "event_queue_drops": self._event_drop_count,
                    "duration": self._duration,
                    "viewport": self._viewport,
                    "preview_bias_frames": (
                        self._edl.preview_bias_frames
                        if self._mode in ("edl", "skim") and self._edl is not None
                        else 0
                    ),
                    "preview_bias_reason": (
                        self._edl.preview_bias_reason
                        if self._mode in ("edl", "skim") and self._edl is not None
                        else None
                    ),
                    "preview_tick_collision_frames": (
                        self._edl.preview_tick_collision_frames
                        if self._mode in ("edl", "skim") and self._edl is not None
                        else ()
                    ),
                }
            )
            return value

    def snapshot(self) -> dict[str, Any]:
        return self.snapshot_perf()

    def reset_perf_stats(self) -> None:
        with self._lock:
            if self._closed:
                raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")
            self._stats = self._new_stats()
            self._stats["pace_mode"] = self._pace_mode

    def begin_perf_segment(self) -> None:
        with self._lock:
            if self._closed:
                raise PreviewEngineError("ENGINE_CLOSED", "preview engine is closed")
            self.reset_perf_stats()
            self._stats["playback_active"] = self._playing

    def is_playback_active(self) -> bool:
        with self._lock:
            return bool(self._playing)

    def is_alive(self) -> bool:
        with self._lock:
            # A timed-out close keeps the native player owned by this engine so
            # the caller can retry termination.  Report that resource as alive
            # until the terminate worker has actually finished, even though
            # ``_closed`` rejects all new playback commands.
            return self._player is not None

    def close(self, timeout: float = 1.0) -> bool:
        wait_s = max(0.0, float(timeout))
        with self._lock:
            if self._closed and self._player is None:
                return True
            player = self._player
            self._closed = True
            self._playing = False
            self._play_end_reason = "shutdown"
            self._loaded_ready = False
            self._pending_seek = None
            self._pending_resume = False
            self._pending_speed = None
            self._stats["playback_active"] = False
            self._stats["play_end_reason"] = "shutdown"
            # P1-RAM：释放 blob 内存并摘除 stream_cb 注册表；仍在飞行的
            # open 回调读到空引用返回错误码（加载失败安全回落），已打开
            # 的流靠回调闭包里的快照引用保活到 terminate 完成。
            self._skim_blob = None
            self._skim_key = None
            _SKIM_ENGINES.pop(id(self), None)
            # 解码直通：摘注册表 + 关停 dsvc 子进程（防止孤儿进程）
            _DTHROUGH_ENGINES.pop(id(self), None)
            self._dthrough_ready = False
            self._dthrough_ctx = None
            self._dthrough_ledger = None
            self._dthrough_rate = 0.0
            svc = self._dthrough_svc
            self._dthrough_svc = None
            if player is None:
                handle = self._dll_handle
                self._dll_handle = None
                # There is no native player to terminate.  Release any DLL
                # search-path handle acquired before a factory/bootstrap error.
                if svc is not None:
                    try:
                        svc.close()
                    except Exception:
                        pass
                if handle is not None:
                    try:
                        handle.close()
                    except Exception:
                        pass
                return True

            if not self._callbacks_detached:
                for name, callback in tuple(self._property_callbacks):
                    try:
                        player.unobserve_property(callback)
                    except TypeError:
                        try:
                            player.unobserve_property(name, callback)
                        except Exception:
                            pass
                    except Exception:
                        pass
                self._property_callbacks.clear()
                if self._event_callback is not None:
                    try:
                        player.unregister_event_callback(self._event_callback)
                    except Exception:
                        pass
                self._callbacks_detached = True

            done = self._terminate_done
            thread = self._terminate_thread
            if done is None or thread is None:
                done = threading.Event()
                self._terminate_done = done
                self._terminate_error = None

                def terminate() -> None:
                    try:
                        player.terminate()
                    except BaseException as exc:
                        with self._lock:
                            self._terminate_error = exc
                    finally:
                        done.set()

                thread = threading.Thread(
                    target=terminate,
                    name="arknight-mpv-close",
                    daemon=True,
                )
                self._terminate_thread = thread
                thread.start()

        thread.join(wait_s)
        if not done.is_set():
            # Keep the player and termination state so a later close() can
            # retry the same native instance instead of falsely reporting a
            # successful close after a timed-out first attempt.
            return False
        with self._lock:
            terminate_error = self._terminate_error
            if terminate_error is not None:
                # Keep ownership and permit a later close retry.  Native
                # bindings occasionally surface a transient shutdown error.
                self._terminate_thread = None
                self._terminate_done = None
                self._terminate_error = None
                return False
        with self._lock:
            if self._terminate_done is done:
                self._player = None
                self._started = False
                self._terminate_thread = None
                self._terminate_done = None
                self._terminate_error = None
                handle = self._dll_handle
                self._dll_handle = None
            else:
                handle = None
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
        return True


__all__ = ["MpvEngine"]
