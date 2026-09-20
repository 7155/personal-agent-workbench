from __future__ import annotations

import json
import hashlib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from .models import AssetBlob, KNOWLEDGE_SCHEMA_VERSION, KnowledgeLibraryError
from .service import KnowledgeLibraryService


class KnowledgeClient(Protocol):
    """Read-only public contract consumed by the `knowledge` Agent Tool."""

    def list_bases(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        ...

    def search(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        ...

    def find(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        ...

    def open(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        ...

    def status(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class LocalKnowledgeClient:
    service: KnowledgeLibraryService

    def list_bases(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return _agent_bases(self.service.list_bases(agent_only=True))

    def search(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return _agent_search(
            self.service.search(
                str(payload.get("query") or ""),
                base_ids=_kb_ids(payload),
                limit=_top_k(payload, 10) if ("topK" in payload or "limit" in payload) else None,
                mode=(
                    str(payload.get("mode") or payload.get("searchMode"))
                    if payload.get("mode") is not None or payload.get("searchMode") is not None
                    else None
                ),
                threshold=float(payload["threshold"]) if payload.get("threshold") is not None else None,
                rerank=_optional_bool(payload, "rerank"),
                rerank_candidate_depth=(
                    payload["rerankCandidateDepth"]
                    if payload.get("rerankCandidateDepth") is not None
                    else None
                ),
                file_name=str(payload.get("fileName") or ""),
                agent_only=True,
            )
        )

    def find(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        file_id = str(payload.get("fileId") or "")
        if not file_id:
            raise KnowledgeLibraryError("find requires fileId", code="invalid_argument")
        patterns = _find_patterns(payload)
        result = self.service.find_in_document(
            file_id,
            patterns,
            use_regex=payload.get("useRegex") is True,
            case_sensitive=payload.get("caseSensitive") is True,
            max_windows=int(payload.get("maxWindows") or 8),
            window_size=int(payload.get("windowSize") or 24),
            offset=int(payload.get("offset") or 0),
            agent_only=True,
        )
        requested_bases = _kb_ids(payload)
        if requested_bases and str(result.get("kbId") or "") not in requested_bases:
            raise KnowledgeLibraryError("document is outside the selected knowledge base", code="scope_mismatch")
        return result

    def open(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        file_id = str(payload.get("fileId") or "")
        chunk_id = str(payload.get("chunkId") or "")
        if chunk_id:
            result = self.service.open(
                chunk_id,
                before=int(payload.get("before") or 1),
                after=int(payload.get("after") or 1),
                agent_only=True,
            )
        elif file_id:
            window_size = max(1, min(300, int(payload.get("windowSize") or 40)))
            line = max(1, int(payload.get("line") or 1))
            offset = max(0, int(payload.get("offset") or 0))
            line_start = max(1, int(payload.get("lineStart") or line) + offset)
            result = self.service.open_document_lines(
                file_id,
                line_start=line_start,
                line_limit=window_size,
                agent_only=True,
            )
        else:
            raise KnowledgeLibraryError("open requires fileId or chunkId", code="invalid_argument")
        requested_bases = _kb_ids(payload)
        if requested_bases and str(result.get("baseId") or "") not in requested_bases:
            raise KnowledgeLibraryError(
                "document is outside the selected knowledge base",
                code="scope_mismatch",
            )
        return _agent_open(result)

    def status(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(self.service.status())
        result["agentEnabledBases"] = self.list_bases({})["bases"]
        result["items"] = []
        result.pop("database", None)
        result.pop("storage", None)
        return result


class HttpKnowledgeClient:
    """HTTP-only client: consumers never open `knowledge.sqlite` themselves."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8769",
        *,
        timeout_seconds: float = 10.0,
        urlopen: Callable[..., Any] | None = None,
    ):
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("knowledge worker URL must use loopback HTTP")
        self.vault_management_token = ""
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        # A loopback worker must never traverse the process-wide HTTP/SOCKS
        # proxy.  CI, desktop shells, and interview sandboxes often export a
        # proxy while leaving NO_PROXY empty; urllib would otherwise send a
        # local management request to that proxy (typically returning 403).
        self.urlopen = urlopen or urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
        ).open

    def list_bases(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._request("GET", "/v1/agent/knowledge/bases")

    def search(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/agent/knowledge/search", dict(payload))

    def find(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/agent/knowledge/find", dict(payload))

    def open(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/agent/knowledge/open", dict(payload))

    def status(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._request("GET", "/v1/agent/knowledge/status")

    def management_vault(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/knowledge/vault", payload, timeout_seconds=60)

    def management_list_bases(self) -> dict[str, Any]:
        return self._request("GET", "/v1/knowledge/bases")

    def management_create_base(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/knowledge/bases", payload)

    def management_get_base(self, kb_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/knowledge/bases/{urllib.parse.quote(kb_id, safe='')}")

    def management_knowledge_graph(
        self,
        kb_id: str,
        *,
        document_id: str = "",
        query: str = "",
        kinds: str = "",
        limit: int = 200,
        depth: int = 2,
        exclude_chunks: bool = True,
        focus_id: str = "",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, "depth": depth}
        if document_id:
            params["documentId"] = document_id
        if query:
            params["query"] = query
        if kinds:
            params["kinds"] = kinds
        if exclude_chunks:
            params["excludeChunks"] = "true"
        if focus_id:
            params["focusId"] = focus_id
        path = (
            f"/v1/knowledge/bases/{urllib.parse.quote(kb_id, safe='')}/graph?"
            f"{urllib.parse.urlencode(params)}"
        )
        return self._request("GET", path)

    def management_rebuild_knowledge_graph(
        self,
        kb_id: str,
        *,
        expected_revision: int | None,
        document_ids: list[str] | None = None,
        extractor_mode: str = "deterministic",
        model_id: str = "",
        batch_size: int = 4,
        extraction_concurrency: int = 2,
        max_entities: int = 5,
        max_relations: int = 4,
        max_topics: int = 2,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "documentIds": list(document_ids or []),
            "extractorMode": extractor_mode,
            "modelId": model_id,
            "batchSize": batch_size,
            "extractionConcurrency": extraction_concurrency,
            "maxEntitiesPerChunk": max_entities,
            "maxRelationsPerChunk": max_relations,
            "maxTopicsPerChunk": max_topics,
        }
        if expected_revision is not None:
            payload["expectedRevision"] = expected_revision
        return self._request(
            "POST",
            f"/v1/knowledge/bases/{urllib.parse.quote(kb_id, safe='')}/graph/rebuild",
            payload,
            timeout_seconds=max(self.timeout_seconds, 1_800.0),
        )

    def management_update_base(self, kb_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", f"/v1/knowledge/bases/{urllib.parse.quote(kb_id, safe='')}", payload)

    def management_delete_base(self, kb_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/v1/knowledge/bases/{urllib.parse.quote(kb_id, safe='')}")

    def management_list_documents(self, kb_id: str) -> dict[str, Any]:
        query = urllib.parse.urlencode({"kbId": kb_id})
        return self._request("GET", f"/v1/knowledge/documents?{query}")

    def management_document_detail(
        self,
        kb_id: str,
        file_id: str,
        *,
        offset: int = 0,
        limit: int = 100,
        line_offset: int = 0,
        line_limit: int = 200,
    ) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {"offset": offset, "limit": limit, "lineOffset": line_offset, "lineLimit": line_limit}
        )
        path = (
            f"/v1/knowledge/bases/{urllib.parse.quote(kb_id, safe='')}/documents/"
            f"{urllib.parse.quote(file_id, safe='')}/detail?{query}"
        )
        return self._request("GET", path)

    def management_preview_chunking(
        self,
        kb_id: str,
        file_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        path = (
            f"/v1/knowledge/bases/{urllib.parse.quote(kb_id, safe='')}/documents/"
            f"{urllib.parse.quote(file_id, safe='')}/chunk-preview"
        )
        return self._request("POST", path, payload)

    def management_read_asset(self, kb_id: str, file_id: str, asset_id: str) -> AssetBlob:
        path = (
            f"/v1/knowledge/bases/{urllib.parse.quote(kb_id, safe='')}/documents/"
            f"{urllib.parse.quote(file_id, safe='')}/assets/{urllib.parse.quote(asset_id, safe='')}"
        )
        request = urllib.request.Request(f"{self.base_url}{path}", headers={"Accept": "image/*"}, method="GET")
        try:
            with self.urlopen(request, timeout=self.timeout_seconds) as response:
                data = response.read(25 * 1024 * 1024 + 1)
                media_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
                etag = str(response.headers.get("ETag") or "").strip('"')
                disposition = str(response.headers.get("Content-Disposition") or "")
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from exc
        except (OSError, urllib.error.URLError) as exc:
            raise KnowledgeLibraryError(f"knowledge worker is unavailable: {exc}", code="worker_unavailable") from exc
        if len(data) > 25 * 1024 * 1024:
            raise KnowledgeLibraryError("asset exceeds the client read size limit", code="asset_too_large")
        if etag != asset_id:
            raise KnowledgeLibraryError("asset ETag does not match assetId", code="asset_integrity_error")
        if hashlib.sha256(data).hexdigest() != asset_id:
            raise KnowledgeLibraryError("asset digest does not match assetId", code="asset_integrity_error")
        return AssetBlob(
            asset_id=asset_id,
            file_name=_content_disposition_name(disposition) or asset_id,
            media_type=media_type,
            data=data,
        )

    def management_read_source(self, kb_id: str, file_id: str) -> AssetBlob:
        path = (
            f"/v1/knowledge/bases/{urllib.parse.quote(kb_id, safe='')}/documents/"
            f"{urllib.parse.quote(file_id, safe='')}/source"
        )
        request = urllib.request.Request(f"{self.base_url}{path}", headers={"Accept": "*/*"}, method="GET")
        try:
            with self.urlopen(request, timeout=self.timeout_seconds) as response:
                data = response.read(50 * 1024 * 1024 + 1)
                media_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
                etag = str(response.headers.get("ETag") or "").strip('"')
                disposition = str(response.headers.get("Content-Disposition") or "")
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from exc
        except (OSError, urllib.error.URLError) as exc:
            raise KnowledgeLibraryError(f"knowledge worker is unavailable: {exc}", code="worker_unavailable") from exc
        if len(data) > 50 * 1024 * 1024:
            raise KnowledgeLibraryError("source exceeds the client preview size limit", code="source_too_large")
        if not re_full_sha256(etag) or hashlib.sha256(data).hexdigest() != etag:
            raise KnowledgeLibraryError("source digest does not match ETag", code="source_integrity_error")
        return AssetBlob(
            asset_id=etag,
            file_name=_content_disposition_name(disposition) or file_id,
            media_type=media_type,
            data=data,
        )

    def management_reindex_preview(self, kb_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v1/knowledge/bases/{urllib.parse.quote(kb_id, safe='')}/reindex-preview",
        )

    def management_rebuild(self, kb_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/knowledge/bases/{urllib.parse.quote(kb_id, safe='')}/rebuild",
            payload,
            timeout_seconds=max(self.timeout_seconds, 1_800.0),
        )

    def management_retry_document(self, file_id: str, *, parser_mode: str | None = None) -> dict[str, Any]:
        payload = {"parserMode": parser_mode} if parser_mode else {}
        return self._request(
            "POST",
            f"/v1/knowledge/documents/{urllib.parse.quote(file_id, safe='')}/retry",
            payload,
            timeout_seconds=max(self.timeout_seconds, 1_800.0),
        )

    def management_delete_document(self, file_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/v1/knowledge/documents/{urllib.parse.quote(file_id, safe='')}")

    def management_search(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/knowledge/search", payload)

    def management_find(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/knowledge/find", payload)

    def management_open(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/knowledge/open", payload)

    def management_jobs(self, kb_id: str = "", *, limit: int = 100) -> dict[str, Any]:
        query: dict[str, Any] = {"limit": int(limit)}
        if kb_id:
            query["kbId"] = kb_id
        return self._request("GET", f"/v1/knowledge/jobs?{urllib.parse.urlencode(query)}")

    def management_cancel_job(self, job_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/knowledge/jobs/{urllib.parse.quote(job_id, safe='')}/cancel",
            {},
        )

    def management_import_document(
        self,
        kb_id: str,
        data: bytes,
        *,
        file_name: str,
        mime_type: str = "application/octet-stream",
        parser_mode: str = "",
        intake_receipt_id: str = "",
        intake_content_hash: str = "",
    ) -> dict[str, Any]:
        query = {"kbId": kb_id, "fileName": file_name}
        if parser_mode:
            query["parserMode"] = parser_mode
        path = f"/v1/knowledge/documents/import?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=bytes(data),
            headers={
                "Content-Type": mime_type or "application/octet-stream",
                "Accept": "application/json",
                "X-Knowledge-Base-Id": kb_id,
                "X-Knowledge-File-Name": urllib.parse.quote(file_name, safe=""),
                "X-Knowledge-Intake-Receipt": intake_receipt_id,
                "X-Knowledge-Intake-Hash": intake_content_hash,
                "X-Knowledge-Intake-Status": "allowed" if intake_receipt_id else "legacy_adapter",
            },
            method="POST",
        )
        try:
            with self.urlopen(request, timeout=max(self.timeout_seconds, 1_800.0)) as response:
                raw = response.read(16 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from exc
        except (OSError, urllib.error.URLError) as exc:
            raise KnowledgeLibraryError(f"knowledge worker is unavailable: {exc}", code="worker_unavailable") from exc
        try:
            result = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise KnowledgeLibraryError("knowledge worker returned invalid JSON", code="worker_bad_response") from exc
        if not isinstance(result, dict):
            raise KnowledgeLibraryError("knowledge worker returned an invalid import receipt", code="worker_bad_response")
        return result

    def management_status(self) -> dict[str, Any]:
        return self._request("GET", "/v1/knowledge/status")

    def mineru_health(self) -> dict[str, Any]:
        return self._request("GET", "/v1/knowledge/mineru/health")

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json", "Accept": "application/json", **({"X-PAW-Vault-Management": self.vault_management_token} if path == "/v1/knowledge/vault" else {})},
            method=method,
        )
        try:
            with self.urlopen(request, timeout=timeout_seconds or self.timeout_seconds) as response:
                raw = response.read(16 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from exc
        except (OSError, urllib.error.URLError) as exc:
            raise KnowledgeLibraryError(f"knowledge worker is unavailable: {exc}", code="worker_unavailable") from exc
        try:
            result = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise KnowledgeLibraryError("knowledge worker returned invalid JSON", code="worker_bad_response") from exc
        if not isinstance(result, dict):
            raise KnowledgeLibraryError("knowledge worker returned an invalid response object", code="worker_bad_response")
        return result

    @staticmethod
    def _http_error(exc: urllib.error.HTTPError) -> KnowledgeLibraryError:
        raw = exc.read(256 * 1024)
        try:
            error = json.loads(raw.decode("utf-8")).get("error", {})
        except (json.JSONDecodeError, UnicodeDecodeError):
            error = {}
        return KnowledgeLibraryError(
            str(error.get("message") or f"knowledge worker returned HTTP {exc.code}"),
            code=str(error.get("code") or "worker_http_error"),
        )


def _agent_bases(result: dict[str, Any]) -> dict[str, Any]:
    bases = [
        {
            "kbId": item["id"],
            "name": item["name"],
            "description": item.get("description", ""),
            "readyFileCount": item.get("readyDocumentCount", 0),
            "chunkCount": item.get("chunkCount", 0),
        }
        for item in result.get("bases", [])
    ]
    return {"schemaVersion": KNOWLEDGE_SCHEMA_VERSION, "bases": bases, "items": bases, "total": len(bases)}


def _agent_search(result: dict[str, Any]) -> dict[str, Any]:
    hits = []
    for item in result.get("hits", []):
        citation = dict(item.get("citation") or {})
        citation.update(
            {
                "fileId": citation.pop("documentId", item.get("documentId")),
                "fileName": citation.pop("documentName", item.get("documentName")),
            }
        )
        hits.append(
            {
                "kbId": item.get("baseId"),
                "kbName": item.get("baseName"),
                "fileId": item.get("documentId"),
                "fileName": item.get("documentName"),
                "chunkId": item.get("chunkId"),
                "content": item.get("content", ""),
                "text": item.get("content", ""),
                "score": item.get("score", 0.0),
                "diagnostics": dict(item.get("diagnostics") or {}),
                "citation": citation,
            }
        )
    return {
        "schemaVersion": KNOWLEDGE_SCHEMA_VERSION,
        "query": result.get("query", ""),
        "hits": hits,
        "items": hits,
        "total": len(hits),
        "retrieval": result.get("retrieval", {}),
    }


def _agent_open(result: dict[str, Any]) -> dict[str, Any]:
    chunks = [
        {
            "chunkId": item.get("chunkId"),
            "ordinal": item.get("ordinal"),
            "content": item.get("content", ""),
            "text": item.get("content", ""),
            "page": item.get("page"),
            "heading": item.get("heading"),
            "lineStart": item.get("lineStart"),
            "lineEnd": item.get("lineEnd"),
        }
        for item in result.get("chunks", [])
    ]
    return {
        "schemaVersion": KNOWLEDGE_SCHEMA_VERSION,
        "kbId": result.get("baseId"),
        "kbName": result.get("baseName"),
        "fileId": result.get("documentId"),
        "fileName": result.get("documentName"),
        "anchorChunkId": result.get("anchorChunkId"),
        "lineStart": result.get("lineStart"),
        "lineEnd": result.get("lineEnd"),
        "totalLines": result.get("totalLines"),
        "nextLineStart": result.get("nextLineStart"),
        "hasMore": result.get("hasMore", False),
        "chunks": chunks,
        "items": chunks,
    }


def _kb_ids(payload: Mapping[str, Any]) -> tuple[str, ...]:
    value = payload.get("kbIds") or payload.get("kbId")
    if not value:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if str(item).strip())
    raise KnowledgeLibraryError("kbId must be a string or string array", code="invalid_argument")


def _top_k(payload: Mapping[str, Any], default: int) -> int:
    return max(1, min(100, int(payload.get("topK") or payload.get("limit") or default)))


def _optional_bool(payload: Mapping[str, Any], field: str) -> bool | None:
    if field not in payload:
        return None
    value = payload.get(field)
    if not isinstance(value, bool):
        raise KnowledgeLibraryError(f"{field} must be a boolean", code="invalid_argument")
    return value


def _find_patterns(payload: Mapping[str, Any]) -> tuple[str, ...]:
    patterns = payload.get("patterns")
    if isinstance(patterns, str):
        result = (patterns.strip(),)
    elif isinstance(patterns, (list, tuple)):
        result = tuple(str(item).strip() for item in patterns if str(item).strip())
    else:
        query = str(payload.get("query") or "").strip()
        result = (query,) if query else ()
    if not result:
        raise KnowledgeLibraryError("find requires patterns", code="invalid_argument")
    return result[:10]


def _content_disposition_name(value: str) -> str:
    marker = "filename*=UTF-8''"
    if marker not in value:
        return ""
    return urllib.parse.unquote(value.split(marker, 1)[1].split(";", 1)[0]).strip()


def re_full_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
