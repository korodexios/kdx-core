"""The heartbeat ticker is a scheduled task on the chassis clock, not a private thread."""

from __future__ import annotations

from pathlib import Path

import pytest

from korodexios_core import Chassis, ChassisOptions
from korodexios_core.kernel.contract import ModuleState
from korodexios_core.kernel.diagnostics import DiagnosticSeverity
from korodexios_core.modules.heartbeat.module import (
    HEARTBEAT_EVENT,
    TICKER_TASK,
    HeartbeatModule,
)
from korodexios_core.paths import AppPaths

FAST_TICKER = {
    "core.log.file_enabled": False,
    "core.ipc.enabled": False,
    "core.lifecycle.tick_interval": 0.01,
    "modules.heartbeat.interval": 0.05,
}


def ticker_chassis(tmp_path: Path, **overrides: object) -> Chassis:
    """Return a chassis whose heartbeat ticks fast enough for a test to watch."""
    paths = AppPaths.discover(root=str(tmp_path), environ={})
    settings = {**FAST_TICKER, **overrides}
    return Chassis.create(
        ChassisOptions(
            home=paths.root,
            environ={},
            cli_overrides=settings,
            install_signals=False,
        )
    )


def test_the_ticker_is_declared_on_the_chassis_scheduler(tmp_path: Path) -> None:
    """start() declares a task; the module keeps no thread of its own."""
    chassis = ticker_chassis(tmp_path)
    chassis.initialize()
    chassis.start(ipc=False)
    try:
        module = chassis.registry.require("heartbeat").module
        assert module.state is ModuleState.RUNNING
        assert module.workers() == {}, "the ticker must not leave a thread to supervise"
        tasks = chassis.context.scheduler.tasks()
        assert [task.name for task in tasks] == [TICKER_TASK]
        assert tasks[0].interval_seconds == pytest.approx(0.05)
        assert tasks[0].module == "heartbeat"
    finally:
        chassis.shutdown()


def test_the_chassis_clock_beats(tmp_path: Path) -> None:
    """A running clock ticks the task and the module reports the same counters."""
    chassis = ticker_chassis(tmp_path)
    chassis.initialize()
    chassis.start(ipc=False)
    try:
        module = chassis.registry.require("heartbeat").module
        seen: list[dict[str, object]] = []
        chassis.context.bus.subscribe(HEARTBEAT_EVENT, lambda event: seen.append(event.payload))
        assert chassis.run_forever(duration=0.2) == 0
        assert module.ticks > 0
        task = chassis.context.scheduler.task(TICKER_TASK)
        assert task is not None
        assert task.runs == module.ticks
        assert task.failures == 0
        health = module.health()
        assert health["ticks"] == module.ticks
        assert health["ticker_scheduled"] is True
        assert health["ticker_runs"] == task.runs
        assert "thread_alive" not in health, "there is no thread left to report"
        assert seen, "each beat publishes one event"
        assert seen[0]["tick"] >= 1
        assert seen[-1]["tick"] == module.ticks
        assert seen[0]["interval"] == pytest.approx(0.05)
    finally:
        chassis.shutdown()


def test_one_beat_publishes_the_heartbeat_event(tmp_path: Path) -> None:
    """Running the task by hand is how a route or CLI command triggers a beat."""
    chassis = ticker_chassis(tmp_path, **{"modules.heartbeat.interval": 5.0})
    chassis.initialize()
    chassis.start(ipc=False)
    try:
        module = chassis.registry.require("heartbeat").module
        payloads: list[dict[str, object]] = []
        chassis.context.bus.subscribe(
            HEARTBEAT_EVENT,
            lambda event: payloads.append(event.payload),
        )
        before = module.ticks
        assert chassis.context.scheduler.run_task(TICKER_TASK) is True
        assert module.ticks == before + 1
        assert payloads == [{"tick": module.ticks, "interval": 5.0}]
    finally:
        chassis.shutdown()


def test_diagnostics_answer_the_clock_truth(tmp_path: Path) -> None:
    """Running, parked, and never-scheduled are three different findings."""
    chassis = ticker_chassis(tmp_path, **{"modules.heartbeat.interval": 5.0})
    chassis.initialize()
    record = chassis.registry.require("heartbeat")
    collected = {check.name: check for check in record.checks}
    assert collected["ticker"].severity is DiagnosticSeverity.INFO
    assert "initialized" in collected["ticker"].message
    chassis.start(ipc=False)
    try:
        live = {check.name: check for check in record.module.diagnostics()}
        assert "alive" in live["ticker"].message
        task = chassis.context.scheduler.task(TICKER_TASK)
        assert task is not None
        task.enabled = False
        parked = {check.name: check for check in record.module.diagnostics()}
        assert parked["ticker"].severity is DiagnosticSeverity.WARNING
        assert "parked" in parked["ticker"].message
        task.enabled = True
        task.last_error = "RuntimeError: beat failed"
        failed = {check.name: check for check in record.module.diagnostics()}
        assert failed["ticker"].severity is DiagnosticSeverity.WARNING
        assert "beat failed" in failed["ticker"].message
    finally:
        chassis.shutdown()
    stopped = {check.name: check for check in record.module.diagnostics()}
    assert stopped["ticker"].message == "heartbeat is stopped"


def test_shutdown_withdraws_the_ticker(tmp_path: Path) -> None:
    """A stopped module leaves no task behind for the next chassis in-process."""
    chassis = ticker_chassis(tmp_path, **{"modules.heartbeat.interval": 5.0})
    chassis.initialize()
    chassis.start(ipc=False)
    module = chassis.registry.require("heartbeat").module
    assert chassis.context.scheduler.task(TICKER_TASK) is not None
    chassis.shutdown()
    assert chassis.context.scheduler.task(TICKER_TASK) is None
    assert chassis.context.scheduler.tasks() == ()
    assert module.health()["ticker_scheduled"] is False


def test_a_module_without_a_chassis_reports_a_baseline() -> None:
    """The hooks answer before init(): a baseline, never an exception."""
    module = HeartbeatModule()
    baseline = module.diagnostics()
    assert [check.name for check in baseline] == [TICKER_TASK]
    assert baseline[0].message == "heartbeat is new"
    assert baseline[0].severity is DiagnosticSeverity.INFO
