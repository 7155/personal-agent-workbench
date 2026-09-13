from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import shutil
import stat
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .embeddings import HashingEmbeddingProvider
from .knowledge_library import KnowledgeLibraryConfig, KnowledgeLibraryError, KnowledgeLibraryService
from .knowledge_library.dense import NullDenseIndex, SqliteDenseIndex
from .knowledge_library.rerank import KnowledgeReranker
from .rag_benchmark import compute_retrieval_metrics
from .rag_benchmark_luna_graph import (
    LUNA_GRAPH_MODEL,
    LUNA_GRAPH_MODEL_REFERENCE,
    LUNA_GRAPH_THINKING,
    benchmark_graph_extractor_factory,
    luna_graph_receipt_summary,
)


RAG_BENCHMARK_SANDBOX_SCHEMA_VERSION = "rag-ime.rag-benchmark-sandbox.v1"
RAG_BENCHMARK_TOOL_SCHEMA_VERSION = "rag-ime.rag-benchmark-tool.v1"
RUN_MARKER_NAME = ".rag-benchmark-run.json"
DELETE_CONFIRMATION = "DELETE_BENCHMARK_RUN"
REBUILD_CONFIRMATION = "REBUILD"

_RUN_ID = re.compile(r"^[a-f0-9]{32}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_OPERATIONS = (
    "create_run",
    "create_base",
    "import_documents",
    "configure_base",
    "rebuild_preview",
    "rebuild",
    "graph_rebuild",
    "search",
    "evaluate_validation",
    "status",
    "cleanup",
)


class RagBenchmarkSandboxError(ValueError):
    def __init__(self, message: str, *, code: str = "rag_benchmark_sandbox_error") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RagBenchmarkSandboxPolicy:
    """Hard budgets for one explicitly configured local benchmark root."""

    max_runs: int = 4
    max_bases_per_run: int = 8
    max_documents_per_run: int = 20_000
    max_documents_per_call: int = 256
    max_document_bytes: int = 4 * 1024 * 1024
    max_total_source_bytes: int = 2 * 1024 * 1024 * 1024
    max_search_calls: int = 50_000
    max_evaluations: int = 256
    max_rebuilds: int = 256

    def __post_init__(self) -> None:
        values = {
            "max_runs": self.max_runs,
            "max_bases_per_run": self.max_bases_per_run,
            "max_documents_per_run": self.max_documents_per_run,
            "max_documents_per_call": self.max_documents_per_call,
            "max_document_bytes": self.max_document_bytes,
            "max_total_source_bytes": self.max_total_source_bytes,
            "max_search_calls": self.max_search_calls,
            "max_evaluations": self.max_evaluations,
            "max_rebuilds": self.max_rebuilds,
        }
        for name, value in values.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_documents_per_call > self.max_documents_per_run:
            raise ValueError("max_documents_per_call may not exceed max_documents_per_run")
        if self.max_document_bytes > self.max_total_source_bytes:
            raise ValueError("max_document_bytes may not exceed max_total_source_bytes")

    def as_dict(self) -> dict[str, int]:
        return {
            "maxRuns": self.max_runs,
            "maxBasesPerRun": self.max_bases_per_run,
            "maxDocumentsPerRun": self.max_documents_per_run,
            "maxDocumentsPerCall": self.max_documents_per_call,
            "maxDocumentBytes": self.max_document_bytes,
            "maxTotalSourceBytes": self.max_total_source_bytes,
            "maxSearchCalls": self.max_search_calls,
            "maxEvaluations": self.max_evaluations,
            "maxRebuilds": self.max_rebuilds,
        }


KnowledgeServiceFactory = Callable[[Path, RagBenchmarkSandboxPolicy], KnowledgeLibraryService]


