"""Plain-text rendering helpers. The rich wizard layer reuses these primitives."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from typing import Any

from ..constants import TAG_ERROR, TAG_NOTE, TAG_OK


def write_line(message: str = "", *, stream: Any = None) -> None:
    """Write one line to stdout (the UI channel) unless *stream* says otherwise."""
    target = sys.stdout if stream is None else stream
    print(message, file=target)


def write_status(message: str, *, tag: str = TAG_OK, stream: Any = None) -> None:
    """Write a tagged status line."""
    write_line(f"{tag} {message}", stream=stream)


def render_key_values(
    pairs: Sequence[tuple[str, Any]],
    *,
    indent: int = 2,
    key_width: int = 26,
) -> list[str]:
    """Render aligned 'key : value' lines."""
    pad = " " * indent
    lines: list[str] = []
    for key, value in pairs:
        rendered = _render(value)
        lines.append(f"{pad}{str(key).ljust(key_width)} {rendered}")
    return lines


def print_key_values(
    pairs: Sequence[tuple[str, Any]],
    *,
    indent: int = 2,
    key_width: int = 26,
    stream: Any = None,
) -> None:
    """Print aligned 'key : value' lines."""
    for line in render_key_values(pairs, indent=indent, key_width=key_width):
        write_line(line, stream=stream)


def render_table(
    rows: Sequence[Sequence[Any]],
    headers: Sequence[str] = (),
    *,
    indent: int = 2,
) -> list[str]:
    """Render a fixed-width text table from stringifiable cells."""
    grid: list[list[str]] = [[_render(cell) for cell in row] for row in rows]
    if headers:
        grid.insert(0, [str(header) for header in headers])
    if not grid:
        return []
    columns = max(len(row) for row in grid)
    grid = [row + [""] * (columns - len(row)) for row in grid]
    widths = [max(len(row[index]) for row in grid) for index in range(columns)]
    pad = " " * indent
    lines = [
        pad + "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip()
        for row in grid
    ]
    if headers:
        rule = pad + "  ".join("-" * width for width in widths)
        lines.insert(1, rule.rstrip())
    return lines


def print_table(
    rows: Sequence[Sequence[Any]],
    headers: Sequence[str] = (),
    *,
    indent: int = 2,
    stream: Any = None,
) -> None:
    """Print a fixed-width text table."""
    for line in render_table(rows, headers, indent=indent):
        write_line(line, stream=stream)


def render_section(title: str, *, indent: int = 0) -> str:
    """Render an underlined section heading."""
    pad = " " * indent
    return f"{pad}{title}\n{pad}{'-' * max(4, len(title))}"


def print_section(title: str, *, indent: int = 0, stream: Any = None) -> None:
    """Print an underlined section heading."""
    for line in render_section(title, indent=indent).splitlines():
        write_line(line, stream=stream)


def dump_json(payload: Any, *, stream: Any = None) -> None:
    """Print *payload* as pretty JSON on the UI channel."""
    write_line(json.dumps(payload, indent=2, sort_keys=True, default=str), stream=stream)


def format_bool(value: bool) -> str:
    """Render a boolean as a friendly yes/no."""
    return "yes" if value else "no"


def format_ms(value: float | None) -> str:
    """Render a millisecond duration, or a dash when unknown."""
    if value is None:
        return "-"
    return f"{value:.1f} ms"


def _render(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple, set, frozenset)):
        return ", ".join(_render(item) for item in value) or "(empty)"
    if isinstance(value, dict):
        return ", ".join(f"{key}={_render(item)}" for key, item in value.items()) or "(empty)"
    return str(value)


__all__ = [
    "TAG_ERROR",
    "TAG_NOTE",
    "TAG_OK",
    "dump_json",
    "format_bool",
    "format_ms",
    "print_key_values",
    "print_section",
    "print_table",
    "render_key_values",
    "render_section",
    "render_table",
    "write_line",
    "write_status",
]
