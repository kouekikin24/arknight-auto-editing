from __future__ import annotations

import hashlib
import json
from fractions import Fraction
from pathlib import Path
from queue import Queue
import tempfile
import unittest
from unittest import mock

import media_info
from certified_edl import build_certified_edl, edl_escape
from cv_engine import CvEngine
from preview_engine import (
    CertifiedEdlRequest,
    PreviewEngineError,
    PreviewPlayRequest,
    SourceSeekRequest,
)
from timeline_plan import TimelinePlan


def _certified_media(
    root: Path,
    rows: list[dict[str, int]],
    *,
    head_anomaly_limit: int = 0,
) -> media_info.MediaInfo:
    source = root / "源 sample.mp4"
    source.write_bytes(b"source")
    ffmpeg = root / "ffmpeg.exe"
    ffmpeg.write_bytes(b"ffmpeg")
    ffmpeg_info = media_info.ToolInfo(
        ffmpeg,
        media_info._sha256_file(ffmpeg),
        "ffmpeg version 7.1.1-test",
        True,
    )
    source_sha = media_info._sha256_file(source)
    evidence = root / "frame-pts.json"
    evidence.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "kind": "production_frame_pts_certification",
                "status": "PASS_WITH_HEAD_ANOMALIES"
                if head_anomaly_limit
                else "PASS",
                "authoritative_frame_timeline": True,
                "reason_codes": [],
                "scope": "full",
                "source": {
                    "path": str(source.resolve()),
                    "sha256": source_sha,
                    "size": source.stat().st_size,
                },
                "time_base": {"numerator": 1, "denominator": 1000, "text": "1/1000"},
                "frame_pts_status": "vfr",
                "frame_count": len(rows),
                "tools": {
                    "ffmpeg": ffmpeg_info.as_dict(),
                },
                "pts_table_sha256": media_info._canonical_pts_table_sha256(rows),
                "pts_table": rows,
                **(
                    {
                        "anomaly_adjudication": {
                            "head_frame_limit": head_anomaly_limit,
                        }
                    }
                    if head_anomaly_limit
                    else {}
                ),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    certification = media_info.FramePtsCertification.from_evidence(
        evidence,
        status="vfr",
        source_sha256=source_sha,
        time_base=Fraction(1, 1000),
    )
    stream = media_info.VideoStreamInfo(
        index=0,
        codec_name="h264",
        codec_long_name="H.264",
        width=16,
        height=16,
        pixel_format="yuv420p",
        time_base=Fraction(1, 1000),
        start_time=Fraction(0, 1),
        duration=Fraction(rows[-1]["pts"] + rows[-1]["duration"], 1000),
        avg_frame_rate=None,
        r_frame_rate=None,
        frame_count=len(rows),
        start_pts=rows[0]["pts"],
        duration_ts=rows[-1]["pts"] + rows[-1]["duration"],
    )
    media = media_info.MediaInfo(
        source_path=source,
        source_sha256=source_sha,
        source_size=source.stat().st_size,
        source_mtime_ns=source.stat().st_mtime_ns,
        format_name="mp4",
        format_long_name="MP4",
        duration=stream.duration,
        start_time=Fraction(0, 1),
        video_streams=(stream,),
        audio_streams=(),
        vfr_status="vfr",
        ffmpeg=ffmpeg_info,
    )
    return media.certify_frame_pts(certification)


class _FakeIO:
    def __init__(self, path, frame_queue):
        self.path = path
        self.frame_queue = frame_queue
        self.fps = 60.0
        self.total = 20
        self.commands = []
        self.closed = False

    def start(self):
        self.started = True

    def send(self, command):
        self.commands.append(command)
        return True

    def set_pace_mode(self, mode):
        self.pace_mode = mode

    def get_pace_mode(self):
        return getattr(self, "pace_mode", "opt")

    def snapshot_perf(self):
        return {"presented": 0}

    def reset_perf_stats(self):
        pass

    def begin_perf_segment(self):
        pass

    def is_playback_active(self):
        return False

    def is_alive(self):
        return not self.closed

    def close(self, timeout=1.0):
        self.closed = True
        return True


