from __future__ import annotations

import unittest

import numpy as np

from edit_commands import SetClipBounds, SetPauseMaskRun, SetPauseMode
from project_state import ProjectState


class ProjectStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = ProjectState()
        self.state.replace_project(
            pause_segments=[
                {
                    "id": 1,
                    "start": 2,
                    "end": 5,
                    "mode": "auto",
                    "local_del_mask": np.asarray([0, 1, 1, 0], dtype=np.uint8),
                }
            ],
            speed_segments=[{"id": 2, "start": 0, "end": 1, "type": 1}],
            clip_segments=[
                {"id": 3, "start": 6, "end": 9, "keep_in": 6, "keep_out": 9}
            ],
        )

    def test_project_and_timeline_revisions_are_independent(self) -> None:
        self.assertEqual(self.state.project_generation, 1)
        self.assertEqual(self.state.timeline_revision, 0)
        snapshot = self.state.apply(SetPauseMode(1, "all"))
        self.assertEqual(snapshot.project_generation, 1)
        self.assertEqual(snapshot.timeline_revision, 1)
        replacement = self.state.replace_project()
        self.assertEqual(replacement.project_generation, 2)
        self.assertEqual(replacement.timeline_revision, 0)

    def test_snapshots_do_not_share_mutable_masks(self) -> None:
        before = self.state.snapshot()
        edited = self.state.apply(SetPauseMaskRun(1, 1, 3, 2))
        np.testing.assert_array_equal(
            before.pause_segments[0]["local_del_mask"], [0, 1, 1, 0]
        )
        np.testing.assert_array_equal(
            edited.pause_segments[0]["local_del_mask"], [0, 2, 2, 0]
        )
        edited.pause_segments[0]["local_del_mask"][0] = 3
        np.testing.assert_array_equal(
            self.state.snapshot().pause_segments[0]["local_del_mask"], [0, 2, 2, 0]
        )

    def test_clip_command_uses_half_open_keep_range(self) -> None:
        snapshot = self.state.apply(SetClipBounds(3, 7, 9))
        clip = snapshot.clip_segments[0]
        self.assertEqual((clip["keep_in"], clip["keep_out"]), (7, 8))
        with self.assertRaises(ValueError):
            self.state.apply(SetClipBounds(3, 9, 8))

    def test_invalid_commands_fail_without_advancing_revision(self) -> None:
        revision = self.state.timeline_revision
        with self.assertRaises(KeyError):
            self.state.apply(SetPauseMode(999, "all"))
        with self.assertRaises(ValueError):
            self.state.apply(SetPauseMaskRun(1, -1, 2, 2))
        self.assertEqual(self.state.timeline_revision, revision)

    def test_command_batch_is_atomic_and_publishes_one_revision(self) -> None:
        before = self.state.snapshot()
        edited = self.state.apply_many(
            (SetPauseMaskRun(1, 0, 1, 2), SetPauseMode(1, "keep"))
        )
        self.assertEqual(edited.timeline_revision, before.timeline_revision + 1)
        self.assertEqual(edited.pause_segments[0]["mode"], "keep")
        np.testing.assert_array_equal(
            edited.pause_segments[0]["local_del_mask"], [2, 1, 1, 0]
        )

        with self.assertRaises(KeyError):
            self.state.apply_many(
                (SetPauseMode(1, "all"), SetPauseMode(999, "keep"))
            )
        after_failure = self.state.snapshot()
        self.assertEqual(after_failure.timeline_revision, edited.timeline_revision)
        self.assertEqual(after_failure.pause_segments[0]["mode"], "keep")

    def test_replacement_rejects_duplicate_ids_and_malformed_segments(self) -> None:
        revision = self.state.timeline_revision
        with self.assertRaisesRegex(ValueError, "duplicate pause segment id"):
            self.state.replace_timeline(
                [
                    {"id": 1, "start": 0, "end": 0, "mode": "all"},
                    {"id": 1, "start": 1, "end": 1, "mode": "all"},
                ],
                [],
                [],
            )
        with self.assertRaisesRegex(ValueError, "mask length"):
            self.state.replace_timeline(
                [
                    {
                        "id": 4,
                        "start": 0,
                        "end": 2,
                        "mode": "auto",
                        "local_del_mask": np.asarray([0, 1], dtype=np.uint8),
                    }
                ],
                [],
                [],
            )
        with self.assertRaisesRegex(ValueError, "keep range"):
            self.state.replace_timeline(
                [],
                [],
                [
                    {
                        "id": 5,
                        "start": 3,
                        "end": 5,
                        "keep_in": 2,
                        "keep_out": 5,
                    }
                ],
            )
        self.assertEqual(self.state.timeline_revision, revision)


if __name__ == "__main__":
    unittest.main()
