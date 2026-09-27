from __future__ import annotations

from collections.abc import Mapping

import base64
import hashlib
import tempfile
import threading
import unittest
import json
import os
import sqlite3
import subprocess
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from rag_ime.agent_runtime_driver import AgentRuntimeError
from rag_ime.agent_command_receipts import (
    AgentCommandReceiptConflict,
    AgentCommandReceiptFailed,
    AgentCommandReceiptPending,
)
from rag_ime.debug_server import (
    DebugImeService,
    DebugRequestHandler,
    DebugServerConfig,
    QuietThreadingHTTPServer,
)
from rag_ime.agent_workspace import WorkspaceSnapshotError
from rag_ime.predictor_latency import PredictorLatencyTrace, append_latency_trace
from rag_ime.local_sqlite_core import LocalSqliteCoreClient
from rag_ime.knowledge_workbench import KnowledgeGenerationResult, KnowledgeWorkbenchRequest
from rag_ime.memory_ingest import normalize_text, upsert_memory_item
from rag_ime.memory_evidence_admission import transition_evidence_admission
from rag_ime.memory_maintenance_settings import MemoryMaintenanceSettings
from rag_ime.memory_book_compiler import (
    apply_memory_book_plan,
    build_memory_book_source_bundle,
    memory_book_plan_from_compile_output,
    store_memory_book_plan,
)
from rag_ime.management_service import page_request
from rag_ime.models import InputEvent, MemoryAction, ModelPrediction
from rag_ime.predictor import OpenAICompatiblePredictionConfig
from rag_ime.retrieval_docs import rebuild_retrieval_docs
from rag_ime.rime_lexicon_review import CONFIRM_TEXT
from rag_ime.rime_rank_export import record_rime_rank_feedback


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _capture_v2(
    text: str,
    *,
    capture_id: str,
    app: str = "com.apple.TextEdit",
    occurred_at_ms: int | None = None,
) -> dict[str, object]:
    timestamp = int(occurred_at_ms or time.time() * 1_000)
    return {
        "schemaVersion": "rag-ime.input-capture.v2",
        "captureId": capture_id,
        "transactionId": f"transaction:{capture_id}",
        "sequence": 1,
        "channel": "input_method",
        "boundaryKind": "host_return",
        "boundaryConfidence": "strong",
        "nativeCompositionBefore": False,
        "rimeHandled": False,
        "hostForwarded": True,
        "modifiedReturn": False,
        "finalCommitted": True,
        "controllerEpoch": 1,
        "focusEpoch": 1,
        "appBundleId": app,
        "fieldIdentitySha256": hashlib.sha256(
            f"field:{capture_id}".encode("utf-8")
        ).hexdigest(),
        "privacyRevision": "foreground-privacy.v1",
        "occurredStartMs": timestamp,
        "occurredEndMs": timestamp + 1,
        "contentSha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "captureSource": "text_input_client",
        "fallbackReason": "",
        "fieldContextChars": len(text),
        "imeBufferChars": len(text),
        "selectionRule": "final_committed_segment",
    }


class _ManagementPredictionProvider:
    def predict(self, *, current_input: str, recent_context: str = "", max_candidates: int = 5):
        text = f"{current_input}候选" if current_input else "继续完善候选栏展示"
        return [
            ModelPrediction(
                text=text,
                rank=1,
                provider_name="test-local-model",
                latency_ms=3,
                confidence=0.82,
                metadata={"prompt": recent_context, "reason": "test"},
            )
        ][:max_candidates]


class _CapabilityProbePredictionProvider(_ManagementPredictionProvider):
    config = OpenAICompatiblePredictionConfig(
        base_url="http://127.0.0.1:8767",
        model="probe-model",
        provider_name="local-mlx",
        profile="qwen3_06b_ime_hot",
    )

    def __init__(self) -> None:
        self.probe_calls = 0

    def capability_probe(self) -> dict[str, object]:
        self.probe_calls += 1
        return {
            "ok": True,
            "providerName": "local-mlx",
            "model": "/models/minimind-ime-v3",
            "modelFingerprint": "sha256:" + "b" * 64,
            "capabilities": {"streaming": True, "serverTiming": True},
            "promptCache": {"enabled": False},
        }


class _WarmupEmbeddingProvider:
    fingerprint = "mlx-bert:test-q8:cfg-test"

    def __init__(self) -> None:
        self.queries: list[str] = []

    def embed(self, text: str) -> list[float]:
        return self.embed_query(text)

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return [0.25, 0.75]


class _KnowledgeGenerator:
    ready = True

    def generate(self, request, *, evidence, notion_answer="", notion_sources=None):
        _ = request, evidence, notion_sources
        return KnowledgeGenerationResult(
            answer="本地知识答案" if not notion_answer else f"已合并：{notion_answer}",
            elapsed_ms=5,
            model="deepseek-test",
            prompt_diagnostics={"success": True, "notionIncluded": bool(notion_answer)},
        )


class DebugManagementApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._pinyin_env_keys = (
            "RAG_IME_PINYIN_FUZZY_ENABLED",
            "RAG_IME_PINYIN_FUZZY_PROFILE",
            "RAG_IME_PINYIN_FUZZY_Z_ZH",
            "RAG_IME_PINYIN_FUZZY_C_CH",
            "RAG_IME_PINYIN_FUZZY_S_SH",
            "RAG_IME_PINYIN_FUZZY_EN_ENG",
            "RAG_IME_PINYIN_FUZZY_IN_ING",
            "RAG_IME_PINYIN_FUZZY_ONG_ON",
            "RAG_IME_PINYIN_FUZZY_N_L",
            "RAG_IME_PINYIN_FUZZY_F_H",
        )
        self._pinyin_env = {key: os.environ.get(key) for key in self._pinyin_env_keys}
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-debug-management-")
        self.db_path = Path(self.tmp.name) / "rag-ime.sqlite"
        self.core = LocalSqliteCoreClient(self.db_path)
        self.core.initialize()
        self.service = DebugImeService(
            DebugServerConfig(
                db_path=self.db_path,
                seed_if_empty=False,
                predictor=_ManagementPredictionProvider(),
                rime_user_dir=Path(self.tmp.name) / "Rime",
                rime_lexicon_backup_root=Path(self.tmp.name) / "LexiconBackups",
            )
        )

    def tearDown(self) -> None:
        self.service.agent.close()
        for key, value in self._pinyin_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def _compile_phrase(self, event_ref: str, text: str, *, tags: tuple[str, ...] = ()) -> None:
        event_id = int(event_ref.split(":", 1)[1])
        plan = memory_book_plan_from_compile_output(
            {
                "phraseCandidates": [
                    {"text": text, "tags": list(tags), "sourceEventIds": [event_id], "weight": 0.8}
                ]
            },
            project="wisdom-weasel-rag-ime",
            provider="deepseek",
            model="deepseek-v4-flash",
        )
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            apply_memory_book_plan(conn, plan)

    def test_room_sse_does_not_send_200_before_the_stream_is_readable(self) -> None:
        closed = threading.Event()

        def broken_stream():
            try:
                raise sqlite3.OperationalError("unable to open database file")
                yield b""  # pragma: no cover - keeps this a generator
            finally:
                closed.set()

        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = Path("debug")
        server = QuietThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(
                self.service.agent,
                "subscribe_room_events",
                return_value=broken_stream(),
            ):
                with self.assertRaises(HTTPError) as captured:
                    urlopen(
                        f"http://127.0.0.1:{server.server_port}"
                        "/api/agent/rooms/room%3Asse-failure/events",
                        timeout=5,
                    )
                try:
                    body = json.loads(captured.exception.read().decode("utf-8"))
                finally:
                    captured.exception.close()
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(captured.exception.code, 503)
        self.assertFalse(body["ok"])
        self.assertEqual(body["errorCode"], "event_stream_unavailable")
        self.assertNotIn("unable to open database file", body["error"])
        self.assertTrue(closed.wait(1))

    def test_room_sse_prefetch_preserves_the_first_frame_and_closes_stream(self) -> None:
        closed = threading.Event()

        def finite_stream():
            try:
                yield b": connected\n\n"
                yield b"event: status\ndata: {\"ok\":true}\n\n"
            finally:
                closed.set()

        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = Path("debug")
        server = QuietThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        expected_body = (
            b": connected\n\nevent: status\ndata: {\"ok\":true}\n\n"
        )
        try:
            with patch.object(
                self.service.agent,
                "subscribe_room_events",
                return_value=finite_stream(),
            ):
                with urlopen(
                    f"http://127.0.0.1:{server.server_port}"
                    "/api/agent/rooms/room%3Asse-success/events",
                    timeout=5,
                ) as response:
                    status = response.status
                    content_type = response.headers.get("Content-Type")
                    body = response.read(len(expected_body))
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(status, 200)
        self.assertEqual(content_type, "text/event-stream; charset=utf-8")
        self.assertEqual(body, expected_body)
        self.assertTrue(closed.wait(1))
        self.assertEqual(server.active_event_streams, 0)

    def test_gateway_event_stream_capacity_is_bounded_and_recoverable(self) -> None:
        class Handler(DebugRequestHandler):
            pass

        class OneStreamServer(QuietThreadingHTTPServer):
            max_event_streams = 1

        Handler.service = self.service
        Handler.static_dir = Path("debug")
        server = OneStreamServer(("127.0.0.1", 0), Handler)
        try:
            self.assertTrue(server.acquire_event_stream())
            self.assertEqual(server.active_event_streams, 1)
            self.assertFalse(server.acquire_event_stream())
            server.release_event_stream()
            self.assertEqual(server.active_event_streams, 0)
            self.assertTrue(server.acquire_event_stream())
            server.release_event_stream()
        finally:
            server.server_close()

    def test_plugin_inbox_defaults_to_the_managed_pi_runtime_boundary(self) -> None:
        self.assertEqual(
            self.service.agent_extensions.inbox_root,
            (self.db_path.parent / "Agent" / "plugin-inbox").resolve(),
        )

    def test_debug_service_binds_the_host_owned_vertical_sandbox_connector(self) -> None:
        connector = self.service.agent_tools.sandbox_connector

        self.assertIsNotNone(connector)
        status = connector.status()
        self.assertEqual(status["executionOwner"], "paw_host")
        self.assertEqual(status["connectorOwner"], "pi_package")
        self.assertIn("sgg", {item["suiteId"] for item in status["supportedSuites"]})

    def test_candidate_explain_returns_ranking_reasons(self) -> None:
        event_ref = self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=1_900_000_100_001,
                source="manual",
                committed_text="候选解释需要展示排序原因",
                privacy_disposition="allowed",
                recent_context="RAG 输入法管理界面",
                project="wisdom-weasel-rag-ime",
                tags=("phrase-memory",),
            )
        )

        report = self.service.candidate_explain({"query": "候选解释", "recentContext": "管理界面", "topK": 3})

        self.assertTrue(report["ok"])
        self.assertEqual(report["schemaVersion"], "rag-ime.management-candidate-explain.v1")
        self.assertTrue(report["queryHash"].startswith("sha256:"))
        self.assertGreaterEqual(len(report["candidates"]), 1)
        first = report["candidates"][0]
        self.assertIn("sourceType", first)
        self.assertIn("score", first)
        self.assertIn("reason", first)

    def test_provider_configuration_apply_is_hash_bound_and_never_echoes_secrets(self) -> None:
        support = Path(self.tmp.name) / "ProviderSupport"
        support.mkdir()
        predictor_env = support / "predictor.env"
        predictor_env.write_text(
            "RAG_IME_PREDICTOR_PROVIDER=mlx\n"
            "RAG_IME_PREDICTOR_BASE_URL=http://127.0.0.1:8767\n"
            "RAG_IME_PREDICTOR_MODEL=\n"
            "RAG_IME_PREDICTOR_API_KEY=must-not-leak\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"RAG_IME_APP_SUPPORT_DIR": str(support)}):
            before = self.service.management.provider_configuration()
            applied = self.service.management.provider_configuration_apply(
                {
                    "slot": "instant",
                    "provider": "ollama",
                    "endpoint": "http://127.0.0.1:11434/v1",
                    "model": "qwen3:0.6b",
                    "expectedConfigurationHash": before["configurationHash"],
                }
            )

            self.assertTrue(applied["ok"])
            self.assertEqual(applied["after"]["provider"], "ollama")
            self.assertEqual(applied["restartComponent"], "predictor")
            self.assertTrue(applied["existingSecretPreserved"])
            self.assertNotIn("must-not-leak", str(applied))
            self.assertIn("RAG_IME_PREDICTOR_API_KEY=must-not-leak", predictor_env.read_text())

            current = self.service.management.provider_configuration()
            voice = self.service.management.provider_configuration_apply(
                {
                    "slot": "voice",
                    "provider": "http_transcription",
                    "expectedConfigurationHash": current["configurationHash"],
                }
            )
            self.assertEqual(voice["after"]["provider"], "http_transcription")
            self.assertEqual(voice["restartComponent"], "voice")
            self.assertNotIn("must-not-leak", str(voice))
            self.assertEqual((support / "voice-provider.json").stat().st_mode & 0o777, 0o600)

            with self.assertRaisesRegex(ValueError, "changed after"):
                self.service.management.provider_configuration_apply(
                    {
                        "slot": "instant",
                        "provider": "mlx",
                        "endpoint": "http://127.0.0.1:8767",
                        "model": "",
                        "expectedConfigurationHash": before["configurationHash"],
                    }
                )

    def test_provider_configuration_rejects_endpoint_without_hostname_before_write(self) -> None:
        support = Path(self.tmp.name) / "ProviderValidationSupport"
        support.mkdir()
        with patch.dict(os.environ, {"RAG_IME_APP_SUPPORT_DIR": str(support)}):
            before = self.service.management.provider_configuration()
            with self.assertRaisesRegex(ValueError, "complete http or https URL"):
                self.service.management.provider_configuration_apply(
                    {
                        "slot": "instant",
                        "provider": "openai-compatible",
                        "endpoint": "https://",
                        "model": "local-model",
                        "expectedConfigurationHash": before["configurationHash"],
                    }
                )

        self.assertFalse((support / "predictor.env").exists())

    def test_local_mlx_embedding_warmup_reports_pending_then_complete(self) -> None:
        provider = _WarmupEmbeddingProvider()
        with tempfile.TemporaryDirectory(prefix="rag-ime-embedding-warmup-") as tmp:
            with patch("rag_ime.debug_server.embedding_provider_from_env", return_value=provider):
                service = DebugImeService(
                    DebugServerConfig(
                        db_path=Path(tmp) / "warmup.sqlite",
                        seed_if_empty=False,
                        predictor=_ManagementPredictionProvider(),
                    )
                )
                pending = service.health()["embeddingWarmup"]
                service.start_background_services()
                deadline = time.monotonic() + 2
                while service.health()["embeddingWarmup"]["status"] != "complete":
                    if time.monotonic() >= deadline:
                        self.fail("embedding warmup did not complete")
                    time.sleep(0.01)
                report = service.health()["embeddingWarmup"]
                service.close()

        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["delayMs"], 0)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["delayMs"], 0)
        self.assertTrue(report["enabled"])
        self.assertTrue(report["ok"])
        self.assertEqual(report["dimensions"], 2)
        self.assertEqual(provider.queries, ["输入法语义检索预热"])

    def test_predictor_status_reports_cache_capabilities(self) -> None:
        provider = _CapabilityProbePredictionProvider()
        self.service.predictor = provider

        status = self.service.predictor_status()
        cached = self.service.predictor_status()
        forced = self.service.model_probe({})

        self.assertTrue(status["ok"])
        self.assertIn("predictor", status)
        self.assertIn("capabilities", status["predictor"])
        self.assertEqual(provider.probe_calls, 2)
        self.assertFalse(status["predictor"]["statusCache"]["hit"])
        self.assertTrue(cached["predictor"]["statusCache"]["hit"])
        self.assertFalse(forced["predictor"]["statusCache"]["hit"])

        runtime = self.service.management.runtime_status()
        detail = runtime["components"]["predictor"]["detail"]
        self.assertIn("minimind-ime-v3", detail)
        self.assertIn("bbbbbbbbbbbb", detail)

    def test_pinyin_settings_apply_to_runtime_env_immediately(self) -> None:
        result = self.service.settings_update(
            {
                "pinyin.fuzzyProfile": "none",
                "pinyin.rerankUsesFuzzy": False,
                "pinyin.pairs.sSh": False,
                "pinyin.pairs.nL": True,
            }
        )
        health = self.service.health()

        self.assertTrue(result["ok"])
        self.assertEqual(os.environ["RAG_IME_PINYIN_FUZZY_ENABLED"], "0")
        self.assertEqual(os.environ["RAG_IME_PINYIN_FUZZY_PROFILE"], "none")
        self.assertEqual(os.environ["RAG_IME_PINYIN_FUZZY_S_SH"], "0")
        self.assertEqual(os.environ["RAG_IME_PINYIN_FUZZY_N_L"], "1")
        self.assertFalse(health["pinyinRuntime"]["fuzzyEnabled"])

    def test_predictor_latency_api_redacts_prompt_text(self) -> None:
        trace_path = Path(self.tmp.name) / "predictor-latency.jsonl"
        append_latency_trace(
            {
                "requestId": "req-1",
                "requestType": "ime_hot",
                "firstCandidateMs": 12,
                "totalMs": 18,
                "prompt": "secret prompt",
                "rawText": "secret raw",
            },
            path=trace_path,
        )

        report = self.service.predictor_latency({"log": str(trace_path), "last": 5})

        self.assertTrue(report["ok"])
        self.assertEqual(report["count"], 1)
        self.assertEqual(report["summary"]["firstCandidateMs"]["p50Ms"], 12)

    def test_predictor_benchmark_runs_as_job(self) -> None:
        report = self.service.predictor_benchmark(
            {
                "cases": "eval/predictor_latency_cases.jsonl",
                "profile": "qwen3_06b_ime_hot",
                "repeat": 1,
            }
        )

        self.assertEqual(report["schemaVersion"], "rag-ime.predictor-benchmark.v1")
        self.assertGreaterEqual(report["sampleCount"], 1)

    def test_clear_cache_requires_confirm_text(self) -> None:
        denied = self.service.predictor_cache_clear({"confirmText": "wrong"})
        allowed = self.service.predictor_cache_clear({"confirmText": "CLEAR PREDICTOR CACHE"})

        self.assertFalse(denied["ok"])
        self.assertTrue(allowed["ok"])
        self.assertFalse(allowed["cleared"])

    def test_history_endpoint_redacts_raw_text_by_default_and_tombstone_audits(self) -> None:
        self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=1_900_000_100_010,
                source="manual",
                committed_text="这是一整段旧输入历史不应该默认完整展示",
                privacy_disposition="allowed",
                recent_context="包含用户真实输入上下文",
                project="wisdom-weasel-rag-ime",
                tags=("user-input",),
            )
        )

        history = self.service.management_history({"limit": 5, "project": "wisdom-weasel-rag-ime"})

        self.assertTrue(history["ok"])
        item = history["items"][0]
        self.assertNotIn("text", item)
        self.assertNotIn("recentContext", item)
        self.assertTrue(item["textHash"].startswith("sha256:"))
        self.assertIn("textPreview", item)

        tombstone = self.service.management_history_tombstone({"eventId": 1, "reason": "test tombstone"})

        self.assertTrue(tombstone["ok"])
        self.assertGreater(int(tombstone["auditId"]), 0)
        self.assertEqual(self._audit_count("history_tombstone"), 1)
        governance = self.core.inspect_memory_governance(limit=10)
        self.assertTrue(any(item["targetValue"] == "event:1" for item in governance["tombstones"]))

    def test_history_can_show_raw_text_only_when_debug_flag_enabled(self) -> None:
        self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=1_900_000_100_011,
                source="manual",
                committed_text="只有显式调试开关才展示完整原文",
                privacy_disposition="allowed",
                recent_context="debug raw text",
                project="wisdom-weasel-rag-ime",
            )
        )
        service = DebugImeService(
            DebugServerConfig(
                db_path=self.db_path,
                seed_if_empty=False,
                predictor=_ManagementPredictionProvider(),
                include_raw_text=True,
            )
        )

        history = service.management_history({"limit": 1, "project": "wisdom-weasel-rag-ime"})

        self.assertTrue(history["rawTextVisible"])
        self.assertEqual(history["items"][0]["text"], "只有显式调试开关才展示完整原文")

    def test_memory_and_lexicon_review_actions_update_status_and_audit(self) -> None:
        self._upsert_item(memory_id="stable:pending", kind="stable_memory", text="稳定记忆待审核")
        self._upsert_item(memory_id="phrase:pending", kind="phrase", text="连续预测")

        memories = self.service.management_memories({"status": "pending"})
        lexicon = self.service.management_lexicon({"status": "pending"})

        self.assertTrue(any(item["memoryId"] == "stable:pending" for item in memories["items"]))
        self.assertTrue(any(item["memoryId"] == "phrase:pending" for item in lexicon["items"]))
        self.assertNotIn("text", memories["items"][0])

        approved = self.service.management_memory_action({"memoryId": "stable:pending", "action": "approve"})
        rejected = self.service.management_lexicon_action({"memoryId": "phrase:pending", "action": "reject"})

        self.assertTrue(approved["ok"])
        self.assertTrue(rejected["ok"])
        self.assertEqual(self.core.inspect_memory_v2(status="approved")["items"][0]["memoryId"], "stable:pending")
        self.assertEqual(self.core.inspect_memory_v2(status="rejected")["items"][0]["memoryId"], "phrase:pending")
        self.assertEqual(self._audit_count("memory_approve"), 1)
        self.assertEqual(self._audit_count("lexicon_reject"), 1)

    def test_lexicon_rime_export_is_approved_phrase_dry_run_preview(self) -> None:
        self._upsert_item(memory_id="phrase:approved", kind="phrase", text="连续预测", status="approved")
        self._upsert_item(memory_id="phrase:pending", kind="phrase", text="待审核短语", status="pending")
        self._upsert_item(memory_id="memory:approved", kind="stable_memory", text="稳定记忆", status="approved")

        preview = self.service.management_lexicon_export_rime(
            {"project": "wisdom-weasel-rag-ime", "status": "approved", "dryRun": True}
        )
        pending_attempt = self.service.management_lexicon_export_rime({"status": "pending", "dryRun": True})
        apply_attempt = self.service.management_lexicon_export_rime({"dryRun": False})

        self.assertTrue(preview["ok"])
        self.assertTrue(preview["dryRun"])
        self.assertFalse(preview["applySupported"])
        self.assertEqual(preview["entryCount"], 1)
        self.assertEqual(preview["entries"][0]["phrase"], "连续预测")
        self.assertIn("连续预测", preview["text"])
        self.assertNotIn("待审核短语", preview["text"])
        self.assertNotIn("稳定记忆", preview["text"])
        self.assertFalse(pending_attempt["ok"])
        self.assertEqual(pending_attempt["requiredStatus"], "approved")
        self.assertFalse(apply_attempt["ok"])
        self.assertIn("dryRun", apply_attempt["error"])

    def test_native_rime_feedback_lexicon_requires_review_token_and_can_rollback(self) -> None:
        for index in range(2):
            record_rime_rank_feedback(
                self.db_path,
                preedit="biao qing bao",
                accepted_text="表情包",
                action="accepted",
                candidate_rank=1,
                context_hash=f"context-{index}",
                project="wisdom-weasel-rag-ime",
            )

        review = self.service.rime_lexicon_review({})
        self.assertEqual(review["entryCount"], 1)
        self.assertTrue(review["reviewRequired"])
        self.assertEqual(review["organization"]["owner"], "maintenance_poll")
        self.assertEqual(review["organization"]["decoderOwner"], "rime")
        self.assertTrue(review["organization"]["enabled"])
        self.assertIsNotNone(review["organization"]["nextRunAtMs"])

        stale = self.service.rime_lexicon_apply(
            {
                "reviewToken": "stale",
                "selectedKeys": [review["entries"][0]["reviewKey"]],
                "confirmText": CONFIRM_TEXT,
            }
        )
        self.assertEqual(stale["reason"], "review_token_stale")

        applied = self.service.rime_lexicon_apply(
            {
                "reviewToken": review["reviewToken"],
                "selectedKeys": [review["entries"][0]["reviewKey"]],
                "confirmText": CONFIRM_TEXT,
            }
        )
        self.assertTrue(applied["applied"])
        self.assertTrue(applied["requiresRedeploy"])
        self.assertTrue((Path(self.tmp.name) / "Rime" / "rag_ime_user.dict.yaml").is_file())
        self.assertEqual(self.service.rime_lexicon_review({})["entryCount"], 0)

        rolled_back = self.service.rime_lexicon_rollback({"rollbackId": applied["rollbackId"]})
        self.assertTrue(rolled_back["rolledBack"])
        self.assertFalse((Path(self.tmp.name) / "Rime" / "rag_ime_user.dict.yaml").exists())
        self.assertEqual(self.service.rime_lexicon_review({})["entryCount"], 1)

    def test_rime_lexicon_rollbacks_must_run_in_reverse_apply_order(self) -> None:
        for index in range(2):
            record_rime_rank_feedback(
                self.db_path,
                preedit="pai hui hua",
                accepted_text="派会话",
                action="accepted",
                candidate_rank=1,
                context_hash=f"first-{index}",
                project="wisdom-weasel-rag-ime",
            )
        first_review = self.service.rime_lexicon_review({})
        first = self.service.rime_lexicon_apply(
            {
                "reviewToken": first_review["reviewToken"],
                "selectedKeys": [first_review["entries"][0]["reviewKey"]],
                "confirmText": CONFIRM_TEXT,
            }
        )
        self.assertTrue(first["applied"])

        for index in range(2):
            record_rime_rank_feedback(
                self.db_path,
                preedit="shen du jian suo",
                accepted_text="深度检索",
                action="accepted",
                candidate_rank=1,
                context_hash=f"second-{index}",
                project="wisdom-weasel-rag-ime",
            )
        time.sleep(0.002)
        second_review = self.service.rime_lexicon_review({})
        second_key = next(
            item["reviewKey"]
            for item in second_review["entries"]
            if item["text"] == "深度检索"
        )
        second = self.service.rime_lexicon_apply(
            {
                "reviewToken": second_review["reviewToken"],
                "selectedKeys": [second_key],
                "confirmText": CONFIRM_TEXT,
            }
        )
        self.assertTrue(second["applied"])

        blocked = self.service.rime_lexicon_rollback({"rollbackId": first["rollbackId"]})
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["reason"], "newer_rollback_required_first")
        self.assertEqual(blocked["blockingRollbackId"], second["rollbackId"])

        self.assertTrue(
            self.service.rime_lexicon_rollback({"rollbackId": second["rollbackId"]})["rolledBack"]
        )
        self.assertTrue(
            self.service.rime_lexicon_rollback({"rollbackId": first["rollbackId"]})["rolledBack"]
        )
        duplicate = self.service.rime_lexicon_rollback({"rollbackId": first["rollbackId"]})
        self.assertFalse(duplicate["ok"])
        self.assertEqual(duplicate["reason"], "rollback_already_applied")

    def test_cleanup_diff_apply_and_rollback_are_audited(self) -> None:
        event_ref = self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=1_900_000_100_020,
                source="manual",
                committed_text="离线整理稳定记忆",
                privacy_disposition="allowed",
                recent_context="用户接受过这个短语",
                project="wisdom-weasel-rag-ime",
                tags=("memory",),
            )
        )
        self._compile_phrase(event_ref, "离线整理稳定记忆", tags=("memory",))
        self.core.apply_action(
            MemoryAction(
                action_id=None,
                created_at_ms=1_900_000_100_021,
                memory_id="event:1",
                action_type="accepted",
                query="离线整理",
            )
        )
        plan = self.core.build_memory_cleanup_plan(project="wisdom-weasel-rag-ime")
        run = self.core.list_memory_cleanup_runs(run_id=str(plan["runId"]), limit=1)
        diff_id = int(run["items"][0]["diffs"][0]["diffId"])

        inspected = self.service.management_cleanup_diff({"id": diff_id})
        missing_apply_confirm = self.service.management_cleanup_diff_apply({"diffId": diff_id})
        applied = self.service.management_cleanup_diff_apply({"diffId": diff_id, "confirm": "apply"})
        missing_rollback_confirm = self.service.management_cleanup_diff_rollback({"diffId": diff_id})
        rolled_back = self.service.management_cleanup_diff_rollback({"diffId": diff_id, "confirm": "rollback"})

        self.assertTrue(inspected["ok"])
        self.assertFalse(missing_apply_confirm["ok"])
        self.assertEqual(missing_apply_confirm["requiredConfirm"], "apply")
        self.assertTrue(applied["ok"])
        self.assertFalse(missing_rollback_confirm["ok"])
        self.assertEqual(missing_rollback_confirm["requiredConfirm"], "rollback")
        self.assertTrue(rolled_back["ok"])
        self.assertGreater(int(applied["auditId"]), 0)
        self.assertEqual(self._audit_count("cleanup_diff_apply"), 1)
        self.assertEqual(self._audit_count("cleanup_diff_rollback"), 1)

    def test_debug_management_health_is_localhost_only_by_default(self) -> None:
        health = self.service.health()

        self.assertTrue(health["management"]["localhostOnly"])
        self.assertFalse(health["management"]["rawTextVisible"])
        self.assertIn("settings", health["management"])

    def test_settings_update_schema_and_reset_are_audited(self) -> None:
        schema = self.service.settings_schema()
        update = self.service.settings_update({"display.badges.model": "AI", "interaction.postCommit.optionNumber": "disabled"})
        settings = self.service.settings()
        reset = self.service.settings_reset_section({"section": "display"})

        self.assertTrue(schema["ok"])
        self.assertIn("interaction", {item["id"] for item in schema["sections"]})
        self.assertTrue(update["ok"])
        self.assertGreater(int(update["auditId"]), 0)
        self.assertEqual(settings["settings"]["display"]["badges"]["model"], "AI")
        self.assertEqual(settings["settings"]["interaction"]["postCommit"]["numberKeys"], "pass_through")
        self.assertEqual(settings["settings"]["interaction"]["postCommit"]["optionNumber"], "disabled")
        self.assertEqual(reset["settings"]["display"]["badges"]["model"], "模")
        self.assertEqual(self._audit_count("settings_update"), 1)
        self.assertEqual(self._audit_count("settings_reset_section"), 1)

    def test_active_rag_shortcut_update_returns_runtime_sync_commands(self) -> None:
        runner = Mock(
            return_value=subprocess.CompletedProcess(["defaults"], 0, "", "")
        )
        self.service._runtime_command_runner = runner
        result = self.service.active_rag_settings_update(
            {
                "shortcut": "ctrl+r",
                "capture": {
                    "accessibility": True,
                    "clipboardFallback": False,
                },
            }
        )
        runtime_sync = result["runtimeSync"]
        commands = runtime_sync["commands"]

        self.assertTrue(result["ok"])
        self.assertEqual(result["settings"]["activeRag"]["shortcut"], "ctrl+r")
        self.assertEqual(runtime_sync["shortcut"], "ctrl+r")
        self.assertTrue(runtime_sync["attempted"])
        self.assertTrue(runtime_sync["applied"])
        self.assertEqual(runner.call_count, 4)
        self.assertIn("activeRag.shortcut", result["changedKeys"])
        self.assertNotIn("confirmText", result["settings"])
        self.assertIn(
            ["defaults", "write", "im.rime.inputmethod.Squirrel", "RagImeActiveRagShortcut", "-string", "ctrl+r"],
            commands,
        )
        self.assertIn(
            ["defaults", "write", "im.rime.inputmethod.Squirrel", "RagImeActiveRagCaptureClipboardFallback", "-bool", "false"],
            commands,
        )

    def test_display_badge_customization_applies_to_rime_suggest_preview(self) -> None:
        self.service.settings_update({"display.badges.model": "AI", "display.maxPostCommitCandidates": 2})
        payload = {
            "sessionId": "custom-display",
            "requestSeq": 1,
            "privacyDisposition": "allowed",
            "rawInput": "",
            "preedit": "",
            "committedContext": "我想设计一个候选栏",
            "commitTextPreview": "候选栏",
            "predictionFirstMerge": True,
            "maxSideCandidates": 8,
            "latencyBudgetMs": 1000,
            "rimeContext": {"candidates": []},
            "foregroundText": {
                "surroundingBefore": "我想设计一个候选栏",
                "surroundingAfter": "",
                "source": "accessibility",
                "captureAgeMs": 0,
            },
        }
        with patch.dict("os.environ", {"RAG_IME_ENABLE_POST_COMMIT_AUTO_MODEL": "1"}, clear=False):
            response = self.service.rime_suggest(payload)
            model_items = [item for item in response["displayCandidates"] if item["sourceType"] == "model"]
            for request_seq in range(2, 6):
                if model_items:
                    break
                time.sleep(0.2)
                response = self.service.rime_suggest({**payload, "requestSeq": request_seq})
                model_items = [item for item in response["displayCandidates"] if item["sourceType"] == "model"]

        self.assertLessEqual(len([item for item in response["displayCandidates"] if item["selectionAction"] != "none"]), 2)
        self.assertTrue(model_items)
        self.assertEqual(model_items[0]["badge"], "AI")

    def test_disable_composition_prediction_keeps_rime_like_candidates_only(self) -> None:
        self.service.settings_update({"interaction.composition.showPrediction": False})
        response = self.service.rime_suggest(
            {
                "sessionId": "custom-composition",
                "requestSeq": 1,
                "privacyDisposition": "allowed",
                "rawInput": "houxuan",
                "preedit": "houxuan",
                "committedContext": "输入法",
                "predictionFirstMerge": True,
                "maxSideCandidates": 8,
                "rimeContext": {"candidates": [{"label": "1", "text": "候选"}]},
            }
        )

        self.assertTrue(all(item["sourceType"] in {"rime", "raw_english", "status"} for item in response["displayCandidates"]))

    def test_rag_preview_reports_disabled_lanes_from_settings(self) -> None:
        self.service.settings_update({"rag.lanes.bm25Tags": False})

        preview = self.service.rag_core_v3_query_preview({"query": "多路召回"})

        self.assertTrue(preview["ok"])
        self.assertTrue(preview["lanes"]["bm25Tags"]["disabledBySettings"])
        self.assertIn({"lane": "bm25Tags", "reason": "disabled_by_management_settings"}, preview["blocked"])

    def test_vocabulary_and_active_rag_management_api(self) -> None:
        added = self.service.vocabulary_item_save(
            {
                "surface": "StableCandidateSnapshot",
                "aliases": ["候选快照"],
                "pinyin": "hou xuan kuai zhao",
                "tags": ["输入法"],
                "priority": 100,
            },
            action="add",
        )
        items = self.service.vocabulary_items({"query": "Stable"})
        export_preview = self.service.vocabulary_rime_export_preview({})
        active = self.service.active_rag_preview(
            {
                "selectedText": "候选栏闪烁，需要解释原因",
                "privacyDisposition": "allowed",
            }
        )

        self.assertTrue(added["ok"])
        self.assertEqual(items["items"][0]["surface"], "StableCandidateSnapshot")
        self.assertIn("StableCandidateSnapshot", export_preview["text"])
        self.assertTrue(active["ok"])
        self.assertTrue(active["dryRun"])
        self.assertIn("selectedTextHash", active)

    def test_management_security_token_gate_when_enabled(self) -> None:
        self.service.settings_update({"managementSecurity.requireToken": True, "managementSecurity.token": "secret-token"})

        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = Path("debug")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(
                f"http://127.0.0.1:{server.server_port}/api/settings/update",
                data=json.dumps({"display.badges.model": "AI"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                urlopen(request, timeout=5)
                denied_payload = {"ok": True}
            except HTTPError as exc:
                try:
                    denied_payload = json.loads(exc.read().decode("utf-8"))
                finally:
                    exc.close()

            allowed_request = Request(
                f"http://127.0.0.1:{server.server_port}/api/settings/update",
                data=json.dumps({"display.badges.model": "AI"}).encode("utf-8"),
                headers={"Content-Type": "application/json", "X-RAG-IME-Admin-Token": "secret-token"},
                method="POST",
            )
            with urlopen(allowed_request, timeout=5) as response:
                allowed_payload = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertFalse(denied_payload["ok"])
        self.assertIn("token", denied_payload["error"])
        self.assertTrue(allowed_payload["ok"])

    def test_active_rag_rejects_screenshots_in_favor_of_ax_window_context(self) -> None:
        with self.assertRaisesRegex(ValueError, "AX windowContext"):
            self.service._active_rag_request_from_payload(
                {
                    "selectedText": "根据界面补全",
                    "privacyDisposition": "allowed",
                    "frontAppBundleId": "com.example.Editor",
                    "visualContext": {
                        "schemaVersion": "rag-ime.visual-context.v1",
                        "mimeType": "image/png",
                        "dataBase64": base64.b64encode(PNG_1X1).decode("ascii"),
                        "pixelWidth": 1,
                        "pixelHeight": 1,
                        "source": "front_app_window",
                    },
                }
            )

    def test_rag_core_v3_preview_is_read_only(self) -> None:
        event_ref = self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=1_900_000_100_030,
                source="manual",
                committed_text="多路召回",
                privacy_disposition="allowed",
                recent_context="RAG 输入法",
                project="wisdom-weasel-rag-ime",
                tags=("RAG",),
            )
        )
        self._compile_phrase(event_ref, "多路召回", tags=("RAG",))
        before = self._retrieval_doc_count()

        preview = self.service.rag_core_v3_query_preview({"query": "多路召回", "project": "wisdom-weasel-rag-ime"})
        after = self._retrieval_doc_count()

        self.assertTrue(preview["ok"])
        self.assertEqual(preview["schemaVersion"], "rag-ime.rag-core-v3-preview.v1")
        self.assertEqual(before, after)

    def test_rag_core_v3_preview_redacts_raw_text_by_default(self) -> None:
        event_ref = self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=1_900_000_100_031,
                source="manual",
                committed_text="隐私短语",
                privacy_disposition="allowed",
                recent_context="超级秘密上下文",
                project="wisdom-weasel-rag-ime",
                tags=("RAG",),
            )
        )
        self._compile_phrase(event_ref, "隐私短语", tags=("RAG",))
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            rebuild_retrieval_docs(conn, project="wisdom-weasel-rag-ime")

        preview = self.service.rag_core_v3_query_preview({"query": "隐私短语", "project": "wisdom-weasel-rag-ime"})
        blob = json.dumps(preview, ensure_ascii=False)

        self.assertTrue(preview["ok"])
        self.assertFalse(preview["rawTextVisible"])
        self.assertNotIn("隐私短语 超级秘密上下文", blob)
        self.assertNotIn("超级秘密上下文", blob)
        self.assertIn("evidencePreviewHash", blob)

    def test_rag_core_v3_preview_reports_lane_breakdown(self) -> None:
        event_ref = self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=1_900_000_100_032,
                source="manual",
                committed_text="多路召回",
                privacy_disposition="allowed",
                recent_context="RAG 输入法",
                project="wisdom-weasel-rag-ime",
                tags=("RAG",),
            )
        )
        self._compile_phrase(event_ref, "BM25 加向量召回用于输入法候选", tags=("RAG",))

        rebuild = self.service.rag_core_v3_rebuild_retrieval_docs({"project": "wisdom-weasel-rag-ime"})
        preview = self.service.rag_core_v3_query_preview({"query": "多路召回", "project": "wisdom-weasel-rag-ime"})

        self.assertTrue(rebuild["ok"])
        self.assertIn("bm25Raw", preview["lanes"])
        self.assertGreaterEqual(preview["lanes"]["bm25Raw"]["count"], 1)

    def test_deepseek_preview_requires_explicit_flag_or_token(self) -> None:
        with patch.dict("os.environ", {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "0"}, clear=False):
            preview = self.service.deepseek_completion_preview({"currentContext": "RAG 输入法"})

        self.assertFalse(preview["ok"])
        self.assertIn("RAG_IME_DEEPSEEK_ACTIVE_RAG=1", preview["requires"])

    def test_deepseek_preview_blocks_secure_input_before_retrieval_or_prompt_build(self) -> None:
        secret = "账号 admin 密码 cannot-leave-this-machine"

        preview = self.service.deepseek_completion_preview(
            {
                "currentContext": secret,
                "selectedText": secret,
                "secureInput": True,
                "dryRun": False,
            }
        )

        blob = json.dumps(preview, ensure_ascii=False)
        self.assertFalse(preview["ok"])
        self.assertEqual(preview["error"], "sensitive_field_blocked")
        self.assertFalse(preview["retrieval"]["called"])
        self.assertFalse(preview["remoteModel"]["requested"])
        self.assertEqual(preview["messages"], [])
        self.assertEqual(preview["evidencePack"], [])
        self.assertEqual(preview["candidates"], [])
        self.assertNotIn(secret, blob)
        self.assertNotIn("sha256:", blob)

    def test_deepseek_preview_dry_run_builds_single_candidate_rag_evidence(self) -> None:
        event_ref = self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=1_900_000_100_034,
                source="manual",
                committed_text="BM25 加向量召回用于输入法候选",
                privacy_disposition="allowed",
                recent_context="RAG 输入法核心改造",
                project="wisdom-weasel-rag-ime",
                tags=("RAG",),
            )
        )
        self._compile_phrase(event_ref, "BM25 加向量召回用于输入法候选", tags=("RAG",))

        self.service.settings_update(
            {
                "activeRag.allowRemoteModel": True,
                "privacy.allowRemoteModelForActiveRag": True,
                "confirmText": "ALLOW REMOTE MODEL",
            }
        )
        with patch.dict(
            "os.environ",
            {
                "RAG_IME_DEEPSEEK_ACTIVE_RAG": "1",
                "RAG_IME_DEEPSEEK_API_KEY": "test-key-not-sent-by-dry-run",
            },
            clear=False,
        ):
            preview = self.service.deepseek_completion_preview(
                {"currentContext": "RAG 输入法核心改造", "dryRun": True}
            )

        self.assertTrue(preview["ok"])
        self.assertTrue(preview["dryRun"])
        self.assertGreaterEqual(len(preview["evidencePack"]), 1)
        self.assertTrue(preview["requestDiagnostics"]["injection"]["success"])
        self.assertTrue(preview["requestDiagnostics"]["injection"]["evidenceIncluded"])
        self.assertNotIn("text", preview["messages"][1]["content"])
        self.assertIn("hash", preview["messages"][1]["content"])
        self.assertNotIn("RAG 输入法核心改造", json.dumps(preview, ensure_ascii=False))

    def test_active_rag_route_status_reports_every_remote_gate(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "RAG_IME_DEEPSEEK_ACTIVE_RAG": "1",
                "RAG_IME_DEEPSEEK_API_KEY": "route-test-key",
            },
            clear=False,
        ):
            self.service.settings_update({"activeRag.allowRemoteModel": False})
            blocked = self.service.active_rag_route_status(local_only=False)
            self.service.settings_update(
                {
                    "activeRag.allowRemoteModel": True,
                    "privacy.allowRemoteModelForActiveRag": True,
                    "confirmText": "ALLOW REMOTE MODEL",
                }
            )
            ready = self.service.active_rag_route_status(local_only=False)

        self.assertFalse(blocked["remoteReady"])
        self.assertEqual(blocked["skipReason"], "active_rag_remote_not_allowed")
        self.assertTrue(ready["remoteReady"])
        self.assertTrue(all(ready["gates"].values()))
        self.assertEqual(
            ready["selectedModel"],
            f"{ready['provider']}/{ready['model']}",
        )
        self.assertFalse(ready["passivePostCommitRemoteAllowed"])

    def test_knowledge_workbench_runs_explicit_deepseek_session(self) -> None:
        self.service.knowledge_workbench.generator = _KnowledgeGenerator()
        self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=1_900_000_100_044,
                source="manual",
                committed_text="个人知识工作台使用本地 RAG 证据",
                privacy_disposition="allowed",
                recent_context="DeepSeek 显式知识问答",
                project="wisdom-weasel-rag-ime",
                tags=("knowledge-workbench",),
            )
        )
        self._upsert_item(
            memory_id="phrase:knowledge-workbench",
            kind="phrase",
            text="个人知识工作台使用本地 RAG 证据",
            status="approved",
        )
        self.service.rag_core_v3_rebuild_retrieval_docs({"project": "wisdom-weasel-rag-ime"})
        self.service.settings_update(
            {
                "activeRag.allowRemoteModel": True,
                "privacy.allowRemoteModelForActiveRag": True,
                "confirmText": "ALLOW REMOTE MODEL",
            }
        )
        with patch.dict(
            "os.environ",
            {
                "RAG_IME_DEEPSEEK_ACTIVE_RAG": "1",
                "RAG_IME_DEEPSEEK_API_KEY": "knowledge-route-test-key",
            },
            clear=False,
        ):
            started = self.service.knowledge_workbench_start(
                {
                    "mode": "knowledge_answer",
                    "question": "个人知识工作台使用本地 RAG 证据",
                    "context": "本地 RAG",
                    "generation": 3,
                    "includeNotion": False,
                }
            )
            deadline = time.monotonic() + 2
            response = started
            while response.get("status") not in {"ready", "error", "cancelled"} and time.monotonic() < deadline:
                time.sleep(0.005)
                response = self.service.knowledge_workbench_status({"sessionId": started["sessionId"]})

        self.assertTrue(started["ok"])
        self.assertEqual(response["status"], "ready")
        self.assertEqual(response["answer"], "本地知识答案")
        self.assertEqual(response["generation"], 3)
        self.assertGreaterEqual(len(response["evidence"]), 1)
        self.assertIn("contextInjection", response["diagnostics"])

    def test_knowledge_workbench_retrieves_memories_from_other_apps(self) -> None:
        self._upsert_item(
            memory_id="phrase:cross-app-bge",
            kind="phrase",
            text="BGE 向量索引需要在 provider 指纹变化后重新构建",
            status="approved",
            app="com.openai.codex",
        )
        self.service.rag_core_v3_rebuild_retrieval_docs({"project": "wisdom-weasel-rag-ime"})

        evidence = self.service._knowledge_workbench_evidence(
            KnowledgeWorkbenchRequest(
                question="BGE provider 指纹变化后怎么处理向量索引",
                mode="knowledge_answer",
                app="com.rag-ime.control",
            )
        )

        self.assertIn("phrase:cross-app-bge", [item["sourceId"] for item in evidence])
        matched = next(item for item in evidence if item["sourceId"] == "phrase:cross-app-bge")
        self.assertIn("BGE 向量索引", matched["text"])
        self.assertGreater(matched["score"], 0.0)

    def test_knowledge_workbench_reserves_structured_book_and_atom_evidence(self) -> None:
        phrase_hits = [
            {
                "doc_id": "phrase:noisy",
                "doc_type": "phrase",
                "source_id": "phrase:noisy",
                "text": "重复的短语证据",
                "source_lane": lane,
                "rank": 1,
                "tags": ["输入法"],
                "metadata": {},
            }
            for lane in ("bm25_raw", "bm25_tags", "tagmemo")
        ]
        structured_hits = [
            {
                "doc_id": "book:demo",
                "doc_type": "book",
                "source_id": "book:demo",
                "text": "输入法优先演示路线",
                "source_lane": "bm25_raw",
                "rank": 2,
                "tags": ["输入法", "RAG"],
                "metadata": {"bookTitle": "输入法优先演示路线"},
            },
            {
                "doc_id": "atom:demo",
                "doc_type": "atom",
                "source_id": "atom:demo",
                "text": "DeepSeek 只处理显式知识工作台查询",
                "source_lane": "bm25_tags",
                "rank": 3,
                "tags": ["DeepSeek"],
                "metadata": {},
            },
        ]
        with patch(
            "rag_ime.debug_server.retrieve_hybrid_rag_candidates",
            return_value={"candidates": [], "hits": [*phrase_hits, *structured_hits]},
        ) as retrieve:
            evidence = self.service._knowledge_workbench_evidence(
                KnowledgeWorkbenchRequest(
                    question="输入法演示如何联动知识工作台",
                    mode="knowledge_answer",
                    app="com.rag-ime.control.demo",
                )
            )

        self.assertEqual([item["sourceId"] for item in evidence[:2]], ["book:demo", "atom:demo"])
        self.assertEqual(retrieve.call_args.args[1].app, "")

    def test_knowledge_workbench_today_query_drops_old_evidence(self) -> None:
        old_timestamp_ms = 1_735_689_600_000
        event_token = self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=old_timestamp_ms,
                source="manual",
                committed_text="很久以前完成的输入法工作",
                privacy_disposition="allowed",
                recent_context="旧记录",
                project="wisdom-weasel-rag-ime",
                tags=("old",),
            )
        )
        hits = [{
            "doc_id": "atom:old",
            "doc_type": "atom",
            "source_id": "atom:old",
            "text": "很久以前完成的输入法工作",
            "source_lane": "bm25_raw",
            "rank": 1,
            "tags": ["old"],
            "metadata": {"sourceEventIds": [int(event_token.removeprefix("event:"))]},
        }]
        with patch("rag_ime.debug_server.retrieve_hybrid_rag_candidates", return_value={"candidates": [], "hits": hits}):
            evidence = self.service._knowledge_workbench_evidence(
                KnowledgeWorkbenchRequest(question="今天我干了哪些", mode="knowledge_answer")
            )

        self.assertEqual(evidence, ())

    def test_knowledge_workbench_today_query_reads_today_timeline_without_keyword_match(self) -> None:
        event_token = self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=int(time.time() * 1000),
                source="manual",
                committed_text="完成了输入法知识引用界面的可读性修复",
                privacy_disposition="allowed",
                recent_context="",
                project="wisdom-weasel-rag-ime",
                tags=("today",),
            )
        )
        with patch("rag_ime.debug_server.retrieve_hybrid_rag_candidates") as retrieve:
            evidence = self.service._knowledge_workbench_evidence(
                KnowledgeWorkbenchRequest(question="今天我干了哪些", mode="recall")
            )

        self.assertIn(event_token, [item["sourceId"] for item in evidence])
        self.assertTrue(all(item["sourceLane"] == "temporal_timeline" for item in evidence))
        retrieve.assert_not_called()

    def test_knowledge_workbench_yesterday_query_uses_yesterday_only(self) -> None:
        timestamp_ms = int(time.time() * 1000) - 24 * 60 * 60 * 1000
        token = self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=timestamp_ms,
                source="manual",
                committed_text="昨天完成了时间范围解析器的设计",
                privacy_disposition="allowed",
                recent_context="",
                project="wisdom-weasel-rag-ime",
                tags=("yesterday",),
            )
        )

        evidence = self.service._knowledge_workbench_evidence(
            KnowledgeWorkbenchRequest(question="昨天做了什么", mode="recall")
        )

        self.assertIn(token, [item["sourceId"] for item in evidence])
        self.assertTrue(all(item["sourceLane"] == "temporal_timeline" for item in evidence))

    def test_knowledge_workbench_blocks_sensitive_text_before_retrieval(self) -> None:
        response = self.service.knowledge_workbench_start(
            {
                "mode": "recall",
                "question": "帮我回忆账号密码",
                "context": "password=must-not-leave",
                "secureInput": True,
            }
        )

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], "sensitive_field_blocked")
        self.assertFalse(response["retrieval"]["called"])
        self.assertFalse(response["remoteModel"]["requested"])

    def test_knowledge_route_reports_worker_and_polling_as_separate_gates(self) -> None:
        route = self.service.knowledge_workbench_route_status()

        self.assertIn("notion", route)
        self.assertIn("defaultOrganizationInstruction", route)
        self.assertIn("个人、项目与长期工作主题", route["defaultOrganizationInstruction"])
        self.assertIn("submitConfigured", route["notion"])
        self.assertIn("pollConfigured", route["notion"])
        self.assertFalse(route["notion"]["ready"])

    def test_memory_maintenance_status_reports_the_configured_model(self) -> None:
        managed = MemoryMaintenanceSettings(
            automatic_organization_model="openai-codex/gpt-6-luna",
        )
        with patch(
            "rag_ime.debug_server.MemoryMaintenanceSettings.load",
            return_value=managed,
        ):
            status = self.service.agent_memory_maintenance_status(
                {"project": "wisdom-weasel-rag-ime", "limit": 1}
            )

        self.assertEqual(status["automation"]["model"], "openai-codex/gpt-6-luna")
        self.assertEqual(status["modelCuration"]["requiredModel"], "openai-codex/gpt-6-luna")

    def test_agent_memory_maintenance_status_auto_applies_governed_runs_and_counts_legacy_drafts(self) -> None:
        event_ref = self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=int(time.time() * 1000),
                source="pi_agent_user",
                committed_text="Pi 最终消息等待异步整理",
                privacy_disposition="allowed",
                recent_context="",
                project="wisdom-weasel-rag-ime",
            )
        )
        event_id = int(event_ref.split(":", 1)[1])
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            source_bundle = build_memory_book_source_bundle(
                conn,
                project="wisdom-weasel-rag-ime",
            )
        plan = memory_book_plan_from_compile_output(
            {
                "schemaVersion": "rag-ime.memory-book-compile.v1",
                "dailyBooks": [
                    {
                        "bookKey": "2026-07-14",
                        "title": "Pi 会话整理草案",
                        "summary": "只保存草案，等待原生审批。",
                        "sourceEventIds": [event_id],
                    }
                ],
            },
            project="wisdom-weasel-rag-ime",
            provider="deepseek",
            model="deepseek-test",
            source_bundle=source_bundle,
        )
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            store_memory_book_plan(conn, plan)

        status = self.service.agent_memory_maintenance_status(
            {"project": "wisdom-weasel-rag-ime", "limit": 5}
        )

        self.assertTrue(status["ok"])
        self.assertEqual(status["policy"], "auto_governed")
        self.assertTrue(status["autoApply"])
        self.assertFalse(status["scheduledDraftOnly"])
        self.assertEqual(status["automation"]["runsPerDay"], 2)
        self.assertEqual(
            status["automation"]["model"],
            "openai-codex/gpt-5.6-luna",
        )
        self.assertEqual(status["automation"]["curationProtocol"], "atom-first-v1")
        self.assertEqual(status["automation"]["targetSourceCount"], 1_000)
        self.assertEqual(status["automation"]["maximumSourceCount"], 1_500)
        self.assertEqual(status["automation"]["maximumInputTokens"], 200_000)
        self.assertEqual(status["automation"]["reservedContextTokens"], 72_000)
        self.assertEqual(
            status["modelCuration"]["requiredModel"],
            "openai-codex/gpt-5.6-luna",
        )
        self.assertEqual(status["modelCuration"]["requiredThinkingLevel"], "max")
        self.assertEqual(status["modelCuration"]["minimumContextTokens"], 272_000)
        self.assertEqual(status["modelCuration"]["runs"], [])
        self.assertTrue(status["bookProjection"]["inSync"])
        self.assertEqual(status["ownerCuration"]["policy"]["cadence"], "twice_daily")
        self.assertTrue(
            status["ownerCuration"]["policy"]["autoApplyGovernedWrites"]
        )
        self.assertFalse(
            status["ownerCuration"]["policy"]["semanticWritesRequireReview"]
        )
        self.assertGreaterEqual(status["compileState"]["pendingEventCount"], 1)

        self.assertEqual(status["pendingDraftCount"], 1)
        self.assertEqual(status["runs"][0]["runId"], plan["runId"])
        self.assertEqual(status["runs"][0]["status"], "draft")

        compiled_event_id = int(plan["metadata"]["sourceCursor"]["toEventId"])
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            conn.execute(
                """
                INSERT INTO memory_compile_state(
                    project, last_compiled_event_id, last_run_ms, pending_event_count, last_bundle_hash
                ) VALUES (?, ?, ?, 0, ?)
                ON CONFLICT(project) DO UPDATE SET
                    last_compiled_event_id = excluded.last_compiled_event_id,
                    last_run_ms = excluded.last_run_ms,
                    pending_event_count = 0,
                    last_bundle_hash = excluded.last_bundle_hash
                """,
                (
                    "wisdom-weasel-rag-ime",
                    compiled_event_id,
                    int(time.time() * 1000),
                    str(plan["metadata"]["bundleHash"]),
                ),
            )
            conn.commit()

        stale_status = self.service.agent_memory_maintenance_status(
            {"project": "wisdom-weasel-rag-ime", "limit": 5}
        )
        self.assertEqual(stale_status["pendingDraftCount"], 0)
        self.assertEqual(stale_status["runs"][0]["status"], "superseded")

    def test_agent_memory_maintenance_projection_only_skips_full_database_status(self) -> None:
        projection = {
            "schemaVersion": "rag-ime.memory-projection-runtime.v1",
            "ok": True,
            "configured": True,
            "running": True,
            "lastRunAtMs": 42,
        }
        job = {
            "schemaVersion": "rag-ime.gateway-memory-maintenance-job.v1",
            "ok": True,
            "jobId": "memory-maintenance:active",
            "state": "running",
        }
        catalog = {
            "schemaVersion": "rag-ime.memory-catalog-consolidation.v1",
            "project": "wisdom-weasel-rag-ime",
            "state": "never",
            "due": True,
            "dueReason": "first_run",
        }
        with (
            patch.object(self.service, "memory_projection_status", return_value=projection) as project,
            patch.object(self.service.memory_maintenance_jobs, "latest_status", return_value=job) as latest_job,
            patch.object(self.service.memory_catalog_scheduler, "status", return_value=catalog) as catalog_status,
            patch("rag_ime.debug_server.memory_compile_due") as compile_due,
            patch("rag_ime.debug_server.owner_memory_curation_status") as owner_status,
        ):
            status = self.service.agent_memory_maintenance_status(
                {"projectionOnly": True}
            )

        self.assertEqual(
            status,
            {
                "schemaVersion": "rag-ime.agent-memory-maintenance-status.v1",
                "ok": True,
                "job": job,
                "catalogConsolidation": {
                    **catalog,
                    "enabled": True,
                    "automaticOrganizationEnabled": True,
                    "cadenceDays": 7,
                },
                "projection": projection,
            },
        )
        project.assert_called_once_with()
        latest_job.assert_called_once_with(project=self.service.config.project)
        catalog_status.assert_called_once_with(self.service.config.project)
        compile_due.assert_not_called()
        owner_status.assert_not_called()

    def test_memory_lifecycle_refresh_is_consumed_by_gateway_worker(self) -> None:
        submitted = self.service.memory_lifecycle_refresh({
            "operation": "daily_report",
            "project": "wisdom-weasel-rag-ime",
            "date": "2026-09-12",
            "timezone": "UTC",
        })
        deadline = time.monotonic() + 2
        status = self.service.memory_lifecycle_jobs.status(submitted["jobId"])
        while status["state"] not in {"completed", "failed", "paused", "stale"} and time.monotonic() < deadline:
            time.sleep(0.01)
            status = self.service.memory_lifecycle_jobs.status(submitted["jobId"])
        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["result"], {"ok": True, "reportReady": True})
        projection = self.service.memory_lifecycle_status({"project": "wisdom-weasel-rag-ime"})
        self.assertEqual(projection["jobs"][0]["job_id"], submitted["jobId"])
        self.assertEqual(projection["jobs"][0]["state"], "completed")

    def test_global_catalog_policy_change_runs_once_then_reuses(self) -> None:
        managed = MemoryMaintenanceSettings(
            automatic_organization_enabled=True,
            dreaming_enabled=False,
            catalog_consolidation_enabled=True,
        )

        def organizer_result(_request, *, source_bundle, **_kwargs):
            return {
                "ok": True,
                "modelCalled": True,
                "sealedCatalogDigest": source_bundle["catalogDigest"],
                "storedRun": {"runId": "memory-book-run:policy", "status": "applied"},
            }

        def run():
            return self.service._execute_gateway_memory_catalog_consolidation(
                project="wisdom-weasel-rag-ime", manual=True, managed=managed,
            )

        policy = "rag_ime.memory_curation.MEMORY_TOPIC_AGGREGATION_POLICY_VERSION"
        with patch.object(self.service, "_knowledge_workbench_database_organizer", side_effect=organizer_result) as organizer:
            with patch(policy, "previous-topic-policy", create=True):
                before = run()
            with patch(policy, "automatic-topic-aggregation", create=True):
                changed = run()
                repeated = run()

        self.assertTrue(before["ok"])
        self.assertTrue(changed["ok"])
        self.assertFalse(changed["skipped"])
        self.assertNotEqual(before["catalogDigest"], changed["catalogDigest"])
        self.assertEqual(organizer.call_count, 2)
        self.assertTrue(repeated["skipped"])
        self.assertEqual(repeated["reason"], "catalog_unchanged")

    def test_global_catalog_unchanged_digest_skips_a_second_model_call(self) -> None:
        managed = MemoryMaintenanceSettings(
            automatic_organization_enabled=True,
            dreaming_enabled=False,
            catalog_consolidation_enabled=True,
            catalog_consolidation_cadence_days=7,
        )
        def organizer_result(
            _request: object,
            *,
            source_bundle: Mapping[str, object],
            **_kwargs: object,
        ) -> dict[str, object]:
            sealed_digest = str(source_bundle["catalogDigest"])
            return {
                "ok": True,
                "modelCalled": True,
                "sealedCatalogDigest": sealed_digest,
                "storedRun": {
                    "runId": "memory-book-run:catalog-1",
                    "status": "applied",
                    "sealedCatalogDigest": sealed_digest,
                },
            }

        with patch.object(
            self.service,
            "_knowledge_workbench_database_organizer",
            side_effect=organizer_result,
        ) as organizer:
            first = self.service._execute_gateway_memory_catalog_consolidation(
                project="wisdom-weasel-rag-ime",
                manual=True,
                managed=managed,
            )
            second = self.service._execute_gateway_memory_catalog_consolidation(
                project="wisdom-weasel-rag-ime",
                manual=True,
                managed=managed,
            )

        self.assertTrue(first["ok"])
        self.assertFalse(first["skipped"])
        self.assertTrue(first["modelCalled"])
        self.assertEqual(first["state"], "completed")
        self.assertTrue(first["catalogDigest"])
        self.assertTrue(second["ok"])
        self.assertTrue(second["skipped"])
        self.assertFalse(second["modelCalled"])
        self.assertEqual(second["reason"], "catalog_unchanged")
        self.assertEqual(second["catalogDigest"], first["catalogDigest"])
        self.assertEqual(second["curationRunId"], first["curationRunId"])
        self.assertEqual(second["state"], "completed")
        organizer.assert_called_once()
        handed_off_bundle = organizer.call_args.kwargs["source_bundle"]
        self.assertEqual(
            handed_off_bundle["catalogDigest"],
            first["catalogDigest"],
        )
        self.assertTrue(handed_off_bundle["catalogComplete"])

    def test_generic_memory_organizer_reports_invalid_plan_without_masking_validation(self) -> None:
        request = KnowledgeWorkbenchRequest(
            question="整理记忆", mode="database_organize", project="wisdom-weasel-rag-ime",
            curation_scope="incremental", curation_policy="conservative",
        )
        organizer = Mock(provider_name="test")
        organizer.config.model = "test"
        organizer.compile_memory_curation.return_value = {}
        validation = {"ok": False, "errors": [{"code": "book_merge_member_scope_mismatch"}]}
        with (
            patch("rag_ime.debug_server.build_governed_memory_model_executor"),
            patch("rag_ime.debug_server.ManagedPiMemoryOrganizer", return_value=organizer),
            patch("rag_ime.debug_server.inspect_memory_book_plan", return_value=validation),
            patch("rag_ime.debug_server.store_memory_book_plan") as store,
            patch.object(self.service.agent.observations, "emit_memory_event", wraps=self.service.agent.observations.emit_memory_event) as emit,
        ):
            result = self.service._knowledge_workbench_database_organizer(request)
        self.assertFalse(result["ok"])
        self.assertEqual(result["validation"], validation)
        self.assertEqual(result["error"], "memory_book_plan_failed_validation")
        self.assertEqual(emit.call_args.kwargs["run_id"], result["plan"]["runId"])
        store.assert_not_called()

    def test_generic_memory_organizer_observation_failure_preserves_stored_result(self) -> None:
        request = KnowledgeWorkbenchRequest(
            question="整理记忆", mode="database_organize", project="wisdom-weasel-rag-ime",
            curation_scope="incremental", curation_policy="conservative",
        )
        organizer = Mock(provider_name="test")
        organizer.config.model = "test"
        organizer.compile_memory_curation.return_value = {}
        with (
            patch("rag_ime.debug_server.build_governed_memory_model_executor"),
            patch("rag_ime.debug_server.ManagedPiMemoryOrganizer", return_value=organizer),
            patch.object(self.service.agent.observations, "emit_memory_event", side_effect=RuntimeError("journal unavailable")),
        ):
            result = self.service._knowledge_workbench_database_organizer(request)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["storedRun"]["runId"])

    def test_generic_memory_organizer_auto_applies_a_valid_reused_draft(self) -> None:
        event_ref = self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=1_900_000_100_035,
                source="manual",
                committed_text="记忆整理通过治理校验后自动应用。",
                privacy_disposition="allowed",
                project="wisdom-weasel-rag-ime",
            )
        )
        event_id = int(event_ref.split(":", 1)[1])
        plan = memory_book_plan_from_compile_output(
            {
                "phraseCandidates": [{
                    "text": "记忆整理自动应用",
                    "tags": ["memory"],
                    "sourceEventIds": [event_id],
                    "weight": 0.8,
                }],
            },
            project="wisdom-weasel-rag-ime",
            provider="test",
            model="test",
        )
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            stored = store_memory_book_plan(conn, plan)

        request = KnowledgeWorkbenchRequest(
            question="整理记忆",
            mode="database_organize",
            project="wisdom-weasel-rag-ime",
            curation_scope="incremental",
            curation_policy="conservative",
        )
        with patch(
            "rag_ime.debug_server.find_memory_book_draft_for_bundle",
            return_value=stored,
        ):
            result = self.service._knowledge_workbench_database_organizer(request)

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["autoApplied"], result)
        self.assertFalse(result["dryRun"])
        self.assertFalse(result["applyRequiresReview"])
        self.assertFalse(result["storedDraft"])
        self.assertIn(result["storedRun"]["status"], {"applied", "partial"})

    def test_gateway_maintenance_runs_dreaming_when_automatic_organization_is_disabled(
        self,
    ) -> None:
        managed = MemoryMaintenanceSettings(
            automatic_organization_enabled=False,
            dreaming_enabled=True,
            dreaming_model="openai-codex/gpt-5.6-luna",
            dreaming_thinking_level="max",
        )
        executor = Mock(
            reference="openai-codex/gpt-5.6-luna",
            thinking_level="max",
            selected_model={"contextWindow": 400_000},
        )
        organizer = Mock()
        runner = Mock()
        runner.run_once.return_value = {
            "schemaVersion": "rag-ime.personal-context-maintenance-run.v1",
            "ok": True,
            "targets": [],
        }

        with (
            patch(
                "rag_ime.debug_server.MemoryMaintenanceSettings.load",
                return_value=managed,
            ),
            patch(
                "rag_ime.debug_server.run_due_lexicon_organization"
            ) as lexicon,
            patch(
                "rag_ime.debug_server.OwnerMemoryCurator"
            ) as owner_curator,
            patch(
                "rag_ime.debug_server.build_governed_memory_model_executor",
                return_value=executor,
            ) as build_executor,
            patch(
                "rag_ime.debug_server.ManagedPiMemoryOrganizer",
                return_value=organizer,
            ),
            patch(
                "rag_ime.debug_server.PersonalContextMaintenanceRunner",
                return_value=runner,
            ) as runner_type,
        ):
            report = self.service._execute_gateway_memory_maintenance(
                {
                    "project": "wisdom-weasel-rag-ime",
                    "manual": True,
                    "maxSources": 321,
                }
            )

        self.assertTrue(report["ok"])
        self.assertTrue(report["skipped"])
        self.assertEqual(report["reason"], "automatic_organization_disabled")
        self.assertTrue(report["lexiconOrganization"]["skipped"])
        self.assertTrue(report["dreaming"]["ok"])
        self.assertEqual(report["dreaming"]["executionOwner"], "agent_gateway")
        lexicon.assert_not_called()
        owner_curator.assert_not_called()
        build_executor.assert_called_once_with(
            self.service.agent.runtime,
            "openai-codex/gpt-5.6-luna",
            "max",
            db_path=self.db_path,
        )
        config = runner_type.call_args.kwargs["config"]
        self.assertTrue(config.enabled)
        self.assertTrue(config.consolidate_roles)
        self.assertFalse(config.build_timelines)
        self.assertTrue(config.apply_safe_recent_work)
        self.assertFalse(config.auto_publish_timelines)
        self.assertEqual(config.batch_limit, 321)
        runner.run_once.assert_called_once_with(force=True)
        organizer.close.assert_called_once_with()

    def test_scheduled_gateway_memory_maintenance_auto_applies_governed_drafts(self) -> None:
        managed = MemoryMaintenanceSettings(
            automatic_organization_enabled=True,
            dreaming_enabled=False,
            catalog_consolidation_enabled=False,
        )
        executor = Mock(
            reference="openai-codex/gpt-5.6-luna",
            thinking_level="max",
            selected_model={"contextWindow": 400_000},
        )
        organizer = Mock(curation_protocol_version="atom-first-v1")
        curator = Mock()
        curator.run_due.return_value = {"ok": True, "results": []}

        with (
            patch(
                "rag_ime.debug_server.MemoryMaintenanceSettings.load",
                return_value=managed,
            ),
            patch(
                "rag_ime.debug_server.run_due_lexicon_organization",
                return_value={"ok": True},
            ),
            patch(
                "rag_ime.debug_server.build_governed_memory_model_executor",
                return_value=executor,
            ),
            patch(
                "rag_ime.debug_server.ManagedPiMemoryOrganizer",
                return_value=organizer,
            ),
            patch(
                "rag_ime.debug_server.OwnerMemoryCurator",
                return_value=curator,
            ) as curator_type,
            patch.object(
                self.service,
                "_execute_gateway_memory_dreaming",
                return_value={"ok": True, "skipped": True},
            ),
        ):
            report = self.service._execute_gateway_memory_maintenance({})

        self.assertTrue(report["ok"])
        self.assertTrue(curator_type.call_args.kwargs["auto_apply"])
        curator.run_due.assert_called_once_with(
            manual=False,
            owner_kind="",
            owner_id="",
            instruction="",
        )

    def test_manual_memory_maintenance_drains_bounded_batches_and_is_degraded_on_auxiliary_failure(self) -> None:
        managed = MemoryMaintenanceSettings(
            automatic_organization_enabled=True,
            dreaming_enabled=True,
            catalog_consolidation_enabled=False,
        )
        executor = Mock(
            reference="openai-codex/gpt-5.6-luna",
            thinking_level="max",
            selected_model={"contextWindow": 400_000},
        )
        organizer = Mock(curation_protocol_version="atom-first-v1")
        curator = Mock()
        curator.run_due.side_effect = [
            {
                "ok": True,
                "results": [{
                    "ownerKind": "user",
                    "ownerId": "default",
                    "runId": "run-1",
                    "runStatus": "applied",
                    "sourceCount": 6,
                    "modelSourceCount": 6,
                    "autoApplied": True,
                    "remainingSourceCount": 6,
                }],
                "status": {"scopes": [{
                    "ownerKind": "user",
                    "ownerId": "default",
                    "pendingSourceCount": 6,
                    "lastSourceCursor": {"createdAtMs": 100, "sourceId": "source-1"},
                }]},
            },
            {
                "ok": True,
                "results": [{
                    "ownerKind": "user",
                    "ownerId": "default",
                    "runId": "run-2",
                    "runStatus": "applied",
                    "sourceCount": 6,
                    "modelSourceCount": 6,
                    "autoApplied": True,
                    "remainingSourceCount": 0,
                }],
                "status": {"scopes": [{
                    "ownerKind": "user",
                    "ownerId": "default",
                    "pendingSourceCount": 0,
                    "lastSourceCursor": {"createdAtMs": 200, "sourceId": "source-2"},
                }]},
            },
        ]

        with (
            patch(
                "rag_ime.debug_server.MemoryMaintenanceSettings.load",
                return_value=managed,
            ),
            patch(
                "rag_ime.debug_server.run_due_lexicon_organization",
                return_value={"ok": True},
            ),
            patch(
                "rag_ime.debug_server.build_governed_memory_model_executor",
                return_value=executor,
            ),
            patch(
                "rag_ime.debug_server.ManagedPiMemoryOrganizer",
                return_value=organizer,
            ),
            patch(
                "rag_ime.debug_server.OwnerMemoryCurator",
                return_value=curator,
            ) as curator_type,
            patch.object(
                self.service,
                "_execute_gateway_memory_dreaming",
                return_value={"ok": False, "error": "timeline unavailable"},
            ),
        ):
            progress: list[dict[str, object]] = []
            report = self.service._execute_gateway_memory_maintenance({
                "project": "wisdom-weasel-rag-ime",
                "manual": True,
                "maxSources": 6,
                "_progressCallback": progress.append,
            })

        self.assertTrue(report["ok"])
        self.assertTrue(report["degraded"])
        self.assertEqual(report["pendingSourceCount"], 0)
        self.assertEqual(report["batchCount"], 2)
        self.assertEqual(report["stageErrors"][0]["stage"], "dreaming")
        self.assertEqual(curator.run_due.call_count, 2)
        self.assertEqual(
            [(item["processedSourceCount"], item["pendingSourceCount"]) for item in progress],
            [(6, 6), (12, 0)],
        )

    def test_owner_scoped_manual_maintenance_skips_unrelated_dreaming(self) -> None:
        managed = MemoryMaintenanceSettings(
            automatic_organization_enabled=True,
            dreaming_enabled=True,
            catalog_consolidation_enabled=False,
        )
        executor = Mock(
            reference="openai-codex/gpt-5.6-luna",
            thinking_level="max",
            selected_model={"contextWindow": 400_000},
        )
        organizer = Mock(curation_protocol_version="atom-first-v1")
        curator = Mock()
        curator.run_due.return_value = {
            "ok": True,
            "results": [{
                "ownerKind": "user",
                "ownerId": "default",
                "runStatus": "empty",
                "sourceCount": 0,
                "remainingSourceCount": 0,
            }],
            "status": {"scopes": [{
                "ownerKind": "user",
                "ownerId": "default",
                "pendingSourceCount": 0,
                "lastSourceCursor": {},
            }]},
        }

        with (
            patch(
                "rag_ime.debug_server.MemoryMaintenanceSettings.load",
                return_value=managed,
            ),
            patch(
                "rag_ime.debug_server.run_due_lexicon_organization",
                return_value={"ok": True},
            ),
            patch(
                "rag_ime.debug_server.build_governed_memory_model_executor",
                return_value=executor,
            ),
            patch(
                "rag_ime.debug_server.ManagedPiMemoryOrganizer",
                return_value=organizer,
            ),
            patch(
                "rag_ime.debug_server.OwnerMemoryCurator",
                return_value=curator,
            ) as curator_type,
            patch.object(
                self.service,
                "_execute_gateway_memory_dreaming",
                side_effect=AssertionError("owner curation must not start dreaming"),
            ),
        ):
            report = self.service._execute_gateway_memory_maintenance({
                "project": "wisdom-weasel-rag-ime",
                "manual": True,
                "ownerKind": "user",
                "ownerId": "default",
                "maxSources": 1500,
            })

        self.assertTrue(report["ok"], report)
        self.assertTrue(report["dreaming"]["skipped"])
        self.assertEqual(report["dreaming"]["reason"], "owner_curation_only")
        self.assertTrue(report["catalogConsolidation"]["skipped"])
        self.assertEqual(report["catalogConsolidation"]["reason"], "owner_curation_only")
        self.assertEqual(curator_type.call_args.kwargs["max_sources"], 6)
        self.assertEqual(curator.run_due.call_count, 1)

    def test_scheduled_gateway_dreaming_enables_bounded_timeline_backfill(self) -> None:
        managed = MemoryMaintenanceSettings(
            automatic_organization_enabled=True,
            dreaming_enabled=False,
        )
        executor = Mock()
        organizer = Mock()
        runner = Mock()
        runner.run_once.return_value = {
            "schemaVersion": "rag-ime.personal-context-maintenance-run.v1",
            "ok": True,
            "activityTimelineCatchUp": {
                "ok": True,
                "completedDayCount": 1,
                "remainingDayCount": 14,
            },
            "targets": [],
        }
        progress = Mock()

        with (
            patch(
                "rag_ime.debug_server.build_governed_memory_model_executor",
                return_value=executor,
            ),
            patch(
                "rag_ime.debug_server.ManagedPiMemoryOrganizer",
                return_value=organizer,
            ),
            patch(
                "rag_ime.debug_server.PersonalContextMaintenanceRunner",
                return_value=runner,
            ) as runner_type,
        ):
            report = self.service._execute_gateway_memory_dreaming(
                project="wisdom-weasel-rag-ime",
                manual=False,
                managed=managed,
                max_sources=500,
                progress=progress,
            )

        self.assertTrue(report["ok"])
        config = runner_type.call_args.kwargs["config"]
        self.assertEqual(config.timeline_catch_up_limit, 1)
        runner.run_once.assert_called_once_with(force=False, progress=progress)
        organizer.close.assert_called_once_with()

    def test_activity_calendar_exposes_automatic_backfill_status(self) -> None:
        calendar = self.service.activity_timeline_calendar({"month": "2026-08"})

        self.assertTrue(calendar["automation"]["enabled"])
        self.assertEqual(calendar["automation"]["state"], "caught_up")
        self.assertEqual(calendar["automation"]["batchDayLimit"], 1)
        self.assertEqual(calendar["automation"]["remainingDayCount"], 0)

    def test_gateway_maintenance_skips_dreaming_only_when_both_lanes_are_disabled(
        self,
    ) -> None:
        managed = MemoryMaintenanceSettings(
            automatic_organization_enabled=False,
            dreaming_enabled=False,
        )

        with (
            patch(
                "rag_ime.debug_server.MemoryMaintenanceSettings.load",
                return_value=managed,
            ),
            patch(
                "rag_ime.debug_server.build_governed_memory_model_executor"
            ) as build_executor,
            patch(
                "rag_ime.debug_server.PersonalContextMaintenanceRunner"
            ) as runner_type,
        ):
            report = self.service._execute_gateway_memory_maintenance({})

        self.assertTrue(report["ok"])
        self.assertEqual(report["dreaming"]["reason"], "memory_maintenance_disabled")
        build_executor.assert_not_called()
        runner_type.assert_not_called()

    def test_gateway_maintenance_surfaces_dreaming_failure_in_combined_result(
        self,
    ) -> None:
        managed = MemoryMaintenanceSettings(
            automatic_organization_enabled=False,
            dreaming_enabled=True,
        )
        runner = Mock()
        runner.run_once.side_effect = RuntimeError("dreaming model unavailable")

        with (
            patch(
                "rag_ime.debug_server.MemoryMaintenanceSettings.load",
                return_value=managed,
            ),
            patch(
                "rag_ime.debug_server.build_governed_memory_model_executor",
                return_value=Mock(),
            ),
            patch(
                "rag_ime.debug_server.ManagedPiMemoryOrganizer",
                return_value=Mock(),
            ),
            patch(
                "rag_ime.debug_server.PersonalContextMaintenanceRunner",
                return_value=runner,
            ),
        ):
            report = self.service._execute_gateway_memory_maintenance({})

        self.assertFalse(report["ok"])
        self.assertFalse(report["dreaming"]["ok"])
        self.assertIn("dreaming model unavailable", report["dreaming"]["error"])

    def test_memory_catalog_filters_owner_scopes_and_reports_owner_labels(self) -> None:
        event_ref = self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=int(time.time() * 1000),
                source="pi_agent_user",
                committed_text="角色记忆归属测试",
                privacy_disposition="allowed",
                recent_context="",
                project="wisdom-weasel-rag-ime",
            )
        )
        event_id = int(event_ref.split(":", 1)[1])
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            for role_id, atom_id, text in (
                ("role-a", "atom:owner-catalog-a", "甲角色私有事实"),
                ("role-b", "atom:owner-catalog-b", "乙角色私有事实"),
            ):
                apply_memory_book_plan(
                    conn,
                    memory_book_plan_from_compile_output(
                        {
                            "memoryAtoms": [
                                {
                                    "atomId": atom_id,
                                    "canonicalText": text,
                                    "sourceEventIds": [event_id],
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

        page = self.service.management.memory_page(
            "atoms",
            page_request(
                {
                    "limit": 20,
                    "ownerKind": "agent",
                    "ownerId": "role-a",
                }
            ),
        )
        summary = self.service.management.memory_summary()

        self.assertEqual([item["id"] for item in page["items"]], ["atom:owner-catalog-a"])
        self.assertEqual(page["items"][0]["ownerKind"], "agent")
        self.assertEqual(page["items"][0]["ownerId"], "role-a")
        owners = {
            (item["ownerKind"], item["ownerId"])
            for item in summary["owners"]
        }
        self.assertIn(("agent", "role-a"), owners)
        self.assertIn(("agent", "role-b"), owners)

    def test_memory_source_forget_restore_is_audited_without_restoring_raw_input_to_evidence(self) -> None:
        event_ref, receipt = self.core.record_event_with_capture_receipt(
            InputEvent(
                event_id=None,
                created_at_ms=int(time.time() * 1000),
                source="squirrel_input_segment",
                committed_text="这条输入应该可以手动遗忘后恢复",
                privacy_disposition="allowed",
                project="wisdom-weasel-rag-ime",
                app="com.apple.TextEdit",
                capture_metadata=_capture_v2(
                    "这条输入应该可以手动遗忘后恢复",
                    capture_id="capture:management:forget-restore",
                ),
            )
        )
        self.assertTrue(event_ref.startswith("event:"))
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            source_id = str(
                conn.execute(
                    "SELECT source_id FROM agent_memory_sources WHERE input_event_id = ?",
                    (int(event_ref.split(":", 1)[1]),),
                ).fetchone()[0]
            )
            transition_evidence_admission(
                conn,
                str(receipt["evidenceId"]),
                new_state="admitted",
                reason_code="luna_personal_memory_confirmed",
                actor_kind="luna",
                created_at_ms=int(time.time() * 1000) + 1,
            )
        page = self.service.management.memory_page(
            "evidence",
            page_request(
                {
                    "limit": 20,
                    "ownerKind": "user",
                    "ownerId": "default",
                }
            ),
        )
        summary = self.service.management.memory_summary()

        forgotten = self.service.management.memory_source_disposition(
            {
                "sourceId": source_id,
                "disposition": "not_for_memory",
            }
        )
        forgotten_page = self.service.management.memory_page(
            "evidence",
            page_request({"limit": 20}),
        )
        restored = self.service.management.memory_source_disposition(
            {
                "sourceId": source_id,
                "disposition": "pending",
            }
        )
        restored_page = self.service.management.memory_page(
            "evidence",
            page_request({"limit": 20}),
        )

        self.assertEqual(summary["evidenceSourceCount"], 1)
        self.assertEqual(page["items"][0]["status"], "admitted")
        self.assertEqual(forgotten["source"]["disposition"], "not_for_memory")
        self.assertEqual(forgotten_page["items"], [])
        self.assertEqual(restored["source"]["disposition"], "needs_review")
        self.assertEqual(restored_page["items"], [])
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            transitions = conn.execute(
                """
                SELECT new_disposition, reason_code, actor_kind
                FROM memory_source_disposition_events
                WHERE source_id = ?
                ORDER BY created_at_ms, event_id
                """,
                (source_id,),
            ).fetchall()
        self.assertEqual(
            [tuple(row) for row in transitions[-2:]],
            [
                ("not_for_memory", "user_forgotten", "user"),
                ("needs_review", "user_restored_for_review", "user"),
            ],
        )
    def test_memory_evidence_catalog_never_exposes_sensitive_audit_sources(self) -> None:
        event_ref, receipt = self.core.record_event_with_capture_receipt(
            InputEvent(
                event_id=None,
                created_at_ms=int(time.time() * 1000),
                source="squirrel_input_segment",
                committed_text="临时 token=sk-abcdefghijk 不要显示",
                privacy_disposition="allowed",
                project="wisdom-weasel-rag-ime",
                app="com.apple.TextEdit",
                capture_metadata=_capture_v2(
                    "临时 token=sk-abcdefghijk 不要显示",
                    capture_id="capture:management:sensitive-evidence",
                ),
            )
        )

        # A typed capture whose text needs redaction must be acknowledged
        # without creating an input event or a memory source. This keeps the
        # native capture identity honest and prevents sensitive audit text from
        # entering the evidence catalog.
        self.assertEqual(event_ref, "skipped:typed_capture_requires_no_store")
        self.assertEqual(receipt["outcome"], "no_store")
        self.assertEqual(receipt["reason"], "typed_capture_requires_no_store")

        page = self.service.management.memory_page(
            "evidence",
            page_request({"limit": 20}),
        )

        self.assertEqual(page["items"], [])
        self.assertNotIn("sk-abcdefghijk", json.dumps(page, ensure_ascii=False))

    def test_agent_memory_run_review_and_apply_are_owner_scoped(self) -> None:
        event_ref = self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=int(time.time() * 1000),
                source="pi_agent_user",
                committed_text="甲角色的待审阅长期事实",
                privacy_disposition="allowed",
                recent_context="正在核对角色记忆来源",
                app="com.openai.codex",
                project="wisdom-weasel-rag-ime",
                context_group_id="app:codex",
            )
        )
        event_id = int(event_ref.split(":", 1)[1])
        plan = memory_book_plan_from_compile_output(
            {
                "memoryAtoms": [
                    {
                        "atomId": "atom:owner-review-a",
                        "canonicalText": "甲角色的待审阅长期事实",
                        "sourceEventIds": [event_id],
                    }
                ]
            },
            project="wisdom-weasel-rag-ime",
            provider="test",
            model="test",
            source_bundle={
                "inputs": [
                    {
                        "sourceRef": "S1",
                        "sourceId": "source:owner-review-a",
                        "sourceIds": ["source:owner-review-a"],
                        "sourceEventIds": [event_id],
                        "sourceKind": "user_final",
                        "source": "pi_agent_user",
                        "createdAtMs": 1_000,
                        "sourceOccurredAtMs": 1_000,
                        "app": "com.openai.codex",
                        "contextGroupId": "app:codex",
                    }
                ],
                "recentEvents": [
                    {
                        "eventId": event_id,
                        "sourceEventIds": [event_id],
                        "createdAtMs": 1_000,
                        "sourceOccurredAtMs": 1_000,
                        "source": "pi_agent_user",
                        "app": "com.openai.codex",
                        "contextGroupId": "app:codex",
                    }
                ],
            },
            owner_kind="agent",
            owner_id="role-a",
            run_kind="manual_curation",
        )
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            store_memory_book_plan(conn, plan)

        with self.assertRaisesRegex(ValueError, "owner scope"):
            self.service.agent_memory_maintenance_run(
                {
                    "runId": plan["runId"],
                    "project": "wisdom-weasel-rag-ime",
                    "visibleOwners": [
                        {"ownerKind": "agent", "ownerId": "role-b"}
                    ],
                }
            )
        review = self.service.agent_memory_maintenance_run(
            {
                "runId": plan["runId"],
                "project": "wisdom-weasel-rag-ime",
                "visibleOwners": [
                    {"ownerKind": "agent", "ownerId": "role-a"}
                ],
            }
        )
        self.assertEqual(
            review["run"]["changes"][0]["sourceEventIds"],
            [event_id],
        )
        self.assertEqual(
            review["run"]["sourceInputRefs"][0]["sourceEventIds"],
            [event_id],
        )
        self.assertEqual(
            review["run"]["sourceInputRefs"][0]["contextGroupId"],
            "app:codex",
        )
        with self.assertRaisesRegex(ValueError, "approved role"):
            self.service.knowledge_workbench_database_apply(
                {
                    "runId": plan["runId"],
                    "confirm": "apply",
                    "expectedOwnerKind": "agent",
                    "expectedOwnerId": "role-b",
                }
            )
        applied = self.service.knowledge_workbench_database_apply(
            {
                "runId": plan["runId"],
                "confirm": "apply",
                "expectedOwnerKind": "agent",
                "expectedOwnerId": "role-a",
            }
        )

        self.assertEqual(review["run"]["ownerKind"], "agent")
        self.assertEqual(review["run"]["ownerId"], "role-a")
        self.assertEqual(review["run"]["runKind"], "manual_curation")
        self.assertTrue(applied["ok"])

    def test_knowledge_database_draft_requires_confirmation_and_supports_rollback(self) -> None:
        event_ref = self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=1_900_000_100_045,
                source="manual",
                committed_text="数据库整理草案验证",
                privacy_disposition="allowed",
                recent_context="Memory Book",
                project="wisdom-weasel-rag-ime",
            )
        )
        event_id = int(event_ref.split(":", 1)[1])
        plan = memory_book_plan_from_compile_output(
            {
                "schemaVersion": "rag-ime.memory-book-compile.v1",
                "dailyBooks": [
                    {
                        "bookKey": "2026-07-10",
                        "title": "数据库整理草案",
                        "summary": "显式审阅后才应用。",
                        "sourceEventIds": [event_id],
                    }
                ],
                "memoryAtoms": [],
                "tagEdges": [],
                "phraseCandidates": [],
                "warnings": [],
            },
            project="wisdom-weasel-rag-ime",
            provider="deepseek",
            model="deepseek-test",
        )
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            store_memory_book_plan(conn, plan)

        blocked = self.service.knowledge_workbench_database_apply({"runId": plan["runId"]})
        draft_review = self.service.agent_memory_maintenance_run(
            {"runId": plan["runId"], "project": "wisdom-weasel-rag-ime"}
        )
        applied = self.service.knowledge_workbench_database_apply(
            {"runId": plan["runId"], "confirm": "apply"}
        )
        applied_review = self.service.agent_memory_maintenance_run(
            {"runId": plan["runId"], "project": "wisdom-weasel-rag-ime"}
        )
        rolled_back = self.service.knowledge_workbench_database_rollback(
            {"runId": plan["runId"], "confirm": "rollback"}
        )
        rolled_back_review = self.service.agent_memory_maintenance_run(
            {"runId": plan["runId"], "project": "wisdom-weasel-rag-ime"}
        )

        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["requiredConfirm"], "apply")
        self.assertTrue(draft_review["canApply"])
        self.assertFalse(draft_review["canRollback"])
        self.assertTrue(str(draft_review["revisionHash"]).startswith("sha256:"))
        self.assertNotIn("diffs", draft_review["run"])
        self.assertEqual(draft_review["run"]["changes"][0]["operationLabel"], "更新工具书")
        self.assertNotIn("payload", draft_review["run"]["changes"][0])
        self.assertEqual(applied["run"]["status"], "applied")
        self.assertFalse(applied_review["canApply"])
        self.assertTrue(applied_review["canRollback"])
        self.assertEqual(rolled_back["run"]["status"], "rolled_back")
        self.assertFalse(rolled_back_review["canRollback"])
        self.assertIn("retrieval", applied)

    def test_legacy_control_surfaces_are_removed_in_favor_of_the_electron_host(self) -> None:
        root = Path(__file__).resolve().parents[1]

        self.assertFalse((root / "debug" / "index.html").exists())
        self.assertFalse((root / "debug" / "app.js").exists())
        self.assertFalse((root / "debug" / "styles.css").exists())
        self.assertFalse((root / "macos" / "RagImeControl").exists())
        self.assertFalse((root / "macos" / "RagImeControlWebHost").exists())
        self.assertFalse((root / "scripts" / "build_control_center_web_host.sh").exists())
        self.assertTrue((root / "control-center-web" / "src" / "app" / "App.tsx").is_file())
        self.assertTrue((root / "control-center-web" / "electron" / "main.mjs").is_file())

    def test_memory_book_preview_is_dry_run_and_redacted(self) -> None:
        self.core.record_event_with_capture_receipt(
            InputEvent(
                event_id=None,
                created_at_ms=1_900_000_100_033,
                source="squirrel_input_segment",
                committed_text="真实历史整理入口",
                privacy_disposition="allowed",
                recent_context="不应该默认展示的上下文",
                project="wisdom-weasel-rag-ime",
                tags=("RAG",),
                app="com.apple.TextEdit",
                capture_metadata=_capture_v2(
                    "真实历史整理入口",
                    capture_id="capture:management:memory-book-preview",
                    occurred_at_ms=1_900_000_100_033,
                ),
            )
        )

        preview = self.service.rag_core_v3_memory_book_preview({"project": "wisdom-weasel-rag-ime", "limit": 10})
        blob = json.dumps(preview, ensure_ascii=False)

        self.assertTrue(preview["ok"])
        self.assertTrue(preview["dryRun"])
        self.assertFalse(preview["rawTextVisible"])
        self.assertNotIn("真实历史整理入口", blob)
        self.assertNotIn("不应该默认展示的上下文", blob)
        self.assertIn("textHash", blob)

    def test_management_http_routes_are_available(self) -> None:
        self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=1_900_000_100_040,
                source="manual",
                committed_text="HTTP 管理接口默认脱敏展示",
                privacy_disposition="allowed",
                recent_context="debug management route",
                project="wisdom-weasel-rag-ime",
            )
        )

        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = Path("debug")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(f"http://127.0.0.1:{server.server_port}/api/history?limit=3", timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["schemaVersion"], "rag-ime.management-history.v1")
        self.assertFalse(payload["rawTextVisible"])
        self.assertNotIn("text", payload["items"][0])

    def test_plugin_inventory_http_route_stays_structured_when_pi_is_unavailable(self) -> None:
        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = Path("debug")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(
                f"http://127.0.0.1:{server.server_port}/api/agent/extensions",
                timeout=5,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["schemaVersion"], "rag-ime.plugin-inventory.v1")
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["runtimeAvailable"])
        self.assertEqual(payload["items"], [])

    def test_tool_receipt_lookup_http_keeps_gateway_auth_and_exact_binding(self) -> None:
        session_id = str(self.service.agent.sessions.create(title="receipt lookup")["id"])
        with self.service.agent.sessions.approval_creation_scope(
            session_id=session_id, tool_call_id="call:http:lost-response",
        ):
            approval = self.service.agent.sessions.create_approval(
                session_id=session_id, tool_name="workspace_write", operation="apply",
                payload_sha256="a" * 64, preview={}, risk_level="R2",
            )
        approval_id = str(approval["approvalId"])
        self.service.agent.sessions.decide_approval(
            approval_id, approved=True, payload_sha256="a" * 64,
        )
        receipt = {"mutationApplied": True, "summary": "Already applied", "postimageSha256": "b" * 64}
        self.service.agent.sessions.complete_approval(approval_id, state="applied", receipt=receipt)

        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = Path("debug")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/api/agent/tool/approval-result"
        payload = {"sessionId": session_id, "toolCallId": "call:http:lost-response", "tool": "workspace_write"}

        def request(body, *, authenticated=True):
            headers = {"Content-Type": "application/json"}
            if authenticated:
                headers["X-RAG-IME-Agent-Token"] = self.service.agent.tool_token
            return Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")

        try:
            with self.assertRaises(HTTPError) as denied:
                urlopen(request(payload, authenticated=False), timeout=5)
            self.assertEqual(denied.exception.code, 403)
            denied.exception.close()
            with patch.object(self.service.agent_tools, "execute") as execute:
                for _ in range(2):
                    with urlopen(request(payload), timeout=5) as response:
                        result = json.loads(response.read())
                    self.assertEqual(result["lookupState"], "applied")
                    self.assertEqual(result["approval"]["receipt"], receipt)
                execute.assert_not_called()
            with self.assertRaises(HTTPError) as mismatch:
                urlopen(request({**payload, "tool": "workspace_shell"}), timeout=5)
            self.assertEqual(mismatch.exception.code, 400)
            mismatch.exception.close()
            with urlopen(request({"sessionId": session_id, "approvalId": approval_id}), timeout=5) as response:
                self.assertEqual(json.loads(response.read())["approval"]["receipt"], receipt)
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_agent_session_http_routes_are_operational(self) -> None:
        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = Path("debug")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}/api/agent"
        try:
            create_request = Request(
                f"{base_url}/sessions",
                data=json.dumps({"title": "连续对话"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(create_request, timeout=5) as response:
                created = json.loads(response.read().decode("utf-8"))
            session_id = created["session"]["id"]

            # Memory is opt-in at the Session capability boundary. Exercise
            # the real HTTP update before invoking the protected tool route.
            disclosure_request = Request(
                f"{base_url}/sessions/{session_id}",
                data=json.dumps(
                    {
                        "capabilityDisclosurePreferences": {
                            "tool:memory": "enabled",
                        }
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="PATCH",
            )
            with urlopen(disclosure_request, timeout=5) as response:
                disclosure = json.loads(response.read().decode("utf-8"))

            with urlopen(f"{base_url}/sessions", timeout=5) as response:
                listed = json.loads(response.read().decode("utf-8"))
            with urlopen(f"{base_url}/sessions?projectionOnly=1", timeout=5) as response:
                directory = json.loads(response.read().decode("utf-8"))
            with urlopen(f"{base_url}/runtime", timeout=5) as response:
                runtime = json.loads(response.read().decode("utf-8"))
            with urlopen(f"{base_url}/roles", timeout=5) as response:
                roles = json.loads(response.read().decode("utf-8"))
            with urlopen(f"{base_url}/memory-maintenance?limit=5", timeout=5) as response:
                maintenance = json.loads(response.read().decode("utf-8"))
            with urlopen(f"{base_url}/memory-maintenance?projectionOnly=1", timeout=5) as response:
                maintenance_activity = json.loads(response.read().decode("utf-8"))

            pi_model = {
                "provider": "openrouter",
                "id": "anthropic/claude-sonnet",
                "name": "Claude Sonnet",
                "api": "openai-completions",
                "reasoning": True,
                "thinkingLevels": ["off", "low", "medium", "high"],
                "supportsImages": True,
                "contextWindow": 200000,
                "maxTokens": 16384,
            }
            model_catalog_payload = {
                "schemaVersion": "rag-ime.agent-model-catalog.v1",
                "ok": True,
                "sessionId": session_id,
                "selected": pi_model,
                "thinkingLevel": "medium",
                "providers": [
                    {"id": "openrouter", "displayName": "OpenRouter", "models": [pi_model]}
                ],
            }
            model_selection_payload = {
                "schemaVersion": "rag-ime.agent-model-selection.v1",
                "ok": True,
                "sessionId": session_id,
                "selected": pi_model,
                "session": {
                    **created["session"],
                    "modelProfile": "openrouter/anthropic/claude-sonnet",
                },
            }
            command_catalog_payload = {
                "schemaVersion": "rag-ime.agent-command-catalog.v1",
                "ok": True,
                "sessionId": session_id,
                "runtimeAvailable": True,
                "items": [
                    {
                        "name": "review",
                        "invocation": "/review",
                        "description": "Review the active change",
                        "source": "extension",
                    }
                ],
            }
            command_invocation_payload = {
                "schemaVersion": "rag-ime.agent-command-invocation.v1",
                "ok": True,
                "sessionId": session_id,
                "command": "/workflow",
                "name": "workflow",
                "handled": True,
                "result": "No active workflow",
                "leafId": "entry-command-1",
            }
            fork_catalog_payload = {
                "schemaVersion": "rag-ime.agent-session-fork-candidates.v1",
                "ok": True,
                "sessionId": session_id,
                "items": [
                    {
                        "entryId": "entry-user-1",
                        "text": "从这里分支",
                        "role": "user",
                        "createdAtMs": 0,
                    }
                ],
            }
            fork_create_payload = {
                "schemaVersion": "rag-ime.agent-session-fork-create.v1",
                "ok": True,
                "sourceSessionId": session_id,
                "entryId": "entry-user-1",
                "selectedText": "从这里分支",
                "session": {**created["session"], "id": "agent:forked", "title": "新分支"},
            }
            with (
                patch.object(self.service.agent, "model_catalog", return_value=model_catalog_payload),
                patch.object(self.service.agent, "select_model", return_value=model_selection_payload),
                patch.object(self.service.agent, "command_catalog", return_value=command_catalog_payload),
                patch.object(self.service.agent, "invoke_command", return_value=command_invocation_payload),
                patch.object(self.service.agent, "fork_candidates", return_value=fork_catalog_payload),
                patch.object(self.service.agent, "fork_session", return_value=fork_create_payload),
            ):
                with urlopen(f"{base_url}/sessions/{session_id}/models", timeout=5) as response:
                    model_catalog = json.loads(response.read().decode("utf-8"))
                with urlopen(f"{base_url}/sessions/{session_id}/commands", timeout=5) as response:
                    command_catalog = json.loads(response.read().decode("utf-8"))
                command_request = Request(
                    f"{base_url}/sessions/{session_id}/commands",
                    data=json.dumps({"command": "/workflow"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(command_request, timeout=5) as response:
                    command_invocation = json.loads(response.read().decode("utf-8"))
                model_request = Request(
                    f"{base_url}/sessions/{session_id}/model",
                    data=json.dumps(
                        {"provider": "openrouter", "modelId": "anthropic/claude-sonnet"}
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(model_request, timeout=5) as response:
                    model_selection = json.loads(response.read().decode("utf-8"))
                with urlopen(f"{base_url}/sessions/{session_id}/forks", timeout=5) as response:
                    fork_catalog = json.loads(response.read().decode("utf-8"))
                fork_request = Request(
                    f"{base_url}/sessions/{session_id}/forks",
                    data=json.dumps({"entryId": "entry-user-1", "title": "新分支"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(fork_request, timeout=5) as response:
                    fork_status = response.status
                    fork_created = json.loads(response.read().decode("utf-8"))

            deep_runtime = {
                "schemaVersion": "rag-ime.agent-runtime.v1",
                "enabled": True,
                "status": "ready",
                "activeSessionId": session_id,
                "capabilities": {"rpc": True, "modelConfigured": True},
            }
            with (
                patch.object(self.service.agent, "runtime_status", return_value=deep_runtime),
                patch.object(
                    self.service.agent.runtime,
                    "prompt",
                    return_value={
                        "accepted": True,
                        "turnId": "turn:http:deep",
                        "piEntryId": "pi-entry:http:deep",
                        "response": {"success": True},
                    },
                ),
            ):
                deep_request = Request(
                    f"{base_url}/deep-search",
                    data=json.dumps(
                        {
                            "query": "继续深度查找",
                            "context": "输入法当前上下文",
                            "privacyDisposition": "allowed",
                            "evidence": [],
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(deep_request, timeout=5) as response:
                    deep_status = response.status
                    deep_search = json.loads(response.read().decode("utf-8"))

            denied_tool_request = Request(
                f"{base_url}/tool/execute",
                data=json.dumps(
                    {
                        "schemaVersion": "rag-ime.agent-tool-call.v1",
                        "sessionId": session_id,
                        "tool": "memory",
                        "toolCallId": "tool:http:1",
                        "args": {"op": "catalog", "query": "输入法"},
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as denied_context:
                urlopen(denied_tool_request, timeout=5)
            self.assertEqual(denied_context.exception.code, 403)
            denied_context.exception.close()

            allowed_tool_request = Request(
                f"{base_url}/tool/execute",
                data=denied_tool_request.data,
                headers={
                    "Content-Type": "application/json",
                    "X-RAG-IME-Agent-Token": self.service.agent.tool_token,
                },
                method="POST",
            )
            with urlopen(allowed_tool_request, timeout=5) as response:
                tool_result = json.loads(response.read().decode("utf-8"))

            self.service.agent.sessions.mutate_agent_goal(
                session_id,
                {
                    "action": "confirm_setup",
                    "confirmed": True,
                    "expectedRevision": 0,
                    "objective": "验证 Pi Goal 用量幂等上报",
                    "tokenBudget": 1_000,
                },
            )
            workflow_request = Request(
                f"{base_url}/tool/workflow-state",
                data=json.dumps({"sessionId": session_id}).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-RAG-IME-Agent-Token": self.service.agent.tool_token,
                },
                method="POST",
            )
            with urlopen(workflow_request, timeout=5) as response:
                workflow_state = json.loads(response.read().decode("utf-8"))
            goal_usage_body = json.dumps(
                {
                    "sessionId": session_id,
                    "turnId": "turn:http:usage",
                    "eventId": "event:http:usage",
                    "idempotencyKey": "goal-usage:http:turn-1",
                    "tokenDelta": 120,
                    "elapsedDeltaMs": 250,
                }
            ).encode("utf-8")
            goal_usage_request = Request(
                f"{base_url}/tool/goal-usage",
                data=goal_usage_body,
                headers={
                    "Content-Type": "application/json",
                    "X-RAG-IME-Agent-Token": self.service.agent.tool_token,
                },
                method="POST",
            )
            with urlopen(goal_usage_request, timeout=5) as response:
                goal_usage = json.loads(response.read().decode("utf-8"))
            with urlopen(goal_usage_request, timeout=5) as response:
                duplicate_goal_usage = json.loads(response.read().decode("utf-8"))
            goal_settle_request = Request(
                f"{base_url}/tool/goal-settle",
                data=json.dumps(
                    {
                        "schemaVersion": "rag-ime.agent-goal-settle-request.v1",
                        "sessionId": session_id,
                        "settleScopeId": "scope:http:goal-settle",
                        "settleAttempt": 1,
                        "freshToolEvidenceCount": 0,
                        "freshToolEvidenceSha256": hashlib.sha256(
                            b""
                        ).hexdigest(),
                    }
                ).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-RAG-IME-Agent-Token": self.service.agent.tool_token,
                },
                method="POST",
            )
            with urlopen(goal_settle_request, timeout=5) as response:
                goal_settle = json.loads(response.read().decode("utf-8"))

            context_refresh_body = json.dumps(
                {
                    "schemaVersion": "rag-ime.agent-session-context-refresh-request.v1",
                    "sessionId": session_id,
                    "trigger": "session_start",
                    "queryText": "验证 Session 记忆注入",
                    "recentMessages": [
                        {"role": "user", "text": "验证 Session 记忆注入"},
                    ],
                }
            ).encode("utf-8")
            denied_context_refresh = Request(
                f"{base_url}/tool/context-refresh",
                data=context_refresh_body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as denied_refresh_context:
                urlopen(denied_context_refresh, timeout=5)
            self.assertEqual(denied_refresh_context.exception.code, 403)
            denied_refresh_context.exception.close()

            allowed_context_refresh = Request(
                f"{base_url}/tool/context-refresh",
                data=context_refresh_body,
                headers={
                    "Content-Type": "application/json",
                    "X-RAG-IME-Agent-Token": self.service.agent.tool_token,
                },
                method="POST",
            )
            with urlopen(allowed_context_refresh, timeout=5) as response:
                context_refresh = json.loads(response.read().decode("utf-8"))

            approval = self.service.agent.sessions.create_approval(
                session_id=session_id,
                tool_name="input",
                operation="apply_settings",
                payload_sha256="a" * 64,
                preview={"summary": "关闭模糊音"},
                risk_level="R1",
            )
            with urlopen(f"{base_url}/approvals?sessionId={session_id}", timeout=5) as response:
                approvals = json.loads(response.read().decode("utf-8"))
            decision_request = Request(
                f"{base_url}/approvals/{approval['approvalId']}/decision",
                data=json.dumps({"decision": "reject", "payloadSha256": "a" * 64}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(decision_request, timeout=5) as response:
                decision = json.loads(response.read().decode("utf-8"))

            approval_result_request = Request(
                f"{base_url}/tool/approval-result",
                data=json.dumps(
                    {"sessionId": session_id, "approvalId": approval["approvalId"]}
                ).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-RAG-IME-Agent-Token": self.service.agent.tool_token,
                },
                method="POST",
            )
            with urlopen(approval_result_request, timeout=5) as response:
                approval_result = json.loads(response.read().decode("utf-8"))

            media_import_request = Request(
                f"{base_url}/media/import?sessionId={session_id}&fileName=screen.png",
                data=PNG_1X1,
                headers={"Content-Type": "image/png"},
                method="POST",
            )
            with urlopen(media_import_request, timeout=5) as response:
                media_import = json.loads(response.read().decode("utf-8"))
            media_id = media_import["media"]["mediaId"]
            with urlopen(
                f"{base_url}/media/{media_id}/receipt?sessionId={session_id}",
                timeout=5,
            ) as response:
                media_receipt = json.loads(response.read().decode("utf-8"))
            with urlopen(
                f"{base_url}/media/{media_id}/content?sessionId={session_id}",
                timeout=5,
            ) as response:
                media_content = response.read()
                media_headers = response.headers
            with urlopen(f"{base_url}/media?sessionId={session_id}", timeout=5) as response:
                media_list = json.loads(response.read().decode("utf-8"))

            context_item = self.service.agent.context_runtime.enqueue(
                session_id=session_id,
                source_kind="test_task",
                source_id="task:http",
                lane="status",
                lifecycle="until_ack",
                title="异步任务已完成",
                summary="等待确认",
            )
            trace_id = self.service.agent.context_runtime.begin_trace(
                session_id,
                source_kind="user",
            )
            self.service.agent.context_runtime.add_trace_node(
                trace_id,
                stage="input",
                label="当前输入",
                source_kind="user",
                content="private prompt",
            )
            self.service.agent.context_runtime.finalize_trace(
                trace_id,
                status="accepted",
                turn_id="turn:http:trace",
                final_content="private prompt",
            )
            encoded_session = quote(session_id, safe="")
            encoded_item = quote(str(context_item["itemId"]), safe="")
            encoded_trace = quote(trace_id, safe="")
            with urlopen(
                f"{base_url}/sessions/{encoded_session}/context-items?limit=10",
                timeout=5,
            ) as response:
                context_items = json.loads(response.read().decode("utf-8"))
            with urlopen(
                f"{base_url}/sessions/{encoded_session}/context-traces?limit=10",
                timeout=5,
            ) as response:
                context_traces = json.loads(response.read().decode("utf-8"))
            with urlopen(
                f"{base_url}/sessions/{encoded_session}/context-traces/{encoded_trace}",
                timeout=5,
            ) as response:
                context_trace = json.loads(response.read().decode("utf-8"))
            context_ack_request = Request(
                (
                    f"{base_url}/sessions/{encoded_session}/context-items/"
                    f"{encoded_item}/ack"
                ),
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(context_ack_request, timeout=5) as response:
                context_ack = json.loads(response.read().decode("utf-8"))

            update_request = Request(
                f"{base_url}/sessions/{session_id}",
                data=json.dumps({"title": "深度检索"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="PATCH",
            )
            with urlopen(update_request, timeout=5) as response:
                updated = json.loads(response.read().decode("utf-8"))

            delete_request = Request(
                f"{base_url}/sessions/{session_id}",
                method="DELETE",
            )
            with urlopen(delete_request, timeout=5) as response:
                deleted = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertTrue(created["ok"])
        self.assertEqual(
            disclosure["session"]["capabilityDisclosurePreferences"],
            {"tool:memory": "enabled"},
        )
        self.assertEqual(listed["items"][0]["id"], session_id)
        self.assertEqual(directory["items"][0]["id"], session_id)
        self.assertNotIn("sessionFile", directory["items"][0])
        self.assertEqual(runtime["status"], "disabled")
        self.assertEqual(roles["items"][0]["displayName"], "Agent 3")
        self.assertNotIn("systemPrompt", roles["items"][0])
        self.assertEqual(maintenance["policy"], "auto_governed")
        self.assertTrue(maintenance["autoApply"])
        self.assertFalse(maintenance["scheduledDraftOnly"])
        self.assertEqual(maintenance["catalogConsolidation"]["state"], "never")
        self.assertEqual(
            set(maintenance_activity),
            {"schemaVersion", "ok", "job", "catalogConsolidation", "projection"},
        )
        self.assertEqual(
            maintenance_activity["schemaVersion"],
            "rag-ime.agent-memory-maintenance-status.v1",
        )
        self.assertEqual(model_catalog["providers"][0]["displayName"], "OpenRouter")
        self.assertEqual(command_catalog["items"][0]["invocation"], "/review")
        self.assertTrue(command_invocation["handled"])
        self.assertEqual(command_invocation["result"], "No active workflow")
        self.assertEqual(
            model_selection["session"]["modelProfile"],
            "openrouter/anthropic/claude-sonnet",
        )
        self.assertEqual(fork_catalog["items"][0]["entryId"], "entry-user-1")
        self.assertEqual(fork_status, 201)
        self.assertEqual(fork_created["session"]["id"], "agent:forked")
        self.assertEqual(deep_status, 202)
        self.assertEqual(deep_search["schemaVersion"], "rag-ime.agent-deep-search.v1")
        self.assertEqual(deep_search["sessionId"], session_id)
        self.assertEqual(deep_search["turnId"], "turn:http:deep")
        self.assertTrue(tool_result["ok"])
        self.assertEqual(tool_result["operation"], "catalog")
        self.assertEqual(set(workflow_state["result"]), {"todo", "goal", "actGate"})
        self.assertEqual(goal_usage["result"]["goal"]["usage"]["tokens"], 120)
        self.assertEqual(duplicate_goal_usage["result"]["goal"]["usage"]["tokens"], 120)
        self.assertEqual(goal_settle["result"]["state"], "continue")
        self.assertEqual(
            goal_settle["result"]["reason"],
            "goal_active",
        )
        self.assertTrue(context_refresh["ok"])
        self.assertEqual(context_refresh["result"]["trigger"], "first_user_prompt")
        self.assertEqual(context_refresh["result"]["sourceCount"], 0)
        # An empty recall still carries the coverage boundary; it must not
        # imply that the user has never expressed a relevant preference.
        self.assertIn("记忆整理覆盖范围尚未确认", context_refresh["result"]["sessionContext"])
        self.assertIn("无命中不代表用户没有表达过", context_refresh["result"]["sessionContext"])
        self.assertEqual(approvals["items"][0]["approvalId"], approval["approvalId"])
        self.assertEqual(decision["approval"]["state"], "rejected")
        self.assertEqual(approval_result["approval"]["state"], "rejected")
        self.assertEqual(media_receipt["media"]["mediaId"], media_id)
        self.assertEqual(media_content, PNG_1X1)
        self.assertEqual(media_headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(media_list["items"][0]["fileName"], "screen.png")
        self.assertEqual(context_items["items"][0]["title"], "异步任务已完成")
        self.assertEqual(context_traces["items"][0]["traceId"], trace_id)
        self.assertEqual(context_trace["nodes"][0]["label"], "当前输入")
        self.assertNotIn("private prompt", json.dumps(context_trace))
        self.assertEqual(context_ack["item"]["status"], "acknowledged")
        self.assertEqual(updated["session"]["title"], "深度检索")
        self.assertEqual(deleted["sessionId"], session_id)

    def test_memory_maintenance_missing_job_is_json_expired_after_gateway_restart(self) -> None:
        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = Path("debug")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        job_id = "memory-maintenance:after-gateway-restart"
        try:
            with urlopen(
                f"http://127.0.0.1:{server.server_port}/api/agent/memory-maintenance"
                f"?jobId={quote(job_id)}",
                timeout=5,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
                status_code = response.status
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(status_code, 200)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["jobId"], job_id)
        self.assertEqual(payload["state"], "expired")
        self.assertFalse(payload["recovery"]["recoverable"])
        self.assertTrue(payload["recovery"]["retryable"])
        self.assertEqual(payload["recovery"]["action"], "trigger_new_job")

    def test_agent_session_models_projects_missing_workspace_as_json_conflict(self) -> None:
        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = Path("debug")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}/api/agent"
        try:
            with patch.object(
                self.service.agent,
                "model_catalog",
                side_effect=AgentRuntimeError(
                    "Workspace does not exist: /private/tmp/expired/workspace"
                ),
            ):
                with self.assertRaises(HTTPError) as raised:
                    urlopen(f"{base_url}/sessions/agent%3Astale/models", timeout=5)
                failure = raised.exception
                payload = json.loads(failure.read().decode("utf-8"))
                failure.close()

            with patch.object(
                self.service.agent.message_snapshot,
                "messages",
                side_effect=AgentRuntimeError(
                    "Workspace does not exist: /private/tmp/expired/workspace"
                ),
            ):
                with self.assertRaises(HTTPError) as messages_raised:
                    urlopen(
                        f"{base_url}/sessions/agent%3Astale/messages",
                        timeout=5,
                    )
                messages_failure = messages_raised.exception
                messages_payload = json.loads(
                    messages_failure.read().decode("utf-8")
                )
                messages_failure.close()

            with patch.object(
                self.service.agent,
                "prompt",
                side_effect=AgentRuntimeError(
                    "Workspace does not exist: /private/tmp/expired/workspace"
                ),
            ):
                prompt_request = Request(
                    f"{base_url}/sessions/agent%3Astale/prompt",
                    data=json.dumps(
                        {
                            "message": "继续",
                            "clientMessageId": "workspace-recovery-test",
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(HTTPError) as prompt_raised:
                    urlopen(prompt_request, timeout=5)
                prompt_failure = prompt_raised.exception
                prompt_payload = json.loads(
                    prompt_failure.read().decode("utf-8")
                )
                prompt_failure.close()

            # The failed request returned a typed response rather than dropping
            # the socket; the same server remains usable afterwards.
            with urlopen(f"{base_url}/sessions?limit=1", timeout=5) as response:
                healthy_status = response.status
                healthy_payload = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(failure.code, 409)
        self.assertEqual(payload["schemaVersion"], "rag-ime.local-api-error.v1")
        self.assertEqual(payload["errorCode"], "session_workspace_missing")
        self.assertFalse(payload["retryable"])
        self.assertEqual(payload["recovery"]["action"], "select_workspace")
        self.assertNotIn("/private/", json.dumps(payload))
        self.assertEqual(messages_failure.code, 409)
        self.assertEqual(messages_payload, payload)
        self.assertEqual(prompt_failure.code, 409)
        self.assertEqual(prompt_payload, payload)
        self.assertEqual(healthy_status, 200)
        self.assertTrue(healthy_payload["ok"])

    def test_agent_file_preview_http_route_preserves_session_and_digest_authority(self) -> None:
        session = self.service.agent.create_session({"title": "文件预览"})["session"]
        session_id = str(session["id"])
        other = self.service.agent.create_session({"title": "其他会话"})["session"]
        other_session_id = str(other["id"])
        markdown = "# 交付\n\n- 已验证".encode("utf-8")

        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = Path("debug")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}/api/agent"
        try:
            request = Request(
                f"{base_url}/media/import?sessionId={quote(session_id, safe='')}&fileName=handoff.md",
                data=markdown,
                headers={"Content-Type": "text/markdown"},
                method="POST",
            )
            with urlopen(request, timeout=5) as response:
                imported = json.loads(response.read().decode("utf-8"))
            receipt = imported["media"]
            media_id = str(receipt["mediaId"])
            digest = str(receipt["sha256"])
            preview_url = (
                f"{base_url}/media/{quote(media_id, safe='')}/preview"
                f"?sessionId={quote(session_id, safe='')}&sha256={digest}"
            )
            with urlopen(preview_url, timeout=5) as response:
                preview = json.loads(response.read().decode("utf-8"))
            with urlopen(
                f"{base_url}/media/{quote(media_id, safe='')}/content"
                f"?sessionId={quote(session_id, safe='')}",
                timeout=5,
            ) as response:
                self.assertEqual(response.read(), markdown)
                self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
                self.assertIn("sandbox", response.headers["Content-Security-Policy"])
                self.assertEqual(response.headers["Cache-Control"], "private, no-store")

            with self.assertRaises(HTTPError) as wrong_session:
                urlopen(
                    f"{base_url}/media/{quote(media_id, safe='')}/preview"
                    f"?sessionId={quote(other_session_id, safe='')}",
                    timeout=5,
                )
            self.assertEqual(wrong_session.exception.code, 400)
            wrong_session.exception.close()

            with self.assertRaises(HTTPError) as wrong_digest:
                urlopen(
                    f"{base_url}/media/{quote(media_id, safe='')}/preview"
                    f"?sessionId={quote(session_id, safe='')}&sha256={'f' * 64}",
                    timeout=5,
                )
            self.assertEqual(wrong_digest.exception.code, 400)
            wrong_digest.exception.close()
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(preview["schemaVersion"], "rag-ime.agent-file-preview.v1")
        self.assertEqual(preview["descriptor"]["previewKind"], "markdown")
        self.assertEqual(preview["descriptor"]["sessionId"], session_id)
        self.assertEqual(preview["descriptor"]["sha256"], digest)
        self.assertEqual(preview["content"], markdown.decode("utf-8"))

    def test_isolated_html_preview_bootstrap_has_opaque_script_capability(self) -> None:
        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = Path("debug")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(
                f"http://127.0.0.1:{server.server_port}/__paw_html_preview",
                timeout=5,
            ) as response:
                body = response.read().decode("utf-8")
                csp = response.headers["Content-Security-Policy"]
                cache_control = response.headers["Cache-Control"]
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertIn("document.write(source)", body)
        self.assertIn("script-src 'unsafe-inline'", csp)
        self.assertIn("sandbox", csp)
        self.assertIn("allow-scripts", csp)
        self.assertNotIn("allow-same-origin", csp)
        self.assertEqual(cache_control, "private, no-store")

    def test_lifecycle_hook_http_routes_require_runtime_token_and_keep_policy_product_owned(self) -> None:
        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = Path("debug")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}/api/agent"
        body = json.dumps(
            {
                "schemaVersion": "rag-ime.agent-lifecycle-event.v1",
                "eventId": "http-lifecycle-1",
                "eventType": "tool_failed",
                "sessionId": "session-http",
                "payload": {
                    "facts": [],
                    "auditOnly": True,
                    "reason": "tool_failure_is_not_a_durable_memory_fact",
                },
            }
        ).encode("utf-8")
        try:
            denied = Request(
                f"{base_url}/tool/lifecycle-event",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as denied_context:
                urlopen(denied, timeout=5)
            self.assertEqual(denied_context.exception.code, 403)
            denied_context.exception.close()

            allowed = Request(
                f"{base_url}/tool/lifecycle-event",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-RAG-IME-Agent-Token": self.service.agent.tool_token,
                },
                method="POST",
            )
            with urlopen(allowed, timeout=5) as response:
                event = json.loads(response.read().decode("utf-8"))
            with urlopen(f"{base_url}/lifecycle-hooks?limit=5", timeout=5) as response:
                snapshot = json.loads(response.read().decode("utf-8"))

            update = Request(
                f"{base_url}/lifecycle-hooks",
                data=json.dumps({"eventType": "idle", "enabled": False}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="PATCH",
            )
            with urlopen(update, timeout=5) as response:
                updated = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(event["result"]["status"], "recorded")
        self.assertEqual(event["result"]["nextTurnContext"], "")
        self.assertFalse(event["guardrails"]["writesLongTermMemory"])
        self.assertEqual(snapshot["recentEvents"][0]["eventId"], "http-lifecycle-1")
        idle = next(value for value in updated["policies"] if value["eventType"] == "idle")
        self.assertFalse(idle["enabled"])

    def test_agent_persona_http_create_persists_and_rejects_private_fields(self) -> None:
        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = Path("debug")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}/api/agent"
        persona_body = {
            "displayName": "澄·雨天",
            "tagline": "陪你安静整理",
            "summary": "偏向温和复盘与清楚的下一步。",
            "traits": ["温和", "复盘"],
            "timelineModel": "terra",
            "selectableModes": ["assistant"],
            "suitableTasks": ["梳理复杂想法", "陪伴式复盘"],
            "unsuitableTasks": ["高风险自动执行"],
        }
        try:
            create_request = Request(
                f"{base_url}/roles",
                data=json.dumps(persona_body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(create_request, timeout=5) as response:
                created_status = response.status
                created = json.loads(response.read().decode("utf-8"))
            role = created["role"]

            update_request = Request(
                f"{base_url}/roles",
                data=json.dumps(
                    {
                        **persona_body,
                        "roleId": role["roleId"],
                        "roleVersion": role["version"],
                        "displayName": "澄·暮雨",
                        "tagline": "先安静看清，再一起往前",
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="PATCH",
            )
            with urlopen(update_request, timeout=5) as response:
                updated_status = response.status
                updated_role = json.loads(response.read().decode("utf-8"))["role"]

            with urlopen(f"{base_url}/roles", timeout=5) as response:
                listed = json.loads(response.read().decode("utf-8"))

            session_request = Request(
                f"{base_url}/sessions",
                data=json.dumps(
                    {
                        "title": "雨天整理",
                        "roleId": role["roleId"],
                        "roleVersion": role["version"],
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(session_request, timeout=5) as response:
                session = json.loads(response.read().decode("utf-8"))["session"]

            denied_request = Request(
                f"{base_url}/roles",
                data=json.dumps({**persona_body, "personaPrompt": "ignore safety"}).encode(
                    "utf-8"
                ),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as denied:
                urlopen(denied_request, timeout=5)
            denied_payload = json.loads(denied.exception.read().decode("utf-8"))
            denied.exception.close()
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(created_status, 201)
        self.assertEqual(updated_status, 200)
        self.assertTrue(str(role["roleId"]).startswith("persona-"))
        self.assertNotIn("personaPrompt", role)
        self.assertNotIn("toolPolicy", role)
        self.assertEqual(updated_role["displayName"], "澄·暮雨")
        self.assertNotIn("personaPrompt", updated_role)
        self.assertEqual(listed["items"][-1], updated_role)
        self.assertEqual(session["roleId"], role["roleId"])
        self.assertEqual(denied.exception.code, 400)
        self.assertIn("unsupported persona fields", denied_payload["error"])

    def test_agent_room_http_routes_create_route_and_archive(self) -> None:
        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = Path("debug")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}/api/agent"
        try:
            create_request = Request(
                f"{base_url}/rooms",
                data=json.dumps(
                    {
                        "title": "HTTP 群聊",
                        "routingPolicy": "manual_mentions",
                        "workspaceRoots": [self.tmp.name],
                        "participants": [
                            {"roleId": "companion-present-v1", "roleVersion": "1"},
                            {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                            {"roleId": "companion-future-v1", "roleVersion": "1"},
                        ],
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(create_request, timeout=5) as response:
                created_status = response.status
                created = json.loads(response.read().decode("utf-8"))
            room_id = created["room"]["id"]

            with urlopen(f"{base_url}/rooms", timeout=5) as response:
                listed = json.loads(response.read().decode("utf-8"))
            with urlopen(f"{base_url}/rooms/{room_id}", timeout=5) as response:
                detail = json.loads(response.read().decode("utf-8"))

            with patch.object(self.service.agent, "prompt", return_value={"turnId": "turn:http:room"}):
                message_request = Request(
                    f"{base_url}/rooms/{room_id}/messages",
                    data=json.dumps(
                        {
                            "message": "@Agent 2 请诊断状态",
                            "clientMessageId": "room-http-client-1",
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(message_request, timeout=5) as response:
                    message_status = response.status
                    accepted = json.loads(response.read().decode("utf-8"))

            abort_receipt = {
                "schemaVersion": "rag-ime.agent-room-abort.v1",
                "ok": True,
                "roomId": room_id,
                "roomTurnId": accepted["roomTurnId"],
                "status": "terminated",
                "cancellationReceiptId": "room-cancel:http",
                "surfaces": {},
                "pendingTargets": [],
            }
            with patch.object(
                self.service.agent,
                "abort_room_turn",
                return_value=abort_receipt,
            ) as abort_room_turn:
                abort_request = Request(
                    f"{base_url}/rooms/{room_id}/abort",
                    data=json.dumps(
                        {
                            "roomTurnId": accepted["roomTurnId"],
                            "clientRequestId": "room-http-abort-1",
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(abort_request, timeout=5) as response:
                    abort_status = response.status
                    aborted = json.loads(response.read().decode("utf-8"))

            with urlopen(f"{base_url}/rooms/{room_id}/snapshot", timeout=5) as response:
                snapshot = json.loads(response.read().decode("utf-8"))

            archive_request = Request(
                f"{base_url}/rooms/{room_id}",
                data=json.dumps({"archived": True}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="PATCH",
            )
            with urlopen(archive_request, timeout=5) as response:
                archived = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(created_status, 201)
        self.assertEqual(len(created["room"]["participants"]), 3)
        self.assertEqual(listed["items"][0]["id"], room_id)
        self.assertEqual(detail["room"]["id"], room_id)
        self.assertEqual(message_status, 202)
        self.assertEqual(accepted["participant"]["roleId"], "companion-firstlight-v1")
        self.assertEqual(accepted["clientMessageId"], "room-http-client-1")
        self.assertEqual(abort_status, 200)
        self.assertEqual(aborted, abort_receipt)
        abort_room_turn.assert_called_once_with(
            room_id,
            {
                "roomTurnId": accepted["roomTurnId"],
                "clientRequestId": "room-http-abort-1",
            },
        )
        self.assertEqual(snapshot["schemaVersion"], "rag-ime.agent-room-snapshot.v1")
        self.assertEqual(snapshot["room"]["id"], room_id)
        self.assertEqual(snapshot["lastSequence"], snapshot["room"]["lastEventSequence"])
        user_event = next(
            event for event in snapshot["events"] if event["eventType"] == "user_message"
        )
        self.assertEqual(user_event["payload"]["clientMessageId"], "room-http-client-1")
        self.assertEqual(archived["room"]["status"], "archived")

    def test_agent_intercom_and_artifact_http_routes_preserve_session_authority(self) -> None:
        session_id = "agent:http-room-source"
        intercom_payload = {
            "schemaVersion": "rag-ime.agent-room-intercom-enqueue.v1",
            "ok": True,
            "accepted": True,
            "message": {"id": "room-message:http"},
        }
        mailbox_payload = {
            "schemaVersion": "rag-ime.agent-room-intercom-list.v1",
            "ok": True,
            "sessionId": session_id,
            "items": [],
        }
        artifact_payload = {
            "schemaVersion": "rag-ime.agent-artifact-inspection.v1",
            "artifact": {"artifactId": "artifact:http"},
            "records": [],
            "totalRecords": 0,
            "returnedRecords": 0,
            "truncated": False,
            "limits": {"requestedRecords": 5, "maxRecords": 500, "maxOutputBytes": 262144},
        }

        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = Path("debug")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}/api/agent"
        try:
            with (
                patch.object(
                    self.service.agent,
                    "send_room_intercom",
                    return_value=intercom_payload,
                ) as send,
                patch.object(
                    self.service.agent,
                    "list_room_intercom",
                    return_value=mailbox_payload,
                ) as mailbox,
                patch.object(
                    self.service.agent,
                    "delegation_artifact",
                    return_value=artifact_payload,
                ) as artifact,
            ):
                request = Request(
                    f"{base_url}/sessions/{session_id}/intercom",
                    data=json.dumps(
                        {
                            "kind": "send",
                            "targetParticipantId": "participant:target",
                            "clientMessageId": "http-1",
                            "content": "请复核",
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=5) as response:
                    posted_status = response.status
                    posted = json.loads(response.read().decode("utf-8"))
                with urlopen(
                    f"{base_url}/sessions/{session_id}/intercom?status=queued&limit=5",
                    timeout=5,
                ) as response:
                    mailbox_result = json.loads(response.read().decode("utf-8"))
                with urlopen(
                    f"{base_url}/artifacts/artifact:http?sessionId={session_id}&limit=5",
                    timeout=5,
                ) as response:
                    artifact_result = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(posted_status, 202)
        self.assertEqual(posted["message"]["id"], "room-message:http")
        self.assertEqual(mailbox_result["sessionId"], session_id)
        self.assertEqual(artifact_result["artifact"]["artifactId"], "artifact:http")
        self.assertEqual(send.call_args.args[0], session_id)
        self.assertEqual(mailbox.call_args.args[0], session_id)
        self.assertEqual(artifact.call_args.args[:2], (session_id, "artifact:http"))

    def test_agent_intercom_get_returns_json_4xx_for_non_room_session(self) -> None:
        session_id = self.service.agent.create_session({"title": "普通会话"})["session"]["id"]

        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = Path("debug")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            with self.assertRaises(HTTPError) as caught:
                urlopen(f"{base_url}/api/agent/sessions/{session_id}/intercom", timeout=5)
            try:
                error_status = caught.exception.code
                content_type = caught.exception.headers.get_content_type()
                payload = json.loads(caught.exception.read().decode("utf-8"))
            finally:
                caught.exception.close()

            with urlopen(f"{base_url}/api/health", timeout=5) as response:
                health = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(error_status, 400)
        self.assertEqual(content_type, "application/json")
        self.assertEqual(
            payload,
            {
                "schemaVersion": "rag-ime.local-api-error.v1",
                "ok": False,
                "errorCode": "invalid_request",
                "error": "session is not a room participant",
            },
        )
        self.assertTrue(health["ok"])

    def test_agent_tool_validation_error_is_non_retryable(self) -> None:
        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = Path("debug")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        request = Request(
            f"{base_url}/api/agent/tool/execute",
            data=json.dumps(
                {
                    "schemaVersion": "rag-ime.agent-tool-call.v1",
                    "sessionId": "session:test",
                    "tool": "memory",
                    "toolCallId": "tool:test:invalid",
                    "args": {"op": "curation_prepare"},
                }
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-RAG-IME-Agent-Token": self.service.agent.tool_token,
            },
            method="POST",
        )
        try:
            with (
                patch.object(
                    self.service.agent_tools,
                    "execute",
                    side_effect=ValueError(
                        "$.segments[1].title: string is longer than 160"
                    ),
                ),
                patch.dict(
                    os.environ,
                    {
                        "NO_PROXY": "127.0.0.1,localhost",
                        "no_proxy": "127.0.0.1,localhost",
                    },
                ),
                self.assertRaises(HTTPError) as caught,
            ):
                urlopen(request, timeout=5)
            try:
                error_status = caught.exception.code
                payload = json.loads(caught.exception.read().decode("utf-8"))
            finally:
                caught.exception.close()
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(error_status, 400, payload)
        self.assertEqual(
            payload,
            {
                "schemaVersion": "rag-ime.agent-tool-error.v1",
                "ok": False,
                "error": "$.segments[1].title: string is longer than 160",
                "errorCode": "invalid_request",
                "retryable": False,
            },
        )

    def test_agent_tool_stale_snapshot_is_a_typed_retryable_conflict(self) -> None:
        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = Path("debug")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        request = Request(
            f"http://127.0.0.1:{server.server_port}/api/agent/tool/execute",
            data=json.dumps(
                {
                    "schemaVersion": "rag-ime.agent-tool-call.v1",
                    "sessionId": "session:test",
                    "tool": "workspace_edit",
                    "toolCallId": "tool:test:stale-snapshot",
                    "args": {
                        "op": "apply",
                        "path": "example.py",
                        "resourceRevision": f"sha256:{'a' * 64}",
                        "edits": [{"oldText": "before", "newText": "after"}],
                    },
                }
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-RAG-IME-Agent-Token": self.service.agent.tool_token,
            },
            method="POST",
        )
        try:
            with (
                patch.object(
                    self.service.agent_tools,
                    "execute",
                    side_effect=WorkspaceSnapshotError(
                        "stale_snapshot",
                        "workspace_edit snapshot is stale",
                        retryable=True,
                    ),
                ),
                patch.dict(
                    os.environ,
                    {
                        "NO_PROXY": "127.0.0.1,localhost",
                        "no_proxy": "127.0.0.1,localhost",
                    },
                ),
                self.assertRaises(HTTPError) as caught,
            ):
                urlopen(request, timeout=5)
            try:
                status = caught.exception.code
                payload = json.loads(caught.exception.read().decode("utf-8"))
            finally:
                caught.exception.close()
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["errorCode"], "stale_snapshot")
        self.assertTrue(payload["retryable"])

    def test_agent_tool_workflow_gate_is_a_typed_non_retryable_conflict(self) -> None:
        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = Path("debug")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        request = Request(
            f"{base_url}/api/agent/tool/execute",
            data=json.dumps(
                {
                    "schemaVersion": "rag-ime.agent-tool-call.v1",
                    "sessionId": "session:test",
                    "tool": "workspace_edit",
                    "toolCallId": "tool:test:plan-required",
                    "args": {"op": "apply", "path": "example.py", "edits": []},
                }
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-RAG-IME-Agent-Token": self.service.agent.tool_token,
            },
            method="POST",
        )
        try:
            with (
                patch.object(
                    self.service.agent_tools,
                    "execute",
                    side_effect=ValueError(
                        "Act Gate blocked workspace mutation (plan_required): "
                        "先创建执行计划并提交审阅。"
                    ),
                ),
                patch.dict(
                    os.environ,
                    {
                        "NO_PROXY": "127.0.0.1,localhost",
                        "no_proxy": "127.0.0.1,localhost",
                    },
                ),
                self.assertRaises(HTTPError) as caught,
            ):
                urlopen(request, timeout=5)
            try:
                error_status = caught.exception.code
                payload = json.loads(caught.exception.read().decode("utf-8"))
            finally:
                caught.exception.close()
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(error_status, 409, payload)
        self.assertEqual(payload["errorCode"], "workflow_gate_closed")
        self.assertFalse(payload["retryable"])
        self.assertIn("plan_required", payload["error"])

    def test_agent_prompt_http_preserves_typed_command_receipts(
        self,
    ) -> None:
        session_id = self.service.agent.create_session(
            {"title": "命令回执投影"}
        )["session"]["id"]

        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = Path("debug")
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            Handler,
        )
        thread = threading.Thread(
            target=server.serve_forever,
            daemon=True,
        )
        thread.start()
        url = (
            f"http://127.0.0.1:{server.server_port}"
            f"/api/agent/sessions/{session_id}/prompt"
        )
        failures = (
            AgentCommandReceiptPending(
                "still pending",
                client_message_id="command-pending",
                recovery_state="unresolved",
            ),
            AgentCommandReceiptFailed(
                "provider failed",
                client_message_id="command-failed",
            ),
            AgentCommandReceiptConflict(
                "payload changed",
                client_message_id="command-conflict",
            ),
        )
        try:
            with patch.object(
                self.service.agent,
                "prompt",
                side_effect=failures,
            ):
                payloads: list[dict[str, object]] = []
                for index in range(len(failures)):
                    request = Request(
                        url,
                        data=json.dumps(
                            {
                                "message": "继续",
                                "clientMessageId": (
                                    f"command-{index}"
                                ),
                            }
                        ).encode("utf-8"),
                        headers={
                            "Content-Type": "application/json"
                        },
                        method="POST",
                    )
                    with self.assertRaises(HTTPError) as caught:
                        urlopen(request, timeout=5)
                    try:
                        self.assertEqual(
                            caught.exception.code,
                            409,
                        )
                        payloads.append(
                            json.loads(
                                caught.exception.read().decode(
                                    "utf-8"
                                )
                            )
                        )
                    finally:
                        caught.exception.close()
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(
            [payload["code"] for payload in payloads],
            [
                "AGENT_COMMAND_PENDING",
                "AGENT_COMMAND_FAILED",
                "AGENT_COMMAND_CONFLICT",
            ],
        )
        self.assertEqual(
            [
                payload["commandReceipt"]["state"]
                for payload in payloads
            ],
            ["pending", "failed", "conflict"],
        )
        self.assertEqual(
            payloads[0]["commandReceipt"]["recoveryState"],
            "unresolved",
        )

    def test_agent_external_result_http_route_finalizes_durable_receipt(self) -> None:
        session = self.service.agent.create_session({"title": "外部监督器回执"})["session"]
        command = ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.rag-ime.sidecar"]
        command_sha256 = hashlib.sha256(
            json.dumps(command, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        approval = self.service.agent.sessions.create_approval(
            session_id=str(session["id"]),
            tool_name="runtime",
            operation="restart_sidecar",
            payload_sha256="f" * 64,
            preview={"summary": "重启 Sidecar"},
            risk_level="R2",
        )
        decided = self.service.agent.sessions.decide_approval(
            str(approval["approvalId"]),
            approved=True,
            payload_sha256="f" * 64,
        )
        self.service.agent.sessions.complete_approval(
            str(decided["approvalId"]),
            state="external_pending",
            receipt={
                "schemaVersion": "rag-ime.agent-operation-receipt.v1",
                "mutationApplied": False,
                "externalActionPending": True,
                "externalAction": "restart_sidecar",
                "externalCommand": command,
                "externalCommandSha256": command_sha256,
                "originProcessId": os.getpid(),
            },
        )

        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = Path("debug")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(
                f"http://127.0.0.1:{server.server_port}/api/agent/approvals/"
                f"{approval['approvalId']}/external-result",
                data=json.dumps(
                    {
                        "payloadSha256": "f" * 64,
                        "externalAction": "restart_sidecar",
                        "externalCommandSha256": command_sha256,
                        "succeeded": False,
                        "exitCode": 7,
                        "timedOut": False,
                        "error": "launchctl failed",
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=5) as response:
                result = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(result["approval"]["state"], "failed")
        self.assertFalse(result["approval"]["receipt"]["externalActionPending"])
        self.assertEqual(result["approval"]["receipt"]["exitCode"], 7)
        self.assertEqual(result["approval"]["receipt"]["reason"], "external_supervisor_failed")

    def test_agent_task_action_and_rollback_use_real_management_service(self) -> None:
        task = self.service.management.planning_save_task(
            {
                "id": "task:agent-real",
                "date": "2026-07-13",
                "title": "验证 Pi 审批闭环",
                "status": "todo",
                "project": self.service.config.project,
            }
        )["task"]
        session = self.service.agent.create_session({"title": "真实任务审批"})["session"]
        prepared = self.service.agent_tools.execute(
            {
                "schemaVersion": "rag-ime.agent-tool-call.v1",
                "sessionId": session["id"],
                "tool": "planning",
                "toolCallId": "tool:real:complete",
                "args": {
                    "op": "task_action",
                    "taskId": task["id"],
                    "date": "2026-07-13",
                    "action": "complete",
                },
            }
        )["result"]["approval"]
        with (
            patch.object(self.service.agent.runtime, "has_pending_approval", return_value=True),
            patch.object(self.service.agent.runtime, "resolve_approval"),
        ):
            applied = self.service.agent.decide_approval(
                prepared["approvalId"],
                {"decision": "approve", "payloadSha256": prepared["payloadSha256"]},
            )["approval"]

        self.assertEqual(applied["state"], "applied")
        self.assertEqual(applied["receipt"]["task"]["status"], "done")
        self.assertTrue(applied["receipt"]["undoAvailable"])

        rollback = self.service.agent_tools.execute(
            {
                "schemaVersion": "rag-ime.agent-tool-call.v1",
                "sessionId": session["id"],
                "tool": "planning",
                "toolCallId": "tool:real:undo",
                "args": {
                    "op": "undo_task_event",
                    "eventId": applied["receipt"]["taskEventId"],
                },
            }
        )["result"]["approval"]
        with (
            patch.object(self.service.agent.runtime, "has_pending_approval", return_value=True),
            patch.object(self.service.agent.runtime, "resolve_approval"),
        ):
            reverted = self.service.agent.decide_approval(
                rollback["approvalId"],
                {"decision": "approve", "payloadSha256": rollback["payloadSha256"]},
            )["approval"]

        self.assertEqual(reverted["state"], "applied")
        self.assertEqual(reverted["receipt"]["task"]["status"], "todo")
        self.assertEqual(
            reverted["receipt"]["revertedTaskEventId"],
            applied["receipt"]["taskEventId"],
        )
        sources = self.service.agent.list_memory_sources({"sessionId": session["id"]})["items"]
        self.assertEqual([item["sourceRole"] for item in sources], ["tool_receipt", "tool_receipt"])
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM phrase_stats").fetchone()[0], 0)

    def test_agent_input_settings_use_real_store_and_approved_rollback(self) -> None:
        session = self.service.agent.create_session({"title": "真实输入设置审批"})["session"]
        prepared = self.service.agent_tools.execute(
            {
                "schemaVersion": "rag-ime.agent-tool-call.v1",
                "sessionId": session["id"],
                "tool": "input",
                "toolCallId": "tool:real:settings",
                "args": {
                    "op": "apply_settings",
                    "changes": [{"key": "pinyin.pairs.nL", "value": True}],
                },
            }
        )["result"]["approval"]
        self.assertFalse(self.service.settings()["settings"]["pinyin"]["pairs"]["nL"])
        with (
            patch.object(self.service.agent.runtime, "has_pending_approval", return_value=True),
            patch.object(self.service.agent.runtime, "resolve_approval"),
        ):
            applied = self.service.agent.decide_approval(
                prepared["approvalId"],
                {"decision": "approve", "payloadSha256": prepared["payloadSha256"]},
            )["approval"]
        self.assertEqual(applied["state"], "applied")
        self.assertTrue(self.service.settings()["settings"]["pinyin"]["pairs"]["nL"])
        self.assertEqual(applied["receipt"]["settingKeys"], ["pinyin.pairs.nL"])

        rollback = self.service.agent_tools.execute(
            {
                "schemaVersion": "rag-ime.agent-tool-call.v1",
                "sessionId": session["id"],
                "tool": "input",
                "toolCallId": "tool:real:settings:rollback",
                "args": {
                    "op": "rollback_settings",
                    "sourceApprovalId": prepared["approvalId"],
                },
            }
        )["result"]["approval"]
        with (
            patch.object(self.service.agent.runtime, "has_pending_approval", return_value=True),
            patch.object(self.service.agent.runtime, "resolve_approval"),
        ):
            reverted = self.service.agent.decide_approval(
                rollback["approvalId"],
                {"decision": "approve", "payloadSha256": rollback["payloadSha256"]},
            )["approval"]
        self.assertEqual(reverted["state"], "applied")
        self.assertFalse(self.service.settings()["settings"]["pinyin"]["pairs"]["nL"])
        self.assertEqual(
            reverted["receipt"]["revertedSettingsApprovalId"],
            prepared["approvalId"],
        )

    def test_agent_runtime_toggle_reconfigures_owned_runtime(self) -> None:
        self.assertFalse(self.service.agent.runtime_status()["enabled"])

        enabled = self.service.settings_update({"agent.pi.enabled": True})
        self.assertIn("agent.pi.enabled", enabled["changedKeys"])
        self.assertTrue(self.service.agent.runtime_status()["enabled"])

        self.service.settings_update({"agent.pi.enabled": False})
        self.assertFalse(self.service.agent.runtime_status()["enabled"])

    def test_agent_configuration_and_control_bootstrap_are_revision_bound(self) -> None:
        bootstrap = self.service.control_api.bootstrap()
        initial = self.service.agent.configuration()
        update_payload = {
            "expectedRevision": initial["configuration"]["revision"],
            "changes": {"coordination.enabled": True},
            "updatedBy": "api-test",
        }
        updated = self.service.agent.update_configuration(update_payload)

        with self.assertRaisesRegex(ValueError, "revision changed"):
            self.service.agent.update_configuration(update_payload)

        self.assertTrue(updated["ok"])
        self.assertEqual(bootstrap["apiVersion"], "control-api.v1")
        self.assertIn(
            "agent.configuration.update",
            {item["pathId"] for item in bootstrap["routes"]},
        )
        self.assertEqual(updated["configuration"]["revision"], 2)
        self.assertTrue(updated["configuration"]["configuration"]["coordination"]["enabled"])
        self.assertEqual(updated["event"]["eventType"], "configuration_changed")

    def test_planning_and_yaml_configuration_http_routes_are_operational(self) -> None:
        config_path = Path(self.tmp.name) / "rag-ime.config.yaml"
        config_path.write_text(
            "schemaVersion: rag-ime.user-config.v1\n"
            "settings:\n"
            "  context:\n"
            "    tokenBudget: 4096\n"
            "    reservedOutputTokens: 1024\n",
            encoding="utf-8",
        )
        config_path.chmod(0o644)

        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = Path("debug")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            task_domain = {
                "date": "2026-07-13",
                "title": "完成配置与规划联调",
                "priority": 2,
            }
            planning_preview_request = Request(
                f"http://127.0.0.1:{server.server_port}/api/planning/mutation/preview",
                data=json.dumps(
                    {
                        "kind": "task.save",
                        "payload": task_domain,
                        "expectedRuntimeRevision": self.service.management.revision().runtime_revision,
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(planning_preview_request, timeout=5) as response:
                planning_preview = json.loads(response.read().decode("utf-8"))

            task_request = Request(
                f"http://127.0.0.1:{server.server_port}/api/planning/task/save",
                data=json.dumps(
                    {
                        **task_domain,
                        "expectedRuntimeRevision": planning_preview["expectedRevision"]["runtimeRevision"],
                        "previewToken": planning_preview["previewToken"],
                        "payloadSha256": planning_preview["payloadSha256"],
                        "confirmText": "apply",
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(task_request, timeout=5) as response:
                task_payload = json.loads(response.read().decode("utf-8"))

            goal_domain = {
                "goalId": "goal:http-contract",
                "title": "完成 Web 控制中心切换",
                "detail": "验证目标写入和规划页读取使用同一份数据",
                "horizon": "medium_term",
                "status": "active",
                "priority": 3,
                "targetDate": "2026-07-31",
            }
            goal_preview_request = Request(
                f"http://127.0.0.1:{server.server_port}/api/planning/mutation/preview",
                data=json.dumps(
                    {
                        "kind": "goal.save",
                        "payload": goal_domain,
                        "expectedRuntimeRevision": self.service.management.revision().runtime_revision,
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(goal_preview_request, timeout=5) as response:
                goal_preview = json.loads(response.read().decode("utf-8"))

            goal_request = Request(
                f"http://127.0.0.1:{server.server_port}/api/planning/goal/save",
                data=json.dumps(
                    {
                        **goal_domain,
                        "expectedRuntimeRevision": goal_preview["expectedRevision"]["runtimeRevision"],
                        "previewToken": goal_preview["previewToken"],
                        "payloadSha256": goal_preview["payloadSha256"],
                        "confirmText": "apply",
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(goal_request, timeout=5) as response:
                goal_payload = json.loads(response.read().decode("utf-8"))

            config_request = Request(
                f"http://127.0.0.1:{server.server_port}/api/configuration/import-preview",
                data=json.dumps({"path": str(config_path)}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(config_request, timeout=5) as response:
                config_payload = json.loads(response.read().decode("utf-8"))

            with urlopen(
                f"http://127.0.0.1:{server.server_port}/api/planning/dashboard?date=2026-07-13",
                timeout=5,
            ) as response:
                dashboard_payload = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertTrue(task_payload["ok"])
        self.assertEqual(task_payload["pathId"], "planning.task.save")
        self.assertTrue(task_payload["receiptId"].startswith("work-receipt:"))
        self.assertEqual(task_payload["task"]["title"], "完成配置与规划联调")
        self.assertTrue(goal_payload["ok"])
        self.assertEqual(goal_payload["pathId"], "planning.goal.save")
        self.assertEqual(goal_payload["goal"]["id"], "goal:http-contract")
        self.assertEqual(goal_payload["goal"]["horizon"], "medium_term")
        self.assertTrue(config_payload["valid"])
        self.assertTrue(config_payload["source"]["permissionsHardened"])
        self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(dashboard_payload["tasks"][0]["title"], "完成配置与规划联调")
        self.assertEqual(dashboard_payload["goals"][0]["title"], "完成 Web 控制中心切换")

    def test_management_cleanup_diff_http_requires_confirmation(self) -> None:
        event_ref = self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=1_900_000_100_050,
                source="manual",
                committed_text="离线整理稳定记忆",
                privacy_disposition="allowed",
                recent_context="用户接受过这个短语",
                project="wisdom-weasel-rag-ime",
                tags=("memory",),
            )
        )
        self._compile_phrase(event_ref, "离线整理稳定记忆", tags=("memory",))
        self.core.apply_action(
            MemoryAction(
                action_id=None,
                created_at_ms=1_900_000_100_051,
                memory_id="event:1",
                action_type="accepted",
                query="离线整理",
            )
        )
        plan = self.core.build_memory_cleanup_plan(project="wisdom-weasel-rag-ime")
        run = self.core.list_memory_cleanup_runs(run_id=str(plan["runId"]), limit=1)
        diff_id = int(run["items"][0]["diffs"][0]["diffId"])

        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = Path("debug")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            missing_request = Request(
                f"http://127.0.0.1:{server.server_port}/api/cleanup-diff/apply",
                data=json.dumps({"diffId": diff_id}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(missing_request, timeout=5) as missing_response:
                missing_payload = json.loads(missing_response.read().decode("utf-8"))

            apply_request = Request(
                f"http://127.0.0.1:{server.server_port}/api/cleanup-diff/apply",
                data=json.dumps({"diffId": diff_id, "confirm": "apply"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(apply_request, timeout=5) as apply_response:
                apply_payload = json.loads(apply_response.read().decode("utf-8"))
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertFalse(missing_payload["ok"])
        self.assertEqual(missing_payload["requiredConfirm"], "apply")
        self.assertTrue(apply_payload["ok"])
        self.assertEqual(apply_payload["result"]["diff"]["status"], "applied")
        self.assertEqual(self._audit_count("cleanup_diff_apply"), 1)

    def test_observation_snapshot_is_available_on_local_and_gateway_paths(self) -> None:
        self.service.agent.observations.emit_memory_event(
            phase="draft_ready",
            status="waiting",
            summary="记忆整理草案已生成，等待审阅",
            run_id="memory-run-http",
            metrics={"changeCount": 3},
        )
        self.service.agent.observations.emit_memory_event(
            phase="expired",
            status="expired",
            summary="记忆整理租约已过期",
            run_id="memory-run-http",
            metrics={},
        )

        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = Path("debug")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            payloads = []
            for path in (
                "/api/observability/snapshot?category=memory&runId=memory-run-http&limit=10",
                "/control/v1/observability/snapshot?category=memory&runId=memory-run-http&limit=10",
            ):
                with urlopen(
                    f"http://127.0.0.1:{server.server_port}{path}",
                    timeout=5,
                ) as response:
                    payloads.append(json.loads(response.read().decode("utf-8")))
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        for payload in payloads:
            self.assertEqual(payload["schemaVersion"], "rag-ime.observation-snapshot.v1")
            self.assertEqual(payload["counts"]["total"], 2)
            self.assertEqual(
                {"expired": 1, "waiting": 1},
                payload["counts"]["byStatus"],
            )
            for item in payload["items"]:
                self.assertEqual(item["runId"], "memory-run-http")
                self.assertNotIn("text", item["attributes"])

    def test_api_root_names_the_single_native_control_center(self) -> None:
        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = Path(".")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
                content_type = response.headers.get_content_type()
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(content_type, "application/json")
        self.assertEqual(payload["schemaVersion"], "rag-ime.local-api-root.v1")
        self.assertEqual(payload["controlCenter"], "RagImeControl.app")
        self.assertFalse(payload["browserUI"])

    def _upsert_item(
        self,
        *,
        memory_id: str,
        kind: str,
        text: str,
        status: str = "pending",
        app: str = "",
    ) -> None:
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            upsert_memory_item(
                conn,
                memory_id=memory_id,
                kind=kind,
                text=text,
                normalized_text=normalize_text(text),
                summary="management test",
                source_event_id=None,
                project="wisdom-weasel-rag-ime",
                app=app,
                confidence=0.7,
                quality_score=0.5,
                status=status,
                privacy_class="local",
                created_at_ms=1_900_000_100_030,
                updated_at_ms=1_900_000_100_030,
                metadata={"direct_candidate_allowed": True},
                tags=("management-test",),
                embedding_provider=None,
            )

    def _audit_count(self, action: str) -> int:
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM management_audit_log WHERE action = ?",
                (action,),
            ).fetchone()
            return int(row["count"])

    def _retrieval_doc_count(self) -> int:
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            row = conn.execute("SELECT COUNT(*) AS count FROM memory_retrieval_docs").fetchone()
            return int(row["count"])


if __name__ == "__main__":
    unittest.main()
