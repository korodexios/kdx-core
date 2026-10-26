# Changelog

Notable changes to korodexios-core. Versions follow the git tags; the phase
history lives in [docs/ROADMAP.md](docs/ROADMAP.md).

## [Unreleased]

_Nothing pending: the working tree is the `v0.3.1` tag._

## [0.3.1] - 2026-10-26

A portability patch on the frozen `v2` line: the Windows and macOS CI legs that `v0.3.0`
left red are green again, and the fixes are in the chassis, not the runners. Nothing in the
frozen surface changed shape - no new package-root export, no changed signature, no renamed
configuration key; the changes are additive behaviour on the platforms the chassis already
claimed to support.

### Fixed

- **`pid_alive` is now honest on Windows**: POSIX asked the kernel with `kill(pid, 0)`, but
  Windows `os.kill` routes through `TerminateProcess` and answers nothing useful for a
  probe, so a dead high PID read as *live* and a stale instance lock was never detected
  (six tests in `test_instance_lock` failed on every Windows leg since `v0.2.9`). On `win32`
  the answer now comes from `OpenProcess` - the same primitive `tasklist` uses, via stdlib
  `ctypes`, no new dependency - and `IpcDiscovery.is_process_alive` reuses the shared
  `pid_alive` instead of carrying a second copy of the buggy `os.kill` logic, so a stale
  discovery file no longer makes `kdx status` hang reaching a daemon that is gone.
- **The control-plane shutdown is requested before it is promised**: the `POST /api/v1/shutdown`
  handler committed its `200 {"accepted": true}` reply before calling `request_shutdown()`,
  so a client that saw `accepted` could not rely on the shutdown having been asked for. On
  a slow runner (the macOS arm64 legs) the response landed first and `calls["shutdown"]` was
  still `0` when the test checked, failing `test_shutdown_post_asks_the_chassis_to_stop` and
  `test_anonymous_shutdown_when_configured`. The handler now asks, then replies, so the
  contract the payload advertises holds on every host.
- **The backup engine and its tests stop assuming the host separator**: `plan.files` and the
  size assertions compared `Path.relative_to` output, which carries `\` on Windows, against
  forward-slash strings (and the fixtures wrote text that Windows translated to CRLF, drifting
  the byte count by two). The assertions now compare in `.as_posix()` form and the fixtures
  write bytes, so a backup plans and sizes the same on every platform.
- **The IPC tests respect the platforms they run on**: `process_memory` on Windows answers
  `unavailable` (no `/proc`, no `getrusage`), which the test now accepts; the discovery-file
  tests that assert `0o600` mode bits and `os.replace` over a live file are marked `posix`, the
  marker this suite already uses for exactly that kind of behaviour.

### Added

- Regression tests: `pid_alive` routes to the `win32` probe (not `os.kill`) and the probe's
  answer flows out; the shutdown handler requests before it replies, so `accepted: true` is a
  promise the runner cannot race past.

## [0.3.0] - 2026-10-26

A consumer minor on the frozen `v2` line, additive in every direction: a project can state
one identity and hear it back the same way - `core.project.name`, `.version`, `.contract_dir`,
answered from `core.name`, the version of `core.distribution`, and the home when the project
stays silent, and printed identically by `kdx doctor`, `kdx status --json`, and `kdx contracts
check`; a value can now mean *no opinion here* - `core.config.sentinels` and a module's
`ConfigField(sentinel=...)` let a layer fall through to the layer below, whose provenance reads
`inherit sentinel`, while `kdx config show` prints `(unset)` instead of presenting the token as
a set value and schema validation stops calling the token an invalid value; and a project or
its modules can declare a verified file set through the optional `contracts()` hook or
`ChassisOptions.project_contracts`, which `kdx contracts check` hashes, reporting drift or a
missing file at exit `3` and rewriting nothing - the classification and merge-policy words stay
the project's own. New exports: `ProjectIdentity`, `project_identity`, `ContractRow`,
`ContractDrift`, `ContractReport`, `contract_rows`, `verify_contracts`. No import moved, no
signature changed, no configuration key renamed.

## [0.2.10] - 2026-10-25

A consumer patch on the frozen `v2` line: the two shapes of `core.env.aliases` both work,
a version-mismatched module can no longer boot past the chassis that promised not to run on
it, the chassis says out loud which modules it skipped and which keys it owns, a project can
ship plugins as an installed distribution, and the bootstrap documentation finally admits
that uv (Astral) owns the virtualenv. Nothing in the frozen surface changed shape - no new
package-root export, no changed signature, no renamed configuration key; two documented
opt-in configuration keys were added.

### Fixed

- **Both environment-alias shapes are read, at the same time**: a mapping written under
  `core.env.aliases` in `kdx.yml` reaches the chassis flattened to
  `core.env.aliases.NAME` leaves, which `_aliases_from` never looked at, so the alias was
  declared, valid to `kdx config check`, and silently inert. The loader now reads the exact
  key and its leaves from the file, local and CLI layers, and reads the `NAME=key` list an
  operator exports as `<PREFIX>_CORE__ENV__ALIASES` for the resolved prefix instead of the
  two hard-coded variable names - so `KDX_CORE__ENV__ALIASES=LEGACY=core.key` and
  `core: env: aliases: {LEGACY: core.key}` are two spellings of one decision. The status
  snapshot prints `LayeredConfig.env_aliases()`, the effective map, instead of
  `dict(config.get("core.env.aliases") or {})`, which raised `ValueError: dictionary update
  sequence element #0 has length 1` on the list shape and took `kdx doctor` down with it.
  Provenance still names the variable that carried the value, and a properly prefixed
  variable still outranks an alias mapped to the same key.

