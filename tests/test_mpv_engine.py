from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import time
import unittest
from fractions import Fraction

from certified_edl import _fraction_seconds
from mpv_engine import MpvEngine
from preview_engine import CertifiedEdlRequest, PreviewEngineError, SourceSeekRequest
from timeline_plan import TimelinePlan


class _FakePlayer:
    def __init__(self, **options):
        self.options = options
        self.commands = []
        self.event_callback = None
        self.property_callbacks = {}
        self.path = None
        self.terminated = 0
        self._pause = True
        self.speed = 1.0

    def register_event_callback(self, callback):
        self.event_callback = callback

    def unregister_event_callback(self, callback):
        if self.event_callback is callback:
            self.event_callback = None

    def observe_property(self, name, callback):
        self.property_callbacks[name] = callback

    def unobserve_property(self, name, callback):
        self.property_callbacks.pop(name, None)

    def play(self, path):
        self.commands.append(("play", path))

    def command(self, *args):
        self.commands.append(tuple(args))

    @property
    def pause(self):
        return self._pause

    @pause.setter
    def pause(self, value):
        self._pause = bool(value)

    def terminate(self):
        self.terminated += 1


class _AsyncLoadPlayer(_FakePlayer):
    """Binding subset exposing fire-and-forget async commands."""

    def __init__(self, **options):
        super().__init__(**options)
        self.async_commands = []

    def command_async(self, *args, **_kwargs):
        self.async_commands.append(tuple(args))


class _BlockingTerminatePlayer(_FakePlayer):
    def __init__(self, **options):
        super().__init__(**options)
        self.allow_terminate = threading.Event()

    def terminate(self):
        self.terminated += 1
        self.allow_terminate.wait(1.0)


