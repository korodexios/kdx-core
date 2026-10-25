"""A managed asyncio loop the chassis owns, so modules never roll their own.

Async work belongs to the process, not to one plugin: a module that spawns its
own loop thread has to solve startup ordering, shutdown joins, and exception
reporting alone. The chassis runs one loop on one daemon thread and hands it
out, so submitting a coroutine is a method call and stop() drains the loop
inside the lifecycle stop budget.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from typing import Any

from ..exceptions import ChassisError

__all__ = ["DEFAULT_SHUTDOWN_TIMEOUT", "AsyncRuntime"]

DEFAULT_SHUTDOWN_TIMEOUT = 2.0


class AsyncRuntime:
    """One asyncio event loop on a dedicated thread, owned by the chassis."""

    def __init__(
        self,
        *,
        name: str = "kdx-async",
        logger: Any = None,
        shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT,
    ) -> None:
        self._name = str(name)
        self._logger = logger
        self._shutdown_timeout = max(0.05, float(shutdown_timeout))
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._submitted = 0
        self._completed = 0
        self._failed = 0
        self._pending = 0
        self._started_at = 0.0
        self._last_error = ""

    def start(self) -> AsyncRuntime:
        """Create the loop and start serving it on a daemon thread."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            loop = asyncio.new_event_loop()
            self._loop = loop
            self._started_at = time.time()
            self._thread = threading.Thread(target=self._serve, name=self._name, daemon=True)
            self._thread.start()
        if self._logger is not None:
            self._logger.debug("async loop started", thread=self._name)
        return self

    def _serve(self) -> None:
        """Run the loop until stop() asks it to exit."""
        loop = self._loop
        if loop is None:  # pragma: no cover - start() always sets the loop
            return
        asyncio.set_event_loop(loop)
        loop.run_forever()

    def stop(self, timeout: float | None = None) -> None:
        """Cancel pending work, stop the loop, join the thread, close the loop."""
        with self._lock:
            loop = self._loop
            thread = self._thread
            self._loop = None
            self._thread = None
        if loop is None:
            return
        limit = self._shutdown_timeout if timeout is None else max(0.0, float(timeout))
        if thread is not None and thread.is_alive() and loop.is_running():
            try:
                drained = asyncio.run_coroutine_threadsafe(self._drain(), loop)
                drained.result(timeout=limit + 0.5)
            except Exception as exc:  # a stuck task must not hang the shutdown
                self._last_error = f"{type(exc).__name__}: {exc}"
                if self._logger is not None:
                    self._logger.warning(
                        "async loop shutdown timed out",
                        seconds=round(limit, 3),
                        error=self._last_error,
                    )
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.05, limit))
        if not loop.is_closed():
            loop.close()
        if self._logger is not None:
            self._logger.debug(
                "async loop stopped",
                pending=self._pending,
                failed=self._failed,
                thread_joined=thread is None or not thread.is_alive(),
            )

    @staticmethod
    async def _drain() -> None:
        """Cancel and await everything still scheduled on the loop.

        The drain task itself is excluded: cancelling itself would recurse
        through its own child tasks and never finish.
        """
        current = asyncio.current_task()
        pending = [
            task
            for task in asyncio.all_tasks()
            if not task.done() and task is not current
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    @property
    def running(self) -> bool:
        """Return True while the loop thread is serving."""
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        """Return the live loop, or None when the runtime is stopped."""
        return self._loop

    def submit(self, coro: Any, *, name: str | None = None) -> Any:
        """Schedule *coro* on the chassis loop; return a concurrent Future."""
        loop = self._loop
        if loop is None or not self.running:
            raise ChassisError(
                "the chassis asyncio loop is not running",
                hint="Enable core.async.enabled (or kdx run --asyncio) before "
                "submitting coroutines.",
            )
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        label = str(name) if name else getattr(coro, "__qualname__", "coroutine")
        with self._lock:
            self._submitted += 1
            self._pending += 1

        def _tally(done: Any) -> None:
            with self._lock:
                self._pending = max(0, self._pending - 1)
                if future.cancelled():
                    self._failed += 1
                    return
                if future.exception() is not None:
                    self._failed += 1
                    self._last_error = str(future.exception())
                else:
                    self._completed += 1
            if self._logger is not None and future.cancelled() is False:
                error = future.exception()
                if error is not None:
                    self._logger.error(
                        "async task failed", task=label, error=str(error)
                    )

        future.add_done_callback(_tally)
        return future

    def run(self, coro: Any, timeout: float | None = None) -> Any:
        """Submit *coro* and block for its result (the module-friendly spelling)."""
        future = self.submit(coro)
        try:
            return future.result(timeout)
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            raise

    def call_soon(self, callback: Callable[..., None], *args: Any) -> None:
        """Schedule *callback* on the loop thread without awaiting a result."""
        loop = self._loop
        if loop is None or not self.running:
            raise ChassisError(
                "the chassis asyncio loop is not running",
                hint="Enable core.async.enabled before scheduling callbacks.",
            )
        loop.call_soon_threadsafe(callback, *args)

    def stats(self) -> dict[str, Any]:
        """Return counters describing loop activity."""
        with self._lock:
            return {
                "running": self.running,
                "submitted": self._submitted,
                "completed": self._completed,
                "failed": self._failed,
                "pending": self._pending,
                "thread": self._name,
                "uptime_seconds": round(max(0.0, time.time() - self._started_at), 3)
                if self._started_at
                else 0.0,
                "last_error": self._last_error or None,
            }

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view for 'kdx doctor'."""
        return self.stats()

    def describe(self) -> str:
        """Return the one-line status shown by 'kdx doctor'."""
        stats = self.stats()
        state = "running" if stats["running"] else "stopped"
        return (
            f"{state} on {stats['thread']} "
            f"(submitted={stats['submitted']} completed={stats['completed']} "
            f"failed={stats['failed']})"
        )

    def __repr__(self) -> str:
        return f"AsyncRuntime(thread={self._name!r}, running={self.running})"
