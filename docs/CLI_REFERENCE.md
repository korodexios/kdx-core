# CLI Reference

Every command is a thin client of the core: the same behaviour is available through
the Python API. All examples use the primary alias `kdx`.

## Invocation

```bash
kdx <command> [subcommand] [flags]     # primary short alias
korodexios <command>                   # canonical name
```

Help text follows the alias you invoke (`usage: kdx ...`), so screenshots match the
docs. `kdx --help` lists every command, including the ones modules contributed.

## Distribution and installation

| Install | Command | Notes |
| --- | --- | --- |
| pip in a venv | `pip install korodexios-core` | from an index, once published |
| uv | `uv pip install korodexios-core` | the same wheel |
| pipx / uv tool | `pipx install korodexios-core` | the two console scripts land on PATH |
| git bootstrap | `pip install "korodexios-core @ git+ssh://git@github.com/korodexios/kdx-core.git@v0.2.2"` | the documented path before an index publish |
| development | `pip install -e ".[dev]"` | the chassis repository itself |

The wheel carries `py.typed` and every built-in module manifest
(`kdx_module.toml`), so a plugin installed into the same environment can read the
manifests it needs. The sdist carries `docs/`, `README.md`, `AGENTS.md`, `llms.txt`,
`.agents/skills/`, `kdx.yml`, and the tests, so an sdist install can run `make check`.

## Global options

| Option | Effect | Default |
| --- | --- | --- |
| `--version` | print chassis and core API version, exit | - |
| `--home PATH` | chassis root directory | discovered root |
| `--config FILE` | explicit config file (`kdx.yml`, `korodexios.yml`) | first candidate |
| `--env-file FILE` | explicit `.env` path | `<root>/.env` |
| `--log-level LEVEL` | console log level (diagnostics go to stderr) | `INFO` |
| `--log-json` | emit JSON log lines | off |
| `--no-log-file` | skip the rotating log file | file enabled |
| `--modules-dir PATH` | extra plugin directory (repeatable) | - |
| `--ipc-host HOST` | control-plane bind address (`0.0.0.0` for LAN) | `127.0.0.1` |
| `--ipc-port PORT` | control-plane port (`0` = ephemeral) | `8123` |
| `--auth` / `--no-auth` | require or allow anonymous control-plane reads | auth on |
| `--ipc-token TOKEN` | static shared secret for the control plane | empty (ephemeral) |
| `--ipc-timeout SECONDS` | client timeout for control-plane reads | `2.0` |
| `--no-ipc` | do not open the control plane in `kdx run` | ipc enabled |
| `--instance-lock` / `--no-instance-lock` | claim or skip `var/chassis.lock` for this run | lock claimed |
| `--asyncio` / `--no-asyncio` | start or skip the chassis-managed asyncio loop | loop off |
| `--env-alias NAME=KEY` | map a legacy environment variable onto a dotted key (repeatable) | - |
| `--remote` | delegate the command to a running daemon over IPC, then exit | - |
| `--allow-anonymous-shutdown` | unauthenticated clients may stop the daemon | off |
| `--tick-interval SECONDS` | lifecycle tick interval | `1.0` |
| `--stop-timeout SECONDS` | budget for module `stop()` hooks | `5.0` |
| `--logs-poll-interval SECONDS` | follow poll interval for `kdx logs` | `0.5` |
| `--log-max-bytes BYTES` | rotating log size limit | `1048576` |
| `--log-backup-count COUNT` | rotating log archives kept | `5` |
| `--cli-width COLUMNS` | terminal table width for the CLI | `100` |
| `--confirmations` / `--no-confirmations` | ask before destructive module actions | on |
| `--no-autodiscover` | disable module auto-discovery for this run | autodiscover on |
| `--enable-module NAME` / `--disable-module NAME` | force one module on or off (repeatable) | - |
| `--name NAME` | chassis display name (banner, doctor) | `korodexios` |
| `--no-color` | disable coloured output | colour on a TTY |
| `-q, --quiet` | warnings and errors only | - |
| `-v, --verbose` | debug logging and tracebacks | - |
| `-y, --yes` | accept defaults, never prompt (auto-detected for non-TTY) | - |

Two bootstrap keys stay config/env-only: `core.distribution` (packaging metadata) and
`core.env.prefix` (it selects the env layer the CLI would itself override).

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

## `kdx reload`

```bash
kdx reload                      # ask the running daemon to re-read its configuration
kdx reload --json               # {"reloaded": true, "changed": [...]}
```

The daemon stays up: the layers are re-read with the same precedence, the changed keys
are published as `core.config.reloaded`, and every module's optional `on_config_reload`
hook runs with its scoped view. Log level changes apply immediately; moving a path role
(`core.paths.*`, `core.log.dir`) needs a restart. With no daemon answering the command
prints the note and exits `0` - a reload is never an error, and `kdx doctor` tells you
whether SIGHUP is wired.

