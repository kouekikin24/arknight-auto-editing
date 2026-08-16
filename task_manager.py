"""Shared background-task lifecycle for the Tk application.

Workers never call Tk. They publish results into this manager, and the owner
thread dispatches callbacks from a periodic ``after`` poll. Each task kind has
a generation so a replaced task cannot overwrite newer UI state.
"""
from __future__ import annotations

from concurrent.futures import CancelledError, Future
from contextlib import contextmanager
from dataclasses import dataclass, field
from queue import Empty, Queue
import itertools
import threading
import time
import traceback
from typing import Any, Callable, Generic, TypeVar


ResultT = TypeVar("ResultT")
SuccessCallback = Callable[[Any], None]
ErrorCallback = Callable[[BaseException], None]
ProgressCallback = Callable[[Any], None]
CancelledCallback = Callable[[], None]
DoneCallback = Callable[[str], None]
_MISSING = object()


class TaskCancelled(RuntimeError):
    """Cooperative cancellation raised inside a task worker."""


@dataclass(slots=True)
class TaskHandle(Generic[ResultT]):
    task_id: int
    kind: str
    generation: int
    project_generation: int | None
    timeline_revision: int | None
    cancel_event: threading.Event = field(repr=False)
    future: Future[ResultT] | None = field(default=None, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)
    final_committed: bool = field(default=False, init=False)

    def cancel(self) -> None:
        self.cancel_event.set()
        if self.future is not None:
            self.future.cancel()

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set() or bool(
            self.future is not None and self.future.cancelled()
        )


class TaskContext:
    """Worker-facing cancellation and progress API."""

    __slots__ = ("_manager", "_handle")

    def __init__(self, manager: "TaskManager", handle: TaskHandle[Any]):
        self._manager = manager
        self._handle = handle

    @property
    def task_id(self) -> int:
        return self._handle.task_id

    @property
    def kind(self) -> str:
        return self._handle.kind

    @property
    def generation(self) -> int:
        return self._handle.generation

    @property
    def project_generation(self) -> int | None:
        return self._handle.project_generation

    @property
    def timeline_revision(self) -> int | None:
        return self._handle.timeline_revision

    @property
    def cancelled(self) -> bool:
        return self._handle.cancel_event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise TaskCancelled(
                f"task {self.kind!r} generation {self.generation} was cancelled"
            )

    # Short aliases make worker code readable and preserve the terminology in
    # the handoff notes.
    checkpoint = raise_if_cancelled

    def report_progress(self, value: Any) -> None:
        self.raise_if_cancelled()
        self._manager._record_progress(self._handle, value)

    report = report_progress

    def commit(
        self,
        action: Callable[..., ResultT],
        *args,
        final: bool = False,
        **kwargs,
    ) -> ResultT:
        """Run a short final publish only while this task still owns its scope."""
        return self._manager._commit(
            self._handle, action, *args, final=final, **kwargs
        )


@dataclass(slots=True)
class _Callbacks:
    on_success: SuccessCallback | None
    on_error: ErrorCallback | None
    on_progress: ProgressCallback | None
    on_cancelled: CancelledCallback | None
    on_done: DoneCallback | None


@dataclass(slots=True)
class _Completion:
    handle: TaskHandle[Any]
    status: str
    payload: Any = None


