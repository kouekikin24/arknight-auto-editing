from __future__ import annotations

from pathlib import Path
from queue import Queue
import threading
import tempfile
import unittest

from cv_engine import CvEngine
from mpv_engine import MpvEngine
from preview_engine import PreviewEngineError, PreviewPlayRequest, SourceSeekRequest
from timeline_plan import TimelinePlan


class _CvIO:
    fps = 60.0
    total = 20

    def __init__(self, path, frame_queue):
        self.path = path
        self.frame_queue = frame_queue
        self.commands = []
        self.started = False
        self.close_calls = 0
        self.closed = False
        self.pace_mode = "opt"

    def start(self):
        self.started = True

    def send(self, command):
        self.commands.append(command)
        return not self.closed

    def set_pace_mode(self, mode):
        self.pace_mode = str(mode)

    def get_pace_mode(self):
        return self.pace_mode

    def snapshot_perf(self):
        return {"presented": 0}

    def reset_perf_stats(self):
        pass

    def begin_perf_segment(self):
        pass

    def is_playback_active(self):
        return False

    def is_alive(self):
        return not self.closed

    def close(self, timeout=1.0):
        self.close_calls += 1
        self.closed = True
        return True


class _RetryCvIO(_CvIO):
    def close(self, timeout=1.0):
        self.close_calls += 1
        if self.close_calls == 1:
            return False
        self.closed = True
        return True


class _MpvPlayer:
    def __init__(self, **options):
        self.options = options
        self.commands = []
        self.event_callback = None
        self.property_callbacks = {}
        self._pause = True
        self.speed = 1.0
        self.terminated = 0

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


class _BlockingMpvPlayer(_MpvPlayer):
    def __init__(self, **options):
        super().__init__(**options)
        self.allow_terminate = threading.Event()

    def terminate(self):
        self.terminated += 1
        self.allow_terminate.wait(1.0)


def _source_play_request() -> PreviewPlayRequest:
    return PreviewPlayRequest(
        start_frame=0,
        playback_rate=1.0,
        preview_step=1,
        speed_multiplier=1.0,
        skip_trimmed=False,
        speedup_1x=False,
        speedup_02=False,
        speedup_02_factor=1,
        timeline_plan=TimelinePlan.from_deleted_ranges(20, []),
        speed_segments=(),
        canvas_size=(320, 180),
        project_generation=1,
        timeline_revision=1,
    )


class PreviewLifecycleTests(unittest.TestCase):
    def test_cv_close_is_one_way_and_idempotent(self):
        holder = {}

        def factory(path, frame_queue):
            holder["io"] = _CvIO(path, frame_queue)
            return holder["io"]

        engine = CvEngine("source.mp4", Queue(), io_factory=factory)
        engine.start()
        self.assertTrue(engine.close())
        self.assertTrue(engine.close())
        self.assertEqual(holder["io"].close_calls, 1)
        self.assertEqual(
            sum(event["event"] == "closed" for event in engine.poll_events()),
            1,
        )

        requests = [
            lambda: engine.start(),
            lambda: engine.bind_media_info(None),
            lambda: engine.seek_source(SourceSeekRequest(0, (1, 1), 0, exact=False)),
            lambda: engine.play(_source_play_request()),
            lambda: engine.stop(),
            lambda: engine.send({"type": "stop"}),
            lambda: engine.set_viewport(320, 180),
            lambda: engine.set_pace_mode("base"),
            lambda: engine.reset_perf_stats(),
            lambda: engine.begin_perf_segment(),
        ]
        for command in requests:
            with self.subTest(command=command):
                with self.assertRaises(PreviewEngineError) as raised:
                    command()
                self.assertEqual(raised.exception.code, "ENGINE_CLOSED")
        self.assertEqual(holder["io"].commands, [])

    def test_cv_timeout_closes_command_surface_but_allows_retry(self):
        holder = {}

        def factory(path, frame_queue):
            holder["io"] = _RetryCvIO(path, frame_queue)
            return holder["io"]

        engine = CvEngine("source.mp4", Queue(), io_factory=factory)
        engine.start()
        self.assertFalse(engine.close(timeout=0.0))
        with self.assertRaises(PreviewEngineError) as raised:
            engine.stop()
        self.assertEqual(raised.exception.code, "ENGINE_CLOSED")
        self.assertTrue(engine.close(timeout=0.0))
        self.assertEqual(holder["io"].close_calls, 2)

    def test_mpv_closed_commands_are_rejected_but_quit_is_idempotent(self):
        holder = {}

        def factory(**options):
            holder["player"] = _MpvPlayer(**options)
            return holder["player"]

        engine = MpvEngine("source.mp4", mpv_factory=factory, total=20)
        engine.start()
        self.assertTrue(engine.close())
        self.assertEqual(engine.snapshot_perf()["play_end_reason"], "shutdown")
        self.assertTrue(engine.send({"type": "quit"}))

        requests = [
            lambda: engine.bind_media_info(None),
            lambda: engine.seek_source(SourceSeekRequest(0, (1, 1), 0, exact=False)),
            lambda: engine.play(_source_play_request()),
            lambda: engine.stop(),
            lambda: engine.send({"type": "stop"}),
            lambda: engine.send({"type": "unsupported"}),
            lambda: engine.set_viewport(320, 180),
            lambda: engine.set_pace_mode("base"),
            lambda: engine.reset_perf_stats(),
            lambda: engine.begin_perf_segment(),
        ]
        for command in requests:
            with self.subTest(command=command):
                with self.assertRaises(PreviewEngineError) as raised:
                    command()
                self.assertEqual(raised.exception.code, "ENGINE_CLOSED")

    def test_mpv_close_cancels_pending_file_loaded_actions(self):
        holder = {}

        def factory(**options):
            holder["player"] = _BlockingMpvPlayer(**options)
            return holder["player"]

        engine = MpvEngine("source.mp4", mpv_factory=factory, total=20)
        engine.start()
        player = holder["player"]
        self.assertTrue(engine.play(_source_play_request()))
        generation = engine.snapshot_perf()["generation"]
        # file-loaded now releases the queued seek/resume on the binding's
        # event thread itself, so the seek lands before close() begins.
        player.event_callback({"event": "file-loaded", "generation": generation})
        flushed_seeks = sum(1 for command in player.commands if command[0] == "seek")
        self.assertEqual(flushed_seeks, 1)
        self.assertFalse(engine.close(timeout=0.001))
        # Once close begins, later load completions and owner polls must
        # not issue any further commands.  close() has already detached the
        # binding callback, so deliver the event to the engine directly.
        engine._on_event({"event": "file-loaded", "generation": generation})
        engine.poll_events()
        self.assertEqual(
            sum(1 for command in player.commands if command[0] == "seek"),
            flushed_seeks,
        )
        player.allow_terminate.set()
        self.assertTrue(engine.close(timeout=1.0))


if __name__ == "__main__":
    unittest.main()
