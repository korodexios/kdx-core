"""Periodic tasks: declarative recurring work on the chassis clock.

A module that needs recurring work used to write its own thread, its own stop
flag, and its own join at shutdown - three pieces of bookkeeping the chassis
already owns. Here a module declares an interval and a callback instead: the
scheduler decides when it is due, runs it on the chassis clock, keeps a failure
from ending the tick loop, and stops cleanly inside core.lifecycle.stop_timeout.

Two execution models share the same bookkeeping. The default is tick-driven: the
chassis tick loop calls run_due(), so a periodic task costs no thread and stays
deterministic under test. Threaded mode (core.scheduler.threaded) runs the same
due-check on one named daemon thread for a chassis whose tick is slow or absent.
"""

from __future__ import annotations

import contextlib
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "MAX_ERROR_LENGTH",
    "MAX_NAME_SUFFIX",
    "TASK_FAILED_EVENT",
    "TASK_RUN_EVENT",
    "ScheduledTask",
    "TaskScheduler",
]

TASK_RUN_EVENT = "core.task.run"
TASK_FAILED_EVENT = "core.task.failed"
DEFAULT_POLL_INTERVAL: float = 0.05
MAX_ERROR_LENGTH: Final[int] = 500
"""Longest task error the scheduler keeps: the traceback belongs in the log."""
MAX_NAME_SUFFIX: Final[int] = 1000
"""How far task-name suffixes are numbered before the scheduler calls it a leak."""
_NAME_PATTERN = re.compile(r"[^a-z0-9_.-]+")


def truncate_error(text: str, limit: int = MAX_ERROR_LENGTH) -> str:
    """Return *text* capped at *limit* characters, marked as truncated.

    A task that fails every tick would otherwise carry one ever-longer string in
    every status snapshot and every 'kdx doctor' run; the detail is in the log.
    """
    clean = str(text)
    if len(clean) <= limit:
        return clean
    return clean[: max(1, limit - 1)].rstrip() + "\u2026"


def task_name(value: Any, fallback: str = "task") -> str:
    """Return a safe, readable task name for *value*.

    A task name is a label, not a file name: it shows up in events, in 'kdx
    doctor', and in repr() output, so it is normalised rather than rejected.
    """
    text = str(value or "").strip()
    if not text:
        return fallback
    cleaned = _NAME_PATTERN.sub("_", text).strip("_")
    return cleaned or fallback


