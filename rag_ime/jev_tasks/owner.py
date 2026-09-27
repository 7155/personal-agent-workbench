"""Atomic Jev commands through the existing AgentRoomWorkStore.

No SQL here changes WorkItem owner/state/result. The two tiny work.py hooks
make an observation check and a command receipt part of the owner's transaction.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from typing import Any, Callable

from rag_ime.rooms.work_transaction import TransactionHooks, guarded_work_transaction

from .effects import settle_unsent_cancellation
from .ledger import GraphLedger, Snapshot, task_from_row
from .types import Candidate, ExecutionFact, GraphConflict, GraphError, STATES, canonical, digest, text


class _Replay(Exception):
    def __init__(self, receipt: dict[str, object]):
        self.receipt = receipt


_ALLOWED_ARGUMENTS = {
    "reassign": {"targetParticipantId", "reason", "execution"},
    "removal_transfer": {"targetParticipantId", "removedParticipantId", "reason", "execution"},
    "retry": {"targetParticipantId", "reason", "execution"},
    "submit": {"resultSummary", "artifactRefs", "evidenceRefs", "execution"},
    "accept": {"reason", "evidenceRefs", "operabilityVerdict", "requirementVerdict", "verifiedTaskHash", "execution"},
    "return": {"reason", "evidenceRefs", "operabilityVerdict", "requirementVerdict", "verifiedTaskHash", "execution"},
    "claim_dispatch": {"dispatchId", "contextManifest", "execution"},
    "fail_dispatch": {"dispatchId", "previousAcceptedTurnId", "reason", "execution"},
}


def execution_dict(fact: ExecutionFact) -> dict[str, object]:
    return asdict(fact)


def execution_from(value: object) -> ExecutionFact:
    if not isinstance(value, dict):
        raise GraphError("missing runtime-owner execution observation")
    try:
        return ExecutionFact(**value)
    except TypeError:
        raise GraphError("invalid runtime-owner execution observation") from None


class GuardedWorkOwner:
    def __init__(self, work_store: Any, ledger: GraphLedger,
                 *, artifact_preflight: Callable[[Snapshot, Candidate], None] | None = None):
        if getattr(work_store, "task_graph_guard_version", None) != 1:
            raise RuntimeError("install the WorkItem transaction hook before constructing the Jev owner")
        if work_store.db_path.resolve() != ledger.db_path.resolve():
            raise RuntimeError("Jev and WorkItem owner must use the same database")
        self.work = work_store
        self.ledger = ledger
        self.artifact_preflight = artifact_preflight

    def apply(self, expected: Snapshot, candidate: Candidate, *, command_id: str,
              reclaim_id: str = "", command_intent_hash: str | None = None,
              expected_root_epoch: int | None = None,
              on_applied: Callable[[sqlite3.Connection], None] | None = None) -> dict[str, object]:
        text(command_id, "command id")
        if candidate.operation not in _ALLOWED_ARGUMENTS:
            raise GraphError("operation is not an owned WorkItem mutation")
        arguments = candidate.arguments()
        if set(arguments) != _ALLOWED_ARGUMENTS[candidate.operation]:
            raise GraphError("unexpected or missing command arguments")
        task = expected.task(candidate.task_id)
        fact = execution_from(arguments["execution"])
        # Public commands bind idempotency to their original request, so replay
        # remains readable after this transaction changes assignment/snapshot.
        intent_hash = (text(command_intent_hash, "command intent hash")
                       if command_intent_hash is not None else
                       digest([candidate.id, candidate.operation, candidate.task_id,
                               arguments, expected.fingerprint, reclaim_id]))
        if candidate.operation in {"accept", "return"} and self.artifact_preflight is not None:
            # A committed command is immutable even if a file changes later.
            # The preflight does bounded owner-scoped I/O before any writer.
            with self.ledger.connection() as conn:
                previous = self.ledger.prior(conn, command_id, expected.graph_id, intent_hash)
            if previous is not None:
                return previous
            try:
                self.artifact_preflight(expected, candidate)
            except GraphConflict:
                # A concurrent identical review may have committed while the
                # bytes were read. Its durable receipt remains authoritative.
                with self.ledger.connection() as conn:
                    previous = self.ledger.prior(conn, command_id, expected.graph_id, intent_hash)
                if previous is not None:
                    return previous
                raise
        receipt: dict[str, object] = {}

        def before(conn: sqlite3.Connection) -> None:
            previous = self.ledger.prior(conn, command_id, expected.graph_id, intent_hash)
            if previous is not None:
                raise _Replay(previous)
            # Stop commits its host policy before WorkItem cancellation. The
            # submit observation must still hold inside this write transaction.
            if expected_root_epoch is not None:
                policy = conn.execute(
                    "SELECT stopped,epoch FROM agent_jev_host_roots WHERE graph_id=?",
                    (expected.graph_id,),
                ).fetchone()
                if policy is None or policy["stopped"] or policy["epoch"] != expected_root_epoch:
                    raise GraphConflict("execution belongs to a stopped or superseded Root")
            self.ledger.require_unchanged(conn, expected,
                allowed_root_states=tuple(STATES) if candidate.operation == "fail_dispatch"
                else ("active", "review") if candidate.operation in {"accept", "return"}
                else ("active", "blocked", "failed") if candidate.operation == "retry" and task.id == expected.root_work_id
                else ("active",))
            if candidate.operation == "removal_transfer":
                if task.state not in {"queued", "active", "blocked"}:
                    raise GraphConflict("review or terminal responsibility must settle before removal")
                removed = arguments["removedParticipantId"]
                if removed not in {task.owner_id, task.accountable_id}:
                    offered = conn.execute("SELECT offered_to_participant_id FROM agent_room_work_items WHERE id=?",
                                           (task.id,)).fetchone()
                    if offered is None or offered[0] != removed:
                        raise GraphConflict("participant does not own this open responsibility")
                pending_removal = conn.execute(
                    "SELECT targets_json FROM agent_jev_participant_removals WHERE room_id=? AND participant_id=? "
                    "AND status='pending'", (expected.room_id, removed)).fetchone()
                if pending_removal is None:
                    raise GraphConflict("no durable participant removal owns this transfer")
                if json.loads(pending_removal["targets_json"]).get(task.id) != arguments["targetParticipantId"]:
                    raise GraphConflict("removal destination changed before responsibility transfer")
                target = conn.execute("SELECT participant_status FROM agent_room_participants "
                                      "WHERE room_id=? AND id=?", (expected.room_id,
                                      arguments["targetParticipantId"])).fetchone()
                if target is None or target[0] != "active":
                    raise GraphConflict("removal destination is no longer active")
                if conn.execute("SELECT 1 FROM agent_jev_participant_removals "
                                "WHERE room_id=? AND participant_id=? AND status='pending'",
                                (expected.room_id, arguments["targetParticipantId"])).fetchone():
                    raise GraphConflict("removal destination is itself pending removal")
            if candidate.operation != "fail_dispatch":
                if not expected.is_active(task.id):
                    raise GraphConflict("task belongs to a superseded graph version")
                if conn.execute(
                    "SELECT 1 FROM agent_jev_revision_targets t JOIN agent_jev_task_revisions r "
                    "ON r.revision_id=t.revision_id WHERE r.graph_id=? "
                    "AND r.status='awaiting_drain' AND t.task_id=?",
                    (expected.graph_id, task.id),
                ).fetchone():
                    raise GraphConflict("task belongs to a pending graph revision")
            if not fact.matches(task):
                raise GraphConflict("runtime evidence belongs to another assignment/attempt")
            pending_effects = expected.pending_dispatches(task.id)
            if pending_effects and candidate.operation != "fail_dispatch":
                raise GraphConflict("dispatch admission is pending/unknown; reconcile it before another task operation")
            if candidate.operation == "claim_dispatch" and task.accepted_turn_id:
                prior_effect = conn.execute("SELECT state,request_json FROM agent_jev_runtime_effects WHERE effect_id=? AND graph_id=?",
                                            (task.accepted_turn_id, expected.graph_id)).fetchone()
                if prior_effect:
                    prior_request = json.loads(prior_effect["request_json"])
                    if (prior_effect["state"] == "accepted"
                        and prior_request["taskRevision"] == task.revision
                        and prior_request["assignmentKey"] == task.assignment_key):
                        raise GraphConflict("this task attempt already ran; collect its result or explicitly revise, do not replay")
            if candidate.operation == "fail_dispatch":
                rejected = conn.execute("SELECT state,request_json FROM agent_jev_runtime_effects WHERE effect_id=? AND graph_id=?",
                                        (arguments["dispatchId"], expected.graph_id)).fetchone()
                if rejected is None or rejected["state"] not in {"rejected", "not_sent"}:
                    raise GraphConflict("claim release requires a proven rejected or locally unsent effect")
                request = json.loads(rejected["request_json"])
                if (request["taskId"] != task.id or request["acceptedTurnId"] != task.accepted_turn_id
                    or request["previousAcceptedTurnId"] != arguments["previousAcceptedTurnId"]):
                    raise GraphConflict("claim release refers to another prepared operation")
            pending = conn.execute("SELECT * FROM agent_jev_reclaims WHERE task_id=? AND status='requested'", (task.id,)).fetchone()
            if pending is not None:
                retargeted = False
                if candidate.operation == "removal_transfer":
                    removal = conn.execute(
                        "SELECT targets_json FROM agent_jev_participant_removals "
                        "WHERE room_id=? AND participant_id=? AND status='pending'",
                        (expected.room_id, arguments["removedParticipantId"])).fetchone()
                    retargeted = bool(removal and json.loads(removal["targets_json"]).get(task.id)
                                      == arguments["targetParticipantId"])
                if (candidate.operation not in {"reassign", "retry", "removal_transfer"} or reclaim_id != pending["reclaim_id"]
                    or pending["graph_id"] != expected.graph_id
                    or json.loads(pending["binding_json"]) != list(task.binding)
                    or (arguments["targetParticipantId"] != pending["target_participant_id"] and not retargeted)
                    or fact.dispatch_id != pending["dispatch_id"] or fact.session_id != pending["runtime_session_id"]):
                    raise GraphConflict("assignment is being reclaimed; stale submission/action is fenced")
            elif reclaim_id:
                raise GraphConflict("reclaim does not match this task")
            if candidate.operation in {"reassign", "retry", "removal_transfer", "claim_dispatch", "accept", "return"}:
                if fact.status not in {"idle", "drained"}:
                    raise GraphConflict("cannot change/accept a task while its executor is live or unknown")
                if (fact.status == "drained" or task.accepted_turn_id) and not fact.effects_reconciled:
                    raise GraphConflict("execution stopped but its side effects are not reconciled")
            if candidate.operation == "submit" and fact.status not in {"running", "drained"}:
                raise GraphConflict("submission needs an exact live/finished dispatch")
            if candidate.operation == "fail_dispatch" and (fact.status != "idle" or not fact.effects_reconciled):
                raise GraphConflict("only a proven non-admission can release a failed dispatch claim")
            if candidate.operation == "accept" and task.id not in expected.graph().frontier({task.id: fact}).review:
                raise GraphConflict("acceptance still has unresolved dependency or child responsibility")
            if candidate.operation in {"accept", "return"}:
                if arguments["verifiedTaskHash"] != digest(asdict(task)):
                    raise GraphConflict("verification covers another task version/result")
            if fact.status == "drained" and fact.effects_reconciled:
                settle_unsent_cancellation(conn, expected.graph_id, fact, self.ledger.clock_ms())
            slot = conn.execute("SELECT * FROM agent_jev_executor_claims WHERE task_id=? AND graph_id=?",
                                (task.id, expected.graph_id)).fetchone()
            if slot is not None and candidate.operation in {"reassign", "retry", "removal_transfer", "accept", "return"}:
                if (fact.status != "drained" or fact.session_id != slot["session_id"]
                    or fact.dispatch_id != slot["effect_id"]
                    or json.loads(slot["binding_json"]) != list(task.binding)):
                    raise GraphConflict("executor reservation has not been proven drained for this attempt")
            if candidate.operation == "claim_dispatch":
                if conn.execute("SELECT 1 FROM agent_jev_participant_removals WHERE room_id=? "
                                "AND participant_id IN (?, ?) AND status='pending'",
                                (expected.room_id, task.owner_id, task.accountable_id)).fetchone():
                    raise GraphConflict("task responsibility is pending participant removal")
                target = conn.execute("SELECT session_id FROM agent_room_participants WHERE id=? AND room_id=?",
                                      (task.owner_id, expected.room_id)).fetchone()
                if target is None or conn.execute("SELECT 1 FROM agent_jev_executor_claims WHERE session_id=?", (target[0],)).fetchone():
                    raise GraphConflict("executor is reserved by another prepared or active operation")
                if task.id not in expected.graph().frontier({task.id: fact}).ready:
                    raise GraphConflict("task has unmet dependency or open child work")
                manifest = arguments["contextManifest"]
                if (not isinstance(manifest, dict) or manifest.get("taskId") != task.id
                    or manifest.get("taskRevision") != task.revision):
                    raise GraphError("context manifest belongs to another task version")
            if candidate.operation == "submit":
                # The worker session, not a model-supplied actor, must own this task.
                row = conn.execute("SELECT participant_status,session_id FROM agent_room_participants WHERE id=? AND room_id=?",
                                   (task.owner_id, expected.room_id)).fetchone()
                if row is None or row[0] != "active" or row[1] != fact.session_id:
                    raise GraphConflict("submission executor is no longer the active task owner")

        def after(conn: sqlite3.Connection) -> None:
            raw = conn.execute("SELECT * FROM agent_room_work_items WHERE id=?", (task.id,)).fetchone()
            if raw is None:
                raise GraphConflict("owner unexpectedly removed the task")
            current = task_from_row(dict(raw))
            if reclaim_id:
                changed = conn.execute(
                    "UPDATE agent_jev_reclaims SET status='applied',proof_json=?,updated_at_ms=? WHERE reclaim_id=? AND status='requested'",
                    (canonical(asdict(fact)), self.ledger.clock_ms(), reclaim_id))
                if changed.rowcount != 1:
                    raise GraphConflict("reclaim changed before commit")
            if candidate.operation in {"reassign", "retry", "removal_transfer", "accept", "return", "fail_dispatch"} or (
                candidate.operation == "submit" and fact.status == "drained" and fact.effects_reconciled
            ):
                conn.execute("DELETE FROM agent_jev_executor_claims WHERE graph_id=? AND task_id=? AND binding_json=?",
                             (expected.graph_id, task.id, canonical(list(task.binding))))
            receipt.update({"commandId": command_id, "operation": candidate.operation,
                            "status": "applied", "replayed": False, "task": json.loads(canonical(asdict(current)))})
            if candidate.operation == "submit":
                receipt["submissionHash"] = digest({"summary": arguments["resultSummary"],
                    "evidence": arguments["evidenceRefs"], "artifacts": arguments["artifactRefs"]})
            self.ledger.save_receipt(conn, command_id, expected, intent_hash, candidate.operation, task.id, receipt)
            if on_applied is not None:
                # The originating command may enlist its durable wakeup in this
                # same transaction; failure rolls back work, receipt, and event.
                on_applied(conn)
            if candidate.operation == "claim_dispatch":
                # A prepared effect is NOT an accepted Pi turn. Its consumer must
                # use an existing runtime primitive, never post a fake user message.
                after_snapshot = self.ledger.read_in_transaction(conn, expected.graph_id, expected.controller_id)
                target = conn.execute("SELECT session_id FROM agent_room_participants WHERE id=? AND room_id=? AND participant_status='active'",
                                      (current.owner_id, expected.room_id)).fetchone()
                if target is None:
                    raise GraphConflict("dispatch target is no longer active")
                request = {"graphId": expected.graph_id, "controllerId": expected.controller_id,
                           "admissionHash": after_snapshot.admission_hash(task.id), "roomId": expected.room_id,
                           "rootId": expected.root_id, "taskId": task.id,
                           "dispatchId": arguments["dispatchId"], "ownerId": current.owner_id, "sessionId": target[0],
                           "taskRevision": current.revision, "assignmentKey": current.assignment_key,
                           "acceptedTurnId": current.accepted_turn_id,
                           "previousAcceptedTurnId": task.accepted_turn_id,
                           "taskBrief": {"objective": current.objective, "expectedOutput": current.expected_output,
                                         "acceptanceCriteria": list(current.acceptance)},
                           "rootObjective": (conn.execute(
                               "SELECT current_objective FROM agent_jev_host_roots WHERE graph_id=?",
                               (expected.graph_id,),
                           ).fetchone()[0] or after_snapshot.task(after_snapshot.root_work_id).objective),
                           "purpose": "execute",
                           "contextManifest": {**arguments["contextManifest"], "readReceipts": [
                               {**receipt, "attemptId": arguments["dispatchId"]}
                               for receipt in arguments["contextManifest"].get("readReceipts", [])]}}

                conn.execute("INSERT INTO agent_jev_runtime_effects VALUES(?,?,?,'dispatch',?,'pending','{}',?)",
                             (arguments["dispatchId"], expected.graph_id, command_id, canonical(request), self.ledger.clock_ms()))
                conn.execute("INSERT INTO agent_jev_executor_claims VALUES(?,?,?,?,?,?)",
                             (target[0], expected.graph_id, task.id, arguments["dispatchId"],
                              canonical(list(current.binding)), self.ledger.clock_ms()))

        hooks = TransactionHooks(self.ledger.db_path, before, after)
        try:
            with guarded_work_transaction(hooks):
                op = candidate.operation
                if op == "removal_transfer":
                    self.work.transfer_for_jev_removal(
                        task.id, removed_participant_id=arguments["removedParticipantId"],
                        target_participant_id=arguments["targetParticipantId"],
                        expected_revision=task.revision, expected_owner_id=task.owner_id,
                        expected_assignment_key=task.assignment_key,
                        expected_accepted_turn_id=task.accepted_turn_id,
                        reason=arguments["reason"])
                elif op == "reassign":
                    self.work.reassign(task.id, actor_participant_id=expected.participant_id,
                                       current_owner_participant_id=arguments["targetParticipantId"], reason=arguments["reason"])
                elif op == "retry":
                    self.work.retry(task.id, actor_participant_id=expected.participant_id,
                                    current_owner_participant_id=arguments["targetParticipantId"],
                                    expected_revision=task.revision, reason=arguments["reason"])
                elif op == "submit":
                    self.work.submit(fact.session_id, {"workId": task.id,
                                     **{k: arguments[k] for k in ("resultSummary", "artifactRefs", "evidenceRefs")}})
                elif op in {"accept", "return"}:
                    method = self.work.accept if op == "accept" else self.work.return_for_revision
                    method(expected.session_id, {"workId": task.id, "expectedRevision": task.revision,
                           **{k: arguments[k] for k in ("reason", "evidenceRefs", "operabilityVerdict", "requirementVerdict")}})
                elif op == "claim_dispatch":
                    self.work.claim_dispatch(task.id, room_id=expected.room_id, owner_participant_id=task.owner_id,
                        assignment_key=task.assignment_key, previous_accepted_turn_id=task.accepted_turn_id,
                        room_turn_id=arguments["dispatchId"], root_turn_id=expected.root_id)
                else:
                    self.work.fail_dispatch(task.id, room_id=expected.room_id, actor_participant_id=task.owner_id,
                        room_turn_id=arguments["dispatchId"], previous_accepted_turn_id=arguments["previousAcceptedTurnId"],
                        reason=arguments["reason"])
        except _Replay as replay:
            return replay.receipt
        except Exception:
            # Existing terminal observers can fail *after* their DB commit.
            # Recover the durable receipt rather than running the mutation twice.
            with self.ledger.connection() as conn:
                committed = self.ledger.prior(conn, command_id, expected.graph_id, intent_hash)
            if committed:
                return {**committed, "postCommitNotice": "owner committed; its later callback did not finish"}
            raise
        if not hooks.finished or not receipt:
            raise RuntimeError("owner did not participate in the guarded transaction")
        return receipt
