"""G6 gate: libmpv lifecycle and distribution evidence on a real window.

G6 asks whether the preview engine can be created, started, and torn down
repeatedly without leaking OS resources or locking the project-owned DLL, and
whether the bundled runtime is distributable.  It collects:

- Repeated create/start/close cycles of the real MpvEngine (real binding,
  real WID), asserting close() succeeds and is_alive() drops each cycle.
- Process handle and thread counts across the cycles to detect leaks.
- A DLL lock probe: after all cycles, the libmpv-2.dll must be renamable,
  proving the add_dll_directory handle was released.
- A distribution manifest check: the provenance bundle files all exist with
  matching SHA-256, so the onedir bundle can ship the exact DLL that was
  gate-tested.

Note: true onedir execution on a clean machine requires building a frozen
binary, which is a separate packaging step; this gate records the lifecycle
and DLL-provenance evidence that makes that step safe.
"""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

CFR_FIXTURE = REPO / ".cache" / "mpv_spike" / "pts_fixtures" / "cfr.mp4"
DEFAULT_DLL_DIR = REPO / "tools" / "libmpv" / "dll"
PROVENANCE = REPO / "tools" / "libmpv" / "provenance.json"
OUT_ROOT = REPO / ".cache" / "mpv_spike" / "g6_lifecycle"


def _handle_count() -> int:
    try:
        pid = os.getpid()
        ph = ctypes.windll.kernel32.OpenProcess(0x0400, False, pid)  # PROCESS_QUERY_INFORMATION
        if not ph:
            return -1
        count = ctypes.c_ulong(0)
        ok = ctypes.windll.kernel32.GetProcessHandleCount(ph, ctypes.byref(count))
        ctypes.windll.kernel32.CloseHandle(ph)
        return int(count.value) if ok else -1
    except Exception:
        return -1


