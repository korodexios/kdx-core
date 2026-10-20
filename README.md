# korodexios-core

Industrial-grade, headless **microkernel chassis** that seeds every Korodexios project.
The core is a daemon-style engine that owns lifecycle, configuration, structured logging,
module discovery, and signal-safe shutdown. User interfaces (CLI, TUI, REST) are thin
clients that talk to the same core and never reimplement it.

- **Package:** `korodexios-core` (import name `korodexios_core`)
- **Console script:** `korodex` (alias: `korodexios`)
- **Python:** 3.11+ (developed on 3.13), `uv`-first, `venv` + `pip` compatible
- **Dependencies:** `pyyaml` (config files), `rich` (Phase 2 wizard layer)

---

## 1. Architecture

```
korodexios-core/
├── pyproject.toml              # PEP 621 metadata, console scripts, tool config
├── kdx.yml                     # project configuration (discovered by name)
├── .env.example                # environment overrides (layer 2 of 4)
├── Makefile                    # uv-first developer shortcuts
├── AGENTS.md                   # working agreement for humans and coding agents
├── .agents/skills/             # canonical skills: setup, wizard UX, verification
├── src/korodexios_core/
│   ├── version.py              # single source of truth for versions
│   ├── constants.py            # exit codes, status tags, key names
│   ├── exceptions.py           # error hierarchy (message + exit code, no tracebacks)
│   ├── paths.py                # AppPaths: root discovery, runtime directories
│   ├── validate.py             # input validators: polite messages, no tracebacks
│   ├── units.py                # format_bytes / format_duration helpers
│   ├── config/                 # LayeredConfig: CLI > env > file > defaults
│   ├── logging/                # structured logging: stderr + rotating file
│   ├── events/                 # EventBus: thread-safe pub/sub with wildcards
│   ├── kernel/                 # the microkernel
│   │   ├── cli_contract.py     # CliCommand / CliArgument / CliSurface / CliRequest
│   │   ├── contract.py         # Module contract, ModuleSpec, ModuleState, Module base
│   │   ├── context.py          # CoreContext handed to every module
│   │   ├── discovery.py        # auto-discovery (built-in + external plugin dirs)
│   │   ├── registry.py         # module state, lifecycle ordering, CLI collection
│   │   ├── ipc.py              # HTTP control plane: health, status, shutdown (LAN-bindable)
│   │   ├── signals.py          # SIGINT / SIGTERM / SIGBREAK coordinator
│   │   └── chassis.py          # Chassis: init -> start -> run -> stop coordinator
│   ├── cli/                    # thin client: argparse + rich wizard, core commands, entry
│   │   ├── surface.py          # CliCommand tree -> argparse subparsers
│   │   ├── ux.py               # rich menus, bracketed defaults, validation loops
│   │   ├── wizard.py           # root wizard over the registered command tree
│   │   ├── init_project.py     # 'korodex init' project templating (zero-stub seed)
│   │   ├── scaffold.py         # 'korodex modules create' package scaffolding
│   │   ├── commands.py         # version / doctor / status / init / modules / config / run / wizard
│   │   └── entry.py            # two-pass parser, global flags, error rendering
│   └── modules/                # discovery root (built-in plugins live here)
│       ├── heartbeat/          # background thread, events, clean shutdown
│       ├── system_info/        # OS / CPU / RAM / disk diagnostics (stdlib only)
│       └── backup_engine/      # source -> destination backups with dry runs
└── tests/                      # pytest suite: 188 tests across 19 modules
```

### 1.1 The module contract

Every plugin exposes exactly this surface (`korodexios_core.kernel.contract`):

| Member | Purpose |
| --- | --- |
| `name` | lower_case identifier; also the config namespace `modules.<name>` |
| `description` | one-liner shown by `korodex modules list` |
| `version` | `MAJOR.MINOR.PATCH`, validated at discovery |
| `init(context)` | receives `CoreContext` (config, logger, bus, paths, registry) |
| `register_cli(surface)` | injects declarative commands into the root CLI/wizard |
| `start()` | launches background threads, timers, sockets |
| `stop()` | releases resources; the chassis enforces a timeout |

`ModuleProtocol` is a `runtime_checkable` Protocol, so plain duck-typed classes work;
`korodexios_core.kernel.contract.Module` is an optional base class that adds state
tracking, scoped config, namespaced loggers, and `health()`.

### 1.2 Auto-discovery

`ModuleDiscovery` scans `korodexios_core/modules/` (built-ins) plus every directory in
`core.modules.extra_paths` (external plugins). Each candidate is:

