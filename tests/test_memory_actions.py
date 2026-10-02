from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from rag_ime.debug_server import DebugImeService, DebugRequestHandler, DebugServerConfig
from rag_ime.local_sqlite_core import LocalSqliteCoreClient
from rag_ime.management_service import page_request
from rag_ime.memory_actions import execute_memory_action, mutate_memory_action
from rag_ime.models import InputEvent


class MemoryActionMutationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-memory-actions-")
        self.db_path = Path(self.tmp.name) / "rag-ime.sqlite"
        self.core = LocalSqliteCoreClient(self.db_path)
        self.core.initialize()

    def tearDown(self) -> None:
        self.core.close()
        self.tmp.cleanup()

    def test_pin_updates_metadata_quality_weight_status_and_real_ranking(self) -> None:
        target_id = self._record_phrase("连续优化", created_at_ms=100)
        other_id = self._record_phrase("连续测试", created_at_ms=200)

        before = self._query_texts("连续")
        self.assertIn("连续优化", before)
        self.assertIn("连续测试", before)
        self.assertLess(before.index("连续测试"), before.index("连续优化"))

        invalidations: list[str] = []
        events: list[tuple[str, dict[str, object]]] = []
        result = execute_memory_action(
            connection_factory=self.core._connect,  # type: ignore[attr-defined]
            payload={"memoryId": target_id, "action": "pin", "reason": "keep this phrase"},
            cache_invalidator=lambda: invalidations.append("cleared"),
            event_publisher=lambda name, payload: events.append((name, dict(payload))),
        )

        with self.core._connect() as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                "SELECT id, status, confidence, quality_score, metadata_json FROM memory_items WHERE memory_id = ?",
                (target_id,),
            ).fetchone()
            tag_weights = [
                float(item[0])
                for item in conn.execute(
                    "SELECT weight FROM memory_item_tags WHERE memory_item_id = ?",
                    (int(row[0]),),
                ).fetchall()
            ]
            accepted = int(
                conn.execute(
                    "SELECT COUNT(*) FROM candidate_feedback WHERE memory_id = ? AND action = 'accepted'",
                    (target_id,),
                ).fetchone()[0]
            )
        metadata = json.loads(row[4])
        after = self._query_texts("连续")

        self.assertEqual(row[1], "approved")
        self.assertGreaterEqual(float(row[2]), 0.90)
        self.assertGreaterEqual(float(row[3]), 0.92)
        self.assertTrue(metadata["pinned"])
        self.assertEqual(metadata["rankWeight"], 1.25)
        self.assertTrue(tag_weights)
        self.assertTrue(all(weight >= 1.25 for weight in tag_weights))
        self.assertEqual(accepted, 1)
        self.assertEqual(after[0], "连续优化")
        self.assertNotEqual(target_id, other_id)
        self.assertEqual(invalidations, ["cleared"])
        self.assertEqual(events[0][0], "memory_changed")
        self.assertEqual(events[0][1]["memoryId"], target_id)
        self.assertTrue(result["cacheInvalidation"]["performed"])
        self.assertTrue(result["event"]["published"])

    def test_disable_removes_real_memory_id_from_docs_and_query(self) -> None:
        memory_id = self._record_phrase("灵动候选", created_at_ms=100)
        self.assertIn("灵动候选", self._query_texts("灵动"))

        with self.core._connect() as conn:  # type: ignore[attr-defined]
            result = mutate_memory_action(
                conn,
                {"memoryId": memory_id, "action": "disable"},
                changed_at_ms=300,
            )
            row = conn.execute(
                "SELECT status FROM memory_items WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
            doc_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM memory_retrieval_docs WHERE source_id = ?",
                    (memory_id,),
                ).fetchone()[0]
            )

        self.assertEqual(row[0], "disabled")
        self.assertEqual(doc_count, 0)
        self.assertNotIn("灵动候选", self._query_texts("灵动"))
        self.assertTrue(result["retrievalDocs"]["rebuilt"])

    def test_suppress_uses_retriever_memory_id_match_and_blocks_query(self) -> None:
        memory_id = self._record_phrase("上下文注入", created_at_ms=100)
        self.assertIn("上下文注入", self._query_texts("上下文"))

        with self.core._connect() as conn:  # type: ignore[attr-defined]
            result = mutate_memory_action(
                conn,
                {"memoryId": memory_id, "action": "suppress"},
                changed_at_ms=300,
            )
            suppression = conn.execute(
                "SELECT match_type, match_value, action FROM memory_candidate_suppressions WHERE match_value = ?",
                (memory_id,),
            ).fetchone()
            status = conn.execute(
                "SELECT status FROM memory_items WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()[0]
            doc_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM memory_retrieval_docs WHERE source_id = ?",
                    (memory_id,),
                ).fetchone()[0]
            )

        self.assertEqual(tuple(suppression), ("memory_id", memory_id, "block"))
        self.assertEqual(status, "active")
        self.assertEqual(doc_count, 1)
        self.assertEqual(result["changes"]["matchType"], "memory_id")
        self.assertNotIn("上下文注入", self._query_texts("上下文"))

    def test_tombstone_targets_memory_id_and_removes_retrieval_doc(self) -> None:
        memory_id = self._record_phrase("错误噪声", created_at_ms=100)

        with self.core._connect() as conn:  # type: ignore[attr-defined]
            result = mutate_memory_action(
                conn,
                {"memoryId": memory_id, "action": "tombstone"},
                changed_at_ms=300,
            )
            tombstone = conn.execute(
                "SELECT target_type, target_value, active FROM memory_tombstones WHERE target_value = ?",
                (memory_id,),
            ).fetchone()
            status = conn.execute(
                "SELECT status FROM memory_items WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()[0]
            doc_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM memory_retrieval_docs WHERE source_id = ?",
                    (memory_id,),
                ).fetchone()[0]
            )

        self.assertEqual(tuple(tombstone), ("memory_id", memory_id, 1))
        self.assertEqual(status, "tombstoned")
        self.assertEqual(doc_count, 0)
        self.assertEqual(result["changes"]["targetType"], "memory_id")
        self.assertNotIn("错误噪声", self._query_texts("错误"))

    def test_update_phrase_rebuilds_fts_invalidates_vector_and_changes_query(self) -> None:
        memory_id = self._record_phrase("旧版短语", created_at_ms=100)
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            row_id = int(
                conn.execute(
                    "SELECT id FROM memory_items WHERE memory_id = ?",
                    (memory_id,),
                ).fetchone()[0]
            )
            conn.execute(
                """
                INSERT INTO memory_item_vectors(memory_item_id, provider_fingerprint, vector_json, updated_at_ms)
                VALUES (?, 'test-vector', '[1.0,0.0]', 100)
                """,
                (row_id,),
            )

        with self.core._connect() as conn:  # type: ignore[attr-defined]
            result = mutate_memory_action(
                conn,
                {"memoryId": memory_id, "action": "update_phrase", "newPhrase": "灵动推荐"},
                changed_at_ms=300,
            )
            item = conn.execute(
                "SELECT text, normalized_text, status FROM memory_items WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
            fts = conn.execute(
                "SELECT text, normalized_text FROM memory_items_fts WHERE rowid = ?",
                (row_id,),
            ).fetchone()
            vector_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM memory_item_vectors WHERE memory_item_id = ?",
                    (row_id,),
                ).fetchone()[0]
            )
            retrieval_doc = conn.execute(
                "SELECT raw_text FROM memory_retrieval_docs WHERE source_id = ?",
                (memory_id,),
            ).fetchone()
            new_stats = conn.execute(
                "SELECT input_frequency FROM phrase_stats WHERE committed_text = '灵动推荐'",
            ).fetchone()
            old_stats = conn.execute(
                "SELECT input_frequency FROM phrase_stats WHERE committed_text = '旧版短语'",
            ).fetchone()

        self.assertEqual(tuple(item), ("灵动推荐", "灵动推荐", "active"))
        self.assertIn("灵动推荐", fts[0])
        self.assertEqual(fts[1], "灵动推荐")
        self.assertEqual(vector_count, 0)
        self.assertIn("灵动推荐", retrieval_doc[0])
        self.assertIsNotNone(new_stats)
        self.assertIsNone(old_stats)
        self.assertTrue(result["indexInvalidation"]["memoryItemsFtsRebuilt"])
        self.assertEqual(result["indexInvalidation"]["memoryItemVectorsInvalidated"], 1)
        self.assertEqual(result["changes"]["phraseStats"]["global"], 1)
        self.assertIn("灵动推荐", self._query_texts("灵动"))
        self.assertNotIn("旧版短语", self._query_texts("旧版"))

    def test_rebuild_action_calls_real_retrieval_projection(self) -> None:
        memory_id = self._record_phrase("重建文档", created_at_ms=100)
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            conn.execute("DELETE FROM memory_retrieval_docs")
            conn.execute("DELETE FROM memory_retrieval_docs_fts")

        with self.core._connect() as conn:  # type: ignore[attr-defined]
            result = mutate_memory_action(
                conn,
                {"memoryId": memory_id, "action": "rebuild_retrieval_doc"},
                changed_at_ms=300,
            )
            doc_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM memory_retrieval_docs WHERE source_id = ?",
                    (memory_id,),
                ).fetchone()[0]
            )

        self.assertEqual(doc_count, 1)
        self.assertTrue(result["retrievalDocs"]["rebuilt"])
        self.assertGreaterEqual(result["retrievalDocs"]["docCount"], 1)

    def test_update_phrase_rejects_sensitive_text_before_storage(self) -> None:
        memory_id = self._record_phrase("普通短语", created_at_ms=100)

        with self.assertRaisesRegex(ValueError, "sensitive phrases"):
            with self.core._connect() as conn:  # type: ignore[attr-defined]
                mutate_memory_action(
                    conn,
                    {"memoryId": memory_id, "action": "update_phrase", "newPhrase": "password secret-value"},
                    changed_at_ms=300,
                )

        with self.core._connect() as conn:  # type: ignore[attr-defined]
            text = conn.execute(
                "SELECT text FROM memory_items WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()[0]
        self.assertEqual(text, "普通短语")

    def test_sensitive_memory_cannot_be_pinned_or_copied_into_feedback(self) -> None:
        event_memory_id = self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=100,
                source="manual",
                committed_text="password secret-value",
                privacy_disposition="allowed",
                project="wisdom-weasel-rag-ime",
            )
        )
        raw_memory_id = f"raw:{event_memory_id}"

        with self.assertRaisesRegex(ValueError, "sensitive memory"):
            with self.core._connect() as conn:  # type: ignore[attr-defined]
                mutate_memory_action(
                    conn,
                    {"memoryId": raw_memory_id, "action": "pin"},
                    changed_at_ms=300,
                )

        with self.core._connect() as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                "SELECT status, privacy_class FROM memory_items WHERE memory_id = ?",
                (raw_memory_id,),
            ).fetchone()
            feedback_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM candidate_feedback WHERE memory_id = ?",
                    (raw_memory_id,),
                ).fetchone()[0]
            )
        self.assertEqual(tuple(row), ("hidden", "sensitive"))
        self.assertEqual(feedback_count, 0)

    def _record_phrase(self, text: str, *, created_at_ms: int) -> str:
        self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=created_at_ms,
                source="manual",
                committed_text=text,
                privacy_disposition="allowed",
                project="wisdom-weasel-rag-ime",
                tags=("phrase-memory", "连续"),
            )
        )
        self.core.process_memory_projection_outbox()
        return f"phrase:{text.lower()}"

    def _query_texts(self, query: str) -> list[str]:
        return [
            item.text
            for item in self.core.retrieve_candidates_v3(
                current_input=query,
                project="wisdom-weasel-rag-ime",
                top_k=10,
            )
        ]


class ManagementMemoryActionIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-management-memory-actions-")
        self.db_path = Path(self.tmp.name) / "rag-ime.sqlite"
        self.core = LocalSqliteCoreClient(self.db_path)
        self.core.initialize()
        self.service = DebugImeService(
            DebugServerConfig(
                db_path=self.db_path,
                core=self.core,
                seed_if_empty=False,
            )
        )

    def tearDown(self) -> None:
        self.service.close()
        self.core.close()
        self.tmp.cleanup()

    def test_phrase_page_returns_real_memory_id_instead_of_display_hash(self) -> None:
        memory_id = self._record_phrase("真实短语标识")

        response = self.service.management.memory_page(
            "phrases",
            page_request({"query": "真实短语", "limit": 10}),
        )
        item = response["items"][0]

        self.assertEqual(item["id"], memory_id)
        self.assertEqual(item["itemId"], memory_id)
        self.assertEqual(item["memoryId"], memory_id)
        self.assertEqual(item["normalizedText"], "真实短语标识")
        self.assertTrue(item["originalTextHash"].startswith("sha256:"))
        self.assertNotEqual(item["id"], item["textHash"])

    def test_http_memory_action_uses_real_mutation_and_clears_sidecar_cache(self) -> None:
        memory_id = self._record_phrase("接口停用候选")
        self.service._rime_cache["stale-entry"] = object()  # type: ignore[assignment,attr-defined]

        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = Path("debug")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(
                f"http://127.0.0.1:{server.server_port}/api/memory/action",
                data=json.dumps(
                    {"memoryId": memory_id, "itemType": "phrase", "action": "disable"},
                    ensure_ascii=False,
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        with self.core._connect() as conn:  # type: ignore[attr-defined]
            status = conn.execute(
                "SELECT status FROM memory_items WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()[0]
            audit_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM management_audit_log WHERE action = 'memory_disable' AND target_id = ?",
                    (memory_id,),
                ).fetchone()[0]
            )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["schemaVersion"], "rag-ime.management.v1")
        self.assertEqual(payload["mutationSchemaVersion"], "rag-ime.memory-action-mutation.v1")
        self.assertEqual(payload["memoryId"], memory_id)
        self.assertEqual(payload["status"], "disabled")
        self.assertTrue(payload["cacheInvalidation"]["performed"])
        self.assertTrue(payload["event"]["published"])
        self.assertEqual(status, "disabled")
        self.assertEqual(audit_count, 1)
        self.assertEqual(self.service._rime_cache, {})  # type: ignore[attr-defined]
        self.assertNotIn("接口停用候选", self._query_texts("接口停用"))

    def _record_phrase(self, text: str) -> str:
        self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=100,
                source="manual",
                committed_text=text,
                privacy_disposition="allowed",
                project="wisdom-weasel-rag-ime",
                tags=("phrase-memory",),
            )
        )
        return f"phrase:{text.lower()}"

    def _query_texts(self, query: str) -> list[str]:
        return [
            item.text
            for item in self.core.retrieve_candidates_v3(
                current_input=query,
                project="wisdom-weasel-rag-ime",
                top_k=10,
            )
        ]


if __name__ == "__main__":
    unittest.main()
