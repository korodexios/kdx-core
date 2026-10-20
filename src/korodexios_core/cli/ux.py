"""Rich-backed user experience: numbered menus, bracketed defaults, polite retries.

Diagnostics stay on stderr; every prompt, menu, and status line goes to stdout
so scripted output stays clean. Non-interactive mode (CI, Docker, --yes) never
blocks: each question returns its default immediately.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.box import HEAVY_HEAD
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..constants import TAG_ASK, TAG_ERROR, TAG_NOTE, TAG_OK, tags_for
from ..validate import ValidationResult, Validator
from ..validate import require_float as _require_float
from ..validate import require_int as _require_int
from ..validate import require_path as _require_path_fn

__all__ = ["MenuOption", "UserExperience"]

_TAG_STYLES: dict[str, str] = {
    TAG_OK: "bold green",
    TAG_ASK: "bold cyan",
    TAG_ERROR: "bold red",
    TAG_NOTE: "blue",
}

InputFn = Callable[[str], str]


@dataclass(frozen=True, slots=True)
class MenuOption:
    """One selectable menu entry with an optional hint and payload."""

    label: str
    hint: str = ""
    value: Any = None
    disabled: bool = False

    def render(self) -> str:
        """Return the label plus dimmed hint used inside menus."""
        return f"{self.label}  [dim]{self.hint}[/dim]" if self.hint else self.label


class UserExperience:
    """Prompt toolkit honouring core.cli.* settings and non-TTY environments."""

    def __init__(
        self,
        config: Any,
        *,
        input_fn: InputFn | None = None,
        console: Console | None = None,
        stdout: Any = None,
    ) -> None:
        self._config = config
        self._interactive = bool(config.get_bool("core.cli.interactive", True))
        self._color = bool(config.get_bool("core.cli.color", True))
        self._width = max(40, int(config.get_int("core.cli.width", 100)))
        stream = sys.stdout if stdout is None else stdout
        tty = bool(stream.isatty()) if hasattr(stream, "isatty") else False
        self._console = console or Console(
            file=stream,
            width=self._width,
            no_color=not self._color,
            force_terminal=self._color and tty,
            highlight=False,
        )
        self._input_fn = input_fn

    @property
    def interactive(self) -> bool:
        """Return True when prompts may block on keyboard input."""
        return self._interactive

    def is_interactive(self) -> bool:
        """Return the live interactivity flag (input may have closed since start)."""
        return self._interactive

    @property
    def console(self) -> Console:
        """Return the rich console bound to stdout."""
        return self._console

    def _resolve_tag(self, tag: str) -> str:
        """Return an ASCII tag when the console stream cannot encode the unicode one."""
        encoding = getattr(self._console.file, "encoding", None)
        return tags_for(encoding).get(tag, tag)

    @property
    def color(self) -> bool:
        """Return True when ANSI colour is enabled."""
        return self._color

    def status(self, message: str, *, tag: str = TAG_OK, style: str = "") -> None:
        """Print a tagged status line on stdout."""
        tag = self._resolve_tag(tag)
        rendered = Text.assemble((f"{tag} ", _TAG_STYLES.get(tag, "white")), (message, style))
        self._console.print(rendered, markup=False)

    def note(self, message: str) -> None:
        """Print an informational note tagged [i]."""
        self.status(message, tag=TAG_NOTE)

    def error(self, message: str) -> None:
        """Print an error tagged [!]."""
        self.status(message, tag=TAG_ERROR)

    def heading(self, message: str) -> None:
        """Print a section heading."""
        self._console.print(Text(message, style="bold cyan"))

    def panel(self, title: str, lines: Sequence[str]) -> None:
        """Print a boxed block of rich-markup lines."""
        body = Text("\n").join(Text.from_markup(line) for line in lines) if lines else Text("")
        self._console.print(Panel(body, title=title, border_style="cyan", expand=False))

    def table(
        self, headers: Sequence[str], rows: Sequence[Sequence[Any]], *, title: str = ""
    ) -> None:
        """Print a rich table on stdout."""
        table = Table(title=title or None, box=HEAVY_HEAD, header_style="bold cyan", pad_edge=False)
        for header in headers:
            table.add_column(str(header), overflow="fold")
        for row in rows:
            table.add_row(*[Text(str(cell)) if cell is not None else Text("-") for cell in row])
        self._console.print(table)

    def key_values(self, pairs: Sequence[tuple[str, Any]]) -> None:
        """Print aligned key/value lines."""
        if not pairs:
            return
        width = max(len(str(key)) for key, _ in pairs) + 2
        for key, value in pairs:
            self._console.print(f"  {str(key).ljust(width)}{_render(value)}", markup=False)

    def read_line(self, prompt_text: str) -> str:
        """Read one line, echoing prompt_text; a closed stdin means 'use defaults'."""
        try:
            if self._input_fn is not None:
                self._console.print(prompt_text, end="", markup=False)
                answer = self._input_fn(prompt_text)
            else:
                answer = self._console.input(prompt_text, markup=False)
        except (EOFError, StopIteration, KeyboardInterrupt):
            self._interactive = False
            self._console.print()
            self.note("input closed; continuing with defaults")
            return ""
        return answer

    def menu(
        self,
        title: str,
        options: Sequence[MenuOption],
        *,
        default_index: int = 1,
    ) -> int:
        """Show a numbered menu and return the chosen 1-based index.

        Numbered selection beats free text: ENTER accepts the default, and any
        invalid answer prints [!] and re-prompts instead of raising.
        """
        if not options:
            raise ValueError("a menu needs at least one option")
        default_index = min(max(int(default_index), 1), len(options))
        if not self._interactive:
            self.note(f"{title}: default [{default_index}] {options[default_index - 1].label}")
            return default_index

        while self.is_interactive():
            lines = [
                f"[{'bold' if not option.disabled else 'dim'}][{index}][/"
                f"{'bold' if not option.disabled else 'dim'}] {option.render()}"
                for index, option in enumerate(options, start=1)
            ]
            self.panel(title, lines)
            answer = self.read_line(f"{TAG_ASK} {title} [{default_index}]: ").strip()
            if not answer:
                return default_index
            try:
                choice = int(answer)
            except ValueError:
                matched = _match_label(options, answer)
                if matched is None:
                    self.error(f"expected a number between 1 and {len(options)} (or a label)")
                    continue
                return matched
            if not 1 <= choice <= len(options):
                self.error(f"pick a number between 1 and {len(options)}")
                continue
            if options[choice - 1].disabled:
                self.error(f"option [{choice}] is not available")
                continue
            return choice
        return default_index

    def ask_text(
        self,
        label: str,
        *,
        default: str = "",
        validator: Validator | None = None,
        example: str = "",
        required: bool = False,
    ) -> str:
        """Ask for text showing the default in brackets; ENTER accepts it."""
        if not self._interactive:
            return default
        hint = f" (e.g. {example})" if example else ""
        suffix = f" [{default}]: " if default else ": "
        while self.is_interactive():
            answer = self.read_line(f"{TAG_ASK} {label}{hint}{suffix}").strip()
            if not answer:
                if default:
                    return default
                if not required:
                    return ""
                self.error(f"{label} must not be empty")
                continue
            if validator is None:
                return answer
            result = validator(answer)
            if not result.ok:
                self.error(f"{result.message} (expected {result.expected})")
                continue
            return str(result.value)
        return default

    def ask_int(
        self,
        label: str,
        *,
        default: int,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        """Ask for an integer inside an optional range."""
        if not self._interactive:
            return default

        def _validate(text: str) -> ValidationResult:
            return _require_int(text, minimum=minimum, maximum=maximum, label=label)

        answer = self.ask_text(
            label, default=str(default), validator=_validate, example=str(default)
        )
        result = _validate(answer)
        return int(str(result.value)) if result.ok else default

    def ask_float(
        self,
        label: str,
        *,
        default: float,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> float:
        """Ask for a float inside an optional range."""
        if not self._interactive:
            return default

        def _validate(text: str) -> ValidationResult:
            return _require_float(text, minimum=minimum, maximum=maximum, label=label)

        answer = self.ask_text(
            label, default=str(default), validator=_validate, example=str(default)
        )
        result = _validate(answer)
        return float(str(result.value)) if result.ok else default

    def ask_path(
        self,
        label: str,
        *,
        default: str = "",
        must_exist: bool = False,
        kind: str = "any",
        writable: bool = False,
    ) -> Path:
        """Ask for a filesystem path and validate it in a loop."""

        def _validate(text: str) -> ValidationResult:
            return _require_path_fn(text, must_exist=must_exist, kind=kind, writable=writable)

        if not self._interactive:
            result = _validate(default or ".")
            return Path(str(result.value)) if result.ok else Path(default or ".")
        answer = self.ask_text(
            label,
            default=default,
            validator=_validate,
            example=default or "relative or absolute path",
            required=not default,
        )
        result = _validate(answer or default or ".")
        return Path(str(result.value)) if result.ok else Path(default or ".")

    def ask_choice(self, label: str, options: Sequence[str], *, default: str = "") -> str:
        """Ask for a value from a fixed list of choices using a numbered menu."""
        if not self._interactive:
            return default or (str(options[0]) if options else "")
        chosen = self.menu(
            label,
            [MenuOption(label=str(option)) for option in options],
            default_index=_index_of(options, default),
        )
        return str(options[chosen - 1])

    def confirm(self, label: str, *, default: bool = True) -> bool:
        """Ask a yes/no question with the default shown as an uppercase letter."""
        if not self._interactive:
            return default
        suffix = "[Y/n]: " if default else "[y/N]: "
        while self.is_interactive():
            answer = self.read_line(f"{TAG_ASK} {label} {suffix}").strip().lower()
            if not answer:
                return default
            if answer in ("y", "yes", "1", "true"):
                return True
            if answer in ("n", "no", "0", "false"):
                return False
            self.error("answer yes or no (y/n)")
        return default

    def __repr__(self) -> str:
        return (
            f"UserExperience(interactive={self._interactive}, "
            f"color={self._color}, width={self._width})"
        )


def _index_of(options: Sequence[str], value: str) -> int:
    for index, option in enumerate(options, start=1):
        if str(option).lower() == str(value).lower():
            return index
    return 1


def _match_label(options: Sequence[MenuOption], answer: str) -> int | None:
    lowered = answer.strip().lower()
    for index, option in enumerate(options, start=1):
        if option.label.lower() == lowered or option.label.lower().startswith(lowered):
            return None if option.disabled else index
    return None


def _render(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return ", ".join(f"{key}={_render(item)}" for key, item in value.items()) or "(empty)"
    if isinstance(value, (list, tuple, set, frozenset)):
        return ", ".join(_render(item) for item in value) or "(empty)"
    return str(value)
