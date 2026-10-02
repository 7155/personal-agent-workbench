from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent_model_defaults import DEFAULT_AGENT_MODEL_ID
from .activity_timeline_evaluation import (
    LunaStructuredRun,
    load_luna_structured_run,
    run_luna_structured,
)
from .knowledge_library.graph_extractors import (
    DeterministicGraphExtractor,
    GraphExtraction,
    GraphExtractionInput,
    GraphExtractor,
    extraction_from_dict,
)


LUNA_GRAPH_MODEL = DEFAULT_AGENT_MODEL_ID
LUNA_GRAPH_MODEL_REFERENCE = f"openai-codex/{LUNA_GRAPH_MODEL}"
LUNA_GRAPH_THINKING = "max"
LUNA_GRAPH_PROMPT_VERSION = "rag-benchmark-luna-knowledge-graph-v1"
LUNA_GRAPH_SCHEMA_VERSION = "rag-ime.rag-benchmark-luna-graph.v1"
_PHASE = "knowledge-graph-extraction"


@dataclass(frozen=True)
class _LunaGraphConfig:
    model: str = LUNA_GRAPH_MODEL


class LunaKnowledgeGraphExtractor:
    """Benchmark-only evidence-grounded graph extractor using Codex Luna.

    Raw prompts and model outputs stay below the run-owned benchmark root.
    The Knowledge graph normalizer remains authoritative: unsupported entities,
    relations without exact evidence, and low-confidence relations are dropped.
    """

    mode = "model"

    def __init__(
        self,
        artifact_root: str | Path,
        *,
        codex_bin: str = "codex",
        timeout_seconds: float = 1_200.0,
        max_entities: int = 5,
        max_relations: int = 4,
        max_topics: int = 2,
        structured_runner: Callable[..., LunaStructuredRun] = run_luna_structured,
        structured_loader: Callable[..., LunaStructuredRun] = load_luna_structured_run,
    ) -> None:
        self.artifact_root = Path(artifact_root).expanduser().resolve(strict=False)
        self.codex_bin = str(codex_bin or "codex").strip() or "codex"
        self.timeout_seconds = max(1.0, min(3_600.0, float(timeout_seconds)))
        self.max_entities = max(1, min(8, int(max_entities)))
        self.max_relations = max(0, min(8, int(max_relations)))
        self.max_topics = max(0, min(4, int(max_topics)))
        self.config = _LunaGraphConfig()
        self.configured = _executable_available(self.codex_bin)
        self._structured_runner = structured_runner
        self._structured_loader = structured_loader
        public_config = {
            "promptVersion": LUNA_GRAPH_PROMPT_VERSION,
            "schemaVersion": LUNA_GRAPH_SCHEMA_VERSION,
            "model": LUNA_GRAPH_MODEL_REFERENCE,
            "thinking": LUNA_GRAPH_THINKING,
            "maxEntities": self.max_entities,
            "maxRelations": self.max_relations,
            "maxTopics": self.max_topics,
        }
        self.fingerprint = "luna:sha256:" + hashlib.sha256(
            json.dumps(public_config, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def extract(
        self,
        inputs: Sequence[GraphExtractionInput],
    ) -> dict[str, GraphExtraction]:
        values = tuple(inputs)
        if not values:
            return {}
        if not self.configured:
            raise RuntimeError("Luna knowledge graph extraction requires the configured codex executable")
        self.artifact_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.artifact_root.chmod(0o700)
        schema = _output_schema(
            values,
            max_entities=self.max_entities,
            max_relations=self.max_relations,
            max_topics=self.max_topics,
        )
        prompt = _prompt(
            values,
            max_entities=self.max_entities,
            max_relations=self.max_relations,
            max_topics=self.max_topics,
        )
        run = self._load_or_run(prompt=prompt, schema=schema)
        raw_chunks = run.output.get("chunks")
        if not isinstance(raw_chunks, list):
            raise RuntimeError("Luna knowledge graph output is missing chunks[]")
        by_id = {item.chunk_id: item for item in values}
        results: dict[str, GraphExtraction] = {}
        for raw_chunk in raw_chunks:
            if not isinstance(raw_chunk, dict):
                continue
            chunk_id = str(raw_chunk.get("chunkId") or "")
            source = by_id.get(chunk_id)
            if source is None or chunk_id in results:
                continue
            results[chunk_id] = extraction_from_dict(raw_chunk, source)
        return results

    def _load_or_run(
        self,
        *,
        prompt: str,
        schema: Mapping[str, object],
    ) -> LunaStructuredRun:
        schema_text = json.dumps(
            dict(schema),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        prompt_sha256 = _sha256(prompt)
        schema_sha256 = _sha256(schema_text)
        batch_sha256 = _sha256(f"{prompt_sha256}\0{schema_sha256}")
        base_name = f"batch-{batch_sha256[:32]}"
        for attempt in range(100):
            suffix = "" if attempt == 0 else f"-retry-{attempt:02d}"
            directory = self.artifact_root / f"{base_name}{suffix}"
            if directory.exists():
                try:
                    resumed = self._structured_loader(directory, phase=_PHASE)
                except (OSError, RuntimeError, ValueError):
                    continue
                if (
                    resumed.prompt_sha256 == prompt_sha256
                    and resumed.schema_sha256 == schema_sha256
                    and resumed.model == LUNA_GRAPH_MODEL
                    and resumed.thinking == LUNA_GRAPH_THINKING
                    and resumed.exit_code == 0
                ):
                    return resumed
                continue
            return self._structured_runner(
                prompt=prompt,
                schema=schema,
                artifact_dir=directory,
                phase=_PHASE,
                timeout_seconds=self.timeout_seconds,
                codex_bin=self.codex_bin,
            )
        raise RuntimeError("Luna knowledge graph extraction exhausted its retained retry slots")


def benchmark_graph_extractor_factory(
    artifact_root: str | Path,
    *,
    codex_bin: str = "codex",
    timeout_seconds: float = 1_200.0,
) -> Callable[..., GraphExtractor]:
    root = Path(artifact_root).expanduser().resolve(strict=False)

    def build(
        mode: str,
        *,
        model_id: str = "",
        extraction_concurrency: int = 2,
        max_entities: int = 5,
        max_relations: int = 4,
        max_topics: int = 2,
    ) -> GraphExtractor:
        del extraction_concurrency
        normalized_mode = str(mode or "deterministic").strip().lower()
        if normalized_mode == "deterministic":
            return DeterministicGraphExtractor()
        normalized_model = str(model_id or LUNA_GRAPH_MODEL).strip()
        if normalized_mode != "model" or normalized_model not in {
            LUNA_GRAPH_MODEL,
            LUNA_GRAPH_MODEL_REFERENCE,
        }:
            raise ValueError("benchmark model graph extraction is pinned to openai-codex/gpt-5.6-luna")
        return LunaKnowledgeGraphExtractor(
            root,
            codex_bin=codex_bin,
            timeout_seconds=timeout_seconds,
            max_entities=max_entities,
            max_relations=max_relations,
            max_topics=max_topics,
        )

    return build


def luna_graph_receipt_summary(artifact_root: str | Path) -> dict[str, Any]:
    root = Path(artifact_root).expanduser().resolve(strict=False)
    receipts: list[dict[str, Any]] = []
    if root.is_dir():
        for path in sorted(root.glob(f"batch-*/{_PHASE}-receipt.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                receipts.append(dict(payload))
    public_receipts = [
        {
            "model": str(item.get("model") or ""),
            "thinking": str(item.get("thinking") or ""),
            "exitCode": int(item.get("exitCode") or 0),
            "elapsedSeconds": float(item.get("elapsedSeconds") or 0.0),
            "promptSha256": str(item.get("promptSha256") or ""),
            "schemaSha256": str(item.get("schemaSha256") or ""),
            "outputSha256": str(item.get("outputSha256") or ""),
        }
        for item in receipts
    ]
    passed = bool(public_receipts) and all(
        item["model"] == LUNA_GRAPH_MODEL
        and item["thinking"] == LUNA_GRAPH_THINKING
        and item["exitCode"] == 0
        for item in public_receipts
    )
    receipt_set = json.dumps(
        public_receipts,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "modelReference": LUNA_GRAPH_MODEL_REFERENCE,
        "thinkingLevel": LUNA_GRAPH_THINKING,
        "requestCount": len(public_receipts),
        "elapsedSeconds": round(sum(item["elapsedSeconds"] for item in public_receipts), 3),
        "passed": passed,
        "receiptSetSha256": _sha256(receipt_set),
    }


def _prompt(
    inputs: Sequence[GraphExtractionInput],
    *,
    max_entities: int,
    max_relations: int,
    max_topics: int,
) -> str:
    chunks = [
        {
            "chunkId": item.chunk_id,
            "heading": item.heading[:300],
            "text": item.content[:6_000],
        }
        for item in inputs
    ]
    return (
        "你正在为独立的企业文档 Knowledge 系统构建可追溯知识图谱。"
        "只根据每个片段自身文字抽取，不使用个人 Memory，不补充常识，不建立跨片段的无证据关系。"
        "每个实体和关系都必须附带该片段中逐字出现的短证据；不确定就省略。"
        "关系的 source/target 必须同时出现在该片段的 entities 中，confidence 必须真实校准。"
        f"每片段最多 {max_topics} 个主题、{max_entities} 个实体、{max_relations} 条关系。"
        "必须为每个输入 chunkId 返回且只返回一个结果；无可靠实体时返回空数组。"
        "输出必须严格符合 JSON Schema，不要输出 Markdown。\n"
        f"协议：{LUNA_GRAPH_PROMPT_VERSION}\n"
        + json.dumps({"chunks": chunks}, ensure_ascii=False, separators=(",", ":"))
    )


def _output_schema(
    inputs: Sequence[GraphExtractionInput],
    *,
    max_entities: int,
    max_relations: int,
    max_topics: int,
) -> dict[str, Any]:
    chunk_ids = [item.chunk_id for item in inputs]
    entity = {
        "type": "object",
        "additionalProperties": False,
        "required": ["name", "type", "evidence"],
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 160},
            "type": {"type": "string", "minLength": 1, "maxLength": 80},
            "evidence": {"type": "string", "minLength": 1, "maxLength": 240},
        },
    }
    relation = {
        "type": "object",
        "additionalProperties": False,
        "required": ["source", "target", "type", "evidence", "confidence"],
        "properties": {
            "source": {"type": "string", "minLength": 1, "maxLength": 160},
            "target": {"type": "string", "minLength": 1, "maxLength": 160},
            "type": {"type": "string", "minLength": 1, "maxLength": 48},
            "evidence": {"type": "string", "minLength": 1, "maxLength": 240},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        },
    }
    chunk = {
        "type": "object",
        "additionalProperties": False,
        "required": ["chunkId", "topics", "entities", "relations"],
        "properties": {
            "chunkId": {"type": "string", "enum": chunk_ids},
            "topics": {
                "type": "array",
                "maxItems": max_topics,
                "items": {"type": "string", "minLength": 1, "maxLength": 160},
            },
            "entities": {"type": "array", "maxItems": max_entities, "items": entity},
            "relations": {"type": "array", "maxItems": max_relations, "items": relation},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["schemaVersion", "chunks"],
        "properties": {
            "schemaVersion": {
                "type": "string",
                "const": LUNA_GRAPH_SCHEMA_VERSION,
            },
            "chunks": {
                "type": "array",
                "minItems": len(chunk_ids),
                "maxItems": len(chunk_ids),
                "items": chunk,
            },
        },
    }


def _executable_available(value: str) -> bool:
    candidate = Path(value).expanduser()
    if candidate.parent != Path("."):
        return candidate.is_file() and os.access(candidate, os.X_OK)
    return shutil.which(value) is not None


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
