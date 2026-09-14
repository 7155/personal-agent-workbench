from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .agent_events import AgentEventHub
from .agent_sessions import AgentSessionStore


__all__ = [
    "RuntimeModelCatalog", "RuntimeSessionControls", "RuntimeSessionBranching", "RuntimeSessionLifecycle",
    "AgentRuntimeDriver",
    "AgentRuntimeError",
    "AgentRuntimePolicy",
    "CompactionObserver",
    "RuntimeDriverContext",
    "RuntimeDriverFactory",
    "SessionContextProvider",
    "SkillAllowlistProvider",
    "ToolManifestProvider",
    "ScenarioPolicyProvider",
]


MediaResolver = Callable[[str, str, str], str]
SessionContextProvider = Callable[[Mapping[str, object]], Mapping[str, object]]
SkillAllowlistProvider = Callable[[Mapping[str, object]], list[str]]
ToolManifestProvider = Callable[[Mapping[str, object]], list[Mapping[str, object]]]
PromptSettingsProvider = Callable[[Mapping[str, object]], Mapping[str, object]]
ScenarioPolicyProvider = Callable[[Mapping[str, object]], Mapping[str, object]]
CompactionObserver = Callable[
    [str, Mapping[str, object], str],
    Mapping[str, object] | None,
]


class AgentRuntimeError(RuntimeError):
    """Public failure boundary shared by all runtime drivers."""


@dataclass(frozen=True)
class RuntimeDriverContext:
    """Kernel-owned dependencies passed to a runtime driver factory."""

    sessions: AgentSessionStore
    events: AgentEventHub
    tool_gateway_token: str
    tool_gateway_url: str = "http://127.0.0.1:8766/api/agent/tool/execute"
    media_resolver: MediaResolver | None = None
    tool_manifest_provider: ToolManifestProvider | None = None
    skill_allowlist_provider: SkillAllowlistProvider | None = None
    compaction_observer: CompactionObserver | None = None
    prompt_settings_provider: PromptSettingsProvider | None = None
    scenario_policy_provider: ScenarioPolicyProvider | None = None
    candidate_skill_paths_provider: Callable[[Mapping[str, object]], list[str]] | None = None


@dataclass(frozen=True)
class AgentRuntimePolicy:
    """Runtime-neutral desired state owned by the Agent Kernel."""

    enabled: bool
    startup: str = "lazy"
    idle_timeout_seconds: int = 900

    def __post_init__(self) -> None:
        if self.startup != "lazy":
            raise ValueError("agent runtime startup only supports lazy")
        if not 0 <= int(self.idle_timeout_seconds) <= 86_400:
            raise ValueError("agent runtime idle timeout must be between 0 and 86400")


class RuntimeModelCatalog(Protocol):
    def available_models(self) -> list[dict[str, object]]: ...


class RuntimeSessionControls(Protocol):
    def command_catalog(self, session_id: str) -> list[dict[str, object]]: ...

    def invoke_command(self, session_id: str, command: str) -> dict[str, object]: ...

    def model_catalog(self, session_id: str) -> dict[str, object]: ...

    def set_model(
        self,
        session_id: str,
        *,
        provider: str,
        model_id: str,
        max_tokens: int | None = None,
    ) -> dict[str, object]: ...

    def set_thinking_level(
        self, session_id: str, *, level: str
    ) -> dict[str, object]: ...


class RuntimeSessionBranching(Protocol):
    def model_catalog(self, session_id: str) -> dict[str, object]: ...

    def fork_candidates(self, session_id: str) -> list[dict[str, object]]: ...

    def fork_session(
        self,
        source_session_id: str,
        target_session_id: str,
        *,
        entry_id: str,
    ) -> dict[str, object]: ...

    def rewind_session(
        self, session_id: str, *, entry_id: str
    ) -> dict[str, object]: ...


