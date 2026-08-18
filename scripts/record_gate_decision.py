"""Create one immutable, evidence-bound Phase 0 gate decision record.

This is a decision ledger, not a replacement for the raw oracle reports.  It
keeps the canonical G0-G6 status intact and records the more precise G2 route
classification used by the approval/production parallel workflow.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_record(path: Path, *, repo_root: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    record: dict[str, Any] = {
        "path": str(path),
        "relative_path": (
            str(path.relative_to(repo_root.resolve()))
            if path.is_relative_to(repo_root.resolve())
            else None
        ),
        "exists": path.is_file(),
    }
    if path.is_file():
        record.update({"size": path.stat().st_size, "sha256": sha256_file(path)})
    else:
        record.update({"size": None, "sha256": None})
    return record


def _evidence(repo_root: Path, relative_paths: Iterable[str]) -> list[dict[str, Any]]:
    return [file_record(repo_root / value, repo_root=repo_root) for value in relative_paths]


def build_decision(repo_root: Path) -> dict[str, Any]:
    repo_root = repo_root.expanduser().resolve()
    evidence = {
        "source_oracles": _evidence(
            repo_root,
            (
                ".cache/mpv_spike/frame_oracle/1_20260809_threads4.json",
                ".cache/mpv_spike/frame_oracle/2_20260809.json",
                ".cache/mpv_spike/frame_oracle/3_20260809.json",
                ".cache/mpv_spike/frame_oracle/4_20260809_threads4.json",
            ),
        ),
        "g2_candidate_review": _evidence(
            repo_root,
            (
                ".cache/mpv_spike/pts_normalize/3_av_event_candidates_316_326.v3.json",
                ".cache/mpv_spike/pts_normalize/3_av_event_candidates_316_326.contact.png",
                ".cache/mpv_spike/pts_normalize/3_av_event_candidates_316_326_review/start_315.8_316.8.mp4",
                ".cache/mpv_spike/pts_normalize/3_av_event_candidates_316_326_review/end_325.0_326.0.mp4",
            ),
        ),
        "production_contract": _evidence(
            repo_root,
            (
                "frame_pts_certifier.py",
                "pts_timeline.py",
                "media_exporter.py",
                "analyzer.py",
                "scripts/verify_mpv_frames.py",
            ),
        ),
        "production_goldens": _evidence(
            repo_root,
            (
                "PRODUCTION_PTS_GOLDEN_20260816_v3/manifest.json",
                "PRODUCTION_PTS_GOLDEN_VFR_20260816_v2/manifest.json",
            ),
        ),
    }
    return {
        "schema_version": 1,
        "kind": "mpv_phase0_gate_decision",
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "authority": {
            "decision_owner": "Codex",
            "observation_policy": "codex_independent_review_authorized",
            "human_observation_required": False,
        },
        "phase0": {
            "status": "INCONCLUSIVE",
            "release_decision": "NO_GO_CURRENT_SOURCE_SET",
            "reason_codes": [
                "ORIGINAL_SOURCE_PTS_CONFLICT",
                "SAMPLE3_AV_ROUTE_CLOSED",
                "LIBMPV_SUPPLY_CHAIN_INCOMPLETE",
            ],
        },
        "gates": {
            "G0": {"status": "BLOCKED", "reason": "provenance and redistribution evidence incomplete"},
            "G1": {"status": "BLOCKED", "reason": "real WID pixel/DPI/focus evidence incomplete"},
            "G2": {
                "status": "BLOCKED",
                "reason": "no approved real-source time route",
                "subgates": {
                    "G2-A-original_pts": {
                        "status": "REJECTED",
                        "reason": "all four complete source oracles contain duplicate and non-monotonic PTS",
                    },
                    "G2-B-sample3_proxy": {
                        "status": "CLOSED",
                        "reason": "Codex review found NO_USABLE_AV_EVENT; do not repeat the candidate window",
                    },
                    "G2-C-pts_consumer": {
                        "status": "PASS_SYNTHETIC_ONLY",
                        "reason": "short CFR/VFR and A/V fixtures consume the certified tick schedule",
                    },
                    "G2-D-real_av_content": {
                        "status": "BLOCKED",
                        "reason": "no independently bound real content anchor is available",
                    },
                },
            },
            "G3": {"status": "BLOCKED", "reason": "no approved real time route for formal EDL cut-point proof"},
            "G4": {"status": "BLOCKED", "reason": "complete SOURCE/EDL switch evidence incomplete"},
            "G5": {"status": "NOT_RUN", "reason": "frame-step and speed measurements not run"},
            "G6": {"status": "BLOCKED", "reason": "complete WID lifecycle and clean-machine distribution evidence incomplete"},
        },
        "parallel_work": {
            "approval": ["G0 provenance", "G1 WID diagnostics", "G6 lifecycle diagnostics"],
            "production": ["short A/V export golden", "VFR/non-zero tick regression", "atomic cancellation regression"],
        },
        "hard_rules": {
            "production_mpv_engine_allowed": False,
            "video_io_thread_required": True,
            "repeat_closed_sample3_scan": False,
            "fps_timestamp_fallback_allowed": False,
        },
        "evidence": evidence,
    }


def write_once(path: Path, value: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor != -1:
            os.close(descriptor)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    write_once(args.output, build_decision(args.repo_root))
    print(args.output.expanduser().resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
