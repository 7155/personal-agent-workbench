from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import queue
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from rag_ime.agent_definitions import canonical_collaboration_role_id
from rag_ime.agent_role_identity import canonical_agent_role_id
from rag_ime.rooms.routing import (
    normalize_room_kind,
    normalize_routing_config,
    normalize_routing_policy,
    plan_room_route,
    plan_room_routes,
)
from rag_ime.rooms.work import work_item_payload
from rag_ime.contracts.json_schema import validate_contract
from rag_ime.db import apply_database_migrations
from rag_ime.room_permission_policy import (
    normalize_room_permission_policy,
    room_permission_policy_columns,
    room_permission_policy_from_row,
)


ROOM_EVENT_TYPES = frozenset(
    {
        "user_message",
        "route_decision",
        "participant_status",
        "participant_delta",
        "participant_activity",
        "participant_message",
        "room_post",
        "room_config_changed",
        "topic_changed",
        "artifact_changed",
        "turn_completed",
        "turn_failed",
        "snapshot_required",
        "room_start_confirmation_required",
        "room_start_confirmation_confirmed",
        "room_start_confirmation_rejected",
    }
)

ROOM_SNAPSHOT_EVENT_LIMIT = 200
ROOM_CONVERSATION_EVENT_LIMIT = 2_000
ROOM_HISTORY_PAGE_LIMIT = 200
MAX_ACTIVE_ROOM_PARTICIPANTS = 8
MAX_ROOM_WORKSPACE_ROOTS = 5
_ROOM_OWNER_APP_ID_RE = re.compile(r"^extension:[a-z0-9][a-z0-9-]{0,63}$")
_ROOM_SURFACE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
ROOM_COLLABORATION_ROLES = frozenset(
    {
        "coordinator",
        "researcher",
        "implementer",
        "reviewer",
        "specialist",
    }
)
# `specialist` stays readable so historical participants keep a valid role, but
# it is no longer offered: it carries no domain contract, so assigning it would
# promise expertise the runtime cannot supply. The control center already shows
# it disabled; this keeps a direct API call from doing what the UI refuses.
ASSIGNABLE_COLLABORATION_ROLES = ROOM_COLLABORATION_ROLES - {"specialist"}


class AgentRoomNotFound(KeyError):
    pass


class AgentParticipantNotFound(KeyError):
    pass