class MpvEngineContractTests(unittest.TestCase):
    def test_source_seek_uses_absolute_exact_and_close_is_idempotent(self):
        holder = {}

        def factory(**options):
            holder["player"] = _FakePlayer(**options)
            return holder["player"]

        engine = MpvEngine(
            "source.mp4",
            wid=123,
            mpv_factory=factory,
            fps=60.0,
            total=4,
        )
        engine.start()
        with self.assertRaises(PreviewEngineError) as blocked:
            engine.seek_source(SourceSeekRequest(1, (320, 180), 0, exact=True))
        self.assertEqual(blocked.exception.code, "CERTIFICATION_REQUIRED")
        self.assertTrue(engine.close(timeout=1.0))
        self.assertTrue(engine.close(timeout=1.0))
        self.assertEqual(holder["player"].terminated, 1)

    def test_source_zero_seek_is_explicit_after_source_reload(self):
        # A fake timeline is unnecessary for frame zero: non-exact zero seek is
        # the only pre-certification operation allowed by the engine.
        holder = {}

        def factory(**options):
            holder["player"] = _FakePlayer(**options)
            return holder["player"]

        engine = MpvEngine("source.mp4", mpv_factory=factory)
        engine.start()
        # libmpv loads asynchronously; deliver the readiness event that the
        # Tk polling loop would normally consume before asserting the command.
        holder["player"].event_callback({"event": "file-loaded"})
        self.assertTrue(
            engine.seek_source(SourceSeekRequest(0, (1, 1), 0, exact=False))
        )
        self.assertIn(("seek", "0", "absolute"), holder["player"].commands)
        self.assertEqual(holder["player"].options["wid"], None) if "wid" in holder["player"].options else None
        engine.close()

    def test_async_loadfile_replaces_synchronous_play(self):
        # A real EDL with thousands of segments makes a synchronous loadfile
        # block the owner thread for the whole demuxer open; the engine must
        # issue the load fire-and-forget when the binding supports it.
        holder = {}

        def factory(**options):
            holder["player"] = _AsyncLoadPlayer(**options)
            return holder["player"]

        engine = MpvEngine("source.mp4", mpv_factory=factory, total=2)
        engine.start()
        player = holder["player"]
        self.assertEqual(player.async_commands, [("loadfile", engine.path, "replace")])
        # The player is held paused before the load is issued so nothing is
        # presented before the pending seek lands.
        self.assertTrue(player.pause)
        self.assertFalse(any(command[0] == "play" for command in player.commands))
        engine.close()

    def test_timeline_passthrough_matches_full_validation_build(self):
        # The preview engine's caller-owned timeline route must produce the
        # exact artifact the fail-closed exporter route produces.
        from certified_edl import build_certified_edl
        from pts_timeline import CertifiedPtsTimeline
        from tests.test_preview_engine import _certified_media

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = _certified_media(
                root,
                [{"n": i, "pts": i * 40, "duration": 40} for i in range(8)],
            )
            plan = TimelinePlan.from_deleted_ranges(8, [(2, 4)])
            request = CertifiedEdlRequest(media, plan, 0, 0)
            full = build_certified_edl(request, root / "edl")
            timeline = CertifiedPtsTimeline.from_media_info(media)
            fast = build_certified_edl(request, root / "edl", timeline=timeline)
            self.assertEqual(full.content_sha256, fast.content_sha256)
            self.assertEqual(full.segments, fast.segments)
            self.assertEqual(full.path, fast.path)

    def test_play_edl_reuses_cached_artifact_for_same_plan(self):
        import mpv_engine as mpv_engine_module
        from tests.test_preview_engine import _certified_media

        holder = {}

        def factory(**options):
            holder["player"] = _FakePlayer(**options)
            return holder["player"]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = _certified_media(
                root,
                [{"n": i, "pts": i * 40, "duration": 40} for i in range(8)],
            )
            engine = MpvEngine(
                str(media.source_path), total=8, mpv_factory=factory, edl_dir=root / "edl"
            )
            engine.bind_media_info(media)
            engine.start()
            plan = TimelinePlan.from_deleted_ranges(8, [(2, 4)])
            request = CertifiedEdlRequest(media, plan, 3, 1)
            calls: list[bool] = []
            original = mpv_engine_module.build_certified_edl

            def counting(request_, dir_, **kwargs):
                calls.append(kwargs.get("timeline") is not None)
                return original(request_, dir_, **kwargs)

            mpv_engine_module.build_certified_edl = counting
            try:
                self.assertTrue(engine.play_edl(request, start_frame=0))
                self.assertTrue(engine.play_edl(request, start_frame=0))
            finally:
                mpv_engine_module.build_certified_edl = original
            # One build for both toggles, and it received the engine's
            # already-bound timeline instead of re-deriving it.
            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0])
            engine.close()

    def test_edl_request_rejects_foreign_certified_source_before_publish(self):
        from tests.test_preview_engine import _certified_media

        holder = {}

        def factory(**options):
            holder["player"] = _FakePlayer(**options)
            return holder["player"]

        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            media_a = _certified_media(
                Path(first),
                [{"n": i, "pts": i * 40, "duration": 40} for i in range(2)],
            )
            media_b = _certified_media(
                Path(second),
                [{"n": i, "pts": i * 40, "duration": 40} for i in range(2)],
            )
            engine = MpvEngine(str(media_a.source_path), mpv_factory=factory, total=2)
            request = CertifiedEdlRequest(
                media_b,
                TimelinePlan.from_deleted_ranges(2, []),
                0,
                0,
            )
            with self.assertRaises(PreviewEngineError) as raised:
                engine.play_edl(request, start_frame=0)
            self.assertEqual(raised.exception.code, "SOURCE_MISMATCH")
            self.assertFalse((Path(engine._edl_dir)).exists())
            engine.close()

    def test_file_loaded_flushes_pending_exact_seek_and_ignores_stale_generation(self):
        from tests.test_preview_engine import _certified_media

        holder = {}

        def factory(**options):
            holder["player"] = _FakePlayer(**options)
            return holder["player"]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = _certified_media(
                root,
                [{"n": i, "pts": i * 40, "duration": 40} for i in range(8)],
            )
            engine = MpvEngine(
                str(media.source_path),
                total=8,
                mpv_factory=factory,
                edl_dir=root / "edl",
            )
            engine.bind_media_info(media)
            engine.start()
            player = holder["player"]
            source_generation = engine.snapshot_perf()["generation"]

            plan = TimelinePlan.from_deleted_ranges(8, [(2, 4)])
            self.assertTrue(
                engine.play_edl(
                    CertifiedEdlRequest(media, plan, 7, 3),
                    start_frame=5,
                )
            )
            edl_generation = engine.snapshot_perf()["generation"]
            self.assertGreater(edl_generation, source_generation)

            # A real binding has no Python generation field; an old path
            # property still has to prevent its file-loaded event from
            # unlocking the replacement EDL.
            engine._current_path = str(Path(engine.path).resolve())
            player.event_callback({"event": "file-loaded"})
            engine.poll_events()
            self.assertFalse(any(command[0] == "seek" for command in player.commands))

            # The old load completion must not release the EDL request.
            player.event_callback({"event": "file-loaded", "generation": source_generation})
            engine.poll_events()
            self.assertFalse(any(command[0] == "seek" for command in player.commands))

            engine._current_path = str(engine._edl.path)
            player.event_callback(
                {
                    "event": "file-loaded",
                    "generation": edl_generation,
                    "project_generation": 7,
                    "timeline_revision": 3,
                }
            )
            engine.poll_events()
            seek_commands = [command for command in player.commands if command[0] == "seek"]
            # Paused exact seeks carry the half-tick boundary nudge.
            self.assertEqual(
                seek_commands[-1],
                ("seek", _fraction_seconds(Fraction(12, 100) + Fraction(5, 120)),
                 "absolute+exact"),
            )
            self.assertEqual(engine.snapshot_perf()["mode"], "edl")

            self.assertTrue(
                engine.seek_source(SourceSeekRequest(6, (320, 180), 4, exact=True))
            )
            source_reload_generation = engine.snapshot_perf()["generation"]
            player.event_callback(
                {"event": "file-loaded", "generation": edl_generation}
            )
            engine.poll_events()
            self.assertEqual(len([command for command in player.commands if command[0] == "seek"]), 1)
            engine._current_path = str(Path(engine.path).resolve())
            player.event_callback(
                {
                    "event": "file-loaded",
                    "generation": source_reload_generation,
                    "project_generation": 7,
                    "timeline_revision": 4,
                }
            )
            engine.poll_events()
            seek_commands = [command for command in player.commands if command[0] == "seek"]
            # Paused exact seeks carry the half-tick boundary nudge.
            self.assertEqual(
                seek_commands[-1],
                ("seek", _fraction_seconds(Fraction(24, 100) + Fraction(5, 120)),
                 "absolute+exact"),
            )
            self.assertEqual(engine.snapshot_perf()["mode"], "source")
            self.assertTrue(engine.close())

    def test_seek_edl_requires_built_artifact(self):
        holder = {}

        def factory(**options):
            holder["player"] = _FakePlayer(**options)
            return holder["player"]

        engine = MpvEngine("source.mp4", mpv_factory=factory, total=2)
        engine.start()
        with self.assertRaises(PreviewEngineError) as raised:
            engine.seek_edl(3)
        self.assertEqual(raised.exception.code, "EDL_NOT_READY")
        engine.close()

    def test_seek_edl_stays_in_edl_mode_and_snaps_deleted_frames(self):
        from tests.test_preview_engine import _certified_media

        holder = {}

        def factory(**options):
            holder["player"] = _FakePlayer(**options)
            return holder["player"]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = _certified_media(
                root,
                [{"n": i, "pts": i * 40, "duration": 40} for i in range(8)],
            )
            engine = MpvEngine(
                str(media.source_path),
                total=8,
                mpv_factory=factory,
                edl_dir=root / "edl",
            )
            engine.bind_media_info(media)
            engine.start()
            player = holder["player"]

            plan = TimelinePlan.from_deleted_ranges(8, [(2, 4)])
            self.assertTrue(
                engine.play_edl(CertifiedEdlRequest(media, plan, 0, 0), start_frame=0)
            )
            edl_generation = engine.snapshot_perf()["generation"]
            engine._current_path = str(engine._edl.path)
            player.event_callback({"event": "file-loaded", "generation": edl_generation})
            engine.poll_events()
            self.assertEqual(engine.snapshot_perf()["mode"], "edl")

            # Kept frame: seek lands on its own virtual time, no reload happens.
            loads_before = len([command for command in player.commands if command[0] == "play"])
            self.assertTrue(engine.seek_edl(5))
            seek_commands = [command for command in player.commands if command[0] == "seek"]
            # Playing (unpaused) exact seeks carry no boundary nudge.
            self.assertEqual(seek_commands[-1], ("seek", "0.12", "absolute+exact"))
            self.assertEqual(engine.snapshot_perf()["mode"], "edl")
            self.assertEqual(
                len([command for command in player.commands if command[0] == "play"]),
                loads_before,
            )

            # Deleted frame snaps to the containing span end (frame 4), the
            # same mapping playback uses; _source_frame follows the artifact.
            self.assertTrue(engine.seek_edl(3))
            seek_commands = [command for command in player.commands if command[0] == "seek"]
            self.assertEqual(seek_commands[-1], ("seek", "0.08", "absolute+exact"))
            self.assertEqual(engine._source_frame, 4)
            self.assertTrue(engine.close())

    def test_scope_mismatch_event_is_dropped(self):
        holder = {}

        def factory(**options):
            holder["player"] = _FakePlayer(**options)
            return holder["player"]

        engine = MpvEngine("source.mp4", mpv_factory=factory, total=2)
        engine.start()
        player = holder["player"]
        engine._project_generation = 4
        engine._timeline_revision = 9
        player.event_callback(
            {
                "event": "file-loaded",
                "generation": engine.snapshot_perf()["generation"],
                "project_generation": 3,
                "timeline_revision": 9,
            }
        )
        engine.poll_events()
        self.assertFalse(engine._loaded_ready)
        player.event_callback(
            {
                "event": "file-loaded",
                "generation": engine.snapshot_perf()["generation"],
                "project_generation": 4,
                "timeline_revision": 9,
            }
        )
        engine.poll_events()
        self.assertTrue(engine._loaded_ready)
        engine.close()

    def test_file_loaded_uses_native_path_when_observed_property_lags(self):
        holder = {}

        def factory(**options):
            holder["player"] = _FakePlayer(**options)
            return holder["player"]

        engine = MpvEngine("source.mp4", mpv_factory=factory, total=2)
        engine.start()
        player = holder["player"]
        player.path = engine.snapshot_perf().get("path") or engine.path
        engine._current_path = str(Path("old.mp4").resolve())
        self.assertTrue(
            engine.seek_source(SourceSeekRequest(0, (1, 1), 0, exact=False))
        )
        player.event_callback({"event": "file-loaded"})
        engine.poll_events()
        self.assertIn(("seek", "0", "absolute"), player.commands)
        engine.close()

    def test_bind_media_info_derives_outside_lock(self):
        """绑定认证媒体时，重派生不得在引擎锁内做（09-24 review T0）。

        原实现在 `with self._lock` 内建 424k 行时间轴（实测 ~2.4s），渲染
        循环（`snapshot_perf` 同锁）整段卡住 = 播放中认证完成时一次 2s+
        冻结。现派生移到锁外；本用例用「派生期间另一线程能否拿到锁」证明
        锁未被长持有。
        """
        from tests.test_preview_engine import _certified_media

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = _certified_media(
                root,
                [{"n": i, "pts": i * 40, "duration": 40} for i in range(8)],
            )
            engine = MpvEngine(str(media.source_path), total=8)
            acquired = []

            import threading as _threading
            real_prepare = engine._prepare_timeline_derivations

            def slow_prepare(candidate_timeline):
                # 模拟「重派生很慢」：期间另一线程必须能拿到引擎锁
                def probe():
                    got = engine._lock.acquire(timeout=0.5)
                    acquired.append(bool(got))
                    if got:
                        engine._lock.release()
                t = _threading.Thread(target=probe)
                t.start()
                time.sleep(0.15)   # 给探针线程时间尝试取锁
                t.join(1.0)
                return real_prepare(candidate_timeline)

            engine._prepare_timeline_derivations = slow_prepare
            engine.bind_media_info(media)
            self.assertEqual(acquired, [True],
                             "派生期间引擎锁必须可用（不得锁内做重活）")
            self.assertIsNotNone(engine._timeline)
            engine.close()

    def test_source_stat_change_rejects_cached_exact_pts_seek(self):
        from tests.test_preview_engine import _certified_media

        holder = {}

        def factory(**options):
            holder["player"] = _FakePlayer(**options)
            return holder["player"]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = _certified_media(
                root,
                [{"n": i, "pts": i * 40, "duration": 40} for i in range(4)],
            )
            engine = MpvEngine(str(media.source_path), mpv_factory=factory, total=4)
            engine.bind_media_info(media)
            media.source_path.write_bytes(b"changed-source")
            with self.assertRaises(PreviewEngineError) as raised:
                engine.seek_source(SourceSeekRequest(1, (1, 1), 0, exact=True))
            self.assertEqual(raised.exception.code, "SOURCE_CHANGED_AFTER_PROBE")
            engine.close()

    def test_stop_cancels_pending_resume_after_file_loaded(self):
        holder = {}

        def factory(**options):
            holder["player"] = _FakePlayer(**options)
            return holder["player"]

        engine = MpvEngine("source.mp4", mpv_factory=factory, total=2)
        engine.start()
        player = holder["player"]
        # A zero-frame source play queues work until file-loaded.
        from preview_engine import PreviewPlayRequest
        from timeline_plan import TimelinePlan

        request = PreviewPlayRequest(
            start_frame=0,
            playback_rate=1.0,
            preview_step=1,
            speed_multiplier=1.0,
            skip_trimmed=False,
            speedup_1x=False,
            speedup_02=False,
            speedup_02_factor=1,
            timeline_plan=TimelinePlan.from_deleted_ranges(2, []),
            speed_segments=(),
            canvas_size=(1, 1),
            project_generation=1,
            timeline_revision=1,
        )
        self.assertTrue(engine.play(request))
        self.assertTrue(engine.stop())
        player.event_callback({"event": "file-loaded", "generation": engine.snapshot_perf()["generation"]})
        engine.poll_events()
        self.assertFalse(engine.is_playback_active())
        self.assertFalse(any(command[0] == "seek" for command in player.commands))
        engine.close()

    def test_business_frame_speed_policy_switches_on_segment_boundary(self):
        # 业务变速段（上游 VideoIOThread.speed_step 语义）：1x 段且
        # speedup_1x → 2；出段回 1。time-pos 驱动，source 模式直接映射。
        from preview_engine import PreviewPlayRequest
        from pts_timeline import CertifiedPtsTimeline
        from tests.test_preview_engine import _certified_media

        holder = {}

        def factory(**options):
            holder["player"] = _AsyncLoadPlayer(**options)
            return holder["player"]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = _certified_media(
                root,
                [{"n": i, "pts": i * 40, "duration": 40} for i in range(100)],
            )
            engine = MpvEngine(
                str(media.source_path), mpv_factory=factory, fps=25.0, total=100
            )
            engine.bind_media_info(media)
            engine.start()
            request = PreviewPlayRequest(
                start_frame=0,
                playback_rate=1.0,
                preview_step=1,
                speed_multiplier=1.0,
                skip_trimmed=False,
                speedup_1x=True,
                speedup_02=True,
                speedup_02_factor=5,
                timeline_plan=TimelinePlan.from_deleted_ranges(100, []),
                # 30..59 = 1x 段（→2）；70..89 = 0.2x 段（→max(2,5)=5）；其余普通（→1）
                speed_segments=((30, 59, 2), (70, 89, 4)),
                canvas_size=(1, 1),
                project_generation=0,
                timeline_revision=0,
            )
            self.assertTrue(engine.play(request))
            player = holder["player"]
            player.event_callback({"event": "file-loaded", "generation": engine.snapshot_perf()["generation"]})
            engine.poll_events()
            self.assertTrue(engine.is_playback_active())
            # time-pos 回调：跨进 1x 段 → speed=2（25fps → 1 帧=40ms）
            player.property_callbacks["time-pos"]("time-pos", 30 * 0.040)
            self.assertEqual(player.speed, 2.0)
            # 仍在段内不重复写（speed 只变化一次）
            calls_before = len(player.commands)
            player.property_callbacks["time-pos"]("time-pos", 45 * 0.040)
            self.assertEqual(len(player.commands), calls_before)
            # 跨进 0.2x 段 → speed=max(2, factor)=5。源模式不封顶：连续
            # 文件无拼接边界风暴（T0：连续 10x rt9.978 干净、引擎级源
            # 模式 OSD≥57.5），cap parity 只治 EDL Tier1（见
            # test_edl_tier1_caps_business_speed）。
            player.property_callbacks["time-pos"]("time-pos", 70 * 0.040)
            self.assertEqual(player.speed, 5.0)
            # 出段回普通 → speed=1
            player.property_callbacks["time-pos"]("time-pos", 95 * 0.040)
            self.assertEqual(player.speed, 1.0)
            engine.close()

    def test_edl_tier1_caps_business_speed(self):
        # Tier1（活 EDL）：业务 factor 5 × UI 1x → min(5, cap=3)=3。
        # EDL 拼接边界风暴只在拼接流上发生（F1/F2 T0），cap 作用乘积。
        from preview_engine import PreviewPlayRequest
        from tests.test_preview_engine import _certified_media

        holder = {}

        def factory(**options):
            holder["player"] = _AsyncLoadPlayer(**options)
            return holder["player"]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = _certified_media(
                root,
                [{"n": i, "pts": i * 40, "duration": 40} for i in range(100)],
            )
            engine = MpvEngine(
                str(media.source_path), mpv_factory=factory, fps=25.0,
                total=100, edl_dir=root / "edl",
            )
            engine.bind_media_info(media)
            engine.start()
            plan = TimelinePlan.from_deleted_ranges(100, [(20, 30)])
            request = PreviewPlayRequest(
                start_frame=0,
                playback_rate=1.0,
                preview_step=1,
                speed_multiplier=1.0,
                skip_trimmed=True,
                speedup_1x=False,
                speedup_02=True,
                speedup_02_factor=5,
                timeline_plan=plan,
                speed_segments=((0, 99, 4),),
                canvas_size=(1, 1),
                project_generation=0,
                timeline_revision=0,
                preview_step_cap=3,
            )
            self.assertTrue(engine.play(request))
            player = holder["player"]
            player.event_callback({
                "event": "file-loaded",
                "generation": engine.snapshot_perf()["generation"],
            })
            engine.poll_events()
            self.assertEqual(engine.snapshot_perf()["mode"], "edl")
            # 虚拟 tp → EDL 映射折回源帧 → 0.2X 段（全覆盖）→ 5 封顶 3
            player.property_callbacks["time-pos"]("time-pos", 0.5)
            self.assertEqual(player.speed, 3.0)
            engine.close()

    def test_business_speed_disabled_when_no_biz_flags(self):
        # 无业务段/开关：基准速率 1 次钉死，time-pos 不再反复写 speed。
        from preview_engine import PreviewPlayRequest

        holder = {}

        def factory(**options):
            holder["player"] = _AsyncLoadPlayer(**options)
            return holder["player"]

        engine = MpvEngine("source.mp4", mpv_factory=factory, fps=30.0, total=100)
        engine.start()
        request = PreviewPlayRequest(
            start_frame=0,
            playback_rate=2.0,
            preview_step=1,
            speed_multiplier=1.0,
            skip_trimmed=False,
            speedup_1x=False,
            speedup_02=False,
            speedup_02_factor=1,
            timeline_plan=TimelinePlan.from_deleted_ranges(100, []),
            speed_segments=(),
            canvas_size=(1, 1),
            project_generation=0,
            timeline_revision=0,
        )
        self.assertTrue(engine.play(request))
        player = holder["player"]
        player.event_callback({"event": "file-loaded", "generation": engine.snapshot_perf()["generation"]})
        engine.poll_events()
        self.assertEqual(player.speed, 2.0)
        calls_before = len(player.commands)
        player.property_callbacks["time-pos"]("time-pos", 50 / 30.0)
        self.assertEqual(len(player.commands), calls_before)
        engine.close()

    def test_step_frame_issues_native_commands(self):
        # 暂停步进直发 mpv 原生命令（探针D T0：frame-step/frame-back-step
        # 位级精确 0.3-17.2ms；input-command 元命令在此构建不可用）。
        holder = {}

        def factory(**options):
            holder["player"] = _FakePlayer(**options)
            return holder["player"]

        engine = MpvEngine("source.mp4", mpv_factory=factory, fps=30.0, total=100)
        engine.start()
        player = holder["player"]
        self.assertTrue(engine.step_frame(+1))
        self.assertTrue(engine.step_frame(-1))
        self.assertTrue(engine.step_frame(0))  # 非负均按前进
        self.assertIn(("frame-step",), player.commands)
        self.assertIn(("frame-back-step",), player.commands)
        self.assertEqual(engine.snapshot_perf()["step_count"], 3)
        engine.close()

    def test_step_frame_pauses_playing_player_first(self):
        # 上游语义「暂停才步进」：播放中调用先落暂停再步进。
        holder = {}

        def factory(**options):
            holder["player"] = _FakePlayer(**options)
            return holder["player"]

        engine = MpvEngine("source.mp4", mpv_factory=factory, fps=30.0, total=100)
        engine.start()
        player = holder["player"]
        player._pause = False
        engine.step_frame(+1)
        self.assertTrue(player._pause)
        self.assertIn(("frame-step",), player.commands)
        engine.close()

    def test_step_frame_rejects_after_close(self):
        holder = {}

        def factory(**options):
            holder["player"] = _FakePlayer(**options)
            return holder["player"]

        engine = MpvEngine("source.mp4", mpv_factory=factory, fps=30.0, total=100)
        engine.start()
        engine.close()
        with self.assertRaises(PreviewEngineError) as ctx:
            engine.step_frame(+1)
        self.assertEqual(ctx.exception.code, "ENGINE_CLOSED")

    # ---- 选项2：闭环验证定位 seek_source_verified ----
    # 证据链（T0）：无修正 seek 偏差∈[0,2]帧零过冲（probe_seek_sweep 2341点）；
    # playback-restart 后 time-pos 报实际呈现帧（探针E/Tier2）；frame-step
    # 位级精确（探针D）。瞬态期（restart 前）tp 报目标值——相位门必须挡住。

    def _ready_engine(self, temporary):
        """绑定 25fps 认证表（帧i↔i*40ms）并加载就绪的引擎。"""
        from tests.test_preview_engine import _certified_media

        holder = {}

        def factory(**options):
            holder["player"] = _AsyncLoadPlayer(**options)
            return holder["player"]

        root = Path(temporary)
        media = _certified_media(
            root,
            [{"n": i, "pts": i * 40, "duration": 40} for i in range(100)],
        )
        engine = MpvEngine(
            str(media.source_path), mpv_factory=factory, fps=25.0, total=100
        )
        engine.bind_media_info(media)
        engine.start()
        player = holder["player"]
        player.event_callback({
            "event": "file-loaded",
            "generation": engine.snapshot_perf()["generation"],
        })
        engine.poll_events()
        return engine, player

    @staticmethod
    def _seek_req(frame: int) -> SourceSeekRequest:
        return SourceSeekRequest(
            source_frame=frame, canvas_size=(1, 1), timeline_revision=0,
            exact=True, latest_only=True,
        )

    @staticmethod
    def _verified_events(engine):
        return [e for e in engine.poll_events()
                if e.get("event") == "seek-verified"]

    def test_verified_seek_exact_landing_succeeds(self):
        with tempfile.TemporaryDirectory() as temporary:
            engine, player = self._ready_engine(temporary)
            self.assertTrue(engine.seek_source_verified(self._seek_req(10)))
            # 无修正 seek：0.4 而非旧路径的 +5/120（0.44166…）
            seeks = [c for c in player.commands if c and c[0] == "seek"]
            self.assertEqual(len(seeks), 1)
            self.assertAlmostEqual(float(seeks[0][1]), 0.4, places=9)
            self.assertEqual(seeks[0][2], "absolute+exact")
            player.event_callback({"event": "playback-restart"})
            player.property_callbacks["time-pos"]("time-pos", 0.4)
            events = self._verified_events(engine)
            self.assertEqual(len(events), 1)
            self.assertTrue(events[0]["ok"])
            self.assertEqual(events[0]["reason"], "exact")
            self.assertEqual(events[0]["frame"], 10)
            self.assertEqual(events[0]["steps"], 0)
            self.assertEqual(engine.snapshot_perf()["verify_ok"], 1)
            engine.close()

    def test_verified_seek_gated_until_restart(self):
        # 瞬态防护：playback-restart 之前的 tp 观察（可能仍是目标值假象）
        # 不得触发评估；restart 到达后同值观察立即落账。
        with tempfile.TemporaryDirectory() as temporary:
            engine, player = self._ready_engine(temporary)
            engine.seek_source_verified(self._seek_req(10))
            player.property_callbacks["time-pos"]("time-pos", 0.4)
            self.assertEqual(self._verified_events(engine), [])
            player.event_callback({"event": "playback-restart"})
            events = self._verified_events(engine)
            self.assertEqual(len(events), 1)
            self.assertTrue(events[0]["ok"])
            engine.close()

    def test_verified_seek_corrects_stale_landing(self):
        with tempfile.TemporaryDirectory() as temporary:
            engine, player = self._ready_engine(temporary)
            engine.seek_source_verified(self._seek_req(10))
            player.event_callback({"event": "playback-restart"})
            player.property_callbacks["time-pos"]("time-pos", 0.36)  # 停早在帧9
            steps = [c for c in player.commands if c == ("frame-step",)]
            self.assertEqual(len(steps), 1)  # 只停早一帧就只补一步
            player.property_callbacks["time-pos"]("time-pos", 0.4)
            events = self._verified_events(engine)
            self.assertEqual(len(events), 1)
            self.assertTrue(events[0]["ok"])
            self.assertEqual(events[0]["reason"], "corrected")
            self.assertEqual(events[0]["steps"], 1)
            self.assertEqual(engine.snapshot_perf()["verify_steps"], 1)
            engine.close()

    def test_verified_seek_rejects_overshoot(self):
        # 全片扫描零过冲（T0）；出现过冲=账本外行为，不猜、直接失败走 C4
        with tempfile.TemporaryDirectory() as temporary:
            engine, player = self._ready_engine(temporary)
            engine.seek_source_verified(self._seek_req(10))
            player.event_callback({"event": "playback-restart"})
            player.property_callbacks["time-pos"]("time-pos", 0.44)  # 帧11
            self.assertNotIn(("frame-step",), player.commands)
            events = self._verified_events(engine)
            self.assertEqual(len(events), 1)
            self.assertFalse(events[0]["ok"])
            self.assertEqual(events[0]["reason"], "overshoot")
            self.assertEqual(engine.snapshot_perf()["verify_fail"], 1)
            engine.close()

    def test_verified_seek_budget_exhaustion_fails(self):
        # 病理循环：补满预算后 tp 仍不动（步进未生效）→ budget 失败，
        # 绝不无限补步
        with tempfile.TemporaryDirectory() as temporary:
            engine, player = self._ready_engine(temporary)
            engine.seek_source_verified(self._seek_req(10))
            player.event_callback({"event": "playback-restart"})
            player.property_callbacks["time-pos"]("time-pos", 0.24)  # 帧6，差4
            steps = [c for c in player.commands if c == ("frame-step",)]
            self.assertEqual(len(steps), 4)
            player.property_callbacks["time-pos"]("time-pos", 0.24)  # 没动
            events = self._verified_events(engine)
            self.assertEqual(len(events), 1)
            self.assertFalse(events[0]["ok"])
            self.assertEqual(events[0]["reason"], "budget")
            engine.close()

    def test_verified_seek_timeout_watchdog(self):
        with tempfile.TemporaryDirectory() as temporary:
            engine, player = self._ready_engine(temporary)
            engine.seek_source_verified(self._seek_req(10))
            engine._verify["deadline"] = time.monotonic() - 0.001
            engine.poll_events()  # 第一拍触发看门狗（fail 事件在 drain 后入队）
            events = self._verified_events(engine)
            self.assertEqual(len(events), 1)
            self.assertFalse(events[0]["ok"])
            self.assertEqual(events[0]["reason"], "timeout")
            engine.close()

    def test_verified_seek_rejects_dup_segment_interior(self):
        # 重复 PTS 段：段首放行（34/34 必落段首 T0），段内帧拒收
        # （tp 无法区分段内帧）→ False 且不发寻址，调用方走 C4。
        # 名册直接注入：真实源的段行为由阶段0探针覆盖，单测只验拦截逻辑。
        with tempfile.TemporaryDirectory() as temporary:
            engine, player = self._ready_engine(temporary)
            engine._dup_run = tuple([-1] * 10 + [-1, 10] + [-1] * 88)
            self.assertFalse(engine.seek_source_verified(self._seek_req(11)))
            self.assertEqual(
                [c for c in player.commands if c and c[0] == "seek"], [])
            self.assertTrue(engine.seek_source_verified(self._seek_req(10)))
            self.assertTrue(engine.seek_source_verified(self._seek_req(12)))
            seeks = [c for c in player.commands if c and c[0] == "seek"]
            self.assertEqual(len(seeks), 2)
            self.assertEqual(engine.snapshot_perf()["verify_count"], 2)
            engine.close()

    def test_dup_registry_detects_monotonized_runs(self):
        # 认证表强制严格递增：真实重复 PTS 建表时被单调化（+1 tick）。
        # 名册必须按「间隙<2ms」识别准重复段：帧2/3 间隙 1ms → 帧3 段内
        # （dup=2），帧2 段首放行；其余 40ms 间隙 → 唯一。
        from tests.test_preview_engine import _certified_media

        holder = {}

        def factory(**options):
            holder["player"] = _AsyncLoadPlayer(**options)
            return holder["player"]

        rows = []
        prev = None
        for i in range(100):
            if i == 0:
                p = 0
            elif i == 3:
                p = prev + 1  # 1ms 间隙 = 单调化重复对
            else:
                p = prev + 40
            rows.append({"n": i, "pts": p,
                         "duration": 1 if i == 2 else 40})
            prev = p
        # 修 duration：行 i 的 duration = pts[i+1]-pts[i]
        for i in range(99):
            rows[i]["duration"] = rows[i + 1]["pts"] - rows[i]["pts"]
        rows[99]["duration"] = 40
        with tempfile.TemporaryDirectory() as temporary:
            media = _certified_media(Path(temporary), rows)
            engine = MpvEngine(
                str(media.source_path), mpv_factory=factory,
                fps=25.0, total=100,
            )
            engine.bind_media_info(media)
            self.assertEqual(engine._dup_run[3], 2)   # 段内 → 段首帧号
            self.assertEqual(engine._dup_run[2], -1)  # 段首 → 放行
            self.assertEqual(engine._dup_run[4], -1)
            self.assertEqual(engine._dup_run[50], -1)
            engine.close()

    def test_verified_seek_rejects_edl_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            engine, player = self._ready_engine(temporary)
            engine._mode = "edl"
            self.assertFalse(engine.seek_source_verified(self._seek_req(10)))
            self.assertEqual(
                [c for c in player.commands if c and c[0] == "seek"], [])
            engine.close()

    def test_verified_seek_requires_certification(self):
        holder = {}

        def factory(**options):
            holder["player"] = _AsyncLoadPlayer(**options)
            return holder["player"]

        engine = MpvEngine("source.mp4", mpv_factory=factory, fps=25.0, total=100)
        engine.start()
        with self.assertRaises(PreviewEngineError) as ctx:
            engine.seek_source_verified(self._seek_req(10))
        self.assertEqual(ctx.exception.code, "CERTIFICATION_REQUIRED")
        engine.close()

    def test_verified_seek_pending_load_flushes_without_correction(self):
        # 文件未就绪：seek 进 pending 队列，file-loaded 后经 flush 下发，
        # 验证任务飞行中同样不得带 +5/120 修正
        holder = {}

        def factory(**options):
            holder["player"] = _AsyncLoadPlayer(**options)
            return holder["player"]

        from tests.test_preview_engine import _certified_media
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = _certified_media(
                root,
                [{"n": i, "pts": i * 40, "duration": 40} for i in range(100)],
            )
            engine = MpvEngine(
                str(media.source_path), mpv_factory=factory,
                fps=25.0, total=100,
            )
            engine.bind_media_info(media)
            engine.start()
            player = holder["player"]
            self.assertTrue(engine.seek_source_verified(self._seek_req(10)))
            self.assertEqual(
                [c for c in player.commands if c and c[0] == "seek"], [])
            player.event_callback({
                "event": "file-loaded",
                "generation": engine.snapshot_perf()["generation"],
            })
            engine.poll_events()
            seeks = [c for c in player.commands if c and c[0] == "seek"]
            self.assertEqual(len(seeks), 1)
            self.assertAlmostEqual(float(seeks[0][1]), 0.4, places=9)
            player.event_callback({"event": "playback-restart"})
            player.property_callbacks["time-pos"]("time-pos", 0.4)
            events = self._verified_events(engine)
            self.assertEqual(len(events), 1)
            self.assertTrue(events[0]["ok"])
            engine.close()

    def test_plain_paused_seek_keeps_tick_correction(self):
        # 回归护栏：旧路径（拖动/播放中/回退）的暂停 exact seek 仍带
        # +5/120 修正——验证任务的免修正只属于闭环自己
        with tempfile.TemporaryDirectory() as temporary:
            engine, player = self._ready_engine(temporary)
            engine.seek_source(self._seek_req(10))
            seeks = [c for c in player.commands if c and c[0] == "seek"]
            self.assertEqual(len(seeks), 1)
            self.assertAlmostEqual(float(seeks[0][1]), 0.4 + 5 / 120, places=6)
            engine.close()

    def test_step_frame_supersedes_inflight_verify(self):
        # 用户步进取代飞行中的验证任务：静默作废，不再出 seek-verified
        with tempfile.TemporaryDirectory() as temporary:
            engine, player = self._ready_engine(temporary)
            engine.seek_source_verified(self._seek_req(10))
            engine.step_frame(+1)
            player.event_callback({"event": "playback-restart"})
            player.property_callbacks["time-pos"]("time-pos", 0.4)
            self.assertEqual(self._verified_events(engine), [])
            engine.close()

    def test_close_timeout_is_retryable_without_duplicate_terminate(self):
        holder = {}

        def factory(**options):
            holder["player"] = _BlockingTerminatePlayer(**options)
            return holder["player"]

        engine = MpvEngine("source.mp4", mpv_factory=factory)
        engine.start()
        player = holder["player"]
        self.assertFalse(engine.close(timeout=0.001))
        self.assertTrue(engine.is_alive())
        player.allow_terminate.set()
        self.assertTrue(engine.close(timeout=1.0))
        self.assertFalse(engine.is_alive())
        self.assertEqual(player.terminated, 1)

    def test_canonical_load_target_scheme_bypass(self):
        # ram:// URL 原样保留（Path.resolve 会揉碎 scheme），文件路径仍规范化
        from mpv_engine import canonical_load_target

        self.assertEqual(canonical_load_target("ram://skim3"), "ram://skim3")
        resolved = canonical_load_target("source.mp4")
        self.assertNotIn("://", resolved)
        self.assertTrue(resolved.endswith("source.mp4"))

    def test_skim_ineligible_on_fake_binding_falls_back_to_tier1(self):
        # 假绑定没有 handle → stream_cb 未注册 → skim_ready False →
        # blob 拒装；EDL 播放保持 Tier1（mode="edl"），行为与合入前一致。
        from tests.test_preview_engine import _certified_media

        holder = {}

        def factory(**options):
            holder["player"] = _AsyncLoadPlayer(**options)
            return holder["player"]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = _certified_media(
                root,
                [{"n": i, "pts": i * 40, "duration": 40} for i in range(8)],
            )
            engine = MpvEngine(
                str(media.source_path), total=8, mpv_factory=factory,
                edl_dir=root / "edl",
            )
            engine.bind_media_info(media)
            engine.start()
            self.assertFalse(engine.skim_ready)
            self.assertFalse(engine.set_skim_blob(
                bytearray(b"\x47" * 188), ("sha", "fp", 0, 0, 480, 30, 6),
            ))
            plan = TimelinePlan.from_deleted_ranges(8, [(2, 4)])
            self.assertTrue(engine.play_edl(
                CertifiedEdlRequest(media, plan, 0, 0), start_frame=0))
            self.assertEqual(engine.snapshot_perf()["mode"], "edl")
            engine.close()

    def test_play_edl_loads_skim_blob_with_matching_key_and_caps_speed(self):
        from tests.test_preview_engine import _certified_media

        holder = {}

        def factory(**options):
            holder["player"] = _AsyncLoadPlayer(**options)
            return holder["player"]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = _certified_media(
                root,
                [{"n": i, "pts": i * 40, "duration": 40} for i in range(8)],
            )
            engine = MpvEngine(
                str(media.source_path), total=8, mpv_factory=factory,
                edl_dir=root / "edl",
            )
            engine.bind_media_info(media)
            engine.start()
            player = holder["player"]
            plan = TimelinePlan.from_deleted_ranges(8, [(2, 4)])
            # 假绑定无 handle：手动立起注册标志模拟真绑定的 rc=0
            engine._skim_ready = True
            key = (str(media.source_sha256), str(plan.fingerprint), 3, 1,
                   480, 30, 6)
            self.assertTrue(engine.set_skim_blob(
                bytearray(b"\x47" * 188 * 4), key))
            self.assertTrue(engine.play_edl(
                CertifiedEdlRequest(media, plan, 3, 1),
                start_frame=0, playback_rate=20.0))
            loads = [c for c in player.async_commands if c[0] == "loadfile"]
            # loadfile <url> <flags> <index> <options>（0.41 签名；4 参字符串
            # 形态把 options 落进 index 槽被静默吞掉——diag_rawload T0）
            self.assertEqual(
                loads[-1], ("loadfile", "ram://skim1", "replace", "0", "cache=no"))
            player.event_callback({
                "event": "file-loaded",
                "generation": engine.snapshot_perf()["generation"],
            })
            engine.poll_events()
            self.assertEqual(engine.snapshot_perf()["mode"], "skim")
            # Tier2 天花板守卫：20x 请求 → 12x（T0：blob 软解饱和 14.3x）
            self.assertEqual(player.speed, 12.0)
            # 换代：编辑抬 revision → 新 key 新代号；旧请求不再匹配 →
            # 自动回落 Tier1 活 EDL（陈旧 blob 永不服务新段表）
            plan2 = TimelinePlan.from_deleted_ranges(8, [(2, 5)])
            key2 = (str(media.source_sha256), str(plan2.fingerprint), 3, 2,
                    480, 30, 6)
            self.assertTrue(engine.set_skim_blob(
                bytearray(b"\x47" * 188 * 4), key2))
            self.assertTrue(engine.play_edl(
                CertifiedEdlRequest(media, plan, 3, 1), start_frame=0))
            self.assertEqual(engine.snapshot_perf()["mode"], "edl")
            self.assertTrue(engine.play_edl(
                CertifiedEdlRequest(media, plan2, 3, 2), start_frame=0))
            loads = [c for c in player.async_commands if c[0] == "loadfile"]
            self.assertEqual(
                loads[-1], ("loadfile", "ram://skim2", "replace", "0", "cache=no"))
            self.assertEqual(engine.snapshot_perf()["mode"], "skim")
            engine.close()
            self.assertIsNone(engine.skim_key)

    def test_play_edl_skim_gated_by_speed_cap(self):
        # 09-13 T0：blob 在库时 ≤cap 的播放必须走 Tier1 活 EDL（原画质），
        # >cap 才用代理（旧语义 1x 也糊）。产品路径 play() 恒传 speed_cap=3。
        from tests.test_preview_engine import _certified_media

        holder = {}

        def factory(**options):
            holder["player"] = _AsyncLoadPlayer(**options)
            return holder["player"]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = _certified_media(
                root,
                [{"n": i, "pts": i * 40, "duration": 40} for i in range(8)],
            )
            engine = MpvEngine(
                str(media.source_path), total=8, mpv_factory=factory,
                edl_dir=root / "edl",
            )
            engine.bind_media_info(media)
            engine.start()
            player = holder["player"]
            engine._skim_ready = True
            plan = TimelinePlan.from_deleted_ranges(8, [(2, 4)])
            key = (str(media.source_sha256), str(plan.fingerprint), 3, 1,
                   480, 30, 6)
            self.assertTrue(engine.set_skim_blob(
                bytearray(b"G" * 188 * 4), key))
            # 1x ≤ cap3：blob 在库也不得用 → 活 EDL 原画质
            self.assertTrue(engine.play_edl(
                CertifiedEdlRequest(media, plan, 3, 1),
                start_frame=0, playback_rate=1.0, speed_cap=3.0))
            self.assertEqual(engine.snapshot_perf()["mode"], "edl")
            # 4x > cap3：代理才接管
            self.assertTrue(engine.play_edl(
                CertifiedEdlRequest(media, plan, 3, 1),
                start_frame=0, playback_rate=4.0, speed_cap=3.0))
            self.assertEqual(engine.snapshot_perf()["mode"], "skim")
            engine.close()

    def test_seek_edl_preserves_skim_mode_and_stream(self):
        # review 修复 F1 回归守卫：skim 播放中拖时间轴/步进走 seek_edl →
        # 必须留在 ram:// 流内（暂停→寻址→恢复），绝不重载 EDL 文件——
        # 否则 Tier1 活流继承 skim 的 12x 上限，边界风暴复活。
        from tests.test_preview_engine import _certified_media

        holder = {}

        def factory(**options):
            holder["player"] = _AsyncLoadPlayer(**options)
            return holder["player"]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = _certified_media(
                root,
                [{"n": i, "pts": i * 40, "duration": 40} for i in range(8)],
            )
            engine = MpvEngine(
                str(media.source_path), total=8, mpv_factory=factory,
                edl_dir=root / "edl",
            )
            engine.bind_media_info(media)
            engine.start()
            player = holder["player"]
            plan = TimelinePlan.from_deleted_ranges(8, [(2, 4)])
            engine._skim_ready = True
            key = (str(media.source_sha256), str(plan.fingerprint), 0, 0,
                   480, 30, 6)
            self.assertTrue(engine.set_skim_blob(
                bytearray(b"\x47" * 188 * 4), key))
            self.assertTrue(engine.play_edl(
                CertifiedEdlRequest(media, plan, 0, 0), start_frame=0))
            player.event_callback({
                "event": "file-loaded",
                "generation": engine.snapshot_perf()["generation"],
            })
            engine.poll_events()
            self.assertEqual(engine.snapshot_perf()["mode"], "skim")
            loads_before = len(player.async_commands)
            # 播放中寻址：先落暂停 → 流内 seek → 恢复播放；零重载
            self.assertTrue(engine.seek_edl(5))
            self.assertEqual(engine.snapshot_perf()["mode"], "skim")
            self.assertEqual(len(player.async_commands), loads_before)
            self.assertTrue(any(c[0] == "seek" for c in player.commands))
            self.assertTrue(engine.is_playback_active())
            # skim 模式暂停态精确 seek 不带 +5/120 修正（blob 时间轴均匀，
            # T0 实测 5.0→5.0 精确落位；带修正会过冲 ~2.5 帧）
            seek_cmds = [c for c in player.commands if c[0] == "seek"]
            self.assertTrue(seek_cmds)
            from mpv_engine import _fraction_seconds
            virtual = engine._edl.virtual_time_for_source(5, snap=True)
            self.assertEqual(seek_cmds[-1][1], _fraction_seconds(virtual))
            engine.close()


