from __future__ import annotations

import hashlib
import json
import math
import os
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol


QWEN3_RERANKER_MODEL_ID = "mlx-community/Qwen3-Reranker-0.6B-4bit"
QWEN3_RERANKER_DEFAULT_INSTRUCTION = (
    "Given a Chinese knowledge-base query, retrieve passages that directly "
    "contain evidence needed to answer it"
)
_MODEL_MANIFEST_FILES = (
    "config.json",
    "generation_config.json",
    "model.safetensors",
    "model.safetensors.index.json",
    "tokenizer.json",
    "tokenizer_config.json",
)


class _PairScoreRuntime(Protocol):
    def score(self, query: str, document: str) -> float: ...

    def close_batch(self) -> None: ...


class KnowledgeReranker(Protocol):
    configured: bool

    def rerank(
        self,
        query: str,
        candidates: Sequence[Mapping[str, Any]],
        *,
        limit: int,
        candidate_limit: int = 100,
    ) -> list[dict[str, Any]]: ...

    def status(self) -> dict[str, Any]: ...


def knowledge_reranker_profile_sha256(
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    source = os.environ if environ is None else environ
    raw_path = str(source.get("RAG_IME_KNOWLEDGE_RERANK_MODEL_PATH", "")).strip()
    model_path = (
        str(Path(raw_path).expanduser().resolve(strict=False)) if raw_path else ""
    )
    payload = {
        "provider": str(
            source.get("RAG_IME_KNOWLEDGE_RERANK_PROVIDER", "none")
        ).strip().lower(),
        "modelPath": model_path,
        "modelRevision": str(
            source.get("RAG_IME_KNOWLEDGE_RERANK_MODEL_REVISION", "")
        ).strip(),
        "instruction": " ".join(
            str(
                source.get(
                    "RAG_IME_KNOWLEDGE_RERANK_INSTRUCTION",
                    QWEN3_RERANKER_DEFAULT_INSTRUCTION,
                )
            ).split()
        ),
        "maxLength": str(
            source.get("RAG_IME_KNOWLEDGE_RERANK_MAX_LENGTH", "1536")
        ).strip(),
    }
    return _sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def knowledge_reranker_from_env(
    root_dir: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> KnowledgeReranker | None:
    source = os.environ if environ is None else environ
    provider = str(source.get("RAG_IME_KNOWLEDGE_RERANK_PROVIDER", "none")).strip().lower()
    if provider in {"", "none", "disabled"}:
        return None
    if provider == "typesafe-jev":
        from .jev_rerank import JevKnowledgeReranker

        return JevKnowledgeReranker()
    if provider not in {"mlx-qwen3-reranker", "qwen3-reranker"}:
        raise ValueError(f"unsupported Knowledge reranker provider: {provider}")
    raw_model_path = str(source.get("RAG_IME_KNOWLEDGE_RERANK_MODEL_PATH", "")).strip()
    if not raw_model_path:
        raise ValueError(
            "RAG_IME_KNOWLEDGE_RERANK_MODEL_PATH is required for the MLX Qwen3 reranker"
        )
    raw_max_length = str(
        source.get("RAG_IME_KNOWLEDGE_RERANK_MAX_LENGTH", "1536")
    ).strip()
    try:
        max_length = int(raw_max_length)
    except ValueError as exc:
        raise ValueError("RAG_IME_KNOWLEDGE_RERANK_MAX_LENGTH must be an integer") from exc
    cache_value = str(source.get("RAG_IME_KNOWLEDGE_RERANK_CACHE_PATH", "")).strip()
    cache_path = (
        Path(cache_value).expanduser()
        if cache_value
        else Path(root_dir).expanduser() / "private" / "reranker-scores.json"
    )
    instruction = str(
        source.get(
            "RAG_IME_KNOWLEDGE_RERANK_INSTRUCTION",
            QWEN3_RERANKER_DEFAULT_INSTRUCTION,
        )
    )
    return MlxQwen3KnowledgeReranker(
        raw_model_path,
        model_revision=str(
            source.get("RAG_IME_KNOWLEDGE_RERANK_MODEL_REVISION", "")
        ),
        instruction=instruction,
        max_length=max_length,
        cache_path=cache_path,
    )


class MlxQwen3KnowledgeReranker:
    """Local Knowledge cross-encoder reranker with an observable identity.

    This owner is deliberately separate from the input-method candidate
    reranker and from Agent/subagent reasoning. It scores only a bounded set of
    query-passage pairs and never retrieves, mutates a base or judges an answer.
    """

    provider = "mlx-qwen3-reranker"

    def __init__(
        self,
        model_path: str | Path,
        *,
        model_revision: str = "",
        instruction: str = QWEN3_RERANKER_DEFAULT_INSTRUCTION,
        max_length: int = 1_536,
        cache_path: str | Path | None = None,
        runtime_factory: Callable[[Path, str, int], _PairScoreRuntime] | None = None,
    ) -> None:
        self.model_path = Path(model_path).expanduser().resolve(strict=False)
        self.model_revision = str(model_revision or "").strip()[:160]
        self.instruction = " ".join(str(instruction or "").split())[:500]
        if not self.instruction:
            raise ValueError("reranker instruction must not be empty")
        self.max_length = max(256, min(4_096, int(max_length)))
        self.configured = _model_ready(self.model_path)
        self.fingerprint = _model_fingerprint(
            self.model_path,
            model_revision=self.model_revision,
            instruction=self.instruction,
            max_length=self.max_length,
        )
        self.cache_path = (
            Path(cache_path).expanduser().resolve(strict=False)
            if cache_path is not None
            else None
        )
        self._runtime_factory = runtime_factory or _MlxQwen3PairScoreRuntime
        self._runtime: _PairScoreRuntime | None = None
        self._score_cache = self._load_score_cache()
        self._persistent_cache_loaded = len(self._score_cache)
        self._persistent_cache_writes = 0
        self._cache_dirty = False
        self._calls = 0
        self._scored_pairs = 0
        self._cache_hits = 0
        self._elapsed_seconds = 0.0
        self._error_count = 0
        self._last_error = ""

    @property
    def model_reference(self) -> str:
        suffix = f"@{self.model_revision}" if self.model_revision else ""
        return f"{QWEN3_RERANKER_MODEL_ID}{suffix}"

    def rerank(
        self,
        query: str,
        candidates: Sequence[Mapping[str, Any]],
        *,
        limit: int,
        candidate_limit: int = 100,
    ) -> list[dict[str, Any]]:
        normalized_query = " ".join(str(query or "").split())
        if not normalized_query:
            raise ValueError("reranker query must not be empty")
        if not self.configured:
            raise RuntimeError("MLX Qwen3 reranker model is not ready")
        bounded_candidate_limit = max(1, min(100, int(candidate_limit)))
        # The Tool-facing final depth remains capped separately.  The scorer
        # may return the entire bounded candidate set so the caller can apply
        # source diversity without losing lower-ranked unique documents.
        bounded_limit = max(1, min(100, int(limit), bounded_candidate_limit))
        unique: list[tuple[int, str, str, dict[str, Any]]] = []
        seen_ids: set[str] = set()
        for original_rank, candidate in enumerate(
            candidates[:bounded_candidate_limit],
            start=1,
        ):
            item = dict(candidate)
            item_id = _candidate_id(item)
            content = " ".join(str(item.get("content") or "").split())
            if not item_id or not content or item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            unique.append((original_rank, item_id, content, item))
        if not unique:
            return []

        started = time.monotonic()
        self._calls += 1
        try:
            runtime: _PairScoreRuntime | None = None
            scored: list[tuple[float, int, str, dict[str, Any]]] = []
            for original_rank, item_id, content, item in unique:
                cache_key = _pair_cache_key(
                    self.fingerprint,
                    normalized_query,
                    content,
                )
                score = self._score_cache.get(cache_key)
                if score is None:
                    runtime = runtime or self._load_runtime()
                    score = float(runtime.score(normalized_query, content))
                    if not math.isfinite(score):
                        raise RuntimeError("reranker returned a non-finite score")
                    score = max(0.0, min(1.0, score))
                    self._score_cache[cache_key] = score
                    self._scored_pairs += 1
                    self._cache_dirty = True
                else:
                    self._cache_hits += 1
                scored.append((score, original_rank, item_id, item))
            if runtime is not None:
                runtime.close_batch()
            self._persist_score_cache()
        except Exception as exc:
            self._error_count += 1
            self._last_error = f"{type(exc).__name__}: reranker call failed"
            raise
        finally:
            self._elapsed_seconds += time.monotonic() - started

        ranked = sorted(scored, key=lambda value: (-value[0], value[1], value[2]))
        output: list[dict[str, Any]] = []
        for rerank_rank, (score, original_rank, _item_id, item) in enumerate(
            ranked[:bounded_limit],
            start=1,
        ):
            item["rerankScore"] = round(score, 8)
            item["rerankOriginalRank"] = original_rank
            item["rerankRank"] = rerank_rank
            output.append(item)
        return output

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "modelReference": self.model_reference,
            "configured": self.configured,
            "fingerprint": self.fingerprint,
            "maxLength": self.max_length,
            "instructionSha256": _sha256(self.instruction.encode("utf-8")),
            "calls": self._calls,
            "scoredPairs": self._scored_pairs,
            "cacheHits": self._cache_hits,
            "scoreCacheEntries": len(self._score_cache),
            "persistentCacheEnabled": self.cache_path is not None,
            "persistentCacheLoadedEntries": self._persistent_cache_loaded,
            "persistentCacheWrites": self._persistent_cache_writes,
            "elapsedSeconds": round(self._elapsed_seconds, 3),
            "errorCount": self._error_count,
            "fallbackCount": 0,
            "lastError": self._last_error,
            "independentStage": True,
            "subagentSubstitute": False,
        }

    def _load_runtime(self) -> _PairScoreRuntime:
        if self._runtime is None:
            self._runtime = self._runtime_factory(
                self.model_path,
                self.instruction,
                self.max_length,
            )
        return self._runtime

    def _load_score_cache(self) -> dict[str, float]:
        if self.cache_path is None or not self.cache_path.is_file():
            return {}
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, Mapping) or payload.get("fingerprint") != self.fingerprint:
            return {}
        raw_scores = payload.get("scores")
        if not isinstance(raw_scores, Mapping):
            return {}
        scores: dict[str, float] = {}
        for raw_key, raw_score in raw_scores.items():
            key = str(raw_key)
            try:
                score = float(raw_score)
            except (TypeError, ValueError):
                continue
            if len(key) == 64 and all(character in "0123456789abcdef" for character in key) and math.isfinite(score):
                scores[key] = max(0.0, min(1.0, score))
        return scores

    def _persist_score_cache(self) -> None:
        if self.cache_path is None or not self._cache_dirty:
            return
        self.cache_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.cache_path.parent, 0o700)
        except OSError:
            pass
        payload = {
            "schemaVersion": "rag-ime.knowledge-reranker-score-cache.v1",
            "fingerprint": self.fingerprint,
            "scores": dict(sorted(self._score_cache.items())),
        }
        temporary = self.cache_path.with_suffix(
            f"{self.cache_path.suffix}.{os.getpid()}.tmp"
        )
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.cache_path)
        self._persistent_cache_writes += 1
        self._cache_dirty = False


