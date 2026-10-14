from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from rag_ime.agent_context_runtime import AgentContextRuntime
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.embeddings import HashingEmbeddingProvider
from rag_ime.local_sqlite_core import LocalSqliteCoreClient
from rag_ime.memory_book_compiler import (
    apply_memory_book_plan,
    memory_book_plan_from_compile_output,
)
from rag_ime.models import InputEvent
from rag_ime.retrieval_docs import rebuild_retrieval_docs
from rag_ime.retrieval_vector_index import rebuild_retrieval_doc_vectors
from rag_ime.session_memory_recall import (
    SessionMemoryRecallBuilder,
    _select_hits,
    _visible_memory_owners_for_trigger,
)
from rag_ime.text_utils import now_ms


PROJECT = "wisdom-weasel-rag-ime"


class SessionMemoryRecallTests(unittest.TestCase):
    def test_current_recall_strategy_and_historical_snapshot_contract(self) -> None:
        from copy import deepcopy
        from rag_ime.contracts.json_schema import ContractValidationError, validate_contract

        payload = self.builder.build(self.session_id, role_id="", query_text="缓存方案")["payload"]
        self.assertEqual(payload["retrieval"]["strategy"], "hybrid_book_atom")
        validate_contract(payload, "session-memory-recall.v1.json")
        historical = deepcopy(payload)
        historical["retrieval"]["strategy"] = "vcp_hybrid_book_atom"
        validate_contract(historical, "session-memory-recall.v1.json")
        historical["retrieval"]["strategy"] = "unknown_strategy"
        with self.assertRaises(ContractValidationError):
            validate_contract(historical, "session-memory-recall.v1.json")

    def test_selected_evidence_preserves_scope_time_and_fact_support(self):
        selected, _ = _select_hits([{
            "source_id": "timeline:review", "doc_type": "timeline",
            "text": "昨天讨论了缓存方案", "score": 1.0, "confidence": 0.9,
            "evidence_event_ids": [42], "tags": ["缓存方案"],
            "metadata": {"docType": "timeline", "sourceId": "timeline:review",
                "title": "缓存方案", "lanes": ["bm25_raw"], "sourceUpdatedAtMs": 1234, "sourceStartMs": 1000,
                "sourceEndMs": 1200, "project": PROJECT, "claimState": "current",
                "corroborationOnly": True, "maySupportFacts": False},
        }], query_text="昨天的缓存方案", max_items=4, max_chars=2000,
            detail_level="compact", timeline_allowed=True, timeline_max_items=2)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["sourceUpdatedAtMs"], 1234)
        self.assertEqual(selected[0]["project"], PROJECT)
        self.assertEqual(selected[0]["evidenceEventIds"], [42])
        self.assertTrue(selected[0]["corroborationOnly"])
        self.assertFalse(selected[0]["maySupportFacts"])

    def test_successful_embedding_is_not_reported_as_fallback(self):
        payload = SessionMemoryRecallBuilder(self.db_path, project=PROJECT,
            embedding_provider=HashingEmbeddingProvider(dimensions=8)).build(
                self.session_id, role_id="", query_text="缓存方案")["payload"]
        self.assertEqual(payload["retrieval"]["requestedEmbeddingProvider"],
                         payload["retrieval"]["embeddingProvider"])
        self.assertFalse(payload["retrieval"]["embeddingFallback"])

    def test_no_hits_still_exposes_curation_watermark_without_claiming_full_coverage(self):
        from rag_ime.agent_context_runtime import render_provider_context_items
        with self.connect() as conn:
            conn.execute("""INSERT INTO memory_curation_cursors
                (owner_kind, owner_id, project, lane, last_source_created_at_ms, last_source_id,
                 last_run_ms, status, next_due_at_ms, updated_at_ms)
                VALUES ('user', 'default', ?, 'daily', 1787152674541, 'source:covered',
                        1788610060286, 'idle', 1788652319882, 1788610060286)""", (PROJECT,))
        payload = self.builder.build(self.session_id, role_id="", query_text="不存在的项目要求")["payload"]
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["coverage"]["processedThroughAtMs"], 1787152674541)
        rendered = render_provider_context_items([{"sourceKind": "memory_bootstrap", "payload": payload}])
        self.assertIn("2026-08-19", rendered)
        self.assertIn("无命中不代表", rendered)
        room_payload = self.builder.build(self.session_id, role_id="", query_text="项目要求", trigger="room_task")["payload"]
        self.assertNotIn("coverage", room_payload)

    def test_room_and_subagent_tasks_exclude_cross_project_personal_memory(self) -> None:
        for trigger in ("room_task", "subagent_task"):
            with self.subTest(trigger=trigger):
                owners = _visible_memory_owners_for_trigger(
                    project="wisdom-weasel-rag-ime",
                    role_id="implementer",
                    session_id="agent:minecraft-partner",
                    room_ids=("room:minecraft",),
                    trigger=trigger,
                )
                self.assertEqual(
                    owners,
                    (
                        ("session", "agent:minecraft-partner"),
                        ("room", "room:minecraft"),
                    ),
                )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="rag-ime-session-recall-")
        self.db_path = Path(self.temporary.name) / "rag-ime.sqlite"
        self.core = LocalSqliteCoreClient(self.db_path)
        self.core.initialize()
        self.sessions = AgentSessionStore(self.db_path)
        self.context_runtime = AgentContextRuntime(self.db_path)
        self.builder = SessionMemoryRecallBuilder(self.db_path, project=PROJECT)
        self.session = self.sessions.create(title="角色 A", role_id="role-a")
        self.session_id = str(self.session["id"])

    def tearDown(self) -> None:
        self.temporary.cleanup()

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

    def test_recent_complete_input_drives_recall_without_crossing_role_scope(self) -> None:
        event_id = self._record_input("最近正在讨论角色私有方案")
        with self.connect() as conn:
            for role_id, atom_id, text, alias in (
                ("role-a", "atom:role-a-salt", "甲角色采用海盐缓存方案", "海盐密钥甲"),
                ("role-b", "atom:role-b-mint", "乙角色采用薄荷缓存方案", "薄荷密钥乙"),
            ):
                apply_memory_book_plan(
                    conn,
                    memory_book_plan_from_compile_output(
                        {
                            "memoryAtoms": [
                                {
                                    "atomId": atom_id,
                                    "kind": "project_fact",
                                    "canonicalText": text,
                                    "aliases": [alias],
                                    "tags": ["缓存方案"],
                                    "sourceEventIds": [event_id],
                                    "confidence": 0.95,
                                    "qualityScore": 0.95,
                                }
                            ]
                        },
                        project=PROJECT,
                        provider="test",
                        model="test",
                        owner_kind="agent",
                        owner_id=role_id,
                        run_kind="daily_curation",
                    ),
                )
            rebuild_retrieval_docs(conn, project=PROJECT)
            rebuild_retrieval_doc_vectors(
                conn,
                HashingEmbeddingProvider(dimensions=16),
                project=PROJECT,
            )

        self._record_input("海盐密钥甲")
        role_a = self.builder.build(
            self.session_id,
            role_id="role-a",
            query_text="继续刚才那个方案",
        )
        role_b = self.builder.build(
            "agent:role-b-test",
            role_id="role-b",
            query_text="继续刚才那个方案",
        )
        lexical_fallback = SessionMemoryRecallBuilder(
            self.db_path,
            project=PROJECT,
            embedding_provider=_BrokenEmbeddingProvider(),
        ).build(
            "agent:role-a-fallback",
            role_id="role-a",
            query_text="海盐缓存方案",
        )

        role_a_payload = role_a["payload"]
        role_b_payload = role_b["payload"]
        self.assertTrue(role_a_payload["query"]["recentCompleteInputUsedForRetrieval"])
        self.assertIn("atom:role-a-salt", role_a_payload["sourceIds"])
        selected_atom = next(item for item in role_a_payload["items"] if item["sourceId"] == "atom:role-a-salt")
        self.assertEqual(selected_atom["project"], PROJECT)
        self.assertEqual(selected_atom["claimState"], "current")
        self.assertIn("validFromMs", selected_atom)
        self.assertIn("sourceUpdatedAtMs", selected_atom)
        self.assertNotIn("atom:role-b-mint", role_a_payload["sourceIds"])
        self.assertNotIn("atom:role-a-salt", role_b_payload["sourceIds"])
        self.assertEqual(role_a_payload["policy"]["rawRecentInputInjected"], False)
        self.assertTrue(
            lexical_fallback["payload"]["retrieval"]["embeddingFallback"]
        )
        self.assertIn("atom:role-a-salt", lexical_fallback["payload"]["sourceIds"])

        self.context_runtime.enqueue(**role_a)
        rendered = str(self.context_runtime.materialize(self.session_id)["prompt"])
        self.assertIn("## Session 记忆", rendered)
        self.assertIn("### 事实与偏好", rendered)
        self.assertIn("甲角色采用海盐缓存方案", rendered)
        self.assertNotIn("乙角色采用薄荷缓存方案", rendered)
        self.assertIn("atom:role-a-salt", rendered)
        self.assertNotIn("agent/role-a", rendered)
        self.assertNotIn("命中通道", rendered)
        self.assertNotIn("相关度", rendered)
        self.assertFalse(role_a_payload["retrieval"]["temporalIntent"])
        self.assertEqual(
            role_a_payload["retrieval"]["timelineIntent"]["reason"],
            "none",
        )
        self.assertFalse(role_a_payload["retrieval"]["activityTimelineIncluded"])
        self.assertEqual(role_a["lifecycle"], "persistent")

    def test_preverified_snapshot_initializes_without_replaying_migrations(self) -> None:
        builder = SessionMemoryRecallBuilder(
            self.db_path,
            project=PROJECT,
            preverified_schema=True,
        )

        with patch(
            "rag_ime.session_memory_recall.apply_database_migrations",
            side_effect=AssertionError("migration replay is forbidden"),
        ) as migrate:
            builder.initialize()

        migrate.assert_not_called()

    def test_corrected_content_with_same_source_id_changes_refresh_identity(self) -> None:
        def retrieval(text: str) -> dict[str, object]:
            return {
                "query": {
                    "primary": "同一查询",
                    "matchedAliases": [],
                    "activatedTags": [],
                    "vectorFusion": {
                        "applied": False,
                        "queryWeight": 1.0,
                        "contextWeight": 0.0,
                    },
                },
                "memoryHits": [
                    {
                        "doc_type": "atom",
                        "source_id": "atom:stable-source",
                        "text": text,
                        "score": 1.0,
                        "confidence": 1.0,
                        "tags": [],
                        "metadata": {"lanes": ["bm25_raw"]},
                    }
                ],
            }

        with patch(
            "rag_ime.session_memory_recall.retrieve_hybrid_rag_candidates",
            return_value=retrieval("旧的项目事实"),
        ):
            before = self.builder.build(
                self.session_id,
                role_id="role-a",
                query_text="同一查询",
                trigger="turn_start",
            )
        with patch(
            "rag_ime.session_memory_recall.retrieve_hybrid_rag_candidates",
            return_value=retrieval("纠正后的项目事实"),
        ):
            after = self.builder.build(
                self.session_id,
                role_id="role-a",
                query_text="同一查询",
                trigger="turn_start",
            )

        self.assertEqual(
            before["payload"]["sourceIds"],
            after["payload"]["sourceIds"],
        )
        self.assertNotEqual(
            before["payload"]["recallId"],
            after["payload"]["recallId"],
        )
        self.assertNotEqual(before["dedupe_key"], after["dedupe_key"])

    def test_daily_activity_book_requires_temporal_or_continuation_intent(self) -> None:
        hits = [
            {
                "doc_type": "book",
                "source_id": "book:daily:activity:test",
                "text": (
                    "当天时间线：上午学习 FFN 隐藏层。；"
                    "下午修复 RAG IME 的 Session 召回。；"
                    + "晚上整理其他课程内容。；" * 80
                ),
                "score": 1.2,
                "confidence": 1.0,
                "tags": ["daily", "activity-timeline"],
                "metadata": {"lanes": ["bm25_raw", "time"]},
            },
            {
                "doc_type": "atom",
                "source_id": "atom:pi-context",
                "text": "Pi Runtime 将召回包放进 developer 上下文",
                "score": 1.1,
                "confidence": 1.0,
                "tags": ["PI", "RAG"],
                "metadata": {"lanes": ["bm25_raw"]},
            },
        ]

        focused, _ = _select_hits(
            hits,
            query_text="Pi Runtime 如何注入 RAG？",
            max_items=8,
            max_chars=6_400,
        )
        continuing, _ = _select_hits(
            hits,
            query_text="继续最近的 RAG IME 工作",
            max_items=8,
            max_chars=6_400,
        )

        self.assertEqual([item["sourceId"] for item in focused], ["atom:pi-context"])
        self.assertEqual(
            [item["sourceId"] for item in continuing],
            ["book:daily:activity:test", "atom:pi-context"],
        )
        self.assertIn("Session 召回", continuing[0]["text"])
        self.assertNotIn("FFN 隐藏层", continuing[0]["text"])

        unrelated, _ = _select_hits(
            [
                {
                    "doc_type": "book",
                    "source_id": "book:daily:activity:unrelated",
                    "text": "当天时间线：上午切换 CAS 账号；下午学习 FFN 隐藏层。",
                    "score": 1.2,
                    "confidence": 1.0,
                    "tags": ["daily", "activity-timeline"],
                    "metadata": {"lanes": ["bm25_raw", "time"]},
                },
                hits[1],
            ],
            query_text="我最近在 RAG IME 项目里做了什么？",
            max_items=8,
            max_chars=6_400,
        )
        self.assertEqual(
            [item["sourceId"] for item in unrelated],
            ["atom:pi-context"],
        )

    def test_recall_detail_level_expands_budgets_without_injecting_full_book_by_default(
        self,
    ) -> None:
        long_text = "主题摘要。" + "这是用于验证渐进式展开的事实片段。" * 120
        hits = [
            {
                "doc_type": "book",
                "source_id": "book:progressive",
                "text": long_text,
                "score": 1.0,
                "confidence": 1.0,
                "tags": ["记忆"],
                "metadata": {"lanes": ["bm25_raw"]},
            }
        ]

        compact, _ = _select_hits(
            hits,
            query_text="记忆怎么召回",
            max_items=12,
            max_chars=14_000,
            detail_level="compact",
        )
        detailed, _ = _select_hits(
            hits,
            query_text="记忆怎么召回",
            max_items=12,
            max_chars=14_000,
            detail_level="detailed",
        )

        self.assertLessEqual(len(compact[0]["text"]), 480)
        self.assertGreater(len(detailed[0]["text"]), len(compact[0]["text"]))

    def test_selected_book_suppresses_its_duplicate_member_atom(self) -> None:
        selected, omitted = _select_hits(
            [
                {
                    "doc_type": "atom",
                    "source_id": "atom:delivery-preference",
                    "text": "代码任务先读测试，再做最小改动。",
                    "score": 1.2,
                    "confidence": 1.0,
                    "tags": ["代码", "测试"],
                    "metadata": {"lanes": ["bm25_raw"]},
                },
                {
                    "doc_type": "book",
                    "source_id": "book:delivery-preference",
                    "text": "代码任务先读测试，再做最小改动并用真实测试交付。",
                    "score": 1.1,
                    "confidence": 1.0,
                    "tags": ["代码", "测试"],
                    "metadata": {
                        "lanes": ["bm25_raw"],
                        "memoryAtomIds": ["atom:delivery-preference"],
                    },
                },
            ],
            query_text="代码任务如何测试交付？",
            max_items=8,
            max_chars=6_400,
        )

        self.assertEqual(
            [item["sourceId"] for item in selected],
            ["book:delivery-preference"],
        )
        self.assertEqual(selected[0]["rank"], 1)
        self.assertEqual(omitted, 1)

    def test_small_book_injects_all_atoms_within_consumer_budget(self) -> None:
        selected, _ = _select_hits(
            [
                {
                    "doc_type": "book",
                    "source_id": "book:input-method",
                    "text": "输入法长期约束。",
                    "score": 1.2,
                    "confidence": 1.0,
                    "tags": ["输入法"],
                    "metadata": {
                        "lanes": ["bm25_raw"],
                        "memoryAtomIds": ["atom:rime", "atom:stale"],
                        "inlineAtomsComplete": True,
                        "inlineAtoms": [
                            {
                                "atomId": "atom:rime",
                                "text": "Rime 候选不能被模型候选重排。",
                            },
                            {
                                "atomId": "atom:stale",
                                "text": "应用切换后必须让旧模型候选失效。",
                            },
                        ],
                    },
                }
            ],
            query_text="输入法候选约束",
            max_items=8,
            max_chars=6_400,
        )

        self.assertIn("Rime 候选不能被模型候选重排", selected[0]["text"])
        self.assertIn("应用切换后必须让旧模型候选失效", selected[0]["text"])

    def test_large_book_keeps_summary_and_leaves_relevant_atom_selectable(self) -> None:
        oversized = "完整事实" * 300
        selected, _ = _select_hits(
            [
                {
                    "doc_type": "book",
                    "source_id": "book:large",
                    "text": "大型主题书摘要。",
                    "score": 1.2,
                    "confidence": 1.0,
                    "tags": ["记忆"],
                    "metadata": {
                        "lanes": ["bm25_raw"],
                        "memoryAtomIds": ["atom:large"],
                        "inlineAtomsComplete": True,
                        "inlineAtoms": [
                            {"atomId": "atom:large", "text": oversized}
                        ],
                    },
                },
                {
                    "doc_type": "atom",
                    "source_id": "atom:large",
                    "text": "当前查询真正相关的独立事实。",
                    "score": 1.1,
                    "confidence": 1.0,
                    "tags": ["记忆"],
                    "metadata": {"lanes": ["bm25_raw"]},
                },
            ],
            query_text="记忆事实",
            max_items=8,
            max_chars=6_400,
        )

        self.assertEqual(
            [item["sourceId"] for item in selected],
            ["book:large", "atom:large"],
        )
        self.assertEqual(selected[0]["text"], "大型主题书摘要。")

    def test_overlapping_small_books_inline_shared_atom_only_once(self) -> None:
        shared = "应用切换后必须让旧模型候选失效。"
        selected, _ = _select_hits(
            [
                {
                    "doc_type": "book",
                    "source_id": "book:input-method",
                    "text": "输入法主题。",
                    "score": 1.2,
                    "confidence": 1.0,
                    "tags": ["输入法"],
                    "metadata": {
                        "lanes": ["bm25_raw"],
                        "memoryAtomIds": ["atom:shared", "atom:rime"],
                        "inlineAtomsComplete": True,
                        "inlineAtoms": [
                            {"atomId": "atom:shared", "text": shared},
                            {
                                "atomId": "atom:rime",
                                "text": "Rime 候选保持原始排序。",
                            },
                        ],
                    },
                },
                {
                    "doc_type": "book",
                    "source_id": "book:context",
                    "text": "上下文稳定性主题。",
                    "score": 1.1,
                    "confidence": 1.0,
                    "tags": ["上下文"],
                    "metadata": {
                        "lanes": ["bm25_raw"],
                        "memoryAtomIds": ["atom:shared", "atom:focus"],
                        "inlineAtomsComplete": True,
                        "inlineAtoms": [
                            {"atomId": "atom:shared", "text": shared},
                            {
                                "atomId": "atom:focus",
                                "text": "焦点变化后必须刷新上下文纪元。",
                            },
                        ],
                    },
                },
            ],
            query_text="输入法上下文稳定性",
            max_items=8,
            max_chars=6_400,
        )

        rendered = " ".join(str(item["text"]) for item in selected)
        self.assertEqual(rendered.count(shared), 1)
        self.assertIn("Rime 候选保持原始排序", rendered)
        self.assertIn("焦点变化后必须刷新上下文纪元", rendered)

    def test_compaction_refresh_injects_task_plan_and_recent_dialogue_without_debug_metadata(self) -> None:
        event_id = self._record_input("压缩后继续完成 Session RAG 上下文")
        provider = HashingEmbeddingProvider(dimensions=16)
        with self.connect() as conn:
            apply_memory_book_plan(
                conn,
                memory_book_plan_from_compile_output(
                    {
                        "memoryAtoms": [
                            {
                                "atomId": "atom:session-rag",
                                "kind": "project_requirement",
                                "canonicalText": "Session 压缩后重新召回与当前任务相关的记忆。",
                                "aliases": ["Session RAG"],
                                "tags": ["Session", "RAG"],
                                "sourceEventIds": [event_id],
                                "confidence": 0.95,
                                "qualityScore": 0.95,
                            }
                        ]
                    },
                    project=PROJECT,
                    provider="test",
                    model="test",
                ),
            )
            rebuild_retrieval_docs(conn, project=PROJECT)
            rebuild_retrieval_doc_vectors(conn, provider, project=PROJECT)

        specification = SessionMemoryRecallBuilder(
            self.db_path,
            project=PROJECT,
            embedding_provider=provider,
        ).build(
            self.session_id,
            role_id="role-a",
            query_text="继续实现 Session RAG",
            trigger="compaction",
            vector_context_text="已经完成召回基础链路，下一步验证压缩刷新",
            vector_context_weight=0.2,
            recent_messages=[
                {"role": "user", "text": "继续完成这个功能"},
                {"role": "assistant", "text": "我已经接通基础召回"},
            ],
            todo_context={
                "items": [
                    {"status": "in_progress", "content": "验证压缩后的上下文刷新"}
                ]
            },
            task_context={
                "kind": "subagent",
                "objective": "完成 Session 压缩后的 RAG 刷新",
                "expectedOutput": "代码与测试",
                "acceptanceCriteria": ["Provider 请求中出现新上下文"],
            },
        )
        payload = specification["payload"]
        self.assertEqual(payload["trigger"], "compaction")
        self.assertEqual(
            payload["retrieval"]["vectorFusion"],
            {"applied": True, "queryWeight": 0.8, "contextWeight": 0.2},
        )
        self.context_runtime.replace_active(**specification)
        rendered = str(self.context_runtime.materialize(self.session_id)["prompt"])
        self.assertIn("## 当前任务", rendered)
        self.assertIn("完成 Session 压缩后的 RAG 刷新", rendered)
        self.assertIn("## 当前 Todo", rendered)
        self.assertIn("验证压缩后的上下文刷新", rendered)
        self.assertIn("## 最近对话", rendered)
        self.assertIn("我已经接通基础召回", rendered)
        self.assertIn("Session 压缩后重新召回", rendered)
        self.assertNotIn("vectorFusion", rendered)
        self.assertNotIn("score=", rendered)
        self.assertNotIn("sourceId", rendered)
        self.assertNotIn("recallId", rendered)
        self.assertNotIn("ownerId", rendered)
        self.assertNotIn("rawScores", rendered)
        self.assertNotIn("sha256", rendered)
        self.assertIn("atom:session-rag", rendered)

    def test_topic_book_quota_prefers_query_tags_and_vector_relevance(self) -> None:
        def book(
            source_id: str,
            *,
            tags: list[str],
            vector_raw: float,
            score: float,
        ) -> dict[str, object]:
            return {
                "doc_type": "book",
                "source_id": source_id,
                "text": source_id,
                "score": score,
                "confidence": 1.0,
                "tags": tags,
                "metadata": {
                    "lanes": ["bm25_raw", "vector_raw"],
                    "rawScores": {"vector_raw": vector_raw},
                },
            }

        selected, _ = _select_hits(
            [
                book(
                    "book:candidate-ui",
                    tags=["候选展示", "UI"],
                    vector_raw=0.35,
                    score=1.2,
                ),
                book(
                    "book:memory-rag",
                    tags=["记忆", "RAG"],
                    vector_raw=0.55,
                    score=1.0,
                ),
                book(
                    "book:pi-gateway",
                    tags=["PI", "网关型Agent"],
                    vector_raw=0.39,
                    score=0.98,
                ),
            ],
            query_text="Pi Runtime 如何注入记忆 RAG？",
            max_items=8,
            max_chars=6_400,
        )

        self.assertEqual(
            [item["sourceId"] for item in selected],
            ["book:memory-rag", "book:pi-gateway"],
        )

    def _record_input(self, text: str) -> int:
        memory_id = self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=now_ms(),
                source="manual",
                committed_text=text,
                privacy_disposition="allowed",
                project=PROJECT,
            )
        )
        return int(memory_id.split(":", 1)[1])


class _BrokenEmbeddingProvider:
    fingerprint = "local-hash:16:v1"

    def embed(self, _text: str) -> list[float]:
        raise RuntimeError("embedding endpoint unavailable")

    def embed_many(self, _texts: list[str], *, batch_size: int = 32) -> list[list[float]]:
        del batch_size
        raise RuntimeError("embedding endpoint unavailable")


if __name__ == "__main__":
    unittest.main()
