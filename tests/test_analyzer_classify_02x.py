"""0.2x 判据修复回归钉（09-24「开工」；与上游 v26.10.07 PR #12 同源修复）。

**缺陷**：0.2x 模板是「正在播放」图标，在 1x/2x 帧上也会高分。旧判据
「0.2x 分数过阈值即判 0.2x」在 1x/2x 分数**打平**（谁也没超过谁）且 0.2x
过线时，会把该帧误判成 0.2x（上游原话「会把 2 倍速判成 0.2 倍速」）。
**修复**：0.2x 必须是三个速度类里最高的那个（`s02 > max(x1s, x2s)`）。

判据是剪辑决策的输入——误判会触发错误的变速动作，故用网格穷举钉住
「与上游修复版 classify_gray 逐点一致」+ 缺陷场景回归。
"""
from __future__ import annotations

import unittest
from itertools import product
from unittest import mock

import numpy as np

import analyzer
from frame_types import (
    FRAME_TYPE_0_2X,
    FRAME_TYPE_1X,
    FRAME_TYPE_2X,
    FRAME_TYPE_NORMAL,
    FRAME_TYPE_PAUSE,
)

_T = 0.7
_CONFIGS = {
    "pause": ["pause"],
    "speed_1x": ["1x"],
    "speed_2x": ["2x"],
    "speed_0_2x": ["0.2x"],
}
_THRESHOLDS = {"pause": _T, "speed_1x": _T, "speed_2x": _T, "speed_0_2x": _T}


def _classify(pause: float, x1: float, x2: float, s02: float) -> int:
    """驱动生产 `_classify_gray`；分数经 `_get_best_score` 注入。"""
    scores = {"pause": pause, "1x": x1, "2x": x2, "0.2x": s02}

    def fake(gray, templates, proc_res):
        return scores[templates[0]]

    with mock.patch.object(analyzer, "_get_best_score", side_effect=fake):
        return analyzer._classify_gray(
            np.zeros((4, 4), np.uint8), _CONFIGS, _THRESHOLDS, (400, 225))


def _upstream_reference(pause: float, x1: float, x2: float, s02: float) -> int:
    """上游 v26.10.07 `matcher.classify_gray` 修复版语义（对照实现）。"""
    if pause >= _T:
        return FRAME_TYPE_PAUSE
    if x1 >= _T and x1 > x2:
        return FRAME_TYPE_1X
    if x2 >= _T and x2 > x1:
        return FRAME_TYPE_2X
    if s02 >= _T and s02 > max(x1, x2):
        return FRAME_TYPE_0_2X
    return FRAME_TYPE_NORMAL


class Classify02xTests(unittest.TestCase):
    def test_flat_speed_scores_not_misclassified_as_02x(self):
        # 缺陷现场：1x/2x 打平且 0.2x 过线。旧判据 → 0_2X（误），修复 → NORMAL
        assert _classify(pause=0.0, x1=0.8, x2=0.8, s02=0.75) == FRAME_TYPE_NORMAL

    def test_02x_requires_strictly_highest(self):
        # 1x/2x 打平（谁也没严格高过谁）时 0.2x 才有资格被考虑
        assert _classify(0.0, 0.8, 0.8, 0.9) == FRAME_TYPE_0_2X
        # 与速度类打平（s02 == x1 == x2）不算最高
        assert _classify(0.0, 0.9, 0.9, 0.9) == FRAME_TYPE_NORMAL
        # 差一个浮点尾数即成立（严格大于）
        assert _classify(0.0, 0.8, 0.8, 0.8 + 1e-9) == FRAME_TYPE_0_2X
        # 速度类之一过线且严格更高时，短路优先于 0.2x（优先级不变）
        assert _classify(0.0, 0.8, 0.85, 0.9) == FRAME_TYPE_2X

    def test_02x_when_speeds_below_threshold(self):
        # 速度类都不过线时，0.2x 只要过线且高于它们即可（无回归）
        assert _classify(0.0, 0.5, 0.4, 0.8) == FRAME_TYPE_0_2X

    def test_1x_2x_pause_paths_unchanged(self):
        # 短路优先级不变：pause > 1x > 2x > 0.2x
        assert _classify(0.9, 0.0, 0.0, 0.0) == FRAME_TYPE_PAUSE
        assert _classify(0.0, 0.9, 0.8, 0.95) == FRAME_TYPE_1X
        assert _classify(0.0, 0.8, 0.9, 0.95) == FRAME_TYPE_2X

    def test_missing_02x_templates_never_raise(self):
        # 模板目录缺失时 load_templates 给空表；0.2x 分支须安静跳过
        # （旧版 `config and score(...)` 短路，新版 `if config:` 等价）
        for empty in ([], None):
            cfg = {**_CONFIGS, "speed_0_2x": empty}
            calls = []

            def fake(gray, templates, proc_res, _calls=calls):
                _calls.append(templates[0])
                # pause 不过线；速度类都不过线 → 结果只能是 NORMAL
                return 0.0 if templates[0] == "pause" else 0.9

            with mock.patch.object(analyzer, "_get_best_score", side_effect=fake):
                state = analyzer._classify_gray(
                    np.zeros((4, 4), np.uint8), cfg, _THRESHOLDS, (400, 225))
            assert state == FRAME_TYPE_NORMAL
            assert calls == ["pause", "1x", "2x"], (
                "空 0.2x 配置不得调用打分（与旧版短路调用次数一致）")

    def test_grid_parity_with_upstream_fixed_reference(self):
        vals = (0.0, 0.69, 0.70, 0.71, 0.80, 0.90, 0.95)
        mismatches = []
        for x1, x2, s02 in product(vals, repeat=3):
            for pause in (0.0, 0.70, 0.9):
                got = _classify(pause, x1, x2, s02)
                want = _upstream_reference(pause, x1, x2, s02)
                if got != want:
                    mismatches.append((pause, x1, x2, s02, got, want))
        self.assertEqual(mismatches, [], f"与上游修复版不一致: {mismatches[:8]}")


if __name__ == "__main__":
    unittest.main()
