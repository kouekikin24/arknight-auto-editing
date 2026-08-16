from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

import numpy as np

import preview_player
import analyzer
from preview_player import VideoPreviewPlayer
from task_manager import TaskCancelled, TaskManager


class _Button:
    def __init__(self, owner):
        self.owner = owner
        self.calls = []

    def config(self, **kwargs):
        if threading.get_ident() != self.owner:
            raise AssertionError("button updated outside owner thread")
        self.calls.append(kwargs)


class _Settings:
    def __init__(self):
        self.params = {
            "proc_res": (400, 225),
            "thresholds": {"pause": 1},
            "compare": {"motion_thresh": 2.0},
            "batch": 4,
            "threads": 1,
            "decode_backend": "opencv",
            "ffmpeg_path": None,
        }

    def get_params(self):
        return {
            **self.params,
            "proc_res": tuple(self.params["proc_res"]),
            "thresholds": dict(self.params["thresholds"]),
            "compare": dict(self.params["compare"]),
        }


class PreviewAnalysisTaskTests(unittest.TestCase):
    @staticmethod
    def _dispatch_until(manager, predicate):
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            manager.dispatch_pending()
            if predicate():
                return
            time.sleep(0.001)
        manager.dispatch_pending()

    def _player_stub(self, manager):
        owner = threading.get_ident()
        player = VideoPreviewPlayer.__new__(VideoPreviewPlayer)
        player.task_manager = manager
        player._analysis_handle = None
        player._closing = False
        player.video_path = "old.mp4"
        player.fps = 60.0
        player.settings = _Settings()
        player.btn_analyze = _Button(owner)
        player._finished = []
        player._finish_analysis = lambda *args, **kwargs: player._finished.append(
            (args, kwargs, threading.get_ident())
        )
        return player

    def test_reload_invalidates_old_analysis_and_new_snapshot_wins(self):
        owner = threading.get_ident()
        manager = TaskManager(max_workers=2)
        player = self._player_stub(manager)
        old_started = threading.Event()
        release_old = threading.Event()
        snapshots = []

        def run_task(context, snapshot):
            snapshots.append(dict(snapshot))
            if snapshot["video_path"] == "old.mp4":
                old_started.set()
                release_old.wait(1.0)
                context.checkpoint()
            states = np.array([1], dtype=np.int8)
            return states, states, [], [], snapshot["backend_label"], False, False

        try:
            with mock.patch.object(preview_player, "_run_analysis_task", run_task):
                player._start_analysis()
                old = player._analysis_handle
                self.assertEqual(
                    (old.project_generation, old.timeline_revision), (0, 0)
                )
                self.assertTrue(old_started.wait(1.0))

                player._invalidate_analysis_for_reload()
                player.video_path = "new.mp4"
                player.fps = 24.0
                player.settings.params["batch"] = 8
                player._start_analysis()
                new = player._analysis_handle
                assert new is not None and new.future is not None
                self.assertEqual(
                    (new.project_generation, new.timeline_revision), (0, 0)
                )
                new.future.result(timeout=2.0)
                self._dispatch_until(manager, lambda: len(player._finished) == 1)

                release_old.set()
                assert old is not None and old.future is not None
                try:
                    old.future.result(timeout=2.0)
                except TaskCancelled:
                    pass
                manager.dispatch_pending()

            self.assertEqual([item["video_path"] for item in snapshots], ["old.mp4", "new.mp4"])
            self.assertEqual(snapshots[0]["fps"], 60.0)
            self.assertEqual(snapshots[0]["batch"], 4)
            self.assertEqual(snapshots[1]["fps"], 24.0)
            self.assertEqual(snapshots[1]["batch"], 8)
            self.assertEqual(len(player._finished), 1)
            self.assertEqual(player._finished[0][2], owner)
            self.assertEqual(manager.current_generation("player.analysis"), 3)
        finally:
            release_old.set()
            manager.close(wait=True)

    def test_worker_uses_plain_snapshot_and_reports_through_context(self):
        class Context:
            def __init__(self):
                self.checkpoints = 0
                self.progress = []

            def checkpoint(self):
                self.checkpoints += 1

            def report(self, value):
                self.progress.append(value)

        class Capture:
            def read(self):
                return True, np.zeros((100, 200, 3), dtype=np.uint8)

            def release(self):
                pass

        seen = {}

        def analyze_call(path, configs, thresholds, proc_res, batch, threads, progress, **kwargs):
            seen["analyze"] = (
                path, configs, thresholds, proc_res, batch, threads, kwargs
            )
            progress(0.25)
            values = np.array([1, 2], dtype=np.int8)
            return values, values, {"complete": True, "pause_boundary_diffs": []}

        def build_call(states, diffs, path, proc_res, compare, fps, progress, **kwargs):
            seen["build"] = (path, proc_res, compare, fps, kwargs)
            progress(0.75)
            return [{"start": 0, "end": 0}], []

        snapshot = {
            "video_path": "snap.mp4",
            "fps": 59.94,
            "proc_res": (400, 225),
            "thresholds": {"pause": 1},
            "compare": {"motion": 2},
            "batch": 8,
            "threads": 3,
            "backend_key": "opencv",
            "backend_label": "OpenCV",
            "backend_note": "",
            "ffmpeg_path": None,
        }
        context = Context()
        with (
            mock.patch.object(preview_player.cv2, "VideoCapture", return_value=Capture()),
            mock.patch.object(analyzer, "load_templates", return_value=(["cfg"], 1)),
            mock.patch.object(analyzer, "analyze_video_with_context", side_effect=analyze_call),
            mock.patch.object(analyzer, "build_segments", side_effect=build_call),
            mock.patch.object(
                analyzer, "analysis_context_skips_second_scan", return_value=True
            ),
        ):
            result = preview_player._run_analysis_task(context, snapshot)

        self.assertEqual(seen["analyze"][0], "snap.mp4")
        self.assertEqual(seen["analyze"][3], (400, 200))
        self.assertEqual(seen["build"][1], (400, 200))
        self.assertEqual(seen["build"][3], 59.94)
        self.assertEqual(context.progress, [("OpenCV", 0.25), ("OpenCV", 0.75)])
        self.assertGreaterEqual(context.checkpoints, 5)
        self.assertTrue(result[5])
        self.assertFalse(result[6])


if __name__ == "__main__":
    unittest.main()
