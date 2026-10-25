"""Console entry point: a thin client that never touches chassis internals."""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..config.layered import LayeredConfig
from ..config.loaders import normalize_alias_mapping
from ..constants import TAG_ERROR, TAG_NOTE, TRACEBACK_ENV_VAR, ExitCode
from ..exceptions import KorodexiosError
from ..kernel.chassis import Chassis, ChassisOptions, stdin_is_tty
from ..kernel.cli_contract import CliRequest
from ..kernel.ipc import DISCOVERY_NAME, IpcDiscovery
from ..paths import AppPaths
from ..version import format_version
from .commands import core_commands, render_rpc_reply, wizard_command
from .render import write_line
from .surface import HANDLER_ATTR, ArgparseSurface

LOG_LEVEL_CHOICES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
PROG_ALIASES = ("kdx", "korodexios", "korodexios-core")
DEFAULT_PROG = "kdx"


def console_prog() -> str:
    """Return the name the console was invoked as, so help matches the prompt.

    'kdx --help' should read 'usage: kdx ...'; a module run ('python -m ...') falls
    back to the canonical long name instead of showing '__main__.py'.
    """
    candidate = Path(sys.argv[0]).stem
    return candidate if candidate in PROG_ALIASES else DEFAULT_PROG


def global_flags() -> argparse.ArgumentParser:
    """Return a help-less parser holding only the global flags (usable as parent)."""
    parser = argparse.ArgumentParser(prog=DEFAULT_PROG, add_help=False)
    _add_global_flags(parser)
    return parser


def build_bootstrap_parser() -> argparse.ArgumentParser:
    """Parser accepting only the global flags (first parsing pass).

    Help is deliberately left to the root parser so 'kdx --help' lists every
    command contributed by modules, not just the global flags.
    """
    return argparse.ArgumentParser(
        prog=DEFAULT_PROG,
        description="korodexios-core chassis console.",
        add_help=False,
        parents=[global_flags()],
    )


