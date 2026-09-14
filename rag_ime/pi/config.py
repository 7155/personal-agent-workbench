"""Configuration, provider settings and prompt assembly for the managed Pi Host."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit
from urllib.request import getproxies
from rag_ime.agent_core_policy import base_agent_safety_policy_prompt, core_agent_policy_prompt
from rag_ime.agent_tool_ids import CONTROL_TOOL_IDS, MEMORY_CURATION_TOOL_PROFILE
from rag_ime.pi.values import PiRuntimeError
from rag_ime.agent_roles import PersonaManifest, agent_role
from rag_ime.agent_templates import agent_template, progressive_capability_policy
from rag_ime.deepseek_config import load_deepseek_config
from rag_ime.managed_pi_runtime import ManagedPiRuntimeError, discover_managed_pi_runtime
from rag_ime.pi.provider_config import PiProviderConfigError, load_pi_provider_config
from rag_ime.pi.protocols import normalize_protocol_version
from rag_ime.secure_files import atomic_write, regular_reader

__all__ = ["PiRuntimeConfig"]

_DEFAULT_DEBUG_CONTEXT_MAX_BYTES = 5 * 1024 * 1024 * 1024


_MAX_DEBUG_CONTEXT_MAX_BYTES = 64 * 1024 * 1024 * 1024


_DEFAULT_DEBUG_CONTEXT_MAX_CALLS = 128


_MAX_DEBUG_CONTEXT_MAX_CALLS = 256


def _descriptor_path(path: Path) -> Path:
    """Normalize only macOS root aliases before O_NOFOLLOW file access."""

    absolute = path.expanduser().absolute()
    parts = absolute.parts
    if len(parts) > 1 and parts[1] in {"var", "tmp", "etc"}:
        name = parts[1]
        private_target = Path("/private") / name
        try:
            if (Path("/") / name).resolve(strict=False) == private_target:
                return private_target.joinpath(*parts[2:])
        except (OSError, RuntimeError):
            pass
    return absolute


_IME_SURFACE_SYSTEM_PROMPT = """你是输入法中的连续联想引擎，只处理用户明确点击触发的文字生成。

输出规则：
- 只输出可以直接插入光标或替换选区的正文，不解释过程，不加标题、引号、Markdown 围栏或候选编号。
- 延续当前文字的语言、语气、时态与格式；需要改写时只返回改写结果。
- 请求附带当前应用截图时，把可见界面作为辅助上下文，但不要复述界面、泄露无关内容或猜测不可见信息。
- 没有足够上下文时给出最保守、最短的自然续写，不调用工具，也不声称执行了任何操作。
"""


_VOICE_REFINEMENT_SYSTEM_PROMPT = """你是语音转写的第三遍文字校对器。

只输出校对后的原文，不解释、不回答原文中的问题、不使用 Markdown。
只修正有把握的识别错误、口头重复、无意义语气词、标点和空格。
必须保留原意、事实、语气、人称、数字、英文、代码和专有名词；不得扩写、总结或补充信息。
"""


_MEMORY_CURATION_SYSTEM_PROMPT = """You are the governed personal-memory curation engine.

The request is an immutable, database-owned curation packet. Follow only the
task contract supplied by the application. Return exactly one JSON object with
no Markdown or prose outside it. Never call tools, inspect the workspace, load
project files, use ordinary memory recall, or infer facts from prior Session
history. Context-only rows may clarify language but cannot support a claim.
When evidence is incomplete, conflicting, outside the personal-memory domain,
or the requested output cannot be covered exactly, fail closed in the JSON
result instead of guessing.
"""


def _empty_role_book_prompt(_session: Mapping[str, object]) -> str:
    return ""


def _session_mode_prompt(
    session: Mapping[str, object],
    template_id: str,
) -> str:
    if template_id or str(session.get("mode") or "assistant") != "coordinator":
        return ""
    return """<session-mode kind="coordinator">
