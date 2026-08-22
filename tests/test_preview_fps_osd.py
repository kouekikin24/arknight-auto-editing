from __future__ import annotations

from collections import deque
from types import SimpleNamespace
import unittest
from unittest import mock

from preview_player import VideoPreviewPlayer
from preview_engine import PreviewEngineError


class _BoolVar:
    def __init__(self, value: bool):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = bool(value)


def _bare_player() -> VideoPreviewPlayer:
    player = VideoPreviewPlayer.__new__(VideoPreviewPlayer)
    player._closing = False
    player.is_playing = False
    player._fps_samples = deque(maxlen=64)
    player._osd_tick = 0
    player._osd_pending = False
    player._io = mock.Mock()
    player._io.native_rendering = True
    player.show_frame_osd_var = _BoolVar(True)
    return player


class InstantFpsOsdTests(unittest.TestCase):
    def test_presented_fps_subtracts_drop_rate(self):
        player = _bare_player()
        player.is_playing = True
        corner_texts: list[str] = []
        player._io.show_osd_corner_text.side_effect = corner_texts.append

        # 15 ticks (one throttle window) spanning 0.9s at 60fps with 1 drop/s.
        base = 1000.0
        ticks = 15
        step = 0.9 / (ticks - 1)
        with mock.patch("preview_player.time.monotonic") as clock:
            for i in range(ticks):
                clock.return_value = base + i * step
                player._osd_tick = i + 1  # last tick hits % 15 == 0
                player._note_fps_sample(100 + int(round(i * 60 * step)), 10 + i)
        # advanced = 54 frames / 0.9s = 60fps; drops = 14 / 0.9s ≈ 15.6/s
        self.assertEqual(len(corner_texts), 1)
        value = float(corner_texts[0].split()[0])
        self.assertAlmostEqual(value, 54 / 0.9 - 14 / 0.9, places=1)

    def test_pause_and_seek_reset_the_window(self):
        player = _bare_player()
        player.is_playing = True
        with mock.patch("preview_player.time.monotonic", return_value=1.0):
            player._note_fps_sample(100, 0)
        self.assertEqual(len(player._fps_samples), 1)
        player.is_playing = False
        with mock.patch("preview_player.time.monotonic", return_value=2.0):
            player._note_fps_sample(100, 0)
        self.assertEqual(len(player._fps_samples), 0)

        # A seek also clears the window so the jump is not counted as fps.
        player._fps_samples.append((2.0, 100, 0))
        player._auto_rate_clear = mock.Mock()
        player.total_frames = 10
        player.current_frame_idx = 0
        player.timeline = mock.Mock()
        player.skip_trimmed = _BoolVar(False)
        player._task_scope = mock.Mock(return_value=(0, 0))
        player._canvas_wh = mock.Mock(return_value=(320, 180))
        player._preview_pts_ready = mock.Mock(return_value=False)
        player._seek(5)
        self.assertEqual(len(player._fps_samples), 0)


class EdlAwareSeekTests(unittest.TestCase):
    def _player(self, *, skip_on: bool, skip_segs: list) -> VideoPreviewPlayer:
        player = _bare_player()
        player.total_frames = 10
        player.current_frame_idx = 0
        player.timeline = mock.Mock()
        player.skip_trimmed = _BoolVar(skip_on)
        player._all_skip_segs_snap = mock.Mock(return_value=skip_segs)
        player._auto_rate_clear = mock.Mock()
        player._task_scope = mock.Mock(return_value=(0, 0))
        player._canvas_wh = mock.Mock(return_value=(320, 180))
        player._preview_pts_ready = mock.Mock(return_value=True)
        player.lbl_info = mock.Mock()
        return player

    def test_skip_mode_steps_inside_edl_view(self):
        player = self._player(skip_on=True, skip_segs=[(2, 4)])
        player._seek(5)
        player._io.seek_edl.assert_called_once_with(5)
        player._io.seek_source.assert_not_called()

    def test_seek_edl_not_ready_falls_back_to_source_seek(self):
        player = self._player(skip_on=True, skip_segs=[(2, 4)])
        player._io.seek_edl.side_effect = PreviewEngineError(
            "EDL_NOT_READY", "no artifact yet"
        )
        player._seek(5)
        player._io.seek_source.assert_called_once()
        request = player._io.seek_source.call_args[0][0]
        self.assertEqual(request.source_frame, 5)

    def test_skip_checkbox_off_uses_source_seek(self):
        player = self._player(skip_on=False, skip_segs=[(2, 4)])
        player._seek(5)
        player._io.seek_edl.assert_not_called()
        player._io.seek_source.assert_called_once()

    def test_no_skip_segments_uses_source_seek(self):
        player = self._player(skip_on=True, skip_segs=[])
        player._seek(5)
        player._io.seek_edl.assert_not_called()
        player._io.seek_source.assert_called_once()


if __name__ == "__main__":
    unittest.main()
