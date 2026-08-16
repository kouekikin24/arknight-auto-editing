from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "scripts" / "prefix_audio_copy.py"
SPEC = importlib.util.spec_from_file_location("prefix_audio_copy", MODULE_PATH)
assert SPEC and SPEC.loader
prefix = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prefix)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audio_rows(count: int, *, pts: list[int] | None = None) -> list[dict]:
    pts = pts or [index * 2 for index in range(count)]
    return [
        {
            "n": index,
            "pts": value,
            "pts_time": value / 100,
            "nb_samples": 2,
            "rate": 100,
            "channels": 2,
            "chlayout": "stereo",
            "checksum": f"{index + 1:08X}",
        }
        for index, value in enumerate(pts)
    ]


def audio_report(rows: list[dict], *, status: str = "PASS", reason_codes: list[str] | None = None) -> dict:
    reason_codes = reason_codes or []
    first = rows[0] if rows else None
    last = rows[-1] if rows else None
    end_pts = last["pts"] + last["nb_samples"] if last else None
    return {
        "status": status,
        "reason_codes": reason_codes,
        "command": ["ffmpeg", "-i", "test-input"],
        "stream_present": bool(rows),
        "codec": "aac",
        "format": {
            "sample_rate": 100,
            "channels": 2,
            "channel_layout": "stereo",
        },
        "frames": {
            "count": len(rows),
            "pts_table": rows,
            "first": first,
            "last": last,
            "total_samples": len(rows) * 2,
            "start_pts": first["pts"] if first else None,
            "end_pts_exclusive": end_pts,
            "start_time": first["pts_time"] if first else None,
            "end_time": end_pts / 100 if end_pts is not None else None,
        },
        "continuity": {
            "status": "PASS" if rows else "BLOCKED",
            "error_count": 0,
            "examples": [],
        },
        "format_validation": {
            "status": "PASS" if rows else "BLOCKED",
            "error_count": 0,
            "examples": [],
        },
    }


class AudioPrefixCompareTests(unittest.TestCase):
    def test_matching_shorter_prefix_preserves_pts_and_pcm_checksums(self) -> None:
        source = audio_report(audio_rows(4))
        proxy = audio_report(audio_rows(3))

        result = prefix._audio_prefix_compare(source, proxy)

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["source_frames"], 4)
        self.assertEqual(result["proxy_frames"], 3)

    def test_checksum_or_pts_mismatch_blocks_prefix(self) -> None:
        source = audio_report(audio_rows(4))
        proxy_rows = audio_rows(3)
        proxy_rows[1]["checksum"] = "DEADBEEF"
        proxy = audio_report(proxy_rows)

        result = prefix._audio_prefix_compare(source, proxy)

        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("AUDIO_PREFIX_PCM_OR_PTS_MISMATCH", result["reason_codes"])

    def test_full_length_audio_is_not_accepted_as_prefix(self) -> None:
        rows = audio_rows(3)

        result = prefix._audio_prefix_compare(audio_report(rows), audio_report(rows))

        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("AUDIO_PREFIX_SCOPE_INVALID", result["reason_codes"])


