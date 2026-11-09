#!/usr/bin/env python3
"""graphify 过滤版构建（R2：复用现成工具，只做过滤层）。

为什么需要这个脚本（10-24 实测教训）：
- graphify 默认会把 docs/tests/scripts 一起扫进图：我们的仓库 4511 节点里
  808 个（18%）来自 .md 文档，explain 会返回"被《六、已证明能力表》引用"
  这类噪音。
- 标准库节点（fractions/numpy/os…）会参与最短路径，导致 path 查询给出
  "proxy_cache → fractions ← mpv_engine" 这类语义无用的捷径。
- 过滤后：4511→1399 节点、构建 11.4s→1.2s、explain 输出干净。

能力边界（实测，不要过度承诺）：
- explain（函数/类被谁调用、调用了谁，带文件行号）：可信。
- path（两个模块间最短路径）：3/5 正确。根因=**函数内局部 import 不被
  AST 提取器建模**（如 preview_player.py 里的 `def f(): import proxy_cache`），
  以及"两个模块本无直接依赖"时返回的真实最短路径与人的预期不符。
  → path 结果必须回源码人工核对，不能直接当依赖结论。

用法：
    python tools/graphify_build.py            # 构建过滤版图
    python tools/graphify_build.py --explain _send_play
    python tools/graphify_build.py --path preview_player proxy_cache

依赖：隔离环境 .cache/graphify_env（uv/pip 安装 graphifyy）。
    python -m venv .cache/graphify_env
    .cache/graphify_env/Scripts/python -m pip install graphifyy
本脚本不会把 graphify 装进项目 venv。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_PY = ROOT / ".cache" / "graphify_env" / "Scripts" / "python.exe"
VENV_CLI = ROOT / ".cache" / "graphify_env" / "Scripts" / "graphify.exe"
OUT_DIR = ROOT / ".cache" / "graphify_out"
OUT_GRAPH = OUT_DIR / "graph_filtered.json"

# 只扫产品代码：排除历史探针、测试、文档、构建产物。
EXCLUDE_DIR_PARTS = (
    ".cache", ".git", "__pycache__", "node_modules",
    "scripts/", "tests/", "agents/",
)
CODE_SUFFIXES = {".py", ".rs"}

# 标准库 / 第三方顶层模块：从图中移除，避免 path 走语义无用捷径。
STDLIB_AND_THIRD_PARTY = {
    "os", "sys", "time", "json", "pathlib", "hashlib", "tempfile", "subprocess",
    "numpy", "threading", "math", "collections", "typing", "dataclasses", "re",
    "shutil", "fractions", "av", "cv2", "tkinter", "unittest", "mock", "io",
    "csv", "glob", "random", "traceback", "itertools", "functools", "operator",
    "warnings", "weakref", "queue", "signal", "socket", "struct", "ctypes",
    "platform", "statistics", "string", "textwrap", "uuid", "zipfile",
    "bisect", "copy", "enum", "abc", "datetime", "decimal", "errno", "gc",
    "inspect", "logging", "multiprocessing", "pickle", "pprint", "secrets",
    "select", "stat", "subprocess", "sysconfig", "tempfile", "urllib", "zlib",
}


def _fail(msg: str) -> int:
    print(f"[graphify_build] ERROR: {msg}", file=sys.stderr)
    return 1


def build_graph() -> int:
    if not VENV_PY.is_file():
        return _fail(
            f"隔离环境不存在：{VENV_PY}\n"
            "先建环境：python -m venv .cache/graphify_env && "
            ".cache/graphify_env/Scripts/python -m pip install graphifyy"
        )
    # 构建逻辑必须在独立 .py 里跑（stdin/-c 会让 graphify 退回串行并告警）。
    builder = Path(__file__).resolve()
    r = subprocess.run(
        [str(VENV_PY), str(builder), "--_build"],
        cwd=str(ROOT),
    )
    return r.returncode


def _do_build() -> int:
    sys.path.insert(0, str(ROOT / ".cache" / "graphify_env" / "Lib" / "site-packages"))
    from graphify.build import build
    from graphify.extract import collect_files, extract
    import networkx as nx

    root = ROOT
    files = collect_files(root, root=root)
    norm = lambda p: str(p).replace("\\", "/")  # noqa: E731
    code = [
        f for f in files
        if f.suffix in CODE_SUFFIXES
        and not any(part in norm(f) for part in EXCLUDE_DIR_PARTS)
    ]
    print(f"[collect] {len(files)} files -> {len(code)} product-code files")
    print(f"[collect] by suffix: {dict(Counter(f.suffix for f in code))}")

    result = extract(code, root=root)
    print(f"[extract] nodes={len(result.get('nodes', []))} "
          f"edges={len(result.get('edges', []))}")

    graph = build([result], directed=True, root=root)
    before = (graph.number_of_nodes(), graph.number_of_edges())
    graph.remove_nodes_from(
        [n for n in list(graph.nodes) if str(n) in STDLIB_AND_THIRD_PARTY]
    )
    print(f"[build] {before[0]} nodes/{before[1]} edges -> "
          f"{graph.number_of_nodes()} nodes/{graph.number_of_edges()} edges "
          f"(stdlib removed)")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    data = nx.node_link_data(graph, edges="links")
    OUT_GRAPH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(f"[write] {OUT_GRAPH} ({OUT_GRAPH.stat().st_size // 1024} KB)")
    return 0


def _run_cli(args: list[str]) -> int:
    if not VENV_CLI.is_file():
        return _fail(f"graphify CLI 不存在：{VENV_CLI}")
    if not OUT_GRAPH.is_file():
        return _fail(f"过滤版图不存在：{OUT_GRAPH}（先不带参数运行本脚本）")
    r = subprocess.run([str(VENV_CLI), *args, "--graph", str(OUT_GRAPH)],
                       cwd=str(ROOT))
    return r.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description="graphify 过滤版构建/查询")
    ap.add_argument("--_build", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--explain", metavar="NODE", help="解释一个节点的连接")
    ap.add_argument("--path", nargs=2, metavar=("A", "B"),
                    help="两个节点间最短路径（须回源码核对）")
    args = ap.parse_args()

    if args._build:
        return _do_build()
    if args.explain:
        return _run_cli(["explain", args.explain])
    if args.path:
        return _run_cli(["path", *args.path, "--undirected"])
    return build_graph()


if __name__ == "__main__":
    raise SystemExit(main())