class _FakeDThroughSvc:
    """免进程 dsvc 桩：契约测只看命令面与台账，不看帧流。"""

    def __init__(self):
        self.loads = []
        self.closed = 0
        self.actuals = None
        self.eof = False

    def load(self, src, spans, fps):
        self.loads.append((src, list(spans), fps))
        return 1

    def drain_actuals(self):
        return self.actuals

    def eof_seen(self):
        return bool(self.eof)

    def close(self):
        self.closed += 1


class MpvDThroughContractTests(unittest.TestCase):
    """解码直通档契约（1c）：URL/5 参 loadfile/恒速/台账 seek 重定基/实测读回。"""

    def _media(self, tmp: Path):
        from fractions import Fraction as F

        import media_info
        from media_info import MediaInfo, VideoStreamInfo

        source = tmp / "src.mp4"
        source.write_bytes(b"0" * 16)
        stream = VideoStreamInfo(
            index=0, codec_name="h264", codec_long_name=None, width=64, height=64,
            pixel_format="yuv420p", time_base=F(1, 15360), start_time=None,
            duration=None, avg_frame_rate=F(60, 1), r_frame_rate=F(60, 1),
            frame_count=120,
        )
        return MediaInfo(
            source_path=source, source_sha256=media_info._sha256_file(source),
            source_size=16, source_mtime_ns=0, format_name="mov",
            format_long_name=None, duration=F(2, 1), start_time=None,
            video_streams=(stream,), audio_streams=(), vfr_status="cfr",
        )

    def _engine(self, holder, path: str):
        def factory(**options):
            holder["player"] = _AsyncLoadPlayer(**options)
            return holder["player"]

        engine = MpvEngine(path, mpv_factory=factory, fps=60.0, total=120)
        engine.start()
        holder["player"].event_callback({"event": "file-loaded"})
        engine._dthrough_ready = True  # 假绑定无 stream_cb；契约测只看命令面
        engine._dthrough_svc = _FakeDThroughSvc()
        return engine

    def _request(self, media):
        return CertifiedEdlRequest(
            media_info=media,
            timeline_plan=TimelinePlan.from_delete_mask([False] * 120),
            project_generation=0,
            timeline_revision=0,
        )

    def _spans(self):
        from dthrough_ledger import LedgerSpan

        return [LedgerSpan(0, 60, 10, 0.0, 1.0), LedgerSpan(60, 120, 1, 1.0, 2.0)]

    def test_snapshot_source_frame_none_until_position_read(self):
        """换载窗口不得报复位值 0（09-24 review T0：红条闪回开头根因）。

        `_ensure_loaded_locked` 把 `_source_frame` 复位 0；GUI 播放中无条件
        采信引擎帧号 → 跨段起播/编辑后重播时红条闪 0（实测 2 拍 ~20ms）。
        现 `snapshot_perf` 在「本代尚无 time-pos 读回」时报 None，消费者
        据 `isinstance(int)` 自然跳过。
        """
        holder = {}
        media = self._media(Path(tempfile.mkdtemp()))
        engine = self._engine(holder, str(media.source_path))
        try:
            # 起始：source 模式已载但尚未读回位置
            self.assertIsNone(engine.snapshot_perf().get("source_frame"))
            holder["player"].event_callback({"event": "file-loaded"})
            # 首个 time-pos 到达 → 转为具体帧号（本夹具无认证时间轴，
            # source 模式映射停在 0，但**契约是 None→int**，不再报 None）
            holder["player"].property_callbacks["time-pos"]("time-pos", 0.5)
            self.assertIsInstance(engine.snapshot_perf().get("source_frame"), int)
            # 换载（新代）→ 回到 None，而非 0
            engine.play_dthrough(self._request(media), spans=self._spans(),
                                 fps=60.0, start_source_frame=90)
            self.assertIsNone(engine.snapshot_perf().get("source_frame"),
                              "换载窗口必须报 None 而非复位值 0")
        finally:
            engine.close()

    def test_play_dthrough_contract_loadfile_constant_speed_ledger(self):
        holder = {}
        media = self._media(Path(tempfile.mkdtemp()))
        engine = self._engine(holder, str(media.source_path))
        try:
            self.assertTrue(engine.play_dthrough(
                self._request(media),
                spans=self._spans(),
                fps=60.0,
                start_source_frame=90,
            ))
            player = holder["player"]
            loads = [
                c for c in player.async_commands
                if c[0] == "loadfile" and str(c[1]).startswith("rawpush://")
            ]
            self.assertEqual(len(loads), 1)
            # 5 参签名（0.41 陷阱：options 落 index 槽=静默丢失）+ 纯 scheme URL
            self.assertEqual(loads[0][:2], ("loadfile", "rawpush://dt1"))
            self.assertEqual(loads[0][2:4], ("replace", "0"))
            options = loads[0][4]
            for token in ("cache=no", "framedrop=no", "demuxer=rawvideo",
                          "demuxer-rawvideo-format=I420", "start=0.600000"):
                self.assertIn(token, options)
            # 恒速：业务倍速在推流端抽帧，mpv speed 恒 1.0（经 pending
            # 冲刷落到 speed 属性；假绑定的属性赋值即写 player.speed）
            player.event_callback({"event": "file-loaded"})
            self.assertEqual(player.speed, 1.0)
            self.assertEqual(engine._mode, "dthrough")
            # 台账随播放建立：起点 90 在段2(rate1) → 名义 tick = 6+30 = 36
            self.assertEqual(
                engine._dthrough_ledger.nominal_tick_for_source_frame(90), 36
            )
            # 档位速率=起点段抽帧率（角标数据源）
            self.assertEqual(engine._dthrough_rate, 1.0)
        finally:
            engine.close()

    def test_play_dthrough_midstart_rebases_ledger(self):
        """中途起播必须重定基（09-23 真机 T0：红条横跳根因）。

        demuxer 的 per-file ``start=`` 会经 seek_fn 触发 dsvc **seek 代**，
        本代只从目标段起推（目标段之前的 per_span 全 0）。若台账不重定基，
        那些 0 会把实测轴 tick→源帧映射整体拽偏，且随 span 事件到达阶跃
        变化——真机探针实测 start=12000 读回 23999（红条横跳）。
        """
        holder = {}
        media = self._media(Path(tempfile.mkdtemp()))
        engine = self._engine(holder, str(media.source_path))
        try:
            self.assertTrue(engine.play_dthrough(
                self._request(media),
                spans=self._spans(),
                fps=60.0,
                start_source_frame=90,
            ))
            ledger = engine._dthrough_ledger
            # 起点 90 在段2(rate1) → 名义 tick=36 → 实测轴原点=36
            self.assertEqual(ledger._tick_origin, 36)
            # 新代实测（绝对段号）：段0/段1 未推=0，段2 实推 10（90..99）
            engine._dthrough_svc.actuals = ([0, 10, 0], False)
            # 落点 tick 必须回到 90（未重定基时会被 0 前缀拽到段1 起点 60）
            self.assertEqual(engine._frame_for_position_locked(36 / 60), 90)
            # 落点之后落在截断段内（90..120），不得越出到段1
            after = engine._frame_for_position_locked(40 / 60)
            self.assertGreaterEqual(after, 90)
            self.assertLess(after, 120)
        finally:
            engine.close()

    def test_resume_same_span_skips_reload(self):
        """暂停→播放同段：免整路重载（09-23 真机 T0：恢复首秒不满 60 帧）。

        真机探针（probe_resume_stall）：重载恢复首 0.5s 窗 0/18.6/23.5fps，
        纯 resume 恒 59~61fps。故同段 resume 必须走快速路径（不新增
        dsvc.load、不改 URL 代）。
        """
        holder = {}
        media = self._media(Path(tempfile.mkdtemp()))
        engine = self._engine(holder, str(media.source_path))
        try:
            req = self._request(media)
            spans = self._spans()
            self.assertTrue(engine.play_dthrough(
                req, spans=spans, fps=60.0, start_source_frame=90))
            holder["player"].event_callback({"event": "file-loaded"})
            # 模拟播放推进：time-pos 回调把引擎位置落在段2（60..120）
            holder["player"].property_callbacks["time-pos"]("time-pos", 40 / 60)
            self.assertEqual(engine._source_frame, 94)
            engine.stop()
            loads_before = len(engine._dthrough_svc.loads)
            url_id_before = engine._dthrough_id
            # 同一段内恢复（94→95 同段）
            self.assertTrue(engine.play_dthrough(
                req, spans=spans, fps=60.0, start_source_frame=95))
            self.assertEqual(len(engine._dthrough_svc.loads), loads_before,
                             "同段 resume 不得重载 dsvc 流")
            self.assertEqual(engine._dthrough_id, url_id_before,
                             "同段 resume 不得换 URL 代（否则 mpv 整路重开）")
            self.assertTrue(engine.is_playback_active())
        finally:
            engine.close()

    def test_resume_cross_span_still_reloads(self):
        """跨段起播必须回落完整重载（推流相位与 UI 位置一致性）。"""
        holder = {}
        media = self._media(Path(tempfile.mkdtemp()))
        engine = self._engine(holder, str(media.source_path))
        try:
            req = self._request(media)
            spans = self._spans()
            self.assertTrue(engine.play_dthrough(
                req, spans=spans, fps=60.0, start_source_frame=10))
            holder["player"].event_callback({"event": "file-loaded"})
            engine.stop()
            loads_before = len(engine._dthrough_svc.loads)
            # 起点 90 在段2，当前在段1 → 跨段 → 必须重载
            self.assertTrue(engine.play_dthrough(
                req, spans=spans, fps=60.0, start_source_frame=90))
            self.assertGreater(len(engine._dthrough_svc.loads), loads_before,
                               "跨段起播必须重载")
        finally:
            engine.close()

    def test_resume_span_table_change_reloads(self):
        """段表变化（编辑后）必须重载，不得复用旧流。"""
        holder = {}
        media = self._media(Path(tempfile.mkdtemp()))
        engine = self._engine(holder, str(media.source_path))
        try:
            req = self._request(media)
            spans = self._spans()
            self.assertTrue(engine.play_dthrough(
                req, spans=spans, fps=60.0, start_source_frame=90))
            holder["player"].event_callback({"event": "file-loaded"})
            engine.stop()
            loads_before = len(engine._dthrough_svc.loads)
            from dthrough_ledger import LedgerSpan
            changed = [LedgerSpan(0, 60, 5, 0.0, 1.0),
                       LedgerSpan(60, 120, 1, 1.0, 2.0)]
            self.assertTrue(engine.play_dthrough(
                req, spans=changed, fps=60.0, start_source_frame=90))
            self.assertGreater(len(engine._dthrough_svc.loads), loads_before,
                               "段表变化必须重载")
        finally:
            engine.close()

    def test_resume_after_stream_eof_reloads(self):
        """本代流已推完（EOF）时同段 resume 必须重载，不得空转假播放。"""
        holder = {}
        media = self._media(Path(tempfile.mkdtemp()))
        engine = self._engine(holder, str(media.source_path))
        try:
            req = self._request(media)
            spans = self._spans()
            self.assertTrue(engine.play_dthrough(
                req, spans=spans, fps=60.0, start_source_frame=90))
            holder["player"].event_callback({"event": "file-loaded"})
            holder["player"].property_callbacks["time-pos"]("time-pos", 40 / 60)
            engine.stop()
            engine._dthrough_svc.eof = True   # 本代流推完
            loads_before = len(engine._dthrough_svc.loads)
            self.assertTrue(engine.play_dthrough(
                req, spans=spans, fps=60.0, start_source_frame=95))
            self.assertGreater(len(engine._dthrough_svc.loads), loads_before,
                               "EOF 后的同段 resume 必须重载起新代")
        finally:
            engine.close()

    def test_play_dthrough_rejects_empty_spans(self):
        holder = {}
        media = self._media(Path(tempfile.mkdtemp()))
        engine = self._engine(holder, str(media.source_path))
        try:
            with self.assertRaises(PreviewEngineError) as blocked:
                engine.play_dthrough(self._request(media), spans=[], fps=60.0)
            self.assertEqual(blocked.exception.code, "DTHROUGH_EMPTY_SPANS")
        finally:
            engine.close()

    def test_seek_dthrough_source_frame_rebases_ledger_and_readback(self):
        holder = {}
        media = self._media(Path(tempfile.mkdtemp()))
        engine = self._engine(holder, str(media.source_path))
        try:
            engine.play_dthrough(self._request(media), spans=self._spans(), fps=60.0)
            # load 代 tp 读回（名义暂替）：tick 40 → 段2 槽 34 → 帧 94
            self.assertEqual(engine._frame_for_position_locked(40 / 60), 94)
            # dsvc 实测到达（段1 名义 60 只实推 30，VFR 压缩）→ 禁名义值
            engine._dthrough_svc.actuals = ([30, 60], False)
            self.assertEqual(engine._frame_for_position_locked(35 / 60), 65)
            # 档位速率随读回段切换：帧 65 在段2 rate=1
            self.assertEqual(engine._dthrough_rate, 1.0)
            # 播中 seek（源帧入口）：名义 tick 36 → mpv 0.6s；dthrough 暂停
            # 态豁免 +5/120 修正。dsvc seek 会重置本代实测（actuals=None）
            engine._dthrough_svc.actuals = None
            self.assertTrue(engine.seek_dthrough_source_frame(90))
            self.assertIn(("seek", "0.6", "absolute+exact"), holder["player"].commands)
            # 台账重定基：实测轴原点=36，落点帧=90
            self.assertEqual(engine._dthrough_ledger._tick_origin, 36)
            self.assertEqual(engine._source_frame, 90)
            # 新代读回：截断段名义估计（剩余 30 槽）tick 40 → 90+4=94；
            # 新代实测到达（dsvc per_span 每代重置：段2 区域实推 40）→ 93；
            # 原点前的 tick 钳落点帧
            self.assertEqual(engine._frame_for_position_locked(40 / 60), 94)
            engine._dthrough_svc.actuals = ([0, 40], False)
            self.assertEqual(engine._frame_for_position_locked(40 / 60), 93)
            self.assertEqual(engine._frame_for_position_locked(35 / 60), 90)
        finally:
            engine.close()

    def test_seek_dthrough_raw_tick_still_rebases(self):
        holder = {}
        media = self._media(Path(tempfile.mkdtemp()))
        engine = self._engine(holder, str(media.source_path))
        try:
            engine.play_dthrough(self._request(media), spans=self._spans(), fps=60.0)
            self.assertTrue(engine.seek_dthrough_frame(36))  # 段2 槽 30 → 帧 90
            self.assertEqual(engine._dthrough_ledger._tick_origin, 36)
            self.assertEqual(engine._source_frame, 90)
        finally:
            engine.close()

    def test_seek_dthrough_source_frame_requires_ledger(self):
        holder = {}
        media = self._media(Path(tempfile.mkdtemp()))
        engine = self._engine(holder, str(media.source_path))
        try:
            # 未播放（无台账）：直通 seek 拒收而非错位寻址
            engine._mode = "dthrough"
            with self.assertRaises(PreviewEngineError) as blocked:
                engine.seek_dthrough_source_frame(10)
            self.assertEqual(blocked.exception.code, "DTHROUGH_NOT_ACTIVE")
        finally:
            engine.close()


if __name__ == "__main__":
    unittest.main()