- a **package** with `__init__.py` and an optional `korodex_module.toml` manifest, or
- a **single file** `name.py` (metadata derived from the docstring and the module object).

Manifest shape:

```toml
[module]
name = "heartbeat"
description = "Publishes a periodic heartbeat event."
version = "0.1.0"
entrypoint = "MODULE"        # attribute, factory, or class inside the module
enabled = true
depends_on = []          # these modules init/start first (topological order)
priority = 50                # lower starts first, stops last
requires_core = ">=0.1,<1.0"

[metadata]
tags = ["lifecycle"]
```
`depends_on` is honoured by the registry: dependencies run `init()` and `start()`
before their dependents (priority stays as the tie-break), and `korodex doctor`
reports unknown dependencies and dependency cycles instead of failing the boot.


Discovery imports each candidate in isolation, resolves the entrypoint, instantiates it,
validates the contract, and checks `requires_core`. **A broken plugin becomes a logged
failure, never a crashed chassis.** Enable/disable is driven by the manifest, by
`core.modules.enabled` / `core.modules.disabled`, and by `modules.<name>.enabled`.

### 1.3 Configuration flow

Precedence is strict and reported per key by `korodex config show`:

```
CLI arguments  >  environment (.env + process env)  >  local override (kdx.local.yml)
>  config file (kdx.yml)  >  built-in defaults
```

- Defaults live in `src/korodexios_core/config/defaults.py`.
- Config files are discovered at the root in this order: `kdx.yml` / `kdx.yaml` /
  `kdx.json` (primary), `korodexios.yml` / `.yaml` / `.json` (canonical long name), then
  `korodex.yml` / `.yaml` / `.json` retained as a legacy fallback. A gitignored
  `kdx.local.yml` (or `korodexios.local.yml`) is loaded as an override layer between the
  config file and the environment - machine-local tweaks without touching git.
- Environment keys map to dotted config keys: strip the prefix, split nesting on `__`,
  lower-case, map `-` to `_`. Prefixes are consulted in order - `KDX_` (primary), `KORODEXIOS_`
  (canonical), `KORODEX_` (legacy) - so `KDX_CORE__LOG__LEVEL=DEBUG` → `core.log.level` and
  `KDX_CORE__IPC__HOST=0.0.0.0` → `core.ipc.host`. A private `core.env.prefix: MYAPP` means
  exactly `MYAPP_`, with no implicit aliases.
- A real environment variable always beats the same key in `.env`.
- Values are coerced at the read site (`get_bool`, `get_int`, `get_path`, `get_list`), and a
  bad value raises a polite `ConfigValueError` naming key, value, and source.
- Modules receive a `ScopedConfig` rooted at `modules.<name>` that falls back to global keys.

### 1.4 Lifecycle and signals

```
Chassis.create()   -> paths, config, logging, bus, discovery, registration, signal handlers
  .initialize()    -> module init(context) in priority order
  .start()         -> module start()
  .run_forever()   -> publishes core.tick, waits on the shutdown gate
  .shutdown()      -> module stop() in reverse order (with timeout), restore signals
```

`SIGINT`, `SIGTERM` (and `SIGBREAK` on Windows) set one shutdown gate. The first signal
logs a single `[i] abort requested by SIGINT` line on stderr; repeats are ignored. The
process exits `130` for Ctrl+C (`128 + signum`), and a Python `KeyboardInterrupt`
traceback is never shown. Module `stop()` hooks always run.

---

## 2. Quickstart

The console installs three names for one entry point. **`kdx`** is the primary
high-velocity alias, **`korodexios`** is the canonical long name, and **`korodex`** is
kept as a backward-compatibility alias. They are identical, so every example below works
with any of them.

```bash
uv venv --python 3.13            # or: python3 -m venv .venv
uv pip install -e ".[dev]"       # or:   pip install -e ".[dev]"
source .venv/bin/activate

kdx --version                    # byte-identical from korodexios and korodex too
kdx doctor                       # environment inspection
kdx status                       # query the running daemon over its control plane
kdx logs -n 50                   # tail the rotating log file (no POSIX tail needed)
kdx logs -f                      # follow it; Ctrl-C stops cleanly, --clear truncates
kdx modules list                 # what auto-discovery found
kdx config show --key core.      # resolved values + provenance
kdx run --once                   # boot, one tick, clean stop (CI friendly)
kdx run --duration 3             # run for three seconds
kdx run --host 0.0.0.0 --port 8123   # expose the control plane to the LAN
kdx heartbeat status             # a module-provided command
kdx system info                  # OS / CPU / memory / disk / uptime
kdx system diagnostics           # interactive diagnostics menu (pick a section)
kdx backup plan                  # validate + show what a backup would copy
kdx backup run --dry-run         # simulate; --exclude '*.log' etc. are honoured
kdx modules create my_plugin --path ./my_plugins   # scaffold a plugin package
kdx init my_service --path ../my_service # seed a whole new project from the chassis
kdx wizard                       # interactive numbered-menu wizard (TTY only)
```

