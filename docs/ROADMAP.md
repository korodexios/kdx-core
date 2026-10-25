# Roadmap

The chassis ships in phases, each one ending with `make check` green and the docs
updated in the same commit. Phase numbers are historical: they record the order the
work landed in, not a promise about the future. This file is the phase history and
the short list of what ships next; the README keeps one sentence about it.

## Phases

| Phase | Scope | State |
| --- | --- | --- |
| 1 | Packaging, config hierarchy, logging, event bus, module contract, discovery, registry, signals, chassis, argparse CLI, `heartbeat` proof of concept, tests | **delivered** |
| 2 | `rich` UX layer: numbered-menu wizard over the registered commands, `[default]` prompts, `while True` validation loops, validators (port/IP/path/size/duration), `--yes` + non-TTY auto-detection | **delivered** |
| 3 | Built-in `system_info` (OS/CPU/RAM/disk diagnostics) and `backup_engine` (validated source to destination backups with dry runs), full test coverage, docs polish | **delivered** |
| 4 | Review hardening (event-bus recursion guard, slow-handler timing, plugin import isolation, backup destination and symlink guards, ASCII tag fallback), module dependency DAG (`depends_on`), `kdx modules create` scaffolding | **delivered** |
| 5 | Daemon IPC: loopback HTTP control plane (`/health`, `/api/v1/status`, `POST /api/v1/shutdown`), discovery file with a shared token, and the `kdx status` command | **delivered** |
| 6 | `kdx init <name>`: zero-stub project seed - pyproject, kdx.yml, Makefile, README, AGENTS.md, `.agents/skills/`, a first module, smoke tests, optional `web/` client; `--minimal`, `--force`, `--ecosystem` | **delivered** |
| 7 | Distribution readiness: dynamic version from `version.py`, audited wheel and sdist (`py.typed` + every module manifest), isolated `uv tool` / `pipx` install, the `kdx logs` operator helper, and a 3-OS x 3-Python CI matrix | **delivered** |
| 8 | `kdx` as the primary short alias (`korodexios` canonical), LAN-bindable control plane (`core.ipc.host`), optional auth (`core.ipc.auth`, `--no-auth`), static token (`core.ipc.token`), the anonymous-shutdown guard, and `kdx status --target` | **delivered** |
| 9 | Naming alignment: `kdx.yml` / `korodexios.yml` config candidates, a `kdx.local.yml` override layer, `KDX_` as the primary env prefix (`KORODEXIOS_` understood), and `kdx`-first docs | **delivered** |
| 10 | Production release: `v0.1.0` tagged with the version synchronized across `version.py`, docs, and artifacts | **delivered** |
| 11 | Release pipeline: a `v*` tag runs the full 3-OS x 3-Python matrix and the package audit, then publishes the audited wheel and sdist to a GitHub Release; a branch push runs a trimmed matrix for fast feedback | **delivered** |
| 12 | Operator ergonomics: `kdx logs --rotate` (archive the file, the running chassis reopens the path), `kdx doctor --json` for machines, and the i18n convention for seeded projects (stdlib gettext, source strings as the fallback) | **delivered** |
| 13 | Configuration honesty: unknown-key detection at boot, `kdx config check` as a CI gate, a versioned `doctor --json` payload (`schema: 1`), a seed that installs the chassis from the repository today (`--chassis-source`), a CI workflow contract test, and a coverage ratchet in `make check` | **delivered** |
| 14 | Naming purge: two console scripts (`kdx`, `korodexios`), `kdx_module.toml` manifests, `kdx.log`, `X-Kdx-Token`, `kdx.*` loggers, `KorodexiosError`/`KorodexiosLogger` (core API `v2`), the `GET /api/v1/schema` contract endpoint, global flags for every core setting, per-module READMEs, and `CHANGELOG.md` | **delivered** |
| 15 | Core API `v2` production hardening, additive throughout: single-instance lock (`var/chassis.lock`), SIGHUP and `POST /api/v1/reload` configuration reload without exit, `kdx rpc` and `kdx --remote` command delegation over `POST /api/v1/rpc`, declarative `IpcRoute` registration, declarative module configuration schemas gated by `kdx config check`, secret redaction in `kdx config show` (`--reveal`), environment aliasing with provenance, configurable `state_dir`/`data_dir` auto-prepared at create, an opt-in managed asyncio loop, worker supervision that marks a module `failed`, and module-contributed `kdx doctor` diagnostics | **delivered** |

