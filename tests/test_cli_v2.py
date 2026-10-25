"""Core API v2 console surface: masking, schema checks, reload, rpc, remote delegation."""

from __future__ import annotations

import json
from pathlib import Path

from korodexios_core.cli.entry import (
    _cli_overrides,
    _remote_command_path,
    build_bootstrap_parser,
    main,
)
from korodexios_core.constants import ExitCode
from korodexios_core.kernel.chassis import Chassis, ChassisOptions

DAEMON_CONFIG = """
core:
  name: v2cli
  log:
    level: INFO
    file_enabled: false
  lifecycle:
    tick_interval: 0.05
    stop_timeout: 2.0
  ipc:
    enabled: true
    port: 0
  modules:
    autodiscover: true
"""


def base_args(project_root: Path) -> list[str]:
    return ["--home", str(project_root), "--no-log-file", "--no-color"]


def write_root(root: Path, text: str = DAEMON_CONFIG) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "kdx.yml").write_text(text, encoding="utf-8")
    return root


def test_new_global_flags_map_to_config_keys() -> None:
    namespace, _ = build_bootstrap_parser().parse_known_args(
        [
            "--asyncio",
            "--no-instance-lock",
            "--env-alias",
            "LEGACY_TOKEN=core.ipc.token",
            "--env-alias",
            "OLD_LEVEL=core.log.level",
        ]
    )
    overrides = _cli_overrides(namespace)
    assert overrides["core.async.enabled"] is True
    assert overrides["core.instance.lock"] is False
    assert overrides["core.env.aliases"] == {
        "LEGACY_TOKEN": "core.ipc.token",
        "OLD_LEVEL": "core.log.level",
    }

    namespace, _ = build_bootstrap_parser().parse_known_args(["--no-asyncio", "--instance-lock"])
    overrides = _cli_overrides(namespace)
    assert overrides["core.async.enabled"] is False
    assert overrides["core.instance.lock"] is True


def test_remote_command_path_splits_command_from_tokens() -> None:
    assert _remote_command_path(["heartbeat", "status", "-n", "5"]) == (
        "heartbeat status",
        ["-n", "5"],
    )
    assert _remote_command_path(["rpc", "heartbeat", "status"]) == ("heartbeat status", [])
    assert _remote_command_path([]) == ("", [])
    assert _remote_command_path(["--json"]) == ("", ["--json"])


def test_config_show_masks_secrets_by_default(capsys, project_root) -> None:
    write_root(
        project_root,
        DAEMON_CONFIG.replace(
            "  modules:\n    autodiscover: true",
            "  ipc:\n    token: supersecret\n  modules:\n    autodiscover: false",
        ).replace("    port: 0\n", "    port: 0\n"),
    )
    assert main([*base_args(project_root), "config", "show", "--key", "core.ipc"]) == int(
        ExitCode.SUCCESS
    )
    captured = capsys.readouterr()
    assert "supersecret" not in captured.out
    assert "***redacted***" in captured.out
    assert "--reveal" in captured.out

    assert main(
        [*base_args(project_root), "config", "show", "--key", "core.ipc", "--reveal"]
    ) == int(ExitCode.SUCCESS)
    revealed = capsys.readouterr()
    assert "supersecret" in revealed.out
    assert "***redacted***" not in revealed.out

    assert main(
        [*base_args(project_root), "config", "show", "--key", "core.ipc", "--json"]
    ) == int(ExitCode.SUCCESS)
    payload = json.loads(capsys.readouterr().out)
    assert payload["core.ipc.token"]["value"] == "***redacted***"


def test_config_check_reports_module_schemas(capsys, project_root) -> None:
    write_root(project_root)
    assert main([*base_args(project_root), "config", "check"]) == int(ExitCode.SUCCESS)
    captured = capsys.readouterr()
    assert "Module configuration schemas" in captured.out

    assert main([*base_args(project_root), "config", "check", "--json"]) == int(ExitCode.SUCCESS)
    payload = json.loads(capsys.readouterr().out)
    # v0.2.2: the built-in proof modules declare their own scoped schemas
    assert payload["schemas"]["modules"] == 3
    assert payload["schemas"]["issues"] == []
    # v0.2.4 added the load watcher keys, v0.2.5 the sweep keys, v0.2.7 the retention keys.
    for declared in ("backup_engine: 10 keys", "heartbeat: 1 keys", "system_info: 4 keys"):
        assert declared in captured.out, declared


SCHEMA_MODULE = """
from korodexios_core.config.schema import ConfigField, ConfigKind, ConfigSchema
from korodexios_core.kernel.contract import Module


class ProbeModule(Module):
    name = "probe"
    description = "Declares a configuration schema."
    version = "0.1.0"

    def config_schema(self):
        return ConfigSchema(
            module=self.name,
            fields=(
                ConfigField("dsn", ConfigKind.STR, secret=True, required=True),
                ConfigField("retries", ConfigKind.INT, default=3),
            ),
        )


MODULE = ProbeModule()
"""


