from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from rag_ime.agent_execution_policy import (
    FULL_TRUST_EXECUTION_MODE,
    PER_ACTION_EXECUTION_MODE,
    READ_ONLY_EXECUTION_MODE,
    WORKSPACE_MANAGED_EXECUTION_MODE,
    WORKSPACE_SCOPE_CONFIRMATION,
    normalize_execution_mode,
)
from rag_ime.agent_personas import AgentPersonaStore
from rag_ime.agent_roles import PersonaManifest
from rag_ime.rooms.participants import (
    RoomParticipantLifecycleService,
    project_room_participant_policy,
)
from rag_ime.agent_workspace_roots import existing_workspace_roots
from rag_ime.rooms.store import (
    AgentRoomEventHub,
    AgentRoomStore,
    MAX_ACTIVE_ROOM_PARTICIPANTS,
    normalize_collaboration_role,
)
from rag_ime.agent_sessions import (
    AgentSessionNotFound,
    AgentSessionStore,
)
from rag_ime.room_permission_policy import (
    normalize_room_permission_policy,
    resolve_room_permission_policy,
)
from rag_ime.agent_tool_ids import DANGEROUS_MODE_CONFIRMATION


@dataclass(frozen=True)
class RoomParticipantPlan:
    persona: PersonaManifest
    collaboration_role: str


@dataclass(frozen=True)
class RoomCreationPlan:
    room_kind: str
    title: str
    routing_policy: str
    moderator_ordinal: int
    workspace_roots: tuple[str, ...]
    permission_policy: dict[str, object]
    execution_mode: str
    owner_app_id: str
    surface_key: str
    participants: tuple[RoomParticipantPlan, ...]


