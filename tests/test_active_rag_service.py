from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from rag_ime.active_rag_service import (
    ACTIVE_RAG_DEFAULT_MAX_CHARS,
    ActiveRagService,
    ActiveRagStartRequest,
    _recent_input_history_evidence,
)
from rag_ime.active_rag_models import ActiveRagEvidence
from rag_ime.daily_planner import local_date_string
from rag_ime.deepseek_completion import CompletionCandidateDelta, DeepSeekCompletionError, build_deepseek_completion_messages
from rag_ime.local_sqlite_core import LocalSqliteCoreClient
from rag_ime.models import InputEvent
from rag_ime.text_utils import now_ms, stable_text_hash


class ActiveRagServiceTests(unittest.TestCase):
    def test_local_note_reference_reuses_native_guards_without_model(self):
        provider = FakeActiveRagProvider(("must not run",))
        service = ActiveRagService(completion_provider=provider)
        self.addCleanup(service.close)
        request = ActiveRagStartRequest(selected_text="hello",selected_text_hash=stable_text_hash("hello"),
            frontend_revision=1,selection_epoch=2,panel_session_id="panel",front_app_bundle_id="test.app")
        valid = [True]
        result = service.start_reference(request, {"id":"r","markdown":"current note"}, lambda: valid[0])
        self.assertEqual(result["status"], "ready")
        args = dict(session_id=result["sessionId"],candidate_id="vault:r",selected_text_hash=request.selected_text_hash,
            frontend_revision=1,selection_epoch=2,panel_session_id="panel",front_app_bundle_id="test.app")
        self.assertFalse(service.accept(**{**args,"front_app_bundle_id":"other"})["ok"])
        self.assertFalse(service.accept(**{**args,"selection_epoch":3})["ok"])
        valid[0]=False
        self.assertFalse(service.accept(**args)["ok"])
        valid[0]=True
        accepted=service.accept(**args)
        self.assertEqual(accepted["insertText"],"current note")
        self.assertEqual(accepted["placement"],"insert")

    def test_recent_input_history_keeps_configured_window_for_generation(self) -> None:
        evidence = tuple(
            ActiveRagEvidence(
                evidence_id=f"recent:{index}",
                text=f"最近输入 {index}",
                source_type="recent_input_context",
                source_lane="timeline_recent_input",
            )
            for index in range(1, 7)
        )

        selected = _recent_input_history_evidence(evidence)

        self.assertEqual([item.text for item in selected], [f"最近输入 {index}" for index in range(1, 7)])

    def test_sensitive_field_is_blocked_before_hash_validation_retrieval_and_remote(self) -> None:
        provider = FakeActiveRagProvider(("绝不应该调用",))
        service = ActiveRagService(completion_provider=provider)
        secret = "账号 user@example.test 密码 swordfish"
        request = ActiveRagStartRequest(
            selected_text=secret,
            selected_text_hash="frontend-supplied-secret-hash-must-not-return",
            frontend_revision=9,
            selection_epoch=2,
            context=secret,
            secure_input=True,
            evidence_pack=({"surfaceHints": ["绝不应该召回"]},),
            remote_model_allowed=True,
        )

        with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
            blocked = service.start(request)
            status = service.status(str(blocked["sessionId"]))
            diagnostics = service.diagnostics(str(blocked["sessionId"]))

        blob = json.dumps(blocked, ensure_ascii=False)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["error"], "sensitive_field_blocked")
        self.assertEqual(blocked["selectedTextHash"], "")
        self.assertEqual(blocked["candidateCount"], 0)
        self.assertEqual(blocked["candidates"], [])
        self.assertFalse(blocked["diagnostics"]["retrieval"]["called"])
        self.assertFalse(blocked["diagnostics"]["remoteModel"]["requested"])
        self.assertEqual(blocked["diagnostics"]["remoteModel"]["skipReason"], "sensitive_field_blocked")
        self.assertEqual(provider.calls, [])
        self.assertEqual(status["status"], "blocked")
        self.assertEqual(diagnostics["status"], "blocked")
        self.assertNotIn(secret, blob)
        self.assertNotIn("frontend-supplied-secret-hash", blob)
        self.assertNotIn("sha256:", blob)

    def test_sensitive_text_guard_blocks_credential_value_even_without_secure_flag(self) -> None:
        service = ActiveRagService()
        secret = "请保存 API key sk-example-secret"
        request = ActiveRagStartRequest(
            selected_text=secret,
            selected_text_hash=stable_text_hash(secret),
            frontend_revision=1,
            selection_epoch=1,
            context=secret,
            sensitive_text_guard_enabled=False,
        )

        blocked = service.preview(request, local_only=True)

        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["evidenceCount"], 0)
        self.assertEqual(blocked["candidateCount"], 0)
        self.assertNotIn(secret, json.dumps(blocked, ensure_ascii=False))

    def test_sensitive_text_guard_allows_discussion_of_credential_settings(self) -> None:
        service = ActiveRagService()
        discussion = "检查 API Key 设置说明，以及 access token 配置页面的交互"
        request = ActiveRagStartRequest(
            selected_text=discussion,
            selected_text_hash=stable_text_hash(discussion),
            frontend_revision=1,
            selection_epoch=1,
            context=discussion,
            remote_model_allowed=False,
        )

        preview = service.preview(request, local_only=True)

        self.assertNotEqual(preview["status"], "blocked")
        self.assertNotEqual(preview.get("error"), "sensitive_field_blocked")

    def test_active_rag_local_evidence_pack_runs_without_deepseek_flag(self) -> None:
        provider = FakeActiveRagProvider(("DeepSeek不应调用",))
        service = ActiveRagService(completion_provider=provider)
        request = _request(selected_text="选区")

        with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": ""}):
            started = service.start(request)
            self.assertGreaterEqual(started["elapsedMs"], 0)
            ready = _wait_ready(service, str(started["sessionId"]))

        self.assertEqual(provider.calls, [])
        self.assertEqual(ready["status"], "ready")
        self.assertGreaterEqual(ready["elapsedMs"], 0)
        self.assertEqual(ready["pollAfterMs"], 0)
        self.assertEqual(ready["uiMode"], "active_rag_assist")
        self.assertFalse(ready["keyPolicy"]["thinkingRowSelectable"])
        self.assertEqual([item["text"] for item in ready["candidates"]], ["主动候选"])

    def test_active_rag_pending_status_reports_poll_backoff(self) -> None:
        gate = threading.Event()
        provider = BlockingActiveRagProvider(gate)
        service = ActiveRagService(completion_provider=provider)
        request = _request(selected_text="选区", latency_budget_ms=7000)

        with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
            started = service.start(request)
            pending = service.status(str(started["sessionId"]))
            gate.set()
            ready = _wait_ready(service, str(started["sessionId"]))

        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["expiresAfterMs"], 7000)
        self.assertGreaterEqual(pending["elapsedMs"], 0)
        self.assertGreater(pending["pollAfterMs"], 0)
        self.assertLessEqual(pending["pollAfterMs"], 160)
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["pollAfterMs"], 0)
        self.assertEqual(ready["expiresAfterMs"], 7000)

    def test_active_rag_pending_status_exposes_truthful_context_and_retrieval_progress(self) -> None:
        gate = threading.Event()
        provider = BlockingActiveRagProvider(gate)
        service = ActiveRagService(completion_provider=provider)
        selected = "测试中间进度"
        request = ActiveRagStartRequest(
            selected_text=selected,
            selected_text_hash=stable_text_hash(selected),
            frontend_revision=8,
            selection_epoch=4,
            context=selected,
            frontend_context_chars=len(selected),
            evidence_pack=(
                {
                    "text": "检查并完善记忆检索的时间衰减设计",
                    "summary": "补齐时间衰减权重和回归测试",
                    "tags": ["时间衰减", "记忆检索"],
                    "sourceType": "todo",
                    "sourceLane": "planning_open_task",
                },
            ),
            window_context={
                "captureMode": "accessibility_semantics",
                "nodeCount": 16,
                "application": {"name": "Codex", "windowTitle": "当前任务"},
                "nodes": [
                    {
                        "nodeRef": "ax_editor",
                        "role": "AXTextArea",
                        "value": "这是 AX 树实际捕获的编辑区内容",
                        "focused": True,
                    }
                ],
            },
            max_chars=120,
        )

        with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
            started = service.start(request)
            deadline = time.monotonic() + 1
            while not provider.calls and time.monotonic() < deadline:
                time.sleep(0.01)
            pending = service.status(str(started["sessionId"]))
            gate.set()
            ready = _wait_ready(service, str(started["sessionId"]))

        progress = pending["diagnostics"]["progress"]
        self.assertEqual(progress["stage"], "generating")
        self.assertEqual(progress["context"]["foregroundChars"], len(selected))
        self.assertEqual(progress["context"]["windowNodeCount"], 16)
        self.assertEqual(progress["retrieval"]["evidenceCount"], 0)
        self.assertEqual(progress["retrieval"]["contextEvidenceCount"], 1)
        self.assertEqual(progress["retrieval"]["items"], [])
        context_view = pending["diagnostics"]["contextView"]
        self.assertEqual(context_view["source"], "provider_request")
        self.assertEqual(context_view["currentRequest"], selected)
        self.assertEqual(
            context_view["windowContext"]["nodes"][0]["value"],
            "这是 AX 树实际捕获的编辑区内容",
        )
        self.assertEqual(context_view["windowContext"]["projection"], "generation_text")
        self.assertEqual(context_view["planning"]["items"][0]["title"], "检查并完善记忆检索的时间衰减设计")
        self.assertFalse(context_view["planning"]["maySupportFacts"])
        self.assertNotIn("nodeRef", str(context_view["windowContext"]))
        self.assertNotIn("actions", str(context_view["windowContext"]))
        self.assertNotIn("你是 macOS 输入法", str(context_view))
        self.assertEqual(ready["diagnostics"]["progress"]["stage"], "ready")

    def test_active_rag_provider_context_view_keeps_sourced_application_semantics(self) -> None:
        provider = FakeActiveRagProvider(("已结合编辑区和项目目录",))
        service = ActiveRagService(completion_provider=provider)
        request = ActiveRagStartRequest(
            selected_text="解释终端上方代码",
            selected_text_hash=stable_text_hash("解释终端上方代码"),
            frontend_revision=1,
            selection_epoch=1,
            context="解释终端上方代码",
            window_context={
                "schemaVersion": "rag-ime.window-context.v1",
                "captureMode": "accessibility_semantics",
                "application": {
                    "name": "Zed",
                    "windowTitle": "Project — main.py",
                },
                "nodes": [],
                "applicationSemantics": {
                    "source": "zed_workspace_state",
                    "freshness": "best_effort_local_state",
                    "projectName": "Project",
                    "activeFile": "src/main.py",
                    "editorExcerpt": "1: print('hello')",
                    "editorExcerptStartLine": 1,
                    "projectEntries": ["src/", "README.md"],
                    "contentOrigin": "workspace_file",
                    "trust": {"mayLagUnsavedChanges": True},
                },
            },
        )

        with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
            started = service.start(request)
            ready = _wait_ready(service, str(started["sessionId"]))

        semantics = ready["diagnostics"]["contextView"]["windowContext"]["applicationSemantics"]
        self.assertEqual(semantics["activeFile"], "src/main.py")
        self.assertIn("print('hello')", semantics["editorExcerpt"])
        self.assertTrue(semantics["trust"]["mayLagUnsavedChanges"])
        self.assertNotIn("/Volumes/", json.dumps(semantics))

    def test_active_rag_trace_observer_runs_without_enabling_the_jsonl_journal(self) -> None:
        records: list[dict[str, object]] = []
        service = ActiveRagService(
            completion_provider=FakeActiveRagProvider(("观察候选",)),
            trace_observer=lambda record: records.append(record),
        )

        with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
            started = service.start(_request(selected_text="观察上下文"))
            _wait_ready(service, str(started["sessionId"]))

        self.assertEqual(
            [record["phase"] for record in records],
            ["started", "retrieval_complete", "ready"],
        )
        self.assertTrue(all(record["privacy"]["rawTextIncluded"] is False for record in records))
        self.assertNotIn("观察上下文", json.dumps(records, ensure_ascii=False))
        self.assertNotIn("观察候选", json.dumps(records, ensure_ascii=False))

    def test_active_rag_observation_observer_receives_only_compact_metadata(self) -> None:
        records: list[dict[str, object]] = []
        service = ActiveRagService(
            completion_provider=FakeActiveRagProvider(("观察候选",)),
            observation_observer=lambda record: records.append(record),
        )

        with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
            started = service.start(_request(selected_text="观察上下文"))
            _wait_ready(service, str(started["sessionId"]))

        self.assertEqual(
            [record["phase"] for record in records],
            ["started", "retrieval_complete", "ready"],
        )
        self.assertEqual(records[0]["request"]["selectedText"]["chars"], 5)
        self.assertNotIn("model", records[0])
        self.assertNotIn("traceEvents", records[0])
        self.assertNotIn("观察上下文", json.dumps(records, ensure_ascii=False))
        self.assertNotIn("观察候选", json.dumps(records, ensure_ascii=False))

    def test_active_rag_persists_redacted_full_chain_across_terminal_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            trace_path = Path(tmpdir) / "active-rag-chain.jsonl"
            provider = FakeActiveRagProvider(("诊断链路最终候选",))
            service = ActiveRagService(completion_provider=provider, trace_path=trace_path)
            request = _request(
                selected_text="红色轨道上下文测试",
                max_chars=120,
                evidence_pack=(
                    {
                        "text": "默认日志不应保存的证据正文",
                        "title": "默认日志不应保存的证据标题",
                        "summary": "默认日志不应保存的证据摘要",
                        "tags": ["默认日志不应保存的标签"],
                    },
                ),
            )

            with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
                started = service.start(request)
                ready = _wait_ready(service, str(started["sessionId"]))
            journal = service.trace_records(session_id=str(started["sessionId"]), limit=20)

        self.assertEqual(ready["status"], "ready")
        self.assertTrue(journal["enabled"])
        self.assertFalse(journal["rawTextIncluded"])
        self.assertEqual(
            [record["phase"] for record in journal["records"]],
            ["started", "retrieval_complete", "ready"],
        )
        terminal = journal["records"][-1]
        self.assertEqual(terminal["retrieval"]["evidenceCount"], 1)
        self.assertEqual(terminal["generation"]["candidateCount"], 1)
        self.assertTrue(terminal["model"]["modelInput"]["resolvedRequest"]["hash"])
        blob = json.dumps(journal, ensure_ascii=False)
        self.assertNotIn("红色轨道上下文测试", blob)
        self.assertNotIn("诊断链路最终候选", blob)
        self.assertNotIn("默认日志不应保存的证据正文", blob)
        self.assertNotIn("默认日志不应保存的证据标题", blob)
        self.assertNotIn("默认日志不应保存的证据摘要", blob)
        self.assertNotIn("默认日志不应保存的标签", blob)
        metadata = terminal["retrieval"]["evidence"][0]["metadata"]
        self.assertTrue(metadata["summary"]["hash"])

    def test_active_rag_trace_can_include_exact_context_evidence_and_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            trace_path = Path(tmpdir) / "active-rag-chain.jsonl"
            provider = FakeActiveRagProvider(("完整链路候选正文",))
            service = ActiveRagService(
                completion_provider=provider,
                trace_path=trace_path,
                trace_include_text=True,
            )
            request = _request(
                selected_text="完整链路前台选区",
                max_chars=120,
                evidence_pack=({"text": "完整链路证据片段", "sourceLane": "bm25_raw"},),
            )

            with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
                started = service.start(request)
                _wait_ready(service, str(started["sessionId"]))
            terminal = service.trace_records(session_id=str(started["sessionId"]))["records"][-1]

        blob = json.dumps(terminal, ensure_ascii=False)
        self.assertTrue(terminal["privacy"]["rawTextIncluded"])
        self.assertIn("完整链路前台选区", blob)
        self.assertIn("完整链路证据片段", blob)
        self.assertIn("完整链路候选正文", blob)
        self.assertIn("messages", terminal["model"]["modelInput"])

    def test_active_rag_failure_trace_keeps_structured_transport_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            trace_path = Path(tmpdir) / "active-rag-chain.jsonl"
            service = ActiveRagService(
                completion_provider=DiagnosticFailingActiveRagProvider(),
                trace_path=trace_path,
            )
            request = _request(selected_text="失败链路诊断", max_chars=120)

            with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
                started = service.start(request)
                failed = _wait_ready(service, str(started["sessionId"]))
            terminal = service.trace_records(session_id=str(started["sessionId"]))["records"][-1]

        self.assertEqual(failed["status"], "ready")
        self.assertEqual(failed["error"], "")
        self.assertEqual(failed["candidates"][0]["text"], "暂未完成，可以重试")
        self.assertTrue(failed["candidates"][0]["metadata"]["activeRagNoSuggestion"])
        self.assertEqual(failed["diagnostics"]["progress"]["stage"], "error")
        self.assertFalse(failed["diagnostics"]["progress"]["workerPending"])
        transport = terminal["model"]["request"]["transport"]
        self.assertEqual(transport["terminalReason"], "upstream_stream_closed")
        self.assertEqual(transport["contentChars"], 7)
        self.assertNotIn("responsePreview", transport)

    def test_active_rag_publishes_unselectable_partial_stream_before_ready(self) -> None:
        provider = StreamingActiveRagProvider()
        service = ActiveRagService(completion_provider=provider)
        request = _request(selected_text="选区", max_chars=120)

        with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
            started = service.start(request)
            self.assertTrue(provider.emitted.wait(timeout=1))
            pending = service.status(str(started["sessionId"]))
            provider.release.set()
            ready = _wait_ready(service, str(started["sessionId"]))

        self.assertEqual(pending["status"], "pending")
        self.assertFalse(pending["keyPolicy"]["ready"])
        self.assertEqual(pending["candidates"][0]["text"], "第一段正在流式返回")
        self.assertFalse(pending["candidates"][0]["isSelectable"])
        self.assertEqual(pending["candidates"][0]["selectionAction"], "none")
        self.assertIsNone(pending["candidates"][0]["selectionKey"])
        self.assertTrue(pending["candidates"][0]["metadata"]["streamingPartial"])
        self.assertTrue(pending["diagnostics"]["modelRequest"]["partialVisible"])
        self.assertGreater(pending["diagnostics"]["progress"]["model"]["firstTokenMs"], 0)
        self.assertEqual(pending["diagnostics"]["progress"]["stage"], "streaming")
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["candidates"][0]["text"], "第一段已经完整返回")
        self.assertEqual(
            pending["candidates"][0]["candidateStableId"],
            ready["candidates"][0]["candidateStableId"],
        )

    def test_active_rag_preserves_visible_partial_when_stream_then_fails(self) -> None:
        provider = InterruptedStreamingActiveRagProvider()
        service = ActiveRagService(completion_provider=provider)
        request = _request(selected_text="检查生成稳定性", max_chars=120)

        with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
            started = service.start(request)
            self.assertTrue(provider.emitted.wait(timeout=1))
            pending = service.status(str(started["sessionId"]))
            provider.release.set()
            ready = _wait_ready(service, str(started["sessionId"]))

        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["candidates"][0]["text"], "已经生成的正文必须保留给用户确认。")
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["candidates"][0]["text"], "已经生成的正文必须保留给用户确认。")
        self.assertFalse(ready["candidates"][0]["metadata"]["streamingPartial"])
        self.assertTrue(ready["candidates"][0]["metadata"]["partialRecovered"])
        self.assertTrue(ready["diagnostics"]["modelRequest"]["partialRecovered"])
        self.assertIn(
            "active_rag_partial_recovered",
            [item["name"] for item in ready["traceEvents"]],
        )

    def test_active_rag_does_not_promote_incomplete_partial_when_provider_finishes_empty(self) -> None:
        provider = EmptyAfterPartialActiveRagProvider()
        service = ActiveRagService(completion_provider=provider)
        request = _request(selected_text="检查空结束恢复", max_chars=120)

        with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
            started = service.start(request)
            ready = _wait_ready(service, str(started["sessionId"]))

        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["candidates"][0]["text"], "这次没有合适建议")
        self.assertFalse(ready["candidates"][0]["isSelectable"])
        self.assertFalse(ready["diagnostics"]["modelRequest"].get("partialRecovered", False))
        self.assertEqual(ready["diagnostics"]["generation"]["displayedCandidateCount"], 0)

    def test_active_rag_uses_deepseek_when_enabled(self) -> None:
        provider = FakeActiveRagProvider(("这是原始选区长句", "DeepSeek主动候选"))
        service = ActiveRagService(completion_provider=provider)
        request = _request(selected_text="这是原始选区长句，需要被避免复读", max_candidates=2)

        with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
            started = service.start(request)
            ready = _wait_ready(service, str(started["sessionId"]))

        self.assertEqual(provider.calls[0].scene, "active_rag")
        self.assertEqual(provider.calls[0].max_chars, 24)
        self.assertEqual(ready["status"], "ready")
        self.assertEqual([item["text"] for item in ready["candidates"]], ["DeepSeek主动候选"])
        self.assertEqual(ready["candidates"][0]["selectionAction"], "commit_side_candidate")
        self.assertEqual(ready["candidates"][0]["candidateOrdinal"], 1)
        self.assertEqual(ready["candidates"][0]["metadata"]["frontendRevision"], request.frontend_revision)
        accepted = service.accept(
            session_id=str(ready["sessionId"]),
            candidate_id=str(ready["candidates"][0]["candidateId"]),
            selected_text_hash=request.selected_text_hash,
            frontend_revision=request.frontend_revision,
            selection_epoch=request.selection_epoch,
        )
        self.assertTrue(accepted["ok"])
        self.assertEqual(accepted["insertText"], "DeepSeek主动候选")

    def test_active_rag_trace_records_direct_transport_and_stream_terminal_state(self) -> None:
        provider = FakeActiveRagProvider(
            ("远程生成已完整结束。",),
            metadata={
                "parseMode": "content",
                "finishReason": "stop",
                "doneMarkerSeen": True,
                "streamInterrupted": False,
                "continuationAttempted": True,
                "continuationAdvanced": True,
                "continuationCompleted": True,
                "proxyBypassed": True,
                "transportMode": "direct_no_proxy",
            },
        )
        service = ActiveRagService(completion_provider=provider)

        with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
            started = service.start(_request(selected_text="检查生成通道", max_chars=120))
            ready = _wait_ready(service, str(started["sessionId"]))

        model_request = ready["diagnostics"]["modelRequest"]
        completed = next(item for item in ready["traceEvents"] if item["name"] == "deepseek_request_completed")
        self.assertTrue(model_request["proxyBypassed"])
        self.assertEqual(model_request["transportModes"], ["direct_no_proxy"])
        self.assertEqual(model_request["finishReasons"], ["stop"])
        self.assertTrue(model_request["doneMarkerSeen"])
        self.assertTrue(model_request["continuationCompleted"])
        self.assertTrue(completed["fields"]["proxyBypassed"])
        self.assertEqual(completed["fields"]["finishReasons"], ["stop"])

    def test_active_rag_default_deepseek_budget_is_paragraph_length(self) -> None:
        paragraph = "我会把 DeepSeek 主动生成和 LLM 预测显示拆成两条稳定链路，让显式触发时输出一段完整正文。"
        provider = FakeActiveRagProvider((paragraph,))
        service = ActiveRagService(completion_provider=provider)
        request = ActiveRagStartRequest(
            selected_text="DeepSeek 输出我希望是一段话",
            selected_text_hash=stable_text_hash("DeepSeek 输出我希望是一段话"),
            frontend_revision=7,
            selection_epoch=3,
            context="Ctrl+Enter不行，DeepSeek没输出，LLM不显示",
        )

        with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
            started = service.start(request)
            ready = _wait_ready(service, str(started["sessionId"]))

        self.assertEqual(provider.calls[0].max_chars, ACTIVE_RAG_DEFAULT_MAX_CHARS)
        self.assertEqual(ACTIVE_RAG_DEFAULT_MAX_CHARS, 0)
        self.assertEqual(provider.calls[0].context_packet["outputContract"]["maxCandidateChars"], 0)
        self.assertEqual(provider.calls[0].context_packet["outputContract"]["outputFormat"], "candidate_document")
        self.assertEqual(ready["status"], "ready")
        self.assertGreaterEqual(len(ready["candidates"][0]["text"]), 40)

    def test_active_rag_default_keeps_full_multi_paragraph_remote_result(self) -> None:
        first = "第一段说明前台上下文优先，并保留完整的用户意图。" * 5
        second = "第二段说明检索证据、生成正文和确认插入之间的边界。" * 5
        expected = f"{first}\n\n{second}"
        provider = FakeActiveRagProvider((expected,))
        service = ActiveRagService(completion_provider=provider)
        request = ActiveRagStartRequest(
            selected_text="请输出多段完整说明",
            selected_text_hash=stable_text_hash("请输出多段完整说明"),
            frontend_revision=7,
            selection_epoch=3,
            context="请输出多段完整说明",
        )

        with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
            started = service.start(request)
            ready = _wait_ready(service, str(started["sessionId"]))

        self.assertEqual(ready["status"], "ready")
        self.assertGreater(len(expected), 180)
        self.assertEqual(ready["candidates"][0]["text"], expected)
        self.assertNotIn("lengthGoverned", ready["candidates"][0]["metadata"])

    def test_active_rag_governs_remote_candidate_length(self) -> None:
        provider = FakeActiveRagProvider(("这是一个过长候选，需要缩短，保留关键动作",))
        service = ActiveRagService(completion_provider=provider)
        request = _request(selected_text="候选长度治理", max_chars=6)

        with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
            started = service.start(request)
            ready = _wait_ready(service, str(started["sessionId"]))

        self.assertEqual(ready["status"], "ready")
        self.assertLessEqual(len(ready["candidates"][0]["text"]), 6)
        self.assertTrue(ready["candidates"][0]["metadata"]["lengthGoverned"])
        self.assertEqual(ready["candidates"][0]["metadata"]["maxChars"], 6)

    def test_active_rag_governs_local_evidence_length_with_surface_hints(self) -> None:
        service = ActiveRagService()
        request = _request(
            selected_text="本地证据长度治理",
            max_chars=8,
            evidence_pack=(
                {
                    "surfaceHints": ["时间线笔记本", "这是一整段很长的原始 RAG 证据，不应该直接显示给输入法候选框"],
                    "tags": ["RAG"],
                },
            ),
        )

        started = service.start(request)
        ready = _wait_ready(service, str(started["sessionId"]))

        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["candidates"][0]["text"], "时间线笔记本")
        self.assertLessEqual(len(ready["candidates"][0]["text"]), 8)
        self.assertFalse(ready["candidates"][0]["metadata"]["lengthGoverned"])

    def test_active_rag_allows_short_keyword_reuse_but_rejects_exact_selection_echo(self) -> None:
        provider = FakeActiveRagProvider(("DeepSeek 生成按钮和 RAG 记忆上下文", "生成按钮优化"))
        service = ActiveRagService(completion_provider=provider)
        request = _request(selected_text="DeepSeek 生成按钮和 RAG 记忆上下文", max_candidates=2)

        with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
            started = service.start(request)
            ready = _wait_ready(service, str(started["sessionId"]))

        self.assertEqual(ready["status"], "ready")
        self.assertEqual([item["text"] for item in ready["candidates"]], ["生成按钮优化"])

    def test_active_rag_deepseek_empty_result_returns_stable_no_suggestion_status(self) -> None:
        provider = FakeActiveRagProvider(())
        service = ActiveRagService(completion_provider=provider)
        request = _request(selected_text="选区", max_candidates=2)

        with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
            started = service.start(request)
            ready = _wait_ready(service, str(started["sessionId"]))

        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["candidateCount"], 0)
        self.assertEqual(ready["candidates"][0]["sourceType"], "status")
        self.assertEqual(ready["candidates"][0]["text"], "这次没有合适建议")
        self.assertEqual(ready["candidates"][0]["insertText"], "")
        self.assertTrue(ready["candidates"][0]["metadata"]["activeRagNoSuggestion"])
        self.assertNotIn("request_fallback", json.dumps(ready, ensure_ascii=False))
        self.assertEqual(len(provider.calls), 2)
        self.assertTrue(ready["diagnostics"]["modelRequest"]["contentRetryAttempted"])
        self.assertFalse(ready["diagnostics"]["modelRequest"]["contentRetryCompleted"])
        self.assertTrue(ready["diagnostics"]["generation"]["noSuitableSuggestion"])

    def test_active_rag_local_empty_result_is_retriable_not_error(self) -> None:
        service = ActiveRagService()
        selected_text = "没有本地证据也要保持稳定反馈"
        request = ActiveRagStartRequest(
            selected_text=selected_text,
            selected_text_hash=stable_text_hash(selected_text),
            frontend_revision=4,
            selection_epoch=2,
            context=selected_text,
            evidence_pack=(),
            local_retrieval_allowed=False,
            remote_model_allowed=False,
            max_chars=0,
        )

        started = service.start(request)
        ready = _wait_ready(service, str(started["sessionId"]))

        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["error"], "")
        self.assertEqual(ready["candidates"][0]["text"], "这次没有合适建议")
        self.assertTrue(ready["candidates"][0]["metadata"]["activeRagNoSuggestion"])

    def test_active_rag_retries_once_without_rag_when_first_result_is_empty(self) -> None:
        provider = SequencedActiveRagProvider(((), ("重新依据当前请求生成可用正文",)))
        service = ActiveRagService(completion_provider=provider)
        request = _request(
            selected_text="检查当前请求和RAG拼接",
            max_chars=120,
            evidence_pack=(
                {
                    "evidenceId": "atom:retry-grounding",
                    "surfaceHints": ["主题书和原子事实用于首次生成"],
                    "sourceType": "memory",
                    "sourceLane": "vector_raw",
                    "atomIds": ["atom:retry-grounding"],
                },
                {
                    "evidenceId": "recent:retry-continuity",
                    "surfaceHints": ["最近完整输入用于恢复对话连续性"],
                    "sourceType": "recent_input_context",
                    "sourceLane": "timeline_recent_input",
                    "contextOnly": True,
                    "maySupportFacts": False,
                },
            ),
        )

        with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
            started = service.start(request)
            ready = _wait_ready(service, str(started["sessionId"]))

        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["candidates"][0]["text"], "重新依据当前请求生成可用正文")
        self.assertEqual(len(provider.calls), 2)
        self.assertTrue(provider.calls[0].evidence_pack)
        self.assertEqual(provider.calls[1].evidence_pack, ())
        self.assertTrue(provider.calls[1].recovery_mode)
        self.assertEqual(provider.calls[1].selected_text, request.selected_text)
        self.assertNotIn("groundingEvidence", provider.calls[1].context_packet)
        self.assertNotIn("ragEvidenceHints", provider.calls[1].context_packet)
        initial_payload = json.loads(build_deepseek_completion_messages(provider.calls[0])[1]["content"])
        recovery_payload = json.loads(build_deepseek_completion_messages(provider.calls[1])[1]["content"])
        self.assertTrue(initial_payload["groundingEvidence"])
        self.assertEqual(initial_payload["groundingMode"], "rag_grounded")
        self.assertTrue(recovery_payload["recoveryMode"])
        self.assertNotEqual(recovery_payload["groundingMode"], "rag_grounded")
        self.assertEqual(recovery_payload["groundingEvidence"], [])
        self.assertTrue(recovery_payload["contextPacket"]["recentCompleteInputs"])
        self.assertIn(
            "最近完整输入用于恢复对话连续性",
            json.dumps(recovery_payload["contextPacket"]["recentCompleteInputs"], ensure_ascii=False),
        )
        self.assertTrue(ready["diagnostics"]["modelRequest"]["contentRetryCompleted"])

    def test_active_rag_reports_quality_retry_while_recovery_request_is_running(self) -> None:
        provider = BlockingRecoveryActiveRagProvider()
        service = ActiveRagService(completion_provider=provider)
        request = _request(selected_text="检查质量重试进度", max_chars=120)

        with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
            started = service.start(request)
            self.assertTrue(provider.recovery_started.wait(timeout=1))
            pending = service.status(str(started["sessionId"]))
            provider.release.set()
            ready = _wait_ready(service, str(started["sessionId"]))

        progress = pending["diagnostics"]["progress"]
        self.assertEqual(progress["stage"], "quality_retry")
        self.assertTrue(progress["model"]["qualityRetry"])
        self.assertEqual(
            progress["model"]["qualityRetryReason"],
            "empty_or_governed_remote_candidates",
        )
        self.assertEqual(ready["status"], "ready")

    def test_active_rag_generates_surface_request_id_when_panel_session_is_missing(self) -> None:
        provider = FakeActiveRagProvider(("已生成内部请求标识并完成补全",))
        service = ActiveRagService(completion_provider=provider)

        with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
            started = service.start(_request(selected_text="补全当前内容", panel_session_id=""))
            ready = _wait_ready(service, str(started["sessionId"]))

        completion_request = provider.calls[0]
        self.assertEqual(ready["status"], "ready")
        self.assertTrue(completion_request.surface_request_id.startswith("active-rag-surface:"))
        self.assertEqual(
            completion_request.context_packet["currentInput"]["panelSessionId"],
            completion_request.surface_request_id,
        )

    def test_active_rag_cancel_uses_generated_surface_request_id(self) -> None:
        provider = CancellableManagedActiveRagProvider()
        service = ActiveRagService(completion_provider=provider)

        started = service.start(_request(selected_text="取消无面板请求", panel_session_id=""))
        session_id = str(started["sessionId"])
        self.assertTrue(provider.started.wait(timeout=1))

        cancelled = service.cancel(session_id)
        provider.gate.set()
        provider.released.wait(timeout=1)

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertFalse(cancelled["diagnostics"]["progress"]["workerPending"])
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(provider.cancelled_ids, [provider.calls[0].surface_request_id])
        self.assertTrue(provider.cancelled_ids[0].startswith("active-rag-surface:"))

    def test_active_rag_governed_recovery_finishes_as_no_suggestion_with_diagnostics(self) -> None:
        provider = FailingActiveRagProvider()
        service = ActiveRagService(completion_provider=provider)
        request = _request(selected_text="改正", max_chars=120)

        with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
            started = service.start(request)
            ready = _wait_ready(service, str(started["sessionId"]))

        model_request = ready["diagnostics"]["modelRequest"]
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["candidates"][0]["text"], "这次没有合适建议")
        self.assertEqual(len(provider.calls), 2)
        self.assertTrue(provider.calls[1].recovery_mode)
        self.assertEqual(provider.calls[1].selected_text, "")
        self.assertTrue(model_request["contentRetryAttempted"])
        self.assertFalse(model_request["contentRetryCompleted"])
        self.assertIn(
            "deepseek_context_only_retry_started",
            [item["name"] for item in ready["traceEvents"]],
        )
        self.assertIn(
            "active_rag_no_suitable_suggestion",
            [item["name"] for item in ready["traceEvents"]],
        )

    def test_active_rag_uses_fresh_context_when_semantic_anchor_is_stale(self) -> None:
        provider = FakeActiveRagProvider(("使用当前输入重新构建查询并过滤旧锚点",))
        service = ActiveRagService(completion_provider=provider)
        stale = "上一轮已经失效的请求"
        current = "现在需要检查上下文构建逻辑，避免空结果被直接输出。"
        request = ActiveRagStartRequest(
            selected_text=stale,
            selected_text_hash=stable_text_hash(stale),
            frontend_revision=12,
            selection_epoch=8,
            placement="insert_after_selection",
            intent="answer",
            context=current,
            max_chars=120,
        )

        with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
            started = service.start(request)
            ready = _wait_ready(service, str(started["sessionId"]))

        payload = json.loads(build_deepseek_completion_messages(provider.calls[0])[1]["content"])
        self.assertEqual(ready["status"], "ready")
        self.assertIn("避免空结果", payload["currentRequest"])
        self.assertNotEqual(payload["currentRequest"], stale)
        self.assertEqual(ready["diagnostics"]["retrieval"]["querySource"], "foreground_context")
        self.assertEqual(ready["diagnostics"]["retrieval"]["queryChars"], len(current))
        self.assertEqual(
            ready["diagnostics"]["contextInjection"]["resolvedRequestChars"],
            len(payload["currentRequest"]),
        )

    def test_active_rag_pending_timeout_returns_pinned_retriable_status(self) -> None:
        gate = threading.Event()
        provider = BlockingActiveRagProvider(gate)
        service = ActiveRagService(completion_provider=provider)
        request = _request(selected_text="LLM没有输出，现在只要一个框")

        with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
            started = service.start(request)
            session_id = str(started["sessionId"])
            with service._lock:
                service._sessions[session_id].created_at_ms -= 121_000
            ready = service.status(session_id)
            gate.set()

        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["pollAfterMs"], 0)
        self.assertEqual(ready["candidateCount"], 0)
        self.assertEqual(ready["candidates"][0]["sourceType"], "status")
        self.assertEqual(ready["candidates"][0]["text"], "暂未完成，可以重试")
        self.assertEqual(ready["error"], "")
        self.assertEqual(ready["candidates"][0]["metadata"]["reason"], "visible_timeout")

    def test_active_rag_visible_timeout_keeps_open_generation_stage_truthful(self) -> None:
        gate = threading.Event()
        provider = BlockingActiveRagProvider(gate)
        service = ActiveRagService(completion_provider=provider)
        request = _request(selected_text="超时后模型仍在生成")

        with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
            started = service.start(request)
            session_id = str(started["sessionId"])
            deadline = time.monotonic() + 1
            while not provider.calls and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(provider.calls, "provider must be in-flight before the visible timeout projection")
            with service._lock:
                service._sessions[session_id].created_at_ms -= 121_000

            visible_timeout = service.status(session_id)
            progress = visible_timeout["diagnostics"]["progress"]
            self.assertEqual(visible_timeout["status"], "ready")
            self.assertFalse(visible_timeout["keyPolicy"]["ready"])
            self.assertEqual(progress["stage"], "generating")
            self.assertTrue(progress["workerPending"])
            self.assertTrue(progress["model"]["attempted"])

            gate.set()
            deadline = time.monotonic() + 1
            settled = service.status(session_id)
            while settled["diagnostics"]["progress"]["stage"] != "ready" and time.monotonic() < deadline:
                time.sleep(0.01)
                settled = service.status(session_id)

        self.assertEqual(settled["status"], "ready")
        self.assertEqual(settled["diagnostics"]["progress"]["stage"], "ready")
        self.assertFalse(settled["diagnostics"]["progress"]["workerPending"])

    def test_active_rag_deepseek_prompt_gets_timeline_context_pack(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-active-rag-timeline-") as tmp:
            core = LocalSqliteCoreClient(Path(tmp) / "active-rag-timeline.sqlite")
            event_id = _record_timeline_event(core, "主动 DeepSeek 生成按钮进入候选框")
            _insert_planning_and_activity_context(core, event_id=event_id)
            fact_pack = tuple(
                {
                    "evidenceId": f"hit:atom:{index}",
                    "text": f"输入法事实原子 {index} 说明混合检索链路。",
                    "evidencePreview": f"输入法事实原子 {index} 说明混合检索链路。",
                    "title": f"当前事实 {index}",
                    "sourceType": "memory",
                    "sourceLane": "vector_raw",
                    "atomIds": [f"atom:context:{index}"],
                }
                for index in range(1, 8)
            ) + (
                {
                    "evidenceId": "hit:topic-book",
                    "text": "输入法主题书记录长期上下文架构。",
                    "evidencePreview": "输入法主题书记录长期上下文架构。",
                    "title": "输入法主题书",
                    "sourceType": "memory",
                    "sourceLane": "bm25_tags",
                    "bookIds": ["book:ime-context"],
                },
                {
                    "evidenceId": "hit:ordinary-rag",
                    "text": "普通 RAG 文档记录显式生成接口。",
                    "evidencePreview": "普通 RAG 文档记录显式生成接口。",
                    "title": "生成接口文档",
                    "sourceType": "rag",
                    "sourceLane": "bm25_raw",
                },
            )
            provider = FakeActiveRagProvider(("DeepSeek主动候选",))
            service = ActiveRagService(core=core, completion_provider=provider)
            request = _request(
                selected_text="主动候选",
                evidence_pack=fact_pack,
                max_chars=120,
            )

            with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
                started = service.start(request)
                ready = _wait_ready(service, str(started["sessionId"]))

        evidence_pack = provider.calls[0].evidence_pack
        context_packet = provider.calls[0].context_packet
        source_types = [item.get("sourceType") for item in evidence_pack]
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(len(provider.calls), 1)
        self.assertNotIn("recent_input_context", source_types)
        self.assertIn("memory", source_types)
        self.assertIn("rag", source_types)
        self.assertEqual(context_packet["schemaVersion"], "rag-ime.smart-context-packet.v1")
        self.assertEqual(context_packet["currentInput"]["mode"], "active_rag")
        self.assertTrue(context_packet["oneRing"]["events"])
        self.assertFalse(context_packet["oneRing"]["maySupportFacts"])
        self.assertEqual(context_packet["oneRing"]["baselineEvents"], 4)
        self.assertLessEqual(len(context_packet["oneRing"]["events"]), 4)
        self.assertTrue(context_packet["planning"]["items"])
        self.assertFalse(context_packet["planning"]["maySupportFacts"])
        self.assertEqual(len(context_packet["activityTimeline"]["items"]), 2)
        self.assertFalse(context_packet["activityTimeline"]["maySupportFacts"])
        grounding_types = [item["sourceType"] for item in context_packet["groundingEvidence"]]
        self.assertLessEqual(len(grounding_types), 6)
        self.assertIn("memory_atom", grounding_types)
        self.assertIn("memory_book", grounding_types)
        self.assertNotIn("activity_timeline", grounding_types)
        self.assertIn("recentCompleteInputs", context_packet["trace"]["contextSourceTokens"])
        self.assertTrue(context_packet["notebook"]["items"])
        payload = json.loads(build_deepseek_completion_messages(provider.calls[0])[1]["content"])
        self.assertIn("planning", payload["contextPacket"])
        self.assertIn("activityTimeline", payload["contextPacket"])
        self.assertIn("recentCompleteInputs", payload["contextPacket"])
        self.assertIn("groundingEvidence", payload)
        self.assertNotIn("groundingEvidence", payload["contextPacket"])
        self.assertNotIn("ragEvidenceHints", payload["contextPacket"])
        self.assertNotIn("evidenceHints", payload)
        self.assertLessEqual(len(payload["groundingEvidence"]), 6)
        diagnostics = ready["diagnostics"]
        self.assertTrue(diagnostics["contextInjection"]["applied"])
        self.assertGreater(diagnostics["contextInjection"]["contextChars"], 0)
        self.assertTrue(str(diagnostics["contextInjection"]["contextHash"]).startswith("sha256:"))
        self.assertGreaterEqual(diagnostics["retrieval"]["evidenceCount"], 1)
        self.assertGreaterEqual(diagnostics["retrieval"]["contextEvidenceCount"], 1)
        self.assertTrue(diagnostics["retrieval"]["lanes"])
        self.assertTrue(diagnostics["remoteModel"]["allowed"])
        self.assertEqual(diagnostics["remoteModel"]["provider"], "custom")
        self.assertGreaterEqual(diagnostics["remoteModel"]["elapsedMs"], 0)
        trace_names = [item["name"] for item in ready["traceEvents"]]
        self.assertIn("deepseek_request_context_built", trace_names)
        self.assertIn("deepseek_request_completed", trace_names)
        context_view = diagnostics["contextView"]
        self.assertTrue(context_view["planning"]["items"])
        self.assertEqual(len(context_view["activityTimeline"]["items"]), 2)
        self.assertLessEqual(len(context_view["recentCompleteInputs"]), 4)
        self.assertEqual(context_view["groundingEvidence"], payload["groundingEvidence"])

    def test_active_rag_keeps_short_foreground_context_primary_over_recent_voice_tail(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-active-rag-voice-context-") as tmp:
            core = LocalSqliteCoreClient(Path(tmp) / "active-rag-voice-context.sqlite")
            foreground = "前台上下文只有十五个字"
            voice_text = (
                "这是一次很长的语音输入，前面讨论了模型训练、候选窗口、流式识别和记忆整理，"
                "用户最后真正关心的是当前输入能否完整进入生成请求。" + foreground
            )
            _record_timeline_event(core, voice_text)
            provider = FakeActiveRagProvider(("近期语音输入已进入当前生成上下文",))
            service = ActiveRagService(core=core, completion_provider=provider)
            request = ActiveRagStartRequest(
                selected_text=foreground,
                selected_text_hash=stable_text_hash(foreground),
                frontend_revision=11,
                selection_epoch=5,
                context=foreground,
                project="wisdom-weasel-rag-ime",
                max_candidates=1,
                max_chars=120,
            )

            with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
                started = service.start(request)
                ready = _wait_ready(service, str(started["sessionId"]))

        sent_context = provider.calls[0].current_context
        injection = ready["diagnostics"]["contextInjection"]
        self.assertEqual(sent_context, foreground)
        self.assertEqual(injection["foregroundContextChars"], len(foreground))
        self.assertEqual(injection["effectiveContextChars"], injection["foregroundContextChars"])
        self.assertFalse(injection["augmentedWithTimelineRecentInput"])
        self.assertTrue(injection["timelineRecentInputUsedForGeneration"])
        self.assertEqual(injection["contextPolicy"], "foreground_primary_history_secondary")
        self.assertNotIn("timeline_recent_input", injection["source"])
        self.assertNotIn("recent_input_context", [item.get("sourceType") for item in provider.calls[0].evidence_pack])
        payload = json.loads(build_deepseek_completion_messages(provider.calls[0])[1]["content"])
        self.assertEqual(payload["groundingMode"], "foreground_with_history")
        self.assertTrue(payload["contextPacket"]["recentCompleteInputs"])
        self.assertIn("这是一次很长的语音输入", str(payload["contextPacket"]["recentCompleteInputs"]))
        context_view = ready["diagnostics"]["contextView"]
        self.assertEqual(context_view["source"], "provider_request")
        self.assertEqual(context_view["currentContext"], foreground)
        self.assertIn("这是一次很长的语音输入", str(context_view["recentCompleteInputs"]))

    def test_active_rag_short_unmatched_foreground_does_not_promote_recent_request(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-active-rag-short-context-") as tmp:
            core = LocalSqliteCoreClient(Path(tmp) / "active-rag-short-context.sqlite")
            recent = "请完成上下文构建、空结果恢复和前台诊断的全部修复。"
            _record_timeline_event(core, recent)
            provider = FakeActiveRagProvider(("已完成短上下文恢复链路修复",))
            service = ActiveRagService(core=core, completion_provider=provider)
            request = ActiveRagStartRequest(
                selected_text="改正",
                selected_text_hash=stable_text_hash("改正"),
                frontend_revision=15,
                selection_epoch=9,
                placement="insert_after_selection",
                intent="complete",
                context="改正",
                surrounding_before="改正",
                project="wisdom-weasel-rag-ime",
                max_candidates=1,
                max_chars=120,
            )

            with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
                started = service.start(request)
                ready = _wait_ready(service, str(started["sessionId"]))

        payload = json.loads(build_deepseek_completion_messages(provider.calls[0])[1]["content"])
        injection = ready["diagnostics"]["contextInjection"]
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(provider.calls[0].current_context, "改正")
        self.assertEqual(payload["currentRequest"], "改正")
        self.assertTrue(injection["shortForegroundCapture"])
        self.assertFalse(injection["fullForegroundDocumentCaptured"])
        self.assertEqual(injection["resolvedRequestChars"], len("改正"))
        self.assertGreater(injection["timelineRecentInputChars"], len("改正"))
        self.assertTrue(injection["timelineRecentInputUsedForGeneration"])
        self.assertEqual(payload["groundingMode"], "foreground_with_history")
        self.assertIn(recent, str(payload["contextPacket"]["recentCompleteInputs"]))

    def test_generic_foreground_terms_do_not_turn_timeline_book_into_rag_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-active-rag-generic-timeline-") as tmp:
            core = LocalSqliteCoreClient(Path(tmp) / "active-rag-generic-timeline.sqlite")
            event_id = _record_timeline_event(core, "前台上下文测试需要稳定显示当前输入内容")
            _insert_timeline_book(core, event_id=event_id)
            with core._connect() as conn:
                conn.execute(
                    """
                    UPDATE memory_books
                    SET title = ?, summary = ?, normalized_text = ?,
                        tags_json = ?, surface_hints_json = ?
                    WHERE book_id = 'book:daily:active-rag'
                    """,
                    (
                        "前台上下文运行记录",
                        "当前前台上下文测试需要稳定显示输入内容。",
                        "当前 前台 上下文 测试 输入 内容",
                        json.dumps(["前台", "上下文"], ensure_ascii=False),
                        json.dumps(["前台上下文", "当前测试"], ensure_ascii=False),
                    ),
                )
            provider = FakeActiveRagProvider(("只依据鹤白螺钉九三当前输入",))
            service = ActiveRagService(core=core, completion_provider=provider)
            request = ActiveRagStartRequest(
                selected_text="鹤白螺钉九三用于当前前台上下文测试",
                selected_text_hash=stable_text_hash("鹤白螺钉九三用于当前前台上下文测试"),
                frontend_revision=17,
                selection_epoch=11,
                context="鹤白螺钉九三用于当前前台上下文测试",
                surrounding_before="鹤白螺钉九三用于当前前台上下文测试",
                project="wisdom-weasel-rag-ime",
                max_candidates=1,
                max_chars=120,
            )

            with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
                started = service.start(request)
                ready = _wait_ready(service, str(started["sessionId"]))

        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["diagnostics"]["retrieval"]["evidenceCount"], 0)
        self.assertEqual(provider.calls[0].evidence_pack, ())
        self.assertTrue(ready["diagnostics"]["contextInjection"]["timelineRecentInputUsedForGeneration"])

    def test_active_rag_short_commit_observes_but_does_not_inject_recent_field_context(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-active-rag-short-commit-") as tmp:
            core = LocalSqliteCoreClient(Path(tmp) / "active-rag-short-commit.sqlite")
            recent = "请检查当前前台上下文是否完整注入，并在证据为空时明确降级。"
            core.record_event(
                InputEvent(
                    event_id=None,
                    created_at_ms=now_ms(),
                    source="squirrel",
                    committed_text="改正",
                    privacy_disposition="allowed",
                    recent_context=recent,
                    project="wisdom-weasel-rag-ime",
                    tags=("user-input",),
                )
            )
            provider = FakeActiveRagProvider(("已修复短提交上下文恢复",))
            service = ActiveRagService(core=core, completion_provider=provider)
            request = ActiveRagStartRequest(
                selected_text="改正",
                selected_text_hash=stable_text_hash("改正"),
                frontend_revision=16,
                selection_epoch=10,
                placement="insert_after_selection",
                intent="complete",
                context="改正",
                surrounding_before="改正",
                project="wisdom-weasel-rag-ime",
                max_candidates=1,
                max_chars=120,
            )

            with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
                started = service.start(request)
                ready = _wait_ready(service, str(started["sessionId"]))

        payload = json.loads(build_deepseek_completion_messages(provider.calls[0])[1]["content"])
        injection = ready["diagnostics"]["contextInjection"]
        self.assertEqual(provider.calls[0].current_context, "改正")
        self.assertEqual(payload["currentRequest"], "改正")
        self.assertGreater(injection["timelineRecentInputChars"], len("改正"))
        self.assertEqual(injection["resolvedRequestChars"], len("改正"))
        self.assertTrue(injection["timelineRecentInputUsedForGeneration"])
        self.assertEqual(payload["groundingMode"], "foreground_with_history")
        self.assertIn(recent, str(payload["contextPacket"]["recentCompleteInputs"]))

    def test_active_rag_local_timeline_context_does_not_impersonate_fact_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-active-rag-local-timeline-") as tmp:
            core = LocalSqliteCoreClient(Path(tmp) / "active-rag-local-timeline.sqlite")
            event_id = _record_timeline_event(core, "LLM 候选需要明确显示模型预测来源")
            _insert_timeline_book(core, event_id=event_id)
            service = ActiveRagService(core=core)
            request = ActiveRagStartRequest(
                selected_text="主动候选",
                selected_text_hash=stable_text_hash("主动候选"),
                frontend_revision=7,
                selection_epoch=3,
                context="LLM不显示，RAG能命中，并且DeepSeek需要根据笔记本预测",
                project="wisdom-weasel-rag-ime",
                max_candidates=1,
                max_chars=18,
            )
            started = service.start(request)
            ready = _wait_ready(service, str(started["sessionId"]))

        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["evidenceCount"], 0)
        self.assertNotEqual(ready["candidates"][0]["text"], "最近输入上下文")
        self.assertEqual(ready["candidates"][0]["sourceType"], "status")
        injection = ready["diagnostics"]["contextInjection"]
        self.assertGreater(injection["timelineRecentInputChars"], 0)
        self.assertGreater(injection["timelineRecentInputRecordCount"], 0)
        self.assertFalse(injection["timelineRecentInputUsedForGeneration"])
        self.assertEqual(injection["contextPolicy"], "foreground_primary_history_secondary")
        self.assertIn("recent_input_history_available_not_injected", injection["warnings"])

    def test_unprojected_timeline_book_never_impersonates_hybrid_memory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-active-rag-local-fallback-") as tmp:
            core = LocalSqliteCoreClient(Path(tmp) / "active-rag-local-fallback.sqlite")
            event_id = _record_timeline_event(core, "LLM消失LLM")
            _insert_timeline_book(core, event_id=event_id)
            service = ActiveRagService(core=core)
            request = ActiveRagStartRequest(
                selected_text="主动候选",
                selected_text_hash=stable_text_hash("主动候选"),
                frontend_revision=7,
                selection_epoch=3,
                context="LLM不显示，RAG能命中，并且DeepSeek需要根据笔记本预测",
                project="wisdom-weasel-rag-ime",
                max_candidates=1,
                max_chars=18,
            )
            started = service.start(request)
            ready = _wait_ready(service, str(started["sessionId"]))

        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["candidates"][0]["sourceType"], "status")
        self.assertTrue(ready["candidates"][0]["text"])
        self.assertNotEqual(ready["candidates"][0]["text"], "修复LLM显示")
        self.assertNotIn("request_fallback", json.dumps(ready, ensure_ascii=False))

    def test_active_rag_keeps_foreground_beyond_old_1200_character_limit(self) -> None:
        foreground = "这段前台输入用于验证模型能收到完整工程上下文，而不是只留下固定长度的尾部。" * 45
        self.assertGreater(len(foreground), 1200)
        provider = FakeActiveRagProvider(("完整上下文已经进入最终模型请求",))
        service = ActiveRagService(completion_provider=provider)
        request = ActiveRagStartRequest(
            selected_text="完整上下文",
            selected_text_hash=stable_text_hash("完整上下文"),
            frontend_revision=21,
            selection_epoch=4,
            context=foreground,
            project="wisdom-weasel-rag-ime",
            max_candidates=1,
            max_chars=120,
        )

        with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
            started = service.start(request)
            ready = _wait_ready(service, str(started["sessionId"]))

        self.assertEqual(ready["status"], "ready")
        self.assertEqual(provider.calls[0].current_context, foreground)
        self.assertFalse(ready["diagnostics"]["contextInjection"]["contextTruncatedToBudget"])

    def test_active_rag_final_packet_caps_recent_inputs_at_four(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-active-rag-context-budget-") as tmp:
            core = LocalSqliteCoreClient(Path(tmp) / "active-rag-context-budget.sqlite")
            for index in range(1, 41):
                _record_timeline_event(core, f"最近完整输入记录{index}包含足够语义用于连续上下文")
            provider = FakeActiveRagProvider(("动态预算已经完成最终上下文选择",))
            service = ActiveRagService(core=core, completion_provider=provider)
            request = ActiveRagStartRequest(
                selected_text="继续处理上下文",
                selected_text_hash=stable_text_hash("继续处理上下文"),
                frontend_revision=22,
                selection_epoch=5,
                context="请结合最近完整输入继续处理上下文预算。",
                project="wisdom-weasel-rag-ime",
                max_candidates=1,
                max_chars=120,
            )

            with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
                started = service.start(request)
                ready = _wait_ready(service, str(started["sessionId"]))

        trace = provider.calls[0].context_packet["trace"]
        payload = json.loads(build_deepseek_completion_messages(provider.calls[0])[1]["content"])
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(trace["contextSourceCounts"]["recentInputs"], 4)
        self.assertLessEqual(trace["estimatedContextTokens"], trace["availableContextTokens"])
        self.assertTrue(trace["withinSoftBudget"])
        self.assertEqual(
            len(payload["contextPacket"]["recentCompleteInputs"]),
            trace["contextSourceCounts"]["recentInputs"],
        )
        self.assertEqual(
            payload["contextPacket"]["contextBudget"]["contextSourceCounts"],
            trace["contextSourceCounts"],
        )

    def test_active_rag_accept_validates_selection_anchor(self) -> None:
        provider = FakeActiveRagProvider(("DeepSeek主动候选",))
        service = ActiveRagService(completion_provider=provider)
        request = _request(selected_text="选区")

        with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
            started = service.start(request)
            ready = _wait_ready(service, str(started["sessionId"]))

        rejected = service.accept(
            session_id=str(ready["sessionId"]),
            candidate_id=str(ready["candidates"][0]["candidateId"]),
            selected_text_hash=stable_text_hash("别的选区"),
            frontend_revision=request.frontend_revision,
            selection_epoch=request.selection_epoch,
        )

        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["reason"], "selected_text_hash_mismatch")

    def test_active_rag_accept_validates_panel_and_front_app_when_supplied(self) -> None:
        service = ActiveRagService()
        request = _request(selected_text="选区", panel_session_id="panel-a", front_app_bundle_id="app.a")
        started = service.start(request)
        ready = _wait_ready(service, str(started["sessionId"]))

        rejected_panel = service.accept(
            session_id=str(ready["sessionId"]),
            candidate_id=str(ready["candidates"][0]["candidateId"]),
            selected_text_hash=request.selected_text_hash,
            frontend_revision=request.frontend_revision,
            selection_epoch=request.selection_epoch,
            panel_session_id="panel-b",
            front_app_bundle_id="app.a",
        )
        rejected_app = service.accept(
            session_id=str(ready["sessionId"]),
            candidate_id=str(ready["candidates"][0]["candidateId"]),
            selected_text_hash=request.selected_text_hash,
            frontend_revision=request.frontend_revision,
            selection_epoch=request.selection_epoch,
            panel_session_id="panel-a",
            front_app_bundle_id="app.b",
        )

        self.assertEqual(rejected_panel["reason"], "panel_session_id_mismatch")
        self.assertEqual(rejected_app["reason"], "front_app_bundle_id_mismatch")

    def test_active_rag_accept_records_feedback_event(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-active-rag-feedback-") as tmp:
            core = LocalSqliteCoreClient(Path(tmp) / "active-rag.sqlite")
            service = ActiveRagService(core=core)
            request = _request(selected_text="选区", panel_session_id="panel-a", front_app_bundle_id="app.a")
            started = service.start(request)
            ready = _wait_ready(service, str(started["sessionId"]))

            accepted = service.accept(
                session_id=str(ready["sessionId"]),
                candidate_id=str(ready["candidates"][0]["candidateId"]),
                selected_text_hash=request.selected_text_hash,
                frontend_revision=request.frontend_revision,
                selection_epoch=request.selection_epoch,
                panel_session_id=request.panel_session_id,
                front_app_bundle_id=request.front_app_bundle_id,
            )
            with core._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT candidate_id, candidate_text, action, context_hash, metadata_json
                    FROM memory_feedback_events
                    ORDER BY id ASC
                    """
                ).fetchall()

        self.assertTrue(accepted["ok"])
        self.assertGreaterEqual(len(rows), 2)
        actions = [row["action"] for row in rows]
        self.assertIn("active_rag_shown", actions)
        self.assertIn("active_rag_accept", actions)
        accept_row = next(row for row in rows if row["action"] == "active_rag_accept")
        self.assertEqual(accept_row["candidate_text"], "主动候选")
        self.assertEqual(accept_row["context_hash"], request.selected_text_hash)
        self.assertIn("panel-a", accept_row["metadata_json"])

    def test_active_rag_cancel_records_feedback_event(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-active-rag-cancel-") as tmp:
            core = LocalSqliteCoreClient(Path(tmp) / "active-rag.sqlite")
            gate = threading.Event()
            provider = BlockingActiveRagProvider(gate)
            service = ActiveRagService(core=core, completion_provider=provider)
            request = ActiveRagStartRequest(
                selected_text="准备取消的选区",
                selected_text_hash=stable_text_hash("准备取消的选区"),
                frontend_revision=9,
                selection_epoch=4,
                panel_session_id="panel-cancel",
                front_app_bundle_id="app.cancel",
            )
            with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
                started = service.start(request)
                cancelled = service.cancel(str(started["sessionId"]))
                gate.set()
                provider.released.wait(timeout=0.2)
            with core._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT action, context_hash, metadata_json
                    FROM memory_feedback_events
                    ORDER BY id ASC
                    """
                ).fetchall()

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual([row["action"] for row in rows], ["active_rag_cancel"])
        self.assertEqual(rows[0]["context_hash"], request.selected_text_hash)
        self.assertIn("panel-cancel", rows[0]["metadata_json"])

    def test_active_rag_deepseek_stale_session_dropped(self) -> None:
        gate = threading.Event()
        provider = BlockingActiveRagProvider(gate)
        service = ActiveRagService(completion_provider=provider)
        first = _request(selected_text="第一段选区", frontend_revision=1, selection_epoch=1)
        second = _request(selected_text="第二段选区", frontend_revision=2, selection_epoch=2)

        with patch.dict(os.environ, {"RAG_IME_DEEPSEEK_ACTIVE_RAG": "1"}):
            first_started = service.start(first)
            second_started = service.start(second)
            first_status = service.status(str(first_started["sessionId"]))
            gate.set()
            second_ready = _wait_ready(service, str(second_started["sessionId"]))

        self.assertEqual(first_status["status"], "stale_dropped")
        self.assertEqual(second_ready["status"], "ready")
        self.assertIn("第二个主动候选", [item["text"] for item in second_ready["candidates"]])


class FakeActiveRagProvider:
    def __init__(self, texts: tuple[str, ...], *, metadata: dict[str, object] | None = None):
        self.texts = texts
        self.metadata = dict(metadata or {})
        self.calls: list[object] = []

    def stream_candidates(self, request):
        self.calls.append(request)
        for text in self.texts:
            yield CompletionCandidateDelta(text=text, insert_text=text, metadata=dict(self.metadata))


class SequencedActiveRagProvider:
    def __init__(self, responses: tuple[tuple[str, ...], ...]):
        self.responses = responses
        self.calls: list[object] = []

    def stream_candidates(self, request):
        call_index = len(self.calls)
        self.calls.append(request)
        texts = self.responses[min(call_index, len(self.responses) - 1)]
        for text in texts:
            yield CompletionCandidateDelta(text=text, insert_text=text)


class FailingActiveRagProvider:
    def __init__(self):
        self.calls: list[object] = []

    def stream_candidates(self, request):
        self.calls.append(request)
        # The service deliberately trusts the producer's structured
        # terminal/transport pair, not the legacy compact exception message.
        # Keep this fixture a governed empty-result recovery rather than an
        # unclassified provider failure.
        raise DeepSeekCompletionError(
            "active_rag_no_insertable_content:governor_rejected_content",
            diagnostics={
                "terminalReason": "governor_rejected_content",
                "transportReason": "empty_remote_content",
            },
        )
        yield


class DiagnosticFailingActiveRagProvider:
    def stream_candidates(self, request):
        _ = request
        raise DeepSeekCompletionError(
            "active_rag_no_insertable_content:upstream_stream_closed",
            diagnostics={
                "terminalReason": "upstream_stream_closed",
                "transportReason": "url_error:ConnectionResetError",
                "attemptCount": 1,
                "hadContent": True,
                "contentChars": 7,
                "reasoningChars": 0,
                "safePartialChars": 0,
                "responsePreview": "不应写入默认日志",
            },
        )
        yield


class BlockingActiveRagProvider:
    def __init__(self, gate: threading.Event):
        self.gate = gate
        self.released = threading.Event()
        self.calls: list[object] = []

    def stream_candidates(self, request):
        self.calls.append(request)
        self.gate.wait(timeout=2)
        self.released.set()
        yield CompletionCandidateDelta(text="第二个主动候选", insert_text="第二个主动候选")


class CancellableManagedActiveRagProvider:
    uses_managed_pi = True

    def __init__(self):
        self.gate = threading.Event()
        self.started = threading.Event()
        self.released = threading.Event()
        self.calls: list[object] = []
        self.cancelled_ids: list[str] = []

    def stream_candidates(self, request):
        self.calls.append(request)
        self.started.set()
        self.gate.wait(timeout=2)
        self.released.set()
        yield CompletionCandidateDelta(text="取消前不应落地", insert_text="取消前不应落地")

    def cancel(self, request_id: str) -> None:
        self.cancelled_ids.append(request_id)


class BlockingRecoveryActiveRagProvider:
    def __init__(self):
        self.calls: list[object] = []
        self.recovery_started = threading.Event()
        self.release = threading.Event()

    def stream_candidates(self, request):
        self.calls.append(request)
        if len(self.calls) == 1:
            return
        self.recovery_started.set()
        self.release.wait(timeout=2)
        yield CompletionCandidateDelta(
            text="质量检查后返回可插入正文",
            insert_text="质量检查后返回可插入正文",
        )


class StreamingActiveRagProvider:
    supports_text_delta_callback = True

    def __init__(self):
        self.emitted = threading.Event()
        self.release = threading.Event()

    def stream_candidates(self, request, *, on_text_delta=None):
        _ = request
        if on_text_delta is not None:
            on_text_delta("第一段正在流式返回")
        self.emitted.set()
        self.release.wait(timeout=2)
        yield CompletionCandidateDelta(text="第一段已经完整返回", insert_text="第一段已经完整返回")


class InterruptedStreamingActiveRagProvider:
    supports_text_delta_callback = True

    def __init__(self):
        self.emitted = threading.Event()
        self.release = threading.Event()

    def stream_candidates(self, request, *, on_text_delta=None):
        _ = request
        if on_text_delta is not None:
            on_text_delta("已经生成的正文必须保留给用户确认。")
        self.emitted.set()
        self.release.wait(timeout=2)
        raise DeepSeekCompletionError("active_rag_no_insertable_content:stream_interrupted")
        yield


class EmptyAfterPartialActiveRagProvider:
    supports_text_delta_callback = True

    def stream_candidates(self, request, *, on_text_delta=None):
        _ = request
        if on_text_delta is not None:
            on_text_delta("流已经安全显示，空结束也不能把它清掉")
        return
        yield


def _record_timeline_event(core: LocalSqliteCoreClient, text: str) -> int:
    memory_id = core.record_event(
        InputEvent(
            event_id=None,
            created_at_ms=now_ms(),
            source="manual",
            committed_text=text,
            privacy_disposition="allowed",
            recent_context="Active RAG 前台验收",
            project="wisdom-weasel-rag-ime",
            tags=("RAG", "DeepSeek"),
        )
    )
    return int(str(memory_id).split(":", 1)[1])


def _insert_timeline_book(core: LocalSqliteCoreClient, *, event_id: int) -> None:
    core.initialize()
    timestamp = now_ms()
    with core._connect() as conn:
        conn.execute(
            """
            INSERT INTO memory_books(
                book_id, book_type, book_key, title, summary, normalized_text,
                project, app, tags_json, surface_hints_json, query_expansions_json,
                source_event_ids_json, memory_atom_ids_json, status, confidence,
                quality_score, created_at_ms, updated_at_ms, metadata_json
            )
            VALUES (?, 'daily', '2026-07-07', ?, ?, ?, ?, '', ?, ?, ?, ?, '[]', 'active', 0.8, 0.8, ?, ?, '{}')
            """,
            (
                "book:daily:active-rag",
                "Active RAG 时间线",
                "用户希望 RAG 先做 evidence，再由 DeepSeek 生成一条可确认候选。",
                "active rag timeline deepseek",
                "wisdom-weasel-rag-ime",
                json.dumps(["RAG", "DeepSeek"], ensure_ascii=False),
                json.dumps(["DeepSeek 生成", "主动候选"], ensure_ascii=False),
                json.dumps(["Active RAG", "候选框"], ensure_ascii=False),
                json.dumps([event_id], ensure_ascii=False),
                timestamp,
                timestamp,
            ),
        )


def _insert_planning_and_activity_context(
    core: LocalSqliteCoreClient,
    *,
    event_id: int,
) -> None:
    core.initialize()
    timestamp = now_ms()
    day = local_date_string()
    segments = [
        {
            "segmentId": "task:memory",
            "title": "整理记忆召回",
            "summary": "梳理个人记忆、主题书与混合检索。",
            "startMs": timestamp - 7_200_000,
            "endMs": timestamp - 5_400_000,
            "apps": ["Codex", "Ghostty"],
            "eventCount": 8,
            "sourceEventIds": [event_id],
        },
        {
            "segmentId": "task:generation",
            "title": "验证候选生成",
            "summary": "确认计划与时间线进入最终 provider payload。",
            "startMs": timestamp - 5_200_000,
            "endMs": timestamp - 3_800_000,
            "apps": ["Ghostty"],
            "eventCount": 6,
            "sourceEventIds": [event_id],
        },
        {
            "segmentId": "task:ui",
            "title": "完善控制中心",
            "summary": "第三项语义任务用于验证最多注入两项。",
            "startMs": timestamp - 3_000_000,
            "endMs": timestamp - 2_000_000,
            "apps": ["Chrome"],
            "eventCount": 4,
            "sourceEventIds": [event_id],
        },
    ]
    with core._connect() as conn:
        conn.execute(
            """
            INSERT INTO planning_daily(
                plan_id, plan_date, project, intention, notes, reflection,
                assistant_summary, created_at_ms, updated_at_ms, metadata_json
            ) VALUES (?, ?, ?, ?, '', '', '', ?, ?, '{}')
            """,
            (
                "plan:active-rag",
                day,
                "wisdom-weasel-rag-ime",
                "完成显式生成上下文注入",
                timestamp,
                timestamp,
            ),
        )
        conn.execute(
            """
            INSERT INTO planning_tasks(
                task_id, plan_date, title, detail, status, priority, due_at_ms,
                project, goal_id, source, confidence, created_at_ms, updated_at_ms,
                completed_at_ms, metadata_json
            ) VALUES (?, ?, ?, ?, 'in_progress', 5, NULL, ?, '', 'manual', 1.0, ?, ?, NULL, '{}')
            """,
            (
                "task:active-rag-context",
                day,
                "修复生成上下文链路",
                "让当前请求、计划、活动时间线与事实证据保持类型边界。",
                "wisdom-weasel-rag-ime",
                timestamp,
                timestamp,
            ),
        )
        conn.execute(
            """
            INSERT INTO daily_activity_timelines(
                timeline_id, project, timeline_date, timezone, status,
                source_event_ids_json, source_event_hash, segments_json,
                summary_text, event_count, segment_count, approved_book_id,
                approved_by, approved_at_ms, rejection_reason, metadata_json,
                created_at_ms, updated_at_ms
            ) VALUES (?, ?, ?, 'Asia/Shanghai', 'approved', ?, ?, ?, ?, 18, 3,
                      '', 'user:test', ?, '', '{}', ?, ?)
            """,
            (
                "activity-timeline:active-rag",
                "wisdom-weasel-rag-ime",
                day,
                json.dumps([event_id], ensure_ascii=False),
                "sha256:active-rag-approved-timeline",
                json.dumps(segments, ensure_ascii=False),
                "上午完成记忆召回与候选生成链路。",
                timestamp,
                timestamp,
                timestamp,
            ),
        )


def _request(
    *,
    selected_text: str,
    frontend_revision: int = 7,
    selection_epoch: int = 3,
    panel_session_id: str = "",
    front_app_bundle_id: str = "",
    max_candidates: int = 1,
    max_chars: int = 24,
    latency_budget_ms: int = 120_000,
    evidence_pack: tuple[dict[str, object], ...] | None = None,
) -> ActiveRagStartRequest:
    return ActiveRagStartRequest(
        selected_text=selected_text,
        selected_text_hash=stable_text_hash(selected_text),
        frontend_revision=frontend_revision,
        selection_epoch=selection_epoch,
        panel_session_id=panel_session_id,
        front_app_bundle_id=front_app_bundle_id,
        context="RAG 输入法主动改写",
        evidence_pack=evidence_pack or ({"surfaceHints": ["主动候选"], "tags": ["RAG"]},),
        max_candidates=max_candidates,
        max_chars=max_chars,
        latency_budget_ms=latency_budget_ms,
    )


def _wait_ready(service: ActiveRagService, session_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 2
    last = service.status(session_id)
    while time.monotonic() < deadline:
        last = service.status(session_id)
        if last["status"] in {"ready", "error", "stale_dropped", "cancelled"}:
            return last
        time.sleep(0.01)
    return last


if __name__ == "__main__":
    unittest.main()
