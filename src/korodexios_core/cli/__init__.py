"""Thin CLI client over the headless chassis (argparse in Phase 1, rich wizard later)."""

from __future__ import annotations

from .commands import core_commands
from .entry import build_root_parser, main
from .render import print_key_values, print_table, write_line
from .surface import ArgparseSurface

__all__ = [
    "ArgparseSurface",
    "build_root_parser",
    "core_commands",
    "main",
    "print_key_values",
    "print_table",
    "write_line",
]
