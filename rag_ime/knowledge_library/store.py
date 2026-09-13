from __future__ import annotations

import json
import hashlib
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator, Sequence

from .models import KNOWLEDGE_SCHEMA_VERSION, KnowledgeConflictError, KnowledgeLibraryError, KnowledgeNotFoundError, SearchHit
from .permissions import harden_knowledge_tree, secure_file


def now_ms() -> int:
    return int(time.time() * 1_000)


class KnowledgeStore:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        harden_knowledge_tree(self.database_path.parent)
        self._migrate()
        secure_file(self.database_path)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.database_path), timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
            if self.database_path.exists():
                secure_file(self.database_path)
            for suffix in ("-wal", "-shm"):
                sidecar = self.database_path.with_name(self.database_path.name + suffix)
                try:
                    if sidecar.exists():
                        secure_file(sidecar)
                except FileNotFoundError:
                    pass

    def _migrate(self) -> None:
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS knowledge_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_bases (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL DEFAULT '',
                    parser_mode TEXT NOT NULL DEFAULT 'auto',
                    agent_enabled INTEGER NOT NULL DEFAULT 0 CHECK (agent_enabled IN (0, 1)),
                    chunking_config_json TEXT NOT NULL DEFAULT '{}',
                    retrieval_config_json TEXT NOT NULL DEFAULT '{}',
                    config_revision INTEGER NOT NULL DEFAULT 1,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_documents (
                    id TEXT PRIMARY KEY,
                    base_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
                    display_name TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    media_type TEXT NOT NULL DEFAULT 'application/octet-stream',
                    sha256 TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    stored_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    parser_provider TEXT NOT NULL DEFAULT '',
                    parser_version TEXT NOT NULL DEFAULT '',
                    parser_params_hash TEXT NOT NULL DEFAULT '',
                    output_hash TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 1,
                    chunk_count INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    artifact_path TEXT NOT NULL DEFAULT '',
                    indexed_config_revision INTEGER NOT NULL DEFAULT 0,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    UNIQUE (base_id, sha256)
                );
                CREATE INDEX IF NOT EXISTS idx_knowledge_documents_base_status
                    ON knowledge_documents(base_id, status, updated_at_ms DESC);
                CREATE TABLE IF NOT EXISTS knowledge_jobs (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
                    revision INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    parser_mode TEXT NOT NULL DEFAULT 'auto',
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at_ms INTEGER NOT NULL,
                    started_at_ms INTEGER,
                    finished_at_ms INTEGER,
                    updated_at_ms INTEGER NOT NULL,
                    UNIQUE (document_id, revision, kind)
                );
                CREATE INDEX IF NOT EXISTS idx_knowledge_jobs_document
                    ON knowledge_jobs(document_id, created_at_ms DESC);
                CREATE TABLE IF NOT EXISTS knowledge_chunks (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
                    base_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    heading TEXT NOT NULL DEFAULT '',
                    page INTEGER,
                    content_hash TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    UNIQUE (document_id, ordinal)
                );
                CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_document
                    ON knowledge_chunks(document_id, ordinal);
                CREATE TABLE IF NOT EXISTS knowledge_assets (
                    sha256 TEXT PRIMARY KEY,
                    media_type TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    stored_path TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_document_assets (
                    document_id TEXT NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
                    asset_sha256 TEXT NOT NULL REFERENCES knowledge_assets(sha256) ON DELETE RESTRICT,
                    original_name TEXT NOT NULL,
                    PRIMARY KEY (document_id, asset_sha256, original_name)
                );
                CREATE TABLE IF NOT EXISTS knowledge_graph_state (
                    base_id TEXT PRIMARY KEY REFERENCES knowledge_bases(id) ON DELETE CASCADE,
                    revision INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'stale',
                    source_fingerprint TEXT NOT NULL DEFAULT '',
                    document_ids_json TEXT NOT NULL DEFAULT '[]',
                    job_id TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    updated_at_ms INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS knowledge_graph_jobs (
                    id TEXT PRIMARY KEY,
                    base_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    document_ids_json TEXT NOT NULL DEFAULT '[]',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at_ms INTEGER NOT NULL,
                    finished_at_ms INTEGER,
                    updated_at_ms INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_knowledge_graph_jobs_base
                    ON knowledge_graph_jobs(base_id, created_at_ms DESC);
                CREATE TABLE IF NOT EXISTS knowledge_graph_extractions (
                    chunk_id TEXT NOT NULL REFERENCES knowledge_chunks(id) ON DELETE CASCADE,
                    content_hash TEXT NOT NULL,
                    extractor_fingerprint TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_message TEXT NOT NULL DEFAULT '',
                    updated_at_ms INTEGER NOT NULL,
                    PRIMARY KEY (chunk_id, extractor_fingerprint)
                );
                CREATE TABLE IF NOT EXISTS knowledge_graph_nodes (
                    id TEXT PRIMARY KEY,
                    base_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    label TEXT NOT NULL,
                    document_id TEXT REFERENCES knowledge_documents(id) ON DELETE CASCADE,
                    document_name TEXT NOT NULL DEFAULT '',
                    chunk_id TEXT REFERENCES knowledge_chunks(id) ON DELETE CASCADE,
                    heading TEXT NOT NULL DEFAULT '',
                    excerpt TEXT NOT NULL DEFAULT '',
                    page INTEGER,
                    weight REAL NOT NULL DEFAULT 1.0,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at_ms INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_knowledge_graph_nodes_base_kind
                    ON knowledge_graph_nodes(base_id, kind, label COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS idx_knowledge_graph_nodes_document
                    ON knowledge_graph_nodes(document_id, kind);
                CREATE TABLE IF NOT EXISTS knowledge_graph_edges (
                    id TEXT PRIMARY KEY,
                    base_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
                    source_id TEXT NOT NULL REFERENCES knowledge_graph_nodes(id) ON DELETE CASCADE,
                    target_id TEXT NOT NULL REFERENCES knowledge_graph_nodes(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    weight REAL NOT NULL DEFAULT 1.0,
                    document_id TEXT REFERENCES knowledge_documents(id) ON DELETE CASCADE,
                    chunk_id TEXT REFERENCES knowledge_chunks(id) ON DELETE CASCADE,
                    created_at_ms INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_knowledge_graph_edges_base
                    ON knowledge_graph_edges(base_id, source_id, target_id);
                """
            )
            try:
                connection.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_fts USING fts5(chunk_id UNINDEXED, content, tokenize='unicode61')"
                )
            except sqlite3.OperationalError as exc:
                raise KnowledgeLibraryError("this Python SQLite build does not include FTS5", code="fts5_unavailable") from exc
            _ensure_column(connection, "knowledge_bases", "chunking_config_json", "TEXT NOT NULL DEFAULT '{}'")
            _ensure_column(connection, "knowledge_bases", "retrieval_config_json", "TEXT NOT NULL DEFAULT '{}'")
            _ensure_column(connection, "knowledge_bases", "config_revision", "INTEGER NOT NULL DEFAULT 1")
            _ensure_column(connection, "knowledge_documents", "artifact_path", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(connection, "knowledge_documents", "indexed_config_revision", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(connection, "knowledge_jobs", "parser_mode", "TEXT NOT NULL DEFAULT 'auto'")
            _ensure_column(connection, "knowledge_graph_state", "extractor_mode", "TEXT NOT NULL DEFAULT 'deterministic'")
            _ensure_column(connection, "knowledge_graph_state", "extractor_model", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(connection, "knowledge_graph_state", "extraction_stats_json", "TEXT NOT NULL DEFAULT '{}'")
            _ensure_column(connection, "knowledge_graph_jobs", "extractor_mode", "TEXT NOT NULL DEFAULT 'deterministic'")
            _ensure_column(connection, "knowledge_graph_jobs", "stats_json", "TEXT NOT NULL DEFAULT '{}'")
            connection.execute(
                "INSERT INTO knowledge_meta(key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (KNOWLEDGE_SCHEMA_VERSION,),
            )

    def one(self, query: str, params: Sequence[Any] = ()) -> sqlite3.Row:
        with self.connection() as connection:
            row = connection.execute(query, params).fetchone()
        if row is None:
            raise KnowledgeNotFoundError("knowledge library record was not found")
        return row

    def all(self, query: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self.connection() as connection:
            return list(connection.execute(query, params).fetchall())

    def create_base(
        self,
        *,
        base_id: str,
        name: str,
        normalized_name: str,
        description: str,
        parser_mode: str,
        agent_enabled: bool,
        chunking_config_json: str,
        retrieval_config_json: str,
    ) -> sqlite3.Row:
        timestamp = now_ms()
        try:
            with self.connection() as connection:
                connection.execute(
                    "INSERT INTO knowledge_bases "
                    "(id, name, normalized_name, description, parser_mode, agent_enabled, chunking_config_json, "
                    "retrieval_config_json, created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        base_id, name, normalized_name, description, parser_mode, int(agent_enabled),
                        chunking_config_json, retrieval_config_json, timestamp, timestamp,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise KnowledgeConflictError(f"a knowledge base named {name!r} already exists") from exc
        return self.get_base(base_id)

    def get_base(self, base_id: str) -> sqlite3.Row:
        return self.one(
            "SELECT b.*, "
            "COUNT(d.id) AS document_count, "
            "COALESCE(SUM(CASE WHEN d.status='ready' THEN 1 ELSE 0 END), 0) AS ready_document_count, "
            "COALESCE(SUM(d.chunk_count), 0) AS chunk_count "
            "FROM knowledge_bases b LEFT JOIN knowledge_documents d ON d.base_id=b.id "
            "WHERE b.id=? GROUP BY b.id",
            (base_id,),
        )

    def list_bases(self, *, agent_only: bool = False) -> list[sqlite3.Row]:
        condition = "WHERE b.agent_enabled=1" if agent_only else ""
        return self.all(
            "SELECT b.*, COUNT(d.id) AS document_count, "
            "COALESCE(SUM(CASE WHEN d.status='ready' THEN 1 ELSE 0 END), 0) AS ready_document_count, "
            "COALESCE(SUM(d.chunk_count), 0) AS chunk_count "
            "FROM knowledge_bases b LEFT JOIN knowledge_documents d ON d.base_id=b.id "
            f"{condition} GROUP BY b.id ORDER BY b.updated_at_ms DESC, b.name COLLATE NOCASE"
        )

    def update_base(self, base_id: str, fields: dict[str, Any]) -> sqlite3.Row:
        if not fields:
            return self.get_base(base_id)
        assignments = [f"{name}=?" for name in fields]
        values = list(fields.values())
        assignments.append("updated_at_ms=?")
        values.extend([now_ms(), base_id])
        try:
            with self.connection() as connection:
                cursor = connection.execute(
                    f"UPDATE knowledge_bases SET {', '.join(assignments)} WHERE id=?",
                    values,
                )
                if cursor.rowcount == 0:
                    raise KnowledgeNotFoundError(f"knowledge base {base_id!r} was not found")
        except sqlite3.IntegrityError as exc:
            raise KnowledgeConflictError("a knowledge base with that name already exists") from exc
        return self.get_base(base_id)

    def delete_base(self, base_id: str) -> None:
        with self.connection() as connection:
            chunk_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM knowledge_chunks WHERE base_id=?",
                    (base_id,),
                )
            ]
            connection.executemany("DELETE FROM knowledge_chunks_fts WHERE chunk_id=?", ((item,) for item in chunk_ids))
            cursor = connection.execute("DELETE FROM knowledge_bases WHERE id=?", (base_id,))
            if cursor.rowcount == 0:
                raise KnowledgeNotFoundError(f"knowledge base {base_id!r} was not found")

    def insert_document(self, values: dict[str, Any]) -> sqlite3.Row:
        columns = list(values)
        placeholders = ", ".join("?" for _ in columns)
        try:
            with self.connection() as connection:
                connection.execute(
                    f"INSERT INTO knowledge_documents ({', '.join(columns)}) VALUES ({placeholders})",
                    [values[column] for column in columns],
                )
        except sqlite3.IntegrityError as exc:
            raise KnowledgeConflictError("this file is already present in the selected knowledge base") from exc
        return self.get_document(str(values["id"]))

    def get_document(self, document_id: str) -> sqlite3.Row:
        return self.one(
            "SELECT d.*, b.name AS base_name, b.agent_enabled AS base_agent_enabled "
            "FROM knowledge_documents d JOIN knowledge_bases b ON b.id=d.base_id WHERE d.id=?",
            (document_id,),
        )

    def list_documents(self, base_id: str) -> list[sqlite3.Row]:
        self.get_base(base_id)
        return self.all(
            "SELECT d.*, b.name AS base_name, b.agent_enabled AS base_agent_enabled "
            "FROM knowledge_documents d JOIN knowledge_bases b ON b.id=d.base_id "
            "WHERE d.base_id=? ORDER BY d.updated_at_ms DESC, d.display_name COLLATE NOCASE",
            (base_id,),
        )

    def mark_base_documents_stale(self, base_id: str) -> int:
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE knowledge_documents SET status='stale', updated_at_ms=? WHERE base_id=? AND status='ready'",
                (now_ms(), base_id),
            )
            return int(cursor.rowcount)

    def advance_ready_index_revision(self, base_id: str, revision: int) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE knowledge_documents SET indexed_config_revision=? WHERE base_id=? AND status='ready'",
                (int(revision), base_id),
            )

    def update_document(self, document_id: str, fields: dict[str, Any]) -> sqlite3.Row:
        if not fields:
            return self.get_document(document_id)
        assignments = [f"{name}=?" for name in fields]
        values = list(fields.values())
        assignments.append("updated_at_ms=?")
        values.extend([now_ms(), document_id])
        with self.connection() as connection:
            cursor = connection.execute(
                f"UPDATE knowledge_documents SET {', '.join(assignments)} WHERE id=?",
                values,
            )
            if cursor.rowcount == 0:
                raise KnowledgeNotFoundError(f"document {document_id!r} was not found")
        return self.get_document(document_id)

    def delete_document(self, document_id: str) -> sqlite3.Row:
        row = self.get_document(document_id)
        with self.connection() as connection:
            chunk_ids = [item[0] for item in connection.execute("SELECT id FROM knowledge_chunks WHERE document_id=?", (document_id,))]
            connection.executemany("DELETE FROM knowledge_chunks_fts WHERE chunk_id=?", ((item,) for item in chunk_ids))
            connection.execute("DELETE FROM knowledge_documents WHERE id=?", (document_id,))
            connection.execute(
                "DELETE FROM knowledge_graph_nodes WHERE base_id=? AND kind IN ('entity', 'term') "
                "AND NOT EXISTS (SELECT 1 FROM knowledge_graph_edges e WHERE e.base_id=knowledge_graph_nodes.base_id "
                "AND e.kind='mentions' AND (e.source_id=knowledge_graph_nodes.id OR e.target_id=knowledge_graph_nodes.id))",
                (str(row["base_id"]),),
            )
        return row

    def insert_job(self, values: dict[str, Any]) -> None:
        columns = list(values)
        with self.connection() as connection:
            connection.execute(
                f"INSERT INTO knowledge_jobs ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                [values[column] for column in columns],
            )

    def update_job(self, job_id: str, fields: dict[str, Any]) -> None:
        assignments = [f"{name}=?" for name in fields]
        values = list(fields.values())
        assignments.append("updated_at_ms=?")
        values.extend([now_ms(), job_id])
        with self.connection() as connection:
            connection.execute(f"UPDATE knowledge_jobs SET {', '.join(assignments)} WHERE id=?", values)

    def get_job(self, job_id: str) -> sqlite3.Row:
        return self.one(
            "SELECT j.*, d.base_id, d.display_name AS file_name, d.status AS document_status, "
            "d.revision AS document_revision FROM knowledge_jobs j "
            "JOIN knowledge_documents d ON d.id=j.document_id WHERE j.id=?",
            (job_id,),
        )

    def recoverable_jobs(self) -> list[sqlite3.Row]:
        return self.all(
            "SELECT j.*, d.base_id, d.display_name AS file_name, d.status AS document_status, "
            "d.revision AS document_revision FROM knowledge_jobs j "
            "JOIN knowledge_documents d ON d.id=j.document_id "
            "WHERE j.status IN ('queued', 'running') ORDER BY j.created_at_ms, j.id"
        )

    def reset_job_for_recovery(self, job_id: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE knowledge_jobs SET status='queued', stage='recovered', error_code='', "
                "error_message='', started_at_ms=NULL, finished_at_ms=NULL, updated_at_ms=? "
                "WHERE id=? AND status IN ('queued', 'running')",
                (now_ms(), job_id),
            )

    def cancel_job(self, job_id: str) -> sqlite3.Row:
        timestamp = now_ms()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT j.*, d.base_id, d.display_name AS file_name, d.status AS document_status, "
                "d.revision AS document_revision FROM knowledge_jobs j "
                "JOIN knowledge_documents d ON d.id=j.document_id WHERE j.id=?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise KnowledgeNotFoundError(f"knowledge job {job_id!r} was not found")
            if str(row["status"]) in {"queued", "running"}:
                connection.execute(
                    "UPDATE knowledge_jobs SET status='cancelled', stage='cancelled', "
                    "error_code='cancelled', error_message='cancelled by user', "
                    "finished_at_ms=?, updated_at_ms=? WHERE id=?",
                    (timestamp, timestamp, job_id),
                )
                connection.execute(
                    "UPDATE knowledge_documents SET revision=revision+1, status='failed', "
                    "error_code='cancelled', error_message='processing cancelled by user', updated_at_ms=? "
                    "WHERE id=? AND revision=? AND status IN ('queued', 'parsing', 'indexing')",
                    (timestamp, str(row["document_id"]), int(row["revision"])),
                )
        return self.get_job(job_id)

    def job_is_cancelled(self, job_id: str) -> bool:
        try:
            return str(self.one("SELECT status FROM knowledge_jobs WHERE id=?", (job_id,))["status"]) == "cancelled"
        except KnowledgeNotFoundError:
            return True

    def replace_chunks_if_revision(
        self,
        *,
        document_id: str,
        revision: int,
        chunks: list[dict[str, Any]],
        document_fields: dict[str, Any],
    ) -> bool:
        with self.connection() as connection:
            current = connection.execute(
                "SELECT revision FROM knowledge_documents WHERE id=?",
                (document_id,),
            ).fetchone()
            if current is None:
                return False
            if int(current["revision"]) != int(revision):
                return False
            old_ids = [item[0] for item in connection.execute("SELECT id FROM knowledge_chunks WHERE document_id=?", (document_id,))]
            connection.executemany("DELETE FROM knowledge_chunks_fts WHERE chunk_id=?", ((item,) for item in old_ids))
            connection.execute("DELETE FROM knowledge_chunks WHERE document_id=?", (document_id,))
            connection.execute(
                "DELETE FROM knowledge_graph_nodes WHERE base_id=(SELECT base_id FROM knowledge_documents WHERE id=?) "
                "AND kind IN ('entity', 'term') AND NOT EXISTS (SELECT 1 FROM knowledge_graph_edges e "
                "WHERE e.base_id=knowledge_graph_nodes.base_id AND e.kind='mentions' "
                "AND (e.source_id=knowledge_graph_nodes.id OR e.target_id=knowledge_graph_nodes.id))",
                (document_id,),
            )
            for chunk in chunks:
                connection.execute(
                    "INSERT INTO knowledge_chunks "
                    "(id, document_id, base_id, ordinal, content, heading, page, content_hash, created_at_ms) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        chunk["id"], document_id, chunk["base_id"], chunk["ordinal"], chunk["content"],
                        chunk.get("heading", ""), chunk.get("page"), chunk["content_hash"], now_ms(),
                    ),
                )
                connection.execute(
                    "INSERT INTO knowledge_chunks_fts(chunk_id, content) VALUES (?, ?)",
                    (chunk["id"], chunk["content"]),
                )
            assignments = [f"{name}=?" for name in document_fields]
            values = list(document_fields.values())
            assignments.append("updated_at_ms=?")
            values.extend([now_ms(), document_id, revision])
            cursor = connection.execute(
                f"UPDATE knowledge_documents SET {', '.join(assignments)} WHERE id=? AND revision=?",
                values,
            )
            return cursor.rowcount == 1

    def add_asset(self, *, sha256: str, media_type: str, byte_size: int, stored_path: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO knowledge_assets(sha256, media_type, byte_size, stored_path, created_at_ms) "
                "VALUES (?, ?, ?, ?, ?)",
                (sha256, media_type, byte_size, stored_path, now_ms()),
            )

    def link_asset(self, *, document_id: str, asset_sha256: str, original_name: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO knowledge_document_assets(document_id, asset_sha256, original_name) VALUES (?, ?, ?)",
                (document_id, asset_sha256, original_name),
            )

    def clear_document_asset_links(self, document_id: str) -> None:
        with self.connection() as connection:
            connection.execute("DELETE FROM knowledge_document_assets WHERE document_id=?", (document_id,))

    def replace_document_asset_links(
        self,
        document_id: str,
        assets: Sequence[tuple[str, str]],
    ) -> None:
        with self.connection() as connection:
            if connection.execute("SELECT 1 FROM knowledge_documents WHERE id=?", (document_id,)).fetchone() is None:
                raise KnowledgeNotFoundError(f"document {document_id!r} was not found")
            connection.execute("DELETE FROM knowledge_document_assets WHERE document_id=?", (document_id,))
            connection.executemany(
                "INSERT OR IGNORE INTO knowledge_document_assets(document_id, asset_sha256, original_name) "
                "VALUES (?, ?, ?)",
                ((document_id, sha256, name) for sha256, name in assets),
            )

    def delete_unreferenced_assets(self) -> list[str]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT a.sha256, a.stored_path FROM knowledge_assets a "
                "LEFT JOIN knowledge_document_assets da ON da.asset_sha256=a.sha256 "
                "WHERE da.asset_sha256 IS NULL"
            ).fetchall()
            connection.executemany(
                "DELETE FROM knowledge_assets WHERE sha256=?",
                ((str(row["sha256"]),) for row in rows),
            )
        return [str(row["stored_path"]) for row in rows]

    def search(
        self,
        query: str,
        *,
        base_ids: Sequence[str],
        limit: int,
        agent_only: bool,
        document_ids: Sequence[str] = (),
    ) -> list[SearchHit]:
        fts_query = _fts_query(query)
        if not fts_query:
            return []
        params: list[Any] = [fts_query]
        filters = ["d.status='ready'"]
        if agent_only:
            filters.append("b.agent_enabled=1")
        if base_ids:
            filters.append(f"c.base_id IN ({', '.join('?' for _ in base_ids)})")
            params.extend(base_ids)
        if document_ids:
            filters.append(f"c.document_id IN ({', '.join('?' for _ in document_ids)})")
            params.extend(document_ids)
        params.append(max(1, min(100, int(limit))))
        sql = (
            "SELECT c.id AS chunk_id, c.base_id, b.name AS base_name, c.document_id, "
            "d.display_name AS document_name, c.ordinal, c.content, c.heading, c.page, "
            "bm25(knowledge_chunks_fts) AS rank "
            "FROM knowledge_chunks_fts "
            "JOIN knowledge_chunks c ON c.id=knowledge_chunks_fts.chunk_id "
            "JOIN knowledge_documents d ON d.id=c.document_id "
            "JOIN knowledge_bases b ON b.id=c.base_id "
            f"WHERE knowledge_chunks_fts MATCH ? AND {' AND '.join(filters)} "
            "ORDER BY rank ASC, c.ordinal ASC LIMIT ?"
        )
        try:
            rows = self.all(sql, params)
        except sqlite3.OperationalError as exc:
            raise KnowledgeLibraryError(f"invalid full-text query: {exc}", code="invalid_search_query") from exc
        scores = _relative_fts_scores(rows)
        return [
            SearchHit(
                chunk_id=str(row["chunk_id"]),
                base_id=str(row["base_id"]),
                base_name=str(row["base_name"]),
                document_id=str(row["document_id"]),
                document_name=str(row["document_name"]),
                ordinal=int(row["ordinal"]),
                content=str(row["content"]),
                score=scores[index],
                page=int(row["page"]) if row["page"] is not None else None,
                heading=str(row["heading"] or ""),
            )
            for index, row in enumerate(rows)
        ]

    def export_search_snapshot(self, base_id: str) -> dict[str, Any]:
        """Export frozen search data without storage paths, jobs or credentials."""
        with self.connection() as connection:
            connection.execute("BEGIN")
            base = connection.execute("SELECT * FROM knowledge_bases WHERE id=?", (base_id,)).fetchone()
            if base is None:
                raise KnowledgeNotFoundError("knowledge base was not found")
            documents = connection.execute("SELECT id,display_name,sha256,byte_size,chunk_count,status,indexed_config_revision FROM knowledge_documents WHERE base_id=? ORDER BY rowid", (base_id,)).fetchall()
            size = connection.execute("SELECT COUNT(*),COALESCE(SUM(length(CAST(content AS BLOB))),0) FROM knowledge_chunks WHERE base_id=?", (base_id,)).fetchone()
            if not documents or len(documents) > 20000 or size[0] > 200000 or size[1] > 64 * 1024 * 1024:
                raise KnowledgeLibraryError("search snapshot exceeds the portable document or chunk budget", code="snapshot_budget")
            if any(row["status"] != "ready" or row["indexed_config_revision"] != base["config_revision"] for row in documents):
                raise KnowledgeLibraryError("search snapshot requires a complete current index", code="index_not_ready")
            chunks = connection.execute("SELECT id,document_id,ordinal,content,heading,page,content_hash FROM knowledge_chunks WHERE base_id=? ORDER BY rowid", (base_id,)).fetchall()
            if sum(row["chunk_count"] for row in documents) != len(chunks):
                raise KnowledgeLibraryError("search snapshot chunk counts are inconsistent", code="index_not_ready")
            return {"schemaVersion": "paw.knowledge-search-snapshot.v1",
                    "base": {"id": base_id, "name": base["name"], "chunkingConfig": json.loads(base["chunking_config_json"]),
                             "retrievalConfig": json.loads(base["retrieval_config_json"])},
                    "documents": [{"id": row["id"], "title": row["display_name"], "sha256": row["sha256"],
                                   "byteSize": row["byte_size"], "chunkCount": row["chunk_count"]} for row in documents],
                    "chunks": [{"id": row["id"], "documentId": row["document_id"], "ordinal": row["ordinal"],
                                "content": row["content"], "heading": row["heading"], "page": row["page"],
                                "contentHash": row["content_hash"]} for row in chunks]}

    def import_search_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Rehydrate an exported search cache into an empty library, atomically.

        This is an internal package/cache contract, not the document-ingestion
        path. It does not claim the original source files are present.
        """
        if (snapshot.get("schemaVersion") != "paw.knowledge-search-snapshot.v1"
                or not isinstance(snapshot.get("base"), dict) or not isinstance(snapshot.get("documents"), list)
                or not isinstance(snapshot.get("chunks"), list) or not 1 <= len(snapshot["documents"]) <= 20000
                or not 1 <= len(snapshot["chunks"]) <= 200000):
            raise KnowledgeLibraryError("invalid search snapshot", code="invalid_snapshot")
        base, documents, chunks = snapshot["base"], snapshot["documents"], snapshot["chunks"]
        ids = {row["id"] for row in documents}
        if len(ids) != len(documents) or len({row["id"] for row in chunks}) != len(chunks):
            raise KnowledgeLibraryError("duplicate search snapshot identity", code="invalid_snapshot")
        counts = {identifier: 0 for identifier in ids}
        total = 0
        for chunk in chunks:
            content = chunk["content"].encode("utf-8")
            total += len(content)
            if (chunk["documentId"] not in ids or total > 64 * 1024 * 1024
                    or hashlib.sha256(content).hexdigest() != chunk["contentHash"]):
                raise KnowledgeLibraryError("search snapshot content or reference mismatch", code="invalid_snapshot")
            counts[chunk["documentId"]] += 1
        if any(row["chunkCount"] != counts[row["id"]] for row in documents):
            raise KnowledgeLibraryError("search snapshot chunk counts do not match", code="invalid_snapshot")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM knowledge_bases LIMIT 1").fetchone():
                raise KnowledgeConflictError("search snapshot needs an empty package cache")
            connection.execute("INSERT INTO knowledge_bases(id,name,normalized_name,parser_mode,agent_enabled,chunking_config_json,retrieval_config_json,created_at_ms,updated_at_ms) VALUES(?,?,?,'builtin',1,?,?,0,0)",
                               (base["id"], base["name"], base["id"], json.dumps(base["chunkingConfig"]), json.dumps(base["retrievalConfig"])))
            connection.executemany("INSERT INTO knowledge_documents(id,base_id,display_name,source_name,sha256,byte_size,stored_path,status,chunk_count,indexed_config_revision,created_at_ms,updated_at_ms) VALUES(?,?,?,?,?,?,'','ready',?,1,0,0)",
                                   [(row["id"], base["id"], row["title"], "", row["sha256"], row["byteSize"], row["chunkCount"]) for row in documents])
            connection.executemany("INSERT INTO knowledge_chunks(id,document_id,base_id,ordinal,content,heading,page,content_hash,created_at_ms) VALUES(?,?,?,?,?,?,?,?,0)",
                                   [(row["id"], row["documentId"], base["id"], row["ordinal"], row["content"], row.get("heading", ""), row.get("page"), row["contentHash"]) for row in chunks])
            connection.executemany("INSERT INTO knowledge_chunks_fts(chunk_id,content) VALUES(?,?)", [(row["id"], row["content"]) for row in chunks])

    def hydrate_dense_hits(
        self,
        scored_ids: Sequence[tuple[str, float]],
        *,
        base_ids: Sequence[str],
        agent_only: bool,
        document_ids: Sequence[str] = (),
    ) -> list[SearchHit]:
        return self._hydrate_scored_hits(
            scored_ids,
            base_ids=base_ids,
            agent_only=agent_only,
            document_ids=document_ids,
            cosine_scores=True,
        )

    def hydrate_graph_hits(
        self,
        scored_ids: Sequence[tuple[str, float]],
        *,
        base_ids: Sequence[str],
        agent_only: bool,
        document_ids: Sequence[str] = (),
    ) -> list[SearchHit]:
        return self._hydrate_scored_hits(
            scored_ids,
            base_ids=base_ids,
            agent_only=agent_only,
            document_ids=document_ids,
            cosine_scores=False,
        )

    def _hydrate_scored_hits(
        self,
        scored_ids: Sequence[tuple[str, float]],
        *,
        base_ids: Sequence[str],
        agent_only: bool,
        document_ids: Sequence[str],
        cosine_scores: bool,
    ) -> list[SearchHit]:
        if not scored_ids:
            return []
        chunk_ids = [item[0] for item in scored_ids]
        filters = [f"c.id IN ({', '.join('?' for _ in chunk_ids)})", "d.status='ready'"]
        params: list[Any] = list(chunk_ids)
        if base_ids:
            filters.append(f"c.base_id IN ({', '.join('?' for _ in base_ids)})")
            params.extend(base_ids)
        if document_ids:
            filters.append(f"c.document_id IN ({', '.join('?' for _ in document_ids)})")
            params.extend(document_ids)
        if agent_only:
            filters.append("b.agent_enabled=1")
        rows = self.all(
            "SELECT c.id AS chunk_id, c.base_id, b.name AS base_name, c.document_id, "
            "d.display_name AS document_name, c.ordinal, c.content, c.heading, c.page "
            "FROM knowledge_chunks c JOIN knowledge_documents d ON d.id=c.document_id "
            "JOIN knowledge_bases b ON b.id=c.base_id "
            f"WHERE {' AND '.join(filters)}",
            params,
        )
        rows_by_id = {str(row["chunk_id"]): row for row in rows}
        hits: list[SearchHit] = []
        for chunk_id, raw_score in scored_ids:
            row = rows_by_id.get(chunk_id)
            if row is None:
                continue
            normalized_score = (float(raw_score) + 1.0) / 2.0 if cosine_scores else float(raw_score)
            hits.append(
                SearchHit(
                    chunk_id=chunk_id,
                    base_id=str(row["base_id"]),
                    base_name=str(row["base_name"]),
                    document_id=str(row["document_id"]),
                    document_name=str(row["document_name"]),
                    ordinal=int(row["ordinal"]),
                    content=str(row["content"]),
                    score=round(max(0.0, min(1.0, normalized_score)), 6),
                    page=int(row["page"]) if row["page"] is not None else None,
                    heading=str(row["heading"] or ""),
                )
            )
        return hits

    def document_ids_for_file_name(
        self,
        file_name: str,
        *,
        base_ids: Sequence[str],
        agent_only: bool,
    ) -> tuple[str, ...]:
        filters = ["d.status='ready'", "(LOWER(d.display_name)=LOWER(?) OR LOWER(d.source_name)=LOWER(?))"]
        params: list[Any] = [file_name, file_name]
        if base_ids:
            filters.append(f"d.base_id IN ({', '.join('?' for _ in base_ids)})")
            params.extend(base_ids)
        if agent_only:
            filters.append("b.agent_enabled=1")
        rows = self.all(
            "SELECT d.id FROM knowledge_documents d JOIN knowledge_bases b ON b.id=d.base_id "
            f"WHERE {' AND '.join(filters)} ORDER BY d.updated_at_ms DESC, d.id",
            params,
        )
        return tuple(str(row["id"]) for row in rows)

    def find_document_chunks(
        self,
        document_id: str,
        patterns: Sequence[str],
        *,
        case_sensitive: bool,
        offset: int,
        limit: int,
        agent_only: bool,
    ) -> list[sqlite3.Row]:
        document = self.get_document(document_id)
        if str(document["status"]) != "ready" or (agent_only and not bool(document["base_agent_enabled"])):
            raise KnowledgeNotFoundError(f"document {document_id!r} is not available")
        expression = "c.content" if case_sensitive else "LOWER(c.content)"
        predicates = [f"INSTR({expression}, ?) > 0" for _ in patterns]
        values = list(patterns if case_sensitive else [pattern.lower() for pattern in patterns])
        values.extend([document_id, max(0, int(offset)), max(1, min(500, int(limit)))])
        return self.all(
            "SELECT c.*, d.display_name AS document_name, b.name AS base_name, "
            "(SELECT COALESCE(SUM(1 + LENGTH(p.content) - LENGTH(REPLACE(p.content, CHAR(10), ''))), 0) "
            " FROM knowledge_chunks p WHERE p.document_id=c.document_id AND p.ordinal<c.ordinal) AS lines_before "
            "FROM knowledge_chunks c JOIN knowledge_documents d ON d.id=c.document_id "
            "JOIN knowledge_bases b ON b.id=c.base_id "
            f"WHERE ({' OR '.join(predicates)}) AND c.document_id=? AND c.ordinal>=? "
            "ORDER BY c.ordinal LIMIT ?",
            values,
        )

    def find_documents(self, query: str, *, base_ids: Sequence[str], limit: int, agent_only: bool) -> list[sqlite3.Row]:
        filters = ["d.status='ready'", "(LOWER(d.display_name) LIKE ? OR LOWER(d.source_name) LIKE ?)"]
        needle = f"%{query.strip().lower()}%"
        params: list[Any] = [needle, needle]
        if agent_only:
            filters.append("b.agent_enabled=1")
        if base_ids:
            filters.append(f"d.base_id IN ({', '.join('?' for _ in base_ids)})")
            params.extend(base_ids)
        params.append(max(1, min(100, int(limit))))
        return self.all(
            "SELECT d.*, b.name AS base_name, b.agent_enabled AS base_agent_enabled "
            "FROM knowledge_documents d JOIN knowledge_bases b ON b.id=d.base_id "
            f"WHERE {' AND '.join(filters)} ORDER BY d.updated_at_ms DESC LIMIT ?",
            params,
        )

    def open_chunk(self, chunk_id: str, *, before: int, after: int, agent_only: bool) -> list[sqlite3.Row]:
        anchor = self.one(
            "SELECT c.*, d.status AS document_status, d.display_name AS document_name, "
            "b.name AS base_name, b.agent_enabled AS base_agent_enabled "
            "FROM knowledge_chunks c JOIN knowledge_documents d ON d.id=c.document_id "
            "JOIN knowledge_bases b ON b.id=c.base_id WHERE c.id=?",
            (chunk_id,),
        )
        if str(anchor["document_status"]) != "ready" or (agent_only and not bool(anchor["base_agent_enabled"])):
            raise KnowledgeNotFoundError(f"chunk {chunk_id!r} is not available")
        return self.all(
            "SELECT c.*, d.display_name AS document_name, b.name AS base_name "
            "FROM knowledge_chunks c JOIN knowledge_documents d ON d.id=c.document_id "
            "JOIN knowledge_bases b ON b.id=c.base_id "
            "WHERE c.document_id=? AND c.ordinal BETWEEN ? AND ? ORDER BY c.ordinal",
            (
                anchor["document_id"],
                max(0, int(anchor["ordinal"]) - max(0, int(before))),
                int(anchor["ordinal"]) + max(0, int(after)),
            ),
        )

    def open_document(
        self,
        document_id: str,
        *,
        offset: int,
        limit: int,
        agent_only: bool,
    ) -> list[sqlite3.Row]:
        document = self.get_document(document_id)
        if str(document["status"]) != "ready" or (agent_only and not bool(document["base_agent_enabled"])):
            raise KnowledgeNotFoundError(f"document {document_id!r} is not available")
        return self.all(
            "SELECT c.*, d.display_name AS document_name, b.name AS base_name "
            "FROM knowledge_chunks c JOIN knowledge_documents d ON d.id=c.document_id "
            "JOIN knowledge_bases b ON b.id=c.base_id "
            "WHERE c.document_id=? AND c.ordinal>=? ORDER BY c.ordinal LIMIT ?",
            (document_id, max(0, int(offset)), max(1, min(50, int(limit)))),
        )

    def open_document_lines(
        self,
        document_id: str,
        *,
        line_start: int,
        line_limit: int,
        agent_only: bool,
    ) -> tuple[list[sqlite3.Row], int]:
        document = self.get_document(document_id)
        if str(document["status"]) != "ready" or (agent_only and not bool(document["base_agent_enabled"])):
            raise KnowledgeNotFoundError(f"document {document_id!r} is not available")
        start = max(1, int(line_start))
        end = start + max(1, min(300, int(line_limit)))
        rows = self.all(
            "WITH numbered AS ("
            " SELECT c.*, d.display_name AS document_name, b.name AS base_name, "
            "  1 + LENGTH(c.content) - LENGTH(REPLACE(c.content, CHAR(10), '')) AS line_count, "
            "  COALESCE(SUM(1 + LENGTH(c.content) - LENGTH(REPLACE(c.content, CHAR(10), ''))) "
            "   OVER (ORDER BY c.ordinal ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS lines_before "
            " FROM knowledge_chunks c JOIN knowledge_documents d ON d.id=c.document_id "
            " JOIN knowledge_bases b ON b.id=c.base_id WHERE c.document_id=?"
            ") SELECT * FROM numbered WHERE lines_before + line_count >= ? AND lines_before < ? ORDER BY ordinal",
            (document_id, start, end),
        )
        total = int(
            self.one(
                "SELECT COALESCE(SUM(1 + LENGTH(content) - LENGTH(REPLACE(content, CHAR(10), ''))), 0) AS count "
                "FROM knowledge_chunks WHERE document_id=?",
                (document_id,),
            )["count"]
        )
        return rows, total

    def document_chunks(self, document_id: str, *, offset: int, limit: int) -> tuple[list[sqlite3.Row], int]:
        total = int(self.one("SELECT COUNT(*) AS count FROM knowledge_chunks WHERE document_id=?", (document_id,))["count"])
        rows = self.all(
            "SELECT * FROM knowledge_chunks WHERE document_id=? ORDER BY ordinal LIMIT ? OFFSET ?",
            (document_id, max(1, min(500, int(limit))), max(0, int(offset))),
        )
        return rows, total

    def document_pages(self, document_id: str) -> list[sqlite3.Row]:
        return self.all(
            "SELECT page, COUNT(*) AS chunk_count, MIN(ordinal) AS first_ordinal, MAX(ordinal) AS last_ordinal "
            "FROM knowledge_chunks WHERE document_id=? AND page IS NOT NULL GROUP BY page ORDER BY page",
            (document_id,),
        )

    def document_assets(self, document_id: str) -> list[sqlite3.Row]:
        return self.all(
            "SELECT a.sha256, a.media_type, a.byte_size, a.stored_path, da.original_name "
            "FROM knowledge_document_assets da JOIN knowledge_assets a ON a.sha256=da.asset_sha256 "
            "WHERE da.document_id=? ORDER BY da.original_name COLLATE NOCASE, a.sha256",
            (document_id,),
        )

    def document_asset(self, *, base_id: str, document_id: str, asset_id: str) -> sqlite3.Row:
        return self.one(
            "SELECT a.sha256, a.media_type, a.byte_size, a.stored_path, da.original_name "
            "FROM knowledge_document_assets da JOIN knowledge_assets a ON a.sha256=da.asset_sha256 "
            "JOIN knowledge_documents d ON d.id=da.document_id "
            "WHERE d.base_id=? AND d.id=? AND a.sha256=?",
            (base_id, document_id, asset_id),
        )

    def counts(self) -> dict[str, int]:
        with self.connection() as connection:
            return {
                "baseCount": int(connection.execute("SELECT COUNT(*) FROM knowledge_bases").fetchone()[0]),
                "agentEnabledBaseCount": int(connection.execute("SELECT COUNT(*) FROM knowledge_bases WHERE agent_enabled=1").fetchone()[0]),
                "documentCount": int(connection.execute("SELECT COUNT(*) FROM knowledge_documents").fetchone()[0]),
                "readyDocumentCount": int(connection.execute("SELECT COUNT(*) FROM knowledge_documents WHERE status='ready'").fetchone()[0]),
                "failedDocumentCount": int(connection.execute("SELECT COUNT(*) FROM knowledge_documents WHERE status='failed'").fetchone()[0]),
                "staleDocumentCount": int(connection.execute("SELECT COUNT(*) FROM knowledge_documents WHERE status='stale'").fetchone()[0]),
                "chunkCount": int(connection.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0]),
                "runningJobCount": int(connection.execute("SELECT COUNT(*) FROM knowledge_jobs WHERE status IN ('queued','running')").fetchone()[0]),
            }

    def list_jobs(self, *, base_id: str = "", limit: int = 100) -> list[sqlite3.Row]:
        params: list[Any] = []
        condition = ""
        if base_id:
            condition = "WHERE d.base_id=?"
            params.append(base_id)
        params.append(max(1, min(500, int(limit))))
        return self.all(
            "SELECT j.*, d.base_id, d.display_name AS file_name "
            "FROM knowledge_jobs j JOIN knowledge_documents d ON d.id=j.document_id "
            f"{condition} ORDER BY j.created_at_ms DESC LIMIT ?",
            params,
        )


def _relative_fts_scores(rows: Sequence[sqlite3.Row]) -> list[float]:
    if not rows:
        return []
    ranks = [float(row["rank"] or 0.0) for row in rows]
    best = min(ranks)
    worst = max(ranks)
    spread = worst - best
    if spread <= max(1e-12, abs(best) * 1e-9):
        return [1.0] * len(ranks)
    # FTS5 BM25 is ordered ascending and is commonly negative. Normalize the
    # returned set instead of clamping negative ranks, which made every hit 100%.
    return [round(0.2 + 0.8 * ((worst - rank) / spread), 6) for rank in ranks]


def _fts_query(query: str) -> str:
    normalized = " ".join(str(query or "").replace("\x00", " ").split()).strip()
    if not normalized:
        return ""
    tokens = [token for token in re_split_query(normalized) if token][:12]
    if not tokens:
        return ""
    return " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


def re_split_query(value: str) -> list[str]:
    import re

    return re.findall(r"[\w\u3400-\u9fff]+", value, flags=re.UNICODE)


def decode_metadata(row: sqlite3.Row) -> dict[str, Any]:
    try:
        value = json.loads(str(row["metadata_json"] or "{}"))
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def rank_retrieval_hits(
    lexical_hits: Sequence[Any],
    dense_hits: Sequence[Any],
    graph_hits: Sequence[Any] = (),
    *,
    requested_mode: str,
    config: dict[str, Any],
) -> tuple[list[Any], str]:
    if requested_mode == "lexical":
        return [
            replace(
                hit,
                diagnostics={
                    "effectiveMode": "lexical",
                    "lexicalRank": rank,
                    "lexicalScore": hit.score,
                },
            )
            for rank, hit in enumerate(lexical_hits, start=1)
        ], "lexical"
    if requested_mode == "dense":
        return [
            replace(
                hit,
                diagnostics={
                    "effectiveMode": "dense",
                    "denseRank": rank,
                    "denseScore": hit.score,
                },
            )
            for rank, hit in enumerate(dense_hits, start=1)
        ], "dense"
    if not dense_hits and not graph_hits:
        return [
            replace(
                hit,
                diagnostics={
                    "effectiveMode": "lexical",
                    "fallbackFrom": "hybrid",
                    "lexicalRank": rank,
                    "lexicalScore": hit.score,
                },
            )
            for rank, hit in enumerate(lexical_hits, start=1)
        ], "lexical"
    if not lexical_hits and not graph_hits:
        return [
            replace(
                hit,
                diagnostics={
                    "effectiveMode": "dense",
                    "fallbackFrom": "hybrid",
                    "denseRank": rank,
                    "denseScore": hit.score,
                },
            )
            for rank, hit in enumerate(dense_hits, start=1)
        ], "dense"

    lexical_weight = float(config["lexicalWeight"])
    dense_weight = float(config["denseWeight"])
    graph_weight = float(config["graphWeight"]) if graph_hits else 0.0
    rrf_k = int(config["rrfK"])
    lexical_ranks = {hit.chunk_id: rank for rank, hit in enumerate(lexical_hits, start=1)}
    dense_ranks = {hit.chunk_id: rank for rank, hit in enumerate(dense_hits, start=1)}
    graph_ranks = {hit.chunk_id: rank for rank, hit in enumerate(graph_hits, start=1)}
    lexical_scores = {hit.chunk_id: hit.score for hit in lexical_hits}
    dense_scores = {hit.chunk_id: hit.score for hit in dense_hits}
    graph_scores = {hit.chunk_id: hit.score for hit in graph_hits}
    graph_diagnostics = {hit.chunk_id: dict(hit.diagnostics) for hit in graph_hits}
    hits_by_id = {hit.chunk_id: hit for hit in lexical_hits}
    hits_by_id.update({hit.chunk_id: hit for hit in dense_hits})
    hits_by_id.update({hit.chunk_id: hit for hit in graph_hits})
    available_weight = (
        (lexical_weight if lexical_hits else 0.0)
        + (dense_weight if dense_hits else 0.0)
        + graph_weight
    )
    maximum = available_weight / (rrf_k + 1)
    ranked: list[Any] = []
    for chunk_id, hit in hits_by_id.items():
        lexical_rank = lexical_ranks.get(chunk_id)
        dense_rank = dense_ranks.get(chunk_id)
        graph_rank = graph_ranks.get(chunk_id)
        raw_score = 0.0
        if lexical_rank is not None:
            raw_score += lexical_weight / (rrf_k + lexical_rank)
        if dense_rank is not None:
            raw_score += dense_weight / (rrf_k + dense_rank)
        if graph_rank is not None:
            raw_score += graph_weight / (rrf_k + graph_rank)
        fused_score = raw_score / maximum if maximum > 0 else 0.0
        ranked.append(
            replace(
                hit,
                score=round(max(0.0, min(1.0, fused_score)), 6),
                diagnostics={
                    "effectiveMode": "hybrid",
                    "fusion": "weighted-rrf-graph" if graph_hits else "weighted-rrf",
                    "fusionScoreRaw": round(raw_score, 9),
                    "lexicalRank": lexical_rank,
                    "denseRank": dense_rank,
                    "graphRank": graph_rank,
                    "lexicalScore": lexical_scores.get(chunk_id),
                    "denseScore": dense_scores.get(chunk_id),
                    "graphScore": graph_scores.get(chunk_id),
                    "lexicalWeight": lexical_weight,
                    "denseWeight": dense_weight,
                    "graphWeight": graph_weight,
                    "graphMatches": graph_diagnostics.get(chunk_id, {}).get("graphMatches", []),
                    "graphPaths": graph_diagnostics.get(chunk_id, {}).get("graphPaths", []),
                    "rrfK": rrf_k,
                },
            )
        )
    ranked.sort(key=lambda hit: (-hit.score, hit.chunk_id))
    return ranked, "hybrid"
