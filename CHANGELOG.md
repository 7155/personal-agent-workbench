# Changelog

Personal Agent Workbench follows a source-first development model. A public
source checkpoint is not a signed and notarized macOS binary release.

## Unreleased

- Include current prerequisite revisions, acceptance state and result
  availability in Jev scheduling decisions. Dependency materials also carry
  the canonical completion timestamp so verifiers can check acceptance before
  downstream execution. Do not confuse historical worker
  prose or completed verification with a new dependency barrier; reference
  edges remain non-blocking, and downstream execution still needs real receipts.
- Route Pi native `read` callbacks for managed resource references to their
  authorized owners, preserving line-based pagination and bounded output.
  Jev verifier instructions now use the actual native read contract.
- Keep planner-assigned work owners separate from runtime-selected independent
  verifiers; represent explicitly requested named inspections as owned tasks
  rather than unsupported promises in another task's acceptance text.
- Keep reading-motion preferences usable with legacy media-query listeners or
  preview environments without media-query subscriptions.

- Add Workspace V3 reading navigation and local text/motion preferences, task
  and delivered-file filters, collapsed ended-task history, and explicit
  copy/download feedback without changing execution or file authority.
- Add an on-demand Multi-Agent V4 collaboration panorama: stable dependency
  layout, partner responsibilities and revision-bound execution/review receipts.
  Keep reference edges separate from blocking dependencies, use readable lists
  in narrow windows, and reuse the existing task Dialog and operations.

- Add TypeSafe / Jev credentials and scenario status to Model accounts, and an
  opt-in Knowledge reranker that preserves source content and citations. Keep
  approval fallback explicit and reject malformed typed decisions.

- Connect Jev Room planning, dependency dispatch, independent verification,
  repair and synthesis to ordinary Pi Sessions, durable events and exact
  execution receipts. Add GPT-6 model routing and role cards, optional automatic
  Jev decisions for dangerous tools, and visible task/Tool progress with
  reduced-motion support. Preserve read-only projections and historical Rooms.
- Let users review, adjust or defer a complete Jev plan before execution, with
  clarification when necessary and version-bound idempotent approval. Keep the
  central conversation stable as active partner windows change by phase, and
  use consistent planet avatars across collaboration surfaces.
- Open approved Jev plans in a separate, scrollable window with visible
  dependencies, deliverables and acceptance criteria. Keep the heading and
  close control in view, and preserve the conversation position while new
  partner activity arrives.
- Show delivered file evidence when its path includes a SHA-256 annotation,
  keeping the full original reference available in task details.
- Preserve complete Jev revision feedback when optional context metadata
  exceeds the prompt budget. Include the current worker's Browser command
  receipts in independent verification, and let that dispatch stop its own
  temporary background jobs without another approval.
- Allow explicit Jev responsibility reassignment through its existing task
  owner, with current-version and execution checks. Persist assignment,
  command receipt and follow-up event together, and replay prior receipts
  without redispatching work.
- Keep in-flight Jev partner steering available after old Room messages are
  trimmed. Validate the current Root and exact execution claim before sending
  through Pi, preserving duplicate-message protection and legacy Room behavior.
- Recheck missing Jev verification evidence against the same submitted result
  before requesting code changes. Bound follow-up verification attempts and
  report unresolved evidence explicitly when they are exhausted.
- Let users change a task's responsible partner from its details window.
  Show eligible partners on demand, reclaim running work through exact Pi
  cancellation, and retain the old owner until execution has stopped. Preserve
  uncertain command keys across view reloads and show pending handover progress.
- State the current task revision and specific review feedback in a returned
  task's next Pi input, preserving its original scope and retry budget.
- Follow the first Jev graph arriving in an open Room without losing its draft.
  Allow the change-partner request through the real frontend transport and
  distinguish an unsent local rejection from an uncertain execution outcome.
- Let a simple direct task use its existing partner for a separate verification
  turn when no other reviewer is available. Keep explicit independent review,
  planned tasks and previously created roots bound to a different partner.
- Use a compact composer with one toolbar row across Room and Session, keeping
  partner settings in a dialog and preserving drafts in expanded editing.
  Open bound partner identities and dispatch records in their real Sessions;
  expose task and document destinations without inventing links for raw IDs.
