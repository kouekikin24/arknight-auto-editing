"""A2 补充实验：生产命令形态的单趟成本定位（音频图 vs 视频图 vs 编码）。

背景（A 主实验发现）：-f null 隔离下，平铺 select/setpts 到 1600 段仅 +18s，
N=2623 外推仅 ~263s → 视频滤镜图不是单趟超时的主因。
本实验定位真凶：
  A2a 视频-only 生产形态 N=2623（完整 x264 编码 + 全部 vfr 参数 + mp4 输出），
      cap 4800s，每 30s 采样一次输出文件大小/进度 → 回答"视频-only 单趟真实成本"。
  A2b 音频 atrim/concat 图（fork VFR 分支同款，tick 换算秒）-f null，
      N ∈ {400, 800, 2623}，cap 600s → 验证音频图是否在千段规模爆炸。
产出：A2_production_singlepass_results.json。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".cache"))

import analyzer  # noqa: E402
from exp_batched_seek import load_schedule  # noqa: E402

FFMPEG = (
    ROOT / "tools" / "ffmpeg-7.1.0" / "bundle"
    / "ffmpeg-7.1-essentials_build" / "bin" / "ffmpeg.exe"
)
OUT = ROOT / ".cache" / "research_phase3"
RESULTS: list[dict] = []


def run_with_progress(cmd, out_path: Path | None, cap: float, tag: str, poll: float = 30.0):
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    samples = []
    killed = False
    while True:
        rc = proc.poll()
        if rc is not None:
            break
        wall = time.perf_counter() - t0
        if wall > cap:
            proc.kill()
            proc.wait()
            killed = True
            break
        size = out_path.stat().st_size if out_path and out_path.exists() else 0
        samples.append({"t_s": round(wall, 1), "out_bytes": size})
        time.sleep(poll)
    wall = time.perf_counter() - t0
    status = "TIMEOUT_KILLED" if killed else f"rc={proc.returncode}"
    print(f"    [{tag}] {status}  {wall:8.1f}s", flush=True)
    return rc, wall, killed, samples


def a2a_video_only(src, sched, tag) -> None:
    filt = analyzer._pts_select_setpts_video_filter(sched)
    fpath = OUT / "A2_filter_full_vfr.txt"
    fpath.write_text(filt, "utf-8")
    kept = sum(e - s for _sf, _ef, _st, et in sched)  # 帧数=段帧和（近似）
    kept = sum(ef - sf for sf, ef, _st, _et in sched)
    out = OUT / "A2a_singlepass_v2623.mp4"
    cmd = [
        str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error", "-nostats",
        "-copyts", "-i", str(src),
        "-filter_complex_script", str(fpath),
        "-map", "[outv]",
        "-fps_mode:v", "passthrough",
        "-c:v", "libx264", "-crf", str(analyzer._video_encoder_args(17, False, "")[3]),
        "-pix_fmt", "yuv420p", "-threads", "0",
        "-bf", "0", "-enc_time_base:v", "1/15360", "-video_track_timescale", "15360",
        "-frames:v", str(kept + 1),
        "-movflags", "+faststart", "-an", str(out),
    ]
    print(f"[*] A2a 视频-only 生产形态 N={len(sched)} kept={kept}", flush=True)
    rc, wall, killed, samples = run_with_progress(cmd, out, 4800.0, tag)
    RESULTS.append({"tag": tag, "n": len(sched), "rc": rc, "wall_s": wall,
                    "killed": killed, "samples_tail": samples[-6:],
                    "final_bytes": out.stat().st_size if out.exists() else 0})


def a2b_audio_graph(src, sched, time_base, ns, tag_prefix) -> None:
    for n in ns:
        sub = sched[:n]
        lines, inputs = [], []
        for i, (_sf, _ef, st, et) in enumerate(sub):
            start_seconds = analyzer._fraction_filter_seconds(st * time_base)
            end_seconds = analyzer._fraction_filter_seconds(et * time_base)
            lines.append(
                f"[0:a:0]atrim=start={start_seconds}:end={end_seconds},"
                f"asetpts=PTS-STARTPTS[a{i}]")
            inputs.append(f"[a{i}]")
        lines.append("".join(inputs) + f"concat=n={len(sub)}:v=0:a=1[outa]")
        fpath = OUT / f"A2_filter_audio_{n}.txt"
        fpath.write_text(";\n".join(lines), "utf-8")
        cmd = [
            str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error", "-nostats",
            "-copyts", "-i", str(src),
            "-filter_complex_script", str(fpath),
            "-map", "[outa]", "-f", "null", "-",
        ]
        print(f"[*] A2b 音频图 -f null N={n}", flush=True)
        rc, wall, killed, _ = run_with_progress(cmd, None, 600.0, f"{tag_prefix}_{n}")
        RESULTS.append({"tag": f"{tag_prefix}_{n}", "n": n, "rc": rc,
                        "wall_s": wall, "killed": killed})


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    src, tb, sched = load_schedule(4)
    print(f"[*] 样本4 段数={len(sched)} tb={tb}", flush=True)

    a2b_audio_graph(src, sched, tb, (400, 800, 2623), "s4_audio_null")
    a2a_video_only(src, sched, "s4_video_only_prod_2623")

    (OUT / "A2_production_singlepass_results.json").write_text(
        json.dumps(RESULTS, ensure_ascii=False, indent=2), "utf-8")
    print("[✓] A2 完成", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
