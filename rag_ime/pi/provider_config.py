from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit


_MAX_PROVIDER_CONFIG_BYTES = 2 * 1024 * 1024
_PROVIDER_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}[a-z0-9]$")
_MODEL_ID_PATTERN = re.compile(r"^[^\s]{1,160}$")
_MODEL_NAME_MAX_CHARS = 160
_MAX_SAFE_JSON_INTEGER = 9_007_199_254_740_991
_PI_THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")


class PiProviderConfigError(ValueError):
    pass


@dataclass(frozen=True)
class PiProviderBundle:
    providers: Mapping[str, Mapping[str, object]] = field(default_factory=dict, repr=False)
    environment: Mapping[str, str] = field(default_factory=dict, repr=False)
    first_provider: str = ""
    first_model: str = ""


def load_pi_provider_config(path: str | Path) -> PiProviderBundle:
    """Translate a user-owned OpenCode provider file into Pi's models.json shape.

    Secrets never enter the returned provider JSON. Each key is replaced by a
    child-process-only environment reference so the generated Pi config remains
    safe to inspect and back up.
    """

    source = Path(path).expanduser()
    if not source.is_file() or source.is_symlink():
        raise PiProviderConfigError("Pi Provider 配置文件不存在或不是普通文件")
    if source.stat().st_size > _MAX_PROVIDER_CONFIG_BYTES:
        raise PiProviderConfigError("Pi Provider 配置文件过大")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PiProviderConfigError("Pi Provider 配置不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise PiProviderConfigError("Pi Provider 配置根节点必须是对象")

    raw_providers = payload.get("provider")
    if not isinstance(raw_providers, dict):
        raise PiProviderConfigError("Pi Provider 配置缺少 provider 对象")

    providers: dict[str, Mapping[str, object]] = {}
    environment: dict[str, str] = {}
    first_provider = ""
    first_model = ""
    for source_id, raw_provider in raw_providers.items():
        if not isinstance(raw_provider, dict):
            continue
        provider_id = _pi_provider_id(str(source_id))
        is_openai_compatible = provider_id == "gpt"
        options = raw_provider.get("options")
        if not isinstance(options, dict):
            options = {}
        base_url = _remote_base_url(options.get("baseURL") or options.get("baseUrl"))
        api_key = str(options.get("apiKey") or "").strip()
        models = _models(
            raw_provider.get("models"),
            include_openai_metadata=is_openai_compatible,
        )
        if not base_url or not api_key or not models:
            continue

        environment_name = f"RAG_IME_PI_{provider_id.upper().replace('-', '_')}_API_KEY"
        environment[environment_name] = api_key
        providers[provider_id] = {
            # Managed Pi 0.84.2 treats this as a standalone custom provider,
            # so its transport and allowlisted model capabilities are explicit.
            **(
                {"api": "openai-completions"}
                if is_openai_compatible
                else {}
            ),
            "baseUrl": base_url,
            "apiKey": f"${environment_name}",
            **(
                {"compat": {"supportsToolSearch": False}}
                if is_openai_compatible
                else {}
            ),
            "models": models,
        }
        if not first_provider:
            first_provider = provider_id
            first_model = str(models[0]["id"])

    if not providers:
        raise PiProviderConfigError("Pi Provider 配置中没有可用的 HTTPS 模型入口")
    return PiProviderBundle(
        providers=providers,
        environment=environment,
        first_provider=first_provider,
        first_model=first_model,
    )


def _pi_provider_id(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    # Do not override Pi's built-in OpenAI catalog and redirect unrelated
    # models to a user gateway. Give imported GPT gateways their own namespace.
    if normalized in {"openai", "openai-compatible", "gpt"}:
        return "gpt"
    normalized = re.sub(r"[^a-z0-9-]+", "-", normalized).strip("-")
    if not _PROVIDER_ID_PATTERN.fullmatch(normalized):
        raise PiProviderConfigError("Pi Provider 名称不合法")
    return normalized


def _remote_base_url(value: object) -> str:
    base_url = str(value or "").strip().rstrip("/")
    if not base_url:
        return ""
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise PiProviderConfigError("远程 Pi Provider 必须使用有效的 HTTPS 地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise PiProviderConfigError("远程 Pi Provider 地址不能包含凭据、查询参数或片段")
    return base_url


def _models(
    value: object,
    *,
    include_openai_metadata: bool = False,
) -> list[dict[str, object]]:
    if not isinstance(value, dict):
        return []
    models: list[dict[str, object]] = []
    for raw_id, raw_model in value.items():
        model_id = str(raw_id).strip()
        if not _MODEL_ID_PATTERN.fullmatch(model_id):
            continue
        model: dict[str, object] = {"id": model_id}
        if include_openai_metadata and isinstance(raw_model, dict):
            name = _model_name(raw_model.get("name"))
            if name:
                model["name"] = name

            limits = raw_model.get("limit")
            if isinstance(limits, dict):
                context_window = _positive_token_limit(limits.get("context"))
                max_tokens = _positive_token_limit(limits.get("output"))
                if context_window is not None:
                    model["contextWindow"] = context_window
                if max_tokens is not None:
                    model["maxTokens"] = max_tokens

            # Preserve Pi's model-specific image limits instead of silently
            # replacing gateway settings with generic defaults. Only documented
            # scalar fields cross this boundary; no arbitrary provider payload.
            input_limits = _image_input_limits(raw_model.get("inputLimits"))
            if input_limits:
                model["inputLimits"] = input_limits
            cache = raw_model.get("promptCache")
            if isinstance(cache, dict):
                lifetimes = {key: seconds for key in ("short", "long")
                             if (seconds := _positive_token_limit(cache.get(key))) is not None}
                if lifetimes:
                    model["promptCache"] = lifetimes
            modalities = raw_model.get("modalities")
            inputs = modalities.get("input") if isinstance(modalities, dict) else None
            if isinstance(inputs, list) and inputs and all(
                isinstance(item, str) and item in {"text", "image"} for item in inputs
            ):
                model["input"] = list(dict.fromkeys(inputs))

            variants = raw_model.get("variants")
            if isinstance(variants, dict):
                supported = {
                    level
                    for level in _PI_THINKING_LEVELS
                    if level in variants and isinstance(variants[level], dict)
                }
                if supported - {"off"}:
                    model["reasoning"] = True
                    model["thinkingLevelMap"] = {
                        level: (
                            "none"
                            if level == "off" and level in supported
                            else level if level in supported else None
                        )
                        for level in _PI_THINKING_LEVELS
                    }
        models.append(model)
    return models


def _image_input_limits(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    result = {}
    request_bytes = _positive_token_limit(value.get("maxRequestBytes"))
    if request_bytes is not None:
        result["maxRequestBytes"] = request_bytes
    image = value.get("images")
    if not isinstance(image, dict):
        return result
    limits = {}
    for key in ("maxPerMessage", "maxPerRequest"):
        limit = _positive_token_limit(image.get(key))
        if limit is not None:
            limits[key] = limit
    resize = image.get("resize")
    if isinstance(resize, dict):
        bounded = {}
        for key in ("maxWidth", "maxHeight", "maxBytes", "jpegQuality"):
            limit = _positive_token_limit(resize.get(key))
            if limit is not None and (key != "jpegQuality" or limit <= 100):
                bounded[key] = limit
        if bounded:
            limits["resize"] = bounded
    if limits:
        result["images"] = limits
    return result


def _model_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    name = value.strip()
    if (
        not name
        or len(name) > _MODEL_NAME_MAX_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        return ""
    return name


def _positive_token_limit(value: object) -> int | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > _MAX_SAFE_JSON_INTEGER
    ):
        return None
    return value
