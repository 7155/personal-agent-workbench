from __future__ import annotations

import unittest

from rag_ime.agent_event_projection import (
    AgentEventProjectionService,
    room_event_projection,
    runtime_event_metrics,
)
from rag_ime.agent_protocol import AgentEventEnvelope
from rag_ime.pi.event_projection import tool_event_payload


class _Sessions:
    def __init__(self) -> None:
        self.event_types: list[str] = []
        self.runtime_events: list[dict[str, object]] = []

    def record_runtime_event(self, **values: object) -> None:
        self.event_types.append(str(values["event_type"]))
        self.runtime_events.append(dict(values))


class _Rooms:
    def participant_for_session(self, _session_id: str) -> dict[str, object]:
        return {"id": "participant:1", "roomId": "room:1"}


class _Observations:
    def enqueue_agent_event(self, _event: object, *, room_id: str) -> None:
        if room_id != "room:1":
            raise AssertionError(room_id)


class _RoomEvents:
    def __init__(self) -> None:
        self.items: list[dict[str, object]] = []

    def publish(self, **values: object) -> None:
        self.items.append(dict(values))

    def publish_child_terminal(
        self, *, runtime_event_id: str, dispatch_id: str, **values: object,
    ) -> None:
        self.publish(**values)


class _Turns:
    def __init__(self, *, child: bool = False) -> None:
        self.child = child

    def private_intercom_for_event(self, _event: object) -> str:
        return ""

    def registered_turn_for_event(self, _event: object) -> str:
        return "room-turn:1"

    def turn_for_event(self, _event: object) -> str:
        return "room-turn:1"

    def is_cancelled(self, _session_id: str, _turn_id: str) -> bool:
        return False

    def dispatch_for_event(self, _event: object) -> str:
        return "room-child:1" if self.child else ""

    def work_identity_for_event(self, _event: object) -> dict[str, object]:
        if not self.child:
            return {}
        return {
            "workItemId": "work:1",
            "workItemRevision": 2,
            "attemptId": "attempt:2",
        }

    def child_for_event(self, _event: object) -> bool:
        return self.child

    def topic_for_turn(self, _turn_id: str) -> str:
        return "topic:1"

    def finish(self, *_args: object) -> None:
        return None


