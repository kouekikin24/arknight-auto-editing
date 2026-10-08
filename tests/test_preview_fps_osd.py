from __future__ import annotations

import queue
import time
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
    player._paused_seek_guard_until = 0.0
    player._is_dragging = False
    player._verify_pending_frame = None
    player._verify_pending_until = 0.0
    player._io = mock.Mock()
    player._io.native_rendering = True
    player._io.fps = 60.0  # time-pos 口径需要真实源帧率
    player._io.dthrough_active = False  # 防 Mock 自动属性真值劫持（1c 直通拦截）
    # 默认引擎拒收验证定位：既有测试组保持旧 seek+贴帧路径语义，
    # VerifiedSeekTests 显式覆写为 True（防 Mock 自动属性真值劫持）
    player._io.seek_source_verified = mock.Mock(return_value=False)
    player.show_frame_osd_var = _BoolVar(True)
    player.show_fps_osd_var = _BoolVar(True)
    player.preview_speed_var = SimpleNamespace(get=lambda: "1x")
    return player


class InstantFpsOsdTests(unittest.TestCase):
    def test_tier_tag_shows_skim_speed(self):
        # G4：角标带档位+实时速率——「掠过10.0x」= 0.2x 区域业务加速
        # 此刻真实生效的直接可视证据；FPS 仍按显示节奏钳到 60。
        player = _bare_player()
        player.is_playing = True
        corner_texts: list[str] = []
        player._io.show_osd_corner_text.side_effect = corner_texts.append
        base = 1000.0
        ticks = 15
        step = 0.9 / (ticks - 1)
        perf = {"mode": "skim", "speed": 10.0}
        with mock.patch("preview_player.time.monotonic") as clock:
            for i in range(ticks):
                clock.return_value = base + i * step
                player._osd_tick = i + 1
                # 媒体时间以 10x 推进（虚拟秒/墙钟秒=10）
                player._note_fps_sample(10.0 * i * step, 0, perf=perf)
        self.assertEqual(len(corner_texts), 1)
        self.assertTrue(corner_texts[0].startswith("掠过10.0x | "))
        self.assertIn("60.0 FPS", corner_texts[0])

    def test_tier_tag_edl_source_and_no_perf(self):
        player = _bare_player()
        player.is_playing = True
        texts: list[str] = []
        player._io.show_osd_corner_text.side_effect = texts.append
        player._osd_tick = 15
        player._note_fps_sample(1.0, 0, perf={"mode": "edl", "speed": 3.0})
        self.assertTrue(texts[-1].startswith("保底3.0x | "))
        player._osd_tick = 30
        player._note_fps_sample(2.0, 0, perf={"mode": "source", "speed": 1.0})
        self.assertTrue(texts[-1].startswith("源1.0x | "))
        # 无 perf（cv 引擎/旧路径）→ 无标签，行为与改动前一致
        player._osd_tick = 45
        player._note_fps_sample(3.0, 0)
        self.assertNotIn("|", texts[-1])
        # speed 缺失 → 只有档位名
        player._osd_tick = 60
        player._note_fps_sample(4.0, 0, perf={"mode": "skim"})
        self.assertTrue(texts[-1].startswith("掠过 | "))

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

    def test_speed_2x_raises_clamp_ceiling(self):
        # mpv 的 2x 是 playback_rate 原生变速：内容帧率上限 = 源帧率×2，
        # 钳到源帧率会把真实的 ~120 误显示成 60。
        player = _bare_player()
        player.preview_speed_var = SimpleNamespace(get=lambda: "2x")
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
                # time-pos 以 2 倍墙钟推进（playback_rate=2），无丢帧
                player._note_fps_sample(2.0 * i * step, 0)
        self.assertEqual(len(corner_texts), 1)
        value = float(corner_texts[0].split()[0])
        self.assertAlmostEqual(value, 120.0, places=1)

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


