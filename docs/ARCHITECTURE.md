# Architecture

How korodexios-core is put together: a microkernel chassis that owns process
lifecycle, and a discovery-driven module system that owns everything else.

## 1. The idea

The chassis is a foundation, not an application. It answers three questions for every
project built on it:

1. **How does my process start, keep running, and stop cleanly?** - the kernel.
2. **Where does my code plug in?** - module discovery under a pure discovery root.
3. **Where does my configuration come from?** - a layered config with per-key
   provenance.

Everything else is a module. The kernel never learns a product name: it validates a
contract, orders startup, and cleans up.

```text
                 +----------------------------- kernel -----------------------------+
                 |  paths      config      logging      events      registry        |
                 |  chassis    signals     ipc          cli_contract                |
                 +-----------------------------------------------------------------+
                        ^                              ^
                        | contract                     | publish / subscribe
                 +------+--------- modules ------------+---------------------------+
                 |  system_info   heartbeat   backup_engine   <your modules>       |
                 +-----------------------------------------------------------------+
                 |  cli (thin client: argparse surface, wizard, commands, seeds)    |
                 +-----------------------------------------------------------------+
```

## 2. Boot sequence

`Chassis.create()` performs, in order:

1. **Discover paths** - `AppPaths.discover()` walks up to a root marker, then resolves
   `var/`, `logs/`, `data/`, and the config candidates.
2. **Load configuration** - `LayeredConfig.load()` merges defaults, config file, local
   override, `.env`, process environment, and CLI overrides.
3. **Build logging** - console handler on stderr plus an optional rotating file, levels
   resolved from config.
4. **Create the event bus** - one bus per chassis, shared by every module.
5. **Discover modules** - scan the builtin root, configured `extra_paths`, and any
   `--modules-dir`; read manifests; import packages; resolve `entrypoint`; validate the
   contract; gate on `requires_core` and `enabled`.
6. **Register** - sort by dependency topology with `priority` as tie-breaker.
7. **`init(context)`** - every module receives the `CoreContext`.
8. **`register_cli(surface)`** - modules contribute their declarative command tree.
9. **`start()`** - in order; a failure marks that module `failed` and the boot continues.
10. **Lifecycle loop** - ticks every `core.lifecycle.tick_interval` until stopped.

Shutdown reverses step 9, then closes logging. `stop()` runs even after a failed
`start()`, bounded by `core.lifecycle.stop_timeout`.

## 3. Layered configuration flow

```text
  defaults            CORE_DEFAULTS, flat dotted keys
      v
  config file         kdx.yml | korodexios.yml        (first candidate wins)
      v
  local override      kdx.local.yml                            (gitignored, machine-local)
      v
  environment         .env, then process env  (KDX_ > KORODEXIOS_)
      v
  CLI overrides       --log-level, --ipc-host, --ipc-port, --auth ...
      v
  LayeredConfig       get / get_int / get_bool / get_list / source_of / origin_of
```

Each key remembers where its winning value came from, so `kdx config show` can print
provenance and `kdx doctor` can explain a surprise. `ScopedConfig` gives a module the
`modules.<name>` subtree with automatic fallback to global keys, so module config keys
never repeat the module name.

## 4. Event bus

One in-process bus per chassis, thread-safe, with dot-segmented patterns:

- `*` matches exactly one segment, `**` matches any number of segments.
- `publish(name, payload, source=)` returns the number of handlers that ran.
- Handlers run outside the lock, so they may subscribe, unsubscribe, or publish again;
  recursion is bounded by `max_depth` (16 by default).
- Every handler is timed. A handler slower than the slow-handler budget is recorded in
  `stats()`, which is how a blocking subscriber becomes visible.
- A bounded ring buffer backs `history(limit)`; `wait_for(name, timeout)` lets a test or
  a module synchronise on an event without sleeping.
- `once=True` subscriptions self-remove; `Subscription.unsubscribe(bus)` is the manual
  cancel handle.

Events are the only sanctioned channel between modules. Nothing imports nothing: a
module that reaches for another module's object has created a coupling the chassis
cannot order.

## 5. IPC control plane

`IpcServer` runs a stdlib HTTP server alongside the lifecycle loop:

| Route | Purpose |
| --- | --- |
| `GET /health` | liveness; never needs a token |
| `GET /api/v1/schema` | machine-readable contract: routes, auth flags, response fields |
| `GET /api/v1/status` | full chassis report: state, uptime, ticks, modules, health |
| `POST /api/v1/shutdown` | operator stop; the process exits 0 |

