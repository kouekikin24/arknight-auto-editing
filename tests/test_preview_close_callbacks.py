from __future__ import annotations

from queue import Queue
from types import SimpleNamespace
import unittest
from unittest import mock

from preview_player import VideoPreviewPlayer


class _BoolVar:
    def __init__(self, value: bool):
        self.value = value
        self.set_calls = []

    def get(self):
        return self.value

    def set(self, value):
        self.set_calls.append(value)
        self.value = bool(value)


class PreviewCloseCallbackTests(unittest.TestCase):
    @staticmethod
    def _closed_player():
        player = VideoPreviewPlayer.__new__(VideoPreviewPlayer)
        player._closing = True
        return player

    def test_timer_callbacks_stop_immediately_after_close(self):
        player = self._closed_player()
        player._render_after_id = "render"
        player._calib_after_id = "calibration"
        player._key_held = "Right"
        player.after = mock.Mock(side_effect=AssertionError("timer was rescheduled"))

        player._render_loop()
        player._calib_finish_run()
        player._preview_tick()

        self.assertIsNone(player._render_after_id)
        self.assertIsNone(player._calib_after_id)
        player.after.assert_not_called()

    def test_root_key_callbacks_are_noops_after_close(self):
        player = self._closed_player()
        player._key_held = "Right"
        player._key_after_id = "repeat"
        player._key_preview_id = "preview"
        player._key_hold_fired = True
        player._frame_q = Queue()
        player.focus_get = mock.Mock(
            side_effect=AssertionError("destroyed widget focus was queried")
        )
        player.after_cancel = mock.Mock()
        player._do_preview_seek = mock.Mock()
        player.toggle_play = mock.Mock()

        self.assertEqual(player._on_key_space(SimpleNamespace()), "break")
        player._on_key_release(SimpleNamespace(keysym="Right"))

        player.focus_get.assert_not_called()
        player.toggle_play.assert_not_called()
        player.after_cancel.assert_not_called()
        player._do_preview_seek.assert_not_called()

    def test_preview_toggle_and_direct_play_are_noops_after_close(self):
        player = self._closed_player()
        player.preview_opt_var = _BoolVar(False)
        player.focus_get = mock.Mock(
            side_effect=AssertionError("destroyed widget focus was queried")
        )
        player._on_preview_opt_change = mock.Mock()
        player.is_playing = False
        player.current_frame_idx = 0
        player._reset_ui_gap_stats = mock.Mock()
        player._auto_rate_mark_start = mock.Mock()
        player._send_play = mock.Mock()

        self.assertEqual(player._toggle_preview_opt(), "break")
        player.toggle_play()

        player.focus_get.assert_not_called()
        self.assertEqual(player.preview_opt_var.set_calls, [])
        player._on_preview_opt_change.assert_not_called()
        self.assertFalse(player.is_playing)
        player._reset_ui_gap_stats.assert_not_called()
        player._auto_rate_mark_start.assert_not_called()
        player._send_play.assert_not_called()


if __name__ == "__main__":
    unittest.main()
