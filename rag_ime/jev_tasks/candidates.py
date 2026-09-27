"""Construct complete, bounded actions from canonical tasks and owner facts."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass

from .context import ContextManifest
from .ledger import Snapshot
from .owner import execution_dict
from .types import Candidate, ExecutionFact, Executor, GraphError, digest, integer, text


def needs_verification_evidence(operability: str, requirement: str) -> bool:
    """An unknown axis is not a defect; an observed failure still needs repair."""
    return (operability in {"passed", "unverified"}
            and requirement in {"satisfied", "unverified"}
            and "unverified" in (operability, requirement))


@dataclass(frozen=True)
class Verification:
    """Supplied by the actual verification owner, never parsed from worker prose."""

    task_id: str
    task_hash: str
    operability: str
    requirement: str
    reason: str
    evidence_refs: tuple[str, ...]

    @property
    def needs_evidence(self) -> bool:
        return needs_verification_evidence(self.operability, self.requirement)

    def __post_init__(self) -> None:
        if self.operability not in {"passed", "failed", "unverified"}:
            raise GraphError("invalid operability verdict")
        if self.requirement not in {"satisfied", "not_satisfied", "unverified"}:
            raise GraphError("invalid requirement verdict")
        text(self.reason, "verification reason", 2000)
        if not self.evidence_refs or len(self.evidence_refs) > 24:
            raise GraphError("verification needs bounded evidence refs")
        for ref in self.evidence_refs:
            text(ref, "evidence ref", 1000)


@dataclass(frozen=True)
class CandidateSet:
    actions: tuple[Candidate, ...]
    missing: tuple[str, ...]


@dataclass(frozen=True)
class OwnerPreference:
    """Host-observed comparative fit for one current, not-yet-running owner.

    The host must ground this in a task-specific declared responsibility. It is
    advisory; eligibility and the exact execution fact remain separate gates.
    """

    owner_id: str
    participant_id: str
    task_revision: int
    basis: str

    def __post_init__(self) -> None:
        text(self.owner_id, "preference current owner")
        text(self.participant_id, "preference target")
        integer(self.task_revision, "preference task revision")
        text(self.basis, "preference basis", 1000)
        if self.owner_id == self.participant_id:
            raise GraphError("owner preference must identify a different participant")


def build_candidates(
    snapshot: Snapshot,
    *,
    event_id: str,
    executions: Mapping[str, ExecutionFact],
    executors: Sequence[Executor],
    eligible_pairs: frozenset[tuple[str, str]],
    manifests: Mapping[str, ContextManifest],
    owner_preferences: Mapping[str, OwnerPreference] | None = None,
    eligibility_missing: Mapping[str, str] | None = None,
    verifications: Mapping[str, Verification] | None = None,
    recovery_needed: frozenset[str] = frozenset(),
) -> CandidateSet:
    """Host supplies actual capability/permission-qualified pairs and recovery facts.

    No lexical role guessing; no model grants scope. User model/assignee locks
    must already be represented in eligible_pairs. Task-specific comparative
    preferences only expose eligible alternatives; they never grant eligibility.
    A queued legacy offer is not another ready task. Missing global/runtime
    data does not mean idle.
    """
    text(event_id, "event id")
    graph = snapshot.graph()
    frontier = graph.frontier(executions)
    actors = {x.participant_id: x for x in executors}
    if len(actors) != len(executors) or len({x.session_id for x in executors}) != len(
        executors
    ):
        raise GraphError("duplicate executor identity")
    if any(
        task not in graph.tasks or actor not in actors for task, actor in eligible_pairs
    ):
        raise GraphError("eligibility references a missing task/executor")
    if not recovery_needed <= graph.tasks.keys():
        raise GraphError("unknown recovery task")
    preferences = owner_preferences or {}
    missing_eligibility = eligibility_missing or {}
    if any(task_id not in graph.tasks or not isinstance(preference, OwnerPreference)
           for task_id, preference in preferences.items()):
        raise GraphError("invalid owner preference reference")
    reclaiming = {x["task_id"] for x in json.loads(snapshot.reclaims_json)}
    revising = set(snapshot.pending_revision_task_ids)
    actions, missing = [], []
    in_doubt = {
        task.id for task in snapshot.tasks if snapshot.pending_dispatches(task.id)
    }
    missing.extend(
        task_id + ": pending runtime admission requires reconciliation"
        for task_id in sorted(in_doubt)
    )
    for task_id in frontier.ready:
        task = graph.tasks[task_id]
        if task_id in reclaiming or task_id in in_doubt or task_id in revising:
            continue
        if task_id in missing_eligibility:
            missing.append(task_id + ": " + missing_eligibility[task_id])
        fact = executions[task_id]
        if (task.accepted_turn_id or fact.status == "drained") and not fact.effects_reconciled:
            missing.append(task_id + ": previous execution effects need reconciliation")
            continue
        manifest = manifests.get(task_id)
        if (
            manifest is None
            or manifest.task_id != task_id
            or manifest.task_revision != task.revision
        ):
            missing.append(task_id + ": context not prepared for current revision")
            continue
        current = actors.get(task.owner_id)
        owner_ready = (
            current is not None
            and current.available
            and current.session_id not in snapshot.reserved_sessions
            and (task_id, task.owner_id) in eligible_pairs
        )
        preference = preferences.get(task_id)
        preferred_id = (
            preference.participant_id
            if preference is not None
            and preference.owner_id == task.owner_id
            and preference.task_revision == task.revision
            and preference.participant_id in actors
            and actors[preference.participant_id].available
            and actors[preference.participant_id].session_id not in snapshot.reserved_sessions
            and (task_id, preference.participant_id) in eligible_pairs
            else ""
        )
        for pid, executor in sorted(actors.items(), key=lambda pair: (pair[0] != preferred_id, pair[0])):
            if (
                not executor.available
                or executor.session_id in snapshot.reserved_sessions
                or (task_id, pid) not in eligible_pairs
            ):
                continue
            # An equally eligible alternative is not a reason to move a ready
            # owner. An explicit, task-bound better fit can be offered first.
            if owner_ready and pid not in {task.owner_id, preferred_id}:
                continue
            if pid == task.owner_id:
                dispatch_id = (
                    "jev-dispatch:"
                    + digest([snapshot.graph_id, event_id, task.binding])[:40]
                )
                actions.append(
                    Candidate.make(
                        "claim_dispatch",
                        task_id,
                        f"执行责任「{task.objective}」；使用执行者 {pid}：{executor.description}。已有当前版本上下文。",
                        {
                            "dispatchId": dispatch_id,
                            "contextManifest": manifest.for_executor(),
                            "execution": execution_dict(fact),
                        },
                    )
                )
            else:
                preferred = owner_ready and pid == preferred_id
                actions.append(
                    Candidate.make(
                        "reassign",
                        task_id,
                        f"把尚未运行的责任「{task.objective}」交给 {pid}：{executor.description}；任务身份与历史不变。"
                        + (f"当前任务的执行者偏好依据：{preference.basis}。" if preferred else ""),
                        {
                            "targetParticipantId": pid,
                            "reason": (
                                "Jev selected a better task-specific owner: " + preference.basis
                                if preferred else "Jev selected a capability-qualified executor"
                            ),
                            "execution": execution_dict(fact),
                        },
                    )
                )
    for task_id in frontier.review:
        if task_id in reclaiming or task_id in in_doubt or task_id in revising:
            continue
        task = graph.tasks[task_id]
        proof = (verifications or {}).get(task_id)
        if (
            proof is None
            or proof.task_id != task_id
            or proof.task_hash != digest(asdict(task))
        ):
            missing.append(
                task_id + ": current result has no bound verification evidence"
            )
            continue
        if proof.needs_evidence:
            missing.append(task_id + ": current result needs additional verification evidence")
            continue
        operation = (
            "accept"
            if proof.operability == "passed" and proof.requirement == "satisfied"
            else "return"
        )
        # Existing owner's revision limit still applies. Do not invent a new
        # task to evade its retry limit.
        if operation == "return" and task.revision >= 2:
            missing.append(
                task_id + ": revision limit reached; needs explicit unresolved closeout"
            )
            continue
        actions.append(
            Candidate.make(
                operation,
                task_id,
                (f"验收通过责任「{task.objective}」的当前提交，推进依赖该结果的责任。"
                 if operation == "accept" else
                 f"退回返修责任「{task.objective}」的当前提交；沿用同一任务并增加版本，补齐核验未满足的要求。")
                + f"已有匹配当前提交的独立核验：{proof.operability}/{proof.requirement}。",
                {
                    "reason": proof.reason,
                    "evidenceRefs": list(proof.evidence_refs),
                    "operabilityVerdict": proof.operability,
                    "requirementVerdict": proof.requirement,
                    "verifiedTaskHash": proof.task_hash,
                    "execution": execution_dict(executions[task_id]),
                },
            )
        )
    for task_id in sorted(recovery_needed):
        if task_id in reclaiming or task_id in in_doubt or task_id in revising:
            continue
        task = graph.tasks[task_id]
        fact = executions.get(task_id)
        if fact is None or not fact.matches(task) or fact.status == "unknown":
            missing.append(task_id + ": runtime identity needs reconciliation")
            continue
        for pid, executor in sorted(actors.items()):
            if (
                not executor.available
                or executor.session_id in snapshot.reserved_sessions
                or (task_id, pid) not in eligible_pairs
            ):
                continue
            if fact.status == "running" and pid != task.owner_id:
                actions.append(
                    Candidate.make(
                        "request_reclaim",
                        task_id,
                        f"回收责任「{task.objective}」的当前执行权；核实停止与副作用后才改派给 {pid}。",
                        {"targetParticipantId": pid, "execution": execution_dict(fact)},
                    )
                )
            elif (
                task.state in {"blocked", "failed"}
                and task.revision < 2
                and fact.status in {"idle", "drained"}
            ):
                actions.append(
                    Candidate.make(
                        "retry",
                        task_id,
                        f"在同一责任上重试「{task.objective}」，交给 {pid}；保留旧失败和成果。",
                        {
                            "targetParticipantId": pid,
                            "reason": "Jev selected recovery of the existing responsibility",
                            "execution": execution_dict(fact),
                        },
                    )
                )
    # A chosen wait consumes this owner event. Without a real running attempt,
    # pending admission or reclaim, nothing can wake that decision again.
    # Missing evidence remains explicit and the decider can still abstain;
    # neither absence nor an accepted receipt invents a running execution.
    if frontier.running or in_doubt or reclaiming:
        actions.append(
            Candidate.make(
                "wait",
                "",
                f"等待已有进展：{len(frontier.running)} 项真实运行、{len(in_doubt)} 项未决派遣、"
                f"{len(reclaiming)} 项回收核实。等待其终态或身份核实，不重试旧工具、不取消现有任务。",
                {},
            )
        )
    return CandidateSet(tuple(actions), tuple(missing))
