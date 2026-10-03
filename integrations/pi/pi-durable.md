# Experimental Durable Sessions

The paired Pi 1.0 Host offers a native Durable engine for **new standalone
Sessions**. This is an explicit creation choice, not a global engine change.
Classic remains the default. Existing Sessions, Room partners, Lab runs and
their transcripts retain their current owners.

## Create and continue

1. Build and activate the paired native Mac Host using the explicit Pi checkout
   in the [managed-runtime guide](pi-0.99-codemode.md). Pi version `1.0.0` and
   protocol `2` alone do not establish Durable support: the negotiated
   `sessionEngines.durable` capability must also be available.
2. On the Agent home surface, select the experimental Durable execution mode
   before creating a Session. Its engine cannot be changed after creation.
3. Send text normally. The same configured Provider/model, streaming timeline,
   product Gateway and Tool receipts remain in use.
4. After a process restart, opening the Session restores history and observes
   its original native task. Unfinished work remains paused. Select
   **继续当前任务** to resume that admitted input. Reloading, directory listing
   and history reads never trigger a model or Tool invocation.

An empty Session or one whose original task has completed reopens idle, with
`paused: false`, `recoverable: false` and no active recovery target. Send a new
message normally; no continuation request or replay of completed work is needed.
This product state does not start native scheduling during passive history reads.

The continuation request contains the original `sessionId`, `turnId` and
`clientMessageId`; it contains no new prompt. Repeated continuation joins the
same native work or returns its saved settlement. Failed reads and missing or
foreign metadata grant no continuation authority. The shared live store accepts
current execution metadata separately from older history pages, retaining the
existing terminal fences and cross-Session isolation.

Stop also targets the original input. The UI can show cancellation before all
physical work has drained; only the native settlement and owned Gateway/process
receipts prove completion. Input during this drain is rejected as
`SESSION_ABORTING` and can be retried after settlement. A delayed Stop for an
older input cannot cancel the next task.

Standalone background jobs follow that same original-turn fence. Stop captures
their persisted binding before the native RPC and waits for their owned process
groups outside the Runtime lock. Completed HTTP delivery is not process
completion. Unknown or ambiguous identity remains pending, and Room-owned jobs
remain with the Room cancellation owner.

## Standalone compaction recovery

Interrupted standalone compaction has no user input identity. Its current
metadata instead exposes `compactionTarget` with exactly `kind: "compaction"`,
`runtimeSessionId` (the native `piSessionId`) and a nonempty unique `taskIds`
list. IDs are positive safe integers encoded as `durable:task:N`, ordered
lexicographically (`durable:task:10` precedes `durable:task:2`). The complete
native target must be preserved; mixed turn/cancellation identities, foreign
sessions and partial task sets are rejected rather than guessed.

The paused workspace offers **继续压缩** and **停止压缩**. Both use that exact
target, preserve the draft, and never submit a prompt or a replacement compact
request. This requires negotiated `sessionCompactionRecovery: true` plus
`engineCapabilities.compactionRecovery: true`; older Hosts retain ordinary
input recovery but cannot execute this new control path.

Compaction Stop has no cancellation authority over user turns, approvals,
workspace commands or background jobs. A successful
`rag-ime.pi-compaction-abort.v1` receipt reports `drained: true` only after all
named native tasks are terminal, retaining their original `completed`,
`aborted` or `failed` outcomes on repeats. An older target cannot stop its
successor. `rag-ime.pi-compaction-resume.v1` similarly reports whether original
work resumed, without substituting current work.

A running target stays visible until authoritative native metadata clears it.
The existing compaction activity completion is not a drain receipt. Pi emits
`compaction_settled` only after exact native termination; PAW rereads current
control state and projects that metadata independently of old input events or
historical transcript pages. Delayed responses and Session switches cannot
rebind the controls.

## Supported boundary

