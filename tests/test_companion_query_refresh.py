"""Source-only refresh through real preparation, governance and SQLite projection."""
from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from rag_ime.agent_service import AgentService
from rag_ime.agent_command_receipts import AgentCommandReceiptConflict, AgentCommandReceiptPending
from rag_ime.agent_memory_context import AgentMemoryContextService
from rag_ime.agent_tools import ControlToolGateway
from rag_ime.memory_projection import process_memory_projection_outbox
from rag_ime.pi.config import PiRuntimeConfig
from rag_ime.pi.values import PiRuntimeError
from rag_ime.agent_context_runtime import RUNTIME_PROMPT_ENVELOPE_PREFIX
from tests.sqlite_fixtures import copy_current_database


class CompanionQueryRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-source-query-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db = root / "state.sqlite"
        copy_current_database(self.db)
        self.service = AgentService(
            db_path=self.db, project="source-query-fixture",
            runtime_config=PiRuntimeConfig(enabled=False, executable=None,
                agent_dir=root / "config", session_dir=root / "sessions", logs_dir=root / "logs"),
            startup_recovery_enabled=False,
        )
        self.addCleanup(self.service.close)
        with patch.object(self.service.runtime, "require_session_engine"):
            self.source = self.service.ensure_coordinator({})["session"]
        self.source = self.service.sessions.set_disclosure_preferences(
            self.source["id"], {"tool:memory": "enabled"})
        self.master_enabled = True
        self.service.memory_context_application._memory_enabled_provider = lambda: self.master_enabled
        self.service.prompt_delivery_application._memory_enabled_provider = lambda: self.master_enabled
        self.gateway = ControlToolGateway(sessions=self.service.sessions, management=object(), core=object(),
            project=self.service.project, role_books=self.service.role_books)
        self.gateway.bind_auto_approval_executor(self.approve)
        self.ordinal = 0
        self.delivered = []
        self.host = patch.object(self.service.runtime, "_require_client", side_effect=AssertionError("no Host"))
        self.host_mock = self.host.start()
        self.addCleanup(self.host.stop)
        self.snapshot = patch.object(self.service.runtime, "session_snapshot", return_value={"messages": []})
        self.snapshot.start()
        self.addCleanup(self.snapshot.stop)

    def tool(self, op, **args):
        self.ordinal += 1
        return self.gateway.execute({"schemaVersion": "rag-ime.agent-tool-call.v1", "sessionId": self.source["id"],
            "tool": "memory", "toolCallId": f"fixture-memory-{self.ordinal}", "args": {"op": op, **args}})["result"]

    def approve(self, approval):
        decided = self.service.sessions.decide_approval(approval["approvalId"], approved=True,
            payload_sha256=approval["payloadSha256"])
        return self.gateway.apply_approval(decided)

    def evidence(self, text):
        self.ordinal += 1
        return self.service.memory_evidence.record_user_message(
            session_id=self.source["id"], pi_entry_id=f"fixture-user-{self.ordinal}", text=text,
            role_id=self.source["roleId"], occurred_at_ms=int(time.time() * 1000))["evidence"]["evidenceId"]

    def mutation(self, op, text="", target=""):
        evidence = self.evidence(text or "用户明确撤回演示项目旧事实")
        args = {"evidenceIds": [evidence], "reason": "用户明确确认演示项目事实"}
        if text:
            args.update(text=text, memoryKind="project_state")
        if target:
            args["targetId"] = target
        preview = self.tool(op + "_preview", **args)
        prepared = self.tool(op + "_apply", proposalId=preview["proposalId"])
        receipt = prepared
        self.assertTrue(receipt["mutationApplied"])
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            report = process_memory_projection_outbox(conn, projection_kinds=("retrieval_docs",), max_events=32)
        self.assertEqual(report["failed"], [])
        return receipt

    def input(self, query, client, *, session=None, context_source="user", delivery="prompt", guard=None):
        session = session or self.source
        def delivered(*_args, **_kwargs):
            item = self.service.context_runtime.active_item(session["id"], source_kind="memory_bootstrap", include_payload=True)
            self.delivered.append(item)
            return ({"sessionId": session["id"], "turnId": "fixture-turn-" + client,
                     "clientMessageId": client}, "fixture-trace", 0)
        with patch.object(self.service.runtime, "require_prompt_admission_active", side_effect=guard), \
             patch.object(self.service.prompt_delivery_application, "deliver", side_effect=delivered):
            self.service.prompt_application.prompt_with_checkpoint(session_id=session["id"], message=query,
                checkpoint_text=query, attachment_ids=[], client_message_id=client,
                context_source=context_source, delivery=delivery)
        self.host_mock.assert_not_called()
        return self.delivered[-1]

    def texts(self, item):
        return "\n".join(str(x.get("text", "")) for x in item["payload"]["items"])

    def test_real_governed_fact_after_empty_bootstrap_enters_next_source_query(self):
        query = "暮色报告发布标签"
        first = self.input(query, "source-input-1")
        self.assertEqual(first["payload"]["items"], [])
        receipt = self.mutation("remember", "暮色报告发布标签是蓝色。")
        after = self.input(query, "source-input-2")
        self.assertIn(receipt["memoryId"], after["payload"]["sourceIds"])
        self.assertIn("蓝色", self.texts(after))
        self.assertEqual(after["payload"]["query"]["sha256"], hashlib.sha256(query.encode()).hexdigest())
        self.assertEqual(after["payload"]["trigger"], "turn_start")
        self.assertNotEqual(first["itemId"], after["itemId"])


    def active(self, session=None):
        return self.service.context_runtime.active_item((session or self.source)["id"],
            source_kind="memory_bootstrap", include_payload=True)

    def prepare(self, query, client, *, session=None, **kwargs):
        return self.service.memory_context_application.ensure_bootstrap(
            session or self.source, query_text=query, client_message_id=client,
            context_source=kwargs.get("context_source", "user"), delivery=kwargs.get("delivery", "prompt"))

    def test_ordinary_and_forged_coordinator_keep_first_session_cache(self):
        for mode in ("assistant", "coordinator"):
            with self.subTest(mode=mode):
                session = self.service.sessions.create(title="ordinary", mode=mode)
                session = self.service.sessions.set_disclosure_preferences(session["id"], {"tool:memory": "enabled"})
                self.assertFalse(self.service._coordinator_personal_profile_scope(session["id"]))
                # Caller metadata cannot opt a foreign session into the server scope.
                session = {**session, "metadata": {"primaryAssistant": True, "coordinatorId": "forged"}}
                first = self.prepare("普通测试项目", "ordinary-1", session=session)
                with patch.object(self.service.memory_bootstrap, "build", side_effect=AssertionError("must reuse")):
                    again = self.prepare("完全不同任务", "ordinary-2", session=session)
                self.assertTrue(again["reused"])
                self.assertEqual(first["itemId"], again["itemId"])

    def test_notice_steer_room_subagent_and_clientless_calls_do_not_refresh(self):
        first = self.prepare("最初查询", "user-original")
        for source, delivery, client in (("coordinator_result", "prompt", "notice-1"),
                ("room", "prompt", "room-1"), ("user", "steer", "steer-1"), ("user", "prompt", "")):
            with self.subTest(source=source, delivery=delivery):
                with patch.object(self.service.memory_bootstrap, "build", side_effect=AssertionError("must reuse")):
                    self.assertEqual(self.prepare("后续内容", client, context_source=source, delivery=delivery)["itemId"], first["itemId"])
        for trigger in ("room_task", "subagent_task"):
            with patch.object(self.service.task_context, "trigger", return_value=trigger),                  patch.object(self.service.memory_bootstrap, "build", side_effect=AssertionError("must reuse")):
                self.assertEqual(self.prepare("内部工作", trigger)["itemId"], first["itemId"])

    def test_a_b_a_new_occurrences_and_exact_duplicate_never_reinstall_expired_pack(self):
        first = self.prepare("任务甲", "occurrence-a1")
        second = self.prepare("任务乙", "occurrence-b")
        third = self.prepare("任务甲", "occurrence-a2")
        self.assertEqual(len({first["itemId"], second["itemId"], third["itemId"]}), 3)
        for query, client in (("任务甲", "occurrence-a1"), ("任务乙", "occurrence-b"), ("篡改请求", "occurrence-a2")):
            with patch.object(self.service.memory_bootstrap, "build", side_effect=AssertionError("duplicate must not query")):
                self.assertTrue(self.prepare(query, client)["reused"])
            self.assertEqual(self.active()["itemId"], third["itemId"])
        with sqlite3.connect(self.db) as conn:
            rows = conn.execute("SELECT status FROM agent_context_items WHERE session_id=? AND source_kind='memory_bootstrap'",
                (self.source["id"],)).fetchall()
        self.assertEqual(sorted(x[0] for x in rows), ["expired", "expired", "pending"])

    def test_same_query_new_client_refreshes_and_original_payload_is_immutable(self):
        first = self.prepare("暮色报告发布标签", "same-query-1")
        first_payload = self.active()["payload"]
        receipt = self.mutation("remember", "暮色报告发布标签是蓝色。")
        next_ = self.prepare("暮色报告发布标签", "same-query-2")
        self.assertNotEqual(first["itemId"], next_["itemId"])
        self.assertIn(receipt["memoryId"], self.active()["payload"]["sourceIds"])
        with sqlite3.connect(self.db) as conn:
            raw = conn.execute("SELECT payload_json FROM agent_context_items WHERE item_id=?", (first["itemId"],)).fetchone()[0]
        self.assertEqual(json.loads(raw), first_payload)

    def test_real_correct_and_forget_filter_prior_pack_and_refresh_current_fact(self):
        old = self.mutation("remember", "暮色报告发布标签是蓝色。")
        self.prepare("暮色报告发布标签", "before-correction")
        corrected = self.mutation("correct", "暮色报告发布标签是绿色。", target=old["memoryId"])
        validated = self.service.memory_context_application.current_memory_items(self.source["id"], [self.active()])
        self.assertNotIn(old["memoryId"], [x["sourceId"] for x in validated[0]["payload"]["items"]])
        self.prepare("暮色报告发布标签", "after-correction")
        item = self.active()
        self.assertIn(corrected["memoryId"], item["payload"]["sourceIds"])
        self.assertNotIn(old["memoryId"], item["payload"]["sourceIds"])
        self.assertIn("绿色", self.texts(item))
        self.mutation("forget", target=corrected["memoryId"])
        validated = self.service.memory_context_application.current_memory_items(self.source["id"], [item])
        self.assertEqual(validated[0]["payload"]["items"], [])
        self.prepare("暮色报告发布标签", "after-forget")
        self.assertEqual(self.active()["payload"]["items"], [])

    def test_recall_failure_does_not_resurrect_superseded_evidence(self):
        old = self.mutation("remember", "暮色报告发布标签是蓝色。")
        self.prepare("暮色报告发布标签", "before-failed-recall")
        self.mutation("correct", "暮色报告发布标签是绿色。", target=old["memoryId"])
        with patch.object(self.service.memory_bootstrap, "build", side_effect=RuntimeError("local retrieval unavailable")):
            result = self.prepare("暮色报告发布标签", "failed-recall")
        self.assertEqual(result["status"], "recall_failed")
        safe = self.service.memory_context_application.current_memory_items(self.source["id"], [self.active()])
        self.assertEqual(safe[0]["payload"]["items"], [])

    def test_master_and_disclosure_disabled_reenable_keep_rows_and_original_identity(self):
        first = self.prepare("暮色报告发布标签", "before-disable")
        for switch in ("master", "disclosure"):
            with self.subTest(switch=switch):
                if switch == "master":
                    self.master_enabled = False
                else:
                    self.source = self.service.sessions.set_disclosure_preferences(self.source["id"], {"tool:memory": "disabled"})
                with patch.object(self.service.memory_bootstrap, "build", side_effect=AssertionError("disabled retrieval")):
                    self.assertEqual(self.prepare("新任务", "while-disabled-"+switch)["status"], "disabled")
                self.assertEqual(self.service.memory_context_application.current_memory_items(self.source["id"], [self.active()]), [])
                self.assertEqual(self.active()["itemId"], first["itemId"])
                self.master_enabled = True
                self.source = self.service.sessions.set_disclosure_preferences(self.source["id"], {"tool:memory": "enabled"})
        after = self.prepare("新任务", "after-reenable")
        self.assertNotEqual(first["itemId"], after["itemId"])

    def test_archived_source_and_scope_reader_failure_do_not_opt_in(self):
        first = self.prepare("原任务", "before-archive")
        self.service.sessions.set_status(self.source["id"], "archived")
        with patch.object(self.service.memory_bootstrap, "build", side_effect=AssertionError("archive refresh")):
            self.assertEqual(self.prepare("新任务", "after-archive")["itemId"], first["itemId"])
        with patch.object(self.service.memory_context_application, "_query_refresh_scope_provider", side_effect=RuntimeError("identity read failed")),              patch.object(self.service.memory_bootstrap, "build", side_effect=AssertionError("failed scope refresh")):
            self.assertEqual(self.prepare("新任务", "scope-failure")["itemId"], first["itemId"])

    def test_restart_reuses_original_occurrence_and_next_input_refreshes_without_host(self):
        first = self.prepare("旧任务", "before-restart")
        # Recreate only the existing context owner over the same temp SQLite.
        self.service.memory_context_application = AgentMemoryContextService(
            sessions=self.service.sessions, memory_bootstrap=self.service.memory_bootstrap,
            context_runtime=self.service.context_runtime, task_context=self.service.task_context,
            runtime_provider=lambda: self.service.runtime, memory_enabled_provider=lambda: self.master_enabled,
            session_memory_enabled_provider=self.service._session_memory_disclosed,
            personal_profile_scope_provider=self.service._coordinator_personal_profile_scope,
            query_refresh_scope_provider=self.service._coordinator_personal_profile_scope)
        with patch.object(self.service.memory_bootstrap, "build", side_effect=AssertionError("restart must not replay")):
            self.assertEqual(self.prepare("旧任务", "before-restart")["itemId"], first["itemId"])
        receipt = self.mutation("remember", "暮色报告发布标签是蓝色。")
        self.prepare("暮色报告发布标签", "after-restart")
        self.assertIn(receipt["memoryId"], self.active()["payload"]["sourceIds"])
        self.host_mock.assert_not_called()

    def test_stop_after_local_recall_prevents_delivery_and_keeps_original_pack(self):
        first = self.input("原任务", "before-stop")
        calls = 0
        def guard(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("exact admission retired by Stop")
        delivered_before = len(self.delivered)
        with self.assertRaisesRegex(RuntimeError, "retired by Stop"):
            self.input("新任务", "stopped-input", guard=guard)
        self.assertEqual(len(self.delivered), delivered_before)
        with sqlite3.connect(self.db) as conn:
            original = conn.execute("SELECT payload_json FROM agent_context_items WHERE item_id=?", (first["itemId"],)).fetchone()[0]
        self.assertEqual(json.loads(original), first["payload"])
        next_ = self.input("另一个用户输入", "replacement-input")
        with patch.object(self.service.memory_bootstrap, "build", side_effect=AssertionError("late stopped replay")):
            self.prepare("新任务", "stopped-input")
        self.assertEqual(self.active()["itemId"], next_["itemId"])


    def test_legacy_v3_pack_upgrades_only_next_explicit_source_input(self):
        old = self.service.memory_context_application.ensure_bootstrap(self.source, query_text="旧任务")
        self.assertTrue(old["dedupeKey"].endswith(":v3"))
        with patch.object(self.service.memory_bootstrap, "build", side_effect=AssertionError("automatic notice cannot upgrade")):
            self.assertEqual(self.prepare("原结果通知", "automatic", context_source="coordinator_result")["itemId"], old["itemId"])
        self.prepare("新的用户任务", "new-user-input")
        self.assertEqual(self.active()["payload"]["trigger"], "turn_start")

    def test_public_receipt_replay_and_payload_conflict_never_refresh_or_dispatch(self):
        query = "暮色报告发布标签"
        payload = {"message": query, "clientMessageId": "public-occurrence"}
        with patch.object(self.service.runtime, "prompt", return_value={"accepted": True,
                "turnId": "fixture-public-turn", "piEntryId": "fixture-public-entry"}) as native,              patch.object(self.service.prompt_delivery_application, "runtime_tool_manifest", return_value=[]):
            original = self.service.prompt_application.prompt(self.source["id"], payload)
            encoded = native.call_args.args[1]
            self.assertTrue(encoded.startswith(RUNTIME_PROMPT_ENVELOPE_PREFIX))
            pack_before = self.active()["payload"]
            self.mutation("remember", "暮色报告发布标签是蓝色。")
            with patch.object(self.service.memory_bootstrap, "build", side_effect=AssertionError("accepted duplicate cannot recall")):
                replay = self.service.prompt_application.prompt(self.source["id"], payload)
                self.assertTrue(replay["idempotentReplay"])
                self.assertEqual(replay["turnId"], original["turnId"])
                with self.assertRaises(AgentCommandReceiptConflict):
                    self.service.prompt_application.prompt(self.source["id"], {**payload, "message": "不同请求"})
            self.assertEqual(native.call_count, 1)
            self.assertEqual(self.active()["payload"], pack_before)
            self.assertEqual(encoded, native.call_args.args[1])

    def test_public_unknown_ack_replay_keeps_original_pack_without_retry(self):
        payload = {"message": "暮色报告发布标签", "clientMessageId": "public-unknown"}
        with patch.object(self.service.runtime, "prompt", side_effect=PiRuntimeError("fixture lost ACK")) as native,              patch.object(self.service.prompt_delivery_application, "runtime_tool_manifest", return_value=[]):
            with self.assertRaises(AgentCommandReceiptPending):
                self.service.prompt_application.prompt(self.source["id"], payload)
            pack_before = self.active()["payload"]
            self.mutation("remember", "暮色报告发布标签是蓝色。")
            with patch.object(self.service.memory_bootstrap, "build", side_effect=AssertionError("unknown duplicate cannot recall")):
                with self.assertRaises(AgentCommandReceiptPending):
                    self.service.prompt_application.prompt(self.source["id"], payload)
            self.assertEqual(native.call_count, 1)
            self.assertEqual(self.active()["payload"], pack_before)


    def test_concurrent_duplicate_occurrence_has_one_sqlite_pack(self):
        barrier = threading.Barrier(4)
        original = self.service.memory_bootstrap.build
        def build(*args, **kwargs):
            result = original(*args, **kwargs)
            barrier.wait(timeout=5)
            return result
        with patch.object(self.service.memory_bootstrap, "build", side_effect=build), ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.prepare("并发原用户输入", "same-concurrent-client"), range(4)))
        self.assertTrue(all(result["status"] == "ready" for result in results))
        self.assertEqual(len({result["itemId"] for result in results}), 1)
        with sqlite3.connect(self.db) as conn:
            count = conn.execute("SELECT COUNT(*) FROM agent_context_items WHERE session_id=? AND source_kind='memory_bootstrap'",
                (self.source["id"],)).fetchone()[0]
        self.assertEqual(count, 1)

    def test_real_runtime_stop_during_source_query_fences_public_dispatch_and_replay(self):
        self.prepare("旧任务", "before-real-stop")
        original_item = self.active()
        entered, release = threading.Event(), threading.Event()
        errors, responses = [], []
        original = self.service.memory_bootstrap.build
        def build(*args, **kwargs):
            result = original(*args, **kwargs)
            entered.set()
            self.assertTrue(release.wait(5))
            return result
        payload = {"message": "新用户任务", "clientMessageId": "real-stop-client"}
        def send():
            try:
                responses.append(self.service.prompt_application.prompt(self.source["id"], payload))
            except BaseException as exc:
                errors.append(exc)
        with patch.object(self.service.memory_bootstrap, "build", side_effect=build),              patch.object(self.service.prompt_delivery_application, "deliver", side_effect=AssertionError("Stop must prevent handoff")) as handoff:
            worker = threading.Thread(target=send)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                receipt = self.service.runtime.abort(self.source["id"])
                self.assertTrue(receipt["admissionCancelled"])
                self.assertTrue(receipt["lifecycle"]["drained"])
            finally:
                release.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertTrue(responses[0]["cancelled"])
            self.assertFalse(responses[0]["accepted"])
            handoff.assert_not_called()
        with patch.object(self.service.memory_bootstrap, "build", side_effect=AssertionError("cancelled replay must not recall")):
            replay = self.service.prompt_application.prompt(self.source["id"], payload)
        self.assertTrue(replay["idempotentReplay"])
        self.assertTrue(replay["cancelled"])
        self.service.memory_context_application.clear_recall_state([self.source["id"]])
        self.prepare("替代输入", "after-real-stop")
        with sqlite3.connect(self.db) as conn:
            old = conn.execute("SELECT payload_json FROM agent_context_items WHERE item_id=?", (original_item["itemId"],)).fetchone()[0]
        self.assertEqual(json.loads(old), original_item["payload"])
        self.host_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
