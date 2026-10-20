"""The chassis: lifecycle coordinator wiring config, logging, bus, signals, modules."""

from __future__ import annotations

import os
import platform
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..config.layered import LayeredConfig
from ..constants import DOCTOR_SCHEMA_VERSION, PACKAGE_NAME, TAG_ERROR, TAG_NOTE, TAG_OK
from ..events.bus import EventBus
from ..exceptions import ChassisError
from ..logging.engine import KorodexiosLogger, build_logger, close_logger, ensure_utf8_streams
from ..paths import AppPaths
from ..version import CORE_API_VERSION, __version__, format_version
from .cli_contract import CliCommand, CliSurface
from .context import CoreContext
from .discovery import DiscoveryReport, ModuleDiscovery
from .ipc import (
    DISCOVERY_NAME,
    IpcError,
    IpcOptions,
    IpcServer,
    new_token,
    process_memory,
)
from .registry import ModuleRegistry
from .signals import SignalCoordinator, signal_name

TICK_EVENT = "core.tick"


class ChassisState(Enum):
    """Lifecycle states of the chassis process itself."""

    NEW = "new"
    PREPARED = "prepared"
    INITIALIZED = "initialized"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"


_CONFIG_WARNING_LIMIT = 4
"""Warnings logged at boot before the rest is folded into one summary line."""


def _advice(hint: str) -> str:
    """Return the parenthetical hint shown next to an unknown configuration key."""
    if not hint:
        return ""
    if hint.startswith("core."):
        return f" (did you mean {hint}?)"
    return f" (module {hint.partition('.')[2]} is discovered)"


@dataclass(slots=True)
class ChassisOptions:
    """Everything the chassis accepts from the caller before booting."""

    home: Path | None = None
    config_file: Path | None = None
    env_file: Path | None = None
    environ: Mapping[str, str] | None = None
    cli_overrides: dict[str, Any] = field(default_factory=dict)
    extra_module_dirs: Sequence[Path] = ()
    install_signals: bool = True
    utf8_streams: bool = True


