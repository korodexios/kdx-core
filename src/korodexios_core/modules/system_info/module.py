"""system_info: host diagnostics proving config scoping, events, and CLI registration."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, cast

from ...cli.render import dump_json, print_key_values, print_section, write_line
from ...config.schema import ConfigField, ConfigKind, ConfigSchema
from ...constants import TAG_NOTE, TAG_OK
from ...kernel.cli_contract import CliArgument, CliCommand, CliRequest, CliSurface
from ...kernel.context import CoreContext
from ...kernel.contract import Module, ModuleState
from ...kernel.diagnostics import DiagnosticCheck, DiagnosticSeverity
from ...kernel.ipc import IpcRequest, IpcRoute
from .collector import disk_snapshot, full_snapshot, load_reading, supports_load_average

DEFAULT_DISK_KEY = "disk_path"
SNAPSHOT_EVENT = "system.info.collected"
LOAD_EVENT = "system.load.sampled"
SECTIONS = ("all", "os", "cpu", "memory", "disk", "uptime")
WATCHER_NAME = "load watcher"
WATCHER_THREAD = "kdx-system-info-load"
DEFAULT_LOAD_INTERVAL = 5.0
MIN_LOAD_INTERVAL = 0.05
# The load average is a Unix reading; ask the platform before promising a watcher.
WATCH_LOAD_DEFAULT = supports_load_average()


class SystemInfoModule(Module):
    """Collects OS/CPU/RAM/disk facts, samples the load average, and serves the CLI.

    Its load watcher is the built-in that reports a real thread through `workers()`, so
    `WorkerSupervisor` and `kdx doctor` are pointed at shipped code: a sampler that dies is a
    finding, and shutdown joins what it started.
    """

    name = "system_info"
    description = "Inspects host OS, CPU, RAM, and disk, and adds system diagnostics commands."
    version = "0.1.0"

    def __init__(self) -> None:
        super().__init__()
        self._disk_path = "."
        self._include_model = True
        self._snapshots = 0
        self._last: dict[str, Any] | None = None
        self._load: list[float] | None = None
        self._load_reads = 0
        self._watch_load = False
        self._load_interval = DEFAULT_LOAD_INTERVAL
        self._thread: threading.Thread | None = None
        self._stop_worker = threading.Event()

    def init(self, context: CoreContext) -> None:
        """Read scoped settings: which filesystem to report and how verbose to be."""
        super().init(context)
        self._disk_path = str(self.config.get_str(DEFAULT_DISK_KEY, "."))
        self._include_model = bool(self.config.get_bool("include_cpu_model", True))
        self._watch_load = bool(self.config.get_bool("watch_load", WATCH_LOAD_DEFAULT))
        self._load_interval = max(
            MIN_LOAD_INTERVAL, float(self.config.get_float("load_interval", DEFAULT_LOAD_INTERVAL))
        )
        # A chassis that re-initializes this module starts counting again: the
        # counters describe the run the operator is looking at, not the last one.
        self._load = None
        self._load_reads = 0
        self._stop_worker.clear()
        self.logger.debug(
            "system_info configured",
            disk_path=self._disk_path,
            include_cpu_model=self._include_model,
            watch_load=self._watch_load,
            load_interval_seconds=round(self._load_interval, 3),
        )

    def config_schema(self) -> ConfigSchema:
        """Declare the scoped keys this module reads."""
        return ConfigSchema(
            module=self.name,
            fields=(
                ConfigField(
                    "disk_path",
                    kind=ConfigKind.PATH,
                    default=".",
                    description="Filesystem the disk section reports on.",
                ),
                ConfigField(
                    "include_cpu_model",
                    kind=ConfigKind.BOOL,
                    default=True,
                    description="Ask the platform for the CPU model string.",
                ),
                ConfigField(
                    "watch_load",
                    kind=ConfigKind.BOOL,
                    default=WATCH_LOAD_DEFAULT,
                    description="Sample the load average on a watched background thread.",
                ),
                ConfigField(
                    "load_interval",
                    kind=ConfigKind.FLOAT,
                    default=DEFAULT_LOAD_INTERVAL,
                    description=(
                        "Seconds between load readings; clamped to at least "
                        f"{MIN_LOAD_INTERVAL}."
                    ),
                ),
            ),
        )

    def ipc_routes(self) -> tuple[IpcRoute, ...]:
        """Expose one system snapshot to the control plane, read-only."""
        return (
            IpcRoute(
                path="/api/v1/system/snapshot",
                handler=self._snapshot_route,
                module=self.name,
                summary="one host snapshot as JSON",
            ),
        )

    def _snapshot_route(self, request: IpcRequest) -> dict[str, Any]:
        """Answer the snapshot route with the same payload 'system info' prints."""
        return self.snapshot()

    def register_cli(self, surface: CliSurface) -> None:
        """Contribute the 'system' command group to the root surface."""
        surface.add_command(
            CliCommand(
                name="system",
                help="Host diagnostics (OS, CPU, memory, disk).",
                module_name=self.name,
                subcommands=(
                    CliCommand(
                        name="info",
                        help="Print a full system snapshot.",
                        module_name=self.name,
                        handler=_info_command,
                        args=(
                            CliArgument(
                                ("--json",),
                                dest="as_json",
                                action="store_true",
                                help="Emit the snapshot as JSON.",
                            ),
                        ),
                    ),
                    CliCommand(
                        name="diagnostics",
                        help="Interactive diagnostics menu (pick the sections to show).",
                        module_name=self.name,
                        handler=_diagnostics_command,
                        args=(
                            CliArgument(
                                ("--section",),
                                dest="section",
                                metavar="SECTION",
                                choices=SECTIONS,
                                default="all",
                                help="Section to display without prompting.",
                            ),
                        ),
                    ),
                    CliCommand(
                        name="disk",
                        help="Report disk usage for a path.",
                        module_name=self.name,
                        handler=_disk_command,
                        args=(
                            CliArgument(
                                ("path",),
                                dest="path",
                                metavar="PATH",
                                nargs="?",
                                default=".",
                                help="Filesystem path to inspect.",
                            ),
                            CliArgument(
                                ("--json",),
                                dest="as_json",
                                action="store_true",
                                help="Emit the report as JSON.",
                            ),
                        ),
                    ),
                ),
            )
        )

    def start(self) -> None:
        """Take the first snapshot, announce it, and start the load watcher."""
        super().start()
        snapshot = self.snapshot()
        self.bus.publish(
            SNAPSHOT_EVENT,
            {"sections": sorted(snapshot), "snapshots": self._snapshots},
            source=self.name,
        )
        memory = snapshot.get("memory", {})
        self.logger.info(
            f"{TAG_OK} system snapshot ready",
            cores=snapshot.get("cpu", {}).get("logical_cores"),
            memory_total=memory.get("total", "-"),
            disk_used=snapshot.get("disk", {}).get("used", "-"),
        )
        self._start_watcher()

    def stop(self) -> None:
        """Join the load watcher and release the cached snapshot."""
        self._stop_watcher()
        super().stop()
        self.logger.info(
            f"{TAG_NOTE} system_info released",
            snapshots=self._snapshots,
            last_sections=len(self._last or {}),
            load_reads=self._load_reads,
        )
        self._last = None

    def snapshot(self) -> dict[str, Any]:
        """Collect (and cache) a snapshot; returns the most recent mapping."""
        data = full_snapshot(self._disk_path, include_model=self._include_model)
        self._snapshots += 1
        self._last = data
        return data

    def workers(self) -> dict[str, threading.Thread]:
        """Report the sampler thread while it runs, so supervision can watch it."""
        worker = self._thread
        if worker is not None and worker.is_alive():
            return {WATCHER_NAME: worker}
        return {}

    def diagnostics(self) -> tuple[DiagnosticCheck, ...]:
        """Report where disk readings come from and whether the watcher is alive."""
        if self.context is None:
            # A module that was only discovered reports its state and nothing else.
            return (
                DiagnosticCheck(
                    name="watcher",
                    message=f"system_info is {self.state.value}",
                    module=self.name,
                ),
            )
        checks: list[DiagnosticCheck] = []
        if not Path(self._disk_path).exists():
            checks.append(
                DiagnosticCheck(
                    name="collector",
                    message=f"disk path {self._disk_path!r} does not exist",
                    severity=DiagnosticSeverity.WARNING,
                    hint="point modules.system_info.disk_path at a real filesystem",
                    module=self.name,
                )
            )
        worker = self._thread
        if self.state is not ModuleState.RUNNING:
            checks.append(
                DiagnosticCheck(
                    name="watcher",
                    message=f"system_info is {self.state.value}",
                    hint="the load watcher starts with the module",
                    module=self.name,
                )
            )
        elif not self._watch_load:
            checks.append(
                DiagnosticCheck(
                    name="watcher",
                    message="load watcher is parked",
                    hint="set modules.system_info.watch_load to sample load in the background",
                    module=self.name,
                )
            )
        elif worker is None:
            checks.append(
                DiagnosticCheck(
                    name="watcher",
                    message="load watcher read no load average",
                    severity=DiagnosticSeverity.WARNING,
                    hint="this platform has no load average; turn watch_load off",
                    module=self.name,
                )
            )
        elif worker.is_alive():
            checks.append(
                DiagnosticCheck(
                    name="watcher",
                    message=f"load watcher alive after {self._load_reads} reads",
                    hint=f"one reading every {round(self._load_interval, 3)} seconds",
                    module=self.name,
                )
            )
        elif self._load_reads == 0:
            checks.append(
                DiagnosticCheck(
                    name="watcher",
                    message="load watcher read no load average",
                    severity=DiagnosticSeverity.WARNING,
                    hint="this platform has no load average; turn watch_load off",
                    module=self.name,
                )
            )
        else:
            checks.append(
                DiagnosticCheck(
                    name="watcher",
                    message=f"load watcher stopped after {self._load_reads} reads",
                    severity=DiagnosticSeverity.WARNING,
                    hint="worker supervision reported it; read the log for the reason",
                    module=self.name,
                )
            )
        return tuple(checks)

    def _start_watcher(self) -> None:
        """Start the load sampler when the configuration asked for one.

        The thread is reported through `workers()`, so a sampler that stops on its own is a
        supervision finding rather than a silent absence of work.
        """
        if not self._watch_load:
            self.logger.debug("load watcher parked", watch_load=False)
            return
        if load_reading() is None:
            # The reading is the whole job; a platform that cannot answer it
            # parks the watcher with a word instead of spinning a thread.
            self.logger.warning(
                "load average is not available here; watcher parked",
                watch_load=True,
            )
            return
        self._stop_worker.clear()
        worker = threading.Thread(
            target=self._watch_load_loop, name=WATCHER_THREAD, daemon=True
        )
        self._thread = worker
        worker.start()
        self.logger.info(
            "system_info load watcher started",
            interval_seconds=round(self._load_interval, 3),
        )

    def _stop_watcher(self) -> None:
        """Ask the sampler for its thread back: what start() acquired, stop() joins."""
        self._stop_worker.set()
        worker = self._thread
        self._thread = None
        if worker is not None and worker.is_alive():
            worker.join(timeout=self.stop_timeout)

    def _watch_load_loop(self) -> None:
        """Read the load average until stop() asks for the thread back."""
        while not self._stop_worker.is_set():
            self._sample_load()
            self._stop_worker.wait(self._load_interval)

    def _sample_load(self) -> None:
        """Cache one reading and publish it; None means this platform cannot answer."""
        reading = load_reading()
        if reading is None:
            return
        self._load = reading
        self._load_reads += 1
        self.bus.publish(
            LOAD_EVENT,
            {"load_average": list(reading), "reads": self._load_reads},
            source=self.name,
        )

    def health(self) -> dict[str, Any]:
        """Return module counters for 'kdx modules show'."""
        base = super().health()
        base.update(
            {
                "snapshots": self._snapshots,
                "disk_path": self._disk_path,
                "last_sections": sorted(self._last) if self._last else [],
                "watch_load": self._watch_load,
                "load_interval_seconds": round(self._load_interval, 3),
                "load_reads": self._load_reads,
                "load_average": self._load,
                "load_watcher": self._thread is not None and self._thread.is_alive(),
            }
        )
        return base


def _module(request: CliRequest) -> SystemInfoModule:
    registry = request.context.registry
    if registry is None:
        raise RuntimeError("the system_info command needs the module registry")
    return cast(SystemInfoModule, registry.require("system_info").module)


def _info_command(request: CliRequest) -> int:
    module = _module(request)
    data = module.snapshot()
    if request.flag("as_json"):
        dump_json(data)
        return 0
    _render_sections(data, "all")
    return 0


def _diagnostics_command(request: CliRequest) -> int:
    """Show one section (or ask which one) - the interactive diagnostics option."""
    from ...cli.ux import MenuOption, UserExperience

    module = _module(request)
    data = module.snapshot()
    section = str(request.option("section") or "")
    ux = UserExperience(request.context.config)
    if not section or section == "all":
        if ux.interactive:
            choice = ux.menu(
                "Diagnostics section",
                [MenuOption(label=name) for name in SECTIONS],
                default_index=1,
            )
            section = SECTIONS[choice - 1]
        else:
            section = "all"
    _render_sections(data, section)
    return 0


def _disk_command(request: CliRequest) -> int:
    target = str(request.option("path") or ".")
    data = disk_snapshot(target)
    if request.flag("as_json"):
        dump_json(data)
        return 0
    print_section("Disk")
    print_key_values(
        [
            ("path", data["path"]),
            ("total", data["total"]),
            ("used", f"{data['used']} ({data['used_percent']}%)"),
            ("free", data["free"]),
            ("inodes", f"{data.get('inodes_free', '-')} free of {data.get('inodes_total', '-')}"),
        ]
    )
    write_line("")
    return 0


def _render_sections(data: dict[str, Any], section: str) -> None:
    wanted = set(SECTIONS[1:]) if section in ("", "all") else {section}
    if "os" in wanted:
        print_section("Operating system")
        print_key_values(_rows(data.get("os", {})))
    if "cpu" in wanted:
        cpu = dict(data.get("cpu", {}))
        load = cpu.get("load_average")
        cpu["load_average"] = " / ".join(f"{value:.2f}" for value in load) if load else "-"
        print_section("CPU")
        print_key_values(_rows(cpu))
    if "memory" in wanted:
        memory = dict(data.get("memory", {}))
        percent = memory.get("used_percent")
        memory["used_percent"] = f"{percent}%" if percent is not None else "-"
        print_section("Memory")
        print_key_values(_rows(memory))
    if "disk" in wanted:
        disk = dict(data.get("disk", {}))
        percent = disk.get("used_percent")
        disk["used_percent"] = f"{percent}%" if percent is not None else "-"
        print_section("Disk")
        print_key_values(_rows(disk))
    if "uptime" in wanted:
        print_section("Uptime")
        print_key_values(_rows(data.get("uptime", {})))


def _rows(mapping: dict[str, Any]) -> list[tuple[str, Any]]:
    """Drop raw byte counts from the human view; JSON keeps them."""
    return [
        (key, value)
        for key, value in mapping.items()
        if value is not None and not key.endswith("_bytes")
    ]


MODULE = SystemInfoModule()