## `kdx rpc`

```bash
kdx rpc heartbeat status                 # run a module command inside the daemon
kdx rpc heartbeat status --args '{"lines": 5}'
kdx rpc backup run --json                # the daemon's reply, verbatim
kdx rpc db index rebuild --args "{"lines": 8}"}   # a nested path, any depth
```

The client reads `var/chassis.ipc`, posts one `POST /api/v1/rpc`, and prints the answer:
`ok`, `command`, and `result` on success, `error` plus the daemon's HTTP status otherwise.
Exit code is `0` when the daemon's command exited `0`, else `1`; an unknown command is `404`
on the wire and `1` here. `kdx --remote heartbeat status` is the same delegation spelled
as a global flag: the client never boots a chassis of its own.

The command path nests as deep as the module declared it: the daemon walks
`CliCommand.subcommands` token by token, so `kdx rpc db index rebuild` is one rpc call. Its
reply carries `path` and `depth`, and a name that resolves to a group with no handler answers
`404` naming the group that has no such child.

## `kdx doctor`

```bash
kdx doctor                    # environment, paths, config provenance, modules, IPC
kdx doctor --json             # the same report as JSON
```

Works without a running daemon - it boots the chassis in-process, so it is the first
command to run when something looks misconfigured. The report covers the instance lock
(held, stale, and whether a daemon answers), the asyncio loop, watched workers, and the
diagnostics every module contributes; `kdx doctor --json` carries the same under
`instance`, `async`, `supervision`, `scheduler`, `locks`, `static`, and `diagnostics`. Scheduled
tasks that keep failing and lock files left behind by a dead process report diagnostics of
their own, so the run explains a stuck task and an abandoned lock without a module having to
mention either. A module in `failed` health or an `error` diagnostic exits the command `1`,
so it works as a deployment gate.

The report asks each module for `diagnostics()` and `health()`, so the command initializes the
discovered modules first; `modules.modules[].declarations_after_init` says whether a module's
routes, schemas, and checks were read with its context in hand. A module that only
answers those hooks with its configuration therefore contributes real findings here rather
than a `ModuleContractError` in the log. The built-in modules contribute their own rows
here since `v0.2.2`: `backup_engine` reports whether its source tree is readable and its
destination writable, and `heartbeat` reports whether its ticker thread is alive.

## `kdx logs`

```bash
kdx logs -n 50                # last 50 lines (default)
kdx logs -f                   # follow until Ctrl-C
kdx logs -f --timeout 5       # follow for 5 seconds (CI friendly)
kdx logs --raw                # only log lines, no decorations
kdx logs --clear              # truncate the log file (asks first; --yes accepts)
kdx logs --rotate             # archive the file and start a fresh one
```

No POSIX `tail` required: the file lives under the configured log directory and is
resolved from configuration, not a hardcoded path. Rotation names the archive
`kdx.log.<timestamp>`, keeps the newest `core.log.backup_count` archives, and is
cooperative: the running chassis notices the renamed file on its next write and
reopens the path, so it never keeps appending into the archive.

## `kdx modules`

```bash
kdx modules list              # name, version, state, origin, dependencies, description
kdx modules list --json
kdx modules show <name>       # spec plus live health for one module (or all)
kdx modules create <name> --path src/<pkg>/modules [--title TEXT] [--force] [--json]
```

`create` scaffolds `__init__.py`, `module.py`, and `kdx_module.toml` - zero stubs,
immediately runnable.

## `kdx config`

```bash
kdx config show                       # every resolved key with its source
kdx config show --key core.log        # only keys under a prefix
kdx config show --json                # mapping as JSON
kdx config show --reveal              # show secret values instead of ***redacted***
kdx config file                       # which file won, and the local override if any
kdx config check                      # unknown keys, module schema violations
kdx config check --json               # the same report for machines
```

Sources: `built-in default`, `config file`, `local override file`, `environment`,
`CLI override`.

