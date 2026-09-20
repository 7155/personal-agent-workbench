from __future__ import annotations

import hashlib
import json
import time
import threading
from collections.abc import Mapping
from typing import Any

from .knowledge_library import AssetBlob, KnowledgeLibraryError
from .agent_knowledge_promotion import KnowledgePromotionStore
from .knowledge_worker_supervisor import KnowledgeWorkerSupervisor
from .knowledge_embedding_profile import normalize_knowledge_embedding_profile
from .management_work_contract import ManagementWorkContract, ManagementWorkError, WorkExecution


class KnowledgeControlFacade:
    """Thin 8766 management facade over the isolated 8769 worker."""

    def __init__(
        self,
        *,
        worker: KnowledgeWorkerSupervisor,
        work_contract: ManagementWorkContract,
        runtime_provider=None,
        activity_report_provider=None,
        ime_project_provider=None,
    ) -> None:
        self._ime_project_provider = ime_project_provider
        self._ime_reference = None
        self._vault_organize_lock = threading.RLock()
        self.activity_report_provider = activity_report_provider
        self.runtime_provider = runtime_provider
        self.worker = worker
        self.work_contract = work_contract
        self.knowledge_intake = KnowledgePromotionStore(work_contract.db_path)
        self.knowledge_intake.initialize()

    def vault(self, payload: Mapping[str, object]) -> dict[str, object]:
        action = payload.get('action')
        if action == 'ime_target':
            return {'project':str(self._ime_project_provider() if self._ime_project_provider else '')}
        if action == 'ime_prepare':
            import uuid
            project = str(self._ime_project_provider() if self._ime_project_provider else payload.get('project') or '').strip()
            if not project:
                raise ValueError('输入法当前工作区不可用，请先在 Input Studio 配置。')
            packet = self.vault({**dict(payload), 'action': 'context'})
            if not packet['markdown'].strip():
                raise ValueError('请选择笔记。')
            self._ime_reference = {'id': uuid.uuid4().hex, 'project':project,
                'payload':{'action':'context','vaultId':payload['vaultId'],'noteIds':payload['noteIds']},
                'markdown':packet['markdown'], 'expires':time.monotonic()+300}
            return {'ready':True,'targetProject':project,'notice':f'已为输入法当前工作区 {project} 准备。5 分钟内在目标输入框触发辅助，核对后点击插入；不会发送，也不改变笔记归属。'}
        if action == 'organize':
            from .vault_organizer import organize
            with self._vault_organize_lock:
                return organize(self, dict(payload))
        if action in {'adopt_memory', 'adoptions'}:
            from .vault_memory import adopt, reconcile
            return adopt(self, dict(payload)) if action == 'adopt_memory' else reconcile(self, dict(payload))
        if action in {'organize_lookup', 'organize_receipt', 'store_diary', 'organize_context', 'memory_prepare', 'memory_receipt', 'memory_links'}:
            raise ValueError('internal action')
        if action in {"graph_business", "context"}:
            from .vault_memory import reconcile
            reconcile(self, dict(payload))
        result = self.worker.management_call("management_vault", dict(payload))
        if (action == 'forget' or (action == 'pause' and payload.get('paused') is True)):
            pending = self._ime_reference
            if pending and pending['payload']['vaultId'] == payload.get('vaultId'):
                self._ime_reference = None
        if action == 'day' and payload.get('includeActivity') is True:
            if not str(payload.get('project') or '').strip():
                raise ValueError('请选择明确项目后再读取活动；不会默认归到其他项目。')
            if self.activity_report_provider is None:
                result['activityUnavailable'] = True
            else:
                result['activityReport'] = self.activity_report_provider({**dict(payload), 'noTimeline': True})
        return result

    def ime_reference(self, project):
        pending = self._ime_reference
        if not pending or pending['project'] != project or pending['expires'] < time.monotonic():
            return None
        current = self.vault(pending['payload'])
        if current['markdown'] != pending['markdown']:
            self._ime_reference = None
            raise ValueError('笔记已变化，请回 PAW 重新预览。')
        return dict(pending)

    def validate_ime_reference(self, identity, project):
        pending = self.ime_reference(project)
        valid = bool(pending and pending['id'] == identity)
        if valid:
            self._ime_reference = None
        return valid

    def list_bases(self) -> dict[str, object]:
        result = self.worker.management_call("management_list_bases")
        bases = [_public_base(item) for item in _items(result, "bases")]
        return {"schemaVersion": "rag-ime.knowledge-bases.v1", "ok": True, "items": bases, "total": len(bases)}

    def embedding_profile(self) -> dict[str, object]:
        settings = self.worker.settings_provider()
        profile = normalize_knowledge_embedding_profile(settings)
        runtime = self.health()
        dense = runtime.get("dense") if isinstance(runtime.get("dense"), Mapping) else {}
        provider = dense.get("provider") if isinstance(dense.get("provider"), Mapping) else {}
        vector_count = int(dense.get("vectorCount") or 0)
        chunk_count = int(runtime.get("chunkCount") or 0)
        provider_matches = (
            str(provider.get("provider") or "") == str(profile["provider"])
            and (
                not str(profile["model"])
                or str(provider.get("model") or "") == str(profile["model"])
            )
        )
        lexical_only = profile["provider"] == "none"
        active = lexical_only or (
            dense.get("available") is True
            and provider_matches
            and vector_count >= chunk_count
        )
        phase = "active" if active else (
            "applied_pending_rebuild" if provider_matches else "applied_pending_restart"
        )
        return {
            "schemaVersion": "rag-ime.knowledge-embedding-profile.v1",
            "ok": True,
            "profile": profile,
            "phase": phase,
            "runtime": {
                "provider": dict(provider),
                "fingerprint": str(dense.get("fingerprint") or ""),
                "dimensions": dense.get("dimensions"),
                "vectorCount": vector_count,
                "chunkCount": chunk_count,
                "coverage": vector_count / chunk_count if chunk_count else 1.0,
                "available": dense.get("available") is True,
                "degraded": dense.get("degraded") is True,
                "reason": str(dense.get("reason") or ""),
            },
            "secretsVisible": False,
        }

    def embedding_probe(self, payload: Mapping[str, object]) -> dict[str, object]:
        candidate = _embedding_candidate(payload)
        probe = self.worker.probe_embedding_profile(candidate)
        return {
            "schemaVersion": "rag-ime.knowledge-embedding-probe.v1",
            "ok": True,
            **probe,
            "secretsVisible": False,
        }

    def embedding_impact(self, payload: Mapping[str, object]) -> dict[str, object]:
        candidate = _embedding_candidate(payload)
        probe = self.worker.probe_embedding_profile(candidate)
        bases = self.list_bases()["items"]
        current = self.embedding_profile()
        current_runtime = (
            current.get("runtime") if isinstance(current.get("runtime"), Mapping) else {}
        )
        affected = [
            {
                "kbId": str(base.get("id") or ""),
                "name": str(base.get("name") or ""),
                "documentCount": int(base.get("documentCount") or 0),
                "chunkCount": int(base.get("chunkCount") or 0),
            }
            for base in bases
            if isinstance(base, Mapping)
        ]
        requires_restart = (
            str(current_runtime.get("fingerprint") or "")
            != str(probe.get("fingerprint") or "")
        )
        return {
            "schemaVersion": "rag-ime.knowledge-embedding-impact.v1",
            "ok": True,
            "candidate": candidate,
            "probe": probe,
            "currentProfileSha256": str(
                (current.get("profile") or {}).get("profileSha256")
                if isinstance(current.get("profile"), Mapping)
                else ""
            ),
            "configurationChanges": {
                f"knowledgeLibrary.embedding.{key}": value
                for key, value in candidate.items()
            },
            "requiresWorkerRestart": requires_restart,
            "requiresRebuild": requires_restart and any(
                int(item["documentCount"]) > 0 for item in affected
            ),
            "affectedBases": affected,
            "affectedBaseCount": len(affected),
            "affectedDocumentCount": sum(
                int(item["documentCount"]) for item in affected
            ),
            "affectedChunkCount": sum(int(item["chunkCount"]) for item in affected),
            "approvalRequiredForApply": True,
            "secretsVisible": False,
        }

    def create_base(self, payload: Mapping[str, object]) -> dict[str, object]:
        name = _required_text(payload.get("name"), "name", maximum=300)
        result = self.worker.management_call(
            "management_create_base",
            {
                "name": name,
                "description": _text(payload.get("description"), maximum=4_000),
                "agentEnabled": bool(payload.get("agentEnabled", False)),
                "parserMode": _parser_mode(payload.get("parserProvider")),
                "chunkingConfig": _object(payload.get("chunkingConfig"), "chunkingConfig"),
                "retrievalConfig": _object(payload.get("retrievalConfig"), "retrievalConfig"),
            },
        )
        return {"schemaVersion": "rag-ime.knowledge-base.v1", "ok": True, "base": _public_base(result)}

    def get_base(self, kb_id: str) -> dict[str, object]:
        result = self.worker.management_call("management_get_base", _identifier(kb_id, "kbId"))
        return {"schemaVersion": "rag-ime.knowledge-base.v1", "ok": True, "base": _public_base(result)}

    def update_base(self, kb_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        kb_id = _identifier(kb_id, "kbId")
        current = self.worker.management_call("management_get_base", kb_id)
        _require_revision(payload.get("expectedRevision"), _base_revision(current))
        patch: dict[str, object] = {}
        if "name" in payload:
            patch["name"] = _required_text(payload.get("name"), "name", maximum=300)
        if "description" in payload:
            patch["description"] = _text(payload.get("description"), maximum=4_000)
        if "agentEnabled" in payload:
            patch["agentEnabled"] = bool(payload.get("agentEnabled"))
        if "parserProvider" in payload:
            patch["parserMode"] = _parser_mode(payload.get("parserProvider"))
        if "chunkingConfig" in payload:
            patch["chunkingConfig"] = _object(payload.get("chunkingConfig"), "chunkingConfig")
        if "retrievalConfig" in payload:
            patch["retrievalConfig"] = _object(payload.get("retrievalConfig"), "retrievalConfig")
        patch["expectedRevision"] = _base_revision(current)
        result = self.worker.management_call("management_update_base", kb_id, patch)
        return {"schemaVersion": "rag-ime.knowledge-base.v1", "ok": True, "base": _public_base(result)}

    def delete_preview(self, kb_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        kb_id = _identifier(kb_id, "kbId")
        current = self.worker.management_call("management_get_base", kb_id)
        revision = _base_revision(current)
        _require_revision(payload.get("expectedRevision"), revision)
        return self.work_contract.create_preview(
            path_id="knowledgeBases.delete.apply",
            payload={"kbId": kb_id},
            expected_revision={"subjectRevision": revision},
            required_confirm="delete",
            summary={
                "title": "删除文档知识库",
                "items": [
                    str(current.get("name") or "当前知识库"),
                    f"删除 {int(current.get('documentCount') or 0)} 个文档及其可重建索引。",
                ],
                "risk": "R2",
            },
        )

    def delete_apply(self, kb_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        kb_id = _identifier(kb_id, "kbId")

        def current_revision(_connection: object) -> Mapping[str, object]:
            current = self.worker.management_call("management_get_base", kb_id)
            return {"subjectRevision": _base_revision(current)}

        def execute(_connection: object) -> WorkExecution:
            result = self.worker.management_call("management_delete_base", kb_id)
            return WorkExecution(
                result={"ok": True, **result},
                audit_action="knowledge_base_delete",
                target_type="document_knowledge_base",
                target_id=kb_id,
            )

        try:
            return self.work_contract.execute_apply(
                path_id="knowledgeBases.delete.apply",
                payload={"kbId": kb_id},
                preview_token=str(payload.get("previewToken") or ""),
                payload_sha256=str(payload.get("payloadSha256") or ""),
                confirm_text=str(payload.get("confirmText") or ""),
                current_revision=current_revision,
                executor=execute,
            )
        except Exception as exc:
            return self.work_contract.error_payload(exc, current_revision=current_revision(None))

    def list_documents(self, kb_id: str) -> dict[str, object]:
        result = self.worker.management_call("management_list_documents", _identifier(kb_id, "kbId"))
        documents = [_public_document(item) for item in _items(result, "documents")]
        return {"schemaVersion": "rag-ime.knowledge-documents.v1", "ok": True, "items": documents, "total": len(documents)}

    def import_document(
        self,
        kb_id: str,
        *,
        data: bytes,
        file_name: str,
        mime_type: str,
        parser_provider: str = "auto",
    ) -> dict[str, object]:
        import_id = "document-intake:" + hashlib.sha256(
            b"\0".join((str(kb_id).encode(), file_name.encode(), bytes(data)))
        ).hexdigest()[:32]
        intake = self.knowledge_intake.scan_external_before_copy(
            import_id=import_id,
            source_name=file_name,
            raw_bytes=bytes(data),
            created_at_ms=int(time.time() * 1000),
        )
        if intake["scanStatus"] != "allowed":
            raise KnowledgeLibraryError(
                "document was quarantined before managed copy",
                code="secret_quarantine",
            )
        return self.worker.management_call(
            "management_import_document",
            _identifier(kb_id, "kbId"),
            data,
            file_name=_required_text(file_name, "fileName", maximum=512),
            mime_type=_required_text(mime_type, "mimeType", maximum=200),
            parser_mode=_parser_mode(parser_provider),
            intake_receipt_id=import_id,
            intake_content_hash=str(intake["contentHash"]),
        )

    def retry_document(
        self,
        kb_id: str,
        file_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        kb_id = _identifier(kb_id, "kbId")
        file_id = _identifier(file_id, "fileId")
        result = self.worker.management_call(
            "management_retry_document",
            file_id,
            parser_mode=_parser_mode(payload.get("parserProvider")) if payload.get("parserProvider") else None,
        )
        if str(result.get("kbId") or result.get("baseId") or "") != kb_id:
            raise KnowledgeLibraryError("document is outside the selected knowledge base", code="scope_mismatch")
        return {"schemaVersion": "rag-ime.knowledge-document.v1", "ok": True, "document": _public_document(result)}

    def delete_document(self, kb_id: str, file_id: str) -> dict[str, object]:
        result = self.worker.management_call(
            "management_delete_document",
            _identifier(file_id, "fileId"),
        )
        if str(result.get("baseId") or result.get("kbId") or "") != _identifier(kb_id, "kbId"):
            raise KnowledgeLibraryError("document is outside the selected knowledge base", code="scope_mismatch")
        return {"schemaVersion": "rag-ime.knowledge-document-delete.v1", "ok": True, **result}

    def document_detail(
        self,
        kb_id: str,
        file_id: str,
        query: Mapping[str, object],
    ) -> dict[str, object]:
        result = self.worker.management_call(
            "management_document_detail",
            _identifier(kb_id, "kbId"),
            _identifier(file_id, "fileId"),
            offset=_bounded_int(query.get("offset"), default=0, minimum=0, maximum=50_000_000),
            limit=_bounded_int(query.get("limit"), default=100, minimum=1, maximum=500),
            line_offset=_bounded_int(query.get("lineOffset"), default=0, minimum=0, maximum=50_000_000),
            line_limit=_bounded_int(query.get("lineLimit"), default=200, minimum=1, maximum=1_000),
        )
        return {"ok": True, **result}

    def document_source(self, kb_id: str, file_id: str) -> AssetBlob:
        return self.worker.management_call(
            "management_read_source",
            _identifier(kb_id, "kbId"),
            _identifier(file_id, "fileId"),
        )  # type: ignore[return-value]

    def document_asset(self, kb_id: str, file_id: str, asset_id: str) -> AssetBlob:
        return self.worker.management_call(
            "management_read_asset",
            _identifier(kb_id, "kbId"),
            _identifier(file_id, "fileId"),
            _identifier(asset_id, "assetId"),
        )  # type: ignore[return-value]

    def graph(self, kb_id: str, query: Mapping[str, object]) -> dict[str, object]:
        return dict(
            self.worker.management_call(
                "management_knowledge_graph",
                _identifier(kb_id, "kbId"),
                document_id=_text(query.get("documentId"), maximum=160),
                query=_text(query.get("query"), maximum=200),
                kinds=_text(query.get("kinds"), maximum=200),
                limit=_bounded_int(query.get("limit"), default=200, minimum=10, maximum=1_000),
                depth=_bounded_int(query.get("depth"), default=2, minimum=1, maximum=5),
                exclude_chunks=str(query.get("excludeChunks") or "true").strip().lower() in {"1", "true", "yes"},
                focus_id=_text(query.get("focusId"), maximum=160),
            )
        )

    def rebuild_graph(self, kb_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        document_ids = payload.get("documentIds")
        if document_ids is not None and not isinstance(document_ids, list):
            raise KnowledgeLibraryError("documentIds must be an array", code="invalid_argument")
        return dict(
            self.worker.management_call(
                "management_rebuild_knowledge_graph",
                _identifier(kb_id, "kbId"),
                expected_revision=(
                    int(payload["expectedRevision"])
                    if payload.get("expectedRevision") is not None
                    else None
                ),
                document_ids=[_identifier(item, "documentId") for item in (document_ids or [])],
                extractor_mode=str(payload.get("extractorMode") or "deterministic"),
                model_id=_text(payload.get("modelId"), maximum=160),
                batch_size=_bounded_int(payload.get("batchSize"), default=4, minimum=1, maximum=8),
                extraction_concurrency=_bounded_int(
                    payload.get("extractionConcurrency"), default=2, minimum=1, maximum=4
                ),
                max_entities=_bounded_int(payload.get("maxEntitiesPerChunk"), default=5, minimum=1, maximum=8),
                max_relations=_bounded_int(payload.get("maxRelationsPerChunk"), default=4, minimum=0, maximum=8),
                max_topics=_bounded_int(payload.get("maxTopicsPerChunk"), default=2, minimum=0, maximum=4),
            )
        )

    def reindex_preview(self, kb_id: str) -> dict[str, object]:
        kb_id = _identifier(kb_id, "kbId")
        result = dict(self.worker.management_call("management_reindex_preview", kb_id))
        binding = {
            "kbId": kb_id,
            "previewToken": str(result.get("previewToken") or ""),
            "expectedRevision": result.get("configRevision"),
        }
        result["payloadSha256"] = _payload_sha256(binding)
        return {"ok": True, **result}

    def rebuild(self, kb_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        kb_id = _identifier(kb_id, "kbId")
        preview = dict(self.worker.management_call("management_reindex_preview", kb_id))
        expected_revision = preview.get("configRevision")
        _require_revision(payload.get("expectedRevision"), expected_revision)
        preview_token = _required_text(payload.get("previewToken"), "previewToken", maximum=128)
        if preview_token != str(preview.get("previewToken") or ""):
            raise KnowledgeLibraryError("reindex preview is stale", code="stale_preview")
        binding = {"kbId": kb_id, "previewToken": preview_token, "expectedRevision": expected_revision}
        if str(payload.get("payloadSha256") or "") != _payload_sha256(binding):
            raise KnowledgeLibraryError("reindex payload hash does not match preview", code="payload_hash_mismatch")
        result = self.worker.management_call(
            "management_rebuild",
            kb_id,
            {
                "previewToken": preview_token,
                "expectedRevision": expected_revision,
                "confirmText": str(payload.get("confirmText") or ""),
                "payloadSha256": str(payload.get("payloadSha256") or ""),
            },
        )
        return {"ok": True, **result}

    def jobs(self, kb_id: str) -> dict[str, object]:
        result = self.worker.management_call("management_jobs", _identifier(kb_id, "kbId"), limit=100)
        return {"schemaVersion": "rag-ime.knowledge-jobs.v1", "ok": True, "items": _items(result, "jobs")}

    def cancel_job(self, kb_id: str, job_id: str) -> dict[str, object]:
        kb_id = _identifier(kb_id, "kbId")
        result = self.worker.management_call("management_cancel_job", _identifier(job_id, "jobId"))
        job = dict(result.get("job") or result)
        if str(job.get("kbId") or job.get("baseId") or "") != kb_id:
            raise KnowledgeLibraryError("job is outside the selected knowledge base", code="scope_mismatch")
        return {"schemaVersion": "rag-ime.knowledge-job.v1", "ok": True, "job": job}

    def preview_chunking(
        self,
        kb_id: str,
        file_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        result = self.worker.management_call(
            "management_preview_chunking",
            _identifier(kb_id, "kbId"),
            _identifier(file_id, "fileId"),
            {
                "chunkingConfig": _object(payload.get("chunkingConfig"), "chunkingConfig"),
                "limit": _bounded_int(payload.get("limit"), default=12, minimum=1, maximum=30),
            },
        )
        return {"ok": True, **result}

    def search(self, kb_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        request: dict[str, object] = {
            "kbId": _identifier(kb_id, "kbId"),
            "query": _required_text(payload.get("query"), "query", maximum=2_000),
        }
        if payload.get("topK") is not None:
            request["limit"] = _bounded_int(payload.get("topK"), default=20, minimum=1, maximum=100)
        if payload.get("mode") is not None:
            request["mode"] = _optional_retrieval_mode(payload.get("mode"))
        if payload.get("threshold") is not None:
            request["threshold"] = _bounded_float(payload.get("threshold"), default=0.0, minimum=0.0, maximum=1.0)
        if payload.get("rerank") is not None:
            if not isinstance(payload.get("rerank"), bool):
                raise KnowledgeLibraryError("rerank must be boolean", code="invalid_argument")
            request["rerank"] = payload["rerank"]
        if payload.get("rerankCandidateDepth") is not None:
            request["rerankCandidateDepth"] = _bounded_int(payload.get("rerankCandidateDepth"), default=40, minimum=1, maximum=100)
        if payload.get("fileName") is not None:
            request["fileName"] = _required_text(payload.get("fileName"), "fileName", maximum=512)
        result = self.worker.management_call("management_search", request)
        return {"schemaVersion": "rag-ime.knowledge-search.v1", "ok": True, "items": _items(result, "hits"), **{key: value for key, value in result.items() if key in {"query", "retrieval", "total"}}}

    def find(self, kb_id: str, file_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        result = self.worker.management_call(
            "management_find",
            {
                "kbId": _identifier(kb_id, "kbId"),
                "fileId": _identifier(file_id, "fileId"),
                "patterns": [_required_text(payload.get("query"), "query", maximum=500)],
                "useRegex": payload.get("regex") is True,
                "maxWindows": 20,
                "windowSize": _bounded_int(payload.get("lineWindow"), default=20, minimum=4, maximum=100),
            },
        )
        return {"schemaVersion": "rag-ime.knowledge-find.v1", "ok": True, **result}

    def open(self, kb_id: str, file_id: str, query: Mapping[str, object]) -> dict[str, object]:
        result = self.worker.management_call(
            "management_open",
            {
                "kbId": _identifier(kb_id, "kbId"),
                "fileId": _identifier(file_id, "fileId"),
                "chunkId": _text(query.get("chunkId"), maximum=128),
                "offset": _bounded_int(query.get("startLine"), default=0, minimum=0, maximum=50_000_000),
                "limit": _bounded_int(query.get("lines"), default=80, minimum=1, maximum=300),
            },
        )
        return {"schemaVersion": "rag-ime.knowledge-content.v1", "ok": True, **result}

    def health(self) -> dict[str, object]:
        result = dict(self.worker.management_call("management_status"))
        result.pop("database", None)
        result.pop("storage", None)
        return {"ok": True, "available": True, **result}

    def parsers(self) -> dict[str, object]:
        mineru = self.worker.management_call("mineru_health")
        items = [
            {"id": "auto", "name": "自动", "available": True},
            {"id": "builtin", "name": "内置解析", "available": True},
            {
                "id": "mineru_local_http",
                "name": "MinerU",
                "available": str(mineru.get("status") or "").lower() in {"ok", "ready", "healthy"},
                "status": str(mineru.get("status") or "disabled"),
            },
        ]
        return {"schemaVersion": "rag-ime.knowledge-parsers.v1", "ok": True, "items": items}


def _public_base(value: Mapping[str, object]) -> dict[str, object]:
    parser = str(value.get("parserMode") or value.get("parser") or "auto")
    return {
        **dict(value),
        "id": str(value.get("id") or value.get("baseId") or ""),
        "parser": "mineru" if parser in {"mineru", "mineru_local_http"} else parser,
        "revision": _base_revision(value),
        "status": str(value.get("status") or "ready"),
    }


def _public_document(value: Mapping[str, object]) -> dict[str, object]:
    error = value.get("error")
    return {
        **dict(value),
        "id": str(value.get("id") or value.get("documentId") or ""),
        "name": str(value.get("fileName") or value.get("name") or ""),
        "baseId": str(value.get("baseId") or value.get("kbId") or ""),
        "errorMessage": str(error.get("message") or "") if isinstance(error, Mapping) else "",
    }


def _items(value: Mapping[str, object], key: str) -> list[dict[str, object]]:
    raw = value.get("items") if isinstance(value.get("items"), list) else value.get(key)
    return [dict(item) for item in raw or [] if isinstance(item, Mapping)]


def _base_revision(value: Mapping[str, object]) -> object:
    return value.get("revision") if value.get("revision") is not None else int(value.get("updatedAtMs") or 0)


def _require_revision(provided: object, current: object) -> None:
    if isinstance(provided, Mapping):
        provided = provided.get("subjectRevision")
    if provided != current:
        raise ManagementWorkError(
            "revision_mismatch",
            "The document knowledge snapshot is stale.",
            current_revision={"subjectRevision": current},
        )


def _parser_mode(value: object) -> str:
    raw = str(value or "auto").strip().lower()
    if raw == "mineru_local_http":
        return "mineru"
    if raw not in {"auto", "builtin", "mineru"}:
        raise KnowledgeLibraryError("parserProvider is not allowlisted", code="invalid_argument")
    return raw


def _embedding_candidate(payload: Mapping[str, object]) -> dict[str, object]:
    raw = payload.get("profile") if isinstance(payload.get("profile"), Mapping) else payload
    allowed = (
        "provider",
        "model",
        "baseUrl",
        "dimensions",
        "secretReference",
        "queryPrefix",
        "documentPrefix",
        "denseBackend",
    )
    unknown = sorted(str(key) for key in set(raw) - set(allowed))
    if unknown:
        raise KnowledgeLibraryError(
            f"unsupported embedding profile field: {unknown[0]}",
            code="invalid_argument",
        )
    missing = [key for key in allowed if key not in raw]
    if missing:
        raise KnowledgeLibraryError(
            f"embedding profile is missing {missing[0]}",
            code="invalid_argument",
        )
    return {key: raw[key] for key in allowed}


def _identifier(value: object, field: str) -> str:
    result = _required_text(value, field, maximum=160)
    if not all(char.isalnum() or char in "._:-" for char in result):
        raise KnowledgeLibraryError(f"invalid {field}", code="invalid_argument")
    return result


def _required_text(value: object, field: str, *, maximum: int) -> str:
    result = _text(value, maximum=maximum)
    if not result:
        raise KnowledgeLibraryError(f"{field} is required", code="invalid_argument")
    return result


def _text(value: object, *, maximum: int) -> str:
    return " ".join(str(value or "").replace("\x00", " ").split()).strip()[:maximum]


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _bounded_float(value: object, *, default: float, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _object(value: object, field: str) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise KnowledgeLibraryError(f"{field} must be an object", code="invalid_argument")
    return {str(key): item for key, item in value.items()}


def _optional_retrieval_mode(value: object) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    mode = str(value).strip().lower()
    if mode not in {"lexical", "hybrid", "dense"}:
        raise KnowledgeLibraryError("retrieval mode is not allowlisted", code="invalid_argument")
    return mode


def _payload_sha256(value: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
