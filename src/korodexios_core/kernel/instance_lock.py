"""Single-instance process lock: one chassis daemon per root directory.

Two daemons on one root silently split the state directory and, worse, the
second one binds a fallback port while the operator believes it took the
configured one. The lock is advisory at the OS level (flock on POSIX, a
non-blocking byte-range lock on Windows), so a crashed owner never leaves a
lock behind, and the payload names the live owner for the operator.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..constants import ENCODING, INSTANCE_LOCK_NAME
from ..exceptions import InstanceLockError

__all__ = [
    "INSTANCE_LOCK_NAME",
    "InstanceLock",
    "LockHolder",
    "lock_descriptor",
    "pid_alive",
    "unlock_descriptor",
]

_LOCK_BYTES: int = 4096


def _platform_locker() -> Any:
    """Return the platform module that can take a non-blocking exclusive lock."""
    if os.name == "nt":  # pragma: no cover - Windows only
        import msvcrt

        return msvcrt
    import fcntl

    return fcntl


def lock_descriptor(descriptor: int, *, block: bool = False) -> None:
    """Take the platform's exclusive lock on *descriptor*.

    The non-blocking form (the default) raises OSError the moment the lock is
    busy, which is what every chassis caller wants: fail fast, then say who holds
    it. ``block=True`` waits on the OS instead, for a caller that would rather
    queue than poll.
    """
    if os.name == "nt":  # pragma: no cover - Windows only
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        if block:
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, _LOCK_BYTES)  # type: ignore[attr-defined]
        else:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, _LOCK_BYTES)  # type: ignore[attr-defined]
        return
    import fcntl

    flags = fcntl.LOCK_EX if block else fcntl.LOCK_EX | fcntl.LOCK_NB
    fcntl.flock(descriptor, flags)


def unlock_descriptor(descriptor: int) -> None:
    """Release the platform's exclusive lock on *descriptor*."""
    if os.name == "nt":  # pragma: no cover - Windows only
        import msvcrt

        with contextlib.suppress(OSError):
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, _LOCK_BYTES)  # type: ignore[attr-defined]
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


