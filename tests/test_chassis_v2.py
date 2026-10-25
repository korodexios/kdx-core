"""Core API v2 chassis behaviour: instance lock, async loop, supervision, reload, rpc."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from korodexios_core.config.layered import LayeredConfig
from korodexios_core.config.schema import ConfigField, ConfigKind, ConfigSchema
from korodexios_core.constants import ExitCode
from korodexios_core.exceptions import InstanceLockError
from korodexios_core.kernel.chassis import (
    RELOAD_EVENT,
    WORKER_LOST_EVENT,
    Chassis,
    ChassisOptions,
    ChassisState,
)
from korodexios_core.kernel.cli_contract import CliArgument, CliCommand
from korodexios_core.kernel.contract import ModuleSpec
from korodexios_core.kernel.diagnostics import DiagnosticCheck, DiagnosticSeverity
from korodexios_core.kernel.ipc import IpcDiscovery, IpcRoute
from korodexios_core.modules.system_info.module import WATCH_LOAD_DEFAULT, WATCHER_NAME
from korodexios_core.paths import AppPaths

PROJECT_CONFIG = """
core:
  name: v2probe
  log:
    level: INFO
    file_enabled: false
  lifecycle:
    tick_interval: 0.05
    stop_timeout: 2.0
  ipc:
    enabled: false
  modules:
    autodiscover: false
"""


class V2Module:
    """Contract-satisfying double that declares every optional v2 hook."""

    name = "v2probe"
    description = "Exercises the optional v2 module hooks."
    version = "0.1.0"
    priority = 60

    def __init__(self) -> None:
        self.context = None
        self.reloaded: list[LayeredConfig] = []
        self.worker: threading.Thread | None = None
        self.hold = threading.Event()
        self.last_request = None
        self.bad_diagnostics = False

    def init(self, context: object) -> None:
        self.context = context

    def register_cli(self, surface: object) -> None:
        surface.add_command(
            CliCommand(
                name="v2probe",
                help="Probe command.",
                args=(
                    CliArgument(("--lines",), dest="lines", type="int", default=3),
                    CliArgument(("--json",), dest="as_json", action="store_true"),
                ),
                handler=self.handle,
                module_name=self.name,
            )
        )

    def handle(self, request: object) -> int:
        self.last_request = request
        return 0

    def start(self) -> None:
        self.hold.clear()
        self.worker = threading.Thread(
            target=lambda: self.hold.wait(5), name="v2-worker", daemon=True
        )
        self.worker.start()

    def stop(self) -> None:
        if self.worker is not None:
            self.hold.set()
            self.worker.join(2)

    def health(self) -> dict[str, object]:
        return {"name": self.name, "reloads": len(self.reloaded)}

    def config_schema(self) -> ConfigSchema:
        return ConfigSchema(
            module=self.name,
            fields=(
                ConfigField("port", ConfigKind.INT, default=1500),
                ConfigField("token", ConfigKind.STR, secret=True, required=True),
            ),
        )

    def ipc_routes(self) -> list[IpcRoute]:
        return [
            IpcRoute(
                path="/api/v1/v2probe",
                handler=lambda request: {"echo": request.argument("q")},
                module=self.name,
            )
        ]

    def diagnostics(self) -> list[DiagnosticCheck]:
        checks = [
            DiagnosticCheck(
                name="probe",
                message="probe is healthy",
                severity=DiagnosticSeverity.INFO,
                module=self.name,
            )
        ]
        if self.bad_diagnostics:
            checks.append(
                DiagnosticCheck(
                    name="broken", message="probe is broken", severity="error"
                )
            )
        return checks

    def workers(self) -> dict[str, threading.Thread]:
        return {"v2-worker": self.worker} if self.worker is not None else {}

    def on_config_reload(self, config: LayeredConfig) -> None:
        self.reloaded.append(config)


class FakeSurface:
    """Minimal CliSurface stand-in: the registry wraps it and records the commands."""

    def __init__(self) -> None:
        self.added: list[CliCommand] = []

    def add_command(self, command: CliCommand) -> None:
        self.added.append(command)


def write_root(root: Path, text: str = PROJECT_CONFIG) -> Path:
    """Write a throwaway chassis root with discovery switched off."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "kdx.yml").write_text(text, encoding="utf-8")
    return root