`kdx config check` reports two high-confidence classes of typo: a `core.*` key with no
built-in default and no mapping default above it, and a `modules.<name>.*` key whose module
was never discovered. A mapping-valued default is a namespace, so the subkeys a project
declares under it - `core.env.aliases.LEGACY_RUN_MODE` - are valid paths and never fail the
run. Each suggestion stays inside the namespace the typo named: a sibling key of the
module the typo named, and never an unrelated loaded module. A foreign top-level key
(`greeting`, `app.name`, `project.owner`) is the project's own
it is listed as information and never fails the command. The chassis logs the same
findings once at boot, on stderr, and `kdx doctor --json` carries them under
`config.unknown`. The command exits `3` (configuration error) when it finds a key
nothing reads, so CI can gate on it. The second half of the report is declarative
module schemas: `config_schema()` hooks are read when a module registers and collected again
after `init(context)` ran, validated against the module's scoped keys, and printed under
"Module configuration schemas" - the three built-in proof modules declare
`backup_engine: 7 keys`, `heartbeat: 1 keys`, and `system_info: 2 keys` out of the box, so a
project is validated against a real schema before it owns one. A required
key with no value, a value that will not coerce, or a value outside the declared
`choices` is an error and exits `3`; the JSON payload carries them under `schemas.issues`.

## `kdx init`

```bash
kdx init my_app                       # interactive wizard (name, ecosystem, author)
kdx init my_app --path ../my_app --minimal --yes
kdx init my_app --ecosystem fullstack # adds web/ static client and web-ui skill
kdx init my_app --force               # write into a directory that already holds files
kdx init my_app --chassis-source index # how the project resolves the chassis
```

The seed ships `kdx.yml`, `.env.example`, `Makefile`, `README.md`, `AGENTS.md`,
`docs/AGENTS_GUIDE.md`, `docs/ARCHITECTURE.md`, `.agents/skills/`, a first module, and
smoke tests. It passes `make check` on arrival.

The chassis dependency is pinned to the repository by default
(`korodexios-core @ git+https://github.com/korodexios/kdx-core.git@v0.2.2`), so a fresh
venv can `pip install -e .` the seeded project before any index publish. Pass
`--chassis-source index` once the package is on an index; an unknown choice is refused
with a hint.

## `kdx wizard`

Interactive menu over the same declarative command tree the modules register. Numbered
choices, `[default]` in brackets, validation loops, `[OK] [?] [!] [i]` tags. Non-TTY
runs and `--yes` skip the prompts.

## `kdx version`

Prints the chassis version, the core API version, and the Python runtime line.

## Command nesting and arguments

Module commands are trees, not flat names, and every surface reads the same tree:

```bash
kdx db index rebuild --table page --lines 8   # argparse builds this from subcommands
kdx wizard                                    # the menu descends group by group
kdx rpc db index rebuild                      # the daemon walks the same path
```

- A group is a `CliCommand` with `subcommands` and no handler; asking for the group alone
  prints its usage (CLI) or answers `404` (rpc) - never a silent no-op.
- An option declared `required=True` is honoured: leaving it out exits `2`. It is refused for
  `store_true`, `count`, and `*_const` actions, because a flag is `False` when absent.
- A positional argument with `nargs="?"` and a `default` lands on that default, not on
  `None`; a positional is mandatory unless its `nargs` says otherwise.
- Duplicate or blank names inside one tree are rejected when the command is built, so a typo
  fails at import time instead of three levels down.

The chassis primitives, and the keys that configure them:

| Setting | Config key | Env | Default |
| --- | --- | --- | --- |
| Durable storage role | `core.paths.durable_dir` | `KDX_CORE__PATHS__DURABLE_DIR` | `data` |
| Periodic tasks on | `core.scheduler.enabled` | `KDX_CORE__SCHEDULER__ENABLED` | `true` |
| Scheduler on its own thread | `core.scheduler.threaded` | `KDX_CORE__SCHEDULER__THREADED` | `false` |
| Serve module static mounts | `core.ipc.static` | `KDX_CORE__IPC__STATIC` | `true` |

Named locks live in `&lt;state_dir&gt;/locks/&lt;name&gt;.lock`: `kdx doctor` lists them, and shutdown
releases every name the chassis took. Files that must outlive a moved `var/` belong under
`core.paths.durable_dir` (default `data/`), and recurring work belongs in
`context.schedule(interval_seconds, callback)`.

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
| `4`-`125` | a command handler's own verdict code, passed through unchanged |
| `130` | SIGINT |
| `143` | SIGTERM |

## Scripting rules

- Non-TTY runs never prompt; `--yes` also forces defaults in a TTY.
- `--json` is available on `doctor`, `status`, `modules list`, `modules show`,
  `modules create`, `config show`, `config check`, `reload`, `rpc`, and the module status
  commands.
- `kdx reload` exits `0` even with no daemon running; `kdx rpc` and `kdx --remote` exit `1`
  when nothing answers, because the operator asked for work that did not happen.
- A module command's exit code is the integer its handler returned, verdict codes
  included, so `kdx` can gate a deployment on `kdx index check` exiting `4` and not only on `0`/`1`.
- Diagnostics go to stderr, UI output to stdout: redirect stderr away and stdout stays
  parseable.
- `--home PATH` makes any command runnable from anywhere; it beats `KDX_HOME`.