这是 Agent Session 的协调模式。若本轮同时提供 <room-context>，以其中的 Room 身份、
WorkItem 和 Room 协作工具为准；没有 <room-context> 时才按普通 Session 协调任务。
只有当子任务边界清楚、可独立验收并且并行确实有益时才委派；保留主任务责任，
核对返回证据，再向用户交付。
</session-mode>"""


def _render_session_prompt(layers: list[tuple[str, str]]) -> str:
    parts = ['<agent-prompt-plan schema="rag-ime.agent-prompt-plan.v1">\n']
    for order, (name, content) in enumerate(layers, start=1):
        parts.extend(
            (
                f'<layer order="{order}" name="{name}">\n',
                content.strip(),
                "\n</layer>\n",
            )
        )
    parts.append("</agent-prompt-plan>\n")
    return "".join(parts)


def _project_context_bootstrap_prompt(
    session: Mapping[str, object],
) -> str:
    if session.get("projectContextEnabled") is False:
        return ""
    roots = session.get("workspaceRoots")
    if not isinstance(roots, list) or not roots:
        return ""
    root: Path | None = None
    for value in roots:
        try:
            candidate = Path(str(value)).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if candidate == Path("/") or not candidate.is_dir() or candidate.is_symlink():
            continue
        root = candidate
        break
    if root is None:
        return ""
    guide = root / "AGENTS.md"
    if guide.is_file() and not guide.is_symlink():
        return ""
    if guide.exists() or guide.is_symlink():
        return (
            "项目根的 AGENTS.md 不是普通文件。不要覆盖或绕过它；"
            "先向用户报告这个项目上下文入口异常。"
        )
    return (
        "当前项目根缺少 AGENTS.md。开始项目修改前，先调用 skill_load "
        "加载 bootstrap-project-context，并按该 Skill 检查项目与 docs；"
        "在授权允许时用 resourceRevision=missing 创建根 AGENTS.md。"
        "AGENTS.md 只保存稳定入口与索引；当前 WorkItem、Session、审批和运行状态"
        "继续由 Runtime 与 WorkDocument registry 管理。"
    )


def _split_model_reference(value: object) -> tuple[str, str]:
    reference = str(value or "").strip()
    if reference == "pi/default" or "/" not in reference:
        return "", ""
    provider, model = (part.strip() for part in reference.split("/", 1))
    if not provider or not model:
        return "", ""
    return provider, model


def _pi_model_configuration_from_environment(
    *,
    system_proxy_default: bool = True,
) -> tuple[
    str,
    str,
    str,
    dict[str, str],
    dict[str, Mapping[str, object]],
    str,
]:
    explicit_provider = os.environ.get("RAG_IME_PI_PROVIDER", "").strip()
    explicit_model = os.environ.get("RAG_IME_PI_MODEL", "").strip()
    providers: dict[str, Mapping[str, object]] = {}
    provider_environment = _pi_system_proxy_environment(
        enabled_default=system_proxy_default
    )
    deepseek_error = ""
    try:
        knowledge = load_deepseek_config()
    except (OSError, ValueError) as exc:
        knowledge = None
        deepseek_error = f"DeepSeek 配置不可用：{exc}"

    model_base_url = ""
    deepseek_model = ""
    if knowledge is not None and knowledge.provider_name == "deepseek":
        model_base_url = knowledge.api_base_url
        deepseek_model = knowledge.model
        endpoint = urlsplit(model_base_url)
        if endpoint.scheme != "https" or not endpoint.hostname:
            deepseek_error = "Pi DeepSeek 网关必须使用有效的 HTTPS 地址"
        elif not deepseek_model:
            deepseek_error = "尚未选择 Pi DeepSeek 模型"
        elif not knowledge.api_key:
            deepseek_error = "尚未在模型设置中配置 DeepSeek 凭据"
        elif knowledge.extra_headers:
            deepseek_error = "带自定义请求头的 DeepSeek 网关尚未接入 Pi 隔离配置"
        else:
            providers["deepseek"] = _deepseek_pi_provider(
                model_base_url,
                model=deepseek_model,
                supports_reasoning=knowledge.pi_supports_reasoning,
                supports_reasoning_effort=knowledge.pi_supports_reasoning_effort,
                supports_usage_in_streaming=knowledge.pi_supports_usage_in_streaming,
                requires_reasoning_content=knowledge.pi_requires_reasoning_content,
                thinking_format=knowledge.pi_thinking_format,
            )
            provider_environment["DEEPSEEK_API_KEY"] = knowledge.api_key
            deepseek_error = ""

    imported_error = ""
    imported_first_provider = ""
    imported_first_model = ""
    provider_config_path = os.environ.get("RAG_IME_PI_PROVIDER_CONFIG", "").strip()
    if provider_config_path:
        try:
            imported = load_pi_provider_config(provider_config_path)
        except (OSError, PiProviderConfigError) as exc:
            imported_error = str(exc)
        else:
            providers.update(imported.providers)
            provider_environment.update(imported.environment)
            imported_first_provider = imported.first_provider
            imported_first_model = imported.first_model

    provider = explicit_provider or (
        "deepseek" if "deepseek" in providers else imported_first_provider
    )
    if provider == "deepseek":
        model = explicit_model or deepseek_model
    else:
        model = (
            explicit_model
            or _first_provider_model(providers.get(provider))
            or imported_first_model
        )
    configured_ids = _configured_model_ids(providers.get(provider))
    if configured_ids and model not in configured_ids:
        model = configured_ids[0]

    if not providers:
        error = imported_error or deepseek_error or "尚未配置 Pi 对话模型"
        return provider, model, model_base_url, provider_environment, {}, error
    if provider not in providers:
        return (
            provider,
            model,
            model_base_url,
            provider_environment,
            providers,
            f"当前 Pi Provider 不可用：{provider or '<empty>'}",
        )
    if not model:
        return (
            provider,
            model,
            model_base_url,
            provider_environment,
            providers,
            "尚未选择 Pi 对话模型",
        )
    # An optional imported provider must not take a working DeepSeek route down.
    # It becomes fatal only when the user explicitly selected that provider.
    if imported_error and explicit_provider and explicit_provider != "deepseek":
        return (
            provider,
            model,
            model_base_url,
            provider_environment,
            providers,
            imported_error,
        )
    return provider, model, model_base_url, provider_environment, providers, ""


def _pi_system_proxy_environment(*, enabled_default: bool = True) -> dict[str, str]:
    """Pass the user's HTTP proxy only to remote Pi provider processes."""

    configured = os.environ.get("RAG_IME_PI_SYSTEM_PROXY")
    enabled = (
        enabled_default
        if configured is None
        else configured.strip().lower() not in {"0", "false", "no", "off"}
    )
    if not enabled:
        return {}
    try:
        proxies = getproxies()
    except OSError:
        return {}
    http_proxy = _supported_pi_proxy(proxies.get("http"))
    https_proxy = _supported_pi_proxy(proxies.get("https")) or http_proxy
    if not http_proxy and not https_proxy:
        return {}
    no_proxy = _pi_no_proxy(proxies.get("no"))
    environment = {
        "NODE_USE_ENV_PROXY": "1",
        "NO_PROXY": no_proxy,
        "no_proxy": no_proxy,
    }
    if http_proxy:
        environment.update(
            {
                "HTTP_PROXY": http_proxy,
                "http_proxy": http_proxy,
            }
        )
    if https_proxy:
        environment.update(
            {
                "HTTPS_PROXY": https_proxy,
                "https_proxy": https_proxy,
            }
        )
    return environment