def build_root_parser(flags: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
    """Parser used for the real dispatch pass (second parsing pass)."""
    return argparse.ArgumentParser(
        prog=console_prog(),
        description="korodexios-core chassis console: headless core, thin clients.",
        epilog=(
            "aliases: kdx (short), korodexios (canonical).\n"
            "Every command accepts the global flags above. Use --yes in CI to accept defaults."
        ),
        add_help=True,
        parents=[flags or global_flags()],
    )


def _add_global_flags(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("global options")
    group.add_argument(
        "--version",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Print the version and exit.",
    )
    group.add_argument(
        "--home",
        metavar="PATH",
        default=argparse.SUPPRESS,
        help="Chassis root directory.",
    )
    group.add_argument(
        "--config",
        metavar="FILE",
        default=argparse.SUPPRESS,
        help="Path to kdx.yml or korodexios.yml.",
    )
    group.add_argument(
        "--env-file",
        metavar="FILE",
        default=argparse.SUPPRESS,
        help="Path to a .env file.",
    )
    group.add_argument(
        "--log-level",
        metavar="LEVEL",
        choices=LOG_LEVEL_CHOICES,
        default=argparse.SUPPRESS,
        help="Console log level (diagnostics go to stderr).",
    )
    group.add_argument(
        "--log-json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Emit JSON log lines.",
    )
    group.add_argument(
        "--no-log-file",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Skip the rotating log file.",
    )
    group.add_argument(
        "--modules-dir",
        metavar="PATH",
        action="append",
        default=argparse.SUPPRESS,
        help="Extra plugin directory (repeatable).",
    )
    group.add_argument(
        "--ipc-host",
        metavar="HOST",
        default=argparse.SUPPRESS,
        help="Control-plane bind address (0.0.0.0 for LAN access) [127.0.0.1].",
    )
    group.add_argument(
        "--ipc-port",
        metavar="PORT",
        type=int,
        default=argparse.SUPPRESS,
        help="Control-plane port (0 for ephemeral) [8123].",
    )
    group.add_argument(
        "--auth",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Require the shared token on control-plane reads.",
    )
    group.add_argument(
        "--no-auth",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Allow anonymous /health and /api/v1/status reads.",
    )
    group.add_argument(
        "--ipc-token",
        metavar="TOKEN",
        default=argparse.SUPPRESS,
        help="Static shared secret for the control plane.",
    )
    group.add_argument(
        "--ipc-timeout",
        metavar="SECONDS",
        type=float,
        default=argparse.SUPPRESS,
        help="Client timeout for control-plane reads [2.0].",
    )
    group.add_argument(
        "--no-ipc",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Do not open the control plane in 'kdx run'.",
    )
    group.add_argument(
        "--allow-anonymous-shutdown",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Let unauthenticated clients POST /api/v1/shutdown.",
    )
    group.add_argument(
        "--tick-interval",
        metavar="SECONDS",
        type=float,
        default=argparse.SUPPRESS,
        help="Lifecycle tick interval [1.0].",
    )
    group.add_argument(
        "--stop-timeout",
        metavar="SECONDS",
        type=float,
        default=argparse.SUPPRESS,
        help="Budget for module stop() hooks [5.0].",
    )
    group.add_argument(
        "--logs-poll-interval",
        metavar="SECONDS",
        type=float,
        default=argparse.SUPPRESS,
        help="Follow poll interval for 'kdx logs' [0.5].",
    )
    group.add_argument(
        "--log-max-bytes",
        metavar="BYTES",
        type=int,
        default=argparse.SUPPRESS,
        help="Rotating log size limit [1048576].",
    )
    group.add_argument(
        "--log-backup-count",
        metavar="COUNT",
        type=int,
        default=argparse.SUPPRESS,
        help="Rotating log archives kept [5].",
    )
    group.add_argument(
        "--cli-width",
        metavar="COLUMNS",
        type=int,
        default=argparse.SUPPRESS,
        help="Terminal table width for the CLI [100].",
    )
    group.add_argument(
        "--confirmations",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Ask before destructive module actions.",
    )
    group.add_argument(
        "--no-confirmations",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Skip destructive confirmations.",
    )
    group.add_argument(
        "--no-autodiscover",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Disable module auto-discovery for this run.",
    )
    group.add_argument(
        "--enable-module",
        metavar="NAME",
        action="append",
        default=argparse.SUPPRESS,
        help="Force-enable one module (repeatable).",
    )
    group.add_argument(
        "--disable-module",
        metavar="NAME",
        action="append",
        default=argparse.SUPPRESS,
        help="Force-disable one module (repeatable).",
    )
    group.add_argument(
        "--name",
        metavar="NAME",
        default=argparse.SUPPRESS,
        help="Chassis display name (banner, doctor).",
    )
    group.add_argument(
        "--no-color",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Disable coloured output.",
    )
    group.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Log warnings and errors only.",
    )
    group.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Debug logging and tracebacks.",
    )
    group.add_argument(
        "-y",
        "--yes",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Accept defaults; never prompt (also auto-detected for non-TTY runs).",
    )
    group.add_argument(
        "--asyncio",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Run module coroutines on a chassis-managed asyncio loop.",
    )
    group.add_argument(
        "--no-asyncio",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Skip the asyncio loop; 'run_async' uses a private loop per call.",
    )
    group.add_argument(
        "--no-instance-lock",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Let a second daemon share this chassis root.",
    )
    group.add_argument(
        "--instance-lock",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Require the chassis-level instance lock (default).",
    )
    group.add_argument(
        "--remote",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Delegate the command to a live daemon; never boot a chassis here.",
    )
    group.add_argument(
        "--env-alias",
        metavar="NAME=KEY",
        action="append",
        default=argparse.SUPPRESS,
        help="Map a legacy environment variable onto a config key (repeatable).",
    )


