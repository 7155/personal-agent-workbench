# Jev in PAW

Jev supplies structured decisions, not generated summaries. PAW uses the native
[TypeSafe API](https://docs.typesafe.ai/introduction/quickstart), with the
`jev-latest` model and typed Choice/Score questions.

## Configuration

In a build containing this integration, open System Settings → Model accounts →
TypeSafe / Jev. Save the API key in macOS Keychain. The existing command-line
alternative is `bash scripts/configure_jev_key.sh`. Neither the catalog nor
receipts contain the key. `TYPESAFE_API_KEY` takes precedence; remove it and
restart the service before managing that credential through the UI.

The scenario list separates configured credentials, available adapters and
unimplemented candidates. It does not claim that a saved credential was tested
or that every scenario has been activated.

## Implemented paths

- **Room task execution:** a user request can enter routing, planning, execution,
  independent verification and final synthesis. Jev selects from host-validated
  actions; ordinary Pi Sessions do the work. Task dependencies, versioned
  submissions, repair, exact cancellation, durable events and restart recovery
  use the existing Room/WorkStore owners. An admission receipt means admitted,
  not running or completed; release waits for the actual turn and its causal
  resources to settle.
- **Room model routing:** the balanced profile uses GPT-6.1 Sol medium for routine
  execution, xhigh for planning, verification and complex integration, and
  GPT-6 Luna max only for explicitly simple execution or synthesis. Astra is
  selected only by an explicit model lock. The participant profile keeps the chosen Session
  models. Each dispatch checks the configured Provider's actual model catalog;
  an unavailable model produces a recoverable error instead of a silent
  downgrade. Role-card community notes are anecdotal guidance, not PAW scores.
- **Room tools:** the default `dispatch` policy uses the active dispatch and
  existing workspace authority without adding per-tool approvals. The optional
  `jev_dangerous` policy automatically evaluates dangerous prepared operations
  through the existing approval owner. A failed Jev evaluation leaves the
  operation unexecuted and retryable; it does not fall back to another model.
- **Room verification:** new roots default to `verificationMode=auto`. Jev
  assesses the submitted result and current evidence after the executor drains.
  It can deliver a sufficient result directly, or request an additional Pi
  inspection when evidence is missing or the user requires it. Both choices are
  recorded against the exact task and artifact revisions. `independent` always
  requires a different partner. Stored roots without this policy retain
  independent verification. Planning favors independent deliverables assigned
  to different partners; dependencies represent actual prerequisites, not a
  mandatory sequence of stages. Completed multi-task and file-delivery work
  publishes a results report with deliverables, recorded verification and open
  items in the public Room conversation. This uses the existing finalization
  receipt and does not create another model turn; short text-only replies remain
  direct answers.
- **Tool approval:** configured Jev is preferred on the next new approval.
  Confidence below 0.70 yields a denial without asking another model to override it.
  Transport/provider/response failures use the current Codex approval-model fallback.
  A completed approval reuses its stored receipt.
- **Knowledge reranking:** select `RAG_IME_KNOWLEDGE_RERANK_PROVIDER=typesafe-jev`
  in the Knowledge worker environment, restart that worker, and enable reranking
  in the base retrieval settings or retrieval test. This is an explicit remote
  path: queries and selected candidate passages are sent to TypeSafe. Merely
  saving a Jev key does not select this reranker. The existing local Qwen3 path
  and default provider remain unchanged.

The reranker scores up to 100 candidates in one request (48,000 UTF-8 bytes
maximum). Scores on a four-level rubric are normalized to 0–1. Source IDs,
content and citations are preserved; equal scores preserve initial order.
Failures are reported through Knowledge's existing reranker error boundary,
never represented as successful reranking. The existing retrieval diagnostics
show provider, original rank, final rank and score. This adapter has synthetic
smoke coverage, not a corpus-quality benchmark; compare representative queries
before replacing a base's existing retrieval profile.

## Room API and verification

`GET /api/agent/rooms/{roomId}/jev` lists roots; adding `graphId` reads one
projection. These reads never dispatch work. `POST` on the same route accepts
`action=create` with `message`, stable `clientMessageId`, optional attachments,
`strategy` (`auto`, `direct`, `plan`), `modelRouting`, `toolApprovalMode` and
`verificationMode` (`auto`, `independent`). Select `independent` when the request
requires review by someone other than its implementer.
The new Room interface sends `executionApproval=true`: a planning Session first
clarifies necessary missing information or produces a reviewable plan. No
worker starts while `planApproval` awaits input or confirmation. The user can
choose to start, adjust, or defer the whole plan. `approve_plan`, `adjust_plan`
and `defer_plan` bind `graphId`, `rootId`, `planHash` and a stable
`clientMessageId`; adjustments also send `message` and optional attachments.
An updated plan invalidates the earlier hash. Replaying approval cannot create
another task graph. This is one plan decision, not a per-task or per-tool gate.
Existing API callers and already running roots retain their prior behavior
unless they explicitly request this workflow.
New Agent entries default to Jev when no preference has been saved. An explicit
Traditional preference is preserved, ordinary single-Agent Sessions remain
available, and existing Rooms keep their recorded entry or task-graph owner.
`action=stop` targets the exact `rootId`. The Room event stream publishes
`jev_updated`; reconnecting clients reload the projection. A stopped root is
not resumed by stale callbacks. A new request can reference `previousRootId`
after that root has stopped or finalized.
An already-open empty Room also follows its first graph event and reads the
current owner without remounting its conversation or discarding an unsent draft.

Task details provide a change-partner action. `assignment_options` reads one
task's current `taskHash`, permitted action and eligible participant IDs;
it does not schedule work. Send that hash with `taskId`, `targetParticipantId`,
`reason`, `graphId` and a stable `clientMessageId` to `reassign` or
`request_reclaim`. A running attempt must be reclaimed first. A cancellation
receipt retains the old owner until its exact execution is proven drained;
the projection's `reclaims` distinguish waiting for stop from waiting for
assignment. The host rechecks target eligibility at handover. An uncertain
request is reconciled by replaying the same command key and unchanged intent.

Task details also support changing a current task's requirements. Read
`revision_options` for its exact `taskHash`, `rootId`,
`expectedTopologyRevision`, `expectedRequirementsRevision`, affected downstream
tasks and retained accepted tasks. Submit those bindings to `revise_task` with
`objective`, `expectedOutput`, one to eight `acceptanceCriteria`, `reason` and a
stable `clientMessageId`. An optional `rootObjective` updates the overall goal
alongside this explicit task change. The task keeps its owner, capabilities,
context and write scope. This operation revises an explicitly selected task;
it does not infer an arbitrary changed branch from a new chat message.

The host fences the affected old attempts immediately, requests exact
cancellation, and waits for real execution drain before atomically creating
successor WorkItems. Independent tasks and accepted results are retained.
`revisions` reports `awaiting_drain`, `applied` or cancellation; `activeTaskIds`
identifies the current graph, while historical WorkItems remain inspectable.
Committed `successors` mappings connect old tasks to their replacements.
Only current tasks count toward final acceptance. Replaying an uncertain
revision uses the same command key and unchanged payload; stopping the Root
prevents late receipts from activating replacements.

The UI shows planning, assignment, execution, verification, repair and final
answer from these receipts. New requests made during execution are queued for
the next root. Tool details remain inspectable after cancellation, and motion
respects reduced-motion preferences. Planet faces use separate SVG eyes, brows
and mouths for blinking, gaze and expression changes driven by live activity;
historical messages stay still.
Partner names and dispatch actions open the matching Session when their stored
Room, participant and Session identities agree. Task details expose supported
file, WorkDocument and Trace references; an unknown identifier stays copyable
instead of becoming a guessed file path. Room and Session share a compact
composer; partner settings and expanded draft editing open in dialogs.
Confirmed reclaims display a stopped handover only when the old execution has
drained and the current task points to an accepted execution by its new owner.
Original receipts remain available; an unconfirmed cancel or a separate model
error is not presented as a successful handover. Opening a Room with its work
directory closed does not scan the entire Session catalog.
Returned tasks retain their identity and revision budget. Their next Pi input
states the current revision and specific review feedback, alongside the
existing task-scoped materials, so a previous completion is not the new task.

Run isolated regression coverage with:

```bash
python3 -m unittest discover -s tests -p 'test_jev*py'
python3 -m unittest tests.test_pi_exact_turn_cancellation
```

The opt-in live canary uses a supplied Pi payload, actual configured Jev and
OpenAI Codex credentials, a private temporary database and synthetic files:

```bash
python3 scripts/canary_jev_room.py --runtime-payload /path/to/payload \
  --output /tmp/paw-jev-live-report.json
```

Add `--approve-plan` to exercise the whole-plan hold and HTTP approval replay.
For frontend/backend integration, use `--browser-approval --linger 300`: the
canary prints its private loopback API URL and Room ID, then waits for the real
frontend to approve the synthetic plan. Point the development frontend proxy at
that API and open its live Room fixture. No model calls or events are mocked.
The audit replays the browser's exact approval identity and checks the actual
worker results, dependency ordering, independent verdicts and drain receipts.

It makes real model calls and reports its own checks. Unit tests and browser
fixtures do not establish live execution or an installed product release.

## Candidate scenarios

| Scenario | Potential Jev responsibility | Existing owner retained |
| --- | --- | --- |
| Context compression | Decide which spans must survive | Pi generates summaries and owns compaction |
| Memory deduplication/conflicts | Compare candidate facts | Memory retains evidence and writeback authority |
| Retrieval routing | Decide whether/how to retrieve | Knowledge executes bounded retrieval |
| Evidence sufficiency | Identify unsupported answer requirements | Agent decides follow-up and generates the answer |

These four are not enabled by this integration. No summarization, memory
writeback, background routing or automatic extra calls are introduced.
