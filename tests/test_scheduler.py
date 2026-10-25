"""Periodic task scheduler: declared intervals, the chassis clock, clean stop."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from korodexios_core.events.bus import EventBus
from korodexios_core.kernel.scheduler import (
    MAX_ERROR_LENGTH,
    MAX_NAME_SUFFIX,
    TASK_FAILED_EVENT,
    TASK_RUN_EVENT,
    ScheduledTask,
    TaskScheduler,
    truncate_error,
)


class FakeClock:
    """A monotonic clock the test advances by hand."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


def make_scheduler(**kwargs: Any) -> tuple[TaskScheduler, FakeClock]:
    """Return a scheduler on a hand-driven clock."""
    clock = FakeClock()
    return TaskScheduler(clock=clock, **kwargs), clock


def test_schedule_names_the_callback_and_defers_the_first_run() -> None:
    scheduler, clock = make_scheduler()

    def flush() -> None:
        return None

    task = scheduler.schedule(2.0, flush)
    assert task.name == "flush"
    assert task.interval_seconds == 2.0
    assert task.next_due == clock.now + 2.0
    assert task.runs == 0
    assert scheduler.schedule(1.0, flush).name == "flush-2"
    assert scheduler.schedule(1.0, flush, name="flush").name == "flush-3"


def test_schedule_rejects_a_dead_interval_or_callback() -> None:
    scheduler, _clock = make_scheduler()
    for interval in (0, -1.0, 0.0):
        with pytest.raises(ValueError):
            scheduler.schedule(interval, lambda: None)
    with pytest.raises(TypeError):
        scheduler.schedule(1.0, "not callable")  # type: ignore[arg-type]


def test_run_due_fires_once_per_interval_and_measures_duration() -> None:
    hits: list[float] = []
    scheduler, clock = make_scheduler()
    task = scheduler.schedule(1.0, lambda: hits.append(clock.now))
    clock.advance(0.5)
    assert scheduler.run_due() == []
    assert hits == []
    clock.advance(0.5)
    assert scheduler.run_due() == [task.name]
    clock.advance(0.5)
    assert scheduler.run_due() == []
    clock.advance(1.0)
    assert scheduler.run_due() == [task.name]
    assert hits == [1.0, 2.5]
    assert task.runs == 2
    assert task.next_due == 3.5
    assert task.last_duration_ms >= 0.0
    assert task.last_error == ""


def test_first_delay_and_run_now_change_only_the_first_fire() -> None:
    scheduler, _clock = make_scheduler()
    later = scheduler.schedule(1.0, lambda: None, name="later", first_delay=30.0)
    immediate = scheduler.schedule(1.0, lambda: None, name="now", run_now=True)
    assert later.next_due == 30.0
    assert immediate.next_due == 0.0
    assert scheduler.run_due() == ["now"]
    assert scheduler.next_due_in() == 1.0
    assert later.next_due == 30.0


def test_repeat_false_runs_once_and_parks_the_task() -> None:
    calls: list[int] = []
    scheduler, clock = make_scheduler()
    task = scheduler.schedule(1.0, lambda: calls.append(1), name="once", repeat=False)
    clock.advance(1.0)
    assert scheduler.run_due() == ["once"]
    assert scheduler.run_due() == []
    clock.advance(5.0)
    assert scheduler.run_due() == []
    assert task.done is True
    assert task.enabled is False
    assert calls == [1]


def test_a_failing_task_is_counted_and_never_breaks_the_loop() -> None:
    events: list[str] = []
    bus = EventBus()
    bus.subscribe(TASK_RUN_EVENT, lambda event: events.append("run"))
    bus.subscribe(TASK_FAILED_EVENT, lambda event: events.append("failed"))

    def boom() -> None:
        raise RuntimeError("nope")

    good_calls: list[int] = []
    scheduler, clock = make_scheduler(bus=bus)
    good = scheduler.schedule(1.0, lambda: good_calls.append(1), name="good")
    bad = scheduler.schedule(1.0, boom, name="bad")
    clock.advance(1.0)
    ran = scheduler.run_due()
    assert ran == ["good"]
    assert bad.failures == 1
    assert bad.last_error == "RuntimeError: nope"
    assert bad.runs == 0
    assert good.runs == 1
    clock.advance(1.0)
    scheduler.run_due()
    assert bad.failures == 2
    assert events.count("failed") == 2
    assert scheduler.failing_tasks() == (bad,)
    assert scheduler.total_runs() == 2
    assert scheduler.total_failures() == 2


def test_unschedule_withdraws_a_task_and_reports_whether_it_existed() -> None:
    scheduler, _clock = make_scheduler()
    scheduler.schedule(1.0, lambda: None, name="alpha")
    assert scheduler.unschedule("alpha") is True
    assert scheduler.unschedule("alpha") is False
    assert scheduler.tasks() == ()
    assert "alpha" not in scheduler


