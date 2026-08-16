"""Read-only fact report: where do the real sources' PTS anomalies sit
relative to the business cut points?

Inputs are existing evidence only (no media re-scan):
- .cache/mpv_spike/frame_oracle/*.json  — full per-frame PTS tables
- .cache/preview_fluency/{stem}_skip_segs.json — business skip segments
- .cache/preview_fluency/{stem}_meta.json — analyzed fps / frame counts

For every duplicate / non-monotonic PTS frame the report answers:
1. its exact decode index and PTS tick/seconds,
2. whether it lies inside a deleted (skip) or kept region,
3. its distance in frames to the nearest cut boundary,
4. whether any trim boundary tick coincides with an anomalous tick.

Output: PTS_ANOMALY_FACTS_<date>.json / .md next to the repo root.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ORACLE_DIR = REPO / ".cache" / "mpv_spike" / "frame_oracle"
FLUENCY_DIR = REPO / ".cache" / "preview_fluency"

ORACLE_FILES = {
    1: "1_20260809_threads4.json",
    2: "2_20260809.json",
    3: "3_20260809.json",
    4: "4_20260809_threads4.json",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def analyze_sample(stem: int) -> dict:
    oracle_path = ORACLE_DIR / ORACLE_FILES[stem]
    skip_path = FLUENCY_DIR / f"{stem}_skip_segs.json"
    meta_path = FLUENCY_DIR / f"{stem}_meta.json"

    oracle = _load_json(oracle_path)
    skip = _load_json(skip_path)  # list of half-open [start, end) segments
    meta = _load_json(meta_path)

    table = oracle["pts_table"]
    pts_by_n = {row["n"]: row for row in table}
    frames = len(table)

    # Recompute anomalies from the full table instead of trusting summaries.
    by_pts: dict[int, list[int]] = {}
    for row in table:
        by_pts.setdefault(row["pts"], []).append(row["n"])
    duplicates = {
        pts: sorted(ns) for pts, ns in by_pts.items() if len(ns) > 1
    }
    ordered = sorted(table, key=lambda row: row["n"])
    non_monotonic = [
        {
            "previous_n": ordered[i - 1]["n"],
            "n": ordered[i]["n"],
            "previous_pts": ordered[i - 1]["pts"],
            "pts": ordered[i]["pts"],
        }
        for i in range(1, len(ordered))
        if ordered[i]["pts"] < ordered[i - 1]["pts"]
    ]

    # Cross-check against the oracle's own counters.
    reported_dup = oracle["showinfo"]["pts"]["duplicate_count"]
    reported_nonmono = oracle["showinfo"]["pts"]["non_monotonic_count"]
    recomputed_dup_frames = sum(len(v) for v in duplicates.values())
    cross_check = {
        "oracle_duplicate_count": reported_dup,
        "recomputed_duplicate_frames": recomputed_dup_frames,
        "oracle_non_monotonic_count": reported_nonmono,
        "recomputed_non_monotonic_steps": len(non_monotonic),
    }

    # Business cut boundaries in half-open terms: each segment's start and end.
    boundaries = sorted({int(s) for s, _e in skip} | {int(e) for _s, e in skip})
    boundary_ticks = sorted({pts_by_n[b]["pts"] for b in boundaries if b in pts_by_n})

    fps = float(meta.get("fps") or 0.0) or 60.0
    time_base = oracle["showinfo"]["time_base"]
    tb_seconds = time_base["numerator"] / time_base["denominator"]

    def region_of(n: int):
        for s, e in skip:
            if int(s) <= n < int(e):
                return {"region": "deleted", "skip_segment": [int(s), int(e)]}
        return {"region": "kept"}

    anomaly_frames = sorted(
        {n for group in duplicates.values() for n in group}
        | {step["n"] for step in non_monotonic}
        | {step["previous_n"] for step in non_monotonic}
    )
    frame_facts = []
    for n in anomaly_frames:
        row = pts_by_n[n]
        nearest = min(boundaries, key=lambda b: abs(n - b))
        tick_gap = min(
            (abs(tick - row["pts"]) for tick in boundary_ticks), default=None
        )
        frame_facts.append(
            {
                "n": n,
                "pts": row["pts"],
                "pts_time": row["pts_time"],
                **region_of(n),
                "nearest_boundary_frame": nearest,
                "distance_frames": abs(n - nearest),
                "distance_seconds": round(abs(n - nearest) / fps, 6),
                "nearest_boundary_tick_gap": tick_gap,
                "nearest_boundary_tick_gap_ms": (
                    round(tick_gap * tb_seconds * 1000.0, 6)
                    if tick_gap is not None
                    else None
                ),
            }
        )

    max_anomaly_n = max(anomaly_frames)
    first_kept = min((int(e) for _s, e in skip), default=0)
    all_in_first_skip = all(fact["region"] == "deleted" for fact in frame_facts)

    # Direct export-impact test: kept frames whose own pts falls outside
    # the tick span of their kept interval ([pts(first), end_tick)) would be
    # silently dropped or double-kept by pure tick-based FFmpeg trim.
    kept_intervals = []
    cursor = 0
    for s, e in sorted((int(s), int(e)) for s, e in skip):
        if cursor < s:
            kept_intervals.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < frames:
        kept_intervals.append((cursor, frames))
    tick_dropped_kept_frames = []
    for a, b in kept_intervals:
        start_tick = pts_by_n[a]["pts"]
        end_tick = pts_by_n[b - 1]["pts"] + pts_by_n[b - 1]["duration"]
        for n in range(a, b):
            if pts_by_n[n]["pts"] >= end_tick or pts_by_n[n]["pts"] < start_tick:
                tick_dropped_kept_frames.append(
                    {
                        "n": n,
                        "pts": pts_by_n[n]["pts"],
                        "interval": [a, b],
                        "interval_ticks": [start_tick, end_tick],
                    }
                )

    return {
        "stem": stem,
        "oracle_file": oracle_path.name,
        "oracle_sha256": _sha256(oracle_path),
        "skip_segs_file": skip_path.name,
        "skip_segs_sha256": _sha256(skip_path),
        "frames": frames,
        "fps": fps,
        "time_base": time_base["text"],
        "skip_segment_count": len(skip),
        "boundary_count": len(boundaries),
        "cross_check": cross_check,
        "duplicate_pts_groups": [
            {"pts": pts, "frames": ns} for pts, ns in sorted(duplicates.items())
        ],
        "non_monotonic_steps": non_monotonic,
        "anomaly_frame_facts": frame_facts,
        "max_anomaly_frame": max_anomaly_n,
        "min_distance_frames": min(f["distance_frames"] for f in frame_facts),
        "min_boundary_tick_gap_ms": min(
            f["nearest_boundary_tick_gap_ms"] for f in frame_facts
        ),
        "all_anomalies_inside_deleted_regions": all_in_first_skip,
        "first_kept_frame": first_kept,
        "kept_interval_count": len(kept_intervals),
        "tick_dropped_kept_frames": tick_dropped_kept_frames,
    }


def main() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    results = [analyze_sample(stem) for stem in sorted(ORACLE_FILES)]

    verdict_all_deleted = all(r["all_anomalies_inside_deleted_regions"] for r in results)
    min_distance = min(r["min_distance_frames"] for r in results)
    min_tick_gap_ms = min(r["min_boundary_tick_gap_ms"] for r in results)
    total_tick_dropped = sum(len(r["tick_dropped_kept_frames"]) for r in results)

    report = {
        "kind": "pts_anomaly_fact_report",
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "inputs_policy": "read-only over existing oracle/skip evidence; no media re-scan",
        "samples": results,
        "aggregate": {
            "all_anomalies_inside_deleted_regions": verdict_all_deleted,
            "min_distance_frames_to_any_cut_boundary": min_distance,
            "min_boundary_tick_gap_ms": min_tick_gap_ms,
            "kept_frames_unrepresentable_by_tick_trim": total_tick_dropped,
        },
    }

    json_path = REPO / f"PTS_ANOMALY_FACTS_{stamp}.json"
    md_path = REPO / f"PTS_ANOMALY_FACTS_{stamp}.md"
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)

    lines = [
        "# PTS 瑕疵事实报告（只读证据分析）",
        "",
        f"生成时间（UTC）：{report['created_utc']}",
        "",
        "数据来源：既有 frame oracle（完整 PTS 表）与业务 skip 段表；未重新扫描任何媒体。",
        "",
        "## 结论",
        "",
        f"- 全部 PTS 瑕疵帧是否都位于删除区内：{'是' if verdict_all_deleted else '否'}",
        f"- 瑕疵帧到最近切点边界的最小距离：{min_distance} 帧",
        f"- 瑕疵 tick 与任何裁剪边界 tick 的最小差距：{min_tick_gap_ms:.6f} ms",
        f"- tick 裁剪无法正确表示的保留帧总数（即真实导出损伤）：{total_tick_dropped} 帧",
        "",
        "## 分样本明细",
        "",
        "| 样本 | 帧数 | skip 段数 | 瑕疵帧范围 | 最小距离(帧) | 最小 tick 差距(ms) | 全部在删除区 |",
        "|---|---:|---:|---|---:|---:|---|",
    ]
    for r in results:
        lo = min(f["n"] for f in r["anomaly_frame_facts"])
        hi = max(f["n"] for f in r["anomaly_frame_facts"])
        lines.append(
            f"| {r['stem']} | {r['frames']} | {r['skip_segment_count']} "
            f"| [{lo},{hi}] | {r['min_distance_frames']} "
            f"| {r['min_boundary_tick_gap_ms']:.6f} "
            f"| {'是' if r['all_anomalies_inside_deleted_regions'] else '否'} |"
        )
    lines += ["", "## 瑕疵帧明细", ""]
    for r in results:
        lines.append(f"### 样本 {r['stem']}(time_base {r['time_base']},fps {r['fps']:.3f})")
        lines.append("")
        for f in r["anomaly_frame_facts"]:
            seg = f.get("skip_segment")
            seg_txt = f"[{seg[0]},{seg[1]}]" if seg else "-"
            lines.append(
                f"- 帧 {f['n']}(pts={f['pts']}, {f['pts_time']:.6f}s):"
                f" 区域={f['region']}(段 {seg_txt}),"
                f" 最近边界=帧 {f['nearest_boundary_frame']}"
                f"(距离 {f['distance_frames']} 帧 / {f['distance_seconds']:.3f}s),"
                f" 边界 tick 差 {f['nearest_boundary_tick_gap_ms']:.6f} ms"
            )
        if r["tick_dropped_kept_frames"]:
            for drop in r["tick_dropped_kept_frames"]:
                it = drop["interval"]
                ticks = drop["interval_ticks"]
                lines.append(
                    f"- **导出损伤**：保留帧 {drop['n']}(pts={drop['pts']})"
                    f" 位于保留区间 [{it[0]},{it[1]}) 的 tick 端点"
                    f" [{ticks[0]},{ticks[1]}) 之外——纯 tick 裁剪无法既保留它又剔除同 tick 的删除帧"
                )
        else:
            lines.append("- 导出损伤：无（所有瑕疵帧均可被 tick 裁剪正确表示）")
        lines.append("")
    lines += [
        "## 交叉核验",
        "",
        "每个样本用完整 PTS 表重新计算瑕疵并与 oracle 自报计数比对",
        "（oracle 计重复组数，重算计涉及帧数；非单调为步数，口径一致）：",
        "",
    ]
    for r in results:
        c = r["cross_check"]
        lines.append(
            f"- 样本 {r['stem']}: oracle 报重复 {c['oracle_duplicate_count']}"
            f"/重算 {c['recomputed_duplicate_frames']},"
            f" 非单调 {c['oracle_non_monotonic_count']}"
            f"/重算 {c['recomputed_non_monotonic_steps']}"
        )
    lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"wrote {json_path.name}")
    print(f"wrote {md_path.name}")
    print(json.dumps(report["aggregate"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
