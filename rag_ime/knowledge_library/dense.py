from __future__ import annotations

import base64
import math
import sys
import zlib
from array import array
from collections.abc import Mapping

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

try:
    from ..embeddings import (
        EmbeddingProvider,
        HashingEmbeddingProvider,
        NullEmbeddingProvider,
        cosine_similarity,
        embed_query,
        embedding_provider_info,
    )
except ImportError:  # Standalone packages contain their own embedding owner.
    from .embeddings import (
        EmbeddingProvider,
        HashingEmbeddingProvider,
        NullEmbeddingProvider,
        cosine_similarity,
        embed_query,
        embedding_provider_info,
    )



DENSE_SNAPSHOT_SCHEMA = "paw.knowledge-dense-snapshot.v1"
MAX_DENSE_SNAPSHOT_VECTORS = 200_000
MAX_DENSE_SNAPSHOT_DIMENSIONS = 65_536
MAX_DENSE_SNAPSHOT_FILE_BYTES = 80 * 1024 * 1024
MAX_DENSE_SNAPSHOT_RAW_BYTES = 256 * 1024 * 1024


def _float32_bytes(values: Sequence[float]) -> bytes:
    converted = array("f", [float(value) for value in values])
    if sys.byteorder != "little":
        converted.byteswap()
    return converted.tobytes()


def _float32_values(payload: bytes) -> list[float]:
    if len(payload) % 4:
        raise ValueError("float32 payload is not aligned")
    converted = array("f")
    converted.frombytes(payload)
    if sys.byteorder != "little":
        converted.byteswap()
    return [float(value) for value in converted]


class DenseIndex(Protocol):
    """Optional projection index. SQLite remains the canonical content store."""

    def replace_document(self, document_id: str, chunks: Sequence[dict[str, Any]]) -> None:
        ...

    def delete_document(self, document_id: str) -> None:
        ...

    def search(
        self,
        query: str,
        *,
        base_ids: Sequence[str],
        limit: int,
        document_ids: Sequence[str] = (),
    ) -> Sequence[tuple[str, float]]:
        ...

    def status(self) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class NullDenseIndex:
    reason: str = "dense index is not configured; lexical FTS5 search remains available"

    def replace_document(self, document_id: str, chunks: Sequence[dict[str, Any]]) -> None:
        return None

    def delete_document(self, document_id: str) -> None:
        return None

    def search(
        self,
        query: str,
        *,
        base_ids: Sequence[str],
        limit: int,
        document_ids: Sequence[str] = (),
    ) -> Sequence[tuple[str, float]]:
        return ()

    def status(self) -> dict[str, Any]:
        return {
            "available": False,
            "degraded": True,
            "kind": "none",
            "ann": False,
            "scalable": False,
            "provider": embedding_provider_info(NullEmbeddingProvider()),
            "reason": self.reason,
        }


