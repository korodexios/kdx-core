"""Root wizard: numbered menus built from the declarative CliCommand tree.

Modules register commands once; this wizard turns that same tree into an
interactive flow, so no module ever needs to know about menus. Non-interactive
runs pick the default entry, answer every question with its default, and exit
without touching stdin.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from typing import Any

from ..constants import TAG_NOTE, ExitCode
from ..kernel.cli_contract import CliArgument, CliCommand, CliRequest
from ..validate import require_text
from .ux import MenuOption, UserExperience

__all__ = ["WIZARD_FLAG_DEFAULTS", "RootWizard"]

QUIT_LABEL = "Quit"
BACK_LABEL = "Back to the main menu"

# UX overrides so a wizard visit never blocks longer than the user expects.
WIZARD_FLAG_DEFAULTS: dict[str, dict[str, bool]] = {"run": {"once": True}}

_TYPE_PROMPTS: dict[str, str] = {"int": "int", "float": "float", "path": "path"}


class RootWizard:
    """Interactive command picker driven by the registered CLI surface."""

    def __init__(
        self,
        ux: UserExperience,
        context: Any,
        commands: Sequence[CliCommand],
        *,
        options: dict[str, Any] | None = None,
    ) -> None:
        self._ux = ux
        self._context = context
        self._commands = [
            command for command in commands if getattr(command, "show_in_wizard", True)
        ]
        self._options = dict(options or {})
        if not self._commands:
            raise ValueError("the wizard needs at least one visible command")

    @property
    def commands(self) -> list[CliCommand]:
        """Return the commands offered by this wizard."""
        return list(self._commands)

    def run(self) -> int:
        """Run the wizard loop and return a process exit code."""
        self._ux.heading(f"korodexios-core wizard ({self._context.app_name})")
        if self._ux.interactive:
            self._ux.note("Enter accepts the default in brackets; choose by number or name.")
        while True:
            choice = self._menu_entry()
            if choice is None:
                return int(ExitCode.SUCCESS)
            command, path = choice
            code = self._execute(command, path)
            if not self._ux.interactive:
                return code
            if code == int(ExitCode.SUCCESS):
                self._ux.status(f"{path} finished", tag=TAG_NOTE)
            else:
                self._ux.error(f"{path} exited with code {code}")

    def _menu_entry(self) -> tuple[CliCommand, str] | None:
        """Show the top-level menu and return the chosen (command, path) pair."""
        options = [MenuOption(label=command.name, hint=command.help) for command in self._commands]
        options.append(MenuOption(label=QUIT_LABEL, hint="leave the wizard"))
        index = self._ux.menu("Command", options, default_index=1)
        if index == len(options):
            return None
        command = self._commands[index - 1]
        return command, command.name

    def _submenu(self, command: CliCommand, path: str) -> tuple[CliCommand, str] | None:
        """Drill into a command group; returns None when the user goes back."""
        children = list(command.subcommands)
        options = [MenuOption(label=child.name, hint=child.help) for child in children]
        options.append(MenuOption(label=BACK_LABEL))
        index = self._ux.menu(f"{path} action", options, default_index=1)
        if index == len(options):
            return None
        child = children[index - 1]
        return child, f"{path} {child.name}"

    def _execute(self, command: CliCommand, path: str) -> int:
        """Resolve nested groups, ask for arguments, then call the handler."""
        seen = path
        current = command
        while current.is_group:
            if not self._ux.interactive:
                current = current.subcommands[0]
                seen = f"{seen} {current.name}"
                continue
            picked = self._submenu(current, seen)
            if picked is None:
                return int(ExitCode.SUCCESS)
            current, seen = picked
        if current.handler is None:
            self._ux.error(f"command {seen!r} has no handler")
            return int(ExitCode.ERROR)
        namespace = self._collect_answers(current, seen)
        request = CliRequest(
            context=self._context,
            command=seen,
            namespace=namespace,
            options=dict(self._options),
        )
        try:
            return int(current.handler(request))
        except Exception as exc:
            self._ux.error(f"{seen} failed: {type(exc).__name__}: {exc}")
            return int(ExitCode.ERROR)

    def _collect_answers(self, command: CliCommand, path: str) -> argparse.Namespace:
        """Prompt for each declared argument, honouring defaults when not interactive."""
        answers: dict[str, Any] = {}
        overrides = WIZARD_FLAG_DEFAULTS.get(command.name, {})
        for argument in command.args:
            dest = argument.dest or argument.flags[-1].lstrip("-")
            answers[dest] = self._ask_argument(argument, dest, overrides, path)
        return argparse.Namespace(**answers)

    def _ask_argument(
        self,
        argument: CliArgument,
        dest: str,
        overrides: dict[str, bool],
        path: str,
    ) -> Any:
        label = _argument_label(argument, dest)
        if argument.action in ("store_true", "store_false", "count"):
            fallback = bool(argument.default) if argument.default is not None else False
            return self._ux.confirm(f"{path}: {label}", default=overrides.get(dest, fallback))
        suggestion = self._suggestion_for(dest)
        if suggestion:
            return self._ux.ask_choice(f"{path}: {label}", suggestion, default=str(suggestion[0]))
        kind = _TYPE_PROMPTS.get(str(argument.type or ""))
        default_text = "" if argument.default is None else str(argument.default)
        if kind == "int":
            return self._ux.ask_int(label, default=int(argument.default or 0))
        if kind == "float":
            return self._ux.ask_float(label, default=float(argument.default or 0.0))
        if kind == "path":
            return self._ux.ask_path(label, default=default_text)
        if argument.choices:
            return self._ux.ask_choice(
                label,
                [str(item) for item in argument.choices],
                default=default_text,
            )
        if not default_text:
            return self._ux.ask_text(
                label,
                default="",
                validator=lambda text: require_text(text, label=label),
                required=True,
            )
        return self._ux.ask_text(label, default=default_text)

    def _suggestion_for(self, dest: str) -> list[str]:
        """Offer friendly choices for well-known argument names."""
        if dest == "name":
            registry = getattr(self._context, "registry", None)
            if registry is not None:
                return list(registry.names())
        return []

    def __repr__(self) -> str:
        return f"RootWizard(commands={len(self._commands)}, interactive={self._ux.interactive})"


def _argument_label(argument: CliArgument, dest: str) -> str:
    """Ask the question in the words the command author already wrote."""
    help_text = (argument.help or "").strip().rstrip(".")
    if help_text and len(help_text) <= 80:
        return help_text[0].upper() + help_text[1:]
    cleaned = dest.replace("_", " ").strip()
    return cleaned or dest
