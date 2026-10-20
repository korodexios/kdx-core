"""Validator tests: polite rejection messages, never exceptions to the user."""

from __future__ import annotations

from pathlib import Path

import pytest

from korodexios_core import validate as v


def test_parse_size_units() -> None:
    assert v.parse_size("500") == 500
    assert v.parse_size("10MB") == 10_000_000
    assert v.parse_size("1.5GiB") == 1_610_612_736
    assert v.parse_size("2 TB") == 2_000_000_000_000


def test_parse_size_rejects_nonsense() -> None:
    with pytest.raises(ValueError, match="unknown size unit"):
        v.parse_size("10QB")
    with pytest.raises(ValueError, match="not a size"):
        v.parse_size("wide")


def test_parse_duration_units() -> None:
    assert v.parse_duration("90s") == 90.0
    assert v.parse_duration("5m") == 300.0
    assert v.parse_duration("2h") == 7200.0
    assert v.parse_duration("1.5d") == 129_600.0
    with pytest.raises(ValueError, match="not a duration"):
        v.parse_duration("soon")


def test_require_port_bounds() -> None:
    assert v.require_port("8080").ok
    assert v.require_port("8080").value == 8080
    rejected = v.require_port("0")
    assert not rejected.ok
    assert "outside 1-65535" in rejected.message
    assert "whole number" in v.require_port("http").message


def test_require_int_and_float_ranges() -> None:
    assert v.require_int("5", minimum=1, maximum=10).value == 5
    assert not v.require_int("0", minimum=1).ok
    assert not v.require_int("11", maximum=10).ok
    assert v.require_float("1.5", minimum=1.0).value == 1.5
    assert not v.require_float("abc").ok


def test_require_ip_accepts_addresses_and_hostnames() -> None:
    assert v.require_ip("127.0.0.1").value == "127.0.0.1"
    assert v.require_ip("::1").ok
    assert v.require_ip("example.com").ok
    assert not v.require_ip("not a host").ok


def test_require_path_existence_and_kind(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    rejected = v.require_path(str(missing), must_exist=True, kind="dir")
    assert not rejected.ok and "does not exist" in rejected.message
    accepted = v.require_path(str(tmp_path), must_exist=True, kind="dir")
    assert accepted.ok and isinstance(accepted.value, Path)
    wrong_kind = v.require_path(str(tmp_path), kind="file")
    assert not wrong_kind.ok and "is not a file" in wrong_kind.message
    relative = v.require_path("relative/thing")
    assert relative.ok and Path(str(relative.value)).is_absolute()


def test_require_choice_is_case_insensitive() -> None:
    assert v.require_choice("Y", ["yes", "no"]).value == "yes"
    assert v.require_choice("yes", ["yes", "no"], case_sensitive=True).ok
    assert not v.require_choice("maybe", ["yes", "no"]).ok


def test_require_size_and_duration_wrappers() -> None:
    assert v.require_size("10MB").value == 10_000_000
    assert not v.require_size("10MB", minimum=20_000_000).ok
    assert v.require_duration("5m").value == 300.0
    assert not v.require_duration("5s", minimum=60.0).ok


def test_require_regex_validator() -> None:
    validator = v.require_regex(r"^[a-z]+$", "lowercase letters")
    assert validator("abc").ok
    bad = validator("ABC")
    assert not bad.ok and "does not match" in bad.message


def test_require_text_length_window() -> None:
    assert v.require_text("  hello  ").value == "hello"
    assert not v.require_text("   ").ok
    assert not v.require_text("way too long", max_length=5).ok
