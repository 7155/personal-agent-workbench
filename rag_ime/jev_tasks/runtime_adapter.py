"""Exact Pi effects; descendant drain remains its own causal-owner proof."""
from __future__ import annotations

from collections.abc import Mapping
import sqlite3

from rag_ime.db import sqlite_connection
from rag_ime.pi.values import PiRuntimeError

from .types import GraphConflict, digest


def _accepted_identity(request: Mapping[str, object], receipt: Mapping[str, object]) -> tuple[str, str, str]:
    if (receipt.get("state") != "accepted"
        or any(receipt.get(key) != request.get(key) for key in ("dispatchId", "taskId", "sessionId"))):
        raise GraphConflict("operation does not match the accepted dispatch receipt")
    session_id = str(request.get("sessionId") or "")
    dispatch_id = str(request.get("dispatchId") or "")
    turn_id = str(receipt.get("turnId") or "")
    if not session_id or not dispatch_id or not turn_id:
        raise GraphConflict("operation requires a real accepted Pi turn identity")
    if request.get("turnId") and request["turnId"] != turn_id:
        raise GraphConflict("operation refers to a different Pi turn")
    return session_id, dispatch_id, turn_id


def cancel_attempt(service, request: Mapping[str, object], dispatch_receipt: Mapping[str, object], *,
                   lookup_only: bool = False) -> dict[str, object]:
    """Cancel/query one accepted attempt. Never invoke a broad Session/Root stop."""
    session_id, dispatch_id, turn_id = _accepted_identity(request, dispatch_receipt)
    reclaim_id = str(request.get("reclaimId") or "")
    if not reclaim_id:
        raise GraphConflict("exact cancellation requires reclaimId")
    abort_turn = getattr(service.runtime, "abort_turn", None)
    if not callable(abort_turn):
        return {"state": "unknown", "reason": "Runtime does not support exact attempt cancellation"}
    try:
        actual = abort_turn(session_id, turn_id, client_message_id=dispatch_id,
                            cancel_id=reclaim_id, lookup_only=lookup_only)
    except (PiRuntimeError, TimeoutError) as exc:
        return {"state": "unknown", "reason": " ".join(str(exc).split())[:240]}
    if (not isinstance(actual, Mapping)
        or actual.get("schemaVersion") != "rag-ime.pi-exact-turn-cancel.v1"
        or actual.get("sessionId") != session_id or actual.get("turnId") != turn_id
        or actual.get("clientMessageId") != dispatch_id or actual.get("cancelId") != reclaim_id
        or actual.get("state") not in {"accepted", "rejected"}
        or not isinstance(actual.get("receiptId"), str) or not actual["receiptId"]):
        return {"state": "unknown"}
    return {"state": actual["state"], "receiptId": actual["receiptId"],
            "dispatchId": dispatch_id, "taskId": request["taskId"], "sessionId": session_id,
            "reclaimId": reclaim_id, "runtimeCancellation": dict(actual)}


def execution_settlement(service, request: Mapping[str, object], dispatch_receipt: Mapping[str, object], *,
                         timeout_seconds: float = 1.0) -> dict[str, object] | None:
    """Read terminal proof for this accepted turn, independently of Session reuse."""
    session_id, dispatch_id, turn_id = _accepted_identity(request, dispatch_receipt)
    try:
        settlement = service.runtime.await_turn_settled(session_id, turn_id,
            client_message_id=dispatch_id, timeout_seconds=timeout_seconds)
    except (PiRuntimeError, TimeoutError, ValueError):
        return None
    if (not isinstance(settlement, Mapping)
        or settlement.get("schemaVersion") != "rag-ime.pi-turn-settlement.v1"
        or settlement.get("sessionId") != session_id or settlement.get("turnId") != turn_id
        or settlement.get("clientMessageId") != dispatch_id):
        return None
    receipt = settlement.get("receipt")
    runtime_id = settlement.get("runtimeSessionId")
    if (not isinstance(receipt, Mapping) or not isinstance(runtime_id, str) or not runtime_id
        or receipt.get("schemaVersion") != "pi.agent-settled.v2"
        or receipt.get("sessionId") != runtime_id or receipt.get("runId") != turn_id
        or receipt.get("scopeId") != f"{runtime_id}:{turn_id}"
        or not isinstance(receipt.get("receiptId"), str) or not receipt["receiptId"]
        or receipt.get("disposition") not in {"completed", "failed", "aborted"}
        or receipt.get("aborted") is not (receipt.get("disposition") == "aborted")
        or type(receipt.get("pendingOperations")) is not int or receipt["pendingOperations"] != 0):
        return None
    operations, continuations = receipt.get("operations"), receipt.get("continuations")
    if (not isinstance(operations, Mapping) or type(operations.get("pending")) is not int
        or operations["pending"] != 0 or not isinstance(continuations, Mapping)):
        return None
    if any(continuations.get(key) != [] for key in ("pendingIds", "readyIds", "scheduledIds", "leasedIds")):
        return None
    counts = continuations.get("counts")
    if (not isinstance(counts, Mapping)
        or any(type(counts.get(key)) is not int or counts[key] != 0 for key in ("pending", "leased"))):
        return None
    return dict(settlement)


