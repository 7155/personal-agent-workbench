"""Bounded, project-scoped context for the Team workbench.

The project overview is deliberately a metadata projection.  It reads the
Team identity database and an already-created project state database, but it
does not construct a space service, start a Pi host, read a transcript, or
create a repository as a side effect of a GET.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable

from ..db import sqlite_connection
from .errors import TeamError


_MAX_ROOMS = 50
_MAX_WORK_ITEMS_PER_ROOM = 50
_MAX_DRAFTS = 50
_MAX_BRIEF_HISTORY = 20
_MAX_OBJECTIVE_LENGTH = 4_000
_MAX_CRITERIA = 20
_MAX_CRITERION_LENGTH = 500
_MAX_REVISION = 2**31 - 1
_MAX_METADATA_TEXT = 4_000
_SQLITE_BUSY_TIMEOUT_MS = 5_000


def _now_ms() -> int:
    return int(time.time() * 1000)


def _text(value: object, field: str, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise TeamError(400, "invalid_input", f"{field} must be text")
    if "\x00" in value:
        raise TeamError(400, "invalid_input", f"{field} contains an invalid character")
    normalized = value.strip()
    if not normalized and not allow_empty:
        raise TeamError(400, "invalid_input", f"{field} is required")
    if len(normalized) > maximum:
        raise TeamError(400, "invalid_input", f"{field} is too long")
    return normalized


def _identifier(value: object, field: str) -> str:
    return _text(value, field, 256)


def _not_found() -> TeamError:
    return TeamError(404, "space_not_found", "space not found")


def _invalid_body(message: str) -> TeamError:
    return TeamError(400, "invalid_input", message)


def _bounded_text(value: object, maximum: int = _MAX_METADATA_TEXT) -> str:
    text = str(value or "")
    return text[:maximum]


def _json_list(value: object) -> list[object]:
    try:
        decoded = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return decoded if isinstance(decoded, list) else []


def _json_object(value: object) -> dict[str, object]:
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(decoded) if isinstance(decoded, Mapping) else {}


def _requirements_revision(row: sqlite3.Row) -> int:
    """Read a frozen requirements revision with a legacy-schema default."""

    if "requirements_revision" not in row.keys():
        return 0
    return int(row["requirements_revision"])


def _brief_payload(row: sqlite3.Row | None) -> dict[str, object]:
    if row is None:
        return {
            "revision": 0,
            "objective": "",
            "acceptanceCriteria": [],
            "updatedAtMs": 0,
            "updatedByUserId": "",
            "updatedByDisplayName": "",
        }
    criteria = [
        item
        for item in _json_list(row["acceptance_criteria_json"])
        if isinstance(item, str)
    ]
    return {
        "revision": int(row["revision"]),
        "objective": str(row["objective"]),
        "acceptanceCriteria": criteria,
        "updatedAtMs": int(row["updated_at_ms"]),
        "updatedByUserId": str(row["updated_by_user_id"]),
        "updatedByDisplayName": str(row["updated_by_display_name"]),
    }


def _public_draft(
    row: sqlite3.Row,
    display_names: Mapping[str, str],
    current_requirements_revision: int,
) -> dict[str, object]:
    owner_id = str(row["owner_user_id"])
    requirements_revision = _requirements_revision(row)
    result: dict[str, object] = {
        "draftId": str(row["id"]),
        "spaceId": str(row["space_id"]),
        "targetBranch": str(row["target_branch"]),
        "workspaceId": str(row["workspace_id"]),
        "sessionId": str(row["session_id"]),
        "ownerUserId": owner_id,
        "description": str(row["description"]),
        "baseCommit": str(row["base_commit"]),
        "baseRevision": int(row["base_revision"]),
        "draftCommit": str(row["draft_commit"]),
        "manifest": _json_list(row["manifest_json"]),
        "manifestSha256": str(row["manifest_sha256"]),
        "status": str(row["status"]),
        "createdAtMs": int(row["created_at_ms"]),
        # Keep compatibility with the Gateway's existing public draft shape.
        "title": str(row["description"]),
        "creatorUserId": owner_id,
        "manifestHash": str(row["manifest_sha256"]),
        "creatorDisplayName": display_names.get(owner_id, ""),
        "requirementsRevision": requirements_revision,
        "currentRequirementsRevision": current_requirements_revision,
        "requirementsStale": requirements_revision != current_requirements_revision,
    }
    metadata = _json_object(row["result_json"])
    excluded = metadata.get("excludedPaths")
    if isinstance(excluded, list):
        result["excludedPaths"] = [item for item in excluded[:64] if isinstance(item, str)][:64]
        result["excludedPathsTruncated"] = bool(metadata.get("excludedPathsTruncated"))
    if row["integrated_at_ms"] is not None:
        result["integratedAtMs"] = int(row["integrated_at_ms"])
    if row["integrated_commit"] is not None:
        result["integratedCommit"] = str(row["integrated_commit"])
    return result


class TeamProjectContext:
    """Project overview and compare-and-swap brief publishing."""

    def __init__(self, team: Any, *, clock: Callable[[], int] | None = None) -> None:
        identity = getattr(team, "identity", None)
        if identity is None:
            raise ValueError("TeamProjectContext requires a team identity store")
        self.team = team
        self.identity = identity
        self.clock = clock or _now_ms

    def overview(self, actor: object, space_id: object) -> dict[str, object]:
        normalized_actor = _identifier(actor, "actor_user_id")
        normalized_space = _identifier(space_id, "space_id")
        space = self.identity.require_space(normalized_actor, normalized_space, action="read")
        if str(space.get("kind") or "") != "project":
            raise _not_found()

        members = self.identity.project_members(normalized_actor, normalized_space)
        (
            brief,
            brief_history,
            brief_history_truncated,
            drafts,
            drafts_truncated,
            repository,
            display_names,
            bindings,
            workspace_requirements,
            current_requirements_revision,
        ) = self._identity_metadata(
            normalized_space,
        )
        rooms, rooms_truncated, work_items_truncated = self._room_metadata(
            normalized_space,
            display_names,
            bindings,
            workspace_requirements,
            current_requirements_revision,
        )
        return {
            "project": {
                "id": str(space["id"]),
                "name": str(space["name"]),
                "role": str(space["role"]),
            },
            "brief": brief,
            "briefHistory": brief_history,
            "members": members,
            "rooms": rooms,
            "drafts": drafts,
            "repository": repository,
            "runtime": {"configured": getattr(self.team, "execution", None) is not None},
            "truncated": {
                "rooms": rooms_truncated,
                "workItems": work_items_truncated,
                "drafts": drafts_truncated,
                "briefHistory": brief_history_truncated,
            },
        }

    def publish_brief(
        self,
        actor: object,
        space_id: object,
        body: Mapping[str, object],
    ) -> dict[str, object]:
        normalized_actor = _identifier(actor, "actor_user_id")
        normalized_space = _identifier(space_id, "space_id")
        if not isinstance(body, Mapping):
            raise _invalid_body("brief body must be an object")
        expected_fields = {"baseRevision", "objective", "acceptanceCriteria"}
        if set(body) != expected_fields:
            raise _invalid_body("brief body must contain exactly baseRevision, objective, and acceptanceCriteria")

        base_revision = body.get("baseRevision")
        if isinstance(base_revision, bool) or not isinstance(base_revision, int):
            raise _invalid_body("baseRevision must be an integer")
        if not 0 <= base_revision <= _MAX_REVISION:
            raise _invalid_body("baseRevision is out of range")
        objective = _text(body.get("objective"), "objective", _MAX_OBJECTIVE_LENGTH)
        criteria_value = body.get("acceptanceCriteria")
        if not isinstance(criteria_value, list):
            raise _invalid_body("acceptanceCriteria must be an array")
        if not 1 <= len(criteria_value) <= _MAX_CRITERIA:
            raise _invalid_body("acceptanceCriteria must contain between 1 and 20 items")
        criteria: list[str] = []
        for index, value in enumerate(criteria_value):
            criteria.append(_text(value, f"acceptanceCriteria[{index}]", _MAX_CRITERION_LENGTH))

        self.identity.initialize()
        with sqlite_connection(
            self.identity.db_path,
            row_factory=sqlite3.Row,
            foreign_keys=True,
        ) as conn:
            conn.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}")
            conn.execute("BEGIN IMMEDIATE")
            space, user = self._require_project_manager(conn, normalized_actor, normalized_space)
            current_row = conn.execute(
                "SELECT revision FROM team_project_briefs WHERE space_id = ? ORDER BY revision DESC LIMIT 1",
                (normalized_space,),
            ).fetchone()
            current_revision = int(current_row["revision"]) if current_row is not None else 0
            if base_revision != current_revision:
                raise TeamError(
                    409,
                    "brief_revision_conflict",
                    "project brief changed; reload before publishing",
                )
            next_revision = current_revision + 1
            if next_revision > _MAX_REVISION:
                raise TeamError(409, "brief_revision_conflict", "project brief revision limit reached")
            now = int(self.clock())
            display_name = str(user["display_name"] or user["username"])
            conn.execute(
                """
                INSERT INTO team_project_briefs(
                    space_id, revision, objective, acceptance_criteria_json,
                    updated_at_ms, updated_by_user_id, updated_by_display_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(space["id"]),
                    next_revision,
                    objective,
                    json.dumps(criteria, ensure_ascii=False, separators=(",", ":")),
                    now,
                    normalized_actor,
                    display_name,
                ),
            )
            row = conn.execute(
                "SELECT * FROM team_project_briefs WHERE space_id = ? AND revision = ?",
                (normalized_space, next_revision),
            ).fetchone()
            assert row is not None
            return _brief_payload(row)

    def _require_project_manager(
        self,
        conn: sqlite3.Connection,
        actor: str,
        space_id: str,
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        user = conn.execute("SELECT * FROM team_users WHERE id = ?", (actor,)).fetchone()
        if user is None or not bool(user["active"]):
            raise TeamError(401, "authentication_required", "authentication is required")
        space = conn.execute("SELECT * FROM team_spaces WHERE id = ?", (space_id,)).fetchone()
        if space is None or str(space["kind"]) != "project":
            raise _not_found()
        membership = conn.execute(
            "SELECT role, active FROM team_project_members WHERE space_id = ? AND user_id = ?",
            (space_id, actor),
        ).fetchone()
        if membership is None or not bool(membership["active"]):
            raise _not_found()
        if str(membership["role"]) not in {"owner", "maintainer"}:
            raise TeamError(403, "forbidden", "project manage access is required")
        return space, user

    def _identity_metadata(
        self,
        space_id: str,
    ) -> tuple[
        dict[str, object],
        list[dict[str, object]],
        bool,
        list[dict[str, object]],
        bool,
        dict[str, object] | None,
        dict[str, str],
        dict[str, str],
        dict[str, int],
        int,
    ]:
        with sqlite_connection(
            self.identity.db_path,
            row_factory=sqlite3.Row,
            foreign_keys=True,
        ) as conn:
            current = conn.execute(
                "SELECT * FROM team_project_briefs WHERE space_id = ? ORDER BY revision DESC LIMIT 1",
                (space_id,),
            ).fetchone()
            current_requirements_revision = int(current["revision"]) if current is not None else 0
            history_rows = conn.execute(
                "SELECT * FROM team_project_briefs WHERE space_id = ? ORDER BY revision DESC LIMIT ?",
                (space_id, _MAX_BRIEF_HISTORY + 1),
            ).fetchall()
            draft_rows = conn.execute(
                "SELECT * FROM team_drafts WHERE space_id = ? ORDER BY created_at_ms DESC, id DESC LIMIT ?",
                (space_id, _MAX_DRAFTS + 1),
            ).fetchall()
            project = conn.execute(
                """
                SELECT target_branch, head_commit, revision
                FROM team_projects
                WHERE space_id = ?
                ORDER BY CASE WHEN target_branch = 'main' THEN 0 ELSE 1 END,
                         updated_at_ms DESC, target_branch
                LIMIT 1
                """,
                (space_id,),
            ).fetchone()
            names = {
                str(row["id"]): str(row["display_name"] or row["username"])
                for row in conn.execute("SELECT id, username, display_name FROM team_users")
            }
            display_names = {
                str(row["owner_user_id"]): names.get(str(row["owner_user_id"]), "")
                for row in draft_rows
            }
            brief_history = [_brief_payload(row) for row in history_rows[:_MAX_BRIEF_HISTORY]]
            workspace_requirements = {
                str(row["session_id"]): _requirements_revision(row)
                for row in conn.execute(
                    "SELECT * FROM team_workspaces WHERE space_id = ? AND session_id IS NOT NULL",
                    (space_id,),
                )
            }
            drafts = [
                _public_draft(row, display_names, current_requirements_revision)
                for row in draft_rows[:_MAX_DRAFTS]
            ]
            repository = (
                {
                    "branch": str(project["target_branch"]),
                    "headCommit": str(project["head_commit"]),
                    "revision": int(project["revision"]),
                }
                if project is not None
                else None
            )
            # Keep owner names for Room participants and all current/retired
            # Session bindings in this project without exposing those bindings.
            bindings = {
                str(row["session_id"]): str(row["owner_user_id"])
                for row in conn.execute(
                    "SELECT session_id, owner_user_id FROM team_session_bindings WHERE space_id = ? AND session_id IS NOT NULL",
                    (space_id,),
                )
            }
            display_names.update(
                {
                    user_id: names.get(user_id, "")
                    for user_id in bindings.values()
                }
            )
            return (
                _brief_payload(current),
                brief_history,
                len(history_rows) > _MAX_BRIEF_HISTORY,
                drafts,
                len(draft_rows) > _MAX_DRAFTS,
                repository,
                display_names,
                bindings,
                workspace_requirements,
                current_requirements_revision,
            )

    def _space_state_path(self, space_id: str) -> Path | None:
        data_root = getattr(self.team, "data_root", None)
        if data_root is not None:
            root = Path(str(data_root)).expanduser()
            if root.is_symlink():
                return None
            return root / "spaces" / space_id / "state.sqlite"
        services = getattr(self.team, "_services", None)
        if isinstance(services, Mapping):
            service = services.get(space_id)
            agent = getattr(service, "agent", None)
            candidate = getattr(agent, "db_path", None) or getattr(service, "db_path", None)
            if candidate is not None:
                return Path(str(candidate)).expanduser()
        return None

    def _room_metadata(
        self,
        space_id: str,
        display_names: Mapping[str, str],
        bindings: Mapping[str, str],
        workspace_requirements: Mapping[str, int],
        current_requirements_revision: int,
    ) -> tuple[list[dict[str, object]], bool, bool]:
        state_path = self._space_state_path(space_id)
        if state_path is None or state_path.is_symlink() or not state_path.is_file():
            return [], False, False
        try:
            with sqlite_connection(state_path, row_factory=sqlite3.Row, foreign_keys=True) as conn:
                room_rows = conn.execute(
                    """
                    SELECT id, title, status, updated_at_ms
                    FROM agent_rooms
                    ORDER BY updated_at_ms DESC, id DESC
                    LIMIT ?
                    """,
                    (_MAX_ROOMS + 1,),
                ).fetchall()
                has_work_items = self._table_exists(conn, "agent_room_work_items")
                has_participants = self._table_exists(conn, "agent_room_participants")
                rooms: list[dict[str, object]] = []
                work_items_truncated = False
                for room_row in room_rows[:_MAX_ROOMS]:
                    room_id = str(room_row["id"])
                    participant_count = 0
                    if has_participants:
                        participant_count = int(
                            conn.execute(
                                "SELECT COUNT(*) FROM agent_room_participants WHERE room_id = ? AND participant_status = 'active'",
                                (room_id,),
                            ).fetchone()[0]
                        )
                    items: list[dict[str, object]] = []
                    if has_work_items and has_participants:
                        work_rows = conn.execute(
                            """
                            SELECT w.id, w.room_id, w.objective, w.state,
                                   w.current_owner_participant_id,
                                   w.expected_output, w.updated_at_ms,
                                   p.session_id
                            FROM agent_room_work_items AS w
                            LEFT JOIN agent_room_participants AS p
                              ON p.id = w.current_owner_participant_id
                            WHERE w.room_id = ?
                            ORDER BY w.updated_at_ms DESC, w.id DESC
                            LIMIT ?
                            """,
                            (room_id, _MAX_WORK_ITEMS_PER_ROOM + 1),
                        ).fetchall()
                        if len(work_rows) > _MAX_WORK_ITEMS_PER_ROOM:
                            work_items_truncated = True
                        for work_row in work_rows[:_MAX_WORK_ITEMS_PER_ROOM]:
                            participant_id = str(work_row["current_owner_participant_id"] or "")
                            owner_session_id = str(work_row["session_id"] or "")
                            owner_user_id = bindings.get(owner_session_id, "")
                            owner_user_id = str(owner_user_id or "")
                            owner_requirements_revision = workspace_requirements.get(owner_session_id, 0)
                            items.append(
                                {
                                    "id": str(work_row["id"]),
                                    "roomId": str(work_row["room_id"]),
                                    "objective": _bounded_text(work_row["objective"]),
                                    "state": str(work_row["state"]),
                                    "currentOwnerParticipantId": participant_id,
                                    "ownerSessionId": owner_session_id or None,
                                    "currentOwnerUserId": owner_user_id,
                                    "currentOwnerDisplayName": display_names.get(owner_user_id, ""),
                                    "ownerRequirementsRevision": owner_requirements_revision,
                                    "requirementsStale": bool(owner_session_id)
                                    and owner_requirements_revision != current_requirements_revision,
                                    "expectedOutput": _bounded_text(work_row["expected_output"]),
                                    "updatedAtMs": int(work_row["updated_at_ms"]),
                                }
                            )
                    rooms.append(
                        {
                            "id": room_id,
                            "title": str(room_row["title"]),
                            "status": str(room_row["status"]),
                            "updatedAtMs": int(room_row["updated_at_ms"]),
                            "participantCount": participant_count,
                            "workItems": items,
                        }
                    )
                return rooms, len(room_rows) > _MAX_ROOMS, work_items_truncated
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return [], False, False
            raise TeamError(500, "project_metadata_unavailable", "project metadata is unavailable") from exc
        except (OSError, sqlite3.DatabaseError) as exc:
            raise TeamError(500, "project_metadata_unavailable", "project metadata is unavailable") from exc

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        return row is not None


__all__ = ["TeamProjectContext"]
