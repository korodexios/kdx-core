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
| Version | `0.1.0` (production), core API frozen at `v2` (additive changes only inside v2) |
| Runtime | CPython 3.11+, stdlib first (PyYAML for config, rich for the CLI surface) |
| Config file | `kdx.yml` / `kdx.yaml` / `kdx.json` (then `korodexios.*`) |
| Env prefix | `KDX_` primary; `KORODEXIOS_` is the canonical long prefix |
| Layout | `kernel/` infrastructure, `modules/` pure discovery root, `cli/` thin clients |
| State dir | `var/` (logs, `chassis.ipc`); never committed |
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
- Lifecycle states: `new` -> `initialized` -> `running` -> `stopped`, with `failed`
  and `disabled` as terminal side doors. A module that raises during a phase is marked
  `failed`, logged, and the chassis keeps booting - one bad plugin never kills the boot.

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
- Never construct an `ArgumentParser` inside a module.

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
| SIGINT | `130` |
| SIGTERM | `143` |

- The chassis installs signal handlers; a module must never call `signal.signal`.
- `stop()` always runs, including after a failed `start()`, and is bounded by
  `core.lifecycle.stop_timeout`.
- `kdx run --once` publishes a single tick and stops: the CI-safe smoke test.

## 11. IPC Control Plane

| Route | Method | Needs token |
| --- | --- | --- |
| `/health` | GET | never |
| `/api/v1/schema` | GET | never |
| `/api/v1/status` | GET | when `core.ipc.auth` is on |
| `/api/v1/shutdown` | POST | when auth is on, or when anonymous shutdown is disallowed |

- Bind address and port come from `core.ipc.host` / `core.ipc.port` (CLI
  `--ipc-host` / `--ipc-port`, or `kdx run --host/--port`). `0.0.0.0` means LAN
  accessible and is announced as `127.0.0.1` in the discovery file.
- The discovery file is `var/chassis.ipc`, mode `0600`, holding `pid`, `host`, `port`,
  `token`, `started_at`. Read it with `IpcDiscovery.load(path)` or reach a remote
  daemon with `IpcDiscovery.remote("http://host:port")`.
- The shared secret is `core.ipc.token` (`KDX_CORE__IPC__TOKEN`). When it is empty the
  chassis mints an ephemeral token at boot (`token_source: ephemeral`); set it to make
  the secret stable across restarts (`token_source: static`).
- `core.ipc.allow_anonymous_shutdown` (default `false`) is the only way to let an
  unauthenticated client stop the daemon.
- `kdx status --target http://host:port [--token SECRET]` queries any reachable
  daemon; `--json` is the machine contract.

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
| `IpcError` | control plane | no daemon answering, refused bind, 401 |
| `CliError` | CLI | bad usage, wizard abort |
| `ProjectError` | `kdx init` | busy target directory, bad project name |

## 15. Recipes

- **Add a module**: create `src/<pkg>/modules/<name>/` with `__init__.py`,
  `module.py` (blueprint shape, `MODULE` at the bottom), and `kdx_module.toml`;
  run `kdx modules list`, then `kdx <name> status`.
- **Add a config key**: put the default in `CORE_DEFAULTS`, document it in the config
  file, and read it with a typed getter - never a bare string compare.
- **Add a CLI command**: extend the module's `register_cli`; argparse and the wizard
  update themselves.
- **Add a test**: build the smallest chassis root in `tmp_path`, call the public API,
  assert on behaviour and provenance - not on log text.
- **Release**: bump `version.py`, run `make check`, tag `vX.Y.Z` annotated.