class PrefixManifestAndScopeTests(unittest.TestCase):
    PNG_CAPTURE = b"\x89PNG\r\n\x1a\nTEST-PNG"
    WAV_CAPTURE = b"RIFF\x10\x00\x00\x00WAVETEST-WAV"

    def _reextraction_patch(self):
        def reproduce(_command: list[str], output: Path, _name: str) -> None:
            output.write_bytes(
                self.PNG_CAPTURE
                if output.suffix.lower() == ".png"
                else self.WAV_CAPTURE
            )

        return mock.patch.object(
            prefix.audio_verifier,
            "_run_content_anchor_reextraction",
            side_effect=reproduce,
        )

    def _fixture(self, temp: Path, *, source_pts: list[int] | None = None) -> dict:
        source = temp / "source.mp4"
        video = temp / "video-only.mp4"
        audio_proxy = temp / "audio-proxy.mp4"
        ffmpeg = temp / "ffmpeg.exe"
        source.write_bytes(b"source")
        video.write_bytes(b"video")
        audio_proxy.write_bytes(b"audio-proxy")
        ffmpeg.write_bytes(b"ffmpeg")

        rows = [
            {
                "n": index,
                "pts": value,
                "pts_time": value / 100,
                "duration": 2,
                "duration_time": 0.02,
                "checksum": f"{index + 1:08X}",
            }
            for index, value in enumerate(source_pts or [0, 2, 4, 6])
        ]
        oracle = {
            "schema_version": prefix.frame_oracle.SCHEMA_VERSION,
            "kind": "mpv_phase0_frame_pts_oracle",
            "status": "BLOCKED",
            "reason_codes": ["PTS_DUPLICATE", "PTS_NON_MONOTONIC"],
            "video": {
                "path": str(source),
                "sha256": sha256(source),
                "size": source.stat().st_size,
            },
            "showinfo": {
                "time_base": {"numerator": 1, "denominator": 100},
                "pixel_formats": ["yuv420p"],
                "parsed_frames": len(rows),
            },
            "ffmpeg": {"returncode": 0},
            "pts_table": rows,
        }
        oracle_path = temp / "source.oracle.json"
        oracle_path.write_text(json.dumps(oracle), encoding="utf-8")
        oracle_record = {"path": str(oracle_path), "sha256": sha256(oracle_path)}

        video_manifest = {
            "schema_version": prefix.proxy_verifier.SCHEMA_VERSION,
            "kind": prefix.proxy_verifier.MANIFEST_KIND,
            "source": {
                "path": str(source),
                "sha256": sha256(source),
                "size": source.stat().st_size,
                "scope": "prefix",
                "business_frame_count": len(rows),
                "oracle": oracle_record,
            },
            "proxy": {
                "path": str(video),
                "sha256": sha256(video),
                "size": video.stat().st_size,
            },
            "normalized_timeline": {
                "time_base": {"numerator": 1, "denominator": 100},
                "duration_ticks": 2,
                "business_pts_start": 0,
                "business_pts_end_exclusive": len(rows) * 2,
            },
            "terminal_guard": {
                "method": "clone_last_business_frame",
                "proxy_frame_index": len(rows),
                "pts_ticks": len(rows) * 2,
                "expected_checksum": rows[-1]["checksum"],
                "business_frame_domain": [0, len(rows)],
                "included_in_business_domain": False,
            },
        }
        video_manifest_path = temp / "video.manifest.json"
        video_manifest_path.write_text(json.dumps(video_manifest), encoding="utf-8")

        manifest = {
            "schema_version": prefix.SCHEMA_VERSION,
            "kind": prefix.MANIFEST_KIND,
            "scope": "prefix",
            "source": {
                "path": str(source),
                "sha256": sha256(source),
                "size": source.stat().st_size,
                "scope": "prefix",
                "business_frame_count": len(rows),
                "oracle": oracle_record,
            },
            "proxy": {
                "path": str(audio_proxy),
                "sha256": sha256(audio_proxy),
                "size": audio_proxy.stat().st_size,
            },
            "video_manifest": {
                "path": str(video_manifest_path),
                "sha256": sha256(video_manifest_path),
            },
            "video_proxy": video_manifest["proxy"],
            "normalized_timeline": video_manifest["normalized_timeline"],
        }
        mux_window = prefix._mux_window(video_manifest["normalized_timeline"])
        temporary_output = temp / ".audio-proxy.0123456789abcdef.tmp.mp4"
        manifest["audio_route"] = {
            "name": "packet_copy",
            "source_stream": "1:a:0",
            "codec_copy": True,
            "command": prefix._build_mux_command(
                ffmpeg,
                video,
                source,
                temporary_output,
                mux_end_exclusive_seconds=mux_window["mux_end_exclusive_seconds"],
            ),
            **mux_window,
        }
        manifest["tools"] = {
            "ffmpeg": {"path": str(ffmpeg), "sha256": sha256(ffmpeg)},
            "prefix_audio_copy": {
                "path": str(Path(prefix.__file__).resolve()),
                "sha256": sha256(Path(prefix.__file__).resolve()),
            },
        }
        manifest_path = temp / "prefix.manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return {
            "manifest": manifest,
            "manifest_path": manifest_path,
            "video_manifest": video_manifest,
            "video_manifest_path": video_manifest_path,
            "source": source,
            "video": video,
            "audio_proxy": audio_proxy,
            "ffmpeg": ffmpeg,
            "source_audio": audio_report(audio_rows(4)),
            "proxy_audio": audio_report(audio_rows(3)),
            "oracle": oracle,
        }

    def _verify(self, fixture: dict, *, oracle_exit: int = 0) -> dict:
        oracle_output = fixture["manifest_path"].with_name("proxy.oracle.json")
        report_output = fixture["manifest_path"].with_name("verify.json")
        video_validation = {"status": "PASS"}
        with (
            mock.patch.object(
                prefix.frame_oracle,
                "probe_video",
                return_value=({}, oracle_exit),
            ),
            mock.patch.object(
                prefix.proxy_verifier,
                "evaluate_video_proxy",
                return_value=video_validation,
            ),
            mock.patch.object(
                prefix.audio_verifier,
                "decode_audio",
                side_effect=[fixture["source_audio"], fixture["proxy_audio"]],
            ),
        ):
            return prefix.verify_prefix(
                fixture["manifest_path"],
                oracle_output,
                report_output,
                fixture["ffmpeg"],
            )

    def _bound_content_anchor_manifest(self, fixture: dict) -> tuple[Path, Path]:
        source_rows = fixture["oracle"]["pts_table"]
        proxy_rows = [
            {
                "n": index,
                "pts": index * 2,
                "pts_time": index * 0.02,
                "duration": 2,
                "duration_time": 0.02,
                "checksum": row["checksum"],
            }
            for index, row in enumerate(source_rows)
        ]
        proxy_rows.append(
            {
                "n": len(source_rows),
                "pts": len(source_rows) * 2,
                "pts_time": len(source_rows) * 0.02,
                "duration": 0,
                "duration_time": 0.0,
                "checksum": source_rows[-1]["checksum"],
            }
        )
        proxy_oracle = {
            "schema_version": prefix.frame_oracle.SCHEMA_VERSION,
            "kind": "mpv_phase0_frame_pts_oracle",
            "status": "PASS",
            "authoritative_frame_timeline": True,
            "reason_codes": [],
            "video": {
                "path": str(fixture["audio_proxy"].resolve()),
                "sha256": sha256(fixture["audio_proxy"]),
                "size": fixture["audio_proxy"].stat().st_size,
            },
            "showinfo": {
                "time_base": {"numerator": 1, "denominator": 100},
                "pixel_formats": ["yuv420p"],
                "parsed_frames": len(proxy_rows),
            },
            "ffmpeg": {"returncode": 0},
            "pts_table": proxy_rows,
        }
        proxy_oracle_path = fixture["manifest_path"].with_name(
            "existing.proxy.oracle.json"
        )
        proxy_oracle_path.write_text(json.dumps(proxy_oracle), encoding="utf-8")

        zone_indices = {
            "start": 0,
            "pts_conflict": 2,
            "middle": 1,
            "end": 3,
        }
        anchors = []
        for zone, index in zone_indices.items():
            sample = min(index * 2, 5)
            windows = {}
            for side, media, oracle_path, evidence in (
                (
                    "source",
                    fixture["source"],
                    Path(fixture["manifest"]["source"]["oracle"]["path"]),
                    fixture["source_audio"],
                ),
                (
                    "proxy",
                    fixture["audio_proxy"],
                    proxy_oracle_path,
                    fixture["proxy_audio"],
                ),
            ):
                visual = fixture["manifest_path"].with_name(f"{zone}.{side}.png")
                audio_window = fixture["manifest_path"].with_name(
                    f"{zone}.{side}.wav"
                )
                captured_visual = visual.with_name(
                    f".{visual.stem}.{'0' * 32}.capture{visual.suffix}"
                )
                captured_audio = audio_window.with_name(
                    f".{audio_window.stem}.{'1' * 32}.capture{audio_window.suffix}"
                )
                visual.write_bytes(self.PNG_CAPTURE)
                audio_window.write_bytes(self.WAV_CAPTURE)
                decoded_start = evidence["frames"]["start_pts"]
                decoded_end = evidence["frames"]["end_pts_exclusive"]
                start_sample = max(decoded_start, sample - 1)
                end_sample = min(decoded_end, sample + 2)
                audio_frame = next(
                    row
                    for row in evidence["frames"]["pts_table"]
                    if row["pts"] <= sample < row["pts"] + row["nb_samples"]
                )
                window = {
                    "schema_version": prefix.audio_verifier.CONTENT_ANCHOR_SCHEMA_VERSION,
                    "kind": prefix.audio_verifier.CONTENT_ANCHOR_WINDOW_KIND,
                    "side": side,
                    "scope": "prefix",
                    "media": {
                        "path": str(media.resolve()),
                        "sha256": sha256(media),
                        "size": media.stat().st_size,
                    },
                    "oracle": {
                        "path": str(oracle_path.resolve()),
                        "sha256": sha256(oracle_path),
                    },
                    "video_frame": {
                        "index": index,
                        "checksum": source_rows[index]["checksum"],
                    },
                    "audio_frame": {
                        field: audio_frame[field]
                        for field in ("n", "pts", "nb_samples", "checksum")
                    },
                    "audio_window": {
                        "stream": "0:a:0",
                        "sample_rate": 100,
                        "anchor_sample": sample,
                        "start_sample": start_sample,
                        "end_sample_exclusive": end_sample,
                    },
                    "visual_artifact": {
                        "path": str(visual.resolve()),
                        "sha256": sha256(visual),
                        "size": visual.stat().st_size,
                        "media_type": "image/png",
                    },
                    "audio_artifact": {
                        "path": str(audio_window.resolve()),
                        "sha256": sha256(audio_window),
                        "size": audio_window.stat().st_size,
                        "media_type": "audio/wav",
                    },
                    "capture": {
                        "tool": {
                            "path": str(
                                prefix.audio_verifier.CONTENT_ANCHOR_CAPTURE_TOOL.resolve()
                            ),
                            "sha256": sha256(
                                prefix.audio_verifier.CONTENT_ANCHOR_CAPTURE_TOOL
                            ),
                        },
                        "ffmpeg": {
                            "path": str(fixture["ffmpeg"].resolve()),
                            "sha256": sha256(fixture["ffmpeg"]),
                        },
                        "commands": {
                            "video": prefix.audio_verifier.content_anchor_video_capture_command(
                                fixture["ffmpeg"], media, index, captured_visual
                            ),
                            "audio": prefix.audio_verifier.content_anchor_audio_capture_command(
                                fixture["ffmpeg"],
                                media,
                                decoded_start_sample=start_sample - decoded_start,
                                decoded_end_sample_exclusive=end_sample - decoded_start,
                                output=captured_audio,
                            ),
                        },
                        "publication": {
                            "method": "hardlink_create_new",
                            "visual_target": str(visual.resolve()),
                            "audio_target": str(audio_window.resolve()),
                        },
                    },
                }
                window_path = fixture["manifest_path"].with_name(
                    f"{zone}.{side}.window.json"
                )
                window_path.write_text(json.dumps(window), encoding="utf-8")
                windows[side] = {
                    "path": str(window_path.resolve()),
                    "sha256": sha256(window_path),
                }
            anchors.append(
                {
                    "zone": zone,
                    "evidence_id": f"prefix-{zone}",
                    "source_frame_index": index,
                    "proxy_frame_index": index,
                    "source_audio_sample": sample,
                    "proxy_audio_sample": sample,
                    "source_frame_checksum": source_rows[index]["checksum"],
                    "proxy_frame_checksum": proxy_rows[index]["checksum"],
                    "landmark": f"prefix landmark {zone}",
                    "event": {
                        "observed": True,
                        "method": "manual independent content observation",
                        "description": f"visible and audible event for {zone}",
                        "video": {
                            "observed": True,
                            "description": f"visible change for {zone}",
                        },
                        "audio": {
                            "observed": True,
                            "description": f"audio transient for {zone}",
                        },
                    },
                    "source_observation": windows["source"],
                    "proxy_observation": windows["proxy"],
                }
            )
        observation = {
            "schema_version": prefix.audio_verifier.CONTENT_ANCHOR_SCHEMA_VERSION,
            "kind": prefix.audio_verifier.CONTENT_ANCHOR_OBSERVATION_KIND,
            "scope": "prefix",
            "observer": {
                "path": str(
                    prefix.audio_verifier.CONTENT_ANCHOR_CAPTURE_TOOL.resolve()
                ),
                "sha256": sha256(
                    prefix.audio_verifier.CONTENT_ANCHOR_CAPTURE_TOOL
                ),
            },
            "method": "manual frame/audio-window landmark comparison",
            "audio_clock": {
                "source_stream": "0:a:0",
                "proxy_stream": "0:a:0",
                "sample_index_basis": "decoded_ashowinfo_pts",
                "source_sample_rate": 100,
                "proxy_sample_rate": 100,
            },
            "anchors": anchors,
        }
        observation_path = fixture["manifest_path"].with_name(
            "content-anchor.observations.json"
        )
        observation_path.write_text(json.dumps(observation), encoding="utf-8")
        source_observation = {
            "schema_version": prefix.audio_verifier.SOURCE_CONTENT_ANCHOR_SCHEMA_VERSION,
            "kind": prefix.audio_verifier.SOURCE_CONTENT_ANCHOR_OBSERVATION_KIND,
            "scope": "prefix",
            "source_frame_domain": [0, len(source_rows)],
            "observer": observation["observer"],
            "method": "manual source-only frame/audio-window event registration",
            "audio_clock": {
                "source_stream": "0:a:0",
                "sample_index_basis": "decoded_ashowinfo_pts",
                "source_sample_rate": 100,
            },
            "anchors": [
                {
                    key: value
                    for key, value in anchor.items()
                    if key
                    in {
                        "zone",
                        "evidence_id",
                        "source_frame_index",
                        "source_audio_sample",
                        "source_frame_checksum",
                        "event",
                        "source_observation",
                    }
                }
                for anchor in anchors
            ],
        }
        source_observation_path = fixture["manifest_path"].with_name(
            "source-anchor.observations.json"
        )
        source_observation_path.write_text(
            json.dumps(source_observation), encoding="utf-8"
        )
        source_anchor_manifest_path = fixture["manifest_path"].with_name(
            "source-anchor.manifest.json"
        )
        content_manifest_path = fixture["manifest_path"].with_name(
            "content-anchor.manifest.json"
        )
        ffmpeg_record = {
            "path": str(fixture["ffmpeg"].resolve()),
            "sha256": sha256(fixture["ffmpeg"]),
            "version_line": "ffmpeg version test",
            "library_lines": [],
        }
        with (
            mock.patch.object(
                prefix.audio_verifier,
                "ffmpeg_version_record",
                return_value=ffmpeg_record,
            ),
            mock.patch.object(
                prefix.audio_verifier,
                "decode_audio",
                side_effect=lambda media, _ffmpeg: (
                    fixture["source_audio"]
                    if Path(media).resolve() == fixture["source"].resolve()
                    else fixture["proxy_audio"]
                ),
            ),
            self._reextraction_patch(),
        ):
            source_manifest = (
                prefix.audio_verifier.create_source_content_anchor_manifest(
                    fixture["source"],
                    Path(fixture["manifest"]["source"]["oracle"]["path"]),
                    source_observation_path,
                    source_anchor_manifest_path,
                    fixture["ffmpeg"],
                    scope="prefix",
                )
            )
            subject = json.loads(
                fixture["manifest_path"].read_text(encoding="utf-8")
            )
            subject["created_utc"] = (
                datetime.fromisoformat(source_manifest["created_utc"])
                + timedelta(seconds=1)
            ).isoformat()
            subject["source_anchor_manifest"] = {
                "path": str(source_anchor_manifest_path.resolve()),
                "sha256": sha256(source_anchor_manifest_path),
            }
            fixture["manifest_path"].write_text(
                json.dumps(subject), encoding="utf-8"
            )
            prefix.audio_verifier.create_content_anchor_manifest(
                fixture["manifest_path"],
                fixture["video_manifest_path"],
                fixture["source"],
                fixture["audio_proxy"],
                Path(fixture["manifest"]["source"]["oracle"]["path"]),
                proxy_oracle_path,
                observation_path,
                content_manifest_path,
                fixture["ffmpeg"],
                scope="prefix",
                max_error_seconds=0.010,
                source_anchor_manifest_path=source_anchor_manifest_path,
            )
        return proxy_oracle_path, content_manifest_path

    def test_prefix_report_is_blocked_for_scope_and_missing_content_anchors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            report = self._verify(fixture)

            self.assertEqual(report["status"], "BLOCKED")
            self.assertIn("SOURCE_PREFIX_ONLY", report["reason_codes"])
            self.assertIn("AV_CONTENT_ANCHORS_NOT_PROVIDED", report["reason_codes"])
            self.assertNotIn("VIDEO_VALIDATION_NOT_PASS", report["reason_codes"])

    def test_guard_only_generic_oracle_block_does_not_override_dedicated_video_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            report = self._verify(fixture, oracle_exit=prefix.frame_oracle.EXIT_BLOCKED)

            self.assertEqual(report["video_validation"]["status"], "PASS")
            self.assertNotIn("VIDEO_VALIDATION_NOT_PASS", report["reason_codes"])
            self.assertIn("SOURCE_PREFIX_ONLY", report["reason_codes"])

    def test_negative_audio_start_is_a_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            rows = audio_rows(3, pts=[-2, 0, 2])
            fixture["proxy_audio"] = audio_report(rows)
            report = self._verify(fixture)

            self.assertEqual(report["status"], "BLOCKED")
            self.assertNotIn(
                "AV_START_ANCHOR_ERROR_EXCEEDED", report["reason_codes"]
            )
            self.assertIn(
                "AV_START_ANCHOR_ERROR_EXCEEDED", report["diagnostic_codes"]
            )
            self.assertEqual(
                report["av_sync"]["timestamp_boundaries"]["start_error_seconds"],
                0.02,
            )

    def test_identity_offset_span_is_explicit_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value), source_pts=[0, 4, 4, 6])
            report = self._verify(fixture)

            self.assertEqual(report["status"], "BLOCKED")
            self.assertNotIn("VIDEO_NORMALIZATION_OFFSET_SPAN_EXCEEDED", report["reason_codes"])
            self.assertEqual(report["source_pts_identity"]["status"], "NOT_PRESERVED")
            self.assertAlmostEqual(report["normalization_offset"]["span_seconds"], 0.02)

    def test_stale_source_binding_is_rejected_before_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            fixture["manifest"]["source"]["sha256"] = "0" * 64
            fixture["manifest_path"].write_text(
                json.dumps(fixture["manifest"]), encoding="utf-8"
            )

            with self.assertRaises(prefix.PrefixEvidenceError):
                self._verify(fixture)

    def test_scope_tampering_is_rejected_before_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            fixture["manifest"]["scope"] = "full"
            fixture["manifest_path"].write_text(
                json.dumps(fixture["manifest"]), encoding="utf-8"
            )

            with self.assertRaises(prefix.PrefixEvidenceError):
                self._verify(fixture)

    def test_video_proxy_chain_binding_is_rejected_before_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            fixture["manifest"]["video_proxy"]["sha256"] = "0" * 64
            fixture["manifest_path"].write_text(
                json.dumps(fixture["manifest"]), encoding="utf-8"
            )

            with self.assertRaises(prefix.PrefixEvidenceError):
                self._verify(fixture)

    def test_full_scope_is_explicitly_blocked_even_with_consistent_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            fixture["manifest"]["scope"] = "full"
            fixture["manifest"]["source"]["scope"] = "full"
            fixture["video_manifest"]["source"]["scope"] = "full"
            fixture["video_manifest_path"].write_text(
                json.dumps(fixture["video_manifest"]), encoding="utf-8"
            )
            fixture["manifest"]["video_manifest"]["sha256"] = sha256(
                fixture["video_manifest_path"]
            )
            fixture["manifest_path"].write_text(
                json.dumps(fixture["manifest"]), encoding="utf-8"
            )

            report = self._verify(fixture)

            self.assertEqual(report["status"], "BLOCKED")
            self.assertIn("AUDIO_PREFIX_SCOPE_NOT_PREFIX", report["reason_codes"])

    def test_passing_content_anchors_pass_av_sync_but_prefix_scope_blocks_overall(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(
                Path(temp_value), source_pts=[0, 2, 2, 6]
            )
            proxy_oracle_path, content_manifest_path = (
                self._bound_content_anchor_manifest(fixture)
            )
            report_output = fixture["manifest_path"].with_name("verify.json")
            with (
                mock.patch.object(
                    prefix.frame_oracle,
                    "probe_video",
                    side_effect=AssertionError("existing oracle must avoid a new probe"),
                ),
                mock.patch.object(
                    prefix.proxy_verifier,
                    "evaluate_video_proxy",
                    return_value={"status": "PASS"},
                ),
                mock.patch.object(
                    prefix.audio_verifier,
                    "decode_audio",
                    side_effect=[fixture["source_audio"], fixture["proxy_audio"]],
                ),
                self._reextraction_patch(),
            ):
                report = prefix.verify_prefix(
                    fixture["manifest_path"],
                    None,
                    report_output,
                    fixture["ffmpeg"],
                    proxy_oracle_path=proxy_oracle_path,
                    content_anchor_manifest_path=content_manifest_path,
                )

            self.assertEqual(report["av_sync"]["status"], "PASS")
            self.assertEqual(
                report["av_sync"]["content_anchors"]["status"], "PASS"
            )
            self.assertEqual(report["status"], "BLOCKED")
            self.assertEqual(report["reason_codes"], ["SOURCE_PREFIX_ONLY"])
            self.assertEqual(report["source_pts_identity"]["status"], "NOT_PRESERVED")
            self.assertIn(
                "AV_END_ANCHOR_ERROR_EXCEEDED", report["diagnostic_codes"]
            )

    def test_av_sync_cannot_pass_when_video_component_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(
                Path(temp_value), source_pts=[0, 2, 2, 6]
            )
            proxy_oracle_path, content_manifest_path = (
                self._bound_content_anchor_manifest(fixture)
            )
            report_output = fixture["manifest_path"].with_name("verify.json")
            with (
                mock.patch.object(
                    prefix.frame_oracle,
                    "probe_video",
                    side_effect=AssertionError("existing oracle must avoid a new probe"),
                ),
                mock.patch.object(
                    prefix.proxy_verifier,
                    "evaluate_video_proxy",
                    return_value={"status": "BLOCKED", "reason_codes": ["TEST"]},
                ),
                mock.patch.object(
                    prefix.audio_verifier,
                    "decode_audio",
                    side_effect=[fixture["source_audio"], fixture["proxy_audio"]],
                ),
                self._reextraction_patch(),
            ):
                report = prefix.verify_prefix(
                    fixture["manifest_path"],
                    None,
                    report_output,
                    fixture["ffmpeg"],
                    proxy_oracle_path=proxy_oracle_path,
                    content_anchor_manifest_path=content_manifest_path,
                )

            self.assertEqual(report["av_sync"]["status"], "BLOCKED")
            self.assertIn("VIDEO_VALIDATION_NOT_PASS", report["av_sync"]["reason_codes"])
            self.assertEqual(
                report["av_sync"]["components"]["content_anchors"], "PASS"
            )
            self.assertEqual(
                report["av_sync"]["components"]["video_validation"], "BLOCKED"
            )

    def test_old_video_manifest_schema_is_rejected_by_build_and_verify(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            fixture = self._fixture(temp)
            fixture["video_manifest"]["schema_version"] = (
                prefix.proxy_verifier.SCHEMA_VERSION - 1
            )
            fixture["video_manifest_path"].write_text(
                json.dumps(fixture["video_manifest"]), encoding="utf-8"
            )
            with self.assertRaisesRegex(prefix.PrefixEvidenceError, "schema or kind"):
                prefix.build_prefix(
                    fixture["video_manifest_path"],
                    temp / "build-output.mp4",
                    temp / "build.manifest.json",
                    fixture["ffmpeg"],
                )

            fixture["manifest"]["video_manifest"]["sha256"] = sha256(
                fixture["video_manifest_path"]
            )
            fixture["manifest_path"].write_text(
                json.dumps(fixture["manifest"]), encoding="utf-8"
            )
            with self.assertRaisesRegex(prefix.PrefixEvidenceError, "schema or kind"):
                self._verify(fixture)

    def test_atomic_publish_race_does_not_overwrite_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            fixture = self._fixture(temp)
            output = temp / "race-output.mp4"
            manifest_output = temp / "race.manifest.json"

            def finish_after_competitor(command, **_kwargs):
                Path(command[-1]).write_bytes(b"candidate-output")
                output.write_bytes(b"existing-winner")
                return prefix.subprocess.CompletedProcess(command, 0, b"", b"")

            with mock.patch.object(
                prefix.subprocess, "run", side_effect=finish_after_competitor
            ):
                with self.assertRaises(FileExistsError):
                    prefix.build_prefix(
                        fixture["video_manifest_path"],
                        output,
                        manifest_output,
                        fixture["ffmpeg"],
                    )

            self.assertEqual(output.read_bytes(), b"existing-winner")
            self.assertFalse(manifest_output.exists())
            self.assertEqual(
                list(temp.glob(".race-output.*.tmp.mp4")),
                [],
            )

    def test_json_serialization_failure_does_not_consume_output_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            output = Path(temp_value) / "report.json"

            with self.assertRaises(ValueError):
                prefix._write_json_new(output, {"threshold": float("nan")})

            self.assertFalse(output.exists())

    def test_invalid_threshold_is_rejected_before_evidence_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            fixture = self._fixture(temp)
            oracle_output = temp / "proxy.oracle.json"
            report_output = temp / "verify.json"

            with mock.patch.object(
                prefix.frame_oracle,
                "probe_video",
                side_effect=AssertionError("invalid thresholds must fail first"),
            ):
                with self.assertRaisesRegex(prefix.PrefixEvidenceError, "threshold"):
                    prefix.verify_prefix(
                        fixture["manifest_path"],
                        oracle_output,
                        report_output,
                        fixture["ffmpeg"],
                        max_anchor_error_seconds=float("inf"),
                    )

            self.assertFalse(oracle_output.exists())
            self.assertFalse(report_output.exists())

    def test_verify_rejects_both_generated_and_existing_proxy_oracles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            fixture = self._fixture(temp)
            with self.assertRaises(prefix.PrefixEvidenceError):
                prefix.verify_prefix(
                    fixture["manifest_path"],
                    temp / "new.oracle.json",
                    temp / "verify.json",
                    fixture["ffmpeg"],
                    proxy_oracle_path=temp / "existing.oracle.json",
                )

    def test_verify_requires_one_proxy_oracle_route(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            fixture = self._fixture(temp)
            with self.assertRaises(prefix.PrefixEvidenceError):
                prefix.verify_prefix(
                    fixture["manifest_path"],
                    None,
                    temp / "verify.json",
                    fixture["ffmpeg"],
                )

    def test_verify_cli_proxy_oracle_routes_are_mutually_exclusive(self) -> None:
        with self.assertRaises(SystemExit):
            prefix._parser().parse_args(
                [
                    "verify",
                    "prefix.manifest.json",
                    "--oracle-output",
                    "new.oracle.json",
                    "--proxy-oracle",
                    "existing.oracle.json",
                    "--output",
                    "verify.json",
                ]
            )


if __name__ == "__main__":
    unittest.main()
