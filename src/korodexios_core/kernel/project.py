"""The one identity the chassis reports for the project it hosts."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Final

from ..constants import PACKAGE_NAME
from ..version import __version__

PROJECT_NAME_KEY: Final[str] = "core.project.name"
PROJECT_VERSION_KEY: Final[str] = "core.project.version"
PROJECT_CONTRACT_DIR_KEY: Final[str] = "core.project.contract_dir"

DEFAULT_CONTRACT_DIR: Final[str] = "contracts"
UNKNOWN_VERSION: Final[str] = "unknown"


@dataclass(frozen=True, slots=True)
class ProjectIdentity:
    """The project triple: who it is, which version it is, where its contracts live."""

    name: str
    version: str
    contract_dir: str

    def as_dict(self) -> dict[str, Any]:
        """Return the triple as a JSON-friendly mapping."""
        return {
            "name": self.name,
            "version": self.version,
            "contract_dir": self.contract_dir,
        }


def installed_version(distribution: str) -> str:
    """Return the installed version of *distribution*, or 'unknown' when it is not."""
    name = str(distribution).strip()
    if not name:
        return UNKNOWN_VERSION
    try:
        return str(metadata.version(name))
    except Exception:  # pragma: no cover - a broken distribution record is not ours to fix
        return UNKNOWN_VERSION


def _text(config: Any, key: str) -> str:
    """Read one dotted key as text from whatever configuration object was handed in."""
    getter = getattr(config, "get_str", None)
    if callable(getter):
        value: Any = getter(key)
    else:
        lookup = getattr(config, "get", None)
        value = lookup(key) if callable(lookup) else None
    return str(value).strip() if value is not None else ""


def project_identity(config: Any, home: Path | None = None) -> ProjectIdentity:
    """Derive the project identity from configuration alone.

    A project states itself with 'core.project.name', 'core.project.version', and
    'core.project.contract_dir'. Each one falls back to something the chassis already
    knows: the app name in 'core.name', the version of its own distribution - the
    chassis reads its own number from the chassis, never from stale metadata - and a
    'contracts/' directory under the chassis home. A project therefore reports one
    identity - the same triple in 'kdx doctor', in 'kdx status --json', and in a module's
    context - without the chassis learning any project's own layout.
    """
    name = _text(config, PROJECT_NAME_KEY) or _text(config, "core.name")
    root = home if home is not None else Path.cwd()
    if not name:
        name = root.name or "project"
    distribution = _text(config, "core.distribution")
    version = _text(config, PROJECT_VERSION_KEY)
    if not version and distribution and distribution != PACKAGE_NAME:
        version = installed_version(distribution)
    if not version and distribution == PACKAGE_NAME:
        version = __version__
    declared = _text(config, PROJECT_CONTRACT_DIR_KEY) or DEFAULT_CONTRACT_DIR
    contract = Path(declared)
    resolved = contract if contract.is_absolute() else root / contract
    return ProjectIdentity(
        name=name,
        version=version or UNKNOWN_VERSION,
        contract_dir=str(resolved),
    )


__all__ = [
    "DEFAULT_CONTRACT_DIR",
    "PROJECT_CONTRACT_DIR_KEY",
    "PROJECT_NAME_KEY",
    "PROJECT_VERSION_KEY",
    "UNKNOWN_VERSION",
    "ProjectIdentity",
    "installed_version",
    "project_identity",
]

