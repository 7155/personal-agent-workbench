---
name: facilitate-room
description: "Coordinate visible Partner responsibilities as the designated Room Facilitator. Use when the current request truly needs multiple independently accountable outcomes, dependency stages, or useful concurrency; choose the smallest topology, integrate Partner evidence, verify operability and requirement satisfaction, and publish one Root result. Do not use for one coherent action the Facilitator can complete directly, or in a Room Partner, Reviewer, private Tool Agent, or standalone Session; e.g., answering one code question needs no Room workflow."
---

# Facilitate a Room

Room is a lightweight composition of ordinary Pi Sessions. This Skill governs
visible responsibility topology and Root closeout; it does not define another
Agent loop, scheduler, context engine, permission system, document lifecycle, or
work method. Partners remain ordinary Sessions and use the same task Skills as a
standalone Session.

The Facilitator owns the Room Goal, WorkItem assignment, integration, evidence-
backed acceptance, and the single user-facing Root result. Partners are peers in
execution and may communicate directly, but they do not accept another
Partner's WorkItem or finalize the Root.

When Jev owns the task graph, a partner receiving a `purpose=plan` execution
pack is a planner, not the Room Facilitator. Use `implementation-planning` and
submit the plan through the supplied contract; leave dispatch, result decisions,
and Root completion to Jev. This Skill does not override that ownership.

## Workflow

1. **Align.** Read the current Root Goal, controlling requirements, acceptance,
   Runtime projection, and existing unresolved WorkItems. Inspect facts before
   asking the user. Use `alignment-and-decision` only when a material user-owned
   choice still changes the result.
2. **Choose the smallest topology.** Handle ordinary conversation and one
   coherent action directly. Use a private child through `agents` when the
   result is supporting evidence owned by the parent. Use a visible Room Partner
   only when the responsibility should be independently visible and accountable.
3. **Decompose by outcome.** Create the smallest independently inspectable
   WorkItems. Prefer vertical deliverables over arbitrary frontend/backend/test
   lanes. Record objective, scope, expected output, acceptance, owner,
   dependencies, capabilities, workspace, references, and result shape.
4. **Coordinate.** First establish interfaces and separate write targets so
   independently useful lanes can run together. Do not turn broad phases into
   a serial chain when that prerequisite can be removed by a small contract.
   Dispatch genuine dependencies later and retain only necessary integration
   barriers. Let Partners communicate directly instead of relaying their wording.
5. **Integrate.** Treat Partner output as a submission. Inspect the actual
   artifacts, Runtime receipts, and downstream effects rather than relying on
   the Partner's conclusion.
6. **Verify two axes.** Decide separately whether the implementation or real
   path runs and whether the observed result satisfies the current precise
   requirement. A pass on one axis is not closure.
7. **Recover truthfully.** Revise, retry, reassign, block, or abandon the same
   responsibility from current Runtime state. Do not create replacement work to
   hide a failed revision. Reconcile every item before closing the Root.
8. **Finalize once.** Automatically publish one user-facing results report:
   what was completed, where the deliverables are and how to open/use them,
   which checks actually ran, and what remains incomplete or unverified. Do
   not require the user to reconstruct the outcome from Tool logs. A short
   text-only conversation can use the answer itself as its report. Publish one
   Root result; mechanical terminality belongs to Runtime, not prose.

## Hard Invariants

- Partner assignment exists only after a real delegated dispatch. A Todo label,
  plan paragraph, or document heading is not an assignment.
- Artifacts and evidence are prerequisites for submission, not acceptance.
  Partner completion does not prove requirement satisfaction.
- When one stage has several independent lanes, use one `delegate_batch` call;
  never describe consecutive `delegate` calls as parallel.
- Delegation returns an immediate durable dispatch receipt. Completion arrives
  through a durable wake; use `room_partner collect` or `wait` only at an
  explicit integration barrier. A `wait` timeout never cancels the Partner.
- For a submitted WorkItem, explicitly call `accept` or `return` using the live
  revision and evidence. If a reviewer reported `unverified`, `failed`,
  `changes_required`, or unresolved material findings, do not record passed and
  satisfied.
- Do not pause a live Room Goal to wait. If progress cannot continue, emit the best evidence-backed partial or blocked final. Do not use pause a live Room Goal as a wait.
- Inventory unfinished, failed, orphaned, partial, and unclosed responsibilities
  before creating more work.
- Browser acceptance uses the product `browser` tool. A successful build,
  screenshot, or Partner statement is not foreground evidence by itself.

## Progressive Runtime Guidance

Use the currently disclosed `room_partner` schema, allowed operations,
`recommendedOperation`, WorkItem state, and live `authorityRevision`. Do not
reconstruct the Room state machine from memory. Read
[references/runtime-operations.md](references/runtime-operations.md) only when a
current dispatch, review, recovery, document binding, or terminal operation
needs its compatibility details.

Before any requirements, business-code, configuration, or test write, use the
existing Root Goal when one is already active. If the Runtime requires setup,
`agent_goal confirm_setup` establishes that authority. A useful audit document
may be bound with
`workDocument={authorityKind:"session_goal", authorityId:<goalId>, authorityRevision:<revision>, title:<title>}`.
A successful `workDocumentRegistration` is evidence, never a delegation or
acceptance gate. When binding fails, do not create an unbound requirements file.
A plain `docs/agent/requirements.md`, Todo, or Room post is not that binding.
WorkDocuments are optional semantic evidence; their failure must not block
non-document work.

## Independent Review

Author verification belongs to the implementer; integration verification
belongs to the Facilitator. Add `independent-review` only when the user requests
it or the risk justifies a genuinely separate reviewer. The Reviewer owns the
fixed-scope verdict, not repair assignment or the final user response.

## Output

Return the common `AgentResult` envelope with:

```text
Root Goal and topology | WorkItems, owners, and dependencies
dispatch and Runtime receipts | integrated outcome
operability verdict | requirement-satisfaction verdict
accepted, revised, reassigned, blocked, or abandoned items
artifacts and evidence | document refs when useful
residual risks | unverified boundaries | next action
```

## Not For

Do not load this Skill merely because the Session belongs to a Room. Do not use
it inside a Partner, Reviewer, private child Session, or standalone Session. Do
not reproduce task-method instructions, grant permissions, infer Runtime state
from Markdown, manufacture parallel work, make review mandatory, or make a
WorkDocument a completion gate.

Example: a Facilitator answering one bounded code question works directly. A
feature with independently accountable runtime and UI outcomes may delegate
those visible responsibilities, integrate their evidence, and return one Root
result.

## Jev-owned execution

When the current execution pack identifies a Jev `purpose`, its task graph owns scheduling. Use `room_partner list` to read that bound purpose and its disclosed submission operation. For `plan`, submit a bounded typed plan with `plan_submit`; for `execute`, submit actual result/evidence with `result_submit`; for `verify`, independently inspect the fixed result then use `verification_submit`; for `synthesize`, use `final_submit`. These operations are available only to the matching live Pi execution. Do not use legacy delegate/retry/accept/return to bypass the graph. Stop remains the original Runtime stop. A submission receipt is not verification or execution drain; finish the turn after a successful submission.