CI / non-interactive runs: pass `--yes` (accept defaults) and `--no-log-file` if the
filesystem is read-only. `kdx run --once` never prompts and never hangs, and the
chassis also **auto-detects non-TTY stdin**, forcing `core.cli.interactive=false` so CI
can never stall on a question.

Development shortcuts: `make install`, `make test`, `make lint`, `make typecheck`,
`make check`, `make demo`.

### 2.1 The crash-proof wizard

`korodex wizard` (and a bare `korodex` in a terminal) opens a numbered menu built from
the **same `CliCommand` objects** modules register for argparse — no module ever knows a
wizard exists.

```text
╭──────────────────────────── Command ────────────────────────────╮
│ [1] version  Print the chassis and core API version.            │
│ [2] doctor   Inspect the environment, configuration, and logs.  │
│ [3] modules  Inspect auto-discovered modules.                   │
│ [7] Quit     leave the wizard                                   │
╰─────────────────────────────────────────────────────────────────╯
[?] Command [1]:
[?] modules show: Module name; omit for all [1]:
[?] modules show: Emit the detail as JSON [y/N]:
```

UX rules enforced by `cli/ux.py` and `validate.py`:

- **Numbers beat typing** — every menu is `[1] [2] [3]`; ENTER accepts the highlighted
  default; a label prefix (`modules`) also works.
- **Defaults are always visible** — `Port [8080]: `, `[Y/n]` / `[y/N]`.
- **Free text is guarded by a `while True` validation loop** — an invalid answer prints
  `[!] port 99999 is outside 1-65535 (expected port number between 1 and 65535)` and
  re-prompts. Nothing crashes, nothing tracebacks.
- **Non-interactive is first-class** — `--yes` (or non-TTY stdin) answers every question
  with its default, so CI, Docker, and `| tee` never hang. A closed stdin mid-run falls
  back to defaults with an `[i] input closed; continuing with defaults` note.
- **Colour is opt-out** — `--no-color`, `core.cli.color`, or a non-TTY stdout; rich keeps
  Windows CMD/PowerShell, macOS, and Linux honest.

---

### 2.2 The control plane (daemon IPC)

A running chassis is headless, so it announces itself instead of requiring a client
library. `kdx run` (not `--once`) starts a stdlib `ThreadingHTTPServer` and writes a private
discovery file at `<state_dir>/chassis.ipc` (`var/chassis.ipc` by default, mode `0600`):

```json
{"pid": 8145, "port": 8123, "host": "127.0.0.1", "token": "91d0a1…", "started_at": 1792501644.35}
```

| Endpoint | Token | Purpose |
| --- | --- | --- |
| `GET /health` | never | liveness for Docker `HEALTHCHECK`, Kubernetes, curl |
| `GET /api/v1/status` | `X-Korodex-Token` while `core.ipc.auth: true` (the default) | the full `health_report()` payload as JSON |
| `POST /api/v1/shutdown` | token, unless `core.ipc.allow_anonymous_shutdown: true` | ask the daemon to stop; it exits `0` |

**Where it binds.** `core.ipc.host` defaults to `127.0.0.1`. `0.0.0.0` or an interface IP exposes
the same JSON to the LAN, and the chassis logs `[i] control plane listening on 0.0.0.0 (LAN
accessible)` - no `IpcError`, no fuss. A wildcard bind is announced in the discovery file as
`127.0.0.1`, the address a local client can actually dial. Flags: `kdx run --host 0.0.0.0
--port 8123`, or globally `--ipc-host` / `--ipc-port`.

**Who may call it.** `core.ipc.auth: false` (or `--no-auth`) lets any host on the network read
`/health` and `/api/v1/status` with no header - what a homelab dashboard or a VPN peer wants.
The destructive `POST /api/v1/shutdown` still demands the token unless the operator explicitly
sets `core.ipc.allow_anonymous_shutdown: true`, so a LAN-visible port is never an accidental
stop button. The shared secret is `core.ipc.token` (`KDX_CORE__IPC__TOKEN`,
`--ipc-token`): a static key you can hand to a remote client, falling back to a fresh
ephemeral token per boot when unset. The token never leaves the `0600` discovery file -
`kdx doctor` and `kdx status` hide it.

