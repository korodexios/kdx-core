# Korodexios Chassis - Agent Guide

Dense, contract-first reference for autonomous coding agents and LLMs working on
korodexios-core or any project seeded from it. Read this before editing: every
section is a contract, not a suggestion. Human-oriented narrative lives in
[ARCHITECTURE.md](ARCHITECTURE.md), [CLI_REFERENCE.md](CLI_REFERENCE.md), and
[TUTORIAL.md](TUTORIAL.md).

## 0. Facts

| Fact | Value |
| --- | --- |
| Distribution | `korodexios-core` |
| Import root | `korodexios_core` |
| Console scripts | `kdx` (primary short alias), `korodexios` (canonical) |
| Version | `0.2.6` (production), core API frozen at `v2` (additive changes only inside v2) |
| Runtime | CPython 3.11+, stdlib first (PyYAML for config, rich for the CLI surface) |
| Config file | `kdx.yml` / `kdx.yaml` / `kdx.json` (then `korodexios.*`) |
| Env prefix | `KDX_` primary; `KORODEXIOS_` is the canonical long prefix |
| Mapping defaults | a mapping-valued chassis key is a namespace: `core.env.aliases.NAME` is a valid chassis path |
| Layout | `kernel/` infrastructure, `modules/` pure discovery root, `cli/` thin clients |
| State dir | `var/` (logs, `chassis.ipc`, `chassis.lock`); never committed |
| Instance lock | `var/chassis.lock` in the state dir; a second daemon on the same root exits `1` |
| Named locks | `var/locks/<name>.lock` via `Chassis.acquire_lock(name)` or `context.paths.lock(name)`; released as one unit at shutdown |
| Durable dir | `data/` (`core.paths.durable_dir`): project state that outlives a moved `state_dir` |
| Periodic tasks | `context.schedule(interval_seconds, callback)`: the chassis clock runs them and stops them inside `stop_timeout` |
| Hook ordering | the declarative hooks are read at registration and collected again after `init(context)`; inspection commands initialize discovered modules first |
| Public surface | the package root `__all__`; anything deeper is importable but internal |

## 1. Golden Rules of the Chassis

1. **Headless first.** The chassis owns lifecycle, discovery, configuration, and
   shutdown. CLI, TUI, and any future front end are thin clients of that core: if a
   capability exists only in the CLI, it is a bug.
2. **Zero stubs.** No TODO bodies, no `pass` placeholders, no mock returns. If a
   branch exists, it runs - and a test runs it.
