"""Causal object ownership around ordinary Pi Sessions and lightweight Rooms.

Creation is passive. Commands call the existing owners; this module never
runs a model loop, schedules work, or claims a terminal result from an ACK.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from threading import RLock
from typing import TYPE_CHECKING, Protocol

from .agent_tool_ids import DANGEROUS_AUTO_APPROVE_TOOL_PROFILE

if TYPE_CHECKING:
    from .agent_session_application import AgentSessionApplicationService
    from .agent_sessions import AgentSessionStore
    from .rooms.store import AgentRoomStore


class CoordinatorRuntime(Protocol):
    """Only the existing Runtime's exact-source ownership fences are needed."""

    def gateway_turn_effect_fence(
        self, session_id: str, binding: Mapping[str, object], *, observe: bool = True,
    ) -> AbstractContextManager[None]: ...

    def gateway_control_scope(
        self, source_session_id: str, binding: Mapping[str, object] | None,
    ) -> AbstractContextManager[None]: ...

    def is_gateway_turn_active(
        self, session_id: str, turn_id: str, *, client_message_id: str,
    ) -> bool: ...


class CreateCoordinatorRoom(Protocol):
    def __call__(
        self, payload: Mapping[str, object], *,
        coordinator_binding: Mapping[str, object] | None = None,
        inherited_model_selection: Mapping[str, object] | None = None,
        effect_fence: Callable[[], AbstractContextManager[None]] | None = None,
    ) -> dict[str, object]: ...


class ActivateCoordinatorRoom(Protocol):
    def __call__(
        self, room_id: str, *, room: Mapping[str, object] | None = None,
    ) -> int: ...


@dataclass(frozen=True)
class CoordinatorPorts:
    """Existing owners selected by the composition root; no facade or new owner."""

    sessions: AgentSessionStore
    rooms: AgentRoomStore
    session_application: AgentSessionApplicationService
    runtime: CoordinatorRuntime
    create_room: CreateCoordinatorRoom
    activate_room: ActivateCoordinatorRoom
    room_artifacts: Callable[[str, Mapping[str, object]], dict[str, object]]
    require_mutable: Callable[[str], dict[str, object]]
    prompt: Callable[[str, Mapping[str, object]], dict[str, object]]
    abort: Callable[[str, Mapping[str, object]], dict[str, object]]
    resume: Callable[[str, Mapping[str, object]], dict[str, object]]
    post_room: Callable[[str, Mapping[str, object]], dict[str, object]]
    abort_room: Callable[[str, Mapping[str, object]], dict[str, object]]
    session_result: Callable[[str, str, str], dict[str, object]]


_LOCKS: dict[str, RLock] = {}
_LOCKS_GUARD = RLock()


def _text(value: object, key: str, maximum: int = 240) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{key} requires a non-empty string of at most {maximum} characters")
    return value.strip()


def coordinator_identity(sessions: AgentSessionStore, session_id: str) -> str:
    with sessions._read_connect() as conn:
        row = conn.execute("SELECT coordinator_id FROM agent_coordinators WHERE session_id = ?", (session_id,)).fetchone()
    if row is None or sessions.get(session_id)["status"] == "archived":
        raise ValueError("sourceSessionId must identify the active persistent Agent")
    return str(row[0])


def _session_payload(title: str, *, runtime_engine: str = "classic") -> dict[str, object]:
    return {"title": title, "runtimeEngine": runtime_engine, "mode": "coordinator",
            "executionMode": "full_trust", "toolProfileVersion": DANGEROUS_AUTO_APPROVE_TOOL_PROFILE,
            "workspaceRoots": [], "projectContextEnabled": False}