class PausedSeekGuardTests(unittest.TestCase):
    """暂停中步进寻址后，渲染循环不得用引擎回读的旧帧号回写 UI。"""

    def _player(self) -> VideoPreviewPlayer:
        player = _bare_player()
        player.total_frames = 100
        player.current_frame_idx = 10
        player.timeline = mock.Mock()
        return player

    def test_paused_step_not_reverted_by_stale_engine_frame(self):
        player = self._player()
        # 用户在帧 10 暂停，按 → 步进到 11（_seek 会武装保护窗口）
        player.current_frame_idx = 11
        player._paused_seek_guard_until = time.monotonic() + 0.25
        # 引擎还回读旧值 10（mpv 寻址异步，旧 time-pos 事件先到）
        player._apply_native_perf(
            {"source_frame": 10, "time_pos": None, "mpv_frame_drops": None}
        )
        self.assertEqual(player.current_frame_idx, 11)
        # guard 生效时不得回写 timeline（timeline 是 Mock，被赋值即留下记录）
        self.assertNotEqual(player.timeline.current_frame_idx, 10)

    def test_playing_always_adopts_engine_frame(self):
        player = self._player()
        player.is_playing = True
        # 播放中即使保护窗口未过期，也以引擎为权威（播放会自己前进）
        player._paused_seek_guard_until = time.monotonic() + 60.0
        player._apply_native_perf(
            {"source_frame": 42, "time_pos": None, "mpv_frame_drops": None}
        )
        self.assertEqual(player.current_frame_idx, 42)

    def test_seek_frame_arms_guard_when_paused(self):
        player = self._player()
        player.skip_trimmed = _BoolVar(False)
        player._auto_rate_clear = mock.Mock()
        player._task_scope = mock.Mock(return_value=(0, 0))
        player._canvas_wh = mock.Mock(return_value=(320, 180))
        player._preview_pts_ready = mock.Mock(return_value=False)
        player._seek(11)
        self.assertGreater(player._paused_seek_guard_until, time.monotonic())


class PausedStepSemanticsTests(unittest.TestCase):
    """暂停中 ←/→ 原始 ±1 步进，UI 帧号即权威，不被引擎旧回读弹回。"""

    def _player(self, current: int, total: int = 100) -> VideoPreviewPlayer:
        player = _bare_player()
        player.total_frames = total
        player.current_frame_idx = current
        player.timeline = mock.Mock()
        player.skip_trimmed = _BoolVar(True)
        player._all_skip_segs_snap = mock.Mock(return_value=[(10, 20)])
        player._seek = mock.Mock()
        player._update_labels = mock.Mock()
        # 本组测旧（非原生）路径：cv 引擎无 step_frame，走 seek+贴帧
        player._io.native_rendering = False
        return player

    def test_right_step_moves_by_one_and_seeks(self):
        player = self._player(current=5)
        player._step_frame(+1, seek=True)
        self.assertEqual(player.current_frame_idx, 6)
        player._seek.assert_called_once_with(6, skip_trim=False)

    def test_left_step_moves_by_one_and_seeks(self):
        player = self._player(current=5)
        player._step_frame(-1, seek=True)
        self.assertEqual(player.current_frame_idx, 4)
        player._seek.assert_called_once_with(4, skip_trim=False)

    def test_engine_stale_readback_does_not_revert_paused_step(self):
        # 按下 → 到 6，保护窗过期后引擎还回读旧值 5（VFR/EDL 的 ±1 偏差）。
        # 暂停中 UI 帧号是权威，不得从 6 弹回 5。
        player = self._player(current=5)
        player.is_playing = False
        player._step_frame(+1, seek=True)
        player._paused_seek_guard_until = 0.0  # 保护窗已过期
        player._apply_native_perf(
            {"source_frame": 5, "time_pos": None, "mpv_frame_drops": None}
        )
        self.assertEqual(player.current_frame_idx, 6)

    def test_playing_adopts_engine_frame(self):
        # 播放中引擎是权威：回读多少 UI 就是多少
        player = self._player(current=5)
        player.is_playing = True
        player._paused_seek_guard_until = 0.0
        player._apply_native_perf(
            {"source_frame": 42, "time_pos": None, "mpv_frame_drops": None}
        )
        self.assertEqual(player.current_frame_idx, 42)


