"""Declarative CLI contract: modules inject commands without touching the core."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .context import CoreContext

CliHandler = Callable[["CliRequest"], int]


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

    def __post_init__(self) -> None:
        if not self.flags:
            raise ValueError("a CLI argument needs at least one flag or positional name")
        if self.type not in {"str", "int", "float", "path", "flag"}:
            raise ValueError(f"unsupported CLI argument type {self.type!r}")


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

    @property
    def is_group(self) -> bool:
        """Return True when this command only groups subcommands."""
        return self.handler is None and bool(self.subcommands)


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
