"""Declarative CLI contract: modules inject commands without touching the core."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .context import CoreContext

CliHandler = Callable[["CliRequest"], int]
_FLAG_ACTIONS: tuple[str, ...] = (
    "store_true",
    "store_false",
    "count",
    "store_const",
    "append_const",
    "none_const",
)
_TYPE_MAP: dict[str, Callable[[Any], Any]] = {
    "str": str,
    "int": int,
    "float": float,
    "path": Path,
}


def add_command_arguments(parser: Any, arguments: Sequence[CliArgument]) -> None:
    """Attach declarative arguments to an argparse parser.

    The surface builds the real CLI this way, and so does the chassis when a
    one-shot client forwards the tokens it received: one declaration, two
    consumers, no second parser to keep in sync.
    """
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
            # A positional is mandatory unless its nargs says otherwise, and
            # argparse refuses 'required' for one; only the default is honoured,
            # and only when it carries a value. So 'kdx backup create' with
            # nargs='?' plus a default lands on that default instead of None.
            if argument.default is not None:
                kwargs["default"] = argument.default
            parser.add_argument(*argument.flags, **kwargs)
            continue
        if argument.dest:
            kwargs["dest"] = argument.dest
        if argument.required:
            kwargs["required"] = True
        elif argument.default is not None:
            kwargs["default"] = argument.default
        if not argument.action and argument.type != "flag":
            kwargs["type"] = _TYPE_MAP.get(argument.type, str)
        parser.add_argument(*argument.flags, **kwargs)


@dataclass(frozen=True, slots=True)
class CliArgument:
    """One argparse-style argument declared by a module command."""

    flags: tuple[str, ...]
    dest: str | None = None
    metavar: str | None = None
    help: str = ""
    action: str | None = None
    nargs: str | int | None = None
    choices: tuple[str, ...] = ()
    default: Any = None
    type: str = "str"
    required: bool = False

    def __post_init__(self) -> None:
        if not self.flags:
            raise ValueError("a CLI argument needs at least one flag or positional name")
        if self.type not in {"str", "int", "float", "path", "flag"}:
            raise ValueError(f"unsupported CLI argument type {self.type!r}")
        if self.required and self.action in _FLAG_ACTIONS:
            raise ValueError(
                f"argument {self.flags[-1]!r} is a boolean flag; a flag is False when "
                "absent, so it is never required"
            )

    @property
    def is_positional(self) -> bool:
        """Return True when every flag is a bare positional name."""
        return all(not str(flag).startswith("-") for flag in self.flags)

    def destination(self) -> str:
        """Return the namespace attribute this argument writes."""
        if self.dest:
            return str(self.dest)
        text = str(self.flags[-1])
        if text.startswith("--"):
            return text[2:].replace("-", "_")
        if text.startswith("-"):
            return text[1:]
        return text.replace("-", "_")


@dataclass(frozen=True, slots=True)
class CliCommand:
    """A command (or command group) contributed to the root CLI surface."""

    name: str
    help: str = ""
    handler: CliHandler | None = None
    args: tuple[CliArgument, ...] = ()
    subcommands: tuple[CliCommand, ...] = ()
    module_name: str | None = None
    group: str = "module"
    show_in_wizard: bool = True

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("a CLI command needs a name")
        if self.handler is None and not self.subcommands:
            raise ValueError(
                f"command {self.name!r} needs either a handler or subcommands to be useful"
            )
        names = [str(child.name) for child in self.subcommands]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(
                f"command {self.name!r} lists duplicate subcommands: {', '.join(duplicates)}"
            )
        for child in self.subcommands:
            if not str(child.name).strip():
                raise ValueError(f"command {self.name!r} has a subcommand with no name")

    @property
    def is_group(self) -> bool:
        """Return True when this command only groups subcommands."""
        return self.handler is None and bool(self.subcommands)

    def find(self, name: str) -> CliCommand | None:
        """Return the direct child named *name*, or None."""
        cleaned = str(name).strip()
        for child in self.subcommands:
            if child.name == cleaned:
                return child
        return None

    @property
    def depth(self) -> int:
        """Return how deep this command tree goes: a leaf is 1, a group is deeper."""
        if not self.subcommands:
            return 1
        return 1 + max(child.depth for child in self.subcommands)

    @property
    def leaves(self) -> tuple[CliCommand, ...]:
        """Return every runnable command at or below this one, in declaration order."""
        found: list[CliCommand] = []
        for _path, command in self.walk():
            if command.handler is not None:
                found.append(command)
        return tuple(found)

    def walk(self, prefix: str = "") -> Iterator[tuple[str, CliCommand]]:
        """Yield (space-joined path, command) for this node and every node below it."""
        path = f"{prefix} {self.name}".strip()
        yield path, self
        for child in self.subcommands:
            yield from child.walk(path)


@dataclass(slots=True)
class CliRequest:
    """Everything a command handler needs: context, parsed namespace, options."""

    context: CoreContext
    command: str
    namespace: Any = None
    options: Mapping[str, Any] = field(default_factory=dict)

    def option(self, name: str, default: Any = None) -> Any:
        """Return a parsed option value from the namespace, then from *options*."""
        value = getattr(self.namespace, name, None) if self.namespace is not None else None
        if value is None:
            value = self.options.get(name, default)
        return default if value is None else value

    def flag(self, name: str) -> bool:
        """Return True when a boolean flag was supplied."""
        value = self.option(name, False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on"}
        return bool(value)


@runtime_checkable
class CliSurface(Protocol):
    """Anything that can receive declarative commands: argparse, rich menus, docs."""

    def add_command(self, command: CliCommand) -> None:
        """Attach *command* to this surface."""
        ...


def command_path(prefix: str, command: CliCommand) -> str:
    """Join a rendering prefix with a command name for dotted command labels."""
    return f"{prefix} {command.name}".strip()
