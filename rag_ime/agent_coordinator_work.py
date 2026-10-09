"""Passive original-command references and result projection for StarMate.

Pi keeps execution and settlement; AgentContextRuntime keeps the one inbox.
This owner never submits a prompt, resumes work, or treats evidence as acceptance.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from .agent_coordinator import require_binding_source
from .agent_command_receipts import AgentCommandReceiptPending

if TYPE_CHECKING:
    from .agent_command_receipts import AgentCommandReceiptStore
    from .agent_context_runtime import AgentContextRuntime
    from .agent_sessions import AgentSessionStore


class AgentCoordinatorWork:
    def __init__(
        self, *, sessions: AgentSessionStore, receipts: AgentCommandReceiptStore,
        context: AgentContextRuntime, read_result: Callable[[str, str, str], dict[str, object]],
        read_room_result: Callable[[str, str], dict[str, object]],
    ) -> None:
        self.sessions = sessions
        self.receipts = receipts
        self.context = context
        self.read_result = read_result
        self.read_room_result = read_room_result

    def register_room_prompt(self, source_id: str, room_id: str, request: Mapping[str, object]) -> bool:
        client = str(request.get("clientMessageId") or "").strip()
        if not client:
            raise ValueError("Room work requires the original clientMessageId")
        digest = hashlib.sha256(json.dumps(dict(request), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.sessions._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            link = conn.execute("SELECT * FROM agent_coordinator_objects WHERE target_id=? AND source_session_id=? AND target_kind='room'",
                                (room_id, source_id)).fetchone()
            if link is None:
                raise ValueError("Room is not controlled by this Agent")
            require_binding_source(conn, {"coordinatorId": link["coordinator_id"], "sourceSessionId": source_id})
            if not self._active_link(conn, {"target_kind": "room", "target_room_id": room_id,
                                         "coordinator_id": link["coordinator_id"], "source_session_id": source_id}):
                raise ValueError("Room work ownership is retired")
            old = conn.execute("SELECT * FROM agent_coordinator_room_work_attempts WHERE target_room_id=? AND client_message_id=?",
                               (room_id, client)).fetchone()
            if old is not None:
                if old["request_sha256"] != digest or old["source_session_id"] != source_id or old["coordinator_id"] != link["coordinator_id"]:
                    raise ValueError("clientMessageId belongs to a different Room attempt")
                return False
            # A command already accepted/pending/failed through another entry
            # cannot be retroactively attributed to this Source.
            if conn.execute("SELECT 1 FROM agent_command_receipts WHERE command_scope='room_message' AND scope_id=? AND client_message_id=?",
                            (room_id, client)).fetchone() is not None:
                raise ValueError("Room command predates this Source attempt")
            conn.execute("INSERT INTO agent_coordinator_room_work_attempts(attempt_id,work_id,coordinator_id,source_session_id,target_room_id,client_message_id,request_sha256,task,created_at_ms) VALUES (?,?,?,?,?,?,?,?,?)",
                         (f"coordinator-room-attempt:{uuid.uuid4()}", f"coordinator-room-work:{uuid.uuid4()}",
                          link["coordinator_id"], source_id, room_id, client, digest, link["task"], _now()))
        return True

    def reconcile_room_acceptance(self, room_id: str, client: str) -> str:
        client = client.strip()
        response = self.receipts.accepted_response_for_exact_command(
            command_scope="room_message", scope_id=room_id, client_message_id=client)
        if response is None or response.get("accepted") is False:
            return ""
        root = str(response.get("roomTurnId") or "")
        if not root or response.get("roomId") != room_id or response.get("clientMessageId") != client:
            raise ValueError("original Room acceptance has no exact identity")
        with self.sessions._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT root_id FROM agent_coordinator_room_work_attempts WHERE target_room_id=? AND client_message_id=?",
                               (room_id, client)).fetchone()
            if old is None:
                return ""
            if old["root_id"] and old["root_id"] != root:
                raise ValueError("original Room acceptance changed Root")
            proof = {"roomId": room_id, "roomTurnId": root, "clientMessageId": client}
            conn.execute("UPDATE agent_coordinator_room_work_attempts SET root_id=?, acceptance_json=? WHERE target_room_id=? AND client_message_id=?",
                         (root, json.dumps(proof, sort_keys=True), room_id, client))
        return root

    def original_room_response(self, room_id: str, client: str) -> dict[str, object]:
        self.reconcile_room_acceptance(room_id, client)
        response = self.receipts.accepted_response_for_exact_command(
            command_scope="room_message", scope_id=room_id, client_message_id=client.strip())
        if response is None:
            raise AgentCommandReceiptPending("Original Room dispatch is unresolved; it will not be replayed",
                                           client_message_id=client, recovery_state="in_flight")
        return {**response, "idempotentReplay": True}

    def register_prompt(self, source_id: str, target_id: str, request: Mapping[str, object]) -> None:
        client = str(request.get("clientMessageId") or "").strip()
        if not client or request.get("delivery", "prompt") != "prompt":
            # Steer/follow-up have different input ownership and are not P1.
            return
        digest = hashlib.sha256(json.dumps(dict(request), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        retry = str(request.get("retryOfClientMessageId") or "").strip()
        now = _now()
        with self.sessions._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            link = conn.execute("SELECT * FROM agent_coordinator_objects WHERE target_id=? AND source_session_id=? AND target_kind='session'",
                                (target_id, source_id)).fetchone()
            if link is None:
                raise ValueError("target is not controlled by this Agent")
            require_binding_source(conn, {"coordinatorId": link["coordinator_id"], "sourceSessionId": source_id})
            if self.sessions._get(conn, target_id)["status"] == "archived":
                raise ValueError("archived Session cannot register work")
            old = conn.execute("SELECT a.*, w.source_session_id, w.coordinator_id FROM agent_coordinator_work_attempts a "
                               "JOIN agent_coordinator_work w ON w.work_id=a.work_id WHERE a.target_session_id=? AND a.client_message_id=?",
                               (target_id, client)).fetchone()
            if old is not None:
                if old["request_sha256"] != digest or old["source_session_id"] != source_id or old["coordinator_id"] != link["coordinator_id"]:
                    raise ValueError("clientMessageId belongs to a different coordinator attempt")
                return
            previous = conn.execute("SELECT w.* FROM agent_coordinator_work_attempts a JOIN agent_coordinator_work w ON w.work_id=a.work_id "
                                    "WHERE a.target_session_id=? AND a.client_message_id=? AND w.source_session_id=? AND w.coordinator_id=?",
                                    (target_id, retry, source_id, link["coordinator_id"])).fetchone() if retry else None
            work_id = str(previous["work_id"]) if previous is not None else f"coordinator-work:{uuid.uuid4()}"
            if previous is None:
                conn.execute("INSERT INTO agent_coordinator_work VALUES (?, ?, ?, ?, ?, ?)",
                             (work_id, link["coordinator_id"], source_id, target_id, link["task"], now))
            conn.execute("INSERT INTO agent_coordinator_work_attempts(attempt_id, work_id, target_session_id, client_message_id, "
                         "request_sha256, retry_of_client_message_id, created_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (f"coordinator-attempt:{uuid.uuid4()}", work_id, target_id, client, digest, retry, now))

    def reconcile_acceptance(self, target_id: str, client: str) -> str:
        client = client.strip()
        acceptance = self.receipts.acceptance_evidence_for_exact_command(
            command_scope="session_prompt", scope_id=target_id, client_message_id=client)
        if acceptance is None:
            acceptance = self.sessions.prompt_acceptance_evidence(target_id, client)
        if not acceptance:
            return ""
        turn_id = str(acceptance.get("turnId") or "")
        if not turn_id or acceptance.get("clientMessageId") != client:
            raise ValueError("original coordinator acceptance has no exact identity")
        with self.sessions._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT turn_id FROM agent_coordinator_work_attempts WHERE target_session_id=? AND client_message_id=?",
                               (target_id, client)).fetchone()
            if row is None:
                return ""
            if row["turn_id"] and row["turn_id"] != turn_id:
                raise ValueError("original coordinator attempt belongs to a different turn")
            conn.execute("UPDATE agent_coordinator_work_attempts SET turn_id=?, acceptance_json=? WHERE target_session_id=? AND client_message_id=?",
                         (turn_id, json.dumps(acceptance, ensure_ascii=False, sort_keys=True), target_id, client))
        return turn_id

    def attempts_for_source(self, source_id: str, *, limit: int = 100) -> list[dict[str, object]]:
        with self.sessions._read_connect() as conn:
            rows = conn.execute("SELECT a.* FROM agent_coordinator_work_attempts a JOIN agent_coordinator_work w ON w.work_id=a.work_id WHERE w.source_session_id=? ORDER BY a.created_at_ms,a.attempt_id LIMIT ?",
                                (source_id, _limit(limit))).fetchall()
            room_rows = conn.execute("SELECT * FROM agent_coordinator_room_work_attempts WHERE source_session_id=? ORDER BY created_at_ms,attempt_id LIMIT ?",
                                     (source_id, _limit(limit))).fetchall()
        selected = sorted([(row, "session") for row in rows] + [(row, "room") for row in room_rows],
                          key=lambda entry: (entry[0]["created_at_ms"], entry[0]["attempt_id"]))[:limit]
        return [{"attemptId": row["attempt_id"], "workId": row["work_id"],
                 **({"targetSessionId": row["target_session_id"], "turnId": row["turn_id"]} if kind == "session" else
                    {"targetRoomId": row["target_room_id"], "roomTurnId": row["root_id"]}),
                 "clientMessageId": row["client_message_id"], "contextItemId": row["context_item_id"],
                 "projectedAtMs": row["projected_at_ms"], "retiredReason": row["retired_reason"],
                 "lastErrorCode": row["last_error_code"]} for row, kind in selected]

    def reconcile_once(self, *, limit: int = 20) -> int:
        with self.sessions._read_connect() as conn:
            # Both typed lanes share the existing bounded scheduler tick;
            # unresolved Session commands cannot starve original Room evidence.
            candidates = conn.execute("SELECT attempt_id,checked_at_ms,created_at_ms,'session' AS kind FROM agent_coordinator_work_attempts WHERE projected_at_ms IS NULL AND retired_reason='' "
                "UNION ALL SELECT attempt_id,checked_at_ms,created_at_ms,'room' FROM agent_coordinator_room_work_attempts WHERE projected_at_ms IS NULL AND retired_reason='' "
                "ORDER BY checked_at_ms,created_at_ms,attempt_id LIMIT ?", (_limit(limit),)).fetchall()
            rows = [(candidate["kind"], conn.execute(
                "SELECT * FROM agent_coordinator_room_work_attempts WHERE attempt_id=?" if candidate["kind"] == "room" else
                "SELECT a.*,w.coordinator_id,w.source_session_id FROM agent_coordinator_work_attempts a JOIN agent_coordinator_work w ON w.work_id=a.work_id WHERE a.attempt_id=?",
                (candidate["attempt_id"],)).fetchone()) for candidate in candidates]
        projected = 0
        for kind, row in rows:
            try:
                projected += self._reconcile_room(row) if kind == "room" else self._reconcile(row)
            except Exception as error:
                # Unknown acceptance stays pending; this owner never dispatches.
                table = "agent_coordinator_room_work_attempts" if kind == "room" else "agent_coordinator_work_attempts"
                with self.sessions._connect() as conn:
                    conn.execute(f"UPDATE {table} SET checked_at_ms=?,last_error_code=? WHERE attempt_id=? AND projected_at_ms IS NULL",
                                 (_now(), type(error).__name__[:80], row["attempt_id"]))
        return projected

    def _reconcile_room(self, row: sqlite3.Row) -> int:
        binding = {**dict(row), "target_kind": "room"}
        with self.sessions._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self._active_link(conn, binding):
                conn.execute("UPDATE agent_coordinator_room_work_attempts SET retired_reason='ownership_retired',checked_at_ms=? WHERE attempt_id=? AND projected_at_ms IS NULL",
                             (_now(), row["attempt_id"]))
                return 0
            conn.execute("UPDATE agent_coordinator_room_work_attempts SET checked_at_ms=? WHERE attempt_id=?", (_now(), row["attempt_id"]))
        root = self.reconcile_room_acceptance(row["target_room_id"], row["client_message_id"])
        if not root:
            return 0
        result = self.read_room_result(row["target_room_id"], root)
        if (result.get("execution") != {"roomId": row["target_room_id"], "roomTurnId": root}
            or result.get("state") not in {"completed", "failed", "aborted", "cancelled"}
            or not result.get("acceptanceRef") or not any(
                ref.get("turnId") == root and (
                    (ref.get("finalizationId") and not ref.get("participantId") and not ref.get("sourceSessionId"))
                    or (result.get("state") == "completed" and ref.get("eventType") == "room_post"
                        and ref.get("rootDisposition") == "result" and ref.get("status") == "completed"
                        and ref.get("postId") and ref.get("participantId") and ref.get("sourceSessionId")))
                for ref in result.get("terminalRefs", []) if isinstance(ref, Mapping))):
            return 0
        with self.sessions._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            fresh = conn.execute("SELECT * FROM agent_coordinator_room_work_attempts WHERE attempt_id=?", (row["attempt_id"],)).fetchone()
            if fresh is None or fresh["projected_at_ms"] is not None or fresh["retired_reason"]:
                return 0
            if not self._active_link(conn, binding):
                conn.execute("UPDATE agent_coordinator_room_work_attempts SET retired_reason='ownership_retired' WHERE attempt_id=?", (row["attempt_id"],))
                return 0
            item = self.context.enqueue(session_id=row["source_session_id"], source_kind="coordinator_result",
                source_id=row["attempt_id"], lane="result", lifecycle="until_ack", dedupe_key=f"coordinator-result:{row['attempt_id']}",
                title="原工作结果待核对", summary="原 Room Root 结果证据；尚未由星伴验收",
                payload={"schemaVersion": "rag-ime.coordinator-room-work-result-context.v1", "workId": row["work_id"],
                    "attemptId": row["attempt_id"], "coordinatorId": row["coordinator_id"], "sourceSessionId": row["source_session_id"],
                    "targetRoomId": row["target_room_id"], "roomTurnId": root, "authority": "evidence_only",
                    "result": {**result, "evidenceOnly": True}}, _connection=conn)
            conn.execute("UPDATE agent_coordinator_room_work_attempts SET terminal_refs_json=?,context_item_id=?,projected_at_ms=?,last_error_code='' WHERE attempt_id=?",
                         (json.dumps(result["terminalRefs"], sort_keys=True), item["itemId"], _now(), row["attempt_id"]))
        return 1

    def _reconcile(self, row: sqlite3.Row) -> int:
        with self.sessions._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self._active_link(conn, row):
                conn.execute("UPDATE agent_coordinator_work_attempts SET retired_reason='ownership_retired', checked_at_ms=? "
                             "WHERE attempt_id=? AND projected_at_ms IS NULL", (_now(), row["attempt_id"]))
                return 0
            conn.execute("UPDATE agent_coordinator_work_attempts SET checked_at_ms=? WHERE attempt_id=?", (_now(), row["attempt_id"]))
        turn = self.reconcile_acceptance(str(row["target_session_id"]), str(row["client_message_id"]))
        if not turn or self.sessions.runtime_turn_terminal_event(str(row["target_session_id"]), turn) is None:
            return 0
        result = self.read_result(str(row["target_session_id"]), turn, str(row["client_message_id"]))
        if not result.get("terminalRefs") or result.get("messagesUnavailable"):
            return 0
        with self.sessions._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            fresh = conn.execute("SELECT * FROM agent_coordinator_work_attempts WHERE attempt_id=?", (row["attempt_id"],)).fetchone()
            if fresh is None or fresh["projected_at_ms"] is not None or fresh["retired_reason"]:
                return 0
            if not self._active_link(conn, row):
                conn.execute("UPDATE agent_coordinator_work_attempts SET retired_reason='ownership_retired' WHERE attempt_id=?", (row["attempt_id"],))
                return 0
            item = self.context.enqueue(session_id=str(row["source_session_id"]), source_kind="coordinator_result",
                source_id=str(row["attempt_id"]), lane="result", lifecycle="until_ack",
                dedupe_key=f"coordinator-result:{row['attempt_id']}", title="原工作结果待核对",
                summary="原 Session 结果证据；尚未由星伴验收", payload={"schemaVersion": "rag-ime.coordinator-work-result-context.v1",
                    "workId": row["work_id"], "attemptId": row["attempt_id"], "coordinatorId": row["coordinator_id"],
                    "sourceSessionId": row["source_session_id"], "targetSessionId": row["target_session_id"],
                    "authority": "evidence_only", "result": {**result, "evidenceOnly": True}}, _connection=conn)
            conn.execute("UPDATE agent_coordinator_work_attempts SET terminal_refs_json=?, context_item_id=?, projected_at_ms=?, last_error_code='' WHERE attempt_id=?",
                         (json.dumps(result["terminalRefs"], ensure_ascii=False, sort_keys=True), item["itemId"], _now(), row["attempt_id"]))
        return 1

    @staticmethod
    def _active_link(conn: sqlite3.Connection, row: Mapping[str, object] | sqlite3.Row) -> bool:
        # Recheck at the atomic inbox write: retirement may happen during a
        # passive history read. Neither original nor replacement IDs grant it.
        if "target_kind" in row.keys() and row["target_kind"] == "room":
            return conn.execute("SELECT 1 FROM agent_coordinators c JOIN agent_sessions s ON s.id=c.session_id "
                "JOIN agent_coordinator_objects o ON o.coordinator_id=c.coordinator_id AND o.source_session_id=c.session_id "
                "JOIN agent_rooms r ON r.id=o.target_id WHERE c.coordinator_id=? AND c.session_id=? AND o.target_kind='room' AND o.target_id=? "
                "AND s.status<>'archived' AND r.status<>'archived'",
                (row["coordinator_id"], row["source_session_id"], row["target_room_id"])).fetchone() is not None
        return conn.execute("SELECT 1 FROM agent_coordinators c JOIN agent_sessions s ON s.id=c.session_id "
                            "JOIN agent_coordinator_objects o ON o.coordinator_id=c.coordinator_id AND o.source_session_id=c.session_id "
                            "JOIN agent_sessions t ON t.id=o.target_id WHERE c.coordinator_id=? AND c.session_id=? "
                            "AND o.target_kind='session' AND o.target_id=? AND s.status<>'archived' AND t.status<>'archived'",
                            (row["coordinator_id"], row["source_session_id"], row["target_session_id"])).fetchone() is not None


def _limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise ValueError("passive work limit must be an integer between 1 and 100")
    return value


def _now() -> int:
    return int(time.time() * 1000)
