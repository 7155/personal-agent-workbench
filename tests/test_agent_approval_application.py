from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from rag_ime.agent_approval_application import AgentApprovalApplicationService, ApprovalRuntime
from rag_ime.agent_approval_model import ApprovalModelArbiter
from rag_ime.agent_events import AgentEventHub
from rag_ime.agent_memory_sources import AgentMemorySourceStore
from rag_ime.rooms.turn_registry import RoomTurnRegistry
from rag_ime.rooms.store import AgentRoomEventHub
from tests import test_agent_room_work as fixtures


class ApprovalApplicationTests(unittest.TestCase):
    tearDown = fixtures.AgentRoomWorkTests.tearDown

    def setUp(self):
        key_patch = patch("rag_ime.agent_approval_model._jev_api_key", return_value="")
        key_patch.start()
        self.addCleanup(key_patch.stop)
        fixtures.AgentRoomWorkTests.setUp(self)
        self.runtime = Mock(spec=ApprovalRuntime)
        self.runtime.has_pending_approval.return_value = True
        self.events = AgentEventHub()
        self.addCleanup(self.events.close)
        self.memory = AgentMemorySourceStore(self.db_path, project="test")
        self.memory.initialize()
        self.app = AgentApprovalApplicationService(
            sessions=self.sessions, events=self.events, rooms=self.rooms,
            room_events=AgentRoomEventHub(self.rooms), room_turns=RoomTurnRegistry(),
            memory_sources=self.memory,
            approval_model=ApprovalModelArbiter(self.db_path, runtime_provider=lambda: self.runtime),
            runtime_provider=lambda: self.runtime, executor_provider=lambda: None,
            process_id_provider=lambda: 123,
            record_tool_receipt_evidence=lambda _: {},
            active_room_dispatch_context=lambda _: None,
        )

    def test_rejects_with_real_store_and_notifies_current_runtime_after_replacement(self):
        session_id = str(self.worker["id"])
        approval = self.sessions.create_approval(
            session_id=session_id, tool_name="workspace_write_file", operation="write",
            payload_sha256="a" * 64, preview={"path": "README.md"}, risk_level="R2", ttl_ms=120000,
        )
        original = self.runtime
        self.runtime = Mock(spec=ApprovalRuntime)
        self.runtime.has_pending_approval.return_value = True
        result = self.app.decide_approval(str(approval["approvalId"]), {"decision": "reject", "payloadSha256": "a" * 64})
        self.assertEqual(result["approval"]["state"], "rejected")
        self.assertTrue(self.runtime.resolve_approval.called)
        self.assertFalse(original.resolve_approval.called)
        self.assertIs(self.app.external.sessions, self.sessions)
        self.assertIs(self.app.external.events, self.events)
        self.assertEqual(len(self.app.list_approvals({"sessionId": session_id})["items"]), 1)
        self.assertFalse(hasattr(self.app, "host"))
        self.assertFalse(hasattr(self.app.external, "host"))

    def test_external_finalizer_validates_persisted_state_before_process_or_effects(self):
        approval = self.sessions.create_approval(
            session_id=str(self.worker["id"]), tool_name="runtime", operation="restart_sidecar",
            payload_sha256="a" * 64, preview={}, risk_level="R2", ttl_ms=120000,
        )
        with self.assertRaisesRegex(ValueError, "not waiting"):
            self.app.external.finalize(str(approval["approvalId"]), {})
        self.assertEqual(self.sessions.get_approval(str(approval["approvalId"]))["state"], "pending")
        self.assertFalse(self.runtime.resolve_approval.called)

    def test_tool_call_result_recovers_existing_applied_receipt_without_executing(self):
        session_id = str(self.worker["id"])
        with self.sessions.approval_creation_scope(session_id=session_id, tool_call_id="call:lost-response"):
            approval = self.sessions.create_approval(
                session_id=session_id, tool_name="workspace_write", operation="apply",
                payload_sha256="a" * 64, preview={}, risk_level="R2",
            )
        approval_id = str(approval["approvalId"])
        self.sessions.decide_approval(approval_id, approved=True, payload_sha256="a" * 64)
        receipt = {"mutationApplied": True, "summary": "Written once", "postimageSha256": "b" * 64}
        self.sessions.complete_approval(approval_id, state="applied", receipt=receipt)

        with patch.object(self.app, "execute_approved") as execute, patch.object(
            self.sessions, "get_approval", side_effect=AssertionError("lookup must not expire or mutate approvals")
        ):
            result = self.app.approval_result({
                "sessionId": session_id, "toolCallId": "call:lost-response", "tool": "workspace_write",
            })
        self.assertEqual(result["lookupState"], "applied")
        self.assertEqual(result["approval"]["approvalId"], approval_id)
        self.assertEqual(result["approval"]["receipt"], receipt)
        execute.assert_not_called()
        self.runtime.resolve_approval.assert_not_called()

    def test_tool_call_result_is_exact_and_does_not_guess_missing_or_duplicate_bindings(self):
        session_id = str(self.worker["id"])
        payload = {"sessionId": session_id, "toolCallId": "call:exact", "tool": "workspace_write"}
        self.assertEqual(self.app.approval_result(payload)["lookupState"], "not_found")
        for index in range(2):
            with self.sessions.approval_creation_scope(session_id=session_id, tool_call_id="call:exact"):
                self.sessions.create_approval(
                    session_id=session_id, tool_name="workspace_write", operation="apply",
                    payload_sha256="a" * 64, preview={}, risk_level="R2",
                )
            if index == 0:
                self.assertEqual(self.app.approval_result(payload)["lookupState"], "pending")
                with self.assertRaisesRegex(ValueError, "tool does not match"):
                    self.app.approval_result({**payload, "tool": "workspace_shell"})
                other = self.sessions.create(title="other session")
                foreign = self.app.approval_result({**payload, "sessionId": other["id"]})
                self.assertEqual(foreign["lookupState"], "not_found")
                self.assertNotIn("approval", foreign)
        duplicate = self.app.approval_result(payload)
        self.assertEqual(duplicate["lookupState"], "ambiguous")
        self.assertNotIn("approval", duplicate)

    def test_tool_call_result_does_not_expire_pending_approval_or_mix_identity_modes(self):
        session_id = str(self.worker["id"])
        with self.sessions.approval_creation_scope(session_id=session_id, tool_call_id="call:old"):
            approval = self.sessions.create_approval(
                session_id=session_id, tool_name="workspace_write", operation="apply",
                payload_sha256="a" * 64, preview={}, risk_level="R2", requested_at_ms=1, ttl_ms=1000,
            )
        payload = {"sessionId": session_id, "toolCallId": "call:old", "tool": "workspace_write"}
        self.assertEqual(self.app.approval_result(payload)["approval"]["state"], "pending")
        with self.assertRaisesRegex(ValueError, "one lookup identity"):
            self.app.approval_result({**payload, "approvalId": approval["approvalId"]})

    def test_full_access_applies_without_human_or_model_decision(self):
        session_id = str(self.worker["id"])
        self.sessions.set_runtime_policy(
            session_id, mode="coordinator",
            tool_profile_version="control-center-full-access-v1",
            execution_mode="per_action", workspace_roots=["/"], allowed_tools=None,
        )
        approval = self.sessions.create_approval(
            session_id=session_id, tool_name="planning", operation="task_action",
            payload_sha256="a" * 64, preview={}, risk_level="R3", ttl_ms=120000,
        )
        executor = Mock(return_value={"mutationApplied": True, "summary": "Applied"})
        self.app._executor_provider = lambda: executor
        with patch.object(self.app.approval_model, "decide") as decide:
            result = self.app.auto_approve_pending(approval)

        decide.assert_not_called()
        executor.assert_called_once()
        self.assertFalse(result["approvalRequired"])
        self.assertTrue(result["autoApproved"])
        self.assertEqual(result["decisionMode"], "policy")
        self.assertEqual(result["approval"]["state"], "applied")
        self.assertEqual(result["approval"]["decidedBy"], "execution-policy:full_access")
