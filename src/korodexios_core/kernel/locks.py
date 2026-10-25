"""Named advisory locks: one isolated coordination point per name.

The instance lock guards the whole process. An application needs more than one
critical section - a reindex here, a file write there, a nightly job somewhere
else - and one shared lock serialises work that never competed. A named lock is
the same OS-level advisory lock on its own file under ``state_dir/locks``, so a
held name blocks only its own name, a crashed owner never leaves the name stuck,
and the payload still names the live owner for the operator.

Nothing here knows what a lock protects: the name is the whole contract, which is
exactly what keeps the chassis generic.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..constants import (
    DEFAULT_LOCK_TIMEOUT,
    ENCODING,
    LOCK_FILE_SUFFIX,
    LOCK_POLL_INTERVAL,
)
from ..exceptions import LockTimeoutError
from ..paths import is_lock_file_name, lock_name
from .instance_lock import LockHolder, lock_descriptor, pid_alive, unlock_descriptor

__all__ = [
    "DEFAULT_LOCK_TIMEOUT",
    "LOCK_FILE_SUFFIX",
    "LockHolder",
    "LockTimeoutError",
    "NamedLock",
    "NamedLockRegistry",
    "lock_name",
]


@dataclass(slots=True)
class _LockHandle:
    """Context manager returned by :meth:`NamedLockRegistry.hold`.

    The name is already reserved by the time this object exists, so taking it is
    one NamedLock.acquire call and the registry stays honest about names that
    nobody holds.
    """

    registry: NamedLockRegistry
    name: str
    timeout: float | None = DEFAULT_LOCK_TIMEOUT
    blocking: bool = True
    metadata: Mapping[str, Any] | None = field(default=None)
    # Set by NamedLockRegistry.hold when it reserves the name; None until then.
    lock: NamedLock | None = None

    def __enter__(self) -> NamedLock:
        reserved = self.lock
        if reserved is None:
            # Constructed without a reservation: the registry takes the name and
            # records it, which is also the re-entrant path 'acquire' documents.
            self.lock = self.registry.acquire(
                self.name,
                timeout=self.timeout,
                blocking=self.blocking,
                metadata=self.metadata,
            )
            return self.lock
        try:
            self.lock = reserved.acquire(timeout=self.timeout, blocking=self.blocking)
        except LockTimeoutError:
            # The reservation was this call's alone: hand the name back so the
            # registry never lists a lock nobody holds.
            self.registry.release(self.name)
            self.lock = None
            raise
        return self.lock

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.lock is not None:
            # Released and forgotten: a registry that still listed a free lock
            # would report a name nobody holds.
            self.registry.release(self.name)
            self.lock = None


class NamedLock:
    """Advisory exclusive lock over one named file, with the owner inside it."""

    def __init__(
        self,
        path: Path,
        *,
        name: str = "",
        metadata: Mapping[str, Any] | None = None,
        logger: Any = None,
        poll_interval: float = LOCK_POLL_INTERVAL,
    ) -> None:
        self._path = Path(path)
        self._name = lock_name(name or self._path.stem)
        self._metadata: dict[str, Any] = dict(metadata or {})
        self._logger = logger
        self._poll = max(0.005, float(poll_interval))
        self._descriptor: int | None = None
        self._acquired_at: float | None = None

    @property
    def name(self) -> str:
        """Return the lock's name, the only identity a caller needs."""
        return self._name

    @property
    def path(self) -> Path:
        """Return the file this lock guards."""
        return self._path

    @property
    def held(self) -> bool:
        """Return True while this process owns the lock."""
        return self._descriptor is not None

    @property
    def held_seconds(self) -> float:
        """Return how long this process has held the lock."""
        if self._acquired_at is None:
            return 0.0
        return max(0.0, time.monotonic() - self._acquired_at)

    def acquire(
        self,
        timeout: float | None = DEFAULT_LOCK_TIMEOUT,
        *,
        blocking: bool = True,
    ) -> NamedLock:
        """Take the lock; raise :class:`LockTimeoutError` when it stays busy.

        ``timeout=0.0`` (the default) is a single non-blocking attempt. A positive
        timeout polls until the deadline. ``timeout=None`` means "wait as long as it
        takes" while *blocking* is True - the plain blocking form every caller
        expects from a lock - and one attempt when *blocking* is False. Re-acquiring
        a lock this handle already owns is a no-op.
        """
        if self._descriptor is not None:
            return self
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(str(self._path), os.O_RDWR | os.O_CREAT, 0o600)
        started = time.monotonic()
        deadline: float | None
        if not blocking:
            deadline = started
        elif timeout is None:
            deadline = None
        else:
            deadline = started + max(0.0, float(timeout))
        while True:
            try:
                lock_descriptor(descriptor)
                break
            except OSError as exc:
                remaining = None if deadline is None else deadline - time.monotonic()
                if deadline is None:
                    remaining = self._poll
                if remaining is None or remaining <= 0:
                    os.close(descriptor)
                    raise self._busy_error(exc, timeout) from exc
                time.sleep(min(self._poll, remaining))
        self._descriptor = descriptor
        self._acquired_at = time.monotonic()
        stale = self._takeover_stale()
        if stale is not None and self._logger is not None:
            self._logger.info(
                "taking over a stale named lock from a dead process",
                name=self._name,
                path=str(self._path),
                old_pid=stale.pid,
            )
        try:
            self._write_payload()
        except OSError:
            # A lock that cannot say who holds it is not a lock the next process can
            # trust, so the claim is withdrawn and the failure is the caller's to read.
            self.release()
            raise
        if self._logger is not None:
            self._logger.debug(
                "named lock acquired", name=self._name, path=str(self._path), pid=os.getpid()
            )
        return self

    def release(self) -> None:
        """Release the lock and empty its payload; the file itself stays behind."""
        descriptor = self._descriptor
        self._descriptor = None
        self._acquired_at = None
        if descriptor is None:
            return
        try:
            unlock_descriptor(descriptor)
        finally:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        if self._logger is not None:
            self._logger.debug("named lock released", name=self._name, path=str(self._path))

    def holder(self) -> LockHolder | None:
        """Return the payload the lock file carries, or None when unreadable."""
        try:
            raw = self._path.read_text(encoding=ENCODING)
        except (OSError, UnicodeDecodeError):
            return None
        try:
            data = json.loads(raw) if raw.strip() else None
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        holder = LockHolder.from_mapping(data)
        return holder if holder.pid else None

    def is_stale(self) -> bool:
        """Return True when the recorded owner is gone but the file stays behind."""
        holder = self.holder()
        return holder is not None and not holder.is_alive

    def describe(self) -> str:
        """Return the operator-facing line shown by 'kdx doctor'."""
        if self._descriptor is not None:
            return f"{self._name}: held by this process (pid {os.getpid()})"
        holder = self.holder()
        if holder is not None and holder.is_alive:
            return f"{self._name}: held by {holder.describe()}"
        if holder is not None:
            return f"{self._name}: stale (last held by pid {holder.pid})"
        return f"{self._name}: free"

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view of this lock."""
        holder = self.holder()
        return {
            "name": self._name,
            "path": str(self._path),
            "held": self._descriptor is not None,
            "held_seconds": round(self.held_seconds, 3),
            "stale": self.is_stale(),
            "holder": holder.as_dict() if holder else None,
        }

    def _busy_error(self, exc: OSError, timeout: float | None) -> LockTimeoutError:
        """Build the failure that names the live owner, not just the syscall."""
        holder = self.holder()
        if holder is not None and holder.is_alive:
            message = f"named lock {self._name!r} is held by {holder.describe()}"
        elif holder is not None:
            message = f"named lock {self._name!r} is locked by a process this chassis cannot read"
        else:
            message = f"named lock {self._name!r} is busy"
        return LockTimeoutError(
            message,
            name=self._name,
            timeout=timeout,
            hint="wait for the owner, raise the timeout, or run 'kdx doctor' to see the holder",
        )

    def _takeover_stale(self) -> LockHolder | None:
        """Return the stale holder whose claim this process just adopted, if any.

        A process that died by ``kill -9``, a power loss, or an OS crash leaves its
        lock file behind with a dead PID inside, while the OS-level lock is already
        free. ``flock`` (or ``msvcrt``) succeeds against that file, so the payload is
        the only thing still claiming it: when the recorded owner is gone the claim
        is stale, and this process adopts it by rewriting the payload with its own
        details. A lock whose owner is still alive - or a file that is not a
        readable payload at all - is left untouched, because its descriptor is the
        one the other process actually holds.
        """
        descriptor = self._descriptor
        if descriptor is None:
            return None
        try:
            raw = os.read(descriptor, 65536).decode(ENCODING)
        except (OSError, UnicodeDecodeError):
            return None
        try:
            data = json.loads(raw) if raw.strip() else None
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        holder = LockHolder.from_mapping(data)
        if not holder.pid or holder.is_alive:
            return None
        return holder

    def _write_payload(self) -> None:
        """Write the owner payload into the locked file; raise on an I/O failure.

        The payload is what makes an advisory lock explainable, so a failed write is a
        failed acquisition: `acquire()` releases the descriptor it just took rather than
        leaving a held lock whose file says nothing about its owner.
        """
        descriptor = self._descriptor
        if descriptor is None:
            return
        payload = {
            "pid": os.getpid(),
            "label": str(self._metadata.get("label", self._name)),
            "name": self._name,
            "started_at": float(self._metadata.get("started_at", time.time())),
            "host": str(self._metadata.get("host", "")),
            "port": int(self._metadata.get("port", 0) or 0),
        }
        text = json.dumps(payload, sort_keys=True) + "\n"
        try:
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            with os.fdopen(os.dup(descriptor), "w", encoding=ENCODING) as handle:
                handle.write(text)
        except OSError as exc:
            if self._logger is not None:
                self._logger.warning(
                    f"named lock payload unwritable: {exc}",
                    name=self._name,
                    path=str(self._path),
                )
            raise

    def __enter__(self) -> NamedLock:
        return self.acquire(blocking=True)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()

    def __repr__(self) -> str:
        return f"NamedLock(name={self._name!r}, path={str(self._path)!r}, held={self.held})"


class NamedLockRegistry:
    """Every named lock this process holds, keyed by name, released as one unit.

    The registry exists so shutdown has one place to go: whatever a module
    acquired by name, `release_all()` hands back before the process exits, even
    when a module forgot to. It also answers the operator's question - which names
    are held, by whom, and which are stale - without opening a socket.
    """

    def __init__(
        self,
        directory: Path | str,
        *,
        logger: Any = None,
        poll_interval: float = LOCK_POLL_INTERVAL,
    ) -> None:
        self._directory = Path(directory)
        self._logger = logger
        self._poll = max(0.005, float(poll_interval))
        self._held: dict[str, NamedLock] = {}
        self._guard = threading.RLock()

    @property
    def directory(self) -> Path:
        """Return the directory every named lock file lives in."""
        return self._directory

    def path_for(self, name: str) -> Path:
        """Return the file backing *name*; creates nothing."""
        return self._directory / f"{lock_name(name)}{LOCK_FILE_SUFFIX}"

    def acquire(
        self,
        name: str,
        *,
        timeout: float | None = DEFAULT_LOCK_TIMEOUT,
        blocking: bool = True,
        metadata: Mapping[str, Any] | None = None,
    ) -> NamedLock:
        """Take named lock *name*, or raise :class:`LockTimeoutError`.

        Re-acquiring a name this registry already holds returns the same handle: a
        lock is a resource, not a counter, so a second call cannot invite an
        unbalanced release.
        """
        cleaned = lock_name(name)
        with self._guard:
            existing = self._held.get(cleaned)
            if existing is not None and existing.held:
                return existing
        handle = NamedLock(
            self.path_for(cleaned),
            name=cleaned,
            metadata=metadata,
            logger=self._logger,
            poll_interval=self._poll,
        )
        handle.acquire(timeout=timeout, blocking=blocking)
        with self._guard:
            self._held[cleaned] = handle
        return handle

    def hold(
        self,
        name: str,
        *,
        timeout: float | None = DEFAULT_LOCK_TIMEOUT,
        blocking: bool = True,
        metadata: Mapping[str, Any] | None = None,
    ) -> _LockHandle:
        """Reserve *name* now; return a context manager that takes and releases it.

        Inspection and reservation happen in one guarded step, so two threads that
        call hold on the same name at the same moment cannot both pass a check and
        then race into acquire: the loser gets the collision error immediately
        instead of a timeout that depends on who won the file lock first. Nesting
        the same lock is a design smell worth a stack trace, not a double-hold.
        """
        cleaned = lock_name(name)
        handle = _LockHandle(self, cleaned, timeout, blocking, metadata)
        with self._guard:
            current = self._held.get(cleaned)
            if current is not None:
                raise LockTimeoutError(
                    f"named lock {cleaned!r} is already held by this process",
                    name=cleaned,
                    timeout=timeout,
                    hint="release it before nesting 'with registry.hold(...)'",
                )
            handle.lock = NamedLock(
                self.path_for(cleaned),
                name=cleaned,
                metadata=metadata,
                logger=self._logger,
                poll_interval=self._poll,
            )
            self._held[cleaned] = handle.lock
        return handle

    def get(self, name: str) -> NamedLock | None:
        """Return the handle this registry holds for *name*, or None."""
        with self._guard:
            return self._held.get(lock_name(name))

    def held(self, name: str) -> bool:
        """Return True when this process holds *name*."""
        handle = self.get(name)
        return bool(handle is not None and handle.held)

    def names(self) -> tuple[str, ...]:
        """Return every name this registry holds, sorted."""
        with self._guard:
            return tuple(sorted(self._held))

    def release(self, name: str) -> bool:
        """Release *name*; return whether this registry held it."""
        cleaned = lock_name(name)
        with self._guard:
            handle = self._held.pop(cleaned, None)
        if handle is None:
            return False
        handle.release()
        return True

    def release_all(self) -> int:
        """Release every name this registry holds; return how many went free."""
        with self._guard:
            handles = list(self._held.values())
            self._held.clear()
        for handle in handles:
            handle.release()
        return len(handles)

    def stale_names(self) -> tuple[str, ...]:
        """Return lock files on disk whose recorded owner is gone."""
        return tuple(sorted(item.stem for item in self._scan_files() if _stale_file(item)))

    def status(self) -> list[dict[str, Any]]:
        """Return one row per lock file on disk: name, holder, staleness."""
        rows: list[dict[str, Any]] = []
        for item in self._scan_files():
            rows.append(NamedLock(item, name=item.stem).as_dict())
        return rows

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view for 'kdx doctor' and the status snapshot."""
        return {
            "directory": str(self._directory),
            "held": len(self._held),
            "names": list(self.names()),
            "on_disk": [item.stem for item in self._scan_files()],
            "stale": list(self.stale_names()),
        }

    def describe(self) -> str:
        """Return the one-line summary shown by 'kdx doctor'."""
        names = self.names()
        if not names:
            return "no named locks held"
        return f"{len(names)} named lock(s) held: {', '.join(names)}"

    def _scan_files(self) -> tuple[Path, ...]:
        """Return every lock file the chassis wrote; empty when the directory is absent.

        Editor artefacts and dot files in the locks directory are not locks, and a stale
        inspection never asks who held a swap file.
        """
        if not self._directory.is_dir():
            return ()
        return tuple(
            sorted(
                item
                for item in self._directory.iterdir()
                if item.is_file() and is_lock_file_name(item.name)
            )
        )

    def __len__(self) -> int:
        return len(self._held)

    def __contains__(self, name: object) -> bool:
        return self.held(str(name))

    def __repr__(self) -> str:
        return f"NamedLockRegistry(directory={str(self._directory)!r}, held={list(self.names())})"


def _stale_file(path: Path) -> bool:
    """Return True when *path* names a lock whose owner process is gone."""
    try:
        raw = path.read_text(encoding=ENCODING)
        data = json.loads(raw) if raw.strip() else None
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    try:
        pid = int(data.get("pid", 0) or 0)
    except (TypeError, ValueError):
        return False
    return bool(pid) and not pid_alive(pid)
