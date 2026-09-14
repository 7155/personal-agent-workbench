"""Construct the sole Pi Host adapter behind the runtime-neutral factory contract."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from rag_ime.agent_runtime_driver import (
    AgentRuntimeDriver,
    AgentRuntimePolicy,
    RuntimeDriverContext,
    SessionContextProvider,
)
from rag_ime.pi.config import PiRuntimeConfig
from rag_ime.pi.runtime import PiRuntimeHostManager

__all__ = ["PiRuntimeDriverFactory"]


class PiRuntimeDriverFactory:
    """Pi-specific construction kept behind the runtime-neutral factory contract."""

    runtime_kind = "pi_rpc"
    driver_id = "managed-pi"

    def __init__(
        self,
        config: PiRuntimeConfig,
        *,
        execution_owner: bool = True,
    ) -> None:
        # The Sidecar and Agent Gateway may share the same configuration DB,
        # but only one process may own a managed Runtime Host.  Keep this
        # process-local fence outside the persisted Agent policy so a Sidecar
        # cannot be re-enabled when the shared `runtime.enabled` setting is
        # applied or refreshed.
        self._execution_owner = bool(execution_owner)
        self._config = replace(
            config,
            enabled=config.enabled and self._execution_owner,
        )

    @property
    def execution_owner(self) -> bool:
        return self._execution_owner

    @property
    def session_root(self) -> Path:
        return self._config.session_dir

    @property
    def working_root(self) -> Path:
        return self._config.agent_dir

    @property
    def default_model_profile(self) -> str:
        provider = str(self._config.provider or "").strip()
        model = str(self._config.model or "").strip()
        return f"{provider}/{model}" if provider and model else "pi/default"

    @property
    def config(self) -> PiRuntimeConfig:
        return self._config

    def create(
        self,
        context: RuntimeDriverContext,
        *,
        purpose: str,
        session_context_provider: SessionContextProvider | None = None,
    ) -> AgentRuntimeDriver:
        if purpose not in {"interactive", "delegated"}:
            raise ValueError("runtime driver purpose must be interactive or delegated")
        config = replace(
            self._config,
            tool_gateway_token=context.tool_gateway_token,
            tool_gateway_url=context.tool_gateway_url,
            idle_timeout_seconds=(
                0 if purpose == "delegated" else self._config.idle_timeout_seconds
            ),
        )
        return PiRuntimeHostManager(
            config=config,
            sessions=context.sessions,
            events=context.events,
            media_resolver=context.media_resolver,
            session_context_provider=session_context_provider,
            tool_manifest_provider=context.tool_manifest_provider,
            skill_allowlist_provider=context.skill_allowlist_provider,
            compaction_observer=context.compaction_observer,
            prompt_settings_provider=context.prompt_settings_provider,
            scenario_policy_provider=context.scenario_policy_provider,
            candidate_skill_paths_provider=context.candidate_skill_paths_provider,
        )

    def reconfigure(self, config: object) -> None:
        if not isinstance(config, PiRuntimeConfig):
            raise TypeError("Pi runtime factory requires PiRuntimeConfig")
        self._config = replace(
            config,
            enabled=config.enabled and self._execution_owner,
        )

    def apply_policy(self, policy: AgentRuntimePolicy) -> None:
        if not isinstance(policy, AgentRuntimePolicy):
            raise TypeError("Pi runtime factory requires AgentRuntimePolicy")
        self._config = replace(
            self._config,
            enabled=policy.enabled and self._execution_owner,
            idle_timeout_seconds=policy.idle_timeout_seconds,
        )
