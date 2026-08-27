from __future__ import annotations

import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import preview_player
from preview_player import VideoPreviewPlayer
from task_manager import TaskManager


class _Context:
    def __init__(self) -> None:
        self.checkpoints = 0

    def checkpoint(self) -> None:
        self.checkpoints += 1


class _Settings:
    def get_params(self) -> dict:
        return {
            "ffmpeg_path": "D:/tools/ffmpeg.exe",
        }


class PreviewMediaInfoTests(unittest.TestCase):
    def test_probe_task_passes_the_bound_tool_paths(self) -> None:
        context = _Context()
        snapshot = {
            "video_path": "D:/media/source.mp4",
            "ffmpeg_path": "D:/tools/ffmpeg.exe",
        }
        expected = object()

        with mock.patch("media_info.probe_media", return_value=expected) as probe:
            result = preview_player._run_media_info_task(context, snapshot)

        self.assertIs(result, expected)
        self.assertEqual(context.checkpoints, 2)
        probe.assert_called_once_with(
            "D:/media/source.mp4",
            ffmpeg_path="D:/tools/ffmpeg.exe",
        )

    def test_probe_result_is_published_only_for_the_current_source(self) -> None:
        manager = TaskManager(max_workers=1)
        player = VideoPreviewPlayer.__new__(VideoPreviewPlayer)
        player._closing = False
        player.video_path = "source.mp4"
        player.settings = _Settings()
        player.task_manager = manager
        player._timeline_revision = 0
        player.media_info = None
        player.media_info_error = None
        player._media_info_handle = None
        player.frame_pts_error = None
        player.frame_pts_status = None
        expected = SimpleNamespace(source_path=Path("source.mp4").resolve())

        try:
            with (
                mock.patch.object(
                    preview_player,
                    "_run_media_info_task",
                    return_value=expected,
                ),
                mock.patch.object(player, "_start_frame_pts_certification") as certify,
            ):
                player._start_media_info_probe()
                handle = player._media_info_handle
                self.assertIsNotNone(handle)
                assert handle is not None and handle.future is not None
                self.assertIsNone(handle.timeline_revision)
                manager.invalidate_scope(project_generation=0, timeline_revision=1)
                self.assertFalse(handle.cancelled)
                handle.future.result(timeout=2.0)

                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and player.media_info is None:
                    manager.dispatch_pending()
                    time.sleep(0.001)
                manager.dispatch_pending()

            self.assertIs(player.media_info, expected)
            self.assertIsNone(player.media_info_error)
            certify.assert_called_once()
        finally:
            manager.close(wait=True)


if __name__ == "__main__":
    unittest.main()
