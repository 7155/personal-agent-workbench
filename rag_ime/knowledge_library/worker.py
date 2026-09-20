from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import platform
import re
import sqlite3
import sys
import tempfile
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from ..db import sqlite_connection
from .client import LocalKnowledgeClient
from .identity import knowledge_worker_fingerprint, normalized_knowledge_root
from .models import (
    DEFAULT_MINERU_TIMEOUT_SECONDS,
    AssetBlob,
    KNOWLEDGE_SCHEMA_VERSION,
    KnowledgeConflictError,
    KnowledgeLibraryConfig,
    KnowledgeLibraryError,
    KnowledgeNotFoundError,
)
from .permissions import secure_directory, secure_file
from .rerank import knowledge_reranker_from_env, knowledge_reranker_profile_sha256
from .service import KnowledgeLibraryService


IMPORT_SCHEMA_VERSION = "rag-ime.knowledge-document-import.v1"
MAX_JSON_BYTES = 2 * 1024 * 1024


class KnowledgeWorkerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        service: KnowledgeLibraryService,
        *,
        owner: str = "",
        idle_seconds: float = 900.0,
        parent_pid: int = 0,
        intake_validator: Callable[[str, str], bool] | None = None,
    ):
        super().__init__(address, KnowledgeWorkerHandler)
        self.service = service
        from .vault_auth import management_token
        self.vault_management_token = management_token(Path(service.config.root_dir))
        self.agent_client = LocalKnowledgeClient(service)
        self.last_activity = time.monotonic()
        self._next_vault_capture = 0.0
        self.worker_root = normalized_knowledge_root(service.config.root_dir)
        self.worker_owner = owner or f"standalone:{os.getpid()}"
        self.parent_pid = max(0, int(parent_pid))
        self.intake_validator = intake_validator
        self.config_fingerprint = knowledge_worker_fingerprint(
            service.config.root_dir,
            mineru_enabled=service.config.mineru_enabled,
            mineru_port=service.config.mineru_port,
            mineru_timeout_seconds=service.config.mineru_timeout_seconds,
            idle_seconds=idle_seconds,
            python_executable=os.environ.get("RAG_IME_KNOWLEDGE_PYTHON", "").strip() or sys.executable,
            python_version=platform.python_version(),
            embedding_provider=os.environ.get("RAG_IME_EMBEDDING_PROVIDER", "none"),
            embedding_model=os.environ.get("RAG_IME_EMBEDDING_MODEL", ""),
            dense_backend=os.environ.get("RAG_IME_KNOWLEDGE_DENSE_BACKEND", "sqlite-exact"),
            embedding_profile_sha256=os.environ.get(
                "RAG_IME_KNOWLEDGE_EMBEDDING_PROFILE_SHA256",
                "",
            ),
            reranker_provider=os.environ.get(
                "RAG_IME_KNOWLEDGE_RERANK_PROVIDER",
                "none",
            ),
            reranker_model_path=os.environ.get(
                "RAG_IME_KNOWLEDGE_RERANK_MODEL_PATH",
                "",
            ),
            reranker_model_revision=os.environ.get(
                "RAG_IME_KNOWLEDGE_RERANK_MODEL_REVISION",
                "",
            ),
            reranker_profile_sha256=knowledge_reranker_profile_sha256(),
        )

    def service_actions(self):
        current = time.monotonic()
        if current >= self._next_vault_capture:
            self._next_vault_capture = current + 30
            if self.service.schedule_vault_capture():
                self.last_activity = current


