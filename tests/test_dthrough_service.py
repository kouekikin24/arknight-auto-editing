"""dthrough_service 单测（1d）：事件层免进程直测 + spawn 契约。

事件层是直通台账实测轴的数据源（1c-① 禁名义值），gen 门是 seek 竞态
（旧代尾 eof 把新流误标 EOF）的防线——全部免 dsvc.exe 可测。
"""

import threading

import pytest

from dthrough_service import DThroughService, DThroughServiceError


def _svc() -> DThroughService:
    """免子进程实例：只测事件层（_handle_event/drain_actuals）。"""
    svc = object.__new__(DThroughService)
    svc._events = []
    svc._evt_cond = threading.Condition()
    svc._cond = threading.Condition()
    svc._loaded_kfs = None
    svc._per_span = None
    svc._last_error = None
    svc._span_counts = []
    svc._prev_pushed = 0
    svc._counts_complete = False
    svc._span_modes = {}
    svc._gen = 3
    svc._stream_done = False
    svc._eof = False
    svc._alive = True
    return svc


class TestSpanModes:
    def test_span_modes_captured_and_filtered(self):
        # 1d：span 事件 mode 字段捕获（cont/restart）；缺失/非法值不入表
        svc = _svc()
        svc._handle_event({"ev": "span", "si": 0, "pushed": 0, "gen": 3,
                           "mode": "restart"})
        svc._handle_event({"ev": "span", "si": 1, "pushed": 12, "gen": 3,
                           "mode": "cont"})
        svc._handle_event({"ev": "span", "si": 2, "pushed": 40, "gen": 3})
        svc._handle_event({"ev": "span", "si": 3, "pushed": 55, "gen": 3,
                           "mode": "bogus"})
        svc._handle_event({"ev": "span", "si": 4, "pushed": 60, "gen": 2,
                           "mode": "cont"})  # 旧代：gen 门拦截
        assert svc.span_modes() == {0: "restart", 1: "cont"}


class TestEventAccumulation:
    def test_span_events_accumulate_deltas(self):
        svc = _svc()
        svc._handle_event({"ev": "span", "si": 0, "pushed": 0, "gen": 3})
        svc._handle_event({"ev": "span", "si": 1, "pushed": 12, "gen": 3})
        svc._handle_event({"ev": "span", "si": 2, "pushed": 40, "gen": 3})
        assert svc.drain_actuals() == ([12, 28], False)

    def test_seek_origin_gap_fills_zeros(self):
        # seek 落段 3：新代段 0..2 未播=0，段 3 实推 7
        svc = _svc()
        svc._handle_event({"ev": "span", "si": 3, "pushed": 0, "gen": 3})
        svc._handle_event({"ev": "span", "si": 4, "pushed": 7, "gen": 3})
        assert svc.drain_actuals() == ([0, 0, 0, 7], False)

    def test_stale_gen_eof_ignored(self):
        # 1b 竞态修复的回归钉：旧代尾 eof 不得把新流误标 EOF
        svc = _svc()
        svc._handle_event({"ev": "eof", "pushed": 5, "gen": 2, "per_span": [5]})
        assert svc._stream_done is False
        assert svc.drain_actuals() is None

    def test_own_gen_eof_authoritative(self):
        svc = _svc()
        svc._handle_event({"ev": "span", "si": 1, "pushed": 9, "gen": 3})
        svc._handle_event({"ev": "eof", "pushed": 30, "gen": 3, "per_span": [9, 21]})
        assert svc._stream_done is True
        counts, complete = svc.drain_actuals()
        assert counts == [9, 21]
        assert complete is True

    def test_future_gen_events_gated(self):
        svc = _svc()
        svc._handle_event({"ev": "span", "si": 1, "pushed": 5, "gen": 4})
        assert svc.drain_actuals() is None

    def test_error_event_records_message(self):
        svc = _svc()
        svc._handle_event({"ev": "error", "gen": 3, "msg": "boom"})
        assert svc._last_error == "boom"


class TestSpawn:
    def test_missing_exe_raises_dsvc_missing(self, tmp_path):
        with pytest.raises(DThroughServiceError) as ei:
            DThroughService(str(tmp_path / "nope.exe"))
        assert ei.value.args[0] == "DSVC_MISSING"