| 16 | Core API `v2` primitives, additive throughout: arbitrary-depth `CliCommand` nesting with `CliArgument.required` and honest `default is not None` handling, custom control-plane content types (`IpcResponse`, `guess_content_type`) and `register_static_dir(route_prefix, directory_path)` static mounts with `ETag`, `HEAD`, and traversal guards, a named-lock registry (`Chassis.acquire_lock`, `context.paths.lock`, `NamedLockRegistry` in `var/locks/`) released as one unit at shutdown, `core.paths.durable_dir` as the project's durable storage role, and a periodic task scheduler (`context.schedule`, `TaskScheduler`) that runs on the chassis clock and stops inside `stop_timeout` | **delivered** |

| 17 | `v0.2.1` maintenance patch on the `v2` line: mapping-valued chassis defaults are read as namespaces (`core.env.aliases.NAME` passes `kdx config check`), unknown-key suggestions are scoped to the module the typo named, the declarative hooks are collected again once `init(context)` ran so `kdx doctor` and `kdx modules show` never query a module too early, and a command handler's verdict exit code is documented and tested as part of the CLI contract | **delivered** |

| 18 | `v0.2.2` hardening and dogfooding on the `v2` line: `NamedLockRegistry.hold()` inspects the
registry and reserves the name in one guarded step, so two threads claiming one name never both
pass a check and wait on a collision that is already decided; `IpcStaticMount` normalisation
is hardened for Windows (the subpath is unquoted before it is validated, `/` and `\` are
folded, drive letters, alternate data streams, and reserved device names are refused, and
containment is verified on the resolved path with a case-folded second look); the Linux
memory probe is cached at import instead of importing `platform` per reading; `TaskScheduler`
bookkeeping is bounded (a truncated `last_error`, a purged task table, an issued task number
that is not recycled) - and the three built-in proof modules adopt the hooks themselves,
declaring `config_schema()`, `diagnostics()`, and `ipc_routes()` and adding
`/api/v1/backup/plan` and `/api/v1/system/snapshot` to the control plane | **delivered** |

| 19 | `v0.2.3` dogfooding and edge cases on the `v2` line: `heartbeat` schedules its ticker
through `context.schedule()` so the built-in owns no thread and supervision has nothing to
join, `backup_engine` guards every run with the named lock the chassis hands out (a busy copy
is a polite `BackupError`, a `lock` row in `kdx doctor`, and exit `1` from `kdx backup run`), Windows
`SIGBREAK` shares the `SIGINT` gate so an interrupt still exits `130`, a relative `--config` or
`--env-file` is anchored to `--home` before it is read, JSON log fields keep their scalar and
container types, and a named lock that cannot write its payload withdraws the claim |
**delivered** |

| 20 | `v0.2.4` closes the supervision dogfood and hardens two edges: `system_info` samples the
load average on a real background thread it reports through `workers()` (`modules.system_info
.watch_load`, defaulted where the platform has a load average), so `WorkerSupervisor` and `kdx
doctor` point at shipped code and shutdown joins what it started; a scheduled task may
`unschedule()` itself or its neighbour mid-sweep because `run_due()` works from a guarded
snapshot and calls callbacks unlocked; the control plane sets `close_connection` next to its
`Connection: close` header so a keep-alive client never stalls; `backup_engine` and `system_info`
call the base `start()`/`stop()` so `kdx doctor` reports a running module as `running`; and the
coverage floor moves to `87` | **delivered** |

| 21 | `v0.2.5` pairs the chassis primitives in shipped code and hardens three edges of the
control plane: `backup_engine` runs an optional periodic `auto_sweep` task whose copy takes the
same named lock a manual run takes (`modules.backup_engine.schedule_interval`, `auto_prune_days`
with stamped copies aged out of the destination), the discovery file is published by rename
from a `0600` sibling so a polling reader never finds it empty, a socket that closes during
shutdown is answered quietly instead of raising in the handler, and a scan of the locks
directory ignores dot files, swap files, and names `lock_name()` refuses | **delivered** |

| 22 | `v0.2.6` closes four edges a running daemon and a second copy of the CLI could find:
  a same-second `backup_engine` run no longer plans the identical destination folder -
  `build_plan()` appends an incrementing suffix whenever `dest / <stamp>` already exists;
  a lock a killed process left behind is adopted instead of forcing a manual delete, with
  the payload atomically rewritten once the dead PID is confirmed; the control plane defines
  `MAX_IPC_PAYLOAD_BYTES` (10 MiB) and answers an oversized body with HTTP 413 without
  buffering it; and a permission-refusal test skips on Windows, where directories ignore
  POSIX mode bits | **delivered** |

| 23 | `v0.2.7` decouples one lifecycle and hardens three edges a threaded daemon and a
  packaging run could find: `backup_engine` retention becomes its own maintenance sweep -
  `modules.backup_engine.prune_interval` declares an `auto_prune` task that ages stamped copies
  out on the chassis clock under the same named lock a copy takes, and reports last run and
  reclaimed space to `health()`, `kdx status`, and a `retention` diagnostic row; a threaded
  scheduler stop that overruns `core.lifecycle.stop_timeout` names the stuck task and its
  running duration and stops launching callbacks once `stop()` is set, so queued work is
  skipped rather than started late; `IpcRoute` refuses a path carrying whitespace, a CR, an LF,
  NUL, or DEL and a method outside `GET`, `POST`, `PUT`, `DELETE`, `PATCH`, `HEAD`, `OPTIONS`;
  and the wheel and sdist exclusions are declared in `pyproject.toml` and audited by the suite,
  so `var/`, `*.log`, `*.tmp`, byte code, and dev caches can never ride into a publish | **delivered** |

| 24 | `v0.2.8` types the platform edges and lets one platform carry a release: the
  instance lock resolves `msvcrt.locking` on Windows and `fcntl.flock` on POSIX once at
  import, `ipc.process_memory()` reads `resource.getrusage` only where that module exists,
  and `mypy --strict --platform win32` is clean with no `type: ignore` left on a POSIX
  attribute; the lock drops the platform module reference it kept but never called; and the
  CI matrix marks a macOS or Windows failure `continue-on-error`, so a tag publishes its
  wheel and sdist on the Linux bar while the portability hosts keep reporting | **delivered** |

| 25 | `v0.2.9` takes the timing brittleness out of the two lock tests a loaded shared
  runner can lose: a same-name claim that must fail at once is bounded at 500 ms rather
  than 50 ms, and a 200 ms named-lock timeout is bounded at a 120 ms floor and a 1.2 s
  ceiling rather than 150 ms and 1.0 s, with each bound naming the semantic it protects - so
  `make check` stops measuring how busy the runner is and keeps measuring the chassis
  | **delivered** |
## Next

1. **Publish to an index.** PyPI through trusted publishing. The git bootstrap is
   already live in the seed: `kdx init` pins the chassis to the repository, so a
   freshly seeded project installs today without waiting for an index.
2. **A Textual/TUI front end** over the same declarative `CliCommand` tree the wizard
   already reads - no module knows a second surface exists.
3. **Retention an operator can read**: `v0.2.7` gave retention a clock of its own - an
   `auto_prune` task that ages stamped copies out on its own interval, under the named lock a
   copy takes, reporting last run and reclaimed space to `health()`, `kdx status`, and a
   `retention` diagnostic row. The next honest step is putting that window in front of a person:
   a `kdx backup retention` report (window, sweep interval, archives standing, last sweep, space
   taken back) so an operator can ask what the policy did without reading JSON.
4. **Windows in CI** types clean and waits on its runtime path. The red was ours, not
   mypy's: the runner starts bash with `-e`, so the failing mypy call ended the step
   before any diagnostic ran, and the POSIX-only `os.sysconf` / `os.getrusage` reads
   failed the Windows platform check - `v0.2.8` resolved those reads at import, so a
   `--platform win32` check reports no errors. What remains is the Windows runtime path:
   test, boot, and seed steps now get to run there for the first time.

## Release cadence

A `v*` tag is the release trigger: the tag runs the full matrix, the package audit, and
then the `release` job, which builds `uv build` artifacts and attaches the wheel and
sdist to a GitHub Release. Moving a tag is allowed before `1.0`; after a tag is public,
the next release is a new version, not a moved tag. A patch on the `0.2` line ships under the
same frozen `v2` surface: fixes and documented invariants, no new export, no changed
signature, no renamed configuration key.