### Added

- **`core.modules.strict_discovery` (default `false`)** is the loud version gate a consumer
  asked for: `requires_core` was enforced by discovery and then only logged, so a module
  needing a core the installed chassis is not produced a boot that exited `0` on three of
  four modules. With the switch on, `Chassis.start()` raises a configuration error -
  `kdx run --once` exits `3` with one line naming the module, the stage, and the spec it
  required - while `kdx doctor` and `kdx status` still run and report the same finding as an
  `error` severity check, so `kdx doctor` exits `1`. Inspection commands never refuse. The
  `requires_core` grammar is unchanged: `==`, `>=`, `<=`, `!=`, `>`, `<` and
  comma-separated sets.
- **`core.modules.foreign_roots` (default `[]`)** names the import roots a project ships as
  an installed distribution, so `llmwiki_ext.plugins.x` is someone else's missing package
  and stays an optional-dependency hint with an install command - including when that root
  collides with the module's own package name. The set is read through the typed getter, so
  one variable can carry it (`KDX_CORE__MODULES__FOREIGN_ROOTS=llmwiki_ext,other_ext`).
  Nothing is foreign until the project says so: the chassis' own roots stay hard failures.
- **A module the chassis did not load is announced at boot** - one folded line naming each
  skipped module with the reason the chassis actually decided on (`disabled by its
  manifest`, `not listed in core.modules.enabled`, `listed in core.modules.disabled`,
  `modules.<name>.enabled is false`), replacing one reason string that covered four
  decisions - and `kdx modules list` repeats the same rows, with the optional-dependency
  rows, below the table. `kdx modules list --json` gained the additive `skipped`,
  `optional`, and `failures` keys beside the health block.
- **A module that declares a chassis-owned key is named, not skipped in silence**:
  `modules.<name>.enabled` and `modules.<name>.priority` are read before a module is
  imported, so a module that declared `enabled` for itself handed the chassis a lifecycle
  decision - the behaviour switch went false, the module vanished from the registry, and its
  remaining keys were then reported as unknown configuration. Registration warns per
  collision, `ModuleRegistry.settings_collisions()` reports them, `kdx doctor` carries them
  as a `module-setting` warning, and `kdx modules list --json` / `kdx modules show` carry a
  `settings_collisions` row per module.

### Changed