**Querying a remote chassis.** `kdx status --target http://192.168.1.50:8123` (also
`--host`/`--port`, plus `--token` when auth is on) queries that HTTP endpoint directly instead
of reading the local `var/chassis.ipc` file, and a 401 explains that a token is required
rather than failing silently.

`kdx status` reads the discovery file, checks the pid, and renders live state, uptime, modules,
event-bus counters, and process memory - or `[i] chassis daemon is not running`.
`Chassis.shutdown()` closes the server and deletes the file inside the same
`core.lifecycle.stop_timeout` budget as module `stop()` hooks, so a remote shutdown still runs
every cleanup hook.

### 2.3 Seeding a new project: `korodex init`

The chassis is the seed, not the dependency you copy. `korodex init my_service`
asks for a name, an author, and a target ecosystem (numbered menu, defaults in
brackets, `--yes` for CI) and writes an independent, runnable repository:

```text
my_service/
  pyproject.toml            # src layout, korodexios-core dependency, ruff/mypy/pytest config
  kdx.yml                   # core.modules.extra_paths -> src/my_service/modules
  Makefile                  # check: ruff + mypy --strict + pytest
  README.md, AGENTS.md      # quickstart and the working agreement
  .agents/skills/           # project-setup, cli-wizard-ux, verification (+ web-ui)
  src/my_service/cli.py     # thin entry point: boots the chassis with --home at this root
  src/my_service/modules/app/   # a real module: manifest, contract, CLI command, health()
  tests/test_smoke.py       # discovery, config, and contract proven
```

| Option | Effect |
| --- | --- |
| `--path DIR` | where to write (default: `./<name>`) |
| `--ecosystem python\|fullstack` | fullstack adds `web/` (static client for the IPC endpoint) and the `web-ui` skill |
| `--minimal` | bare chassis: no example module, `heartbeat` and `backup_engine` disabled in `kdx.yml` |
| `--force` | write into a directory that already holds files |

Nothing is a placeholder: the generated project passes `ruff check`, `mypy
--strict`, and `pytest` immediately, and `korodex --home <project> modules list`
shows the new `app` module as `external`.


### 2.4 Distribution and installation

The chassis installs like any PEP 517 package; nothing inside it assumes a checkout.

| Method | Command | What you get |
| --- | --- | --- |
| uv (recommended) | `uv sync --extra dev` | a project environment with `korodex` on PATH |
| plain pip | `pip install -e ".[dev]"` | the same, no uv required |
| isolated tool | `uv tool install .` or `pipx install .` | a global `korodex` with its own environment |
| from an artifact | `uv build && pip install dist/*.whl` | wheel and sdist, audited in CI |

- **One version source.** `src/korodexios_core/version.py` is the only declaration;
  `pyproject.toml` says `dynamic = ["version"]` and hatchling reads it with an explicit
  pattern that a test keeps honest. Current release: **`0.1.0rc2`**, core API `v1`.
- **Typed.** `py.typed` ships in the wheel, so downstream projects get full static
  checking against the chassis API, not just runtime behaviour.
- **Package data.** Every `modules/*/korodex_module.toml` manifest is an explicit wheel
  artifact; the sdist carries `README.md`, `AGENTS.md`, `.agents/skills/`, `kdx.yml`,
  `Makefile`, and the test suite. Skills are generated by `cli/init_project.py`, so no
  template directory is needed at runtime.
- **Console scripts.** `korodex` and `korodexios` both resolve to
  `korodexios_core.cli.entry:main`.

### 2.5 Log inspection without `tail`

`korodex logs` reads the rotating log file (`var/log/korodex.log` by default) with no
POSIX dependency, which matters on Windows CMD and in containers.

| Option | Effect |
| --- | --- |
| `-n, --lines N` | print the last N lines [50] |
| `-f, --follow` | stream appended lines; polling, so it behaves the same everywhere |
| `--timeout SECONDS` | stop following after SECONDS - CI and scripts never hang |
| `--clear` | truncate the file, asking `[?] Truncate log file? [y/N]` first (`--yes` assents) |
| `--raw` | print log lines only, no decorations, for piping |

A disabled or missing log file prints `[i] no log file found at <path>` and exits **0**;
`-n 0` is the only refusal (`[!] --lines must be at least 1`). The chassis owns the signal
handlers, so `Ctrl-C` while following exits **130** (SIGTERM **143**) with
`[i] stopped following korodex.log` - the same contract as `kdx run`, never a traceback.


