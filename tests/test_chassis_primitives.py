"""Chassis wiring for the v2 primitives: scheduler, locks, durable paths, static mounts."""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

import pytest

from korodexios_core.kernel.chassis import Chassis, ChassisOptions
from korodexios_core.kernel.contract import Module, ModuleSpec
from korodexios_core.kernel.ipc import IpcDiscovery, IpcStaticMount
from korodexios_core.kernel.scheduler import TASK_RUN_EVENT, ScheduledTask

PRIMITIVES_CONFIG = """
core:
  name: primitives
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


class ProbeModule(Module):
    """A module that uses every chassis primitive: task, lock, durable dir, mount."""

    name = "probe"
    description = "Uses the chassis primitives."
    version = "0.1.0"
    priority = 60

    def __init__(self, ui_dir: Path) -> None:
        super().__init__()
        self.ui_dir = ui_dir
        self.ticks: list[float] = []
        self.lock_names: list[str] = []
        self.durable: Path | None = None
        self.static_called = False

    def start(self) -> None:
        context = self.require_context()
        scheduler = self.scheduler
        self.lock_names.append(context.acquire_lock("probe", timeout=0.5).name)
        self.durable = context.durable_module_dir("probe")
        scheduler.schedule(
            0.01,
            self.sweep,
            name="sweep",
            first_delay=0.0,
            module=self.name,
        )

    def sweep(self) -> None:
        self.ticks.append(1.0)

    def static_dirs(self) -> tuple[IpcStaticMount, ...]:
        self.static_called = True
        return (IpcStaticMount("/ui", self.ui_dir, module=self.name),)

    def stop(self) -> None:
        return None


def write_root(root: Path, text: str = PRIMITIVES_CONFIG) -> Path:
    """Write a throwaway chassis root with discovery switched off."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "kdx.yml").write_text(text, encoding="utf-8")
    return root


def make_chassis(root: Path, **overrides: object) -> Chassis:
    """Create a chassis over the throwaway root."""
    return Chassis.create(
        ChassisOptions(
            home=root,
            environ={},
            install_signals=False,
            cli_overrides={"core.log.file_enabled": False, **overrides},
        )
    )


def register_probe(chassis: Chassis, ui_dir: Path) -> ProbeModule:
    """Register the primitive probe module and return it."""
    module = ProbeModule(ui_dir)
    chassis.registry.register(
        module,
        ModuleSpec(name="probe", description=ProbeModule.description, version="0.1.0"),
    )
    return module


def test_the_tick_loop_runs_a_declared_task(project_root: Path, tmp_path: Path) -> None:
    write_root(project_root)
    ui = tmp_path / "ui"
    ui.mkdir()
    chassis = make_chassis(project_root)
    module = register_probe(chassis, ui)
    chassis.start(ipc=False)
    try:
        assert chassis.scheduler is chassis.context.scheduler
        assert chassis.scheduler is not None
        assert [task.name for task in chassis.scheduler.tasks()] == ["sweep"]
        seen: list[str] = []
        chassis.bus.subscribe(
            TASK_RUN_EVENT,
            lambda event: seen.append(str(event.payload.get("name", ""))),
        )
        chassis.run_once()
        assert seen == ["sweep"], "the task announced itself on the bus"
        assert module.ticks, "the chassis clock ran the declared task"
        stats = chassis.scheduler.stats()
        assert stats["runs"] >= 1
        assert stats["tasks"][0]["module"] == "probe"
        assert chassis.scheduler.pending() == () or chassis.scheduler.pending() is not None
        assert chassis.scheduler.next_due_in() is None or chassis.scheduler.next_due_in() >= 0.0
        assert chassis.scheduler.run_task("sweep") is True
        assert chassis.scheduler.run_task("nope") is False
        assert module.durable == project_root / "data" / "modules" / "probe"
        assert module.durable.is_dir()
        assert module.lock_names == ["probe"]
        assert chassis.locks is not None
        assert chassis.locks.names() == ("probe",)
    finally:
        chassis.shutdown()
        chassis.signals.uninstall()
    assert chassis.scheduler is not None
    assert chassis.scheduler.running is False
    assert chassis.locks is not None
    assert chassis.locks.names() == ()


