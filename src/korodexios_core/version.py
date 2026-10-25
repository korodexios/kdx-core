"""Single source of truth for the chassis and module API versions."""

from __future__ import annotations

import re
from typing import Final, NamedTuple

__version__: Final[str] = "0.2.5"
CORE_API_VERSION: Final[str] = "2"

_VERSION_PATTERN = re.compile(
    r"^(?P<major>\d+)(?:\.(?P<minor>\d+))?(?:\.(?P<patch>\d+))?(?P<prerelease>[A-Za-z0-9._+-]*)$"
)


class VersionInfo(NamedTuple):
    """Comparable, structured representation of a chassis version string."""

    major: int
    minor: int
    patch: int
    prerelease: str = ""

    def as_tuple(self) -> tuple[int, int, int, tuple[str, ...]]:
        """Return a tuple usable for ordering comparisons."""
        pre: tuple[str, ...] = (self.prerelease,) if self.prerelease else ()
        return (self.major, self.minor, self.patch, pre)


def parse_version(text: str) -> VersionInfo:
    """Parse a version string into VersionInfo, padding partial versions.

    'MAJOR', 'MAJOR.MINOR', and 'MAJOR.MINOR.PATCH' are all accepted so that
    requirement clauses such as '>=0.1' compare cleanly against full versions.
    """
    match = _VERSION_PATTERN.match(text.strip())
    if match is None:
        raise ValueError(
            f"version {text!r} is malformed; expected 'MAJOR.MINOR.PATCH' (for example '0.1.0')"
        )
    prerelease = (match.group("prerelease") or "").lstrip("-.")
    return VersionInfo(
        int(match.group("major")),
        int(match.group("minor") or 0),
        int(match.group("patch") or 0),
        prerelease,
    )


def current_version() -> VersionInfo:
    """Return the parsed version of the installed chassis."""
    return parse_version(__version__)


def format_version(version: str = __version__, api: str = CORE_API_VERSION) -> str:
    """Render the one-line version banner used by 'kdx --version'."""
    return f"korodexios-core {version} (core api v{api}, python chassis)"