class RoomLifecycleService:
    """Own Room creation, configuration, archival, and deletion."""

    def __init__(
        self,
        *,
        rooms: AgentRoomStore,
        sessions: AgentSessionStore,
        personas: AgentPersonaStore,
        events: AgentRoomEventHub,
        participants: RoomParticipantLifecycleService,
        create_session: Callable[
            [Mapping[str, object]],
            dict[str, object],
        ],
        delete_session: Callable[
            [str],
            dict[str, object],
        ],
        close_session: Callable[[str], None] | None = None,
    ) -> None:
        self.rooms = rooms
        self.sessions = sessions
        self.personas = personas
        self.events = events
        self.participants = participants
        self.create_session = create_session
        self.delete_session = delete_session
        self.close_session = close_session

    def list_rooms(
        self,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        value = dict(payload or {})
        list_page = (
            self.rooms.list_directory_page
            if _bool(value.get("projectionOnly"))
            else self.rooms.list_page
        )
        page = list_page(
            include_archived=_bool(
                value.get("includeArchived")
            ),
            limit=_integer(
                value.get("limit"),
                default=100,
                minimum=1,
                maximum=200,
            ),
            before_updated_at_ms=_optional_integer(value.get("beforeUpdatedAtMs")),
            before_id=_optional_cursor_id(value.get("beforeId")),
            owner_app_id=(
                str(value["ownerAppId"])
                if "ownerAppId" in value
                else None
            ),
            # Debug/HTTP adapters preserve optional query fields as empty
            # strings.  An omitted surfaceKey means "all surfaces owned by
            # this App", not the literal empty surface.
            surface_key=str(value.get("surfaceKey") or "").strip() or None,
        )
        return {
            "schemaVersion": "rag-ime.agent-room-list.v1",
            "ok": True,
            **page,
        }

    def get_room(self, room_id: str) -> dict[str, object]:
        room = self.rooms.get(room_id)
        self.participants.restore_sessions(room)
        return {
            "schemaVersion": "rag-ime.agent-room-get.v1",
            "ok": True,
            "room": self.rooms.get(room_id),
        }

    def snapshot(self, room_id: str) -> dict[str, object]:
        room = self.rooms.get(room_id)
        self.participants.restore_sessions(room)
        return self.rooms.snapshot(room_id)

    def conversation_snapshot(self, room_id: str) -> dict[str, object]:
        room = self.rooms.get(room_id)
        self.participants.restore_sessions(room)
        return self.rooms.conversation_snapshot(room_id)

    def history(
        self,
        room_id: str,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        value = dict(payload or {})
        return self.rooms.history_page(
            room_id,
            before_sequence=_integer(
                value.get("beforeSequence"),
                default=0,
                minimum=0,
                maximum=2_147_483_647,
            ),
            limit=_integer(
                value.get("limit"),
                default=100,
                minimum=1,
                maximum=200,
            ),
            # The application history endpoint is the recovery path for the
            # append-only Room archive.  The raw store keeps its bounded
            # projection as the explicit default for local callers/tests.
            full_history=True,
        )

    def update(
        self,
        room_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        if not payload:
            raise ValueError(
                "agent room update requires at least one field"
            )
        if "archived" in payload and len(payload) > 1:
            raise ValueError(
                "archive state must be updated separately "
                "from room configuration"
            )
        execution_keys = {
            "executionMode",
            "permissionPolicy",
            "workspaceScopeConfirmation",
            "dangerousModeConfirmation",
        }
        if "workspaceRoots" in payload and not (
            {"executionMode", "permissionPolicy"} & set(payload)
        ):
            return self._update_workspace_roots(
                room_id,
                payload,
            )
        if {"executionMode", "permissionPolicy"} & set(payload):
            execution_payload = {
                key: value
                for key, value in payload.items()
                if key in execution_keys
            }
            config_payload = {
                key: value
                for key, value in payload.items()
                if key not in execution_keys
            }
            return self._update_execution_mode(
                room_id,
                execution_payload,
                config_payload=config_payload,
            )
        if set(payload) == {"archived"}:
            archived = _bool(payload.get("archived"))
            room = self.rooms.archive(
                room_id,
                archived=archived,
            )
            if not archived:
                self.participants.restore_sessions(room)
            event = self.events.publish(
                room_id=room_id,
                event_type="participant_status",
                payload={
                    "status": (
                        "room_archived"
                        if room["status"] == "archived"
                        else "room_restored"
                    )
                },
            )
        else:
            room = self.rooms.update_config(
                room_id,
                payload,
            )
            event = self.events.publish(
                room_id=room_id,
                event_type="room_config_changed",
                payload={
                    "status": "room_config_updated",
                    "changedFields": sorted(payload),
                    "configRevision": room[
                        "configRevision"
                    ],
                    "routingPolicy": room[
                        "routingPolicy"
                    ],
                    "roomKind": room["roomKind"],
                },
                topic_id=str(
                    room.get("activeTopicId") or ""
                ),
            )
        return {
            "schemaVersion": "rag-ime.agent-room-update.v1",
            "ok": True,
            "room": self.rooms.get(room_id),
            "event": event,
        }

    def _update_workspace_roots(
        self,
        room_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        room = self.rooms.get(room_id)
        room_kind = str(room.get("roomKind") or "collaboration")
        current_policy = normalize_room_permission_policy(
            room.get("permissionPolicy"),
            room_kind=room_kind,
            current=room,
            legacy_execution_mode=room.get("executionMode"),
        )
        execution_mode = normalize_execution_mode(
            payload.get("executionMode"),
            default=current_policy["room"]["executionMode"],
        )
        permission_policy = normalize_room_permission_policy(
            {
                **current_policy,
                "room": {"executionMode": execution_mode},
            },
            room_kind=room_kind,
        )
        effective_policy = resolve_room_permission_policy(permission_policy)
        effective_execution_mode = effective_policy["partner"]["executionMode"]
        workspace_roots = _workspace_roots(
            payload,
            room_kind=room_kind,
            permission_policy=permission_policy,
            execution_mode=execution_mode,
        )
        policy = project_room_participant_policy(
            room,
            permission_policy=permission_policy,
            workspace_roots=workspace_roots,
        )
        unrestricted = policy.unrestricted
        if unrestricted:
            workspace_roots = list(policy.workspace_roots)
        if effective_execution_mode == WORKSPACE_MANAGED_EXECUTION_MODE and (
            str(payload.get("workspaceScopeConfirmation") or "")
            != WORKSPACE_SCOPE_CONFIRMATION
        ):
            raise ValueError(
                "workspace-managed Room execution requires an explicit workspace scope confirmation"
            )
        configuration = {
            key: value
            for key, value in payload.items()
            if key
            not in {
                "executionMode",
                "permissionPolicy",
                "workspaceRoots",
                "workspaceScopeConfirmation",
                "dangerousModeConfirmation",
            }
        }
        with self.participants.turn_lock:
            sessions = self.participants.participant_sessions(room)
            active_session_ids = self.participants.active_runtime_session_ids()
            if any(
                self.participants.session_is_busy(
                    str(session["id"]),
                    session,
                    active_session_ids=active_session_ids,
                )
                for session in sessions
            ):
                raise ValueError(
                    "wait for all Room participant turns to finish before changing workspace"
                )
            with self.rooms.write_transaction() as conn:
                for session in sessions:
                    session_policy = project_room_participant_policy(
                        room,
                        mode=str(session.get("mode") or "coordinator"),
                        permission_policy=permission_policy,
                        workspace_roots=workspace_roots,
                    )
                    self.sessions.set_runtime_policy(
                        str(session["id"]),
                        **session_policy.runtime_kwargs(),
                        connection=conn,
                    )
                self.rooms.update_config(
                    room_id,
                    {
                        **configuration,
                        "workspaceRoots": list(workspace_roots),
                    },
                    connection=conn,
                )
        if self.close_session is not None:
            for session in sessions:
                self.close_session(str(session["id"]))
        room = self.rooms.get(room_id)
        event = self.events.publish(
            room_id=room_id,
            event_type="room_config_changed",
            payload={
                "status": "room_workspace_updated",
                "changedFields": sorted(
                    {*configuration, "workspaceRoots", "executionMode"}
                ),
                "executionMode": room["executionMode"],
                "workspaceRootCount": len(workspace_roots),
                "configRevision": room["configRevision"],
            },
            topic_id=str(room.get("activeTopicId") or ""),
        )
        return {
            "schemaVersion": "rag-ime.agent-room-update.v1",
            "ok": True,
            "room": room,
            "event": event,
        }

    def _update_execution_mode(
        self,
        room_id: str,
        payload: Mapping[str, object],
        *,
        config_payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        room = self.rooms.get(room_id)
        room_kind = str(room.get("roomKind") or "collaboration")
        current_policy = normalize_room_permission_policy(
            room.get("permissionPolicy"),
            room_kind=room_kind,
            current=room,
            legacy_execution_mode=room.get("executionMode"),
        )
        if "permissionPolicy" in payload:
            permission_policy = normalize_room_permission_policy(
                payload.get("permissionPolicy"),
                room_kind=room_kind,
                current=room,
                legacy_execution_mode=payload.get("executionMode"),
            )
        else:
            permission_policy = normalize_room_permission_policy(
                {
                    **current_policy,
                    "room": {
                        "executionMode": payload.get(
                            "executionMode"
                        )
                    },
                },
                room_kind=room_kind,
            )
        current_execution_mode = current_policy["room"]["executionMode"]
        execution_mode = permission_policy["room"]["executionMode"]
        effective_execution_mode = resolve_room_permission_policy(
            permission_policy
        )["partner"]["executionMode"]
        raw_roots = [
            str(value)
            for value in room.get("workspaceRoots") or []
            if str(value).strip()
        ]
        requested_roots = raw_roots
        if config_payload is not None and "workspaceRoots" in config_payload:
            requested_raw = config_payload.get("workspaceRoots")
            if not isinstance(requested_raw, list):
                raise ValueError("workspaceRoots must be an array")
            requested_roots = [
                str(value)
                for value in requested_raw
                if str(value).strip()
            ]
        unrestricted_policy = project_room_participant_policy(
            room,
            permission_policy=permission_policy,
            workspace_roots=requested_roots,
        )
        unrestricted = unrestricted_policy.unrestricted
        roots = list(
            _workspace_roots(
                {"workspaceRoots": requested_roots},
                room_kind=room_kind,
                permission_policy=permission_policy,
                execution_mode=execution_mode,
            )
        )
        policy = project_room_participant_policy(
            room,
            permission_policy=permission_policy,
            workspace_roots=roots,
        )
        roots = list(policy.workspace_roots)
        if effective_execution_mode == WORKSPACE_MANAGED_EXECUTION_MODE and (
            not roots
        ):
            raise ValueError(
                "managed and full-trust Room execution require a workspace"
            )
        if effective_execution_mode == WORKSPACE_MANAGED_EXECUTION_MODE and (
            str(payload.get("workspaceScopeConfirmation") or "")
            != WORKSPACE_SCOPE_CONFIRMATION
        ):
            raise ValueError(
                "workspace-managed Room execution requires an explicit workspace scope confirmation"
            )
        if (
            execution_mode == FULL_TRUST_EXECUTION_MODE
            and room_kind != "collaboration"
            and current_execution_mode != FULL_TRUST_EXECUTION_MODE
            and str(payload.get("dangerousModeConfirmation") or "")
            != DANGEROUS_MODE_CONFIRMATION
        ):
            raise ValueError(
                "full-trust Room execution requires an explicit native confirmation"
            )
        configuration = dict(config_payload or {})
        if (
            "workspaceRoots" in configuration
            or unrestricted
            or roots != raw_roots
        ):
            configuration["workspaceRoots"] = roots
        if permission_policy != current_policy:
            configuration["permissionPolicy"] = permission_policy
        configuration_changed = bool(configuration)
        with self.participants.turn_lock:
            sessions = self.participants.participant_sessions(room)
            active_session_ids = self.participants.active_runtime_session_ids()
            if any(
                self.participants.session_is_busy(
                    str(session["id"]),
                    session,
                    active_session_ids=active_session_ids,
                )
                for session in sessions
            ):
                raise ValueError(
                    "wait for all Room participant turns to finish before changing execution mode"
                )
            with self.rooms.write_transaction() as conn:
                for session in sessions:
                    session_policy = project_room_participant_policy(
                        room,
                        mode=str(session.get("mode") or "coordinator"),
                        permission_policy=permission_policy,
                        workspace_roots=roots,
                    )
                    runtime_kwargs = session_policy.runtime_kwargs()
                    if not session_policy.unrestricted:
                        runtime_kwargs["project_context_enabled"] = None
                    self.sessions.set_runtime_policy(
                        str(session["id"]),
                        **runtime_kwargs,
                        connection=conn,
                    )
                if configuration:
                    updated = self.rooms.update_config(
                        room_id,
                        configuration,
                        connection=conn,
                    )
                    if int(updated["configRevision"]) == int(
                        room["configRevision"]
                    ):
                        self.rooms.touch_config(
                            room_id,
                            connection=conn,
                        )
                else:
                    self.rooms.touch_config(
                        room_id,
                        connection=conn,
                    )
        room = self.rooms.get(room_id)
        changed_fields = sorted(
            {*configuration, "executionMode"}
        )
        event = self.events.publish(
            room_id=room_id,
            event_type="room_config_changed",
            payload={
                "status": (
                    "room_config_and_execution_mode_updated"
                    if configuration_changed
                    else "room_execution_mode_updated"
                ),
                "changedFields": changed_fields,
                "executionMode": room["executionMode"],
                "permissionPolicy": room["permissionPolicy"],
                "configRevision": room["configRevision"],
            },
            topic_id=str(room.get("activeTopicId") or ""),
        )
        return {
            "schemaVersion": "rag-ime.agent-room-update.v1",
            "ok": True,
            "room": self.rooms.get(room_id),
            "event": event,
        }

    def create(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        plan = self._creation_plan(payload)
        created_session_ids: list[str] = []
        participants: list[dict[str, object]] = []
        try:
            for participant_plan in plan.participants:
                role = participant_plan.persona
                session = self.create_session(
                    _participant_session_payload(
                        plan,
                        role,
                    )
                )["session"]
                created_session_ids.append(
                    str(session["id"])
                )
                participants.append(
                    {
                        "sessionId": session["id"],
                        "roleId": role.role_id,
                        "roleVersion": role.version,
                        "displayName": role.display_name,
                        "collaborationRole": (
                            participant_plan.collaboration_role
                        ),
                    }
                )
            room = self.rooms.create(
                title=plan.title,
                routing_policy=plan.routing_policy,
                participants=participants,
                workspace_roots=list(
                    plan.workspace_roots
                ),
                moderator_ordinal=(
                    plan.moderator_ordinal
                ),
                room_kind=plan.room_kind,
                permission_policy=plan.permission_policy,
                avatar=str(
                    payload.get("avatar") or "members"
                ),
                description=str(
                    payload.get("description") or ""
                ),
                scenario_prompt=str(
                    payload.get("scenarioPrompt") or ""
                ),
                routing_config=(
                    payload.get("routingConfig")
                    if isinstance(
                        payload.get("routingConfig"),
                        Mapping,
                    )
                    else None
                ),
                owner_app_id=plan.owner_app_id,
                surface_key=plan.surface_key,
            )
        except Exception:
            for session_id in reversed(
                created_session_ids
            ):
                try:
                    self.sessions.delete(session_id)
                except Exception:
                    pass
            raise

        event = self.events.publish(
            room_id=str(room["id"]),
            event_type="participant_status",
            payload={
                "status": "room_created",
                "routingPolicy": plan.routing_policy,
                "roomKind": plan.room_kind,
                "participants": [
                    {
                        "participantId": item["id"],
                        "displayName": item[
                            "displayName"
                        ],
                        "roleId": item["roleId"],
                        "collaborationRole": item[
                            "collaborationRole"
                        ],
                    }
                    for item in room["participants"]
                    if isinstance(item, Mapping)
                ],
            },
        )
        return {
            "schemaVersion": "rag-ime.agent-room-create.v1",
            "ok": True,
            "room": self.rooms.get(str(room["id"])),
            "event": event,
        }

    def delete(
        self,
        room_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        room = self.rooms.get(room_id)
        if str(payload.get("confirmTitle") or "") != str(
            room.get("title") or ""
        ):
            raise ValueError(
                "confirmTitle must exactly match the Room title"
            )
        active_session_ids = (
            self.participants.active_runtime_session_ids()
        )
        with self.participants.turn_lock:
            for session in (
                self.participants.participant_sessions(
                    room
                )
            ):
                session_id = str(
                    session.get("id") or ""
                )
                if self.participants.session_is_busy(
                    session_id,
                    session,
                    active_session_ids=active_session_ids,
                ):
                    raise ValueError(
                        "wait for all Room participant turns "
                        "to finish before deleting it"
                    )
            deleted = self.rooms.delete(room_id)
        cleanup: list[dict[str, object]] = []
        for session_id in deleted["sessionIds"]:
            try:
                cleanup.append(
                    self.delete_session(str(session_id))
                )
            except AgentSessionNotFound:
                cleanup.append(
                    {
                        "schemaVersion": (
                            "rag-ime.agent-session-delete.v1"
                        ),
                        "ok": True,
                        "sessionId": str(session_id),
                        "alreadyMissing": True,
                        "sessionFileDeleted": False,
                        "mediaFilesDeleted": 0,
                    }
                )
        return {
            "schemaVersion": "rag-ime.agent-room-delete.v1",
            "ok": True,
            "roomId": room_id,
            "deletedSessionIds": list(
                deleted["sessionIds"]
            ),
            "sessionCleanup": cleanup,
        }

    def _creation_plan(
        self,
        payload: Mapping[str, object],
    ) -> RoomCreationPlan:
        room_kind = str(
            payload.get("roomKind") or "collaboration"
        ).strip().lower()
        if room_kind not in {
            "collaboration",
            "roleplay",
        }:
            raise ValueError(
                "roomKind must be collaboration or roleplay"
            )
        raw_participants = payload.get("participants")
        if not isinstance(raw_participants, list):
            raise ValueError(
                "room participants must be an array"
            )
        if not 2 <= len(raw_participants) <= MAX_ACTIVE_ROOM_PARTICIPANTS:
            raise ValueError(
                "agent room requires between 2 and 8 "
                "participants"
            )
        permission_policy = normalize_room_permission_policy(
            payload.get("permissionPolicy"),
            room_kind=room_kind,
            legacy_execution_mode=payload.get("executionMode"),
        )
        execution_mode = permission_policy["room"]["executionMode"]
        workspace_roots = _workspace_roots(
            payload,
            room_kind=room_kind,
            permission_policy=permission_policy,
            execution_mode=execution_mode,
        )
        routing_policy = str(
            payload.get("routingPolicy") or "natural"
        )
        owner_app_id, surface_key = _room_surface_identity(
            payload.get("ownerAppId"),
            payload.get("surfaceKey"),
        )
        requested_moderator_role_id = str(
            payload.get("moderatorRoleId") or ""
        ).strip()
        participants = self._participant_plans(
            raw_participants,
            room_kind=room_kind,
            coordinator_role_id=(
                requested_moderator_role_id
                if routing_policy == "moderator"
                else ""
            ),
        )
        roles = tuple(
            item.persona for item in participants
        )
        moderator_ordinal = _moderator_ordinal(
            roles,
            routing_policy=routing_policy,
            requested_role_id=requested_moderator_role_id,
        )
        return RoomCreationPlan(
            room_kind=room_kind,
            title=" ".join(
                str(
                    payload.get("title") or "新群聊"
                ).split()
            )[:120],
            routing_policy=routing_policy,
            moderator_ordinal=moderator_ordinal,
            workspace_roots=workspace_roots,
            permission_policy=permission_policy,
            execution_mode=execution_mode,
            owner_app_id=owner_app_id,
            surface_key=surface_key,
            participants=participants,
        )

    def _participant_plans(
        self,
        raw_participants: list[object],
        *,
        room_kind: str,
        coordinator_role_id: str,
    ) -> tuple[RoomParticipantPlan, ...]:
        participants: list[RoomParticipantPlan] = []
        seen: set[tuple[str, str]] = set()
        required_mode = (
            "coordinator"
            if room_kind == "collaboration"
            else "assistant"
        )
        for index, raw in enumerate(raw_participants):
            if not isinstance(raw, Mapping):
                raise ValueError(
                    "each room participant must be an object"
                )
            role = self.personas.resolve_active(
                raw.get("roleId"),
                raw.get("roleVersion") or "1",
            )
            if required_mode not in role.selectable_modes:
                raise ValueError(
                    f"role {role.role_id}@{role.version} "
                    "cannot join this room kind"
                )
            key = (role.role_id, role.version)
            if key in seen:
                raise ValueError(
                    "room participant roles must be unique "
                    "in the first room version"
                )
            seen.add(key)
            default_role = (
                "coordinator"
                if room_kind == "collaboration"
                and (
                    role.role_id == coordinator_role_id
                    or not coordinator_role_id
                    and index == 0
                )
                else "implementer"
            )
            participants.append(
                RoomParticipantPlan(
                    persona=role,
                    collaboration_role=(
                        normalize_collaboration_role(
                            raw.get("collaborationRole")
                            or default_role
                        )
                    ),
                )
            )
        return tuple(participants)


def _workspace_roots(
    payload: Mapping[str, object],
    *,
    room_kind: str,
    permission_policy: Mapping[str, object] | None = None,
    execution_mode: str = WORKSPACE_MANAGED_EXECUTION_MODE,
) -> tuple[str, ...]:
    raw = payload.get("workspaceRoots")
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ValueError("workspaceRoots must be an array")
    raw_roots = tuple(
        dict.fromkeys(
            str(value or "").strip()
            for value in raw
            if str(value or "").strip()
        )
    )
    context_roots = tuple(
        value for value in raw_roots if value != "/"
    )
    if len(context_roots) > 4:
        raise ValueError(
            "agent room accepts at most four workspace roots"
        )
    normalized_policy = permission_policy
    if normalized_policy is None:
        normalized_policy = normalize_room_permission_policy(
            None,
            room_kind=room_kind,
            legacy_execution_mode=execution_mode,
        )
    policy = project_room_participant_policy(
        {
            "roomKind": room_kind,
            "permissionPolicy": normalized_policy,
            "executionMode": execution_mode,
        },
        permission_policy=normalized_policy,
        workspace_roots=raw_roots,
    )
    effective_execution_mode = resolve_room_permission_policy(
        normalized_policy
    )["partner"]["executionMode"]
    if (
        room_kind == "collaboration"
        and not raw_roots
        and not policy.unrestricted
        and effective_execution_mode != READ_ONLY_EXECUTION_MODE
    ):
        raise ValueError(
            "agent room requires an authorized workspace"
        )
    if policy.unrestricted:
        return policy.workspace_roots
    return existing_workspace_roots(raw_roots)


def _room_surface_identity(
    owner_app_id: object,
    surface_key: object,
) -> tuple[str, str]:
    owner = str(owner_app_id or "").strip()
    key = str(surface_key or "").strip()
    if owner and not owner.startswith("extension:"):
        raise ValueError("room ownerAppId must be an extension App id")
    if owner and not key:
        raise ValueError("room surfaceKey is required for an App-owned Room")
    if key and not owner:
        raise ValueError("room surfaceKey requires ownerAppId")
    if len(owner) > 80 or len(key) > 64 or any(
        char.isspace() for char in owner + key
    ):
        raise ValueError("room App ownership identity is invalid")
    return owner, key


def _moderator_ordinal(
    roles: tuple[PersonaManifest, ...],
    *,
    routing_policy: str,
    requested_role_id: str,
) -> int:
    if routing_policy != "moderator":
        return 0
    moderator_role_id = requested_role_id or next(
        (
            role.role_id
            for role in roles
            if role.role_id == "companion-future-v1"
        ),
        roles[0].role_id,
    )
    matches = [
        index
        for index, role in enumerate(roles)
        if role.role_id == moderator_role_id
    ]
    if len(matches) != 1:
        raise ValueError(
            "moderatorRoleId must identify one room "
            "participant"
        )
    return matches[0]


def _participant_session_payload(
    plan: RoomCreationPlan,
    role: PersonaManifest,
) -> dict[str, object]:
    policy = project_room_participant_policy(
        {
            "roomKind": plan.room_kind,
            "permissionPolicy": plan.permission_policy,
            "executionMode": plan.execution_mode,
            "workspaceRoots": plan.workspace_roots,
        },
        permission_policy=plan.permission_policy,
        workspace_roots=plan.workspace_roots,
    )
    return {
        "title": f"{plan.title} · {role.display_name}",
        "_modelRoute": "roomCoordinator",
        "roleId": role.role_id,
        "roleVersion": role.version,
        **policy.create_fields(),
        **({
            "surfaceKind": "extension_app",
            "ownerAppId": plan.owner_app_id,
            "surfaceKey": plan.surface_key,
        } if plan.owner_app_id else {}),
    }


def _bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _integer(
    value: object,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _optional_integer(value: object) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, min(9_223_372_036_854_775_807, parsed))


def _optional_cursor_id(value: object) -> str | None:
    normalized = " ".join(str(value or "").split())[:200]
    return normalized or None
