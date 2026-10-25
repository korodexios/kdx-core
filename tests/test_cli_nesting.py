"""Recursive CLI nesting: arbitrary depth, required arguments, rpc by path."""

from __future__ import annotations

import argparse
import io
from pathlib import Path
from typing import Any

import pytest

from korodexios_core.cli.surface import ArgparseSurface
from korodexios_core.cli.ux import UserExperience
from korodexios_core.cli.wizard import RootWizard
from korodexios_core.config.defaults import CORE_DEFAULTS
from korodexios_core.config.layered import LayeredConfig
from korodexios_core.constants import ExitCode
from korodexios_core.kernel.chassis import Chassis, ChassisOptions
from korodexios_core.kernel.cli_contract import (
    CliArgument,
    CliCommand,
    CliRequest,
    add_command_arguments,
)
from korodexios_core.kernel.contract import ModuleSpec

NESTING_CONFIG = """
core:
  name: nesting
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


def leaf(name: str, calls: list[str], *, args: tuple[CliArgument, ...] = ()) -> CliCommand:
    """Return a runnable command that records its own path when handled."""

    def handle(request: Any) -> int:
        calls.append(str(request.command))
        return int(ExitCode.SUCCESS)

    return CliCommand(name=name, help=f"{name} action.", args=args, handler=handle)


def build_tree(calls: list[str]) -> CliCommand:
    """Return a four-level command tree: root group, group, group, action."""
    return CliCommand(
        name="probe",
        help="Probe module.",
        subcommands=(
            CliCommand(
                name="db",
                help="Database group.",
                subcommands=(
                    CliCommand(
                        name="index",
                        help="Index group.",
                        subcommands=(leaf("rebuild", calls),),
                    ),
                    leaf("vacuum", calls),
                ),
            ),
        ),
    )


def make_parser() -> tuple[argparse.ArgumentParser, argparse._SubParsersAction, ArgparseSurface]:
    """Return a parser plus the argparse subparsers action its surface writes into."""
    parser = argparse.ArgumentParser(prog="kdx")
    subparsers = parser.add_subparsers(dest="command", metavar="command")
    surface = ArgparseSurface(subparsers, context=None)
    return parser, subparsers, surface


def test_a_command_describes_its_own_depth_and_leaves() -> None:
    calls: list[str] = []
    tree = build_tree(calls)
    assert tree.depth == 4
    assert [command.name for command in tree.leaves] == ["rebuild", "vacuum"]
    assert tree.find("db") is not None
    assert tree.find("db").find("index").find("rebuild").handler is not None  # type: ignore[union-attr]
    assert tree.find("nope") is None
    assert tree.is_group is True
    assert leaf("solo", calls).is_group is False


def test_walk_visits_every_path_space_joined() -> None:
    tree = build_tree([])
    paths = [path for path, _node in tree.walk()]
    assert paths == [
        "probe",
        "probe db",
        "probe db index",
        "probe db index rebuild",
        "probe db vacuum",
    ]


def test_the_surface_rejects_a_broken_tree() -> None:
    calls: list[str] = []
    with pytest.raises(ValueError):
        CliCommand(name="dupe", subcommands=(leaf("x", calls), leaf("x", calls)))
    with pytest.raises(ValueError):
        CliCommand(name="", help="blank")
    with pytest.raises(ValueError):
        CliCommand(
            name="bad",
            subcommands=(CliCommand(name="x"), CliCommand(name="x", help="other")),
        )
    with pytest.raises(ValueError):
        CliCommand(name="ghost")  # neither a group nor a runnable command


def test_the_surface_renders_arbitrary_depth() -> None:
    parser, subparsers, surface = make_parser()
    surface.add_command(build_tree([]))
    assert surface.depth() == 4
    assert surface.paths() == [
        "probe",
        "probe db",
        "probe db index",
        "probe db index rebuild",
        "probe db vacuum",
    ]
    assert surface.leaves() == ["probe db index rebuild", "probe db vacuum"]
    assert "probe db vacuum" in surface
    assert "probe db vacuum --fast" not in surface
    assert len(subparsers.choices) == 1
    namespace = parser.parse_args(["probe", "db", "index", "rebuild"])
    resolved = surface.resolve(namespace)
    assert resolved is not None
    assert resolved[0] == "probe db index rebuild"


def test_a_group_without_a_handler_shows_its_usage(capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[str] = []
    parser, _subparsers, surface = make_parser()
    surface.add_command(build_tree(calls))
    namespace = parser.parse_args(["probe", "db"])
    path, handler = surface.resolve(namespace)  # type: ignore[misc]
    assert path == "probe db"
    request = CliRequest(context=None, command=path, namespace=namespace)
    assert int(handler(request)) == 0
    assert "usage" in capsys.readouterr().out.lower()
    assert calls == []


def test_deep_dispatch_runs_the_leaf_handler() -> None:
    calls: list[str] = []
    parser, _subparsers, surface = make_parser()
    surface.add_command(build_tree(calls))
    namespace = parser.parse_args(["probe", "db", "index", "rebuild"])
    path, handler = surface.resolve(namespace)  # type: ignore[misc]
    assert int(handler(CliRequest(context=None, command=path, namespace=namespace))) == 0
    assert calls == ["probe db index rebuild"]


def test_required_argument_asks_argparse_to_insist() -> None:
    parser = argparse.ArgumentParser(prog="kdx probe index rebuild")
    add_command_arguments(
        parser,
        (
            CliArgument(("--table",), dest="table", required=True, help="table name"),
            CliArgument(("--lines",), dest="lines", type="int", default=3),
        ),
    )
    assert parser.parse_args(["--table", "page"]).table == "page"
    assert parser.parse_args(["--table", "page", "--lines", "9"]).lines == 9
    with pytest.raises(SystemExit) as early:
        parser.parse_args([])
    assert early.value.code == int(ExitCode.USAGE_ERROR)


def test_required_is_refused_for_flags_that_cannot_be_required() -> None:
    with pytest.raises(ValueError):
        CliArgument(("--json",), dest="as_json", action="store_true", required=True)
    with pytest.raises(ValueError):
        CliArgument(("-v",), dest="verbose", action="count", required=True)
    with pytest.raises(ValueError):
        CliArgument(("--once",), action="store_const", required=True)
    with pytest.raises(ValueError):
        CliArgument(("--tag",), action="append_const", required=True)
    assert CliArgument(("--json",), dest="as_json", action="store_true").required is False


def test_a_positional_argument_keeps_its_default_when_optional() -> None:
    parser = argparse.ArgumentParser(prog="kdx backup create")
    add_command_arguments(
        parser,
        (
            CliArgument(("source",), nargs="?", default=".", help="what to back up"),
            CliArgument(("--label",), dest="label", default="nightly"),
            CliArgument(("--dry-run",), dest="dry_run", action="store_true"),
        ),
    )
    namespace = parser.parse_args([])
    assert namespace.source == "."
    assert namespace.label == "nightly"
    assert namespace.dry_run is False
    assert parser.parse_args(["/srv", "--label", "weekly"]).label == "weekly"


def test_positional_destination_and_flags() -> None:
    assert CliArgument(("source",)).destination() == "source"
    assert CliArgument(("source",), dest="src_path").destination() == "src_path"
    assert CliArgument(("--dry-run",), dest="dry_run").destination() == "dry_run"
    assert CliArgument(("--dry-run",), dest="dry_run").is_positional is False
    assert CliArgument(("-x", "--extra")).destination() == "extra"
    with pytest.raises(ValueError):
        CliArgument(())
    with pytest.raises(ValueError):
        CliArgument(("src",), type="bool")


def make_wizard(answers: list[str], interactive: bool) -> RootWizard:
    """Return a wizard over the four-level tree driven by scripted answers."""
    iterator = iter(answers)

    def reader(prompt: str) -> str:
        value = next(iterator, None)
        if value is None:
            raise EOFError
        return value

    config = LayeredConfig(
        defaults=dict(CORE_DEFAULTS),
        cli_values={"core.cli.interactive": interactive, "core.cli.color": False},
    )
    ux = UserExperience(config, input_fn=reader, stdout=io.StringIO())
    calls: list[str] = []
    return RootWizard(ux, _FakeContext(), [build_tree(calls)])


class _FakeRegistry:
    def names(self) -> list[str]:
        return ["probe"]


class _FakeContext:
    app_name = "korodexios"
    registry = _FakeRegistry()


def test_the_wizard_lists_every_deep_path() -> None:
    wizard = make_wizard([], interactive=True)
    assert wizard.paths == ("probe db index rebuild", "probe db vacuum")


def test_a_non_interactive_wizard_walks_straight_to_the_first_leaf() -> None:
    wizard = make_wizard(["", ""], interactive=False)
    assert int(wizard.run()) == int(ExitCode.SUCCESS)


def test_repeated_answers_drive_the_deepest_path() -> None:
    wizard = make_wizard(["4", "1", "1", "1", "y"], interactive=True)
    assert int(wizard.run()) == int(ExitCode.SUCCESS)


def write_root(root: Path, text: str = NESTING_CONFIG) -> Path:
    """Write a throwaway root with discovery switched off."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "kdx.yml").write_text(text, encoding="utf-8")
    return root


