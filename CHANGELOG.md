# Changelog

Notable changes to korodexios-core. Versions follow the git tags; the phase
history lives in [docs/ROADMAP.md](docs/ROADMAP.md).

## [Unreleased]

_Nothing pending: the working tree is the `v0.2.0` tag._

## [0.2.0] - 2026-10-22

Core API `v2` in production: an additive hardening pass that turns the chassis into a
daemon an operator can leave running. Nothing in the frozen surface changed shape - every
capability below is a new export, a new flag, a new route, or a new *optional* module
hook.

### Added

- **Single-instance lock**: `InstanceLock` claims `var/chassis.lock` in the state
  directory before any module starts; a second daemon on the same root raises
  `InstanceLockError` naming the live holder (pid, label, host, port, uptime) instead of
  binding a fallback port nobody asked for. `--instance-lock`/`--no-instance-lock`,
  `core.instance.lock`, and `kdx doctor` reporting.
- **Configuration reload without exit**: SIGHUP (POSIX) and `POST /api/v1/reload` re-read
  the layers, publish `core.config.reloaded` with the changed keys, and call each module's
  optional `on_config_reload(config)`. `kdx reload` is the client.
- **Command delegation**: `POST /api/v1/rpc` runs a registered module command inside the
  running daemon; `kdx rpc COMMAND` and `kdx --remote COMMAND` are thin clients that never
  boot a second chassis loop.
- **Declarative control-plane routes**: `IpcRoute(path, handler, methods, auth, module)`
  from the optional `ipc_routes()` hook or `chassis.register_ipc_route()`. Core paths are
  reserved at registration, and `GET /api/v1/schema` lists the module routes a client can
  expect.
- **Declarative module configuration schemas**: `ConfigSchema`, `ConfigField`,
  `ConfigKind`, and `ConfigIssue` from the optional `config_schema()` hook, validated at
  boot and by `kdx config check` (exit `3` on a violation, machine-readable with `--json`).
- **Secret hygiene**: `LayeredConfig.as_dict(redact=True)` masks secret-shaped keys with
  `***redacted***`; `kdx config show` masks by default and `--reveal` shows the value.
- **Environment aliasing**: `core.env.aliases` and `kdx --env-alias NAME=core.key` map a
  legacy variable onto a dotted key, with provenance `environment alias NAME`; a real
  `KDX_*` value always outranks the alias.
- **Managed asyncio loop**: `AsyncRuntime` runs one event loop on a named daemon thread
  (`--asyncio`, `core.async.enabled`); `context.submit_async()` returns a
  `concurrent.futures.Future` and `context.run_async()` falls back to a private loop, so
  module code never branches on the option.
- **Worker supervision**: `WorkerSupervisor` watches the threads modules declare through
  the optional `workers()` hook; the first death moves the module to `failed` in health
  reports and publishes `core.worker.lost` once per worker.
- **Module-contributed diagnostics**: the optional `diagnostics()` hook returns
  `DiagnosticCheck` objects; `kdx doctor` prints them next to the chassis' own checks and
  exits `1` when one is an `error`.
- **Path roles and injection**: `core.paths.state_dir` and `core.paths.data_dir` relocate
  the writable roles and `Chassis.create()` prepares them before any module starts;
  `ChassisOptions.config` accepts an already-resolved `LayeredConfig`.
- **Optional-dependency hints**: discovery records a module skipped for a missing
  importable as `DiscoveryOptional`, so `kdx doctor` says which extra to install.

### Added

- **Arbitrary-depth command nesting**: `CliCommand.subcommands` nests as deep as a CLI
  needs, and one tree drives the argparse surface, the wizard menus, and rpc:
  `CliCommand.walk()/find()/depth/leaves` describe it, `ArgparseSurface` renders it, and
  `POST /api/v1/rpc` dispatches `kdx db index rebuild` with the same walker, naming the group
  that has no such child. `CliArgument.required` is honoured for options (a missing option
  exits `2`) and refused for `store_true`, `count`, and `*_const` actions, and a positional
  `default` reaches argparse only when it is not `None`.
