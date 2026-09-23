from __future__ import annotations

from .agent_runtime_driver import RuntimeSessionControls
from .agent_sessions import AgentSessionStore
from rag_ime.rooms.store import AgentRoomStore
from .agent_events import AgentEventHub

from collections.abc import Callable, Mapping

from .agent_runtime_driver import AgentRuntimeError
from .agent_execution_policy import (
    FULL_TRUST_EXECUTION_MODE,
    PER_ACTION_EXECUTION_MODE,
    WORKSPACE_MANAGED_EXECUTION_MODE,
    WORKSPACE_SCOPE_CONFIRMATION,
    canonical_tool_profile,
    normalize_execution_mode,
    workspace_scope_sha256,
)
from .agent_tool_ids import (
    CONTROL_CENTER_TOOL_PROFILE,
    CONTROL_TOOL_IDS,
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
from .contracts.json_schema import validate_contract


class AgentSessionPolicyService:
    """Own mutable Session permissions and provider model choices."""

    def __init__(
        self,
        *,
        sessions: AgentSessionStore,
        runtime_provider: Callable[[], RuntimeSessionControls],
        rooms: AgentRoomStore,
        events: AgentEventHub,
        runtime_status: Callable[[], Mapping[str, object]],
        probe_memory_maintenance: Callable[..., Mapping[str, object]],
    ) -> None:
        self.sessions = sessions
        self._runtime_provider = runtime_provider
        self.rooms = rooms
        self.events = events
        self.runtime_status = runtime_status
        self.probe_memory_maintenance = probe_memory_maintenance
        self._extension_app_skill_owners_provider: (
            Callable[[], Mapping[str, str]] | None
        ) = None

    @property
    def runtime(self) -> RuntimeSessionControls:
        return self._runtime_provider()

    def model_catalog(self, session_id: str) -> dict[str, object]:
        catalog = self.runtime.model_catalog(session_id)
        models = (
            catalog.get("models")
            if isinstance(catalog.get("models"), list)
            else []
        )
        selected = catalog.get("selected")
        if isinstance(selected, Mapping):
            selected_provider = str(selected.get("provider") or "")
            selected_id = str(
                selected.get("id") or selected.get("modelId") or ""
            )
            if selected_provider and selected_id and not any(
                isinstance(value, Mapping)
                and str(value.get("provider") or "")
                == selected_provider
                and str(
                    value.get("id") or value.get("modelId") or ""
                )
                == selected_id
                for value in models
            ):
                models = [*models, dict(selected)]
        providers: dict[str, list[dict[str, object]]] = {}
        for value in models:
            if not isinstance(value, Mapping):
                continue
            provider = str(value.get("provider") or "")
            providers.setdefault(provider, []).append(dict(value))
        response = {
            "schemaVersion": "rag-ime.agent-model-catalog.v1",
            "ok": True,
            "sessionId": session_id,
            "selected": selected,
            "thinkingLevel": str(
                catalog.get("thinkingLevel") or "off"
            ),
            "providers": [
                {
                    "id": provider,
                    "displayName": _provider_display_name(provider),
                    "models": items,
                }
                for provider, items in sorted(
                    providers.items(),
                    key=lambda item: item[0].lower(),
                )
            ],
        }
        validate_contract(response, "agent-model-catalog.v1.json")
        return response

    def command_catalog(self, session_id: str) -> dict[str, object]:
        session = self.sessions.get(session_id)
        runtime_available = True
        try:
            commands = self.runtime.command_catalog(session_id)
        except AgentRuntimeError:
            runtime_available = False
            commands = []
        owners = self._extension_app_skill_owners()
        commands = [
            command
            for command in commands
            if self._command_allowed_for_session(
                command,
                session=session,
                owners=owners,
            )
        ]
        return {
            "schemaVersion": "rag-ime.agent-command-catalog.v1",
            "ok": True,
            "sessionId": session_id,
            "runtimeAvailable": runtime_available,
            "items": commands,
        }

    def invoke_command(
        self,
        session_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        session = self.sessions.get(session_id)
        command = _required_text(payload, "command")
        if not self._command_allowed_for_session(
            {"name": command.removeprefix("/").split(maxsplit=1)[0], "source": "skill"},
            session=session,
            owners=self._extension_app_skill_owners(),
        ):
            raise ValueError(
                "Pi Skill belongs to Extension App and is unavailable in this Session"
            )
        result = self.runtime.invoke_command(session_id, command)
        self.events.publish(
            session_id,
            "session_command_invoked",
            {
                "command": str(result.get("command") or command),
                "name": str(result.get("name") or ""),
                "handled": result.get("handled") is True,
            },
        )
        return {
            "schemaVersion": "rag-ime.agent-command-invocation.v1",
            "ok": True,
            "sessionId": session_id,
            "command": str(result.get("command") or command),
            "name": str(result.get("name") or ""),
            "handled": result.get("handled") is True,
            "result": result.get("result"),
            "leafId": result.get("leafId"),
        }

    def bind_extension_app_skill_owners(
        self,
        provider: Callable[[], Mapping[str, str]],
    ) -> None:
        if not callable(provider):
            raise TypeError("extension App Skill owner provider must be callable")
        self._extension_app_skill_owners_provider = provider

    def _extension_app_skill_owners(self) -> dict[str, str]:
        provider = self._extension_app_skill_owners_provider
        if provider is None:
            return {}
        try:
            values = provider()
        except Exception:
            return {}
        if not isinstance(values, Mapping):
            return {}
        return {
            str(skill_ref).strip(): str(owner_app_id).strip()
            for skill_ref, owner_app_id in values.items()
            if str(skill_ref).strip() and str(owner_app_id).strip()
        }

    def extension_app_skill_owners(self) -> dict[str, str]:
        """Return only enabled App Skills validated by the Package inventory."""

        return self._extension_app_skill_owners()

    @staticmethod
    def _command_allowed_for_session(
        command: Mapping[str, object],
        *,
        session: Mapping[str, object],
        owners: Mapping[str, str],
    ) -> bool:
        if str(command.get("source") or "") != "skill":
            return True
        name = str(command.get("name") or "").strip().removeprefix("/")
        if not name.startswith("skill:"):
            return True
        owner_app_id = str(owners.get(name.removeprefix("skill:")) or "")
        if not owner_app_id:
            return True
        return (
            str(session.get("surfaceKind") or "agent") == "extension_app"
            and str(session.get("ownerAppId") or "") == owner_app_id
        )

    def select_model(
        self,
        session_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        selected = self.runtime.set_model(
            session_id,
            provider=_required_text(payload, "provider"),
            model_id=_required_text(payload, "modelId"),
        )
        self.events.publish(
            session_id,
            "session_configuration_changed",
            {
                "kind": "model",
                "selected": selected["selected"],
                "modelProfile": selected["session"].get(
                    "modelProfile",
                    "",
                ),
            },
        )
        response = {
            "schemaVersion": "rag-ime.agent-model-selection.v1",
            "ok": True,
            "sessionId": session_id,
            "selected": selected["selected"],
            "session": selected["session"],
        }
        validate_contract(response, "agent-model-selection.v1.json")
        return response

    def select_thinking_level(
        self,
        session_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        selected = self.runtime.set_thinking_level(
            session_id,
            level=_required_text(payload, "level"),
        )
        self.events.publish(
            session_id,
            "session_configuration_changed",
            {
                "kind": "thinking",
                "thinkingLevel": selected["thinkingLevel"],
                "selected": selected.get("selected"),
            },
        )
        response = {
            "schemaVersion": "rag-ime.agent-thinking-selection.v1",
            "ok": True,
            "sessionId": session_id,
            "thinkingLevel": selected["thinkingLevel"],
            "selected": selected.get("selected"),
        }
        validate_contract(
            response,
            "agent-thinking-selection.v1.json",
        )
        return response

    def update_session(
        self,
        session_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        session = self.sessions.get(session_id)
        runtime_policy_update = _has_runtime_policy_update(payload)
        disclosure_update = "capabilityDisclosurePreferences" in payload
        if runtime_policy_update:
            self._validate_room_runtime_policy(session_id)
        if runtime_policy_update or disclosure_update:
            self._validate_session_idle(session_id)
        self._validate_archive(session_id, payload)
        if "title" in payload:
            session = self.sessions.rename(
                session_id,
                str(payload.get("title") or ""),
            )
        if "archived" in payload:
            session = self.sessions.archive(
                session_id,
                archived=_bool(payload.get("archived")),
            )
        if runtime_policy_update:
            session = self._update_runtime_policy(
                session_id,
                session,
                payload,
            )
        if disclosure_update:
            preferences = payload.get("capabilityDisclosurePreferences")
            if not isinstance(preferences, Mapping):
                raise ValueError(
                    "capabilityDisclosurePreferences must be an object"
                )
            session = self.sessions.set_disclosure_preferences(
                session_id,
                preferences,
            )
            self.events.publish(
                session_id,
                "session_configuration_changed",
                {
                    "kind": "capability_disclosure_preferences",
                    "policyRevision": session.get("policyRevision"),
                },
            )
        if runtime_policy_update or disclosure_update:
            self._retire_target_idle_runtime(session_id)
        maintenance = (
            self.probe_memory_maintenance(
                session_id,
                trigger="session_archive",
            )
            if "archived" in payload
            else {}
        )
        return {
            "schemaVersion": "rag-ime.agent-session-update.v1",
            "ok": True,
            "session": session,
            "memoryMaintenance": maintenance,
            "policyRevision": int(session.get("policyRevision") or 1),
        }

    def _validate_room_runtime_policy(self, session_id: str) -> None:
        participant = self.rooms.participant_for_session(
            session_id,
            active_only=False,
        )
        if (
            participant is None
            or str(participant.get("status") or "") != "active"
        ):
            return
        room = self.rooms.get(str(participant["roomId"]))
        if str(room.get("status") or "") != "active":
            return
        raise ValueError(
            "Active Room participant runtime permissions are managed by the Room"
        )
    def _validate_session_idle(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        runtime = self.runtime_status()
        if str(session.get("status") or "") == "busy" or (
            str(runtime.get("status") or "") == "busy"
            and str(runtime.get("activeSessionId") or "") == session_id
        ):
            raise ValueError("结束当前 Agent Loop 后才能调整运行权限")

    def _retire_target_idle_runtime(self, session_id: str) -> None:
        runtime = self.runtime_status()
        if (
            runtime.get("activeSessionId") != session_id
            and session_id
            not in {
                str(value)
                for value in runtime.get("openSessionIds") or []
            }
        ):
            return
        close_session = getattr(self.runtime, "close_session", None)
        if callable(close_session):
            close_session(session_id)

    def _validate_archive(
        self,
        session_id: str,
        payload: Mapping[str, object],
    ) -> None:
        if "archived" not in payload or not _bool(
            payload.get("archived")
        ):
            return
        participant = self.rooms.participant_for_session(
            session_id,
            active_only=False,
        )
        if (
            participant is not None
            and str(participant.get("status") or "") == "active"
        ):
            room = self.rooms.get(str(participant["roomId"]))
            if str(room.get("status") or "") == "active":
                raise ValueError(
                    "Active Room participant Sessions cannot be archived directly"
                )

    def _update_runtime_policy(
        self,
        session_id: str,
        session: Mapping[str, object],
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        requested_mode = str(
            payload.get("mode") or session.get("mode") or ""
        ).strip()
        roots = payload.get("workspaceRoots")
        if roots is not None and not isinstance(roots, list):
            raise ValueError("workspaceRoots must be an array")
        requested_profile = str(
            payload.get("toolProfileVersion")
            or session.get("toolProfileVersion")
            or "control-center-v1"
        ).strip()
        execution_value: object = payload.get("executionMode")
        if execution_value is None:
            execution_value = (
                None
                if "toolProfileVersion" in payload
                else session.get("executionMode")
            )
        requested_execution_mode = normalize_execution_mode(
            execution_value,
            tool_profile_version=requested_profile,
        )
        requested_profile = canonical_tool_profile(
            requested_profile,
            execution_mode=requested_execution_mode,
        )
        self._validate_tool_profile(
            requested_profile,
            requested_mode=requested_mode,
            requested_execution_mode=requested_execution_mode,
        )
        effective_roots = (
            [str(value) for value in roots]
            if isinstance(roots, list)
            else [str(value) for value in session.get("workspaceRoots") or []]
        )
        trace_project_binding = is_trace_project_bound_surface(
            session.get("surfaceKind"),
            session.get("ownerAppId"),
            session.get("surfaceKey"),
        )
        if trace_project_binding:
            effective_roots = list(
                exact_trace_project_workspace_roots(effective_roots)
            )
        elif requested_profile in {
            DANGEROUS_AUTO_APPROVE_TOOL_PROFILE,
            FULL_ACCESS_TOOL_PROFILE,
        }:
            effective_roots = list(
                system_wide_workspace_roots(effective_roots)
            )
        if (
            not trace_project_binding
            and isinstance(roots, list)
            and requested_mode == "coordinator"
            and requested_profile
            not in {
                DANGEROUS_AUTO_APPROVE_TOOL_PROFILE,
                FULL_ACCESS_TOOL_PROFILE,
            }
        ):
            effective_roots = list(
                existing_workspace_roots(effective_roots)
            )
        scope_changed = (
            workspace_scope_sha256(effective_roots)
            != str(session.get("workspaceScopeSha256") or "")
        )
        entering_execution_mode = (
            requested_execution_mode
            != str(session.get("executionMode") or "")
        )
        grant_workspace_scope = False
        if requested_execution_mode == WORKSPACE_MANAGED_EXECUTION_MODE and (
            entering_execution_mode or scope_changed
        ):
            if (
                str(payload.get("workspaceScopeConfirmation") or "")
                != WORKSPACE_SCOPE_CONFIRMATION
            ):
                raise ValueError(
                    "workspace-managed execution requires an explicit workspace scope confirmation"
                )
            grant_workspace_scope = True
        if (
            requested_execution_mode == FULL_TRUST_EXECUTION_MODE
            and (
                entering_execution_mode
                or scope_changed
            )
            and requested_profile != DANGEROUS_AUTO_APPROVE_TOOL_PROFILE
        ):
            if (
                str(payload.get("dangerousModeConfirmation") or "")
                != DANGEROUS_MODE_CONFIRMATION
            ):
                raise ValueError(
                    "full-trust execution requires an explicit native confirmation"
                )
            grant_workspace_scope = True
        allowed_tools = self._allowed_tools(
            session,
            payload,
            requested_mode=requested_mode,
            requested_profile=requested_profile,
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
                raise ValueError(
                    f"{boolean_key} must be a boolean"
                )
        unrestricted_profile = requested_profile in {
            DANGEROUS_AUTO_APPROVE_TOOL_PROFILE,
            FULL_ACCESS_TOOL_PROFILE,
        }
        updated = self.sessions.set_runtime_policy(
            session_id,
            mode=requested_mode,
            tool_profile_version=requested_profile,
            execution_mode=requested_execution_mode,
            grant_workspace_scope=grant_workspace_scope,
            allowed_tools=allowed_tools,
            project_context_enabled=(
                True
                if unrestricted_profile
                else (
                    bool(payload["projectContextEnabled"])
                    if "projectContextEnabled" in payload
                    else None
                )
            ),
            pi_skills_enabled=(
                True
                if unrestricted_profile
                else (
                    bool(payload["piSkillsEnabled"])
                    if "piSkillsEnabled" in payload
                    else None
                )
            ),
            codex_skills_enabled=(
                True
                if unrestricted_profile
                else (
                    bool(payload["codexSkillsEnabled"])
                    if "codexSkillsEnabled" in payload
                    else None
                )
            ),
            workspace_roots=(
                effective_roots
                if unrestricted_profile or isinstance(roots, list)
                else None
            ),
        )
        self.events.publish(
            session_id,
            "session_configuration_changed",
            {
                "kind": "execution_policy",
                "executionMode": updated.get("executionMode"),
                "workspaceScopeGranted": updated.get(
                    "workspaceScopeGranted"
                ),
                "workspaceScopeSha256": updated.get(
                    "workspaceScopeSha256"
                ),
            },
        )
        return updated
    @staticmethod
    def _validate_tool_profile(
        requested_profile: str,
        *,
        requested_mode: str,
        requested_execution_mode: str,
    ) -> None:
        if requested_profile not in {
            CONTROL_CENTER_TOOL_PROFILE,
            READONLY_TOOL_PROFILE,
            DANGEROUS_AUTO_APPROVE_TOOL_PROFILE,
            FULL_ACCESS_TOOL_PROFILE,
        }:
            raise ValueError("unsupported Agent tool profile")
        if requested_profile == DANGEROUS_AUTO_APPROVE_TOOL_PROFILE and (
            requested_mode != "coordinator"
            or requested_execution_mode != FULL_TRUST_EXECUTION_MODE
        ):
            raise ValueError(
                "automatic approval requires coordinator mode and full-trust execution"
            )
        if requested_profile == FULL_ACCESS_TOOL_PROFILE and (
            requested_mode != "coordinator"
            or requested_execution_mode != PER_ACTION_EXECUTION_MODE
        ):
            raise ValueError(
                "full access requires coordinator mode and per-action execution"
            )

    @staticmethod
    def _allowed_tools(
        session: Mapping[str, object],
        payload: Mapping[str, object],
        *,
        requested_mode: str,
        requested_profile: str,
    ) -> list[str] | None:
        if requested_profile in {
            DANGEROUS_AUTO_APPROVE_TOOL_PROFILE,
            FULL_ACCESS_TOOL_PROFILE,
        }:
            return None
        requested_allowlist_mode = str(
            payload.get("toolAllowlistMode")
            or (
                "explicit"
                if "allowedTools" in payload
                else session.get("toolAllowlistMode")
            )
            or "profile"
        ).strip()
        if requested_allowlist_mode not in {"profile", "explicit"}:
            raise ValueError(
                "unsupported Agent tool allowlist mode"
            )
        if requested_allowlist_mode == "profile":
            return None
        if "allowedTools" not in payload:
            allowed_tools = (
                [
                    str(value)
                    for value in session.get("allowedTools") or []
                ]
                if session.get("toolAllowlistMode") == "explicit"
                else []
            )
            return allowed_tools
        raw_allowed_tools = payload.get("allowedTools")
        if not isinstance(raw_allowed_tools, list):
            raise ValueError("allowedTools must be an array")
        allowed_tools: list[str] = []
        for value in raw_allowed_tools:
            tool_id = str(value or "").strip()
            if tool_id not in CONTROL_TOOL_IDS:
                raise ValueError(
                    f"unknown Agent tool: {tool_id or '(empty)'}"
                )
            if tool_id not in allowed_tools:
                allowed_tools.append(tool_id)
        if requested_mode == "assistant" and any(
            tool_id.startswith("workspace_")
            for tool_id in allowed_tools
        ):
            raise ValueError(
                "assistant sessions cannot enable workspace tools"
            )
        return allowed_tools


def _has_runtime_policy_update(
    payload: Mapping[str, object],
) -> bool:
    return any(
        key in payload
        for key in (
            "mode",
            "executionMode",
            "workspaceRoots",
            "toolProfileVersion",
            "toolAllowlistMode",
            "allowedTools",
            "projectContextEnabled",
            "piSkillsEnabled",
            "codexSkillsEnabled",
        )
    )


def _provider_display_name(provider: str) -> str:
    normalized = provider.strip().lower()
    return {
        "anthropic": "Anthropic",
        "google": "Google",
        "openai": "OpenAI",
        "gpt": "GPT",
        "deepseek": "DeepSeek",
        "openrouter": "OpenRouter",
        "xai": "xAI",
    }.get(normalized, provider)


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
    return str(value or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
