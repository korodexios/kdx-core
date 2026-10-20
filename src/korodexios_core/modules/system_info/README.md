# system_info

Host diagnostics proving scoped configuration, events, and declarative CLI
registration. Built-in; discovered from `korodexios_core/modules/system_info/`.

## Contract

- Lifecycle: `init` reads the scoped config, `start` collects the first snapshot
  and publishes it, `stop` only releases counters (the module owns no threads).
- Events published: `system.info.collected` (payload: the snapshot sections).
- CLI contributed: `kdx system info`, `kdx system diagnostics`, `kdx system disk`.
- Manifest: `kdx_module.toml` (`priority = 40`, `entrypoint = "MODULE"`).

## Configuration keys (`modules.system_info.*`)

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | gate registration at discovery |
| `disk_path` | `.` | filesystem reported by `kdx system disk` |
| `include_cpu_model` | `true` | include the CPU model string in snapshots |

## How to test

`pytest -q tests/test_system_info.py` (collector boundaries and CLI handlers); the
live check is `kdx system info`.
