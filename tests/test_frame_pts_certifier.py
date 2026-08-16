from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
import tempfile
import unittest

import frame_pts_certifier
import media_info
from scripts import verify_mpv_frames
from task_manager import TaskCancelled


class FramePtsCertifierTests(unittest.TestCase):
    @staticmethod
    def _media(root: Path, *, frame_count: int = 4) -> media_info.MediaInfo:
        source = root / "source.mp4"
        ffmpeg_path = root / "ffmpeg.exe"
        ffprobe_path = root / "ffprobe.exe"
        source.write_bytes(b"source-media")
        ffmpeg_path.write_bytes(b"ffmpeg-tool")
        ffprobe_path.write_bytes(b"ffprobe-tool")
        ffmpeg = media_info.ToolInfo(
            ffmpeg_path,
            media_info._sha256_file(ffmpeg_path),
            "ffmpeg version 7.1-essentials_build-www.gyan.dev",
            True,
        )
        ffprobe = media_info.ToolInfo(
            ffprobe_path,
            media_info._sha256_file(ffprobe_path),
            "ffprobe version 7.1-essentials_build-www.gyan.dev",
            True,
        )
        video = media_info.VideoStreamInfo(
            index=0,
            codec_name="h264",
            codec_long_name="H.264",
            width=192,
            height=96,
            pixel_format="yuv420p",
            time_base=Fraction(1, 1000),
            start_time=Fraction(-1, 50),
            duration=Fraction(1, 1),
            avg_frame_rate=Fraction(25, 1),
            r_frame_rate=Fraction(25, 1),
            frame_count=frame_count,
            start_pts=-20,
            duration_ts=1000,
        )
        stat = source.stat()
        return media_info.MediaInfo(
            source_path=source.resolve(),
            source_sha256=media_info._sha256_file(source),
            source_size=stat.st_size,
            source_mtime_ns=stat.st_mtime_ns,
            format_name="mp4",
            format_long_name="MPEG-4",
            duration=Fraction(1, 1),
            start_time=Fraction(-1, 50),
            video_streams=(video,),
            audio_streams=(),
            vfr_status="rate_match",
            ffprobe=ffprobe,
            ffmpeg=ffmpeg,
        )

    @staticmethod
    def _report(
        media: media_info.MediaInfo,
        rows: list[dict[str, int]],
        *,
        status: str = "PASS",
        reason_codes: list[str] | None = None,
        ffmpeg_returncode: int = 0,
    ) -> dict:
        assert media.ffmpeg is not None
        return {
            "schema_version": 1,
            "kind": "mpv_phase0_frame_pts_oracle",
            "status": status,
            "authoritative_frame_timeline": status == "PASS",
            "reason_codes": list(reason_codes or []),
            "video": {
                "path": str(media.source_path),
                "sha256": media.source_sha256,
                "size": media.source_size,
                "mtime_ns": media.source_mtime_ns,
            },
            "ffmpeg": {
                "path": str(media.ffmpeg.path),
                "sha256": media.ffmpeg.sha256,
                "version_line": media.ffmpeg.version_line,
                "verified": True,
                "resolution_source": "media_info",
                "command": verify_mpv_frames.build_ffmpeg_command(
                    media.ffmpeg.path,
                    media.source_path,
                    threads=1,
                ),
                "returncode": ffmpeg_returncode,
            },
            "showinfo": {
                "parsed_frames": len(rows),
                "time_base": {
                    "numerator": 1,
                    "denominator": 1000,
                    "text": "1/1000",
                },
            },
            "pts_conflict_assessment": {"scan_scope": "complete"},
            "pts_table": rows,
        }

    def test_pass_is_write_once_cached_and_classified_from_ticks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = self._media(root)
            evidence = root / "evidence.json"
            rows = [
                {"n": 0, "pts": -20, "duration": 20},
                {"n": 1, "pts": 0, "duration": 30},
                {"n": 2, "pts": 30, "duration": 50},
                {"n": 3, "pts": 80, "duration": 20},
            ]
            calls = []

            def probe(source, ffmpeg, *, checkpoint):
                calls.append((source, ffmpeg))
                checkpoint()
                return self._report(media, rows), 0

            checkpoints = []
            outcome = frame_pts_certifier.produce_frame_pts_certification(
                media,
                evidence_path=evidence,
                checkpoint=lambda: checkpoints.append(True),
                oracle_probe=probe,
            )
            self.assertTrue(outcome.certified)
            self.assertTrue(outcome.generated)
            self.assertEqual(outcome.media_info.frame_pts_certification.status, "vfr")
            self.assertEqual(calls, [(media.source_path, media.ffmpeg.path)])
            self.assertGreaterEqual(len(checkpoints), 3)

            cached = frame_pts_certifier.produce_frame_pts_certification(
                media,
                evidence_path=evidence,
                oracle_probe=lambda *_args, **_kwargs: self.fail("cache reran oracle"),
            )
            self.assertTrue(cached.certified)
            self.assertFalse(cached.generated)

    def test_duplicate_pts_returns_blocked_without_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = self._media(root, frame_count=34)
            evidence = root / "blocked.json"
            rows = [
                {"n": n, "pts": n * 20, "duration": 20} for n in range(33)
            ]
            # Duplicate beyond the adjudicable head window must stay BLOCKED.
            rows.append({"n": 33, "pts": 32 * 20, "duration": 20})

            outcome = frame_pts_certifier.produce_frame_pts_certification(
                media,
                evidence_path=evidence,
                oracle_probe=lambda *_args, **_kwargs: (
                    self._report(
                        media,
                        rows,
                        status="BLOCKED",
                        reason_codes=["PTS_DUPLICATE"],
                    ),
                    10,
                ),
            )

            self.assertEqual(outcome.status, "BLOCKED")
            self.assertFalse(outcome.media_info.complete_for_export)
            self.assertIn("PTS_DUPLICATE", outcome.reason_codes)
            self.assertIn("FRAME_PTS_EVIDENCE_NON_MONOTONIC", outcome.reason_codes)
            payload = json.loads(evidence.read_text(encoding="utf-8"))
            self.assertEqual(payload["pts_table"], rows)
            self.assertIsNone(payload["pts_table_sha256"])

    def test_blocked_evidence_requires_explicit_retry_and_preserves_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = self._media(root, frame_count=34)
            evidence = root / "blocked.json"
            blocked_rows = [
                {"n": n, "pts": n * 20, "duration": 20} for n in range(33)
            ]
            # Duplicate beyond the adjudicable head window stays BLOCKED.
            blocked_rows.append({"n": 33, "pts": 32 * 20, "duration": 20})
            pass_rows = [
                {"n": n, "pts": n * 20, "duration": 20}
                for n in range(34)
            ]
            calls: list[str] = []

            def blocked_probe(*_args, **_kwargs):
                calls.append("blocked")
                return (
                    self._report(
                        media,
                        blocked_rows,
                        status="BLOCKED",
                        reason_codes=["PTS_DUPLICATE"],
                    ),
                    10,
                )

            first = frame_pts_certifier.produce_frame_pts_certification(
                media,
                evidence_path=evidence,
                oracle_probe=blocked_probe,
            )
            self.assertEqual(first.status, "BLOCKED")
            self.assertEqual(first.evidence_path, evidence.resolve())
            old_payload = json.loads(evidence.read_text(encoding="utf-8"))
            old_sha256 = media_info._sha256_file(evidence)

            cached = frame_pts_certifier.produce_frame_pts_certification(
                media,
                evidence_path=evidence,
                oracle_probe=lambda *_args, **_kwargs: self.fail(
                    "default cache path reran a blocked oracle"
                ),
            )
            self.assertEqual(cached.status, "BLOCKED")
            self.assertFalse(cached.generated)
            self.assertEqual(calls, ["blocked"])

            def retry_probe(*_args, **_kwargs):
                calls.append("retry")
                return self._report(media, pass_rows), 0

            retried = frame_pts_certifier.produce_frame_pts_certification(
                media,
                evidence_path=evidence,
                retry_blocked=True,
                oracle_probe=retry_probe,
            )
            self.assertTrue(retried.certified)
            self.assertTrue(retried.generated)
            self.assertNotEqual(retried.evidence_path, evidence.resolve())
            self.assertTrue(retried.evidence_path.name.startswith("blocked.retry-"))
            self.assertEqual(calls, ["blocked", "retry"])
            self.assertEqual(media_info._sha256_file(evidence), old_sha256)
            self.assertEqual(
                json.loads(evidence.read_text(encoding="utf-8")),
                old_payload,
            )

            retry_payload = json.loads(
                retried.evidence_path.read_text(encoding="utf-8")
            )
            self.assertEqual(retry_payload["status"], "PASS")
            self.assertEqual(
                retry_payload["retry"]["previous_evidence_path"],
                str(evidence.resolve()),
            )
            self.assertEqual(
                retry_payload["retry"]["previous_evidence_sha256"],
                old_sha256,
            )

    def test_source_change_during_probe_is_rejected_without_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = self._media(root)
            evidence = root / "evidence.json"
            rows = [
                {"n": n, "pts": n * 20, "duration": 20}
                for n in range(4)
            ]

            def probe(*_args, **_kwargs):
                report = self._report(media, rows)
                media.source_path.write_bytes(b"changed-source")
                return report, 0

            with self.assertRaises(media_info.MediaInfoError) as caught:
                frame_pts_certifier.produce_frame_pts_certification(
                    media,
                    evidence_path=evidence,
                    oracle_probe=probe,
                )
            self.assertEqual(caught.exception.code, "SOURCE_CHANGED_DURING_FRAME_PTS_PROBE")
            self.assertFalse(evidence.exists())

    def test_tool_change_invalidates_certified_media(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = self._media(root)
            rows = [
                {"n": n, "pts": n * 20, "duration": 20}
                for n in range(4)
            ]
            outcome = frame_pts_certifier.produce_frame_pts_certification(
                media,
                evidence_path=root / "evidence.json",
                oracle_probe=lambda *_args, **_kwargs: (self._report(media, rows), 0),
            )
            self.assertTrue(outcome.media_info.complete_for_export)
            assert media.ffmpeg is not None
            media.ffmpeg.path.write_bytes(b"changed-tool")
            self.assertFalse(outcome.media_info.complete_for_export)

    def test_cancellation_does_not_publish_partial_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = self._media(root)
            evidence = root / "evidence.json"
            calls = 0

            def checkpoint() -> None:
                nonlocal calls
                calls += 1
                if calls >= 2:
                    raise TaskCancelled("cancelled")

            def probe(_source, _ffmpeg, *, checkpoint):
                checkpoint()
                self.fail("cancelled checkpoint returned")

            with self.assertRaises(TaskCancelled):
                frame_pts_certifier.produce_frame_pts_certification(
                    media,
                    evidence_path=evidence,
                    checkpoint=checkpoint,
                    oracle_probe=probe,
                )
            self.assertFalse(evidence.exists())

    # B' targeted adjudication (2026-08-16 ruling) -------------------------------

    HEAD_ROWS = [
        # Recording-start artifact shape (mirrors the four real samples):
        # duplicate ticks and one backward step, all confined to the head.
        {"n": 0, "pts": 0, "duration": 40},
        {"n": 1, "pts": 40, "duration": 40},
        {"n": 2, "pts": 120, "duration": 40},
        {"n": 3, "pts": 40, "duration": 40},
        {"n": 4, "pts": 80, "duration": 40},
        {"n": 5, "pts": 120, "duration": 40},
        {"n": 6, "pts": 160, "duration": 40},
        {"n": 7, "pts": 200, "duration": 40},
    ]

    def test_head_confined_pts_anomaly_adjudicates_as_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = self._media(root, frame_count=8)
            evidence = root / "adjudicated.json"

            outcome = frame_pts_certifier.produce_frame_pts_certification(
                media,
                evidence_path=evidence,
                oracle_probe=lambda *_args, **_kwargs: (
                    self._report(
                        media,
                        self.HEAD_ROWS,
                        status="BLOCKED",
                        reason_codes=["PTS_DUPLICATE", "PTS_NON_MONOTONIC"],
                    ),
                    10,
                ),
            )

            self.assertEqual(
                outcome.status, media_info.ADJUDICATED_EVIDENCE_STATUS
            )
            self.assertTrue(outcome.certified)
            self.assertTrue(outcome.media_info.complete_for_export)
            self.assertEqual(outcome.reason_codes, ())
            payload = json.loads(evidence.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["status"], media_info.ADJUDICATED_EVIDENCE_STATUS
            )
            self.assertTrue(payload["authoritative_frame_timeline"])
            self.assertEqual(payload["reason_codes"], [])
            adjudication = payload["anomaly_adjudication"]
            self.assertEqual(
                adjudication["head_frame_limit"],
                media_info.HEAD_ANOMALY_FRAME_LIMIT,
            )
            self.assertIn(3, adjudication["facts"]["involved_frames"])
            self.assertEqual(
                payload["pts_table_sha256"],
                media_info._canonical_pts_table_sha256(self.HEAD_ROWS),
            )

            cached = frame_pts_certifier.produce_frame_pts_certification(
                media,
                evidence_path=evidence,
                oracle_probe=lambda *_args, **_kwargs: self.fail(
                    "cache reran oracle"
                ),
            )
            self.assertEqual(
                cached.status, media_info.ADJUDICATED_EVIDENCE_STATUS
            )
            self.assertTrue(cached.certified)

            from pts_timeline import CertifiedPtsTimeline

            timeline = CertifiedPtsTimeline.from_media_info(cached.media_info)
            self.assertEqual(
                timeline.head_anomaly_limit,
                media_info.HEAD_ANOMALY_FRAME_LIMIT,
            )
            interval = timeline.interval_for_range((0, 5))
            self.assertEqual((interval.start_tick, interval.end_tick), (0, 120))

    def test_head_anomaly_with_decode_failure_stays_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = self._media(root, frame_count=8)
            evidence = root / "blocked.json"

            outcome = frame_pts_certifier.produce_frame_pts_certification(
                media,
                evidence_path=evidence,
                oracle_probe=lambda *_args, **_kwargs: (
                    self._report(
                        media,
                        self.HEAD_ROWS,
                        status="BLOCKED",
                        reason_codes=["PTS_DUPLICATE", "PTS_NON_MONOTONIC"],
                        ffmpeg_returncode=1,
                    ),
                    10,
                ),
            )

            self.assertEqual(outcome.status, "BLOCKED")
            self.assertFalse(outcome.media_info.complete_for_export)
            self.assertIn("FFMPEG_DECODE_FAILED", outcome.reason_codes)
            self.assertIn("PTS_DUPLICATE", outcome.reason_codes)

    def test_decode_threads_parameter_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = self._media(root)
            with self.assertRaises(media_info.MediaInfoError) as ctx:
                frame_pts_certifier.produce_frame_pts_certification(
                    media,
                    evidence_path=root / "never.json",
                    decode_threads=0,
                )
            self.assertEqual(ctx.exception.code, "FRAME_PTS_THREADS_INVALID")


if __name__ == "__main__":
    unittest.main()
