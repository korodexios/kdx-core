"""Argparse adapter turning declarative CliCommand objects into subparsers."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ..exceptions import CliError
from ..kernel.cli_contract import CliArgument, CliCommand, CliRequest

_TYPE_MAP: dict[str, Callable[[Any], Any]] = {
    "str": str,
    "int": int,
    "float": float,
    "path": Path,
}

COMMAND_ATTR = "__korodex_command__"
HANDLER_ATTR = "__korodex_handler__"


class ArgparseSurface:
    """Builds argparse subparsers from module commands and resolves handlers."""

    def __init__(self, subparsers: Any, *, context: Any, parents: Sequence[Any] = ()) -> None:
        self._subparsers = subparsers
        self._context = context
        self._parents = tuple(parents)
        self._handlers: dict[str, tuple[CliCommand, Callable[[CliRequest], int]]] = {}
        self._added: list[CliCommand] = []

    @property
    def context(self) -> Any:
        """Return the core context handed to every handler."""
        return self._context

    def add_command(self, command: CliCommand) -> None:
        """Create a subparser (plus nested subparsers for groups)."""
        try:
            parser = self._subparsers.add_parser(
                command.name, help=command.help or argparse.SUPPRESS, parents=self._parents
            )
        except argparse.ArgumentError as exc:
            raise CliError(
                f"command name {command.name!r} is already taken: {exc}",
                hint="Rename the module command or disable the competing module.",
            ) from exc
        self._add_arguments(parser, command.args)
        if command.subcommands:
            nested = parser.add_subparsers(dest="subcommand", metavar="action")
            for child in command.subcommands:
                child_parser = nested.add_parser(
                    child.name,
                    help=child.help or argparse.SUPPRESS,
                    parents=self._parents,
                )
                self._add_arguments(child_parser, child.args)
                self._bind(child_parser, child, f"{command.name} {child.name}")
            self._bind(parser, command, command.name, group=True)
        else:
            self._bind(parser, command, command.name)
        self._added.append(command)

    def _bind(
        self,
        parser: argparse.ArgumentParser,
        command: CliCommand,
        path: str,
        *,
        group: bool = False,
    ) -> None:
        handler = command.handler
        if handler is None:
            handler = self._group_handler(parser, path)
        parser.set_defaults(**{COMMAND_ATTR: path, HANDLER_ATTR: handler})
        self._handlers[path] = (command, handler)

    def _group_handler(
        self, parser: argparse.ArgumentParser, path: str
    ) -> Callable[[CliRequest], int]:
        def _show_usage(request: CliRequest) -> int:
            parser.print_help()
            return 0

        return _show_usage

    def _add_arguments(
        self, parser: argparse.ArgumentParser, arguments: tuple[CliArgument, ...]
    ) -> None:
        for argument in arguments:
            kwargs: dict[str, Any] = {}
            is_positional = all(not str(flag).startswith("-") for flag in argument.flags)
            if argument.help:
                kwargs["help"] = argument.help
            if argument.metavar:
                kwargs["metavar"] = argument.metavar
            if argument.action:
                kwargs["action"] = argument.action
            if argument.nargs is not None:
                kwargs["nargs"] = argument.nargs
            if argument.choices:
                kwargs["choices"] = list(argument.choices)
            if is_positional:
                parser.add_argument(*argument.flags, **kwargs)
                continue
            if argument.dest:
                kwargs["dest"] = argument.dest
            if argument.default is not None:
                kwargs["default"] = argument.default
            if not argument.action and argument.type != "flag":
                kwargs["type"] = _TYPE_MAP.get(argument.type, str)
            parser.add_argument(*argument.flags, **kwargs)

    def resolve(
        self, namespace: argparse.Namespace
    ) -> tuple[str, Callable[[CliRequest], int]] | None:
        """Return (command path, handler) for a parsed namespace, or None."""
        path = getattr(namespace, COMMAND_ATTR, None)
        handler = getattr(namespace, HANDLER_ATTR, None)
        if not path or not callable(handler):
            return None
        return str(path), handler

    def commands(self) -> list[CliCommand]:
        """Return every command registered on this surface."""
        return list(self._added)

    def paths(self) -> list[str]:
        """Return every command path this surface can dispatch."""
        return sorted(self._handlers)

    def __contains__(self, path: object) -> bool:
        return str(path) in self._handlers

    def __repr__(self) -> str:
        return f"ArgparseSurface(commands={sorted(self._handlers)})"