def recover_retired_attempt(service, request: Mapping[str, object], dispatch_receipt: Mapping[str, object]) -> bool:
    """Ask the Pi owner to settle only a proven, already retired exact turn.

    This is a reconcile operation, never a read projection or a fallback abort.
    Success merely permits another settlement lookup; causal drain is separate.
    """
    session_id, dispatch_id, turn_id = _accepted_identity(request, dispatch_receipt)
    cancel_id = "jev-retired-recovery:" + dispatch_id
    recover = getattr(service.runtime, "abort_turn", None)
    if not callable(recover):
        return False
    try:
        result = recover(session_id, turn_id, client_message_id=dispatch_id,
                         cancel_id=cancel_id, recover_retired_only=True)
    except (PiRuntimeError, TimeoutError, ValueError):
        return False
    return _recovery_receipt_drained(result, session_id, turn_id, dispatch_id, cancel_id,
                                     reason="retired_turn_recovery")


def _recovery_receipt_drained(result, session_id, turn_id, dispatch_id, cancel_id, *, reason):
    if (not isinstance(result, Mapping)
        or result.get("schemaVersion") != "rag-ime.pi-exact-turn-cancel.v1"
        or result.get("sessionId") != session_id or result.get("turnId") != turn_id
        or result.get("clientMessageId") != dispatch_id or result.get("cancelId") != cancel_id
        or result.get("state") != "accepted" or result.get("phase") != "settled"
        or not str(result.get("receiptId") or "")):
        return False
    receipt = result.get("runtimeReceipt")
    if (not isinstance(receipt, Mapping)
        or receipt.get("schemaVersion") != "rag-ime.pi-session-abort-receipt.v1"
        or receipt.get("sessionId") != session_id or receipt.get("turnId") != turn_id):
        return False
    lifecycle = receipt.get("lifecycle")
    return bool(isinstance(lifecycle, Mapping)
                and lifecycle.get("schemaVersion") == "pi.agent-abort-receipt.v1"
                and lifecycle.get("reason") == reason
                and lifecycle.get("idle") is True and lifecycle.get("drained") is True
                and all(lifecycle.get(key) == [] for key in
                        ("operations", "pendingOperations", "failedOperationIds", "cancelledContinuationIds")))


def recover_interrupted_attempt(service, request: Mapping[str, object], dispatch_receipt: Mapping[str, object]) -> bool:
    """Retire only a Host-proven cold/idle exact binding, then recover its receipt.

    Caller must hold the current graph claim and have scoped turn_failed evidence.
    At most one status query per reconciliation; pending/failed never means drain.
    The manager gates the new Host capability. There is no ordinary-abort fallback.
    """
    session_id, dispatch_id, turn_id = _accepted_identity(request, dispatch_receipt)
    cancel_id = "jev-interrupted-recovery:" + dispatch_id
    retire = getattr(service.runtime, "abort_turn", None)
    if not callable(retire):
        return False
    try:
        result = retire(session_id, turn_id, client_message_id=dispatch_id,
                        cancel_id=cancel_id, recover_interrupted_only=True)
        if (isinstance(result, Mapping) and result.get("state") == "accepted"
            and result.get("phase") == "requested"
            and result.get("schemaVersion") == "rag-ime.pi-exact-turn-cancel.v1"
            and result.get("sessionId") == session_id and result.get("turnId") == turn_id
            and result.get("clientMessageId") == dispatch_id and result.get("cancelId") == cancel_id):
            result = retire(session_id, turn_id, client_message_id=dispatch_id,
                            cancel_id=cancel_id, recover_interrupted_only=True, lookup_only=True)
    except (PiRuntimeError, TimeoutError, ValueError):
        return False
    if not _recovery_receipt_drained(result, session_id, turn_id, dispatch_id, cancel_id,
                                    reason="interrupted_turn_recovery"):
        return False
    if result["runtimeReceipt"]["lifecycle"].get("cancelledOperationIds") != []:
        return False
    return recover_retired_attempt(service, request, dispatch_receipt)