class AgentRoomStore:
    """Persistent cross-session room timeline with a JSONL audit mirror."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        room_dir: str | Path | None = None,
        persistent_reads: bool = False,
    ):
        self.db_path = Path(db_path)
        self.room_dir = Path(room_dir) if room_dir is not None else self.db_path.parent / "Agent" / "rooms"
        self._persistent_reads = bool(persistent_reads)
        self._read_lock = threading.RLock()
        self._read_connection: sqlite3.Connection | None = None

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.room_dir.mkdir(parents=True, exist_ok=True)
        if self._persistent_reads:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            try:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys = ON")
                apply_database_migrations(conn)
                conn.commit()
            except BaseException:
                conn.close()
                raise
            with self._read_lock:
                previous = self._read_connection
                self._read_connection = conn
            if previous is not None:
                previous.close()
            return
        with self._connect() as conn:
            apply_database_migrations(conn)

    def create(
        self,
        *,
        title: str,
        routing_policy: str,
        participants: Sequence[Mapping[str, object]],
        workspace_roots: Sequence[str] = (),
        moderator_ordinal: int = 0,
        room_kind: str = "collaboration",
        avatar: str = "members",
        description: str = "",
        scenario_prompt: str = "",
        routing_config: Mapping[str, object] | None = None,
        permission_policy: Mapping[str, object] | None = None,
        execution_mode: str | None = None,
        owner_app_id: str = "",
        surface_key: str = "",
        created_at_ms: int | None = None,
    ) -> dict[str, object]:
        normalized_title = " ".join(str(title).split())[:120]
        if not normalized_title:
            raise ValueError("agent room title must not be empty")
        routing = normalize_routing_policy(routing_policy)
        kind = normalize_room_kind(room_kind)
        room_policy = normalize_room_permission_policy(
            permission_policy,
            room_kind=kind,
            legacy_execution_mode=execution_mode,
        )
        permission_columns = room_permission_policy_columns(room_policy)
        config = normalize_routing_config(routing_config)
        normalized_avatar = " ".join(str(avatar or "members").split())[:80] or "members"
        normalized_description = " ".join(str(description or "").split())[:500]
        normalized_scenario = str(scenario_prompt or "").strip()[:8_000]
        normalized_owner_app_id, normalized_surface_key = _room_surface_identity(
            owner_app_id,
            surface_key,
        )
        values = [dict(item) for item in participants]
        if not 2 <= len(values) <= MAX_ACTIVE_ROOM_PARTICIPANTS:
            raise ValueError(
                "agent room requires between 2 and 8 participants"
            )
        if not 0 <= moderator_ordinal < len(values):
            raise ValueError("agent room moderator ordinal is out of range")
        roots = _workspace_roots(workspace_roots)
        if len(roots) > MAX_ROOM_WORKSPACE_ROOTS:
            raise ValueError(
                f"agent room accepts at most {MAX_ROOM_WORKSPACE_ROOTS} workspace roots"
            )

        timestamp = _timestamp(created_at_ms)
        room_id = f"room:{uuid.uuid4()}"
        topic_id = f"topic:{uuid.uuid4()}"
        participant_ids = [f"participant:{uuid.uuid4()}" for _ in values]
        moderator_id = participant_ids[moderator_ordinal]
        room_file = self._room_file(room_id)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_rooms(
                    id, title, routing_policy, moderator_participant_id, status,
                    room_file, workspace_roots_json, room_kind, avatar, description,
                    scenario_prompt, routing_mode, routing_config_json,
                    next_speaker_ordinal, active_topic_id, config_revision,
                    execution_mode, partner_execution_mode, tool_agent_execution_mode,
                    owner_app_id, surface_key, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    room_id,
                    normalized_title,
                    _legacy_policy(routing),
                    moderator_id,
                    str(room_file),
                    json.dumps(roots, ensure_ascii=False, separators=(",", ":")),
                    kind,
                    normalized_avatar,
                    normalized_description,
                    normalized_scenario,
                    routing,
                    json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    topic_id,
                    permission_columns["execution_mode"],
                    permission_columns["partner_execution_mode"],
                    permission_columns["tool_agent_execution_mode"],
                    normalized_owner_app_id,
                    normalized_surface_key,
                    timestamp,
                    timestamp,
                ),
            )
            for ordinal, (participant_id, value) in enumerate(zip(participant_ids, values, strict=True)):
                session_id = _required_text(value, "sessionId")
                role_id = canonical_agent_role_id(
                    _required_text(value, "roleId")
                )
                role_version = _required_text(value, "roleVersion")
                display_name = " ".join(_required_text(value, "displayName").split())[:40]
                collaboration_role = normalize_collaboration_role(
                    value.get("collaborationRole")
                )
                conn.execute(
                    """
                    INSERT INTO agent_room_participants(
                        id, room_id, session_id, role_id, role_version, display_name,
                        collaboration_role, collaboration_role_key,
                        participant_status, ordinal, created_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        participant_id,
                        room_id,
                        session_id,
                        role_id,
                        role_version,
                        display_name,
                        _legacy_collaboration_role(collaboration_role),
                        collaboration_role,
                        ordinal,
                        timestamp,
                    ),
                )
            conn.execute(
                """
                INSERT INTO agent_room_topics(
                    id, room_id, title, summary, topic_status, ordinal,
                    created_at_ms, updated_at_ms
                ) VALUES (?, ?, '主话题', '', 'active', 0, ?, ?)
                """,
                (topic_id, room_id, timestamp, timestamp),
            )
        return self.get(room_id)

    def get(self, room_id: str) -> dict[str, object]:
        with self._connect() as conn:
            return self._get(conn, room_id)

    @staticmethod
    def _get(
        conn: sqlite3.Connection,
        room_id: str,
    ) -> dict[str, object]:
        row = conn.execute(
            "SELECT * FROM agent_rooms WHERE id = ?",
            (room_id,),
        ).fetchone()
        if row is None:
            raise AgentRoomNotFound(room_id)
        participants = conn.execute(
            """
            SELECT p.*
            FROM agent_room_participants AS p
            WHERE room_id = ? ORDER BY ordinal ASC
            """,
            (room_id,),
        ).fetchall()
        topics = conn.execute(
            """
            SELECT * FROM agent_room_topics
            WHERE room_id = ?
            ORDER BY topic_status ASC, ordinal ASC, created_at_ms ASC
            """,
            (room_id,),
        ).fetchall()
        artifacts = conn.execute(
            """
            SELECT * FROM agent_room_artifacts
            WHERE room_id = ? AND artifact_status = 'active'
            ORDER BY updated_at_ms DESC LIMIT 100
            """,
            (room_id,),
        ).fetchall()
        work_items = conn.execute(
            """
            SELECT * FROM agent_room_work_items
            WHERE room_id = ?
            ORDER BY
                CASE state
                    WHEN 'blocked' THEN 0
                    WHEN 'review' THEN 1
                    WHEN 'active' THEN 2
                    WHEN 'queued' THEN 3
                    ELSE 4
                END,
                updated_at_ms DESC
            LIMIT 100
            """,
            (room_id,),
        ).fetchall()
        start_gate_row = conn.execute(
            "SELECT room_id, status, objective_text, work_item_id, client_message_id, root_id, confirmed_at_ms FROM agent_room_start_gates WHERE room_id = ?",
            (room_id,),
        ).fetchone()
        return _room_payload(
            row,
            participants,
            topics,
            artifacts,
            work_items,
            start_gate=_room_start_gate_payload(start_gate_row),
        )

    def list(
        self,
        *,
        include_archived: bool = False,
        limit: int = 100,
        before_updated_at_ms: int | None = None,
        before_id: str | None = None,
        owner_app_id: str | None = None,
        surface_key: str | None = None,
    ) -> list[dict[str, object]]:
        page = self.list_page(
            include_archived=include_archived,
            limit=limit,
            before_updated_at_ms=before_updated_at_ms,
            before_id=before_id,
            owner_app_id=owner_app_id,
            surface_key=surface_key,
        )
        return cast(list[dict[str, object]], page["items"])

    def list_page(
        self,
        *,
        include_archived: bool = False,
        limit: int = 100,
        before_updated_at_ms: int | None = None,
        before_id: str | None = None,
        owner_app_id: str | None = None,
        surface_key: str | None = None,
    ) -> dict[str, object]:
        """Return a keyset-paginated Room listing.

        Room ids are the deterministic tie-breaker for equal update times.
        The legacy ``list`` method continues to return only the item array.
        """
        bounded = max(1, min(int(limit), 200))
        clauses = [] if include_archived else ["status = 'active'"]
        normalized_owner = _optional_room_owner(owner_app_id)
        normalized_surface = _optional_room_surface_key(surface_key)
        if normalized_owner is not None:
            clauses.append("owner_app_id = ?")
        if normalized_surface is not None:
            clauses.append("surface_key = ?")
        normalized_before_id = str(before_id or "").strip()
        if before_updated_at_ms is not None and normalized_before_id:
            clauses.append(
                "(updated_at_ms < ? OR "
                "(updated_at_ms = ? AND id < ?))"
            )
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query_params: list[object] = []
        if normalized_owner is not None:
            query_params.append(normalized_owner)
        if normalized_surface is not None:
            query_params.append(normalized_surface)
        if before_updated_at_ms is not None and normalized_before_id:
            query_params.extend(
                [before_updated_at_ms, before_updated_at_ms, normalized_before_id]
            )
        query_params.append(bounded + 1)
        with self._read_connect() as conn:
            rows = conn.execute(
                f"SELECT id, updated_at_ms FROM agent_rooms {where} "
                "ORDER BY updated_at_ms DESC, id DESC LIMIT ?",  # noqa: S608
                query_params,
            ).fetchall()
            has_more = len(rows) > bounded
            page_rows = rows[:bounded]
            items = [self._get(conn, str(row["id"])) for row in page_rows]
        next_updated_at_ms = (
            int(page_rows[-1]["updated_at_ms"])
            if has_more and page_rows
            else None
        )
        next_id = str(page_rows[-1]["id"]) if has_more and page_rows else None
        next_cursor = (
            {
                "beforeUpdatedAtMs": next_updated_at_ms,
                "beforeId": next_id,
            }
            if has_more
            else None
        )
        return {
            "items": items,
            "hasMore": has_more,
            "nextBeforeUpdatedAtMs": next_updated_at_ms,
            "nextBeforeId": next_id,
            "nextCursor": next_cursor,
        }

    def list_directory_page(
        self,
        *,
        include_archived: bool = False,
        limit: int = 100,
        before_updated_at_ms: int | None = None,
        before_id: str | None = None,
        owner_app_id: str | None = None,
        surface_key: str | None = None,
    ) -> dict[str, object]:
        """Return the desktop Room projection without N full Room hydrations."""
        bounded = max(1, min(int(limit), 200))
        clauses = [] if include_archived else ["status = 'active'"]
        normalized_owner = _optional_room_owner(owner_app_id)
        normalized_surface = _optional_room_surface_key(surface_key)
        if normalized_owner is not None:
            clauses.append("owner_app_id = ?")
        if normalized_surface is not None:
            clauses.append("surface_key = ?")
        params: list[object] = []
        if normalized_owner is not None:
            params.append(normalized_owner)
        if normalized_surface is not None:
            params.append(normalized_surface)
        normalized_before_id = str(before_id or "").strip()
        if before_updated_at_ms is not None and normalized_before_id:
            clauses.append(
                "(updated_at_ms < ? OR (updated_at_ms = ? AND id < ?))"
            )
            params.extend(
                [before_updated_at_ms, before_updated_at_ms, normalized_before_id]
            )
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(bounded + 1)
        with self._read_connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, title, status, routing_policy, routing_mode,
                       moderator_participant_id, workspace_roots_json,
                       room_kind, execution_mode, partner_execution_mode,
                       tool_agent_execution_mode,
                       owner_app_id, surface_key, updated_at_ms
                FROM agent_rooms {where}
                ORDER BY updated_at_ms DESC, id DESC LIMIT ?
                """,  # noqa: S608 - clauses are fixed internal SQL
                params,
            ).fetchall()
            has_more = len(rows) > bounded
            page_rows = rows[:bounded]
            room_ids = [str(row["id"]) for row in page_rows]
            participants_by_room: dict[str, list[dict[str, object]]] = {
                room_id: [] for room_id in room_ids
            }
            work_by_room: dict[str, list[dict[str, object]]] = {
                room_id: [] for room_id in room_ids
            }
            if room_ids:
                placeholders = ",".join("?" for _ in room_ids)
                participant_rows = conn.execute(
                    f"""
                    SELECT id, room_id, session_id, role_id, role_version,
                           display_name, collaboration_role_key,
                           collaboration_role, participant_status, ordinal
                    FROM agent_room_participants
                    WHERE room_id IN ({placeholders})
                    ORDER BY room_id, ordinal
                    """,  # noqa: S608 - placeholders are generated, values bound
                    room_ids,
                ).fetchall()
                for participant in participant_rows:
                    participants_by_room[str(participant["room_id"])].append(
                        {
                            "schemaVersion": "rag-ime.agent-participant.v1",
                            "id": str(participant["id"]),
                            "roomId": str(participant["room_id"]),
                            "sessionId": str(participant["session_id"]),
                            "roleId": canonical_agent_role_id(participant["role_id"]),
                            "roleVersion": str(participant["role_version"]),
                            "displayName": str(participant["display_name"]),
                            "collaborationRole": str(
                                participant["collaboration_role_key"]
                                or participant["collaboration_role"]
                                or "implementer"
                            ),
                            "status": str(participant["participant_status"]),
                            "ordinal": int(participant["ordinal"]),
                        }
                    )
                work_rows = conn.execute(
                    f"""
                    SELECT room_id, state, objective, result_summary,
                           blocker_json, updated_at_ms
                    FROM (
                        SELECT room_id, state, objective, result_summary,
                               blocker_json, updated_at_ms,
                               ROW_NUMBER() OVER (
                                   PARTITION BY room_id
                                   ORDER BY CASE state
                                       WHEN 'blocked' THEN 0
                                       WHEN 'active' THEN 1
                                       WHEN 'review' THEN 2
                                       WHEN 'queued' THEN 3
                                       WHEN 'failed' THEN 4
                                       WHEN 'done' THEN 5
                                       ELSE 6
                                   END,
                                   updated_at_ms DESC
                               ) AS directory_rank
                        FROM agent_room_work_items
                        WHERE room_id IN ({placeholders})
                    )
                    WHERE directory_rank = 1
                    """,  # noqa: S608 - placeholders are generated, values bound
                    room_ids,
                ).fetchall()
                for work in work_rows:
                    blocker = json.loads(str(work["blocker_json"] or "{}"))
                    work_by_room[str(work["room_id"])].append(
                        {
                            "state": str(work["state"]),
                            "objective": str(work["objective"] or ""),
                            "resultSummary": str(work["result_summary"] or ""),
                            "blocker": dict(blocker) if isinstance(blocker, Mapping) else {},
                            "updatedAtMs": int(work["updated_at_ms"]),
                        }
                    )
        items = [
            {
                "schemaVersion": "rag-ime.agent-room-directory-entry.v1",
                "id": str(row["id"]),
                "title": str(row["title"]),
                "status": str(row["status"]),
                "routingPolicy": str(row["routing_mode"] or row["routing_policy"]),
                "executionMode": str(row["execution_mode"] or ""),
                "permissionPolicy": room_permission_policy_from_row(row),
                "moderatorParticipantId": str(row["moderator_participant_id"] or ""),
                "ownerAppId": str(row["owner_app_id"] or ""),
                "surfaceKey": str(row["surface_key"] or ""),
                "workspaceRoots": [
                    str(value)
                    for value in json.loads(str(row["workspace_roots_json"] or "[]"))
                    if str(value).strip()
                ],
                "updatedAtMs": int(row["updated_at_ms"]),
                "participants": participants_by_room[str(row["id"])],
                "workItems": work_by_room[str(row["id"])],
            }
            for row in page_rows
        ]
        next_updated_at_ms = (
            int(page_rows[-1]["updated_at_ms"])
            if has_more and page_rows
            else None
        )
        next_id = str(page_rows[-1]["id"]) if has_more and page_rows else None
        return {
            "items": items,
            "hasMore": has_more,
            "nextBeforeUpdatedAtMs": next_updated_at_ms,
            "nextBeforeId": next_id,
            "nextCursor": (
                {"beforeUpdatedAtMs": next_updated_at_ms, "beforeId": next_id}
                if has_more
                else None
            ),
        }

    def archive(self, room_id: str, *, archived: bool, updated_at_ms: int | None = None) -> dict[str, object]:
        timestamp = _timestamp(updated_at_ms)
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE agent_rooms SET status = ?, updated_at_ms = ? WHERE id = ?",
                ("archived" if archived else "active", timestamp, room_id),
            )
            if cursor.rowcount != 1:
                raise AgentRoomNotFound(room_id)
        return self.get(room_id)

    def touch_config(
        self,
        room_id: str,
        *,
        updated_at_ms: int | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, object]:
        timestamp = _timestamp(updated_at_ms)
        if connection is not None:
            self._touch_config(connection, room_id, timestamp=timestamp)
            return self._get(connection, room_id)
        with self._connect() as conn:
            self._touch_config(conn, room_id, timestamp=timestamp)
        return self.get(room_id)

    @staticmethod
    def _touch_config(
        conn: sqlite3.Connection,
        room_id: str,
        *,
        timestamp: int,
    ) -> None:
        cursor = conn.execute(
            """
            UPDATE agent_rooms
            SET config_revision = config_revision + 1, updated_at_ms = ?
            WHERE id = ?
            """,
            (timestamp, room_id),
        )
        if cursor.rowcount != 1:
            raise AgentRoomNotFound(room_id)

    def add_participant(
        self,
        room_id: str,
        *,
        session_id: str,
        role_id: str,
        role_version: str,
        display_name: str,
        collaboration_role: str = "implementer",
        created_at_ms: int | None = None,
    ) -> dict[str, object]:
        """Join a participant without replaying the Room's pre-join transcript."""

        room = self.get(room_id)
        if str(room.get("status") or "") != "active":
            raise ValueError("agent room is archived")
        active = [
            value
            for value in room.get("participants", [])
            if isinstance(value, Mapping) and value.get("status") == "active"
        ]
        if len(active) >= MAX_ACTIVE_ROOM_PARTICIPANTS:
            raise ValueError("agent room accepts at most eight active participants")
        normalized_role_id = canonical_agent_role_id(
            _required_text_value(role_id, "role_id", 63)
        )
        normalized_role_version = _required_text_value(
            role_version,
            "role_version",
            40,
        )
        if any(
            str(value.get("roleId") or "") == normalized_role_id
            and str(value.get("roleVersion") or "") == normalized_role_version
            for value in active
        ):
            raise ValueError("this role is already active in the Room")
        normalized_collaboration_role = normalize_collaboration_role(
            collaboration_role
        )
        participant_id = f"participant:{uuid.uuid4()}"
        timestamp = _timestamp(created_at_ms)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            room_row = conn.execute(
                "SELECT status FROM agent_rooms WHERE id = ?",
                (room_id,),
            ).fetchone()
            if room_row is None:
                raise AgentRoomNotFound(room_id)
            if str(room_row["status"]) != "active":
                raise ValueError("agent room is archived")
            active_count = conn.execute(
                """
                SELECT COUNT(*) FROM agent_room_participants
                WHERE room_id = ? AND participant_status = 'active'
                """,
                (room_id,),
            ).fetchone()
            if (
                int(active_count[0] if active_count is not None else 0)
                >= MAX_ACTIVE_ROOM_PARTICIPANTS
            ):
                raise ValueError(
                    "agent room accepts at most eight active participants"
                )
            duplicate = conn.execute(
                """
                SELECT 1 FROM agent_room_participants
                WHERE room_id = ? AND participant_status = 'active'
                  AND role_id = ? AND role_version = ?
                LIMIT 1
                """,
                (room_id, normalized_role_id, normalized_role_version),
            ).fetchone()
            if duplicate is not None:
                raise ValueError("this role is already active in the Room")
            ordinal_row = conn.execute(
                "SELECT COALESCE(MAX(ordinal), -1) + 1 FROM agent_room_participants WHERE room_id = ?",
                (room_id,),
            ).fetchone()
            ordinal = int(ordinal_row[0] if ordinal_row is not None else 0)
            conn.execute(
                """
                INSERT INTO agent_room_participants(
                    id, room_id, session_id, role_id, role_version, display_name,
                    collaboration_role, collaboration_role_key,
                    participant_status, ordinal, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    participant_id,
                    room_id,
                    _required_text_value(session_id, "session_id", 320),
                    normalized_role_id,
                    normalized_role_version,
                    _required_text_value(display_name, "display_name", 40),
                    _legacy_collaboration_role(
                        normalized_collaboration_role
                    ),
                    normalized_collaboration_role,
                    ordinal,
                    timestamp,
                ),
            )
            topic_rows = conn.execute(
                "SELECT id FROM agent_room_topics WHERE room_id = ?",
                (room_id,),
            ).fetchall()
            topic_ids = ["", *(str(value["id"]) for value in topic_rows)]
            for topic_id in dict.fromkeys(topic_ids):
                sequence_row = conn.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0) FROM agent_room_events
                    WHERE room_id = ? AND topic_id = ?
                    """,
                    (room_id, topic_id),
                ).fetchone()
                sequence = int(sequence_row[0] if sequence_row is not None else 0)
                conn.execute(
                    """
                    INSERT INTO agent_room_delivery_cursors(
                        room_id, participant_id, topic_id, last_sequence, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (room_id, participant_id, topic_id, sequence, timestamp),
                )
            conn.execute(
                "UPDATE agent_rooms SET updated_at_ms = ? WHERE id = ?",
                (timestamp, room_id),
            )
        return self.participant(participant_id)

    def update_participant_role(
        self,
        room_id: str,
        participant_id: str,
        collaboration_role: str,
        *,
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        """Change future dispatch responsibility without rewriting history."""

        normalized_role = normalize_collaboration_role(
            collaboration_role,
            assignable_only=True,
        )
        timestamp = _timestamp(updated_at_ms)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            room_row = conn.execute(
                "SELECT status FROM agent_rooms WHERE id = ?",
                (room_id,),
            ).fetchone()
            if room_row is None:
                raise AgentRoomNotFound(room_id)
            if str(room_row["status"]) != "active":
                raise ValueError("agent room is archived")
            participant_row = conn.execute(
                """
                SELECT room_id, participant_status
                FROM agent_room_participants
                WHERE id = ?
                """,
                (participant_id,),
            ).fetchone()
            if participant_row is None:
                raise AgentParticipantNotFound(participant_id)
            if str(participant_row["room_id"]) != room_id:
                raise ValueError(
                    "participant does not belong to this Room"
                )
            if str(participant_row["participant_status"]) != "active":
                raise ValueError("participant is not active")
            conn.execute(
                """
                UPDATE agent_room_participants
                SET collaboration_role = ?,
                    collaboration_role_key = ?
                WHERE id = ?
                """,
                (
                    _legacy_collaboration_role(normalized_role),
                    normalized_role,
                    participant_id,
                ),
            )
            conn.execute(
                "UPDATE agent_rooms SET updated_at_ms = ? WHERE id = ?",
                (timestamp, room_id),
            )
        return self.participant(participant_id)

    def remove_participant(
        self,
        room_id: str,
        participant_id: str,
        *,
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        """Soft-remove a member while preserving message and work audit identity."""

        room = self.get(room_id)
        if str(room.get("status") or "") != "active":
            raise ValueError("agent room is archived")
        participant = self.participant(participant_id)
        if str(participant.get("roomId") or "") != room_id:
            raise ValueError("participant does not belong to this Room")
        if str(participant.get("status") or "") != "active":
            return participant
        active = [
            value
            for value in room.get("participants", [])
            if isinstance(value, Mapping) and value.get("status") == "active"
        ]
        if len(active) <= 2:
            raise ValueError("agent room requires at least two active participants")
        if (
            str(room.get("routingPolicy") or "") == "moderator"
            and str(room.get("moderatorParticipantId") or "") == participant_id
        ):
            raise ValueError("change the Room moderator before removing this participant")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            room_row = conn.execute(
                """
                SELECT routing_policy, moderator_participant_id, routing_config_json
                FROM agent_rooms WHERE id = ?
                """,
                (room_id,),
            ).fetchone()
            if room_row is None:
                raise AgentRoomNotFound(room_id)
            if (
                str(room_row["routing_policy"]) == "moderator"
                and str(room_row["moderator_participant_id"] or "") == participant_id
            ):
                raise ValueError("change the Room moderator before removing this participant")
            active_count = conn.execute(
                """
                SELECT COUNT(*) FROM agent_room_participants
                WHERE room_id = ? AND participant_status = 'active'
                """,
                (room_id,),
            ).fetchone()
            if int(active_count[0] if active_count is not None else 0) <= 2:
                raise ValueError("agent room requires at least two active participants")
            open_work = conn.execute(
                """
                SELECT COUNT(*) FROM agent_room_work_items
                WHERE room_id = ? AND state IN ('queued', 'active', 'review', 'blocked')
                  AND (
                    accountable_participant_id = ?
                    OR current_owner_participant_id = ?
                    OR offered_to_participant_id = ?
                  )
                """,
                (room_id, participant_id, participant_id, participant_id),
            ).fetchone()
            if int(open_work[0] if open_work is not None else 0) > 0:
                raise ValueError(
                    "participant still owns open Room work; complete or reassign it first"
                )
            timestamp = _timestamp(updated_at_ms)
            conn.execute(
                """
                UPDATE agent_room_participants
                SET participant_status = 'removed'
                WHERE id = ? AND room_id = ?
                """,
                (participant_id, room_id),
            )
            moderator_id = str(room_row["moderator_participant_id"] or "")
            routing_config = normalize_routing_config(
                json.loads(str(room_row["routing_config_json"] or "{}"))
            )
            if moderator_id == participant_id:
                moderator_id = ""
            if str(routing_config.get("fallbackParticipantId") or "") == participant_id:
                routing_config["fallbackParticipantId"] = ""
            conn.execute(
                """
                UPDATE agent_rooms
                SET moderator_participant_id = ?, routing_config_json = ?, updated_at_ms = ?
                WHERE id = ?
                """,
                (
                    moderator_id,
                    json.dumps(
                        routing_config,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    timestamp,
                    room_id,
                ),
            )
        return self.participant(participant_id)

    def rebind_participant_session(
        self,
        room_id: str,
        participant_id: str,
        *,
        expected_session_id: str,
        session_id: str,
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        """Repair one active participant whose first-class Session was lost."""

        timestamp = _timestamp(updated_at_ms)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE agent_room_participants
                SET session_id = ?
                WHERE id = ? AND room_id = ? AND participant_status = 'active'
                  AND session_id = ?
                """,
                (
                    _required_text_value(session_id, "session_id", 320),
                    participant_id,
                    room_id,
                    expected_session_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Room participant Session changed while it was being repaired")
            conn.execute(
                "UPDATE agent_rooms SET updated_at_ms = ? WHERE id = ?",
                (timestamp, room_id),
            )
        return self.participant(participant_id)

    def delete(self, room_id: str) -> dict[str, object]:
        """Permanently delete an archived Room and its Room-owned audit graph."""

        room = self.get(room_id)
        if str(room.get("status") or "") != "archived":
            raise ValueError("archive the Room before permanently deleting it")
        session_ids = [
            str(value.get("sessionId") or "")
            for value in room.get("participants", [])
            if isinstance(value, Mapping) and str(value.get("sessionId") or "")
        ]
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            room_state = conn.execute(
                "SELECT status, room_file FROM agent_rooms WHERE id = ?",
                (room_id,),
            ).fetchone()
            if room_state is None:
                raise AgentRoomNotFound(room_id)
            if str(room_state["status"]) != "archived":
                raise ValueError("archive the Room before permanently deleting it")
            open_work = conn.execute(
                """
                SELECT COUNT(*) FROM agent_room_work_items
                WHERE room_id = ? AND state IN ('queued', 'active', 'review', 'blocked')
                """,
                (room_id,),
            ).fetchone()
            if int(open_work[0] if open_work is not None else 0) > 0:
                raise ValueError(
                    "complete or cancel open Room work before permanently deleting it"
                )
            room_file = Path(str(room_state["room_file"]))
            cursor = conn.execute("DELETE FROM agent_rooms WHERE id = ?", (room_id,))
            if cursor.rowcount != 1:
                raise AgentRoomNotFound(room_id)
        try:
            root = self.room_dir.expanduser().resolve(strict=False)
            candidate = room_file.expanduser().resolve(strict=False)
            if candidate.parent == root and candidate.is_file() and not candidate.is_symlink():
                candidate.unlink()
        except OSError:
            pass
        return {"roomId": room_id, "title": str(room.get("title") or ""), "sessionIds": session_ids}

    def update_config(
        self,
        room_id: str,
        values: Mapping[str, object],
        *,
        updated_at_ms: int | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, object]:
        if connection is not None:
            return self._update_config(
                connection,
                room_id,
                values,
                updated_at_ms=updated_at_ms,
            )
        with self._connect() as conn:
            self._update_config(
                conn,
                room_id,
                values,
                updated_at_ms=updated_at_ms,
            )
        return self.get(room_id)

    def _update_config(
        self,
        conn: sqlite3.Connection,
        room_id: str,
        values: Mapping[str, object],
        *,
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        allowed = {
            "title",
            "roomKind",
            "avatar",
            "description",
            "scenarioPrompt",
            "routingPolicy",
            "routingConfig",
            "moderatorParticipantId",
            "activeTopicId",
            "workspaceRoots",
            "permissionPolicy",
            "executionMode",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"unsupported agent room configuration fields: {', '.join(sorted(unknown))}")
        current = self._get(conn, room_id)
        active_participant_ids = {
            str(item["id"])
            for item in current["participants"]
            if isinstance(item, Mapping) and item.get("status") == "active"
        }
        updates: dict[str, object] = {}
        permission_policy: dict[str, object] | None = None
        if "permissionPolicy" in values:
            permission_policy = normalize_room_permission_policy(
                values.get("permissionPolicy"),
                room_kind=current.get("roomKind", "collaboration"),
                current=current,
                legacy_execution_mode=values.get("executionMode"),
            )
        elif "executionMode" in values:
            permission_policy = normalize_room_permission_policy(
                None,
                room_kind=current.get("roomKind", "collaboration"),
                current=current,
                legacy_execution_mode=values.get("executionMode"),
            )
        if permission_policy is not None:
            updates.update(room_permission_policy_columns(permission_policy))
        if "title" in values:
            title = " ".join(str(values.get("title") or "").split())[:120]
            if not title:
                raise ValueError("agent room title must not be empty")
            updates["title"] = title
        if "roomKind" in values:
            room_kind = normalize_room_kind(values.get("roomKind"))
            if room_kind != str(current.get("roomKind") or "collaboration"):
                raise ValueError("roomKind cannot be changed after room creation")
        if "avatar" in values:
            updates["avatar"] = " ".join(str(values.get("avatar") or "members").split())[:80] or "members"
        if "description" in values:
            updates["description"] = " ".join(str(values.get("description") or "").split())[:500]
        if "scenarioPrompt" in values:
            updates["scenario_prompt"] = str(values.get("scenarioPrompt") or "").strip()[:8_000]
        if "routingPolicy" in values:
            policy = normalize_routing_policy(values.get("routingPolicy"))
            updates["routing_mode"] = policy
            updates["routing_policy"] = _legacy_policy(policy)
            if policy == "moderator":
                moderator_id = (
                    str(values.get("moderatorParticipantId") or "")
                    or str(current.get("moderatorParticipantId") or "")
                )
                if moderator_id not in active_participant_ids:
                    raise ValueError("moderated room requires an active moderator participant")
        if "routingConfig" in values:
            routing_config = normalize_routing_config(values.get("routingConfig"))
            fallback_id = str(routing_config.get("fallbackParticipantId") or "")
            if fallback_id and fallback_id not in active_participant_ids:
                raise ValueError("fallbackParticipantId must identify an active room participant")
            updates["routing_config_json"] = json.dumps(
                routing_config,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        if "moderatorParticipantId" in values:
            moderator_id = str(values.get("moderatorParticipantId") or "").strip()
            if moderator_id and moderator_id not in active_participant_ids:
                raise ValueError("moderatorParticipantId must identify one active room participant")
            updates["moderator_participant_id"] = moderator_id
        if "activeTopicId" in values:
            topic_id = str(values.get("activeTopicId") or "").strip()
            if topic_id not in {
                str(item["id"])
                for item in current.get("topics", [])
                if isinstance(item, Mapping) and item.get("status") == "active"
            }:
                raise ValueError("activeTopicId must identify an active room topic")
            updates["active_topic_id"] = topic_id
        if "workspaceRoots" in values:
            roots = values.get("workspaceRoots")
            if not isinstance(roots, list):
                raise ValueError("workspaceRoots must be an array")
            normalized_roots = _workspace_roots(
                [str(value) for value in roots]
            )
            if len(normalized_roots) > MAX_ROOM_WORKSPACE_ROOTS:
                raise ValueError(
                    f"agent room accepts at most {MAX_ROOM_WORKSPACE_ROOTS} workspace roots"
                )
            updates["workspace_roots_json"] = json.dumps(
                normalized_roots,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        if not updates:
            return current
        timestamp = _timestamp(updated_at_ms)
        assignments = ", ".join(f"{column} = ?" for column in updates)
        cursor = conn.execute(
            f"""
            UPDATE agent_rooms
            SET {assignments}, config_revision = config_revision + 1, updated_at_ms = ?
            WHERE id = ?
            """,  # noqa: S608 - column names come from the fixed allowlist above
            (*updates.values(), timestamp, room_id),
        )
        if cursor.rowcount != 1:
            raise AgentRoomNotFound(room_id)
        return self._get(conn, room_id)

    def participant(self, participant_id: str) -> dict[str, object]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_room_participants WHERE id = ?",
                (participant_id,),
            ).fetchone()
        if row is None:
            raise AgentParticipantNotFound(participant_id)
        return _participant_payload(row)

    def participant_for_session(
        self,
        session_id: str,
        *,
        active_only: bool = True,
    ) -> dict[str, object] | None:
        active_filter = (
            "AND p.participant_status = 'active' AND r.status = 'active'"
            if active_only
            else ""
        )
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT p.* FROM agent_room_participants p
                JOIN agent_rooms r ON r.id = p.room_id
                WHERE p.session_id = ? {active_filter}
                """,  # noqa: S608 - active_filter is a fixed internal clause
                (session_id,),
            ).fetchone()
        return _participant_payload(row) if row is not None else None

    def room_ids_for_session(
        self,
        session_id: str,
        *,
        active_only: bool = True,
    ) -> list[str]:
        """Return every Room whose participant identity owns this Session."""

        active_filter = (
            "AND p.participant_status = 'active' AND r.status = 'active'"
            if active_only
            else ""
        )
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT p.room_id
                FROM agent_room_participants AS p
                JOIN agent_rooms AS r ON r.id = p.room_id
                WHERE p.session_id = ? {active_filter}
                ORDER BY p.room_id
                """,  # noqa: S608 - active_filter is a fixed internal clause
                (session_id,),
            ).fetchall()
        return [str(row["room_id"]) for row in rows]

    def participants_for_sessions(
        self,
        session_ids: Sequence[str],
        *,
        active_only: bool = True,
    ) -> dict[str, dict[str, object]]:
        normalized_ids = tuple(
            dict.fromkeys(
                str(session_id).strip()
                for session_id in session_ids
                if str(session_id).strip()
            )
        )
        if not normalized_ids:
            return {}
        active_filter = (
            "AND p.participant_status = 'active' AND r.status = 'active'"
            if active_only
            else ""
        )
        placeholders = ", ".join("?" for _ in normalized_ids)
        with self._read_connect() as conn:
            rows = conn.execute(
                f"""
                SELECT p.* FROM agent_room_participants p
                JOIN agent_rooms r ON r.id = p.room_id
                WHERE p.session_id IN ({placeholders}) {active_filter}
                ORDER BY r.updated_at_ms DESC, p.created_at_ms DESC
                """,  # noqa: S608 - placeholders and active_filter are fixed internally
                normalized_ids,
            ).fetchall()
        result: dict[str, dict[str, object]] = {}
        for row in rows:
            session_id = str(row["session_id"])
            result.setdefault(session_id, _participant_payload(row))
        return result

    def plan_route(
        self,
        room_id: str,
        text: str,
        *,
        requested_participant_ids: Sequence[str] = (),
        profiles: Mapping[str, Mapping[str, object]] | None = None,
        authoritative_participant_id: str = "",
        conversation_only: bool = False,
    ) -> dict[str, object]:
        room = self.get(room_id)
        if room["status"] != "active":
            raise ValueError("agent room is archived")
        return plan_room_route(
            room,
            text,
            requested_participant_ids=requested_participant_ids,
            profiles=profiles,
            authoritative_participant_id=authoritative_participant_id,
            conversation_only=conversation_only,
        )

    def plan_routes(
        self,
        room_id: str,
        text: str,
        *,
        requested_participant_ids: Sequence[str] = (),
        profiles: Mapping[str, Mapping[str, object]] | None = None,
        authoritative_participant_id: str = "",
        conversation_only: bool = False,
    ) -> list[dict[str, object]]:
        room = self.get(room_id)
        if room["status"] != "active":
            raise ValueError("agent room is archived")
        return plan_room_routes(
            room,
            text,
            requested_participant_ids=requested_participant_ids,
            profiles=profiles,
            authoritative_participant_id=authoritative_participant_id,
            conversation_only=conversation_only,
        )

    def route_target(self, room_id: str, text: str) -> dict[str, object]:
        decision = self.plan_route(room_id, text)
        selected = str(decision["targetParticipantId"])
        return self.participant(selected)

    def commit_route(
        self,
        room_id: str,
        decision: Mapping[str, object],
        *,
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        if str(decision.get("routingPolicy") or "") != "sequential":
            return self.get(room_id)
        target = self.participant(str(decision.get("targetParticipantId") or ""))
        if str(target["roomId"]) != room_id:
            raise ValueError("route decision target does not belong to this room")
        room = self.get(room_id)
        active_ordinals = [
            int(item["ordinal"])
            for item in room["participants"]
            if isinstance(item, Mapping) and item.get("status") == "active"
        ]
        if not active_ordinals:
            return room
        current = int(target["ordinal"])
        later = sorted(value for value in active_ordinals if value > current)
        next_ordinal = later[0] if later else min(active_ordinals)
        timestamp = _timestamp(updated_at_ms)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE agent_rooms
                SET next_speaker_ordinal = ?, updated_at_ms = ? WHERE id = ?
                """,
                (next_ordinal, timestamp, room_id),
            )
        return self.get(room_id)

    def list_topics(self, room_id: str, *, include_archived: bool = False) -> list[dict[str, object]]:
        self.get(room_id)
        where = "" if include_archived else "AND topic_status = 'active'"
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM agent_room_topics
                WHERE room_id = ? {where}
                ORDER BY ordinal ASC, created_at_ms ASC
                """,  # noqa: S608 - where is a fixed internal clause
                (room_id,),
            ).fetchall()
        return [_topic_payload(row) for row in rows]

    def create_topic(
        self,
        room_id: str,
        *,
        title: str,
        summary: str = "",
        created_at_ms: int | None = None,
    ) -> dict[str, object]:
        self.get(room_id)
        normalized_title = " ".join(str(title or "").split())[:120]
        if not normalized_title:
            raise ValueError("room topic title must not be empty")
        normalized_summary = " ".join(str(summary or "").split())[:2_000]
        timestamp = _timestamp(created_at_ms)
        topic_id = f"topic:{uuid.uuid4()}"
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(ordinal), -1) + 1 FROM agent_room_topics WHERE room_id = ?",
                (room_id,),
            ).fetchone()
            ordinal = int(row[0]) if row is not None else 0
            conn.execute(
                """
                INSERT INTO agent_room_topics(
                    id, room_id, title, summary, topic_status, ordinal,
                    created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    topic_id,
                    room_id,
                    normalized_title,
                    normalized_summary,
                    ordinal,
                    timestamp,
                    timestamp,
                ),
            )
            conn.execute(
                """
                UPDATE agent_rooms
                SET active_topic_id = ?, config_revision = config_revision + 1,
                    updated_at_ms = ?
                WHERE id = ?
                """,
                (topic_id, timestamp, room_id),
            )
        return self.topic(topic_id)

    def topic(self, topic_id: str) -> dict[str, object]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_room_topics WHERE id = ?",
                (topic_id,),
            ).fetchone()
        if row is None:
            raise ValueError("room topic was not found")
        return _topic_payload(row)

    def update_topic(
        self,
        room_id: str,
        topic_id: str,
        values: Mapping[str, object],
        *,
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        allowed = {"title", "summary", "archived", "ordinal", "activate"}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"unsupported room topic fields: {', '.join(sorted(unknown))}")
        room = self.get(room_id)
        topic = self.topic(topic_id)
        if str(topic["roomId"]) != room_id:
            raise ValueError("room topic does not belong to this room")
        updates: dict[str, object] = {}
        if "title" in values:
            title = " ".join(str(values.get("title") or "").split())[:120]
            if not title:
                raise ValueError("room topic title must not be empty")
            updates["title"] = title
        if "summary" in values:
            updates["summary"] = " ".join(str(values.get("summary") or "").split())[:2_000]
        if "ordinal" in values:
            updates["ordinal"] = max(0, min(int(values.get("ordinal") or 0), 999))
        if "archived" in values:
            archived = bool(values.get("archived"))
            if archived and str(room.get("activeTopicId") or "") == topic_id:
                alternatives = [
                    item
                    for item in room.get("topics", [])
                    if isinstance(item, Mapping)
                    and item.get("status") == "active"
                    and item.get("id") != topic_id
                ]
                if not alternatives:
                    raise ValueError("a room must keep at least one active topic")
                updates["topic_status"] = "archived"
                updates["activateFallback"] = str(alternatives[0]["id"])
            else:
                updates["topic_status"] = "archived" if archived else "active"
        activate = bool(values.get("activate"))
        timestamp = _timestamp(updated_at_ms)
        fallback = str(updates.pop("activateFallback", ""))
        with self._connect() as conn:
            if updates:
                assignments = ", ".join(f"{column} = ?" for column in updates)
                conn.execute(
                    f"""
                    UPDATE agent_room_topics
                    SET {assignments}, updated_at_ms = ?
                    WHERE id = ? AND room_id = ?
                    """,  # noqa: S608 - column names come from the fixed allowlist above
                    (*updates.values(), timestamp, topic_id, room_id),
                )
            active_topic_id = topic_id if activate else fallback
            if active_topic_id:
                conn.execute(
                    """
                    UPDATE agent_rooms
                    SET active_topic_id = ?, config_revision = config_revision + 1,
                        updated_at_ms = ?
                    WHERE id = ?
                    """,
                    (active_topic_id, timestamp, room_id),
                )
            elif updates:
                conn.execute(
                    """
                    UPDATE agent_rooms
                    SET config_revision = config_revision + 1, updated_at_ms = ?
                    WHERE id = ?
                    """,
                    (timestamp, room_id),
                )
        return self.topic(topic_id)

    def list_artifacts(
        self,
        room_id: str,
        *,
        include_archived: bool = False,
        topic_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, object]]:
        self.get(room_id)
        clauses = ["room_id = ?"]
        params: list[object] = [room_id]
        if not include_archived:
            clauses.append("artifact_status = 'active'")
        if topic_id:
            clauses.append("topic_id = ?")
            params.append(topic_id)
        params.append(max(1, min(int(limit), 200)))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM agent_room_artifacts
                WHERE {' AND '.join(clauses)}
                ORDER BY updated_at_ms DESC LIMIT ?
                """,  # noqa: S608 - clauses are fixed internal strings
                tuple(params),
            ).fetchall()
        return [_artifact_payload(row) for row in rows]

    def add_artifact(
        self,
        room_id: str,
        *,
        path: str,
        display_name: str = "",
        topic_id: str = "",
        media_type: str = "",
        created_by_participant_id: str = "",
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        room = self.get(room_id)
        resolved = _authorized_artifact_path(path, room.get("workspaceRoots", []))
        if topic_id:
            topic = self.topic(topic_id)
            if str(topic["roomId"]) != room_id or topic["status"] != "active":
                raise ValueError("artifact topic must be active and belong to this room")
        else:
            topic_id = str(room.get("activeTopicId") or "")
        if created_by_participant_id:
            participant = self.participant(created_by_participant_id)
            if str(participant["roomId"]) != room_id:
                raise ValueError("artifact author does not belong to this room")
        timestamp = _timestamp(updated_at_ms)
        name = " ".join(str(display_name or resolved.name).split())[:240] or resolved.name
        mime = str(media_type or mimetypes.guess_type(resolved.name)[0] or "application/octet-stream")[:120]
        byte_size = resolved.stat().st_size
        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT * FROM agent_room_artifacts
                WHERE room_id = ? AND path = ?
                ORDER BY updated_at_ms DESC LIMIT 1
                """,
                (room_id, str(resolved)),
            ).fetchone()
            if existing is None:
                artifact_id = f"room-artifact:{uuid.uuid4()}"
                conn.execute(
                    """
                    INSERT INTO agent_room_artifacts(
                        id, room_id, topic_id, path, display_name, media_type,
                        byte_size, sha256, revision, artifact_status,
                        created_by_participant_id, created_at_ms, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, '', 1, 'active', ?, ?, ?)
                    """,
                    (
                        artifact_id,
                        room_id,
                        topic_id,
                        str(resolved),
                        name,
                        mime,
                        byte_size,
                        created_by_participant_id,
                        timestamp,
                        timestamp,
                    ),
                )
            else:
                artifact_id = str(existing["id"])
                conn.execute(
                    """
                    UPDATE agent_room_artifacts
                    SET topic_id = ?, display_name = ?, media_type = ?,
                        byte_size = ?, revision = revision + 1,
                        artifact_status = 'active',
                        created_by_participant_id = ?, updated_at_ms = ?
                    WHERE id = ?
                    """,
                    (
                        topic_id,
                        name,
                        mime,
                        byte_size,
                        created_by_participant_id,
                        timestamp,
                        artifact_id,
                    ),
                )
        return self.artifact(artifact_id)

    def artifact(self, artifact_id: str) -> dict[str, object]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_room_artifacts WHERE id = ?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise ValueError("room artifact was not found")
        return _artifact_payload(row)

    def archive_artifact(
        self,
        room_id: str,
        artifact_id: str,
        *,
        archived: bool,
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        artifact = self.artifact(artifact_id)
        if str(artifact["roomId"]) != room_id:
            raise ValueError("room artifact does not belong to this room")
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE agent_room_artifacts
                SET artifact_status = ?, updated_at_ms = ?
                WHERE id = ? AND room_id = ?
                """,
                (
                    "archived" if archived else "active",
                    _timestamp(updated_at_ms),
                    artifact_id,
                    room_id,
                ),
            )
        return self.artifact(artifact_id)

    def recent_public_messages(
        self,
        room_id: str,
        *,
        topic_id: str = "",
        exclude_turn_id: str = "",
        limit: int = 24,
    ) -> list[dict[str, object]]:
        self.get(room_id)
        clauses = [
            "room_id = ?",
            "event_type IN ('user_message', 'participant_message', 'room_post')",
        ]
        params: list[object] = [room_id]
        if topic_id:
            clauses.append("topic_id = ?")
            params.append(topic_id)
        if exclude_turn_id:
            clauses.append("turn_id != ?")
            params.append(exclude_turn_id)
        params.append(max(1, min(int(limit), 100)))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM (
                    SELECT * FROM agent_room_events
                    WHERE {' AND '.join(clauses)}
                    ORDER BY sequence DESC LIMIT ?
                ) ORDER BY sequence ASC
                """,  # noqa: S608 - clauses are fixed internal strings
                tuple(params),
            ).fetchall()
        return [_room_event_payload(row) for row in rows]

    def unread_public_messages(
        self,
        room_id: str,
        participant_id: str,
        *,
        topic_id: str = "",
        exclude_turn_id: str = "",
        limit: int = 24,
    ) -> dict[str, object]:
        """Return one participant's bounded unread public lane and ack watermark."""

        bounded = max(1, min(int(limit), 100))
        with self._connect() as conn:
            participant = conn.execute(
                """
                SELECT id FROM agent_room_participants
                WHERE id = ? AND room_id = ?
                """,
                (participant_id, room_id),
            ).fetchone()
            if participant is None:
                raise AgentParticipantNotFound(participant_id)
            cursor = conn.execute(
                """
                SELECT last_sequence FROM agent_room_delivery_cursors
                WHERE room_id = ? AND participant_id = ? AND topic_id = ?
                """,
                (room_id, participant_id, topic_id),
            ).fetchone()
            after_sequence = int(cursor["last_sequence"]) if cursor is not None else 0
            clauses = [
                "room_id = ?",
                "sequence > ?",
                "event_type IN ('user_message', 'participant_message', 'room_post')",
            ]
            params: list[object] = [room_id, after_sequence]
            if topic_id:
                clauses.append("topic_id = ?")
                params.append(topic_id)
            if exclude_turn_id:
                clauses.append("turn_id != ?")
                params.append(exclude_turn_id)
            count_row = conn.execute(
                f"""
                SELECT COUNT(*) AS count, COALESCE(MAX(sequence), ?) AS through_sequence
                FROM agent_room_events
                WHERE {' AND '.join(clauses)}
                """,  # noqa: S608 - clauses are fixed internal strings
                (after_sequence, *params),
            ).fetchone()
            rows = conn.execute(
                f"""
                SELECT * FROM (
                    SELECT * FROM agent_room_events
                    WHERE {' AND '.join(clauses)}
                    ORDER BY sequence DESC LIMIT ?
                ) ORDER BY sequence ASC
                """,  # noqa: S608 - clauses are fixed internal strings
                (*params, bounded),
            ).fetchall()
        count = int(count_row["count"] if count_row is not None else 0)
        through_sequence = int(
            count_row["through_sequence"] if count_row is not None else after_sequence
        )
        return {
            "items": [_room_event_payload(row) for row in rows],
            "cursorSequence": after_sequence,
            "throughSequence": through_sequence,
            "omittedCount": max(0, count - len(rows)),
        }

    def advance_delivery_cursor(
        self,
        room_id: str,
        participant_id: str,
        *,
        topic_id: str = "",
        through_sequence: int,
        updated_at_ms: int | None = None,
    ) -> None:
        sequence = max(0, int(through_sequence))
        timestamp = _timestamp(updated_at_ms)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_room_delivery_cursors(
                    room_id, participant_id, topic_id, last_sequence, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(room_id, participant_id, topic_id) DO UPDATE SET
                    last_sequence = MAX(last_sequence, excluded.last_sequence),
                    updated_at_ms = excluded.updated_at_ms
                """,
                (room_id, participant_id, topic_id, sequence, timestamp),
            )

    def latest_turn_for_participant(self, room_id: str, participant_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT turn_id FROM agent_room_events
                WHERE room_id = ? AND participant_id = ? AND turn_id != ''
                ORDER BY sequence DESC LIMIT 1
                """,
                (room_id, participant_id),
            ).fetchone()
        return str(row["turn_id"]) if row is not None else ""

    def append_event(
        self,
        *,
        room_id: str,
        event_type: str,
        payload: Mapping[str, object],
        turn_id: str = "",
        participant_id: str | None = None,
        source_session_id: str = "",
        topic_id: str = "",
        created_at_ms: int | None = None,
        retain_per_room: int = 2000,
    ) -> dict[str, object]:
        event, created = self._append_event(
            room_id=room_id,
            event_type=event_type,
            payload=payload,
            turn_id=turn_id,
            participant_id=participant_id,
            source_session_id=source_session_id,
            topic_id=topic_id,
            created_at_ms=created_at_ms,
            retain_per_room=retain_per_room,
            projection_key="",
        )
        if not created or event is None:
            raise RuntimeError("ordinary Room event append did not create an event")
        return event

    def append_projected_event(
        self,
        *,
        projection_key: str,
        room_id: str,
        event_type: str,
        payload: Mapping[str, object],
        turn_id: str = "",
        participant_id: str | None = None,
        source_session_id: str = "",
        topic_id: str = "",
        created_at_ms: int | None = None,
        retain_per_room: int = 2000,
    ) -> tuple[dict[str, object] | None, bool]:
        """Append one durable public projection exactly once.

        The receipt outlives timeline retention, so a replayed runtime event
        cannot reappear as a duplicate after its old display event is pruned.
        """

        normalized_key = str(projection_key or "").strip()
        if not normalized_key:
            raise ValueError("Room projection key must not be empty")
        return self._append_event(
            room_id=room_id,
            event_type=event_type,
            payload=payload,
            turn_id=turn_id,
            participant_id=participant_id,
            source_session_id=source_session_id,
            topic_id=topic_id,
            created_at_ms=created_at_ms,
            retain_per_room=retain_per_room,
            projection_key=normalized_key,
        )

    def has_projection(self, projection_key: str) -> bool:
        normalized_key = str(projection_key or "").strip()
        if not normalized_key:
            return False
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM agent_room_public_projection_receipts
                WHERE projection_key = ? LIMIT 1
                """,
                (normalized_key,),
            ).fetchone()
        return row is not None

    def append_child_terminal_projection(
        self,
        *,
        runtime_event_id: str,
        dispatch_id: str,
        room_id: str,
        event_type: str,
        payload: Mapping[str, object],
        turn_id: str = "",
        participant_id: str | None = None,
        source_session_id: str = "",
        topic_id: str = "",
        created_at_ms: int | None = None,
    ) -> tuple[dict[str, object] | None, bool]:
        """Publish one Pi child terminal across live delivery and recovery.

        Recovery may have less display metadata than the live event. Its
        identity is the exact dispatch and Runtime event, while ordinary
        public projections continue requiring identical payloads on replay.
        """
        identity = (str(dispatch_id).strip(), str(runtime_event_id).strip())
        if (
            not all(identity)
            or event_type != "participant_activity"
            or not _matches_child_terminal(payload, identity)
        ):
            raise ValueError("invalid Room child Runtime terminal projection")
        return self._append_event(
            room_id=room_id,
            event_type=event_type,
            payload=payload,
            turn_id=turn_id,
            participant_id=participant_id,
            source_session_id=source_session_id,
            topic_id=topic_id,
            created_at_ms=created_at_ms,
            retain_per_room=2000,
            projection_key=f"room-partner-terminal:{identity[0]}:{identity[1]}",
            child_terminal_identity=identity,
        )

    def has_tool_terminal(
        self,
        room_id: str,
        tool_call_id: str,
        *,
        root_id: str = "",
        dispatch_id: str = "",
    ) -> bool:
        """Return whether Room already contains one exact Tool terminal."""

        normalized_room_id = str(room_id or "").strip()
        normalized_tool_call_id = str(tool_call_id or "").strip()
        normalized_root_id = str(root_id or "").strip()
        normalized_dispatch_id = str(dispatch_id or "").strip()
        if not normalized_room_id or not normalized_tool_call_id:
            return False
        # The LIKE predicates are only a bounded prefilter. Parse every match
        # before accepting it so a substring collision cannot become identity
        # evidence.
        quoted_identity = json.dumps(
            normalized_tool_call_id,
            ensure_ascii=False,
        )
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT payload_json
                FROM agent_room_events
                WHERE room_id = ?
                  AND payload_json LIKE ?
                ORDER BY sequence DESC
                LIMIT 100
                """,
                (normalized_room_id, f'%"toolCallId":{quoted_identity}%'),
            ).fetchall()
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            data = payload.get("data")
            if (
                isinstance(data, dict)
                and str(payload.get("sourceEventType") or "")
                == "tool_finished"
                and str(data.get("toolCallId") or "")
                == normalized_tool_call_id
                and (
                    not normalized_root_id
                    or str(data.get("rootId") or "") == normalized_root_id
                )
                and (
                    not normalized_dispatch_id
                    or str(data.get("dispatchId") or "")
                    == normalized_dispatch_id
                )
            ):
                return True
        return False

    def _append_event(
        self,
        *,
        room_id: str,
        event_type: str,
        payload: Mapping[str, object],
        turn_id: str,
        participant_id: str | None,
        source_session_id: str,
        topic_id: str,
        created_at_ms: int | None,
        retain_per_room: int,
        projection_key: str,
        child_terminal_identity: tuple[str, str] | None = None,
    ) -> tuple[dict[str, object] | None, bool]:
        if event_type not in ROOM_EVENT_TYPES:
            raise ValueError(f"unsupported agent room event type: {event_type}")
        timestamp = _timestamp(created_at_ms)
        safe_payload = dict(payload)
        payload_json = json.dumps(safe_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        projection_hash = _room_projection_hash(
            room_id=room_id,
            event_type=event_type,
            payload=safe_payload,
            turn_id=turn_id,
            participant_id=participant_id,
            source_session_id=source_session_id,
            topic_id=topic_id,
        )
        with self._connect() as conn:
            if child_terminal_identity is not None:
                # A live callback and startup recovery can race, including
                # through separate EventHub instances sharing this database.
                conn.execute("BEGIN IMMEDIATE")
            if projection_key:
                receipt = conn.execute(
                    """
                    SELECT room_id, event_id, payload_hash
                    FROM agent_room_public_projection_receipts
                    WHERE projection_key = ?
                    """,
                    (projection_key,),
                ).fetchone()
                if receipt is not None:
                    if (
                        str(receipt["room_id"]) != room_id
                        or (
                            child_terminal_identity is None
                            and str(receipt["payload_hash"]) != projection_hash
                        )
                    ):
                        raise ValueError("Room projection key was rebound")
                    row = conn.execute(
                        "SELECT * FROM agent_room_events WHERE event_id = ?",
                        (str(receipt["event_id"]),),
                    ).fetchone()
                    if row is not None and child_terminal_identity is not None:
                        existing = _room_event_payload(row)
                        if (
                            str(existing["turnId"]) != turn_id
                            or existing["participantId"] != participant_id
                            or str(existing["sourceSessionId"]) != source_session_id
                            or not _matches_child_terminal(
                                existing["payload"], child_terminal_identity,
                                phase=_child_terminal_data(payload).get("phase"),
                            )
                        ):
                            raise ValueError("Room projection key was rebound")
                    return (
                        _room_event_payload(row) if row is not None else None,
                        False,
                    )
            if child_terminal_identity is not None:
                # Older live publications predate durable projection keys.
                # Adopt their exact event without replaying another UI row.
                rows = conn.execute(
                    """
                    SELECT * FROM agent_room_events
                    WHERE room_id = ? AND turn_id = ?
                      AND participant_id IS ? AND source_session_id = ?
                      AND event_type = 'participant_activity'
                      AND (json_extract(payload_json, '$.sourceEventId') = ?
                           OR json_extract(payload_json, '$.sourceRuntimeEventId') = ?)
                    ORDER BY sequence DESC
                    """,
                    (room_id, turn_id, participant_id, source_session_id,
                     child_terminal_identity[1], child_terminal_identity[1]),
                ).fetchall()
                for row in rows:
                    existing = _room_event_payload(row)
                    if not _matches_child_terminal(
                        existing["payload"], child_terminal_identity,
                        phase=_child_terminal_data(payload).get("phase"),
                    ):
                        continue
                    conn.execute(
                        """
                        INSERT INTO agent_room_public_projection_receipts(
                            projection_key, room_id, event_id, payload_hash, created_at_ms
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (projection_key, room_id, existing["eventId"], projection_hash, timestamp),
                    )
                    return existing, False
            room = conn.execute("SELECT * FROM agent_rooms WHERE id = ?", (room_id,)).fetchone()
            if room is None:
                raise AgentRoomNotFound(room_id)
            if participant_id:
                participant = conn.execute(
                    "SELECT session_id FROM agent_room_participants WHERE id = ? AND room_id = ?",
                    (participant_id, room_id),
                ).fetchone()
                if participant is None:
                    raise AgentParticipantNotFound(participant_id)
            sequence = int(room["last_event_sequence"]) + 1
            event_id = f"{room_id}:{sequence}"
            event = {
                "schemaVersion": "rag-ime.agent-room-event.v1",
                "eventId": event_id,
                "roomId": room_id,
                "sequence": sequence,
                "turnId": str(turn_id or ""),
                "eventType": event_type,
                "participantId": participant_id,
                "sourceSessionId": str(source_session_id or ""),
                "topicId": str(topic_id or room["active_topic_id"] or ""),
                "createdAtMs": timestamp,
                "payload": safe_payload,
                "resumeToken": event_id,
            }
            validate_contract(event, "agent-room-event.v1.json")
            conn.execute(
                """
                INSERT INTO agent_room_events(
                    event_id, room_id, sequence, turn_id, event_type,
                    participant_id, source_session_id, topic_id, created_at_ms, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    room_id,
                    sequence,
                    str(turn_id or ""),
                    event_type,
                    participant_id,
                    str(source_session_id or ""),
                    str(topic_id or room["active_topic_id"] or ""),
                    timestamp,
                    payload_json,
                ),
            )
            conn.execute(
                """
                UPDATE agent_rooms
                SET last_event_sequence = ?, updated_at_ms = ? WHERE id = ?
                """,
                (sequence, timestamp, room_id),
            )
            if projection_key:
                conn.execute(
                    """
                    INSERT INTO agent_room_public_projection_receipts(
                        projection_key, room_id, event_id, payload_hash, created_at_ms
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        projection_key,
                        room_id,
                        event_id,
                        projection_hash,
                        timestamp,
                    ),
                )
            if participant_id and event_type in {"participant_message", "turn_completed"}:
                conn.execute(
                    "UPDATE agent_room_participants SET last_spoke_at_ms = ? WHERE id = ?",
                    (timestamp, participant_id),
                )
            conn.execute(
                "DELETE FROM agent_room_events WHERE room_id = ? AND sequence <= ?",
                (room_id, max(0, sequence - max(100, int(retain_per_room)))),
            )
            self._append_jsonl(Path(str(room["room_file"])), event)
        return event, True

    def list_events(
        self,
        room_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 500,
    ) -> list[dict[str, object]]:
        self.get(room_id)
        bounded = max(1, min(int(limit), 2000))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_room_events
                WHERE room_id = ? AND sequence > ?
                ORDER BY sequence ASC LIMIT ?
                """,
                (room_id, max(0, int(after_sequence)), bounded),
            ).fetchall()
        return [_room_event_payload(row) for row in rows]

    def list_events_for_turn(
        self,
        room_id: str,
        turn_id: str,
        *,
        event_types: Sequence[str] = (),
        after_sequence: int = 0,
        limit: int = 500,
    ) -> list[dict[str, object]]:
        """Read one turn without decoding unrelated retained Room events."""

        self.get(room_id)
        normalized_types = tuple(
            dict.fromkeys(str(value or "").strip() for value in event_types)
        )
        unsupported = set(normalized_types) - ROOM_EVENT_TYPES
        if unsupported:
            raise ValueError(
                f"unsupported agent room event type: {sorted(unsupported)[0]}"
            )
        bounded = max(1, min(int(limit), 2000))
        query = """
            SELECT * FROM agent_room_events
            WHERE room_id = ? AND turn_id = ? AND sequence > ?
        """
        parameters: list[object] = [
            room_id,
            str(turn_id or ""),
            max(0, int(after_sequence)),
        ]
        if normalized_types:
            placeholders = ", ".join("?" for _ in normalized_types)
            query += f" AND event_type IN ({placeholders})"
            parameters.extend(normalized_types)
        query += " ORDER BY sequence ASC LIMIT ?"
        parameters.append(bounded)
        with self._connect() as conn:
            rows = conn.execute(query, parameters).fetchall()
        return [_room_event_payload(row) for row in rows]

    def list_recovery_events_for_dispatches(
        self,
        room_id: str,
        turn_id: str,
        *,
        dispatch_ids: Sequence[str],
        limit: int = 500,
    ) -> list[dict[str, object]]:
        """Read only recovery events that name an expected child dispatch."""

        self.get(room_id)
        normalized_dispatch_ids = tuple(
            dict.fromkeys(
                value
                for value in (
                    str(dispatch_id or "").strip()
                    for dispatch_id in dispatch_ids
                )
                if value
            )
        )
        if not normalized_dispatch_ids:
            return []
        bounded = max(1, min(int(limit), 2000))
        dispatch_ids_json = json.dumps(
            normalized_dispatch_ids,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._connect() as conn:
            rows = conn.execute(
                """
                WITH expected_dispatches(dispatch_id) AS (
                    SELECT CAST(value AS TEXT) FROM json_each(?)
                )
                SELECT * FROM agent_room_events
                WHERE room_id = ?
                  AND turn_id = ?
                  AND event_type IN (
                      'room_post',
                      'participant_message',
                      'participant_activity'
                  )
                  AND (
                      json_extract(payload_json, '$.post.dispatchId') IN (
                          SELECT dispatch_id FROM expected_dispatches
                      )
                      OR json_extract(payload_json, '$.data.dispatchId') IN (
                          SELECT dispatch_id FROM expected_dispatches
                      )
                      OR json_extract(payload_json, '$.data.childDispatchId') IN (
                          SELECT dispatch_id FROM expected_dispatches
                      )
                      OR json_extract(payload_json, '$.dispatchId') IN (
                          SELECT dispatch_id FROM expected_dispatches
                      )
                      OR json_extract(payload_json, '$.childDispatchId') IN (
                          SELECT dispatch_id FROM expected_dispatches
                      )
                  )
                ORDER BY sequence ASC LIMIT ?
                """,
                (
                    dispatch_ids_json,
                    room_id,
                    str(turn_id or ""),
                    bounded,
                ),
            ).fetchall()
        return [_room_event_payload(row) for row in rows]

    def has_typed_result(self, room_id: str, turn_id: str) -> bool:
        """Check the authoritative typed Root result without scanning the room."""

        self.get(room_id)
        normalized_turn_id = str(turn_id or "")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM agent_room_events
                WHERE room_id = ?
                  AND turn_id = ?
                  AND event_type = 'room_post'
                  AND json_extract(payload_json, '$.post.kind') = 'result'
                ORDER BY sequence DESC LIMIT 1
                """,
                (room_id, normalized_turn_id),
            ).fetchone()
        if row is None:
            return False
        event = _room_event_payload(row)
        payload = event.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        post = payload.get("post")
        return (
            str(event.get("roomId") or "") == room_id
            and str(event.get("turnId") or "") == normalized_turn_id
            and str(event.get("eventType") or "") == "room_post"
            and isinstance(post, Mapping)
            and str(post.get("kind") or "") == "result"
        )

    def history_page(
        self,
        room_id: str,
        *,
        before_sequence: int = 0,
        limit: int = 100,
    ) -> dict[str, object]:
        """Return one bounded retained-event page, oldest to newest."""

        self.get(room_id)
        bounded = max(1, min(int(limit), ROOM_HISTORY_PAGE_LIMIT))
        with self._connect() as conn:
            bounds = conn.execute(
                """
                SELECT COALESCE(MIN(sequence), 0) AS first_sequence,
                       COALESCE(MAX(sequence), 0) AS last_sequence
                FROM agent_room_events WHERE room_id = ?
                """,
                (room_id,),
            ).fetchone()
            retained_first = int(bounds["first_sequence"]) if bounds is not None else 0
            retained_last = int(bounds["last_sequence"]) if bounds is not None else 0
            cursor = int(before_sequence)
            if cursor <= 0:
                cursor = retained_last + 1
            rows = conn.execute(
                """
                SELECT * FROM (
                    SELECT * FROM agent_room_events
                    WHERE room_id = ? AND sequence < ?
                    ORDER BY sequence DESC LIMIT ?
                ) ORDER BY sequence ASC
                """,
                (room_id, cursor, bounded),
            ).fetchall()
        items = [_room_event_payload(row) for row in rows]
        first_sequence = int(items[0]["sequence"]) if items else 0
        last_sequence = int(items[-1]["sequence"]) if items else 0
        has_more = bool(items and retained_first < first_sequence)
        response: dict[str, object] = {
            "schemaVersion": "rag-ime.agent-room-event-page.v1",
            "ok": True,
            "roomId": room_id,
            "items": items,
            "firstSequence": first_sequence,
            "lastSequence": last_sequence,
            "nextBeforeSequence": first_sequence if has_more else 0,
            "hasMore": has_more,
            "retainedFirstSequence": retained_first,
            "retainedLastSequence": retained_last,
            "retainedPrefixTruncated": retained_first > 1,
        }
        validate_contract(response, "agent-room-event-page.v1.json")
        return response

    def snapshot(
        self,
        room_id: str,
        *,
        conversation_only: bool = False,
    ) -> dict[str, object]:
        """Read room metadata and a retained projection from one SQLite snapshot."""
        with self._connect() as conn:
            # sqlite3 does not keep multiple SELECT statements on one snapshot
            # unless a transaction is opened explicitly.
            conn.execute("BEGIN")
            room_row = conn.execute(
                "SELECT * FROM agent_rooms WHERE id = ?",
                (room_id,),
            ).fetchone()
            if room_row is None:
                raise AgentRoomNotFound(room_id)
            participant_rows = conn.execute(
                """
                SELECT p.*
                FROM agent_room_participants AS p
                WHERE room_id = ? ORDER BY ordinal ASC
                """,
                (room_id,),
            ).fetchall()
            topic_rows = conn.execute(
                """
                SELECT * FROM agent_room_topics
                WHERE room_id = ? ORDER BY topic_status ASC, ordinal ASC, created_at_ms ASC
                """,
                (room_id,),
            ).fetchall()
            artifact_rows = conn.execute(
                """
                SELECT * FROM agent_room_artifacts
                WHERE room_id = ? AND artifact_status = 'active'
                ORDER BY updated_at_ms DESC LIMIT 100
                """,
                (room_id,),
            ).fetchall()
            work_rows = conn.execute(
                """
                SELECT * FROM agent_room_work_items
                WHERE room_id = ?
                ORDER BY
                    CASE state
                        WHEN 'blocked' THEN 0
                        WHEN 'review' THEN 1
                        WHEN 'active' THEN 2
                        WHEN 'queued' THEN 3
                        ELSE 4
                    END,
                    updated_at_ms DESC
                LIMIT 100
                """,
                (room_id,),
            ).fetchall()
            bounds_row = conn.execute(
                """
                SELECT COUNT(*) AS event_count,
                       COALESCE(MIN(sequence), 0) AS first_sequence,
                       COALESCE(MAX(sequence), 0) AS last_sequence
                FROM agent_room_events WHERE room_id = ?
                """,
                (room_id,),
            ).fetchone()
            conversation_count_row: sqlite3.Row | None = None
            if conversation_only:
                conversation_count_row = conn.execute(
                    """
                    SELECT COUNT(*) AS event_count
                    FROM agent_room_events
                    WHERE room_id = ? AND event_type <> 'participant_activity'
                    """,
                    (room_id,),
                ).fetchone()
                event_rows = conn.execute(
                    """
                    SELECT * FROM (
                        SELECT * FROM agent_room_events
                        WHERE room_id = ? AND event_type <> 'participant_activity'
                        ORDER BY sequence DESC LIMIT ?
                    ) ORDER BY sequence ASC
                    """,
                    (room_id, ROOM_CONVERSATION_EVENT_LIMIT),
                ).fetchall()
            else:
                event_rows = conn.execute(
                    """
                    SELECT * FROM (
                        SELECT * FROM agent_room_events
                        WHERE room_id = ?
                        ORDER BY sequence DESC LIMIT ?
                    ) ORDER BY sequence ASC
                    """,
                    (room_id, ROOM_SNAPSHOT_EVENT_LIMIT),
                ).fetchall()
            # The live snapshot is a projection input, not an arbitrary tail
            # of the audit log.  Cutting through the middle of a Room turn can
            # drop its user message and route decision while retaining later
            # Partner messages.  A fresh UI would then misclassify those
            # messages as unrouted and lose a final result that was visible
            # before refresh.  Keep the normal bounded tail, but expand it to
            # the start of the earliest turn already represented by that tail.
            if event_rows and not conversation_only:
                leading_turn_id = str(event_rows[0]["turn_id"] or "")
                if leading_turn_id:
                    boundary_row = conn.execute(
                        """
                        SELECT COALESCE(MIN(sequence), 0) AS first_sequence
                        FROM agent_room_events
                        WHERE room_id = ? AND turn_id = ?
                        """,
                        (room_id, leading_turn_id),
                    ).fetchone()
                    turn_first_sequence = (
                        int(boundary_row["first_sequence"])
                        if boundary_row is not None
                        else 0
                    )
                    if turn_first_sequence < int(event_rows[0]["sequence"]):
                        event_rows = conn.execute(
                            """
                            SELECT * FROM agent_room_events
                            WHERE room_id = ? AND sequence >= ?
                            ORDER BY sequence ASC
                            """,
                            (room_id, turn_first_sequence),
                        ).fetchall()
            start_gate_row = conn.execute(
                "SELECT room_id, status, objective_text, work_item_id, client_message_id, root_id, confirmed_at_ms FROM agent_room_start_gates WHERE room_id = ?",
                (room_id,),
            ).fetchone()

        room = _room_payload(
            room_row,
            participant_rows,
            topic_rows,
            artifact_rows,
            work_rows,
            start_gate=_room_start_gate_payload(start_gate_row),
        )
        events = [_room_event_payload(row) for row in event_rows]
        retained_count = int(bounds_row["event_count"]) if bounds_row is not None else 0
        retained_first = int(bounds_row["first_sequence"]) if bounds_row is not None else 0
        last_sequence = int(room["lastEventSequence"])
        retained_last = int(bounds_row["last_sequence"]) if bounds_row is not None else 0
        if retained_count and retained_last != last_sequence:
            raise RuntimeError("agent room event cursor is inconsistent with retained events")
        first_sequence = int(events[0]["sequence"]) if events else 0
        if conversation_only:
            conversation_count = (
                int(conversation_count_row["event_count"])
                if conversation_count_row is not None
                else 0
            )
            snapshot = {
                "schemaVersion": "rag-ime.agent-room-conversation-snapshot.v1",
                "ok": True,
                "room": room,
                "events": events,
                "firstEventSequence": first_sequence,
                "cursorSequence": last_sequence,
                "resumeToken": f"{room_id}:{last_sequence}" if last_sequence else "",
                "deferredEventCount": max(0, retained_count - conversation_count),
                "truncated": bool(
                    conversation_count > len(events)
                    or retained_first > 1
                ),
            }
            validate_contract(
                snapshot,
                "agent-room-conversation-snapshot.v1.json",
            )
            return snapshot
        snapshot = {
            "schemaVersion": "rag-ime.agent-room-snapshot.v1",
            "ok": True,
            "room": room,
            "events": events,
            "firstSequence": first_sequence,
            "lastSequence": last_sequence,
            "resumeToken": f"{room_id}:{last_sequence}" if last_sequence else "",
            "truncated": bool(
                retained_count > len(events)
                or retained_first > 1
                or (last_sequence > 0 and not events)
            ),
        }
        validate_contract(snapshot, "agent-room-snapshot.v1.json")
        return snapshot

    def conversation_snapshot(self, room_id: str) -> dict[str, object]:
        """Return the message/terminal projection before deferred Tool activity."""
        return self.snapshot(room_id, conversation_only=True)

    def event_bounds(self, room_id: str) -> tuple[int, int]:
        self.get(room_id)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(MIN(sequence), 0), COALESCE(MAX(sequence), 0)
                FROM agent_room_events WHERE room_id = ?
                """,
                (room_id,),
            ).fetchone()
        return (int(row[0]), int(row[1])) if row is not None else (0, 0)

    def _room_file(self, room_id: str) -> Path:
        safe_name = room_id.replace(":", "-")
        return self.room_dir / f"{safe_name}.jsonl"

    @staticmethod
    def _append_jsonl(path: Path, event: Mapping[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            payload = (json.dumps(dict(event), ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def _read_connect(self) -> Iterator[sqlite3.Connection]:
        with self._read_lock:
            conn = self._read_connection
            if conn is not None:
                yield conn
                return
        with self._connect() as fallback:
            yield fallback

    def close(self) -> None:
        with self._read_lock:
            conn = self._read_connection
            self._read_connection = None
        if conn is not None:
            conn.close()

    @contextmanager
    def write_transaction(self) -> Iterator[sqlite3.Connection]:
        """Share one immediate transaction across Room and Session stores."""

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn


class AgentRoomEventHub:
    """Ordered room SSE fan-out backed by AgentRoomStore replay."""

    def __init__(self, store: AgentRoomStore) -> None:
        self.store = store
        self._lock = threading.RLock()
        self._subscribers: dict[str, set[queue.Queue[dict[str, object]]]] = {}
        self._observers: set[Callable[[Mapping[str, object]], None]] = set()

    def publish(self, **values: object) -> dict[str, object]:
        with self._lock:
            event = self.store.append_event(**values)  # type: ignore[arg-type]
        self._fanout(event)
        return event

    def publish_projection(
        self,
        *,
        projection_key: str,
        **values: object,
    ) -> dict[str, object] | None:
        with self._lock:
            event, created = self.store.append_projected_event(
                projection_key=projection_key,
                **values,  # type: ignore[arg-type]
            )
        if created:
            if event is None:
                raise RuntimeError("created Room projection has no event")
            self._fanout(event)
        return event

    def publish_child_terminal(self, **values: object) -> dict[str, object] | None:
        with self._lock:
            event, created = self.store.append_child_terminal_projection(
                **values,  # type: ignore[arg-type]
            )
        if created:
            if event is None:
                raise RuntimeError("created Room child terminal has no event")
            self._fanout(event)
        return event

    def has_projection(self, projection_key: str) -> bool:
        return self.store.has_projection(projection_key)

    def has_tool_terminal(
        self,
        room_id: str,
        tool_call_id: str,
        *,
        root_id: str = "",
        dispatch_id: str = "",
    ) -> bool:
        return self.store.has_tool_terminal(
            room_id,
            tool_call_id,
            root_id=root_id,
            dispatch_id=dispatch_id,
        )

    def _fanout(self, event: dict[str, object]) -> None:
        with self._lock:
            subscribers = tuple(
                self._subscribers.get(str(event["roomId"]), ())
            )
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                    subscriber.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass
        with self._lock:
            observers = tuple(self._observers)
        for observer in observers:
            try:
                observer(event)
            except Exception:
                # Room projections are diagnostic side effects and must never
                # interrupt the primary conversation or intercom delivery.
                pass

    def add_observer(
        self,
        observer: Callable[[Mapping[str, object]], None],
    ) -> Callable[[], None]:
        with self._lock:
            self._observers.add(observer)

        def remove() -> None:
            with self._lock:
                self._observers.discard(observer)

        return remove

    def subscribe(
        self,
        room_id: str,
        *,
        after_event_id: str = "",
        heartbeat_seconds: float = 10.0,
    ) -> Iterator[bytes]:
        after_sequence = _room_event_sequence(room_id, after_event_id)
        if after_event_id and after_sequence is None:
            raise ValueError("room event resume token does not belong to this room")
        subscriber: queue.Queue[dict[str, object]] = queue.Queue(maxsize=128)
        with self._lock:
            first_sequence, last_sequence = self.store.event_bounds(room_id)
            gap = bool(
                after_sequence
                and (
                    after_sequence > last_sequence
                    or (first_sequence > 0 and after_sequence < first_sequence - 1)
                )
            )
            if gap:
                replay = [
                    _room_replay_gap_event(
                        room_id=room_id,
                        after_event_id=after_event_id,
                        last_sequence=last_sequence,
                    )
                ]
            else:
                replay = self.store.list_events(room_id, after_sequence=after_sequence or 0, limit=2000)
            self._subscribers.setdefault(room_id, set()).add(subscriber)
        # A replay-gap control is subscriber-local and therefore cannot advance
        # the durable Room cursor. Anchor live delivery at the shared high-water
        # mark so even a future client cursor can recover after taking a snapshot.
        delivered_sequence = last_sequence if gap else after_sequence or 0
        try:
            yield b": connected\n\n"
            for event in replay:
                if not gap:
                    delivered_sequence = max(delivered_sequence, int(event["sequence"]))
                yield _room_event_sse(event)
            while True:
                try:
                    event = subscriber.get(timeout=heartbeat_seconds)
                    if int(event["sequence"]) <= delivered_sequence:
                        continue
                    delivered_sequence = int(event["sequence"])
                    yield _room_event_sse(event)
                except queue.Empty:
                    yield b": heartbeat\n\n"
        finally:
            with self._lock:
                self._subscribers.get(room_id, set()).discard(subscriber)
                if not self._subscribers.get(room_id):
                    self._subscribers.pop(room_id, None)


def _room_payload(
    row: sqlite3.Row,
    participants: Sequence[sqlite3.Row],
    topics: Sequence[sqlite3.Row],
    artifacts: Sequence[sqlite3.Row],
    work_items: Sequence[sqlite3.Row],
    start_gate: Mapping[str, object] | None = None,
) -> dict[str, object]:
    routing_policy = str(row["routing_mode"] or row["routing_policy"])
    permission_policy = room_permission_policy_from_row(row)
    payload: dict[str, object] = {
        "schemaVersion": "rag-ime.agent-room.v1",
        "id": str(row["id"]),
        "title": str(row["title"]),
        "status": str(row["status"]),
        "roomKind": str(row["room_kind"] or "collaboration"),
        "avatar": str(row["avatar"] or "members"),
        "description": str(row["description"] or ""),
        "scenarioPrompt": str(row["scenario_prompt"] or ""),
        "ownerAppId": str(row["owner_app_id"] or ""),
        "surfaceKey": str(row["surface_key"] or ""),
        "executionMode": permission_policy["room"]["executionMode"],
        "permissionPolicy": permission_policy,
        "routingPolicy": routing_policy,
        "routingConfig": normalize_routing_config(
            json.loads(str(row["routing_config_json"] or "{}"))
        ),
        "moderatorParticipantId": str(row["moderator_participant_id"] or ""),
        "nextSpeakerOrdinal": int(row["next_speaker_ordinal"] or 0),
        "activeTopicId": str(row["active_topic_id"] or ""),
        "configRevision": int(row["config_revision"] or 1),
        "workspaceRoots": [
            str(value)
            for value in json.loads(str(row["workspace_roots_json"] or "[]"))
            if str(value).strip()
        ],
        "createdAtMs": int(row["created_at_ms"]),
        "updatedAtMs": int(row["updated_at_ms"]),
        "lastEventSequence": int(row["last_event_sequence"]),
        "participants": [_participant_payload(item) for item in participants],
        "topics": [_topic_payload(item) for item in topics],
        "artifacts": [_artifact_payload(item) for item in artifacts],
        "workItems": [work_item_payload(item) for item in work_items],
        "startGate": start_gate,
    }
    validate_contract(payload, "agent-room.v1.json")
    return payload


def _room_start_gate_payload(row: sqlite3.Row | None) -> dict[str, object] | None:
    if row is None:
        return None
    return {
        "status": str(row["status"]),
        "objective": str(row["objective_text"] or ""),
        "workItemId": str(row["work_item_id"] or ""),
        "clientMessageId": str(row["client_message_id"] or ""),
        "gateId": f"room-start:{row['room_id']}" if "room_id" in row.keys() else "",
        "rootId": str(row["root_id"] or ""),
        "confirmedAtMs": int(row["confirmed_at_ms"] or 0),
    }




def _participant_payload(row: sqlite3.Row) -> dict[str, object]:
    payload: dict[str, object] = {
        "schemaVersion": "rag-ime.agent-participant.v1",
        "id": str(row["id"]),
        "roomId": str(row["room_id"]),
        "sessionId": str(row["session_id"]),
        "roleId": canonical_agent_role_id(row["role_id"]),
        "roleVersion": str(row["role_version"]),
        "displayName": str(row["display_name"]),
        "collaborationRole": str(
            row["collaboration_role_key"]
            or row["collaboration_role"]
            or "implementer"
        ),
        "status": str(row["participant_status"]),
        "ordinal": int(row["ordinal"]),
        "createdAtMs": int(row["created_at_ms"]),
        "lastSpokeAtMs": int(row["last_spoke_at_ms"]) if row["last_spoke_at_ms"] is not None else None,
    }
    validate_contract(payload, "agent-participant.v1.json")
    return payload


def _topic_payload(row: sqlite3.Row) -> dict[str, object]:
    return {
        "schemaVersion": "rag-ime.agent-room-topic.v1",
        "id": str(row["id"]),
        "roomId": str(row["room_id"]),
        "title": str(row["title"]),
        "summary": str(row["summary"] or ""),
        "status": str(row["topic_status"]),
        "ordinal": int(row["ordinal"]),
        "createdAtMs": int(row["created_at_ms"]),
        "updatedAtMs": int(row["updated_at_ms"]),
    }


def _artifact_payload(row: sqlite3.Row) -> dict[str, object]:
    return {
        "schemaVersion": "rag-ime.agent-room-artifact.v1",
        "id": str(row["id"]),
        "roomId": str(row["room_id"]),
        "topicId": str(row["topic_id"] or ""),
        "path": str(row["path"]),
        "displayName": str(row["display_name"]),
        "mediaType": str(row["media_type"]),
        "byteSize": int(row["byte_size"]),
        "sha256": str(row["sha256"] or ""),
        "revision": int(row["revision"]),
        "status": str(row["artifact_status"]),
        "createdByParticipantId": str(row["created_by_participant_id"] or ""),
        "createdAtMs": int(row["created_at_ms"]),
        "updatedAtMs": int(row["updated_at_ms"]),
    }


def _workspace_roots(values: Sequence[str]) -> list[str]:
    roots: list[str] = []
    for value in values:
        path = str(value or "").strip()
        if not path:
            continue
        normalized = str(Path(path).expanduser().resolve(strict=False))
        if normalized not in roots:
            roots.append(normalized)
    return roots


def _room_surface_identity(owner_app_id: object, surface_key: object) -> tuple[str, str]:
    owner = str(owner_app_id or "").strip()
    key = str(surface_key or "").strip()
    if owner and not _ROOM_OWNER_APP_ID_RE.fullmatch(owner):
        raise ValueError("room ownerAppId must be an extension App id")
    if key and not _ROOM_SURFACE_KEY_RE.fullmatch(key):
        raise ValueError("room surfaceKey is invalid")
    if key and not owner:
        raise ValueError("room surfaceKey requires ownerAppId")
    return owner, key


def _optional_room_owner(value: object) -> str | None:
    if value is None:
        return None
    owner, _ = _room_surface_identity(value, "")
    return owner


def _optional_room_surface_key(value: object) -> str | None:
    if value is None:
        return None
    key = str(value or "").strip()
    if key and not _ROOM_SURFACE_KEY_RE.fullmatch(key):
        raise ValueError("room surfaceKey is invalid")
    return key


def _room_event_payload(row: sqlite3.Row) -> dict[str, object]:
    payload: dict[str, object] = {
        "schemaVersion": "rag-ime.agent-room-event.v1",
        "eventId": str(row["event_id"]),
        "roomId": str(row["room_id"]),
        "sequence": int(row["sequence"]),
        "turnId": str(row["turn_id"] or ""),
        "eventType": str(row["event_type"]),
        "participantId": str(row["participant_id"]) if row["participant_id"] is not None else None,
        "sourceSessionId": str(row["source_session_id"] or ""),
        "topicId": str(row["topic_id"] or ""),
        "createdAtMs": int(row["created_at_ms"]),
        "payload": json.loads(str(row["payload_json"] or "{}")),
        "resumeToken": str(row["event_id"]),
    }
    validate_contract(payload, "agent-room-event.v1.json")
    return payload


def _child_terminal_data(payload: Mapping[str, object]) -> Mapping[str, object]:
    data = payload.get("data")
    return data if isinstance(data, Mapping) else payload


def _matches_child_terminal(
    payload: Mapping[str, object],
    identity: tuple[str, str],
    *,
    phase: object = None,
) -> bool:
    data = _child_terminal_data(payload)
    return (
        data.get("activityKind") == "child"
        and data.get("phase") in {"completed", "failed", "aborted"}
        and (phase is None or data.get("phase") == phase)
        and str(data.get("dispatchId") or data.get("childDispatchId") or "") == identity[0]
        and str(payload.get("sourceEventId") or payload.get("sourceRuntimeEventId") or "") == identity[1]
    )


def _room_projection_hash(
    *,
    room_id: str,
    event_type: str,
    payload: Mapping[str, object],
    turn_id: str,
    participant_id: str | None,
    source_session_id: str,
    topic_id: str,
) -> str:
    material = {
        "roomId": room_id,
        "eventType": event_type,
        "turnId": str(turn_id or ""),
        "participantId": participant_id,
        "sourceSessionId": str(source_session_id or ""),
        "topicId": str(topic_id or ""),
        "payload": dict(payload),
    }
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _room_event_sse(event: Mapping[str, object]) -> bytes:
    body = json.dumps(dict(event), ensure_ascii=False, separators=(",", ":"))
    return (
        f"id: {event['resumeToken']}\n"
        f"event: {event['eventType']}\n"
        f"data: {body}\n\n"
    ).encode("utf-8")


def _room_replay_gap_event(
    *,
    room_id: str,
    after_event_id: str,
    last_sequence: int,
) -> dict[str, object]:
    """Build a transient recovery control without moving shared Room history."""

    cursor = max(0, int(last_sequence))
    resume_token = f"{room_id}:{cursor}"
    event = {
        "schemaVersion": "rag-ime.agent-room-event.v1",
        "eventId": f"{room_id}:snapshot-required:{uuid.uuid4().hex}",
        "roomId": room_id,
        "sequence": cursor + 1,
        "turnId": "",
        "eventType": "snapshot_required",
        "participantId": None,
        "sourceSessionId": "",
        "topicId": "",
        "createdAtMs": _timestamp(None),
        "payload": {
            "reason": "room_event_replay_gap",
            "afterEventId": after_event_id,
        },
        "resumeToken": resume_token,
    }
    validate_contract(event, "agent-room-event.v1.json")
    return event


def _room_event_sequence(room_id: str, event_id: str) -> int | None:
    if not event_id:
        return 0
    prefix = f"{room_id}:"
    if not event_id.startswith(prefix):
        return None
    try:
        return int(event_id[len(prefix) :])
    except ValueError:
        return None


def _authorized_artifact_path(value: str, workspace_roots: object) -> Path:
    path = Path(str(value or "")).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError("room artifact must reference a regular file")
    roots = [
        Path(str(root)).expanduser().resolve(strict=False)
        for root in workspace_roots
        if str(root or "").strip()
    ] if isinstance(workspace_roots, Sequence) and not isinstance(workspace_roots, (str, bytes)) else []
    if not any(_is_relative_to(path, root) for root in roots):
        raise ValueError("room artifact must stay inside an authorized workspace")
    return path


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _legacy_policy(policy: str) -> str:
    return policy if policy in {"manual_mentions", "moderator"} else "manual_mentions"


def _required_text(payload: Mapping[str, object], key: str) -> str:
    value = " ".join(str(payload.get(key) or "").split())
    if not value:
        raise ValueError(f"missing required field: {key}")
    return value


def _required_text_value(value: object, field: str, maximum: int) -> str:
    normalized = " ".join(str(value or "").split())
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    if len(normalized) > maximum:
        raise ValueError(f"{field} must not exceed {maximum} characters")
    return normalized


def normalize_collaboration_role(value: object, *, assignable_only: bool = False) -> str:
    normalized = canonical_collaboration_role_id(value)
    if normalized not in ROOM_COLLABORATION_ROLES:
        raise ValueError("unsupported room collaboration role")
    if assignable_only and normalized not in ASSIGNABLE_COLLABORATION_ROLES:
        raise ValueError(
            "room collaboration role is retained for history only and cannot be assigned"
        )
    return normalized


def _legacy_collaboration_role(value: str) -> str:
    if value in {"coordinator", "researcher"}:
        return value
    return "executor"


def _timestamp(value: int | None) -> int:
    return int(value if value is not None else time.time() * 1000)
