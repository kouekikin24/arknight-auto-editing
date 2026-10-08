"""_send_play 直通分档门单测（1c-②）：判定条件、spans 派生、失败回落。

09-13 门语义修订：①UI>3x 才进直通（旧门按「段倍率>3」判定会把 UI 1x 的
0.2x 业务段误送直通）；②碎段守卫——任一 span 呈现帧数 <60 回 Tier1
（换段税 T0：保留段中位 11 帧的项目在直通档 ~30fps）。
"""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace
import unittest
from unittest import mock

from frame_types import FRAME_TYPE_0_2X
from preview_engine import PreviewEngineError
from timeline_plan import TimelinePlan

from tests.test_preview_fps_osd import _bare_player

# 长段夹具：删 [2000,2100)，0.2x 业务段 [3000,4999]（闭区间）→ 四个 span
# 的呈现帧数 200/90/100/100，全部 ≥60（过碎段守卫）
_TOTAL = 6000
_CUT = (2000, 2100)
_BIZ_SEG = (3000, 4999)

_MEDIA = None


def _fake_media() -> "object":
    """最小真 MediaInfo（CertifiedEdlRequest __post_init__ 强类型校验）。"""
    import tempfile
    from fractions import Fraction
    from pathlib import Path

    import media_info
    from media_info import MediaInfo, VideoStreamInfo

    source = Path(tempfile.mkdtemp()) / "src.mp4"
    source.write_bytes(b"0" * 8)
    stream = VideoStreamInfo(
        index=0, codec_name="h264", codec_long_name=None, width=64, height=64,
        pixel_format="yuv420p", time_base=Fraction(1, 15360), start_time=None,
        duration=None, avg_frame_rate=Fraction(60, 1), r_frame_rate=Fraction(60, 1),
        frame_count=_TOTAL,
    )
    return MediaInfo(
        source_path=source, source_sha256=media_info._sha256_file(source),
        source_size=8, source_mtime_ns=0, format_name="mov",
        format_long_name=None, duration=Fraction(_TOTAL, 60), start_time=None,
        video_streams=(stream,), audio_streams=(), vfr_status="cfr",
    )


class _BoolVarLocal:
    def __init__(self, value: bool):
        self.value = bool(value)

    def get(self):
        return self.value


def _tier_player(speed: str = "10x", skim: bool = True, skip: bool = True,
                 has_cuts: bool = True, biz: bool = True,
                 cut: tuple[int, int] | None = _CUT):
    global _MEDIA
    if _MEDIA is None:
        _MEDIA = _fake_media()
    player = _bare_player()
    player.preview_speed_var = SimpleNamespace(get=lambda: speed)
    player._apply_pace_mode_to_io = mock.Mock()
    player._reject_play_request = mock.Mock()
    player.settings = SimpleNamespace(get_params=lambda: {
        'skim_preview': skim,
        'speedup_1x': False,
        'speedup_02': True,
        'speedup_02_factor': 10,
    })
    player.skip_trimmed = _BoolVarLocal(skip)
    player.total_frames = _TOTAL
    mask = [False] * _TOTAL
    if has_cuts and cut:
        mask[cut[0]:cut[1]] = [True] * (cut[1] - cut[0])
    player._build_timeline_plan = mock.Mock(
        return_value=TimelinePlan.from_delete_mask(mask)
    )
    segs = [(3000, 4999, FRAME_TYPE_0_2X)]
    player._speed_segs_snap = mock.Mock(return_value=segs if biz else [])
    player._canvas_wh = mock.Mock(return_value=(320, 180))
    player._fps_samples = deque()
    player.media_info = _MEDIA  # 非 None 即过门（真 MediaInfo 供请求构造）
    player._io.dthrough_ready = True
    return player


