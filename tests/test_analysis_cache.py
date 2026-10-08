"""analysis_cache 单测：键语义、命中/未命中、损坏降级、版本迁移、LRU、孤儿清理。

不依赖真实视频：用假 media_info/snapshot 构造键，用 numpy 造小产物。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from analysis_cache import (
    ANALYSIS_CACHE_VERSION,
    AnalysisCache,
    AnalysisCacheKey,
    ffmpeg_identity,
    make_key,
    params_fingerprint,
)


def _media(sha: str = "a" * 64, ff_sha: str = "f" * 64, ver: str = "ffmpeg 7.1.1"):
    return SimpleNamespace(
        source_sha256=sha,
        ffmpeg=SimpleNamespace(sha256=ff_sha, version_line=ver),
    )


def _snapshot(**over):
    base = {
        "thresholds": {"pause": 0.7, "speed_1x": 0.8},
        "compare": {"motion_thresh": 2.0, "still_time_thresh": 0.1},
        "proc_res": (400, 225),
        "batch": 128,
        "threads": 16,
        "fps": 60.0,
        "backend_key": "ffmpeg_sw_passthrough",
    }
    base.update(over)
    return base


def _cache(tmp_path: Path) -> AnalysisCache:
    return AnalysisCache(tmp_path / "ac", max_entries=8)


def _artifacts(n: int = 1000):
    states = np.zeros(n, dtype=np.int8)
    states[10:20] = 1
    diffs = np.linspace(0, 1, n).astype(np.float32)
    return states, diffs


class TestKey:
    def test_same_inputs_same_digest(self):
        m, s = _media(), _snapshot()
        assert make_key(m, s).digest() == make_key(m, s).digest()

    def test_param_change_changes_digest(self):
        # §8.1 风险 #1：阈值/参数变化必须换键（否则静默错命中）
        m = _media()
        k1 = make_key(m, _snapshot())
        for field, value in (
            ("thresholds", {"pause": 0.9, "speed_1x": 0.8}),
            ("compare", {"motion_thresh": 3.0, "still_time_thresh": 0.1}),
            ("proc_res", (800, 450)),
            ("batch", 64),
            ("threads", 8),
            ("fps", 59.94),
        ):
            k2 = make_key(m, _snapshot(**{field: value}))
            assert k1.digest() != k2.digest(), f"{field} 未进键"

    def test_backend_changes_digest(self):
        m = _media()
        assert (make_key(m, _snapshot()).digest()
                != make_key(m, _snapshot(backend_key="opencv")).digest())

    def test_source_change_changes_digest(self):
        s = _snapshot()
        assert make_key(_media(sha="a" * 64), s).digest() \
            != make_key(_media(sha="b" * 64), s).digest()

    def test_ffmpeg_identity_change_changes_digest(self):
        s = _snapshot()
        k1 = make_key(_media(ff_sha="f" * 64), s)
        k2 = make_key(_media(ff_sha="e" * 64), s)
        k3 = make_key(_media(ver="ffmpeg 8.0"), s)
        assert k1.digest() != k2.digest()
        assert k1.digest() != k3.digest()

    def test_format_version_in_digest(self):
        k1 = AnalysisCacheKey("a" * 64, "p" * 32, "opencv", "f" * 16,
                              format_version=1)
        k2 = AnalysisCacheKey("a" * 64, "p" * 32, "opencv", "f" * 16,
                              format_version=2)
        assert k1.digest() != k2.digest()

    def test_missing_ffmpeg_is_stable(self):
        m = SimpleNamespace(source_sha256="a" * 64, ffmpeg=None)
        assert ffmpeg_identity(m) == "none"


class TestNumpyTruthiness:
    """09-16 review：对 numpy 数组用 `or` 会抛真值歧义（两处同类缺陷）。"""

    def test_proc_res_as_ndarray(self):
        import numpy as np
        base = {"thresholds": {}, "compare": {}, "batch": 1, "threads": 1,
                "fps": 60.0}
        a = params_fingerprint({**base, "proc_res": (400, 225)})
        b = params_fingerprint({**base, "proc_res": np.array([400, 225])})
        assert a == b          # 同值同指纹，且不抛异常

    def test_proc_res_missing(self):
        assert params_fingerprint({"thresholds": {}, "compare": {}, "batch": 1,
                                   "threads": 1, "fps": 60.0})


class TestPoisoningGuard:
    """09-16 review：源身份缺失时必须拒绝构造键（否则所有视频撞同一键）。"""

    def test_make_key_rejects_missing_media(self):
        from analysis_cache import AnalysisCacheError
        with pytest.raises(AnalysisCacheError) as ei:
            make_key(None, _snapshot())
        assert ei.value.code == "KEY_SOURCE_UNIDENTIFIED"

    def test_make_key_rejects_empty_sha(self):
        from analysis_cache import AnalysisCacheError
        m = SimpleNamespace(source_sha256="", ffmpeg=None)
        with pytest.raises(AnalysisCacheError):
            make_key(m, _snapshot())

    def test_make_key_rejects_short_sha(self):
        from analysis_cache import AnalysisCacheError
        m = SimpleNamespace(source_sha256="abc", ffmpeg=None)
        with pytest.raises(AnalysisCacheError):
            make_key(m, _snapshot())

    def test_load_rejects_invalid_key(self, tmp_path):
        from analysis_cache import AnalysisCacheError
        c = _cache(tmp_path)
        bad = AnalysisCacheKey("", "p" * 32, "opencv", "f" * 16)
        with pytest.raises(AnalysisCacheError):
            c.load(bad)

    def test_store_rejects_invalid_key(self, tmp_path):
        from analysis_cache import AnalysisCacheError
        c = _cache(tmp_path)
        bad = AnalysisCacheKey("not-a-sha", "p" * 32, "opencv", "f" * 16)
        states, diffs = _artifacts(100)
        with pytest.raises(AnalysisCacheError):
            c.store(bad, states=states, diffs=diffs, pauses=[], speeds=[])


class TestRoundTrip:
    def test_store_then_load(self, tmp_path):
        c = _cache(tmp_path)
        m, s = _media(), _snapshot()
        key = make_key(m, s)
        states, diffs = _artifacts()
        assert c.store(key, states=states, diffs=diffs,
                       pauses=[{"id": 1, "start": 0, "end": 9, "mode": "auto"}],
                       speeds=[], backend_label="FFmpeg A_PT",
                       source_size=123, source_mtime_ns=456) is not None
        got = c.load(key)
        assert got is not None
        assert np.array_equal(got["states"], states)
        assert np.allclose(got["diffs"], diffs)
        assert got["pauses"] and got["pauses"][0]["id"] == 1
        assert got["meta"]["backend_label"] == "FFmpeg A_PT"
        assert got["meta"]["source_size"] == 123
        assert got["meta"]["source_mtime_ns"] == 456

    def test_miss_when_absent(self, tmp_path):
        c = _cache(tmp_path)
        assert c.load(make_key(_media(), _snapshot())) is None

    def test_miss_on_param_change(self, tmp_path):
        c = _cache(tmp_path)
        m = _media()
        states, diffs = _artifacts()
        c.store(make_key(m, _snapshot()), states=states, diffs=diffs,
                pauses=[], speeds=[])
        # 换阈值 → 不同键 → 未命中（不会拿到旧参数结果）
        assert c.load(make_key(m, _snapshot(
            thresholds={"pause": 0.95, "speed_1x": 0.8}))) is None

    def test_miss_on_backend_change(self, tmp_path):
        c = _cache(tmp_path)
        m = _media()
        states, diffs = _artifacts()
        c.store(make_key(m, _snapshot()), states=states, diffs=diffs,
                pauses=[], speeds=[])
        assert c.load(make_key(m, _snapshot(backend_key="opencv"))) is None


class TestCorruption:
    def test_corrupted_file_is_miss_not_crash(self, tmp_path):
        c = _cache(tmp_path)
        m, s = _media(), _snapshot()
        key = make_key(m, s)
        states, diffs = _artifacts()
        assert c.store(key, states=states, diffs=diffs, pauses=[], speeds=[])
        path = c._path_for(key)
        raw = bytearray(path.read_bytes())
        raw[len(raw) // 2] ^= 0xFF          # 翻转一个字节（CRC 应捕获）
        path.write_bytes(bytes(raw))
        assert c.load(key) is None           # 降级为未命中，不抛异常

    def test_truncated_file_is_miss(self, tmp_path):
        c = _cache(tmp_path)
        key = make_key(_media(), _snapshot())
        states, diffs = _artifacts()
        c.store(key, states=states, diffs=diffs, pauses=[], speeds=[])
        path = c._path_for(key)
        path.write_bytes(path.read_bytes()[:64])
        assert c.load(key) is None

    def test_garbage_file_is_miss(self, tmp_path):
        c = _cache(tmp_path)
        key = make_key(_media(), _snapshot())
        path = c._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not an npz at all")
        assert c.load(key) is None


class TestVersionMigration:
    def test_old_version_unreachable(self, tmp_path):
        # 版本号进键（§8.3 方案 A）：旧版产物自然不可达
        c = _cache(tmp_path)
        m, s = _media(), _snapshot()
        old = AnalysisCacheKey(m.source_sha256, params_fingerprint(s),
                               "ffmpeg_sw_passthrough",
                               ffmpeg_identity(m), format_version=0)
        states, diffs = _artifacts()
        c.store(old, states=states, diffs=diffs, pauses=[], speeds=[])
        new_key = make_key(m, s)   # format_version=ANALYSIS_CACHE_VERSION
        assert old.digest() != new_key.digest()
        assert c.load(new_key) is None


class TestRealShapePauses:
    """09-16 致命缺陷回归：真实 analyzer 产物的 pauses 带 numpy mask。

    v1 用 json.dumps 打包整个 meta → TypeError 被吞 → **从未写成功过**。
    v2 把 mask 抽为 npz 独立成员。以下用例的 pauses **由真实生产函数
    build_segments 产出**（R8：不手搓近似样本）。
    """

    @staticmethod
    def _real_pauses():
        import analyzer
        states = np.zeros(300, dtype=np.int8)
        states[10:60] = analyzer.FRAME_TYPE_PAUSE
        states[60:100] = analyzer.FRAME_TYPE_1X
        states[100:180] = analyzer.FRAME_TYPE_PAUSE
        diffs = np.zeros(300, dtype=np.float32)
        diffs[100:180] = 3.0
        return analyzer.build_segments(
            states, diffs, "x.mp4", (400, 225),
            {"still_time_thresh": 0.1, "motion_thresh": 2.0,
             "boundary_thresh": 5.0}, 60.0)

    def test_real_pauses_are_ndarray_masked(self):
        # 口径自证：确认真实产物就是 ndarray（否则本类测试无意义）
        pauses, _ = self._real_pauses()
        assert pauses
        assert all(isinstance(p["local_del_mask"], np.ndarray)
                   for p in pauses)

    def test_real_pauses_store_succeeds(self, tmp_path):
        # v1 会在此失败（store 返回 None / 抛错）
        c = _cache(tmp_path)
        states, diffs = _artifacts(300)
        pauses, speeds = self._real_pauses()
        written = c.store(make_key(_media(), _snapshot()), states=states,
                          diffs=diffs, pauses=pauses, speeds=speeds)
        assert written.exists()

    def test_real_pauses_round_trip_bit_exact(self, tmp_path):
        c = _cache(tmp_path)
        key = make_key(_media(), _snapshot())
        states, diffs = _artifacts(300)
        pauses, speeds = self._real_pauses()
        c.store(key, states=states, diffs=diffs, pauses=pauses, speeds=speeds)
        got = c.load(key)
        assert got is not None
        assert len(got["pauses"]) == len(pauses)
        for back, orig in zip(got["pauses"], pauses):
            assert isinstance(back["local_del_mask"], np.ndarray)
            assert back["local_del_mask"].dtype == np.uint8
            assert np.array_equal(back["local_del_mask"],
                                  orig["local_del_mask"])
            assert back["mode"] == orig["mode"]
            assert back["start"] == orig["start"]
        assert got["speeds"] == speeds

    def test_placeholder_not_leaked(self, tmp_path):
        c = _cache(tmp_path)
        key = make_key(_media(), _snapshot())
        states, diffs = _artifacts(300)
        pauses, speeds = self._real_pauses()
        c.store(key, states=states, diffs=diffs, pauses=pauses, speeds=speeds)
        got = c.load(key)
        for seg in got["pauses"]:
            assert "__npz_mask__" not in seg

    def test_loaded_pauses_consumable_downstream(self, tmp_path):
        # 载入产物必须能被真实下游消费（project_state + 导出计划）
        import analyzer
        import project_state
        c = _cache(tmp_path)
        key = make_key(_media(), _snapshot())
        states, diffs = self._real_pauses()
        pauses, speeds = self._real_pauses()
        states = np.zeros(300, dtype=np.int8)
        states[10:60] = analyzer.FRAME_TYPE_PAUSE
        states[100:180] = analyzer.FRAME_TYPE_PAUSE
        diffs = np.zeros(300, dtype=np.float32)
        c.store(key, states=states, diffs=diffs, pauses=pauses,
                speeds=speeds)
        got = c.load(key)
        ps = project_state.ProjectState()
        ps.replace_timeline(got["pauses"], got["speeds"], [])
        segs = ps.snapshot().mutable_segments()
        plan = analyzer.build_timeline_plan(
            300, got["states"], segs[0], segs[1], segs[2], False, False, 1)
        assert sum(e - s for s, e in plan.deleted_ranges) > 0

    def test_mask_absent_and_none_stay_distinct(self, tmp_path):
        # 三态：有 mask / mask=None / 无字段——语义必须保持
        c = _cache(tmp_path)
        key = make_key(_media(), _snapshot())
        states, diffs = _artifacts(100)
        pauses = [
            {"id": 0, "start": 0, "end": 9, "mode": "auto",
             "local_del_mask": np.array([0, 1, 2] * 3 + [1], dtype=np.uint8)},
            {"id": 1, "start": 10, "end": 19, "mode": "auto",
             "local_del_mask": None},
            {"id": 2, "start": 20, "end": 29, "mode": "all"},
        ]
        c.store(key, states=states, diffs=diffs, pauses=pauses, speeds=[])
        got = c.load(key)
        first, second, third = got["pauses"]
        assert isinstance(first["local_del_mask"], np.ndarray)
        assert np.array_equal(first["local_del_mask"],
                              pauses[0]["local_del_mask"])
        assert second["local_del_mask"] is None
        assert "local_del_mask" not in third

    def test_mask_value_loss_rejected(self, tmp_path):
        # uint8 静默截断（如 300→44）必须被拒——静默错数据比崩溃更坏
        from analysis_cache import AnalysisCacheError
        c = _cache(tmp_path)
        key = make_key(_media(), _snapshot())
        states, diffs = _artifacts(50)
        with pytest.raises(AnalysisCacheError) as ei:
            c.store(key, states=states, diffs=diffs, pauses=[
                {"id": 0, "start": 0, "end": 2, "mode": "auto",
                 "local_del_mask": np.array([0, 300, 1])}], speeds=[])
        assert ei.value.code == "STORE_MASK_VALUE_LOSS"
        assert list(c.dir.glob("*.npz")) == []      # 不留半成品

    def test_mask_2d_rejected(self, tmp_path):
        # 二维 mask 会按行切片还原出错误形状 → 必须拒绝
        from analysis_cache import AnalysisCacheError
        c = _cache(tmp_path)
        key = make_key(_media(), _snapshot())
        states, diffs = _artifacts(50)
        with pytest.raises(AnalysisCacheError) as ei:
            c.store(key, states=states, diffs=diffs, pauses=[
                {"id": 0, "start": 0, "end": 2, "mode": "auto",
                 "local_del_mask": np.zeros((2, 2), dtype=np.uint8)}],
                speeds=[])
        assert ei.value.code == "STORE_MASK_INVALID_SHAPE"

    def test_mask_bool_and_float_roundtrip(self, tmp_path):
        # 合法等价类型（bool/float 的 0/1）应正常通过并还原为 uint8
        c = _cache(tmp_path)
        key = make_key(_media(), _snapshot())
        states, diffs = _artifacts(50)
        c.store(key, states=states, diffs=diffs, pauses=[
            {"id": 0, "start": 0, "end": 3, "mode": "auto",
             "local_del_mask": np.array([True, False, True, False])}],
            speeds=[])
        got = c.load(key)
        assert got["pauses"][0]["local_del_mask"].tolist() == [1, 0, 1, 0]

    def test_analysis_path_flags_round_trip(self, tmp_path):
        # context_used/templates_missing 入档：缓存命中时提示框须复述**当时**
        # 的真实路径（写死 True 会谎称"已跳过二次扫片"）
        c = _cache(tmp_path)
        key = make_key(_media(), _snapshot())
        states, diffs = _artifacts(100)
        pauses, speeds = self._real_pauses()
        c.store(key, states=states, diffs=diffs, pauses=pauses, speeds=speeds,
                context_used=False, templates_missing=True)
        got = c.load(key)
        assert got["meta"]["context_used"] is False
        assert got["meta"]["templates_missing"] is True

    def test_analysis_path_flags_default_absent(self, tmp_path):
        # 未显式给出时的默认（老调用方兼容）：context_used=True/无缺失
        c = _cache(tmp_path)
        key = make_key(_media(), _snapshot())
        states, diffs = _artifacts(100)
        c.store(key, states=states, diffs=diffs, pauses=[], speeds=[])
        got = c.load(key)
        assert got["meta"]["context_used"] is True
        assert got["meta"]["templates_missing"] is False

    def test_inconsistent_mask_lengths_is_miss(self, tmp_path):
        # 内部不一致（长度和≠拼接体）必须整次载入失败——宁可回退完整分析，
        # 也不返回"少了几段 mask"的错结果（静默错数据比崩溃更坏）
        c = _cache(tmp_path)
        key = make_key(_media(), _snapshot())
        states, diffs = _artifacts(50)
        c.store(key, states=states, diffs=diffs, pauses=[
            {"id": 0, "start": 0, "end": 9, "mode": "auto",
             "local_del_mask": np.zeros(10, dtype=np.uint8)}], speeds=[])
        path = c._path_for(key)
        with np.load(path, allow_pickle=False) as d:
            data = {k: np.array(d[k]) for k in d.files}
        data["mask_lengths"] = np.array([11], dtype=np.int64)   # 篡改
        np.savez(path, **data)
        assert c.load(key) is None

    def test_mask_index_out_of_range_is_miss(self, tmp_path):
        c = _cache(tmp_path)
        key = make_key(_media(), _snapshot())
        states, diffs = _artifacts(50)
        c.store(key, states=states, diffs=diffs, pauses=[
            {"id": 0, "start": 0, "end": 9, "mode": "auto",
             "local_del_mask": np.zeros(10, dtype=np.uint8)}], speeds=[])
        path = c._path_for(key)
        with np.load(path, allow_pickle=False) as d:
            data = {k: np.array(d[k]) for k in d.files}
        meta = json.loads(str(data["meta_json"]))
        meta["pauses"][0]["__npz_mask__"] = 5      # 越界索引
        data["meta_json"] = np.array(json.dumps(meta))
        np.savez(path, **data)
        assert c.load(key) is None

    def test_store_raises_with_code_on_unserializable(self, tmp_path):
        # 不可序列化的 meta 必须**带 code 抛出**（不再吞成 None）
        from analysis_cache import AnalysisCacheError
        c = _cache(tmp_path)
        key = make_key(_media(), _snapshot())
        states, diffs = _artifacts(50)
        with pytest.raises(AnalysisCacheError) as ei:
            c.store(key, states=states, diffs=diffs,
                    pauses=[{"id": 0, "start": 0, "end": 9,
                             "weird": object()}], speeds=[])
        assert ei.value.code == "STORE_META_NOT_SERIALIZABLE"

    def test_no_tmp_left_after_failure(self, tmp_path):
        c = _cache(tmp_path)
        key = make_key(_media(), _snapshot())
        states, diffs = _artifacts(50)
        try:
            c.store(key, states=states, diffs=diffs,
                    pauses=[{"id": 0, "weird": object()}], speeds=[])
        except Exception:
            pass
        assert list(c.dir.glob(".tmp_analysis_*")) == []


class TestTemplateFingerprint:
    """09-16 review 发现：分类完全靠模板匹配，模板必须进键（否则静默错命中）。"""

    def _fake_root(self, tmp_path: Path):
        root = tmp_path / "repo"
        for name in ("templates_pause", "templates_1x", "templates_2x",
                     "templates_play"):
            d = root / name
            d.mkdir(parents=True)
            (d / "t.png").write_bytes(b"\x89PNG-fake-" + name.encode())
        return root

    def test_template_fingerprint_stable(self, tmp_path):
        from analysis_cache import templates_fingerprint as tf
        root = self._fake_root(tmp_path)
        assert tf(root) == tf(root)
        assert tf(root) != "none"

    def test_template_content_change_changes_fingerprint(self, tmp_path):
        from analysis_cache import templates_fingerprint as tf
        root = self._fake_root(tmp_path)
        before = tf(root)
        (root / "templates_pause" / "t.png").write_bytes(b"DIFFERENT")
        assert tf(root) != before

    def test_template_add_change_changes_fingerprint(self, tmp_path):
        from analysis_cache import templates_fingerprint as tf
        root = self._fake_root(tmp_path)
        before = tf(root)
        (root / "templates_pause" / "2.png").write_bytes(b"new template")
        assert tf(root) != before

    def test_missing_dirs_is_none(self, tmp_path):
        from analysis_cache import templates_fingerprint as tf
        assert tf(tmp_path / "empty") == "none"

    def test_snapshot_override_and_key_inclusion(self, tmp_path):
        # snapshot 可显式给出（测试/迁移），且必须影响 digest
        m = _media()
        base = _snapshot()
        k1 = make_key(m, {**base, "templates_fingerprint": "aaaa"})
        k2 = make_key(m, {**base, "templates_fingerprint": "bbbb"})
        assert k1.digest() != k2.digest()
        assert k1.templates_fingerprint == "aaaa"

    def test_real_repo_templates_are_fingerprinted(self):
        # 生产默认按 CWD 解析（与 analyzer.load_templates 口径一致），故这里
        # 显式传仓库根，避免测试结果依赖 pytest 的工作目录（09-16 复查：
        # 原写法 tf() != "none" 从非仓库目录跑会假失败）
        from analysis_cache import templates_fingerprint as tf
        repo = Path(__file__).resolve().parents[1]
        assert tf(repo) != "none"

    def test_default_fingerprint_follows_cwd(self, tmp_path):
        # 默认参数=CWD（口径自证）：这是与 load_templates 对齐的有意设计
        import os
        from analysis_cache import templates_fingerprint as tf
        root = tmp_path / "root"
        (root / "templates_pause").mkdir(parents=True)
        (root / "templates_pause" / "1.png").write_bytes(b"X")
        old = os.getcwd()
        try:
            os.chdir(root)
            assert tf() != "none"
        finally:
            os.chdir(old)


class TestMaskTamperGuards:
    """09-16 review：损坏检测边界补齐（负数长度/重复索引）。"""

    @staticmethod
    def _rewrite(path: Path, **overrides):
        with np.load(path, allow_pickle=False) as d:
            data = {k: np.array(d[k]) for k in d.files}
        data.update(overrides)
        np.savez(path, **data)

    def test_negative_length_rejected(self, tmp_path):
        c = _cache(tmp_path)
        key = make_key(_media(), _snapshot())
        states, diffs = _artifacts(50)
        c.store(key, states=states, diffs=diffs, pauses=[
            {"id": 0, "start": 0, "end": 9, "mode": "auto",
             "local_del_mask": np.zeros(10, dtype=np.uint8)}], speeds=[])
        self._rewrite(c._path_for(key),
                      mask_lengths=np.array([-1, 11], dtype=np.int64))
        assert c.load(key) is None

    def test_duplicate_index_rejected(self, tmp_path):
        c = _cache(tmp_path)
        key = make_key(_media(), _snapshot())
        states, diffs = _artifacts(50)
        c.store(key, states=states, diffs=diffs, pauses=[
            {"id": 0, "start": 0, "end": 4, "mode": "auto",
             "local_del_mask": np.zeros(5, dtype=np.uint8)},
            {"id": 1, "start": 5, "end": 9, "mode": "auto",
             "local_del_mask": np.ones(5, dtype=np.uint8)}], speeds=[])
        path = c._path_for(key)
        with np.load(path, allow_pickle=False) as d:
            data = {k: np.array(d[k]) for k in d.files}
        meta = json.loads(str(data["meta_json"]))
        meta["pauses"][1]["__npz_mask__"] = 0    # 两段指向同一张 mask
        data["meta_json"] = np.array(json.dumps(meta))
        np.savez(path, **data)
        assert c.load(key) is None

    def test_meta_pauses_placeholder_replaced(self, tmp_path):
        # 载入后 meta["pauses"] 不应留占位符（否则再 store 会错配）
        c = _cache(tmp_path)
        key = make_key(_media(), _snapshot())
        states, diffs = _artifacts(50)
        c.store(key, states=states, diffs=diffs, pauses=[
            {"id": 0, "start": 0, "end": 9, "mode": "auto",
             "local_del_mask": np.zeros(10, dtype=np.uint8)}], speeds=[])
        got = c.load(key)
        assert "__npz_mask__" not in got["meta"]["pauses"][0]
        assert isinstance(got["meta"]["pauses"][0]["local_del_mask"],
                          np.ndarray)

    def test_restore_after_load_is_stable(self, tmp_path):
        # store(load(x)) 必须与 x 等价（回归：占位符残留会导致错配）
        c = _cache(tmp_path)
        m, s = _media(), _snapshot()
        k1 = make_key(m, s)
        states, diffs = _artifacts(50)
        pauses = [{"id": 0, "start": 0, "end": 9, "mode": "auto",
                   "local_del_mask": np.array([0, 1] * 5, dtype=np.uint8)}]
        c.store(k1, states=states, diffs=diffs, pauses=pauses, speeds=[])
        got = c.load(k1)
        c.store(k1, states=got["states"], diffs=got["diffs"],
                pauses=got["pauses"], speeds=got["speeds"])
        again = c.load(k1)
        assert np.array_equal(again["pauses"][0]["local_del_mask"],
                              pauses[0]["local_del_mask"])


class TestLosslessCasts:
    """09-16 review：states/diffs 的 dtype 转换也必须无损（与 mask 同类）。"""

    def test_states_value_loss_rejected(self, tmp_path):
        from analysis_cache import AnalysisCacheError
        c = _cache(tmp_path)
        key = make_key(_media(), _snapshot())
        # int64 300 装不进 int8（会静默变 44）→ 必须拒绝
        bad_states = np.array([0, 200, 300], dtype=np.int64)
        with pytest.raises(AnalysisCacheError) as ei:
            c.store(key, states=bad_states,
                    diffs=np.zeros(3, dtype=np.float32), pauses=[], speeds=[])
        assert ei.value.code == "STORE_STATES_VALUE_LOSS"

    def test_diffs_value_loss_rejected(self, tmp_path):
        from analysis_cache import AnalysisCacheError
        c = _cache(tmp_path)
        key = make_key(_media(), _snapshot())
        # 1e40 超出 float32 范围 → inf（无损检查须捕获）
        bad_diffs = np.array([0.0, 1e40], dtype=np.float64)
        with pytest.raises(AnalysisCacheError) as ei:
            c.store(key, states=np.zeros(2, dtype=np.int8),
                    diffs=bad_diffs, pauses=[], speeds=[])
        assert ei.value.code == "STORE_DIFFS_VALUE_LOSS"

    def test_float64_precision_rounding_is_allowed(self, tmp_path):
        # 09-16 复查：float64 的 0.1→float32 是目标格式定义内的舍入，**不是**
        # 数据损坏；原实现用 array_equal 会误拒（把合法输入全挡掉）
        c = _cache(tmp_path)
        key = make_key(_media(), _snapshot())
        d64 = np.array([0.1, 0.2, 0.3, 0.0], dtype=np.float64)
        written = c.store(key, states=np.zeros(4, dtype=np.int8),
                          diffs=d64, pauses=[], speeds=[])
        assert written.exists()
        got = c.load(key)
        # 值应是 float32 舍入后的结果（而非被拒或全零）
        assert np.allclose(got["diffs"], d64, atol=1e-6)

    def test_nan_diffs_pass_through(self, tmp_path):
        # NaN→NaN 视为无损（equal_nan 语义），不炸
        c = _cache(tmp_path)
        key = make_key(_media(), _snapshot())
        d = np.array([0.0, np.nan], dtype=np.float32)
        c.store(key, states=np.zeros(2, dtype=np.int8), diffs=d,
                pauses=[], speeds=[])
        got = c.load(key)
        assert np.isnan(got["diffs"][1])

    def test_int64_states_in_range_allowed(self, tmp_path):
        # 整数判据仍是逐元素相等：值域内的 int64 允许（转 int8 可精确往返）
        c = _cache(tmp_path)
        key = make_key(_media(), _snapshot())
        st = np.array([0, 1, 4, -128, 127], dtype=np.int64)
        c.store(key, states=st, diffs=np.zeros(5, dtype=np.float32),
                pauses=[], speeds=[])
        assert np.array_equal(c.load(key)["states"], st)

    def test_valid_round_trip_still_works(self, tmp_path):
        c = _cache(tmp_path)
        key = make_key(_media(), _snapshot())
        states = np.array([0, 1, 4], dtype=np.int64)   # 值域内即可
        diffs = np.array([0.5, 1.25, 2.0], dtype=np.float64)
        c.store(key, states=states, diffs=diffs, pauses=[], speeds=[])
        got = c.load(key)
        assert np.array_equal(got["states"], states)
        assert np.allclose(got["diffs"], diffs)

    def test_pack_failure_is_coded(self, tmp_path):
        # _pack_pauses 抛出的裸异常也必须包成带 code 的 AnalysisCacheError
        from analysis_cache import AnalysisCacheError
        c = _cache(tmp_path)
        key = make_key(_media(), _snapshot())
        with pytest.raises(AnalysisCacheError) as ei:
            c.store(key, states=np.zeros(3, dtype=np.int8),
                    diffs=np.zeros(3, dtype=np.float32),
                    pauses=[{"id": 0, "start": 0, "end": 2, "mode": "auto",
                             "local_del_mask": np.array(["a", "b", "c"])}],
                    speeds=[])
        assert ei.value.code


class TestMaintenance:
    def test_evict_keeps_newest(self, tmp_path):
        c = AnalysisCache(tmp_path / "ac", max_entries=2)
        states, diffs = _artifacts(100)
        keys = []
        for i in range(4):
            k = AnalysisCacheKey(f"{i:064d}", "p" * 32, "opencv", "f" * 16)
            keys.append(k)
            c.store(k, states=states, diffs=diffs, pauses=[], speeds=[])
            time.sleep(0.02)          # 拉开 mtime
        assert c.evict() >= 0
        remaining = sorted(p.name for p in (tmp_path / "ac").glob("*.npz"))
        assert len(remaining) <= 2
        # 最新两个应存活
        assert keys[-1].digest()[:32] + ".npz" in remaining

    def test_cleanup_orphans(self, tmp_path):
        c = _cache(tmp_path)
        c.dir.mkdir(parents=True, exist_ok=True)
        orphan = c.dir / ".tmp_analysis_dead.npz"
        orphan.write_bytes(b"x")
        assert c.cleanup_orphans() == 1
        assert not orphan.exists()

    def test_describe(self, tmp_path):
        c = _cache(tmp_path)
        m, s = _media(), _snapshot()
        key = make_key(m, s)
        states, diffs = _artifacts(200_000)   # 足够大以让 mb 非零
        c.store(key, states=states, diffs=diffs,
                pauses=[{"id": 1}], speeds=[{"id": 2}],
                backend_label="FFmpeg A_PT")
        info = c.describe(key)
        assert info is not None
        assert info["pause_n"] == 1 and info["speed_n"] == 1
        assert info["mb"] >= 0.0   # 测试数据小，可能舍入到 0
        assert info["created_wall"]


class TestAtomicWrite:
    def test_no_tmp_left_behind(self, tmp_path):
        c = _cache(tmp_path)
        key = make_key(_media(), _snapshot())
        states, diffs = _artifacts(100)
        c.store(key, states=states, diffs=diffs, pauses=[], speeds=[])
        assert list(c.dir.glob(".tmp_analysis_*")) == []

    def test_store_reports_failure_with_code(self, tmp_path):
        # 写失败必须带原因抛出（v1 曾吞成 None，令"从未写成功"被掩盖）
        from analysis_cache import AnalysisCacheError
        c = AnalysisCache(tmp_path / "ac")
        c.dir.mkdir(parents=True, exist_ok=True)
        key = make_key(_media(), _snapshot())
        states, diffs = _artifacts(100)
        try:
            result = c.store(key, states=states, diffs=diffs, pauses=[],
                             speeds=[])
        except AnalysisCacheError as exc:
            assert exc.code  # 带机器可读 code
        else:
            assert result.exists()   # 环境可写 → 正常落盘
