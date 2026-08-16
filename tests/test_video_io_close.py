from __future__ import annotations

from queue import Queue
import threading
import unittest
from unittest import mock

import cv2

import video_io
from video_io import CMD_PLAY, CMD_QUIT, CMD_STOP, VideoIOThread


class _FakeCapture:
    def __init__(self):
        self.release_count = 0

    def get(self, prop):
        if prop == cv2.CAP_PROP_FPS:
            return 30.0
        if prop == cv2.CAP_PROP_FRAME_COUNT:
            return 12
        return 0

    def release(self):
        self.release_count += 1


class VideoIOCloseTests(unittest.TestCase):
    def test_close_joins_started_thread_and_releases_capture_once(self):
        capture = _FakeCapture()
        with mock.patch.object(video_io.cv2, "VideoCapture", return_value=capture):
            io_thread = VideoIOThread("fake.mp4", Queue())

        io_thread.start()
        self.assertTrue(io_thread.close(timeout=1.0))
        self.assertFalse(io_thread.is_alive())
        self.assertEqual(capture.release_count, 1)
        self.assertFalse(io_thread.send({"type": CMD_PLAY, "params": {}}))

    def test_close_releases_capture_when_thread_was_never_started(self):
        capture = _FakeCapture()
        with mock.patch.object(video_io.cv2, "VideoCapture", return_value=capture):
            io_thread = VideoIOThread("fake.mp4", Queue())

        self.assertTrue(io_thread.close(timeout=0.0))
        self.assertEqual(capture.release_count, 1)

    def test_close_is_idempotent_before_start(self):
        capture = _FakeCapture()
        with mock.patch.object(video_io.cv2, "VideoCapture", return_value=capture):
            io_thread = VideoIOThread("fake.mp4", Queue())

        self.assertTrue(io_thread.close(timeout=0.0))
        self.assertTrue(io_thread.close(timeout=0.0))
        self.assertEqual(capture.release_count, 1)

    def test_start_after_prestart_close_does_not_release_capture_twice(self):
        capture = _FakeCapture()
        with mock.patch.object(video_io.cv2, "VideoCapture", return_value=capture):
            io_thread = VideoIOThread("fake.mp4", Queue())

        self.assertTrue(io_thread.close(timeout=0.0))
        io_thread.start()
        self.assertTrue(io_thread.close(timeout=1.0))
        self.assertFalse(io_thread.is_alive())
        self.assertEqual(capture.release_count, 1)

    def test_close_discards_queued_work_and_rejects_late_commands(self):
        capture = _FakeCapture()
        with mock.patch.object(video_io.cv2, "VideoCapture", return_value=capture):
            io_thread = VideoIOThread("fake.mp4", Queue())

        self.assertTrue(io_thread.send({"type": CMD_PLAY, "params": {}}))
        self.assertTrue(io_thread.send({"type": CMD_STOP}))
        self.assertTrue(io_thread.close(timeout=0.0))

        self.assertEqual(io_thread.cmd_q.get_nowait()["type"], CMD_QUIT)
        self.assertTrue(io_thread.cmd_q.empty())
        self.assertFalse(io_thread.send({"type": CMD_STOP}))

    def test_close_timeout_can_be_retried_until_worker_exits(self):
        capture = _FakeCapture()
        with mock.patch.object(video_io.cv2, "VideoCapture", return_value=capture):
            io_thread = VideoIOThread("fake.mp4", Queue())

        started = threading.Event()
        release = threading.Event()

        def blocking_loop():
            started.set()
            release.wait(1.0)

        io_thread._run_loop = blocking_loop
        io_thread.start()
        self.assertTrue(started.wait(1.0))
        self.assertFalse(io_thread.close(timeout=0.0))
        self.assertTrue(io_thread.is_alive())
        self.assertEqual(capture.release_count, 0)
        self.assertFalse(io_thread.send({"type": CMD_STOP}))

        release.set()
        self.assertTrue(io_thread.close(timeout=1.0))
        self.assertFalse(io_thread.is_alive())
        self.assertEqual(capture.release_count, 1)


if __name__ == "__main__":
    unittest.main()
