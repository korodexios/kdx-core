"""Root wizard tests: menus over the declarative command tree, no core changes."""

from __future__ import annotations

import io
from dataclasses import replace
from typing import Any

from korodexios_core.cli.ux import UserExperience
from korodexios_core.cli.wizard import RootWizard
from korodexios_core.config.defaults import CORE_DEFAULTS
from korodexios_core.config.layered import LayeredConfig
from korodexios_core.constants import ExitCode
from korodexios_core.kernel.cli_contract import CliArgument, CliCommand


class FakeRegistry:
    def names(self) -> list[str]:
        return ["alpha", "beta"]


class FakeContext:
    app_name = "korodexios"
    registry = FakeRegistry()


def make_ux(answers: list[str] | None, *, interactive: bool) -> tuple[UserExperience, io.StringIO]:
    buffer = io.StringIO()
    iterator = iter(answers or [])
    config = LayeredConfig(
        defaults=dict(CORE_DEFAULTS),
        cli_values={"core.cli.interactive": interactive, "core.cli.color": False},
    )

    def reader(prompt: str) -> str:
        if iterator is None:
            raise EOFError
        value = next(iterator, None)
        if value is None:
            raise EOFError
        return value

    return UserExperience(config, input_fn=reader, stdout=buffer), buffer


def sample_commands(calls: list[str]) -> list[CliCommand]:

    def alpha(request: Any) -> int:
        calls.append(str(request.command))
        return int(ExitCode.SUCCESS)

    def beta_one(request: Any) -> int:
        calls.append(f"{request.command}:{request.flag('flag')}")
        return int(ExitCode.SUCCESS)

    def broken(request: Any) -> int:
        raise RuntimeError("kaboom")

    commands = [
        CliCommand(name="alpha", help="first", handler=alpha, group="core"),
        CliCommand(
            name="beta",
            help="group",
            group="core",
            subcommands=(
                CliCommand(
                    name="one",
                    help="child",
                    handler=beta_one,
                    args=(
                        CliArgument(("--flag",), dest="flag", action="store_true", default=False),
                    ),
                ),
            ),
        ),
        CliCommand(name="broken", help="raises", handler=broken, group="core"),
    ]
    return commands


def test_non_interactive_wizard_runs_default_without_reading_input() -> None:
    ux, buffer = make_ux([], interactive=False)
    calls: list[str] = []
    wizard = RootWizard(ux, FakeContext(), sample_commands(calls))
    assert wizard.run() == int(ExitCode.SUCCESS)
    printed = buffer.getvalue()
    assert "default [1] alpha" in printed
    assert "korodexios-core wizard" in printed
    assert calls == ["alpha"]


def test_wizard_navigates_groups_and_collects_flags() -> None:
    ux, buffer = make_ux(["beta", "one", "y", "Quit"], interactive=True)
    calls: list[str] = []
    wizard = RootWizard(ux, FakeContext(), sample_commands(calls))
    assert wizard.run() == int(ExitCode.SUCCESS)
    assert calls == ["beta one:True"]
    assert "beta action" in buffer.getvalue()


def test_wizard_reports_handler_errors_without_traceback() -> None:
    ux, buffer = make_ux(["broken", "Quit"], interactive=True)
    wizard = RootWizard(ux, FakeContext(), sample_commands([]))
    assert wizard.run() == int(ExitCode.SUCCESS)
    assert "[!] broken failed: RuntimeError: kaboom" in buffer.getvalue()


def test_wizard_offers_module_names_for_name_arguments() -> None:
    ux, buffer = make_ux(["alpha", "Quit"], interactive=True)
    wizard = RootWizard(ux, FakeContext(), sample_commands([]))
    assert wizard.run() == int(ExitCode.SUCCESS)
    assert "[1] alpha" in buffer.getvalue()


def test_wizard_hides_commands_marked_invisible() -> None:
    commands = sample_commands([])
    hidden = [replace(command, show_in_wizard=command.name == "alpha") for command in commands]
    ux, _ = make_ux([], interactive=False)
    wizard = RootWizard(ux, FakeContext(), hidden)
    assert [command.name for command in wizard.commands] == ["alpha"]