def pid_alive(pid: int) -> bool:
    """Return True when *pid* still names a live process."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


@dataclass(frozen=True, slots=True)
class LockHolder:
    """What the lock file says about the daemon that owns it."""

    pid: int = 0
    label: str = ""
    host: str = ""
    port: int = 0
    started_at: float = 0.0

    @property
    def is_alive(self) -> bool:
        """Return True when the recorded process still exists."""
        return pid_alive(self.pid)

    def uptime(self, now: float | None = None) -> float:
        """Return how long the recorded owner has been running."""
        if not self.started_at:
            return 0.0
        return max(0.0, (time.time() if now is None else now) - self.started_at)

    def describe(self) -> str:
        """Return the operator-facing line naming the running daemon."""
        where = f" at {self.host}:{self.port}" if self.host and self.port else ""
        label = self.label or "chassis"
        return f"pid {self.pid} ({label}){where}"

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view of the holder."""
        return {
            "pid": self.pid,
            "label": self.label,
            "host": self.host,
            "port": self.port,
            "started_at": self.started_at,
            "alive": self.is_alive,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> LockHolder:
        """Build a holder from the lock-file payload, tolerating partial data."""
        try:
            pid = int(data.get("pid", 0) or 0)
        except (TypeError, ValueError):
            pid = 0
        try:
            port = int(data.get("port", 0) or 0)
        except (TypeError, ValueError):
            port = 0
        try:
            started = float(data.get("started_at", 0.0) or 0.0)
        except (TypeError, ValueError):
            started = 0.0
        return cls(
            pid=pid,
            label=str(data.get("label", "") or ""),
            host=str(data.get("host", "") or ""),
            port=port,
            started_at=started,
        )


class InstanceLock:
    """Advisory exclusive lock over one file, with the owner's identity inside."""

    def __init__(
        self,
        path: Path,
        *,
        metadata: Mapping[str, Any] | None = None,
        logger: Any = None,
    ) -> None:
        self._path = Path(path)
        self._metadata: dict[str, Any] = dict(metadata or {})
        self._logger = logger
        self._descriptor: int | None = None
        self._locker = _platform_locker()

    @property
    def path(self) -> Path:
        """Return the lock file this instance guards."""
        return self._path

    @property
    def held(self) -> bool:
        """Return True while this process owns the lock."""
        return self._descriptor is not None

    def acquire(self) -> InstanceLock:
        """Take the lock, or raise InstanceLockError naming the live owner."""
        if self._descriptor is not None:
            return self
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(str(self._path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            self._lock_descriptor(descriptor)
        except OSError as exc:
            os.close(descriptor)
            holder = self.holder()
            if holder is not None and holder.is_alive:
                raise InstanceLockError(
                    f"another chassis daemon already owns {self._path.parent}: "
                    f"{holder.describe()}",
                    hint="Stop that daemon first; two daemons on one root share one "
                    "state directory and one control plane.",
                ) from exc
            raise InstanceLockError(
                f"the instance lock at {self._path} is held by a process this chassis "
                "cannot identify",
                hint="Remove the lock file only when no chassis daemon is running.",
            ) from exc
        self._descriptor = descriptor
        stale = self._takeover_stale()
        if stale is not None and self._logger is not None:
            self._logger.info(
                "taking over a stale instance lock from a dead process",
                path=str(self._path),
                old_pid=stale.pid,
            )
        self._write_payload()
        if self._logger is not None:
            self._logger.debug("instance lock acquired", path=str(self._path), pid=os.getpid())
        return self

    def update(self, metadata: Mapping[str, Any]) -> InstanceLock:
        """Merge *metadata* into the payload and rewrite it (host and port land here)."""
        self._metadata.update({str(key): value for key, value in metadata.items()})
        if self._descriptor is not None:
            self._write_payload()
        return self

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
        """Return True when the recorded owner is gone but the file remains."""
        holder = self.holder()
        return holder is not None and not holder.is_alive

    def release(self) -> None:
        """Release the lock and empty the payload; the file itself stays behind."""
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is None:
            return
        try:
            with contextlib.suppress(OSError):
                os.ftruncate(descriptor, 0)
            self._unlock_descriptor(descriptor)
        finally:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        if self._logger is not None:
            self._logger.debug("instance lock released", path=str(self._path))

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view for 'kdx doctor'."""
        holder = self.holder()
        return {
            "path": str(self._path),
            "held": self.held,
            "stale": self.is_stale(),
            "holder": holder.as_dict() if holder else None,
        }

    def describe(self) -> str:
        """Return the one-line status shown by 'kdx doctor'."""
        if self._descriptor is not None:
            return f"held by this process (pid {os.getpid()}) at {self._path}"
        holder = self.holder()
        if holder is not None and holder.is_alive:
            return f"held by {holder.describe()}"
        return f"free ({self._path})"

    def _lock_descriptor(self, descriptor: int) -> None:
        """Take the platform's non-blocking exclusive lock."""
        lock_descriptor(descriptor)

    def _unlock_descriptor(self, descriptor: int) -> None:
        """Release the platform's exclusive lock."""
        unlock_descriptor(descriptor)

    def _takeover_stale(self) -> LockHolder | None:
        """Return the stale holder whose claim this process just adopted, if any.

        A daemon that died by ``kill -9``, a power loss, or an OS crash leaves its
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
        """Write the owner payload into the locked file."""
        descriptor = self._descriptor
        if descriptor is None:
            return
        payload = {
            "pid": os.getpid(),
            "started_at": self._metadata.get("started_at", time.time()),
            "label": self._metadata.get("label", "chassis"),
            "host": self._metadata.get("host", ""),
            "port": self._metadata.get("port", 0),
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
                    f"instance lock payload unreadable: {exc}", path=str(self._path)
                )

    def __enter__(self) -> InstanceLock:
        return self.acquire()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()

    def __repr__(self) -> str:
        return f"InstanceLock(path={str(self._path)!r}, held={self.held})"
