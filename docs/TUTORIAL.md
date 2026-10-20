# Tutorial: Five Minutes to Your First Module

Goal: from an empty directory to a running chassis with a custom module of your own,
verified by `make check`. You need Python 3.11+ and `uv` (or `pip`).

## 0. Install the chassis (30 seconds)

```bash
uv tool install korodexios-core        # or: pip install korodexios-core
kdx --version
```

If you are inside this repository instead:

```bash
make install && .venv/bin/kdx --version
```

## 1. Seed a project (60 seconds)

```bash
kdx init demo_app --path ../demo_app --yes
cd ../demo_app
```

You now have a project that already passes its own checks:

```text
demo_app/
  kdx.yml                     # chassis config (discovered by name)
  .env.example                # KDX_* overrides
  Makefile                    # install / check / lint / typecheck / test
  README.md, AGENTS.md
  docs/AGENTS_GUIDE.md        # the contract, for you and for AI agents
  docs/ARCHITECTURE.md        # how this project is wired
  .agents/skills/             # agent skills: setup, CLI UX, verification
  src/demo_app/
    cli.py                    # thin CLI entry point
    modules/app/              # your first module: __init__.py, module.py, manifest
  tests/test_smoke.py
```

Prove it to yourself:

```bash
make check                          # ruff, mypy --strict, pytest
kdx --home . modules list           # discovery found your module
kdx --home . run --once             # boot, one tick, clean stop, exit 0
```

## 2. Boot the daemon and look at it (60 seconds)

In one terminal:

```bash
kdx run --duration 120
```

In another:

```bash
kdx status                 # live report over the control plane
kdx logs -n 20             # the rotating log file, no tail required
kdx logs --rotate          # archive it; the daemon reopens the path
kdx doctor                 # config provenance and module states
```

Ctrl-C in the first terminal exits `130`; a `POST /api/v1/shutdown` from the control
plane exits `0`. Both paths run the same stop sequence.

## 3. Write your own module (90 seconds)

Scaffold it, then replace the body with the blueprint from
[`docs/AGENTS_GUIDE.md`](AGENTS_GUIDE.md):

```bash
kdx modules create metrics --path src/demo_app/modules --title "Samples a counter"
```

Three files, no stubs:

```text
src/demo_app/modules/metrics/
  korodex_module.toml   # [module] name, description, version, entrypoint = "MODULE"
  module.py             # class MetricsModule(Module): ... ; MODULE = MetricsModule()
  __init__.py           # re-export MODULE and the class
```

The minimum contract is seven members - `name`, `description`, `version`,
`init(context)`, `register_cli(surface)`, `start()`, `stop()`. Everything else is
yours. The shortest honest module:

```python
from korodexios_core import CliCommand, CoreContext, Module


class GreeterModule(Module):
    name = "greeter"
    description = "Greets on demand and proves the contract end to end."
    version = "0.1.0"

    def init(self, context: CoreContext) -> None:
        super().init(context)
        self.logger.debug("greeter configured")

    def register_cli(self, surface) -> None:
        surface.add_command(
            CliCommand(
                name="greeter",
                help="Greeter module utilities.",
                module_name=self.name,
                handler=lambda request: self._greet(request),
            )
        )

    def _greet(self, request) -> int:
        who = request.option("who", "korodexios")
        self.logger.info("greeting", who=who)
        return 0
```

Read configuration through the scoped view (`self.config.get_float("interval", 5.0)`
means `modules.metrics.interval`), log through `self.logger`, and talk to other
modules only through `self.bus`.

## 4. Prove it (60 seconds)

```bash
kdx --home . modules list            # metrics appears with origin 'project'
kdx --home . modules show metrics    # spec + live health
kdx --home . run --duration 3        # the sampler thread runs and stops cleanly
kdx --home . metrics status          # your CLI command, wired by register_cli
make check                           # still green
```

Add a test while you are here: build the smallest chassis root in `tmp_path`, boot a
`Chassis`, and assert on `health()` - not on log text.

## 5. Where to go next

- Contract details, manifest reference, verification loop:
  [`docs/AGENTS_GUIDE.md`](AGENTS_GUIDE.md) (mirrored as `llms.txt`).
- Design narrative: [`docs/ARCHITECTURE.md`](ARCHITECTURE.md).
- Every flag: [`docs/CLI_REFERENCE.md`](CLI_REFERENCE.md).