class NestModule:
    """A module whose command tree is three levels deep."""

    name = "nestdb"
    description = "Nests commands three levels deep."
    version = "0.1.0"
    priority = 60

    def __init__(self) -> None:
        self.paths: list[str] = []
        self.requests: list[Any] = []

    def init(self, context: Any) -> None:
        self.context = context

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def register_cli(self, surface: Any) -> None:
        surface.add_command(
            CliCommand(
                name=self.name,
                help="Nested module command.",
                module_name=self.name,
                subcommands=(
                    CliCommand(
                        name="index",
                        help="Index group.",
                        module_name=self.name,
                        subcommands=(
                            CliCommand(
                                name="rebuild",
                                help="Rebuild the index.",
                                module_name=self.name,
                                args=(
                                    CliArgument(
                                        ("--lines",), dest="lines", type="int", default=3
                                    ),
                                ),
                                handler=self.handle,
                            ),
                        ),
                    ),
                ),
            )
        )

    def handle(self, request: Any) -> int:
        self.paths.append(str(request.command))
        self.requests.append(request)
        return 0


def test_rpc_dispatch_walks_a_three_level_path(project_root: Path) -> None:
    write_root(project_root)
    chassis = Chassis.create(
        ChassisOptions(home=project_root, environ={}, install_signals=False)
    )
    module = NestModule()
    chassis.registry.register(
        module,
        ModuleSpec(name="nestdb", description=NestModule.description, version="0.1.0"),
    )
    chassis.start(ipc=False)
    try:
        result = chassis.dispatch_rpc("nestdb index rebuild")
        assert result["path"] == "nestdb index rebuild"
        assert result["depth"] == 3
        assert result["exit_code"] == 0
        assert result["module"] == "nestdb"
        assert module.paths == ["nestdb index rebuild"]
        assert module.requests[-1].namespace.lines == 3
        chassis.dispatch_rpc("nestdb index rebuild", {"lines": 8})
        assert module.requests[-1].namespace.lines == 8
        chassis.dispatch_rpc("nestdb index rebuild --lines 5")
        assert module.requests[-1].namespace.lines == 5
        with pytest.raises(LookupError):
            chassis.dispatch_rpc("nestdb index missing")
        with pytest.raises(LookupError):
            chassis.dispatch_rpc("nestdb missing")
        with pytest.raises(LookupError):
            chassis.dispatch_rpc("nestdb")  # a group is not an action
    finally:
        chassis.shutdown()
        chassis.signals.uninstall()
