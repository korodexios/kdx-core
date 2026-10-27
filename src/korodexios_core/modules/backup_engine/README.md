# backup_engine

Validated source-to-destination backups with dry runs and polite size guards.
Built-in; discovered from `korodexios_core/modules/backup_engine/`.

## Contract

- Lifecycle: `init` validates source, destination, patterns, and `max_size`;
  `start` and `stop` only publish counters and log - copying happens on demand.
- Events published: `backup.configured` (at `kdx backup configure`) and
  `backup.completed` (after each run, with file and byte counts).
- CLI contributed: `kdx backup plan`, `kdx backup run`, `kdx backup retention`,
  `kdx backup configure`.
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
| `schedule_interval` | `0` | copy on the chassis clock (task `auto_sweep`); `0` means copies only on demand |
| `auto_prune_days` | `0` | how long a stamped copy survives; `0` keeps every copy |
| `prune_interval` | `0` | retention's own beat (task `auto_prune`); needs `auto_prune_days` above `0` |

## Retention report

`kdx backup retention [--json]` answers what the policy has done: the window and sweep
beat from the configuration, the stamped copies standing in the destination (count,
bytes, oldest, newest, how many already stand past the window), and the sweep record
(runs, last sweep, removed, reclaimed). Only folders whose own name is a backup stamp
count - the same rule a sweep deletes by - and the report never removes anything. The
counters name the answering process; a running daemon's record answers through
`kdx status`, `kdx doctor`, `kdx --remote backup retention`, or
`GET /api/v1/backup/retention`.

## How to test

`pytest -q tests/test_backup_engine.py` (boundaries, failure paths, destination
guards); the live check is `kdx backup run --dry-run`.
