"""Bounded durable event delivery on the host's existing maintenance tick.

Lease expiry permits receipt recovery, never a second send of an unknown effect.
This module owns no thread, timer, Provider call or Runtime operation.
"""
from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from uuid import uuid4

from .ledger import GraphLedger
from .room_driver import OwnerEvent
from .types import GraphConflict, GraphError, canonical, text


@dataclass(frozen=True)
class EventClaim:
    event: OwnerEvent
    owner_id: str
    generation: int
    attempt_count: int
    lease_until_ms: int


class DurableEventQueue:
    def __init__(self, ledger: GraphLedger, *, owner_id: str | None = None,
                 lease_ms: int = 30000, max_attempts: int = 6,
                 base_retry_ms: int = 1000, max_retry_ms: int = 60000):
        for name, value in (("lease_ms", lease_ms), ("max_attempts", max_attempts),
                            ("base_retry_ms", base_retry_ms), ("max_retry_ms", max_retry_ms)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise GraphError("invalid event recovery limit: " + name)
        if max_retry_ms < base_retry_ms:
            raise GraphError("maximum retry delay is smaller than base delay")
        self.ledger = ledger
        self.owner_id = text(owner_id or "jev-event-worker:" + uuid4().hex, "event worker")
        self.lease_ms, self.max_attempts = lease_ms, max_attempts
        self.base_retry_ms, self.max_retry_ms = base_retry_ms, max_retry_ms

    def enqueue(self, conn, graph_id: str, source_id: str, kind: str) -> bool:
        """Enlist in the source owner's transaction; duplicates never wake work."""
        for name, value in (("graph", graph_id), ("source", source_id), ("kind", kind)):
            text(value, name)
        prior = conn.execute(
            "SELECT kind FROM agent_jev_owner_events WHERE graph_id=? AND source_id=?",
            (graph_id, source_id)).fetchone()
        if prior is not None:
            if prior[0] != kind:
                raise GraphConflict("source event identity reused with another kind")
            return False
        now = self.ledger.clock_ms()
        conn.execute(
            "INSERT INTO agent_jev_owner_events(source_id,graph_id,kind,created_at_ms,updated_at_ms,attempt_budget) "
            "VALUES(?,?,?,?,?,?)", (source_id, graph_id, kind, now, now, self.max_attempts))
        return True

    def recover_expired(self, *, limit: int = 64) -> int:
        """Only expired ownership is recoverable; another live host keeps its lease."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1024:
            raise GraphError("invalid recovery batch bound")
        now = self.ledger.clock_ms()
        with self.ledger.connection(write=True) as conn:
            rows = conn.execute(
                "SELECT e.graph_id,e.source_id FROM agent_jev_owner_events e "
                "JOIN agent_jev_host_roots h USING(graph_id) WHERE e.state='processing' "
                "AND e.lease_until_ms<=? AND h.stopped=0 AND h.recovery_policy='authorized_unsent' "
                "ORDER BY e.lease_until_ms,e.created_at_ms LIMIT ?", (now, limit)).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE agent_jev_owner_events SET state='retry',lease_owner='',lease_until_ms=0,"
                    "next_retry_at_ms=?,result_kind='lease_expired',updated_at_ms=? "
                    "WHERE graph_id=? AND source_id=? AND state='processing' AND lease_until_ms<=?",
                    (now, now, row["graph_id"], row["source_id"], now))
        return len(rows)

    def claim_next(self, *, exclude_graph_ids: Collection[str] = ()) -> EventClaim | None:
        excluded = tuple(exclude_graph_ids)
        now = self.ledger.clock_ms()
        with self.ledger.connection(write=True) as conn:
            conn.execute(
                "UPDATE agent_jev_owner_events SET state='blocked',lease_owner='',lease_until_ms=0,"
                "result_kind='budget_exhausted',wake_condition='owner_event',updated_at_ms=? "
                "WHERE attempt_count>=attempt_budget AND "
                "((state IN ('pending','retry','reconcile') AND next_retry_at_ms<=?) "
                "OR (state='processing' AND lease_until_ms<=?))", (now, now, now))
            exclusion = " AND e.graph_id NOT IN (" + ",".join("?" for _ in excluded) + ")" if excluded else ""
            row = conn.execute(
                "SELECT e.*,g.controller_id,g.room_id,g.root_turn_id FROM agent_jev_owner_events e "
                "JOIN agent_jev_graphs g USING(graph_id) JOIN agent_jev_host_roots h USING(graph_id) "
                "WHERE h.stopped=0 AND (e.state='pending' OR h.recovery_policy='authorized_unsent') "
                "AND e.attempt_count<e.attempt_budget "
                "AND ((e.state IN ('pending','retry','reconcile') AND e.next_retry_at_ms<=?) "
                "OR (e.state='processing' AND e.lease_until_ms<=?)) "
                "AND NOT EXISTS(SELECT 1 FROM agent_jev_owner_events active "
                "WHERE active.graph_id=e.graph_id AND active.state='processing' AND active.lease_until_ms>?)"
                + exclusion + " ORDER BY h.last_event_claim_sequence,e.created_at_ms,e.source_id LIMIT 1",
                (now, now, now, *excluded)).fetchone()
            if row is None:
                return None
            generation = row["lease_generation"] + 1
            conn.execute(
                "UPDATE agent_jev_owner_events SET state='processing',lease_owner=?,lease_generation=?,"
                "lease_until_ms=?,attempt_count=attempt_count+1,updated_at_ms=? "
                "WHERE graph_id=? AND source_id=?",
                (self.owner_id, generation, now + self.lease_ms, now, row["graph_id"], row["source_id"]))
            conn.execute(
                "UPDATE agent_jev_host_roots SET last_event_claim_sequence="
                "(SELECT COALESCE(MAX(last_event_claim_sequence),0)+1 FROM agent_jev_host_roots) WHERE graph_id=?",
                (row["graph_id"],))
        event = OwnerEvent(row["source_id"], row["kind"], row["graph_id"], row["controller_id"],
                           row["room_id"], row["root_turn_id"])
        return EventClaim(event, self.owner_id, generation, row["attempt_count"] + 1, now + self.lease_ms)

    def renew(self, claim: EventClaim) -> bool:
        now = self.ledger.clock_ms()
        with self.ledger.connection(write=True) as conn:
            return conn.execute(
                "UPDATE agent_jev_owner_events SET lease_until_ms=?,updated_at_ms=? "
                "WHERE graph_id=? AND source_id=? AND state='processing' AND lease_owner=? "
                "AND lease_generation=? AND lease_until_ms>?",
                (now + self.lease_ms, now, claim.event.graph_id, claim.event.source_id,
                 claim.owner_id, claim.generation, now)).rowcount == 1

    @staticmethod
    def _outcome(result: Mapping[str, object]) -> tuple[str, str, str]:
        status = str(result.get("status") or "host_error")
        effects = result.get("effects") or []
        if status == "reconciliation_required" or any(
            isinstance(effect, Mapping) and effect.get("state") in {"sending", "unknown"} for effect in effects
        ):
            return "reconcile", "reconciliation", "exact_receipt_available"
        if any(isinstance(effect, Mapping) and effect.get("state") == "pending" for effect in effects):
            return "retry", "prepared_effect", "recovery_policy_changed"
        if status in {"decision_external_not_allowed", "disabled", "configuration_missing", "provider_not_configured"}:
            return "blocked", status, "configuration_changed"
        if status in {"no_progress_budget", "budget_exhausted"}:
            return "blocked", status, "snapshot_changed"
        if status in {"applied", "completed", "requested", "released", "waiting", "abstained", "ignored_event", "stopped", "stale_output"}:
            return "done", "committed" if status in {"applied", "completed", "requested", "released"} else "noop", ""
        return "retry", status, "owner_event"

    def finish(self, claim: EventClaim, result: Mapping[str, object]) -> bool:
        """ACK success/no-op or persist retry/blocked; an expired ACK is rejected."""
        now = self.ledger.clock_ms()
        state, kind, wake = self._outcome(result)
        with self.ledger.connection(write=True) as conn:
            row = conn.execute(
                "SELECT attempt_budget,attempt_count FROM agent_jev_owner_events WHERE graph_id=? AND source_id=? "
                "AND state='processing' AND lease_owner=? AND lease_generation=? AND lease_until_ms>?",
                (claim.event.graph_id, claim.event.source_id, claim.owner_id, claim.generation, now)).fetchone()
            if row is None:
                return False
            if state in {"retry", "reconcile"} and row["attempt_count"] >= row["attempt_budget"]:
                state, kind = "blocked", "budget_exhausted:" + kind
            delay = min(self.max_retry_ms, self.base_retry_ms * 2 ** min(row["attempt_count"] - 1, 20))
            conn.execute(
                "UPDATE agent_jev_owner_events SET state=?,result_json=?,result_kind=?,wake_condition=?,"
                "next_retry_at_ms=?,lease_owner='',lease_until_ms=0,updated_at_ms=? WHERE graph_id=? AND source_id=?",
                (state, canonical(dict(result)), kind, wake, now + delay if state in {"retry", "reconcile"} else 0,
                 now, claim.event.graph_id, claim.event.source_id))
        return True

    def wake(self, graph_id: str, *, condition: str = "configuration_changed") -> int:
        """A trusted meaningful change grants one new bounded delivery budget."""
        text(condition, "wake condition")
        with self.ledger.connection(write=True) as conn:
            return conn.execute(
                "UPDATE agent_jev_owner_events SET state='retry',attempt_budget=attempt_count+?,"
                "next_retry_at_ms=0,wake_condition='',updated_at_ms=? "
                "WHERE graph_id=? AND state='blocked' AND wake_condition=?",
                (self.max_attempts, self.ledger.clock_ms(), graph_id, condition)).rowcount
