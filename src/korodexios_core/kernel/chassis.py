"""The chassis: lifecycle coordinator wiring config, logging, bus, signals, modules."""

from __future__ import annotations

import argparse
import os
import platform
import re
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..config.layered import LayeredConfig
from ..constants import (
    DOCTOR_SCHEMA_VERSION,
    PACKAGE_NAME,
    TAG_ERROR,
    TAG_NOTE,
    TAG_OK,
)
from ..events.bus import EventBus
from ..exceptions import ChassisError, ConfigurationError
from ..logging.engine import (
    KorodexiosLogger,
    build_logger,
    close_logger,
    ensure_utf8_streams,
    resolve_level,
)
from ..paths import AppPaths
from ..version import CORE_API_VERSION, __version__, format_version
from .async_runtime import AsyncRuntime
from .cli_contract import CliCommand, CliRequest, CliSurface, add_command_arguments
from .context import CoreContext
from .contracts import (
    ContractReport,
    ContractRow,
    contract_rows,
    dedupe_rows,
    verify_contracts,
)
from .diagnostics import (
    DiagnosticCheck,
    DiagnosticSeverity,
    aggregate_diagnostics,
    diagnostic_check,
)
from .discovery import DiscoveryReport, ModuleDiscovery
from .instance_lock import InstanceLock
from .ipc import (
    DISCOVERY_NAME,
    IpcDiscovery,
    IpcError,
    IpcOptions,
    IpcRoute,
    IpcServer,
    IpcStaticMount,
    new_token,
    process_memory,
)
from .locks import NamedLock, NamedLockRegistry
from .project import ProjectIdentity, project_identity
from .registry import ModuleRegistry
from .scheduler import TaskScheduler
from .signals import SignalCoordinator, signal_name
from .supervision import WorkerRecord, WorkerSupervisor

TICK_EVENT = "core.tick"
RELOAD_EVENT = "core.config.reloaded"
WORKER_LOST_EVENT = "core.worker.lost"
STATIC_DIR_PREFIXES = ("static", "ui", "assets")
"""Directory names a module conventionally ships; nothing here depends on them."""

