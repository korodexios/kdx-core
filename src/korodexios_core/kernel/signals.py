"""Signal handling: one clean abort path for SIGINT, SIGTERM, and SIGBREAK."""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable
from types import FrameType
from typing import Final

from ..constants import TAG_NOTE
from ..logging.engine import KorodexiosLogger

Handler = Callable[[int], None]

# Windows delivers SIGBREAK for the Ctrl+Break that POSIX never sends, and the documented
# gate says that abort exits 130 like SIGINT. Recording the raw number would make
# '128 + signum' answer 149, so the coordinator canonicalises it at the one place a
# number turns into an exit code. POSIX has no SIGBREAK, where the mapping is a no-op.
def _signal_number(name: str) -> int | None:
    """Return this platform's number for *name*, or None where the signal is absent."""
    number = getattr(signal, name, None)
    return None if number is None else int(number)


_SIGBREAK_NUMBER: Final[int | None] = _signal_number("SIGBREAK")


def _canonical_signal(signum: int) -> int:
    """Return the signal number the shutdown gate answers with.

    The received signal stays visible in the log; only the exit-code rule is shared, so a
    Windows operator and a POSIX operator both see 130 after an interrupt.
    """
    if _SIGBREAK_NUMBER is not None and int(signum) == _SIGBREAK_NUMBER:
        return int(signal.SIGINT)
    return int(signum)

_EXTRA_SIGNALS: tuple[str, ...] = ("SIGINT", "SIGTERM", "SIGBREAK")
_RELOAD_SIGNALS: tuple[str, ...] = ("SIGHUP",)


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
        logger: KorodexiosLogger,
        *,
        signals: tuple[str, ...] = _EXTRA_SIGNALS,
        reload_signals: tuple[str, ...] = _RELOAD_SIGNALS,
        reload_enabled: bool = True,
    ) -> None:
        self._logger = logger
        self._requested_names = tuple(signals)
        self._requested_reload_names = tuple(reload_signals) if reload_enabled else ()
        self._reload_enabled = bool(reload_enabled)
        self._previous: dict[int, object] = {}
        self._installed: tuple[int, ...] = ()
        self._installed_reload: tuple[int, ...] = ()
        self._event = threading.Event()
        self._reload_event = threading.Event()
        self._lock = threading.RLock()
        self._signal_number: int | None = None
        self._count = 0
        self._reload_count = 0
        self._clean_stop = False

    def install(self) -> tuple[int, ...]:
        """Install handlers, returning the signal numbers actually handled.

        Shutdown signals stop the daemon; reload signals (SIGHUP on POSIX) only
        ask it to re-read its configuration. A platform without SIGHUP, such as
        Windows, simply skips it.
        """
        installed: list[int] = []
        installed_reload: list[int] = []
        skipped: list[str] = []
        for name, builder, bucket in (
            *((name, self._build_handler, installed) for name in self._requested_names),
            *(
                (name, self._build_reload_handler, installed_reload)
                for name in self._requested_reload_names
            ),
        ):
            signum = getattr(signal, name, None)
            if signum is None:
                skipped.append(name)
                continue
            handler = builder(int(signum))
            try:
                self._previous[int(signum)] = signal.signal(int(signum), handler)
            except (ValueError, OSError, RuntimeError):
                # Not the main thread (embedded use): degrade instead of crashing.
                skipped.append(name)
                continue
            bucket.append(int(signum))
        self._installed = tuple(installed)
        self._installed_reload = tuple(installed_reload)
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
            self._installed_reload = ()

    def _build_handler(self, signum: int) -> Callable[[int, FrameType | None], None]:
        def _handler(number: int, _frame: FrameType | None) -> None:
            self.record(int(number) or signum)

        return _handler

    def _build_reload_handler(
        self, signum: int
    ) -> Callable[[int, FrameType | None], None]:
        def _handler(number: int, _frame: FrameType | None) -> None:
            self.record_reload(int(number) or signum)

        return _handler

    def record_reload(self, signum: int | None = None) -> None:
        """Record a reload request; the daemon keeps running."""
        with self._lock:
            self._reload_count += 1
            count = self._reload_count
        name = signal_name(int(signum)) if signum is not None else "RELOAD"
        self._logger.info(
            f"{TAG_NOTE} configuration reload requested by {name}",
            signal=name,
            reloads=count,
        )
        self._reload_event.set()

    @property
    def reload_requested(self) -> bool:
        """Return True when a reload was requested and not yet served."""
        return self._reload_event.is_set()

    @property
    def reload_count(self) -> int:
        """Return how many reload requests arrived."""
        return self._reload_count

    @property
    def reload_event(self) -> threading.Event:
        """Return the event a reload request sets."""
        return self._reload_event

    @property
    def reload_enabled(self) -> bool:
        """Return True when SIGHUP (or an IPC reload) can be served."""
        return self._reload_enabled

    def request_reload(self) -> None:
        """Ask for a configuration reload, as the IPC route and tests do."""
        self.record_reload(None)

    def clear_reload(self) -> None:
        """Forget a served reload request so the next one is visible again."""
        self._reload_event.clear()

    def record(self, signum: int) -> None:
        """Record a signal, logging only the first arrival."""
        with self._lock:
            self._count += 1
            first = self._signal_number is None
            if first:
                self._signal_number = _canonical_signal(int(signum))
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

    def request_clean_stop(self) -> None:
        """Ask for a graceful stop that is not a signal (IPC shutdown, --duration)."""
        with self._lock:
            self._count += 1
            self._clean_stop = True
        self._event.set()

    def reset(self) -> None:
        """Clear shutdown state so the chassis can be reused in-process."""
        with self._lock:
            self._signal_number = None
            self._count = 0
            self._reload_count = 0
            self._clean_stop = False
        self._event.clear()
        self._reload_event.clear()

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
        """Return the one-line status shown by 'kdx doctor'."""
        installed = ", ".join(signal_name(num) for num in self._installed) or "(none)"
        state = signal_name(self._signal_number) if self._signal_number is not None else "none"
        reloads = f"; reloads={self._reload_count}" if self._reload_enabled else ""
        return f"handlers={installed}; received={state}; count={self._count}{reloads}"

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-friendly view of the signal state."""
        return {
            "installed": [signal_name(num) for num in self._installed],
            "received": signal_name(self._signal_number)
            if self._signal_number is not None
            else None,
            "count": self._count,
            "shutdown_requested": self._event.is_set(),
            "clean_stop": self._clean_stop,
            "reload_enabled": self._reload_enabled,
            "reload_count": self._reload_count,
            "reload_requested": self._reload_event.is_set(),
            "reload_signals": [signal_name(num) for num in self._installed_reload],
        }

    def __repr__(self) -> str:
        return (
            f"SignalCoordinator(installed={self._installed}, "
            f"received={self._signal_number!r}, count={self._count}, "
            f"reloads={self._reload_count})"
        )
