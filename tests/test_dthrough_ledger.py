"""dthrough_ledger 单测（一期 1c-①）：名义/实测双轴、VFR 精化、rebase。

名义轴必须逐位镜像 dsvc locate（floor((b-a)*fps) → ceil(n/rate)）；
实测轴必须用 per_span 实测数（3.mp4 教训：名义 520 vs 实测 413）。
"""

import pytest

from dthrough_ledger import DThroughLedger, LedgerSpan, coerce_span, derive_spans
from frame_types import FRAME_TYPE_0_2X, FRAME_TYPE_1X

FPS = 60.0


def _fs(ranges):
    """[(帧起, 帧止, rate)] → LedgerSpan 列表（帧形态测试助手）。"""
    return [LedgerSpan(s, e, r, s / FPS, e / FPS) for s, e, r in ranges]


def _led(ranges, fps=FPS):
    return DThroughLedger(_fs(ranges), fps=fps)


class TestDeriveSpans:
    def test_intersects_business_segments_with_closed_interval(self):
        # 1X 业务段 [50,80] 闭区间：speedup 后 50..80 翻倍 → 尾切点 = 81
        spans = derive_spans(
            [(0, 100), (200, 300)],
            [(50, 80, FRAME_TYPE_1X)],
            speedup_1x=True, speedup_02=False, factor_02=10,
            ui_rate=10.0, fps=60.0,
        )
        assert [(s.start_frame, s.end_frame, s.rate) for s in spans] == [
            (0, 50, 10), (50, 81, 20), (81, 100, 10), (200, 300, 10),
        ]

    def test_02x_segment_uses_factor_when_speedup_on(self):
        spans = derive_spans(
            [(0, 100)], [(10, 20, FRAME_TYPE_0_2X)],
            speedup_1x=False, speedup_02=True, factor_02=10,
            ui_rate=4.0, fps=60.0,
        )
        assert [(s.start_frame, s.end_frame, s.rate) for s in spans] == [
            (0, 10, 4), (10, 21, 40), (21, 100, 4),
        ]

    def test_rate_floor_at_one(self):
        spans = derive_spans([(0, 10)], None, speedup_1x=False, speedup_02=False,
                             factor_02=1, ui_rate=0.5, fps=60.0)
        assert spans[0].rate == 1

    def test_first_matching_segment_wins_on_overlap(self):
        # 闭区间重叠时按列表序首个命中（上游 _biz_speed_step parity）
        spans = derive_spans(
            [(0, 100)],
            [(10, 30, FRAME_TYPE_1X), (20, 40, FRAME_TYPE_0_2X)],
            speedup_1x=True, speedup_02=True, factor_02=10,
            ui_rate=2.0, fps=60.0,
        )
        mid = [s for s in spans if s.start_frame == 20 and s.end_frame == 31]
        assert mid and mid[0].rate == 4  # 1X→step2（首个命中），非 0.2X→step10

    def test_invalid_fps_raises(self):
        with pytest.raises(ValueError):
            derive_spans([(0, 10)], None, speedup_1x=False, speedup_02=False,
                         factor_02=1, ui_rate=1.0, fps=0.0)

    def test_unsorted_input_matches_list_order_semantics(self):
        """乱序输入仍按「列表序首个命中」判定（09-24 提速后 parity 钉子）。

        提速（820ms→14ms，09-24 review T0）把 segs 排序后二分；无重叠时
        排序不改变「谁命中」的答案——本用例钉死这一点。
        """
        spans = derive_spans(
            [(0, 100)],
            [(60, 80, FRAME_TYPE_0_2X), (10, 40, FRAME_TYPE_1X)],
            speedup_1x=True, speedup_02=True, factor_02=10,
            ui_rate=1.0, fps=60.0,
        )
        early = [s for s in spans if s.start_frame == 10 and s.end_frame == 41]
        late = [s for s in spans if s.start_frame == 60 and s.end_frame == 81]
        assert early and early[0].rate == 2   # 1X→step2
        assert late and late[0].rate == 10    # 0.2X→step10

    def test_overlapping_segments_keep_first_listed_priority(self):
        """重叠段表：排序会改变命中答案，故必须回落原 O(n) 语义。"""
        spans = derive_spans(
            [(0, 100)],
            [(60, 80, FRAME_TYPE_1X), (30, 70, FRAME_TYPE_0_2X)],
            speedup_1x=True, speedup_02=True, factor_02=10,
            ui_rate=1.0, fps=60.0,
        )
        # 重叠区 [60,70]：列表序首个命中 = 1X 段（step2）→ rate 2，非 10
        mid = [s for s in spans if s.start_frame == 60 and s.end_frame == 71]
        assert mid and mid[0].rate == 2

    def test_rate_cap_governs_decode_budget(self):
        # 09-13 真机首测：rate100 段（0.2x×factor10×UI10）解码账单 6000fps
        # 饿死管道 → cap 截断，普通段不受影响
        spans = derive_spans(
            [(0, 100)],
            [(10, 20, FRAME_TYPE_0_2X)],
            speedup_1x=False, speedup_02=True, factor_02=10,
            ui_rate=10.0, fps=60.0, rate_cap=20,
        )
        assert [(s.start_frame, s.end_frame, s.rate) for s in spans] == [
            (0, 10, 10), (10, 21, 20), (21, 100, 10),
        ]


