---
name: implementation-planning
description: "Turn a confirmed change into the smallest dependency-aware plan without executing or assigning it. Use when work spans multiple dependent steps, owner boundaries, shared contracts, integration points, or rollback seams and a plan will materially reduce risk. Do not use for one coherent action, an unknown failure, an unresolved material choice, or to manufacture parallel tasks; e.g., one localized edit plus its focused test needs no plan."
---

# Plan Implementation

Produce only the minimum plan needed to make a confirmed change executable.
Planning describes responsibility and dependency topology; Runtime owns actual
Sessions, assignments, workspace bindings, and live state.

## Workflow

1. Read the confirmed requirement, acceptance, relevant decisions, ContextRefs,
   and current implementation seams.
2. Stop and route to `alignment-and-decision` only when a newly discovered
   material user-owned choice prevents a valid plan. Route an unknown failure to
   `systematic-debugging` instead of planning around a guess.
3. Map every acceptance criterion to an observable implementation seam and two
   distinct verification questions: whether the implementation or real path
   runs, and whether the observed result satisfies the current precise
   requirement.
4. Create the smallest vertical WorkItems that yield independently inspectable
   results. Do not use file lists, technology labels, or test/documentation
   phases as artificial tasks.
5. Record objective, expected output, acceptance, dependencies, owner role,
   verification responsibility, integration order, rollback point, exact refs,
   and capability/workspace needs for each item.
6. In a multi-partner Room, actively look for useful parallel deliverables
   before choosing a serial plan. Agree on small interfaces, data shapes, and
   separate write targets first so partners can produce compatible work at the
   same time. Add a dependency only when a task actually consumes an unfinished
   result; narrative order, shared subject matter, or a preference to finish one
   broad phase first is not a prerequisite. Keep only the necessary integration
   barrier after independent work. Explain why genuine prerequisites prevent
   concurrency when no useful parallel frontier exists. Do not manufacture
   tasks merely to fill every partner. Recommend a separate review only when
   the user request or missing evidence makes it useful.
7. Return the executable frontier and proposed workboard delta. Leave Agent
   creation, dispatch, reassignment, and execution to the supervising Session or
   Room Facilitator. In a Jev-owned plan purpose, submit the bounded plan through
   the supplied `plan_submit` contract. Jev owns dispatch and acceptance; do not
   load `facilitate-room` or take over its orchestration.

## Output

Return the common `AgentResult` envelope with:

```text
WorkItems and owner roles | acceptance-to-seam mapping
dependencies | executable frontier | integration order
operability checks | requirement-satisfaction checks
capability and workspace needs | rollback points
review recommendation | proposed workboard delta
```

## Not For

Do not execute work, create or assign Agents, reproduce a Runtime state machine,
force parallelism, invent a worktree, or make planning and review mandatory.

Example: a cross-process contract change with a migration, frontend consumer,
and rollback boundary benefits from a plan. A localized behavior change with one
known owner and focused test should proceed directly.