def ensure_coordinator(ports: CoordinatorPorts, payload: Mapping[str, object]) -> dict[str, object]:
    if payload:
        raise ValueError("global Agent ensure accepts no workspace or permission overrides")
    with ports.sessions._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM agent_coordinators WHERE singleton = 1").fetchone()
        if row is None:
            session = ports.session_application._create_session_record(_session_payload("星伴", runtime_engine="durable"), connection=conn)
            identity = f"coordinator:{uuid.uuid4()}"
            conn.execute("INSERT INTO agent_coordinators VALUES (1, ?, ?, ?)", (identity, session["id"], int(time.time()*1000)))
            created = True
        else:
            identity = str(row["coordinator_id"])
            try:
                session = ports.sessions._get(conn, str(row["session_id"]))
            except KeyError:
                session = {"status": "archived"}
            if session["status"] == "archived":
                # An explicit archive retires the old coordinator and its scope.
                session = ports.session_application._create_session_record(_session_payload("星伴", runtime_engine="durable"), connection=conn)
                identity = f"coordinator:{uuid.uuid4()}"
                conn.execute("UPDATE agent_coordinators SET coordinator_id=?, session_id=?, created_at_ms=? WHERE singleton=1", (identity, session["id"], int(time.time()*1000)))
                created = True
            else:
                created = False
    return {"schemaVersion": "rag-ime.agent-coordinator-ensure.v1", "ok": True, "created": created, "coordinatorId": identity,
            "sourceSessionId": session["id"], "session": session,
            "objects": owned_objects(ports, str(session["id"]))}


def owned_objects(ports: CoordinatorPorts, source_id: str) -> list[dict[str, object]]:
    identity = coordinator_identity(ports.sessions, source_id)
    with ports.sessions._read_connect() as conn:
        links = conn.execute("SELECT * FROM agent_coordinator_objects WHERE coordinator_id=? AND source_session_id=? ORDER BY created_at_ms DESC, target_id DESC LIMIT 100", (identity, source_id)).fetchall()
    objects = []
    for link in links:
        try:
            target = (ports.sessions.get(str(link["target_id"])) if link["target_kind"] == "session"
                      else ports.rooms.get(str(link["target_id"])))
        except KeyError:
            continue
        if target.get("status") == "archived":
            continue
        outputs: list[dict[str, object]] = []
        if link["target_kind"] == "session":
            goal = ports.sessions.agent_goal(str(link["target_id"]))
            audit = goal.get("completionAudit")
            if goal.get("status") == "completed" and isinstance(audit, Mapping):
                outputs = [{"reference": str(item.get("reference") or "")[:1000],
                            "title": str(item.get("summary") or item.get("reference") or "")[:160],
                            "sessionId": str(link["target_id"])}
                           for item in list(audit.get("evidence") or [])[:4] if isinstance(item, Mapping) and item.get("reference")]
        else:
            participants = {item["id"]: item["sessionId"] for item in target.get("participants", []) if isinstance(item, Mapping)}
            outputs = [{"reference": item["path"], "title": item["displayName"],
                        "sessionId": participants.get(item.get("createdByParticipantId"), "")}
                       for item in ports.room_artifacts(str(link["target_id"]), {"limit": 4})["items"]]
        objects.append({"kind": link["target_kind"], "id": link["target_id"],
                        "sourceSessionId": source_id, "coordinatorId": identity,
                        "task": link["task"], "createdAtMs": link["created_at_ms"], "target": target, "outputs": outputs})
    return objects


