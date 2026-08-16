from __future__ import annotations

import tempfile
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import analyzer
import numpy as np
from task_manager import TaskCancelled
from timeline_plan import TimelinePlan


class ExportCancellationTests(unittest.TestCase):
    def test_terminate_process_escalates_from_terminate_to_kill(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = [
            subprocess.TimeoutExpired(["ffmpeg"], 2.0),
            subprocess.TimeoutExpired(["ffmpeg"], 2.0),
        ]

        analyzer._terminate_process(process)

        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)

    def test_interruptible_ffmpeg_terminates_process_on_cancel(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 0

        def cancel():
            raise TaskCancelled("cancelled")

        with mock.patch.object(analyzer.subprocess, "Popen", return_value=process):
            with self.assertRaises(TaskCancelled):
                analyzer._run_ffmpeg_interruptible(
                    ["ffmpeg", "-i", "source.mp4"],
                    timeout=10.0,
                    cancel_cb=cancel,
                )

        process.terminate.assert_called_once_with()
        process.kill.assert_not_called()

    def test_interruptible_ffmpeg_checks_cancel_after_process_exit(self) -> None:
        process = mock.Mock()
        process.poll.return_value = 0
        process.wait.return_value = 0
        checks = 0

        def cancel():
            nonlocal checks
            checks += 1
            if checks == 2:
                raise TaskCancelled("cancelled after exit")

        with mock.patch.object(analyzer.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(TaskCancelled, "after exit"):
                analyzer._run_ffmpeg_interruptible(
                    ["ffmpeg", "-i", "source.mp4"],
                    timeout=10.0,
                    cancel_cb=cancel,
                )

        self.assertEqual(checks, 2)
        process.wait.assert_called_once_with(timeout=0.1)
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_fast_filter_cancellation_does_not_fall_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "filtered.mp4"
            with (
                mock.patch.object(analyzer, "resolve_ffmpeg_path", return_value="ffmpeg"),
                mock.patch.object(analyzer, "_video_encoder_args", return_value=[]),
                mock.patch.object(
                    analyzer,
                    "_run_ffmpeg_interruptible",
                    side_effect=TaskCancelled("cancelled"),
                ) as run_ffmpeg,
            ):
                with self.assertRaises(TaskCancelled):
                    analyzer._export_ranges_with_ffmpeg_filters(
                        "source.mp4",
                        str(output),
                        [(0, 1)],
                        30.0,
                        8,
                        False,
                        "",
                        False,
                        ffmpeg_path="ffmpeg",
                        cancel_cb=lambda: None,
                    )

            run_ffmpeg.assert_called_once()
            self.assertFalse(output.exists())

    def test_audio_mux_cancellation_propagates_and_cleans_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "muxed.mp4"
            output.write_bytes(b"partial")
            with (
                mock.patch.object(analyzer, "resolve_ffmpeg_path", return_value="ffmpeg"),
                mock.patch.object(
                    analyzer,
                    "_run_ffmpeg_interruptible",
                    side_effect=TaskCancelled("cancelled"),
                ) as run_ffmpeg,
            ):
                with self.assertRaises(TaskCancelled):
                    analyzer._mux_audio_for_ranges(
                        "source.mp4",
                        "video-only.mp4",
                        str(output),
                        [(0, 1)],
                        30.0,
                        ffmpeg_path="ffmpeg",
                        source_has_audio=True,
                        cancel_cb=lambda: None,
                    )

            run_ffmpeg.assert_called_once()
            self.assertFalse(output.exists())

    def test_export_ranges_requires_ffmpeg_before_opening_media(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "output.mp4"

            with (
                mock.patch.object(
                    analyzer, "resolve_ffmpeg_path", side_effect=FileNotFoundError
                ),
                mock.patch.object(analyzer.cv2, "VideoCapture") as video_capture,
                mock.patch.object(
                    analyzer,
                    "_export_ranges_with_ffmpeg_filters",
                    side_effect=AssertionError("filter export should not be attempted"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "分段导出需要 FFmpeg"):
                    analyzer.export_ranges(
                        "source.mp4",
                        str(output),
                        [(0, 3)],
                        30.0,
                        8,
                    )

            video_capture.assert_not_called()
            self.assertFalse(output.exists())

    def test_ffmpeg_pipe_cancellation_releases_capture_and_process(self) -> None:
        class FakeCapture:
            def __init__(self):
                self.position = 0
                self.release_count = 0
                self.read_count = 0

            def set(self, _prop, value):
                self.position = int(value)

            def get(self, _prop):
                return self.position

            def read(self):
                self.read_count += 1
                frame = np.zeros((2, 2, 3), dtype=np.uint8)
                self.position += 1
                return True, frame

            def release(self):
                self.release_count += 1

        class FakeStdin:
            def __init__(self):
                self.frames = []
                self.closed = False

            def write(self, frame):
                self.frames.append(frame)

            def close(self):
                self.closed = True

        capture = FakeCapture()
        stdin = FakeStdin()
        process = mock.Mock()
        process.stdin = stdin
        process.poll.return_value = None
        process.wait.return_value = 0
        stderr = tempfile.TemporaryFile()
        pipe = analyzer._FFmpegPipe(process, stderr)
        checks = 0

        def cancel():
            nonlocal checks
            checks += 1
            if checks >= 4:
                raise TaskCancelled("cancelled")

        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "partial.mp4"

            def open_pipe(path, *_args, **_kwargs):
                Path(path).write_bytes(b"partial")
                return pipe

            process.poll.return_value = None

            try:
                with (
                    mock.patch.object(
                        analyzer, "resolve_ffmpeg_path", return_value="ffmpeg"
                    ),
                    mock.patch.object(
                        analyzer,
                        "_export_ranges_with_ffmpeg_filters",
                        return_value=False,
                    ),
                    mock.patch.object(
                        analyzer.cv2, "VideoCapture", return_value=capture
                    ),
                    mock.patch.object(
                        analyzer, "_open_ffmpeg_pipe_writer", side_effect=open_pipe
                    ),
                ):
                    with self.assertRaises(TaskCancelled):
                        analyzer.export_ranges(
                            "source.mp4",
                            str(output),
                            [(0, 3)],
                            30.0,
                            8,
                            cancel_cb=cancel,
                        )
            finally:
                try:
                    stderr.close()
                except Exception:
                    pass

            self.assertFalse(output.exists())

        self.assertEqual(capture.release_count, 1)
        self.assertEqual(len(stdin.frames), 1)
        self.assertTrue(stdin.closed)
        self.assertTrue(stderr.closed)
        process.terminate.assert_called_once_with()
        process.kill.assert_not_called()

    def test_ffmpeg_pipe_start_failure_releases_capture_and_partial_output(self) -> None:
        class FakeCapture:
            def __init__(self):
                self.position = 0
                self.release_count = 0

            def set(self, _prop, value):
                self.position = int(value)

            def get(self, _prop):
                return self.position

            def read(self):
                self.position += 1
                return True, np.zeros((2, 2, 3), dtype=np.uint8)

            def release(self):
                self.release_count += 1

        capture = FakeCapture()

        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "partial.mp4"

            def fail_to_start_pipe(path, *_args, **_kwargs):
                Path(path).write_bytes(b"partial")
                raise RuntimeError("ffmpeg pipe failed to start")

            with (
                mock.patch.object(analyzer, "resolve_ffmpeg_path", return_value="ffmpeg"),
                mock.patch.object(
                    analyzer, "_export_ranges_with_ffmpeg_filters", return_value=False
                ),
                mock.patch.object(analyzer.cv2, "VideoCapture", return_value=capture),
                mock.patch.object(
                    analyzer,
                    "_open_ffmpeg_pipe_writer",
                    side_effect=fail_to_start_pipe,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "ffmpeg pipe failed to start"):
                    analyzer.export_ranges(
                        "source.mp4",
                        str(output),
                        [(0, 1)],
                        30.0,
                        8,
                    )

            self.assertEqual(capture.release_count, 1)
            self.assertFalse(output.exists())

    def test_full_frame_export_early_eof_preserves_destination(self) -> None:
        class FakeCapture:
            def __init__(self):
                self.position = 0
                self.release_count = 0
                self.read_count = 0

            def set(self, _prop, value):
                self.position = int(value)

            def read(self):
                self.read_count += 1
                if self.read_count <= 2:
                    self.position += 1
                    return True, np.zeros((2, 2, 3), dtype=np.uint8)
                return False, None

            def release(self):
                self.release_count += 1

        class FakeStdin:
            def __init__(self):
                self.closed = False

            def write(self, _frame):
                return None

            def close(self):
                self.closed = True

        capture = FakeCapture()
        stdin = FakeStdin()
        process = mock.Mock()
        process.stdin = stdin
        process.poll.return_value = None
        process.wait.return_value = 0
        stderr = tempfile.TemporaryFile()
        pipe = analyzer._FFmpegPipe(process, stderr)

        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "result.mp4"
            output.write_bytes(b"old")

            def open_pipe(path, *_args, **_kwargs):
                Path(path).write_bytes(b"partial")
                return pipe

            preflight = analyzer.inspect_export_plan(
                np.zeros(3, dtype=bool),
                include_audio=False,
            )
            preflight.update({
                "video_path": os.path.normcase(os.path.abspath("source.mp4")),
                "ffmpeg_path": "ffmpeg",
                "export_block_reasons": [],
                "export_blocked": False,
            })
            try:
                with (
                    mock.patch.object(analyzer.cv2, "VideoCapture", return_value=capture),
                    mock.patch.object(
                        analyzer,
                        "_export_ranges_with_ffmpeg_filters",
                        return_value=False,
                    ),
                    mock.patch.object(
                        analyzer, "_open_ffmpeg_pipe_writer", side_effect=open_pipe
                    ),
                ):
                    with self.assertRaisesRegex(RuntimeError, "提前结束"):
                        analyzer.export_video(
                            "source.mp4",
                            str(output),
                            np.zeros(3, dtype=bool),
                            30.0,
                            8,
                            include_audio=False,
                            preflight=preflight,
                        )
            finally:
                try:
                    stderr.close()
                except Exception:
                    pass

            self.assertEqual(output.read_bytes(), b"old")
            self.assertEqual(list(Path(tmpdir).glob(".*.partial.mp4*")), [])

        self.assertEqual(capture.release_count, 1)
        self.assertTrue(stdin.closed)
        process.terminate.assert_called_once_with()

    def test_preflight_is_forwarded_without_a_second_probe(self) -> None:
        plan = TimelinePlan.from_delete_mask(np.zeros(1, dtype=bool))
        preflight = {
            "n_ranges": len(plan.kept_ranges),
            "audio_limit": 80,
            "audio_probe": {
                "status": "pass",
                "present": False,
            },
            "audio_drop_reasons": [],
            "audio_drop_requires_confirmation": False,
            "ffmpeg_path": None,
            "timeline_fingerprint": plan.fingerprint,
            "include_audio": False,
            "video_path": os.path.normcase(os.path.abspath("source.mp4")),
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "result.mp4"
            seen = {}

            def impl(_video, staging, *_args, **kwargs):
                seen["preflight"] = kwargs.get("preflight")
                Path(staging).write_bytes(b"encoded")
                return 1, 1, {"audio_mode": "disabled"}

            with mock.patch.object(
                analyzer,
                "inspect_export_plan",
                side_effect=AssertionError("preflight was recomputed"),
            ):
                with mock.patch.object(analyzer, "_export_video_impl", side_effect=impl):
                    result = analyzer.export_video(
                        "source.mp4",
                        str(output),
                        plan,
                        30.0,
                        8,
                        include_audio=False,
                        preflight=preflight,
                    )

            self.assertEqual(result[:2], (1, 1))
            self.assertIs(seen["preflight"], preflight)
            self.assertEqual(output.read_bytes(), b"encoded")

    def test_ffmpeg_writer_is_terminated_when_cancelled_while_closing(self) -> None:
        events = []

        class FakeStdin:
            def __init__(self):
                self.closed = False

            def close(self):
                events.append("stdin.close")
                self.closed = True

        class FakeProcess:
            def __init__(self):
                self.terminated = False
                self.killed = False
                self.stdin = FakeStdin()

            def poll(self):
                events.append("poll")
                return None

            def terminate(self):
                events.append("terminate")
                self.terminated = True

            def kill(self):
                events.append("kill")
                self.killed = True

            def wait(self, timeout=None):
                events.append("wait")
                if not self.killed:
                    raise subprocess.TimeoutExpired(["ffmpeg"], timeout)
                return 0

        process = FakeProcess()
        stderr = tempfile.TemporaryFile()
        pipe = analyzer._FFmpegPipe(process, stderr)
        def cancel():
            raise TaskCancelled("cancelled")

        try:
            with self.assertRaises(TaskCancelled):
                analyzer._close_video_writer(
                    "ffmpeg", None, pipe, cancel_cb=cancel
                )
        finally:
            # The cancellation path closes stderr itself; this is harmless if
            # a future implementation changes that ownership.
            try:
                stderr.close()
            except Exception:
                pass

        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertLess(events.index("terminate"), events.index("stdin.close"))
        self.assertLess(events.index("kill"), events.index("stdin.close"))


if __name__ == "__main__":
    unittest.main()
