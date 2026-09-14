from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Iterable, Mapping, cast
from urllib.parse import quote

from .agent_approval_model import pending_model_arbitration
from .agent_role_identity import (
    canonical_agent_role_id,
    canonical_role_book_revision_id,
)
from .agent_execution_policy import (
    FULL_TRUST_EXECUTION_MODE,
    PER_ACTION_EXECUTION_MODE,
    READ_ONLY_EXECUTION_MODE,
    ROOM_UNRESTRICTED_EXECUTION_MODE,
    WORKSPACE_MANAGED_EXECUTION_MODE,
    canonical_tool_profile,
    normalize_execution_mode,
    workspace_scope_is_granted,
    workspace_scope_sha256,
)
from .agent_events import AgentTerminalProjectionDuplicate
from .agent_tool_ids import (
    DANGEROUS_AUTO_APPROVE_TOOL_PROFILE,
    FULL_ACCESS_TOOL_PROFILE,
    SUPPORTED_AGENT_TOOL_PROFILES,
)
from .agent_workspace_roots import (
    exact_trace_project_workspace_roots,
    is_trace_project_bound_surface,
    system_wide_workspace_roots,
)
from .contracts.json_schema import validate_contract
from .db import apply_database_migrations


class AgentSessionNotFound(KeyError):
    pass


class AgentApprovalNotFound(KeyError):
    pass


class AgentGoalExecutionBlocked(ValueError):
    """Configured Goal cannot incur new model or delegation work.

    ``error_code`` stays lowercase (`goal_paused`, …) so Session receipts and
    the workflow actGate share one spelling. Room event projection may
    uppercase when publishing durable causeCode fields.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"Goal execution blocked ({reason}): {message}")
        self.error_code = reason


_EXTENSION_APP_ID = re.compile(r"^extension:[a-z0-9][a-z0-9-]{0,63}$")
_SURFACE_KEY = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_BUILTIN_APP_IDS = frozenset({"memory"})
_MEMORY_JOURNAL_KEY = re.compile(r"^journal-(\d{4}-\d{2}-\d{2})$")


_SESSION_SELECT = """
SELECT
    s.*,
    b.driver_id AS runtime_driver_id,
    b.runtime_kind AS runtime_kind,
    b.generation AS runtime_generation,
    b.binding_state AS runtime_binding_state,
    b.created_at_ms AS runtime_binding_created_at_ms,
    b.updated_at_ms AS runtime_binding_updated_at_ms,
    tp.allowed_tools_json AS allowed_tools_json,
    tp.disclosure_preferences_json AS disclosure_preferences_json,
    tp.policy_revision AS policy_revision,
    COALESCE((
        SELECT terminal.turn_id
        FROM agent_runtime_events AS terminal
        WHERE terminal.session_id = s.id
          AND terminal.event_type IN ('turn_completed', 'turn_failed')
          AND terminal.turn_id <> ''
        ORDER BY terminal.sequence DESC
        LIMIT 1
    ), '') AS last_terminal_turn_id
FROM agent_sessions AS s
LEFT JOIN agent_runtime_bindings AS b ON b.session_id = s.id
LEFT JOIN agent_session_tool_policies AS tp ON tp.session_id = s.id
"""

_SESSION_DIRECTORY_SELECT = """
SELECT
    s.id,
    s.title,
    s.session_mode,
    s.status,
    s.session_kind,
    s.surface_kind,
    s.owner_app_id,
    s.surface_key,
    s.role_id,
    s.role_version,
    s.model_profile,
    s.thinking_level,
    s.tool_profile_version,
    s.execution_mode,
    s.room_execution_mode,
    s.workspace_scope_sha256,
    s.workspace_scope_granted_at_ms,
    s.project_context_enabled,
    s.pi_skills_enabled,
    s.codex_skills_enabled,
    s.updated_at_ms,
    s.message_count,
    s.last_message_preview,
    s.workspace_roots_json
