"""Signal handling: one clean abort path for SIGINT, SIGTERM, and SIGBREAK."""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable
from types import FrameType

from ..constants import TAG_NOTE
from ..logging.engine import KorodexLogger

Handler = Callable[[int], None]

_EXTRA_SIGNALS: tuple[str, ...] = ("SIGINT", "SIGTERM", "SIGBREAK")


def signal_name(signum: int) -> str:
    """Return a readable signal name, falling back to the number."""
    try:
        return signal.Signals(signum).name
    except (ValueError, AttributeError):  # pragma: no cover - exotic platforms
        return f"SIGNAL_{signum}"


class SignalCoordinator:
    """Owns process signals so shutdown happens once, cleanly, and traceably."""

    def __init__(
        self,
        logger: KorodexLogger,
        *,
        signals: tuple[str, ...] = _EXTRA_SIGNALS,
    ) -> None:
        self._logger = logger
        self._requested_names = tuple(signals)
        self._previous: dict[int, object] = {}
        self._installed: tuple[int, ...] = ()
        self._event = threading.Event()
        self._lock = threading.RLock()
        self._signal_number: int | None = None
        self._count = 0

    def install(self) -> tuple[int, ...]:
        """Install handlers, returning the signal numbers actually handled."""
        installed: list[int] = []
        skipped: list[str] = []
        for name in self._requested_names:
            signum = getattr(signal, name, None)
            if signum is None:
                skipped.append(name)
                continue
            handler = self._build_handler(int(signum))
            try:
                self._previous[int(signum)] = signal.signal(int(signum), handler)
            except (ValueError, OSError, RuntimeError):
                # Not the main thread (embedded use): degrade instead of crashing.
                skipped.append(name)
                continue
            installed.append(int(signum))
        self._installed = tuple(installed)
        if skipped:
            self._logger.debug(
                "signal handlers partially installed",
                installed=", ".join(signal_name(num) for num in installed) or "(none)",
                skipped=", ".join(skipped) or "(none)",
            )
        return self._installed

    def uninstall(self) -> None:
        """Restore the handlers that were live before install()."""
        with self._lock:
            for signum, previous in list(self._previous.items()):
                try:
                    signal.signal(signum, previous)  # type: ignore[arg-type]
                except (ValueError, OSError, RuntimeError, TypeError):
                    continue
            self._previous.clear()
            self._installed = ()

    def _build_handler(self, signum: int) -> Callable[[int, FrameType | None], None]:
        def _handler(number: int, _frame: FrameType | None) -> None:
            self.record(int(number) or signum)

        return _handler

    def record(self, signum: int) -> None:
        """Record a signal, logging only the first arrival."""
        with self._lock:
            self._count += 1
            first = self._signal_number is None
            if first:
                self._signal_number = int(signum)
            count = self._count
        name = signal_name(int(signum))
        if first:
            self._logger.info(f"{TAG_NOTE} abort requested by {name}", signal=name)
        else:
            self._logger.debug(
                "repeat signal ignored; shutdown already running",
                signal=name,
                count=count,
            )
        self._event.set()

    def request_shutdown(self, signum: int | None = None) -> None:
        """Trigger the shutdown path programmatically (tests, CLI flags)."""
        self.record(int(signum) if signum is not None else signal.SIGINT)

    def reset(self) -> None:
        """Clear shutdown state so the chassis can be reused in-process."""
        with self._lock:
            self._signal_number = None
            self._count = 0
        self._event.clear()

    @property
    def shutdown_requested(self) -> bool:
        """Return True once any handled signal arrived."""
        return self._event.is_set()

    @property
    def signal_number(self) -> int | None:
        """Return the first signal number seen, or None."""
        return self._signal_number

    @property
    def signal_count(self) -> int:
        """Return how many signals arrived."""
        return self._count

    @property
    def event(self) -> threading.Event:
        """Return the shutdown gate for cooperative waits."""
        return self._event

    @property
    def installed(self) -> tuple[int, ...]:
        """Return the signal numbers currently handled by the coordinator."""
        return self._installed

    def wait(self, timeout: float | None = None) -> bool:
        """Wait up to *timeout* seconds for a shutdown request."""
        return self._event.wait(timeout)

    def describe(self) -> str:
        """Return the one-line status shown by 'korodex doctor'."""
        installed = ", ".join(signal_name(num) for num in self._installed) or "(none)"
        state = signal_name(self._signal_number) if self._signal_number is not None else "none"
        return f"handlers={installed}; received={state}; count={self._count}"

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-friendly view of the signal state."""
        return {
            "installed": [signal_name(num) for num in self._installed],
            "received": signal_name(self._signal_number)
            if self._signal_number is not None
            else None,
            "count": self._count,
            "shutdown_requested": self._event.is_set(),
        }

    def __repr__(self) -> str:
        return (
            f"SignalCoordinator(installed={self._installed}, "
            f"received={self._signal_number!r}, count={self._count})"
        )
