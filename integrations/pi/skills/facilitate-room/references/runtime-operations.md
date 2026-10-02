# Room Runtime Operations Reference

Read this file only after the Facilitator has selected a concrete Room action.
The live Tool schema and returned Runtime projection are authoritative; this
reference explains intent and recovery boundaries without replacing them.

## Inspect And Recover

Start from `room_partner list` when the current WorkItem state or available
participants are not already present in the Runtime context. Prefer the returned
`allowedOperations`, `recommendedOperation`, IDs, and revisions. A returned
active item with review feedback continues through the same WorkItem revision
chain; do not create a replacement item merely to retry it.

## Delegate

Use `delegate` for one visible responsibility. Use `delegate_batch` once for a
stage containing multiple independent, non-overlapping responsibilities. A
dispatch receipt proves that the assignment was created, not that work finished.
Keep its `workItemId` and `childDispatchId` for later collection and evidence.

A bounded Partner brief carries:

```text
requirement refs | objective | scope | expected output | acceptance
current owner | accountable owner | dependencies
ContextRefs | SkillRefs | capabilities | workspace | result shape
```

Send references instead of copied transcripts or full documents.

## Wait And Collect

Partner execution is asynchronous. A completion wake tells the Facilitator to
integrate; it is not automatic acceptance. Use `collect` for current results and
`wait` only at a real barrier where no useful supervising work remains. Timeout
means still running and does not cancel, fail, or justify duplicate delegation.

## Submit And Review

A Partner submits a typed `work_result` with an honest proposed operability and
requirement verdict plus artifact or evidence refs. The WorkItem enters review.

The Facilitator accepts only when current evidence supports both:

```text
operability = passed
requirement = satisfied
```

Otherwise return it with concrete feedback. Continue a returned or failed item
through the Runtime's current retry/reassignment operation using its live
revision. Never overwrite a failed or unverified proposal with a passing review
without new, explicitly linked superseding evidence.

## Progress And Final Result

A `room_partner post(kind=progress)` is non-terminal. Use it only for material
information a user should see; do not narrate every Tool call or infer
terminality from a content prefix.

The current compatibility path is:

1. Reconcile every WorkItem and record the Root operability and requirement
   verdicts from evidence.
2. For success, call `room_partner post(kind=result)` exactly once. Only after
   that receipt succeeds, call `agent_goal complete`, then end the ordinary
   assistant turn so Pi can settle it.
3. For a partial or blocked boundary, call `room_partner post(kind=blocked)`
   once with what is established, the blocker, and one executable next action.
   Do not pause the live Root merely to wait for another message.

Runtime owns idempotency, unique terminality, wake suppression, and settlement
validation. These calls express the semantic result required by the current
compatibility contract; they do not make Skill prose the state authority. When
the Tool exposes a newer atomic terminal operation, follow that live schema
instead of preserving this sequence from memory.

## WorkDocuments

A bound WorkDocument can preserve stable requirements, decisions, evidence, and
handoff. Read the live authority revision from Runtime immediately before a
bound write. Registration, hash, or sync failure should produce one visible
residual item and must not cause duplicate delegation, retry, return, or failure
unless the requested deliverable is the document itself.

## Browser And Foreground Evidence

Use the PAW product `browser` Tool for web acceptance. Do not launch or drive a
second desktop browser. Native foreground claims require the corresponding
foreground receipt; source tests and screenshots alone do not prove them.

## Jev-owned execution

When the current execution pack identifies a Jev `purpose`, its task graph owns scheduling. Use `room_partner list` to read that bound purpose and its disclosed submission operation. For `plan`, submit a bounded typed plan with `plan_submit`; for `execute`, submit actual result/evidence with `result_submit`; for `verify`, independently inspect the fixed result then use `verification_submit`; for `synthesize`, use `final_submit`. These operations are available only to the matching live Pi execution. Do not use legacy delegate/retry/accept/return to bypass the graph. Stop remains the original Runtime stop. A submission receipt is not verification or execution drain; finish the turn after a successful submission.
