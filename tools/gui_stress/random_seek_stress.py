#!/usr/bin/env python3
"""随机跳转压测（常驻工具，R8 同源；真 Tk 事件注入 + 帧号/延迟实时读取）。

用法：`python tools/gui_stress/random_seek_stress.py`
  可选环境变量：STRESS_SEED / STRESS_N / STRESS_SRC（默认 4.mp4）


**为什么做**：owner 问「能否自己随机跳转并实时读帧数和响应时间」——本探针
就是这件事的落地。与既有探针的关键区别：

  · **真事件层**：用 `canvas.event_generate("<Button-1>"/"<B1-Motion>"/"<ButtonRelease-1>")`
    注入，走 Tk 真实绑定链（`_on_mousedown`/`_on_mousemove`/`_on_mouseup`），
    而不是直接调回调——后者会漏掉绑定层与事件顺序问题（09-24「点击后红条
    冻死」这类状态机缺陷正是绑定层才暴露）。
  · **真 GUI**：驱动 `main.py` 的 `main()`（R8 同源），真分析缓存段表。
  · **随机 + 可复现**：固定 seed，可重跑对比。
  · **实时读数**：每次跳转后采样 `current_frame_idx`（红条数据源）与
    `_io.snapshot_perf()`（引擎侧 tp/帧号），测「落点偏差」与「响应延迟」。

**测量项（每点）**：
  target     = 点击换算出的目标帧
  landed     = 跳转稳定后的 UI 帧号
  err        = landed - target（网格量化容差内应为小值）
  latency_ms = 从 mouseup 到「UI 帧号首次接近 target」的墙钟
  moved      = 跳转后 1.2s 内红条是否继续推进（抓「冻死」类缺陷）
  flags      = _is_dragging / timeline.active_handle（抓状态残留）
"""
from __future__ import annotations

import json
import os
import random
import statistics
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "tools"))
sys.path.insert(0, str(_REPO / ".cache" / "v3ass"))

SRC = Path(r"D:\qq下载\920\4.mp4")
OUT = _REPO / ".cache" / "v3ass" / "gui_stress_results.json"
os.environ.setdefault("ARKNIGHT_PREVIEW_ENGINE", "mpv")

SEED = int(os.environ.get("STRESS_SEED", "20260924"))
N_SEEKS = int(os.environ.get("STRESS_N", "24"))


def find_player(root):
    from preview_player import VideoPreviewPlayer
    stack = [root]
    while stack:
        w = stack.pop()
        if isinstance(w, VideoPreviewPlayer):
            return w
        try:
            stack.extend(w.winfo_children())
        except Exception:
            pass
    return None


def main():
    import probe_guard
    probe_guard.assert_measurement_hygiene()
    _dll = _REPO / "tools" / "libmpv" / "dll"
    os.environ["PATH"] = str(_dll) + os.pathsep + os.environ.get("PATH", "")
    try:
        os.add_dll_directory(str(_dll))
    except Exception:
        pass
    import tkinter as tk
    import tkinter.messagebox as _mb
    import main as app_main

    captured = []
    _mb.showinfo = lambda title=None, message=None, *a, **k: captured.append(("info", str(title)))
    _mb.showwarning = lambda title=None, message=None, *a, **k: captured.append(("warning", str(title)))
    _mb.showerror = lambda title=None, message=None, *a, **k: captured.append(("error", str(title)))
    _mb.askyesno = lambda title=None, message=None, *a, **k: (captured.append(("askyesno", str(title))) or True)

    results = {}

    def rec(k, v):
        results[k] = v
        print("[{}] {}".format(k, json.dumps(v, ensure_ascii=False, default=str)),
              flush=True)

    orig = tk.Tk.mainloop

    def hooked(self, *a, **kw):
        pl = find_player(self)
        if pl is None:
            results["_error"] = "player not found"
        else:
            try:
                run_checks(pl, rec)
            except Exception as e:
                import traceback
                results["_error"] = "{}: {}".format(type(e).__name__, e)
                results["_trace"] = traceback.format_exc()[-2500:]
        results["dialogs"] = captured
        try:
            OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1, default=str),
                           encoding="utf-8")
            print("=== WRITTEN ===", flush=True)
        except Exception as exc:
            print("write failed:", exc, flush=True)
        try:
            self.destroy()
        except Exception:
            pass

    tk.Tk.mainloop = hooked
    try:
        app_main.main()
    finally:
        tk.Tk.mainloop = orig
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print("=== DONE ===", flush=True)


