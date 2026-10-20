"""Reusable input validators: polite messages instead of stack traces.

Every validator returns a ValidationResult, so the UX layer can print
'[!] expected ...' and re-prompt instead of crashing with a traceback.
"""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ValidationResult",
    "Validator",
    "parse_duration",
    "parse_size",
    "require_choice",
    "require_duration",
    "require_float",
    "require_int",
    "require_ip",
    "require_path",
    "require_port",
    "require_regex",
    "require_size",
    "require_text",
]

Validator = Callable[[str], "ValidationResult"]

_SIZE_PATTERN = re.compile(r"^(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>[A-Za-z]{0,3})$")
_SIZE_UNITS: dict[str, int] = {
    "": 1,
    "B": 1,
    "KB": 1_000,
    "MB": 1_000**2,
    "GB": 1_000**3,
    "TB": 1_000**4,
    "PB": 1_000**5,
    "KIB": 1_024,
    "MIB": 1_024**2,
    "GIB": 1_024**3,
    "TIB": 1_024**4,
    "PIB": 1_024**5,
}
_DURATION_PATTERN = re.compile(r"^(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>[A-Za-z]{0,3})$")
_DURATION_UNITS: dict[str, float] = {
    "": 1.0,
    "MS": 0.001,
    "S": 1.0,
    "SEC": 1.0,
    "M": 60.0,
    "MIN": 60.0,
    "H": 3_600.0,
    "D": 86_400.0,
    "W": 604_800.0,
}
_HOSTNAME_PATTERN = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\.?$"
)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Outcome of one validation: accepted value plus an expected-format hint."""

    ok: bool
    value: object
    expected: str
    message: str = ""

    @staticmethod
    def accepted(value: object, expected: str) -> ValidationResult:
        """Return a successful result."""
        return ValidationResult(True, value, expected, "")

    @staticmethod
    def rejected(expected: str, message: str) -> ValidationResult:
        """Return a failed result carrying the polite re-prompt message."""
        return ValidationResult(False, None, expected, message)


def parse_size(text: str) -> int:
    """Parse '500', '10MB', '1.5GiB' into a whole number of bytes."""
    match = _SIZE_PATTERN.match(text.strip())
    if match is None:
        raise ValueError(f"{text!r} is not a size; expected for example '10MB' or '1.5GiB'")
    unit = match.group("unit").upper()
    if unit not in _SIZE_UNITS:
        raise ValueError(
            f"unknown size unit {match.group('unit')!r}; use B, KB, MB, GB, TB, KiB, MiB, GiB"
        )
    amount = float(match.group("amount"))
    scaled = amount * _SIZE_UNITS[unit]
    if scaled != int(scaled):
        raise ValueError(f"{text!r} does not resolve to whole bytes; use a coarser unit")
    return int(scaled)


def parse_duration(text: str) -> float:
    """Parse '90s', '5m', '2h', '1.5d', '3600' into seconds."""
    match = _DURATION_PATTERN.match(text.strip())
    if match is None:
        raise ValueError(f"{text!r} is not a duration; expected for example '90s', '5m', '2h'")
    unit = match.group("unit").upper()
    if unit not in _DURATION_UNITS:
        raise ValueError(f"unknown duration unit {match.group('unit')!r}; use ms, s, m, h, d, w")
    return float(match.group("amount")) * _DURATION_UNITS[unit]


def require_text(
    text: str,
    *,
    min_length: int = 1,
    max_length: int = 256,
    label: str = "text",
) -> ValidationResult:
    """Require non-empty text within a length window."""
    expected = f"{label} of {min_length}-{max_length} characters"
    cleaned = text.strip()
    if len(cleaned) < min_length:
        return ValidationResult.rejected(
            expected, f"{label} must be at least {min_length} character(s)"
        )
    if len(cleaned) > max_length:
        return ValidationResult.rejected(
            expected, f"{label} must be at most {max_length} characters"
        )
    return ValidationResult.accepted(cleaned, expected)


def require_port(text: str) -> ValidationResult:
    """Require a TCP/UDP port number between 1 and 65535."""
    expected = "port number between 1 and 65535"
    try:
        port = int(text.strip())
    except ValueError:
        return ValidationResult.rejected(expected, f"{text!r} is not a whole number")
    if not 1 <= port <= 65_535:
        return ValidationResult.rejected(expected, f"port {port} is outside 1-65535")
    return ValidationResult.accepted(port, expected)


def require_int(
    text: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    label: str = "whole number",
) -> ValidationResult:
    """Require an integer inside an optional inclusive range."""
    bounds: list[str] = []
    if minimum is not None:
        bounds.append(f">= {minimum}")
    if maximum is not None:
        bounds.append(f"<= {maximum}")
    expected = f"{label} ({', '.join(bounds)})" if bounds else label
    try:
        value = int(text.strip())
    except ValueError:
        return ValidationResult.rejected(expected, f"{text!r} is not a whole number")
    if minimum is not None and value < minimum:
        return ValidationResult.rejected(expected, f"{label} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        return ValidationResult.rejected(expected, f"{label} must be <= {maximum}, got {value}")
    return ValidationResult.accepted(value, expected)


def require_float(
    text: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    label: str = "number",
) -> ValidationResult:
    """Require a float inside an optional inclusive range."""
    bounds: list[str] = []
    if minimum is not None:
        bounds.append(f">= {minimum}")
    if maximum is not None:
        bounds.append(f"<= {maximum}")
    expected = f"{label} ({', '.join(bounds)})" if bounds else label
    try:
        value = float(text.strip())
    except ValueError:
        return ValidationResult.rejected(expected, f"{text!r} is not a number")
    if minimum is not None and value < minimum:
        return ValidationResult.rejected(expected, f"{label} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        return ValidationResult.rejected(expected, f"{label} must be <= {maximum}, got {value}")
    return ValidationResult.accepted(value, expected)


def require_ip(text: str) -> ValidationResult:
    """Require an IPv4/IPv6 address or a sane hostname."""
    expected = "IPv4/IPv6 address or hostname (for example 127.0.0.1)"
    cleaned = text.strip()
    try:
        address = ipaddress.ip_address(cleaned)
    except ValueError:
        if _HOSTNAME_PATTERN.match(cleaned.lower()):
            return ValidationResult.accepted(cleaned, expected)
        return ValidationResult.rejected(
            expected, f"{cleaned!r} is not a valid address or hostname"
        )
    return ValidationResult.accepted(str(address), expected)


def require_path(
    text: str,
    *,
    must_exist: bool = False,
    kind: str = "any",
    writable: bool = False,
) -> ValidationResult:
    """Require a filesystem path, optionally existing, of a given kind, and writable."""
    if kind not in ("any", "dir", "file"):
        raise ValueError(f"unsupported path kind {kind!r}")
    expected = "filesystem path"
    if must_exist:
        expected = f"filesystem path that exists ({kind})"
    cleaned = text.strip()
    if not cleaned:
        return ValidationResult.rejected(expected, "path must not be empty")
    candidate = Path(cleaned).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = Path(os.path.normpath(str(candidate)))
    if must_exist and not candidate.exists():
        return ValidationResult.rejected(expected, f"{candidate} does not exist")
    if kind == "dir" and candidate.exists() and not candidate.is_dir():
        return ValidationResult.rejected(expected, f"{candidate} is not a directory")
    if kind == "file" and candidate.exists() and not candidate.is_file():
        return ValidationResult.rejected(expected, f"{candidate} is not a file")
    if writable and candidate.exists() and not os.access(str(candidate), os.W_OK):
        return ValidationResult.rejected(expected, f"{candidate} is not writable")
    return ValidationResult.accepted(candidate, expected)


def require_choice(
    text: str, choices: Sequence[str], *, case_sensitive: bool = False
) -> ValidationResult:
    """Require one of choices, comparing case-insensitively by default."""
    listing = ", ".join(str(option) for option in choices)
    expected = "one of: " + listing
    cleaned = text.strip()
    if not choices:
        return ValidationResult.rejected(expected, "no choices were offered")
    lookup = {str(option).strip(): str(option).strip() for option in choices}
    if case_sensitive:
        if cleaned in lookup:
            return ValidationResult.accepted(lookup[cleaned], expected)
    else:
        lowered = {key.lower(): value for key, value in lookup.items()}
        key_lower = cleaned.lower()
        if key_lower in lowered:
            return ValidationResult.accepted(lowered[key_lower], expected)
        if key_lower:
            matches = [value for key, value in lowered.items() if key.startswith(key_lower)]
            if len(matches) == 1:
                return ValidationResult.accepted(matches[0], expected)
    return ValidationResult.rejected(expected, f"{cleaned!r} is not one of: {listing}")


def require_size(text: str, *, minimum: int | None = None) -> ValidationResult:
    """Require a size string such as '10MB' or '1.5GiB'."""
    expected = "size such as 500, 10MB, 1.5GiB"
    try:
        value = parse_size(text)
    except ValueError as exc:
        return ValidationResult.rejected(expected, str(exc))
    if minimum is not None and value < minimum:
        return ValidationResult.rejected(expected, f"size must be >= {minimum} bytes, got {value}")
    return ValidationResult.accepted(value, expected)


def require_duration(text: str, *, minimum: float | None = None) -> ValidationResult:
    """Require a duration string such as '90s' or '2h'."""
    expected = "duration such as 90s, 5m, 2h"
    try:
        value = parse_duration(text)
    except ValueError as exc:
        return ValidationResult.rejected(expected, str(exc))
    if minimum is not None and value < minimum:
        return ValidationResult.rejected(expected, f"duration must be >= {minimum}s, got {value}")
    return ValidationResult.accepted(value, expected)


def require_regex(pattern: str, expected: str, *, flags: int = 0) -> Validator:
    """Build a validator from a regular expression."""
    compiled = re.compile(pattern, flags)

    def _validate(text: str) -> ValidationResult:
        cleaned = text.strip()
        if compiled.match(cleaned):
            return ValidationResult.accepted(cleaned, expected)
        return ValidationResult.rejected(expected, f"{cleaned!r} does not match {expected}")

    return _validate