3. **Stdlib first.** A new dependency needs a paragraph of justification, not a line.
4. **Precedence is sacred.** CLI > environment (`KDX_*`, then `KORODEXIOS_*`, then
   `KORODEXIOS_*) > `kdx.local.yml` > `kdx.yml` > built-in defaults. Read
   configuration through `LayeredConfig` / `ScopedConfig`, never `os.environ`.
5. **Diagnostics to stderr, UI to stdout.** Modules never write to stdout; the CLI
   owns stdout. Log records, warnings, and tracebacks go to stderr.
6. **Clean shutdown.** Whatever `start()` acquires, `stop()` releases inside
   `core.lifecycle.stop_timeout`. SIGINT exits 130, SIGTERM 143, an operator stop
   (including `POST /api/v1/shutdown`) exits 0. Never surface a `KeyboardInterrupt`
   traceback.
7. **Crash-proof CLI.** Numbered menus, `[default]` in brackets, `while True`
   validation loops, `[OK] [?] [!] [i]` tags, `--yes` plus automatic non-TTY detection.
8. **Discovery is the extension point.** Adding a directory under a module root is the
   intended way to grow the system; `kernel/` never learns a product name.
9. **Verification before claims.** Quote the failing command before and after a fix.
   `make check` is the bar.

## 2. The Module Contract

A module is any object exposing these seven members (`REQUIRED_MEMBERS`):

| Member | Kind | When the chassis uses it | Notes |
| --- | --- | --- | --- |
| `name` | `str` attribute | discovery, config scope, logger scope | `^[a-z][a-z0-9_]*$`, must equal the directory name |
| `description` | `str` attribute | `kdx modules list` | one sentence |
| `version` | `str` attribute | reports, gating | `^\d+\.\d+\.\d+[A-Za-z0-9._+-]*$` |
| `init(context)` | method | after discovery, before any other hook | receive `CoreContext`; read config here |
| `register_cli(surface)` | method | once, right after `init` | declarative only; no argparse in modules |
| `start()` | method | dependency order, then ascending `priority` | launch background work; return promptly |
| `stop()` | method | reverse order, bounded by `stop_timeout` | release everything `start()` took |

- Subclass `korodexios_core.Module` and you inherit real defaults for all seven plus
  `state`, `context`, `started_at`, `stop_timeout`, `require_context()`, `config`,
  `logger`, `bus`, `paths`, `health()`, and `__repr__`.
- Identity is validated at construction: a missing or malformed `name`,
  `description`, or `version` raises `ModuleContractError` immediately.
- `check_contract(candidate) -> list[str]` returns human-readable violations (empty
  when valid); `as_module(candidate)` raises the same information as an exception.
- A foreign object that satisfies the seven members structurally is accepted
  (`ModuleProtocol`); no base class is required.
- `register_cli` runs exactly once per module, during `Chassis.initialize()`, into a
  headless surface: the command tree is kernel state, not argparse state. `kdx` later
  replays those same `CliCommand` objects into its own surface, so an embedded daemon
  (`rag serve` and friends) answers `POST /api/v1/rpc` without importing `cli/entry.py`.
- Lifecycle states: `new` -> `initialized` -> `running` -> `stopped`, with `failed`
  and `disabled` as terminal side doors. A module that raises during a phase is marked
  `failed`, logged, and the chassis keeps booting - one bad plugin never kills the boot.

### 2.1 Optional hooks (Core API v2)

The seven members above are the whole contract. These extra methods are duck-typed
opportunities, never requirements - the chassis `getattr`s them and moves on:

| Hook | Returns | What the chassis does with it |
| --- | --- | --- |
| `config_schema()` | `ConfigSchema`, fields, or a mapping | `kdx config check` and boot validate the module's scoped keys |
| `ipc_routes()` | `IpcRoute` sequence | the control plane answers the module's paths, token-gated per route |
| `diagnostics()` | `DiagnosticCheck` sequence | `kdx doctor` prints them and exits `1` when one is an error |
| `workers()` | name -> `Thread` mapping or pairs | supervision watches them; a dead worker marks the module `failed` |
| `on_config_reload(config)` | nothing | runs with the module's scoped config after `kdx reload` or SIGHUP |
| `static_dirs()` | `IpcStaticMount` sequence | the control plane serves those directories on its own port, labelling each file by its own media type |

- A hook that raises is logged and skipped: a broken `diagnostics()` never stops a boot,
  a doctor run, or a reload.
- The chassis reads the declarative hooks when a module registers, which is before
  `init(context)` has run, and collects them again once it has. A hook that needs
  configuration, paths, or its logger therefore answers with an empty or baseline report on
  the first pass - `()`, never an exception - and its real findings appear in `kdx doctor`,
  `kdx config check`, and the control plane after initialization. Registration-time failures
  are logged at debug level for exactly that reason.
- `kdx doctor` and `kdx modules show` initialize the discovered modules before asking for
  `diagnostics()` or `health()`: an inspection command never queries a module that has only
  been discovered. `kdx modules show` still records a hook failure as `health_error` rather than
  letting it end the report.
- `kdx doctor` also reports the chassis' own checks (log file, state dirs, stale lock,
  control-plane errors, failing scheduled tasks, abandoned named locks), so a module only
  contributes what the core cannot know.
- Since `v0.2.4` the built-ins report their runtime habits as well as their declarations:
  `system_info` starts a load sampler thread when `modules.system_info.watch_load` is on - the
  default wherever the platform can answer a load average - and hands that thread to
  supervision through `workers()`, so a shipped module's thread is what `kdx doctor` and
  `core.worker.lost` are pointed at. `heartbeat` schedules its ticker instead and reports no
  thread at all: the two hooks answer two different questions and neither is a formality.
- The base class hands over two chassis services without any plumbing: the read-only
  `scheduler` property (declare recurring work in `start()`, see section 16) and
  `context.paths.durable_dir` for files that must survive a moved state directory.
- Since `v0.2.2` the three built-in proof modules declare these hooks themselves -
  `backup_engine` (a seven-key schema, a source-and-destination check, `/api/v1/backup/plan`),
  `heartbeat` (an interval schema and a ticker liveness check), and `system_info` (a two-key
  schema and `/api/v1/system/snapshot`). A new module therefore has three working examples
  inside the package, and `kdx config check` validates three module schemas before a project
  runs its first command.

## 3. Canonical Module Blueprint

Copy this class into `src/<package>/modules/<name>/module.py`. It is executable code,
not pseudocode: a test in the chassis suite boots it against a real `CoreContext`.

```python
"""metrics: the canonical chassis module - config, logging, CLI, threads, events."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from korodexios_core import (
    TAG_NOTE,
    TAG_OK,
    CliArgument,
    CliCommand,
    CliRequest,
    CliSurface,
    CoreContext,
    Event,
    Module,
    Subscription,
)
from korodexios_core.cli.render import print_key_values, write_line

METRIC_EVENT = "metrics.sampled"
DEFAULT_INTERVAL = 5.0


class MetricsModule(Module):
    """Samples a counter on a timer and answers 'kdx metrics status'."""

    name = "metrics"
    description = "Samples a counter on a timer and answers 'kdx metrics status'."
    version = "0.1.0"

    def __init__(self) -> None:
        super().__init__()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._samples: list[float] = []
        self._beats: list[int] = []
        self._subscription: Subscription | None = None
        self._interval = DEFAULT_INTERVAL

    def init(self, context: CoreContext) -> None:
        """Read scoped config and subscribe before any background work starts."""
        super().init(context)  # stores the context; state -> INITIALIZED
        self._interval = max(0.05, self.config.get_float("interval", DEFAULT_INTERVAL))
        self._subscription = self.bus.subscribe(
            "core.heartbeat", self._on_beat, name="metrics"
        )
        self.logger.debug("metrics configured", interval_seconds=round(self._interval, 3))

    def register_cli(self, surface: CliSurface) -> None:
        """Contribute 'kdx metrics status' to whatever surface is being built."""
        surface.add_command(
            CliCommand(
                name="metrics",
                help="Metrics module utilities.",
                module_name=self.name,
                subcommands=(
                    CliCommand(
                        name="status",
                        help="Print sample counters and the configured interval.",
                        module_name=self.name,
                        handler=_status_command,
                        args=(
                            CliArgument(
                                ("--json",),
                                dest="as_json",
                                action="store_true",
                                help="Emit machine-readable JSON.",
                            ),
                        ),
                    ),
                ),
            )
        )

    def start(self) -> None:
        """Launch the sampler thread; the chassis already marked us RUNNING."""
        super().start()
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._sample_loop, name="kdx-metrics", daemon=True
        )
        self._worker.start()
        self.logger.info(
            "metrics sampler started", interval_seconds=round(self._interval, 3)
        )

    def stop(self) -> None:
        """Release what start() acquired: thread first, then the subscription."""
        self._stop.set()
        worker = self._worker
        self._worker = None
        if worker is not None and worker.is_alive():
            worker.join(timeout=max(0.5, self._interval + 0.5))
        if self._subscription is not None:
            self.bus.unsubscribe(self._subscription)
            self._subscription = None
        super().stop()
        self.logger.info("metrics sampler stopped", samples=len(self._samples))

    def health(self) -> dict[str, Any]:
        """Extend the standard lifecycle block with what an operator cares about."""
        data = super().health()
        data.update(
            {
                "interval_seconds": round(self._interval, 3),
                "samples": len(self._samples),
                "heartbeats_seen": len(self._beats),
                "thread_alive": bool(self._worker and self._worker.is_alive()),
            }
        )
        return data

    def _on_beat(self, event: Event) -> None:
        payload = event.payload if isinstance(event.payload, dict) else {}
        self._beats.append(int(payload.get("tick", 0)))

    def _sample_loop(self) -> None:
        while not self._stop.wait(self._interval):
            self._samples.append(time.monotonic())
            self.bus.publish(
                METRIC_EVENT,
                {"count": len(self._samples), "interval": round(self._interval, 3)},
                source=self.name,
            )


def _status_command(request: CliRequest) -> int:
    """Render counters from the registered instance; return a process exit code."""
    registry = request.context.registry
    record = registry.get("metrics") if registry is not None else None
    if record is None:
        write_line(f"{TAG_NOTE} metrics module is not registered")
        return 0
    health = (
        record.module.health()
        if hasattr(record.module, "health")
        else {"state": record.state.value}
    )
    if request.flag("as_json"):
        write_line(json.dumps(health, sort_keys=True, default=str))
        return 0
    print_key_values(
        [
            ("state", health.get("state", record.state.value)),
            ("interval seconds", health.get("interval_seconds", "-")),
            ("samples", health.get("samples", 0)),
            ("heartbeats seen", health.get("heartbeats_seen", 0)),
            ("thread alive", health.get("thread_alive", False)),
        ]
    )
    write_line(f"{TAG_OK} metrics {record.spec.version} is {record.state.value}")
    return 0


MODULE = MetricsModule()  # the manifest entrypoint: discovery imports this instance
```

What the blueprint proves:

- **Scoped configuration** - `self.config` is rooted at `modules.metrics` and falls
  back to global keys, so `interval` means `modules.metrics.interval`.
- **Namespaced logging** - `self.logger` is namespaced to the module; every record
  carries the module name, and structured kwargs become log fields.
- **Declarative CLI** - `register_cli` adds a command tree; the same data builds
  argparse flags and the interactive wizard menus. Handlers are plain functions
  `CliRequest -> int`.
- **Background execution** - a daemon thread driven by `threading.Event.wait`, so
  `stop()` is a set-then-join with a bounded timeout, never a bare `join()`.
- **Event bus** - subscribes in `init`, publishes from its own thread, and
  unsubscribes in `stop` (a module that outlives its subscription leaks handlers).
- **Health reporting** - `health()` returns a JSON-friendly dict; `kdx modules show`
  and `kdx status` render it verbatim.
- **Discovery handle** - the module-level `MODULE` instance is what the manifest's
  `entrypoint` names.

## 4. Scoped Configuration

`ScopedConfig` getters take a default as the second argument and raise
`ConfigValueError` naming key, value, and source when coercion fails:

| Getter | Accepts | Returns |
| --- | --- | --- |
| `get_str(key, default)` | any scalar | `str` |
| `get_int(key, default)` | digits, `"12"` | `int` |
| `get_float(key, default)` | numeric strings | `float` |
| `get_bool(key, default)` | `true/false`, `yes/no`, `on/off`, `1/0` (case-insensitive) | `bool` |
| `get_path(key, default)` | string | resolved `Path` |
| `get_list(key, default)` | list, or comma/newline separated string | `list[str]` |

- `has(key)`, `keys()`, `child("prefix")`, `source_of(key)`, `origin_of(key)`, and
  `as_dict(redact=True)` are available on the layered view.
- Provenance is per key: `ConfigSource.DEFAULT < FILE < LOCAL < ENV < CLI`.
- Secret-shaped keys (`token`, `secret`, `password`, `api_key`, `access_token`, `dsn`, ...)
  are masked as `***redacted***` by `as_dict(redact=True)` and by `kdx config show`;
  `kdx config show --reveal` is the operator's way to see the real value.
- `core.env.aliases` (plus `kdx --env-alias NAME=core.key`) maps a legacy environment
  variable onto a dotted key. The alias appears in provenance as
  `environment alias NAME`, and a real `KDX_` variable always outranks it.
- A mapping-valued default is a namespace, not a leaf: `core.env.aliases.LEGACY_RUN_MODE` is a
  chassis path, so `kdx config check` accepts the subkeys a project declares under it. Its
  `did you mean` answer stays inside the namespace the typo named - a module's own sibling key
  for a module-scoped typo, and never an unrelated loaded module.
- Module code never sees the `modules.<name>.` prefix; the chassis strips it.

## 5. Namespaced Logging

- `self.logger.info(msg, **fields)` - `debug`, `info`, `warning`, `error`,
  `exception` follow the same shape. Fields are structured kwargs, never f-stringed
  into the message.
- Levels and files: `core.log.level` (console), `core.log.file_level`,
  `core.log.file_enabled`, `core.log.dir`, `core.log.filename`,
  `core.log.max_bytes`, `core.log.backup_count`, `core.log.json`.
- Records go to **stderr**; the CLI owns stdout. `--log-json` emits machine-readable
  lines for shipping.
- A JSON line keeps the types the module passed: a count stays a number, a flag stays a
  boolean, a nested mapping stays a mapping, a list or tuple stays a list. Only what JSON
  cannot hold - a `Path`, an exception, a custom object - is shortened to text, so a shipper
  filters on data instead of parsing strings (`v0.2.3`).

## 6. Event Bus

```python
sub = self.bus.subscribe("core.*.stopped", handler, name="metrics", once=False)
count = self.bus.publish("metrics.sampled", {"count": 1}, source=self.name)
event = self.bus.wait_for("core.heartbeat", timeout=2.0)   # None on timeout
self.bus.unsubscribe(sub)
```

- Patterns are dot-segmented: `*` matches exactly one segment, `**` matches any
  number. `publish` returns how many handlers ran successfully.
- Handlers run **outside** the bus lock, so a handler may subscribe, unsubscribe, or
  publish. Nested publishing is bounded by `max_depth` (default 16).
- Every handler is timed; anything slower than the slow-handler budget shows up in
  `bus.stats()` so a blocking subscriber is visible.
- `history(limit)` returns the ring buffer of recent events; `clear()` empties it.
- `once=True` fires the subscription at most one time. `Subscription.unsubscribe(bus)`
  is the cancel handle.
- Event fields: `name`, `payload`, `source`, `at`, `sequence`, and `age(now=None)`.

## 7. Declarative CLI

`CliCommand` fields: `name`, `help`, `args` (tuple of `CliArgument`), `handler`
(`CliRequest -> int`), `subcommands` (tuple), `module_name`, `group`, `hidden`.

`CliArgument` fields: positional `flags` tuple (e.g. `("--json",)` or
`("-n", "--lines")`), `dest`, `metavar`, `type` (`str|int|float|path|flag`),
`action` (`store_true`, `append`), `choices`, `default`, `required`, `help`.

`CliRequest` gives handlers `context` (the `CoreContext`), `command`, `namespace`,
`options`, plus `option(name, default=None)` and `flag(name)` readers. A handler
returns a process exit code; `0` means success.

- Modules call `surface.add_command(...)` once per command; the chassis builds
  argparse and the wizard from the same tree, so a module command is automatically
  documented and menu-driven.
- `subcommands` nests to any depth, and one tree drives everything: the argparse surface,
  the wizard menus, and `POST /api/v1/rpc` all walk `kdx db index rebuild` the same way and
  report a `LookupError` naming the group that has no such child.
- `CliCommand` refuses a blank name, a duplicate subcommand, and a tree with neither a
  handler nor subcommands - all at construction, not at parse time.
- `CliArgument.required=True` is honoured for options: argparse insists, and the missing
  option exits `2`. It is refused for `store_true`, `count`, and `*_const` actions, because a
  flag is `False` when absent and so is never required.
- A positional argument receives its `default` only when that default is not `None`, so
  `nargs="?"` plus `default="."` lands on `"."` instead of on `None`.

## 8. Module Manifest Reference - `kdx_module.toml`

Optional metadata file inside the module directory. Discovery reads the `[module]`
table and passes `[metadata]` through untouched.

```toml
# kdx_module.toml - read by korodexios_core.kernel.discovery
[module]
name = "metrics"              # must equal the directory name; ^[a-z][a-z0-9_]*$
description = "Samples a counter on a timer."   # shown by 'kdx modules list'
version = "0.1.0"             # ^\d+\.\d+\.\d+[A-Za-z0-9._+-]*$
entrypoint = "MODULE"         # dotted path to the instance inside the package
enabled = true                # false => state 'disabled', never started
priority = 100                # int >= 0; lower starts earlier, ties broken by name
depends_on = ["heartbeat"]    # these names must start before this module
requires_core = ">=0.1"       # chassis version gate; unmet => skipped with a reason

[metadata]
tags = ["metrics", "proof-of-concept"]
owner = "korodexios"
```

| Key | Type | Default | Effect when wrong |
| --- | --- | --- | --- |
| `name` | str | directory name | mismatch with the directory is a discovery failure |
| `description` | str | "" | blank column in listings |
| `version` | str | `0.0.0` | malformed version is a contract violation |
| `entrypoint` | dotted str | `MODULE` | missing attribute is reported with the file path |
| `enabled` | bool | `true` | `false` records "disabled by manifest or configuration" |
| `priority` | int | `100` | negative raises a manifest error |
| `depends_on` | list[str] | `[]` | unknown or cyclic names are reported, never started |
| `requires_core` | specifier | none | unmet gate records "needs core ..." and skips |
| `[metadata]` | table | `{}` | free-form; never validated, never required |

- No manifest is legal: discovery falls back to the directory name and `MODULE`.
- A manifest that fails to parse, a package that fails to import, and a class that
  breaks the contract all become logged discovery failures - the boot continues.
- `kdx modules list` shows origin (`builtin`, `project`, `path`) and state; `kdx
  modules show <name>` prints the spec plus `health()`.

## 9. Configuration Precedence and Discovery

Five layers, lowest to highest: **defaults -> `kdx.yml` -> `kdx.local.yml` ->
environment (`.env` then process env) -> CLI overrides**.

- Candidate order at the root: `kdx.yml`, `kdx.yaml`, `kdx.json`, then
  `korodexios.yml`, `korodexios.yaml`, `korodexios.json`.
  The first hit wins; the rest are ignored.
- Local override candidates: `kdx.local.yml`, `kdx.local.yaml`, `kdx.local.json`,
  `korodexios.local.*`. Gitignored by the seed template.
- Environment mapping: strip the prefix, split nesting on `__`, lower-case, map `-`
  to `_`. Prefixes are consulted in order `KDX`, `KORODEXIOS`; the first
  match wins. A custom `core.env.prefix` (e.g. `MYAPP`) means exactly `MYAPP_` with
  no implicit aliases.
- Root discovery walks upward from the current directory looking for
  `pyproject.toml` with a `[project]` table, `pyproject.toml`, `kdx_module.toml`,
  or any config candidate; `.kdx-root` or `.korodexios-root` stops the walk explicitly.
- Environment overrides: `KDX_HOME`, `KDX_CONFIG`, `KDX_ENV_FILE`, `KDX_TRACEBACK`
  (and the `KORODEXIOS_` spelling).
- Secrets: never read `os.environ` in a module; a value that came from the
  environment must be visible in `kdx config show` provenance.
- `core.paths.state_dir` and `core.paths.data_dir` move the writable roles off `var/`;
  `Chassis.create()` creates both before any module starts, so a module never `mkdir`s
  its own home.
- `Chassis.create()` accepts an already-built `LayeredConfig` (`ChassisOptions.config`):
  an embedder that resolved configuration itself hands it over and the chassis does not
  read the disk again.

## 10. Lifecycle, Signals, Exit Codes

Boot order: discover paths -> load config -> build logger -> create bus -> discover
modules -> validate contracts -> `init()` -> `register_cli()` -> `start()` in
dependency order -> lifecycle loop -> `stop()` in reverse -> close logging.

| Situation | Exit code |
| --- | --- |
| Clean run or operator stop (`kdx status` shutdown, `request_clean_stop`) | `0` |
| Handled chassis or CLI failure | `1` |
| Usage error (bad flags) | `2` |
| Configuration error | `3` |
| A command's own verdict code (`4`-`125`) | passed through unchanged |
| SIGINT, and Windows `SIGBREAK` on the same gate | `130` |
| SIGTERM | `143` |

- A command handler returns an integer and `kdx` returns it verbatim: a module command may
  report a domain verdict (`4`, `42`, `77`) and a deployment script branches on it. Only the
  chassis' own failures are mapped to the codes above.
- The chassis installs signal handlers; a module must never call `signal.signal`.
- `stop()` always runs, including after a failed `start()`, and is bounded by
  `core.lifecycle.stop_timeout`.
- `kdx run --once` publishes a single tick and stops: the CI-safe smoke test.
- SIGHUP (POSIX) asks for a configuration reload without exiting: the layers are
  re-read, the diff is published as `core.config.reloaded`, and each module's
  `on_config_reload` runs. `core.reload.sighup` (default `true`) turns that off;
  `kdx reload` asks the same thing of a running daemon over IPC.
- Before any module starts, the daemon claims `var/chassis.lock`. A second daemon on the
  same root raises `InstanceLockError` naming the live holder instead of binding a
  fallback port nobody asked for. `--no-instance-lock` (or `core.instance.lock: false`) is
  for a one-shot command that must not disturb the daemon.

## 11. IPC Control Plane

| Route | Method | Needs token |
| --- | --- | --- |
| `/health` | GET | never |
| `/api/v1/schema` | GET | never |
| `/api/v1/status` | GET | when `core.ipc.auth` is on |
| `/api/v1/shutdown` | POST | when auth is on, or when anonymous shutdown is disallowed |
| `/api/v1/reload` | POST | when `core.ipc.auth` is on |
| `/api/v1/rpc` | POST | when `core.ipc.auth` is on |

- Bind address and port come from `core.ipc.host` / `core.ipc.port` (CLI
  `--ipc-host` / `--ipc-port`, or `kdx run --host/--port`). `0.0.0.0` means LAN
  accessible and is announced as `127.0.0.1` in the discovery file.
- The discovery file is `var/chassis.ipc`, mode `0600`, holding `pid`, `host`, `port`,
  `token`, `started_at`. Read it with `IpcDiscovery.load(path)` or reach a remote
  daemon with `IpcDiscovery.remote("http://host:port")`. The chassis writes the file to a
  `0600` sibling and renames it into place, so a reader never finds it empty and mistakes a
  healthy daemon for a dead one (`v0.2.5`).
- The shared secret is `core.ipc.token` (`KDX_CORE__IPC__TOKEN`). When it is empty the
  chassis mints an ephemeral token at boot (`token_source: ephemeral`); set it to make
  the secret stable across restarts (`token_source: static`).
- `core.ipc.allow_anonymous_shutdown` (default `false`) is the only way to let an
  unauthenticated client stop the daemon.
- `kdx status --target http://host:port [--token SECRET]` queries any reachable
  daemon; `--json` is the machine contract.
- Every answer ends the connection: the handler sends `Connection: close` and sets its own
  `close_connection` flag, so a client that offered HTTP/1.1 keep-alive gets one response and
  a closed socket instead of waiting for a second answer (`v0.2.4`).
- A socket that closes while the control plane is stopping is an ending, not a fault: the
  handler records one quiet line - debug while the chassis stops, warning while it runs - and
  answers the next client. A fault that is not a socket is never swallowed (`v0.2.5`).
- `POST /api/v1/reload` re-reads the configuration in the running daemon; the reply
  carries `reloaded` and the list of `changed` keys.
- `POST /api/v1/rpc` with `{"command": "heartbeat status", "args": {...}}` runs one
  registered module command inside the daemon: `200` with `result`, `404` for an unknown
  command, `400` for arguments that do not parse, `501` when the daemon takes no rpc.
- `kdx rpc COMMAND` and `kdx --remote COMMAND` are thin clients of that route: they read
  the discovery file, post once, and print the daemon's answer - no second chassis loop.
- `IpcRoute(path, handler, methods, auth, module)` is how a module joins the control
  plane, declared from `ipc_routes()` or registered live with
  `chassis.register_ipc_route(route)`. Core paths are reserved: a module that asks for
  `/health` is told so at registration, not at request time.
- A route answers JSON unless the handler says otherwise. It may return `bytes` (labelled
  from the route's `content_type`, else guessed from the route path's extension), a `str`
  with a declared non-JSON type, or an `IpcResponse` that owns its body, media type, status,
  and headers. `IpcResponse.text()`, `.html()`, and `.file()` cover the common shapes, and
  `guess_content_type(name)` is the same labelling function the chassis uses.
- `chassis.register_static_dir(route_prefix, directory_path)` - or a `static_dirs()` hook -
  serves a directory on the control plane's port: index names, `Cache-Control`, weak `ETag`
  with `304`, `HEAD` without a body, `405` for a write, `401` for an `auth=True` mount, and a
  traversal guard that refuses to read above the mounted directory. That guard is
  path-normalisation strict on purpose: the subpath is unquoted first (a client's `%5c` is a
  separator, not a filename), `/` and `\\` are folded to one separator, and a drive letter, an
  alternate data stream (`report.txt:Zone.Identifier`), or a reserved device name (`NUL`,
  `com1.log`) is refused; containment is verified on the resolved path and case-folded on a
  case-insensitive filesystem, so a real file never answers 404 on a case mismatch. Static
  mounts default to `auth=False` because a browser cannot send the token header.
- `core.ipc.static` (default `true`) gates whether module mounts are published at all.
- `IPC_SCHEMA_VERSION` stays `1`: `/api/v1/schema` only gained `content_type` per route and a
  `static_mounts` list.
- The token compare is constant-time (`hmac.compare_digest`): a wrong-length guess costs
  the same as a right-length one.

## 12. Verification Loop

Run this sequence, in this order, before claiming any change works:

```bash
make check        # ruff check src tests, then mypy --strict, then pytest -q
kdx run --once    # boot, one lifecycle tick, clean stop; must exit 0
kdx status        # live report from a running daemon over the control plane
```

- `make check` proves style, types, and behaviour in one shot; the bar is 100% green.
- `kdx run --once` proves the chassis boots from real config and shuts down cleanly.
- `kdx status` proves the control plane answers and the registry reports live state.
- In a seeded project the same bar is `make check PYTHON=<venv>/bin/python`.
- A new capability is not done until it has a test that fails without it.

## 13. Agent Do / Don't

- **Do** keep `kernel/` free of product knowledge; put new behaviour in a module.
- **Do** raise `KorodexiosError` subclasses with a `hint=` - the CLI prints it.
- **Do** make every command safe for non-TTY runs (`--yes`, no prompts, no menus).
- **Do** add a test with every change; tests use no network and no sleeps longer than
  one lifecycle tick.
- **Don't** read `os.environ` outside the config layer.
- **Don't** write to stdout from a module or from library code.
- **Don't** spawn a thread without a `stop()` path that joins it.
- **Don't** add a dependency without a paragraph of justification.
- **Don't** rename public API names: the core API is frozen at `v2`.
- **Don't** commit `var/`, `.env`, or `kdx.local.yml`.

## 14. Error Taxonomy

`KorodexiosError` is the base; every subclass carries an optional operator `hint`.

| Exception | Raised by | Typical cause |
| --- | --- | --- |
| `PathDiscoveryError` | `AppPaths.discover` | no chassis root found, bad `KDX_HOME` |
| `ConfigFileError` | config loaders | unreadable or malformed config file |
| `ConfigurationError` | `LayeredConfig` | invalid key type or unknown option |
| `ConfigValueError` | `ScopedConfig` getters | value cannot be coerced to the asked type |
| `ModuleContractError` | contract checks | identity or hook violations |
| `ModuleDiscoveryError` | discovery | import failure, bad manifest, cycle |
| `ModuleLifecycleError` | registry | `start` or `stop` raised or timed out |
| `ChassisError` | chassis | lifecycle misuse |
| `IpcError` | control plane | no daemon answering, refused bind, 401, a bad static mount |
| `LockTimeoutError` | named locks | another holder owns the name (`acquire_lock`, `context.lock`) |
| `CliError` | CLI | bad usage, wizard abort |
| `ProjectError` | `kdx init` | busy target directory, bad project name |

## 15. Recipes

- **Add a module**: create `src/<pkg>/modules/<name>/` with `__init__.py`,
  `module.py` (blueprint shape, `MODULE` at the bottom), and `kdx_module.toml`;
  run `kdx modules list`, then `kdx <name> status`.
- **Declare a config schema**: return `ConfigSchema(module=self.name, fields=(...))` from
  `config_schema()`, then gate it in CI with `kdx config check` (exit `3` on a violation,
  machine-readable with `--json`).
- **Expose an endpoint**: add `ipc_routes()` returning `IpcRoute` objects and answer
  `curl -H "X-Kdx-Token: $TOKEN" http://127.0.0.1:PORT/api/v1/<name>`.
- **Delegate to the daemon**: `kdx --remote heartbeat status` from a client checkout - the
  daemon does the work and the client exits with the daemon's code.
- **Add a config key**: put the default in `CORE_DEFAULTS`, document it in the config
  file, and read it with a typed getter - never a bare string compare.
- **Add a CLI command**: extend the module's `register_cli`; argparse and the wizard
  update themselves.
- **Schedule recurring work**: call `context.schedule(300.0, self.sweep, name="sweep")` in
  `start()`; the chassis clock runs it and `make check` needs no sleeps.
- **Guard a coordination point**: `with chassis.lock("index"): ...` - released with the
  chassis, even when the block raises.
- **Serve a dashboard**: return `IpcStaticMount("/ui", self.assets_dir)` from `static_dirs()`
  and answer `GET /ui/index.html` on the control plane's port.
- **Add a test**: build the smallest chassis root in `tmp_path`, call the public API,
  assert on behaviour and provenance - not on log text.
- **Release**: bump `version.py`, run `make check`, tag `vX.Y.Z` annotated.

## 16. Chassis Primitives: Durable Paths, Named Locks, Periodic Tasks

Three primitives a module used to hand-roll: a directory that survives a moved state
directory, a coordination point that is shed at shutdown, and a recurring task that stops
with the process. All three are chassis-owned, so the module declares, never manages.

### 16.1 Path roles

```python
paths.durable_dir                 # <root>/data - committed databases, ledgers, indexes
paths.with_durable_dir("keep")    # project-relative, resolved under the root
paths.durable_module_dir("ops")   # <durable>/modules/ops, created on demand
paths.locks_dir                   # <state_dir>/locks - one file per named lock
paths.lock_path("index")          # <locks_dir>/index.lock
paths.lock("index", timeout=0.5, blocking=True, metadata={"label": "reindex"})
paths.named_lock_files()          # lock files present on disk, sorted
```

`state_dir` is the runtime role: logs, `chassis.ipc`, `chassis.lock`, and the locks directory -
safe to delete between runs. `durable_dir` is the project role: it never moves when an
operator moves `state_dir`, and it is not the same as `data_dir` (module *data* caches). The
default is `data/` under the root, so a project's committed database stays put. An explicit
project file is anchored before the filesystem is asked about it: a relative `--config` or
`--env-file` is resolved under the chassis root, so `kdx --home /srv/app --config kdx.yml`
reads `/srv/app/kdx.yml` while the operator stands somewhere else, and a missing file is named
under the root in the error.

### 16.2 Named locks

```python
handle = chassis.acquire_lock("index", timeout=0.5, blocking=True, metadata={"job": "nightly"})
with chassis.lock("reindex", timeout=0.0) as held:   # context manager, released on exit
    ...
chassis.release_lock("index")        # False when this chassis never held it
chassis.named_lock("index")          # the handle this process holds, or None
chassis.locks.names()                # every name this chassis holds, sorted
chassis.locks.stale_names()          # lock files whose recorded process is gone
```

The same names are reachable from a module as `context.acquire_lock(...)`,
`context.release_lock(...)`, and `context.paths.lock(name)`. Acquisition is either
non-blocking (`timeout=0.0`, the default: try once, raise `LockTimeoutError`) or bounded
(`timeout=2.0`, retrying every 20 ms), and `blocking=False` means one attempt. The lock file
carries `pid`, `label`, `name`, `started_at`, `host`, and `port`, so `holder()` and `is_stale()`
answer who owns it and whether that owner is gone - the same honesty as the instance lock,
one name at a time. Shutdown releases every name the chassis took (`release_all()`), so a
crashed run leaves a *stale* file, not a permanent lock: `kdx doctor` reports it as `info`.
`chassis.lock(name)` inspects the registry and reserves the name in one guarded step, so two
threads that claim the same name at the same moment never both pass a check: the loser
raises `LockTimeoutError` immediately instead of waiting out a timeout it had already lost.
A lock that cannot write its owner payload withdraws the claim: `acquire()` releases the
descriptor it just took and re-raises, so `held` is never true over a lock file that says
nothing (`v0.2.3`). `backup_engine` takes this lock for every copy, which is why a scheduled
sweep and a manual `kdx backup run` never write one destination at the same moment - the
refused run comes back as a `BackupError` with a hint, and the lock file stays behind for
`kdx doctor` to read.

Names are validated at the call site: 1-64 characters of letters, digits, `_`, `.`, `-`, and
spaces, no leading dot and no `..`. `../escape` and `a/b` are refused, so a lock name can
never name a file outside `var/locks/`.

### 16.3 Periodic tasks

```python
task = context.schedule(300.0, self.sweep, name="sweep", module=self.name)
task = context.schedule(60.0, self.flush, first_delay=10.0, repeat=True, run_now=False)
context.scheduler.unschedule("sweep")     # or cancel(name)
context.scheduler.stats()                 # runs, failures, and one row per task
context.scheduler.run_due()               # the chassis tick calls this; returns names run
context.scheduler.run_task("sweep")       # run now: a route or CLI command triggers it
```

By default the scheduler is **tick-driven**: the chassis clock calls `run_due()` on every
tick, so nothing runs on its own thread and `make check` needs no sleeps. Set
`core.scheduler.threaded: true` (or `start(threaded=True)`) for a chassis that blocks in a
loop of its own; the thread is named `kdx-scheduler` and is joined inside
`core.lifecycle.stop_timeout`. `core.scheduler.enabled: false` parks every task.

A callback takes no arguments and its exceptions are contained: a task that raises is
counted (`failures`, `last_error`), logged to stderr once per failure, published as
`core.task.failed`, and stays scheduled - `kdx doctor` shows a `warning` per failing task.
A successful run publishes `core.task.run` with the task's own `as_dict()` payload.
`repeat=False` parks the task after one run; `first_delay` separates the first fire from the
interval; duplicate names get `-2`, `-3` suffixes instead of clobbering each other.
Two bounds keep a long-lived chassis honest: `last_error` is capped at `MAX_ERROR_LENGTH`
characters (the traceback is in the log, not in every status snapshot), and `unschedule()`
drops the task, its place in the declaration order, and its suffix bookkeeping, so
`sweep-2` in an old log line is never a different task tomorrow. `heartbeat` is the shipped
dogfood for the primitive (`v0.2.3`): its ticker is one `ticker` task on the chassis clock, so
the module owns no thread, supervision has no worker to join, and a stopped module leaves no
task behind. A callback may take scheduler decisions of its own - schedule another task,
unschedule a neighbour, or withdraw its own name mid-run - because a sweep works from a
snapshot of what was due, taken with the guard held, and calls each task with the guard
released: self-cancellation is a decision, not a missed beat (`v0.2.4`).
`backup_engine` is the shipped pairing of the two primitives (`v0.2.5`): when `schedule_interval` is
positive the module declares an `auto_sweep` task, and the copy that task starts takes the same
named lock a manual `kdx backup run` takes, so a sweep that arrives mid-copy steps aside, counts
the refusal, and says so in `kdx doctor` instead of reporting a failing task.

### 16.4 Configuration and public surface

| Key | Default | Meaning |
| --- | --- | --- |
| `core.paths.durable_dir` | `data` | project-relative durable role |
| `core.scheduler.enabled` | `true` | the tick loop runs declared tasks |
| `core.scheduler.threaded` | `false` | run the scheduler on its own thread instead |
| `core.ipc.static` | `true` | publish module `static_dirs()` mounts on the control plane |

Exported from `korodexios_core` and `korodexios_core.kernel`: `NamedLock`,
`NamedLockRegistry`, `TaskScheduler`, `ScheduledTask`, `IpcResponse`, `IpcStaticMount`,
`guess_content_type`, and `LockTimeoutError`. `context.as_dict()` gained `scheduler` and
`locks`; `kdx doctor` gained `scheduler`, `locks`, and `static` sections.
