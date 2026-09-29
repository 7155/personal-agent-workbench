"""Bounded glue from trusted Room-owner events to the existing Jev task kernel.

This module does NOT subscribe to events, create/adopt Roots, run a model loop,
start threads, or call Room.post_message. Composition remains the host's job.
`OwnerEvent` kinds are NEW internal adapter names, not existing HTTP/SSE enums.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from .candidates import OwnerPreference, Verification, build_candidates
from .context import ContextManifest
from .controller import JevTaskController
from .effects import RuntimeEffects
from .ledger import GraphLedger, Snapshot
from .types import ExecutionFact, Executor, GraphConflict, GraphError, canonical, digest, text

EventKind = Literal[
    "work_created", "work_submitted", "work_reviewed", "assignment_changed",
    "requirements_changed", "executor_drained", "recovery_requested",
    "reclaim_requested", "revision_requested",  # Host continuations; no new model decision.
]
DECISION_EVENTS = frozenset({
    "work_created", "work_submitted", "work_reviewed", "assignment_changed",
    "requirements_changed", "executor_drained", "recovery_requested",
})


@dataclass(frozen=True)
class OwnerEvent:
    """Construct only after an existing owner commits its durable source event.

    `source_id` must be stable across delivery/restart, not a new UUID per retry.
    Do not construct from UI messages, LLM output, token deltas, or snapshot GETs.
    """
    source_id: str
    kind: str
    graph_id: str
    controller_id: str
    room_id: str
    root_id: str

    def __post_init__(self) -> None:
        for name in ("source_id", "kind", "graph_id", "controller_id", "room_id", "root_id"):
            text(getattr(self, name), name)

    @property
    def journal_key(self) -> str:
        return "room-owner-event:" + digest([self.kind, self.source_id])


@dataclass(frozen=True)
class OwnerObservation:
    """Owner facts + an already permission-filtered, externally permitted pack.

    Executions must come from exact registry/dispatch receipts. Absence is NOT
    idle. `eligible_pairs` encodes actual scope, capability and user locks. A
    readable local document is not necessarily authorized for external Jev use.
    No host integration may set this flag merely because an API key exists.
    """
    snapshot_fingerprint: str
    executions: Mapping[str, ExecutionFact]
    executors: tuple[Executor, ...]
    eligible_pairs: frozenset[tuple[str, str]]
    manifests: Mapping[str, ContextManifest]
    decision_state: Mapping[str, object]
    decision_external_allowed: bool = False
    verifications: Mapping[str, Verification] = field(default_factory=dict)
    recovery_needed: frozenset[str] = frozenset()
    owner_preferences: Mapping[str, OwnerPreference] = field(default_factory=dict)
    eligibility_missing: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        text(self.snapshot_fingerprint, "observation fingerprint")
        if not isinstance(self.decision_external_allowed, bool):
            raise GraphError("external decision policy must be a boolean owner fact")


class JevRoomDriver:
    """One committed event -> at most one choice -> one owner command.

    New owner events advance independent work; this method does not recursively
    drive a graph until completion. Run it on an existing service work queue,
    OUTSIDE SQLite writes and EventHub publication locks. Never intercept plain
    Session sends or the Stop path. `enabled` gates NEW scheduling only; recovery
    reads remain usable after it is disabled.
    """
    def __init__(
        self, *, ledger: GraphLedger, controller: JevTaskController,
        effects: RuntimeEffects,
        observe: Callable[[Snapshot, OwnerEvent], OwnerObservation],
        enabled: bool = False, max_effects_per_call: int = 8,
    ) -> None:
        if not isinstance(enabled, bool):
            raise GraphError("enabled must be boolean")
        if (isinstance(max_effects_per_call, bool)
                or not isinstance(max_effects_per_call, int)
                or not 1 <= max_effects_per_call <= 64):
            raise GraphError("invalid effect batch bound")
        if (controller.ledger.db_path != ledger.db_path
                or effects.ledger.db_path != ledger.db_path):
            raise GraphError("driver, controller and effects must share the canonical database")
        self.ledger, self.controller, self.effects = ledger, controller, effects
        self.observe = observe
        self.enabled = enabled
        self.max_effects = max_effects_per_call

    @staticmethod
    def _check_scope(snapshot: Snapshot, event: OwnerEvent) -> None:
        if (snapshot.room_id != event.room_id or snapshot.root_id != event.root_id
                or snapshot.controller_id != event.controller_id):
            raise GraphConflict("event belongs to another Room/Root/controller")

    def handle(self, event: OwnerEvent) -> dict[str, object]:
        if not self.enabled:
            return {"status": "disabled", "effects": []}
        if event.kind not in DECISION_EVENTS:
            return {"status": "ignored_event", "effects": []}

        snapshot = self.ledger.snapshot(event.graph_id, event.controller_id)
        self._check_scope(snapshot, event)

        # Recover BEFORE rebuilding manifests or calling Jev. A committed
        # command keeps its exact effect ID; uncertain sends are lookup-only.
        previous = self.controller.event_receipt(
            event.graph_id, event.controller_id, event.journal_key)
        if previous is not None:
            application = previous.get("application")
            recovered = []
            if isinstance(application, Mapping) and isinstance(application.get("commandId"), str):
                recovered = self._recover_committed_command(
                    event.graph_id, event.controller_id, application["commandId"])
            elif previous["status"] == "reconciliation_required":
                recovered = self.reconcile(event.graph_id, event.controller_id)["effects"]
            return {"status": str(previous["status"]), "replayed": True,
                    "receipt": previous, "effects": recovered}

        with self.ledger.connection() as conn:
            try:
                self.ledger.require_unchanged(
                    conn, snapshot, allowed_root_states=("active", "review", "blocked", "failed"))
            except GraphConflict:
                return {"status": "inactive_or_changed", "effects": []}

        observation = self.observe(snapshot, event)
        if observation.snapshot_fingerprint != snapshot.fingerprint:
            return {"status": "stale_observation", "effects": []}
        candidates = build_candidates(
            snapshot, event_id=event.journal_key,
            executions=observation.executions, executors=observation.executors,
            eligible_pairs=observation.eligible_pairs, manifests=observation.manifests,
            owner_preferences=observation.owner_preferences,
            eligibility_missing=observation.eligibility_missing,
            verifications=observation.verifications,
            recovery_needed=observation.recovery_needed,
        )
        if not any(action.operation != "wait" for action in candidates.actions):
            return self._record_noop(snapshot, event, "waiting", candidates.missing)
        if not observation.decision_external_allowed:
            # Policy can be changed and explicitly wake this same source event.
            # It is a blocked prerequisite, not a completed no-op decision.
            return {"status": "decision_external_not_allowed", "effects": []}

        # The host pack is data, not a second transcript. The controller persists
        # its fingerprint, finite choice and owner receipt in the existing DB.
        state = {
            "source": {"id": event.source_id, "kind": event.kind},
            "context": json.loads(canonical(dict(observation.decision_state))),
            "missing": list(candidates.missing),
        }
        try:
            receipt = self.controller.handle_event(
                snapshot, event_id=event.journal_key, state=state,
                candidates=candidates.actions)
        except GraphConflict:
            # Snapshot may change while context is assembled, BEFORE inference.
            return {"status": "stale_observation", "effects": []}
        delivered: list[dict[str, object]] = []
        application = receipt.get("application")
        if (isinstance(application, Mapping) and isinstance(application.get("commandId"), str)):
            deliver = self._recover_committed_command if receipt.get("replayed") else self._deliver_committed_command
            delivered = deliver(
                event.graph_id, event.controller_id, str(application["commandId"]))
        return {"status": str(receipt["status"]), "receipt": receipt,
                "effects": delivered, "missing": list(candidates.missing)}

    def _record_noop(self, snapshot: Snapshot, event: OwnerEvent,
                     status: str, missing: tuple[str, ...]) -> dict[str, object]:
        """Durably record an empty frontier without spending another model call."""
        try:
            result = self.controller.record_noop(
                snapshot, event_id=event.journal_key, status=status, missing=missing)
        except GraphConflict:
            return {"status": "stale_observation", "effects": []}
        return {"status": str(result["status"]), "receipt": result, "effects": [],
                "missing": list(missing), "replayed": bool(result.get("replayed"))}

    def _recover_committed_command(self, graph_id: str, controller_id: str,
                                   command_id: str) -> list[dict[str, object]]:
        effect_ids, more = self._effect_ids(graph_id, controller_id, command_id=command_id)
        if more:
            raise GraphError("one owner command exceeds the bounded effect batch")
        with self.ledger.connection() as conn:
            policy = conn.execute(
                "SELECT stopped,recovery_policy FROM agent_jev_host_roots WHERE graph_id=?", (graph_id,)).fetchone()
        resume = bool(self.enabled and policy and not policy["stopped"]
                      and policy["recovery_policy"] == "authorized_unsent")
        result = []
        for effect_id in effect_ids:
            effect = self.effects.get(effect_id)
            if effect["state"] in {"sending", "unknown"}:
                effect = self.effects.reconcile(effect_id)
            elif effect["state"] == "pending" and resume:
                try:
                    effect = self.effects.deliver(effect_id)
                except GraphConflict:
                    effect = {**self.effects.get(effect_id), "deliveryConflict": True}
            result.append(effect)
        return result

    def _effect_ids(self, graph_id: str, controller_id: str,
                    *, command_id: str | None = None,
                    in_doubt_only: bool = False) -> tuple[list[str], bool]:
        with self.ledger.connection() as conn:
            self.ledger.read_in_transaction(conn, graph_id, controller_id)
            sql = "SELECT effect_id FROM agent_jev_runtime_effects WHERE graph_id=?"
            args: list[object] = [graph_id]
            if command_id is not None:
                sql += " AND command_id=?"
                args.append(command_id)
            if in_doubt_only:
                sql += " AND state IN ('sending','unknown')"
            sql += " ORDER BY updated_at_ms,effect_id LIMIT ?"
            args.append(self.max_effects + 1)
            rows = conn.execute(sql, args).fetchall()
        return [str(row[0]) for row in rows[:self.max_effects]], len(rows) > self.max_effects

    def _deliver_committed_command(self, graph_id: str, controller_id: str,
                                   command_id: str) -> list[dict[str, object]]:
        effect_ids, more = self._effect_ids(graph_id, controller_id, command_id=command_id)
        if more:
            raise GraphError("one owner command exceeds the bounded effect batch")
        result = []
        for effect_id in effect_ids:
            # RuntimeEffects owns pending->sending CAS and exact binding checks;
            # it NEVER resends sending/unknown work. Unrelated pending commands
            # are not opportunistically drained by another event.
            try:
                result.append(self.effects.deliver(effect_id))
            except GraphConflict:
                # A conflict can occur before admission OR while recording
                # contradictory receipts. Never label both cases "not sent".
                result.append({**self.effects.get(effect_id),
                               "deliveryConflict": True, "requiresOwnerReview": True})
        return result

    def reconcile(self, graph_id: str, controller_id: str) -> dict[str, object]:
        """Restart recovery: bounded receipt lookups ONLY, including when disabled.

        `hasMore` requires another host recovery turn; it is not an internal
        polling loop. pending effects are left untouched for explicit resumption.
        """
        effect_ids, more = self._effect_ids(graph_id, controller_id, in_doubt_only=True)
        return {"effects": [self.effects.reconcile(eid) for eid in effect_ids],
                "hasMore": more, "toolsReplayed": False}

    def resume_prepared(self, graph_id: str, controller_id: str,
                        effect_id: str) -> dict[str, object]:
        """Trusted host's explicit resume path, NOT automatic startup replay.

        The caller must have accepted a current resume intent. Runtime admission
        must still atomically recheck Root stop state, exact assignment, shared
        Session registry and permissions. Do not expose this as an unauthenticated
        generic effect endpoint. Disabling scheduling prevents new dispatch here.
        """
        if not self.enabled:
            return {"status": "disabled", "effects": []}
        with self.ledger.connection() as conn:
            self.ledger.read_in_transaction(conn, graph_id, controller_id)
            row = conn.execute(
                "SELECT graph_id FROM agent_jev_runtime_effects WHERE effect_id=?", (effect_id,)).fetchone()
            if row is None or row[0] != graph_id:
                raise GraphConflict("effect is not owned by this graph")
        return self.effects.deliver(effect_id)

    def projection(self, graph_id: str, controller_id: str,
                   executions: Mapping[str, ExecutionFact], *,
                   snapshot: Snapshot | None = None) -> dict[str, object]:
        """Read-only PROPOSED view, not an already deployed HTTP/SSE contract."""
        if snapshot is None:
            snapshot = self.ledger.snapshot(graph_id, controller_id)
        elif snapshot.graph_id != graph_id or snapshot.controller_id != controller_id:
            raise GraphConflict("projection snapshot identity mismatch")
        frontier = snapshot.graph().frontier({key: value for key, value in executions.items()
                                               if snapshot.is_active(key)})
        return {
            "schemaVersion": "jev-task-view/1", "graphId": snapshot.graph_id,
            "rootId": snapshot.root_id, "snapshotVersion": snapshot.fingerprint,
            "ready": list(frontier.ready), "running": list(frontier.running),
            "review": list(frontier.review),
            "blocked": [{"taskId": task_id, "reasons": list(reasons)}
                        for task_id, reasons in frontier.blocked],
            "tasks": [{"id": task.id, "state": task.state, "revision": task.revision,
                       "ownerId": task.owner_id, "parentId": task.parent_id}
                      for task in snapshot.active_tasks],
        }
