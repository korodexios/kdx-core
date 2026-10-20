# heartbeat

Proof-of-concept module: one daemon-thread ticker proving background work, events,
and clean shutdown. Built-in; discovered from `korodexios_core/modules/heartbeat/`.

## Contract

- Lifecycle: `init` reads the scoped config, `start` spawns the `kdx-heartbeat`
  daemon thread, `stop` joins it inside `core.lifecycle.stop_timeout` and reports
  how many beats were published.
- Events published: `core.heartbeat` (payload: tick counters and interval).
- CLI contributed: `kdx heartbeat status`.
- Manifest: `kdx_module.toml` (`priority = 50`, `entrypoint = "MODULE"`).

## Configuration keys (`modules.heartbeat.*`)

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | gate registration at discovery |
| `interval` | `2.0` | seconds between beats, floored at `0.05` |

## How to test

`pytest -q tests/test_discovery.py tests/test_chassis.py` covers discovery,
lifecycle, and the thread stop; the live check is `kdx heartbeat status`.
