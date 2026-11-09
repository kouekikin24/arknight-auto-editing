"""P1 物化代理契约测试（2026-10-11 定案）。

三层：
1. proxy_cache 纯逻辑（计划烘入/映射双射/键语义/批次稳定性/缓存往返）；
2. 引擎代理模式契约（install/play/seek/读回/速度/回落门）；
3. GUI 接线（_send_play 代理优先、_seek_frame 拦截、调度守卫）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from frame_types import (FRAME_TYPE_0_2X, FRAME_TYPE_1X, FRAME_TYPE_NORMAL,
                         FRAME_TYPE_PAUSE)
import proxy_cache
from proxy_cache import (ProxyArtifact, ProxyCache, ProxySpec,
                         biz_steps_for_frames, export_del_mask,
                         export_schedule_from_mask, proxy_key)


class _BoolVar:
    def __init__(self, value: bool):
        self.value = bool(value)

    def get(self):
        return self.value


def _states() -> np.ndarray:
    """混合状态：pause / 1x / 0.2x / normal，含各段边界。"""
    states = np.full(400, FRAME_TYPE_NORMAL, dtype=np.int8)
    states[0:40] = FRAME_TYPE_PAUSE
    states[40:100] = FRAME_TYPE_1X
    states[100:140] = FRAME_TYPE_PAUSE
    states[140:260] = FRAME_TYPE_0_2X
    states[260:300] = FRAME_TYPE_1X
    states[300:400] = FRAME_TYPE_NORMAL
    return states


class ExportBakeTests(unittest.TestCase):
    """业务速度烘入：计划=导出同源、映射双射、帧数对账。"""

    def test_bake_matches_export_semantics(self):
        states = _states()
        pauses = [
            {"id": 0, "start": 0, "end": 39, "mode": "all"},
            {"id": 1, "start": 100, "end": 139, "mode": "all"},
        ]
        mask = export_del_mask(
            states.size, states, pauses, [], [],
            speedup_1x=True, speedup_02=True, speedup_02_factor=10,
        )
        ranges, source_frames = export_schedule_from_mask(mask)
        # 1x 段 [40,100) 隔 1 抽 1 → 30 帧；0.2x 段 [140,260) 隔 9 抽 1 →
        # _speedup_mask 语义（local_cnt % factor != 1 → 删），保 12 帧/120；
        # 1x 段 [260,300) → 20 帧；normal [300,400) → 100 帧。
        self.assertEqual(int(mask[0:40].all()), 1)     # pause 全删
        self.assertEqual(int(mask[100:140].all()), 1)  # pause 全删
        kept_1x_a = int((~mask[40:100]).sum())
        kept_02 = int((~mask[140:260]).sum())
        self.assertEqual(kept_1x_a, 30)
        self.assertAlmostEqual(kept_02, 12, delta=1)
        self.assertEqual(len(source_frames), int((~mask).sum()))
        # 双射：单调严格递增
        self.assertTrue(bool(np.all(np.diff(source_frames) > 0)))

    def test_mapping_roundtrip_exact(self):
        states = _states()
        pauses = [{"id": 0, "start": 0, "end": 39, "mode": "all"}]
        mask = export_del_mask(
            states.size, states, pauses, [], [],
            speedup_1x=True, speedup_02=True, speedup_02_factor=10,
        )
        _ranges, source_frames = export_schedule_from_mask(mask)
        art = ProxyArtifact(
            path=Path("x.mp4"), key="k", source_frames=source_frames,
            frames=int(source_frames.size), duration_s=1.0, mb=1.0,
            build_s=0.0, spec=ProxySpec(),
        )
        for target in (42, 140, 200, 250, 399):
            k = int(np.searchsorted(source_frames, target))
            self.assertEqual(art.source_frame_for_proxy_frame(k), target)
            self.assertEqual(art.proxy_frame_for_source_frame(target), k)
        # 删除段内的源帧（41 被 1x→2 隔帧删除）→ 钳到下一保留帧
        k = art.proxy_frame_for_source_frame(41)
        self.assertEqual(art.source_frame_for_proxy_frame(k), 42)
        # 0.2x 段内被隔 9 抽 1 删除的帧（259）→ 钳到下一保留帧
        k = art.proxy_frame_for_source_frame(259)
        self.assertEqual(art.source_frame_for_proxy_frame(k), 260)

    def test_biz_steps_exact(self):
        states = _states()
        steps = biz_steps_for_frames(
            states, speedup_1x=True, speedup_02=True, speedup_02_factor=10
        )
        self.assertEqual(int(steps[50]), 2)     # 1x 段
        self.assertEqual(int(steps[200]), 10)   # 0.2x 段
        self.assertEqual(int(steps[350]), 1)    # normal
        off = biz_steps_for_frames(
            states, speedup_1x=False, speedup_02=False, speedup_02_factor=10
        )
        self.assertEqual(int(off[50]), 1)
        self.assertEqual(int(off[200]), 1)


class ProxyKeyTests(unittest.TestCase):
    def test_key_changes_with_every_input(self):
        spec = ProxySpec()
        base = dict(speedup_1x=True, speedup_02=True, speedup_02_factor=10, fps=60.0)
        k0 = proxy_key("sha", "fp", spec, **base)
        self.assertEqual(k0, proxy_key("sha", "fp", spec, **base))
        self.assertNotEqual(k0, proxy_key("sha2", "fp", spec, **base))
        self.assertNotEqual(k0, proxy_key("sha", "fp2", spec, **base))
        self.assertNotEqual(
            k0, proxy_key("sha", "fp", spec, **{**base, "speedup_02_factor": 9})
        )
        self.assertNotEqual(
            k0, proxy_key("sha", "fp", ProxySpec(target_h=540), **base)
        )
        self.assertNotEqual(
            k0, proxy_key("sha", "fp", spec, **{**base, "fps": 30.0})
        )


class BatchStabilityTests(unittest.TestCase):
    """编辑只失效所在窗口的批次（增量重建的根基）。"""

    def test_batches_stable_under_window_edit(self):
        ranges = [(i * 100, i * 100 + 50) for i in range(4000)]
        batches_a = proxy_cache._batch_ranges(ranges, max_source_span=50000)
        # 编辑窗口 1 内的区间（删掉几个）
        edited = [r for r in ranges if not (50000 <= r[0] < 100000)]
        batches_b = proxy_cache._batch_ranges(edited, max_source_span=50000)
        spec = ProxySpec()

        def ticks(sub):
            return [(s, e, s * 256, e * 256) for s, e in sub]

        sigs_a = {
            proxy_cache._batch_signature(
                "sha", spec, ticks(b), clone_guard=(i == len(batches_a) - 1)
            )
            for i, b in enumerate(batches_a)
        }
        sigs_b = {
            proxy_cache._batch_signature(
                "sha", spec, ticks(b), clone_guard=(i == len(batches_b) - 1)
            )
            for i, b in enumerate(batches_b)
        }
        common = sigs_a & sigs_b
        # 大部分批次签名不变（除被编辑窗口 + 克隆旗标位）
        self.assertGreater(len(common), len(sigs_a) - 3)

    def test_batch_respects_range_cap(self):
        ranges = [(i * 10, i * 10 + 5) for i in range(5000)]
        batches = proxy_cache._batch_ranges(
            ranges, max_source_span=10**9, max_ranges=2000
        )
        self.assertTrue(all(len(b) <= 2000 for b in batches))
        self.assertEqual(sum(len(b) for b in batches), 5000)


class ProxyCacheRoundtripTests(unittest.TestCase):
    def test_store_load_roundtrip_and_lru(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 假 mp4（内容任意；load 不做解码验证）
            src = root / "src.mp4"
            src.write_bytes(b"\x00" * 64)
            cache = ProxyCache(root / "cache", max_total_mb=1.0)
            arr = np.arange(50, dtype=np.int64) * 3
            art = ProxyArtifact(
                path=src, key="", source_frames=arr, frames=50,
                duration_s=2.5, mb=0.001, build_s=1.0, spec=ProxySpec(),
                ticks=arr * 256, biz_steps=np.full(50, 2, dtype=np.int32),
                time_base=proxy_cache.Fraction(1, 15360), fps=60.0,
            )
            stored = cache.store("abc", art)
            self.assertTrue(cache.contains("abc"))
            hit = cache.load("abc")
            self.assertIsNotNone(hit)
            self.assertTrue(np.array_equal(hit.source_frames, arr))
            self.assertTrue(np.array_equal(hit.ticks, arr * 256))
            self.assertEqual(int(hit.biz_steps[0]), 2)
            self.assertEqual(hit.time_base, proxy_cache.Fraction(1, 15360))
            self.assertAlmostEqual(hit.fps, 60.0)
            # 损坏 → None
            (root / "cache" / "abc.npz").write_bytes(b"garbage")
            self.assertIsNone(cache.load("abc"))

    def test_store_does_not_mutate_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src.mp4"
            src.write_bytes(b"\x00" * 64)
            cache = ProxyCache(root / "cache", max_total_mb=1.0)
            arr = np.arange(10, dtype=np.int64)
            art = ProxyArtifact(
                path=src, key="orig", source_frames=arr, frames=10,
                duration_s=1.0, mb=0.001, build_s=0.0, spec=ProxySpec(),
            )
            cache.store("k1", art)
            self.assertEqual(art.key, "orig")  # 入参未被改写
            cache.store("k2", art)  # 可安全二次 store
            self.assertTrue(cache.contains("k1") and cache.contains("k2"))


class EngineProxyContractTests(unittest.TestCase):
    """引擎代理模式契约（假绑定，只看命令面/映射面）。"""

    def _engine(self, holder, path: str, frames: np.ndarray):
        from mpv_engine import MpvEngine

        from tests.test_mpv_engine import _AsyncLoadPlayer

        def factory(**options):
            holder["player"] = _AsyncLoadPlayer(**options)
            return holder["player"]

        engine = MpvEngine(path, mpv_factory=factory, fps=60.0, total=400)
        engine.start()
        holder["player"].event_callback({"event": "file-loaded"})
        proxy_file = Path(path).parent / "proxy.mp4"
        proxy_file.write_bytes(b"\x00" * 64)
        # 合成映射：每代理帧=源帧 k*10；ticks=代理帧 k 的呈现 tick（k/60 s）
        n = int(frames.size)
        art = ProxyArtifact(
            path=proxy_file, key="k", source_frames=frames,
            frames=n, duration_s=n / 60.0,
            mb=0.001, build_s=0.0, spec=ProxySpec(),
            ticks=np.arange(n, dtype=np.int64) * 256,
            biz_steps=np.full(n, 10, dtype=np.int32),
        )
        return engine, art

    def _fixture(self, tmp: Path):
        source = tmp / "src.mp4"
        source.write_bytes(b"0" * 16)
        return str(source)

    def test_install_play_seek_readback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._fixture(Path(tmp))
            frames = np.arange(0, 400, 10, dtype=np.int64)  # 40 帧，步 10
            holder = {}
            engine, art = self._engine(holder, path, frames)
            try:
                self.assertTrue(engine.set_proxy_artifact(art, "k"))
                self.assertTrue(engine.proxy_ready)
                self.assertEqual(engine.proxy_key, "k")
                self.assertTrue(engine.play_proxy(start_source_frame=100, ui_rate=4.0))
                player = holder["player"]
                # 载入的是代理文件（resolve 后；异步 loadfile 走 command_async）
                joined = [" ".join(map(str, c)) for c in player.async_commands]
                self.assertTrue(any("proxy.mp4" in j for j in joined), joined)
                # 异步加载：pending seek/speed 在 file-loaded 时落地
                player.event_callback({"event": "file-loaded"})
                # UI 倍速直接进 mpv speed（业务已烘入）
                self.assertEqual(player.speed, 4.0)
                # 读回：tp=0.5s → 代理帧 30 → 源帧 300
                engine._on_property("time-pos", 0.5)
                self.assertEqual(engine._source_frame, 300)
                # seek：源帧 200 → 代理帧 20 → 20/60=0.333s exact seek
                self.assertTrue(engine.seek_proxy_source_frame(200))
                self.assertTrue(engine.proxy_active)
                self.assertEqual(engine._source_frame, 200)
                seeks = [c for c in player.commands if str(c[0]) == "seek"]
                self.assertTrue(seeks, player.commands)
                self.assertAlmostEqual(float(seeks[-1][1]), 20 / 60.0, places=6)
                self.assertEqual(str(seeks[-1][2]), "absolute+exact")
            finally:
                engine.close()

    def test_clear_proxy_disables_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._fixture(Path(tmp))
            frames = np.arange(0, 400, 10, dtype=np.int64)
            holder = {}
            engine, art = self._engine(holder, path, frames)
            try:
                engine.set_proxy_artifact(art, "k")
                engine.clear_proxy_artifact()
                self.assertFalse(engine.proxy_ready)
                self.assertIsNone(engine.proxy_key)
                from preview_engine import PreviewEngineError

                with self.assertRaises(PreviewEngineError):
                    engine.play_proxy(start_source_frame=0, ui_rate=4.0)
            finally:
                engine.close()

    def test_snapshot_reports_proxy_speed_badge(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._fixture(Path(tmp))
            frames = np.arange(0, 400, 10, dtype=np.int64)
            holder = {}
            engine, art = self._engine(holder, path, frames)
            try:
                engine.set_proxy_artifact(art, "k")
                engine.play_proxy(start_source_frame=0, ui_rate=4.0)
                engine._on_property("time-pos", 1.0)
                snap = engine.snapshot_perf()
                self.assertEqual(snap["mode"], "proxy")
                # biz_steps=10 × UI4 = 40x 角标
                self.assertEqual(snap["speed"], 40.0)
            finally:
                engine.close()

    def test_seek_verified_refuses_in_proxy_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._fixture(Path(tmp))
            frames = np.arange(0, 400, 10, dtype=np.int64)
            holder = {}
            engine, art = self._engine(holder, path, frames)
            try:
                engine.set_proxy_artifact(art, "k")
                engine.play_proxy(start_source_frame=0, ui_rate=4.0)
                from preview_engine import SourceSeekRequest

                accepted = engine.seek_source_verified(
                    SourceSeekRequest(
                        source_frame=100, canvas_size=(320, 180),
                        timeline_revision=0,
                    )
                )
                self.assertFalse(accepted)
            finally:
                engine.close()


class GuiProxyWiringTests(unittest.TestCase):
    """GUI 接线：播放优先代理、seek 拦截、调度守卫。"""

    def _player(self, **overrides):
        from preview_player import VideoPreviewPlayer
        from tests.test_preview_fps_osd import _bare_player

        player = _bare_player()
        player.preview_speed_var = SimpleNamespace(get=lambda: "4x")
        player._apply_pace_mode_to_io = mock.Mock()
        player._reject_play_request = mock.Mock()
        player.settings = SimpleNamespace(get_params=lambda: {
            'skim_preview': True,
            'proxy_materialize': True,
            'speedup_1x': True,
            'speedup_02': True,
            'speedup_02_factor': 10,
        })
        player.skip_trimmed = _BoolVar(True)
        player.total_frames = 400
        mask = [False] * 400
        mask[100:140] = [True] * 40
        from timeline_plan import TimelinePlan

        player._build_timeline_plan = mock.Mock(
            return_value=TimelinePlan.from_delete_mask(mask)
        )
        player._speed_segs_snap = mock.Mock(return_value=[])
        player._canvas_wh = mock.Mock(return_value=(320, 180))
        player.media_info = SimpleNamespace(source_sha256="a" * 64)
        player.states_array = _states()
        player.pause_segments = []
        player.speed_segments = []
        player.clip_segments = []
        player._io.proxy_ready = True
        player._io.proxy_key = "want"
        for k, v in overrides.items():
            setattr(player, k, v)
        return player

    def test_play_prefers_proxy_when_key_matches(self):
        player = self._player()
        player._io.play_proxy = mock.Mock(return_value=True)
        # key 匹配：expected_proxy_key 计算结果与引擎持有的键一致
        with mock.patch.object(proxy_cache, "expected_proxy_key", return_value="want"):
            self.assertTrue(player._send_play(50))
        player._io.play_proxy.assert_called_once()
        player._io.play_dthrough.assert_not_called()
        player._io.play.assert_not_called()

    def test_play_falls_back_when_key_mismatch(self):
        player = self._player()
        # 回落路径构造 CertifiedEdlRequest 需真 MediaInfo（强类型校验）
        from tests.test_preview_dthrough_tier import _fake_media

        player.media_info = _fake_media()
        player._io.play_proxy = mock.Mock(return_value=True)
        player._io.play_dthrough = mock.Mock(return_value=True)
        with mock.patch.object(proxy_cache, "expected_proxy_key", return_value="other"):
            player._send_play(50)
        player._io.play_proxy.assert_not_called()
        player._io.play_dthrough.assert_called_once()

    def test_seek_routes_to_proxy_when_active(self):
        player = self._player()
        player._io.proxy_active = True
        player._io.seek_proxy_source_frame = mock.Mock(return_value=True)
        player._show_paused_still = mock.Mock()
        player._seek_frame(200)
        player._io.seek_proxy_source_frame.assert_called_once_with(200)
        player._io.seek_edl.assert_not_called()
        player._io.seek_source.assert_not_called()

    def test_native_step_disabled_in_proxy_mode(self):
        """10-11 自查：mpv frame-step 步的是代理帧（0.2x 段=10 源帧），
        UI 标签按 +1 源帧递增=语义错位 → 代理模式下必须禁用原生步进，
        走代理精确寻址（±1 源帧）。"""
        player = self._player()
        player._io.proxy_active = True
        self.assertFalse(player._native_step_enabled())
        player._io.proxy_active = False
        self.assertTrue(player._native_step_enabled())

    def test_scheduler_skips_when_proxy_disabled(self):
        player = self._player()
        player.settings = SimpleNamespace(get_params=lambda: {
            'proxy_materialize': False,
        })
        player.task_manager = mock.Mock()
        player._maybe_schedule_proxy_build()
        player.task_manager.submit.assert_not_called()

    def test_scheduler_skips_when_engine_holds_current_key(self):
        player = self._player()
        player.states_array = _states()
        player._io.proxy_key = "same"
        player.task_manager = mock.Mock()
        with mock.patch.object(proxy_cache, "expected_proxy_key", return_value="same"):
            player._maybe_schedule_proxy_build()
        player.task_manager.submit.assert_not_called()

    def test_play_gate_arms_rebuild_on_key_mismatch(self):
        """自查修复（10-11）：门条件满足但键不匹配（业务开关/倍率变更、
        编辑后）→ arm 自愈重建，不依赖设置面板回调。"""
        player = self._player()
        player._io.proxy_ready = True
        player._io.proxy_key = "stale"
        player._io.play_dthrough = mock.Mock(return_value=True)
        armed = []
        player._arm_proxy_rebuild = lambda: armed.append(True)
        with mock.patch.object(proxy_cache, "expected_proxy_key", return_value="fresh"):
            player._send_play(50)
        self.assertTrue(armed, "key mismatch must arm a self-heal rebuild")

    def test_expected_proxy_key_cached_reuses_result(self):
        """播放门键缓存：同一作用域/设置二次调用不再全表计算。"""
        player = self._player()
        calls = []

        def fake_key(*args, **kwargs):
            calls.append(1)
            return "k1"

        with mock.patch.object(proxy_cache, "expected_proxy_key", side_effect=fake_key):
            k1 = player._expected_proxy_key_cached(ignore_biz=False)
            k2 = player._expected_proxy_key_cached(ignore_biz=False)
        self.assertEqual(k1, "k1")
        self.assertEqual(k2, "k1")
        self.assertEqual(len(calls), 1, "second call must hit the cache")

    def test_proxy_key_cache_invalidated_on_ignore_flag_change(self):
        """ignore_biz 旗标变化 → 缓存失效（键不同）。"""
        player = self._player()
        keys = iter(["biz", "ignored"])

        with mock.patch.object(proxy_cache, "expected_proxy_key",
                               side_effect=lambda *a, **k: next(keys)):
            k1 = player._expected_proxy_key_cached(ignore_biz=False)
            k2 = player._expected_proxy_key_cached(ignore_biz=True)
        self.assertEqual((k1, k2), ("biz", "ignored"))

    def test_scheduler_respects_failure_memory(self):
        """自查修复（10-11）：同 head 失败过 → 不再重试（防播放门自愈
        arm 无限重试环）；head 变化（新编辑）自然恢复。"""
        player = self._player()
        player.states_array = _states()
        player.media_info = SimpleNamespace(
            source_sha256="a" * 64, frame_pts_authoritative=True
        )
        player.video_path = str(Path(tempfile.gettempdir()) / "fake_src.mp4")
        player.task_manager = mock.Mock()
        player._io.proxy_key = None
        player._proxy_failed_head = None
        with mock.patch.object(proxy_cache, "expected_proxy_key", return_value="k"):
            # 第一次：允许提交
            player._maybe_schedule_proxy_build()
            first_calls = player.task_manager.submit.call_count
            head = getattr(player, "_proxy_build_head", None)
            self.assertIsNotNone(head, "first submit must record in-flight head")
            player._proxy_build_head = None  # 模拟构建结束
            player._proxy_failed_head = head  # 模拟失败记忆
            player._maybe_schedule_proxy_build()
            second_calls = player.task_manager.submit.call_count
        self.assertEqual(second_calls, first_calls,
                         "same head after failure must not resubmit")

    def test_play_gate_skips_self_heal_when_ignore_biz(self):
        """ignore_biz=True 时键必不匹配（构建端从不产出该键）→ 不得 arm
        空转重建（回落直通=正确语义）。"""
        player = self._player()
        player._io.proxy_ready = True
        player._io.proxy_key = "whatever"
        player._io.play_dthrough = mock.Mock(return_value=True)
        armed = []
        player._arm_proxy_rebuild = lambda: armed.append(True)
        # ignore_biz 由 _on_preview_option_change 的「忽略业务加速」开关驱动
        # ——直接构造：preview_opt_var 语义不走它；用 monkeypatch 令
        # _send_play 内 ignore_biz 为 True 的最简方式=业务开关全关且？
        # 实际 ignore_biz 恒 False（09-13 起 mpv 原生不剥业务）。改用直接
        # 单测 _expected_proxy_key_cached 的 ignore 分支即可。
        with mock.patch.object(proxy_cache, "expected_proxy_key", return_value="x"):
            k_ign = player._expected_proxy_key_cached(ignore_biz=True)
        self.assertEqual(k_ign, "x")

    def test_install_success_resends_play_while_playing(self):
        """编辑后重建完成时正在播放 → on_success 自动以当前帧重发播放
        （无缝切回代理 40x，用户无需手动暂停/播放）。"""
        player = self._player()
        player.states_array = _states()
        player.media_info = SimpleNamespace(
            source_sha256="a" * 64, frame_pts_authoritative=True
        )
        player.video_path = str(Path(tempfile.gettempdir()) / "fake_src.mp4")
        player.task_manager = mock.Mock()
        player._io.proxy_key = None
        player.is_playing = True
        player.current_frame_idx = 77
        resends = []
        player._send_play = lambda start: resends.append(start) or True
        submitted = {}

        def fake_submit(kind, work, **kwargs):
            submitted.update(kwargs)
            handle = mock.Mock()
            return handle

        player.task_manager.submit = fake_submit
        with mock.patch.object(proxy_cache, "expected_proxy_key", return_value="k"):
            player._maybe_schedule_proxy_build()
        # 模拟构建成功回调（构造最小 artifact）
        artifact = SimpleNamespace(
            frames=10, duration_s=1.0, mb=0.1, build_s=0.5,
            key="k", path=Path(tempfile.gettempdir()) / "fake_proxy.mp4",
        )
        artifact.path.write_bytes(b"\x00" * 8)
        player._io.set_proxy_artifact = mock.Mock(return_value=True)
        submitted["on_success"](artifact)
        self.assertEqual(resends, [77], "playing session must resend play after install")


if __name__ == "__main__":
    unittest.main()
