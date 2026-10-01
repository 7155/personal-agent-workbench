from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rag_ime.agent_service import AgentService
from rag_ime.pi.config import PiRuntimeConfig


class AgentApprovalExactlyOnceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="rag-ime-agent-approval-exactly-once-"
        )
        self.root = Path(self.temporary.name)
        self.db_path = self.root / "rag-ime.sqlite"
        self.services: list[AgentService] = []

    def tearDown(self) -> None:
        for service in reversed(self.services):
            service.close()
        self.temporary.cleanup()

    def _service(
        self,
        *,
        suffix: str,
        defer_startup_recovery: bool = False,
    ) -> AgentService:
        service = AgentService(
            db_path=self.db_path,
            runtime_config=PiRuntimeConfig(
                enabled=False,
                executable=None,
                agent_dir=self.root / f"agent-config-{suffix}",
                session_dir=self.root / f"sessions-{suffix}",
                logs_dir=self.root / f"logs-{suffix}",
            ),
            defer_startup_recovery=defer_startup_recovery,
        )
        self.services.append(service)
        return service

    def _automatic_approval(
        self,
        service: AgentService,
        *,
        suffix: str,
    ) -> tuple[str, dict[str, object]]:
        session = service.create_session(
            {"title": f"automatic effect {suffix}"}
        )["session"]
        session_id = str(session["id"])
        service.update_session(
            session_id,
            {
                "mode": "coordinator",
                "executionMode": "full_trust",
                "toolProfileVersion": "control-center-auto-approve-v1",
                "workspaceRoots": [self.root.as_posix()],
            },
        )
        approval = service.sessions.create_approval(
            session_id=session_id,
            tool_name="workspace_shell",
            operation="run",
            payload_sha256=hashlib.sha256(suffix.encode("utf-8")).hexdigest(),
            preview={"title": "apply one effect", "summary": suffix},
            risk_level="R2",
            causal_metadata={"turnId": f"turn:{suffix}"},
        )
        approval = service.sessions.bind_approval_tool_call(
            str(approval["approvalId"]),
            tool_call_id=f"tool:{suffix}",
        )
        return session_id, approval

    def _room_bound_approval(
        self,
        service: AgentService,
        *,
        suffix: str,
    ) -> tuple[dict[str, str], dict[str, object]]:
        room = service.create_room(
            {
                "title": f"Room terminal retention {suffix}",
                "workspaceRoots": [self.root.as_posix()],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        participant = room["participants"][0]
        session_id = str(participant["sessionId"])
        root_id = f"root:{suffix}"
        dispatch_id = f"dispatch:{suffix}"
        turn_id = f"turn:{suffix}"
        tool_call_id = f"tool:{suffix}"
        service.sessions.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id=f"pi:{suffix}",
        )
        service.room_turns.begin(
            session_id,
            root_id,
            dispatch_id=dispatch_id,
        )
        service.room_turns.accept(session_id, turn_id, root_id)
        room_context = service._active_room_dispatch_context(session_id)
        self.assertIsNotNone(room_context)
        approval = service.sessions.create_approval(
            session_id=session_id,
            tool_name="workspace_shell",
            operation="run",
            payload_sha256=hashlib.sha256(suffix.encode("utf-8")).hexdigest(),
            preview={"title": "one retained effect", "summary": suffix},
            risk_level="R2",
            causal_metadata={"turnId": turn_id, "roomBound": True},
        )
        approval_id = str(approval["approvalId"])
        service.sessions.bind_approval_tool_call(
            approval_id,
            tool_call_id=tool_call_id,
        )
        approval = service.sessions.bind_approval_room_dispatch(
            approval_id,
            room_context=room_context or {},
        )
        return (
            {
                "approvalId": approval_id,
                "sessionId": session_id,
                "roomId": str(room["id"]),
                "rootId": root_id,
                "dispatchId": dispatch_id,
                "turnId": turn_id,
                "toolCallId": tool_call_id,
            },
            approval,
        )

    def test_executor_success_followed_by_terminal_write_failure_is_unknown_and_never_replayed(
        self,
    ) -> None:
        service = self._service(suffix="write-failure")
        _session_id, approval = self._automatic_approval(
            service,
            suffix="write-failure",
        )
        effect_count = 0

        def apply_effect(decided: dict[str, object]) -> dict[str, object]:
            nonlocal effect_count
            effect_count += 1
            return {
                "schemaVersion": "rag-ime.agent-operation-receipt.v1",
                "approvalId": decided["approvalId"],
                "toolId": decided["toolId"],
                "operation": decided["operation"],
                "mutationApplied": True,
                "summary": "external effect completed",
            }

        service.bind_approval_executor(apply_effect)
        original_complete = service.sessions.complete_approval
        completion_attempts = 0

        def fail_first_completion(
            *args: object,
            **kwargs: object,
        ) -> dict[str, object]:
            nonlocal completion_attempts
            completion_attempts += 1
            if completion_attempts == 1:
                raise sqlite3.OperationalError("simulated terminal write failure")
            return original_complete(*args, **kwargs)

        with patch.object(
            service.sessions,
            "complete_approval",
            side_effect=fail_first_completion,
        ):
            result = service.auto_approve_pending(approval)

        stored = service.sessions.get_approval(str(approval["approvalId"]))
        receipt = stored["receipt"]
        self.assertEqual(effect_count, 1)
        self.assertEqual(stored["state"], "failed")
        self.assertIsInstance(receipt, dict)
        self.assertIs(receipt.get("effectMayHaveOccurred"), True)
        self.assertEqual(receipt.get("executionOutcome"), "unknown")
        self.assertIs(receipt.get("replayAllowed"), False)
        self.assertIsNot(receipt.get("mutationApplied"), False)
        self.assertNotIn("原操作没有执行", str(receipt.get("summary") or ""))
        self.assertEqual(result["receipt"], receipt)

        repeated = service.auto_approve_pending(approval)
        self.assertEqual(effect_count, 1)
        self.assertEqual(repeated["receipt"], receipt)

    def test_terminal_branch_cursor_does_not_stale_another_room_approval(self) -> None:
        service = self._service(suffix="cursor-generation")
        binding, pending = self._room_bound_approval(
            service,
            suffix="cursor-generation",
        )
        runtime_binding = service.sessions.runtime_binding(binding["sessionId"])
        self.assertIsNotNone(runtime_binding)
        assert runtime_binding is not None
        service.sessions.advance_runtime_branch_cursor(
            binding["sessionId"],
            branch_anchor="terminal-settlement",
            expected_generation=int(runtime_binding["generation"]),
            expected_external_session_id=str(runtime_binding["externalSessionId"]),
            expected_transcript_ref=str(runtime_binding["transcriptRef"]),
            expected_branch_anchor=str(runtime_binding["branchAnchor"]),
        )
        unchanged = service.sessions.runtime_binding(binding["sessionId"])
        self.assertIsNotNone(unchanged)
        assert unchanged is not None
        self.assertEqual(unchanged["generation"], runtime_binding["generation"])
        self.assertEqual(unchanged["branchAnchor"], "terminal-settlement")

        decided = service.sessions.decide_approval(
            binding["approvalId"],
            approved=True,
            payload_sha256=str(pending["payloadSha256"]),
            decided_by="execution-policy:room_unrestricted",
        )
        claimed = service.sessions.claim_approval_execution(
            binding["approvalId"],
            room_context=service._active_room_dispatch_context(
                binding["sessionId"]
            ),
        )
        self.assertEqual(decided["state"], "approved")
        self.assertEqual(claimed["state"], "approved")

    def test_pruned_runtime_terminal_uses_unpruned_projection_authority_after_restart(
        self,
    ) -> None:
        service = self._service(suffix="retention-before")
        binding, pending = self._room_bound_approval(
            service,
            suffix="retention",
        )
        decided = service.sessions.decide_approval(
            binding["approvalId"],
            approved=True,
            payload_sha256=str(pending["payloadSha256"]),
            decided_by="execution-policy:room_unrestricted",
        )
        claimed = service.sessions.claim_approval_execution(
            binding["approvalId"],
            room_context=service._active_room_dispatch_context(
                binding["sessionId"]
            ),
        )
        self.assertEqual(claimed["state"], "approved")
        terminal = service.sessions.complete_approval(
            binding["approvalId"],
            state="applied",
            receipt={
                "schemaVersion": "rag-ime.agent-operation-receipt.v1",
                "approvalId": binding["approvalId"],
                "toolId": "workspace_shell",
                "operation": "run",
                "mutationApplied": True,
                "summary": "effect applied once",
            },
        )
        self.assertEqual(decided["state"], "approved")
        service.approval_application._project_recovered_session_execution(
            terminal,
            include_approval=True,
            include_tool=True,
        )
        self.assertTrue(service.events.flush())

        with sqlite3.connect(self.db_path) as conn:
            markers = conn.execute(
                "SELECT projection_kind, tool_call_id, turn_id, room_id, "
                "room_root_id, room_dispatch_id, room_generation, event_id "
                "FROM agent_approval_terminal_projections "
                "WHERE session_id = ? AND approval_id = ?",
                (binding["sessionId"], binding["approvalId"]),
            ).fetchall()
        self.assertEqual({row[0] for row in markers}, {"approval_resolved", "tool_finished"})
        self.assertTrue(all(row[1] == binding["toolCallId"] for row in markers))
        self.assertTrue(all(row[2] == binding["turnId"] for row in markers))
        self.assertTrue(all(row[3] == binding["roomId"] for row in markers))
        self.assertTrue(all(row[4] == binding["rootId"] for row in markers))
        self.assertTrue(all(row[5] == binding["dispatchId"] for row in markers))
        self.assertTrue(all(row[6] == 1 for row in markers))

        # Force the original Tool terminal out of the bounded in-memory cache.
        # The independent SQLite authority still returns its original identity
        # and suppresses both a second durable row and a second live projection.
        service.events._terminal_cache_limit = 1
        service.events.publish(
            binding["sessionId"],
            "tool_finished",
            {"toolCallId": "tool:unrelated", "toolName": "overview"},
            turn_id="turn:unrelated",
        )
        sequence_before_duplicate = service.sessions.max_event_sequence(
            binding["sessionId"]
        )
        duplicate = service.events.publish(
            binding["sessionId"],
            "tool_finished",
            {
                "approvalId": binding["approvalId"],
                "toolCallId": binding["toolCallId"],
                "toolName": "workspace_shell",
                "state": "applied",
                "status": "completed",
            },
            turn_id=binding["turnId"],
        )
        tool_marker = next(row for row in markers if row[0] == "tool_finished")
        self.assertEqual(duplicate.event_id, tool_marker[7])
        self.assertEqual(
            service.sessions.max_event_sequence(binding["sessionId"]),
            sequence_before_duplicate,
        )

        start_sequence = service.sessions.max_event_sequence(
            binding["sessionId"]
        )
        for offset in range(140):
            sequence = start_sequence + offset + 1
            service.sessions.record_runtime_event(
                event_id=f"{binding['sessionId']}:{sequence}",
                session_id=binding["sessionId"],
                turn_id=f"turn:noise:{offset}",
                sequence=sequence,
                event_type="status_changed",
                created_at_ms=10_000 + offset,
                retain_per_session=100,
            )

        with sqlite3.connect(self.db_path) as conn:
            retained_terminals = conn.execute(
                "SELECT COUNT(*) FROM agent_runtime_events "
                "WHERE session_id = ? AND event_type IN "
                "('approval_resolved', 'tool_finished')",
                (binding["sessionId"],),
            ).fetchone()[0]
        self.assertEqual(retained_terminals, 0)
        self.assertTrue(
            service.sessions.has_runtime_approval_resolution(
                binding["sessionId"], binding["approvalId"]
            )
        )
        self.assertTrue(
            service.sessions.has_runtime_tool_terminal(
                binding["sessionId"],
                binding["toolCallId"],
                turn_id=binding["turnId"],
                tool_name="workspace_shell",
            )
        )
        service.close()
        self.services.remove(service)

        restarted = self._service(
            suffix="retention-after",
            defer_startup_recovery=True,
        )
        before_recovery = restarted.sessions.max_event_sequence(
            binding["sessionId"]
        )
        with patch.object(
            restarted,
            "_approval_executor",
            side_effect=AssertionError("restart replayed the Tool effect"),
        ):
            restarted.run_startup_recovery()
        self.assertTrue(restarted.events.flush())
        self.assertEqual(
            restarted.sessions.max_event_sequence(binding["sessionId"]),
            before_recovery,
        )
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM agent_runtime_events "
                    "WHERE session_id = ? AND event_type IN "
                    "('approval_resolved', 'tool_finished')",
                    (binding["sessionId"],),
                ).fetchone()[0],
                0,
            )

    def test_real_session_delete_releases_terminal_dedupe_state(self) -> None:
        service = self._service(suffix="delete")
        session_id, approval = self._automatic_approval(
            service,
            suffix="delete",
        )
        service.events.publish(
            session_id,
            "approval_resolved",
            {"approvalId": approval["approvalId"], "state": "failed"},
            turn_id="turn:delete",
        )
        service.events.publish(
            session_id,
            "tool_finished",
            {"toolCallId": approval["toolCallId"]},
            turn_id="turn:delete",
        )
        self.assertTrue(
            any(key[0] == session_id for key in service.events._approval_event_cache)
        )
        self.assertTrue(
            any(
                key[0] == session_id
                for key in service.events._tool_terminal_event_cache
            )
        )

        service.delete_session(session_id)

        self.assertFalse(
            any(key[0] == session_id for key in service.events._approval_event_cache)
        )
        self.assertFalse(
            any(
                key[0] == session_id
                for key in service.events._tool_terminal_event_cache
            )
        )
        self.assertNotIn(session_id, service.events._events)
        self.assertNotIn(session_id, service.events._sequences)


if __name__ == "__main__":
    unittest.main()