- **The bootstrap documentation leads with uv (Astral)**, because the venv is uv's:
  `uv venv`, `uv sync --extra dev`, `uv run kdx ...`, `uv tool install korodexios-core`,
  `uv pip install "korodexios-core @ git+…@v0.2.10"`, and `uv lock` for a consumer's own
  pins, with `python3 -m venv` + `pip` kept as the documented fallback and a note for the
  PEP 668 `EXTERNALLY-MANAGED` host that has neither `pip` nor `ensurepip`. `kdx init` now
  prints `next: … && uv sync --extra dev && uv run <name> run` followed by the one-line pip
  fallback, the agent guide's verification loop runs under `uv run`, and
  `--chassis-source git` is documented as needing network plus uv at install time.

## [0.2.9] - 2026-10-25

Test-only patch: two lock assertions measured wall-clock time tighter than a shared CI
runner can deliver, which is how a Linux suite that passes on the reference machine lost
a `make check` leg on one host. The semantics stay the same - a claim that cannot take the
lock fails at once, and a claim with a timeout waits it out - only the tolerance moves.

### Fixed

- **The lock timing bounds stop measuring the runner**: `test_locks` demanded a same-name
  claim fail inside 50 ms and `test_named_locks` demanded that a 200 ms timeout measure at
  least 150 ms, so a descheduled thread on a loaded runner could fail the suite with no
  chassis defect. The bounds now carry runner-sized slack - 500 ms for a call that must not
  wait at all, a 120 ms floor and a 1.2 s ceiling for a 200 ms wait - and each assertion
  says in its message which semantic it is really protecting.
## [0.2.8] - 2026-10-25

Maintenance patch on the platform edges: the two places that reached for a POSIX-only
call now answer for their platform once, at import, so a Windows type check is clean with
no `type: ignore` left on a POSIX attribute, and a tag's release artifacts stop waiting on
hosts that only report. Nothing in the frozen surface changed shape - no new package-root
export, no changed signature, no renamed configuration key.

### Fixed

- **The instance lock and the memory probe resolve their platform at import**:
  `instance_lock` maps one private pair of calls onto `msvcrt.locking` on Windows and
  `fcntl.flock` on POSIX, and `ipc.process_memory()` reads `resource.getrusage` only where
  that module exists, so `mypy --strict --platform win32` reports no errors for a daemon
  that only ever runs there. The lock also stopped carrying a platform module reference it
  never called.

### Changed

- **A release follows the Linux bar**: the CI matrix marks a macOS or Windows failure
  `continue-on-error`, so a tag publishes its wheel and sdist once the Linux legs and the
  package audit pass instead of publishing nothing because one host went red. The
  portability hosts keep running, keep reporting, and keep their diagnostics branch.
## [0.2.7] - 2026-10-25

Maintenance and hardening patch: retention stops being a side effect of a copy, a
threaded scheduler says which task it could not stop, a malformed route declaration is
refused before it ever reaches the socket, and the distribution artifacts are audited by
the suite. Nothing in the frozen surface changed shape - no new package-root export, no
changed signature, no renamed configuration key.

### Added

- **`backup_engine` retention sweeps on its own clock**: `modules.backup_engine.prune_interval`
  (float, default `0.0`, disabled) declares an independent `auto_prune` maintenance task through
  `context.schedule()` whenever it is above `0` and `auto_prune_days` is above `0`, so archives
  age out on the retention interval even when copies are weekly, manual, or never happen at all.
  The sweep takes the same `backup_engine` named lock a copy takes, so an archive is never
  deleted beside a copy that is still landing; a sweep that finds the lock busy steps aside,
  counts the refusal, and says so in `kdx doctor` rather than reporting a failing task. The old
  inline behaviour is untouched at `0.0`. `health()` carries `prune_interval_seconds`,
  `prune_scheduled`, `prune_sweeps`, `prune_sweeps_refused`, `prune_first_due_seconds`,
  `prune_last_run_seconds_ago`, `prune_last_removed`, `prune_reclaimed_bytes`, and
  `prune_reclaimed`; `diagnostics()` answers with a `retention` row for each honest state - left
  to the copy, armed with no window to age out, no clock to run on, task withdrawn, alive and
  reporting what it took back. `backup.pruned` now carries `reclaimed_bytes` beside `days` and
  `removed`, because a retention window an operator cannot measure is a guess.
