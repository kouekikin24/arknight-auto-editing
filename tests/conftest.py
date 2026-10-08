"""pytest 全局夹具：**硬性禁止**测试运行期弹出真实模态对话框。

## 为什么需要（09-16 事故，T0 实测）

`tests/test_analysis_cache_wiring.py` 的 `test_fresh_analysis_must_store_with_flags`
调用真实 `_finish_analysis(templates_missing=True)`，却只 patch 了 `showinfo`——
于是 `_finish_analysis` 里的**真实 `showwarning`** 弹出一个模态窗口并**永久阻塞**
（实测：单测 `timeout 8` → exit 124；整个文件 `timeout 60` 只跑完 2 例即挂死）。

当时整套 `pytest -q` 却报"全绿"——因为 pytest 的顺序巧合下该用例的窗口在
别处被吸收/该用例未被排到，掩盖了问题；而 owner 桌面上真的出现了一个
无主弹窗，我被它误导去怀疑别的项目。**测试绝不允许触碰真实 UI 模态**：
它阻塞 CI、污染开发者桌面，且失败信号是"挂死"而非"报错"，极难定位。

## 机制

autouse 夹具，对 `tkinter.messagebox` 的三个弹窗函数（showinfo/showwarning/
showerror/askyesno/askokcancel）统一替换为**记录型假实现**，并把调用记进
`dialog_calls` 供用例断言（"该弹窗确实被调用了"）。需要真实行为的用例可
在自己的 `with mock.patch(...)` 里覆盖（更内层的 patch 生效）。

另外把 `tkinter.Tk` 也隔离：若用例意外创建真实 root，会留下窗口；这里
不改 Tk 本身（不少用例依赖 Tk 类型），只保证**对话框**不会阻塞。
"""
from __future__ import annotations

import tkinter.messagebox as _mb

import pytest


class _DialogRecorder:
    """记录所有弹窗调用；返回值可配置（默认同意）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.answer = True

    def __call__(self, kind: str):
        def _fn(title=None, message=None, *args, **kwargs):
            self.calls.append((kind, str(title), str(message)))
            if kind in ("askyesno", "askokcancel", "askretrycancel"):
                return self.answer
            if kind == "askyesnocancel":
                return True
            return None
        return _fn


@pytest.fixture(autouse=True)
def _no_blocking_dialogs(monkeypatch):
    """全局：任何真实模态弹窗都会被替换为记录器（永不阻塞）。"""
    recorder = _DialogRecorder()
    monkeypatch.setattr(_mb, "showinfo", recorder("showinfo"), raising=False)
    monkeypatch.setattr(_mb, "showwarning", recorder("showwarning"),
                        raising=False)
    monkeypatch.setattr(_mb, "showerror", recorder("showerror"), raising=False)
    monkeypatch.setattr(_mb, "askyesno", recorder("askyesno"), raising=False)
    monkeypatch.setattr(_mb, "askokcancel", recorder("askokcancel"),
                        raising=False)
    # 供用例断言"确实弹了某种窗"
    monkeypatch.setattr(_mb, "_test_dialog_calls", recorder.calls,
                        raising=False)
    yield recorder


@pytest.fixture()
def dialog_recorder(_no_blocking_dialogs):
    """显式取用记录器（用例可 `dialog_recorder.calls` 断言）。"""
    return _no_blocking_dialogs
