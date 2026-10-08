#!/usr/bin/env python3
"""P1-RAM Tier2 的 GUI 调度语义测试：debounce 预热 + 代数守卫 + 安装回调。

不起 Tk：VideoPreviewPlayer 的调度方法只依赖实例属性，绑到假对象上
直接驱动（与 legacy 桩测试同一惯例）。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from preview_player import VideoPreviewPlayer


class _FakeVar:
    def __init__(self, value):
        self._value = value

    def get(self):
        return self._value


class _FakeEngine:
    def __init__(self, skim_ready=True, skim_key=None):
        self.skim_ready = skim_ready
        self.skim_key = skim_key
        self.installed = []

    def set_skim_blob(self, data, key):
        self.installed.append((len(data), tuple(key)))
        self.skim_key = tuple(key)
        return True


class _FakeMedia:
    source_sha256 = "sha-x"
    frame_pts_authoritative = True


class _FakePlan:
    fingerprint = "fp-x"
    deleted_ranges = ((2, 4),)


class _FakeTaskManager:
    def __init__(self):
        self.submitted = []

    def submit(self, kind, work, *, on_success=None, on_error=None,
               on_cancelled=None, on_done=None, project_generation=None,
               timeline_revision=None, replace=True):
        handle = object()
        self.submitted.append({
            "kind": kind, "on_success": on_success, "on_error": on_error,
            "on_cancelled": on_cancelled, "on_done": on_done,
            "project_generation": project_generation,
            "timeline_revision": timeline_revision, "replace": replace,
            "handle": handle,
        })
        return handle


class _DummyPlayer:
    """调度方法依赖的最小属性面（闭包里的 self 即本对象）。"""

    _closing = False

    def __init__(self, engine=None):
        self._io = engine if engine is not None else _FakeEngine()
        self.media_info = _FakeMedia()
        self.skip_trimmed = _FakeVar(True)
        self.video_path = "src.mp4"
        self.task_manager = _FakeTaskManager()
        self._skim_build_handle = None
        self._skim_build_head = None

    def _build_timeline_plan(self):  # pragma: no cover - 被下方赋值覆盖
        raise AssertionError("replaced per instance")

    _build_timeline_plan = lambda self: _FakePlan()
    _task_scope = lambda self: (7, 3)


class _FakeBlob:
    data = bytearray(b"\x47" * 100)
    key = ("sha-x", "fp-x", 7, 3, 480, 30, 6)
    mb = 0.1
    kept_frames = 10
    build_s = 1.0


class SkimScheduleTests(unittest.TestCase):
    def _schedule(self, dummy):
        VideoPreviewPlayer._maybe_schedule_skim_build(dummy)

    def test_happy_path_submits_scoped_replace_task(self):
        dummy = _DummyPlayer()
        self._schedule(dummy)
        self.assertEqual(len(dummy.task_manager.submitted), 1)
        sub = dummy.task_manager.submitted[0]
        self.assertEqual(sub["kind"], "player.skim_build")
        self.assertEqual(sub["project_generation"], 7)
        self.assertEqual(sub["timeline_revision"], 3)
        self.assertTrue(sub["replace"])
        # on_success 安装 blob 到引擎并回填 skim_key
        sub["on_success"](_FakeBlob())
        self.assertEqual(dummy._io.installed, [(100, _FakeBlob.key)])
        self.assertIsNone(dummy._skim_build_head)

    def test_inflight_head_guard_blocks_duplicate(self):
        dummy = _DummyPlayer()
        self._schedule(dummy)
        self._schedule(dummy)
        self.assertEqual(len(dummy.task_manager.submitted), 1)

    def test_matching_engine_key_skips_build(self):
        engine = _FakeEngine(skim_key=("sha-x", "fp-x", 7, 3, 480, 30, 6))
        dummy = _DummyPlayer(engine)
        self._schedule(dummy)
        self.assertEqual(dummy.task_manager.submitted, [])

    def test_guards_noop(self):
        # 未认证 / 跳裁剪关 / 无删除段 / 无引擎 → 全部静默
        d = _DummyPlayer()
        d.media_info = _FakeMedia()
        d.media_info.frame_pts_authoritative = False
        self._schedule(d)
        d2 = _DummyPlayer()
        d2.skip_trimmed = _FakeVar(False)
        self._schedule(d2)
        d3 = _DummyPlayer()
        d3._build_timeline_plan = lambda: type("P", (), {
            "fingerprint": "f", "deleted_ranges": ()})()
        self._schedule(d3)
        d4 = _DummyPlayer()
        d4._io = None
        self._schedule(d4)
        d5 = _DummyPlayer(_FakeEngine(skim_ready=False))
        self._schedule(d5)
        for subject in (d, d2, d3, d4, d5):
            self.assertEqual(subject.task_manager.submitted, [])

    def test_error_and_cancel_clear_head_silently(self):
        import skim_cache
        from task_manager import TaskCancelled

        dummy = _DummyPlayer()
        self._schedule(dummy)
        sub = dummy.task_manager.submitted[0]
        sub["on_error"](skim_cache.SkimBuildCancelled())
        self.assertIsNone(dummy._skim_build_head)
        # 头清空后允许再次调度
        self._schedule(dummy)
        self.assertEqual(len(dummy.task_manager.submitted), 2)
        sub2 = dummy.task_manager.submitted[1]
        sub2["on_error"](TaskCancelled("scope"))
        self.assertIsNone(dummy._skim_build_head)

    def test_settings_switch_off_blocks_build(self):
        class _Settings:
            def get_params(self):
                return {"skim_preview": False}

        dummy = _DummyPlayer()
        dummy.settings = _Settings()
        self._schedule(dummy)
        self.assertEqual(dummy.task_manager.submitted, [])

        class _SettingsOn:
            def get_params(self):
                return {"skim_preview": True}

        dummy.settings = _SettingsOn()
        self._schedule(dummy)
        self.assertEqual(len(dummy.task_manager.submitted), 1)


class SkimDebounceTests(unittest.TestCase):
    class _Tkish:
        _closing = False
        _SKIM_REBUILD_DEBOUNCE_MS = VideoPreviewPlayer._SKIM_REBUILD_DEBOUNCE_MS
        # 绑真方法：arm 里 self._on_skim_rebuild_due 必须解析到被测实现
        _on_skim_rebuild_due = VideoPreviewPlayer._on_skim_rebuild_due

        def __init__(self):
            self.afters = []
            self.cancelled = []
            self._skim_rebuild_after = None

        def after(self, ms, fn):
            token = ("after", ms, fn)
            self.afters.append(token)
            return token

        def after_cancel(self, token):
            self.cancelled.append(token)

    def _arm(self, subject):
        VideoPreviewPlayer._arm_skim_rebuild(subject)

    def test_arm_sets_debounced_after(self):
        subject = self._Tkish()
        self._arm(subject)
        self.assertIsNotNone(subject._skim_rebuild_after)
        ms = subject._skim_rebuild_after[1]
        self.assertEqual(ms, VideoPreviewPlayer._SKIM_REBUILD_DEBOUNCE_MS)

    def test_rearm_cancels_previous(self):
        subject = self._Tkish()
        self._arm(subject)
        first = subject._skim_rebuild_after
        self._arm(subject)
        self.assertEqual(subject.cancelled, [first])
        self.assertIsNot(subject._skim_rebuild_after, first)

    def test_closing_or_no_after_noop(self):
        subject = self._Tkish()
        subject._closing = True
        self._arm(subject)
        self.assertIsNone(subject._skim_rebuild_after)
        bare = object()
        # legacy 桩：无 after 也不炸（getattr 防御）
        VideoPreviewPlayer._arm_skim_rebuild(bare)

    def test_due_clears_token_and_schedules(self):
        scheduled = []

        class _Due(self._Tkish):
            def _maybe_schedule_skim_build(self):
                scheduled.append(True)

        subject = _Due()
        self._arm(subject)
        fn = subject._skim_rebuild_after[2]
        self.assertEqual(fn.__func__, VideoPreviewPlayer._on_skim_rebuild_due)
        fn()
        self.assertIsNone(subject._skim_rebuild_after)
        self.assertEqual(scheduled, [True])


class SendPlaySkimGateTests(unittest.TestCase):
    """开关语义（review G 轮补漏）：关掉「快速掠过代理」时，_send_play
    必须清掉引擎里已装的 blob——只停新构建不够，旧代理会继续被代数
    命中（手检 e 项：关开关后应永远保底 3x）。"""

    def _gate_player(self, skim_on: bool):
        from tests.test_preview_fps_osd import _bare_player

        player = _bare_player()
        player.settings = SimpleNamespace(
            get_params=lambda: {"skim_preview": skim_on})
        player._apply_pace_mode_to_io = lambda: None
        player.total_frames = 0  # 门槛触发后经「无可播放帧」早退，不进播放链
        player.btn_play = mock.Mock()
        player.lbl_info = mock.Mock()
        return player

    def test_switch_off_clears_installed_blob(self):
        player = self._gate_player(False)
        self.assertFalse(player._send_play(0))
        player._io.clear_skim_blob.assert_called_once_with()

    def test_switch_on_keeps_blob(self):
        player = self._gate_player(True)
        self.assertFalse(player._send_play(0))
        clear = getattr(player._io, "clear_skim_blob", None)
        if clear is not None:
            clear.assert_not_called()


if __name__ == "__main__":
    unittest.main()
