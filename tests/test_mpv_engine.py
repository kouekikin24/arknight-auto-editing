from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import unittest

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
            self.assertEqual(seek_commands[-1], ("seek", "0.12", "absolute+exact"))
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
            self.assertEqual(seek_commands[-1], ("seek", "0.24", "absolute+exact"))
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

    def test_business_frame_speed_policy_fails_closed(self):
        from preview_engine import PreviewPlayRequest

        engine = MpvEngine("source.mp4", mpv_factory=lambda **options: _FakePlayer(**options), total=2)
        request = PreviewPlayRequest(
            start_frame=0,
            playback_rate=1.0,
            preview_step=1,
            speed_multiplier=1.0,
            skip_trimmed=False,
            speedup_1x=True,
            speedup_02=False,
            speedup_02_factor=1,
            timeline_plan=TimelinePlan.from_deleted_ranges(2, []),
            speed_segments=((0, 1, 1),),
            canvas_size=(1, 1),
            project_generation=0,
            timeline_revision=0,
        )
        with self.assertRaises(PreviewEngineError) as raised:
            engine.play(request)
        self.assertEqual(raised.exception.code, "MPV_SPEED_POLICY_UNSUPPORTED")
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


if __name__ == "__main__":
    unittest.main()
