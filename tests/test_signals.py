"""SIGBREAK joins the SIGINT gate: an interrupt aborts with 130 on every platform."""

from __future__ import annotations

import signal

import pytest

from korodexios_core.constants import ExitCode
from korodexios_core.kernel import signals as signals_module
from korodexios_core.kernel.signals import (
    SignalCoordinator,
    _canonical_signal,
    signal_name,
)


class RecordingLogger:
    """The coordinator only logs, so a recorder is enough to watch what it says."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, dict[str, object]]] = []

    def _note(self, message: str, **fields: object) -> None:
        self.lines.append((message, fields))

    def info(self, message: str, **fields: object) -> None:
        self._note(message, **fields)

    def debug(self, message: str, **fields: object) -> None:
        self._note(message, **fields)

    def warning(self, message: str, **fields: object) -> None:
        self._note(message, **fields)


def test_platforms_without_sigbreak_keep_their_own_numbers() -> None:
    """Where the platform has no SIGBREAK, the mapping is a no-op, not a wrong number."""
    assert getattr(signal, "SIGBREAK", None) == signals_module._SIGBREAK_NUMBER
    assert _canonical_signal(int(signal.SIGINT)) == int(signal.SIGINT)
    assert _canonical_signal(int(signal.SIGTERM)) == int(signal.SIGTERM)


def test_sigbreak_is_canonical_to_sigint(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate shares one exit rule, so Ctrl+Break behaves like Ctrl+C."""
    monkeypatch.setattr(signals_module, "_SIGBREAK_NUMBER", int(signal.SIGUSR1))
    assert _canonical_signal(int(signal.SIGUSR1)) == int(signal.SIGINT)
    assert _canonical_signal(int(signal.SIGINT)) == int(signal.SIGINT)
    assert _canonical_signal(int(signal.SIGTERM)) == int(signal.SIGTERM)


def test_the_coordinator_records_the_gate_number(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recorded SIGBREAK leaves the SIGINT number, so 128 plus signum stays 130."""
    monkeypatch.setattr(signals_module, "_SIGBREAK_NUMBER", int(signal.SIGUSR1))
    logger = RecordingLogger()
    coordinator = SignalCoordinator(logger)  # type: ignore[arg-type]
    coordinator.reset()
    coordinator.record(int(signal.SIGUSR1))
    assert coordinator.signal_number == int(signal.SIGINT)
    assert ExitCode.from_signal(coordinator.signal_number or 0) == 130
    assert coordinator.signal_count == 1
    first = logger.lines[0]
    assert "abort requested by" in first[0], first[0]
    assert first[1]["signal"] == signal_name(int(signal.SIGUSR1))


def test_a_repeat_signal_does_not_move_the_exit_code() -> None:
    """The first signal decides; later ones are counted for the log only."""
    coordinator = SignalCoordinator(RecordingLogger())  # type: ignore[arg-type]
    coordinator.reset()
    coordinator.record(int(signal.SIGTERM))
    coordinator.record(int(signal.SIGINT))
    assert coordinator.signal_number == int(signal.SIGTERM)
    assert ExitCode.from_signal(coordinator.signal_number or 0) == int(
        ExitCode.from_signal(int(signal.SIGTERM))
    )
    assert coordinator.signal_count == 2
