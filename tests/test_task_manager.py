from __future__ import annotations

import threading
import time
import unittest
from concurrent.futures import CancelledError
from unittest.mock import patch

from task_manager import TaskCancelled, TaskManager


class TaskManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = TaskManager(max_workers=2)

    def tearDown(self) -> None:
        self.manager.close(wait=True)

    def _finish(self, *handles) -> None:
        for handle in handles:
            if handle.future is None:
                continue
            try:
                handle.future.result(timeout=2.0)
            except (CancelledError, TaskCancelled):
                pass
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            with self.manager._lock:
                needs_capture = bool(self.manager._active)
            if not needs_capture or not self.manager._completions.empty():
                break
            time.sleep(0.001)
        self.manager.dispatch_pending()

    def _wait_for_completion_capture(self) -> None:
        deadline = time.monotonic() + 1.0
        while self.manager._completions.empty() and time.monotonic() < deadline:
            time.sleep(0.001)

    def test_callbacks_run_only_when_owner_dispatches(self) -> None:
        owner_thread = threading.get_ident()
        callback_threads = []

        handle = self.manager.submit(
            "probe",
            lambda _ctx: threading.get_ident(),
            on_success=lambda _result: callback_threads.append(threading.get_ident()),
        )
        assert handle.future is not None
        worker_thread = handle.future.result(timeout=2.0)
        self.assertEqual(callback_threads, [])

        self._wait_for_completion_capture()
        self.manager.dispatch_pending()
        self.assertNotEqual(worker_thread, owner_thread)
        self.assertEqual(callback_threads, [owner_thread])

    def test_new_generation_suppresses_an_older_result(self) -> None:
        release_old = threading.Event()
        results = []

        old = self.manager.submit(
            "analysis",
            lambda _ctx: (release_old.wait(1.0), "old")[1],
            on_success=results.append,
        )
        new = self.manager.submit(
            "analysis",
            lambda _ctx: "new",
            on_success=results.append,
        )
        release_old.set()
        self._finish(old, new)

        self.assertTrue(old.cancelled)
        self.assertEqual(results, ["new"])
        self.assertEqual(self.manager.current_generation("analysis"), 2)

    def test_cancel_is_cooperative_and_reports_cancelled(self) -> None:
        started = threading.Event()
        statuses = []

        def work(ctx):
            started.set()
            while True:
                ctx.raise_if_cancelled()
                time.sleep(0.001)

        handle = self.manager.submit("export", work, on_done=statuses.append)
        self.assertTrue(started.wait(1.0))
        self.assertTrue(self.manager.cancel("export"))
        self._finish(handle)

        self.assertEqual(statuses, ["cancelled"])

    def test_progress_is_coalesced_to_the_latest_value(self) -> None:
        progress = []

        def work(ctx):
            ctx.report_progress(1)
            ctx.report_progress(2)
            return "done"

        handle = self.manager.submit(
            "analysis",
            work,
            on_progress=progress.append,
        )
        self._finish(handle)
        self.assertEqual(progress, [2])

    def test_close_suppresses_callbacks_and_rejects_new_work(self) -> None:
        callbacks = []
        handle = self.manager.submit(
            "probe",
            lambda _ctx: "done",
            on_success=callbacks.append,
        )
        assert handle.future is not None
        handle.future.result(timeout=2.0)
        self.manager.close(wait=True)
        self.manager.dispatch_pending()

        self.assertEqual(callbacks, [])
        with self.assertRaises(RuntimeError):
            self.manager.submit("other", lambda _ctx: None)

    def test_invalidate_discards_an_already_completed_result(self) -> None:
        results = []
        handle = self.manager.submit(
            "analysis",
            lambda _ctx: "old",
            on_success=results.append,
        )
        assert handle.future is not None
        handle.future.result(timeout=2.0)

        generation = self.manager.invalidate("analysis")
        self.manager.dispatch_pending()

        self.assertEqual(generation, 2)
        self.assertEqual(results, [])

    def test_success_callback_error_is_reported_as_task_error(self) -> None:
        reported = []

        class Scheduler:
            def after(self, _delay, _callback):
                return "poll"

            def after_cancel(self, _after_id):
                pass

            def report_callback_exception(self, exc_type, exc, _traceback):
                reported.append((exc_type, str(exc)))

        manager = TaskManager(Scheduler())
        errors = []
        done = []
        try:
            handle = manager.submit(
                "probe",
                lambda _ctx: "ok",
                on_success=lambda _result: (_ for _ in ()).throw(ValueError("boom")),
                on_error=lambda exc: errors.append((type(exc), str(exc))),
                on_done=done.append,
            )
            assert handle.future is not None
            handle.future.result(timeout=2.0)
            deadline = time.monotonic() + 1.0
            while manager._completions.empty() and time.monotonic() < deadline:
                time.sleep(0.001)
            manager.dispatch_pending()
        finally:
            manager.close(wait=True)

        self.assertEqual(reported, [])
        self.assertEqual(errors, [(ValueError, "boom")])
        self.assertEqual(done, ["error"])

    def test_close_has_one_deadline_and_reports_non_cooperative_worker(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def work(_ctx):
            started.set()
            release.wait(1.0)

        handle = self.manager.submit("probe", work)
        self.assertTrue(started.wait(1.0))
        survivors = self.manager.close(timeout=0.01)
        self.assertEqual(survivors, [handle.thread.name])

        release.set()
        handle.thread.join(1.0)
        self.assertEqual(self.manager.close(timeout=0.01), [])

    def test_cancel_after_worker_completion_is_dispatched_as_cancelled(self) -> None:
        successes = []
        statuses = []
        handle = self.manager.submit(
            "probe",
            lambda _ctx: "done",
            on_success=successes.append,
            on_done=statuses.append,
        )
        assert handle.future is not None
        handle.future.result(timeout=2.0)
        handle.cancel()
        deadline = time.monotonic() + 1.0
        while self.manager._completions.empty() and time.monotonic() < deadline:
            time.sleep(0.001)
        self.manager.dispatch_pending()

        self.assertEqual(successes, [])
        self.assertEqual(statuses, ["cancelled"])

    def test_generation_started_by_success_callback_suppresses_old_done(self) -> None:
        events = []
        replacements = []

        def start_next(_value):
            events.append("old-success")
            replacements.append(self.manager.submit(
                "probe",
                lambda _ctx: "new",
                on_success=lambda value: events.append(value),
            ))

        old = self.manager.submit("probe", lambda _ctx: "old", on_success=start_next,
                                  on_done=lambda status: events.append("old-" + status))
        assert old.future is not None
        old.future.result(timeout=2.0)
        self._wait_for_completion_capture()
        self.manager.dispatch_pending()
        self.assertEqual(len(replacements), 1)
        new = replacements[0]
        assert new.future is not None
        new.future.result(timeout=2.0)
        self.manager.dispatch_pending()

        self.assertEqual(events, ["old-success", "new"])

    def test_invalidate_immediately_releases_stale_callbacks_and_handle(self) -> None:
        started = threading.Event()
        release = threading.Event()
        results = []

        def work(_ctx):
            started.set()
            release.wait(1.0)
            return "stale"

        handle = self.manager.submit("analysis", work, on_success=results.append)
        self.assertTrue(started.wait(1.0))
        self.manager.invalidate("analysis")

        self.assertNotIn(handle.task_id, self.manager._callbacks)
        self.assertNotIn(handle.task_id, self.manager._handles)
        self.assertNotIn("analysis", self.manager._active)

        release.set()
        assert handle.thread is not None
        handle.thread.join(1.0)
        self.manager.dispatch_pending()
        self.assertEqual(results, [])
        self.assertTrue(self.manager._completions.empty())

    def test_replacement_immediately_releases_old_callbacks_and_handle(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def old_work(_ctx):
            started.set()
            release.wait(1.0)
            return "old"

        old = self.manager.submit("analysis", old_work, on_success=lambda _value: None)
        self.assertTrue(started.wait(1.0))
        new = self.manager.submit("analysis", lambda _ctx: "new")

        self.assertNotIn(old.task_id, self.manager._callbacks)
        self.assertNotIn(old.task_id, self.manager._handles)
        self.assertIs(self.manager._active["analysis"], new)

        release.set()
        self._finish(old, new)

    def test_close_drains_queued_and_drops_late_completions(self) -> None:
        completed = self.manager.submit("completed", lambda _ctx: bytearray(1024))
        assert completed.future is not None
        completed.future.result(timeout=2.0)
        deadline = time.monotonic() + 1.0
        while self.manager._completions.empty() and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertFalse(self.manager._completions.empty())

        started = threading.Event()
        release = threading.Event()

        def late_work(_ctx):
            started.set()
            release.wait(1.0)
            return bytearray(1024)

        late = self.manager.submit("late", late_work)
        self.assertTrue(started.wait(1.0))
        self.manager.close(timeout=0.0)
        self.assertTrue(self.manager._completions.empty())

        release.set()
        assert late.thread is not None
        late.thread.join(1.0)
        self.assertTrue(self.manager._completions.empty())

    def test_second_close_can_wait_for_a_reported_survivor(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def work(_ctx):
            started.set()
            release.wait(1.0)

        handle = self.manager.submit("probe", work)
        self.assertTrue(started.wait(1.0))
        self.assertEqual(self.manager.close(timeout=0.0), [handle.thread.name])

        timer = threading.Timer(0.03, release.set)
        timer.start()
        try:
            self.assertEqual(self.manager.close(timeout=0.5), [])
        finally:
            release.set()
            timer.cancel()
        assert handle.thread is not None
        self.assertFalse(handle.thread.is_alive())

    def test_task_kind_is_normalized_for_all_public_operations(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def work(ctx):
            started.set()
            release.wait(1.0)
            ctx.checkpoint()

        handle = self.manager.submit("  probe  ", work)
        self.assertTrue(started.wait(1.0))
        self.assertEqual(handle.kind, "probe")
        self.assertEqual(self.manager.current_generation(" probe "), 1)
        self.assertTrue(self.manager.cancel(" probe "))
        self.assertEqual(self.manager.invalidate(" probe "), 2)
        self.assertEqual(self.manager.current_generation("probe"), 2)
        release.set()
        assert handle.thread is not None
        handle.thread.join(1.0)

    def test_thread_start_failure_rolls_back_task_state(self) -> None:
        with patch("task_manager.threading.Thread.start", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                self.manager.submit("probe", lambda _ctx: None)

        self.assertNotIn("probe", self.manager._active)
        self.assertEqual(self.manager._callbacks, {})
        self.assertEqual(self.manager._handles, {})
        self.assertEqual(self.manager._threads, set())

    def test_post_runs_only_when_owner_dispatches(self) -> None:
        owner = threading.get_ident()
        seen = []
        worker = threading.Thread(
            target=lambda: self.manager.post(
                lambda value: seen.append((value, threading.get_ident())), 7
            ),
            daemon=True,
        )
        worker.start()
        worker.join(1.0)
        self.assertEqual(seen, [])
        self.manager.dispatch_pending()
        self.assertEqual(seen, [(7, owner)])

    def test_posted_callbacks_are_dropped_on_close(self) -> None:
        seen = []
        self.assertTrue(self.manager.post(lambda: seen.append("late")))
        self.manager.close(wait=True)
        self.manager.dispatch_pending()
        self.assertEqual(seen, [])

    def test_project_scope_is_immutable_on_handle_and_context(self) -> None:
        seen = []

        def work(context):
            seen.append((context.project_generation, context.timeline_revision))
            return "ok"

        handle = self.manager.submit(
            "scoped",
            work,
            project_generation=7,
            timeline_revision=11,
        )
        self._finish(handle)

        self.assertEqual((handle.project_generation, handle.timeline_revision), (7, 11))
        self.assertEqual(seen, [(7, 11)])

    def test_scope_invalidation_cancels_stale_tasks_but_not_unscoped_work(self) -> None:
        release = threading.Event()

        def work(context):
            release.wait(1.0)
            context.checkpoint()

        old_project = self.manager.submit(
            "analysis", work, project_generation=1, timeline_revision=4
        )
        old_revision = self.manager.submit(
            "export", work, project_generation=2, timeline_revision=3
        )
        current = self.manager.submit(
            "segments", work, project_generation=2, timeline_revision=4
        )
        unscoped = self.manager.submit("gpu", work)

        invalidated = self.manager.invalidate_scope(
            project_generation=2, timeline_revision=4
        )
        self.assertEqual(invalidated, tuple(sorted((old_project.task_id, old_revision.task_id))))
        self.assertTrue(old_project.cancelled)
        self.assertTrue(old_revision.cancelled)
        self.assertFalse(current.cancelled)
        self.assertFalse(unscoped.cancelled)

        release.set()
        self._finish(old_project, old_revision, current, unscoped)

    def test_scope_change_blocks_final_commit_after_last_checkpoint(self) -> None:
        ready = threading.Event()
        allow_commit = threading.Event()
        committed = []

        def work(context):
            context.checkpoint()
            ready.set()
            allow_commit.wait(1.0)
            return context.commit(committed.append, "stale")

        self.manager.invalidate_scope(project_generation=2, timeline_revision=4)
        handle = self.manager.submit(
            "export",
            work,
            project_generation=2,
            timeline_revision=4,
        )
        self.assertTrue(ready.wait(1.0))
        with self.manager.scope_transition():
            self.manager.invalidate_scope(project_generation=2, timeline_revision=5)
        allow_commit.set()
        assert handle.future is not None
        with self.assertRaises(TaskCancelled):
            handle.future.result(timeout=2.0)
        self.assertEqual(committed, [])

    def test_stale_scope_is_rejected_before_worker_submission(self) -> None:
        self.manager.invalidate_scope(project_generation=5, timeline_revision=8)

        with self.assertRaisesRegex(RuntimeError, "stale before submission"):
            self.manager.submit(
                "old-project",
                lambda _context: None,
                project_generation=4,
                timeline_revision=8,
            )
        with self.assertRaisesRegex(RuntimeError, "stale before submission"):
            self.manager.submit(
                "old-revision",
                lambda _context: None,
                project_generation=5,
                timeline_revision=7,
            )

        self.assertEqual(self.manager.current_generation("old-project"), 0)
        self.assertEqual(self.manager.current_generation("old-revision"), 0)
        self.assertEqual(self.manager._handles, {})

    def test_scope_change_after_final_commit_keeps_completed_result(self) -> None:
        committed = threading.Event()
        release = threading.Event()
        published = []
        callbacks = []

        def work(context):
            context.commit(published.append, "artifact", final=True)
            committed.set()
            release.wait(1.0)
            return "done"

        self.manager.invalidate_scope(project_generation=6, timeline_revision=2)
        handle = self.manager.submit(
            "export",
            work,
            project_generation=6,
            timeline_revision=2,
            on_success=lambda value: callbacks.append(("success", value)),
            on_cancelled=lambda: callbacks.append(("cancelled", None)),
            on_done=lambda status: callbacks.append(("done", status)),
        )
        self.assertTrue(committed.wait(1.0))

        invalidated = self.manager.invalidate_scope(
            project_generation=6, timeline_revision=3
        )
        self.assertEqual(invalidated, ())
        self.assertFalse(handle.cancelled)

        release.set()
        self._finish(handle)
        self.assertEqual(published, ["artifact"])
        self.assertEqual(callbacks, [("success", "done"), ("done", "success")])

    def test_scope_invalidation_runs_cancelled_cleanup_not_success(self) -> None:
        ready = threading.Event()
        release = threading.Event()
        callbacks = []

        def work(context):
            ready.set()
            release.wait(1.0)
            context.checkpoint()
            return "stale-success"

        self.manager.invalidate_scope(project_generation=3, timeline_revision=1)
        handle = self.manager.submit(
            "analysis",
            work,
            project_generation=3,
            timeline_revision=1,
            on_success=lambda value: callbacks.append(("success", value)),
            on_cancelled=lambda: callbacks.append(("cancelled", None)),
            on_done=lambda status: callbacks.append(("done", status)),
        )
        self.assertTrue(ready.wait(1.0))
        self.manager.invalidate_scope(project_generation=3, timeline_revision=2)
        release.set()
        self._finish(handle)
        self.assertEqual(callbacks, [("cancelled", None), ("done", "cancelled")])


if __name__ == "__main__":
    unittest.main()
