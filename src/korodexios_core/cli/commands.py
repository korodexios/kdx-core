"""Core commands contributed by the chassis itself, not by any module."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ..constants import TAG_ERROR, TAG_NOTE, TAG_OK, ExitCode
from ..kernel.cli_contract import CliArgument, CliCommand, CliRequest
from ..kernel.ipc import (
    DEFAULT_PORT,
    DISCOVERY_NAME,
    STATUS_PATH,
    IpcDiscovery,
    IpcError,
)
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
            name="logs",
            help="Show, follow, truncate, or rotate the chassis log file.",
            args=(
                CliArgument(
                    ("-n", "--lines"),
                    dest="lines",
                    metavar="N",
                    type="int",
                    help="print the last N lines [50]",
                ),
                CliArgument(
                    ("-f", "--follow"),
                    dest="follow",
                    action="store_true",
                    help="stream newly appended lines until Ctrl-C",
                ),
                CliArgument(
                    ("--clear",),
                    dest="clear",
                    action="store_true",
                    help="truncate the log file (asks first)",
                ),
                CliArgument(
                    ("--rotate",),
                    dest="rotate",
                    action="store_true",
                    help="archive the log file and start a fresh one",
                ),
                CliArgument(
                    ("--raw",),
                    dest="raw",
                    action="store_true",
                    help="print only log lines, no decorations",
                ),
                CliArgument(
                    ("--timeout",),
                    dest="timeout",
                    metavar="SECONDS",
                    type="float",
                    help="stop --follow after SECONDS (CI friendly)",
                ),
            ),
            handler=_logs_command,
            group="core",
        ),
        CliCommand(
            name="init",
            help="Scaffold a new project from the chassis template.",
            args=(
                CliArgument(
                    ("name",),
                    nargs="?",
                    metavar="NAME",
                    help="project name; prompted when omitted",
                ),
                CliArgument(
                    ("--path",),
                    dest="path",
                    metavar="DIR",
                    help="target directory (default: ./<name>)",
                ),
                CliArgument(
                    ("--author",),
                    dest="author",
                    metavar="TEXT",
                    help="author or owner [Korodexios]",
                ),
                CliArgument(
                    ("--ecosystem",),
                    dest="ecosystem",
                    metavar="CHOICE",
                    help="python | fullstack",
                ),
                CliArgument(
                    ("--minimal",),
                    dest="minimal",
                    action="store_true",
                    help="bare chassis: no example modules",
                ),
                CliArgument(
                    ("--force",),
                    dest="force",
                    action="store_true",
                    help="write into a directory that already holds files",
                ),
                CliArgument(
                    ("--chassis-source",),
                    dest="chassis_source",
                    metavar="CHOICE",
                    help="how the project resolves the chassis: git | index [git]",
                ),
            ),
            handler=_init_command,
            group="core",
        ),
        CliCommand(
            name="status",
            help="Query a running chassis over its control plane (local or remote).",
            args=(
                CliArgument(
                    ("--json",),
                    dest="as_json",
                    action="store_true",
                    help="Emit the live report as JSON.",
                ),
                CliArgument(
                    ("--target",),
                    dest="target",
                    metavar="URL",
                    help="query http://host:port directly, skipping the local discovery file",
                ),
                CliArgument(
                    ("--host",),
                    dest="target_host",
                    metavar="HOST",
                    help="query this host (with --port) instead of the local endpoint",
                ),
                CliArgument(
                    ("--port",),
                    dest="target_port",
                    metavar="PORT",
                    type="int",
                    help="port of --host [core.ipc.port]",
                ),
                CliArgument(
                    ("--token",),
                    dest="target_token",
                    metavar="TOKEN",
                    help="shared token for a remote endpoint with auth enabled",
                ),
            ),
            handler=_status_command,
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
                    name="create",
                    help="Scaffold a new module package (asks for a name when omitted).",
                    args=(
                        CliArgument(
                            ("name",), nargs="?", help="Module name, for example backup_engine."
                        ),
                        CliArgument(
                            ("--path",),
                            dest="path",
                            metavar="DIR",
                            help="Directory to create the module in.",
                        ),
                        CliArgument(
                            ("--title",),
                            dest="title",
                            metavar="TEXT",
                            help="One-line module description.",
                        ),
                        CliArgument(
                            ("--force",),
                            dest="force",
                            action="store_true",
                            help="Replace an existing module directory.",
                        ),
                        CliArgument(
                            ("--json",),
                            dest="as_json",
                            action="store_true",
                            help="Emit the created files as JSON.",
                        ),
                    ),
                    handler=_modules_create_command,
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
            help="Show, prove, and validate the resolved configuration.",
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
                CliCommand(
                    name="check",
                    help="Report keys nothing reads; exit 3 when a key is a typo.",
                    args=(
                        CliArgument(
                            ("--json",),
                            dest="as_json",
                            action="store_true",
                            help="Emit the report as JSON.",
                        ),
                    ),
                    handler=_config_check_command,
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
                CliArgument(
                    ("--host",),
                    dest="ipc_host",
                    metavar="HOST",
                    help="bind the control plane here (0.0.0.0 for LAN access)",
                ),
                CliArgument(
                    ("--port",),
                    dest="ipc_port",
                    metavar="PORT",
                    type="int",
                    help="control-plane port (0 for an ephemeral port)",
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
    ipc = dict(report.get("ipc") or {})
    memory = dict(report.get("memory") or {})
    print_section("Control plane")
    print_key_values(
        [
            ("running", format_bool(bool(ipc.get("running")))),
            (
                "endpoint",
                f"{ipc.get('host', '-')}:{ipc.get('port', '-')}" if ipc.get("running") else "-",
            ),
            ("requests served", ipc.get("requests", 0)),
            ("discovery file", report.get("ipc_file", "-")),
            ("process memory", memory.get("rss", "-")),
        ]
    )
    print_section("Modules")
    modules = report["modules"]
    dependencies = chassis.registry.dependency_report()
    print_key_values(
        [
            ("total", modules["total"]),
            ("failed", modules["failed"] or "(none)"),
            ("unknown dependencies", ", ".join(dependencies["missing"]) or "(none)"),
            ("dependency cycles", ", ".join(dependencies["cycles"]) or "(none)"),
        ]
    )
    if modules["modules"]:
        print_table(
            [
                [
                    item["name"],
                    item["version"],
                    item["state"],
                    item["origin"],
                    ", ".join(item["commands"]) or "-",
                    ", ".join(item.get("depends_on", [])) or "-",
                ]
                for item in modules["modules"]
            ],
            headers=("name", "version", "state", "origin", "commands", "depends"),
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
            [
                item["name"],
                item["version"],
                item["state"],
                item["origin"],
                ", ".join(item.get("depends_on", [])) or "-",
                item["description"],
            ]
            for item in health["modules"]
        ],
        headers=("name", "version", "state", "origin", "depends", "description"),
    )
    write_line()
    write_line(f"{TAG_OK} {chassis.discovery.summary()}")
    for failure in chassis.discovery.failures:
        write_line(f"  {TAG_ERROR} {failure.describe()}")
    return int(ExitCode.ERROR) if health["failed"] else int(ExitCode.SUCCESS)


def _logs_command(request: CliRequest) -> int:
    """Show, follow, truncate, or rotate the chassis log file."""
    import signal
    import time

    from ..logging.engine import (
        DEFAULT_TAIL_LINES,
        LogTailer,
        rotate_log,
        tail_lines,
        truncate_log,
    )
    from .ux import UserExperience

    chassis = _chassis(request)
    ux = UserExperience(chassis.config)
    log_path = Path(chassis.paths.log_dir) / chassis.config.get_str("core.log.file", "kdx.log")
    raw = bool(request.flag("raw"))
    follow = bool(request.flag("follow"))
    lines_value = request.option("lines")
    count = int(str(lines_value)) if lines_value is not None else DEFAULT_TAIL_LINES
    if count < 1:
        write_line(f"{TAG_ERROR} --lines must be at least 1, got {count}")
        write_line(f"{TAG_NOTE} kdx logs -n 200")
        return int(ExitCode.ERROR)
    timeout_value = request.option("timeout")
    timeout = float(str(timeout_value)) if timeout_value is not None else None

    def emit(line: str) -> None:
        write_line(line)

    def note(message: str) -> None:
        if not raw:
            write_line(f"{TAG_NOTE} {message}")

    if request.flag("rotate") and request.flag("clear"):
        write_line(f"{TAG_ERROR} --rotate and --clear are mutually exclusive")
        note("kdx logs --rotate archives the file; --clear empties it")
        return int(ExitCode.USAGE_ERROR)

    if not log_path.is_file():
        reason = (
            "file logging is disabled (core.log.file_enabled=false)"
            if not chassis.config.get_bool("core.log.file_enabled", True)
            else "the chassis has not written a log line yet"
        )
        write_line(f"{TAG_NOTE} no log file found at {log_path}")
        note(reason)
        return int(ExitCode.SUCCESS)

    if request.flag("rotate"):
        proceed = True
        if ux.is_interactive():
            proceed = ux.confirm(f"Rotate log file {log_path}?", default=False)
        if not proceed:
            note("log file left alone")
            return int(ExitCode.SUCCESS)
        archive, archived = rotate_log(
            log_path, backup_count=chassis.config.get_int("core.log.backup_count", 5)
        )
        write_status(f"log file rotated to {archive.name} ({archived} bytes archived)")
        return int(ExitCode.SUCCESS)

    if request.flag("clear"):
        proceed = True
        if ux.is_interactive():
            proceed = ux.confirm(f"Truncate log file {log_path}?", default=False)
        if not proceed:
            note("log file left alone")
            return int(ExitCode.SUCCESS)
        removed = truncate_log(log_path)
        write_status(f"log file truncated ({removed} bytes freed)")
        return int(ExitCode.SUCCESS)

    if follow:
        poll = chassis.config.get_float("core.logs.poll_interval", 0.5)
        tailer = LogTailer(log_path, poll_interval=poll)
        for line in tail_lines(log_path, min(count, 10)):
            emit(line)
        tailer.seek_end()
        deadline = time.monotonic() + timeout if timeout is not None and timeout > 0 else None
        note(f"following {log_path} (Ctrl-C to stop)")
        interrupted = False
        try:
            while not chassis.signals.shutdown_requested:
                for line in tailer.read_new():
                    emit(line)
                if deadline is not None and time.monotonic() >= deadline:
                    break
                tailer.wait()
        except KeyboardInterrupt:
            interrupted = True
        # The chassis owns the signal handlers, so Ctrl-C arrives as a shutdown
        # request rather than a KeyboardInterrupt; honour the recorded signal.
        signalled = chassis.signals.shutdown_requested
        stop_signal = chassis.signals.signal_number if signalled else None
        if interrupted or signalled:
            note(f"stopped following {log_path.name}")
            return int(ExitCode.from_signal(stop_signal or signal.SIGINT))
        return int(ExitCode.SUCCESS)

    if not raw:
        note(f"{log_path} ({log_path.stat().st_size} bytes)")
    for line in tail_lines(log_path, count):
        emit(line)
    return int(ExitCode.SUCCESS)


def _init_command(request: CliRequest) -> int:
    """Scaffold a brand-new chassis project from a template."""
    from .init_project import (
        CHASSIS_SOURCES,
        ECOSYSTEMS,
        ProjectError,
        init_project,
        prompt_project,
    )
    from .ux import UserExperience

    chassis = _chassis(request)
    ux = UserExperience(chassis.config)
    try:
        project = prompt_project(
            ux,
            name=str(request.option("name") or "").strip(),
            author=str(request.option("author") or "").strip(),
            ecosystem=str(request.option("ecosystem") or "").strip(),
            minimal=bool(request.flag("minimal")),
            chassis_source=str(request.option("chassis_source") or "").strip(),
        )
        target_text = str(request.option("path") or "").strip()
        target = Path(target_text).expanduser() if target_text else Path.cwd() / project.package
        result = init_project(target, project, force=bool(request.flag("force")))
    except ProjectError as exc:
        write_line(f"{TAG_ERROR} {exc}")
        if exc.hint:
            write_line(f"{TAG_NOTE} {exc.hint}")
        return int(ExitCode.ERROR)
    write_status(f"project {project.name} created")
    print_key_values(
        [
            ("root", str(result.root)),
            ("package", project.package),
            ("ecosystem", ECOSYSTEMS[project.ecosystem]),
            ("modules", "(bare chassis)" if project.minimal else "app"),
            ("files written", len(result.files)),
            ("chassis", f"korodexios-core {project.core_version}"),
            ("chassis dependency", CHASSIS_SOURCES[project.chassis_source]),
        ]
    )
    write_line(
        f"{TAG_NOTE} next: cd {result.root} && pip install -e '.[dev]' && {project.script} run"
    )
    return int(ExitCode.SUCCESS)


def _status_command(request: CliRequest) -> int:
    """Query a running chassis through its control plane, locally or over the LAN."""
    chassis = _chassis(request)
    timeout = float(chassis.config.get_float("core.ipc.timeout", 2.0))
    default_port = int(chassis.config.get_int("core.ipc.port", 8123)) or DEFAULT_PORT
    discovery_path = Path(chassis.paths.state_dir) / DISCOVERY_NAME
    token = str(
        request.option("target_token") or chassis.config.get_str("core.ipc.token", "") or ""
    ).strip()
    target = str(request.option("target") or "").strip()
    target_host = str(request.option("target_host") or "").strip()
    target_port = request.option("target_port")
    remote = bool(target or target_host)
    try:
        discovery: IpcDiscovery | None
        if target:
            discovery = IpcDiscovery.remote(target, default_port=default_port, token=token)
        elif target_host:
            address = target_host
            if target_port is not None:
                address = f"{target_host}:{int(target_port)}"
            discovery = IpcDiscovery.remote(address, default_port=default_port, token=token)
        else:
            discovery = IpcDiscovery.load(discovery_path)
    except IpcError as exc:
        write_line(f"{TAG_ERROR} {exc}")
        if exc.hint:
            write_line(f"{TAG_NOTE} {exc.hint}")
        return int(ExitCode.ERROR)
    if discovery is None or (not remote and not discovery.is_process_alive()):
        if request.flag("as_json"):
            dump_json({"running": False, "discovery_file": str(discovery_path)})
        else:
            write_line(f"{TAG_NOTE} chassis daemon is not running")
            write_line(f"{TAG_NOTE} looked for {discovery_path}")
        return int(ExitCode.SUCCESS)
    endpoint = discovery.base_url()
    try:
        code, payload = discovery.request(STATUS_PATH, timeout=timeout)
    except OSError as exc:
        write_line(f"{TAG_ERROR} no control plane answering at {endpoint}")
        write_line(f"{TAG_NOTE} {exc}")
        return int(ExitCode.ERROR)
    if code in (401, 403):
        write_line(f"{TAG_ERROR} the daemon rejected this client ({endpoint} requires a token)")
        if remote:
            write_line(
                f"{TAG_NOTE} pass --token TOKEN, or set core.ipc.token / KDX_CORE__IPC__TOKEN"
            )
        else:
            write_line(f"{TAG_NOTE} start 'kdx run' again so {discovery_path} is refreshed")
        return int(ExitCode.ERROR)
    if code != 200 or not isinstance(payload, dict):
        write_line(f"{TAG_ERROR} the daemon answered HTTP {code}")
        return int(ExitCode.ERROR)
    if request.flag("as_json"):
        dump_json(payload)
        return int(ExitCode.SUCCESS)
    app = dict(payload.get("app") or {})
    modules = dict(payload.get("modules") or {})
    bus = dict(payload.get("bus") or {})
    memory = dict(payload.get("memory") or {})
    ipc = dict(payload.get("ipc") or {})
    print_section("Chassis")
    print_key_values(
        [
            ("state", app.get("state", "-")),
            ("uptime", f"{float(app.get('uptime_seconds', 0.0) or 0.0):.1f} s"),
            ("pid", ipc.get("pid", "-")),
            ("endpoint", endpoint),
            ("auth", "token required" if ipc.get("auth") else "anonymous reads"),
            (
                "shutdown",
                "anonymous"
                if ipc.get("allow_anonymous_shutdown") and not ipc.get("auth")
                else "token required",
            ),
            (
                "source",
                f"target {endpoint}" if remote else f"discovery file {discovery_path}",
            ),
        ]
    )
    print_section("Modules")
    rows = [
        [item.get("name"), item.get("state"), ", ".join(item.get("commands") or []) or "-"]
        for item in list(modules.get("modules") or [])
    ]
    print_key_values(
        [("total", modules.get("total", 0)), ("failed", modules.get("failed") or "(none)")]
    )
    if rows:
        print_table(rows, headers=("name", "state", "commands"))
    print_section("Event bus")
    print_key_values(
        [
            ("published", bus.get("published", 0)),
            ("delivered", bus.get("delivered", 0)),
            ("handler errors", bus.get("errors", 0)),
            ("slow handlers", bus.get("slow_handlers", 0)),
            ("subscriptions", bus.get("subscriptions", 0)),
        ]
    )
    print_section("Memory")
    print_key_values([("resident", memory.get("rss", "-")), ("source", memory.get("source", "-"))])
    return int(ExitCode.SUCCESS)


def _modules_create_command(request: CliRequest) -> int:
    """Scaffold a new module package and report what landed on disk."""
    from .scaffold import ScaffoldError, scaffold_module, validate_module_name
    from .ux import UserExperience

    chassis = _chassis(request)
    ux = UserExperience(chassis.config)
    raw_name = str(request.option("name") or "").strip()
    if not raw_name:
        raw_name = ux.ask_text("Module name", default="", required=True, example="backup_engine")
    try:
        name = validate_module_name(raw_name)
    except ScaffoldError as exc:
        write_line(f"{TAG_ERROR} {exc}")
        if exc.hint:
            write_line(f"{TAG_NOTE} {exc.hint}")
        return int(ExitCode.ERROR)
    raw_path = str(request.option("path") or "").strip()
    parent = Path(raw_path).expanduser() if raw_path else Path(chassis.paths.builtin_modules_dir)
    if not parent.is_absolute():
        parent = Path.cwd() / parent
    title = str(request.option("title") or "").strip()
    force = bool(request.flag("force"))
    directory = parent / name
    exists = directory.exists() and any(directory.iterdir())
    if exists and not force:
        if not ux.interactive:
            write_line(f"{TAG_ERROR} {directory} already holds a module")
            write_line(f"{TAG_NOTE} pass --force to replace its files")
            return int(ExitCode.ERROR)
        if not ux.confirm(f"Replace existing module {name}?", default=False):
            write_line(f"{TAG_NOTE} nothing written")
            return int(ExitCode.SUCCESS)
    try:
        result = scaffold_module(
            parent, name, title=title, force=force, project_root=chassis.paths.root
        )
    except ScaffoldError as exc:
        write_line(f"{TAG_ERROR} {exc}")
        if exc.hint:
            write_line(f"{TAG_NOTE} {exc.hint}")
        return int(ExitCode.ERROR)
    rows: list[tuple[str, Any]] = [
        ("directory", str(result.directory)),
        ("files", ", ".join(item.name for item in result.created)),
    ]
    if result.test_file is not None:
        rows.append(("test", str(result.test_file)))
    if request.flag("as_json"):
        dump_json({"module": name, **result.as_dict()})
        return int(ExitCode.SUCCESS)
    write_status(f"module {name} created")
    print_key_values(rows)
    builtin = Path(chassis.paths.builtin_modules_dir)
    if result.directory.parent.resolve() != builtin.resolve():
        write_line(
            f"{TAG_NOTE} discovery needs {result.directory.parent} in core.modules.extra_paths "
            "or passed as --modules-dir"
        )
    write_line(f"{TAG_NOTE} next: kdx modules list")
    return int(ExitCode.SUCCESS)


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


def _config_check_command(request: CliRequest) -> int:
    """Report configuration keys the chassis does not read."""
    chassis = _chassis(request)
    unknown = chassis.config.unknown_keys(chassis.registry.names())
    project_keys = list(chassis.config.project_keys())
    report = {
        "config_file": (
            str(chassis.config.config_file) if chassis.config.config_file else None
        ),
        "checked": len(chassis.config.keys()),
        "unknown": [
            {
                "key": key,
                "origin": chassis.config.origin_of(key),
                "suggestion": hint,
                "advice": _advice_for(hint),
            }
            for key, hint in unknown
        ],
        "project_keys": project_keys,
    }
    if request.flag("as_json"):
        dump_json(report)
        return int(ExitCode.CONFIG_ERROR) if unknown else int(ExitCode.SUCCESS)
    print_key_values(
        [
            ("config file", report["config_file"] or "(none: defaults only)"),
            ("keys checked", str(report["checked"])),
            ("unknown keys", str(len(unknown))),
            ("project keys", ", ".join(project_keys) or "(none)"),
        ]
    )
    for key, hint in unknown:
        write_status(
            f"unknown configuration key {key} [{chassis.config.origin_of(key)}]"
            + _advice_for(hint),
            tag=TAG_ERROR,
        )
    if unknown:
        write_status(
            f"{len(unknown)} configuration keys are not read by the chassis",
            tag=TAG_NOTE,
        )
        return int(ExitCode.CONFIG_ERROR)
    write_status("configuration is understood", tag=TAG_OK)
    return int(ExitCode.SUCCESS)


def _advice_for(hint: str) -> str:
    """Return the parenthetical hint for an unknown configuration key."""
    if not hint:
        return ""
    if hint.startswith("core."):
        return f" (did you mean {hint}?)"
    return f" (module {hint.partition('.')[2]} is discovered)"


def _run_command(request: CliRequest) -> int:
    """Boot the chassis, run the lifecycle, then stop cleanly."""
    chassis = _chassis(request)
    duration_value = request.option("duration")
    duration = float(duration_value) if duration_value is not None else None
    overrides: dict[str, Any] = {}
    host_value = request.option("ipc_host")
    if host_value:
        overrides["core.ipc.host"] = str(host_value)
    port_value = request.option("ipc_port")
    if port_value is not None:
        overrides["core.ipc.port"] = int(port_value)
    if overrides:
        chassis.config = chassis.config.with_cli_overrides(overrides)
    chassis.ensure_initialized()
    if chassis.state.value == "failed":
        for name in chassis.registry.health()["failed"]:
            write_line(f"{TAG_ERROR} module {name} failed to initialize", stream=sys.stderr)
    chassis.start(ipc=not request.flag("once"))
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
