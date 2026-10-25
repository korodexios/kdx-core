"""Named lock registry: hold() inspects and reserves in one guarded step (v0.2.2)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from korodexios_core.exceptions import LockTimeoutError
from korodexios_core.kernel.locks import NamedLockRegistry


def make_registry(tmp_path: Path) -> NamedLockRegistry:
    """Return a registry rooted in a throwaway locks directory."""
    return NamedLockRegistry(tmp_path / "state" / "locks", poll_interval=0.01)


def test_hold_reserves_the_name_before_the_block_runs(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    manager = registry.hold("index")
    reserved = registry.get("index")
    assert reserved is not None
    assert reserved.held is False
    assert registry.names() == ("index",)
    with manager as handle:
        assert handle.held is True
    assert registry.names() == ()
    assert registry.get("index") is None


def test_a_second_hold_on_a_held_name_fails_without_waiting(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    with registry.hold("index", timeout=None):
        started = time.monotonic()
        with pytest.raises(LockTimeoutError) as captured:
            registry.hold("index", timeout=None)
        waited = time.monotonic() - started
    assert waited < 0.5, "a same-name claim fails at once, never after a wait"
    assert captured.value.name == "index"
    assert "already held by this process" in str(captured.value)


def test_concurrent_holds_reserve_exactly_one_winner(tmp_path: Path) -> None:
    """Four threads claim the same name at once; only the reservation can win."""
    registry = make_registry(tmp_path)
    start = threading.Barrier(4)
    winners: list[str] = []
    losers: list[str] = []
    lock = threading.Lock()

    def claim(worker: str) -> None:
        start.wait()
        try:
            with registry.hold("index", timeout=0.0):
                with lock:
                    winners.append(worker)
                time.sleep(0.01)
        except LockTimeoutError:
            with lock:
                losers.append(worker)

    threads = [
        threading.Thread(target=claim, name=f"claimer-{index}", args=(f"claimer-{index}",))
        for index in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert [thread.is_alive() for thread in threads] == [False, False, False, False]
    assert len(winners) == 1, (winners, losers)
    assert len(losers) == 3, (winners, losers)
    assert registry.names() == ()


def test_a_failed_claim_leaves_no_reservation_behind(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    holder = registry.acquire("index")
    try:
        with pytest.raises(LockTimeoutError), registry.hold("index", timeout=0.0):
            pass
        assert registry.names() == ("index",)  # only the real holder is listed
    finally:
        registry.release("index")
    assert holder.held is False
    assert registry.names() == ()
