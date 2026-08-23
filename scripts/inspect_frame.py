#!/usr/bin/env python3
"""查某一帧的暂停检测诊断：分数/明暗/状态/前后曲线 + 漏检判断。

用法:
    python scripts/inspect_frame.py 12345            # 查 2.mp4 的第 12345 帧
    python scripts/inspect_frame.py 12345 --video 4  # 查 4.mp4
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[1]
_CACHE = _REPO / ".cache" / "preview_fluency"

_NAMES = {0: "NORMAL(普通)", 1: "PAUSE(暂停)", 2: "1X", 3: "2X", 4: "0.2X(子弹时间)"}


def _load(stem: str):
    base = _CACHE
    ps = base / f"{stem}_pause_score.npy"
    lu = base / f"{stem}_luma.npy"
    st = base / f"{stem}_states.npy"
    df = base / f"{stem}_diffs.npy"
    missing = [str(p) for p in (ps, lu, st) if not p.is_file()]
    if missing:
        raise SystemExit(
            "缺诊断数据文件，先用 --diagnostics 跑一遍分析：\n  " + "\n  ".join(missing)
        )
    return (
        np.load(ps),
        np.load(lu),
        np.load(st),
        np.load(df) if df.is_file() else None,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("frame", type=int, help="帧号（0 起）")
    ap.add_argument("--video", type=str, default="2", help="视频 stem，默认 2")
    ap.add_argument("--span", type=int, default=8, help="前后各看几帧，默认 8")
    args = ap.parse_args()

    ps, lu, st, df = _load(args.video)
    n = len(ps)
    f = args.frame
    if not (0 <= f < n):
        raise SystemExit(f"帧号越界：{f}，有效范围 0 ~ {n-1}")

    state = int(st[f])
    score = float(ps[f])
    luma = float(lu[f])
    diff = float(df[f]) if df is not None and f < len(df) else None

    print(f"=== {args.video}.mp4  第 {f} 帧 ===")
    print(f"  判定状态   : {_NAMES.get(state, state)}")
    print(f"  暂停分数   : {score:.3f}   (阈值 0.7，{'过线' if score >= 0.7 else '【没过线】'})")
    print(f"  画面明暗   : {luma:.1f}    (暂停段典型 ~41，正常战斗 ~63)")
    if diff is not None:
        print(f"  与上帧差异 : {diff:.2f}    (动作阈值 2.0，{'静止' if diff <= 2.0 else '有动作'})")

    # 前后曲线
    lo = max(0, f - args.span)
    hi = min(n, f + args.span + 1)
    print(f"\n  前后 {args.span} 帧曲线（帧号: 状态 分数 明暗 差异）:")
    for i in range(lo, hi):
        mark = " <== 你指的这帧" if i == f else ""
        d = f"{float(df[i]):5.2f}" if df is not None and i < len(df) else "  -- "
        print(
            f"    {i:>7}: {_NAMES.get(int(st[i]),'?'):<12} "
            f"分={float(ps[i]):.3f} 明={float(lu[i]):5.1f} 差={d}{mark}"
        )

    # 自动判断
    print("\n=== 自动判断 ===")
    in_pause = state == 1
    if in_pause:
        print("  这帧已被判为 PAUSE，正常应在暂停段内。")
        print("  若它在成片里残留 → 问题在【段内缓冲/裁剪】，不是漏检。")
    else:
        near = ps[max(0, f - 30): f + 31]
        if score >= 0.45 and score < 0.7:
            print(f"  这帧分数 {score:.3f} 卡在过渡带（0.45~0.7），高度疑似【阈值漏检】。")
            print("  → 可考虑：阈值下调到 0.5 左右，或加'变暗'辅助判据把它捞回。")
        elif score < 0.45 and luma < 48:
            print(f"  这帧分数低({score:.3f})但画面偏暗({luma:.1f})，疑似暂停但 S 样板没匹配上。")
            print("  → 模板没盖住这种暂停形态，可能要加样板或靠明暗辅助。")
        elif state == 4:
            print("  这帧被判成了 0.2X 子弹时间，不是暂停。")
            print("  → 你看到的'残留'是子弹时间慢放，属于 0.2X 段的预览/裁剪策略问题，不是暂停漏检。")
        else:
            print(f"  这帧分数 {score:.3f}、明暗 {luma:.1f}，看起来更像正常战斗画面。")
            print("  → 若你确定这是暂停，那是一种新形态，需要看实际截图才能识别。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