def _cli_overrides(namespace: argparse.Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if getattr(namespace, "log_level", None):
        overrides["core.log.level"] = str(namespace.log_level)
    if getattr(namespace, "quiet", False):
        overrides["core.log.level"] = "WARNING"
    if getattr(namespace, "verbose", False):
        overrides["core.log.level"] = "DEBUG"
    if getattr(namespace, "log_json", False):
        overrides["core.log.json"] = True
    if getattr(namespace, "no_log_file", False):
        overrides["core.log.file_enabled"] = False
    if getattr(namespace, "no_color", False):
        overrides["core.log.color"] = False
        overrides["core.cli.color"] = False
    if getattr(namespace, "yes", False):
        overrides["core.cli.interactive"] = False
    ipc_host = getattr(namespace, "ipc_host", None)
    if ipc_host:
        overrides["core.ipc.host"] = str(ipc_host)
    ipc_port = getattr(namespace, "ipc_port", None)
    if ipc_port is not None:
        overrides["core.ipc.port"] = int(ipc_port)
    if getattr(namespace, "no_auth", False):
        overrides["core.ipc.auth"] = False
    if getattr(namespace, "auth", False):
        overrides["core.ipc.auth"] = True
    ipc_token = getattr(namespace, "ipc_token", None)
    if ipc_token:
        overrides["core.ipc.token"] = str(ipc_token)
    ipc_timeout = getattr(namespace, "ipc_timeout", None)
    if ipc_timeout is not None:
        overrides["core.ipc.timeout"] = float(ipc_timeout)
    if getattr(namespace, "no_ipc", False):
        overrides["core.ipc.enabled"] = False
    if getattr(namespace, "allow_anonymous_shutdown", False):
        overrides["core.ipc.allow_anonymous_shutdown"] = True
    tick_interval = getattr(namespace, "tick_interval", None)
    if tick_interval is not None:
        overrides["core.lifecycle.tick_interval"] = float(tick_interval)
    stop_timeout = getattr(namespace, "stop_timeout", None)
    if stop_timeout is not None:
        overrides["core.lifecycle.stop_timeout"] = float(stop_timeout)
    logs_poll = getattr(namespace, "logs_poll_interval", None)
    if logs_poll is not None:
        overrides["core.logs.poll_interval"] = float(logs_poll)
    log_max_bytes = getattr(namespace, "log_max_bytes", None)
    if log_max_bytes is not None:
        overrides["core.log.max_bytes"] = int(log_max_bytes)
    log_backup_count = getattr(namespace, "log_backup_count", None)
    if log_backup_count is not None:
        overrides["core.log.backup_count"] = int(log_backup_count)
    cli_width = getattr(namespace, "cli_width", None)
    if cli_width is not None:
        overrides["core.cli.width"] = int(cli_width)
    if getattr(namespace, "confirmations", False):
        overrides["core.cli.confirmations"] = True
    if getattr(namespace, "no_confirmations", False):
        overrides["core.cli.confirmations"] = False
    if getattr(namespace, "no_autodiscover", False):
        overrides["core.modules.autodiscover"] = False
    if getattr(namespace, "no_asyncio", False):
        overrides["core.async.enabled"] = False
    if getattr(namespace, "asyncio", False):
        overrides["core.async.enabled"] = True
    if getattr(namespace, "no_instance_lock", False):
        overrides["core.instance.lock"] = False
    if getattr(namespace, "instance_lock", False):
        overrides["core.instance.lock"] = True
    env_aliases = getattr(namespace, "env_alias", None)
    if env_aliases:
        overrides["core.env.aliases"] = normalize_alias_mapping(list(env_aliases))
    enabled_modules = getattr(namespace, "enable_module", None)
    if enabled_modules:
        overrides["core.modules.enabled"] = list(enabled_modules)
    disabled_modules = getattr(namespace, "disable_module", None)
    if disabled_modules:
        overrides["core.modules.disabled"] = list(disabled_modules)
    chassis_name = getattr(namespace, "name", None)
    if chassis_name:
        overrides["core.name"] = str(chassis_name)
    if getattr(namespace, "home", None):
        overrides["core.home"] = str(Path(str(namespace.home)).expanduser().resolve())
    return overrides


def _options(namespace: argparse.Namespace) -> ChassisOptions:
    def to_path(value: Any) -> Path | None:
        if not value:
            return None
        return Path(str(value)).expanduser().resolve()

    overrides = _cli_overrides(namespace)
    asyncio_enabled: bool | None = None
    if getattr(namespace, "asyncio", False):
        asyncio_enabled = True
    if getattr(namespace, "no_asyncio", False):
        asyncio_enabled = False
    instance_lock: bool | None = None
    if getattr(namespace, "instance_lock", False):
        instance_lock = True
    if getattr(namespace, "no_instance_lock", False):
        instance_lock = False
    return ChassisOptions(
        home=to_path(getattr(namespace, "home", None)),
        config_file=to_path(getattr(namespace, "config", None)),
        env_file=to_path(getattr(namespace, "env_file", None)),
        cli_overrides=overrides,
        instance_lock=instance_lock,
        asyncio_enabled=asyncio_enabled,
        extra_module_dirs=tuple(
            to_path(value) or Path(".") for value in getattr(namespace, "modules_dir", None) or []
        ),
    )


def _render_error(exc: KorodexiosError, *, verbose: bool) -> None:
    print(exc.render(), file=sys.stderr)
    if verbose or os.environ.get(TRACEBACK_ENV_VAR, "").strip():
        print("".join(traceback.format_exception(exc)).rstrip(), file=sys.stderr)


def _render_unexpected(exc: BaseException, *, verbose: bool) -> None:
    print(f"{TAG_ERROR} unexpected {type(exc).__name__}: {exc}", file=sys.stderr)
    if verbose or os.environ.get(TRACEBACK_ENV_VAR, "").strip():
        print("".join(traceback.format_exception(exc)).rstrip(), file=sys.stderr)
    else:
        print(f"{TAG_NOTE} rerun with -v for the full traceback", file=sys.stderr)


def _system_exit_code(request: SystemExit) -> int:
    code = request.code
    if code is None:
        return int(ExitCode.SUCCESS)
    if isinstance(code, int):
        return code
    return int(ExitCode.USAGE_ERROR)


def _launch_wizard(context: Any, surface: Any) -> int:
    """Open the interactive wizard when 'kdx' runs bare in a terminal."""
    from .ux import UserExperience
    from .wizard import RootWizard

    commands = list(surface.commands())
    if not commands:
        return int(ExitCode.SUCCESS)
    ux = UserExperience(context.config)
    return RootWizard(ux, context, commands, options={"surface": surface}).run()


def _remote_command_path(tokens: Sequence[str]) -> tuple[str, list[str]]:
    """Split the leftover argv into a command path and the tokens after it.

    'kdx --remote heartbeat status -n 5' reaches this as ['heartbeat', 'status',
    '-n', '5'] because the bootstrap parser already consumed the global flags.
    The path runs up to the first option-looking token; everything after it goes
    to the daemon, which parses it against the command's own declared arguments.
    A leading 'rpc' is the explicit delegation verb, not part of the path.
    """
    path: list[str] = []
    stop = len(tokens)
    for position, token in enumerate(tokens):
        if str(token).startswith("-"):
            stop = position
            break
        path.append(str(token))
    rest = [str(token) for token in tokens[stop:]]
    if path and path[0] == "rpc":
        path = path[1:]
    return " ".join(path), rest


def _remote_client(pre_args: argparse.Namespace) -> tuple[AppPaths, LayeredConfig]:
    """Resolve paths and configuration the way the chassis does, without booting one."""
    paths = AppPaths.discover(
        root=str(pre_args.home) if getattr(pre_args, "home", None) else None,
        config_file=str(pre_args.config) if getattr(pre_args, "config", None) else None,
        env_file=str(pre_args.env_file) if getattr(pre_args, "env_file", None) else None,
    )
    config = LayeredConfig.load(
        config_file=paths.config_file,
        local_config_file=paths.local_config_file,
        env_file=paths.env_file,
        environ=os.environ,
        cli_overrides=_cli_overrides(pre_args),
        runtime_defaults={"core.home": str(paths.root)},
    )
    return paths.with_state_dir(config.get_str("core.paths.state_dir")), config


def _delegate_remote(pre_args: argparse.Namespace, tokens: Sequence[str]) -> int:
    """Forward the command to a live daemon over its control plane; boot nothing."""
    command, argv_rest = _remote_command_path(tokens)
    if not command:
        print(f"{TAG_ERROR} --remote needs a command to delegate", file=sys.stderr)
        print(f"{TAG_NOTE} kdx --remote heartbeat status", file=sys.stderr)
        return int(ExitCode.USAGE_ERROR)
    paths, config = _remote_client(pre_args)
    discovery_path = Path(paths.state_dir) / DISCOVERY_NAME
    token = str(config.get_str("core.ipc.token", "") or "").strip()
    timeout = float(config.get_float("core.ipc.timeout", 2.0))
    discovery = IpcDiscovery.load(discovery_path)
    if discovery is not None and token and not discovery.token:
        discovery.token = token
    if discovery is None or not discovery.is_process_alive():
        print(f"{TAG_NOTE} no chassis daemon answering; run 'kdx run' first", file=sys.stderr)
        print(f"{TAG_NOTE} looked for {discovery_path}", file=sys.stderr)
        return int(ExitCode.ERROR)
    args: dict[str, Any] = {"argv": argv_rest} if argv_rest else {}
    try:
        code, payload = discovery.call(command, args, timeout=timeout)
    except OSError as exc:
        print(f"{TAG_ERROR} no control plane answering at {discovery.base_url()}", file=sys.stderr)
        print(f"{TAG_NOTE} {exc}", file=sys.stderr)
        return int(ExitCode.ERROR)
    render_rpc_reply(payload, command, as_json=False)
    return int(ExitCode.SUCCESS) if code < 400 else int(ExitCode.ERROR)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the console client and return the process exit code.

    A command handler's integer is returned verbatim, so a command may report a domain
    verdict - 4, 42, 77 - and the shell that called `kdx` sees exactly that number. Only the
    chassis' own failures are mapped to `ExitCode`: a chassis or CLI error is 1, a usage error
    2, a configuration error 3, SIGINT 130, and SIGTERM 143. Nothing between the handler and
    the process clamps the number, so `kdx` works as a gate in a deployment script.
    """
    argv = list(sys.argv[1:] if argv is None else argv)

    bootstrap = build_bootstrap_parser()
    try:
        pre_args, extra_args = bootstrap.parse_known_args(argv)
    except SystemExit as bootstrap_exit:
        return _system_exit_code(bootstrap_exit)

    if getattr(pre_args, "version", False):
        write_line(format_version())
        return int(ExitCode.SUCCESS)

    if getattr(pre_args, "remote", False):
        return _delegate_remote(pre_args, [str(item) for item in extra_args])

    chassis: Chassis | None = None
    try:
        chassis = Chassis.create(_options(pre_args))
        context = chassis.context
        context.registry = chassis.registry
        context.chassis = chassis

        flags = global_flags()
        parser = build_root_parser(flags)
        subparsers = parser.add_subparsers(dest="command", metavar="command")
        surface = ArgparseSurface(subparsers, context=context, parents=[flags])
        for command in core_commands(chassis):
            surface.add_command(command)
        surface.add_command(wizard_command())
        chassis.collect_cli(surface)

        try:
            namespace = parser.parse_args(argv)
        except SystemExit as parse_exit:
            return _system_exit_code(parse_exit)

        module_command_names = {command.name for command in chassis.registry.commands()}
        if getattr(namespace, "command", None) in module_command_names:
            chassis.ensure_initialized()
        handler = getattr(namespace, HANDLER_ATTR, None)
        if not callable(handler):
            if chassis.config.get_bool("core.cli.interactive", True) and stdin_is_tty():
                return _launch_wizard(context, surface)
            parser.print_help(sys.stdout)
            return int(ExitCode.SUCCESS)
        request = CliRequest(
            context=context,
            command=str(getattr(namespace, "command", "") or ""),
            namespace=namespace,
            options={"chassis": chassis, "surface": surface},
        )
        return int(handler(request))
    except KorodexiosError as exc:
        _render_error(exc, verbose=bool(getattr(pre_args, "verbose", False)))
        return int(exc.exit_code)
    except KeyboardInterrupt:
        print(f"{TAG_NOTE} interrupted", file=sys.stderr)
        return int(ExitCode.ABORTED)
    except SystemExit as handler_exit:  # pragma: no cover - argparse inside handlers
        return _system_exit_code(handler_exit)
    except Exception as exc:
        _render_unexpected(exc, verbose=bool(getattr(pre_args, "verbose", False)))
        return int(ExitCode.ERROR)
    finally:
        if chassis is not None and chassis.state.value != "stopped":
            try:
                chassis.shutdown()
            except Exception as exc:
                print(f"{TAG_ERROR} shutdown problem: {exc}", file=sys.stderr)
        if chassis is not None:
            chassis.signals.uninstall()


if __name__ == "__main__":  # pragma: no cover - console script wrapper
    raise SystemExit(main())