FROM agent_sessions AS s
"""


class AgentSessionStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        persistent_reads: bool = False,
    ):
        self.db_path = Path(db_path)
        self._persistent_reads = bool(persistent_reads)
        self._read_lock = RLock()
        self._read_connection: sqlite3.Connection | None = None
        self._approval_creation_context: ContextVar[
            dict[str, object] | None
        ] = ContextVar(
            "rag_ime_agent_approval_creation_context",
            default=None,
        )

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
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

    def create_child(self, parent_session_id: str, **kwargs: Any) -> dict[str, object]:
        """Create from an explicit persisted parent, retaining the normal store owner."""
        self.get(parent_session_id)
        return self.create(**kwargs)

    def create(
        self,
        *,
        title: str,
        mode: str = "assistant",
        role_id: str = "companion-future-v1",
        role_version: str = "1",
        role_book_revision_id: str = "",
        model_profile: str = "openai-codex/gpt-5.6-sol",
        thinking_level: str = "max",
        tool_profile_version: str = "control-center-v1",
        execution_mode: str | None = None,
        room_execution_mode: str | None = None,
        project_context_enabled: bool = False,
        pi_skills_enabled: bool = False,
        codex_skills_enabled: bool = False,
        workspace_roots: Iterable[str] = (),
        shell_policy_version: str | None = None,
        session_kind: str = "conversation",
        evaluation_snapshot: bool = False,
        surface_kind: str = "agent",
        owner_app_id: str = "",
        surface_key: str = "",
        created_at_ms: int | None = None,
        _connection: sqlite3.Connection | None = None,
    ) -> dict[str, object]:
        if mode not in {"assistant", "coordinator"}:
            raise ValueError("agent session mode must be assistant or coordinator")
        normalized_title = " ".join(str(title).split())[:120]
        if not normalized_title:
            raise ValueError("agent session title must not be empty")
        normalized_model_profile = str(model_profile or "").strip()
        if not normalized_model_profile:
            raise ValueError("agent session model profile must not be empty")
        roots = _workspace_roots(workspace_roots)
        normalized_kind = str(session_kind or "").strip()
        if normalized_kind not in {"conversation", "subagent_runtime"}:
            raise ValueError("agent session kind must be conversation or subagent_runtime")
        if evaluation_snapshot and normalized_kind != "conversation":
            raise ValueError("evaluation snapshots must use the conversation session kind")
        normalized_surface, normalized_owner_app_id, normalized_surface_key = (
            _surface_ownership(
                surface_kind,
                owner_app_id,
                surface_key,
                require_surface_key=True,
            )
        )
        if normalized_kind != "conversation" and normalized_surface != "agent":
            raise ValueError("internal Agent sessions cannot be owned by an App surface")
        normalized_thinking = str(thinking_level or "").strip().lower()
        if normalized_thinking not in {
            "",
            "off",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        }:
            raise ValueError("agent thinking level is not supported")
        normalized_execution_mode = normalize_execution_mode(
            execution_mode,
            tool_profile_version=tool_profile_version,
        )
        normalized_room_execution_mode = _normalize_room_execution_mode(
            room_execution_mode
        )
        normalized_tool_profile = canonical_tool_profile(
            tool_profile_version,
            execution_mode=normalized_execution_mode,
        )
        unrestricted_profile = normalized_tool_profile in {
            DANGEROUS_AUTO_APPROVE_TOOL_PROFILE,
            FULL_ACCESS_TOOL_PROFILE,
        }
        trace_project_binding = is_trace_project_bound_surface(
            normalized_surface,
            normalized_owner_app_id,
            normalized_surface_key,
        )
        if trace_project_binding:
            roots = list(exact_trace_project_workspace_roots(roots))
        elif unrestricted_profile:
            roots = list(system_wide_workspace_roots(roots))
            project_context_enabled = True
            pi_skills_enabled = True
            codex_skills_enabled = True
        if normalized_tool_profile not in SUPPORTED_AGENT_TOOL_PROFILES:
            raise ValueError("unsupported Agent tool profile")
        read_only_subagent = (
            normalized_kind == "subagent_runtime"
            and mode == "assistant"
            and normalized_tool_profile == "subagent-readonly-v1"
            and normalized_execution_mode == "read_only"
        )
        if mode == "assistant" and roots and not read_only_subagent:
            raise ValueError(
                "assistant conversation sessions cannot carry workspace roots"
            )
        if normalized_execution_mode in {
            WORKSPACE_MANAGED_EXECUTION_MODE,
            FULL_TRUST_EXECUTION_MODE,
        } and (mode != "coordinator" or not roots):
            raise ValueError(
                "managed and full-trust execution require a coordinator workspace"
            )
        timestamp = int(created_at_ms if created_at_ms is not None else time.time() * 1000)
        scope_sha256 = (
            workspace_scope_sha256(roots)
            if normalized_execution_mode
            in {
                WORKSPACE_MANAGED_EXECUTION_MODE,
                FULL_TRUST_EXECUTION_MODE,
            }
            else ""
        )
        scope_granted_at_ms = timestamp if scope_sha256 else 0
        session_id = f"agent:{uuid.uuid4()}"
        normalized_role_id = canonical_agent_role_id(role_id)
        normalized_role_book_revision_id = canonical_role_book_revision_id(
            role_book_revision_id
        )
        if len(normalized_role_book_revision_id) > 240:
            raise ValueError("agent role book revision id is too long")
        shell_policy = shell_policy_version or (
            "coordinator-per-command-v1" if mode == "coordinator" else "assistant-no-shell-v1"
        )
        values = (
            session_id,
            normalized_title,
            mode,
            normalized_role_id,
            role_version,
            normalized_role_book_revision_id,
            normalized_model_profile,
            normalized_thinking,
            normalized_tool_profile,
            normalized_execution_mode,
            normalized_room_execution_mode,
            scope_sha256,
            scope_granted_at_ms,
            1 if project_context_enabled else 0,
            1 if pi_skills_enabled else 0,
            1 if codex_skills_enabled else 0,
            json.dumps(roots, ensure_ascii=False, separators=(",", ":")),
            shell_policy,
            normalized_kind,
            1 if evaluation_snapshot else 0,
            normalized_surface,
            normalized_owner_app_id,
            normalized_surface_key,
            timestamp,
            timestamp,
            timestamp,
        )

        def insert(conn: sqlite3.Connection) -> dict[str, object]:
            conn.execute(
                """
                INSERT INTO agent_sessions(
                    id, title, session_mode, role_id, role_version, role_book_revision_id,
                    model_profile, thinking_level,
                    tool_profile_version, execution_mode, room_execution_mode,
                    workspace_scope_sha256, workspace_scope_granted_at_ms,
                    project_context_enabled,
                    pi_skills_enabled, codex_skills_enabled, workspace_roots_json,
                    shell_policy_version, session_kind, evaluation_snapshot,
                    surface_kind, owner_app_id, surface_key,
                    created_at_ms, updated_at_ms,
                    last_opened_at_ms, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'idle')
                """,
                values,
            )
            return self._get(conn, session_id)

        if _connection is not None:
            return insert(_connection)
        with self._connect() as conn:
            return insert(conn)

    def ensure_surface_session(
        self,
        *,
        surface_kind: str,
        owner_app_id: str,
        surface_key: str,
        create: Callable[[sqlite3.Connection], Mapping[str, object]],
    ) -> tuple[bool, dict[str, object]]:
        normalized_surface, normalized_owner, normalized_key = _surface_ownership(
            surface_kind,
            owner_app_id,
            surface_key,
            require_surface_key=True,
        )
        if normalized_surface == "agent":
            raise ValueError("surface ensure requires an App-owned Session")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"""
                {_SESSION_SELECT}
                WHERE s.session_kind = 'conversation'
                  AND s.status <> 'archived'
                  AND s.surface_kind = ?
                  AND s.owner_app_id = ?
                  AND s.surface_key = ?
                ORDER BY s.updated_at_ms DESC, s.id DESC
                LIMIT 1
                """,
                (normalized_surface, normalized_owner, normalized_key),
            ).fetchone()
            if row is not None:
                return False, _session_payload(row, _joined_runtime_binding(row))
            created = dict(create(conn))
            if (
                created.get("sessionKind") != "conversation"
                or created.get("surfaceKind") != normalized_surface
                or created.get("ownerAppId") != normalized_owner
                or created.get("surfaceKey") != normalized_key
            ):
                raise RuntimeError("surface Session creator returned a mismatched Session")
            return True, created

    def set_role_book_revision(
        self,
        session_id: str,
        revision_id: str,
        *,
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        normalized = canonical_role_book_revision_id(revision_id)
        if not normalized:
            raise ValueError("agent role book revision id must not be empty")
        if len(normalized) > 240:
            raise ValueError("agent role book revision id is too long")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT role_book_revision_id FROM agent_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise AgentSessionNotFound(session_id)
            current = str(row["role_book_revision_id"] or "")
            if current and current != normalized:
                raise ValueError(
                    "agent session role book revision is immutable after pinning"
                )
            if not current:
                conn.execute(
                    """
                    UPDATE agent_sessions
                    SET role_book_revision_id = ?, updated_at_ms = ?
                    WHERE id = ? AND role_book_revision_id = ''
                    """,
                    (normalized, _timestamp(updated_at_ms), session_id),
                )
                selected = conn.execute(
                    "SELECT role_book_revision_id FROM agent_sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if selected is None or str(selected[0]) != normalized:
                    raise RuntimeError("agent session role book pin changed concurrently")
        return self.get(session_id)

    def get(self, session_id: str) -> dict[str, object]:
        with self._connect() as conn:
            return self._get(conn, session_id)

    @staticmethod
    def _get(
        conn: sqlite3.Connection,
        session_id: str,
    ) -> dict[str, object]:
        row = conn.execute(
            f"{_SESSION_SELECT} WHERE s.id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise AgentSessionNotFound(session_id)
        return _session_payload(row, _joined_runtime_binding(row))

    def list(
        self,
        *,
        include_archived: bool = False,
        include_internal: bool = False,
        limit: int = 100,
        before_updated_at_ms: int | None = None,
        before_id: str | None = None,
        surface_kind: str | None = None,
        owner_app_id: str = "",
        surface_key: str = "",
    ) -> list[dict[str, object]]:
        page = self.list_page(
            include_archived=include_archived,
            include_internal=include_internal,
            limit=limit,
            before_updated_at_ms=before_updated_at_ms,
            before_id=before_id,
            surface_kind=surface_kind,
            owner_app_id=owner_app_id,
            surface_key=surface_key,
        )
        return cast(list[dict[str, object]], page["items"])

    def list_page(
        self,
        *,
        include_archived: bool = False,
        include_internal: bool = False,
        limit: int = 100,
        before_updated_at_ms: int | None = None,
        before_id: str | None = None,
        surface_kind: str | None = None,
        owner_app_id: str = "",
        surface_key: str = "",
        projection_only: bool = False,
    ) -> dict[str, object]:
        """Return a stable page of Sessions ordered by recency.

        ``before_updated_at_ms`` and ``before_id`` form one keyset cursor.
        The id tie-breaker is required because multiple Sessions can be
        touched during the same millisecond. ``list`` remains the legacy
        array-shaped API for internal callers; this page-shaped method is the
        listing seam used by the HTTP application service.
        """
        bounded_limit = max(1, min(int(limit), 500))
        if surface_kind is None or not str(surface_kind).strip():
            if str(owner_app_id or "").strip() or str(surface_key or "").strip():
                raise ValueError("session surface filters require a surface kind")
            normalized_surface = ""
            normalized_owner_app_id = ""
            normalized_surface_key = ""
        else:
            normalized_surface, normalized_owner_app_id, normalized_surface_key = (
                _surface_ownership(
                    surface_kind,
                    owner_app_id,
                    surface_key,
                    require_surface_key=False,
                )
            )
        clauses = [] if include_archived else ["s.status <> 'archived'"]
        if not include_internal:
            clauses.extend(
                [
                    "s.session_kind = 'conversation'",
                    "s.id NOT IN (SELECT child_session_id FROM agent_subagent_runs)",
                ]
            )
        query_params: list[object] = []
        if normalized_surface:
            clauses.append("s.surface_kind = ?")
            query_params.append(normalized_surface)
        if normalized_surface in {"extension_app", "builtin_app"}:
            clauses.append("s.owner_app_id = ?")
            query_params.append(normalized_owner_app_id)
            if normalized_surface_key:
                clauses.append("s.surface_key = ?")
                query_params.append(normalized_surface_key)
        normalized_before_id = str(before_id or "").strip()
        if before_updated_at_ms is not None and normalized_before_id:
            clauses.append(
                "(s.updated_at_ms < ? OR "
                "(s.updated_at_ms = ? AND s.id < ?))"
            )
            query_params.extend(
                [before_updated_at_ms, before_updated_at_ms, normalized_before_id]
            )
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query_params.append(bounded_limit + 1)
        select = _SESSION_DIRECTORY_SELECT if projection_only else _SESSION_SELECT
        with self._read_connect() as conn:
            rows = conn.execute(
                f"{select} {where} "
                "ORDER BY s.updated_at_ms DESC, s.id DESC LIMIT ?",  # noqa: S608
                query_params,
            ).fetchall()
        has_more = len(rows) > bounded_limit
        page_rows = rows[:bounded_limit]
        items = (
            [_session_directory_payload(row) for row in page_rows]
            if projection_only
            else [
                _session_payload(row, _joined_runtime_binding(row))
                for row in page_rows
            ]
        )
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

    def search_history(
        self,
        *,
        query: str = "",
        requester_session_id: str = "",
        include_archived: bool = False,
        limit: int = 20,
    ) -> list[dict[str, object]]:
        """Return bounded, content-minimized navigation anchors for Sessions.

        This is deliberately not a transcript reader. It searches only the
        Session title, the already-redacted last-message preview, and durable
        runtime-event summaries. The result is enough to navigate to a
        historical Session/turn without copying raw prompts, Tool output, or
        machine-local transcript paths into the current model context.
        """

        normalized_query = " ".join(str(query or "").split())[:240]
        bounded_limit = max(1, min(int(limit), 50))
        where = [
            "s.session_kind = 'conversation'",
            "s.surface_kind = 'agent'",
            "s.id NOT IN (SELECT child_session_id FROM agent_subagent_runs)",
        ]
        params: list[object] = []
        if not include_archived:
            where.append("s.status <> 'archived'")
        event_match = "1 = 1"
        if normalized_query:
            escaped = _like_pattern(normalized_query)
            like_value = f"%{escaped}%"
            where.append(
                """
                (
                    s.title LIKE ? ESCAPE '\\'
                    OR s.last_message_preview LIKE ? ESCAPE '\\'
                    OR EXISTS (
                        SELECT 1
                        FROM agent_runtime_events AS matched
                        WHERE matched.session_id = s.id
                          AND matched.redacted_summary LIKE ? ESCAPE '\\'
                    )
                )
                """
            )
            params.extend((like_value, like_value, like_value))
            event_match = "e.redacted_summary LIKE ? ESCAPE '\\'"
            params.extend((like_value, like_value, like_value))
        params.append(bounded_limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    s.id,
                    s.title,
                    s.status,
                    s.session_mode,
                    s.created_at_ms,
                    s.updated_at_ms,
                    s.message_count,
                    s.last_message_preview,
                    COALESCE((
                        SELECT e.event_id
                        FROM agent_runtime_events AS e
                        WHERE e.session_id = s.id AND {event_match}
                        ORDER BY e.sequence DESC
                        LIMIT 1
                    ), '') AS anchor_event_id,
                    COALESCE((
                        SELECT e.turn_id
                        FROM agent_runtime_events AS e
                        WHERE e.session_id = s.id AND {event_match}
                        ORDER BY e.sequence DESC
                        LIMIT 1
                    ), '') AS anchor_turn_id,
                    COALESCE((
                        SELECT e.redacted_summary
                        FROM agent_runtime_events AS e
                        WHERE e.session_id = s.id AND {event_match}
                        ORDER BY e.sequence DESC
                        LIMIT 1
                    ), '') AS anchor_summary
                FROM agent_sessions AS s
                WHERE {' AND '.join(where)}
                ORDER BY
                    CASE WHEN s.id = ? THEN 0 ELSE 1 END,
                    s.updated_at_ms DESC,
                    s.id ASC
                LIMIT ?
                """,  # noqa: S608 -- clauses are static; values remain bound.
                (
                    *params[:-1],
                    str(requester_session_id or ""),
                    params[-1],
                ),
            ).fetchall()

        anchors: list[dict[str, object]] = []
        for row in rows:
            session_id = str(row["id"])
            event_id = str(row["anchor_event_id"] or "")
            turn_id = str(row["anchor_turn_id"] or "")
            event_summary = " ".join(str(row["anchor_summary"] or "").split())[:240]
            last_preview = " ".join(
                str(row["last_message_preview"] or "").split()
            )[:240]
            preview = event_summary or last_preview
            if normalized_query and event_summary:
                match_kind = "runtime_event"
            elif normalized_query and normalized_query.casefold() in str(row["title"]).casefold():
                match_kind = "title"
            elif normalized_query:
                match_kind = "last_message"
            else:
                match_kind = "recent"
            anchors.append(
                {
                    "schemaVersion": "rag-ime.agent-session-anchor.v1",
                    "sessionId": session_id,
                    "title": str(row["title"]),
                    "mode": str(row["session_mode"]),
                    "status": str(row["status"]),
                    "messageCount": int(row["message_count"]),
                    "createdAtMs": int(row["created_at_ms"]),
                    "updatedAtMs": int(row["updated_at_ms"]),
                    "matchKind": match_kind,
                    "preview": preview,
                    "eventId": event_id,
                    "turnId": turn_id,
                    "isCurrentSession": session_id == str(requester_session_id or ""),
                    "href": f"#/agent?session={_url_query_value(session_id)}",
                    "evidenceRef": (
                        f"{session_id}#event:{event_id}"
                        if event_id
                        else session_id
                    ),
                }
            )
        return anchors

    def runtime_binding(self, session_id: str) -> dict[str, object] | None:
        self.get(session_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_runtime_bindings WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return _runtime_binding_payload(row) if row is not None else None

    def recent_message_projection(
        self,
        session_id: str,
    ) -> dict[str, object] | None:
        """Read one bounded, non-authoritative recent-message projection."""

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT transcript_ref, external_session_id, branch_anchor,
                       transcript_device, transcript_inode, transcript_size,
                       transcript_mtime_ns, transcript_boundary_sha256,
                       messages_json, updated_at_ms
                FROM agent_recent_message_projections
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            decoded = json.loads(str(row["messages_json"] or "[]"))
        except json.JSONDecodeError:
            return None
        if isinstance(decoded, list):
            # v1 cached only messages and cannot restore ordering metadata or
            # Tool/thinking progress. Treat it as stale acceleration data so
            # the bounded transcript reader replaces it with the v2 envelope.
            return None
        if isinstance(decoded, Mapping):
            raw_messages = decoded.get("messages")
            raw_tool_history_events = decoded.get("toolHistoryEvents")
        else:
            return None
        if not isinstance(raw_messages, list) or not all(
            isinstance(message, Mapping)
            for message in raw_messages
        ):
            return None
        if not isinstance(raw_tool_history_events, list) or not all(
            isinstance(event, Mapping)
            for event in raw_tool_history_events
        ):
            return None
        return {
            "transcriptRef": str(row["transcript_ref"]),
            "externalSessionId": str(row["external_session_id"]),
            "branchAnchor": str(row["branch_anchor"]),
            "transcriptDevice": int(row["transcript_device"]),
            "transcriptInode": int(row["transcript_inode"]),
            "transcriptSize": int(row["transcript_size"]),
            "transcriptMtimeNs": int(row["transcript_mtime_ns"]),
            "transcriptBoundarySha256": str(
                row["transcript_boundary_sha256"]
            ),
            "messages": [dict(message) for message in raw_messages],
            "toolHistoryEvents": [
                dict(event) for event in raw_tool_history_events
            ],
            "updatedAtMs": int(row["updated_at_ms"]),
        }

    def save_recent_message_projection(
        self,
        session_id: str,
        *,
        transcript_ref: str,
        external_session_id: str,
        branch_anchor: str,
        transcript_device: int,
        transcript_inode: int,
        transcript_size: int,
        transcript_mtime_ns: int,
        transcript_boundary_sha256: str,
        messages: Sequence[Mapping[str, object]],
        tool_history_events: Sequence[Mapping[str, object]] = (),
        updated_at_ms: int | None = None,
    ) -> None:
        """Replace the cache only for one exact immutable transcript view."""

        normalized_messages = [dict(message) for message in messages]
        normalized_tool_history_events = [
            dict(event) for event in tool_history_events
        ]
        encoded = json.dumps(
            {
                "messages": normalized_messages,
                "toolHistoryEvents": normalized_tool_history_events,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(encoded.encode("utf-8")) > 64 * 1024:
            raise ValueError("recent message projection exceeds 64 KiB")
        timestamp = _timestamp(updated_at_ms)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_recent_message_projections(
                    session_id, transcript_ref, external_session_id,
                    branch_anchor, transcript_device, transcript_inode,
                    transcript_size, transcript_mtime_ns,
                    transcript_boundary_sha256, messages_json, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    transcript_ref = excluded.transcript_ref,
                    external_session_id = excluded.external_session_id,
                    branch_anchor = excluded.branch_anchor,
                    transcript_device = excluded.transcript_device,
                    transcript_inode = excluded.transcript_inode,
                    transcript_size = excluded.transcript_size,
                    transcript_mtime_ns = excluded.transcript_mtime_ns,
                    transcript_boundary_sha256 = excluded.transcript_boundary_sha256,
                    messages_json = excluded.messages_json,
                    updated_at_ms = excluded.updated_at_ms
                """,
                (
                    session_id,
                    str(transcript_ref),
                    str(external_session_id),
                    str(branch_anchor),
                    int(transcript_device),
                    int(transcript_inode),
                    max(0, int(transcript_size)),
                    max(0, int(transcript_mtime_ns)),
                    str(transcript_boundary_sha256),
                    encoded,
                    timestamp,
                ),
            )

    def invalidate_recent_message_projection(self, session_id: str) -> None:
        """Drop a derived recent window before an in-place branch rewrite."""

        with self._connect() as conn:
            conn.execute(
                "DELETE FROM agent_recent_message_projections WHERE session_id = ?",
                (session_id,),
            )

    def bind_runtime_session(
        self,
        session_id: str,
        *,
        driver_id: str,
        runtime_kind: str,
        external_session_id: str,
        transcript_ref: str = "",
        branch_anchor: str = "",
        binding_state: str = "active",
        metadata: Mapping[str, object] | None = None,
        message_count: int = 0,
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        driver = _runtime_binding_text(driver_id, field="driverId", maximum=120)
        kind = _runtime_binding_text(runtime_kind, field="runtimeKind", maximum=80)
        external_id = _runtime_binding_text(
            external_session_id,
            field="externalSessionId",
            maximum=240,
        )
        transcript = _runtime_binding_text(
            transcript_ref,
            field="transcriptRef",
            maximum=4096,
            required=False,
        )
        anchor = _runtime_binding_text(
            branch_anchor,
            field="branchAnchor",
            maximum=240,
            required=False,
        )
        if binding_state not in {"prepared", "active", "stale"}:
            raise ValueError("runtime binding state must be prepared, active, or stale")
        try:
            metadata_json = json.dumps(
                dict(metadata or {}),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("runtime binding metadata must be JSON serializable") from exc
        if len(metadata_json.encode("utf-8")) > 16_384:
            raise ValueError("runtime binding metadata is too large")
        timestamp = _timestamp(updated_at_ms)
        with self._connect() as conn:
            session = conn.execute(
                "SELECT id FROM agent_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise AgentSessionNotFound(session_id)
            existing = conn.execute(
                "SELECT * FROM agent_runtime_bindings WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if existing is not None and (
                str(existing["driver_id"]) != driver
                or str(existing["runtime_kind"]) != kind
            ):
                raise ValueError("an Agent session cannot change runtime driver in place")
            if existing is not None and not anchor:
                anchor = str(existing["branch_anchor"] or "")
            generation = int(existing["generation"]) + 1 if existing is not None else 1
            created_at_ms = int(existing["created_at_ms"]) if existing is not None else timestamp
            try:
                conn.execute(
                    """
                    INSERT INTO agent_runtime_bindings(
                        session_id, driver_id, runtime_kind, external_session_id,
                        transcript_ref, branch_anchor, generation, binding_state,
                        metadata_json, created_at_ms, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        external_session_id = excluded.external_session_id,
                        transcript_ref = excluded.transcript_ref,
                        branch_anchor = excluded.branch_anchor,
                        generation = excluded.generation,
                        binding_state = excluded.binding_state,
                        metadata_json = excluded.metadata_json,
                        updated_at_ms = excluded.updated_at_ms
                    """,
                    (
                        session_id,
                        driver,
                        kind,
                        external_id,
                        transcript,
                        anchor,
                        generation,
                        binding_state,
                        metadata_json,
                        created_at_ms,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("runtime session is already bound to another Agent session") from exc
            assignments = ["updated_at_ms = ?"]
            values: list[object] = [timestamp]
            if binding_state == "active":
                assignments.extend(
                    ["status = 'active'", "last_opened_at_ms = ?", "message_count = ?"]
                )
                values.extend([timestamp, max(0, int(message_count))])
            if kind == "pi_rpc" and driver == "managed-pi":
                assignments.extend(["pi_session_id = ?", "session_file = ?"])
                values.extend([external_id, transcript])
            conn.execute(
                f"UPDATE agent_sessions SET {', '.join(assignments)} WHERE id = ?",  # noqa: S608
                (*values, session_id),
            )
        return self.get(session_id)

    def prepare_session_file(
        self,
        session_id: str,
        *,
        pi_session_id: str,
        session_file: str,
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        """Attach a prebuilt managed Pi branch without marking it active."""

        if not str(pi_session_id).strip() or not str(session_file).strip():
            raise ValueError("prepared Pi session identity and file must not be empty")
        return self.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id=pi_session_id,
            transcript_ref=session_file,
            binding_state="prepared",
            updated_at_ms=updated_at_ms,
        )

    def rename(self, session_id: str, title: str, *, updated_at_ms: int | None = None) -> dict[str, object]:
        normalized = " ".join(str(title).split())[:120]
        if not normalized:
            raise ValueError("agent session title must not be empty")
        self._update(
            session_id,
            "title = ?, updated_at_ms = ?",
            (normalized, _timestamp(updated_at_ms)),
        )
        return self.get(session_id)

    def set_model_profile(
        self,
        session_id: str,
        model_profile: str,
        *,
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        normalized = "/".join(part.strip() for part in str(model_profile).split("/", 1))
        if "/" not in normalized or normalized.startswith("/") or normalized.endswith("/"):
            raise ValueError("agent model profile must be provider/model")
        if len(normalized) > 240:
            raise ValueError("agent model profile is too long")
        self._update(
            session_id,
            "model_profile = ?, updated_at_ms = ?",
            (normalized, _timestamp(updated_at_ms)),
        )
        return self.get(session_id)

    def set_thinking_level(
        self,
        session_id: str,
        thinking_level: str,
        *,
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        normalized = str(thinking_level or "").strip().lower()
        if normalized not in {
            "off",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        }:
            raise ValueError("agent thinking level is not supported")
        self._update(
            session_id,
            "thinking_level = ?, updated_at_ms = ?",
            (normalized, _timestamp(updated_at_ms)),
        )
        return self.get(session_id)

    def set_mode(
        self,
        session_id: str,
        mode: str,
        *,
        workspace_roots: Iterable[str] | None = None,
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        normalized_mode = str(mode or "").strip()
        if normalized_mode not in {"assistant", "coordinator"}:
            raise ValueError("agent session mode must be assistant or coordinator")
        current = self.get(session_id)
        roots = _workspace_roots(
            current.get("workspaceRoots", []) if workspace_roots is None else workspace_roots
        )
        read_only_subagent = (
            str(current.get("sessionKind") or "conversation") == "subagent_runtime"
            and normalized_mode == "assistant"
            and str(current.get("toolProfileVersion") or "") == "subagent-readonly-v1"
        )
        if normalized_mode == "assistant" and not read_only_subagent:
            roots = []
        shell_policy = (
            "coordinator-per-command-v1"
            if normalized_mode == "coordinator"
            else "assistant-no-shell-v1"
        )
        self._update(
            session_id,
            "session_mode = ?, execution_mode = ?, workspace_scope_sha256 = '', "
            "workspace_scope_granted_at_ms = 0, workspace_roots_json = ?, "
            "shell_policy_version = ?, updated_at_ms = ?",
            (
                normalized_mode,
                PER_ACTION_EXECUTION_MODE,
                json.dumps(roots, ensure_ascii=False, separators=(",", ":")),
                shell_policy,
                _timestamp(updated_at_ms),
            ),
        )
        return self.get(session_id)

    def set_runtime_policy(
        self,
        session_id: str,
        *,
        mode: str,
        tool_profile_version: str,
        execution_mode: str | None = None,
        room_execution_mode: str | None = None,
        grant_workspace_scope: bool = False,
        allowed_tools: Iterable[str] | None,
        project_context_enabled: bool | None = None,
        pi_skills_enabled: bool | None = None,
        codex_skills_enabled: bool | None = None,
        workspace_roots: Iterable[str] | None = None,
        updated_at_ms: int | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, object]:
        if connection is not None:
            return self._set_runtime_policy(
                connection,
                session_id,
                mode=mode,
                tool_profile_version=tool_profile_version,
                execution_mode=execution_mode,
                room_execution_mode=room_execution_mode,
                grant_workspace_scope=grant_workspace_scope,
                allowed_tools=allowed_tools,
                project_context_enabled=project_context_enabled,
                pi_skills_enabled=pi_skills_enabled,
                codex_skills_enabled=codex_skills_enabled,
                workspace_roots=workspace_roots,
                updated_at_ms=updated_at_ms,
            )
        with self._connect() as conn:
            self._set_runtime_policy(
                conn,
                session_id,
                mode=mode,
                tool_profile_version=tool_profile_version,
                execution_mode=execution_mode,
                room_execution_mode=room_execution_mode,
                grant_workspace_scope=grant_workspace_scope,
                allowed_tools=allowed_tools,
                project_context_enabled=project_context_enabled,
                pi_skills_enabled=pi_skills_enabled,
                codex_skills_enabled=codex_skills_enabled,
                workspace_roots=workspace_roots,
                updated_at_ms=updated_at_ms,
            )
        return self.get(session_id)

    def set_room_execution_mode(
        self,
        session_id: str,
        room_execution_mode: str,
        *,
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        """Persist the confirmed Room approval overlay.

        This is separate from ``execution_mode`` on purpose. A Room's
        unrestricted setting may remove repeated per-Tool prompts only after
        its start confirmation; the Session's ordinary mode and workspace
        grant remain independently auditable.
        """

        normalized = _normalize_room_execution_mode(room_execution_mode)
        timestamp = _timestamp(updated_at_ms)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_sessions
                SET room_execution_mode = ?, updated_at_ms = ?
                WHERE id = ?
                """,
                (normalized, timestamp, session_id),
            )
            if cursor.rowcount != 1:
                raise AgentSessionNotFound(session_id)
        return self.get(session_id)
    def set_disclosure_preferences(
        self,
        session_id: str,
        preferences: Mapping[str, object],
        *,
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        normalized = _disclosure_preferences(preferences)
        timestamp = _timestamp(updated_at_ms)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self._get(conn, session_id)
            if normalized == current.get("capabilityDisclosurePreferences"):
                return current
            cursor = conn.execute(
                """
                INSERT INTO agent_session_tool_policies(
                    session_id, allowed_tools_json, disclosure_preferences_json,
                    policy_revision, updated_at_ms
                ) VALUES (?, 'null', ?, 2, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    disclosure_preferences_json = excluded.disclosure_preferences_json,
                    policy_revision = agent_session_tool_policies.policy_revision + 1,
                    updated_at_ms = excluded.updated_at_ms
                """,
                (
                    session_id,
                    json.dumps(
                        normalized,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    timestamp,
                ),
            )
            if cursor.rowcount != 1:
                raise AgentSessionNotFound(session_id)
            conn.execute(
                "UPDATE agent_sessions SET updated_at_ms = ? WHERE id = ?",
                (timestamp, session_id),
            )
        return self.get(session_id)

    def _set_runtime_policy(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        *,
        mode: str,
        tool_profile_version: str,
        execution_mode: str | None,
        room_execution_mode: str | None,
        grant_workspace_scope: bool,
        allowed_tools: Iterable[str] | None,
        project_context_enabled: bool | None,
        pi_skills_enabled: bool | None,
        codex_skills_enabled: bool | None,
        workspace_roots: Iterable[str] | None,
        updated_at_ms: int | None,
    ) -> dict[str, object]:
        normalized_mode = str(mode or "").strip()
        if normalized_mode not in {"assistant", "coordinator"}:
            raise ValueError("agent session mode must be assistant or coordinator")
        current = self._get(conn, session_id)
        normalized_room_execution_mode = (
            str(current.get("roomExecutionMode") or "")
            if room_execution_mode is None
            else _normalize_room_execution_mode(room_execution_mode)
        )
        execution_value: object = execution_mode
        if execution_value is None:
            execution_value = (
                None
                if str(tool_profile_version or "").strip()
                in {
                    "control-center-v1",
                    "subagent-readonly-v1",
                    "control-center-auto-approve-v1",
                    "control-center-full-access-v1",
                }
                else current.get("executionMode")
            )
        normalized_execution_mode = normalize_execution_mode(
            execution_value,
            tool_profile_version=tool_profile_version,
        )
        profile = canonical_tool_profile(
            tool_profile_version,
            execution_mode=normalized_execution_mode,
        )
        if profile not in SUPPORTED_AGENT_TOOL_PROFILES:
            raise ValueError("unsupported Agent tool profile")
        if profile == DANGEROUS_AUTO_APPROVE_TOOL_PROFILE and (
            normalized_mode != "coordinator"
            or normalized_execution_mode != FULL_TRUST_EXECUTION_MODE
        ):
            raise ValueError(
                "automatic approval requires coordinator mode and full-trust execution"
            )
        if profile == FULL_ACCESS_TOOL_PROFILE and (
            normalized_mode != "coordinator"
            or normalized_execution_mode != PER_ACTION_EXECUTION_MODE
        ):
            raise ValueError(
                "full access requires coordinator mode and per-action execution"
            )
        unrestricted_profile = profile in {
            DANGEROUS_AUTO_APPROVE_TOOL_PROFILE,
            FULL_ACCESS_TOOL_PROFILE,
        }
        roots = _workspace_roots(
            current.get("workspaceRoots", []) if workspace_roots is None else workspace_roots
        )
        trace_project_binding = is_trace_project_bound_surface(
            current.get("surfaceKind"),
            current.get("ownerAppId"),
            current.get("surfaceKey"),
        )
        if trace_project_binding:
            roots = list(exact_trace_project_workspace_roots(roots))
        elif unrestricted_profile:
            roots = list(system_wide_workspace_roots(roots))
            project_context_enabled = True
            pi_skills_enabled = True
            codex_skills_enabled = True
        read_only_subagent = (
            str(current.get("sessionKind") or "conversation") == "subagent_runtime"
            and normalized_mode == "assistant"
            and profile == "subagent-readonly-v1"
        )
        if normalized_mode == "assistant" and not read_only_subagent:
            roots = []
        if normalized_execution_mode in {
            WORKSPACE_MANAGED_EXECUTION_MODE,
            FULL_TRUST_EXECUTION_MODE,
        } and (normalized_mode != "coordinator" or not roots):
            raise ValueError(
                "managed and full-trust execution require a coordinator workspace"
            )
        expected_scope_sha256 = workspace_scope_sha256(roots)
        preserve_scope = (
            str(current.get("executionMode") or "")
            == normalized_execution_mode
            and str(current.get("workspaceScopeSha256") or "")
            == expected_scope_sha256
            and int(current.get("workspaceScopeGrantedAtMs") or 0) > 0
        )
        scope_sha256 = (
            expected_scope_sha256
            if normalized_execution_mode
            in {
                WORKSPACE_MANAGED_EXECUTION_MODE,
                FULL_TRUST_EXECUTION_MODE,
            }
            and (grant_workspace_scope or preserve_scope)
            else ""
        )
        scope_granted_at_ms = (
            _timestamp(updated_at_ms)
            if scope_sha256 and grant_workspace_scope
            else int(current.get("workspaceScopeGrantedAtMs") or 0)
            if scope_sha256 and preserve_scope
            else 0
        )
        normalized_tools = (
            None
            if unrestricted_profile
            else _allowed_tools(allowed_tools)
        )
        context_enabled = (
            bool(current.get("projectContextEnabled", False))
            if project_context_enabled is None
            else bool(project_context_enabled)
        )
        load_pi_skills = (
            bool(current.get("piSkillsEnabled", False))
            if pi_skills_enabled is None
            else bool(pi_skills_enabled)
        )
        load_codex_skills = (
            bool(current.get("codexSkillsEnabled", False))
            if codex_skills_enabled is None
            else bool(codex_skills_enabled)
        )
        shell_policy = (
            "coordinator-per-command-v1"
            if normalized_mode == "coordinator"
            else "assistant-no-shell-v1"
        )
        timestamp = _timestamp(updated_at_ms)
        cursor = conn.execute(
            """
            UPDATE agent_sessions
            SET session_mode = ?, tool_profile_version = ?, execution_mode = ?,
                room_execution_mode = ?,
                workspace_scope_sha256 = ?, workspace_scope_granted_at_ms = ?,
                workspace_roots_json = ?,
                shell_policy_version = ?, project_context_enabled = ?,
                pi_skills_enabled = ?, codex_skills_enabled = ?, updated_at_ms = ?
            WHERE id = ?
            """,
            (
                normalized_mode,
                profile,
                normalized_execution_mode,
                normalized_room_execution_mode,
                scope_sha256,
                scope_granted_at_ms,
                json.dumps(roots, ensure_ascii=False, separators=(",", ":")),
                shell_policy,
                1 if context_enabled else 0,
                1 if load_pi_skills else 0,
                1 if load_codex_skills else 0,
                timestamp,
                session_id,
            ),
        )
        if cursor.rowcount != 1:
            raise AgentSessionNotFound(session_id)
        conn.execute(
            """
            INSERT INTO agent_session_tool_policies(
                session_id,
                allowed_tools_json,
                updated_at_ms
            )
            VALUES (?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                allowed_tools_json = excluded.allowed_tools_json,
                updated_at_ms = excluded.updated_at_ms
            """,
            (
                session_id,
                json.dumps(
                    normalized_tools,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                if normalized_tools is not None
                else "null",
                timestamp,
            ),
        )
        return self._get(conn, session_id)

    def archive(self, session_id: str, *, archived: bool = True, updated_at_ms: int | None = None) -> dict[str, object]:
        timestamp = _timestamp(updated_at_ms)
        if archived:
            self._update(
                session_id,
                "status = 'archived', archived_at_ms = ?, updated_at_ms = ?",
                (timestamp, timestamp),
            )
        else:
            self._update(
                session_id,
                "status = 'idle', archived_at_ms = NULL, updated_at_ms = ?",
                (timestamp,),
            )
        return self.get(session_id)

    def retire_internal(
        self,
        session_id: str,
        *,
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        """Make a delegated runtime identity non-resumable without deleting its audit owner."""

        timestamp = _timestamp(updated_at_ms)
        with self._connect() as conn:
            owned = conn.execute(
                "SELECT 1 FROM agent_subagent_runs WHERE child_session_id = ?",
                (session_id,),
            ).fetchone()
            if owned is None:
                raise ValueError("only delegated runtime sessions can be retired")
            conn.execute(
                "DELETE FROM agent_runtime_bindings WHERE session_id = ?",
                (session_id,),
            )
            cursor = conn.execute(
                """
                UPDATE agent_sessions
                SET session_kind = 'subagent_runtime', status = 'archived',
                    pi_session_id = '', session_file = '', workspace_roots_json = '[]',
                    last_message_preview = '', archived_at_ms = ?, updated_at_ms = ?
                WHERE id = ?
                """,
                (timestamp, timestamp, session_id),
            )
            if cursor.rowcount != 1:
                raise AgentSessionNotFound(session_id)
        return self.get(session_id)

    def retire_system_internal(
        self,
        session_id: str,
        *,
        tool_profile_version: str,
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        """Retire one product-owned hidden Session after its durable run ends."""

        expected_profile = str(tool_profile_version or "").strip()
        if not expected_profile:
            raise ValueError("internal Session tool profile is required")
        timestamp = _timestamp(updated_at_ms)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT session_kind, tool_profile_version
                FROM agent_sessions
                WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                raise AgentSessionNotFound(session_id)
            if (
                str(row["session_kind"]) != "subagent_runtime"
                or str(row["tool_profile_version"]) != expected_profile
            ):
                raise ValueError("only the matching system-internal Session can be retired")
            conn.execute(
                "DELETE FROM agent_runtime_bindings WHERE session_id = ?",
                (session_id,),
            )
            conn.execute(
                """
                UPDATE agent_sessions
                SET status = 'archived', pi_session_id = '', session_file = '',
                    workspace_roots_json = '[]', last_message_preview = '',
                    archived_at_ms = ?, updated_at_ms = ?
                WHERE id = ?
                """,
                (timestamp, timestamp, session_id),
            )
        return self.get(session_id)

    def destroy_internal(self, session_id: str) -> dict[str, object]:
        """Delete an expired delegated Session while leaving its run projection intact."""

        session = self.get(session_id)
        with self._connect() as conn:
            owned = conn.execute(
                """
                SELECT state FROM agent_subagent_runs
                WHERE child_session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if owned is None:
                raise ValueError("only delegated runtime sessions can be destroyed")
            if str(owned["state"]) not in {"completed", "failed", "aborted", "timed_out"}:
                raise ValueError("active delegated runtime sessions cannot be destroyed")
            cursor = conn.execute(
                """
                DELETE FROM agent_sessions
                WHERE id = ? AND session_kind = 'subagent_runtime'
                """,
                (session_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError("only internal subagent sessions can be destroyed")
        return session

    def delete(self, session_id: str) -> dict[str, object]:
        session = self.get(session_id)
        with self._connect() as conn:
            conn.execute("DELETE FROM agent_sessions WHERE id = ?", (session_id,))
        return session

    def bind_pi_session(
        self,
        session_id: str,
        *,
        pi_session_id: str,
        session_file: str,
        message_count: int = 0,
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        return self.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id=pi_session_id,
            transcript_ref=session_file,
            binding_state="active",
            message_count=message_count,
            updated_at_ms=updated_at_ms,
        )

    def set_status(
        self,
        session_id: str,
        status: str,
        *,
        message_count: int | None = None,
        last_message_preview: str | None = None,
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        if status not in {"idle", "active", "busy", "faulted", "archived"}:
            raise ValueError(f"unsupported agent session status: {status}")
        assignments = ["status = ?", "updated_at_ms = ?"]
        values: list[object] = [status, _timestamp(updated_at_ms)]
        if message_count is not None:
            assignments.append("message_count = ?")
            values.append(max(0, int(message_count)))
        if last_message_preview is not None:
            assignments.append("last_message_preview = ?")
            values.append(" ".join(last_message_preview.split())[:240])
        self._update(session_id, ", ".join(assignments), tuple(values))
        return self.get(session_id)

    def max_event_sequence(self, session_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM agent_runtime_events WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return int(row[0] if row else 0)

    def runtime_turn_terminal_event(
        self,
        session_id: str,
        turn_id: str,
    ) -> dict[str, object] | None:
        """Return the durable terminal event for one exact Provider turn."""

        normalized_turn_id = str(turn_id).strip()
        if not normalized_turn_id:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT event_id, sequence, event_type, created_at_ms,
                       redacted_summary
                FROM agent_runtime_events
                WHERE session_id = ? AND turn_id = ?
                  AND event_type IN ('turn_completed', 'turn_failed')
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (session_id, normalized_turn_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "eventId": str(row["event_id"]),
            "sessionId": str(session_id),
            "turnId": normalized_turn_id,
            "sequence": int(row["sequence"]),
            "eventType": str(row["event_type"]),
            "createdAtMs": int(row["created_at_ms"]),
            "status": str(row["redacted_summary"] or ""),
        }

    def latest_runtime_turn_id(self, session_id: str) -> str:
        self.get(session_id)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT turn_id
                FROM agent_runtime_events
                WHERE session_id = ? AND turn_id <> ''
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        return str(row["turn_id"]) if row is not None else ""

    def runtime_review_request_turn_ids(
        self,
        session_id: str,
        run_id: str,
        *,
        limit: int = 512,
    ) -> list[str]:
        """Return durable turn IDs for one memory-review request.

        Runtime event payloads are intentionally not persisted wholesale. The
        event projection stores a small ``uiRequest`` identity index instead,
        which is enough to bind stale review recovery to the original turn.
        """

        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            return []
        self.get(session_id)
        bounded_limit = max(1, min(int(limit), 512))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT turn_id, metrics_json
                FROM agent_runtime_events
                WHERE session_id = ?
                  AND event_type = 'user_input_required'
                  AND turn_id <> ''
                ORDER BY sequence DESC
                LIMIT ?
                """,
                (session_id, bounded_limit),
            ).fetchall()
        turn_ids: list[str] = []
        seen: set[str] = set()
        for row in rows:
            try:
                metrics = json.loads(str(row["metrics_json"] or "{}"))
            except (TypeError, ValueError):
                continue
            if not isinstance(metrics, dict):
                continue
            ui_request = metrics.get("uiRequest")
            if not isinstance(ui_request, dict):
                continue
            if (
                str(ui_request.get("requestKind") or "")
                != "memory_review"
                or str(ui_request.get("runId") or "")
                != normalized_run_id
            ):
                continue
            turn_id = str(row["turn_id"] or "").strip()
            if turn_id and turn_id not in seen:
                seen.add(turn_id)
                turn_ids.append(turn_id)
        return turn_ids

    def has_runtime_approval_resolution(
        self,
        session_id: str,
        approval_id: str,
        *,
        limit: int = 512,
    ) -> bool:
        """Return whether one exact approval terminal is durably recorded."""

        normalized_approval_id = str(approval_id or "").strip()
        if not normalized_approval_id:
            return False
        self.get(session_id)
        bounded_limit = max(1, min(int(limit), 1000))
        with self._connect() as conn:
            marker = conn.execute(
                """
                SELECT 1
                FROM agent_approval_terminal_projections
                WHERE session_id = ? AND approval_id = ?
                  AND projection_kind = 'approval_resolved'
                LIMIT 1
                """,
                (session_id, normalized_approval_id),
            ).fetchone()
            if marker is not None:
                return True
            rows = conn.execute(
                """
                SELECT metrics_json
                FROM agent_runtime_events
                WHERE session_id = ? AND event_type = 'approval_resolved'
                ORDER BY sequence DESC
                LIMIT ?
                """,
                (session_id, bounded_limit),
            ).fetchall()
        for row in rows:
            try:
                metrics = json.loads(str(row["metrics_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            identity = (
                metrics.get("approvalIdentity")
                if isinstance(metrics, dict)
                else None
            )
            if (
                isinstance(identity, dict)
                and str(identity.get("approvalId") or "")
                == normalized_approval_id
            ):
                return True
        return False

    def has_runtime_tool_terminal(
        self,
        session_id: str,
        tool_call_id: str,
        *,
        turn_id: str = "",
        tool_name: str = "",
        requested_after_ms: int = 0,
        limit: int = 1000,
    ) -> bool:
        """Return whether one exact Tool terminal is durably recorded.

        New rows carry a content-free Tool identity in ``metrics_json``.
        Older installed rows predate that index, so a bounded fallback accepts
        only a terminal in the exact owning turn, after the approval request,
        with the same redacted Tool name. This avoids synthesizing duplicate
        terminals for pre-upgrade successful operations.
        """

        normalized_tool_call_id = str(tool_call_id or "").strip()
        if not normalized_tool_call_id:
            return False
        normalized_turn_id = str(turn_id or "").strip()
        normalized_tool_name = str(tool_name or "").strip()
        self.get(session_id)
        bounded_limit = max(1, min(int(limit), 2000))
        with self._connect() as conn:
            marker = conn.execute(
                """
                SELECT 1
                FROM agent_approval_terminal_projections
                WHERE session_id = ?
                  AND projection_kind = 'tool_finished'
                  AND tool_call_id = ?
                  AND (? = '' OR turn_id = ?)
                  AND (? = '' OR tool_name = ?)
                  AND projected_at_ms >= ?
                LIMIT 1
                """,
                (
                    session_id,
                    normalized_tool_call_id,
                    normalized_turn_id,
                    normalized_turn_id,
                    normalized_tool_name,
                    normalized_tool_name,
                    max(0, int(requested_after_ms or 0)),
                ),
            ).fetchone()
            if marker is not None:
                return True
            rows = conn.execute(
                """
                SELECT turn_id, created_at_ms, redacted_summary, metrics_json
                FROM agent_runtime_events
                WHERE session_id = ? AND event_type = 'tool_finished'
                  AND created_at_ms >= ?
                ORDER BY sequence DESC
                LIMIT ?
                """,
                (
                    session_id,
                    max(0, int(requested_after_ms or 0)),
                    bounded_limit,
                ),
            ).fetchall()
        for row in rows:
            try:
                metrics = json.loads(str(row["metrics_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                metrics = {}
            identity = (
                metrics.get("toolIdentity")
                if isinstance(metrics, dict)
                else None
            )
            if isinstance(identity, dict):
                if (
                    str(identity.get("toolCallId") or "")
                    == normalized_tool_call_id
                    and (
                        not normalized_turn_id
                        or str(row["turn_id"] or "") == normalized_turn_id
                    )
                    and (
                        not normalized_tool_name
                        or str(identity.get("toolName") or "")
                        == normalized_tool_name
                    )
                ):
                    return True
                # An indexed row belongs to another Tool and must not enter
                # the legacy turn/name fallback.
                continue
            if (
                normalized_turn_id
                and str(row["turn_id"] or "") == normalized_turn_id
                and normalized_tool_name
                and str(row["redacted_summary"] or "")
                == normalized_tool_name
            ):
                return True
        return False

    def prompt_acceptance_evidence(
        self,
        session_id: str,
        client_message_id: str,
    ) -> dict[str, object] | None:
        """Return durable, content-free proof that Pi accepted one prompt."""

        normalized_client_message_id = str(
            client_message_id
        ).strip()
        if not normalized_client_message_id:
            return None
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT event_id, turn_id, created_at_ms, metrics_json
                FROM agent_runtime_events
                WHERE session_id = ? AND event_type = 'message_completed'
                ORDER BY sequence DESC
                """,
                (session_id,),
            )
            # Normal runtime retention bounds this cursor to the latest
            # events plus acceptance records whose exact command receipt is
            # still pending. Iterating rather than fetchall keeps lookup
            # memory bounded while still finding pinned older evidence.
            for row in rows:
                try:
                    metrics = json.loads(
                        str(row["metrics_json"] or "{}")
                    )
                except (TypeError, ValueError):
                    continue
                if not isinstance(metrics, dict):
                    continue
                acceptance = metrics.get(
                    "promptAcceptance"
                )
                if (
                    not isinstance(acceptance, dict)
                    or str(
                        acceptance.get(
                            "clientMessageId"
                        )
                        or ""
                    )
                    != normalized_client_message_id
                ):
                    continue
                return {
                    "eventId": str(row["event_id"] or ""),
                    "turnId": str(
                        acceptance.get("turnId")
                        or row["turn_id"]
                        or ""
                    ),
                    "messageId": str(
                        acceptance.get("messageId") or ""
                    ),
                    "clientMessageId": (
                        normalized_client_message_id
                    ),
                    "retryOfClientMessageId": str(
                        acceptance.get(
                            "retryOfClientMessageId"
                        )
                        or ""
                    ),
                    "createdAtMs": int(
                        row["created_at_ms"] or 0
                    ),
                }
        return None

    def agent_todo(self, session_id: str) -> dict[str, object]:
        self.get(session_id)
        with self._connect() as conn:
            return _agent_todo_projection(conn, session_id)

    def mutate_agent_todo(
        self,
        session_id: str,
        payload: Mapping[str, object],
        *,
        actor: str = "agent",
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        operation = str(payload.get("op") or "").strip()
        if operation not in {
            "init",
            "start",
            "done",
            "drop",
            "block",
            "unblock",
            "append",
            "rm",
            "view",
        }:
            raise ValueError("unsupported todo operation")
        timestamp = _timestamp(updated_at_ms)
        normalized_actor = _bounded_todo_text(
            actor,
            field="actor",
            maximum=120,
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            session = conn.execute(
                "SELECT id FROM agent_sessions WHERE id = ? AND status <> 'archived'",
                (session_id,),
            ).fetchone()
            if session is None:
                raise AgentSessionNotFound(session_id)
            current = _agent_todo_projection(conn, session_id)
            if operation == "view":
                return {
                    "schemaVersion": "rag-ime.agent-todo-mutation-result.v1",
                    "ok": True,
                    "operation": operation,
                    "changed": False,
                    "todo": current,
                    "phases": current["phases"],
                    "storage": "session",
                }

            # Todo is private Session state. Room membership no longer creates
            # a second Kernel-owned Todo authority or a lineage gate.
            room_lineage = None

            phases = _clone_todo_phases(current["phases"])
            previous_statuses = _todo_statuses(phases)
            if operation == "init":
                phases = _todo_init(payload)
            elif operation == "start":
                task = _required_todo_target(payload, "task", maximum=240)
                target = _find_todo_task(phases, task)
                if target is None:
                    raise ValueError(_todo_task_not_found(task, phases))
                for phase in phases:
                    for item in phase["tasks"]:
                        if item["status"] == "in_progress":
                            item["status"] = "pending"
                target["status"] = "in_progress"
                target.pop("reason", None)
            elif operation in {"done", "drop"}:
                targets = _todo_targets(phases, payload)
                target_status = "completed" if operation == "done" else "abandoned"
                reason = _optional_todo_reason(payload)
                for item in targets:
                    item["status"] = target_status
                    if operation == "drop" and reason:
                        item["reason"] = reason
                    else:
                        item.pop("reason", None)
            elif operation == "block":
                targets = _todo_targets(phases, payload)
                if any(item["status"] in {"completed", "abandoned"} for item in targets):
                    raise ValueError("completed or abandoned todo tasks cannot be blocked")
                reason = _optional_todo_reason(payload)
                for item in targets:
                    item["status"] = "blocked"
                    if reason:
                        item["reason"] = reason
                    else:
                        item.pop("reason", None)
            elif operation == "unblock":
                targets = _todo_targets(phases, payload)
                if any(item["status"] != "blocked" for item in targets):
                    raise ValueError("only blocked todo tasks can be unblocked")
                for item in targets:
                    item["status"] = "pending"
                    item.pop("reason", None)
            elif operation == "append":
                phase_name = _required_todo_target(payload, "phase", maximum=80)
                items = _todo_input_items(payload.get("items"), field="items")
                existing = {
                    str(item["content"])
                    for phase in phases
                    for item in phase["tasks"]
                }
                duplicates = [item for item in items if item in existing]
                if duplicates:
                    raise ValueError(f'Task "{duplicates[0]}" already exists')
                target_phase = next(
                    (phase for phase in phases if phase["name"] == phase_name),
                    None,
                )
                if target_phase is None:
                    target_phase = {"name": phase_name, "tasks": []}
                    phases.append(target_phase)
                target_phase["tasks"].extend(
                    {"content": item, "status": "pending"} for item in items
                )
            else:
                _todo_remove(phases, payload)

            _normalize_todo_in_progress(phases)
            _validate_todo_phases(phases)
            revision = int(current["revision"]) + 1
            event_id = f"todo-event:{uuid.uuid4()}"
            conn.execute(
                """
                INSERT INTO agent_todo_events(
                    event_id, session_id, revision, operation, phases_json,
                    actor, created_at_ms, room_lineage_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    session_id,
                    revision,
                    operation,
                    json.dumps(
                        phases,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    normalized_actor,
                    timestamp,
                    json.dumps(
                        room_lineage,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )
            conn.execute(
                "UPDATE agent_sessions SET updated_at_ms = ? WHERE id = ?",
                (timestamp, session_id),
            )
            todo = _agent_todo_projection(conn, session_id)
        completed_tasks = [
            {"phase": str(phase["name"]), "task": str(item["content"])}
            for phase in todo["phases"]
            for item in phase["tasks"]
            if item["status"] == "completed"
            and previous_statuses.get(str(item["content"])) != "completed"
        ]
        result: dict[str, object] = {
            "schemaVersion": "rag-ime.agent-todo-mutation-result.v1",
            "ok": True,
            "operation": operation,
            "changed": True,
            "event": {
                "eventId": event_id,
                "revision": revision,
                "operation": operation,
                "actor": normalized_actor,
                "createdAtMs": timestamp,
                "roomLineage": room_lineage,
            },
            "todo": todo,
            "phases": todo["phases"],
            "storage": "session",
        }
        if completed_tasks:
            result["completedTasks"] = completed_tasks
        return result

    def agent_goal(self, session_id: str) -> dict[str, object]:
        self.get(session_id)
        with self._connect() as conn:
            return _agent_goal_projection(conn, session_id)

    def workflow_state(
        self,
        session_id: str,
        *,
        room_dispatch_authorized: bool = False,
    ) -> dict[str, object]:
        self.get(session_id)
        with self._connect() as conn:
            todo = _agent_todo_projection(conn, session_id)
            goal = _agent_goal_projection(conn, session_id)
        return _workflow_projection(
            session_id,
            todo,
            goal,
            room_dispatch_authorized=room_dispatch_authorized,
        )

    def mutate_agent_goal(
        self,
        session_id: str,
        payload: Mapping[str, object],
        *,
        actor: str = "control-center-user",
        updated_at_ms: int | None = None,
        lifecycle_request: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        action = str(payload.get("action") or "").strip()
        if action not in {
            "confirm_setup",
            "update",
            "pause",
            "resume",
            "complete",
            "cancel",
            "clear",
        }:
            raise ValueError("unsupported agent goal action")
        if action == "confirm_setup" and payload.get("confirmed") is not True:
            raise ValueError("agent goal setup must be explicitly confirmed")
        timestamp = _timestamp(updated_at_ms)
        normalized_actor = _bounded_goal_text(actor, field="actor", maximum=120)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            session = conn.execute(
                "SELECT id FROM agent_sessions WHERE id = ? AND status <> 'archived'",
                (session_id,),
            ).fetchone()
            if session is None:
                raise AgentSessionNotFound(session_id)
            current = _agent_goal_projection(conn, session_id)
            expected_revision = payload.get("expectedRevision")
            if expected_revision is None:
                raise ValueError("agent goal mutation requires expectedRevision")
            if expected_revision is not None and int(expected_revision) != int(
                current["revision"]
            ):
                raise ValueError("agent goal changed; refresh before saving")
            configured = bool(current["configured"])
            current_status = str(current["status"])
            if action == "confirm_setup" and configured:
                raise ValueError("clear the current agent goal before setting another")
            if action in {
                "update",
                "pause",
                "resume",
                "complete",
                "cancel",
                "clear",
            } and not configured:
                raise ValueError("this Session has no active agent goal")
            if action == "update" and current_status not in {"active", "paused"}:
                raise ValueError("only an active or paused goal can be edited")
            if action == "pause" and current_status != "active":
                raise ValueError("only an active goal can be paused")
            if action == "resume" and current_status != "paused":
                raise ValueError("only a paused goal can be resumed")
            if action == "complete" and current_status not in {"active", "paused"}:
                raise ValueError(
                    "only an active or paused goal can be completed"
                )
            if action == "cancel" and current_status not in {"active", "paused"}:
                raise ValueError(
                    "only an active or paused goal can be cancelled"
                )
            if action == "complete":
                _agent_goal_completion_authority_gate(
                    conn,
                    session_id=session_id,
                    goal_id=str(current["goalId"]),
                )

            goal_id = (
                f"goal:{uuid.uuid4()}"
                if action == "confirm_setup"
                else str(current["goalId"])
            )
            objective = _bounded_goal_text(
                payload.get("objective")
                if action == "confirm_setup" or "objective" in payload
                else current.get("objective"),
                field="objective",
                maximum=4_000,
            )
            success_criteria = _bounded_goal_text(
                payload.get("successCriteria")
                if "successCriteria" in payload
                else current.get("successCriteria"),
                field="successCriteria",
                maximum=2_000,
                required=False,
            )
            evidence_expectations = _goal_evidence_expectations(
                payload.get("evidenceExpectations")
                if "evidenceExpectations" in payload
                else current.get("evidenceExpectations")
            )
            current_budget = (
                current.get("budget")
                if isinstance(current.get("budget"), Mapping)
                else {}
            )
            token_budget = _optional_positive_budget(
                payload.get("tokenBudget")
                if "tokenBudget" in payload
                else current_budget.get("tokenLimit"),
                field="tokenBudget",
                maximum=100_000_000,
            )
            time_budget_ms = _optional_positive_budget(
                payload.get("timeBudgetMs")
                if "timeBudgetMs" in payload
                else current_budget.get("timeLimitMs"),
                field="timeBudgetMs",
                maximum=365 * 24 * 60 * 60 * 1000,
            )
            usage = (
                current.get("usage")
                if isinstance(current.get("usage"), Mapping)
                else {}
            )
            tokens_used = int(usage.get("tokens") or 0) if configured else 0
            elapsed_ms = int(usage.get("elapsedMs") or 0) if configured else 0
            completion_audit_id: str | None = None
            cancellation_audit_id: str | None = None
            if action == "complete":
                summary = _bounded_goal_text(
                    payload.get("summary"),
                    field="completion summary",
                    maximum=2_000,
                )
                evidence = _goal_completion_evidence(payload.get("evidence"))
                completion_audit_id = f"goal-audit:{uuid.uuid4()}"
                conn.execute(
                    """
                    INSERT INTO agent_goal_completion_audits(
                        audit_id, session_id, goal_id, summary, evidence_json,
                        completed_by, created_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        completion_audit_id,
                        session_id,
                        goal_id,
                        summary,
                        json.dumps(
                            evidence,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        normalized_actor,
                        timestamp,
                    ),
                )
            elif action == "cancel":
                reason = _bounded_goal_text(
                    payload.get("reason"),
                    field="cancellation reason",
                    maximum=1_000,
                )
                cancellation_audit_id = f"goal-cancellation:{uuid.uuid4()}"
                conn.execute(
                    """
                    INSERT INTO agent_goal_cancellation_audits(
                        audit_id, session_id, goal_id, reason,
                        cancelled_by, created_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cancellation_audit_id,
                        session_id,
                        goal_id,
                        reason,
                        normalized_actor,
                        timestamp,
                    ),
                )

            target_status = {
                "confirm_setup": "active",
                "update": current_status,
                "pause": "paused",
                "resume": "active",
                "complete": "completed",
                "cancel": "cancelled",
                "clear": "cleared",
            }[action]
            event = _append_agent_goal_event(
                conn,
                session_id,
                goal_id=goal_id,
                objective=objective,
                success_criteria=success_criteria,
                evidence_expectations=evidence_expectations,
                status=target_status,
                token_budget=token_budget,
                time_budget_ms=time_budget_ms,
                tokens_used=tokens_used,
                elapsed_ms=elapsed_ms,
                completion_audit_id=completion_audit_id,
                cancellation_audit_id=cancellation_audit_id,
                actor=normalized_actor,
                created_at_ms=timestamp,
            )
            if action != "update":
                _reset_agent_goal_continuation_budget(
                    conn,
                    session_id=session_id,
                    goal_id=goal_id,
                    reset_existing=action != "confirm_setup",
                    updated_at_ms=timestamp,
                )
            conn.execute(
                "UPDATE agent_sessions SET updated_at_ms = ? WHERE id = ?",
                (timestamp, session_id),
            )
            goal = _agent_goal_projection(conn, session_id)
            todo = _agent_todo_projection(conn, session_id)
            lifecycle_audit = (
                _create_lifecycle_cancellation_audit(
                    conn,
                    session_id=session_id,
                    request=lifecycle_request,
                    transition_revision=int(goal["revision"]),
                    created_at_ms=timestamp,
                )
                if lifecycle_request is not None
                else None
            )
        result: dict[str, object] = {
            "schemaVersion": "rag-ime.agent-goal-mutation-result.v1",
            "ok": True,
            "action": action,
            "event": event,
            "workflow": _workflow_projection(session_id, todo, goal),
        }
        if lifecycle_audit is not None:
            result["lifecycleAudit"] = lifecycle_audit
        return result

    def agent_goal_continuation_budget(
        self,
        session_id: str,
        *,
        goal_id: str,
        limit: int,
    ) -> dict[str, int]:
        normalized_goal_id = str(goal_id or "").strip()
        normalized_limit = int(limit)
        if normalized_limit < 1:
            raise ValueError("goal continuation limit must be positive")
        with self._connect() as conn:
            return _agent_goal_continuation_budget(
                conn,
                session_id=session_id,
                goal_id=normalized_goal_id,
                limit=normalized_limit,
            )

    def claim_agent_goal_continuation(
        self,
        session_id: str,
        *,
        goal_id: str,
        request_key: str,
        limit: int,
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        """Atomically claim one durable automatic continuation for this Goal.

        The counter is scoped to a Goal lifecycle epoch, not a Pi cancel scope.
        A new user turn or runtime restart therefore cannot replenish it.
        Goal confirmation/pause/resume/complete/cancel/clear transitions reset
        explicitly in ``mutate_agent_goal``.
        """

        normalized_goal_id = str(goal_id or "").strip()
        normalized_request_key = str(request_key or "").strip()
        normalized_limit = int(limit)
        if not normalized_goal_id or len(normalized_goal_id) > 240:
            raise ValueError("goal continuation goalId is invalid")
        if (
            not normalized_request_key
            or len(normalized_request_key) > 160
            or "\x00" in normalized_request_key
        ):
            raise ValueError("goal continuation request key is invalid")
        if normalized_limit < 1 or normalized_limit > 100:
            raise ValueError("goal continuation limit is invalid")
        timestamp = _timestamp(updated_at_ms)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            goal = _agent_goal_projection(conn, session_id)
            gate = _agent_act_gate(goal)
            budget = _ensure_agent_goal_continuation_budget(
                conn,
                session_id=session_id,
                goal_id=normalized_goal_id,
                limit=normalized_limit,
                updated_at_ms=timestamp,
            )
            if (
                goal.get("configured") is not True
                or str(goal.get("goalId") or "") != normalized_goal_id
                or str(goal.get("status") or "") != "active"
                or gate.get("allowed") is not True
            ):
                return {
                    **budget,
                    "claimed": False,
                    "replayed": False,
                    "reason": str(gate.get("reason") or "goal_not_active"),
                    "goal": goal,
                    "actGate": gate,
                }
            receipt = conn.execute(
                """
                SELECT goal_id, epoch
                FROM agent_goal_continuation_receipts
                WHERE session_id = ? AND request_key = ?
                """,
                (session_id, normalized_request_key),
            ).fetchone()
            if receipt is not None:
                if str(receipt["goal_id"]) != normalized_goal_id:
                    raise ValueError(
                        "goal continuation request key was reused for another goal"
                    )
                if int(receipt["epoch"]) != int(budget["epoch"]):
                    raise ValueError(
                        "goal continuation request key belongs to another lifecycle epoch"
                    )
                return {
                    **budget,
                    "claimed": True,
                    "replayed": True,
                    "reason": "replay",
                    "goal": goal,
                    "actGate": gate,
                }
            if int(budget["issuedCount"]) >= normalized_limit:
                return {
                    **budget,
                    "claimed": False,
                    "replayed": False,
                    "reason": "goal_continuation_limit",
                    "goal": goal,
                    "actGate": gate,
                }
            issued_index = int(budget["issuedCount"]) + 1
            conn.execute(
                """
                UPDATE agent_goal_continuation_budgets
                SET issued_count = ?, updated_at_ms = ?
                WHERE session_id = ? AND goal_id = ? AND epoch = ?
                """,
                (
                    issued_index,
                    timestamp,
                    session_id,
                    normalized_goal_id,
                    int(budget["epoch"]),
                ),
            )
            conn.execute(
                """
                INSERT INTO agent_goal_continuation_receipts(
                    receipt_id, session_id, goal_id, request_key, epoch,
                    issued_index, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"goal-continuation:{uuid.uuid4()}",
                    session_id,
                    normalized_goal_id,
                    normalized_request_key,
                    int(budget["epoch"]),
                    issued_index,
                    timestamp,
                ),
            )
            return {
                "epoch": int(budget["epoch"]),
                "issuedCount": issued_index,
                "limit": normalized_limit,
                "remaining": max(0, normalized_limit - issued_index),
                "claimed": True,
                "replayed": False,
                "reason": "claimed",
                "goal": goal,
                "actGate": gate,
            }

    def record_agent_goal_usage(
        self,
        session_id: str,
        *,
        idempotency_key: str,
        turn_id: str = "",
        event_id: str = "",
        token_delta: int = 0,
        elapsed_delta_ms: int = 0,
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        normalized_key = str(idempotency_key or "").strip()
        normalized_turn_id = str(turn_id or "").strip()
        normalized_event_id = str(event_id or "").strip()
        if not normalized_key or len(normalized_key) > 300 or "\x00" in normalized_key:
            raise ValueError("goal usage idempotencyKey is invalid")
        if len(normalized_turn_id) > 240 or "\x00" in normalized_turn_id:
            raise ValueError("goal usage turnId is invalid")
        if len(normalized_event_id) > 240 or "\x00" in normalized_event_id:
            raise ValueError("goal usage eventId is invalid")
        normalized_token_delta = int(token_delta)
        normalized_elapsed_delta = int(elapsed_delta_ms)
        if normalized_token_delta < 0 or normalized_token_delta > 10_000_000:
            raise ValueError("goal tokenDelta is invalid")
        if normalized_elapsed_delta < 0 or normalized_elapsed_delta > 7 * 24 * 60 * 60 * 1000:
            raise ValueError("goal elapsedDeltaMs is invalid")
        timestamp = _timestamp(updated_at_ms)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            receipt = conn.execute(
                """
                SELECT goal_id, turn_id, source_event_id, token_delta, elapsed_delta_ms
                FROM agent_goal_usage_receipts
                WHERE session_id = ? AND idempotency_key = ?
                """,
                (session_id, normalized_key),
            ).fetchone()
            if receipt is not None:
                if (
                    str(receipt["turn_id"]) != normalized_turn_id
                    or str(receipt["source_event_id"]) != normalized_event_id
                    or int(receipt["token_delta"]) != normalized_token_delta
                    or int(receipt["elapsed_delta_ms"]) != normalized_elapsed_delta
                ):
                    raise ValueError("goal usage idempotencyKey was reused with different data")
                todo = _agent_todo_projection(conn, session_id)
                goal = _agent_goal_projection(conn, session_id)
                return _workflow_projection(session_id, todo, goal)
            current = _agent_goal_projection(conn, session_id)
            if not current["configured"] or current["status"] != "active":
                raise ValueError("goal usage can only be recorded for an active goal")
            budget = current["budget"] if isinstance(current.get("budget"), Mapping) else {}
            usage = current["usage"] if isinstance(current.get("usage"), Mapping) else {}
            usage_event = _append_agent_goal_event(
                conn,
                session_id,
                goal_id=str(current["goalId"]),
                objective=str(current["objective"]),
                success_criteria=str(current.get("successCriteria") or ""),
                evidence_expectations=_goal_evidence_expectations(
                    current.get("evidenceExpectations")
                ),
                status="active",
                token_budget=_optional_positive_budget(
                    budget.get("tokenLimit"), field="tokenBudget", maximum=100_000_000
                ),
                time_budget_ms=_optional_positive_budget(
                    budget.get("timeLimitMs"),
                    field="timeBudgetMs",
                    maximum=365 * 24 * 60 * 60 * 1000,
                ),
                tokens_used=int(usage.get("tokens") or 0) + normalized_token_delta,
                elapsed_ms=int(usage.get("elapsedMs") or 0) + normalized_elapsed_delta,
                completion_audit_id=None,
                cancellation_audit_id=None,
                actor="pi-runtime",
                created_at_ms=timestamp,
            )
            conn.execute(
                """
                INSERT INTO agent_goal_usage_receipts(
                    receipt_id, session_id, goal_id, idempotency_key, turn_id,
                    source_event_id, token_delta, elapsed_delta_ms,
                    goal_event_id, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"goal-usage:{uuid.uuid4()}",
                    session_id,
                    str(current["goalId"]),
                    normalized_key,
                    normalized_turn_id,
                    normalized_event_id,
                    normalized_token_delta,
                    normalized_elapsed_delta,
                    str(usage_event["eventId"]),
                    timestamp,
                ),
            )
            todo = _agent_todo_projection(conn, session_id)
            goal = _agent_goal_projection(conn, session_id)
        return _workflow_projection(session_id, todo, goal)

    def require_workspace_act(
        self,
        session_id: str,
        *,
        room_dispatch_authorized: bool = False,
    ) -> dict[str, object]:
        state = self.workflow_state(
            session_id,
            room_dispatch_authorized=room_dispatch_authorized,
        )
        gate = state["actGate"] if isinstance(state.get("actGate"), Mapping) else {}
        if gate.get("allowed") is not True:
            reason = str(gate.get("reason") or "act_gate_closed")
            message = str(gate.get("message") or "workspace mutation is not approved")
            raise ValueError(f"Act Gate blocked workspace mutation ({reason}): {message}")
        return state

    def require_goal_execution(self, session_id: str) -> dict[str, object]:
        """Reject new cost-incurring work while a configured Goal cannot run."""

        state = self.workflow_state(session_id)
        goal = state["goal"] if isinstance(state.get("goal"), Mapping) else {}
        if goal.get("configured") is not True:
            return state
        status = str(goal.get("status") or "")
        if status == "paused":
            raise AgentGoalExecutionBlocked(
                "goal_paused",
                "当前 Goal 已暂停，恢复后才能继续调用模型或委派任务。",
            )
        if status == "cancelled":
            raise AgentGoalExecutionBlocked(
                "goal_cancelled",
                "当前 Goal 已取消，不能继续调用模型或委派任务。",
            )
        if goal.get("budgetExceeded") is True:
            raise AgentGoalExecutionBlocked(
                "goal_budget_exhausted",
                "Goal 的 Token 或时间预算已经耗尽。",
            )
        return state


    def record_runtime_event(
        self,
        *,
        event_id: str,
        session_id: str,
        turn_id: str,
        sequence: int,
        event_type: str,
        created_at_ms: int,
        redacted_summary: str = "",
        metrics: Mapping[str, object] | None = None,
        retain_per_session: int = 1000,
    ) -> None:
        try:
            metrics_json = json.dumps(
                dict(metrics or {}),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("runtime event metrics must be JSON serializable") from exc
        if len(metrics_json.encode("utf-8")) > 8_192:
            raise ValueError("runtime event metrics are too large")
        with self._connect() as conn:
            projection_claim = self._claim_terminal_projection(
                conn,
                event_id=event_id,
                session_id=session_id,
                turn_id=turn_id,
                sequence=sequence,
                event_type=event_type,
                created_at_ms=created_at_ms,
                redacted_summary=redacted_summary,
                metrics=metrics or {},
            )
            if projection_claim == "existing":
                return
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO agent_runtime_events(
                    event_id, session_id, turn_id, sequence, event_type,
                    created_at_ms, redacted_summary, metrics_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    session_id,
                    turn_id,
                    sequence,
                    event_type,
                    created_at_ms,
                    " ".join(redacted_summary.split())[:240],
                    metrics_json,
                ),
            )
            if projection_claim == "claimed" and cursor.rowcount != 1:
                raise sqlite3.IntegrityError(
                    "terminal projection event identity was already used"
                )
            stale_rows = conn.execute(
                """
                SELECT event_id, turn_id, sequence, event_type,
                       created_at_ms, redacted_summary, metrics_json
                FROM agent_runtime_events
                WHERE session_id = ? AND sequence <= (
                    SELECT COALESCE(MAX(sequence), 0) - ?
                    FROM agent_runtime_events
                    WHERE session_id = ?
                )
                """,
                (session_id, max(100, int(retain_per_session)), session_id),
            ).fetchall()
            if not stale_rows:
                return
            pending_client_message_ids = {
                str(row[0])
                for row in conn.execute(
                    """
                    SELECT client_message_id
                    FROM agent_command_receipts
                    WHERE command_scope = 'session_prompt'
                      AND scope_id = ? AND state = 'pending'
                    """,
                    (session_id,),
                )
            }
            deletable_event_ids: list[str] = []
            for stale_row in stale_rows:
                keep_for_pending_receipt = False
                try:
                    stale_metrics = json.loads(
                        str(
                            stale_row["metrics_json"]
                            or "{}"
                        )
                    )
                except (TypeError, ValueError):
                    stale_metrics = {}
                if isinstance(stale_metrics, dict):
                    if stale_row["event_type"] in {
                        "approval_resolved",
                        "tool_finished",
                    }:
                        self._claim_terminal_projection(
                            conn,
                            event_id=str(stale_row["event_id"]),
                            session_id=session_id,
                            turn_id=str(stale_row["turn_id"] or ""),
                            sequence=int(stale_row["sequence"]),
                            event_type=str(stale_row["event_type"]),
                            created_at_ms=int(stale_row["created_at_ms"]),
                            redacted_summary=str(
                                stale_row["redacted_summary"] or ""
                            ),
                            metrics=stale_metrics,
                            allow_existing=True,
                        )
                    acceptance = stale_metrics.get(
                        "promptAcceptance"
                    )
                    if isinstance(acceptance, dict):
                        client_message_id = str(
                            acceptance.get(
                                "clientMessageId"
                            )
                            or ""
                        )
                        keep_for_pending_receipt = (
                            client_message_id
                            in pending_client_message_ids
                        )
                if not keep_for_pending_receipt:
                    deletable_event_ids.append(
                        str(stale_row["event_id"])
                    )
            conn.executemany(
                """
                DELETE FROM agent_runtime_events
                WHERE session_id = ? AND event_id = ?
                """,
                (
                    (session_id, stale_event_id)
                    for stale_event_id in deletable_event_ids
                ),
            )

    def _claim_terminal_projection(
        self,
        conn: sqlite3.Connection,
        *,
        event_id: str,
        session_id: str,
        turn_id: str,
        sequence: int,
        event_type: str,
        created_at_ms: int,
        redacted_summary: str,
        metrics: Mapping[str, object],
        allow_existing: bool = False,
    ) -> str:
        """Claim an unpruned exactly-once owner in the event transaction."""

        candidate = _terminal_projection_candidate(
            conn,
            session_id=session_id,
            turn_id=turn_id,
            event_type=event_type,
            redacted_summary=redacted_summary,
            metrics=metrics,
        )
        if candidate is None:
            return "none"
        approval_id = str(candidate["approval_id"])
        existing = conn.execute(
            """
            SELECT event_id, event_sequence, turn_id, projected_at_ms
            FROM agent_approval_terminal_projections
            WHERE session_id = ? AND approval_id = ?
              AND projection_kind = ?
            """,
            (session_id, approval_id, event_type),
        ).fetchone()
        if existing is not None:
            if allow_existing or str(existing["event_id"]) == str(event_id):
                return "existing"
            raise AgentTerminalProjectionDuplicate(
                event_id=str(existing["event_id"]),
                sequence=int(existing["event_sequence"]),
                turn_id=str(existing["turn_id"] or ""),
                created_at_ms=int(existing["projected_at_ms"]),
            )
        conn.execute(
            """
            INSERT INTO agent_approval_terminal_projections(
                session_id, approval_id, projection_kind, tool_call_id,
                turn_id, tool_name, room_id, room_root_id,
                room_dispatch_id, room_generation, event_id,
                event_sequence, projected_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                approval_id,
                event_type,
                str(candidate["tool_call_id"] or ""),
                str(turn_id or candidate["causal_turn_id"] or ""),
                str(candidate["tool_name"] or ""),
                str(candidate["room_id"] or ""),
                str(candidate["room_root_id"] or ""),
                str(candidate["room_dispatch_id"] or ""),
                int(candidate["room_generation"] or 0),
                str(event_id),
                int(sequence),
                int(created_at_ms),
            ),
        )
        return "claimed"

    def lifecycle_cancellation_audit(
        self,
        request_id: str,
    ) -> dict[str, object] | None:
        normalized = str(request_id or "").strip()
        if not normalized:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM agent_lifecycle_cancellation_audits
                WHERE request_id = ?
                """,
                (normalized,),
            ).fetchone()
        return _lifecycle_cancellation_payload(row) if row is not None else None

    def lifecycle_cancellation_for_transition(
        self,
        session_id: str,
        *,
        scope_kind: str,
        scope_id: str,
        transition_revision: int,
        action: str,
    ) -> dict[str, object] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM agent_lifecycle_cancellation_audits
                WHERE session_id = ? AND scope_kind = ? AND scope_id = ?
                  AND transition_revision = ? AND action = ?
                """,
                (
                    session_id,
                    scope_kind,
                    scope_id,
                    int(transition_revision),
                    action,
                ),
            ).fetchone()
        return _lifecycle_cancellation_payload(row) if row is not None else None

    def lifecycle_cancellation_audits(
        self,
        session_id: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, object]]:
        self.get(session_id)
        bounded_limit = max(1, min(int(limit), 100))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_lifecycle_cancellation_audits
                WHERE session_id = ?
                ORDER BY created_at_ms DESC, request_id
                LIMIT ?
                """,
                (session_id, bounded_limit),
            ).fetchall()
        return [_lifecycle_cancellation_payload(row) for row in rows]

    def record_lifecycle_owner_receipt(
        self,
        request_id: str,
        *,
        owner: str,
        status: str,
        receipt: Mapping[str, object],
        updated_at_ms: int | None = None,
    ) -> dict[str, object]:
        owner_columns = {
            "runtime": ("runtime_status", "runtime_receipt_json"),
            "approval": ("approval_status", "approval_receipt_json"),
            "job": ("job_status", "job_receipt_json"),
            "delegation": (
                "delegation_status",
                "delegation_receipt_json",
            ),
        }
        if owner not in owner_columns:
            raise ValueError("unsupported lifecycle cancellation owner")
        if status not in {"succeeded", "excluded", "partial", "unknown"}:
            raise ValueError("unsupported lifecycle cancellation owner status")
        normalized_request_id = str(request_id or "").strip()
        if not normalized_request_id:
            raise ValueError("lifecycle cancellation requestId is required")
        timestamp = _timestamp(updated_at_ms)
        _assert_lifecycle_receipt_safe(receipt)
        receipt_json = json.dumps(
            dict(receipt),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(receipt_json.encode("utf-8")) > 64 * 1024:
            raise ValueError("lifecycle cancellation owner receipt is too large")
        status_column, receipt_column = owner_columns[owner]
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM agent_lifecycle_cancellation_audits
                WHERE request_id = ?
                """,
                (normalized_request_id,),
            ).fetchone()
            if row is None:
                raise ValueError("lifecycle cancellation audit was not found")
            conn.execute(
                f"""
                UPDATE agent_lifecycle_cancellation_audits
                SET {status_column} = ?, {receipt_column} = ?, updated_at_ms = ?
                WHERE request_id = ?
                """,
                (status, receipt_json, timestamp, normalized_request_id),
            )
            refreshed = conn.execute(
                """
                SELECT * FROM agent_lifecycle_cancellation_audits
                WHERE request_id = ?
                """,
                (normalized_request_id,),
            ).fetchone()
            assert refreshed is not None
            aggregate = _lifecycle_cancellation_state(refreshed)
            conn.execute(
                """
                UPDATE agent_lifecycle_cancellation_audits
                SET state = ?, updated_at_ms = ?
                WHERE request_id = ?
                """,
                (aggregate, timestamp, normalized_request_id),
            )
            final = conn.execute(
                """
                SELECT * FROM agent_lifecycle_cancellation_audits
                WHERE request_id = ?
                """,
                (normalized_request_id,),
            ).fetchone()
        assert final is not None
        return _lifecycle_cancellation_payload(final)

    def cancel_causal_approvals(
        self,
        session_id: str,
        *,
        request_id: str,
        scope_kind: str,
        scope_id: str,
        source_revision: int,
        reason: str,
        decided_at_ms: int | None = None,
    ) -> dict[str, object]:
        if scope_kind != "goal":
            raise ValueError("unsupported lifecycle cancellation scope")
        timestamp = _timestamp(decided_at_ms)
        normalized_reason = " ".join(str(reason or "").split())[:1000]
        causal_clause = "causal_goal_id = ?"
        causal_values: tuple[object, ...] = (scope_id,)
        owner = f"lifecycle:{request_id}"[:120]
        cancelled: list[str] = []
        excluded_room_bound: list[str] = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                f"""
                SELECT approval_id, state, decided_by, room_bound, receipt_json
                FROM agent_approvals
                WHERE session_id = ? AND {causal_clause}
                  AND state IN ('pending', 'approved', 'external_pending', 'stale')
                ORDER BY requested_at_ms, approval_id
                """,
                (session_id, *causal_values),
            ).fetchall()
            for row in rows:
                approval_id = str(row["approval_id"])
                if bool(row["room_bound"]):
                    excluded_room_bound.append(approval_id)
                    continue
                if str(row["state"]) == "stale":
                    if str(row["decided_by"]) == owner:
                        cancelled.append(approval_id)
                    continue
                receipt = {
                    "schemaVersion": "rag-ime.agent-approval-lifecycle-cancellation.v1",
                    "requestId": request_id,
                    "approvalId": approval_id,
                    "sessionId": session_id,
                    "scopeKind": scope_kind,
                    "scopeId": scope_id,
                    "sourceRevision": int(source_revision),
                    "reason": normalized_reason,
                    "mutationApplied": False,
                    "createdAtMs": timestamp,
                }
                cursor = conn.execute(
                    """
                    UPDATE agent_approvals
                    SET state = 'stale', decided_at_ms = ?, decided_by = ?,
                        receipt_json = ?
                    WHERE approval_id = ?
                      AND state IN ('pending', 'approved', 'external_pending')
                      AND room_bound = 0
                    """,
                    (
                        timestamp,
                        owner,
                        json.dumps(
                            receipt,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        approval_id,
                    ),
                )
                if cursor.rowcount == 1:
                    cancelled.append(approval_id)
        return {
            "schemaVersion": "rag-ime.agent-approval-lifecycle-cancellation-summary.v1",
            "requestId": request_id,
            "sessionId": session_id,
            "scopeKind": scope_kind,
            "scopeId": scope_id,
            "sourceRevision": int(source_revision),
            "cancelledApprovalIds": cancelled,
            "excludedRoomBoundApprovalIds": excluded_room_bound,
            "createdAtMs": timestamp,
        }

    def cancel_pending_approvals(
        self,
        session_id: str,
        *,
        reason: str = "user_abort",
        turn_id: str = "",
        now_ms: int | None = None,
    ) -> dict[str, object]:
        """Close pending approvals when the owning turn is aborted.

        The update is conditional and therefore safe to repeat after a late
        runtime callback or a second stop request.
        """

        self.get(session_id)
        timestamp = _timestamp(now_ms)
        normalized_reason = " ".join(str(reason or "user_abort").split())[:240]
        normalized_turn_id = " ".join(str(turn_id or "").split())[:240]
        cancelled: list[str] = []
        executing: list[str] = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT *
                FROM agent_approvals
                WHERE session_id = ? AND state IN ('pending', 'approved')
                ORDER BY requested_at_ms, approval_id
                """,
                (session_id,),
            ).fetchall()
            for row in rows:
                approval_id = str(row["approval_id"])
                if int(row["execution_claimed_at_ms"] or 0) > 0:
                    executing.append(approval_id)
                    continue
                causal_turn_id = str(row["causal_turn_id"] or "")
                receipt = {
                    "schemaVersion": (
                        "rag-ime.agent-approval-cancellation.v1"
                    ),
                    "approvalId": approval_id,
                    "sessionId": session_id,
                    "toolCallId": str(row["tool_call_id"] or ""),
                    "turnId": causal_turn_id or normalized_turn_id,
                    "reason": normalized_reason,
                    "mutationApplied": False,
                    "createdAtMs": timestamp,
                }
                cursor = conn.execute(
                    """
                    UPDATE agent_approvals
                    SET state = 'stale', decided_at_ms = ?,
                        decided_by = ?, receipt_json = ?
                    WHERE approval_id = ?
                      AND state IN ('pending', 'approved')
                      AND execution_claimed_at_ms = 0
                    """,
                    (
                        timestamp,
                        "runtime-cancellation",
                        json.dumps(
                            receipt,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        approval_id,
                    ),
                )
                if cursor.rowcount == 1:
                    cancelled.append(approval_id)
        return {
            "schemaVersion": (
                "rag-ime.agent-approval-cancellation-summary.v1"
            ),
            "sessionId": session_id,
            "turnId": normalized_turn_id,
            "reason": normalized_reason,
            "cancelledApprovalIds": cancelled,
            "executingApprovalIds": executing,
            "createdAtMs": timestamp,
        }

    @contextmanager
    def approval_creation_scope(
        self,
        *,
        session_id: str,
        tool_call_id: str,
        room_context: Mapping[str, object] | None = None,
    ) -> Iterator[None]:
        """Bind one request identity to any approval created in this call.

        Tool-specific preparation methods all converge on ``create_approval``.
        Keeping the request identity in a ContextVar avoids threading it
        through every preview builder while remaining isolated across
        concurrent Runtime calls. The eventual INSERT persists the Tool and
        exact Room dispatch identity in the same transaction as the approval.
        """

        normalized_session_id = str(session_id).strip()
        normalized_tool_call_id = str(tool_call_id).strip()
        if not normalized_session_id:
            raise ValueError("approval sessionId must not be empty")
        if not normalized_tool_call_id or len(normalized_tool_call_id) > 512:
            raise ValueError(
                "approval toolCallId must contain between 1 and 512 characters"
            )
        normalized_room_context: dict[str, object] | None = None
        if room_context is not None:
            room_id = " ".join(
                str(room_context.get("roomId") or "").split()
            )[:240]
            root_id = " ".join(
                str(room_context.get("rootId") or "").split()
            )[:240]
            dispatch_id = " ".join(
                str(room_context.get("dispatchId") or "").split()
            )[:240]
            try:
                generation = int(room_context.get("generation") or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "Room approval binding requires an active Room root, "
                    "dispatch, and Runtime generation"
                ) from exc
            if not room_id or not root_id or not dispatch_id or generation <= 0:
                raise ValueError(
                    "Room approval binding requires an active Room root, "
                    "dispatch, and Runtime generation"
                )
            normalized_room_context = {
                "roomId": room_id,
                "rootId": root_id,
                "dispatchId": dispatch_id,
                "generation": generation,
            }
        token = self._approval_creation_context.set(
            {
                "sessionId": normalized_session_id,
                "toolCallId": normalized_tool_call_id,
                "roomContext": normalized_room_context,
            }
        )
        try:
            yield
        finally:
            self._approval_creation_context.reset(token)

    def create_approval(
        self,
        *,
        session_id: str,
        tool_name: str,
        operation: str,
        payload_sha256: str,
        preview: Mapping[str, object],
        risk_level: str,
        requested_at_ms: int | None = None,
        ttl_ms: int = 60_000,
        causal_metadata: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        session = self.get(session_id)
        tool = " ".join(str(tool_name).split())[:120]
        action = " ".join(str(operation).split())[:120]
        digest = str(payload_sha256).strip().lower()
        if not tool or not action:
            raise ValueError("approval tool and operation must not be empty")
        if not _valid_sha256(digest):
            raise ValueError("approval payloadSha256 must be a 64-character hex digest")
        if risk_level not in {"R1", "R2", "R3"}:
            raise ValueError("approval risk level must be R1, R2, or R3")
        timestamp = _timestamp(requested_at_ms)
        execution_mode = normalize_execution_mode(
            session.get("executionMode"),
            tool_profile_version=session.get("toolProfileVersion"),
        )
        effective_ttl_ms = max(ttl_ms, 180_000) if execution_mode == FULL_TRUST_EXECUTION_MODE else ttl_ms
        bounded_ttl = max(1_000, min(int(effective_ttl_ms), 5 * 60_000))
        approval_id = f"approval:{uuid.uuid4()}"
        request_context = self._approval_creation_context.get()
        if (
            not isinstance(request_context, Mapping)
            or str(request_context.get("sessionId") or "") != session_id
        ):
            request_context = {}
        bound_tool_call_id = str(
            request_context.get("toolCallId") or ""
        ).strip()
        room_context = (
            request_context.get("roomContext")
            if isinstance(request_context.get("roomContext"), Mapping)
            else None
        )
        preview_payload = dict(preview)
        if (
            execution_mode == FULL_TRUST_EXECUTION_MODE
            and room_context is None
        ):
            preview_payload["approvalArbitration"] = pending_model_arbitration()
        preview_json = json.dumps(
            preview_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._connect() as conn:
            causal = _approval_causal_metadata(
                conn,
                session_id=session_id,
                preview=preview,
                supplied=causal_metadata,
            )
            if room_context is not None:
                causal.update(
                    {
                        "roomBound": True,
                        "roomId": str(room_context["roomId"]),
                        "rootId": str(room_context["rootId"]),
                        "dispatchId": str(room_context["dispatchId"]),
                        "generation": int(room_context["generation"]),
                    }
                )
            conn.execute(
                """
                INSERT INTO agent_approvals(
                    approval_id, session_id, tool_name, operation, payload_sha256,
                    preview_json, risk_level, state, requested_at_ms, expires_at_ms,
                    causal_todo_id, causal_todo_revision, causal_goal_id,
                    causal_goal_revision, causal_turn_id, room_bound,
                    tool_call_id, room_id, room_root_id, room_dispatch_id,
                    room_generation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval_id,
                    session_id,
                    tool,
                    action,
                    digest,
                    preview_json,
                    risk_level,
                    timestamp,
                    timestamp + bounded_ttl,
                    causal["todoId"],
                    causal["todoRevision"],
                    causal["goalId"],
                    causal["goalRevision"],
                    causal["turnId"],
                    int(bool(causal["roomBound"])),
                    bound_tool_call_id,
                    str(causal["roomId"]),
                    str(causal["rootId"]),
                    str(causal["dispatchId"]),
                    int(causal["generation"]),
                ),
            )
        return self.get_approval(approval_id, now_ms=timestamp)

    def get_approval(self, approval_id: str, *, now_ms: int | None = None) -> dict[str, object]:
        timestamp = _timestamp(now_ms)
        with self._connect() as conn:
            self._expire_approvals(conn, timestamp, approval_id=approval_id)
            row = conn.execute(
                "SELECT * FROM agent_approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
        if row is None:
            raise AgentApprovalNotFound(approval_id)
        return _approval_payload(row)

    def bind_approval_tool_call(
        self,
        approval_id: str,
        *,
        tool_call_id: str,
    ) -> dict[str, object]:
        """Attach one immutable Pi tool-call identity to an approval."""

        normalized_tool_call_id = str(tool_call_id).strip()
        if not normalized_tool_call_id or len(normalized_tool_call_id) > 512:
            raise ValueError(
                "approval toolCallId must contain between 1 and 512 characters"
            )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT tool_call_id
                FROM agent_approvals
                WHERE approval_id = ?
                """,
                (approval_id,),
            ).fetchone()
            if row is None:
                raise AgentApprovalNotFound(approval_id)
            existing = str(row["tool_call_id"] or "")
            if existing and existing != normalized_tool_call_id:
                raise ValueError("approval is already bound to another tool call")
            if not existing:
                conn.execute(
                    """
                    UPDATE agent_approvals
                    SET tool_call_id = ?
                    WHERE approval_id = ? AND tool_call_id = ''
                    """,
                    (normalized_tool_call_id, approval_id),
                )
        return self.get_approval(approval_id)

    def bind_approval_room_dispatch(
        self,
        approval_id: str,
        *,
        room_context: Mapping[str, object],
        now_ms: int | None = None,
    ) -> dict[str, object]:
        """Durably bind a pending approval to an already-authorized Room dispatch.

        The Tool gateway validates the live dispatch before calling this method.
        Persisting the binding keeps the approval application from losing the
        Room policy when it re-reads the ordinary Session row.  A stale
        full-trust model-arbitration preview is removed at the same boundary so
        the UI never claims an independent approval Agent is involved.
        """

        timestamp = _timestamp(now_ms)
        room_id = " ".join(str(room_context.get("roomId") or "").split())[:240]
        root_id = " ".join(str(room_context.get("rootId") or "").split())[:240]
        dispatch_id = " ".join(
            str(room_context.get("dispatchId") or "").split()
        )[:240]
        generation = max(0, int(room_context.get("generation") or 0))
        if not room_id or not root_id or not dispatch_id or generation <= 0:
            raise ValueError(
                "Room approval binding requires an active Room root, dispatch, "
                "and Runtime generation"
            )
        with self._connect() as conn:
            self._expire_approvals(conn, timestamp, approval_id=approval_id)
            row = conn.execute(
                "SELECT state, preview_json FROM agent_approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if row is None:
                raise AgentApprovalNotFound(approval_id)
            if str(row["state"]) != "pending":
                raise ValueError("only pending approvals can bind a Room dispatch")
            preview = json.loads(str(row["preview_json"] or "{}"))
            preview_payload = preview if isinstance(preview, dict) else {}
            preview_payload.pop("approvalArbitration", None)
            conn.execute(
                """
                UPDATE agent_approvals
                SET room_bound = 1, room_id = ?, room_root_id = ?,
                    room_dispatch_id = ?, room_generation = ?, preview_json = ?
                WHERE approval_id = ? AND state = 'pending'
                """,
                (
                    room_id,
                    root_id,
                    dispatch_id,
                    generation,
                    json.dumps(
                        preview_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    approval_id,
                ),
            )
        return self.get_approval(approval_id, now_ms=timestamp)

    def claim_approval_execution(
        self,
        approval_id: str,
        *,
        room_context: Mapping[str, object] | None = None,
        now_ms: int | None = None,
    ) -> dict[str, object]:
        """Atomically fence cancellation and Room rotation before an effect."""

        timestamp = _timestamp(now_ms)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT approval.*, COALESCE(binding.generation, 0)
                       AS current_runtime_generation,
                       session.execution_mode AS current_execution_mode,
                       session.tool_profile_version AS current_tool_profile_version
                FROM agent_approvals AS approval
                JOIN agent_sessions AS session
                  ON session.id = approval.session_id
                LEFT JOIN agent_runtime_bindings AS binding
                  ON binding.session_id = approval.session_id
                WHERE approval.approval_id = ?
                """,
                (approval_id,),
            ).fetchone()
            if row is None:
                raise AgentApprovalNotFound(approval_id)
            if (
                str(row["state"]) != "approved"
                or int(row["execution_claimed_at_ms"] or 0) != 0
            ):
                raise ValueError(
                    "approval execution cannot start from "
                    f"state {row['state']} with claim {row['execution_claimed_at_ms']}"
                )

            current_mode = normalize_execution_mode(
                row["current_execution_mode"],
                tool_profile_version=row["current_tool_profile_version"],
            )
            if current_mode == READ_ONLY_EXECUTION_MODE:
                conn.execute(
                    """
                    UPDATE agent_approvals
                    SET state = 'rejected', decided_at_ms = ?, decided_by = ?
                    WHERE approval_id = ? AND state = 'approved'
                      AND execution_claimed_at_ms = 0
                    """,
                    (
                        timestamp,
                        "execution-policy:read_only",
                        approval_id,
                    ),
                )
            elif bool(row["room_bound"]):
                live = room_context if isinstance(room_context, Mapping) else {}
                persisted_room_id = str(row["room_id"] or "").strip()
                persisted_root_id = str(row["room_root_id"] or "").strip()
                persisted_dispatch_id = str(
                    row["room_dispatch_id"] or ""
                ).strip()
                persisted_generation = int(row["room_generation"] or 0)
                live_room_id = str(live.get("roomId") or "").strip()
                live_root_id = str(live.get("rootId") or "").strip()
                live_dispatch_id = str(
                    live.get("dispatchId") or ""
                ).strip()
                live_generation = int(live.get("generation") or 0)
                current_runtime_generation = int(
                    row["current_runtime_generation"] or 0
                )
                binding_matches = (
                    bool(persisted_room_id)
                    and bool(persisted_root_id)
                    and bool(persisted_dispatch_id)
                    and persisted_generation > 0
                    and bool(live_room_id)
                    and bool(live_root_id)
                    and bool(live_dispatch_id)
                    and live_generation > 0
                    and current_runtime_generation > 0
                    and live_room_id == persisted_room_id
                    and live_root_id == persisted_root_id
                    and live_dispatch_id == persisted_dispatch_id
                    and live_generation == persisted_generation
                    and current_runtime_generation == persisted_generation
                )
                if not binding_matches:
                    conn.execute(
                        """
                        UPDATE agent_approvals
                        SET state = 'stale', decided_at_ms = ?, decided_by = ?
                        WHERE approval_id = ? AND state = 'approved'
                          AND execution_claimed_at_ms = 0
                        """,
                        (
                            timestamp,
                            "execution-policy:room_dispatch_stale",
                            approval_id,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE agent_approvals
                        SET execution_claimed_at_ms = ?
                        WHERE approval_id = ? AND state = 'approved'
                          AND execution_claimed_at_ms = 0
                        """,
                        (timestamp, approval_id),
                    )
            else:
                cursor = conn.execute(
                    """
                    UPDATE agent_approvals
                    SET execution_claimed_at_ms = ?
                    WHERE approval_id = ? AND state = 'approved'
                      AND execution_claimed_at_ms = 0
                    """,
                    (timestamp, approval_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("approval execution claim was lost")
        return self.get_approval(approval_id, now_ms=timestamp)

    def fail_abandoned_approval_executions(
        self,
        *,
        now_ms: int | None = None,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        """Close interrupted approvals inherited from an earlier Host.

        A claim is the irreversible boundary immediately before the Tool
        executor.  After a process restart PAW cannot prove whether the
        external effect happened, so replaying it could duplicate a
        non-idempotent action. Room approvals interrupted before that boundary
        are known not to have executed and can be made retryable without ever
        asking a human or a model. Persist an explicit receipt for both cases;
        terminal claimed rows are also returned so missing Session/Room
        projections can be repaired without replaying the effect.
        """

        timestamp = _timestamp(now_ms)
        recovered_ids: list[str] = []
        reconciliation_ids: list[str] = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT *
                FROM agent_approvals
                WHERE (
                    state = 'approved' AND execution_claimed_at_ms > 0
                ) OR (
                    room_bound = 1
                    AND room_id <> ''
                    AND room_root_id <> ''
                    AND room_dispatch_id <> ''
                    AND room_generation > 0
                    AND state IN ('pending', 'approved')
                    AND execution_claimed_at_ms = 0
                )
                ORDER BY requested_at_ms, approval_id
                """
            ).fetchall()
            for row in rows:
                approval_id = str(row["approval_id"])
                claimed_at_ms = int(row["execution_claimed_at_ms"] or 0)
                effect_may_have_occurred = claimed_at_ms > 0
                reason = (
                    "abandoned_execution_claim"
                    if effect_may_have_occurred
                    else "room_execution_interrupted_before_claim"
                    if str(row["state"] or "") == "approved"
                    else "room_execution_interrupted_before_decision"
                )
                receipt = {
                    "schemaVersion": "rag-ime.agent-operation-receipt.v1",
                    "approvalId": approval_id,
                    "toolId": str(row["tool_name"] or ""),
                    "operation": str(row["operation"] or ""),
                    "summary": (
                        "上次运行在工具执行后、终态保存前中断；"
                        "结果未知，本次不会自动重放。"
                        if effect_may_have_occurred
                        else "Room 在工具执行前中断；原操作没有执行，可以重新发起任务。"
                    ),
                    "reason": reason,
                    "mutationApplied": (
                        None if effect_may_have_occurred else False
                    ),
                    "executionOutcome": (
                        "unknown"
                        if effect_may_have_occurred
                        else "not_started"
                    ),
                    "effectMayHaveOccurred": effect_may_have_occurred,
                    "replayAllowed": not effect_may_have_occurred,
                    "retryTaskAllowed": not effect_may_have_occurred,
                    "executionClaimedAtMs": claimed_at_ms,
                    "reconciledAtMs": timestamp,
                }
                cursor = conn.execute(
                    """
                    UPDATE agent_approvals
                    SET state = 'failed', decided_at_ms = ?, decided_by = ?,
                        receipt_json = ?
                    WHERE approval_id = ?
                      AND state = ?
                      AND execution_claimed_at_ms = ?
                    """,
                    (
                        timestamp,
                        "startup-recovery",
                        json.dumps(
                            receipt,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        approval_id,
                        str(row["state"] or ""),
                        claimed_at_ms,
                    ),
                )
                if cursor.rowcount == 1:
                    recovered_ids.append(approval_id)
            # The approval row and its public Session/Room projection cannot be
            # committed in one SQLite transaction because projection owners
            # are separate services. Include earlier unknown-outcome rows on
            # every failed startup retry; downstream projection keys are
            # idempotent, so a crash after the state transition can heal
            # without ever replaying the Tool effect itself.
            retry_rows = conn.execute(
                """
                SELECT *
                FROM agent_approvals
                WHERE (
                    execution_claimed_at_ms > 0
                    AND state IN ('applied', 'failed', 'stale')
                ) OR (
                    room_bound = 1
                    AND room_id <> ''
                    AND room_root_id <> ''
                    AND room_dispatch_id <> ''
                    AND room_generation > 0
                    AND state = 'failed'
                )
                ORDER BY requested_at_ms, approval_id
                """
            ).fetchall()
            for row in retry_rows:
                try:
                    receipt = json.loads(str(row["receipt_json"] or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                exact_room_binding = (
                    bool(row["room_bound"])
                    and bool(str(row["room_id"] or "").strip())
                    and bool(str(row["room_root_id"] or "").strip())
                    and bool(str(row["room_dispatch_id"] or "").strip())
                    and int(row["room_generation"] or 0) > 0
                )
                if isinstance(receipt, dict) and (
                    exact_room_binding
                    or receipt.get("reason") == "abandoned_execution_claim"
                    or (
                        receipt.get("effectMayHaveOccurred") is True
                        and receipt.get("executionOutcome") == "unknown"
                        and receipt.get("replayAllowed") is False
                    )
                ):
                    reconciliation_ids.append(str(row["approval_id"]))
        return (
            [
                self.get_approval(value, now_ms=timestamp)
                for value in recovered_ids
            ],
            [
                self.get_approval(value, now_ms=timestamp)
                for value in reconciliation_ids
            ],
        )

    def list_approvals(
        self,
        *,
        session_id: str = "",
        state: str = "",
        limit: int = 100,
        now_ms: int | None = None,
    ) -> list[dict[str, object]]:
        normalized_session_id = str(session_id or "").strip()
        if normalized_session_id:
            self.get(normalized_session_id)
        allowed_states = {
            "pending",
            "approved",
            "external_pending",
            "rejected",
            "expired",
            "stale",
            "applied",
            "failed",
        }
        if state and state not in allowed_states:
            raise ValueError("unsupported approval state")
        bounded_limit = max(1, min(int(limit), 500))
        timestamp = _timestamp(now_ms)
        with self._connect() as conn:
            self._expire_approvals(conn, timestamp, session_id=normalized_session_id)
            if normalized_session_id and state:
                rows = conn.execute(
                    """
                    SELECT * FROM agent_approvals
                    WHERE session_id = ? AND state = ?
                    ORDER BY requested_at_ms DESC LIMIT ?
                    """,
                    (normalized_session_id, state, bounded_limit),
                ).fetchall()
            elif normalized_session_id:
                rows = conn.execute(
                    """
                    SELECT * FROM agent_approvals
                    WHERE session_id = ?
                    ORDER BY requested_at_ms DESC LIMIT ?
                    """,
                    (normalized_session_id, bounded_limit),
                ).fetchall()
            elif state:
                rows = conn.execute(
                    """
                    SELECT * FROM agent_approvals
                    WHERE state = ?
                    ORDER BY requested_at_ms DESC LIMIT ?
                    """,
                    (state, bounded_limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM agent_approvals
                    ORDER BY requested_at_ms DESC LIMIT ?
                    """,
                    (bounded_limit,),
                ).fetchall()
        return [_approval_payload(row) for row in rows]

    def decide_approval(
        self,
        approval_id: str,
        *,
        approved: bool,
        payload_sha256: str,
        decided_by: str = "native-control-center",
        decided_at_ms: int | None = None,
    ) -> dict[str, object]:
        timestamp = _timestamp(decided_at_ms)
        digest = str(payload_sha256).strip().lower()
        terminal_error = ""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if row is None:
                raise AgentApprovalNotFound(approval_id)
            if str(row["state"]) != "pending":
                terminal_error = f"approval is already {row['state']}"
            elif int(row["expires_at_ms"]) <= timestamp:
                conn.execute(
                    "UPDATE agent_approvals SET state = 'expired', decided_at_ms = ? WHERE approval_id = ?",
                    (timestamp, approval_id),
                )
                terminal_error = "approval has expired"
            elif not _valid_sha256(digest) or digest != str(row["payload_sha256"]):
                conn.execute(
                    "UPDATE agent_approvals SET state = 'stale', decided_at_ms = ? WHERE approval_id = ?",
                    (timestamp, approval_id),
                )
                terminal_error = "approval payload is stale"
            else:
                conn.execute(
                    """
                    UPDATE agent_approvals
                    SET state = ?, decided_at_ms = ?, decided_by = ?
                    WHERE approval_id = ? AND state = 'pending'
                    """,
                    (
                        "approved" if approved else "rejected",
                        timestamp,
                        " ".join(str(decided_by).split())[:120],
                        approval_id,
                    ),
                )
        if terminal_error:
            raise ValueError(terminal_error)
        return self.get_approval(approval_id, now_ms=timestamp)

    def complete_approval(
        self,
        approval_id: str,
        *,
        state: str,
        receipt: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        if state not in {"external_pending", "applied", "failed", "stale"}:
            raise ValueError("unsupported approval completion state")
        receipt_json = (
            json.dumps(dict(receipt), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if receipt is not None
            else None
        )
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_approvals SET state = ?, receipt_json = ?
                WHERE approval_id = ? AND state = 'approved'
                """,
                (state, receipt_json, approval_id),
            )
            if cursor.rowcount != 1:
                row = conn.execute(
                    "SELECT state FROM agent_approvals WHERE approval_id = ?",
                    (approval_id,),
                ).fetchone()
                if row is None:
                    raise AgentApprovalNotFound(approval_id)
                raise ValueError(f"approval cannot complete from state {row['state']}")
        return self.get_approval(approval_id)

    def fail_claimed_approval_execution(
        self,
        approval_id: str,
        *,
        receipt: Mapping[str, object],
    ) -> dict[str, object]:
        """Persist an unknown outcome after crossing the execution claim.

        This is deliberately separate from ``complete_approval``. It is the
        narrow recovery write used when the executor may have produced an
        external effect but the ordinary terminal write failed. A terminal
        row that actually committed before its caller saw an exception wins;
        only an ``approved`` row with an irreversible claim can become the
        unknown/fail-closed terminal.
        """

        normalized_receipt = dict(receipt)
        if (
            normalized_receipt.get("effectMayHaveOccurred") is not True
            or normalized_receipt.get("executionOutcome") != "unknown"
            or normalized_receipt.get("replayAllowed") is not False
            or normalized_receipt.get("mutationApplied") is False
        ):
            raise ValueError(
                "claimed approval recovery requires an unknown, non-replayable receipt"
            )
        receipt_json = json.dumps(
            normalized_receipt,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        terminal_state = ""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT state, execution_claimed_at_ms
                FROM agent_approvals
                WHERE approval_id = ?
                """,
                (approval_id,),
            ).fetchone()
            if row is None:
                raise AgentApprovalNotFound(approval_id)
            terminal_state = str(row["state"])
            if terminal_state in {
                "external_pending",
                "rejected",
                "expired",
                "stale",
                "applied",
                "failed",
            }:
                pass
            elif (
                terminal_state == "approved"
                and int(row["execution_claimed_at_ms"] or 0) > 0
            ):
                cursor = conn.execute(
                    """
                    UPDATE agent_approvals
                    SET state = 'failed', receipt_json = ?
                    WHERE approval_id = ? AND state = 'approved'
                      AND execution_claimed_at_ms > 0
                    """,
                    (receipt_json, approval_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError(
                        "claimed approval recovery lost its execution fence"
                    )
            else:
                raise ValueError(
                    "approval has no claimed execution to reconcile"
                )
        return self.get_approval(approval_id)

    def finalize_external_approval(
        self,
        approval_id: str,
        *,
        state: str,
        receipt: Mapping[str, object],
    ) -> dict[str, object]:
        if state not in {"applied", "failed"}:
            raise ValueError("unsupported external approval completion state")
        receipt_json = json.dumps(
            dict(receipt),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_approvals SET state = ?, receipt_json = ?
                WHERE approval_id = ? AND state = 'external_pending'
                """,
                (state, receipt_json, approval_id),
            )
            if cursor.rowcount != 1:
                row = conn.execute(
                    "SELECT state FROM agent_approvals WHERE approval_id = ?",
                    (approval_id,),
                ).fetchone()
                if row is None:
                    raise AgentApprovalNotFound(approval_id)
                raise ValueError(f"external approval cannot complete from state {row['state']}")
        return self.get_approval(approval_id)

    @staticmethod
    def _expire_approvals(
        conn: sqlite3.Connection,
        now_ms: int,
        *,
        approval_id: str = "",
        session_id: str = "",
    ) -> None:
        where = ["state = 'pending'", "expires_at_ms <= ?"]
        values: list[object] = [now_ms]
        if approval_id:
            where.append("approval_id = ?")
            values.append(approval_id)
        if session_id:
            where.append("session_id = ?")
            values.append(session_id)
        conn.execute(
            f"UPDATE agent_approvals SET state = 'expired', decided_at_ms = ? WHERE {' AND '.join(where)}",  # noqa: S608
            (now_ms, *values),
        )

    def _update(self, session_id: str, assignments: str, values: tuple[object, ...]) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE agent_sessions SET {assignments} WHERE id = ?",  # noqa: S608
                (*values, session_id),
            )
            if cursor.rowcount != 1:
                raise AgentSessionNotFound(session_id)

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


def _session_payload(
    row: sqlite3.Row,
    runtime_binding: Mapping[str, object] | None = None,
) -> dict[str, object]:
    roots = json.loads(str(row["workspace_roots_json"] or "[]"))
    allowed_tools = _stored_allowed_tools(row["allowed_tools_json"])
    disclosure_preferences = _stored_disclosure_preferences(
        row["disclosure_preferences_json"]
    )
    model_profile = str(row["model_profile"] or "").strip() or "pi/default"
    payload: dict[str, object] = {
        "schemaVersion": "rag-ime.agent-session.v1",
        "id": str(row["id"]),
        "piSessionId": str(row["pi_session_id"] or ""),
        "sessionFile": str(row["session_file"] or ""),
        "title": str(row["title"]),
        "mode": str(row["session_mode"]),
        "status": str(row["status"]),
        "sessionKind": str(row["session_kind"]),
        "evaluationSnapshot": bool(row["evaluation_snapshot"]),
        "surfaceKind": str(row["surface_kind"]),
        "ownerAppId": str(row["owner_app_id"]),
        "surfaceKey": str(row["surface_key"]),
        "roleId": canonical_agent_role_id(row["role_id"]),
        "roleVersion": str(row["role_version"]),
        "roleBookRevisionId": canonical_role_book_revision_id(
            row["role_book_revision_id"]
        ),
        "modelProfile": model_profile,
        "thinkingLevel": str(row["thinking_level"] or ""),
        "toolProfileVersion": str(row["tool_profile_version"]),
        "executionMode": normalize_execution_mode(
            row["execution_mode"],
            tool_profile_version=row["tool_profile_version"],
        ),
        "roomExecutionMode": _normalize_room_execution_mode(
            row["room_execution_mode"]
        ),
        # The durable grant is only valid for the exact roots it authorized.
        # Older rows can retain a grant hash after their roots were cleared;
        # never project that stale metadata as an active capability.
        "workspaceScopeGranted": workspace_scope_is_granted(
            {
                "workspaceRoots": [str(value) for value in roots if str(value).strip()],
                "toolProfileVersion": str(row["tool_profile_version"]),
                "executionMode": normalize_execution_mode(
                    row["execution_mode"],
                    tool_profile_version=row["tool_profile_version"],
                ),
                "workspaceScopeSha256": str(row["workspace_scope_sha256"] or ""),
                "workspaceScopeGrantedAtMs": int(
                    row["workspace_scope_granted_at_ms"] or 0
                ),
            }
        ),
        "workspaceScopeSha256": str(row["workspace_scope_sha256"] or ""),
        "workspaceScopeGrantedAtMs": int(
            row["workspace_scope_granted_at_ms"] or 0
        ),
        "toolAllowlistMode": "explicit" if allowed_tools is not None else "profile",
        "allowedTools": allowed_tools or [],
        "capabilityDisclosurePreferences": disclosure_preferences,
        "policyRevision": int(row["policy_revision"] or 1),
        "projectContextEnabled": bool(row["project_context_enabled"]),
        "piSkillsEnabled": bool(row["pi_skills_enabled"]),
        "codexSkillsEnabled": bool(row["codex_skills_enabled"]),
        "createdAtMs": int(row["created_at_ms"]),
        "updatedAtMs": int(row["updated_at_ms"]),
        "lastOpenedAtMs": int(row["last_opened_at_ms"]),
        "archivedAtMs": int(row["archived_at_ms"]) if row["archived_at_ms"] is not None else None,
        "messageCount": int(row["message_count"]),
        "lastMessagePreview": str(row["last_message_preview"] or ""),
        "lastTerminalTurnId": str(row["last_terminal_turn_id"] or ""),
        "workspaceRoots": [str(value) for value in roots if str(value).strip()],
        "shellPolicyVersion": str(row["shell_policy_version"]),
    }
    if runtime_binding is not None:
        payload["runtimeBinding"] = dict(runtime_binding)
    validate_contract(payload, "agent-session.v1.json")
    return payload


def _session_directory_payload(row: sqlite3.Row) -> dict[str, object]:
    roots = json.loads(str(row["workspace_roots_json"] or "[]"))
    return {
        "schemaVersion": "rag-ime.agent-session-directory-entry.v1",
        "id": str(row["id"]),
        "title": str(row["title"]),
        "mode": str(row["session_mode"]),
        "status": str(row["status"]),
        "sessionKind": str(row["session_kind"]),
        "surfaceKind": str(row["surface_kind"]),
        "ownerAppId": str(row["owner_app_id"]),
        "surfaceKey": str(row["surface_key"]),
        "roleId": canonical_agent_role_id(row["role_id"]),
        "roleVersion": str(row["role_version"]),
        "updatedAtMs": int(row["updated_at_ms"]),
        "messageCount": int(row["message_count"]),
        "lastMessagePreview": str(row["last_message_preview"] or ""),
        "workspaceRoots": [
            str(value) for value in roots if str(value).strip()
        ],
    }


def _joined_runtime_binding(row: sqlite3.Row) -> dict[str, object] | None:
    if row["runtime_driver_id"] is None:
        return None
    return {
        "schemaVersion": "rag-ime.agent-runtime-binding.v1",
        "driverId": str(row["runtime_driver_id"]),
        "runtimeKind": str(row["runtime_kind"]),
        "generation": int(row["runtime_generation"]),
        "state": str(row["runtime_binding_state"]),
        "createdAtMs": int(row["runtime_binding_created_at_ms"]),
        "updatedAtMs": int(row["runtime_binding_updated_at_ms"]),
    }


def _runtime_binding_payload(row: sqlite3.Row) -> dict[str, object]:
    try:
        metadata = json.loads(str(row["metadata_json"] or "{}"))
    except json.JSONDecodeError:
        metadata = {}
    return {
        "schemaVersion": "rag-ime.agent-runtime-binding.v1",
        "sessionId": str(row["session_id"]),
        "driverId": str(row["driver_id"]),
        "runtimeKind": str(row["runtime_kind"]),
        "externalSessionId": str(row["external_session_id"]),
        "transcriptRef": str(row["transcript_ref"] or ""),
        "branchAnchor": str(row["branch_anchor"] or ""),
        "generation": int(row["generation"]),
        "state": str(row["binding_state"]),
        "metadata": metadata if isinstance(metadata, dict) else {},
        "createdAtMs": int(row["created_at_ms"]),
        "updatedAtMs": int(row["updated_at_ms"]),
    }


def _agent_todo_projection(
    conn: sqlite3.Connection,
    session_id: str,
) -> dict[str, object]:
    row = conn.execute(
        """
        SELECT revision, phases_json, actor, created_at_ms, room_lineage_json
        FROM agent_todo_events
        WHERE session_id = ?
        ORDER BY revision DESC
        LIMIT 1
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        phases: list[dict[str, object]] = []
        revision = 0
        actor = "agent"
        updated_at_ms = 0
        room_lineage: dict[str, object] | None = None
    else:
        try:
            decoded = json.loads(str(row["phases_json"]))
        except json.JSONDecodeError as exc:
            raise ValueError("stored todo state is invalid") from exc
        if not isinstance(decoded, list):
            raise ValueError("stored todo state is invalid")
        phases = [dict(phase) for phase in decoded if isinstance(phase, Mapping)]
        if len(phases) != len(decoded):
            raise ValueError("stored todo state is invalid")
        revision = int(row["revision"])
        actor = str(row["actor"])
        updated_at_ms = int(row["created_at_ms"])
        try:
            decoded_lineage = json.loads(str(row["room_lineage_json"] or "null"))
        except json.JSONDecodeError as exc:
            raise ValueError("stored Todo Room lineage is invalid") from exc
        if decoded_lineage is not None and not isinstance(
            decoded_lineage,
            Mapping,
        ):
            raise ValueError("stored Todo Room lineage is invalid")
        room_lineage = (
            dict(decoded_lineage)
            if isinstance(decoded_lineage, Mapping)
            else None
        )
    _validate_todo_phases(phases)
    counts = {
        "total": 0,
        "pending": 0,
        "inProgress": 0,
        "blocked": 0,
        "completed": 0,
        "abandoned": 0,
    }
    for phase in phases:
        for item in phase["tasks"]:
            status = str(item["status"])
            counts["total"] += 1
            counts[{
                "pending": "pending",
                "in_progress": "inProgress",
                "blocked": "blocked",
                "completed": "completed",
                "abandoned": "abandoned",
            }[status]] += 1
    return {
        "schemaVersion": "rag-ime.agent-todo.v1",
        "id": f"todo:{session_id}",
        "sessionId": session_id,
        "revision": revision,
        "actor": actor,
        "updatedAtMs": updated_at_ms,
        "roomLineage": room_lineage,
        "phases": phases,
        "counts": counts,
    }


def agent_todo_projection(
    conn: sqlite3.Connection,
    session_id: str,
) -> dict[str, object]:
    """Read the existing Session Todo authority through a shared projection."""

    return _agent_todo_projection(conn, session_id)


def _clone_todo_phases(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise ValueError("todo phases must be an array")
    return [
        {
            "name": str(phase["name"]),
            "tasks": [
                {
                    "content": str(item["content"]),
                    "status": str(item["status"]),
                    **(
                        {"reason": str(item["reason"])}
                        if str(item.get("reason") or "").strip()
                        else {}
                    ),
                }
                for item in phase["tasks"]
            ],
        }
        for phase in value
    ]


def _todo_init(payload: Mapping[str, object]) -> list[dict[str, object]]:
    if "list" in payload and "items" in payload:
        raise ValueError("init accepts list or items, not both")
    phases: list[dict[str, object]] = []
    if "list" in payload:
        raw_list = payload.get("list")
        if not isinstance(raw_list, list):
            raise ValueError("Missing list for init operation")
        phase_names: set[str] = set()
        task_contents: set[str] = set()
        for raw_phase in raw_list:
            if not isinstance(raw_phase, Mapping):
                raise ValueError("todo init list entries must be objects")
            phase_name = _bounded_todo_text(
                raw_phase.get("phase"),
                field="phase",
                maximum=80,
            )
            if phase_name in phase_names:
                raise ValueError(f'Duplicate phase "{phase_name}" in init list')
            phase_names.add(phase_name)
            items = _todo_input_items(raw_phase.get("items"), field="items")
            for item in items:
                if item in task_contents:
                    raise ValueError(f'Duplicate task "{item}" in init list')
                task_contents.add(item)
            phases.append(
                {
                    "name": phase_name,
                    "tasks": [
                        {"content": item, "status": "pending"}
                        for item in items
                    ],
                }
            )
    else:
        items = _todo_input_items(payload.get("items"), field="items")
        phase_name = _bounded_todo_text(
            payload.get("phase") or "Tasks",
            field="phase",
            maximum=80,
        )
        phases.append(
            {
                "name": phase_name,
                "tasks": [
                    {"content": item, "status": "pending"}
                    for item in items
                ],
            }
        )
    return phases


def _todo_input_items(value: object, *, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"Missing {field} for todo operation")
    items = [
        _bounded_todo_text(item, field="task content", maximum=240)
        for item in value
    ]
    if len(set(items)) != len(items):
        duplicate = next(item for item in items if items.count(item) > 1)
        raise ValueError(f'Duplicate task "{duplicate}"')
    return items


def _required_todo_target(
    payload: Mapping[str, object],
    field: str,
    *,
    maximum: int,
) -> str:
    if field not in payload:
        label = "task content" if field == "task" else "phase name"
        raise ValueError(f"Missing {label}")
    return _bounded_todo_text(payload.get(field), field=field, maximum=maximum)


def _optional_todo_reason(payload: Mapping[str, object]) -> str:
    if "reason" not in payload or not str(payload.get("reason") or "").strip():
        return ""
    return _bounded_todo_text(
        payload.get("reason"),
        field="reason",
        maximum=500,
    )


def _find_todo_task(
    phases: list[dict[str, object]],
    content: str,
) -> dict[str, object] | None:
    for phase in phases:
        for item in phase["tasks"]:
            if item["content"] == content:
                return item
    return None


def _todo_task_not_found(
    content: str,
    phases: list[dict[str, object]],
) -> str:
    suffix = ""
    if not any(phase["tasks"] for phase in phases):
        suffix = "; todo list is empty"
    elif content.startswith("task-") or content.startswith("todo-"):
        suffix = "; tasks are referenced by exact content, not generated ids"
    return f'Task "{content}" not found{suffix}'


def _todo_targets(
    phases: list[dict[str, object]],
    payload: Mapping[str, object],
) -> list[dict[str, object]]:
    has_task = "task" in payload
    has_phase = "phase" in payload
    if has_task and has_phase:
        raise ValueError("todo operation accepts task or phase, not both")
    if has_task:
        task = _required_todo_target(payload, "task", maximum=240)
        target = _find_todo_task(phases, task)
        if target is None:
            raise ValueError(_todo_task_not_found(task, phases))
        return [target]
    if has_phase:
        phase_name = _required_todo_target(payload, "phase", maximum=80)
        phase = next(
            (candidate for candidate in phases if candidate["name"] == phase_name),
            None,
        )
        if phase is None:
            raise ValueError(f'Phase "{phase_name}" not found')
        return list(phase["tasks"])
    raise ValueError("todo operation requires a task or phase")


def _todo_remove(
    phases: list[dict[str, object]],
    payload: Mapping[str, object],
) -> None:
    has_task = "task" in payload
    has_phase = "phase" in payload
    if has_task and has_phase:
        raise ValueError("todo operation accepts task or phase, not both")
    if has_task:
        task = _required_todo_target(payload, "task", maximum=240)
        for phase in phases:
            tasks = phase["tasks"]
            for index, item in enumerate(tasks):
                if item["content"] == task:
                    tasks.pop(index)
                    return
        raise ValueError(_todo_task_not_found(task, phases))
    if has_phase:
        phase_name = _required_todo_target(payload, "phase", maximum=80)
        phase = next(
            (candidate for candidate in phases if candidate["name"] == phase_name),
            None,
        )
        if phase is None:
            raise ValueError(f'Phase "{phase_name}" not found')
        phase["tasks"] = []
        return
    for phase in phases:
        phase["tasks"] = []


def _normalize_todo_in_progress(phases: list[dict[str, object]]) -> None:
    active_seen = False
    for phase in phases:
        for item in phase["tasks"]:
            if item["status"] != "in_progress":
                continue
            if active_seen:
                item["status"] = "pending"
            else:
                active_seen = True
    if active_seen:
        return
    for phase in phases:
        for item in phase["tasks"]:
            if item["status"] == "pending":
                item["status"] = "in_progress"
                return


def _todo_statuses(phases: list[dict[str, object]]) -> dict[str, str]:
    return {
        str(item["content"]): str(item["status"])
        for phase in phases
        for item in phase["tasks"]
    }


def _validate_todo_phases(phases: list[dict[str, object]]) -> None:
    if len(phases) > 16:
        raise ValueError("todo is limited to 16 phases")
    phase_names: set[str] = set()
    task_contents: set[str] = set()
    active_count = 0
    task_count = 0
    for phase in phases:
        if not isinstance(phase, Mapping):
            raise ValueError("todo phase must be an object")
        phase_name = _bounded_todo_text(
            phase.get("name"),
            field="phase",
            maximum=80,
        )
        if phase_name in phase_names:
            raise ValueError(f'Duplicate phase "{phase_name}"')
        phase_names.add(phase_name)
        tasks = phase.get("tasks")
        if not isinstance(tasks, list):
            raise ValueError("todo phase tasks must be an array")
        for item in tasks:
            if not isinstance(item, Mapping):
                raise ValueError("todo task must be an object")
            content = _bounded_todo_text(
                item.get("content"),
                field="task content",
                maximum=240,
            )
            if content in task_contents:
                raise ValueError(f'Duplicate task "{content}"')
            task_contents.add(content)
            status = str(item.get("status") or "")
            if status not in {
                "pending",
                "in_progress",
                "blocked",
                "completed",
                "abandoned",
            }:
                raise ValueError("todo task status is invalid")
            reason = str(item.get("reason") or "").strip()
            if reason:
                _bounded_todo_text(reason, field="reason", maximum=500)
            active_count += int(status == "in_progress")
            task_count += 1
    if active_count > 1:
        raise ValueError("only one todo task may be in_progress")
    if task_count > 100:
        raise ValueError("todo is limited to 100 tasks")


def _bounded_todo_text(
    value: object,
    *,
    field: str,
    maximum: int,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"todo {field} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"todo {field} must not be empty")
    if len(text) > maximum or "\x00" in text:
        raise ValueError(f"todo {field} is invalid")
    return text


def _agent_goal_projection(
    conn: sqlite3.Connection,
    session_id: str,
) -> dict[str, object]:
    row = conn.execute(
        """
        SELECT event_id, goal_id, sequence, objective, success_criteria,
               evidence_expectations_json, status, token_budget,
               time_budget_ms, tokens_used, elapsed_ms, completion_audit_id,
               cancellation_audit_id, actor, created_at_ms
        FROM agent_thread_goal_events
        WHERE session_id = ?
        ORDER BY sequence DESC
        LIMIT 1
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        return {
            "schemaVersion": "rag-ime.agent-goal.v1",
            "sessionId": session_id,
            "configured": False,
            "goalId": "",
            "revision": 0,
            "objective": "",
            "successCriteria": "",
            "evidenceExpectations": [],
            "status": "cleared",
            "budget": {"tokenLimit": None, "timeLimitMs": None},
            "usage": {"tokens": 0, "elapsedMs": 0},
            "remaining": {"tokens": None, "timeMs": None},
            "budgetExceeded": False,
            "completionAudit": None,
            "cancellationAudit": None,
            "updatedAtMs": 0,
        }
    token_budget = (
        int(row["token_budget"]) if row["token_budget"] is not None else None
    )
    time_budget_ms = (
        int(row["time_budget_ms"])
        if row["time_budget_ms"] is not None
        else None
    )
    tokens_used = int(row["tokens_used"])
    elapsed_ms = int(row["elapsed_ms"])
    remaining_tokens = (
        max(0, token_budget - tokens_used)
        if token_budget is not None
        else None
    )
    remaining_time = (
        max(0, time_budget_ms - elapsed_ms)
        if time_budget_ms is not None
        else None
    )
    completion_audit = None
    completion_audit_id = str(row["completion_audit_id"] or "")
    if completion_audit_id:
        audit_row = conn.execute(
            """
            SELECT audit_id, summary, evidence_json, completed_by, created_at_ms
            FROM agent_goal_completion_audits
            WHERE audit_id = ? AND session_id = ?
            """,
            (completion_audit_id, session_id),
        ).fetchone()
        if audit_row is not None:
            try:
                evidence = json.loads(str(audit_row["evidence_json"] or "[]"))
            except json.JSONDecodeError:
                evidence = []
            completion_audit = {
                "auditId": str(audit_row["audit_id"]),
                "summary": str(audit_row["summary"]),
                "evidence": evidence if isinstance(evidence, list) else [],
                "completedBy": str(audit_row["completed_by"]),
                "createdAtMs": int(audit_row["created_at_ms"]),
            }
    cancellation_audit = None
    cancellation_audit_id = str(row["cancellation_audit_id"] or "")
    if cancellation_audit_id:
        cancellation_row = conn.execute(
            """
            SELECT audit_id, reason, cancelled_by, created_at_ms
            FROM agent_goal_cancellation_audits
            WHERE audit_id = ? AND session_id = ?
            """,
            (cancellation_audit_id, session_id),
        ).fetchone()
        if cancellation_row is not None:
            cancellation_audit = {
                "auditId": str(cancellation_row["audit_id"]),
                "reason": str(cancellation_row["reason"]),
                "cancelledBy": str(cancellation_row["cancelled_by"]),
                "createdAtMs": int(cancellation_row["created_at_ms"]),
            }
    try:
        evidence_expectations = json.loads(
            str(row["evidence_expectations_json"] or "[]")
        )
    except json.JSONDecodeError:
        evidence_expectations = []
    if not isinstance(evidence_expectations, list):
        evidence_expectations = []
    status = str(row["status"])
    configured = status != "cleared"
    return {
        "schemaVersion": "rag-ime.agent-goal.v1",
        "sessionId": session_id,
        "configured": configured,
        "goalId": str(row["goal_id"]),
        "revision": int(row["sequence"]),
        "objective": str(row["objective"]),
        "successCriteria": str(row["success_criteria"]),
        "evidenceExpectations": evidence_expectations,
        "status": status,
        "budget": {
            "tokenLimit": token_budget,
            "timeLimitMs": time_budget_ms,
        },
        "usage": {"tokens": tokens_used, "elapsedMs": elapsed_ms},
        "remaining": {"tokens": remaining_tokens, "timeMs": remaining_time},
        "budgetExceeded": (
            (token_budget is not None and tokens_used >= token_budget)
            or (time_budget_ms is not None and elapsed_ms >= time_budget_ms)
        ),
        "completionAudit": completion_audit,
        "cancellationAudit": cancellation_audit,
        "updatedAtMs": int(row["created_at_ms"]),
    }


def _agent_goal_completion_authority_gate(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    goal_id: str,
) -> None:
    """Reject a Goal completion receipt that would strand open owned work.

    This is a read-only authority projection over the shared database, not a
    Markdown or prose check. Room WorkItems this Session owns, created, or is
    accountable for must already be reconciled (done/failed/cancelled, or
    explicitly blocked), and the Root WorkDocument bound to this Goal must not
    sit in a broken ``error`` lifecycle that cannot accept the terminal
    receipt. ``cancel`` stays ungated as the explicit abandon path.
    """

    participant = conn.execute(
        "SELECT id, room_id FROM agent_room_participants WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if participant is not None:
        participant_id = str(participant["id"])
        open_items = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM agent_room_work_items
                WHERE room_id = ?
                  AND state IN ('queued', 'active', 'review')
                  AND (
                      current_owner_participant_id = ?
                      OR created_by_participant_id = ?
                      OR accountable_participant_id = ?
                  )
                """,
                (
                    str(participant["room_id"]),
                    participant_id,
                    participant_id,
                    participant_id,
                ),
            ).fetchone()[0]
        )
        if open_items:
            raise AgentGoalExecutionBlocked(
                "goal_open_work_items",
                f"仍有 {open_items} 个此 Session 负责的 Room WorkItem 处于 "
                "queued/active/review；必须先显式验收、退回、阻塞或放弃这些工作"
                "再完成 Goal。",
            )
    broken_documents = int(
        conn.execute(
            "SELECT COUNT(*) FROM work_documents "
            "WHERE authority_key = ? AND state = 'error'",
            (f"session_goal:{goal_id}",),
        ).fetchone()[0]
    )
    if broken_documents:
        raise AgentGoalExecutionBlocked(
            "goal_root_work_document_error",
            "绑定此 Goal 的 Root WorkDocument 处于 error 状态，无法随完成回执"
            "进入终态；请先修复其归档生命周期。",
        )


def _agent_goal_continuation_budget(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    goal_id: str,
    limit: int,
) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT epoch, issued_count
        FROM agent_goal_continuation_budgets
        WHERE session_id = ? AND goal_id = ?
        """,
        (session_id, goal_id),
    ).fetchone()
    epoch = int(row["epoch"]) if row is not None else 1
    issued_count = int(row["issued_count"]) if row is not None else 0
    return {
        "epoch": epoch,
        "issuedCount": issued_count,
        "limit": int(limit),
        "remaining": max(0, int(limit) - issued_count),
    }


def _ensure_agent_goal_continuation_budget(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    goal_id: str,
    limit: int,
    updated_at_ms: int,
) -> dict[str, int]:
    conn.execute(
        """
        INSERT INTO agent_goal_continuation_budgets(
            session_id, goal_id, epoch, issued_count, updated_at_ms
        ) VALUES (?, ?, 1, 0, ?)
        ON CONFLICT(session_id, goal_id) DO NOTHING
        """,
        (session_id, goal_id, updated_at_ms),
    )
    return _agent_goal_continuation_budget(
        conn,
        session_id=session_id,
        goal_id=goal_id,
        limit=limit,
    )


def _reset_agent_goal_continuation_budget(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    goal_id: str,
    reset_existing: bool,
    updated_at_ms: int,
) -> None:
    conn.execute(
        """
        INSERT INTO agent_goal_continuation_budgets(
            session_id, goal_id, epoch, issued_count, updated_at_ms
        ) VALUES (?, ?, 1, 0, ?)
        ON CONFLICT(session_id, goal_id) DO UPDATE SET
            epoch = CASE
                WHEN ? THEN agent_goal_continuation_budgets.epoch + 1
                ELSE agent_goal_continuation_budgets.epoch
            END,
            issued_count = 0,
            updated_at_ms = excluded.updated_at_ms
        """,
        (
            session_id,
            goal_id,
            updated_at_ms,
            1 if reset_existing else 0,
        ),
    )


def _append_agent_goal_event(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    goal_id: str,
    objective: str,
    success_criteria: str,
    evidence_expectations: list[str],
    status: str,
    token_budget: int | None,
    time_budget_ms: int | None,
    tokens_used: int,
    elapsed_ms: int,
    completion_audit_id: str | None,
    cancellation_audit_id: str | None,
    actor: str,
    created_at_ms: int,
) -> dict[str, object]:
    sequence = int(
        conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM agent_thread_goal_events WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
    )
    event_id = f"goal-event:{uuid.uuid4()}"
    conn.execute(
        """
        INSERT INTO agent_thread_goal_events(
            event_id, session_id, goal_id, sequence, objective,
            success_criteria, evidence_expectations_json, status,
            token_budget, time_budget_ms, tokens_used, elapsed_ms,
            completion_audit_id, cancellation_audit_id, actor, created_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            session_id,
            goal_id,
            sequence,
            objective,
            success_criteria,
            json.dumps(
                evidence_expectations,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            status,
            token_budget,
            time_budget_ms,
            tokens_used,
            elapsed_ms,
            completion_audit_id,
            cancellation_audit_id,
            actor,
            created_at_ms,
        ),
    )
    return {
        "eventId": event_id,
        "goalId": goal_id,
        "sequence": sequence,
        "status": status,
        "actor": actor,
        "createdAtMs": created_at_ms,
    }


def _workflow_projection(
    session_id: str,
    todo: Mapping[str, object],
    goal: Mapping[str, object],
    *,
    room_dispatch_authorized: bool = False,
) -> dict[str, object]:
    gate = (
        _room_dispatch_act_gate(goal)
        if room_dispatch_authorized
        else _agent_act_gate(goal)
    )
    return {
        "schemaVersion": "rag-ime.agent-workflow-state.v1",
        "ok": True,
        "sessionId": session_id,
        "todo": dict(todo),
        "goal": dict(goal),
        "actGate": {
            **gate,
            "todoRevision": int(todo.get("revision") or 0),
            "goalRevision": int(goal.get("revision") or 0),
        },
    }


def _agent_act_gate(
    goal: Mapping[str, object],
) -> dict[str, object]:
    if goal.get("configured") is True:
        goal_status = str(goal.get("status") or "")
        if goal_status == "paused":
            return {
                "allowed": False,
                "reason": "goal_paused",
                "message": "当前 Goal 已暂停，恢复后才能继续写入。",
            }
        if goal_status == "completed":
            return {
                "allowed": False,
                "reason": "goal_completed",
                "message": "当前 Goal 已完成审计，请清除或设置新 Goal。",
            }
        if goal_status == "cancelled":
            return {
                "allowed": False,
                "reason": "goal_cancelled",
                "message": "当前 Goal 已取消；清除后才能开始新的 Goal。",
            }
        if goal.get("budgetExceeded") is True:
            return {
                "allowed": False,
                "reason": "goal_budget_exhausted",
                "message": "Goal 的 Token 或时间预算已经耗尽。",
            }
    return {
        "allowed": True,
        "reason": "user_execution_request",
        "message": (
            "用户的执行请求允许在已授权工作区内继续；"
            "破坏性操作、范围扩张与其他高风险能力仍由原有策略审批。"
        ),
    }


def _room_dispatch_act_gate(
    goal: Mapping[str, object],
) -> dict[str, object]:
    if goal.get("configured") is True:
        goal_status = str(goal.get("status") or "")
        if goal_status == "paused":
            return {
                "allowed": False,
                "reason": "goal_paused",
                "message": "当前 Goal 已暂停，恢复后才能继续写入。",
            }
        if goal_status == "completed":
            return {
                "allowed": False,
                "reason": "goal_completed",
                "message": "当前 Goal 已完成审计，请清除或设置新 Goal。",
            }
        if goal_status == "cancelled":
            return {
                "allowed": False,
                "reason": "goal_cancelled",
                "message": "当前 Goal 已取消；清除后才能开始新的 Goal。",
            }
        if goal.get("budgetExceeded") is True:
            return {
                "allowed": False,
                "reason": "goal_budget_exhausted",
                "message": "Goal 的 Token 或时间预算已经耗尽。",
            }
    return {
        "allowed": True,
        "reason": "approved",
        "message": (
            "当前 Room 任务已经开始，可以在本轮权限范围内继续工作。"
        ),
    }


def _optional_positive_budget(
    value: object,
    *,
    field: str,
    maximum: int,
) -> int | None:
    if value in (None, ""):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"agent goal {field} must be an integer") from exc
    if normalized <= 0 or normalized > maximum:
        raise ValueError(f"agent goal {field} is out of range")
    return normalized


def _bounded_goal_text(
    value: object,
    *,
    field: str,
    maximum: int,
    required: bool = True,
) -> str:
    text = " ".join(str(value or "").split())[:maximum]
    if "\x00" in text or (required and not text):
        raise ValueError(f"agent goal {field} must not be empty")
    return text


def _goal_evidence_expectations(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError("goal evidenceExpectations must contain at most 20 items")
    expectations: list[str] = []
    for item in value:
        expectation = " ".join(str(item or "").split())[:600]
        if not expectation or "\x00" in expectation:
            raise ValueError("goal evidence expectation must not be empty")
        expectations.append(expectation)
    return expectations


def _goal_completion_evidence(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        raise ValueError("goal completion requires 1 to 20 evidence items")
    evidence: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("goal completion evidence must be an object")
        kind = str(item.get("kind") or "").strip()
        if kind not in {"test", "artifact", "commit", "receipt", "note"}:
            raise ValueError("goal completion evidence kind is invalid")
        summary = " ".join(str(item.get("summary") or "").split())[:600]
        reference = " ".join(str(item.get("reference") or "").split())[:1_000]
        if not summary or not reference:
            raise ValueError("goal completion evidence requires summary and reference")
        evidence.append({"kind": kind, "summary": summary, "reference": reference})
    return evidence


def _runtime_binding_text(
    value: object,
    *,
    field: str,
    maximum: int,
    required: bool = True,
) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ValueError(f"runtime binding {field} must not be empty")
    if len(text) > maximum or "\x00" in text:
        raise ValueError(f"runtime binding {field} is invalid")
    return text


def _like_pattern(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _url_query_value(value: str) -> str:
    return quote(str(value), safe="")


def _approval_causal_metadata(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    preview: Mapping[str, object],
    supplied: Mapping[str, object] | None,
) -> dict[str, object]:
    base_state = (
        preview.get("baseState")
        if isinstance(preview.get("baseState"), Mapping)
        else {}
    )
    room_bound = bool(
        str(base_state.get("roomInvocationReceiptId") or "").strip()
    )
    todo = _agent_todo_projection(conn, session_id)
    goal = _agent_goal_projection(conn, session_id)
    latest_turn = conn.execute(
        """
        SELECT turn_id
        FROM agent_runtime_events
        WHERE session_id = ? AND turn_id <> ''
        ORDER BY sequence DESC
        LIMIT 1
        """,
        (session_id,),
    ).fetchone()
    causal: dict[str, object] = {
        "todoId": str(todo["id"]),
        "todoRevision": int(todo["revision"]),
        "goalId": (
            str(goal["goalId"])
            if bool(goal["configured"])
            and str(goal["status"]) in {"active", "paused"}
            else ""
        ),
        "goalRevision": (
            int(goal["revision"])
            if bool(goal["configured"])
            and str(goal["status"]) in {"active", "paused"}
            else 0
        ),
        "turnId": str(latest_turn["turn_id"]) if latest_turn is not None else "",
        "roomBound": room_bound,
        "roomId": "",
        "rootId": "",
        "dispatchId": "",
        "generation": 0,
    }
    if supplied is not None:
        for key in (
            "todoId",
            "todoRevision",
            "goalId",
            "goalRevision",
            "turnId",
            "roomBound",
            "roomId",
            "rootId",
            "dispatchId",
            "generation",
        ):
            if key in supplied and key != "roomBound":
                causal[key] = supplied[key]
    causal["todoId"] = str(causal["todoId"] or "").strip()[:240]
    causal["todoRevision"] = max(0, int(causal["todoRevision"] or 0))
    causal["goalId"] = str(causal["goalId"] or "").strip()[:240]
    causal["goalRevision"] = max(0, int(causal["goalRevision"] or 0))
    causal["turnId"] = str(causal["turnId"] or "").strip()[:240]
    causal["roomId"] = str(causal["roomId"] or "").strip()[:240]
    causal["rootId"] = str(causal["rootId"] or "").strip()[:240]
    causal["dispatchId"] = str(causal["dispatchId"] or "").strip()[:240]
    causal["generation"] = max(0, int(causal["generation"] or 0))
    causal["roomBound"] = room_bound or bool(
        supplied.get("roomBound") if supplied is not None else False
    )
    return causal


def _create_lifecycle_cancellation_audit(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    request: Mapping[str, object],
    transition_revision: int,
    created_at_ms: int,
) -> dict[str, object]:
    request_id = str(request.get("requestId") or "").strip()
    scope_kind = str(request.get("scopeKind") or "").strip()
    scope_id = str(request.get("scopeId") or "").strip()
    action = str(request.get("action") or "").strip()
    source_revision = int(request.get("sourceRevision") or 0)
    source_turn_id = str(request.get("sourceTurnId") or "").strip()[:240]
    reason = " ".join(str(request.get("reason") or "").split())[:1000]
    if not request_id or len(request_id) > 240:
        raise ValueError("lifecycle cancellation requestId is invalid")
    if scope_kind != "goal" or not scope_id:
        raise ValueError("lifecycle cancellation scope is invalid")
    if action not in {"cancel", "pause"}:
        raise ValueError("lifecycle cancellation action is invalid")
    conn.execute(
        """
        INSERT OR IGNORE INTO agent_lifecycle_cancellation_audits(
            request_id, session_id, scope_kind, scope_id, source_revision,
            transition_revision, action, reason, state, source_turn_id,
            runtime_status, approval_status, job_status, delegation_status,
            created_at_ms, updated_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, 'pending', 'pending',
                  'pending', 'pending', ?, ?)
        """,
        (
            request_id,
            session_id,
            scope_kind,
            scope_id,
            source_revision,
            int(transition_revision),
            action,
            reason,
            source_turn_id,
            created_at_ms,
            created_at_ms,
        ),
    )
    row = conn.execute(
        """
        SELECT * FROM agent_lifecycle_cancellation_audits
        WHERE request_id = ?
        """,
        (request_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("lifecycle cancellation audit was not persisted")
    identity = (
        str(row["session_id"]),
        str(row["scope_kind"]),
        str(row["scope_id"]),
        int(row["source_revision"]),
        str(row["action"]),
    )
    expected = (
        session_id,
        scope_kind,
        scope_id,
        source_revision,
        action,
    )
    if identity != expected:
        raise ValueError("lifecycle cancellation requestId was reused")
    return _lifecycle_cancellation_payload(row)


def _lifecycle_cancellation_state(row: Mapping[str, object]) -> str:
    statuses = [
        str(row["runtime_status"]),
        str(row["approval_status"]),
        str(row["job_status"]),
        str(row["delegation_status"]),
    ]
    if "pending" in statuses:
        return "pending"
    if all(status in {"succeeded", "excluded"} for status in statuses):
        return "completed"
    if all(status == "unknown" for status in statuses):
        return "unknown"
    return "partial"


def _lifecycle_cancellation_payload(
    row: Mapping[str, object],
) -> dict[str, object]:
    def owner_payload(owner: str) -> dict[str, object]:
        raw = row[f"{owner}_receipt_json"]
        try:
            receipt = json.loads(str(raw or "{}"))
        except (TypeError, json.JSONDecodeError):
            receipt = {}
        return {
            "status": str(row[f"{owner}_status"]),
            "receipt": receipt if isinstance(receipt, dict) else {},
        }

    payload: dict[str, object] = {
        "schemaVersion": "rag-ime.agent-lifecycle-cancellation-audit.v1",
        "requestId": str(row["request_id"]),
        "sessionId": str(row["session_id"]),
        "scopeKind": str(row["scope_kind"]),
        "scopeId": str(row["scope_id"]),
        "sourceRevision": int(row["source_revision"]),
        "transitionRevision": int(row["transition_revision"]),
        "action": str(row["action"]),
        "reason": str(row["reason"]),
        "state": str(row["state"]),
        "sourceTurnId": str(row["source_turn_id"]),
        "owners": {
            "runtime": owner_payload("runtime"),
            "approval": owner_payload("approval"),
            "job": owner_payload("job"),
            "delegation": owner_payload("delegation"),
        },
        "createdAtMs": int(row["created_at_ms"]),
        "updatedAtMs": int(row["updated_at_ms"]),
    }
    validate_contract(
        payload,
        "agent-lifecycle-cancellation-audit.v1.json",
    )
    return payload


def _assert_lifecycle_receipt_safe(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = "".join(
                character
                for character in str(key).casefold()
                if character.isalnum()
            )
            if normalized in {
                "pid",
                "processid",
                "processhandle",
                "processgroupid",
            }:
                raise ValueError(
                    "process identity must not enter lifecycle cancellation audit"
                )
            _assert_lifecycle_receipt_safe(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _assert_lifecycle_receipt_safe(child)


def _approval_payload(row: sqlite3.Row) -> dict[str, object]:
    preview = json.loads(str(row["preview_json"] or "{}"))
    receipt = json.loads(str(row["receipt_json"])) if row["receipt_json"] else None
    payload: dict[str, object] = {
        "schemaVersion": "rag-ime.agent-approval.v1",
        "approvalId": str(row["approval_id"]),
        "sessionId": str(row["session_id"]),
        "toolId": str(row["tool_name"]),
        "operation": str(row["operation"]),
        "payloadSha256": str(row["payload_sha256"]),
        "preview": preview if isinstance(preview, dict) else {},
        "riskLevel": str(row["risk_level"]),
        "state": str(row["state"]),
        "requestedAtMs": int(row["requested_at_ms"]),
        "expiresAtMs": int(row["expires_at_ms"]),
        "decidedBy": str(row["decided_by"] or ""),
        "decidedAtMs": int(row["decided_at_ms"]) if row["decided_at_ms"] is not None else None,
        "receipt": receipt if isinstance(receipt, dict) else None,
        "causalMetadata": {
            "todoId": str(row["causal_todo_id"] or ""),
            "todoRevision": int(row["causal_todo_revision"] or 0),
            "goalId": str(row["causal_goal_id"] or ""),
            "goalRevision": int(row["causal_goal_revision"] or 0),
            "turnId": str(row["causal_turn_id"] or ""),
            "roomBound": bool(row["room_bound"]),
            "roomId": str(row["room_id"] or ""),
            "rootId": str(row["room_root_id"] or ""),
            "dispatchId": str(row["room_dispatch_id"] or ""),
            "generation": int(row["room_generation"] or 0),
        },
    }
    tool_call_id = str(row["tool_call_id"] or "")
    if tool_call_id:
        payload["toolCallId"] = tool_call_id
    validate_contract(payload, "agent-approval.v1.json")
    return payload


def _terminal_projection_candidate(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    turn_id: str,
    event_type: str,
    redacted_summary: str,
    metrics: Mapping[str, object],
) -> sqlite3.Row | None:
    columns = """
        approval_id, tool_call_id, causal_turn_id, tool_name,
        room_id, room_root_id, room_dispatch_id, room_generation
    """
    if event_type == "approval_resolved":
        identity = metrics.get("approvalIdentity")
        approval_id = (
            str(identity.get("approvalId") or "").strip()
            if isinstance(identity, Mapping)
            else ""
        )
        if not approval_id:
            return None
        return conn.execute(
            f"""
            SELECT {columns}
            FROM agent_approvals
            WHERE session_id = ? AND approval_id = ?
              AND state IN ('rejected', 'expired', 'stale', 'applied', 'failed')
            LIMIT 1
            """,  # noqa: S608 -- selected columns are static above.
            (session_id, approval_id),
        ).fetchone()
    if event_type != "tool_finished":
        return None

    identity = metrics.get("toolIdentity")
    tool_call_id = (
        str(identity.get("toolCallId") or "").strip()
        if isinstance(identity, Mapping)
        else ""
    )
    tool_name = (
        str(identity.get("toolName") or "").strip()
        if isinstance(identity, Mapping)
        else ""
    )
    normalized_turn_id = str(turn_id or "").strip()
    if tool_call_id:
        return conn.execute(
            f"""
            SELECT {columns}
            FROM agent_approvals
            WHERE session_id = ? AND tool_call_id = ?
              AND state IN (
                  'rejected', 'expired', 'stale', 'applied', 'failed'
              )
              AND (? = '' OR causal_turn_id IN ('', ?))
            ORDER BY
                CASE WHEN causal_turn_id = ? THEN 0 ELSE 1 END,
                requested_at_ms DESC,
                approval_id DESC
            LIMIT 1
            """,  # noqa: S608 -- selected columns are static above.
            (
                session_id,
                tool_call_id,
                normalized_turn_id,
                normalized_turn_id,
                normalized_turn_id,
            ),
        ).fetchone()

    # Rows created before the Tool identity index can still be claimed while
    # they are in the bounded event ledger. Match only the exact owning turn
    # and the already-redacted Tool name, mirroring the legacy read fallback.
    legacy_tool_name = " ".join(str(redacted_summary or "").split())[:120]
    if not normalized_turn_id or not legacy_tool_name:
        return None
    return conn.execute(
        f"""
        SELECT {columns}
        FROM agent_approvals
        WHERE session_id = ? AND causal_turn_id = ? AND tool_name = ?
          AND tool_call_id <> ''
          AND state IN (
              'rejected', 'expired', 'stale', 'applied', 'failed'
          )
        ORDER BY requested_at_ms DESC, approval_id DESC
        LIMIT 1
        """,  # noqa: S608 -- selected columns are static above.
        (session_id, normalized_turn_id, legacy_tool_name),
    ).fetchone()


def _workspace_roots(values: Iterable[str]) -> list[str]:
    roots: list[str] = []
    for value in values:
        path = str(value).strip()
        if not path:
            continue
        normalized = str(Path(path).expanduser().resolve(strict=False))
        if normalized not in roots:
            roots.append(normalized)
    return roots


def _surface_ownership(
    surface_kind: object,
    owner_app_id: object,
    surface_key: object,
    *,
    require_surface_key: bool,
) -> tuple[str, str, str]:
    normalized_surface = str(surface_kind or "agent").strip().lower()
    normalized_owner = str(owner_app_id or "").strip()
    normalized_key = str(surface_key or "").strip()
    if normalized_surface not in {"agent", "extension_app", "builtin_app"}:
        raise ValueError(
            "session surface kind must be agent, extension_app, or builtin_app"
        )
    if normalized_surface == "agent":
        if normalized_owner or normalized_key:
            raise ValueError("Agent sessions cannot carry App surface ownership")
        return "agent", "", ""
    if normalized_surface == "extension_app":
        if _EXTENSION_APP_ID.fullmatch(normalized_owner) is None:
            raise ValueError("Extension App sessions require a valid owner app id")
        if require_surface_key and not normalized_key:
            raise ValueError("Extension App sessions require a surface key")
        if normalized_key and _SURFACE_KEY.fullmatch(normalized_key) is None:
            raise ValueError("Extension App session surface key is invalid")
        return "extension_app", normalized_owner, normalized_key
    if normalized_owner not in _BUILTIN_APP_IDS:
        raise ValueError("built-in App sessions require a stable owner app id")
    if require_surface_key and not normalized_key:
        raise ValueError("built-in App sessions require a surface key")
    if not normalized_key:
        return "builtin_app", normalized_owner, ""
    if normalized_key != "timeline":
        match = _MEMORY_JOURNAL_KEY.fullmatch(normalized_key)
        if match is None:
            raise ValueError("Memory Session surface key is invalid")
        try:
            date.fromisoformat(match.group(1))
        except ValueError as exc:
            raise ValueError("Memory Session journal date is invalid") from exc
    return "builtin_app", normalized_owner, normalized_key


def _normalize_room_execution_mode(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in {"", ROOM_UNRESTRICTED_EXECUTION_MODE}:
        raise ValueError("unsupported Room execution mode")
    return normalized


def _allowed_tools(values: Iterable[str] | None) -> list[str] | None:
    if values is None:
        return None
    if isinstance(values, (str, bytes)):
        raise ValueError("allowedTools must be an array")
    tools: list[str] = []
    for value in values:
        tool = str(value or "").strip()
        if not tool or len(tool) > 120 or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in tool):
            raise ValueError("allowedTools contains an invalid tool id")
        if tool not in tools:
            tools.append(tool)
    if len(tools) > 100:
        raise ValueError("allowedTools contains too many tool ids")
    return tools


def _stored_allowed_tools(value: object) -> list[str] | None:
    if value is None:
        return None
    try:
        parsed = json.loads(str(value or "null"))
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list):
        return None
    return [str(item) for item in parsed if str(item).strip()]


def _disclosure_preferences(
    value: Mapping[str, object],
) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("capabilityDisclosurePreferences must be an object")
    if len(value) > 512:
        raise ValueError("capabilityDisclosurePreferences contains too many items")
    normalized: dict[str, str] = {}
    for raw_id, raw_preference in value.items():
        capability_id = str(raw_id or "").strip()
        if (
            not capability_id
            or len(capability_id) > 240
            or any(
                character
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:._-"
                for character in capability_id
            )
        ):
            raise ValueError(
                "capabilityDisclosurePreferences contains an invalid capability id"
            )
        preference = str(raw_preference or "").strip().lower()
        if preference not in {"inherit", "enabled", "disabled"}:
            raise ValueError(
                "capability disclosure preference must be inherit, enabled, or disabled"
            )
        normalized[capability_id] = preference
    return dict(sorted(normalized.items()))


def _stored_disclosure_preferences(value: object) -> dict[str, str]:
    if value is None:
        return {}
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, Mapping):
        return {}
    try:
        return _disclosure_preferences(parsed)
    except ValueError:
        return {}


def _timestamp(value: int | None) -> int:
    return int(value if value is not None else time.time() * 1000)


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
