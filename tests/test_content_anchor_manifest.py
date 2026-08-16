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
MODULE_PATH = REPO / "scripts" / "verify_proxy_audio_av.py"
SPEC = importlib.util.spec_from_file_location("verify_proxy_audio_av", MODULE_PATH)
assert SPEC and SPEC.loader
audio = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audio)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ContentAnchorManifestTests(unittest.TestCase):
    BUSINESS_FRAME_COUNT = 12
    DURATION_TICKS = 1000
    SAMPLE_RATE = 1000
    PNG_CAPTURE = b"\x89PNG\r\n\x1a\nTEST-PNG"
    WAV_CAPTURE = b"RIFF\x10\x00\x00\x00WAVETEST-WAV"

    def _write_json(self, path: Path, value: object) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _media_record(self, path: Path) -> dict:
        return {
            "path": str(path.resolve()),
            "sha256": sha256(path),
            "size": path.stat().st_size,
        }

    def _reextraction_patch(self):
        def reproduce(_command: list[str], output: Path, _name: str) -> None:
            output.write_bytes(
                self.PNG_CAPTURE
                if output.suffix.lower() == ".png"
                else self.WAV_CAPTURE
            )

        return mock.patch.object(
            audio,
            "_run_content_anchor_reextraction",
            side_effect=reproduce,
        )

    def _oracle(
        self,
        media: Path,
        rows: list[dict],
        *,
        status: str,
        authoritative: bool,
    ) -> dict:
        return {
            "schema_version": audio.frame_oracle.SCHEMA_VERSION,
            "kind": "mpv_phase0_frame_pts_oracle",
            "status": status,
            "authoritative_frame_timeline": authoritative,
            "reason_codes": (
                [] if status == "PASS" else ["PTS_DUPLICATE", "PTS_NON_MONOTONIC"]
            ),
            "video": self._media_record(media),
            "showinfo": {
                "time_base": {"numerator": 1, "denominator": 1000},
                "pixel_formats": ["yuv420p"],
                "parsed_frames": len(rows),
            },
            "ffmpeg": {"returncode": 0},
            "pts_table": rows,
        }

    def _audio_report(self) -> dict:
        end_sample = self.BUSINESS_FRAME_COUNT * self.SAMPLE_RATE
        rows = [
            {
                "n": index,
                "pts": index * self.SAMPLE_RATE,
                "pts_time": float(index),
                "nb_samples": self.SAMPLE_RATE,
                "rate": self.SAMPLE_RATE,
                "channels": 2,
                "chlayout": "stereo",
                "checksum": f"A{index + 1:07X}",
            }
            for index in range(self.BUSINESS_FRAME_COUNT)
        ]
        return {
            "status": "PASS",
            "reason_codes": [],
            "command": ["ffmpeg", "-i", "test-input"],
            "stream_present": True,
            "codec": "aac",
            "format": {
                "sample_rate": self.SAMPLE_RATE,
                "channels": 2,
                "channel_layout": "stereo",
            },
            "frames": {
                "count": self.BUSINESS_FRAME_COUNT,
                "pts_table": rows,
                "first": rows[0],
                "last": rows[-1],
                "total_samples": end_sample,
                "start_pts": 0,
                "end_pts_exclusive": end_sample,
                "start_time": 0.0,
                "end_time": end_sample / self.SAMPLE_RATE,
            },
            "continuity": {
                "status": "PASS",
                "error_count": 0,
                "examples": [],
            },
            "format_validation": {
                "status": "PASS",
                "error_count": 0,
                "examples": [],
            },
        }

    def _fixture(self, temp: Path) -> dict:
        source_media = temp / "source.mp4"
        proxy_media = temp / "proxy.mp4"
        source_media.write_bytes(b"content-anchor-source-media")
        proxy_media.write_bytes(b"content-anchor-proxy-media")

        checksums = [f"{index + 1:08X}" for index in range(self.BUSINESS_FRAME_COUNT)]
        source_pts = [
            0,
            1000,
            2000,
            3000,
            2000,
            5000,
            6000,
            7000,
            8000,
            9000,
            10000,
            11000,
        ]
        source_rows = [
            {
                "n": index,
                "pts": source_pts[index],
                "pts_time": source_pts[index] / 1000,
                "duration": self.DURATION_TICKS,
                "duration_time": 1.0,
                "checksum": checksum,
            }
            for index, checksum in enumerate(checksums)
        ]
        proxy_rows = [
            {
                "n": index,
                "pts": index * self.DURATION_TICKS,
                "pts_time": float(index),
                "duration": self.DURATION_TICKS,
                "duration_time": 1.0,
                "checksum": checksum,
            }
            for index, checksum in enumerate(checksums)
        ]
        proxy_rows.append(
            {
                "n": self.BUSINESS_FRAME_COUNT,
                "pts": self.BUSINESS_FRAME_COUNT * self.DURATION_TICKS,
                "pts_time": float(self.BUSINESS_FRAME_COUNT),
                "duration": 0,
                "duration_time": 0.0,
                "checksum": checksums[-1],
            }
        )

        source_oracle = self._oracle(
            source_media,
            source_rows,
            status="BLOCKED",
            authoritative=False,
        )
        proxy_oracle = self._oracle(
            proxy_media,
            proxy_rows,
            status="BLOCKED",
            authoritative=False,
        )
        proxy_oracle["reason_codes"] = ["FRAME_DURATION_NON_POSITIVE"]
        proxy_oracle["examples"] = {
            "duration_problems": [
                {
                    "n": self.BUSINESS_FRAME_COUNT,
                    "field": "duration",
                    "value": 0,
                }
            ]
        }
        proxy_oracle["ffmpeg"] = {"returncode": 0}
        source_oracle_path = temp / "source.oracle.json"
        proxy_oracle_path = temp / "proxy.oracle.json"
        self._write_json(source_oracle_path, source_oracle)
        self._write_json(proxy_oracle_path, proxy_oracle)

        ffmpeg = temp / "ffmpeg.exe"
        ffmpeg.write_bytes(b"bound-test-ffmpeg")
        source_audio = self._audio_report()
        proxy_audio = self._audio_report()

        timeline = {
            "time_base": {"numerator": 1, "denominator": 1000},
            "duration_ticks": self.DURATION_TICKS,
            "business_pts_start": 0,
            "business_pts_end_exclusive": (
                self.BUSINESS_FRAME_COUNT * self.DURATION_TICKS
            ),
        }
        subject_manifest = {
            "schema_version": audio.proxy_verifier.SCHEMA_VERSION,
            "kind": audio.proxy_verifier.MANIFEST_KIND,
            "created_utc": "2099-01-01T00:00:00+00:00",
            "scope": "full",
            "source": {
                **self._media_record(source_media),
                "scope": "full",
                "business_frame_count": self.BUSINESS_FRAME_COUNT,
                "oracle": {
                    "path": str(source_oracle_path.resolve()),
                    "sha256": sha256(source_oracle_path),
                },
            },
            "proxy": self._media_record(proxy_media),
            "normalized_timeline": timeline,
            "terminal_guard": {
                "proxy_frame_index": self.BUSINESS_FRAME_COUNT,
                "pts_ticks": self.BUSINESS_FRAME_COUNT * self.DURATION_TICKS,
                "business_frame_domain": [0, self.BUSINESS_FRAME_COUNT],
                "included_in_business_domain": False,
                "expected_checksum": checksums[-1],
            },
            "generation": {"encoder": {"pixel_format": "yuv420p"}},
        }
        subject_manifest_path = temp / "subject.manifest.json"
        self._write_json(subject_manifest_path, subject_manifest)

        # Full verification uses one manifest as both the subject binding and
        # the owner of the normalized timeline.  Prefix verification may pass
        # two different paths through the same public API.
        timeline_manifest_path = subject_manifest_path

        zone_indices = {
            "start": 0,
            "pts_conflict": 4,
            "middle": 6,
            "end": self.BUSINESS_FRAME_COUNT - 1,
        }
        anchors: list[dict] = []
        observation_artifacts: list[Path] = []
        for zone, index in zone_indices.items():
            sample = index * self.SAMPLE_RATE
            window_records: dict[str, dict] = {}
            for side, media, oracle_path, evidence in (
                ("source", source_media, source_oracle_path, source_audio),
                ("proxy", proxy_media, proxy_oracle_path, proxy_audio),
            ):
                visual = temp / f"{zone}.{side}.png"
                audio_window = temp / f"{zone}.{side}.wav"
                captured_visual = visual.with_name(
                    f".{visual.stem}.{'0' * 32}.capture{visual.suffix}"
                )
                captured_audio = audio_window.with_name(
                    f".{audio_window.stem}.{'1' * 32}.capture{audio_window.suffix}"
                )
                visual.write_bytes(self.PNG_CAPTURE)
                audio_window.write_bytes(self.WAV_CAPTURE)
                start_sample = max(0, sample - 1)
                end_sample = min(
                    self.BUSINESS_FRAME_COUNT * self.SAMPLE_RATE,
                    sample + 2,
                )
                audio_frame = evidence["frames"]["pts_table"][index]
                window = {
                    "schema_version": audio.CONTENT_ANCHOR_SCHEMA_VERSION,
                    "kind": audio.CONTENT_ANCHOR_WINDOW_KIND,
                    "side": side,
                    "scope": "full",
                    "media": self._media_record(media),
                    "oracle": {
                        "path": str(oracle_path.resolve()),
                        "sha256": sha256(oracle_path),
                    },
                    "video_frame": {
                        "index": index,
                        "checksum": checksums[index],
                    },
                    "audio_frame": {
                        field: audio_frame[field]
                        for field in ("n", "pts", "nb_samples", "checksum")
                    },
                    "audio_window": {
                        "stream": "0:a:0",
                        "sample_rate": self.SAMPLE_RATE,
                        "anchor_sample": sample,
                        "start_sample": start_sample,
                        "end_sample_exclusive": end_sample,
                    },
                    "visual_artifact": {
                        **self._media_record(visual),
                        "media_type": "image/png",
                    },
                    "audio_artifact": {
                        **self._media_record(audio_window),
                        "media_type": "audio/wav",
                    },
                    "capture": {
                        "tool": self._media_record(
                            audio.CONTENT_ANCHOR_CAPTURE_TOOL
                        ),
                        "ffmpeg": self._media_record(ffmpeg),
                        "commands": {
                            "video": audio.content_anchor_video_capture_command(
                                ffmpeg, media, index, captured_visual
                            ),
                            "audio": audio.content_anchor_audio_capture_command(
                                ffmpeg,
                                media,
                                decoded_start_sample=start_sample,
                                decoded_end_sample_exclusive=end_sample,
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
                window_path = temp / f"{zone}.{side}.window.json"
                self._write_json(window_path, window)
                window_records[side] = {
                    "path": str(window_path.resolve()),
                    "sha256": sha256(window_path),
                }
                observation_artifacts.extend((window_path, visual, audio_window))
            anchors.append(
                {
                    "zone": zone,
                    "evidence_id": f"sample3-{zone}",
                    "source_frame_index": index,
                    "proxy_frame_index": index,
                    "source_audio_sample": sample,
                    "proxy_audio_sample": sample,
                    "source_frame_checksum": checksums[index],
                    "proxy_frame_checksum": checksums[index],
                    "landmark": f"visible/audio landmark for {zone}",
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
                    "source_observation": window_records["source"],
                    "proxy_observation": window_records["proxy"],
                }
            )
        observation = {
            "schema_version": audio.CONTENT_ANCHOR_SCHEMA_VERSION,
            "kind": audio.CONTENT_ANCHOR_OBSERVATION_KIND,
            "scope": "full",
            "observer": {},
            "method": "manual frame/audio-window landmark comparison",
            "audio_clock": {
                "source_stream": "0:a:0",
                "proxy_stream": "0:a:0",
                "sample_index_basis": "decoded_ashowinfo_pts",
                "source_sample_rate": self.SAMPLE_RATE,
                "proxy_sample_rate": self.SAMPLE_RATE,
            },
            "anchors": anchors,
        }
        observation["observer"] = {
            "path": str(audio.CONTENT_ANCHOR_CAPTURE_TOOL.resolve()),
            "sha256": sha256(audio.CONTENT_ANCHOR_CAPTURE_TOOL),
        }
        observation_path = temp / "content-anchor.observations.json"
        self._write_json(observation_path, observation)

        source_observation = {
            "schema_version": audio.SOURCE_CONTENT_ANCHOR_SCHEMA_VERSION,
            "kind": audio.SOURCE_CONTENT_ANCHOR_OBSERVATION_KIND,
            "scope": "full",
            "source_frame_domain": [0, self.BUSINESS_FRAME_COUNT],
            "observer": observation["observer"],
            "method": "manual source-only frame/audio-window event registration",
            "audio_clock": {
                "source_stream": "0:a:0",
                "sample_index_basis": "decoded_ashowinfo_pts",
                "source_sample_rate": self.SAMPLE_RATE,
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
        source_observation_path = temp / "source-anchor.observations.json"
        self._write_json(source_observation_path, source_observation)
        source_anchor_manifest_path = temp / "source-anchor.manifest.json"

        output_path = temp / "content-anchor.manifest.json"
        return {
            "subject_manifest_path": subject_manifest_path,
            "timeline_manifest_path": timeline_manifest_path,
            "source_media_path": source_media,
            "proxy_media_path": proxy_media,
            "source_oracle_path": source_oracle_path,
            "proxy_oracle_path": proxy_oracle_path,
            "source_oracle": source_oracle,
            "proxy_oracle": proxy_oracle,
            "source_audio": source_audio,
            "proxy_audio": proxy_audio,
            "observation_path": observation_path,
            "source_observation_path": source_observation_path,
            "source_anchor_manifest_path": source_anchor_manifest_path,
            "observation_artifacts": observation_artifacts,
            "output_path": output_path,
            "ffmpeg": ffmpeg,
        }

    def _create(self, fixture: dict) -> dict:
        ffmpeg_record = {
            "path": str(fixture["ffmpeg"].resolve()),
            "sha256": sha256(fixture["ffmpeg"]),
            "version_line": "ffmpeg version test",
            "library_lines": [],
        }
        with (
            mock.patch.object(
                audio, "ffmpeg_version_record", return_value=ffmpeg_record
            ),
            mock.patch.object(
                audio,
                "decode_audio",
                side_effect=lambda media, _ffmpeg: (
                    fixture["source_audio"]
                    if Path(media).resolve() == fixture["source_media_path"].resolve()
                    else fixture["proxy_audio"]
                ),
            ),
            self._reextraction_patch(),
        ):
            if not fixture["source_anchor_manifest_path"].exists():
                source_manifest = audio.create_source_content_anchor_manifest(
                    fixture["source_media_path"],
                    fixture["source_oracle_path"],
                    fixture["source_observation_path"],
                    fixture["source_anchor_manifest_path"],
                    fixture["ffmpeg"],
                    scope="full",
                )
                subject = json.loads(
                    fixture["subject_manifest_path"].read_text(encoding="utf-8")
                )
                registered = datetime.fromisoformat(source_manifest["created_utc"])
                subject["created_utc"] = (
                    registered + timedelta(seconds=1)
                ).isoformat()
                subject["source_anchor_manifest"] = {
                    "path": str(
                        fixture["source_anchor_manifest_path"].resolve()
                    ),
                    "sha256": sha256(fixture["source_anchor_manifest_path"]),
                }
                self._write_json(fixture["subject_manifest_path"], subject)
            return audio.create_content_anchor_manifest(
                fixture["subject_manifest_path"],
                fixture["timeline_manifest_path"],
                fixture["source_media_path"],
                fixture["proxy_media_path"],
                fixture["source_oracle_path"],
                fixture["proxy_oracle_path"],
                fixture["observation_path"],
                fixture["output_path"],
                fixture["ffmpeg"],
                scope="full",
                max_error_seconds=0.010,
                source_anchor_manifest_path=fixture[
                    "source_anchor_manifest_path"
                ],
            )

    def _evaluate(self, fixture: dict, manifest_path: Path | None = None) -> dict:
        if manifest_path is None:
            manifest_path = fixture["output_path"]
        with self._reextraction_patch():
            return audio.evaluate_bound_content_anchor_manifest(
                manifest_path,
                subject_manifest_path=fixture["subject_manifest_path"],
                timeline_manifest_path=fixture["timeline_manifest_path"],
                source_media_path=fixture["source_media_path"],
                proxy_media_path=fixture["proxy_media_path"],
                source_oracle_path=fixture["source_oracle_path"],
                proxy_oracle_path=fixture["proxy_oracle_path"],
                source_oracle=fixture["source_oracle"],
                proxy_oracle=fixture["proxy_oracle"],
                source_audio=fixture["source_audio"],
                proxy_audio=fixture["proxy_audio"],
                expected_scope="full",
                max_error_seconds=0.010,
                expected_ffmpeg_path=fixture["ffmpeg"],
            )

    def _full_video_report(self, fixture: dict) -> Path:
        video_manifest = json.loads(
            fixture["subject_manifest_path"].read_text(encoding="utf-8")
        )
        claimed = audio.proxy_verifier.evaluate_video_proxy(
            video_manifest,
            fixture["source_oracle"]["pts_table"],
            fixture["proxy_oracle"],
        )
        report = {
            "schema_version": audio.proxy_verifier.SCHEMA_VERSION,
            "kind": audio.proxy_verifier.REPORT_KIND,
            "manifest": {
                "path": str(fixture["subject_manifest_path"].resolve()),
                "sha256": sha256(fixture["subject_manifest_path"]),
            },
            "scope": "full",
            "decoded_proxy_oracle": {
                "path": str(fixture["proxy_oracle_path"].resolve()),
                "sha256": sha256(fixture["proxy_oracle_path"]),
            },
            "video_validation": claimed,
        }
        report_path = fixture["subject_manifest_path"].with_name("video.verify.json")
        self._write_json(report_path, report)
        return report_path

    def _verify_full(
        self,
        fixture: dict,
        *,
        content_anchor_manifest_path: Path | None,
    ) -> dict:
        report_path = self._full_video_report(fixture)
        run_path = fixture["subject_manifest_path"].with_name("audio.run.json")
        output_path = fixture["subject_manifest_path"].with_name("audio.verify.json")
        ffmpeg = audio.frame_oracle.FfmpegExecutable(
            fixture["ffmpeg"].resolve(), "--ffmpeg"
        )
        ffmpeg_record = {
            "path": str(fixture["ffmpeg"].resolve()),
            "sha256": sha256(fixture["ffmpeg"]),
            "version_line": "ffmpeg version test",
            "library_lines": [],
        }
        with (
            mock.patch.object(
                audio,
                "decode_audio",
                side_effect=[fixture["source_audio"], fixture["proxy_audio"]],
            ),
            mock.patch.object(
                audio, "ffmpeg_version_record", return_value=ffmpeg_record
            ),
            self._reextraction_patch(),
        ):
            return audio.verify_audio_av(
                fixture["subject_manifest_path"],
                report_path,
                run_path,
                output_path,
                ffmpeg,
                max_anchor_error_seconds=0.010,
                max_identity_offset_span_seconds=0.010,
                allow_no_audio=False,
                content_anchor_manifest_path=content_anchor_manifest_path,
            )

    def _rewrite_observation(self, fixture: dict, mutate) -> None:
        observation = json.loads(
            fixture["observation_path"].read_text(encoding="utf-8")
        )
        mutate(observation)
        self._write_json(fixture["observation_path"], observation)

        # Keep the outer binding current so semantic-negative tests reach the
        # anchor evaluator instead of stopping at the observation-file hash.
        manifest = json.loads(fixture["output_path"].read_text(encoding="utf-8"))
        matches = 0

        def rebind(value: object) -> None:
            nonlocal matches
            if isinstance(value, dict):
                path_value = value.get("path")
                if isinstance(path_value, str):
                    try:
                        same_path = (
                            Path(path_value).expanduser().resolve()
                            == fixture["observation_path"].resolve()
                        )
                    except (OSError, RuntimeError):
                        same_path = False
                    if same_path and "sha256" in value:
                        value["sha256"] = sha256(fixture["observation_path"])
                        matches += 1
                for child in value.values():
                    rebind(child)
            elif isinstance(value, list):
                for child in value:
                    rebind(child)

        rebind(manifest)
        self.assertGreater(matches, 0, "manifest must bind the observation input")
        self._write_json(fixture["output_path"], manifest)

    def test_valid_four_zone_manifest_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            manifest = self._create(fixture)
            result = self._evaluate(fixture)

            self.assertEqual(
                manifest["kind"], audio.CONTENT_ANCHOR_MANIFEST_KIND
            )
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["reason_codes"], [])
            self.assertEqual(
                result["observed_zones"],
                ["start", "pts_conflict", "middle", "end"],
            )
            source_manifest = json.loads(
                fixture["source_anchor_manifest_path"].read_text(encoding="utf-8")
            )
            self.assertTrue(source_manifest["source_only"])
            self.assertEqual(
                manifest["bindings"]["source_anchor_manifest"]["sha256"],
                sha256(fixture["source_anchor_manifest_path"]),
            )

            def assert_no_proxy_key(value: object) -> None:
                if isinstance(value, dict):
                    for key, child in value.items():
                        self.assertNotIn("proxy", key.lower())
                        assert_no_proxy_key(child)
                elif isinstance(value, list):
                    for child in value:
                        assert_no_proxy_key(child)

            assert_no_proxy_key(source_manifest)

    def test_proxy_manifest_without_source_registration_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            self._create(fixture)
            manifest = json.loads(
                fixture["output_path"].read_text(encoding="utf-8")
            )
            manifest["bindings"].pop("source_anchor_manifest")
            manifest.pop("source_anchor_registration")
            self._write_json(fixture["output_path"], manifest)

            result = self._evaluate(fixture)
            self.assertEqual(result["status"], "BLOCKED")
            self.assertIn(
                "AV_SOURCE_ANCHOR_MANIFEST_NOT_PROVIDED",
                result["reason_codes"],
            )

    def test_subject_proxy_missing_generation_binding_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            self._create(fixture)
            subject = json.loads(
                fixture["subject_manifest_path"].read_text(encoding="utf-8")
            )
            subject.pop("source_anchor_manifest")
            self._write_json(fixture["subject_manifest_path"], subject)
            manifest = json.loads(
                fixture["output_path"].read_text(encoding="utf-8")
            )
            subject_sha = sha256(fixture["subject_manifest_path"])
            manifest["bindings"]["subject_manifest"]["sha256"] = subject_sha
            manifest["bindings"]["timeline_manifest"]["sha256"] = subject_sha
            self._write_json(fixture["output_path"], manifest)

            result = self._evaluate(fixture)
            self.assertEqual(result["status"], "BLOCKED")
            self.assertIn(
                "AV_SOURCE_ANCHOR_NOT_BOUND_AT_PROXY_GENERATION",
                result["reason_codes"],
            )

    def test_subject_proxy_bound_to_another_source_manifest_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            self._create(fixture)
            other_source_manifest = fixture[
                "source_anchor_manifest_path"
            ].with_name("other-source-anchor.manifest.json")
            other_source_manifest.write_bytes(
                fixture["source_anchor_manifest_path"].read_bytes()
            )
            subject = json.loads(
                fixture["subject_manifest_path"].read_text(encoding="utf-8")
            )
            subject["source_anchor_manifest"] = {
                "path": str(other_source_manifest.resolve()),
                "sha256": sha256(other_source_manifest),
            }
            self._write_json(fixture["subject_manifest_path"], subject)
            manifest = json.loads(
                fixture["output_path"].read_text(encoding="utf-8")
            )
            subject_sha = sha256(fixture["subject_manifest_path"])
            manifest["bindings"]["subject_manifest"]["sha256"] = subject_sha
            manifest["bindings"]["timeline_manifest"]["sha256"] = subject_sha
            self._write_json(fixture["output_path"], manifest)

            result = self._evaluate(fixture)
            self.assertEqual(result["status"], "BLOCKED")
            self.assertIn(
                "AV_SOURCE_ANCHOR_NOT_BOUND_AT_PROXY_GENERATION",
                result["reason_codes"],
            )

    def test_source_registration_tampering_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            self._create(fixture)
            fixture["source_anchor_manifest_path"].write_text(
                fixture["source_anchor_manifest_path"].read_text(encoding="utf-8")
                + " ",
                encoding="utf-8",
            )

            result = self._evaluate(fixture)
            self.assertEqual(result["status"], "BLOCKED")
            self.assertIn(
                "AV_SOURCE_ANCHOR_MANIFEST_INVALID", result["reason_codes"]
            )

    def test_source_registration_created_after_proxy_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            self._create(fixture)
            source_manifest = json.loads(
                fixture["source_anchor_manifest_path"].read_text(encoding="utf-8")
            )
            subject = json.loads(
                fixture["subject_manifest_path"].read_text(encoding="utf-8")
            )
            source_manifest["created_utc"] = (
                datetime.fromisoformat(subject["created_utc"])
                + timedelta(seconds=1)
            ).isoformat()
            self._write_json(
                fixture["source_anchor_manifest_path"], source_manifest
            )
            subject["source_anchor_manifest"]["sha256"] = sha256(
                fixture["source_anchor_manifest_path"]
            )
            self._write_json(fixture["subject_manifest_path"], subject)
            manifest = json.loads(
                fixture["output_path"].read_text(encoding="utf-8")
            )
            subject_sha = sha256(fixture["subject_manifest_path"])
            manifest["bindings"]["subject_manifest"]["sha256"] = subject_sha
            manifest["bindings"]["timeline_manifest"]["sha256"] = subject_sha
            source_record = manifest["bindings"]["source_anchor_manifest"]
            source_record["sha256"] = sha256(
                fixture["source_anchor_manifest_path"]
            )
            registration = manifest["source_anchor_registration"]
            registration["sha256"] = source_record["sha256"]
            registration["created_utc"] = source_manifest["created_utc"]
            self._write_json(fixture["output_path"], manifest)

            result = self._evaluate(fixture)
            self.assertEqual(result["status"], "BLOCKED")
            self.assertIn(
                "AV_SOURCE_ANCHOR_CREATED_AFTER_PROXY", result["reason_codes"]
            )

    def test_formula_derived_source_event_is_rejected_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            source_observation = json.loads(
                fixture["source_observation_path"].read_text(encoding="utf-8")
            )
            source_observation["anchors"][0]["event"]["method"] = (
                "derived from normalized frame formula"
            )
            source_observation["anchors"][0]["derived_from_formula"] = True
            self._write_json(
                fixture["source_observation_path"], source_observation
            )

            with self.assertRaises(audio.SourceAnchorEvidenceError) as caught:
                self._create(fixture)
            self.assertEqual(
                caught.exception.reason_code,
                "AV_SOURCE_ANCHOR_FORMULA_DERIVED",
            )
            self.assertFalse(fixture["source_anchor_manifest_path"].exists())
            self.assertFalse(fixture["output_path"].exists())

    def test_none_manifest_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            result = audio.evaluate_bound_content_anchor_manifest(
                None,
                subject_manifest_path=fixture["subject_manifest_path"],
                timeline_manifest_path=fixture["timeline_manifest_path"],
                source_media_path=fixture["source_media_path"],
                proxy_media_path=fixture["proxy_media_path"],
                source_oracle_path=fixture["source_oracle_path"],
                proxy_oracle_path=fixture["proxy_oracle_path"],
                source_oracle=fixture["source_oracle"],
                proxy_oracle=fixture["proxy_oracle"],
                source_audio=fixture["source_audio"],
                proxy_audio=fixture["proxy_audio"],
                expected_scope="full",
                max_error_seconds=0.010,
            )

            self.assertEqual(result["status"], "BLOCKED")
            self.assertIn("AV_CONTENT_ANCHORS_NOT_PROVIDED", result["reason_codes"])

    def test_observation_input_hash_tampering_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            self._create(fixture)
            fixture["observation_path"].write_text("tampered\n", encoding="utf-8")

            self.assertEqual(self._evaluate(fixture)["status"], "BLOCKED")

    def test_each_observation_artifact_is_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            self._create(fixture)
            fixture["observation_artifacts"][3].write_text(
                "tampered artifact\n", encoding="utf-8"
            )

            self.assertEqual(self._evaluate(fixture)["status"], "BLOCKED")

    def test_fabricated_visual_with_updated_bindings_fails_reextraction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            self._create(fixture)
            window_path = fixture["observation_artifacts"][0]
            visual_path = fixture["observation_artifacts"][1]
            visual_path.write_bytes(self.PNG_CAPTURE + b"-fabricated")
            window = json.loads(window_path.read_text(encoding="utf-8"))
            window["visual_artifact"].update(
                {
                    "sha256": sha256(visual_path),
                    "size": visual_path.stat().st_size,
                }
            )
            self._write_json(window_path, window)
            observation = json.loads(
                fixture["observation_path"].read_text(encoding="utf-8")
            )
            observation["anchors"][0]["source_observation"]["sha256"] = sha256(
                window_path
            )
            self._write_json(fixture["observation_path"], observation)
            manifest = json.loads(
                fixture["output_path"].read_text(encoding="utf-8")
            )
            manifest["bindings"]["observation_input"]["sha256"] = sha256(
                fixture["observation_path"]
            )
            self._write_json(fixture["output_path"], manifest)

            result = self._evaluate(fixture)
            self.assertEqual(result["status"], "BLOCKED")
            self.assertIn("AV_CONTENT_ANCHOR_MANIFEST_INVALID", result["reason_codes"])

    def test_capture_command_tampering_is_blocked_with_current_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            self._create(fixture)
            window_path = fixture["observation_artifacts"][0]
            window = json.loads(window_path.read_text(encoding="utf-8"))
            window["capture"]["commands"]["video"].append("tampered")
            self._write_json(window_path, window)
            observation = json.loads(
                fixture["observation_path"].read_text(encoding="utf-8")
            )
            observation["anchors"][0]["source_observation"]["sha256"] = sha256(
                window_path
            )
            self._write_json(fixture["observation_path"], observation)
            manifest = json.loads(
                fixture["output_path"].read_text(encoding="utf-8")
            )
            manifest["bindings"]["observation_input"]["sha256"] = sha256(
                fixture["observation_path"]
            )
            self._write_json(fixture["output_path"], manifest)

            result = self._evaluate(fixture)
            self.assertEqual(result["status"], "BLOCKED")
            self.assertIn("AV_CONTENT_ANCHOR_MANIFEST_INVALID", result["reason_codes"])

    def test_media_binding_tampering_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            self._create(fixture)
            fixture["proxy_media_path"].write_bytes(b"changed-proxy-media")

            self.assertEqual(self._evaluate(fixture)["status"], "BLOCKED")

    def test_oracle_binding_tampering_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            self._create(fixture)
            fixture["source_oracle_path"].write_text(
                fixture["source_oracle_path"].read_text(encoding="utf-8") + " ",
                encoding="utf-8",
            )

            self.assertEqual(self._evaluate(fixture)["status"], "BLOCKED")

    def test_tool_hash_tampering_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            self._create(fixture)
            manifest = json.loads(fixture["output_path"].read_text(encoding="utf-8"))
            tool_records = [
                value
                for value in manifest.get("tools", {}).values()
                if isinstance(value, dict) and "sha256" in value
            ]
            self.assertTrue(tool_records, "manifest must bind its tools")
            tool_records[-1]["sha256"] = "0" * 64
            self._write_json(fixture["output_path"], manifest)

            self.assertEqual(self._evaluate(fixture)["status"], "BLOCKED")

    def test_missing_required_zone_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            self._create(fixture)
            self._rewrite_observation(
                fixture, lambda value: value["anchors"].pop()
            )

            result = self._evaluate(fixture)
            self.assertEqual(result["status"], "BLOCKED")
            self.assertIn("AV_CONTENT_ANCHOR_ZONES_INCOMPLETE", result["reason_codes"])

    def test_creation_rejects_semantically_invalid_observation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            observation = json.loads(
                fixture["observation_path"].read_text(encoding="utf-8")
            )
            observation["anchors"].pop()
            self._write_json(fixture["observation_path"], observation)

            with self.assertRaises(audio.AudioEvidenceError):
                self._create(fixture)
            self.assertFalse(fixture["output_path"].exists())

    def test_duplicate_evidence_id_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            self._create(fixture)

            def duplicate_id(value: dict) -> None:
                value["anchors"][1]["evidence_id"] = value["anchors"][0][
                    "evidence_id"
                ]

            self._rewrite_observation(fixture, duplicate_id)
            self.assertEqual(self._evaluate(fixture)["status"], "BLOCKED")

    def test_terminal_guard_cannot_be_used_as_a_content_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            self._create(fixture)

            def use_guard(value: dict) -> None:
                anchor = value["anchors"][-1]
                anchor["proxy_frame_index"] = self.BUSINESS_FRAME_COUNT
                anchor["proxy_frame_checksum"] = f"{self.BUSINESS_FRAME_COUNT:08X}"

            self._rewrite_observation(fixture, use_guard)
            self.assertEqual(self._evaluate(fixture)["status"], "BLOCKED")

    def test_observed_frame_checksum_mismatch_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            self._create(fixture)

            def change_checksum(value: dict) -> None:
                value["anchors"][0]["source_frame_checksum"] = "DEADBEEF"

            self._rewrite_observation(fixture, change_checksum)
            self.assertEqual(self._evaluate(fixture)["status"], "BLOCKED")

    def test_pts_conflict_zone_must_reference_an_actual_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            self._create(fixture)

            def move_from_conflict(value: dict) -> None:
                anchor = value["anchors"][1]
                index = 8
                checksum = f"{index + 1:08X}"
                anchor["source_frame_index"] = index
                anchor["proxy_frame_index"] = index
                anchor["source_audio_sample"] = index * self.SAMPLE_RATE
                anchor["proxy_audio_sample"] = index * self.SAMPLE_RATE
                anchor["source_frame_checksum"] = checksum
                anchor["proxy_frame_checksum"] = checksum

            self._rewrite_observation(fixture, move_from_conflict)
            self.assertEqual(self._evaluate(fixture)["status"], "BLOCKED")

    def test_manifest_creation_is_write_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            self._create(fixture)

            with self.assertRaises(FileExistsError):
                self._create(fixture)

    def test_full_verify_requires_content_anchor_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            report = self._verify_full(
                fixture,
                content_anchor_manifest_path=None,
            )

            self.assertEqual(report["status"], "BLOCKED")
            self.assertEqual(report["av_sync"]["status"], "BLOCKED")
            self.assertIn(
                "AV_CONTENT_ANCHORS_NOT_PROVIDED", report["reason_codes"]
            )

    def test_full_verify_cli_accepts_content_anchor_manifest(self) -> None:
        args = audio._parser().parse_args(
            [
                "verify",
                "video.manifest.json",
                "--video-report",
                "video.verify.json",
                "--run-manifest",
                "audio.run.json",
                "--output",
                "audio.verify.json",
                "--content-anchor-manifest",
                "content-anchor.manifest.json",
            ]
        )

        self.assertEqual(
            args.content_anchor_manifest, Path("content-anchor.manifest.json")
        )

    def test_full_verify_blocks_tampered_content_anchor_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            self._create(fixture)
            fixture["observation_artifacts"][0].write_text(
                "tampered after manifest creation\n", encoding="utf-8"
            )

            report = self._verify_full(
                fixture,
                content_anchor_manifest_path=fixture["output_path"],
            )

            self.assertEqual(report["status"], "BLOCKED")
            self.assertIn(
                "AV_CONTENT_ANCHOR_MANIFEST_INVALID", report["reason_codes"]
            )

    def test_full_verify_treats_pts_identity_and_boundaries_as_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            fixture = self._fixture(Path(temp_value))
            # Keep bound PCM/sample evidence identical while perturbing only
            # the legacy decoded end-time diagnostic.
            fixture["proxy_audio"]["frames"]["end_time"] += 1.0
            self._create(fixture)

            report = self._verify_full(
                fixture,
                content_anchor_manifest_path=fixture["output_path"],
            )

            self.assertEqual(report["status"], "PASS")
            self.assertEqual(report["av_sync"]["status"], "PASS")
            self.assertEqual(
                report["av_sync"]["source_pts_identity"]["status"],
                "NOT_PRESERVED",
            )
            self.assertEqual(
                report["av_sync"]["timestamp_boundaries"][
                    "source_pts_relationship_status"
                ],
                "CHANGED",
            )
            self.assertIn(
                "SOURCE_PTS_RELATIVE_AV_BOUNDARY_CHANGED",
                report["av_sync"]["diagnostic_codes"],
            )
            self.assertNotIn(
                "SOURCE_PTS_IDENTITY_NOT_PRESERVED",
                report["reason_codes"],
            )
            self.assertIn(
                "SOURCE_PTS_IDENTITY_NOT_PRESERVED",
                report["diagnostic_codes"],
            )


if __name__ == "__main__":
    unittest.main()