class DThroughTierGateTests(unittest.TestCase):
    def test_high_rate_routes_to_play_dthrough(self):
        player = _tier_player()
        self.assertTrue(player._send_play(1200))
        player._io.play_dthrough.assert_called_once()
        player._io.play.assert_not_called()
        kwargs = player._io.play_dthrough.call_args.kwargs
        # spans=认证 plan 保留段 × 业务段：UI 10x，0.2x 段 factor 10 = 100
        # → 解码预算封顶 20（09-16 真机 T0 回撤：帽 24 实测卡顿/半秒冻结）；
        # 闭区间 [3000,4999] → 尾切点 5000
        rates = [(s.start_frame, s.end_frame, s.rate) for s in kwargs["spans"]]
        self.assertEqual(rates, [
            (0, 2000, 10), (2100, 3000, 10), (3000, 5000, 20), (5000, 6000, 10),
        ])
        self.assertEqual(kwargs["fps"], 60.0)
        self.assertEqual(kwargs["start_source_frame"], 1200)
        # 直通接管=原画质 rawpush：skim 代理必须清场
        player._io.clear_skim_blob.assert_called_once()
        request = player._io.play_dthrough.call_args.args[0]
        self.assertEqual(request.timeline_plan.fingerprint,
                         player._build_timeline_plan.return_value.fingerprint)

    def test_ui_2x_routes_dthrough_with_true_biz_pacing(self):
        # 09-13 午：≤3x→Tier1 旧规废除（碎段税已根除+owner 目标=预览节奏
        # 与导出一致）——UI 2x 走直通，0.2x 段 rate=2×10=20（无帽触发）
        player = _tier_player(speed="2x", biz=True)
        self.assertTrue(player._send_play(10))
        player._io.play_dthrough.assert_called_once()
        player._io.play.assert_not_called()
        rates = [(s.start_frame, s.end_frame, s.rate)
                 for s in player._io.play_dthrough.call_args.kwargs["spans"]]
        self.assertEqual(rates, [
            (0, 2000, 2), (2100, 3000, 2), (3000, 5000, 20), (5000, 6000, 2),
        ])

    def test_ui_2x_without_biz_routes_dthrough(self):
        # 同上：无业务段也走直通（rate=UI）
        player = _tier_player(speed="2x", biz=False)
        self.assertTrue(player._send_play(10))
        player._io.play_dthrough.assert_called_once()
        player._io.play.assert_not_called()

    def test_small_island_routes_dthrough_after_continuous_fix(self):
        # 碎段守卫放宽（09-13 立项：小间隙连续解码把换段税对连续边界归零）：
        # 删 [100,2900) → 岛 (0,100) 在 UI 10x 有 10 个呈现帧（≥2）→ 走直通
        player = _tier_player(cut=(100, 2900))
        self.assertTrue(player._send_play(10))
        player._io.play_dthrough.assert_called_once()
        player._io.play.assert_not_called()

    def test_single_presented_frame_island_routes_dthrough(self):
        # 守卫已移除（09-13 真机 T0：0.2x 岛 cap32 下呈现 0.34 帧，旧口径把
        # 真实项目整单打回 Tier1+skim）：单呈现帧岛也走直通
        player = _tier_player(cut=(2, 2900))
        self.assertTrue(player._send_play(10))
        player._io.play_dthrough.assert_called_once()
        player._io.play.assert_not_called()

    def test_tail_start_wraps_only_after_natural_eof(self):
        # P0-A（09-16）：片段停在末段最后呈现帧时再播=空流假播放。
        # 判据演进：v1「start≥末段末帧」→ owner 实测「暂停后点播放被拉回开头」
        # （停在片尾附近即误触发）；v2 加 not is_playing → 与 toggle_play 先置位
        # 矛盾、永久失效。现用 **EOF 标志**：只有自然播完才回绕。
        player = _tier_player()
        last_kept = player._build_timeline_plan.return_value.kept_ranges[-1][1]
        last_presentable = max(player._build_timeline_plan.return_value
                              .kept_ranges[-1][0], last_kept - 1)

        # (a) 未播完 + 停在末尾帧（=暂停/恢复场景）→ **不回绕**
        player._play_ended_at_eof = False
        player._send_play(last_presentable)
        self.assertEqual(
            player._io.play_dthrough.call_args.kwargs["start_source_frame"],
            last_presentable)
        player._io.play_dthrough.reset_mock()

        # (b) 自然播完（EOF）+ 停在末尾帧 → 回绕到 0
        player._play_ended_at_eof = True
        player._send_play(last_presentable)
        self.assertEqual(
            player._io.play_dthrough.call_args.kwargs["start_source_frame"], 0)
        self.assertFalse(player._play_ended_at_eof)   # 标志已被消费
        player._io.play_dthrough.reset_mock()

        # (c) 中段起点不受影响
        player._send_play(1200)
        self.assertEqual(
            player._io.play_dthrough.call_args.kwargs["start_source_frame"], 1200)

    def test_user_seek_clears_eof_rewind_flag(self):
        # 用户主动定位后，下一次播放不得被当作「重播」回绕
        # （桩无 timeline 属性，直接验证标志清除语义本身）
        player = _tier_player()
        player._play_ended_at_eof = True
        # _seek 是公开入口（内部第一步即清标志），桩补 timeline 薄壳
        player.timeline = SimpleNamespace(current_frame_idx=0, mark_dirty=lambda: None)
        player._io.dthrough_active = False    # Mock 真值劫持：显式关（走源路径）
        player._seek(1200)
        self.assertFalse(player._play_ended_at_eof)

    def test_toggle_off_stays_tier1(self):
        player = _tier_player(skim=False)
        self.assertTrue(player._send_play(10))
        player._io.play_dthrough.assert_not_called()
        player._io.play.assert_called_once()

    def test_skip_trimmed_off_stays_tier1(self):
        # 跳过裁剪区关闭=整源播放：源模式 4x 是既有能力，不偷换语义
        player = _tier_player(skip=False)
        self.assertTrue(player._send_play(10))
        player._io.play_dthrough.assert_not_called()
        player._io.play.assert_called_once()

    def test_no_deleted_ranges_stays_tier1(self):
        player = _tier_player(has_cuts=False)
        self.assertTrue(player._send_play(10))
        player._io.play_dthrough.assert_not_called()
        player._io.play.assert_called_once()

    def test_dthrough_failure_falls_back_to_tier1(self):
        player = _tier_player()
        player._io.play_dthrough = mock.Mock(
            side_effect=PreviewEngineError("DTHROUGH_NOT_READY", "x"))
        self.assertTrue(player._send_play(10))
        player._io.play_dthrough.assert_called_once()
        player._io.play.assert_called_once()

    def test_engine_not_ready_stays_tier1(self):
        player = _tier_player()
        player._io.dthrough_ready = False
        self.assertTrue(player._send_play(10))
        player._io.play_dthrough.assert_not_called()
        player._io.play.assert_called_once()

    def test_slow_play_takes_dthrough_without_skim_build(self):
        # 09-13 午：1x/2x 走直通（不建代理不清场逻辑照旧——直通接管即清）
        player = _tier_player(speed="2x", biz=False)
        player._maybe_schedule_skim_build = mock.Mock()
        self.assertTrue(player._send_play(10))
        player._maybe_schedule_skim_build.assert_not_called()
        player._io.play_dthrough.assert_called_once()
        player._io.clear_skim_blob.assert_called_once()

    def test_high_speed_without_dthrough_still_schedules_skim_build(self):
        # >3x 且直通失格（engine 未就绪）→ Tier2 代理仍是有效兜底
        player = _tier_player()
        player._io.dthrough_ready = False
        player._maybe_schedule_skim_build = mock.Mock()
        self.assertTrue(player._send_play(10))
        player._maybe_schedule_skim_build.assert_called_once()
        player._io.play.assert_called_once()


if __name__ == "__main__":
    unittest.main()
