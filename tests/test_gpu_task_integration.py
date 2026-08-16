from __future__ import annotations

from pathlib import Path
import threading
import time
import unittest
from unittest import mock

import settings_panel
from settings_panel import SettingsPanel
from task_manager import TaskManager, TaskCancelled


class _OwnerVar:
    def __init__(self, value, owner: int):
        self.value = value
        self.owner = owner
        self.get_threads = []
        self.set_threads = []

    def get(self):
        self.get_threads.append(threading.get_ident())
        if threading.get_ident() != self.owner:
            raise AssertionError("Tk variable read from worker thread")
        return self.value

    def set(self, value):
        self.set_threads.append(threading.get_ident())
        if threading.get_ident() != self.owner:
            raise AssertionError("Tk variable written from worker thread")
        self.value = value


class _Combo:
    def __init__(self):
        self.values = []
        self.state = None
        self.current_index = None

    def __setitem__(self, key, value):
        if key != "values":
            raise KeyError(key)
        self.values = list(value)

    def current(self, index):
        self.current_index = index

    def config(self, **kwargs):
        self.state = kwargs.get("state", self.state)


def _panel_stub(manager: TaskManager, owner: int, path: str = "auto"):
    panel = object.__new__(SettingsPanel)
    panel.task_manager = manager
    panel._owns_task_manager = False
    panel._gpu_probe_handle = None
    panel.ffmpeg_path_var = _OwnerVar(path, owner)
    panel.gpu_encoder_hint = _OwnerVar("", owner)
    panel.export_use_gpu_var = _OwnerVar(False, owner)
    panel.gpu_encoder_var = _OwnerVar("", owner)
    panel.gpu_encoder_combo = _Combo()
    return panel


class GpuTaskIntegrationTests(unittest.TestCase):
    @staticmethod
    def _dispatch_until(manager: TaskManager, predicate) -> None:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            manager.dispatch_pending()
            if predicate():
                return
            time.sleep(0.001)
        manager.dispatch_pending()

    def test_path_is_snapshotted_on_owner_and_ui_updates_are_dispatched(self):
        owner = threading.get_ident()
        manager = TaskManager(max_workers=1)
        panel = _panel_stub(manager, owner, "C:/ffmpeg.exe")
        worker_threads = []

        def probe(path, _context):
            worker_threads.append(threading.get_ident())
            self.assertEqual(path, "C:/ffmpeg.exe")
            return ["h264_nvenc"], ["h264_nvenc"]

        try:
            with mock.patch.object(settings_panel, "_probe_gpu_encoders", probe):
                handle = SettingsPanel._detect_gpu_encoder(panel)
                self.assertEqual(panel.ffmpeg_path_var.get_threads, [owner])
                assert handle.future is not None
                handle.future.result(timeout=2.0)
                self._dispatch_until(
                    manager,
                    lambda: panel.gpu_encoder_combo.values == ["h264_nvenc"],
                )

            self.assertTrue(worker_threads)
            self.assertTrue(all(value != owner for value in worker_threads))
            self.assertEqual(panel.gpu_encoder_combo.values, ["h264_nvenc"])
            self.assertEqual(panel.gpu_encoder_var.value, "h264_nvenc")
            self.assertTrue(panel.export_use_gpu_var.value)
            self.assertTrue(all(value == owner for value in panel.gpu_encoder_hint.set_threads))
        finally:
            manager.close(wait=True)

    def test_replaced_probe_cannot_overwrite_newer_result(self):
        owner = threading.get_ident()
        manager = TaskManager(max_workers=2)
        panel = _panel_stub(manager, owner, "old")
        old_started = threading.Event()
        release_old = threading.Event()

        def probe(path, context):
            if path == "old":
                old_started.set()
                release_old.wait(1.0)
                context.checkpoint()
                return ["old"], ["old"]
            return ["new"], ["new"]

        try:
            with mock.patch.object(settings_panel, "_probe_gpu_encoders", probe):
                old = SettingsPanel._detect_gpu_encoder(panel)
                self.assertTrue(old_started.wait(1.0))
                panel.ffmpeg_path_var.set("new")
                new = SettingsPanel._detect_gpu_encoder(panel)
                assert new.future is not None
                new.future.result(timeout=2.0)
                self._dispatch_until(
                    manager,
                    lambda: panel.gpu_encoder_combo.values == ["new"],
                )
                release_old.set()
                if old.future is not None:
                    try:
                        old.future.result(timeout=2.0)
                    except TaskCancelled:
                        pass
                manager.dispatch_pending()

            self.assertEqual(panel.gpu_encoder_var.value, "new")
            self.assertEqual(panel.gpu_encoder_combo.values, ["new"])
        finally:
            release_old.set()
            manager.close(wait=True)


if __name__ == "__main__":
    unittest.main()
