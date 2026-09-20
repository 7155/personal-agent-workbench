from __future__ import annotations

import hashlib
import hmac
import json
import math
import mimetypes
import os
import re
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Sequence

from ..contracts.json_schema import validate_contract
from ..embeddings import embedding_provider_from_env
from .dense import DenseIndex, dense_index_from_env
from .graph import GRAPH_NODE_KINDS, KnowledgeGraph
from .models import (
    AssetBlob,
    KNOWLEDGE_SCHEMA_VERSION,
    PARSER_MODES,
    DocumentParseError,
    KnowledgeLibraryConfig,
    KnowledgeLibraryError,
    KnowledgeNotFoundError,
    ParsedDocument,
)
from .parsers import ParserRouter
from .permissions import harden_knowledge_tree, secure_directory, secure_file
from .rerank import KnowledgeReranker
from .vault import MarkdownVault
from .store import KnowledgeStore, decode_metadata, now_ms, rank_retrieval_hits as _rank_retrieval_hits


DEFAULT_CHUNKING_CONFIG: dict[str, Any] = {
    "strategy": "markdown",
    "size": 1_200,
    "overlap": 160,
    "separator": "\n\n",
    "respectHeadings": True,
    "respectPageBoundaries": True,
}
DEFAULT_RETRIEVAL_CONFIG: dict[str, Any] = {
    "mode": "hybrid",
    "topK": 10,
    "threshold": 0.0,
    "lexicalWeight": 1.0,
    "denseWeight": 1.0,
    "graphEnabled": True,
    "graphWeight": 0.7,
    "rrfK": 60,
    "candidateMultiplier": 4,
    "rerankEnabled": False,
    "rerankCandidateDepth": 40,
}
AGENT_SEARCH_TOP_K_LIMIT = 12
CHUNKING_STRATEGIES = ("general", "markdown", "book", "qa", "laws", "separator", "fixed")
_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp"})
_SOURCE_PREVIEW_MIME_TYPES = _IMAGE_MIME_TYPES | frozenset(
    {
        "image/tiff",
        "application/pdf",
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/json",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
)
_MAX_TABLE_ARTIFACTS = 32
_MAX_TABLE_ROWS = 200
_MAX_TABLE_COLUMNS = 32
_MAX_TABLE_CELL_CHARS = 500
_MAX_TABLE_MARKDOWN_CHARS = 64_000
_MAX_HTML_TABLE_SCAN_CHARS = 2 * 1024 * 1024
_MAX_TABLE_LINE_CHARS = 64 * 1024


class KnowledgeLibraryService:
    def __init__(
        self,
        config: KnowledgeLibraryConfig,
        *,
        parser_router: ParserRouter | None = None,
        dense_index: DenseIndex | None = None,
        reranker: KnowledgeReranker | None = None,
        background_jobs: bool = False,
    ):
        self.config = config
        harden_knowledge_tree(self.config.root_dir)
        secure_directory(self.config.files_dir)
        secure_directory(self.config.assets_dir)
        secure_directory(self.config.artifacts_dir)
        self.store = KnowledgeStore(config.database_path)
        self.vault = MarkdownVault(self.store)
        self.graph = KnowledgeGraph(self.store)
        self.parsers = parser_router or ParserRouter(config)
        self.dense_index = dense_index or dense_index_from_env(
            config.database_path,
            embedding_provider_from_env(),
        )
        self.reranker = reranker
        self._dense_error = ""
        self._ingest_lock = threading.Lock()
        self._vault_capture_future = None
        self._job_executor = (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="rag-ime-knowledge")
            if background_jobs
            else None
        )
        if self._job_executor is not None:
            self._recover_incomplete_jobs()

    def close(self, *, wait: bool = True) -> None:
        if self._job_executor is not None:
            self._job_executor.shutdown(wait=wait, cancel_futures=not wait)
            self._job_executor = None

    def schedule_vault_capture(self) -> bool:
        """Use the existing Knowledge worker queue, never a second agent loop."""
        if self._job_executor is None:
            return False
        if self._vault_capture_future is not None and not self._vault_capture_future.done():
            return True
        with self.store.connection() as db:
            configured = [r[0] for r in db.execute(
                "SELECT v.id FROM knowledge_vaults v JOIN knowledge_vault_policy p ON v.id=p.vault_id "
                "WHERE v.paused=0 AND (COALESCE(json_extract(p.policy_json,'$.captureFolder'),'')<>'' OR COALESCE(json_extract(p.policy_json,'$.activityProject'),'')<>'')")]
        if not configured:
            return False
        self._vault_capture_future = self._job_executor.submit(self._capture_vaults, configured)
        return True

    def _capture_vaults(self, identities):
        for identity in identities:
            try:
                # dispatch checks the current pause state and reads current policy;
                # queued work has no authority from an older settings snapshot.
                self.vault.dispatch({"action":"snapshot", "vaultId":identity})
                policy = self.vault.dispatch({"action":"settings", "vaultId":identity})['policy']
                if policy['activityProject']:
                    from datetime import datetime
                    from zoneinfo import ZoneInfo
                    zone = policy['activityTimezone']
                    self.vault.dispatch({"action":"day","vaultId":identity,"project":policy['activityProject'],
                        "date":datetime.now(ZoneInfo(zone)).date().isoformat(),"timezone":zone})
            except (OSError, KnowledgeLibraryError):
                continue

    def create_base(
        self,
        name: str,
        *,
        description: str = "",
        parser_mode: str = "auto",
        agent_enabled: bool = False,
        chunking_config: dict[str, Any] | None = None,
        retrieval_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean_name = _clean_name(name, field="knowledge base name")
        parser_mode = _parser_mode(parser_mode)
        normalized_chunking = _normalize_chunking_config(
            chunking_config,
            defaults={
                **DEFAULT_CHUNKING_CONFIG,
                "size": self.config.chunk_chars,
                "overlap": self.config.chunk_overlap_chars,
            },
        )
        normalized_retrieval = _normalize_retrieval_config(retrieval_config)
        row = self.store.create_base(
            base_id=uuid.uuid4().hex,
            name=clean_name,
            normalized_name=clean_name.casefold(),
            description=str(description or "").strip()[:4_000],
            parser_mode=parser_mode,
            agent_enabled=bool(agent_enabled),
            chunking_config_json=json.dumps(normalized_chunking, sort_keys=True),
            retrieval_config_json=json.dumps(normalized_retrieval, sort_keys=True),
        )
        return _base_to_dict(row)

    def list_bases(self, *, agent_only: bool = False) -> dict[str, Any]:
        bases = [_base_to_dict(row) for row in self.store.list_bases(agent_only=agent_only)]
        return {"schemaVersion": KNOWLEDGE_SCHEMA_VERSION, "bases": bases, "total": len(bases)}

    def get_base(self, base_id: str) -> dict[str, Any]:
        return _base_to_dict(self.store.get_base(_identifier(base_id, "base id")))

    def update_base(
        self,
        base_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        parser_mode: str | None = None,
        agent_enabled: bool | None = None,
        chunking_config: dict[str, Any] | None = None,
        retrieval_config: dict[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        base_id = _identifier(base_id, "base id")
        current = self.store.get_base(base_id)
        if expected_revision is not None and int(current["config_revision"]) != int(expected_revision):
            raise KnowledgeLibraryError("knowledge base revision does not match", code="stale_revision")
        fields: dict[str, Any] = {}
        requires_reindex = False
        if name is not None:
            fields["name"] = _clean_name(name, field="knowledge base name")
            fields["normalized_name"] = fields["name"].casefold()
        if description is not None:
            fields["description"] = str(description).strip()[:4_000]
        if parser_mode is not None:
            normalized_parser = _parser_mode(parser_mode)
            fields["parser_mode"] = normalized_parser
            requires_reindex = normalized_parser != str(current["parser_mode"])
        if agent_enabled is not None:
            fields["agent_enabled"] = int(bool(agent_enabled))
        if chunking_config is not None:
            normalized_chunking = _normalize_chunking_config(
                chunking_config,
                defaults=_json_object(current["chunking_config_json"], DEFAULT_CHUNKING_CONFIG),
            )
            serialized = json.dumps(normalized_chunking, sort_keys=True)
            fields["chunking_config_json"] = serialized
            requires_reindex = requires_reindex or serialized != str(current["chunking_config_json"])
        if retrieval_config is not None:
            fields["retrieval_config_json"] = json.dumps(
                _normalize_retrieval_config(
                    retrieval_config,
                    defaults=_json_object(current["retrieval_config_json"], DEFAULT_RETRIEVAL_CONFIG),
                ),
                sort_keys=True,
            )
        if fields:
            fields["config_revision"] = int(current["config_revision"]) + 1
        updated = self.store.update_base(base_id, fields)
        stale_count = self.store.mark_base_documents_stale(base_id) if requires_reindex else 0
        if fields and not requires_reindex:
            self.store.advance_ready_index_revision(base_id, int(updated["config_revision"]))
        result = _base_to_dict(self.store.get_base(base_id))
        result["reindexRequired"] = stale_count > 0
        result["staleDocumentCount"] = stale_count
        return result

    def delete_base(self, base_id: str) -> dict[str, Any]:
        base_id = _identifier(base_id, "base id")
        documents = self.store.list_documents(base_id)
        for row in self.store.list_jobs(base_id=base_id, limit=500):
            if str(row["status"]) in {"queued", "running"}:
                self.store.cancel_job(str(row["id"]))
        self.store.delete_base(base_id)
        for row in documents:
            _unlink_quietly(Path(str(row["stored_path"])))
            self._delete_dense(str(row["id"]))
        shutil.rmtree(self.config.files_dir / base_id, ignore_errors=True)
        shutil.rmtree(self.config.artifacts_dir / base_id, ignore_errors=True)
        self._remove_unreferenced_assets()
        return {"schemaVersion": KNOWLEDGE_SCHEMA_VERSION, "deleted": True, "baseId": base_id}

    def import_document(
        self,
        base_id: str,
        source_path: str | Path,
        *,
        display_name: str = "",
        mime_type: str = "",
        parser_mode: str | None = None,
    ) -> dict[str, Any]:
        base_id = _identifier(base_id, "base id")
        base = self.store.get_base(base_id)
        source = _validated_source(Path(source_path), max_bytes=self.config.max_source_bytes)
        source_hash, byte_size = _hash_file(source)
        document_id = uuid.uuid4().hex
        suffix = source.suffix.lower()[:20]
        stored_dir = self.config.files_dir / base_id
        secure_directory(stored_dir)
        stored_path = stored_dir / f"{source_hash}{suffix}"
        if not stored_path.exists():
            temporary = stored_path.with_name(f".{stored_path.name}.{uuid.uuid4().hex}.tmp")
            shutil.copyfile(source, temporary)
            secure_file(temporary)
            os.replace(temporary, stored_path)
        secure_file(stored_path)
        timestamp = now_ms()
        guessed_mime = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        supplied_mime = str(mime_type or "").split(";", 1)[0].strip().lower()
        effective_mime = guessed_mime if not supplied_mime or supplied_mime == "application/octet-stream" else supplied_mime
        row = self.store.insert_document(
            {
                "id": document_id,
                "base_id": base_id,
                "display_name": _clean_name(display_name or source.name, field="document name"),
                "source_name": source.name,
                "media_type": effective_mime[:200],
                "sha256": source_hash,
                "byte_size": byte_size,
                "stored_path": str(stored_path),
                "status": "queued",
                "revision": 1,
                "created_at_ms": timestamp,
                "updated_at_ms": timestamp,
            }
        )
        selected_mode = _parser_mode(parser_mode) if parser_mode else str(base["parser_mode"])
        return self._dispatch_document(row, parser_mode=selected_mode)

    def retry_document(self, document_id: str, *, parser_mode: str | None = None) -> dict[str, Any]:
        document_id = _identifier(document_id, "document id")
        row = self.store.get_document(document_id)
        base = self.store.get_base(str(row["base_id"]))
        selected_mode = (
            _parser_mode(parser_mode)
            if parser_mode is not None
            else _parser_mode_for_provider(str(row["parser_provider"] or ""), fallback=str(base["parser_mode"]))
        )
        row = self.store.update_document(
            document_id,
            {
                "revision": int(row["revision"]) + 1,
                "status": "queued",
                "error_code": "",
                "error_message": "",
            },
        )
        return self._dispatch_document(row, parser_mode=selected_mode)

    def list_documents(self, base_id: str) -> dict[str, Any]:
        documents = [_document_to_dict(row) for row in self.store.list_documents(_identifier(base_id, "base id"))]
        return {"schemaVersion": KNOWLEDGE_SCHEMA_VERSION, "documents": documents, "total": len(documents)}

    def list_jobs(self, *, base_id: str = "", limit: int = 100) -> dict[str, Any]:
        if base_id:
            self.store.get_base(_identifier(base_id, "base id"))
        jobs = [_job_to_dict(row) for row in self.store.list_jobs(base_id=base_id, limit=limit)]
        return {"schemaVersion": KNOWLEDGE_SCHEMA_VERSION, "jobs": jobs, "items": jobs, "total": len(jobs)}

    def cancel_job(self, job_id: str, *, base_id: str = "") -> dict[str, Any]:
        job_id = _identifier(job_id, "job id")
        row = self.store.get_job(job_id)
        if base_id and str(row["base_id"]) != _identifier(base_id, "base id"):
            raise KnowledgeNotFoundError(f"knowledge job {job_id!r} was not found")
        cancelled = self.store.cancel_job(job_id)
        return {"schemaVersion": KNOWLEDGE_SCHEMA_VERSION, "job": _job_to_dict(cancelled)}

    def knowledge_graph(
        self,
        base_id: str,
        *,
        document_id: str = "",
        query: str = "",
        kinds: Sequence[str] = (),
        limit: int = 200,
        depth: int = 2,
        exclude_chunks: bool = True,
        focus_id: str = "",
    ) -> dict[str, Any]:
        base_id = _identifier(base_id, "base id")
        document_id = _identifier(document_id, "document id") if document_id else ""
        focus_id = _identifier(focus_id, "focus id") if focus_id else ""
        normalized_kinds = tuple(str(item).strip().lower() for item in kinds if str(item).strip())
        unknown = sorted(set(normalized_kinds) - GRAPH_NODE_KINDS)
        if unknown:
            raise KnowledgeLibraryError(
                f"unsupported knowledge graph node kinds: {', '.join(unknown)}",
                code="invalid_argument",
            )
        result = self.graph.read(
            base_id,
            document_id=document_id,
            query=str(query or "")[:200],
            kinds=normalized_kinds,
            limit=limit,
            depth=depth,
            exclude_chunks=exclude_chunks,
            focus_id=focus_id,
        )
        validate_contract(result, "knowledge-graph.v1.json")
        return result

    def rebuild_knowledge_graph(
        self,
        base_id: str,
        *,
        expected_revision: int | None,
        document_ids: Sequence[str] = (),
        extractor_mode: str = "deterministic",
        model_id: str = "",
        batch_size: int = 4,
        extraction_concurrency: int = 2,
        max_entities: int = 5,
        max_relations: int = 4,
        max_topics: int = 2,
    ) -> dict[str, Any]:
        base_id = _identifier(base_id, "base id")
        normalized_ids = tuple(_identifier(item, "document id") for item in document_ids)
        mode = str(extractor_mode or "deterministic").strip().lower()
        if mode not in {"deterministic", "model"}:
            raise KnowledgeLibraryError("extractorMode must be deterministic or model", code="invalid_argument")
        options = {
            "extractor_mode": mode,
            "model_id": str(model_id or "").strip()[:160],
            "batch_size": max(1, min(8, int(batch_size))),
            "extraction_concurrency": max(1, min(4, int(extraction_concurrency))),
            "max_entities": max(1, min(8, int(max_entities))),
            "max_relations": max(0, min(8, int(max_relations))),
            "max_topics": max(0, min(4, int(max_topics))),
        }
        if mode == "model" and self._job_executor is not None:
            graph_job_id = f"kg-{uuid.uuid4().hex}"
            current_revision = self.graph.reserve_rebuild(
                base_id,
                expected_revision=expected_revision,
                job_id=graph_job_id,
                document_ids=normalized_ids,
                extractor_mode=mode,
                extractor_model=str(options["model_id"]),
            )
            self._job_executor.submit(
                self.graph.rebuild,
                base_id,
                expected_revision=expected_revision,
                document_ids=normalized_ids,
                job_id=graph_job_id,
                **options,
            )
            return {
                "ok": True,
                "jobId": graph_job_id,
                "status": "queued",
                "revision": current_revision + 1,
                "extractor": {
                    "mode": mode,
                    "model": options["model_id"],
                    "extractionConcurrency": options["extraction_concurrency"],
                },
            }
        return self.graph.rebuild(
            base_id,
            expected_revision=expected_revision,
            document_ids=normalized_ids,
            **options,
        )

    def preview_chunking(
        self,
        base_id: str,
        document_id: str,
        chunking_config: dict[str, Any],
        *,
        limit: int = 12,
    ) -> dict[str, Any]:
        base_id = _identifier(base_id, "base id")
        document_id = _identifier(document_id, "document id")
        base = self.store.get_base(base_id)
        document = self.store.get_document(document_id)
        if str(document["base_id"]) != base_id:
            raise KnowledgeNotFoundError(
                f"document {document_id!r} was not found in knowledge base {base_id!r}"
            )
        raw_artifact_path = str(document["artifact_path"] or "")
        if not raw_artifact_path:
            raise KnowledgeLibraryError(
                "document has no parsed Markdown artifact to preview",
                code="artifact_unavailable",
            )
        artifact_path = _safe_stored_path(Path(raw_artifact_path), root=self.config.artifacts_dir)
        if artifact_path.stat().st_size > self.config.max_source_bytes:
            raise KnowledgeLibraryError("parsed artifact is too large to preview", code="artifact_too_large")
        normalized = _normalize_chunking_config(
            chunking_config,
            defaults=_json_object(base["chunking_config_json"], DEFAULT_CHUNKING_CONFIG),
        )
        parsed = ParsedDocument(
            text=artifact_path.read_text(encoding="utf-8"),
            provider="artifact_preview",
            metadata=decode_metadata(document),
        )
        chunks = _chunk_document(
            parsed,
            document_id=document_id,
            base_id=base_id,
            chunking_config=normalized,
        )
        preview_limit = max(1, min(30, int(limit)))
        return {
            "schemaVersion": KNOWLEDGE_SCHEMA_VERSION,
            "kbId": base_id,
            "fileId": document_id,
            "config": normalized,
            "total": len(chunks),
            "items": [_chunk_to_dict(item) for item in chunks[:preview_limit]],
            "truncated": len(chunks) > preview_limit,
        }

    def get_document(self, document_id: str) -> dict[str, Any]:
        return _document_to_dict(self.store.get_document(_identifier(document_id, "document id")))

    def delete_document(self, document_id: str) -> dict[str, Any]:
        document_id = _identifier(document_id, "document id")
        for job in self.store.list_jobs(limit=500):
            if str(job["document_id"]) == document_id and str(job["status"]) in {"queued", "running"}:
                self.store.cancel_job(str(job["id"]))
        row = self.store.delete_document(document_id)
        _unlink_quietly(Path(str(row["stored_path"])))
        shutil.rmtree(self.config.artifacts_dir / str(row["base_id"]) / document_id, ignore_errors=True)
        self._delete_dense(document_id)
        self._remove_unreferenced_assets()
        return {
            "schemaVersion": KNOWLEDGE_SCHEMA_VERSION,
            "deleted": True,
            "baseId": str(row["base_id"]),
            "documentId": document_id,
        }

    def document_detail(
        self,
        base_id: str,
        document_id: str,
        *,
        offset: int = 0,
        limit: int = 100,
        line_offset: int = 0,
        line_limit: int = 200,
    ) -> dict[str, Any]:
        base_id = _identifier(base_id, "base id")
        document_id = _identifier(document_id, "document id")
        document = self.store.get_document(document_id)
        if str(document["base_id"]) != base_id:
            raise KnowledgeNotFoundError(f"document {document_id!r} was not found in knowledge base {base_id!r}")
        chunk_offset = max(0, int(offset))
        chunk_limit = max(1, min(500, int(limit)))
        chunk_rows, chunk_total = self.store.document_chunks(document_id, offset=chunk_offset, limit=chunk_limit)
        page_rows = self.store.document_pages(document_id)
        assets = [_asset_to_dict(row, base_id=base_id, document_id=document_id) for row in self.store.document_assets(document_id)]
        artifact = self._artifact_window(
            document,
            line_offset=max(0, int(line_offset)),
            line_limit=max(1, min(500, int(line_limit))),
        )
        tables = self._artifact_tables(document)
        return {
            "schemaVersion": KNOWLEDGE_SCHEMA_VERSION,
            "document": {
                **_document_to_dict(document),
                "sourceReadPath": f"/api/knowledge-bases/{base_id}/documents/{document_id}/source",
            },
            "chunks": {
                "items": [_chunk_to_dict(row) for row in chunk_rows],
                "offset": chunk_offset,
                "limit": chunk_limit,
                "total": chunk_total,
                "hasMore": chunk_offset + len(chunk_rows) < chunk_total,
            },
            "pages": [
                {
                    "page": int(row["page"]),
                    "chunkCount": int(row["chunk_count"]),
                    "firstOrdinal": int(row["first_ordinal"]),
                    "lastOrdinal": int(row["last_ordinal"]),
                }
                for row in page_rows
            ],
            "assets": assets,
            "tables": tables,
            **artifact,
        }

    def read_document_asset(self, base_id: str, document_id: str, asset_id: str) -> AssetBlob:
        base_id = _identifier(base_id, "base id")
        document_id = _identifier(document_id, "document id")
        if not re.fullmatch(r"[a-f0-9]{64}", str(asset_id or "")):
            raise KnowledgeLibraryError("invalid asset id", code="invalid_argument")
        row = self.store.document_asset(base_id=base_id, document_id=document_id, asset_id=asset_id)
        media_type = str(row["media_type"] or "").lower()
        if media_type not in _IMAGE_MIME_TYPES:
            raise KnowledgeLibraryError("asset MIME type is not allowed for inline reading", code="asset_type_not_allowed")
        byte_size = int(row["byte_size"])
        if byte_size > self.config.max_asset_read_bytes:
            raise KnowledgeLibraryError("asset exceeds the inline read size limit", code="asset_too_large")
        path = _safe_stored_path(Path(str(row["stored_path"])), root=self.config.assets_dir)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise KnowledgeNotFoundError(f"asset {asset_id!r} is unavailable") from exc
        if len(data) != byte_size or len(data) > self.config.max_asset_read_bytes:
            raise KnowledgeLibraryError("asset size does not match its stored metadata", code="asset_integrity_error")
        if hashlib.sha256(data).hexdigest() != asset_id:
            raise KnowledgeLibraryError("asset digest verification failed", code="asset_integrity_error")
        return AssetBlob(
            asset_id=asset_id,
            file_name=str(row["original_name"]),
            media_type=media_type,
            data=data,
        )

    def read_document_source(self, base_id: str, document_id: str) -> AssetBlob:
        base_id = _identifier(base_id, "base id")
        document_id = _identifier(document_id, "document id")
        document = self.store.get_document(document_id)
        if str(document["base_id"]) != base_id:
            raise KnowledgeNotFoundError(f"document {document_id!r} was not found in knowledge base {base_id!r}")
        media_type = str(document["media_type"] or "").split(";", 1)[0].lower()
        if media_type not in _SOURCE_PREVIEW_MIME_TYPES:
            raise KnowledgeLibraryError("source MIME type is not allowed for inline preview", code="source_type_not_allowed")
        byte_size = int(document["byte_size"])
        if byte_size > self.config.max_source_preview_bytes:
            raise KnowledgeLibraryError("source exceeds the inline preview size limit", code="source_too_large")
        base_files_root = self.config.files_dir / base_id
        path = _safe_stored_path(Path(str(document["stored_path"])), root=base_files_root)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise KnowledgeNotFoundError(f"source for document {document_id!r} is unavailable") from exc
        if len(data) != byte_size or len(data) > self.config.max_source_preview_bytes:
            raise KnowledgeLibraryError("source size does not match its stored metadata", code="source_integrity_error")
        sha256 = str(document["sha256"])
        if hashlib.sha256(data).hexdigest() != sha256:
            raise KnowledgeLibraryError("source digest verification failed", code="source_integrity_error")
        return AssetBlob(
            asset_id=sha256,
            file_name=str(document["display_name"]),
            media_type=media_type,
            data=data,
        )

    def reindex_preview(self, base_id: str) -> dict[str, Any]:
        base_id = _identifier(base_id, "base id")
        base = self.get_base(base_id)
        documents = self.store.list_documents(base_id)
        candidates = [row for row in documents if str(row["status"]) in {"ready", "stale", "failed"}]
        preview_token = _reindex_preview_token(base_id, int(base["configRevision"]), candidates)
        return {
            "schemaVersion": KNOWLEDGE_SCHEMA_VERSION,
            "kbId": base_id,
            "configRevision": base["configRevision"],
            "chunkingConfig": base["chunkingConfig"],
            "retrievalConfig": base["retrievalConfig"],
            "documentCount": len(documents),
            "rebuildCandidateCount": len(candidates),
            "staleDocumentCount": sum(str(row["status"]) == "stale" for row in documents),
            "estimatedSourceBytes": sum(int(row["byte_size"]) for row in candidates),
            "previewToken": preview_token,
            "documents": [
                {"fileId": str(row["id"]), "fileName": str(row["display_name"]), "status": str(row["status"])}
                for row in candidates
            ],
        }

    def rebuild_base(
        self,
        base_id: str,
        *,
        preview_token: str,
        expected_revision: int,
        confirm_text: str,
    ) -> dict[str, Any]:
        base_id = _identifier(base_id, "base id")
        base = self.store.get_base(base_id)
        candidates = [
            row for row in self.store.list_documents(base_id) if str(row["status"]) in {"ready", "stale", "failed"}
        ]
        if confirm_text != "REBUILD":
            raise KnowledgeLibraryError("confirmText must equal REBUILD", code="confirmation_required")
        if int(base["config_revision"]) != int(expected_revision):
            raise KnowledgeLibraryError("knowledge base configuration changed after preview", code="stale_preview")
        expected_token = _reindex_preview_token(base_id, int(base["config_revision"]), candidates)
        if not preview_token or not _constant_time_equal(preview_token, expected_token):
            raise KnowledgeLibraryError("reindex preview is stale or invalid", code="stale_preview")
        rebuilt: list[dict[str, Any]] = []
        for existing in candidates:
            row = self.store.update_document(
                str(existing["id"]),
                {
                    "revision": int(existing["revision"]) + 1,
                    "status": "queued",
                    "error_code": "",
                    "error_message": "",
                },
            )
            selected_mode = _parser_mode_for_provider(
                str(existing["parser_provider"] or ""),
                fallback=str(base["parser_mode"]),
            )
            rebuilt.append(self._dispatch_document(row, parser_mode=selected_mode, job_kind="reindex"))
        return {
            "schemaVersion": KNOWLEDGE_SCHEMA_VERSION,
            "kbId": base_id,
            "configRevision": int(base["config_revision"]),
            "requested": len(candidates),
            "ready": sum(item["status"] == "ready" for item in rebuilt),
            "failed": sum(item["status"] == "failed" for item in rebuilt),
            "queued": sum(item["status"] in {"queued", "parsing", "indexing"} for item in rebuilt),
            "documents": rebuilt,
        }

    def search(
        self,
        query: str,
        *,
        base_ids: Sequence[str] = (),
        limit: int | None = None,
        mode: str | None = None,
        threshold: float | None = None,
        rerank: bool | None = None,
        rerank_candidate_depth: int | None = None,
        file_name: str = "",
        agent_only: bool = False,
    ) -> dict[str, Any]:
        query = _query(query)
        selected_ids = tuple(_identifier(item, "base id") for item in base_ids)
        file_name = str(file_name or "").strip()[:512]
        if selected_ids:
            base_rows = [self.store.get_base(base_id) for base_id in selected_ids]
        else:
            base_rows = self.store.list_bases(agent_only=agent_only)
        if agent_only:
            base_rows = [row for row in base_rows if bool(row["agent_enabled"])]

        all_hits = []
        library_diagnostics: list[dict[str, Any]] = []
        dense_errors: list[str] = []
        resolved_configs: list[dict[str, Any]] = []
        for base in base_rows:
            base_id = str(base["id"])
            retrieval_config = _normalize_retrieval_config(
                _json_object(base["retrieval_config_json"], DEFAULT_RETRIEVAL_CONFIG)
            )
            if mode is not None:
                retrieval_config["mode"] = mode
            if limit is not None:
                retrieval_config["topK"] = limit
            if threshold is not None:
                retrieval_config["threshold"] = threshold
            if rerank is not None:
                retrieval_config["rerankEnabled"] = rerank
            if rerank_candidate_depth is not None:
                retrieval_config["rerankCandidateDepth"] = rerank_candidate_depth
            retrieval_config = _normalize_retrieval_config(retrieval_config)
            if agent_only:
                # Base-level tuning remains authoritative when the Tool does not
                # override topK, but an Agent read must stay bounded even if a
                # management profile is configured for a wider analyst view.
                retrieval_config["topK"] = min(
                    AGENT_SEARCH_TOP_K_LIMIT,
                    int(retrieval_config["topK"]),
                )
            resolved_configs.append(retrieval_config)
            requested_mode = str(retrieval_config["mode"])
            document_ids: Sequence[str] = ()
            if file_name:
                document_ids = self.store.document_ids_for_file_name(
                    file_name,
                    base_ids=(base_id,),
                    agent_only=agent_only,
                )
                if not document_ids:
                    library_diagnostics.append(
                        {
                            "kbId": base_id,
                            "kbName": str(base["name"]),
                            "config": retrieval_config,
                            "effectiveMode": "no-matching-file",
                            "candidateLimit": 0,
                            "lexicalCandidates": 0,
                            "denseCandidates": 0,
                            "returned": 0,
                        }
                    )
                    continue
            candidate_limit = min(
                100,
                max(
                    int(retrieval_config["topK"])
                    * int(retrieval_config["candidateMultiplier"]),
                    (
                        int(retrieval_config["rerankCandidateDepth"])
                        if retrieval_config["rerankEnabled"]
                        else 0
                    ),
                ),
            )
            lexical_hits = (
                self.store.search(
                    query,
                    base_ids=(base_id,),
                    limit=candidate_limit,
                    agent_only=agent_only,
                    document_ids=document_ids,
                )
                if requested_mode in {"lexical", "hybrid"}
                else []
            )
            dense_scored: Sequence[tuple[str, float]] = ()
            if requested_mode in {"dense", "hybrid"}:
                try:
                    dense_scored = self.dense_index.search(
                        query,
                        base_ids=(base_id,),
                        limit=candidate_limit,
                        document_ids=document_ids,
                    )
                except Exception as exc:
                    dense_errors.append(f"{base_id}: {exc}")
            dense_hits = self.store.hydrate_dense_hits(
                dense_scored,
                base_ids=(base_id,),
                agent_only=agent_only,
                document_ids=document_ids,
            )
            graph_result: dict[str, Any] = {"status": "not-requested", "items": [], "matchedNodes": []}
            graph_hits: list[Any] = []
            graph_requested = (
                requested_mode == "hybrid"
                and bool(retrieval_config["graphEnabled"])
                and float(retrieval_config["graphWeight"]) > 0.0
            )
            if requested_mode == "hybrid" and not graph_requested:
                graph_result["status"] = "disabled"
            if graph_requested:
                seed_chunk_ids = tuple(dict.fromkeys(
                    [hit.chunk_id for hit in lexical_hits[:10]]
                    + [hit.chunk_id for hit in dense_hits[:10]]
                ))
                graph_result = self.graph.retrieval_candidates(
                    base_id,
                    query,
                    seed_chunk_ids=seed_chunk_ids,
                    limit=candidate_limit,
                )
                graph_items = list(graph_result.get("items") or [])
                graph_by_id = {str(item.get("chunkId") or ""): item for item in graph_items}
                hydrated_graph_hits = self.store.hydrate_graph_hits(
                    [
                        (str(item.get("chunkId") or ""), float(item.get("score") or 0.0))
                        for item in graph_items
                        if str(item.get("chunkId") or "")
                    ],
                    base_ids=(base_id,),
                    agent_only=agent_only,
                    document_ids=document_ids,
                )
                graph_hits = [
                    replace(
                        hit,
                        diagnostics={
                            "graphMatches": list(graph_by_id.get(hit.chunk_id, {}).get("matchedNodes") or []),
                            "graphPaths": list(graph_by_id.get(hit.chunk_id, {}).get("paths") or []),
                        },
                    )
                    for hit in hydrated_graph_hits
                ]
            ranked, effective_mode = _rank_retrieval_hits(
                lexical_hits,
                dense_hits,
                graph_hits,
                requested_mode=requested_mode,
                config=retrieval_config,
            )
            ranked_candidates = [
                hit for hit in ranked if hit.score >= float(retrieval_config["threshold"])
            ]
            rerank_applied = bool(retrieval_config["rerankEnabled"])
            rerank_candidate_count = 0
            if rerank_applied:
                rerank_candidate_count = min(
                    len(ranked_candidates),
                    int(retrieval_config["rerankCandidateDepth"]),
                )
                ranked = self._rerank_hits(
                    query,
                    ranked_candidates[:rerank_candidate_count],
                    limit=int(retrieval_config["topK"]),
                    candidate_limit=int(retrieval_config["rerankCandidateDepth"]),
                )
            else:
                ranked = ranked_candidates[: int(retrieval_config["topK"])]
            all_hits.extend(ranked)
            library_diagnostics.append(
                {
                    "kbId": base_id,
                    "kbName": str(base["name"]),
                    "config": retrieval_config,
                    "effectiveMode": effective_mode,
                    "candidateLimit": candidate_limit,
                    "lexicalCandidates": len(lexical_hits),
                    "denseCandidates": len(dense_hits),
                    "graphCandidates": len(graph_hits),
                    "graphStatus": str(graph_result.get("status") or "unavailable"),
                    "graphMatchedNodes": len(list(graph_result.get("matchedNodes") or [])),
                    "graphRelationRanking": (
                        dict(graph_result["relationRanking"])
                        if isinstance(graph_result.get("relationRanking"), dict)
                        else None
                    ),
                    "rerankApplied": rerank_applied,
                    "rerankCandidates": rerank_candidate_count,
                    "returned": len(ranked),
                }
            )
        self._dense_error = "; ".join(dense_errors)[:2_000]
        result_limit = max(1, min(100, int(limit or max((item["topK"] for item in resolved_configs), default=10))))
        all_hits.sort(key=lambda hit: (-hit.score, hit.base_id, hit.chunk_id))
        all_hits = all_hits[:result_limit]
        requested_modes = {str(item["mode"]) for item in resolved_configs}
        effective_modes = {str(item["effectiveMode"]) for item in library_diagnostics}
        return {
            "schemaVersion": KNOWLEDGE_SCHEMA_VERSION,
            "query": query,
            "fileName": file_name or None,
            "hits": [hit.to_dict() for hit in all_hits],
            "total": len(all_hits),
            "retrieval": {
                "mode": next(iter(requested_modes)) if len(requested_modes) == 1 else "per-library",
                "effectiveMode": next(iter(effective_modes)) if len(effective_modes) == 1 else "mixed",
                "config": resolved_configs[0] if len(resolved_configs) == 1 else None,
                "libraries": library_diagnostics,
                "lexicalAvailable": True,
                "dense": self._dense_status(),
                "reranker": self._reranker_status(),
            },
        }

    def find(
        self,
        query: str,
        *,
        base_ids: Sequence[str] = (),
        limit: int = 20,
        agent_only: bool = False,
    ) -> dict[str, Any]:
        query = _query(query)
        rows = self.store.find_documents(
            query,
            base_ids=tuple(_identifier(item, "base id") for item in base_ids),
            limit=limit,
            agent_only=agent_only,
        )
        documents = [_document_to_dict(row, include_error=False) for row in rows]
        return {"schemaVersion": KNOWLEDGE_SCHEMA_VERSION, "query": query, "documents": documents, "total": len(documents)}

    def find_in_document(
        self,
        document_id: str,
        patterns: Sequence[str],
        *,
        use_regex: bool = False,
        case_sensitive: bool = False,
        max_windows: int = 8,
        window_size: int = 24,
        offset: int = 0,
        agent_only: bool = False,
    ) -> dict[str, Any]:
        document_id = _identifier(document_id, "document id")
        normalized_patterns = tuple(_query(pattern)[:240] for pattern in patterns[:10])
        if not normalized_patterns:
            raise KnowledgeLibraryError("find requires at least one pattern", code="invalid_argument")
        matchers = _document_matchers(
            normalized_patterns,
            use_regex=use_regex,
            case_sensitive=case_sensitive,
        )
        document = self.store.get_document(document_id)
        if str(document["status"]) != "ready" or (agent_only and not bool(document["base_agent_enabled"])):
            raise KnowledgeNotFoundError(f"document {document_id!r} is not available")
        page_limit = max(1, min(20, int(max_windows)))
        chunk_offset = max(0, int(offset))
        if use_regex:
            rows = _regex_document_rows(
                self.store,
                document_id,
                matchers,
                offset=chunk_offset,
                limit=page_limit + 1,
            )
        else:
            rows = self.store.find_document_chunks(
                document_id,
                normalized_patterns,
                case_sensitive=case_sensitive,
                offset=chunk_offset,
                limit=page_limit + 1,
                agent_only=agent_only,
            )
        has_more = len(rows) > page_limit
        rows = rows[:page_limit]
        windows: list[dict[str, Any]] = []
        for row in rows:
            content = str(row["content"] or "")
            lines = content.splitlines() or [content]
            lines_before = int(row["lines_before"] or 0)
            for line_index, line in enumerate(lines):
                matched_pattern = next((pattern for pattern, matcher in matchers if matcher(line)), "")
                if not matched_pattern:
                    continue
                radius = max(1, min(120, int(window_size))) // 2
                start_index = max(0, line_index - radius)
                end_index = min(len(lines), line_index + radius + 1)
                windows.append(
                    {
                        "kbId": str(document["base_id"]),
                        "kbName": str(document["base_name"]),
                        "fileId": document_id,
                        "fileName": str(document["display_name"]),
                        "chunkId": str(row["id"]),
                        "pattern": matched_pattern,
                        "content": "\n".join(lines[start_index:end_index]),
                        "lineStart": lines_before + start_index + 1,
                        "lineEnd": lines_before + end_index,
                        "matchLine": lines_before + line_index + 1,
                        "page": int(row["page"]) if row["page"] is not None else None,
                        "heading": str(row["heading"] or "") or None,
                    }
                )
                # Pagination advances by matching chunk ordinal. One bounded
                # context window per chunk avoids ambiguous intra-chunk cursors.
                break
        next_offset = int(rows[-1]["ordinal"]) + 1 if rows and has_more else None
        return {
            "schemaVersion": KNOWLEDGE_SCHEMA_VERSION,
            "kbId": str(document["base_id"]),
            "fileId": document_id,
            "patterns": list(normalized_patterns),
            "windows": windows,
            "items": windows,
            "total": len(windows),
            "offset": chunk_offset,
            "nextOffset": next_offset,
            "hasMore": has_more,
        }

    def open(
        self,
        chunk_id: str,
        *,
        before: int = 1,
        after: int = 1,
        agent_only: bool = False,
    ) -> dict[str, Any]:
        chunk_id = _identifier(chunk_id, "chunk id")
        rows = self.store.open_chunk(
            chunk_id,
            before=max(0, min(10, int(before))),
            after=max(0, min(10, int(after))),
            agent_only=agent_only,
        )
        chunks = [
            {
                "chunkId": str(row["id"]),
                "ordinal": int(row["ordinal"]),
                "content": str(row["content"]),
                "page": int(row["page"]) if row["page"] is not None else None,
                "heading": str(row["heading"] or "") or None,
            }
            for row in rows
        ]
        first = rows[0]
        return {
            "schemaVersion": KNOWLEDGE_SCHEMA_VERSION,
            "baseId": str(first["base_id"]),
            "baseName": str(first["base_name"]),
            "documentId": str(first["document_id"]),
            "documentName": str(first["document_name"]),
            "anchorChunkId": chunk_id,
            "chunks": chunks,
        }

    def open_document(
        self,
        document_id: str,
        *,
        offset: int = 0,
        limit: int = 5,
        agent_only: bool = False,
    ) -> dict[str, Any]:
        document_id = _identifier(document_id, "document id")
        rows = self.store.open_document(
            document_id,
            offset=max(0, int(offset)),
            limit=max(1, min(50, int(limit))),
            agent_only=agent_only,
        )
        if not rows:
            document = self.store.get_document(document_id)
            return {
                "schemaVersion": KNOWLEDGE_SCHEMA_VERSION,
                "baseId": str(document["base_id"]),
                "baseName": str(document["base_name"]),
                "documentId": document_id,
                "documentName": str(document["display_name"]),
                "chunks": [],
            }
        first = rows[0]
        return {
            "schemaVersion": KNOWLEDGE_SCHEMA_VERSION,
            "baseId": str(first["base_id"]),
            "baseName": str(first["base_name"]),
            "documentId": document_id,
            "documentName": str(first["document_name"]),
            "chunks": [
                {
                    "chunkId": str(row["id"]),
                    "ordinal": int(row["ordinal"]),
                    "content": str(row["content"]),
                    "page": int(row["page"]) if row["page"] is not None else None,
                    "heading": str(row["heading"] or "") or None,
                }
                for row in rows
            ],
        }

    def open_document_lines(
        self,
        document_id: str,
        *,
        line_start: int = 1,
        line_limit: int = 180,
        agent_only: bool = False,
    ) -> dict[str, Any]:
        document_id = _identifier(document_id, "document id")
        requested_start = max(1, int(line_start))
        requested_limit = max(1, min(300, int(line_limit)))
        rows, total_lines = self.store.open_document_lines(
            document_id,
            line_start=requested_start,
            line_limit=requested_limit,
            agent_only=agent_only,
        )
        document = self.store.get_document(document_id)
        requested_end = requested_start + requested_limit - 1
        chunks: list[dict[str, Any]] = []
        for row in rows:
            chunk_start = int(row["lines_before"]) + 1
            chunk_end = chunk_start + int(row["line_count"]) - 1
            visible_start = max(requested_start, chunk_start)
            visible_end = min(requested_end, chunk_end)
            lines = (str(row["content"] or "").splitlines() or [str(row["content"] or "")])
            local_start = visible_start - chunk_start
            local_end = visible_end - chunk_start + 1
            chunks.append(
                {
                    "chunkId": str(row["id"]),
                    "ordinal": int(row["ordinal"]),
                    "content": "\n".join(lines[local_start:local_end]),
                    "page": int(row["page"]) if row["page"] is not None else None,
                    "heading": str(row["heading"] or "") or None,
                    "lineStart": visible_start,
                    "lineEnd": visible_end,
                }
            )
        returned_end = chunks[-1]["lineEnd"] if chunks else min(total_lines, requested_end)
        return {
            "schemaVersion": KNOWLEDGE_SCHEMA_VERSION,
            "baseId": str(document["base_id"]),
            "baseName": str(document["base_name"]),
            "documentId": document_id,
            "documentName": str(document["display_name"]),
            "lineStart": requested_start,
            "lineEnd": returned_end,
            "totalLines": total_lines,
            "nextLineStart": returned_end + 1 if returned_end < total_lines else None,
            "hasMore": returned_end < total_lines,
            "chunks": chunks,
        }

    def status(self) -> dict[str, Any]:
        return {
            "schemaVersion": KNOWLEDGE_SCHEMA_VERSION,
            "status": "ready",
            "database": {"path": str(self.config.database_path), "authoritative": True, "fts5": True},
            "storage": {"root": str(self.config.root_dir), "files": str(self.config.files_dir)},
            "mineru": {
                "enabled": self.config.mineru_enabled,
                "port": self.config.mineru_port,
            },
            "chunking": {
                "strategies": list(CHUNKING_STRATEGIES),
                "presets": list(CHUNKING_STRATEGIES),
                "default": dict(DEFAULT_CHUNKING_CONFIG),
                "semanticChunking": False,
            },
            "dense": self._dense_status(),
            "reranker": self._reranker_status(),
            **self.store.counts(),
        }

    def _rerank_hits(
        self,
        query: str,
        hits: Sequence[Any],
        *,
        limit: int,
        candidate_limit: int,
    ) -> list[Any]:
        reranker = self.reranker
        status = self._reranker_status()
        if reranker is None or status.get("configured") is not True:
            raise KnowledgeLibraryError(
                "knowledge reranker is enabled but not configured",
                code="reranker_unavailable",
            )
        bounded_candidates = list(hits[: max(1, min(100, int(candidate_limit)))])
        if not bounded_candidates:
            return []
        original_by_id = {hit.chunk_id: hit for hit in bounded_candidates}
        original_ranks = {
            hit.chunk_id: rank for rank, hit in enumerate(bounded_candidates, start=1)
        }
        payloads = [hit.to_dict() for hit in bounded_candidates]
        try:
            reranked = reranker.rerank(
                query,
                payloads,
                limit=max(1, min(int(limit), len(payloads))),
                candidate_limit=len(payloads),
            )
        except Exception as exc:
            raise KnowledgeLibraryError(
                "knowledge reranker failed",
                code="reranker_failed",
            ) from exc
        output: list[Any] = []
        seen: set[str] = set()
        public_status = self._reranker_status()
        for final_rank, item in enumerate(reranked, start=1):
            if not isinstance(item, dict):
                raise KnowledgeLibraryError(
                    "knowledge reranker returned an invalid result",
                    code="reranker_invalid_result",
                )
            chunk_id = str(item.get("chunkId") or "")
            if not chunk_id or chunk_id in seen or chunk_id not in original_by_id:
                raise KnowledgeLibraryError(
                    "knowledge reranker returned an unknown or duplicate candidate",
                    code="reranker_invalid_result",
                )
            raw_score = item.get("rerankScore")
            if isinstance(raw_score, bool):
                raw_score = None
            try:
                score = float(raw_score)
            except (TypeError, ValueError) as exc:
                raise KnowledgeLibraryError(
                    "knowledge reranker omitted a numeric score",
                    code="reranker_invalid_result",
                ) from exc
            if not math.isfinite(score) or not 0.0 <= score <= 1.0:
                raise KnowledgeLibraryError(
                    "knowledge reranker score must be between 0 and 1",
                    code="reranker_invalid_result",
                )
            seen.add(chunk_id)
            hit = original_by_id[chunk_id]
            output.append(
                replace(
                    hit,
                    score=round(score, 8),
                    diagnostics={
                        **dict(hit.diagnostics),
                        "retrievalScore": hit.score,
                        "retrievalRank": original_ranks[chunk_id],
                        "rerankScore": round(score, 8),
                        "rerankRank": final_rank,
                        "rerankProvider": str(public_status.get("provider") or ""),
                        "rerankFingerprint": str(public_status.get("fingerprint") or ""),
                        "independentRerankStage": True,
                        "subagentSubstitute": False,
                    },
                )
            )
        return output

    def _reranker_status(self) -> dict[str, Any]:
        if self.reranker is None:
            return {
                "provider": "none",
                "configured": False,
                "independentStage": True,
                "subagentSubstitute": False,
                "fallbackCount": 0,
            }
        try:
            status = dict(self.reranker.status())
        except Exception:
            status = {
                "provider": "unknown",
                "configured": False,
                "errorCount": 1,
            }
        status["independentStage"] = True
        status["subagentSubstitute"] = False
        status.setdefault("fallbackCount", 0)
        return status

    def mineru_health(self) -> dict[str, Any]:
        if not self.config.mineru_enabled:
            return {"status": "disabled", "enabled": False, "port": self.config.mineru_port}
        return {"enabled": True, **self.parsers.mineru.health()}

    def _dispatch_document(self, row: Any, *, parser_mode: str, job_kind: str = "parse_and_index") -> dict[str, Any]:
        document_id = str(row["id"])
        revision = int(row["revision"])
        job_id = uuid.uuid4().hex
        timestamp = now_ms()
        self.store.insert_job(
            {
                "id": job_id,
                "document_id": document_id,
                "revision": revision,
                "kind": job_kind,
                "parser_mode": parser_mode,
                "status": "queued",
                "stage": "queued",
                "created_at_ms": timestamp,
                "updated_at_ms": timestamp,
            }
        )
        if self._job_executor is not None:
            self._job_executor.submit(
                self._process_document_locked,
                row,
                parser_mode=parser_mode,
                job_kind=job_kind,
                job_id=job_id,
            )
            return _document_to_dict(self.store.get_document(document_id))
        return self._process_document_locked(
            row,
            parser_mode=parser_mode,
            job_kind=job_kind,
            job_id=job_id,
        )

    def _recover_incomplete_jobs(self) -> None:
        if self._job_executor is None:
            return
        for job in self.store.recoverable_jobs():
            job_id = str(job["id"])
            document_id = str(job["document_id"])
            if int(job["revision"]) != int(job["document_revision"]):
                self.store.update_job(
                    job_id,
                    {"status": "superseded", "stage": "stale_result_dropped", "finished_at_ms": now_ms()},
                )
                continue
            try:
                row = self.store.get_document(document_id)
            except KnowledgeNotFoundError:
                continue
            parser_mode = _parser_mode(str(job["parser_mode"] or "auto"))
            self.store.reset_job_for_recovery(job_id)
            self._job_executor.submit(
                self._process_document_locked,
                row,
                parser_mode=parser_mode,
                job_kind=str(job["kind"]),
                job_id=job_id,
            )

    def _process_document_locked(
        self,
        row: Any,
        *,
        parser_mode: str,
        job_kind: str,
        job_id: str,
    ) -> dict[str, Any]:
        with self._ingest_lock:
            return self._process_document(row, parser_mode=parser_mode, job_kind=job_kind, job_id=job_id)

    def _process_document(
        self,
        row: Any,
        *,
        parser_mode: str,
        job_kind: str,
        job_id: str,
    ) -> dict[str, Any]:
        document_id = str(row["id"])
        base_id = str(row["base_id"])
        revision = int(row["revision"])
        if self.store.job_is_cancelled(job_id):
            return self._document_result_or_deleted(document_id, base_id=base_id)
        base = self.store.get_base(base_id)
        chunking_config = _normalize_chunking_config(
            _json_object(base["chunking_config_json"], DEFAULT_CHUNKING_CONFIG),
            defaults=DEFAULT_CHUNKING_CONFIG,
        )
        params_hash = hashlib.sha256(
            json.dumps({"mode": parser_mode, "chunking": chunking_config}, sort_keys=True).encode("utf-8")
        ).hexdigest()
        try:
            self.store.update_document(document_id, {"status": "parsing", "parser_params_hash": params_hash})
            self.store.update_job(job_id, {"status": "running", "stage": "parsing", "started_at_ms": now_ms()})
            parsed = self.parsers.parse(Path(str(row["stored_path"])), mode=parser_mode)
            if self.store.job_is_cancelled(job_id):
                return self._document_result_or_deleted(document_id, base_id=base_id)
            chunks = _chunk_document(
                parsed,
                document_id=document_id,
                base_id=base_id,
                chunking_config=chunking_config,
            )
            if not chunks:
                raise DocumentParseError("document produced no indexable chunks", code="empty_document")
            self.store.update_document(document_id, {"status": "indexing"})
            self.store.update_job(job_id, {"stage": "indexing"})
            asset_links = self._store_assets(parsed)
            output_hash = hashlib.sha256(parsed.text.encode("utf-8")).hexdigest()
            artifact_path = self._store_artifact(base_id, document_id, revision, parsed.text)
            if self.store.job_is_cancelled(job_id):
                _unlink_quietly(artifact_path)
                self._remove_unreferenced_assets()
                return self._document_result_or_deleted(document_id, base_id=base_id)
            applied = self.store.replace_chunks_if_revision(
                document_id=document_id,
                revision=revision,
                chunks=chunks,
                document_fields={
                    "status": "ready",
                    "parser_provider": parsed.provider,
                    "parser_version": parsed.provider_version,
                    "output_hash": output_hash,
                    "chunk_count": len(chunks),
                    "error_code": "",
                    "error_message": "",
                    "metadata_json": json.dumps(parsed.metadata, ensure_ascii=False, sort_keys=True),
                    "artifact_path": str(artifact_path),
                    "indexed_config_revision": int(base["config_revision"]),
                },
            )
            if not applied:
                self.store.update_job(
                    job_id,
                    {"status": "superseded", "stage": "stale_result_dropped", "finished_at_ms": now_ms()},
                )
                _unlink_quietly(artifact_path)
                self._remove_unreferenced_assets()
                return self._document_result_or_deleted(document_id, base_id=base_id)
            self.store.replace_document_asset_links(document_id, asset_links)
            self._remove_unreferenced_assets()
            self._remove_superseded_artifacts(base_id, document_id, keep=artifact_path)
            try:
                self.dense_index.replace_document(document_id, chunks)
                self._dense_error = ""
            except Exception as exc:
                self._dense_error = str(exc)
            self.store.update_job(job_id, {"status": "succeeded", "stage": "ready", "finished_at_ms": now_ms()})
        except Exception as exc:
            error = exc if isinstance(exc, KnowledgeLibraryError) else DocumentParseError(str(exc))
            try:
                current = self.store.get_document(document_id)
            except KnowledgeNotFoundError:
                current = None
            if current is not None and int(current["revision"]) == revision:
                self.store.update_document(
                    document_id,
                    {"status": "failed", "error_code": error.code, "error_message": str(error)[:2_000]},
                )
            if not self.store.job_is_cancelled(job_id):
                self.store.update_job(
                    job_id,
                    {
                        "status": "failed",
                        "stage": "failed",
                        "error_code": error.code,
                        "error_message": str(error)[:2_000],
                        "finished_at_ms": now_ms(),
                    },
                )
        return self._document_result_or_deleted(document_id, base_id=base_id)

    def _store_assets(self, parsed: ParsedDocument) -> list[tuple[str, str]]:
        links: list[tuple[str, str]] = []
        for asset in parsed.assets:
            suffix = mimetypes.guess_extension(asset.media_type) or ""
            path = self.config.assets_dir / f"{asset.sha256}{suffix}"
            if not path.exists():
                temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
                temporary.write_bytes(asset.data)
                secure_file(temporary)
                os.replace(temporary, path)
            secure_file(path)
            self.store.add_asset(
                sha256=asset.sha256,
                media_type=asset.media_type,
                byte_size=len(asset.data),
                stored_path=str(path),
            )
            links.append((asset.sha256, asset.name))
        return links

    def _remove_unreferenced_assets(self) -> None:
        for path in self.store.delete_unreferenced_assets():
            _unlink_quietly(Path(path))

    def _document_result_or_deleted(self, document_id: str, *, base_id: str) -> dict[str, Any]:
        try:
            return _document_to_dict(self.store.get_document(document_id))
        except KnowledgeNotFoundError:
            return {
                "schemaVersion": KNOWLEDGE_SCHEMA_VERSION,
                "documentId": document_id,
                "baseId": base_id,
                "status": "deleting",
            }

    def _store_artifact(self, base_id: str, document_id: str, revision: int, text: str) -> Path:
        directory = self.config.artifacts_dir / base_id / document_id
        secure_directory(directory)
        path = directory / f"revision-{revision}.md"
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(text, encoding="utf-8")
        secure_file(temporary)
        os.replace(temporary, path)
        secure_file(path)
        return path

    def _remove_superseded_artifacts(self, base_id: str, document_id: str, *, keep: Path) -> None:
        directory = self.config.artifacts_dir / base_id / document_id
        for candidate in directory.glob("revision-*.md"):
            if candidate != keep:
                _unlink_quietly(candidate)

    def _artifact_window(self, document: Any, *, line_offset: int, line_limit: int) -> dict[str, Any]:
        raw_path = str(document["artifact_path"] or "")
        if not raw_path:
            return {
                "artifact": {"available": False, "format": "markdown"},
                "contentWindow": {"items": [], "offset": line_offset, "limit": line_limit, "total": 0, "hasMore": False},
            }
        try:
            path = _safe_stored_path(Path(raw_path), root=self.config.artifacts_dir)
            data = path.read_bytes()
        except (OSError, KnowledgeLibraryError):
            return {
                "artifact": {"available": False, "format": "markdown"},
                "contentWindow": {"items": [], "offset": line_offset, "limit": line_limit, "total": 0, "hasMore": False},
            }
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        window = lines[line_offset : line_offset + line_limit]
        return {
            "artifact": {
                "available": True,
                "format": "markdown",
                "mimeType": "text/markdown; charset=utf-8",
                "byteSize": len(data),
                "lineCount": len(lines),
                "sha256": hashlib.sha256(data).hexdigest(),
            },
            "contentWindow": {
                "items": [
                    {"lineNumber": line_offset + index + 1, "content": content}
                    for index, content in enumerate(window)
                ],
                "offset": line_offset,
                "limit": line_limit,
                "total": len(lines),
                "hasMore": line_offset + len(window) < len(lines),
            },
        }

    def _artifact_tables(self, document: Any) -> list[dict[str, Any]]:
        raw_path = str(document["artifact_path"] or "")
        if not raw_path:
            return []
        try:
            path = _safe_stored_path(Path(raw_path), root=self.config.artifacts_dir)
            text = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, KnowledgeLibraryError):
            return []
        return _extract_table_artifacts(text)

    def _delete_dense(self, document_id: str) -> None:
        try:
            self.dense_index.delete_document(document_id)
            self._dense_error = ""
        except Exception as exc:
            self._dense_error = str(exc)

    def _dense_status(self) -> dict[str, Any]:
        try:
            status = dict(self.dense_index.status())
        except Exception as exc:
            status = {"available": False, "degraded": True, "reason": str(exc)}
        if self._dense_error:
            status.update({"available": False, "degraded": True, "reason": self._dense_error})
        return status




def _regex_document_rows(
    store: KnowledgeStore,
    document_id: str,
    matchers: Sequence[tuple[str, Any]],
    *,
    offset: int,
    limit: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    scan_offset = 0
    lines_before = 0
    while len(rows) < limit:
        batch, total = store.document_chunks(document_id, offset=scan_offset, limit=200)
        if not batch:
            break
        for row in batch:
            content = str(row["content"] or "")
            line_count = max(1, len(content.splitlines()))
            if int(row["ordinal"]) >= offset and any(
                matcher(line)
                for line in (content.splitlines() or [content])
                for _pattern, matcher in matchers
            ):
                item = dict(row)
                item["lines_before"] = lines_before
                rows.append(item)
                if len(rows) >= limit:
                    break
            lines_before += line_count
        scan_offset += len(batch)
        if scan_offset >= total:
            break
    return rows


def _chunk_document(
    parsed: ParsedDocument,
    *,
    document_id: str,
    base_id: str,
    chunking_config: dict[str, Any],
) -> list[dict[str, Any]]:
    chunk_chars = int(chunking_config["size"])
    overlap_chars = int(chunking_config["overlap"])
    strategy = str(chunking_config["strategy"])
    separator = str(chunking_config.get("separator") or "\n\n")
    respect_headings = bool(chunking_config["respectHeadings"])
    respect_pages = bool(chunking_config["respectPageBoundaries"])
    chunks: list[dict[str, Any]] = []
    ordinal = 0
    current_heading = ""
    has_page_metadata = parsed.metadata.get("pageSeparator") == "\f" or "\f" in parsed.text
    page_texts = parsed.text.split("\f") if respect_pages else [parsed.text.replace("\f", "\n\n")]
    for page_index, page_text in enumerate(page_texts, start=1):
        page = page_index if respect_pages and has_page_metadata else None
        blocks = _chunk_strategy_blocks(page_text, strategy=strategy, separator=separator)
        buffer = ""
        buffer_heading = current_heading
        for block in blocks:
            block_heading = _chunk_block_heading(block, strategy=strategy)
            if block_heading and respect_headings:
                current_heading = block_heading[:300]
                if not buffer:
                    buffer_heading = current_heading
            candidate = f"{buffer}\n\n{block}".strip() if buffer else block
            if len(candidate) <= chunk_chars:
                buffer = candidate
                continue
            if buffer:
                chunks.append(_chunk_record(document_id, base_id, ordinal, buffer, buffer_heading, page))
                ordinal += 1
                overlap = buffer[-overlap_chars:].lstrip() if overlap_chars else ""
                buffer = overlap
                buffer_heading = current_heading
            remainder = f"{buffer}\n\n{block}".strip() if buffer else block
            while len(remainder) > chunk_chars:
                split_at = chunk_chars
                if strategy != "fixed":
                    boundary = max(remainder.rfind("\n", chunk_chars // 2, chunk_chars), remainder.rfind(" ", chunk_chars // 2, chunk_chars))
                    if boundary > chunk_chars // 2:
                        split_at = boundary
                content = remainder[:split_at].strip()
                if content:
                    chunks.append(_chunk_record(document_id, base_id, ordinal, content, current_heading, page))
                    ordinal += 1
                next_start = max(1, split_at - overlap_chars)
                remainder = remainder[next_start:].lstrip()
            buffer = remainder
        if buffer:
            chunks.append(_chunk_record(document_id, base_id, ordinal, buffer, buffer_heading, page))
            ordinal += 1
    return chunks


def _chunk_strategy_blocks(text: str, *, strategy: str, separator: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if strategy == "fixed":
        return [text]
    if strategy == "separator":
        return [item.strip() for item in text.split(separator) if item.strip()]
    if strategy == "markdown":
        pattern = r"(?m)(?=^[ \t]{0,3}#{1,6}[ \t]+)|\n\s*\n"
    elif strategy == "book":
        pattern = (
            r"(?im)(?=^[ \t]{0,3}#{1,6}[ \t]+|^第[零〇一二三四五六七八九十百千万0-9]+[章节篇部卷][^\n]*$|"
            r"^chapter[ \t]+[0-9ivxlcdm]+\b)"
        )
    elif strategy == "qa":
        pattern = r"(?im)(?=^(?:q(?:uestion)?[ \t]*[:：]|问题[ \t]*[:：]|问[ \t]*[:：]|\d+[.)、][ \t]+))"
    elif strategy == "laws":
        pattern = r"(?m)(?=^第[零〇一二三四五六七八九十百千万0-9]+条(?:之[一二三四五六七八九十0-9]+)?[ \t]*)"
    else:
        pattern = r"\n\s*\n"
    blocks = [item.strip() for item in re.split(pattern, text) if item.strip()]
    if len(blocks) == 1 and strategy in {"book", "qa", "laws"}:
        return [item.strip() for item in re.split(r"\n\s*\n", text) if item.strip()]
    return blocks


def _chunk_block_heading(block: str, *, strategy: str) -> str:
    first_line = (block.splitlines() or [""])[0].strip()
    markdown = re.match(r"^#{1,6}\s+(.+?)(?:\s+#+)?$", first_line)
    if markdown:
        return markdown.group(1).strip()
    if strategy == "book" and re.match(
        r"(?i)^(?:第[零〇一二三四五六七八九十百千万0-9]+[章节篇部卷]|chapter\s+[0-9ivxlcdm]+\b)",
        first_line,
    ):
        return first_line
    if strategy == "qa" and re.match(r"(?i)^(?:q(?:uestion)?\s*[:：]|问题\s*[:：]|问\s*[:：]|\d+[.)、]\s+)", first_line):
        return first_line
    if strategy == "laws" and re.match(r"^第[零〇一二三四五六七八九十百千万0-9]+条", first_line):
        return first_line
    return ""


def _chunk_record(
    document_id: str,
    base_id: str,
    ordinal: int,
    content: str,
    heading: str,
    page: int | None,
) -> dict[str, Any]:
    content = content.strip()
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {
        "id": uuid.uuid5(uuid.NAMESPACE_URL, f"rag-ime:{document_id}:{ordinal}:{content_hash}").hex,
        "document_id": document_id,
        "base_id": base_id,
        "ordinal": ordinal,
        "content": content,
        "heading": heading,
        "page": page,
        "content_hash": content_hash,
    }


def _job_to_dict(row: Any) -> dict[str, Any]:
    status = str(row["status"])
    stage = str(row["stage"])
    progress_by_stage = {
        "queued": 0.0,
        "recovered": 0.05,
        "parsing": 0.35,
        "indexing": 0.8,
        "ready": 1.0,
        "failed": 1.0,
        "cancelled": 1.0,
        "stale_result_dropped": 1.0,
    }
    return {
        "jobId": str(row["id"]),
        "kbId": str(row["base_id"]),
        "fileId": str(row["document_id"]),
        "fileName": str(row["file_name"]),
        "revision": int(row["revision"]),
        "kind": str(row["kind"]),
        "parserMode": str(row["parser_mode"] or "auto"),
        "status": status,
        "stage": stage,
        "progress": progress_by_stage.get(stage, 0.5 if status == "running" else 0.0),
        "cancellable": status in {"queued", "running"},
        "error": (
            {"code": str(row["error_code"]), "message": str(row["error_message"])}
            if row["error_code"] or row["error_message"]
            else None
        ),
        "createdAtMs": int(row["created_at_ms"]),
        "startedAtMs": int(row["started_at_ms"]) if row["started_at_ms"] is not None else None,
        "finishedAtMs": int(row["finished_at_ms"]) if row["finished_at_ms"] is not None else None,
        "updatedAtMs": int(row["updated_at_ms"]),
    }


def _base_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "name": str(row["name"]),
        "description": str(row["description"] or ""),
        "parserMode": str(row["parser_mode"]),
        "agentEnabled": bool(row["agent_enabled"]),
        "chunkingConfig": _json_object(row["chunking_config_json"], DEFAULT_CHUNKING_CONFIG),
        "retrievalConfig": _json_object(row["retrieval_config_json"], DEFAULT_RETRIEVAL_CONFIG),
        "configRevision": int(row["config_revision"]),
        "revision": int(row["config_revision"]),
        "documentCount": int(row["document_count"]),
        "readyDocumentCount": int(row["ready_document_count"]),
        "chunkCount": int(row["chunk_count"]),
        "createdAtMs": int(row["created_at_ms"]),
        "updatedAtMs": int(row["updated_at_ms"]),
    }


def _document_to_dict(row: Any, *, include_error: bool = True) -> dict[str, Any]:
    metadata = decode_metadata(row)
    result = {
        "id": str(row["id"]),
        "documentId": str(row["id"]),
        "kbId": str(row["base_id"]),
        "baseId": str(row["base_id"]),
        "baseName": str(row["base_name"]),
        "fileName": str(row["display_name"]),
        "sourceName": str(row["source_name"]),
        "mimeType": str(row["media_type"]),
        "byteSize": int(row["byte_size"]),
        "sha256": str(row["sha256"]),
        "status": str(row["status"]),
        "parserProvider": str(row["parser_provider"] or ""),
        "parserVersion": str(row["parser_version"] or ""),
        "revision": int(row["revision"]),
        "indexedConfigRevision": int(row["indexed_config_revision"]),
        "chunkCount": int(row["chunk_count"]),
        "pageCount": _metadata_page_count(metadata),
        "metadata": metadata,
        "createdAtMs": int(row["created_at_ms"]),
        "updatedAtMs": int(row["updated_at_ms"]),
    }
    if include_error:
        result["error"] = (
            {"code": str(row["error_code"]), "message": str(row["error_message"])}
            if row["error_code"] or row["error_message"]
            else None
        )
    return result


def _chunk_to_dict(row: Any) -> dict[str, Any]:
    return {
        "chunkId": str(row["id"]),
        "ordinal": int(row["ordinal"]),
        "content": str(row["content"]),
        "charCount": len(str(row["content"])),
        "page": int(row["page"]) if row["page"] is not None else None,
        "heading": str(row["heading"] or "") or None,
        "sha256": str(row["content_hash"]),
    }


def _asset_to_dict(row: Any, *, base_id: str, document_id: str) -> dict[str, Any]:
    asset_id = str(row["sha256"])
    return {
        "assetId": asset_id,
        "name": str(row["original_name"]),
        "mimeType": str(row["media_type"]),
        "byteSize": int(row["byte_size"]),
        "sha256": asset_id,
        "readPath": (
            f"/api/knowledge-bases/{base_id}/documents/{document_id}/assets/{asset_id}"
        ),
    }


class _BoundedHTMLTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._table_depth = 0
        self._in_caption = False
        self._caption_parts: list[str] = []
        self._row: list[tuple[str, bool]] | None = None
        self._cell_parts: list[str] | None = None
        self._cell_is_header = False
        self.rows: list[list[tuple[str, bool]]] = []

    @property
    def caption(self) -> str:
        return _bounded_table_cell(" ".join(self._caption_parts), maximum=300)

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "table":
            self._table_depth += 1
            return
        if self._table_depth != 1:
            return
        if tag == "caption":
            self._in_caption = True
        elif tag == "tr":
            self._row = []
        elif tag in {"th", "td"} and self._row is not None:
            self._cell_parts = []
            self._cell_is_header = tag == "th"
        elif tag == "br" and self._cell_parts is not None:
            self._cell_parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "table":
            self._table_depth = max(0, self._table_depth - 1)
            return
        if self._table_depth != 1:
            return
        if tag == "caption":
            self._in_caption = False
        elif tag in {"th", "td"} and self._cell_parts is not None and self._row is not None:
            if len(self._row) < _MAX_TABLE_COLUMNS:
                self._row.append((_bounded_table_cell(" ".join(self._cell_parts)), self._cell_is_header))
            self._cell_parts = None
        elif tag == "tr" and self._row is not None:
            if self._row and len(self.rows) <= _MAX_TABLE_ROWS:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._table_depth != 1:
            return
        if self._in_caption:
            self._caption_parts.append(data)
        if self._cell_parts is not None:
            self._cell_parts.append(data)


def _extract_table_artifacts(markdown: str) -> list[dict[str, Any]]:
    if not markdown:
        return []
    html_candidates, html_spans = _extract_html_tables(markdown)
    candidates = html_candidates + _extract_pipe_tables(markdown, excluded_spans=html_spans)
    candidates.sort(key=lambda item: int(item.pop("_position")))
    artifacts: list[dict[str, Any]] = []
    for index, item in enumerate(candidates[:_MAX_TABLE_ARTIFACTS], start=1):
        raw_markdown = str(item["markdown"])
        table_id = hashlib.sha256(
            f"{index}\0{raw_markdown}".encode("utf-8", errors="replace")
        ).hexdigest()[:24]
        artifacts.append(
            {
                "tableId": f"table-{table_id}",
                "title": str(item["title"] or f"表格 {index}")[:300],
                "page": item["page"],
                "columns": item["columns"],
                "rows": item["rows"],
                "markdown": raw_markdown[:_MAX_TABLE_MARKDOWN_CHARS],
            }
        )
    return artifacts


def _extract_html_tables(markdown: str) -> tuple[list[dict[str, Any]], list[tuple[int, int]]]:
    candidates: list[dict[str, Any]] = []
    spans: list[tuple[int, int]] = []
    for match in re.finditer(r"(?is)<table\b[^>]*>.*?</table\s*>", markdown):
        spans.append(match.span())
        if len(candidates) >= _MAX_TABLE_ARTIFACTS:
            continue
        parser = _BoundedHTMLTableParser()
        raw_table = markdown[match.start() : min(match.end(), match.start() + _MAX_HTML_TABLE_SCAN_CHARS)]
        try:
            parser.feed(raw_table)
            parser.close()
        except (ValueError, AssertionError):
            continue
        if not parser.rows:
            continue
        has_header = any(is_header for _, is_header in parser.rows[0])
        if has_header:
            columns = [value for value, _ in parser.rows[0]][:_MAX_TABLE_COLUMNS]
            data_rows = parser.rows[1 : _MAX_TABLE_ROWS + 1]
            width = len(columns)
        else:
            columns = [value for value, _ in parser.rows[0]][:_MAX_TABLE_COLUMNS]
            data_rows = parser.rows[1 : _MAX_TABLE_ROWS + 1]
            width = len(columns)
        if not width:
            continue
        candidates.append(
            {
                "_position": match.start(),
                "title": parser.caption or _nearest_markdown_heading(markdown, match.start()),
                "page": _artifact_page(markdown, match.start()),
                "columns": [_bounded_table_cell(value) for value in columns],
                "rows": [_normalize_table_row(row, width=width) for row in data_rows],
                "markdown": raw_table[:_MAX_TABLE_MARKDOWN_CHARS],
            }
        )
    return candidates, spans


def _extract_pipe_tables(markdown: str, *, excluded_spans: Sequence[tuple[int, int]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    lines = markdown.splitlines(keepends=True)
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)
    in_fence = False
    index = 0
    while index + 1 < len(lines) and len(candidates) < _MAX_TABLE_ARTIFACTS:
        line = lines[index].rstrip("\r\n")
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            index += 1
            continue
        position = offsets[index]
        if in_fence or _position_in_spans(position, excluded_spans):
            index += 1
            continue
        header = _split_markdown_table_row(line[:_MAX_TABLE_LINE_CHARS])
        separator = _split_markdown_table_row(lines[index + 1].rstrip("\r\n")[:_MAX_TABLE_LINE_CHARS])
        if (
            len(header) < 1
            or len(separator) != len(header)
            or "|" not in line
            or not all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in separator)
        ):
            index += 1
            continue
        width = min(len(header), _MAX_TABLE_COLUMNS)
        columns = [_bounded_table_cell(cell) or f"Column {column + 1}" for column, cell in enumerate(header[:width])]
        rows: list[list[str]] = []
        raw_lines = [line, lines[index + 1].rstrip("\r\n")]
        next_index = index + 2
        while next_index < len(lines):
            raw = lines[next_index].rstrip("\r\n")
            if _position_in_spans(offsets[next_index], excluded_spans) or "|" not in raw:
                break
            cells = _split_markdown_table_row(raw[:_MAX_TABLE_LINE_CHARS])
            if not cells:
                break
            if len(rows) < _MAX_TABLE_ROWS:
                rows.append(_normalize_table_row([(cell, False) for cell in cells], width=width))
                raw_lines.append(raw[:_MAX_TABLE_LINE_CHARS])
            next_index += 1
        candidates.append(
            {
                "_position": position,
                "title": _nearest_markdown_heading(markdown, position),
                "page": _artifact_page(markdown, position),
                "columns": columns,
                "rows": rows,
                "markdown": _bounded_table_markdown(raw_lines),
            }
        )
        index = max(next_index, index + 2)
    return candidates


def _split_markdown_table_row(value: str) -> list[str]:
    source = value.strip()
    if not source or "|" not in source:
        return []
    if source.startswith("|"):
        source = source[1:]
    if source.endswith("|") and not source.endswith("\\|"):
        source = source[:-1]
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    code_ticks = 0
    for character in source:
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "`":
            code_ticks = 0 if code_ticks else 1
            current.append(character)
        elif character == "|" and not code_ticks:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(character)
    if escaped:
        current.append("\\")
    cells.append("".join(current).strip())
    return cells


def _normalize_table_row(row: Sequence[tuple[str, bool]], *, width: int) -> list[str]:
    cells = [_bounded_table_cell(value) for value, _ in row[:width]]
    return cells + [""] * max(0, width - len(cells))


def _bounded_table_cell(value: str, *, maximum: int = _MAX_TABLE_CELL_CHARS) -> str:
    return " ".join(str(value or "").replace("\x00", " ").split())[:maximum]


def _bounded_table_markdown(lines: Sequence[str]) -> str:
    parts: list[str] = []
    remaining = _MAX_TABLE_MARKDOWN_CHARS
    for line in lines:
        if remaining <= 0:
            break
        addition = ("\n" if parts else "") + line
        parts.append(addition[:remaining])
        remaining -= len(parts[-1])
    return "".join(parts)


def _nearest_markdown_heading(markdown: str, position: int) -> str:
    window = markdown[max(0, position - 20_000) : position]
    matches = re.findall(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", window)
    return _bounded_table_cell(matches[-1], maximum=300) if matches else ""


def _artifact_page(markdown: str, position: int) -> int | None:
    return markdown.count("\f", 0, position) + 1 if "\f" in markdown else None


def _position_in_spans(position: int, spans: Sequence[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in spans)


def _metadata_page_count(metadata: dict[str, Any]) -> int:
    value = metadata.get("pageCount", 0)
    if isinstance(value, bool):
        return 0
    try:
        count = int(value)
    except (TypeError, ValueError):
        return 0
    return count if 0 < count <= 100_000 else 0


def _normalize_chunking_config(
    value: dict[str, Any] | None,
    *,
    defaults: dict[str, Any],
) -> dict[str, Any]:
    if value is not None and not isinstance(value, dict):
        raise KnowledgeLibraryError("chunkingConfig must be an object", code="invalid_argument")
    provided = dict(value or {})
    allowed = {"strategy", "preset", "size", "overlap", "separator", "respectHeadings", "respectPageBoundaries"}
    unknown = sorted(set(provided) - allowed)
    if unknown:
        raise KnowledgeLibraryError(f"unknown chunkingConfig fields: {', '.join(unknown)}", code="invalid_argument")
    merged = {**DEFAULT_CHUNKING_CONFIG, **defaults, **provided}
    if provided.get("preset") is not None and provided.get("strategy") is None:
        merged["strategy"] = provided["preset"]
    strategy = str(merged["strategy"] or "").strip().lower()
    if strategy == "paragraph":
        strategy = "general"
    if strategy not in CHUNKING_STRATEGIES:
        raise KnowledgeLibraryError(
            f"chunking strategy must be one of: {', '.join(CHUNKING_STRATEGIES)}",
            code="invalid_argument",
        )
    size = _strict_int(merged["size"], field="chunking size", minimum=200, maximum=8_000)
    overlap = _strict_int(merged["overlap"], field="chunking overlap", minimum=0, maximum=2_000)
    if overlap >= size:
        raise KnowledgeLibraryError("chunking overlap must be smaller than size", code="invalid_argument")
    separator = str(merged.get("separator") or "")
    if strategy == "separator" and not separator:
        raise KnowledgeLibraryError("separator strategy requires a non-empty separator", code="invalid_argument")
    if len(separator) > 100:
        raise KnowledgeLibraryError("chunking separator must be at most 100 characters", code="invalid_argument")
    for field in ("respectHeadings", "respectPageBoundaries"):
        if not isinstance(merged[field], bool):
            raise KnowledgeLibraryError(f"{field} must be boolean", code="invalid_argument")
    return {
        "strategy": strategy,
        "size": size,
        "overlap": overlap,
        "separator": separator,
        "respectHeadings": merged["respectHeadings"],
        "respectPageBoundaries": merged["respectPageBoundaries"],
    }


def _normalize_retrieval_config(
    value: dict[str, Any] | None,
    *,
    defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if value is not None and not isinstance(value, dict):
        raise KnowledgeLibraryError("retrievalConfig must be an object", code="invalid_argument")
    provided = dict(value or {})
    allowed = {
        "mode",
        "topK",
        "threshold",
        "lexicalWeight",
        "denseWeight",
        "graphEnabled",
        "graphWeight",
        "rrfK",
        "candidateMultiplier",
        "rerankEnabled",
        "rerankCandidateDepth",
    }
    unknown = sorted(set(provided) - allowed)
    if unknown:
        raise KnowledgeLibraryError(f"unknown retrievalConfig fields: {', '.join(unknown)}", code="invalid_argument")
    merged = {**DEFAULT_RETRIEVAL_CONFIG, **(defaults or {}), **provided}
    mode = str(merged["mode"] or "").strip().lower()
    if mode not in {"lexical", "hybrid", "dense"}:
        raise KnowledgeLibraryError("retrieval mode must be lexical, hybrid, or dense", code="invalid_argument")
    top_k = _strict_int(merged["topK"], field="retrieval topK", minimum=1, maximum=100)
    if isinstance(merged["threshold"], bool):
        raise KnowledgeLibraryError("retrieval threshold must be numeric", code="invalid_argument")
    try:
        threshold = float(merged["threshold"])
    except (TypeError, ValueError) as exc:
        raise KnowledgeLibraryError("retrieval threshold must be numeric", code="invalid_argument") from exc
    if not 0.0 <= threshold <= 1.0:
        raise KnowledgeLibraryError("retrieval threshold must be between 0 and 1", code="invalid_argument")
    lexical_weight = _strict_float(
        merged["lexicalWeight"], field="retrieval lexicalWeight", minimum=0.0, maximum=10.0
    )
    dense_weight = _strict_float(
        merged["denseWeight"], field="retrieval denseWeight", minimum=0.0, maximum=10.0
    )
    if not isinstance(merged["graphEnabled"], bool):
        raise KnowledgeLibraryError("graphEnabled must be boolean", code="invalid_argument")
    graph_enabled = merged["graphEnabled"]
    graph_weight = _strict_float(
        merged["graphWeight"], field="retrieval graphWeight", minimum=0.0, maximum=10.0
    )
    if lexical_weight + dense_weight <= 0:
        raise KnowledgeLibraryError("retrieval weights cannot both be zero", code="invalid_argument")
    rrf_k = _strict_int(merged["rrfK"], field="retrieval rrfK", minimum=1, maximum=1_000)
    candidate_multiplier = _strict_int(
        merged["candidateMultiplier"],
        field="retrieval candidateMultiplier",
        minimum=1,
        maximum=20,
    )
    if not isinstance(merged["rerankEnabled"], bool):
        raise KnowledgeLibraryError("rerankEnabled must be boolean", code="invalid_argument")
    rerank_candidate_depth = _strict_int(
        merged["rerankCandidateDepth"],
        field="retrieval rerankCandidateDepth",
        minimum=1,
        maximum=100,
    )
    if merged["rerankEnabled"] and top_k > 20:
        raise KnowledgeLibraryError(
            "reranked retrieval topK must not exceed 20",
            code="invalid_argument",
        )
    if merged["rerankEnabled"] and rerank_candidate_depth < top_k:
        raise KnowledgeLibraryError(
            "rerankCandidateDepth must be at least topK",
            code="invalid_argument",
        )
    return {
        "mode": mode,
        "topK": top_k,
        "threshold": threshold,
        "lexicalWeight": lexical_weight,
        "denseWeight": dense_weight,
        "graphEnabled": graph_enabled,
        "graphWeight": graph_weight,
        "rrfK": rrf_k,
        "candidateMultiplier": candidate_multiplier,
        "rerankEnabled": merged["rerankEnabled"],
        "rerankCandidateDepth": rerank_candidate_depth,
    }


def _json_object(value: Any, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        decoded = {}
    return {**fallback, **decoded} if isinstance(decoded, dict) else dict(fallback)


def _strict_int(value: Any, *, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise KnowledgeLibraryError(f"{field} must be an integer", code="invalid_argument")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise KnowledgeLibraryError(f"{field} must be an integer", code="invalid_argument") from exc
    if result != value and not (isinstance(value, str) and value.strip() == str(result)):
        raise KnowledgeLibraryError(f"{field} must be an integer", code="invalid_argument")
    if not minimum <= result <= maximum:
        raise KnowledgeLibraryError(f"{field} must be between {minimum} and {maximum}", code="invalid_argument")
    return result


def _strict_float(value: Any, *, field: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise KnowledgeLibraryError(f"{field} must be numeric", code="invalid_argument")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise KnowledgeLibraryError(f"{field} must be numeric", code="invalid_argument") from exc
    if not minimum <= result <= maximum:
        raise KnowledgeLibraryError(f"{field} must be between {minimum} and {maximum}", code="invalid_argument")
    return result


def _safe_stored_path(path: Path, *, root: Path) -> Path:
    if path.is_symlink():
        raise KnowledgeLibraryError("stored knowledge symlinks are not allowed", code="unsafe_storage_path")
    try:
        resolved_root = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise KnowledgeNotFoundError("stored knowledge artifact is unavailable") from exc
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise KnowledgeLibraryError("stored knowledge path escaped its storage root", code="unsafe_storage_path") from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise KnowledgeLibraryError("stored knowledge artifact is not a regular file", code="unsafe_storage_path")
    return resolved


def _reindex_preview_token(base_id: str, config_revision: int, documents: Sequence[Any]) -> str:
    material = {
        "kbId": base_id,
        "configRevision": int(config_revision),
        "documents": [
            {
                "fileId": str(row["id"]),
                "revision": int(row["revision"]),
                "status": str(row["status"]),
            }
            for row in documents
        ],
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(str(left).encode("ascii", errors="ignore"), str(right).encode("ascii"))


def _clean_name(value: str, *, field: str) -> str:
    clean = " ".join(str(value or "").replace("\x00", " ").split()).strip()
    if not clean:
        raise KnowledgeLibraryError(f"{field} is required", code="invalid_argument")
    if len(clean) > 300:
        raise KnowledgeLibraryError(f"{field} is too long", code="invalid_argument")
    return clean


def _identifier(value: str, field: str) -> str:
    clean = str(value or "").strip()
    if not clean or len(clean) > 128 or not re.fullmatch(r"[A-Za-z0-9._:-]+", clean):
        raise KnowledgeLibraryError(f"invalid {field}", code="invalid_argument")
    return clean


def _query(value: str) -> str:
    clean = " ".join(str(value or "").replace("\x00", " ").split()).strip()
    if not clean:
        raise KnowledgeLibraryError("search query is required", code="invalid_argument")
    return clean[:2_000]


def _document_matchers(
    patterns: Sequence[str],
    *,
    use_regex: bool,
    case_sensitive: bool,
) -> tuple[tuple[str, Any], ...]:
    flags = 0 if case_sensitive else re.IGNORECASE
    matchers: list[tuple[str, Any]] = []
    for pattern in patterns:
        if use_regex:
            if "(?" in pattern or re.search(r"\\[1-9]", pattern) or re.search(r"\([^)]*[+*{][^)]*\)[+*{]", pattern):
                raise KnowledgeLibraryError("unsafe regular expression", code="unsafe_pattern")
            try:
                compiled = re.compile(pattern, flags)
            except re.error as exc:
                raise KnowledgeLibraryError(f"invalid regular expression: {exc}", code="invalid_argument") from exc
            matchers.append((pattern, lambda value, regex=compiled: regex.search(value) is not None))
        else:
            needle = pattern if case_sensitive else pattern.casefold()
            matchers.append(
                (
                    pattern,
                    lambda value, expected=needle: expected in (value if case_sensitive else value.casefold()),
                )
            )
    return tuple(matchers)


def _parser_mode(value: str) -> str:
    clean = str(value or "auto").strip().lower()
    if clean not in PARSER_MODES:
        raise KnowledgeLibraryError(f"invalid parser mode: {value}", code="invalid_argument")
    return clean


def _parser_mode_for_provider(provider: str, *, fallback: str) -> str:
    clean = str(provider or "").strip().lower()
    if clean == "mineru_local_http":
        return "mineru"
    if clean == "builtin":
        return "builtin"
    return _parser_mode(fallback)


def _validated_source(path: Path, *, max_bytes: int) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise KnowledgeLibraryError("symbolic-link imports are not allowed", code="unsafe_source")
    try:
        resolved = expanded.resolve(strict=True)
        stat = resolved.stat()
    except OSError as exc:
        raise KnowledgeNotFoundError(f"source file was not found: {expanded}") from exc
    if not resolved.is_file():
        raise KnowledgeLibraryError("source must be a regular file", code="unsafe_source")
    if stat.st_size > max_bytes:
        raise KnowledgeLibraryError("source file exceeds the configured size limit", code="source_too_large")
    return resolved


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_size = 0
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
            byte_size += len(block)
    return digest.hexdigest(), byte_size


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
