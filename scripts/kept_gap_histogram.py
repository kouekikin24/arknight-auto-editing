#!/usr/bin/env python3
"""Read-only kept-gap histogram from A_PT skip segs. Does not change playback code."""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
ROOT = _REPO / ".cache" / "preview_fluency"


def collapse_overlap_and_touch(segs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not segs:
        return []
    out: list[list[int]] = [list(segs[0])]
    for a, b in segs[1:]:
        if a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def merge_gaps(segs: list[tuple[int, int]], k: int) -> list[tuple[int, int]]:
    if not segs:
        return []
    out: list[list[int]] = [list(segs[0])]
    for a, b in segs[1:]:
        gap = a - out[-1][1]
        if gap <= k:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def pctile(xs: list[int], p: float) -> float:
    if not xs:
        return 0.0
    sg = sorted(xs)
    if len(sg) == 1:
        return float(sg[0])
    k = (len(sg) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sg) - 1)
    if f == c:
        return float(sg[f])
    return float(sg[f] + (sg[c] - sg[f]) * (k - f))


def main() -> int:
    meta_path = ROOT / "2_meta.json"
    segs_path = ROOT / "2_skip_segs.json"
    if not meta_path.is_file() or not segs_path.is_file():
        print(f"FAIL: missing cache under {ROOT}", file=sys.stderr)
        return 2

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    raw = json.loads(segs_path.read_text(encoding="utf-8"))
    segs = [(int(a), int(b)) for a, b in raw]
    segs_sorted = sorted(segs, key=lambda x: (x[0], x[1]))
    segs_sorted = [(a, b) for a, b in segs_sorted if b > a]

    print("=== meta ===")
    for k in sorted(meta.keys()):
        v = meta[k]
        if isinstance(v, (int, float, str, bool)) or v is None:
            print(f"  {k}: {v}")

    print(f"raw skip segs: {len(segs)}")
    print(f"valid segs: {len(segs_sorted)}")

    collapsed = collapse_overlap_and_touch(segs_sorted)
    print(f"after collapse overlap/touch: {len(collapsed)} delete-runs")

    gaps = [collapsed[i + 1][0] - collapsed[i][1] for i in range(len(collapsed) - 1)]

    fps = float(meta.get("fps") or 60.0)
    total = int(meta.get("total_frames") or meta.get("L") or meta.get("frames") or 0)
    if not total:
        try:
            import numpy as np

            total = int(np.load(ROOT / "2_states.npy").shape[0])
        except Exception:
            total = collapsed[-1][1] if collapsed else 0

    print(f"fps={fps:.4f} total_frames={total}")
    print(f"kept-gaps between delete-runs: n={len(gaps)}")

    buckets = [
        (0, 0, "0 (none after touch-collapse)"),
        (1, 1, "1 frame"),
        (2, 3, "2-3"),
        (4, 6, "4-6"),
        (7, 12, "7-12  <- default K=12"),
        (13, 30, "13-30"),
        (31, 60, "31-60"),
        (61, 120, "61-120"),
        (121, 300, "121-300"),
        (301, 10**12, "301+"),
    ]

    print()
    print("=== kept-gap histogram ===")
    print(f"{'bucket':<32} {'count':>6} {'pct':>7}")
    for lo, hi, label in buckets:
        c = sum(1 for g in gaps if lo <= g <= hi)
        pct = 100.0 * c / len(gaps) if gaps else 0.0
        print(f"{label:<32} {c:6d} {pct:6.1f}%")

    le12 = sum(1 for g in gaps if 1 <= g <= 12)
    le30 = sum(1 for g in gaps if 1 <= g <= 30)
    le60 = sum(1 for g in gaps if 1 <= g <= 60)
    print(f"gaps <=12: {le12}/{len(gaps)} = {100 * le12 / max(len(gaps), 1):.1f}%")
    print(f"gaps <=30: {le30}/{len(gaps)} = {100 * le30 / max(len(gaps), 1):.1f}%")
    print(f"gaps <=60: {le60}/{len(gaps)} = {100 * le60 / max(len(gaps), 1):.1f}%")

    if gaps:
        print(
            "gap min/p50/p90/p95/max = "
            f"{min(gaps)} / {pctile(gaps, 50):.0f} / {pctile(gaps, 90):.0f} / "
            f"{pctile(gaps, 95):.0f} / {max(gaps)}"
        )
        print(
            f"gap mean = {sum(gaps) / len(gaps):.1f} frames "
            f"({sum(gaps) / len(gaps) / fps * 1000:.1f} ms)"
        )

    spans = [b - a for a, b in collapsed]
    print()
    print(f"=== delete-run span distribution (n={len(spans)}) ===")
    for lo, hi in [
        (1, 12),
        (13, 30),
        (31, 100),
        (101, 300),
        (301, 1000),
        (1001, 3000),
        (3001, 10**9),
    ]:
        c = sum(1 for s in spans if lo <= s <= hi)
        lab = f"{lo}-{hi if hi < 10**9 else 'inf'}"
        print(f"  span {lab}: {c} ({100 * c / max(len(spans), 1):.1f}%)")
    if spans:
        print(
            f"  span min/p50/max = {min(spans)} / "
            f"{sorted(spans)[len(spans) // 2]} / {max(spans)}"
        )
        print(
            f"  total deleted frames = {sum(spans)} "
            f"({100 * sum(spans) / max(total, 1):.1f}% of video)"
        )

    thr = 100
    print()
    print("=== merge simulation: jump count vs K ===")
    print(
        f"{'K':>6} {'jumps':>8} {'delta_raw':>10} {'delta_base':>10} "
        f"{'hidden_frames':>14} {'hidden_s':>10} {'seek_like':>10}"
    )
    raw_jumps = len(segs_sorted)
    base_jumps = len(collapsed)
    for k in [0, 1, 3, 6, 12, 30, 60, 120, 300, 10**9]:
        m = merge_gaps(collapsed, k)
        if k == 0:
            hidden = 0
        elif k >= 10**9:
            hidden = sum(gaps)
        else:
            hidden = sum(g for g in gaps if 1 <= g <= k)
        seekish = sum(1 for a, b in m if (b - a) > thr)
        label = "inf" if k >= 10**9 else str(k)
        print(
            f"{label:>6} {len(m):8d} {raw_jumps - len(m):+10d} "
            f"{base_jumps - len(m):+10d} {hidden:14d} {hidden / fps:10.2f}s "
            f"{seekish:10d}"
        )

    print()
    print("=== cluster collapse potential ===")
    for max_gap in [1, 3, 6, 12, 30]:
        if not collapsed:
            break
        sizes = [1]
        for g in gaps:
            if g <= max_gap:
                sizes[-1] += 1
            else:
                sizes.append(1)
        multi = [s for s in sizes if s >= 2]
        print(
            f"  K={max_gap}: clusters={len(sizes)}, multi-run={len(multi)}, "
            f"runs_saved={sum(s - 1 for s in sizes)}, "
            f"largest_cluster_runs={max(sizes)}"
        )

    report = {
        "n_raw_segs": len(segs_sorted),
        "n_collapsed_runs": len(collapsed),
        "n_gaps": len(gaps),
        "gaps_le_12": le12,
        "gaps_le_12_pct": round(100 * le12 / max(len(gaps), 1), 2),
        "gaps_le_30": le30,
        "gaps_le_30_pct": round(100 * le30 / max(len(gaps), 1), 2),
        "jumps_K0": len(collapsed),
        "jumps_K12": len(merge_gaps(collapsed, 12)),
        "jumps_K30": len(merge_gaps(collapsed, 30)),
        "jumps_saved_K12": len(collapsed) - len(merge_gaps(collapsed, 12)),
        "jumps_saved_K30": len(collapsed) - len(merge_gaps(collapsed, 30)),
        "hidden_frames_K12": sum(g for g in gaps if 1 <= g <= 12),
        "hidden_s_K12": round(sum(g for g in gaps if 1 <= g <= 12) / fps, 3),
        "hidden_frames_K30": sum(g for g in gaps if 1 <= g <= 30),
        "hidden_s_K30": round(sum(g for g in gaps if 1 <= g <= 30) / fps, 3),
        "seek_like_K0": sum(1 for a, b in collapsed if (b - a) > thr),
        "seek_like_K12": sum(
            1 for a, b in merge_gaps(collapsed, 12) if (b - a) > thr
        ),
        "seek_like_K30": sum(
            1 for a, b in merge_gaps(collapsed, 30) if (b - a) > thr
        ),
        "verdict": "",
    }
    saved_pct = 100 * report["jumps_saved_K12"] / max(report["jumps_K0"], 1)
    if saved_pct < 10:
        report["verdict"] = (
            "SHELVE: K=12 saves <10% jumps; current spikes are large-span seeks; "
            "fragment-merge is low value for this source."
        )
    elif saved_pct < 25:
        report["verdict"] = (
            "OPTIONAL: modest jump reduction; only worth it with opt-in UI and tests."
        )
    else:
        report["verdict"] = (
            "CANDIDATE: material jump reduction at K=12; implement behind checkbox."
        )

    out = ROOT / "kept_gap_histogram_report.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print()
    print(f"wrote {out}")
    print("VERDICT:", report["verdict"])
    print("SUMMARY_JSON", json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
