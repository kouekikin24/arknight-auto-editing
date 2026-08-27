from __future__ import annotations

import json
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock

import media_info


def _probe_payload(*, vfr: bool = False, audio: bool = True, start: str = "0/1", pix_fmt: str = "yuv420p") -> dict:
    streams = [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "h264",
            "codec_long_name": "H.264",
            "width": 1920,
            "height": 1080,
            "pix_fmt": pix_fmt,
            "time_base": "1/15360",
            "start_time": start,
            "duration": "10/1",
            "avg_frame_rate": "30000/1001" if vfr else "60/1",
            "r_frame_rate": "60/1",
            "nb_frames": "600" if not vfr else "597",
        }
    ]
    if audio:
        streams.append(
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "aac",
                "codec_long_name": "AAC",
                "sample_rate": "48000",
                "channels": 2,
                "channel_layout": "stereo",
                "sample_fmt": "fltp",
                "time_base": "1/48000",
                "start_time": start,
                "duration": "10/1",
                "bit_rate": "128000",
            }
        )
    return {
        "streams": streams,
        "format": {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "format_long_name": "QuickTime / MOV",
            "start_time": start,
            "duration": "10/1",
        },
    }


def _test_ffmpeg_tool(root: Path) -> media_info.ToolInfo:
    ffmpeg_path = root / "ffmpeg.exe"
    if not ffmpeg_path.exists():
        ffmpeg_path.write_bytes(b"ffmpeg-tool")
    return media_info.ToolInfo(
        ffmpeg_path,
        media_info._sha256_file(ffmpeg_path),
        "ffmpeg version 7.1-essentials_build-www.gyan.dev",
        True,
    )


def _production_evidence(
    *,
    rows: list[dict[str, int]],
    source_path: Path,
    source_sha256: str,
    source_size: int,
    time_base: Fraction,
    ffmpeg: media_info.ToolInfo,
    status: str = "cfr",
    reason_codes: list[str] | None = None,
) -> dict:
    reasons = list(reason_codes or [])
    return {
        "schema_version": 2,
        "kind": "production_frame_pts_certification",
        "status": "PASS",
        "authoritative_frame_timeline": True,
        "reason_codes": reasons,
        "scope": "full",
        "source": {
            "path": str(source_path.resolve()),
            "sha256": source_sha256,
            "size": source_size,
        },
        "time_base": {
            "numerator": time_base.numerator,
            "denominator": time_base.denominator,
            "text": f"{time_base.numerator}/{time_base.denominator}",
        },
        "frame_pts_status": status,
        "frame_count": len(rows),
        "tools": {
            "ffmpeg": ffmpeg.as_dict(),
        },
        "pts_table_sha256": media_info._canonical_pts_table_sha256(rows),
        "pts_table": rows,
    }


