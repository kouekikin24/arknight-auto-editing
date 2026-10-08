"""分析缓存接线契约测试（09-16 review 补：此前 pytest 完全不覆盖接线）。

覆盖三类**用户可感知**的接线语义（09-16 事故的教训：缓存写失败被静默吞掉
且日志谎报，正是因为没有测试盯住"该存的时候必须存、该跳的时候必须跳"）：
  1. 缓存命中 → `_finish_analysis(from_cache=True)` → **不得**再写盘；
  2. 真实分析完成 → `_finish_analysis(from_cache=False)` → **必须**写盘，
     且把 context_used/templates_missing 如实带下去（提示框口径）；
  3. 用户拒绝缓存 → 记住该键，此后不打扰、不自动载入；重算落盘后解除。
"""
from __future__ import annotations

import threading
import unittest
from unittest import mock

import numpy as np

from preview_player import VideoPreviewPlayer


class _Settings:
    def get_params(self):
        return {
            "proc_res": (400, 225),
            "thresholds": {"pause": 1},
            "compare": {"motion_thresh": 2.0},
            "batch": 4,
            "threads": 1,
            "decode_backend": "opencv",
            "ffmpeg_path": None,
        }


def _bare_player() -> VideoPreviewPlayer:
    player = VideoPreviewPlayer.__new__(VideoPreviewPlayer)
    player._closing = False
    player.video_path = "x.mp4"
    player.fps = 60.0
    player.total_frames = 3
    player.settings = _Settings()
    # 装配段（replace_timeline/publish/redraw/messagebox）全部短路：本测试只
    # 关心"存/不存"这一接线决策，不涉及 UI 本体。
    player._build_clip_segments = staticmethod(lambda pauses, total: [])
    player._publish_project_snapshot = lambda snapshot: None
    player.timeline = mock.MagicMock()
    player.project_state = mock.MagicMock()
    player.project_state.replace_timeline.return_value = mock.MagicMock(
        project_generation=1, timeline_revision=1)
    player.task_manager = mock.MagicMock()
    player.btn_analyze = mock.MagicMock()
    player.states_array = None
    player.diffs_array = None
    player._analysis_cache_declined_key = None
    player._analysis_cache_prompted = True
    return player