def _thread_count() -> int:
    try:
        snapshot = ctypes.windll.kernel32.CreateToolhelp32Snapshot(0x4, 0)
        if snapshot == -1:
            return -1

        class THREADENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
                ("th32ThreadID", ctypes.c_ulong), ("th32OwnerProcessID", ctypes.c_ulong),
                ("tpBasePri", ctypes.c_long), ("tpDeltaPri", ctypes.c_long), ("dwFlags", ctypes.c_ulong),
            ]

        try:
            entry = THREADENTRY32()
            entry.dwSize = ctypes.sizeof(THREADENTRY32)
            pid = os.getpid()
            n = 0
            ok = ctypes.windll.kernel32.Thread32First(snapshot, ctypes.byref(entry))
            while ok:
                if entry.th32OwnerProcessID == pid:
                    n += 1
                ok = ctypes.windll.kernel32.Thread32Next(snapshot, ctypes.byref(entry))
            return n
        finally:
            ctypes.windll.kernel32.CloseHandle(snapshot)
    except Exception:
        return -1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_g6(fixture: Path, dll_dir: Path, out_dir: Path, cycles: int) -> dict:
    import tkinter as tk

    from mpv_engine import MpvEngine

    out_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "schema_version": 1,
        "kind": "mpv_phase0_g6_lifecycle",
        "gate": "G6",
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fixture": str(fixture),
        "cycles": cycles,
        "cycle_results": [],
        "resource": {},
        "dll_lock": {},
        "distribution": {},
        "status": "fail",
        "reason_codes": [],
    }

    root = tk.Tk()
    root.title("G6 lifecycle")
    root.geometry("480x270+200+200")
    host = tk.Frame(root, bg="black")
    host.pack(fill=tk.BOTH, expand=True)
    root.update_idletasks()
    wid = host.winfo_id()

    handles_before = _handle_count()
    threads_before = _thread_count()
    close_failures = []
    try:
        for i in range(cycles):
            engine = MpvEngine(str(fixture), wid=wid, fps=25.0, total=13,
                               dll_dir=dll_dir, edl_dir=out_dir / "edl")
            engine.start()
            deadline = time.time() + 8.0
            loaded = False
            while time.time() < deadline:
                root.update()
                for event in engine.poll_events():
                    if event.get("event") == "file-loaded":
                        loaded = True
                if loaded:
                    break
                time.sleep(0.04)
            closed = engine.close(timeout=3.0)
            alive_after = engine.is_alive()
            entry = {
                "cycle": i, "loaded": loaded, "closed": bool(closed),
                "alive_after_close": alive_after,
                "handles": _handle_count(), "threads": _thread_count(),
            }
            result["cycle_results"].append(entry)
            if not closed or alive_after:
                close_failures.append(entry)
    finally:
        try:
            root.destroy()
        except Exception:
            pass

    handles_after = _handle_count()
    threads_after = _thread_count()
    result["resource"] = {
        "handles_before": handles_before,
        "handles_after": handles_after,
        "handle_growth": (handles_after - handles_before) if handles_before >= 0 and handles_after >= 0 else None,
        "threads_before": threads_before,
        "threads_after": threads_after,
        "thread_growth": (threads_after - threads_before) if threads_before >= 0 and threads_after >= 0 else None,
    }

    # DLL lock probe: a held add_dll_directory handle would block rename.
    dll = dll_dir / "libmpv-2.dll"
    try:
        probe = str(dll) + ".probe"
        os.rename(dll, probe)
        os.rename(probe, dll)
        result["dll_lock"] = {"renamable": True, "pass": True}
    except OSError as exc:
        result["dll_lock"] = {"renamable": False, "pass": False, "error": str(exc)}

    # Distribution: verify the provenance bundle on disk matches its manifest.
    dist = {"manifest_present": PROVENANCE.is_file(), "files": [], "all_match": False}
    if PROVENANCE.is_file():
        manifest = json.loads(PROVENANCE.read_text(encoding="utf-8"))
        base = PROVENANCE.parent
        all_match = True
        for key in ("archive", "dll", "build", "license", "redistribution"):
            entry = manifest.get(key)
            if not entry:
                continue
            path = base / entry["path"]
            ok = path.is_file() and _sha256(path) == entry["sha256"]
            dist["files"].append({"part": key, "path": entry["path"], "match": bool(ok)})
            all_match = all_match and ok
        dist["all_match"] = all_match
    result["distribution"] = dist

    codes = []
    if close_failures:
        codes.append("CLOSE_OR_TEARDOWN_FAILED")
    if not result["dll_lock"].get("pass"):
        codes.append("DLL_LOCKED_AFTER_CLOSE")
    if not (dist["manifest_present"] and dist["all_match"]):
        codes.append("DISTRIBUTION_MANIFEST_MISMATCH")
    tg = result["resource"]["thread_growth"]
    if tg is not None and tg > cycles:
        codes.append("THREAD_LEAK_SUSPECTED")
    result["reason_codes"] = codes
    result["status"] = "pass" if not codes else "fail"
    result["close_failures"] = close_failures
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=CFR_FIXTURE)
    parser.add_argument("--dll-dir", type=Path, default=DEFAULT_DLL_DIR)
    parser.add_argument("--out-dir", type=Path, default=OUT_ROOT)
    parser.add_argument("--cycles", type=int, default=20)
    args = parser.parse_args(argv)

    if not args.fixture.is_file():
        print(f"fixture missing: {args.fixture}", file=sys.stderr)
        return 2
    if not (args.dll_dir / "libmpv-2.dll").is_file():
        print(f"libmpv-2.dll missing under {args.dll_dir}", file=sys.stderr)
        return 2

    out_dir = args.out_dir / datetime.now().strftime("%Y%m%d-%H%M%S")
    result = run_g6(args.fixture, args.dll_dir, out_dir, args.cycles)
    report = out_dir / "g6_lifecycle.json"
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": result["status"],
        "reason_codes": result["reason_codes"],
        "cycles": f"{args.cycles - len(result['close_failures'])}/{args.cycles} clean",
        "thread_growth": result["resource"].get("thread_growth"),
        "handle_growth": result["resource"].get("handle_growth"),
        "dll_renamable": result["dll_lock"].get("renamable"),
        "distribution_match": result["distribution"].get("all_match"),
        "report": str(report),
    }, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
