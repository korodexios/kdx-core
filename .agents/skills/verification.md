# Skill: verification

Prove a change instead of assuming it.

## Loop
1. `make lint` - ruff over `src` and `tests`.
2. `make typecheck` - mypy strict over `korodexios_core` (42 source files).
3. `make test` - pytest; no network, no sleeps longer than one tick.
4. Boot it: `korodex run --once`, `korodex status`, `korodex logs -n 20`, then Ctrl-C
   and check the exit code (130 SIGINT, 143 SIGTERM, 0 for an operator stop).
5. Seed it: `korodex init probe --path /tmp/probe && cd /tmp/probe && make check`.
6. Ship it: `uv build` and audit the wheel and sdist contents.

## Evidence
Quote the failing command and its output before and after the fix. A green suite that
never touches the changed path is not proof for that path.
