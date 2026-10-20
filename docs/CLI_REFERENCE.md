# CLI Reference

Every command is a thin client of the core: the same behaviour is available through
the Python API. All examples use the primary alias `kdx`.

## Invocation

```bash
kdx <command> [subcommand] [flags]     # primary short alias
korodexios <command>                   # canonical name
korodex <command>                      # kept for backward compatibility
```

Help text follows the alias you invoke (`usage: kdx ...`), so screenshots match the
docs. `kdx --help` lists every command, including the ones modules contributed.

## Global options

| Option | Effect | Default |
| --- | --- | --- |
| `--version` | print chassis and core API version, exit | - |
| `--home PATH` | chassis root directory | discovered root |
| `--config FILE` | explicit config file (`kdx.yml`, `korodexios.yml`, `korodex.yml`) | first candidate |
| `--env-file FILE` | explicit `.env` path | `<root>/.env` |
| `--log-level LEVEL` | console log level (diagnostics go to stderr) | `INFO` |
| `--log-json` | emit JSON log lines | off |
| `--no-log-file` | skip the rotating log file | file enabled |
| `--modules-dir PATH` | extra plugin directory (repeatable) | - |
| `--ipc-host HOST` | control-plane bind address (`0.0.0.0` for LAN) | `127.0.0.1` |
| `--ipc-port PORT` | control-plane port (`0` = ephemeral) | `8123` |
| `--auth` / `--no-auth` | require or allow anonymous control-plane reads | auth on |
| `--ipc-token TOKEN` | static shared secret for the control plane | empty (ephemeral) |
| `--no-color` | disable coloured output | colour on a TTY |
| `-q, --quiet` | warnings and errors only | - |
| `-v, --verbose` | debug logging and tracebacks | - |
| `-y, --yes` | accept defaults, never prompt (auto-detected for non-TTY) | - |

## `kdx run`

```bash
kdx run                       # boot and run until interrupted or stopped remotely
kdx run --once                # publish one tick and stop: the CI-safe smoke test
kdx run --duration 60         # stop automatically after 60 seconds
kdx run --host 0.0.0.0 --port 8123    # LAN-reachable control plane
```

Exit codes: `0` clean or operator stop, `130` SIGINT, `143` SIGTERM.

## `kdx status`

```bash
kdx status                    # read the local discovery file, print the live report
kdx status --json             # machine-readable report
kdx status --target http://10.50.50.198:8123 --token SECRET   # remote daemon
kdx status --host 127.0.0.1 --port 8123                        # explicit local endpoint
```

Reports endpoint, auth policy, shutdown policy, the source of the endpoint, chassis
state, uptime, ticks, and every module's health. A 401 prints a hint to pass `--token`
or set `KDX_CORE__IPC__TOKEN`.

## `kdx doctor`

```bash
kdx doctor                    # environment, paths, config provenance, modules, IPC
kdx doctor --json             # the same report as JSON
```

Works without a running daemon - it boots the chassis in-process, so it is the first
command to run when something looks misconfigured.

## `kdx logs`

```bash
kdx logs -n 50                # last 50 lines (default)
kdx logs -f                   # follow until Ctrl-C
kdx logs -f --timeout 5       # follow for 5 seconds (CI friendly)
kdx logs --raw                # only log lines, no decorations
kdx logs --clear              # truncate the log file (asks first; --yes accepts)
```

No POSIX `tail` required: the file lives under the configured log directory and is
resolved from configuration, not a hardcoded path.

## `kdx modules`

```bash
kdx modules list              # name, version, state, origin, dependencies, description
kdx modules list --json
kdx modules show <name>       # spec plus live health for one module (or all)
kdx modules create <name> --path src/<pkg>/modules [--title TEXT] [--force] [--json]
```

`create` scaffolds `__init__.py`, `module.py`, and `korodex_module.toml` - zero stubs,
immediately runnable.

## `kdx config`

```bash
kdx config show                       # every resolved key with its source
kdx config show --key core.log        # only keys under a prefix
kdx config show --json                # mapping as JSON
kdx config file                       # which file won, and the local override if any
```

Sources: `built-in default`, `config file`, `local override file`, `environment`,
`CLI override`.

## `kdx init`

```bash
kdx init my_app                       # interactive wizard (name, ecosystem, author)
kdx init my_app --path ../my_app --minimal --yes
kdx init my_app --ecosystem fullstack # adds web/ static client and web-ui skill
kdx init my_app --force               # write into a directory that already holds files
```

The seed ships `kdx.yml`, `.env.example`, `Makefile`, `README.md`, `AGENTS.md`,
`docs/AGENTS_GUIDE.md`, `docs/ARCHITECTURE.md`, `.agents/skills/`, a first module, and
smoke tests. It passes `make check` on arrival.

## `kdx wizard`

Interactive menu over the same declarative command tree the modules register. Numbered
choices, `[default]` in brackets, validation loops, `[OK] [?] [!] [i]` tags. Non-TTY
runs and `--yes` skip the prompts.

## `kdx version`

Prints the chassis version, the core API version, and the Python runtime line.

## Built-in module commands

```bash
kdx system info | diagnostics | disk      # host OS, CPU, memory, disk
kdx heartbeat status [--json]             # counters and the configured interval
kdx backup plan | run | configure         # dry-run, execute, or reconfigure
```

Each is registered by its module through `register_cli`, so argparse and the wizard
both know about them.

## LAN and auth options

```bash
kdx run --host 0.0.0.0 --port 8123        # bind the control plane to all interfaces
kdx run --no-auth                         # anonymous /health and /api/v1/status
kdx run --ipc-token my-static-lan-key     # stable secret across restarts
kdx status --target http://HOST:8123 --token my-static-lan-key
```

| Setting | Config key | Env | CLI |
| --- | --- | --- | --- |
| Bind address | `core.ipc.host` | `KDX_CORE__IPC__HOST` | `--ipc-host` / `run --host` |
| Port | `core.ipc.port` | `KDX_CORE__IPC__PORT` | `--ipc-port` / `run --port` |
| Read auth | `core.ipc.auth` | `KDX_CORE__IPC__AUTH` | `--auth` / `--no-auth` |
| Shared secret | `core.ipc.token` | `KDX_CORE__IPC__TOKEN` | `--ipc-token` |
| Anonymous stop | `core.ipc.allow_anonymous_shutdown` | `KDX_CORE__IPC__ALLOW_ANONYMOUS_SHUTDOWN` | - |

A wildcard bind (`0.0.0.0`, `::`) is announced as `127.0.0.1` in `var/chassis.ipc`, so
local clients need no change; remote clients pass `--target`.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | success, or an operator-requested stop |
| `1` | handled failure (chassis or CLI error with a hint) |
| `2` | usage error (argparse) |
| `3` | configuration error |
| `130` | SIGINT |
| `143` | SIGTERM |

## Scripting rules

- Non-TTY runs never prompt; `--yes` also forces defaults in a TTY.
- `--json` is available on `doctor`, `status`, `modules list`, `modules show`,
  `modules create`, `config show`, and the module status commands.
- Diagnostics go to stderr, UI output to stdout: redirect stderr away and stdout stays
  parseable.
- `--home PATH` makes any command runnable from anywhere; it beats `KDX_HOME`.