| Capability | Durable engine |
| --- | --- |
| Text and configured models/thinking level | Supported |
| Existing product Gateway tools and saved receipts | Supported |
| Streaming, full/recent history, native compaction | Supported |
| Exact cancellation and explicit native recovery | Supported |
| Native MCP and Code Mode | Unavailable |
| Managed plugins and Pi/Codex Skills | Unavailable |
| Images, conversation fork/rewrite and command catalog | Unavailable |

An explicit `maxTokens` override on model selection is also unavailable because
native Conversation streaming does not carry it. Normal model/thinking selection
uses the configured native model policy.

Authorized `workspace_*` tools are discoverable directly in Durable, preserving
their required `op` argument and existing Gateway permissions. Classic continues
to use Pi's resident workspace aliases. Neither mode grants tools that the
Session's disclosure or policy excludes.

Unsupported actions are rejected by both the product and Host boundaries.
Creation/send retains unsupported attached images and the draft for correction.
A withdrawn Host capability blocks Durable creation; it does not silently fall
back to Classic. The Classic feature set remains available in Classic Sessions.

## Ownership and persistence

PAW migration `0218` adds `agent_sessions.runtime_engine`, defaulting existing
rows to `classic`. Session creation atomically stores the Durable identity and
prepared `managed-pi` / `pi_durable` binding. PAW's Session directory and existing
Gateway idempotency store retain their responsibilities.

The managed Host uses a separate per-Session store under its owned session root:
`durable/<opaque-session-id>/session.sqlite`. Its native Harness owns
Conversation entries, task scheduling, model/Tool loops and checkpoints. An
exclusive directory lease prevents concurrent Host ownership. The product
extension document binds original client identity and argument fingerprint to
native submission/generations and saved terminal receipts; it is an adapter,
not another execution loop.

Model compaction changes the native context head. Public full history instead
paginates native entries, preserving original message IDs, Tool evidence and
receipts. A bounded recent suffix cannot rewrite PAW's total message count.
Classic JSONL readers, recent caches and recovered-turn retirement never read
or retire Durable work.

## Uncertain effects and rollback

A saved execution-intent checkpoint does not prove that an external effect
failed. Native recovery refuses to replay unsafe tools after that boundary.
Safe replay requires both stored and current policy. Product Gateway lookup
also retains the original request and receipt; changed intent conflicts and an
unknown outcome cannot be executed again. There is no arbitrary external
exactly-once promise, and no automatic resend with a new identity.

The migration is append-only. Reverting source does not convert a Durable
Session to Classic or remove its native journal. Keep new stores, bindings and
receipts; an older Host must not execute Durable bindings. Do not restore an
older database over new work or rename a native store into Classic JSONL.
Reconcile active/unknown work before any runtime downgrade. Account settings,
credentials and existing user history are not migrated.

## Verification

- PAW behavior: `python3 -m unittest tests.test_pi_durable_runtime` and the
  existing Runtime/Gateway regressions.
- Workspace discovery: `python3 -m unittest tests.test_pi_durable_workspace_manifests`.
  The native loader contract additionally requires the explicit
  `PAW_PI_WORKSPACE_TOOL_TEST_HOST_DIST` path to the paired built Host dist;
  without that path only this native case is skipped.
- Physical background cancellation:
  `python3 -m unittest tests.test_workspace_job_turn_cancellation` covers real
  process groups, delayed effects, late admission and launch-release races.
- Pi native behavior: the Host's `test/durable-product-session.test.ts` covers
  same-input recovery, unsafe-effect recovery, generation ownership, physical
  drain, lost responses and history after compaction. Native `abortRun` has
  separate Harness tests.
- Frontend behavior: live-store/reducer, mounted SessionWorkspace and timeline
  checks preserve current metadata authority, exact continuation, duplicate
  clicks, rejected drafts and static paused presentation.
- Run the documented Session/packages/resilience staged payload tests after
  the paired native build. Configured Provider and installed browser/native
  acceptance remain additional checks; controlled native tests do not prove
  those boundaries.
