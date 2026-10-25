"""system_info: host diagnostics proving config scoping, events, and CLI registration."""

from __future__ import annotations

from typing import Any, cast

from ...cli.render import dump_json, print_key_values, print_section, write_line
from ...config.schema import ConfigField, ConfigKind, ConfigSchema
from ...constants import TAG_NOTE, TAG_OK
from ...kernel.cli_contract import CliArgument, CliCommand, CliRequest, CliSurface
from ...kernel.context import CoreContext
from ...kernel.contract import Module
from ...kernel.ipc import IpcRequest, IpcRoute
from .collector import disk_snapshot, full_snapshot

DEFAULT_DISK_KEY = "disk_path"
SNAPSHOT_EVENT = "system.info.collected"
SECTIONS = ("all", "os", "cpu", "memory", "disk", "uptime")


class SystemInfoModule(Module):
    """Collects OS/CPU/RAM/disk facts and exposes them through the root CLI."""

    name = "system_info"
    description = "Inspects host OS, CPU, RAM, and disk, and adds system diagnostics commands."
    version = "0.1.0"

    def __init__(self) -> None:
        super().__init__()
        self._disk_path = "."
        self._include_model = True
        self._snapshots = 0
        self._last: dict[str, Any] | None = None

    def init(self, context: CoreContext) -> None:
        """Read scoped settings: which filesystem to report and how verbose to be."""
        super().init(context)
        self._disk_path = str(self.config.get_str(DEFAULT_DISK_KEY, "."))
        self._include_model = bool(self.config.get_bool("include_cpu_model", True))
        self.logger.debug(
            "system_info configured",
            disk_path=self._disk_path,
            include_cpu_model=self._include_model,
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
        """Take the first snapshot and announce it on the event bus."""
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

    def stop(self) -> None:
        """Release the cached snapshot; nothing else is held open."""
        self.logger.info(
            f"{TAG_NOTE} system_info released",
            snapshots=self._snapshots,
            last_sections=len(self._last or {}),
        )
        self._last = None

    def snapshot(self) -> dict[str, Any]:
        """Collect (and cache) a snapshot; returns the most recent mapping."""
        data = full_snapshot(self._disk_path, include_model=self._include_model)
        self._snapshots += 1
        self._last = data
        return data

    def health(self) -> dict[str, Any]:
        """Return module counters for 'kdx modules show'."""
        base = super().health()
        base.update(
            {
                "snapshots": self._snapshots,
                "disk_path": self._disk_path,
                "last_sections": sorted(self._last) if self._last else [],
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