def _supported_pi_proxy(value: object) -> str:
    proxy = str(value or "").strip()
    if not proxy:
        return ""
    parsed = urlsplit(proxy)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.query
        or parsed.fragment
    ):
        return ""
    return proxy


def _pi_no_proxy(value: object) -> str:
    entries = [
        item.strip()
        for item in str(value or "").replace(" ", ",").split(",")
        if item.strip()
    ]
    for required in ("127.0.0.1", "localhost", "::1"):
        if required not in entries:
            entries.append(required)
    return ",".join(entries)


def _deepseek_pi_provider(
    base_url: str,
    *,
    model: str = "",
    supports_reasoning: bool = True,
    supports_reasoning_effort: bool = True,
    supports_usage_in_streaming: bool = True,
    requires_reasoning_content: bool = True,
    thinking_format: str = "deepseek",
) -> dict[str, object]:
    provider: dict[str, object] = {
        "baseUrl": base_url,
        "apiKey": "$DEEPSEEK_API_KEY",
        "compat": {
            "supportsStore": False,
            "supportsDeveloperRole": False,
            "supportsReasoningEffort": supports_reasoning_effort,
            "supportsUsageInStreaming": supports_usage_in_streaming,
            "requiresReasoningContentOnAssistantMessages": requires_reasoning_content,
            "thinkingFormat": thinking_format,
        },
    }
    if not supports_reasoning:
        model_ids = {"deepseek-v4-flash", "deepseek-v4-pro"}
        if model.strip():
            model_ids.add(model.strip())
        provider["modelOverrides"] = {
            model_id: {"reasoning": False} for model_id in sorted(model_ids)
        }
    return provider


