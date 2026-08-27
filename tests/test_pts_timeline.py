from __future__ import annotations

import hashlib
import json
from fractions import Fraction
from pathlib import Path
import tempfile
import unittest

import media_info
from pts_timeline import CertifiedPtsTimeline
from timeline_plan import TimelinePlan


class CertifiedPtsTimelineTests(unittest.TestCase):
    @staticmethod
    def _certification(root: Path, rows: list[dict[str, int]]) -> media_info.FramePtsCertification:
        evidence = root / "frame-pts.json"
        source_sha256 = hashlib.sha256(b"source").hexdigest()
        source_path = root / "source.mp4"
        source_path.write_bytes(b"source")
        ffmpeg = root / "ffmpeg.exe"
        ffmpeg.write_bytes(b"ffmpeg-tool")
        ffmpeg_info = media_info.ToolInfo(
            ffmpeg,
            media_info._sha256_file(ffmpeg),
            "ffmpeg version 7.1.1-essentials_build-www.gyan.dev",
            True,
        )
        evidence.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "kind": "production_frame_pts_certification",
                    "status": "PASS",
                    "authoritative_frame_timeline": True,
                    "reason_codes": [],
                    "scope": "full",
                    "source": {
                        "path": str(source_path.resolve()),
                        "sha256": source_sha256,
                        "size": source_path.stat().st_size,
                    },
                    "time_base": {"numerator": 1, "denominator": 1000, "text": "1/1000"},
                    "frame_pts_status": "vfr",
                    "frame_count": len(rows),
                    "tools": {
                        "ffmpeg": ffmpeg_info.as_dict(),
                    },
                    "pts_table_sha256": media_info._canonical_pts_table_sha256(rows),
                    "pts_table": rows,
                }
            ),
            encoding="utf-8",
        )
        return media_info.FramePtsCertification.from_evidence(
            evidence,
            status="vfr",
            source_sha256=source_sha256,
            time_base=Fraction(1, 1000),
        )

    def test_frame_ranges_map_to_exact_pts_and_duration_ticks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            certification = self._certification(
                Path(temporary),
                [
                    {"n": 0, "pts": -20, "duration": 20},
                    {"n": 1, "pts": 0, "duration": 30},
                    {"n": 2, "pts": 30, "duration": 50},
                    {"n": 3, "pts": 80, "duration": 20},
                ],
            )
            timeline = CertifiedPtsTimeline.from_certification(certification)

        first = timeline.interval_for_range((0, 1))
        middle = timeline.interval_for_range((1, 3))
        self.assertEqual((first.start_tick, first.end_tick), (-20, 0))
        self.assertEqual((middle.start_tick, middle.end_tick), (0, 80))
        self.assertEqual(middle.duration_time, Fraction(2, 25))

    def test_timeline_plan_maps_only_canonical_kept_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            certification = self._certification(
                Path(temporary),
                [
                    {"n": 0, "pts": 0, "duration": 10},
                    {"n": 1, "pts": 10, "duration": 15},
                    {"n": 2, "pts": 25, "duration": 20},
                    {"n": 3, "pts": 45, "duration": 25},
                    {"n": 4, "pts": 70, "duration": 30},
                ],
            )
            timeline = CertifiedPtsTimeline.from_certification(certification)
        plan = TimelinePlan.from_deleted_ranges(5, [(1, 3)])

        intervals = timeline.intervals_for_plan(plan)

        self.assertEqual(
            [
                (value.start_frame, value.end_frame, value.start_tick, value.end_tick)
                for value in intervals
            ],
            [(0, 1, 0, 10), (3, 5, 45, 100)],
        )

    def test_source_binding_and_frame_count_mismatch_are_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            certification = self._certification(
                Path(temporary),
                [
                    {"n": 0, "pts": 0, "duration": 10},
                    {"n": 1, "pts": 10, "duration": 10},
                ],
            )
            with self.assertRaises(media_info.MediaInfoError) as source_error:
                CertifiedPtsTimeline.from_certification(
                    certification,
                    expected_source_sha256="0" * 64,
                )
            timeline = CertifiedPtsTimeline.from_certification(certification)

        self.assertEqual(source_error.exception.code, "FRAME_PTS_SOURCE_MISMATCH")
        with self.assertRaises(media_info.MediaInfoError) as count_error:
            timeline.intervals_for_plan(TimelinePlan.from_deleted_ranges(3, []))
        self.assertEqual(count_error.exception.code, "FRAME_PTS_FRAME_COUNT_MISMATCH")

    def test_invalid_ranges_are_rejected_without_clamping_or_fps_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            certification = self._certification(
                Path(temporary),
                [
                    {"n": 0, "pts": 0, "duration": 10},
                    {"n": 1, "pts": 10, "duration": 10},
                ],
            )
            timeline = CertifiedPtsTimeline.from_certification(certification)

        for value in ((-1, 1), (0, 0), (1, 3), (False, 1), (0.0, 1)):
            with self.subTest(value=value):
                with self.assertRaises(media_info.MediaInfoError):
                    timeline.interval_for_range(value)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
