from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from .embeddings import embed_query, embedding_provider_from_env, embedding_provider_info


EMBEDDING_PROFILE_PROVIDERS = frozenset(
    {
        "environment",
        "none",
        "local-hash",
        "sentence-transformers",
        "mlx-bert",
        "openai-compatible",
    }
)
EMBEDDING_DENSE_BACKENDS = frozenset({"sqlite-exact", "usearch"})
_SECRET_REFERENCE = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")
_PROVIDER_ALIASES = {
    "hash": "local-hash",
    "term-vector": "local-hash",
    "local-term-vector": "local-hash",
    "sentence-transformer": "sentence-transformers",
    "local-model": "sentence-transformers",
    "local-bge": "sentence-transformers",
    "mlx-bge": "mlx-bert",
    "local-bge-mlx": "mlx-bert",
    "openai": "openai-compatible",
}


def normalize_knowledge_embedding_profile(
    settings: Mapping[str, object],
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return a normalized, secret-free active Knowledge embedding profile."""

    source_environment = os.environ if environ is None else environ
    raw_profile = _settings_profile(settings)
    selected_provider = str(raw_profile.get("provider") or "environment").strip().lower()
    selected_provider = _PROVIDER_ALIASES.get(selected_provider, selected_provider)
    if selected_provider not in EMBEDDING_PROFILE_PROVIDERS:
        raise ValueError(f"unsupported Knowledge embedding provider: {selected_provider}")

    if selected_provider == "environment":
        provider = _canonical_provider(
            source_environment.get("RAG_IME_EMBEDDING_PROVIDER", "none")
        )
        profile = {
            "source": "environment",
            "provider": provider,
            "model": str(source_environment.get("RAG_IME_EMBEDDING_MODEL", "")).strip(),
            "baseUrl": str(source_environment.get("RAG_IME_EMBEDDING_BASE_URL", "")).strip(),
            "dimensions": _dimension(
                source_environment.get("RAG_IME_EMBEDDING_DIMENSIONS"),
                default=96 if provider == "local-hash" else 0,
            ),
            "secretReference": "RAG_IME_EMBEDDING_API_KEY",
            "queryPrefix": str(
                source_environment.get("RAG_IME_EMBEDDING_QUERY_PREFIX", "")
            ),
            "documentPrefix": str(
                source_environment.get("RAG_IME_EMBEDDING_DOCUMENT_PREFIX", "")
            ),
            "denseBackend": _dense_backend(
                source_environment.get("RAG_IME_KNOWLEDGE_DENSE_BACKEND", "sqlite-exact")
            ),
        }
    else:
        profile = {
            "source": "settings",
            "provider": selected_provider,
            "model": _bounded_text(raw_profile.get("model"), maximum=1_000),
            "baseUrl": _bounded_text(raw_profile.get("baseUrl"), maximum=2_000),
            "dimensions": _dimension(raw_profile.get("dimensions"), default=0),
            "secretReference": _secret_reference(raw_profile.get("secretReference")),
            "queryPrefix": _bounded_text(raw_profile.get("queryPrefix"), maximum=500),
            "documentPrefix": _bounded_text(
                raw_profile.get("documentPrefix"), maximum=500
            ),
            "denseBackend": _dense_backend(
                raw_profile.get("denseBackend") or "sqlite-exact"
            ),
        }
    for key, environment_key in (("modelRevision", "RAG_IME_EMBEDDING_MODEL_REVISION"), ("modelReference", "RAG_IME_EMBEDDING_MODEL_REFERENCE")):
        value = source_environment.get(environment_key, "") if selected_provider == "environment" else raw_profile.get(key, "")
        if value:
            profile[key] = _bounded_text(value, maximum=1000)
    if profile.get("modelRevision") and not re.fullmatch(r"[0-9a-f]{40}", profile["modelRevision"]):
        raise ValueError("Embedding model revision must be a fixed commit")
    _validate_profile(profile)
    secret_reference = str(profile["secretReference"])
    secret_available = bool(
        secret_reference and str(source_environment.get(secret_reference, "")).strip()
    )
    config_payload = {
        key: value
        for key, value in profile.items()
        if key not in {"secretAvailable", "profileSha256"}
    }
    return {
        **profile,
        "secretAvailable": secret_available,
        "profileSha256": hashlib.sha256(
            json.dumps(
                config_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "secretsVisible": False,
    }


def embedding_environment_from_settings(
    settings: Mapping[str, object],
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Resolve one profile into the worker's standard embedding variables."""

    source_environment = os.environ if environ is None else environ
    profile = normalize_knowledge_embedding_profile(
        settings,
        environ=source_environment,
    )
    if profile["source"] == "environment":
        return {
            str(key): str(value)
            for key, value in source_environment.items()
            if key.startswith("RAG_IME_EMBEDDING_")
            or key == "RAG_IME_KNOWLEDGE_DENSE_BACKEND"
        }

    result = {
        "RAG_IME_EMBEDDING_PROVIDER": str(profile["provider"]),
        "RAG_IME_KNOWLEDGE_DENSE_BACKEND": str(profile["denseBackend"]),
    }
    optional = {
        "RAG_IME_EMBEDDING_MODEL": profile["model"],
        "RAG_IME_EMBEDDING_MODEL_REVISION": profile.get("modelRevision", ""),
        "RAG_IME_EMBEDDING_MODEL_REFERENCE": profile.get("modelReference", ""),
        "RAG_IME_EMBEDDING_BASE_URL": profile["baseUrl"],
        "RAG_IME_EMBEDDING_QUERY_PREFIX": profile["queryPrefix"],
        "RAG_IME_EMBEDDING_DOCUMENT_PREFIX": profile["documentPrefix"],
    }
    result.update({key: str(value) for key, value in optional.items() if value})
    if int(profile["dimensions"] or 0) > 0:
        result["RAG_IME_EMBEDDING_DIMENSIONS"] = str(profile["dimensions"])
    secret_reference = str(profile["secretReference"])
    if secret_reference:
        secret = str(source_environment.get(secret_reference, "")).strip()
        if secret:
            result["RAG_IME_EMBEDDING_API_KEY"] = secret
    return result


def probe_knowledge_embedding_profile(
    settings: Mapping[str, object],
    *,
    environ: Mapping[str, str] | None = None,
    sample_text: str = "Knowledge embedding retrieval probe",
) -> dict[str, Any]:
    """Load/call a candidate profile and report only public diagnostics."""

    profile = normalize_knowledge_embedding_profile(settings, environ=environ)
    worker_environment = embedding_environment_from_settings(settings, environ=environ)
    provider = embedding_provider_from_env(worker_environment)
    provider_info = embedding_provider_info(provider)
    if profile["provider"] == "none":
        return {
            "schemaVersion": "rag-ime.knowledge-embedding-probe.v1",
            "ready": True,
            "profileSha256": profile["profileSha256"],
            "provider": "none",
            "model": "",
            "fingerprint": str(provider_info.get("fingerprint") or "none"),
            "dimensions": 0,
            "semantic": False,
            "latencyMs": 0.0,
            "secretsVisible": False,
        }
    if provider_info.get("configured") is not True:
        raise ValueError("Knowledge embedding profile is not configured")
    started = time.perf_counter()
    query_vector = embed_query(provider, sample_text)
    document_vector = provider.embed(sample_text)
    latency_ms = (time.perf_counter() - started) * 1_000
    if not query_vector or not document_vector:
        raise ValueError("Knowledge embedding probe returned an empty vector")
    if len(query_vector) != len(document_vector):
        raise ValueError("Knowledge embedding query/document dimensions differ")
    if not all(math.isfinite(float(value)) for value in (*query_vector, *document_vector)):
        raise ValueError("Knowledge embedding probe returned a non-finite vector")
    expected_dimensions = int(profile["dimensions"] or 0)
    if expected_dimensions and len(query_vector) != expected_dimensions:
        raise ValueError(
            "Knowledge embedding probe dimension does not match the profile"
        )
    return {
        "schemaVersion": "rag-ime.knowledge-embedding-probe.v1",
        "ready": True,
        "profileSha256": profile["profileSha256"],
        "provider": provider_info.get("provider"),
        "model": provider_info.get("model"),
        "fingerprint": provider_info.get("fingerprint"),
        "dimensions": len(query_vector),
        "semantic": provider_info.get("semantic") is True,
        "latencyMs": round(latency_ms, 3),
        "secretsVisible": False,
    }


def _settings_profile(settings: Mapping[str, object]) -> Mapping[str, object]:
    knowledge = settings.get("knowledgeLibrary")
    if not isinstance(knowledge, Mapping):
        return {}
    embedding = knowledge.get("embedding")
    return embedding if isinstance(embedding, Mapping) else {}


def _canonical_provider(value: object) -> str:
    provider = str(value or "none").strip().lower()
    provider = _PROVIDER_ALIASES.get(provider, provider)
    return provider if provider in EMBEDDING_PROFILE_PROVIDERS - {"environment"} else "none"


def _dense_backend(value: object) -> str:
    backend = str(value or "sqlite-exact").strip().lower()
    if backend not in EMBEDDING_DENSE_BACKENDS:
        raise ValueError(f"unsupported Knowledge dense backend: {backend}")
    return backend


def _dimension(value: object, *, default: int) -> int:
    try:
        parsed = int(value if value not in {None, ""} else default)
    except (TypeError, ValueError) as exc:
        raise ValueError("Knowledge embedding dimensions must be an integer") from exc
    if parsed < 0 or parsed > 65_536:
        raise ValueError("Knowledge embedding dimensions must be between 0 and 65536")
    return parsed


def _secret_reference(value: object) -> str:
    reference = str(value or "").strip()
    if reference and not _SECRET_REFERENCE.fullmatch(reference):
        raise ValueError("Knowledge embedding secretReference must name an environment secret")
    return reference


def _bounded_text(value: object, *, maximum: int) -> str:
    text = str(value or "").strip()
    if len(text) > maximum:
        raise ValueError("Knowledge embedding profile field is too long")
    return text


def _validate_profile(profile: Mapping[str, object]) -> None:
    if profile.get("source") == "environment":
        return
    provider = str(profile["provider"])
    model = str(profile["model"])
    base_url = str(profile["baseUrl"])
    dimensions = int(profile["dimensions"] or 0)
    if provider in {"sentence-transformers", "mlx-bert", "openai-compatible"} and not model:
        raise ValueError(f"Knowledge embedding provider {provider} requires a model")
    if provider == "openai-compatible":
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("openai-compatible Knowledge embedding requires an http(s) baseUrl")
    elif base_url:
        raise ValueError("baseUrl is supported only for openai-compatible embeddings")
    if provider == "local-hash" and dimensions < 8:
        raise ValueError("local-hash Knowledge embedding requires at least 8 dimensions")