class NativeFrameStepTests(unittest.TestCase):
    """暂停 ←/→ 走 mpv 原生帧步进（探针D T0：毫秒级帧精确）：
    直发 step_frame，撤贴图层，且静帧收敛标记阻止追帧重贴慢速 C4。"""

    def _player(self, current: int, total: int = 100) -> VideoPreviewPlayer:
        player = _bare_player()  # native_rendering=True
        player.total_frames = total
        player.current_frame_idx = current
        player.timeline = mock.Mock()
        player.skip_trimmed = _BoolVar(False)
        player._seek = mock.Mock()
        player._update_labels = mock.Mock()
        player._auto_rate_clear = mock.Mock()
        player._drop_still_overlay = mock.Mock()
        player._still_frame_idx = None
        player._io.step_frame = mock.Mock(return_value=True)
        return player

    def test_right_step_uses_native_command_and_drops_overlay(self):
        player = self._player(current=5)
        player._step_frame(+1, seek=True)
        self.assertEqual(player.current_frame_idx, 6)
        player._io.step_frame.assert_called_once_with(+1)
        player._seek.assert_not_called()
        player._drop_still_overlay.assert_called_once()
        # 收敛标记对齐当前帧号：渲染循环追帧不会重新贴 C4 盖住原生帧
        self.assertEqual(player._still_frame_idx, 6)
        self.assertTrue(player._osd_pending)

    def test_left_step_uses_native_command(self):
        player = self._player(current=5)
        player._step_frame(-1, seek=True)
        self.assertEqual(player.current_frame_idx, 4)
        player._io.step_frame.assert_called_once_with(-1)
        player._seek.assert_not_called()

    def test_native_step_failure_falls_back_to_seek(self):
        player = self._player(current=5)
        player._io.step_frame.return_value = False
        player._step_frame(+1, seek=True)
        self.assertEqual(player.current_frame_idx, 6)
        player._seek.assert_called_once_with(6, skip_trim=False)
        player._drop_still_overlay.assert_not_called()

    def test_native_step_exception_falls_back_to_seek(self):
        player = self._player(current=5)
        player._io.step_frame.side_effect = RuntimeError("boom")
        player._step_frame(+1, seek=True)
        player._seek.assert_called_once_with(6, skip_trim=False)

    def test_playing_uses_legacy_seek_not_native_step(self):
        player = self._player(current=5)
        player.is_playing = True
        player._step_frame(+1, seek=True)
        player._io.step_frame.assert_not_called()
        player._seek.assert_called_once_with(6, skip_trim=False)

    def test_non_native_engine_uses_legacy_seek(self):
        player = self._player(current=5)
        player._io.native_rendering = False
        player._step_frame(+1, seek=True)
        player._io.step_frame.assert_not_called()
        player._seek.assert_called_once_with(6, skip_trim=False)

    def test_boundary_step_does_not_touch_engine(self):
        player = self._player(current=0)
        player._step_frame(-1, seek=True)
        self.assertEqual(player.current_frame_idx, 0)
        player._io.step_frame.assert_not_called()
        player._seek.assert_not_called()

    def test_native_hold_repeat_skips_preview_seek(self):
        # 原生步进每拍直达引擎：长按不再周期预览寻址（寻址会触发慢速贴帧）
        player = self._player(current=5)
        player._schedule_preview = mock.Mock()
        player._repeat_frame = mock.Mock()
        player._start_repeat("Right")
        self.assertTrue(player._key_hold_fired)
        player._schedule_preview.assert_not_called()
        player._repeat_frame.assert_called_once_with("Right")

    def test_legacy_hold_repeat_schedules_preview_seek(self):
        player = self._player(current=5)
        player._io.native_rendering = False
        player._schedule_preview = mock.Mock()
        player._repeat_frame = mock.Mock()
        player._start_repeat("Right")
        player._schedule_preview.assert_called_once()

    def test_native_release_skips_final_preview_seek(self):
        # 松手时引擎已停在最后一拍目标帧，末拍寻址只会触发慢速贴帧
        player = self._player(current=5)
        player._key_held = "Right"
        player._key_after_id = None
        player._key_preview_id = None
        player._key_hold_fired = True
        player._frame_q = queue.Queue()
        player._do_preview_seek = mock.Mock()
        player._on_key_release(SimpleNamespace(keysym="Right"))
        player._do_preview_seek.assert_not_called()
        self.assertFalse(player._key_hold_fired)

    def test_legacy_release_runs_final_preview_seek(self):
        player = self._player(current=5)
        player._io.native_rendering = False
        player._key_held = "Right"
        player._key_after_id = None
        player._key_preview_id = None
        player._key_hold_fired = True
        player._frame_q = queue.Queue()
        player._do_preview_seek = mock.Mock()
        player._on_key_release(SimpleNamespace(keysym="Right"))
        player._do_preview_seek.assert_called_once()


