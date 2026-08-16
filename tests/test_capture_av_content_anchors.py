from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]


def load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


generate = load_script(
    "generate_av_sync_fixtures_capture_test",
    REPO / "scripts" / "generate_av_sync_fixtures.py",
)
capture = load_script(
    "capture_av_content_anchors_test",
    REPO / "scripts" / "capture_av_content_anchors.py",
)


class RealContentAnchorCaptureTests(unittest.TestCase):
    def test_real_capture_is_reproducible_and_rejects_rebound_tampering(self) -> None:
        try:
            ffmpeg_path = generate.video_fixture.resolve_ffmpeg()
        except FileNotFoundError as exc:
            self.skipTest(str(exc))

        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            fixture = generate.generate_fixture(temp / "fixture", ffmpeg=ffmpeg_path)
            media_path = Path(fixture["video"])
            ffmpeg = capture.frame_oracle.FfmpegExecutable(ffmpeg_path, "test")
            oracle, _oracle_exit = capture.frame_oracle.probe_video(media_path, ffmpeg)
            oracle_path = temp / "source.oracle.json"
            oracle_path.write_text(
                json.dumps(
                    oracle,
                    ensure_ascii=False,
                    allow_nan=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            visual_path = temp / "anchor.png"
            audio_path = temp / "anchor.wav"
            window_path = temp / "anchor.window.json"
            frame_index = 2
            audio_sample = generate.PULSE_RANGES_SAMPLES[0][0]

            capture.capture_window(
                media_path,
                oracle_path,
                visual_path,
                audio_path,
                window_path,
                ffmpeg,
                side="source",
                scope="full",
                frame_index=frame_index,
                audio_sample=audio_sample,
                window_radius_samples=512,
            )
            audio = capture.verifier.decode_audio(media_path, ffmpeg_path)
            checksum = oracle["pts_table"][frame_index]["checksum"]

            def load_window() -> dict:
                return capture.verifier._load_content_anchor_window(
                    capture.verifier._file_record(window_path),
                    side="source",
                    scope="full",
                    media_path=media_path,
                    oracle_path=oracle_path,
                    frame_index=frame_index,
                    frame_checksum=checksum,
                    audio_sample=audio_sample,
                    audio=audio,
                    observer_tool_path=capture.verifier.CONTENT_ANCHOR_CAPTURE_TOOL,
                    expected_ffmpeg_path=ffmpeg_path,
                )

            verified = load_window()
            self.assertEqual(
                verified["independent_reextraction"]["status"], "PASS"
            )

            visual_path.write_bytes(visual_path.read_bytes() + b"tampered")
            window = json.loads(window_path.read_text(encoding="utf-8"))
            window["visual_artifact"]["sha256"] = capture.verifier.sha256_file(
                visual_path
            )
            window["visual_artifact"]["size"] = visual_path.stat().st_size
            window_path.write_text(
                json.dumps(
                    window,
                    ensure_ascii=False,
                    allow_nan=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                capture.verifier.AudioEvidenceError,
                "differs from independent re-extraction",
            ):
                load_window()

    def test_publish_race_preserves_competing_output_and_cleans_owned_temps(self) -> None:
        try:
            ffmpeg_path = generate.video_fixture.resolve_ffmpeg()
        except FileNotFoundError as exc:
            self.skipTest(str(exc))

        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            fixture = generate.generate_fixture(temp / "fixture", ffmpeg=ffmpeg_path)
            media_path = Path(fixture["video"])
            ffmpeg = capture.frame_oracle.FfmpegExecutable(ffmpeg_path, "test")
            oracle, _oracle_exit = capture.frame_oracle.probe_video(media_path, ffmpeg)
            oracle_path = temp / "source.oracle.json"
            oracle_path.write_text(
                json.dumps(oracle, allow_nan=False), encoding="utf-8"
            )
            visual_path = temp / "race.png"
            audio_path = temp / "race.wav"
            window_path = temp / "race.window.json"

            def run_then_compete(_command: list[str], output: Path, name: str) -> None:
                output.write_bytes(
                    b"\x89PNG\r\n\x1a\nREAL-PNG"
                    if output.suffix.lower() == ".png"
                    else b"RIFF\x10\x00\x00\x00WAVEREAL-WAV"
                )
                if name == "audio window":
                    visual_path.write_bytes(b"competing-output")

            with mock.patch.object(
                capture, "_run_capture", side_effect=run_then_compete
            ):
                with self.assertRaises(FileExistsError):
                    capture.capture_window(
                        media_path,
                        oracle_path,
                        visual_path,
                        audio_path,
                        window_path,
                        ffmpeg,
                        side="source",
                        scope="full",
                        frame_index=2,
                        audio_sample=generate.PULSE_RANGES_SAMPLES[0][0],
                        window_radius_samples=512,
                    )

            self.assertEqual(visual_path.read_bytes(), b"competing-output")
            self.assertFalse(audio_path.exists())
            self.assertFalse(window_path.exists())
            self.assertEqual(list(temp.glob(".race.*.capture.*")), [])


if __name__ == "__main__":
    unittest.main()