### 2.6 Core API freeze (core api v1)

The release candidate **`v0.1.0rc1`** freezes the contract every Korodexios project
will build on. **`v0.1.0rc2`** adds the `kdx` alias, a LAN-bindable control plane with
optional per-route auth, `kdx status --target`, and the `kdx.yml` / `KDX_` naming
alignment; the frozen public surface is unchanged. The package root is the public surface - one import, no archaeology:

```python
from korodexios_core import (
    Chassis, ChassisOptions, ChassisState,      # lifecycle
    Module, ModuleProtocol, ModuleSpec,          # the module contract
    ModuleState, check_contract, as_module,
    CliCommand, CliArgument, CliRequest,         # the declarative CLI contract
    CliSurface,
    LayeredConfig, ScopedConfig, CORE_DEFAULTS,  # CLI > env > file > defaults
    EventBus, Event, Subscription,               # the event bus
    IpcServer, IpcOptions, IpcDiscovery,         # the control plane
    AppPaths, ExitCode, KorodexLogger,
)
```

| Frozen surface | Stability policy |
| --- | --- |
| `ModuleProtocol` / `Module` members, `check_contract` | additive only; a new required member is a major version |
| `CliCommand` / `CliArgument` / `CliRequest` | new optional fields are minor; renamed flags are major |
| `LayeredConfig` precedence and provenance | the four layers and their order are permanent |
| `EventBus` semantics (wildcards, `once`, `wait_for`, recursion guard) | behaviour is part of the contract |
| `IpcServer` routes and token header | new routes are minor; a route change is major |
| Exit codes | `0` operator stop, `130` SIGINT, `143` SIGTERM, `1` handled error |

`tests/test_public_api.py` fails the build if a frozen name disappears, and
`korodexios_core.CORE_API_VERSION` stays `"1"` for the whole v1 line.


## 3. Roadmap

| Phase | Scope | State |
| --- | --- | --- |
| 6 | `korodex init <name>`: zero-stub project seed - pyproject, kdx.yml, Makefile, README, AGENTS.md, `.agents/skills/`, a first module, smoke tests, optional `web/` client; `--minimal`, `--force`, `--ecosystem` | **delivered** |
| 5 | Daemon IPC: loopback HTTP control plane (`/health`, `/api/v1/status`, `POST /api/v1/shutdown`), discovery file with a shared token, and the `korodex status` command | **delivered** |
| 4 | Review hardening (event-bus recursion guard, slow-handler timing, plugin import isolation, backup destination and symlink guards, ASCII tag fallback), module dependency DAG (`depends_on`), `korodex modules create` scaffolding | **delivered** |
| 3 | Built-in `system_info` (OS/CPU/RAM/disk diagnostics) and `backup_engine` (validated source to destination backups with dry runs), full test coverage, docs polish | **delivered** |
| 2 | `rich` UX layer: numbered-menu wizard over the registered commands, `[default]` prompts, `while True` validation loops, validators (port/IP/path/size/duration), `--yes` + non-TTY auto-detection | **delivered** |
| 1 | Packaging, config hierarchy, logging, event bus, module contract, discovery, registry, signals, chassis, argparse CLI, `heartbeat` proof of concept, tests | **delivered** |
| 7 | Distribution readiness: dynamic version from `version.py`, audited wheel and sdist (`py.typed` + every module manifest), isolated `uv tool` / `pipx` install, the `korodex logs` operator helper, and a 3-OS x 3-Python CI matrix | **delivered** |
| 8 | `kdx` as the primary short alias (`korodexios` canonical, `korodex` kept for backward compatibility), LAN-bindable control plane (`core.ipc.host`), optional auth (`core.ipc.auth`, `--no-auth`), static token (`core.ipc.token`), the anonymous-shutdown guard, and `kdx status --target` | **delivered** |
| 9 | Naming alignment: `kdx.yml` / `korodexios.yml` config candidates with `korodex.yml` kept as a legacy fallback, a `kdx.local.yml` override layer, `KDX_` as the primary env prefix (`KORODEXIOS_`/`KORODEX_` understood), and `kdx`-first docs | **delivered** |
| 10 (next) | Publish `v0.1.0` to an index (`twine upload` / GitHub Releases) or a documented git bootstrap, a Textual/TUI front end over the same declarative `CliCommand` tree, and `korodex logs --rotate` | planned |


Status tags used across the UI: `[✓]` success, `[?]` question, `[!]` error, `[i]` note.
Diagnostics always go to **stderr**; **stdout** stays clean for scripting.

## License

MIT — see [LICENSE](LICENSE).