- **The scheduler reports the callback it is running**: `TaskScheduler.active_task` and
  `active_seconds()` name the task a worker is inside and how long it has been there, and
  `scheduler.stats()` gained `active_task`, so `kdx status` and `kdx doctor --json` can say who
  is holding a stop open instead of leaving the operator with a thread name.
- **The distribution audit is declared, not remembered**: `pyproject.toml` names what must never
  ship for the wheel and the sdist (`var/`, `*.log`, `*.tmp`, byte code, the dev caches,
  `.coverage`, `dist/`, environments) instead of relying on `.gitignore`, which a container that
  builds from a tarball has no way to read.

### Fixed

- **A threaded shutdown names the task it could not stop**: with `core.scheduler.threaded: true`,
  `stop(timeout)` used to report only that the `kdx-scheduler` thread was still alive. It now
  reports the callback that is still running - its name in the message, with `task` and
  `running_seconds` on the log record - and says so plainly when a live thread has no task in it,
  so a stop that overran its budget points at work instead of at a thread.
- **A stop is never answered with new work**: `run_due()` checks the stop flag again immediately
  before it launches each callback, so a task queued behind a slow one is skipped rather than
  started past the shutdown budget. Skipping is not losing: the queued task keeps its schedule
  and runs the next time the clock is open.
- **An `IpcRoute` can no longer smuggle a header**: `IpcRoute.__post_init__` refuses a path
  carrying whitespace, a CR, an LF, NUL, or DEL, and refuses a `methods` entry that trims to
  nothing or sits outside `GET`, `POST`, `PUT`, `DELETE`, `PATCH`, `HEAD`, `OPTIONS` - each with
  an `IpcError` and a hint at declaration time, so the refusal lands at registration and a
  malformed route never enters the server table.

### Changed

- **`IpcRoute` methods are normalised rather than guessed**: entries are trimmed and upper-cased,
  duplicates collapse, and an empty tuple keeps the documented `GET` default
  (`DEFAULT_ROUTE_METHODS`). What used to be a silent default - a blank entry vanishing into
  `GET` - is now a refusal, which is what the tuple said it meant.
- **The suite audits what ships**: `tests/test_packaging.py` builds the wheel and the sdist and
  refuses any `var/`, `.log`, `.tmp`, byte-code, or dev-cache entry, while requiring `py.typed`,
  the module manifests, `docs/`, `llms.txt`, `LICENSE`, and `Makefile` inside them.
- **The version moves to `0.2.7`** with the above; `docs/`, `llms.txt`, and `docs/ROADMAP.md` are
  synchronized to the patch, and `modules.backup_engine` declares 10 configuration keys.

## [0.2.6] - 2026-10-25

Maintenance patch: the four edges a running daemon and a second copy of the CLI
could find are closed. A stamp that once meant "the same folder" now means a
fresh one, a lock a killed process left behind is adopted instead of forcing a
manual delete, the control plane stops buffering an unbounded body, and a
test that leaned on POSIX mode bits now knows when the platform will not honour
them. Nothing in the frozen surface changed shape - no new export, no changed
signature, no renamed configuration key.

### Fixed

- **A same-second backup no longer clobbers its neighbour**: `STAMP_FORMAT`
  carries one second of resolution, so two runs inside one second - rapid CLI
  invocations, a sub-second schedule interval, a fast test run - planned the
  identical `dest/<stamp>` folder and overwrote each other. `build_plan()` now
  appends an incrementing suffix (`-1`, `-2`, ...) whenever `dest / <stamp>`
  already exists, so every planned backup owns a distinct, isolated destination
  directory.
- **A dead PID no longer strands the lock file**: a daemon killed by `kill -9`,
  a power loss, or an OS crash left its lock file in `var/` with the old JSON
  payload inside, and `acquire()` raised `InstanceLockError` / `LockTimeoutError`
  because the file still claimed the name, even though `holder.is_alive` was
  `False`. Both `InstanceLock` and `NamedLock` now check, after the OS-level lock
  succeeds, whether the recorded owner is gone; when it is, the lock is taken
  over, the payload is atomically rewritten with the current process, and the
  acquisition proceeds cleanly. A lock whose owner is still alive is still
  refused, because its descriptor is the one the other process actually holds.
