"""v0.2.1 fixes: mapping subkeys, hook ordering, and verdict exit codes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from korodexios_core import Chassis, ChassisOptions
from korodexios_core.cli.entry import main
from korodexios_core.config import keys as key_rules
from korodexios_core.constants import ExitCode
from korodexios_core.kernel.contract import Module, ModuleSpec
from korodexios_core.kernel.diagnostics import DiagnosticCheck
from korodexios_core.kernel.registry import ModuleRegistry

from .conftest import PROJECT_CONFIG

LOADED = ["heartbeat", "system_info", "backup_engine"]
KNOWN = [
    "core.ipc.port",
    "core.log.level",
    "core.env.aliases.LEGACY_RUN_MODE",
    "modules.heartbeat.interval",
    "modules.heartbeat.lines",
    "modules.backup_engine.destination",
]

ALIASED_CONFIG = """
core:
  log:
    level: INFO
    file_enabled: false
  modules:
    autodiscover: false
    extra_paths: []
  lifecycle:
    tick_interval: 0.05
    stop_timeout: 2.0
  ipc:
    enabled: false
  env:
    aliases:
      LEGACY_RUN_MODE: core.log.level
      LEGACY_HOME: core.home
"""


def base_args(project_root: Path) -> list[str]:
    """Return the global flags every CLI test in this file needs."""
    return ["--home", str(project_root), "--no-log-file", "--no-color"]


class RecordingLogger:
    """Registry logger double that remembers the level of every message."""

    def __init__(self) -> None:
        self.levels: list[str] = []

    def _emit(self, level: str, *args: Any, **kwargs: Any) -> None:
        self.levels.append(level)

    def __getattr__(self, name: str) -> Any:
        def _hook(*args: Any, **kwargs: Any) -> None:
            self._emit(name, *args)

        return _hook


class ContextReadingModule(Module):
    """A module whose declarative hooks read the context they were handed."""

    name = "eager"
    description = "Reads configuration in every declarative hook."
    version = "0.1.0"

    def diagnostics(self) -> tuple[Any, ...]:
        level = str(self.config.get("level", "INFO"))
        return (DiagnosticCheck(name="log-level", message=f"log level is {level}"),)

    def register_cli(self, surface: Any) -> None:
        pass


def make_spec(name: str = "eager") -> ModuleSpec:
    return ModuleSpec(
        name=name,
        description=f"{name} module.",
        version="0.1.0",
        priority=100,
        origin="test",
    )


# --------------------------------------------------------------------------- G11


def test_mapping_default_subkeys_are_valid_chassis_paths() -> None:
    assert key_rules.MAPPING_PREFIXES == ("core.env.aliases",)
    assert key_rules.mapping_subkey("core.env.aliases.LEGACY_RUN_MODE") == "core.env.aliases"
    assert key_rules.is_core_key("core.env.aliases.LEGACY_RUN_MODE") is True
    assert key_rules.is_known_key("core.env.aliases.LEGACY_RUN_MODE", LOADED) is True


def test_malformed_alias_subkeys_stay_unknown() -> None:
    assert key_rules.mapping_subkey("core.env.aliases.") == ""
    assert key_rules.mapping_subkey("core.env.aliases.LEGACY.mode") == ""
    assert key_rules.is_core_key("core.env.aliases.") is False


def test_alias_subkeys_pass_config_check(tmp_path: Path, capsys: Any) -> None:
    (tmp_path / "kdx.yml").write_text(ALIASED_CONFIG, encoding="utf-8")
    assert main([*base_args(tmp_path), "config", "check"]) == int(ExitCode.SUCCESS)
    capsys.readouterr()  # the first run renders for a human; the next one does not
    main([*base_args(tmp_path), "config", "check", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["checked"] > 0
    assert [item["key"] for item in report["unknown"]] == []


def test_suggestions_stay_inside_the_module_they_name() -> None:
    named = key_rules.suggest("modules.heartbeat.intervl", LOADED, KNOWN)
    assert named == "modules.heartbeat.interval"
    assert key_rules.suggest("modules.backup_engine.destinaton", LOADED, KNOWN) == (
        "modules.backup_engine.destination"
    )
    assert key_rules.suggest("modules.backup.root", LOADED, KNOWN) == "modules.backup_engine"
    assert key_rules.suggest("modules.metrics.interval", LOADED, KNOWN) == ""
    assert key_rules.suggest("core.log.levle", LOADED, KNOWN) == "core.log.level"


def test_unknown_keys_scope_suggestions_with_the_keys_they_check() -> None:
    found = key_rules.unknown_keys([*KNOWN, "core.log.levle"], LOADED)
    assert found == [("core.log.levle", "core.log.level")]


# --------------------------------------------------------------------------- G12


def test_hooks_read_the_context_after_registration() -> None:
    logger = RecordingLogger()
    registry = ModuleRegistry(logger)
    record = registry.register(ContextReadingModule(), make_spec())
    assert record.checks == ()
    assert "error" not in logger.levels
    assert "debug" in logger.levels


def test_declarations_are_collected_again_after_init(project_root: Path) -> None:
    chassis = Chassis.create(
        ChassisOptions(
            home=project_root,
            environ={},
            cli_overrides={"core.log.file_enabled": False, "core.ipc.enabled": False},
        )
    )
    chassis.registry.register(ContextReadingModule(), make_spec())
    before = chassis.registry.require("eager")
    assert before.checks == ()
    assert before.declarations_after_init is False
    chassis.initialize().shutdown()
    after = chassis.registry.require("eager")
    assert [check.name for check in after.checks] == ["log-level"]
    assert after.declarations_after_init is True


VERDICT_MODULE = """
\"\"\"A discovered module that reads its context in every optional hook.\"\"\"

