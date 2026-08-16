from pathlib import Path
import json
import tempfile
import unittest

from scripts.record_gate_decision import build_decision, write_once


class GateDecisionTests(unittest.TestCase):
    def test_decision_keeps_canonical_gates_and_splits_g2(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".cache/mpv_spike/frame_oracle").mkdir(parents=True)
            (root / ".cache/mpv_spike/frame_oracle/1_20260809_threads4.json").write_bytes(b"oracle")
            decision = build_decision(root)
            self.assertEqual(decision["phase0"]["status"], "INCONCLUSIVE")
            self.assertEqual(decision["phase0"]["release_decision"], "NO_GO_CURRENT_SOURCE_SET")
            self.assertEqual(decision["gates"]["G2"]["status"], "BLOCKED")
            self.assertEqual(decision["gates"]["G2"]["subgates"]["G2-A-original_pts"]["status"], "REJECTED")
            self.assertEqual(
                decision["gates"]["G2"]["subgates"]["G2-B-sample3_proxy"]["status"],
                "CLOSED",
            )
            self.assertTrue(decision["evidence"]["source_oracles"][0]["exists"])

    def test_write_once_rejects_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "decision.json"
            write_once(path, {"status": "first"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "first")
            with self.assertRaises(FileExistsError):
                write_once(path, {"status": "second"})


if __name__ == "__main__":
    unittest.main()