@dataclass(slots=True)
class ScheduledTask:
    """One declared periodic task and everything known about its runs."""

    name: str
    interval_seconds: float
    callback: Callable[[], Any]
    repeat: bool = True
    first_delay: float | None = None
    module: str = ""
    next_due: float = 0.0
    runs: int = 0
    failures: int = 0
    last_run: float | None = None
    last_duration_ms: float = 0.0
    last_error: str = ""
    enabled: bool = True
    done: bool = False

    def __post_init__(self) -> None:
        self.last_error = truncate_error(str(self.last_error))
        if not str(self.name).strip():
            raise ValueError("a scheduled task needs a name")
        if not callable(self.callback):
            raise ValueError(f"task {self.name!r} needs a callable callback")
        if float(self.interval_seconds) <= 0:
            raise ValueError(
                f"task {self.name!r} needs a positive interval_seconds, got {self.interval_seconds}"
            )

    def record_error(self, text: str) -> str:
        """Store *text*, capped, as this task's last error and return what was kept."""
        self.last_error = truncate_error(text)
        return self.last_error

    @property
    def overdue(self) -> bool:
        """Return True when the task is due right now (not done, not disabled)."""
        return bool(self.enabled) and not self.done

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view shown by 'kdx doctor' and status."""
        return {
            "name": self.name,
            "interval_seconds": round(float(self.interval_seconds), 6),
            "repeat": bool(self.repeat),
            "runs": int(self.runs),
            "failures": int(self.failures),
            "last_run": round(self.last_run, 3) if self.last_run is not None else None,
            "last_duration_ms": round(float(self.last_duration_ms), 3),
            "last_error": self.last_error,
            "enabled": bool(self.enabled),
            "done": bool(self.done),
            "module": self.module,
        }


class TaskScheduler:
    """Runs declared periodic callbacks on a monotonic clock, with clean stop.

    Every method that mutates tasks is safe to call from a module's init(),
    start(), or a control-plane route: the scheduler owns no thread unless it is
    asked to, so a module can declare work before the chassis decides how to run
    it. Callback failures are counted, logged, and published - never re-raised -
    because one bad task must not take the tick loop down with it.
    """

    def __init__(
        self,
        *,
        logger: Any = None,
        bus: Any = None,
        clock: Callable[[], float] = time.monotonic,
        enabled: bool = True,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        source: str = "scheduler",
    ) -> None:
        self._logger = logger
        self._bus = bus
        self._clock = clock
        self._enabled = bool(enabled)
        self._poll = max(0.01, float(poll_interval))
        self._source = source
        self._tasks: dict[str, ScheduledTask] = {}
        self._order: list[str] = []
        self._bases: dict[str, str] = {}
        self._suffixes: dict[str, int] = {}
        self._guard = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False
        self._started_at: float | None = None
        self._active_task: str | None = None
        self._active_since: float | None = None

    @property
    def enabled(self) -> bool:
        """Return True when this scheduler answers run_due() at all."""
        return self._enabled

    @property
    def running(self) -> bool:
        """Return True between start() and stop()."""
        return self._started

    @property
    def thread_alive(self) -> bool:
        """Return True while the optional scheduler thread is serving."""
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    @property
    def active_task(self) -> str | None:
        """Return the name of the callback running right now, None when idle.

        A task name is the answer an operator wants at shutdown: the tick loop and
        the scheduler thread are healthy, so the only thing that can hold a stop open
        is the callback already on a worker and impossible to recall.
        """
        return self._active_task

    def active_seconds(self) -> float:
        """Return how long the active callback has been running, 0.0 when idle."""
        started = self._active_since
        if started is None:
            return 0.0
        return max(0.0, time.monotonic() - started)

    def schedule(
        self,
        interval_seconds: float,
        callback: Callable[[], Any],
        *,
        name: str = "",
        first_delay: float | None = None,
        repeat: bool = True,
        module: str = "",
        run_now: bool = False,
    ) -> ScheduledTask:
        """Declare a task running every *interval_seconds*.

        The name defaults to the callback's own - 'flush' for flush() - and is made
        unique with a numeric suffix, so two modules can both schedule 'sweep'
        without one silently replacing the other. The first run lands one interval
        out, so booting never opens with a burst; pass `first_delay=0.0` (or
        `run_now=True`) to run immediately, and `first_delay=30` to hold off.
        Intervals are always measured from the run that just happened.
        """
        if not callable(callback):
            raise TypeError(
                f"schedule({interval_seconds}, callback) needs a callable, "
                f"got {type(callback).__name__}"
            )
        interval = float(interval_seconds)
        if interval <= 0:
            raise ValueError(f"interval_seconds must be positive, got {interval_seconds!r}")
        delay = interval if first_delay is None else max(0.0, float(first_delay))
        if run_now:
            delay = 0.0
        now = float(self._clock())
        with self._guard:
            base = task_name(name or getattr(callback, "__name__", "")) or "task"
            key = base if base not in self._tasks else self._next_suffix(base)
            task = ScheduledTask(
                name=key,
                interval_seconds=interval,
                callback=callback,
                repeat=repeat,
                first_delay=None if first_delay is None else delay,
                module=module,
                next_due=now + delay,
            )
            self._tasks[key] = task
            self._bases[key] = base
            self._order.append(key)
        if self._logger is not None:
            self._logger.debug(
                "task scheduled",
                name=key,
                interval_seconds=round(interval, 6),
                repeat=bool(repeat),
                module=module or None,
            )
        return task

    def unschedule(self, name: str) -> bool:
        """Withdraw the task named *name*; return whether one existed.

        Every internal record of that task goes with it - the task table, the
        declaration order, and its entry in the suffix bookkeeping - so the tables
        stay the size of the work that is actually scheduled. Calling it from a task
        callback, including on the callback's own name, is safe: a running sweep
        already works from a snapshot of what was due. A suffix this
        scheduler handed out is never recycled while another task of the same base
        is running, so 'sweep-2' in an old log line never names a new task.
        """
        cleaned = str(name).strip()
        with self._guard:
            existed = self._tasks.pop(cleaned, None) is not None
            if existed:
                with contextlib.suppress(ValueError):
                    self._order.remove(cleaned)
                base = self._bases.pop(cleaned, "")
                if base and base not in set(self._bases.values()):
                    self._suffixes.pop(base, None)
        return existed

    cancel = unschedule

    def _next_suffix(self, base: str) -> str:
        """Return a free base-N key, never reusing a number already issued.

        Called with the guard held. The scan is bounded by the tasks that are
        actually scheduled; past MAX_NAME_SUFFIX the scheduler calls it a leak
        rather than numbering names into the millions.
        """
        suffix = self._suffixes.get(base, 1) + 1
        while suffix <= MAX_NAME_SUFFIX:
            candidate = f"{base}-{suffix}"
            if candidate not in self._tasks:
                self._suffixes[base] = suffix
                return candidate
            suffix += 1
        raise ValueError(
            f"a {base!r} task number above {MAX_NAME_SUFFIX} is a leak: unschedule one first"
        )

    def task(self, name: str) -> ScheduledTask | None:
        """Return the task named *name*, or None."""
        with self._guard:
            return self._tasks.get(str(name).strip())

    def tasks(self) -> tuple[ScheduledTask, ...]:
        """Return a snapshot of every declared task, in declaration order.

        The copy is taken with the guard held, so a reader on the CLI thread and a
        sweep on the scheduler thread never watch `_order` change size underneath them.
        """
        with self._guard:
            return tuple(self._tasks[key] for key in self._order if key in self._tasks)

    def pending(self, *, now: float | None = None) -> list[ScheduledTask]:
        """Return the tasks due at *now*; the scheduler's clock decides by default."""
        moment = float(self._clock() if now is None else now)
        return [
            task
            for task in self.tasks()
            if task.enabled and not task.done and task.next_due <= moment
        ]

    def next_due_in(self, *, now: float | None = None) -> float | None:
        """Return seconds until the soonest due task, or None when nothing is due."""
        moments = [
            task.next_due
            for task in self.tasks()
            if task.enabled and not task.done
        ]
        if not moments:
            return None
        moment = float(self._clock() if now is None else now)
        return max(0.0, min(moments) - moment)

    def run_due(self, *, now: float | None = None) -> list[str]:
        """Run every task due at *now*; return the names that ran.

        The clock is sampled once and the due list is a snapshot taken with the
        guard held, so a slow callback cannot make its own task look overdue twice
        and a table that changes size mid-sweep cannot skip a task. Callbacks run
        with the guard released: a task may schedule another one, unschedule its
        neighbour, or withdraw its own name - and this sweep honours that decision
        instead of running a task that is already gone. Shutdown is honoured the
        same way: once stop() has set the flag, this sweep stops launching callbacks
        and leaves the rest of the queue alone, so a stop is never answered by new
        work the shutdown budget cannot wait for.
        """
        if not self._enabled:
            return []
        moment = float(self._clock() if now is None else now)
        due = self.pending(now=moment)
        ran: list[str] = []
        for task in due:
            if self._stop.is_set():
                break
            if self.task(task.name) is not task:
                # An earlier callback withdrew this task, or replaced it under the
                # same name: the withdrawal is a decision, not a missed beat.
                continue
            if self._stop.is_set():
                # Shutdown commenced while this task waited its turn: launching a
                # fresh callback now would start work the stop() budget cannot wait
                # for, so the sweep stops here with the queue still full.
                break
            if self.run_task(task.name, now=moment):
                ran.append(task.name)
        return ran

    def run_task(self, name: str, *, now: float | None = None) -> bool:
        """Run one task by name right now, ignoring the clock; return True on success.

        This is also the manual trigger a CLI or a control-plane route uses to ask
        for work immediately, which is why the clock is optional here. The task is
        looked up under the guard and then called with it released, so the callback
        may take scheduler decisions of its own.
        """
        task = self.task(name)
        if task is None:
            return False
        if not self._enabled or task.done:
            return False
        moment = float(self._clock() if now is None else now)
        started = time.monotonic()
        self._active_task = task.name
        self._active_since = started
        try:
            task.callback()
        except Exception as exc:
            task.failures += 1
            task.record_error(f"{type(exc).__name__}: {exc}")
            task.last_run = moment
            task.last_duration_ms = (time.monotonic() - started) * 1000.0
            task.next_due = moment + float(task.interval_seconds)
            if not task.repeat:
                task.done = True
                task.enabled = False
            self._log_task_failure(task)
            self._publish(TASK_FAILED_EVENT, task)
            return False
        finally:
            self._active_task = None
            self._active_since = None
        task.runs += 1
        task.last_run = moment
        task.last_error = ""
        task.last_duration_ms = (time.monotonic() - started) * 1000.0
        task.next_due = moment + float(task.interval_seconds)
        if not task.repeat:
            task.done = True
            task.enabled = False
        if self._logger is not None:
            self._logger.debug(
                "task ran",
                name=task.name,
                runs=task.runs,
                duration_ms=round(task.last_duration_ms, 3),
            )
        self._publish(TASK_RUN_EVENT, task)
        return True

    def start(self, *, threaded: bool | None = None) -> TaskScheduler:
        """Open the scheduler; join the tick loop, or take a thread when asked.

        Starting is idempotent. Threaded mode names its thread 'kdx-scheduler' so
        a stack dump says who owns it, and the loop only ever calls run_due() - the
        intervals, the failures, and the stop logic all live in one place.
        """
        if self._started:
            return self
        self._started = True
        self._started_at = time.monotonic()
        self._stop.clear()
        if threaded:
            self._thread = threading.Thread(
                target=self._loop, name="kdx-scheduler", daemon=True
            )
            self._thread.start()
        if self._logger is not None:
            self._logger.debug(
                "scheduler started",
                tasks=len(self._tasks),
                threaded=bool(threaded),
                enabled=self._enabled,
            )
        return self

    def _loop(self) -> None:
        """Poll the due list on the scheduler thread until stop() fires."""
        while not self._stop.is_set():
            wait_for = self.next_due_in()
            if wait_for is None:
                wait_for = self._poll
            self._stop.wait(max(0.005, min(float(wait_for), self._poll * 4)))
            if self._stop.is_set():
                break
            self.run_due()

    def stop(self, timeout: float | None = None) -> None:
        """Stop the scheduler and join its thread; the caller owns the deadline.

        The chassis passes core.lifecycle.stop_timeout, so a periodic task that
        outlives its budget is reported, not allowed to hold the process open.
        """
        if not self._started:
            return
        logger = self._logger
        thread = self._thread
        self._thread = None
        self._stop.set()
        self._started = False
        if thread is not None and thread.is_alive():
            budget = max(0.0, float(timeout)) if timeout else self._poll
            thread.join(timeout=budget)
            if thread.is_alive() and logger is not None:
                # The thread is alive because a callback is. Naming that callback, and
                # how long it has been running, is what turns "shutdown was slow" into
                # a task an operator can read in 'kdx doctor' and act on.
                active = self._active_task
                if active is None:
                    logger.warning(
                        "scheduler thread outlived the stop timeout with no task running",
                        name=thread.name,
                        timeout=timeout,
                        active_task=None,
                    )
                else:
                    logger.warning(
                        f"scheduler task {active!r} outlived the stop timeout",
                        name=thread.name,
                        task=active,
                        running_seconds=round(self.active_seconds(), 3),
                        timeout=timeout,
                    )
        if logger is not None:
            logger.debug(
                "scheduler stopped", runs=self.total_runs(), failures=self.total_failures()
            )

    def stats(self) -> dict[str, Any]:
        """Return the scheduler's self-report for health and status payloads."""
        return {
            "enabled": self._enabled,
            "running": self._started,
            "threaded": self.thread_alive,
            "scheduled": len(self._tasks),
            "active_task": self._active_task,
            "runs": self.total_runs(),
            "failures": self.total_failures(),
            "uptime_seconds": round(max(0.0, time.monotonic() - self._started_at), 3)
            if self._started_at is not None
            else 0.0,
            "tasks": [task.as_dict() for task in self.tasks()],
        }

    as_dict = stats

    def describe(self) -> str:
        """Return the one-line summary shown by 'kdx doctor'."""
        tasks = self.tasks()
        if not tasks:
            return "no scheduled tasks"
        parts = [
            f"{task.name} every {task.interval_seconds:g}s ({task.runs} run(s))"
            for task in tasks
        ]
        return f"{len(tasks)} scheduled task(s): " + ", ".join(parts)

    def failing_tasks(self) -> tuple[ScheduledTask, ...]:
        """Return the tasks with at least one failed run."""
        return tuple(task for task in self.tasks() if task.failures)

    def total_runs(self) -> int:
        """Return the runs across every declared task."""
        return int(sum(task.runs for task in self.tasks()))

    def total_failures(self) -> int:
        """Return the failures across every declared task."""
        return int(sum(task.failures for task in self.tasks()))

    def _publish(self, event: str, task: ScheduledTask) -> None:
        bus = self._bus
        if bus is None:
            return
        bus.publish(event, task.as_dict(), source=self._source)

    def _log_task_failure(self, task: ScheduledTask) -> None:
        if self._logger is None:
            return
        self._logger.warning(
            f"scheduled task {task.name!r} failed: {task.last_error}",
            name=task.name,
            failures=task.failures,
        )

    def __len__(self) -> int:
        return len(self._tasks)

    def __contains__(self, name: object) -> bool:
        return str(name).strip() in self._tasks

    def __repr__(self) -> str:
        return (
            f"TaskScheduler(tasks={list(self._tasks)}, running={self._started}, "
            f"enabled={self._enabled})"
        )
