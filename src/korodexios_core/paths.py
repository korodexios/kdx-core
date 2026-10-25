"""Filesystem layout for one chassis instance, discovered without side effects."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .kernel.locks import NamedLock

from .constants import (
    CONFIG_CANDIDATES,
    CONFIG_ENV_VARS,
    DATA_DIR_NAME,
    DEFAULT_LOCK_TIMEOUT,
    DOTENV_NAME,
    DURABLE_DIR_NAME,
    ENV_FILE_ENV_VARS,
    HOME_ENV_VAR,
    HOME_ENV_VARS,
    INSTANCE_LOCK_NAME,
    LOCAL_CONFIG_CANDIDATES,
    LOCK_FILE_SUFFIX,
    LOCKS_DIR_NAME,
    LOG_DIR_NAME,
    MODULES_DIR_NAME,
    ROOT_MARKER_FILES,
    ROOT_MARKER_PROJECTS,
    STATE_DIR_NAME,
)
from .exceptions import PathDiscoveryError

_PACKAGE_DIR = Path(__file__).resolve().parent


_MAX_LOCK_NAME = 64
_LOCK_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-. "
)


def _coerce_path(value: str | os.PathLike[str]) -> Path:
    return Path(value).expanduser()


def lock_name(name: str) -> str:
    """Return *name* as a safe named-lock label, or raise ValueError.

    A lock name becomes a file name, so it is exactly one label: no separators, no
    '..', no leading dot, at most 64 characters. A space is allowed, so
    'nightly sweep' stays readable in a report and honest on disk.
    """
    cleaned = str(name).strip()
    if not cleaned:
        raise ValueError("a named lock needs a name")
    if len(cleaned) > _MAX_LOCK_NAME:
        raise ValueError(f"lock name {name!r} is longer than {_MAX_LOCK_NAME} characters")
    if not _LOCK_CHARACTERS.issuperset(cleaned):
        raise ValueError(
            f"lock name {name!r} may only contain letters, digits, '_', '.', '-', and spaces"
        )
    if cleaned.startswith(".") or ".." in cleaned:
        raise ValueError(f"lock name {name!r} may not start with a dot or contain '..'")
    return cleaned


def is_lock_file_name(name: str) -> bool:
    """Return whether *name* names a lock file this chassis could have written.

    A lock name is exactly one label, so an editor's swap file, a dot file, and a stray
    'foo:bar.lock' are not locks merely because they end in '.lock'. A scan of the locks
    directory answers with only the files whose stem a named lock would accept.
    """
    if not name.endswith(LOCK_FILE_SUFFIX) or name.startswith("."):
        return False
    try:
        lock_name(name[: -len(LOCK_FILE_SUFFIX)])
    except ValueError:
        return False
    return True


def _is_chassis_project_file(candidate: Path) -> bool:
    if candidate.name in ROOT_MARKER_FILES:
        return True
    if candidate.name == "pyproject.toml" and candidate.is_file():
        try:
            text = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        return any(marker in text for marker in ROOT_MARKER_PROJECTS)
    return False


def discover_root(
    start: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Walk upward from 'start' looking for the project root marker."""
    env = os.environ if environ is None else environ
    override = _env_lookup(env, HOME_ENV_VARS)
    if override:
        candidate = _coerce_path(override)
        if not candidate.is_absolute():
            raise PathDiscoveryError(
                f"{HOME_ENV_VAR}={override!r} must be an absolute path",
                hint="Point KDX_HOME at the directory that holds kdx.yml.",
            )
        return candidate.resolve()

    cursor = Path(start).expanduser().resolve() if start else Path.cwd()
    if not cursor.exists():
        raise PathDiscoveryError(
            f"cannot start root discovery from missing directory {cursor}",
            hint="Run the chassis from an existing directory.",
        )
    if cursor.is_file():
        cursor = cursor.parent

    for directory in (cursor, *cursor.parents):
        for child in sorted(directory.iterdir(), key=lambda item: item.name):
            if _is_chassis_project_file(child):
                return directory
    return cursor


def _env_lookup(env: Mapping[str, str], names: Sequence[str]) -> str:
    """Return the first non-empty value among *names*, primary prefix first."""
    for name in names:
        value = env.get(name, "").strip()
        if value:
            return value
    return ""


def _anchored(value: str | os.PathLike[str], root: Path) -> Path:
    """Return *value* as an absolute path, anchored to *root* when it is relative.

    A caller may hand over a path written for another directory: "`--config kdx.yml` says
    where the file sits under the chassis root, and a pre-resolved string from a shell
    in a different working directory must not decide the answer. Anchoring happens here,
    once, before any existence check.
    """
    candidate = _coerce_path(value)
    if candidate.is_absolute():
        return candidate.resolve()
    return (root / candidate).resolve()


