from __future__ import annotations

from .agent_runtime_driver import RuntimeSessionLifecycle
from .agent_sessions import AgentSessionStore
from .agent_runtime_driver import RuntimeDriverFactory
from .agent_configuration import AgentConfigurationStore
from rag_ime.rooms.store import AgentRoomStore
from .agent_delegation import AgentDelegationCoordinator
from .agent_media import AgentMediaStore
from .agent_events import AgentEventHub

import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path

from .agent_execution_policy import (
    FULL_TRUST_EXECUTION_MODE,
    PER_ACTION_EXECUTION_MODE,
    WORKSPACE_MANAGED_EXECUTION_MODE,
    WORKSPACE_SCOPE_CONFIRMATION,
    canonical_tool_profile,
    normalize_execution_mode,
)
from .agent_role_identity import canonical_agent_role_id
from .agent_tool_ids import (
    CONTROL_CENTER_TOOL_PROFILE,
    DANGEROUS_AUTO_APPROVE_TOOL_PROFILE,
    DANGEROUS_MODE_CONFIRMATION,
    FULL_ACCESS_TOOL_PROFILE,
    READONLY_TOOL_PROFILE,
)
from .agent_workspace_roots import (
    exact_trace_project_workspace_roots,
    existing_workspace_roots,
    is_trace_project_bound_surface,
    system_wide_workspace_roots,
)
from .agent_memory_context_support import compaction_summary


