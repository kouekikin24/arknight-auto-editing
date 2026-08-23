"""Real-app preview smoke: VideoPreviewPlayer + mpv engine + real analysis.

Drives the actual Tk player widget (not the bare engine) on the production
sample-4 recording with the cached certification and the real A_PT analysis
(2210 pause / 2212 speed segments, 2623 deleted ranges).  Exercises the daily
path end to end: load, certification bind, play with business speed segments
(auto-ignored for native rendering), frame-number OSD, seek, SOURCE<->EDL
toggle, fluency label, and the startup cover lifecycle.

Exit code 0 = all checks passed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import pickle
from pathlib import Path
import sys
import time
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DEFAULT_DLL_DIR = REPO / "tools" / "libmpv" / "dll"
CACHE = REPO / ".cache" / "preview_fluency"
OUT_ROOT = REPO / ".cache" / "mpv_spike" / "app_smoke"


def _window_stats(image) -> dict:
    import numpy as np

    arr = np.asarray(image.convert("RGB"), dtype=np.uint8)
    flat = arr.reshape(-1, 3)
    step = max(1, len(flat) // 40_000)
    return {
        "distinct_colors": int(len(np.unique(flat[::step], axis=0))),
        "mean_luma": float(
            (0.299 * flat[:, 0] + 0.587 * flat[:, 1] + 0.114 * flat[:, 2]).mean()
        ),
    }


def run(source: Path, dll_dir: Path, out_dir: Path) -> dict:
    import tkinter as tk
    from tkinter import ttk
    from PIL import ImageGrab

    import numpy as np
    import preview_player
    from settings_panel import SettingsPanel
    from media_info import probe_media
    from frame_pts_certifier import produce_frame_pts_certification

    out_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "schema_version": 1,
        "kind": "mpv_app_preview_smoke",
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": str(source),
        "checks": {},
        "status": "fail",
        "reason_codes": [],
    }

    # Certification is a ~13 s multi-gigabyte hash on this source.  Doing it
    # before the Tk window exists keeps the smoke window from freezing white
    # ("not responding") before it ever paints.
    media = probe_media(source)
    outcome = produce_frame_pts_certification(media, decode_threads=4)
    if not outcome.certified:
        result["reason_codes"] = ["CERTIFICATION_FAILED"]
        return result
    certified_media = outcome.media_info
    with (CACHE / "4_pauses.pkl").open("rb") as stream:
        pauses = pickle.load(stream)
    with (CACHE / "4_speeds.pkl").open("rb") as stream:
        speeds = pickle.load(stream)
    states_array = np.load(CACHE / "4_states.npy")

    root = tk.Tk()
    root.title("App preview smoke")
    root.geometry("1280x760+60+40")
    # Mirror main.py's layout: player left (stretch), settings right (360).
    paned = tk.PanedWindow(root, orient=tk.HORIZONTAL, sashwidth=6, sashrelief=tk.RAISED, bg="#555555")
    paned.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)
    left_frame = ttk.Frame(paned)
    paned.add(left_frame, stretch="always", minsize=500)
    right_frame = ttk.Frame(paned)
    paned.add(right_frame, stretch="never", minsize=240, width=360)
    settings = SettingsPanel(right_frame)
    settings.pack(fill=tk.BOTH, expand=True)
    os.environ.setdefault("MPV_DLL_DIR", str(dll_dir))
    player = preview_player.VideoPreviewPlayer(
        left_frame, settings, str(source), preview_engine="mpv"
    )
    player.pack(fill=tk.BOTH, expand=True)

    def pump(seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            root.update()
            time.sleep(0.01)

    def grab_window():
        root.update_idletasks()
        root.update()
        x, y = root.winfo_rootx(), root.winfo_rooty()
        w, h = root.winfo_width(), root.winfo_height()
        full = ImageGrab.grab()
        scale = full.size[0] / root.winfo_screenwidth()
        return ImageGrab.grab(
            bbox=(int(x * scale), int(y * scale), int((x + w) * scale), int((y + h) * scale))
        )

    failures: list[str] = []
    try:
        engine = player._io
        result["engine"] = {
            "class": type(engine).__name__,
            "native_rendering": bool(getattr(engine, "native_rendering", False)),
        }
        if type(engine).__name__ != "MpvEngine":
            result["reason_codes"] = ["MPV_ENGINE_NOT_SELECTED"]
            return result

        # Bind certification + real analysis the way the app tasks would.
        player.media_info = certified_media
        player.frame_pts_error = None
        player.frame_pts_status = "AUTHORITATIVE"
        engine.bind_media_info(certified_media)
        player.total_frames = int(certified_media.frame_pts_certification.frame_count)
        # Publish the analysis through the app's own owner-state path; the
        # cut plan is derived from project_state, not the public lists.
        clips = player._build_clip_segments(pauses, player.total_frames)
        snapshot = player.project_state.replace_timeline(pauses, speeds, clips)
        player.task_manager.invalidate_scope(
            project_generation=snapshot.project_generation,
            timeline_revision=snapshot.timeline_revision,
        )
        player._publish_project_snapshot(snapshot)
        player.states_array = states_array
        player._cut_plan_cache = None

        # -- startup cover lifecycle --------------------------------------
        pump(0.05)
        cover_at_start = player._native_cover is not None
        pump(3.5)
        cover_gone = player._native_cover is None
        result["checks"]["startup_cover"] = {
            "raised_at_start": cover_at_start,
            "removed_after_load": cover_gone,
        }
        if not cover_gone:
            failures.append("STARTUP_COVER_STUCK")

        # -- play with real speed segments (auto-ignore path) -------------
        t0 = time.perf_counter()
        player.toggle_play()
        play_call_s = round(time.perf_counter() - t0, 3)
        accepted = player.is_playing
        info_text = str(player.lbl_info.cget("text"))
        pump(8.0)
        snap = engine.snapshot_perf()
        window_image = grab_window()
        window_image.save(out_dir / "window_play.png")
        stats = _window_stats(window_image)
        perf_text = str(player.lbl_perf.cget("text"))
        result["checks"]["play_with_speed_segments"] = {
            "accepted": bool(accepted),
            "ui_play_call_seconds": play_call_s,
            "speed_segment_count": len(speeds),
            "pause_segment_count": len(pauses),
            "engine_mode": engine.mode,
            "presented": int(snap.get("presented", 0)),
            "rt_ratio": snap.get("rt_ratio"),
            "mpv_frame_drops": snap.get("mpv_frame_drops"),
            "distinct_colors": stats["distinct_colors"],
            "mean_luma": round(stats["mean_luma"], 1),
            "info_text": info_text,
            "perf_text": perf_text,
        }
        if not accepted or engine.mode != "edl":
            failures.append("PLAY_REJECTED_OR_WRONG_MODE")
        if play_call_s > 1.0:
            failures.append("PLAY_CALL_BLOCKED_UI")
        if stats["distinct_colors"] <= 8:
            failures.append("WINDOW_BLANK_DURING_PLAY")
        if "mpv丢帧" not in perf_text or "实测比" not in perf_text:
            failures.append("FLUENCY_LABEL_MISSING_MPV_FIELDS")
        if "变速段按原速" not in info_text:
            failures.append("SPEED_POLICY_HINT_MISSING")

        # -- frame OSD (persistent top-left overlay) ------------------------
        osd_ok = bool(engine.show_osd_topleft_text("帧号OSD自检 12345"))
        result["checks"]["frame_osd"] = {"command_accepted": osd_ok}
        if not osd_ok:
            failures.append("OSD_COMMAND_REJECTED")
        pump(0.4)

        # -- corner FPS overlay ---------------------------------------------
        corner_ok = bool(engine.show_osd_corner_text("59.9 FPS"))
        pump(0.6)
        corner_image = grab_window()
        w, h = corner_image.size
        # 干净文本应只在右上角一小条；若再出现 Dialogue 长串会横向铺满顶部
        tr = corner_image.crop((int(w * 0.7), 0, w, int(h * 0.12))).convert("L")
        tl = corner_image.crop((int(w * 0.3), 0, int(w * 0.7), int(h * 0.12))).convert("L")
        bright_tr = sum(1 for v in tr.getdata() if v > 140)
        bright_midtop = sum(1 for v in tl.getdata() if v > 140)
        result["checks"]["corner_fps_osd"] = {
            "command_accepted": corner_ok,
            "top_right_bright_pixels": bright_tr,
            "top_middle_bright_pixels": bright_midtop,
        }
        if not corner_ok or bright_tr <= 40:
            failures.append("CORNER_FPS_OSD_MISSING")
        # Dialogue 前缀泄漏会横跨顶部中间区域，出现大片亮字
        if bright_midtop > 400:
            failures.append("CORNER_FPS_OSD_DIALOGUE_LEAK")
        engine.show_osd_corner_text("")
        engine.show_osd_topleft_text("")

        # -- EDL-mode frame stepping must stay in EDL -----------------------
        player._stop_playback_ui(from_user=True)
        pump(0.5)
        mode_before_step = engine.mode
        player._on_key_press_right(SimpleNamespace())
        player._on_key_release(SimpleNamespace(keysym="Right"))
        pump(1.0)
        mode_after_step = engine.mode
        frame_after_step = engine.snapshot_perf().get("source_frame")
        result["checks"]["edl_frame_step"] = {
            "mode_before": mode_before_step,
            "mode_after": mode_after_step,
            "source_frame_after_step": frame_after_step,
        }
        if mode_after_step != "edl":
            failures.append("EDL_FRAME_STEP_LEFT_EDL_MODE")

        # -- seek then toggle to SOURCE view --------------------------------
        # 逐帧后引擎可能停在源帧 0 或某个裁剪段内：先起播一次，把 EDL
        # 视图推进到可见内容，再检查切换到 SOURCE 的真实路径。
        player.toggle_play()
        pump(2.0)
        player._seek(180_000)
        # 播放中寻址后画面仍在推进；先停播再读帧号做容差判定
        pump(0.3)
        player._stop_playback_ui(from_user=True)
        pump(1.0)
        frame_after_seek = engine.snapshot_perf().get("source_frame")
        player.toggle_play()
        pump(0.8)
        player.skip_trimmed.set(False)
        player._on_preview_option_change()
        pump(2.5)
        mode_after_toggle = engine.mode
        player._stop_playback_ui(from_user=True)
        pump(0.5)
        result["checks"]["seek_and_source_toggle"] = {
            "requested_frame": 180_000,
            "observed_source_frame": frame_after_seek,
            "mode_after_toggle": mode_after_toggle,
        }
        if not isinstance(frame_after_seek, int) or abs(frame_after_seek - 180_000) > 90:
            failures.append("SEEK_LANDING_OFF")
        if mode_after_toggle != "source":
            failures.append("SOURCE_TOGGLE_FAILED")

        # -- back to EDL ----------------------------------------------------
        player.skip_trimmed.set(True)
        # 播放已停：选项变更不会重发播放命令，直接重新起播进 EDL。
        player.toggle_play()
        pump(2.5)
        result["checks"]["back_to_edl"] = {"mode": engine.mode}
        if engine.mode != "edl":
            failures.append("EDL_TOGGLE_FAILED")
    finally:
        try:
            player.close(timeout=3.0)
        except Exception:
            pass
        try:
            settings.task_manager.close(timeout=1.0)
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass

    result["failures"] = failures
    result["status"] = "pass" if not failures else "fail"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--dll-dir", type=Path, default=DEFAULT_DLL_DIR)
    parser.add_argument("--out-dir", type=Path, default=OUT_ROOT)
    args = parser.parse_args(argv)

    if not args.source.is_file():
        print(f"source missing: {args.source}", file=sys.stderr)
        return 2
    out_dir = args.out_dir / datetime.now().strftime("%Y%m%d-%H%M%S")
    result = run(args.source, args.dll_dir, out_dir)
    report = out_dir / "app_smoke.json"
    report.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "status": result["status"],
        "failures": result.get("failures"),
        "report": str(report),
    }, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