class KnowledgeWorkerHandler(BaseHTTPRequestHandler):
    server: KnowledgeWorkerServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def log_message(self, format: str, *args: object) -> None:
        return None

    def _dispatch(self, method: str) -> None:
        self.server.last_activity = time.monotonic()
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)
        try:
            result, status = self._route(method, path, query)
        except KnowledgeNotFoundError as exc:
            self._error(HTTPStatus.NOT_FOUND, exc)
            return
        except KnowledgeConflictError as exc:
            self._error(HTTPStatus.CONFLICT, exc)
            return
        except KnowledgeLibraryError as exc:
            status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE if exc.code in {"asset_too_large", "source_too_large", "request_too_large"} else HTTPStatus.BAD_REQUEST
            self._error(status, exc)
            return
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._error(
                HTTPStatus.BAD_REQUEST,
                KnowledgeLibraryError(str(exc) or "invalid request", code="invalid_request"),
            )
            return
        except Exception as exc:
            self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                KnowledgeLibraryError(str(exc) or "knowledge worker failed", code="internal_error"),
            )
            return
        if isinstance(result, AssetBlob):
            self._asset(status, result)
        else:
            self._json(status, result)

    def _route(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
    ) -> tuple[dict[str, Any] | AssetBlob, HTTPStatus]:
        service = self.server.service
        agent = self.server.agent_client
        if method == "GET" and path == "/v1/health":
            return {
                "status": "ok",
                "schemaVersion": KNOWLEDGE_SCHEMA_VERSION,
                "root": self.server.worker_root,
                "configFingerprint": self.server.config_fingerprint,
                "owner": self.server.worker_owner,
                "parentPid": self.server.parent_pid or None,
            }, HTTPStatus.OK
        if method == "GET" and path == "/v1/agent/knowledge/bases":
            return agent.list_bases({}), HTTPStatus.OK
        if method == "GET" and path == "/v1/agent/knowledge/status":
            return agent.status({}), HTTPStatus.OK
        if method == "POST" and path == "/v1/agent/knowledge/search":
            body = self._json_body()
            return agent.search(body), HTTPStatus.OK
        if method == "POST" and path == "/v1/agent/knowledge/find":
            body = self._json_body()
            return agent.find(body), HTTPStatus.OK
        if method == "POST" and path == "/v1/agent/knowledge/open":
            body = self._json_body()
            return agent.open(body), HTTPStatus.OK
        if method == "GET" and path == "/v1/knowledge/status":
            return service.status(), HTTPStatus.OK
        if method == "GET" and path == "/v1/knowledge/mineru/health":
            return service.mineru_health(), HTTPStatus.OK
        if method == "POST" and path == "/v1/knowledge/vault":
            if not hmac.compare_digest(self.headers.get('X-PAW-Vault-Management', ''), self.server.vault_management_token):
                raise KnowledgeLibraryError('Vault management authentication required', code='unauthorized')
            return service.vault.dispatch(self._json_body()), HTTPStatus.OK
        if method == "POST" and path == "/v1/knowledge/vault/editor":
            token = self.headers.get('Authorization', '').removeprefix('Bearer ')
            return service.vault.workflow.editor(token, self._json_body()), HTTPStatus.OK
        if method == "GET" and path == "/v1/knowledge/bases":
            return service.list_bases(), HTTPStatus.OK
        if method == "POST" and path == "/v1/knowledge/bases":
            body = self._json_body()
            return service.create_base(
                str(body.get("name") or ""),
                description=str(body.get("description") or ""),
                parser_mode=str(body.get("parserMode") or body.get("parserProvider") or "auto"),
                agent_enabled=bool(body.get("agentEnabled", False)),
                chunking_config=body.get("chunkingConfig"),
                retrieval_config=body.get("retrievalConfig"),
            ), HTTPStatus.CREATED
        if path.startswith("/v1/knowledge/bases/"):
            tail = path.removeprefix("/v1/knowledge/bases/")
            parts = tail.split("/")
            kb_id = urllib.parse.unquote(parts[0])
            if len(parts) == 2 and parts[1] == "graph" and method == "GET":
                kinds = tuple(
                    item.strip().lower()
                    for item in str((query.get("kinds") or [""])[0]).split(",")
                    if item.strip()
                )
                return service.knowledge_graph(
                    kb_id,
                    document_id=str((query.get("documentId") or [""])[0]),
                    query=str((query.get("query") or [""])[0]),
                    kinds=kinds,
                    limit=int(_first(query, "limit", default="200")),
                    depth=int(_first(query, "depth", default="2")),
                    exclude_chunks=_first(query, "excludeChunks", default="true").strip().lower() in {"1", "true", "yes"},
                    focus_id=str((query.get("focusId") or [""])[0]),
                ), HTTPStatus.OK
            if len(parts) == 3 and parts[1:] == ["graph", "rebuild"] and method == "POST":
                body = self._json_body()
                raw_document_ids = body.get("documentIds")
                document_ids = _string_list(raw_document_ids) if raw_document_ids is not None else ()
                return service.rebuild_knowledge_graph(
                    kb_id,
                    expected_revision=(
                        int(body["expectedRevision"])
                        if body.get("expectedRevision") is not None
                        else None
                    ),
                    document_ids=document_ids,
                    extractor_mode=str(body.get("extractorMode") or "deterministic"),
                    model_id=str(body.get("modelId") or ""),
                    batch_size=int(body.get("batchSize") or 4),
                    extraction_concurrency=int(body.get("extractionConcurrency") or 2),
                    max_entities=int(body.get("maxEntitiesPerChunk") or 5),
                    max_relations=int(body.get("maxRelationsPerChunk") or 4),
                    max_topics=int(body.get("maxTopicsPerChunk") or 2),
                ), HTTPStatus.OK
            if len(parts) == 2 and parts[1] == "reindex-preview" and method == "GET":
                return service.reindex_preview(kb_id), HTTPStatus.OK
            if len(parts) == 2 and parts[1] == "rebuild" and method == "POST":
                body = self._json_body()
                return service.rebuild_base(
                    kb_id,
                    preview_token=str(body.get("previewToken") or ""),
                    expected_revision=int(body.get("expectedRevision") or 0),
                    confirm_text=str(body.get("confirmText") or ""),
                ), HTTPStatus.OK
            if len(parts) == 4 and parts[1] == "documents" and parts[3] == "detail" and method == "GET":
                return service.document_detail(
                    kb_id,
                    urllib.parse.unquote(parts[2]),
                    offset=int(_first(query, "offset", default="0")),
                    limit=int(_first(query, "limit", default="100")),
                    line_offset=int(_first(query, "lineOffset", default="0")),
                    line_limit=int(_first(query, "lineLimit", default="200")),
                ), HTTPStatus.OK
            if len(parts) == 4 and parts[1] == "documents" and parts[3] == "source" and method == "GET":
                return service.read_document_source(kb_id, urllib.parse.unquote(parts[2])), HTTPStatus.OK
            if len(parts) == 5 and parts[1] == "documents" and parts[3] == "assets" and method == "GET":
                return service.read_document_asset(
                    kb_id,
                    urllib.parse.unquote(parts[2]),
                    urllib.parse.unquote(parts[4]),
                ), HTTPStatus.OK
            if len(parts) == 4 and parts[1] == "documents" and parts[3] == "chunk-preview" and method == "POST":
                body = self._json_body()
                return service.preview_chunking(
                    kb_id,
                    urllib.parse.unquote(parts[2]),
                    body.get("chunkingConfig") if isinstance(body.get("chunkingConfig"), dict) else {},
                    limit=int(body.get("limit") or 12),
                ), HTTPStatus.OK
            if len(parts) != 1:
                raise KnowledgeNotFoundError(f"worker route not found: {method} {path}")
            if method == "GET":
                return service.get_base(kb_id), HTTPStatus.OK
            if method == "PATCH":
                body = self._json_body()
                return service.update_base(
                    kb_id,
                    name=body.get("name") if "name" in body else None,
                    description=body.get("description") if "description" in body else None,
                    parser_mode=(body.get("parserMode") if "parserMode" in body else body.get("parserProvider")),
                    agent_enabled=body.get("agentEnabled") if "agentEnabled" in body else None,
                    chunking_config=body.get("chunkingConfig") if "chunkingConfig" in body else None,
                    retrieval_config=body.get("retrievalConfig") if "retrievalConfig" in body else None,
                    expected_revision=int(body["expectedRevision"]) if body.get("expectedRevision") is not None else None,
                ), HTTPStatus.OK
            if method == "DELETE":
                return service.delete_base(kb_id), HTTPStatus.OK
        if method == "GET" and path == "/v1/knowledge/documents":
            return service.list_documents(_first(query, "kbId")), HTTPStatus.OK
        if method == "GET" and path == "/v1/knowledge/jobs":
            return service.list_jobs(
                base_id=str((query.get("kbId") or [""])[0]),
                limit=int(_first(query, "limit", default="100")),
            ), HTTPStatus.OK
        if method == "POST" and path.startswith("/v1/knowledge/jobs/") and path.endswith("/cancel"):
            job_id = urllib.parse.unquote(path.removeprefix("/v1/knowledge/jobs/").removesuffix("/cancel"))
            return service.cancel_job(job_id), HTTPStatus.OK
        if method == "POST" and path == "/v1/knowledge/documents/import":
            return self._import_document(query), HTTPStatus.CREATED
        if path.startswith("/v1/knowledge/documents/"):
            tail = path.removeprefix("/v1/knowledge/documents/")
            if tail.endswith("/retry") and method == "POST":
                file_id = urllib.parse.unquote(tail.removesuffix("/retry"))
                body = self._json_body()
                return service.retry_document(file_id, parser_mode=body.get("parserMode")), HTTPStatus.OK
            file_id = urllib.parse.unquote(tail)
            if method == "GET":
                return service.get_document(file_id), HTTPStatus.OK
            if method == "DELETE":
                return service.delete_document(file_id), HTTPStatus.OK
        if method == "POST" and path == "/v1/knowledge/search":
            body = self._json_body()
            return service.search(
                str(body.get("query") or ""),
                base_ids=_string_list(body.get("kbIds") or body.get("kbId")),
                limit=int(body["limit"]) if body.get("limit") is not None else None,
                mode=(
                    str(body.get("mode") or body.get("searchMode"))
                    if body.get("mode") is not None or body.get("searchMode") is not None
                    else None
                ),
                threshold=float(body["threshold"]) if body.get("threshold") is not None else None,
                rerank=body.get("rerank") if "rerank" in body else None,
                rerank_candidate_depth=(
                    body.get("rerankCandidateDepth")
                    if "rerankCandidateDepth" in body
                    else None
                ),
                file_name=str(body.get("fileName") or ""),
            ), HTTPStatus.OK
        if method == "POST" and path == "/v1/knowledge/find":
            body = self._json_body()
            requested_query = _query_from_body(body)
            if body.get("fileId"):
                return service.find_in_document(
                    str(body["fileId"]),
                    _patterns_from_body(body),
                    use_regex=body.get("useRegex") is True or body.get("regex") is True,
                    case_sensitive=body.get("caseSensitive") is True,
                    max_windows=int(body.get("maxWindows") or body.get("limit") or 8),
                    window_size=int(body.get("windowSize") or body.get("lineWindow") or 24),
                    offset=int(body.get("offset") or 0),
                ), HTTPStatus.OK
            find_result = service.find(
                requested_query,
                base_ids=_string_list(body.get("kbIds") or body.get("kbId")),
                limit=int(body.get("limit") or body.get("topK") or 20),
            )
            search_result = service.search(
                requested_query,
                base_ids=_string_list(body.get("kbIds") or body.get("kbId")),
                limit=int(body.get("limit") or body.get("topK") or 20),
            )
            return {**find_result, "contentHits": search_result["hits"]}, HTTPStatus.OK
        if method == "POST" and path == "/v1/knowledge/open":
            body = self._json_body()
            if body.get("chunkId"):
                return service.open(
                    str(body["chunkId"]),
                    before=int(body.get("before") or 1),
                    after=int(body.get("after") or 1),
                ), HTTPStatus.OK
            return service.open_document(
                str(body.get("fileId") or ""),
                offset=int(body.get("offset") or 0),
                limit=int(body.get("limit") or body.get("topK") or 5),
            ), HTTPStatus.OK
        raise KnowledgeNotFoundError(f"worker route not found: {method} {path}")

    def _import_document(self, query: dict[str, list[str]]) -> dict[str, Any]:
        content_length = _content_length(self.headers.get("Content-Length"))
        if content_length > self.server.service.config.max_source_bytes:
            raise KnowledgeLibraryError("source file exceeds the configured size limit", code="source_too_large")
        kb_id = self.headers.get("X-Knowledge-Base-Id") or _first(query, "kbId")
        encoded_name = self.headers.get("X-Knowledge-File-Name") or _first(query, "fileName", default="document.bin")
        file_name = Path(urllib.parse.unquote(encoded_name)).name
        intake_receipt_id = str(self.headers.get("X-Knowledge-Intake-Receipt") or "").strip()
        intake_hash = str(self.headers.get("X-Knowledge-Intake-Hash") or "").strip()
        intake_status = str(self.headers.get("X-Knowledge-Intake-Status") or "").strip()
        if intake_status != "allowed" or not intake_receipt_id or len(intake_hash) != 64:
            raise KnowledgeLibraryError("allowed intake receipt is required before import", code="intake_required")
        if self.server.intake_validator is None or not self.server.intake_validator(intake_receipt_id, intake_hash):
            raise KnowledgeLibraryError("intake receipt is unknown, quarantined, or stale", code="intake_invalid")
        if not file_name or file_name in {".", ".."}:
            raise KnowledgeLibraryError("file name is required", code="invalid_argument")
        incoming = self.server.service.config.root_dir / "incoming"
        secure_directory(incoming)
        suffix = Path(file_name).suffix[:20]
        temporary_path: Path | None = None
        try:
            intake_hasher = hashlib.sha256()
            with tempfile.NamedTemporaryFile(dir=incoming, suffix=suffix, delete=False) as target:
                temporary_path = Path(target.name)
                remaining = content_length
                while remaining:
                    block = self.rfile.read(min(1024 * 1024, remaining))
                    if not block:
                        raise KnowledgeLibraryError("request body ended before Content-Length", code="invalid_request")
                    target.write(block)
                    intake_hasher.update(block)
                    remaining -= len(block)
            if intake_hasher.hexdigest() != intake_hash:
                raise KnowledgeLibraryError("intake receipt hash does not match upload", code="intake_hash_mismatch")
            secure_file(temporary_path)
            document = self.server.service.import_document(
                kb_id,
                temporary_path,
                display_name=file_name,
                mime_type=str(self.headers.get("Content-Type") or "application/octet-stream").split(";", 1)[0],
                parser_mode=(str(query["parserMode"][0]) if query.get("parserMode") else None),
            )
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        receipt = {
            "kbId": document["kbId"],
            "documentId": document["documentId"],
            "fileName": document["fileName"],
            "mimeType": document["mimeType"],
            "byteSize": document["byteSize"],
            "sha256": document["sha256"],
            "status": document["status"],
        }
        return {"schemaVersion": IMPORT_SCHEMA_VERSION, "ok": True, "receipt": receipt}

    def _json_body(self) -> dict[str, Any]:
        content_length = _content_length(self.headers.get("Content-Length"))
        if content_length > MAX_JSON_BYTES:
            raise KnowledgeLibraryError("JSON body is too large", code="request_too_large")
        if content_length == 0:
            return {}
        raw = self.rfile.read(content_length)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise KnowledgeLibraryError("JSON request body must be an object", code="invalid_request")
        return payload

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _asset(self, status: HTTPStatus, asset: AssetBlob) -> None:
        encoded_name = urllib.parse.quote(asset.file_name, safe="")
        self.send_response(int(status))
        self.send_header("Content-Type", asset.media_type)
        self.send_header("Content-Length", str(asset.byte_size))
        self.send_header("ETag", f'"{asset.asset_id}"')
        self.send_header("Cache-Control", "private, max-age=31536000, immutable")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{encoded_name}")
        self.end_headers()
        self.wfile.write(asset.data)

    def _error(self, status: HTTPStatus, error: KnowledgeLibraryError) -> None:
        self._json(
            status,
            {
                "schemaVersion": KNOWLEDGE_SCHEMA_VERSION,
                "ok": False,
                "error": {"code": error.code, "message": str(error)},
            },
        )


