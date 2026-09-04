#!/usr/bin/env python3
"""Inventory 保鲜硬机制（R4 的机械版）：扫实际资产，报"没登记"的。

两种模式：
  --write  把当前扫描结果写成机读索引 asset_index.json（登记基线）
  默认     扫描并与 asset_index.json 比对，只报"新增/未登记"项
防"资产/探针/决策产生了却没进清单"的漂移。只报告，不改生产。
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "asset_index.json"

# 已在 ASSET_INVENTORY 按"类"登记的资产前缀（不逐件列）。
CLASS_PREFIXES = [
    "PRODUCTION_", "MPV_GATE_DECISION_", "MPV_PHASE0_", "HANDOFF_",
    "ff_", "gt_", "gt2_", "mpv_", "c4_", "vc4_", "spike_", "verify_",
    "landing", "probe_", "calibrat", "transfer_curve", "window_curve",
    "default_size_probe", "static_shift_probe", "duo_instance",
    "c4_region_diag", "step_latency_probe", "analyze_user_pair",
]


def scan():
    items = []
    items += sorted((ROOT / ".cache" / "v3ass").glob("*.py"))
    items += sorted((ROOT / ".cache" / "v3ass").glob("*results*.json"))
    items += sorted(ROOT.glob("PRODUCTION_*"))
    items += sorted(ROOT.glob("MPV_GATE_DECISION_*.json"))
    items += sorted((ROOT / ".cache" / "pyav_kf").glob("*.npz"))
    items += sorted(ROOT.glob("HANDOFF_*.md"))
    for extra in ["AGENTS.md", "checklist.md", "ASSET_INVENTORY.md",
                  "asset_index.json"]:
        p = ROOT / extra
        if p.is_file():
            items.append(p)
    return items


def _classify(name):
    for c in CLASS_PREFIXES:
        if name.startswith(c):
            return c
    return "individual"


def write_index():
    entries = [{"path": str(p.relative_to(ROOT)), "class": _classify(p.name)}
               for p in scan()]
    INDEX.write_text(json.dumps({"assets": entries}, ensure_ascii=False,
                                indent=1), encoding="utf-8")
    print(f"已写 asset_index.json：{len(entries)} 项")
    return 0


def main():
    if "--write" in sys.argv:
        return write_index()
    if not INDEX.is_file():
        print("asset_index.json 不存在，先跑 `inventory_audit.py --write`")
        return 1
    registered = {e["path"] for e in
                  json.loads(INDEX.read_text(encoding="utf-8"))["assets"]}
    live = [str(p.relative_to(ROOT)) for p in scan()]
    new = [x for x in live if x not in registered]
    gone = [x for x in registered if x not in live]
    print(f"== Inventory 审计：在盘 {len(live)}，已登记 {len(registered)}，"
          f"新增 {len(new)}，消失 {len(gone)} ==")
    for x in new:
        print("  [新增未登记]", x)
    for x in gone:
        print("  [登记但已消失]", x)
    if not new and not gone:
        print("  一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