def _first_provider_model(provider: Mapping[str, object] | None) -> str:
    if not isinstance(provider, Mapping):
        return ""
    models = provider.get("models")
    if not isinstance(models, list) or not models:
        return ""
    first = models[0]
    return str(first.get("id") or "").strip() if isinstance(first, Mapping) else ""


def _configured_model_ids(provider: Mapping[str, object] | None) -> list[str]:
    if not isinstance(provider, Mapping):
        return []
    raw_models = provider.get("models")
    if not isinstance(raw_models, list):
        return []
    return [
        model_id
        for value in raw_models
        if isinstance(value, Mapping)
        for model_id in [str(value.get("id") or "").strip()]
        if model_id
    ]


def _provider_credential_values(environment: Mapping[str, str]) -> list[str]:
    """Return secret-bearing Provider values without treating proxy flags as secrets."""

    credential_markers = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
    return [
        str(value)
        for key, value in environment.items()
        if value and any(marker in str(key).upper() for marker in credential_markers)
    ]


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return min(maximum, max(minimum, value))


@dataclass(frozen=True)
class PiRuntimeConfig:
    enabled: bool
    executable: Path | None
    agent_dir: Path
    session_dir: Path
    logs_dir: Path
    debug_context_dir: Path | None = None
    debug_context_max_bytes: int = _DEFAULT_DEBUG_CONTEXT_MAX_BYTES
    debug_context_max_calls: int = _DEFAULT_DEBUG_CONTEXT_MAX_CALLS
    node_executable: str = "node"
    idle_timeout_seconds: int = 900
    command_timeout_seconds: float = 15.0
    provider: str = ""
    model: str = ""
    extension_path: Path | None = None
    tools: tuple[str, ...] = ()
    tool_gateway_url: str = "http://127.0.0.1:8766/api/agent/tool/execute"
    tool_gateway_token: str = ""
    plugin_approval_token: str = ""
    provider_environment: Mapping[str, str] = field(default_factory=dict, repr=False)
    model_providers: Mapping[str, Mapping[str, object]] = field(
        default_factory=dict, repr=False
    )
    model_base_url: str = ""
    model_configured: bool = True
    model_configuration_error: str = ""
    pi_version: str = ""
    installation_error: str = ""
    protocol_version: str = "2"
    max_sessions: int = 8
    provider_retry_enabled: bool = True
    # Seven exponential delays starting at 2.5s total 317.5s. Pi owns this
    # same-Session Provider retry window before control returns to the caller.
    provider_retry_max_retries: int = 7
    provider_retry_base_delay_ms: int = 2_500
    role_resolver: Callable[[object, object], PersonaManifest] = field(
        default=agent_role,
        repr=False,
        compare=False,
    )
    role_book_resolver: Callable[[Mapping[str, object]], str] = field(
        default=_empty_role_book_prompt,
        repr=False,
        compare=False,
    )
    # Team execution supplies a container-visible environment overlay and an
    # opaque ExecutionSpec.  Both are opt-in; personal macOS Hosts retain the
    # environment and local process launch path above unchanged.
    runtime_environment: Mapping[str, str] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    execution_spec: Any = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        normalize_protocol_version(self.protocol_version)

    @classmethod
    def from_environment(
        cls,
        *,
        enabled_default: bool = False,
        idle_timeout_default: int = 900,
        system_proxy_default: bool = True,
    ) -> PiRuntimeConfig:
        app_support = Path(
            os.environ.get("RAG_IME_APP_SUPPORT_DIR")
            or Path.home() / "Library" / "Application Support" / "RagIme"
        ).expanduser()
        executable_value = os.environ.get("RAG_IME_PI_EXECUTABLE", "").strip()
        extension_value = os.environ.get("RAG_IME_PI_EXTENSION", "").strip()
        debug_context_value = os.environ.get("RAG_IME_PI_DEBUG_CONTEXT_DIR", "").strip()
        node_value = os.environ.get("RAG_IME_PI_NODE", "").strip()
        # The active managed-runtime manifest owns the production Pi version.
        # RAG_IME_PI_VERSION remains an explicit development/diagnostic pin,
        # but an absent value must not freeze every future atomic activation to
        # the version that happened to be current when this adapter was written.
        expected_pi_version = os.environ.get("RAG_IME_PI_VERSION", "").strip()
        development_tools = tuple(
            item.strip()
            for item in os.environ.get(
                "RAG_IME_PI_TOOLS",
                ",".join(CONTROL_TOOL_IDS),
            ).split(",")
            if item.strip()
        )
        executable: Path | None = None
        extension: Path | None = None
        node_executable = ""
        tools: tuple[str, ...] = ()
        pi_version = expected_pi_version
        installation_error = ""
        protocol_version = os.environ.get("RAG_IME_PI_PROTOCOL_VERSION", "").strip()
        if executable_value:
            # Explicit executable overrides are for source-tree development only.
            # Product discovery never falls back to PATH or a user's global Pi.
            executable = Path(executable_value).expanduser()
            extension = Path(extension_value).expanduser() if extension_value else None
            node_executable = node_value or "node"
            tools = development_tools if extension is not None else ()
        elif node_value or extension_value:
            installation_error = (
                "development Pi node/extension overrides require RAG_IME_PI_EXECUTABLE"
            )
        else:
            try:
                installation = discover_managed_pi_runtime(
                    app_support,
                    expected_pi_version=expected_pi_version,
                )
            except ManagedPiRuntimeError as exc:
                installation_error = str(exc)
            else:
                executable = installation.executable
                extension = installation.extension_path
                node_executable = installation.node_executable
                tools = installation.tools
                pi_version = installation.pi_version
                protocol_version = protocol_version or installation.protocol_version
        (
            provider,
            model,
            model_base_url,
            provider_environment,
            model_providers,
            model_error,
        ) = _pi_model_configuration_from_environment(
            system_proxy_default=system_proxy_default
        )
        return cls(
            enabled=_env_bool("RAG_IME_PI_ENABLED", enabled_default),
            executable=executable,
            agent_dir=app_support / "Agent" / "config",
            session_dir=app_support / "Agent" / "sessions",
            logs_dir=app_support / "Agent" / "logs",
            debug_context_dir=(
                Path(debug_context_value).expanduser() if debug_context_value else None
            ),
            debug_context_max_bytes=_env_int(
                "RAG_IME_PI_DEBUG_CONTEXT_MAX_BYTES",
                _DEFAULT_DEBUG_CONTEXT_MAX_BYTES,
                minimum=1,
                maximum=_MAX_DEBUG_CONTEXT_MAX_BYTES,
            ),
            debug_context_max_calls=_env_int(
                "RAG_IME_PI_DEBUG_CONTEXT_MAX_CALLS",
                _DEFAULT_DEBUG_CONTEXT_MAX_CALLS,
                minimum=1,
                maximum=_MAX_DEBUG_CONTEXT_MAX_CALLS,
            ),
            node_executable=node_executable,
            idle_timeout_seconds=_env_int(
                "RAG_IME_PI_IDLE_TIMEOUT_SECONDS",
                idle_timeout_default,
                minimum=0,
                maximum=86400,
            ),
            command_timeout_seconds=float(
                _env_int(
                    "RAG_IME_PI_COMMAND_TIMEOUT_SECONDS", 15, minimum=1, maximum=300
                )
            ),
            provider=provider,
            model=model,
            extension_path=extension,
            tools=tools,
            tool_gateway_url=(
                os.environ.get("RAG_IME_AGENT_TOOL_URL", "").strip()
                or "http://127.0.0.1:8766/api/agent/tool/execute"
            ),
            pi_version=pi_version,
            installation_error=installation_error,
            protocol_version=protocol_version or "2",
            max_sessions=_env_int("RAG_IME_PI_MAX_SESSIONS", 8, minimum=1, maximum=32),
            provider_retry_enabled=_env_bool(
                "RAG_IME_PI_PROVIDER_RETRY_ENABLED",
                True,
            ),
            provider_retry_max_retries=_env_int(
                "RAG_IME_PI_PROVIDER_RETRY_MAX_RETRIES",
                7,
                minimum=1,
                maximum=8,
            ),
            provider_retry_base_delay_ms=_env_int(
                "RAG_IME_PI_PROVIDER_RETRY_BASE_DELAY_MS",
                2_500,
                minimum=250,
                maximum=60_000,
            ),
            provider_environment=provider_environment,
            model_providers=model_providers,
            model_base_url=model_base_url,
            model_configured=not bool(model_error),
            model_configuration_error=model_error,
        )

    def launch_host_command(self) -> list[str]:
        # A team Host command lives in the trusted runtime image, so its
        # executable is intentionally absent from the server's host filesystem.
        # The isolated launcher validates and supplies this immutable argv;
        # personal Hosts continue through the installed executable checks.
        if self.isolated_execution:
            spec = self.execution_spec
            command = tuple(str(part) for part in getattr(spec, "runtime_command", ()))
            if not command or any(
                not part or any(ord(char) < 32 for char in part) for part in command
            ):
                raise PiRuntimeError("isolated team runtime command is unavailable")
            return list(command)
        if self.executable is None:
            raise PiRuntimeError("managed Pi runtime is not installed")
        executable = self.executable.expanduser().resolve(strict=False)
        if not executable.is_file():
            raise PiRuntimeError("managed Pi executable does not exist")
        if executable.suffix.lower() in {".js", ".mjs", ".cjs"}:
            if not self.node_executable:
                raise PiRuntimeError("managed Pi Node runtime is unavailable")
            return [self.node_executable, str(executable)]
        return [str(executable)]

    def system_prompt_for_session(
        self,
        session: Mapping[str, object],
        *,
        prompt_settings: Mapping[str, object] | None = None,
    ) -> str:
        tool_profile = str(session.get("toolProfileVersion") or "")
        if tool_profile == "ime-surface-v1":
            return _IME_SURFACE_SYSTEM_PROMPT
        if tool_profile == "voice-refinement-v1":
            return _VOICE_REFINEMENT_SYSTEM_PROMPT
        if tool_profile == MEMORY_CURATION_TOOL_PROFILE:
            return _MEMORY_CURATION_SYSTEM_PROMPT
        core_prompt = core_agent_policy_prompt(
            base_agent_safety_policy_prompt(),
            session,
        )
        template_id = str(session.get("agentTemplateId") or "").strip()
        template = None
        if template_id:
            template = agent_template(
                template_id,
                session.get("agentTemplateVersion") or "1",
            )
        session_mode_prompt = _session_mode_prompt(session, template_id)
        capability_prompt = (
            template.runtime_prompt
            if template is not None
            else progressive_capability_policy()
        )
        # Persona is an optional future Package. Role metadata may remain on
        # historical Sessions, but the Runtime Host must not inject it unless a
        # Package explicitly contributes context.
        layers = [("core_rails", core_prompt)]
        project_bootstrap_prompt = _project_context_bootstrap_prompt(session)
        if project_bootstrap_prompt:
            layers.append(("project_context_bootstrap", project_bootstrap_prompt))
        if session_mode_prompt:
            layers.append(("session_mode_policy", session_mode_prompt))
        layers.append(("agent_template_policy", capability_prompt))
        user_instructions = str(
            (prompt_settings or {}).get("systemInstructions") or ""
        ).strip()
        if user_instructions:
            layers.append(("user_system_instructions", user_instructions))
        return _render_session_prompt(layers)

    def resolved_model_reference(
        self, session: Mapping[str, object]
    ) -> tuple[str, str]:
        session_provider, session_model = _split_model_reference(
            session.get("modelProfile")
        )
        selected_provider = session_provider or self.provider
        selected_model = session_model or self.model
        configured_ids = _configured_model_ids(
            self.model_providers.get(selected_provider)
        )
        if configured_ids and selected_model not in configured_ids:
            # Pi/provider configuration is the only model catalog authority.
            # Persona names are not model aliases and must not participate in
            # compatibility fallback.
            if self.provider == selected_provider and self.model in configured_ids:
                selected_model = self.model
            else:
                selected_model = configured_ids[0]
        return selected_provider, selected_model

    @property
    def isolated_execution(self) -> bool:
        """Whether this config is bound to a server-owned isolated attempt."""

        return self.execution_spec is not None

    def workspace_paths_for_host(self, roots: Sequence[str]) -> list[str]:
        """Map persisted host workspace roots into the worker namespace.

        Session rows keep the canonical checkout path because the server and
        workspace coordinator own that state.  A team Pi Host runs in a
        container where only the attempt workspace is mounted at
        ``/workspace``.  Refusing roots outside the bound checkout prevents a
        stale or forged Session row from turning ``cwd`` into a host path.
        Personal Hosts retain their existing paths unchanged.
        """

        if not self.isolated_execution:
            return [str(value) for value in roots if str(value).strip()]
        spec = self.execution_spec
        host_root = Path(str(getattr(spec, "workspace_root"))).expanduser().resolve(
            strict=False
        )
        container_root = Path(str(getattr(spec, "container_workspace")))
        values = [str(value).strip() for value in roots if str(value).strip()]
        if not values:
            raise PiRuntimeError(
                "isolated team Session requires a bound workspace root"
            )
        mapped: list[str] = []
        for value in values:
            candidate = Path(value).expanduser().resolve(strict=False)
            try:
                relative = candidate.relative_to(host_root)
            except ValueError as exc:
                raise PiRuntimeError(
                    "team Session workspace root is outside its execution workspace"
                ) from exc
            mapped.append((container_root / relative).as_posix())
        return mapped

    def container_session_path(self, path: str) -> str:
        """Translate a persisted session transcript path for Pi in the worker."""

        value = str(path or "").strip()
        if not value or not self.isolated_execution:
            return value
        spec = self.execution_spec
        session_root = self.session_dir.expanduser().resolve(strict=False)
        candidate = Path(value).expanduser().resolve(strict=False)
        try:
            relative = candidate.relative_to(session_root)
        except ValueError as exc:
            raise PiRuntimeError(
                "team Session transcript is outside its scoped session directory"
            ) from exc
        return (Path(str(getattr(spec, "container_session_dir"))) / relative).as_posix()

    def host_session_path(self, path: str) -> str:
        """Translate a worker transcript path back to the persisted host path."""

        value = str(path or "").strip()
        if not value or not self.isolated_execution:
            return value
        spec = self.execution_spec
        container_root = Path(str(getattr(spec, "container_session_dir")))
        candidate = Path(value).expanduser().resolve(strict=False)
        try:
            relative = candidate.relative_to(container_root)
        except ValueError as exc:
            raise PiRuntimeError(
                "Pi returned a transcript outside its scoped session directory"
            ) from exc
        return (self.session_dir.expanduser() / relative).as_posix()

    def child_environment(
        self, *, session: Mapping[str, object] | None = None
    ) -> dict[str, str]:
        allowed = (
            "HOME",
            "PATH",
            "TMPDIR",
            "LANG",
            "LC_ALL",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        )
        environment = (
            {}
            if self.isolated_execution
            else {key: os.environ[key] for key in allowed if os.environ.get(key)}
        )
        environment.update(
            {str(key): str(value) for key, value in self.provider_environment.items()}
        )
        environment["RAG_IME_APP_SUPPORT_DIR"] = str(self.agent_dir.parent.parent)
        environment["PI_CODING_AGENT_DIR"] = str(self.agent_dir)
        environment["RAG_IME_PI_AGENT_DIR"] = str(self.agent_dir)
        environment["RAG_IME_PI_SESSION_DIR"] = str(self.session_dir)
        if self.debug_context_dir is not None:
            environment["RAG_IME_PI_DEBUG_CONTEXT_DIR"] = str(self.debug_context_dir)
            environment["RAG_IME_PI_DEBUG_CONTEXT_MAX_BYTES"] = str(
                min(
                    _MAX_DEBUG_CONTEXT_MAX_BYTES,
                    max(1, int(self.debug_context_max_bytes)),
                )
            )
            environment["RAG_IME_PI_DEBUG_CONTEXT_MAX_CALLS"] = str(
                min(
                    _MAX_DEBUG_CONTEXT_MAX_CALLS,
                    max(1, int(self.debug_context_max_calls)),
                )
            )
        environment["RAG_IME_PI_MAX_SESSIONS"] = str(self.max_sessions)
        if self.tool_gateway_token:
            environment["RAG_IME_AGENT_TOOL_TOKEN"] = self.tool_gateway_token
            environment["RAG_IME_AGENT_TOOL_URL"] = self.tool_gateway_url
            environment["RAG_IME_TOOL_GATEWAY_TOKEN"] = self.tool_gateway_token
            environment["RAG_IME_TOOL_GATEWAY_URL"] = self.tool_gateway_url
        if self.plugin_approval_token:
            environment["RAG_IME_PLUGIN_APPROVAL_TOKEN"] = self.plugin_approval_token
        # A trusted team launcher may replace host paths with the fixed paths
        # visible inside its isolated container.  Apply this last so no
        # inherited HOME, provider endpoint or host state directory wins over
        # the server-derived scope.  Personal configs leave it empty.
        environment.update(
            {str(key): str(value) for key, value in self.runtime_environment.items()}
        )
        if session is not None:
            environment["RAG_IME_AGENT_SESSION_ID"] = str(session.get("id") or "")
            environment["RAG_IME_AGENT_SESSION_MODE"] = str(
                session.get("mode") or "assistant"
            )
            environment["RAG_IME_AGENT_TOOL_PROFILE_VERSION"] = str(
                session.get("toolProfileVersion") or "control-center-v1"
            )
            environment["RAG_IME_AGENT_EXECUTION_MODE"] = str(
                session.get("executionMode") or "per_action"
            )
            if session.get("roomParticipant") is not None:
                environment["RAG_IME_AGENT_ROOM_BOUND"] = "1"
            environment["RAG_IME_AGENT_DELEGATION_DEPTH"] = str(
                session.get("delegationDepth") or 0
            )
        return environment

    def prepare_agent_config(self) -> None:
        if not self.model_configured:
            return
        self.agent_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.agent_dir.is_symlink():
            raise PiRuntimeError("managed Pi agent directory must not be a symlink")
        os.chmod(self.agent_dir, 0o700)
        self._prepare_retry_settings()
        target = self.agent_dir / "models.json"
        if target.is_symlink():
            raise PiRuntimeError("managed Pi models.json must not be a symlink")
        descriptor_target = _descriptor_path(target)
        providers = dict(self.model_providers)
        if not providers and self.provider == "deepseek" and self.model_base_url:
            providers = {
                "deepseek": _deepseek_pi_provider(
                    self.model_base_url,
                    model=self.model,
                ),
            }
        if not providers:
            return
        payload = {"providers": providers}
        encoded = (
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        for secret in _provider_credential_values(self.provider_environment):
            if secret and secret.encode("utf-8") in encoded:
                raise PiRuntimeError(
                    "managed Pi models.json must not contain provider credentials"
                )
        try:
            # ``atomic_write`` opens every parent component with
            # O_NOFOLLOW, creates the temporary file with O_EXCL, and
            # replaces the target relative to the held parent descriptor.
            # A concurrent directory/file symlink swap therefore cannot
            # redirect this config write outside the scoped agent directory.
            atomic_write(descriptor_target, encoded)
        except (OSError, ValueError) as exc:
            raise PiRuntimeError(
                "managed Pi models.json could not be written safely"
            ) from exc

    def _prepare_retry_settings(self) -> None:
        """Fill PAW defaults into Pi's native retry settings without overriding the user."""

        target = self.agent_dir / "settings.json"
        if target.is_symlink():
            raise PiRuntimeError("managed Pi settings.json must not be a symlink")
        descriptor_target = _descriptor_path(target)
        settings: dict[str, object] = {}
        if target.exists():
            try:
                # Read through a descriptor opened relative to O_NOFOLLOW
                # parent directories.  The path checks above are only a
                # friendly error; they are not the security boundary.
                with regular_reader(descriptor_target) as source:
                    loaded = json.loads(source.read().decode("utf-8"))
            except FileNotFoundError:
                # A concurrent unlink is harmless: the atomic replacement
                # below will recreate the scoped settings file.
                loaded = {}
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise PiRuntimeError(
                    "managed Pi settings.json is not valid JSON"
                ) from exc
            except ValueError as exc:
                raise PiRuntimeError(
                    "managed Pi settings.json must be a regular file"
                ) from exc
            if not isinstance(loaded, dict):
                raise PiRuntimeError("managed Pi settings.json must contain an object")
            settings = dict(loaded)

        retry_value = settings.get("retry")
        if retry_value is None:
            retry: dict[str, object] = {}
        elif isinstance(retry_value, Mapping):
            retry = dict(retry_value)
        else:
            raise PiRuntimeError("managed Pi retry settings must contain an object")
        retry.setdefault("enabled", bool(self.provider_retry_enabled))
        retry.setdefault(
            "maxRetries",
            min(8, max(1, int(self.provider_retry_max_retries))),
        )
        retry.setdefault(
            "baseDelayMs",
            min(60_000, max(250, int(self.provider_retry_base_delay_ms))),
        )
        settings["retry"] = retry

        encoded = (
            json.dumps(settings, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        try:
            atomic_write(descriptor_target, encoded)
        except (OSError, ValueError) as exc:
            raise PiRuntimeError(
                "managed Pi settings.json could not be written safely"
            ) from exc