- Show an exactly confirmed Jev handover as a stopped execution while retaining
  its original receipt. Keep genuine provider failures and uncertain outcomes
  visible. Load Room metadata without waiting for the full Session directory,
  and cancel directory pagination when it is no longer needed.
- Explain the existing 2,000-character workspace command limit to the model
  and distinguish overlong commands from invalid control characters.

- Preserve original Knowledge source wording and connect reader tables/images
  to persisted source blocks. Retain partial chart caches and explicit preview
  limits; add PDF hit-page navigation and recovery guidance for blank previews.

- Add EPUB spine parsing, Office image/chart evidence and optional local
  LibreOffice conversion for legacy DOC/XLS/PPT. Keep missing chart data explicit
  and exclude printed page numbers from standalone Knowledge search evidence.
- Preserve Memory organization checkpoints across restart and recover active
  jobs beyond recent history. Refresh observer progress from durable receipts;
  distinguish unreadable status from execution failure in the organization UI.
- Preserve MinerU content-list structure, page coordinates and raw JSON through
  Knowledge ingestion and citations. Add paper chunking with retained references
  and bounded table splitting; improve DOCX paragraphs/tables, XLSX cells/sheets
  and PPTX slide/notes parsing. Keep structure available after re-chunking,
  restart and portable search export, with backward-compatible chunk storage.
- Add request-only Knowledge retrieval tests with resettable parameters and
  recorded result settings. Filter stale index candidates before ranking, and
  apply score thresholds after optional reranking. Fence indexing attempts
  against newer retries and preserve cancellation when late work completes.
- Keep long local MinerU parses alive with a configurable per-document deadline,
  distinguish timeout from service-unavailable errors, and restart workers when
  the deadline changes.
- Show recorded RAG parameters, candidate counts, retrieval and reranker ranks
  in Knowledge search and Lab recall tests. Distinguish configured models from
  stages that actually ran, and preserve unavailable measurements as unknown.
- Recover Knowledge search after cancellation, timeout and tab switches;
  keep late responses from replacing a newer query. Give document actions
  enough space and wrap long titles in the detail panel.
- Redesign the Lab home, project navigation and experiment reports. Visualize
  recorded stages, quality and cost, with precise artifact selection and source
  references; retain historical limitations and the original records.
- Guide projects through material intake, execution, optimization and App
  delivery, with responsive previews, local App state and file downloads.
- Route isolated HTML previews through the desktop and development hosts so
  embedded Apps load their own interface and retain the preview policy.
- Browse local files independently of Agent Sessions; keep existing Session
  authority for editing and saving.
- Include the actual retrieved evidence when judging Knowledge answers, under
  a newly versioned protocol. Preserve previous judgments and calibrations.
- Install and package the shared Agent controls needed by exported Apps, and
  remove unused frontend components, assets and dependencies.

## 0.1.0-alpha.7 — 2026-09-12

- Lead the README with reducing communication and model costs, with a labeled
  context-reuse example and source-linked vertical task evaluation results.
- Keep Memory lifecycle metric labels in the text column when no icon is
  supplied, with responsive layout checks for PAWOS and the compatibility UI.
- Preserve Browser connection failures and Chromium error-page state instead
  of reporting an invalid URL. Project failed tool receipts consistently in
  live and restored conversations, and support recovery in the same tab.
- Continue accepted failed turns in their existing Session, while retaining
  the original submission when an unaccepted message needs a retry.
- Integrate the Agent Capsule entry, desktop context requests and shared
  conversation controls.
- Add Memory import/export, daily reports and privacy filtering before
  capture. Resume owner-scoped maintenance jobs without letting observers
  start duplicate work or store redacted placeholders as Evidence.
- Extend Lab project, material and evaluation workflows with persisted
  receipts. Keep preparation completion distinct from evaluation results.
- Consolidate descriptor routing and Session/Trace lifecycle ownership, and
  refine stellar materials and workspace presentation.

## 0.1.0-alpha.6 — 2026-09-10

