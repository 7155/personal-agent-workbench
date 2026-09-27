"""A finite, recoverable event handler, not a second Agent/model loop."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict

from .decider import JevChoices
from .ledger import GraphLedger, Snapshot
from .owner import GuardedWorkOwner, execution_from
from .reclaim import Reclaims
from .types import (
    Candidate,
    DecisionUnavailable,
    GraphConflict,
    GraphError,
    canonical,
    digest,
    text,
)


class JevTaskController:
    def __init__(
        self,
        ledger: GraphLedger,
        owner: GuardedWorkOwner,
        decider: JevChoices,
        *,
        decision_lease_ms: int = 30000,
        max_decisions_per_snapshot: int = 4,
    ):
        if (
            isinstance(decision_lease_ms, bool)
            or not isinstance(decision_lease_ms, int)
            or decision_lease_ms < 1000
            or isinstance(max_decisions_per_snapshot, bool)
            or not isinstance(max_decisions_per_snapshot, int)
            or max_decisions_per_snapshot < 1
        ):
            raise GraphError("invalid decision limits")
        self.ledger, self.owner, self.decider = ledger, owner, decider
        self.reclaims = Reclaims(ledger, owner)
        self.lease_ms, self.max_decisions = (
            decision_lease_ms,
            max_decisions_per_snapshot,
        )

    def _transition(self, conn, row, phase, answer, *, selected_id="") -> bool:
        now = self.ledger.clock_ms()
        count = conn.execute(
            "UPDATE agent_jev_decisions SET phase=?,selected_id=?,answer_json=?,updated_at_ms=? "
            "WHERE graph_id=? AND event_id=? AND generation=?",
            (
                phase,
                selected_id,
                canonical(answer),
                now,
                row["graph_id"],
                row["event_id"],
                row["generation"],
            ),
        ).rowcount
        if count:
            conn.execute(
                "UPDATE agent_jev_decision_generations SET phase=?,selected_id=?,answer_json=?,updated_at_ms=? "
                "WHERE graph_id=? AND event_id=? AND generation=?",
                (
                    phase,
                    selected_id,
                    canonical(answer),
                    now,
                    row["graph_id"],
                    row["event_id"],
                    row["generation"],
                ),
            )
        return count == 1

    def _receipt(self, conn, row) -> dict[str, object] | None:
        if row is None:
            return None
        if row["phase"] == "applied":
            return {**json.loads(row["answer_json"]), "replayed": True}
        # Check every preserved generation before authorizing another decision.
        # Normally only the current generation can commit (migration 0209 fence).
        committed = conn.execute(
            "SELECT c.result_json FROM agent_jev_commands c WHERE c.graph_id=? AND "
            "(c.command_id=? OR c.command_id IN (SELECT command_id FROM agent_jev_decision_generations "
            "WHERE graph_id=? AND event_id=?)) ORDER BY c.created_at_ms LIMIT 1",
            (row["graph_id"], row["command_id"], row["graph_id"], row["event_id"]),
        ).fetchone()
        if committed:
            application = json.loads(committed[0])
            result = {
                "eventId": row["event_id"],
                "generation": row["generation"],
                "decision": json.loads(row["answer_json"]),
                "application": application,
                "status": application["status"],
            }
            self._transition(
                conn, row, "applied", result, selected_id=row["selected_id"]
            )
            return {**result, "replayed": True, "recoveredFromOwnerReceipt": True}
        return None

    def _recovery_barrier(
        self, conn, graph_id: str, event_id: str, row
    ) -> dict[str, object] | None:
        if conn.execute(
            "SELECT 1 FROM agent_jev_runtime_effects WHERE graph_id=? AND state IN ('sending','unknown') LIMIT 1",
            (graph_id,),
        ).fetchone():
            return {"eventId": event_id, "status": "reconciliation_required"}
        if (
            row is not None
            and row["phase"] == "evaluating"
            and row["lease_until_ms"] > self.ledger.clock_ms()
        ):
            return {
                "eventId": event_id,
                "status": "pending",
                "generation": row["generation"],
            }
        return None

    def event_receipt(
        self, graph_id: str, controller_id: str, event_id: str
    ) -> dict[str, object] | None:
        """Recover commits first; None permits a fresh, bounded observation.

        Failed/expired/uncommitted decisions do not masquerade as completed
        receipts. Unknown effects and live evaluations still prevent a new call.
        No Provider or Runtime operation is performed here.
        """
        text(event_id, "event id")
        with self.ledger.connection(write=True) as conn:
            self.ledger.read_in_transaction(conn, graph_id, controller_id)
            row = conn.execute(
                "SELECT * FROM agent_jev_decisions WHERE graph_id=? AND event_id=?",
                (graph_id, event_id),
            ).fetchone()
            return self._receipt(conn, row) or self._recovery_barrier(
                conn, graph_id, event_id, row
            )

    def _start(self, conn, expected, event_id, state, candidates, *, model_call=True):
        row = conn.execute(
            "SELECT * FROM agent_jev_decisions WHERE graph_id=? AND event_id=?",
            (expected.graph_id, event_id),
        ).fetchone()
        prior = self._receipt(conn, row) or self._recovery_barrier(
            conn, expected.graph_id, event_id, row
        )
        if prior is not None:
            return None, prior
        self.ledger.require_unchanged(
            conn,
            expected,
            allowed_root_states=("active", "review", "blocked", "failed"),
        )
        input_hash = digest(
            [expected.fingerprint, dict(state), [asdict(c) for c in candidates]]
        )
        if (
            row is not None
            and row["phase"] == "decided"
            and row["input_hash"] == input_hash
        ):
            # A crash after choice but before owner apply can reuse only these
            # exact frozen inputs, never the old choice against a fresh snapshot.
            return dict(row), None
        if model_call:
            total = conn.execute(
                "SELECT COALESCE(SUM(model_call),0) FROM agent_jev_decision_generations "
                "WHERE graph_id=? AND snapshot_hash=?",
                (expected.graph_id, expected.fingerprint),
            ).fetchone()[0]
            if total >= self.max_decisions:
                return None, {"status": "no_progress_budget", "eventId": event_id}
        if row is not None and row["phase"] in {"evaluating", "decided"}:
            self._transition(
                conn,
                row,
                "superseded",
                json.loads(row["answer_json"]),
                selected_id=row["selected_id"],
            )
        generation = row["generation"] + 1 if row else 1
        command_id = (
            "jev-command:" + digest([expected.graph_id, event_id, generation])[:40]
        )
        now = self.ledger.clock_ms()
        conn.execute(
            "INSERT INTO agent_jev_decisions "
            "(graph_id,event_id,input_hash,snapshot_hash,generation,lease_until_ms,phase,selected_id,answer_json,command_id,updated_at_ms) "
            "VALUES(?,?,?,?,?,?,'evaluating','','{}',?,?) ON CONFLICT(graph_id,event_id) DO UPDATE SET "
            "input_hash=excluded.input_hash,snapshot_hash=excluded.snapshot_hash,generation=excluded.generation,"
            "lease_until_ms=excluded.lease_until_ms,phase='evaluating',selected_id='',answer_json='{}',"
            "command_id=excluded.command_id,updated_at_ms=excluded.updated_at_ms",
            (
                expected.graph_id,
                event_id,
                input_hash,
                expected.fingerprint,
                generation,
                now + self.lease_ms,
                command_id,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO agent_jev_decision_generations "
            "(graph_id,event_id,generation,input_hash,snapshot_hash,observation_json,candidates_json,phase,command_id,model_call,created_at_ms,updated_at_ms) "
            "VALUES(?,?,?,?,?,?,?,'evaluating',?,?,?,?)",
            (
                expected.graph_id,
                event_id,
                generation,
                input_hash,
                expected.fingerprint,
                canonical(dict(state)),
                canonical([asdict(c) for c in candidates]),
                command_id,
                int(model_call),
                now,
                now,
            ),
        )
        return dict(
            conn.execute(
                "SELECT * FROM agent_jev_decisions WHERE graph_id=? AND event_id=?",
                (expected.graph_id, event_id),
            ).fetchone()
        ), None

    def record_noop(
        self,
        expected: Snapshot,
        *,
        event_id: str,
        status: str,
        missing: Sequence[str] = (),
    ) -> dict[str, object]:
        """Persist a no-progress observation without spending a Provider call."""
        result = {
            "eventId": event_id,
            "status": status,
            "decision": None,
            "application": {"status": status},
            "missing": list(missing),
        }
        with self.ledger.connection(write=True) as conn:
            row, previous = self._start(
                conn, expected, event_id, result, (), model_call=False
            )
            if previous is not None:
                return previous
            self._transition(conn, row, "applied", result)
        return result

    def handle_event(
        self,
        expected: Snapshot,
        *,
        event_id: str,
        state: Mapping[str, object],
        candidates: Sequence[Candidate],
    ) -> dict[str, object]:
        """One meaningful owner event, one bounded generation, one owner command."""
        text(event_id, "event id")
        by_id = {c.id: c for c in candidates}
        if len(by_id) != len(candidates):
            raise GraphError("duplicate action id")
        with self.ledger.connection(write=True) as conn:
            row, previous = self._start(conn, expected, event_id, state, candidates)
            if previous is not None:
                return previous
        command_id, generation = row["command_id"], row["generation"]
        decision = json.loads(row["answer_json"]) if row["phase"] == "decided" else None
        if decision is None:
            try:
                # These are already authority-checked business candidates.
                # Probabilities rank alternatives, not permission to execute.
                # Equivalent ready tasks must not deadlock on a 0.75 majority;
                # the explicit insufficient_evidence choice still abstains.
                action, answer = self.decider.choose_action(
                    state, candidates, min_probability=0.0, min_margin=0.0
                )
                decision = {
                    "selectedId": action.id if action else "",
                    "answer": asdict(answer) if answer else None,
                }
            except DecisionUnavailable as exc:
                status = (
                    "configuration_missing"
                    if isinstance(exc.__cause__, RuntimeError)
                    and str(exc.__cause__) == "Jev key is not configured"
                    else "decision_unavailable"
                )
                result = {
                    "status": status,
                    "eventId": event_id,
                    "generation": generation,
                }
                with self.ledger.connection(write=True) as conn:
                    self._transition(conn, row, "failed", result)
                return result
            with self.ledger.connection(write=True) as conn:
                if not self._transition(
                    conn, row, "decided", decision, selected_id=decision["selectedId"]
                ):
                    return {"status": "superseded_evaluation", "eventId": event_id}
        selected = by_id.get(decision["selectedId"])
        try:
            if selected is None or selected.operation == "wait":
                with self.ledger.connection() as conn:
                    self.ledger.require_unchanged(
                        conn,
                        expected,
                        allowed_root_states=("active", "review", "blocked", "failed"),
                    )
                applied = {
                    "status": "waiting" if selected else "abstained",
                    "eventId": event_id,
                }
            elif selected.operation == "request_reclaim":
                arguments = selected.arguments()
                if set(arguments) != {"targetParticipantId", "execution"}:
                    raise GraphError("invalid reclaim candidate")
                applied = self.reclaims.request(
                    expected,
                    command_id=command_id,
                    task_id=selected.task_id,
                    target_participant_id=arguments["targetParticipantId"],
                    execution=execution_from(arguments["execution"]),
                )
            else:
                applied = self.owner.apply(expected, selected, command_id=command_id)
        except Exception as exc:
            with self.ledger.connection(write=True) as conn:
                # An owner may fail after its transaction committed; its receipt
                # wins over the exception and prevents a second application.
                current = conn.execute(
                    "SELECT * FROM agent_jev_decisions WHERE graph_id=? AND event_id=?",
                    (expected.graph_id, event_id),
                ).fetchone()
                recovered = self._receipt(conn, current)
                if recovered is not None:
                    return recovered
                phase, status = (
                    ("superseded", "stale_decision")
                    if isinstance(exc, GraphConflict)
                    else ("failed", "owner_error")
                )
                self._transition(conn, row, phase, {"status": status})
            return {"status": status, "eventId": event_id, "generation": generation}
        result = json.loads(
            canonical(
                {
                    "eventId": event_id,
                    "generation": generation,
                    "decision": decision,
                    "application": applied,
                    "status": applied["status"],
                }
            )
        )
        with self.ledger.connection(write=True) as conn:
            if not self._transition(
                conn, row, "applied", result, selected_id=decision["selectedId"]
            ):
                return {"status": "superseded_evaluation", "eventId": event_id}
        return result
