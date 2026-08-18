"""Build the v3 Phase 0 gate decision reflecting measured G0/G5 progress.

This builder is intentionally separate from ``record_gate_decision.py`` so the
write-once v2 ledger stays untouched.  v3 records what actually changed:

- G0 -> PASS.  The environment probe bound the project-owned LGPL libmpv DLL
  by path and SHA-256 against the five-piece provenance bundle.
- G5 -> PASS_WITH_EXCEPTIONS.  Absolute certified-time stepping and x2/x10/x20
  rates pass the pre-registered thresholds; the x80 high-speed ceiling exceeds
  the threshold (best ~46x) and was accepted by the owner as app-level frame
  skipping, not a blocking defect.
- G1/G3/G4/G6 stay BLOCKED: no real-window evidence exists yet.

It also re-binds the production contract hashes, which drifted after v2 and
were recorded as intentional drift in HANDOFF_CURRENT.md.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from record_gate_decision import _evidence, file_record, write_once


def build_decision_v3(repo_root: Path) -> dict:
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
        "g0_environment_probe": _evidence(
            repo_root,
            (
                ".cache/mpv_spike/20260817-083902/environment.json",
                ".cache/mpv_spike/20260817-083902/"
                "environment.run-20260817-083902-1786927142402945800-f73f02.json",
            ),
        ),
        "g0_provenance": _evidence(
            repo_root,
            (
                "tools/libmpv/provenance.json",
                "tools/libmpv/build-record.txt",
                "tools/libmpv/LICENSE.mpv",
                "tools/libmpv/REDISTRIBUTION.md",
                "tools/libmpv/dll/libmpv-2.dll",
            ),
        ),
        "g5_stepspeed": _evidence(
            repo_root,
            (
                ".cache/mpv_spike/thresholds/stepspeed-20260817.json",
                ".cache/mpv_spike/20260817-212832/stepspeed.json",
                ".cache/mpv_spike/20260817-212832/"
                "stepspeed.run-20260817-212832-1786973312367176700-16179c.json",
            ),
        ),
        "production_contract": _evidence(
            repo_root,
            (
                "frame_pts_certifier.py",
                "pts_timeline.py",
                "media_exporter.py",
                "analyzer.py",
                "preview_engine.py",
                "cv_engine.py",
                "mpv_engine.py",
                "certified_edl.py",
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
                "REAL_WINDOW_EVIDENCE_INCOMPLETE",
            ],
        },
        "gates": {
            "G0": {
                "status": "PASS",
                "reason": (
                    "project-owned LGPL libmpv provenance bundle complete; environment "
                    "probe bound the loaded DLL by path and SHA-256 (client API 2.5, "
                    "mpv v0.41.0-926-ge034d612c)"
                ),
            },
            "G1": {
                "status": "BLOCKED",
                "reason": "real Tk WID pixel/resize/DPI/focus evidence not yet collected",
            },
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
                    "G2-E-bprime_adjudicated_preview": {
                        "status": "ACCEPTED_PREVIEW_ONLY",
                        "reason": (
                            "owner ruling 2026-08-17: B' adjudicated certification accepted "
                            "as the EDL time route for preview-only scope; not an export authority"
                        ),
                    },
                },
            },
            "G3": {
                "status": "BLOCKED",
                "reason": (
                    "synthetic EDL cut-point proof exact; real-source cut-point pixel "
                    "proof blocked on G1 real-window evidence"
                ),
            },
            "G4": {
                "status": "BLOCKED",
                "reason": "complete SOURCE/EDL switching pressure evidence incomplete",
            },
            "G5": {
                "status": "PASS_WITH_EXCEPTIONS",
                "reason": (
                    "absolute certified-time stepping and x2/x10/x20 rates pass; x80 "
                    "high-speed ceiling accepted by owner as app-level frame skipping"
                ),
                "subgates": {
                    "G5-stepping_abs_exact": {
                        "status": "PASS",
                        "reason": "absolute+exact stepping 0.0 frames drift, 0.0 ms max deviation, back-step p95 20.7 ms",
                    },
                    "G5-speeds_low": {
                        "status": "PASS",
                        "reason": "x2/x10/x20 measured ratios 0.996/1.000/1.000 within [0.8, 1.25]",
                    },
                    "G5-speeds_x80_ceiling": {
                        "status": "FAIL_ACCEPTED",
                        "reason": (
                            "x80 min ratio 0.563 < 0.8 threshold (best ~46x with decoder "
                            "framedrop); owner accepted the ceiling with app-level frame "
                            "skipping for faster scrubbing"
                        ),
                    },
                },
            },
            "G6": {
                "status": "BLOCKED",
                "reason": "complete WID lifecycle and clean-machine onedir distribution evidence incomplete",
            },
        },
        "parallel_work": {
            "approval": ["G1 WID diagnostics", "G3 cut-point proof", "G4 switching", "G6 lifecycle diagnostics"],
            "production": ["P1 batched-seek export (sample 4)", "P2 legacy fast-lane toggle"],
        },
        "hard_rules": {
            "production_mpv_engine_allowed": False,
            "video_io_thread_required": True,
            "repeat_closed_sample3_scan": False,
            "fps_timestamp_fallback_allowed": False,
        },
        "evidence": evidence,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    write_once(args.output, build_decision_v3(args.repo_root))
    print(args.output.expanduser().resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
