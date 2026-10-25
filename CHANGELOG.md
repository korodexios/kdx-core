# Changelog

Notable changes to korodexios-core. Versions follow the git tags; the phase
history lives in [docs/ROADMAP.md](docs/ROADMAP.md).

## [Unreleased]

_Nothing pending: the working tree is the `v0.2.4` tag._

## [0.2.4] - 2026-10-26

The supervision dogfood closes: a shipped built-in module now runs a worker thread the
chassis watches, joins on shutdown, and can report when it dies. Two edges that only a
running system could find are fixed, and the coverage ratchet moves up a point. Nothing in
the frozen surface changed shape - no new export, no changed signature, no renamed
configuration key.

### Added

- **`system_info` samples the load average on a watched worker**: the module starts a
  `kdx-system-info-load` daemon thread that reads `os.getloadavg()` on an interval, caches the
  reading, and publishes `system.load.sampled`. It reports that thread through `workers()`, so for
  the first time `WorkerSupervisor`, `core.worker.lost`, and `kdx doctor` are pointed at shipped code
  rather than a test fixture: a sampler that stops on its own moves `system_info` to `failed` the
  way the roadmap always claimed it would. Two scoped keys control it - `watch_load` (default
  `true` where the platform has a load average, `false` where it does not) and `load_interval`
  (default `5.0` seconds, clamped) - and a platform that cannot answer the reading parks the
  watcher with one warning in the log instead of spinning a thread that reads nothing.

### Fixed

- **A scheduled task may withdraw itself mid-sweep**: a callback that called `unschedule()` on
  its own name, or on a neighbour, was acting on tables the running sweep was still reading.
  `run_due()` now takes its list of due tasks as a snapshot with the guard held, calls each
  callback with the guard released, and skips a task an earlier callback withdrew or replaced -
  so self-cancellation is a decision the sweep honours, not a missed beat, and `tasks()`,
  `task()`, `pending()`, and `next_due_in()` answer from an atomic copy even while the scheduler
  thread and a CLI thread both reach for the same tables.
- **The control plane means its own `Connection: close`**: `_ControlHandler` sent the header
  without setting `close_connection`, so an HTTP/1.1 client that offered keep-alive got its
  answer and then waited on a socket the chassis was never going to reuse. The handler sets the
  flag on every response: one request, one answer, one closed socket - a keep-alive client can
  hang up now instead of timing out.
- **The built-in modules report the state they are in**: `backup_engine` and `system_info` overrode
  `start()` and `stop()` without the base hooks, so a running module answered `initialized` to
  `health()`, `kdx doctor`, and `kdx modules show`, and a stopped one never answered `stopped`. Both
  call `super()` now, which is also what makes a supervision finding readable: a module whose
  worker died is `failed`, not a module that never started.

### Changed

- **The coverage floor moved from 86 to 87.**: the ratchet is one point under the measured
  total, `v0.2.3` measured 87.63%, so `COVERAGE_FLOOR` in the `Makefile` and the working agreement now
  read `87` and a new untested branch fails the bar.
- **`system_info` speaks for itself in `kdx doctor`**: the module declares `diagnostics()` - the
  configured disk path and the watcher thread, alive, parked by configuration, parked for the
  platform, or stopped - and its `health()` block carries `watch_load`, `load_interval_seconds`,
  `load_reads`, `load_average`, and `load_watcher`. Counters describe the current run: a chassis that
  re-initializes the module starts counting again.

## [0.2.3] - 2026-10-26

The last built-in habits move to chassis primitives, and the rough edges found by running
them are smoothed away. Nothing in the frozen surface changed shape - no new export, no
changed signature, no renamed configuration key.

### Fixed

- **Windows `SIGBREAK` exits 130**: `ExitCode.from_signal()` computes `128 + signum`, and `SIGBREAK` is
  `21` on Windows, so an interrupt that behaves exactly like Ctrl+C left the process with `149`.
  The coordinator records it as the `SIGINT` number now - one gate, one exit code - while the log
  line still names the signal that actually arrived. Where the platform has no `SIGBREAK` the
  mapping is a no-op, so no number is claimed for a signal that does not exist.
