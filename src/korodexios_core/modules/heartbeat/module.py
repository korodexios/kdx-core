"""Heartbeat: a periodic ticker proving the chassis clock, events, and clean shutdown."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from ...cli.render import print_key_values, write_line
from ...config.schema import ConfigField, ConfigKind, ConfigSchema
from ...constants import TAG_NOTE, TAG_OK
from ...kernel.cli_contract import CliArgument, CliCommand, CliRequest, CliSurface
from ...kernel.context import CoreContext
from ...kernel.contract import Module, ModuleState
from ...kernel.diagnostics import DiagnosticCheck, DiagnosticSeverity
from ...kernel.scheduler import ScheduledTask

DEFAULT_INTERVAL = 2.0
MIN_INTERVAL = 0.05
HEARTBEAT_EVENT = "core.heartbeat"
TICKER_TASK = "ticker"


class HeartbeatModule(Module):
    """Publishes 'core.heartbeat' events on the chassis clock until stop() runs."""

    name = "heartbeat"
    description = (
        "Publishes a periodic heartbeat event, proving the chassis clock and clean "
        "shutdown."
    )
    version = "0.1.0"

    def __init__(self) -> None:
        super().__init__()
        self._task: ScheduledTask | None = None
        self._ticks = 0
        self._last_beat: float | None = None
        self._interval = DEFAULT_INTERVAL

    def init(self, context: CoreContext) -> None:
        """Read the configured interval from the module's scoped configuration."""
        super().init(context)
        self._interval = max(MIN_INTERVAL, self.config.get_float("interval", DEFAULT_INTERVAL))
        self.logger.debug("heartbeat configured", interval_seconds=round(self._interval, 3))

    def config_schema(self) -> ConfigSchema:
        """Declare the one scoped key this module reads."""
        return ConfigSchema(
            module=self.name,
            fields=(
                ConfigField(
                    "interval",
                    kind=ConfigKind.FLOAT,
                    default=DEFAULT_INTERVAL,
                    description=f"Seconds between beats; clamped to at least {MIN_INTERVAL}.",
                ),
            ),
        )

    def diagnostics(self) -> tuple[DiagnosticCheck, ...]:
        """Report whether the chassis clock still runs this module's ticker."""
        if self.state is not ModuleState.RUNNING:
            # 'kdx doctor' can ask before start() and after stop(); a ticker that
            # was never started is a fact, not a fault.
            return (
                DiagnosticCheck(
                    name="ticker",
                    message=f"heartbeat is {self.state.value}",
                    module=self.name,
                ),
            )
        task = self._task
        if task is None:
            return (
                DiagnosticCheck(
                    name="ticker",
                    message="heartbeat scheduled no ticker",
                    severity=DiagnosticSeverity.ERROR,
                    hint="start() found no chassis scheduler to declare the task on",
                    module=self.name,
                ),
            )
        if task.done or not task.enabled:
            return (
                DiagnosticCheck(
                    name="ticker",
                    message=f"heartbeat ticker is parked after {self._ticks} ticks",
                    severity=DiagnosticSeverity.WARNING,
                    hint="'core.scheduler.enabled: false' parks every task, not just this one",
                    module=self.name,
                ),
            )
        if task.last_error:
            return (
                DiagnosticCheck(
                    name="ticker",
                    message=f"heartbeat ticker failed: {task.last_error}",
                    severity=DiagnosticSeverity.WARNING,
                    hint="a beat raised; the traceback is in the log",
                    module=self.name,
                ),
            )
        return (
            DiagnosticCheck(
                name="ticker",
                message=f"heartbeat ticker alive after {self._ticks} ticks",
                module=self.name,
            ),
        )

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
        """Declare the ticker as a periodic task on the chassis clock."""
        super().start()
        context = self.context
        self._task = (
            None
            if context is None
            else context.schedule(
                self._interval, self._beat_tick, name=TICKER_TASK, module=self.name
            )
        )
        self.logger.info(
            "heartbeat ticker scheduled" if self._task is not None else "heartbeat ticker parked",
            interval_seconds=round(self._interval, 3),
        )

    def stop(self) -> None:
        """Withdraw the ticker and report how many beats were published."""
        task = self._task
        self._task = None
        scheduler = self.scheduler if self.context is not None else None
        if task is not None and scheduler is not None:
            scheduler.unschedule(task.name)
        super().stop()
        self.logger.info("heartbeat ticker stopped", ticks=self._ticks)

    def workers(self) -> dict[str, threading.Thread]:
        """Report no thread of its own: the chassis clock runs the ticker, so the
        supervisor has nothing to watch and shutdown has nothing to join."""
        return {}

    def health(self) -> dict[str, Any]:
        """Report counters alongside the standard lifecycle health block."""
        data = super().health()
        data.update(
            {
                "interval_seconds": round(self._interval, 3),
                "ticks": self._ticks,
                "last_beat": (round(time.time() - self._last_beat, 3) if self._last_beat else None),
                "ticker_scheduled": self._task is not None,
                "ticker_runs": 0 if self._task is None else self._task.runs,
                "ticker_errors": 0 if self._task is None else self._task.failures,
            }
        )
        return data

    @property
    def ticks(self) -> int:
        """Return how many heartbeat events were published."""
        return self._ticks

    def _beat_tick(self) -> None:
        """Publish one beat; the chassis clock calls this, not a thread of its own."""
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
            ("ticker scheduled", health.get("ticker_scheduled", False)),
            ("ticker runs", health.get("ticker_runs", 0)),
        ]
    )
    write_line(f"{TAG_OK} heartbeat {record.spec.version} is {record.state.value}")
    return 0


MODULE = HeartbeatModule()
