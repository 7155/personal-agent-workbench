"""Jev host composition over canonical Room work and ordinary Pi admission.

The existing service wake tick drains committed events. Reads never schedule;
restart reconciles receipts without sending a prepared/uncertain operation.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from threading import Lock
from pathlib import Path

from .lifecycle import JevLifecycle
from .event_queue import DurableEventQueue
from rag_ime.rooms.work_transaction import TransactionHooks, guarded_work_transaction

from .materials import JevMaterialService
from .controller import JevTaskController
from .decider import JevChoices
from .effects import RuntimeEffects
from .runtime_adapter import cancel_attempt, execution_drained, execution_settlement, recover_retired_attempt, recover_interrupted_attempt
from .policy import normalize_policy, select_model, model_cards
from .revisions import JevTaskRevisions
from .ledger import GraphLedger
from .removal import JevParticipantRemoval
from .owner import GuardedWorkOwner, execution_dict
from .room_driver import JevRoomDriver, OwnerObservation
from .submission_contracts import submission_contract, validate_submission
from .types import (
    Candidate,
    DecisionUnavailable,
    Edge,
    ExecutionFact,
    Executor,
    GraphConflict,
    canonical,
    digest,
    text,
)


@dataclass(frozen=True)
class _ProjectionReads:
    """Request-local database facts; never a substitute for live Runtime identity."""
    participants: dict
    dispatches: dict
    terminals: dict
    activity: dict


class JevRoomApplication:
    def __init__(self, service, *, decider=None):
        self.service = service
        self.ledger = GraphLedger(service.db_path)
        self.owner = GuardedWorkOwner(service.room_work, self.ledger)
        self.effects = RuntimeEffects(
            self.ledger, self.perform, self.lookup, self.validate_effect_intent
        )
        self.driver = JevRoomDriver(
            ledger=self.ledger,
            controller=JevTaskController(
                self.ledger, self.owner, decider or JevChoices.from_paw()
            ),
            effects=self.effects,
            observe=self.observe,
            enabled=True,
        )
        self.materials = JevMaterialService(service)
        self.lifecycle = JevLifecycle(self)
        self.owner.artifact_preflight = self.lifecycle.require_current_verification_artifacts
        self.revisions = JevTaskRevisions(self)
        self.removal = JevParticipantRemoval(self)
        self.queue = DurableEventQueue(self.ledger, lease_ms=120000)
        self._tick_lock = Lock()

    def binding(self, room_id, graph_id, *, conn=None):
        if conn is None:
            with self.ledger.connection() as db:
                return self.binding(room_id, graph_id, conn=db)
        row = conn.execute(
            "SELECT * FROM agent_jev_graphs WHERE graph_id=? AND room_id=?",
            (graph_id, room_id),
        ).fetchone()
        if row is None:
            raise ValueError("Jev graph does not belong to this workspace")
        return dict(row)

    def create(self, room_id, payload):
        """An explicit Jev request permits its bounded task pack at the Jev provider."""
        client_id = text(payload.get("clientMessageId"), "clientMessageId")
        objective = text(payload.get("message"), "message", 8000)
        room = self.service.rooms.get(room_id)
        if (
            room["status"] != "active"
            or room.get("roomKind", "collaboration") != "collaboration"
        ):
            raise ValueError("Jev needs an active collaboration workspace")
        controller = str(
            payload.get("controllerParticipantId")
            or room.get("moderatorParticipantId")
            or ""
        )
        participants = [p for p in room["participants"] if p["status"] == "active"]
        if not controller and participants:
            controller = str(participants[0]["id"])
        actor = next((p for p in participants if p["id"] == controller), None)
        if actor is None or controller in self.removal.pending_ids(room_id):
            raise ValueError("Jev controller must be active and not pending removal")
        graph_id = "jev-graph:" + digest([room_id, client_id])[:40]
        root_id = "jev-root:" + digest([room_id, client_id])[:40]
        root_work_id = "room-work:" + digest([graph_id, "root"])[:40]
        request_hash = digest(dict(payload))
        strategy = str(payload.get("strategy") or "auto")
        if strategy not in {"auto", "direct", "plan"}:
            raise ValueError("invalid Jev strategy")
        execution_approval = payload.get("executionApproval", False)
        if not isinstance(execution_approval, bool):
            raise ValueError("executionApproval must be boolean")
        external_allowed = payload.get("externalAllowed", True)
        if not isinstance(external_allowed, bool):
            raise ValueError("externalAllowed must be boolean")
        root_policy = normalize_policy(payload)
        attachment_ids = payload.get("attachmentIds", [])
        if not isinstance(attachment_ids, list) or len(attachment_ids) > 16:
            raise ValueError("attachmentIds must be a bounded array")
        attachment_ids = list(
            dict.fromkeys(text(value, "attachmentId") for value in attachment_ids)
        )
        self.service._resolve_room_attachments(room_id, [], attachment_ids)
        previous_root = str(payload.get("previousRootId") or "")
        if previous_root:
            with self.ledger.connection() as conn:
                previous = conn.execute(
                    "SELECT h.stopped,h.final_json FROM agent_jev_graphs g JOIN agent_jev_host_roots h USING(graph_id) WHERE g.room_id=? AND g.root_turn_id=?",
                    (room_id, previous_root),
                ).fetchone()
            if previous is None or (not previous[0] and previous[1] == "{}"):
                raise GraphConflict(
                    "previousRootId must identify a finished or stopped Jev Root in this Room"
                )
        with self.ledger.connection() as conn:
            prior = conn.execute(
                "SELECT request_hash FROM agent_jev_host_roots WHERE graph_id=?",
                (graph_id,),
            ).fetchone()
        if prior and prior[0] != request_hash:
            raise GraphConflict("clientMessageId reused with different Jev request")
        if not prior:
            self.service._restore_room_participant_sessions(room)
        # Root, controller binding and first event become durable together.
        with self.ledger.connection(write=True) as conn:
            if conn.execute("SELECT 1 FROM agent_jev_participant_removals WHERE room_id=? "
                            "AND participant_id=? AND status='pending'", (room_id, controller)).fetchone():
                raise GraphConflict("Jev controller is pending Room removal")
            prior = conn.execute(
                "SELECT request_hash FROM agent_jev_host_roots WHERE graph_id=?",
                (graph_id,),
            ).fetchone()
            if prior and prior[0] != request_hash:
                raise GraphConflict("clientMessageId reused with different Jev request")
            if not prior:
                self.service.room_work.create_root_in_transaction(
                    conn,
                    work_id=root_work_id,
                    room_id=room_id,
                    objective=objective,
                    prepared_only=True,
                    expected_output=str(
                        payload.get("expectedOutput")
                        or "直接回应用户要求；问候与闲聊自然简短。实际执行任务时提供成果、相关证据和真实未解决项，不添加空的流程报告。"
                    ),
                    current_owner_participant_id=controller,
                    created_by_participant_id=controller,
                    client_message_id=client_id,
                    acceptance_criteria=payload.get("acceptanceCriteria")
                    or ["满足本次用户要求，并如实说明验证边界"],
                    created_at_ms=self.ledger.clock_ms(),
                    root_turn_id=root_id,
                    topic_id=str(room.get("activeTopicId") or ""),
                )
                conn.execute(
                    "INSERT INTO agent_jev_graphs VALUES(?,?,?,?,?,?,?,'jev',0,?)",
                    (
                        graph_id,
                        room_id,
                        root_id,
                        root_work_id,
                        graph_id,
                        controller,
                        actor["sessionId"],
                        self.ledger.clock_ms(),
                    ),
                )
                conn.execute(
                    "INSERT INTO agent_jev_host_roots(graph_id,request_hash,phase,external_allowed,previous_root_id) VALUES(?,?,?,?,?)",
                    (
                        graph_id,
                        request_hash,
                        "plan" if execution_approval and strategy != "auto" else {"auto": "route", "direct": "execute", "plan": "plan"}[
                            strategy
                        ],
                        int(external_allowed),
                        str(payload.get("previousRootId") or ""),
                    ),
                )
                conn.execute(
                    "UPDATE agent_jev_host_roots SET attachment_ids_json=?,policy_json=? WHERE graph_id=?",
                    (canonical(attachment_ids), canonical(root_policy), graph_id),
                )
                if execution_approval:
                    conn.execute("INSERT INTO agent_jev_plan_approvals(graph_id,status,updated_at_ms) VALUES(?,'planning',?)",
                        (graph_id, self.ledger.clock_ms()))
                self._enqueue(conn, graph_id, client_id, "work_created")
        self.publish_input(self.binding(room_id, graph_id), client_id=client_id)
        self.service.wake_scheduler.wake()
        return {
            "ok": True,
            "accepted": True,
            "mode": "jev",
            "graphId": graph_id,
            "roomId": room_id,
            "rootId": root_id,
            "workItemId": root_work_id,
            "idempotentReplay": bool(prior),
        }

    def _enqueue(self, conn, graph_id, source_id, kind):
        return self.queue.enqueue(conn, graph_id, source_id, kind)

    def projection(self, room_id, graph_id=""):
        # Collect one coherent, bounded DB view, then release it before taking
        # Room registry / Runtime locks. GET never requests settlement or recovery.
        with self.ledger.connection() as conn:
            self.service.rooms.get(room_id, conn=conn)
            if not graph_id:
                bindings = [dict(r) for r in conn.execute(
                    "SELECT g.*,h.stopped,h.phase,h.final_json,w.objective,w.client_message_id AS clientMessageId FROM agent_jev_graphs g JOIN agent_jev_host_roots h USING(graph_id) JOIN agent_room_work_items w ON w.id=g.root_work_id WHERE g.room_id=? ORDER BY g.created_at_ms DESC LIMIT 100",
                    (room_id,))]
                return {"ok": True, "mode": "jev", "items": bindings, "modelCards": model_cards(),
                        "participantRemovals": self.removal.projection(room_id, conn=conn)}
            binding = self.binding(room_id, graph_id, conn=conn)
            snapshot = self.ledger.read_in_transaction(conn, graph_id, binding["controller_id"])
            effects, dispatches = self.effects.projection_records(
                conn, graph_id, [task.accepted_turn_id for task in snapshot.tasks])
            reads = self._projection_execution_reads(conn, snapshot, dispatches, effects)
            events = [dict(r) for r in conn.execute(
                "SELECT source_id,kind,state,result_json FROM agent_jev_owner_events WHERE graph_id=? ORDER BY created_at_ms DESC,rowid DESC LIMIT 100",
                (graph_id,))]
            policy = self.lifecycle.policy(graph_id, conn)
            root_attachments = self._input_attachments(binding, policy, conn=conn)
            approval = self.lifecycle.plan_approval(graph_id, conn)
            revisions = self.revisions.projection(conn, graph_id)
            participant_removals = self.removal.projection(room_id, conn=conn)
            current_objective = self.revisions.current_objective(conn, snapshot)
            superseded_by = {row[0]: row[1] for row in conn.execute(
                "SELECT old_task_id,new_task_id FROM agent_jev_task_supersessions WHERE graph_id=?",
                (graph_id,))}
        executions = self.executions(snapshot, reads=reads)
        view = self.driver.projection(
            graph_id, binding["controller_id"], executions, snapshot=snapshot)
        for effect in effects:
            effect["executionStatus"] = self.execution_status(effect, reads=reads)
        return {
            "ok": True,
            "mode": "jev",
            **view,
            "stopped": bool(policy["stopped"]),
            "phase": policy["phase"],
            "requirementsRevision": policy["requirements_revision"],
            "currentRootObjective": current_objective,
            "roomId": room_id,
            "rootAttachmentReceipts": root_attachments,
            "activeTaskIds": list(snapshot.active_task_ids),
            "revisions": revisions,
            "participantRemovals": participant_removals,
            "final": json.loads(policy["final_json"]),
            "policy": normalize_policy(
                json.loads(policy["policy_json"]), legacy=policy["policy_json"] == "{}", stored=True
            ),
            "modelCards": model_cards(),
            "tasks": [{**asdict(task), "taskHash": digest(asdict(task)),
                       "activeVersion": snapshot.is_active(task.id),
                       "supersededByTaskId": superseded_by.get(task.id, "")}
                      for task in snapshot.tasks],
            "reclaims": [{
                "reclaimId": reclaim["reclaim_id"], "taskId": reclaim["task_id"],
                "taskRevision": json.loads(reclaim["binding_json"])[1],
                "dispatchId": reclaim["dispatch_id"],
                "targetParticipantId": reclaim["target_participant_id"],
                "stage": "awaiting_assignment" if executions[reclaim["task_id"]].status == "drained"
                    and executions[reclaim["task_id"]].effects_reconciled else "awaiting_stop",
            } for reclaim in json.loads(snapshot.reclaims_json)],
            "edges": [asdict(e) for e in snapshot.graph().edges],
            "effects": effects,
            "events": events,
            **({"planApproval": approval} if approval is not None else {}),
        }

    def _projection_execution_reads(self, conn, snapshot, dispatches, effects):
        participants = self.service.rooms.participants_by_id(
            [task.owner_id for task in snapshot.tasks], conn=conn)
        terminals = {}
        ids = tuple(dispatches)
        for start in range(0, len(ids), 500):
            batch = ids[start:start + 500]
            placeholders = ",".join("?" for _ in batch)
            for row in conn.execute(
                f"SELECT dispatch_id,proof_json FROM agent_jev_execution_drains WHERE dispatch_id IN ({placeholders})", batch):
                terminals[row[0]] = self._terminal_record(json.loads(row[1]))
        # Only the displayed effects need event-derived badges. Current task
        # frontier continues to use exact live identity and owner drain proofs.
        pairs = tuple(dict.fromkeys(
            (e["request"]["sessionId"], e["receipt"]["turnId"])
            for e in effects if e["operation"] == "dispatch" and e["state"] == "accepted"
            and e["effectId"] not in terminals and e["receipt"].get("turnId")))
        activity = {}
        for start in range(0, len(pairs), 250):
            batch = pairs[start:start + 250]
            values = ",".join("(?,?)" for _ in batch)
            rows = conn.execute(
                f"WITH requested(session_id,turn_id) AS (VALUES {values}) "
                "SELECT e.session_id,e.turn_id,MAX(e.event_type IN ('turn_completed','turn_failed')) "
                "FROM requested r JOIN agent_runtime_events e ON e.session_id=r.session_id AND e.turn_id=r.turn_id "
                "WHERE e.event_type IN ('turn_started','turn_completed','turn_failed',"
                "'tool_started','tool_progress','tool_finished','provider_request_completed',"
                "'provider_request_failed','text_delta','reasoning_summary','message_completed') "
                "GROUP BY e.session_id,e.turn_id", tuple(value for pair in batch for value in pair))
            activity.update(((row[0], row[1]), row[2]) for row in rows)
        return _ProjectionReads(participants, dispatches, terminals, activity)

    def execution_status(self, effect, *, reads=None):
        """Read-only evidence for UI; acceptance is distinct from running/drain."""
        if effect["operation"] != "dispatch":
            return effect["state"]
        if effect["state"] != "accepted":
            return {
                "pending": "prepared",
                "sending": "unknown",
                "not_sent": "rejected",
            }.get(effect["state"], effect["state"])
        if self.execution_terminal(effect, lookup=False, reads=reads):
            return "drained"
        request = effect["request"]
        turn_id = effect["receipt"].get("turnId")
        if not turn_id:
            return "unknown"  # Unscoped Session events cannot identify this attempt.
        if reads is not None:
            observed = (reads.activity.get((request["sessionId"], turn_id)),)
        else:
            with self.ledger.connection() as conn:
                observed = conn.execute(
                    """SELECT MAX(event_type IN ('turn_completed','turn_failed'))
                       FROM agent_runtime_events WHERE session_id=? AND turn_id=?
                       AND event_type IN ('turn_started','turn_completed','turn_failed',
                           'tool_started','tool_progress','tool_finished',
                           'provider_request_completed','provider_request_failed',
                           'text_delta','reasoning_summary','message_completed')""",
                    (request["sessionId"], turn_id),
                ).fetchone()
        # Native Pi publishes activity, not turn_started. Historical activity
        # survives Host restarts and Session reuse, so the Runtime must also
        # still own this exact live turn. Neither absence nor a terminal event
        # proves physical drain without the owner receipt above.
        if observed[0] is not None:
            if observed[0]:
                return "unknown"
            active = getattr(self.service.runtime, "is_turn_active", None)
            return "running" if callable(active) and active(
                request["sessionId"], turn_id, client_message_id=effect["effectId"]
            ) is True else "unknown"
        return "admitted"

    def command(self, room_id, payload):
        action = str(payload.get("action") or "create")
        if action == "create":
            return self.create(room_id, payload)
        graph_id = text(payload.get("graphId"), "graphId")
        binding = self.binding(room_id, graph_id)
        if action == "revision_options":
            return self.revisions.options(binding, text(payload.get("taskId"), "taskId"))
        client_id = text(payload.get("clientMessageId"), "clientMessageId")
        intent_hash = digest(["manual-" + action + ".v1", room_id, payload])
        # A committed command is readable even after its task or Root changes.
        # Check every action here so reusing its key for another action conflicts.
        with self.ledger.connection() as conn:
            prior = self.ledger.prior(conn, client_id, graph_id, intent_hash)
        if prior is not None:
            return {**prior, "idempotentReplay": True} if action == "revise_task" else prior
        if action == "assignment_options":
            return self.assignment_options(binding, text(payload.get("taskId"), "taskId"))
        if action == "stop":
            return self.stop(room_id, binding["root_turn_id"])
        if action == "reconcile":
            return self.reconcile_graph(binding)
        if action == "retry_route":
            with self.ledger.connection(write=True) as conn:
                policy = self.lifecycle.policy(graph_id, conn)
                latest = conn.execute(
                    "SELECT state,result_json FROM agent_jev_owner_events WHERE graph_id=? "
                    "ORDER BY created_at_ms DESC,rowid DESC LIMIT 1", (graph_id,),
                ).fetchone()
                if (policy["stopped"] or policy["final_json"] != "{}" or policy["phase"] != "route"
                        or latest is None or latest["state"] != "done"
                        or json.loads(latest["result_json"]).get("status") != "abstained"):
                    raise GraphConflict("route retry requires the current abstained Root")
                queued = self._enqueue(conn, graph_id, "route-retry:" + graph_id, "work_created")
            if queued:
                self.service.wake_scheduler.wake()
            return {"ok": True, "accepted": True, "graphId": graph_id, "queued": queued}
        if action in {"approve_plan", "adjust_plan", "defer_plan"}:
            return self.lifecycle.control_plan(binding, payload)
        if action == "revise_task":
            with self.service.room_turns.lock:
                return self.revisions.request(binding, payload,
                    command_intent_hash=intent_hash)
        if action in {"reassign", "request_reclaim"}:
            # Serialize with Root Stop. Replay is a read of a committed receipt,
            # including after Stop, never a new assignment under stale authority.
            with self.service.room_turns.lock:
                with self.ledger.connection() as conn:
                    prior = self.ledger.prior(conn, client_id, graph_id, intent_hash)
                if prior is not None:
                    return prior
                self.require_active(graph_id)
                snapshot = self.ledger.snapshot(graph_id, binding["controller_id"])
                task = snapshot.task(text(payload.get("taskId"), "taskId"))
                with self.ledger.connection() as conn:
                    self.revisions.require_current(conn, snapshot, task.id)
                if payload.get("taskHash") != digest(asdict(task)):
                    raise GraphConflict("reassignment must cover the current task and assignment")
                target = text(payload.get("targetParticipantId"), "targetParticipantId")
                specification = self.lifecycle.specifications(graph_id).get(task.id, {})
                if target not in {p["id"] for p in self.eligible_participants(
                        snapshot, specification, include_busy=True)}:
                    raise GraphConflict("reassignment target does not satisfy the task capability/scope or owner lock")
                if action == "request_reclaim":
                    result = self.driver.controller.reclaims.request(
                        snapshot, command_id=client_id, task_id=task.id, target_participant_id=target,
                        execution=self.executions(snapshot)[task.id], command_intent_hash=intent_hash,
                        reason=text(payload.get("reason"), "reason", 2000),
                        on_applied=lambda conn: self._enqueue(conn, graph_id, client_id, "reclaim_requested"))
                    self.service.wake_scheduler.wake()
                    return result
                candidate = Candidate.make("reassign", task.id, "明确的人工改派", {
                    "targetParticipantId": target,
                    "reason": text(payload.get("reason"), "reason", 2000),
                    "execution": execution_dict(self.executions(snapshot)[task.id]),
                })
                result = self.owner.apply(snapshot, candidate, command_id=client_id,
                    command_intent_hash=intent_hash,
                    on_applied=lambda conn: self._enqueue(
                        conn, graph_id, "reassign:" + client_id, "requirements_changed"))
                self.service.wake_scheduler.wake()
                return result
        if action == "resume":
            self.require_active(graph_id)
            result = self.driver.resume_prepared(
                graph_id,
                binding["controller_id"],
                text(payload.get("effectId"), "effectId"),
            )
            self.cleanup_nonadmission(graph_id)
            return result
        if action == "advance":
            self.require_active(graph_id)
            with self.ledger.connection(write=True) as conn:
                self._enqueue(
                    conn, graph_id, "resume:" + client_id, "recovery_requested"
                )
            self.service.wake_scheduler.wake()
            return {"ok": True, "status": "queued"}
        if action in {"edges", "accept", "return"}:
            with self.service.room_turns.lock:
                # Pin replay, policy and task state to one read snapshot. An
                # identical command can commit outside the Room lock during
                # file preflight; its new task must not outrun its receipt.
                with self.ledger.connection() as conn:
                    prior = self.ledger.prior(conn, client_id, graph_id, intent_hash)
                    if prior is not None:
                        return prior
                    policy = self.lifecycle.policy(graph_id, conn)
                    if policy["stopped"]:
                        raise GraphConflict("Jev root stopped")
                    root_epoch = policy["epoch"]
                    snapshot = self.ledger.read_in_transaction(conn, graph_id, binding["controller_id"])
                if action == "edges":
                    with self.ledger.connection() as conn:
                        if self.revisions.pending(conn, graph_id) is not None:
                            raise GraphConflict("task graph revision is awaiting drain")

                    def edges(key):
                        raw = payload.get(key, [])
                        if not isinstance(raw, list) or len(raw) > 100:
                            raise ValueError("edges must be a bounded array")
                        return [Edge(**item) for item in raw]

                    result = self.ledger.change_edges(
                        snapshot, command_id=client_id, add=edges("add"), remove=edges("remove"),
                        command_intent_hash=intent_hash,
                        on_applied=lambda conn: self._enqueue(conn, graph_id, client_id, "requirements_changed")
                    )
                    self.service.wake_scheduler.wake()
                    return result
                if action in {"accept", "return"}:
                    task = snapshot.task(text(payload.get("taskId"), "taskId"))
                    if payload.get("taskHash") != digest(asdict(task)):
                        raise GraphConflict(
                            "review must cover the current task result and revision"
                        )
                    fact = self.executions(snapshot)[task.id]
                    candidate = Candidate.make(
                        action,
                        task.id,
                        "明确的验收结论",
                        {
                            "reason": text(payload.get("reason"), "reason", 2000),
                            "evidenceRefs": payload.get("evidenceRefs"),
                            "operabilityVerdict": payload.get("operabilityVerdict"),
                            "requirementVerdict": payload.get("requirementVerdict"),
                            "verifiedTaskHash": payload["taskHash"],
                            "execution": execution_dict(fact),
                        },
                    )
            # Bounded artifact reads run outside the Room turn lock. The owner
            # transaction still fences this snapshot and the Root epoch.
            return self.owner.apply(snapshot, candidate, command_id=client_id,
                                    command_intent_hash=intent_hash,
                                    expected_root_epoch=root_epoch)
        raise ValueError("unsupported Jev action")

    def require_active(self, graph_id):
        with self.ledger.connection() as conn:
            row = conn.execute(
                "SELECT stopped FROM agent_jev_host_roots WHERE graph_id=?", (graph_id,)
            ).fetchone()
        if row is None or row[0]:
            raise GraphConflict("Jev root stopped")

    def executions(self, snapshot, *, reads=None):
        result = {}
        for task in snapshot.tasks:
            actor = reads.participants[task.owner_id] if reads is not None else self.service.rooms.participant(task.owner_id)
            status, proof, reconciled = "unknown", "", False
            dispatch = task.accepted_turn_id
            binding = task.binding
            if not dispatch:
                status, proof, reconciled = "idle", "work:" + digest(binding), True
            else:
                effect = reads.dispatches.get(dispatch) if reads is not None else self.lifecycle.effect_for_dispatch(dispatch)
                if effect:
                    request = effect["request"]
                    original = (
                        request["taskId"],
                        request["taskRevision"],
                        request["ownerId"],
                        request["assignmentKey"],
                        request["acceptedTurnId"],
                    )
                    if effect["state"] == "accepted":
                        terminal = self.execution_terminal(effect, lookup=False, reads=reads)
                        if terminal:
                            if original == binding:
                                status, proof, reconciled = (
                                    "drained",
                                    terminal["eventId"],
                                    True,
                                )
                            elif (
                                task.revision > request["taskRevision"]
                                and task.owner_id == request["ownerId"]
                                and task.assignment_key == request["assignmentKey"]
                            ):
                                # The owner's recorded return created a new revision;
                                # this is idle-with-prior-drain, not a current execution.
                                status, proof, reconciled = (
                                    "idle",
                                    "revision-after:" + terminal["eventId"],
                                    True,
                                )
                        elif (
                            original == binding
                            and self.service.room_turns.active_turn(actor["sessionId"])
                            == (snapshot.root_id, dispatch)
                        ):
                            # Restored Room dispatch maps outlive the Host that
                            # executed them. Keep the claim unresolved until the
                            # Runtime confirms this exact turn or a drain receipt.
                            active = getattr(self.service.runtime, "is_turn_active", None)
                            if callable(active) and active(
                                request["sessionId"], str(effect["receipt"].get("turnId") or ""),
                                client_message_id=dispatch,
                            ) is True:
                                status, proof = "running", "dispatch:" + dispatch
                    elif (
                        effect["state"] in {"not_sent", "rejected"}
                        and original == binding
                    ):
                        status, proof, reconciled = (
                            "idle",
                            "nonadmission:" + dispatch,
                            True,
                        )
            result[task.id] = ExecutionFact(
                task.id,
                status,
                dispatch_id=dispatch,
                session_id=actor["sessionId"],
                task_revision=task.revision,
                owner_id=task.owner_id,
                assignment_key=task.assignment_key,
                accepted_turn_id=task.accepted_turn_id,
                proof_ref=proof,
                effects_reconciled=reconciled,
            )
        return result

    def manifest(
        self, snapshot, task, *, selections=None, executor_id=None, purpose="execute"
    ):
        specification = self.lifecycle.specifications(snapshot.graph_id).get(
            task.id, {}
        )
        # Planner references never authorize external disclosure. The task
        # contract is already authorized by the persisted Root policy.
        references = [
            ({**ref, "externalAllowed": False} if isinstance(ref, dict) else ref)
            for ref in specification.get("contextRefs", [])
        ]
        if executor_id:
            task = replace(task, owner_id=executor_id)
        manifest = self.materials.manifest(
            snapshot,
            task,
            context_refs=references,
            decision_external_allowed=bool(
                self.lifecycle.policy(snapshot.graph_id)["external_allowed"]
            ),
            selections=selections,
            write_targets=specification.get("writeTargets", []),
        )
        root_policy = self.lifecycle.policy(snapshot.graph_id)
        policy = normalize_policy(
            json.loads(root_policy["policy_json"]),
            legacy=root_policy["policy_json"] == "{}",
            stored=True,
        )
        scope = json.loads(manifest.execution_scope_json)
        if policy["modelRouting"] == "balanced":
            actor = self.service.rooms.participant(task.owner_id)
            session = self.service.sessions.get(actor["sessionId"])
            provider, _ = self.service.runtime.config.resolved_model_reference(session)
            scope["modelSelection"] = select_model(
                self.service.runtime.available_models(),
                purpose=purpose,
                difficulty=specification.get("difficulty", "routine"),
                preferred_provider=provider,
            )
        # Managed attachments remain under their original Room/media owner;
        # native image input is not misrepresented as an extracted text read.
        scope["attachmentIds"] = json.loads(root_policy["attachment_ids_json"])
        scope["toolApprovalMode"] = policy["toolApprovalMode"]
        return replace(
            manifest,
            execution_scope_json=canonical(scope),
            digest=digest([manifest.digest, scope]),
        )

    def observe(self, snapshot, event):
        from rag_ime.agent_definitions import canonical_collaboration_role_id

        from .candidates import OwnerPreference

        self.require_active(snapshot.graph_id)
        policy = self.lifecycle.policy(snapshot.graph_id)
        approval = self.lifecycle.plan_approval(snapshot.graph_id)
        approval_fact = {
            "status": approval["status"] if approval else "not_required",
            "planHash": approval["planHash"] if approval else "",
            "requirementsRevision": approval["requirementsRevision"] if approval else policy["requirements_revision"],
            "currentRequirementsRevision": policy["requirements_revision"],
            "executionAuthorized": approval is None or (
                approval["status"] == "approved"
                and approval["requirementsRevision"] == policy["requirements_revision"]
            ),
        }
        specifications = self.lifecycle.specifications(snapshot.graph_id)
        room = self.service.rooms.get(snapshot.room_id)
        actors = []
        for participant in room["participants"]:
            if participant["status"] != "active":
                continue
            available = self.service._room_target_idle(participant["sessionId"])
            actors.append(
                Executor(
                    participant["id"],
                    participant["sessionId"],
                    available,
                    description=str(participant.get("collaborationRole") or ""),
                )
            )
        manifests = {}
        pairs = set()
        skill_catalogs = {}
        eligibility_missing = {}
        for task in snapshot.active_tasks:
            if task.id == snapshot.root_work_id and len(snapshot.active_tasks) > 1:
                continue  # Aggregation is a synthesize purpose, never worker execution.
            if self.revisions.is_target_for(snapshot.graph_id, task.id):
                continue
            if task.state == "active":
                manifests[task.id] = self.manifest(snapshot, task)
                if manifests[task.id].missing:
                    continue
            specification = specifications.get(task.id, {})
            if self.write_conflict(snapshot, task.id, specification):
                continue
            skill_unavailability = {}
            eligible = self.eligible_participants(
                snapshot, specification,
                skill_catalogs=skill_catalogs,
                skill_unavailability=skill_unavailability,
            )
            if not eligible and skill_unavailability:
                eligibility_missing[task.id] = "; ".join(
                    sorted(set(skill_unavailability.values()))
                )
            pairs.update(
                (task.id, p["id"])
                for p in eligible
            )
        with self.ledger.connection() as conn:
            occupied = conn.execute(
                "SELECT COUNT(*) FROM agent_jev_executor_claims WHERE graph_id=?",
                (snapshot.graph_id,),
            ).fetchone()[0]
        if occupied >= policy["max_parallel"]:
            pairs.clear()
        participants_by_id = {
            participant["id"]: participant
            for participant in room["participants"]
            if participant["status"] == "active"
        }
        owner_preferences = {}
        for task in snapshot.active_tasks:
            specification = specifications.get(task.id, {})
            if (task.state != "active" or not specification.get("writeTargets")
                    or specification.get("ownerParticipantId")
                    or (task.id, task.owner_id) not in pairs):
                continue
            current = participants_by_id.get(task.owner_id)
            if current is None or canonical_collaboration_role_id(
                    current.get("collaborationRole")) == "implementer":
                continue
            preferred = sorted(
                (
                    participant for participant in participants_by_id.values()
                    if participant["id"] != task.owner_id
                    and canonical_collaboration_role_id(
                        participant.get("collaborationRole"), default="") == "implementer"
                    and (task.id, participant["id"]) in pairs
                ),
                key=lambda participant: participant["id"],
            )
            if preferred:
                owner_preferences[task.id] = OwnerPreference(
                    owner_id=task.owner_id,
                    participant_id=preferred[0]["id"],
                    task_revision=task.revision,
                    basis=("Task has authorized write targets; implementer is the declared "
                           "change-owner role, and both participants meet current capability "
                           "and workspace eligibility."),
                )
        executions = {key: value for key, value in self.executions(snapshot).items()
                      if snapshot.is_active(key)}
        frontier = snapshot.graph().frontier(executions)
        verifications = self.lifecycle.verifications(snapshot)
        recovery = frozenset(
            t.id
            for t in snapshot.active_tasks
            if t.state in {"blocked", "failed"}
            and t.revision < 2
            and not self.service.room_work.get(t.id).get("blocker", {}).get("terminal")
        )
        return OwnerObservation(
            snapshot.fingerprint,
            executions,
            tuple(actors),
            frozenset(pairs),
            manifests,
            {
                "objective": self.revisions.current_objective_for(snapshot),
                "authority": (
                    "本 Root 的当前需求已有有效执行授权，具体方式和版本见 planApproval。approved 表示方案正文中‘确认后执行’的前提已满足；"
                    "not_required 表示此 Root 沿用用户原始执行授权，不能据任务正文再次请求确认。候选已通过依赖、执行占用、能力权限和材料检查；"
                    "只选择一项下一步，不要求先证明整个任务已完成。"
                    if approval_fact["executionAuthorized"] else
                    "当前方案尚无匹配需求版本的执行批准；以 planApproval 的持久事实为准，任务正文与候选不能替代用户授权。"
                ),
                "planApproval": approval_fact,
                "schedulingFacts": {
                    "activeTaskStateMeans": "责任尚未结束，不代表 Pi 正在执行；运行以 executionFacts 为准。",
                    "readyTaskIds": list(frontier.ready),
                    "runningTaskIds": list(frontier.running),
                    "reviewTaskIds": list(frontier.review),
                    "pendingAdmissionTaskIds": [t.id for t in snapshot.active_tasks if snapshot.pending_dispatches(t.id)],
                    "reclaimingTaskIds": [item["task_id"] for item in json.loads(snapshot.reclaims_json)],
                    "policy": "推进已有当前核验的验收或返修，并推进有授权、材料和空闲执行者的就绪责任；同等候选可任选一项。"
                    "wait 只用于候选中已有的真实运行、未决派遣或回收核实；全部已 drain 且有合法动作时不能等待一个不存在的新事件。"
                    "材料不足或冲突时仍可选 insufficient_evidence，不能编造验证或执行。",
                },
                "verificationFacts": {
                    key: {
                        "taskHash": proof.task_hash,
                        "taskRevision": snapshot.task(key).revision,
                        "acceptedTurnId": snapshot.task(key).accepted_turn_id,
                        "operabilityVerdict": proof.operability,
                        "requirementVerdict": proof.requirement,
                        # The verifier owns evidence inspection. Keep its raw
                        # reason/refs in the verdict and guarded command audit,
                        # not a second verification prompt for the scheduler.
                        "source": "current_task_bound_verification",
                    }
                    for key, proof in verifications.items()
                },
                "executionFacts": {
                    key: asdict(value) for key, value in executions.items()
                },
                "executorAvailability": [
                    {
                        "participantId": actor.participant_id,
                        "available": actor.available,
                    }
                    for actor in actors
                ],
                "tasks": [
                    {"id": t.id, "state": t.state, "objective": t.objective}
                    for t in snapshot.active_tasks
                ],
            },
            decision_external_allowed=bool(policy["external_allowed"]),
            verifications=verifications,
            recovery_needed=recovery,
            owner_preferences=owner_preferences,
            eligibility_missing=eligibility_missing,
        )

    def write_conflict(self, snapshot, task_id, specification):
        targets = [Path(p).resolve() for p in specification.get("writeTargets", [])]
        if not targets:
            return False
        with self.ledger.connection() as conn:
            occupied = conn.execute(
                "SELECT s.specification_json FROM agent_jev_executor_claims c JOIN agent_jev_task_requirements s ON s.task_id=c.task_id WHERE c.task_id<>?",
                (task_id,),
            ).fetchall()
        for row in occupied:
            other = [
                Path(p).resolve() for p in json.loads(row[0]).get("writeTargets", [])
            ]
            if any(
                a == b or a.is_relative_to(b) or b.is_relative_to(a)
                for a in targets
                for b in other
            ):
                return True
        return False

    def validate_current(self, operation, request):
        approval = self.lifecycle.plan_approval(request["graphId"])
        if operation == "dispatch" and approval is not None and approval["status"] != "approved" and request.get("purpose", "execute") != "plan":
            raise GraphConflict("the complete plan has not been approved for execution")
        self.require_active(request["graphId"])
        snapshot = self.ledger.snapshot(request["graphId"], request["controllerId"])
        task = snapshot.task(request["taskId"])
        if operation == "dispatch":
            with self.ledger.connection() as conn:
                self.revisions.require_current(
                    conn, snapshot, task.id,
                    purpose=request.get("purpose", "execute"))
        if request.get("purpose", "execute") != "execute":
            with self.ledger.connection() as conn:
                self.lifecycle._live(conn, request)
        if operation == "dispatch":
            if snapshot.admission_hash(task.id) != request["admissionHash"]:
                raise GraphConflict("prepared dispatch changed")
            prepared = request["contextManifest"]
            selections = {
                item["id"]: item.get("selection", item["mode"])
                for item in prepared.get("materials", [])
            }
            fresh = self.manifest(
                snapshot,
                task,
                selections=selections,
                executor_id=request["ownerId"],
                purpose=request.get("purpose", "execute"),
            ).for_executor(attempt_id=request["dispatchId"])
            if fresh != prepared:
                raise GraphConflict(
                    "execution materials or scope changed since preparation"
                )
        specification = self.lifecycle.specifications(snapshot.graph_id).get(
            task.id, {}
        )
        if request.get("purpose", "execute") == "execute":
            if request["ownerId"] not in {
                p["id"]
                for p in self.eligible_participants(
                    snapshot, specification, include_busy=True
                )
            }:
                raise GraphConflict(
                    "executor no longer satisfies the task capability/scope"
                )
            if self.write_conflict(snapshot, task.id, specification):
                raise GraphConflict("another execution owns the write target")
        actor = self.service.rooms.participant(request["ownerId"])
        if request["ownerId"] in self.removal.pending_ids(request["roomId"]):
            raise GraphConflict("executor is pending removal from this Room")
        if actor["status"] != "active" or actor["sessionId"] != request["sessionId"]:
            raise GraphConflict("executor permission/binding changed")
        self.service._guard_room_session_route(
            "room.message.execute", request["sessionId"]
        )

    def validate_effect_intent(self, operation, request):
        # This check only validates immutable intent shape. Mutable authority is
        # rechecked under admission/cancellation's exact Runtime fence, where a
        # rejection can be proven not sent and its resources cleaned up.
        if operation not in {"dispatch", "cancel"}:
            raise ValueError("unsupported Jev runtime operation")
        for key in ("graphId", "controllerId", "taskId", "dispatchId", "sessionId"):
            text(request.get(key), key)

    def perform(self, operation, request):
        if operation == "cancel":
            original = self.effects.get(request["dispatchId"])
            return cancel_attempt(self.service, request, original["receipt"])
        result = self.service.room_dispatch.dispatch_prepared(
            request,
            validate=lambda: self.validate_current(operation, request),
            configure=lambda: self.configure_execution(request),
            reserve=lambda: self.service.runtime.reserve_prompt_admission(
                request["sessionId"], client_message_id=request["dispatchId"]
            ),
            release=lambda: self.service.runtime.release_prompt_admission(
                request["sessionId"], client_message_id=request["dispatchId"]
            ),
            require_active=lambda: self.service.runtime.require_prompt_admission_active(
                request["sessionId"], client_message_id=request["dispatchId"]
            ),
        )
        if result.get("notSent"):
            return self.receipt(request, "rejected")
        if result.get("accepted"):
            return {
                **self.receipt(request, "accepted", turn_id=result["sessionTurnId"]),
                **(
                    {"projectionSync": result["projectionSync"]}
                    if result.get("projectionSync")
                    else {}
                ),
            }
        # dispatch_target also catches transport exceptions. Absence of success
        # is NOT proof of rejection; use the exact durable command receipt.
        return self.lookup(operation, request)

    def configure_execution(self, request):
        scope = request["contextManifest"].get("executionScope", {})
        selected = scope.get("modelSelection")
        if selected:
            actual = self.service.runtime.set_model(
                request["sessionId"],
                provider=selected["provider"],
                model_id=selected["modelId"],
            )
            chosen = actual.get("selected", {})
            if (
                chosen.get("provider") != selected["provider"]
                or chosen.get("id") != selected["modelId"]
            ):
                raise GraphConflict("Pi did not apply the exact prepared model")
            thinking = self.service.runtime.set_thinking_level(
                request["sessionId"], level=selected["thinkingLevel"]
            )
            if thinking.get("thinkingLevel") != selected["thinkingLevel"]:
                raise GraphConflict("Pi did not apply the prepared reasoning effort")
        self.service._resolve_room_attachments(
            request["roomId"], [request["sessionId"]], scope.get("attachmentIds", [])
        )

    @staticmethod
    def receipt(request, state, *, turn_id=""):
        return {
            "state": state,
            "receiptId": "session-command:" + request["dispatchId"],
            "dispatchId": request["dispatchId"],
            "taskId": request["taskId"],
            "sessionId": request["sessionId"],
            "turnId": turn_id,
        }

    def lookup(self, operation, request):
        if operation == "cancel":
            original = self.effects.get(request["dispatchId"])
            return cancel_attempt(
                self.service, request, original["receipt"], lookup_only=True
            )
        evidence = self.service.command_receipts.acceptance_evidence_for_exact_command(
            command_scope="session_prompt",
            scope_id=request["sessionId"],
            client_message_id=request["dispatchId"],
        )
        evidence = evidence or self.service.sessions.prompt_acceptance_evidence(
            request["sessionId"], request["dispatchId"]
        )
        if evidence and evidence.get("turnId"):
            receipt = self.receipt(request, "accepted", turn_id=evidence["turnId"])
            return self.service.room_dispatch.repair_prepared_projection(
                request,
                receipt,
                validate=lambda: self.validate_accepted_binding(request),
                runtime=self.service.runtime,
            )
        failure = self.service.command_receipts.failure_evidence_for_exact_command(
            command_scope="session_prompt",
            scope_id=request["sessionId"],
            client_message_id=request["dispatchId"],
        )
        if failure and failure.get("causeCode") in {
            "PI_COMMAND_REJECTED",
            "PI_TURN_CONFLICT",
            "ROOM_PARTICIPANT_BUSY",
            "AGENT_TURN_CONFLICT",
        }:
            return self.receipt(request, "rejected")
        return {"state": "unknown"}

    def validate_accepted_binding(self, request):
        with self.ledger.connection() as conn:
            self.lifecycle._live(
                conn, request, subject=request.get("purpose") != "plan"
            )

    def cleanup_nonadmission(self, graph_id):
        binding = self.binding_by_graph(graph_id)
        with self.ledger.connection() as conn:
            ids = [
                r[0]
                for r in conn.execute(
                    "SELECT effect_id FROM agent_jev_runtime_effects WHERE graph_id=? AND operation='dispatch' AND state IN ('not_sent','rejected')",
                    (graph_id,),
                )
            ]
        for effect_id in ids:
            effect = self.effects.get(effect_id)
            request = effect["request"]
            if request.get("purpose", "execute") != "execute":
                with self.ledger.connection(write=True) as conn:
                    conn.execute(
                        "DELETE FROM agent_jev_executor_claims WHERE effect_id=? AND session_id=?",
                        (effect_id, request["sessionId"]),
                    )
                continue
            snapshot = self.ledger.snapshot(graph_id, binding["controller_id"])
            task = snapshot.task(request["taskId"])
            if task.accepted_turn_id != effect_id:
                continue
            fact = self.executions(snapshot)[task.id]
            candidate = Candidate.make(
                "fail_dispatch",
                task.id,
                "释放确认未受理的派遣",
                {
                    "dispatchId": effect_id,
                    "previousAcceptedTurnId": request["previousAcceptedTurnId"],
                    "reason": "Jev dispatch was not admitted",
                    "execution": execution_dict(fact),
                },
            )
            self.owner.apply(snapshot, candidate, command_id="release:" + effect_id)

    def binding_by_graph(self, graph_id):
        with self.ledger.connection() as conn:
            return dict(
                conn.execute(
                    "SELECT * FROM agent_jev_graphs WHERE graph_id=?", (graph_id,)
                ).fetchone()
            )

    def stop(self, room_id, root_id):
        with self.ledger.connection() as conn:
            row = conn.execute(
                "SELECT * FROM agent_jev_graphs WHERE room_id=? AND root_turn_id=?",
                (room_id, root_id),
            ).fetchone()
        if row is None:
            return None
        binding = dict(row)
        # Same registry fence as begin/reserve; no model call on Stop.
        with self.service.room_turns.lock:
            with self.ledger.connection(write=True) as conn:
                conn.execute(
                    "UPDATE agent_jev_host_roots SET stopped=1 WHERE graph_id=?",
                    (binding["graph_id"],),
                )
                conn.execute(
                    "UPDATE agent_jev_task_revisions SET status='cancelled' "
                    "WHERE graph_id=? AND status='awaiting_drain'",
                    (binding["graph_id"],),
                )
            self.service.room_turns.record_cancellation(root_id, "jev-stop:" + root_id)
        self.service.room_work.cancel_root(
            room_id=room_id,
            root_turn_id=root_id,
            actor_participant_id=binding["controller_participant_id"],
        )
        with self.ledger.connection() as conn:
            pending = [
                r[0]
                for r in conn.execute(
                    "SELECT effect_id FROM agent_jev_runtime_effects WHERE graph_id=? AND state='pending'",
                    (binding["graph_id"],),
                )
            ]
        for effect_id in pending:
            self.effects.abandon_prepared(
                effect_id, reason="Root explicitly stopped before dispatch"
            )
        self.cleanup_nonadmission(binding["graph_id"])
        return self.service.room_cancellation.abort_turn(room_id, room_turn_id=root_id)

    def reconcile_graph(self, binding):
        graph_id, controller = binding["graph_id"], binding["controller_id"]
        result = self.driver.reconcile(graph_id, controller)
        self.cleanup_nonadmission(graph_id)
        self.lifecycle.reconcile_auxiliary(binding)
        with self.ledger.connection() as conn:
            effect_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT effect_id FROM agent_jev_runtime_effects WHERE graph_id=? AND operation='dispatch' AND state='accepted' AND COALESCE(json_extract(request_json,'$.purpose'),'execute')='execute'",
                    (graph_id,),
                )
            ]
        for effect_id in effect_ids:
            effect = self.effects.get(effect_id)
            request = effect["request"]
            terminal = self.execution_terminal(effect)
            if terminal is None and self.recover_retired_execution(effect):
                # Recovery acceptance is not drain. Re-read Pi's actual
                # settlement and the existing causal descendant proof.
                terminal = self.execution_terminal(effect)
            if terminal is None:
                continue
            proof = ExecutionFact(
                request["taskId"],
                "drained",
                dispatch_id=effect_id,
                session_id=request["sessionId"],
                task_revision=request["taskRevision"],
                owner_id=request["ownerId"],
                assignment_key=request["assignmentKey"],
                accepted_turn_id=request["acceptedTurnId"],
                proof_ref=terminal["eventId"],
                effects_reconciled=True,
            )
            with self.ledger.connection() as conn:
                reclaim = conn.execute(
                    "SELECT reclaim_id,target_participant_id FROM agent_jev_reclaims WHERE graph_id=? AND dispatch_id=? AND status='requested'",
                    (graph_id, effect_id),
                ).fetchone()
            if reclaim and not self.lifecycle.policy(graph_id)["stopped"]:
                current = self.ledger.snapshot(graph_id, controller)
                specification = self.lifecycle.specifications(graph_id).get(request["taskId"], {})
                # Cancellation may take time. Recheck the target's present
                # capabilities/scope before changing responsibility. A drained
                # old executor can still release its slot while this waits.
                target_id = self.removal.reclaim_target(current, {
                    "task_id": request["taskId"], "target_participant_id": reclaim["target_participant_id"]})
                if target_id and target_id in {p["id"] for p in self.eligible_participants(
                        current, specification, include_busy=True)}:
                    self.driver.controller.reclaims.finish(
                        current,
                        reclaim_id=reclaim["reclaim_id"],
                        command_id="finish:" + reclaim["reclaim_id"],
                        proof=proof,
                        **({"target_participant_id": target_id}
                           if target_id != reclaim["target_participant_id"] else {}),
                    )
            released = self.effects.release_executor(
                graph_id, controller, command_id="drain:" + effect_id, proof=proof
            )
            try:
                record = self.service.room_partner_dispatches.get(effect_id)
            except KeyError:
                record = None
            if record and record["status"] in {"prepared", "dispatched"}:
                phase = (
                    "failed"
                    if terminal["eventType"] == "turn_failed"
                    else "aborted"
                    if terminal["status"] == "aborted"
                    else "completed"
                )
                revision_retired = (self.revisions.is_target_for(graph_id, request["taskId"])
                                    or not self.ledger.snapshot(graph_id, controller).is_active(request["taskId"]))
                if reclaim or revision_retired:
                    # Settle only the old dispatch record. Its late result or
                    # abort must not mutate a reclaimed or superseded responsibility.
                    self.service.room_partner_dispatches.settle(
                        record["childDispatchId"], status=phase,
                        result=str(record.get("result") or ""), completion_source="session_terminal")
                else:
                    self.settle_dispatch(
                        record, phase=phase, result=str(record.get("result") or ""),
                        completion_source="session_terminal")
            # A recovered durable settlement need not replay live events. Drop
            # only its exact Room projection, preserving a newer Session turn.
            self.service.room_turns.finish_exact_dispatch(
                request["sessionId"], effect["receipt"]["turnId"], request["rootId"], effect_id,
            )
            with self.ledger.connection(write=True) as conn:
                self._enqueue(conn, graph_id, terminal["eventId"], "executor_drained")
            if released.get("removedReservation"):
                self.wake_resources(request["sessionId"], source_id=effect_id)
        self.revisions.advance_pending(graph_id)
        current = self.ledger.snapshot(graph_id, controller)
        if self.lifecycle.policy(graph_id)["final_json"] != "{}":
            self.lifecycle.publish_final(current)
        return result

    @contextmanager
    def participant_steer_context(self, room_id, root_id, participant_id):
        """Confirm JEV control identity without depending on retained UI anchors."""
        with self.ledger.connection() as conn:
            graph = conn.execute("SELECT * FROM agent_jev_graphs WHERE root_turn_id=?", (root_id,)).fetchone()
        if graph is None:
            yield None  # Keep legacy Room control/receipt validation unchanged.
            return
        if graph["room_id"] != room_id:
            raise GraphConflict("JEV Root does not belong to this Room")
        # Keep Room Stop/new dispatch outside confirmation + original steer send.
        with self.service.room_turns.lock:
            with self.ledger.connection() as conn:
                policy = self.lifecycle.policy(graph["graph_id"], conn)
                rows = conn.execute(
                    "SELECT c.*,e.request_json,e.receipt_json,e.state AS effect_state "
                    "FROM agent_jev_executor_claims c JOIN agent_jev_runtime_effects e ON e.effect_id=c.effect_id "
                    "WHERE c.graph_id=?", (graph["graph_id"],)).fetchall()
            if (policy["stopped"] or policy["final_json"] != "{}"
                    or self.service.rooms.get(room_id)["status"] != "active"):
                raise GraphConflict("JEV Root is stopped, finished, or its Room is inactive")
            candidates = [(row, json.loads(row["request_json"])) for row in rows]
            if participant_id:
                candidates = [(row, request) for row, request in candidates if request.get("ownerId") == participant_id]
            if len(candidates) != 1:
                raise GraphConflict("steer requires one explicitly selected current JEV claim")
            row, request = candidates[0]
            receipt = json.loads(row["receipt_json"])
            participant = self.service.rooms.participant(request["ownerId"])
            session_id, dispatch_id = row["session_id"], row["effect_id"]
            turn_id = str(receipt.get("turnId") or "")
            purpose = request.get("purpose", "execute")
            claim_binding = ([request.get(field) for field in
                ("taskId", "taskRevision", "ownerId", "assignmentKey", "acceptedTurnId")]
                if purpose == "execute" else {"purpose": purpose,
                    "subjectHash": request.get("subjectHash"), "dispatchId": dispatch_id})
            if (row["effect_state"] != "accepted" or receipt.get("state") != "accepted" or not turn_id
                    or participant["roomId"] != room_id or participant["status"] != "active"
                    or participant["id"] in self.removal.pending_ids(room_id)
                    or participant["sessionId"] != session_id or request.get("sessionId") != session_id
                    or request.get("rootId") != root_id or request.get("roomId") != room_id
                    or request.get("graphId") != graph["graph_id"] or request.get("taskId") != row["task_id"]
                    or request.get("dispatchId") != dispatch_id or row["binding_json"] != canonical(claim_binding)
                    or policy["epoch"] != request.get("rootEpoch", policy["epoch"])
                    or self.service.room_turns.is_cancelled(session_id, root_id)
                    or self.service.room_turns.active_turn(session_id) != (root_id, dispatch_id)
                    or self.service.room_turns.turn_by_session_turn.get((session_id, turn_id)) != root_id
                    or self.service.room_turns.dispatch_by_session_turn.get((session_id, turn_id)) != dispatch_id
                    or self.service.runtime.is_turn_active(session_id, turn_id, client_message_id=dispatch_id) is not True):
                raise GraphConflict("steer target no longer has its exact active JEV turn")
            yield {"participantId": participant["id"], "sessionId": session_id,
                   "turnId": turn_id, "dispatchId": dispatch_id}

    def cancel_workspace_job(self, session_id, job_id, *, causal, reason=None):
        """Consume existing dispatch authority; never grant general Room cancellation."""
        effect = self.lifecycle.effect_for_dispatch(str(causal.get("dispatchId") or ""))
        if effect is None:
            return None  # Legacy/non-JEV callers retain the ordinary job boundary.
        request, receipt = effect["request"], effect["receipt"]
        turn_id = str(causal.get("turnId") or "")
        with self.service.room_turns.lock:
            live = self.service._active_room_dispatch_context(session_id)
            key = (session_id, turn_id)
            if (not causal.get("roomBound") or not live or not turn_id
                    or effect["state"] != "accepted"
                    or request.get("sessionId") != session_id
                    or receipt.get("turnId") != turn_id
                    or any(causal.get(field) != live.get(field)
                           for field in ("roomId", "rootId", "dispatchId", "generation"))
                    or any(causal.get(field) != request.get(field)
                           for field in ("roomId", "rootId", "dispatchId"))
                    or self.service.room_turns.turn_by_session_turn.get(key) != causal["rootId"]
                    or self.service.room_turns.dispatch_by_session_turn.get(key) != effect["effectId"]):
                raise GraphConflict("workspace job cancellation requires its exact active JEV dispatch")
            purpose = request.get("purpose", "execute")
            # Submission may revise the task while its admitted dispatch is still
            # cleaning up. Its retained claim, not the revised task, owns the job.
            claim_binding = ([request[field] for field in
                ("taskId", "taskRevision", "ownerId", "assignmentKey", "acceptedTurnId")]
                if purpose == "execute" else {"purpose": purpose,
                    "subjectHash": request["subjectHash"], "dispatchId": effect["effectId"]})
            with self.ledger.connection() as conn:
                policy = self.lifecycle.policy(request["graphId"], conn)
                claimed = conn.execute(
                    "SELECT 1 FROM agent_jev_executor_claims WHERE graph_id=? AND task_id=? "
                    "AND session_id=? AND effect_id=? AND binding_json=?",
                    (request["graphId"], request["taskId"], session_id, effect["effectId"],
                     canonical(claim_binding)),
                ).fetchone()
                if not claimed or policy["stopped"] or policy["epoch"] != request.get("rootEpoch", policy["epoch"]):
                    raise GraphConflict("workspace job cancellation no longer owns the JEV claim")
            if self.service.runtime.is_turn_active(session_id, turn_id,
                    client_message_id=effect["effectId"]) is not True:
                raise GraphConflict("workspace job cancellation requires its exact live Runtime turn")
            return self.service.background_jobs.cancel_room_dispatch_owned(
                session_id, job_id, context=causal, reason=reason)

    def recover_retired_execution(self, effect):
        """Reconcile a current claim through exact Host-fenced recovery only."""
        request = effect["request"]
        active = getattr(self.service.runtime, "is_turn_active", None)
        if callable(active) and active(request["sessionId"],
                str(effect["receipt"].get("turnId") or ""),
                client_message_id=effect["effectId"]) is True:
            return False
        with self.ledger.connection() as conn:
            try:
                snapshot, task = self.lifecycle._live(conn, request)
            except GraphConflict:
                return False
            purpose = request.get("purpose", "execute")
            claim_binding = (list(task.binding) if purpose == "execute" else {
                "purpose": purpose, "subjectHash": request["subjectHash"], "dispatchId": effect["effectId"]})
            claimed = conn.execute(
                "SELECT 1 FROM agent_jev_executor_claims WHERE graph_id=? AND task_id=? "
                "AND session_id=? AND effect_id=? AND binding_json=?",
                (snapshot.graph_id, task.id, request["sessionId"], effect["effectId"],
                 canonical(claim_binding)),
            ).fetchone()
            failed = conn.execute(
                "SELECT 1 FROM agent_runtime_events WHERE session_id=? AND turn_id=? AND event_type='turn_failed' LIMIT 1",
                (request["sessionId"], str(effect["receipt"].get("turnId") or "")),
            ).fetchone()
        if not claimed or execution_settlement(self.service, request, effect["receipt"]) is not None:
            return False
        if recover_retired_attempt(self.service, request, effect["receipt"]):
            return True
        return bool(failed) and recover_interrupted_attempt(self.service, request, effect["receipt"])

    def recover(self):
        self.queue.recover_expired()
        with self.ledger.connection() as conn:
            bindings = [
                dict(row) for row in conn.execute("SELECT * FROM agent_jev_graphs")
            ]
        for binding in bindings:
            try:
                self.publish_input(binding)
                self.reconcile_graph(binding)
            except Exception:
                # One corrupt/unavailable graph cannot starve independent work.
                with self.ledger.connection(write=True) as conn:
                    self._enqueue(
                        conn,
                        binding["graph_id"],
                        "startup:" + str(self.ledger.clock_ms()),
                        "recovery_requested",
                    )
        self.removal.advance_all()

    def tick(self, *, limit=8):
        if not self._tick_lock.acquire(blocking=False):
            return 0
        count = 0
        try:
            self.queue.recover_expired()
            with self.ledger.connection() as conn:
                bindings = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM agent_jev_graphs WHERE graph_id IN (SELECT graph_id FROM agent_jev_runtime_effects WHERE state IN ('sending','unknown','accepted'))"
                    )
                ]
            for binding in bindings:
                try:
                    self.reconcile_graph(binding)
                except Exception:
                    continue  # The source event retains its own retry/error receipt.
            for _ in range(limit):
                claim = self.queue.claim_next()
                if claim is None:
                    break
                event = claim.event
                try:
                    self.publish_input(self.binding_by_graph(event.graph_id))
                    result = (self.resume_reclaim(event) if event.kind == "reclaim_requested"
                              else self.revisions.advance_pending(event.graph_id)
                              if event.kind == "revision_requested" else self.lifecycle.advance(event))
                    if result is None:
                        result = self.driver.handle(event)
                    self.cleanup_nonadmission(event.graph_id)
                except DecisionUnavailable:
                    result = {"status": "decision_unavailable"}
                except Exception as exc:
                    result = {"status": "host_error", "errorType": type(exc).__name__}
                committed = self.queue.finish(claim, result)
                if committed:
                    self.service.room_events.publish_projection(
                        projection_key="jev-event:"
                        + event.graph_id
                        + ":"
                        + event.source_id
                        + ":"
                        + str(claim.generation),
                        room_id=event.room_id,
                        event_type="participant_status",
                        turn_id=event.root_id,
                        payload={
                            "status": "jev_updated",
                            "graphId": event.graph_id,
                            "sourceId": event.source_id,
                            "decisionStatus": result["status"],
                        },
                    )
                count += 1
            self.removal.advance_all()
        finally:
            self._tick_lock.release()
        return count

    def resume_reclaim(self, event):
        """Deliver one durably requested cancellation on the existing owner queue."""
        self.require_active(event.graph_id)
        with self.ledger.connection() as conn:
            reclaim = conn.execute(
                "SELECT 1 FROM agent_jev_reclaims WHERE graph_id=? AND reclaim_id=?",
                (event.graph_id, event.source_id)).fetchone()
        if reclaim is None:
            raise GraphConflict("reclaim event has no committed intent")
        effects = self.driver._recover_committed_command(event.graph_id, event.controller_id, event.source_id)
        return {"status": "reconciliation_required" if any(e["state"] in {"sending", "unknown"} for e in effects) else "applied",
                "effects": effects}

    def _input_attachments(self, binding, policy=None, *, conn=None, ids=None):
        if ids is None:
            policy = policy or self.lifecycle.policy(binding["graph_id"])
            ids = json.loads(policy["attachment_ids_json"])
        receipts = []
        for media_id in ids:
            try:
                receipts.append(self.service.media.receipt(media_id, room_id=binding["room_id"], conn=conn))
            except (FileNotFoundError, ValueError, KeyError):
                # Keep the Root and its task state readable if a stored file
                # becomes unavailable after admission.
                continue
        return receipts

    def publish_input(self, binding, *, client_id=""):
        root = self.service.room_work.get(binding["root_work_id"])
        key = "jev-input:" + binding["graph_id"]
        if not self.service.room_events.store.has_projection(key):
            attachments = self._input_attachments(binding)
            self.service.room_events.publish_projection(
                projection_key=key,
                room_id=binding["room_id"],
                event_type="user_message",
                payload={
                    "text": root["objective"],
                    "clientMessageId": client_id or root["clientMessageId"],
                    "mode": "jev",
                    "graphId": binding["graph_id"],
                    **({"attachmentReceipts": attachments} if attachments else {}),
                },
                turn_id=binding["root_turn_id"],
                topic_id=str(root.get("topicId") or ""),
            )
        self.lifecycle.publish_plan_adjustments(binding)

    def execution_terminal(self, effect, *, lookup=True, reads=None):
        if reads is not None:
            if lookup:
                raise ValueError("projection reads cannot perform settlement lookup")
            return reads.terminals.get(effect["effectId"])
        request = effect["request"]
        with self.ledger.connection() as conn:
            prior = conn.execute(
                "SELECT proof_json FROM agent_jev_execution_drains WHERE dispatch_id=?",
                (effect["effectId"],),
            ).fetchone()
        if prior:
            proof = json.loads(prior[0])
        else:
            if not lookup:
                return None
            proof = execution_drained(self.service, request, effect["receipt"])
            if proof is None:
                return None
            with self.ledger.connection(write=True) as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO agent_jev_execution_drains VALUES(?,?,?)",
                    (effect["effectId"], canonical(proof), self.ledger.clock_ms()),
                )
        return self._terminal_record(proof)

    @staticmethod
    def _terminal_record(proof):
        return {
            "eventId": proof["proofRef"],
            "status": proof["terminal"],
            "eventType": "turn_failed"
            if proof["terminal"] == "failed"
            else "turn_completed",
        }

    def assignment_options(self, binding, task_id):
        """Inspect one task on demand; normal projection reads stay inexpensive.

        This is an advisory menu, not admission. The eventual command rechecks
        its exact task hash, current eligibility and physical execution fact.
        """
        graph_id = binding["graph_id"]
        snapshot = self.ledger.snapshot(graph_id, binding["controller_id"])
        task = snapshot.task(task_id)
        policy = self.lifecycle.policy(graph_id)
        reason, action, targets = "", "", []
        if policy["stopped"] or policy["final_json"] != "{}":
            reason = "root_inactive"
        elif policy["phase"] != "execute":
            reason = "phase_unavailable"
        elif not snapshot.is_active(task.id) or self.revisions.is_target_for(graph_id, task.id):
            reason = "task_revision_pending_or_superseded"
        elif any(item.parent_id == task.id for item in snapshot.tasks):
            reason = "container_task"
        elif any(item["task_id"] == task.id for item in json.loads(snapshot.reclaims_json)):
            reason = "reclaim_pending"
        elif task.state not in {"active", "queued"}:
            reason = "task_unavailable"
        else:
            fact = self.executions(snapshot)[task.id]
            if (snapshot.pending_dispatches(task.id) or not fact.matches(task)
                or fact.status not in {"running", "idle", "drained"}
                or (fact.status == "drained" and not fact.effects_reconciled)):
                reason = "execution_unknown"
            else:
                specification = self.lifecycle.specifications(graph_id).get(task.id, {})
                targets = [p["id"] for p in self.eligible_participants(
                    snapshot, specification, include_busy=True) if p["id"] != task.owner_id]
                if targets:
                    action = "request_reclaim" if fact.status == "running" else "reassign"
                else:
                    reason = "owner_locked" if specification.get("ownerParticipantId") else "no_eligible_partner"
        return {"ok": True, "graphId": graph_id, "taskId": task.id,
            "taskHash": digest(asdict(task)), "ownerId": task.owner_id,
            "action": action, "targetParticipantIds": targets, "unavailableReason": reason}

    def eligible_participants(self, snapshot, specification, *, include_busy=False,
                              skill_catalogs=None, skill_unavailability=None):
        from .skill_requirements import split_required_capabilities

        result = []
        required_tools, required_skills = split_required_capabilities(specification)
        writes = specification.get("writeTargets", [])
        locked = specification.get("ownerParticipantId")
        catalogs = skill_catalogs if skill_catalogs is not None else {}
        departing = self.removal.pending_ids(snapshot.room_id)
        for participant in self.service.rooms.get(snapshot.room_id)["participants"]:
            if participant["status"] != "active" or participant["id"] in departing or (
                locked and participant["id"] != locked
            ):
                continue
            session = self.service.sessions.get(participant["sessionId"])
            tools = {
                str(t.get("id") or t.get("name"))
                for t in self.service._runtime_tool_manifest(session)
                if t.get("available", True)
            }
            if any(cap not in tools for cap in required_tools):
                continue
            if writes and (
                session.get("toolProfileVersion") == "subagent-readonly-v1"
                or session.get("executionMode") == "read_only"
            ):
                continue
            roots = [Path(p).resolve() for p in session.get("workspaceRoots", [])]
            valid = True
            for target in writes:
                if not isinstance(target, str) or not Path(target).is_absolute():
                    valid = False
                    break
                path = Path(target).resolve()
                if not any(path.is_relative_to(root) for root in roots):
                    valid = False
                    break
            if not valid:
                continue
            if not include_busy and (
                participant["sessionId"] in snapshot.reserved_sessions
                or not self.service._room_target_idle(participant["sessionId"])
            ):
                continue
            if required_skills:
                session_id = participant["sessionId"]
                if session_id not in catalogs:
                    catalogs[session_id] = self.service.session_policy.skill_catalog(session_id)
                catalog = catalogs[session_id]
                if catalog.get("runtimeAvailable") is not True:
                    if skill_unavailability is not None:
                        skill_unavailability[session_id] = "Pi Skill catalog unavailable"
                    continue
                available_skills = {
                    str(command.get("name") or "").removeprefix("skill:")
                    for command in catalog.get("items", [])
                    if isinstance(command, dict)
                    and command.get("source") == "skill"
                    and str(command.get("name") or "").startswith("skill:")
                }
                missing = sorted(set(required_skills) - available_skills)
                if missing:
                    if skill_unavailability is not None:
                        skill_unavailability[session_id] = (
                            "required Skill absent from Pi catalog: " + ", ".join(missing)
                        )
                    continue
            result.append(participant)
        return result

    def wake_resources(self, session_id, *, source_id):
        with self.ledger.connection(write=True) as conn:
            rows = conn.execute(
                "SELECT g.graph_id FROM agent_jev_graphs g JOIN agent_room_participants p ON p.room_id=g.room_id JOIN agent_jev_host_roots h ON h.graph_id=g.graph_id WHERE p.session_id=? AND h.stopped=0 AND h.final_json='{}'",
                (session_id,),
            ).fetchall()
            for row in rows:
                self._enqueue(conn, row[0], "resource:" + source_id, "executor_drained")

    def submit_execution(self, effect, proposal):
        request = effect["request"]
        if (
            effect["state"] != "accepted"
            or request.get("purpose", "execute") != "execute"
        ):
            raise GraphConflict("result has no accepted business execution")
        validate_submission("result_submit", proposal)
        summary = proposal["resultSummary"]
        evidence = proposal["evidenceRefs"]
        artifacts = proposal.get("artifactRefs", [])
        graph_id = request["graphId"]
        command_id = "submit:" + request["dispatchId"]
        payload_hash = digest(
            {"summary": summary, "evidence": evidence, "artifacts": artifacts}
        )
        with self.ledger.connection() as conn:
            prior = conn.execute(
                "SELECT result_json,intent_hash FROM agent_jev_commands WHERE command_id=? AND graph_id=?",
                (command_id, graph_id),
            ).fetchone()
            if prior:
                if json.loads(prior[0]).get("submissionHash") != payload_hash:
                    raise GraphConflict("result changed after submission")
                return {**json.loads(prior[0]), "replayed": True}
        snapshot = self.ledger.snapshot(graph_id, request["controllerId"])
        task = snapshot.task(request["taskId"])
        with self.ledger.connection() as conn:
            self.lifecycle._live(conn, request)
            root_epoch = self.lifecycle.policy(graph_id, conn)["epoch"]
        terminal = self.execution_terminal(effect)
        fact = ExecutionFact(
            task.id,
            "drained" if terminal else "running",
            dispatch_id=request["dispatchId"],
            session_id=request["sessionId"],
            task_revision=request["taskRevision"],
            owner_id=request["ownerId"],
            assignment_key=request["assignmentKey"],
            accepted_turn_id=request["acceptedTurnId"],
            proof_ref=terminal["eventId"]
            if terminal
            else "pi:" + effect["receipt"]["turnId"],
            effects_reconciled=bool(terminal),
        )
        candidate = Candidate.make(
            "submit",
            task.id,
            "接收精确执行提交",
            {
                "resultSummary": summary,
                "artifactRefs": artifacts,
                "evidenceRefs": evidence,
                "execution": execution_dict(fact),
            },
        )
        result = self.owner.apply(snapshot, candidate, command_id=command_id,
                                  expected_root_epoch=root_epoch)
        return result

    def settle_dispatch(self, record, *, phase, result, completion_source, **kwargs):
        effect = self.lifecycle.effect_for_dispatch(record["childDispatchId"])
        if effect is None:
            return None
        request = effect["request"]
        if request.get("purpose", "execute") != "execute":
            return dict(record)
        if effect["state"] != "accepted":
            effect = self.effects.reconcile(record["childDispatchId"])
        if effect["state"] != "accepted":
            return dict(record)
        try:
            with self.ledger.connection() as conn:
                self.lifecycle._live(conn, request)
                if conn.execute("SELECT 1 FROM agent_jev_reclaims WHERE graph_id=? AND dispatch_id=? AND status='requested'",
                                (request["graphId"], request["dispatchId"])).fetchone():
                    return dict(record)
        except GraphConflict:
            return dict(record)
        current = self.service.room_work.get(request["taskId"])
        if current["state"] in {"review", "done", "cancelled", "blocked", "failed"}:
            status = (
                "review"
                if current["state"] in {"review", "done"}
                else "aborted"
                if current["state"] == "cancelled"
                else "failed"
            )
            return self.service.room_partner_dispatches.settle(
                record["childDispatchId"],
                status=status,
                result=str(current.get("resultSummary") or result),
                completion_source=completion_source,
            )
        if phase == "completed" and result:
            self.submit_execution(
                effect,
                {
                    "resultSummary": result[:4000],
                    "evidenceRefs": [record["childDispatchId"]],
                    "artifactRefs": [],
                },
            )
            return self.service.room_partner_dispatches.settle(
                record["childDispatchId"],
                status="review",
                result=result,
                completion_source=completion_source,
            )
        if self.execution_terminal(effect):
            snapshot = self.ledger.snapshot(request["graphId"], request["controllerId"])
            task = snapshot.task(request["taskId"])

            def before(conn):
                self.ledger.require_unchanged(conn, snapshot)
                self.lifecycle._live(conn, request)

            def after(conn):
                self.ledger.save_receipt(
                    conn,
                    "terminal-failure:" + request["dispatchId"],
                    snapshot,
                    digest([request["dispatchId"], phase]),
                    "execution_failed",
                    task.id,
                    {
                        "status": "applied",
                        "dispatchId": request["dispatchId"],
                        "phase": phase,
                    },
                )

            with guarded_work_transaction(
                TransactionHooks(self.ledger.db_path, before, after)
            ):
                self.service.room_work.escalate(
                    request["sessionId"],
                    {
                        "workId": task.id,
                        "reason": result[:2000] or "Pi ended without a valid result",
                        "nextStep": "Jev retries the same responsibility within its revision budget",
                    },
                )
        return dict(record)

    def guard_legacy_work(self, *, session_id="", payload=None, task_id=""):
        """Legacy public mutations cannot bypass the bound Jev owner commands."""
        values = payload or {}
        if session_id:
            _, dispatch_id = self.service.room_turns.active_turn(session_id)
            if self.lifecycle.effect_for_dispatch(dispatch_id):
                raise GraphConflict(
                    "use the current Jev purpose submission for managed work"
                )
        work_id = task_id or str(
            values.get("workId") or values.get("parentWorkId") or ""
        )
        root_id = str(values.get("rootTurnId") or "")
        with self.ledger.connection() as conn:
            if work_id:
                row = conn.execute(
                    "SELECT root_turn_id FROM agent_room_work_items WHERE id=?",
                    (work_id,),
                ).fetchone()
                root_id = row[0] if row else root_id
            if (
                root_id
                and conn.execute(
                    "SELECT 1 FROM agent_jev_graphs WHERE root_turn_id=?", (root_id,)
                ).fetchone()
            ):
                raise GraphConflict("use Jev commands for this managed task graph")

    def tool_operation(self, session_id, args, *, tool_call_id):
        root_id, dispatch_id = self.service.room_turns.active_turn(session_id)
        effect = self.lifecycle.effect_for_dispatch(dispatch_id)
        if effect is None:
            return None
        operation = str(args.get("op") or "list")
        request = effect["request"]
        self.require_active(request["graphId"])
        if operation in {
            "plan_submit",
            "result_submit",
            "verification_submit",
            "final_submit",
        }:
            return self.lifecycle.submit(
                session_id, operation, args.get("proposal"), tool_call_id=tool_call_id
            )
        if operation == "list":
            view = self.projection(request["roomId"], request["graphId"])
            # Room outcomes are shared; other executors' prepared material
            # bodies and private decision journals are not another context pack.
            # Keep list useful for dependencies without recursively reinjecting
            # every dispatch's prompts, manifests and event payloads into Pi.
            public = {key: view[key] for key in (
                "ok", "mode", "schemaVersion", "graphId", "rootId", "snapshotVersion",
                "ready", "running", "review", "blocked", "tasks", "edges", "stopped",
                "phase", "requirementsRevision", "final", "planApproval",
            ) if key in view}
            public["executions"] = [{
                "dispatchId": item["effectId"], "state": item["state"],
                "executionStatus": item.get("executionStatus", "unknown"),
                **{key: item["request"].get(key) for key in ("taskId", "ownerId", "purpose")},
            } for item in view["effects"] if item["operation"] == "dispatch"]
            contract = submission_contract({
                "plan": "plan_submit", "execute": "result_submit", "verify": "verification_submit",
                "synthesize": "final_submit",
            }[request.get("purpose", "execute")])
            return {
                **public,
                **({"submissionContract": contract} if contract is not None else {}),
                "purpose": request.get("purpose", "execute"),
                "submissionOperation": {
                    "plan": "plan_submit",
                    "execute": "result_submit",
                    "verify": "verification_submit",
                    "synthesize": "final_submit",
                }[request.get("purpose", "execute")],
                "participants": self.service.rooms.get(request["roomId"])[
                    "participants"
                ],
            }
        if operation in {
            "delegate",
            "delegate_batch",
            "retry",
            "accept",
            "return",
            "remove_participant",
        }:
            raise GraphConflict(
                "Jev owns this task graph; use the current purpose's structured submission instead of legacy scheduling"
            )
        if operation == "post" and args.get("kind") in {
            "result",
            "work_result",
            "review_result",
        }:
            if request.get("purpose", "execute") != "execute":
                raise GraphConflict(
                    "auxiliary executions must use their structured submission operation"
                )
            return self.lifecycle.submit(
                session_id,
                "result_submit",
                {
                    "resultSummary": str(args.get("content") or "")[:4000],
                    "artifactRefs": [],
                    "evidenceRefs": list(args.get("evidenceRefs") or [dispatch_id]),
                },
                tool_call_id=tool_call_id,
            )
        return None
