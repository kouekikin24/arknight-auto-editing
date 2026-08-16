from __future__ import annotations

import unittest

import numpy as np

import analyzer
from frame_types import FRAME_TYPE_0_2X, FRAME_TYPE_1X, FRAME_TYPE_NORMAL
from timeline_plan import TimelinePlan


class AnalyzerTimelinePlanTests(unittest.TestCase):
    def test_speedup_mask_handles_many_runs_without_changing_run_positions(self) -> None:
        states = np.asarray(
            [FRAME_TYPE_1X, FRAME_TYPE_NORMAL, FRAME_TYPE_1X,
             FRAME_TYPE_1X, FRAME_TYPE_NORMAL, FRAME_TYPE_1X,
             FRAME_TYPE_1X, FRAME_TYPE_1X],
            dtype=np.int8,
        )
        excluded = np.zeros(len(states), dtype=bool)
        actual = analyzer._speedup_mask(states, FRAME_TYPE_1X, 2, excluded)
        # Every second frame in each contiguous 1x run is removed.
        np.testing.assert_array_equal(
            actual,
            np.asarray([False, False, False, True, False, False, True, False]),
        )

    def test_legacy_edits_resolve_to_one_half_open_plan(self) -> None:
        states = np.asarray(
            [
                FRAME_TYPE_1X,
                FRAME_TYPE_1X,
                FRAME_TYPE_1X,
                FRAME_TYPE_1X,
                FRAME_TYPE_NORMAL,
                FRAME_TYPE_NORMAL,
                FRAME_TYPE_NORMAL,
                FRAME_TYPE_NORMAL,
                FRAME_TYPE_0_2X,
                FRAME_TYPE_0_2X,
                FRAME_TYPE_0_2X,
                FRAME_TYPE_0_2X,
            ],
            dtype=np.int8,
        )
        pauses = [
            {
                "start": 4,
                "end": 7,
                "mode": "auto",
                "local_del_mask": np.asarray([0, 1, 2, 3], dtype=np.uint8),
            }
        ]
        clips = [
            {"start": 0, "end": 3, "keep_in": 1, "keep_out": 2}
        ]

        plan = analyzer.build_timeline_plan(
            12,
            states,
            pauses,
            [],
            clips,
            speedup_1x=True,
            speedup_02=True,
            speedup_02_factor=3,
        )

        self.assertIsInstance(plan, TimelinePlan)
        self.assertEqual(
            plan.deleted_ranges,
            ((0, 1), (2, 4), (5, 7), (9, 11)),
        )
        self.assertEqual(plan.kept_ranges, ((1, 2), (4, 5), (7, 9), (11, 12)))

        compatibility_mask = analyzer.build_delete_set(
            12,
            states,
            pauses,
            [],
            clips,
            speedup_1x=True,
            speedup_02=True,
            speedup_02_factor=3,
        )
        np.testing.assert_array_equal(
            compatibility_mask,
            np.asarray(plan.to_delete_mask(), dtype=bool),
        )

    def test_export_preflight_preserves_plan_identity(self) -> None:
        plan = TimelinePlan.from_deleted_ranges(8, [(1, 3), (5, 7)])
        preflight = analyzer.inspect_export_plan(plan, include_audio=False)
        self.assertEqual(preflight["timeline_fingerprint"], plan.fingerprint)
        self.assertEqual(preflight["n_ranges"], 3)
        self.assertEqual(preflight["kept_frames"], 4)


if __name__ == "__main__":
    unittest.main()
