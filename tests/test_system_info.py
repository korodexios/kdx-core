"""system_info module tests: collector accuracy, CLI commands, lifecycle counters."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from korodexios_core.cli.entry import main
from korodexios_core.constants import ExitCode
from korodexios_core.kernel.chassis import Chassis, ChassisOptions
from korodexios_core.kernel.cli_contract import CliCommand
from korodexios_core.kernel.diagnostics import DiagnosticSeverity
from korodexios_core.modules.system_info import MODULE, SystemInfoModule, collector
from korodexios_core.modules.system_info import module as system_info_module
from korodexios_core.modules.system_info.collector import (
    cpu_snapshot,
    disk_snapshot,
    format_bytes,
    full_snapshot,
    memory_snapshot,
    os_snapshot,
    uptime_snapshot,
)
from korodexios_core.modules.system_info.module import (
    LOAD_EVENT,
    WATCH_LOAD_DEFAULT,
    WATCHER_NAME,
    WATCHER_THREAD,
)
from korodexios_core.paths import AppPaths
from korodexios_core.units import DEFAULT_PAGE_SIZE, page_size, sysconf_value
from tests.test_chassis import build_chassis


def base_args(project_root) -> list[str]:
    return ["--home", str(project_root), "--no-log-file", "--no-color"]


def test_format_bytes_reads_well() -> None:
    assert format_bytes(None) == "-"
    assert format_bytes(512) == "512 bytes"
    assert format_bytes(2_048) == "2.0 KB"
    assert format_bytes(5 * 1_024**2) == "5.0 MB"


def test_os_and_cpu_snapshots_are_populated() -> None:
    os_info = os_snapshot()
    assert os_info["system"]
    assert os_info["python"]
    cpu = cpu_snapshot()
    assert cpu["logical_cores"] and cpu["logical_cores"] > 0
    assert "load_average" in cpu


def test_memory_snapshot_reports_total_bytes() -> None:
    memory = memory_snapshot()
    assert memory["source"] in ("proc/meminfo", "sysconf", "unavailable")
    if memory["source"] != "unavailable":
        assert memory["total_bytes"] and memory["total_bytes"] > 0
        assert memory["total"] != "-"


@pytest.mark.posix
def test_sysconf_helpers_fall_back_without_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    assert sysconf_value("SC_PAGE_SIZE") is not None
    assert page_size() == sysconf_value("SC_PAGE_SIZE")
    monkeypatch.delattr(os, "sysconf", raising=False)
    assert sysconf_value("SC_PAGE_SIZE") is None
    assert page_size() == DEFAULT_PAGE_SIZE


@pytest.mark.posix
def test_memory_snapshot_without_meminfo_uses_sysconf(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collector, "_meminfo_values", lambda: {})
    memory = memory_snapshot()
    assert memory["source"] == "sysconf"
    assert memory["total_bytes"] and memory["total_bytes"] > 0


def test_memory_snapshot_without_posix_reports_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collector, "_meminfo_values", lambda: {})
    monkeypatch.delattr(os, "sysconf", raising=False)
    memory = memory_snapshot()
    assert memory["source"] == "unavailable"
    assert memory["total_bytes"] is None
    assert memory["total"] == "-"


def test_disk_snapshot_targets_the_requested_path(tmp_path: Path) -> None:
    disk = disk_snapshot(tmp_path)
    assert disk["path"] == str(tmp_path)
    assert disk["total_bytes"] > 0
    assert disk["used_percent"] is not None


def test_uptime_snapshot_is_shaped() -> None:
    uptime = uptime_snapshot()
    assert set(uptime) == {"seconds", "pretty", "source"}


def test_full_snapshot_has_every_section() -> None:
    data = full_snapshot(".")
    for key in ("timestamp", "os", "cpu", "memory", "disk", "uptime"):
        assert key in data


def test_module_lifecycle_counts_snapshots(project_root) -> None:
    chassis = build_chassis(project_root)
    chassis.initialize()
    chassis.start()
    record = chassis.registry.require("system_info")
    assert isinstance(record.module, SystemInfoModule)
    health = record.module.health()
    assert health["snapshots"] >= 1
    assert "os" in health["last_sections"]
    chassis.shutdown()
    assert record.module.health()["snapshots"] >= 1


class RecordingSurface:
    """Minimal CliSurface double: records what a module contributes."""

    def __init__(self) -> None:
        self.added: list[CliCommand] = []

    def add_command(self, command: CliCommand) -> None:
        self.added.append(command)


def test_register_cli_contributes_the_system_group() -> None:
    surface = RecordingSurface()
    SystemInfoModule().register_cli(surface)
    assert [command.name for command in surface.added] == ["system"]
    children = [child.name for child in surface.added[0].subcommands]
    assert children == ["info", "diagnostics", "disk"]
    assert all(child.module_name == "system_info" for child in surface.added[0].subcommands)


def test_cli_system_info_json(capsys, project_root) -> None:
    args = [*base_args(project_root), "system", "info", "--json"]
    assert main(args) == int(ExitCode.SUCCESS)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    for key in ("os", "cpu", "memory", "disk", "uptime"):
        assert key in payload


def test_cli_system_disk_reports_path(capsys, project_root) -> None:
    args = [*base_args(project_root), "system", "disk", str(project_root), "--json"]
    assert main(args) == int(ExitCode.SUCCESS)
    payload = json.loads(capsys.readouterr().out)
    assert payload["path"] == str(project_root)
    assert payload["total_bytes"] > 0


def test_module_instance_is_shared_with_discovery() -> None:
    assert isinstance(MODULE, SystemInfoModule)
    assert MODULE.name == "system_info"


def wait_for_reads(module: SystemInfoModule, at_least: int, timeout: float = 2.0) -> bool:
    """Wait for the watcher to report *at_least* readings; a slow platform gets 2 seconds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if int(module.health()["load_reads"]) >= at_least:
            return True
        time.sleep(0.02)
    return False