class SqliteDenseIndex:
    """Persistent local vector projection backed by the document library SQLite file."""

    def __init__(
        self,
        database_path: Path,
        provider: EmbeddingProvider,
        *,
        batch_size: int = 32,
        fallback_from: str = "",
        fallback_reason: str = "",
    ):
        self.database_path = Path(database_path)
        self.provider = provider
        self.batch_size = max(1, min(128, int(batch_size)))
        self.fallback_from = str(fallback_from or "")
        self.fallback_reason = str(fallback_reason or "")
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.database_path), timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _migrate(self) -> None:
        with self._connection() as connection:
            columns = connection.execute(
                "PRAGMA table_info(knowledge_dense_chunks)"
            ).fetchall()
            primary_key = [
                str(row["name"])
                for row in sorted(columns, key=lambda row: int(row["pk"]))
                if int(row["pk"]) > 0
            ]
            if columns and primary_key != ["chunk_id", "fingerprint"]:
                connection.execute("DROP INDEX IF EXISTS idx_knowledge_dense_base")
                connection.execute(
                    "ALTER TABLE knowledge_dense_chunks "
                    "RENAME TO knowledge_dense_chunks_legacy_v1"
                )
                self._create_projection_table(connection)
                connection.execute(
                    "INSERT OR REPLACE INTO knowledge_dense_chunks"
                    "(chunk_id, document_id, base_id, fingerprint, vector_json) "
                    "SELECT chunk_id, document_id, base_id, fingerprint, vector_json "
                    "FROM knowledge_dense_chunks_legacy_v1"
                )
                connection.execute("DROP TABLE knowledge_dense_chunks_legacy_v1")
            else:
                self._create_projection_table(connection)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_knowledge_dense_base ON knowledge_dense_chunks(base_id, fingerprint)"
            )

    @staticmethod
    def _create_projection_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS knowledge_dense_chunks ("
            "chunk_id TEXT NOT NULL, document_id TEXT NOT NULL, base_id TEXT NOT NULL, "
            "fingerprint TEXT NOT NULL, vector_json TEXT NOT NULL, "
            "PRIMARY KEY(chunk_id, fingerprint))"
        )

    def replace_document(self, document_id: str, chunks: Sequence[dict[str, Any]]) -> None:
        records: list[tuple[str, str, str, str, str]] = []
        texts = [str(chunk.get("content") or "") for chunk in chunks]
        embed_many = getattr(self.provider, "embed_many", None)
        vectors = (
            embed_many(texts, batch_size=self.batch_size)
            if callable(embed_many)
            else [self.provider.embed(text) for text in texts]
        )
        if len(vectors) != len(chunks):
            raise RuntimeError("embedding provider returned an unexpected batch size")
        for chunk, vector in zip(chunks, vectors):
            if not vector:
                continue
            records.append(
                (
                    str(chunk["id"]),
                    document_id,
                    str(chunk["base_id"]),
                    str(self.provider.fingerprint),
                    json.dumps(vector, separators=(",", ":")),
                )
            )
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM knowledge_dense_chunks WHERE document_id=? AND fingerprint=?",
                (document_id, str(self.provider.fingerprint)),
            )
            connection.executemany(
                "INSERT INTO knowledge_dense_chunks(chunk_id, document_id, base_id, fingerprint, vector_json) "
                "VALUES (?, ?, ?, ?, ?)",
                records,
            )

    def export_snapshot(
        self,
        *,
        base_id: str,
        document_hashes: Mapping[str, str] | None = None,
        provider_config: Mapping[str, Any] | None = None,
        model_revision: str = "",
    ) -> dict[str, Any]:
        """Export this exact provider projection as a compact portable payload.

        The SQLite table stays the owner of vector identity and the exported
        payload is only a rebuildable cache.  Vectors are serialized as
        little-endian float32 values so an App can import them back into the
        same ``knowledge_dense_chunks`` table and reuse the normal exact scan.
        """
        clean_base_id = str(base_id or "")
        if not clean_base_id:
            raise ValueError("dense snapshot requires a base id")
        hashes = {str(key): str(value) for key, value in (document_hashes or {}).items()}
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT chunk_id, document_id, base_id, vector_json "
                "FROM knowledge_dense_chunks WHERE base_id=? AND fingerprint=? "
                "ORDER BY chunk_id",
                (clean_base_id, str(self.provider.fingerprint)),
            ).fetchall()
        if not rows:
            raise ValueError("dense snapshot has no vectors for this base")
        if len(rows) > MAX_DENSE_SNAPSHOT_VECTORS:
            raise ValueError("dense snapshot exceeds the portable vector budget")

        chunks: list[dict[str, str]] = []
        raw_vectors: list[bytes] = []
        dimensions = 0
        seen_chunks: set[str] = set()
        seen_documents: set[str] = set()
        for row in rows:
            chunk_id = str(row["chunk_id"])
            document_id = str(row["document_id"])
            row_base_id = str(row["base_id"])
            if chunk_id in seen_chunks or row_base_id != clean_base_id:
                raise ValueError("dense snapshot contains duplicate or foreign chunk identity")
            seen_chunks.add(chunk_id)
            seen_documents.add(document_id)
            try:
                vector = [float(value) for value in json.loads(str(row["vector_json"]))]
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError("dense snapshot contains an invalid vector") from exc
            if not vector or len(vector) > MAX_DENSE_SNAPSHOT_DIMENSIONS or not all(math.isfinite(value) for value in vector):
                raise ValueError("dense snapshot contains an invalid vector dimension")
            if dimensions and len(vector) != dimensions:
                raise ValueError("dense snapshot vectors have inconsistent dimensions")
            dimensions = len(vector)
            raw = _float32_bytes(vector)
            raw_vectors.append(raw)
            chunks.append(
                {
                    "chunkId": chunk_id,
                    "documentId": document_id,
                    "baseId": row_base_id,
                    "vectorSha256": hashlib.sha256(raw).hexdigest(),
                }
            )
        raw_payload = b"".join(raw_vectors)
        compressed = zlib.compress(raw_payload, level=9)
        if len(compressed) > MAX_DENSE_SNAPSHOT_FILE_BYTES or len(raw_payload) > MAX_DENSE_SNAPSHOT_RAW_BYTES:
            raise ValueError("dense snapshot exceeds the portable byte budget")
        documents = [
            {"id": document_id, "sha256": hashes.get(document_id, "")}
            for document_id in sorted(seen_documents)
        ]
        allowed_provider_fields = {"provider", "model", "modelReference", "modelRevision", "dimensions", "queryPrefix",
            "documentPrefix", "denseBackend", "providerFingerprint", "portableFingerprint", "fingerprint", "semantic",
            "configured", "modelConfigSha256", "modelCardSha256"}
        public_provider = {key: value for key, value in (provider_config or embedding_provider_info(self.provider)).items()
                           if key in allowed_provider_fields}
        portable_fingerprint = str(
            public_provider.get("portableFingerprint")
            or public_provider.get("providerFingerprint")
            or ""
        )
        public_provider.pop("apiKey", None)
        public_provider.pop("secret", None)
        public_provider.pop("portableFingerprint", None)
        origin_fingerprint = str(self.provider.fingerprint)
        return {
            "schemaVersion": DENSE_SNAPSHOT_SCHEMA,
            "backend": "sqlite-exact",
            "baseId": clean_base_id,
            "fingerprint": portable_fingerprint or origin_fingerprint,
            **(
                {"originFingerprintSha256": hashlib.sha256(origin_fingerprint.encode("utf-8")).hexdigest()}
                if portable_fingerprint and portable_fingerprint != origin_fingerprint
                else {}
            ),
            "provider": public_provider,
            "modelRevision": str(model_revision or ""),
            "vectorFormat": "float32-le",
            "compression": "zlib",
            "dimension": dimensions,
            "vectorCount": len(chunks),
            "chunks": chunks,
            "documents": documents,
            "payloadSha256": hashlib.sha256(compressed).hexdigest(),
            "payload": base64.b64encode(compressed).decode("ascii"),
        }

    def import_snapshot(
        self,
        snapshot: Mapping[str, Any],
        *,
        expected_base_id: str = "",
        expected_document_hashes: Mapping[str, str] | None = None,
        expected_fingerprint: str = "",
        expected_model_revision: str = "",
        replace: bool = False,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> None:
        """Validate and import one portable vector projection into this table."""
        if not isinstance(snapshot, Mapping) or snapshot.get("schemaVersion") != DENSE_SNAPSHOT_SCHEMA:
            raise ValueError("invalid dense snapshot")
        base_id = str(snapshot.get("baseId") or "")
        if not base_id or (expected_base_id and base_id != str(expected_base_id)):
            raise ValueError("dense snapshot base identity mismatch")
        fingerprint = str(snapshot.get("fingerprint") or "")
        provider_fingerprint = str(getattr(self.provider, "fingerprint", "") or "")
        if not fingerprint or fingerprint != provider_fingerprint or (expected_fingerprint and fingerprint != str(expected_fingerprint)):
            raise ValueError("dense snapshot provider fingerprint mismatch")
        origin_hash = str(snapshot.get("originFingerprintSha256") or "")
        if origin_hash and (len(origin_hash) != 64 or any(char not in "0123456789abcdef" for char in origin_hash.lower())):
            raise ValueError("dense snapshot origin fingerprint hash is invalid")
        model_revision = str(snapshot.get("modelRevision") or "")
        if expected_model_revision and model_revision != str(expected_model_revision):
            raise ValueError("dense snapshot model revision mismatch")
        if snapshot.get("backend") != "sqlite-exact" or snapshot.get("vectorFormat") != "float32-le" or snapshot.get("compression") != "zlib":
            raise ValueError("unsupported dense snapshot format")
        try:
            dimensions = int(snapshot.get("dimension"))
            vector_count = int(snapshot.get("vectorCount"))
        except (TypeError, ValueError) as exc:
            raise ValueError("dense snapshot dimensions are invalid") from exc
        chunks = snapshot.get("chunks")
        documents = snapshot.get("documents")
        if not 1 <= dimensions <= MAX_DENSE_SNAPSHOT_DIMENSIONS or not 1 <= vector_count <= MAX_DENSE_SNAPSHOT_VECTORS:
            raise ValueError("dense snapshot dimensions or count are invalid")
        if not isinstance(chunks, list) or len(chunks) != vector_count or not isinstance(documents, list):
            raise ValueError("dense snapshot manifest counts are invalid")
        if len({str(item.get("chunkId") or "") for item in chunks if isinstance(item, Mapping)}) != vector_count:
            raise ValueError("dense snapshot contains duplicate chunks")
        document_rows: dict[str, str] = {}
        for item in documents:
            if not isinstance(item, Mapping) or not str(item.get("id") or ""):
                raise ValueError("dense snapshot document identity is invalid")
            document_id = str(item["id"])
            digest = str(item.get("sha256") or "")
            if digest and (len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest.lower())):
                raise ValueError("dense snapshot document hash is invalid")
            if document_id in document_rows:
                raise ValueError("dense snapshot contains duplicate documents")
            document_rows[document_id] = digest
        expected_hashes = {str(key): str(value) for key, value in (expected_document_hashes or {}).items()}
        if expected_hashes and document_rows != expected_hashes:
            raise ValueError("dense snapshot document hashes mismatch")
        expected_bytes = vector_count * dimensions * 4
        if expected_bytes > MAX_DENSE_SNAPSHOT_RAW_BYTES:
            raise ValueError("dense snapshot exceeds the portable byte budget")
        try:
            compressed = base64.b64decode(str(snapshot.get("payload") or ""), validate=True)
            if len(compressed) > MAX_DENSE_SNAPSHOT_FILE_BYTES:
                raise ValueError("dense snapshot exceeds the portable byte budget")
            if hashlib.sha256(compressed).hexdigest() != str(snapshot.get("payloadSha256") or ""):
                raise ValueError("dense snapshot payload hash mismatch")
            # Never hand an unbounded compressed stream to zlib.  A valid
            # projection has an exact raw size derived from its manifest, so
            # allow only one extra byte while decoding and require one
            # complete stream with no concatenated/trailing data.
            decoder = zlib.decompressobj()
            raw_payload = decoder.decompress(compressed, expected_bytes + 1)
            if len(raw_payload) > expected_bytes or decoder.unconsumed_tail:
                raise ValueError("dense snapshot payload exceeds its declared size")
            raw_payload += decoder.flush(expected_bytes + 1 - len(raw_payload))
            if (
                len(raw_payload) > expected_bytes
                or not decoder.eof
                or decoder.unused_data
                or decoder.unconsumed_tail
            ):
                raise ValueError("dense snapshot payload is not one complete stream")
        except (ValueError, zlib.error) as exc:
            raise ValueError("dense snapshot payload is invalid") from exc
        if len(raw_payload) != expected_bytes:
            raise ValueError("dense snapshot payload size mismatch")
        vectors = _float32_values(raw_payload)
        records: list[tuple[str, str, str, str, str]] = []
        with self._connection() as connection:
            for index, item in enumerate(chunks):
                if index % 128 == 0 and cancelled():
                    raise InterruptedError("dense projection restoration was cancelled")
                if not isinstance(item, Mapping):
                    raise ValueError("dense snapshot chunk identity is invalid")
                chunk_id = str(item.get("chunkId") or "")
                document_id = str(item.get("documentId") or "")
                row_base_id = str(item.get("baseId") or "")
                if not chunk_id or document_id not in document_rows or row_base_id != base_id:
                    raise ValueError("dense snapshot chunk identity mismatch")
                row = connection.execute(
                    "SELECT c.document_id, c.base_id, d.sha256 FROM knowledge_chunks c "
                    "JOIN knowledge_documents d ON d.id=c.document_id WHERE c.id=?",
                    (chunk_id,),
                ).fetchone()
                if row is None or str(row["document_id"]) != document_id or str(row["base_id"]) != base_id:
                    raise ValueError("dense snapshot chunk is not in the current search snapshot")
                expected_hash = document_rows[document_id]
                if expected_hash and str(row["sha256"]) != expected_hash:
                    raise ValueError("dense snapshot document content mismatch")
                vector = vectors[index * dimensions : (index + 1) * dimensions]
                if not all(math.isfinite(value) for value in vector):
                    raise ValueError("dense snapshot contains non-finite vectors")
                raw = _float32_bytes(vector)
                if hashlib.sha256(raw).hexdigest() != str(item.get("vectorSha256") or ""):
                    raise ValueError("dense snapshot vector hash mismatch")
                records.append(
                    (chunk_id, document_id, base_id, fingerprint, json.dumps(vector, separators=(",", ":")))
                )
            canonical_ids = {str(row[0]) for row in connection.execute("SELECT id FROM knowledge_chunks WHERE base_id=?", (base_id,))}
            if canonical_ids != {item[0] for item in records}:
                raise ValueError("dense snapshot must cover the complete search projection")
            existing = int(
                connection.execute(
                    "SELECT COUNT(*) FROM knowledge_dense_chunks WHERE base_id=? AND fingerprint=?",
                    (base_id, fingerprint),
                ).fetchone()[0]
            )
            if existing and not replace:
                raise ValueError("dense snapshot cache already contains this projection")
            if replace:
                connection.execute(
                    "DELETE FROM knowledge_dense_chunks WHERE base_id=? AND fingerprint=?",
                    (base_id, fingerprint),
                )
            if cancelled():
                raise InterruptedError("dense projection restoration was cancelled")
            connection.executemany(
                "INSERT INTO knowledge_dense_chunks(chunk_id, document_id, base_id, fingerprint, vector_json) "
                "VALUES (?, ?, ?, ?, ?)",
                records,
            )
            if cancelled():
                raise InterruptedError("dense projection restoration was cancelled")

    def delete_document(self, document_id: str) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM knowledge_dense_chunks WHERE document_id=?", (document_id,))

    def search(
        self,
        query: str,
        *,
        base_ids: Sequence[str],
        limit: int,
        document_ids: Sequence[str] = (),
    ) -> Sequence[tuple[str, float]]:
        vector = embed_query(self.provider, query)
        if not vector:
            return ()
        if not all(math.isfinite(value) for value in vector):
            raise RuntimeError("dense query encoder returned non-finite values")
        params: list[Any] = [str(self.provider.fingerprint)]
        filter_sql = ""
        if base_ids:
            filter_sql += f" AND base_id IN ({', '.join('?' for _ in base_ids)})"
            params.extend(base_ids)
        if document_ids:
            filter_sql += f" AND document_id IN ({', '.join('?' for _ in document_ids)})"
            params.extend(document_ids)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT chunk_id, vector_json FROM knowledge_dense_chunks WHERE fingerprint=?{filter_sql}",
                params,
            ).fetchall()
        scored: list[tuple[str, float]] = []
        for row in rows:
            try:
                stored = [float(value) for value in json.loads(str(row["vector_json"]))]
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if len(stored) != len(vector) or not all(math.isfinite(value) for value in stored):
                raise RuntimeError("dense query encoder dimensions do not match the frozen projection")
            scored.append((str(row["chunk_id"]), cosine_similarity(vector, stored)))
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[: max(1, min(100, int(limit)))]

    def status(self) -> dict[str, Any]:
        with self._connection() as connection:
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM knowledge_dense_chunks WHERE fingerprint=?",
                    (str(self.provider.fingerprint),),
                ).fetchone()[0]
            )
        provider = embedding_provider_info(self.provider)
        degraded = isinstance(self.provider, HashingEmbeddingProvider) or bool(self.fallback_reason)
        result = {
            "available": True,
            "degraded": degraded,
            "kind": "sqlite-exact-vector-scan",
            "backend": "sqlite-exact",
            "ann": False,
            "scalable": False,
            "fingerprint": str(self.provider.fingerprint),
            "provider": provider,
            "batchSize": self.batch_size,
            "vectorCount": count,
        }
        if isinstance(self.provider, HashingEmbeddingProvider):
            result["reason"] = "local-hash is a lexical baseline, not a semantic embedding model"
        if self.fallback_reason:
            result.update(
                {
                    "fallbackFrom": self.fallback_from,
                    "reason": self.fallback_reason,
                }
            )
        return result