class TestNominalAxis:
    def test_mirrors_dsvc_locate_math(self):
        led = _led([(0, 120, 2)])  # 120 帧 @rate2 → 60 保留
        assert led.nominal_tick_for_source_frame(0) == 0
        assert led.nominal_tick_for_source_frame(59) == 29  # 59//2
        assert led.nominal_tick_for_source_frame(60) == 30
        assert led.source_frame_for_nominal_tick(30) == 60
        assert led.total_nominal_ticks() == 60

    def test_two_spans_prefix(self):
        led = _led([(0, 60, 1), (60, 120, 1)])
        assert led.nominal_tick_for_source_frame(60) == 60
        assert led.source_frame_for_nominal_tick(60) == 60
        assert led.source_frame_for_nominal_tick(119) == 119

    def test_deleted_gap_snaps_forward(self):
        led = _led([(0, 60, 1), (100, 160, 1)])
        assert led.nominal_tick_for_source_frame(80) == 60  # 删除段向前贴段首
        assert led.source_frame_for_nominal_tick(60) == 100
        assert led.source_frame_for_nominal_tick(59) == 59

    def test_tick_beyond_end_clamps_to_last_frame(self):
        led = _led([(0, 60, 1)])
        assert led.source_frame_for_nominal_tick(10**9) == 59


class TestActualAxis:
    def test_nominal_estimate_before_actuals(self):
        led = _led([(0, 60, 1), (60, 120, 1)])
        assert led.source_frame_for_tick(90) == 90  # 未喂实测 → 名义暂替

    def test_vfr_actuals_compress_readback(self):
        # 3.mp4 教训的单元形态：段1 名义 60 帧只实推 30 帧
        led = _led([(0, 60, 1), (60, 120, 1)])
        led.set_actuals([30, 60])
        assert led.source_frame_for_tick(35) == 65   # 段2 内 5/60 → 帧 65（名义误读 35）
        assert led.tick_for_source_frame(65) == 35.0
        assert led.source_frame_for_tick(15) == 30   # 段1 内线性
        assert led.source_frame_for_tick(29) == 58
        assert led.source_frame_for_tick(30) == 60   # 段边界精确落位

    def test_eof_complete_refines(self):
        led = _led([(0, 60, 1)])
        led.set_actuals([41], complete=True)
        assert led.source_frame_for_tick(40) == 59  # 40/41×60
        assert led.source_frame_for_tick(41) == 59  # 超出钳末帧

    def test_rate_for_source_frame(self):
        led = _led([(0, 60, 2), (60, 120, 4)])
        assert led.rate_for_source_frame(30) == 2
        assert led.rate_for_source_frame(90) == 4
        assert led.rate_for_source_frame(500) == 4  # 越界钳末段


class TestRebase:
    def test_rebase_shifts_tick_origin_only(self):
        led = _led([(0, 60, 1), (60, 120, 1)])
        k = led.nominal_tick_for_source_frame(90)
        origin = led.source_frame_for_nominal_tick(k)
        led.rebase(k, origin)
        assert led.source_frame_for_tick(95) == 95
        # 名义轴不受 rebase 影响（dsvc.locate 永远全表分割）
        assert led.nominal_tick_for_source_frame(30) == 30

    def test_rebase_split_span_estimate_then_actuals(self):
        led = _led([(0, 60, 2), (60, 120, 2)])
        k = led.nominal_tick_for_source_frame(70)      # 段2 src 10 → 槽 5 → tick 35
        origin = led.source_frame_for_nominal_tick(k)  # 帧 70
        led.rebase(k, origin)
        assert origin == 70
        # 截断首段 [70,120) rate2：名义剩余 25 槽；tick+10 → 段内 0.4 → 帧 90
        assert led.source_frame_for_tick(45) == 90
        led.set_actuals([0, 25])  # 绝对段号：新代段2 区域实推 25
        assert led.source_frame_for_tick(59) == 118  # 24/25 → 帧 70+48

    def test_rebase_twice_keeps_nominal_full_table(self):
        led = _led([(0, 60, 1), (60, 120, 1), (120, 180, 1)])
        led.rebase(90, 90)
        led.rebase(30, led.source_frame_for_nominal_tick(30))
        # 第二次落回段 0：act 视图从段 0 槽 30 重开；原点前的 tick 钳到落点帧
        assert led.source_frame_for_tick(5) == 30
        assert led.source_frame_for_tick(35) == 35
        assert led.nominal_tick_for_source_frame(150) == 150


class TestCoerceSpan:
    def test_pairs_convert_with_frame_rounding(self):
        s = coerce_span([1.0 / 60, 121.0 / 60, 3], 60.0)
        assert (s.start_frame, s.end_frame, s.rate) == (1, 121, 3)
        assert s.a == 1.0 / 60 and s.b == 121.0 / 60

    def test_ledger_accepts_pair_form(self):
        led = DThroughLedger([[0.0, 1.0, 2]], fps=60.0)
        assert led.nominal_tick_for_source_frame(59) == 29
        assert led.source_frame_for_nominal_tick(0) == 0
