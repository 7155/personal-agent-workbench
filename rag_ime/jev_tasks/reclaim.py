"""Durable reclaim intent, physical-stop proof, then same-task reassignment.

This module never interprets a wait timeout as a stop, never deletes an old
result, and never claims that invalidating an assignment undoes external writes.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict

from .ledger import GraphLedger, Snapshot
from .owner import GuardedWorkOwner, execution_dict
from .types import Candidate, ExecutionFact, GraphConflict, GraphError, canonical, digest, text


class Reclaims:
    def __init__(self, ledger: GraphLedger, owner: GuardedWorkOwner):
        self.ledger, self.owner = ledger, owner

    def request(self, expected: Snapshot, *, command_id: str, task_id: str,
                target_participant_id: str, execution: ExecutionFact,
                command_intent_hash: str | None = None, reason: str = "",
                on_applied: Callable[[sqlite3.Connection], None] | None = None) -> dict[str, object]:
        text(command_id, "command id")
        text(target_participant_id, "reclaim target")
        task = expected.task(task_id)
        if (not execution.matches(task) or execution.status != "running"
            or not execution.dispatch_id or target_participant_id == task.owner_id):
            raise GraphError("reclaim requires exact running attempt and a different target")
        text(reason, "reclaim reason", 2000, empty=True)
        intent_hash = (text(command_intent_hash, "command intent hash") if command_intent_hash is not None
                       else digest(["reclaim", expected.fingerprint, task_id, target_participant_id, asdict(execution)]
                                   + ([reason] if reason else [])))
        with self.ledger.connection(write=True) as conn:
            prior = self.ledger.prior(conn, command_id, expected.graph_id, intent_hash)
            if prior:
                return prior
            self.ledger.require_unchanged(conn, expected)
            source = conn.execute("SELECT session_id FROM agent_room_participants WHERE id=? AND room_id=?",
                                  (task.owner_id, expected.room_id)).fetchone()
            if source is None or source[0] != execution.session_id:
                raise GraphConflict("reclaim evidence refers to another executor session")
            target = conn.execute("SELECT participant_status FROM agent_room_participants WHERE id=? AND room_id=?",
                                  (target_participant_id, expected.room_id)).fetchone()
            if target is None or target[0] != "active":
                raise GraphError("target is not active in this graph")
            if conn.execute("SELECT 1 FROM agent_jev_reclaims WHERE task_id=? AND status='requested'", (task_id,)).fetchone():
                raise GraphConflict("a reclaim is already pending")
            now = self.ledger.clock_ms()
            conn.execute("INSERT INTO agent_jev_reclaims VALUES(?,?,?,?,?,?,?,'requested','{}',?,?)",
                         (command_id, expected.graph_id, task_id, canonical(list(task.binding)), execution.dispatch_id,
                          execution.session_id, target_participant_id, now, now))
            result = {"commandId": command_id, "reclaimId": command_id, "status": "requested",
                      "taskId": task_id, "dispatchId": execution.dispatch_id,
                      "mustDrainBeforeReassign": True, "replayed": False, "reason": reason}
            self.ledger.save_receipt(conn, command_id, expected, intent_hash, "reclaim", task_id, result)
            cancel = {"reclaimId": command_id, "graphId": expected.graph_id, "controllerId": expected.controller_id,
                      "rootId": expected.root_id, "taskId": task_id, "dispatchId": execution.dispatch_id,
                      "sessionId": execution.session_id, "binding": list(task.binding)}
            conn.execute("INSERT INTO agent_jev_runtime_effects VALUES(?,?,?,'cancel',?,'pending','{}',?)",
                         ("cancel:" + command_id, expected.graph_id, command_id, canonical(cancel), now))
            if on_applied is not None:
                on_applied(conn)
            return result

    def finish(self, expected: Snapshot, *, reclaim_id: str, command_id: str,
               proof: ExecutionFact, target_participant_id: str = "") -> dict[str, object]:
        with self.ledger.connection() as conn:
            row = conn.execute("SELECT * FROM agent_jev_reclaims WHERE reclaim_id=? AND graph_id=?",
                               (reclaim_id, expected.graph_id)).fetchone()
        if row is None:
            raise GraphError("unknown reclaim")
        # require_exact_matches is repeated inside the original WorkItem transaction.
        if (proof.status != "drained" or not proof.effects_reconciled
            or proof.dispatch_id != row["dispatch_id"] or proof.session_id != row["runtime_session_id"]
            or list(expected.task(row["task_id"]).binding) != json.loads(row["binding_json"])):
            raise GraphConflict("old executor has not been proven drained and reconciled")
        with self.ledger.connection() as conn:
            removal = conn.execute(
                "SELECT participant_id,targets_json FROM agent_jev_participant_removals "
                "WHERE room_id=? AND participant_id=? AND status='pending'",
                (expected.room_id, expected.task(row["task_id"]).owner_id)).fetchone()
        chosen_target = target_participant_id or row["target_participant_id"]
        managed_removal = bool(removal and json.loads(removal["targets_json"]).get(row["task_id"])
                               == chosen_target)
        if target_participant_id and not managed_removal:
            raise GraphConflict("reclaim destination change needs a durable removal retarget")
        operation = ("removal_transfer" if managed_removal else
                     "retry" if expected.task(row["task_id"]).state in {"blocked", "failed"}
                     else "reassign")
        candidate = Candidate.make(operation, row["task_id"], "已核实旧执行停止，在同一责任上改派", {
            "targetParticipantId": chosen_target,
            "reason": "Jev reclaim " + reclaim_id + "; physical stop proof: " + proof.proof_ref,
            "execution": execution_dict(proof),
            **({"removedParticipantId": removal["participant_id"]} if managed_removal else {})})
        return self.owner.apply(expected, candidate, command_id=command_id, reclaim_id=reclaim_id)