def make_chassis(root: Path, **overrides: object) -> Chassis:
    """Create a chassis over the throwaway root with the module hooks under test."""
    return Chassis.create(
        ChassisOptions(
            home=root,
            environ={},
            install_signals=False,
            cli_overrides={"core.log.file_enabled": False, **overrides},
        )
    )


def register_v2(chassis: Chassis, *, bad_diagnostics: bool = False) -> V2Module:
    """Register the v2 double and return it."""
    module = V2Module()
    module.bad_diagnostics = bad_diagnostics
    chassis.registry.register(
        module,
        ModuleSpec(
            name=V2Module.name,
            description=V2Module.description,
            version="0.1.0",
            priority=60,
            origin="test",
        ),
    )
    return module


def test_create_reports_the_new_sections(project_root: Path) -> None:
    write_root(project_root)
    chassis = make_chassis(project_root)
    try:
        assert chassis.instance_lock is None
        assert chassis.supervisor is not None
        assert chassis.async_runtime is None
        assert chassis.context.async_available is False
        assert chassis.reloads == 0
        report = chassis.health_report()
        for key in ("instance", "async", "supervision", "diagnostics"):
            assert key in report, key
        assert report["instance"]["enabled"] is True
        assert str(project_root / "var" / "chassis.lock") == report["instance"]["lock"]
        assert report["async"]["running"] is False
        assert report["supervision"]["watched"] == 0
        assert report["config"]["reloads"] == 0
    finally:
        chassis.signals.uninstall()


def test_injected_config_wins(project_root: Path) -> None:
    write_root(project_root)
    paths = AppPaths.discover(root=str(project_root), environ={})
    injected = LayeredConfig.load(
        config_file=paths.config_file,
        environ={},
        cli_overrides={"core.name": "injected"},
    )
    chassis = Chassis.create(
        ChassisOptions(home=project_root, environ={}, install_signals=False, config=injected)
    )
    try:
        assert chassis.context.app_name == "injected"
        assert chassis.config.get_str("core.name") == "injected"
        assert chassis.config.config_file == injected.config_file
    finally:
        chassis.signals.uninstall()


def test_state_and_data_dirs_are_prepared(project_root: Path) -> None:
    write_root(
        project_root,
        PROJECT_CONFIG.replace(
            "  ipc:\n    enabled: false",
            "  ipc:\n    enabled: false\n  paths:\n    state_dir: state\n    data_dir: state/lib",
        ),
    )
    chassis = make_chassis(project_root)
    try:
        assert (project_root / "state").is_dir()
        assert (project_root / "state" / "lib").is_dir()
        assert chassis.paths.lock_file == project_root / "state" / "chassis.lock"
        assert chassis.paths.lock_file.parent.is_dir()
    finally:
        chassis.signals.uninstall()


def test_instance_lock_blocks_a_second_daemon(project_root: Path) -> None:
    write_root(project_root)
    first = make_chassis(project_root)
    first.start(ipc=False)
    try:
        assert first.instance_lock is not None
        assert first.instance_lock.held is True
        second = make_chassis(project_root)
        with pytest.raises(InstanceLockError) as captured:
            second.start(ipc=False)
        assert "chassis.lock" not in captured.value.message
        assert str(project_root / "var") in captured.value.message
        second.shutdown()
        assert first.state is ChassisState.RUNNING
        assert first.shutdown() == int(ExitCode.SUCCESS)
        third = make_chassis(project_root)
        third.start(ipc=False)
        assert third.instance_lock is not None and third.instance_lock.held is True
        third.shutdown()
    finally:
        first.signals.uninstall()


