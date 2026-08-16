from __future__ import annotations

import unittest

from timeline_plan import TimelinePlan


class TimelinePlanTests(unittest.TestCase):
    def test_ranges_are_sorted_unioned_and_complemented(self) -> None:
        plan = TimelinePlan.from_deleted_ranges(
            12,
            [(8, 10), (2, 4), (3, 6), (10, 10), (0, 1)],
        )
        self.assertEqual(plan.deleted_ranges, ((0, 1), (2, 6), (8, 10)))
        self.assertEqual(plan.kept_ranges, ((1, 2), (6, 8), (10, 12)))
        self.assertEqual(plan.virtual_prefix, (0, 1, 3, 5))
        self.assertEqual(plan.deleted_frames, 7)
        self.assertEqual(plan.kept_frames, 5)
        self.assertEqual(sum(end - start for start, end in plan.kept_ranges), 5)

    def test_mapping_is_exact_and_deleted_frames_do_not_snap(self) -> None:
        plan = TimelinePlan.from_deleted_ranges(10, [(2, 4), (7, 9)])
        for source in (0, 1, 4, 5, 6, 9):
            virtual = plan.source_to_virtual(source)
            self.assertIsNotNone(virtual)
            self.assertEqual(plan.virtual_to_source(virtual), source)
        for source in (2, 3, 7, 8):
            self.assertIsNone(plan.source_to_virtual(source))
            self.assertTrue(plan.is_deleted(source))
        self.assertEqual(plan.snap_source(3, "next"), 4)
        self.assertEqual(plan.snap_source(3, "previous"), 1)
        self.assertEqual(plan.snap_source(3, "nearest"), 4)

    def test_all_deleted_and_empty_timelines(self) -> None:
        empty = TimelinePlan.from_deleted_ranges(0, [])
        self.assertEqual(empty.kept_ranges, ())
        self.assertEqual(empty.kept_frames, 0)
        all_deleted = TimelinePlan.from_deleted_ranges(4, [(0, 4)])
        self.assertEqual(all_deleted.kept_ranges, ())
        self.assertEqual(all_deleted.to_delete_mask(), (True, True, True, True))
        with self.assertRaises(IndexError):
            all_deleted.virtual_to_source(0)

    def test_invalid_ranges_are_rejected_instead_of_clamped(self) -> None:
        with self.assertRaises(ValueError):
            TimelinePlan.from_deleted_ranges(4, [(-1, 2)])
        with self.assertRaises(ValueError):
            TimelinePlan.from_deleted_ranges(4, [(3, 2)])
        with self.assertRaises(ValueError):
            TimelinePlan.from_deleted_ranges(4, [(0, 5)])
        with self.assertRaises(ValueError):
            TimelinePlan.from_deleted_ranges(4, [(0.5, 2)])

    def test_fingerprint_and_serialization_are_stable(self) -> None:
        left = TimelinePlan.from_delete_mask([False, True, True, False])
        right = TimelinePlan.from_deleted_ranges(4, [(1, 3)])
        self.assertEqual(left.fingerprint, right.fingerprint)
        self.assertEqual(left.as_dict(), right.as_dict())

    def test_kept_range_constructor_uses_half_open_ranges(self) -> None:
        plan = TimelinePlan.from_kept_ranges(
            12,
            [(8, 12), (1, 3), (3, 5), (8, 10)],
        )
        self.assertEqual(plan.kept_ranges, ((1, 5), (8, 12)))
        self.assertEqual(plan.deleted_ranges, ((0, 1), (5, 8)))
        self.assertEqual(plan.kept_frames, 8)

    def test_kept_range_constructor_rejects_inclusive_overflow(self) -> None:
        with self.assertRaises(ValueError):
            TimelinePlan.from_kept_ranges(4, [(0, 5)])


if __name__ == "__main__":
    unittest.main()
