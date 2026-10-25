"""Heartbeat: a background ticker proving lifecycle, events, and clean shutdown."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from ...cli.render import print_key_values, write_line
from ...constants import TAG_NOTE, TAG_OK
from ...kernel.cli_contract import CliArgument, CliCommand, CliRequest, CliSurface
from ...kernel.context import CoreContext
from ...kernel.contract import Module

DEFAULT_INTERVAL = 2.0
MIN_INTERVAL = 0.05
HEARTBEAT_EVENT = "core.heartbeat"


class HeartbeatModule(Module):
    """Publishes 'core.heartbeat' events on a daemon thread until stop() runs."""

    name = "heartbeat"
    description = (
        "Publishes a periodic heartbeat event, proving background threads and clean shutdown."
    )
    version = "0.1.0"

    def __init__(self) -> None:
        super().__init__()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ticks = 0
        self._last_beat: float | None = None
        self._interval = DEFAULT_INTERVAL

    def init(self, context: CoreContext) -> None:
        """Read the configured interval from the module's scoped configuration."""
        super().init(context)
        self._interval = max(MIN_INTERVAL, self.config.get_float("interval", DEFAULT_INTERVAL))
        self.logger.debug("heartbeat configured", interval_seconds=round(self._interval, 3))

    def register_cli(self, surface: CliSurface) -> None:
        """Contribute the 'heartbeat status' command to the root surface."""
        surface.add_command(
            CliCommand(
                name="heartbeat",
                help="Heartbeat module utilities.",
                module_name=self.name,
                subcommands=(
                    CliCommand(
                        name="status",
                        help="Print heartbeat counters and the configured interval.",
                        module_name=self.name,
                        handler=_status_command,
                        args=(
                            CliArgument(
                                ("--json",),
                                dest="as_json",
                                action="store_true",
                                help="Emit machine-readable JSON.",
                            ),
                        ),
                    ),
                ),
            )
        )

    def start(self) -> None:
        """Launch the ticker thread."""
        super().start()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._beat_loop, name="kdx-heartbeat", daemon=True
        )
        self._thread.start()
        self.logger.info("heartbeat thread started", interval_seconds=round(self._interval, 3))

    def stop(self) -> None:
        """Stop the ticker thread and report how many beats were published."""
        self._stop.set()
        worker = self._thread
        self._thread = None
        if worker is not None and worker.is_alive():
            worker.join(timeout=max(0.5, self._interval + 0.5))
        super().stop()
        self.logger.info("heartbeat thread stopped", ticks=self._ticks)

    def workers(self) -> dict[str, threading.Thread]:
        """Hand the ticker thread to chassis supervision (the optional v2 hook)."""
        return {"heartbeat": self._thread} if self._thread is not None else {}

    def health(self) -> dict[str, Any]:
        """Report counters alongside the standard lifecycle health block."""
        data = super().health()
        data.update(
            {
                "interval_seconds": round(self._interval, 3),
                "ticks": self._ticks,
                "last_beat": (round(time.time() - self._last_beat, 3) if self._last_beat else None),
                "thread_alive": bool(self._thread and self._thread.is_alive()),
            }
        )
        return data

    @property
    def ticks(self) -> int:
        """Return how many heartbeat events were published."""
        return self._ticks

    def _beat_loop(self) -> None:
        while not self._stop.wait(self._interval):
            self._ticks += 1
            self._last_beat = time.time()
            self.bus.publish(
                HEARTBEAT_EVENT,
                {"tick": self._ticks, "interval": round(self._interval, 3)},
                source=self.name,
            )


def _status_command(request: CliRequest) -> int:
    """Render heartbeat counters from the registered module instance."""
    registry = request.context.registry
    record = registry.get(HeartbeatModule.name) if registry is not None else None
    if record is None:
        write_line(f"{TAG_NOTE} heartbeat module is not registered")
        return 0
    health = (
        record.module.health()
        if hasattr(record.module, "health")
        else {"state": record.state.value}
    )
    if request.flag("as_json"):
        write_line(json.dumps(health, sort_keys=True, default=str))
        return 0
    print_key_values(
        [
            ("state", health.get("state", record.state.value)),
            ("interval seconds", health.get("interval_seconds", "-")),
            ("ticks", health.get("ticks", 0)),
            ("seconds since last beat", health.get("last_beat", "-")),
            ("thread alive", health.get("thread_alive", False)),
        ]
    )
    write_line(f"{TAG_OK} heartbeat {record.spec.version} is {record.state.value}")
    return 0


MODULE = HeartbeatModule()
