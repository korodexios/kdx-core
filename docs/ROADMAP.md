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
| 4 | Review hardening (event-bus recursion guard, slow-handler timing, plugin import isolation, backup destination and symlink guards, ASCII tag fallback), module dependency DAG (`depends_on`), `korodex modules create` scaffolding | **delivered** |
| 5 | Daemon IPC: loopback HTTP control plane (`/health`, `/api/v1/status`, `POST /api/v1/shutdown`), discovery file with a shared token, and the `korodex status` command | **delivered** |
| 6 | `korodex init <name>`: zero-stub project seed - pyproject, kdx.yml, Makefile, README, AGENTS.md, `.agents/skills/`, a first module, smoke tests, optional `web/` client; `--minimal`, `--force`, `--ecosystem` | **delivered** |
| 7 | Distribution readiness: dynamic version from `version.py`, audited wheel and sdist (`py.typed` + every module manifest), isolated `uv tool` / `pipx` install, the `korodex logs` operator helper, and a 3-OS x 3-Python CI matrix | **delivered** |
| 8 | `kdx` as the primary short alias (`korodexios` canonical, `korodex` kept for backward compatibility), LAN-bindable control plane (`core.ipc.host`), optional auth (`core.ipc.auth`, `--no-auth`), static token (`core.ipc.token`), the anonymous-shutdown guard, and `kdx status --target` | **delivered** |
| 9 | Naming alignment: `kdx.yml` / `korodexios.yml` config candidates with `korodex.yml` kept as a legacy fallback, a `kdx.local.yml` override layer, `KDX_` as the primary env prefix (`KORODEXIOS_`/`KORODEX_` understood), and `kdx`-first docs | **delivered** |
| 10 | Production release: `v0.1.0` tagged with the version synchronized across `version.py`, docs, and artifacts | **delivered** |
| 11 | Release pipeline: a `v*` tag runs the full 3-OS x 3-Python matrix and the package audit, then publishes the audited wheel and sdist to a GitHub Release; a branch push runs a trimmed matrix for fast feedback | **delivered** |
| 12 | Operator ergonomics: `kdx logs --rotate` (archive the file, the running chassis reopens the path), `kdx doctor --json` for machines, and the i18n convention for seeded projects (stdlib gettext, source strings as the fallback) | **delivered** |
| 13 | Configuration honesty: unknown-key detection at boot, `kdx config check` as a CI gate, a versioned `doctor --json` payload (`schema: 1`), a seed that installs the chassis from the repository today (`--chassis-source`), a CI workflow contract test, and a coverage ratchet in `make check` | **delivered** |

## Next

1. **Publish to an index.** PyPI through trusted publishing. The git bootstrap is
   already live in the seed: `kdx init` pins the chassis to the repository, so a
   freshly seeded project installs today without waiting for an index.
2. **A Textual/TUI front end** over the same declarative `CliCommand` tree the wizard
   already reads - no module knows a second surface exists.
3. **Windows type check in CI** is red: `mypy` on `windows-latest` exits in about three
   seconds, before any chassis test runs. The check matrix keeps `fail-fast: false`, so
   Linux and macOS stay green and the release job waits for the whole set.

## Release cadence

A `v*` tag is the release trigger: the tag runs the full matrix, the package audit, and
then the `release` job, which builds `uv build` artifacts and attaches the wheel and
sdist to a GitHub Release. Moving a tag is allowed before `1.0`; after a tag is public,
the next release is a new version, not a moved tag.

