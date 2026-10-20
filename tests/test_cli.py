"""End-to-end CLI tests: the console client is a thin layer over the chassis."""

from __future__ import annotations

import json
import sys

import pytest

from korodexios_core.cli.entry import console_prog, main
from korodexios_core.constants import ExitCode


def base_args(project_root) -> list[str]:
    return ["--home", str(project_root), "--no-log-file", "--no-color"]


def test_version_flag(capsys, project_root) -> None:
    assert main(["--version"]) == int(ExitCode.SUCCESS)
    captured = capsys.readouterr()
    assert "korodexios-core" in captured.out
    assert captured.err == ""


def test_console_prog_follows_the_invoked_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["/usr/local/bin/kdx", "--version"])
    assert console_prog() == "kdx"
    monkeypatch.setattr(sys, "argv", ["/usr/local/bin/korodexios", "status"])
    assert console_prog() == "korodexios"
    monkeypatch.setattr(sys, "argv", ["__main__.py", "status"])
    assert console_prog() == "korodex"


def test_no_command_prints_help(capsys, project_root) -> None:
    assert main(base_args(project_root)) == int(ExitCode.SUCCESS)
    captured = capsys.readouterr()
    assert "usage: korodex" in captured.out


def test_modules_list_shows_builtin(capsys, project_root) -> None:
    args = [*base_args(project_root), "modules", "list"]
    assert main(args) == int(ExitCode.SUCCESS)
    captured = capsys.readouterr()
    assert "heartbeat" in captured.out
    assert "builtin" in captured.out


def test_config_show_reports_provenance(capsys, project_root) -> None:
    args = [*base_args(project_root), "config", "show", "--key", "core.log"]
    assert main(args) == int(ExitCode.SUCCESS)
    captured = capsys.readouterr()
    assert "core.log.level" in captured.out
    assert "config file" in captured.out


def test_config_file_reports_paths(capsys, project_root) -> None:
    args = [*base_args(project_root), "config", "file"]
    assert main(args) == int(ExitCode.SUCCESS)
    assert str(project_root) in capsys.readouterr().out


def test_doctor_json_is_parseable(capsys, project_root) -> None:
    args = [*base_args(project_root), "doctor", "--json"]
    assert main(args) == int(ExitCode.SUCCESS)
    report = json.loads(capsys.readouterr().out)
    assert report["app"]["distribution"] == "korodexios-core"
    assert any(item["name"] == "heartbeat" for item in report["modules"]["modules"])


def test_run_once_boots_and_stops(capsys, project_root) -> None:
    args = [*base_args(project_root), "run", "--once"]
    assert main(args) == int(ExitCode.SUCCESS)
    captured = capsys.readouterr()
    assert "chassis prepared" not in captured.out  # diagnostics stay on stderr
    assert "chassis stopped" not in captured.out


def test_module_command_is_injected(capsys, project_root) -> None:
    args = [*base_args(project_root), "heartbeat", "status"]
    assert main(args) == int(ExitCode.SUCCESS)
    captured = capsys.readouterr()
    assert "heartbeat" in captured.out


def test_unknown_command_exits_with_usage_error(capsys, project_root) -> None:
    args = [*base_args(project_root), "definitely-not-a-command"]
    assert main(args) == int(ExitCode.USAGE_ERROR)
    assert "invalid choice" in capsys.readouterr().err


def test_missing_config_override_exits_with_config_error(capsys, tmp_path) -> None:
    args = ["--home", str(tmp_path), "--config", str(tmp_path / "nope.yml"), "version"]
    assert main(args) == int(ExitCode.CONFIG_ERROR)
    assert "[!]" in capsys.readouterr().err


def test_wizard_runs_non_interactively(capsys, project_root) -> None:
    args = [*base_args(project_root), "wizard", "--yes"]
    assert main(args) == int(ExitCode.SUCCESS)
    captured = capsys.readouterr()
    assert "korodexios-core wizard" in captured.out
    assert "default [1]" in captured.out


def test_help_lists_module_and_wizard_commands(capsys, project_root) -> None:
    assert main([*base_args(project_root), "--help"]) == int(ExitCode.SUCCESS)
    captured = capsys.readouterr()
    for marker in ("wizard", "heartbeat", "doctor"):
        assert marker in captured.out


def test_modules_create_scaffolds_and_reports(capsys, project_root, tmp_path) -> None:
    plugins = tmp_path / "plugins"
    args = [
        *base_args(project_root),
        "modules",
        "create",
        "demo",
        "--path",
        str(plugins),
        "--title",
        "Scaffolded demo.",
        "--json",
    ]
    assert main(args) == int(ExitCode.SUCCESS)
    payload = json.loads(capsys.readouterr().out)
    assert payload["module"] == "demo"
    assert len(payload["created"]) == 3
    assert (plugins / "demo" / "korodex_module.toml").is_file()


def test_modules_create_rejects_bad_names(capsys, project_root, tmp_path) -> None:
    args = [
        *base_args(project_root),
        "modules",
        "create",
        "BadName",
        "--path",
        str(tmp_path / "plugins"),
    ]
    assert main(args) == int(ExitCode.ERROR)
    captured = capsys.readouterr()
    assert "[!]" in captured.out
    assert "[i]" in captured.out


def test_modules_create_refuses_to_clobber_without_force(capsys, project_root, tmp_path) -> None:
    plugins = tmp_path / "plugins"
    first = [*base_args(project_root), "modules", "create", "demo", "--path", str(plugins)]
    assert main(first) == int(ExitCode.SUCCESS)
    second = [*base_args(project_root), "modules", "create", "demo", "--path", str(plugins)]
    assert main(second) == int(ExitCode.ERROR)
    assert "--force" in capsys.readouterr().out