def run_checks(pl, rec):
    root = pl.winfo_toplevel()

    def pump_until(pred, timeout_s, tick=0.02):
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < timeout_s:
            root.update()
            if pred():
                return time.perf_counter() - t0, True
            time.sleep(tick)
        root.update()
        return time.perf_counter() - t0, bool(pred())

    # ---- 装载 + 分析（走真实缓存） ----
    assert pl.load_video(str(SRC)) is not False
    _, ok = pump_until(lambda: pl.media_info is not None, 600.0)
    rec("media_ready", {"ok": bool(ok)})
    if not ok:
        return
    pl._start_analysis()
    _, done = pump_until(
        lambda: pl.states_array is not None and pl._analysis_handle is None, 300.0)
    rec("analysis", {"done": bool(done), "pauses": len(pl.pause_segments)})
    if not done:
        return

    tl = pl.timeline
    total = int(pl.total_frames)
    # 等时间轴画布真实映射（event_generate 需要真实几何；withdraw 下无效）
    root.update()
    for _ in range(40):
        if tl.canvas.winfo_width() > 50 and tl.canvas.winfo_height() > 20:
            break
        root.update()
        time.sleep(0.05)
    cw = tl.canvas.winfo_width()
    ch = tl.canvas.winfo_height()
    rec("canvas", {"w": cw, "h": ch, "viewable": bool(tl.canvas.winfo_viewable())})
    if cw <= 50:
        # event_generate 依赖真实映射的窗口（Tk 实测：withdraw/未映射时
        # 事件不投递，绑定不触发）——故本工具必须跑在可见窗口上，
        # 不能在 CI/headless 里用。单测层改用直调回调 + 状态断言。
        rec("_error", "canvas 未映射（event_generate 需真实窗口）；"
                      "请在可见桌面会话运行本工具")
        return

    # 手柄命中带（Y 32..h-20）——用带内 Y 以覆盖「候选分支」这条最险路径
    y_handle = 45 if ch >= 70 else max(10, ch // 2)
    rec("geometry", {"handle_band_y": y_handle})

    def real_click(x, *, y=None, state=0, drag_px=0, steps=3):
        """真事件注入：按下 →（可选拖动）→ 松开，走 Tk 真实绑定链。"""
        y = y if y is not None else y_handle
        tl.canvas.event_generate("<Button-1>", x=int(x), y=int(y), state=state)
        root.update()
        if drag_px:
            for i in range(1, steps + 1):
                xm = int(x + drag_px * i / steps)
                tl.canvas.event_generate("<B1-Motion>", x=xm, y=int(y), state=state)
                root.update()
                time.sleep(0.01)
        x_end = int(x + drag_px)
        tl.canvas.event_generate("<ButtonRelease-1>", x=x_end, y=int(y), state=state)
        root.update()

    def ui_frame():
        return int(pl.current_frame_idx)

    def engine_state():
        try:
            s = pl._io.snapshot_perf()
            return {"sf": s.get("source_frame"), "tp": s.get("time_pos"),
                    "playing": bool(s.get("playing"))}
        except Exception:
            return {}

    def flags():
        return {
            "is_dragging": bool(getattr(pl, "_is_dragging", False)),
            "active_handle": getattr(tl, "active_handle", None),
            "pending_cands": len(getattr(tl, "_pending_candidates", []) or []),
        }

    def click_and_measure(frac, *, label):
        """单击 → 测落点/延迟/后续推进。"""
        x = max(1, min(cw - 2, int(cw * frac)))
        target = int(tl._x2f(x, cw))
        before = ui_frame()
        t0 = time.perf_counter()
        real_click(x)
        # 等 UI 帧号首次进入目标 ±3%（或超时）
        tol = max(30, int(total * 0.03))
        lat, hit = None, False
        while time.perf_counter() - t0 < 3.0:
            root.update()
            f = ui_frame()
            if abs(f - target) <= tol:
                lat = (time.perf_counter() - t0) * 1000
                hit = True
                break
            time.sleep(0.01)
        time.sleep(0.25)
        root.update()
        landed = ui_frame()
        # 跳转后是否继续推进（暂停态不推进属正常，这里先不播）
        return {
            "label": label,
            "frac": round(frac, 4),
            "target": target,
            "before": before,
            "landed": landed,
            "err": landed - target,
            "latency_ms": round(lat, 1) if lat is not None else None,
            "hit_tol": hit,
            "flags": flags(),
        }

    def click_and_measure_playing(frac, *, label):
        """播放中单击 → 测落点 + 之后红条是否继续动（抓冻死类缺陷）。"""
        x = max(1, min(cw - 2, int(cw * frac)))
        target = int(tl._x2f(x, cw))
        real_click(x)
        t0 = time.perf_counter()
        seq = []
        while time.perf_counter() - t0 < 2.0:
            root.update()
            seq.append(ui_frame())
            time.sleep(0.02)
        tail = seq[-8:]
        return {
            "label": label,
            "frac": round(frac, 4),
            "target": target,
            "seq_head": seq[:6],
            "seq_tail": tail,
            "advanced_after": (max(tail) - min(tail)) if tail else 0,
            "flags": flags(),
        }

    # ---- 阶段 1：暂停态随机跳转（落点精度 + 延迟） ----
    rng = random.Random(SEED)
    pl.preview_speed_var.set("1x")
    rows = []
    for i in range(N_SEEKS):
        frac = rng.uniform(0.02, 0.98)
        row = click_and_measure(frac, label=f"paused#{i}")
        rows.append(row)
        if i < 3:
            rec(f"sample_paused#{i}", row)
    errs = [r["err"] for r in rows]
    lats = [r["latency_ms"] for r in rows if r["latency_ms"] is not None]
    rec("P1_paused_random_seek", {
        "n": len(rows),
        "err_abs_max": max(abs(e) for e in errs),
        "err_abs_mean": round(statistics.fmean(abs(e) for e in errs), 1),
        "latency_ms_p50": round(statistics.median(lats), 1) if lats else None,
        "latency_ms_max": round(max(lats), 1) if lats else None,
        "misses": [r for r in rows if not r["hit_tol"]][:3],
        "stuck_flags": [r for r in rows if r["flags"]["is_dragging"]][:3],
    })

    # ---- 阶段 2：播放中随机跳转（红条是否继续跟随） ----
    pl.is_playing = True
    pl._send_play(pl.current_frame_idx)
    time.sleep(1.0)
    prows = []
    for i in range(8):
        frac = rng.uniform(0.05, 0.95)
        row = click_and_measure_playing(frac, label=f"playing#{i}")
        prows.append(row)
        if i < 3:
            rec(f"sample_playing#{i}", row)
    rec("P2_playing_random_seek", {
        "n": len(prows),
        "advanced_after_min": min(r["advanced_after"] for r in prows),
        "frozen_cases": [r for r in prows if r["advanced_after"] == 0][:3],
        "stuck_flags": [r for r in prows if r["flags"]["is_dragging"]][:3],
    })

    # ---- 阶段 3：拖拽端点（真 B1-Motion）后红条是否仍跟随 ----
    pl._stop_playback_ui(from_user=True)
    root.update()
    time.sleep(0.3)
    x_drag = int(cw * 0.5)
    real_click(x_drag, drag_px=40)
    time.sleep(0.3)
    root.update()
    rec("P3_drag_endpoint", {
        "flags_after_drag": flags(),
        "frame": ui_frame(),
    })

    pl.is_playing = True
    pl._send_play(pl.current_frame_idx)
    t0 = time.perf_counter()
    seq = []
    while time.perf_counter() - t0 < 1.5:
        root.update()
        seq.append(ui_frame())
        time.sleep(0.02)
    rec("P3_after_drag_play", {
        "advanced": (max(seq) - min(seq)) if seq else 0,
        "flags": flags(),
        "head": seq[:5],
    })
    pl._stop_playback_ui(from_user=True)
    root.update()


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
