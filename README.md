# korodexios-core

Industrial-grade, headless **microkernel chassis** that seeds every Korodexios project.
The core is a daemon-style engine that owns lifecycle, configuration, structured logging,
module discovery, and signal-safe shutdown. User interfaces (CLI, TUI, REST) are thin
clients that talk to the same core and never reimplement it.

- **Package:** `korodexios-core` (import name `korodexios_core`)
- **Console scripts:** `kdx` (primary), `korodexios` (canonical), `korodex` (legacy)
- **Python:** 3.11+ (developed on 3.13), `uv`-first, `venv` + `pip` compatible
- **Dependencies:** `pyyaml` (config files), `rich` (the wizard layer)
- **Release:** `v0.1.0`, core API frozen at `v1`

---

## Documentation

| Document | Audience | What it answers |
| --- | --- | --- |
| [docs/TUTORIAL.md](docs/TUTORIAL.md) | humans, new | five minutes from `kdx init` to a running custom module |
| [docs/CLI_REFERENCE.md](docs/CLI_REFERENCE.md) | humans, operators | every command, flag, exit code, install path, LAN and auth option |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | humans, engineers | microkernel design, config flow, event bus, IPC, signals, API freeze |
| [docs/ROADMAP.md](docs/ROADMAP.md) | everyone | the phase history and what ships next |
| [docs/AGENTS_GUIDE.md](docs/AGENTS_GUIDE.md) | AI agents, LLMs | contract-first reference: golden rules, module blueprint, manifest spec, verification loop |
| [llms.txt](llms.txt) | AI agents, LLMs | byte-identical mirror of the agent guide, sized for a context window |
| [AGENTS.md](AGENTS.md) | both | the working agreement: commands, layout, rules, the verification bar |
| [.agents/skills/](.agents/skills/) | AI agents | playbooks: project setup, CLI wizard UX, verification |

`docs/AGENTS_GUIDE.md` and `llms.txt` are kept byte-identical by a test, so an agent can
read either and hold the same contract. `kdx init` seeds both documents, tailored, into
every project it generates.

---

## Quickstart

```bash
uv venv --python 3.13            # or: python3 -m venv .venv
uv pip install -e ".[dev]"       # or:   pip install -e ".[dev]"
source .venv/bin/activate

kdx --version                    # byte-identical from korodexios and korodex too
kdx doctor                       # environment, config provenance, modules (--json for CI)
kdx status                       # query the running daemon over its control plane
kdx logs -n 50                   # tail the rotating log file (no POSIX tail needed)
kdx logs --rotate                # archive the log; the daemon reopens the path
kdx modules list                 # what auto-discovery found
kdx config show --key core.      # resolved values with the layer each came from
kdx run --once                   # boot, one tick, clean stop (CI friendly)
kdx run --host 0.0.0.0 --port 8123   # expose the control plane to the LAN
kdx heartbeat status             # a module-provided command
kdx system diagnostics           # interactive diagnostics menu (pick a section)
kdx backup run --dry-run         # simulate; --exclude patterns are honoured
kdx modules create my_plugin --path ./my_plugins
kdx init my_service --path ../my_service   # seed a whole new project from the chassis
kdx wizard                       # interactive numbered-menu wizard (TTY only)
```

CI and non-interactive runs pass `--yes`, plus `--no-log-file` on a read-only
filesystem. `kdx run --once` never prompts and never hangs: the chassis auto-detects
non-TTY stdin and forces `core.cli.interactive=false`. Development shortcuts:
`make install`, `make lint`, `make typecheck`, `make test`, `make check`, `make demo`.

## What the chassis guarantees

- **Precedence is sacred.** `CLI > env > kdx.local.yml > kdx.yml > defaults`. The env
  layer maps `KDX_CORE__IPC__PORT` to `core.ipc.port`; `KORODEXIOS_` and the legacy
  `KORODEX_` prefixes are understood too. `kdx config show` names the layer of every key.
