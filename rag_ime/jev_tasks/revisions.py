"""Durable, selective revisions of an already executing Jev task graph.

The old WorkItems and Pi effects remain historical facts. An affected branch
is fenced when the intent commits, and successors become current only after
every exact old dispatch is either proven unsent or physically drained.
"""
from __future__ import annotations

import json
from dataclasses import asdict

from .types import Edge, GraphConflict, GraphError, canonical, digest, text


class JevTaskRevisions:
    def __init__(self, app):
        self.app = app
        self.ledger = app.ledger

    @staticmethod
    def current_objective(conn, snapshot) -> str:
        row = conn.execute(
            "SELECT current_objective FROM agent_jev_host_roots WHERE graph_id=?",
            (snapshot.graph_id,),
        ).fetchone()
        return str(row[0] or snapshot.task(snapshot.root_work_id).objective)

    def current_objective_for(self, snapshot) -> str:
        with self.ledger.connection() as conn:
            return self.current_objective(conn, snapshot)

    @staticmethod
    def pending(conn, graph_id):
        return conn.execute(
            "SELECT * FROM agent_jev_task_revisions WHERE graph_id=? AND status='awaiting_drain'",
            (graph_id,),
        ).fetchone()

    def pending_for(self, graph_id) -> bool:
        with self.ledger.connection() as conn:
            return self.pending(conn, graph_id) is not None

    @staticmethod
    def is_target(conn, graph_id, task_id) -> bool:
        return conn.execute(
            "SELECT 1 FROM agent_jev_revision_targets t JOIN agent_jev_task_revisions r "
            "ON r.revision_id=t.revision_id WHERE r.graph_id=? AND r.status='awaiting_drain' "
            "AND t.task_id=?",
            (graph_id, task_id),
        ).fetchone() is not None

    def is_target_for(self, graph_id, task_id) -> bool:
        with self.ledger.connection() as conn:
            return self.is_target(conn, graph_id, task_id)

    def require_current(self, conn, snapshot, task_id, *, purpose="execute"):
        if not snapshot.is_active(task_id):
            raise GraphConflict("task belongs to a superseded graph version")
        if self.is_target(conn, snapshot.graph_id, task_id) or (
            purpose == "synthesize" and self.pending(conn, snapshot.graph_id) is not None
        ):
            raise GraphConflict("task belongs to a pending graph revision")

    @staticmethod
    def affected(snapshot, task_id) -> tuple[str, ...]:
        graph = snapshot.graph()
        if task_id not in graph.tasks or task_id == snapshot.root_work_id:
            raise GraphConflict("only a current planned responsibility can be revised")
        # Context edges also carry input from a changed task. They do not gate
        # execution, but their consumers must not retain acceptance as current.
        downstream = {key: set() for key in graph.tasks}
        for edge in graph.edges:
            downstream[edge.prerequisite].add(edge.dependent)
        impacted, queue = {task_id}, [task_id]
        while queue:
            for key in sorted(downstream[queue.pop(0)] - impacted):
                impacted.add(key)
                queue.append(key)
        return tuple(sorted(impacted))

    def options(self, binding, task_id):
        with self.ledger.connection() as conn:
            snapshot = self.ledger.read_in_transaction(
                conn, binding["graph_id"], binding["controller_id"])
            policy = self.app.lifecycle.policy(snapshot.graph_id, conn)
            task = snapshot.task(task_id)
            available, reason = True, ""
            if not snapshot.is_active(task_id) or task_id == snapshot.root_work_id:
                available, reason = False, "task_not_current_leaf"
            elif policy["stopped"] or policy["final_json"] != "{}":
                available, reason = False, "root_inactive"
            elif policy["phase"] != "execute":
                available, reason = False, "phase_unavailable"
            elif self.pending(conn, snapshot.graph_id) is not None:
                available, reason = False, "revision_pending"
            elif task.state == "cancelled":
                available, reason = False, "task_unavailable"
            affected = self.affected(snapshot, task_id) if available else ()
            if available and any(item["task_id"] in affected for item in json.loads(snapshot.reclaims_json)):
                available, reason = False, "reclaim_pending"
            retained = tuple(sorted(t.id for t in snapshot.active_tasks
                                    if t.id != snapshot.root_work_id and t.id not in affected and t.state == "done"))
            return {
                "ok": True, "graphId": snapshot.graph_id, "rootId": snapshot.root_id,
                "taskId": task_id, "taskHash": digest(asdict(task)),
                "expectedTopologyRevision": snapshot.topology_revision,
                "expectedRequirementsRevision": policy["requirements_revision"],
                "affectedTaskIds": list(affected),
                "downstreamTaskIds": [key for key in affected if key != task_id],
                "retainedAcceptedTaskIds": list(retained),
                "available": available, "unavailableReason": reason,
            }

    def request(self, binding, payload, *, command_intent_hash):
        graph_id = binding["graph_id"]
        command_id = text(payload.get("clientMessageId"), "clientMessageId")
        root_id = text(payload.get("rootId"), "rootId")
        task_id = text(payload.get("taskId"), "taskId")
        if root_id != binding["root_turn_id"]:
            raise GraphConflict("revision belongs to another Root")
        objective = text(payload.get("objective"), "objective", 8000)
        expected_output = text(payload.get("expectedOutput"), "expectedOutput", 8000)
        reason = text(payload.get("reason"), "reason", 2000)
        criteria = payload.get("acceptanceCriteria")
        if not isinstance(criteria, list) or not 1 <= len(criteria) <= 8:
            raise GraphError("revision needs one to eight acceptance criteria")
        criteria = [text(value, "acceptance criterion", 500) for value in criteria]
        root_objective = (text(payload["rootObjective"], "rootObjective", 8000)
                          if "rootObjective" in payload else None)
        for key in ("expectedTopologyRevision", "expectedRequirementsRevision"):
            if type(payload.get(key)) is not int or payload[key] < 0:
                raise GraphError("revision requires exact integer " + key)
        revision_id = "jev-revision:" + digest([graph_id, command_id])[:40]
        with self.ledger.connection(write=True) as conn:
            prior = self.ledger.prior(conn, command_id, graph_id, command_intent_hash)
            if prior is not None:
                return {**prior, "idempotentReplay": True}
            snapshot = self.ledger.read_in_transaction(conn, graph_id, binding["controller_id"])
            self.ledger.require_unchanged(conn, snapshot)
            policy = self.app.lifecycle.policy(graph_id, conn)
            if policy["stopped"] or policy["final_json"] != "{}" or policy["phase"] != "execute":
                raise GraphConflict("Root is not accepting an executing-plan revision")
            approval = self.app.lifecycle.plan_approval(graph_id, conn)
            if approval is not None and approval["status"] != "approved":
                raise GraphConflict("the complete plan is not approved")
            if self.pending(conn, graph_id) is not None:
                raise GraphConflict("another task revision is awaiting exact drain")
            if (snapshot.topology_revision != payload["expectedTopologyRevision"]
                or policy["requirements_revision"] != payload["expectedRequirementsRevision"]):
                raise GraphConflict("task graph or goal revision changed")
            task = snapshot.task(task_id)
            if not snapshot.is_active(task_id) or task_id == snapshot.root_work_id:
                raise GraphConflict("task is not a current planned responsibility")
            if task.state == "cancelled":
                raise GraphConflict("task is unavailable for revision")
            if payload.get("taskHash") != digest(asdict(task)):
                raise GraphConflict("task revision must cover the current result and assignment")
            affected = self.affected(snapshot, task_id)
            if any(item["task_id"] in affected for item in json.loads(snapshot.reclaims_json)):
                raise GraphConflict("affected task is already being reclaimed")
            specs = self.app.lifecycle.specifications(graph_id)
            if any(key not in specs for key in affected):
                raise GraphConflict("affected plan task has no persisted specification")
            changed_spec = dict(specs[task_id], objective=objective,
                                expectedOutput=expected_output, acceptanceCriteria=criteria)
            if task.owner_id not in {p["id"] for p in self.app.eligible_participants(
                    snapshot, changed_spec, include_busy=True)}:
                raise GraphConflict("current task owner no longer satisfies its scope or capability")
            retained = tuple(sorted(t.id for t in snapshot.active_tasks
                                    if t.id != snapshot.root_work_id and t.id not in affected and t.state == "done"))
            required = []
            for row in conn.execute(
                "SELECT e.effect_id,e.state,e.request_json,c.effect_id AS claimed FROM agent_jev_runtime_effects e "
                "LEFT JOIN agent_jev_executor_claims c ON c.effect_id=e.effect_id "
                "WHERE e.graph_id=? AND e.operation='dispatch' AND e.state IN "
                "('pending','sending','unknown','accepted') ORDER BY e.effect_id", (graph_id,)):
                request = json.loads(row["request_json"])
                purpose = request.get("purpose", "execute")
                if request["taskId"] not in affected and purpose != "synthesize":
                    continue
                if row["state"] == "accepted" and row["claimed"] is None:
                    continue
                required.append(row["effect_id"])
            if len(required) > 64:
                raise GraphConflict("revision exceeds bounded exact dispatch set")
            now = self.ledger.clock_ms()
            request_data = {
                "objective": objective, "expectedOutput": expected_output,
                "acceptanceCriteria": criteria, "reason": reason,
                "rootObjective": root_objective,
            }
            result = {
                "ok": True, "revisionId": revision_id, "graphId": graph_id,
                "status": "awaiting_drain", "changedTaskId": task_id,
                "affectedTaskIds": list(affected),
                "retainedAcceptedTaskIds": list(retained),
                "requiredDispatchIds": required,
                "idempotentReplay": False,
            }
            self.ledger.save_receipt(conn, command_id, snapshot, command_intent_hash,
                                     "revise_task", task_id, result)
            conn.execute(
                "INSERT INTO agent_jev_task_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,'{}',?,0)",
                (revision_id, graph_id, command_id, "awaiting_drain",
                 snapshot.topology_revision, policy["requirements_revision"], task_id,
                 canonical(request_data), canonical(affected), canonical(retained),
                 canonical(required), now),
            )
            conn.executemany(
                "INSERT INTO agent_jev_revision_targets VALUES(?,?)",
                [(revision_id, key) for key in affected],
            )
            conn.executemany(
                "INSERT INTO agent_jev_revision_dispatches VALUES(?,?)",
                [(revision_id, key) for key in required],
            )
            # Pending is proof that no Runtime call started. Withdraw it inside
            # the same writer transaction that fences its old task version.
            for dispatch_id in required:
                conn.execute(
                    "UPDATE agent_jev_runtime_effects SET state='not_sent',receipt_json=?,updated_at_ms=? "
                    "WHERE effect_id=? AND state='pending'",
                    (canonical({"state": "not_sent", "source": "local_outbox",
                                "reason": "task graph revision before dispatch"}), now, dispatch_id),
                )
            self.app._enqueue(conn, graph_id, revision_id, "revision_requested")
        self.app.service.wake_scheduler.wake()
        return result

    def projection(self, conn, graph_id):
        return [{
            "revisionId": row["revision_id"], "status": row["status"],
            "changedTaskId": row["changed_task_id"],
            "affectedTaskIds": json.loads(row["affected_json"]),
            "retainedAcceptedTaskIds": json.loads(row["retained_accepted_json"]),
            "requiredDispatchIds": json.loads(row["required_dispatch_ids_json"]),
            "successorTaskIds": list(json.loads(row["successor_json"]).values()),
            "successors": json.loads(row["successor_json"]),
            "createdAtMs": row["created_at_ms"], "appliedAtMs": row["applied_at_ms"],
        } for row in conn.execute(
            "SELECT * FROM agent_jev_task_revisions WHERE graph_id=? ORDER BY created_at_ms,revision_id",
            (graph_id,),
        )]

    @staticmethod
    def _cancel_id(revision_id, dispatch_id):
        return "revision:" + digest([revision_id, dispatch_id])[:40]

    def _ensure_cancel(self, revision, effect):
        request = effect["request"]
        cancel_id = self._cancel_id(revision["revision_id"], effect["effectId"])
        effect_id = "cancel:" + cancel_id
        with self.ledger.connection(write=True) as conn:
            row = self.pending(conn, revision["graph_id"])
            if row is None or row["revision_id"] != revision["revision_id"]:
                return None
            conn.execute(
                "INSERT OR IGNORE INTO agent_jev_runtime_effects "
                "VALUES(?,?,?,'cancel',?,'pending','{}',?)",
                (effect_id, revision["graph_id"], revision["command_id"],
                 canonical({"reclaimId": cancel_id, "graphId": request["graphId"],
                            "controllerId": request["controllerId"],
                            "rootId": request["rootId"], "taskId": request["taskId"],
                            "dispatchId": effect["effectId"], "sessionId": request["sessionId"],
                            "binding": [request[k] for k in
                                ("taskId", "taskRevision", "ownerId", "assignmentKey", "acceptedTurnId")]}),
                 self.ledger.clock_ms()),
            )
        return effect_id

    def advance_pending(self, graph_id):
        """Reconcile one revision; never infer drain from cancellation acceptance."""
        with self.ledger.connection() as conn:
            revision = self.pending(conn, graph_id)
        if revision is None:
            return {"status": "none"}
        required = json.loads(revision["required_dispatch_ids_json"])
        for dispatch_id in required:
            effect = self.app.effects.get(dispatch_id)
            if effect["state"] in {"sending", "unknown"}:
                effect = self.app.effects.reconcile(dispatch_id)
            if effect["state"] in {"sending", "unknown", "pending"}:
                return {"status": "awaiting_reconciliation", "revisionId": revision["revision_id"]}
            if effect["state"] in {"not_sent", "rejected"}:
                self.app.cleanup_nonadmission(graph_id)
                continue
            if effect["state"] != "accepted":
                raise GraphConflict("revision dispatch has no recognized outcome")
            terminal = self.app.execution_terminal(effect)
            cancel_id = self._ensure_cancel(revision, effect)
            if cancel_id is None:
                return {"status": "superseded"}
            cancel = self.app.effects.get(cancel_id)
            if terminal is not None and cancel["state"] == "pending":
                cancel = self.app.effects.abandon_prepared(
                    cancel_id, reason="exact old execution drained before revision cancel")
            elif cancel["state"] == "pending":
                cancel = self.app.effects.deliver(cancel_id)
            elif cancel["state"] in {"sending", "unknown"}:
                cancel = self.app.effects.reconcile(cancel_id)
            if terminal is None or cancel["state"] in {"pending", "sending", "unknown"}:
                return {"status": "awaiting_drain", "revisionId": revision["revision_id"]}
        return self._apply(revision["revision_id"])

    def _apply(self, revision_id):
        # The host's normal reconcile path releases each exact claim and settles
        # auxiliary turns. Recheck all of that under the final graph writer.
        with self.ledger.connection(write=True) as conn:
            revision = conn.execute(
                "SELECT * FROM agent_jev_task_revisions WHERE revision_id=?", (revision_id,)
            ).fetchone()
            if revision is None:
                raise GraphError("unknown task revision")
            if revision["status"] == "applied":
                return {"status": "applied", "revisionId": revision_id,
                        "successorTaskIds": json.loads(revision["successor_json"])}
            graph_id = revision["graph_id"]
            policy = self.app.lifecycle.policy(graph_id, conn)
            if policy["stopped"] or policy["final_json"] != "{}":
                return {"status": "root_inactive", "revisionId": revision_id}
            controller_id = conn.execute(
                "SELECT controller_id FROM agent_jev_graphs WHERE graph_id=?", (graph_id,)
            ).fetchone()[0]
            snapshot = self.ledger.read_in_transaction(conn, graph_id, controller_id)
            self.ledger.require_unchanged(
                conn, snapshot, allowed_root_states=("active", "review", "blocked", "failed"))
            if (snapshot.topology_revision != revision["base_topology_revision"]
                or policy["requirements_revision"] != revision["base_requirements_revision"]):
                raise GraphConflict("task graph changed while revision awaited drain")
            required = json.loads(revision["required_dispatch_ids_json"])
            for dispatch_id in required:
                effect = conn.execute(
                    "SELECT state FROM agent_jev_runtime_effects WHERE effect_id=? AND graph_id=?",
                    (dispatch_id, graph_id),
                ).fetchone()
                if effect is None or effect["state"] not in {"accepted", "rejected", "not_sent"}:
                    return {"status": "awaiting_reconciliation", "revisionId": revision_id}
                if conn.execute("SELECT 1 FROM agent_jev_executor_claims WHERE effect_id=?",
                                (dispatch_id,)).fetchone():
                    return {"status": "awaiting_drain", "revisionId": revision_id}
                if effect["state"] == "accepted":
                    if conn.execute(
                        "SELECT 1 FROM agent_jev_execution_drains WHERE dispatch_id=?",
                        (dispatch_id,),
                    ).fetchone() is None:
                        return {"status": "awaiting_drain", "revisionId": revision_id}
                    cancel_id = "cancel:" + self._cancel_id(revision_id, dispatch_id)
                    cancel = conn.execute(
                        "SELECT state FROM agent_jev_runtime_effects WHERE effect_id=?",
                        (cancel_id,),
                    ).fetchone()
                    if cancel is None or cancel["state"] in {"pending", "sending", "unknown"}:
                        return {"status": "awaiting_reconciliation", "revisionId": revision_id}
            affected = set(json.loads(revision["affected_json"]))
            if any(not snapshot.is_active(key) for key in affected):
                raise GraphConflict("affected task version changed before successor commit")
            request = json.loads(revision["request_json"])
            specs = {
                row[0]: json.loads(row[1])
                for row in conn.execute(
                    "SELECT task_id,specification_json FROM agent_jev_task_requirements WHERE graph_id=?",
                    (graph_id,),
                )
            }
            graph = snapshot.graph()
            successors = {}
            for task_id in affected:
                old = snapshot.task(task_id)
                owner = conn.execute(
                    "SELECT participant_status FROM agent_room_participants WHERE id=? AND room_id=?",
                    (old.owner_id, snapshot.room_id),
                ).fetchone()
                if owner is None or owner[0] != "active":
                    return {"status": "awaiting_owner", "revisionId": revision_id}
            for task_id in sorted(affected):
                old = snapshot.task(task_id)
                spec = dict(specs[task_id])
                if task_id == revision["changed_task_id"]:
                    spec.update(objective=request["objective"],
                                expectedOutput=request["expectedOutput"],
                                acceptanceCriteria=request["acceptanceCriteria"])
                new = self.app.service.room_work.create(
                    room_id=snapshot.room_id,
                    objective=spec["objective"], expected_output=spec["expectedOutput"],
                    current_owner_participant_id=old.owner_id,
                    created_by_participant_id=snapshot.participant_id,
                    accountable_participant_id=snapshot.participant_id,
                    client_message_id="revision:" + revision_id + ":" + task_id,
                    root_turn_id=snapshot.root_id,
                    parent_work_id=snapshot.root_work_id,
                    acceptance_criteria=spec["acceptanceCriteria"], depth=2,
                    _connection=conn,
                )
                successors[task_id] = new["id"]
                conn.execute("INSERT INTO agent_jev_task_requirements VALUES(?,?,?)",
                             (new["id"], graph_id, canonical(spec)))
            for edge in graph.edges:
                if edge.prerequisite in affected or edge.dependent in affected:
                    conn.execute(
                        "INSERT INTO agent_jev_edges VALUES(?,?,?,?)",
                        (graph_id, successors.get(edge.prerequisite, edge.prerequisite),
                         successors.get(edge.dependent, edge.dependent), edge.kind),
                    )
            now = self.ledger.clock_ms()
            for old_id, new_id in successors.items():
                self.app.service.room_work.retire_superseded_in_transaction(
                    conn, work_id=old_id, actor_participant_id=snapshot.participant_id,
                    reason=request["reason"],
                )
                conn.execute(
                    "INSERT INTO agent_jev_task_supersessions VALUES(?,?,?,?,?)",
                    (graph_id, old_id, new_id, revision_id, now),
                )
            current_goal = request["rootObjective"] or self.current_objective(conn, snapshot)
            conn.execute(
                "UPDATE agent_jev_graphs SET topology_revision=topology_revision+1 WHERE graph_id=?",
                (graph_id,),
            )
            conn.execute(
                "UPDATE agent_jev_host_roots SET requirements_revision=requirements_revision+1,"
                "current_objective=? WHERE graph_id=?",
                (current_goal, graph_id),
            )
            current = self.ledger.read_in_transaction(conn, graph_id, snapshot.controller_id)
            current.graph()  # An invalid successor graph rolls back all WorkItems/events.
            if self.app.lifecycle.plan_approval(graph_id, conn) is not None:
                current_specs = [specs.get(t.id) or json.loads(conn.execute(
                    "SELECT specification_json FROM agent_jev_task_requirements WHERE task_id=?",
                    (t.id,),
                ).fetchone()[0]) for t in current.active_tasks if t.id != current.root_work_id]
                proposal = {"requirementsRevision": policy["requirements_revision"] + 1,
                            "topologyRevision": current.topology_revision, "tasks": current_specs}
                conn.execute(
                    "UPDATE agent_jev_plan_approvals SET requirements_revision=?,plan_hash=?,"
                    "proposal_json=?,status='approved',updated_at_ms=? WHERE graph_id=?",
                    (policy["requirements_revision"] + 1, digest(proposal),
                     canonical(proposal), now, graph_id),
                )
            conn.execute(
                "UPDATE agent_jev_task_revisions SET status='applied',successor_json=?,applied_at_ms=? "
                "WHERE revision_id=? AND status='awaiting_drain'",
                (canonical(successors), now, revision_id),
            )
            self.app._enqueue(conn, graph_id, "revision-applied:" + revision_id,
                              "requirements_changed")
        self.app.service.wake_scheduler.wake()
        return {"status": "applied", "revisionId": revision_id,
                "successorTaskIds": successors}
