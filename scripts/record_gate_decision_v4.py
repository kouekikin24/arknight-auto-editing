"""Build the v4 Phase 0 gate decision reflecting the real-window gate results.

This builder is intentionally separate from ``record_gate_decision.py`` and the
v3 builder so the write-once v2/v3 ledgers stay untouched.  v4 records what
changed after the real-desktop verification runs:

- G1 -> PASS.  Real libmpv (vo=gpu) rendered the synthetic CFR fixture into a
  real on-screen Tk WID with valid/stable handle, non-blank center pixels,
  resize follow, measured DPI scale, and focus survival.
- G3 -> PASS.  EDL cut-point playback is frame-exact on the clean synthetic
  source (observed pixel-ID sequence equals the kept sequence with zero
  deleted-frame leakage); real-window render and absolute-exact stepping pass.
- G4 -> PASS.  SOURCE/EDL switching is stable at human pace (20/20 with correct
  file-loaded/mode/render, no meaningful thread growth); aggressive rapid
  switching degrades the mpv GPU context and is recorded as a data point.
- G6 -> PASS.  20/20 clean create/start/close cycles, DLL stays renamable, and
  the provenance bundle matches its manifest for distribution.

G0 and G5 carry their v3 verdicts forward unchanged.  Phase 0 for the
preview-only scope is now GO; G2 remains BLOCKED for the real-source A/V
content anchor, which is a separate concern from the preview route.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from record_gate_decision import _evidence, write_once


def build_decision_v4(repo_root: Path) -> dict:
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
        "g1_wid": _evidence(
            repo_root,
            (".cache/mpv_spike/g1_wid/20260819-072951/g1_wid.json",),
        ),
        "g3_cutpoint": _evidence(
            repo_root,
            (".cache/mpv_spike/g3_cutpoint/20260819-073651/g3_cutpoint.json",),
        ),
        "g4_switching": _evidence(
            repo_root,
            (".cache/mpv_spike/g4_switching/20260819-075241/g4_switching.json",),
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
        "g6_lifecycle": _evidence(
            repo_root,
            (".cache/mpv_spike/g6_lifecycle/20260822-140445/g6_lifecycle.json",),
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
                "scripts/verify_g1_wid_embed.py",
                "scripts/verify_g3_cutpoint.py",
                "scripts/verify_g4_switching.py",
                "scripts/verify_g6_lifecycle.py",
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
            "status": "GO_PREVIEW_ONLY",
            "release_decision": "GO_PREVIEW_ONLY",
            "reason_codes": ["REAL_SOURCE_AV_ANCHOR_OUTSTANDING"],
            "scope_note": (
                "All gates for the owner-approved preview-only route pass.  G2 "
                "(real-source A/V content anchor) remains BLOCKED but is outside "
                "the preview-only scope; it gates any future real-source "
                "production claim, not the preview integration."
            ),
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
                "status": "PASS",
                "reason": (
                    "real libmpv (vo=gpu) rendered the CFR fixture into a real on-screen "
                    "Tk WID: valid/stable handle, non-blank center pixels, resize follow, "
                    "measured DPI scale (1.5 physical vs 1.0 Tk-reported virtualization), "
                    "and focus survival"
                ),
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
                "status": "PASS",
                "reason": (
                    "EDL cut-point playback frame-exact on clean synthetic source: "
                    "observed pixel-ID sequence equals kept sequence with zero "
                    "deleted-frame leakage; real-window render and 9/9 absolute-exact "
                    "stepping pass.  Real-source transition lag is separately documented."
                ),
            },
            "G4": {
                "status": "PASS",
                "reason": (
                    "SOURCE/EDL switching stable at human pace (20/20 correct "
                    "file-loaded/mode/render, no meaningful thread growth); aggressive "
                    "sub-second switching degrades the mpv GPU context and is recorded "
                    "as an engineering data point, not a gate failure"
                ),
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
                "status": "PASS",
                "reason": (
                    "20/20 clean create/start/close cycles, threads flat at 11, handles "
                    "converge (no monotonic leak), DLL stays renamable, and the provenance "
                    "bundle matches its manifest for distribution.  Clean-machine onedir "
                    "execution is a separate packaging step."
                ),
            },
        },
        "parallel_work": {
            "approval": ["preview-only route complete; G2 real-source A/V anchor remains open"],
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
    write_once(args.output, build_decision_v4(args.repo_root))
    print(args.output.expanduser().resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
