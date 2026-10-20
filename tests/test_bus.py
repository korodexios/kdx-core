"""Event bus behaviour: matching, isolation, history, and waiting."""

from __future__ import annotations

import threading
import time

from korodexios_core.events.bus import Event, EventBus, matches, pattern_help


def test_publish_reaches_matching_subscribers() -> None:
    bus = EventBus()
    seen: list[str] = []
    bus.subscribe("core.tick", lambda event: seen.append(event.name))
    bus.subscribe("core.*", lambda event: seen.append(f"wild:{event.name}"))
    delivered = bus.publish("core.tick", {"tick": 1})
    assert delivered == 2
    assert seen == ["core.tick", "wild:core.tick"]


def test_wildcard_depths() -> None:
    assert matches("core.*", "core.tick")
    assert not matches("core.*", "core.module.started")
    assert matches("core.**", "core.module.started")
    assert matches("*", "core.tick")
    assert not matches("core.*", "core")
    assert "patterns" in pattern_help()


def test_once_subscription_fires_only_once() -> None:
    bus = EventBus()
    hits: list[int] = []
    bus.subscribe("job.done", lambda event: hits.append(1), once=True)
    bus.publish("job.done")
    bus.publish("job.done")
    assert hits == [1]
    assert len(bus) == 0


def test_handler_errors_are_isolated() -> None:
    bus = EventBus()

    def _boom(event: object) -> None:
        raise RuntimeError("handler exploded")

    seen: list[str] = []
    bus.subscribe("a", _boom)
    bus.subscribe("a", lambda event: seen.append(event.name))  # type: ignore[attr-defined]
    delivered = bus.publish("a")
    assert delivered == 1
    assert seen == ["a"]
    assert bus.stats()["errors"] == 1


def test_history_and_stats() -> None:
    bus = EventBus(history_limit=4)
    for index in range(6):
        bus.publish("core.tick", {"tick": index})
    history = bus.history()
    assert len(history) == 4
    assert history[-1].payload == {"tick": 5}
    assert bus.stats()["published"] == 6


def test_wait_for_returns_the_next_event() -> None:
    bus = EventBus()

    def _publisher() -> None:
        time.sleep(0.02)
        bus.publish("core.stopped", {"code": 0})

    worker = threading.Thread(target=_publisher)
    worker.start()
    event = bus.wait_for("core.stopped", timeout=2.0)
    worker.join(timeout=2.0)
    assert event is not None
    assert event.payload == {"code": 0}


def test_wait_for_times_out_without_events() -> None:
    bus = EventBus()
    assert bus.wait_for("never.happens", timeout=0.05) is None


def test_clear_drops_history_and_subscriptions() -> None:
    bus = EventBus()
    bus.subscribe("a", lambda event: None)
    bus.publish("a")
    bus.clear()
    assert bus.history() == []
    assert len(bus) == 0


def test_event_recursion_is_bounded() -> None:
    bus = EventBus(max_depth=3)

    def loop(event: Event) -> None:
        bus.publish(event.name, source="loop")

    bus.subscribe("tick", loop)
    bus.publish("tick")
    stats = bus.stats()
    assert stats["max_depth"] >= 4
    assert stats["errors"] >= 1


def test_slow_handler_is_reported() -> None:
    import time

    bus = EventBus(slow_handler_seconds=0.01)

    def slow(event: Event) -> None:
        time.sleep(0.05)

    bus.subscribe("tick", slow)
    assert bus.publish("tick") == 1
    assert bus.stats()["slow_handlers"] == 1


def test_concurrent_publishing_keeps_counters_exact() -> None:
    import threading

    bus = EventBus()
    bus.subscribe("work", lambda event: None)

    def worker() -> None:
        for index in range(200):
            bus.publish("work", {"index": index})

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    stats = bus.stats()
    assert stats["published"] == 1600
    assert stats["delivered"] == 1600
    assert stats["errors"] == 0