- **Clean shutdown.** `SIGINT` exits `130`, `SIGTERM` exits `143`, an operator stop
  (including `POST /api/v1/shutdown`) exits `0`; `SIGBREAK` on Windows joins the same
  gate. Module `stop()` hooks always run, in reverse order, inside `stop_timeout`.
- **Zero stubs.** Every branch runs. `kdx init` writes a project whose own `make check`
  is green on arrival, with a smoke test that boots the chassis in-process.
- **Stdlib first.** PyYAML parses configuration, rich drives the terminal; everything
  else is the standard library. A new dependency needs a paragraph of justification.
- **Headless first.** The CLI is a thin client of the same in-process core a TUI or a
  REST client will use. Modules never write to stdout.
- **Crash-proof CLI.** Numbered menus, `[default]` in brackets, `while True` validation
  loops, and no traceback at the operator.
- **Diagnostics to stderr.** Status tags `[✓] [?] [!] [i]` are locale-neutral, stdout
  stays clean for scripting, and `--json` payloads are machine-stable.

## Layout

```text
src/korodexios_core/
  version.py / constants.py / exceptions.py   # versions, exit codes, error hierarchy
  paths.py / validate.py / units.py           # root discovery, polite validators, formatters
  config/    # LayeredConfig: five layers, dotted keys, typed getters, provenance
  logging/   # structured logging: stderr + rotating file, cooperative rotation
  events/    # EventBus: thread-safe pub/sub, wildcards, recursion guard
  kernel/    # the microkernel: contract, CLI contract, context, discovery, registry,
             # chassis, signals, ipc
  cli/       # thin clients: argparse surface, rich wizard, core commands,
             # project templating (init_project.py), scaffolding (scaffold.py)
  modules/   # built-in proof modules: heartbeat, system_info, backup_engine - a pure
             # discovery root: adding code here is the intended extension point
docs/        # the documentation suite, including the roadmap
.agents/skills/  # canonical playbooks, seeded into every project
tests/       # pytest; no network, no sleeps longer than one lifecycle tick
```

## Control plane

`kdx run` binds `127.0.0.1:8123` by default and writes `var/chassis.ipc` (mode `0600`)
with `pid`, `port`, `host`, and `token`. Three endpoints answer: `GET /health`,
`GET /api/v1/status`, and `POST /api/v1/shutdown`; reads require the token
(`X-Korodex-Token`) unless `core.ipc.auth: false`. Set `core.ipc.host: 0.0.0.0` to serve
the LAN, and `kdx status --target host:port` to query a remote chassis.

## Seeding a project

`kdx init my_service` writes a chassis project: `pyproject.toml` with a console script,
`kdx.yml`, `Makefile`, `README.md`, `AGENTS.md`, `docs/`, `.agents/skills/`,
`src/<pkg>/cli.py`, a first module with a manifest, and smoke tests that boot the
chassis in-process. Nothing is a placeholder: the generated tree passes `ruff`, strict
`mypy`, and `pytest` on arrival. The project owns its user-facing language through
`i18n.translate()` (stdlib gettext, source strings as the fallback), so the chassis
stays locale-neutral and English logs and IPC payloads stay machine-readable.

## Core API freeze

The package root `__all__` is the public surface, versioned as `CORE_API_VERSION`
(`v1`). Modules import only from the package root:

```python
from korodexios_core import CliArgument, CliCommand, Module, ScopedConfig
```

Additive changes are allowed inside `v1`; anything that breaks an import or a signature
moves the major version. The wheel ships a `py.typed` marker.

## Roadmap

The phase history and what ships next live in [docs/ROADMAP.md](docs/ROADMAP.md). The
next step is publishing to an index (PyPI via trusted publishing) or a documented git
bootstrap, then a Textual/TUI front end over the same declarative `CliCommand` tree.

## License

MIT — see [LICENSE](LICENSE).