- **Custom control-plane content types**: a route handler may return `bytes` (labelled from
  the route's `content_type`, else from the path extension), a `str` with a declared
  non-JSON type (`text/html`, `text/css`, `application/javascript`, …), or an
  `IpcResponse(body, content_type, status=200, headers=())` that owns all three;
  `IpcResponse.text()/.html()/.file()` and `guess_content_type()` cover the common shapes, and
  `/api/v1/schema` reports `content_type` for every route.
- **Static directories on the control plane**: `chassis.register_static_dir(route_prefix,
  directory_path)` - or the optional `static_dirs()` hook - serves a module's files on the
  chassis port with index names, `Cache-Control`, a weak `ETag` with `304`, `HEAD` without a
  body, `405` for writes, opt-in `auth=True`, and a traversal guard that reads nothing above
  the mounted directory. `core.ipc.static` gates publication.
- **Named locks**: `Chassis.acquire_lock(name, timeout=0.0, blocking=True, metadata=None)`,
  `with chassis.lock(name)`, `context.paths.lock(name)`, and `NamedLockRegistry` over
  `var/locks/<name>.lock`: non-blocking, bounded, or blocking acquisition, `LockTimeoutError`
  naming the live holder, stale-owner detection for files left by a dead process, and
  `release_all()` as part of shutdown.
- **Durable project storage**: `core.paths.durable_dir` (default `data/`) is the project
  role, kept apart from the ephemeral `state_dir`: `paths.durable_dir`,
  `with_durable_dir()`, `durable_module_dir(name)`, and `context.paths.durable_dir`.
- **Periodic task scheduler**: `context.schedule(interval_seconds, callback, name=,
  first_delay=, repeat=, module=, run_now=)` and `TaskScheduler` (`run_due`, `run_task`,
  `unschedule`, `stats`). Tick-driven by default so tests need no sleeps;
  `core.scheduler.threaded` moves it to a named thread; a failing task is counted, logged,
  published as `core.task.failed`, and reported by `kdx doctor`; every task stops inside
  `core.lifecycle.stop_timeout`.

### Changed

- Naming purge: the console installs two entry points (`kdx`, `korodexios`); the
  `korodex` alias, the `KORODEX_` environment prefix, the `korodex.yml` config
  candidates, and the `.korodex-root` marker are retired.
- Module manifests are `kdx_module.toml`; the rotating log file defaults to
  `var/log/kdx.log`; the control-plane token header is `X-Kdx-Token`; loggers
  live under `kdx.*`; the internal record attribute is `kdx_fields`.
- Public API renames, moving the core API from `v1` to `v2`: `KorodexError` to
  `KorodexiosError` and `KorodexLogger` to `KorodexiosLogger`.
- `kdx doctor` grew "Instance and concurrency" and "Diagnostics" sections, reports the
  daemon behind the discovery file, and exits `1` for a failed module or an error
  diagnostic; `doctor --json` carries `instance`, `async`, `supervision`, and
  `diagnostics`.
- `kdx config check` reports module schema findings next to unknown keys and exits `3`
  for either class of problem.
- The control-plane token compare is constant-time (`hmac.compare_digest`).
- Coverage ratchet raised to 84% (measured 85% across 320 tests), then re-raised to 86%
  (measured 87% across 405 tests) with the new primitives under test.
- `kdx doctor --json` and `kdx status` gained `scheduler`, `locks`, and `static` sections;
  `context.as_dict()` gained `scheduler` and `locks`.
- `/api/v1/schema` gained `content_type` per route and a `static_mounts` list; the contract
  `schema` version stays `1` because the change is additive.

### Added

- `GET /api/v1/schema` on the control plane: the machine-readable contract
  (routes, auth flags, response fields), versioned by `IPC_SCHEMA_VERSION`.
- Global CLI flags for the remaining core settings: `--tick-interval`,
  `--stop-timeout`, `--ipc-timeout`, `--no-ipc`, `--allow-anonymous-shutdown`,
  `--logs-poll-interval`, `--log-max-bytes`, `--log-backup-count`, `--cli-width`,
  `--confirmations`/`--no-confirmations`, `--no-autodiscover`,
  `--enable-module`/`--disable-module`, and `--name`.
- Per-module READMEs, this changelog, and `.gitignore` coverage for
  `kdx.local.*` and `korodexios.local.*`.

### Fixed

- Windows portability of the memory probes: `os.sysconf`, `os.getrusage`, and
  `os.RUSAGE_SELF` exist only on POSIX, so page size and physical memory now
  resolve through `korodexios_core.units` helpers with an explicit fallback
  (4 KiB page, `unavailable` source) instead of bare attribute access that the
  Windows platform check rejects.
- CI: the Windows type-check step published nothing because the runner starts
  bash with `-e`; the mypy status is captured before errexit ends the step, and
  the output is published on the `ci/windows-mypy` branch.
- `kdx rpc` and `kdx reload` no longer print nothing when no daemon answers:
  `IpcDiscovery.load()` returns `None` for a missing discovery file, and both
  commands say so and exit with a hint.
- Command collection is a kernel lifecycle step, not a CLI side effect:
  `Chassis.initialize()` collects each module's `register_cli` tree into a headless
  surface, so `registry.commands()` is populated at boot and `POST /api/v1/rpc` answers
  an embedded daemon that never imported `cli/entry.py` (it used to answer `404`).
  `collect_cli(surface)` now replays the recorded commands instead of calling the hook
  twice.

## [0.1.0] - 2026-10-20

Production release of the chassis: microkernel lifecycle, layered configuration
with provenance, structured logging, the event bus, module discovery and the
registry, signal-safe shutdown, the argparse CLI with the rich wizard, the
built-in proof modules (`heartbeat`, `system_info`, `backup_engine`), the
loopback-by-default IPC control plane, the `kdx init` project seed, and the
3-OS x 3-Python CI matrix.