def test_instance_lock_can_be_switched_off(project_root: Path) -> None:
    write_root(project_root)
    chassis = Chassis.create(
        ChassisOptions(
            home=project_root,
            environ={},
            install_signals=False,
            instance_lock=False,
            cli_overrides={"core.log.file_enabled": False},
        )
    )
    chassis.start(ipc=False)
    try:
        assert chassis.instance_lock is None
        assert not (project_root / "var" / "chassis.lock").exists()
    finally:
        chassis.shutdown()
        chassis.signals.uninstall()


def test_async_loop_is_opt_in(project_root: Path) -> None:
    write_root(project_root)
    chassis = Chassis.create(
        ChassisOptions(
            home=project_root,
            environ={},
            install_signals=False,
            asyncio_enabled=True,
            cli_overrides={"core.log.file_enabled": False},
        )
    )
    chassis.start(ipc=False)
    try:
        assert chassis.async_runtime is not None
        assert chassis.context.async_available is True

        async def answer() -> int:
            return 7

        assert chassis.context.run_async(answer(), timeout=2) == 7
        future = chassis.context.submit_async(answer())
        assert future.result(2) == 7
        assert chassis.health_report()["async"]["running"] is True
    finally:
        chassis.shutdown()
        chassis.signals.uninstall()


def test_run_async_falls_back_to_a_private_loop(project_root: Path) -> None:
    write_root(project_root)
    chassis = make_chassis(project_root)
    try:
        assert chassis.context.async_available is False

        async def answer() -> str:
            return "private"

        assert chassis.context.run_async(answer(), timeout=2) == "private"
    finally:
        chassis.signals.uninstall()


def test_supervision_marks_a_dead_worker_failed(project_root: Path) -> None:
    write_root(project_root)
    chassis = make_chassis(project_root)
    module = register_v2(chassis)
    chassis.start(ipc=False)
    try:
        assert chassis.health_report()["supervision"]["watched"] == 1
        events: list[dict] = []
        chassis.bus.subscribe(WORKER_LOST_EVENT, lambda event: events.append(event.payload))
        assert chassis.supervise() == []  # the worker is still alive
        assert module.worker is not None
        module.hold.set()
        module.worker.join(2)
        dead = chassis.supervise()
        assert [record.name for record in dead] == ["v2-worker"]
        assert chassis.supervise() == []  # reported once
        health = chassis.registry.health_for("v2probe")
        assert health is not None and health["state"] == "failed"
        assert events and events[0]["worker"] == "v2-worker"
        assert chassis.health_report()["supervision"]["dead"] == 1
    finally:
        chassis.shutdown()
        chassis.signals.uninstall()


def test_builtins_schedule_and_supervise_their_own_workers(project_root: Path) -> None:
    """v0.2.3 scheduled the ticker; v0.2.4 gave a built-in a thread worth watching."""
    write_root(project_root, PROJECT_CONFIG.replace("autodiscover: false", "autodiscover: true"))
    chassis = make_chassis(project_root)
    chassis.start(ipc=False)
    try:
        ticker = chassis.registry.require("heartbeat").module
        assert ticker.workers() == {}, "the scheduled ticker leaves no thread behind"
        watcher = chassis.registry.require("system_info").module
        expected = 1 if WATCH_LOAD_DEFAULT else 0
        assert bool(watcher.workers()) is bool(expected)
        watched = [(record.module, record.name) for record in chassis.supervisor.records()]
        assert watched == ([("system_info", WATCHER_NAME)] if expected else [])
        assert chassis.health_report()["supervision"]["watched"] == expected
        assert chassis.supervise() == [], "a live worker is not a loss"
        tasks = chassis.context.scheduler.tasks()
        assert [task.name for task in tasks] == ["ticker"]
        assert tasks[0].module == "heartbeat"
    finally:
        chassis.shutdown()
        chassis.signals.uninstall()


