from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "scripts" / "verify_proxy_audio_av.py"
SPEC = importlib.util.spec_from_file_location("verify_proxy_audio_av_nonzero", MODULE_PATH)
assert SPEC and SPEC.loader
audio = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audio)


class NonzeroContentAnchorMappingTests(unittest.TestCase):
    SOURCE_START = 17
    FRAME_COUNT = 40
    SOURCE_END = SOURCE_START + FRAME_COUNT

    def _rows(self, *, repeated_checksum: bool = False) -> tuple[list[dict], list[dict]]:
        source_rows = [
            {
                "n": index,
                "pts": index * 100,
                "duration": 100,
                "checksum": "SAME" if repeated_checksum else f"SRC-{index:03d}",
            }
            for index in range(60)
        ]
        # Keep one real conflict inside the selected source window.
        source_rows[20]["pts"] = source_rows[19]["pts"]
        proxy_rows = [
            {
                "n": local_index,
                "pts": local_index * 100,
                "duration": 100,
                "checksum": source_rows[self.SOURCE_START + local_index]["checksum"],
            }
            for local_index in range(self.FRAME_COUNT)
        ]
        return source_rows, proxy_rows

    def _anchors(self, source_rows: list[dict]) -> list[dict]:
        source_indices = (17, 20, 37, 56)
        anchors = []
        for zone, source_index in zip(audio.REQUIRED_CONTENT_ANCHOR_ZONES, source_indices):
            proxy_index = source_index - self.SOURCE_START
            anchors.append(
                {
                    "zone": zone,
                    "evidence_id": f"nonzero-{zone}",
                    "source_frame_index": source_index,
                    "proxy_frame_index": proxy_index,
                    "source_audio_sample": source_index * 1000,
                    "proxy_audio_sample": proxy_index * 1000,
                    "source_frame_checksum": source_rows[source_index]["checksum"],
                    "proxy_frame_checksum": source_rows[source_index]["checksum"],
                    "landmark": f"observed nonzero event for {zone}",
                    "event": {
                        "observed": True,
                        "method": "manual independent content observation",
                        "description": f"visible and audible event for {zone}",
                        "video": {
                            "observed": True,
                            "description": f"visible event for {zone}",
                        },
                        "audio": {
                            "observed": True,
                            "description": f"audible event for {zone}",
                        },
                    },
                    "source_observation": {"path": f"source-{zone}.json"},
                    "proxy_observation": {"path": f"proxy-{zone}.json"},
                }
            )
        return anchors

    def _write_observation(self, path: Path, value: dict) -> dict:
        path.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        return audio._file_record(path)

    def _observer(self) -> dict:
        return audio._file_record(audio.CONTENT_ANCHOR_CAPTURE_TOOL)

    def _audio(self) -> dict:
        return {
            "status": "PASS",
            "reason_codes": [],
            "stream_present": True,
            "codec": "pcm_s16le",
            "format": {
                "sample_rate": 1000,
                "channels": 1,
                "channel_layout": "mono",
            },
            "frames": {
                "count": 1,
                "total_samples": 60000,
                "start_pts": 0,
                "end_pts_exclusive": 60000,
                "pts_table": [
                    {
                        "n": 0,
                        "pts": 0,
                        "pts_time": 0.0,
                        "nb_samples": 60000,
                        "rate": 1000,
                        "channels": 1,
                        "chlayout": "mono",
                        "checksum": "AUDIO",
                    }
                ],
            },
            "continuity": {"status": "PASS"},
            "format_validation": {"status": "PASS"},
            "command": ["ffmpeg", "source"],
        }

    def test_source_anchor_api_records_exact_nonzero_source_domain(self) -> None:
        source_rows, _ = self._rows()
        paired_anchors = self._anchors(source_rows)
        source_observation = {
            "schema_version": audio.SOURCE_CONTENT_ANCHOR_SCHEMA_VERSION,
            "kind": audio.SOURCE_CONTENT_ANCHOR_OBSERVATION_KIND,
            "scope": "prefix",
            "source_frame_domain": [self.SOURCE_START, self.SOURCE_END],
            "observer": self._observer(),
            "method": "manual source-only event registration",
            "audio_clock": {
                "source_stream": "0:a:0",
                "sample_index_basis": "decoded_ashowinfo_pts",
                "source_sample_rate": 1000,
            },
            "anchors": [
                audio._source_anchor_projection(value) for value in paired_anchors
            ],
        }
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            source = temp / "source.mp4"
            source.write_bytes(b"source-media")
            source_record = audio._file_record(source, include_size=True)
            oracle = {
                "schema_version": audio.frame_oracle.SCHEMA_VERSION,
                "kind": "mpv_phase0_frame_pts_oracle",
                "video": source_record,
                "ffmpeg": {"returncode": 0},
                "pts_table": source_rows,
            }
            oracle_path = temp / "source.oracle.json"
            oracle_path.write_text(json.dumps(oracle), encoding="utf-8")
            observation_path = temp / "source-observation.json"
            self._write_observation(observation_path, source_observation)
            ffmpeg = temp / "ffmpeg.exe"
            ffmpeg.write_bytes(b"ffmpeg")
            output = temp / "source-anchor.manifest.json"
            ffmpeg_record = {
                **audio._file_record(ffmpeg),
                "version_line": "ffmpeg test",
                "library_lines": [],
            }
            with (
                mock.patch.object(audio, "decode_audio", return_value=self._audio()),
                mock.patch.object(
                    audio,
                    "_load_content_anchor_window",
                    return_value={"status": "BOUND"},
                ),
                mock.patch.object(
                    audio, "ffmpeg_version_record", return_value=ffmpeg_record
                ),
            ):
                manifest = audio.create_source_content_anchor_manifest(
                    source,
                    oracle_path,
                    observation_path,
                    output,
                    ffmpeg,
                    scope="prefix",
                    source_start_frame=self.SOURCE_START,
                    business_frame_count=self.FRAME_COUNT,
                )

        self.assertEqual(manifest["source"]["business_frame_start"], 17)
        self.assertEqual(manifest["source"]["business_frame_count"], 40)
        self.assertEqual(manifest["source"]["source_frame_domain"], [17, 57])

    def test_timeline_snapshot_rederives_nonzero_mapping(self) -> None:
        mapping = audio.proxy_verifier._frame_mapping(
            self.SOURCE_START, self.FRAME_COUNT
        )
        manifest = {
            "source": {
                "scope": "prefix",
                "business_frame_start": self.SOURCE_START,
                "business_frame_count": self.FRAME_COUNT,
                "source_frame_domain": mapping["source_frame_domain"],
                "proxy_frame_domain": mapping["proxy_frame_domain"],
                "frame_mapping": mapping,
            },
            "normalized_timeline": {
                "time_base": {"numerator": 1, "denominator": 100},
                "duration_ticks": 100,
                "business_pts_start": 0,
                "business_pts_end_exclusive": self.FRAME_COUNT * 100,
                "source_frame_start": self.SOURCE_START,
                "source_frame_end_exclusive": self.SOURCE_END,
                "source_frame_domain": mapping["source_frame_domain"],
                "proxy_frame_domain": mapping["proxy_frame_domain"],
                "frame_mapping": mapping,
            },
        }

        snapshot = audio._timeline_snapshot(manifest)

        self.assertEqual(snapshot["source_frame_domain"], [17, 57])
        self.assertEqual(snapshot["proxy_frame_domain"], [0, 40])
        self.assertEqual(snapshot["frame_mapping"], mapping)
        manifest["normalized_timeline"]["frame_mapping"] = (
            audio.proxy_verifier._frame_mapping(16, self.FRAME_COUNT)
        )
        with self.assertRaises(audio.AudioEvidenceError):
            audio._timeline_snapshot(manifest)

    def test_source_and_paired_observations_use_absolute_to_local_mapping(self) -> None:
        source_rows, proxy_rows = self._rows()
        paired_anchors = self._anchors(source_rows)
        source_anchors = [audio._source_anchor_projection(value) for value in paired_anchors]
        source_observation = {
            "schema_version": audio.SOURCE_CONTENT_ANCHOR_SCHEMA_VERSION,
            "kind": audio.SOURCE_CONTENT_ANCHOR_OBSERVATION_KIND,
            "scope": "prefix",
            "source_frame_domain": [self.SOURCE_START, self.SOURCE_END],
            "observer": self._observer(),
            "method": "manual source-only event registration",
            "audio_clock": {
                "source_stream": "0:a:0",
                "sample_index_basis": "decoded_ashowinfo_pts",
                "source_sample_rate": 1000,
            },
            "anchors": source_anchors,
        }
        paired_observation = {
            "schema_version": audio.CONTENT_ANCHOR_SCHEMA_VERSION,
            "kind": audio.CONTENT_ANCHOR_OBSERVATION_KIND,
            "scope": "prefix",
            "observer": self._observer(),
            "method": "manual source/proxy event comparison",
            "audio_clock": {
                "source_stream": "0:a:0",
                "proxy_stream": "0:a:0",
                "sample_index_basis": "decoded_ashowinfo_pts",
                "source_sample_rate": 1000,
                "proxy_sample_rate": 1000,
            },
            "anchors": paired_anchors,
        }
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            source_record = self._write_observation(
                temp / "source-observation.json", source_observation
            )
            paired_record = self._write_observation(
                temp / "paired-observation.json", paired_observation
            )
            with mock.patch.object(
                audio,
                "_load_content_anchor_window",
                return_value={"status": "BOUND"},
            ):
                loaded_source = audio._load_source_content_anchor_observations(
                    source_record,
                    scope="prefix",
                    source_start_frame=self.SOURCE_START,
                    frame_count=self.FRAME_COUNT,
                    source_rows=source_rows,
                    source_audio=self._audio(),
                    source_media_path=temp / "source.mp4",
                    source_oracle_path=temp / "source.oracle.json",
                    expected_ffmpeg_path=temp / "ffmpeg.exe",
                )
                loaded_pair = audio._load_content_anchor_observations(
                    paired_record,
                    scope="prefix",
                    source_start_frame=self.SOURCE_START,
                    frame_count=self.FRAME_COUNT,
                    source_rows=source_rows,
                    proxy_rows=proxy_rows,
                    source_audio=self._audio(),
                    proxy_audio=self._audio(),
                    source_media_path=temp / "source.mp4",
                    proxy_media_path=temp / "proxy.mp4",
                    source_oracle_path=temp / "source.oracle.json",
                    proxy_oracle_path=temp / "proxy.oracle.json",
                    expected_ffmpeg_path=temp / "ffmpeg.exe",
                    registered_source_anchors=loaded_source["anchors"],
                )

        self.assertEqual(loaded_pair["semantic_reason_codes"], [])
        self.assertEqual(
            [value["proxy_frame_index"] for value in loaded_pair["anchors"]],
            [0, 3, 20, 39],
        )

    def test_evaluator_blocks_wrong_mapping_even_with_repeated_checksums(self) -> None:
        source_rows, proxy_rows = self._rows(repeated_checksum=True)
        anchors = self._anchors(source_rows)
        result = audio.evaluate_content_anchor_evidence(
            anchors,
            source_rows,
            proxy_rows,
            business_frame_count=self.FRAME_COUNT,
            source_start_frame=self.SOURCE_START,
            duration_ticks=100,
            time_base_numerator=1,
            time_base_denominator=100,
            audio_sample_rate=1000,
            source_audio_start_sample=0,
            source_audio_end_sample_exclusive=60000,
            proxy_audio_start_sample=0,
            proxy_audio_end_sample_exclusive=40000,
        )
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(all(value["error_seconds"] == 0 for value in result["anchors"]))

        anchors[0]["proxy_frame_index"] = 1
        blocked = audio.evaluate_content_anchor_evidence(
            anchors,
            source_rows,
            proxy_rows,
            business_frame_count=self.FRAME_COUNT,
            source_start_frame=self.SOURCE_START,
            duration_ticks=100,
            time_base_numerator=1,
            time_base_denominator=100,
            audio_sample_rate=1000,
            source_audio_start_sample=0,
            source_audio_end_sample_exclusive=60000,
            proxy_audio_start_sample=0,
            proxy_audio_end_sample_exclusive=40000,
        )
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertIn(
            "AV_CONTENT_ANCHOR_FRAME_MAPPING_INVALID", blocked["reason_codes"]
        )

    def test_source_only_rejects_camel_case_proxy_field_but_allows_path_value(self) -> None:
        audio._assert_source_only_payload(
            {"tools": {"verifier": {"path": "verify_proxy_audio_av.py"}}}
        )
        with self.assertRaises(audio.SourceAnchorEvidenceError):
            audio._assert_source_only_payload({"proxyFrameIndex": 0})


if __name__ == "__main__":
    unittest.main()
