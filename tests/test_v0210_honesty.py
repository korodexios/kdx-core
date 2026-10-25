"""v0.2.10: both alias shapes, the loud version gate, and boot honesty about modules."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from korodexios_core import Chassis, ChassisOptions
from korodexios_core.cli.entry import main
from korodexios_core.config.layered import LayeredConfig
from korodexios_core.constants import ExitCode
from korodexios_core.kernel.discovery import (
    DiscoveryOptional,
    classify_import_error,
    foreign_roots_from,
)

BASE_CONFIG = """
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
"""

MAPPING_ALIASES = """
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
      LEGACY_LEVEL: core.log.level
      LEGACY_HOME: core.home
"""

PREFIXED_ALIASES = """
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
    prefix: LLMWIKI
    aliases: {}
"""

STALE_SOURCE = """
from korodexios_core.kernel.contract import Module


class Stale(Module):
    name = "stale"
    description = "Needs a core version this chassis is not."
    version = "1.0.0"


MODULE = Stale()
"""

STALE_MANIFEST = """
[module]
name = "stale"
description = "Needs a core version this chassis is not."
version = "1.0.0"
entrypoint = "MODULE"
requires_core = "==99.99.99"
"""

SNEAKY_SOURCE = """
from korodexios_core.kernel.contract import Module


class Sneaky(Module):
    name = "sneaky"
    description = "Declares enabled as its own behaviour switch."
    version = "1.0.0"

    def config_schema(self) -> dict[str, str]:
        return {"enabled": "bool", "threshold": "float"}


MODULE = Sneaky()
"""

SNEAKY_MANIFEST = """
[module]
name = "sneaky"
description = "Declares enabled as its own behaviour switch."
version = "1.0.0"
entrypoint = "MODULE"
"""

DISABLED_MANIFEST = """
[module]
name = "retired"
description = "Turned off in its own manifest."
version = "1.0.0"
entrypoint = "MODULE"
enabled = false
"""


def base_args(project_root: Path) -> list[str]:
    """Return the global flags every CLI test in this file needs."""
    return ["--home", str(project_root), "--no-log-file", "--no-color"]


def write_module(directory: Path, name: str, *, source: str, manifest: str) -> Path:
    """Write one importable module package with a manifest."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "__init__.py").write_text(source.lstrip(), encoding="utf-8")
    (directory / "kdx_module.toml").write_text(manifest.lstrip(), encoding="utf-8")
    return directory


def config_text(*, strict: bool = False, extra_module_lines: str = "") -> str:
    """Return a project configuration with the discovery gate optionally armed."""
    strict_lines = "    strict_discovery: true\n" if strict else ""
    return BASE_CONFIG.replace(
        "    extra_paths: []\n",
        f"    extra_paths: []\n{strict_lines}",
    ) + extra_module_lines


def load_json(captured: str) -> dict[str, Any]:
    """Return the JSON payload a CLI command printed."""
    return json.loads(captured)


# R1: both environment alias shapes work at the same time.


def test_mapping_alias_shape_feeds_the_chassis(tmp_path: Path) -> None:
    """A mapping written in kdx.yml is flattened to leaves and still resolves."""
    config_file = tmp_path / "kdx.yml"
    config_file.write_text(MAPPING_ALIASES, encoding="utf-8")
    config = LayeredConfig.load(
        config_file=config_file, env_file=None, environ={"LEGACY_LEVEL": "DEBUG"}
    )
    assert config.get_str("core.log.level") == "DEBUG"
    assert config.origin_of("core.log.level") == "environment alias LEGACY_LEVEL"
    assert config.env_aliases() == {
        "LEGACY_LEVEL": "core.log.level",
        "LEGACY_HOME": "core.home",
    }


def test_environment_list_shape_feeds_the_chassis(tmp_path: Path) -> None:
    """The NAME=key list an operator exports resolves with the same provenance."""
    config = LayeredConfig.load(
        config_file=None,
        env_file=None,
        environ={
            "KDX_CORE__ENV__ALIASES": "LEGACY_LEVEL=core.log.level",
            "LEGACY_LEVEL": "DEBUG",
        },
    )
    assert config.get_str("core.env.aliases") == "LEGACY_LEVEL=core.log.level"
    assert config.get_str("core.log.level") == "DEBUG"
    assert config.origin_of("core.log.level") == "environment alias LEGACY_LEVEL"
    assert config.env_aliases() == {"LEGACY_LEVEL": "core.log.level"}


def test_a_prefixed_variable_outranks_the_alias(tmp_path: Path) -> None:
    """A real KDX_ value still wins over an alias mapped to the same key."""
    config_file = tmp_path / "kdx.yml"
    config_file.write_text(MAPPING_ALIASES, encoding="utf-8")
    config = LayeredConfig.load(
        config_file=config_file,
        env_file=None,
        environ={"KDX_CORE__LOG__LEVEL": "WARNING", "LEGACY_LEVEL": "DEBUG"},
    )
    assert config.get_str("core.log.level") == "WARNING"
    assert config.origin_of("core.log.level") == "process environment"


