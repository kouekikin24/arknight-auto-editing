from __future__ import annotations

from types import SimpleNamespace
import unittest

import numpy as np

from edit_commands import SetClipBounds, SetPauseMaskRun
from timeline_widget import TimelineWidget, _PAUSE_BAND_Y1

TIMELINE_BAND_Y = _PAUSE_BAND_Y1 + 5  # 落在暂停段带内


class _CanvasStub:
    def winfo_width(self) -> int:
        return 100


class TimelineWidgetCommandTests(unittest.TestCase):
    def _widget_stub(self) -> TimelineWidget:
        widget = TimelineWidget.__new__(TimelineWidget)
        widget.total_frames = 4
        widget.fps = 60.0
        widget.scroll_offset = 0.0
        widget.zoom_level = 1.0
        widget.pause_segments = [
            {
                "id": 1,
                "start": 0,
                "end": 3,
                "mode": "auto",
                "local_del_mask": np.asarray([0, 0, 1, 1], dtype=np.uint8),
            }
        ]
        widget.clip_segments = [
            {"id": 2, "start": 0, "end": 3, "keep_in": 1, "keep_out": 2}
        ]
        widget._clip_preview_bounds = {}
        widget._pause_mask_preview = {}
        widget._pending_candidates = []
        widget._mousedown_x = 0
        widget._edit_changed = False
        widget.active_handle = None
        widget.canvas = _CanvasStub()
        widget.mark_dirty = lambda: None
        widget._draw_dynamic = lambda: None
        return widget

    def _click_widget(self, ctrl: bool = False):
        w = self._widget_stub()
        w.current_frame_idx = 0  # 指针在 x=0，点击 x=50 不会命中指针拖拽
        w.selected_pause_id = None
        w.on_seek_cb = lambda tf: setattr(w, "_seeked_to", tf)
        w.on_pause_select_cb = lambda sid: setattr(w, "_picked", sid)
        w.canvas = SimpleNamespace(winfo_width=lambda: 100, focus_set=lambda: None)
        ev = SimpleNamespace(x=50, y=TIMELINE_BAND_Y, state=(0x0004 if ctrl else 0))
        w._on_mousedown(ev)
        return w

    def test_plain_click_seeks_instead_of_selecting_segment(self) -> None:
        # P1-C（09-16 真机 T0）：裸单击落在暂停段带内=跳转（旧行为被选段吞掉，
        # 报「点不到」）；选段须显式 Ctrl+单击。
        w = self._click_widget(ctrl=False)
        self.assertTrue(hasattr(w, "_seeked_to"))
        self.assertFalse(hasattr(w, "_picked"))

    def test_plain_click_fallback_must_end_drag(self) -> None:
        """点击回落为 seek 时必须调 on_handle_end_cb（09-24 真机 T0）。

        根因：GUI 的 `on_seek_cb`（`_on_tl_seek`）置 `_is_dragging=True`，
        只有 `on_handle_end_cb`（`_on_tl_drag_end`）复位。原实现在「悬停
        候选但未拖动」分支只调前者就 return → `_is_dragging` 永久 True →
        渲染循环的 `_apply_native_perf` 被永久跳过 → **红条冻死不再随播放
        移动**（owner 实测：点击跳转后红条停在 0）。
        """
        w = self._widget_stub()
        # 密集夹具（复刻真实项目 ~1.5px 间距）：多点段，点击处必落在手柄
        # 命中带内 → 产生候选，走「点击回落」分支。
        w.total_frames = 1000
        w.pause_segments = [
            {"id": i, "start": i * 10, "end": i * 10 + 8, "mode": "auto",
             "local_del_mask": np.asarray([0] * 9, dtype=np.uint8)}
            for i in range(100)
        ]
        w.clip_segments = [
            {"id": 1000 + i, "start": i * 10, "end": i * 10 + 8,
             "keep_in": i * 10, "keep_out": i * 10 + 8}
            for i in range(100)
        ]
        w.current_frame_idx = 0
        w.selected_pause_id = None
        w.on_seek_cb = lambda tf: setattr(w, "_seeked_to", tf)
        w.on_pause_select_cb = lambda sid: None
        w.on_handle_end_cb = lambda: setattr(w, "_drag_ended", True)
        w.canvas = SimpleNamespace(winfo_width=lambda: 100, focus_set=lambda: None)
        # 手柄命中带（_CLIP_HANDLE_Y1=32 .. TL_HEIGHT-20=60）：候选只在此带产生
        ev = SimpleNamespace(x=50, y=45, state=0)

        w._on_mousedown(ev)          # 密集区：必然产生手柄候选
        self.assertTrue(w._pending_candidates, "夹具应命中候选分支")
        w._on_mouseup(ev)

        self.assertTrue(hasattr(w, "_seeked_to"), "点击应跳转")
        self.assertTrue(getattr(w, "_drag_ended", False),
                        "点击回落分支必须调 on_handle_end_cb（否则拖动标志卡死）")
        self.assertEqual(w._pending_candidates, [])
        self.assertIsNone(w.active_handle)

    def test_every_mouse_path_closes_drag_state(self) -> None:
        """**所有**鼠标路径都必须以 `on_handle_end_cb` 收尾（09-24 事故教训）。

        `on_seek_cb`（GUI `_on_tl_seek`）置 `_is_dragging=True`，只有
        `on_handle_end_cb` 复位。09-24 事故=某条早退分支漏调收尾 → 标志永久
        为真 → 红条冻死。本用例遍历四条典型路径，统一断言「松开后必须收尾」。

        路径：①裸击段带（无候选）②裸击手柄带（有候选→回落 seek）
              ③Ctrl+击（选段）④红条拖拽（active_handle='red'）
        """
        cases = {
            "plain_band_no_cand": dict(x=50, y=TIMELINE_BAND_Y, state=0, move=None),
            "plain_handle_fallback": dict(x=50, y=45, state=0, move=None),
            "ctrl_select": dict(x=50, y=TIMELINE_BAND_Y, state=0x0004, move=None),
            "red_drag": dict(x=50, y=10, state=0, move=80),
        }
        for label, spec in cases.items():
            with self.subTest(path=label):
                w = self._widget_stub()
                w.current_frame_idx = 0
                w.selected_pause_id = None
                w.on_seek_cb = lambda tf: setattr(w, "_seeked_to", tf)
                w.on_pause_select_cb = lambda sid: None
                w.on_edit_cb = lambda cmd: None
                w.on_handle_end_cb = lambda: setattr(w, "_ended", True)
                w.canvas = SimpleNamespace(winfo_width=lambda: 100,
                                           focus_set=lambda: None)
                ev = SimpleNamespace(x=spec["x"], y=spec["y"], state=spec["state"])
                w._on_mousedown(ev)
                if spec["move"] is not None:
                    w._on_mousemove(SimpleNamespace(x=spec["move"],
                                                    y=spec["y"], state=spec["state"]))
                w._on_mouseup(SimpleNamespace(x=(spec["move"] or spec["x"]),
                                              y=spec["y"], state=spec["state"]))
                self.assertTrue(
                    getattr(w, "_ended", False),
                    f"{label}: 松开后必须调 on_handle_end_cb（否则拖动标志卡死）")
                self.assertIsNone(w.active_handle, f"{label}: active_handle 应清空")
                self.assertEqual(w._pending_candidates, [],
                                 f"{label}: 候选应清空")

    def test_ctrl_click_selects_segment(self) -> None:
        w = self._click_widget(ctrl=True)
        self.assertTrue(hasattr(w, "_picked"))
        self.assertEqual(w._picked, 1)
        self.assertFalse(hasattr(w, "_seeked_to"))

    def test_plain_click_on_selected_segment_clears_and_seeks(self) -> None:
        # 点已选中段：清选 + 跳转（不再吞掉点击）
        w = self._widget_stub()
        w.current_frame_idx = 0  # 避开指针命中
        w.selected_pause_id = 1
        w.on_seek_cb = lambda tf: setattr(w, "_seeked_to", tf)
        w.on_pause_select_cb = lambda sid: setattr(w, "_picked", sid)
        w.canvas = SimpleNamespace(winfo_width=lambda: 100, focus_set=lambda: None)
        w._on_mousedown(SimpleNamespace(x=50, y=TIMELINE_BAND_Y, state=0))
        self.assertIsNone(w.selected_pause_id)
        self.assertTrue(hasattr(w, "_seeked_to"))

    def test_clip_drag_preview_does_not_mutate_shared_segment(self) -> None:
        widget = self._widget_stub()
        original = dict(widget.clip_segments[0])

        widget._begin_clip_preview(2)
        widget._move_clip_handle(2, "in", 3)

        self.assertEqual(widget.clip_segments[0], original)
        self.assertEqual(widget._clip_preview_bounds[2], (3, 3))

    def test_clip_release_emits_half_open_command(self) -> None:
        widget = self._widget_stub()
        emitted = []
        widget.on_edit_cb = emitted.append
        widget.on_handle_end_cb = lambda: None
        widget._clip_preview_bounds[2] = (2, 4)
        widget.active_handle = ("clip_out", 2)
        widget._edit_changed = True

        widget._on_mouseup(SimpleNamespace())

        self.assertEqual(emitted, [SetClipBounds(2, 2, 4)])

    def test_clip_release_at_original_bounds_emits_no_command(self) -> None:
        widget = self._widget_stub()
        emitted = []
        widget.on_edit_cb = emitted.append
        widget.on_handle_end_cb = lambda: None
        widget._clip_preview_bounds[2] = (1, 3)
        widget.active_handle = ("clip_out", 2)
        widget._edit_changed = True

        widget._on_mouseup(SimpleNamespace())

        self.assertEqual(emitted, [])

    def test_clip_out_uses_exclusive_boundary_directly(self) -> None:
        widget = self._widget_stub()

        widget._begin_clip_preview(2)
        widget._move_clip_handle(2, "out", 1)

        self.assertEqual(widget._clip_preview_bounds[2], (1, 1))

    def test_clip_out_coordinate_round_trip_emits_no_command(self) -> None:
        widget = self._widget_stub()
        emitted = []
        widget.on_edit_cb = emitted.append
        widget.on_handle_end_cb = lambda: None

        keep_end = widget.clip_segments[0]["keep_out"] + 1
        handle_x = widget._f2x(keep_end, widget.canvas.winfo_width())
        boundary = widget._x2f(handle_x, widget.canvas.winfo_width())
        widget._begin_clip_preview(2)
        widget._move_clip_handle(2, "out", boundary)
        widget.active_handle = ("clip_out", 2)
        widget._edit_changed = True

        widget._on_mouseup(SimpleNamespace())

        self.assertEqual(boundary, keep_end)
        self.assertEqual(emitted, [])

    def test_clip_out_round_trip_preserves_video_end_boundary(self) -> None:
        widget = self._widget_stub()
        widget.clip_segments[0]["keep_out"] = 3

        keep_end = widget.total_frames
        handle_x = widget._f2x(keep_end, widget.canvas.winfo_width())
        boundary = widget._x2f(handle_x, widget.canvas.winfo_width())
        widget._begin_clip_preview(2)
        widget._move_clip_handle(2, "out", boundary)

        self.assertEqual(boundary, widget.total_frames)
        self.assertEqual(widget._clip_preview_bounds[2], (1, 4))

    def test_pause_right_click_emits_run_without_mutating_shared_mask(self) -> None:
        widget = self._widget_stub()
        emitted = []
        widget.on_edit_cb = emitted.append
        widget.selected_pause_id = None

        widget._on_right_click(SimpleNamespace(x=25, y=20))

        np.testing.assert_array_equal(widget.pause_segments[0]["local_del_mask"], [0, 0, 1, 1])
        self.assertEqual(emitted, [SetPauseMaskRun(1, 0, 2, 2)])
        np.testing.assert_array_equal(widget._pause_mask_preview[1], [2, 2, 1, 1])


if __name__ == "__main__":
    unittest.main()