def test_reload_notifies_modules_and_publishes_an_event(project_root: Path) -> None:
    write_root(project_root)
    chassis = make_chassis(project_root)
    module = register_v2(chassis)
    chassis.start(ipc=False)
    try:
        events: list[dict] = []
        chassis.bus.subscribe(RELOAD_EVENT, lambda event: events.append(event.payload))
        summary = chassis.reload_config()
        assert summary["reloads"] == 1
        assert summary["keys"] > 0
        assert chassis.reloads == 1
        assert chassis.last_reload is not None
        assert module.reloaded and module.reloaded[0].get_str("port", "") == ""
        assert events and events[0]["reloads"] == 1
        (project_root / "kdx.yml").write_text(
            PROJECT_CONFIG.replace("level: INFO", "level: WARNING"),
            encoding="utf-8",
        )
        second = chassis.reload_config()
        assert second["reloads"] == 2
        assert "core.log.level" in second["changed"]
        assert chassis.config.get_str("core.log.level") == "WARNING"
        assert chassis.reload_config()["reloads"] == 3
        assert chassis.reload_config()["changed"] == []
    finally:
        chassis.shutdown()
        chassis.signals.uninstall()


def test_dispatch_rpc_runs_registered_commands(project_root: Path) -> None:
    write_root(project_root)
    chassis = make_chassis(project_root)
    module = register_v2(chassis)
    chassis.start(ipc=False)  # no CLI surface: the kernel collected the commands
    try:
        assert [command.name for command in chassis.registry.commands()] == ["v2probe"]
        result = chassis.dispatch_rpc("v2probe")
        assert result["exit_code"] == 0
        assert result["module"] == "v2probe"
        assert result["path"] == "v2probe"
        assert result["health"] is not None
        assert module.last_request is not None
        assert module.last_request.namespace.lines == 3
        assert module.last_request.options["rpc"] is True
        chassis.dispatch_rpc("v2probe", {"lines": 9})
        assert module.last_request.namespace.lines == 9
        chassis.dispatch_rpc("v2probe", {"argv": ["--lines", "5", "--json"]})
        assert module.last_request.namespace.lines == 5
        assert module.last_request.namespace.as_json is True
        chassis.dispatch_rpc("v2probe --lines 4")
        assert module.last_request.namespace.lines == 4
        with pytest.raises(LookupError):
            chassis.dispatch_rpc("nope")
        with pytest.raises(LookupError):
            chassis.dispatch_rpc("")
        with pytest.raises(LookupError):
            chassis.dispatch_rpc("v2probe nope")
    finally:
        chassis.shutdown()
        chassis.signals.uninstall()


def test_embedded_daemon_answers_rpc_over_http(project_root: Path) -> None:
    """No CLI surface and no cli.entry import: the control plane dispatches commands."""
    config = PROJECT_CONFIG.replace("autodiscover: false", "autodiscover: true")
    config = config.replace(
        "    enabled: false",
        "    enabled: true\n    port: 0",
    )
    write_root(project_root, config)
    chassis = make_chassis(project_root)
    chassis.start()
    try:
        discovery = IpcDiscovery.load(project_root / "var" / "chassis.ipc")
        assert discovery is not None
        code, payload = discovery.call("heartbeat status", timeout=5)
        assert code == 200
        assert payload["ok"] is True
        assert payload["result"]["exit_code"] == 0
        assert payload["result"]["module"] == "heartbeat"
        code, payload = discovery.call("nope nothing", timeout=5)
        assert code == 404
        assert "no module command" in payload["error"]
    finally:
        chassis.shutdown()
        chassis.signals.uninstall()


def test_commands_exist_without_a_cli_surface(project_root: Path) -> None:
    """An embedded daemon knows its module commands; the CLI was never imported."""
    write_root(project_root, PROJECT_CONFIG.replace("autodiscover: false", "autodiscover: true"))
    chassis = make_chassis(project_root)
    assert chassis.registry.commands() == []  # register_cli runs at initialize(), not create()
    chassis.start(ipc=False)
    try:
        names = [command.name for command in chassis.registry.commands()]
        assert names == ["system", "heartbeat", "backup"]
        result = chassis.dispatch_rpc("heartbeat status")
        assert result["exit_code"] == 0
        assert result["module"] == "heartbeat"
        assert result["path"] == "heartbeat status"
        surface = FakeSurface()
        assert [command.name for command in chassis.collect_cli(surface)] == names
        assert [command.name for command in surface.added] == names  # replayed, not re-asked
    finally:
        chassis.shutdown()
        chassis.signals.uninstall()