def test_a_private_prefix_reads_its_own_alias_variable(tmp_path: Path) -> None:
    """core.env.prefix: LLMWIKI means LLMWIKI_CORE__ENV__ALIASES, not only KDX_."""
    config_file = tmp_path / "kdx.yml"
    config_file.write_text(PREFIXED_ALIASES, encoding="utf-8")
    config = LayeredConfig.load(
        config_file=config_file,
        env_file=None,
        environ={
            "LLMWIKI_CORE__ENV__ALIASES": "LEGACY_LEVEL=core.log.level",
            "LEGACY_LEVEL": "DEBUG",
        },
    )
    assert config.get_str("core.log.level") == "DEBUG"
    assert config.env_aliases() == {"LEGACY_LEVEL": "core.log.level"}


def test_doctor_reports_the_effective_aliases_in_either_shape(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    """The status snapshot is shape-tolerant, so doctor never raises on a string."""
    (tmp_path / "kdx.yml").write_text(BASE_CONFIG, encoding="utf-8")
    monkeypatch.setenv("KDX_CORE__ENV__ALIASES", "LEGACY_LEVEL=core.log.level")
    monkeypatch.setenv("LEGACY_LEVEL", "DEBUG")
    assert main([*base_args(tmp_path), "config", "check"]) == int(ExitCode.SUCCESS)
    assert main([*base_args(tmp_path), "doctor"]) == int(ExitCode.SUCCESS)
    capsys.readouterr()
    assert main([*base_args(tmp_path), "doctor", "--json"]) == int(ExitCode.SUCCESS)
    report = load_json(capsys.readouterr().out)
    assert report["config"]["aliases"] == {"LEGACY_LEVEL": "core.log.level"}
    assert main([*base_args(tmp_path), "config", "check"]) == int(ExitCode.SUCCESS)


# R2: a loud core-version gate.


def test_strict_discovery_refuses_the_boot_with_a_configuration_error(
    tmp_path: Path,
    capsys,
) -> None:
    """With the gate on, a requires_core mismatch ends the boot at exit 3."""
    root = tmp_path / "strict"
    root.mkdir()
    (root / "kdx.yml").write_text(config_text(strict=True), encoding="utf-8")
    write_module(
        root / "modules" / "stale", "stale", source=STALE_SOURCE, manifest=STALE_MANIFEST
    )
    args = [*base_args(root), "--modules-dir", str(root / "modules")]
    assert main([*args, "run", "--once"]) == int(ExitCode.CONFIG_ERROR)
    captured = capsys.readouterr()
    assert "needs core ==99.99.99" in captured.err
    assert "module discovery refused the boot" in captured.err
    assert main([*args, "doctor"]) == int(ExitCode.ERROR)
    capsys.readouterr()
    assert main([*args, "doctor", "--json"]) == int(ExitCode.SUCCESS)
    report = load_json(capsys.readouterr().out)
    severities = [
        check["severity"]
        for check in report["diagnostics"]["checks"]
        if check["name"] == "discovery"
    ]
    assert severities == ["error"]


def test_discovery_failure_stays_a_warning_while_the_gate_is_off(
    tmp_path: Path,
    capsys,
) -> None:
    """The default keeps booting: the mismatch is a warning, and run --once exits 0."""
    root = tmp_path / "lenient"
    root.mkdir()
    (root / "kdx.yml").write_text(config_text(), encoding="utf-8")
    write_module(
        root / "modules" / "stale", "stale", source=STALE_SOURCE, manifest=STALE_MANIFEST
    )
    args = [*base_args(root), "--modules-dir", str(root / "modules")]
    assert main([*args, "run", "--once"]) == int(ExitCode.SUCCESS)
    assert main([*args, "doctor"]) == int(ExitCode.SUCCESS)
    capsys.readouterr()
    assert main([*args, "doctor", "--json"]) == int(ExitCode.SUCCESS)
    report = load_json(capsys.readouterr().out)
    severities = [
        check["severity"]
        for check in report["diagnostics"]["checks"]
        if check["name"] == "discovery"
    ]
    assert severities == ["warning"]


# R6: boot-time honesty about modules.


def test_module_declaring_a_chassis_owned_key_is_warned_about(tmp_path: Path) -> None:
    """A module whose schema declares 'enabled' hands the chassis a lifecycle key."""
    root = tmp_path / "collision"
    root.mkdir()
    (root / "kdx.yml").write_text(config_text(), encoding="utf-8")
    write_module(
        root / "modules" / "sneaky", "sneaky", source=SNEAKY_SOURCE, manifest=SNEAKY_MANIFEST
    )
    chassis = Chassis.create(
        ChassisOptions(
            home=root,
            environ={},
            extra_module_dirs=[root / "modules"],
            install_signals=False,
            utf8_streams=False,
        )
    )
    try:
        chassis.ensure_initialized()
        assert chassis.registry.settings_collisions() == {"sneaky": ("enabled",)}
        checks = [
            check
            for check in chassis.health_report()["diagnostics"]["checks"]
            if check["name"] == "module-setting"
        ]
        assert checks and checks[0]["severity"] == "warning"
        assert "modules.sneaky.enabled" in checks[0]["message"]
    finally:
        chassis.shutdown()


def test_skipped_modules_are_named_with_the_reason_that_decided_it(
    tmp_path: Path,
    capsys,
) -> None:
    """Manifest and configuration each name their own reason."""
    root = tmp_path / "skipped"
    root.mkdir()
    config = config_text(extra_module_lines="modules:\n  sneaky:\n    enabled: false\n")
    (root / "kdx.yml").write_text(config, encoding="utf-8")
    plugins = root / "modules"
    write_module(plugins / "sneaky", "sneaky", source=SNEAKY_SOURCE, manifest=SNEAKY_MANIFEST)
    write_module(plugins / "retired", "retired", source=SNEAKY_SOURCE, manifest=DISABLED_MANIFEST)
    args = [*base_args(root), "--modules-dir", str(root / "modules")]
    assert main([*args, "modules", "list"]) == int(ExitCode.SUCCESS)
    captured = capsys.readouterr()
    assert "modules.sneaky.enabled is false" in captured.out
    assert "disabled by its manifest" in captured.out
    capsys.readouterr()
    assert main([*args, "modules", "list", "--json"]) == int(ExitCode.SUCCESS)
    payload = load_json(capsys.readouterr().out)
    assert {"skipped", "optional", "failures"} <= set(payload)
    assert ("sneaky", "modules.sneaky.enabled is false") in [
        (row["name"], row["reason"]) for row in payload["skipped"]
    ]


# R7: a namespace convention for plugins shipped as an installed distribution.


def _missing(name: str) -> ModuleNotFoundError:
    error = ModuleNotFoundError(f"No module named {name!r}")
    error.name = name
    return error


def test_a_declared_foreign_root_keeps_the_optional_hint() -> None:
    """A root the project declared foreign is an installation question, not its own code."""
    own_root = classify_import_error(_missing("llmwiki_ext.plugins.x"), "llmwiki_ext")
    assert own_root is None
    declared = classify_import_error(
        _missing("llmwiki_ext.plugins.x"), "llmwiki_ext", foreign_roots=("llmwiki_ext",)
    )
    assert isinstance(declared, DiscoveryOptional)
    assert declared.module == "llmwiki_ext.plugins.x"
    assert "llmwiki_ext" in declared.hint


def test_undeclared_roots_keep_their_current_verdict() -> None:
    """Nothing is foreign until the project declares it: the chassis roots stay hard."""
    unknown = classify_import_error(_missing("llmwiki_ext.plugins.x"), "trigger")
    assert isinstance(unknown, DiscoveryOptional)
    assert classify_import_error(_missing("korodexios_ext.ledger"), "trigger") is None
    assert classify_import_error(_missing("korodexios_core.paths"), "trigger") is None


def test_foreign_roots_are_read_through_configuration(tmp_path: Path) -> None:
    """One variable can carry the set, because the list getter splits it."""
    config_file = tmp_path / "kdx.yml"
    config_file.write_text(
        BASE_CONFIG.replace(
            "    extra_paths: []\n",
            "    extra_paths: []\n    foreign_roots:\n      - llmwiki_ext\n",
        ),
        encoding="utf-8",
    )
    from_file = LayeredConfig.load(config_file=config_file, env_file=None, environ={})
    assert foreign_roots_from(from_file) == ("llmwiki_ext",)
    from_env = LayeredConfig.load(
        config_file=config_file,
        env_file=None,
        environ={"KDX_CORE__MODULES__FOREIGN_ROOTS": "llmwiki_ext, other_ext"},
    )
    assert foreign_roots_from(from_env) == ("llmwiki_ext", "other_ext")
    assert foreign_roots_from(None) == ()


# R8: uv-first bootstrap.


def test_init_next_step_leads_with_uv(tmp_path: Path, capsys) -> None:
    """The seeded project's next step is uv, with a one-line pip fallback."""
    target = tmp_path / "uv_next"
    args = [
        *base_args(tmp_path),
        "--yes",
        "init",
        "uv_next",
        "--path",
        str(target),
        "--minimal",
    ]
    assert main(args) == int(ExitCode.SUCCESS)
    captured = capsys.readouterr()
    assert "uv sync --extra dev" in captured.out
    assert "uv run uv-next run" in captured.out
    assert "without uv" in captured.out