@dataclass(slots=True)
class Chassis:
    """The headless core: owns config, logging, bus, signals, and the registry."""

    context: CoreContext
    registry: ModuleRegistry
    signals: SignalCoordinator
    logger: KorodexiosLogger
    config: LayeredConfig
    paths: AppPaths
    bus: EventBus
    discovery: DiscoveryReport
    state: ChassisState = ChassisState.NEW
    started_at: float = field(default_factory=time.time)
    ipc: IpcServer | None = None

    @classmethod
    def create(cls, options: ChassisOptions | None = None) -> Chassis:
        """Boot the chassis: paths, config, logging, bus, discovery, registration."""
        options = options or ChassisOptions()
        environ = options.environ if options.environ is not None else os.environ

        paths = AppPaths.discover(
            root=str(options.home) if options.home else None,
            environ=environ,
            config_file=str(options.config_file) if options.config_file else None,
            env_file=str(options.env_file) if options.env_file else None,
            extra_module_dirs=tuple(options.extra_module_dirs),
        )
        config = LayeredConfig.load(
            config_file=paths.config_file,
            local_config_file=paths.local_config_file,
            env_file=paths.env_file,
            environ=environ,
            cli_overrides=options.cli_overrides,
            runtime_defaults={"core.home": str(paths.root)},
        )
        paths = paths.with_log_dir(config.get("core.log.dir")).with_extra_module_dirs(
            [str(item) for item in config.get_list("core.modules.extra_paths")]
        )
        if config.get_bool("core.cli.interactive", True) and not stdin_is_tty():
            config = config.with_cli_overrides({"core.cli.interactive": False})

        _root_logger, factory = build_logger(config, paths)
        log = factory.get("chassis")
        if options.utf8_streams:
            log.debug("stream encodings ready", **ensure_utf8_streams())
        bus = EventBus(logger=log)
        context = CoreContext(
            config=config,
            paths=paths,
            logs=factory,
            bus=bus,
            app_name=config.get_str("core.name", "korodexios"),
        )
        registry = ModuleRegistry(log.child("registry"), bus=bus)
        context.registry = registry

        discovery = ModuleDiscovery(
            paths,
            config=config,
            enabled=config.get_list("core.modules.enabled"),
            disabled=config.get_list("core.modules.disabled"),
            autodiscover=config.get_bool("core.modules.autodiscover", True),
            logger=log.child("discovery"),
        ).scan()
        registry.register_discovered(discovery)
        unknown = config.unknown_keys(registry.names())
        for key, hint in unknown[:_CONFIG_WARNING_LIMIT]:
            log.warning(f"{TAG_NOTE} unknown configuration key {key}{_advice(hint)}")
        if len(unknown) > _CONFIG_WARNING_LIMIT:
            extra = ", ".join(key for key, _ in unknown[_CONFIG_WARNING_LIMIT:])
            log.warning(f"{TAG_NOTE} more unknown configuration keys: {extra}")

        signals = SignalCoordinator(log.child("signals"))
        if options.install_signals:
            signals.install()

        chassis = cls(
            context=context,
            registry=registry,
            signals=signals,
            logger=log,
            config=config,
            paths=paths,
            bus=bus,
            discovery=discovery,
            state=ChassisState.PREPARED,
        )
        context.chassis = chassis
        log.info(
            f"{TAG_OK} chassis prepared",
            modules=registry.health()["total"],
            ready=len(discovery.candidates),
            failed=len(discovery.failures),
            config_file=str(paths.config_file or "(defaults only)"),
        )
        for failure in discovery.failures:
            log.warning(f"{TAG_ERROR} {failure.describe()}")
        return chassis

    def ensure_initialized(self) -> Chassis:
        """Initialize module hooks once; a no-op when already initialized or running."""
        if self.state in (ChassisState.PREPARED, ChassisState.NEW):
            self.initialize()
        return self

    def initialize(self) -> Chassis:
        """Run module init hooks and mark the chassis initialized."""
        self._require_state(ChassisState.PREPARED, ChassisState.NEW)
        self.registry.initialize_all(self.context)
        self.state = ChassisState.FAILED if self._failed_modules() else ChassisState.INITIALIZED
        self.bus.publish("core.initialized", {"modules": len(self.registry)}, source="chassis")
        return self

    def start(self, *, ipc: bool | None = None) -> Chassis:
        """Run module start hooks, open the control plane, and enter the running state."""
        allowed = (ChassisState.INITIALIZED, ChassisState.PREPARED, ChassisState.FAILED)
        if self.state not in allowed:
            raise ChassisError(
                f"cannot start a chassis in state {self.state.value}",
                hint="Call initialize() before start().",
            )
        self.ensure_initialized()
        self.registry.start_all()
        self.state = ChassisState.RUNNING
        self.started_at = time.time()
        self.bus.publish(
            "core.started",
            {"modules": len(self.registry), "version": __version__},
            source="chassis",
        )
        self.start_ipc(enabled=ipc)
        self.logger.debug("chassis running", modules=len(self.registry))
        return self

    def start_ipc(self, *, enabled: bool | None = None) -> Chassis:
        """Open the loopback control plane; a busy port never breaks the boot."""
        if self.ipc is not None and self.ipc.running:
            return self
        allowed = (ChassisState.RUNNING, ChassisState.INITIALIZED, ChassisState.PREPARED)
        if self.state not in allowed:
            return self
        wanted = (
            self.config.get_bool("core.ipc.enabled", True) if enabled is None else bool(enabled)
        )
        if not wanted:
            self.ipc = None
            return self
        static_token = str(self.config.get("core.ipc.token", "") or "").strip()
        options = IpcOptions(
            host=self.config.get_str("core.ipc.host", "127.0.0.1"),
            port=int(self.config.get_int("core.ipc.port", 8123)),
            discovery_path=self.paths.state_dir / DISCOVERY_NAME,
            token=static_token or new_token(),
            token_source="static" if static_token else "ephemeral",
            auth=self.config.get_bool("core.ipc.auth", True),
            allow_anonymous_shutdown=self.config.get_bool(
                "core.ipc.allow_anonymous_shutdown", False
            ),
        )
        server = IpcServer(
            status_fn=self.health_report,
            shutdown_fn=self.signals.request_clean_stop,
            state_fn=lambda: self.state.value,
            options=options,
            logger=self.logger.child("ipc"),
        )
        try:
            server.start()
        except (OSError, IpcError) as exc:
            hint = getattr(exc, "hint", None)
            self.logger.warning(
                f"{TAG_ERROR} ipc control plane unavailable: {exc}",
                hint=hint or "set core.ipc.port to 0 for an ephemeral port",
            )
            return self
        self.ipc = server
        if server.lan_accessible:
            self.logger.info(
                f"{TAG_NOTE} control plane listening on {options.host} (LAN accessible)",
                auth=options.auth,
                token_source=options.token_source,
            )
        self.bus.publish(
            "core.ipc.started",
            {
                "host": options.host,
                "port": server.discovery.port if server.discovery else None,
                "auth": options.auth,
                "token_source": options.token_source,
                "lan_accessible": server.lan_accessible,
            },
            source="chassis",
        )
        return self

    def stop_ipc(self, timeout: float | None = None) -> Chassis:
        """Close the control plane and remove its discovery file."""
        server = self.ipc
        self.ipc = None
        if server is None:
            return self
        limit = (
            self.config.get_float("core.lifecycle.stop_timeout", 5.0)
            if timeout is None
            else timeout
        )
        server.stop(timeout=limit)
        self.bus.publish(
            "core.ipc.stopped", {"requests": server.as_dict()["requests"]}, source="chassis"
        )
        return self

    def run_forever(self, duration: float | None = None) -> int:
        """Tick until a signal arrives or *duration* seconds elapse."""
        if self.state is not ChassisState.RUNNING:
            raise ChassisError(
                "the chassis is not running",
                hint="Call start() before run_forever().",
            )
        interval = self.config.get_float("core.lifecycle.tick_interval", 1.0)
        deadline = (time.time() + duration) if duration else None
        ticks = 0
        while not self.signals.shutdown_requested:
            remaining = (deadline - time.time()) if deadline is not None else None
            if remaining is not None and remaining <= 0:
                break
            ticks += 1
            self.bus.publish(
                TICK_EVENT,
                {"tick": ticks, "uptime": round(self.context.uptime(), 3)},
                source="chassis",
            )
            wait_for = interval if remaining is None else min(interval, remaining)
            self.signals.wait(max(0.001, wait_for))
        self.logger.debug("tick loop finished", ticks=ticks)
        return int(self.exit_code)

    def run_once(self) -> int:
        """Publish a single tick; the frictionless path for CI and smoke tests."""
        if self.state is not ChassisState.RUNNING:
            raise ChassisError(
                "the chassis is not running",
                hint="Call start() before run_once().",
            )
        self.bus.publish(
            TICK_EVENT,
            {"tick": 1, "uptime": round(self.context.uptime(), 3)},
            source="chassis",
        )
        return int(self.exit_code)

    def shutdown(self) -> int:
        """Stop modules, restore signals, close log handlers; return the exit code."""
        code = int(self.exit_code)
        reason = (
            f"{TAG_NOTE} aborted by {signal_name(self.signals.signal_number)}"
            if self.signals.signal_number is not None
            else f"{TAG_OK} chassis stopped"
        )
        self.bus.publish(
            "core.stopping",
            {"signal": self.signals.signal_number, "exit_code": code},
            source="chassis",
        )
        timeout = self.config.get_float("core.lifecycle.stop_timeout", 5.0)
        self.stop_ipc(timeout=timeout)
        self.registry.stop_all(timeout=timeout)
        self.state = ChassisState.STOPPED
        self.bus.publish("core.stopped", {"exit_code": code}, source="chassis")
        self.logger.info(reason, exit_code=code)
        self.signals.uninstall()
        close_logger(self.context.logs.root_logger)
        return code

    def collect_cli(self, surface: CliSurface) -> list[CliCommand]:
        """Let modules inject their commands into *surface* (argparse, menus, docs)."""
        return self.registry.collect_cli(surface)

    @property
    def exit_code(self) -> Any:
        """Return the exit code the chassis would exit with right now."""
        from ..constants import ExitCode

        if self.signals.signal_number is not None:
            return ExitCode.from_signal(self.signals.signal_number)
        if self._failed_modules():
            return ExitCode.ERROR
        return ExitCode.SUCCESS

    def health_report(self) -> dict[str, Any]:
        """Return the inspection payload rendered by 'kdx doctor'."""
        return {
            "schema": DOCTOR_SCHEMA_VERSION,
            "app": {
                "name": self.context.app_name,
                "distribution": PACKAGE_NAME,
                "version": __version__,
                "core_api": CORE_API_VERSION,
                "state": self.state.value,
                "uptime_seconds": round(self.context.uptime(), 3),
            },
            "runtime": {
                "python": sys.version.split()[0],
                "executable": sys.executable or "(unknown)",
                "platform": platform.platform(),
                "pid": os.getpid(),
                "cwd": str(Path.cwd()),
                "argv": list(sys.argv),
                "encoding": {"stdout": _encoding("stdout"), "stderr": _encoding("stderr")},
            },
            "paths": self.paths.as_dict(),
            "config": {
                "file": str(self.config.config_file) if self.config.config_file else None,
                "env_file": str(self.config.env_file) if self.config.env_file else None,
                "prefix": self.config.prefix,
                "keys": len(self.config.keys()),
                "unknown": [
                    {"key": key, "suggestion": hint, "origin": self.config.origin_of(key)}
                    for key, hint in self.config.unknown_keys(self.registry.names())
                ],
                "project_keys": list(self.config.project_keys()),
            },
            "logging": self.context.logs.stats(),
            "modules": self.registry.health(),
            "discovery": self.discovery.as_dict(),
            "signals": self.signals.as_dict(),
            "bus": self.bus.stats(),
            "memory": process_memory(),
            "ipc": self.ipc.as_dict()
            if self.ipc is not None
            else {"running": False, "requests": 0},
            "ipc_file": str(self.paths.state_dir / DISCOVERY_NAME),
        }

    def describe(self) -> str:
        """Return the banner printed by 'kdx doctor'."""
        report = self.health_report()
        app = report["app"]
        return (
            f"{format_version()} | app={app['name']} state={app['state']} "
            f"modules={report['modules']['total']} python={report['runtime']['python']}"
        )

    def _failed_modules(self) -> list[str]:
        return list(self.registry.health()["failed"])

    def _require_state(self, *allowed: ChassisState) -> None:
        if self.state not in allowed:
            raise ChassisError(
                f"chassis is in state {self.state.value}; expected "
                f"{', '.join(state.value for state in allowed)}",
                hint="Create the chassis, then initialize(), start(), shutdown().",
            )

    def __enter__(self) -> Chassis:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.shutdown()


def stdin_is_tty() -> bool:
    """Return True when an interactive wizard can safely prompt."""
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except (ValueError, OSError):
        return False


def _encoding(name: str) -> str:
    stream = getattr(sys, name, None)
    return str(getattr(stream, "encoding", "unknown"))
