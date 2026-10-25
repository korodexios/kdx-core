"""Chassis-managed asyncio loop, worker supervision, and diagnostics checks."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from korodexios_core.exceptions import ChassisError
from korodexios_core.kernel.async_runtime import AsyncRuntime
from korodexios_core.kernel.diagnostics import (
    DiagnosticCheck,
    DiagnosticSeverity,
    aggregate_diagnostics,
    diagnostic_check,
)
from korodexios_core.kernel.supervision import WorkerRecord, WorkerSupervisor


def make_thread(
    name: str = "worker",
    *,
    delay: float = 0.0,
    hold: threading.Event | None = None,
) -> threading.Thread:
    """Return a daemon thread that finishes after *delay* seconds.

    Pass a cleared *hold* event to keep the thread running until the test sets it.
    """

    def body() -> None:
        if delay:
            time.sleep(delay)
        if hold is not None:
            hold.wait(5)

    return threading.Thread(target=body, name=name, daemon=True)


def test_async_runtime_runs_coroutines() -> None:
    runtime = AsyncRuntime(name="kdx-test", shutdown_timeout=1.0)
    assert runtime.running is False
    runtime.start()
    assert runtime.running is True
    assert isinstance(runtime.loop, asyncio.AbstractEventLoop)

    async def answer() -> int:
        await asyncio.sleep(0)
        return 42

    assert runtime.run(answer(), timeout=2) == 42
    future = runtime.submit(answer())
    assert future.result(2) == 42

    seen: list[int] = []
    runtime.call_soon(seen.append, 7)
    runtime.run(answer(), timeout=2)
    assert seen == [7]

    stats = runtime.stats()
    assert stats["running"] is True
    assert stats["submitted"] >= 3
    assert stats["completed"] >= 3
    assert stats["thread"] == "kdx-test"
    assert "kdx-test" in runtime.describe()
    assert runtime.as_dict()["running"] is True
    assert runtime.as_dict()["thread"] == "kdx-test"
    runtime.stop()
    assert runtime.running is False
    assert runtime.loop is None
    runtime.stop()


def test_async_runtime_reports_failures() -> None:
    runtime = AsyncRuntime(shutdown_timeout=1.0)
    runtime.start()

    async def boom() -> None:
        raise RuntimeError("coroutine exploded")

    with pytest.raises(RuntimeError):
        runtime.run(boom(), timeout=2)
    assert "RuntimeError" in str(runtime.stats()["last_error"])
    assert runtime.stats()["failed"] >= 1
    runtime.stop()


def test_async_runtime_rejects_double_start_and_late_work() -> None:
    runtime = AsyncRuntime(shutdown_timeout=1.0)
    runtime.start()
    assert runtime.start() is runtime  # idempotent, never a second thread

    async def slow() -> int:
        await asyncio.sleep(5)
        return 1

    pending = runtime.submit(slow(), name="slow")
    runtime.stop()
    assert pending.cancelled() or not pending.running()
    late_submit = slow()
    with pytest.raises(ChassisError):
        runtime.submit(late_submit)
    late_submit.close()
    late_run = slow()
    with pytest.raises(ChassisError):
        runtime.run(late_run, timeout=1)
    late_run.close()
    assert runtime.stats()["running"] is False


def test_async_runtime_cancels_pending_tasks_on_stop() -> None:
    runtime = AsyncRuntime(shutdown_timeout=0.5)
    runtime.start()

    async def forever() -> None:
        while True:
            await asyncio.sleep(1)

    runtime.submit(forever(), name="forever")
    started = time.monotonic()
    runtime.stop()
    assert time.monotonic() - started < 1.0
    assert runtime.running is False
    assert runtime.stats()["pending"] == 0


def test_worker_supervisor_tracks_live_and_dead_threads() -> None:
    supervisor = WorkerSupervisor()
    assert supervisor.enabled is True
    gate = threading.Event()
    alive = make_thread("alive", hold=gate)
    alive.start()
    record = supervisor.watch("scripted", alive, name="alive")
    assert record.alive is True
    assert supervisor.check() == []
    assert supervisor.stats()["watched"] == 1

    dying = make_thread("dying", delay=0.01)
    dying.start()
    dying.join(1)
    dead = supervisor.watch("scripted", dying, name="dying")
    assert dead.alive is False
    findings = supervisor.check()
    assert [item.name for item in findings] == ["dying"]
    assert dead.died_at is not None
    assert supervisor.check() == []  # reported once, never twice
    stats = supervisor.stats()
    assert stats["watched"] == 2 and stats["dead"] == 1 and stats["losses"] == 1
    assert "watched=2" in supervisor.describe()
    payload = supervisor.as_dict()
    assert payload["workers"][0]["module"] == "scripted"
    assert len(payload["workers"]) == 2

    assert supervisor.unwatch("scripted", "dying") is True
    assert supervisor.unwatch("scripted", "nobody") is False
    assert len(supervisor) == 1
    gate.set()
    alive.join(2)
    assert alive.is_alive() is False


def test_worker_supervisor_rejects_non_threads_and_records_shape() -> None:
    supervisor = WorkerSupervisor(enabled=False)
    assert supervisor.enabled is False
    assert supervisor.watch("scripted", make_thread("x")).module == "scripted"
    assert supervisor.check() == []  # disabled supervisors stay quiet
    with pytest.raises(TypeError):
        supervisor.watch("scripted", object())  # type: ignore[arg-type]
    record = WorkerRecord(module="scripted", name="solo", thread=make_thread("solo"))
    assert record.alive is False
    assert record.describe() == "scripted/solo (dead)"
    assert record.as_dict()["name"] == "solo"
    assert record.reported is False
    assert record.expected is True


def test_diagnostics_checks_and_aggregate() -> None:
    assert DiagnosticSeverity.ERROR.is_problem is True
    assert DiagnosticSeverity.INFO.is_problem is False
    assert DiagnosticSeverity("error") is DiagnosticSeverity.ERROR
    check = diagnostic_check(
        name="unknown-key",
        message="core.nope is not read",
        severity="warning",
        hint="remove it",
        module="chassis",
    )
    assert check.severity is DiagnosticSeverity.WARNING
    assert check.describe() == "chassis/unknown-key: core.nope is not read (warning)"
    assert check.as_dict()["module"] == "chassis"
    assert check.as_dict()["hint"] == "remove it"
    bare = DiagnosticCheck(name="bare")
    assert bare.severity is DiagnosticSeverity.INFO
    assert bare.is_problem is False
    assert bare.describe() == "bare (info)"

    report = aggregate_diagnostics(
        [
            check,
            diagnostic_check(name="broken", message="boom", severity="error", module="backup"),
            diagnostic_check(name="note", message="fyi", module="heartbeat"),
        ]
    )
    assert report["total"] == 3
    assert report["errors"] == 1
    assert report["warnings"] == 1
    assert report["info"] == 1
    assert report["ok"] is False
    assert [item["name"] for item in report["checks"]] == ["unknown-key", "broken", "note"]
    assert aggregate_diagnostics([])["ok"] is True
