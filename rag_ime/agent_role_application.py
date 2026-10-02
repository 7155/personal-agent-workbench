from __future__ import annotations

from .agent_model_defaults import DEFAULT_AGENT_MODEL_ID
from .agent_runtime_driver import RuntimeModelCatalog
from .agent_runtime_driver import RuntimeDriverFactory

from collections.abc import Callable, Mapping

from .agent_personas import AgentPersonaStore
from .agent_roles import PersonaManifest, agent_role_catalog
from .agent_runtime_driver import AgentRuntimeError
from .contracts.json_schema import validate_contract


class AgentRoleApplicationService:
    """Own persona catalog projection and per-role runtime defaults."""

    def __init__(
        self,
        *,
        personas: AgentPersonaStore,
        runtime_provider: Callable[[], RuntimeModelCatalog],
        runtime_factory: RuntimeDriverFactory,
        default_model_profile_provider: Callable[[], str],
    ) -> None:
        self.personas = personas
        self._runtime_provider = runtime_provider
        self.runtime_factory = runtime_factory
        self._default_model_profile_provider = default_model_profile_provider

    @property
    def runtime(self) -> RuntimeModelCatalog:
        """Resolve the current interactive Runtime after policy replacement."""

        return self._runtime_provider()

    def list_roles(self) -> dict[str, object]:
        available_models = self.available_models()
        roles = [
            *agent_role_catalog(),
            *(persona.to_payload() for persona in self.personas.list()),
        ]
        return {
            "schemaVersion": "rag-ime.agent-role-list.v1",
            "ok": True,
            "items": [
                self.role_payload(
                    role,
                    available_models=available_models,
                )
                for role in roles
            ],
        }

    def create_role(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        persona = self.personas.create(payload)
        return {
            "schemaVersion": "rag-ime.agent-role-create.v1",
            "ok": True,
            "role": self.role_payload(persona.to_payload()),
        }

    def update_role(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        role_id = _required_text(payload, "roleId")
        role_version = _required_text(payload, "roleVersion")
        public_fields = {
            key: value
            for key, value in payload.items()
            if key not in {"roleId", "roleVersion"}
        }
        persona = self.personas.update(role_id, role_version, public_fields)
        return {
            "schemaVersion": "rag-ime.agent-role-update.v1",
            "ok": True,
            "role": self.role_payload(persona.to_payload()),
        }

    def archive_role(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        role_id = _required_text(payload, "roleId")
        role_version = _required_text(payload, "roleVersion")
        persona = self.personas.archive(role_id, role_version)
        return {
            "schemaVersion": "rag-ime.agent-role-archive.v1",
            "ok": True,
            "roleId": persona.role_id,
            "roleVersion": persona.version,
        }

    def model_catalog(self) -> dict[str, object]:
        try:
            available = self.runtime.available_models()
        except AgentRuntimeError as exc:
            return {
                "schemaVersion": (
                    "rag-ime.agent-role-model-catalog.v1"
                ),
                "ok": False,
                "selected": None,
                "thinkingLevel": "off",
                "providers": [],
                "error": str(exc),
            }
        grouped: dict[str, list[dict[str, object]]] = {}
        for value in available:
            if not isinstance(value, Mapping):
                continue
            provider = str(value.get("provider") or "")
            if provider:
                grouped.setdefault(provider, []).append(dict(value))
        providers = [
            {
                "id": provider,
                "displayName": _provider_display_name(provider),
                "models": models,
            }
            for provider, models in sorted(
                grouped.items(),
                key=lambda item: item[0].lower(),
            )
        ]
        default_profile = str(
            self._default_model_profile_provider() or "pi/default"
        )
        default_provider, _, default_model = default_profile.partition(
            "/"
        )
        selected = next(
            (
                {"provider": default_provider, "id": default_model}
                for provider in providers
                for model in provider["models"]
                if isinstance(model, Mapping)
                and provider["id"] == default_provider
                and model.get("id") == default_model
            ),
            None,
        )
        return {
            "schemaVersion": "rag-ime.agent-role-model-catalog.v1",
            "ok": True,
            "selected": selected,
            "thinkingLevel": "off",
            "providers": providers,
        }

    def update_runtime_defaults(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        role_id = _required_text(payload, "roleId")
        role_version = _required_text(payload, "roleVersion")
        provider = _required_text(payload, "provider")
        model_id = _required_text(payload, "modelId")
        thinking_level = _required_text(
            payload,
            "thinkingLevel",
        ).lower()
        catalog = self.model_catalog()
        selected_model = next(
            (
                model
                for provider_item in catalog["providers"]
                if isinstance(provider_item, Mapping)
                and provider_item.get("id") == provider
                for model in provider_item.get("models", [])
                if isinstance(model, Mapping)
                and model.get("id") == model_id
            ),
            None,
        )
        if selected_model is None:
            raise ValueError(
                "所选模型不在当前 Pi 模型目录中"
            )
        if selected_model.get("reasoning") is not True:
            raise ValueError("伙伴默认模型必须支持推理")
        supported = selected_model.get("thinkingLevels")
        if (
            not isinstance(supported, list)
            or thinking_level == "off"
            or thinking_level not in supported
        ):
            raise ValueError("伙伴默认模型必须启用受支持的推理强度")
        defaults = self.personas.set_runtime_defaults(
            role_id,
            role_version,
            model_profile=f"{provider}/{model_id}",
            thinking_level=thinking_level,
        )
        role = self.personas.resolve(role_id, role_version)
        return {
            "schemaVersion": (
                "rag-ime.agent-role-runtime-defaults.v1"
            ),
            "ok": True,
            "defaults": defaults,
            "role": self.role_payload(role.to_payload()),
        }

    def role_payload(
        self,
        value: Mapping[str, object],
        *,
        available_models: set[tuple[str, str]] | None = None,
    ) -> dict[str, object]:
        payload = dict(value)
        defaults_value = payload.get("defaults")
        defaults = (
            dict(defaults_value)
            if isinstance(defaults_value, Mapping)
            else {}
        )
        role = self.personas.resolve(
            payload.get("roleId"),
            payload.get("version"),
        )
        stored = self.personas.runtime_defaults(
            role.role_id,
            role.version,
        )
        defaults.update(
            stored
            or self.initial_runtime_defaults(
                role,
                available_models=available_models,
            )
        )
        payload["defaults"] = defaults
        validate_contract(payload, "agent-persona.v1.json")
        return payload

    def initial_runtime_defaults(
        self,
        role: PersonaManifest,
        *,
        default_model_profile: str | None = None,
        available_models: set[tuple[str, str]] | None = None,
    ) -> dict[str, str]:
        if role.defaults.model_policy == "fixed":
            return {
                "modelProfile": role.defaults.model_profile,
                "thinkingLevel": role.defaults.thinking_level,
            }
        default_profile = str(
            default_model_profile
            or self.runtime_factory.default_model_profile
            or "pi/default"
        )
        provider, separator, configured_model = (
            default_profile.partition("/")
        )
        timeline = {
            "rag-ime-timeline-past-v1": (
                DEFAULT_AGENT_MODEL_ID,
                "max",
            ),
            "rag-ime-timeline-present-v1": (
                DEFAULT_AGENT_MODEL_ID,
                "max",
            ),
            "rag-ime-timeline-future-v1": (
                DEFAULT_AGENT_MODEL_ID,
                "xhigh",
            ),
        }.get(role.visual_profile.avatar_asset_id)
        if not separator or timeline is None:
            return {
                "modelProfile": default_profile,
                "thinkingLevel": "off",
            }
        model_id, thinking_level = timeline
        resolved_models = (
            available_models
            if available_models is not None
            else self.available_models()
        )
        if resolved_models is not None:
            providers = sorted(
                model_provider
                for model_provider, candidate_id in resolved_models
                if candidate_id == model_id
            )
            if not providers:
                return {
                    "modelProfile": default_profile,
                    "thinkingLevel": "off",
                }
            timeline_provider = (
                "gpt" if "gpt" in providers else providers[0]
            )
            return {
                "modelProfile": (
                    f"{timeline_provider}/{model_id}"
                ),
                "thinkingLevel": thinking_level,
            }
        if (
            provider != "gpt"
            and configured_model != DEFAULT_AGENT_MODEL_ID
        ):
            return {
                "modelProfile": default_profile,
                "thinkingLevel": "off",
            }
        return {
            "modelProfile": f"{provider}/{model_id}",
            "thinkingLevel": thinking_level,
        }

    def available_models(
        self,
    ) -> set[tuple[str, str]] | None:
        available_models = getattr(
            self.runtime,
            "available_models",
            None,
        )
        if not callable(available_models):
            return None
        try:
            available = available_models()
        except AgentRuntimeError:
            return None
        return {
            (
                str(model.get("provider") or ""),
                str(model.get("id") or ""),
            )
            for model in available
            if isinstance(model, Mapping)
            and str(model.get("provider") or "")
            and str(model.get("id") or "")
        }


def _provider_display_name(provider: str) -> str:
    normalized = provider.strip().lower()
    return {
        "anthropic": "Anthropic",
        "google": "Google",
        "openai": "OpenAI",
        "gpt": "GPT",
        "deepseek": "DeepSeek",
    }.get(normalized, provider)


def _required_text(payload: Mapping[str, object], key: str) -> str:
    value = " ".join(str(payload.get(key) or "").split())
    if not value:
        raise ValueError(f"{key} must not be empty")
    return value
