from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from rag_ime.agent_memory_sources import AgentMemorySourceStore
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.hybrid_rag_models import HybridRagQuery
from rag_ime.hybrid_rag_retriever import (
    _semantic_query_vector,
    retrieve_hybrid_rag_candidates,
)
from rag_ime.local_sqlite_core import LocalSqliteCoreClient
from rag_ime.memory_book_compiler import apply_memory_book_plan, memory_book_plan_from_compile_output
from rag_ime.memory_ownership import agent_visible_memory_owners
from rag_ime.models import InputEvent
from rag_ime.retrieval_docs import rebuild_retrieval_docs
from rag_ime.retrieval_vector_index import rebuild_retrieval_doc_vectors, warm_retrieval_doc_vector_cache
from rag_ime.text_utils import now_ms


class HybridRagRetrieverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-hybrid-rag-")
        self.db_path = Path(self.tmp.name) / "rag-ime.sqlite"
        self.core = LocalSqliteCoreClient(self.db_path)
        self.core.initialize()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def test_semantic_query_blends_user_task_and_compaction_summary_80_20(self) -> None:
        class Provider:
            fingerprint = "test:blend"

            def embed(self, text: str) -> list[float]:
                return {
                    "用户当前任务": [1.0, 0.0],
                    "压缩摘要": [0.0, 1.0],
                }[text]

        vector, metadata = _semantic_query_vector(
            Provider(),
            "用户当前任务",
            context_text="压缩摘要",
            context_weight=0.2,
        )

        self.assertTrue(metadata["applied"])
        self.assertEqual(metadata["queryWeight"], 0.8)
        self.assertEqual(metadata["contextWeight"], 0.2)
        self.assertAlmostEqual(vector[0], 0.9701425, places=6)
        self.assertAlmostEqual(vector[1], 0.2425356, places=6)

    def test_fts_discovery_hydrates_related_primary_keys_before_ranking(self) -> None:
        from rag_ime.hybrid_rag_retriever import _active_docs
        from tests.test_memory_projection_consistency import MemoryProjectionConsistencyTests

        with self.connect() as conn:
            for index in range(100):
                MemoryProjectionConsistencyTests._insert_atom(conn, f"budget:{index}",
                    "LayerTwoGold exact fact" if index == 99 else f"unrelated public fact {index}", 10)
            rebuild_retrieval_docs(conn, project="test")
            hydrated: list[int] = []
            def observed(*args, **kwargs):
                docs = _active_docs(*args, **kwargs)
                hydrated.append(len(docs))
                return docs
            with patch("rag_ime.hybrid_rag_retriever._active_docs", side_effect=observed):
                payload = retrieve_hybrid_rag_candidates(conn, HybridRagQuery(query_text="LayerTwoGold", project="test"))
            self.assertEqual(payload["memoryHits"][0]["source_id"], "budget:99")
            self.assertEqual(hydrated, [1])

    def test_unavailable_embedding_and_fts_retain_complete_substring_recall(self) -> None:
        from tests.test_memory_projection_consistency import MemoryProjectionConsistencyTests
        class Unavailable:
            fingerprint = "none"
            def embed(self, text):
                raise AssertionError("Unavailable embedding must not execute")
        with self.connect() as conn:
            for index in range(100):
                MemoryProjectionConsistencyTests._insert_atom(conn, f"fallback:{index}",
                    "FallbackGold exact fact" if index == 99 else f"unrelated public fact {index}", 10)
            rebuild_retrieval_docs(conn, project="test")
            conn.execute("DROP TABLE memory_retrieval_docs_fts")
            payload = retrieve_hybrid_rag_candidates(conn, HybridRagQuery(query_text="FallbackGold", project="test"), Unavailable())
        self.assertEqual(payload["memoryHits"][0]["source_id"], "fallback:99")
        self.assertFalse(payload["candidateDiscovery"]["candidateCountBounded"])
        self.assertEqual(payload["lanes"]["bm25_raw"]["implementation"], "lexical_substring_fallback")

    def test_governed_empty_fts_prefix_retains_tail_recall_after_canonical_forget(self) -> None:
        from tests.test_memory_projection_consistency import MemoryProjectionConsistencyTests
        with self.connect() as conn:
            for index in range(600):
                MemoryProjectionConsistencyTests._insert_atom(conn, f"forgotten:{index}", "GovernanceGold", 10)
            MemoryProjectionConsistencyTests._insert_atom(conn, "valid:tail", "GovernanceGold and many unrelated filler words to lower lexical rank", 10)
            rebuild_retrieval_docs(conn, project="test")
            conn.execute("UPDATE memory_atoms SET status = 'forgotten' WHERE id LIKE 'forgotten:%'")
            projection_body_reads: list[str] = []
            def record_projection_row(cursor: sqlite3.Cursor, row: tuple[object, ...]) -> sqlite3.Row:
                columns = [column[0] for column in cursor.description]
                if {"doc_id", "doc_type", "source_id", "raw_text"}.issubset(columns):
                    projection_body_reads.append(str(row[columns.index("doc_id")]))
                return sqlite3.Row(cursor, row)
            conn.row_factory = record_projection_row
            try:
                payload = retrieve_hybrid_rag_candidates(conn, HybridRagQuery(query_text="GovernanceGold", project="test"))
            finally:
                conn.row_factory = sqlite3.Row
        self.assertTrue(payload["memoryHits"])
        self.assertEqual({hit["source_id"] for hit in payload["memoryHits"]}, {"valid:tail"})
        discovery = payload["candidateDiscovery"]
        self.assertEqual(discovery["strategy"], "governed_fts_prefix_empty")
        self.assertFalse(discovery["candidateCountBounded"])
        self.assertFalse(discovery["scanBounded"])
        self.assertEqual(discovery.get("admittedDocuments"), 1)
        self.assertNotIn("hydratedDocuments", discovery)
        self.assertGreater(len(projection_body_reads), discovery["admittedDocuments"])
        self.assertIn("atom:forgotten:0", projection_body_reads)

    def test_bm25_raw_exact_keyword_hit(self) -> None:
        self._record_event("多路召回", recent_context="RAG 输入法", tags=("RAG", "检索"))
        with self.connect() as conn:
            rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")
            payload = retrieve_hybrid_rag_candidates(conn, HybridRagQuery(query_text="多路召回", project="wisdom-weasel-rag-ime"))

        texts = [item["text"] for item in payload["candidates"]]
        self.assertTrue(any(text.startswith("多路召回") for text in texts), texts)
        self.assertGreaterEqual(payload["lanes"]["bm25_raw"]["count"], 1)
        self.assertEqual(payload["lanes"]["bm25_raw"]["implementation"], "sqlite_fts5_bm25")
        self.assertTrue(payload["lanes"]["bm25_raw"]["fts5Bm25"])
        raw_hits = [item for item in payload["hits"] if item["source_lane"] == "bm25_raw"]
        self.assertTrue(raw_hits)
        self.assertLess(raw_hits[0]["raw_score"], 0.0)

    def test_bm25_tags_hits_alias_when_raw_text_misses(self) -> None:
        event_id = self._record_event("本地模型实验", tags=("输入法",))
        with self.connect() as conn:
            apply_memory_book_plan(conn, memory_book_plan_from_compile_output(qwen_compile_output(event_id), project="wisdom-weasel-rag-ime", provider="deepseek", model="deepseek-v4-flash"))
            rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")
            payload = retrieve_hybrid_rag_candidates(conn, HybridRagQuery(query_text="千文三", project="wisdom-weasel-rag-ime"))

        self.assertGreaterEqual(payload["lanes"]["bm25_tags"]["count"], 1)
        self.assertIn("Qwen3", payload["query"]["matchedAliases"])
        self.assertEqual(payload["lanes"]["bm25_tags"]["implementation"], "sqlite_fts5_bm25")

    def test_tagmemo_graph_unlocks_related_topic(self) -> None:
        event_id = self._record_event("本地模型实验", tags=("输入法",))
        with self.connect() as conn:
            apply_memory_book_plan(conn, memory_book_plan_from_compile_output(qwen_compile_output(event_id), project="wisdom-weasel-rag-ime", provider="deepseek", model="deepseek-v4-flash"))
            rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")
            payload = retrieve_hybrid_rag_candidates(conn, HybridRagQuery(query_text="输入法", project="wisdom-weasel-rag-ime"))

        self.assertIn("大模型", payload["query"]["activatedTags"])
        self.assertGreaterEqual(payload["lanes"]["tagmemo"]["count"], 1)
        self.assertEqual(payload["lanes"]["tagmemo"]["implementation"], "sqlite_fts5_bm25")

    def test_bm25_reports_substring_fallback_only_when_fts_table_is_unavailable(self) -> None:
        self._record_event("多路召回", recent_context="RAG 输入法", tags=("RAG", "检索"))
        with self.connect() as conn:
            rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")
            conn.execute("DROP TABLE memory_retrieval_docs_fts")
            payload = retrieve_hybrid_rag_candidates(
                conn,
                HybridRagQuery(query_text="多路召回", project="wisdom-weasel-rag-ime"),
            )

        lane = payload["lanes"]["bm25_raw"]
        self.assertGreaterEqual(lane["count"], 1)
        self.assertEqual(lane["implementation"], "lexical_substring_fallback")
        self.assertTrue(lane["lexicalFallback"])
        self.assertFalse(lane["fts5Bm25"])

    def test_time_lane_promotes_independent_timeline(self) -> None:
        event_id = self._record_event("RAG 输入法多路召回方案", tags=("RAG",))
        with self.connect() as conn:
            self._insert_timeline(
                conn,
                timeline_id="timeline:2026-07-18",
                timeline_date="2026-07-18",
                summary="当天继续完善 RAG 输入法。",
                task_title="多路召回",
                source_event_ids=(event_id,),
            )
            rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")
            payload = retrieve_hybrid_rag_candidates(conn, HybridRagQuery(query_text="Daily Book", project="wisdom-weasel-rag-ime"))

        self.assertGreaterEqual(payload["lanes"]["time"]["count"], 1)
        self.assertEqual(
            payload["query"]["timelineIntent"]["reason"],
            "explicit_timeline",
        )
        self.assertIn("多路召回", [item["text"] for item in payload["candidates"]])
        self.assertNotIn("RAG 输入法多路召回方案", [item["text"] for item in payload["candidates"]])
        self.assertTrue(
            any(
                item["doc_type"] == "timeline"
                and item["source_id"] == "timeline:2026-07-18"
                for item in payload["memoryHits"]
            )
        )

    def test_daily_timeline_does_not_pollute_ordinary_fact_recall(self) -> None:
        event_id = self._record_event("RAG 输入法多路召回方案", tags=("RAG",))
        with self.connect() as conn:
            self._insert_timeline(
                conn,
                timeline_id="timeline:ordinary-query",
                timeline_date="2026-07-18",
                summary="当天继续完善 RAG 输入法。",
                task_title="多路召回",
                source_event_ids=(event_id,),
            )
            apply_memory_book_plan(
                conn,
                memory_book_plan_from_compile_output(
                    qwen_compile_output(event_id),
                    project="wisdom-weasel-rag-ime",
                    provider="deepseek",
                    model="deepseek-v4-flash",
                ),
            )
            rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")
            payload = retrieve_hybrid_rag_candidates(
                conn,
                HybridRagQuery(
                    query_text="输入法本地模型如何配置",
                    project="wisdom-weasel-rag-ime",
                ),
            )

        self.assertFalse(payload["lanes"]["time"]["enabled"])
        self.assertEqual(payload["lanes"]["time"]["skippedReason"], "not_requested_by_query")
        self.assertFalse(
            any(item["doc_type"] == "timeline" for item in payload["memoryHits"])
        )
        self.assertTrue(
            any(item["source_id"] == "atom:qwen3-local-model" for item in payload["memoryHits"])
        )

    def test_vague_history_word_does_not_enable_daily_timeline(self) -> None:
        event_id = self._record_event("RAG 输入法多路召回方案", tags=("RAG",))
        with self.connect() as conn:
            self._insert_timeline(
                conn,
                timeline_id="timeline:vague-query",
                timeline_date="2026-07-18",
                summary="当天继续完善 RAG 输入法。",
                task_title="多路召回",
                source_event_ids=(event_id,),
            )
            rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")
            payload = retrieve_hybrid_rag_candidates(
                conn,
                HybridRagQuery(
                    query_text="之前那个模型怎么优化",
                    project="wisdom-weasel-rag-ime",
                    top_k=5,
                ),
            )

        self.assertFalse(payload["query"]["timelineRequested"])
        self.assertEqual(
            payload["query"]["timelineIntent"],
            {
                "requested": False,
                "reason": "none",
                "matched": [],
                "range": "",
            },
        )
        self.assertFalse(payload["lanes"]["time"]["enabled"])
        self.assertFalse(
            any(item["doc_type"] == "timeline" for item in payload["memoryHits"])
        )

    def test_recent_timeline_query_orders_independent_timelines_newest_first(self) -> None:
        old_event_id = self._record_event("旧的输入法工作", tags=("输入法",))
        new_event_id = self._record_event("新的记忆工作", tags=("记忆",))
        with self.connect() as conn:
            self._insert_timeline(
                conn,
                timeline_id="timeline:old",
                timeline_date="2026-07-15",
                summary="旧的输入法工作。",
                task_title="旧工作",
                source_event_ids=(old_event_id,),
            )
            self._insert_timeline(
                conn,
                timeline_id="timeline:new",
                timeline_date="2026-07-18",
                summary="新的记忆工作。",
                task_title="新工作",
                source_event_ids=(new_event_id,),
            )
            rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")
            with patch(
                "rag_ime.hybrid_rag_retriever.time.time",
                return_value=1_784_430_000,
            ):
                payload = retrieve_hybrid_rag_candidates(
                    conn,
                    HybridRagQuery(
                        query_text="最近几天我在做什么",
                        project="wisdom-weasel-rag-ime",
                        top_k=5,
                    ),
                )

        timeline_hits = [
            item for item in payload["memoryHits"] if item["doc_type"] == "timeline"
        ]
        self.assertTrue(payload["query"]["recentTimelineRequested"])
        self.assertEqual(
            payload["query"]["timelineIntent"]["range"],
            "recent_days",
        )
        self.assertGreaterEqual(len(timeline_hits), 2)
        self.assertEqual(timeline_hits[0]["metadata"]["timelineDate"], "2026-07-18")

    def test_yesterday_hard_filters_timeline_to_the_resolved_date(self) -> None:
        old_event_id = self._record_event("旧的 RAG IME 工作", tags=("RAG",))
        yesterday_event_id = self._record_event(
            "昨天的 RAG IME 工作",
            tags=("RAG",),
        )
        with self.connect() as conn:
            self._insert_timeline(
                conn,
                timeline_id="timeline:wrong-day",
                timeline_date="2026-07-11",
                summary="旧的 RAG IME 工作。",
                task_title="旧工作",
                source_event_ids=(old_event_id,),
            )
            self._insert_timeline(
                conn,
                timeline_id="timeline:yesterday",
                timeline_date="2026-07-18",
                summary="昨天的 RAG IME 工作。",
                task_title="昨天工作",
                source_event_ids=(yesterday_event_id,),
            )
            rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")
            with patch(
                "rag_ime.hybrid_rag_retriever.time.time",
                return_value=1_784_430_000,
            ):
                payload = retrieve_hybrid_rag_candidates(
                    conn,
                    HybridRagQuery(
                        query_text="昨天 RAG IME 做到哪里了",
                        project="wisdom-weasel-rag-ime",
                        top_k=8,
                    ),
                )

        timeline_hits = [
            item for item in payload["memoryHits"] if item["doc_type"] == "timeline"
        ]
        self.assertEqual(
            payload["query"]["timelineIntent"]["range"],
            "yesterday",
        )
        self.assertEqual(
            [item["source_id"] for item in timeline_hits],
            ["timeline:yesterday"],
        )

    def test_feedback_lane_promotes_accepted_phrase(self) -> None:
        self._record_event("多路召回", recent_context="RAG 输入法", tags=("RAG",))
        with self.connect() as conn:
            rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")
            conn.execute(
                """
                INSERT INTO candidate_feedback(created_at_ms, query_hash, candidate_text, source_type, memory_id, action, app, project, metadata_json)
                VALUES (?, 'q', '多路召回', 'phrase', 'phrase:多路召回', 'accepted', '', 'wisdom-weasel-rag-ime', '{}')
                """,
                (now_ms(),),
            )
            payload = retrieve_hybrid_rag_candidates(conn, HybridRagQuery(query_text="多路召回", project="wisdom-weasel-rag-ime"))

        self.assertGreaterEqual(payload["lanes"]["feedback"]["count"], 1)
        first = payload["candidates"][0]
        self.assertEqual(first["text"], "多路召回")
        self.assertIn("feedback_bonus", first["debug_features"])

    def test_feedback_lane_does_not_promote_unrelated_accepted_memory(self) -> None:
        self._record_event("多路召回", recent_context="RAG 输入法", tags=("RAG", "检索"))
        with self.connect() as conn:
            rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")
            conn.execute(
                """
                INSERT INTO candidate_feedback(created_at_ms, query_hash, candidate_text, source_type, memory_id, action, app, project, metadata_json)
                VALUES (?, 'q', '多路召回', 'phrase', 'phrase:多路召回', 'accepted', '', 'wisdom-weasel-rag-ime', '{}')
                """,
                (now_ms(),),
            )
            payload = retrieve_hybrid_rag_candidates(
                conn,
                HybridRagQuery(query_text="语音二遍识别", project="wisdom-weasel-rag-ime"),
            )

        self.assertEqual(payload["lanes"]["feedback"]["count"], 0)

    def test_hybrid_retrieval_does_not_surface_raw_sentence(self) -> None:
        self._record_event("这是一个很长的历史输入句子，不应该直接复读出来", recent_context="RAG 输入法", tags=("RAG",))
        with self.connect() as conn:
            rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")
            payload = retrieve_hybrid_rag_candidates(conn, HybridRagQuery(query_text="历史输入句子", project="wisdom-weasel-rag-ime"))

        self.assertNotIn("这是一个很长的历史输入句子，不应该直接复读出来", [item["text"] for item in payload["candidates"]])

    def test_hybrid_retriever_rejects_legacy_item_but_keeps_phrase(self) -> None:
        with self.connect() as conn:
            timestamp = now_ms()
            self._insert_phrase_source(
                conn,
                memory_id="phrase:遗留策略短语",
                text="遗留策略短语",
                timestamp=timestamp,
            )
            conn.executemany(
                """
                INSERT INTO memory_retrieval_docs(
                    doc_id, doc_type, source_id, raw_text, tags_text, aliases_text,
                    surface_hints_text, query_expansions_text, time_key, project, app,
                    owner_kind, owner_id, status, updated_at_ms, metadata_json
                ) VALUES (?, ?, ?, ?, '', '', ?, '', '',
                          'wisdom-weasel-rag-ime', '', 'user', 'default', 'active', ?, '{}')
                """,
                (
                    (
                        "item:stable:legacy",
                        "item",
                        "stable:legacy",
                        "遗留策略原文不应进入召回",
                        "",
                        timestamp,
                    ),
                    (
                        "phrase:遗留策略短语",
                        "phrase",
                        "phrase:遗留策略短语",
                        "遗留策略短语",
                        "遗留策略短语",
                        timestamp,
                    ),
                ),
            )
            payload = retrieve_hybrid_rag_candidates(
                conn,
                HybridRagQuery(
                    query_text="遗留策略",
                    project="wisdom-weasel-rag-ime",
                ),
            )

        self.assertNotIn("item", {hit["doc_type"] for hit in payload["memoryHits"]})
        self.assertNotIn(
            "遗留策略原文不应进入召回",
            [candidate["text"] for candidate in payload["candidates"]],
        )
        self.assertIn(
            "遗留策略短语",
            [candidate["text"] for candidate in payload["candidates"]],
        )

    def test_hybrid_retrieval_respects_tombstone_and_suppression(self) -> None:
        self._record_event("多路召回", recent_context="RAG 输入法", tags=("RAG",))
        self._record_event("TagMemo", recent_context="RAG 输入法", tags=("RAG",))
        with self.connect() as conn:
            rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")
            conn.execute(
                "INSERT INTO memory_tombstones(created_at_ms, target_type, target_value, reason, active, metadata_json) VALUES (?, 'memory_id', 'phrase:多路召回', 'test', 1, '{}')",
                (now_ms(),),
            )
            conn.execute(
                "INSERT INTO memory_candidate_suppressions(id, match_type, match_value, action, reason, strength, created_at_ms) VALUES ('s1', 'memory_id', 'phrase:TagMemo', 'block', 'test', 1.0, ?)",
                (now_ms(),),
            )
            payload = retrieve_hybrid_rag_candidates(conn, HybridRagQuery(query_text="RAG", project="wisdom-weasel-rag-ime"))

        texts = [item["text"] for item in payload["candidates"]]
        self.assertFalse(any(text.startswith("多路召回") for text in texts), texts)
        self.assertFalse(any(text.startswith("TagMemo") for text in texts), texts)

    def test_hybrid_retrieval_rejects_stale_projection_for_forgotten_evidence(self) -> None:
        event_id = self._record_event(
            "候选数据库验证",
            recent_context="正式激活前先验证",
            tags=("数据库",),
        )
        with self.connect() as conn:
            rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")
            self.assertIsNotNone(
                conn.execute(
                    """SELECT 1 FROM memory_retrieval_docs
                       WHERE source_id = 'phrase:候选数据库验证'"""
                ).fetchone()
            )
            # Do not rebuild the projection after forgetting the evidence. The
            # read path itself must fail closed during the outbox freshness gap.
            conn.execute(
                """INSERT INTO memory_tombstones(
                       created_at_ms, target_type, target_value, reason,
                       active, metadata_json
                   ) VALUES (?, 'source_event_id', ?, 'test-forget-source', 1, '{}')""",
                (now_ms(), str(event_id)),
            )
            payload = retrieve_hybrid_rag_candidates(
                conn,
                HybridRagQuery(
                    query_text="候选数据库验证",
                    project="wisdom-weasel-rag-ime",
                ),
            )

        self.assertFalse(
            any(
                item["source_id"] == "phrase:候选数据库验证"
                for item in payload["memoryHits"]
            )
        )
        self.assertNotIn(
            "候选数据库验证",
            [item["text"] for item in payload["candidates"]],
        )

    def test_hybrid_retrieval_hides_derived_artifact_when_raw_event_is_hidden(self) -> None:
        event_id = self._record_event("本地模型实验", tags=("输入法",))
        sessions = AgentSessionStore(self.db_path)
        session = sessions.create(title="hidden-raw", created_at_ms=1)
        sources = AgentMemorySourceStore(self.db_path, project="wisdom-weasel-rag-ime")
        sources.checkpoint_user_message(
            session_id=str(session["id"]),
            pi_entry_id="entry:hidden-raw",
            turn_id="turn:hidden-raw",
            text="这条逻辑来源用于治理隐藏原文后的派生记忆。",
            created_at_ms=1_700_000_000_000,
        )
        with self.connect() as conn:
            conn.execute(
                """UPDATE agent_memory_sources
                   SET input_event_id = ?, disposition = 'remember',
                       disposition_reason = 'manual_review_remembered'
                   WHERE pi_entry_id = 'entry:hidden-raw'""",
                (event_id,),
            )
            apply_memory_book_plan(
                conn,
                memory_book_plan_from_compile_output(
                    qwen_compile_output(event_id),
                    project="wisdom-weasel-rag-ime",
                    provider="deepseek",
                    model="deepseek-v4-flash",
                ),
            )
            conn.execute(
                "UPDATE memory_state SET deleted = 1 WHERE event_id = ?",
                (event_id,),
            )
            rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")
            payload = retrieve_hybrid_rag_candidates(
                conn,
                HybridRagQuery(
                    query_text="千文三",
                    project="wisdom-weasel-rag-ime",
                ),
            )

        self.assertFalse(
            any(
                item["source_id"] == "atom:qwen3-local-model"
                for item in payload["memoryHits"]
            ),
            payload["memoryHits"],
        )

    def test_hybrid_retrieval_rejects_stale_projection_with_only_unavailable_source(self) -> None:
        event_id = self._record_event("不应继续召回的治理证据", tags=("治理",))
        sessions = AgentSessionStore(self.db_path)
        session = sessions.create(title="unavailable-source", created_at_ms=1)
        sources = AgentMemorySourceStore(self.db_path, project="wisdom-weasel-rag-ime")
        sources.checkpoint_user_message(
            session_id=str(session["id"]),
            pi_entry_id="entry:unavailable-source",
            turn_id="turn:unavailable-source",
            text="这条逻辑来源稍后会被排除。",
            created_at_ms=1_700_000_000_000,
        )
        with self.connect() as conn:
            rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")
            self.assertIsNotNone(
                conn.execute(
                    """SELECT 1 FROM memory_retrieval_docs
                       WHERE source_id = 'phrase:不应继续召回的治理证据'"""
                ).fetchone()
            )
            # Simulate the interval before the projection worker consumes the
            # governance outbox. The read gate must still stop the stale row.
            conn.execute(
                """UPDATE agent_memory_sources
                   SET input_event_id = ?, disposition = 'not_for_memory',
                       disposition_reason = 'manual_review_excluded'
                   WHERE pi_entry_id = 'entry:unavailable-source'""",
                (event_id,),
            )
            payload = retrieve_hybrid_rag_candidates(
                conn,
                HybridRagQuery(
                    query_text="不应继续召回的治理证据",
                    project="wisdom-weasel-rag-ime",
                ),
            )

        self.assertFalse(
            any(
                item["source_id"] == "phrase:不应继续召回的治理证据"
                for item in payload["memoryHits"]
            )
        )
        self.assertNotIn(
            "不应继续召回的治理证据",
            [item["text"] for item in payload["candidates"]],
        )

    def test_hybrid_retrieval_stays_under_budget(self) -> None:
        self._record_event("多路召回", recent_context="RAG 输入法", tags=("RAG",))
        with self.connect() as conn:
            rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")
            payload = retrieve_hybrid_rag_candidates(conn, HybridRagQuery(query_text="多路召回", project="wisdom-weasel-rag-ime", latency_budget_ms=1000))

        self.assertLessEqual(payload["elapsedMs"], 1000)
        self.assertFalse(payload["overBudget"])

    def test_disabled_lane_is_not_executed_and_vector_lanes_report_unavailable(self) -> None:
        self._record_event("多路召回", recent_context="RAG 输入法", tags=("RAG",))
        with self.connect() as conn:
            rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")
            payload = retrieve_hybrid_rag_candidates(
                conn,
                HybridRagQuery(
                    query_text="多路召回",
                    project="wisdom-weasel-rag-ime",
                    enabled_lanes=(("bm25_raw", False), ("bm25_tags", True)),
                ),
            )

        self.assertFalse(payload["lanes"]["bm25_raw"]["enabled"])
        self.assertEqual(payload["lanes"]["bm25_raw"]["count"], 0)
        self.assertEqual(
            payload["lanes"]["bm25_raw"]["skippedReason"],
            "disabled_by_effective_runtime_config",
        )
        self.assertFalse(payload["lanes"]["vector_raw"]["available"])
        self.assertEqual(
            payload["lanes"]["vector_raw"]["skippedReason"],
            "embedding_provider_not_wired",
        )
        self.assertTrue(all(item["source_lane"] != "bm25_raw" for item in payload["hits"]))
        self.assertFalse(payload["lanes"]["bm25_tags"]["lexicalFallback"])
        self.assertTrue(payload["lanes"]["bm25_tags"]["fts5Bm25"])
        self.assertEqual(
            payload["lanes"]["bm25_tags"]["implementation"],
            "sqlite_fts5_bm25",
        )

    def test_precomputed_vector_lanes_run_in_parallel(self) -> None:
        self._record_event("苹果电脑输入方案", tags=("输入法", "本地模型"))
        provider = SemanticFakeProvider()
        with self.connect() as conn:
            rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")
            stats = rebuild_retrieval_doc_vectors(conn, provider, project="wisdom-weasel-rag-ime")
            payload = retrieve_hybrid_rag_candidates(
                conn,
                HybridRagQuery(query_text="Mac 上怎么打字", project="wisdom-weasel-rag-ime"),
                provider,
            )

        self.assertGreater(stats["documents"], 0)
        self.assertTrue(payload["parallelExecution"])
        self.assertGreater(payload["vectorIndexDocuments"], 0)
        self.assertTrue(payload["lanes"]["vector_raw"]["available"])
        self.assertGreater(payload["lanes"]["vector_raw"]["count"], 0)
        self.assertEqual(payload["lanes"]["vector_tag_boost"]["implementation"], "precomputed_cosine")

    def test_vector_warmup_populates_provider_cache_for_first_query(self) -> None:
        self._record_event("苹果电脑输入方案", tags=("输入法", "本地模型"))
        provider = SemanticFakeProvider()
        with self.connect() as conn:
            rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")
            rebuild_retrieval_doc_vectors(conn, provider, project="wisdom-weasel-rag-ime")
            warmup = warm_retrieval_doc_vector_cache(
                conn,
                provider.fingerprint,
                project="wisdom-weasel-rag-ime",
            )
            with patch("rag_ime.retrieval_vector_index._vector", side_effect=AssertionError("cold parse")):
                payload = retrieve_hybrid_rag_candidates(
                    conn,
                    HybridRagQuery(query_text="Mac 上怎么打字", project="wisdom-weasel-rag-ime"),
                    provider,
                )

        self.assertTrue(warmup["ok"])
        self.assertGreater(warmup["documents"], 0)
        self.assertGreater(payload["vectorIndexDocuments"], 0)

    def test_group_compatibility_prioritizes_exact_and_rejects_other_short_term(self) -> None:
        with self.connect() as conn:
            docs = [
                ("phrase:exact", "phrase:exact", "完成前台闭环", "wisdom-weasel-rag-ime", "com.apple.TextEdit", '{"contextGroupId":"doc:a","shortTerm":true}'),
                ("phrase:project", "phrase:project", "限制模型调用", "wisdom-weasel-rag-ime", "com.apple.TextEdit", '{"contextGroupId":"doc:b"}'),
                ("phrase:blocked-short", "phrase:blocked-short", "其他文档短期内容", "wisdom-weasel-rag-ime", "com.apple.TextEdit", '{"contextGroupId":"doc:b","shortTerm":true}'),
                ("phrase:global", "phrase:global", "保持普通拼音稳定", "", "", '{"contextGroupId":"global"}'),
            ]
            for doc_id, source_id, hint, project, app, metadata in docs:
                self._insert_phrase_source(
                    conn,
                    memory_id=source_id,
                    text=hint,
                    timestamp=now_ms(),
                    project=project,
                    app=app,
                )
                conn.execute(
                    """
                    INSERT INTO memory_retrieval_docs(
                        doc_id, doc_type, source_id, raw_text, tags_text, aliases_text,
                        surface_hints_text, query_expansions_text, time_key, project, app,
                        status, updated_at_ms, metadata_json
                    ) VALUES (?, 'phrase', ?, '前台上下文', '前台', '', ?, '', '', ?, ?, 'active', ?, ?)
                    """,
                    (doc_id, source_id, hint, project, app, now_ms(), metadata),
                )
            payload = retrieve_hybrid_rag_candidates(
                conn,
                HybridRagQuery(
                    query_text="前台",
                    project="wisdom-weasel-rag-ime",
                    app="com.apple.TextEdit",
                    context_group_id="doc:a",
                    context_group_level="document",
                ),
            )

        texts = [item["text"] for item in payload["candidates"]]
        self.assertEqual(texts[0], "完成前台闭环")
        self.assertIn("限制模型调用", texts)
        self.assertIn("保持普通拼音稳定", texts)
        self.assertNotIn("其他文档短期内容", texts)
        compatibility = {item["text"]: item["metadata"]["groupCompatibility"] for item in payload["candidates"]}
        self.assertEqual(compatibility["完成前台闭环"], 1.0)
        self.assertEqual(compatibility["限制模型调用"], 0.75)
        self.assertEqual(compatibility["保持普通拼音稳定"], 0.2)
        self.assertEqual(payload["lanes"]["bm25_raw"]["implementation"], "lexical_substring_fallback")

    def test_vector_revision_mismatch_falls_back_to_bm25_without_pseudo_consensus(self) -> None:
        self._record_event("VPN 账号使用 account-B", tags=("VPN",))

        class Provider:
            fingerprint = "test:mismatched-vector"

            def embed(self, text: str) -> list[float]:
                return [1.0, float(len(text) % 7)]

        with self.connect() as conn:
            rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")
            doc = conn.execute(
                """
                SELECT doc_id, source_revision, projection_version
                FROM memory_retrieval_docs
                WHERE doc_type = 'phrase'
                ORDER BY doc_id
                LIMIT 1
                """
            ).fetchone()
            conn.execute(
                """
                INSERT INTO memory_retrieval_doc_vectors(
                    doc_id, provider_fingerprint, raw_vector_json,
                    tag_vector_json, group_vector_json, dimensions,
                    source_revision, projection_version, built_at_ms, updated_at_ms
                ) VALUES (?, ?, '[1,0]', '[1,0]', '[]', 2, ?, ?, 1, 1)
                """,
                (
                    doc["doc_id"],
                    Provider.fingerprint,
                    int(doc["source_revision"]) + 1,
                    int(doc["projection_version"]),
                ),
            )
            payload = retrieve_hybrid_rag_candidates(
                conn,
                HybridRagQuery(
                    query_text="VPN account-B",
                    project="wisdom-weasel-rag-ime",
                ),
                Provider(),
            )

        self.assertEqual(payload["vectorIndexDocuments"], 0)
        self.assertTrue(
            any("account-B" in item["text"] for item in payload["memoryHits"]),
            payload,
        )
        self.assertEqual(payload["lanes"]["vector_raw"]["count"], 0)

    def test_owner_visibility_is_enforced_before_alias_and_retrieval_lanes(self) -> None:
        event_id = self._record_event("公共输入法记忆", tags=("输入法",))
        with self.connect() as conn:
            for role_id, atom_id, text, alias in (
                ("role-a", "atom:role-a-private", "甲角色的私有海盐方案", "海盐密钥甲"),
                ("role-b", "atom:role-b-private", "乙角色的私有薄荷方案", "薄荷密钥乙"),
            ):
                apply_memory_book_plan(
                    conn,
                    memory_book_plan_from_compile_output(
                        {
                            "memoryAtoms": [
                                {
                                    "atomId": atom_id,
                                    "kind": "preference",
                                    "canonicalText": text,
                                    "aliases": [alias],
                                    "surfaceHints": [text],
                                    "sourceEventIds": [event_id],
                                    "confidence": 0.95,
                                    "qualityScore": 0.95,
                                }
                            ]
                        },
                        project="wisdom-weasel-rag-ime",
                        provider="test",
                        model="test",
                        owner_kind="agent",
                        owner_id=role_id,
                        run_kind="daily_curation",
                    ),
                )
            rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")

            default_payload = retrieve_hybrid_rag_candidates(
                conn,
                HybridRagQuery(
                    query_text="海盐密钥甲",
                    project="wisdom-weasel-rag-ime",
                ),
            )
            role_a_payload = retrieve_hybrid_rag_candidates(
                conn,
                HybridRagQuery(
                    query_text="海盐密钥甲",
                    project="wisdom-weasel-rag-ime",
                    visible_owners=agent_visible_memory_owners(
                        project="wisdom-weasel-rag-ime",
                        role_id="role-a",
                    ),
                ),
            )
            role_b_payload = retrieve_hybrid_rag_candidates(
                conn,
                HybridRagQuery(
                    query_text="海盐密钥甲",
                    project="wisdom-weasel-rag-ime",
                    visible_owners=agent_visible_memory_owners(
                        project="wisdom-weasel-rag-ime",
                        role_id="role-b",
                    ),
                ),
            )

        self.assertNotIn("海盐密钥甲", default_payload["query"]["matchedAliases"])
        self.assertFalse(
            any(item["source_id"] == "atom:role-a-private" for item in default_payload["hits"])
        )
        self.assertIn("海盐密钥甲", role_a_payload["query"]["matchedAliases"])
        self.assertTrue(
            any(item["source_id"] == "atom:role-a-private" for item in role_a_payload["hits"])
        )
        self.assertTrue(
            all(item["metadata"]["ownerId"] != "role-b" for item in role_a_payload["hits"])
        )
        self.assertNotIn("海盐密钥甲", role_b_payload["query"]["matchedAliases"])
        self.assertFalse(
            any(item["source_id"] == "atom:role-a-private" for item in role_b_payload["hits"])
        )

    def _record_event(self, text: str, *, recent_context: str = "", tags: tuple[str, ...] = ()) -> int:
        memory_id = self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=now_ms(),
                source="manual",
                committed_text=text,
                privacy_disposition="allowed",
                recent_context=recent_context,
                project="wisdom-weasel-rag-ime",
                tags=tags,
            )
        )
        event_id = int(memory_id.split(":", 1)[1])
        with self.connect() as conn:
            apply_memory_book_plan(
                conn,
                memory_book_plan_from_compile_output(
                    {
                        "phraseCandidates": [
                            {"text": text, "tags": list(tags), "sourceEventIds": [event_id], "weight": 0.8}
                        ]
                    },
                    project="wisdom-weasel-rag-ime",
                    provider="deepseek",
                    model="deepseek-v4-flash",
                ),
            )
        return event_id

    def _insert_phrase_source(
        self,
        conn: sqlite3.Connection,
        *,
        memory_id: str,
        text: str,
        timestamp: int,
        project: str = "wisdom-weasel-rag-ime",
        app: str = "",
    ) -> None:
        conn.execute(
            """
            INSERT INTO memory_items(
                memory_id, kind, text, normalized_text, summary, project, app,
                status, privacy_class, created_at_ms, updated_at_ms, metadata_json
            ) VALUES (?, 'phrase', ?, ?, '', ?, ?, 'approved', 'local', ?, ?, '{}')
            """,
            (memory_id, text, text, project, app, timestamp, timestamp),
        )

    def _insert_timeline(
        self,
        conn: sqlite3.Connection,
        *,
        timeline_id: str,
        timeline_date: str,
        summary: str,
        task_title: str,
        source_event_ids: tuple[int, ...],
    ) -> None:
        timestamp = now_ms()
        segments = [
            {
                "title": task_title,
                "summary": summary,
                "app": "com.openai.codex",
                "apps": ["com.openai.codex"],
                "sourceEventIds": list(source_event_ids),
            }
        ]
        conn.execute(
            """
            INSERT INTO daily_activity_timelines(
                timeline_id, project, timeline_date, timezone, status,
                source_event_ids_json, source_event_hash, segments_json,
                summary_text, event_count, segment_count, approved_book_id,
                approved_by, approved_at_ms, metadata_json,
                created_at_ms, updated_at_ms
            ) VALUES (?, 'wisdom-weasel-rag-ime', ?, 'Asia/Shanghai', 'approved',
                      ?, ?, ?, ?, ?, 1, '', 'test:auto', ?, ?, ?, ?)
            """,
            (
                timeline_id,
                timeline_date,
                json.dumps(list(source_event_ids)),
                f"hash:{timeline_id}",
                json.dumps(segments, ensure_ascii=False),
                summary,
                len(source_event_ids),
                timestamp,
                json.dumps(
                    {
                        "derivedArtifactType": "daily_activity_timeline",
                        "automaticPromotion": True,
                        "explicitApprovalRequired": False,
                    }
                ),
                timestamp,
                timestamp,
            ),
        )


