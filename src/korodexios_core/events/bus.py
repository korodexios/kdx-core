"""Thread-safe in-process event bus with wildcard subscriptions and history."""

from __future__ import annotations

import fnmatch
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

EventHandler = Callable[["Event"], None]

_SEGMENT = "\x00seg\x00"
_ANY = "\x00any\x00"


@dataclass(frozen=True, slots=True)
class Event:
    """An immutable event delivered to subscribers."""

    name: str
    payload: Any = None
    source: str | None = None
    at: float = field(default_factory=time.time)
    sequence: int = 0

    def age(self, now: float | None = None) -> float:
        """Return the seconds elapsed since the event was published."""
        return max(0.0, (time.time() if now is None else now) - self.at)


@dataclass(slots=True)
class Subscription:
    """A live subscription that can be cancelled by the subscriber."""

    pattern: str
    handler: EventHandler
    name: str = ""
    once: bool = False
    active: bool = True

    def unsubscribe(self, bus: EventBus) -> bool:
        """Cancel this subscription on *bus*."""
        return bus.unsubscribe(self)


class EventBus:
    """Publish/subscribe hub; handler failures never break the publisher."""

    def __init__(self, *, history_limit: int = 256, logger: Any = None) -> None:
        self._lock = threading.RLock()
        self._subscriptions: list[Subscription] = []
        self._history: deque[Event] = deque(maxlen=max(0, int(history_limit)))
        self._sequence = 0
        self._published = 0
        self._delivered = 0
        self._errors = 0
        self._logger = logger

    def subscribe(
        self,
        pattern: str,
        handler: EventHandler,
        *,
        name: str | None = None,
        once: bool = False,
    ) -> Subscription:
        """Register *handler* for *pattern*; '*' matches one segment, '**' any."""
        if not str(pattern).strip():
            raise ValueError("event pattern must not be empty")
        if not callable(handler):
            raise TypeError("event handler must be callable")
        subscription = Subscription(
            pattern=str(pattern).strip("."),
            handler=handler,
            name=str(name) if name else _default_handler_name(handler),
            once=bool(once),
        )
        with self._lock:
            self._subscriptions.append(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> bool:
        """Remove *subscription* if present; return whether it was live."""
        with self._lock:
            for index, existing in enumerate(self._subscriptions):
                if existing is subscription:
                    existing.active = False
                    del self._subscriptions[index]
                    return True
        return False

    def publish(
        self,
        name: str,
        payload: Any = None,
        *,
        source: str | None = None,
    ) -> int:
        """Publish an event and return how many handlers ran successfully."""
        clean = str(name).strip(".")
        if not clean:
            raise ValueError("event name must not be empty")
        with self._lock:
            self._sequence += 1
            event = Event(
                name=clean,
                payload=dict(payload) if isinstance(payload, Mapping) else payload,
                source=source,
                at=time.time(),
                sequence=self._sequence,
            )
            self._history.append(event)
            self._published += 1
            matches = [sub for sub in self._subscriptions if _matches(sub.pattern, clean)]
            if any(sub.once for sub in matches):
                for sub in matches:
                    if sub.once:
                        self._subscriptions.remove(sub)
                        sub.active = False
        delivered = 0
        for subscription in matches:
            try:
                subscription.handler(event)
                delivered += 1
            except Exception as exc:
                self._errors += 1
                if self._logger is not None:
                    self._logger.warning(
                        "event handler failed",
                        event=clean,
                        handler=subscription.name,
                        error=str(exc),
                    )
        self._delivered += delivered
        return delivered

    def wait_for(self, name: str, timeout: float | None = None) -> Event | None:
        """Block until an event matching *name* is published, or *timeout* elapses."""
        gate = threading.Event()
        box: list[Event] = []

        def _capture(event: Event) -> None:
            box.append(event)
            gate.set()

        subscription = self.subscribe(name, _capture, name="wait_for", once=True)
        if gate.wait(timeout):
            self.unsubscribe(subscription)
            return box[0] if box else None
        self.unsubscribe(subscription)
        return box[0] if box else None

    def history(self, limit: int | None = None) -> list[Event]:
        """Return recent events, oldest first."""
        with self._lock:
            events = list(self._history)
        return events[-int(limit) :] if limit else events

    def clear(self) -> None:
        """Drop history and subscriptions (used by tests and resets)."""
        with self._lock:
            self._history.clear()
            for subscription in self._subscriptions:
                subscription.active = False
            self._subscriptions.clear()

    def stats(self) -> dict[str, Any]:
        """Return counters describing bus activity."""
        with self._lock:
            return {
                "subscriptions": len(self._subscriptions),
                "published": self._published,
                "delivered": self._delivered,
                "errors": self._errors,
                "history": len(self._history),
                "sequence": self._sequence,
            }

    def __len__(self) -> int:
        with self._lock:
            return len(self._subscriptions)

    def __repr__(self) -> str:
        return f"EventBus(subscriptions={len(self)}, published={self._published})"


def _default_handler_name(handler: EventHandler) -> str:
    return getattr(handler, "__name__", type(handler).__name__)


def _compile(pattern: str) -> re.Pattern[str]:
    """Compile an event pattern into a regex, honouring '*' and '**' wildcards."""
    placeholder = pattern.replace("**", _ANY).replace("*", _SEGMENT)
    escaped = (
        re.escape(placeholder).replace(re.escape(_SEGMENT), "[^.]+").replace(re.escape(_ANY), ".+")
    )
    return re.compile(f"^{escaped}$")


def _matches(pattern: str, name: str) -> bool:
    if pattern == name or pattern == "*":
        return True
    if "*" not in pattern and "**" not in pattern:
        return False
    return bool(_compile(pattern).match(name))


def matches(pattern: str, name: str) -> bool:
    """Public helper: does subscription *pattern* catch event *name*?"""
    return _matches(pattern, name)


def pattern_help() -> str:
    """Return the one-line pattern cheat sheet shown in the CLI."""
    return "patterns: 'a.b' exact, 'a.*' one segment, 'a.**' any depth, '*' everything"


_ = fnmatch  # kept for readability of the import block
