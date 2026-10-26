"""A verified file set: the chassis hashes what a project says it owns."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

CONTRACT_CHUNK: Final[int] = 65_536
CONTRACT_NAMED_ROWS: Final[int] = 8


def sha256_file(path: Path) -> str:
    """Return the hex SHA-256 of *path*, read in blocks so a large file stays cheap."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        block = handle.read(CONTRACT_CHUNK)
        while block:
            digest.update(block)
            block = handle.read(CONTRACT_CHUNK)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class ContractRow:
    """One declared file: where it is, what it hashes to, and the project's words for it.

    'classification' and 'merge_policy' are carried verbatim. The chassis never interprets
    them, so a project that sorts its files into kits, or one that replaces a file only
    while it is pristine, keeps that vocabulary in its own layer.
    """

    path: str
    sha256: str
    classification: str = ""
    merge_policy: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Return the row as a JSON-friendly mapping."""
        return {
            "path": self.path,
            "sha256": self.sha256,
            "classification": self.classification,
            "merge_policy": self.merge_policy,
        }


@dataclass(frozen=True, slots=True)
class ContractDrift:
    """One declared file whose bytes answer to a different hash."""

    path: str
    declared: str
    actual: str

    def as_dict(self) -> dict[str, Any]:
        """Return the finding as a JSON-friendly mapping."""
        return {"path": self.path, "declared": self.declared, "actual": self.actual}


@dataclass(frozen=True, slots=True)
class ContractReport:
    """What the chassis found when it hashed the declared files. It never wrote one."""

    rows: tuple[ContractRow, ...] = ()
    verified: tuple[str, ...] = ()
    drifted: tuple[ContractDrift, ...] = ()
    missing: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Return True when nothing declared is missing and nothing drifted."""
        return not self.drifted and not self.missing

    def as_dict(self) -> dict[str, Any]:
        """Return the full report, so a caller renders the counts it wants."""
        return {
            "declared": len(self.rows),
            "verified": list(self.verified),
            "drifted": [drift.as_dict() for drift in self.drifted],
            "missing": list(self.missing),
            "ok": self.ok,
            "rows": [row.as_dict() for row in self.rows],
        }

    def summary(self) -> str:
        """Return the counts a report prints in one line."""
        if not self.rows:
            return "no contracts declared"
        parts = [f"{len(self.rows)} declared", f"{len(self.verified)} match"]
        if self.drifted:
            parts.append(f"{len(self.drifted)} drifted")
        if self.missing:
            parts.append(f"{len(self.missing)} missing")
        return ", ".join(parts)


def contract_rows(report: Any) -> tuple[ContractRow, ...]:
    """Normalize what a 'contracts()' hook returned into rows.

    A hook may return 'ContractRow' objects, mappings carrying the same four keys, or a
    sequence of either, and 'None' is a valid answer for a module that declares nothing.
    A row whose 'sha256' is empty states only that the file must exist.
    """
    if report is None:
        return ()
    if isinstance(report, (ContractRow, Mapping, str)):
        items: list[Any] = [report]
    else:
        try:
            items = list(report)
        except TypeError:
            return ()
    rows: list[ContractRow] = []
    for item in items:
        if isinstance(item, ContractRow):
            rows.append(item)
        elif isinstance(item, Mapping):
            path = str(item.get("path", "") or "").strip()
            if not path:
                continue
            rows.append(
                ContractRow(
                    path=path,
                    sha256=str(item.get("sha256", "") or "").strip(),
                    classification=str(item.get("classification", "") or ""),
                    merge_policy=str(item.get("merge_policy", "") or ""),
                )
            )
    return tuple(rows)


def dedupe_rows(rows: Sequence[ContractRow]) -> tuple[ContractRow, ...]:
    """Keep the first declaration of each path, so two hooks cannot fight over one file."""
    seen: dict[str, ContractRow] = {}
    for row in rows:
        seen.setdefault(str(row.path).strip(), row)
    return tuple(seen.values())


def verify_contracts(rows: Sequence[ContractRow], root: Path | None = None) -> ContractReport:
    """Hash each declared file and compare it, without touching a single one of them.

    A relative 'path' resolves under *root* - the project's contract directory - and an
    absolute one is read as it stands. A declared file that cannot be read is reported as
    missing: the chassis has no business guessing the bytes of a file it cannot open, and
    it certainly does not rewrite the file it was only asked to verify.
    """
    ordered = dedupe_rows(rows)
    verified: list[str] = []
    drifted: list[ContractDrift] = []
    missing: list[str] = []
    for row in ordered:
        declared = str(row.sha256).strip().lower()
        candidate = Path(row.path)
        resolved = candidate if candidate.is_absolute() or root is None else root / candidate
        try:
            actual = sha256_file(resolved) if resolved.is_file() else ""
        except OSError:
            actual = ""
        if not actual:
            missing.append(row.path)
        elif not declared or actual == declared:
            verified.append(row.path)
        else:
            drifted.append(ContractDrift(path=row.path, declared=declared, actual=actual))
    return ContractReport(
        rows=ordered,
        verified=tuple(verified),
        drifted=tuple(drifted),
        missing=tuple(missing),
    )


__all__ = [
    "CONTRACT_CHUNK",
    "CONTRACT_NAMED_ROWS",
    "ContractDrift",
    "ContractReport",
    "ContractRow",
    "contract_rows",
    "dedupe_rows",
    "sha256_file",
    "verify_contracts",
]