class SemanticFakeProvider:
    fingerprint = "test-semantic:v1"

    def embed(self, text: str) -> list[float]:
        semantic = any(term in text for term in ("苹果电脑", "输入方案", "输入法", "本地模型"))
        return [1.0, 0.0] if semantic else [0.0, 1.0]

    def embed_query(self, text: str) -> list[float]:
        return [1.0, 0.0] if "Mac" in text or "打字" in text else [0.0, 1.0]


def qwen_compile_output(event_id: int) -> dict[str, object]:
    return {
        "schemaVersion": "rag-ime.memory-book-compile.v1",
        "dailyBooks": [],
        "memoryAtoms": [
            {
                "atomId": "atom:qwen3-local-model",
                "kind": "project_fact",
                "claimKey": "project:rag-ime.local-model",
                "canonicalText": "Qwen3 是输入法本地模型候选实验的一部分。",
                "summary": "用户会把千问三、千文三都指向 Qwen3 本地模型。",
                "tags": ["大模型", "本地模型", "LLM", "Qwen", "输入法"],
                "aliases": ["千文三", "千问三", "Qwen3"],
                "surfaceHints": ["Qwen3"],
                "queryExpansions": ["千问三", "Qwen3", "大模型"],
                "sourceEventIds": [event_id],
                "directCandidateAllowed": False,
                "confidence": 0.9,
                "qualityScore": 0.86,
            }
        ],
        "tagEdges": [
            {"src": "输入法", "dst": "大模型", "edgeType": "related", "weight": 0.8, "evidenceEventIds": [event_id]},
            {"src": "大模型", "dst": "本地模型", "edgeType": "related", "weight": 0.8, "evidenceEventIds": [event_id]},
        ],
        "phraseCandidates": [],
        "warnings": [],
    }


def book_compile_output(event_id: int) -> dict[str, object]:
    return {
        "schemaVersion": "rag-ime.memory-book-compile.v1",
        "dailyBooks": [
            {
                "bookKey": "2026-07-06",
                "title": "RAG 输入法多路召回方案",
                "summary": "用户希望借鉴 VCP 的 BM25、向量、TagMemo 和 Time。",
                "tags": ["RAG", "输入法", "VCP"],
                "surfaceHints": ["多路召回", "TagMemo"],
                "queryExpansions": ["VCP RAG", "Daily Book"],
                "sourceEventIds": [event_id],
                "confidence": 0.86,
            }
        ],
        "memoryAtoms": [],
        "tagEdges": [],
        "phraseCandidates": [],
        "warnings": [],
    }


if __name__ == "__main__":
    unittest.main()
