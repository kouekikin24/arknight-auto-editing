from __future__ import annotations

import unittest
from unittest import mock

from frame_types import FRAME_TYPE_0_2X
from preview_player import VideoPreviewPlayer


class _Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class _Settings:
    def __init__(self, **params):
        self.params = params

    def get_params(self):
        return dict(self.params)


class _IO:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def snapshot_perf(self):
        return dict(self.snapshot)


class PreviewPacingTests(unittest.TestCase):
    @staticmethod
    def _player(speed: str, *, params=None, segments=None):
        player = VideoPreviewPlayer.__new__(VideoPreviewPlayer)
        player.preview_speed_var = _Value(speed)
        player.settings = _Settings(**(params or {}))
        player.speed_segments = list(segments or [])
        return player

    def test_expected_rate_matches_slow_clock_and_fast_step_policy(self) -> None:
        expected = {
            "0.1x": 0.1,
            "0.25x": 0.25,
            "0.5x": 0.5,
            "1x": 1.0,
            "2x": 2.0,
            "4x": 3.0,
        }
        for speed, rate in expected.items():
            with self.subTest(speed=speed):
                player = self._player(speed)
                measured, _raw, _capped, _note = player._calib_expected_rate(
                    0, ignore_biz=True
                )
                self.assertAlmostEqual(measured, rate)

    def test_slow_clock_combines_with_capped_business_step(self) -> None:
        player = self._player(
            "0.5x",
            params={"speedup_02": True, "speedup_02_factor": 10},
            segments=[{"start": 0, "end": 20, "type": FRAME_TYPE_0_2X}],
        )
        rate, raw, capped, _note = player._calib_expected_rate(
            10, ignore_biz=False
        )
        self.assertEqual((raw, capped), (10, 3))
        self.assertAlmostEqual(rate, 1.5)

    def test_measurement_prefers_io_play_steps_and_excludes_trim_jumps(self) -> None:
        player = self._player("1x")
        player.current_frame_idx = 1000
        player._io = _IO(
            {
                "wall_s": 10.0,
                "rate_play_frames": 150,
                "rate_trim_frames": 900,
            }
        )
        with mock.patch("preview_player.time.monotonic", return_value=99.0):
            result = player._playback_rate_measurement(1.0, 10)
        self.assertEqual(result, (10.0, 150, 900, "play-steps", 1000))

    def test_measurement_falls_back_to_forward_ui_delta(self) -> None:
        player = self._player("1x")
        player.current_frame_idx = 70
        player._io = None
        with mock.patch("preview_player.time.monotonic", return_value=5.0):
            result = player._playback_rate_measurement(1.0, 10)
        self.assertEqual(result, (4.0, 60, 0, "ui-frames", 70))

        player.current_frame_idx = 5
        with mock.patch("preview_player.time.monotonic", return_value=5.0):
            self.assertIsNone(player._playback_rate_measurement(1.0, 10))


if __name__ == "__main__":
    unittest.main()