- **Relative `--config` and `--env-file` are anchored to `--home`**: the CLI resolved a relative path
  against the current working directory and path discovery asked `.is_file()` on that answer, so
  `kdx --home /srv/app --config kdx.yml` from another directory reported a file that does exist. A
  relative project file is now joined to the resolved root exactly once, an absolute path is
  used exactly as given, and the error names the path under the root that was really read.
- **A JSON log field keeps its type**: `JsonFormatter` ran every structured kwarg through
  `_shorten()`, so `"copied": "12"` reached the shipper and a filter had to parse strings. Scalars,
  mappings, and sequences keep their JSON types now; only what JSON cannot hold - a `Path`, an
  exception, a custom object - is shortened to text.
- **A named lock that cannot write its payload withdraws the claim**: the payload write
  swallowed the `OSError` after the `flock` succeeded, leaving a handle that reported `held=True` over a
  lock file naming nobody. The descriptor is released and the error re-raised, so the next
  acquirer starts from a clean claim.

### Added

- **`backup_engine` guards each run with a named lock**: the module takes `backup_engine` through the
  chassis lock primitive before it copies, so a scheduled sweep and a manual `kdx backup run` never
  write one destination at the same moment. The refused run raises `BackupError` naming the lock
  ("another backup is running: named lock 'backup_engine' is busy") with a hint, `kdx backup run`
  answers with exit `1` and that hint instead of a traceback, and `kdx doctor` grows a `lock` row once a run
  has stepped aside. The lock file stays behind with its payload, so `kdx doctor` can say who is
  copying right now.
- **`LockTimeoutError` is a real export**: the guide told module code to import it from
  `korodexios_core` and `korodexios_core.kernel`; the name is in both `__all__` lists now, so the documented
  import is the one that works.

### Changed

- **`heartbeat` ticks on the chassis clock**: the manual `threading.Thread` and its `_beat_loop` are gone,
  replaced by one `ticker` task scheduled through `context.schedule()` on the module interval. The
  built-in owns no thread, so `workers()` reports nothing, worker supervision has no thread to
  watch, and shutdown has nothing to join - stopping unschedules the task, so a stopped chassis
  leaves no ticker behind. `heartbeat status` and `health()` speak the same language now: `ticker`
  `scheduled`, `ticker runs`, and `ticker errors` replace `thread alive`, and `kdx doctor` answers whether the
  ticker is alive, parked after N ticks, or failing.

## [0.2.2] - 2026-10-26

A hardening and dogfooding patch on the core API `v2` line: three edge-case defects
closed, Windows path normalisation in static mounting made strict, and the built-in
proof modules finally eating their own dogfood. Nothing in the frozen surface changed
shape - no new export, no changed signature, no renamed configuration key.

### Fixed

- **Named-lock race**: `NamedLockRegistry.hold()` inspected the registry outside its guard
  and reserved inside it, so two threads could both pass the check and then race into
  `acquire()` - paying a timeout for a collision that was already decided. Inspection and
  reservation now happen in one guarded step: the loser of a same-name `hold()` raises
  `LockTimeoutError` immediately, and a claim that fails leaves no reservation behind.