def watcher_chassis(project_root: Path, **overrides: object) -> Chassis:
    """Return a running chassis whose load watcher ticks fast enough to watch."""
    settings: dict[str, object] = {
        "core.log.file_enabled": False,
        "core.ipc.enabled": False,
        "modules.system_info.watch_load": True,
        "modules.system_info.load_interval": 0.05,
        **overrides,
    }
    paths = AppPaths.discover(root=str(project_root), environ={})
    chassis = Chassis.create(
        ChassisOptions(
            home=paths.root,
            environ={},
            cli_overrides=settings,
            install_signals=False,
        )
    )
    chassis.initialize()
    chassis.start(ipc=False)
    return chassis


def test_load_reading_matches_platform_support() -> None:
    """The reading is either there or the platform cannot answer; both are reported."""
    reading = collector.load_reading()
    assert collector.supports_load_average() is (reading is not None)
    if reading is not None:
        assert len(reading) == 3
        assert all(isinstance(value, float) for value in reading)


def test_the_load_watcher_reports_its_thread_to_supervision(project_root: Path) -> None:
    """The built-in thread is a watched worker and shutdown joins what it started."""
    if not WATCH_LOAD_DEFAULT:
        pytest.skip("this platform has no load average to watch")
    chassis = watcher_chassis(project_root)
    module = chassis.registry.require("system_info").module
    try:
        assert module.health()["watch_load"] is True
        events: list[object] = []
        chassis.context.bus.subscribe(
            LOAD_EVENT, lambda event: events.append(event.payload)
        )
        assert wait_for_reads(module, 2), "the watcher reads the load average on its own"
        assert events, "each reading is announced on the event bus"
        assert "load_average" in events[-1]  # type: ignore[operator]
        workers = module.workers()
        assert list(workers) == [WATCHER_NAME]
        assert workers[WATCHER_NAME].name == WATCHER_THREAD
        watched = [(r.module, r.name) for r in chassis.supervisor.records()]
        assert watched == [("system_info", WATCHER_NAME)]
        assert chassis.health_report()["supervision"]["watched"] == 1
        assert chassis.supervise() == [], "a live worker is nothing to report"
        alive = {check.name: check for check in module.diagnostics()}
        assert "alive" in alive["watcher"].message
        assert module.health()["load_average"] is not None
    finally:
        worker = workers[WATCHER_NAME]
        chassis.shutdown()
    assert worker.is_alive() is False, "stop() joined the thread it started"
    assert module.workers() == {}
    assert module.health()["load_watcher"] is False


def test_a_parked_watcher_says_so(project_root: Path) -> None:
    """An operator who turned the watcher off gets a fact, not a warning."""
    chassis = watcher_chassis(project_root, **{"modules.system_info.watch_load": False})
    module = chassis.registry.require("system_info").module
    try:
        assert module.workers() == {}
        assert chassis.supervisor.records() == []
        assert chassis.health_report()["supervision"]["watched"] == 0
        parked = {check.name: check for check in module.diagnostics()}
        assert parked["watcher"].severity is DiagnosticSeverity.INFO
        assert parked["watcher"].message == "load watcher is parked"
        assert "watch_load" in parked["watcher"].hint
        assert module.health()["load_watcher"] is False
    finally:
        chassis.shutdown()


def test_a_platform_without_load_average_parks_the_watcher(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A forced watcher on a platform with no load average stops politely, in the log."""
    monkeypatch.setattr(system_info_module, "load_reading", lambda: None)
    chassis = watcher_chassis(project_root)
    module = chassis.registry.require("system_info").module
    try:
        assert module.workers() == {}, "no thread spins for a reading that cannot exist"
        assert module.health()["load_reads"] == 0
        missing = {check.name: check for check in module.diagnostics()}
        assert missing["watcher"].severity is DiagnosticSeverity.WARNING
        assert "no load average" in missing["watcher"].message
    finally:
        chassis.shutdown()


def test_a_watcher_that_stopped_is_reported(project_root: Path) -> None:
    """A sampler that died is a warning the operator can read in kdx doctor."""
    if not WATCH_LOAD_DEFAULT:
        pytest.skip("this platform has no load average to watch")
    chassis = watcher_chassis(project_root)
    module = chassis.registry.require("system_info").module
    try:
        assert wait_for_reads(module, 1)
        worker = module.workers()[WATCHER_NAME]
        # The module's own release latch, set early: the thread is now a worker
        # that stopped on its own from the supervisor's point of view.
        module._stop_worker.set()
        worker.join(timeout=2.0)
        assert worker.is_alive() is False
        stopped = {check.name: check for check in module.diagnostics()}
        assert stopped["watcher"].severity is DiagnosticSeverity.WARNING
        assert "stopped after" in stopped["watcher"].message
        losses = chassis.supervise()
        assert [(r.module, r.name) for r in losses] == [("system_info", WATCHER_NAME)]
        assert chassis.registry.require("system_info").state.value == "failed"
    finally:
        chassis.shutdown()
