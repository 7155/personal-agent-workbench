from __future__ import annotations

import json
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from rag_ime.debug_server import DebugImeService, DebugServerConfig
from rag_ime.contracts.json_schema import validate_contract
from rag_ime.text_utils import stable_text_hash


class ActiveRagDebugServerTests(unittest.TestCase):
    def test_debug_service_caps_active_rag_request_to_the_visible_generation_budget(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            closing(DebugImeService(DebugServerConfig(
                db_path=Path(temp_dir) / "active-rag-budget.sqlite",
                seed_if_empty=False,
            ))) as service,
        ):
            service.settings_update({"activeRag.latencyBudgetMs": 6000})

            capped = service._active_rag_request_from_payload(
                {
                    "selectedText": "按设置收起生成框",
                    "privacyDisposition": "allowed",
                    "latencyBudgetMs": 30000,
                }
            )
            shorter = service._active_rag_request_from_payload(
                {
                    "selectedText": "允许客户端更早结束",
                    "privacyDisposition": "allowed",
                    "latencyBudgetMs": 4000,
                }
            )

        self.assertEqual(capped.latency_budget_ms, 6000)
        self.assertEqual(shorter.latency_budget_ms, 4000)

    def test_debug_service_normalizes_accessibility_window_context(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            closing(DebugImeService(DebugServerConfig(
                db_path=Path(temp_dir) / "active-rag-window.sqlite",
                seed_if_empty=False,
            ))) as service,
        ):
            request = service._active_rag_request_from_payload(
                {
                    "selectedText": "结合当前窗口继续写",
                    "privacyDisposition": "allowed",
                    "windowContext": {
                        "schemaVersion": "rag-ime.window-context.v1",
                        "captureMode": "accessibility_semantics",
                        "snapshotId": "axsnap_1",
                        "revision": 2,
                        "privacyDisposition": "allowed",
                        "application": {
                            "pid": 42,
                            "bundleId": "app.test",
                            "name": "Test",
                            "windowTitle": "项目计划",
                        },
                        "focusedNodeRef": "ax_text",
                        "nodes": [
                            {
                                "nodeRef": "ax_text",
                                "parentRef": "",
                                "depth": 0,
                                "role": "AXTextArea",
                                "label": "正文",
                                "value": "真实窗口语义",
                                "focused": True,
                            }
                        ],
                    },
                }
            )

        self.assertEqual(request.window_context["captureMode"], "accessibility_semantics")
        self.assertEqual(request.window_context["application"]["windowTitle"], "项目计划")
        self.assertEqual(request.window_context["nodes"][0]["value"], "真实窗口语义")

    def test_debug_service_enriches_and_revalidates_application_semantics(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            closing(DebugImeService(DebugServerConfig(
                db_path=Path(temp_dir) / "active-rag-zed.sqlite",
                seed_if_empty=False,
            ))) as service,
        ):
            semantics = {
                "source": "zed_workspace_state",
                "projectName": "Project",
                "activeFile": "src/main.py",
                "editorExcerpt": "1: print('hello')",
                "editorExcerptStartLine": 1,
                "projectEntries": ["src/", "README.md"],
                "contentOrigin": "workspace_file",
                "trust": {"mayLagUnsavedChanges": True},
            }
            with patch(
                "rag_ime.debug_server.enrich_window_context_with_app_semantics",
                side_effect=lambda context: {
                    **context,
                    "applicationSemantics": semantics,
                },
            ):
                request = service._active_rag_request_from_payload(
                    {
                        "selectedText": "解释终端上方代码",
                        "privacyDisposition": "allowed",
                        "windowContext": {
                            "schemaVersion": "rag-ime.window-context.v1",
                            "captureMode": "accessibility_semantics",
                            "snapshotId": "axsnap_zed",
                            "revision": 1,
                            "privacyDisposition": "allowed",
                            "application": {
                                "pid": 42,
                                "bundleId": "dev.zed.Zed",
                                "name": "Zed",
                                "windowTitle": "Project — main.py",
                            },
                            "nodes": [],
                        },
                    }
                )

        app_semantics = request.window_context["applicationSemantics"]
        self.assertEqual(app_semantics["activeFile"], "src/main.py")
        self.assertTrue(app_semantics["trust"]["mayLagUnsavedChanges"])
        self.assertNotIn("absolutePath", json.dumps(app_semantics))

    def test_debug_service_blocks_sensitive_active_rag_without_storing_hash_or_candidate(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            closing(DebugImeService(DebugServerConfig(
                db_path=Path(temp_dir) / "active-rag-sensitive.sqlite",
                seed_if_empty=False,
            ))) as service,
        ):
            secret = "password=hunter2"

            blocked = service.active_rag_start(
                {
                    "selectedText": secret,
                    "privacyDisposition": "allowed",
                    "selectedTextHash": stable_text_hash(secret),
                    "context": secret,
                    "sensitiveField": True,
                    "frontendRevision": 4,
                    "selectionEpoch": 8,
                }
            )
            status = service.active_rag_status({"sessionId": blocked["sessionId"]})

        blob = json.dumps(blocked, ensure_ascii=False)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(status["status"], "blocked")
        self.assertEqual(blocked["candidateCount"], 0)
        self.assertFalse(blocked["diagnostics"]["retrieval"]["called"])
        self.assertEqual(blocked["diagnostics"]["remoteModel"]["skipReason"], "sensitive_field_blocked")
        self.assertNotIn(secret, blob)
        self.assertNotIn("sha256:", blob)
        validate_contract(blocked, "active-rag-status.v1.json")

    def test_debug_service_exposes_active_rag_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, closing(DebugImeService(
                DebugServerConfig(
                    db_path=Path(temp_dir) / "active-rag-debug.sqlite",
                    seed_if_empty=False,
                )
            )) as service:

            started = service.active_rag_start(
                {
                    "selectedText": "选区 RAG 助手显式触发",
                    "privacyDisposition": "allowed",
                    "frontendRevision": 1,
                    "selectionEpoch": 1,
                    "panelSessionId": "panel-1",
                    "frontAppBundleId": "app.test",
                    "evidencePack": [{"surfaceHints": ["显式 RAG 候选"], "tags": ["RAG"]}],
                }
            )
            ready = _wait_ready(service, str(started["sessionId"]))
            cancelled = service.active_rag_cancel({"sessionId": str(started["sessionId"])})

            self.assertEqual(started["status"], "pending")
            self.assertGreater(started["pollAfterMs"], 0)
            self.assertEqual(started["candidates"][0]["sourceType"], "status")
            self.assertIn("正在生成", started["candidates"][0]["text"])
            self.assertTrue(started["candidates"][0]["metadata"]["animated"])
            self.assertEqual(ready["status"], "ready")
            self.assertEqual(ready["pollAfterMs"], 0)
            self.assertEqual(ready["uiMode"], "active_rag_assist")
            self.assertFalse(ready["keyPolicy"]["thinkingRowSelectable"])
            self.assertEqual(cancelled["status"], "ready")
            validate_contract(started, "active-rag-status.v1.json")
            validate_contract(ready, "active-rag-status.v1.json")

    def test_active_rag_preview_is_read_only_and_redacts_selected_text(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            closing(DebugImeService(DebugServerConfig(
                db_path=Path(temp_dir) / "active-rag-debug.sqlite",
                seed_if_empty=False,
            ))) as service,
        ):
            selected_text = "这段真实选区只允许显式请求进入后端，不能出现在默认调试响应"
            current_context = "通过辅助功能捕获的光标上下文"

            preview = service.active_rag_preview(
                {
                    "selectedText": selected_text,
                    "privacyDisposition": "allowed",
                    "selectedTextChars": len(selected_text),
                    "currentContext": current_context,
                    "contextSource": "accessibility_selected_text",
                    "contextChars": len(current_context),
                    "contextHash": stable_text_hash(current_context),
                    "frontendRevision": 3,
                    "selectionEpoch": 5,
                    "panelSessionId": "panel-preview",
                    "frontAppBundleId": "app.preview",
                    "evidencePack": [{"surfaceHints": ["主动候选短语"], "tags": ["demo"]}],
                    "maxCandidates": 3,
                }
            )
            with service.core._connect() as conn:
                feedback_count = conn.execute("SELECT COUNT(*) FROM memory_feedback_events").fetchone()[0]

        serialized = json.dumps(preview, ensure_ascii=False, sort_keys=True)
        self.assertTrue(preview["ok"])
        self.assertTrue(preview["dryRun"])
        self.assertEqual(preview["status"], "ready")
        self.assertEqual(preview["sessionId"], "active-rag:preview")
        self.assertEqual(feedback_count, 0)
        self.assertNotIn(selected_text, serialized)
        self.assertEqual(preview["selectedTextHash"], stable_text_hash(selected_text))
        self.assertEqual(preview["keyPolicy"]["numberKeys"], "select_candidate_when_ready_else_noop")
        self.assertEqual(preview["keyPolicy"]["tab"], "accept_top_when_ready")
        self.assertFalse(preview["diagnostics"]["contextInjection"]["applied"])
        self.assertEqual(preview["diagnostics"]["contextInjection"]["source"], "accessibility_selected_text")
        self.assertEqual(preview["diagnostics"]["contextInjection"]["contextChars"], len(current_context))
        self.assertEqual(preview["diagnostics"]["contextInjection"]["contextHash"], stable_text_hash(current_context))
        self.assertEqual(preview["diagnostics"]["remoteModel"]["skipReason"], "local_only_request")
        self.assertNotIn(selected_text, json.dumps(preview["diagnostics"], ensure_ascii=False))
        self.assertNotIn(current_context, json.dumps(preview["diagnostics"], ensure_ascii=False))

    def test_missing_and_unknown_privacy_block_before_retrieval_provider_or_hash(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            closing(DebugImeService(DebugServerConfig(
                db_path=Path(temp_dir) / "active-rag-privacy.sqlite",
                seed_if_empty=False,
            ))) as service,
        ):
            secret = "selected text must not be hashed or sent"
            with patch.object(
                service.active_rag,
                "_retrieve_local_evidence",
                side_effect=AssertionError("retriever must not run"),
            ) as retriever, patch.object(
                service.active_rag,
                "_deepseek_candidates",
                side_effect=AssertionError("provider must not run"),
            ) as provider:
                missing = service.active_rag_start({"selectedText": secret})
                unknown = service.active_rag_start(
                    {"selectedText": secret, "privacyDisposition": "unknown"}
                )

            for response in (missing, unknown):
                blob = json.dumps(response, ensure_ascii=False)
                self.assertEqual(response["status"], "blocked")
                self.assertTrue(response["noStore"])
                self.assertFalse(response["stored"])
                self.assertEqual(response["privacyAssessment"]["disposition"], "unknown")
                self.assertEqual(response["selectedTextHash"], "")
                self.assertFalse(response["diagnostics"]["retrieval"]["called"])
                self.assertFalse(response["diagnostics"]["remoteModel"]["requested"])
                self.assertNotIn(secret, blob)
                self.assertNotIn("sha256:", blob)
            retriever.assert_not_called()
            provider.assert_not_called()


def _wait_ready(service: DebugImeService, session_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 2
    last = service.active_rag_status({"sessionId": session_id})
    while time.monotonic() < deadline:
        last = service.active_rag_status({"sessionId": session_id})
        if last.get("status") in {"ready", "error", "stale_dropped", "cancelled"}:
            return last
        time.sleep(0.01)
    return last


if __name__ == "__main__":
    unittest.main()