class AgentEventProjectionTests(unittest.TestCase):
    def _service(
        self,
        *,
        child: bool = False,
    ) -> tuple[AgentEventProjectionService, _Sessions, _RoomEvents]:
        sessions = _Sessions()
        room_events = _RoomEvents()
        return (
            AgentEventProjectionService(
                sessions=sessions,
                rooms=_Rooms(),
                agent_blocks=None,
                observations=_Observations(),
                room_events=room_events,
                room_turns=_Turns(child=child),
                append_recent_message=lambda *_args: None,
                record_assistant_evidence=lambda _event: {},
                notify_intercom=lambda: None,
            ),
            sessions,
            room_events,
        )

    def test_transient_fragments_stay_out_of_durable_runtime_events(self) -> None:
        service, sessions, _room_events = self._service()
        for sequence, event_type in enumerate(("text_delta", "tool_progress", "turn_completed"), start=1):
            service.record(AgentEventEnvelope(
                event_id=f"event:{sequence}",
                session_id="session:1",
                turn_id="turn:1",
                sequence=sequence,
                created_at_ms=sequence,
                event_type=event_type,
                payload={},
                resume_token=f"event:{sequence}",
            ))
        self.assertEqual(sessions.event_types, ["turn_completed"])

    def test_room_projection_uses_the_public_session_event_stream(self) -> None:
        service, _sessions, room_events = self._service()
        event = AgentEventEnvelope(
            event_id="event:1",
            session_id="session:1",
            turn_id="turn:1",
            sequence=1,
            created_at_ms=1,
            event_type="text_delta",
            payload={
                "messageId": "message:1",
                "delta": "你好",
                "sourceLoopId": "pi:message:assistant:101",
            },
            resume_token="event:1",
        )
        service.mirror_to_room(event)
        self.assertEqual(room_events.items[0]["event_type"], "participant_delta")
        self.assertEqual(room_events.items[0]["turn_id"], "room-turn:1")
        self.assertEqual(
            room_events.items[0]["payload"]["data"]["sourceTurnId"],
            "turn:1",
        )
        self.assertEqual(
            room_events.items[0]["payload"]["data"]["sourceLoopId"],
            "pi:message:assistant:101",
        )

    def test_room_projection_keeps_the_hash_needed_for_inline_approval(self) -> None:
        event = AgentEventEnvelope(
            event_id="event:approval",
            session_id="session:1",
            turn_id="turn:1",
            sequence=1,
            created_at_ms=1,
            event_type="approval_required",
            payload={
                "approvalId": "approval:1",
                "payloadSha256": "a" * 64,
                "toolId": "workspace_write",
                "operation": "apply",
                "state": "pending",
            },
            resume_token="event:approval",
        )

        event_type, payload = room_event_projection(event)

        self.assertEqual(event_type, "participant_activity")
        self.assertEqual(payload["approvalId"], "approval:1")
        self.assertEqual(payload["payloadSha256"], "a" * 64)

    def test_room_projection_coalesces_micro_deltas_before_terminal(self) -> None:
        service, _sessions, room_events = self._service()
        for sequence in range(1, 201):
            service.mirror_to_room(AgentEventEnvelope(
                event_id=f"event:{sequence}",
                session_id="session:1",
                turn_id="turn:1",
                sequence=sequence,
                created_at_ms=sequence,
                event_type="text_delta",
                payload={"messageId": "message:1", "delta": "字"},
                resume_token=f"event:{sequence}",
            ))
        service.mirror_to_room(AgentEventEnvelope(
            event_id="event:201",
            session_id="session:1",
            turn_id="turn:1",
            sequence=201,
            created_at_ms=201,
            event_type="turn_completed",
            payload={"status": "completed"},
            resume_token="event:201",
        ))

        deltas = [
            item for item in room_events.items
            if item["event_type"] == "participant_delta"
        ]
        self.assertLess(len(deltas), 20)
        self.assertEqual(
            "".join(str(item["payload"]["data"]["delta"]) for item in deltas),
            "字" * 200,
        )
        self.assertEqual(room_events.items[-1]["event_type"], "turn_completed")

    def test_tool_failure_maps_to_a_terminal_public_activity(self) -> None:
        event = AgentEventEnvelope(
            event_id="event:tool",
            session_id="session:1",
            turn_id="turn:1",
            sequence=1,
            created_at_ms=1,
            event_type="tool_finished",
            payload={"toolName": "read", "isError": True, "status": "failed"},
            resume_token="event:tool",
        )
        event_type, payload = room_event_projection(event)
        self.assertEqual(event_type, "participant_activity")
        self.assertTrue(payload["isError"])
        self.assertEqual(payload["status"], "failed")

    def test_no_progress_failure_keeps_bounded_recovery_step(self) -> None:
        event_type, payload = room_event_projection(AgentEventEnvelope(
            event_id="event:no-progress",
            session_id="session:1",
            turn_id="turn:1",
            sequence=2,
            created_at_ms=2,
            event_type="turn_failed",
            payload={
                "failureKind": "tool_loop",
                "reason": "repeated_failure_signature",
                "toolNames": ["read"],
                "nextStep": "检查工具 read 的调用参数或权限，修正后在当前任务上重试。",
            },
            resume_token="event:no-progress",
        ))
        self.assertEqual(event_type, "turn_failed")
        self.assertEqual(payload["summary"], "工具连续失败，已停止本轮以避免继续空转")
        self.assertEqual(payload["error"], payload["summary"])
        self.assertEqual(payload["reason"], "repeated_failure_signature")
        self.assertEqual(payload["toolNames"], ["read"])
        self.assertIn("当前任务上重试", payload["nextStep"])

    def test_recovery_step_does_not_reclassify_other_failures_as_no_progress(self) -> None:
        _event_type, payload = room_event_projection(AgentEventEnvelope(
            event_id="event:provider-failure",
            session_id="session:1",
            turn_id="turn:1",
            sequence=3,
            created_at_ms=3,
            event_type="turn_failed",
            payload={
                "failureKind": "provider",
                "reason": "provider_unavailable",
                "nextStep": "检查 Provider 配置后重试。",
            },
            resume_token="event:provider-failure",
        ))
        self.assertEqual(payload["summary"], "模型响应失败，任务已暂停等待恢复")
        self.assertEqual(payload["nextStep"], "检查 Provider 配置后重试。")

    def test_oauth_failure_keeps_actionable_account_recovery_in_room(self) -> None:
        event_type, payload = room_event_projection(AgentEventEnvelope(
            event_id="event:oauth-failure", session_id="session:1", turn_id="turn:1",
            sequence=3, created_at_ms=3, event_type="turn_failed",
            payload={"error": "Encountered invalidated oauth token for user, failing request"},
            resume_token="event:oauth-failure",
        ))
        self.assertEqual(event_type, "turn_failed")
        self.assertEqual(payload["reason"], "provider_auth_failure")
        self.assertIn("重新登录", payload["summary"])
        self.assertEqual(payload["nextStep"], payload["summary"])

    def test_child_session_terminal_is_activity_not_second_room_final(self) -> None:
        service, _sessions, room_events = self._service(child=True)
        service.mirror_to_room(AgentEventEnvelope(
            event_id="event:child-terminal",
            session_id="session:1",
            turn_id="turn:child",
            sequence=7,
            created_at_ms=7,
            event_type="turn_completed",
            payload={"status": "completed", "summary": "child done"},
            resume_token="event:child-terminal",
        ))
        self.assertEqual(len(room_events.items), 1)
        self.assertEqual(room_events.items[0]["event_type"], "participant_activity")
        data = room_events.items[0]["payload"]["data"]
        self.assertEqual(data["activityKind"], "child")
        self.assertEqual(data["phase"], "completed")
        self.assertEqual(data["dispatchId"], "room-child:1")
        self.assertEqual(data["workItemId"], "work:1")
        self.assertEqual(data["workItemRevision"], 2)
        self.assertEqual(data["attemptId"], "attempt:2")

    def test_child_session_failure_keeps_sanitized_terminal_details(self) -> None:
        service, _sessions, room_events = self._service(child=True)
        service.mirror_to_room(AgentEventEnvelope(
            event_id="event:child-failure",
            session_id="session:1",
            turn_id="turn:child",
            sequence=8,
            created_at_ms=8,
            event_type="turn_failed",
            payload={
                "error": (
                    "FATAL ERROR: CALL_AND_RETRY_LAST Allocation failed - "
                    "JavaScript heap out of memory"
                ),
                "failureKind": "runtime_host_exit",
                "exitCode": -6,
                "nextStep": "重启 Runtime 后重试当前任务。",
            },
            resume_token="event:child-failure",
        ))

        data = room_events.items[0]["payload"]["data"]
        self.assertEqual(data["activityKind"], "child")
        self.assertEqual(data["phase"], "failed")
        self.assertEqual(data["status"], "failed")
        self.assertTrue(data["isError"])
        self.assertEqual(data["error"], data["summary"])
        self.assertIn("重启 Runtime", data["nextStep"])
        self.assertNotIn("FATAL ERROR", str(data))

    def test_runtime_failure_metrics_keep_bounded_typed_classification(self) -> None:
        metrics = runtime_event_metrics(AgentEventEnvelope(
            event_id="event:runtime-failure",
            session_id="session:1",
            turn_id="turn:1",
            sequence=1,
            created_at_ms=1,
            event_type="turn_failed",
            payload={
                "failureKind": "runtime_host_exit",
                "reasonCode": "runtime_host_exit",
                "exitCode": -6,
                "hadToolActivity": False,
                "retryable": True,
                "error": "private runtime detail",
            },
            resume_token="event:runtime-failure",
        ))

        self.assertEqual(
            metrics,
            {
                "failureKind": "runtime_host_exit",
                "reasonCode": "runtime_host_exit",
                "exitCode": -6,
                "hadToolActivity": False,
                "retryable": True,
            },
        )
        self.assertEqual(
            runtime_event_metrics(AgentEventEnvelope(
                event_id="event:invalid-runtime-failure",
                session_id="session:1",
                turn_id="turn:1",
                sequence=2,
                created_at_ms=2,
                event_type="turn_failed",
                payload={
                    "failureKind": "secret value " + ("x" * 80),
                    "reasonCode": {"private": "detail"},
                    "exitCode": True,
                    "hadToolActivity": "false",
                    "retryable": 1,
                },
                resume_token="event:invalid-runtime-failure",
            )),
            {},
        )

        service, sessions, _room_events = self._service()
        service.record(AgentEventEnvelope(
            event_id="event:runtime-failure",
            session_id="session:1",
            turn_id="turn:1",
            sequence=1,
            created_at_ms=1,
            event_type="turn_failed",
            payload={
                "failureKind": "runtime_host_exit",
                "reasonCode": "runtime_host_exit",
                "exitCode": -6,
                "hadToolActivity": False,
                "retryable": True,
                "error": "private runtime detail",
            },
            resume_token="event:runtime-failure",
        ))
        self.assertEqual(
            sessions.runtime_events[0]["metrics"],
            {
                "failureKind": "runtime_host_exit",
                "reasonCode": "runtime_host_exit",
                "exitCode": -6,
                "hadToolActivity": False,
                "retryable": True,
            },
        )

    def test_gateway_outcome_survives_pi_and_room_projection_in_live_and_history_shapes(self) -> None:
        for tool_name in ("write", "workspace_write", "custom_tool"):
            for outcome, phase, replay in (("unknown", "sent", False), ("not_started", "queued", True), ("applied", "sent", False)):
                for nested in (False, True):
                    with self.subTest(tool=tool_name, outcome=outcome, nested=nested):
                        details = {
                            "executionOutcome": outcome, "gatewayRequestPhase": phase,
                            "replayAllowed": replay, "retryable": replay,
                            "gatewayRecovery": {"state": "pending", "error": "sk-" + "123456789012345678901234567890"},
                            "gatewayError": "Tool gateway request timed out after 30000ms",
                            "gatewayTimeoutMs": 30000,
                            "errorCode": "tool_gateway_timeout",
                            "summary": "结果未知，不要重发",
                            "privateBody": "do-not-publish-this-body",
                        }
                        raw_result = {"details": details, "content": []} if nested else details
                        kind, pi_payload = tool_event_payload({
                            "toolCallId": "call:projection", "toolName": tool_name,
                            "args": {"path": "/tmp/receipt.txt"}, "result": raw_result,
                        }, event_type="tool_execution_end", source_loop_id="loop:projection")
                        _, room = room_event_projection(AgentEventEnvelope(
                            event_id="event:projection", session_id="session:1", turn_id="turn:1",
                            sequence=1, created_at_ms=1, event_type=kind, payload=pi_payload,
                            resume_token="event:projection",
                        ))
                        result = room["result"]
                        self.assertEqual(result["executionOutcome"], outcome)
                        self.assertEqual(result["gatewayRequestPhase"], phase)
                        self.assertEqual(result["replayAllowed"], replay)
                        self.assertEqual(result["gatewayTimeoutMs"], 30000)
                        self.assertEqual(result["gatewayRecovery"]["state"], "pending")
                        self.assertNotIn("123456789012345678901234567890", str(result))
                        self.assertNotIn("do-not-publish-this-body", str(result))

    def test_room_gateway_outcome_is_not_inferred_from_tool_output_text(self) -> None:
        _, room = room_event_projection(AgentEventEnvelope(
            event_id="event:output", session_id="session:1", turn_id="turn:1",
            sequence=1, created_at_ms=1, event_type="tool_finished", resume_token="event:output",
            payload={"toolName": "bash", "result": {"details": {"stdout":
                '{"executionOutcome":"applied","gatewayRequestPhase":"sent","replayAllowed":false}'
            }}},
        ))
        self.assertNotIn("executionOutcome", room.get("result", {}))

    def test_room_gateway_outcome_ignores_malformed_structured_state(self) -> None:
        for details in (
            {"executionOutcome": ["unknown"], "gatewayRequestPhase": "sent"},
            {"executionOutcome": "applied", "gatewayRequestPhase": "queued"},
        ):
            _, room = room_event_projection(AgentEventEnvelope(
                event_id="event:malformed", session_id="session:1", turn_id="turn:1",
                sequence=1, created_at_ms=1, event_type="tool_finished", resume_token="event:malformed",
                payload={"toolName": "write", "result": {"details": details}},
            ))
            self.assertNotIn("executionOutcome", room.get("result", {}))

    def test_room_partner_public_tool_result_is_bounded(self) -> None:
        event_type, payload = room_event_projection(AgentEventEnvelope(
            event_id="event:partner-tool",
            session_id="session:1",
            turn_id="turn:1",
            sequence=8,
            created_at_ms=8,
            event_type="tool_finished",
            payload={
                "toolName": "room_partner",
                "args": {
                    "op": "delegate",
                    "targetParticipantId": "participant:2",
                    "task": "inspect",
                    "timeoutSeconds": 30,
                },
                "result": {
                    "ok": True,
                    "result": {
                        "operation": "delegate",
                        "status": "completed",
                        "result": "X" * 10_000,
                    },
                },
                "status": "completed",
            },
            resume_token="event:partner-tool",
        ))
        self.assertEqual(event_type, "participant_activity")
        self.assertEqual(payload["arguments"]["timeoutSeconds"], 30)
        self.assertEqual(len(payload["result"]["result"]), 500)

    def test_room_partner_tool_error_preserves_runtime_text(self) -> None:
        event_type, payload = room_event_projection(AgentEventEnvelope(
            event_id="event:partner-tool-error",
            session_id="session:1",
            turn_id="turn:1",
            sequence=9,
            created_at_ms=9,
            event_type="tool_finished",
            payload={
                "toolName": "room_partner",
                "args": {
                    "op": "accept",
                    "workItemId": "room-work:1",
                    "expectedRevision": 1,
                },
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": "Room work must be in review",
                        }
                    ],
                    "details": {},
                },
                "isError": True,
            },
            resume_token="event:partner-tool-error",
        ))

        self.assertEqual(event_type, "participant_activity")
        self.assertEqual(payload["error"], "Room work must be in review")

    def test_nested_tool_event_preserves_parent_and_terminal_result(self) -> None:
        event_type, payload = tool_event_payload(
            {
                "toolCallId": "codemode:1/2",
                "parentToolCallId": "codemode:1",
                "toolName": "workspace_read",
                "args": {"path": "/Users/private/project/README.md"},
                "result": {
                    "content": [{"type": "text", "text": "hello"}],
                    "details": {"summary": "读取完成"},
                },
                "isError": False,
            },
            event_type="tool_execution_end",
            source_loop_id="loop:codemode",
        )

        self.assertEqual(event_type, "tool_finished")
        self.assertEqual(payload["parentToolCallId"], "codemode:1")
        self.assertEqual(payload["toolCallId"], "codemode:1/2")
        self.assertEqual(payload["result"]["details"]["summary"], "读取完成")
        _room_type, room = room_event_projection(AgentEventEnvelope(
            event_id="event:nested-tool",
            session_id="session:1",
            turn_id="turn:1",
            sequence=1,
            created_at_ms=1,
            event_type=event_type,
            payload=payload,
            resume_token="event:nested-tool",
        ))
        self.assertEqual(room["parentToolCallId"], "codemode:1")

    def test_nested_calls_are_safe_public_metadata_without_result_duplication(self) -> None:
        _event_type, payload = tool_event_payload(
            {
                "toolCallId": "codemode:1",
                "toolName": "codemode",
                "args": {"code": "await tools.workspace_read({path: 'README.md'})"},
                "result": {
                    "details": {
                        "calls": [
                            {
                                "id": "codemode:1/1",
                                "name": "workspace_read",
                                "args": '{"path":"README.md"}',
                                "status": "ok",
                                "durationMs": 12,
                            },
                        ],
                    },
                },
                "isError": False,
            },
            event_type="tool_execution_end",
            source_loop_id="loop:codemode",
        )

        self.assertEqual(payload["nestedCalls"][0]["id"], "codemode:1/1")
        self.assertEqual(payload["nestedCalls"][0]["args"], {"path": "README.md"})
        self.assertEqual(payload["nestedCalls"][0]["status"], "ok")
        # The outer receipt retains its own result; nested rows are represented
        # by parent-linked live events rather than copied into another result.
        self.assertNotIn("result", payload["nestedCalls"][0])


if __name__ == "__main__":
    unittest.main()
