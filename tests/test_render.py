"""Render helpers stay readable on streams that cannot encode the check mark."""

from __future__ import annotations

import io

from korodexios_core.cli.render import write_line, write_status
from korodexios_core.constants import TAG_ALIASES, TAG_OK, tags_for


def latin_stream() -> io.TextIOWrapper:
    """Return an in-memory stream that behaves like a legacy Windows console.

    newline is set to an empty string so the wrapper does not translate newlines
    to the platform line ending, keeping the byte-exact assertion below stable
    on every host.
    """
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict", newline="")


def test_tags_for_only_aliases_when_needed() -> None:
    assert tags_for("utf-8") == {}
    assert tags_for("") == {}
    assert tags_for("cp1252")[TAG_OK] == TAG_ALIASES[TAG_OK]


def test_write_line_falls_back_to_ascii_tags() -> None:
    stream = latin_stream()
    write_status("chassis stopped", stream=stream)
    write_line(f"{TAG_OK} done", stream=stream)
    stream.flush()
    written = stream.buffer.getvalue().decode("cp1252")
    assert written == "[OK] chassis stopped\n[OK] done\n"


def test_write_line_keeps_unicode_on_utf8_streams() -> None:
    buffer = io.StringIO()
    write_status("chassis stopped", stream=buffer)
    assert buffer.getvalue() == "[✓] chassis stopped\n"
