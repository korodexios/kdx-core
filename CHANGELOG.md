# Changelog

Notable changes to korodexios-core. Versions follow the git tags; the phase
history lives in [docs/ROADMAP.md](docs/ROADMAP.md).

## [Unreleased]

### Changed

- Naming purge: the console installs two entry points (`kdx`, `korodexios`); the
  `korodex` alias, the `KORODEX_` environment prefix, the `korodex.yml` config
  candidates, and the `.korodex-root` marker are retired.
- Module manifests are `kdx_module.toml`; the rotating log file defaults to
  `var/log/kdx.log`; the control-plane token header is `X-Kdx-Token`; loggers
  live under `kdx.*`; the internal record attribute is `kdx_fields`.
- Public API renames, moving the core API from `v1` to `v2`: `KorodexError` to
  `KorodexiosError` and `KorodexLogger` to `KorodexiosLogger`.

### Added

- `GET /api/v1/schema` on the control plane: the machine-readable contract
  (routes, auth flags, response fields), versioned by `IPC_SCHEMA_VERSION`.
- Global CLI flags for the remaining core settings: `--tick-interval`,
  `--stop-timeout`, `--ipc-timeout`, `--no-ipc`, `--allow-anonymous-shutdown`,
  `--logs-poll-interval`, `--log-max-bytes`, `--log-backup-count`, `--cli-width`,
  `--confirmations`/`--no-confirmations`, `--no-autodiscover`,
  `--enable-module`/`--disable-module`, and `--name`.
- Per-module READMEs, this changelog, and `.gitignore` coverage for
  `kdx.local.*` and `korodexios.local.*`.

## [0.1.0] - 2026-10-20

Production release of the chassis: microkernel lifecycle, layered configuration
with provenance, structured logging, the event bus, module discovery and the
registry, signal-safe shutdown, the argparse CLI with the rich wizard, the
built-in proof modules (`heartbeat`, `system_info`, `backup_engine`), the
loopback-by-default IPC control plane, the `kdx init` project seed, and the
3-OS x 3-Python CI matrix.