class USearchDenseIndex(SqliteDenseIndex):
    """Optional persistent USearch HNSW projection.

    Vector metadata remains canonical in SQLite. HNSW files are rebuildable
    projections sharded by base so scoped searches do not need a global scan.
    """

    def __init__(
        self,
        database_path: Path,
        provider: EmbeddingProvider,
        *,
        batch_size: int = 32,
        index_factory: Callable[..., Any] | None = None,
        array_factory: Callable[[Sequence[Any], str], Any] | None = None,
    ):
        self._index_factory = index_factory
        self._array_factory = array_factory
        self._ann_lock = threading.RLock()
        self.index_root = Path(database_path).parent / "ann"
        self.index_root.mkdir(parents=True, exist_ok=True)
        super().__init__(database_path, provider, batch_size=batch_size)
        self._migrate_ann()
        self._startup_rebuilt = 0
        self._startup_error = ""
        try:
            self._dependencies()
            self._synchronize_projection()
        except RuntimeError as exc:
            self._startup_error = str(exc)

    def _migrate_ann(self) -> None:
        with self._connection() as connection:
            columns = connection.execute(
                "PRAGMA table_info(knowledge_ann_keys)"
            ).fetchall()
            unique_columns = [
                [
                    str(row["name"])
                    for row in connection.execute(
                        f"PRAGMA index_info('{str(index['name'])}')"
                    ).fetchall()
                ]
                for index in connection.execute(
                    "PRAGMA index_list(knowledge_ann_keys)"
                ).fetchall()
                if int(index["unique"]) == 1
            ]
            if columns and ["chunk_id", "fingerprint"] not in unique_columns:
                connection.execute("DROP INDEX IF EXISTS idx_knowledge_ann_base")
                connection.execute(
                    "ALTER TABLE knowledge_ann_keys "
                    "RENAME TO knowledge_ann_keys_legacy_v1"
                )
                self._create_ann_projection_table(connection)
                connection.execute(
                    "INSERT OR REPLACE INTO knowledge_ann_keys"
                    "(ann_key, chunk_id, document_id, base_id, fingerprint) "
                    "SELECT ann_key, chunk_id, document_id, base_id, fingerprint "
                    "FROM knowledge_ann_keys_legacy_v1"
                )
                connection.execute("DROP TABLE knowledge_ann_keys_legacy_v1")
            else:
                self._create_ann_projection_table(connection)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_knowledge_ann_base "
                "ON knowledge_ann_keys(base_id, fingerprint)"
            )

    @staticmethod
    def _create_ann_projection_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS knowledge_ann_keys ("
            "ann_key INTEGER PRIMARY KEY AUTOINCREMENT, chunk_id TEXT NOT NULL, "
            "document_id TEXT NOT NULL, base_id TEXT NOT NULL, fingerprint TEXT NOT NULL, "
            "UNIQUE(chunk_id, fingerprint))"
        )

    def _dependencies(self) -> tuple[Callable[..., Any], Callable[[Sequence[Any], str], Any]]:
        if self._index_factory is not None and self._array_factory is not None:
            return self._index_factory, self._array_factory
        try:
            import numpy as np
            from usearch.index import Index
        except ImportError as exc:
            raise RuntimeError(
                "USearch ANN requires the 'knowledge-ann' optional dependency"
            ) from exc
        return Index, lambda values, dtype: np.asarray(values, dtype=dtype)

    def replace_document(self, document_id: str, chunks: Sequence[dict[str, Any]]) -> None:
        with self._ann_lock:
            old_bases = self._document_bases(document_id)
            super().replace_document(document_id, chunks)
            with self._connection() as connection:
                connection.execute(
                    "DELETE FROM knowledge_ann_keys WHERE document_id=? AND fingerprint=?",
                    (document_id, str(self.provider.fingerprint)),
                )
                connection.execute(
                    "INSERT INTO knowledge_ann_keys(chunk_id, document_id, base_id, fingerprint) "
                    "SELECT chunk_id, document_id, base_id, fingerprint FROM knowledge_dense_chunks "
                    "WHERE document_id=? AND fingerprint=?",
                    (document_id, str(self.provider.fingerprint)),
                )
            new_bases = {str(chunk["base_id"]) for chunk in chunks}
            for base_id in sorted(old_bases | new_bases):
                self.rebuild_base(base_id)

    def delete_document(self, document_id: str) -> None:
        with self._ann_lock:
            bases = self._document_bases(document_id)
            super().delete_document(document_id)
            with self._connection() as connection:
                connection.execute("DELETE FROM knowledge_ann_keys WHERE document_id=?", (document_id,))
            for base_id in sorted(bases):
                self.rebuild_base(base_id)

    def rebuild(self) -> None:
        with self._connection() as connection:
            base_ids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT base_id FROM knowledge_dense_chunks WHERE fingerprint=?",
                    (str(self.provider.fingerprint),),
                ).fetchall()
            ]
        for base_id in base_ids:
            self.rebuild_base(base_id)

    def _synchronize_projection(self) -> None:
        fingerprint = str(self.provider.fingerprint)
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM knowledge_ann_keys WHERE NOT EXISTS ("
                "SELECT 1 FROM knowledge_dense_chunks d WHERE d.chunk_id=knowledge_ann_keys.chunk_id "
                "AND d.fingerprint=knowledge_ann_keys.fingerprint)"
            )
            connection.execute(
                "INSERT OR IGNORE INTO knowledge_ann_keys(chunk_id, document_id, base_id, fingerprint) "
                "SELECT chunk_id, document_id, base_id, fingerprint FROM knowledge_dense_chunks WHERE fingerprint=?",
                (fingerprint,),
            )
            base_ids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT base_id FROM knowledge_dense_chunks WHERE fingerprint=? ORDER BY base_id",
                    (fingerprint,),
                ).fetchall()
            ]
        for base_id in base_ids:
            expected = self._base_vector_count(base_id)
            actual = self._index_file_count(base_id)
            if actual != expected:
                self.rebuild_base(base_id)
                self._startup_rebuilt += 1

    def rebuild_base(self, base_id: str) -> None:
        with self._ann_lock:
            rows = self._ann_rows(base_id)
            path = self._index_path(base_id)
            if not rows:
                path.unlink(missing_ok=True)
                return
            vectors = [json.loads(str(row["vector_json"])) for row in rows]
            dimensions = len(vectors[0])
            if dimensions <= 0 or any(len(vector) != dimensions for vector in vectors):
                raise RuntimeError("dense vectors have inconsistent dimensions")
            index_factory, array_factory = self._dependencies()
            index = index_factory(ndim=dimensions, metric="cos", dtype="f32")
            index.add(
                array_factory([int(row["ann_key"]) for row in rows], "uint64"),
                array_factory(vectors, "float32"),
            )
            temporary = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
            index.save(temporary)
            os.replace(temporary, path)

    def search(
        self,
        query: str,
        *,
        base_ids: Sequence[str],
        limit: int,
        document_ids: Sequence[str] = (),
    ) -> Sequence[tuple[str, float]]:
        if document_ids:
            # A file-scoped search is already a narrow projection. Keep it exact
            # rather than silently dropping filtered HNSW candidates.
            return super().search(
                query,
                base_ids=base_ids,
                limit=limit,
                document_ids=document_ids,
            )
        vector = embed_query(self.provider, query)
        if not vector:
            return ()
        selected_bases = list(base_ids) or self._indexed_bases()
        index_factory, array_factory = self._dependencies()
        scored: list[tuple[str, float]] = []
        with self._ann_lock:
            for base_id in selected_bases:
                path = self._index_path(base_id)
                if not path.is_file():
                    self.rebuild_base(base_id)
                if not path.is_file():
                    continue
                count = self._base_vector_count(base_id)
                if count <= 0:
                    continue
                index = index_factory(ndim=len(vector), metric="cos", dtype="f32")
                index.load(path)
                matches = index.search(
                    array_factory(vector, "float32"),
                    min(count, max(1, min(100, int(limit)))),
                )
                keys = [int(value) for value in matches.keys]
                distances = [float(value) for value in matches.distances]
                mapping = self._chunk_ids_for_keys(keys)
                scored.extend(
                    (mapping[key], max(-1.0, min(1.0, 1.0 - distance)))
                    for key, distance in zip(keys, distances)
                    if key in mapping
                )
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[: max(1, min(100, int(limit)))]

    def status(self) -> dict[str, Any]:
        base = super().status()
        try:
            self._dependencies()
            dependency_available = True
            dependency_error = ""
        except RuntimeError as exc:
            dependency_available = False
            dependency_error = str(exc)
        with self._connection() as connection:
            index_count = int(
                connection.execute(
                    "SELECT COUNT(DISTINCT base_id) FROM knowledge_ann_keys WHERE fingerprint=?",
                    (str(self.provider.fingerprint),),
                ).fetchone()[0]
            )
            mapped_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM knowledge_ann_keys WHERE fingerprint=?",
                    (str(self.provider.fingerprint),),
                ).fetchone()[0]
            )
        vector_count = int(base.get("vectorCount") or 0)
        stale_count = 0
        for base_id in self._indexed_bases():
            if self._index_file_count(base_id) != self._base_vector_count(base_id):
                stale_count += 1
        base.update(
            {
                "available": dependency_available,
                "degraded": not dependency_available or isinstance(self.provider, HashingEmbeddingProvider),
                "kind": "usearch-hnsw",
                "backend": "usearch",
                "ann": True,
                "scalable": True,
                "indexCount": index_count,
                "mappedVectorCount": mapped_count,
                "projectionConsistent": mapped_count == vector_count and stale_count == 0,
                "staleIndexCount": stale_count,
                "startupRebuiltIndexCount": self._startup_rebuilt,
            }
        )
        if dependency_error or self._startup_error:
            base["reason"] = dependency_error or self._startup_error
            base["degraded"] = True
        return base

    def _ann_rows(self, base_id: str) -> list[sqlite3.Row]:
        with self._connection() as connection:
            return list(
                connection.execute(
                    "SELECT a.ann_key, d.vector_json FROM knowledge_ann_keys a "
                    "JOIN knowledge_dense_chunks d ON d.chunk_id=a.chunk_id "
                    "WHERE a.base_id=? AND a.fingerprint=? AND d.fingerprint=? ORDER BY a.ann_key",
                    (base_id, str(self.provider.fingerprint), str(self.provider.fingerprint)),
                ).fetchall()
            )

    def _document_bases(self, document_id: str) -> set[str]:
        with self._connection() as connection:
            return {
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT base_id FROM knowledge_ann_keys WHERE document_id=?",
                    (document_id,),
                ).fetchall()
            }

    def _indexed_bases(self) -> list[str]:
        with self._connection() as connection:
            return [
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT base_id FROM knowledge_ann_keys WHERE fingerprint=? ORDER BY base_id",
                    (str(self.provider.fingerprint),),
                ).fetchall()
            ]

    def _base_vector_count(self, base_id: str) -> int:
        with self._connection() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM knowledge_ann_keys WHERE base_id=? AND fingerprint=?",
                    (base_id, str(self.provider.fingerprint)),
                ).fetchone()[0]
            )

    def _index_file_count(self, base_id: str) -> int:
        path = self._index_path(base_id)
        if not path.is_file():
            return 0
        rows = self._ann_rows(base_id)
        if not rows:
            return 0
        try:
            index_factory, _array_factory = self._dependencies()
            vector = json.loads(str(rows[0]["vector_json"]))
            index = index_factory(ndim=len(vector), metric="cos", dtype="f32")
            index.load(path)
            return int(getattr(index, "size", len(index)))
        except Exception:
            return -1

    def _chunk_ids_for_keys(self, keys: Sequence[int]) -> dict[int, str]:
        if not keys:
            return {}
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT ann_key, chunk_id FROM knowledge_ann_keys WHERE ann_key IN "
                f"({', '.join('?' for _ in keys)})",
                list(keys),
            ).fetchall()
        return {int(row["ann_key"]): str(row["chunk_id"]) for row in rows}

    def _index_path(self, base_id: str) -> Path:
        fingerprint_hash = hashlib.sha256(str(self.provider.fingerprint).encode()).hexdigest()[:16]
        safe_base = hashlib.sha256(base_id.encode()).hexdigest()[:24]
        directory = self.index_root / fingerprint_hash
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{safe_base}.usearch"