- Enter the collaboration layout only through its explicit control. Show
  persistent background Bash and browser observers inside that layout;
  dismissing an observer leaves its tool running and resource completion closes it.
- Default conversation memory to off and add partner-specific memory, tool,
  plugin and Skill controls to Room composers. Honor explicit capability
  choices in Full Access; filter disabled packages before Pi imports them.
- Explain invalidated model credentials with account reconnection guidance,
  including failures retained in existing Room history.

## 0.1.0-alpha.5 — 2026-09-10

- Make Full Access execute tools without workspace path restrictions or
  per-action human/model approval, including existing Full Access Sessions.
  Keep stored policy identifiers compatible and report the effective policy
  consistently in the UI and Runtime prompt.

## 0.1.0-alpha.4 — 2026-09-10

- Make stellar cloud drift and planet movement visible while the desktop is
  exposed. Move crossfade layers together and retain pause/reduced-motion rules.
- Fill the Trace Agent window with its navigation and content, preserving
  compact navigation on narrow windows and scrolling for long reports.

## 0.1.0-alpha.3 — 2026-09-10

- Add an unsigned Apple Silicon offline DMG installer with a native installation
  window, bundled Python and Pi Runtime, payload verification and pre-update
  component backups. Reuse the stack installer without target-machine builds.
- Connect installed Electron voice settings to the existing Voice Agent for
  status, service controls, permission requests and credential configuration.
  Desktop voice status no longer waits on unrelated global runtime probes.

## 0.1.0-alpha.2 — 2026-09-10

Source preview; no signed or notarized macOS installer is included.

- Correct the installed app, Finder and pinned Dock name to Personal Agent
  Workbench. Preserve existing launch paths, bundle identity and user data;
  retain previous app bundles for local recovery.


- Give Room work, dispatch, cancellation and approval services explicit
  dependencies; preserve shared stores, event ordering and Runtime replacement.
- Separate Host transport from Session lifecycle, group Pi/Room sources by
  capability, and add typed construction boundaries and an offline Lab example.

- Retire the legacy Pi RPC executor and protocol selection. Shared configuration
  and factory code now construct only the protocol 2 Host; old transcript data
  remains readable, while protocol 1 packages cannot be built or activated.
- Name the current PAWOS React frontend React OS; keep existing product,
  Runtime and persisted layout identifiers compatible.
- Group Agent Lab backend modules under `rag_ime/agent_lab/` and place portable
  Agent controls with their shared Agent owner; update imports and export paths.
- Move supplied frontend reference archives and dated acceptance records into
  their owning directories, retaining original contents and attribution.

## 0.1.0-alpha.1 — 2026-09-07

First tagged PAW source preview. This release does not ship a signed or
notarized macOS installer.

### Highlights

- PAWOS desktop with persistent Agent Sessions, lightweight Rooms, tools,
  governed memory, document Knowledge, and Agent Lab.
- Generic Lab project materials, evaluation history, Knowledge workflows, and
  versioned Extension App preview/export.
- Shared Agent model selection, reasoning controls, progress and error recovery
  for embedded and exported App conversations; local PAW execution preserves
  the existing Pi Provider/OAuth boundary.
- Durable App request history, cancellation and reconnect without replaying an
  uncertain invocation; source-bound exports and document citations.
- Restored background cancellation and process-group cleanup on macOS Python
  3.12 with non-reaping child observation; retained protection against PID reuse.
- Gateway updates stop only their own stale listener, preserving isolated
  source candidates on other workspaces and ports.
- Rewritten project README and separate local-operation documentation; corrected
  Electron build path and current Pi compatibility reference.

### Earlier development changes included in this source preview

### Added

- Structured Agent Sessions and lightweight multi-Agent Rooms that compose
  ordinary Pi Sessions while retaining durable dispatch, cancellation, ordered
  public events, and terminal evidence.
- Capability-bound Tool disclosure, authorization, approval, execution, and
  terminal receipts.
- Protocol-owned Pi v1/v2 selection and an enforced public runtime contract.
- Paper-workspace Control Center UI with responsive Session and Room views.
- Local browser, voice, foreground-context, knowledge, planning, governed
  memory, backup, and restore boundaries.
