from __future__ import annotations

import json
import io
import urllib.error
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import rag_ime.agent_approval_model as approval_model_module
from rag_ime import jev
from rag_ime.agent_approval_model import ApprovalModelArbiter
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.db import sqlite_connection


class FakeCompletionRuntime:
    def __init__(self, response: object) -> None:
        self.response = response
        self.requests: list[dict[str, object]] = []

    def complete_once(self, **kwargs: object) -> dict[str, object]:
        self.requests.append(dict(kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return dict(self.response) if isinstance(self.response, dict) else {"text": self.response}


class ApprovalModelArbiterTests(unittest.TestCase):
    def setUp(self) -> None:
        key_patch = patch.object(approval_model_module, "_jev_api_key", return_value="")
        key_patch.start()
        self.addCleanup(key_patch.stop)
        self.temporary = tempfile.TemporaryDirectory(prefix="approval-model-")
        self.db_path = Path(self.temporary.name) / "agent.sqlite3"
        self.workspace = Path(self.temporary.name) / "workspace"
        self.workspace.mkdir()
        self.sessions = AgentSessionStore(self.db_path)
        self.sessions.initialize()
        self.session = self.sessions.create(
            title="Model arbitration",
            mode="coordinator",
            execution_mode="full_trust",
            workspace_roots=[str(self.workspace)],
            created_at_ms=10,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def approval(self, *, preview: dict[str, object] | None = None) -> dict[str, object]:
        return self.sessions.create_approval(
            session_id=str(self.session["id"]),
            tool_name="workspace_shell",
            operation="run",
            payload_sha256="a" * 64,
            preview=preview or {
                "title": "Run bounded command",
                "summary": "Run printf in the selected workspace",
                "baseState": {"workspaceRootSha256": "b" * 64},
            },
            risk_level="R2",
            requested_at_ms=20,
        )

    def test_luna_max_decision_is_hash_bound_persisted_and_reused(self) -> None:
        runtime = FakeCompletionRuntime(
            {
                "text": json.dumps(
                    {
                        "decision": "approve",
                        "reasonCodes": ["bounded_operation", "authorized_scope"],
                        "rationaleSummary": "操作被限定在已授权工作区。",
                    },
                    ensure_ascii=False,
                )
            }
        )
        ticks = iter((100, 120))
        arbiter = ApprovalModelArbiter(
            self.db_path,
            runtime_provider=lambda: runtime,
            clock_ms=lambda: next(ticks),
        )
        approval = self.approval()

        receipt = arbiter.decide(approval, self.session)
        repeated = arbiter.decide(approval, self.session)

        self.assertEqual(receipt, repeated)
        self.assertEqual(receipt["decision"], "approve")
        self.assertEqual(receipt["status"], "decided")
        self.assertEqual(receipt["modelProfile"], "openai-codex/gpt-5.6-luna")
        self.assertEqual(receipt["thinkingLevel"], "max")
        self.assertEqual(receipt["payloadSha256"], "a" * 64)
        self.assertEqual(len(runtime.requests), 1)
        request = runtime.requests[0]
        self.assertEqual(request["provider"], "openai-codex")
        self.assertEqual(request["model_id"], "gpt-5.6-luna")
        self.assertEqual(request["thinking_level"], "max")
        with self.assertRaises(sqlite3.IntegrityError):
            with sqlite_connection(self.db_path) as conn:
                conn.execute(
                    "UPDATE agent_approval_model_decisions SET decision = 'deny' WHERE approval_id = ?",
                    (approval["approvalId"],),
                )

    def test_jev_decision_is_recorded_with_structured_identity_when_configured(self) -> None:
        runtime = FakeCompletionRuntime({"text": "should not be called"})
        arbiter = ApprovalModelArbiter(
            self.db_path,
            runtime_provider=lambda: runtime,
            clock_ms=lambda: 125,
        )
        approval = self.approval()
        with patch.object(approval_model_module, "_jev_api_key", return_value="jev-test-key"), patch.object(
            approval_model_module,
            "_jev_decide",
            return_value=("approve", ["bounded_operation", "requested_effect_matches_preview"], "Jev 判定通过。"),
        ):
            receipt = arbiter.decide(approval, self.session)
        self.assertEqual(receipt["modelProvider"], "typesafe")
        self.assertEqual(receipt["modelProfile"], "typesafe/jev-latest")
        self.assertEqual(receipt["thinkingLevel"], "structured")
        self.assertEqual(receipt["promptVersion"], "approval-arbiter-jev-v1")
        self.assertEqual(runtime.requests, [])

    def test_jev_transport_failure_falls_back_to_luna(self) -> None:
        runtime = FakeCompletionRuntime({
            "text": json.dumps({
                "decision": "deny",
                "reasonCodes": ["policy_boundary"],
                "rationaleSummary": "Luna fallback deny",
            })
        })
        arbiter = ApprovalModelArbiter(self.db_path, runtime_provider=lambda: runtime, clock_ms=lambda: 126)
        approval = self.approval()
        with patch.object(approval_model_module, "_jev_api_key", return_value="jev-test-key"), patch.object(
            approval_model_module, "_jev_decide", side_effect=RuntimeError("endpoint unavailable")
        ):
            receipt = arbiter.decide(approval, self.session)
        self.assertEqual(receipt["modelProfile"], "openai-codex/gpt-5.6-luna")
        self.assertIn("model_unavailable", receipt["reasonCodes"])
        self.assertEqual(len(runtime.requests), 1)

    def test_jev_low_confidence_deny_is_persisted_without_luna_fallback(self) -> None:
        runtime = FakeCompletionRuntime({"text": "must not run"})
        arbiter = ApprovalModelArbiter(self.db_path, runtime_provider=lambda: runtime)
        approval = self.approval()
        with patch.object(approval_model_module, "_jev_api_key", return_value="fake"), patch.object(
            approval_model_module, "_jev_decide", return_value=("deny", ["insufficient_evidence"], "low confidence")
        ) as decide:
            receipt = arbiter.decide(approval, self.session)
            self.assertEqual(arbiter.decide(approval, self.session), receipt)
            decide.assert_called_once()
        self.assertEqual(receipt["decision"], "deny")
        self.assertEqual(receipt["modelProvider"], "typesafe")
        self.assertEqual(runtime.requests, [])

    def test_model_input_binds_request_identity_arguments_scope_and_task(self) -> None:
        runtime = FakeCompletionRuntime(
            {
                "text": json.dumps(
                    {
                        "decision": "approve",
                        "reasonCodes": ["bounded_operation", "authorized_scope"],
                        "rationaleSummary": "范围和参数均已绑定。",
                    },
                    ensure_ascii=False,
                )
            }
        )
        scope_sha256 = str(self.session["workspaceScopeSha256"])
        approval = self.sessions.create_approval(
            session_id=str(self.session["id"]),
            tool_name="workspace_shell",
            operation="run",
            payload_sha256="c" * 64,
            preview={
                "title": "Bounded read",
                "summary": "Read the selected workspace",
                "actionPayload": {
                    "command": "printf 'ok'",
                    "cwd": str(self.workspace),
                    "allowNetwork": False,
                },
                "baseState": {"workspaceRootsSha256": scope_sha256},
            },
            risk_level="R2",
            causal_metadata={"turnId": "turn-current"},
            requested_at_ms=30,
        )
        approval = self.sessions.bind_approval_tool_call(
            str(approval["approvalId"]),
            tool_call_id="tool-call-current",
        )
        arbiter = ApprovalModelArbiter(
            self.db_path,
            runtime_provider=lambda: runtime,
            context_provider=lambda current, session: {
                "contextAvailable": True,
                "contextKind": "session",
                "contextId": session["id"],
                "userRequests": [
                    {
                        "role": "user",
                        "text": "Read the selected workspace file.",
                    }
                ],
                "currentTask": {
                    "kind": "session_task",
                    "taskId": "task-current",
                    "objective": "Read the selected workspace file.",
                },
            },
            clock_ms=lambda: 150,
        )

        arbiter.decide(approval, self.session)

        model_input = json.loads(
            str(runtime.requests[0]["message"]).split(
                "UNTRUSTED_APPROVAL_EVIDENCE_JSON:\n", 1
            )[1]
        )
        self.assertEqual(
            model_input["requestIdentity"],
            {"turnId": "turn-current", "toolCallId": "tool-call-current"},
        )
        self.assertEqual(
            model_input["currentApproval"]["arguments"]["command"],
            "printf 'ok'",
        )
        self.assertEqual(
            model_input["currentApproval"]["workspaceScope"]["scopeSha256"],
            scope_sha256,
        )
        self.assertEqual(
            model_input["currentApproval"]["riskClassification"]["riskLevel"],
            "R2",
        )
        self.assertEqual(
            model_input["approvalContext"]["currentTask"]["taskId"],
            "task-current",
        )
        self.assertEqual(
            model_input["approvalContext"]["userRequests"][0]["text"],
            "Read the selected workspace file.",
        )

    def test_invalid_or_unavailable_model_fails_closed_without_leaking_secrets(self) -> None:
        runtime = FakeCompletionRuntime(
            {
                "text": json.dumps(
                    {
                        "decision": "approve",
                        "reasonCodes": ["bounded_operation"],
                        "rationaleSummary": "ok",
                        "unexpected": True,
                    }
                )
            }
        )
        arbiter = ApprovalModelArbiter(
            self.db_path,
            runtime_provider=lambda: runtime,
            clock_ms=lambda: 200,
        )
        approval = self.approval(
            preview={
                "title": "Untrusted preview",
                "token": "sk-this-must-not-leak",
                "changes": [
                    {
                        "label": "command",
                        "after": "rm -rf .env",
                    }
                ],
            }
        )

        receipt = arbiter.decide(approval, self.session)

        self.assertEqual(receipt["decision"], "deny")
        self.assertEqual(receipt["status"], "failed_closed")
        self.assertEqual(receipt["reasonCodes"], ["model_invalid_response"])
        prompt = str(runtime.requests[0]["message"])
        self.assertNotIn("sk-this-must-not-leak", prompt)
        self.assertIn("destructive_effect", prompt)
        self.assertIn("sensitive_target", prompt)

    def test_room_history_is_shared_across_sessions_and_excludes_agent_output(self) -> None:
        runtime = FakeCompletionRuntime(
            {
                "text": json.dumps(
                    {
                        "decision": "approve",
                        "reasonCodes": ["bounded_operation"],
                        "rationaleSummary": "当前任务允许该受限操作。",
                    },
                    ensure_ascii=False,
                )
            }
        )
        second_session = self.sessions.create(
            title="Second room participant",
            mode="coordinator",
            execution_mode="full_trust",
            workspace_roots=[str(self.workspace)],
            created_at_ms=11,
        )
        second_approval = self.sessions.create_approval(
            session_id=str(second_session["id"]),
            tool_name="workspace_shell",
            operation="run",
            payload_sha256="b" * 64,
            preview={
                "title": "Second bounded command",
                "summary": "Run another bounded command",
                "baseState": {"workspaceRootSha256": "b" * 64},
            },
            risk_level="R2",
            requested_at_ms=21,
        )

        def room_context(
            approval: dict[str, object],
            session: dict[str, object],
        ) -> dict[str, object]:
            return {
                "contextKind": "room",
                "contextId": "room-shared",
                "userRequests": [
                    {
                        "role": "user",
                        "text": "Only update the bounded workspace.",
                        "createdAtMs": 9,
                    }
                ],
                "currentTask": {
                    "kind": "room_task",
                    "taskId": "task-1",
                    "objective": "Apply the bounded change",
                },
                "actor": {"sessionId": session["id"]},
                "primaryAgentOutput": "PRIMARY_AGENT_MUST_NOT_REACH_ARBITER",
            }

        arbiter = ApprovalModelArbiter(
            self.db_path,
            runtime_provider=lambda: runtime,
            context_provider=room_context,
            clock_ms=lambda: 300 + len(runtime.requests),
        )
        first_approval = self.approval()

        first = arbiter.decide(first_approval, self.session)
        second = arbiter.decide(second_approval, second_session)

        self.assertEqual(first["contextKind"], "room")
        self.assertEqual(first["contextId"], "room-shared")
        self.assertEqual(first["historyEntryCount"], 0)
        self.assertEqual(second["historyEntryCount"], 1)
        second_prompt = str(runtime.requests[1]["message"])
        self.assertNotIn("PRIMARY_AGENT_MUST_NOT_REACH_ARBITER", second_prompt)
        model_input = json.loads(
            second_prompt.split("UNTRUSTED_APPROVAL_EVIDENCE_JSON:\n", 1)[1]
        )
        self.assertEqual(
            model_input["approvalContext"]["userRequests"][0]["text"],
            "Only update the bounded workspace.",
        )
        self.assertEqual(
            model_input["approvalContext"]["currentTask"]["taskId"],
            "task-1",
        )
        self.assertEqual(
            model_input["decisionHistory"][0]["approvalId"],
            first_approval["approvalId"],
        )
        self.assertEqual(
            model_input["decisionHistory"][0]["decision"],
            "approve",
        )

    def test_missing_authoritative_context_fails_closed_before_model_call(self) -> None:
        runtime = FakeCompletionRuntime({"text": "{}"})
        arbiter = ApprovalModelArbiter(
            self.db_path,
            runtime_provider=lambda: runtime,
            context_provider=lambda approval, session: {
                "contextAvailable": False,
                "contextKind": "session",
                "contextId": session["id"],
            },
            clock_ms=lambda: 400,
        )

        receipt = arbiter.decide(self.approval(), self.session)

        self.assertEqual(receipt["decision"], "deny")
        self.assertEqual(receipt["status"], "failed_closed")
        self.assertEqual(
            receipt["failureCode"],
            "APPROVAL_CONTEXT_UNAVAILABLE",
        )
        self.assertEqual(runtime.requests, [])

    def test_pending_full_automation_approval_is_marked_model_owned_and_extended(self) -> None:
        approval = self.approval()

        self.assertEqual(
            approval["preview"]["approvalArbitration"],
            {

                "mode": "model",
                "status": "running",
                "modelProfile": "openai-codex/gpt-5.6-luna",
                "thinkingLevel": "max",
                "promptVersion": "approval-arbiter-v2",
            },
        )
        self.assertEqual(approval["expiresAtMs"], 180_020)

    def test_pending_approval_cancellation_is_terminal_and_idempotent(self) -> None:
        approval = self.approval()

        first = self.sessions.cancel_pending_approvals(
            str(self.session["id"]),
            reason="user_abort",
            turn_id="turn-abort",
            now_ms=500,
        )
        second = self.sessions.cancel_pending_approvals(
            str(self.session["id"]),
            reason="user_abort",
            turn_id="turn-abort",
            now_ms=501,
        )

        self.assertEqual(first["cancelledApprovalIds"], [approval["approvalId"]])
        self.assertEqual(second["cancelledApprovalIds"], [])
        cancelled = self.sessions.get_approval(str(approval["approvalId"]))
        self.assertEqual(cancelled["state"], "stale")
        self.assertEqual(cancelled["decidedBy"], "runtime-cancellation")
        self.assertEqual(cancelled["receipt"]["turnId"], "turn-abort")


if __name__ == "__main__":
    unittest.main()


class JevWireTests(unittest.TestCase):
    def decide(self, answer):
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps({"answers": {"approval": answer}}).encode())
        with patch.object(jev.urllib.request, "build_opener", return_value=opener):
            result = approval_model_module._jev_decide({"test": "synthetic"}, api_key="fake-key", timeout_seconds=3)
        request = opener.open.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(body["model"], "jev-latest")
        self.assertEqual(body["questions"]["approval"]["type"], "choice")
        self.assertEqual(request.get_header("Authorization"), "Bearer fake-key")
        return result

    def test_choice_and_confidence_threshold(self):
        for choice, confidence, expected in [("approve", .99, "approve"), ("deny", .99, "deny"),
                                               ("approve", .69, "deny"), ("approve", .70, "approve")]:
            with self.subTest(choice=choice, confidence=confidence):
                self.assertEqual(self.decide({"type": "choice", "choice": choice, "confidence": confidence})[0], expected)

    def test_malformed_confidence_and_answer_type_are_rejected(self):
        for confidence in [True, "0.99", None, -1, 2, float("nan"), float("inf")]:
            with self.subTest(confidence=confidence), self.assertRaises(ValueError):
                self.decide({"type": "choice", "choice": "approve", "confidence": confidence})
        for answer in [{}, {"type": "score", "choice": "approve", "confidence": .99},
                       {"type": "choice", "choice": "unknown", "confidence": .99}]:
            with self.subTest(answer=answer), self.assertRaises(ValueError):
                self.decide(answer)

    def test_http_error_does_not_expose_provider_body(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError("https://api.typesafe.ai/v1/systemone", 401,
                                                         "private-provider-detail", {}, None)
        with patch.object(jev.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(RuntimeError, "^Jev HTTP 401$"):
                approval_model_module._jev_decide({}, api_key="fake", timeout_seconds=3)
