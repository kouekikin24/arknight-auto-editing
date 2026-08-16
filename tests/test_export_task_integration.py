from __future__ import annotations

import threading
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import numpy as np

import analyzer
from preview_player import VideoPreviewPlayer
from task_manager import TaskCancelled, TaskManager
from timeline_plan import TimelinePlan


class _OwnerValue:
    def __init__(self, owner, value=None):
        self.owner = owner
        self.value = value
        self.threads = []

    def set(self, value):
        self.threads.append(threading.get_ident())
        if threading.get_ident() != self.owner:
            raise AssertionError("UI value updated outside owner thread")
        self.value = value

    def get(self):
        if threading.get_ident() != self.owner:
            raise AssertionError("UI value read outside owner thread")
        return self.value


class _Button:
    def __init__(self, owner):
        self.owner = owner
        self.state = None
        self.threads = []

    def config(self, **kwargs):
        self.threads.append(threading.get_ident())
        if threading.get_ident() != self.owner:
            raise AssertionError("button updated outside owner thread")
        if "state" in kwargs:
            self.state = kwargs["state"]


class _Settings:
    def __init__(self, owner):
        self.export_btn = _Button(owner)
        self.export_progress_var = _OwnerValue(owner, 0)
        self.export_status_var = _OwnerValue(owner, "")
        self.segment_export_btn = _Button(owner)
        self.segment_export_progress_var = _OwnerValue(owner, 0)
        self.segment_export_status_var = _OwnerValue(owner, "")
        self.segment_split_by_speed_var = _OwnerValue(owner, False)
        self.merge_pause_ops_var = _OwnerValue(owner, False)
        self.params = {
            "output": "out.mp4",
            "quality": 8,
            "speedup_1x": False,
            "speedup_02": False,
            "speedup_02_factor": 10,
            "export_use_gpu": False,
            "gpu_encoder": "",
            "ffmpeg_path": None,
            "export_keep_audio": False,
        }

    def get_params(self):
        return dict(self.params)