- Separate public-source and distributable-binary readiness decisions.
- Governed coordinator background jobs include approval-bound launch, durable
  redacted cursor logs, lifecycle events, management APIs, cancellation, and
  Control Center controls. Focused validation, current-source browser preview,
  and installed backend-connected acceptance pass; installed native-app visual
  acceptance remains open.
- Session-local phased Todo tracking and Goal lifecycles, per-capability
  Tool/Skill disclosure, Session overrides, governed workspace operations,
  explicit LSP degradation, and active/archived Work Document flows share
  backend-owned contracts with Control Center projections. `todo` and
  `agent_goal` are discoverable through the managed Pi Tool catalog.
- Source support lets Room messages carry bounded, Room-owned clipboard or file
  images through authorized participant dispatch without exposing Session-private
  media; focused image-flow validation and same-snapshot installed acceptance
  remain open.

### Changed

- The public project identity is now Personal Agent Workbench; the default
  on-device companion is “澄.”
- Python 3.12 is the declared and tested minimum.
- Model selection is feature-owned: Agent/Room roles, Active RAG, and voice
  refinement use real runtime model catalogs instead of dead settings.
- The approval-gated Agent Plan lifecycle is retired. Migration `0135` converts
  active legacy rows to phased Todo events, renames causal receipts, and drops
  the old Plan tables; Todo records progress but never grants execution authority.
- HTTP route ownership is consolidated for ordinary families, with a
  fail-closed ratchet for deliberately special routes.
- Room-turn bookkeeping has one mutation owner.
- Pi v1 and v2 share explicit public projection/value contracts.
- Managed Rooms now use one Facilitator Session plus optional Partner Sessions;
  parallelism and independent review are selected only when useful, while the
  Facilitator integrates evidence and owns one final result.
- Core work guidance is now a conditional set of task Skills shared by
  standalone Sessions, Partners, and Tool Agents. The fixed
  execution/quality/review/handoff/archive pipeline is retired.
- The Control Center Room timeline now separates live reasoning, Tool activity,
  public participant reports, shared review, and the final reply in equal,
  responsive participant lanes with user-facing status language.
- Settled Room reports now show the linked runtime response's Provider, model,
  input/output/cache usage, and an exact Session-turn context-inspection action;
  missing telemetry is labeled unavailable instead of inferred.
- The Control Center Room Tasks view now leads with a responsive lifecycle
  graph from creation through participant work and execution to the shared
  result. Per-participant details remain available in a secondary disclosure;
  final-state rendering stays bound to the authoritative Root projection.
- Agent and Room prompts now keep the durable core, discovered Skills, working
  directory, and Tool schemas as a cache-stable prefix. Execution policy,
  query-aware recalled memory, workflow/lifecycle state, and current-turn context
  are ordered hidden runtime messages; recall refreshes only at Session start or
  after compaction instead of being rebuilt on every turn.
- Ask-style user questions now replace the ordinary Agent or Room composer with
  an in-place, responsive answer card; no modal masks the conversation, and the
  timeline reserves the card's measured height while the turn is waiting.
- Ordinary Agent Sessions now keep `ask` and `todo` as non-hideable base
  capabilities. Ask remains Pi-host-owned, Todo keeps the backend-owned
  lifecycle contract, and material user-owned choices route through the
  alignment-and-decision Skill before structured Ask UI is shown.
- The no-tools Provider cache Canary now accepts and records an explicit
  thinking level, so Luna Max cache evidence cannot silently run at a weaker
  reasoning setting.

### Fixed

- Agent approvals now retain their originating Tool-call identity and causal
  turn, so pending and fail-closed Luna decisions stay inside the owning task
  row instead of creating a duplicate timeline task.
- Exact failed Room Tool commands cannot loop under a new call ID without new
  successful Tool evidence.
- A terminal Tool invocation receipt cannot execute twice.
- Worktree-based audit paths resolve through Git's common directory.
- IME display feedback is deduplicated and stale fuzzy holdover is removed.
- Duplicate definitions, unreachable route branches, a live IME `NameError`,
  and response-contract enforcement gaps were repaired.
- Native Control Center Room creation and policy updates now forward governed
  execution fields instead of rejecting the Web contract at the bridge.