class RagBenchmarkSandbox:
    """Benchmark-only owner over isolated KnowledgeLibraryService instances.

    The manager accepts inline public fixture text only. It never accepts an
    arbitrary host path, never opens the production Knowledge root, and only
    removes a marker-bound child directory created by this instance contract.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        policy: RagBenchmarkSandboxPolicy | None = None,
        service_factory: KnowledgeServiceFactory | None = None,
        reranker: KnowledgeReranker | None = None,
        evaluation_suites: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
    ) -> None:
        requested = Path(root).expanduser()
        if requested.exists() and requested.is_symlink():
            raise RagBenchmarkSandboxError(
                "benchmark root may not be a symbolic link",
                code="unsafe_root",
            )
        resolved = requested.resolve(strict=False)
        broad_roots = {
            Path("/").resolve(),
            Path.home().resolve(strict=False),
            Path.cwd().resolve(strict=False),
        }
        if resolved in broad_roots:
            raise RagBenchmarkSandboxError(
                "benchmark root is too broad",
                code="unsafe_root",
            )
        resolved.mkdir(mode=0o700, parents=True, exist_ok=True)
        if resolved.is_symlink() or not resolved.is_dir():
            raise RagBenchmarkSandboxError(
                "benchmark root must be a real directory",
                code="unsafe_root",
            )
        os.chmod(resolved, 0o700)
        self.root = resolved
        self.policy = policy or RagBenchmarkSandboxPolicy()
        self._service_factory = service_factory or _default_service_factory
        self._reranker = reranker
        self._evaluation_suites = _normalize_evaluation_suites(evaluation_suites or {})
        self._services: dict[str, KnowledgeLibraryService] = {}
        self._lock = threading.RLock()
        self._closed = False

    def create_run(self, owner_session_id: object, *, label: object = "") -> dict[str, Any]:
        owner = _identifier(owner_session_id, "owner session id")
        clean_label = _text(label, "label", maximum=200, required=False)
        with self._lock:
            self._require_open()
            if self._active_run_count() >= self.policy.max_runs:
                raise _budget_error("active benchmark run limit reached")
            run_id = uuid.uuid4().hex
            run_path = self.root / f"run-{run_id}"
            run_path.mkdir(mode=0o700, exist_ok=False)
            manifest = {
                "schemaVersion": RAG_BENCHMARK_SANDBOX_SCHEMA_VERSION,
                "runId": run_id,
                "ownerSessionSha256": _sha256(owner),
                "label": clean_label,
                "createdAtMs": int(time.time() * 1_000),
                "state": "active",
                "bases": {},
                "documents": {},
                "usage": {
                    "sourceBytes": 0,
                    "searchCalls": 0,
                    "rerankCalls": 0,
                    "evaluationCalls": 0,
                    "rebuilds": 0,
                    "graphRebuilds": 0,
                },
            }
            try:
                _write_manifest(run_path, manifest)
            except Exception:
                shutil.rmtree(run_path, ignore_errors=True)
                raise
            return {
                "schemaVersion": RAG_BENCHMARK_SANDBOX_SCHEMA_VERSION,
                "runId": run_id,
                "label": clean_label,
                "state": "active",
                "limits": self.policy.as_dict(),
                "localOnly": True,
                "networkUploadAllowed": False,
            }

    def create_base(
        self,
        owner_session_id: object,
        run_id: object,
        *,
        alias: object,
        name: object,
        description: object = "",
        chunking_config: Mapping[str, object] | None = None,
        retrieval_config: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        clean_alias = _identifier(alias, "base alias")
        clean_name = _text(name, "base name", maximum=240)
        clean_description = _text(description, "description", maximum=4_000, required=False)
        with self._lock:
            run_path, manifest = self._load_owned_run(owner_session_id, run_id)
            bases = _manifest_mapping(manifest, "bases")
            if clean_alias in bases:
                raise RagBenchmarkSandboxError(
                    f"benchmark base alias {clean_alias!r} already exists",
                    code="duplicate_base",
                )
            if len(bases) >= self.policy.max_bases_per_run:
                raise _budget_error("knowledge base limit reached")
            service = self._service(str(manifest["runId"]), run_path)
            base = _knowledge_call(
                service.create_base,
                clean_name,
                description=clean_description,
                parser_mode="builtin",
                agent_enabled=True,
                chunking_config=dict(chunking_config) if chunking_config is not None else None,
                retrieval_config=dict(retrieval_config) if retrieval_config is not None else None,
            )
            bases[clean_alias] = {
                "kbId": str(base["id"]),
                "name": str(base["name"]),
            }
            _write_manifest(run_path, manifest)
            return _public_base(clean_alias, base)

    def import_documents(
        self,
        owner_session_id: object,
        run_id: object,
        *,
        base_alias: object,
        documents: object,
    ) -> dict[str, Any]:
        clean_alias = _identifier(base_alias, "base alias")
        if not isinstance(documents, list) or not documents:
            raise RagBenchmarkSandboxError(
                "documents must be a non-empty array",
                code="invalid_argument",
            )
        if len(documents) > self.policy.max_documents_per_call:
            raise _budget_error("document import batch limit reached")

        normalized = [_normalize_inline_document(item) for item in documents]
        if len({item["externalId"] for item in normalized}) != len(normalized):
            raise RagBenchmarkSandboxError(
                "document batch contains duplicate external IDs",
                code="duplicate_document",
            )
        with self._lock:
            run_path, manifest = self._load_owned_run(owner_session_id, run_id)
            base = self._base_record(manifest, clean_alias)
            known_documents = _manifest_mapping(manifest, "documents")
            for item in normalized:
                if item["externalId"] in known_documents:
                    raise RagBenchmarkSandboxError(
                        f"external document {item['externalId']!r} already exists",
                        code="duplicate_document",
                    )
                if item["byteSize"] > self.policy.max_document_bytes:
                    raise _budget_error("document exceeds the per-document byte limit")
            if len(known_documents) + len(normalized) > self.policy.max_documents_per_run:
                raise _budget_error("document limit reached")
            usage = _manifest_mapping(manifest, "usage")
            incoming_bytes = sum(int(item["byteSize"]) for item in normalized)
            if int(usage.get("sourceBytes") or 0) + incoming_bytes > self.policy.max_total_source_bytes:
                raise _budget_error("benchmark source byte limit reached")

            service = self._service(str(manifest["runId"]), run_path)
            source_root = run_path / "sources" / clean_alias
            source_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            results: list[dict[str, Any]] = []
            for item in normalized:
                suffix = ".md" if str(item["name"]).lower().endswith((".md", ".markdown")) else ".txt"
                source_name = f"{_sha256(str(item['externalId']))[:24]}{suffix}"
                source_path = source_root / source_name
                _atomic_write_bytes(source_path, item["text"].encode("utf-8"))
                imported = _knowledge_call(
                    service.import_document,
                    str(base["kbId"]),
                    source_path,
                    display_name=str(item["name"]),
                    mime_type=str(item["mimeType"]),
                    parser_mode="builtin",
                )
                if str(imported.get("status") or "") != "ready":
                    raise RagBenchmarkSandboxError(
                        f"benchmark document {item['externalId']!r} did not become ready",
                        code="document_not_ready",
                    )
                record = {
                    "documentId": str(imported["documentId"]),
                    "baseAlias": clean_alias,
                    "name": str(item["name"]),
                    "byteSize": int(item["byteSize"]),
                    "sha256": str(item["sha256"]),
                }
                known_documents[str(item["externalId"])] = record
                usage["sourceBytes"] = int(usage.get("sourceBytes") or 0) + int(item["byteSize"])
                _write_manifest(run_path, manifest)
                results.append(_public_document(str(item["externalId"]), imported))
            return {
                "schemaVersion": RAG_BENCHMARK_SANDBOX_SCHEMA_VERSION,
                "runId": str(manifest["runId"]),
                "baseAlias": clean_alias,
                "importedCount": len(results),
                "documents": results,
                "usage": dict(usage),
            }

    def configure_base(
        self,
        owner_session_id: object,
        run_id: object,
        *,
        base_alias: object,
        expected_revision: object,
        chunking_config: Mapping[str, object] | None = None,
        retrieval_config: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        clean_alias = _identifier(base_alias, "base alias")
        revision = _positive_integer(expected_revision, "expected revision")
        if chunking_config is None and retrieval_config is None:
            raise RagBenchmarkSandboxError(
                "configure_base requires chunkingConfig or retrievalConfig",
                code="invalid_argument",
            )
        with self._lock:
            run_path, manifest = self._load_owned_run(owner_session_id, run_id)
            base_record = self._base_record(manifest, clean_alias)
            service = self._service(str(manifest["runId"]), run_path)
            updated = _knowledge_call(
                service.update_base,
                str(base_record["kbId"]),
                chunking_config=dict(chunking_config) if chunking_config is not None else None,
                retrieval_config=dict(retrieval_config) if retrieval_config is not None else None,
                expected_revision=revision,
            )
            _write_manifest(run_path, manifest)
            return _public_base(clean_alias, updated)

    def rebuild_preview(
        self,
        owner_session_id: object,
        run_id: object,
        *,
        base_alias: object,
    ) -> dict[str, Any]:
        clean_alias = _identifier(base_alias, "base alias")
        with self._lock:
            run_path, manifest = self._load_owned_run(owner_session_id, run_id)
            base = self._base_record(manifest, clean_alias)
            service = self._service(str(manifest["runId"]), run_path)
            preview = _knowledge_call(service.reindex_preview, str(base["kbId"]))
            reverse_documents = _reverse_document_ids(manifest)
            return {
                "schemaVersion": RAG_BENCHMARK_SANDBOX_SCHEMA_VERSION,
                "runId": str(manifest["runId"]),
                "baseAlias": clean_alias,
                "configRevision": int(preview["configRevision"]),
                "chunkingConfig": dict(preview["chunkingConfig"]),
                "retrievalConfig": dict(preview["retrievalConfig"]),
                "documentCount": int(preview["documentCount"]),
                "rebuildCandidateCount": int(preview["rebuildCandidateCount"]),
                "staleDocumentCount": int(preview["staleDocumentCount"]),
                "estimatedSourceBytes": int(preview["estimatedSourceBytes"]),
                "previewToken": str(preview["previewToken"]),
                "documents": [
                    {
                        "externalDocumentId": reverse_documents.get(str(item["fileId"]), ""),
                        "name": str(item["fileName"]),
                        "status": str(item["status"]),
                    }
                    for item in preview["documents"]
                ],
            }

    def rebuild(
        self,
        owner_session_id: object,
        run_id: object,
        *,
        base_alias: object,
        preview_token: object,
        expected_revision: object,
        confirm_text: object,
    ) -> dict[str, Any]:
        clean_alias = _identifier(base_alias, "base alias")
        clean_preview = _text(preview_token, "preview token", maximum=256)
        revision = _positive_integer(expected_revision, "expected revision")
        if str(confirm_text or "") != REBUILD_CONFIRMATION:
            raise RagBenchmarkSandboxError(
                f"confirmText must equal {REBUILD_CONFIRMATION}",
                code="confirmation_required",
            )
        with self._lock:
            run_path, manifest = self._load_owned_run(owner_session_id, run_id)
            usage = _manifest_mapping(manifest, "usage")
            if _total_rebuilds(usage) >= self.policy.max_rebuilds:
                raise _budget_error("rebuild limit reached")
            usage["rebuilds"] = int(usage.get("rebuilds") or 0) + 1
            _write_manifest(run_path, manifest)
            base = self._base_record(manifest, clean_alias)
            service = self._service(str(manifest["runId"]), run_path)
            rebuilt = _knowledge_call(
                service.rebuild_base,
                str(base["kbId"]),
                preview_token=clean_preview,
                expected_revision=revision,
                confirm_text=REBUILD_CONFIRMATION,
            )
            return {
                "schemaVersion": RAG_BENCHMARK_SANDBOX_SCHEMA_VERSION,
                "runId": str(manifest["runId"]),
                "baseAlias": clean_alias,
                "configRevision": int(rebuilt["configRevision"]),
                "requested": int(rebuilt["requested"]),
                "ready": int(rebuilt["ready"]),
                "failed": int(rebuilt["failed"]),
                "queued": int(rebuilt["queued"]),
            }

    def export_search_snapshot(self, owner_session_id: object, run_id: object, *, base_alias: object) -> dict[str, Any]:
        """Read-only export of the caller-owned ready index for App packaging."""
        clean_alias = _identifier(base_alias, "base alias")
        with self._lock:
            run_path, manifest = self._load_owned_run(owner_session_id, run_id)
            base = self._base_record(manifest, clean_alias)
            service = self._service(str(manifest["runId"]), run_path)
            snapshot = _knowledge_call(service.store.export_search_snapshot, str(base["kbId"]))
            snapshot["externalDocumentIds"] = _reverse_document_ids(manifest)
            return snapshot

    def export_dense_snapshot(self, owner_session_id: object, run_id: object, *, base_alias: object) -> dict[str, Any]:
        """Export the actual owned projection; this never embeds documents."""
        from .embeddings import portable_embedding_configuration
        with self._lock:
            run_path, manifest = self._load_owned_run(owner_session_id, run_id)
            base = self._base_record(manifest, _identifier(base_alias, "base alias"))
            service = self._service(str(manifest["runId"]), run_path)
            export = getattr(service.dense_index, "export_snapshot", None)
            if not callable(export):
                raise RagBenchmarkSandboxError("owned index has no exportable dense projection")
            search = service.store.export_search_snapshot(str(base["kbId"]))
            configuration = portable_embedding_configuration(service.dense_index.provider)
            snapshot = export(base_id=str(base["kbId"]),
                document_hashes={d["id"]: d["sha256"] for d in search["documents"]},
                provider_config=configuration, model_revision=configuration["modelRevision"])
            if {c["chunkId"] for c in snapshot["chunks"]} != {c["id"] for c in search["chunks"]}:
                raise RagBenchmarkSandboxError("dense projection is incomplete for the frozen search snapshot")
            snapshot["provider"]["dimensions"] = snapshot["dimension"]
            return snapshot

    def restore_search_snapshot(self, owner_session_id: object, run_id: object, *, base_alias: object,
                                snapshot: dict[str, Any], external_document_ids: Mapping[str, str],
                                dense_snapshot: dict[str, Any] | None = None,
                                cancelled: Callable[[], bool] = lambda: False) -> dict[str, Any]:
        """Restore derived Knowledge caches into an empty caller-owned run."""
        clean_alias = _identifier(base_alias, "base alias")
        with self._lock:
            run_path, manifest = self._load_owned_run(owner_session_id, run_id)
            if manifest["bases"] or manifest["documents"]:
                raise RagBenchmarkSandboxError("snapshot restoration requires an empty owned run")
            documents = snapshot["documents"]
            if (len(documents) > self.policy.max_documents_per_run
                    or sum(d["byteSize"] for d in documents) > self.policy.max_total_source_bytes
                    or any(d["byteSize"] > self.policy.max_document_bytes for d in documents)
                    or set(external_document_ids) != {d["id"] for d in documents}
                    or len(set(external_document_ids.values())) != len(documents)):
                raise RagBenchmarkSandboxError("snapshot source mapping or run budget is invalid")
            service = self._service(str(manifest["runId"]), run_path)
            if cancelled():
                raise InterruptedError("snapshot restoration was cancelled")
            service.store.import_search_snapshot(snapshot)
            if dense_snapshot is not None:
                importer = getattr(service.dense_index, "import_snapshot", None)
                if not callable(importer):
                    raise RagBenchmarkSandboxError("owned run has no compatible dense projection owner")
                importer(dense_snapshot, expected_base_id=snapshot["base"]["id"],
                    expected_document_hashes={d["id"]: d["sha256"] for d in documents},
                    expected_fingerprint=dense_snapshot["fingerprint"], expected_model_revision=dense_snapshot["modelRevision"],
                    cancelled=cancelled)
            if cancelled():
                raise InterruptedError("snapshot restoration was cancelled")
            # Only a complete import becomes a visible base in the run manifest.
            manifest["bases"][clean_alias] = {"kbId": snapshot["base"]["id"], "name": snapshot["base"]["name"]}
            manifest["documents"] = {external_document_ids[d["id"]]: {"documentId": d["id"], "baseAlias": clean_alias,
                "name": d["title"], "byteSize": d["byteSize"], "sha256": d["sha256"]} for d in documents}
            manifest["usage"]["sourceBytes"] = sum(d["byteSize"] for d in documents)
            _write_manifest(run_path, manifest)
            return {"runId": manifest["runId"], "documentCount": len(documents), "chunkCount": len(snapshot["chunks"]),
                    "embeddingModelCalls": 0, "restored": True}

    def rebuild_graph(
        self,
        owner_session_id: object,
        run_id: object,
        *,
        base_alias: object,
        expected_revision: object,
        extractor_mode: object = "luna",
        batch_size: object = 8,
        extraction_concurrency: object = 4,
        max_entities: object = 5,
        max_relations: object = 4,
        max_topics: object = 2,
    ) -> dict[str, Any]:
        """Rebuild only the isolated Knowledge graph, never personal Memory."""

        clean_alias = _identifier(base_alias, "base alias")
        revision = _bounded_integer(
            expected_revision,
            "expected graph revision",
            minimum=0,
            maximum=2_147_483_647,
        )
        requested_mode = str(extractor_mode or "luna").strip().lower()
        if requested_mode not in {"luna", "deterministic"}:
            raise RagBenchmarkSandboxError(
                "extractorMode must be luna or deterministic",
                code="invalid_argument",
            )
        clean_batch_size = _bounded_integer(batch_size, "batchSize", minimum=1, maximum=8)
        clean_concurrency = _bounded_integer(
            extraction_concurrency,
            "extractionConcurrency",
            minimum=1,
            maximum=4,
        )
        clean_max_entities = _bounded_integer(max_entities, "maxEntities", minimum=1, maximum=8)
        clean_max_relations = _bounded_integer(max_relations, "maxRelations", minimum=0, maximum=8)
        clean_max_topics = _bounded_integer(max_topics, "maxTopics", minimum=0, maximum=4)
        with self._lock:
            run_path, manifest = self._load_owned_run(owner_session_id, run_id)
            usage = _manifest_mapping(manifest, "usage")
            if _total_rebuilds(usage) >= self.policy.max_rebuilds:
                raise _budget_error("rebuild limit reached")
            usage["graphRebuilds"] = int(usage.get("graphRebuilds") or 0) + 1
            _write_manifest(run_path, manifest)
            base = self._base_record(manifest, clean_alias)
            service = self._service(str(manifest["runId"]), run_path)
            rebuilt = _knowledge_call(
                service.rebuild_knowledge_graph,
                str(base["kbId"]),
                expected_revision=revision,
                extractor_mode="model" if requested_mode == "luna" else "deterministic",
                model_id=LUNA_GRAPH_MODEL if requested_mode == "luna" else "",
                batch_size=clean_batch_size,
                extraction_concurrency=clean_concurrency,
                max_entities=clean_max_entities,
                max_relations=clean_max_relations,
                max_topics=clean_max_topics,
            )
            graph = _knowledge_call(
                service.knowledge_graph,
                str(base["kbId"]),
                limit=10,
                exclude_chunks=True,
            )
            extractor = dict(graph["extractor"]) if isinstance(graph.get("extractor"), Mapping) else {}
            result = {
                "schemaVersion": RAG_BENCHMARK_SANDBOX_SCHEMA_VERSION,
                "runId": str(manifest["runId"]),
                "baseAlias": clean_alias,
                "jobId": str(rebuilt.get("jobId") or graph.get("jobId") or ""),
                "status": str(graph.get("status") or rebuilt.get("status") or ""),
                "revision": int(graph.get("revision") or rebuilt.get("revision") or 0),
                "sourceRevision": str(graph.get("sourceRevision") or ""),
                "stats": dict(graph["stats"]) if isinstance(graph.get("stats"), Mapping) else {},
                "extractor": extractor,
                "requestedExtractor": {
                    "mode": requested_mode,
                    "modelReference": LUNA_GRAPH_MODEL_REFERENCE if requested_mode == "luna" else "",
                    "thinkingLevel": LUNA_GRAPH_THINKING if requested_mode == "luna" else "",
                    "batchSize": clean_batch_size,
                    "extractionConcurrency": clean_concurrency,
                    "maxEntities": clean_max_entities,
                    "maxRelations": clean_max_relations,
                    "maxTopics": clean_max_topics,
                },
                "knowledgeOnly": True,
                "memoryMutationPerformed": False,
                "usage": dict(usage),
            }
            if requested_mode == "luna":
                receipts = luna_graph_receipt_summary(run_path / "luna-knowledge-graph")
                result["lunaReceipts"] = receipts
                result["lunaExtractionPassed"] = (
                    result["status"] == "ready"
                    and receipts["passed"] is True
                    and extractor.get("configured") is True
                    and int(extractor.get("modelChunkCount") or 0) > 0
                    and int(extractor.get("fallbackChunkCount") or 0) == 0
                    and int(extractor.get("errorCount") or 0) == 0
                    and str(extractor.get("model") or "") == LUNA_GRAPH_MODEL
                )
            return result

    def search(
        self,
        owner_session_id: object,
        run_id: object,
        *,
        base_alias: object,
        query: object,
        top_k: object = 10,
        mode: object = "hybrid",
        threshold: object = 0.0,
        rerank: object = False,
        rerank_candidate_depth: object = 20,
    ) -> dict[str, Any]:
        clean_alias = _identifier(base_alias, "base alias")
        clean_query = _text(query, "query", maximum=20_000)
        limit = _bounded_integer(top_k, "topK", minimum=1, maximum=100)
        clean_mode = str(mode or "hybrid").strip().lower()
        if clean_mode not in {"lexical", "dense", "hybrid"}:
            raise RagBenchmarkSandboxError(
                "mode must be lexical, dense, or hybrid",
                code="invalid_argument",
            )
        clean_threshold = _finite_number(threshold, "threshold", minimum=0.0, maximum=1.0)
        if not isinstance(rerank, bool):
            raise RagBenchmarkSandboxError(
                "rerank must be a boolean",
                code="invalid_argument",
            )
        rerank_enabled = rerank
        candidate_depth = limit
        if rerank_enabled:
            if limit > 20:
                raise RagBenchmarkSandboxError(
                    "reranked topK may not exceed 20",
                    code="invalid_argument",
                )
            candidate_depth = _bounded_integer(
                rerank_candidate_depth,
                "rerankCandidateDepth",
                minimum=max(2, limit),
                maximum=100,
            )
            if self._reranker is None or not self._reranker.configured:
                raise RagBenchmarkSandboxError(
                    "independent Knowledge reranker is not configured",
                    code="reranker_unavailable",
                )
        with self._lock:
            run_path, manifest = self._load_owned_run(owner_session_id, run_id)
            usage = _manifest_mapping(manifest, "usage")
            if int(usage.get("searchCalls") or 0) >= self.policy.max_search_calls:
                raise _budget_error("search call limit reached")
            usage["searchCalls"] = int(usage.get("searchCalls") or 0) + 1
            if rerank_enabled:
                usage["rerankCalls"] = int(usage.get("rerankCalls") or 0) + 1
            _write_manifest(run_path, manifest)
            base = self._base_record(manifest, clean_alias)
            service = self._service(str(manifest["runId"]), run_path)
            result = _knowledge_call(
                service.search,
                clean_query,
                base_ids=(str(base["kbId"]),),
                limit=candidate_depth,
                mode=clean_mode,
                threshold=clean_threshold,
            )
            reverse_documents = _reverse_document_ids(manifest)
            hits: list[dict[str, Any]] = []
            for hit in list(result.get("hits") or []):
                internal_document_id = str(hit.get("documentId") or "")
                external_document_id = reverse_documents.get(internal_document_id)
                if not external_document_id:
                    raise RagBenchmarkSandboxError(
                        "search returned a document outside the benchmark manifest",
                        code="integrity_error",
                    )
                citation_ref = _citation_ref(external_document_id)
                citation = hit.get("citation") if isinstance(hit.get("citation"), Mapping) else {}
                projected_hit = {
                    "externalDocumentId": external_document_id,
                    "citationRef": citation_ref,
                    "chunkId": str(hit.get("chunkId") or ""),
                    "documentName": str(hit.get("documentName") or ""),
                    "ordinal": int(hit.get("ordinal") or 0),
                    "content": str(hit.get("content") or ""),
                    "citation": {
                        "externalDocumentId": external_document_id,
                        "citationRef": citation_ref,
                        "documentName": str(citation.get("documentName") or ""),
                        "page": citation.get("page"),
                        "heading": citation.get("heading"),
                        "chunkId": str(citation.get("chunkId") or ""),
                    },
                }
                score = _optional_search_score(hit)
                if score is not None:
                    projected_hit["score"] = score
                hits.append(projected_hit)
            rerank_receipt: dict[str, Any] = {
                "enabled": False,
                "independentStage": True,
                "subagentSubstitute": False,
            }
            if rerank_enabled:
                assert self._reranker is not None
                reranked_hits = self._reranker.rerank(
                    clean_query,
                    hits,
                    limit=candidate_depth,
                    candidate_limit=candidate_depth,
                )
                hits, duplicate_document_hits = _diversify_reranked_hits(
                    reranked_hits,
                    limit=limit,
                )
                rerank_receipt = {
                    **self._reranker.status(),
                    "enabled": True,
                    "candidateDepth": candidate_depth,
                    "finalDepth": limit,
                    "preDiversityCount": len(reranked_hits),
                    "diversityKey": "externalDocumentId",
                    "duplicateDocumentHitsDropped": duplicate_document_hits,
                }
            retrieval = result.get("retrieval") if isinstance(result.get("retrieval"), Mapping) else {}
            libraries = list(retrieval.get("libraries") or [])
            library = libraries[0] if libraries and isinstance(libraries[0], Mapping) else {}
            return {
                "schemaVersion": RAG_BENCHMARK_SANDBOX_SCHEMA_VERSION,
                "runId": str(manifest["runId"]),
                "baseAlias": clean_alias,
                "query": clean_query,
                "hits": hits,
                "total": len(hits),
                "retrieval": {
                    "mode": str(retrieval.get("mode") or clean_mode),
                    "effectiveMode": str(retrieval.get("effectiveMode") or ""),
                    "config": dict(retrieval["config"]) if isinstance(retrieval.get("config"), Mapping) else None,
                    "candidateLimit": int(library.get("candidateLimit") or 0),
                    "lexicalCandidates": int(library.get("lexicalCandidates") or 0),
                    "denseCandidates": int(library.get("denseCandidates") or 0),
                    "graphCandidates": int(library.get("graphCandidates") or 0),
                    "graphStatus": str(library.get("graphStatus") or ""),
                    "graphRelationRanking": (
                        dict(library["graphRelationRanking"])
                        if isinstance(library.get("graphRelationRanking"), Mapping)
                        else None
                    ),
                    "rerank": rerank_receipt,
                },
                "usage": dict(usage),
            }

    def evaluate_validation(
        self,
        owner_session_id: object,
        run_id: object,
        *,
        base_alias: object,
        evaluation_suite_id: object,
        top_k: object = 10,
        mode: object = "hybrid",
        threshold: object = 0.0,
        rerank: object = False,
        rerank_candidate_depth: object = 20,
    ) -> dict[str, Any]:
        """Score one host-registered validation suite without disclosing qrels.

        The Agent receives only aggregate metrics and immutable suite hashes.
        Held-out cases are rejected during sandbox construction and therefore
        cannot be queried through this Tool while tuning.
        """

        clean_alias = _identifier(base_alias, "base alias")
        suite_id = _identifier(evaluation_suite_id, "evaluation suite id")
        limit = _bounded_integer(top_k, "topK", minimum=1, maximum=20)
        suite = self._evaluation_suites.get(suite_id)
        if suite is None:
            raise RagBenchmarkSandboxError(
                "validation evaluation suite is unavailable",
                code="not_found",
            )
        cases = [dict(item) for item in suite["cases"]]
        started = time.monotonic()
        with self._lock:
            run_path, manifest = self._load_owned_run(owner_session_id, run_id)
            self._base_record(manifest, clean_alias)
            documents = set(_manifest_mapping(manifest, "documents"))
            missing_documents = sorted(
                {
                    document_id
                    for case in cases
                    for document_id in dict(case["relevant"])
                    if document_id not in documents
                }
            )
            if missing_documents:
                raise RagBenchmarkSandboxError(
                    "validation qrels reference documents not imported into this run",
                    code="integrity_error",
                )
            usage = _manifest_mapping(manifest, "usage")
            if int(usage.get("evaluationCalls") or 0) >= self.policy.max_evaluations:
                raise _budget_error("validation evaluation limit reached")
            if int(usage.get("searchCalls") or 0) + len(cases) > self.policy.max_search_calls:
                raise _budget_error("validation evaluation exceeds the remaining search budget")
            usage["evaluationCalls"] = int(usage.get("evaluationCalls") or 0) + 1
            _write_manifest(run_path, manifest)

            metric_cases: list[dict[str, object]] = []
            for case in cases:
                result = self.search(
                    owner_session_id,
                    run_id,
                    base_alias=clean_alias,
                    query=case["query"],
                    top_k=limit,
                    mode=mode,
                    threshold=threshold,
                    rerank=rerank,
                    rerank_candidate_depth=rerank_candidate_depth,
                )
                retrieved = list(
                    dict.fromkeys(
                        str(hit.get("externalDocumentId") or "")
                        for hit in result["hits"]
                        if str(hit.get("externalDocumentId") or "")
                    )
                )
                metric_cases.append(
                    {
                        "queryId": str(case["caseId"]),
                        "relevant": dict(case["relevant"]),
                        "retrieved": retrieved,
                    }
                )

            metric_k_values = tuple(
                sorted({value for value in (1, 3, 5, 10, limit) if value <= limit})
            )
            scored = compute_retrieval_metrics(
                metric_cases,
                k_values=metric_k_values,
            )
            _, latest_manifest = self._load_owned_run(owner_session_id, run_id)
            result = {
                "schemaVersion": "rag-ime.rag-benchmark-validation-evaluation.v1",
                "runId": str(latest_manifest["runId"]),
                "baseAlias": clean_alias,
                "evaluationSuiteId": suite_id,
                "evaluationSuiteSha256": str(suite["suiteSha256"]),
                "caseIdsSha256": str(suite["caseIdsSha256"]),
                "split": "validation",
                "caseCount": len(cases),
                "kValues": list(scored["kValues"]),
                "metrics": dict(scored["metrics"]),
                "retrieval": {
                    "topK": limit,
                    "mode": str(mode or "hybrid").strip().lower(),
                    "threshold": float(threshold),
                    "rerank": rerank is True,
                    "rerankCandidateDepth": (
                        int(rerank_candidate_depth) if rerank is True else 0
                    ),
                },
                "qrelsVisibleToAgent": False,
                "perCaseResultsVisible": False,
                "heldOutLabelsObserved": False,
                "localOnly": True,
                "uploaded": False,
                "elapsedMs": round((time.monotonic() - started) * 1_000, 3),
                "usage": dict(_manifest_mapping(latest_manifest, "usage")),
            }
            result["evaluationReceiptSha256"] = _sha256(
                json.dumps(
                    result,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return result

    def status(self, owner_session_id: object, run_id: object) -> dict[str, Any]:
        with self._lock:
            run_path, manifest = self._load_owned_run(owner_session_id, run_id)
            service = self._service(str(manifest["runId"]), run_path)
            bases: list[dict[str, Any]] = []
            for alias, record in sorted(_manifest_mapping(manifest, "bases").items()):
                base = _knowledge_call(service.get_base, str(record["kbId"]))
                public_base = _public_base(alias, base)
                graph = _knowledge_call(
                    service.knowledge_graph,
                    str(record["kbId"]),
                    limit=10,
                    exclude_chunks=True,
                )
                public_base["knowledgeGraph"] = {
                    "status": str(graph.get("status") or ""),
                    "revision": int(graph.get("revision") or 0),
                    "sourceRevision": str(graph.get("sourceRevision") or ""),
                    "stats": dict(graph["stats"]) if isinstance(graph.get("stats"), Mapping) else {},
                    "extractor": (
                        dict(graph["extractor"])
                        if isinstance(graph.get("extractor"), Mapping)
                        else {}
                    ),
                }
                bases.append(public_base)
            service_status = service.status()
            dense = service_status.get("dense") if isinstance(service_status.get("dense"), Mapping) else {}
            return {
                "schemaVersion": RAG_BENCHMARK_SANDBOX_SCHEMA_VERSION,
                "runId": str(manifest["runId"]),
                "label": str(manifest.get("label") or ""),
                "state": str(manifest.get("state") or "active"),
                "localOnly": True,
                "networkUploadAllowed": False,
                "baseCount": len(bases),
                "documentCount": len(_manifest_mapping(manifest, "documents")),
                "bases": bases,
                "usage": dict(_manifest_mapping(manifest, "usage")),
                "limits": self.policy.as_dict(),
                "dense": {
                    "available": dense.get("available") is True,
                    "degraded": dense.get("degraded") is True,
                    "kind": str(dense.get("kind") or ""),
                    "backend": str(dense.get("backend") or ""),
                    "ann": dense.get("ann") is True,
                    "scalable": dense.get("scalable") is True,
                    "provider": dict(dense["provider"]) if isinstance(dense.get("provider"), Mapping) else {},
                    "vectorCount": int(dense.get("vectorCount") or 0),
                    "indexCount": int(dense.get("indexCount") or 0),
                    "mappedVectorCount": int(dense.get("mappedVectorCount") or 0),
                    "projectionConsistent": dense.get("projectionConsistent") is True,
                    "staleIndexCount": int(dense.get("staleIndexCount") or 0),
                    "fallbackFrom": str(dense.get("fallbackFrom") or ""),
                    "reason": str(dense.get("reason") or ""),
                    "benchmarkDeterministicExact": dense.get("benchmarkDeterministicExact") is True,
                    "benchmarkMatrixCached": dense.get("benchmarkMatrixCached") is True,
                    "benchmarkQueryCacheEntries": int(dense.get("benchmarkQueryCacheEntries") or 0),
                    "benchmarkNumpyAcceleration": dense.get("benchmarkNumpyAcceleration") is True,
                },
                "reranker": (
                    self._reranker.status()
                    if self._reranker is not None
                    else {
                        "provider": "none",
                        "configured": False,
                        "independentStage": True,
                        "subagentSubstitute": False,
                    }
                ),
            }

    def cleanup(
        self,
        owner_session_id: object,
        run_id: object,
        *,
        confirm_text: object,
    ) -> dict[str, Any]:
        if str(confirm_text or "") != DELETE_CONFIRMATION:
            raise RagBenchmarkSandboxError(
                f"confirmText must equal {DELETE_CONFIRMATION}",
                code="confirmation_required",
            )
        with self._lock:
            run_path, manifest = self._load_owned_run(owner_session_id, run_id)
            clean_run_id = str(manifest["runId"])
            expected = self.root / f"run-{clean_run_id}"
            if run_path != expected or run_path.resolve(strict=True).parent != self.root:
                raise RagBenchmarkSandboxError(
                    "benchmark run path no longer matches its marker",
                    code="marker_mismatch",
                )
            service = self._services.pop(clean_run_id, None)
            if service is not None:
                service.close()
            cleanup_error: OSError | None = None
            for attempt in range(3):
                try:
                    shutil.rmtree(run_path)
                except FileNotFoundError:
                    break
                except OSError as exc:
                    cleanup_error = exc
                    if exc.errno != errno.ENOTEMPTY or attempt == 2:
                        raise
                    time.sleep(0.01)
                else:
                    break
            if run_path.exists():
                raise RagBenchmarkSandboxError(
                    "benchmark run cleanup did not remove its marker-bound directory",
                    code="cleanup_failed",
                ) from cleanup_error
            return {
                "schemaVersion": RAG_BENCHMARK_SANDBOX_SCHEMA_VERSION,
                "runId": clean_run_id,
                "deleted": True,
                "recoverable": False,
            }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            services = tuple(self._services.values())
            self._services.clear()
        for service in services:
            service.close()

    def _require_open(self) -> None:
        if self._closed:
            raise RagBenchmarkSandboxError(
                "benchmark sandbox is closed",
                code="sandbox_closed",
            )

    def _active_run_count(self) -> int:
        count = 0
        for child in self.root.iterdir():
            if child.name.startswith("run-") and child.is_dir() and not child.is_symlink():
                count += 1
        return count

    def _load_owned_run(
        self,
        owner_session_id: object,
        run_id: object,
    ) -> tuple[Path, dict[str, Any]]:
        self._require_open()
        owner = _identifier(owner_session_id, "owner session id")
        clean_run_id = str(run_id or "").strip().lower()
        if not _RUN_ID.fullmatch(clean_run_id):
            raise RagBenchmarkSandboxError("runId is invalid", code="invalid_argument")
        run_path = self.root / f"run-{clean_run_id}"
        try:
            run_stat = run_path.lstat()
        except FileNotFoundError as exc:
            raise RagBenchmarkSandboxError("benchmark run was not found", code="not_found") from exc
        if not stat.S_ISDIR(run_stat.st_mode) or stat.S_ISLNK(run_stat.st_mode):
            raise RagBenchmarkSandboxError(
                "benchmark run is not a real directory",
                code="marker_mismatch",
            )
        if run_path.resolve(strict=True).parent != self.root:
            raise RagBenchmarkSandboxError(
                "benchmark run escaped the configured root",
                code="marker_mismatch",
            )
        marker_path = run_path / RUN_MARKER_NAME
        try:
            marker_stat = marker_path.lstat()
        except FileNotFoundError as exc:
            raise RagBenchmarkSandboxError("benchmark marker is missing", code="marker_mismatch") from exc
        if not stat.S_ISREG(marker_stat.st_mode) or stat.S_ISLNK(marker_stat.st_mode):
            raise RagBenchmarkSandboxError("benchmark marker is invalid", code="marker_mismatch")
        try:
            manifest = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RagBenchmarkSandboxError("benchmark marker cannot be read", code="marker_mismatch") from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("schemaVersion") != RAG_BENCHMARK_SANDBOX_SCHEMA_VERSION
            or manifest.get("runId") != clean_run_id
            or manifest.get("state") != "active"
            or not isinstance(manifest.get("bases"), dict)
            or not isinstance(manifest.get("documents"), dict)
            or not isinstance(manifest.get("usage"), dict)
        ):
            raise RagBenchmarkSandboxError("benchmark marker does not match the run", code="marker_mismatch")
        if str(manifest.get("ownerSessionSha256") or "") != _sha256(owner):
            raise RagBenchmarkSandboxError(
                "benchmark run belongs to a different session",
                code="owner_mismatch",
            )
        return run_path, manifest

    def _service(self, run_id: str, run_path: Path) -> KnowledgeLibraryService:
        service = self._services.get(run_id)
        if service is None:
            service = self._service_factory(run_path / "knowledge", self.policy)
            self._services[run_id] = service
        return service

    @staticmethod
    def _base_record(manifest: Mapping[str, object], alias: str) -> dict[str, Any]:
        record = _manifest_mapping(manifest, "bases").get(alias)
        if not isinstance(record, dict) or not str(record.get("kbId") or ""):
            raise RagBenchmarkSandboxError(
                f"benchmark base alias {alias!r} was not found",
                code="not_found",
            )
        return record


class RagBenchmarkSandboxTool:
    """Tool adapter disclosed only by the local evaluation harness."""

    tool_id = "rag_benchmark"

    def __init__(self, sandbox: RagBenchmarkSandbox) -> None:
        self.sandbox = sandbox

    def manifest(self) -> dict[str, Any]:
        return {
            "schemaVersion": RAG_BENCHMARK_TOOL_SCHEMA_VERSION,
            "id": self.tool_id,
            "displayName": "RAG Benchmark Sandbox",
            "description": (
                "Build, tune, rebuild, construct a Luna-backed Knowledge-only graph, independently rerank, "
                "score a host-registered validation suite without revealing qrels, query, and clean "
                "an isolated local Knowledge benchmark. "
                "Only inline public fixture text is accepted; host paths and production knowledge roots are unavailable."
            ),
            "scope": "benchmark-only",
            "operations": list(_OPERATIONS),
            "localOnly": True,
            "networkUploadAllowed": False,
            "parameters": _tool_parameter_schema(),
        }

    def execute(self, owner_session_id: object, args: Mapping[str, object]) -> dict[str, Any]:
        if not isinstance(args, Mapping):
            raise RagBenchmarkSandboxError("tool arguments must be an object", code="invalid_argument")
        operation = str(args.get("op") or "").strip()
        if operation not in _OPERATIONS:
            raise RagBenchmarkSandboxError("unsupported rag_benchmark operation", code="invalid_argument")
        allowed = _OPERATION_ARGUMENTS[operation]
        unexpected = sorted(set(args) - allowed)
        if unexpected:
            raise RagBenchmarkSandboxError(
                f"unsupported {operation} arguments: {', '.join(unexpected)}",
                code="invalid_argument",
            )
        if operation == "create_run":
            return self.sandbox.create_run(owner_session_id, label=args.get("label", ""))
        run_id = args.get("runId")
        if operation == "create_base":
            return self.sandbox.create_base(
                owner_session_id,
                run_id,
                alias=args.get("baseAlias"),
                name=args.get("name"),
                description=args.get("description", ""),
                chunking_config=_optional_mapping(args.get("chunkingConfig"), "chunkingConfig"),
                retrieval_config=_optional_mapping(args.get("retrievalConfig"), "retrievalConfig"),
            )
        if operation == "import_documents":
            return self.sandbox.import_documents(
                owner_session_id,
                run_id,
                base_alias=args.get("baseAlias"),
                documents=args.get("documents"),
            )
        if operation == "configure_base":
            return self.sandbox.configure_base(
                owner_session_id,
                run_id,
                base_alias=args.get("baseAlias"),
                expected_revision=args.get("expectedRevision"),
                chunking_config=_optional_mapping(args.get("chunkingConfig"), "chunkingConfig"),
                retrieval_config=_optional_mapping(args.get("retrievalConfig"), "retrievalConfig"),
            )
        if operation == "rebuild_preview":
            return self.sandbox.rebuild_preview(
                owner_session_id,
                run_id,
                base_alias=args.get("baseAlias"),
            )
        if operation == "rebuild":
            return self.sandbox.rebuild(
                owner_session_id,
                run_id,
                base_alias=args.get("baseAlias"),
                preview_token=args.get("previewToken"),
                expected_revision=args.get("expectedRevision"),
                confirm_text=args.get("confirmText"),
            )
        if operation == "graph_rebuild":
            return self.sandbox.rebuild_graph(
                owner_session_id,
                run_id,
                base_alias=args.get("baseAlias"),
                expected_revision=args.get("expectedRevision"),
                extractor_mode=args.get("extractorMode", "luna"),
                batch_size=args.get("batchSize", 8),
                extraction_concurrency=args.get("extractionConcurrency", 4),
                max_entities=args.get("maxEntities", 5),
                max_relations=args.get("maxRelations", 4),
                max_topics=args.get("maxTopics", 2),
            )
        if operation == "search":
            return self.sandbox.search(
                owner_session_id,
                run_id,
                base_alias=args.get("baseAlias"),
                query=args.get("query"),
                top_k=args.get("topK", 10),
                mode=args.get("mode", "hybrid"),
                threshold=args.get("threshold", 0.0),
                rerank=args.get("rerank", False),
                rerank_candidate_depth=args.get("rerankCandidateDepth", 20),
            )
        if operation == "evaluate_validation":
            return self.sandbox.evaluate_validation(
                owner_session_id,
                run_id,
                base_alias=args.get("baseAlias"),
                evaluation_suite_id=args.get("evaluationSuiteId"),
                top_k=args.get("topK", 10),
                mode=args.get("mode", "hybrid"),
                threshold=args.get("threshold", 0.0),
                rerank=args.get("rerank", False),
                rerank_candidate_depth=args.get("rerankCandidateDepth", 20),
            )
        if operation == "status":
            return self.sandbox.status(owner_session_id, run_id)
        return self.sandbox.cleanup(
            owner_session_id,
            run_id,
            confirm_text=args.get("confirmText"),
        )


_OPERATION_ARGUMENTS: dict[str, frozenset[str]] = {
    "create_run": frozenset({"op", "label"}),
    "create_base": frozenset(
        {"op", "runId", "baseAlias", "name", "description", "chunkingConfig", "retrievalConfig"}
    ),
    "import_documents": frozenset({"op", "runId", "baseAlias", "documents"}),
    "configure_base": frozenset(
        {"op", "runId", "baseAlias", "expectedRevision", "chunkingConfig", "retrievalConfig"}
    ),
    "rebuild_preview": frozenset({"op", "runId", "baseAlias"}),
    "rebuild": frozenset(
        {"op", "runId", "baseAlias", "previewToken", "expectedRevision", "confirmText"}
    ),
    "graph_rebuild": frozenset(
        {
            "op",
            "runId",
            "baseAlias",
            "expectedRevision",
            "extractorMode",
            "batchSize",
            "extractionConcurrency",
            "maxEntities",
            "maxRelations",
            "maxTopics",
        }
    ),
    "search": frozenset(
        {
            "op",
            "runId",
            "baseAlias",
            "query",
            "topK",
            "mode",
            "threshold",
            "rerank",
            "rerankCandidateDepth",
            "evaluationCaseId",
        }
    ),
    "evaluate_validation": frozenset(
        {
            "op",
            "runId",
            "baseAlias",
            "evaluationSuiteId",
            "topK",
            "mode",
            "threshold",
            "rerank",
            "rerankCandidateDepth",
        }
    ),
    "status": frozenset({"op", "runId"}),
    "cleanup": frozenset({"op", "runId", "confirmText"}),
}


def _default_service_factory(
    root: Path,
    policy: RagBenchmarkSandboxPolicy,
) -> KnowledgeLibraryService:
    config = KnowledgeLibraryConfig(
        root,
        max_source_bytes=policy.max_document_bytes,
    )
    service = KnowledgeLibraryService(
        config,
        dense_index=NullDenseIndex("benchmark dense index is initializing"),
        background_jobs=False,
    )
    service.dense_index = SqliteDenseIndex(
        config.database_path,
        HashingEmbeddingProvider(dimensions=96),
    )
    service.graph.extractor_factory = benchmark_graph_extractor_factory(
        root.parent / "luna-knowledge-graph",
        codex_bin=os.environ.get("RAG_IME_RAG_BENCHMARK_CODEX_BIN", "codex"),
        timeout_seconds=_benchmark_luna_timeout(),
    )
    return service


def _diversify_reranked_hits(
    hits: Sequence[Mapping[str, Any]],
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    """Keep the best passage per source before bounded context packing."""

    bounded_limit = max(1, min(20, int(limit)))
    selected: list[dict[str, Any]] = []
    seen_documents: set[str] = set()
    duplicate_count = 0
    for hit in hits:
        item = dict(hit)
        document_id = str(item.get("externalDocumentId") or "").strip()
        if document_id and document_id in seen_documents:
            duplicate_count += 1
            continue
        if document_id:
            seen_documents.add(document_id)
        item["packedRank"] = len(selected) + 1
        selected.append(item)
        if len(selected) >= bounded_limit:
            break
    return selected, duplicate_count


def _knowledge_call(function: Callable[..., Any], *args: object, **kwargs: object) -> Any:
    try:
        return function(*args, **kwargs)
    except KnowledgeLibraryError as exc:
        raise RagBenchmarkSandboxError(str(exc), code=str(exc.code or "knowledge_error")) from exc


def _normalize_inline_document(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RagBenchmarkSandboxError("each document must be an object", code="invalid_argument")
    allowed = {"externalId", "name", "text", "mimeType"}
    unexpected = sorted(set(value) - allowed)
    if unexpected:
        raise RagBenchmarkSandboxError(
            f"inline documents do not accept: {', '.join(unexpected)}",
            code="invalid_argument",
        )
    external_id = _identifier(value.get("externalId"), "external document id")
    name = _text(value.get("name"), "document name", maximum=240)
    if Path(name).name != name or any(character in name for character in ("/", "\\", "\x00")):
        raise RagBenchmarkSandboxError(
            "document name must be a plain file name",
            code="invalid_argument",
        )
    text = value.get("text")
    if not isinstance(text, str) or not text.strip() or "\x00" in text:
        raise RagBenchmarkSandboxError(
            "document text must be non-empty UTF-8 text",
            code="invalid_argument",
        )
    encoded = text.encode("utf-8")
    mime_type = str(value.get("mimeType") or "text/markdown").split(";", 1)[0].strip().lower()
    if mime_type not in {"text/plain", "text/markdown"}:
        raise RagBenchmarkSandboxError(
            "benchmark inline documents support text/plain or text/markdown only",
            code="invalid_argument",
        )
    return {
        "externalId": external_id,
        "name": name,
        "text": text,
        "mimeType": mime_type,
        "byteSize": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _public_base(alias: str, base: Mapping[str, object]) -> dict[str, Any]:
    return {
        "schemaVersion": RAG_BENCHMARK_SANDBOX_SCHEMA_VERSION,
        "baseAlias": alias,
        "name": str(base.get("name") or ""),
        "description": str(base.get("description") or ""),
        "parserMode": str(base.get("parserMode") or ""),
        "chunkingConfig": dict(base["chunkingConfig"]) if isinstance(base.get("chunkingConfig"), Mapping) else {},
        "retrievalConfig": dict(base["retrievalConfig"]) if isinstance(base.get("retrievalConfig"), Mapping) else {},
        "configRevision": int(base.get("configRevision") or 0),
        "documentCount": int(base.get("documentCount") or 0),
        "readyDocumentCount": int(base.get("readyDocumentCount") or 0),
        "chunkCount": int(base.get("chunkCount") or 0),
        "reindexRequired": base.get("reindexRequired") is True,
        "staleDocumentCount": int(base.get("staleDocumentCount") or 0),
    }


def _public_document(external_id: str, document: Mapping[str, object]) -> dict[str, Any]:
    return {
        "externalDocumentId": external_id,
        "name": str(document.get("fileName") or ""),
        "mimeType": str(document.get("mimeType") or ""),
        "byteSize": int(document.get("byteSize") or 0),
        "sha256": str(document.get("sha256") or ""),
        "status": str(document.get("status") or ""),
        "chunkCount": int(document.get("chunkCount") or 0),
    }


def _citation_ref(external_id: str) -> str:
    """Return a short, stable handle so models need not transcribe opaque IDs."""

    return "K-" + hashlib.sha256(external_id.encode("utf-8")).hexdigest()[:10]


def _reverse_document_ids(manifest: Mapping[str, object]) -> dict[str, str]:
    result: dict[str, str] = {}
    for external_id, record in _manifest_mapping(manifest, "documents").items():
        if isinstance(record, Mapping) and str(record.get("documentId") or ""):
            result[str(record["documentId"])] = str(external_id)
    return result


def _manifest_mapping(manifest: Mapping[str, object], key: str) -> dict[str, Any]:
    value = manifest.get(key)
    if not isinstance(value, dict):
        raise RagBenchmarkSandboxError(
            f"benchmark marker field {key} is invalid",
            code="marker_mismatch",
        )
    return value


def _write_manifest(run_path: Path, manifest: Mapping[str, object]) -> None:
    marker = run_path / RUN_MARKER_NAME
    payload = json.dumps(
        manifest,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8") + b"\n"
    _atomic_write_bytes(marker, payload)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _tool_parameter_schema() -> dict[str, Any]:
    run_id = {"type": "string", "pattern": "^[a-f0-9]{32}$"}
    alias = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$"}
    chunking_config = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "strategy": {
                "type": "string",
                "enum": ["general", "markdown", "book", "qa", "laws", "separator", "fixed"],
            },
            "preset": {
                "type": "string",
                "enum": [
                    "general",
                    "paragraph",
                    "markdown",
                    "book",
                    "qa",
                    "laws",
                    "separator",
                    "fixed",
                ],
            },
            "size": {"type": "integer", "minimum": 200, "maximum": 8_000},
            "overlap": {"type": "integer", "minimum": 0, "maximum": 2_000},
            "separator": {"type": "string", "maxLength": 100},
            "respectHeadings": {"type": "boolean"},
            "respectPageBoundaries": {"type": "boolean"},
        },
    }
    retrieval_config = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "mode": {"type": "string", "enum": ["lexical", "dense", "hybrid"]},
            "topK": {"type": "integer", "minimum": 1, "maximum": 100},
            "threshold": {"type": "number", "minimum": 0, "maximum": 1},
            "lexicalWeight": {"type": "number", "minimum": 0, "maximum": 10},
            "denseWeight": {"type": "number", "minimum": 0, "maximum": 10},
            "graphEnabled": {"type": "boolean"},
            "graphWeight": {"type": "number", "minimum": 0, "maximum": 10},
            "rrfK": {"type": "integer", "minimum": 1, "maximum": 1_000},
            "candidateMultiplier": {"type": "integer", "minimum": 1, "maximum": 20},
        },
    }
    inline_document = {
        "type": "object",
        "additionalProperties": False,
        "required": ["externalId", "name", "text"],
        "properties": {
            "externalId": alias,
            "name": {"type": "string", "minLength": 1, "maxLength": 240},
            "text": {"type": "string", "minLength": 1},
            "mimeType": {"type": "string", "enum": ["text/plain", "text/markdown"]},
        },
    }

    def operation_schema(
        operation: str,
        required: Sequence[str],
        properties: Mapping[str, object],
    ) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["op", *required],
            "properties": {"op": {"const": operation}, **dict(properties)},
        }

    return {
        "type": "object",
        "oneOf": [
            operation_schema("create_run", [], {"label": {"type": "string", "maxLength": 200}}),
            operation_schema(
                "create_base",
                ["runId", "baseAlias", "name"],
                {
                    "runId": run_id,
                    "baseAlias": alias,
                    "name": {"type": "string", "minLength": 1, "maxLength": 240},
                    "description": {"type": "string", "maxLength": 4_000},
                    "chunkingConfig": chunking_config,
                    "retrievalConfig": retrieval_config,
                },
            ),
            operation_schema(
                "import_documents",
                ["runId", "baseAlias", "documents"],
                {
                    "runId": run_id,
                    "baseAlias": alias,
                    "documents": {"type": "array", "minItems": 1, "items": inline_document},
                },
            ),
            operation_schema(
                "configure_base",
                ["runId", "baseAlias", "expectedRevision"],
                {
                    "runId": run_id,
                    "baseAlias": alias,
                    "expectedRevision": {"type": "integer", "minimum": 1},
                    "chunkingConfig": chunking_config,
                    "retrievalConfig": retrieval_config,
                },
            ),
            operation_schema(
                "rebuild_preview",
                ["runId", "baseAlias"],
                {"runId": run_id, "baseAlias": alias},
            ),
            operation_schema(
                "rebuild",
                ["runId", "baseAlias", "previewToken", "expectedRevision", "confirmText"],
                {
                    "runId": run_id,
                    "baseAlias": alias,
                    "previewToken": {"type": "string", "minLength": 1},
                    "expectedRevision": {"type": "integer", "minimum": 1},
                    "confirmText": {"const": REBUILD_CONFIRMATION},
                },
            ),
            operation_schema(
                "graph_rebuild",
                ["runId", "baseAlias", "expectedRevision"],
                {
                    "runId": run_id,
                    "baseAlias": alias,
                    "expectedRevision": {"type": "integer", "minimum": 0},
                    "extractorMode": {"type": "string", "enum": ["luna", "deterministic"]},
                    "batchSize": {"type": "integer", "minimum": 1, "maximum": 8},
                    "extractionConcurrency": {"type": "integer", "minimum": 1, "maximum": 4},
                    "maxEntities": {"type": "integer", "minimum": 1, "maximum": 8},
                    "maxRelations": {"type": "integer", "minimum": 0, "maximum": 8},
                    "maxTopics": {"type": "integer", "minimum": 0, "maximum": 4},
                },
            ),
            operation_schema(
                "search",
                ["runId", "baseAlias", "query"],
                {
                    "runId": run_id,
                    "baseAlias": alias,
                    "query": {"type": "string", "minLength": 1, "maxLength": 20_000},
                    "topK": {"type": "integer", "minimum": 1, "maximum": 20},
                    "mode": {"type": "string", "enum": ["lexical", "dense", "hybrid"]},
                    "threshold": {"type": "number", "minimum": 0, "maximum": 1},
                    "rerank": {"type": "boolean"},
                    "rerankCandidateDepth": {
                        "type": "integer",
                        "minimum": 2,
                        "maximum": 100,
                    },
                    "evaluationCaseId": alias,
                },
            ),
            operation_schema(
                "evaluate_validation",
                ["runId", "baseAlias", "evaluationSuiteId"],
                {
                    "runId": run_id,
                    "baseAlias": alias,
                    "evaluationSuiteId": alias,
                    "topK": {"type": "integer", "minimum": 1, "maximum": 20},
                    "mode": {"type": "string", "enum": ["lexical", "dense", "hybrid"]},
                    "threshold": {"type": "number", "minimum": 0, "maximum": 1},
                    "rerank": {"type": "boolean"},
                    "rerankCandidateDepth": {
                        "type": "integer",
                        "minimum": 2,
                        "maximum": 100,
                    },
                },
            ),
            operation_schema("status", ["runId"], {"runId": run_id}),
            operation_schema(
                "cleanup",
                ["runId", "confirmText"],
                {"runId": run_id, "confirmText": {"const": DELETE_CONFIRMATION}},
            ),
        ]
    }


def _normalize_evaluation_suites(
    value: Mapping[str, Sequence[Mapping[str, object]]],
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping):
        raise RagBenchmarkSandboxError(
            "evaluation suites must be an object",
            code="invalid_argument",
        )
    if len(value) > 64:
        raise RagBenchmarkSandboxError(
            "too many validation evaluation suites",
            code="budget_exceeded",
        )
    suites: dict[str, dict[str, Any]] = {}
    for raw_suite_id, raw_cases in value.items():
        suite_id = _identifier(raw_suite_id, "evaluation suite id")
        if not isinstance(raw_cases, (list, tuple)) or not raw_cases:
            raise RagBenchmarkSandboxError(
                "each validation evaluation suite must contain cases",
                code="invalid_argument",
            )
        if len(raw_cases) > 1_000:
            raise RagBenchmarkSandboxError(
                "validation evaluation suite exceeds 1000 cases",
                code="budget_exceeded",
            )
        cases: list[dict[str, Any]] = []
        seen_case_ids: set[str] = set()
        for raw_case in raw_cases:
            if not isinstance(raw_case, Mapping):
                raise RagBenchmarkSandboxError(
                    "validation evaluation cases must be objects",
                    code="invalid_argument",
                )
            unexpected = sorted(
                set(raw_case) - {"caseId", "split", "query", "relevant"}
            )
            if unexpected:
                raise RagBenchmarkSandboxError(
                    f"unsupported validation case field: {unexpected[0]}",
                    code="invalid_argument",
                )
            case_id = _identifier(raw_case.get("caseId"), "evaluation case id")
            if case_id in seen_case_ids:
                raise RagBenchmarkSandboxError(
                    "validation evaluation suite contains duplicate case IDs",
                    code="invalid_argument",
                )
            seen_case_ids.add(case_id)
            if str(raw_case.get("split") or "").strip().lower() != "validation":
                raise RagBenchmarkSandboxError(
                    "Agent-callable evaluation suites may contain validation cases only",
                    code="held_out_forbidden",
                )
            query = _text(raw_case.get("query"), "evaluation query", maximum=20_000)
            raw_relevant = raw_case.get("relevant")
            if not isinstance(raw_relevant, Mapping) or not raw_relevant:
                raise RagBenchmarkSandboxError(
                    "validation evaluation qrels must be a non-empty object",
                    code="invalid_argument",
                )
            if len(raw_relevant) > 100:
                raise RagBenchmarkSandboxError(
                    "validation evaluation case exceeds 100 qrels",
                    code="budget_exceeded",
                )
            relevant: dict[str, float] = {}
            for raw_document_id, raw_gain in raw_relevant.items():
                document_id = _identifier(
                    raw_document_id,
                    "relevant external document id",
                )
                gain = _finite_number(
                    raw_gain,
                    "relevance gain",
                    minimum=0.0,
                    maximum=1_000_000.0,
                )
                if gain <= 0:
                    raise RagBenchmarkSandboxError(
                        "relevance gains must be positive",
                        code="invalid_argument",
                    )
                relevant[document_id] = gain
            cases.append(
                {
                    "caseId": case_id,
                    "split": "validation",
                    "query": query,
                    "relevant": relevant,
                }
            )
        encoded = json.dumps(
            cases,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        case_ids = [str(case["caseId"]) for case in cases]
        suites[suite_id] = {
            "cases": tuple(cases),
            "suiteSha256": _sha256(encoded),
            "caseIdsSha256": _sha256(
                json.dumps(case_ids, ensure_ascii=False, separators=(",", ":"))
            ),
        }
    return suites


def _optional_mapping(value: object, field: str) -> Mapping[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise RagBenchmarkSandboxError(f"{field} must be an object", code="invalid_argument")
    return value


def _identifier(value: object, field: str) -> str:
    normalized = str(value or "").strip()
    if not _IDENTIFIER.fullmatch(normalized):
        raise RagBenchmarkSandboxError(f"{field} is invalid", code="invalid_argument")
    return normalized


def _text(value: object, field: str, *, maximum: int, required: bool = True) -> str:
    if not isinstance(value, str):
        if value is None and not required:
            return ""
        raise RagBenchmarkSandboxError(f"{field} must be text", code="invalid_argument")
    normalized = value.strip()
    if (required and not normalized) or len(normalized) > maximum or "\x00" in normalized:
        raise RagBenchmarkSandboxError(f"{field} is invalid", code="invalid_argument")
    return normalized


def _positive_integer(value: object, field: str) -> int:
    return _bounded_integer(value, field, minimum=1, maximum=2_147_483_647)


def _bounded_integer(value: object, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise RagBenchmarkSandboxError(f"{field} must be an integer", code="invalid_argument")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise RagBenchmarkSandboxError(f"{field} must be an integer", code="invalid_argument") from exc
    if normalized < minimum or normalized > maximum:
        raise RagBenchmarkSandboxError(
            f"{field} must be between {minimum} and {maximum}",
            code="invalid_argument",
        )
    return normalized


def _finite_number(value: object, field: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise RagBenchmarkSandboxError(f"{field} must be a number", code="invalid_argument")
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise RagBenchmarkSandboxError(f"{field} must be a number", code="invalid_argument") from exc
    if not math.isfinite(normalized) or normalized < minimum or normalized > maximum:
        raise RagBenchmarkSandboxError(
            f"{field} must be between {minimum} and {maximum}",
            code="invalid_argument",
        )
    return normalized


def _optional_search_score(hit: Mapping[str, object]) -> float | None:
    """Project a measured score without turning an omitted value into zero."""

    if "score" not in hit or hit.get("score") is None:
        return None
    value = hit.get("score")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RagBenchmarkSandboxError("search returned invalid score", code="integrity_error")
    try:
        score = float(value)
    except (OverflowError, ValueError) as exc:
        raise RagBenchmarkSandboxError("search returned invalid score", code="integrity_error") from exc
    if not math.isfinite(score):
        raise RagBenchmarkSandboxError("search returned invalid score", code="integrity_error")
    return score


def _budget_error(message: str) -> RagBenchmarkSandboxError:
    return RagBenchmarkSandboxError(message, code="budget_exceeded")


def _total_rebuilds(usage: Mapping[str, object]) -> int:
    return int(usage.get("rebuilds") or 0) + int(usage.get("graphRebuilds") or 0)


def _benchmark_luna_timeout() -> float:
    try:
        value = float(os.environ.get("RAG_IME_RAG_BENCHMARK_LUNA_TIMEOUT_SECONDS", "1200"))
    except ValueError:
        value = 1_200.0
    return max(1.0, min(3_600.0, value))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
