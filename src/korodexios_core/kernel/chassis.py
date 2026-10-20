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
from ..constants import PACKAGE_NAME, TAG_ERROR, TAG_NOTE, TAG_OK
from ..events.bus import EventBus
from ..exceptions import ChassisError
from ..logging.engine import KorodexLogger, build_logger, close_logger, ensure_utf8_streams
from ..paths import AppPaths
from ..version import CORE_API_VERSION, __version__, format_version
from .cli_contract import CliCommand, CliSurface
from .context import CoreContext
from .discovery import DiscoveryReport, ModuleDiscovery
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
    logger: KorodexLogger
    config: LayeredConfig
    paths: AppPaths
    bus: EventBus
    discovery: DiscoveryReport
    state: ChassisState = ChassisState.NEW
    started_at: float = field(default_factory=time.time)

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

    def start(self) -> Chassis:
        """Run module start hooks and enter the running state."""
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
        self.logger.debug("chassis running", modules=len(self.registry))
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
        """Return the inspection payload rendered by 'korodex doctor'."""
        return {
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
            },
            "logging": self.context.logs.stats(),
            "modules": self.registry.health(),
            "discovery": self.discovery.as_dict(),
            "signals": self.signals.as_dict(),
            "bus": self.bus.stats(),
        }

    def describe(self) -> str:
        """Return the banner printed by 'korodex doctor'."""
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
