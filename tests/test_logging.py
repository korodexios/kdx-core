"""Structured log fields keep their JSON types in the JSON log line."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from korodexios_core.logging.engine import JsonFormatter, TextFormatter


def record(fields: dict[str, Any], level: int = logging.INFO) -> logging.LogRecord:
    """Return a record carrying *fields* the way the chassis logger does."""
    made = logging.LogRecord(
        "kdx.backup_engine",
        level,
        "module.py",
        12,
        "backup finished",
        None,
        None,
    )
    made.kdx_fields = fields
    return made


def payload(fields: dict[str, Any], level: int = logging.INFO) -> dict[str, Any]:
    """Return the parsed JSON line for one record."""
    return json.loads(JsonFormatter().format(record(fields, level)))


def test_counts_and_flags_stay_numbers_and_booleans() -> None:
    """A count is not a string in a JSON log line, and neither is a flag."""
    fields = payload({"copied": 12, "dry_run": False, "ratio": 0.25, "seconds": 3})["fields"]
    assert fields["copied"] == 12
    assert isinstance(fields["copied"], int)
    assert fields["dry_run"] is False
    assert fields["ratio"] == 0.25
    assert isinstance(fields["seconds"], int)
    assert isinstance(fields["ratio"], float)


def test_nested_structures_stay_structures() -> None:
    """A module may log a mapping or a list; the shipper reads them as data."""
    fields = payload(
        {
            "target": {"path": "/var/lib/backups", "files": 3},
            "names": ["a.txt", "b.txt"],
            "skips": (1, 2),
        }
    )["fields"]
    assert fields["target"] == {"path": "/var/lib/backups", "files": 3}
    assert fields["names"] == ["a.txt", "b.txt"]
    assert fields["skips"] == [1, 2]


def test_values_json_cannot_hold_are_shortened_to_text() -> None:
    """A path or an exception is data too, but JSON has no native form for them."""
    fields = payload(
        {
            "dest": Path("/var/lib/backups/2026-10-26"),
            "error": RuntimeError("disk full"),
            "note": None,
        }
    )["fields"]
    assert fields["dest"] == "/var/lib/backups/2026-10-26"
    assert "disk full" in str(fields["error"]), fields["error"]
    assert fields["note"] is None


def test_the_envelope_is_unchanged() -> None:
    """Level, logger, and message still frame the fields, so shippers keep working."""
    line = payload({"copied": 1}, level=logging.WARNING)
    assert line["level"] == "WARNING"
    assert line["logger"] == "kdx.backup_engine"
    assert line["message"] == "backup finished"
    assert line["ts"]


def test_the_human_formatter_still_shortens_fields() -> None:
    """One formatter keeps types, the other keeps lines readable; neither grows."""
    text = TextFormatter(color=False).format(record({"copied": 12, "dest": Path("/tmp/x")}))
    assert "copied" in text and "12" in text
    assert "/tmp/x" in text