class _MlxQwen3PairScoreRuntime:
    _PREFIX = (
        '<|im_start|>system\nJudge whether the Document meets the requirements '
        'based on the Query and the Instruct provided. Note that the answer can '
        'only be "yes" or "no".<|im_end|>\n<|im_start|>user\n'
    )
    _SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"

    def __init__(self, model_path: Path, instruction: str, max_length: int) -> None:
        try:
            import mlx.core as mx
            from mlx_lm import load
        except ImportError as exc:  # pragma: no cover - environment-specific
            raise RuntimeError(
                "MLX Knowledge reranking requires the 'rerank-mlx' optional dependencies"
            ) from exc
        model, tokenizer = load(str(model_path))
        tokenizer_backend = getattr(tokenizer, "_tokenizer", tokenizer)
        true_id = tokenizer_backend.convert_tokens_to_ids("yes")
        false_id = tokenizer_backend.convert_tokens_to_ids("no")
        if not isinstance(true_id, int) or not isinstance(false_id, int):
            raise RuntimeError("Qwen3 reranker tokenizer lacks yes/no token ids")
        self._mx = mx
        self._model = model
        self._tokenizer = tokenizer_backend
        self._instruction = instruction
        self._max_length = max_length
        self._true_id = true_id
        self._false_id = false_id
        self._prefix_ids = tokenizer_backend.encode(
            self._PREFIX,
            add_special_tokens=False,
        )
        self._suffix_ids = tokenizer_backend.encode(
            self._SUFFIX,
            add_special_tokens=False,
        )

    def score(self, query: str, document: str) -> float:
        content_prefix = (
            f"<Instruct>: {self._instruction}\n<Query>: {query}\n<Document>: "
        )
        fixed_ids = self._prefix_ids + self._tokenizer.encode(
            content_prefix,
            add_special_tokens=False,
        )
        available = self._max_length - len(fixed_ids) - len(self._suffix_ids)
        if available < 8:
            raise RuntimeError("reranker query/instruction exceeds max length")
        document_ids = self._tokenizer.encode(
            document,
            add_special_tokens=False,
        )[:available]
        input_ids = fixed_ids + document_ids + self._suffix_ids
        logits = self._model(self._mx.array([input_ids]))[:, -1, :]
        pair = self._mx.stack(
            [logits[0, self._false_id], logits[0, self._true_id]]
        )
        probabilities = self._mx.exp(pair - self._mx.logsumexp(pair))
        self._mx.eval(probabilities)
        return float(probabilities[1])

    def close_batch(self) -> None:
        self._mx.clear_cache()


