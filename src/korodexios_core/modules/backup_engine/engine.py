"""Backup planning and execution: source tree to timestamped destination folder."""

from __future__ import annotations

import fnmatch
import os
import shutil
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ...units import format_bytes

__all__ = [
    "BackupError",
    "BackupPlan",
    "BackupResult",
    "BackupSettings",
    "build_plan",
    "default_stamp",
    "describe_plan",
    "describe_result",
    "execute_plan",
]

STAMP_FORMAT = "%Y-%m-%dT%H-%M-%S"


class BackupError(ValueError):
    """A polite backup failure: a message, a hint, and no traceback."""

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return self.message if not self.hint else f"{self.message} (hint: {self.hint})"


@dataclass(frozen=True, slots=True)
class BackupSettings:
    """Everything needed to plan a backup, resolved from config plus CLI flags."""

    source: Path
    dest: Path
    include: tuple[str, ...] = ("*",)
    exclude: tuple[str, ...] = ()
    max_bytes: int | None = None
    dry_run: bool = False
    follow_symlinks: bool = False
    stamp: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view with absolute paths."""
        return {
            "source": str(self.source),
            "dest": str(self.dest),
            "include": list(self.include),
            "exclude": list(self.exclude),
            "max_bytes": self.max_bytes,
            "dry_run": self.dry_run,
            "follow_symlinks": self.follow_symlinks,
            "stamp": self.stamp or default_stamp(),
        }


@dataclass(slots=True)
class BackupPlan:
    """What a backup would do: files, bytes, target folder, and skipped entries."""

    settings: BackupSettings
    target: Path
    files: list[Path] = field(default_factory=list)
    total_bytes: int = 0
    skipped: list[str] = field(default_factory=list)

    @property
    def file_count(self) -> int:
        """Return how many files the plan covers."""
        return len(self.files)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly summary of the plan."""
        return {
            "target": str(self.target),
            "files": self.file_count,
            "total_bytes": self.total_bytes,
            "skipped": len(self.skipped),
            "sample": [str(path.relative_to(self.settings.source)) for path in self.files[:10]],
        }


@dataclass(slots=True)
class BackupResult:
    """Outcome of running (or simulating) a plan."""

    plan: BackupPlan
    copied: int = 0
    bytes_copied: int = 0
    elapsed: float = 0.0
    dry_run: bool = False
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly summary of the run."""
        return {
            "target": str(self.plan.target),
            "dry_run": self.dry_run,
            "copied": self.copied,
            "bytes_copied": self.bytes_copied,
            "elapsed_seconds": round(self.elapsed, 3),
            "errors": list(self.errors),
        }


def default_stamp(now: float | None = None) -> str:
    """Return a filesystem-safe timestamp for the destination folder."""
    return datetime.fromtimestamp(now if now is not None else time.time()).strftime(STAMP_FORMAT)


def build_plan(settings: BackupSettings, *, stamp: str | None = None) -> BackupPlan:
    """Walk the source tree and decide what a backup would copy."""
    source = Path(settings.source)
    if not source.exists():
        raise BackupError(
            f"backup source {source} does not exist",
            hint="Point --source (or modules.backup_engine.source) at a real directory.",
        )
    if not source.is_dir():
        raise BackupError(f"backup source {source} is not a directory", hint="Backups copy trees.")
    label = stamp or settings.stamp or default_stamp()
    target = Path(settings.dest) / label
    plan = BackupPlan(settings=settings, target=target)
    for root, dirs, files in os.walk(source, followlinks=settings.follow_symlinks):
        root_path = Path(root)
        dirs[:] = sorted(
            name
            for name in dirs
            if not _excluded(_relative(source, root_path / name), settings.exclude)
        )
        for name in sorted(files):
            candidate = root_path / name
            relative = _relative(source, candidate)
            if _excluded(relative, settings.exclude):
                plan.skipped.append(relative)
                continue
            if not _included(relative, settings.include):
                plan.skipped.append(relative)
                continue
            if candidate.is_symlink() and not settings.follow_symlinks:
                plan.skipped.append(f"{relative} (symlink)")
                continue
            try:
                size = candidate.stat().st_size
            except OSError as exc:
                plan.skipped.append(f"{relative} ({exc.strerror or 'stat failed'})")
                continue
            plan.files.append(candidate)
            plan.total_bytes += size
    if settings.max_bytes is not None and plan.total_bytes > settings.max_bytes:
        raise BackupError(
            f"backup would copy {plan.total_bytes} bytes, over the {settings.max_bytes} byte limit",
            hint="Raise modules.backup_engine.max_size, pass --max-size, or narrow --include.",
        )
    return plan


def execute_plan(plan: BackupPlan, *, dry_run: bool | None = None) -> BackupResult:
    """Copy planned files into the target folder (or simulate when dry_run)."""
    simulate = plan.settings.dry_run if dry_run is None else bool(dry_run)
    result = BackupResult(plan=plan, dry_run=simulate)
    started = time.perf_counter()
    source = Path(plan.settings.source)
    if not simulate:
        plan.target.mkdir(parents=True, exist_ok=True)
    for candidate in plan.files:
        relative = candidate.relative_to(source)
        destination = plan.target / relative
        if simulate:
            result.copied += 1
            result.bytes_copied += candidate.stat().st_size
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(candidate), str(destination))
            result.copied += 1
            result.bytes_copied += destination.stat().st_size
        except OSError as exc:
            result.errors.append(f"{relative}: {exc.strerror or exc}")
    result.elapsed = time.perf_counter() - started
    return result


def describe_plan(plan: BackupPlan) -> list[tuple[str, Any]]:
    """Return key/value rows describing a plan for the CLI."""
    return [
        ("source", str(plan.settings.source)),
        ("destination", str(plan.target)),
        ("include", ", ".join(plan.settings.include) or "-"),
        ("exclude", ", ".join(plan.settings.exclude) or "-"),
        ("files", plan.file_count),
        ("total size", _human(plan.total_bytes)),
        ("skipped", len(plan.skipped)),
        ("mode", "dry run" if plan.settings.dry_run else "copy"),
    ]


def describe_result(result: BackupResult) -> list[tuple[str, Any]]:
    """Return key/value rows describing a run for the CLI."""
    return [
        ("target", str(result.plan.target)),
        ("mode", "dry run" if result.dry_run else "copy"),
        ("files copied", result.copied),
        ("bytes copied", _human(result.bytes_copied)),
        ("elapsed", f"{result.elapsed:.3f} s"),
        ("errors", ", ".join(result.errors) if result.errors else "none"),
    ]


def _relative(source: Path, candidate: Path) -> str:
    try:
        return candidate.relative_to(source).as_posix()
    except ValueError:
        return candidate.as_posix()


def _excluded(relative: str, patterns: Sequence[str]) -> bool:
    parts = relative.split("/")
    for pattern in patterns:
        cleaned = pattern.strip()
        if not cleaned:
            continue
        if any(fnmatch.fnmatch(part, cleaned) for part in parts):
            return True
        if fnmatch.fnmatch(relative, cleaned) or fnmatch.fnmatch(relative, f"{cleaned}/*"):
            return True
    return False


def _included(relative: str, patterns: Sequence[str]) -> bool:
    cleaned = [pattern.strip() for pattern in patterns if pattern.strip()]
    if not cleaned or cleaned == ["*"]:
        return True
    name = Path(relative).name
    for pattern in cleaned:
        if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(name, pattern):
            return True
    return False


def _human(value: int) -> str:
    return format_bytes(value)