class VerifiedSeekTests(unittest.TestCase):
    """选项2：暂停点轴 → 闭环验证定位。成功=主窗原生精确帧（撤贴层），
    失败/拒收=回退 C4 静帧管线；飞行中追帧逻辑不得抢跑贴帧。"""

    def _player(self) -> VideoPreviewPlayer:
        player = _bare_player()
        player.total_frames = 100
        player.current_frame_idx = 10
        player.timeline = mock.Mock()
        player.skip_trimmed = _BoolVar(False)
        player._all_skip_segs_snap = mock.Mock(return_value=[])
        player._task_scope = mock.Mock(return_value=(0, 0))
        player._canvas_wh = mock.Mock(return_value=(320, 180))
        player._preview_pts_ready = mock.Mock(return_value=True)
        player._show_paused_still = mock.Mock()
        player._auto_rate_clear = mock.Mock()
        player._is_dragging = False
        player.lbl_info = mock.Mock()
        player._io.seek_source_verified = mock.Mock(return_value=True)
        return player

    def test_paused_click_uses_verified_seek(self):
        player = self._player()
        player._seek_frame(50)
        player._io.seek_source_verified.assert_called_once()
        req = player._io.seek_source_verified.call_args[0][0]
        self.assertEqual(req.source_frame, 50)
        self.assertTrue(req.exact)
        player._io.seek_source.assert_not_called()
        player._show_paused_still.assert_not_called()
        self.assertEqual(player._verify_pending_frame, 50)
        self.assertGreater(player._verify_pending_until, time.monotonic())

    def test_verified_refusal_falls_back_to_legacy(self):
        # 引擎拒收（重复PTS段/EDL）→ 旧路径 seek+C4，一个不少
        player = self._player()
        player._io.seek_source_verified.return_value = False
        player._seek_frame(50)
        player._io.seek_source.assert_called_once()
        player._show_paused_still.assert_called_once_with(50)
        self.assertIsNone(player._verify_pending_frame)

    def test_verified_certification_error_falls_back(self):
        player = self._player()
        player._io.seek_source_verified.side_effect = PreviewEngineError(
            "CERTIFICATION_REQUIRED", "not bound")
        player._seek_frame(50)
        player._io.seek_source.assert_called_once()
        player._show_paused_still.assert_called_once_with(50)

    def test_dragging_uses_legacy_path(self):
        player = self._player()
        player._is_dragging = True
        player._seek_frame(50)
        player._io.seek_source_verified.assert_not_called()
        player._io.seek_source.assert_called_once()

    def test_playing_uses_legacy_path(self):
        player = self._player()
        player.is_playing = True
        player._seek_frame(50)
        player._io.seek_source_verified.assert_not_called()
        player._io.seek_source.assert_called_once()

    def test_ok_event_shows_native_frame(self):
        player = self._player()
        player._verify_pending_frame = 50
        player._drop_still_overlay = mock.Mock()
        player._on_seek_verified(
            {"event": "seek-verified", "ok": True, "frame": 50,
             "reason": "exact"})
        player._drop_still_overlay.assert_called_once()
        self.assertEqual(player._still_frame_idx, 50)
        self.assertTrue(player._osd_pending)
        self.assertIsNone(player._verify_pending_frame)
        player._show_paused_still.assert_not_called()

    def test_fail_event_falls_back_to_still(self):
        player = self._player()
        player._verify_pending_frame = 50
        player._drop_still_overlay = mock.Mock()
        player._on_seek_verified(
            {"event": "seek-verified", "ok": False, "frame": 50,
             "reason": "timeout"})
        player._show_paused_still.assert_called_once_with(50)
        player._drop_still_overlay.assert_not_called()
        self.assertIsNone(player._verify_pending_frame)

    def test_stale_event_ignored(self):
        player = self._player()
        player._verify_pending_frame = 60
        player._on_seek_verified(
            {"event": "seek-verified", "ok": True, "frame": 50})
        player._show_paused_still.assert_not_called()
        self.assertEqual(player._verify_pending_frame, 60)

    def test_native_step_supersedes_pending_verify(self):
        player = self._player()
        player._verify_pending_frame = 50
        player._drop_still_overlay = mock.Mock()
        player._update_labels = mock.Mock()
        player._io.step_frame = mock.Mock(return_value=True)
        player._still_frame_idx = None
        player._step_frame(+1, seek=True)
        self.assertIsNone(player._verify_pending_frame)
        player._io.step_frame.assert_called_once_with(+1)


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

