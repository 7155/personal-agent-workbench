"""Team-only resource checks for the legacy Agent Control API.

The Control API routes are shared with the personal installation and therefore
cannot infer a human actor from a Room moderator, a request body, or a
workspace path.  This module is the small team boundary that resolves those
references against the authenticated user and the space-scoped service before
the existing handlers mutate state.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..db import sqlite_connection
from .errors import TeamError


_ROOM_CREATE_PATH = "agent.rooms.create"

# These are all controls which can change where a Room executes or what it can
# reach.  The route table currently exposes the camelCase names; the snake_case
# aliases are retained here because internal callers may use the same function
# before translating a payload into Control API JSON.
_EXECUTION_BOUNDARY_FIELDS = frozenset(
    {
        "workspaceRoots",
        "workspace_roots",
        "permissionPolicy",
        "permission_policy",
        "executionMode",
        "execution_mode",
        "workspaceScopeConfirmation",
        "workspace_scope_confirmation",
        "dangerousModeConfirmation",
        "dangerous_mode_confirmation",
        "toolProfileVersion",
        "tool_profile_version",
        "allowedTools",
        "allowed_tools",
        "codexSkillsEnabled",
        "codex_skills_enabled",
        "piSkillsEnabled",
        "pi_skills_enabled",
        "grantWorkspaceScope",
        "grant_workspace_scope",
        "workspaceReadOnly",
        "workspace_read_only",
    }
)

_WORK_ITEM_CREATE_PATH = "agent.room.workItem.create"
_WORK_ITEM_REASSIGN_PATH = "agent.room.workItem.reassign"
_WORK_ITEM_RESUME_PATH = "agent.room.workItem.resume"
_ARTIFACT_ADD_PATH = "agent.room.artifact.add"
_PARTICIPANT_REMOVE_PATH = "agent.room.participant.remove"
_PARTICIPANT_UPDATE_PATH = "agent.room.participant.update"
_PARTICIPANT_STEER_PATH = "agent.room.participant.steer"
_ROOM_MESSAGE_PATH = "agent.room.message"
_ROOM_WORK_ITEMS_LIST_PATH = "agent.room.workItems.list"

_SUBAGENT_LIST_PATH = "agent.subagents.list"
_SUBAGENT_CREATE_PATH = "agent.subagents.create"
_SUBAGENT_GET_PATH = "agent.subagent.get"
_SUBAGENT_CONSOLE_PATH = "agent.subagent.console"
_SUBAGENT_CONTROL_PATH = "agent.subagent.control"
_SUBAGENT_ABORT_PATH = "agent.subagent.abort"
_SUBAGENT_RUN_PATHS = frozenset(
    {
        _SUBAGENT_GET_PATH,
        _SUBAGENT_CONSOLE_PATH,
        _SUBAGENT_CONTROL_PATH,
        _SUBAGENT_ABORT_PATH,
    }
)

_NOT_FOUND_MESSAGE = "Team resource not found"
_FORBIDDEN_MESSAGE = "The requested team resource is not authorized"


def authorize_resources(
    team: Any,
    service: Any,
    request: Any,
    body: Mapping[str, object],
) -> dict[str, object]:
    """Authorize resource references in one already route-validated request.

    ``TeamAccessPolicy`` authenticates the user and selects the allowed route.
    This second step verifies the resource IDs and canonicalizes human actor
    fields immediately before the legacy handler receives them.  It deliberately
    returns a fresh dictionary so a caller cannot mutate the immutable
    ``ControlRequest`` body or accidentally retain a caller-owned mapping.
    """

    if not isinstance(body, Mapping):
        raise TeamError(400, "invalid_request", "Request body must be an object")
    actor = _required_identifier(getattr(request, "actor_user_id", ""), "actor")
    space = getattr(request, "space", None)
    if not isinstance(space, Mapping):
        raise TeamError(404, "resource_not_found", _NOT_FOUND_MESSAGE)
    space_id = _required_identifier(space.get("id"), "space")
    service_space_id = str(getattr(service, "space_id", "") or "").strip()
    if service_space_id and service_space_id != space_id:
        raise _not_found()
    route = getattr(getattr(request, "request", None), "path_id", "")
    path = str(route or "").strip()
    result = dict(body)

    # Room creation is server-owned by TeamSpaceService.  Let the parent force
    # its managed checkout after this function returns; later Room mutations
    # never get a chance to replace those controls.
    if path != _ROOM_CREATE_PATH and path.startswith("agent.room."):
        _reject_execution_boundary(result)

    if path.startswith("agent.room."):
        room_id = _request_value(request, "roomId", from_params=True)
        if not room_id:
            raise _not_found()
        room = _room(service, room_id)
        _authorize_room_request(
            team,
            service,
            request,
            result,
            room,
            actor=actor,
            space_id=space_id,
            space=space,
        )

    if path in {_SUBAGENT_LIST_PATH, _SUBAGENT_CREATE_PATH}:
        # TeamAccessPolicy already checks this value, but retain the check here
        # because this function is also useful to internal service adapters.
        session_id = _request_value(request, "sessionId", result=result)
        if not session_id:
            raise TeamError(404, "resource_not_found", _NOT_FOUND_MESSAGE)
        _require_session(team, actor, space_id, session_id, action="write" if path == _SUBAGENT_CREATE_PATH else "read")
    elif path in _SUBAGENT_RUN_PATHS:
        _authorize_subagent_run(
            team,
            service,
            request,
            result,
            actor=actor,
            space_id=space_id,
            path=path,
        )

    return result


def _authorize_room_request(
    team: Any,
    service: Any,
    request: Any,
    body: dict[str, object],
    room: Mapping[str, object],
    *,
    actor: str,
    space_id: str,
    space: Mapping[str, object],
) -> None:
    path = str(getattr(getattr(request, "request", None), "path_id", "") or "")
    participants = _active_participants(room)

    # participantId is a target for participant steer/update/remove, but the
    # same JSON field means "created by" for artifacts.  Only the latter is a
    # human provenance field and may be filled from the authenticated actor.
    actor_fields: tuple[str, ...] = ()
    if path == _ARTIFACT_ADD_PATH:
        actor_fields = ("participantId",)
    elif path == _WORK_ITEM_CREATE_PATH:
        actor_fields = ("createdByParticipantId",)
    elif path in {_WORK_ITEM_REASSIGN_PATH, _WORK_ITEM_RESUME_PATH}:
        actor_fields = ("actorParticipantId",)
    elif path == _PARTICIPANT_REMOVE_PATH:
        # RoomParticipantLifecycleService historically selected the first
        # coordinator when this provenance was omitted.  Inject the
        # authenticated participant so removal/released-work audit cannot be
        # attributed to another user's facilitator.
        actor_fields = ("actorParticipantId",)

    canonical_actor: Mapping[str, object] | None = None
    if actor_fields:
        canonical_actor = _resolve_actor_participant(
            team,
            participants,
            actor=actor,
            space_id=space_id,
            prefer_unique_coordinator=path == _PARTICIPANT_REMOVE_PATH,
            explicit_ids=[
                str(body[field]).strip()
                for field in actor_fields
                if field in body and str(body[field] or "").strip()
            ],
        )
        canonical_id = str(canonical_actor["id"])
        for field in actor_fields:
            supplied = str(body.get(field) or "").strip()
            if supplied and supplied != canonical_id:
                raise _forbidden("A Room actor must belong to the authenticated user")
            # Downstream Room handlers require this provenance.  A unique
            # authenticated participant is safe to derive; the facilitator is
            # never selected as a fallback.
            body[field] = canonical_id

    if path == _PARTICIPANT_REMOVE_PATH or path == _PARTICIPANT_UPDATE_PATH:
        _participant_from_body(
            participants,
            body,
            "participantId",
            required=True,
        )
    elif path == "agent.room.archive":
        moderator_id = str(body.get("moderatorParticipantId") or "").strip()
        if moderator_id:
            _participant_by_id(participants, moderator_id)
    elif path == _PARTICIPANT_STEER_PATH:
        # Steering may omit the target when the Room turn has exactly one
        # routed participant.  A contributor cannot rely on that implicit
        # route because it may be another user's Session; bind the omitted
        # target to the authenticated user's own participant instead.  The
        # downstream turn receipt remains responsible for proving that the
        # target was routed for this turn.
        target = _participant_from_body(
            participants,
            body,
            "participantId",
            required=False,
        )
        if str(space.get("role") or "") not in {"owner", "maintainer"}:
            canonical_actor = _resolve_actor_participant(
                team,
                participants,
                actor=actor,
                space_id=space_id,
                explicit_ids=(),
            )
            canonical_id = str(canonical_actor["id"])
            if target is not None and str(target.get("id") or "") != canonical_id:
                raise _forbidden("Contributors may steer only their own Room participant")
            body["participantId"] = canonical_id
    elif path == _ROOM_MESSAGE_PATH:
        _validate_participant_list(participants, body.get("participantIds"))
    elif path == _ROOM_WORK_ITEMS_LIST_PATH:
        owner_id = _request_value(request, "ownerParticipantId")
        if owner_id:
            _participant_by_id(participants, owner_id)

    if path == _WORK_ITEM_CREATE_PATH:
        current_owner = _require_participant(
            _participant_from_body(
                participants,
                body,
                "currentOwnerParticipantId",
                required=True,
            ),
            "currentOwnerParticipantId",
        )
        accountable = _participant_from_body(participants, body, "accountableParticipantId", required=False)
        _authorize_assignment_targets(
            space,
            canonical_actor,
            current_owner,
            accountable,
        )
        _require_current_assignment_target(team, space_id, current_owner)
        if accountable is not None:
            _require_current_assignment_target(team, space_id, accountable)
    elif path == _WORK_ITEM_REASSIGN_PATH:
        target = _require_participant(
            _participant_from_body(
                participants,
                body,
                "targetParticipantId",
                required=True,
            ),
            "targetParticipantId",
        )
        _authorize_assignment_targets(space, canonical_actor, target, None)
        _require_current_assignment_target(team, space_id, target)
    elif path == _WORK_ITEM_RESUME_PATH:
        # Resume has no target assignment, but its actor field was checked above.
        return
    elif path == _ARTIFACT_ADD_PATH:
        # The file path remains governed by the existing TeamFiles/Room
        # workspace checks.  Here we only bind its creator provenance.
        return


def _reject_execution_boundary(body: Mapping[str, object]) -> None:
    fields = sorted(str(key) for key in set(body) & _EXECUTION_BOUNDARY_FIELDS)
    if fields:
        raise TeamError(
            403,
            "team_scope_required",
            "Team Room execution policy is assigned by the server",
        )


def _resolve_actor_participant(
    team: Any,
    participants: Sequence[Mapping[str, object]],
    *,
    actor: str,
    space_id: str,
    explicit_ids: Sequence[str],
    prefer_unique_coordinator: bool = False,
) -> Mapping[str, object]:
    explicit = {value for value in explicit_ids if value}
    if len(explicit) > 1:
        raise _forbidden("A request cannot name multiple Room actors")

    owned: list[Mapping[str, object]] = []
    for participant in participants:
        session_id = str(participant.get("sessionId") or "").strip()
        if not session_id:
            continue
        try:
            binding = _binding(team, session_id)
        except TeamError:
            # A removed/expired Session is not a current Room participant for
            # this request.  Continue so the final result is a safe 404.
            continue
        if (
            str(binding.get("spaceId") or "") == space_id
            and str(binding.get("ownerUserId") or "") == actor
            and bool(binding.get("active", True))
        ):
            owned.append(participant)

    if explicit:
        participant = _participant_by_id(participants, next(iter(explicit)))
        if participant not in owned:
            raise _forbidden("Room actor is not owned by the authenticated user")
        return participant
    if len(owned) == 1:
        return owned[0]
    if prefer_unique_coordinator:
        # The legacy participant.remove contract has no actorParticipantId.
        # A Room creator may own several participants, so retain a bounded
        # default only when exactly one of the authenticated user's current
        # participants is the coordinator.  Never borrow another owner's
        # facilitator and never guess when coordinator ownership is ambiguous.
        coordinators = [
            participant
            for participant in owned
            if str(participant.get("collaborationRole") or "").strip() == "coordinator"
        ]
        if len(coordinators) == 1:
            return coordinators[0]
    # Zero means this user is not a current participant.  More than one means
    # there is no unambiguous actor; neither case may borrow the moderator.
    if not owned:
        raise _not_found()
    raise TeamError(
        409,
        "participant_selection_required",
        "Select the Room participant acting for this request",
    )


def _require_current_assignment_target(team: Any, space_id: str, participant: Mapping[str, object]) -> None:
    binding = team.grants.binding(str(participant.get('sessionId') or ''), check_current=True)
    if binding['spaceId'] != space_id:
        raise _not_found()


def _authorize_assignment_targets(
    space: Mapping[str, object],
    actor_participant: Mapping[str, object] | None,
    primary: Mapping[str, object],
    secondary: Mapping[str, object] | None,
) -> None:
    role = str(space.get("role") or "")
    if role in {"owner", "maintainer"}:
        return
    actor_id = str((actor_participant or {}).get("id") or "")
    if not actor_id or str(primary.get("id") or "") != actor_id:
        raise _forbidden("Contributors may assign work only to themselves")
    if secondary is not None and str(secondary.get("id") or "") != actor_id:
        raise _forbidden("Contributors may not make another participant accountable")


def _authorize_subagent_run(
    team: Any,
    service: Any,
    request: Any,
    body: Mapping[str, object],
    *,
    actor: str,
    space_id: str,
    path: str,
) -> None:
    run_id = _request_value(request, "runId", from_params=True)
    session_id = _request_value(request, "sessionId", result=dict(body))
    if not run_id or not session_id:
        raise _not_found()

    target = _delegation_target(service, run_id, allow_batch=path == _SUBAGENT_ABORT_PATH)
    parent_session_id = str(target.get("parentSessionId") or "").strip()
    if not parent_session_id or session_id != parent_session_id:
        # Do not reveal whether the run exists for a different member.  This is
        # also the guard against a forged sessionId being combined with a real
        # runId.
        raise _not_found()
    action = "write" if path in {_SUBAGENT_CONTROL_PATH, _SUBAGENT_ABORT_PATH} else "read"
    _require_session(team, actor, space_id, parent_session_id, action=action)


def _delegation_target(service: Any, run_id: str, *, allow_batch: bool) -> dict[str, object]:
    agent = _agent_service(service)
    delegation = getattr(agent, "delegation", None)
    store = getattr(delegation, "store", None)
    if store is not None:
        try:
            run = _store_get_run(store, run_id)
        except (KeyError, LookupError, ValueError):
            if not allow_batch:
                raise _not_found()
            try:
                batch = _store_get_batch(store, run_id)
            except (KeyError, LookupError, ValueError):
                raise _not_found()
            if not isinstance(batch, Mapping):
                raise _not_found()
            parent = str(batch.get("parentSessionId") or "").strip()
            if not parent:
                raise _not_found()
            return {"batchId": run_id, "parentSessionId": parent}
        if not isinstance(run, Mapping):
            raise _not_found()
        direct_parent = str(run.get("parentSessionId") or "").strip()
        if direct_parent:
            return {"runId": run_id, "parentSessionId": direct_parent}
        batch_id = str(run.get("batchId") or "").strip()
        if not batch_id:
            raise _not_found()
        try:
            batch = _store_get_batch(store, batch_id)
        except (KeyError, LookupError, ValueError):
            raise _not_found()
        if not isinstance(batch, Mapping):
            raise _not_found()
        parent = str(batch.get("parentSessionId") or "").strip()
        if not parent:
            raise _not_found()
        return {"runId": run_id, "batchId": batch_id, "parentSessionId": parent}

    # A minimal/read-only fallback keeps this boundary usable while an
    # embedding service is still constructing its coordinator.  It is scoped to
    # the Agent service DB, never the global personal database.
    db_path = getattr(agent, "db_path", None)
    if db_path is None:
        config = getattr(service, "config", None)
        db_path = getattr(config, "db_path", None)
    if db_path is None:
        raise _not_found()
    try:
        with sqlite_connection(Path(str(db_path)), row_factory=sqlite3.Row) as conn:
            row = conn.execute(
                """
                SELECT r.id AS run_id, r.batch_id AS batch_id,
                       b.parent_session_id AS parent_session_id
                FROM agent_subagent_runs AS r
                JOIN agent_subagent_batches AS b ON b.id = r.batch_id
                WHERE r.id = ?
                """,
                (run_id,),
            ).fetchone()
            if row is None and allow_batch:
                row = conn.execute(
                    "SELECT id AS batch_id, parent_session_id FROM agent_subagent_batches WHERE id = ?",
                    (run_id,),
                ).fetchone()
    except (sqlite3.Error, OSError):
        raise _not_found()
    if row is None or not str(row["parent_session_id"] or "").strip():
        raise _not_found()
    return {
        "runId": str(row["run_id"]) if "run_id" in row.keys() else "",
        "batchId": str(row["batch_id"]),
        "parentSessionId": str(row["parent_session_id"]),
    }


def _store_get_run(store: Any, run_id: str) -> object:
    getter = getattr(store, "get_run", None)
    if not callable(getter):
        raise _not_found()
    try:
        return getter(run_id, hydrate_artifacts=False)
    except TypeError:
        return getter(run_id)


def _store_get_batch(store: Any, batch_id: str) -> object:
    getter = getattr(store, "get_batch", None)
    if not callable(getter):
        raise _not_found()
    try:
        return getter(batch_id, hydrate_artifacts=False)
    except TypeError:
        return getter(batch_id)


def _room(service: Any, room_id: str) -> Mapping[str, object]:
    rooms = getattr(_agent_service(service), "rooms", None)
    getter = getattr(rooms, "get", None)
    if not callable(getter):
        raise _not_found()
    try:
        room = getter(room_id)
    except (KeyError, LookupError, ValueError):
        raise _not_found()
    if not isinstance(room, Mapping):
        raise _not_found()
    return room


def _agent_service(service: Any) -> Any:
    """Accept either TeamSpaceService or its embedded AgentService."""

    candidate = getattr(service, "agent", None)
    return candidate if candidate is not None else service


def _active_participants(room: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw = room.get("participants")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise _not_found()
    result: list[Mapping[str, object]] = []
    seen: set[str] = set()
    for value in raw:
        if not isinstance(value, Mapping):
            raise _not_found()
        participant_id = str(value.get("id") or "").strip()
        if not participant_id or participant_id in seen:
            raise _not_found()
        seen.add(participant_id)
        if str(value.get("status") or "") == "active":
            result.append(value)
    return result


def _participant_by_id(
    participants: Sequence[Mapping[str, object]],
    participant_id: str,
) -> Mapping[str, object]:
    matches = [
        participant
        for participant in participants
        if str(participant.get("id") or "") == participant_id
    ]
    if len(matches) != 1:
        raise _not_found()
    return matches[0]


def _participant_from_body(
    participants: Sequence[Mapping[str, object]],
    body: Mapping[str, object],
    field: str,
    *,
    required: bool,
) -> Mapping[str, object] | None:
    value = str(body.get(field) or "").strip()
    if not value:
        if required:
            raise TeamError(400, "invalid_request", f"{field} must not be empty")
        return None
    return _participant_by_id(participants, value)


def _validate_participant_list(
    participants: Sequence[Mapping[str, object]],
    value: object,
) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        raise TeamError(400, "invalid_request", "participantIds must be an array")
    for item in value:
        participant_id = str(item or "").strip()
        if not participant_id:
            raise TeamError(404, "resource_not_found", _NOT_FOUND_MESSAGE)
        _participant_by_id(participants, participant_id)


def _require_participant(
    participant: Mapping[str, object] | None,
    field: str,
) -> Mapping[str, object]:
    if participant is None:
        raise TeamError(400, "invalid_request", f"{field} must not be empty")
    return participant


def _binding(team: Any, session_id: str) -> Mapping[str, object]:
    grants = getattr(team, "grants", None)
    getter = getattr(grants, "binding", None)
    if not callable(getter):
        raise _not_found()
    try:
        binding = getter(session_id, check_current=True)
    except TeamError:
        raise
    except (KeyError, LookupError, ValueError):
        raise _not_found()
    if not isinstance(binding, Mapping):
        raise _not_found()
    return binding


def _require_session(
    team: Any,
    actor: str,
    space_id: str,
    session_id: str,
    *,
    action: str,
) -> Mapping[str, object]:
    grants = getattr(team, "grants", None)
    checker = getattr(grants, "require_session", None)
    if not callable(checker):
        raise _not_found()
    try:
        result = checker(actor, space_id, session_id, action=action)
    except TeamError:
        raise
    except (KeyError, LookupError, ValueError):
        raise _not_found()
    if not isinstance(result, Mapping):
        raise _not_found()
    return result


def _request_value(
    request: Any,
    key: str,
    *,
    result: Mapping[str, object] | None = None,
    from_params: bool = False,
) -> str:
    control = getattr(request, "request", None)
    params = getattr(control, "params", {})
    query = getattr(control, "query", {})
    value: object = ""
    if from_params and isinstance(params, Mapping):
        value = params.get(key, "")
    if not value and isinstance(query, Mapping):
        value = query.get(key, "")
    if not value and result is not None:
        value = result.get(key, "")
    return str(value or "").strip()


def _required_identifier(value: object, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise TeamError(404, "resource_not_found", _NOT_FOUND_MESSAGE)
    return normalized


def _not_found() -> TeamError:
    return TeamError(404, "resource_not_found", _NOT_FOUND_MESSAGE)


def _forbidden(message: str = _FORBIDDEN_MESSAGE) -> TeamError:
    return TeamError(403, "team_resource_forbidden", message)


__all__ = ["authorize_resources"]
