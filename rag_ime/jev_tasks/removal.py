"""Durable JEV participant removal through the existing WorkItem and Pi owners.

A pending intent fences new JEV admission. It never treats cancellation acceptance,
missing activity, or a timeout as physical drain, and never rewrites done work.
"""
from __future__ import annotations

import json
from .owner import execution_dict
from .types import Candidate, GraphConflict, canonical, digest, text


class JevParticipantRemoval:
    def __init__(self, app):
        self.app = app
        self.service = app.service
        self.ledger = app.ledger

    def pending_ids(self, room_id: str, *, conn=None) -> set[str]:
        if conn is None:
            with self.ledger.connection() as db:
                return self.pending_ids(room_id, conn=db)
        return {str(row[0]) for row in conn.execute(
            "SELECT participant_id FROM agent_jev_participant_removals "
            "WHERE room_id=? AND status='pending'", (room_id,)
        )}

    def projection(self, room_id: str, *, conn=None) -> list[dict[str, object]]:
        if conn is None:
            with self.ledger.connection() as db:
                return self.projection(room_id, conn=db)
        rows = conn.execute(
            "SELECT * FROM agent_jev_participant_removals WHERE room_id=? "
            "AND status='pending' ORDER BY created_at_ms, removal_id", (room_id,)
        ).fetchall()
        return [self._public(row) for row in rows]

    @staticmethod
    def _public(row) -> dict[str, object]:
        return {"removalId": row["removal_id"], "participantId": row["participant_id"],
                "status": row["status"], "stage": row["stage"], "detail": row["detail"],
                "targetParticipantId": row["target_participant_id"],
                "stopRoot": bool(row["stop_root"]), "createdAtMs": row["created_at_ms"]}

    def _reply(self, room_id: str, participant_id: str, removal: dict[str, object]) -> dict[str, object]:
        room = self.service.rooms.get(room_id)
        return {"schemaVersion": "rag-ime.agent-room-participant-remove.v1", "ok": True,
                "status": removal["status"], "removal": removal, "room": room,
                "participant": self.service.rooms.participant(participant_id),
                "releasedWorkItems": []}

    def _blocked(self, room_id: str, participant_id: str, stage: str, detail: str) -> dict[str, object]:
        return self._reply(room_id, participant_id, {"removalId": "", "participantId": participant_id,
            "status": "blocked", "stage": stage, "detail": detail,
            "targetParticipantId": "", "stopRoot": False, "createdAtMs": 0})

    def request(self, room_id: str, payload) -> dict[str, object] | None:
        """Durably reserve one of the Room's removable member slots.

        The brief admission gate serializes this insert with the final Pi
        acceptance fence. No Skill catalog, artifact, or Runtime I/O is done
        while holding the gate or the database writer.
        """
        participant_id = text(payload.get("participantId"), "participantId")
        client_id = text(payload.get("clientMessageId") or "membership:" + participant_id,
                         "clientMessageId", 200)
        target_id = text(payload.get("replacementParticipantId") or "", "replacementParticipantId", 320, empty=True)
        reason = text(payload.get("reason") or "", "reason", 500, empty=True)
        stop_root = payload.get("stopRoot", False)
        if not isinstance(stop_root, bool):
            raise ValueError("stopRoot must be boolean")
        intent_hash = digest([room_id, participant_id, client_id, target_id, reason, stop_root])
        with self.service._jev_removal_admission_lock:
            with self.ledger.connection(write=True) as conn:
                prior = conn.execute(
                    "SELECT * FROM agent_jev_participant_removals WHERE room_id=? AND participant_id=? "
                    "ORDER BY created_at_ms DESC LIMIT 1", (room_id, participant_id)
                ).fetchone()
                target_change_hash = digest([room_id, participant_id, client_id, target_id])
                target_change = conn.execute(
                    "SELECT * FROM agent_jev_participant_removal_targets "
                    "WHERE room_id=? AND client_message_id=?", (room_id, client_id)
                ).fetchone()
                if prior and prior["status"] == "pending":
                    if prior["client_message_id"] == client_id:
                        if prior["request_hash"] != intent_hash:
                            raise GraphConflict("reuse the original clientMessageId only for exact replay")
                        return self._reply(room_id, participant_id, self._public(prior))
                    if target_change:
                        if (target_change["removal_id"] != prior["removal_id"]
                            or target_change["request_hash"] != target_change_hash):
                            raise GraphConflict("clientMessageId already names another target change")
                        return self._reply(room_id, participant_id, self._public(prior))
                    if "replacementParticipantId" not in payload:
                        raise GraphConflict("a pending removal needs a new clientMessageId and replacementParticipantId to retarget")
                    if ("stopRoot" in payload and stop_root != bool(prior["stop_root"])) or (
                        "reason" in payload and reason != prior["reason"]
                    ):
                        raise GraphConflict("retarget cannot change stopRoot or reason")
                    room = self.service.rooms.get(room_id, conn=conn)
                    if not any(p["id"] == participant_id and p["status"] == "active"
                               for p in room["participants"]):
                        raise GraphConflict("removal already committed; target can no longer change")
                    active = [p for p in room["participants"] if p["status"] == "active"]
                    if target_id == participant_id or (target_id and (
                        not any(p["id"] == target_id for p in active)
                        or conn.execute("SELECT 1 FROM agent_jev_participant_removals "
                                        "WHERE room_id=? AND participant_id=? AND status='pending'",
                                        (room_id, target_id)).fetchone()
                    )):
                        return self._blocked(room_id, participant_id, "requires_partner",
                                             "replacement must be another available active participant")
                    now = self.ledger.clock_ms()
                    conn.execute("INSERT INTO agent_jev_participant_removal_targets VALUES(?,?,?,?,?,?)",
                                 (room_id, client_id, prior["removal_id"], target_change_hash, target_id, now))
                    conn.execute("UPDATE agent_jev_participant_removals "
                                 "SET target_participant_id=?,targets_json='{}',stage='requested',detail='',updated_at_ms=? "
                                 "WHERE removal_id=? AND status='pending'",
                                 (target_id, now, prior["removal_id"]))
                    updated = dict(prior)
                    updated.update(target_participant_id=target_id, targets_json="{}",
                                   stage="requested", detail="", updated_at_ms=now)
                    self.service.wake_scheduler.wake()
                    return self._reply(room_id, participant_id, self._public(updated))
                if prior and prior["client_message_id"] == client_id:
                    if prior["request_hash"] != intent_hash:
                        raise GraphConflict("clientMessageId already names another removal")
                    return self._reply(room_id, participant_id, self._public(prior))
                if target_change:
                    if target_change["request_hash"] != target_change_hash:
                        raise GraphConflict("clientMessageId already names another target change")
                    if prior and target_change["removal_id"] == prior["removal_id"]:
                        return self._reply(room_id, participant_id, self._public(prior))
                    raise GraphConflict("target change belongs to another removal")
                if conn.execute("SELECT 1 FROM agent_jev_participant_removals "
                                "WHERE room_id=? AND client_message_id=?", (room_id, client_id)).fetchone():
                    raise GraphConflict("clientMessageId already names another removal")
                graphs = conn.execute(
                    "SELECT g.* FROM agent_jev_graphs g JOIN agent_jev_host_roots h USING(graph_id) "
                    "WHERE g.room_id=? AND h.stopped=0 AND h.final_json='{}'", (room_id,)
                ).fetchall()
                if not graphs:
                    actor = conn.execute(
                        "SELECT session_id FROM agent_room_participants WHERE room_id=? AND id=? "
                        "AND participant_status='active'", (room_id, participant_id)
                    ).fetchone()
                    if actor is None or not self._outstanding_effect(
                        conn, room_id, participant_id, actor["session_id"]
                    ):
                        return None
                room = self.service.rooms.get(room_id, conn=conn)
                participant = next((p for p in room["participants"] if p["id"] == participant_id), None)
                if participant is None or participant["status"] != "active":
                    raise ValueError("participant is not active in this Room")
                active = [p for p in room["participants"] if p["status"] == "active"]
                pending_count = conn.execute(
                    "SELECT COUNT(*) FROM agent_jev_participant_removals r "
                    "JOIN agent_room_participants p ON p.id=r.participant_id "
                    "WHERE r.room_id=? AND r.status='pending' AND p.participant_status='active'",
                    (room_id,)
                ).fetchone()[0]
                if len(active) - int(pending_count) - 1 < 2:
                    return self._blocked(room_id, participant_id, "minimum_participants",
                                         "pending removals reserve two active Room participants")
                if room["routingPolicy"] == "moderator" and room["moderatorParticipantId"] == participant_id:
                    return self._blocked(room_id, participant_id, "change_moderator",
                                         "change the Room moderator before removal")
                is_controller = any(g["controller_participant_id"] == participant_id for g in graphs)
                if is_controller and not stop_root:
                    return self._blocked(room_id, participant_id, "controller_requires_stop",
                                         "active JEV controller requires explicit stopRoot")
                if stop_root and not is_controller and not conn.execute(
                    "SELECT 1 FROM agent_jev_graphs g JOIN agent_jev_host_roots h USING(graph_id) "
                    "WHERE g.room_id=? AND g.controller_participant_id=? AND h.stopped=1 LIMIT 1",
                    (room_id, participant_id)
                ).fetchone():
                    return self._blocked(room_id, participant_id, "invalid_stop_mode",
                                         "stopRoot applies only to a JEV controller")
                if target_id == participant_id or (target_id and (
                    not any(p["id"] == target_id for p in active)
                    or conn.execute("SELECT 1 FROM agent_jev_participant_removals "
                                    "WHERE room_id=? AND participant_id=? AND status='pending'",
                                    (room_id, target_id)).fetchone()
                )):
                    return self._blocked(room_id, participant_id, "requires_partner",
                                         "replacement must be another available active participant")
                removal_id = "jev-removal:" + digest([room_id, client_id])[:40]
                now = self.ledger.clock_ms()
                conn.execute(
                    "INSERT INTO agent_jev_participant_removals VALUES(?,?,?,?,?,?,'{}',?,?,'pending','requested','',?,?)",
                    (removal_id, room_id, participant_id, client_id, intent_hash,
                     target_id, int(stop_root), reason, now, now)
                )
        self.service.wake_scheduler.wake()
        return self._reply(room_id, participant_id, {"removalId": removal_id,
            "participantId": participant_id, "status": "pending", "stage": "requested", "detail": "",
            "targetParticipantId": target_id, "stopRoot": stop_root, "createdAtMs": now})

    def advance_all(self) -> None:
        with self.ledger.connection() as conn:
            ids = [row[0] for row in conn.execute(
                "SELECT removal_id FROM agent_jev_participant_removals WHERE status='pending' "
                "ORDER BY created_at_ms, removal_id")]
        for removal_id in ids:
            try:
                self.advance(removal_id)
            except Exception as exc:
                self._stage(removal_id, "retrying", type(exc).__name__)

    def _stage(self, removal_id: str, stage: str, detail: str = "") -> None:
        with self.ledger.connection(write=True) as conn:
            conn.execute(
                "UPDATE agent_jev_participant_removals SET stage=?, detail=?, updated_at_ms=? "
                "WHERE removal_id=? AND status='pending' AND (stage<>? OR detail<>?)",
                (stage, detail, self.ledger.clock_ms(), removal_id, stage, detail)
            )

    def _references(self, task, participant_id: str) -> bool:
        if participant_id in {task.owner_id, task.accountable_id}:
            return True
        with self.ledger.connection() as conn:
            offered = conn.execute(
                "SELECT offered_to_participant_id FROM agent_room_work_items WHERE id=?",
                (task.id,)).fetchone()
        return bool(offered and offered[0] == participant_id)

    def _target(self, row, snapshot, task) -> str:
        saved = json.loads(row["targets_json"])
        preferred = saved.get(task.id) or row["target_participant_id"]
        specification = self.app.lifecycle.specifications(snapshot.graph_id).get(task.id, {})
        eligible = [p["id"] for p in self.app.eligible_participants(
            snapshot, specification, include_busy=True) if p["id"] != row["participant_id"]]
        if task.owner_id != row["participant_id"] and task.owner_id in eligible:
            target = task.owner_id
        elif preferred:
            target = preferred if preferred in eligible else ""
        else:
            target = eligible[0] if eligible else ""
        if target and saved.get(task.id) != target:
            saved[task.id] = target
            with self.ledger.connection(write=True) as conn:
                changed = conn.execute(
                    "UPDATE agent_jev_participant_removals SET targets_json=?,updated_at_ms=? "
                    "WHERE removal_id=? AND status='pending' AND target_participant_id=? AND targets_json=?",
                    (canonical(saved), self.ledger.clock_ms(), row["removal_id"],
                     row["target_participant_id"], row["targets_json"]))
                if changed.rowcount != 1:
                    raise GraphConflict("removal destination changed; recompute current target")
        return target

    def reclaim_target(self, snapshot, reclaim) -> str:
        task = snapshot.task(reclaim["task_id"])
        with self.ledger.connection() as conn:
            removal = conn.execute(
                "SELECT * FROM agent_jev_participant_removals "
                "WHERE room_id=? AND participant_id=? AND status='pending'",
                (snapshot.room_id, task.owner_id)).fetchone()
        return self._target(removal, snapshot, task) if removal else reclaim["target_participant_id"]

    def advance(self, removal_id: str) -> None:
        with self.ledger.connection() as conn:
            row = conn.execute("SELECT * FROM agent_jev_participant_removals WHERE removal_id=?",
                               (removal_id,)).fetchone()
            if row is None or row["status"] != "pending":
                return
            graphs = [dict(g) for g in conn.execute(
                "SELECT g.* FROM agent_jev_graphs g JOIN agent_jev_host_roots h USING(graph_id) "
                "WHERE g.room_id=? AND h.stopped=0 AND h.final_json='{}'", (row["room_id"],))]
        room_id, participant_id = row["room_id"], row["participant_id"]
        participant = self.service.rooms.participant(participant_id)
        if participant["status"] == "removed":
            self._finish(row)
            return
        for graph in graphs:
            if graph["controller_participant_id"] == participant_id:
                if not row["stop_root"]:
                    self._stage(removal_id, "controller_requires_stop")
                    return
                self.app.stop(room_id, graph["root_turn_id"])
                self._stage(removal_id, "awaiting_stop")
                return
            snapshot = self.ledger.snapshot(graph["graph_id"], graph["controller_id"])
            facts = self.app.executions(snapshot)
            for task in snapshot.active_tasks:
                if task.state in {"done", "failed", "cancelled"} or not self._references(task, participant_id):
                    continue
                if task.state == "review":
                    # The verifier still needs the original active worker
                    # identity to bind exact execution evidence.
                    self._stage(removal_id, "awaiting_review", task.id)
                    return
                if snapshot.pending_dispatches(task.id):
                    self._stage(removal_id, "awaiting_receipt", task.id)
                    return
                fact = facts[task.id]
                if not fact.matches(task) or fact.status not in {"idle", "drained", "running"} or (
                    fact.status == "drained" and not fact.effects_reconciled
                ):
                    self._stage(removal_id, "awaiting_receipt", task.id)
                    return
                target = self._target(row, snapshot, task)
                if not target:
                    locked = self.app.lifecycle.specifications(graph["graph_id"]).get(task.id, {}).get("ownerParticipantId")
                    self._stage(removal_id, "revise_owner_lock" if locked else "requires_partner", task.id)
                    return
                with self.ledger.connection() as conn:
                    pending = conn.execute(
                        "SELECT reclaim_id FROM agent_jev_reclaims WHERE task_id=? AND status='requested'",
                        (task.id,)).fetchone()
                if pending:
                    if fact.status == "drained":
                        self.app.driver.controller.reclaims.finish(
                            snapshot, reclaim_id=pending["reclaim_id"],
                            command_id="finish:" + pending["reclaim_id"], proof=fact,
                            target_participant_id=target)
                        self.service.wake_scheduler.wake()
                        self._stage(removal_id, "transferring", task.id)
                    else:
                        self._stage(removal_id, "awaiting_stop", task.id)
                    return
                if task.owner_id == participant_id and fact.status == "running":
                    self.app.driver.controller.reclaims.request(
                        snapshot, command_id=removal_id + ":" + task.id,
                        task_id=task.id, target_participant_id=target,
                        execution=fact, reason=row["reason"] or "participant removal",
                        on_applied=lambda conn, graph_id=graph["graph_id"], command_id=removal_id + ":" + task.id: self.app._enqueue(
                            conn, graph_id, command_id, "reclaim_requested"))
                    self.service.wake_scheduler.wake()
                    self._stage(removal_id, "awaiting_stop", task.id)
                    return
                if fact.status == "running":
                    self._stage(removal_id, "awaiting_stop", task.id)
                    return
                candidate = Candidate.make("removal_transfer", task.id,
                    "已核实旧执行停止，移交成员责任", {
                        "targetParticipantId": target,
                        "removedParticipantId": participant_id,
                        "reason": row["reason"] or "participant removal",
                        "execution": execution_dict(fact)})
                self.app.owner.apply(snapshot, candidate,
                    command_id=removal_id + ":transfer:" + task.id,
                    on_applied=lambda conn, graph_id=graph["graph_id"], command_id=removal_id + ":transfer:" + task.id: self.app._enqueue(
                        conn, graph_id, command_id,
                        "assignment_changed"))
                self.service.wake_scheduler.wake()
                self._stage(removal_id, "transferring", task.id)
                return
        if self._undrained_effect(row):
            self._stage(removal_id, "awaiting_stop")
            return
        self._finalize(row)

    @staticmethod
    def _outstanding_effect(conn, room_id: str, participant_id: str, session_id: str) -> bool:
        # A stopped controller still owns the identity of every executor in
        # its immutable Root binding. Accepted dispatches keep a claim until
        # exact drain; pending/sending/unknown dispatches remain ambiguous.
        if conn.execute(
            "SELECT 1 FROM agent_jev_executor_claims c "
            "JOIN agent_jev_graphs g USING(graph_id) WHERE g.room_id=? "
            "AND (c.session_id=? OR g.controller_participant_id=?) LIMIT 1",
            (room_id, session_id, participant_id)
        ).fetchone():
            return True
        return conn.execute(
            "SELECT 1 FROM agent_jev_runtime_effects e JOIN agent_jev_graphs g USING(graph_id) "
            "WHERE g.room_id=? AND e.operation='dispatch' "
            "AND (json_extract(e.request_json,'$.ownerId')=? "
            "OR g.controller_participant_id=?) "
            "AND e.state IN ('pending','sending','unknown') LIMIT 1",
            (room_id, participant_id, participant_id)
        ).fetchone() is not None

    def _undrained_effect(self, row) -> bool:
        session_id = self.service.rooms.participant(row["participant_id"])["sessionId"]
        with self.ledger.connection() as conn:
            return self._outstanding_effect(
                conn, row["room_id"], row["participant_id"], session_id
            )

    def _finalize(self, row) -> None:
        room_id, participant_id = row["room_id"], row["participant_id"]
        lifecycle = self.service.room_management.participants
        participant = self.service.rooms.participant(participant_id)
        session_id = participant["sessionId"]
        session = self.service.sessions.get(session_id)
        if lifecycle.session_is_busy(session_id, session,
                active_session_ids=lifecycle.active_runtime_session_ids()):
            self._stage(row["removal_id"], "awaiting_stop")
            return
        # A concurrent retarget must not change the destination between the
        # safety check and the WorkItem/Room writes. Pi admission uses this same
        # short gate, while catalog and artifact work ran above/outside it.
        with self.service._jev_removal_admission_lock:
            with self.ledger.connection() as conn:
                current = conn.execute(
                    "SELECT * FROM agent_jev_participant_removals WHERE removal_id=?",
                    (row["removal_id"],)).fetchone()
            if current is None or current["status"] != "pending":
                return
            if (current["target_participant_id"] != row["target_participant_id"]
                or current["targets_json"] != row["targets_json"]):
                return  # Retarget won; the next tick uses its fresh destination.
            row = current
            room = self.service.rooms.get(room_id)
            others = [p for p in room["participants"] if p["status"] == "active" and p["id"] != participant_id]
            with self.ledger.connection() as conn:
                other_pending = self.pending_ids(room_id, conn=conn) - {participant_id}
            if len([p for p in others if p["id"] not in other_pending]) < 2:
                self._stage(row["removal_id"], "minimum_participants")
                return
            if room["routingPolicy"] == "moderator" and room["moderatorParticipantId"] == participant_id:
                self._stage(row["removal_id"], "change_moderator")
                return
            available_others = [p for p in others if p["id"] not in other_pending]
            if row["target_participant_id"]:
                if not any(p["id"] == row["target_participant_id"] for p in available_others):
                    self._stage(row["removal_id"], "requires_partner", "selected replacement is unavailable")
                    return
                replacement = row["target_participant_id"]
            else:
                replacement = available_others[0]["id"]
            # Ordinary non-JEV work can still exist. Its existing owner keeps the
            # blocked-release contract; the managed graph path above never uses it.
            self.service.room_work.release_for_participant(room_id, participant_id,
                replacement_participant_id=replacement, reason=row["reason"])
            self.service.sessions.archive(session_id, archived=True)
            try:
                self.service.rooms.remove_participant(room_id, participant_id)
            except Exception:
                # A normal validation failure keeps the member usable. If the
                # process stops between writes, the durable intent resumes removal.
                self.service.sessions.archive(session_id, archived=False)
                raise
        self._finish(row)

    def _finish(self, row) -> None:
        participant = self.service.rooms.participant(row["participant_id"])
        if participant["status"] != "removed":
            return
        self.service.sessions.archive(participant["sessionId"], archived=True)
        self.service.room_events.publish_projection(
            projection_key="jev-removal:" + row["removal_id"],
            room_id=row["room_id"], event_type="participant_status",
            payload={"status": "participant_removed", "participantId": row["participant_id"],
                     "displayName": participant["displayName"], "roleId": participant["roleId"]},
            participant_id=row["participant_id"], source_session_id=participant["sessionId"],
            topic_id=str(self.service.rooms.get(row["room_id"]).get("activeTopicId") or ""))
        with self.ledger.connection(write=True) as conn:
            conn.execute("UPDATE agent_jev_participant_removals SET status='completed',stage='removed',detail='', "
                         "updated_at_ms=? WHERE removal_id=? AND status='pending'",
                         (self.ledger.clock_ms(), row["removal_id"]))