class TaskManager:
    """Own daemon task threads with bounded concurrent work.

    ``max_workers`` bounds work executing inside ``work(context)``.  Each
    submitted task still owns a short-lived daemon thread so shutdown can use
    bounded joins and report non-cooperative survivors without keeping the
    Python process alive.
    """

    def __init__(
        self,
        scheduler: Any | None = None,
        *,
        max_workers: int = 2,
        poll_interval_ms: int = 50,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        if poll_interval_ms < 1:
            raise ValueError("poll_interval_ms must be at least 1")
        self._scheduler = scheduler
        self._poll_interval_ms = int(poll_interval_ms)
        self._slots = threading.BoundedSemaphore(max_workers)
        self._max_workers = int(max_workers)
        self._lock = threading.RLock()
        self._completions: Queue[_Completion] = Queue()
        self._posted: Queue[tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any]]] = Queue()
        self._progress: dict[tuple[str, int], Any] = {}
        self._active: dict[str, TaskHandle[Any]] = {}
        self._handles: dict[int, TaskHandle[Any]] = {}
        self._callbacks: dict[int, _Callbacks] = {}
        self._generations: dict[str, int] = {}
        self._ids = itertools.count(1)
        self._threads: set[threading.Thread] = set()
        self._closed = False
        self._after_id: Any | None = None
        self._last_survivors: tuple[str, ...] = ()
        self._current_scope: tuple[int, int] | None = None
        if scheduler is not None:
            self._schedule_poll()

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def max_workers(self) -> int:
        return self._max_workers

    def current_generation(self, kind: str) -> int:
        kind = self._normalize_kind(kind)
        with self._lock:
            return self._generations.get(kind, 0)

    def submit(
        self,
        kind: str,
        work: Callable[[TaskContext], ResultT],
        *,
        on_success: SuccessCallback | None = None,
        on_error: ErrorCallback | None = None,
        on_progress: ProgressCallback | None = None,
        on_cancelled: CancelledCallback | None = None,
        on_done: DoneCallback | None = None,
        project_generation: int | None = None,
        timeline_revision: int | None = None,
        replace: bool = True,
    ) -> TaskHandle[ResultT]:
        kind = self._normalize_kind(kind)
        if not callable(work):
            raise TypeError("work must be callable")

        with self._lock:
            if self._closed:
                raise RuntimeError("TaskManager is closed")
            if (
                project_generation is not None
                and self._current_scope is not None
                and (
                    int(project_generation) != self._current_scope[0]
                    or (
                        timeline_revision is not None
                        and int(timeline_revision) != self._current_scope[1]
                    )
                )
            ):
                raise RuntimeError(
                    "task scope is stale before submission: "
                    f"got ({project_generation}, {timeline_revision}), "
                    f"current is {self._current_scope}"
                )
            previous = self._active.get(kind)
            if previous is not None and not replace:
                raise RuntimeError(f"task kind {kind!r} is already active")
            if previous is not None:
                self._forget_handle_locked(previous, remove_active=False)
                previous.cancel()
            generation = self._generations.get(kind, 0) + 1
            self._generations[kind] = generation
            handle: TaskHandle[ResultT] = TaskHandle(
                task_id=next(self._ids),
                kind=kind,
                generation=generation,
                project_generation=project_generation,
                timeline_revision=timeline_revision,
                cancel_event=threading.Event(),
            )
            future: Future[ResultT] = Future()
            handle.future = future
            future.add_done_callback(
                lambda completed, task=handle: self._capture_completion(
                    task, completed
                )
            )
            self._active[kind] = handle
            self._handles[handle.task_id] = handle
            self._callbacks[handle.task_id] = _Callbacks(
                on_success=on_success,
                on_error=on_error,
                on_progress=on_progress,
                on_cancelled=on_cancelled,
                on_done=on_done,
            )

            thread = threading.Thread(
                target=self._run_worker,
                args=(handle, work),
                name=f"arknight-task-{kind}-{generation}",
                daemon=True,
            )
            handle.thread = thread
            self._threads.add(thread)
            try:
                thread.start()
            except BaseException:
                self._forget_handle_locked(handle, remove_active=True)
                self._threads.discard(thread)
                handle.cancel()
                raise
        return handle

    def invalidate_scope(
        self,
        *,
        project_generation: int,
        timeline_revision: int,
    ) -> tuple[int, ...]:
        """Cancel active tasks whose bound project snapshot is now stale.

        Unscoped infrastructure tasks (for example the GPU probe) are left
        alone.  Call this whenever the owner publishes a new project/timeline
        snapshot; it centralizes the stale-result contract across task kinds.
        """
        project_generation = int(project_generation)
        timeline_revision = int(timeline_revision)
        invalidated = []
        with self._lock:
            self._current_scope = (project_generation, timeline_revision)
            for handle in tuple(self._active.values()):
                if handle.project_generation is None:
                    continue
                stale = handle.project_generation != project_generation
                if (
                    not stale
                    and handle.timeline_revision is not None
                    and handle.timeline_revision != timeline_revision
                ):
                    stale = True
                if stale:
                    if not handle.final_committed:
                        handle.cancel()
                        invalidated.append(handle.task_id)
        return tuple(sorted(invalidated))

    @contextmanager
    def scope_transition(self):
        """Serialize owner state changes with scoped worker final commits."""
        with self._lock:
            if self._closed:
                raise RuntimeError("TaskManager is closed")
            yield

    def cancel(self, kind: str) -> bool:
        kind = self._normalize_kind(kind)
        with self._lock:
            handle = self._active.get(kind)
            if handle is None:
                return False
            handle.cancel()
            return True

    def invalidate(self, kind: str) -> int:
        """Invalidate a kind even when no replacement task is submitted."""
        kind = self._normalize_kind(kind)
        with self._lock:
            generation = self._generations.get(kind, 0) + 1
            self._generations[kind] = generation
            handle = self._active.pop(kind, None)
            if handle is not None:
                handle.cancel()
                self._forget_handle_locked(handle, remove_active=False)
            return generation

    def cancel_all(self) -> None:
        with self._lock:
            for handle in tuple(self._handles.values()):
                handle.cancel()

    def post(
        self,
        callback: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> bool:
        """Queue a callback for the owner thread without calling Tk directly."""
        if not callable(callback):
            raise TypeError("callback must be callable")
        with self._lock:
            if self._closed:
                return False
            self._posted.put((callback, args, kwargs))
            return True

    def dispatch_pending(self) -> int:
        """Run current callbacks; call this only from the Tk owner thread."""
        delivered = self._dispatch_posted()
        self._dispatch_progress()
        while True:
            try:
                completion = self._completions.get_nowait()
            except Empty:
                break

            handle = completion.handle
            with self._lock:
                callbacks = self._callbacks.pop(handle.task_id, None)
                current = self._active.get(handle.kind)
                is_current = current is handle and not self._closed
                self._handles.pop(handle.task_id, None)
                final_progress = self._progress.pop(
                    (handle.kind, handle.generation), _MISSING
                )

            if not is_current or callbacks is None:
                continue
            if final_progress is not _MISSING and not handle.cancel_event.is_set():
                self._safe_callback(callbacks.on_progress, final_progress)
            if not self._is_current(handle):
                continue

            status = (
                "cancelled"
                if handle.cancel_event.is_set()
                else completion.status
            )
            if status == "success":
                callback_error = self._call_callback(
                    callbacks.on_success, completion.payload
                )
                if callback_error is not None:
                    status = "error"
                    if callbacks.on_error is None:
                        self._report_callback_exception(callback_error)
                    else:
                        self._safe_callback(callbacks.on_error, callback_error)
            elif status == "error":
                self._safe_callback(callbacks.on_error, completion.payload)
            elif status == "cancelled":
                self._safe_callback(callbacks.on_cancelled)
            if self._is_current(handle):
                self._safe_callback(callbacks.on_done, status)
            with self._lock:
                if self._active.get(handle.kind) is handle:
                    self._active.pop(handle.kind, None)
            delivered += 1
        return delivered

    def close(
        self,
        *,
        timeout: float = 2.0,
        wait: bool | None = None,
    ) -> list[str]:
        """Cancel work, stop polling, and join workers up to one deadline.

        ``wait`` is retained as a compatibility convenience: ``wait=True``
        means an unbounded join and ``wait=False`` means no join. New callers
        should use ``timeout`` and inspect the returned survivor names.
        """
        if wait is not None:
            timeout = None if wait else 0.0
        first_close = False
        handles: tuple[TaskHandle[Any], ...] = ()
        after_id: Any | None = None
        scheduler: Any | None = None
        with self._lock:
            if not self._closed:
                first_close = True
                self._closed = True
                handles = tuple(self._handles.values())
                self._active.clear()
                self._handles.clear()
                self._progress.clear()
                self._callbacks.clear()
                after_id = self._after_id
                self._after_id = None
                scheduler = self._scheduler
                self._scheduler = None
            threads = tuple(self._threads)
        if first_close:
            for handle in handles:
                handle.cancel()
            self._drain_completions()
            self._drain_posted()
        if after_id is not None and scheduler is not None:
            try:
                scheduler.after_cancel(after_id)
            except Exception:
                pass

        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        current_thread = threading.current_thread()
        for thread_obj in threads:
            if thread_obj is current_thread or not thread_obj.is_alive():
                continue
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            thread_obj.join(remaining)
        self._drain_completions()
        self._drain_posted()
        with self._lock:
            survivors = tuple(
                sorted(
                    thread.name
                    for thread in self._threads
                    if thread.is_alive()
                )
            )
            self._last_survivors = survivors
        return list(survivors)

    def _run_worker(
        self,
        handle: TaskHandle[ResultT],
        work: Callable[[TaskContext], ResultT],
    ) -> None:
        thread = threading.current_thread()
        acquired = False
        try:
            while not acquired:
                if handle.cancel_event.is_set():
                    handle.future.cancel()
                    return
                acquired = self._slots.acquire(timeout=0.05)
            if not handle.future.set_running_or_notify_cancel():
                return
            context = TaskContext(self, handle)
            context.raise_if_cancelled()
            result = work(context)
            handle.future.set_result(result)
        except BaseException as exc:
            if not handle.future.done():
                handle.future.set_exception(exc)
        finally:
            if acquired:
                self._slots.release()
            with self._lock:
                self._threads.discard(thread)

    def _capture_completion(
        self,
        handle: TaskHandle[Any],
        future: Future[Any],
    ) -> None:
        try:
            payload = future.result()
            status = "cancelled" if handle.cancel_event.is_set() else "success"
            if status == "cancelled":
                payload = None
        except (CancelledError, TaskCancelled):
            status = "cancelled"
            payload = None
        except BaseException as exc:
            status = "error"
            payload = exc
        with self._lock:
            current = self._active.get(handle.kind)
            deliverable = (
                not self._closed
                and current is handle
                and handle.task_id in self._callbacks
            )
            if not deliverable:
                self._forget_handle_locked(handle, remove_active=False)
                return
            self._completions.put(_Completion(handle, status, payload))

    def _record_progress(self, handle: TaskHandle[Any], value: Any) -> None:
        with self._lock:
            if self._closed or self._active.get(handle.kind) is not handle:
                return
            self._progress[(handle.kind, handle.generation)] = value

    def _is_scope_stale_locked(self, handle: TaskHandle[Any]) -> bool:
        scope = self._current_scope
        if scope is None or handle.project_generation is None:
            return False
        if handle.project_generation != scope[0]:
            return True
        return (
            handle.timeline_revision is not None
            and handle.timeline_revision != scope[1]
        )

    def _commit(
        self,
        handle: TaskHandle[Any],
        action: Callable[..., ResultT],
        *args: Any,
        final: bool = False,
        **kwargs: Any,
    ) -> ResultT:
        """Atomically revalidate task ownership and perform its final publish."""
        if not callable(action):
            raise TypeError("commit action must be callable")
        with self._lock:
            if (
                self._closed
                or handle.cancel_event.is_set()
                or self._active.get(handle.kind) is not handle
                or self._is_scope_stale_locked(handle)
            ):
                handle.cancel()
                raise TaskCancelled(
                    f"task {handle.kind!r} generation {handle.generation} "
                    "cannot commit stale output"
                )
            result = action(*args, **kwargs)
            if final:
                handle.final_committed = True
            return result

    def _is_current(self, handle: TaskHandle[Any]) -> bool:
        with self._lock:
            return not self._closed and self._active.get(handle.kind) is handle

    @staticmethod
    def _normalize_kind(kind: str) -> str:
        normalized = str(kind).strip()
        if not normalized:
            raise ValueError("task kind must be non-empty")
        return normalized

    def _forget_handle_locked(
        self,
        handle: TaskHandle[Any],
        *,
        remove_active: bool,
    ) -> None:
        if remove_active and self._active.get(handle.kind) is handle:
            self._active.pop(handle.kind, None)
        self._handles.pop(handle.task_id, None)
        self._callbacks.pop(handle.task_id, None)
        self._progress.pop((handle.kind, handle.generation), None)

    def _drain_completions(self) -> None:
        while True:
            try:
                self._completions.get_nowait()
            except Empty:
                return

    def _drain_posted(self) -> None:
        while True:
            try:
                self._posted.get_nowait()
            except Empty:
                return

    def _dispatch_posted(self) -> int:
        delivered = 0
        while True:
            try:
                callback, args, kwargs = self._posted.get_nowait()
            except Empty:
                return delivered
            with self._lock:
                if self._closed:
                    self._drain_posted()
                    return delivered
            self._safe_callback(callback, *args, **kwargs)
            delivered += 1

    def _dispatch_progress(self) -> None:
        with self._lock:
            pending = list(self._progress.items())
            self._progress.clear()
        for (kind, generation), value in pending:
            with self._lock:
                handle = self._active.get(kind)
                callbacks = (
                    self._callbacks.get(handle.task_id)
                    if (
                        handle is not None
                        and handle.generation == generation
                        and not handle.cancel_event.is_set()
                    )
                    else None
                )
            if callbacks is None or handle is None or not self._is_current(handle):
                continue
            self._safe_callback(callbacks.on_progress, value)

    def _safe_callback(
        self,
        callback: Callable[..., Any] | None,
        *args: Any,
        **kwargs: Any,
    ) -> BaseException | None:
        exc = self._call_callback(callback, *args, **kwargs)
        if exc is not None:
            self._report_callback_exception(exc)
        return exc

    @staticmethod
    def _call_callback(
        callback: Callable[..., Any] | None,
        *args: Any,
        **kwargs: Any,
    ) -> BaseException | None:
        if callback is None:
            return None
        try:
            callback(*args, **kwargs)
        except Exception as exc:
            return exc
        return None

    def _report_callback_exception(self, exc: BaseException) -> None:
        scheduler = self._scheduler
        reporter = getattr(scheduler, "report_callback_exception", None)
        if reporter is not None:
            try:
                reporter(type(exc), exc, exc.__traceback__)
                return
            except Exception:
                pass
        traceback.print_exception(type(exc), exc, exc.__traceback__)

    def _schedule_poll(self) -> None:
        with self._lock:
            if self._closed or self._scheduler is None:
                return
            try:
                self._after_id = self._scheduler.after(
                    self._poll_interval_ms,
                    self._poll,
                )
            except Exception:
                self._after_id = None

    def _poll(self) -> None:
        with self._lock:
            self._after_id = None
            if self._closed:
                return
        self.dispatch_pending()
        self._schedule_poll()