def _first_existing(directory: Path, names: Sequence[str]) -> Path | None:
    for name in names:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Resolved directories for one chassis run."""

    root: Path
    config_file: Path | None = None
    local_config_file: Path | None = None
    env_file: Path | None = None
    state_dir: Path = Path(".")
    log_dir: Path = Path(".")
    data_dir: Path = Path(".")
    durable_dir: Path = Path(".")
    builtin_modules_dir: Path = _PACKAGE_DIR / MODULES_DIR_NAME
    extra_module_dirs: tuple[Path, ...] = ()

    @classmethod
    def discover(
        cls,
        root: str | os.PathLike[str] | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        config_file: str | os.PathLike[str] | None = None,
        env_file: str | os.PathLike[str] | None = None,
        extra_module_dirs: Sequence[str | os.PathLike[str]] = (),
    ) -> AppPaths:
        """Discover the layout, honouring explicit arguments then environment hints."""
        env = os.environ if environ is None else environ
        resolved_root = Path(root).expanduser().resolve() if root else discover_root(environ=env)
        if not resolved_root.is_dir():
            raise PathDiscoveryError(
                f"chassis root {resolved_root} is not a directory",
                hint="Pass --home or set KDX_HOME to an existing directory.",
            )

        explicit_config = config_file or _env_lookup(env, CONFIG_ENV_VARS)
        config_path: Path | None = None
        if explicit_config:
            candidate = _anchored(explicit_config, resolved_root)
            if not candidate.is_file():
                raise PathDiscoveryError(
                    f"config file {candidate} does not exist",
                    hint="Remove the override or create the file.",
                )
            config_path = candidate
        else:
            config_path = _first_existing(resolved_root, CONFIG_CANDIDATES)
        local_config_path = _first_existing(resolved_root, LOCAL_CONFIG_CANDIDATES)
        if local_config_path is not None and local_config_path == config_path:
            local_config_path = None  # never load the same file twice

        explicit_env = env_file or _env_lookup(env, ENV_FILE_ENV_VARS)
        dotenv_path: Path | None = None
        if explicit_env:
            candidate = _anchored(explicit_env, resolved_root)
            if not candidate.is_file():
                raise PathDiscoveryError(
                    f"env file {candidate} does not exist",
                    hint="Remove the override or create the file.",
                )
            dotenv_path = candidate
        else:
            candidate = resolved_root / DOTENV_NAME
            dotenv_path = candidate if candidate.is_file() else None

        state_dir = resolved_root / STATE_DIR_NAME
        return cls(
            root=resolved_root,
            config_file=config_path,
            local_config_file=local_config_path,
            env_file=dotenv_path,
            state_dir=state_dir,
            log_dir=state_dir / Path(LOG_DIR_NAME).name,
            data_dir=state_dir / Path(DATA_DIR_NAME).name,
            durable_dir=resolved_root / DURABLE_DIR_NAME,
            builtin_modules_dir=_PACKAGE_DIR / MODULES_DIR_NAME,
            extra_module_dirs=tuple(
                Path(item).expanduser().resolve() for item in extra_module_dirs
            ),
        )

    def resolve_under_root(self, value: str | os.PathLike[str]) -> Path:
        """Resolve 'value' relative to the chassis root when it is relative."""
        candidate = _coerce_path(value)
        if candidate.is_absolute():
            return candidate.resolve()
        return (self.root / candidate).resolve()

    def with_log_dir(self, value: str | os.PathLike[str] | None) -> AppPaths:
        """Return a copy whose log directory points at 'value'."""
        if not value:
            return self
        return self._clone(log_dir=self.resolve_under_root(value))

    @property
    def lock_file(self) -> Path:
        """Return the single-instance lock path inside the state directory."""
        return self.state_dir / INSTANCE_LOCK_NAME

    @property
    def locks_dir(self) -> Path:
        """Return the directory holding this instance's named advisory locks."""
        return self.state_dir / LOCKS_DIR_NAME

    def lock_path(self, name: str) -> Path:
        """Return the file backing named lock *name*; creates nothing."""
        return self.locks_dir / f"{lock_name(name)}{LOCK_FILE_SUFFIX}"

    def lock(
        self,
        name: str,
        *,
        timeout: float | None = DEFAULT_LOCK_TIMEOUT,
        blocking: bool = True,
        metadata: Mapping[str, Any] | None = None,
        logger: Any = None,
    ) -> NamedLock:
        """Take named lock *name* in this layout; raise when it stays busy.

        The lock primitives live in the kernel, so the import is deferred: the
        filesystem layer must not pull kernel code in at module load.
        """
        from .kernel.locks import NamedLock

        handle = NamedLock(
            self.lock_path(name), name=lock_name(name), metadata=metadata, logger=logger
        )
        return handle.acquire(timeout=timeout, blocking=blocking)

    def named_lock_files(self) -> tuple[str, ...]:
        """Return every named lock file on disk, sorted; empty when none exists.

        An editor that kept a swap file in the locks directory, and a dot file, are not
        locks: only names a lock would accept are listed.
        """
        if not self.locks_dir.is_dir():
            return ()
        return tuple(
            sorted(
                item.name
                for item in self.locks_dir.iterdir()
                if item.is_file() and is_lock_file_name(item.name)
            )
        )

    def with_state_dir(self, value: str | os.PathLike[str] | None) -> AppPaths:
        """Return a copy whose state directory points at 'value'.

        The log and data directories are children of the state directory, so a
        configured state directory moves them with it unless they were configured
        explicitly.
        """
        if not value:
            return self
        state = self.resolve_under_root(value)
        return self._clone(
            state_dir=state,
            log_dir=state / Path(LOG_DIR_NAME).name,
            data_dir=state / Path(DATA_DIR_NAME).name,
        )

    def with_data_dir(self, value: str | os.PathLike[str] | None) -> AppPaths:
        """Return a copy whose data directory points at 'value'."""
        if not value:
            return self
        return self._clone(data_dir=self.resolve_under_root(value))

    def with_durable_dir(self, value: str | os.PathLike[str] | None) -> AppPaths:
        """Return a copy whose durable directory points at 'value'.

        The durable directory is a project-level role, so relocating the state
        directory never moves it: a run that throws away its logs keeps the
        databases and ledgers an operator committed.
        """
        if not value:
            return self
        return self._clone(durable_dir=self.resolve_under_root(value))

    def with_extra_module_dirs(self, dirs: Sequence[str | os.PathLike[str]]) -> AppPaths:
        """Return a copy with additional plugin search directories."""
        merged = list(self.extra_module_dirs)
        for item in dirs:
            candidate = self.resolve_under_root(item)
            if candidate not in merged:
                merged.append(candidate)
        return self._clone(extra_module_dirs=tuple(merged))

    def _clone(self, **overrides: Any) -> AppPaths:
        values: dict[str, Any] = {
            "root": self.root,
            "config_file": self.config_file,
            "local_config_file": self.local_config_file,
            "env_file": self.env_file,
            "state_dir": self.state_dir,
            "log_dir": self.log_dir,
            "data_dir": self.data_dir,
            "durable_dir": self.durable_dir,
            "builtin_modules_dir": self.builtin_modules_dir,
            "extra_module_dirs": self.extra_module_dirs,
        }
        values.update(overrides)
        return AppPaths(**values)

    def module_dir(self, name: str) -> Path:
        """Return (and create) the private data directory for one module."""
        target = self.data_dir / "modules" / name
        target.mkdir(parents=True, exist_ok=True)
        return target

    def durable_module_dir(self, name: str) -> Path:
        """Return (and create) the durable directory for one module.

        Durable means trackable: the SQLite file or ledger an operator commits
        lands here rather than in the gitignored state tree.
        """
        target = self.durable_dir / "modules" / name
        target.mkdir(parents=True, exist_ok=True)
        return target

    def ensure_directories(self) -> tuple[Path, ...]:
        """Create the runtime directories, returning the ones that now exist."""
        created: list[Path] = []
        for directory in (
            self.state_dir,
            self.log_dir,
            self.data_dir,
            self.durable_dir,
            self.locks_dir,
        ):
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise PathDiscoveryError(
                    f"cannot create runtime directory {directory}: {exc}",
                    hint="Check filesystem permissions or set core.log.dir.",
                ) from exc
            created.append(directory)
        return tuple(created)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view used by 'kdx doctor'."""
        return {
            "root": str(self.root),
            "config_file": str(self.config_file) if self.config_file else None,
            "local_config_file": str(self.local_config_file) if self.local_config_file else None,
            "env_file": str(self.env_file) if self.env_file else None,
            "state_dir": str(self.state_dir),
            "log_dir": str(self.log_dir),
            "data_dir": str(self.data_dir),
            "durable_dir": str(self.durable_dir),
            "lock_file": str(self.lock_file),
            "locks_dir": str(self.locks_dir),
            "builtin_modules_dir": str(self.builtin_modules_dir),
            "extra_module_dirs": [str(item) for item in self.extra_module_dirs],
        }

    def describe_rows(self) -> list[tuple[str, str]]:
        """Return label/value rows ready for CLI rendering."""
        rows: list[tuple[str, str]] = []
        for key, value in self.as_dict().items():
            if isinstance(value, list):
                rows.append((key, ", ".join(str(item) for item in value) or "(none)"))
            else:
                rows.append((key, str(value) if value else "(not set)"))
        return rows

    def __str__(self) -> str:
        return str(self.root)


def paths_from_mapping(data: Mapping[str, Any]) -> AppPaths:
    """Rebuild AppPaths from the output of AppPaths.as_dict."""

    def to_path(key: str) -> Path:
        value = data.get(key)
        return Path(str(value)) if value else Path(".")

    config_value = data.get("config_file")
    local_value = data.get("local_config_file")
    env_value = data.get("env_file")
    extra = data.get("extra_module_dirs") or []
    return AppPaths(
        root=to_path("root"),
        config_file=Path(str(config_value)) if config_value else None,
        local_config_file=Path(str(local_value)) if local_value else None,
        env_file=Path(str(env_value)) if env_value else None,
        state_dir=to_path("state_dir"),
        log_dir=to_path("log_dir"),
        data_dir=to_path("data_dir"),
        durable_dir=to_path("durable_dir"),
        builtin_modules_dir=to_path("builtin_modules_dir"),
        extra_module_dirs=tuple(Path(str(item)) for item in extra),
    )