def execution_drained(service, request: Mapping[str, object], dispatch_receipt: Mapping[str, object], *,
                      descendants_proof: Mapping[str, object] | None = None,
                      timeout_seconds: float = 1.0) -> dict[str, object] | None:
    """Combine independent Runtime and causal-resource proofs; neither implies the other."""
    session_id, dispatch_id, turn_id = _accepted_identity(request, dispatch_receipt)
    settlement = execution_settlement(service, request, dispatch_receipt, timeout_seconds=timeout_seconds)
    if settlement is None:
        return None
    if descendants_proof is None:
        descendants_proof = causal_descendants_proof(service, request, dispatch_receipt, settlement=settlement)
    if (not isinstance(descendants_proof, Mapping) or descendants_proof.get("settled") is not True
        or descendants_proof.get("dispatchId") != dispatch_id
        or descendants_proof.get("sessionId") != session_id
        or descendants_proof.get("turnId") != turn_id
        or descendants_proof.get("settlementReceiptId") != settlement["receipt"]["receiptId"]
        or not isinstance(descendants_proof.get("proofRef"), str) or not descendants_proof["proofRef"]):
        return None
    return {"status": "drained", "terminal": settlement["receipt"]["disposition"],
            "turnId": turn_id, "proofRef": "pi-settlement:" + settlement["receipt"]["receiptId"],
            "effectsReconciled": True, "settlement": settlement, "descendantsProof": dict(descendants_proof)}