- **Static mounts on Windows paths**: `IpcStaticMount.resolve()` unquotes the subpath before
  validating it (`%5c`, `%2e`, `%3a` are separators and modifiers, not file names), folds `/` and `\` to
  one separator, refuses Windows drive letters, alternate data streams
  (`report.txt:Zone.Identifier`), reserved device names (`NUL`, `nul.txt`, `com1.log`), and a
  stray NUL byte, and verifies containment on the resolved path - with a case-folded
  second look on case-insensitive filesystems, so a legitimate request is never
  answered with a spurious 404.
- **Per-tick platform probe**: `sys_is_linux()` no longer imports `platform` and calls
  `platform.system()` for every memory reading (one per status snapshot and heartbeat tick);
  the answer is cached in `IS_LINUX` from `sys.platform` at import.
- **Scheduler bookkeeping**: a task's `last_error` is capped at `MAX_ERROR_LENGTH` (500) characters,
  marked with `…`, because the traceback belongs in the log, and `unschedule()` now purges the
  task table, the declaration order, and the suffix bookkeeping with the task. A number
  the scheduler handed out is not recycled while another task of the same base is live,
  so `sweep-2` in an old log line is never a different task, and `MAX_NAME_SUFFIX` bounds the
  numbering instead of letting it grow quietly.

### Added

- **The built-in proof modules declare the v2 hooks themselves**, which is how the
  contract gets proven: `backup_engine` declares all seven of its scoped keys (`source`, `dest`,
  `include`, `exclude`, `max_size`, `dry_run`, `follow_symlinks`), reports whether its source tree is
  readable and its destination writable, and serves `GET /api/v1/backup/plan`; `heartbeat`
  declares `interval` and reports whether its ticker thread is alive while the module is
  running; `system_info` declares `disk_path` and `include_cpu_model` and serves
  `GET /api/v1/system/snapshot`. `kdx config check` validates three module schemas out of the
  box, and `GET /api/v1/schema` answers the two new routes.
- Nineteen regression tests: `tests/test_locks.py` (reservation, threaded claim races),
  `tests/test_v022_dogfood.py` (declarations, `kdx config check`, the live schema route), new
  cases in `tests/test_ipc.py` (Windows-style traversal) and `tests/test_scheduler.py` (the
  bounds), and updated expectations for the built-in schemas in `tests/test_cli_v2.py`.

### Changed

- `kdx config check` prints `backup_engine: 7 keys`, `heartbeat: 1 keys`, and `system_info: 2 keys` under
  `"Module configuration schemas"` instead of `"no module declares a configuration schema"`.
## [0.2.1] - 2026-10-25

A maintenance patch on the core API `v2` line: two defects found during downstream production
integration, and one exit-code invariant made explicit. Nothing in the frozen surface
changed shape - no signature, no route, and no configuration key was renamed or removed.

### Fixed

- **Subkeys under a mapping default are valid paths**: `core.env.aliases` - and any
  mapping-valued chassis key, derived from the defaults table rather than hard-coded - is
  read as a namespace, so `core.env.aliases.LEGACY_RUN_MODE` no longer fails
  `kdx config check` with exit `3`. A malformed subkey (`core.env.aliases.`) and a deeper path
  stay unknown, so a real typo still surfaces.
- **The `did you mean` answer is scoped to the key it names**: a typo inside a module
  namespace is suggested against that module's own keys
  (`modules.heartbeat.intervl` → `modules.heartbeat.interval`), an unloaded module suggests only a
  near namespace (`modules.backup.root` → `modules.backup_engine`), and an unrelated loaded module
  is never dragged in as the suggestion.
- **Hook ordering in inspection commands**: `kdx doctor` and `kdx modules show` initialize the
  discovered modules before asking `diagnostics()` or `health()`, so a module that reads its
  configuration, paths, or logger in those hooks no longer answers `ModuleContractError`:
  `used before init(context) ran`. The registry collects the declarative hooks
  (`config_schema()`, `ipc_routes()`, `static_dirs()`, `diagnostics()`) again once `init(context)` succeeded, so
  `kdx config check`, `kdx doctor`, and the control plane see the module's real declarations, and
  the expected first pass is logged at debug level instead of as a boot error. A module
  that needs its context answers those hooks with an empty or baseline report until then.

### Added

- **Verdict exit codes are part of the CLI contract**: `main()` returns the integer a command
  handler produced, so `kdx <module> <command>` may exit `4` (or `42`, `77`) and a deployment script
  branches on it; only the chassis' own failures map to `ExitCode`. Asserted end to end in
  `tests/test_patch_v021.py` and documented in `docs/CLI_REFERENCE.md`.
- Eleven regression tests cover the three fixes: alias subkeys through `kdx config check`,
  suggestion scoping, pre-init hook resilience, the collected-again-after-init path, and
  exit-code pass-through.

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