_UNSET: Any = object()
"""Sentinel distinguishing 'key absent' from 'key set to None' in a reload diff."""


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
    config: LayeredConfig | None = None
    instance_lock: bool | None = None
    asyncio_enabled: bool | None = None
    supervision: bool | None = None
    scheduler: bool | None = None
    project_contracts: Sequence[ContractRow] = ()


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
    async_runtime: AsyncRuntime | None = None
    supervisor: WorkerSupervisor | None = None
    instance_lock: InstanceLock | None = None
    ipc_routes: tuple[IpcRoute, ...] = ()
    ipc_static: tuple[IpcStaticMount, ...] = ()
    locks: NamedLockRegistry | None = None
    scheduler: TaskScheduler | None = None
    cli_overrides: dict[str, Any] = field(default_factory=dict)
    instance_lock_enabled: bool | None = None
    reloads: int = 0
    last_reload: dict[str, Any] | None = None
    project: ProjectIdentity | None = None
    """The one triple the chassis reports for the project it hosts."""
    project_contracts: tuple[ContractRow, ...] = ()
    """Rows the embedding project declared, beside its modules' own."""

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
        config = options.config if options.config is not None else LayeredConfig.load(
            config_file=paths.config_file,
            local_config_file=paths.local_config_file,
            env_file=paths.env_file,
            environ=environ,
            cli_overrides=options.cli_overrides,
            runtime_defaults={"core.home": str(paths.root)},
        )
        paths = (
            paths.with_state_dir(config.get_str("core.paths.state_dir"))
            .with_data_dir(config.get_str("core.paths.data_dir"))
            .with_durable_dir(config.get_str("core.paths.durable_dir"))
            .with_log_dir(config.get("core.log.dir"))
            .with_extra_module_dirs(
                [str(item) for item in config.get_list("core.modules.extra_paths")]
            )
        )
        paths.ensure_directories()
        if config.get_bool("core.cli.interactive", True) and not stdin_is_tty():
            config = config.with_cli_overrides({"core.cli.interactive": False})

        _root_logger, factory = build_logger(config, paths)
        log = factory.get("chassis")
        if options.utf8_streams:
            log.debug("stream encodings ready", **ensure_utf8_streams())
        bus = EventBus(logger=log)
        supervisor = WorkerSupervisor(
            logger=log.child("supervision"),
            enabled=options.supervision
            if options.supervision is not None
            else config.get_bool("core.supervision.enabled", True),
        )
        async_enabled = (
            options.asyncio_enabled
            if options.asyncio_enabled is not None
            else config.get_bool("core.async.enabled", False)
        )
        locks = NamedLockRegistry(
            paths.locks_dir,
            logger=log.child("locks"),
        )
        scheduler_enabled = (
            options.scheduler
            if options.scheduler is not None
            else config.get_bool("core.scheduler.enabled", True)
        )
        scheduler = TaskScheduler(
            logger=log.child("scheduler"),
            bus=bus,
            enabled=scheduler_enabled,
        )
        async_runtime: AsyncRuntime | None = None
        if async_enabled:
            async_runtime = AsyncRuntime(
                logger=log.child("async"),
                shutdown_timeout=config.get_float("core.async.shutdown_timeout", 2.0) or 2.0,
            )
        context = CoreContext(
            config=config,
            paths=paths,
            logs=factory,
            bus=bus,
            app_name=config.get_str("core.name", "korodexios"),
            async_runtime=async_runtime,
            supervisor=supervisor,
            scheduler=scheduler,
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

        log.info(
            f"{TAG_OK} chassis prepared",
            modules=registry.health()["total"],
            ready=len(discovery.candidates),
            failed=len(discovery.failures),
            config_file=str(paths.config_file or "(defaults only)"),
        )
        for failure in discovery.failures:
            log.warning(f"{TAG_ERROR} {failure.describe()}")
        for optional in discovery.optional:
            log.info(f"{TAG_NOTE} {optional.describe()}")
        if discovery.skipped:
            named = ", ".join(
                f"{name} ({reason})"
                for name, reason in discovery.skipped[:_CONFIG_WARNING_LIMIT]
            )
            unlisted = len(discovery.skipped) - _CONFIG_WARNING_LIMIT
            if unlisted > 0:
                named += f", +{unlisted} more"
            log.info(f"{TAG_NOTE} modules skipped: {named}")
        config.declare_sentinels(registry.schema_sentinels())
        identity = project_identity(config, home=paths.root)
        project_rows = dedupe_rows(contract_rows(options.project_contracts))
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
            async_runtime=async_runtime,
            supervisor=supervisor,
            locks=locks,
            scheduler=scheduler,
            cli_overrides=dict(options.cli_overrides),
            instance_lock_enabled=options.instance_lock,
            project=identity,
            project_contracts=project_rows,
        )
        context.chassis = chassis
        return chassis

    def ensure_initialized(self) -> Chassis:
        """Initialize module hooks once; a no-op when already initialized or running."""
        if self.state in (ChassisState.PREPARED, ChassisState.NEW):
            self.initialize()
        return self

    def initialize(self) -> Chassis:
        """Run module init hooks, collect their commands, mark the chassis initialized.

        Collecting the declarative command tree here - into a headless surface - is what
        lets an embedded daemon dispatch `POST /api/v1/rpc` without the CLI ever being
        imported. A later `collect_cli(argparse_surface)` replays the same commands.
        """
        self._require_state(ChassisState.PREPARED, ChassisState.NEW)
        self.registry.initialize_all(self.context)
        self.registry.collect_headless()
        self.state = ChassisState.FAILED if self._failed_modules() else ChassisState.INITIALIZED
        self.bus.publish("core.initialized", {"modules": len(self.registry)}, source="chassis")
        return self

    def _refuse_strict_discovery(self) -> None:
        """Refuse the boot when discovery failed and the consumer asked it to.

        'core.modules.strict_discovery' is the gate a project sets when running on an
        untested chassis is worse than not running: a module that declares a
        'requires_core' this chassis cannot satisfy, fails to import, breaks the
        contract, or ships an unreadable manifest is a defect in the pairing, not a
        detail to log and boot past. The refusal is one line naming the module and
        what it required, and the exit code is the configuration error a deployment
        script can branch on. Inspection commands never call this, so 'kdx doctor'
        and 'kdx status' stay able to report the refusal instead of hiding it.
        """
        if not self.config.get_bool("core.modules.strict_discovery", False):
            return
        failures = self.discovery.failures
        if not failures:
            return
        detail = failures[0].describe()
        remaining = len(failures) - 1
        tail = f"; {remaining} more discovery failure(s)" if remaining else ""
        self.logger.error(f"{TAG_ERROR} strict discovery: {detail}{tail}")
        raise ConfigurationError(
            f"module discovery refused the boot: {detail}{tail}",
            hint="fix the module, or set core.modules.strict_discovery: false to boot without it",
        )

    def start(self, *, ipc: bool | None = None) -> Chassis:
        """Run module start hooks, open the control plane, and enter the running state."""
        allowed = (ChassisState.INITIALIZED, ChassisState.PREPARED, ChassisState.FAILED)
        if self.state not in allowed:
            raise ChassisError(
                f"cannot start a chassis in state {self.state.value}",
                hint="Call initialize() before start().",
            )
        self._refuse_strict_discovery()
        self.ensure_initialized()
        self._acquire_instance_lock()
        runtime = self.async_runtime
        if runtime is not None:
            runtime.start()
        self.registry.start_all()
        self._watch_module_workers()
        self.ipc_routes = tuple(self.registry.ipc_routes())
        self.ipc_static = tuple(self.registry.static_dirs())
        self.start_scheduler()
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
        routes = list(self.ipc_routes) if self.config.get_bool("core.ipc.routes", True) else []
        statics = (
            list(self.ipc_static) if self.config.get_bool("core.ipc.static", True) else []
        )
        server = IpcServer(
            status_fn=self.health_report,
            shutdown_fn=self.signals.request_clean_stop,
            state_fn=lambda: self.state.value,
            options=options,
            logger=self.logger.child("ipc"),
            routes=routes,
            static_dirs=statics,
            rpc_fn=self.dispatch_rpc
            if self.config.get_bool("core.ipc.rpc", True)
            else None,
            reload_fn=self.reload_config,
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
                "routes": [route.path for route in server.module_routes()],
                "static": [mount.prefix for mount in server.static_mounts()],
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
            self._serve_reload_request()
            self._run_due_tasks()
            self.supervise()
            wait_for = interval if remaining is None else min(interval, remaining)
            self.signals.wait(max(0.001, wait_for))
        self.logger.debug("tick loop finished", ticks=ticks)
        return int(self.exit_code)

    def _acquire_instance_lock(self) -> InstanceLock | None:
        """Claim this root's single-instance lock before any module starts.

        A second daemon on the same root would share the state directory and,
        worse, bind a fallback port while the operator believes it took the
        configured one. The lock is therefore claimed here, not at boot, so only
        a daemon that intends to run pays for it.
        """
        if self.instance_lock is not None:
            return self.instance_lock
        if not self._instance_lock_wanted():
            return None
        lock = InstanceLock(
            self.paths.lock_file,
            metadata={
                "label": self.config.get_str("core.name", "korodexios") or "chassis",
                "started_at": self.started_at,
            },
            logger=self.logger,
        )
        lock.acquire()
        self.instance_lock = lock
        return lock

    def _daemon_endpoint(self) -> str | None:
        """Return the endpoint a live daemon published, when one answers."""
        discovery = IpcDiscovery.load(self.paths.state_dir / DISCOVERY_NAME)
        if discovery is None or not discovery.is_process_alive():
            return None
        return str(discovery.base_url())

    def _lock_holder(self) -> dict[str, Any] | None:
        """Return who owns the instance lock, when the chassis can see one."""
        lock = self.instance_lock
        if lock is None:
            return None
        holder = lock.holder()
        return holder.as_dict() if holder is not None else None

    def _instance_lock_wanted(self) -> bool:
        """Return whether this chassis should claim the single-instance lock.

        The caller's explicit say-so (kdx --no-instance-lock, ChassisOptions)
        outranks the configuration key, which stays the default for daemons.
        """
        if self.instance_lock_enabled is not None:
            return bool(self.instance_lock_enabled)
        return bool(self.config.get_bool("core.instance.lock", True))

    def start_scheduler(self, *, threaded: bool | None = None) -> Chassis:
        """Open the periodic task scheduler; a no-op when it is disabled or absent."""
        scheduler = self.scheduler
        if scheduler is None:
            return self
        wants_thread = (
            threaded
            if threaded is not None
            else self.config.get_bool("core.scheduler.threaded", False)
        )
        scheduler.start(threaded=bool(wants_thread))
        return self

    def stop_scheduler(self, timeout: float | None = None) -> Chassis:
        """Stop the scheduler inside the caller's budget; tasks never outlive stop."""
        scheduler = self.scheduler
        if scheduler is not None:
            scheduler.stop(timeout)
        return self

    def _run_due_tasks(self) -> list[str]:
        """Run the periodic tasks that came due on this tick; never raise."""
        scheduler = self.scheduler
        if scheduler is None:
            return []
        try:
            return scheduler.run_due()
        except Exception as exc:  # a task bug must never end the tick loop
            self.logger.warning(
                f"{TAG_ERROR} scheduled task loop failed: {type(exc).__name__}: {exc}"
            )
            return []

    def acquire_lock(
        self,
        name: str,
        *,
        timeout: float | None = 0.0,
        blocking: bool = True,
        metadata: Mapping[str, Any] | None = None,
    ) -> NamedLock:
        """Take a named advisory lock; raise LockTimeoutError when it stays busy."""
        registry = self.locks
        if registry is None:
            return self.paths.lock(name, timeout=timeout, blocking=blocking, metadata=metadata)
        return registry.acquire(
            name, timeout=timeout, blocking=blocking, metadata=metadata
        )

    def release_lock(self, name: str) -> bool:
        """Release named lock *name*; False when this chassis never held it."""
        registry = self.locks
        return bool(registry.release(name)) if registry is not None else False

    def named_lock(self, name: str) -> NamedLock | None:
        """Return the handle this chassis holds for *name*, or None."""
        registry = self.locks
        return registry.get(name) if registry is not None else None

    def lock(
        self,
        name: str,
        *,
        timeout: float | None = 0.0,
        blocking: bool = True,
        metadata: Mapping[str, Any] | None = None,
    ) -> Any:
        """Return a context manager holding named lock *name* for the block."""
        registry = self.locks
        if registry is None:
            raise ChassisError(
                "this chassis has no named lock registry",
                hint="boot with Chassis.create(); a bare Chassis(...) has no state directory.",
            )
        return registry.hold(name, timeout=timeout, blocking=blocking, metadata=metadata)

    def register_static_dir(
        self,
        route_prefix: str,
        directory: str | Path,
        **kwargs: Any,
    ) -> IpcStaticMount:
        """Serve *directory* under *route_prefix* on the control plane.

        Registration works before and after the listener is up, exactly like a
        route: the mount is kept in the chassis list and published on the live
        server when one runs.
        """
        mount = IpcStaticMount(
            route_prefix,
            Path(directory),
            index_names=tuple(kwargs.get("index", ("index.html",)))
            if not isinstance(kwargs.get("index", "index.html"), str)
            else (str(kwargs.get("index")),),
            cache_control=str(kwargs.get("cache_control", "no-cache")),
            auth=bool(kwargs.get("auth", False)),
            module=str(kwargs.get("module", "")),
        )
        kept = tuple(item for item in self.ipc_static if item.prefix != mount.prefix)
        self.ipc_static = (*kept, mount)
        server = self.ipc
        if server is not None and server.running:
            server.add_static_dir(mount)
        return mount

    def unregister_static_dir(self, route_prefix: str) -> bool:
        """Withdraw a static mount by prefix; return whether one was registered."""
        clean = str(route_prefix).rstrip("/")
        existed = any(item.prefix == clean for item in self.ipc_static)
        self.ipc_static = tuple(item for item in self.ipc_static if item.prefix != clean)
        server = self.ipc
        if server is not None and server.running:
            server.unregister_static_dir(clean)
        return existed

    def static_mounts(self) -> tuple[IpcStaticMount, ...]:
        """Return the static directories published on the control plane."""
        return tuple(self.ipc_static)

    def _release_instance_lock(self) -> None:
        """Give up the single-instance lock so the next start can boot."""
        lock = self.instance_lock
        self.instance_lock = None
        if lock is not None:
            lock.release()

    def _release_named_locks(self) -> None:
        """Release every named lock this process held; shutdown leaves nothing behind."""
        registry = self.locks
        if registry is None:
            return
        released = registry.release_all()
        if released:
            self.logger.debug("named locks released", count=released)

    def _watch_module_workers(self) -> None:
        """Register the workers each module reports so supervision can see them die."""
        supervisor = self.supervisor
        if supervisor is None:
            return
        for module_name, workers in self.registry.declared_workers().items():
            for name, thread in workers:
                supervisor.watch(module_name, thread, name=name)

    def supervise(self) -> list[WorkerRecord]:
        """Check watched workers; a dead one moves its module to FAILED."""
        supervisor = self.supervisor
        if supervisor is None:
            return []
        dead = supervisor.check()
        for record in dead:
            self.registry.mark_failed(
                record.module, f"worker {record.name} stopped unexpectedly"
            )
            self.bus.publish(
                WORKER_LOST_EVENT,
                {
                    "module": record.module,
                    "worker": record.name,
                    "thread": record.thread.name,
                },
                source="chassis",
            )
        return dead

    def _serve_reload_request(self) -> dict[str, Any] | None:
        """Serve a pending SIGHUP or IPC reload request; None when none is pending."""
        if not self.signals.reload_requested:
            return None
        summary = self.reload_config()
        self.signals.clear_reload()
        return summary

    def reload_config(self) -> dict[str, Any]:
        """Re-read the configuration layers while the daemon keeps running.

        The layers are rebuilt from the same sources with the same CLI overrides,
        the diff is announced on the bus as core.config.reloaded, and each module
        gets its scoped view through the optional on_config_reload hook. Path
        roles and the log file location are boot-time decisions: moving them
        means a restart.
        """
        previous = self.config.as_dict()
        reloaded = LayeredConfig.load(
            config_file=self.paths.config_file,
            local_config_file=self.paths.local_config_file,
            env_file=self.paths.env_file,
            environ=os.environ,
            cli_overrides=dict(self.cli_overrides),
            runtime_defaults={"core.home": str(self.paths.root)},
        )
        if reloaded.get_bool("core.cli.interactive", True) and not stdin_is_tty():
            reloaded = reloaded.with_cli_overrides({"core.cli.interactive": False})
        current = reloaded.as_dict()
        changed = sorted(
            key
            for key in set(previous) | set(current)
            if previous.get(key, _UNSET) != current.get(key, _UNSET)
        )
        self.config = reloaded
        self.context.config = reloaded
        self.reloads += 1
        self.last_reload = {
            "reloads": self.reloads,
            "changed": changed,
            "keys": len(current),
            "at": time.time(),
        }
        if "core.log.level" in changed:
            self._apply_log_level(reloaded.get_str("core.log.level"))
        self.registry.notify_config_reload(reloaded)
        summary = dict(self.last_reload)
        summary["changed"] = list(changed)
        self.bus.publish(RELOAD_EVENT, summary, source="chassis")
        self.logger.info(
            f"{TAG_NOTE} configuration reloaded ({len(changed)} keys changed)",
            reloads=self.reloads,
            changed=changed[:_CONFIG_WARNING_LIMIT],
        )
        return summary

    def _apply_log_level(self, level: str) -> None:
        """Move the live logging pipeline to *level* without rebuilding handlers."""
        resolved = resolve_level(level)
        root = self.context.logs.root_logger
        root.setLevel(resolved)
        for handler in root.handlers:
            handler.setLevel(resolved)

    def register_ipc_route(self, route: IpcRoute) -> bool:
        """Publish *route* on the control plane, before or after it is listening.

        A module may register early (in init) or late (on demand); either way the
        route ends up in the chassis list and on the live listener when one runs.
        """
        kept = tuple(item for item in self.ipc_routes if item.path != route.path)
        self.ipc_routes = (*kept, route)
        server = self.ipc
        if server is not None and server.running:
            server.add_route(route)
        return True

    def unregister_ipc_route(self, path: str) -> bool:
        """Withdraw a route by path; return whether one was registered."""
        existed = any(route.path == path for route in self.ipc_routes)
        self.ipc_routes = tuple(route for route in self.ipc_routes if route.path != path)
        server = self.ipc
        if server is not None and server.running:
            server.remove_route(path)
        return existed

    def dispatch_rpc(self, command: str, args: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Run one registered module command here: the one-shot delegation path.

        A module CLI command that finds a live daemon can forward its execution
        instead of booting a chassis of its own. The daemon runs the real handler,
        so the client gets the same exit code plus the module's health snapshot.
        """
        cleaned = str(command).strip()
        parts = [part for part in re.split(r"[. /]+", cleaned) if part]
        if not parts:
            raise LookupError("rpc needs a command name")
        name = parts[0]
        candidates = [
            candidate for candidate in self.registry.commands() if candidate.name == name
        ]
        if not candidates:
            raise LookupError(
                f"no module command named {name!r}; run 'kdx modules list' to see them"
            )
        top = candidates[0]
        target = top
        consumed = 1
        walked = [name]
        while target.subcommands and consumed < len(parts):
            child = target.find(parts[consumed])
            if child is None:
                trail = " ".join(walked)
                raise LookupError(
                    f"{trail} has no subcommand {parts[consumed]!r}; try 'kdx {trail} --help'"
                )
            target = child
            walked.append(child.name)
            consumed += 1
        trailing = parts[consumed:]
        handler = target.handler
        if handler is None:
            raise LookupError(f"{name} is a command group; name one of its subcommands")
        namespace = self._rpc_namespace(target, args, trailing)
        request = CliRequest(
            context=self.context,
            command=cleaned,
            namespace=namespace,
            options={"chassis": self, "rpc": True},
        )
        exit_code = int(handler(request))
        return {
            "command": cleaned,
            "exit_code": exit_code,
            "module": target.module_name or top.module_name or name,
            "health": self.registry.health_for(name),
            "path": " ".join(walked),
            "depth": len(walked),
        }
    def _rpc_namespace(
        self,
        target: CliCommand,
        args: Mapping[str, Any] | None,
        trailing: Sequence[str] = (),
    ) -> Any:
        """Turn delegated args into the namespace the handler expects.

        A client may send declared options directly (a mapping such as
        '{"lines": 5}') or the raw tokens it received after a double dash
        ({"argv": ["-n", "5"]}); the second form is parsed against the command's
        own declarative arguments, so the daemon sees what the operator would
        have seen locally.
        """
        payload = dict(args or {})
        argv = payload.pop("argv", None)
        tokens = [str(item) for item in argv] if isinstance(argv, list) else []
        tokens.extend(str(item) for item in trailing)
        parser = argparse.ArgumentParser(prog=target.name, allow_abbrev=False)
        add_command_arguments(parser, target.args)
        try:
            namespace = parser.parse_args(tokens)
        except SystemExit as exc:
            raise LookupError(
                f"the delegated arguments for {target.name!r} did not parse "
                f"(exit {exc.code})"
            ) from exc
        for key, value in payload.items():
            setattr(namespace, str(key), value)
        return namespace

    def declared_contracts(self) -> tuple[tuple[str, ContractRow], ...]:
        """Return every declared file with the project or module that declared it."""
        rows: list[tuple[str, ContractRow]] = [
            ("project", row) for row in self.project_contracts
        ]
        for name, declared in self.registry.contracts_by_module().items():
            rows.extend((name, row) for row in declared)
        return tuple(rows)

    def register_contracts(self, rows: Any) -> tuple[ContractRow, ...]:
        """Add project-level rows to verify; return everything declared so far.

        An embedding project owns rows the same way a module does: it states them once,
        and the chassis reads them back through 'kdx doctor' and 'kdx contracts check'.
        """
        self.project_contracts = dedupe_rows(self.project_contracts + contract_rows(rows))
        return self.project_contracts

    def contract_report(self) -> ContractReport:
        """Hash the declared files under the project's contract directory, never write one."""
        root = Path(self.project.contract_dir) if self.project is not None else None
        return verify_contracts([row for _, row in self.declared_contracts()], root)

    def _chassis_diagnostics(self) -> list[DiagnosticCheck]:
        """Contribute the chassis' own checks to the unified diagnostics report."""
        checks: list[DiagnosticCheck] = []
        for key, hint in self.config.unknown_keys(self.registry.names()):
            checks.append(
                diagnostic_check(
                    name="unknown-key",
                    message=f"{key}{_advice(hint)}",
                    severity=DiagnosticSeverity.WARNING,
                    hint="remove the key or spell it the way the chassis reads it",
                    module="chassis",
                )
            )
        contracts = self.contract_report()
        if contracts.rows:
            checks.append(
                diagnostic_check(
                    name="contracts",
                    message=f"contracts: {contracts.summary()}",
                    severity=(
                        DiagnosticSeverity.INFO
                        if contracts.ok
                        else DiagnosticSeverity.WARNING
                    ),
                    hint="run 'kdx contracts check' for the named files; the chassis "
                    "never rewrites them",
                    module="chassis",
                )
            )
        strict = self.config.get_bool("core.modules.strict_discovery", False)
        for failure in self.discovery.failures:
            checks.append(
                diagnostic_check(
                    name="discovery",
                    message=failure.describe(),
                    severity=(
                        DiagnosticSeverity.ERROR if strict else DiagnosticSeverity.WARNING
                    ),
                    hint="run 'kdx modules list' for the full import error"
                    + ("; boot is refused" if strict else ""),
                    module="chassis",
                )
            )
        for name, keys in self.registry.settings_collisions().items():
            checked = ", ".join(f"modules.{name}.{key}" for key in keys)
            checks.append(
                diagnostic_check(
                    name="module-setting",
                    message=f"module {name} declares {', '.join(repr(key) for key in keys)} "
                    f"in its own configuration schema; the chassis reads {checked} as "
                    "lifecycle control",
                    severity=DiagnosticSeverity.WARNING,
                    hint="rename the module's own key, or set the chassis key explicitly",
                    module="chassis",
                )
            )
        for optional in self.discovery.optional:
            checks.append(
                diagnostic_check(
                    name="optional-dependency",
                    message=optional.describe(),
                    severity=DiagnosticSeverity.INFO,
                    hint=optional.hint,
                    module="chassis",
                )
            )
        for name in self._failed_modules():
            checks.append(
                diagnostic_check(
                    name="module-state",
                    message=f"module {name} is FAILED",
                    severity=DiagnosticSeverity.ERROR,
                    hint="run 'kdx modules show' " + name + " for its last error",
                    module="chassis",
                )
            )
        scheduler = self.scheduler
        if scheduler is not None:
            for task in scheduler.failing_tasks():
                checks.append(
                    diagnostic_check(
                        name="scheduled-task",
                        message=(
                            f"task {task.name} failed {task.failures} time(s): {task.last_error}"
                        ),
                        severity=DiagnosticSeverity.WARNING,
                        hint="the task still runs on schedule; inspect its last error",
                        module=task.module or "chassis",
                    )
                )
        locks = self.locks
        if locks is not None:
            for stale in locks.stale_names():
                checks.append(
                    diagnostic_check(
                        name="named-lock",
                        message=f"lock {stale} was left behind by a process that is gone",
                        severity=DiagnosticSeverity.INFO,
                        hint="the lock is free; the file only records the last owner",
                        module="chassis",
                    )
                )
        if self.instance_lock is not None and self.instance_lock.is_stale():
            checks.append(
                diagnostic_check(
                    name="instance-lock",
                    message=f"stale lock payload at {self.instance_lock.path}",
                    severity=DiagnosticSeverity.WARNING,
                    hint="the owning process is gone; the lock itself is free",
                    module="chassis",
                )
            )
        return checks

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
        self._run_due_tasks()
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
        self.stop_scheduler(timeout=timeout)
        self.registry.stop_all(timeout=timeout)
        runtime = self.async_runtime
        if runtime is not None:
            runtime.stop()
        self._release_named_locks()
        self._release_instance_lock()
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
                "secrets": list(self.config.secret_keys()),
                "aliases": self.config.env_aliases(),
                "reloads": self.reloads,
            },
            "logging": self.context.logs.stats(),
            "modules": self.registry.health(),
            "discovery": self.discovery.as_dict(),
            "project": self.project.as_dict() if self.project is not None else None,
            "contracts": self.contract_report().as_dict(),
            "signals": self.signals.as_dict(),
            "bus": self.bus.stats(),
            "memory": process_memory(),
            "instance": {
                "lock": str(self.paths.lock_file),
                "enabled": self._instance_lock_wanted(),
                "held": bool(self.instance_lock.held) if self.instance_lock else False,
                "holder": self._lock_holder(),
                "daemon": self._daemon_endpoint(),
            },
            "async": self.async_runtime.as_dict()
            if self.async_runtime is not None
            else {"running": False},
            "supervision": self.supervisor.as_dict()
            if self.supervisor is not None
            else {"watched": 0},
            "scheduler": self.scheduler.stats() if self.scheduler is not None else {"scheduled": 0},
            "locks": self.locks.as_dict() if self.locks is not None else {"held": 0},
            "static": [mount.as_dict() for mount in self.ipc_static],
            "diagnostics": aggregate_diagnostics(
                self._chassis_diagnostics() + self.registry.diagnostics()
            ),
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