class RuntimeSessionLifecycle(Protocol):
    def ensure(self, session_id: str) -> dict[str, object]: ...

    def stop(self) -> None: ...


@runtime_checkable
class AgentRuntimeDriver(
    RuntimeModelCatalog,
    RuntimeSessionControls,
    RuntimeSessionBranching,
    RuntimeSessionLifecycle,
    Protocol,
):
    """Runtime-neutral contract consumed by the Agent service and supervisor."""

    @property
    def runtime_kind(self) -> str: ...

    @property
    def driver_id(self) -> str: ...

    @property
    def session_root(self) -> Path: ...

    @property
    def default_model_profile(self) -> str: ...

    def runtime_status(self) -> dict[str, object]: ...

    def prompt(
        self,
        session_id: str,
        message: str,
        *,
        images: list[Mapping[str, str]] | None = None,
        client_message_id: str = "",
        delivery: str = "prompt",
    ) -> dict[str, object]: ...

    def messages(self, session_id: str) -> list[dict[str, object]]: ...

    def complete_once(
        self,
        *,
        request_id: str,
        provider: str,
        model_id: str,
        thinking_level: str,
        message: str,
        on_text_delta: Callable[[str], None] | None = None,
        timeout_seconds: float = 120.0,
    ) -> dict[str, object]: ...

    def cancel_completion(self, request_id: str) -> bool: ...

    def tool_catalog(self, session_id: str) -> list[dict[str, object]]: ...

    def abort(self, session_id: str) -> Mapping[str, object] | None: ...

    def compact(self, session_id: str, instructions: str = "") -> dict[str, object]: ...

    def has_pending_approval(self, session_id: str, approval_id: str) -> bool: ...

    def has_pending_review(self, session_id: str, run_id: str) -> bool: ...

    def resolve_review(
        self,
        session_id: str,
        run_id: str,
        *,
        reviewed: bool,
    ) -> None: ...

    def resolve_approval(
        self,
        session_id: str,
        approval_id: str,
        *,
        approved: bool,
        resolution_state: str = "",
    ) -> None: ...

    def pending_ui_requests(self, session_id: str) -> list[dict[str, object]]: ...

    def resolve_ui_request(
        self,
        session_id: str,
        request_id: str,
        *,
        response: Mapping[str, object],
    ) -> dict[str, object]: ...

    def plugin_list(self) -> list[dict[str, object]]: ...

    def plugin_create_package(self, payload: Mapping[str, object]) -> dict[str, object]: ...

    def plugin_validate(self, source_path: str) -> dict[str, object]: ...

    def plugin_prepare_package(self, source: str) -> dict[str, object]: ...

    def plugin_install(self, payload: Mapping[str, object]) -> dict[str, object]: ...

    def plugin_enable(
        self,
        plugin_id: str,
        *,
        enabled: bool,
        expected_active_digest: str,
        expected_enabled: bool,
    ) -> dict[str, object]: ...

    def plugin_rollback(
        self,
        plugin_id: str,
        *,
        expected_active_digest: str,
        target_digest: str,
    ) -> dict[str, object]: ...

    def plugin_uninstall(
        self,
        plugin_id: str,
        *,
        expected_active_digest: str,
        expected_enabled: bool,
    ) -> dict[str, object]: ...


class RuntimeDriverFactory(Protocol):
    """Creates isolated runtime drivers without exposing driver config to the Kernel."""

    @property
    def runtime_kind(self) -> str: ...

    @property
    def driver_id(self) -> str: ...

    @property
    def session_root(self) -> Path: ...

    @property
    def working_root(self) -> Path: ...

    @property
    def default_model_profile(self) -> str: ...

    def create(
        self,
        context: RuntimeDriverContext,
        *,
        purpose: str,
        session_context_provider: SessionContextProvider | None = None,
    ) -> AgentRuntimeDriver: ...

    def apply_policy(self, policy: AgentRuntimePolicy) -> None: ...

    def reconfigure(self, config: object) -> None: ...