def _candidate_id(candidate: Mapping[str, Any]) -> str:
    for key in ("chunkId", "externalDocumentId", "documentId", "id"):
        value = " ".join(str(candidate.get(key) or "").split())
        if value:
            return value[:512]
    return ""


def _model_ready(path: Path) -> bool:
    return path.is_dir() and all((path / name).is_file() for name in _MODEL_MANIFEST_FILES)


def _model_fingerprint(
    path: Path,
    *,
    model_revision: str,
    instruction: str,
    max_length: int,
) -> str:
    manifest: dict[str, Any] = {
        "provider": "mlx-qwen3-reranker",
        "model": QWEN3_RERANKER_MODEL_ID,
        "revision": model_revision,
        "instructionSha256": _sha256(instruction.encode("utf-8")),
        "maxLength": max_length,
        "files": [],
    }
    if path.is_dir():
        for name in _MODEL_MANIFEST_FILES:
            candidate = path / name
            if not candidate.is_file():
                continue
            manifest["files"].append(
                {
                    "name": name,
                    "bytes": candidate.stat().st_size,
                    "sha256": _file_sha256(candidate),
                }
            )
    encoded = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "mlx-qwen3-reranker:sha256:" + _sha256(encoded)


def _pair_cache_key(fingerprint: str, query: str, document: str) -> str:
    return _sha256(f"{fingerprint}\0{query}\0{document}".encode("utf-8"))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
