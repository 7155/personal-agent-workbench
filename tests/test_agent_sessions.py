from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from rag_ime.agent_context_runtime import AgentContextRuntime
from rag_ime.rooms.work import AgentRoomWorkStore
from rag_ime.rooms.store import AgentRoomStore
from rag_ime.agent_sessions import (
    AgentGoalExecutionBlocked,
    AgentSessionNotFound,
    AgentSessionStore,
)
from rag_ime.contracts.json_schema import validate_contract
from rag_ime.work_documents import WorkDocumentService


class AgentSessionPersistentReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-session-reads-")
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "rag-ime.sqlite"
        self.store = AgentSessionStore(self.db_path, persistent_reads=True)
        self.addCleanup(self.store.close)
        self.store.initialize()
        self.session_id = str(self.store.create(title="fresh session")["id"])
        self.store.bind_pi_session(
            self.session_id,
            pi_session_id="pi-read-test",
            session_file="/managed/sessions/read-test.jsonl",
        )

    def test_get_reuses_initialized_connection(self) -> None:
        with patch("rag_ime.agent_sessions.sqlite3.connect", wraps=sqlite3.connect) as connect:
            for _ in range(5):
                self.assertEqual(self.store.get(self.session_id)["id"], self.session_id)
            connect.assert_not_called()

    def test_get_observes_external_policy_status_and_generation_commit(self) -> None:
        before = self.store.get(self.session_id)
        self.assertEqual(before["executionMode"], "per_action")
        self.assertEqual(before["status"], "active")
        self.assertEqual(before["runtimeBinding"]["generation"], 1)

        with closing(sqlite3.connect(self.db_path)) as writer:
            writer.execute(
                "UPDATE agent_sessions SET execution_mode = 'read_only', status = 'archived' WHERE id = ?",
                (self.session_id,),
            )
            writer.execute(
                "UPDATE agent_runtime_bindings SET generation = 2 WHERE session_id = ?",
                (self.session_id,),
            )
            writer.commit()

        after = self.store.get(self.session_id)
        self.assertEqual(after["executionMode"], "read_only")
        self.assertEqual(after["status"], "archived")
        self.assertEqual(after["runtimeBinding"]["generation"], 2)

    def test_get_shares_connection_safely_across_threads_and_remains_fresh(self) -> None:
        barrier = Barrier(7)

        def read_in_worker(_: int) -> list[str]:
            barrier.wait(timeout=5)
            before = [str(self.store.get(self.session_id)["title"]) for _ in range(10)]
            barrier.wait(timeout=5)
            barrier.wait(timeout=5)
            after = [str(self.store.get(self.session_id)["title"]) for _ in range(10)]
            return before + after

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(read_in_worker, index) for index in range(6)]
            barrier.wait(timeout=5)
            barrier.wait(timeout=5)
            with closing(sqlite3.connect(self.db_path)) as writer:
                writer.execute(
                    "UPDATE agent_sessions SET title = 'updated session' WHERE id = ?",
                    (self.session_id,),
                )
                writer.commit()
            barrier.wait(timeout=5)
            for future in futures:
                self.assertEqual(
                    future.result(timeout=5),
                    ["fresh session"] * 10 + ["updated session"] * 10,
                )

    def test_get_after_close_uses_temporary_connection(self) -> None:
        connection = self.store._read_connection
        self.store.close()
        self.store.close()
        assert connection is not None
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        with patch("rag_ime.agent_sessions.sqlite3.connect", wraps=sqlite3.connect) as connect:
            self.assertEqual(self.store.get(self.session_id)["id"], self.session_id)
            self.assertEqual(connect.call_count, 1)

    def test_nonpersistent_get_opens_and_closes_temporary_connections(self) -> None:
        store = AgentSessionStore(self.db_path)
        opened: list[sqlite3.Connection] = []
        original_connect = sqlite3.connect

        def connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            opened.append(connection)
            return connection

        with patch("rag_ime.agent_sessions.sqlite3.connect", side_effect=connect):
            for _ in range(2):
                self.assertEqual(store.get(self.session_id)["id"], self.session_id)
        self.assertEqual(len(opened), 2)
        for connection in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")


class AgentSessionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-agent-sessions-")
        self.db_path = Path(self.tmp.name) / "rag-ime.sqlite"
        self.store = AgentSessionStore(self.db_path)
        self.store.initialize()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_create_rejects_empty_model_profile_before_insert(self) -> None:
        with self.assertRaisesRegex(ValueError, "model profile must not be empty"):
            self.store.create(title="bad", model_profile="  ")

        self.assertEqual(self.store.list(), [])
        with closing(sqlite3.connect(self.db_path)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM agent_sessions").fetchone()[0]
        self.assertEqual(count, 0)

    def test_room_execution_mode_is_persisted_separately_from_session_mode(self) -> None:
        session = self.store.create(
            title="confirmed Room",
            mode="coordinator",
            execution_mode="per_action",
            room_execution_mode="room_unrestricted",
            workspace_roots=[self.tmp.name],
            created_at_ms=100,
        )

        self.assertEqual(session["executionMode"], "per_action")
        self.assertEqual(session["roomExecutionMode"], "room_unrestricted")

        updated = self.store.set_room_execution_mode(
            str(session["id"]),
            "",
            updated_at_ms=200,
        )
        self.assertEqual(updated["roomExecutionMode"], "")

    def test_list_recovers_legacy_empty_model_profile(self) -> None:
        session = self.store.create(title="legacy", model_profile="pi/default")
        session_id = str(session["id"])
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE agent_sessions SET model_profile = '' WHERE id = ?",
                (session_id,),
            )
            conn.commit()

        self.assertEqual(self.store.get(session_id)["modelProfile"], "pi/default")
        self.assertEqual(self.store.list()[0]["modelProfile"], "pi/default")

    def test_directory_page_omits_runtime_and_policy_payloads(self) -> None:
        created = self.store.create(
            title="快速目录",
            mode="coordinator",
            workspace_roots=[self.tmp.name],
            created_at_ms=100,
        )

        item = self.store.list_page(projection_only=True)["items"][0]

        self.assertEqual(item["id"], created["id"])
        self.assertEqual(item["title"], "快速目录")
        self.assertEqual(item["workspaceRoots"], [str(Path(self.tmp.name).resolve())])
        self.assertNotIn("sessionFile", item)
        self.assertNotIn("runtimeBinding", item)
        self.assertNotIn("allowedTools", item)

    def test_evaluation_snapshot_is_a_visible_read_only_session_kind(self) -> None:
        snapshot = self.store.create(
            title="EnterpriseOps Validation · Task 1",
            execution_mode="read_only",
            tool_profile_version="subagent-readonly-v1",
            evaluation_snapshot=True,
            created_at_ms=100,
        )

        self.assertEqual(snapshot["sessionKind"], "conversation")
        self.assertTrue(snapshot["evaluationSnapshot"])
        self.assertEqual(snapshot["executionMode"], "read_only")
        self.assertEqual(
            [snapshot["id"]],
            [item["id"] for item in self.store.list()],
        )
        validate_contract(snapshot, "agent-session.v1.json")

    def test_list_projects_latest_terminal_turn_identity_independent_of_session_updates(
        self,
    ) -> None:
        session = self.store.create(title="terminal identity", created_at_ms=100)
        session_id = str(session["id"])
        self.store.record_runtime_event(
            event_id="event:terminal:first",
            session_id=session_id,
            turn_id="turn:first",
            sequence=1,
            event_type="turn_completed",
            created_at_ms=110,
        )
        self.store.record_runtime_event(
            event_id="event:non-terminal",
            session_id=session_id,
            turn_id="turn:first",
            sequence=2,
            event_type="memory_checkpointed",
            created_at_ms=120,
        )

        first = self.store.list()[0]
        self.assertEqual(first["lastTerminalTurnId"], "turn:first")
        self.store.set_status(session_id, "idle", updated_at_ms=900)
        self.assertEqual(
            self.store.list()[0]["lastTerminalTurnId"],
            "turn:first",
        )

        self.store.record_runtime_event(
            event_id="event:terminal:second",
            session_id=session_id,
            turn_id="turn:second",
            sequence=3,
            event_type="turn_failed",
            created_at_ms=910,
        )
        self.assertEqual(
            self.store.list()[0]["lastTerminalTurnId"],
            "turn:second",
        )

    def test_retired_builtin_role_ids_are_read_only_compatibility_aliases(
        self,
    ) -> None:
        session = self.store.create(
            title="canonical identity",
            role_id="vcp-v1",
            role_book_revision_id="role-book:vcp-v1:1:test",
        )

        self.assertEqual(session["roleId"], "companion-future-v1")
        self.assertEqual(
            session["roleBookRevisionId"],
            "role-book:companion-future-v1:1:test",
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            stored = conn.execute(
                """
                SELECT role_id, role_book_revision_id
                FROM agent_sessions
                WHERE id = ?
                """,
                (session["id"],),
            ).fetchone()
        self.assertEqual(
            stored,
            (
                "companion-future-v1",
                "role-book:companion-future-v1:1:test",
            ),
        )

    def test_create_list_bind_archive_restore_and_delete(self) -> None:
        session = self.store.create(title=" 输入助手   今天 ", created_at_ms=100)
        session_id = str(session["id"])

        self.assertEqual(session["title"], "输入助手 今天")
        self.assertEqual(session["mode"], "assistant")
        self.assertEqual(session["roleId"], "companion-future-v1")
        self.assertEqual(session["modelProfile"], "openai-codex/gpt-5.6-sol")
        self.assertEqual(session["thinkingLevel"], "max")
        self.assertEqual(session["executionMode"], "per_action")
        self.assertFalse(session["workspaceScopeGranted"])
        self.assertFalse(session["projectContextEnabled"])
        self.assertFalse(session["piSkillsEnabled"])
        self.assertFalse(session["codexSkillsEnabled"])
        self.assertEqual(session["workspaceRoots"], [])
        self.assertEqual(self.store.list()[0]["id"], session_id)

        without_project_context = self.store.set_runtime_policy(
            session_id,
            mode="assistant",
            tool_profile_version="control-center-v1",
            allowed_tools=None,
            project_context_enabled=False,
            pi_skills_enabled=True,
            codex_skills_enabled=True,
            updated_at_ms=150,
        )
        self.assertFalse(without_project_context["projectContextEnabled"])
        self.assertTrue(without_project_context["piSkillsEnabled"])
        self.assertTrue(without_project_context["codexSkillsEnabled"])

        bound = self.store.bind_pi_session(
            session_id,
            pi_session_id="pi-1",
            session_file="/managed/sessions/pi-1.jsonl",
            message_count=3,
            updated_at_ms=200,
        )
        self.assertEqual(bound["status"], "active")
        self.assertEqual(bound["messageCount"], 3)
        self.assertEqual(bound["runtimeBinding"]["runtimeKind"], "pi_rpc")
        self.assertEqual(bound["runtimeBinding"]["generation"], 1)
        private_binding = self.store.runtime_binding(session_id)
        self.assertEqual(private_binding["transcriptRef"], "/managed/sessions/pi-1.jsonl")

        rebound = self.store.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-1",
            transcript_ref="/managed/sessions/pi-1.jsonl",
            binding_state="active",
            message_count=4,
            updated_at_ms=220,
        )
        self.assertEqual(rebound["runtimeBinding"]["generation"], 2)
        with self.assertRaisesRegex(ValueError, "cannot change runtime driver"):
            self.store.bind_runtime_session(
                session_id,
                driver_id="remote-gateway",
                runtime_kind="gateway_http",
                external_session_id="remote-1",
            )

        selected = self.store.set_model_profile(
            session_id,
            "openrouter/anthropic/claude-sonnet",
            updated_at_ms=250,
        )
        self.assertEqual(selected["modelProfile"], "openrouter/anthropic/claude-sonnet")

        archived = self.store.archive(session_id, updated_at_ms=300)
        self.assertEqual(archived["status"], "archived")
        self.assertEqual(self.store.list(), [])
        self.assertEqual(len(self.store.list(include_archived=True)), 1)

        restored = self.store.archive(session_id, archived=False, updated_at_ms=400)
        self.assertEqual(restored["status"], "idle")
        deleted = self.store.delete(session_id)
        self.assertEqual(deleted["id"], session_id)
        with self.assertRaises(AgentSessionNotFound):
            self.store.get(session_id)

    def test_search_history_returns_bounded_navigation_anchors_only(self) -> None:
        current = self.store.create(title="当前 Session", created_at_ms=100)
        historical = self.store.create(title="Agent TUI 架构", created_at_ms=200)
        archived = self.store.create(title="归档的 Launch Digest", created_at_ms=300)
        internal = self.store.create(
            title="不应出现的子 Agent",
            session_kind="subagent_runtime",
            created_at_ms=400,
        )
        self.store.set_status(
            str(historical["id"]),
            "idle",
            message_count=7,
            last_message_preview="已经讨论 Session flow",
            updated_at_ms=500,
        )
        self.store.record_runtime_event(
            event_id="event:launch-digest",
            session_id=str(historical["id"]),
            turn_id="turn:contract",
            sequence=1,
            event_type="message_completed",
            created_at_ms=510,
            redacted_summary="Launch Digest 与结构化终止合同已确定",
        )
        self.store.archive(str(archived["id"]), updated_at_ms=600)

        anchors = self.store.search_history(
            query="Launch Digest",
            requester_session_id=str(current["id"]),
            limit=10,
        )
        self.assertEqual([item["sessionId"] for item in anchors], [historical["id"]])
        anchor = anchors[0]
        self.assertEqual(anchor["matchKind"], "runtime_event")
        self.assertEqual(anchor["eventId"], "event:launch-digest")
        self.assertEqual(anchor["turnId"], "turn:contract")
        self.assertEqual(
            anchor["evidenceRef"],
            f"{historical['id']}#event:event:launch-digest",
        )
        self.assertIn("#/agent?session=", anchor["href"])
        self.assertNotIn("sessionFile", anchor)
        self.assertNotIn("transcript", str(anchor).lower())

        recent = self.store.search_history(
            requester_session_id=str(current["id"]),
            limit=10,
        )
        self.assertEqual(recent[0]["sessionId"], current["id"])
        self.assertTrue(recent[0]["isCurrentSession"])
        visible_ids = {item["sessionId"] for item in recent}
        self.assertNotIn(archived["id"], visible_ids)
        self.assertNotIn(internal["id"], visible_ids)

        with_archived = self.store.search_history(
            query="Launch Digest",
            include_archived=True,
            limit=10,
        )
        self.assertEqual(
            {item["sessionId"] for item in with_archived},
            {historical["id"], archived["id"]},
        )

    def test_workspace_execution_grant_is_invalidated_and_can_be_regranted(self) -> None:
        first_root = self.tmp.name
        second_root = str(Path(self.tmp.name) / "second")
        Path(second_root).mkdir()
        session = self.store.create(
            title="托管工作区",
            mode="coordinator",
            execution_mode="workspace_managed",
            workspace_roots=[first_root],
            created_at_ms=100,
        )

        self.assertEqual(session["executionMode"], "workspace_managed")
        self.assertTrue(session["workspaceScopeGranted"])
        first_digest = session["workspaceScopeSha256"]

        unchanged = self.store.set_runtime_policy(
            str(session["id"]),
            mode="coordinator",
            tool_profile_version="control-center-v1",
            execution_mode="workspace_managed",
            allowed_tools=None,
            workspace_roots=[first_root],
            updated_at_ms=200,
        )
        self.assertTrue(unchanged["workspaceScopeGranted"])
        self.assertEqual(unchanged["workspaceScopeSha256"], first_digest)

        changed = self.store.set_runtime_policy(
            str(session["id"]),
            mode="coordinator",
            tool_profile_version="control-center-v1",
            execution_mode="workspace_managed",
            allowed_tools=None,
            workspace_roots=[second_root],
            updated_at_ms=300,
        )
        self.assertFalse(changed["workspaceScopeGranted"])
        self.assertEqual(changed["workspaceScopeSha256"], "")

        regranted = self.store.set_runtime_policy(
            str(session["id"]),
            mode="coordinator",
            tool_profile_version="control-center-v1",
            execution_mode="workspace_managed",
            grant_workspace_scope=True,
            allowed_tools=None,
            workspace_roots=[second_root],
            updated_at_ms=400,
        )
        self.assertTrue(regranted["workspaceScopeGranted"])
        self.assertNotEqual(regranted["workspaceScopeSha256"], first_digest)

        per_action = self.store.set_runtime_policy(
            str(session["id"]),
            mode="coordinator",
            tool_profile_version="control-center-v1",
            execution_mode="per_action",
            allowed_tools=None,
            workspace_roots=[second_root],
            updated_at_ms=500,
        )
        self.assertFalse(per_action["workspaceScopeGranted"])
        self.assertEqual(per_action["workspaceScopeSha256"], "")

    def test_workspace_scope_projection_does_not_claim_authorization_after_roots_drift(self) -> None:
        session = self.store.create(
            title="目录授权漂移",
            mode="coordinator",
            execution_mode="workspace_managed",
            workspace_roots=[self.tmp.name],
            created_at_ms=100,
        )
        session_id = str(session["id"])
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE agent_sessions SET workspace_roots_json = '[]' WHERE id = ?",
                (session_id,),
            )
            conn.commit()

        projected = self.store.get(session_id)

        self.assertEqual(projected["workspaceRoots"], [])
        self.assertFalse(projected["workspaceScopeGranted"])

    def test_auto_approve_profile_is_persisted_as_an_unrestricted_policy(self) -> None:
        session = self.store.create(
            title="全盘自动批准",
            mode="coordinator",
            tool_profile_version="control-center-auto-approve-v1",
            workspace_roots=[self.tmp.name],
            created_at_ms=100,
        )

        self.assertEqual(session["executionMode"], "full_trust")
        self.assertEqual(
            session["toolProfileVersion"],
            "control-center-auto-approve-v1",
        )
        self.assertEqual(
            session["workspaceRoots"],
            [str(Path(self.tmp.name).resolve()), "/"],
        )
        self.assertTrue(session["workspaceScopeGranted"])
        self.assertTrue(session["projectContextEnabled"])
        self.assertTrue(session["piSkillsEnabled"])
        self.assertTrue(session["codexSkillsEnabled"])

    def test_full_access_profile_defaults_to_root_and_all_context_capabilities(self) -> None:
        session = self.store.create(
            title="全面访问",
            mode="coordinator",
            tool_profile_version="control-center-full-access-v1",
            execution_mode="per_action",
            created_at_ms=100,
        )

        self.assertEqual(session["workspaceRoots"], ["/"])
        self.assertTrue(session["workspaceScopeGranted"])
        self.assertTrue(session["projectContextEnabled"])
        self.assertTrue(session["piSkillsEnabled"])
        self.assertTrue(session["codexSkillsEnabled"])

    def test_assistant_rejects_workspace_and_coordinator_persists_normalized_roots(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot carry workspace roots"):
            self.store.create(title="bad", workspace_roots=[self.tmp.name])

        coordinator = self.store.create(
            title="运行协调",
            mode="coordinator",
            workspace_roots=[self.tmp.name, self.tmp.name],
            created_at_ms=100,
        )
        self.assertEqual(coordinator["workspaceRoots"], [str(Path(self.tmp.name).resolve())])
        self.assertEqual(coordinator["shellPolicyVersion"], "coordinator-per-command-v1")

        switched = self.store.set_mode(
            str(coordinator["id"]),
            "assistant",
            updated_at_ms=200,
        )
        self.assertEqual(switched["mode"], "assistant")
        self.assertEqual(switched["workspaceRoots"], [])
        self.assertEqual(switched["shellPolicyVersion"], "assistant-no-shell-v1")

        switched_back = self.store.set_mode(
            str(coordinator["id"]),
            "coordinator",
            workspace_roots=[self.tmp.name],
            updated_at_ms=300,
        )
        self.assertEqual(switched_back["mode"], "coordinator")
        self.assertEqual(switched_back["workspaceRoots"], [str(Path(self.tmp.name).resolve())])

    def test_role_book_revision_can_only_be_pinned_once(self) -> None:
        session = self.store.create(title="pinned role book", created_at_ms=100)
        session_id = str(session["id"])

        first = self.store.set_role_book_revision(
            session_id,
            "role-book:one",
            updated_at_ms=200,
        )
        repeated = self.store.set_role_book_revision(
            session_id,
            "role-book:one",
            updated_at_ms=300,
        )

        self.assertEqual(first["roleBookRevisionId"], "role-book:one")
        self.assertEqual(repeated["roleBookRevisionId"], "role-book:one")
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.store.set_role_book_revision(
                session_id,
                "role-book:two",
                updated_at_ms=400,
            )

    def test_runtime_event_sequence_survives_store_recreation(self) -> None:
        session = self.store.create(title="sequence", created_at_ms=100)
        session_id = str(session["id"])
        for sequence in range(1, 4):
            self.store.record_runtime_event(
                event_id=f"{session_id}:{sequence}",
                session_id=session_id,
                turn_id="turn-1",
                sequence=sequence,
                event_type="status_changed",
                created_at_ms=100 + sequence,
                redacted_summary="busy",
            )

        recreated = AgentSessionStore(self.db_path)
        recreated.initialize()
        self.assertEqual(recreated.max_event_sequence(session_id), 3)

        with closing(sqlite3.connect(self.db_path)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM agent_runtime_events WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
        self.assertEqual(count, 3)

    def test_agent_todo_revisions_project_atomic_state_transitions(self) -> None:
        session = self.store.create(title="todo", created_at_ms=100)
        session_id = str(session["id"])

        initialized = self.store.mutate_agent_todo(
            session_id,
            {
                "op": "init",
                "list": [
                    {
                        "phase": "执行",
                        "items": ["核对权限边界", "实现并验收"],
                    }
                ],
            },
            actor="test-agent",
            updated_at_ms=200,
        )
        self.assertEqual(initialized["todo"]["revision"], 1)
        self.assertEqual(initialized["todo"]["actor"], "test-agent")
        self.assertEqual(
            initialized["todo"]["phases"],
            [
                {
                    "name": "执行",
                    "tasks": [
                        {"content": "核对权限边界", "status": "in_progress"},
                        {"content": "实现并验收", "status": "pending"},
                    ],
                }
            ],
        )

        started = self.store.mutate_agent_todo(
            session_id,
            {"op": "start", "task": "核对权限边界"},
            updated_at_ms=300,
        )
        self.assertEqual(started["todo"]["revision"], 2)
        self.assertEqual(
            [
                task["status"]
                for task in started["todo"]["phases"][0]["tasks"]
            ],
            ["in_progress", "pending"],
        )

        completed = self.store.mutate_agent_todo(
            session_id,
            {"op": "done", "task": "核对权限边界"},
            updated_at_ms=400,
        )
        self.assertEqual(completed["todo"]["revision"], 3)
        self.assertEqual(
            completed["completedTasks"],
            [{"phase": "执行", "task": "核对权限边界"}],
        )

        appended = self.store.mutate_agent_todo(
            session_id,
            {"op": "append", "phase": "执行", "items": ["补充回归"]},
            updated_at_ms=500,
        )
        self.assertEqual(appended["todo"]["revision"], 4)
        self.assertEqual(
            [task["content"] for task in appended["todo"]["phases"][0]["tasks"]],
            ["核对权限边界", "实现并验收", "补充回归"],
        )

        dropped = self.store.mutate_agent_todo(
            session_id,
            {"op": "drop", "task": "实现并验收"},
            updated_at_ms=600,
        )
        self.assertEqual(dropped["todo"]["revision"], 5)
        self.assertEqual(
            [task["status"] for task in dropped["todo"]["phases"][0]["tasks"]],
            ["completed", "abandoned", "in_progress"],
        )

        removed = self.store.mutate_agent_todo(
            session_id,
            {"op": "rm", "task": "补充回归"},
            updated_at_ms=700,
        )
        self.assertEqual(removed["todo"]["revision"], 6)
        self.assertEqual(removed["todo"]["counts"]["total"], 2)
        self.assertEqual(
            self.store.agent_todo(session_id),
            removed["todo"],
        )
        viewed = self.store.mutate_agent_todo(session_id, {"op": "view"})
        self.assertFalse(viewed["changed"])
        self.assertEqual(viewed["todo"], removed["todo"])

        with closing(sqlite3.connect(self.db_path)) as conn:
            events = conn.execute(
                """
                SELECT revision, operation
                FROM agent_todo_events
                WHERE session_id = ? ORDER BY revision
                """,
                (session_id,),
            ).fetchall()
        self.assertEqual(
            events,
            [
                (1, "init"),
                (2, "start"),
                (3, "done"),
                (4, "append"),
                (5, "drop"),
                (6, "rm"),
            ],
        )

    def test_agent_todo_block_advances_work_and_unblock_preserves_reasoned_state(self) -> None:
        session_id = str(self.store.create(title="blocked todo", created_at_ms=100)["id"])
        self.store.mutate_agent_todo(
            session_id,
            {
                "op": "init",
                "phase": "执行",
                "items": ["等待用户选择", "继续其余验收"],
            },
            updated_at_ms=200,
        )

        blocked = self.store.mutate_agent_todo(
            session_id,
            {
                "op": "block",
                "task": "等待用户选择",
                "reason": "等待用户决定兼容性范围",
            },
            updated_at_ms=300,
        )["todo"]
        self.assertEqual(
            blocked["phases"][0]["tasks"],
            [
                {
                    "content": "等待用户选择",
                    "status": "blocked",
                    "reason": "等待用户决定兼容性范围",
                },
                {"content": "继续其余验收", "status": "in_progress"},
            ],
        )
        self.assertEqual(blocked["counts"]["blocked"], 1)
        self.assertEqual(blocked["counts"]["inProgress"], 1)

        unblocked = self.store.mutate_agent_todo(
            session_id,
            {"op": "unblock", "task": "等待用户选择"},
            updated_at_ms=400,
        )["todo"]
        self.assertEqual(
            [task["status"] for task in unblocked["phases"][0]["tasks"]],
            ["pending", "in_progress"],
        )
        self.assertNotIn("reason", unblocked["phases"][0]["tasks"][0])

        restarted = self.store.mutate_agent_todo(
            session_id,
            {"op": "start", "task": "等待用户选择"},
            updated_at_ms=500,
        )["todo"]
        self.assertEqual(
            [task["status"] for task in restarted["phases"][0]["tasks"]],
            ["in_progress", "pending"],
        )

    def test_agent_todo_start_transitions_are_atomic_and_keep_one_current_task(
        self,
    ) -> None:
        session_id = str(self.store.create(title="concurrent todo")["id"])
        self.store.mutate_agent_todo(
            session_id,
            {
                "op": "init",
                "items": ["第一项", "第二项"],
            },
        )

        def start(task: str) -> None:
            self.store.mutate_agent_todo(
                session_id,
                {"op": "start", "task": task},
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(start, ("第一项", "第二项")))

        todo = self.store.agent_todo(session_id)
        self.assertEqual(todo["counts"]["inProgress"], 1)
        self.assertEqual(todo["counts"]["total"], 2)
        self.assertEqual(
            sorted(
                task["status"]
                for phase in todo["phases"]
                for task in phase["tasks"]
            ),
            ["in_progress", "pending"],
        )
        self.assertEqual(todo["revision"], 3)
        with closing(sqlite3.connect(self.db_path)) as conn:
            operations = [
                row[0]
                for row in conn.execute(
                    """
                    SELECT operation
                    FROM agent_todo_events
                    WHERE session_id = ? ORDER BY revision
                    """,
                    (session_id,),
                ).fetchall()
            ]
        self.assertEqual(operations, ["init", "start", "start"])

    def test_agent_todo_keeps_creation_order_when_task_status_changes(self) -> None:
        session_id = str(self.store.create(title="stable todo", created_at_ms=100)["id"])
        self.store.mutate_agent_todo(
            session_id,
            {
                "op": "init",
                "list": [
                    {
                        "phase": "交付",
                        "items": ["读取现状", "实现界面"],
                    }
                ],
            },
            updated_at_ms=200,
        )
        self.store.mutate_agent_todo(
            session_id,
            {"op": "done", "task": "读取现状"},
            updated_at_ms=300,
        )
        todo = self.store.mutate_agent_todo(
            session_id,
            {"op": "start", "task": "实现界面"},
            updated_at_ms=400,
        )["todo"]

        self.assertEqual(
            [task["content"] for task in todo["phases"][0]["tasks"]],
            ["读取现状", "实现界面"],
        )
        self.assertEqual(
            [task["status"] for task in todo["phases"][0]["tasks"]],
            ["completed", "in_progress"],
        )

    def test_todo_never_changes_work_authority(self) -> None:
        session_id = str(self.store.create(title="todo authority")["id"])
        before = self.store.workflow_state(session_id)
        self.store.mutate_agent_todo(
            session_id,
            {"op": "init", "items": ["执行用户请求"]},
            actor="agent-runtime",
        )
        after = self.store.mutate_agent_todo(
            session_id,
            {"op": "start", "task": "执行用户请求"},
            actor="agent-runtime",
        )["todo"]

        workflow = self.store.workflow_state(session_id)
        self.assertEqual(before["actGate"]["reason"], "user_execution_request")
        self.assertTrue(workflow["actGate"]["allowed"])
        self.assertEqual(workflow["actGate"]["reason"], "user_execution_request")
        self.assertEqual(workflow["todo"], self.store.agent_todo(session_id))
        self.assertEqual(workflow["actGate"]["todoRevision"], after["revision"])
        self.assertEqual(
            workflow["actGate"]["goalRevision"],
            workflow["goal"]["revision"],
        )

    def test_todo_and_goal_mutation_contract_requires_goal_setup_fence(
        self,
    ) -> None:
        validate_contract(
            {
                "action": "confirm_setup",
                "expectedRevision": 0,
                "confirmed": True,
                "objective": "交付可验证结果",
                "successCriteria": "验收通过",
                "evidenceExpectations": ["测试回执"],
            },
            "agent-goal-mutation.v1.json",
        )
        with self.assertRaisesRegex(ValueError, "missing required field"):
            validate_contract(
                {
                    "action": "confirm_setup",
                    "confirmed": True,
                    "objective": "缺少 revision",
                },
                "agent-goal-mutation.v1.json",
            )
        with self.assertRaisesRegex(ValueError, "missing required field"):
            validate_contract(
                {
                    "action": "cancel",
                    "expectedRevision": 1,
                },
                "agent-goal-mutation.v1.json",
            )

    def test_fenced_room_dispatch_is_a_work_authority_without_rewriting_todo(self) -> None:
        session = self.store.create(title="Room worker", created_at_ms=100)
        session_id = str(session["id"])

        ordinary = self.store.workflow_state(session_id)
        room = self.store.require_workspace_act(
            session_id,
            room_dispatch_authorized=True,
        )

        self.assertTrue(ordinary["actGate"]["allowed"])
        self.assertEqual(ordinary["actGate"]["reason"], "user_execution_request")
        self.assertEqual(room["todo"]["revision"], 0)
        self.assertEqual(room["todo"]["phases"], [])
        self.assertTrue(room["actGate"]["allowed"])
        self.assertIn("当前 Room 任务已经开始", room["actGate"]["message"])

    def test_thread_goal_budget_pause_and_evidence_audit_control_act_gate(self) -> None:
        session = self.store.create(title="goal", created_at_ms=100)
        session_id = str(session["id"])

        goal = self.store.mutate_agent_goal(
            session_id,
            {
                "action": "confirm_setup",
                "confirmed": True,
                "expectedRevision": 0,
                "successCriteria": "所有 Todo 项完成并有可核验回执",
                "evidenceExpectations": ["聚焦测试结果", "交付产物引用"],
                "objective": "在固定预算内交付可验证实现",
                "tokenBudget": 1_000,
                "timeBudgetMs": 60_000,
            },
        )["workflow"]["goal"]
        self.assertEqual(
            goal["successCriteria"],
            "所有 Todo 项完成并有可核验回执",
        )
        self.assertEqual(
            goal["evidenceExpectations"],
            ["聚焦测试结果", "交付产物引用"],
        )
        reconnected_goal = AgentSessionStore(self.db_path).agent_goal(session_id)
        self.assertEqual(reconnected_goal, goal)
        workflow = self.store.workflow_state(session_id)
        self.assertEqual(
            workflow["actGate"]["todoRevision"],
            workflow["todo"]["revision"],
        )
        self.assertEqual(
            workflow["actGate"]["goalRevision"],
            workflow["goal"]["revision"],
        )
        usage = self.store.record_agent_goal_usage(
            session_id,
            idempotency_key="turn:1:usage",
            turn_id="turn:1",
            event_id="event:1",
            token_delta=600,
            elapsed_delta_ms=30_000,
        )["goal"]
        self.assertEqual(usage["usage"], {"tokens": 600, "elapsedMs": 30_000})
        duplicate = self.store.record_agent_goal_usage(
            session_id,
            idempotency_key="turn:1:usage",
            turn_id="turn:1",
            event_id="event:1",
            token_delta=600,
            elapsed_delta_ms=30_000,
        )["goal"]
        self.assertEqual(duplicate["usage"], {"tokens": 600, "elapsedMs": 30_000})
        with self.assertRaisesRegex(ValueError, "reused with different data"):
            self.store.record_agent_goal_usage(
                session_id,
                idempotency_key="turn:1:usage",
                turn_id="turn:1",
                event_id="event:1",
                token_delta=601,
                elapsed_delta_ms=30_000,
            )

        paused = self.store.mutate_agent_goal(
            session_id,
            {"action": "pause", "expectedRevision": usage["revision"]},
        )["workflow"]
        self.assertEqual(paused["actGate"]["reason"], "goal_paused")
        with self.assertRaisesRegex(ValueError, "goal_paused"):
            self.store.require_goal_execution(session_id)
        resumed = self.store.mutate_agent_goal(
            session_id,
            {"action": "resume", "expectedRevision": paused["goal"]["revision"]},
        )["workflow"]["goal"]
        exhausted = self.store.record_agent_goal_usage(
            session_id,
            idempotency_key="turn:2:usage",
            turn_id="turn:2",
            event_id="event:2",
            token_delta=400,
            elapsed_delta_ms=1,
        )
        self.assertTrue(exhausted["goal"]["budgetExceeded"])
        self.assertEqual(exhausted["actGate"]["reason"], "goal_budget_exhausted")
        with self.assertRaisesRegex(ValueError, "goal_budget_exhausted"):
            self.store.require_goal_execution(session_id)

        expanded = self.store.mutate_agent_goal(
            session_id,
            {
                "action": "update",
                "expectedRevision": exhausted["goal"]["revision"],
                "objective": goal["objective"],
                "tokenBudget": 2_000,
                "timeBudgetMs": 120_000,
            },
        )["workflow"]["goal"]
        completed = self.store.mutate_agent_goal(
            session_id,
            {
                "action": "complete",
                "expectedRevision": expanded["revision"],
                "summary": "契约、测试与产物均已验收",
                "evidence": [
                    {
                        "kind": "test",
                        "summary": "聚焦测试通过",
                        "reference": "python3 -m unittest tests.test_agent_sessions",
                    }
                ],
            },
        )["workflow"]
        self.assertEqual(completed["goal"]["completionAudit"]["evidence"][0]["kind"], "test")
        self.assertEqual(completed["actGate"]["reason"], "goal_completed")
        cleared = self.store.mutate_agent_goal(
            session_id,
            {"action": "clear", "expectedRevision": completed["goal"]["revision"]},
        )["workflow"]
        self.assertFalse(cleared["goal"]["configured"])
        self.assertTrue(cleared["actGate"]["allowed"])
    def test_cancelled_goal_is_terminal_audited_and_blocks_future_work(
        self,
    ) -> None:
        session_id = str(self.store.create(title="cancelled goal")["id"])
        goal = self.store.mutate_agent_goal(
            session_id,
            {
                "action": "confirm_setup",
                "confirmed": True,
                "expectedRevision": 0,
                "objective": "在用户终止前持续执行",
                "successCriteria": "用户验收",
                "evidenceExpectations": ["运行回执"],
            },
            actor="control-center-user",
        )["workflow"]["goal"]
        cancelled = self.store.mutate_agent_goal(
            session_id,
            {
                "action": "cancel",
                "expectedRevision": goal["revision"],
                "reason": "目标优先级已经变化",
            },
            actor="user:owner",
            updated_at_ms=900,
        )["workflow"]

        self.assertEqual(cancelled["goal"]["status"], "cancelled")
        self.assertEqual(cancelled["actGate"]["reason"], "goal_cancelled")
        self.assertEqual(
            cancelled["goal"]["cancellationAudit"]["reason"],
            "目标优先级已经变化",
        )
        self.assertEqual(
            cancelled["goal"]["cancellationAudit"]["cancelledBy"],
            "user:owner",
        )
        validate_contract(cancelled, "agent-workflow-state.v1.json")
        reconnected = AgentSessionStore(self.db_path).workflow_state(session_id)
        self.assertEqual(reconnected, cancelled)
        with self.assertRaisesRegex(ValueError, "goal_cancelled"):
            self.store.require_goal_execution(session_id)
        with self.assertRaisesRegex(ValueError, "active goal"):
            self.store.record_agent_goal_usage(
                session_id,
                idempotency_key="cancelled:usage",
                token_delta=1,
            )
        continuation = self.store.claim_agent_goal_continuation(
            session_id,
            goal_id=str(goal["goalId"]),
            request_key="cancelled:continuation",
            limit=4,
        )
        self.assertFalse(continuation["claimed"])
        self.assertEqual(continuation["reason"], "goal_cancelled")
        with self.assertRaisesRegex(ValueError, "changed; refresh"):
            self.store.mutate_agent_goal(
                session_id,
                {
                    "action": "cancel",
                    "expectedRevision": goal["revision"],
                    "reason": "重复提交",
                },
            )
        self.assertEqual(
            self.store.agent_goal(session_id)["cancellationAudit"],
            cancelled["goal"]["cancellationAudit"],
        )
        with self.assertRaisesRegex(ValueError, "active or paused"):
            self.store.mutate_agent_goal(
                session_id,
                {
                    "action": "update",
                    "expectedRevision": cancelled["goal"]["revision"],
                    "objective": "不能复活的目标",
                },
            )
        cleared = self.store.mutate_agent_goal(
            session_id,
            {
                "action": "clear",
                "expectedRevision": cancelled["goal"]["revision"],
            },
        )["workflow"]
        self.assertFalse(cleared["goal"]["configured"])
        self.assertEqual(cleared["goal"]["status"], "cleared")
        self.assertTrue(cleared["actGate"]["allowed"])


    def test_goal_continuation_budget_persists_and_resets_only_on_lifecycle(
        self,
    ) -> None:
        session = self.store.create(title="goal continuation", created_at_ms=100)
        session_id = str(session["id"])
        goal = self.store.mutate_agent_goal(
            session_id,
            {
                "action": "confirm_setup",
                "confirmed": True,
                "expectedRevision": 0,
                "objective": "跨运行时保持续投上限",
            },
        )["workflow"]["goal"]
        goal_id = str(goal["goalId"])

        for index in range(1, 3):
            claim = self.store.claim_agent_goal_continuation(
                session_id,
                goal_id=goal_id,
                request_key=f"scope:{index}:attempt:1",
                limit=4,
                updated_at_ms=100 + index,
            )
            self.assertTrue(claim["claimed"])
            self.assertEqual(claim["issuedCount"], index)
        replay_first = self.store.claim_agent_goal_continuation(
            session_id,
            goal_id=goal_id,
            request_key="scope:1:attempt:1",
            limit=4,
            updated_at_ms=103,
        )
        self.assertTrue(replay_first["claimed"])
        self.assertTrue(replay_first["replayed"])
        self.assertEqual(replay_first["issuedCount"], 2)
        self.assertEqual(replay_first["remaining"], 2)
        for index in range(3, 5):
            claim = self.store.claim_agent_goal_continuation(
                session_id,
                goal_id=goal_id,
                request_key=f"scope:{index}:attempt:1",
                limit=4,
                updated_at_ms=100 + index,
            )
            self.assertTrue(claim["claimed"])
            self.assertEqual(claim["issuedCount"], index)
        replay = self.store.claim_agent_goal_continuation(
            session_id,
            goal_id=goal_id,
            request_key="scope:4:attempt:1",
            limit=4,
            updated_at_ms=200,
        )
        blocked = self.store.claim_agent_goal_continuation(
            session_id,
            goal_id=goal_id,
            request_key="new-process-scope:attempt:1",
            limit=4,
            updated_at_ms=201,
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["issuedCount"], 4)
        self.assertFalse(blocked["claimed"])
        self.assertEqual(blocked["reason"], "goal_continuation_limit")

        paused = self.store.mutate_agent_goal(
            session_id,
            {"action": "pause", "expectedRevision": goal["revision"]},
            updated_at_ms=300,
        )["workflow"]["goal"]
        paused_budget = self.store.agent_goal_continuation_budget(
            session_id,
            goal_id=goal_id,
            limit=4,
        )
        self.assertEqual(paused_budget["issuedCount"], 0)
        self.assertEqual(paused_budget["epoch"], 2)
        resumed = self.store.mutate_agent_goal(
            session_id,
            {"action": "resume", "expectedRevision": paused["revision"]},
            updated_at_ms=301,
        )["workflow"]["goal"]
        with self.assertRaisesRegex(ValueError, "another lifecycle epoch"):
            self.store.claim_agent_goal_continuation(
                session_id,
                goal_id=goal_id,
                request_key="scope:1:attempt:1",
                limit=4,
                updated_at_ms=302,
            )
        resumed_claim = self.store.claim_agent_goal_continuation(
            session_id,
            goal_id=goal_id,
            request_key="after-resume:attempt:1",
            limit=4,
            updated_at_ms=303,
        )
        self.assertTrue(resumed_claim["claimed"])
        self.assertEqual(resumed_claim["issuedCount"], 1)
        self.assertEqual(resumed_claim["epoch"], 3)

        completed = self.store.mutate_agent_goal(
            session_id,
            {
                "action": "complete",
                "expectedRevision": resumed["revision"],
                "summary": "持久续投上限已验证",
                "evidence": [
                    {
                        "kind": "test",
                        "summary": "聚焦测试通过",
                        "reference": "tests.test_agent_sessions",
                    }
                ],
            },
            updated_at_ms=400,
        )["workflow"]["goal"]
        completed_budget = self.store.agent_goal_continuation_budget(
            session_id,
            goal_id=goal_id,
            limit=4,
        )
        self.assertEqual(completed_budget["issuedCount"], 0)
        self.assertEqual(completed_budget["epoch"], 4)
        cleared = self.store.mutate_agent_goal(
            session_id,
            {"action": "clear", "expectedRevision": completed["revision"]},
            updated_at_ms=401,
        )["workflow"]["goal"]
        replacement = self.store.mutate_agent_goal(
            session_id,
            {
                "action": "confirm_setup",
                "confirmed": True,
                "expectedRevision": cleared["revision"],
                "objective": "新的持久 Goal",
            },
            updated_at_ms=402,
        )["workflow"]["goal"]
        replacement_budget = self.store.agent_goal_continuation_budget(
            session_id,
            goal_id=str(replacement["goalId"]),
            limit=4,
        )
        with self.assertRaisesRegex(ValueError, "another goal"):
            self.store.claim_agent_goal_continuation(
                session_id,
                goal_id=str(replacement["goalId"]),
                request_key="scope:1:attempt:1",
                limit=4,
                updated_at_ms=403,
            )
        self.assertFalse(cleared["configured"])
        self.assertNotEqual(replacement["goalId"], goal_id)
        self.assertEqual(
            replacement_budget,
            {"epoch": 1, "issuedCount": 0, "limit": 4, "remaining": 4},
        )

    def test_goal_continuation_budget_serializes_concurrent_claims(self) -> None:
        session_id = str(
            self.store.create(title="concurrent continuation")["id"]
        )
        goal_id = str(
            self.store.mutate_agent_goal(
                session_id,
                {
                    "action": "confirm_setup",
                    "confirmed": True,
                    "expectedRevision": 0,
                    "objective": "并发请求最多续投四次",
                },
            )["workflow"]["goal"]["goalId"]
        )

        def claim(index: int) -> dict[str, object]:
            return self.store.claim_agent_goal_continuation(
                session_id,
                goal_id=goal_id,
                request_key=f"concurrent-scope:{index}",
                limit=4,
                updated_at_ms=500 + index,
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(claim, range(8)))

        claimed = [result for result in results if result["claimed"] is True]
        blocked = [result for result in results if result["claimed"] is False]
        self.assertEqual(
            sorted(int(result["issuedCount"]) for result in claimed),
            [1, 2, 3, 4],
        )
        self.assertEqual(len(blocked), 4)
        self.assertTrue(
            all(
                result["reason"] == "goal_continuation_limit"
                for result in blocked
            )
        )
        self.assertEqual(
            self.store.agent_goal_continuation_budget(
                session_id,
                goal_id=goal_id,
                limit=4,
            ),
            {"epoch": 1, "issuedCount": 4, "limit": 4, "remaining": 0},
        )

    def test_unknown_mode_and_status_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "assistant or coordinator"):
            self.store.create(title="bad", mode="admin")
        session = self.store.create(title="safe", created_at_ms=100)
        with self.assertRaisesRegex(ValueError, "unsupported agent session status"):
            self.store.set_status(str(session["id"]), "executing_without_approval")
        with self.assertRaisesRegex(ValueError, "provider/model"):
            self.store.set_model_profile(str(session["id"]), "missing-provider")

    def test_global_approval_list_spans_sessions_without_losing_session_filter(self) -> None:
        first = self.store.create(title="first", created_at_ms=100)
        second = self.store.create(title="second", created_at_ms=200)
        first_approval = self.store.create_approval(
            session_id=str(first["id"]),
            tool_name="input",
            operation="apply_settings",
            payload_sha256="1" * 64,
            preview={"summary": "first"},
            risk_level="R1",
            requested_at_ms=1_000,
        )
        second_approval = self.store.create_approval(
            session_id=str(second["id"]),
            tool_name="runtime",
            operation="restart",
            payload_sha256="2" * 64,
            preview={"summary": "second"},
            risk_level="R2",
            requested_at_ms=2_000,
        )

        self.assertEqual(
            [item["approvalId"] for item in self.store.list_approvals(now_ms=2_500)],
            [second_approval["approvalId"], first_approval["approvalId"]],
        )
        self.assertEqual(
            [item["approvalId"] for item in self.store.list_approvals(session_id=str(first["id"]), now_ms=2_500)],
            [first_approval["approvalId"]],
        )

    def test_approval_is_hash_bound_expiring_and_receipted(self) -> None:
        session = self.store.create(title="approval", created_at_ms=100)
        session_id = str(session["id"])
        approval = self.store.create_approval(
            session_id=session_id,
            tool_name="input",
            operation="apply_settings",
            payload_sha256="a" * 64,
            preview={"summary": "关闭模糊音", "changes": [{"field": "pinyin.fuzzy", "after": False}]},
            risk_level="R1",
            requested_at_ms=1_000,
            ttl_ms=60_000,
        )
        self.assertEqual(approval["state"], "pending")
        self.assertEqual(self.store.list_approvals(session_id=session_id, now_ms=2_000), [approval])

        decided = self.store.decide_approval(
            str(approval["approvalId"]),
            approved=True,
            payload_sha256="a" * 64,
            decided_at_ms=2_000,
        )
        self.assertEqual(decided["state"], "approved")
        applied = self.store.complete_approval(
            str(approval["approvalId"]),
            state="applied",
            receipt={"auditId": 42, "rollbackAvailable": True},
        )
        self.assertEqual(applied["state"], "applied")
        self.assertEqual(applied["receipt"]["auditId"], 42)

        external = self.store.create_approval(
            session_id=session_id,
            tool_name="runtime",
            operation="restart_sidecar",
            payload_sha256="e" * 64,
            preview={"summary": "重启 Sidecar"},
            risk_level="R2",
            requested_at_ms=2_100,
        )
        external_decided = self.store.decide_approval(
            str(external["approvalId"]),
            approved=True,
            payload_sha256="e" * 64,
            decided_at_ms=2_200,
        )
        external_pending = self.store.complete_approval(
            str(external_decided["approvalId"]),
            state="external_pending",
            receipt={"externalActionPending": True, "externalAction": "restart_sidecar"},
        )
        self.assertEqual(external_pending["state"], "external_pending")
        external_applied = self.store.finalize_external_approval(
            str(external["approvalId"]),
            state="applied",
            receipt={"externalActionPending": False, "mutationApplied": True},
        )
        self.assertEqual(external_applied["state"], "applied")
        with self.assertRaisesRegex(ValueError, "cannot complete from state applied"):
            self.store.finalize_external_approval(
                str(external["approvalId"]),
                state="failed",
                receipt={"mutationApplied": False},
            )

        stale = self.store.create_approval(
            session_id=session_id,
            tool_name="input",
            operation="apply_settings",
            payload_sha256="b" * 64,
            preview={"summary": "变更已过期"},
            risk_level="R1",
            requested_at_ms=3_000,
        )
        with self.assertRaisesRegex(ValueError, "stale"):
            self.store.decide_approval(
                str(stale["approvalId"]),
                approved=True,
                payload_sha256="c" * 64,
                decided_at_ms=4_000,
            )
        self.assertEqual(self.store.get_approval(str(stale["approvalId"]), now_ms=4_000)["state"], "stale")

        expired = self.store.create_approval(
            session_id=session_id,
            tool_name="runtime",
            operation="restart",
            payload_sha256="d" * 64,
            preview={"summary": "重启运行组件"},
            risk_level="R2",
            requested_at_ms=5_000,
            ttl_ms=1_000,
        )
        self.assertEqual(self.store.get_approval(str(expired["approvalId"]), now_ms=6_000)["state"], "expired")
        with self.assertRaisesRegex(ValueError, "already expired"):
            self.store.decide_approval(
                str(expired["approvalId"]),
                approved=True,
                payload_sha256="d" * 64,
                decided_at_ms=6_001,
            )

    def test_approval_tool_call_binding_is_durable_and_immutable(self) -> None:
        session = self.store.create(title="approval identity", created_at_ms=100)
        approval = self.store.create_approval(
            session_id=str(session["id"]),
            tool_name="workspace_shell",
            operation="run",
            payload_sha256="a" * 64,
            preview={"summary": "运行命令"},
            risk_level="R3",
            requested_at_ms=1_000,
        )

        bound = self.store.bind_approval_tool_call(
            str(approval["approvalId"]),
            tool_call_id="tool:workspace-shell:1",
        )
        self.assertEqual(bound["toolCallId"], "tool:workspace-shell:1")
        self.assertEqual(
            self.store.get_approval(str(approval["approvalId"]))["toolCallId"],
            "tool:workspace-shell:1",
        )
        self.assertEqual(
            self.store.bind_approval_tool_call(
                str(approval["approvalId"]),
                tool_call_id="tool:workspace-shell:1",
            )["toolCallId"],
            "tool:workspace-shell:1",
        )
        with self.assertRaisesRegex(ValueError, "another tool call"):
            self.store.bind_approval_tool_call(
                str(approval["approvalId"]),
                tool_call_id="tool:workspace-shell:2",
            )


class GoalCompletionAuthorityGateTests(unittest.TestCase):
    """Goal complete is a terminal authority receipt, not a prose claim.

    It must not succeed while Room WorkItems this Session is responsible for
    are still open without an explicit blocked/failed/cancelled reconciliation,
    or while the Root WorkDocument bound to the Goal sits in a broken ``error``
    lifecycle that cannot accept the terminal receipt.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-goal-gate-")
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "rag-ime.sqlite"
        self.store = AgentSessionStore(self.db_path)
        self.store.initialize()
        self.facilitator = self.store.create(title="Room facilitator")
        self.worker = self.store.create(title="Room implementer")
        self.rooms = AgentRoomStore(self.db_path, room_dir=self.root / "rooms")
        self.rooms.initialize()
        self.room = self.rooms.create(
            title="完成权威门",
            routing_policy="moderator",
            participants=[
                {
                    "sessionId": self.facilitator["id"],
                    "roleId": "coordinator",
                    "roleVersion": "1",
                    "displayName": "协调者",
                    "collaborationRole": "coordinator",
                },
                {
                    "sessionId": self.worker["id"],
                    "roleId": "worker",
                    "roleVersion": "1",
                    "displayName": "实施者",
                    "collaborationRole": "implementer",
                },
            ],
        )
        self.facilitator_participant = str(self.room["participants"][0]["id"])
        self.worker_participant = str(self.room["participants"][1]["id"])
        self.work = AgentRoomWorkStore(self.db_path)
        self.work.initialize()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _confirm_goal(self, session_id: str) -> dict[str, object]:
        return self.store.mutate_agent_goal(
            session_id,
            {
                "action": "confirm_setup",
                "confirmed": True,
                "expectedRevision": 0,
                "objective": "交付 Room 请求并验收全部委派工作",
            },
        )["workflow"]["goal"]

    def _complete(self, session_id: str, revision: object) -> dict[str, object]:
        return self.store.mutate_agent_goal(
            session_id,
            {
                "action": "complete",
                "expectedRevision": revision,
                "summary": "全部委派工作均已验收",
                "evidence": [
                    {
                        "kind": "test",
                        "summary": "聚焦测试通过",
                        "reference": "tests.test_agent_sessions",
                    }
                ],
            },
        )["workflow"]["goal"]

    def _create_item(self, *, state: str, client_message_id: str, owner: str = "", creator: str = "", accountable: str = "") -> dict[str, object]:
        return self.work.create(
            room_id=str(self.room["id"]),
            objective="实现一个受验收约束的变更",
            expected_output="通过验收的变更与证据",
            current_owner_participant_id=owner or self.worker_participant,
            created_by_participant_id=creator or self.facilitator_participant,
            accountable_participant_id=accountable or self.facilitator_participant,
            client_message_id=client_message_id,
            acceptance_criteria=["变更可运行且满足需求"],
            state=state,
        )

    def test_goal_complete_rejects_open_delegated_work_items(self) -> None:
        session_id = str(self.facilitator["id"])
        goal = self._confirm_goal(session_id)
        item = self._create_item(state="active", client_message_id="work:gate:open")

        with self.assertRaises(AgentGoalExecutionBlocked) as blocked:
            self._complete(session_id, goal["revision"])
        self.assertEqual(blocked.exception.error_code, "goal_open_work_items")
        self.assertEqual(self.store.agent_goal(session_id)["status"], "active")

        self.work.block(
            str(self.worker["id"]),
            {
                "workId": item["id"],
                "reason": "上游接口尚未冻结",
                "nextStep": "等待接口冻结后继续",
            },
        )
        completed = self._complete(session_id, goal["revision"])
        self.assertEqual(completed["status"], "completed")

    def test_goal_complete_rejects_review_items_until_explicit_verdict(self) -> None:
        session_id = str(self.facilitator["id"])
        goal = self._confirm_goal(session_id)
        item = self._create_item(state="review", client_message_id="work:gate:review")

        with self.assertRaises(AgentGoalExecutionBlocked) as blocked:
            self._complete(session_id, goal["revision"])
        self.assertEqual(blocked.exception.error_code, "goal_open_work_items")

        accepted = self.work.accept(
            session_id,
            {
                "workId": item["id"],
                "expectedRevision": item["revision"],
                "operabilityVerdict": "passed",
                "requirementVerdict": "satisfied",
                "evidenceRefs": ["test:tests.test_agent_sessions"],
                "reason": "实现可运行且满足验收标准。",
            },
        )
        self.assertEqual(accepted["state"], "done")
        completed = self._complete(session_id, goal["revision"])
        self.assertEqual(completed["status"], "completed")

    def test_goal_complete_ignores_reconciled_and_unowned_work_items(self) -> None:
        session_id = str(self.facilitator["id"])
        goal = self._confirm_goal(session_id)
        for index, state in enumerate(("blocked", "done", "failed", "cancelled")):
            self._create_item(
                state=state,
                client_message_id=f"work:gate:terminal:{index}",
            )
        # An open item that this Session neither owns, created, nor is
        # accountable for is another participant's responsibility.
        self._create_item(
            state="queued",
            client_message_id="work:gate:other",
            owner=self.worker_participant,
            creator=self.worker_participant,
            accountable=self.worker_participant,
        )

        completed = self._complete(session_id, goal["revision"])
        self.assertEqual(completed["status"], "completed")

    def test_goal_cancel_stays_available_while_work_items_are_open(self) -> None:
        session_id = str(self.facilitator["id"])
        goal = self._confirm_goal(session_id)
        self._create_item(state="active", client_message_id="work:gate:cancel")

        cancelled = self.store.mutate_agent_goal(
            session_id,
            {
                "action": "cancel",
                "expectedRevision": goal["revision"],
                "reason": "用户改变了优先级，显式放弃剩余委派工作。",
            },
        )["workflow"]["goal"]
        self.assertEqual(cancelled["status"], "cancelled")

    def test_goal_complete_rejects_error_state_root_work_document(self) -> None:
        session_id = str(self.facilitator["id"])
        goal = self._confirm_goal(session_id)
        workspace = self.root / "workspace"
        (workspace / "docs").mkdir(parents=True)
        (workspace / "docs" / "root.md").write_text(
            "# Root WorkDocument\n",
            encoding="utf-8",
        )
        documents = WorkDocumentService(
            self.db_path,
            sessions=self.store,
            context_runtime=AgentContextRuntime(self.db_path),
        )
        documents.initialize()
        registered = documents.register(
            {
                "authorityKind": "session_goal",
                "authorityId": goal["goalId"],
                "authorityRevision": goal["revision"],
                "workspaceRoot": str(workspace),
                "sourcePath": "docs/root.md",
                "title": "Root WorkDocument",
            }
        )
        self.assertEqual(registered["document"]["state"], "active")
        authority_key = f"session_goal:{goal['goalId']}"
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE work_documents SET state = 'error', error = ? "
                "WHERE authority_key = ?",
                ("simulated reconciler crash", authority_key),
            )
            conn.commit()

        with self.assertRaises(AgentGoalExecutionBlocked) as blocked:
            self._complete(session_id, goal["revision"])
        self.assertEqual(
            blocked.exception.error_code,
            "goal_root_work_document_error",
        )
        self.assertEqual(self.store.agent_goal(session_id)["status"], "active")

        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE work_documents SET state = 'active', error = '' "
                "WHERE authority_key = ?",
                (authority_key,),
            )
            conn.commit()
        completed = self._complete(session_id, goal["revision"])
        self.assertEqual(completed["status"], "completed")


if __name__ == "__main__":
    unittest.main()
