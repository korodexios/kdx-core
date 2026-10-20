"""In-process event bus used by modules to talk without importing each other."""

from __future__ import annotations

from .bus import Event, EventBus, Subscription

__all__ = ["Event", "EventBus", "Subscription"]
