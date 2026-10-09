from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from .agent_blocks import bind_block_scope, provider_block_projection
from .db import apply_database_migrations


MAX_RECENT_MESSAGE_HYDRATION = 96
MAX_ROOT_BLOCK_BYTES = 8 * 1024 * 1024


class AgentBlockConflict(RuntimeError):
    """A stable block identity was rebound to different content."""


class AgentBlockStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def initialize(self) -> int:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            return apply_database_migrations(conn).current_version

    def persist_message(
        self,
        message: Mapping[str, object],
        *,
        root_id: str = "",
        task_id: str = "",
        invocation_id: str = "",
        generation: int = 0,
        created_at_ms: int | None = None,
        native_message_id: str = "",
        native_pi_session_id: str = "",
    ) -> dict[str, object]:
        session_id = _required(message.get("sessionId"), "sessionId")
        message_id = _required(message.get("id"), "message id")
        turn_id = str(message.get("turnId") or "")[:240]
        root = str(root_id).strip() or f"session:{session_id}"
        now = int(created_at_ms if created_at_ms is not None else time.time() * 1000)
        blocks = bind_block_scope(
            [dict(item) for item in message.get("blocks", []) if isinstance(item, Mapping) and item.get("schemaVersion") == "rag-ime.agent-block.v1"],
            session_id=session_id,
            message_id=message_id,
            generation=generation,
        )
        projection = provider_block_projection("", blocks)
        base_message = dict(message)
        base_message["blocks"] = [
            dict(item) for item in message.get("blocks", [])
            if isinstance(item, Mapping)
            and item.get("schemaVersion") != "rag-ime.agent-block.v1"
        ]
        envelope_material = {
            "message": base_message,
            "blockRefs": [str(block["ref"]) for block in blocks],
        }
        message_hash = hashlib.sha256(
            _canonical_json(envelope_material).encode("utf-8")
        ).hexdigest()
        before_bytes = sum(len(_canonical_json(block).encode("utf-8")) for block in blocks)
        after_bytes = len(projection.encode("utf-8"))
        with self._connect(immediate=True) as conn:
            existing_envelope = conn.execute(
                "SELECT message_hash FROM agent_block_message_envelopes WHERE session_id=? AND message_id=? AND generation=?",
                (session_id, message_id, max(0, int(generation))),
            ).fetchone()
            if existing_envelope is not None and str(existing_envelope[0]) != message_hash:
                raise AgentBlockConflict("Agent block message envelope changed after persistence")
            if existing_envelope is not None:
                self._bind_native_alias(conn, session_id, native_pi_session_id, native_message_id, message_id, generation)
                persisted_receipt = conn.execute(
                    "SELECT before_bytes,after_bytes,estimated_tokens_before,estimated_tokens_after,block_count,projection_hash FROM agent_block_projection_receipts WHERE session_id=? AND message_id=? AND generation=?",
                    (session_id, message_id, max(0, int(generation))),
                ).fetchone()
                if persisted_receipt is None:
                    raise RuntimeError("Agent block envelope exists without projection receipt")
                return _persistence_receipt(
                    persisted_receipt,
                    session_id=session_id,
                    message_id=message_id,
                    root_id=root,
                    generation=generation,
                )
            used = int(conn.execute(
                "SELECT COALESCE(SUM(raw_bytes),0) FROM agent_message_block_sidecars WHERE root_id=? AND generation=? AND lifecycle_status IN ('active','completed')",
                (root, max(0, int(generation))),
            ).fetchone()[0])
            if used + before_bytes > MAX_ROOT_BLOCK_BYTES:
                raise ValueError("Agent block Root byte budget exceeded")
            for block in blocks:
                self._persist_block(
                    conn, block, message_id=message_id, session_id=session_id,
                    turn_id=turn_id, root_id=root, task_id=task_id,
                    invocation_id=invocation_id, generation=generation,
                    created_at_ms=now,
                )
            conn.execute(
                """
                INSERT INTO agent_block_message_envelopes(
                    session_id,message_id,generation,root_id,message_hash,message_json,created_at_ms
                ) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(session_id,message_id,generation) DO NOTHING
                """,
                (
                    session_id, message_id, max(0, int(generation)), root,
                    message_hash, _canonical_json(base_message), now,
                ),
            )
            self._bind_native_alias(conn, session_id, native_pi_session_id, native_message_id, message_id, generation)
            receipt_id = f"block-projection:{uuid.uuid4()}"
            projection_hash = hashlib.sha256(projection.encode("utf-8")).hexdigest()
            conn.execute(
                """
                INSERT INTO agent_block_projection_receipts(
                    receipt_id,session_id,message_id,root_id,generation,
                    before_bytes,after_bytes,estimated_tokens_before,
                    estimated_tokens_after,block_count,projection_hash,created_at_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(session_id,message_id,generation) DO NOTHING
                """,
                (
                    receipt_id, session_id, message_id, root, max(0, int(generation)),
                    before_bytes, after_bytes, (before_bytes + 3) // 4,
                    (after_bytes + 3) // 4, len(blocks), projection_hash, now,
                ),
            )
            persisted_receipt = conn.execute(
                "SELECT before_bytes,after_bytes,estimated_tokens_before,estimated_tokens_after,block_count,projection_hash FROM agent_block_projection_receipts WHERE session_id=? AND message_id=? AND generation=?",
                (session_id, message_id, max(0, int(generation))),
            ).fetchone()
            if persisted_receipt is None:
                raise RuntimeError("Agent block projection receipt was not persisted")
            before_bytes, after_bytes, tokens_before, tokens_after, block_count, projection_hash = persisted_receipt
        return _persistence_receipt(
            persisted_receipt,
            session_id=session_id,
            message_id=message_id,
            root_id=root,
            generation=generation,
        )

    def blocks_for_message(self, session_id: str, message_id: str, *, generation: int | None = None) -> list[dict[str, object]]:
        clauses = ["session_id=?", "message_id=?", "lifecycle_status IN ('active','completed')"]
        params: list[object] = [session_id, message_id]
        if generation is not None:
            clauses.append("generation=?")
            params.append(max(0, int(generation)))
        else:
            clauses.append("generation=(SELECT MAX(generation) FROM agent_message_block_sidecars WHERE session_id=? AND message_id=? AND lifecycle_status IN ('active','completed'))")
            params.extend([session_id, message_id])
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT raw_json FROM agent_message_block_sidecars WHERE {' AND '.join(clauses)} ORDER BY created_at_ms,block_ref",
                params,
            ).fetchall()
        return [json.loads(str(row[0])) for row in rows]

    def _bind_native_alias(
        self, conn: sqlite3.Connection, session_id: str, native_pi_session_id: str, native_message_id: str,
        source_message_id: str, generation: int,
    ) -> None:
        if not native_message_id:
            return
        native_id = _required(native_message_id, "native message id")
        target = (source_message_id, max(0, int(generation)))
        existing = conn.execute(
            "SELECT source_message_id,generation FROM agent_block_native_aliases WHERE session_id=? AND native_pi_session_id=? AND native_message_id=?",
            (session_id, native_pi_session_id, native_id),
        ).fetchone()
        if existing is not None and tuple(existing) != target:
            raise AgentBlockConflict("Agent block native alias was rebound")
        claimed = conn.execute(
            "SELECT native_message_id FROM agent_block_native_aliases WHERE session_id=? AND native_pi_session_id=? AND source_message_id=? AND generation=?",
            (session_id, native_pi_session_id, *target),
        ).fetchone()
        if claimed is not None and str(claimed[0]) != native_id:
            raise AgentBlockConflict("Agent block source already has a different native alias")
        conn.execute(
            "INSERT OR IGNORE INTO agent_block_native_aliases(session_id,native_pi_session_id,native_message_id,source_message_id,generation) VALUES (?,?,?,?,?)",
            (session_id, native_pi_session_id, native_id, *target),
        )

    def hydrate_recent_messages(
        self, session_id: str, runtime_messages: Sequence[Mapping[str, object]],
        *, native_pi_session_id: str = "",
    ) -> list[dict[str, object]]:
        """Hydrate only returned IDs; never restore history or infer native aliases."""
        messages = [dict(message) for message in runtime_messages]
        # An unexpected oversized projection stays truthful/plain, without an
        # unbounded query or silently dropping messages from Pi's projection.
        if len(messages) > MAX_RECENT_MESSAGE_HYDRATION:
            return messages
        ids = list(dict.fromkeys(
            message["id"] for message in messages
            if message.get("sessionId") == session_id
            and isinstance(message.get("id"), str) and message["id"]
        ))
        if not ids:
            return messages
        values = ",".join("(?)" for _ in ids)
        with self._connect() as conn:
            bindings = conn.execute(
                f"""
                WITH requested(id) AS (VALUES {values})
                SELECT requested.id, COALESCE(alias.source_message_id,envelope.message_id) AS source_id,
                       COALESCE(alias.generation,envelope.generation) AS generation
                FROM requested
                LEFT JOIN agent_block_native_aliases AS alias
                  ON alias.session_id=? AND alias.native_pi_session_id=? AND alias.native_message_id=requested.id
                LEFT JOIN agent_block_message_envelopes AS envelope
                  ON ?='' AND envelope.session_id=? AND envelope.message_id=requested.id
                 AND envelope.generation=(SELECT MAX(generation) FROM agent_block_message_envelopes
                     WHERE session_id=? AND message_id=requested.id)
                """, (*ids, session_id, native_pi_session_id, native_pi_session_id, session_id, session_id),
            ).fetchall()
            targets = list(dict.fromkeys(
                (str(row["source_id"]), int(row["generation"]))
                for row in bindings if row["source_id"] is not None
            ))
            if not targets:
                return messages
            pair_values = ",".join("(?,?)" for _ in targets)
            rows = conn.execute(
                f"""
                WITH requested(message_id,generation) AS (VALUES {pair_values})
                SELECT sidecar.message_id,sidecar.generation,sidecar.raw_json
                FROM requested JOIN agent_message_block_sidecars AS sidecar
                  ON sidecar.session_id=? AND sidecar.message_id=requested.message_id
                 AND sidecar.generation=requested.generation
                WHERE sidecar.lifecycle_status IN ('active','completed')
                ORDER BY sidecar.created_at_ms,sidecar.block_ref
                """, (*[value for target in targets for value in target], session_id),
            ).fetchall()
        blocks_by_target: dict[tuple[str, int], list[dict[str, object]]] = {}
        for row in rows:
            blocks_by_target.setdefault((str(row["message_id"]), int(row["generation"])), []).append(json.loads(str(row["raw_json"])))
        target_by_id = {str(row["id"]): (str(row["source_id"]), int(row["generation"])) for row in bindings if row["source_id"] is not None}
        for message in messages:
            if message.get("sessionId") != session_id:
                continue
            target = target_by_id.get(str(message.get("id") or ""))
            stored = blocks_by_target.get(target, []) if target is not None else []
            if target is None:
                continue
            # Once a persisted envelope owns this entry, active sidecars own
            # its rich blocks, including revocation. Keep ordinary Runtime text.
            original = [dict(block) for block in message.get("blocks", []) if isinstance(block, Mapping) and block.get("schemaVersion") != "rag-ime.agent-block.v1"]
            message["blocks"] = [*original, *stored]
        return messages

    def hydrate_messages(
        self, session_id: str, runtime_messages: Sequence[Mapping[str, object]],
        *, native_pi_session_id: str | None = "",
    ) -> list[dict[str, object]]:
        """Hydrate runtime history and recover rich messages omitted after compaction/restart."""

        runtime_order: list[str] = []
        runtime_by_id: dict[str, dict[str, object]] = {}
        for message in runtime_messages:
            message_id = str(message.get("id") or "")
            if not message_id or message_id in runtime_by_id:
                continue
            runtime_order.append(message_id)
            runtime_by_id[message_id] = dict(message)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT envelope.*, alias.native_message_id, alias.native_pi_session_id FROM agent_block_message_envelopes AS envelope
                JOIN (
                    SELECT message_id, MAX(generation) AS generation
                    FROM agent_block_message_envelopes WHERE session_id=? GROUP BY message_id
                ) AS latest
                  ON latest.message_id=envelope.message_id
                 AND (latest.generation=envelope.generation OR EXISTS (
                     SELECT 1 FROM agent_block_native_aliases AS exact_alias
                     WHERE exact_alias.session_id=envelope.session_id
                       AND exact_alias.source_message_id=envelope.message_id
                       AND exact_alias.generation=envelope.generation
                       AND exact_alias.native_pi_session_id=?
                 ))
                LEFT JOIN agent_block_native_aliases AS alias
                  ON alias.session_id=envelope.session_id AND alias.source_message_id=envelope.message_id
                 AND alias.generation=envelope.generation
                WHERE envelope.session_id=? ORDER BY envelope.created_at_ms,envelope.message_id
                """,
                (session_id, native_pi_session_id, session_id),
            ).fetchall()
        missing_envelopes: list[tuple[int, str, dict[str, object]]] = []
        for row in rows:
            message_id = str(row["message_id"])
            native_id = str(row["native_message_id"] or "")
            native_scope = str(row["native_pi_session_id"] or "")
            # A historic native namespace retains its own persisted source ID.
            # Only the snapshot's proven Pi scope may use that native entry ID.
            current_native = native_id and native_pi_session_id is not None and native_scope == native_pi_session_id
            target_message_id = native_id if current_native else message_id
            base = runtime_by_id.get(target_message_id)
            if base is None:
                value = json.loads(str(row["message_json"]))
                if not isinstance(value, dict):
                    raise RuntimeError("Agent block message envelope is corrupt")
                base = {**value, "id": target_message_id}
            base_blocks = [
                dict(item) for item in base.get("blocks", [])
                if isinstance(item, Mapping)
                and item.get("schemaVersion") != "rag-ime.agent-block.v1"
            ]
            base["blocks"] = [
                *base_blocks,
                *self.blocks_for_message(
                    session_id, message_id, generation=int(row["generation"])
                ),
            ]
            runtime_by_id[target_message_id] = base
            if target_message_id not in runtime_order:
                missing_envelopes.append((int(row["created_at_ms"]), message_id, base))

        # Pi's runtime snapshot owns message/parent order. Timestamps can drift
        # across processes, so they are used only to place persisted envelopes
        # that the runtime omitted after compaction or restart.
        hydrated = [runtime_by_id[message_id] for message_id in runtime_order]
        for persisted_at_ms, _message_id, envelope in missing_envelopes:
            insert_at = len(hydrated)
            for index, runtime_message in enumerate(hydrated):
                runtime_id = str(runtime_message.get("id") or "")
                if runtime_id not in runtime_order:
                    continue
                runtime_created_at_ms = max(
                    0, int(runtime_message.get("createdAtMs") or 0)
                )
                if persisted_at_ms <= runtime_created_at_ms:
                    insert_at = index
                    break
            hydrated.insert(insert_at, envelope)
        return hydrated

    def cancel_generation(self, root_id: str, generation: int, *, now_ms: int | None = None) -> int:
        with self._connect(immediate=True) as conn:
            cursor = conn.execute(
                "UPDATE agent_message_block_sidecars SET lifecycle_status='cancelled',updated_at_ms=? WHERE root_id=? AND generation<=? AND lifecycle_status='active'",
                (int(now_ms if now_ms is not None else time.time() * 1000), root_id, max(0, int(generation))),
            )
            return max(0, int(cursor.rowcount))

    def revoke(self, block_ref: str, *, root_id: str, session_id: str, now_ms: int | None = None) -> bool:
        with self._connect(immediate=True) as conn:
            cursor = conn.execute(
                "UPDATE agent_message_block_sidecars SET lifecycle_status='revoked',updated_at_ms=? WHERE block_ref=? AND root_id=? AND session_id=? AND lifecycle_status IN ('active','completed')",
                (int(now_ms if now_ms is not None else time.time() * 1000), block_ref, root_id, session_id),
            )
            return cursor.rowcount == 1

    def _persist_block(self, conn: sqlite3.Connection, block: Mapping[str, object], **scope: object) -> None:
        raw = _canonical_json(block)
        digest = _required(block.get("digest"), "digest")
        block_ref = _required(block.get("ref"), "block ref")
        identity = conn.execute(
            "SELECT block_ref,digest,raw_json FROM agent_message_block_sidecars WHERE session_id=? AND message_id=? AND block_id=? AND generation=?",
            (scope["session_id"], scope["message_id"], _required(block.get("id"), "block id"), max(0, int(scope["generation"]))),
        ).fetchone()
        if identity is not None:
            if str(identity[0]) != block_ref or str(identity[1]) != digest or str(identity[2]) != raw:
                raise AgentBlockConflict("Agent block identity was rebound to different content")
            return
        existing = conn.execute("SELECT digest,raw_json FROM agent_message_block_sidecars WHERE block_ref=?", (block_ref,)).fetchone()
        if existing is not None:
            if str(existing[0]) != digest or str(existing[1]) != raw:
                raise AgentBlockConflict("Agent block ref was rebound to different content")
            return
        conn.execute(
            """
            INSERT INTO agent_message_block_sidecars(
                block_ref,block_id,message_id,session_id,turn_id,root_id,
                task_id,invocation_id,generation,block_type,visibility,
                lifecycle_status,digest,summary,raw_json,raw_bytes,
                created_at_ms,updated_at_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                block_ref, _required(block.get("id"), "block id"), scope["message_id"],
                scope["session_id"], scope["turn_id"], scope["root_id"], scope["task_id"],
                scope["invocation_id"], max(0, int(scope["generation"])),
                _required(block.get("type"), "block type"),
                str(block.get("visibility") or "private_session"), "completed", digest,
                str(block.get("summary") or "")[:240], raw, len(raw.encode("utf-8")),
                scope["created_at_ms"], scope["created_at_ms"],
            ),
        )

    @contextmanager
    def _connect(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            if immediate:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _persistence_receipt(
    row: Sequence[object],
    *,
    session_id: str,
    message_id: str,
    root_id: str,
    generation: int,
) -> dict[str, object]:
    before_bytes, after_bytes, tokens_before, tokens_after, block_count, projection_hash = row
    return {
        "schemaVersion": "rag-ime.agent-block-persistence-receipt.v1",
        "sessionId": session_id,
        "messageId": message_id,
        "rootId": root_id,
        "generation": max(0, int(generation)),
        "blockCount": int(block_count),
        "beforeBytes": int(before_bytes),
        "afterBytes": int(after_bytes),
        "estimatedTokensBefore": int(tokens_before),
        "estimatedTokensAfter": int(tokens_after),
        "projectionHash": str(projection_hash),
    }


def _required(value: object, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{label} is required")
    return result
