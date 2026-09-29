"""At-most-one send attempt with explicit in-doubt reconciliation.

The injected callbacks must be the *owning* runtime's exact dispatch/cancel and
receipt lookup functions. This does not invoke shell commands, start an Agent
loop, manufacture a user message, resume a paused goal or run a polling thread.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from collections.abc import Callable, Mapping

from .ledger import GraphLedger
from .types import ExecutionFact, GraphConflict, GraphError, canonical, digest, text


def settle_unsent_cancellation(conn, graph_id: str, proof: ExecutionFact, now_ms: int) -> None:
    """Do not recycle a Session while an old cancel could still arrive."""
    rows = conn.execute(
        "SELECT e.effect_id,e.state FROM agent_jev_reclaims r JOIN agent_jev_runtime_effects e "
        "ON e.effect_id='cancel:' || r.reclaim_id WHERE r.graph_id=? AND r.task_id=? AND r.dispatch_id=?",
        (graph_id, proof.task_id, proof.dispatch_id)).fetchall()
    rows += conn.execute(
        "SELECT e.effect_id,e.state FROM agent_jev_revision_dispatches d "
        "JOIN agent_jev_task_revisions r ON r.revision_id=d.revision_id "
        "JOIN agent_jev_runtime_effects e ON e.command_id=r.command_id "
        "WHERE r.graph_id=? AND d.dispatch_id=? AND e.operation='cancel' "
        "AND json_extract(e.request_json,'$.dispatchId')=?",
        (graph_id, proof.dispatch_id, proof.dispatch_id),
    ).fetchall()
    if any(row["state"] in {"sending", "unknown"} for row in rows):
        raise GraphConflict("old cancellation is still in doubt; reconcile it before recycling the executor")
    for row in rows:
        if row["state"] == "pending":
            conn.execute("UPDATE agent_jev_runtime_effects SET state='not_sent',receipt_json=?,updated_at_ms=? WHERE effect_id=? AND state='pending'",
                         (canonical({"state": "not_sent", "source": "local_outbox",
                                     "reason": "executor drain proven before cancellation was sent", "proofRef": proof.proof_ref}),
                          now_ms, row["effect_id"]))


class RuntimeEffects:
    def __init__(self, ledger: GraphLedger,
                 perform: Callable[[str, Mapping[str, object]], Mapping[str, object]],
                 lookup: Callable[[str, Mapping[str, object]], Mapping[str, object]],
                 validate_current: Callable[[str, Mapping[str, object]], None]):
        self.ledger = ledger
        self.perform = perform
        self.lookup = lookup
        self.validate_current = validate_current

    def get(self, effect_id: str) -> dict[str, object]:
        with self.ledger.connection() as conn:
            row = conn.execute("SELECT * FROM agent_jev_runtime_effects WHERE effect_id=?", (effect_id,)).fetchone()
        if row is None:
            raise GraphError("unknown runtime effect")
        return self.from_row(row)

    @staticmethod
    def from_row(row) -> dict[str, object]:
        return {"effectId": row["effect_id"], "operation": row["operation"], "state": row["state"],
                "request": json.loads(row["request_json"]), "receipt": json.loads(row["receipt_json"])}

    def projection_records(self, conn, graph_id: str, dispatch_ids):
        """Bound display history separately from exact current task bindings."""
        effects = [self.from_row(row) for row in conn.execute(
            "SELECT * FROM agent_jev_runtime_effects WHERE graph_id=? ORDER BY updated_at_ms DESC LIMIT 200",
            (graph_id,))]
        dispatches = {e["effectId"]: e for e in effects if e["operation"] == "dispatch"}
        missing = sorted(set(dispatch_ids) - dispatches.keys() - {""})
        for start in range(0, len(missing), 500):
            batch = missing[start:start + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                "SELECT e.* FROM agent_jev_runtime_effects e JOIN agent_jev_graphs g USING(graph_id) "
                f"WHERE e.operation='dispatch' AND e.effect_id IN ({placeholders})", batch)
            dispatches.update((row["effect_id"], self.from_row(row)) for row in rows)
        return effects, dispatches

    def deliver(self, effect_id: str) -> dict[str, object]:
        # Claim before any external call. A crash after this point MUST reconcile,
        # not assume that a timeout or absent receipt means no side effect.
        with self.ledger.connection(write=True) as conn:
            row = conn.execute("SELECT * FROM agent_jev_runtime_effects WHERE effect_id=?", (effect_id,)).fetchone()
            if row is None:
                raise GraphError("unknown runtime effect")
            if row["state"] != "pending":
                return {"effectId": effect_id, "state": row["state"], "receipt": json.loads(row["receipt_json"])}
            request = json.loads(row["request_json"])
            if row["operation"] == "dispatch":
                latest = self.ledger.read_in_transaction(conn, row["graph_id"], request["controllerId"])
                self.ledger.require_unchanged(conn, latest, allowed_root_states=("active", "review", "blocked", "failed"))
                if not latest.is_active(request["taskId"]) or conn.execute(
                    "SELECT 1 FROM agent_jev_revision_targets t JOIN agent_jev_task_revisions r "
                    "ON r.revision_id=t.revision_id WHERE r.graph_id=? "
                    "AND r.status='awaiting_drain' AND t.task_id=?",
                    (row["graph_id"], request["taskId"]),
                ).fetchone() or (request.get("purpose") == "synthesize" and conn.execute(
                    "SELECT 1 FROM agent_jev_task_revisions WHERE graph_id=? AND status='awaiting_drain'",
                    (row["graph_id"],),
                ).fetchone()):
                    raise GraphConflict("prepared dispatch belongs to a pending or superseded revision")
                if latest.admission_hash(request["taskId"]) != request["admissionHash"]:
                    raise GraphConflict("dispatch preparation is stale; no runtime operation was sent")
            conn.execute("UPDATE agent_jev_runtime_effects SET state='sending',updated_at_ms=? WHERE effect_id=? AND state='pending'",
                         (self.ledger.clock_ms(), effect_id))
        try:
            # The runtime must revalidate assignment, cancellation, credentials
            # and reservation at its own admission boundary. DB checks above
            # cannot protect against a user stopping work during this call.
            self.validate_current(row["operation"], request)
            result = self.perform(row["operation"], request)
            parsed = self._validated_receipt(row["operation"], request, result)
        except Exception:
            parsed = {"state": "unknown", "reason": "runtime result is not yet proven"}
        self._record(effect_id, parsed)
        return self.get(effect_id)

    def abandon_prepared(self, effect_id: str, *, reason: str) -> dict[str, object]:
        """Withdraw only a not-yet-sent intent. Never treat `sending` as unsent.

        Releasing its WorkItem claim is a separate guarded owner operation.
        `not_sent` is local proof and is deliberately not a Runtime receipt.
        """
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise GraphError("a bounded withdrawal reason is required")
        with self.ledger.connection(write=True) as conn:
            row = conn.execute("SELECT state FROM agent_jev_runtime_effects WHERE effect_id=?", (effect_id,)).fetchone()
            if row is None:
                raise GraphError("unknown runtime effect")
            if row[0] != "not_sent":
                if row[0] != "pending":
                    raise GraphConflict("an attempted runtime effect cannot be declared unsent")
                conn.execute("UPDATE agent_jev_runtime_effects SET state='not_sent',receipt_json=?,updated_at_ms=? WHERE effect_id=? AND state='pending'",
                             (canonical({"state": "not_sent", "source": "local_outbox", "reason": reason}), self.ledger.clock_ms(), effect_id))
        return self.get(effect_id)

    def release_executor(self, graph_id: str, controller_id: str, *, command_id: str,
                         proof: ExecutionFact) -> dict[str, object]:
        """Release a finished attempt's slot without accepting its task result.

        The same executor can be reused while its old task is still in review.
        A newer attempt's slot can never be released by an old drain callback.
        """
        text(command_id, "command id")
        if proof.status != "drained" or not proof.effects_reconciled:
            raise GraphConflict("slot release needs proven drain and reconciled side effects")
        intent_hash = digest(["release_executor", graph_id, asdict(proof)])
        with self.ledger.connection(write=True) as conn:
            snapshot = self.ledger.read_in_transaction(conn, graph_id, controller_id)
            snapshot.task(proof.task_id)  # scope, not a claim that current state was accepted
            old = self.ledger.prior(conn, command_id, graph_id, intent_hash)
            if old:
                return old
            settle_unsent_cancellation(conn, graph_id, proof, self.ledger.clock_ms())
            slot = conn.execute("SELECT * FROM agent_jev_executor_claims WHERE session_id=?", (proof.session_id,)).fetchone()
            binding = [proof.task_id, proof.task_revision, proof.owner_id, proof.assignment_key, proof.accepted_turn_id]
            if slot is not None:
                if (slot["graph_id"] != graph_id or slot["task_id"] != proof.task_id
                    or slot["effect_id"] != proof.dispatch_id or json.loads(slot["binding_json"]) != binding):
                    raise GraphConflict("drain proof cannot release a different/newer executor reservation")
                effect = conn.execute("SELECT state FROM agent_jev_runtime_effects WHERE effect_id=?", (proof.dispatch_id,)).fetchone()
                if effect is None or effect[0] != "accepted":
                    raise GraphConflict("reconcile dispatch acceptance before releasing its executor")
                conn.execute("DELETE FROM agent_jev_executor_claims WHERE session_id=? AND effect_id=?",
                             (proof.session_id, proof.dispatch_id))
            result = {"commandId": command_id, "operation": "release_executor", "status": "released",
                      "taskId": proof.task_id, "dispatchId": proof.dispatch_id, "replayed": False,
                      "removedReservation": slot is not None, "taskStateUnchanged": True}
            self.ledger.save_receipt(conn, command_id, snapshot, intent_hash, "release_executor", proof.task_id, result)
        return result

    def reconcile(self, effect_id: str) -> dict[str, object]:
        current = self.get(effect_id)
        if current["state"] in {"accepted", "rejected", "pending", "not_sent"}:
            return current
        try:
            result = self.lookup(current["operation"], current["request"])
            parsed = self._validated_receipt(current["operation"], current["request"], result)
        except Exception:
            return current
        self._record(effect_id, parsed)
        return self.get(effect_id)

    def _record(self, effect_id: str, receipt: Mapping[str, object]) -> None:
        with self.ledger.connection(write=True) as conn:
            # An unknown/late lookup cannot demote an already proven outcome.
            row = conn.execute("SELECT state,receipt_json FROM agent_jev_runtime_effects WHERE effect_id=?", (effect_id,)).fetchone()
            if row is None:
                raise GraphError("unknown runtime effect")
            if row[0] in {"accepted", "rejected", "not_sent"}:
                if receipt["state"] in {"accepted", "rejected"} and row[0] != receipt["state"]:
                    raise GraphConflict("contradictory runtime receipts")
                return
            conn.execute("UPDATE agent_jev_runtime_effects SET state=?,receipt_json=?,updated_at_ms=? WHERE effect_id=?",
                         (receipt["state"], canonical(dict(receipt)), self.ledger.clock_ms(), effect_id))

    @staticmethod
    def _validated_receipt(operation: str, request: Mapping[str, object], result: object) -> dict[str, object]:
        if not isinstance(result, Mapping) or result.get("state") not in {"accepted", "rejected", "unknown"}:
            raise GraphError("invalid runtime receipt")
        if result["state"] == "unknown":
            return {"state": "unknown"}
        if (result.get("dispatchId") != request.get("dispatchId") or result.get("taskId") != request.get("taskId")
            or not isinstance(result.get("receiptId"), str) or not result["receiptId"]):
            raise GraphConflict("runtime receipt refers to a different operation")
        if result.get("sessionId") != request.get("sessionId"):
            raise GraphConflict("runtime receipt belongs to a different executor")
        if operation == "dispatch" and result["state"] == "accepted" and (
            not isinstance(result.get("turnId"), str) or not result["turnId"]
        ):
            raise GraphError("accepted dispatch has no real turn identity")
        if operation == "cancel" and result.get("reclaimId") != request.get("reclaimId"):
            raise GraphConflict("cancellation receipt refers to a different reclaim")
        # `accepted` cancellation only means cancel was accepted, never drained.
        # Do not store arbitrary transport/provider bodies or claim extra Runtime
        # guarantees (such as "drained") based on a cancellation acceptance.
        keys = ["state", "receiptId", "dispatchId", "taskId", "sessionId"]
        keys += ["turnId"] if operation == "dispatch" and result["state"] == "accepted" else []
        keys += ["reclaimId"] if operation == "cancel" else []
        return {key: result[key] for key in keys}