def coordinator_command(ports: CoordinatorPorts, payload: Mapping[str, object], *, execution_binding: Mapping[str, object] | None = None) -> dict[str, object]:
    if set(payload) - {"sourceSessionId", "action", "clientRequestId", "targetId", "input"}:
        raise ValueError("unsupported Agent command field")
    source_id = _text(payload.get("sourceSessionId"), "sourceSessionId")
    identity = coordinator_identity(ports.sessions, source_id)
    effect_fence = (lambda: ports.runtime.gateway_turn_effect_fence(source_id, execution_binding)) if execution_binding is not None else nullcontext
    action = _text(payload.get("action"), "action", 40)
    input_value = payload.get("input", {})
    if not isinstance(input_value, Mapping):
        raise ValueError("input must be an object")
    if action == "read":
        if input_value:
            raise ValueError("read accepts no input")
        return {"schemaVersion": "rag-ime.agent-coordinator-read.v1", "ok": True, "coordinatorId": identity, "sourceSessionId": source_id,
                "objects": owned_objects(ports, source_id)}
    if action == "read_result":
        target_id = _text(payload.get("targetId"), "targetId")
        with ports.sessions._read_connect() as conn:
            link = conn.execute("SELECT target_kind FROM agent_coordinator_objects WHERE target_id=? AND coordinator_id=? AND source_session_id=?", (target_id, identity, source_id)).fetchone()
        if link is None:
            raise ValueError("target is not controlled by this Agent")
        kind = str(link[0])
        if kind == "session":
            if set(input_value) != {"turnId", "clientMessageId"}:
                raise ValueError("read_result requires the exact turnId and clientMessageId")
            result = ports.session_result(target_id, _text(input_value.get("turnId"), "turnId"),
                                          _text(input_value.get("clientMessageId"), "clientMessageId"))
        else:
            if set(input_value) != {"roomTurnId"}:
                raise ValueError("read_result requires the exact roomTurnId")
            result = _room_result(ports, target_id, _text(input_value.get("roomTurnId"), "roomTurnId"))
        return {"schemaVersion": "rag-ime.agent-coordinator-result.v1", "ok": True,
                "evidenceOnly": True, "coordinatorId": identity, "sourceSessionId": source_id,
                "targetId": target_id, "kind": kind, **result}
    if action in {"create_session", "create_room", "prompt", "resume"} and ports.sessions.get(source_id).get("executionMode") != "full_trust":
        raise ValueError("Agent execution permissions changed; full-trust control is no longer authorized")
    if action in {"create_session", "create_room"}:
        request_id = _text(payload.get("clientRequestId"), "clientRequestId")
        task = _text(input_value.get("task"), "task", 4000)
        kind = "session" if action == "create_session" else "room"
        allowed_creation = {"title", "task", "participants", "routingPolicy"}
        if kind == "session":
            allowed_creation.add("purpose")
        if set(input_value) - allowed_creation:
            raise ValueError("creation input contains unsupported fields")
        capture = "purpose" in input_value
        if capture and input_value["purpose"] != "screen_capture":
            raise ValueError("Session purpose must be screen_capture when provided")
        title = _text(input_value.get("title", task[:120]), "title", 120)
        if kind == "session" and set(input_value) & {"participants", "routingPolicy"}:
            raise ValueError("Session creation does not accept Room fields")
        fingerprint = hashlib.sha256(json.dumps({"sourceSessionId": source_id, "action": action, "input": dict(input_value)}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with _LOCKS_GUARD:
            lock = _LOCKS.setdefault(str(ports.sessions.db_path.resolve()), RLock())
        with lock:
            with ports.sessions._read_connect() as conn:
                existing = conn.execute("SELECT * FROM agent_coordinator_objects WHERE client_request_id=?", (request_id,)).fetchone()
            if existing is not None:
                if existing["request_sha256"] != fingerprint or existing["source_session_id"] != source_id:
                    raise ValueError("clientRequestId is bound to a different Agent creation")
                try:
                    target = ports.sessions.get(str(existing["target_id"])) if kind == "session" else ports.rooms.get(str(existing["target_id"]))
                except KeyError as error:
                    raise ValueError("created target was deleted; this creation request is retired") from error
                return {"schemaVersion": "rag-ime.agent-coordinator-create.v1", "ok": True, "created": False, "kind": kind, "target": target, "sourceSessionId": source_id, "coordinatorId": identity, "clientRequestId": request_id}
            binding = {"coordinatorId": identity, "sourceSessionId": source_id, "clientRequestId": request_id,
                       "requestSha256": fingerprint, "task": task}
            if kind == "session":
                with effect_fence(), ports.sessions._connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    require_binding_source(conn, binding)
                    repeated = conn.execute("SELECT * FROM agent_coordinator_objects WHERE client_request_id=?", (request_id,)).fetchone()
                    if repeated is not None:
                        if repeated["request_sha256"] != fingerprint or repeated["source_session_id"] != source_id:
                            raise ValueError("clientRequestId is bound to a different Agent creation")
                        return {"schemaVersion": "rag-ime.agent-coordinator-create.v1", "ok": True, "created": False, "kind": kind, "target": ports.sessions._get(conn, str(repeated["target_id"])), "sourceSessionId": source_id, "coordinatorId": identity, "clientRequestId": request_id}
                    source = ports.sessions._get(conn, source_id)
                    target_payload = (
                        {"title": title, "runtimeEngine": "classic", "mode": "assistant",
                         "executionMode": "per_action", "workspaceRoots": [], "projectContextEnabled": False}
                        if capture else _session_payload(title)
                    )
                    session = ports.session_application._create_session_record(target_payload, connection=conn,
                        inherited_model_selection={"modelProfile": source["modelProfile"], "thinkingLevel": source.get("thinkingLevel", "")})
                    insert_binding(conn, str(session["id"]), "session", binding)
                    if not capture:
                        ports.sessions.mutate_agent_goal(str(session["id"]), {"action": "confirm_setup", "confirmed": True,
                            "expectedRevision": 0, "objective": task, "successCriteria": ""}, _connection=conn)
                target = ports.sessions.get(str(session["id"]))
            else:
                participants = input_value.get("participants")
                if not isinstance(participants, list) or not 2 <= len(participants) <= 8:
                    raise ValueError("Room creation requires 2 to 8 configured participants")
                source = ports.sessions.get(source_id)
                target = ports.create_room({"title": title,
                    "scenarioPrompt": task, "participants": participants,
                    "routingPolicy": input_value.get("routingPolicy", "jev"),
                    "workspaceRoots": [], "executionMode": "full_trust"}, coordinator_binding=binding,
                    inherited_model_selection={key: source.get(key, "") for key in ("modelProfile", "thinkingLevel")}, effect_fence=effect_fence)["room"]
                ports.activate_room(str(target["id"]), room=target)
            return {"schemaVersion": "rag-ime.agent-coordinator-create.v1", "ok": True, "created": True, "kind": kind, "target": target, "sourceSessionId": source_id, "coordinatorId": identity, "clientRequestId": request_id}
    if execution_binding is not None and not ports.runtime.is_gateway_turn_active(source_id, str(execution_binding.get("turnId") or ""), client_message_id=str(execution_binding.get("clientMessageId") or "")):
        raise ValueError("control command belongs to an inactive Runtime turn")
    target_id = _text(payload.get("targetId"), "targetId")
    with ports.sessions._read_connect() as conn:
        link = conn.execute("SELECT target_kind FROM agent_coordinator_objects WHERE target_id=? AND coordinator_id=? AND source_session_id=?", (target_id, identity, source_id)).fetchone()
    if link is None:
        raise ValueError("target is not controlled by this Agent")
    kind = str(link[0])
    allowed = ({"message", "clientMessageId", "delivery", "retryOfClientMessageId"} if action == "prompt" and kind == "session"
               else {"message", "clientMessageId", "topicId"} if action == "prompt"
               else {"turnId", "clientMessageId"} if kind == "session"
               else {"roomTurnId", "clientRequestId"})
    if set(input_value) - allowed:
        raise ValueError("control input contains unsupported fields")
    if action == "prompt":
        _text(input_value.get("message"), "message", 8000)
        _text(input_value.get("clientMessageId"), "clientMessageId")
    if kind == "session":
        ports.require_mutable(target_id)
        if action == "prompt":
            with ports.runtime.gateway_control_scope(source_id, execution_binding):
                return ports.prompt(target_id, input_value)
        if action == "stop":
            if set(input_value) != {"turnId", "clientMessageId"}:
                raise ValueError("Stop requires the exact turnId and clientMessageId")
            return ports.abort(target_id, {"turnTarget": {key: _text(input_value.get(key), key) for key in ("turnId", "clientMessageId")}})
        if action == "resume":
            with ports.runtime.gateway_control_scope(source_id, execution_binding):
                return ports.resume(target_id, input_value)
    else:
        if action == "prompt":
            with ports.runtime.gateway_control_scope(source_id, execution_binding):
                return ports.post_room(target_id, input_value)
        if action == "stop":
            return ports.abort_room(target_id, input_value)
    raise ValueError("unsupported Agent control action for this target")


def insert_binding(conn: sqlite3.Connection, target_id: str, kind: str, binding: Mapping[str, object]) -> None:
    # Room and its causal relation are committed together, before public events.
    require_binding_source(conn, binding)
    conn.execute("INSERT INTO agent_coordinator_objects VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (target_id, kind, binding["coordinatorId"], binding["sourceSessionId"], binding["clientRequestId"],
         binding["requestSha256"], binding["task"], int(time.time()*1000)))


def _room_result(ports: CoordinatorPorts, room_id: str, turn_id: str) -> dict[str, object]:
    # These are original public event receipts, not the Room's latest snapshot.
    # A participant terminal proves only that participant's execution ended.
    events = ports.rooms.list_events_for_turn(room_id, turn_id, limit=100)
    terminals = []
    messages = []
    acceptance = None
    for event in events:
        reference = {key: event.get(key) for key in
                     ("eventId", "sequence", "eventType", "turnId", "participantId", "sourceSessionId", "createdAtMs")}
        payload = event.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        if event.get("eventType") == "user_message" and acceptance is None:
            acceptance = reference
        if event.get("eventType") in {"turn_completed", "turn_failed"}:
            terminals.append({**reference, "status": str(payload.get("status") or "")[:80]})
        if event.get("eventType") in {"participant_message", "room_post"}:
            message = payload.get("message")
            message = message if isinstance(message, Mapping) else {}
            if message.get("role") and message.get("role") != "assistant":
                continue
            text = str(payload.get("text") or message.get("text") or "")
            if not text:
                text = "\n".join(str(data.get("text") or data.get("markdown") or data.get("code") or "")
                                 for block in message.get("blocks", []) if isinstance(block, Mapping)
                                 and block.get("type") in {"text", "code"}
                                 for data in [block.get("data")] if isinstance(data, Mapping))
            if text:
                messages.append({**reference, "messageId": str(message.get("id") or ""),
                                 "text": text[:8000], "truncated": len(text) > 8000})
    remaining = 8000
    selected = messages[-8:]
    for message in reversed(selected):
        text = str(message["text"])
        message["text"] = text[:remaining]
        message["truncated"] = bool(message["truncated"]) or len(text) > remaining
        remaining -= len(str(message["text"]))
    return {"execution": {"roomId": room_id, "roomTurnId": turn_id},
            "state": "unknown", "reason": "room_root_settlement_not_projected" if events else "original_turn_evidence_unavailable",
            "acceptanceRef": acceptance, "terminalRefs": terminals[:16], "finalMessages": selected, "artifacts": [],
            "truncated": len(events) == 100 or len(terminals) > 16 or len(messages) > 8 or any(message["truncated"] for message in selected)}


def require_binding_source(conn: sqlite3.Connection, binding: Mapping[str, object]) -> None:
    row = conn.execute("SELECT s.status, s.execution_mode FROM agent_coordinators c JOIN agent_sessions s ON s.id=c.session_id WHERE c.session_id=? AND c.coordinator_id=?", (binding["sourceSessionId"], binding["coordinatorId"])).fetchone()
    if row is None or row[0] == "archived":
        raise ValueError("sourceSessionId must identify the active persistent Agent")
    if row[1] != "full_trust":
        raise ValueError("Agent execution permissions changed; full-trust creation is no longer authorized")