- Sealed Room maintenance now uses lightweight control-state fencing, preserves
  transcripts when reopening revoked nonresident Sessions, and treats repeated
  Tool-disclosure receipt timestamps as idempotent instead of blocking compaction.
- Ask answers now close only after a contiguous durable resolution event, and
  the Agent status rail consumes the current `workspace_lsp` capability
  projection instead of inferring readiness from historical Tool receipts.
- Room WorkItems now mirror canonical Kernel lifecycle changes and startup
  reconciliation; blocked work is no longer presented as actively executing.
- Room settlement now deduplicates review invitations, retries recoverable
  commits without losing accepted work, and reconciles transient resume lanes
  against the authoritative terminal Root.
- Final integration repair restored the Tool-matrix write receipt, current Room
  migration fixtures, DeepSeek memory-organizer transport, bounded runtime Tool
  manifests, and clarification routing through the dedicated Room Tool.
- Foreground verification now recognizes the AppKit assistant overlay as the
  authoritative post-commit surface, accepts current action-button badges,
  commits native Rime composition before assistant selection, and fails before
  clearing evidence when frontend tracing is disabled.
- Agent project headers now truly hide their conversation rows and preserve
  manual collapse across live Session-summary refreshes; wide desktop Agent
  workspaces open the renamed task center by default.
- Installed-product audit now recognizes the gateway-owned
  `memory-maintenance-trigger` marker at the memory-book maintenance component
  path instead of reporting a false overwritten-marker failure.
- Squirrel preparation now validates the capture-delivery-aware frontend trace
  gate, typechecks the durable input-capture outbox with its client, and uses a
  narrow cross-file delivery method instead of inaccessible private helpers.

### Validation

- `python3 -m unittest discover -s tests` passed 3,289 tests with three
  explicitly skipped cases in 1,429.139 seconds. The preceding audit's sole
  stale execution-policy wording assertion was repaired and the entire suite
  rerun. Existing unclosed-SQLite `ResourceWarning` output remains visible.
- Control Center passed 96 files / 813 tests and its production build. Full
  Playwright passed 160 cases with 35 intentional skips.
- Background jobs passed 235 focused backend lifecycle, Tool, HTTP route,
  projection, Session, and Room cancellation tests; the Control Center path
  passed 5 focused files / 188 tests and a production build. A current-source
  browser preview exercised list, cursor-log disclosure, and two-step cancel UI.
  An installed managed-Pi turn then loaded `workspace_job`, reached `running`
  after governed approval, returned cursor logs, cancelled the process group,
  persisted `cancelled`, and published started/progress/cancelled events.
  Installed native Control Center visual acceptance remains open.
- The Room task-flow source passed 26 focused preview/control tests and
  TypeScript checks. Browser inspection at 1440×1000 and 430×900 verified two
  task/dispatch paths, stage order, detail disclosure, and no graph overflow.
  This proof used the current-source preview transport, not an installed app.
- Import boundaries passed. Route ownership passed with 91 dispatched routes,
  203 declared routes, and 40/40 undeclared-route checks. All 156 generated
  Control Center contracts reproduced without drift.
- The current managed Pi payload passed protocol-v2, OAuth, and Runtime hello
  smoke checks, then installed and activated as the current verified generation.
- A real TextEdit/Squirrel run produced 84 privacy-safe AppKit events and passed
  native Rime composition, post-commit overlay visibility, source badges,
  Option+number assistant selection, commit, and follow-up checks. The focused
  foreground analyzer suite passes 79 tests.
- The repository-only release audit remains blocked by the dirty worktree and a
  pre-existing OpenAI-key-shaped test fixture at
  `tests/test_historical_memory_curation.py:275`; it also reports pending
  tracked-file deletions. Distribution remains blocked by foreground acceptance,
  the declared blocked status, and the missing release manifest.

### Known Distribution Gates

- Fresh installed foreground acceptance for Squirrel, Accessibility, and voice.
- Candidate-quality sign-off for the configured local model.
- Developer ID signing, notarization, stapling, clean-machine installation, and
  a hash-verified `rag-ime.release-manifest.v2`.