def causal_descendants_proof(service, request: Mapping[str, object],
                            dispatch_receipt: Mapping[str, object], *,
                            settlement: Mapping[str, object] | None = None) -> dict[str, object]:
    """Inspect the existing child/job owners by dispatch lineage, without cancelling.

    Nested Tool Agents inherit their root dispatch. The recursive relation also
    covers older child rows that only retained parent_run_id. Unscoped active
    resources on the owner Session remain explicit blockers; unrelated scoped
    siblings and a reused Session's new dispatch do not affect this proof.
    """
    session_id, dispatch_id, turn_id = _accepted_identity(request, dispatch_receipt)
    resources: list[dict[str, object]] = []
    pending: list[str] = []
    if settlement is None:
        settlement = execution_settlement(service, request, dispatch_receipt)
    if (not isinstance(settlement, Mapping) or settlement.get("sessionId") != session_id
        or settlement.get("turnId") != turn_id or settlement.get("clientMessageId") != dispatch_id
        or not isinstance(settlement.get("receipt"), Mapping)
        or not settlement["receipt"].get("receiptId")):
        return {"sessionId": session_id, "dispatchId": dispatch_id, "turnId": turn_id,
                "settled": False, "resources": [], "pending": ["parent_turn_not_settled"], "proofRef": ""}
    def causal_members():
        with sqlite_connection(service.db_path, row_factory=sqlite3.Row) as conn:
            rows = conn.execute("""
                WITH RECURSIVE causal_batches(id) AS (
                    SELECT id FROM agent_subagent_batches
                    WHERE causal_dispatch_id = ? OR
                        (parent_session_id = ? AND causal_dispatch_id = '' AND state IN ('queued','running'))
                    UNION
                    SELECT b.id FROM agent_subagent_batches b
                    JOIN agent_subagent_runs r ON r.id = b.parent_run_id
                    JOIN causal_batches parent ON parent.id = r.batch_id
                )
                SELECT b.id,b.state,r.id AS run_id FROM agent_subagent_batches b
                JOIN causal_batches c ON c.id = b.id
                LEFT JOIN agent_subagent_runs r ON r.batch_id=b.id ORDER BY b.id,r.id
                """, (dispatch_id, session_id)).fetchall()
        return tuple((str(row["id"]), str(row["state"]), str(row["run_id"] or "")) for row in rows)

    try:
        membership = causal_members()
        batches = {batch_id: state for batch_id, state, _ in membership}
        child_sessions = set()
        for batch_id, batch_state in batches.items():
            batch = service.delegation.store.get_batch(batch_id, hydrate_artifacts=False)
            if batch_state not in {"completed", "failed", "aborted", "timed_out"}:
                pending.append("delegation_batch:" + batch_id)
            for run in batch["runs"]:
                run_id = str(run["id"])
                child_sessions.add(str(run["childSessionId"]))
                state = str(run["state"])
                evidence = {"kind": "delegation", "id": run_id, "state": state,
                            "updatedAtMs": run.get("updatedAtMs"), "completedAtMs": run.get("completedAtMs")}
                resources.append(evidence)
                if state not in {"completed", "failed", "aborted", "timed_out"} or not run.get("completedAtMs"):
                    pending.append("delegation:" + run_id)
                    continue
                # Forced supervision is only a logical terminal projection.
                # It cannot prove that an unresponsive child Runtime stopped.
                artifacts = getattr(service.delegation.store, "artifacts", None)
                snapshot = artifacts.snapshot(owner_kind="subagent_run", owner_id=run_id) if artifacts is not None else None
                snapshot = snapshot if isinstance(snapshot, Mapping) else {}
                supervision = snapshot.get("supervision")
                supervision = supervision if isinstance(supervision, Mapping) else {}
                checkpoint = snapshot.get("runtimeCheckpoint")
                checkpoint = checkpoint if isinstance(checkpoint, Mapping) else {}
                if supervision.get("phase") == "forced":
                    pending.append("delegation_forced_unproven:" + run_id)
                elif state != "completed" and run.get("startedAtMs") and not (
                    checkpoint.get("terminalState") in {"completed", "failed", "aborted", "timed_out"}
                    and checkpoint.get("terminalAtMs")
                ):
                    pending.append("delegation_terminal_unproven:" + run_id)
                evidence["terminalCheckpointAtMs"] = checkpoint.get("terminalAtMs")

        # The background owner has no unbounded list API. Select only causal
        # identities in SQL, then read every status from its existing owner.
        child_ids = sorted(child_sessions - {""})
        child_clause = (" OR session_id IN (" + ",".join("?" for _ in child_ids) + ")") if child_ids else ""
        def causal_jobs():
            with sqlite_connection(service.db_path, row_factory=sqlite3.Row) as conn:
                return conn.execute("""
                SELECT job_id, session_id FROM agent_background_jobs
                WHERE causal_dispatch_id = ? OR (session_id = ? AND causal_turn_id = ?)
                   OR (session_id = ? AND causal_dispatch_id = '' AND status IN ('queued','running','cancelling'))
                """ + child_clause + " ORDER BY job_id",
                (dispatch_id, session_id, turn_id, session_id, *child_ids)).fetchall()
        jobs = causal_jobs()
        for row in jobs:
            job_id = str(row["job_id"])
            job = service.background_jobs.status(str(row["session_id"]), job_id)["job"]
            state = str(job["status"])
            resources.append({"kind": "background_job", "id": job_id, "state": state,
                              "updatedAtMs": job.get("updatedAtMs"), "endedAtMs": job.get("endedAtMs")})
            # orphaned means no current owner can prove process termination.
            if state not in {"completed", "failed", "cancelled"} or not job.get("endedAtMs"):
                pending.append("background_job:" + job_id)
        # Running children can spawn descendants while owner reads are in
        # progress. Accept closure only after terminal owner proofs AND an
        # unchanged complete membership, including new runs in existing batches.
        # Changes are retried, never persisted as immutable drain evidence.
        if causal_members() != membership:
            pending.append("causal_membership_changed")
        if [(row["job_id"], row["session_id"]) for row in causal_jobs()] != [
                (row["job_id"], row["session_id"]) for row in jobs]:
            pending.append("causal_jobs_changed")
    except (KeyError, ValueError, OSError, sqlite3.Error, AttributeError, TypeError) as exc:
        pending.append("causal_owner_unavailable:" + type(exc).__name__)
    proof = {"sessionId": session_id, "dispatchId": dispatch_id, "turnId": turn_id,
             "settlementReceiptId": settlement["receipt"]["receiptId"],
             "settled": not pending, "resources": resources, "pending": pending}
    proof["proofRef"] = "causal-owners:" + digest(proof) if not pending else ""
    return proof
