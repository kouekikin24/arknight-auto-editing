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
    player._io.fps = 60.0  # time-pos 口径需要真实源帧率
    player.show_frame_osd_var = _BoolVar(True)
    return player


class InstantFpsOsdTests(unittest.TestCase):
    def test_presented_fps_uses_time_pos_and_clamps(self):
        player = _bare_player()
        player.is_playing = True
        corner_texts: list[str] = []
        player._io.show_osd_corner_text.side_effect = corner_texts.append

        # 15 ticks spanning 0.9s; time-pos advances 0.9 media-seconds (rate 1.0
        # => 60fps), minus ~1 drop/s. Result must clamp to <= src 60.
        base = 1000.0
        ticks = 15
        step = 0.9 / (ticks - 1)
        with mock.patch("preview_player.time.monotonic") as clock:
            for i in range(ticks):
                clock.return_value = base + i * step
                player._osd_tick = i + 1  # last tick hits % 15 == 0
                player._note_fps_sample(i * step, 10 + i)  # time_pos, drops
        self.assertEqual(len(corner_texts), 1)
        value = float(corner_texts[0].split()[0])
        # media_rate = 0.9/0.9 = 1.0 -> 60fps; minus 14 drops/0.9s ≈ 15.6 -> ~44.4
        self.assertAlmostEqual(value, 60.0 - 14 / 0.9, places=1)

    def test_edl_jump_does_not_inflate(self):
        # time-pos 倒退（seek/EDL 重锚）时，有序性检查必须挡住这次输出，
        # 防止把跳变当播放导致读数虚高。
        player = _bare_player()
        player.is_playing = True
        corner_texts: list[str] = []
        player._io.show_osd_corner_text.side_effect = corner_texts.append
        base = 1000.0
        ticks = 15
        step = 0.9 / (ticks - 1)
        with mock.patch("preview_player.time.monotonic") as clock:
            for i in range(ticks):
                clock.return_value = base + i * step
                player._osd_tick = i + 1
                # 中途 time-pos 倒退一次：窗口内出现逆序 -> 该窗口不输出
                pos = i * step if i < 8 else (i - 8) * step
                player._note_fps_sample(pos, 0)
        # 因为窗口里混入了倒退，ordered=False，不产出数值（只允许占位符）
        self.assertTrue(all(t == "… FPS" for t in corner_texts))

    def test_pause_and_seek_reset_the_window(self):
        player = _bare_player()
        player.is_playing = True
        with mock.patch("preview_player.time.monotonic", return_value=1.0):
            player._note_fps_sample(1.0, 0)
        self.assertEqual(len(player._fps_samples), 1)
        player.is_playing = False
        with mock.patch("preview_player.time.monotonic", return_value=2.0):
            player._note_fps_sample(2.0, 0)
        self.assertEqual(len(player._fps_samples), 0)

        # A seek also clears the window so the jump is not counted as fps.
        player._fps_samples.append((2.0, 1.0, 0))
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
