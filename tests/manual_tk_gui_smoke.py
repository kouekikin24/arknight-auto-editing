"""Manual, real-Tk lifecycle smoke harness.

This file is intentionally named ``manual_*`` so normal pytest collection does
not execute it.  It uses the real ``SettingsPanel``, ``VideoPreviewPlayer``,
``VideoIOThread`` and shared ``TaskManager``.  Only expensive analysis/export
workers are replaced for the cancellation scenarios.

Run from the repository root (Windows):

    python tests/manual_tk_gui_smoke.py

The script supplies tiny MJPEG/AVI fixtures when no paths are given.  Set
``TCL_LIBRARY``/``TK_LIBRARY`` when using a Python distribution that cannot
discover its bundled Tcl files automatically.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import threading
import time
import traceback

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import tkinter as tk  # noqa: E402
from tkinter import messagebox  # noqa: E402

import analyzer  # noqa: E402
import preview_player  # noqa: E402
import settings_panel  # noqa: E402
from frame_types import FRAME_TYPE_NORMAL  # noqa: E402
from settings_panel import SettingsPanel  # noqa: E402
from preview_player import VideoPreviewPlayer  # noqa: E402
from task_manager import TaskCancelled, TaskManager  # noqa: E402
from video_io import VideoIOThread  # noqa: E402


class SmokeFailure(AssertionError):
    pass


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def _make_clip(path: Path, *, fps: int, frames: int, color: tuple[int, int, int], label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (160, 96)
    )
    _check(writer.isOpened(), f"cannot create fixture {path}")
    try:
        for idx in range(frames):
            frame = np.full((96, 160, 3), color, dtype=np.uint8)
            x = 8 + (idx * 3) % 136
            cv2.rectangle(frame, (x, 12), (x + 16, 28), (255, 255, 255), -1)
            cv2.putText(
                frame,
                f"{label}{idx:02d}",
                (8, 80),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.45,
                (255, 255, 255),
                1,
                cv2.LINE_AA,
            )
            writer.write(frame)
    finally:
        writer.release()
    _check(path.is_file() and path.stat().st_size > 0, f"empty fixture {path}")


def _pump(root: tk.Misc, seconds: float, predicate=None) -> bool:
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        try:
            root.update()
        except tk.TclError as exc:
            raise SmokeFailure(f"unexpected TclError while pumping Tk: {exc}") from exc
        if predicate is not None and predicate():
            return True
        time.sleep(0.005)
    try:
        root.update()
    except tk.TclError as exc:
        raise SmokeFailure(f"unexpected TclError while pumping Tk: {exc}") from exc
    return bool(predicate and predicate())


def _partial_files(directory: Path, *, recursive: bool = False) -> list[Path]:
    if not directory.exists():
        return []
    candidates = directory.rglob("*") if recursive else directory.iterdir()
    return sorted(
        p for p in candidates
        if p.is_file() and (".partial" in p.name or ".video-only.tmp" in p.name)
    )


def _alive_video_io_threads() -> list[VideoIOThread]:
    return [
        thread
        for thread in threading.enumerate()
        if isinstance(thread, VideoIOThread) and thread.is_alive()
    ]


def _close_like_main(root: tk.Tk, tasks: TaskManager, player: VideoPreviewPlayer, settings: SettingsPanel) -> list[str]:
    """Exercise the same bounded close ordering as ``main.close_app``."""
    tasks.cancel_all()
    io_closed = player.close(timeout=1.5)
    settings.close()
    survivors = tasks.close(timeout=2.0)
    _check(io_closed, "VideoIOThread did not close before the GUI deadline")
    try:
        root.destroy()
    except tk.TclError:
        pass
    return survivors


def run(work_dir: Path, *, real_exports: bool = True) -> dict:
    work_dir.mkdir(parents=True, exist_ok=True)
    initial_partial_files = {
        path.resolve() for path in _partial_files(work_dir, recursive=True)
    }
    video_a = work_dir / "tiny_a_60f.avi"
    video_b = work_dir / "tiny_b_48f.avi"
    if not video_a.exists():
        _make_clip(video_a, fps=30, frames=60, color=(30, 80, 150), label="A")
    if not video_b.exists():
        _make_clip(video_b, fps=24, frames=48, color=(130, 50, 30), label="B")

    # GPU probing is not the subject of this smoke.  Keep the real Tk/task
    # path, but make the probe deterministic and immediate.
    old_probe = settings_panel._probe_gpu_encoders
    settings_panel._probe_gpu_encoders = lambda _path, _context: ([], [])
    original_analysis_worker = preview_player._run_analysis_task

    dialogs: list[tuple[str, str]] = []
    old_info = messagebox.showinfo
    old_warning = messagebox.showwarning
    old_error = messagebox.showerror
    old_yesno = messagebox.askyesno
    messagebox.showinfo = lambda title, text, **_kw: dialogs.append(("info", str(title)))
    messagebox.showwarning = lambda title, text, **_kw: dialogs.append(("warning", str(title)))
    messagebox.showerror = lambda title, text, **_kw: dialogs.append(("error", str(title)))
    messagebox.askyesno = lambda *_args, **_kw: True

    thread_errors: list[str] = []
    old_thread_hook = threading.excepthook
    threading.excepthook = lambda args: thread_errors.append(
        "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
    )

    root = tk.Tk()
    tk_callback_errors: list[str] = []

    def record_tk_callback_error(exc_type, exc_value, exc_traceback):
        tk_callback_errors.append(
            "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        )

    root.report_callback_exception = record_tk_callback_error
    root.title("manual Tk smoke")
    root.geometry("900x600")
    root.withdraw()  # real Tk lifecycle, driven below without desktop focus
    tasks = TaskManager(root, max_workers=2, poll_interval_ms=10)
    settings = SettingsPanel(root, task_manager=tasks)
    settings.pack(fill=tk.BOTH, expand=True)
    player = VideoPreviewPlayer(root, settings=settings, task_manager=tasks)
    player.pack(fill=tk.BOTH, expand=True)
    root.update_idletasks()
    _pump(root, 0.4, lambda: settings._gpu_probe_handle is None)

    result: dict[str, object] = {"video_a": str(video_a), "video_b": str(video_b)}
    try:
        # ------------------------------------------------------------------
        # 1) Real VideoIO load/switch path.
        # ------------------------------------------------------------------
        _check(player.load_video(str(video_a)) is True, "load A failed")
        first_io = player._io
        _check(first_io is not None, "load A did not create VideoIOThread")
        _check(
            _pump(root, 1.2, lambda: player._photo is not None),
            "A never produced a Tk frame",
        )
        _check(first_io.is_alive(), "A decoder exited unexpectedly")
        _check(player.total_frames == 60, f"A frame count mismatch: {player.total_frames}")

        _check(player.load_video(str(video_b)) is True, "load B failed")
        second_io = player._io
        _check(second_io is not None and second_io is not first_io, "switch did not replace decoder")
        _check(not first_io.is_alive(), "old decoder survived video switch")
        _check(
            _pump(
                root,
                1.2,
                lambda: (
                    second_io.is_alive()
                    and player.total_frames == 48
                    and player._photo is not None
                ),
            ),
            "B decoder did not settle and render a frame",
        )
        _check(len(_alive_video_io_threads()) == 1, "video switch left multiple live decoders")
        result["load_switch"] = "passed"

        # ------------------------------------------------------------------
        # 2) Analysis in flight, then replace source video.  The fake worker
        # deliberately returns after cancellation; generation suppression must
        # prevent its result from reaching the new source's UI.
        # ------------------------------------------------------------------
        analysis_started = threading.Event()
        analysis_cancel_seen = threading.Event()
        analysis_finished = threading.Event()

        def slow_analysis(context, snapshot):
            analysis_started.set()
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not context.cancelled:
                time.sleep(0.005)
            if context.cancelled:
                analysis_cancel_seen.set()
            # Return even after cancellation to prove stale completions are
            # dropped rather than relying only on cooperative worker exit.
            time.sleep(0.08)
            analysis_finished.set()
            states = np.full(60, FRAME_TYPE_NORMAL, dtype=np.int8)
            return states, np.zeros(60), [], [], "fake", False, False

        preview_player._run_analysis_task = slow_analysis
        player.load_video(str(video_a))
        player._start_analysis()
        _check(_pump(root, 1.0, analysis_started.is_set), "analysis did not start")
        player.load_video(str(video_b))
        _check(_pump(root, 1.5, analysis_finished.is_set), "cancelled analysis did not finish")
        _check(analysis_cancel_seen.is_set(), "source switch did not cancel analysis")
        _check(player.video_path == str(video_b), "new source was lost after analysis switch")
        _check(player.states_array is None, "stale analysis result reached new source")
        _check(len(_alive_video_io_threads()) == 1, "analysis switch left multiple live decoders")
        result["analysis_switch"] = "passed"
        preview_player._run_analysis_task = original_analysis_worker

        # ------------------------------------------------------------------
        # 3) Real tiny full + segment export success paths.  This is optional
        # because FFmpeg/imageio availability varies across developer machines.
        # ------------------------------------------------------------------
        if real_exports:
            player.load_video(str(video_a))
            settings.export_keep_audio_var.set(False)
            full_output = work_dir / "real_full.mp4"
            full_output.unlink(missing_ok=True)
            settings.output_var.set(str(full_output))
            dialogs.clear()
            player.export_video()
            _check(
                _pump(root, 20.0, lambda: player._export_handle is None),
                "real full export timed out",
            )
            _check(full_output.is_file() and full_output.stat().st_size > 0, "real full export missing")
            _check(not any(kind == "error" for kind, _ in dialogs), "real full export reported an error")

            run_token = f"{os.getpid()}_{time.time_ns()}"
            segment_output = work_dir / f"real_segments_{run_token}.mp4"
            settings.output_var.set(str(segment_output))
            player.states_array = np.zeros(player.total_frames, dtype=np.int8)
            player.export_segments()
            _check(
                _pump(root, 20.0, lambda: player._segment_export_handle is None),
                "real segment export timed out",
            )
            segment_dir = work_dir / f"real_segments_{run_token}_segments"
            segment_files = list(segment_dir.glob("*.mp4")) if segment_dir.exists() else []
            _check(segment_files and all(p.stat().st_size > 0 for p in segment_files), "real segment export missing")
            _check(not _partial_files(segment_dir), "real segment export left partial files")
            result["real_exports"] = {
                "full_bytes": full_output.stat().st_size,
                "segment_files": len(segment_files),
            }

        # ------------------------------------------------------------------
        # 4) Export in flight, then close the window.  Keep the production
        # atomic wrapper; replace only the expensive inner writer with a
        # controllable worker so the close race is deterministic.
        # ------------------------------------------------------------------
        player.load_video(str(video_a))
        cancel_target = work_dir / "cancel_target.mp4"
        cancel_target.write_bytes(b"ORIGINAL-SENTINEL")
        settings.output_var.set(str(cancel_target))
        settings.export_keep_audio_var.set(False)
        export_started = threading.Event()
        export_finished = threading.Event()
        old_preflight = analyzer.inspect_export_plan
        old_impl = analyzer._export_video_impl
        analyzer.inspect_export_plan = lambda *_a, **_kw: {
            "audio_drop_requires_confirmation": False,
            "n_ranges": 1,
            "audio_limit": 100,
            "audio_drop_reasons": [],
        }

        def slow_impl(video_path, output_path, *_args, cancel_cb=None, **_kwargs):
            Path(output_path).write_bytes(b"PARTIAL-STAGING")
            export_started.set()
            try:
                while True:
                    if cancel_cb is not None:
                        cancel_cb()
                    time.sleep(0.005)
            except TaskCancelled:
                raise
            finally:
                export_finished.set()

        analyzer._export_video_impl = slow_impl
        try:
            player.export_video()
            _check(_pump(root, 1.5, export_started.is_set), "slow export did not start")
            survivors = _close_like_main(root, tasks, player, settings)
        finally:
            analyzer.inspect_export_plan = old_preflight
            analyzer._export_video_impl = old_impl
        _check(export_finished.is_set(), "export worker did not stop during close")
        _check(not survivors, f"background survivors after close: {survivors}")
        _check(not _alive_video_io_threads(), "VideoIOThread survived GUI close")
        _check(cancel_target.read_bytes() == b"ORIGINAL-SENTINEL", "cancelled export overwrote original output")
        new_partial_files = {
            path.resolve() for path in _partial_files(work_dir, recursive=True)
        } - initial_partial_files
        _check(
            not new_partial_files,
            f"smoke left staging files: {sorted(map(str, new_partial_files))}",
        )
        result["export_close"] = "passed"
        result["dialogs"] = dialogs
        result["tk_callback_errors"] = tk_callback_errors
        result["thread_errors"] = thread_errors
        _check(not tk_callback_errors, "unhandled Tk callback exception observed")
        _check(not thread_errors, "unhandled worker thread exception observed")
        return result
    finally:
        preview_player._run_analysis_task = original_analysis_worker
        settings_panel._probe_gpu_encoders = old_probe
        messagebox.showinfo = old_info
        messagebox.showwarning = old_warning
        messagebox.showerror = old_error
        messagebox.askyesno = old_yesno
        threading.excepthook = old_thread_hook
        try:
            root_exists = bool(root.winfo_exists())
        except tk.TclError:
            root_exists = False
        if root_exists:
            try:
                _close_like_main(root, tasks, player, settings)
            except Exception:
                try:
                    root.destroy()
                except Exception:
                    pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path("C:/tmp/arknight_gui_smoke_harness"),
    )
    parser.add_argument(
        "--skip-real-export",
        action="store_true",
        help="skip the tiny real full/segment export success checks",
    )
    args = parser.parse_args()
    try:
        result = run(args.work_dir, real_exports=not args.skip_real_export)
    except Exception as exc:
        print(f"SMOKE_FAIL: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return 1
    print("SMOKE_PASS")
    for key, value in result.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
