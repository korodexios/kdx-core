"""The built-in proof modules adopted the Core API v2 declarative hooks (v0.2.2)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from korodexios_core import Chassis, ChassisOptions
from korodexios_core.cli.entry import main
from korodexios_core.config.schema import ConfigKind
from korodexios_core.constants import ExitCode
from korodexios_core.kernel.diagnostics import DiagnosticSeverity
from korodexios_core.kernel.ipc import SCHEMA_PATH
from korodexios_core.modules.backup_engine.module import BackupEngineModule
from korodexios_core.modules.heartbeat.module import HeartbeatModule
from korodexios_core.modules.system_info.module import SystemInfoModule
from korodexios_core.paths import AppPaths

from .conftest import PROJECT_CONFIG

BACKUP_KEYS = (
    "source",
    "dest",
    "include",
    "exclude",
    "max_size",
    "dry_run",
    "follow_symlinks",
    "schedule_interval",
    "auto_prune_days",
)

VIOLATING_CONFIG = """
core:
  log:
    level: INFO
    file_enabled: false
  modules:
    autodiscover: true
    extra_paths: []
  lifecycle:
    tick_interval: 0.05
    stop_timeout: 2.0
  ipc:
    enabled: false
modules:
  heartbeat:
    interval: soon
"""


def base_args(project_root: Path) -> list[str]:
    """Return the global flags every CLI test in this file needs."""
    return ["--home", str(project_root), "--no-log-file", "--no-color"]


def dogfood_chassis(project_root: Path) -> Chassis:
    """Return a chassis with the built-in modules and a plane on an ephemeral port."""
    paths = AppPaths.discover(root=str(project_root), environ={})
    return Chassis.create(
        ChassisOptions(
            home=paths.root,
            environ={},
            cli_overrides={
                "core.log.file_enabled": False,
                "core.ipc.enabled": True,
                "core.ipc.port": 0,
            },
            install_signals=False,
        )
    )


# --------------------------------------------------------------- the declarations


def test_backup_engine_declares_every_key_it_reads() -> None:
    schema = BackupEngineModule().config_schema()
    assert schema.module == "backup_engine"
    assert schema.keys() == BACKUP_KEYS
    assert schema.is_empty is False
    assert schema.required_keys() == ()
    assert schema.validate({}) == []
    kinds = {field.key: field.kind for field in schema.fields}
    assert kinds["source"] is ConfigKind.PATH
    assert kinds["include"] is ConfigKind.LIST
    assert kinds["dry_run"] is ConfigKind.BOOL
    # v0.2.5 added the periodic sweep: the clock interval and the retention window.
    assert kinds["schedule_interval"] is ConfigKind.FLOAT
    assert kinds["auto_prune_days"] is ConfigKind.INT


def test_heartbeat_and_system_info_declare_their_keys() -> None:
    heartbeat = HeartbeatModule().config_schema()
    assert heartbeat.keys() == ("interval",)
    interval = heartbeat.fields[0]
    assert interval.kind is ConfigKind.FLOAT
    assert interval.default == 2.0
    system_info = SystemInfoModule().config_schema()
    # v0.2.4 added the load watcher keys to the same module.
    assert system_info.keys() == (
        "disk_path",
        "include_cpu_model",
        "watch_load",
        "load_interval",
    )
    assert {field.key: field.kind for field in system_info.fields} == {
        "disk_path": ConfigKind.PATH,
        "include_cpu_model": ConfigKind.BOOL,
        "watch_load": ConfigKind.BOOL,
        "load_interval": ConfigKind.FLOAT,
    }


def test_module_routes_are_declared_read_only() -> None:
    backup = BackupEngineModule().ipc_routes()
    assert [route.path for route in backup] == ["/api/v1/backup/plan"]
    assert [route.methods for route in backup] == [("GET",)]
    system_info = SystemInfoModule().ipc_routes()
    assert [route.path for route in system_info] == ["/api/v1/system/snapshot"]
    assert all(route.module and route.summary for route in backup + system_info)


def test_diagnostics_answer_the_pre_init_pass_with_a_baseline() -> None:
    """A hook that needs its context reports a baseline instead of raising."""
    assert BackupEngineModule().diagnostics() == ()
    baseline = HeartbeatModule().diagnostics()
    assert [check.name for check in baseline] == ["ticker"]
    assert baseline[0].severity is DiagnosticSeverity.INFO
    assert "new" in baseline[0].message


# --------------------------------------------------------------- config check CLI


def test_config_check_validates_the_builtin_schemas(tmp_path: Path, capsys: Any) -> None:
    (tmp_path / "kdx.yml").write_text(PROJECT_CONFIG, encoding="utf-8")
    assert main([*base_args(tmp_path), "config", "check"]) == int(ExitCode.SUCCESS)
    capsys.readouterr()  # the human report ran; the next run answers with JSON
    assert main([*base_args(tmp_path), "config", "check", "--json"]) == int(ExitCode.SUCCESS)
    payload = json.loads(capsys.readouterr().out)
    assert payload["schemas"] == {"modules": 3, "issues": []}
    assert payload["unknown"] == [], payload["unknown"]


def test_config_check_reports_a_schema_violation_in_a_built_in(
    tmp_path: Path,
    capsys: Any,
) -> None:
    """A project that sets a built-in key to the wrong type fails its own schema."""
    (tmp_path / "kdx.yml").write_text(VIOLATING_CONFIG, encoding="utf-8")
    code = main([*base_args(tmp_path), "config", "check", "--json"])
    assert code == int(ExitCode.CONFIG_ERROR)
    payload = json.loads(capsys.readouterr().out)
    issues = payload["schemas"]["issues"]
    assert [issue["key"] for issue in issues] == ["modules.heartbeat.interval"]
    assert issues[0]["severity"] == "error"
    assert issues[0]["module"] == "heartbeat"


# ------------------------------------------------------------------ the live plane


def test_schema_route_reports_the_new_module_routes(project_root: Path) -> None:
    chassis = dogfood_chassis(project_root)
    chassis.initialize()
    chassis.start()
    try:
        assert chassis.ipc is not None and chassis.ipc.running
        code, contract = chassis.ipc.discovery.request(SCHEMA_PATH)  # type: ignore[union-attr]
        assert code == 200
        paths = {route["path"] for route in contract["module_routes"]}
        assert {"/api/v1/backup/plan", "/api/v1/system/snapshot"} <= paths
    finally:
        chassis.shutdown()


def test_the_two_new_routes_answer_their_own_payloads(project_root: Path) -> None:
    chassis = dogfood_chassis(project_root)
    chassis.initialize()
    chassis.start()
    try:
        discovery = chassis.ipc.discovery  # type: ignore[union-attr]
        assert discovery is not None
        plan_code, plan = discovery.request("/api/v1/backup/plan")
        assert plan_code == 200
        assert str(plan["settings"]["dest"]).endswith("backups")
        assert str(plan["plan"]["target"]).startswith(str(plan["settings"]["dest"]))
        snapshot_code, snapshot = discovery.request("/api/v1/system/snapshot")
        assert snapshot_code == 200
        assert "os" in snapshot and "disk" in snapshot
    finally:
        chassis.shutdown()


def test_heartbeat_reports_its_worker_alive_once_running(project_root: Path) -> None:
    chassis = dogfood_chassis(project_root)
    chassis.initialize()
    record = chassis.registry.require("heartbeat")
    collected = {check.name: check for check in record.checks}
    assert collected["ticker"].severity is DiagnosticSeverity.INFO
    assert "initialized" in collected["ticker"].message
    chassis.start()
    try:
        live = {check.name: check for check in record.module.diagnostics()}
        assert live["ticker"].severity is not DiagnosticSeverity.ERROR
        assert "alive" in live["ticker"].message
    finally:
        chassis.shutdown()
