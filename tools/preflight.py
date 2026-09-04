#!/usr/bin/env python3
"""Preflight 硬机制（R2 的机械版）：开工前跑，把"家里有什么"摊在眼前。

打印三块：①已证明能力(ASSET_INVENTORY §六) ②上游有/我们mpv有无 的功能
parity 提醒 ③R1-R6 勾单。防止"漏组合现成能力"和"静默砍上游功能"。
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UP = ROOT.parent / "arknight-auto-editing-upstream"


def capabilities():
    inv = (ROOT / "ASSET_INVENTORY.md").read_text(encoding="utf-8")
    m = re.search(r"## 六、已证明能力表.*?(?=^## |\Z)", inv, re.S | re.M)
    rows = []
    if m:
        for line in m.group(0).splitlines():
            if line.startswith("|") and not line.startswith("|--") \
               and "能力" not in line and "---" not in line:
                cells = [c.strip() for c in line.strip("|").split("|")]
                if len(cells) >= 2 and cells[0]:
                    rows.append(cells[0])
    return rows


def parity_reminder():
    biz = ["speedup_1x", "speedup_02", "speed_segs"]
    other = ["preview_step", "speed_multiplier", "jump_pause"]
    up_io = UP / "video_io.py"
    up_txt = up_io.read_text(encoding="utf-8") if up_io.is_file() else ""
    mpv_txt = (ROOT / "mpv_engine.py").read_text(encoding="utf-8")
    stripped = "MPV_SPEED_POLICY_UNSUPPORTED" in mpv_txt
    print("\n[parity 提醒] 上游(liemark) vs 我们 mpv：")
    for f in biz + other:
        up = f in up_txt
        note = ""
        if f in biz and up and stripped:
            note = "  ⚠ 上游有，mpv 当前剥离（待第①步补）"
        print(f"  - {f}: 上游={up}{note}")


def main():
    print("== Preflight（开工前必过）==")
    caps = capabilities()
    print(f"[已证明能力 {len(caps)} 条] 先问能否组合：")
    for c in caps:
        print("  -", c)
    parity_reminder()
    print("\n[R1-R7 勾单] 逐条确认后才许动手：")
    for r in ["R1 引擎改动先出 parity 表", "R2 先盘上游+能力+探针再造",
              "R3 数据先行+测量卫生(跑 probe_guard)", "R4 新资产/能力当场登记",
              "R5 诊断先列≥2假说+ACH矩阵", "R6 事实/判断分离+T0-T3,低置信禁入生产",
              "R7 制度封版:只维护不新增"]:
        print("  [ ]", r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
