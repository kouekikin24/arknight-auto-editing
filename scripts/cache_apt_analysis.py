#!/usr/bin/env python3
"""A_PT-only analysis cache for preview fluency harness.

Always uses decode_backend=a_pt (ffmpeg_sw_passthrough). Never OpenCV analyze.
Writes reusable pause/speed segs + states/diffs under .cache/preview_fluency/.
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
import time
from pathlib import Path

import cv2
import numpy as np

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import analyzer  # noqa: E402
from frame_types import FRAME_TYPE_0_2X, FRAME_TYPE_1X, FRAME_TYPE_2X  # noqa: E402


def _default_video() -> Path:
    return Path(r"D:\qq下载\920\2.mp4")


def _skip_segs_from_pauses(pause_segments: list, clip_segments: list | None = None) -> list:
    """Mirror preview_player._all_skip_segs_snap (preview-only jump table)."""
    segs: list[tuple[int, int]] = []
    for s in pause_segments:
        mode = s.get("mode", "auto")
        start = int(s["start"])
        if mode == "all":
            segs.append((start, int(s["end"]) + 1))
        elif mode == "auto" and "local_del_mask" in s:
            mask = s["local_del_mask"]
            is_del = False
            del_start = 0
            for i in range(len(mask)):
                delete_this = mask[i] == 1 or mask[i] == 2
                if delete_this and not is_del:
                    is_del = True
                    del_start = start + i
                elif not delete_this and is_del:
                    is_del = False
                    segs.append((del_start, start + i))
            if is_del:
                segs.append((del_start, start + len(mask)))
    for s in clip_segments or []:
        ki, ko = s["keep_in"], s["keep_out"]
        if ki > ko:
            segs.append((s["start"], s["end"] + 1))
        else:
            if ki > s["start"]:
                segs.append((s["start"], ki))
            if ko < s["end"]:
                segs.append((ko + 1, s["end"] + 1))
    return segs


def _speed_segs(speed_segments: list) -> list:
    return [(int(s["start"]), int(s["end"]), int(s["type"])) for s in speed_segments]


def main() -> int:
    ap = argparse.ArgumentParser(description="A_PT analysis cache (never OpenCV)")
    ap.add_argument("--video", type=Path, default=_default_video())
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=_REPO / ".cache" / "preview_fluency",
    )
    ap.add_argument("--proc-w", type=int, default=400)
    ap.add_argument("--proc-h", type=int, default=225)
    ap.add_argument("--batch", type=int, default=128)
    ap.add_argument("--threads", type=int, default=max(1, (analyzer.multiprocessing.cpu_count() or 4)))
    ap.add_argument("--ffmpeg", type=str, default=None, help="ffmpeg path; default auto")
    ap.add_argument(
        "--diagnostics",
        action="store_true",
        help="also save raw pause-template score + per-frame luma to *_pause_score.npy / *_luma.npy",
    )
    args = ap.parse_args()

    video = args.video
    if not video.is_file():
        print(f"FAIL: video not found: {video}", file=sys.stderr)
        return 2

    # Force A_PT — never fall back silently to OpenCV.
    backend = analyzer.normalize_decode_backend("a_pt")
    assert backend == analyzer.DECODE_BACKEND_FFMPEG_SW_PASSTHROUGH, backend
    ffmpeg = analyzer.resolve_ffmpeg_path(args.ffmpeg)
    if not ffmpeg:
        print("FAIL: cannot resolve ffmpeg for A_PT", file=sys.stderr)
        return 2

    cap = cv2.VideoCapture(str(video))
    meta_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    cap.release()

    proc_res = [args.proc_w, args.proc_h]
    if proc_res[1] == 225 and h > 0 and w > 0:
        proc_res[1] = int(proc_res[0] * h / w)
    proc_res_t = (int(proc_res[0]), int(proc_res[1]))

    configs, loaded = analyzer.load_templates(proc_res_t)
    print(
        f"[cache_apt] video={video} frames≈{meta_frames} fps={fps:.3f} "
        f"{w}x{h} proc_res={proc_res_t} templates={loaded} backend={backend} ffmpeg={ffmpeg}"
    )
    if loaded == 0:
        print("WARN: no templates loaded; all frames will be NORMAL", flush=True)

    thresholds = {
        "pause": 0.7,
        "speed_1x": 0.7,
        "speed_2x": 0.7,
        "speed_0_2x": 0.7,
    }
    compare = {
        "still_time_thresh": 0.1,
        "motion_thresh": 2.0,
        "boundary_thresh": 5.0,
    }

    t0 = time.monotonic()
    last_pct = [-1]

    def prog(r: float) -> None:
        pct = int(r * 100)
        if pct != last_pct[0] and (pct % 5 == 0 or pct >= 99):
            last_pct[0] = pct
            print(f"[cache_apt] analyze {pct}%", flush=True)

    states, diffs, context = analyzer.analyze_video_with_context(
        str(video),
        configs,
        thresholds,
        proc_res_t,
        args.batch,
        args.threads,
        prog,
        decode_backend=backend,
        ffmpeg_path=ffmpeg,
        want_diagnostics=args.diagnostics,
    )
    t_an = time.monotonic() - t0
    print(
        f"[cache_apt] analyze done in {t_an:.1f}s  L={len(states)} "
        f"context_complete={bool((context or {}).get('complete'))}",
        flush=True,
    )

    pauses, speeds = analyzer.build_segments(
        states,
        diffs,
        str(video),
        proc_res_t,
        compare,
        fps,
        progress_cb=None,
        analysis_context=context,
    )
    skip_segs = _skip_segs_from_pauses(pauses)
    speed_segs = _speed_segs(speeds)

    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)
    stem = video.stem
    np.save(out / f"{stem}_states.npy", states)
    np.save(out / f"{stem}_diffs.npy", diffs)
    diag = (context or {}).get("_diagnostics") if args.diagnostics else None
    if diag is not None:
        np.save(out / f"{stem}_pause_score.npy", diag["pause_score"])
        np.save(out / f"{stem}_luma.npy", diag["luma"])
        print(
            f"[cache_apt] diagnostics saved: pause_score={len(diag['pause_score'])} "
            f"luma={len(diag['luma'])}",
            flush=True,
        )
    with (out / f"{stem}_pauses.pkl").open("wb") as f:
        pickle.dump(pauses, f, protocol=pickle.HIGHEST_PROTOCOL)
    with (out / f"{stem}_speeds.pkl").open("wb") as f:
        pickle.dump(speeds, f, protocol=pickle.HIGHEST_PROTOCOL)

    # JSON-safe jump tables for harness
    skip_path = out / f"{stem}_skip_segs.json"
    speed_path = out / f"{stem}_speed_segs.json"
    skip_path.write_text(json.dumps(skip_segs), encoding="utf-8")
    speed_path.write_text(json.dumps(speed_segs), encoding="utf-8")

    from collections import Counter

    st_counts = Counter(int(x) for x in states.tolist())
    meta = {
        "video": str(video.resolve()),
        "stem": stem,
        "backend": backend,
        "ffmpeg": ffmpeg,
        "fps": fps,
        "meta_frames": meta_frames,
        "analyzed_frames": int(len(states)),
        "proc_res": list(proc_res_t),
        "templates_loaded": int(loaded),
        "n_pause_segments": len(pauses),
        "n_speed_segments": len(speeds),
        "n_skip_segs": len(skip_segs),
        "skip_frames_sum": int(sum(max(0, b - a) for a, b in skip_segs)),
        "state_counts": {str(k): int(v) for k, v in sorted(st_counts.items())},
        "analyze_s": round(t_an, 2),
        "speedup_defaults": {
            "speedup_1x": True,
            "speedup_02": True,
            "speedup_02_factor": 10,
        },
        "paths": {
            "states": str(out / f"{stem}_states.npy"),
            "diffs": str(out / f"{stem}_diffs.npy"),
            "pauses_pkl": str(out / f"{stem}_pauses.pkl"),
            "speeds_pkl": str(out / f"{stem}_speeds.pkl"),
            "skip_segs": str(skip_path),
            "speed_segs": str(speed_path),
        },
    }
    meta_path = out / f"{stem}_meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    print(f"[cache_apt] wrote {meta_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