def test_the_health_report_covers_the_new_sections(project_root: Path) -> None:
    write_root(project_root)
    chassis = make_chassis(project_root)
    chassis.start(ipc=False)
    try:
        chassis.acquire_lock("index", timeout=0.5)
        chassis.register_static_dir("/ui", chassis.paths.root)
        task = chassis.context.schedule(60.0, lambda: None, name="audit", module="ops")
        assert isinstance(task, ScheduledTask)
        report = chassis.health_report()
        assert report["scheduler"]["scheduled"] == 1
        assert report["scheduler"]["tasks"][0]["name"] == "audit"
        assert report["locks"]["held"] == 1
        assert report["locks"]["names"] == ["index"]
        assert [row["route_prefix"] for row in report["static"]] == ["/ui"]
        assert [check.name for check in chassis._chassis_diagnostics()] == []
        assert chassis.context.scheduler.stats()["scheduled"] == 1
    finally:
        chassis.shutdown()
        chassis.signals.uninstall()


def test_the_context_carries_the_project_durable_directory(project_root: Path) -> None:
    write_root(project_root)
    chassis = make_chassis(project_root)
    try:
        assert chassis.context.paths is chassis.paths
        assert chassis.context.paths.durable_dir == project_root / "data"
        assert chassis.paths.durable_dir == project_root / "data"
        assert chassis.context.durable_module_dir("ops").is_dir()
        assert (project_root / "data" / "modules" / "ops").is_dir()
        assert chassis.paths.locks_dir == project_root / "var" / "locks"
        assert chassis.paths.locks_dir.is_dir()
        assert chassis.context.as_dict()["locks"] == {"names": []}
        assert chassis.context.as_dict()["scheduler"]["scheduled"] == 0
        assert chassis.context.as_dict()["paths"]["durable_dir"] == str(project_root / "data")
    finally:
        chassis.signals.uninstall()


def test_a_module_static_directory_is_served_over_http(project_root: Path, tmp_path: Path) -> None:
    config = PRIMITIVES_CONFIG.replace(
        "  ipc:\n    enabled: false",
        "  ipc:\n    enabled: true\n    port: 0",
    )
    write_root(project_root, config)
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "panel.html").write_text("<!doctype html><p>panel</p>", encoding="utf-8")
    chassis = make_chassis(project_root)
    module = register_probe(chassis, ui)
    chassis.start()
    try:
        assert module.static_called is True
        assert [mount.prefix for mount in chassis.static_mounts()] == ["/ui"]
        discovery = IpcDiscovery.load(project_root / "var" / "chassis.ipc")
        assert discovery is not None
        base = discovery.base_url()
        request = urllib.request.Request(base + "/ui/panel.html", method="GET")
        with urllib.request.urlopen(request, timeout=5) as response:
            assert int(response.status) == 200
            assert str(response.headers.get("Content-Type", "")).startswith("text/html")
            assert response.read().startswith(b"<!doctype html>")
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(
                urllib.request.Request(base + "/ui/missing.html", method="GET"), timeout=5
            )
        assert int(missing.value.code) == 404
        late = chassis.register_static_dir("/late", ui, module="probe")
        assert late.prefix == "/late"
        with urllib.request.urlopen(
            urllib.request.Request(base + "/late/panel.html", method="GET"), timeout=5
        ) as response:
            assert int(response.status) == 200
        assert chassis.unregister_static_dir("/late") is True
        assert chassis.unregister_static_dir("/late") is False
        with pytest.raises(urllib.error.HTTPError) as gone:
            urllib.request.urlopen(
                urllib.request.Request(base + "/late/panel.html", method="GET"), timeout=5
            )
        assert int(gone.value.code) == 404
        assert chassis.release_lock("index") is False
        assert chassis.acquire_lock("index", timeout=0.2).held is True
        assert chassis.release_lock("index") is True
    finally:
        chassis.shutdown()
        chassis.signals.uninstall()


def test_a_failing_task_becomes_a_doctor_warning(project_root: Path) -> None:
    write_root(project_root)
    chassis = make_chassis(project_root)
    chassis.start(ipc=False)
    try:
        def boom() -> None:
            raise RuntimeError("task exploded")

        chassis.context.schedule(0.01, boom, name="boom", first_delay=0.0, module="ops")
        assert chassis.run_once() == 0
        report = chassis.health_report()
        rows = [row for row in report["diagnostics"]["checks"] if row["name"] == "scheduled-task"]
        assert rows and rows[0]["severity"] == "warning"
        assert "task exploded" in rows[0]["message"]
        assert "scheduled-task" in [check.name for check in chassis._chassis_diagnostics()]
    finally:
        chassis.shutdown()
        chassis.signals.uninstall()
