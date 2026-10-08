"""dthrough_ledger.py — 解码直通档双轴台账（一期 1c-①，纯 Python 可单测）。

直通档有两根「虚拟帧轴」，命令与读回必须各用各的：

* **名义轴（命令语言）**：dsvc ``locate`` 与 rawvideo demuxer 的 ``start=``/
  stream seek 字节寻址只认名义分割——每段名义保留数 =
  ``ceil(floor((b-a)*fps) / rate)``，其中 b/a 是送 dsvc 的同一批 float 源秒
  （算式逐位复刻 Rust 端，两端对同一轴零歧义）。
* **实测轴（读回语言）**：mpv ``time-pos``×fps = 实际已推帧数。dsvc 实际
  推帧数随源 PTS 密度浮动（3.mp4 实证：名义 520 vs 实测 413），读回映射
  必须用 dsvc span/eof 事件上报的 per_span 实测保留数做分段线性映射，
  **禁用名义值**。

段内一律线性插值：某段的实测保留数未上报前（尚未播到）以名义值暂替，
事件到达即精化——累计误差被逐段实测钳住，不随段数增长。seek 后
:meth:`DThroughLedger.rebase` 重定基：新代流从落点起推、实测累计相对新
代，而名义轴永远按全段表（dsvc.locate 每次都拿全表分割）。

EDL 虚拟秒不进本模块：时间轴指针 = 本模块出源帧 → 既有认证 EDL
（``virtual_time_for_source``）出虚拟秒，避免维护第二套 PTS 表。
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass
from typing import Mapping, Sequence

from frame_types import FRAME_TYPE_0_2X, FRAME_TYPE_1X


@dataclass(frozen=True, slots=True)
class LedgerSpan:
    """一个原子推流段：源帧区间 [start_frame, end_frame) + 抽帧率 rate。

    ``a``/``b`` 与送 dsvc 的 JSON 完全同源（同一 float 值），名义数复刻
    dsvc locate 的算式，保证两端对名义轴的理解逐位一致。
    """

    start_frame: int
    end_frame: int
    rate: int
    a: float  # 源秒起点（=start_frame/fps）
    b: float  # 源秒终点（=end_frame/fps）


def coerce_span(span: "LedgerSpan | Sequence[float]", fps: float) -> LedgerSpan:
    """接受 LedgerSpan 或 dsvc 形态 [a, b, rate]（源秒），归一为 LedgerSpan。"""
    if isinstance(span, LedgerSpan):
        return span
    a, b, rate = (float(span[0]), float(span[1]), int(span[2]))
    fps = float(fps)
    if fps <= 0:
        raise ValueError("fps must be positive")
    start = int(round(a * fps))
    end = max(start + 1, int(round(b * fps)))
    return LedgerSpan(start, end, max(1, rate), a, b)


def _biz_step(seg_type: int, speedup_1x: bool, speedup_02: bool, factor_02: int) -> int:
    """业务段倍率（引擎 _biz_speed_step 的同语义镜像）。"""
    if seg_type == FRAME_TYPE_1X and speedup_1x:
        return 2
    if seg_type == FRAME_TYPE_0_2X and speedup_02:
        return max(2, int(factor_02 or 1))
    return 1


def derive_spans(
    kept_ranges: Sequence[Sequence[int]],
    speed_segments: Sequence[Sequence[int]] | None,
    *,
    speedup_1x: bool,
    speedup_02: bool,
    factor_02: int,
    ui_rate: float,
    fps: float,
    rate_cap: int | None = None,
) -> tuple[LedgerSpan, ...]:
    """认证 plan 保留段 × 业务变速段 → 原子推流段表（GUI→dsvc/台账单一来源）。

    rate = 业务倍速(step) × UI 倍速，四舍五入取整且不低于 1（dsvc 把 rate
    截断成 u64，端上必须先取整）。``rate_cap``=解码预算限速：直通档解码
    账单=rate×60 帧/秒（段内每个源帧都是保留帧的解码依赖，账单与 rate 成
    正比），业务加速段（0.2x×factor×UI 可叠到 100）会超出机器解码头
    （实测 2029fps）饿死管道——超限段按 cap 截断，预览在该段以 cap 倍速
    通过（与 Tier1 封顶 3x 同哲学：cap 以上节奏尽力而为，时间轴指针靠
    台账实测轴保持精确）。业务 step 复刻引擎 _biz_speed_step：
    1X 段 speedup_1x→2、0.2X 段 speedup_02→max(2, factor_02)；闭区间
    [s, e]、按 speed_segments 列表序首个命中段优先（上游 parity）。
    """
    fps = float(fps)
    if fps <= 0:
        raise ValueError("fps must be positive")
    ui_rate = float(ui_rate)
    if not math.isfinite(ui_rate) or ui_rate <= 0:
        raise ValueError("ui_rate must be a finite positive number")
    cap = max(1, int(rate_cap)) if rate_cap else None
    # 业务段归一化一次（原实现每个切点都重新 int() 全部 segs → O(kept×segs)
    # 纯 Python 循环，真实项目实测 1.83s 主线程卡顿，2026-09-24 review T0）。
    ordered: list[tuple[int, int, int]] = []
    for seg in (speed_segments or ()):
        try:
            ordered.append((int(seg[0]), int(seg[1]), int(seg[2])))
        except (TypeError, ValueError, IndexError):
            continue
    segs = sorted(ordered, key=lambda s: s[0])
    # 命中判定「列表序首个命中段优先」在排序后**不再成立**（重叠时排序会改变
    # 谁先命中），故只在无重叠时启用二分快路径，重叠时回落**原始列表序**
    # （ordered，非 segs）——宁慢不错。（09-24 提速时此处曾误用排序表，
    # 被新增的重叠用例当场抓住。）
    overlap = any(b[0] <= a[1] for a, b in zip(segs, segs[1:]))
    fast = not overlap

    def _step_at(frame: int) -> int:
        if not ordered:
            return 1
        if fast:
            # 排序 + 无重叠 ⇒ 二分找最后一个 start<=frame 的段，端点判定即可
            i = bisect_right(segs, (frame, float("inf"), float("inf"))) - 1
            if i >= 0 and segs[i][0] <= frame <= segs[i][1]:
                return _biz_step(segs[i][2], speedup_1x, speedup_02, factor_02)
            return 1
        for s, e, t in ordered:   # 列表序首个命中优先（原语义）
            if s <= frame <= e:
                return _biz_step(t, speedup_1x, speedup_02, factor_02)
        return 1

    spans: list[LedgerSpan] = []
    for kept in kept_ranges:
        ks, ke = int(kept[0]), int(kept[1])
        if ke <= ks:
            continue
        # 切点：业务段边界（闭区间语义 → 段尾切点 = e+1），裁进保留段内
        cuts = {ks, ke}
        for s, e, _t in segs:
            if e < ks or s >= ke:
                continue
            if s > ks:
                cuts.add(min(s, ke))
            if e + 1 > ks:
                cuts.add(min(e + 1, ke))
        points = sorted(c for c in cuts if ks <= c <= ke)
        for x, y in zip(points, points[1:]):
            if y <= x:
                continue
            step = _step_at(x)
            rate = max(1, int(ui_rate * step + 0.5))
            if cap is not None:
                rate = min(rate, cap)
            spans.append(LedgerSpan(x, y, rate, x / fps, y / fps))
    return tuple(spans)


class DThroughLedger:
    """双轴台账：名义轴编码 seek 命令，实测轴解码 time-pos 读数。

    线程约定：所有方法须在同一把外部锁（引擎锁）下调用。
    """

    def __init__(self, spans: Sequence["LedgerSpan | Sequence[float]"], *, fps: float) -> None:
        fps = float(fps)
        if fps <= 0:
            raise ValueError("fps must be positive")
        self._fps = fps
        self._spans: tuple[LedgerSpan, ...] = tuple(coerce_span(s, fps) for s in spans)
        self._nom_kept: list[int] = [self._full_nominal_kept(s) for s in self._spans]
        prefix = [0]
        for kept in self._nom_kept:
            prefix.append(prefix[-1] + kept)
        self._nom_prefix: list[int] = prefix
        # 实测视图（load 代=全表原点 0；seek 代=rebase 截断+tick 原点）
        self._act_spans: tuple[LedgerSpan, ...] = self._spans
        self._origin_span = 0            # act_spans[0] 对应的全表段号
        self._tick_origin = 0            # act_spans[0] 段首的名义 tick
        self._split_remaining = 0        # 截断首段的名义估计（=剩余槽位数）
        self._act_counts: dict[int, int] = {}   # act 段号 → 实测保留数
        self._act_complete = False       # eof 全量 per_span 已精化
        self._split_first = False        # act 首段是否为 rebase 截断段

    # ---- 名义轴（命令语言；永远全段表） ----

    def _full_nominal_kept(self, span: LedgerSpan) -> int:
        """逐位复刻 dsvc locate：n_src=floor((b-a)*fps)，kept=ceil(n_src/rate)。"""
        n_src = math.floor((span.b - span.a) * self._fps)
        rate = max(1, int(span.rate))
        return -(-n_src // rate)

    def total_nominal_ticks(self) -> int:
        return self._nom_prefix[-1] if self._nom_prefix else 0

    def _locate_span_grid(self, frame: int) -> tuple[int, int]:
        """源帧 → (全表段号, 段内网格槽 m)。删除段/越界按「下一保留段首/末段尾」钳位。"""
        if not self._spans:
            raise ValueError("ledger has no spans")
        frame = int(frame)
        for i, span in enumerate(self._spans):
            if frame < span.start_frame:
                return i, 0  # 落在删除段：向前贴下一保留段首
            if frame < span.end_frame:
                return i, (frame - span.start_frame) // span.rate
        last = len(self._spans) - 1
        span = self._spans[last]
        return last, max(0, (span.end_frame - 1 - span.start_frame)) // span.rate

    def nominal_tick_for_source_frame(self, frame: int) -> int:
        """源帧 → 名义轴 tick（dsvc seek / rawvideo start= 的命令坐标）。"""
        i, m = self._locate_span_grid(frame)
        return self._nom_prefix[i] + m

    def source_frame_for_nominal_tick(self, tick: int) -> int | None:
        """名义 tick → 源帧（dsvc locate 的镜像；越界钳到最后一段末槽）。"""
        if not self._spans:
            return None
        tick = int(tick)
        if tick < 0:
            tick = 0
        for i, span in enumerate(self._spans):
            if tick < self._nom_prefix[i + 1] or i == len(self._spans) - 1:
                m = tick - self._nom_prefix[i]
                kept = self._nom_kept[i]
                if m >= kept:
                    m = kept - 1
                if m < 0:
                    m = 0
                frame = span.start_frame + m * span.rate
                return min(max(frame, span.start_frame), span.end_frame - 1)
        return None

    # ---- 实测轴（读回语言） ----

    def set_actuals(self, per_span: Sequence[int], *, complete: bool = False) -> None:
        """喂入当前代的实测 per_span 保留数（**绝对段号**索引，dsvc 语义）。

        截断首段（rebase 后）取 per_span[origin_span] = 本代在该段区域实推数；
        complete=True 表示 eof 全量到达，未列出的段按 0 处理。
        """
        for j in range(len(self._act_spans)):
            abs_i = self._origin_span + j
            if abs_i < len(per_span):
                self._act_counts[j] = max(0, int(per_span[abs_i]))
        if complete:
            self._act_complete = True

    def _estimate_count(self, j: int) -> int:
        if j in self._act_counts:
            return self._act_counts[j]
        if j == 0 and self._split_first:
            return self._split_remaining
        return self._full_nominal_kept(self._act_spans[j])

    def _actual_prefix(self) -> list[int]:
        prefix = [0]
        for j in range(len(self._act_spans)):
            prefix.append(prefix[-1] + self._estimate_count(j))
        return prefix

    def source_frame_for_tick(self, tick: int) -> int | None:
        """实测轴 tick（=time-pos×fps）→ 当前呈现的源帧。"""
        if not self._act_spans:
            return None
        t = int(tick) - self._tick_origin
        if t < 0:
            t = 0
        prefix = self._actual_prefix()
        for j, span in enumerate(self._act_spans):
            if t < prefix[j + 1] or j == len(self._act_spans) - 1:
                count = prefix[j + 1] - prefix[j]
                if count <= 0:
                    return min(max(span.start_frame, 0), span.end_frame - 1)
                frac = (t - prefix[j]) / count
                width = span.end_frame - span.start_frame
                frame = span.start_frame + int(round(frac * width))
                return min(max(frame, span.start_frame), span.end_frame - 1)
        return None

    def tick_for_source_frame(self, frame: int) -> float:
        """源帧 → 实测轴 tick（进度/对账用；origin 之前的帧回原点）。"""
        if not self._act_spans:
            raise ValueError("ledger has no spans")
        frame = int(frame)
        prefix = self._actual_prefix()
        for j, span in enumerate(self._act_spans):
            if frame < span.start_frame:
                return float(self._tick_origin + prefix[j])
            if frame < span.end_frame:
                width = span.end_frame - span.start_frame
                frac = (frame - span.start_frame) / width
                return float(self._tick_origin + prefix[j] + frac * self._estimate_count(j))
        return float(self._tick_origin + prefix[-1])

    def rate_for_source_frame(self, frame: int) -> int:
        """源帧所在段的抽帧率（=该段生效倍速；角标/对账用）。"""
        if not self._act_spans:
            raise ValueError("ledger has no spans")
        frame = int(frame)
        for span in self._act_spans:
            if frame < span.start_frame:
                return span.rate
            if frame < span.end_frame:
                return span.rate
        return self._act_spans[-1].rate

    def span_index_for_source_frame(self, frame: int) -> int | None:
        """源帧在**全表**段号（同段判定用：暂停→播放快速路径）。

        与读回映射无关（名义表，rebase 不影响），故两帧是否同段可直接比。
        删除段/越界按 ``_locate_span_grid`` 的钳位语义（向前贴下一保留段）。
        """
        if not self._spans:
            return None
        try:
            index, _slot = self._locate_span_grid(int(frame))
        except (TypeError, ValueError):
            return None
        return index

    def rebase(self, tick_origin: int, frame_origin: int) -> None:
        """seek 落点重定基：新代流从 (名义 tick, 落点源帧) 起推。

        截断首段保持原段 rate（dsvc 网格相位相对原段首不变，落点向前
        钳到网格槽），名义估计 = 原段槽位总数 - 落点槽号。
        """
        if not self._spans:
            return
        i, m = self._locate_span_grid(frame_origin)
        span = self._spans[i]
        grid_frame = min(span.start_frame + m * span.rate, span.end_frame - 1)
        self._act_spans = (
            (LedgerSpan(grid_frame, span.end_frame, span.rate, span.a, span.b),)
            + self._spans[i + 1:]
        )
        self._origin_span = i
        self._tick_origin = int(tick_origin)
        self._split_remaining = max(0, self._nom_kept[i] - m)
        self._split_first = True
        self._act_counts.clear()
        self._act_complete = False
