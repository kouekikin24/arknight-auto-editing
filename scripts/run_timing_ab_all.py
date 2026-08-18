#!/usr/bin/env python3
"""Drive the fluency bench across all cached videos x {fixed, legacy} timing.

Writes one json per combo into .cache/preview_fluency/ab/ and appends a
compact record to ab/results.jsonl so progress is readable mid-run.

Usage:
  python -u scripts/run_timing_ab_all.py --biz on --repeat 1
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_CACHE = _REPO / ".cache" / "preview_fluency"
_OUT = _CACHE / "ab"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--biz", choices=("on", "off"), default="on")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--stems", type=str, default="3,2,1,4",
                    help="comma list, ordered shortest-first by default")
    args = ap.parse_args()

    _OUT.mkdir(parents=True, exist_ok=True)
    jsonl = _OUT / "results.jsonl"

    stems = [s.strip() for s in args.stems.split(",") if s.strip()]
    combos = []
    for stem in stems:
        for legacy in (False, True):
            combos.append((stem, legacy))

    print(f"[driver] {len(combos)} combos biz={args.biz} repeat={args.repeat}", flush=True)
    t_all = time.monotonic()

    for i, (stem, legacy) in enumerate(combos, 1):
        meta = _CACHE / f"{stem}_meta.json"
        if not meta.is_file():
            print(f"[driver] SKIP {stem}: no meta", flush=True)
            continue
        tag = "legacy" if legacy else "fixed"
        out = _OUT / f"{stem}_{args.biz}_{tag}.json"
        cmd = [
            sys.executable, "-u", str(_REPO / "scripts" / "bench_preview_fluency.py"),
            "--meta", str(meta),
            "--biz", args.biz,
            "--repeat", str(args.repeat),
            "--out", str(out),
        ]
        if legacy:
            cmd.append("--legacy-timing")

        print(f"\n[driver {i}/{len(combos)}] stem={stem} timing={tag} …", flush=True)
        t0 = time.monotonic()
        proc = subprocess.run(cmd, cwd=str(_REPO), capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        el = time.monotonic() - t0
        if proc.returncode != 0:
            print(f"[driver] FAIL rc={proc.returncode}\n{proc.stderr[-2000:]}", flush=True)
            continue

        rec = {"stem": stem, "biz": args.biz, "timing": tag, "driver_wall_s": round(el, 1)}
        if out.is_file():
            obj = json.loads(out.read_text(encoding="utf-8"))
            med = obj.get("median") or {}
            rec.update({
                "grade": obj.get("median_grade"),
                "late1_pct": med.get("late1_pct"),
                "drop_pct": med.get("drop_pct"),
                "seek_count": med.get("seek_count"),
                "discarded": med.get("discarded"),
                "presented": med.get("presented"),
                "catchup_events": med.get("catchup_events"),
                "pace_resets": med.get("pace_resets"),
                "present_ms_avg": med.get("present_ms_avg"),
                "present_ms_p95": med.get("present_ms_p95"),
                "lag_ms_p95": med.get("lag_ms_p95"),
                "wall_s": med.get("wall_s"),
                "realtime_ratio": med.get("realtime_ratio"),
                "runs": obj.get("runs"),
            })
        with jsonl.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"[driver] done {stem}/{tag} in {el:.0f}s "
              f"grade={rec.get('grade')} late1={rec.get('late1_pct')} "
              f"drop={rec.get('drop_pct')}", flush=True)

    print(f"\n[driver] ALL DONE in {(time.monotonic() - t_all) / 60:.1f}min", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
