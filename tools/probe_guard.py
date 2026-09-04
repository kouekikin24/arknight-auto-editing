#!/usr/bin/env python3
"""测量卫生硬机制（R3 的机械版）：探针开头调用，环境不达标直接拒跑。

防"跨双屏被 DWM 混合 DPI 重采样"这类污染。用法：
    from probe_guard import assert_measurement_hygiene
    assert_measurement_hygiene(root_geometry=(2400, 60, 1380, 920))
"""
from __future__ import annotations
import ctypes
from ctypes import wintypes


def _monitors():
    user32 = ctypes.windll.user32
    shcore = ctypes.windll.shcore
    out = []
    def cb(hMon, hdc, pRect, data):
        r = pRect.contents
        out.append((r.left, r.top, r.right, r.bottom))
        return True
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.POINTER(ctypes.c_int),
                              ctypes.POINTER(ctypes.c_int),
                              ctypes.POINTER(wintypes.RECT),
                              ctypes.POINTER(ctypes.c_int))(cb)
    user32.EnumDisplayMonitors(None, None, proc, 0)
    scales = []
    for m in out:
        hmon = user32.MonitorFromPoint(
            wintypes.POINT((m[0] + m[2]) // 2, (m[1] + m[3]) // 2), 2)
        d = ctypes.c_uint()
        try:
            shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(d), ctypes.byref(d))
            scales.append(d.value / 96)
        except Exception:
            scales.append(1.0)
    return out, scales


def assert_measurement_hygiene(root_geometry=None):
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    mons, scales = _monitors()
    primary_right = max(m[2] for m in mons)
    info = {"monitors": mons, "scales": scales,
            "primary_right": primary_right}
    if root_geometry is not None:
        x, y, w, h = root_geometry
        inside = [m for m in mons if m[0] <= x and x + w <= m[2]]
        if not inside:
            raise SystemExit(
                f"[probe_guard] 拒跑：窗口 x={x}..{x+w} 未完整落在任何单屏内"
                f"（跨屏会被 DWM 混合 DPI 重采样污染抓取）。monitors={mons}。"
                f"请把探针窗口完整放进一块屏。")
        info["window"] = (x, y, w, h)
        info["on_monitor"] = inside[0]
    print(f"[probe_guard] OK monitors={mons} scales={scales} "
          f"window={root_geometry}", flush=True)
    return info


def require_lossless(path: str):
    if not str(path).lower().endswith(".png"):
        raise SystemExit(f"[probe_guard] 拒跑：{path} 非无损 PNG（JPEG 噪声会"
                         f"掩埋差异）。测量必须无损。")
