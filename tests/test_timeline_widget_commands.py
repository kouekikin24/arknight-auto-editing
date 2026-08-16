from __future__ import annotations

from types import SimpleNamespace
import unittest

import numpy as np

from edit_commands import SetClipBounds, SetPauseMaskRun
from timeline_widget import TimelineWidget


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
