"""Worker supervision: when a module's background worker dies, say so.

A thread that exits quietly is the classic silent daemon failure: the module
still reports 'running' while nothing works. Modules register the workers they
start; the chassis checks them on each lifecycle tick, and a worker that died
unexpectedly moves its module to FAILED where 'kdx doctor' and 'kdx status'
can see it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

__all__ = ["WorkerRecord", "WorkerSupervisor"]


@dataclass(slots=True)
class WorkerRecord:
    """One background worker a module asked the chassis to watch."""

    module: str
    name: str
    thread: threading.Thread
    expected: bool = True
    started_at: float = field(default_factory=time.time)
    reported: bool = False
    died_at: float | None = None

    @property
    def alive(self) -> bool:
        """Return True while the watched thread is still running."""
        return self.thread.is_alive()

    def describe(self) -> str:
        """Return the one-line rendering used by logs and reports."""
        state = "alive" if self.alive else "dead"
        return f"{self.module}/{self.name} ({state})"

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view of the worker."""
        return {
            "module": self.module,
            "name": self.name,
            "thread": self.thread.name,
            "alive": self.alive,
            "expected": self.expected,
            "died_at": round(self.died_at, 3) if self.died_at else None,
            "uptime_seconds": round(max(0.0, time.time() - self.started_at), 3),
        }


class WorkerSupervisor:
    """Keeps the worker records and reports the ones that died since last check."""

    def __init__(self, logger: Any = None, *, enabled: bool = True) -> None:
        self._logger = logger
        self._enabled = bool(enabled)
        self._records: list[WorkerRecord] = []
        self._lock = threading.RLock()
        self._checks = 0
        self._losses = 0

    @property
    def enabled(self) -> bool:
        """Return True when supervision passes report dead workers."""
        return self._enabled

    def watch(
        self,
        module: str,
        thread: threading.Thread,
        *,
        name: str = "",
        expected: bool = True,
    ) -> WorkerRecord:
        """Register *thread* as a worker of *module* and return its record."""
        if not isinstance(thread, threading.Thread):
            raise TypeError(
                f"module {module!r} registered {thread!r}, which is not a thread"
            )
        label = str(name).strip() or thread.name
        record = WorkerRecord(
            module=str(module), name=label, thread=thread, expected=bool(expected)
        )
        with self._lock:
            self._records.append(record)
        if self._logger is not None:
            self._logger.debug("worker watched", module=module, worker=label)
        return record

    def unwatch(self, module: str, name: str) -> bool:
        """Stop watching one worker; return whether it was registered."""
        with self._lock:
            for index, record in enumerate(self._records):
                if record.module == str(module) and record.name == str(name):
                    del self._records[index]
                    return True
        return False

    def records(self, module: str | None = None) -> list[WorkerRecord]:
        """Return the watched workers, optionally only one module's."""
        with self._lock:
            if module is None:
                return list(self._records)
            return [record for record in self._records if record.module == str(module)]

    def check(self) -> list[WorkerRecord]:
        """Return the workers that died since the previous pass, once each."""
        with self._lock:
            self._checks += 1
            if not self._enabled:
                return []
            found: list[WorkerRecord] = []
            for record in self._records:
                if not record.expected or record.reported or record.alive:
                    continue
                record.reported = True
                record.died_at = time.time()
                found.append(record)
            self._losses += len(found)
        for record in found:
            if self._logger is not None:
                self._logger.error(
                    f"worker {record.module}/{record.name} stopped unexpectedly",
                    module=record.module,
                    worker=record.name,
                )
        return found

    def stats(self) -> dict[str, Any]:
        """Return counters describing supervision activity."""
        with self._lock:
            alive = sum(1 for record in self._records if record.alive)
            dead = sum(
                1 for record in self._records if record.expected and not record.alive
            )
            return {
                "enabled": self._enabled,
                "watched": len(self._records),
                "alive": alive,
                "dead": dead,
                "checks": self._checks,
                "losses": self._losses,
            }

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view including the per-worker detail."""
        stats = self.stats()
        stats["workers"] = [record.as_dict() for record in self.records()]
        return stats

    def describe(self) -> str:
        """Return the one-line status shown by 'kdx doctor'."""
        stats = self.stats()
        return (
            f"watched={stats['watched']} alive={stats['alive']} "
            f"dead={stats['dead']} checks={stats['checks']}"
        )

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

    def __repr__(self) -> str:
        return f"WorkerSupervisor(watched={len(self)}, losses={self._losses})"