- **Bind** - `core.ipc.host` defaults to `127.0.0.1`; `0.0.0.0` (or `::`) makes the
  daemon reachable from the LAN. A wildcard bind is announced as `127.0.0.1` in the
  discovery file so local clients keep working unchanged.
- **Discovery** - `var/chassis.ipc` (mode `0600`) records `pid`, `host`, `port`,
  `token`, `started_at`. Clients read it, then talk HTTP.
- **Auth** - `core.ipc.auth` (default `true`) requires the shared token on
  `/api/v1/status`; `/health` and `/api/v1/schema` stay open (liveness and static
  metadata).
  `core.ipc.allow_anonymous_shutdown` (default `false`) decides whether an
  unauthenticated client may stop the daemon.
- **Token** - `core.ipc.token` is the shared secret. Empty means the chassis mints an
  ephemeral token at boot; a static value survives restarts and is what a remote
  `kdx status --target` should present. The header is `X-Kdx-Token`.
- **Client** - `IpcDiscovery.load(path)` for the local file,
  `IpcDiscovery.remote("http://host:port")` for anything else.

## 6. Signal handling and exit codes

The chassis installs handlers so an interactive Ctrl-C never leaves a traceback:

| Input | Meaning | Exit code |
| --- | --- | --- |
| `SIGINT` | operator interrupt | `130` |
| `SIGTERM` | supervisor stop (systemd, containers) | `143` |
| `SIGBREAK` | Windows console close; joins the same gate as `SIGINT` | `130` |
| `POST /api/v1/shutdown` | planned stop | `0` |
| `Chassis.request_clean_stop()` | in-process stop | `0` |

Both signal paths route into the same stop sequence: modules stop in reverse order,
logging is flushed and closed, and the process exits with the conventional
`128 + signum`. Other codes: `1` handled failure, `2` usage error, `3` configuration
error.

## 7. Threading model

- The **lifecycle loop** owns the ticks and the stop decision.
- The **IPC worker** answers HTTP and posts the stop request.
- **Module threads** are the module's own business; the contract only demands that
  `stop()` joins them inside `stop_timeout`.
- The bus is lock-protected; handlers run outside the lock, so a handler may do
  anything the bus API allows without deadlocking.
- Logging is thread-safe by construction (one `LoggerFactory`, stdlib handlers).

## 8. Discovery and module lifecycle

```text
new --> initialized --> running --> stopped
  \          |              |
   +---------+--------------+--> failed      (a phase raised)
                 |
              disabled   (manifest or configuration said so)
```

Discovery failures are data, not exceptions: a broken plugin is recorded in the
`DiscoveryReport` (candidates, skipped, failures) and reported by `kdx doctor` and
`kdx modules list`. The chassis boots with zero modules rather than dying with one.

The contract is a `runtime_checkable` `Protocol` (`ModuleProtocol`) plus an optional
`Module` base class that wires `config`, `logger`, `bus`, and `context` from the
handed `CoreContext`. Registration checks the protocol and records a failure instead
of raising, so one bad module never costs the chassis the rest.

## 9. Public API and versioning

The package root `__all__` is the frozen public surface (core API `v2`): the module
contract, the declarative CLI contract, layered configuration, the event bus, chassis
lifecycle, paths, logging, exceptions, and the control plane. Additive changes are
allowed inside `v1`; anything that breaks an import or a signature moves the major
version. `korodexios_core.__version__` is the single version source; `pyproject.toml`
reads it dynamically.

The freeze policy is mechanical:

| Change | Effect |
| --- | --- |
| New export, new optional keyword, new command | additive: `v2` keeps going |
| Renamed or removed export, changed signature | breaking: the major version moves |
| `korodexios_core.CORE_API_VERSION` | the string a module can assert against |

The wheel ships a `py.typed` marker, so type information travels with the package.

## 10. Boundaries

- **Single process.** No cluster, no scheduler, no persistence layer. Modules own their
  storage; the chassis owns the directories.
- **No plugin isolation.** A module runs in-process; the contract and the timeout are
  the isolation.
- **Stdlib first.** PyYAML and rich exist for config parsing and the terminal surface;
  everything else is the standard library.
- **Headless first.** The CLI is a thin client; a TUI is planned on the same
  declarative command tree.
