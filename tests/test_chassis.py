"""Chassis lifecycle, signal handling, and exit-code tests."""

from __future__ import annotations

import signal

from korodexios_core.constants import ExitCode
from korodexios_core.kernel.chassis import Chassis, ChassisOptions, ChassisState
from korodexios_core.paths import AppPaths


def build_chassis(project_root, install_signals: bool = False) -> Chassis:
    paths = AppPaths.discover(root=str(project_root), environ={})
    return Chassis.create(
        ChassisOptions(
            home=paths.root,
            environ={},
            cli_overrides={"core.log.file_enabled": False},
            install_signals=install_signals,
        )
    )


def test_create_prepares_config_logging_and_modules(project_root) -> None:
    chassis = build_chassis(project_root)
    assert chassis.state is ChassisState.PREPARED
    assert chassis.config.get_bool("core.log.file_enabled") is False
    assert "heartbeat" in chassis.registry.names()
    assert chassis.paths.log_dir == (project_root / "var" / "log")
    report = chassis.health_report()
    for key in ("app", "runtime", "paths", "config", "logging", "modules", "signals", "bus"):
        assert key in report
    assert report["app"]["version"]
    chassis.signals.uninstall()


def test_full_lifecycle_and_clean_shutdown(project_root) -> None:
    chassis = build_chassis(project_root)
    chassis.initialize()
    assert chassis.state is ChassisState.INITIALIZED
    chassis.start()
    assert chassis.state is ChassisState.RUNNING
    heartbeat = chassis.registry.get("heartbeat")
    assert heartbeat.state.value == "running"
    code = chassis.run_once()
    assert code == int(ExitCode.SUCCESS)
    assert chassis.shutdown() == int(ExitCode.SUCCESS)
    assert chassis.state is ChassisState.STOPPED
    assert heartbeat.state.value == "stopped"
    assert heartbeat.stop_ms is not None


def test_run_forever_honours_duration(project_root) -> None:
    chassis = build_chassis(project_root)
    chassis.initialize().start()
    code = chassis.run_forever(duration=0.12)
    assert code == int(ExitCode.SUCCESS)
    assert chassis.bus.stats()["published"] > 1
    chassis.shutdown()


def test_signal_abort_maps_to_exit_130(project_root) -> None:
    chassis = build_chassis(project_root)
    chassis.initialize().start()
    chassis.signals.request_shutdown(signal.SIGINT)
    code = chassis.run_forever(duration=5.0)
    assert code == int(ExitCode.ABORTED) == 130
    assert chassis.shutdown() == int(ExitCode.ABORTED)


def test_sigterm_maps_to_143(project_root) -> None:
    chassis = build_chassis(project_root)
    chassis.signals.request_shutdown(getattr(signal, "SIGTERM", 15))
    assert int(chassis.exit_code) == 143
    chassis.shutdown()


def test_failed_module_marks_chassis_failed(project_root) -> None:
    chassis = build_chassis(project_root)
    from korodexios_core.kernel.contract import ModuleSpec

    from .conftest import ScriptedModule

    chassis.registry.register(
        ScriptedModule("boom", fail_phase="init"),
        ModuleSpec(name="boom", description="Boom.", version="0.1.0", origin="test"),
    )
    chassis.initialize()
    assert chassis.state is ChassisState.FAILED
    assert int(chassis.exit_code) == int(ExitCode.ERROR)
    chassis.start()
    assert chassis.shutdown() == int(ExitCode.ERROR)


def test_non_tty_stdin_disables_interactive_mode(project_root, monkeypatch) -> None:
    import sys

    class FakeTTY:
        def __init__(self, value: bool) -> None:
            self._value = value

        def isatty(self) -> bool:
            return self._value

    monkeypatch.setattr(sys, "stdin", FakeTTY(True))
    chassis = build_chassis(project_root)
    assert chassis.config.get_bool("core.cli.interactive") is True
    chassis.shutdown()

    monkeypatch.setattr(sys, "stdin", FakeTTY(False))
    chassis = build_chassis(project_root)
    assert chassis.config.get_bool("core.cli.interactive") is False
    assert chassis.config.source_of("core.cli.interactive").name == "CLI"
    chassis.shutdown()


def test_chassis_as_context_manager(project_root) -> None:
    chassis = build_chassis(project_root)
    chassis.initialize().start()
    with chassis:
        assert chassis.state is ChassisState.RUNNING
    assert chassis.state is ChassisState.STOPPED