def test_config_check_fails_on_a_schema_violation(capsys, project_root) -> None:
    """A discovered module's schema is enforced by 'kdx config check'."""
    write_root(project_root)
    modules = project_root / "probes"
    modules.mkdir(parents=True, exist_ok=True)
    (modules / "probe.py").write_text(SCHEMA_MODULE, encoding="utf-8")
    args = [*base_args(project_root), "--modules-dir", str(modules)]
    assert main([*args, "config", "check"]) == int(ExitCode.CONFIG_ERROR)
    captured = capsys.readouterr()
    assert "modules.probe.dsn" in captured.out
    assert "dsn is required by module probe" in captured.out
    assert "[unset]" in captured.out

    (project_root / "kdx.local.yml").write_text(
        "modules:\n  probe:\n    dsn: postgres://user:pass@localhost/db\n    retries: 5\n",
        encoding="utf-8",
    )
    assert main([*args, "config", "check"]) == int(ExitCode.SUCCESS)
    captured = capsys.readouterr()
    assert "probe: 2 keys" in captured.out

    assert main([*args, "config", "check", "--json"]) == int(ExitCode.SUCCESS)
    payload = json.loads(capsys.readouterr().out)
    assert payload["schemas"]["modules"] == 4  # probe plus the three built-in schemas
    assert payload["schemas"]["issues"] == []


def test_reload_without_a_daemon(capsys, project_root) -> None:
    write_root(project_root)
    assert main([*base_args(project_root), "reload"]) == int(ExitCode.SUCCESS)
    captured = capsys.readouterr()
    assert "chassis daemon is not running" in captured.out
    assert "SIGHUP" in captured.out


def test_rpc_without_a_daemon(capsys, project_root) -> None:
    write_root(project_root)
    assert main([*base_args(project_root), "rpc", "heartbeat", "status"]) == int(ExitCode.ERROR)
    captured = capsys.readouterr()
    assert "no chassis daemon answering" in captured.out


def test_rpc_needs_a_command_and_valid_args(capsys, project_root) -> None:
    write_root(project_root)
    assert main([*base_args(project_root), "rpc"]) == int(ExitCode.USAGE_ERROR)
    captured = capsys.readouterr()
    assert "needs a command to delegate" in captured.err + captured.out

    assert main([*base_args(project_root), "rpc", "heartbeat", "--args", "not json"]) == int(
        ExitCode.USAGE_ERROR
    )
    captured = capsys.readouterr()
    assert "not valid JSON" in captured.out

    assert main(
        [*base_args(project_root), "rpc", "heartbeat", "--args", "[1, 2]"]
    ) == int(ExitCode.USAGE_ERROR)
    assert "must be a JSON object" in capsys.readouterr().out


def test_remote_delegation_needs_a_command(capsys, project_root) -> None:
    write_root(project_root)
    assert main([*base_args(project_root), "--remote"]) == int(ExitCode.USAGE_ERROR)
    captured = capsys.readouterr()
    assert "needs a command to delegate" in captured.err

    assert main([*base_args(project_root), "--remote", "heartbeat", "status"]) == int(
        ExitCode.ERROR
    )
    captured = capsys.readouterr()
    assert "no chassis daemon answering" in captured.err


def test_doctor_renders_the_new_sections(capsys, project_root) -> None:
    write_root(project_root)
    assert main([*base_args(project_root), "doctor"]) == int(ExitCode.SUCCESS)
    captured = capsys.readouterr()
    assert "Instance and concurrency" in captured.out
    assert "Diagnostics" in captured.out
    assert "instance lock" in captured.out
    assert "workers watched" in captured.out

    assert main([*base_args(project_root), "doctor", "--json"]) == int(ExitCode.SUCCESS)
    payload = json.loads(capsys.readouterr().out)
    for key in ("instance", "async", "supervision", "diagnostics"):
        assert key in payload, key
    assert payload["instance"]["enabled"] is True
    assert payload["diagnostics"]["total"] >= 0


def test_rpc_and_reload_against_a_live_daemon(project_root: Path) -> None:
    """The daemon answers rpc, reload, and --remote without any client booting a loop."""
    write_root(project_root)
    daemon = Chassis.create(
        ChassisOptions(home=project_root, environ={}, install_signals=False)
    )
    daemon.start()  # no CLI surface: initialize() collected the command tree
    try:
        discovery = project_root / "var" / "chassis.ipc"
        assert discovery.exists()
        code = main([*base_args(project_root), "rpc", "heartbeat", "status", "--json"])
        assert code == int(ExitCode.SUCCESS)
        code = main([*base_args(project_root), "reload", "--json"])
        assert code == int(ExitCode.SUCCESS)
        code = main([*base_args(project_root), "--remote", "heartbeat", "status"])
        assert code == int(ExitCode.SUCCESS)
        code = main([*base_args(project_root), "rpc", "nope", "nothing"])
        assert code == int(ExitCode.ERROR)
    finally:
        daemon.shutdown()
        daemon.signals.uninstall()
