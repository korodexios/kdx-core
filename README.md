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
├── korodex.yml                 # project configuration (layer 3 of 4)
├── .env.example                # environment overrides (layer 2 of 4)
├── Makefile                    # uv-first developer shortcuts
├── src/korodexios_core/
│   ├── version.py              # single source of truth for versions
│   ├── constants.py            # exit codes, status tags, key names
│   ├── exceptions.py           # error hierarchy (message + exit code, no tracebacks)
│   ├── paths.py                # AppPaths: root discovery, runtime directories
│   ├── validate.py             # input validators: polite messages, no tracebacks
│   ├── config/                 # LayeredConfig: CLI > env > file > defaults
│   ├── logging/                # structured logging: stderr + rotating file
│   ├── events/                 # EventBus: thread-safe pub/sub with wildcards
│   ├── kernel/                 # the microkernel
│   │   ├── cli_contract.py     # CliCommand / CliArgument / CliSurface / CliRequest
│   │   ├── contract.py         # Module contract, ModuleSpec, ModuleState, Module base
│   │   ├── context.py          # CoreContext handed to every module
│   │   ├── discovery.py        # auto-discovery (built-in + external plugin dirs)
│   │   ├── registry.py         # module state, lifecycle ordering, CLI collection
│   │   ├── signals.py          # SIGINT / SIGTERM / SIGBREAK coordinator
│   │   └── chassis.py          # Chassis: init -> start -> run -> stop coordinator
│   ├── cli/                    # thin client: argparse + rich wizard, core commands, entry
│   │   ├── surface.py          # CliCommand tree -> argparse subparsers
│   │   ├── ux.py               # rich menus, bracketed defaults, validation loops
│   │   ├── wizard.py           # root wizard over the registered command tree
│   │   ├── commands.py         # version / doctor / modules / config / run / wizard
│   │   └── entry.py            # two-pass parser, global flags, error rendering
│   └── modules/                # discovery root (built-in plugins live here)
│       └── heartbeat/          # proof-of-concept module (background thread + events)
└── tests/                      # pytest suite: config, bus, contract, discovery, CLI
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
priority = 50                # lower starts first, stops last
requires_core = ">=0.1,<1.0"

[metadata]
tags = ["lifecycle"]
```

Discovery imports each candidate in isolation, resolves the entrypoint, instantiates it,
validates the contract, and checks `requires_core`. **A broken plugin becomes a logged
failure, never a crashed chassis.** Enable/disable is driven by the manifest, by
`core.modules.enabled` / `core.modules.disabled`, and by `modules.<name>.enabled`.

### 1.3 Configuration flow

Precedence is strict and reported per key by `korodex config show`:

```
CLI arguments  >  environment (.env + process env)  >  config file (YAML/JSON)  >  defaults
```

- Defaults live in `src/korodexios_core/config/defaults.py`.
- Config files are `korodex.yml`, `korodex.yaml`, or `korodex.json` (discovered at the root).
- Environment keys map to dotted config keys: strip `KORODEX_`, split nesting on `__`,
  lower-case, map `-` to `_`. Example: `KORODEX_CORE__LOG__LEVEL=DEBUG` → `core.log.level`.
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

```bash
uv venv --python 3.13            # or: python3 -m venv .venv
uv pip install -e ".[dev]"       # or:   pip install -e ".[dev]"
source .venv/bin/activate

korodex --version
korodex doctor                   # environment inspection
korodex modules list             # what auto-discovery found
korodex config show --key core.  # resolved values + provenance
korodex run --once               # boot, one tick, clean stop (CI friendly)
korodex run --duration 3         # run for three seconds
korodex heartbeat status         # a module-provided command
korodex wizard                     # interactive numbered-menu wizard (TTY only)
```

CI / non-interactive runs: pass `--yes` (accept defaults) and `--no-log-file` if the
filesystem is read-only. `korodex run --once` never prompts and never hangs, and the
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

## 3. Roadmap

| Phase | Scope | State |
| --- | --- | --- |
| 1 | Packaging, config hierarchy, logging, event bus, module contract, discovery, registry, signals, chassis, argparse CLI, `heartbeat` proof of concept, tests | **delivered** |
| 2 | `rich` UX layer: numbered-menu wizard over the registered commands, `[default]` prompts, `while True` validation loops, validators (port/IP/path/size/duration), `--yes` + non-TTY auto-detection | **delivered** |
| 3 | Built-in `system_info` and `backup_engine` modules, more tests, docs polish | planned |

Status tags used across the UI: `[✓]` success, `[?]` question, `[!]` error, `[i]` note.
Diagnostics always go to **stderr**; **stdout** stays clean for scripting.

## License

MIT — see [LICENSE](LICENSE).
