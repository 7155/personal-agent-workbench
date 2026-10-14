from __future__ import annotations

from dataclasses import replace
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from rag_ime.adapter import InputMethodAdapter
from rag_ime.core_client import FixtureCoreClient
from rag_ime.local_sqlite_core import LocalSqliteCoreClient
from rag_ime.memory_ingest import normalize_text, upsert_memory_item
from rag_ime.predictor import PredictionProvider
from rag_ime.rime_sidecar import build_rime_sidecar_response, record_rime_side_candidate_selection


class _NoopPredictionProvider(PredictionProvider):
    def predict(self, *, current_input: str, recent_context: str = "", max_candidates: int = 3, latency_budget_ms: int = 150):
        del current_input, recent_context, max_candidates, latency_budget_ms
        return []


class _TrackingOptimizerCore(FixtureCoreClient):
    def __init__(self):
        super().__init__()
        self.optimize_calls: list[dict[str, object]] = []

    def optimize_memory_candidates(self, context, base_hits, *, top_k: int, latency_budget_ms: int):
        self.optimize_calls.append(
            {
                "semanticQuery": context.semantic_query,
                "inputMode": context.input_mode,
                "topK": top_k,
                "latencyBudgetMs": latency_budget_ms,
                "baseHitIds": [item.id for item in base_hits],
            }
        )
        return super().optimize_memory_candidates(
            context,
            base_hits,
            top_k=top_k,
            latency_budget_ms=latency_budget_ms,
        )


class _FailingOptimizerCore(FixtureCoreClient):
    def optimize_memory_candidates(self, context, base_hits, *, top_k: int, latency_budget_ms: int):
        del context, base_hits, top_k, latency_budget_ms
        raise RuntimeError("simulated optimizer failure")


class _TraceWriteFailingCore(FixtureCoreClient):
    def optimize_memory_candidates(self, context, base_hits, *, top_k: int, latency_budget_ms: int):
        result = super().optimize_memory_candidates(
            context,
            base_hits,
            top_k=top_k,
            latency_budget_ms=latency_budget_ms,
        )
        return replace(result, trace_id="trace-write-fails")

    def store_memory_optimizer_trace(self, trace):
        del trace
        raise RuntimeError("simulated trace write failure")


class _DegradedOptimizerCore(FixtureCoreClient):
    def optimize_memory_candidates(self, context, base_hits, *, top_k: int, latency_budget_ms: int):
        result = super().optimize_memory_candidates(
            context,
            base_hits,
            top_k=top_k,
            latency_budget_ms=latency_budget_ms,
        )
        return replace(result, trace_id="trace-degraded", latency_ms=float(latency_budget_ms + 1), degraded=True)


class _FeedbackWriteFailingCore(FixtureCoreClient):
    def record_memory_feedback(self, event):
        del event
        raise RuntimeError("simulated feedback write failure")


class MemoryOptimizerSidecarIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original = {key: os.environ.get(key) for key in (
            "RAG_IME_MEMORY_OPTIMIZER",
            "RAG_IME_MEMORY_OPTIMIZER_TRACE",
            "RAG_IME_MEMORY_OPTIMIZER_MAX_MS",
            "RAG_IME_AI_AFTER_COMMIT_ONLY",
            "RAG_IME_ENABLE_PINYIN_CONSTRAINED_MODEL",
        )}
        os.environ["RAG_IME_AI_AFTER_COMMIT_ONLY"] = "0"
        os.environ["RAG_IME_ENABLE_PINYIN_CONSTRAINED_MODEL"] = "1"

    def tearDown(self) -> None:
        for key, value in self.original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_sidecar_reports_optimizer_enabled_by_default(self) -> None:
        os.environ.pop("RAG_IME_MEMORY_OPTIMIZER", None)
        os.environ.pop("RAG_IME_MEMORY_OPTIMIZER_TRACE", None)
        response = build_rime_sidecar_response(
            payload={
                "sessionId": "optimizer-off",
                "requestSeq": 1,
                "privacyDisposition": "allowed",
                "forceSideCandidates": True,
                "committedContext": "我想做一个输入法",
                "maxVisibleCandidates": 4,
                "maxSideCandidates": 2,
                "rimeContext": {"candidates": [{"label": "1", "text": "输入法", "comment": "rime"}]},
            },
            adapter=InputMethodAdapter(FixtureCoreClient()),
            core=FixtureCoreClient(),
            predictor=_NoopPredictionProvider(),
        )

        self.assertIn("memoryOptimizer", response["ragLane"])
        self.assertTrue(response["ragLane"]["memoryOptimizer"]["enabled"])
        self.assertFalse(response["ragLane"]["memoryOptimizer"]["traceEnabled"])

    def test_sidecar_allows_explicit_optimizer_disable(self) -> None:
        os.environ["RAG_IME_MEMORY_OPTIMIZER"] = "0"
        os.environ.pop("RAG_IME_MEMORY_OPTIMIZER_TRACE", None)
        response = build_rime_sidecar_response(
            payload={
                "sessionId": "optimizer-off",
                "requestSeq": 11,
                "privacyDisposition": "allowed",
                "forceSideCandidates": True,
                "committedContext": "我想做一个输入法",
                "maxVisibleCandidates": 4,
                "maxSideCandidates": 2,
                "rimeContext": {"candidates": [{"label": "1", "text": "输入法", "comment": "rime"}]},
            },
            adapter=InputMethodAdapter(FixtureCoreClient()),
            core=FixtureCoreClient(),
            predictor=_NoopPredictionProvider(),
        )

        self.assertIn("memoryOptimizer", response["ragLane"])
        self.assertFalse(response["ragLane"]["memoryOptimizer"]["enabled"])

    def test_sidecar_reports_context_and_query_plan_when_optimizer_trace_enabled(self) -> None:
        os.environ["RAG_IME_MEMORY_OPTIMIZER"] = "1"
        os.environ["RAG_IME_MEMORY_OPTIMIZER_TRACE"] = "1"
        os.environ["RAG_IME_MEMORY_OPTIMIZER_MAX_MS"] = "12"
        core = FixtureCoreClient()
        response = build_rime_sidecar_response(
            payload={
                "sessionId": "optimizer-on",
                "requestSeq": 2,
                "privacyDisposition": "allowed",
                "forceSideCandidates": True,
                "rawInput": "sj",
                "preedit": "sj",
                "committedContext": "我想设计一个输入法",
                "maxVisibleCandidates": 4,
                "maxSideCandidates": 2,
                "rimeContext": {"candidates": [{"label": "1", "text": "设计", "comment": "rime"}]},
            },
            adapter=InputMethodAdapter(core),
            core=core,
            predictor=_NoopPredictionProvider(),
        )

        optimizer = response["ragLane"]["memoryOptimizer"]
        self.assertTrue(optimizer["enabled"])
        self.assertEqual(optimizer["maxMs"], 12)
        self.assertEqual(optimizer["contextFrame"]["semantic_query_source"], "rime_candidate")
        self.assertIn("phrase", optimizer["queryPlan"]["retrievers"])

    def test_sidecar_delegates_optimizer_call_through_core_client(self) -> None:
        os.environ["RAG_IME_MEMORY_OPTIMIZER"] = "1"
        os.environ["RAG_IME_MEMORY_OPTIMIZER_TRACE"] = "1"
        core = _TrackingOptimizerCore()
        response = build_rime_sidecar_response(
            payload={
                "sessionId": "optimizer-delegate",
                "requestSeq": 21,
                "privacyDisposition": "allowed",
                "forceSideCandidates": True,
                "rawInput": "sj",
                "preedit": "sj",
                "committedContext": "我想设计一个输入法",
                "maxVisibleCandidates": 4,
                "maxSideCandidates": 2,
                "rimeContext": {"candidates": [{"label": "1", "text": "设计", "comment": "rime"}]},
            },
            adapter=InputMethodAdapter(core),
            core=core,
            predictor=_NoopPredictionProvider(),
        )

        self.assertTrue(response["ragLane"]["memoryOptimizer"]["enabled"])
        self.assertEqual(len(core.optimize_calls), 1)
        self.assertIn("设计", str(core.optimize_calls[0]["semanticQuery"]))
        self.assertNotEqual(core.optimize_calls[0]["semanticQuery"], "sj")
        self.assertEqual(core.optimize_calls[0]["inputMode"], "pinyin_composition")
        self.assertEqual(core.optimize_calls[0]["topK"], 2)
        self.assertTrue(core.optimize_calls[0]["baseHitIds"])

    def test_sidecar_fail_closes_when_optimizer_core_raises(self) -> None:
        os.environ["RAG_IME_MEMORY_OPTIMIZER"] = "1"
        os.environ["RAG_IME_MEMORY_OPTIMIZER_TRACE"] = "1"
        core = _FailingOptimizerCore()
        response = build_rime_sidecar_response(
            payload={
                "sessionId": "optimizer-fail-closed",
                "requestSeq": 29,
                "privacyDisposition": "allowed",
                "forceSideCandidates": True,
                "committedContext": "我想设计一个输入法",
                "maxVisibleCandidates": 4,
                "maxSideCandidates": 2,
                "rimeContext": {"candidates": [{"label": "1", "text": "设计", "comment": "rime"}]},
            },
            adapter=InputMethodAdapter(core),
            core=core,
            predictor=_NoopPredictionProvider(),
        )

        optimizer = response["ragLane"]["memoryOptimizer"]
        self.assertTrue(optimizer["enabled"])
        self.assertTrue(optimizer["degraded"])
        self.assertTrue(optimizer["failClosed"])
        self.assertIn("optimizer_exception:RuntimeError", optimizer["warnings"])
        self.assertEqual(response["ragCandidates"], [])

    def test_sidecar_ignores_optimizer_trace_write_failure(self) -> None:
        os.environ["RAG_IME_MEMORY_OPTIMIZER"] = "1"
        os.environ["RAG_IME_MEMORY_OPTIMIZER_TRACE"] = "1"
        core = _TraceWriteFailingCore()
        response = build_rime_sidecar_response(
            payload={
                "sessionId": "optimizer-trace-write-fail",
                "requestSeq": 30,
                "privacyDisposition": "allowed",
                "forceSideCandidates": True,
                "committedContext": "我想设计一个输入法",
                "maxVisibleCandidates": 4,
                "maxSideCandidates": 2,
                "rimeContext": {"candidates": [{"label": "1", "text": "设计", "comment": "rime"}]},
            },
            adapter=InputMethodAdapter(core),
            core=core,
            predictor=_NoopPredictionProvider(),
        )

        optimizer = response["ragLane"]["memoryOptimizer"]
        self.assertTrue(optimizer["enabled"])
        self.assertEqual(optimizer["traceId"], "trace-write-fails")
        self.assertTrue(response["ragCandidates"])

    def test_sidecar_fail_closes_when_optimizer_returns_degraded(self) -> None:
        os.environ["RAG_IME_MEMORY_OPTIMIZER"] = "1"
        os.environ["RAG_IME_MEMORY_OPTIMIZER_TRACE"] = "1"
        core = _DegradedOptimizerCore()
        response = build_rime_sidecar_response(
            payload={
                "sessionId": "optimizer-degraded",
                "requestSeq": 32,
                "privacyDisposition": "allowed",
                "forceSideCandidates": True,
                "committedContext": "我想设计一个输入法",
                "maxVisibleCandidates": 4,
                "maxSideCandidates": 2,
                "rimeContext": {"candidates": [{"label": "1", "text": "设计", "comment": "rime"}]},
            },
            adapter=InputMethodAdapter(core),
            core=core,
            predictor=_NoopPredictionProvider(),
        )

        optimizer = response["ragLane"]["memoryOptimizer"]
        self.assertTrue(optimizer["enabled"])
        self.assertEqual(optimizer["traceId"], "trace-degraded")
        self.assertTrue(optimizer["degraded"])
        self.assertTrue(optimizer["failClosed"])
        self.assertIn("optimizer_degraded_timeout", optimizer["warnings"])
        self.assertEqual(response["ragCandidates"], [])

    def test_sidecar_display_feedback_write_failure_does_not_block_suggestions(self) -> None:
        os.environ["RAG_IME_MEMORY_OPTIMIZER"] = "1"
        os.environ["RAG_IME_MEMORY_OPTIMIZER_TRACE"] = "1"
        core = _FeedbackWriteFailingCore()
        response = build_rime_sidecar_response(
            payload={
                "sessionId": "optimizer-feedback-write-fail",
                "requestSeq": 33,
                "privacyDisposition": "allowed",
                "forceSideCandidates": True,
                "committedContext": "我想设计一个输入法",
                "maxVisibleCandidates": 4,
                "maxSideCandidates": 2,
                "rimeContext": {"candidates": [{"label": "1", "text": "设计", "comment": "rime"}]},
            },
            adapter=InputMethodAdapter(core),
            core=core,
            predictor=_NoopPredictionProvider(),
        )

        self.assertEqual(response["schemaVersion"], "rag-ime.rime-sidecar.v1")
        self.assertTrue(response["ragCandidates"])

    def test_selection_feedback_write_failure_does_not_block_commit(self) -> None:
        core = _FeedbackWriteFailingCore()
        result = record_rime_side_candidate_selection(
            payload={
                "sessionId": "selection-feedback-write-fail",
                "requestSeq": 34,
                "privacyDisposition": "allowed",
                "project": "wisdom-weasel-rag-ime",
                "query": "连续",
                "committedContext": "我们继续写 RAG 输入法",
                "candidate": {
                    "text": "连续预测",
                    "insertText": "连续预测",
                    "sourceType": "memory",
                    "memoryId": "mem-feedback",
                    "selectionKey": "2",
                    "selectionRank": 2,
                },
                "shownCandidates": [
                    {
                        "text": "候选排序",
                        "sourceType": "memory",
                        "memoryId": "mem-local-privacy",
                        "selectionKey": "1",
                        "selectionRank": 1,
                    },
                    {
                        "text": "连续预测",
                        "sourceType": "memory",
                        "memoryId": "mem-feedback",
                        "selectionKey": "2",
                        "selectionRank": 2,
                    },
                ],
            },
            adapter=InputMethodAdapter(core),
            core=core,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["insertText"], "连续预测")

    def test_realtime_sidecar_does_not_call_deepseek_v4_or_http_when_optimizer_enabled(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-no-cloud-realtime-") as tmp:
            env_path = Path(tmp) / "deepseek.env"
            env_path.write_text(
                "\n".join(
                    [
                        "RAG_IME_DEEPSEEK_BASE_URL=https://api.example.com/v1",
                        "DEEPSEEK_API_KEY=secret-value",
                        "RAG_IME_DEEPSEEK_MODEL=deepseek-v4-flash",
                    ]
                ),
                encoding="utf-8",
            )
            core = LocalSqliteCoreClient(Path(tmp) / "rag-ime.sqlite")
            core.initialize()
            adapter = InputMethodAdapter(core)
            adapter.commit_text(
                "本地检索优先",
                recent_context="输入法 RAG 和记忆优化需要保持 local-first",
                project="wisdom-weasel-rag-ime",
                privacy_disposition="allowed",
                tags=("phrase-memory",),
            )

            with patch.dict(
                os.environ,
                {
                    "RAG_IME_MEMORY_OPTIMIZER": "1",
                    "RAG_IME_MEMORY_OPTIMIZER_TRACE": "1",
                    "RAG_IME_MODEL_ENV": str(env_path),
                    "RAG_IME_DEEPSEEK_BASE_URL": "https://api.example.com/v1",
                    "DEEPSEEK_API_KEY": "secret-value",
                    "RAG_IME_DEEPSEEK_MODEL": "deepseek-v4-flash",
                },
                clear=False,
            ), patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("/rime-suggest must not make external HTTP calls"),
            ), patch(
                "rag_ime.memory_generator.DeepSeekMemoryGenerator.from_env_path",
                side_effect=AssertionError("/rime-suggest must not load the DeepSeek V4 generator"),
            ), patch(
                "rag_ime.memory_compiler.DeepSeekMemoryGenerator.from_env_path",
                side_effect=AssertionError("/rime-suggest must not load the offline compiler"),
            ):
                response = build_rime_sidecar_response(
                    payload={
                        "sessionId": "optimizer-no-cloud",
                        "requestSeq": 31,
                        "privacyDisposition": "allowed",
                        "forceSideCandidates": True,
                        "committedContext": "我想优化 RAG 和记忆候选",
                        "maxVisibleCandidates": 4,
                        "maxSideCandidates": 2,
                        "rimeContext": {"candidates": [{"label": "1", "text": "优化", "comment": "rime"}]},
                    },
                    adapter=adapter,
                    core=core,
                    predictor=_NoopPredictionProvider(),
                )

        self.assertTrue(response["ragLane"]["memoryOptimizer"]["enabled"])
        self.assertEqual(response["modelLane"]["predictionCount"], 0)

    def test_optimizer_blocks_suppressed_memory_candidate_when_enabled(self) -> None:
        os.environ["RAG_IME_MEMORY_OPTIMIZER"] = "1"
        os.environ["RAG_IME_MEMORY_OPTIMIZER_TRACE"] = "1"
        with tempfile.TemporaryDirectory(prefix="rag-ime-optimizer-suppression-") as tmp:
            core = LocalSqliteCoreClient(Path(tmp) / "rag-ime.sqlite")
            core.initialize()
            adapter = InputMethodAdapter(core)
            adapter.commit_text(
                "连续预测",
                recent_context="RAG 输入法需要更好的候选",
                project="wisdom-weasel-rag-ime",
                privacy_disposition="allowed",
                tags=("phrase-memory",),
            )
            with closing(sqlite3.connect(core.db_path)) as conn, conn:
                conn.execute(
                    """
                    INSERT INTO memory_candidate_suppressions(id, match_type, match_value, action, reason, strength, created_at_ms)
                    VALUES (?, 'memory_id', ?, 'block', 'manual test suppression', 1.0, 1)
                    """,
                    ("suppression-test-1", "event:1"),
                )
                conn.commit()
            response = build_rime_sidecar_response(
                payload={
                    "sessionId": "optimizer-suppressed",
                    "requestSeq": 3,
                    "privacyDisposition": "allowed",
                    "forceSideCandidates": True,
                    "committedContext": "我想继续写连续",
                    "maxVisibleCandidates": 4,
                    "maxSideCandidates": 2,
                    "rimeContext": {"candidates": [{"label": "1", "text": "连续", "comment": "rime"}]},
                },
                adapter=adapter,
                core=core,
                predictor=_NoopPredictionProvider(),
            )
            governance = core.optimizer_governance_snapshot(
                memory_ids=["event:1", "phrase:连续预测"],
                texts=["连续预测"],
                source_event_ids=[1],
                project="wisdom-weasel-rag-ime",
            )

        self.assertEqual(response["ragCandidates"], [])
        self.assertIn("event:1", governance["suppressedMemoryIds"])
        self.assertIn("连续预测", governance["suppressedTexts"])

    def test_sidecar_persists_optimizer_trace_and_candidate_explanation(self) -> None:
        os.environ["RAG_IME_MEMORY_OPTIMIZER"] = "1"
        os.environ["RAG_IME_MEMORY_OPTIMIZER_TRACE"] = "1"
        with tempfile.TemporaryDirectory(prefix="rag-ime-optimizer-trace-") as tmp:
            core = LocalSqliteCoreClient(Path(tmp) / "rag-ime.sqlite")
            core.initialize()
            adapter = InputMethodAdapter(core)
            adapter.commit_text(
                "连续预测",
                recent_context="RAG 输入法需要更好的候选",
                project="wisdom-weasel-rag-ime",
                privacy_disposition="allowed",
                tags=("phrase-memory",),
            )
            with core._connect() as conn:
                upsert_memory_item(
                    conn,
                    memory_id="phrase:连续预测",
                    kind="phrase",
                    text="连续预测",
                    normalized_text=normalize_text("连续预测"),
                    summary="DSV4 compiled phrase",
                    source_event_id=1,
                    project="wisdom-weasel-rag-ime",
                    app="",
                    confidence=0.85,
                    quality_score=0.85,
                    status="approved",
                    privacy_class="local",
                    created_at_ms=1_900_000_200_000,
                    updated_at_ms=1_900_000_200_000,
                    metadata={"direct_candidate_allowed": True, "source": "memory_book_compile"},
                    tags=("输入法",),
                    embedding_provider=core.embedding_provider,
                    tag_source="dsv4",
                )
            response = build_rime_sidecar_response(
                payload={
                    "sessionId": "optimizer-trace",
                    "requestSeq": 4,
                    "privacyDisposition": "allowed",
                    "forceSideCandidates": True,
                    "committedContext": "我想继续写连续",
                    "maxVisibleCandidates": 4,
                    "maxSideCandidates": 2,
                    "rimeContext": {"candidates": [{"label": "1", "text": "连续", "comment": "rime"}]},
                },
                adapter=adapter,
                core=core,
                predictor=_NoopPredictionProvider(),
            )
            optimizer = response["ragLane"]["memoryOptimizer"]
            trace_id = optimizer["traceId"]
            trace = core.get_memory_optimizer_trace(trace_id)
            explanation = core.explain_memory_candidate(
                "phrase:连续预测",
                context_hash=response["committedContextHash"],
            )
            self.assertIsNotNone(trace)
            assert trace is not None
            self.assertEqual(trace["traceId"], trace_id)
            self.assertTrue(any(item["id"] == "phrase:连续预测" for item in trace["rawResults"]))
            self.assertTrue(any(item["reason"] == "recent_committed_echo" for item in trace["blocked"]))
            self.assertIsNotNone(explanation)
            assert explanation is not None
            self.assertEqual(explanation["candidateId"], "phrase:连续预测")
            self.assertEqual(explanation["recentTrace"]["traceId"], trace_id)
            self.assertTrue(any(item["reason"] == "recent_committed_echo" for item in explanation["recentTrace"]["blocked"]))

    def test_tag_graph_candidate_explanation_links_trace_and_activation(self) -> None:
        os.environ["RAG_IME_MEMORY_OPTIMIZER"] = "1"
        os.environ["RAG_IME_MEMORY_OPTIMIZER_TRACE"] = "1"
        with tempfile.TemporaryDirectory(prefix="rag-ime-tag-explain-") as tmp:
            core = LocalSqliteCoreClient(Path(tmp) / "rag-ime.sqlite")
            core.initialize()
            created_at = 1_900_000_200_001
            with core._connect() as conn:
                upsert_memory_item(
                    conn,
                    memory_id="stable:tag-seed",
                    kind="stable_memory",
                    text="输入法基础入口",
                    normalized_text=normalize_text("输入法基础入口"),
                    summary="seed memory",
                    source_event_id=None,
                    project="wisdom-weasel-rag-ime",
                    app="",
                    confidence=0.9,
                    quality_score=0.9,
                    status="approved",
                    privacy_class="local",
                    created_at_ms=created_at,
                    updated_at_ms=created_at,
                    metadata={"direct_candidate_allowed": True},
                    tags=("输入法", "候选排序"),
                    embedding_provider=core.embedding_provider,
                    tag_source="dsv4",
                )
                upsert_memory_item(
                    conn,
                    memory_id="stable:tag-related",
                    kind="stable_memory",
                    text="候选排序能量传播",
                    normalized_text=normalize_text("候选排序能量传播"),
                    summary="related through tag graph",
                    source_event_id=None,
                    project="wisdom-weasel-rag-ime",
                    app="",
                    confidence=0.9,
                    quality_score=0.9,
                    status="approved",
                    privacy_class="local",
                    created_at_ms=created_at + 1,
                    updated_at_ms=created_at + 1,
                    metadata={"direct_candidate_allowed": True},
                    tags=("候选排序", "能量传播"),
                    embedding_provider=core.embedding_provider,
                    tag_source="dsv4",
                )
                input_method_tag = int(conn.execute("SELECT id FROM memory_tags WHERE tag = '输入法'").fetchone()[0])
                ranking_tag = int(conn.execute("SELECT id FROM memory_tags WHERE tag = '候选排序'").fetchone()[0])
                conn.execute(
                    """
                    INSERT INTO memory_tag_edges(
                        src_tag_id, dst_tag_id, edge_type, weight, direction_bias,
                        evidence_count, updated_at_ms, metadata_json
                    ) VALUES (?, ?, 'related', 0.9, 0.0, 2, ?, '{"source":"dsv4"}')
                    """,
                    (input_method_tag, ranking_tag, created_at + 2),
                )
            core.recompute_memory_tags(project="wisdom-weasel-rag-ime")

            response = build_rime_sidecar_response(
                payload={
                    "sessionId": "optimizer-tag-explain",
                    "requestSeq": 35,
                    "privacyDisposition": "allowed",
                    "forceSideCandidates": True,
                    "rawInput": "shurufa",
                    "preedit": "shurufa",
                    "committedContext": "我们讨论",
                    "maxVisibleCandidates": 4,
                    "maxSideCandidates": 2,
                    "rimeContext": {"candidates": [{"label": "1", "text": "输入法", "comment": "rime"}]},
                },
                adapter=InputMethodAdapter(core),
                core=core,
                predictor=_NoopPredictionProvider(),
            )
            explanation = core.explain_memory_candidate(
                "stable:tag-related",
                context_hash=response["committedContextHash"],
            )

        candidate_texts = [
            item.get("surfaceText") or item.get("text") or item.get("insertText")
            for item in response["ragCandidates"]
        ]
        self.assertIn("候选排序能量传播", candidate_texts)
        self.assertIsNotNone(explanation)
        assert explanation is not None
        recent_trace = explanation["recentTrace"]
        self.assertEqual(recent_trace["traceId"], response["ragLane"]["memoryOptimizer"]["traceId"])
        self.assertIn("tag_graph", recent_trace["queryPlan"]["retrievers"])
        self.assertIn("输入法", recent_trace["contextFrame"]["active_tags"])
        optimized = [item for item in recent_trace["optimizedCandidates"] if item["id"] == "stable:tag-related"]
        raw_related = [item for item in recent_trace["rawResults"] if item["id"] == "stable:tag-related"]
        self.assertTrue(optimized)
        self.assertTrue(raw_related)
        self.assertIn("候选排序", optimized[0]["tags"])
        self.assertIn("tag:", raw_related[0]["metadata"]["reason"])
        self.assertGreater(raw_related[0]["metadata"]["score_breakdown"]["components"]["tag"], 0)

    def test_repeated_skips_create_optimizer_cooldown_suppression(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-optimizer-feedback-") as tmp:
            core = LocalSqliteCoreClient(Path(tmp) / "rag-ime.sqlite")
            core.initialize()
            core.record_memory_feedback(
                {
                    "event": "skipped",
                    "candidateId": "phrase:连续预测",
                    "candidateText": "连续预测",
                    "sourceType": "memory",
                    "contextHash": "ctx:selection-feedback",
                    "timestampMs": 1,
                }
            )
            core.record_memory_feedback(
                {
                    "event": "skipped",
                    "candidateId": "phrase:连续预测",
                    "candidateText": "连续预测",
                    "sourceType": "memory",
                    "contextHash": "ctx:selection-feedback",
                    "timestampMs": 2,
                }
            )

            governance = core.optimizer_governance_snapshot(
                memory_ids=["phrase:连续预测"],
                texts=["连续预测"],
            )
            with closing(sqlite3.connect(core.db_path)) as conn, conn:
                feedback_rows = conn.execute(
                    """
                    SELECT action, candidate_id
                    FROM memory_feedback_events
                    ORDER BY created_at_ms
                    """
                ).fetchall()

        self.assertEqual(feedback_rows, [("skipped", "phrase:连续预测"), ("skipped", "phrase:连续预测")])
        self.assertIn("phrase:连续预测", governance["suppressedMemoryIds"])