from korodexios_core import CliCommand, DiagnosticCheck, Module

NEEDS_REINDEX = 4


class VerdictModule(Module):
    name = "verdict"
    description = "Reports a verdict code and context-dependent diagnostics."
    version = "0.1.0"

    def diagnostics(self):
        level = str(self.config.get("level", "INFO"))
        return (DiagnosticCheck(name="log-level", message=f"log level is {level}"),)

    def health(self):
        report = super().health()
        report["durable_dir"] = str(self.require_context().paths.durable_dir)
        return report

    def register_cli(self, surface):
        surface.add_command(
            CliCommand(
                name="verdict",
                help="Report the reindex verdict.",
                module_name=self.name,
                handler=lambda request: NEEDS_REINDEX,
            )
        )


MODULE = VerdictModule()
"""

VERDICT_MANIFEST = """
[module]
name = "verdict"
description = "Reports a verdict code and context-dependent diagnostics."
version = "0.1.0"
entrypoint = "module.MODULE"
priority = 20
"""


def verdict_project(tmp_path: Path) -> Path:
    """Write a project whose only module reads its context in the optional hooks."""
    config = PROJECT_CONFIG.replace(
        "    extra_paths: []",
        "    extra_paths:\n      - modules",
    ).replace("modules:\n  scripted:\n    enabled: true\n", "")
    (tmp_path / "kdx.yml").write_text(config, encoding="utf-8")
    package = tmp_path / "modules"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    verdict = package / "verdict"
    verdict.mkdir()
    (verdict / "__init__.py").write_text("from . import module\n", encoding="utf-8")
    (verdict / "module.py").write_text(VERDICT_MODULE, encoding="utf-8")
    (verdict / "kdx_module.toml").write_text(VERDICT_MANIFEST, encoding="utf-8")
    return tmp_path


def test_doctor_asks_hooks_after_init(tmp_path: Path, capsys: Any) -> None:
    verdict_project(tmp_path)
    assert main([*base_args(tmp_path), "doctor", "--json"]) == int(ExitCode.SUCCESS)
    report = json.loads(capsys.readouterr().out)
    modules = {item["name"]: item for item in report["modules"]["modules"]}
    verdict = modules["verdict"]
    assert [check["name"] for check in verdict["diagnostics"]] == ["log-level"]
    assert verdict["declarations_after_init"] is True


def test_modules_show_reports_health_without_error(tmp_path: Path, capsys: Any) -> None:
    verdict_project(tmp_path)
    assert main([*base_args(tmp_path), "modules", "show", "verdict", "--json"]) == int(
        ExitCode.SUCCESS
    )
    payload = json.loads(capsys.readouterr().out)
    detail = payload[0]
    assert "health_error" not in detail
    assert str(detail["health"]["durable_dir"]).endswith("data")


# ---------------------------------------------------------------------------- G4


def test_handler_verdict_code_passes_through_main(tmp_path: Path) -> None:
    verdict_project(tmp_path)
    assert main([*base_args(tmp_path), "verdict"]) == 4


def test_core_commands_keep_their_exit_codes(tmp_path: Path) -> None:
    verdict_project(tmp_path)
    assert main([*base_args(tmp_path), "config", "show"]) == int(ExitCode.SUCCESS)
    assert main([*base_args(tmp_path), "rpc"]) == int(ExitCode.USAGE_ERROR)