class CacheWiringTests(unittest.TestCase):
    def test_fresh_analysis_must_store_with_flags(self):
        p = _bare_player()
        p.settings.set_selected_pause = lambda *a, **k: None
        # 必须同时屏蔽 showwarning：本用例传 templates_missing=True，
        # _finish_analysis 会弹"模板缺失"警告。**曾经只 patch showinfo** →
        # 真实模态窗弹出并永久阻塞（实测 exit 124 超时；桌面上出现的无主
        # 弹窗即此）。详见 tests/conftest.py 事故说明。
        with mock.patch.object(type(p), "_store_analysis_cache") as store, \
             mock.patch("tkinter.messagebox.showinfo",
                        side_effect=lambda *a, **k: None), \
             mock.patch("tkinter.messagebox.showwarning") as warn:
            p._finish_analysis(np.zeros(3, np.int8), np.zeros(3, np.float32),
                               [], [], backend_label="OpenCV",
                               context_used=False, templates_missing=True,
                               from_cache=False)
        self.assertEqual(store.call_count, 1, "真实分析结果必须落盘")
        kwargs = store.call_args.kwargs
        self.assertIs(kwargs.get("context_used"), False)
        self.assertIs(kwargs.get("templates_missing"), True)
        self.assertEqual(warn.call_count, 1, "模板缺失应提示一次")

    def test_cache_hit_must_not_restore(self):
        p = _bare_player()
        p.settings.set_selected_pause = lambda *a, **k: None
        with mock.patch.object(type(p), "_store_analysis_cache") as store, \
             mock.patch("tkinter.messagebox.showinfo",
                        side_effect=lambda *a, **k: None):
            p._finish_analysis(np.zeros(3, np.int8), np.zeros(3, np.float32),
                               [], [], backend_label="缓存", from_cache=True)
        self.assertEqual(store.call_count, 0, "缓存命中不得重复写盘")

    def test_templates_missing_warns(self):
        p = _bare_player()
        p.settings.set_selected_pause = lambda *a, **k: None
        with mock.patch.object(type(p), "_store_analysis_cache"), \
             mock.patch("tkinter.messagebox.showwarning") as warn, \
             mock.patch("tkinter.messagebox.showinfo",
                        side_effect=lambda *a, **k: None):
            p._finish_analysis(np.zeros(3, np.int8), np.zeros(3, np.float32),
                               [], [], templates_missing=True)
        self.assertEqual(warn.call_count, 1, "模板缺失必须提示（含缓存命中路径）")

    def test_decline_is_remembered_and_not_reloaded(self):
        p = _bare_player()
        p._analysis_cache_prompted = False
        key_digest = "deadbeef" * 4
        with mock.patch("analysis_cache.make_key") as mk, \
             mock.patch("analysis_cache.AnalysisCache") as CacheCls, \
             mock.patch("tkinter.messagebox.askyesno", return_value=False):
            mk.return_value = mock.MagicMock(digest=lambda: key_digest)
            CacheCls.return_value.load.return_value = {
                "states": np.zeros(3, np.int8),
                "diffs": np.zeros(3, np.float32),
                "pauses": [], "speeds": [], "meta": {}, "file_bytes": 1,
            }
            p.media_info = mock.MagicMock()
            first = p._try_load_analysis_cache({"backend_label": "OpenCV"})
        self.assertFalse(first, "拒绝后应回落到完整分析")
        self.assertEqual(p._analysis_cache_declined_key, key_digest)

        # 第二次：不得再询问，且仍不载入
        with mock.patch("analysis_cache.make_key") as mk, \
             mock.patch("analysis_cache.AnalysisCache") as CacheCls, \
             mock.patch("tkinter.messagebox.askyesno") as ask:
            mk.return_value = mock.MagicMock(digest=lambda: key_digest)
            CacheCls.return_value.load.return_value = {
                "states": np.zeros(3, np.int8),
                "diffs": np.zeros(3, np.float32),
                "pauses": [], "speeds": [], "meta": {}, "file_bytes": 1,
            }
            p.media_info = mock.MagicMock()
            second = p._try_load_analysis_cache({"backend_label": "OpenCV"})
        self.assertFalse(second)
        self.assertEqual(ask.call_count, 0, "已拒绝的键不得再次打扰用户")

    def test_other_key_still_asked_after_decline(self):
        p = _bare_player()
        p._analysis_cache_prompted = False
        p._analysis_cache_declined_key = "other" * 8
        with mock.patch("analysis_cache.make_key") as mk, \
             mock.patch("analysis_cache.AnalysisCache") as CacheCls, \
             mock.patch("tkinter.messagebox.askyesno",
                        return_value=True) as ask, \
             mock.patch.object(type(p), "_finish_analysis") as fin:
            mk.return_value = mock.MagicMock(digest=lambda: "newkey" * 5)
            CacheCls.return_value.load.return_value = {
                "states": np.zeros(3, np.int8),
                "diffs": np.zeros(3, np.float32),
                "pauses": [], "speeds": [], "meta": {}, "file_bytes": 1,
            }
            p.media_info = mock.MagicMock()
            hit = p._try_load_analysis_cache({"backend_label": "OpenCV"})
        self.assertTrue(hit)
        self.assertEqual(ask.call_count, 1, "换了键仍应询问")
        self.assertIs(fin.call_args.kwargs.get("from_cache"), True)

    def test_fresh_store_clears_decline(self):
        p = _bare_player()
        p._analysis_cache_declined_key = "deadbeef" * 4
        p.settings.set_selected_pause = lambda *a, **k: None
        with mock.patch.object(type(p), "_store_analysis_cache"), \
             mock.patch("tkinter.messagebox.showinfo",
                        side_effect=lambda *a, **k: None):
            p._finish_analysis(np.zeros(3, np.int8), np.zeros(3, np.float32),
                               [], [], from_cache=False)
        self.assertIsNone(p._analysis_cache_declined_key,
                          "重算落盘后应解除拒绝记录")

    def test_thread_is_owner_thread(self):
        # _finish_analysis 含 messagebox，必须在 owner 线程调用（接线契约）
        p = _bare_player()
        p.settings.set_selected_pause = lambda *a, **k: None
        seen = {}
        with mock.patch.object(type(p), "_store_analysis_cache"), \
             mock.patch("tkinter.messagebox.showinfo",
                        side_effect=lambda *a, **k: None):
            p._finish_analysis(np.zeros(3, np.int8), np.zeros(3, np.float32),
                               [], [])
            seen["thread"] = threading.get_ident()
        self.assertEqual(seen["thread"], threading.get_ident())