def dense_index_from_env(
    database_path: Path,
    provider: EmbeddingProvider,
    env: dict[str, str] | None = None,
) -> DenseIndex:
    source = os.environ if env is None else env
    if isinstance(provider, NullEmbeddingProvider):
        return NullDenseIndex()
    try:
        batch_size = max(1, min(128, int(source.get("RAG_IME_EMBEDDING_BATCH_SIZE", "32"))))
    except ValueError:
        batch_size = 32
    backend = source.get("RAG_IME_KNOWLEDGE_DENSE_BACKEND", "usearch").strip().lower()
    if backend in {"usearch", "hnsw", "usearch-hnsw"}:
        try:
            index = USearchDenseIndex(database_path, provider, batch_size=batch_size)
            index._dependencies()
            return index
        except RuntimeError as exc:
            return SqliteDenseIndex(
                database_path,
                provider,
                batch_size=batch_size,
                fallback_from="usearch",
                fallback_reason=str(exc),
            )
    return SqliteDenseIndex(database_path, provider, batch_size=batch_size)


def reciprocal_rank_fusion(
    lexical_ids: Sequence[str],
    dense_ids: Sequence[str],
    *,
    rank_constant: int = 60,
    lexical_weight: float = 1.2,
    dense_weight: float = 1.0,
) -> list[str]:
    scores: dict[str, float] = {}
    for ranking, weight in ((lexical_ids, lexical_weight), (dense_ids, dense_weight)):
        for rank, item_id in enumerate(ranking, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + max(0.0, float(weight)) / (max(1, rank_constant) + rank)
    return sorted(scores, key=lambda item_id: (-scores[item_id], item_id))
