"""Core commands contributed by the chassis itself, not by any module."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any, cast

from ..constants import TAG_ERROR, TAG_NOTE, TAG_OK, ExitCode
from ..kernel.cli_contract import CliArgument, CliCommand, CliRequest
from ..version import format_version
from .render import (
    dump_json,
    format_bool,
    print_key_values,
    print_section,
    print_table,
    write_line,
    write_status,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..kernel.chassis import Chassis

RUN_HELP = "Boot the chassis, run the lifecycle, then stop cleanly."


def wizard_command() -> CliCommand:
    """Return the root-wizard command; hidden from the wizard itself."""
    return CliCommand(
        name="wizard",
        help="Open the interactive command wizard.",
        handler=_wizard_command,
        group="core",
        show_in_wizard=False,
    )


def _wizard_command(request: CliRequest) -> int:
    """Run the numbered-menu wizard over every registered command."""
    from .ux import UserExperience
    from .wizard import RootWizard

    surface = request.options.get("surface")
    commands = list(surface.commands()) if surface is not None else []
    if not commands:
        write_line(f"{TAG_NOTE} no commands are registered yet")
        return int(ExitCode.SUCCESS)
    ux = UserExperience(request.context.config)
    return RootWizard(ux, request.context, commands, options=dict(request.options)).run()


def core_commands(chassis: Chassis) -> list[CliCommand]:
    """Build the chassis command set bound to *chassis*."""
    return [
        CliCommand(
            name="version",
            help="Print the chassis and core API version.",
            handler=_version_command,
            group="core",
        ),
        CliCommand(
            name="doctor",
            help="Inspect the environment, configuration, logging, and modules.",
            args=(
                CliArgument(
                    ("--json",),
                    dest="as_json",
                    action="store_true",
                    help="Emit the report as JSON.",
                ),
            ),
            handler=_doctor_command,
            group="core",
        ),
        CliCommand(
            name="modules",
            help="Inspect auto-discovered modules.",
            group="core",
            subcommands=(
                CliCommand(
                    name="list",
                    help="List discovered modules and their state.",
                    args=(
                        CliArgument(
                            ("--json",),
                            dest="as_json",
                            action="store_true",
                            help="Emit the listing as JSON.",
                        ),
                    ),
                    handler=_modules_list_command,
                    module_name="core",
                ),
                CliCommand(
                    name="show",
                    help="Show one module in detail (initializes modules).",
                    args=(
                        CliArgument(("name",), nargs="?", help="Module name; omit for all."),
                        CliArgument(
                            ("--json",),
                            dest="as_json",
                            action="store_true",
                            help="Emit the detail as JSON.",
                        ),
                    ),
                    handler=_modules_show_command,
                    module_name="core",
                ),
            ),
        ),
        CliCommand(
            name="config",
            help="Show the resolved configuration and its provenance.",
            group="core",
            subcommands=(
                CliCommand(
                    name="show",
                    help="Print every resolved key with the layer that won.",
                    args=(
                        CliArgument(
                            ("--key",), metavar="KEY", help="Only show keys starting with KEY."
                        ),
                        CliArgument(
                            ("--json",),
                            dest="as_json",
                            action="store_true",
                            help="Emit the mapping as JSON.",
                        ),
                    ),
                    handler=_config_show_command,
                    module_name="core",
                ),
                CliCommand(
                    name="file",
                    help="Print the discovered config and env file paths.",
                    handler=_config_file_command,
                    module_name="core",
                ),
            ),
        ),
        CliCommand(
            name="run",
            help=RUN_HELP,
            args=(
                CliArgument(
                    ("--once",),
                    action="store_true",
                    help="Publish one tick and stop (CI and smoke tests).",
                ),
                CliArgument(
                    ("--duration",),
                    metavar="SECONDS",
                    type="float",
                    help="Stop automatically after SECONDS.",
                ),
            ),
            handler=_run_command,
            group="core",
        ),
    ]


def _version_command(request: CliRequest) -> int:
    """Print the version banner."""
    write_line(format_version())
    return int(ExitCode.SUCCESS)


def _doctor_command(request: CliRequest) -> int:
    """Render the environment inspection report."""
    chassis = _chassis(request)
    report = chassis.health_report()
    interactive = _interactivity(chassis)
    report["interface"] = interactive
    if request.flag("as_json"):
        dump_json(report)
        return int(ExitCode.SUCCESS)

    print_section("Chassis")
    print_key_values(
        [
            ("app", f"{report['app']['name']} ({report['app']['distribution']})"),
            ("version", report["app"]["version"]),
            ("core api", report["app"]["core_api"]),
            ("state", report["app"]["state"]),
            ("uptime", f"{report['app']['uptime_seconds']:.3f} s"),
        ]
    )
    print_section("Runtime")
    print_key_values(
        [
            ("python", report["runtime"]["python"]),
            ("executable", report["runtime"]["executable"]),
            ("platform", report["runtime"]["platform"]),
            ("pid", report["runtime"]["pid"]),
            ("cwd", report["runtime"]["cwd"]),
            ("stdout encoding", report["runtime"]["encoding"]["stdout"]),
            ("stderr encoding", report["runtime"]["encoding"]["stderr"]),
            ("stdin tty", interactive["stdin_tty"]),
            ("stdout tty", interactive["stdout_tty"]),
            ("interactive mode", format_bool(interactive["interactive"])),
        ]
    )
    print_section("Paths")
    print_key_values([(key, value) for key, value in report["paths"].items()])
    print_section("Configuration")
    print_key_values(
        [
            ("config file", report["config"]["file"]),
            ("env file", report["config"]["env_file"]),
            ("env prefix", report["config"]["prefix"]),
            ("known keys", report["config"]["keys"]),
        ]
    )
    print_section("Logging")
    print_key_values([(key, value) for key, value in report["logging"].items()])
    print_section("Signals")
    print_key_values([(key, value) for key, value in report["signals"].items()])
    print_section("Modules")
    modules = report["modules"]
    print_key_values([("total", modules["total"]), ("failed", modules["failed"] or "(none)")])
    if modules["modules"]:
        print_table(
            [
                [
                    item["name"],
                    item["version"],
                    item["state"],
                    item["origin"],
                    ", ".join(item["commands"]) or "-",
                ]
                for item in modules["modules"]
            ],
            headers=("name", "version", "state", "origin", "commands"),
        )
    if report["discovery"]["failures"]:
        write_line()
        write_status("module discovery problems:", tag=TAG_NOTE)
        for failure in report["discovery"]["failures"]:
            write_line(f"  {TAG_ERROR} {failure['name']} [{failure['stage']}] {failure['message']}")
    return int(ExitCode.ERROR) if modules["failed"] else int(ExitCode.SUCCESS)


def _modules_list_command(request: CliRequest) -> int:
    """List discovered modules."""
    chassis = _chassis(request)
    health = chassis.registry.health()
    if request.flag("as_json"):
        dump_json(health)
        return int(ExitCode.ERROR) if health["failed"] else int(ExitCode.SUCCESS)
    if not health["modules"]:
        write_status("no modules discovered yet", tag=TAG_NOTE)
        return int(ExitCode.SUCCESS)
    print_table(
        [
            [item["name"], item["version"], item["state"], item["origin"], item["description"]]
            for item in health["modules"]
        ],
        headers=("name", "version", "state", "origin", "description"),
    )
    write_line()
    write_line(f"{TAG_OK} {chassis.discovery.summary()}")
    for failure in chassis.discovery.failures:
        write_line(f"  {TAG_ERROR} {failure.describe()}")
    return int(ExitCode.ERROR) if health["failed"] else int(ExitCode.SUCCESS)


def _modules_show_command(request: CliRequest) -> int:
    """Show one (or all) modules in detail, initializing on demand."""
    chassis = _chassis(request)
    if chassis.state.value in ("new", "prepared"):
        chassis.initialize()
    name = request.option("name")
    records = [chassis.registry.require(str(name))] if name else list(chassis.registry.ordered())
    payload: list[dict[str, Any]] = []
    for record in records:
        detail = record.as_dict()
        health_hook = getattr(record.module, "health", None)
        if callable(health_hook):
            try:
                detail["health"] = health_hook()
            except Exception as exc:
                detail["health_error"] = f"{type(exc).__name__}: {exc}"
        detail["spec"] = record.spec.as_dict()
        payload.append(detail)
    if request.flag("as_json"):
        dump_json(payload)
        return int(ExitCode.SUCCESS)
    if not payload:
        write_status("no modules registered", tag=TAG_NOTE)
        return int(ExitCode.SUCCESS)
    for detail in payload:
        print_section(str(detail["name"]))
        print_key_values(
            [
                ("version", detail["version"]),
                ("description", detail["description"]),
                ("state", detail["state"]),
                ("origin", detail["origin"]),
                ("priority", detail["priority"]),
                ("source", detail["spec"]["source"]),
                ("entrypoint", detail["spec"]["entrypoint"]),
                ("requires core", detail["spec"]["requires_core"]),
                ("commands", detail["commands"] or "(none)"),
                ("timings", detail["timings_ms"]),
                ("error", detail["error"]),
                ("health", detail.get("health", "(not reported)")),
            ]
        )
    return int(ExitCode.SUCCESS)


def _config_show_command(request: CliRequest) -> int:
    """Print resolved configuration keys with provenance."""
    chassis = _chassis(request)
    prefix = str(request.option("key") or "").strip()
    rows = [
        row for row in chassis.config.describe_rows() if not prefix or row[0].startswith(prefix)
    ]
    if request.flag("as_json"):
        dump_json({row[0]: {"value": row[1], "source": row[2]} for row in rows})
        return int(ExitCode.SUCCESS)
    if not rows:
        write_status(f"no configuration keys start with {prefix!r}", tag=TAG_NOTE)
        return int(ExitCode.SUCCESS)
    print_table(rows, headers=("key", "value", "source"))
    write_line()
    write_line(f"{TAG_NOTE} precedence: CLI > environment > config file > built-in defaults")
    return int(ExitCode.SUCCESS)


def _config_file_command(request: CliRequest) -> int:
    """Print the discovered config and env file paths."""
    chassis = _chassis(request)
    print_key_values(
        [
            ("root", str(chassis.paths.root)),
            (
                "config file",
                str(chassis.paths.config_file) if chassis.paths.config_file else "(none)",
            ),
            ("env file", str(chassis.paths.env_file) if chassis.paths.env_file else "(none)"),
            ("env prefix", chassis.config.prefix),
        ]
    )
    return int(ExitCode.SUCCESS)


def _run_command(request: CliRequest) -> int:
    """Boot the chassis, run the lifecycle, then stop cleanly."""
    chassis = _chassis(request)
    duration_value = request.option("duration")
    duration = float(duration_value) if duration_value is not None else None
    chassis.ensure_initialized()
    if chassis.state.value == "failed":
        for name in chassis.registry.health()["failed"]:
            write_line(f"{TAG_ERROR} module {name} failed to initialize", stream=sys.stderr)
    chassis.start()
    try:
        if request.flag("once"):
            chassis.run_once()
        else:
            chassis.run_forever(duration=duration)
    except KeyboardInterrupt:  # pragma: no cover - handled by the signal path
        chassis.signals.request_shutdown()
    return int(chassis.shutdown())


def _chassis(request: CliRequest) -> Chassis:
    chassis = getattr(request.context, "chassis", None) or request.options.get("chassis")
    if chassis is None:
        raise RuntimeError("core commands require a chassis bound to the request context")
    return cast("Chassis", chassis)


def _interactivity(chassis: Chassis) -> dict[str, Any]:
    def is_tty(name: str) -> bool:
        stream = getattr(sys, name, None)
        try:
            return bool(stream and stream.isatty())
        except (ValueError, OSError):
            return False

    return {
        "stdin_tty": is_tty("stdin"),
        "stdout_tty": is_tty("stdout"),
        "interactive": chassis.config.get_bool("core.cli.interactive", True),
        "color": chassis.config.get_bool("core.cli.color", True),
        "confirmations": chassis.config.get_bool("core.cli.confirmations", True),
    }