- **An oversized IPC body is refused before it is buffered**: `_ControlHandler`
  read whatever `Content-Length` announced straight into memory, so an untrusted
  or malformed request could exhaust the daemon before any handler ran. The
  control plane now defines `MAX_IPC_PAYLOAD_BYTES` (10 MiB) and answers a body
  over the limit with HTTP `413 Payload Too Large` without reading a byte of it.
- **A permission-refusal test no longer assumes POSIX**: a test that asserts a
  write is denied by a `0o400` / `0o500` directory skips on Windows, where
  directories ignore POSIX mode bits (NTFS maps them onto the read-only flag),
  instead of failing on a platform that never honours the mode.

### Changed

- **The version moves to `0.2.6`** with the four fixes above; `docs/`,
  `llms.txt`, and `docs/ROADMAP.md` are synchronized to the patch.

## [0.2.5] - 2026-10-26

The last primitive pairing lands in shipped code: a built-in module now takes a named lock from a
task the chassis clock started. Three rough edges that only a running control plane and an editor
could find are smoothed. Nothing in the frozen surface changed shape - no new export, no changed
signature, no renamed configuration key.

### Added

- **`backup_engine` copies on the clock, under the lock**: `modules.backup_engine.schedule_interval`
  (default `0.0`, which leaves copying to the operator) declares an `auto_sweep` periodic task, and the
  copy that task starts takes the same named lock a manual `kdx backup run` takes. That is the pairing
  the roadmap asked for - `context.schedule()` and `NamedLock` proven together by shipped code - and it
  changes what a collision means: a sweep that arrives while a copy is running steps aside by
  decision of the lock, counts the refusal, and says so in `kdx doctor` rather than reporting a task
  that keeps failing. `modules.backup_engine.auto_prune_days` (default `0`, keep every copy) ages stamped
  copies out of the destination after a copy lands: only a child whose own name is a backup stamp
  is a candidate, so a folder the operator made by hand is never a backup, and the copy that just
  landed is always kept. `health()` carries `sweep_interval_seconds`, `sweep_scheduled`, `sweeps`,
  `sweeps_refused`, `sweep_first_due_seconds`, `auto_prune_days`, and `pruned`; `diagnostics()` answers with a `sweep` row
  for each of the four honest states: left to the operator, waiting for a clock, gone, running.

### Fixed

- **The discovery file is published in one filesystem step**: `IpcServer` truncated `var/chassis.ipc`
  in place, so a reader that polled the file during boot - `kdx status`, an external health check -
  could find zero bytes, fail to decode them, and conclude the daemon was dead. The payload now
  lands in a `0600` sibling the process owns and is renamed into place with `os.replace()`, so a reader
  finds a complete announcement or the previous one, never a half-written one.
- **A socket that closes during teardown stops raising**: stopping the control plane closes the
  listening socket while a worker thread may still be answering a client, and `BrokenPipeError`,
  `ConnectionResetError`, or an `OSError` carrying `EBADF` escaped the handler to stderr. The handler
  recognises those endings, ends the connection, and records one line - `debug` while the chassis is
  stopping, `warning` while it is still running. A fault that is not a socket is still left to rise:
  `kdx doctor` loses no sleep over a peer that hung up, and nobody hides a real bug.
- **A locks scan answers with locks**: `AppPaths.named_lock_files()` and
  `NamedLockRegistry` listed any file ending in `.lock`, so an editor's `.backup.lock` swap file, a dot
  file, and a stray `notes:bad.lock` appeared to `kdx doctor` as held or stale names. Both scans now ask
  `lock_name()` whether the stem is a name a lock could have written, and refuse the artefacts.

### Changed

- **Module counters describe the run the operator is looking at**: a chassis that re-initializes
  `backup_engine` starts counting again, so `runs`, `refusals`, `sweeps`, and `pruned` are today's numbers and
  `kdx doctor` never blames yesterday's refused copy on today's.

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
