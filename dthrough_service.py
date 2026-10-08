"""dthrough_service.py — dsvc.exe（Rust 解码直通服务）协议客户端（一期 1b）。

协议（T0，源=.cache/v3ass/dsvc/src/main.rs）：
  stdin  = JSONL 命令（load/seek/stop/quit）
  stdout = 裸 I420 帧字节流（w*h*1.5 定长，无帧头；帧边界由定长切分）
  stderr = JSONL 事件（gen/loaded/span/seek_ok/eof/error）

世代号（gen）贯穿两端：load/seek 各自增一代；旧代帧数据与事件全部作废。
背压：stdout 管道写阻塞 + 客户端有界队列（满则 reader 停读 → dsvc 写阻塞），
消费端（mpv stream_cb read 回调）按 CFR 节奏取帧，产能天然有界。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time as _time
from collections import deque
from typing import Any, Callable

DEFAULT_EXE = r".cache\v3ass\dsvc\target\release\dsvc.exe"
DEFAULT_FFMPEG_BIN = (
    r".cache\v3ass\ffmpeg-dev\ffmpeg-7.1.1-full_build-shared\bin"
)

# 队列字节上限（对齐 probe_dthrough_e2e CAP=160MB 的背压实测形态）
QUEUE_CAP_BYTES = 160 * 1024 * 1024


class DThroughServiceError(RuntimeError):
    """dsvc 进程/协议异常。"""


class DThroughService:
    """单实例 = 单 dsvc 子进程；close() 前可多次 load/seek（换代复用同一进程）。"""

    def __init__(
        self,
        exe_path: str | None = None,
        *,
        ffmpeg_bin: str | None = None,
        width: int = 1920,
        height: int = 1080,
        queue_cap: int = QUEUE_CAP_BYTES,
    ) -> None:
        base = os.path.dirname(os.path.abspath(__file__))
        self._exe = os.path.join(base, exe_path or DEFAULT_EXE)
        if not os.path.isfile(self._exe):
            raise DThroughServiceError(
                "DSVC_MISSING", f"dsvc executable not found: {self._exe}"
            )
        self._ffmpeg_bin = os.path.join(base, ffmpeg_bin or DEFAULT_FFMPEG_BIN)
        self._frame_size = width * height * 3 // 2
        self._queue_cap = queue_cap

        self._chunks: deque[bytes] = deque()
        self._chunk_bytes = 0
        self._cond = threading.Condition()
        self._eof = False            # stdout 关闭（进程退出）
        self._stream_done = False    # dsvc eof 事件（本代流推完；进程仍存活待 seek）
        self._gen = 0
        self._alive = True

        self._events: deque[dict[str, Any]] = deque(maxlen=256)
        self._evt_cond = threading.Condition()
        self._loaded_kfs: int | None = None
        self._per_span: list[int] | None = None
        self._last_error: str | None = None
        # 当前代实测 per_span（绝对段号索引）：span 事件增量 + eof 全量
        self._span_counts: list[int] = []
        self._prev_pushed = 0
        self._counts_complete = False
        # 各段进入方式（si -> "cont"/"restart"，1d span 事件新字段；缺省视为 restart）
        self._span_modes: dict[int, str] = {}

        env = dict(os.environ)
        if os.path.isdir(self._ffmpeg_bin):
            env["PATH"] = self._ffmpeg_bin + os.pathsep + env.get("PATH", "")
        self._proc = subprocess.Popen(
            [self._exe],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self._reader = threading.Thread(target=self._read_frames, daemon=True)
        self._err = threading.Thread(target=self._read_events, daemon=True)
        self._reader.start()
        self._err.start()

    # ---- 队列（mpv stream_cb read 回调消费） ----

    def pop_chunk(self, max_bytes: int, timeout: float) -> bytes | None:
        """取至多 max_bytes 字节；EOF 且队列空返回 b""；超时无数据返回 None。

        头块超出 max_bytes 时就地切片（返回尺寸永不超限——mpv 的 read
        回调按返回长度写入调用方缓冲，超写=踩内存 segfault）。
        """
        with self._cond:
            while self._alive and not self._chunks and not self._eof and not self._stream_done:
                if not self._cond.wait(timeout):
                    return None
            if self._chunks:
                head = self._chunks[0]
                take_n = min(len(head), max_bytes)
                part = head[:take_n]
                if take_n == len(head):
                    self._chunks.popleft()
                else:
                    self._chunks[0] = head[take_n:]
                self._chunk_bytes -= take_n
                self._cond.notify_all()
                return part
            return b"" if (self._eof or self._stream_done) else None

    def flush_chunks(self) -> None:
        """清空队列（seek 换位时丢弃旧位流残帧）。"""
        with self._cond:
            self._chunks.clear()
            self._chunk_bytes = 0
            self._cond.notify_all()

    def wait_drained(self, timeout: float) -> bool:
        with self._cond:
            return self._cond.wait_for(
                lambda: not self._chunks or self._eof or not self._alive, timeout
            )

    # ---- 命令 ----

    def _send(self, obj: dict[str, Any]) -> None:
        if self._proc.stdin is None or self._proc.poll() is not None:
            raise DThroughServiceError("DSVC_DEAD", "dsvc process is not running")
        try:
            self._proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
            self._proc.stdin.flush()
        except OSError as exc:
            raise DThroughServiceError("DSVC_IO", f"stdin write failed: {exc}") from exc

    def load(self, src: str, spans: list[list[float]], fps: float = 60.0) -> int:
        """装载段表并从头推流；返回新一代号。spans=[start,end,rate] 源秒。"""
        with self._cond:
            self._chunks.clear()
            self._chunk_bytes = 0
            self._eof = False
            self._stream_done = False
        self._loaded_kfs = None
        self._per_span = None
        self._last_error = None
        with self._evt_cond:
            self._span_counts = []
            self._prev_pushed = 0
            self._counts_complete = False
            self._span_modes = {}
        self._send(
            {"op": "load", "src": src, "spans": spans, "fps": fps, "mode": "frames"}
        )
        self._gen += 1
        return self._gen

    def seek(self, virtual_frame: int, src: str, spans: list[list[float]], fps: float = 60.0) -> int:
        """虚拟帧级重定位（帧精确）；返回新一代号。"""
        with self._cond:
            self._chunks.clear()
            self._chunk_bytes = 0
            self._eof = False
            self._stream_done = False
        self._last_error = None
        with self._evt_cond:
            self._per_span = None
            self._span_counts = []
            self._prev_pushed = 0
            self._counts_complete = False
            self._span_modes = {}
        self._send(
            {
                "op": "seek",
                "virtual_frame": int(virtual_frame),
                "src": src,
                "spans": spans,
                "fps": fps,
                "mode": "frames",
            }
        )
        self._gen += 1
        return self._gen

    def stop(self) -> None:
        try:
            self._send({"op": "stop"})
        except DThroughServiceError:
            pass

    # ---- 事件 ----

    def wait_event(self, pred: Callable[[dict[str, Any]], bool], timeout: float,
                   since_gen: int | None = None) -> dict[str, Any] | None:
        gate = threading.Condition()
        with gate:
            end = _time.monotonic() + timeout
            while True:
                for e in list(self._events):
                    if pred(e) and (since_gen is None or e.get("gen", 0) >= since_gen):
                        return e
                remaining = end - _time.monotonic()
                if remaining <= 0:
                    return None
                gate.wait(min(0.05, remaining))

    def latest_gen(self) -> int:
        return self._gen

    def drain_actuals(self) -> tuple[list[int], bool] | None:
        """当前代实测 per_span（绝对段号索引）+ 是否 eof 全量；尚无数据返回 None。

        双源：span 事件增量（播放中逐段到达）/ eof 全量（最权威）。
        供直通台账精化读回映射（1c-①，禁名义值）。
        """
        with self._evt_cond:
            if not self._span_counts and not self._counts_complete:
                return None
            return list(self._span_counts), self._counts_complete

    def eof_seen(self) -> bool:
        with self._cond:
            return self._eof or self._stream_done

    def span_modes(self) -> dict[int, str]:
        """当前代各段进入方式快照（si -> "cont"/"restart"）；探针/诊断用。

        事件缺 mode 字段（旧 dsvc）时该段不入表，调用方按 restart 兜底。
        """
        with self._evt_cond:
            return dict(self._span_modes)

    # ---- 线程 ----

    def _read_frames(self) -> None:
        proc = self._proc
        out = proc.stdout
        assert out is not None
        tail = b""
        try:
            while self._alive:
                chunk = out.read(1 << 22)
                if not chunk:
                    break
                buf = tail + chunk
                nf = len(buf) // self._frame_size
                if nf == 0:
                    tail = buf
                    continue
                cut = nf * self._frame_size
                with self._cond:
                    while self._alive and self._chunk_bytes > self._queue_cap:
                        self._cond.wait(0.05)
                    if not self._alive:
                        return
                    self._chunks.append(buf[:cut])
                    self._chunk_bytes += cut
                    self._cond.notify_all()
                tail = buf[cut:]
        except OSError:
            pass
        finally:
            with self._cond:
                self._eof = True
                self._cond.notify_all()

    def _read_events(self) -> None:
        proc = self._proc
        err = proc.stderr
        assert err is not None
        try:
            for line in iter(err.readline, b""):
                line = line.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    e = {"ev": "raw", "msg": line}
                self._events.append(e)
                self._handle_event(e)
        except OSError:
            pass

    def _handle_event(self, e: dict[str, Any]) -> None:
        """单条 stderr 事件处理（独立成方法以便免进程单测 1d）。"""
        ev = e.get("ev")
        try:
            ev_gen = int(e.get("gen", -1))
        except (TypeError, ValueError):
            ev_gen = -1
        if ev == "loaded":
            self._loaded_kfs = e.get("kfs")
        elif ev == "span" and ev_gen == self._gen:
            # 进入段 si 时已推 pushed 帧 ⇒ 段 si-1 实推数 = 增量；
            # 间隙（零帧段）补 0
            with self._evt_cond:
                try:
                    si = int(e.get("si", 0))
                    pushed = int(e.get("pushed", 0))
                except (TypeError, ValueError):
                    si = None
                if si is not None:
                    while len(self._span_counts) < si:
                        if len(self._span_counts) == si - 1:
                            self._span_counts.append(
                                max(0, pushed - self._prev_pushed)
                            )
                        else:
                            self._span_counts.append(0)
                    self._prev_pushed = pushed
                    mode = e.get("mode")
                    if isinstance(mode, str) and mode in ("cont", "restart"):
                        self._span_modes[si] = mode
        elif ev == "eof" and ev_gen == self._gen:
            # gen 门：旧代尾 eof（seek 竞态）不得把新流误标 EOF
            self._per_span = e.get("per_span")
            with self._evt_cond:
                try:
                    self._span_counts = [
                        max(0, int(v)) for v in (self._per_span or [])
                    ]
                except (TypeError, ValueError):
                    self._span_counts = []
                self._prev_pushed = sum(self._span_counts)
                self._counts_complete = True
                with self._cond:
                    self._stream_done = True
                    self._cond.notify_all()
        elif ev == "error":
            self._last_error = e.get("msg")

    # ---- 清理 ----

    def close(self) -> None:
        if not self._alive:
            return
        self._alive = False
        try:
            self._send({"op": "quit"})
        except DThroughServiceError:
            pass
        try:
            self._proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        for f in (self._proc.stdin, self._proc.stdout, self._proc.stderr):
            try:
                if f is not None:
                    f.close()
            except OSError:
                pass
        with self._cond:
            self._chunks.clear()
            self._chunk_bytes = 0
            self._eof = True
            self._cond.notify_all()
