from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .models import DEFAULT_MINERU_TIMEOUT_SECONDS


_EMBEDDING_PROVIDER_ALIASES = {
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
_KNOWN_EMBEDDING_PROVIDERS = frozenset(
    {
        "none",
        "local-hash",
        "sentence-transformers",
        "mlx-bert",
        "openai-compatible",
    }
)


def normalized_knowledge_embedding_provider(value: object) -> str:
    provider = str(value or "none").strip().lower()
    normalized = _EMBEDDING_PROVIDER_ALIASES.get(provider, provider)
    # Keep the identity in lockstep with the embedding runtime: unknown
    # environment values select the null provider there, so they must not
    # make the supervisor reject an otherwise healthy worker as a mismatch.
    return normalized if normalized in _KNOWN_EMBEDDING_PROVIDERS else "none"


def normalized_knowledge_root(root: Path) -> str:
    return str(Path(root).expanduser().resolve(strict=False))


def knowledge_worker_fingerprint(
    root: Path,
    *,
    mineru_enabled: bool,
    mineru_port: int,
    mineru_timeout_seconds: float = DEFAULT_MINERU_TIMEOUT_SECONDS,
    idle_seconds: float,
    python_executable: str,
    python_version: str,
    embedding_provider: str,
    embedding_model: str,
    dense_backend: str,
    embedding_profile_sha256: str = "",
    reranker_provider: str = "none",
    reranker_model_path: str = "",
    reranker_model_revision: str = "",
    reranker_profile_sha256: str = "",
) -> str:
    payload = {
        "idleSeconds": float(idle_seconds),
        "mineruEnabled": bool(mineru_enabled),
        "mineruPort": int(mineru_port),
        "mineruTimeoutSeconds": float(mineru_timeout_seconds),
        "root": normalized_knowledge_root(root),
        # A virtualenv's python is normally a symlink to the base interpreter.
        # Preserve that configured path so two runtimes with different optional
        # dependencies cannot be mistaken for the same worker identity.
        "pythonExecutable": str(Path(python_executable).expanduser().absolute()),
        "pythonVersion": str(python_version),
        "embeddingProvider": normalized_knowledge_embedding_provider(embedding_provider),
        "embeddingModel": str(embedding_model).strip(),
        "embeddingProfileSha256": str(embedding_profile_sha256).strip().lower(),
        "denseBackend": str(dense_backend).strip().lower(),
        "rerankerProvider": str(reranker_provider).strip().lower(),
        "rerankerModelPath": str(reranker_model_path).strip(),
        "rerankerModelRevision": str(reranker_model_revision).strip(),
        "rerankerProfileSha256": str(reranker_profile_sha256).strip().lower(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
