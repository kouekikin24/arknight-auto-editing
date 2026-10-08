#!/usr/bin/env python3
"""发布前必跑清单（验收门禁）——一条命令跑完全部自动检查。

## 为什么存在

owner 2026-09-24 令「把发布前必跑清单固化下来」。此前桌面层探针散在
`.cache/v3ass/`（gitignore 域、81 个），全靠人记——**漏跑就是事故温床**
（09-24「点击后红条冻死」正是没进清单的那类缺陷）。

## 分层（对应 R8 补充·事件层同源）

| 层 | 跑什么 | 需要什么 | 进 CI？ |
|---|---|---|---|
| L0 环境 | preflight / inventory_audit | 无 | ✅ |
| L1 单测 | pytest 全量 | 无 | ✅ |
| L2 引擎 | dsvc 位级 / 引擎 E2E / 碎段 / 混合倍率 | dsvc.exe + 4.mp4/3.mp4 | 可 |
| L3 GUI | 同源手检 / 缓存命中 / 随机跳转压测 | **可见桌面会话** | ❌ |

`--level` 控制跑到哪层（默认 `L2`）；L3 需显式 `--level L3`（会开真窗口）。

## 用法

    python tools/acceptance_gate.py              # L0+L1+L2（~2 分钟）
    python tools/acceptance_gate.py --level L1   # 只跑快检（~35 秒）
    python tools/acceptance_gate.py --level L3   # 含 GUI（需桌面，~5 分钟）
    python tools/acceptance_gate.py --list       # 只列清单不执行

退出码：0=全过；1=有失败项；2=环境缺失（如素材不在）。

## 判定口径

- **L0/L1**：子进程退出码（pytest 非 0 即失败）。
- **L2/L3**：读探针产出的 results JSON 里的 `verdict` 字段（各探针口径见其
  docstring）。**探针文件缺失=SKIP（不判失败）**——它们在 gitignore 域，
  新克隆的仓库不会有；但会明确打印 SKIP 原因，避免"静默漏跑"。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
V3 = _REPO / ".cache" / "v3ass"

# 通过判定：这些 verdict 值算 PASS（各探针自报口径）
PASS_VERDICTS = {
    "PASS", "FRAG_SMOOTH_VERIFIED", "CAP_FIX_VERIFIED",
    "PREBUILD_OK", "VERIFIED", "TRUE", "OK",
}


@dataclass
class Step:
    key: str
    level: str            # L0 / L1 / L2 / L3
    title: str
    cmd: list[str] = field(default_factory=list)
    script: Path | None = None      # 探针脚本（缺失则 SKIP）
    results: Path | None = None     # 结果 JSON（用于读 verdict）
    timeout_s: int = 600
    needs: tuple[str, ...] = ()     # 依赖素材（缺失则 SKIP）
    note: str = ""


def _steps() -> list[Step]:
    return [
        # ---------------- L0 环境 ----------------
        Step("preflight", "L0", "开工前环境自检（tools/preflight.py）",
             cmd=[sys.executable, "tools/preflight.py"], timeout_s=120),
        Step("audit", "L0", "资产盘点防漂移（tools/inventory_audit.py）",
             cmd=[sys.executable, "tools/inventory_audit.py"], timeout_s=120),

        # ---------------- L1 单测 ----------------
        Step("pytest", "L1", "全量单元测试（含事件契约：四条鼠标路径收尾）",
             cmd=[sys.executable, "-m", "pytest", "-q"], timeout_s=600),

        # ---------------- L2 引擎层（真 dsvc + 真 mpv，headless） ----------------
        Step("dsvc_cont", "L2", "dsvc 小间隙连续解码位级验收（B 帧跨边界）",
             script=V3 / "probe_dsvc_cont.py",
             results=V3 / "probe_dsvc_cont_results.json",
             timeout_s=900, needs=(str(V3 / "dsvc/target/release/dsvc.exe"),),
             note="位级 mad=0；无 results json 时看 stdout 末行"),
        Step("dthrough_engine", "L2", "直通档引擎 E2E（R1-R4：装载/播放/seek/零孤儿）",
             script=V3 / "probe_dthrough_engine.py",
             results=V3 / "probe_dthrough_engine_results.json",
             timeout_s=600, needs=(str(V3 / "dsvc/target/release/dsvc.exe"),),
             note="verdict=PASS"),
        Step("dthrough_frag", "L2", "碎段性能（200 岛×11 帧：停顿账）",
             script=V3 / "probe_dthrough_frag.py",
             results=V3 / "probe_dthrough_frag_results.json",
             timeout_s=900, needs=(str(V3 / "dsvc/target/release/dsvc.exe"),),
             note="verdict=FRAG_SMOOTH_VERIFIED"),
        Step("dthrough_mixedrate", "L2", "混合倍率解码预算（cap 验证）",
             script=V3 / "probe_dthrough_mixedrate.py",
             results=V3 / "probe_dthrough_mixedrate_results.json",
             timeout_s=900, needs=(str(V3 / "dsvc/target/release/dsvc.exe"),),
             note="verdict=CAP_FIX_VERIFIED"),

        # ---------------- L3 GUI 层（需可见桌面会话） ----------------
        Step("handcheck", "L3", "同源手检九项（真 main.main()：装载/直通/回绕/seek/点击/空格）",
             script=V3 / "handcheck_samesource.py",
             results=V3 / "handcheck_samesource_results.json",
             timeout_s=900,
             note="需真实映射窗口；九项逐条看 json"),
        Step("cache_hit", "L3", "分析缓存命中（导入→点分析→秒开）",
             script=V3 / "verify_prebuild_hit.py",
             results=V3 / "verify_prebuild_hit_results.json",
             timeout_s=900,
             note="verdict.hit_prebuilt_cache=true"),
        Step("immediate_click", "L3", "导入后立即点分析（挂起→续跑→命中）",
             script=V3 / "verify_immediate_click.py",
             results=V3 / "verify_immediate_click_results.json",
             timeout_s=900,
             note="verdict.hit_prebuilt_cache=true"),
        Step("random_seek", "L3", "随机跳转压测（真事件注入：落点/延迟/红条跟随）",
             script=_REPO / "tools" / "gui_stress" / "random_seek_stress.py",
             results=V3 / "gui_stress_results.json",
             timeout_s=900,
             note="需真实映射窗口；看 P1 落点误差 / P2 advanced_after_min>0"),
    ]


def _read_stdout_verdict(stdout: str) -> tuple[bool | None, str]:
    """从探针 stdout 提取判定（用于不写 JSON 的探针）。

    已知口径：
      · `probe_dsvc_cont` → `[verdict] A_full_bytes_exact=True A2_seek=True
        B_restart_intact=True C_discard_exact=True`（位级全等才 PASS）
    """
    import re
    for line in reversed(stdout.splitlines()):
        if "[verdict]" not in line:
            continue
        flags = dict(re.findall(r"(\w+)=(True|False)", line))
        if flags:
            ok = all(v == "True" for v in flags.values())
            detail = " ".join(f"{k}={v}" for k, v in flags.items())
            return ok, detail
    return None, "stdout 无 [verdict] 行"


def _read_verdict(path: Path) -> tuple[bool | None, str]:
    """从结果 JSON 里尽力提取通过与否。返回 (判定, 说明)。"""
    if not path.is_file():
        return None, f"结果文件不存在：{path.name}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"结果文件不可读：{exc}"
    if not isinstance(data, dict):
        return None, "结果不是对象"
    # 1) 直接 verdict 字段
    v = data.get("verdict")
    if isinstance(v, str):
        return (v.upper() in PASS_VERDICTS), f"verdict={v}"
    if isinstance(v, dict):
        # 形如 verify_*: {"verdict": {"hit_prebuilt_cache": true, ...}}
        for key in ("hit_prebuilt_cache", "ok", "pass"):
            if key in v:
                return bool(v[key]), f"verdict.{key}={v[key]}"
        return None, f"verdict 对象无已知判定键：{list(v)}"
    # 2) 随机跳转压测口径（无 verdict 字段）
    if "P1_paused_random_seek" in data:
        p1 = data.get("P1_paused_random_seek") or {}
        p2 = data.get("P2_playing_random_seek") or {}
        err_ok = (p1.get("err_abs_max") == 0)
        follow_ok = (p2.get("advanced_after_min", 0) or 0) > 0
        stuck = bool(p1.get("stuck_flags")) or bool(p2.get("stuck_flags"))
        ok = err_ok and follow_ok and not stuck
        return ok, (f"落点误差max={p1.get('err_abs_max')} "
                    f"播放中最小推进={p2.get('advanced_after_min')} "
                    f"状态卡死={stuck}")
    # 3) 同源手检：无 verdict，检查是否有 _error
    if "_error" in data:
        return False, f"探针报错：{str(data['_error'])[:80]}"
    if data.get("T1_load"):
        return True, "九项已产出（逐项详见 json）"
    return None, "无已知判定口径（人工看 json）"


def _has_assets(step: Step) -> tuple[bool, str]:
    for need in step.needs:
        if not Path(need).exists():
            return False, f"缺依赖：{Path(need).name}"
    if step.script is not None and not step.script.is_file():
        return False, f"探针不在库：{step.script.name}（gitignore 域，属正常）"
    return True, ""


def run(level: str, only: str | None, do_list: bool) -> int:
    order = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}
    max_lv = order[level]
    steps = [s for s in _steps() if order[s.level] <= max_lv]
    if only:
        steps = [s for s in steps if only in s.key]

    if do_list:
        for s in steps:
            ok, why = _has_assets(s)
            mark = "  " if ok else "SK"
            print(f"[{mark}] {s.level} {s.key:18s} {s.title}")
            if not ok:
                print(f"        └ {why}")
        return 0

    print(f"=== 发布前门禁：跑到 {level}（{len(steps)} 项）===\n")
    results: list[tuple[Step, str, str]] = []
    t_start = time.perf_counter()

    for i, s in enumerate(steps, 1):
        head = f"[{i}/{len(steps)}] {s.level} {s.key} — {s.title}"
        print(head, flush=True)
        ok_assets, why = _has_assets(s)
        if not ok_assets:
            print(f"      SKIP：{why}\n", flush=True)
            results.append((s, "SKIP", why))
            continue

        if s.cmd:
            cmd = s.cmd
        else:
            assert s.script is not None
            cmd = [sys.executable, str(s.script)]

        t0 = time.perf_counter()
        try:
            proc = subprocess.run(cmd, cwd=str(_REPO), timeout=s.timeout_s,
                                  capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            dt = time.perf_counter() - t0
            print(f"      FAIL：超时 {s.timeout_s}s（{dt:.0f}s）\n", flush=True)
            results.append((s, "FAIL", f"超时>{s.timeout_s}s"))
            continue
        dt = time.perf_counter() - t0

        if s.results is not None:
            passed, detail = _read_verdict(s.results)
            if passed is None:
                # 结果文件缺失/无口径 → 退回 stdout 判定 → 再退回退出码
                so_ok, so_detail = _read_stdout_verdict(proc.stdout or "")
                if so_ok is not None:
                    passed, detail = so_ok, so_detail
                else:
                    passed = (proc.returncode == 0)
                    detail = f"{detail}；回落退出码={proc.returncode}"
        else:
            so_ok, so_detail = _read_stdout_verdict(proc.stdout or "")
            if so_ok is not None:
                passed, detail = so_ok, so_detail
            else:
                passed = (proc.returncode == 0)
                detail = f"退出码={proc.returncode}"

        status = "PASS" if passed else "FAIL"
        print(f"      {status}（{dt:.1f}s）{detail}", flush=True)
        if not passed:
            tail = (proc.stdout or "").strip().splitlines()[-6:]
            for line in tail:
                print(f"        | {line}", flush=True)
            err_tail = (proc.stderr or "").strip().splitlines()[-4:]
            for line in err_tail:
                print(f"        ! {line}", flush=True)
        print(flush=True)
        results.append((s, status, detail))

    total = time.perf_counter() - t_start
    n_pass = sum(1 for _, st, _ in results if st == "PASS")
    n_fail = sum(1 for _, st, _ in results if st == "FAIL")
    n_skip = sum(1 for _, st, _ in results if st == "SKIP")

    print("=" * 72)
    print(f"门禁结果：{n_pass} 通过 / {n_fail} 失败 / {n_skip} 跳过"
          f"（{total:.0f}s）")
    if n_fail:
        print("\n失败项：")
        for s, st, detail in results:
            if st == "FAIL":
                print(f"  ✗ {s.level} {s.key} — {s.title}\n      {detail}")
    if n_skip:
        print("\n跳过项（多为素材/探针不在库，非失败）：")
        for s, st, detail in results:
            if st == "SKIP":
                print(f"  - {s.level} {s.key}：{detail}")
    print("=" * 72)
    return 1 if n_fail else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="发布前必跑清单（验收门禁）")
    ap.add_argument("--level", default="L2", choices=["L0", "L1", "L2", "L3"],
                    help="跑到哪一层（默认 L2；L3 会开真窗口，需可见桌面）")
    ap.add_argument("--only", default=None,
                    help="只跑 key 含该子串的项（如 --only random_seek）")
    ap.add_argument("--list", action="store_true", dest="do_list",
                    help="只列清单不执行")
    args = ap.parse_args()
    return run(args.level, args.only, args.do_list)


if __name__ == "__main__":
    sys.exit(main())
