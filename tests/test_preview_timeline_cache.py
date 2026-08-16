from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

from preview_player import VideoPreviewPlayer
from project_state import ProjectState
from task_manager import TaskManager


class PreviewTimelineCacheTests(unittest.TestCase):
    def _player_stub(self) -> VideoPreviewPlayer:
        player = VideoPreviewPlayer.__new__(VideoPreviewPlayer)
        player.total_frames = 8
        player.states_array = None
        player.pause_segments = [
            {"id": 1, "start": 2, "end": 4, "mode": "all"}
        ]
        player.speed_segments = []
        player.clip_segments = []
        player.project_state = ProjectState()
        player.project_state.replace_project(
            player.pause_segments,
            player.speed_segments,
            player.clip_segments,
        )
        player._timeline_revision = 0
        player._cut_plan_cache = None
        player.task_manager = TaskManager(max_workers=1)
        snapshot = player.project_state.snapshot()
        player.task_manager.invalidate_scope(
            project_generation=snapshot.project_generation,
            timeline_revision=snapshot.timeline_revision,
        )
        self.addCleanup(player.task_manager.close, wait=True)
        return player

    def test_cut_plan_is_reused_until_edit_revision_changes(self) -> None:
        player = self._player_stub()

        first = player._build_timeline_plan()
        second = player._build_timeline_plan()
        self.assertIs(first, second)
        self.assertEqual(first.deleted_ranges, ((2, 5),))

        scope_before = player._task_scope()
        player._invalidate_derived_timeline_plan()
        third = player._build_timeline_plan()
        self.assertIsNot(third, first)
        self.assertEqual(third.fingerprint, first.fingerprint)
        self.assertEqual(player._task_scope(), scope_before)

    def test_explicit_state_snapshot_does_not_poison_cut_cache(self) -> None:
        player = self._player_stub()
        cached = player._build_timeline_plan()
        explicit = player._build_timeline_plan(
            states=np.zeros(player.total_frames, dtype=np.int8)
        )
        self.assertIsNot(explicit, cached)
        self.assertIs(player._build_timeline_plan(), cached)

    def test_analysis_result_invalidates_a_preexisting_plan(self) -> None:
        player = self._player_stub()
        old_plan = player._build_timeline_plan()
        player.timeline = SimpleNamespace(
            pause_segments=None,
            speed_segments=None,
            clip_segments=None,
            selected_pause_id=None,
            mark_dirty=lambda: None,
            redraw=lambda: None,
        )
        player.settings = SimpleNamespace(set_selected_pause=lambda *_args: None)
        player.btn_analyze = SimpleNamespace(config=lambda **_kwargs: None)
        revision_before = player.project_state.timeline_revision

        states = np.zeros(player.total_frames, dtype=np.int8)
        pauses = [{"id": 2, "start": 5, "end": 6, "mode": "all"}]
        with mock.patch("tkinter.messagebox.showinfo"):
            player._finish_analysis(states, np.zeros_like(states), pauses, [])

        new_plan = player._build_timeline_plan()
        self.assertIsNot(new_plan, old_plan)
        self.assertEqual(new_plan.deleted_ranges, ((5, 7),))
        self.assertEqual(
            player.project_state.timeline_revision, revision_before + 1
        )


if __name__ == "__main__":
    unittest.main()