def default_knowledge_root() -> Path:
    return Path.home() / "Library" / "Application Support" / "RagIme" / "Knowledge"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RAG-IME isolated document knowledge worker")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8_769)
    parser.add_argument("--root", type=Path, default=default_knowledge_root())
    parser.add_argument("--mineru-enabled", action="store_true")
    parser.add_argument("--mineru-port", type=int, default=30_001)
    parser.add_argument("--mineru-timeout-seconds", type=float, default=DEFAULT_MINERU_TIMEOUT_SECONDS)
    parser.add_argument("--idle-seconds", type=float, default=900.0)
    parser.add_argument("--owner", default="")
    parser.add_argument("--parent-pid", type=int, default=0)
    parser.add_argument("--intake-db", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("knowledge worker host must be loopback")
    if not 1_024 <= args.port <= 65_535:
        raise SystemExit("knowledge worker port must be between 1024 and 65535")
    if args.owner and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", args.owner) is None:
        raise SystemExit("knowledge worker owner is invalid")
    if args.parent_pid < 0:
        raise SystemExit("knowledge worker parent pid is invalid")
    os.umask(0o077)
    config = KnowledgeLibraryConfig(
        root_dir=args.root,
        mineru_enabled=bool(args.mineru_enabled),
        mineru_port=int(args.mineru_port),
        mineru_timeout_seconds=float(args.mineru_timeout_seconds),
    )
    service = KnowledgeLibraryService(
        config,
        reranker=knowledge_reranker_from_env(config.root_dir),
        background_jobs=True,
    )
    if args.intake_db:
        from ..memory_lifecycle.common import connect as lifecycle_connect
        from ..memory_lifecycle.daily import snapshot as daily_snapshot
        def activity_provider(project, day, timezone):
            with lifecycle_connect(args.intake_db) as conn:
                return daily_snapshot(conn, project=project, day=day, timezone=timezone, include_timeline=False)
        service.vault.activity_provider = activity_provider
    server = KnowledgeWorkerServer(
        (args.host, args.port),
        service,
        owner=args.owner,
        idle_seconds=args.idle_seconds,
        parent_pid=args.parent_pid,
        intake_validator=_database_intake_validator(args.intake_db) if args.intake_db else None,
    )
    server.timeout = 0.5
    try:
        while True:
            server.handle_request()
            server.service_actions()
            if args.parent_pid and os.getppid() != args.parent_pid:
                break
            if args.idle_seconds > 0 and time.monotonic() - server.last_activity >= args.idle_seconds:
                break
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close(wait=True)
    return 0


def _database_intake_validator(db_path: Path) -> Callable[[str, str], bool]:
    path = Path(db_path).expanduser().resolve(strict=False)

    def validate(import_id: str, content_hash: str) -> bool:
        try:
            with sqlite_connection(path) as conn:
                row = conn.execute(
                    """SELECT 1 FROM room_v2_external_import_intakes
                       WHERE import_id=? AND content_hash=? AND scan_status='allowed'
                         AND data_only=1 AND raw_bytes_stored=0""",
                    (import_id, content_hash),
                ).fetchone()
        except sqlite3.Error:
            return False
        return row is not None

    return validate


def _content_length(value: str | None) -> int:
    if value is None:
        raise KnowledgeLibraryError("Content-Length is required", code="length_required")
    try:
        length = int(value)
    except ValueError as exc:
        raise KnowledgeLibraryError("invalid Content-Length", code="invalid_request") from exc
    if length < 0:
        raise KnowledgeLibraryError("invalid Content-Length", code="invalid_request")
    return length


def _first(query: dict[str, list[str]], key: str, *, default: str = "") -> str:
    values = query.get(key) or []
    value = str(values[0]) if values else default
    if not value and not default:
        raise KnowledgeLibraryError(f"{key} is required", code="invalid_argument")
    return value


def _string_list(value: Any) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(str(item) for item in value if str(item).strip())
    raise KnowledgeLibraryError("kbIds must be a string or string array", code="invalid_argument")


def _query_from_body(body: dict[str, Any]) -> str:
    query = str(body.get("query") or "").strip()
    if query:
        return query
    patterns = body.get("patterns")
    if isinstance(patterns, str):
        return patterns
    if isinstance(patterns, list):
        return " ".join(str(item).strip() for item in patterns if str(item).strip())
    return ""


def _patterns_from_body(body: dict[str, Any]) -> tuple[str, ...]:
    patterns = body.get("patterns")
    if isinstance(patterns, str):
        result = (patterns.strip(),)
    elif isinstance(patterns, list):
        result = tuple(str(item).strip() for item in patterns if str(item).strip())
    else:
        query = str(body.get("query") or "").strip()
        result = (query,) if query else ()
    if not result:
        raise KnowledgeLibraryError("find requires query or patterns", code="invalid_argument")
    return result[:10]


if __name__ == "__main__":
    raise SystemExit(main())