def test_ipc_routes_are_collected_and_registrable(project_root: Path) -> None:
    write_root(project_root)
    chassis = make_chassis(project_root)
    module = register_v2(chassis)
    chassis.start(ipc=False)
    try:
        assert [route.path for route in chassis.ipc_routes] == ["/api/v1/v2probe"]
        late = IpcRoute(path="/api/v1/late", handler=lambda request: {"late": True}, module="late")
        assert chassis.register_ipc_route(late) is True
        assert "/api/v1/late" in [route.path for route in chassis.ipc_routes]
        assert chassis.register_ipc_route(late) is True  # replaced, never duplicated
        assert [route.path for route in chassis.ipc_routes].count("/api/v1/late") == 1
        assert chassis.unregister_ipc_route("/api/v1/late") is True
        assert chassis.unregister_ipc_route("/api/v1/late") is False
        assert chassis.context.register_ipc_route(
            IpcRoute(path="/api/v1/ctx", handler=lambda request: {}, module="ctx")
        ) is True
        assert "/api/v1/ctx" in [route.path for route in chassis.ipc_routes]
        assert module.ipc_routes()[0].module == "v2probe"
    finally:
        chassis.shutdown()
        chassis.signals.uninstall()


def test_module_diagnostics_and_schemas_reach_the_report(project_root: Path) -> None:
    write_root(project_root)
    chassis = make_chassis(project_root)
    register_v2(chassis, bad_diagnostics=True)
    chassis.start(ipc=False)
    try:
        schemas = chassis.registry.schemas()
        assert list(schemas) == ["v2probe"]
        assert schemas["v2probe"].secrets() == ("token",)
        issues = chassis.registry.validate_schemas(chassis.config)
        assert [issue.key for issue in issues] == ["modules.v2probe.token"]
        report = chassis.health_report()
        diagnostics = report["diagnostics"]
        assert diagnostics["errors"] == 1
        assert diagnostics["info"] == 1
        assert diagnostics["ok"] is False
        names = {item["name"] for item in diagnostics["checks"]}
        assert "probe" in names and "broken" in names
        modules = {item["module"] for item in diagnostics["checks"]}
        assert "v2probe" in modules
        assert chassis.registry.diagnostics()
    finally:
        chassis.shutdown()
        chassis.signals.uninstall()


def test_signals_reload_without_exit(project_root: Path) -> None:
    write_root(project_root)
    chassis = make_chassis(project_root, **{"core.reload.sighup": True})
    try:
        signals = chassis.signals
        assert signals.reload_enabled is True
        assert signals.reload_requested is False
        assert signals.reload_count == 0
        signals.record_reload()
        signals.record_reload()
        assert signals.reload_requested is True
        assert signals.reload_count == 2
        assert signals.reload_event.is_set() is True
        summary = chassis.reload_config()
        assert summary["reloads"] == 1
        signals.clear_reload()
        assert signals.reload_requested is False
        assert signals.reload_count == 2  # the counter stays cumulative
        assert signals.reload_event.is_set() is False
        signals.request_reload()
        assert signals.reload_requested is True
        assert "reload" in signals.describe()
        assert signals.as_dict()["reload_requested"] is True
    finally:
        chassis.signals.uninstall()


def test_supervision_can_be_switched_off(project_root: Path) -> None:
    write_root(project_root)
    chassis = Chassis.create(
        ChassisOptions(
            home=project_root,
            environ={},
            install_signals=False,
            supervision=False,
            cli_overrides={"core.log.file_enabled": False},
        )
    )
    module = register_v2(chassis)
    chassis.start(ipc=False)
    try:
        assert chassis.supervisor.enabled is False
        assert chassis.supervisor.check() == []
        stats = chassis.health_report()["supervision"]
        assert stats["watched"] == 1 and stats["enabled"] is False and stats["losses"] == 0
        assert module.worker is not None
    finally:
        chassis.shutdown()
        chassis.signals.uninstall()