class MediaInfoParsingTests(unittest.TestCase):
    def _parse(self, payload: dict) -> media_info.MediaInfo:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.mp4"
            path.write_bytes(b"fixture")
            result = media_info.parse_ffprobe_json(payload, path)
            # Keep the object alive only for assertions inside this helper.
            return result

    def _certification(
        self,
        result: media_info.MediaInfo,
        root: Path,
        *,
        status: str = "cfr",
    ) -> media_info.FramePtsCertification:
        frame_count = result.video_streams[0].frame_count or 1
        rows = [
            {"n": n, "pts": n * 100, "duration": 100}
            for n in range(frame_count)
        ]
        assert result.ffmpeg is not None
        time_base = result.video_streams[0].time_base
        assert time_base is not None
        evidence = root / "frame-pts.json"
        evidence.write_text(
            json.dumps(
                _production_evidence(
                    rows=rows,
                    source_path=result.source_path,
                    source_sha256=result.source_sha256,
                    source_size=result.source_size,
                    time_base=time_base,
                    ffmpeg=result.ffmpeg,
                    status=status,
                )
            ),
            encoding="utf-8",
        )
        return media_info.FramePtsCertification.from_evidence(
            evidence,
            status=status,
            source_sha256=result.source_sha256,
            time_base=result.video_streams[0].time_base,
        )

    def test_cfr_audio_and_fraction_fields(self) -> None:
        result = self._parse(_probe_payload())
        self.assertEqual(result.vfr_status, "rate_match")
        self.assertTrue(result.has_audio)
        self.assertEqual(result.duration, Fraction(10, 1))
        self.assertEqual(result.video_streams[0].time_base, Fraction(1, 15360))
        self.assertEqual(result.audio_streams[0].sample_rate, 48000)
        self.assertEqual(result.audio_streams[0].time_base, Fraction(1, 48000))
        self.assertEqual(result.validation_errors, ())
        self.assertFalse(result.complete_for_export)  # tool provenance is required for export.

    def test_vfr_is_detected_without_converting_to_float(self) -> None:
        result = self._parse(_probe_payload(vfr=True))
        self.assertEqual(result.vfr_status, "rate_mismatch")
        self.assertEqual(result.video_streams[0].avg_frame_rate, Fraction(30000, 1001))

    def test_nonzero_start_time_is_preserved(self) -> None:
        result = self._parse(_probe_payload(start="-5/2"))
        self.assertEqual(result.start_time, Fraction(-5, 2))
        self.assertEqual(result.video_streams[0].start_time, Fraction(-5, 2))

    def test_decimal_timestamp_strings_are_parsed_exactly(self) -> None:
        result = self._parse(_probe_payload(start="0.000000"))
        self.assertEqual(result.start_time, Fraction(0, 1))
        self.assertEqual(result.video_streams[0].start_time, Fraction(0, 1))
        self.assertEqual(result.audio_streams[0].start_time, Fraction(0, 1))

    def test_no_audio_is_explicit(self) -> None:
        result = self._parse(_probe_payload(audio=False))
        self.assertFalse(result.has_audio)
        self.assertEqual(result.audio_streams, ())
        self.assertEqual(result.validation_errors, ())

    def test_pixel_format_is_preserved(self) -> None:
        result = self._parse(_probe_payload(pix_fmt="yuv444p10le"))
        self.assertEqual(result.video_streams[0].pixel_format, "yuv444p10le")

    def test_multiple_audio_tracks_are_preserved_in_probe_order(self) -> None:
        payload = _probe_payload()
        second_audio = dict(payload["streams"][1])
        second_audio.update(
            {
                "index": 2,
                "codec_name": "flac",
                "codec_long_name": "FLAC",
                "sample_rate": "96000",
                "channels": 6,
                "channel_layout": "5.1",
                "sample_fmt": "s32",
                "time_base": "1/96000",
                "bit_rate": "512000",
            }
        )
        payload["streams"].append(second_audio)
        result = self._parse(payload)
        self.assertEqual([stream.index for stream in result.audio_streams], [1, 2])
        self.assertEqual(result.audio_streams[1].sample_rate, 96000)
        self.assertEqual(result.audio_streams[1].channel_layout, "5.1")
        self.assertEqual(result.audio_streams[1].sample_format, "s32")

    def test_missing_duration_is_structured_and_not_export_ready(self) -> None:
        payload = _probe_payload()
        del payload["format"]["duration"]
        result = self._parse(payload)
        self.assertIn("format.duration_missing", result.validation_errors)
        self.assertFalse(result.complete_for_export)

    def test_missing_required_stream_field_raises(self) -> None:
        payload = _probe_payload()
        del payload["streams"][0]["pix_fmt"]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.mp4"
            path.write_bytes(b"fixture")
            with self.assertRaises(media_info.MediaInfoError) as caught:
                media_info.parse_ffprobe_json(payload, path)
        self.assertEqual(caught.exception.code, "PROBE_FIELD_MISSING")
        self.assertEqual(caught.exception.details["field"], "video[0].pix_fmt")

    def test_malformed_json_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.mp4"
            path.write_bytes(b"fixture")
            with self.assertRaises(media_info.MediaInfoError) as caught:
                media_info.parse_ffprobe_json("{broken", path)
        self.assertEqual(caught.exception.code, "PROBE_JSON_INVALID")

    def test_raw_timestamp_ticks_are_preserved_and_float_ticks_are_rejected(self) -> None:
        payload = _probe_payload()
        payload["streams"][0].update({"start_pts": "120", "duration_ts": "153600"})
        payload["streams"][1].update({"start_pts": "480", "duration_ts": "480000"})
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.mp4"
            path.write_bytes(b"fixture")
            result = media_info.parse_ffprobe_json(payload, path)
        self.assertEqual(result.video_streams[0].start_pts, 120)
        self.assertEqual(result.video_streams[0].duration_ts, 153600)
        self.assertEqual(result.audio_streams[0].duration_ts, 480000)

        payload["streams"][0]["start_pts"] = 120.0
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.mp4"
            path.write_bytes(b"fixture")
            with self.assertRaises(media_info.MediaInfoError) as caught:
                media_info.parse_ffprobe_json(payload, path)
        self.assertEqual(caught.exception.code, "PROBE_FIELD_INVALID")

    def test_frame_pts_certification_is_required_for_export_readiness(self) -> None:
        payload = _probe_payload()
        for stream in payload["streams"]:
            stream.update({"start_pts": "0", "duration_ts": "480000"})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "sample.mp4"
            ffmpeg = root / "ffmpeg.exe"
            source.write_bytes(b"fixture")
            ffmpeg.write_bytes(b"ffmpeg-tool")
            result = media_info.parse_ffprobe_json(
                payload,
                source,
                ffmpeg=media_info.ToolInfo(
                    ffmpeg,
                    media_info._sha256_file(ffmpeg),
                    "ffmpeg version 7.1-essentials_build-www.gyan.dev",
                    True,
                ),
            )
            self.assertFalse(result.complete_for_export)
            certification = self._certification(result, root)
            certified = result.certify_frame_pts(certification)
            self.assertTrue(certified.complete_for_export)

    def test_incomplete_frame_pts_certification_is_rejected(self) -> None:
        payload = _probe_payload()
        for stream in payload["streams"]:
            stream.update({"start_pts": "0", "duration_ts": "480000"})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "sample.mp4"
            source.write_bytes(b"fixture")
            result = media_info.parse_ffprobe_json(payload, source)
            with self.assertRaisesRegex(
                media_info.MediaInfoError,
                "complete FramePtsCertification",
            ):
                result.certify_frame_pts("cfr")  # type: ignore[arg-type]

    def test_decoded_pts_count_overrides_nb_frames_hint(self) -> None:
        payload = _probe_payload(audio=False)
        payload["streams"][0]["nb_frames"] = "13"
        payload["streams"][0].update({"start_pts": "0", "duration_ts": "480000"})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "sample.mp4"
            ffmpeg = root / "ffmpeg.exe"
            evidence = root / "frame-pts.json"
            source.write_bytes(b"fixture")
            ffmpeg.write_bytes(b"ffmpeg-tool")
            result = media_info.parse_ffprobe_json(
                payload,
                source,
                ffmpeg=media_info.ToolInfo(
                    ffmpeg,
                    media_info._sha256_file(ffmpeg),
                    "ffmpeg version 7.1-essentials_build-www.gyan.dev",
                    True,
                ),
            )
            evidence.write_text(
                json.dumps(
                    _production_evidence(
                        rows=[
                            {"n": n, "pts": n * 100, "duration": 100}
                            for n in range(12)
                        ],
                        source_path=source,
                        source_sha256=result.source_sha256,
                        source_size=source.stat().st_size,
                        time_base=result.video_streams[0].time_base,
                        ffmpeg=result.ffmpeg,
                    )
                ),
                encoding="utf-8",
            )
            certification = media_info.FramePtsCertification.from_evidence(
                evidence,
                status="cfr",
                source_sha256=result.source_sha256,
                time_base=result.video_streams[0].time_base,
            )
            self.assertTrue(result.certify_frame_pts(certification).complete_for_export)

    def test_pts_certification_rejects_duplicate_or_non_monotonic_pts(self) -> None:
        for pts_values, expected_code in (
            ([0, 100, 100], "FRAME_PTS_EVIDENCE_NON_MONOTONIC"),
            ([0, 120, 100], "FRAME_PTS_EVIDENCE_NON_MONOTONIC"),
        ):
            with self.subTest(pts_values=pts_values):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    source = root / "sample.mp4"
                    source.write_bytes(b"fixture")
                    ffmpeg = _test_ffmpeg_tool(root)
                    evidence = root / "frame-pts.json"
                    rows = [
                        {"n": n, "pts": pts, "duration": 100}
                        for n, pts in enumerate(pts_values)
                    ]
                    evidence.write_text(
                        json.dumps(
                            _production_evidence(
                                rows=rows,
                                source_path=source,
                                source_sha256=media_info._sha256_file(source),
                                source_size=source.stat().st_size,
                                time_base=Fraction(1, 1000),
                                ffmpeg=ffmpeg,
                            )
                        ),
                        encoding="utf-8",
                    )
                    with self.assertRaises(media_info.MediaInfoError) as caught:
                        media_info.FramePtsCertification.from_evidence(
                            evidence,
                            status="cfr",
                            source_sha256=media_info._sha256_file(source),
                            time_base=Fraction(1, 1000),
                        )
                    self.assertEqual(caught.exception.code, expected_code)

    def test_pts_certification_rejects_blocked_reason_codes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "sample.mp4"
            source.write_bytes(b"fixture")
            ffmpeg = _test_ffmpeg_tool(root)
            rows = [
                {"n": 0, "pts": 0, "duration": 100},
                {"n": 1, "pts": 100, "duration": 100},
            ]
            evidence = root / "frame-pts.json"
            evidence.write_text(
                json.dumps(
                    _production_evidence(
                        rows=rows,
                        source_path=source,
                        source_sha256=media_info._sha256_file(source),
                        source_size=source.stat().st_size,
                        time_base=Fraction(1, 1000),
                        ffmpeg=ffmpeg,
                        reason_codes=["PTS_DUPLICATE"],
                    )
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                media_info.MediaInfoError,
                "blocking reasons",
            ):
                media_info.FramePtsCertification.from_evidence(
                    evidence,
                    status="cfr",
                    source_sha256="0" * 64,
                    time_base=Fraction(1, 1000),
                )

    def test_source_mutation_invalidates_export_readiness(self) -> None:
        payload = _probe_payload(audio=False)
        payload["streams"][0].update({"start_pts": "0", "duration_ts": "480000"})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "sample.mp4"
            ffmpeg = root / "ffmpeg.exe"
            source.write_bytes(b"fixture-before")
            ffmpeg.write_bytes(b"ffmpeg-tool")
            result = media_info.parse_ffprobe_json(
                payload,
                source,
                ffmpeg=media_info.ToolInfo(
                    ffmpeg,
                    media_info._sha256_file(ffmpeg),
                    "ffmpeg version 7.1-essentials_build-www.gyan.dev",
                    True,
                ),
            )
            certified = result.certify_frame_pts(self._certification(result, root))
            self.assertTrue(certified.complete_for_export)
            source.write_bytes(b"fixture-after")
            self.assertFalse(certified.complete_for_export)
            with self.assertRaisesRegex(
                media_info.MediaInfoError,
                "registered probe identity",
            ):
                certified.assert_source_current()

    def test_auto_resolver_prefers_repository_bundle(self) -> None:
        expected = (
            Path(__file__).resolve().parents[1]
            / "tools"
            / "ffmpeg-7.1.0"
            / "bundle"
            / "ffmpeg-7.1-essentials_build"
            / "bin"
            / "ffmpeg.exe"
        ).resolve()
        self.assertEqual(media_info.resolve_ffmpeg_path(), expected)

    def test_negative_start_pts_is_preserved(self) -> None:
        payload = _probe_payload(start="-5/2")
        payload["streams"][0].update({"start_pts": "-120", "duration_ts": "153600"})
        payload["streams"][1].update({"start_pts": "-480", "duration_ts": "480000"})
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.mp4"
            path.write_bytes(b"fixture")
            result = media_info.parse_ffprobe_json(payload, path)
        self.assertEqual(result.video_streams[0].start_pts, -120)
        self.assertEqual(result.audio_streams[0].start_pts, -480)

    def test_ffmpeg_tool_not_current_is_not_export_ready(self) -> None:
        payload = _probe_payload()
        for stream in payload["streams"]:
            stream.update({"start_pts": "0", "duration_ts": "480000"})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "sample.mp4"
            ffmpeg = root / "ffmpeg.exe"
            source.write_bytes(b"fixture")
            ffmpeg.write_bytes(b"ffmpeg-tool")
            # ToolInfo whose recorded sha256 no longer matches the file on disk
            # is "not current"; a stale ffmpeg must not be export-ready.
            stale_tool = media_info.ToolInfo(
                ffmpeg,
                "0" * 64,
                "ffmpeg version 7.1-essentials_build-www.gyan.dev",
                True,
            )
            result = media_info.parse_ffprobe_json(
                payload,
                source,
                ffmpeg=stale_tool,
            )
            self.assertFalse(result.ffmpeg.is_current())
            certification = self._certification(result, root)
            self.assertFalse(result.certify_frame_pts(certification).complete_for_export)


class MediaInfoProbeTests(unittest.TestCase):
    """The single PyAV metadata path. ffprobe.exe is retired: probe_media reads
    metadata via PyAV and binds only the ffmpeg oracle/export tool."""

    _FFMPEG_VERSION = "ffmpeg version 7.1-essentials_build-www.gyan.dev\n"

    def test_probe_binds_ffmpeg_and_spawns_only_version_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "sample.mp4"
            ffmpeg = root / "ffmpeg.exe"
            source.write_bytes(b"fixture")
            ffmpeg.write_bytes(b"ffmpeg-tool")
            with mock.patch.object(media_info, "subprocess") as subprocess_mock:
                subprocess_mock.run.return_value = mock.Mock(
                    returncode=0, stdout=self._FFMPEG_VERSION, stderr=""
                )
                with mock.patch.object(
                    media_info, "_pyav_probe_payload", return_value=_probe_payload()
                ) as payload:
                    result = media_info.probe_media(source, ffmpeg_path=ffmpeg)
            payload.assert_called_once_with(source.resolve())
            # Only the ffmpeg -version check spawns a subprocess; metadata is PyAV.
            self.assertEqual(subprocess_mock.run.call_count, 1)
            self.assertEqual(result.ffmpeg.path, ffmpeg.resolve())
            self.assertEqual(result.ffmpeg.sha256, media_info._sha256_file(ffmpeg))
            self.assertTrue(result.ffmpeg.verified)
            self.assertTrue(result.ffmpeg_verified)
            self.assertTrue(result.has_audio)
            self.assertEqual(result.video_streams[0].avg_frame_rate, Fraction(60, 1))
            self.assertFalse(result.complete_for_export)

    def test_probe_payload_failure_has_structured_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "sample.mp4"
            ffmpeg = root / "ffmpeg.exe"
            source.write_bytes(b"fixture")
            ffmpeg.write_bytes(b"ffmpeg-tool")
            with mock.patch.object(media_info, "subprocess") as subprocess_mock:
                subprocess_mock.run.return_value = mock.Mock(
                    returncode=0, stdout=self._FFMPEG_VERSION, stderr=""
                )
                with mock.patch.object(
                    media_info, "_pyav_probe_payload", side_effect=RuntimeError("boom")
                ):
                    with self.assertRaises(media_info.MediaInfoError) as caught:
                        media_info.probe_media(source, ffmpeg_path=ffmpeg)
            self.assertEqual(caught.exception.code, "PYAV_PROBE_FAILED")

    def test_source_mutation_during_probe_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "sample.mp4"
            ffmpeg = root / "ffmpeg.exe"
            source.write_bytes(b"fixture-before")
            ffmpeg.write_bytes(b"ffmpeg-tool")

            def _mutating_payload(_path):
                source.write_bytes(b"fixture-after")
                return _probe_payload()

            with mock.patch.object(media_info, "subprocess") as subprocess_mock:
                subprocess_mock.run.return_value = mock.Mock(
                    returncode=0, stdout=self._FFMPEG_VERSION, stderr=""
                )
                with mock.patch.object(
                    media_info, "_pyav_probe_payload", side_effect=_mutating_payload
                ):
                    with self.assertRaises(media_info.MediaInfoError) as caught:
                        media_info.probe_media(source, ffmpeg_path=ffmpeg)
            self.assertEqual(caught.exception.code, "SOURCE_CHANGED_DURING_PROBE")


class MediaInfoRoundingTests(unittest.TestCase):
    def test_round_to_microsecond_matches_probe_repr(self) -> None:
        # duration_ts*time_base exact fractions -> ffprobe %.6f microsecond grid.
        self.assertEqual(
            media_info._round_to_microsecond(Fraction(14903, 30)),
            Fraction(496766667, 1_000_000),
        )
        self.assertEqual(
            media_info._round_to_microsecond(Fraction(1151932, 375)),
            Fraction(3071818667, 1_000_000),
        )
        self.assertEqual(media_info._round_to_microsecond(Fraction(0, 1)), Fraction(0, 1))
        self.assertEqual(media_info._round_to_microsecond(Fraction(1, 100)), Fraction(1, 100))
        self.assertIsNone(media_info._round_to_microsecond(None))


if __name__ == "__main__":
    unittest.main()
