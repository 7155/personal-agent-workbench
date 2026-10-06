from __future__ import annotations

import sqlite3
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from rag_ime.agent_service import AgentService
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.agent_tools import ControlToolGateway
from rag_ime.db import sqlite_connection
from rag_ime.pi.config import PiRuntimeConfig
from tests.sqlite_fixtures import copy_current_database


class PrimaryAssistantSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="paw-primary-session-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db = self.root / "agent.sqlite"
        copy_current_database(self.db)
        self.service = AgentService(db_path=self.db, runtime_config=PiRuntimeConfig(
            enabled=False, executable=None, agent_dir=self.root / "config",
            session_dir=self.root / "sessions", logs_dir=self.root / "logs"))
        self.addCleanup(self.service.close)

    def request(self, **overrides: object) -> dict[str, object]:
        source = self.service.ensure_primary_assistant({})["session"]
        return {"clientRequestId": "request-original", "sourceSessionId": source["id"],
            "objective": "Implement the agreed fix", "acceptanceCriteria": ["Focused tests pass"],
            "workspaceRoots": [str(self.root)], "workspaceScopeConfirmation": "APPROVE_WORKSPACE_SCOPE",
            **overrides}

    def test_concurrent_ensure_and_restart_preserve_exact_identity(self) -> None:
        with ThreadPoolExecutor(max_workers=6) as pool:
            replies = list(pool.map(lambda _: self.service.ensure_primary_assistant({}), range(12)))
        self.assertEqual(sum(reply["created"] for reply in replies), 1)
        self.assertEqual(len({reply["assistantId"] for reply in replies}), 1)
        session_ids = {reply["session"]["id"] for reply in replies}
        self.assertEqual(len(session_ids), 1)
        original = replies[0]["session"]
        restarted = AgentSessionStore(self.db)
        self.assertEqual(restarted.get(original["id"])["metadata"], original["metadata"])
        self.assertEqual(original["runtimeEngine"], "classic")
        self.assertEqual(original["executionMode"], "read_only")
        self.assertFalse(original["workspaceScopeGranted"])
        projected = restarted.list_page(projection_only=True)["items"]
        self.assertEqual(projected[0]["metadata"], original["metadata"])

    def test_projects_are_isolated_and_have_one_shared_assistant_identity(self) -> None:
        plain = self.service.ensure_primary_assistant({})
        project = self.service.ensure_primary_assistant({"workspaceRoots": [str(self.root)]})
        again = self.service.ensure_primary_assistant({"workspaceRoots": [str(self.root / ".")]})
        self.assertEqual(project["session"]["id"], again["session"]["id"])
        self.assertNotEqual(plain["session"]["id"], project["session"]["id"])
        self.assertEqual(plain["assistantId"], project["assistantId"])
        self.assertEqual(project["session"]["workspaceRoots"], [str(self.root.resolve())])
        self.assertEqual(project["session"]["executionMode"], "read_only")

    def test_concurrent_task_retries_bind_original_session_and_goal_once(self) -> None:
        request = self.request()
        with ThreadPoolExecutor(max_workers=6) as pool:
            replies = list(pool.map(lambda _: self.service.create_primary_task(request), range(12)))
        self.assertEqual(sum(reply["created"] for reply in replies), 1)
        self.assertEqual(len({reply["session"]["id"] for reply in replies}), 1)
        result = replies[0]
        task = result["session"]
        self.assertEqual(task["metadata"]["clientRequestId"], request["clientRequestId"])
        self.assertEqual(task["executionMode"], "workspace_managed")
        self.assertTrue(task["workspaceScopeGranted"])
        goal = self.service.sessions.agent_goal(task["id"])
        self.assertEqual(goal["objective"], request["objective"])
        self.assertEqual(goal["revision"], 1)
        self.assertEqual(goal["successCriteria"], "Focused tests pass")
        self.assertEqual(task["goal"], {key: goal[key] for key in
            ("goalId", "revision", "status", "objective", "successCriteria")})
        ensured = self.service.ensure_primary_assistant({})
        self.assertEqual(ensured["tasks"][0]["id"], task["id"])
        self.assertEqual(ensured["session"]["id"], request["sourceSessionId"])
        self.assertEqual(ensured["session"]["executionMode"], "read_only")
        self.assertIsNone(self.service.sessions.runtime_binding(task["id"]))

    def test_archived_discussion_cannot_create_a_task_after_primary_rotation(self) -> None:
        request = self.request()
        self.service.sessions.archive(str(request["sourceSessionId"]))
        replacement = self.service.ensure_primary_assistant({})
        self.assertNotEqual(replacement["session"]["id"], request["sourceSessionId"])
        with patch.object(self.service, "messages", side_effect=AssertionError("retired context must not be read")):
            with self.assertRaisesRegex(ValueError, "archived"):
                self.service.create_primary_task(request)
        self.assertEqual(self.service.sessions.primary_tasks(replacement["assistantId"],
                         source_session_id=str(request["sourceSessionId"])), [])
        self.assertEqual(self.service.ensure_primary_assistant({})["tasks"], [])

    def test_task_transaction_rejects_same_revision_source_archived_after_snapshot(self) -> None:
        request = self.request()
        prepare = self.service._primary_task_brief

        def archive_after_snapshot(authorization):
            brief = prepare(authorization)
            self.service.sessions.archive(str(request["sourceSessionId"]),
                                          updated_at_ms=int(brief["sourceSessionRevision"]))
            return brief

        with patch.object(self.service, "_primary_task_brief", side_effect=archive_after_snapshot):
            with self.assertRaisesRegex(ValueError, "archived"):
                self.service.create_primary_task(request)
        with sqlite_connection(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_primary_session_links WHERE kind='task'").fetchone()[0], 0)

    def test_exact_task_replay_survives_source_archive_and_rotation(self) -> None:
        request = self.request()
        first = self.service.create_primary_task(request)
        self.service.sessions.archive(str(request["sourceSessionId"]))
        self.service.ensure_primary_assistant({})
        with patch.object(self.service, "_primary_task_brief", side_effect=AssertionError("replay must not snapshot")):
            replay = self.service.create_primary_task(request)
        self.assertFalse(replay["created"])
        self.assertEqual(replay["session"]["id"], first["session"]["id"])

    def test_retry_with_changed_request_never_creates_another_task(self) -> None:
        request = self.request()
        original = self.service.create_primary_task(request)
        for changed in ({"objective": "Another task"}, {"acceptanceCriteria": ["Different"]},
                        {"sourceSessionId": self.service.ensure_primary_assistant(
                            {"workspaceRoots": [str(self.root)]})["session"]["id"]}):
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, "already bound"):
                self.service.create_primary_task({**request, **changed})
        self.assertEqual([item["id"] for item in self.service.sessions.primary_tasks(
            original["assistantId"], source_session_id=str(request["sourceSessionId"]))],
                         [original["session"]["id"]])

    def test_project_task_list_is_bound_to_the_ensured_discussion(self) -> None:
        # PR135 / discussion_r4180769529: one assistant does not imply one task scope.
        plain = self.service.ensure_primary_assistant({})
        project = self.service.ensure_primary_assistant({"workspaceRoots": [str(self.root)]})
        first = self.service.create_primary_task(self.request(clientRequestId="plain-task"))["session"]
        second = self.service.create_primary_task(self.request(
            clientRequestId="project-task", sourceSessionId=project["session"]["id"]))["session"]
        self.assertEqual(plain["assistantId"], project["assistantId"])
        self.assertEqual([task["id"] for task in self.service.ensure_primary_assistant({})["tasks"]], [first["id"]])
        self.assertEqual([task["id"] for task in self.service.ensure_primary_assistant(
            {"workspaceRoots": [str(self.root)]})["tasks"]], [second["id"]])

    def test_archived_tasks_do_not_displace_current_results_at_the_context_limit(self) -> None:
        request = self.request(clientRequestId='visible-context-task')
        visible = self.service.create_primary_task(request)['session']
        newer = int(visible['updatedAtMs']) + 1000
        for index in range(8):
            task = self.service.create_primary_task({**request, 'clientRequestId': f'archived-context-{index}'})['session']
            self.service.sessions.archive(task['id'], updated_at_ms=newer + index)
        results = self.service.sessions.primary_task_results(str(request['sourceSessionId']))
        self.assertEqual([item['sessionId'] for item in results], [visible['id']])
        self.assertEqual(self.service.sessions.get(task['id'])['status'], 'archived')

    def test_archived_tasks_do_not_displace_current_tasks_at_the_directory_limit(self) -> None:
        request = self.request(clientRequestId="visible-task")
        visible = self.service.create_primary_task(request)["session"]
        newer = int(visible["updatedAtMs"]) + 1000
        for index in range(100):
            task = self.service.create_primary_task({**request, "clientRequestId": f"archived-task-{index}"})["session"]
            self.service.sessions.archive(task["id"], updated_at_ms=newer + index)
        tasks = self.service.ensure_primary_assistant({})["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["id"], visible["id"])
        self.assertEqual(self.service.sessions.get(task["id"])["status"], "archived")

    def test_task_inherits_source_model_and_thinking_instead_of_global_defaults(self) -> None:
        request = self.request()
        source_id = str(request["sourceSessionId"])
        self.service.sessions.set_model_profile(source_id, "source-provider/source-model")
        self.service.sessions.set_thinking_level(source_id, "xhigh")
        with ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(lambda _: self.service.create_primary_task(request), range(8)))
        self.assertEqual(sum(reply["created"] for reply in replies), 1)
        self.assertEqual(len({reply["session"]["id"] for reply in replies}), 1)
        for reply in replies:
            self.assertEqual(reply["session"]["modelProfile"], "source-provider/source-model")
            self.assertEqual(reply["session"]["thinkingLevel"], "xhigh")
        ordinary = self.service.create_session({"title": "Unrelated new session"})["session"]
        self.assertNotEqual(ordinary["modelProfile"], "source-provider/source-model")
        self.assertEqual(ordinary["thinkingLevel"], "max")

    def test_task_replay_does_not_reconfigure_an_existing_task(self) -> None:
        request = self.request()
        source_id = str(request["sourceSessionId"])
        self.service.sessions.set_thinking_level(source_id, "xhigh")
        first = self.service.create_primary_task(request)
        self.service.sessions.set_model_profile(source_id, "changed-provider/changed-model")
        self.service.sessions.set_thinking_level(source_id, "low")
        replay = self.service.create_primary_task(request)
        self.assertFalse(replay["created"])
        self.assertEqual(replay["session"]["id"], first["session"]["id"])
        self.assertEqual(replay["session"]["modelProfile"], first["session"]["modelProfile"])
        self.assertEqual(replay["session"]["thinkingLevel"], "xhigh")

    def test_same_revision_source_config_change_is_rejected_before_task_creation(self) -> None:
        request = self.request()
        source_id = str(request["sourceSessionId"])
        prepare = self.service._primary_task_brief
        def change_after_snapshot(authorization):
            result = prepare(authorization)
            with sqlite_connection(self.db) as conn:
                conn.execute("UPDATE agent_sessions SET thinking_level='low' WHERE id=?", (source_id,))
            return result
        with patch.object(self.service, "_primary_task_brief", side_effect=change_after_snapshot):
            with self.assertRaisesRegex(ValueError, "source model configuration changed"):
                self.service.create_primary_task(request)
        self.assertEqual(self.service.ensure_primary_assistant({})["tasks"], [])

    def test_snapshot_rereads_source_config_even_when_timestamp_did_not_change(self) -> None:
        request = self.request()
        source_id = str(request["sourceSessionId"])
        reads = []
        def snapshot(_session_id):
            reads.append(_session_id)
            if len(reads) == 1:
                with sqlite_connection(self.db) as conn:
                    conn.execute("UPDATE agent_sessions SET thinking_level='xhigh' WHERE id=?", (source_id,))
            return {"items": [], "lastSequence": 0}
        with patch.object(self.service, "messages", side_effect=snapshot):
            result = self.service.create_primary_task(request)
        self.assertEqual(len(reads), 2)
        self.assertEqual(result["session"]["thinkingLevel"], "xhigh")

    def test_explicit_model_route_and_role_keep_their_creation_policy(self) -> None:
        application = self.service.session_application
        inherited = {"modelProfile": "source-provider/source-model", "thinkingLevel": "xhigh"}
        for explicit in ({"modelProfile": "override-provider/override-model"},
                         {"_modelRoute": "traceDiagnostic"}, {"roleId": "companion-future-v1"}):
            with self.subTest(explicit=explicit):
                expected = application._create_session_record({"title": "Explicit baseline", **explicit})
                actual = application._create_session_record(
                    {"title": "Explicit with source", **explicit}, inherited_model_selection=inherited)
                self.assertEqual(actual["modelProfile"], expected["modelProfile"])
                self.assertEqual(actual["thinkingLevel"], expected["thinkingLevel"])

    def test_creation_requires_explicit_scope_and_primary_source(self) -> None:
        request = self.request()
        ordinary = self.service.create_session({"title": "ordinary"})["session"]
        for changed in ({"workspaceScopeConfirmation": ""}, {"workspaceRoots": []},
                        {"workspaceRoots": ["/"]}, {"workspaceRoots": [str(self.root / "missing")]},
                        {"sourceSessionId": ordinary["id"]}, {"acceptanceCriteria": "do it"},
                        {"objective": ""}, {"metadata": {"primaryTask": True}}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.service.create_primary_task({**request, **changed})
        self.assertEqual(self.service.ensure_primary_assistant({})["tasks"], [])

    def test_source_message_must_belong_to_source_discussion(self) -> None:
        request = self.request(sourceMessageId="message-original")
        with patch.object(self.service, "messages", return_value={"items": [{"id": "other"}]}):
            with self.assertRaisesRegex(ValueError, "does not belong"):
                self.service.create_primary_task(request)
        with patch.object(self.service, "messages", return_value={"items": [self.message("message-original", "assistant", "已确认方案")]}):
            result = self.service.create_primary_task(request)
        self.assertEqual(result["session"]["metadata"]["sourceMessageId"], "message-original")

    def test_primary_permissions_cannot_be_upgraded_or_rebound(self) -> None:
        request = self.request()
        task = self.service.create_primary_task(request)["session"]
        for session_id in (request["sourceSessionId"], task["id"]):
            for mode in ("per_action", "full_trust"):
                with self.subTest(session_id=session_id, mode=mode), self.assertRaises(ValueError):
                    self.service.sessions.set_runtime_policy(session_id, mode="coordinator",
                        execution_mode=mode, tool_profile_version="control-center-v1",
                        allowed_tools=None, workspace_roots=[str(self.root)], grant_workspace_scope=True)
            with self.assertRaises(ValueError):
                self.service.sessions.set_mode(session_id, "coordinator", workspace_roots=[str(self.root)])

    def test_failed_goal_creation_rolls_back_session_and_request_identity(self) -> None:
        request = self.request()
        with patch.object(self.service.sessions, "mutate_agent_goal", side_effect=ValueError("test rollback")):
            with self.assertRaisesRegex(ValueError, "rollback"):
                self.service.create_primary_task(request)
        self.assertEqual(len(self.service.sessions.list()), 1)
        self.assertTrue(self.service.create_primary_task(request)["created"])

    def test_archive_rotation_cannot_restore_two_active_discussions(self) -> None:
        original = self.service.ensure_primary_assistant({})
        self.service.sessions.archive(original["session"]["id"])
        replacement = self.service.ensure_primary_assistant({})
        self.assertEqual(original["assistantId"], replacement["assistantId"])
        self.assertNotEqual(original["session"]["id"], replacement["session"]["id"])
        with self.assertRaisesRegex(ValueError, "already has an active"):
            self.service.sessions.archive(original["session"]["id"], archived=False)

    def test_goal_summary_tracks_real_completion_independently_of_discussion(self) -> None:
        request = self.request()
        task = self.service.create_primary_task(request)["session"]
        self.assertEqual(task["status"], "idle")
        self.assertEqual(task["goal"]["status"], "active")
        self.service.sessions.mutate_agent_goal(task["id"], {
            "action": "complete", "expectedRevision": 1, "summary": "Verified the fix",
            "evidence": [{"kind": "test", "reference": "tests/test_example.py", "summary": "Test passed"}]})
        summary = self.service.ensure_primary_assistant({})["tasks"][0]
        self.assertEqual(summary["goal"]["status"], "completed")
        self.assertEqual(summary["goal"]["goalId"], task["goal"]["goalId"])
        self.assertEqual(summary["id"], task["id"])
        self.assertEqual(self.service.sessions.get(request["sourceSessionId"])["status"], "idle")

    def test_results_reach_only_exact_source_context_and_clear_after_deletion(self) -> None:
        request = self.request()
        task = self.service.create_primary_task(request)["session"]
        source_id = request["sourceSessionId"]
        other = self.service.ensure_primary_assistant({"workspaceRoots": [str(self.root)]})["session"]
        self.assertEqual(self.service.sessions.primary_task_results(other["id"]), [])
        active = self.service.sessions.primary_task_results(source_id)[0]
        self.assertEqual(active["sessionStatus"], "idle")
        self.assertEqual(active["goalStatus"], "active")
        self.assertNotIn("completionAudit", active)
        self.service.sessions.mutate_agent_goal(task["id"], {
            "action": "complete", "expectedRevision": 1, "summary": "RESULT_TO_PRIMARY",
            "evidence": [{"kind": "artifact", "reference": "output/report.html", "summary": "Verified"}]})
        result = self.service.sessions.primary_task_results(source_id)[0]
        goal = self.service.sessions.agent_goal(task["id"])
        self.assertEqual(result["completionAudit"]["auditId"], goal["completionAudit"]["auditId"])
        memory = self.service.memory_context_application
        with patch.object(memory, "_memory_enabled", return_value=False), \
             patch.object(self.service.prompt_delivery_application, "_memory_enabled", return_value=False), \
             patch.object(self.service.runtime, "prompt", return_value={"accepted": True, "turnId": "turn:results"}) as prompt:
            self.service.prompt(source_id, {"message": "结果如何", "clientMessageId": "results-request"})
        from rag_ime.agent_context_runtime import RUNTIME_PROMPT_ENVELOPE_PREFIX
        envelope = json.loads(prompt.call_args.args[1][len(RUNTIME_PROMPT_ENVELOPE_PREFIX):])
        self.assertIn("RESULT_TO_PRIMARY", envelope["sessionContext"])
        self.assertIn("output/report.html", envelope["sessionContext"])
        self.assertNotIn("RESULT_TO_PRIMARY", envelope["message"] + envelope["transientContext"])
        with patch.object(memory, "_memory_enabled", return_value=False):
            self.assertIn("RESULT_TO_PRIMARY", self.service._runtime_session_context(self.service.sessions.get(source_id))["sessionContext"])
            self.assertIn("RESULT_TO_PRIMARY", memory.refresh({"sessionId": source_id, "trigger": "compaction"})["result"]["sessionContext"])
            self.service.sessions.delete(task["id"])
            self.assertNotIn("RESULT_TO_PRIMARY", memory.provider_context(source_id))
            self.assertNotIn("RESULT_TO_PRIMARY", memory.refresh({"sessionId": source_id, "trigger": "compaction"})["result"]["sessionContext"])

    def test_result_ownership_does_not_follow_archived_discussion_rotation_or_deleted_source(self) -> None:
        request = self.request()
        self.service.create_primary_task(request)
        self.service.sessions.archive(request["sourceSessionId"])
        rotated = self.service.ensure_primary_assistant({})["session"]
        self.assertNotEqual(rotated["id"], request["sourceSessionId"])
        self.assertEqual(self.service.sessions.primary_task_results(rotated["id"]), [])
        self.service.sessions.delete(request["sourceSessionId"])
        self.assertEqual(self.service.sessions.primary_task_results(request["sourceSessionId"]), [])

    def test_initial_prompt_retry_uses_original_client_identity_without_reexecution(self) -> None:
        request = self.request()
        task = self.service.create_primary_task(request)["session"]
        prompt_payload = {"message": request["objective"], "clientMessageId": request["clientRequestId"]}
        with patch.object(self.service.runtime, "prompt", return_value={
            "accepted": True, "turnId": "turn:original", "piEntryId": "entry:original",
            "response": {"success": True}}) as runtime_prompt:
            first = self.service.prompt(task["id"], prompt_payload)
            retry_task = self.service.create_primary_task(request)["session"]
            replay = self.service.prompt(retry_task["id"], prompt_payload)
        runtime_prompt.assert_called_once()
        self.assertEqual(runtime_prompt.call_args.kwargs["client_message_id"], request["clientRequestId"])
        self.assertEqual(retry_task["id"], task["id"])
        self.assertEqual(first["turnId"], replay["turnId"])
        self.assertTrue(replay["idempotentReplay"])
        self.assertEqual(first["commandReceipt"], replay["commandReceipt"])
        with self.assertRaisesRegex(ValueError, "different command"):
            self.service.prompt(task["id"], {**prompt_payload, "message": "A different objective"})

    def test_ordinary_fork_does_not_duplicate_assistant_identity_or_authorization(self) -> None:
        source = self.service.ensure_primary_assistant({})["session"]
        with patch.object(self.service.runtime, "fork_session", return_value={"selectedText": "proposal"}):
            fork = self.service.fork_session(source["id"], {"entryId": "entry:original"})["session"]
        self.assertNotIn("metadata", fork)
        self.assertNotEqual(fork["id"], source["id"])
        self.assertEqual(fork["executionMode"], "read_only")
        self.assertFalse(fork["workspaceScopeGranted"])

    def test_deleting_task_keeps_request_tombstone_and_cannot_reexecute(self) -> None:
        request = self.request()
        task = self.service.create_primary_task(request)["session"]
        self.service.sessions.delete(task["id"])
        with self.assertRaises(KeyError):
            self.service.create_primary_task(request)
        self.assertEqual(len(self.service.sessions.list()), 1)
        with sqlite_connection(self.db) as conn:
            receipt = json.loads(conn.execute("SELECT authorization_json FROM agent_primary_session_links WHERE session_id = ?", (task["id"],)).fetchone()[0])
        self.assertEqual(set(receipt), {"requestSha256"})

    @staticmethod
    def message(message_id: str, role: str, text: str) -> dict[str, object]:
        return {"id": message_id, "role": role, "blocks": [{"type": "text", "data": {"text": text}}]}

    def brief(self, session_id: str) -> dict[str, object]:
        items = self.service.context_runtime.materialize(session_id)["items"]
        return next(item["payload"] for item in items if item["sourceKind"] == "primary_task_brief")

    def test_task_brief_freezes_public_plan_at_exact_cutoff_and_replays_without_reread(self) -> None:
        request = self.request(objective="按刚才方案做", sourceMessageId="plan")
        messages = [self.message("request", "user", "先实现缓存再加测试"),
            self.message("plan", "assistant", "方案：只改缓存模块，并验证失效逻辑"),
            self.message("later", "user", "后来的另一个项目，不能混进任务")]
        messages[1]["blocks"].extend([
            {"type": "thinking", "data": {"text": "private reasoning must stay private"}},
            {"type": "tool_result", "data": {"text": "private tool output"}}])
        with patch.object(self.service, "messages", return_value={"items": messages, "lastSequence": 14}):
            result = self.service.create_primary_task(request)
        brief = self.brief(result["session"]["id"])
        self.assertEqual([item["id"] for item in brief["messages"]], ["request", "plan"])
        self.assertEqual(brief["cutoffMessageId"], "plan")
        self.assertEqual(brief["sourceRevision"], 14)
        self.assertEqual(len(brief["sha256"]), 64)
        self.assertEqual(brief["authority"], "context_only")
        self.assertNotIn("private reasoning must stay private", json.dumps(brief))
        self.assertNotIn("private tool output", json.dumps(brief))
        with patch.object(self.service, "messages", side_effect=AssertionError("retry must not reread source")):
            replay = self.service.create_primary_task(request)
        self.assertEqual(result["sourceContext"], replay["sourceContext"])
        self.assertEqual(brief, self.brief(result["session"]["id"]))

    def test_task_brief_rereads_snapshot_after_initial_runtime_reconciliation(self) -> None:
        request = self.request()
        source_id = str(request["sourceSessionId"])
        reads = []
        def snapshot(_session_id):
            reads.append(_session_id)
            if len(reads) == 1:
                with sqlite_connection(self.db) as conn:
                    conn.execute("UPDATE agent_sessions SET updated_at_ms=updated_at_ms+1 WHERE id=?", (source_id,))
                return {"items": [self.message("stale", "assistant", "Before reconciliation")], "lastSequence": 1}
            return {"items": [self.message("current", "assistant", "After reconciliation")], "lastSequence": 2}
        with patch.object(self.service, "messages", side_effect=snapshot):
            result = self.service.create_primary_task(request)
        brief = self.brief(result["session"]["id"])
        self.assertEqual(reads, [source_id, source_id])
        self.assertEqual(brief["sourceRevision"], 2)
        self.assertEqual([item["id"] for item in brief["messages"]], ["current"])
        self.assertEqual(brief["sourceSessionRevision"], self.service.sessions.get(source_id)["updatedAtMs"])

    def test_task_brief_continuously_changing_source_still_rejects_without_task(self) -> None:
        request = self.request()
        source_id = str(request["sourceSessionId"])
        def snapshot(_session_id):
            with sqlite_connection(self.db) as conn:
                conn.execute("UPDATE agent_sessions SET updated_at_ms=updated_at_ms+1 WHERE id=?", (source_id,))
            return {"items": [], "lastSequence": 1}
        with patch.object(self.service, "messages", side_effect=snapshot) as inspect:
            with self.assertRaisesRegex(ValueError, "source discussion changed"):
                self.service.create_primary_task(request)
        self.assertEqual(inspect.call_count, 2)
        self.assertEqual(self.service.ensure_primary_assistant({})["tasks"], [])

    def test_task_brief_change_after_stable_snapshot_still_rejects_at_store_fence(self) -> None:
        request = self.request()
        source_id = str(request["sourceSessionId"])
        prepare = self.service._primary_task_brief
        def change_after_snapshot(authorization):
            result = prepare(authorization)
            with sqlite_connection(self.db) as conn:
                conn.execute("UPDATE agent_sessions SET updated_at_ms=updated_at_ms+1 WHERE id=?", (source_id,))
            return result
        with patch.object(self.service, "messages", return_value={"items": [], "lastSequence": 0}), \
             patch.object(self.service, "_primary_task_brief", side_effect=change_after_snapshot):
            with self.assertRaisesRegex(ValueError, "source discussion changed"):
                self.service.create_primary_task(request)
        self.assertEqual(self.service.ensure_primary_assistant({})["tasks"], [])

    def test_task_brief_is_bounded_and_does_not_cross_project(self) -> None:
        request = self.request()
        messages = [self.message(str(index), "assistant", "公开方案" * 2_000) for index in range(20)]
        with patch.object(self.service, "messages", return_value={"items": messages}):
            result = self.service.create_primary_task(request)
        brief = self.brief(result["session"]["id"])
        self.assertLessEqual(len(brief["messages"]), 6)
        self.assertLessEqual(sum(len(item["text"]) for item in brief["messages"]), 12_000)
        self.assertEqual(brief["cutoffMessageId"], "19")
        self.assertTrue(brief["truncated"])
        self.assertGreater(brief["omittedMessageCount"], 0)
        project_source = self.service.ensure_primary_assistant({"workspaceRoots": [str(self.root)]})["session"]
        other = self.root / "other"
        other.mkdir()
        with self.assertRaisesRegex(ValueError, "match the source discussion project"):
            self.service.create_primary_task({**request, "clientRequestId": "cross-project",
                "sourceSessionId": project_source["id"], "workspaceRoots": [str(other)]})

    def test_context_failure_rolls_back_task_goal_and_dedup_claim(self) -> None:
        request = self.request()
        with patch.object(self.service.context_runtime, "enqueue", side_effect=ValueError("context write failed")):
            with self.assertRaisesRegex(ValueError, "context write failed"):
                self.service.create_primary_task(request)
        self.assertEqual(len(self.service.sessions.list()), 1)
        self.assertTrue(self.service.create_primary_task(request)["created"])

    def test_deleted_source_revokes_brief_without_recreation_on_task_retry(self) -> None:
        request = self.request()
        with patch.object(self.service, "messages", return_value={"items": [self.message("plan", "assistant", "原始方案")]}):
            result = self.service.create_primary_task(request)
        self.service.sessions.delete(request["sourceSessionId"])
        self.assertEqual(self.service.context_runtime.materialize(result["session"]["id"])["items"], [])
        replay = self.service.create_primary_task(request)
        self.assertEqual(replay["session"]["id"], result["session"]["id"])
        self.assertNotIn("sourceContext", replay)

    def test_goal_gateway_exposes_only_read_and_evidence_backed_completion(self) -> None:
        task = self.service.create_primary_task(self.request())["session"]
        gateway = ControlToolGateway(sessions=self.service.sessions,
            management=object(), core=object(), project="primary-assistant-tests")
        goal_tool = next(item for item in gateway.runtime_manifests(task) if item["name"] == "agent_goal")
        self.assertEqual({branch["properties"]["op"]["const"]
                          for branch in goal_tool["parameters"]["oneOf"]}, {"list", "complete"})
        def call(operation: str, **args: object) -> dict[str, object]:
            return gateway.execute({"schemaVersion": "rag-ime.agent-tool-call.v1", "sessionId": task["id"],
                "tool": "agent_goal", "toolCallId": f"goal:{operation}:{len(args)}",
                "args": {"op": operation, **args}})
        self.assertEqual(call("list")["result"]["goal"]["status"], "active")
        settle = {"schemaVersion": "rag-ime.agent-goal-settle-request.v1", "sessionId": task["id"],
            "settleScopeId": "original-scope", "settleAttempt": 1,
            "freshToolEvidenceCount": 1, "freshToolEvidenceSha256": "a" * 64}
        self.assertEqual(self.service.settle_goal_runtime(settle)["result"]["state"], "continue")
        with self.assertRaises(ValueError):
            call("update", objective="Changed without user approval")
        with self.assertRaises(ValueError):
            call("complete", summary="No evidence")
        completed = call("complete", summary="Verified", evidence=[{
            "kind": "test", "reference": "tests/test_primary_assistant_sessions.py", "summary": "Passed"}])
        self.assertEqual(completed["result"]["goal"]["status"], "completed")
        self.assertEqual(self.service.ensure_primary_assistant({})["tasks"][0]["goal"]["status"], "completed")
        self.assertEqual(self.service.settle_goal_runtime({**settle, "settleAttempt": 2})["result"]["state"], "completed")

    def test_only_successful_durable_source_rewrite_revokes_task_brief(self) -> None:
        request = self.request()
        with patch.object(self.service, "messages", return_value={"items": [self.message("plan", "assistant", "原始方案")]}):
            task = self.service.create_primary_task(request)["session"]
        arguments = {"session_id": request["sourceSessionId"], "entry_id": "plan", "message": "新方案",
            "attachment_ids": [], "client_message_id": "rewrite-original"}
        with patch.object(self.service.runtime, "rewind_session", side_effect=ValueError("rewind failed")):
            with self.assertRaisesRegex(ValueError, "rewind failed"):
                self.service.session_branching.rewrite_session_once(**arguments)
        self.assertEqual(self.brief(task["id"])["cutoffMessageId"], "plan")
        with patch.object(self.service.runtime, "rewind_session", return_value={}), patch.object(
            self.service.session_branching, "prompt_with_checkpoint", return_value={"accepted": True, "turnId": "rewrite-turn"}):
            self.service.session_branching.rewrite_session_once(**arguments)
        self.assertEqual(self.service.context_runtime.materialize(task["id"])["items"], [])
        self.assertEqual(self.service.sessions.get(task["id"])["goal"]["status"], "active")

    def test_actual_prompt_contains_frozen_brief_with_memory_disabled(self) -> None:
        request = self.request(objective="按刚才方案做")
        with patch.object(self.service, "messages", return_value={"items": [
            self.message("plan", "assistant", "只修改缓存模块，验证失效逻辑")]}):
            task = self.service.create_primary_task(request)["session"]
        self.service.sessions.set_disclosure_preferences(task["id"], {"tool:memory": "disabled"})
        with patch.object(self.service.runtime, "prompt", return_value={
            "accepted": True, "turnId": "turn:brief", "piEntryId": "entry:brief",
            "response": {"success": True}}) as runtime_prompt:
            self.service.prompt(task["id"], {"message": request["objective"], "clientMessageId": request["clientRequestId"]})
        delivered = json.dumps(runtime_prompt.call_args.args, ensure_ascii=False) + json.dumps(runtime_prompt.call_args.kwargs, ensure_ascii=False)
        self.assertIn("只修改缓存模块，验证失效逻辑", delivered)
        self.assertIn("context_only", delivered)
        self.assertIn("plan", delivered)
        self.assertEqual(self.brief(task["id"])["cutoffMessageId"], "plan")
        restored = self.service._runtime_session_context(self.service.sessions.get(task["id"]))
        self.assertIn("只修改缓存模块，验证失效逻辑", str(restored.get("sessionContext")))
        self.assertIn("sourceSessionRevision", str(restored.get("sessionContext")))
        compacted = self.service.memory_context_application.refresh({"sessionId": task["id"], "trigger": "compaction"})
        self.assertIn("只修改缓存模块，验证失效逻辑", compacted["result"]["sessionContext"])
        self.service.sessions.delete(request["sourceSessionId"])
        restored = self.service._runtime_session_context(self.service.sessions.get(task["id"]))
        self.assertNotIn("只修改缓存模块，验证失效逻辑", str(restored.get("sessionContext")))

    def test_append_only_migration_does_not_adopt_unrelated_sessions(self) -> None:
        original = self.service.create_session({"title": "original"})["session"]
        self.service.sessions.initialize()
        self.service.sessions.initialize()
        self.assertNotIn("metadata", self.service.sessions.get(original["id"]))
        with sqlite_connection(self.db, row_factory=sqlite3.Row) as conn:
            versions = [row[0] for row in conn.execute("SELECT version FROM schema_migrations WHERE version = 220")]
        self.assertEqual(versions, [220])


if __name__ == "__main__":
    unittest.main()
