#!/usr/bin/env python3
"""Headless preview fluency bench — drives VideoIOThread like the GUI play path.

Loads A_PT-produced skip/speed segs from .cache/preview_fluency/.
Does not open Tk. Drains frame_q so the IO thread is not blocked on a full queue.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from pathlib import Path
from queue import Empty, Queue

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from video_io import (  # noqa: E402
    CMD_PLAY,
    CMD_QUIT,
    CMD_STOP,
    VideoIOThread,
)


def _grade(late1_pct: float, drop_pct: float, avg_ms: float, rt: float) -> str:
    # Mirror preview_player._grade_smoothness
    if late1_pct <= 3 and drop_pct <= 1 and avg_ms <= 25 and (rt == 0 or rt <= 1.05):
        return "优"
    if late1_pct <= 10 and drop_pct <= 5 and avg_ms <= 40 and (rt == 0 or rt <= 1.15):
        return "良"
    if late1_pct <= 25 and drop_pct <= 15:
        return "中"
    return "差"


def _realtime_ratio(snap: dict) -> float:
    """Match preview_player: wall / ((presented+discarded) * ideal_gap)."""
    wall_s = float(snap.get("wall_s", 0.0) or 0.0)
    presented = int(snap.get("presented", 0) or 0)
    discarded = int(snap.get("discarded", 0) or 0)
    fps = float(snap.get("_fps", 60.0) or 60.0)
    ideal_gap = 1.0 / max(fps, 1e-6)
    media_s = (presented + discarded) * ideal_gap
    if media_s <= 1e-3:
        return 0.0
    return wall_s / media_s


def _format_line(snap: dict, state: str = "停") -> str:
    late_pct = float(snap.get("late_pct", 0.0))
    late1_pct = float(snap.get("late1_pct", 0.0))
    late2_pct = float(snap.get("late2_pct", 0.0))
    drop_pct = float(snap.get("drop_pct", 0.0))
    avg_ms = float(snap.get("present_ms_avg", 0.0))
    p95_ms = float(snap.get("present_ms_p95", 0.0))
    max_ms = float(snap.get("present_ms_max", 0.0))
    lag_p95 = float(snap.get("lag_ms_p95", 0.0))
    lag_max = float(snap.get("lag_ms_max", 0.0))
    wall_s = float(snap.get("wall_s", 0.0) or 0.0)
    presented = int(snap.get("presented", 0) or 0)
    discarded = int(snap.get("discarded", 0) or 0)
    catchup = int(snap.get("catchup_events", 0) or 0)
    soft = int(snap.get("pace_resets", 0) or 0)
    hard = int(snap.get("hard_resets", 0) or 0)
    seek = int(snap.get("seek_count", 0) or 0)
    q_drop = int(snap.get("q_drop", 0) or 0)
    absorbed = int(snap.get("skip_trim_absorbed", 0) or 0)
    threads = int(snap.get("cap_threads", 0) or 0)
    pace = str(snap.get("pace_mode") or "opt")
    mode_tag = "优化" if pace == "opt" else "基线"
    rt = _realtime_ratio(snap)
    grade = _grade(late1_pct, drop_pct, avg_ms, rt)
    spikes = snap.get("spikes") or []
    tip = ""
    if spikes:
        sp = spikes[-1]
        tip = (
            f" | 末尖峰f{sp.get('frame')} {sp.get('present_ms')}ms"
            f"[{','.join(sp.get('reasons') or [])}]"
        )
    return (
        f"流畅度[{state}/{mode_tag}]{grade} | "
        f"微抖{late_pct:.0f}% >1帧{late1_pct:.0f}% >2帧{late2_pct:.0f}% | "
        f"追帧{discarded}拍/{catchup}次 | "
        f"seek{seek} 软锚{soft} 硬重置{hard} | "
        f"解码{avg_ms:.0f}/p95 {p95_ms:.0f}/max{max_ms:.0f}ms | "
        f"落后p95 {lag_p95:.0f}/max{lag_max:.0f}ms | "
        f"实时比{rt:.2f} | "
        f"q丢{q_drop} | 吸收{absorbed} 线程{threads} | "
        f"{wall_s:.1f}s{tip}"
    )


def _drain_frames(frame_q: Queue, stop_evt: threading.Event, stats: dict) -> None:
    while not stop_evt.is_set():
        try:
            item = frame_q.get(timeout=0.05)
        except Empty:
            continue
        stats["drained"] = int(stats.get("drained", 0)) + 1
        stats["last_idx"] = item[0] if isinstance(item, tuple) else None


def run_once(
    video: Path,
    skip_segs: list,
    speed_segs: list,
    *,
    pace_mode: str,
    ignore_biz: bool,
    speedup_1x: bool,
    speedup_02: bool,
    speedup_02_factor: int,
    preview_step: int,
    canvas_wh: tuple[int, int],
    start_frame: int,
    max_wall_s: float | None,
    preview_step_cap: int,
    legacy_timing: bool = False,
) -> dict:
    frame_q: Queue = Queue(maxsize=4)
    io = VideoIOThread(str(video), frame_q, legacy_timing=legacy_timing)
    io.start()
    # Let cap open settle
    time.sleep(0.05)
    io.set_pace_mode(pace_mode)

    stop_evt = threading.Event()
    drain_stats: dict = {}
    drainer = threading.Thread(
        target=_drain_frames, args=(frame_q, stop_evt, drain_stats), daemon=True
    )
    drainer.start()

    params = {
        "start_frame": int(start_frame),
        "preview_step": int(preview_step),
        "speed_multiplier": 1.0,
        "skip_trimmed": True,
        "speedup_1x": False if ignore_biz else bool(speedup_1x),
        "speedup_02": False if ignore_biz else bool(speedup_02),
        "speedup_02_factor": int(speedup_02_factor),
        "pause_segs": skip_segs,
        "speed_segs": speed_segs,
        "canvas_wh": canvas_wh,
        "preview_step_cap": int(preview_step_cap),
        "skip_trim_min_span": 0,
    }
    io.send({"type": CMD_PLAY, "params": params})

    t_start = time.monotonic()
    last_print = t_start
    try:
        while True:
            time.sleep(0.2)
            now = time.monotonic()
            if max_wall_s is not None and (now - t_start) >= max_wall_s:
                io.send({"type": CMD_STOP})
                # wait stats freeze
                time.sleep(0.3)
                break
            if not io.is_playback_active():
                # small grace for final present
                time.sleep(0.15)
                if not io.is_playback_active():
                    break
            if now - last_print >= 10.0:
                snap = io.snapshot_perf()
                snap["_fps"] = float(io.fps or 60.0)
                print(
                    f"  … wall={snap.get('wall_s', 0):.0f}s "
                    f"presented={snap.get('presented')} "
                    f"seek={snap.get('seek_count')} "
                    f"late1={snap.get('late1_pct', 0):.1f}%",
                    flush=True,
                )
                last_print = now
    finally:
        try:
            io.send({"type": CMD_STOP})
        except Exception:
            pass
        time.sleep(0.2)
        snap = io.snapshot_perf()
        snap["_fps"] = float(io.fps or 60.0)
        snap["_drained"] = int(drain_stats.get("drained", 0))
        snap["_last_idx"] = drain_stats.get("last_idx")
        snap["_total"] = int(io.total or 0)
        try:
            io.stop_and_quit()
        except Exception:
            pass
        stop_evt.set()
        io.join(timeout=3.0)
        drainer.join(timeout=1.0)
    return snap


def main() -> int:
    ap = argparse.ArgumentParser(description="Headless preview fluency bench")
    ap.add_argument(
        "--meta",
        type=Path,
        default=_REPO / ".cache" / "preview_fluency" / "2_meta.json",
        help="A_PT cache meta json from cache_apt_analysis.py",
    )
    ap.add_argument("--video", type=Path, default=None, help="override video path")
    ap.add_argument("--pace", choices=("opt", "base"), default="opt")
    ap.add_argument(
        "--biz",
        choices=("on", "off"),
        default="on",
        help="business speedup on/off (off == ignore biz)",
    )
    ap.add_argument("--preview-step", type=int, default=1)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--max-wall-s", type=float, default=None, help="optional cap")
    ap.add_argument("--canvas", type=str, default="1280x720")
    ap.add_argument("--cap", type=int, default=3, help="preview_step_cap S1")
    ap.add_argument("--out", type=Path, default=None, help="write full snap json")
    ap.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="run N times and report per-run values + median (HANDOFF 9.0)",
    )
    ap.add_argument(
        "--legacy-timing",
        action="store_true",
        help="reproduce pre-fix timing (monotonic 15.6ms clock + cmd_q.get(timeout=) sleep)",
    )
    args = ap.parse_args()

    if not args.meta.is_file():
        print(f"FAIL: meta not found: {args.meta}", file=sys.stderr)
        print("Run: python scripts/cache_apt_analysis.py", file=sys.stderr)
        return 2
    meta = json.loads(args.meta.read_text(encoding="utf-8"))
    video = Path(args.video) if args.video else Path(meta["video"])
    if not video.is_file():
        print(f"FAIL: video not found: {video}", file=sys.stderr)
        return 2

    skip_segs = json.loads(Path(meta["paths"]["skip_segs"]).read_text(encoding="utf-8"))
    speed_segs = json.loads(Path(meta["paths"]["speed_segs"]).read_text(encoding="utf-8"))
    sp = meta.get("speedup_defaults") or {}
    cw, ch = (int(x) for x in args.canvas.lower().split("x"))

    ignore_biz = args.biz == "off"
    print(
        f"[bench] video={video.name} pace={args.pace} biz={args.biz} "
        f"skip_segs={len(skip_segs)} speed_segs={len(speed_segs)} "
        f"analyzed={meta.get('analyzed_frames')} backend={meta.get('backend')}",
        flush=True,
    )
    n = max(1, int(args.repeat))
    runs: list[dict] = []
    t_all = time.perf_counter()
    for i in range(n):
        t0 = time.perf_counter()
        snap = run_once(
            video,
            skip_segs,
            speed_segs,
            pace_mode=args.pace,
            ignore_biz=ignore_biz,
            speedup_1x=bool(sp.get("speedup_1x", True)),
            speedup_02=bool(sp.get("speedup_02", True)),
            speedup_02_factor=int(sp.get("speedup_02_factor", 10)),
            preview_step=args.preview_step,
            canvas_wh=(cw, ch),
            start_frame=args.start,
            max_wall_s=args.max_wall_s,
            preview_step_cap=args.cap,
            legacy_timing=args.legacy_timing,
        )
        snap["_run_idx"] = i + 1
        snap["_run_wall_clock"] = time.perf_counter() - t0
        runs.append(snap)
        print(f"[run {i+1}/{n}] " + _format_line(snap, state="停"), flush=True)

    # ---- 每次运行的原始值全部打印，不做挑选 ----
    keys = (
        "late1_pct",
        "drop_pct",
        "present_ms_avg",
        "present_ms_p95",
        "lag_ms_p95",
        "seek_count",
        "discarded",
        "presented",
        "pace_resets",
        "catchup_events",
        "wall_s",
    )
    print(f"\n=== raw values across {n} run(s) ===", flush=True)
    print(f"{'metric':>16} | " + " | ".join(f"run{i+1:>8}" for i in range(n)) + " |   median", flush=True)
    agg: dict = {}
    for k in keys:
        vals = [float(r.get(k, 0.0) or 0.0) for r in runs]
        med = statistics.median(vals)
        agg[k] = med
        cells = " | ".join(f"{v:12.3f}" for v in vals)
        print(f"{k:>16} | {cells} | {med:9.3f}", flush=True)
    rts = [_realtime_ratio(r) for r in runs]
    agg["realtime_ratio"] = statistics.median(rts)
    print(
        f"{'realtime_ratio':>16} | "
        + " | ".join(f"{v:12.3f}" for v in rts)
        + f" | {agg['realtime_ratio']:9.3f}",
        flush=True,
    )
    grades = [
        _grade(
            float(r.get("late1_pct", 0.0)),
            float(r.get("drop_pct", 0.0)),
            float(r.get("present_ms_avg", 0.0)),
            _realtime_ratio(r),
        )
        for r in runs
    ]
    print(f"{'grade':>16} | " + " | ".join(f"{g:>12}" for g in grades) + " |", flush=True)

    med_grade = _grade(
        agg["late1_pct"], agg["drop_pct"], agg["present_ms_avg"], agg["realtime_ratio"]
    )
    timing_tag = "legacy(monotonic+get_timeout)" if args.legacy_timing else "fixed(perf_counter+sleep)"
    print(
        f"\n[MEDIAN] video={video.name} biz={args.biz} timing={timing_tag} n={n} "
        f"grade={med_grade} late1={agg['late1_pct']:.2f}% drop={agg['drop_pct']:.2f}% "
        f"seek={agg['seek_count']:.0f} discarded={agg['discarded']:.0f} "
        f"decode_avg={agg['present_ms_avg']:.2f}ms rt={agg['realtime_ratio']:.3f}",
        flush=True,
    )
    print("last-run spikes:", flush=True)
    for spk in runs[-1].get("spikes") or []:
        print(
            f"  f{spk.get('frame')} present={spk.get('present_ms')}ms "
            f"lag={spk.get('lag_ms')}ms reasons={spk.get('reasons')}",
            flush=True,
        )
    print(f"[bench] total_wall_clock={time.perf_counter() - t_all:.1f}s", flush=True)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        out_obj = {
            "video": str(video),
            "biz": args.biz,
            "pace": args.pace,
            "legacy_timing": bool(args.legacy_timing),
            "timing": timing_tag,
            "repeat": n,
            "median": agg,
            "median_grade": med_grade,
            "per_run_grades": grades,
            "runs": runs,
        }
        args.out.write_text(
            json.dumps(out_obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print(f"[bench] wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
