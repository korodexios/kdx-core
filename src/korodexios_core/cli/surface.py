"""Argparse adapter turning declarative CliCommand objects into subparsers."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from typing import Any, cast

from ..exceptions import CliError
from ..kernel.cli_contract import (
    CliArgument,
    CliCommand,
    CliRequest,
    add_command_arguments,
)

COMMAND_ATTR = "__kdx_command__"
HANDLER_ATTR = "__kdx_handler__"


def _dest_for_depth(depth: int) -> str:
    """Return the subparsers dest for one nesting level.

    'subcommand' stays the public name for the first level, which is what a
    module handler reads out of a namespace today; deeper levels get their own
    attribute so no level overwrites another's choice.
    """
    return "subcommand" if depth <= 0 else f"kdx_group_{depth}"


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
        """Create one subparser tree, at whatever depth the command declares.

        Nesting is recursive, so a module can contribute 'kdx mod action' today
        and 'kdx mod group action' tomorrow without the chassis learning a third
        level of its own: each group opens the subparsers its children need, and
        every runnable node binds its own handler path.
        """
        self._add_tree(self._subparsers, command, command.name, depth=0)
        self._added.append(command)

    def _add_tree(
        self,
        parent: Any,
        command: CliCommand,
        path: str,
        *,
        depth: int,
    ) -> argparse.ArgumentParser:
        try:
            parser = parent.add_parser(
                command.name, help=command.help or argparse.SUPPRESS, parents=self._parents
            )
        except argparse.ArgumentError as exc:
            raise CliError(
                f"command name {command.name!r} is already taken: {exc}",
                hint="Rename the module command or disable the competing module.",
            ) from exc
        self._add_arguments(parser, command.args)
        if command.subcommands:
            nested = parser.add_subparsers(dest=_dest_for_depth(depth), metavar="action")
            for child in command.subcommands:
                self._add_tree(nested, child, f"{path} {child.name}", depth=depth + 1)
            self._bind(parser, command, path, group=True)
        else:
            self._bind(parser, command, path)
        return cast("argparse.ArgumentParser", parser)

    def depth(self) -> int:
        """Return the deepest command tree this surface rendered (0 when empty)."""
        return max((command.depth for command in self._added), default=0)

    def leaves(self) -> list[str]:
        """Return every runnable command path, space-joined, in registration order."""
        paths: list[str] = []
        for command in self._added:
            for path, node in command.walk():
                if node.handler is not None:
                    paths.append(path)
        return paths

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
        add_command_arguments(parser, arguments)

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