class PendingAnalysisTests(unittest.TestCase):
    """09-23 时序修复：导入后立即点击分析不得静默冷跑。

    缓存键需要源 sha256，而它由后台媒体探测产出（4.mp4 约 5-7s）。原实现
    在 media_info 缺失时静默跳过缓存查询 → 导入后立即点击会白跑数分钟完整
    分析（owner 实测）。现改为挂起请求、探测落定后续跑。
    """

    @staticmethod
    def _player_with_probe_in_flight() -> VideoPreviewPlayer:
        p = _bare_player()
        p._media_info_handle = mock.MagicMock()   # 探测在飞
        p.media_info = None
        p.media_info_error = None
        p._pending_analysis_source = None
        p.task_manager = mock.MagicMock()
        return p

    def test_click_while_probe_in_flight_suspends_instead_of_cold_running(self):
        p = self._player_with_probe_in_flight()
        with mock.patch("analysis_cache.templates_fingerprint",
                        return_value="fp"):
            p._start_analysis()
        self.assertEqual(p._pending_analysis_source, "x.mp4",
                         "探测在飞时应挂起请求")
        self.assertEqual(p.task_manager.submit.call_count, 0,
                         "探测在飞时不得启动冷分析")
        text = p.btn_analyze.config.call_args.kwargs.get("text", "")
        self.assertIn("等待媒体探测", text)

    def test_probe_success_resumes_pending_request(self):
        p = _bare_player()
        p._pending_analysis_source = "x.mp4"
        p.media_info = mock.MagicMock()
        calls = []
        p._start_analysis = lambda: calls.append(True)
        p._maybe_resume_pending_analysis()
        self.assertEqual(calls, [True], "探测落定后应续跑")
        self.assertIsNone(p._pending_analysis_source)

    def test_resume_after_source_switch_is_dropped(self):
        p = _bare_player()
        p._pending_analysis_source = "old.mp4"
        p.video_path = "new.mp4"
        p.media_info = mock.MagicMock()
        calls = []
        p._start_analysis = lambda: calls.append(True)
        p._maybe_resume_pending_analysis()
        self.assertEqual(calls, [], "换源后旧请求应作废")
        self.assertIsNone(p._pending_analysis_source)

    def test_probe_error_cancels_pending_and_warns_once(self):
        p = _bare_player()
        p._pending_analysis_source = "x.mp4"
        captured = {}

        class _TM:
            def submit(self, kind, work, **callbacks):
                captured.update(callbacks)
                return mock.MagicMock()

        p.task_manager = _TM()
        p._start_media_info_probe()
        with mock.patch("tkinter.messagebox.showwarning") as warn:
            captured["on_error"](RuntimeError("probe boom"))
            captured["on_done"]("error")
        self.assertEqual(warn.call_count, 1, "探测失败应提示一次（不得静默）")
        self.assertIsNone(p._pending_analysis_source)

    def test_reload_clears_pending(self):
        p = _bare_player()
        p._pending_analysis_source = "x.mp4"
        p._invalidate_analysis_for_reload()
        self.assertIsNone(p._pending_analysis_source)


if __name__ == "__main__":
    unittest.main()
