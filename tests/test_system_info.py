"""system_info module tests: collector accuracy, CLI commands, lifecycle counters."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from korodexios_core.cli.entry import main
from korodexios_core.constants import ExitCode
from korodexios_core.kernel.cli_contract import CliCommand
from korodexios_core.modules.system_info import MODULE, SystemInfoModule, collector
from korodexios_core.modules.system_info.collector import (
    cpu_snapshot,
    disk_snapshot,
    format_bytes,
    full_snapshot,
    memory_snapshot,
    os_snapshot,
    uptime_snapshot,
)
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


def test_sysconf_helpers_fall_back_without_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    assert sysconf_value("SC_PAGE_SIZE") is not None
    assert page_size() == sysconf_value("SC_PAGE_SIZE")
    monkeypatch.delattr(os, "sysconf", raising=False)
    assert sysconf_value("SC_PAGE_SIZE") is None
    assert page_size() == DEFAULT_PAGE_SIZE


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