class PreviewEngineContractTests(unittest.TestCase):
    def test_cv_adapter_preserves_source_frame_commands(self):
        holder = {}

        def factory(path, queue):
            holder["io"] = _FakeIO(path, queue)
            return holder["io"]

        import queue

        engine = CvEngine("source.mp4", queue.Queue(), io_factory=factory)
        engine.start()
        engine.seek_source(SourceSeekRequest(3, (320, 180), 7, exact=True))
        plan = TimelinePlan.from_deleted_ranges(20, [(5, 8)])
        request = PreviewPlayRequest(
            start_frame=3,
            playback_rate=2.0,
            preview_step=2,
            speed_multiplier=1.0,
            skip_trimmed=True,
            speedup_1x=False,
            speedup_02=False,
            speedup_02_factor=1,
            timeline_plan=plan,
            speed_segments=(),
            canvas_size=(320, 180),
            project_generation=1,
            timeline_revision=7,
        )
        engine.play(request)
        engine.stop()

        commands = holder["io"].commands
        self.assertEqual(commands[0]["type"], "seek_latest")
        self.assertEqual(commands[0]["frame"], 3)
        self.assertEqual(commands[1]["type"], "play")
        self.assertEqual(commands[1]["params"]["pause_segs"], [(5, 8)])
        self.assertEqual(commands[-1]["type"], "stop")
        self.assertTrue(engine.close())

    def test_cv_adapter_keeps_edl_fallback_on_video_io_thread(self):
        holder = {}

        def factory(path, queue):
            holder["io"] = _FakeIO(path, queue)
            return holder["io"]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = _certified_media(
                root,
                [{"n": i, "pts": i * 40, "duration": 40} for i in range(6)],
            )
            engine = CvEngine(str(media.source_path), Queue(), io_factory=factory)
            engine.start()
            plan = TimelinePlan.from_deleted_ranges(6, [(2, 4)])
            self.assertTrue(
                engine.play_edl(
                    CertifiedEdlRequest(media, plan, 3, 5),
                    start_frame=4,
                    playback_rate=2.0,
                )
            )
            command = holder["io"].commands[-1]
            self.assertEqual(command["type"], "play")
            self.assertTrue(command["params"]["skip_trimmed"])
            self.assertEqual(command["params"]["pause_segs"], [(2, 4)])
            events = engine.poll_events()
            self.assertTrue(any(event["event"] == "play-request" for event in events))
            self.assertEqual(engine._mode, "edl")
            self.assertTrue(engine.close())

    def test_cv_edl_request_rejects_a_foreign_certified_source(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            media_a = _certified_media(
                Path(first),
                [{"n": i, "pts": i * 40, "duration": 40} for i in range(2)],
            )
            media_b = _certified_media(
                Path(second),
                [{"n": i, "pts": i * 40, "duration": 40} for i in range(2)],
            )
            engine = CvEngine(str(media_a.source_path), Queue(), io_factory=_FakeIO)
            with self.assertRaises(PreviewEngineError) as raised:
                engine.play_edl(
                    CertifiedEdlRequest(
                        media_b,
                        TimelinePlan.from_deleted_ranges(2, []),
                        0,
                        0,
                    ),
                    start_frame=0,
                )
            self.assertEqual(raised.exception.code, "SOURCE_MISMATCH")
            engine.close()

    def test_nonfinite_playback_rate_is_rejected(self):
        plan = TimelinePlan.from_deleted_ranges(2, [])
        with self.assertRaises(ValueError):
            PreviewPlayRequest(
                start_frame=0,
                playback_rate=float("nan"),
                preview_step=1,
                speed_multiplier=1.0,
                skip_trimmed=False,
                speedup_1x=False,
                speedup_02=False,
                speedup_02_factor=1,
                timeline_plan=plan,
                speed_segments=(),
                canvas_size=(1, 1),
                project_generation=0,
                timeline_revision=0,
            )

    def test_certified_edl_uses_tick_times_and_utf8_byte_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = _certified_media(
                root,
                [
                    {"n": 0, "pts": -20, "duration": 20},
                    {"n": 1, "pts": 0, "duration": 30},
                    {"n": 2, "pts": 30, "duration": 50},
                    {"n": 3, "pts": 80, "duration": 20},
                ],
            )
            plan = TimelinePlan.from_deleted_ranges(4, [(1, 3)])
            from preview_engine import CertifiedEdlRequest

            edl = build_certified_edl(
                CertifiedEdlRequest(media, plan, 1, 2),
                root / "edl",
            )
            text = edl.path.read_text(encoding="utf-8")
            self.assertIn(edl_escape(media.source_path), text)
            self.assertIn(",-0.02,0.02", text)
            self.assertIn(",0.08,0.02", text)
            self.assertNotIn("/fps", text)
            self.assertEqual(edl.segments[0].source_frame_range, (0, 1))
            self.assertEqual(edl.segments[1].source_frame_range, (3, 4))
            self.assertEqual(edl.virtual_time_for_source(3), Fraction(1, 50))
            with self.assertRaises(PreviewEngineError):
                edl.virtual_time_for_source(1)

    def test_certified_edl_sequence_contains_only_kept_source_frames(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = _certified_media(
                root,
                [{"n": i, "pts": i * 40, "duration": 40} for i in range(12)],
            )
            plan = TimelinePlan.from_deleted_ranges(12, [(4, 6), (8, 9)])
            edl = build_certified_edl(CertifiedEdlRequest(media, plan, 1, 1), root / "edl")
            played = [
                frame
                for start, end in (segment.source_frame_range for segment in edl.segments)
                for frame in range(start, end)
            ]
            expected = [0, 1, 2, 3, 6, 7, 9, 10, 11]
            self.assertEqual(played, expected)
            self.assertEqual(edl.expected_frames, len(expected))
            self.assertEqual(
                [edl.source_frame_for_virtual_time(segment.start_time) for segment in edl.segments],
                [0, 6, 9],
            )
            self.assertEqual(edl.preview_bias_frames, 0)

    def test_overlapping_tick_intervals_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = _certified_media(
                root,
                [
                    {"n": 0, "pts": 0, "duration": 10},
                    {"n": 1, "pts": 5, "duration": 10},
                    {"n": 2, "pts": 15, "duration": 10},
                ],
            )
            # The certification itself rejects a non-monotonic table; this
            # assertion documents the fail-closed boundary before file write.
            plan = TimelinePlan.from_deleted_ranges(3, [])
            from preview_engine import CertifiedEdlRequest
            from pts_timeline import PtsTickInterval

            overlapping = (
                PtsTickInterval(0, 1, 0, 10, Fraction(1, 1000)),
                PtsTickInterval(2, 3, 5, 15, Fraction(1, 1000)),
            )
            with mock.patch(
                "certified_edl.CertifiedPtsTimeline.intervals_for_plan",
                return_value=overlapping,
            ):
                with self.assertRaises(PreviewEngineError):
                    build_certified_edl(CertifiedEdlRequest(media, plan, 0, 0), root / "edl")

    def test_vfr_virtual_time_uses_floor_frame_ownership(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = _certified_media(
                root,
                [
                    {"n": 0, "pts": 0, "duration": 100},
                    {"n": 1, "pts": 100, "duration": 300},
                ],
            )
            edl = build_certified_edl(
                CertifiedEdlRequest(
                    media,
                    TimelinePlan.from_deleted_ranges(2, []),
                    0,
                    0,
                ),
                root / "edl",
            )
            self.assertEqual(edl.source_frame_for_virtual_time(Fraction(60, 1000)), 0)
            self.assertEqual(edl.source_frame_for_virtual_time(Fraction(100, 1000)), 1)

    def test_head_tick_collision_is_explicit_preview_bias(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = _certified_media(
                root,
                [
                    {"n": 0, "pts": 0, "duration": 40},
                    {"n": 1, "pts": 40, "duration": 40},
                    {"n": 2, "pts": 120, "duration": 40},
                    {"n": 3, "pts": 40, "duration": 40},
                    {"n": 4, "pts": 80, "duration": 40},
                    {"n": 5, "pts": 120, "duration": 40},
                    {"n": 6, "pts": 160, "duration": 40},
                    {"n": 7, "pts": 200, "duration": 40},
                ],
                head_anomaly_limit=32,
            )
            plan = TimelinePlan.from_deleted_ranges(8, [(5, 8)])
            edl = build_certified_edl(CertifiedEdlRequest(media, plan, 0, 0), root / "edl")
            self.assertEqual(edl.preview_tick_collision_frames, (2,))
            self.assertIn("frame(s) 2", edl.preview_bias_reason or "")
            self.assertIn("tick-collision-frames: 2", edl.path.read_text(encoding="utf-8"))
            with self.assertRaises(PreviewEngineError) as raised:
                edl.virtual_time_for_source(2)
            self.assertEqual(raised.exception.code, "SOURCE_FRAME_TICK_AMBIGUOUS")
            self.assertEqual(edl.virtual_time_for_source(2, snap=True), Fraction(3, 25))

    def test_multiple_head_tick_collisions_fail_before_publish(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = _certified_media(
                root,
                [
                    {"n": 0, "pts": 0, "duration": 40},
                    {"n": 1, "pts": 120, "duration": 40},
                    {"n": 2, "pts": 160, "duration": 40},
                    {"n": 3, "pts": 40, "duration": 40},
                    {"n": 4, "pts": 80, "duration": 40},
                ],
                head_anomaly_limit=32,
            )
            plan = TimelinePlan.from_deleted_ranges(5, [(4, 5)])
            with self.assertRaises(PreviewEngineError) as raised:
                build_certified_edl(CertifiedEdlRequest(media, plan, 0, 0), root / "edl")
            self.assertEqual(raised.exception.code, "FRAME_PTS_TICK_COLLISION_UNADJUDICATED")
            self.assertEqual(list((root / "edl").glob("*.edl")), [])

    def test_equal_edl_boundary_detects_deleted_duplicate_tick(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = _certified_media(
                root,
                [
                    {"n": 0, "pts": 0, "duration": 80},
                    {"n": 1, "pts": 80, "duration": 40},
                    {"n": 2, "pts": 80, "duration": 40},
                    {"n": 3, "pts": 120, "duration": 40},
                ],
                head_anomaly_limit=32,
            )
            plan = TimelinePlan.from_deleted_ranges(4, [(1, 2)])
            edl = build_certified_edl(CertifiedEdlRequest(media, plan, 0, 0), root / "edl")
            self.assertEqual(edl.preview_tick_collision_frames, (1,))
            self.assertIn("1", edl.path.read_text(encoding="utf-8"))


def _reference_head_tick_collision_frames(timeline, intervals):
    """Brute-force oracle for the linear merge in certified_edl.

    This deliberately keeps the original O(intervals x frames) nested scan so
    the production merge can be checked against it on every randomized case.
    """
    import certified_edl

    collisions = []
    kept = [False] * timeline.frame_count
    for interval in intervals:
        for frame in range(interval.start_frame, interval.end_frame):
            kept[frame] = True
            pts = timeline.rows[frame].pts
            if pts < interval.start_tick or pts >= interval.end_tick:
                collisions.append(frame)
    for interval in intervals:
        for frame, row in enumerate(timeline.rows):
            if not kept[frame] and interval.start_tick <= row.pts < interval.end_tick:
                collisions.append(frame)
    collisions = sorted(set(collisions))
    if not collisions:
        return ()
    limit = timeline.head_anomaly_limit
    if (
        limit <= 0
        or len(collisions) > certified_edl._PREVIEW_HEAD_TICK_COLLISION_MAX
        or any(frame >= limit for frame in collisions)
    ):
        raise PreviewEngineError(
            "FRAME_PTS_TICK_COLLISION_UNADJUDICATED",
            "reference policy gate",
        )
    return tuple(collisions)


class CertifiedEdlCollisionScanTests(unittest.TestCase):
    def test_merge_matches_brute_force_on_randomized_heads_and_plans(self):
        import random

        import certified_edl
        from pts_timeline import CertifiedPtsTimeline, FramePtsRow

        rng = random.Random(20260819)
        for _ in range(40):
            frame_count = rng.randint(20, 400)
            # A monotonic base table, then shuffle a small prefix to emulate
            # an adjudicated recording-head anomaly.
            pts_values = []
            tick = 0
            for _index in range(frame_count):
                tick += rng.randint(1, 5)
                pts_values.append(tick)
            head_limit = rng.choice((0, 0, 8, 16, 32))
            if head_limit and head_limit >= 2:
                # Model the real B' recording head: one trailing duplicate /
                # non-monotonic tick confined to the head window, after which
                # the table resumes strict increase.  A wide shuffle would
                # create end-before-start intervals that certification itself
                # rejects, so the anomaly stays small and tick-bounded.
                anomaly = rng.randint(1, head_limit - 1)
                pts_values[anomaly] = pts_values[anomaly - 1]
                if anomaly + 1 < head_limit and rng.random() < 0.5:
                    pts_values[anomaly + 1] = pts_values[anomaly]
            rows = tuple(
                FramePtsRow(n=index, pts=pts, duration=1)
                for index, pts in enumerate(pts_values)
            )
            timeline = CertifiedPtsTimeline(
                source_sha256="a" * 64,
                status="vfr",
                time_base=Fraction(1, 1000),
                pts_table_sha256="b" * 64,
                evidence_sha256="c" * 64,
                rows=rows,
                head_anomaly_limit=head_limit,
            )
            # Random half-open deleted ranges produce the kept intervals.
            deleted = []
            cursor = 0
            while cursor < frame_count and rng.random() < 0.7:
                start = rng.randint(cursor, frame_count - 1)
                end = rng.randint(start + 1, frame_count)
                deleted.append((start, end))
                cursor = end + rng.randint(0, 3)
            plan = TimelinePlan.from_deleted_ranges(frame_count, deleted)
            if not plan.kept_ranges:
                continue
            intervals = timeline.intervals_for_plan(plan)

            def merge_result():
                return certified_edl._head_tick_collision_frames(timeline, intervals)

            def reference_result():
                return _reference_head_tick_collision_frames(timeline, intervals)

            try:
                expected = reference_result()
            except PreviewEngineError:
                with self.assertRaises(PreviewEngineError):
                    merge_result()
            else:
                self.assertEqual(merge_result(), expected)

    def test_real_source_scale_collision_scan_stays_linear(self):
        import time

        import certified_edl
        from pts_timeline import CertifiedPtsTimeline, FramePtsRow

        # Sample-4-shaped load: thousands of kept ranges over hundreds of
        # thousands of frames.  The previous nested scan needed ~1.1e9 inner
        # iterations here; the merge must complete in well under a second.
        frame_count = 424_176
        kept_ranges = []
        cursor = 0
        while len(kept_ranges) < 2623:
            start = cursor + 7
            end = start + 113
            if end >= frame_count:
                break
            kept_ranges.append((start, end))
            cursor = end + 13
        kept_lookup = set()
        for start, end in kept_ranges:
            kept_lookup.update(range(start, end))
        rows = tuple(
            FramePtsRow(n=index, pts=(index + 1) * 2, duration=2)
            for index in range(frame_count)
        )
        timeline = CertifiedPtsTimeline(
            source_sha256="a" * 64,
            status="cfr",
            time_base=Fraction(1, 1000),
            pts_table_sha256="b" * 64,
            evidence_sha256="c" * 64,
            rows=rows,
        )
        plan = TimelinePlan.from_kept_ranges(frame_count, kept_ranges)
        intervals = timeline.intervals_for_plan(plan)
        started = time.perf_counter()
        result = certified_edl._head_tick_collision_frames(timeline, intervals)
        elapsed = time.perf_counter() - started
        self.assertEqual(result, ())
        self.assertLess(elapsed, 1.0, f"collision scan took {elapsed:.3f}s")


if __name__ == "__main__":
    unittest.main()
