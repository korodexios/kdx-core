# backup_engine

Validated source-to-destination backups with dry runs and polite size guards.
Built-in; discovered from `korodexios_core/modules/backup_engine/`.

## Contract

- Lifecycle: `init` validates source, destination, patterns, and `max_size`;
  `start` and `stop` only publish counters and log - copying happens on demand.
- Events published: `backup.configured` (at `kdx backup configure`) and
  `backup.completed` (after each run, with file and byte counts).
- CLI contributed: `kdx backup plan`, `kdx backup run`, `kdx backup configure`.
- Manifest: `kdx_module.toml` (`priority = 60`, `entrypoint = "MODULE"`).

## Configuration keys (`modules.backup_engine.*`)

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | gate registration at discovery |
| `source` | `.` | what to copy, resolved against the chassis root |
| `dest` | `var/lib/backups` | each run lands in `dest/<timestamp>/` |
| `include` | `["*"]` | glob allow-list |
| `exclude` | `.git, .venv, __pycache__, var, node_modules, *.pyc, *.log` | glob deny-list |
| `max_size` | `500MB` | `[!]` failure when the copy would exceed this |
| `dry_run` | `false` | plan without writing |
| `follow_symlinks` | `false` | copy symlink targets too |

## How to test

`pytest -q tests/test_backup_engine.py` (boundaries, failure paths, destination
guards); the live check is `kdx backup run --dry-run`.
