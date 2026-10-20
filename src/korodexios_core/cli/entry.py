"""Console entry point: a thin client that never touches chassis internals."""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..constants import TAG_ERROR, TAG_NOTE, TRACEBACK_ENV_VAR, ExitCode
from ..exceptions import KorodexError
from ..kernel.chassis import Chassis, ChassisOptions, stdin_is_tty
from ..kernel.cli_contract import CliRequest
from ..version import format_version
from .commands import core_commands, wizard_command
from .render import write_line
from .surface import HANDLER_ATTR, ArgparseSurface

LOG_LEVEL_CHOICES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
PROG_ALIASES = ("kdx", "korodexios", "korodex", "korodexios-core")
DEFAULT_PROG = "korodex"


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
            "aliases: kdx (short), korodexios (canonical), korodex (compatibility).\n"
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
        help="Path to korodex.yml or korodex.json.",
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
    if getattr(namespace, "home", None):
        overrides["core.home"] = str(Path(str(namespace.home)).expanduser().resolve())
    return overrides


def _options(namespace: argparse.Namespace) -> ChassisOptions:
    def to_path(value: Any) -> Path | None:
        if not value:
            return None
        return Path(str(value)).expanduser().resolve()

    return ChassisOptions(
        home=to_path(getattr(namespace, "home", None)),
        config_file=to_path(getattr(namespace, "config", None)),
        env_file=to_path(getattr(namespace, "env_file", None)),
        cli_overrides=_cli_overrides(namespace),
        extra_module_dirs=tuple(
            to_path(value) or Path(".") for value in getattr(namespace, "modules_dir", None) or []
        ),
    )


def _render_error(exc: KorodexError, *, verbose: bool) -> None:
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
    """Open the interactive wizard when 'korodex' runs bare in a terminal."""
    from .ux import UserExperience
    from .wizard import RootWizard

    commands = list(surface.commands())
    if not commands:
        return int(ExitCode.SUCCESS)
    ux = UserExperience(context.config)
    return RootWizard(ux, context, commands, options={"surface": surface}).run()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the console client and return the process exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)

    bootstrap = build_bootstrap_parser()
    try:
        pre_args, _ = bootstrap.parse_known_args(argv)
    except SystemExit as bootstrap_exit:
        return _system_exit_code(bootstrap_exit)

    if getattr(pre_args, "version", False):
        write_line(format_version())
        return int(ExitCode.SUCCESS)

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
    except KorodexError as exc:
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
