from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rag_ime.agent_context_runtime import AgentContextRuntime
from rag_ime.agent_prompt_delivery import AgentPromptDeliveryService
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.debug_server import DebugImeService
from rag_ime.embeddings import HashingEmbeddingProvider
from rag_ime.local_sqlite_core import LocalSqliteCoreClient
from rag_ime.memory_maintenance_settings import MemoryMaintenanceSettings
from tests import test_companion_query_refresh as query_fixture


class MemoryConversationReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-memory-receipt-")
        db_path = Path(self.tmp.name) / "trace.sqlite"
        sessions = AgentSessionStore(db_path)
        sessions.initialize()
        self.session_id = str(sessions.create(title="Memory receipt test")["id"])
        self.context = AgentContextRuntime(db_path)
        self.context.initialize()
        self.delivery = AgentPromptDeliveryService(
            sessions=SimpleNamespace(),
            context_runtime=self.context,
            runtime_provider=lambda: None,
            runtime_tool_manifest=lambda _session: [],
            room_public_recovery_context=lambda _session_id: "",
            memory_enabled_provider=lambda: True,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def memory_node(self, items, **kwargs):
        trace_id = self.context.begin_trace(self.session_id, source_kind="user")
        session_node = self.context.add_trace_node(
            trace_id, stage="session", label="Session", source_kind="gateway"
        )
        self.delivery._trace_memory(
            trace_id, session_node=session_node, memory_items=items,
            async_items=[], char_count=128, **kwargs,
        )
        return next(node for node in self.context.trace(trace_id)["nodes"]
                    if node["stage"] == "memory_recall")

    def test_counts_sources_instead_of_context_packs_and_keeps_whole_links(self):
        ids = [f"atom:test:{index}:" + "a" * 70 for index in range(8)]
        node = self.memory_node([{"payload": {
            "trigger": "first_user_prompt", "recallId": "recall:test",
            "items": [{"sourceType": "memory_atom", "sourceId": value,
                       "title": f"记忆标题 {index}"} for index, value in enumerate(ids)],
        }}])
        self.assertEqual(node["metadata"]["hitCount"], 8)
        self.assertEqual(node["metadata"]["itemCount"], 1)
        self.assertEqual(node["metadata"]["recallTrigger"], "first_user_prompt")
        self.assertEqual(node["metadata"]["recallStatus"], "included")
        self.assertEqual(node["label"], "本轮记忆召回")
        self.assertEqual(node["summary"], "已加入首问查询召回的记忆包")
        links = node["metadata"]["memoryAtomIds"].split(",")
        self.assertTrue(links)
        self.assertTrue(set(links).issubset(ids), "Truncated identifiers must not become links")

    def test_empty_recall_is_visible_without_claiming_a_hit(self):
        node = self.memory_node([{"payload": {"trigger": "first_user_prompt", "items": []}}])
        self.assertEqual(node["metadata"]["hitCount"], 0)
        self.assertEqual(node["metadata"]["recallStatus"], "empty")
        self.assertEqual(node["summary"], "首问记忆召回完成，未命中可用来源")

    def test_failure_and_disabled_state_are_not_reported_as_empty_success(self):
        for bootstrap, expected in [
            ({"status": "recall_failed", "ok": False, "errorCode": "memory_bootstrap_failed"}, "failed"),
            ({"status": "disabled", "ok": True}, "disabled"),
        ]:
            with self.subTest(expected=expected):
                node = self.memory_node([], bootstrap=bootstrap)
                self.assertEqual(node["metadata"]["recallStatus"], expected)
                self.assertNotIn("hitCount", node["metadata"])
                self.assertEqual(node["label"], "本轮记忆召回")
                self.assertEqual(node["summary"],
                    "本轮记忆召回失败，当前消息仍可继续" if expected == "failed" else "记忆召回已关闭")

    def test_steer_records_reuse_without_claiming_another_search(self):
        node = self.memory_node([], delivery="steer", bootstrap={
            "status": "ready", "reused": True, "sourceCount": 8,
        })
        self.assertEqual(node["metadata"]["recallStatus"], "reused")
        self.assertEqual(node["metadata"]["hitCount"], 8)
        self.assertEqual(node["summary"], "沿用当前会话已有记忆，没有重复查询")

    def test_turn_start_copy_tracks_existing_source_query_and_included_project_fact(self):
        owner = query_fixture.CompanionQueryRefreshTests()
        owner.setUp()
        self.addCleanup(owner.doCleanups)
        with patch.object(owner.service.runtime, "prompt", return_value={
                "accepted": True, "turnId": "copy-first-turn", "piEntryId": "copy-first-entry"}), \
             patch.object(owner.service.prompt_delivery_application, "runtime_tool_manifest", return_value=[]):
            owner.service.prompt_application.prompt(owner.source["id"], {
                "message": "暮色报告发布标签", "clientMessageId": "copy-first-client"})
        original = owner.active()
        fact = owner.mutation("remember", "暮色报告发布标签是蓝色。")
        with patch.object(owner.service.runtime, "prompt", return_value={
                "accepted": True, "turnId": "copy-next-turn", "piEntryId": "copy-next-entry"}), \
             patch.object(owner.service.prompt_delivery_application, "runtime_tool_manifest", return_value=[]):
            response = owner.service.prompt_application.prompt(owner.source["id"], {
                "message": "暮色报告发布标签", "clientMessageId": "copy-next-client"})
        trace = owner.service.context_trace(owner.source["id"], response["contextTraceId"])
        node = next(item for item in trace["nodes"] if item["stage"] == "memory_recall")
        self.assertEqual(node["metadata"]["recallTrigger"], "turn_start")
        self.assertEqual(node["metadata"]["recallStatus"], "included")
        self.assertEqual(node["metadata"]["hitCount"], 1)
        self.assertEqual(node["metadata"]["memoryAtomIds"], fact["memoryId"])
        self.assertEqual(node["label"], "本轮记忆召回")
        self.assertEqual(node["summary"], "已加入本轮查询召回的记忆包")
        self.assertEqual(node["disposition"], "included")
        self.assertNotEqual(owner.active()["itemId"], original["itemId"])
        owner.host_mock.assert_not_called()

    def test_turn_start_empty_and_unavailable_copy_do_not_claim_first_question(self):
        empty = self.memory_node([{"payload": {"trigger": "turn_start", "items": []}}])
        self.assertEqual(empty["summary"], "本轮记忆召回完成，未命中可用来源")
        unavailable = self.memory_node([])
        self.assertEqual(unavailable["summary"], "本轮尚无可投递的记忆包")
        self.assertEqual(unavailable["metadata"]["recallStatus"], "unavailable")


class CatalogOnlyMaintenanceTests(unittest.TestCase):
    def test_catalog_only_does_not_process_new_sources_or_timeline(self):
        with tempfile.TemporaryDirectory(prefix="paw-catalog-only-") as tmp:
            core = LocalSqliteCoreClient(
                Path(tmp) / "memory.sqlite", embedding_provider=HashingEmbeddingProvider(dimensions=96)
            )
            core.initialize()
            result = {"ok": True, "curationRunId": "catalog:run", "modelCalled": True}
            service = SimpleNamespace(
                core=core, config=SimpleNamespace(project="project:test"),
                _execute_gateway_memory_catalog_consolidation=Mock(return_value=result),
                _execute_gateway_memory_dreaming=Mock(side_effect=AssertionError("No timeline work")),
            )
            settings = MemoryMaintenanceSettings()
            with patch("rag_ime.debug_server.MemoryMaintenanceSettings.load", return_value=settings), \
                 patch("rag_ime.debug_server.build_governed_memory_model_executor", side_effect=AssertionError("No incremental curator")), \
                 patch("rag_ime.debug_server.run_due_lexicon_organization", side_effect=AssertionError("No lexicon work")):
                actual = DebugImeService._execute_gateway_memory_maintenance(
                    service, {"catalogOnly": True, "manual": True}
                )
            self.assertTrue(actual["ok"])
            self.assertEqual(actual["catalogConsolidation"], result)
            self.assertEqual(actual["runId"], "catalog:run")
            service._execute_gateway_memory_catalog_consolidation.assert_called_once_with(
                project="project:test", manual=True, managed=settings
            )


if __name__ == "__main__":
    unittest.main()
