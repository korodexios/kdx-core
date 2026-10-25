# AGENTS.md - korodexios-core

Working agreement for anyone editing this chassis repository. The chassis is a
foundation, not an application: every line here protects that role.

## Commands

```bash
make install      # uv (or pip) editable install with the dev extra
make check        # ruff + mypy --strict + pytest, in that order
make lint         # ruff check src tests
make typecheck    # mypy, strict, over korodexios_core
make test         # pytest -q
make coverage     # pytest -q with the coverage floor (86%) enforced
make demo         # scripted end-to-end tour of the CLI
kdx doctor        # environment, config provenance, modules, control plane
kdx status        # live daemon report over the IPC endpoint
kdx logs -n 50    # the rotating log file, no POSIX tail required
```

## Layout

- `src/korodexios_core/kernel/` - the microkernel: contract, discovery, registry,
  chassis, signals, IPC. Infrastructure only; no product knowledge.
- `src/korodexios_core/modules/` - built-in proof modules. This directory is a pure
  discovery root: adding code here is the intended extension point.
- `src/korodexios_core/cli/` - thin clients: argparse surface, rich wizard, core
  commands, project templating (`init_project.py`), scaffolding (`scaffold.py`).
- `tests/` - pytest, no network, no sleeps longer than one lifecycle tick.
- `var/` - runtime state (logs, `chassis.ipc`, `chassis.lock`, `locks/`); never committed.
- `data/` - durable project state (databases, ledgers, indexes); survives a moved `var/`.

## Rules

1. **Headless first.** The chassis owns lifecycle, discovery, configuration, and
   shutdown. The CLI and any future TUI are thin clients of that core.
2. **Zero stubs.** No TODO bodies, no `pass` placeholders, no mock returns. If a
   branch exists, it runs.
3. **Precedence is sacred.** CLI > env (`KDX_*`, then `KORODEXIOS_*`) >
   `kdx.local.yml` > `kdx.yml` > defaults. Read configuration through
   `LayeredConfig`, never `os.environ`.
4. **Clean shutdown.** Whatever `start()` acquires, `stop()` releases inside
   `core.lifecycle.stop_timeout`. SIGINT exits 130, SIGTERM 143, an operator stop
   (including `POST /api/v1/shutdown`) exits 0. No `KeyboardInterrupt` traceback.
5. **Diagnostics to stderr, UI to stdout.** Modules never write to stdout.
6. **Crash-proof CLI.** Numbered menus, `[default]` in brackets, `while True`
   validation loops, `[✓] [?] [!] [i]` tags, `--yes` plus non-TTY detection.
7. **Stdlib first.** A new dependency needs a paragraph of justification, not a line.

## Verification

Coverage is a ratchet, not a report: `make check` runs `pytest --cov` with
`--cov-fail-under=86`, one point under the measured total, so a new untested
branch fails the bar. `pytest-cov` is a dev-extra tool only - the runtime stays
PyYAML and rich.

Quote the failing command before and after the fix. `make check` is the bar;
`kdx init` output must stay lint-clean, type-clean, and runnable. The console installs
one entry point under two names: `kdx` (primary short alias) and `korodexios` (canonical).
Docs and help lead with `kdx`.