class AgentSessionApplicationService:
    """Own Session lifecycle, runtime policy, and model selection."""

    def __init__(
        self,
        *,
        sessions: AgentSessionStore,
        runtime_provider: Callable[[], RuntimeSessionLifecycle],
        runtime_factory: RuntimeDriverFactory,
        configuration_store: AgentConfigurationStore,
        rooms: AgentRoomStore,
        delegation: AgentDelegationCoordinator,
        media: AgentMediaStore,
        events: AgentEventHub,
        runtime_status: Callable[[], Mapping[str, object]],
        pending_memory_bootstrap: Callable[[Mapping[str, object]], Mapping[str, object]],
        probe_memory_maintenance: Callable[..., Mapping[str, object]],
        cancel_pending_approvals: Callable[..., Mapping[str, object]],
        recent_recall_messages: Callable[[str], list[dict[str, object]]],
        checkpoint_runtime_compaction: Callable[
            [str, Mapping[str, object], str], Mapping[str, object]
        ],
        refresh_session_context: Callable[[Mapping[str, object]], Mapping[str, object]],
        public_error: Callable[[BaseException], str],
    ) -> None:
        self.sessions = sessions
        self._runtime_provider = runtime_provider
        self.runtime_factory = runtime_factory
        self.configuration_store = configuration_store
        self.rooms = rooms
        self.delegation = delegation
        self.media = media
        self.events = events
        self.runtime_status = runtime_status
        self.pending_memory_bootstrap = pending_memory_bootstrap
        self.probe_memory_maintenance = probe_memory_maintenance
        self.cancel_pending_approvals = cancel_pending_approvals
        self.recent_recall_messages = recent_recall_messages
        self.checkpoint_runtime_compaction = checkpoint_runtime_compaction
        self.refresh_session_context = refresh_session_context
        self.public_error = public_error

    @property
    def runtime(self) -> RuntimeSessionLifecycle:
        return self._runtime_provider()

    def ensure_runtime(self, payload: Mapping[str, object]) -> dict[str, object]:
        session_id = _required_text(payload, "sessionId")
        result = self.runtime.ensure(session_id)
        if self.sessions.get(session_id).get("runtimeEngine") == "durable":
            return {"schemaVersion": "rag-ime.agent-runtime-ensure.v1", "ok": True,
                "runtime": dict(self.runtime_status()), "memoryMaintenance": {"status": "not_applicable"}, **result}
        maintenance = self.probe_memory_maintenance(
            session_id,
            trigger="session_switch",
        )
        return {
            "schemaVersion": "rag-ime.agent-runtime-ensure.v1",
            "ok": True,
            "runtime": dict(self.runtime_status()),
            "memoryMaintenance": maintenance,
            **result,
        }

    def resume_session(self, session_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        if self.sessions.get(session_id).get("runtimeEngine") != "durable":
            raise ValueError("Session resume requires a Durable Session")
        turn_id = _required_text(payload, "turnId")
        client_message_id = _required_text(payload, "clientMessageId")
        resume = getattr(self.runtime, "resume_session", None)
        if not callable(resume):
            raise ValueError("Durable Session resume is unavailable")
        receipt = resume(session_id, turn_id=turn_id, client_message_id=client_message_id)
        return {"schemaVersion": "rag-ime.agent-session-resume.v1", "ok": True,
                "sessionId": session_id, "turnId": turn_id, "clientMessageId": client_message_id,
                "runtimeReceipt": dict(receipt)}

    def list_sessions(
        self,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        value = dict(payload or {})
        page = self.sessions.list_page(
            include_archived=_bool(value.get("includeArchived")),
            include_internal=_bool(value.get("includeInternal")),
            limit=_integer(
                value.get("limit"),
                default=100,
                minimum=1,
                maximum=500,
            ),
            before_updated_at_ms=_optional_integer(value.get("beforeUpdatedAtMs")),
            before_id=_optional_cursor_id(value.get("beforeId")),
            surface_kind=str(value.get("surfaceKind") or "agent"),
            owner_app_id=str(value.get("ownerAppId") or ""),
            surface_key=str(value.get("surfaceKey") or ""),
            projection_only=_bool(value.get("projectionOnly")),
        )
        sessions = page["items"]
        if not isinstance(sessions, list):
            raise TypeError("agent session list page items must be a list")
        room_participants = self.rooms.participants_for_sessions(
            [
                str(session.get("id") or "")
                for session in sessions
            ],
            active_only=False,
        )
        projected_sessions: list[dict[str, object]] = []
        for session in sessions:
            projected = dict(session)
            participant = room_participants.get(str(session.get("id") or ""))
            if participant is not None:
                projected["roomParticipant"] = {
                    "roomId": str(participant["roomId"]),
                    "participantId": str(participant["id"]),
                    "status": str(participant["status"]),
                }
            projected_sessions.append(projected)
        return {
            "schemaVersion": "rag-ime.agent-session-list.v1",
            "ok": True,
            "items": projected_sessions,
            "activeSessionId": self.runtime_status().get("activeSessionId"),
            "hasMore": page["hasMore"],
            "nextBeforeUpdatedAtMs": page["nextBeforeUpdatedAtMs"],
            "nextBeforeId": page["nextBeforeId"],
            "nextCursor": page["nextCursor"],
        }

    def create_session(self, payload: Mapping[str, object]) -> dict[str, object]:
        session = self._create_session_record(payload)
        return {
            "schemaVersion": "rag-ime.agent-session-create.v1",
            "ok": True,
            "session": session,
            "roleBook": {
                "ok": True,
                "status": "persona_package_not_installed",
                "revisionId": "",
            },
            "memoryBootstrap": self.pending_memory_bootstrap(session),
        }

    def abort(self, session_id: str) -> dict[str, object]:
        """Stop one Session and settle its pending approval state."""

        runtime_receipt: Mapping[str, object] = {}
        runtime = self.runtime
        fenced_abort = getattr(runtime, "abort_with_approval_fence", None)
        if callable(fenced_abort):
            approval_cancellation: dict[str, object] = {}
            def before_abort(identity: Mapping[str, object]) -> None:
                nonlocal approval_cancellation
                approval_cancellation = dict(self.cancel_pending_approvals(
                    session_id,
                    reason="user_abort",
                    turn_id=str(identity.get("turnId") or ""),
                    client_message_id=(str(identity.get("clientMessageId") or "")
                        if identity.get("pendingAdmission") is True else None),
                ))
            raw_runtime_receipt = fenced_abort(session_id, before_abort)
        else:
            # An older driver has no pre-RPC turn contract. Capture the exact
            # existing IDs instead of turning an empty ACK into a future scan.
            captured_ids = self.sessions.pending_approval_ids(session_id)
            approval_cancellation = self.cancel_pending_approvals(
                session_id,
                reason="user_abort",
                approval_ids=captured_ids,
            )
            raw_runtime_receipt = runtime.abort(session_id)
            if isinstance(raw_runtime_receipt, Mapping) and raw_runtime_receipt.get("turnId"):
                self.cancel_pending_approvals(
                    session_id, reason="user_abort",
                    turn_id=str(raw_runtime_receipt["turnId"]), approval_ids=captured_ids,
                )
                approval_cancellation = {**dict(approval_cancellation), "turnId": str(raw_runtime_receipt["turnId"])}
        if isinstance(raw_runtime_receipt, Mapping):
            runtime_receipt = raw_runtime_receipt
        return {
            "schemaVersion": "rag-ime.agent-abort.v1",
            "ok": True,
            "sessionId": session_id,
            "runtimeReceipt": dict(runtime_receipt),
            "approvalCancellation": approval_cancellation,
        }

    def compact(
        self,
        session_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        """Compact a Session and reconcile its bounded context afterwards."""

        self.recent_recall_messages(session_id)
        result = dict(
            self.runtime.compact(session_id, str(payload.get("instructions") or ""))
        )
        if not isinstance(result.get("memoryCheckpoint"), Mapping):
            result["memoryCheckpoint"] = dict(
                self.checkpoint_runtime_compaction(session_id, result, "manual")
            )
        maintenance = self.probe_memory_maintenance(session_id, trigger="compaction")
        if result.get("contextRefreshApplied") is True:
            context_refresh: Mapping[str, object] = {
                "schemaVersion": "rag-ime.agent-session-context-refresh.v1",
                "ok": True,
                "result": {
                    "sessionId": session_id,
                    "trigger": "compaction",
                    "status": "runtime_applied",
                },
            }
        else:
            try:
                context_refresh = self.refresh_session_context(
                    {
                        "sessionId": session_id,
                        "trigger": "compaction",
                        "summary": compaction_summary(result),
                        "compactionEntryId": result.get("compactionEntryId"),
                        "expectedContextEpoch": result.get("contextEpochBefore"),
                        "recentMessages": self.recent_recall_messages(session_id),
                    }
                )
            except Exception as exc:
                context_refresh = {
                    "schemaVersion": "rag-ime.agent-session-context-refresh.v1",
                    "ok": False,
                    "error": self.public_error(exc),
                }
        return {
            "schemaVersion": "rag-ime.agent-compact.v1",
            "ok": True,
            "sessionId": session_id,
            "result": result,
            "memoryMaintenance": maintenance,
            "contextRefresh": dict(context_refresh),
        }

    def ensure_surface_session(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        surface_kind = str(payload.get("surfaceKind") or "").strip()
        owner_app_id = str(payload.get("ownerAppId") or "").strip()
        surface_key = str(payload.get("surfaceKey") or "").strip()
        created, session = self.sessions.ensure_surface_session(
            surface_kind=surface_kind,
            owner_app_id=owner_app_id,
            surface_key=surface_key,
            create=lambda conn: self._create_session_record(
                payload,
                connection=conn,
            ),
        )
        return {
            "schemaVersion": "rag-ime.agent-surface-session-ensure.v1",
            "ok": True,
            "created": created,
            "session": session,
        }

    def create_in_transaction(
        self, payload: Mapping[str, object], connection: sqlite3.Connection,
    ) -> dict[str, object]:
        """Apply normal Session creation policy inside an owning App transaction."""
        if not connection.in_transaction:
            raise ValueError("App Session creation requires an active transaction")
        return self._create_session_record(payload, connection=connection)

    def _create_session_record(
        self,
        payload: Mapping[str, object],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, object]:
        runtime_engine = payload.get("runtimeEngine", "classic")
        if not isinstance(runtime_engine, str) or runtime_engine not in {"classic", "durable"}:
            raise ValueError("runtime engine must be classic or durable")
        if runtime_engine == "durable":
            if (payload.get("surfaceKind", "agent") != "agent" or payload.get("ownerAppId")
                or payload.get("surfaceKey") or payload.get("_modelRoute", "primary") != "primary"):
                raise ValueError("Durable runtime requires a new standalone Session")
            if payload.get("piSkillsEnabled") or payload.get("codexSkillsEnabled"):
                raise ValueError("Durable Sessions do not support Skills")
            require_engine = getattr(self.runtime, "require_session_engine", None)
            if not callable(require_engine):
                raise ValueError("Durable Runtime Host capability is unavailable")
            require_engine("durable")
        title = str(payload.get("title") or "新对话")
        mode = str(payload.get("mode") or "assistant")
        configuration = self.configuration_store.snapshot()["configuration"]
        session_defaults = configuration["sessionDefaults"]
        model_route_id = str(payload.get("_modelRoute") or "primary").strip()
        model_routes = configuration.get("modelRouting")
        if not isinstance(model_routes, Mapping) or model_route_id not in {
            "primary",
            "traceDiagnostic",
            "toolAgent",
            "subagent",
            "roomCoordinator",
        }:
            raise ValueError("agent model route is invalid")
        configured_model_route = model_routes.get(model_route_id)
        model_route = (
            dict(configured_model_route)
            if isinstance(configured_model_route, Mapping)
            else {"modelProfile": "inherit", "thinkingLevel": "inherit"}
        )
        # Persona is an optional Package boundary, not a Session prerequisite.
        # Keep these columns as compatibility metadata for existing databases;
        # core Session creation must not load a Persona manifest or inject it.
        role_id = canonical_agent_role_id(
            payload.get("roleId") or session_defaults["roleId"]
        )
        role_version = str(
            payload.get("roleVersion") or session_defaults["roleVersion"]
        ).strip()
        if not role_id or not role_version:
            raise ValueError("legacy role metadata must not be empty")
        requested_tool_profile = str(
            payload.get("toolProfileVersion")
            or session_defaults["toolProfileVersion"]
        )
        requested_execution_mode = normalize_execution_mode(
            payload.get("executionMode"),
            tool_profile_version=requested_tool_profile,
            default=PER_ACTION_EXECUTION_MODE,
        )
        requested_tool_profile = canonical_tool_profile(
            requested_tool_profile,
            execution_mode=requested_execution_mode,
        )
        if requested_tool_profile not in {
            CONTROL_CENTER_TOOL_PROFILE,
            DANGEROUS_AUTO_APPROVE_TOOL_PROFILE,
            FULL_ACCESS_TOOL_PROFILE,
            READONLY_TOOL_PROFILE,
        }:
            raise ValueError(
                "new conversations must start in a supported tool profile"
            )
        if requested_tool_profile == DANGEROUS_AUTO_APPROVE_TOOL_PROFILE and (
            mode != "coordinator"
            or requested_execution_mode != FULL_TRUST_EXECUTION_MODE
        ):
            raise ValueError(
                "automatic approval requires coordinator mode and full-trust execution"
            )
        if requested_tool_profile == FULL_ACCESS_TOOL_PROFILE and (
            mode != "coordinator"
            or requested_execution_mode != PER_ACTION_EXECUTION_MODE
        ):
            raise ValueError(
                "full access requires coordinator mode and per-action execution"
            )
        roots_value = payload.get("workspaceRoots")
        if roots_value is None:
            workspace_roots: list[str] = []
        elif isinstance(roots_value, list):
            workspace_roots = [str(item) for item in roots_value]
        else:
            raise ValueError("workspaceRoots must be an array")
        trace_project_binding = is_trace_project_bound_surface(
            payload.get("surfaceKind") or "agent",
            payload.get("ownerAppId") or "",
            payload.get("surfaceKey") or "",
        )
        if trace_project_binding:
            workspace_roots = list(
                exact_trace_project_workspace_roots(workspace_roots)
            )
        elif (
            mode == "coordinator"
            and workspace_roots
            and requested_tool_profile
            not in {
                DANGEROUS_AUTO_APPROVE_TOOL_PROFILE,
                FULL_ACCESS_TOOL_PROFILE,
            }
        ):
            workspace_roots = list(
                existing_workspace_roots(workspace_roots)
            )
        if not trace_project_binding and requested_tool_profile in {
            DANGEROUS_AUTO_APPROVE_TOOL_PROFILE,
            FULL_ACCESS_TOOL_PROFILE,
        }:
            workspace_roots = list(
                system_wide_workspace_roots(workspace_roots)
            )
        for boolean_key in (
            "projectContextEnabled",
            "piSkillsEnabled",
            "codexSkillsEnabled",
        ):
            if (
                boolean_key in payload
                and not isinstance(payload.get(boolean_key), bool)
            ):
                raise ValueError(f"{boolean_key} must be a boolean")
        internal_scope_grant = payload.get("_internalWorkspaceScopeGrant") is True
        if requested_execution_mode == WORKSPACE_MANAGED_EXECUTION_MODE and not (
            internal_scope_grant
            or str(payload.get("workspaceScopeConfirmation") or "")
            == WORKSPACE_SCOPE_CONFIRMATION
        ):
            raise ValueError(
                "workspace-managed execution requires an explicit workspace scope confirmation"
            )
        if (
            requested_execution_mode == FULL_TRUST_EXECUTION_MODE
            and requested_tool_profile != DANGEROUS_AUTO_APPROVE_TOOL_PROFILE
            and not (
                internal_scope_grant
                or str(payload.get("dangerousModeConfirmation") or "")
                == DANGEROUS_MODE_CONFIRMATION
            )
        ):
            raise ValueError(
                "full-trust execution requires an explicit native confirmation"
            )
        unrestricted_profile = requested_tool_profile in {
            DANGEROUS_AUTO_APPROVE_TOOL_PROFILE,
            FULL_ACCESS_TOOL_PROFILE,
        }
        model_profile, thinking_level = self._session_model_defaults(
            payload=payload,
            session_defaults=session_defaults,
            model_route=model_route,
        )
        session = self.sessions.create(
            title=title,
            mode=mode,
            role_id=role_id,
            role_version=role_version,
            role_book_revision_id="",
            model_profile=model_profile,
            thinking_level=thinking_level,
            tool_profile_version=requested_tool_profile,
            execution_mode=requested_execution_mode,
            project_context_enabled=(
                True
                if unrestricted_profile
                else (
                    bool(payload["projectContextEnabled"])
                    if "projectContextEnabled" in payload
                    else bool(workspace_roots)
                )
            ),
            pi_skills_enabled=(
                False if runtime_engine == "durable" else (
                    True if unrestricted_profile else bool(payload.get("piSkillsEnabled", False)))
            ),
            codex_skills_enabled=(
                False if runtime_engine == "durable" else (
                    True if unrestricted_profile else bool(payload.get("codexSkillsEnabled", False)))
            ),
            workspace_roots=workspace_roots,
            runtime_engine=str(runtime_engine),
            surface_kind=str(payload.get("surfaceKind") or "agent"),
            owner_app_id=str(payload.get("ownerAppId") or ""),
            surface_key=str(payload.get("surfaceKey") or ""),
            _connection=connection,
        )
        return session

    def delete_session(self, session_id: str) -> dict[str, object]:
        if self.rooms.participant_for_session(
            session_id,
            active_only=False,
        ) is not None:
            raise ValueError(
                "room participant sessions cannot be deleted directly"
            )
        if self.delegation.owns_session(session_id):
            raise ValueError("subagent sessions cannot be deleted directly")
        runtime = self.runtime_status()
        if (
            runtime.get("activeSessionId") == session_id
            or session_id
            in {
                str(value)
                for value in runtime.get("openSessionIds") or []
            }
        ):
            self.runtime.stop()
        media_files_deleted = self.media.delete_session_files(session_id)
        runtime_binding = self.sessions.runtime_binding(session_id)
        session = self.sessions.delete(session_id)
        drop_live_state = getattr(self.events, "drop_session", None)
        if callable(drop_live_state):
            drop_live_state(session_id)
        session_file = ""
        if (
            isinstance(runtime_binding, Mapping)
            and runtime_binding.get("driverId") == "managed-pi"
            and runtime_binding.get("runtimeKind") == "pi_rpc"
        ):
            session_file = str(runtime_binding.get("transcriptRef") or "")
        if not session_file:
            session_file = str(session.get("sessionFile") or "")
        deleted_file = self._delete_owned_transcript(session_file)
        return {
            "schemaVersion": "rag-ime.agent-session-delete.v1",
            "ok": True,
            "sessionId": session_id,
            "sessionFileDeleted": deleted_file,
            "mediaFilesDeleted": media_files_deleted,
        }

    def _session_model_defaults(
        self,
        *,
        payload: Mapping[str, object],
        session_defaults: Mapping[str, object],
        model_route: Mapping[str, object],
    ) -> tuple[str, str]:
        # Model selection belongs to Pi and the product model-routing policy,
        # not to the legacy Persona catalog.  Role metadata remains readable
        # on historical Sessions, but until Persona is reintroduced as an
        # optional Package it must neither lock nor silently choose a model.
        requested_model_profile = payload.get("modelProfile")
        if requested_model_profile is not None:
            return str(requested_model_profile), ""
        routed_model_profile = str(
            model_route.get("modelProfile") or "inherit"
        ).strip()
        routed_thinking_level = str(
            model_route.get("thinkingLevel") or "inherit"
        ).strip()
        if routed_model_profile != "inherit":
            return (
                routed_model_profile,
                routed_thinking_level
                if routed_thinking_level != "inherit"
                else "",
            )
        return str(session_defaults["modelProfile"]), ""

    def _delete_owned_transcript(self, session_file: str) -> bool:
        if not session_file:
            return False
        path = Path(session_file).expanduser().resolve(strict=False)
        root = (
            self.runtime_factory.session_root
            .expanduser()
            .resolve(strict=False)
        )
        if (
            _is_within(path, root)
            and path.is_file()
            and not path.is_symlink()
        ):
            path.unlink()
            return True
        return False


def _required_text(
    payload: Mapping[str, object],
    key: str,
) -> str:
    value = " ".join(str(payload.get(key) or "").split())
    if not value:
        raise ValueError(f"{key} must not be empty")
    return value


def _bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


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


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