class ExportTaskIntegrationTests(unittest.TestCase):
    @staticmethod
    def _dispatch_until(manager, predicate):
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            manager.dispatch_pending()
            if predicate():
                return
            time.sleep(0.001)
        manager.dispatch_pending()

    def test_full_export_worker_uses_snapshot_and_owner_callbacks(self):
        owner = threading.get_ident()
        manager = TaskManager(max_workers=1)
        player = VideoPreviewPlayer.__new__(VideoPreviewPlayer)
        player._closing = False
        player.video_path = "source.mp4"
        player.fps = 60.0
        player.settings = _Settings(owner)
        player.task_manager = manager
        player._export_handle = None
        player._build_timeline_plan = lambda **_kwargs: TimelinePlan.from_delete_mask(
            np.zeros(2, dtype=bool)
        )
        player._pause_preview_for_export = lambda: True
        resumed = []
        player._resume_preview_after_export = lambda: resumed.append(
            threading.get_ident()
        )
        worker_threads = []
        calls = []
        preflight = {
            "audio_drop_requires_confirmation": False,
            "audio_drop_reasons": [],
            "n_ranges": 1,
            "audio_limit": 80,
        }

        def export_call(video_path, output_path, plan, fps, _quality, progress, **kwargs):
            worker_threads.append(threading.get_ident())
            calls.append((video_path, output_path, plan, fps, kwargs))
            progress(0.5, 1, "half")
            return 2, 2, {"audio_mode": "disabled"}

        try:
            with (
                mock.patch.object(analyzer, "inspect_export_plan", return_value=preflight),
                mock.patch.object(analyzer, "export_video", side_effect=export_call),
                mock.patch("tkinter.messagebox.showinfo") as showinfo,
            ):
                player.export_video()
                handle = player._export_handle
                assert handle is not None and handle.future is not None
                self.assertEqual(
                    (handle.project_generation, handle.timeline_revision), (0, 0)
                )
                player.video_path = "new-source.mp4"
                player.fps = 24.0
                handle.future.result(timeout=2.0)
                self._dispatch_until(
                    manager,
                    lambda: player.settings.export_btn.state == "normal",
                )

            self.assertEqual(worker_threads and worker_threads[0] != owner, True)
            self.assertEqual(calls[0][0], "source.mp4")
            self.assertEqual(calls[0][3], 60.0)
            self.assertIs(calls[0][4]["preflight"], preflight)
            self.assertEqual(player.settings.export_progress_var.value, 100)
            self.assertEqual(player.settings.export_btn.state, "normal")
            self.assertEqual(resumed, [owner])
            self.assertEqual(showinfo.call_count, 1)
            self.assertTrue(
                all(thread_id == owner for thread_id in player.settings.export_status_var.threads)
            )
        finally:
            manager.close(wait=True)

    def test_full_export_remains_successful_if_scope_changes_after_final_replace(self):
        owner = threading.get_ident()
        manager = TaskManager(max_workers=1)
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "result.mp4"
            output.write_bytes(b"old")
            player = VideoPreviewPlayer.__new__(VideoPreviewPlayer)
            player._closing = False
            player.video_path = "source.mp4"
            player.fps = 30.0
            player.settings = _Settings(owner)
            player.settings.params["output"] = str(output)
            player.task_manager = manager
            player._export_handle = None
            player._build_timeline_plan = lambda **_kwargs: TimelinePlan.from_delete_mask(
                np.zeros(2, dtype=bool)
            )
            player._pause_preview_for_export = lambda: True
            player._resume_preview_after_export = lambda: None
            preflight = {
                "audio_drop_requires_confirmation": False,
                "audio_drop_reasons": [],
                "n_ranges": 1,
                "audio_limit": 80,
            }
            committed = threading.Event()
            release = threading.Event()

            def export_call(_video_path, output_path, _plan, _fps, _quality,
                            _progress, **kwargs):
                staging = Path(tmpdir) / "encoded.partial.mp4"
                staging.write_bytes(b"new")
                kwargs["commit_cb"](str(staging), output_path)
                committed.set()
                release.wait(1.0)
                return 2, 2, {"audio_mode": "disabled"}

            try:
                with (
                    mock.patch.object(analyzer, "inspect_export_plan", return_value=preflight),
                    mock.patch.object(analyzer, "export_video", side_effect=export_call),
                    mock.patch("tkinter.messagebox.showinfo") as showinfo,
                ):
                    player.export_video()
                    handle = player._export_handle
                    assert handle is not None and handle.future is not None
                    self.assertTrue(committed.wait(1.0))
                    self.assertEqual(output.read_bytes(), b"new")

                    with manager.scope_transition():
                        invalidated = manager.invalidate_scope(
                            project_generation=0, timeline_revision=1
                        )
                    self.assertEqual(invalidated, ())
                    self.assertFalse(handle.cancelled)

                    release.set()
                    handle.future.result(timeout=2.0)
                    self._dispatch_until(
                        manager,
                        lambda: player.settings.export_btn.state == "normal",
                    )

                self.assertEqual(showinfo.call_count, 1)
                self.assertEqual(player.settings.export_progress_var.value, 100)
                self.assertEqual(player.settings.export_status_var.value, "完成：2/2 帧")
            finally:
                release.set()
                manager.close(wait=True)

    def test_segment_export_is_serial_task_with_atomic_segment_file(self):
        owner = threading.get_ident()
        manager = TaskManager(max_workers=1)
        with tempfile.TemporaryDirectory() as tmpdir:
            output = os.path.join(tmpdir, "result.mp4")
            out_root = Path(tmpdir) / "result_segments"
            old_run = out_root / "run-prior"
            old_run.mkdir(parents=True)
            old_file = old_run / "2_normal.mp4"
            old_file.write_bytes(b"old-segment")
            user_file = out_root / "notes.txt"
            user_file.write_text("keep me", encoding="utf-8")
            player = VideoPreviewPlayer.__new__(VideoPreviewPlayer)
            player._closing = False
            player.video_path = "source.mp4"
            player.fps = 30.0
            player.total_frames = 2
            player.states_array = np.zeros(2, dtype=np.int8)
            player.settings = _Settings(owner)
            player.settings.params["output"] = output
            player.task_manager = manager
            player._segment_export_handle = None
            player._export_handle = None
            player._build_valid_segments_for_export = (
                lambda _states, _split, _merge: [
                    {"label": "normal", "ranges": [(0, 2)]}
                ]
            )
            player._pause_preview_for_export = lambda: True
            resumed = []
            player._resume_preview_after_export = lambda: resumed.append(
                threading.get_ident()
            )
            worker_threads = []

            def export_ranges(video_path, output_path, ranges, fps, quality, **kwargs):
                worker_threads.append(threading.get_ident())
                Path(output_path).write_bytes(b"segment")
                kwargs["cancel_cb"]()
                return 2, 2

            try:
                with (
                    mock.patch.object(analyzer, "export_ranges", side_effect=export_ranges),
                    mock.patch("tkinter.messagebox.showinfo") as showinfo,
                ):
                    player.export_segments()
                    handle = player._segment_export_handle
                    assert handle is not None and handle.future is not None
                    self.assertEqual(
                        (handle.project_generation, handle.timeline_revision), (0, 0)
                    )
                    handle.future.result(timeout=2.0)
                    self._dispatch_until(
                        manager,
                        lambda: player.settings.segment_export_btn.state == "normal",
                    )

                run_dirs = [path for path in out_root.glob("run-*") if path != old_run]
                self.assertEqual(len(run_dirs), 1)
                final_path = run_dirs[0] / "1_normal.mp4"
                self.assertTrue(final_path.exists())
                self.assertEqual(final_path.read_bytes(), b"segment")
                self.assertEqual(list(run_dirs[0].glob("*.mp4")), [final_path])
                self.assertEqual(old_file.read_bytes(), b"old-segment")
                self.assertEqual(user_file.read_text(encoding="utf-8"), "keep me")
                self.assertTrue(worker_threads and worker_threads[0] != owner)
                self.assertEqual(resumed, [owner])
                self.assertEqual(showinfo.call_count, 1)
            finally:
                manager.close(wait=True)

    def test_segment_cancel_keeps_completed_files_and_stops_before_next_segment(self):
        owner = threading.get_ident()
        manager = TaskManager(max_workers=1)
        with tempfile.TemporaryDirectory() as tmpdir:
            output = os.path.join(tmpdir, "result.mp4")
            player = VideoPreviewPlayer.__new__(VideoPreviewPlayer)
            player._closing = False
            player.video_path = "source.mp4"
            player.fps = 30.0
            player.total_frames = 6
            player.states_array = np.zeros(6, dtype=np.int8)
            player.settings = _Settings(owner)
            player.settings.params["output"] = output
            player.task_manager = manager
            player._segment_export_handle = None
            player._export_handle = None
            player._build_valid_segments_for_export = (
                lambda _states, _split, _merge: [
                    {"label": "normal", "ranges": [(0, 2)]},
                    {"label": "normal", "ranges": [(2, 4)]},
                    {"label": "normal", "ranges": [(4, 6)]},
                ]
            )
            player._pause_preview_for_export = lambda: True
            resumed = []
            player._resume_preview_after_export = lambda: resumed.append(
                threading.get_ident()
            )
            calls = []

            def export_ranges(_video_path, output_path, _ranges, _fps, _quality, **_kwargs):
                calls.append(output_path)
                index = len(calls)
                Path(output_path).write_bytes(f"segment-{index}".encode())
                # Cancel while the second file is being encoded.  The first
                # file has already been atomically committed; the second
                # staging file must be removed and the third must not start.
                if index == 2:
                    self.assertTrue(manager.cancel("player.export.segments"))
                return 2, 2

            try:
                with mock.patch.object(analyzer, "export_ranges", side_effect=export_ranges):
                    player.export_segments()
                    handle = player._segment_export_handle
                    assert handle is not None and handle.future is not None
                    with self.assertRaises(TaskCancelled):
                        handle.future.result(timeout=2.0)
                    self._dispatch_until(
                        manager,
                        lambda: player.settings.segment_export_btn.state == "normal",
                    )

                out_root = Path(tmpdir) / "result_segments"
                run_dirs = list(out_root.glob("run-*"))
                self.assertEqual(len(run_dirs), 1)
                out_dir = run_dirs[0]
                first = out_dir / "1_normal.mp4"
                second = out_dir / "2_normal.mp4"
                third = out_dir / "3_normal.mp4"
                self.assertEqual(len(calls), 2)
                self.assertEqual(first.read_bytes(), b"segment-1")
                self.assertFalse(second.exists())
                self.assertFalse(third.exists())
                self.assertEqual(list(out_dir.glob("*.partial.mp4*")), [])
                self.assertTrue(
                    all(
                        tid == owner
                        for tid in player.settings.segment_export_status_var.threads
                    )
                )
                self.assertEqual(player.settings.segment_export_btn.state, "normal")
                self.assertEqual(resumed, [owner])
            finally:
                manager.close(wait=True)


if __name__ == "__main__":
    unittest.main()