def test_run_task_is_the_manual_trigger_a_route_or_cli_uses() -> None:
    calls: list[int] = []
    scheduler, _clock = make_scheduler()
    scheduler.schedule(100.0, lambda: calls.append(1), name="sweep")
    assert scheduler.run_task("sweep") is True
    assert scheduler.run_task("sweep") is True
    assert calls == [1, 1]
    assert scheduler.run_task("missing") is False


def test_a_disabled_scheduler_runs_nothing() -> None:
    scheduler, clock = make_scheduler(enabled=False)
    scheduler.schedule(1.0, lambda: None, name="quiet")
    clock.advance(10.0)
    assert scheduler.run_due() == []
    assert scheduler.run_task("quiet") is False


def test_stats_and_describe_report_the_declared_work() -> None:
    scheduler, clock = make_scheduler()
    assert scheduler.describe() == "no scheduled tasks"
    assert scheduler.stats()["scheduled"] == 0
    scheduler.schedule(1.5, lambda: None, name="sweep", module="probe")
    clock.advance(1.5)
    scheduler.run_due()
    stats = scheduler.stats()
    assert stats["scheduled"] == 1
    assert stats["runs"] == 1
    assert stats["failures"] == 0
    assert stats["enabled"] is True
    assert stats["tasks"][0]["name"] == "sweep"
    assert stats["tasks"][0]["module"] == "probe"
    assert scheduler.describe() == "1 scheduled task(s): sweep every 1.5s (1 run(s))"


def test_threaded_mode_runs_on_its_own_thread_and_stops_cleanly() -> None:
    calls: list[int] = []
    stop_together = threading.Event()
    scheduler = TaskScheduler(poll_interval=0.02)
    scheduler.schedule(0.03, lambda: calls.append(1))
    scheduler.start(threaded=True)
    assert scheduler.running is True
    assert scheduler.thread_alive is True
    deadline = time.monotonic() + 1.0
    while not calls and time.monotonic() < deadline:
        stop_together.wait(0.02)
    scheduler.stop(timeout=1.0)
    assert calls, "the scheduler thread ran the task"
    assert scheduler.running is False
    assert scheduler.thread_alive is False


def test_stop_is_idempotent_and_survives_a_scheduler_that_never_started() -> None:
    scheduler, _clock = make_scheduler()
    scheduler.stop()
    scheduler.start()
    scheduler.stop()
    scheduler.stop()
    assert scheduler.running is False


def test_task_validation_rejects_a_broken_declaration() -> None:
    with pytest.raises(ValueError):
        ScheduledTask(name="", interval_seconds=1.0, callback=lambda: None)
    with pytest.raises(ValueError):
        ScheduledTask(name="x", interval_seconds=0.0, callback=lambda: None)
    with pytest.raises(ValueError):
        ScheduledTask(name="x", interval_seconds=1.0, callback=42)  # type: ignore[arg-type]


def blow_up() -> None:
    """A callback whose failure text is longer than the scheduler keeps."""
    raise RuntimeError("detail " + "x" * 900)


def test_a_task_error_is_kept_bounded_and_marked() -> None:
    scheduler = TaskScheduler()
    scheduler.schedule(1.0, blow_up, name="index")
    assert scheduler.run_task("index") is False
    task = scheduler.task("index")
    assert task is not None
    assert len(task.last_error) == MAX_ERROR_LENGTH
    assert task.last_error.endswith("\u2026")
    assert task.last_error.startswith("RuntimeError: detail")
    assert truncate_error("short") == "short"
    assert len(truncate_error("y" * (MAX_ERROR_LENGTH + 40))) == MAX_ERROR_LENGTH


def test_unschedule_purges_the_tables_and_never_recycles_a_number() -> None:
    scheduler = TaskScheduler()
    first = scheduler.schedule(1.0, blow_up, name="sweep")
    second = scheduler.schedule(1.0, blow_up, name="sweep")
    third = scheduler.schedule(1.0, blow_up, name="sweep")
    assert [first.name, second.name, third.name] == ["sweep", "sweep-2", "sweep-3"]
    for name in ("sweep", "sweep-2", "sweep-3"):
        assert scheduler.unschedule(name) is True
        assert scheduler.unschedule(name) is False
    assert len(scheduler) == 0
    assert scheduler.tasks() == ()
    assert scheduler._order == []
    assert scheduler._bases == {}
    assert scheduler._suffixes == {}
    revived = scheduler.schedule(1.0, blow_up, name="sweep")
    assert revived.name == "sweep"
    assert scheduler.unschedule("sweep") is True
    kept = scheduler.schedule(1.0, blow_up, name="sweep")
    assert scheduler.unschedule(kept.name) is True
    assert MAX_NAME_SUFFIX >= 100
    assert scheduler.stats()["scheduled"] == 0
