# Pi integration owners

Pi is the sole Session transcript, model/Tool loop, context, compaction, Steer,
Stop and recovery owner. PAW adapts that runtime through these concrete modules.
Import a module directly; the package initializer has no construction or exports.

| Module | Responsibility |
| --- | --- |
| `config`, `provider_config`, `provider_auth` | Host launch configuration, Provider configuration and authentication bridge |
| `factory` | Construct the single Host adapter with the caller's stores and callbacks |
| `host_client` | One process connection, correlated JSONL replies, ordered events and stream cleanup |
| `runtime` | Session bindings, admission, settlement, recovery and Runtime-owned in-memory state |
| `transcript` | Pure Classic full/recent history projection, without subprocess or database ownership |
| `transcript_io` | Bounded JSONL tail and append-boundary reads; never opens a Host or changes Session state |
| `event_projection` | Pure Tool/text event payloads and settlement/capability wire projections |
| `ui_requests` | Bounded UI request fields and response validation; pending requests, timers and replies remain in `runtime` |
| `public`, `values` | Declared cross-module message projections, value helpers and error types |
| `protocols` | Validate the supported wire protocol, without executor selection |

Only protocol 2 executes. The old flat `pi_runtime.py` v1 adapter and execution
selector are removed. This does not delete historical JSONL, RuntimeBindings,
manifest metadata or SQLite migrations. Old executable packages must be rebuilt
using the existing managed Host build flow; their metadata can still be inspected.

`managed_pi_runtime` remains the package installation/activation owner, while
`room_runtime_host_kill_gate` remains the shared durable process identity and kill
authority. Neither is replaced by a second lifecycle inside this package.

Terminal paths share only their common message/Tool/retry projection cleanup.
Their admission flags, identity fences, timers, outcome classification and event
order stay explicit in `runtime`; a projection helper cannot complete a turn.

The import gate enforces declared `__all__` surfaces across the Runtime family.
The owner gate prevents transport/history/config from importing application
composition. Tests under `tests/test_pi_runtime*.py` cover these separately.

An immutable Session engine binding selects Classic or explicitly opted-in
Durable inside the same manager and Host pool. Durable uses native Host
snapshots and settlement instead of Classic JSONL tail readers, recent caches
or recovered-turn retirement. Its history and control reads are passive; only
the exact explicit resume action may restart native scheduling. The existing
Gateway owner still fences Tool execution. See the [Durable product
guide](../../integrations/pi/pi-durable.md) and
`tests/test_pi_durable_runtime.py` for the engine boundary and recovery cases.
