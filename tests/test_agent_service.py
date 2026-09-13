from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

from rag_ime.agent_background_jobs import AgentBackgroundJobService
from rag_ime.agent_protocol import AgentEventEnvelope
from rag_ime.agent_blocks import normalize_trusted_agent_blocks, provider_block_projection
from rag_ime.agent_command_receipts import (
    AgentCommandReceiptFailed,
    AgentCommandReceiptPending,
)
from rag_ime.agent_context_runtime import RUNTIME_PROMPT_ENVELOPE_PREFIX
from rag_ime.agent_execution_policy import workspace_scope_sha256
from rag_ime.agent_prompt_delivery import AgentPromptAcceptanceUnknown
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.agent_service import AgentService, pi_runtime_config_from_settings
from rag_ime.agent_tools import ControlToolGateway
from rag_ime.agent_workspace import WorkspaceHarness
from rag_ime.pi.config import PiRuntimeConfig
from rag_ime.pi.factory import PiRuntimeDriverFactory
from rag_ime.pi.values import PiRuntimeError
from rag_ime.pi.public import visible_message_text
from rag_ime.pi.values import (
    PiRuntimeCommandAcceptanceUnknown,
    PiRuntimeCommandRejected,
    PiRuntimeTurnConflict,
)


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _GatewayRuntime:
    runtime_kind = "gateway_http"
    driver_id = "test-gateway"

    def __init__(self, session_root: Path):
        self.session_root = session_root
        self.default_model_profile = "gateway/default"
        self.stopped = False

    def runtime_status(self):
        return {
            "schemaVersion": "rag-ime.agent-runtime.v1",
            "enabled": True,
            "managed": True,
            "status": "ready",
            "driverId": self.driver_id,
            "runtimeKind": self.runtime_kind,
            "runtimeVersion": "test-1",
            "piVersion": "",
            "idleTimeoutSeconds": 0,
            "activeSessionId": None,
            "lastError": "",
            "capabilities": {"sessions": True, "modelConfigured": True},
        }

    def stop(self):
        self.stopped = True


class _GatewayRuntimeFactory:
    runtime_kind = "gateway_http"
    driver_id = "test-gateway"
    default_model_profile = "gateway/default"

    def __init__(self, root: Path):
        self.session_root = root / "gateway-sessions"
        self.working_root = root / "gateway-work"
        self.created_for: list[str] = []
        self.runtime: _GatewayRuntime | None = None

    def create(self, _context, *, purpose, session_context_provider=None):
        del session_context_provider
        self.created_for.append(purpose)
        self.runtime = _GatewayRuntime(self.session_root)
        return self.runtime

    def reconfigure(self, _config):
        return None

    def apply_policy(self, _policy):
        return None


class _ForkRuntime:
    def __init__(self, sessions, root: Path, *, fail: bool = False):
        self.sessions = sessions
        self.root = root
        self.fail = fail
        self.target_session_id = ""

    def fork_candidates(self, session_id: str):
        return [
            {
                "entryId": "entry-user-1",
                "text": "保留这个节点",
                "role": "user",
                "createdAtMs": 0,
            }
        ]

    def fork_session(self, source_session_id: str, target_session_id: str, *, entry_id: str):
        self.target_session_id = target_session_id
        if self.fail:
            raise PiRuntimeError("fork failed")
        transcript = self.root / f"{target_session_id.replace(':', '-')}.jsonl"
        transcript.touch()
        session = self.sessions.bind_runtime_session(
            target_session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-fork-1",
            transcript_ref=str(transcript),
            branch_anchor=entry_id,
            message_count=3,
        )
        return {
            "sourceSessionId": source_session_id,
            "targetSessionId": target_session_id,
            "entryId": entry_id,
            "selectedText": "保留这个节点",
            "session": self.sessions.set_status(target_session_id, "idle"),
        }

    def stop(self):
        return None


class AgentServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-agent-service-")
        self.root = Path(self.tmp.name)
        self.process_id = 100
        self.service = AgentService(
            db_path=self.root / "rag-ime.sqlite",
            runtime_config=PiRuntimeConfig(
                enabled=False,
                executable=None,
                agent_dir=self.root / "agent-config",
                session_dir=self.root / "sessions",
                logs_dir=self.root / "logs",
            ),
            process_id_provider=lambda: self.process_id,
        )

    def tearDown(self) -> None:
        self.service.close()
        self.tmp.cleanup()

    def _prepare_room_bound_approval(
        self,
        *,
        suffix: str,
        tool_name: str = "workspace_shell",
        operation: str = "run",
    ) -> dict[str, str]:
        room = self.service.create_room(
            {
                "title": f"Room approval recovery {suffix}",
                "workspaceRoots": [self.root.as_posix()],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        participant = room["participants"][0]
        room_id = str(room["id"])
        session_id = str(participant["sessionId"])
        self.service.sessions.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id=f"pi-room-recovery-{suffix}",
        )
        root_id = f"root:recovery:{suffix}"
        dispatch_id = f"dispatch:recovery:{suffix}"
        session_turn_id = f"turn:recovery:{suffix}"
        tool_call_id = f"tool:recovery:{suffix}"
        self.service.room_turns.begin(
            session_id,
            root_id,
            dispatch_id=dispatch_id,
        )
        self.service.room_turns.accept(
            session_id,
            session_turn_id,
            root_id,
        )
        room_context = self.service._active_room_dispatch_context(session_id)
        self.assertIsNotNone(room_context)
        self.service.events.publish(
            session_id,
            "tool_started",
            {
                "toolCallId": tool_call_id,
                "toolName": tool_name,
                "summary": "执行 Room 自动授权命令",
            },
            turn_id=session_turn_id,
        )
        self.assertTrue(self.service.events.flush())
        approval = self.service.sessions.create_approval(
            session_id=session_id,
            tool_name=tool_name,
            operation=operation,
            payload_sha256=hashlib.sha256(suffix.encode("utf-8")).hexdigest(),
            preview={"title": "执行命令", "summary": "Room 自动授权"},
            risk_level="R2",
            causal_metadata={
                "turnId": session_turn_id,
                "roomBound": True,
            },
        )
        approval_id = str(approval["approvalId"])
        self.service.sessions.bind_approval_tool_call(
            approval_id,
            tool_call_id=tool_call_id,
        )
        self.service.sessions.bind_approval_room_dispatch(
            approval_id,
            room_context=room_context or {},
        )
        return {
            "approvalId": approval_id,
            "payloadSha256": str(approval["payloadSha256"]),
            "roomId": room_id,
            "sessionId": session_id,
            "rootId": root_id,
            "dispatchId": dispatch_id,
            "sessionTurnId": session_turn_id,
            "toolCallId": tool_call_id,
        }

    def test_evaluation_snapshot_rejects_all_conversation_mutations(self) -> None:
        snapshot = self.service.sessions.create(
            title="EnterpriseOps CSM · Task 1 · 通过",
            model_profile="openai-codex/gpt-5.6-sol",
            thinking_level="high",
            tool_profile_version="subagent-readonly-v1",
            execution_mode="read_only",
            evaluation_snapshot=True,
        )
        session_id = str(snapshot["id"])

        mutations = (
            lambda: self.service.prompt(session_id, {"message": "继续"}),
            lambda: self.service.update_session(session_id, {"title": "改名"}),
            lambda: self.service.select_model(
                session_id,
                {"provider": "openai-codex", "modelId": "gpt-5.6-sol"},
            ),
            lambda: self.service.select_thinking_level(session_id, {"level": "max"}),
            lambda: self.service.invoke_command(session_id, {"command": "/compact"}),
            lambda: self.service.fork_session(session_id, {"entryId": "entry-1"}),
            lambda: self.service.rewrite_session(
                session_id,
                {"entryId": "entry-1", "message": "改写"},
            ),
            lambda: self.service.compact(session_id, {}),
            lambda: self.service.delete_session(session_id),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with self.assertRaisesRegex(ValueError, "evaluation snapshot is read-only"):
                    mutation()

        self.assertTrue(self.service.sessions.get(session_id)["evaluationSnapshot"])

    def test_trace_diagnostic_report_freezes_full_trust_session_and_finalizes_structured_result(self) -> None:
        policy = {
            "mode": "coordinator",
            "toolProfileVersion": "control-center-auto-approve-v1",
            "executionMode": "full_trust",
            "dangerousModeConfirmation": "ENABLE_FULL_TRUST",
            "workspaceRoots": ["/"],
            "toolAllowlistMode": "profile",
            "projectContextEnabled": True,
            "piSkillsEnabled": True,
            "codexSkillsEnabled": True,
        }
        ownerless = self.service.create_session(
            {"title": "ordinary full-trust Session", **policy}
        )["session"]
        with self.assertRaisesRegex(ValueError, "explicit full-trust diagnostic"):
            self.service.create_trace_diagnostic_report(
                {
                    "diagnosticSessionId": ownerless["id"],
                    "title": "must reject ordinary Session",
                    "targets": [],
                }
            )
        session = self.service.create_session(
            {
                "title": "Trace diagnostic",
                "surfaceKind": "extension_app",
                "ownerAppId": "extension:trace-agent",
                "surfaceKey": "diagnostic",
                **policy,
                "workspaceRoots": [str(self.root)],
            }
        )["session"]
        self.assertEqual(session["mode"], "coordinator")
        self.assertEqual(session["toolProfileVersion"], "control-center-auto-approve-v1")
        self.assertEqual(session["executionMode"], "full_trust")
        self.assertEqual(session["workspaceRoots"], [str(self.root.resolve())])
        self.assertEqual(session["toolAllowlistMode"], "profile")
        self.assertTrue(session["projectContextEnabled"])
        self.assertTrue(session["piSkillsEnabled"])
        self.assertTrue(session["codexSkillsEnabled"])
        result = {
            "schemaVersion": "rag-ime.trace-diagnostic-result.v1",
            "summary": "没有权威完成回执，结论保持未知。",
            "hardGates": [
                {
                    "gateId": "task_completion",
                    "status": "unknown",
                    "reason": "缺少回执。",
                    "evidenceIds": [],
                }
            ],
            "judgeScores": [],
            "findings": [],
            "presentation": {
                "headline": "诊断证据不足",
                "impact": "没有权威完成回执，因此不能确认任务完成。",
                "primaryFindingId": "",
                "knownFacts": [],
                "evidenceGaps": [
                    {
                        "gap": "缺少完成回执",
                        "consequence": "任务完成度保持未知",
                        "howToObtain": "读取权威 Runtime 完成事件",
                    }
                ],
                "causalNodes": [],
                "expectedStageCount": 0,
                "recordedStageReceiptEvidenceIds": [],
                "failureAttribution": {
                    "primaryLayer": "unknown",
                    "summary": "现有证据不足以归因。",
                    "layers": [
                        {
                            "layer": layer,
                            "verdict": "unknown",
                            "explanation": "没有足够的冻结证据。",
                            "evidenceIds": [],
                        }
                        for layer in ("tool", "skill", "template", "workflow", "model")
                    ],
                },
            },
        }
        public_snapshot = {
            "ok": True,
            "sessionId": session["id"],
            "status": "idle",
            "items": [
                {
                    "role": "assistant",
                    "status": "completed",
                    "timelineSequence": 1,
                    "blocks": [
                        {
                            "status": "completed",
                            "data": {
                                "text": "--- TRACE_DIAGNOSTIC_RESULT_V1 ---\n"
                                + json.dumps(result, ensure_ascii=False)
                                + "\n--- END_TRACE_DIAGNOSTIC_RESULT_V1 ---"
                            },
                        }
                    ],
                }
            ],
            "liveEvents": [],
        }

        with patch.object(self.service, "messages", return_value=public_snapshot):
            report = self.service.create_trace_diagnostic_report(
                {
                    "diagnosticSessionId": session["id"],
                    "title": "持久化网页报告",
                    "targets": [
                        {"kind": "session", "id": session["id"], "title": "Trace diagnostic"}
                    ],
                }
            )
            completed = self.service.finalize_trace_diagnostic_report(
                report["reportId"],
                {"expectedRevision": report["revision"]},
            )

        self.assertEqual(report["status"], "generating")
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["result"], result)
        self.assertEqual(
            self.service.list_trace_diagnostic_reports()["items"][0]["reportId"],
            report["reportId"],
        )

    def test_trace_diagnostic_report_rejects_ordinary_session_policy(self) -> None:
        session = self.service.create_session({"title": "ordinary Agent"})["session"]
        self.assertEqual(session["executionMode"], "per_action")
        self.assertEqual(session["toolProfileVersion"], "control-center-v1")
        self.assertEqual(session["workspaceRoots"], [])
        with self.assertRaisesRegex(
            ValueError,
            "explicit full-trust diagnostic Session policy",
        ):
            self.service.create_trace_diagnostic_report(
                {
                    "diagnosticSessionId": session["id"],
                    "targets": [
                        {
                            "kind": "session",
                            "id": session["id"],
                            "title": "ordinary Agent",
                        }
                    ],
                }
            )

    def test_trace_diagnostic_report_freezes_matching_source_project_binding(self) -> None:
        source_root = self.root / "source-project"
        source_root.mkdir()
        source = self.service.create_session(
            {
                "title": "Source project Session",
                "mode": "coordinator",
                "workspaceRoots": [str(source_root)],
            }
        )["session"]
        diagnostic = self.service.create_session(
            {
                "title": "Bound Trace diagnostic",
                "surfaceKind": "extension_app",
                "ownerAppId": "extension:trace-agent",
                "surfaceKey": "diagnostic",
                "mode": "coordinator",
                "toolProfileVersion": "control-center-auto-approve-v1",
                "executionMode": "full_trust",
                "dangerousModeConfirmation": "ENABLE_FULL_TRUST",
                "workspaceRoots": [str(source_root)],
                "toolAllowlistMode": "profile",
                "projectContextEnabled": True,
                "piSkillsEnabled": True,
                "codexSkillsEnabled": True,
            }
        )["session"]
        source_snapshot = {
            "ok": True,
            "sessionId": source["id"],
            "status": "idle",
            "items": [],
            "liveEvents": [],
        }

        with patch.object(self.service, "messages", return_value=source_snapshot):
            report = self.service.create_trace_diagnostic_report(
                {
                    "diagnosticSessionId": diagnostic["id"],
                    "title": "Frozen source binding",
                    "targets": [
                        {
                            "kind": "session",
                            "id": source["id"],
                            "title": "Source project Session",
                        }
                    ],
                }
            )

        environment = report["inspection"]["environment"]["targets"][0]
        self.assertEqual(
            environment["workspaceScopeSha256"],
            workspace_scope_sha256([str(source_root.resolve())]),
        )
        self.assertEqual(
            diagnostic["workspaceRoots"],
            [str(source_root.resolve())],
        )

        updated = self.service.update_session(
            diagnostic["id"],
            {
                "mode": "coordinator",
                "toolProfileVersion": "control-center-auto-approve-v1",
                "executionMode": "full_trust",
                "workspaceRoots": [str(source_root)],
                "toolAllowlistMode": "profile",
                "projectContextEnabled": True,
                "piSkillsEnabled": True,
                "codexSkillsEnabled": True,
            },
        )["session"]
        self.assertEqual(updated["workspaceRoots"], [str(source_root.resolve())])

    def test_trace_diagnostic_session_requires_a_project_binding(self) -> None:
        base = {
            "title": "Unbound Trace diagnostic",
            "surfaceKind": "extension_app",
            "ownerAppId": "extension:trace-agent",
            "surfaceKey": "diagnostic",
            "mode": "coordinator",
            "toolProfileVersion": "control-center-auto-approve-v1",
            "executionMode": "full_trust",
            "toolAllowlistMode": "profile",
            "projectContextEnabled": True,
            "piSkillsEnabled": True,
            "codexSkillsEnabled": True,
        }
        for workspace_roots in ([], ["/"]):
            with self.subTest(workspace_roots=workspace_roots):
                with self.assertRaisesRegex(ValueError, "binding_required"):
                    self.service.create_session(
                        {**base, "workspaceRoots": workspace_roots}
                    )

    def test_trace_diagnostic_report_rejects_an_unbound_source(self) -> None:
        source = self.service.create_session(
            {"title": "Unbound source Session"}
        )["session"]
        project_root = self.root / "selected-project"
        project_root.mkdir()
        diagnostic = self.service.create_session(
            {
                "title": "Bound Trace diagnostic",
                "surfaceKind": "extension_app",
                "ownerAppId": "extension:trace-agent",
                "surfaceKey": "diagnostic",
                "mode": "coordinator",
                "toolProfileVersion": "control-center-auto-approve-v1",
                "executionMode": "full_trust",
                "workspaceRoots": [str(project_root)],
                "toolAllowlistMode": "profile",
                "projectContextEnabled": True,
                "piSkillsEnabled": True,
                "codexSkillsEnabled": True,
            }
        )["session"]

        with (
            patch.object(self.service.runtime, "session_snapshot", return_value={"messages": []}),
            self.assertRaisesRegex(ValueError, "binding_required"),
        ):
            self.service.create_trace_diagnostic_report(
                {
                    "diagnosticSessionId": diagnostic["id"],
                    "title": "No source project authority",
                    "targets": [
                        {
                            "kind": "session",
                            "id": source["id"],
                            "title": "Unbound source Session",
                        }
                    ],
                }
            )

    def test_trace_diagnostic_report_rejects_wrong_source_project_binding(self) -> None:
        source_root = self.root / "source-project"
        wrong_root = self.root / "wrong-project"
        source_root.mkdir()
        wrong_root.mkdir()
        source = self.service.create_session(
            {
                "title": "Source project Session",
                "mode": "coordinator",
                "workspaceRoots": [str(source_root)],
            }
        )["session"]
        diagnostic = self.service.create_session(
            {
                "title": "Wrongly bound Trace diagnostic",
                "surfaceKind": "extension_app",
                "ownerAppId": "extension:trace-agent",
                "surfaceKey": "diagnostic",
                "mode": "coordinator",
                "toolProfileVersion": "control-center-auto-approve-v1",
                "executionMode": "full_trust",
                "dangerousModeConfirmation": "ENABLE_FULL_TRUST",
                "workspaceRoots": [str(wrong_root)],
                "toolAllowlistMode": "profile",
                "projectContextEnabled": True,
                "piSkillsEnabled": True,
                "codexSkillsEnabled": True,
            }
        )["session"]
        source_snapshot = {
            "ok": True,
            "sessionId": source["id"],
            "status": "idle",
            "items": [],
            "liveEvents": [],
        }

        with (
            patch.object(self.service, "messages", return_value=source_snapshot),
            self.assertRaisesRegex(ValueError, "project workspace binding"),
        ):
            self.service.create_trace_diagnostic_report(
                {
                    "diagnosticSessionId": diagnostic["id"],
                    "title": "Must reject wrong binding",
                    "targets": [
                        {
                            "kind": "session",
                            "id": source["id"],
                            "title": "Source project Session",
                        }
                    ],
                }
            )

    def test_trace_diagnostic_finalize_requires_exact_revision(self) -> None:
        session = self.service.create_session(
            {
                "title": "Strict Trace diagnostic",
                "surfaceKind": "extension_app",
                "ownerAppId": "extension:trace-agent",
                "surfaceKey": "diagnostic",
                "mode": "coordinator",
                "toolProfileVersion": "control-center-auto-approve-v1",
                "executionMode": "full_trust",
                "dangerousModeConfirmation": "ENABLE_FULL_TRUST",
                "workspaceRoots": [str(self.root)],
                "toolAllowlistMode": "profile",
                "projectContextEnabled": True,
                "piSkillsEnabled": True,
                "codexSkillsEnabled": True,
            }
        )["session"]
        snapshot = {
            "ok": True,
            "sessionId": session["id"],
            "status": "idle",
            "items": [],
            "liveEvents": [],
        }
        with patch.object(self.service, "messages", return_value=snapshot):
            report = self.service.create_trace_diagnostic_report(
                {
                    "diagnosticSessionId": session["id"],
                    "title": "Strict report",
                    "targets": [{"kind": "session", "id": session["id"], "title": "Strict"}],
                }
            )
        for payload in ({}, {"expectedRevision": 0}, {"expectedRevision": "1"}):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValueError, "requires expectedRevision"):
                    self.service.finalize_trace_diagnostic_report(report["reportId"], payload)

    def test_trace_diagnostic_reconcile_fails_admitted_idle_turn_without_result(self) -> None:
        session = self.service.create_session(
            {
                "title": "Admitted Trace diagnostic",
                "surfaceKind": "extension_app",
                "ownerAppId": "extension:trace-agent",
                "surfaceKey": "diagnostic",
                "mode": "coordinator",
                "toolProfileVersion": "control-center-auto-approve-v1",
                "executionMode": "full_trust",
                "dangerousModeConfirmation": "ENABLE_FULL_TRUST",
                "workspaceRoots": [str(self.root)],
                "toolAllowlistMode": "profile",
                "projectContextEnabled": True,
                "piSkillsEnabled": True,
                "codexSkillsEnabled": True,
            }
        )["session"]
        pre_prompt_snapshot = {
            "ok": True,
            "sessionId": session["id"],
            "status": "idle",
            "items": [],
            "liveEvents": [],
        }
        with patch.object(self.service, "messages", return_value=pre_prompt_snapshot):
            report = self.service.create_trace_diagnostic_report(
                {
                    "diagnosticSessionId": session["id"],
                    "title": "Admitted turn without result",
                    "targets": [
                        {
                            "kind": "session",
                            "id": session["id"],
                            "title": "Admitted Trace diagnostic",
                        }
                    ],
                }
            )
        admitted_idle_snapshot = {
            "ok": True,
            "sessionId": session["id"],
            "status": "idle",
            "items": [
                {
                    "messageId": "message:user:admitted",
                    "turnId": "turn:admitted",
                    "role": "user",
                    "status": "completed",
                    "createdAtMs": report["createdAtMs"] + 1,
                    "timelineSequence": 1,
                    "blocks": [
                        {
                            "status": "completed",
                            "data": {"text": "分析这次真实失败并输出结构化报告。"},
                        }
                    ],
                }
            ],
            "liveEvents": [],
        }

        older_idle_snapshot = {
            **admitted_idle_snapshot,
            "items": [
                {
                    **admitted_idle_snapshot["items"][0],
                    "messageId": "message:user:older",
                    "turnId": "turn:older",
                    "createdAtMs": report["createdAtMs"] - 1,
                }
            ],
        }

        with patch.object(
            self.service,
            "messages",
            return_value=older_idle_snapshot,
        ):
            before_current_prompt = self.service.trace_diagnostic_report(
                report["reportId"]
            )

        with patch.object(
            self.service,
            "messages",
            return_value=admitted_idle_snapshot,
        ):
            reconciled = self.service.trace_diagnostic_report(report["reportId"])

        self.assertEqual(report["status"], "generating")
        self.assertEqual(before_current_prompt["status"], "generating")
        self.assertEqual(reconciled["status"], "failed")
        self.assertEqual(reconciled["revision"], report["revision"] + 1)
        self.assertIn("未生成可校验的结构化报告", reconciled["failureReason"])
        self.assertIsNone(reconciled["result"])

    def test_trace_diagnostic_reconcile_keeps_pre_prompt_idle_report_generating(self) -> None:
        session = self.service.create_session(
            {
                "title": "Pre-prompt Trace diagnostic",
                "surfaceKind": "extension_app",
                "ownerAppId": "extension:trace-agent",
                "surfaceKey": "diagnostic",
                "mode": "coordinator",
                "toolProfileVersion": "control-center-auto-approve-v1",
                "executionMode": "full_trust",
                "dangerousModeConfirmation": "ENABLE_FULL_TRUST",
                "workspaceRoots": [str(self.root)],
                "toolAllowlistMode": "profile",
                "projectContextEnabled": True,
                "piSkillsEnabled": True,
                "codexSkillsEnabled": True,
            }
        )["session"]
        pre_prompt_snapshot = {
            "ok": True,
            "sessionId": session["id"],
            "status": "idle",
            "items": [],
            "liveEvents": [],
        }

        with patch.object(self.service, "messages", return_value=pre_prompt_snapshot):
            report = self.service.create_trace_diagnostic_report(
                {
                    "diagnosticSessionId": session["id"],
                    "title": "Not prompted yet",
                    "targets": [
                        {
                            "kind": "session",
                            "id": session["id"],
                            "title": "Pre-prompt Trace diagnostic",
                        }
                    ],
                }
            )

        with patch.object(self.service, "messages", return_value=pre_prompt_snapshot):
            reconciled = self.service.trace_diagnostic_report(report["reportId"])

        self.assertEqual(reconciled, report)

    def test_trace_diagnostic_inspection_rejects_malformed_eval_projection(self) -> None:
        def consume_eval_reader(**kwargs):
            kwargs["eval_reader"]("trace:malformed")
            raise AssertionError("malformed Eval projection should have failed")

        with (
            patch.object(self.service, "observation_evals", return_value={"items": [{}, "bad"]}),
            patch("rag_ime.agent_service.inspect_trace_targets", side_effect=consume_eval_reader),
        ):
            with self.assertRaisesRegex(ValueError, "Eval items must be objects"):
                self.service.trace_diagnostic_inspection(
                    {"targets": [{"kind": "run", "id": "run:1", "title": "Run"}]}
                )

    def test_faulted_diagnostic_session_persists_failed_report_with_redacted_reason(self) -> None:
        session = self.service.create_session(
            {
                "title": "Faulted trace diagnostic",
                "surfaceKind": "extension_app",
                "ownerAppId": "extension:trace-agent",
                "surfaceKey": "diagnostic",
                "mode": "coordinator",
                "toolProfileVersion": "control-center-auto-approve-v1",
                "executionMode": "full_trust",
                "dangerousModeConfirmation": "ENABLE_FULL_TRUST",
                "workspaceRoots": [str(self.root)],
                "toolAllowlistMode": "profile",
                "projectContextEnabled": True,
                "piSkillsEnabled": True,
                "codexSkillsEnabled": True,
            }
        )["session"]
        idle_snapshot = {
            "ok": True,
            "sessionId": session["id"],
            "status": "idle",
            "items": [],
            "liveEvents": [],
        }
        faulted_snapshot = {
            "ok": True,
            "sessionId": session["id"],
            "status": "faulted",
            "items": [],
            "liveEvents": [
                {
                    "eventType": "turn_failed",
                    "payload": {
                        "error": (
                            "Pi exited while reading /Users/undo/private/session.json "
                            "Authorization: sk-faulted-secret"
                        )
                    },
                }
            ],
        }

        with patch.object(self.service, "messages", side_effect=[idle_snapshot, faulted_snapshot]):
            report = self.service.create_trace_diagnostic_report(
                {
                    "diagnosticSessionId": session["id"],
                    "title": "故障报告",
                    "targets": [
                        {
                            "kind": "session",
                            "id": session["id"],
                            "title": "Faulted trace diagnostic",
                        }
                    ],
                }
            )
            failed = self.service.finalize_trace_diagnostic_report(
                report["reportId"],
                {"expectedRevision": report["revision"]},
            )

        self.assertEqual(failed["status"], "failed")
        self.assertIn("faulted", failed["failureReason"])
        self.assertNotIn("/Users/undo", failed["failureReason"])
        self.assertNotIn("sk-faulted-secret", failed["failureReason"])
        self.assertIsNone(failed["result"])

    def test_diagnostic_terminal_failure_aliases_persist_failed_reports(self) -> None:
        for terminal_status in ("failed", "error", "cancelled", "canceled"):
            with self.subTest(terminal_status=terminal_status):
                session = self.service.create_session(
                    {
                        "title": f"{terminal_status} trace diagnostic",
                        "surfaceKind": "extension_app",
                        "ownerAppId": "extension:trace-agent",
                        "surfaceKey": "diagnostic",
                        "mode": "coordinator",
                        "toolProfileVersion": "control-center-auto-approve-v1",
                        "executionMode": "full_trust",
                        "dangerousModeConfirmation": "ENABLE_FULL_TRUST",
                        "workspaceRoots": [str(self.root)],
                        "toolAllowlistMode": "profile",
                        "projectContextEnabled": True,
                        "piSkillsEnabled": True,
                        "codexSkillsEnabled": True,
                    }
                )["session"]
                idle_snapshot = {
                    "ok": True,
                    "sessionId": session["id"],
                    "status": "idle",
                    "items": [],
                    "liveEvents": [],
                }
                terminal_snapshot = {
                    "ok": True,
                    "sessionId": session["id"],
                    "status": terminal_status,
                    "items": [],
                    "liveEvents": [],
                }
                with patch.object(
                    self.service,
                    "messages",
                    side_effect=[idle_snapshot, terminal_snapshot],
                ):
                    report = self.service.create_trace_diagnostic_report(
                        {
                            "diagnosticSessionId": session["id"],
                            "title": f"{terminal_status} report",
                            "targets": [
                                {
                                    "kind": "session",
                                    "id": session["id"],
                                    "title": f"{terminal_status} trace diagnostic",
                                }
                            ],
                        }
                    )
                    failed = self.service.finalize_trace_diagnostic_report(
                        report["reportId"],
                        {"expectedRevision": report["revision"]},
                    )

                self.assertEqual(failed["status"], "failed")
                self.assertIn(terminal_status, failed["failureReason"])
                self.assertIsNone(failed["result"])

    def test_trace_diagnostic_inspection_rejects_non_object_nested_target(self) -> None:
        with patch("rag_ime.agent_service.inspect_trace_targets") as inspector:
            with self.assertRaisesRegex(ValueError, "targets.*objects"):
                self.service.trace_diagnostic_inspection(
                    {
                        "targets": [
                            {"kind": "session", "id": "session:a", "title": "A"},
                            "not-a-target",
                        ]
                    }
                )
        inspector.assert_not_called()

    def test_trace_diagnostic_inspection_rejects_malformed_nested_trace_ids(self) -> None:
        with patch("rag_ime.agent_service.inspect_trace_targets") as inspector:
            with self.assertRaisesRegex(ValueError, "traceIds.*strings"):
                self.service.trace_diagnostic_inspection(
                    {
                        "targets": [
                            {
                                "kind": "session",
                                "id": "session:a",
                                "title": "A",
                                "traceIds": [{"not": "a string"}],
                            }
                        ]
                    }
                )
        inspector.assert_not_called()

    def test_background_job_recovery_publishes_after_event_projection_is_ready(self) -> None:
        recovered_db = self.root / "background-recovery.sqlite"
        session_store = AgentSessionStore(recovered_db)
        session_store.initialize()
        recovered_session_id = str(
            session_store.create(title="background recovery")["id"]
        )

        def publish_recovered_job(background_jobs: AgentBackgroundJobService) -> None:
            background_jobs.events(
                recovered_session_id,
                "background_job_completed",
                {"jobId": "bg_recovered"},
            )

        with patch.object(
            AgentBackgroundJobService,
            "initialize",
            publish_recovered_job,
        ):
            recovered = AgentService(
                db_path=recovered_db,
                runtime_config=PiRuntimeConfig(
                    enabled=False,
                    executable=None,
                    agent_dir=self.root / "background-recovery-agent",
                    session_dir=self.root / "background-recovery-sessions",
                    logs_dir=self.root / "background-recovery-logs",
                ),
            )
        try:
            replay, gap = recovered.events.replay(recovered_session_id)
            self.assertFalse(gap)
            self.assertEqual(
                [event.event_type for event in replay],
                ["background_job_completed"],
            )
        finally:
            recovered.close()





    def test_debug_context_forwards_the_runtime_payload(self) -> None:
        session = self.service.create_session({"title": "debug context"})[
            "session"
        ]
        expected = {
            "schemaVersion": "rag-ime.pi-debug-context-response.v1",
            "sessionId": str(session["id"]),
            "turnId": "turn:one",
            "available": True,
            "context": {"providerRequestReceipts": []},
        }
        with patch.object(
            self.service.runtime,
            "debug_context",
            create=True,
            return_value=expected,
        ) as debug_context:
            response = self.service.debug_context(
                str(session["id"]),
                "turn:one",
            )

        self.assertEqual(response, expected)
        debug_context.assert_called_once_with(
            str(session["id"]),
            "turn:one",
        )

    def test_runtime_and_session_crud_are_typed(self) -> None:
        runtime = self.service.runtime_status()
        self.assertEqual(runtime["schemaVersion"], "rag-ime.agent-runtime.v1")
        self.assertEqual(runtime["status"], "disabled")
        roles = self.service.list_roles()
        self.assertEqual(roles["items"][0]["displayName"], "Agent 3")
        self.assertEqual(
            [item["roleId"] for item in roles["items"]],
            ["companion-future-v1", "companion-present-v1", "companion-firstlight-v1", "companion-flash-v1"],
        )
        self.assertNotIn("systemPrompt", roles["items"][0])

        created = self.service.create_session({"title": " 连续   对话 "})
        session = created["session"]
        session_id = str(session["id"])
        self.assertEqual(session["title"], "连续 对话")
        self.assertFalse(session["projectContextEnabled"])
        self.assertFalse(session["piSkillsEnabled"])
        self.assertFalse(session["codexSkillsEnabled"])

        listed = self.service.list_sessions()
        self.assertEqual(listed["items"][0]["id"], session_id)
        directory_item = self.service.list_sessions({"projectionOnly": True})["items"][0]
        self.assertEqual(directory_item["id"], session_id)
        self.assertNotIn("sessionFile", directory_item)
        self.assertNotIn("runtimeBinding", directory_item)
        renamed = self.service.update_session(session_id, {"title": "检索会话"})
        self.assertEqual(renamed["session"]["title"], "检索会话")
        archived = self.service.update_session(session_id, {"archived": True})
        self.assertEqual(archived["session"]["status"], "archived")
        restored = self.service.update_session(session_id, {"archived": False})
        self.assertEqual(restored["session"]["status"], "idle")
        coordinator = self.service.update_session(
            session_id,
            {"mode": "coordinator", "workspaceRoots": [self.root.as_posix()]},
        )
        self.assertEqual(coordinator["session"]["mode"], "coordinator")
        self.assertEqual(coordinator["session"]["shellPolicyVersion"], "coordinator-per-command-v1")
        controlled = self.service.update_session(session_id, {"mode": "assistant"})
        self.assertEqual(controlled["session"]["mode"], "assistant")

        self.assertEqual(controlled["session"]["workspaceRoots"], [])
        restricted = self.service.update_session(
            session_id,
            {
                "mode": "assistant",
                "toolProfileVersion": "subagent-readonly-v1",
                "allowedTools": ["overview", "memory"],
            },
        )["session"]
        self.assertEqual(restricted["toolProfileVersion"], "subagent-readonly-v1")
        self.assertEqual(restricted["toolAllowlistMode"], "explicit")
        self.assertEqual(restricted["allowedTools"], ["overview", "memory"])
        restored_profile = self.service.update_session(
            session_id,
            {
                "mode": "assistant",
                "toolProfileVersion": "control-center-v1",
                "toolAllowlistMode": "profile",
            },
        )["session"]
        self.assertEqual(restored_profile["toolProfileVersion"], "control-center-v1")
        self.assertEqual(restored_profile["toolAllowlistMode"], "profile")

        self.assertEqual(restored_profile["allowedTools"], [])
        context_disabled = self.service.update_session(
            session_id,
            {"mode": "assistant", "projectContextEnabled": False},
        )["session"]
        self.assertFalse(context_disabled["projectContextEnabled"])
        external_skills_enabled = self.service.update_session(
            session_id,
            {
                "mode": "assistant",
                "piSkillsEnabled": True,
                "codexSkillsEnabled": True,
            },
        )["session"]
        self.assertTrue(external_skills_enabled["piSkillsEnabled"])
        self.assertTrue(external_skills_enabled["codexSkillsEnabled"])
        self.assertFalse(external_skills_enabled["projectContextEnabled"])
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            self.service.update_session(
                session_id,
                {"mode": "assistant", "projectContextEnabled": "false"},
            )
        with self.assertRaisesRegex(ValueError, "codexSkillsEnabled must be a boolean"):
            self.service.update_session(
                session_id,
                {"mode": "assistant", "codexSkillsEnabled": "true"},
            )
        dangerous = self.service.update_session(
            session_id,
            {
                "mode": "coordinator",
                "workspaceRoots": [self.root.as_posix()],
                "toolProfileVersion": "control-center-auto-approve-v1",
            },
        )["session"]
        self.assertEqual(dangerous["executionMode"], "full_trust")
        self.assertEqual(
            dangerous["toolProfileVersion"],
            "control-center-auto-approve-v1",
        )
        self.assertEqual(
            dangerous["workspaceRoots"],
            [str(self.root.resolve()), "/"],
        )
        self.service.update_session(
            session_id,
            {
                "mode": "assistant",
                "toolProfileVersion": "control-center-v1",
                "toolAllowlistMode": "profile",
            },
        )
        with self.assertRaisesRegex(ValueError, "unsupported Agent tool allowlist mode"):
            self.service.update_session(
                session_id,
                {"mode": "assistant", "toolAllowlistMode": "unrestricted"},
            )
        with self.assertRaisesRegex(ValueError, "unknown Agent tool"):
            self.service.update_session(
                session_id,
                {"mode": "assistant", "allowedTools": ["untrusted_tool"]},
            )

        deleted = self.service.delete_session(session_id)
        self.assertTrue(deleted["ok"])
        self.assertEqual(self.service.list_sessions()["items"], [])

    def test_project_session_enables_pi_project_context_by_default(self) -> None:
        project_session = self.service.create_session(
            {
                "title": "项目对话",
                "mode": "coordinator",
                "workspaceRoots": [self.root.as_posix()],
            }
        )["session"]
        self.assertTrue(project_session["projectContextEnabled"])

        project_context_disabled = self.service.create_session(
            {
                "title": "显式关闭项目上下文",
                "mode": "coordinator",
                "workspaceRoots": [self.root.as_posix()],
                "projectContextEnabled": False,
            }
        )["session"]
        self.assertFalse(
            project_context_disabled["projectContextEnabled"]
        )



    def test_room_member_conversation_sessions_remain_visible_in_agent_list(self) -> None:
        room = self.service.create_room(
            {
                "title": "共享 Session",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {
                        "roleId": "companion-present-v1",
                        "roleVersion": "1",
                    },
                    {
                        "roleId": "companion-firstlight-v1",
                        "roleVersion": "1",
                    },
                ],
            }
        )["room"]
        member_session_ids = {
            str(participant["sessionId"])
            for participant in room["participants"]
        }

        listed = self.service.list_sessions()
        visible = {
            str(session["id"]): session
            for session in listed["items"]
        }

        self.assertTrue(member_session_ids.issubset(visible))
        self.assertEqual(
            {
                str(visible[session_id]["sessionKind"])
                for session_id in member_session_ids
            },
            {"conversation"},
        )
        for participant in room["participants"]:
            session = visible[str(participant["sessionId"])]
            self.assertEqual(
                session["roomParticipant"],
                {
                    "roomId": room["id"],
                    "participantId": participant["id"],
                    "status": "active",
                },
            )

    def test_execution_modes_require_scope_confirmation_and_keep_one_policy_owner(self) -> None:
        with self.assertRaisesRegex(ValueError, "workspace scope confirmation"):
            self.service.create_session(
                {
                    "title": "未确认托管",
                    "mode": "coordinator",
                    "executionMode": "workspace_managed",
                    "workspaceRoots": [self.root.as_posix()],
                }
            )
        session = self.service.create_session(
            {
                "title": "权限模式",
                "mode": "coordinator",
                "workspaceRoots": [self.root.as_posix()],
            }
        )["session"]
        session_id = str(session["id"])
        self.assertEqual(session["executionMode"], "per_action")

        with self.assertRaisesRegex(ValueError, "workspace scope confirmation"):
            self.service.update_session(
                session_id,
                {
                    "mode": "coordinator",
                    "executionMode": "workspace_managed",
                    "workspaceRoots": [self.root.as_posix()],
                },
            )
        managed = self.service.update_session(
            session_id,
            {
                "mode": "coordinator",
                "executionMode": "workspace_managed",
                "workspaceRoots": [self.root.as_posix()],
                "workspaceScopeConfirmation": "APPROVE_WORKSPACE_SCOPE",
            },
        )["session"]
        self.assertEqual(managed["executionMode"], "workspace_managed")
        self.assertTrue(managed["workspaceScopeGranted"])

        extra_root = self.root / "second"
        extra_root.mkdir()
        with self.assertRaisesRegex(ValueError, "workspace scope confirmation"):
            self.service.update_session(
                session_id,
                {
                    "mode": "coordinator",
                    "executionMode": "workspace_managed",
                    "workspaceRoots": [extra_root.as_posix()],
                },
            )
        read_only = self.service.update_session(
            session_id,
            {
                "mode": "coordinator",
                "executionMode": "read_only",
                "workspaceRoots": [self.root.as_posix()],
            },
        )["session"]
        self.assertEqual(read_only["executionMode"], "read_only")
        self.assertEqual(read_only["toolProfileVersion"], "subagent-readonly-v1")
        self.assertFalse(read_only["workspaceScopeGranted"])

        with self.assertRaisesRegex(ValueError, "explicit native confirmation"):
            self.service.update_session(
                session_id,
                {
                    "mode": "coordinator",
                    "executionMode": "full_trust",
                    "workspaceRoots": [self.root.as_posix()],
                },
            )
        full_trust = self.service.update_session(
            session_id,
            {
                "mode": "coordinator",
                "executionMode": "full_trust",
                "workspaceRoots": [self.root.as_posix()],
                "dangerousModeConfirmation": "ENABLE_FULL_TRUST",
            },
        )["session"]
        self.assertEqual(full_trust["executionMode"], "full_trust")
        self.assertEqual(full_trust["toolProfileVersion"], "control-center-v1")
        self.assertTrue(full_trust["workspaceScopeGranted"])

    def test_rich_history_hydrates_from_sidecar_after_restart_and_compacted_snapshot(self) -> None:
        session = self.service.create_session({"title": "Rich restart"})["session"]
        session_id = str(session["id"])
        rich = list(normalize_trusted_agent_blocks(
            [{
                "id": "table:restart",
                "type": "table",
                "data": {
                    "title": "重启恢复",
                    "columns": ["状态"],
                    "rows": [["raw-restart-secret"]],
                },
            }],
            source_kind="pi_runtime_event",
            source_ref=f"{session_id}:message:rich",
        ))
        message = {
            "schemaVersion": "rag-ime.agent-message.v1",
            "id": "message:rich",
            "sessionId": session_id,
            "turnId": "turn:rich",
            "role": "assistant",
            "status": "completed",
            "blocks": [
                {
                    "id": "text:rich", "type": "text", "status": "completed",
                    "presentationKind": "markdown", "data": {"text": "可读交付"},
                },
                *rich,
            ],
            "attachments": [],
            "citations": [],
            "createdAtMs": 10,
            "completedAtMs": 10,
        }
        self.service.agent_blocks.persist_message(message, created_at_ms=10)
        db_path = self.root / "rag-ime.sqlite"
        self.service.close()
        self.service = AgentService(
            db_path=db_path,
            runtime_config=PiRuntimeConfig(
                enabled=False,
                executable=None,
                agent_dir=self.root / "agent-config",
                session_dir=self.root / "sessions",
                logs_dir=self.root / "logs",
            ),
            process_id_provider=lambda: self.process_id,
        )
        with patch.object(
            self.service.runtime,
            "session_snapshot",
            create=True,
            return_value={"messages": [], "telemetry": None, "messageQueue": None},
        ):
            recovered = self.service.messages(session_id)["items"]

        self.assertEqual([block["type"] for block in recovered[0]["blocks"]], ["text", "table"])
        self.assertEqual(recovered[0]["blocks"][1]["data"]["rows"], [["raw-restart-secret"]])
        provider_text = provider_block_projection("可读交付", recovered[0]["blocks"][1:])
        self.assertIn("表格：重启恢复，1 行 1 列", provider_text)
        self.assertNotIn("raw-restart-secret", provider_text)

    def test_delete_stops_a_runtime_that_still_has_the_session_open(self) -> None:
        session = self.service.create_session({"title": "打开中的会话"})["session"]
        session_id = str(session["id"])
        with (
            patch.object(
                self.service,
                "runtime_status",
                return_value={
                    "activeSessionId": None,
                    "openSessionIds": [session_id],
                },
            ),
            patch.object(self.service.runtime, "stop") as stop,
        ):
            deleted = self.service.delete_session(session_id)

        self.assertTrue(deleted["ok"])
        stop.assert_called_once_with()

    def test_approval_context_uses_user_requests_and_excludes_agent_messages(self) -> None:
        session = self.service.create_session(
            {"title": "审批上下文测试"}
        )["session"]
        session_id = str(session["id"])
        with patch.object(
            self.service.message_snapshot,
            "messages",
            return_value={
                "messages": [
                    {
                        "id": "message:user",
                        "role": "user",
                        "content": "只修改授权工作区并完成验证。",
                        "createdAtMs": 10,
                    },
                    {
                        "id": "message:assistant",
                        "role": "assistant",
                        "content": "PRIMARY_AGENT_OUTPUT_MUST_BE_EXCLUDED",
                        "createdAtMs": 11,
                    },
                ]
            },
        ):
            context = self.service._approval_model_context(
                {"sessionId": session_id},
                session,
            )

        self.assertTrue(context["contextAvailable"])
        self.assertEqual(context["contextKind"], "session")
        self.assertEqual(context["contextId"], session_id)
        self.assertEqual(
            context["userRequests"],
            [
                {
                    "role": "user",
                    "text": "只修改授权工作区并完成验证。",
                    "turnId": "message:user",
                    "createdAtMs": 10,
                }
            ],
        )
        self.assertEqual(
            context["currentTask"]["activeUserRequest"],
            "只修改授权工作区并完成验证。",
        )
        self.assertNotIn(
            "PRIMARY_AGENT_OUTPUT_MUST_BE_EXCLUDED",
            json.dumps(context, ensure_ascii=False),
        )

    def test_approval_context_recovers_persisted_user_request_after_compaction(self) -> None:
        session = self.service.create_session(
            {"title": "压缩后的审批上下文"}
        )["session"]
        session_id = str(session["id"])
        self.service.memory_sources.checkpoint_user_message(
            session_id=session_id,
            pi_entry_id="pi-entry:before-compaction",
            turn_id="turn:before-compaction",
            text="请在授权工作区完成实现，并按当前目标继续。",
            created_at_ms=20,
        )

        with patch.object(
            self.service.message_snapshot,
            "messages",
            return_value={"items": []},
        ):
            context = self.service._approval_model_context(
                {"sessionId": session_id},
                session,
            )

        self.assertTrue(context["contextAvailable"])
        self.assertEqual(
            context["userRequests"],
            [{
                "role": "user",
                "text": "请在授权工作区完成实现，并按当前目标继续。",
                "turnId": "turn:before-compaction",
                "createdAtMs": 20,
            }],
        )
        self.assertEqual(
            context["currentTask"]["activeUserRequest"],
            "请在授权工作区完成实现，并按当前目标继续。",
        )



    def test_unrestricted_profiles_discard_legacy_allowlists_and_missing_roots(self) -> None:
        for profile, execution_mode in (
            ("control-center-full-access-v1", "per_action"),
            ("control-center-auto-approve-v1", "full_trust"),
        ):
            with self.subTest(profile=profile):
                session = self.service.create_session(
                    {"title": f"Unrestricted {execution_mode}"}
                )["session"]
                session_id = str(session["id"])
                self.service.update_session(
                    session_id,
                    {
                        "mode": "assistant",
                        "toolProfileVersion": "subagent-readonly-v1",
                        "allowedTools": ["overview"],
                    },
                )
                missing_root = self.root / f"missing-{execution_mode}"
                payload: dict[str, object] = {
                    "mode": "coordinator",
                    "executionMode": execution_mode,
                    "toolProfileVersion": profile,
                    "toolAllowlistMode": "explicit",
                    "allowedTools": ["overview"],
                    "workspaceRoots": [str(missing_root)],
                }
                if execution_mode == "full_trust":
                    self.assertNotIn("dangerousModeConfirmation", payload)

                unrestricted = self.service.update_session(
                    session_id,
                    payload,
                )["session"]

                self.assertEqual(unrestricted["toolProfileVersion"], profile)
                self.assertEqual(unrestricted["toolAllowlistMode"], "profile")
                self.assertEqual(unrestricted["allowedTools"], [])
                self.assertEqual(
                    unrestricted["workspaceRoots"],
                    [str(missing_root.resolve()), "/"],
                )
                self.assertTrue(unrestricted["workspaceScopeGranted"])
                self.assertTrue(unrestricted["projectContextEnabled"])
                self.assertTrue(unrestricted["piSkillsEnabled"])
                self.assertTrue(unrestricted["codexSkillsEnabled"])

    def test_unrestricted_profiles_persist_system_root_when_update_omits_roots(self) -> None:
        for profile, execution_mode in (
            ("control-center-full-access-v1", "per_action"),
            ("control-center-auto-approve-v1", "full_trust"),
        ):
            with self.subTest(profile=profile):
                session_id = str(
                    self.service.create_session(
                        {"title": f"System root {execution_mode}"}
                    )["session"]["id"]
                )
                unrestricted = self.service.update_session(
                    session_id,
                    {
                        "mode": "coordinator",
                        "executionMode": execution_mode,
                        "toolProfileVersion": profile,
                    },
                )["session"]

                self.assertEqual(unrestricted["workspaceRoots"], ["/"])

    def test_auto_approve_profile_ignores_hash_and_skips_model_arbiter(self) -> None:
        session = self.service.create_session({"title": "完全信任测试"})["session"]
        session_id = str(session["id"])
        self.service.update_session(
            session_id,
            {
                "mode": "coordinator",
                "workspaceRoots": [self.root.as_posix()],
                "toolProfileVersion": "control-center-auto-approve-v1",
                "dangerousModeConfirmation": "ENABLE_FULL_TRUST",
            },
        )
        approval = self.service.sessions.create_approval(
            session_id=session_id,
            tool_name="planning",
            operation="task_action",
            payload_sha256="a" * 64,
            preview={"title": "测试预览", "summary": "自动批准测试"},
            risk_level="R1",
        )
        self.service.bind_approval_executor(
            lambda decided: {
                "schemaVersion": "rag-ime.agent-operation-receipt.v1",
                "mutationApplied": True,
                "approvalId": decided["approvalId"],
                "toolId": decided["toolId"],
                "operation": decided["operation"],
                "summary": "自动批准已应用",
            }
        )

        with patch.object(
            self.service.approval_model,
            "decide",
            side_effect=AssertionError(
                "unrestricted automatic approval reached model arbitration"
            ),
        ) as decide:
            result = self.service.auto_approve_pending(
                {**approval, "payloadSha256": "b" * 64}
            )
        decide.assert_not_called()

        self.assertTrue(result["autoApproved"])
        self.assertFalse(result["approvalRequired"])
        self.assertEqual(result["approval"]["state"], "applied")
        self.assertEqual(result["decisionMode"], "policy")
        self.assertIsNotNone(
            self.service.sessions.get_approval(str(approval["approvalId"]))["decidedAtMs"]
        )

    def test_room_bound_automatic_approval_does_not_reenter_model_arbiter(self) -> None:
        session = self.service.create_session({"title": "Room 自动执行"})["session"]
        session_id = str(session["id"])
        self.service.update_session(
            session_id,
            {
                "mode": "coordinator",
                "workspaceRoots": [self.root.as_posix()],
                "executionMode": "full_trust",
                "toolProfileVersion": "control-center-v1",
                "grantWorkspaceScope": True,
                "dangerousModeConfirmation": "ENABLE_FULL_TRUST",
            },
        )
        self.service.sessions.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-room-auto",
        )
        approval = self.service.sessions.create_approval(
            session_id=session_id,
            tool_name="runtime",
            operation="restart_sidecar",
            payload_sha256="a" * 64,
            preview={"title": "重启 Runtime", "summary": "Room 自动执行"},
            risk_level="R3",
        )
        room_context = {
            "roomId": "room:auto",
            "rootId": "root:auto",
            "dispatchId": "dispatch:auto",
            "generation": 1,
        }
        approval = self.service.sessions.bind_approval_room_dispatch(
            str(approval["approvalId"]),
            room_context=room_context,
        )
        self.assertNotIn("approvalArbitration", approval["preview"])
        self.service.bind_approval_executor(
            lambda decided: {
                "schemaVersion": "rag-ime.agent-operation-receipt.v1",
                "mutationApplied": True,
                "approvalId": decided["approvalId"],
                "toolId": decided["toolId"],
                "operation": decided["operation"],
                "summary": "Room 操作已执行",
            }
        )

        with (
            patch.object(
                self.service,
                "_active_room_dispatch_context",
                return_value=room_context,
            ),
            patch.object(
                self.service.approval_model,
                "decide",
                side_effect=AssertionError("Room approval reached the model arbiter"),
            ) as decide,
        ):
            result = self.service.auto_approve_pending(approval)

        decide.assert_not_called()
        self.assertTrue(result["autoApproved"])
        self.assertEqual(result["decisionMode"], "policy")
        self.assertEqual(result["approval"]["state"], "applied")

    def test_partial_legacy_room_binding_never_authorizes_tool_execution(self) -> None:
        session = self.service.create_session({"title": "Legacy Room binding"})["session"]
        session_id = str(session["id"])
        self.service.update_session(
            session_id,
            {
                "mode": "coordinator",
                "workspaceRoots": [self.root.as_posix()],
                "executionMode": "full_trust",
                "toolProfileVersion": "control-center-v1",
                "grantWorkspaceScope": True,
                "dangerousModeConfirmation": "ENABLE_FULL_TRUST",
            },
        )
        approval = self.service.sessions.create_approval(
            session_id=session_id,
            tool_name="workspace_shell",
            operation="run",
            payload_sha256="c" * 64,
            preview={"title": "旧 Room 操作", "summary": "绑定字段不完整"},
            risk_level="R2",
        )
        approval_id = str(approval["approvalId"])
        with closing(sqlite3.connect(self.root / "rag-ime.sqlite")) as conn:
            conn.execute(
                """
                UPDATE agent_approvals
                SET room_bound = 1, room_id = ?, room_root_id = ?,
                    room_dispatch_id = '', room_generation = 0
                WHERE approval_id = ?
                """,
                ("room:legacy", "root:legacy", approval_id),
            )
            conn.commit()
        partial = self.service.sessions.get_approval(approval_id)
        incomplete_live = {
            "roomId": "room:legacy",
            "rootId": "root:legacy",
            "dispatchId": "",
            "generation": 0,
        }
        executed: list[dict[str, object]] = []
        self.service.bind_approval_executor(
            lambda decided: executed.append(dict(decided)) or {
                "schemaVersion": "rag-ime.agent-operation-receipt.v1",
                "mutationApplied": True,
                "summary": "不应执行",
            }
        )

        with (
            patch.object(
                self.service,
                "_active_room_dispatch_context",
                return_value=incomplete_live,
            ),
            patch.object(
                self.service.approval_model,
                "decide",
                side_effect=AssertionError(
                    "partial Room binding reached model arbitration"
                ),
            ) as decide,
        ):
            result = self.service.auto_approve_pending(partial)

        decide.assert_not_called()
        self.assertEqual(executed, [])
        self.assertTrue(result["terminal"])
        self.assertFalse(result["autoApproved"])
        self.assertEqual(result["approval"]["state"], "rejected")
        self.assertEqual(
            result["approval"]["decidedBy"],
            "execution-policy:room_dispatch_stale",
        )

    def test_room_bound_approval_rechecks_read_only_before_execution(self) -> None:
        session = self.service.create_session({"title": "Room policy race"})["session"]
        session_id = str(session["id"])
        self.service.update_session(
            session_id,
            {
                "mode": "coordinator",
                "workspaceRoots": [self.root.as_posix()],
                "executionMode": "full_trust",
                "toolProfileVersion": "control-center-v1",
                "grantWorkspaceScope": True,
                "dangerousModeConfirmation": "ENABLE_FULL_TRUST",
            },
        )
        self.service.sessions.set_room_execution_mode(
            session_id,
            "room_unrestricted",
        )
        approval = self.service.sessions.create_approval(
            session_id=session_id,
            tool_name="runtime",
            operation="restart_sidecar",
            payload_sha256="a" * 64,
            preview={"title": "重启 Runtime", "summary": "Room 自动执行"},
            risk_level="R3",
        )
        self.assertIn("approvalArbitration", approval["preview"])
        room_context = {
            "roomId": "room:race",
            "rootId": "root:race",
            "dispatchId": "dispatch:race",
            "generation": 1,
        }
        approval = self.service.sessions.bind_approval_room_dispatch(
            str(approval["approvalId"]),
            room_context=room_context,
        )
        executed: list[dict[str, object]] = []
        self.service.bind_approval_executor(
            lambda decided: executed.append(dict(decided)) or {
                "schemaVersion": "rag-ime.agent-operation-receipt.v1",
                "mutationApplied": True,
                "approvalId": decided["approvalId"],
                "toolId": decided["toolId"],
                "operation": decided["operation"],
                "summary": "不应执行",
            }
        )
        self.service.update_session(
            session_id,
            {
                "executionMode": "read_only",
                "toolProfileVersion": "subagent-readonly-v1",
            },
        )

        with patch.object(
            self.service,
            "_active_room_dispatch_context",
            return_value=room_context,
        ):
            result = self.service.auto_approve_pending(approval)

        self.assertEqual(executed, [])
        self.assertFalse(result["autoApproved"])
        self.assertTrue(result["terminal"])
        self.assertEqual(result["approval"]["state"], "rejected")
        self.assertEqual(
            result["approval"]["decidedBy"],
            "execution-policy:read_only",
        )

    def test_room_execution_prompt_is_derived_only_from_a_live_dispatch(self) -> None:
        session = self.service.create_session({"title": "Room prompt lease"})["session"]
        session_id = str(session["id"])
        self.service.update_session(
            session_id,
            {
                "mode": "coordinator",
                "executionMode": "per_action",
                "toolProfileVersion": "control-center-v1",
            },
        )
        self.service.sessions.set_room_execution_mode(
            session_id,
            "room_unrestricted",
        )
        persisted = self.service.sessions.get(session_id)
        live_context = {
            "roomId": "room:prompt",
            "rootId": "root:prompt",
            "dispatchId": "dispatch:prompt",
            "generation": 3,
        }

        with patch.object(
            self.service,
            "_active_room_dispatch_context",
            return_value=None,
        ):
            direct = str(
                self.service._runtime_session_context(persisted).get(
                    "sessionContext"
                )
                or ""
            )
        with patch.object(
            self.service,
            "_active_room_dispatch_context",
            return_value=live_context,
        ):
            room = str(
                self.service._runtime_session_context(persisted).get(
                    "sessionContext"
                )
                or ""
            )

        self.assertNotIn('room-mode="room_unrestricted"', direct)
        self.assertIn('room-mode="room_unrestricted"', room)

        delivered_messages: list[str] = []

        def deliver_with(room_context, client_message_id: str) -> str:
            with (
                patch.object(
                    self.service,
                    "_active_room_dispatch_context",
                    return_value=room_context,
                ),
                patch.object(
                    self.service.runtime,
                    "prompt",
                    return_value={
                        "accepted": True,
                        "turnId": f"turn:{client_message_id}",
                        "piEntryId": f"entry:{client_message_id}",
                        "response": {"success": True},
                    },
                ) as runtime_prompt,
            ):
                self.service.prompt_delivery_application.deliver(
                    session_id,
                    "执行 Room 当前任务",
                    client_message_id=client_message_id,
                    source_kind="room",
                )
            sent = str(runtime_prompt.call_args.args[1])
            self.assertTrue(sent.startswith(RUNTIME_PROMPT_ENVELOPE_PREFIX))
            delivered_messages.append(sent)
            return str(
                json.loads(sent[len(RUNTIME_PROMPT_ENVELOPE_PREFIX):]).get(
                    "sessionContext"
                )
                or ""
            )

        direct_delivery = deliver_with(None, "direct-policy")
        room_delivery = deliver_with(live_context, "room-policy")

        self.assertNotIn('room-mode="room_unrestricted"', direct_delivery)
        self.assertIn('room-mode="room_unrestricted"', room_delivery)
        self.assertNotIn("Luna", room_delivery)
        self.assertNotIn("人工", room_delivery)
        self.assertEqual(len(delivered_messages), 2)

    def test_room_execution_prompt_rejects_an_incomplete_live_binding(self) -> None:
        session = self.service.create_session(
            {"title": "Room prompt exact binding"}
        )["session"]
        session_id = str(session["id"])
        self.service.sessions.set_room_execution_mode(
            session_id,
            "room_unrestricted",
        )

        malformed = (
            {
                "roomId": "room:prompt",
                "rootId": "root:prompt",
                "dispatchId": "",
                "generation": 1,
            },
            {
                "roomId": "room:prompt",
                "rootId": "root:prompt",
                "dispatchId": "dispatch:prompt",
                "generation": 0,
            },
        )
        for context in malformed:
            with self.subTest(context=context):
                with patch.object(
                    self.service,
                    "_active_room_dispatch_context",
                    return_value=context,
                ):
                    prompt = str(
                        self.service._runtime_session_context(session).get(
                            "sessionContext"
                        )
                        or ""
                    )
                self.assertNotIn('room-mode="room_unrestricted"', prompt)
                self.assertNotIn("不创建任何二次裁决或确认流程", prompt)

    def test_unbound_direct_approval_does_not_inherit_a_stale_room_overlay(self) -> None:
        session = self.service.create_session({"title": "Direct after Room"})["session"]
        session_id = str(session["id"])
        self.service.update_session(
            session_id,
            {
                "mode": "coordinator",
                "executionMode": "per_action",
                "toolProfileVersion": "control-center-v1",
            },
        )
        self.service.sessions.set_room_execution_mode(
            session_id,
            "room_unrestricted",
        )
        approval = self.service.sessions.create_approval(
            session_id=session_id,
            tool_name="runtime",
            operation="restart_sidecar",
            payload_sha256="d" * 64,
            preview={"title": "重启 Runtime", "summary": "普通 Session 操作"},
            risk_level="R3",
        )
        self.service.bind_approval_executor(
            lambda _approval: (_ for _ in ()).throw(
                AssertionError("unbound direct approval executed automatically")
            )
        )

        with (
            patch.object(
                self.service,
                "_active_room_dispatch_context",
                return_value=None,
            ),
            patch.object(
                self.service.approval_model,
                "decide",
                side_effect=AssertionError("direct per-action approval reached Luna"),
            ) as decide,
        ):
            result = self.service.auto_approve_pending(approval)

        decide.assert_not_called()
        self.assertTrue(result["approvalRequired"])
        self.assertFalse(result["autoApproved"])
        self.assertEqual(result["decisionMode"], "manual")
        self.assertEqual(result["approval"]["state"], "pending")

    def test_stale_room_dispatch_neither_executes_nor_falls_back_to_luna(self) -> None:
        session = self.service.create_session({"title": "Stale Room dispatch"})["session"]
        session_id = str(session["id"])
        self.service.update_session(
            session_id,
            {
                "mode": "coordinator",
                "workspaceRoots": [self.root.as_posix()],
                "executionMode": "full_trust",
                "toolProfileVersion": "control-center-v1",
                "grantWorkspaceScope": True,
                "dangerousModeConfirmation": "ENABLE_FULL_TRUST",
            },
        )
        approval = self.service.sessions.create_approval(
            session_id=session_id,
            tool_name="runtime",
            operation="restart_sidecar",
            payload_sha256="a" * 64,
            preview={"title": "重启 Runtime", "summary": "过期 Room 操作"},
            risk_level="R3",
        )
        room_context = {
            "roomId": "room:stale",
            "rootId": "root:old",
            "dispatchId": "dispatch:old",
            "generation": 1,
        }
        approval = self.service.sessions.bind_approval_room_dispatch(
            str(approval["approvalId"]),
            room_context=room_context,
        )
        executed: list[dict[str, object]] = []
        self.service.bind_approval_executor(
            lambda decided: executed.append(dict(decided)) or {}
        )

        with (
            patch.object(
                self.service,
                "_active_room_dispatch_context",
                return_value={**room_context, "rootId": "root:new"},
            ),
            patch.object(
                self.service.approval_model,
                "decide",
                side_effect=AssertionError("stale Room approval reached Luna"),
            ) as decide,
        ):
            result = self.service.auto_approve_pending(approval)

        decide.assert_not_called()
        self.assertEqual(executed, [])
        self.assertFalse(result["autoApproved"])
        self.assertEqual(result["approval"]["state"], "rejected")
        self.assertEqual(
            result["approval"]["decidedBy"],
            "execution-policy:room_dispatch_stale",
        )

    def test_room_stop_wins_between_policy_decision_and_tool_execution(self) -> None:
        session = self.service.create_session({"title": "Room Stop race"})["session"]
        session_id = str(session["id"])
        self.service.update_session(
            session_id,
            {
                "mode": "coordinator",
                "workspaceRoots": [self.root.as_posix()],
                "executionMode": "per_action",
                "toolProfileVersion": "control-center-v1",
                "grantWorkspaceScope": True,
            },
        )
        approval = self.service.sessions.create_approval(
            session_id=session_id,
            tool_name="runtime",
            operation="restart_sidecar",
            payload_sha256="a" * 64,
            preview={"title": "重启 Runtime", "summary": "Room 自动执行"},
            risk_level="R3",
        )
        room_context = {
            "roomId": "room:stop-race",
            "rootId": "root:stop-race",
            "dispatchId": "dispatch:stop-race",
            "generation": 3,
        }
        approval = self.service.sessions.bind_approval_room_dispatch(
            str(approval["approvalId"]),
            room_context=room_context,
        )
        executed: list[dict[str, object]] = []
        self.service.bind_approval_executor(
            lambda decided: executed.append(dict(decided)) or {
                "schemaVersion": "rag-ime.agent-operation-receipt.v1",
                "mutationApplied": True,
                "approvalId": decided["approvalId"],
                "toolId": decided["toolId"],
                "operation": decided["operation"],
                "summary": "不应执行",
            }
        )
        decide = self.service.sessions.decide_approval

        def decide_then_stop(*args, **kwargs):
            decided = decide(*args, **kwargs)
            self.service.sessions.cancel_pending_approvals(
                session_id,
                reason="user_abort",
                turn_id="root:stop-race",
            )
            return decided

        with (
            patch.object(
                self.service,
                "_active_room_dispatch_context",
                return_value=room_context,
            ),
            patch.object(
                self.service.sessions,
                "decide_approval",
                side_effect=decide_then_stop,
            ),
        ):
            result = self.service.auto_approve_pending(approval)

        self.assertEqual(executed, [])
        self.assertFalse(result["autoApproved"])
        self.assertEqual(result["approval"]["state"], "stale")
        self.assertEqual(
            result["approval"]["decidedBy"],
            "runtime-cancellation",
        )

    def test_room_cancelled_root_wins_before_approval_execution_claim(self) -> None:
        room = self.service.create_room(
            {
                "title": "Room cancellation claim race",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        participant = room["participants"][0]
        session_id = str(participant["sessionId"])
        self.service.sessions.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-room-cancel-race",
        )
        root_id = "root:cancel-race"
        dispatch_id = "dispatch:cancel-race"
        self.service.room_turns.begin(
            session_id,
            root_id,
            dispatch_id=dispatch_id,
        )
        room_context = self.service._active_room_dispatch_context(session_id)
        self.assertIsNotNone(room_context)
        approval = self.service.sessions.create_approval(
            session_id=session_id,
            tool_name="runtime",
            operation="restart_sidecar",
            payload_sha256="a" * 64,
            preview={"title": "重启 Runtime", "summary": "Room 自动执行"},
            risk_level="R3",
        )
        approval = self.service.sessions.bind_approval_room_dispatch(
            str(approval["approvalId"]),
            room_context=room_context or {},
        )
        executed: list[dict[str, object]] = []
        self.service.bind_approval_executor(
            lambda decided: executed.append(dict(decided)) or {}
        )
        decide = self.service.sessions.decide_approval

        def decide_then_cancel_root(*args, **kwargs):
            decided = decide(*args, **kwargs)
            self.service.room_turns.record_cancellation(
                root_id,
                "room-cancellation:claim-race",
            )
            return decided

        with patch.object(
            self.service.sessions,
            "decide_approval",
            side_effect=decide_then_cancel_root,
        ):
            result = self.service.auto_approve_pending(approval)

        self.assertEqual(executed, [])
        self.assertFalse(result["autoApproved"])
        self.assertEqual(result["approval"]["state"], "stale")
        self.assertEqual(
            result["approval"]["decidedBy"],
            "execution-policy:room_dispatch_stale",
        )

    def test_room_read_only_change_wins_before_approval_execution_claim(self) -> None:
        session = self.service.create_session({"title": "Room read-only race"})["session"]
        session_id = str(session["id"])
        self.service.sessions.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-room-read-only-race",
        )
        room_context = {
            "roomId": "room:read-only-race",
            "rootId": "root:read-only-race",
            "dispatchId": "dispatch:read-only-race",
            "generation": 1,
        }
        approval = self.service.sessions.create_approval(
            session_id=session_id,
            tool_name="runtime",
            operation="restart_sidecar",
            payload_sha256="a" * 64,
            preview={"title": "重启 Runtime", "summary": "Room 自动执行"},
            risk_level="R3",
        )
        approval = self.service.sessions.bind_approval_room_dispatch(
            str(approval["approvalId"]),
            room_context=room_context,
        )
        executed: list[dict[str, object]] = []
        self.service.bind_approval_executor(
            lambda decided: executed.append(dict(decided)) or {}
        )
        decide = self.service.sessions.decide_approval

        def decide_then_read_only(*args, **kwargs):
            decided = decide(*args, **kwargs)
            self.service.update_session(
                session_id,
                {
                    "executionMode": "read_only",
                    "toolProfileVersion": "subagent-readonly-v1",
                },
            )
            return decided

        with (
            patch.object(
                self.service,
                "_active_room_dispatch_context",
                return_value=room_context,
            ),
            patch.object(
                self.service.sessions,
                "decide_approval",
                side_effect=decide_then_read_only,
            ),
        ):
            result = self.service.auto_approve_pending(approval)

        self.assertEqual(executed, [])
        self.assertFalse(result["autoApproved"])
        self.assertEqual(result["approval"]["state"], "rejected")
        self.assertEqual(
            result["approval"]["decidedBy"],
            "execution-policy:read_only",
        )

    def test_room_dispatch_rotation_wins_between_policy_decision_and_tool_execution(self) -> None:
        session = self.service.create_session({"title": "Room dispatch race"})["session"]
        session_id = str(session["id"])
        self.service.update_session(
            session_id,
            {
                "mode": "coordinator",
                "workspaceRoots": [self.root.as_posix()],
                "executionMode": "per_action",
                "toolProfileVersion": "control-center-v1",
                "grantWorkspaceScope": True,
            },
        )
        self.service.sessions.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-room-dispatch-race",
        )
        approval = self.service.sessions.create_approval(
            session_id=session_id,
            tool_name="runtime",
            operation="restart_sidecar",
            payload_sha256="a" * 64,
            preview={"title": "重启 Runtime", "summary": "Room 自动执行"},
            risk_level="R3",
        )
        original_context = {
            "roomId": "room:dispatch-race",
            "rootId": "root:old",
            "dispatchId": "dispatch:old",
            "generation": 1,
        }
        approval = self.service.sessions.bind_approval_room_dispatch(
            str(approval["approvalId"]),
            room_context=original_context,
        )
        executed: list[dict[str, object]] = []
        self.service.bind_approval_executor(
            lambda decided: executed.append(dict(decided)) or {
                "schemaVersion": "rag-ime.agent-operation-receipt.v1",
                "mutationApplied": True,
                "approvalId": decided["approvalId"],
                "toolId": decided["toolId"],
                "operation": decided["operation"],
                "summary": "不应执行",
            }
        )
        rotated_context = {
            **original_context,
            "rootId": "root:new",
            "dispatchId": "dispatch:new",
        }

        with patch.object(
            self.service,
            "_active_room_dispatch_context",
            side_effect=(original_context, rotated_context),
        ):
            result = self.service.auto_approve_pending(approval)

        self.assertEqual(executed, [])
        self.assertFalse(result["autoApproved"])
        self.assertEqual(result["approval"]["state"], "stale")
        self.assertEqual(
            result["approval"]["decidedBy"],
            "execution-policy:room_dispatch_stale",
        )

    def test_room_runtime_generation_rotation_wins_before_tool_execution(self) -> None:
        session = self.service.create_session({"title": "Room generation race"})["session"]
        session_id = str(session["id"])
        self.service.update_session(
            session_id,
            {
                "mode": "coordinator",
                "workspaceRoots": [self.root.as_posix()],
                "executionMode": "per_action",
                "toolProfileVersion": "control-center-v1",
                "grantWorkspaceScope": True,
            },
        )
        self.service.sessions.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-room-generation-1",
        )
        original_context = {
            "roomId": "room:generation-race",
            "rootId": "root:generation-race",
            "dispatchId": "dispatch:generation-race",
            "generation": 1,
        }
        approval = self.service.sessions.create_approval(
            session_id=session_id,
            tool_name="runtime",
            operation="restart_sidecar",
            payload_sha256="a" * 64,
            preview={"title": "重启 Runtime", "summary": "Room 自动执行"},
            risk_level="R3",
        )
        approval = self.service.sessions.bind_approval_room_dispatch(
            str(approval["approvalId"]),
            room_context=original_context,
        )
        executed: list[dict[str, object]] = []
        self.service.bind_approval_executor(
            lambda decided: executed.append(dict(decided)) or {}
        )
        decide = self.service.sessions.decide_approval

        def decide_then_rebind(*args, **kwargs):
            decided = decide(*args, **kwargs)
            self.service.sessions.bind_runtime_session(
                session_id,
                driver_id="managed-pi",
                runtime_kind="pi_rpc",
                external_session_id="pi-room-generation-2",
            )
            return decided

        with (
            patch.object(
                self.service,
                "_active_room_dispatch_context",
                return_value=original_context,
            ),
            patch.object(
                self.service.sessions,
                "decide_approval",
                side_effect=decide_then_rebind,
            ),
        ):
            result = self.service.auto_approve_pending(approval)

        self.assertEqual(executed, [])
        self.assertFalse(result["autoApproved"])
        self.assertEqual(result["approval"]["state"], "stale")
        self.assertEqual(
            result["approval"]["decidedBy"],
            "execution-policy:room_dispatch_stale",
        )

    def test_startup_recovery_closes_claimed_room_execution_without_replay(self) -> None:
        room = self.service.create_room(
            {
                "title": "Room crash recovery",
                "workspaceRoots": [self.root.as_posix()],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        participant = room["participants"][0]
        room_id = str(room["id"])
        session_id = str(participant["sessionId"])
        self.service.sessions.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-room-crash-recovery",
        )
        root_id = "root:crash-recovery"
        dispatch_id = "dispatch:crash-recovery"
        session_turn_id = "turn:crash-recovery"
        tool_call_id = "tool:crash-recovery"
        self.service.room_turns.begin(
            session_id,
            root_id,
            dispatch_id=dispatch_id,
        )
        self.service.room_turns.accept(
            session_id,
            session_turn_id,
            root_id,
        )
        room_context = self.service._active_room_dispatch_context(session_id)
        self.assertIsNotNone(room_context)
        self.service.events.publish(
            session_id,
            "tool_started",
            {
                "toolCallId": tool_call_id,
                "toolName": "workspace_shell",
                "summary": "执行可能产生外部效果的命令",
            },
            turn_id=session_turn_id,
        )
        approval = self.service.sessions.create_approval(
            session_id=session_id,
            tool_name="workspace_shell",
            operation="run",
            payload_sha256="e" * 64,
            preview={"title": "执行命令", "summary": "可能已产生外部效果"},
            risk_level="R2",
            causal_metadata={
                "turnId": session_turn_id,
                "roomBound": True,
            },
        )
        approval_id = str(approval["approvalId"])
        self.service.sessions.bind_approval_tool_call(
            approval_id,
            tool_call_id=tool_call_id,
        )
        self.service.sessions.bind_approval_room_dispatch(
            approval_id,
            room_context=room_context or {},
        )
        self.service.sessions.decide_approval(
            approval_id,
            approved=True,
            payload_sha256=str(approval["payloadSha256"]),
            decided_by="execution-policy:room_dispatch",
        )
        claimed = self.service.sessions.claim_approval_execution(
            approval_id,
            room_context=room_context or {},
        )
        self.assertEqual(claimed["state"], "approved")
        self.assertTrue(
            any(
                event.get("payload", {}).get("sourceEventType") == "tool_started"
                for event in self.service.rooms.list_events(room_id)
            )
        )

        # Simulate a process crash after the irreversible execution boundary
        # but before complete_approval persisted a terminal receipt.
        self.service.close()
        self.process_id += 1
        self.service = AgentService(
            db_path=self.root / "rag-ime.sqlite",
            runtime_config=PiRuntimeConfig(
                enabled=False,
                executable=None,
                agent_dir=self.root / "agent-config-restarted",
                session_dir=self.root / "sessions-restarted",
                logs_dir=self.root / "logs-restarted",
            ),
            process_id_provider=lambda: self.process_id,
            defer_startup_recovery=True,
        )

        # The approval row, durable Session terminal, and public Room terminal
        # are three separate commit boundaries.  Interrupt each projection in
        # turn and prove later startup attempts repair, rather than replay, the
        # claimed Tool execution.
        with patch.object(
            self.service.approval_application,
            "_project_recovered_session_execution",
            side_effect=RuntimeError("session terminal interrupted"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "session terminal interrupted",
            ):
                self.service.run_startup_recovery()
        self.assertEqual(self.service.startup_recovery_status()["status"], "failed")
        with closing(sqlite3.connect(self.root / "rag-ime.sqlite")) as conn:
            self.assertEqual(
                int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM agent_runtime_events
                        WHERE session_id = ? AND event_type = 'approval_resolved'
                        """,
                        (session_id,),
                    ).fetchone()[0]
                ),
                0,
            )

        with patch.object(
            self.service.approval_application,
            "_project_abandoned_room_execution",
            side_effect=RuntimeError("projection interrupted"),
        ):
            with self.assertRaisesRegex(RuntimeError, "projection interrupted"):
                self.service.run_startup_recovery()
        self.assertEqual(self.service.startup_recovery_status()["status"], "failed")
        self.service.run_startup_recovery()

        with closing(sqlite3.connect(self.root / "rag-ime.sqlite")) as conn:
            self.assertEqual(
                int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM agent_runtime_events
                        WHERE session_id = ? AND event_type = 'approval_resolved'
                        """,
                        (session_id,),
                    ).fetchone()[0]
                ),
                1,
            )

        recovered = self.service.sessions.get_approval(approval_id)
        self.assertEqual(recovered["state"], "failed")
        self.assertEqual(recovered["receipt"]["executionOutcome"], "unknown")
        self.assertTrue(recovered["receipt"]["effectMayHaveOccurred"])
        self.assertFalse(recovered["receipt"]["replayAllowed"])
        self.assertIsNone(recovered["receipt"]["mutationApplied"])
        self.assertEqual(
            self.service.startup_recovery_status()["approvalExecutionRecovery"],
            {
                "recoveredCount": 0,
                "approvalIds": [],
                "sessionReconciledCount": 0,
                "sessionApprovalIds": [],
                "projectionReconciledCount": 1,
                "projectionApprovalIds": [approval_id],
            },
        )
        terminal = self.service.auto_approve_pending(recovered)
        self.assertTrue(terminal["terminal"])
        self.assertFalse(terminal["autoApproved"])
        self.assertEqual(terminal["approval"]["state"], "failed")
        recovered_room_events = [
            event
            for event in self.service.rooms.list_events(room_id)
            if event.get("payload", {}).get("sourceEventType")
            == "approval_resolved"
            and event.get("payload", {}).get("data", {}).get("approvalId")
            == approval_id
        ]
        self.assertEqual(len(recovered_room_events), 1)
        recovered_data = recovered_room_events[0]["payload"]["data"]
        self.assertEqual(recovered_data["state"], "failed")
        self.assertEqual(recovered_data["executionOutcome"], "unknown")
        self.assertTrue(recovered_data["effectMayHaveOccurred"])
        self.assertFalse(recovered_data["replayAllowed"])
        first_session_events, _ = self.service.events.replay(session_id)
        self.assertEqual(
            len(
                [
                    event
                    for event in first_session_events
                    if event.event_type == "approval_resolved"
                    and event.payload.get("approvalId") == approval_id
                ]
            ),
            1,
        )

        # A later real Host process sees the durable failed row again, but
        # neither Session nor Room may gain a duplicate terminal receipt.
        self.service.close()
        self.process_id += 1
        self.service = AgentService(
            db_path=self.root / "rag-ime.sqlite",
            runtime_config=PiRuntimeConfig(
                enabled=False,
                executable=None,
                agent_dir=self.root / "agent-config-restarted-again",
                session_dir=self.root / "sessions-restarted-again",
                logs_dir=self.root / "logs-restarted-again",
            ),
            process_id_provider=lambda: self.process_id,
        )
        # A fresh Host owns a fresh in-memory event hub, so restart idempotency
        # must be asserted against the durable Runtime event ledger rather than
        # the new process's empty replay buffer.
        with closing(sqlite3.connect(self.root / "rag-ime.sqlite")) as conn:
            durable_terminal_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM agent_runtime_events
                    WHERE session_id = ? AND event_type = 'approval_resolved'
                    """,
                    (session_id,),
                ).fetchone()[0]
            )
        self.assertEqual(durable_terminal_count, 1)
        self.assertEqual(
            len(
                [
                    event
                    for event in self.service.rooms.list_events(room_id)
                    if event.get("payload", {}).get("sourceEventType")
                    == "approval_resolved"
                    and event.get("payload", {}).get("data", {}).get(
                        "approvalId"
                    )
                    == approval_id
                ]
            ),
            1,
        )

    def test_startup_recovery_closes_room_approval_before_effect_without_approval(self) -> None:
        for state in ("pending", "approved"):
            with self.subTest(state=state):
                binding = self._prepare_room_bound_approval(
                    suffix=f"{state}-{self.process_id}",
                )
                approval_id = binding["approvalId"]
                if state == "approved":
                    self.service.sessions.decide_approval(
                        approval_id,
                        approved=True,
                        payload_sha256=binding["payloadSha256"],
                        decided_by="execution-policy:room_dispatch",
                    )

                # The old Host disappears before an irreversible execution
                # claim.  Recovery must close the Tool without asking a human
                # or Luna and without entering the executor.
                self.service.close()
                self.process_id += 1
                self.service = AgentService(
                    db_path=self.root / "rag-ime.sqlite",
                    runtime_config=PiRuntimeConfig(
                        enabled=False,
                        executable=None,
                        agent_dir=self.root / f"agent-config-{state}",
                        session_dir=self.root / f"sessions-{state}",
                        logs_dir=self.root / f"logs-{state}",
                    ),
                    process_id_provider=lambda: self.process_id,
                    defer_startup_recovery=True,
                )
                self.service.bind_approval_executor(
                    lambda _approval: self.fail("recovery replayed the Tool")
                )
                with patch.object(
                    self.service.approval_model,
                    "decide",
                    side_effect=AssertionError("Room recovery requested Luna approval"),
                ) as decide:
                    self.service.run_startup_recovery()
                decide.assert_not_called()

                recovered = self.service.sessions.get_approval(approval_id)
                self.assertEqual(recovered["state"], "failed")
                self.assertFalse(recovered["receipt"]["mutationApplied"])
                self.assertFalse(recovered["receipt"]["effectMayHaveOccurred"])
                self.assertTrue(recovered["receipt"]["replayAllowed"])
                self.assertEqual(
                    recovered["receipt"]["executionOutcome"],
                    "not_started",
                )
                session_events, _ = self.service.events.replay(binding["sessionId"])
                self.assertEqual(
                    len(
                        [
                            event
                            for event in session_events
                            if event.event_type == "approval_resolved"
                            and event.payload.get("approvalId") == approval_id
                        ]
                    ),
                    1,
                )
                self.assertEqual(
                    len(
                        [
                            event
                            for event in session_events
                            if event.event_type == "tool_finished"
                            and event.payload.get("toolCallId")
                            == binding["toolCallId"]
                            and event.payload.get("recoveredExecutionTerminal") is True
                        ]
                    ),
                    1,
                )
                self.assertTrue(self.service.events.flush())
                recovered_room_terminals = [
                    event
                    for event in self.service.rooms.list_events(binding["roomId"])
                    if event.get("payload", {}).get("sourceEventType")
                    == "tool_finished"
                    and event.get("payload", {}).get("data", {}).get("approvalId")
                    == approval_id
                    and event.get("payload", {}).get("data", {}).get(
                        "recoveredExecutionTerminal"
                    )
                    is True
                ]
                self.assertEqual(len(recovered_room_terminals), 1)
                self.assertEqual(
                    recovered_room_terminals[0]["payload"]["data"]["status"],
                    "failed",
                )

                # Startup recovery is idempotent, including its durable
                # Session and Room projections.
                self.service.close()
                self.process_id += 1
                self.service = AgentService(
                    db_path=self.root / "rag-ime.sqlite",
                    runtime_config=PiRuntimeConfig(
                        enabled=False,
                        executable=None,
                        agent_dir=self.root / f"agent-config-{state}-again",
                        session_dir=self.root / f"sessions-{state}-again",
                        logs_dir=self.root / f"logs-{state}-again",
                    ),
                    process_id_provider=lambda: self.process_id,
                )
                with closing(sqlite3.connect(self.root / "rag-ime.sqlite")) as conn:
                    terminal_counts = dict(
                        conn.execute(
                            """
                            SELECT event_type, COUNT(*)
                            FROM agent_runtime_events
                            WHERE session_id = ?
                              AND event_type IN ('approval_resolved', 'tool_finished')
                            GROUP BY event_type
                            """,
                            (binding["sessionId"],),
                        ).fetchall()
                    )
                self.assertEqual(terminal_counts.get("approval_resolved"), 1)
                self.assertEqual(terminal_counts.get("tool_finished"), 1)
                self.assertEqual(
                    len(
                        [
                            event
                            for event in self.service.rooms.list_events(
                                binding["roomId"]
                            )
                            if event.get("payload", {}).get("sourceEventType")
                            == "tool_finished"
                            and event.get("payload", {}).get("data", {}).get(
                                "approvalId"
                            )
                            == approval_id
                        ]
                    ),
                    1,
                )

    def test_startup_recovery_projects_completed_room_tool_without_replay(self) -> None:
        binding = self._prepare_room_bound_approval(suffix="applied")
        approval_id = binding["approvalId"]
        self.service.sessions.decide_approval(
            approval_id,
            approved=True,
            payload_sha256=binding["payloadSha256"],
            decided_by="execution-policy:room_dispatch",
        )
        self.service.sessions.claim_approval_execution(
            approval_id,
            room_context=self.service._active_room_dispatch_context(
                binding["sessionId"]
            )
            or {},
        )
        completed = self.service.sessions.complete_approval(
            approval_id,
            state="applied",
            receipt={
                "schemaVersion": "rag-ime.agent-operation-receipt.v1",
                "approvalId": approval_id,
                "toolId": "workspace_shell",
                "operation": "run",
                "summary": "命令已执行完成",
                "mutationApplied": True,
                "executionOutcome": "completed",
                "replayAllowed": False,
                "exitCode": 0,
            },
        )
        self.assertEqual(completed["state"], "applied")

        # Crash after the effect and receipt commit, before the bridge can
        # return a Tool result or publish its terminal events.
        self.service.close()
        self.process_id += 1
        self.service = AgentService(
            db_path=self.root / "rag-ime.sqlite",
            runtime_config=PiRuntimeConfig(
                enabled=False,
                executable=None,
                agent_dir=self.root / "agent-config-applied-recovery",
                session_dir=self.root / "sessions-applied-recovery",
                logs_dir=self.root / "logs-applied-recovery",
            ),
            process_id_provider=lambda: self.process_id,
            defer_startup_recovery=True,
        )
        self.service.bind_approval_executor(
            lambda _approval: self.fail("recovery replayed an applied Tool")
        )
        with patch.object(
            self.service.approval_model,
            "decide",
            side_effect=AssertionError("Room recovery requested Luna approval"),
        ) as decide:
            self.service.run_startup_recovery()
        decide.assert_not_called()

        recovered = self.service.sessions.get_approval(approval_id)
        self.assertEqual(recovered["state"], "applied")
        self.assertEqual(recovered["receipt"], completed["receipt"])
        session_events, _ = self.service.events.replay(binding["sessionId"])
        recovered_tool_events = [
            event
            for event in session_events
            if event.event_type == "tool_finished"
            and event.payload.get("toolCallId") == binding["toolCallId"]
            and event.payload.get("recoveredExecutionTerminal") is True
        ]
        self.assertEqual(len(recovered_tool_events), 1)
        self.assertFalse(recovered_tool_events[0].payload["isError"])
        self.assertEqual(
            recovered_tool_events[0].payload["result"]["summary"],
            "命令已执行完成",
        )
        self.assertTrue(self.service.events.flush())
        room_terminals = [
            event
            for event in self.service.rooms.list_events(binding["roomId"])
            if event.get("payload", {}).get("sourceEventType") == "tool_finished"
            and event.get("payload", {}).get("data", {}).get("approvalId")
            == approval_id
            and event.get("payload", {}).get("data", {}).get(
                "recoveredExecutionTerminal"
            )
            is True
        ]
        self.assertEqual(len(room_terminals), 1)
        self.assertEqual(room_terminals[0]["payload"]["data"]["status"], "completed")
        self.assertTrue(
            room_terminals[0]["payload"]["data"]["effectMayHaveOccurred"]
        )
        applied_approval_events = [
            event
            for event in self.service.rooms.list_events(binding["roomId"])
            if event.get("payload", {}).get("sourceEventType")
            == "approval_resolved"
            and event.get("payload", {}).get("data", {}).get("approvalId")
            == approval_id
        ]
        self.assertEqual(len(applied_approval_events), 1)
        self.assertNotIn("error", applied_approval_events[0]["payload"]["data"])

        self.service.close()
        self.process_id += 1
        self.service = AgentService(
            db_path=self.root / "rag-ime.sqlite",
            runtime_config=PiRuntimeConfig(
                enabled=False,
                executable=None,
                agent_dir=self.root / "agent-config-applied-recovery-again",
                session_dir=self.root / "sessions-applied-recovery-again",
                logs_dir=self.root / "logs-applied-recovery-again",
            ),
            process_id_provider=lambda: self.process_id,
        )
        with closing(sqlite3.connect(self.root / "rag-ime.sqlite")) as conn:
            terminal_counts = dict(
                conn.execute(
                    """
                    SELECT event_type, COUNT(*)
                    FROM agent_runtime_events
                    WHERE session_id = ?
                      AND event_type IN ('approval_resolved', 'tool_finished')
                    GROUP BY event_type
                    """,
                    (binding["sessionId"],),
                ).fetchall()
            )
        self.assertEqual(terminal_counts.get("approval_resolved"), 1)
        self.assertEqual(terminal_counts.get("tool_finished"), 1)
        self.assertEqual(
            len(
                [
                    event
                    for event in self.service.rooms.list_events(binding["roomId"])
                    if event.get("payload", {}).get("sourceEventType")
                    == "tool_finished"
                    and event.get("payload", {}).get("data", {}).get("approvalId")
                    == approval_id
                ]
            ),
            1,
        )

    def test_external_room_finalization_projects_tool_terminal_without_another_restart(self) -> None:
        for succeeded in (True, False):
            with self.subTest(succeeded=succeeded):
                suffix = "external-success" if succeeded else "external-failure"
                binding = self._prepare_room_bound_approval(
                    suffix=suffix,
                    tool_name="runtime",
                    operation="restart_sidecar",
                )
                approval_id = binding["approvalId"]
                self.service.sessions.decide_approval(
                    approval_id,
                    approved=True,
                    payload_sha256=binding["payloadSha256"],
                    decided_by="execution-policy:room_dispatch",
                )
                self.service.sessions.claim_approval_execution(
                    approval_id,
                    room_context=self.service._active_room_dispatch_context(
                        binding["sessionId"]
                    )
                    or {},
                )
                command = [
                    "launchctl",
                    "kickstart",
                    "-k",
                    "gui/501/com.rag-ime.sidecar",
                ]
                command_sha256 = hashlib.sha256(
                    json.dumps(
                        command,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                self.service.sessions.complete_approval(
                    approval_id,
                    state="external_pending",
                    receipt={
                        "schemaVersion": "rag-ime.agent-operation-receipt.v1",
                        "approvalId": approval_id,
                        "toolId": "runtime",
                        "operation": "restart_sidecar",
                        "summary": "等待外部监督器完成",
                        "mutationApplied": False,
                        "externalActionPending": True,
                        "externalAction": "restart_sidecar",
                        "externalCommand": command,
                        "externalCommandSha256": command_sha256,
                        "originProcessId": self.process_id,
                    },
                )
                self.service.events.publish(
                    binding["sessionId"],
                    "approval_resolved",
                    {
                        "approvalId": approval_id,
                        "toolCallId": binding["toolCallId"],
                        "state": "external_pending",
                        "automatic": True,
                        "decisionMode": "policy",
                        "causalMetadata": {
                            "roomBound": True,
                            "roomId": binding["roomId"],
                            "rootId": binding["rootId"],
                            "dispatchId": binding["dispatchId"],
                            "generation": 1,
                        },
                    },
                    turn_id=binding["sessionTurnId"],
                )
                self.assertTrue(self.service.events.flush())

                # The external supervisor reaches a fresh Host. Its volatile
                # Room turn registry is empty, so ordinary event mirroring
                # cannot close the original Room Tool card.
                self.service.close()
                self.process_id += 1
                self.service = AgentService(
                    db_path=self.root / "rag-ime.sqlite",
                    runtime_config=PiRuntimeConfig(
                        enabled=False,
                        executable=None,
                        agent_dir=self.root / f"agent-config-{suffix}",
                        session_dir=self.root / f"sessions-{suffix}",
                        logs_dir=self.root / f"logs-{suffix}",
                    ),
                    process_id_provider=lambda: self.process_id,
                )

                with patch.object(
                    self.service.approval_model,
                    "decide",
                    side_effect=AssertionError(
                        "external Room finalization requested Luna approval"
                    ),
                ) as decide:
                    result = self.service.finalize_external_approval(
                        approval_id,
                        {
                            "payloadSha256": binding["payloadSha256"],
                            "externalAction": "restart_sidecar",
                            "externalCommandSha256": command_sha256,
                            "succeeded": succeeded,
                            "exitCode": 0 if succeeded else 7,
                            "timedOut": False,
                            "error": "" if succeeded else "launchctl failed",
                        },
                    )
                decide.assert_not_called()
                expected_state = "applied" if succeeded else "failed"
                self.assertEqual(result["approval"]["state"], expected_state)
                self.assertTrue(self.service.events.flush())

                session_events, _ = self.service.events.replay(
                    binding["sessionId"]
                )
                session_terminals = [
                    event
                    for event in session_events
                    if event.event_type == "tool_finished"
                    and event.payload.get("toolCallId")
                    == binding["toolCallId"]
                    and event.payload.get("recoveredExecutionTerminal") is True
                ]
                self.assertEqual(len(session_terminals), 1)
                self.assertEqual(
                    session_terminals[0].payload["isError"],
                    not succeeded,
                )
                room_terminals = [
                    event
                    for event in self.service.rooms.list_events(
                        binding["roomId"]
                    )
                    if event.get("payload", {}).get("sourceEventType")
                    == "tool_finished"
                    and event.get("payload", {}).get("data", {}).get(
                        "approvalId"
                    )
                    == approval_id
                ]
                self.assertEqual(len(room_terminals), 1)
                self.assertEqual(
                    room_terminals[0]["payload"]["data"]["status"],
                    "completed" if succeeded else "failed",
                )

                # Re-running the generic startup projector is safe and does
                # not create another Tool terminal or invoke an arbiter.
                self.service.approval_application.reconcile_abandoned_execution_claims()
                self.assertTrue(self.service.events.flush())
                session_events, _ = self.service.events.replay(
                    binding["sessionId"]
                )
                self.assertEqual(
                    len(
                        [
                            event
                            for event in session_events
                            if event.event_type == "tool_finished"
                            and event.payload.get("toolCallId")
                            == binding["toolCallId"]
                        ]
                    ),
                    1,
                )
                self.assertEqual(
                    len(
                        [
                            event
                            for event in self.service.rooms.list_events(
                                binding["roomId"]
                            )
                            if event.get("payload", {}).get("sourceEventType")
                            == "tool_finished"
                            and event.get("payload", {}).get("data", {}).get(
                                "approvalId"
                            )
                            == approval_id
                        ]
                    ),
                    1,
                )

    def test_room_startup_recovery_does_not_close_ordinary_unclaimed_approvals(self) -> None:
        session = self.service.create_session(
            {"title": "ordinary approvals remain user owned"}
        )["session"]
        session_id = str(session["id"])
        pending = self.service.sessions.create_approval(
            session_id=session_id,
            tool_name="workspace_shell",
            operation="run",
            payload_sha256="a" * 64,
            preview={"title": "pending ordinary approval"},
            risk_level="R2",
        )
        approved = self.service.sessions.create_approval(
            session_id=session_id,
            tool_name="workspace_shell",
            operation="run",
            payload_sha256="b" * 64,
            preview={"title": "approved ordinary approval"},
            risk_level="R2",
        )
        self.service.sessions.decide_approval(
            str(approved["approvalId"]),
            approved=True,
            payload_sha256=str(approved["payloadSha256"]),
            decided_by="native-control-center",
        )

        recovered, candidates = (
            self.service.sessions.fail_abandoned_approval_executions()
        )

        self.assertEqual(recovered, [])
        self.assertEqual(candidates, [])
        self.assertEqual(
            self.service.sessions.get_approval(str(pending["approvalId"]))[
                "state"
            ],
            "pending",
        )
        self.assertEqual(
            self.service.sessions.get_approval(str(approved["approvalId"]))[
                "state"
            ],
            "approved",
        )

    def test_recovered_pending_runtime_emits_one_exact_tool_terminal(self) -> None:
        binding = self._prepare_room_bound_approval(suffix="pending-runtime")
        approval_id = binding["approvalId"]
        self.service.sessions.decide_approval(
            approval_id,
            approved=True,
            payload_sha256=binding["payloadSha256"],
            decided_by="execution-policy:room_dispatch",
        )
        self.service.sessions.claim_approval_execution(
            approval_id,
            room_context=self.service._active_room_dispatch_context(
                binding["sessionId"]
            )
            or {},
        )
        approval = self.service.sessions.complete_approval(
            approval_id,
            state="applied",
            receipt={
                "schemaVersion": "rag-ime.agent-operation-receipt.v1",
                "approvalId": approval_id,
                "summary": "persisted result",
                "mutationApplied": True,
            },
        )

        def resolve_runtime(*_args, **_kwargs) -> None:
            self.service.events.publish(
                binding["sessionId"],
                "approval_resolved",
                {
                    "approvalId": approval_id,
                    "state": "applied",
                    "toolCallId": binding["toolCallId"],
                },
                turn_id=binding["sessionTurnId"],
            )
            self.service.events.publish(
                binding["sessionId"],
                "tool_finished",
                {
                    "toolCallId": binding["toolCallId"],
                    "toolName": "workspace_shell",
                    "isError": False,
                    "result": {"summary": "runtime result"},
                },
                turn_id=binding["sessionTurnId"],
            )

        with (
            patch.object(
                self.service.runtime,
                "has_pending_approval",
                return_value=True,
            ),
            patch.object(
                self.service.runtime,
                "resolve_approval",
                side_effect=resolve_runtime,
            ),
        ):
            self.service.approval_application._project_recovered_session_execution(
                approval,
                include_approval=True,
                include_tool=True,
            )

        events, _ = self.service.events.replay(binding["sessionId"])
        approval_terminals = [
            event
            for event in events
            if event.event_type == "approval_resolved"
            and event.payload.get("approvalId") == approval_id
        ]
        tool_terminals = [
            event
            for event in events
            if event.event_type == "tool_finished"
            and event.payload.get("toolCallId") == binding["toolCallId"]
        ]
        self.assertEqual(len(approval_terminals), 1)
        self.assertEqual(len(tool_terminals), 1)

        # A late Pi terminal for the same exact turn/Tool is the same event,
        # not a second durable or visible terminal.
        duplicate = self.service.events.publish(
            binding["sessionId"],
            "tool_finished",
            {
                "toolCallId": binding["toolCallId"],
                "toolName": "workspace_shell",
                "isError": False,
                "result": {"summary": "late runtime result"},
            },
            turn_id=binding["sessionTurnId"],
        )
        self.assertEqual(duplicate.event_id, tool_terminals[0].event_id)
        with closing(sqlite3.connect(self.root / "rag-ime.sqlite")) as conn:
            self.assertEqual(
                int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM agent_runtime_events
                        WHERE session_id = ? AND turn_id = ?
                          AND event_type = 'tool_finished'
                        """,
                        (binding["sessionId"], binding["sessionTurnId"]),
                    ).fetchone()[0]
                ),
                1,
            )

    def test_full_trust_scoped_git_commit_does_not_wait_for_the_model_arbiter(self) -> None:
        workspace = self.root / "room-commit"
        workspace.mkdir()
        session = self.service.create_session({"title": "Room local commit"})["session"]
        session_id = str(session["id"])
        session = self.service.update_session(
            session_id,
            {
                "mode": "coordinator",
                "workspaceRoots": [str(workspace)],
                "executionMode": "full_trust",
                "grantWorkspaceScope": True,
                "dangerousModeConfirmation": "ENABLE_FULL_TRUST",
            },
        )["session"]
        task = "commit isolated result"
        self.service.sessions.mutate_agent_todo(
            session_id,
            {"op": "init", "phase": "Room delivery", "items": [task]},
            actor="test-user",
        )
        self.service.sessions.mutate_agent_todo(
            session_id,
            {"op": "start", "task": task},
            actor="test-user",
        )
        gateway = ControlToolGateway(
            sessions=self.service.sessions,
            management=object(),
            core=object(),
            project="room-acceptance",
            workspace_harness=WorkspaceHarness(
                executor=lambda _prepared: {
                    "schemaVersion": "rag-ime.workspace-command-receipt.v1",
                    "mutationApplied": True,
                    "summary": "local commit applied",
                    "exitCode": 0,
                    "output": "",
                }
            ),
        )
        self.service.bind_approval_executor(gateway.apply_approval)
        gateway.bind_auto_approval_executor(self.service.auto_approve_pending)

        with patch.object(
            self.service.approval_model,
            "decide",
            side_effect=AssertionError("scoped full-trust command reached model approval"),
        ) as decide:
            response = gateway.execute(
                {
                    "schemaVersion": "rag-ime.agent-tool-call.v1",
                    "sessionId": session_id,
                    "tool": "workspace_shell",
                    "toolCallId": "tool:room-local-commit",
                    "args": {
                        "op": "run",
                        "command": 'git commit -m "room acceptance"',
                        "cwd": str(workspace),
                        "allowNetwork": False,
                    },
                }
            )

        decide.assert_not_called()
        result = response["result"]
        self.assertTrue(result["autoApproved"])
        self.assertEqual(result["decisionMode"], "policy")
        self.assertEqual(result["approval"]["state"], "applied")
        self.assertEqual(result["receipt"]["exitCode"], 0)

    def test_automatic_approval_bridge_failure_closes_the_pending_approval(self) -> None:
        session = self.service.create_session({"title": "自动审批桥失败"})["session"]
        session_id = str(session["id"])
        self.service.update_session(
            session_id,
            {
                "mode": "coordinator",
                "workspaceRoots": [self.root.as_posix()],
                "executionMode": "full_trust",
                "grantWorkspaceScope": True,
                "dangerousModeConfirmation": "ENABLE_FULL_TRUST",
            },
        )
        approval = self.service.sessions.create_approval(
            session_id=session_id,
            tool_name="workspace_job",
            operation="start",
            payload_sha256="d" * 64,
            preview={"title": "启动后台任务", "summary": "启动测试任务"},
            risk_level="R2",
        )
        original_decide = self.service.sessions.decide_approval
        attempts = 0

        def fail_once(*args: object, **kwargs: object) -> dict[str, object]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("automatic approval bridge disconnected")
            return original_decide(*args, **kwargs)

        with patch.object(
            self.service.sessions,
            "decide_approval",
            side_effect=fail_once,
        ):
            result = self.service.auto_approve_pending(approval)

        self.assertTrue(result["terminal"])
        self.assertFalse(result["approvalRequired"])
        self.assertFalse(result["autoApproved"])
        self.assertEqual(result["failureCode"], "automatic_approval_bridge_failed")
        self.assertEqual(
            self.service.sessions.get_approval(str(approval["approvalId"]))["state"],
            "rejected",
        )
        events, _ = self.service.events.replay(session_id)
        resolved = next(
            event
            for event in events
            if event.event_type == "approval_resolved"
            and event.payload.get("approvalId") == approval["approvalId"]
        )
        self.assertEqual(resolved.payload["state"], "rejected")
        self.assertTrue(resolved.payload["automatic"])

    def test_full_trust_scoped_text_edit_does_not_wait_for_the_model_arbiter(self) -> None:
        workspace = self.root / "room-edit"
        workspace.mkdir()
        target = workspace / "customer.py"
        target.write_text("status = 'old'\n", encoding="utf-8")
        session = self.service.create_session({"title": "Room safe edit"})["session"]
        session_id = str(session["id"])
        self.service.update_session(
            session_id,
            {
                "mode": "coordinator",
                "workspaceRoots": [str(workspace)],
                "executionMode": "full_trust",
                "grantWorkspaceScope": True,
                "dangerousModeConfirmation": "ENABLE_FULL_TRUST",
            },
        )
        task = "apply safe edit"
        self.service.sessions.mutate_agent_todo(
            session_id,
            {"op": "init", "phase": "Room delivery", "items": [task]},
            actor="test-user",
        )
        self.service.sessions.mutate_agent_todo(
            session_id,
            {"op": "start", "task": task},
            actor="test-user",
        )
        gateway = ControlToolGateway(
            sessions=self.service.sessions,
            management=object(),
            core=object(),
            project="room-acceptance",
            workspace_harness=WorkspaceHarness(executor=lambda _prepared: {}),
        )
        self.service.bind_approval_executor(gateway.apply_approval)
        gateway.bind_auto_approval_executor(self.service.auto_approve_pending)

        with patch.object(
            self.service.approval_model,
            "decide",
            side_effect=AssertionError("scoped full-trust edit reached model approval"),
        ) as decide:
            response = gateway.execute(
                {
                    "schemaVersion": "rag-ime.agent-tool-call.v1",
                    "sessionId": session_id,
                    "tool": "workspace_edit",
                    "toolCallId": "tool:room-safe-edit",
                    "args": {
                        "op": "apply",
                        "path": str(target),
                        "resourceRevision": "sha256:"
                        + hashlib.sha256(target.read_bytes()).hexdigest(),
                        "edits": [
                            {"oldText": "status = 'old'", "newText": "status = 'new'"}
                        ],
                    },
                }
            )

        decide.assert_not_called()
        result = response["result"]
        self.assertTrue(result["autoApproved"])
        self.assertEqual(result["decisionMode"], "policy")
        self.assertEqual(result["approval"]["state"], "applied")
        self.assertEqual(target.read_text(encoding="utf-8"), "status = 'new'\n")

    def test_legacy_full_trust_model_rejection_is_terminal_without_human_approval(self) -> None:
        session = self.service.create_session(
            {"title": "模型拒绝测试"}
        )["session"]
        session_id = str(session["id"])
        self.service.update_session(
            session_id,
            {
                "mode": "coordinator",
                "workspaceRoots": [self.root.as_posix()],
                "executionMode": "full_trust",
                "toolProfileVersion": "control-center-v1",
                "dangerousModeConfirmation": "ENABLE_FULL_TRUST",
            },
        )
        # A persisted Room overlay is historical metadata. Without a live Room
        # dispatch this remains an ordinary full-trust Session and must keep
        # the model-arbitration preview used by its policy owner.
        self.service.sessions.set_room_execution_mode(
            session_id,
            "room_unrestricted",
        )
        approval = self.service.sessions.create_approval(
            session_id=session_id,
            tool_name="workspace_shell",
            operation="run",
            payload_sha256="b" * 64,
            preview={"title": "危险操作", "summary": "删除数据库"},
            risk_level="R3",
        )
        self.assertIn("approvalArbitration", approval["preview"])
        approval = self.service.sessions.bind_approval_tool_call(
            str(approval["approvalId"]),
            tool_call_id="tool:approval-rejection",
        )
        with patch.object(
            self.service.approval_model,
            "decide",
            return_value={
                "receiptId": "approval-model-decision:deny",
                "decision": "deny",
                "status": "decided",
                "reasonCodes": ["destructive_effect"],
                "rationaleSummary": "该操作会产生灾难性破坏。",
            },
        ):
            result = self.service.auto_approve_pending(approval)

        self.assertFalse(result["autoApproved"])
        self.assertFalse(result["approvalRequired"])
        self.assertTrue(result["modelDecided"])
        self.assertEqual(result["decisionMode"], "model")
        self.assertEqual(result["approvalModelDecision"]["decision"], "deny")
        self.assertIn("灾难性破坏", result["summary"])
        stored = self.service.sessions.get_approval(
            str(approval["approvalId"])
        )
        self.assertEqual(stored["state"], "rejected")
        self.assertEqual(
            stored["decidedBy"],
            "approval-model:approval-model-decision:deny",
        )
        events, _ = self.service.events.replay(session_id)
        resolved = next(
            event
            for event in events
            if event.event_type == "approval_resolved"
            and event.payload.get("approvalId") == approval["approvalId"]
        )
        self.assertEqual(
            resolved.payload["toolCallId"],
            "tool:approval-rejection",
        )

    def test_legacy_full_trust_expiry_race_closes_the_pending_pi_tool_call(self) -> None:
        session = self.service.create_session(
            {"title": "自动审批过期竞态"}
        )["session"]
        session_id = str(session["id"])
        self.service.update_session(
            session_id,
            {
                "mode": "coordinator",
                "workspaceRoots": [self.root.as_posix()],
                "executionMode": "full_trust",
                "toolProfileVersion": "control-center-v1",
                "dangerousModeConfirmation": "ENABLE_FULL_TRUST",
            },
        )
        approval = self.service.sessions.create_approval(
            session_id=session_id,
            tool_name="workspace_shell",
            operation="run",
            payload_sha256="c" * 64,
            preview={"title": "运行测试", "summary": "运行一条有界测试命令"},
            risk_level="R2",
        )
        approval = self.service.sessions.bind_approval_tool_call(
            str(approval["approvalId"]),
            tool_call_id="tool:approval-expiry-race",
        )

        def expire_while_model_runs(*_args: object) -> dict[str, object]:
            self.service.sessions.get_approval(
                str(approval["approvalId"]),
                now_ms=int(approval["expiresAtMs"]),
            )
            return {
                "receiptId": "approval-model-decision:late",
                "decision": "deny",
                "status": "failed_closed",
                "reasonCodes": ["policy_boundary"],
                "rationaleSummary": "审批模型未在有效期内完成。",
            }

        with (
            patch.object(
                self.service.approval_model,
                "decide",
                side_effect=expire_while_model_runs,
            ),
            patch.object(
                self.service.runtime,
                "has_pending_approval",
                return_value=True,
            ),
            patch.object(
                self.service.runtime,
                "resolve_approval",
            ) as resolve_approval,
        ):
            result = self.service.auto_approve_pending(approval)

        self.assertTrue(result["terminal"])
        self.assertEqual(result["approval"]["state"], "expired")
        self.assertTrue(result["runtimeNotified"])
        resolve_approval.assert_called_once_with(
            session_id,
            str(approval["approvalId"]),
            approved=False,
            resolution_state="expired",
        )

    def test_paused_or_exhausted_goal_blocks_provider_prompt_before_runtime(self) -> None:
        session_id = str(
            self.service.create_session({"title": "goal gated provider"})["session"]["id"]
        )
        goal = self.service.sessions.mutate_agent_goal(
            session_id,
            {
                "action": "confirm_setup",
                "confirmed": True,
                "expectedRevision": 0,
                "objective": "在预算内完成实现",
                "tokenBudget": 10,
            },
        )["workflow"]["goal"]
        paused = self.service.sessions.mutate_agent_goal(
            session_id,
            {"action": "pause", "expectedRevision": goal["revision"]},
        )["workflow"]["goal"]
        with patch.object(self.service, "_prompt_with_checkpoint") as provider:
            with self.assertRaisesRegex(ValueError, "goal_paused"):
                self.service.prompt(session_id, {"message": "继续执行"})
            provider.assert_not_called()

        resumed = self.service.sessions.mutate_agent_goal(
            session_id,
            {"action": "resume", "expectedRevision": paused["revision"]},
        )["workflow"]["goal"]
        exhausted = self.service.sessions.record_agent_goal_usage(
            session_id,
            idempotency_key="goal-gate:usage",
            turn_id="turn:goal-gate",
            event_id="event:goal-gate",
            token_delta=10,
            elapsed_delta_ms=0,
        )["goal"]
        self.assertGreaterEqual(exhausted["revision"], resumed["revision"])
        with patch.object(self.service, "_prompt_with_checkpoint") as provider:
            with self.assertRaisesRegex(ValueError, "goal_budget_exhausted"):
                self.service.prompt(session_id, {"message": "继续执行"})
            provider.assert_not_called()

        cancelled = self.service.mutate_goal(
            session_id,
            {
                "action": "cancel",
                "expectedRevision": exhausted["revision"],
                "reason": "用户明确终止该 Goal",
            },
        )["goal"]
        self.assertEqual(cancelled["status"], "cancelled")
        with patch.object(self.service, "_prompt_with_checkpoint") as provider:
            with self.assertRaisesRegex(ValueError, "goal_cancelled"):
                self.service.prompt(session_id, {"message": "不要继续执行"})
            provider.assert_not_called()

    def test_paused_goal_prompt_receipt_exposes_goal_paused_cause_code(self) -> None:
        session_id = str(
            self.service.create_session({"title": "goal paused cause"})["session"]["id"]
        )
        goal = self.service.sessions.mutate_agent_goal(
            session_id,
            {
                "action": "confirm_setup",
                "confirmed": True,
                "expectedRevision": 0,
                "objective": "在预算内完成实现",
            },
        )["workflow"]["goal"]
        self.service.sessions.mutate_agent_goal(
            session_id,
            {"action": "pause", "expectedRevision": goal["revision"]},
        )
        with patch.object(self.service, "_prompt_with_checkpoint") as provider:
            with self.assertRaises(AgentCommandReceiptFailed) as failed:
                self.service.prompt(
                    session_id,
                    {
                        "message": "继续执行",
                        "clientMessageId": "goal-paused-cause-1",
                    },
                )
            provider.assert_not_called()
        self.assertEqual(
            failed.exception.response_payload(),
            {
                "code": "AGENT_COMMAND_FAILED",
                "commandReceipt": {
                    "state": "failed",
                    "clientMessageId": "goal-paused-cause-1",
                    "causeCode": "goal_paused",
                },
            },
        )

    def test_paused_goal_blocks_conversation_rewrite_before_runtime(self) -> None:
        session_id = str(
            self.service.create_session({"title": "goal gated rewrite"})["session"]["id"]
        )
        goal = self.service.sessions.mutate_agent_goal(
            session_id,
            {
                "action": "confirm_setup",
                "confirmed": True,
                "expectedRevision": 0,
                "objective": "在预算内完成实现",
            },
        )["workflow"]["goal"]
        self.service.sessions.mutate_agent_goal(
            session_id,
            {"action": "pause", "expectedRevision": goal["revision"]},
        )
        with (
            patch.object(self.service.runtime, "rewind_session") as rewind,
            patch.object(self.service, "_prompt_with_checkpoint") as provider,
        ):
            with self.assertRaisesRegex(ValueError, "goal_paused"):
                self.service.rewrite_session(
                    session_id,
                    {
                        "entryId": "entry-user-1",
                        "message": "修改后的问题",
                        "clientMessageId": "rewrite-while-paused",
                    },
                )
            rewind.assert_not_called()
            provider.assert_not_called()

    def test_goal_settle_continuation_is_deterministic_for_one_active_revision(
        self,
    ) -> None:
        session_id = str(
            self.service.create_session(
                {"title": "goal settle continuation"}
            )["session"]["id"]
        )
        self.service.sessions.mutate_agent_todo(
            session_id,
            {"op": "init", "items": ["完成剩余验收并提供证据"]},
        )
        self.service.sessions.mutate_agent_todo(
            session_id,
            {"op": "start", "task": "完成剩余验收并提供证据"},
        )
        goal = self.service.sessions.mutate_agent_goal(
            session_id,
            {
                "action": "confirm_setup",
                "confirmed": True,
                "expectedRevision": 0,
                "objective": "完成剩余验收并提供证据",
                "tokenBudget": 10_000,
            },
        )["workflow"]["goal"]
        request = {
            "schemaVersion": "rag-ime.agent-goal-settle-request.v1",
            "sessionId": session_id,
            "settleScopeId": "scope:goal-settle:1",
            "settleAttempt": 1,
            "freshToolEvidenceCount": 0,
            "freshToolEvidenceSha256": hashlib.sha256(b"").hexdigest(),
        }
        first_evidence_sha256 = hashlib.sha256(b"evidence:first").hexdigest()
        second_evidence_sha256 = hashlib.sha256(b"evidence:second").hexdigest()

        first = self.service.settle_goal_runtime(request)["result"]
        replay = self.service.settle_goal_runtime(request)["result"]
        stalled = self.service.settle_goal_runtime(
            {**request, "settleAttempt": 2}
        )["result"]
        progressed = self.service.settle_goal_runtime(
            {
                **request,
                "settleAttempt": 2,
                "freshToolEvidenceCount": 1,
                "freshToolEvidenceSha256": first_evidence_sha256,
            }
        )["result"]
        progressed_replay = self.service.settle_goal_runtime(
            {
                **request,
                "settleAttempt": 2,
                "freshToolEvidenceCount": 1,
                "freshToolEvidenceSha256": first_evidence_sha256,
            }
        )["result"]
        progressed_with_different_evidence = self.service.settle_goal_runtime(
            {
                **request,
                "settleAttempt": 2,
                "freshToolEvidenceCount": 1,
                "freshToolEvidenceSha256": second_evidence_sha256,
            }
        )["result"]
        capped = self.service.settle_goal_runtime(
            {
                **request,
                "settleAttempt": 5,
                "freshToolEvidenceCount": 1,
                "freshToolEvidenceSha256": second_evidence_sha256,
            }
        )["result"]
        fourth = self.service.settle_goal_runtime(
            {
                **request,
                "settleScopeId": "scope:goal-settle:2",
                "settleAttempt": 1,
            }
        )["result"]
        globally_capped = self.service.settle_goal_runtime(
            {
                **request,
                "settleScopeId": "scope:goal-settle:3",
                "settleAttempt": 1,
            }
        )["result"]
        goal_after_settle = self.service.workflow_state(session_id)["goal"]

        self.assertEqual(first, replay)
        self.assertEqual(first["state"], "continue")
        self.assertEqual(first["freshToolEvidenceCount"], 0)
        self.assertEqual(
            first["freshToolEvidenceSha256"],
            hashlib.sha256(b"").hexdigest(),
        )
        self.assertEqual(first["goalId"], goal["goalId"])
        self.assertEqual(first["goalRevision"], goal["revision"])
        self.assertTrue(str(first["followUpKey"]).startswith("goal-settle:"))
        self.assertIn("<managed-goal-follow-up", str(first["message"]))
        self.assertIn("不要把仍在进行的 Room Goal 暂停", str(first["message"]))
        self.assertIn("暂停只用于用户明确要求停止", str(first["message"]))
        self.assertNotIn("完成、暂停或取消", str(first["message"]))
        self.assertNotIn("Todo 中有正在执行的任务", str(first["message"]))
        self.assertEqual(stalled["state"], "stalled")
        self.assertEqual(stalled["reason"], "no_progress")
        self.assertEqual(stalled["followUpKey"], "")
        self.assertEqual(progressed, progressed_replay)
        self.assertEqual(progressed["state"], "continue")
        self.assertEqual(progressed["freshToolEvidenceCount"], 1)
        self.assertNotEqual(first["followUpKey"], progressed["followUpKey"])
        self.assertNotEqual(
            progressed["followUpKey"],
            progressed_with_different_evidence["followUpKey"],
        )
        self.assertEqual(capped["state"], "stalled")
        self.assertEqual(capped["reason"], "settle_attempt_limit")
        self.assertEqual(capped["followUpKey"], "")
        self.assertEqual(fourth["state"], "continue")
        self.assertEqual(fourth["continuationCount"], 4)
        self.assertEqual(fourth["continuationRemaining"], 0)
        self.assertEqual(globally_capped["state"], "stalled")
        self.assertEqual(
            globally_capped["reason"],
            "goal_continuation_limit",
        )
        self.assertEqual(globally_capped["continuationCount"], 4)
        self.assertEqual(goal_after_settle["status"], "active")
        self.assertEqual(goal_after_settle["revision"], goal["revision"])

    def test_goal_settle_never_continues_terminal_or_blocked_states(self) -> None:
        session_id = str(
            self.service.create_session(
                {"title": "goal settle terminal states"}
            )["session"]["id"]
        )
        request = {
            "schemaVersion": "rag-ime.agent-goal-settle-request.v1",
            "sessionId": session_id,
            "settleScopeId": "scope:goal-settle:terminal",
            "settleAttempt": 1,
            "freshToolEvidenceCount": 0,
            "freshToolEvidenceSha256": hashlib.sha256(b"").hexdigest(),
        }
        cases = [
            (
                "inactive",
                {
                    "configured": False,
                    "goalId": "",
                    "revision": 0,
                    "status": "cleared",
                    "budgetExceeded": False,
                },
                {"allowed": True, "reason": "user_execution_request"},
            ),
            (
                "paused",
                {
                    "configured": True,
                    "goalId": "goal:paused",
                    "revision": 2,
                    "status": "paused",
                    "budgetExceeded": False,
                },
                {"allowed": False, "reason": "goal_paused"},
            ),
            (
                "completed",
                {
                    "configured": True,
                    "goalId": "goal:completed",
                    "revision": 3,
                    "status": "completed",
                    "budgetExceeded": False,
                },
                {"allowed": False, "reason": "goal_completed"},
            ),
            (
                "cancelled",
                {
                    "configured": True,
                    "goalId": "goal:cancelled",
                    "revision": 4,
                    "status": "cancelled",
                    "budgetExceeded": False,
                },
                {"allowed": False, "reason": "goal_cancelled"},
            ),
            (
                "budget_exhausted",
                {
                    "configured": True,
                    "goalId": "goal:exhausted",
                    "revision": 4,
                    "status": "active",
                    "budgetExceeded": True,
                },
                {"allowed": False, "reason": "goal_budget_exhausted"},
            ),
            (
                "blocked",
                {
                    "configured": True,
                    "goalId": "goal:blocked",
                    "revision": 5,
                    "status": "active",
                    "budgetExceeded": False,
                },
                {"allowed": False, "reason": "native_approval_required"},
            ),
        ]
        for expected, goal, gate in cases:
            with self.subTest(state=expected):
                with patch.object(
                    self.service,
                    "workflow_state",
                    return_value={
                        "todo": {},
                        "goal": goal,
                        "actGate": gate,
                    },
                ):
                    result = self.service.settle_goal_runtime(request)["result"]
                self.assertEqual(result["state"], expected)
                self.assertEqual(result["followUpKey"], "")
                self.assertEqual(result["message"], "")

    def test_session_memory_switch_controls_bootstrap_compaction_and_existing_context(self) -> None:
        session = self.service.sessions.create(
            title="Session memory switch", mode="coordinator", execution_mode="full_trust",
            tool_profile_version="control-center-auto-approve-v1", workspace_roots=[str(self.root)],
        )
        session_id = str(session["id"])
        real_build = self.service.memory_bootstrap.build
        marker = "SESSION_MEMORY_SWITCH_FACT"

        def build_memory(*args, **kwargs):
            spec = real_build(*args, **kwargs)
            spec["payload"]["items"] = [{
                "rank": 1, "sourceType": "memory_atom", "sourceId": "atom:session-switch",
                "title": "测试记忆", "text": marker, "score": 1.0, "confidence": 1.0,
                "lanes": ["bm25_raw"], "rawScores": {}, "tags": [],
                "ownerKind": "user", "ownerId": "default", "evidenceEventIds": [1],
            }]
            return spec

        count = 0
        def accepted(*_args, **_kwargs):
            nonlocal count
            count += 1
            return {"accepted": True, "turnId": f"turn:memory-switch:{count}",
                    "piEntryId": f"entry:memory-switch:{count}", "response": {"success": True}}

        with patch.object(self.service.runtime, "prompt", side_effect=accepted) as prompt, \
             patch.object(self.service.memory_bootstrap, "build", side_effect=build_memory) as build:
            disabled = self.service.prompt(session_id, {"message": "首轮关闭"})
            self.assertEqual(disabled["memoryBootstrap"]["status"], "disabled")
            self.assertEqual(disabled["contextItemsDelivered"], 0)
            build.assert_not_called()

            self.service.sessions.set_disclosure_preferences(session_id, {"tool:memory": "enabled"})
            enabled = self.service.prompt(session_id, {"message": "开启记忆"})
            self.assertEqual(enabled["contextItemsDelivered"], 1)
            item_id = enabled["memoryBootstrap"]["itemId"]
            self.assertEqual(build.call_count, 1)

            self.service.sessions.set_disclosure_preferences(session_id, {"tool:memory": "disabled"})
            refresh = self.service.memory_context_application.refresh({"sessionId": session_id, "trigger": "compaction"})
            self.assertFalse(refresh["memoryEnabled"])
            self.assertEqual(self.service.memory_context_application.provider_context(session_id), "")
            disabled_again = self.service.prompt(session_id, {"message": "已有记忆后再次关闭"})
            self.assertEqual(disabled_again["contextItemsDelivered"], 0)
            self.assertEqual(build.call_count, 1)
            trace = self.service.context_trace(session_id, disabled_again["contextTraceId"])
            node = next(node for node in trace["nodes"] if node["stage"] == "memory_recall")
            self.assertEqual(node["metadata"]["recallStatus"], "disabled")
            self.assertEqual(self.service.context_runtime.active_item(session_id, source_kind="memory_bootstrap")["itemId"], item_id)

            self.service.sessions.set_disclosure_preferences(session_id, {})
            restored = self.service.prompt(session_id, {"message": "恢复跟随默认"})
            self.assertEqual(restored["memoryBootstrap"]["status"], "disabled")
            self.assertEqual(restored["contextItemsDelivered"], 0)
            self.assertEqual(build.call_count, 1)
            contexts = [json.loads(call.args[1][len(RUNTIME_PROMPT_ENVELOPE_PREFIX):]).get("sessionContext", "") for call in prompt.call_args_list]
            self.assertEqual([marker in context for context in contexts], [False, True, False, False])
            self.assertTrue(self.service.memory_enabled(), "Session switch must not change the global setting")

    def test_new_session_reuses_one_query_aware_bootstrap_until_compaction(self) -> None:
        created = self.service.create_session({"title": "个人上下文"})
        session = created["session"]
        session_id = str(session["id"])
        self.service.sessions.set_disclosure_preferences(session_id, {"tool:memory": "enabled"})
        real_memory_build = self.service.memory_bootstrap.build

        def build_memory_with_turn_marker(*args, **kwargs):
            specification = real_memory_build(*args, **kwargs)
            query_text = str(kwargs["query_text"])
            marker = (
                "MEMORY_A_ONLY"
                if "第一轮" in query_text
                else "MEMORY_B_ONLY"
            )
            payload = dict(specification["payload"])
            payload["items"] = [
                {
                    "rank": 1,
                    "sourceType": "memory_atom",
                    "sourceId": f"atom:{marker}",
                    "title": "project_fact",
                    "text": marker,
                    "score": 1.0,
                    "confidence": 1.0,
                    "lanes": ["bm25_raw"],
                    "rawScores": {},
                    "tags": [],
                    "ownerKind": "project",
                    "ownerId": "test",
                    "evidenceEventIds": [1],
                }
            ]
            payload["sourceIds"] = [f"atom:{marker}"]
            specification["payload"] = payload
            specification["source_id"] = str(payload["recallId"])
            return specification

        self.assertEqual(session["roleBookRevisionId"], "")
        self.assertEqual(
            created["roleBook"]["status"],
            "persona_package_not_installed",
        )
        self.assertTrue(created["memoryBootstrap"]["ok"])
        self.assertEqual(
            created["memoryBootstrap"]["status"],
            "awaiting_first_prompt",
        )
        pending = self.service.context_runtime.list_items(session_id)
        self.assertEqual(pending, [])

        accepted_values = [
            {
                "accepted": True,
                "turnId": "turn:bootstrap:1",
                "piEntryId": "entry:bootstrap:1",
                "response": {"success": True},
            },
            {
                "accepted": True,
                "turnId": "turn:bootstrap:2",
                "piEntryId": "entry:bootstrap:2",
                "response": {"success": True},
            },
        ]
        with (
            patch.object(
                self.service.runtime,
                "prompt",
                side_effect=accepted_values,
            ) as runtime_prompt,
            patch.object(
                self.service.memory_bootstrap,
                "build",
                side_effect=build_memory_with_turn_marker,
            ) as build_memory,
        ):
            first = self.service.prompt(session_id, {"message": "第一轮"})
            second = self.service.prompt(session_id, {"message": "第二轮"})

        self.assertEqual(first["contextItemsDelivered"], 1)
        self.assertEqual(
            first["memoryBootstrap"]["dedupeKey"],
            f"memory-bootstrap:{session_id}:v3",
        )
        self.assertEqual(
            second["memoryBootstrap"]["dedupeKey"],
            first["memoryBootstrap"]["dedupeKey"],
        )
        self.assertEqual(second["contextItemsDelivered"], 1)
        first_trace = self.service.context_trace(
            session_id,
            str(first["contextTraceId"]),
        )
        first_memory_node = next(
            node for node in first_trace["nodes"] if node["stage"] == "memory_recall"
        )
        self.assertEqual(
            first_memory_node["metadata"].get("memoryAtomIds"),
            "atom:MEMORY_A_ONLY",
        )
        self.assertNotIn(
            "refreshedForCurrentTurn",
            first["memoryBootstrap"],
        )
        self.assertNotIn(
            "refreshedForCurrentTurn",
            second["memoryBootstrap"],
        )
        self.assertEqual(build_memory.call_count, 1)
        self.assertIn(
            "第一轮",
            build_memory.call_args.kwargs["query_text"],
        )
        self.assertEqual(
            build_memory.call_args.kwargs["trigger"],
            "first_user_prompt",
        )
        runtime_envelopes = []
        for call in runtime_prompt.call_args_list:
            sent = str(call.args[1])
            self.assertTrue(sent.startswith(RUNTIME_PROMPT_ENVELOPE_PREFIX))
            runtime_envelopes.append(
                json.loads(sent[len(RUNTIME_PROMPT_ENVELOPE_PREFIX):])
            )
        self.assertEqual(runtime_envelopes[0]["message"], "第一轮")
        self.assertIn(
            "MEMORY_A_ONLY",
            runtime_envelopes[0]["sessionContext"],
        )
        self.assertNotIn(
            "MEMORY_B_ONLY",
            runtime_envelopes[0]["sessionContext"],
        )
        self.assertEqual(runtime_envelopes[1]["message"], "第二轮")
        self.assertIn(
            "MEMORY_A_ONLY",
            runtime_envelopes[1]["sessionContext"],
        )
        self.assertNotIn(
            "MEMORY_B_ONLY",
            runtime_envelopes[1]["sessionContext"],
        )
        self.assertTrue(first["memoryEvidence"]["stored"])
        self.assertTrue(second["memoryEvidence"]["stored"])
        delivered = self.service.context_runtime.list_items(
            session_id,
            status="delivered",
        )
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0]["lifecycle"], "persistent")
        expired = self.service.context_runtime.list_items(
            session_id,
            status="expired",
        )
        self.assertEqual(expired, [])

        self.service.events.publish(
            session_id,
            "message_completed",
            {
                "message": {
                    "schemaVersion": "rag-ime.agent-message.v1",
                    "id": "entry:assistant:1",
                    "sessionId": session_id,
                    "turnId": "turn:bootstrap:2",
                    "role": "assistant",
                    "status": "completed",
                    "blocks": [
                        {
                            "id": "text:assistant:1",
                            "type": "text",
                            "status": "completed",
                            "presentationKind": "markdown",
                            "data": {"text": "这是最终回答"},
                        }
                    ],
                    "attachments": [],
                    "citations": [],
                    "createdAtMs": 10,
                    "completedAtMs": 11,
                }
            },
            turn_id="turn:bootstrap:2",
        )
        self.assertTrue(self.service.events.flush())
        evidence = self.service.memory_evidence.list(
            role_id=str(session["roleId"]),
            session_id=session_id,
        )
        self.assertEqual(
            [item["sourceKind"] for item in evidence],
            ["assistant_message", "user_message", "user_message"],
        )
        self.assertTrue(all(item["maySupportLongTermFact"] is False for item in evidence))
        self.assertEqual(
            self.service.memory_sources.list_for_owner(
                owner_kind="agent",
                owner_id=str(session["roleId"]),
            ),
            [],
            "ordinary companion-present-v1 assistant turns must not create an automatic curation source",
        )

    def test_context_trace_explains_why_timeline_recall_was_enabled(self) -> None:
        session = self.service.create_session({"title": "时间线门控"})["session"]
        session_id = str(session["id"])
        self.service.sessions.set_disclosure_preferences(session_id, {"tool:memory": "enabled"})
        with patch.object(
            self.service.runtime,
            "prompt",
            return_value={
                "accepted": True,
                "turnId": "turn:timeline-intent",
                "piEntryId": "entry:timeline-intent",
                "response": {"success": True},
            },
        ):
            result = self.service.prompt(
                session_id,
                {"message": "昨天 RAG IME 做到哪里了？"},
            )

        trace = self.service.context_trace(
            session_id,
            str(result["contextTraceId"]),
        )
        memory_node = next(
            node for node in trace["nodes"] if node["stage"] == "memory_recall"
        )
        self.assertEqual(
            memory_node["summary"],
            "已加入首问与最近完整输入召回的角色可见 Timeline/Topic Book/Atom 记忆包",
        )
        self.assertEqual(
            memory_node["metadata"],
            {
                "hitCount": 0,
                "itemCount": 1,
                "lifecycle": "session",
                "priority": "developer",
                "recallStatus": "empty",
                "recallTrigger": "first_user_prompt",
                "timelineMatched": "昨天",
                "timelineRange": "yesterday",
                "timelineReason": "relative_time",
                "timelineRequested": True,
            },
        )

    def test_old_consumed_bootstrap_is_migrated_to_persistent_session_context(self) -> None:
        session = self.service.create_session({"title": "旧启动上下文"})["session"]
        session_id = str(session["id"])
        self.service.sessions.set_disclosure_preferences(session_id, {"tool:memory": "enabled"})
        legacy = self.service.context_runtime.enqueue(
            session_id=session_id,
            source_kind="memory_bootstrap",
            lane="fact",
            lifecycle="once",
            dedupe_key=f"memory-bootstrap:{session_id}:v2",
            title="旧版一次性记忆包",
            payload={"queryFree": False},
        )
        reserved = self.service.context_runtime.materialize_for_delivery(
            session_id,
            delivery_id="dispatch:legacy",
        )
        self.assertEqual(reserved["itemIds"], [legacy["itemId"]])

        with patch.object(
            self.service.runtime,
            "prompt",
            return_value={
                "accepted": True,
                "turnId": "turn:migrated",
                "piEntryId": "entry:migrated",
                "response": {"success": True},
            },
        ) as runtime_prompt:
            result = self.service.prompt(session_id, {"message": "按当前问题重新召回"})

        self.assertEqual(result["memoryBootstrap"]["expiredLegacyItems"], 1)
        self.assertEqual(
            result["memoryBootstrap"]["dedupeKey"],
            f"memory-bootstrap:{session_id}:v3",
        )
        expired = self.service.context_runtime.list_items(session_id, status="expired")
        self.assertEqual([item["itemId"] for item in expired], [legacy["itemId"]])
        delivered = self.service.context_runtime.list_items(
            session_id,
            status="delivered",
        )
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0]["lifecycle"], "persistent")
        runtime_message = str(runtime_prompt.call_args.args[1])
        self.assertTrue(runtime_message.startswith(RUNTIME_PROMPT_ENVELOPE_PREFIX))
        envelope = json.loads(
            runtime_message[len(RUNTIME_PROMPT_ENVELOPE_PREFIX):]
        )
        self.assertEqual(envelope["message"], "按当前问题重新召回")

    def test_compaction_refresh_replaces_session_context_with_recent_dialogue_and_todo(self) -> None:
        session = self.service.create_session({"title": "压缩刷新"})["session"]
        session_id = str(session["id"])
        self.service.sessions.set_disclosure_preferences(session_id, {"tool:memory": "enabled"})
        with patch.object(
            self.service.runtime,
            "prompt",
            return_value={
                "accepted": True,
                "turnId": "turn:before-compact",
                "piEntryId": "entry:before-compact",
                "response": {"success": True},
            },
        ):
            self.service.prompt(session_id, {"message": "继续完成 Session 记忆刷新"})
        self.service.sessions.mutate_agent_todo(
            session_id,
            {
                "op": "init",
                "phase": "压缩验收",
                "items": ["验证压缩后的 Provider 上下文"],
            },
        )
        self.service.sessions.mutate_agent_todo(
            session_id,
            {"op": "start", "task": "验证压缩后的 Provider 上下文"},
        )

        refreshed = self.service.refresh_session_context(
            {
                "sessionId": session_id,
                "trigger": "compaction",
                "summary": "已经完成首轮召回，接下来验证压缩刷新。",
                "recentMessages": [
                    {
                        "role": "user",
                        "text": "继续完成 Session 记忆刷新；验收标记 ORIGINAL-RECOVERY-AC",
                    },
                    {"role": "assistant", "text": "我已经完成首轮召回"},
                ],
                "agentSkillRecovery": {
                    "schemaVersion": "rag-ime.agent-skill-recovery.v1",
                    "items": [
                        {
                            "name": "notfor",
                            "contentRevision": "a" * 64,
                        }
                    ],
                },
                "agentToolRecovery": {
                    "schemaVersion": "rag-ime.agent-tool-recovery.v1",
                    "catalogRevision": "b" * 64,
                    "items": [
                        {
                            "name": "workspace_read",
                            "schemaRevision": "c" * 64,
                        }
                    ],
                },
            }
        )

        context = refreshed["result"]["sessionContext"]
        self.assertIn("## 当前 Todo", context)
        self.assertIn("验证压缩后的 Provider 上下文", context)
        history_heading = "## 压缩恢复回执（非任务状态）"
        self.assertIn(history_heading, context)
        self.assertEqual(context.count(history_heading), 1)
        self.assertNotIn("ORIGINAL-RECOVERY-AC", context)
        self.assertNotIn("我已经完成首轮召回", context)
        self.assertIn(
            "当前用户消息、本轮 workflow_control、当前任务与当前 Todo",
            context,
        )
        self.assertIn("sha256:", context)
        self.assertIn("notfor@sha256:" + "a" * 64, context)
        self.assertIn("workspace_read@sha256:" + "c" * 64, context)
        self.assertNotIn("## 最近对话", context)
        self.assertNotIn("命中通道", context)
        self.assertNotIn("score=", context)
        self.assertTrue(refreshed["result"]["compactionRecoveryPacket"])
        active = self.service.context_runtime.materialize(session_id)
        self.assertEqual(active["itemIds"], [refreshed["result"]["itemId"]])
        self.assertNotIn("ORIGINAL-RECOVERY-AC", active["prompt"])
        self.assertIn("workspace_read@sha256:" + "c" * 64, active["prompt"])
        self.assertEqual(
            len(self.service.context_runtime.list_items(session_id, status="expired")),
            1,
        )
    def test_completed_todo_compaction_recovers_terminal_state_without_restarting_work(self) -> None:
        session = self.service.create_session({"title": "已完成任务压缩"})["session"]
        session_id = str(session["id"])
        self.service.sessions.set_disclosure_preferences(session_id, {"tool:memory": "enabled"})
        tasks = ["运行失败基线", "完成精确修改", "运行回归测试"]
        self.service.sessions.mutate_agent_todo(
            session_id,
            {"op": "init", "phase": "回归验收", "items": tasks},
        )
        for task in tasks:
            self.service.sessions.mutate_agent_todo(
                session_id,
                {"op": "done", "task": task},
            )

        refreshed = self.service.refresh_session_context(
            {
                "sessionId": session_id,
                "trigger": "compaction",
                "summary": "修复和回归均已完成。",
                "recentMessages": [
                    {
                        "role": "user",
                        "text": "完成 normalize_scores 并通过独立回归测试；验收标记 TERMINAL-RECOVERY-AC",
                    },
                    {
                        "role": "assistant",
                        "text": "实现、测试和最终交付都已完成。",
                    },
                ],
                "agentSkillRecovery": {
                    "schemaVersion": "rag-ime.agent-skill-recovery.v1",
                    "items": [],
                },
                "agentToolRecovery": {
                    "schemaVersion": "rag-ime.agent-tool-recovery.v1",
                    "items": [],
                },
            }
        )

        context = refreshed["result"]["sessionContext"]
        self.assertTrue(refreshed["result"]["compactionRecoveryPacket"])
        history_heading = "## 压缩恢复回执（非任务状态）"
        self.assertIn(history_heading, context)
        self.assertNotIn("TERMINAL-RECOVERY-AC", context)
        self.assertNotIn("当前任务：已完成；不要重复执行。", context)
        self.assertNotIn("Todo 状态：completed", context)
        self.assertNotIn("任务已由本 Session 完成；无待交接责任。", context)
        self.assertNotIn("继续执行上述原始需求。", context)
        self.assertNotIn("## 当前 Todo", context)
        self.assertNotIn("[待办]", context)
        self.assertEqual(context.count(history_heading), 1)

    def test_manual_compaction_does_not_repeat_runtime_context_refresh(self) -> None:
        session = self.service.create_session({"title": "压缩去重"})["session"]
        session_id = str(session["id"])
        with (
            patch.object(
                self.service.runtime,
                "compact",
                return_value={
                    "summary": "压缩后摘要",
                    "firstKeptEntryId": "entry:kept",
                    "contextRefreshApplied": True,
                },
            ),
            patch.object(
                self.service,
                "_probe_memory_maintenance",
                return_value={"ok": True},
            ),
            patch.object(
                self.service,
                "refresh_session_context",
            ) as refresh,
        ):
            compacted = self.service.compact(session_id, {})

        refresh.assert_not_called()
        self.assertEqual(
            compacted["contextRefresh"]["result"]["status"],
            "runtime_applied",
        )
        checkpoint = compacted["result"]["memoryCheckpoint"]
        self.assertFalse(checkpoint["stored"])
        self.assertEqual(checkpoint["status"], "session_context_only")
        self.assertFalse(checkpoint["longTermMemoryEligible"])
        with sqlite3.connect(self.service.db_path) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM agent_memory_sources WHERE session_id = ?",
                    (session_id,),
                ).fetchone()[0],
                0,
            )


    def test_subagent_task_is_combined_with_live_user_query(self) -> None:
        session = self.service.create_session({"title": "受管子任务"})["session"]
        session_id = str(session["id"])
        self.service.sessions.set_disclosure_preferences(session_id, {"tool:memory": "enabled"})
        delegated_run = {
            "task": "只核对 Session 记忆压缩后的 0.8/0.2 向量融合",
            "state": "running",
        }
        with (
            patch.object(self.service.delegation, "owns_session", return_value=True),
            patch.object(
                self.service.delegation.store,
                "run_for_child_session",
                return_value=delegated_run,
            ),
            patch.object(
                self.service.memory_bootstrap,
                "build",
                wraps=self.service.memory_bootstrap.build,
            ) as build,
        ):
            refreshed = self.service.refresh_session_context(
                {
                    "sessionId": session_id,
                    "trigger": "session_start",
                    "queryText": "帮忙看一下",
                    "recentMessages": [
                        {"role": "user", "text": "帮忙看一下"},
                    ],
                }
            )

        self.assertEqual(refreshed["result"]["trigger"], "subagent_task")
        recall_query = build.call_args.kwargs["query_text"]
        self.assertIn("帮忙看一下", recall_query)
        self.assertIn(delegated_run["task"], recall_query)
        self.assertEqual(build.call_args.kwargs["vector_context_weight"], 0.0)
        self.assertIn(
            delegated_run["task"],
            refreshed["result"]["sessionContext"],
        )



    def test_next_prompt_repairs_first_query_bootstrap_failure(self) -> None:
        created = self.service.create_session({"title": "可恢复启动上下文"})
        session_id = str(created["session"]["id"])
        self.service.sessions.set_disclosure_preferences(session_id, {"tool:memory": "enabled"})
        self.assertEqual(
            created["memoryBootstrap"]["status"],
            "awaiting_first_prompt",
        )
        with patch.object(
            self.service.memory_bootstrap,
            "build",
            side_effect=RuntimeError("temporary bootstrap failure"),
        ), patch.object(
            self.service.runtime,
            "prompt",
            return_value={
                "accepted": True,
                "turnId": "turn:bootstrap:failed",
                "piEntryId": "entry:bootstrap:failed",
                "response": {"success": True},
            },
        ):
            failed = self.service.prompt(session_id, {"message": "第一轮"})

        self.assertEqual(failed["memoryBootstrap"]["status"], "recall_failed")
        self.assertEqual(failed["contextItemsDelivered"], 0)
        self.assertEqual(self.service.context_runtime.list_items(session_id), [])

        with patch.object(
            self.service.runtime,
            "prompt",
            return_value={
                "accepted": True,
                "turnId": "turn:bootstrap:repaired",
                "piEntryId": "entry:bootstrap:repaired",
                "response": {"success": True},
            },
        ):
            repaired_prompt = self.service.prompt(session_id, {"message": "第二轮"})

        repaired = self.service.context_runtime.list_items(session_id)
        self.assertEqual(len(repaired), 1)
        self.assertEqual(repaired[0]["sourceKind"], "memory_bootstrap")
        self.assertEqual(repaired[0]["status"], "delivered")
        self.assertEqual(repaired_prompt["memoryBootstrap"]["status"], "ready")

    def test_lost_runtime_response_stays_pending_without_reexecution(self) -> None:
        session = self.service.create_session({"title": "响应丢失"})["session"]
        session_id = str(session["id"])
        self.service.sessions.set_disclosure_preferences(session_id, {"tool:memory": "enabled"})
        sent_messages: list[str] = []

        def lose_response(
            _session_id: str,
            message: str,
            **_kwargs: object,
        ) -> dict[str, object]:
            sent_messages.append(message)
            raise RuntimeError("response lost after dispatch")

        with patch.object(self.service.runtime, "prompt", side_effect=lose_response):
            with self.assertRaises(
                AgentCommandReceiptPending,
            ) as pending:
                self.service.prompt(
                    session_id,
                    {
                        "message": "第一轮",
                        "clientMessageId": "client-bootstrap-loss",
                    },
                )
        self.assertEqual(
            pending.exception.recovery_state,
            "in_flight",
        )

        delivered = self.service.context_runtime.list_items(
            session_id,
            status="delivered",
        )
        self.assertEqual(len(delivered), 1)
        self.assertEqual(
            delivered[0]["deliveredTurnId"],
            "dispatch:client:client-bootstrap-loss",
        )

        with patch.object(self.service.runtime, "prompt") as replay:
            with self.assertRaises(
                AgentCommandReceiptPending,
            ):
                self.service.prompt(
                    session_id,
                    {
                        "message": "第一轮",
                        "clientMessageId": "client-bootstrap-loss",
                    },
                )

        sent_message = sent_messages[0]
        self.assertTrue(sent_message.startswith(RUNTIME_PROMPT_ENVELOPE_PREFIX))
        self.assertEqual(
            json.loads(
                sent_message[len(RUNTIME_PROMPT_ENVELOPE_PREFIX):]
            )["message"],
            "第一轮",
        )
        replay.assert_not_called()

    def test_runtime_disabled_before_prompt_closes_receipt_as_failed(self) -> None:
        session = self.service.create_session({"title": "本地运行时未启用"})["session"]
        session_id = str(session["id"])
        payload = {
            "message": "这条消息不可能已被 Pi 接纳",
            "clientMessageId": "runtime-disabled-before-prompt",
        }

        with patch.object(
            self.service.runtime,
            "prompt",
            side_effect=PiRuntimeError("Pi runtime is disabled"),
        ):
            with self.assertRaises(AgentCommandReceiptFailed) as failed:
                self.service.prompt(session_id, payload)

        self.assertEqual(
            failed.exception.response_payload(),
            {
                "code": "AGENT_COMMAND_FAILED",
                "commandReceipt": {
                    "state": "failed",
                    "clientMessageId": "runtime-disabled-before-prompt",
                    "causeCode": "PI_RUNTIME_DISABLED",
                },
            },
        )

    def test_runtime_transport_errors_keep_acceptance_unknown(self) -> None:
        for client_message_id, runtime_error in (
            (
                "runtime-prompt-timeout",
                "Pi Runtime Host command timed out: session.prompt",
            ),
            (
                "runtime-prompt-eof",
                "Pi Runtime Host stdin closed",
            ),
        ):
            with self.subTest(runtime_error=runtime_error):
                session = self.service.create_session(
                    {"title": client_message_id}
                )["session"]
                with patch.object(
                    self.service.runtime,
                    "prompt",
                    side_effect=PiRuntimeError(runtime_error),
                ):
                    with self.assertRaises(AgentCommandReceiptPending) as pending:
                        self.service.prompt(
                            str(session["id"]),
                            {
                                "message": "接纳状态无法由传输错误判断",
                                "clientMessageId": client_message_id,
                            },
                        )
                self.assertEqual(pending.exception.recovery_state, "in_flight")

    def test_session_chat_does_not_load_optional_persona_package(self) -> None:
        with patch.object(
            self.service.role_books,
            "ensure_seeded",
        ) as ensure_seeded:
            created = self.service.create_session({"title": "降级对话"})
            session = created["session"]
            self.service.sessions.set_disclosure_preferences(str(session["id"]), {"tool:memory": "enabled"})
            self.assertEqual(session["roleBookRevisionId"], "")
            self.assertTrue(created["roleBook"]["ok"])
            self.assertEqual(
                created["roleBook"]["status"],
                "persona_package_not_installed",
            )
            with patch.object(
                self.service.runtime,
                "prompt",
                return_value={
                    "accepted": True,
                    "turnId": "turn:fallback",
                    "piEntryId": "entry:fallback",
                    "response": {"success": True},
                },
            ):
                accepted = self.service.prompt(
                    str(session["id"]),
                    {"message": "继续工作"},
                )
        ensure_seeded.assert_not_called()
        self.assertTrue(accepted["ok"])
        self.assertEqual(accepted["contextItemsDelivered"], 1)

    def test_conversation_fork_clones_identity_policy_and_returns_new_session(self) -> None:
        source = self.service.create_session(
            {
                "title": "原对话",
                "mode": "assistant",
                "roleId": "companion-firstlight-v1",
                "roleVersion": "1",
                "modelProfile": "openai-codex/gpt-5.6-luna",
                "toolProfileVersion": "subagent-readonly-v1",
            }
        )["session"]
        source = self.service.update_session(
            str(source["id"]),
            {
                "mode": "assistant",
                "toolProfileVersion": "subagent-readonly-v1",
                "allowedTools": ["overview", "memory"],
            },
        )["session"]
        runtime = _ForkRuntime(self.service.sessions, self.root)
        self.service.runtime = runtime

        catalog = self.service.fork_candidates(str(source["id"]))
        forked = self.service.fork_session(
            str(source["id"]),
            {"entryId": "entry-user-1", "title": "新路线"},
        )

        self.assertEqual(catalog["items"][0]["entryId"], "entry-user-1")
        target = forked["session"]
        self.assertNotEqual(target["id"], source["id"])
        self.assertEqual(target["title"], "新路线")
        for key in (
            "mode",
            "roleId",
            "roleVersion",
            "modelProfile",
            "toolProfileVersion",
            "toolAllowlistMode",
            "allowedTools",
            "workspaceRoots",
            "shellPolicyVersion",
        ):
            self.assertEqual(target[key], source[key], key)
        self.assertEqual(self.service.sessions.get(str(source["id"])), source)
        self.assertIsNotNone(self.service.sessions.runtime_binding(str(target["id"])))

    def test_failed_conversation_fork_removes_provisional_product_session(self) -> None:
        source = self.service.create_session({"title": "原对话"})["session"]
        runtime = _ForkRuntime(self.service.sessions, self.root, fail=True)
        self.service.runtime = runtime

        with self.assertRaisesRegex(PiRuntimeError, "fork failed"):
            self.service.fork_session(
                str(source["id"]),
                {"entryId": "entry-user-1"},
            )

        self.assertEqual([item["id"] for item in self.service.sessions.list()], [source["id"]])
        self.assertTrue(runtime.target_session_id)

    def test_conversation_fork_is_rejected_before_runtime_when_source_is_not_idle(self) -> None:
        source = self.service.create_session({"title": "正在运行"})["session"]
        self.service.sessions.set_status(str(source["id"]), "busy")
        runtime = _ForkRuntime(self.service.sessions, self.root)
        self.service.runtime = runtime

        with self.assertRaisesRegex(ValueError, "only available for idle"):
            self.service.fork_candidates(str(source["id"]))
        with self.assertRaisesRegex(ValueError, "only available for idle"):
            self.service.fork_session(
                str(source["id"]),
                {"entryId": "entry-user-1"},
            )

        self.assertFalse(runtime.target_session_id)

    def test_conversation_fork_accepts_quiescent_open_session(self) -> None:
        source = self.service.create_session({"title": "已打开但空闲"})["session"]
        session_id = str(source["id"])
        transcript = self.root / "open-idle-source.jsonl"
        transcript.touch()
        self.service.sessions.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-open-idle-source",
            transcript_ref=str(transcript),
            message_count=1,
        )
        runtime = _ForkRuntime(self.service.sessions, self.root)
        self.service.runtime = runtime

        catalog = self.service.fork_candidates(session_id)

        self.assertEqual(catalog["items"][0]["entryId"], "entry-user-1")

    def test_conversation_rewrite_rewinds_same_session_and_resets_live_projection(self) -> None:
        session = self.service.create_session({"title": "原位修改"})["session"]
        session_id = str(session["id"])
        old_event = self.service.events.publish(
            session_id,
            "message_completed",
            {"message": {"id": "old-branch-message"}},
        )
        stream = self.service.events.subscribe(session_id)
        self.assertEqual(next(stream), b": connected\n\n")
        self.assertIn(old_event.event_id.encode(), next(stream))
        with (
            patch.object(
                self.service.runtime,
                "rewind_session",
                return_value={"entryId": "entry-user-1", "leafId": "parent-entry"},
            ) as rewind,
            patch.object(
                self.service.runtime,
                "prompt",
                return_value={
                    "accepted": True,
                    "turnId": "turn:rewrite:1",
                    "piEntryId": "pi-entry:rewrite:1",
                    "response": {"success": True},
                },
            ) as prompt,
            patch.object(
                self.service.sessions,
                "invalidate_recent_message_projection",
            ) as invalidate_recent,
        ):
            response = self.service.rewrite_session(
                session_id,
                {
                    "entryId": "entry-user-1",
                    "message": "修改后的问题",
                    "clientMessageId": "rewrite-command-1",
                },
            )

        self.assertEqual(response["schemaVersion"], "rag-ime.agent-session-rewrite.v1")
        self.assertEqual(response["sessionId"], session_id)
        self.assertEqual(response["entryId"], "entry-user-1")
        rewind.assert_called_once_with(session_id, entry_id="entry-user-1")
        invalidate_recent.assert_called_once_with(session_id)
        self.assertEqual(prompt.call_args.args[0], session_id)
        runtime_message = str(prompt.call_args.args[1])
        self.assertTrue(runtime_message.startswith(RUNTIME_PROMPT_ENVELOPE_PREFIX))
        self.assertEqual(
            json.loads(
                runtime_message[len(RUNTIME_PROMPT_ENVELOPE_PREFIX):]
            )["message"],
            "修改后的问题",
        )
        replay, gap = self.service.events.replay(session_id)
        self.assertFalse(gap)
        self.assertNotIn(old_event.event_id, [event.event_id for event in replay])
        self.assertEqual(replay, [])
        invalidation = json.loads(
            next(
                line
                for line in next(stream).decode("utf-8").splitlines()
                if line.startswith("data: ")
            ).removeprefix("data: ")
        )
        self.assertEqual(invalidation["eventType"], "snapshot_required")
        self.assertEqual(invalidation["payload"]["reason"], "session_rewritten")
        stream.close()

        with (
            patch.object(self.service.runtime, "rewind_session") as replay_rewind,
            patch.object(self.service.runtime, "prompt") as replay_prompt,
        ):
            replay_response = self.service.rewrite_session(
                session_id,
                {
                    "entryId": "entry-user-1",
                    "message": "修改后的问题",
                    "clientMessageId": "rewrite-command-1",
                },
            )
        replay_rewind.assert_not_called()
        replay_prompt.assert_not_called()
        self.assertTrue(replay_response["idempotentReplay"])

    def test_conversation_rewrite_invalidates_to_durable_branch_when_replacement_prompt_fails(
        self,
    ) -> None:
        session = self.service.create_session({"title": "原位修改失败"})["session"]
        session_id = str(session["id"])
        old_event = self.service.events.publish(
            session_id,
            "message_completed",
            {"message": {"id": "old-durable-leaf"}},
        )
        stream = self.service.events.subscribe(session_id)
        self.assertEqual(next(stream), b": connected\n\n")
        self.assertIn(old_event.event_id.encode(), next(stream))

        with (
            patch.object(
                self.service.runtime,
                "rewind_session",
                return_value={"entryId": "entry-user-1", "leafId": "parent-entry"},
            ),
            patch.object(
                self.service.runtime,
                "prompt",
                side_effect=RuntimeError("replacement admission failed"),
            ),
            self.assertRaisesRegex(
                AgentPromptAcceptanceUnknown,
                "Pi acceptance is unknown",
            ),
        ):
            self.service.rewrite_session(
                session_id,
                {
                    "entryId": "entry-user-1",
                    "message": "不会被接纳的问题",
                    "clientMessageId": "rewrite-command-failed",
                },
            )

        invalidation = json.loads(
            next(
                line
                for line in next(stream).decode("utf-8").splitlines()
                if line.startswith("data: ")
            ).removeprefix("data: ")
        )
        self.assertEqual(invalidation["eventType"], "snapshot_required")
        self.assertEqual(invalidation["payload"]["reason"], "session_rewrite_failed")
        replay, gap = self.service.events.replay(session_id)
        self.assertFalse(gap)
        self.assertEqual(replay, [])
        stream.close()

    def test_scene_skill_save_during_active_work_keeps_runtime_and_uses_new_selection(self) -> None:
        runtime = self.service.runtime
        revision = self.service.configuration()["configuration"]["revision"]
        with patch.object(runtime, "runtime_status", return_value={"status": "busy"}), patch.object(
            self.service, "_apply_runtime_policy"
        ) as apply_runtime:
            result = self.service.update_configuration({
                "expectedRevision": revision,
                "changes": {"skillRouting.ordinary": ["systematic-debugging"]},
                "updatedBy": "app-center",
            })
            self.assertTrue(result["ok"])
            self.assertIs(self.service.runtime, runtime)
            apply_runtime.assert_not_called()
            session = self.service.create_session({"title": "New scene selection"})["session"]
            self.assertEqual(self.service._runtime_skill_allowlist(session), ["systematic-debugging"])
            with self.assertRaisesRegex(ValueError, "active turn"):
                self.service.update_configuration({
                    "expectedRevision": result["configuration"]["revision"],
                    "changes": {"runtime.enabled": True},
                })

    def test_prompt_settings_are_visible_and_do_not_restart_the_runtime(self) -> None:
        runtime = self.service.runtime
        initial = self.service.configuration()
        self.assertEqual(initial["promptPolicy"]["appliesTo"], "new_sessions")
        self.assertIn("<work-policy>", initial["promptPolicy"]["builtInSystemPrompt"])
        with patch.object(runtime, "stop", side_effect=AssertionError("prompt edit restarted Runtime")):
            result = self.service.update_configuration({
                "expectedRevision": initial["configuration"]["revision"],
                "changes": {"prompts.systemInstructions": "保留明确的验收边界。"},
                "updatedBy": "settings-ui",
            })
        self.assertTrue(result["ok"])
        self.assertIs(self.service.runtime, runtime)
        self.assertEqual(self.service._runtime_prompt_settings({})["systemInstructions"], "保留明确的验收边界。")

    def test_kernel_configuration_drives_new_sessions_and_runtime_policy(self) -> None:
        initial_runtime = self.service.runtime
        initial = self.service.configuration()["configuration"]
        defaults = self.service.update_configuration(
            {
                "expectedRevision": initial["revision"],
                "changes": {
                    "sessionDefaults.roleId": "companion-firstlight-v1",
                    "modelRouting.primary": {
                        "modelProfile": "deepseek/deepseek-chat",
                        "thinkingLevel": "off",
                    },
                },
                "updatedBy": "mac-control",
            }
        )
        session = self.service.create_session({"title": "默认角色"})["session"]
        self.assertEqual(session["roleId"], "companion-firstlight-v1")
        self.assertEqual(session["modelProfile"], "deepseek/deepseek-chat")

        runtime = self.service.update_configuration(
            {
                "expectedRevision": defaults["configuration"]["revision"],
                "changes": {
                    "runtime.enabled": True,
                    "runtime.idleTimeoutSeconds": 321,
                },
                "updatedBy": "phone-control",
            }
        )
        self.assertTrue(runtime["ok"])
        self.assertEqual(runtime["configuration"]["sync"]["state"], "synchronized")
        self.assertIsNot(self.service.runtime, initial_runtime)
        self.assertIs(
            self.service.role_application.runtime,
            self.service.runtime,
        )
        self.assertIs(
            self.service.session_branching.runtime,
            self.service.runtime,
        )
        self.assertIs(
            self.service.message_snapshot.runtime,
            self.service.runtime,
        )
        self.assertTrue(self.service.runtime_status()["enabled"])
        self.assertEqual(self.service.runtime_status()["idleTimeoutSeconds"], 321)

    def test_model_routing_overrides_legacy_persona_defaults_by_runtime_role(self) -> None:
        initial = self.service.configuration()["configuration"]
        self.service.update_configuration(
            {
                "expectedRevision": initial["revision"],
                "changes": {
                    "modelRouting.primary": {
                        "modelProfile": "openai-codex/gpt-5.6-terra",
                        "thinkingLevel": "high",
                    },
                    "modelRouting.roomCoordinator": {
                        "modelProfile": "openai-codex/gpt-5.6-sol",
                        "thinkingLevel": "xhigh",
                    },
                },
                "updatedBy": "models-ui",
            }
        )

        primary = self.service.create_session(
            {
                "title": "默认主 Agent",
                "roleId": "companion-firstlight-v1",
                "roleVersion": "1",
            }
        )["session"]
        room = self.service.create_session(
            {
                "title": "Room 协调",
                "mode": "coordinator",
                "roleId": "companion-future-v1",
                "roleVersion": "1",
                "_modelRoute": "roomCoordinator",
            }
        )["session"]

        self.assertEqual(primary["modelProfile"], "openai-codex/gpt-5.6-terra")
        self.assertEqual(primary["thinkingLevel"], "high")
        self.assertEqual(room["modelProfile"], "openai-codex/gpt-5.6-sol")
        self.assertEqual(room["thinkingLevel"], "xhigh")

        with (
            patch.object(
                self.service.personas,
                "resolve_active",
                side_effect=AssertionError("core Session loaded Persona"),
            ),
            patch.object(
                self.service.personas,
                "runtime_defaults",
                side_effect=AssertionError("Persona selected model"),
            ),
            patch.object(
                self.service.role_books,
                "ensure_seeded",
                side_effect=AssertionError("core Session seeded Role Book"),
            ),
        ):
            explicit = self.service.create_session(
                {
                    "title": "插件身份仅作元数据",
                    "roleId": "future-persona-package-v1",
                    "roleVersion": "2026.1",
                    "modelProfile": "openai-codex/gpt-5.4",
                    "toolProfileVersion": "subagent-readonly-v1",
                }
            )["session"]
        self.assertEqual(explicit["roleId"], "future-persona-package-v1")
        self.assertEqual(explicit["modelProfile"], "openai-codex/gpt-5.4")
        self.assertEqual(explicit["toolProfileVersion"], "subagent-readonly-v1")
        self.assertEqual(explicit["roleBookRevisionId"], "")

    def test_trace_diagnostic_sessions_use_dedicated_route_and_full_trust_at_creation_only(self) -> None:
        initial = self.service.configuration()["configuration"]
        configured = self.service.update_configuration(
            {
                "expectedRevision": initial["revision"],
                "changes": {
                    "modelRouting.traceDiagnostic": {
                        "modelProfile": "openai-codex/gpt-5.6-terra",
                        "thinkingLevel": "medium",
                    },
                },
                "updatedBy": "models-ui",
            }
        )

        existing = self.service.create_session(
            {
                "mode": "coordinator",
                "title": "Trace diagnostic existing",
                "toolProfileVersion": "control-center-auto-approve-v1",
                "executionMode": "full_trust",
                "dangerousModeConfirmation": "ENABLE_FULL_TRUST",
                "workspaceRoots": ["/"],
                "toolAllowlistMode": "profile",
                "projectContextEnabled": True,
                "piSkillsEnabled": True,
                "codexSkillsEnabled": True,
                "_modelRoute": "traceDiagnostic",
            }
        )["session"]

        self.service.update_configuration(
            {
                "expectedRevision": configured["configuration"]["revision"],
                "changes": {
                    "modelRouting.traceDiagnostic": {
                        "modelProfile": "openai-codex/gpt-5.6-sol",
                        "thinkingLevel": "high",
                    },
                },
                "updatedBy": "models-ui",
            }
        )
        new_session = self.service.create_session(
            {
                "mode": "coordinator",
                "title": "Trace diagnostic new",
                "toolProfileVersion": "control-center-auto-approve-v1",
                "executionMode": "full_trust",
                "dangerousModeConfirmation": "ENABLE_FULL_TRUST",
                "workspaceRoots": ["/"],
                "toolAllowlistMode": "profile",
                "projectContextEnabled": True,
                "piSkillsEnabled": True,
                "codexSkillsEnabled": True,
                "_modelRoute": "traceDiagnostic",
            }
        )["session"]

        self.assertEqual(existing["modelProfile"], "openai-codex/gpt-5.6-terra")
        self.assertEqual(existing["thinkingLevel"], "medium")
        self.assertEqual(new_session["modelProfile"], "openai-codex/gpt-5.6-sol")
        self.assertEqual(new_session["thinkingLevel"], "high")

    def test_service_depends_on_runtime_driver_contract_not_pi_manager(self) -> None:
        factory = _GatewayRuntimeFactory(self.root)
        service = AgentService(
            db_path=self.root / "gateway.sqlite",
            runtime_factory=factory,
        )
        try:
            runtime = service.runtime_status()
            session = service.create_session({"title": "网关会话"})["session"]

            self.assertEqual(runtime["runtimeKind"], "gateway_http")
            self.assertEqual(runtime["driverId"], "test-gateway")
            self.assertEqual(session["modelProfile"], "openai-codex/gpt-5.6-luna")
            self.assertEqual(factory.created_for, ["interactive"])
        finally:
            service.close()
        self.assertTrue(factory.runtime and factory.runtime.stopped)

    def test_supplied_pi_factory_receives_the_product_plugin_approval_token(self) -> None:
        config = PiRuntimeConfig(
            enabled=False,
            executable=None,
            agent_dir=self.root / "plugin-token-agent",
            session_dir=self.root / "plugin-token-sessions",
            logs_dir=self.root / "plugin-token-logs",
        )
        factory = PiRuntimeDriverFactory(config)
        service = AgentService(
            db_path=self.root / "plugin-token.sqlite",
            runtime_config=config,
            runtime_factory=factory,
        )
        try:
            self.assertTrue(service.plugin_approval_token)
            self.assertEqual(
                factory.config.plugin_approval_token,
                service.plugin_approval_token,
            )
        finally:
            service.close()

    def test_direct_chat_legacy_role_is_immutable_session_metadata(self) -> None:
        created = self.service.create_session(
            {
                "title": "Hermes 任务",
                "mode": "assistant",
                "roleId": "companion-firstlight-v1",
                "roleVersion": "1",
            }
        )["session"]

        self.assertEqual(created["roleId"], "companion-firstlight-v1")
        self.assertEqual(created["roleVersion"], "1")
        self.assertEqual(created["modelProfile"], "openai-codex/gpt-5.6-luna")
        self.assertEqual(created["roleBookRevisionId"], "")
        self.assertEqual(created["toolProfileVersion"], "control-center-v1")
        renamed = self.service.update_session(str(created["id"]), {"title": "推进任务"})["session"]
        self.assertEqual(renamed["roleId"], "companion-firstlight-v1")
        self.assertEqual(renamed["roleVersion"], "1")

        coordinator = self.service.create_session(
            {
                "title": "未来协调",
                "mode": "coordinator",
                "roleId": "companion-future-v1",
                "roleVersion": "1",
                "workspaceRoots": [self.root.as_posix()],
            }
        )["session"]
        self.assertEqual(coordinator["mode"], "coordinator")
        self.assertEqual(coordinator["workspaceRoots"], [str(self.root.resolve())])

    def test_scheduled_thread_wake_stays_running_until_agent_settles(self) -> None:
        session = self.service.create_session({"title": "定时整理"})["session"]
        wake_at_ms = int(time.time() * 1000) + 2_000
        schedule = self.service.create_wake_schedule(
            {
                "title": "整理今日工作",
                "instruction": "列出完成项并汇报",
                "targetType": "session",
                "targetSessionId": session["id"],
                "wakeAtMs": wake_at_ms,
                "planningTaskId": "task:today",
                "confirmText": "schedule",
            }
        )["schedule"]
        claim = self.service.wake_schedules.claim_due(now_ms=wake_at_ms)[0]

        with patch.object(
            self.service,
            "prompt",
            return_value={"accepted": True, "turnId": "turn:wake"},
        ) as prompt:
            self.service._dispatch_scheduled_wake(claim)

        message = prompt.call_args.args[1]["message"]
        self.assertEqual(message, "预约任务已到期：整理今日工作")
        context_items = self.service.list_context_items(str(session["id"]))["items"]
        self.assertEqual(context_items[0]["sourceKind"], "wake_schedule")
        self.assertEqual(context_items[0]["lane"], "schedule")
        self.assertEqual(
            self.service.get_wake_schedule(str(schedule["id"]))["status"],
            "running",
        )

        self.service.events.publish(
            str(session["id"]),
            "turn_completed",
            {},
            turn_id="turn:wake",
        )
        self.assertTrue(self.service.events.flush())
        finished = self.service.get_wake_schedule(str(schedule["id"]))
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["latestRun"]["state"], "completed")

    def test_scheduled_role_wake_creates_visible_persona_session(self) -> None:
        wake_at_ms = int(time.time() * 1000) + 2_000
        schedule = self.service.create_wake_schedule(
            {
                "title": "研究任务",
                "instruction": "核对今天的研究结论",
                "targetType": "role",
                "targetRoleId": "companion-firstlight-v1",
                "targetRoleVersion": "1",
                "wakeAtMs": wake_at_ms,
                "confirmText": "schedule",
            }
        )["schedule"]
        claim = self.service.wake_schedules.claim_due(now_ms=wake_at_ms)[0]

        with patch.object(
            self.service,
            "prompt",
            return_value={"accepted": True, "turnId": "turn:role-wake"},
        ) as prompt:
            self.service._dispatch_scheduled_wake(claim)

        created_session_id = prompt.call_args.args[0]
        created = self.service.sessions.get(created_session_id)
        self.assertEqual(created["title"], "预约 · 研究任务")
        self.assertEqual(created["roleId"], "companion-firstlight-v1")
        running = self.service.get_wake_schedule(str(schedule["id"]))
        self.assertEqual(running["latestRun"]["sessionId"], created_session_id)

    def test_busy_scheduled_thread_is_deferred_without_consuming_run(self) -> None:
        session = self.service.create_session({"title": "正在执行"})["session"]
        wake_at_ms = int(time.time() * 1000) + 2_000
        schedule = self.service.create_wake_schedule(
            {
                "title": "稍后继续",
                "instruction": "继续现有任务",
                "targetType": "session",
                "targetSessionId": session["id"],
                "wakeAtMs": wake_at_ms,
                "confirmText": "schedule",
            }
        )["schedule"]
        claim = self.service.wake_schedules.claim_due(now_ms=wake_at_ms)[0]
        self.service.sessions.set_status(str(session["id"]), "busy")

        with patch.object(self.service, "prompt") as prompt:
            self.service._dispatch_scheduled_wake(claim)

        prompt.assert_not_called()
        deferred = self.service.get_wake_schedule(str(schedule["id"]))
        self.assertEqual(deferred["status"], "scheduled")
        self.assertEqual(deferred["runCount"], 0)
        self.assertEqual(deferred["latestRun"]["state"], "deferred")

    def test_agent_can_preview_a_wake_for_its_current_thread_without_repeating_id(self) -> None:
        session = self.service.create_session({"title": "当前 Agent"})["session"]

        preview = self.service.preview_wake_schedule(
            {
                "title": "稍后继续",
                "instruction": "继续整理当前结论",
                "targetType": "session",
                "wakeAtMs": int(time.time() * 1000) + 60_000,
            },
            requested_by_session_id=str(session["id"]),
        )

        self.assertEqual(preview["schedule"]["targetSessionId"], session["id"])
        self.assertEqual(preview["schedule"]["targetDisplayName"], "当前 Agent")

    def test_user_created_persona_is_optional_session_metadata(self) -> None:
        created_role = self.service.create_role(
            {
                "displayName": "澄·雨天",
                "tagline": "在安静的雨天陪你整理",
                "summary": "偏向温和复盘与日常记录。",
                "traits": ["温和", "善于复盘"],
                "timelineModel": "terra",
                "selectableModes": ["assistant", "coordinator"],
                "suitableTasks": ["温和复盘", "日常记录"],
                "unsuitableTasks": ["高风险独立决定"],
            }
        )["role"]

        self.assertTrue(str(created_role["roleId"]).startswith("persona-"))
        for internal_key in (
            "personaPrompt",
            "systemPrompt",
            "safetyPolicyPrompt",
            "toolPolicy",
            "origin",
        ):
            self.assertNotIn(internal_key, created_role)
        self.assertEqual(self.service.list_roles()["items"][-1], created_role)
        self.assertEqual(created_role["runtimeCharacteristics"]["suitableTasks"], ["温和复盘", "日常记录"])
        private_role = self.service.personas.resolve(created_role["roleId"], created_role["version"])
        self.assertIn("澄·雨天", private_role.system_prompt)
        self.assertIn("能力可见不等于获得许可", private_role.system_prompt)
        session = self.service.create_session(
            {
                "title": "雨天整理",
                "mode": "coordinator",
                "roleId": created_role["roleId"],
                "roleVersion": created_role["version"],
            }
        )["session"]
        self.assertEqual(session["roleId"], created_role["roleId"])
        self.assertEqual(session["roleVersion"], "1")
        self.assertEqual(session["modelProfile"], "openai-codex/gpt-5.6-luna")
        self.assertEqual(session["toolProfileVersion"], "control-center-v1")

        room = self.service.create_room(
            {
                "title": "雨天协作",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": created_role["roleId"], "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        self.assertEqual(room["participants"][0]["roleId"], created_role["roleId"])

        readonly = self.service.create_session(
            {
                "title": "独立工具策略",
                "roleId": created_role["roleId"],
                "roleVersion": "1",
                "toolProfileVersion": "subagent-readonly-v1",
            }
        )["session"]
        self.assertEqual(readonly["roleId"], created_role["roleId"])
        self.assertEqual(readonly["toolProfileVersion"], "subagent-readonly-v1")
        self.assertEqual(readonly["roleBookRevisionId"], "")

    def test_room_intercom_delivery_uses_pi_transcript_without_memory_checkpoint(self) -> None:
        room = self.service.create_room(
            {
                "title": "边界讨论",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                    {"roleId": "companion-future-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        source, target = room["participants"]
        source_session = self.service.sessions.get(str(source["sessionId"]))
        target_session = self.service.sessions.get(str(target["sessionId"]))
        self.assertEqual(
            source_session["modelProfile"],
            "openai-codex/gpt-5.6-sol",
        )
        self.assertEqual(
            target_session["modelProfile"],
            "openai-codex/gpt-5.6-sol",
        )
        item = {
            "id": "room-message:test",
            "kind": "ask",
            "sourceParticipantId": source["id"],
            "targetParticipantId": target["id"],
            "sourceSessionId": source["sessionId"],
            "targetSessionId": target["sessionId"],
            "replyTo": "",
            "content": "控制面板是否只是配置客户端？",
        }

        with (
            patch.object(self.service, "_room_target_idle", return_value=True),
            patch.object(
                self.service.runtime,
                "prompt",
                return_value={"accepted": True, "turnId": "turn:intercom"},
            ) as prompt,
            patch.object(
                self.service.memory_sources,
                "checkpoint_user_message",
            ) as checkpoint,
        ):
            accepted = self.service._deliver_room_intercom(item)

        self.assertEqual(accepted["turnId"], "turn:intercom")
        self.assertIn("房间协作消息", prompt.call_args.args[1])
        self.assertIn("在 Room 中直接回复结论", prompt.call_args.args[1])
        self.assertNotIn("room_post", prompt.call_args.args[1])
        self.assertNotIn("agents.room_reply", prompt.call_args.args[1])
        checkpoint.assert_not_called()

    def test_plain_room_notice_runs_once_without_public_or_a2a_echo(
        self,
    ) -> None:
        room = self.service.create_room(
            {
                "title": "私有通知边界",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {
                        "roleId": "companion-firstlight-v1",
                        "roleVersion": "1",
                    },
                    {
                        "roleId": "companion-future-v1",
                        "roleVersion": "1",
                    },
                ],
            }
        )["room"]
        source, target = room["participants"]
        room_id = str(room["id"])
        target_session_id = str(target["sessionId"])
        item = {
            "id": "room-message:private-notice",
            "roomId": room_id,
            "kind": "send",
            "sourceParticipantId": source["id"],
            "targetParticipantId": target["id"],
            "sourceSessionId": source["sessionId"],
            "targetSessionId": target_session_id,
            "replyTo": "",
            "workItemId": "",
            "workAction": "",
            "content": "状态已同步，无需回复。",
        }
        public_before = len(
            self.service.rooms.list_events(room_id)
        )
        intercom_before = len(
            self.service.room_intercom.list(
                target_session_id,
            )
        )
        work_before = len(
            self.service.room_work.list_for_room(room_id)
        )

        def prompt_once(
            session_id: str,
            prompt_text: str,
            **_kwargs: object,
        ) -> dict[str, object]:
            self.assertEqual(session_id, target_session_id)
            self.assertNotIn("调用 room_post", prompt_text)
            self.assertIn("不要公开重复或回复礼貌回声", prompt_text)
            self.assertIn("无需行动时直接结束本轮", prompt_text)
            self.service.events.publish(
                session_id,
                "text_delta",
                {"delta": "收到"},
                turn_id="turn:private-notice",
            )
            self.service.events.publish(
                session_id,
                "turn_completed",
                {"status": "completed"},
                turn_id="turn:private-notice",
            )
            return {
                "accepted": True,
                "turnId": "turn:private-notice",
            }

        with (
            patch.object(
                self.service,
                "_room_target_idle",
                return_value=True,
            ),
            patch.object(
                self.service.runtime,
                "prompt",
                side_effect=prompt_once,
            ) as provider_prompt,
        ):
            accepted = self.service._deliver_room_intercom(
                item
            )
            self.service._publish_room_intercom_audit(
                item,
                "queued",
            )
            self.service._publish_room_intercom_audit(
                {
                    **item,
                    "acceptedTurnId": "turn:private-notice",
                },
                "delivered",
            )

        provider_prompt.assert_called_once()
        self.assertTrue(accepted["privateNotice"])
        self.assertEqual(
            len(self.service.rooms.list_events(room_id)),
            public_before,
        )
        self.assertEqual(
            len(
                self.service.room_intercom.list(
                    target_session_id,
                )
            ),
            intercom_before,
        )
        self.assertEqual(
            len(self.service.room_work.list_for_room(room_id)),
            work_before,
        )

    def test_message_snapshot_returns_event_resume_cursor(self) -> None:
        session = self.service.create_session({"title": "恢复游标"})["session"]
        session_id = str(session["id"])
        self.service.sessions.mutate_agent_todo(
            session_id,
            {
                "op": "init",
                "phase": "恢复",
                "items": ["恢复可见 Todo"],
            },
        )
        self.service.sessions.mutate_agent_todo(
            session_id,
            {"op": "start", "task": "恢复可见 Todo"},
        )
        event = self.service.events.publish(session_id, "status_changed", {"status": "ready"})
        with patch.object(self.service.runtime, "session_snapshot", return_value={"messages": []}):
            response = self.service.messages(session_id)

        self.assertEqual(response["lastSequence"], event.sequence)
        self.assertEqual(response["resumeToken"], event.resume_token)
        self.assertEqual(response["status"], "idle")
        # Settled idle status changes only advance the durable resume cursor;
        # they are not restored as a ghost live turn after refresh.
        self.assertEqual(response["liveEvents"], [])
        self.assertEqual(response["todo"]["phases"][0]["tasks"][0]["content"], "恢复可见 Todo")
        self.assertEqual(response["todo"]["phases"][0]["tasks"][0]["status"], "in_progress")



    def test_message_snapshot_reconciles_provider_loop_count_to_visible_messages(self) -> None:
        session = self.service.create_session({"title": "可见消息计数"})["session"]
        session_id = str(session["id"])
        self.service.sessions.set_status(session_id, "idle", message_count=42)
        visible = {
            "schemaVersion": "rag-ime.agent-message.v1",
            "id": "message:visible",
            "sessionId": session_id,
            "turnId": "history:visible",
            "role": "user",
            "status": "completed",
            "blocks": [{
                "id": "message:visible:text",
                "type": "text",
                "status": "completed",
                "presentationKind": "markdown",
                "data": {"text": "恢复这条消息"},
            }],
            "attachments": [],
            "citations": [],
            "createdAtMs": 10,
            "completedAtMs": 10,
        }
        with patch.object(
            self.service.runtime,
            "session_snapshot",
            create=True,
            return_value={
                "messages": [visible],
                "toolHistoryEvents": [],
                "telemetry": None,
                "messageQueue": None,
            },
        ):
            response = self.service.messages(session_id)

        self.assertEqual(len(response["items"]), 1)
        self.assertEqual(self.service.sessions.get(session_id)["messageCount"], 1)

    def test_message_snapshot_keeps_ordinary_session_messages_unchanged(
        self,
    ) -> None:
        session = self.service.create_session(
            {"title": "普通 Session 快照"}
        )["session"]
        session_id = str(session["id"])
        history = [
            {
                "schemaVersion": "rag-ime.agent-message.v1",
                "id": "message:ordinary",
                "sessionId": session_id,
                "turnId": "turn:ordinary",
                "role": "assistant",
                "status": "completed",
                "blocks": [
                    {
                        "id": "message:ordinary:text",
                        "type": "text",
                        "status": "completed",
                        "presentationKind": "markdown",
                        "data": {"text": "普通私有对话保持原样"},
                    }
                ],
                "attachments": [],
                "citations": [],
                "createdAtMs": 10,
                "completedAtMs": 11,
            }
        ]
        with patch.object(
            self.service.runtime,
            "session_snapshot",
            create=True,
            return_value={
                "messages": history,
                "toolHistoryEvents": [],
                "telemetry": None,
                "messageQueue": None,
            },
        ):
            response = self.service.messages(session_id)

        self.assertEqual(response["items"], history)

    def test_message_snapshot_recovers_managed_html_link_for_historical_reply(
        self,
    ) -> None:
        session = self.service.create_session(
            {"title": "历史 HTML 产物"}
        )["session"]
        session_id = str(session["id"])
        receipt = self.service.media.import_bytes(
            session_id=session_id,
            data=b"<!doctype html><html><body>managed report</body></html>",
            mime_type="text/html",
            file_name="project-intro.html",
            origin="tool_result",
            origin_tool="workspace_write",
        )
        history = [
            {
                "schemaVersion": "rag-ime.agent-message.v1",
                "id": "message:historical-html",
                "sessionId": session_id,
                "turnId": "turn:historical-html",
                "role": "assistant",
                "status": "completed",
                "blocks": [
                    {
                        "id": "message:historical-html:text",
                        "type": "text",
                        "status": "completed",
                        "presentationKind": "markdown",
                        "data": {
                            "text": (
                                "已完成独立网页介绍："
                                "[打开 project-intro.html](./project-intro.html)"
                            )
                        },
                    }
                ],
                "attachments": [],
                "citations": [],
                "createdAtMs": 10,
                "completedAtMs": 11,
            }
        ]
        with patch.object(
            self.service.runtime,
            "session_snapshot",
            create=True,
            return_value={
                "messages": history,
                "toolHistoryEvents": [],
                "telemetry": None,
                "messageQueue": None,
            },
        ):
            response = self.service.messages(session_id)

        recovered = response["items"][0]
        file_blocks = [
            block
            for block in recovered["blocks"]
            if block.get("type") == "file"
        ]
        self.assertEqual(len(file_blocks), 1)
        self.assertEqual(
            file_blocks[0]["data"]["mediaId"],
            receipt["mediaId"],
        )
        self.assertEqual(
            file_blocks[0]["data"]["fileName"],
            "project-intro.html",
        )

    def test_message_snapshot_rejects_unowned_or_nested_historical_html_links(
        self,
    ) -> None:
        session = self.service.create_session(
            {"title": "受限历史 HTML"}
        )["session"]
        session_id = str(session["id"])
        other = self.service.create_session(
            {"title": "其他 Session"}
        )["session"]
        self.service.media.import_bytes(
            session_id=session_id,
            data=b"<!doctype html><p>nested</p>",
            mime_type="text/html",
            file_name="nested.html",
            origin="tool_result",
            origin_tool="workspace_write",
        )
        self.service.media.import_bytes(
            session_id=str(other["id"]),
            data=b"<!doctype html><p>other</p>",
            mime_type="text/html",
            file_name="other.html",
            origin="tool_result",
            origin_tool="workspace_write",
        )
        self.service.media.import_bytes(
            session_id=session_id,
            data=b"<!doctype html><p>attachment</p>",
            mime_type="text/html",
            file_name="attachment.html",
            origin="user_attachment",
        )
        history = [{
            "schemaVersion": "rag-ime.agent-message.v1",
            "id": "message:untrusted-html",
            "sessionId": session_id,
            "turnId": "turn:untrusted-html",
            "role": "assistant",
            "status": "completed",
            "blocks": [{
                "id": "message:untrusted-html:text",
                "type": "text",
                "status": "completed",
                "presentationKind": "markdown",
                "data": {
                    "text": (
                        "[嵌套路径](../nested.html) "
                        "[其他会话](other.html) "
                        "[用户附件](attachment.html)"
                    )
                },
            }],
            "attachments": [],
            "citations": [],
            "createdAtMs": 10,
            "completedAtMs": 11,
        }]
        with patch.object(
            self.service.runtime,
            "session_snapshot",
            create=True,
            return_value={
                "messages": history,
                "toolHistoryEvents": [],
                "telemetry": None,
                "messageQueue": None,
            },
        ):
            response = self.service.messages(session_id)

        self.assertFalse(any(
            block.get("type") == "file"
            for block in response["items"][0]["blocks"]
        ))

    def test_recent_room_message_snapshot_skips_runtime_and_bounds_events(
        self,
    ) -> None:
        session = self.service.create_session(
            {"title": "Room 近期快照"}
        )["session"]
        session_id = str(session["id"])
        room_user_event = {
            "eventId": "room:user:recent",
            "eventType": "user_message",
            "turnId": "root:recent",
            "sequence": 1,
            "createdAtMs": 10,
            "payload": {
                "text": "先显示正在发生的工作",
                "clientMessageId": "room-client-recent",
            },
        }
        for index in range(80):
            self.service.events.publish(
                session_id,
                "reasoning_summary",
                {"summary": f"近期进展 {index + 1}"},
                turn_id="turn:recent",
                created_at_ms=100 + index,
            )

        with (
            patch.object(
                self.service.message_snapshot,
                "_room_recent_public_messages",
                return_value={
                    "participantId": "participant:recent",
                    "events": [room_user_event],
                },
            ),
            patch.object(
                self.service.message_snapshot,
                "_runtime_provider",
                side_effect=AssertionError(
                    "recent Room snapshot must not restore Pi Runtime"
                ),
            ),
        ):
            response = self.service.message_snapshot.messages(
                session_id,
                view="recent",
            )

        sequences = [
            int(event["sequence"])
            for event in response["liveEvents"]
        ]
        self.assertEqual(response["snapshotScope"], "recent")
        self.assertTrue(response["partial"])
        self.assertEqual(len(response["liveEvents"]), 48)
        self.assertEqual(sequences, list(range(33, 81)))
        self.assertEqual(response["recentFromSequence"], 33)
        self.assertEqual(response["lastSequence"], 80)
        self.assertEqual(response["resumeToken"], f"{session_id}:80")
        self.assertEqual(
            [item["id"] for item in response["items"]],
            ["room-event:room:user:recent"],
        )
        self.assertEqual(
            response["items"][0]["clientMessageId"],
            "room-client-recent",
        )

    def test_room_conversation_snapshot_uses_management_facade(self) -> None:
        room = self.service.create_room(
            {
                "title": "Room 对话首屏",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {
                        "roleId": "companion-present-v1",
                        "roleVersion": "1",
                    },
                    {
                        "roleId": "companion-firstlight-v1",
                        "roleVersion": "1",
                    },
                ],
            }
        )["room"]
        room_id = str(room["id"])
        event = self.service.rooms.append_event(
            room_id=room_id,
            event_type="user_message",
            payload={"text": "通过公开服务读取"},
            turn_id="root:conversation-facade",
        )

        response = self.service.room_conversation_snapshot(room_id)

        self.assertEqual(
            response["schemaVersion"],
            "rag-ime.agent-room-conversation-snapshot.v1",
        )
        self.assertEqual(response["room"]["id"], room_id)
        self.assertEqual(
            response["events"][-1]["eventId"],
            event["eventId"],
        )

    def test_recent_room_message_snapshot_never_loads_full_room_history(
        self,
    ) -> None:
        room = self.service.create_room(
            {
                "title": "Room 轻量首屏",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {
                        "roleId": "companion-present-v1",
                        "roleVersion": "1",
                    },
                    {
                        "roleId": "companion-firstlight-v1",
                        "roleVersion": "1",
                    },
                ],
            }
        )["room"]
        target = room["participants"][0]
        room_id = str(room["id"])
        session_id = str(target["sessionId"])
        user_event = self.service.rooms.append_event(
            room_id=room_id,
            event_type="user_message",
            payload={"text": "立即显示最近的 Room 对话"},
            turn_id="root:recent-fast",
            created_at_ms=10,
        )

        with (
            patch.object(
                self.service.rooms,
                "list_events",
                side_effect=AssertionError(
                    "recent snapshot must not load the full Room history"
                ),
            ) as full_history,
            patch.object(
                self.service.rooms,
                "recent_public_messages",
                wraps=self.service.rooms.recent_public_messages,
            ) as recent_history,
        ):
            response = self.service.message_snapshot.messages(
                session_id,
                view="recent",
            )

        full_history.assert_not_called()
        recent_history.assert_called_once_with(room_id, limit=100)
        self.assertEqual(response["snapshotScope"], "recent")
        self.assertTrue(response["partial"])
        self.assertEqual(
            [item["id"] for item in response["items"]],
            ["room-event:" + str(user_event["eventId"])],
        )

    def test_full_message_snapshot_remains_complete_after_recent_window(
        self,
    ) -> None:
        session = self.service.create_session(
            {"title": "Room 完整快照兼容"}
        )["session"]
        session_id = str(session["id"])
        durable_history = []
        for index in range(60):
            durable_history.append(self.service.events.publish(
                session_id,
                "reasoning_summary",
                {"summary": f"完整进展 {index + 1}"},
                turn_id="turn:full",
                created_at_ms=100 + index,
            ).to_payload())
        runtime_snapshot = {
            "messages": [],
            "toolHistoryEvents": durable_history,
            "telemetry": None,
            "messageQueue": None,
        }
        with (
            patch.object(
                self.service.message_snapshot,
                "_room_public_messages",
                return_value={
                    "participantId": "participant:full",
                    "events": [],
                },
            ),
            patch.object(
                self.service.runtime,
                "session_snapshot",
                create=True,
                return_value=runtime_snapshot,
            ) as session_snapshot,
        ):
            response = self.service.messages(session_id)

        session_snapshot.assert_called_once_with(session_id)
        self.assertEqual(len(response["liveEvents"]), 60)
        self.assertNotIn("snapshotScope", response)
        self.assertNotIn("partial", response)

    def test_idle_snapshot_drops_settled_stream_journal_and_uses_durable_activity(
        self,
    ) -> None:
        session = self.service.create_session(
            {"title": "已完成流不重放"}
        )["session"]
        session_id = str(session["id"])
        turn_id = "turn:settled-stream"
        for index in range(500):
            self.service.events.publish(
                session_id,
                "text_delta",
                {
                    "messageId": f"{turn_id}:assistant",
                    "blockId": f"{turn_id}:assistant:text",
                    "contentIndex": 0,
                    "delta": f"LINE-{index + 1}\n",
                    "replaceBlock": index == 0,
                },
                turn_id=turn_id,
            )
        self.service.events.publish(
            session_id,
            "message_completed",
            {"message": {"id": f"{turn_id}:assistant"}},
            turn_id=turn_id,
        )
        self.service.events.publish(
            session_id,
            "turn_completed",
            {"status": "completed"},
            turn_id=turn_id,
        )
        self.service.events.publish(
            session_id,
            "status_changed",
            {"status": "aborting", "pendingAdmission": True},
        )
        self.assertTrue(self.service.events.flush())
        durable_reasoning = AgentEventEnvelope(
            event_id=f"{session_id}:history:reasoning",
            session_id=session_id,
            turn_id="history:user-1",
            sequence=1,
            created_at_ms=100,
            event_type="reasoning_summary",
            payload={"summary": "已完成的公开思考摘要"},
            resume_token=f"{session_id}:history:reasoning",
        ).to_payload()
        with patch.object(
            self.service.runtime,
            "session_snapshot",
            create=True,
            return_value={
                "messages": [],
                "toolHistoryEvents": [durable_reasoning],
                "telemetry": None,
                "messageQueue": None,
            },
        ):
            response = self.service.messages(session_id)

        self.assertEqual(
            [event["eventType"] for event in response["liveEvents"]],
            ["reasoning_summary"],
        )
        self.assertLess(
            len(json.dumps(response["liveEvents"], ensure_ascii=False)),
            2_000,
        )

    def test_active_snapshot_coalesces_stream_deltas(self) -> None:
        session = self.service.create_session(
            {"title": "活动流压缩"}
        )["session"]
        session_id = str(session["id"])
        turn_id = "turn:active-stream"
        self.service.sessions.set_status(session_id, "busy")
        for index in range(100):
            self.service.events.publish(
                session_id,
                "text_delta",
                {
                    "messageId": f"{turn_id}:assistant",
                    "blockId": f"{turn_id}:assistant:text",
                    "contentIndex": 0,
                    "delta": str(index),
                    "replaceBlock": index == 0,
                },
                turn_id=turn_id,
            )
        self.service.events.publish(
            session_id,
            "status_changed",
            {"status": "busy"},
        )
        self.assertTrue(self.service.events.flush())
        with (
            patch.object(
                self.service.runtime,
                "session_snapshot",
                create=True,
                return_value={
                    "messages": [],
                    "toolHistoryEvents": [],
                    "telemetry": None,
                    "messageQueue": None,
                },
            ),
            patch.object(
                self.service.runtime,
                "runtime_status",
                return_value={
                    "status": "busy",
                    "activeSessionIds": [session_id],
                    "activeSessionId": session_id,
                },
            ),
        ):
            response = self.service.messages(session_id)

        deltas = [
            event
            for event in response["liveEvents"]
            if event["eventType"] == "text_delta"
        ]
        self.assertEqual(len(deltas), 1)
        self.assertEqual(
            deltas[0]["payload"]["delta"],
            "".join(str(index) for index in range(100)),
        )
        self.assertTrue(deltas[0]["payload"]["replaceBlock"])
        self.assertEqual(
            [
                event["payload"]["status"]
                for event in response["liveEvents"]
                if event["eventType"] == "status_changed"
            ],
            ["busy"],
        )

    def test_recent_view_keeps_ordinary_session_off_the_full_runtime_restore(
        self,
    ) -> None:
        session = self.service.create_session(
            {"title": "普通 Session 近期请求"}
        )["session"]
        session_id = str(session["id"])
        for index in range(80):
            self.service.events.publish(
                session_id,
                "reasoning_summary",
                {"summary": f"普通 Session 近期进展 {index + 1}"},
                turn_id="turn:recent-session",
                created_at_ms=100 + index,
            )
        recent_message = {
            "schemaVersion": "rag-ime.agent-message.v1",
            "id": "recent:assistant",
            "sessionId": session_id,
            "turnId": "history:recent:user",
            "role": "assistant",
            "status": "completed",
            "blocks": [{
                "id": "history:recent:user:text:0",
                "type": "text",
                "status": "completed",
                "presentationKind": "markdown",
                "data": {"text": "最近一轮已经完成"},
            }],
            "attachments": [],
            "citations": [],
            "createdAtMs": 100,
            "completedAtMs": 100,
        }
        with (
            patch.object(
                self.service.runtime,
                "recent_session_snapshot",
                create=True,
                return_value={"messages": [recent_message]},
            ) as recent_snapshot,
            patch.object(
                self.service.runtime,
                "session_snapshot",
                create=True,
                side_effect=AssertionError(
                    "recent ordinary Session snapshot must not restore Pi transcript"
                ),
            ),
            patch.object(
                self.service.runtime,
                "messages",
                create=True,
                side_effect=AssertionError(
                    "recent ordinary Session snapshot must not load full messages"
                ),
            ),
        ):
            response = self.service.message_snapshot.messages(
                session_id,
                view="recent",
            )

        recent_snapshot.assert_called_once_with(session_id)
        self.assertEqual(response["snapshotScope"], "recent")
        self.assertTrue(response["partial"])
        self.assertEqual(len(response["liveEvents"]), 48)
        self.assertEqual(response["recentFromSequence"], 33)
        self.assertEqual(response["lastSequence"], 80)
        self.assertEqual(response["resumeToken"], f"{session_id}:80")
        self.assertEqual(
            [item["id"] for item in response["items"]],
            ["recent:assistant"],
        )

    def test_recent_view_restores_unpersisted_active_text_stream(self) -> None:
        session = self.service.create_session(
            {"title": "近期快照恢复当前输出"}
        )["session"]
        session_id = str(session["id"])
        turn_id = "turn:recent-active-stream"
        self.service.sessions.set_status(session_id, "busy")
        durable = self.service.events.publish(
            session_id,
            "status_changed",
            {"status": "busy"},
            turn_id=turn_id,
        )
        for index in range(100):
            self.service.events.publish(
                session_id,
                "text_delta",
                {
                    "messageId": f"{turn_id}:assistant",
                    "blockId": f"{turn_id}:assistant:text",
                    "contentIndex": 0,
                    "delta": f"chunk-{index};",
                    "replaceBlock": index == 0,
                },
                turn_id=turn_id,
            )

        with patch.object(
            self.service.runtime,
            "recent_session_snapshot",
            create=True,
            return_value={"messages": [], "toolHistoryEvents": []},
        ):
            response = self.service.message_snapshot.messages(
                session_id,
                view="recent",
            )

        self.assertEqual(
            self.service.sessions.max_event_sequence(session_id),
            durable.sequence,
        )
        self.assertEqual(response["lastSequence"], 101)
        self.assertEqual(response["resumeToken"], f"{session_id}:101")
        deltas = [
            event
            for event in response["liveEvents"]
            if event["eventType"] == "text_delta"
        ]
        self.assertEqual(len(deltas), 1)
        self.assertTrue(deltas[0]["payload"]["replaceBlock"])
        self.assertEqual(
            deltas[0]["payload"]["delta"],
            "".join(f"chunk-{index};" for index in range(100)),
        )

    def test_recent_room_view_restores_unpersisted_active_text_stream(
        self,
    ) -> None:
        session = self.service.create_session(
            {"title": "Room 近期快照恢复当前输出"}
        )["session"]
        session_id = str(session["id"])
        turn_id = "turn:recent-room-active-stream"
        self.service.sessions.set_status(session_id, "busy")
        durable = self.service.events.publish(
            session_id,
            "status_changed",
            {"status": "busy"},
            turn_id=turn_id,
        )
        for index in range(100):
            self.service.events.publish(
                session_id,
                "text_delta",
                {
                    "messageId": f"{turn_id}:assistant",
                    "blockId": f"{turn_id}:assistant:text",
                    "contentIndex": 0,
                    "delta": f"room-{index};",
                    "replaceBlock": index == 0,
                },
                turn_id=turn_id,
            )

        with patch.object(
            self.service.message_snapshot,
            "_room_recent_public_messages",
            return_value={"participantId": "participant:recent", "events": []},
        ):
            response = self.service.message_snapshot.messages(
                session_id,
                view="recent",
            )

        self.assertEqual(
            self.service.sessions.max_event_sequence(session_id),
            durable.sequence,
        )
        self.assertEqual(response["lastSequence"], 101)
        self.assertEqual(response["resumeToken"], f"{session_id}:101")
        deltas = [
            event
            for event in response["liveEvents"]
            if event["eventType"] == "text_delta"
        ]
        self.assertEqual(len(deltas), 1)
        self.assertTrue(deltas[0]["payload"]["replaceBlock"])
        self.assertEqual(
            deltas[0]["payload"]["delta"],
            "".join(f"room-{index};" for index in range(100)),
        )

    def test_recent_view_restores_bounded_durable_tool_progress(self) -> None:
        session = self.service.create_session({"title": "近期工具进度"})["session"]
        session_id = str(session["id"])
        history = [
            AgentEventEnvelope(
                event_id=f"{session_id}:history:reasoning",
                session_id=session_id,
                turn_id="history:user",
                sequence=1,
                created_at_ms=100,
                event_type="reasoning_summary",
                payload={"summary": "正在核对文件", "items": ["正在核对文件"], "state": "completed"},
                resume_token=f"{session_id}:history:reasoning",
            ).to_payload(),
            AgentEventEnvelope(
                event_id=f"{session_id}:history:tool",
                session_id=session_id,
                turn_id="history:user",
                sequence=2,
                created_at_ms=110,
                event_type="tool_finished",
                payload={"toolCallId": "tool:recent", "toolName": "workspace_read", "isError": False},
                resume_token=f"{session_id}:history:tool",
            ).to_payload(),
        ]
        with patch.object(
            self.service.runtime,
            "recent_session_snapshot",
            create=True,
            return_value={"messages": [], "toolHistoryEvents": history},
        ):
            response = self.service.message_snapshot.messages(session_id, view="recent")

        self.assertEqual(
            [event["eventId"] for event in response["liveEvents"]],
            [event["eventId"] for event in history],
        )

    def test_recent_view_keeps_durable_pending_user_after_long_live_activity(
        self,
    ) -> None:
        session = self.service.create_session(
            {"title": "刷新后仍显示当前问题"}
        )["session"]
        session_id = str(session["id"])
        self.service.sessions.set_status(session_id, "busy")
        for index in range(80):
            self.service.events.publish(
                session_id,
                "reasoning_summary",
                {"summary": f"后台步骤 {index + 1}"},
                turn_id="turn:pending-refresh",
                created_at_ms=100 + index,
            )
        pending_user = {
            "schemaVersion": "rag-ime.agent-message.v1",
            "id": "recent:pending-user",
            "sessionId": session_id,
            "turnId": "history:pending-user",
            "role": "user",
            "status": "completed",
            "blocks": [{
                "id": "recent:pending-user:text:0",
                "type": "text",
                "status": "completed",
                "presentationKind": "markdown",
                "data": {"text": "刷新后也要保留这一条问题"},
            }],
            "attachments": [],
            "citations": [],
            "createdAtMs": 90,
            "completedAtMs": 90,
        }
        with (
            patch.object(
                self.service.runtime,
                "recent_session_snapshot",
                create=True,
                return_value={"messages": [pending_user]},
            ),
            patch.object(
                self.service.runtime,
                "runtime_status",
                return_value={
                    "status": "busy",
                    "activeSessionId": session_id,
                    "activeSessionIds": [session_id],
                    "openSessionIds": [session_id],
                },
            ),
            patch.object(
                self.service.runtime,
                "session_snapshot",
                create=True,
                side_effect=AssertionError(
                    "recent pending-user projection must not restore full history"
                ),
            ),
        ):
            response = self.service.message_snapshot.messages(
                session_id,
                view="recent",
            )

        self.assertEqual(len(response["liveEvents"]), 48)
        self.assertEqual(
            [item["id"] for item in response["items"]],
            ["recent:pending-user"],
        )
        self.assertEqual(response["status"], "busy")

    def test_recent_view_reconciles_stale_busy_session_from_local_runtime_status(
        self,
    ) -> None:
        session = self.service.create_session(
            {"title": "停止后残留忙碌状态"}
        )["session"]
        session_id = str(session["id"])
        self.service.sessions.set_status(session_id, "busy")

        with (
            patch.object(
                self.service.runtime,
                "runtime_status",
                return_value={
                    "status": "ready",
                    "activeSessionId": session_id,
                    "activeSessionIds": [],
                    "openSessionIds": [session_id],
                },
            ) as runtime_status,
            patch.object(
                self.service.runtime,
                "session_snapshot",
                create=True,
                side_effect=AssertionError(
                    "recent reconciliation must not restore the Pi transcript"
                ),
            ),
        ):
            response = self.service.message_snapshot.messages(
                session_id,
                view="recent",
            )

        runtime_status.assert_called_once_with()
        self.assertEqual(response["status"], "idle")
        self.assertTrue(response["runtimeQuiescent"])
        self.assertEqual(self.service.sessions.get(session_id)["status"], "idle")

    def test_recent_view_keeps_only_the_exact_runtime_session_busy(self) -> None:
        target = self.service.create_session({"title": "近期状态目标"})[
            "session"
        ]
        peer = self.service.create_session({"title": "真实后台回合"})[
            "session"
        ]
        target_id = str(target["id"])
        peer_id = str(peer["id"])
        self.service.sessions.set_status(target_id, "busy")

        with patch.object(
            self.service.runtime,
            "runtime_status",
            return_value={
                "status": "busy",
                "activeSessionId": peer_id,
                "activeSessionIds": [peer_id],
                "openSessionIds": [target_id, peer_id],
            },
        ):
            stale_target = self.service.message_snapshot.messages(
                target_id,
                view="recent",
            )

        self.assertEqual(stale_target["status"], "idle")
        self.service.sessions.set_status(target_id, "busy")
        with patch.object(
            self.service.runtime,
            "runtime_status",
            return_value={
                "status": "busy",
                "activeSessionId": target_id,
                "activeSessionIds": [target_id, peer_id],
                "openSessionIds": [target_id, peer_id],
            },
        ):
            live_target = self.service.message_snapshot.messages(
                target_id,
                view="recent",
            )

        self.assertEqual(live_target["status"], "busy")
        self.assertEqual(self.service.sessions.get(target_id)["status"], "busy")

    def test_room_message_snapshot_projects_public_conversation_once(
        self,
    ) -> None:
        room = self.service.create_room(
            {
                "title": "公开等待快照",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {
                        "roleId": "companion-present-v1",
                        "roleVersion": "1",
                    },
                    {
                        "roleId": "companion-firstlight-v1",
                        "roleVersion": "1",
                    },
                ],
            }
        )["room"]
        target = room["participants"][0]
        other = room["participants"][1]
        room_id = str(room["id"])
        session_id = str(target["sessionId"])
        user_event = self.service.rooms.append_event(
            room_id=room_id,
            event_type="user_message",
            payload={"text": "请先确认采用哪一种方案"},
            turn_id="root:wait",
            created_at_ms=10,
        )
        self.service.rooms.append_event(
            room_id=room_id,
            event_type="participant_message",
            payload={
                "resultPublic": False,
                "data": {
                    "message": {
                        "text": "PRIVATE-PARTICIPANT-OUTPUT",
                    }
                },
            },
            turn_id="root:wait",
            participant_id=str(target["id"]),
            source_session_id=session_id,
            created_at_ms=20,
        )
        first_post_event = self.service.rooms.append_event(
            room_id=room_id,
            event_type="room_post",
            payload={
                "post": {
                    "postId": "post:wait",
                    "content": "我需要你选择稳妥方案或快速方案。",
                }
            },
            turn_id="root:wait",
            participant_id=str(target["id"]),
            source_session_id=session_id,
            created_at_ms=30,
        )
        self.service.rooms.append_event(
            room_id=room_id,
            event_type="room_post",
            payload={
                "post": {
                    "postId": "post:wait",
                    "content": "我需要你选择稳妥方案或快速方案。",
                }
            },
            turn_id="root:wait",
            participant_id=str(target["id"]),
            source_session_id=session_id,
            created_at_ms=31,
        )
        self.service.rooms.append_event(
            room_id=room_id,
            event_type="room_post",
            payload={
                "post": {
                    "postId": "post:other",
                    "content": "这是另一位参与者的公开内容。",
                }
            },
            turn_id="root:other",
            participant_id=str(other["id"]),
            source_session_id=str(other["sessionId"]),
            created_at_ms=32,
        )
        bootstrap = {
            "schemaVersion": "rag-ime.agent-message.v1",
            "id": "message:room-bootstrap",
            "sessionId": session_id,
            "turnId": "root:wait",
            "role": "user",
            "status": "completed",
            "blocks": [
                {
                    "id": "message:room-bootstrap:text",
                    "type": "text",
                    "status": "completed",
                    "presentationKind": "markdown",
                    "data": {
                        "text": (
                            "<room-context>\n"
                            "用户在 Room 中的请求：\n"
                            "请先确认采用哪一种方案\n"
                            "</room-context>"
                        )
                    },
                }
            ],
            "attachments": [],
            "citations": [],
            "createdAtMs": 10,
            "completedAtMs": 10,
        }
        empty_tool_turn = {
            "schemaVersion": "rag-ime.agent-message.v1",
            "id": "message:empty-tool-turn",
            "sessionId": session_id,
            "turnId": "root:wait",
            "role": "assistant",
            "status": "completed",
            "blocks": [
                {
                    "id": "message:empty-tool-turn:tool",
                    "type": "tool_call",
                    "status": "completed",
                    "presentationKind": "tool_call",
                    "data": {
                        "toolName": "room_commit",
                        "toolCallId": "tool:room-wait",
                    },
                }
            ],
            "attachments": [],
            "citations": [],
            "createdAtMs": 20,
            "completedAtMs": 20,
        }
        runtime_snapshot = {
            "messages": [bootstrap, empty_tool_turn],
            "toolHistoryEvents": [],
            "telemetry": None,
            "messageQueue": None,
        }
        event_count = len(self.service.rooms.list_events(room_id))

        with patch.object(
            self.service.runtime,
            "session_snapshot",
            create=True,
            return_value=runtime_snapshot,
        ):
            first = self.service.messages(session_id)
            second = self.service.messages(session_id)

        expected_ids = [
            f"room-event:{user_event['eventId']}",
            "message:empty-tool-turn",
            "room-post:post:wait",
        ]
        self.assertEqual(
            [message["id"] for message in first["items"]],
            expected_ids,
        )
        self.assertEqual(first["items"], second["items"])
        self.assertEqual(
            [
                message["role"]
                for message in first["items"]
            ],
            ["user", "assistant", "assistant"],
        )
        visible_text = [
            str(block.get("data", {}).get("text") or "")
            for message in first["items"]
            for block in message.get("blocks", [])
            if block.get("type") == "text"
        ]
        self.assertEqual(
            visible_text,
            [
                "请先确认采用哪一种方案",
                "我需要你选择稳妥方案或快速方案。",
            ],
        )
        serialized = json.dumps(first["items"], ensure_ascii=False)
        self.assertNotIn("<room-context>", serialized)
        self.assertNotIn("这是另一位参与者的公开内容", serialized)
        self.assertNotIn(
            "PRIVATE-PARTICIPANT-OUTPUT",
            serialized,
        )
        self.assertEqual(
            sum(
                message["id"] == "room-post:post:wait"
                for message in first["items"]
            ),
            1,
        )
        self.assertEqual(
            first_post_event["payload"]["post"]["postId"],
            "post:wait",
        )
        self.assertEqual(
            len(self.service.rooms.list_events(room_id)),
            event_count,
        )
        self.assertEqual(
            self.service.sessions.get(session_id)["messageCount"],
            len(expected_ids),
        )

    def test_room_message_snapshot_uses_pi_user_copy_for_the_completed_turn(
        self,
    ) -> None:
        room = self.service.create_room(
            {
                "title": "Room 与 Pi 用户消息去重",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {
                        "roleId": "companion-present-v1",
                        "roleVersion": "1",
                    },
                    {
                        "roleId": "companion-firstlight-v1",
                        "roleVersion": "1",
                    }
                ],
            }
        )["room"]
        target = room["participants"][0]
        room_id = str(room["id"])
        session_id = str(target["sessionId"])
        self.service.rooms.append_event(
            room_id=room_id,
            event_type="user_message",
            payload={"text": "我想给 Agent 对话加 TUI 模式"},
            turn_id="room-turn:tui",
            created_at_ms=10_000,
        )
        repeated_event = self.service.rooms.append_event(
            room_id=room_id,
            event_type="user_message",
            payload={"text": "我想给 Agent 对话加 TUI 模式"},
            turn_id="room-turn:tui-repeat",
            created_at_ms=100_000,
        )
        pi_user = {
            "schemaVersion": "rag-ime.agent-message.v1",
            "id": "message:pi-user",
            "sessionId": session_id,
            "turnId": "history:pi-user",
            "role": "user",
            "status": "completed",
            "blocks": [
                {
                    "id": "message:pi-user:text",
                    "type": "text",
                    "status": "completed",
                    "presentationKind": "markdown",
                    "data": {"text": "我想给 Agent 对话加 TUI 模式"},
                }
            ],
            "attachments": [],
            "citations": [],
            "createdAtMs": 17_000,
            "completedAtMs": 17_000,
        }
        pi_assistant = {
            "schemaVersion": "rag-ime.agent-message.v1",
            "id": "message:pi-assistant",
            "sessionId": session_id,
            "turnId": "history:pi-user",
            "role": "assistant",
            "status": "completed",
            "blocks": [
                {
                    "id": "message:pi-assistant:text",
                    "type": "text",
                    "status": "completed",
                    "presentationKind": "markdown",
                    "data": {"text": "已完成 TUI 方案调查。"},
                }
            ],
            "attachments": [],
            "citations": [],
            "createdAtMs": 18_000,
            "completedAtMs": 18_000,
        }
        runtime_snapshot = {
            "messages": [pi_user, pi_assistant],
            "toolHistoryEvents": [],
            "telemetry": None,
            "messageQueue": None,
        }

        with patch.object(
            self.service.runtime,
            "session_snapshot",
            create=True,
            return_value=runtime_snapshot,
        ):
            response = self.service.messages(session_id)

        self.assertEqual(
            [message["id"] for message in response["items"]],
            [
                "message:pi-user",
                "message:pi-assistant",
                f"room-event:{repeated_event['eventId']}",
            ],
        )
        self.assertEqual(
            {message["turnId"] for message in response["items"]},
            {"history:pi-user", "room-turn:tui-repeat"},
        )


    def test_message_snapshot_keeps_completed_tools_after_replay_eviction(self) -> None:
        session = self.service.create_session({"title": "工具历史恢复"})["session"]
        session_id = str(session["id"])
        self.service.events.publish(
            session_id,
            "tool_started",
            {"toolCallId": "tool-1", "toolName": "workspace_read", "args": {}},
            turn_id="history:user-1",
        )
        history = [
            AgentEventEnvelope(
                event_id=f"{session_id}:history:start",
                session_id=session_id,
                turn_id="history:user-1",
                sequence=1,
                created_at_ms=100,
                event_type="tool_started",
                payload={"toolCallId": "tool-1", "toolName": "workspace_read", "args": {"path": "README.md"}},
                resume_token=f"{session_id}:history:start",
            ).to_payload(),
            AgentEventEnvelope(
                event_id=f"{session_id}:history:finish",
                session_id=session_id,
                turn_id="history:user-1",
                sequence=2,
                created_at_ms=101,
                event_type="tool_finished",
                payload={"toolCallId": "tool-1", "toolName": "workspace_read", "args": {}, "result": {"summary": "读取完成"}},
                resume_token=f"{session_id}:history:finish",
            ).to_payload(),
        ]
        with patch.object(
            self.service.runtime,
            "session_snapshot",
            create=True,
            return_value={
                "messages": [],
                "toolHistoryEvents": history,
                "telemetry": None,
                "messageQueue": None,
            },
        ), patch.object(
            self.service.observations,
            "snapshot",
            return_value={
                "items": [
                    {
                        "phase": "tool_finished",
                        "createdAtMs": 690,
                        "refs": [{"kind": "tool_call", "id": "tool-1"}],
                    },
                    {
                        "phase": "tool_started",
                        "createdAtMs": 550,
                        "refs": [{"kind": "tool_call", "id": "tool-1"}],
                    },
                ],
            },
        ):
            response = self.service.messages(session_id)

        tool_events = [
            event for event in response["liveEvents"]
            if event["eventType"] in {"tool_started", "tool_finished"}
        ]
        self.assertEqual([event["eventType"] for event in tool_events], ["tool_started", "tool_finished"])
        self.assertEqual([event["createdAtMs"] for event in tool_events], [550, 690])
        self.assertEqual(tool_events[0]["payload"]["args"], {"path": "README.md"})
        self.assertEqual(tool_events[1]["payload"]["result"], {"summary": "读取完成"})

    def test_message_snapshot_treats_open_transcript_as_idle_without_a_live_turn(self) -> None:
        session = self.service.create_session({"title": "旧会话恢复"})["session"]
        session_id = str(session["id"])
        self.service.sessions.set_status(session_id, "active")
        with (
            patch.object(self.service.runtime, "session_snapshot", return_value={"messages": []}),
            patch.object(
                self.service.runtime,
                "runtime_status",
                return_value={
                    "status": "ready",
                    "activeSessionId": session_id,
                    "activeSessionIds": [],
                    "openSessionIds": [session_id],
                },
            ),
        ):
            response = self.service.messages(session_id)

        self.assertEqual(response["status"], "idle")
        self.assertEqual(self.service.sessions.get(session_id)["status"], "idle")

    def test_message_snapshot_keeps_the_exact_runtime_turn_busy(self) -> None:
        session = self.service.create_session({"title": "当前回合"})["session"]
        session_id = str(session["id"])
        self.service.sessions.set_status(session_id, "active")
        with (
            patch.object(self.service.runtime, "session_snapshot", return_value={"messages": []}),
            patch.object(
                self.service.runtime,
                "runtime_status",
                return_value={
                    "status": "busy",
                    "activeSessionId": session_id,
                    "activeSessionIds": [session_id],
                    "openSessionIds": [session_id],
                },
            ),
        ):
            response = self.service.messages(session_id)

        self.assertEqual(response["status"], "busy")
        self.assertEqual(self.service.sessions.get(session_id)["status"], "busy")

    def test_message_snapshot_reconstructs_durable_pending_approval(self) -> None:
        session = self.service.create_session({"title": "待审批恢复"})["session"]
        session_id = str(session["id"])
        approval = self.service.sessions.create_approval(
            session_id=session_id,
            tool_name="workspace_write_file",
            operation="写入 README.md",
            payload_sha256="a" * 64,
            preview={"path": "README.md"},
            risk_level="R2",
            ttl_ms=120_000,
        )
        with patch.object(self.service.runtime, "session_snapshot", return_value={"messages": []}):
            response = self.service.messages(session_id)

        pending = [
            event
            for event in response["liveEvents"]
            if event["eventType"] == "approval_required"
        ]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["payload"]["approvalId"], approval["approvalId"])
        self.assertEqual(pending[0]["payload"]["payloadSha256"], "a" * 64)
        self.assertEqual(pending[0]["turnId"], f"approval:{approval['approvalId']}")

    def test_message_snapshot_does_not_duplicate_replayed_pending_approval(self) -> None:
        session = self.service.create_session({"title": "审批事件去重"})["session"]
        session_id = str(session["id"])
        approval = self.service.sessions.create_approval(
            session_id=session_id,
            tool_name="workspace_write_file",
            operation="写入 README.md",
            payload_sha256="b" * 64,
            preview={"path": "README.md"},
            risk_level="R2",
            ttl_ms=120_000,
        )
        event = self.service.events.publish(
            session_id,
            "approval_required",
            approval,
            turn_id="turn:approval",
        )
        with patch.object(self.service.runtime, "session_snapshot", return_value={"messages": []}):
            response = self.service.messages(session_id)

        pending = [
            item
            for item in response["liveEvents"]
            if item["eventType"] == "approval_required"
        ]
        self.assertEqual(pending, [event.to_payload()])

    def test_model_catalog_and_selection_are_owned_by_pi_session(self) -> None:
        session = self.service.create_session({"title": "模型切换"})["session"]
        session_id = str(session["id"])
        model = {
            "provider": "openrouter",
            "id": "anthropic/claude-sonnet",
            "name": "Claude Sonnet",
            "api": "openai-completions",
            "reasoning": True,
            "thinkingLevels": ["off", "low", "medium", "high"],
            "supportsImages": True,
            "contextWindow": 200000,
            "maxTokens": 16384,
        }
        with patch.object(
            self.service.runtime,
            "model_catalog",
            return_value={"selected": model, "models": [model], "thinkingLevel": "medium"},
        ):
            catalog = self.service.model_catalog(session_id)

        self.assertEqual(catalog["providers"][0]["displayName"], "OpenRouter")
        self.assertEqual(catalog["selected"]["id"], "anthropic/claude-sonnet")
        self.assertEqual(catalog["thinkingLevel"], "medium")
        self.assertNotIn("apiKey", json.dumps(catalog))

        with patch.object(
            self.service.runtime,
            "model_catalog",
            return_value={"selected": model, "models": [], "thinkingLevel": "medium"},
        ):
            partial_catalog = self.service.model_catalog(session_id)
        self.assertEqual(partial_catalog["providers"][0]["models"][0]["id"], model["id"])

        selected_session = self.service.sessions.set_model_profile(
            session_id,
            "openrouter/anthropic/claude-sonnet",
        )
        with patch.object(
            self.service.runtime,
            "set_model",
            return_value={"selected": model, "session": selected_session},
        ) as select:
            response = self.service.select_model(
                session_id,
                {"provider": "openrouter", "modelId": "anthropic/claude-sonnet"},
            )

        select.assert_called_once_with(
            session_id,
            provider="openrouter",
            model_id="anthropic/claude-sonnet",
        )

        self.assertEqual(response["session"]["modelProfile"], "openrouter/anthropic/claude-sonnet")

        with patch.object(
            self.service.runtime,
            "set_thinking_level",
            return_value={"thinkingLevel": "high", "selected": model},
        ) as set_thinking:
            thinking = self.service.select_thinking_level(session_id, {"level": "high"})
        set_thinking.assert_called_once_with(session_id, level="high")
        self.assertEqual(thinking["thinkingLevel"], "high")

        events, gap = self.service.events.replay(session_id)
        self.assertFalse(gap)
        configuration_events = [
            event for event in events if event.event_type == "session_configuration_changed"
        ]
        self.assertEqual(
            [event.payload["kind"] for event in configuration_events],
            ["model", "thinking"],
        )

    def test_role_runtime_defaults_are_legacy_metadata_not_session_model_policy(self) -> None:
        runtime_config = PiRuntimeConfig(
            enabled=False,
            executable=None,
            agent_dir=self.root / "role-agent-config",
            session_dir=self.root / "role-sessions",
            logs_dir=self.root / "role-logs",
            provider="deepseek",
            model="deepseek-v4-flash",
            model_providers={
                "deepseek": {
                    "models": [
                        {
                            "id": "deepseek-v4-flash",
                            "name": "DeepSeek V4 Flash",
                            "reasoning": False,
                        }
                    ]
                }
            },
        )
        service = AgentService(
            db_path=self.root / "role-defaults.sqlite",
            runtime_config=runtime_config,
            process_id_provider=lambda: self.process_id,
        )
        available_models = [
            {
                "provider": "openai-codex",
                "id": "gpt-5.6-luna",
                "name": "GPT-5.6 Luna",
                "reasoning": True,
                "thinkingLevels": ["off", "max"],
            },
            {
                "provider": "openai-codex",
                "id": "gpt-5.6-terra",
                "name": "GPT-5.6 Terra",
                "reasoning": True,
                "thinkingLevels": ["off", "max"],
            },
            {
                "provider": "openai-codex",
                "id": "gpt-5.6-sol",
                "name": "GPT-5.6 Sol",
                "reasoning": True,
                "thinkingLevels": ["off", "xhigh"],
            },
        ]
        with patch.object(service.runtime, "available_models", return_value=available_models):
            initial_roles = {
                item["roleId"]: item["defaults"] for item in service.list_roles()["items"]
            }
        self.assertEqual(
            initial_roles["companion-firstlight-v1"],
            {
                "modelPolicy": "fixed",
                "memoryPolicy": "personal-evidence-v1",
                "toolProfileVersion": "control-center-v1",
                "modelProfile": "openai-codex/gpt-5.6-luna",
                "thinkingLevel": "max",
            },
        )
        self.assertEqual(initial_roles["companion-present-v1"]["modelProfile"], "openai-codex/gpt-5.6-terra")
        self.assertEqual(initial_roles["companion-present-v1"]["thinkingLevel"], "max")
        self.assertEqual(initial_roles["companion-future-v1"]["modelProfile"], "openai-codex/gpt-5.6-sol")
        self.assertEqual(initial_roles["companion-future-v1"]["thinkingLevel"], "max")
        self.assertEqual(initial_roles["companion-flash-v1"]["modelProfile"], "openai-codex/gpt-5.6-luna")
        self.assertEqual(initial_roles["companion-flash-v1"]["thinkingLevel"], "low")
        with patch.object(service.runtime, "available_models", return_value=available_models):
            catalog = service.role_model_catalog()
        self.assertEqual(catalog["providers"][0]["models"][0]["name"], "GPT-5.6 Luna")
        self.assertEqual(
            catalog["selected"],
            {"provider": "openai-codex", "id": "gpt-5.6-luna"},
        )
        with patch.object(service.runtime, "available_models", return_value=available_models):
            updated = service.update_role_runtime_defaults(
                {"roleId": "companion-present-v1", "roleVersion": "1", "provider": "openai-codex",
                 "modelId": "gpt-5.6-luna", "thinkingLevel": "max"}
            )
            with self.assertRaisesRegex(ValueError, "必须启用"):
                service.update_role_runtime_defaults(
                    {"roleId": "companion-present-v1", "roleVersion": "1", "provider": "openai-codex",
                     "modelId": "gpt-5.6-luna", "thinkingLevel": "off"}
                )
        self.assertEqual(updated["defaults"]["modelProfile"], "openai-codex/gpt-5.6-luna")
        with patch.object(service.runtime, "set_thinking_level") as set_thinking:
            session = service.create_session(
                {"title": "角色不决定模型", "roleId": "companion-present-v1", "roleVersion": "1"}
            )["session"]
        self.assertEqual(session["modelProfile"], "openai-codex/gpt-5.6-luna")
        self.assertEqual(session["thinkingLevel"], "max")
        set_thinking.assert_not_called()

        explicit = service.create_session(
            {
                "title": "本轮显式模型",
                "roleId": "companion-present-v1",
                "roleVersion": "1",
                "modelProfile": "openai-codex/gpt-5.6-sol",
            }
        )["session"]
        self.assertEqual(explicit["modelProfile"], "openai-codex/gpt-5.6-sol")
        self.assertEqual(explicit["thinkingLevel"], "")

        with patch.object(
            service.runtime,
            "available_models",
            return_value=[{
                "provider": "deepseek",
                "id": "deepseek-v4-flash",
                "name": "DeepSeek V4 Flash",
                "reasoning": False,
                "thinkingLevels": ["off"],
            }],
        ):
            with self.assertRaisesRegex(ValueError, "必须支持推理"):
                service.update_role_runtime_defaults(
                    {"roleId": "companion-present-v1", "roleVersion": "1", "provider": "deepseek",
                     "modelId": "deepseek-v4-flash", "thinkingLevel": "off"}
                )

    def test_command_catalog_exposes_only_pi_prompt_commands_and_degrades_cleanly(self) -> None:
        session = self.service.create_session({"title": "命令目录"})["session"]
        session_id = str(session["id"])
        pi_commands = [
            {
                "name": "review",
                "invocation": "/review",
                "description": "Review the active change",
                "source": "extension",
            }
        ]
        with patch.object(
            self.service.runtime,
            "command_catalog",
            return_value=pi_commands,
        ):
            response = self.service.command_catalog(session_id)

        self.assertTrue(response["runtimeAvailable"])
        self.assertEqual(response["items"], pi_commands)

        with patch.object(
            self.service.runtime,
            "command_catalog",
            side_effect=PiRuntimeError("private runtime path"),
        ):
            unavailable = self.service.command_catalog(session_id)

        self.assertFalse(unavailable["runtimeAvailable"])
        self.assertEqual(unavailable["items"], [])
        self.assertNotIn("private runtime path", json.dumps(unavailable))

    def test_extension_app_skill_commands_are_scoped_to_the_owning_surface(self) -> None:
        ordinary = self.service.create_session({"title": "普通 Agent"})["session"]
        app = self.service.create_session(
            {
                "title": "掌柜问数 · 问数",
                "surfaceKind": "extension_app",
                "ownerAppId": "extension:zhanggui-wenshu",
                "surfaceKey": "ask",
            }
        )["session"]
        commands = [
            {
                "name": "skill:systematic-debugging",
                "invocation": "/skill:systematic-debugging",
                "description": "通用调试",
                "source": "skill",
            },
            {
                "name": "skill:zhanggui-wenshu",
                "invocation": "/skill:zhanggui-wenshu",
                "description": "掌柜问数专用能力",
                "source": "skill",
            },
            {
                "name": "skill:other-app",
                "invocation": "/skill:other-app",
                "description": "其他 App 专用能力",
                "source": "skill",
            },
        ]
        self.service.bind_extension_app_skill_owners(
            lambda: {
                "zhanggui-wenshu": "extension:zhanggui-wenshu",
                "other-app": "extension:other-app",
            }
        )

        with patch.object(self.service.runtime, "command_catalog", return_value=commands):
            ordinary_catalog = self.service.command_catalog(str(ordinary["id"]))
            app_catalog = self.service.command_catalog(str(app["id"]))

        self.assertEqual(
            [item["name"] for item in ordinary_catalog["items"]],
            ["skill:systematic-debugging"],
        )
        self.assertEqual(
            [item["name"] for item in app_catalog["items"]],
            ["skill:systematic-debugging", "skill:zhanggui-wenshu"],
        )

        with patch.object(
            self.service.runtime,
            "invoke_command",
            return_value={
                "command": "/skill:zhanggui-wenshu",
                "name": "skill:zhanggui-wenshu",
                "handled": True,
                "result": {},
                "leafId": "leaf:app-skill",
            },
        ) as invoke:
            with self.assertRaisesRegex(ValueError, "belongs to Extension App"):
                self.service.invoke_command(
                    str(ordinary["id"]),
                    {"command": "/skill:zhanggui-wenshu"},
                )
            allowed = self.service.invoke_command(
                str(app["id"]),
                {"command": "/skill:zhanggui-wenshu"},
            )

        self.assertTrue(allowed["handled"])
        invoke.assert_called_once()

    def test_session_lifecycle_probes_memory_due_without_running_the_organizer(self) -> None:
        session = self.service.create_session({"title": "生命周期"})["session"]
        session_id = str(session["id"])
        triggers: list[str] = []

        def probe(payload):
            triggers.append(str(payload["trigger"]))
            return {
                "schemaVersion": "rag-ime.agent-memory-maintenance-status.v1",
                "ok": True,
                "policy": "review",
                "autoApply": False,
                "due": True,
                "dueReason": "idle",
                "compileState": {"pendingEventCount": 999},
                "ownerCuration": {
                    "pendingSourceCount": 4,
                    "needsReviewSourceCount": 2,
                },
                "pendingDraftCount": 1,
                "runs": [],
            }

        self.service.bind_memory_maintenance_probe(probe)
        with patch.object(
            self.service.runtime,
            "ensure",
            return_value={"state": {"sessionId": "pi-test"}, "session": session},
        ):
            ensured = self.service.ensure_runtime({"sessionId": session_id})
        archived = self.service.update_session(session_id, {"archived": True})
        with patch.object(self.service.runtime, "compact", return_value={"compacted": True}):
            compacted = self.service.compact(session_id, {})

        self.assertEqual(triggers, ["session_switch", "session_archive", "compaction"])
        self.assertEqual(ensured["memoryMaintenance"]["trigger"], "session_switch")
        self.assertEqual(archived["memoryMaintenance"]["trigger"], "session_archive")
        self.assertEqual(compacted["memoryMaintenance"]["trigger"], "compaction")
        events, gap = self.service.events.replay(session_id)
        self.assertFalse(gap)
        self.assertEqual(
            [event.event_type for event in events],
            ["memory_maintenance_updated"] * 3,
        )
        self.assertTrue(all(event.payload["due"] is True for event in events))
        self.assertTrue(all(event.payload["pendingSourceCount"] == 4 for event in events))
        self.assertTrue(all(event.payload["needsReviewSourceCount"] == 2 for event in events))
        self.assertTrue(all(event.payload["legacyPendingEventCount"] == 999 for event in events))
        self.assertTrue(all("4 条受治理证据" in event.payload["summary"] for event in events))

    def test_session_input_and_runtime_capabilities_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "workspaceRoots must be an array"):
            self.service.create_session({"title": "bad", "workspaceRoots": self.root.as_posix()})
        metadata_only = self.service.create_session(
            {"title": "metadata", "roleId": "model-injected-role"}
        )["session"]
        self.assertEqual(metadata_only["roleId"], "model-injected-role")
        self.assertEqual(metadata_only["roleBookRevisionId"], "")
        with self.assertRaisesRegex(ValueError, "cannot carry workspace roots"):
            self.service.create_session({"title": "bad", "workspaceRoots": [self.root.as_posix()]})

        session_count = len(self.service.list_sessions()["items"])
        with self.assertRaisesRegex(
            ValueError,
            "workspaceRoots contains a directory that no longer exists",
        ):
            self.service.create_session(
                {
                    "title": "不能创建幽灵工作区对话",
                    "mode": "coordinator",
                    "workspaceRoots": [
                        (self.root / "removed-workspace").as_posix()
                    ],
                }
            )
        self.assertEqual(
            len(self.service.list_sessions()["items"]),
            session_count,
        )

        coordinator = self.service.create_session(
            {
                "title": "运行协调",
                "mode": "coordinator",
                "workspaceRoots": [self.root.as_posix()],
            }
        )["session"]
        self.assertEqual(coordinator["mode"], "coordinator")
        with self.assertRaisesRegex(PiRuntimeError, "disabled"):
            self.service.ensure_runtime({"sessionId": coordinator["id"]})

    def test_delete_only_removes_session_file_inside_managed_root(self) -> None:
        inside = self.service.create_session({"title": "inside"})["session"]
        session_root = self.service.runtime.config.session_dir
        session_root.mkdir(parents=True)
        inside_file = session_root / "inside.jsonl"
        inside_file.write_text("{}\n", encoding="utf-8")
        self.service.sessions.bind_pi_session(
            str(inside["id"]),
            pi_session_id="pi-inside",
            session_file=str(inside_file),
        )
        deleted = self.service.delete_session(str(inside["id"]))
        self.assertTrue(deleted["sessionFileDeleted"])
        self.assertFalse(inside_file.exists())

        outside = self.service.create_session({"title": "outside"})["session"]
        outside_file = self.root / "outside.jsonl"
        outside_file.write_text("{}\n", encoding="utf-8")
        self.service.sessions.bind_pi_session(
            str(outside["id"]),
            pi_session_id="pi-outside",
            session_file=str(outside_file),
        )
        deleted = self.service.delete_session(str(outside["id"]))
        self.assertFalse(deleted["sessionFileDeleted"])
        self.assertTrue(outside_file.exists())

    def test_text_attachment_works_with_a_text_only_model_and_preserves_file_history(self) -> None:
        session = self.service.create_session({"title": "长文本"})["session"]
        session_id = str(session["id"])
        body = "全文不会自动塞入上下文\r\n" * 800
        receipt = self.service.import_media(session_id=session_id, data=body.encode(), mime_type="text/plain", file_name="需求.txt")["media"]
        with (
            patch.object(self.service.runtime, "model_catalog", return_value={"selected": {"supportsImages": False}}),
            patch.object(self.service.runtime, "prompt", return_value={"accepted": True, "turnId": "turn:text", "piEntryId": "entry:text", "response": {"success": True}}) as prompt,
        ):
            accepted = self.service.prompt(session_id, {"message": "按需核对需求", "attachments": [receipt["mediaId"]], "clientMessageId": "text-file-prompt"})
        self.assertEqual(prompt.call_args.kwargs["images"], [])
        self.assertIn(f"media://{receipt['mediaId']}", prompt.call_args.args[1])
        self.assertNotIn(body, prompt.call_args.args[1])
        self.assertEqual(visible_message_text("user", prompt.call_args.args[1]), "按需核对需求")
        self.assertEqual(accepted["attachments"][0]["mediaId"], receipt["mediaId"])
        self.assertEqual(self.service.read_media_resource(str(receipt["mediaId"]), session_id=session_id)[1].decode(), body)
        events, _ = self.service.events.replay(session_id)
        user = next(event for event in events if event.event_type == "message_completed")
        self.assertEqual(user.payload["message"]["blocks"][1]["type"], "file")
        self.assertEqual(user.payload["message"]["blocks"][1]["data"]["fileName"], "需求.txt")
        other = self.service.create_session({"title": "另一个 Session"})["session"]
        with self.assertRaises(KeyError):
            self.service.read_media_resource(str(receipt["mediaId"]), session_id=str(other["id"]))

    def test_managed_image_is_bound_to_prompt_and_deleted_with_session(self) -> None:
        session = self.service.create_session({"title": "图片对话"})["session"]
        session_id = str(session["id"])
        imported = self.service.import_media(
            session_id=session_id,
            data=PNG_1X1,
            mime_type="image/png",
            file_name="screen.png",
        )["media"]

        with (
            patch.object(
                self.service.runtime,
                "model_catalog",
                return_value={"selected": {"supportsImages": True}},
            ),
            patch.object(
                self.service.runtime,
                "prompt",
                return_value={
                    "accepted": True,
                    "turnId": "turn:image:1",
                    "piEntryId": "pi-entry:image:1",
                    "response": {"success": True},
                },
            ) as prompt,
        ):
            response = self.service.prompt(
                session_id,
                {
                    "message": "这张图里有什么",
                    "attachments": [imported["mediaId"]],
                    "clientMessageId": "web-image-1",
                },
            )

        images = prompt.call_args.kwargs["images"]
        self.assertEqual(images[0]["mimeType"], "image/png")
        self.assertEqual(base64.b64decode(images[0]["data"]), PNG_1X1)
        self.assertEqual(prompt.call_args.kwargs["client_message_id"], "web-image-1")
        self.assertEqual(response["attachments"][0]["mediaId"], imported["mediaId"])
        self.assertEqual(
            self.service.media.attachments_for_entry(
                session_id=session_id,
                pi_entry_id="pi-entry:image:1",
            )[0]["mediaId"],
            imported["mediaId"],
        )
        events, _ = self.service.events.replay(session_id)
        user_event = next(event for event in events if event.event_type == "message_completed")
        self.assertEqual(user_event.payload["clientMessageId"], "web-image-1")
        self.assertEqual(user_event.payload["message"]["id"], "pi-entry:image:1")
        self.assertEqual(user_event.payload["message"]["clientMessageId"], "web-image-1")
        image_block = user_event.payload["message"]["blocks"][1]
        self.assertEqual(image_block["type"], "image")
        self.assertIn("/api/agent/media/", image_block["data"]["receiptUrl"])

        deleted = self.service.delete_session(session_id)
        self.assertEqual(deleted["mediaFilesDeleted"], 1)
        self.assertEqual(list(self.service.media.root.glob("*.blob")), [])

    def test_prompt_reserves_runtime_admission_before_context_dispatch(
        self,
    ) -> None:
        session = self.service.create_session({"title": "early stop fence"})[
            "session"
        ]
        session_id = str(session["id"])
        timeline: list[str] = []

        real_reserve = self.service.runtime.reserve_prompt_admission
        real_release = self.service.runtime.release_prompt_admission

        def reserve(*_args, **_kwargs):
            timeline.append("reserve")
            return real_reserve(*_args, **_kwargs)

        def prompt(*_args, **_kwargs):
            timeline.append("prompt")
            return {
                "accepted": True,
                "turnId": "turn:early-stop-fence",
                "piEntryId": "pi-entry:early-stop-fence",
                "response": {"success": True},
            }

        def release(*_args, **_kwargs):
            timeline.append("release")
            return real_release(*_args, **_kwargs)

        with (
            patch.object(
                self.service.runtime,
                "reserve_prompt_admission",
                create=True,
                side_effect=reserve,
            ),
            patch.object(
                self.service.runtime,
                "prompt",
                side_effect=prompt,
            ),
            patch.object(
                self.service.runtime,
                "release_prompt_admission",
                create=True,
                side_effect=release,
            ),
        ):
            response = self.service.prompt(
                session_id,
                {
                    "message": "立即发送后停止",
                    "clientMessageId": "web-early-stop-fence",
                },
            )

        self.assertTrue(response["accepted"])
        self.assertEqual(timeline, ["reserve", "prompt", "release"])

    def test_rewrite_admission_preserves_uncertain_dispatch_and_releases_rejection(self) -> None:
        from rag_ime.agent_prompt_delivery import (
            AgentPromptAcceptanceUnknown, AgentPromptPostAcceptanceFailure,
        )
        for error_type, keep_admission in (
            (AgentPromptAcceptanceUnknown, True),
            (AgentPromptPostAcceptanceFailure, True),
            (ValueError, False),
        ):
            with self.subTest(error=error_type.__name__):
                session_id = str(self.service.create_session({"title": "rewrite admission"})["session"]["id"])
                with (
                    patch.object(self.service.prompt_application, "dispatch_checkpoint", side_effect=error_type("dispatch failed")),
                    self.assertRaises(error_type),
                ):
                    self.service.prompt_application.prompt_rewritten_session(
                        session_id=session_id, message="replacement", checkpoint_text="replacement",
                        attachment_ids=[], client_message_id="rewrite-admission", context_source="conversation_rewrite",
                    )
                if keep_admission:
                    self.service.runtime.require_prompt_admission_active(session_id, client_message_id="rewrite-admission")
                else:
                    with self.assertRaises(PiRuntimeCommandRejected):
                        self.service.runtime.require_prompt_admission_active(session_id, client_message_id="rewrite-admission")
                self.service.runtime.release_prompt_admission(session_id, client_message_id="rewrite-admission")

    def test_stop_during_memory_bootstrap_fences_prompt_before_runtime(
        self,
    ) -> None:
        session = self.service.create_session(
            {"title": "stop during memory bootstrap"}
        )["session"]
        session_id = str(session["id"])
        client_message_id = "web-stop-memory-bootstrap"
        bootstrap_entered = Event()
        release_bootstrap = Event()
        cancelled = Event()
        prompt_result: dict[str, object] = {}
        prompt_errors: list[BaseException] = []

        def reserve(*_args, **_kwargs):
            return {"reserved": True}

        def require_active(*_args, **_kwargs):
            if cancelled.is_set():
                raise PiRuntimeCommandRejected(
                    "当前消息已停止，未发送给 Pi",
                    host_error_code="PROMPT_ADMISSION_CANCELLED",
                )

        def bootstrap(*_args, **_kwargs):
            bootstrap_entered.set()
            self.assertTrue(release_bootstrap.wait(2.0))
            return {}

        def abort(*_args, **_kwargs):
            cancelled.set()
            self.service.sessions.set_status(
                session_id,
                "idle",
                last_message_preview="已停止。",
            )
            return {
                "schemaVersion": "rag-ime.pi-session-abort-receipt.v1",
                "sessionId": session_id,
                "turnId": "",
                "pendingAdmission": True,
                "admissionCancelled": True,
                "cancelledDecisionIds": [],
                "cancelledUIRequestIds": [],
                "lifecycle": {
                    "schemaVersion": "pi.agent-abort-receipt.v1",
                    "scopeId": session_id,
                    "generation": 0,
                    "reason": "user_abort",
                    "cancelledContinuationIds": [],
                    "cancelledOperationIds": [],
                    "failedOperationIds": [],
                    "operations": [],
                    "pendingOperations": [],
                    "drained": True,
                    "idle": True,
                },
            }

        def run_prompt() -> None:
            try:
                prompt_result.update(
                    self.service.prompt(
                        session_id,
                        {
                            "message": "停止时不能迟到发送",
                            "clientMessageId": client_message_id,
                        },
                    )
                )
            except BaseException as exc:  # pragma: no cover - assertion below
                prompt_errors.append(exc)

        with (
            patch.object(
                self.service.runtime,
                "reserve_prompt_admission",
                create=True,
                side_effect=reserve,
            ),
            patch.object(
                self.service.runtime,
                "require_prompt_admission_active",
                create=True,
                side_effect=require_active,
            ),
            patch.object(
                self.service.runtime,
                "release_prompt_admission",
                create=True,
                return_value=True,
            ),
            patch.object(
                self.service.runtime,
                "abort",
                side_effect=abort,
            ),
            patch.object(
                self.service.memory_context_application,
                "ensure_bootstrap",
                side_effect=bootstrap,
            ),
            patch.object(self.service.runtime, "prompt") as runtime_prompt,
        ):
            prompt_thread = Thread(target=run_prompt)
            prompt_thread.start()
            self.assertTrue(bootstrap_entered.wait(1.0))
            started = time.monotonic()
            abort_receipt = self.service.abort(session_id)
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertTrue(
                abort_receipt["runtimeReceipt"]["admissionCancelled"]
            )
            self.assertEqual(
                self.service.sessions.get(session_id)["status"],
                "idle",
            )
            release_bootstrap.set()
            prompt_thread.join(timeout=2.0)

        self.assertFalse(prompt_thread.is_alive())
        self.assertEqual(prompt_errors, [])
        runtime_prompt.assert_not_called()
        self.assertFalse(prompt_result["accepted"])
        self.assertTrue(prompt_result["cancelled"])
        self.assertTrue(prompt_result["admissionCancelled"])
        replay = self.service.prompt(
            session_id,
            {
                "message": "停止时不能迟到发送",
                "clientMessageId": client_message_id,
            },
        )
        self.assertTrue(replay["idempotentReplay"])
        self.assertTrue(replay["cancelled"])

    def test_room_prompt_checkpoint_preserves_room_media_ownership(
        self,
    ) -> None:
        room = self.service.create_room(
            {
                "title": "Room 图片检查点",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {
                        "roleId": "companion-firstlight-v1",
                        "roleVersion": "1",
                    },
                    {
                        "roleId": "companion-future-v1",
                        "roleVersion": "1",
                    },
                ],
            }
        )["room"]
        room_id = str(room["id"])
        target_session_id = str(
            room["participants"][0]["sessionId"]
        )
        imported = self.service.import_media(
            room_id=room_id,
            data=PNG_1X1,
            mime_type="image/png",
            file_name="room.png",
        )["media"]
        payload = {
            "message": "检查 Room 图片",
            "attachments": [imported["mediaId"]],
            "clientMessageId": "room-image-checkpoint-1",
            "_contextSourceToken": (
                self.service._context_source_token
            ),
            "_contextSource": "room",
            "_checkpointText": "检查 Room 图片",
            "_mediaOwnerRoomId": room_id,
        }

        with (
            patch.object(
                self.service.runtime,
                "model_catalog",
                return_value={
                    "selected": {"supportsImages": True}
                },
            ),
            patch.object(
                self.service.runtime,
                "prompt",
                return_value={
                    "accepted": True,
                    "turnId": "turn:room-image:1",
                    "piEntryId": "pi-entry:room-image:1",
                },
            ) as runtime_prompt,
            patch.object(
                self.service.media,
                "bind_to_pi_entry",
            ) as bind_to_session_entry,
        ):
            accepted = self.service.prompt(
                target_session_id,
                payload,
            )
            replay = self.service.prompt(
                target_session_id,
                payload,
            )

        delivered = runtime_prompt.call_args.kwargs["images"]
        self.assertEqual(
            base64.b64decode(delivered[0]["data"]),
            PNG_1X1,
        )
        self.assertEqual(
            accepted["attachments"][0]["roomId"],
            room_id,
        )
        self.assertNotIn(
            "sessionId",
            accepted["attachments"][0],
        )
        self.assertTrue(replay["idempotentReplay"])
        runtime_prompt.assert_called_once()
        bind_to_session_entry.assert_not_called()

    def test_room_media_checkpoint_rejects_owner_bypass(
        self,
    ) -> None:
        room = self.service.create_room(
            {
                "title": "Room 图片所有权",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {
                        "roleId": "companion-firstlight-v1",
                        "roleVersion": "1",
                    },
                    {
                        "roleId": "companion-future-v1",
                        "roleVersion": "1",
                    },
                ],
            }
        )["room"]
        room_id = str(room["id"])
        imported = self.service.import_media(
            room_id=room_id,
            data=PNG_1X1,
            mime_type="image/png",
            file_name="private-room.png",
        )["media"]
        unrelated = self.service.create_session(
            {"title": "非 Room 成员"}
        )["session"]
        unrelated_session_id = str(unrelated["id"])
        payload = {
            "message": "不应读取 Room 图片",
            "attachments": [imported["mediaId"]],
            "_contextSourceToken": (
                self.service._context_source_token
            ),
            "_contextSource": "room",
            "_mediaOwnerRoomId": room_id,
        }

        with (
            patch.object(
                self.service.runtime,
                "model_catalog",
                return_value={
                    "selected": {"supportsImages": True}
                },
            ),
            patch.object(
                self.service.runtime,
                "prompt",
            ) as runtime_prompt,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "active participant",
            ):
                self.service.prompt(
                    unrelated_session_id,
                    payload,
                )
            with self.assertRaisesRegex(
                KeyError,
                "not found for this session",
            ):
                self.service.prompt(
                    unrelated_session_id,
                    {
                        key: value
                        for key, value in payload.items()
                        if key != "_contextSourceToken"
                    },
                )
        runtime_prompt.assert_not_called()

    def test_prompt_client_message_id_is_durable_and_idempotent(self) -> None:
        session = self.service.create_session({"title": "多端幂等"})["session"]
        session_id = str(session["id"])
        payload = {"message": "只执行一次", "clientMessageId": "device-command-1"}
        with patch.object(
            self.service.runtime,
            "prompt",
            return_value={
                "accepted": True,
                "turnId": "turn:idempotent:1",
                "piEntryId": "pi-entry:idempotent:1",
                "response": {"success": True},
            },
        ) as prompt:
            first = self.service.prompt(session_id, payload)

        prompt.assert_called_once()
        completed, _ = self.service.events.replay(session_id)
        self.assertEqual(
            len(
                [
                    event
                    for event in completed
                    if event.event_type == "message_completed"
                    and event.payload.get("clientMessageId") == "device-command-1"
                ]
            ),
            1,
        )
        self.service.close()
        self.service = AgentService(
            db_path=self.root / "rag-ime.sqlite",
            runtime_config=PiRuntimeConfig(
                enabled=False,
                executable=None,
                agent_dir=self.root / "agent-config",
                session_dir=self.root / "sessions",
                logs_dir=self.root / "logs",
            ),
            process_id_provider=lambda: self.process_id,
        )
        with patch.object(self.service.runtime, "prompt") as replay_prompt:
            replay = self.service.prompt(session_id, payload)
        replay_prompt.assert_not_called()
        self.assertNotIn("idempotentReplay", first)
        self.assertEqual(
            first["commandReceipt"],
            {
                "state": "accepted",
                "clientMessageId": "device-command-1",
            },
        )
        self.assertTrue(replay["idempotentReplay"])
        self.assertEqual(
            replay["commandReceipt"],
            first["commandReceipt"],
        )
        self.assertEqual(replay["turnId"], first["turnId"])
        with self.assertRaisesRegex(ValueError, "different command"):
            self.service.prompt(
                session_id,
                {
                    "message": "不能复用标识",
                    "clientMessageId": "device-command-1",
                },
            )
        with self.assertRaisesRegex(ValueError, "different command"):
            self.service.prompt(
                session_id,
                {
                    "message": "只执行一次",
                    "delivery": "steer",
                    "clientMessageId": "device-command-1",
                },
            )

    def test_prompt_reopens_from_durable_receipt_without_reexecution(
        self,
    ) -> None:
        session = self.service.create_session(
            {"title": "接受后崩溃恢复"}
        )["session"]
        session_id = str(session["id"])
        payload = {
            "message": "Pi 只应接受一次",
            "clientMessageId": "device-crash-after-accept",
        }
        with (
            patch.object(
                self.service.runtime,
                "prompt",
                return_value={
                    "accepted": True,
                    "turnId": "turn:durable-acceptance:1",
                    "piEntryId": "pi-entry:durable-acceptance:1",
                    "response": {"success": True},
                },
            ) as original_prompt,
            patch.object(
                self.service.command_receipts,
                "complete",
                side_effect=RuntimeError(
                    "simulated process exit before receipt completion"
                ),
            ),
        ):
            recovered_first = self.service.prompt(
                session_id,
                payload,
            )
        original_prompt.assert_called_once()
        self.assertTrue(
            recovered_first["recoveredFromDurableReceipt"]
        )
        self.assertEqual(
            recovered_first["projectionState"],
            "partial",
        )

        self.service.close()
        self.service = AgentService(
            db_path=self.root / "rag-ime.sqlite",
            runtime_config=PiRuntimeConfig(
                enabled=False,
                executable=None,
                agent_dir=self.root / "agent-config",
                session_dir=self.root / "sessions",
                logs_dir=self.root / "logs",
            ),
            process_id_provider=lambda: self.process_id,
        )
        with patch.object(
            self.service.runtime,
            "prompt",
        ) as replay_prompt:
            recovered = self.service.prompt(
                session_id,
                payload,
            )
            replayed_again = self.service.prompt(
                session_id,
                payload,
            )

        replay_prompt.assert_not_called()
        self.assertTrue(recovered["idempotentReplay"])
        self.assertTrue(
            recovered["recoveredFromDurableReceipt"]
        )
        self.assertEqual(
            recovered["turnId"],
            "turn:durable-acceptance:1",
        )
        self.assertEqual(
            recovered["piEntryId"],
            "pi-entry:durable-acceptance:1",
        )
        self.assertEqual(
            recovered["commandReceipt"],
            {
                "state": "accepted",
                "clientMessageId": (
                    "device-crash-after-accept"
                ),
                "recoveredFromDurableEvidence": True,
                "recoveredFromDurableReceipt": True,
                "projectionState": "partial",
            },
        )
        self.assertTrue(replayed_again["idempotentReplay"])
        self.assertEqual(
            replayed_again["commandReceipt"],
            recovered["commandReceipt"],
        )
        self.assertTrue(
            self.service.prompt(
                session_id,
                payload,
            )["idempotentReplay"]
        )

    def test_media_projection_failure_recovers_without_reexecution(
        self,
    ) -> None:
        session = self.service.create_session(
            {"title": "媒体投影失败"}
        )["session"]
        session_id = str(session["id"])
        media = self.service.import_media(
            session_id=session_id,
            data=PNG_1X1,
            mime_type="image/png",
            file_name="accepted.png",
        )["media"]
        payload = {
            "message": "已接受的图片",
            "attachments": [media["mediaId"]],
            "clientMessageId": "media-bind-fault",
        }
        accepted = {
            "accepted": True,
            "turnId": "turn:media-fault",
            "piEntryId": "pi:media-fault",
        }
        with (
            patch.object(
                self.service.runtime,
                "model_catalog",
                return_value={
                    "selected": {"supportsImages": True}
                },
            ),
            patch.object(
                self.service.runtime,
                "prompt",
                return_value=accepted,
            ) as runtime_prompt,
            patch.object(
                self.service.media,
                "bind_to_pi_entry",
                side_effect=RuntimeError("media bind failed"),
            ),
        ):
            recovered = self.service.prompt(
                session_id,
                payload,
            )
        runtime_prompt.assert_called_once()
        self.assertEqual(recovered["projectionState"], "partial")
        self.assertTrue(
            recovered["recoveredFromDurableReceipt"]
        )
        with patch.object(
            self.service.runtime,
            "prompt",
        ) as replay_prompt:
            replay = self.service.prompt(session_id, payload)
        replay_prompt.assert_not_called()
        self.assertTrue(replay["idempotentReplay"])

    def test_event_persistence_failure_recovers_from_receipt_evidence(
        self,
    ) -> None:
        session = self.service.create_session(
            {"title": "事件持久化失败"}
        )["session"]
        session_id = str(session["id"])
        payload = {
            "message": "事件失败也不能重发",
            "clientMessageId": "event-persistence-fault",
        }
        original_recorder = self.service.events._event_recorder

        def fail_user_message(event: AgentEventEnvelope) -> None:
            if event.event_type == "message_completed":
                raise RuntimeError("event persistence failed")
            if original_recorder is not None:
                original_recorder(event)

        with (
            patch.object(
                self.service.runtime,
                "prompt",
                return_value={
                    "accepted": True,
                    "turnId": "turn:event-fault",
                    "piEntryId": "pi:event-fault",
                },
            ) as runtime_prompt,
            patch.object(
                self.service.events,
                "_event_recorder",
                side_effect=fail_user_message,
            ),
        ):
            recovered = self.service.prompt(
                session_id,
                payload,
            )
            self.assertTrue(self.service.events.flush())
        runtime_prompt.assert_called_once()
        self.assertTrue(recovered["accepted"])
        self.assertEqual(recovered["commandReceipt"]["state"], "accepted")
        with patch.object(self.service.runtime, "prompt") as replay_prompt:
            replay = self.service.prompt(session_id, payload)
        replay_prompt.assert_not_called()
        self.assertTrue(replay["idempotentReplay"])

    def test_remote_accept_before_local_evidence_is_unresolved(
        self,
    ) -> None:
        session = self.service.create_session(
            {"title": "接纳证据窗口"}
        )["session"]
        session_id = str(session["id"])
        payload = {
            "message": "远端可能已经接受",
            "clientMessageId": "acceptance-ledger-gap",
        }
        with (
            patch.object(
                self.service.runtime,
                "prompt",
                return_value={
                    "accepted": True,
                    "turnId": "turn:ledger-gap",
                    "piEntryId": "pi:ledger-gap",
                },
            ) as runtime_prompt,
            patch.object(
                self.service.command_receipts,
                "record_acceptance_evidence",
                side_effect=RuntimeError(
                    "process died before local durable evidence"
                ),
            ),
        ):
            with self.assertRaises(
                AgentCommandReceiptPending
            ) as pending:
                self.service.prompt(session_id, payload)
        runtime_prompt.assert_called_once()
        self.assertEqual(
            pending.exception.recovery_state,
            "in_flight",
        )
        self.service.command_receipts.pending_recovery_grace_ms = 0
        with patch.object(
            self.service.runtime,
            "prompt",
        ) as replay_prompt:
            with self.assertRaises(
                AgentCommandReceiptPending
            ) as unresolved:
                self.service.prompt(session_id, payload)
        replay_prompt.assert_not_called()
        self.assertEqual(
            unresolved.exception.recovery_state,
            "unresolved",
        )

    def test_lost_runtime_ack_recovers_from_correlated_event_without_reexecution(
        self,
    ) -> None:
        session = self.service.create_session(
            {"title": "ACK 丢失恢复"}
        )["session"]
        session_id = str(session["id"])
        client_message_id = "client:lost-ack:event-proof"
        payload = {
            "message": "这条请求只能执行一次",
            "clientMessageId": client_message_id,
        }

        def accept_then_lose_ack(
            _session_id: str,
            _message: str,
            **_kwargs: object,
        ) -> dict[str, object]:
            turn_id = "turn:lost-ack:event-proof"
            self.service.events.publish(
                session_id,
                "message_completed",
                {
                    "clientMessageId": client_message_id,
                    "message": {
                        "schemaVersion": "rag-ime.agent-message.v1",
                        "id": "message:lost-ack:user",
                        "sessionId": session_id,
                        "turnId": turn_id,
                        "role": "user",
                        "status": "completed",
                        "blocks": [{
                            "id": "text:lost-ack:user",
                            "type": "text",
                            "status": "completed",
                            "presentationKind": "plain",
                            "data": {"text": "这条请求只能执行一次"},
                        }],
                        "attachments": [],
                        "citations": [],
                        "createdAtMs": 10,
                        "completedAtMs": 10,
                        "clientMessageId": client_message_id,
                    },
                },
                turn_id=turn_id,
            )
            self.assertTrue(self.service.events.flush())
            raise PiRuntimeCommandAcceptanceUnknown(
                "host accepted; acknowledgement was lost"
            )

        with patch.object(
            self.service.runtime,
            "prompt",
            side_effect=accept_then_lose_ack,
        ) as runtime_prompt:
            recovered = self.service.prompt(session_id, payload)

        runtime_prompt.assert_called_once()
        self.assertTrue(recovered["accepted"])
        self.assertEqual(recovered["turnId"], "turn:lost-ack:event-proof")
        self.assertTrue(recovered["recoveredFromDurableEvent"])
        self.assertEqual(
            recovered["commandReceipt"],
            {
                "state": "accepted",
                "clientMessageId": client_message_id,
                "recoveredFromDurableEvidence": True,
                "projectionState": "partial",
                "recoveredFromDurableEvent": True,
            },
        )

        with patch.object(self.service.runtime, "prompt") as replay_prompt:
            replay = self.service.prompt(session_id, payload)
        replay_prompt.assert_not_called()
        self.assertTrue(replay["idempotentReplay"])
        self.assertEqual(replay["turnId"], recovered["turnId"])
        events, _ = self.service.events.replay(session_id)
        self.assertFalse(any(
            event.event_type == "turn_failed" and not event.turn_id
            for event in events
        ))

    def test_prompt_retry_requires_a_failed_equivalent_predecessor(
        self,
    ) -> None:
        session = self.service.create_session(
            {"title": "终态重试关联"}
        )["session"]
        session_id = str(session["id"])
        first_payload = {
            "message": "重新执行同一输入",
            "clientMessageId": "retry-attempt-1",
        }
        with patch.object(
            self.service.runtime,
            "prompt",
            side_effect=PiRuntimeTurnConflict(
                "source-proven pre-accept conflict"
            ),
        ):
            with self.assertRaises(AgentCommandReceiptFailed):
                self.service.prompt(session_id, first_payload)

        payload = {
            **first_payload,
            "clientMessageId": "retry-attempt-2",
            "retryOfClientMessageId": "retry-attempt-1",
        }
        with patch.object(
            self.service.runtime,
            "prompt",
            return_value={
                "accepted": True,
                "turnId": "turn:retry:2",
                "piEntryId": "pi-entry:retry:2",
                "response": {"success": True},
            },
        ):
            accepted = self.service.prompt(session_id, payload)

        self.assertEqual(
            accepted["commandReceipt"],
            {
                "state": "accepted",
                "clientMessageId": "retry-attempt-2",
                "retryOfClientMessageId": "retry-attempt-1",
            },
        )
        events, _ = self.service.events.replay(session_id)
        user_event = next(
            event
            for event in events
            if event.event_type == "message_completed"
        )
        self.assertEqual(
            user_event.payload["message"][
                "retryOfClientMessageId"
            ],
            "retry-attempt-1",
        )
        replay = self.service.prompt(session_id, payload)
        self.assertTrue(replay["idempotentReplay"])
        self.assertEqual(
            replay["commandReceipt"],
            accepted["commandReceipt"],
        )

    def test_prompt_failure_returns_a_typed_durable_receipt(
        self,
    ) -> None:
        session = self.service.create_session(
            {"title": "失败回执"}
        )["session"]
        session_id = str(session["id"])
        payload = {
            "message": "与活动回合冲突",
            "clientMessageId": "failed-command-1",
        }
        with patch.object(
            self.service.runtime,
            "prompt",
            side_effect=PiRuntimeTurnConflict(
                "Pi 正在处理上一轮，请等待结束或停止完成后再发送"
            ),
        ):
            with self.assertRaises(
                AgentCommandReceiptFailed
            ) as failed:
                self.service.prompt(session_id, payload)

        self.assertEqual(
            failed.exception.response_payload(),
            {
                "code": "AGENT_COMMAND_FAILED",
                "commandReceipt": {
                    "state": "failed",
                    "clientMessageId": "failed-command-1",
                    "causeCode": "AGENT_TURN_CONFLICT",
                },
            },
        )
        with self.assertRaises(
            AgentCommandReceiptFailed
        ) as replay_failed:
            self.service.prompt(session_id, payload)
        self.assertEqual(
            replay_failed.exception.response_payload(),
            failed.exception.response_payload(),
        )

    def test_host_rejected_steer_closes_receipt_instead_of_pending(self) -> None:
        session = self.service.create_session(
            {"title": "宿主明确拒绝 Steer"}
        )["session"]
        session_id = str(session["id"])
        payload = {
            "message": "停止原计划，只回复新结论",
            "delivery": "steer",
            "clientMessageId": "rejected-steer-1",
        }
        with patch.object(
            self.service.runtime,
            "prompt",
            side_effect=PiRuntimeCommandRejected(
                "Session has no active turn to receive a queued message",
                host_error_code="SESSION_IDLE",
            ),
        ):
            with self.assertRaises(
                AgentCommandReceiptFailed
            ) as failed:
                self.service.prompt(session_id, payload)

        self.assertEqual(
            failed.exception.response_payload(),
            {
                "code": "AGENT_COMMAND_FAILED",
                "commandReceipt": {
                    "state": "failed",
                    "clientMessageId": "rejected-steer-1",
                    "causeCode": "SESSION_IDLE",
                },
            },
        )

    def test_text_only_pi_model_rejects_managed_image_before_prompt(self) -> None:
        session = self.service.create_session({"title": "文本模型"})["session"]
        session_id = str(session["id"])
        imported = self.service.import_media(
            session_id=session_id,
            data=PNG_1X1,
            mime_type="image/png",
            file_name="screen.png",
        )["media"]

        with (
            patch.object(
                self.service.runtime,
                "model_catalog",
                return_value={"selected": {"supportsImages": False}},
            ),
            patch.object(self.service.runtime, "prompt") as prompt,
        ):
            with self.assertRaisesRegex(ValueError, "当前模型不支持图片"):
                self.service.prompt(
                    session_id,
                    {"message": "看看图片", "attachments": [imported["mediaId"]]},
                )
        prompt.assert_not_called()

    def test_steer_and_follow_up_delivery_reuse_the_active_turn_and_are_visible(self) -> None:
        session = self.service.create_session({"title": "排队消息"})["session"]
        session_id = str(session["id"])
        for delivery in ("steer", "followUp"):
            client_message_id = f"web-{delivery}"
            with patch.object(
                self.service.runtime,
                "prompt",
                return_value={
                    "accepted": True,
                    "queued": True,
                    "delivery": delivery,
                    "turnId": "turn:active:1",
                    "piEntryId": f"queue:{client_message_id}",
                    "response": {"success": True},
                },
            ) as prompt:
                result = self.service.prompt(
                    session_id,
                    {
                        "message": f"{delivery} message",
                        "delivery": delivery,
                        "clientMessageId": client_message_id,
                    },
                )

            self.assertEqual(prompt.call_args.kwargs["delivery"], delivery)
            self.assertEqual(result["turnId"], "turn:active:1")

        events, _ = self.service.events.replay(session_id)
        user_messages = [
            event.payload["message"] for event in events
            if event.event_type == "message_completed"
        ]
        self.assertEqual(
            [message["blocks"][0]["data"]["delivery"] for message in user_messages],
            ["steer", "followUp"],
        )
        self.assertEqual({message["turnId"] for message in user_messages}, {"turn:active:1"})
        with self.assertRaisesRegex(ValueError, "delivery"):
            self.service.prompt(session_id, {"message": "bad", "delivery": "later"})

    def test_steer_can_join_while_initial_prompt_is_still_being_admitted(self) -> None:
        session = self.service.create_session(
            {"title": "首轮接纳期间立即 Steer"}
        )["session"]
        session_id = str(session["id"])
        initial_entered = Event()
        release_initial = Event()
        initial_result: dict[str, object] = {}
        initial_error: list[BaseException] = []

        def runtime_prompt(
            _session_id: str,
            _message: str,
            **kwargs: object,
        ) -> dict[str, object]:
            delivery = str(kwargs.get("delivery") or "prompt")
            if delivery == "prompt":
                initial_entered.set()
                if not release_initial.wait(timeout=3):
                    raise TimeoutError("test did not release initial prompt")
                return {
                    "accepted": True,
                    "turnId": "turn:admission:1",
                    "piEntryId": "pi-entry:initial",
                    "response": {"success": True},
                }
            return {
                "accepted": True,
                "queued": True,
                "delivery": delivery,
                "turnId": "turn:admission:1",
                "piEntryId": "pi-entry:steer",
                "response": {"success": True},
            }

        def send_initial() -> None:
            try:
                initial_result.update(
                    self.service.prompt(
                        session_id,
                        {
                            "message": "先执行原计划",
                            "clientMessageId": "admission-initial",
                        },
                    )
                )
            except BaseException as exc:  # pragma: no cover - assertion path
                initial_error.append(exc)

        with patch.object(
            self.service.runtime,
            "prompt",
            side_effect=runtime_prompt,
        ):
            initial_thread = Thread(target=send_initial)
            initial_thread.start()
            self.assertTrue(initial_entered.wait(timeout=3))
            steer = self.service.prompt(
                session_id,
                {
                    "message": "立即改为新计划",
                    "delivery": "steer",
                    "clientMessageId": "admission-steer",
                },
            )
            release_initial.set()
            initial_thread.join(timeout=3)

        self.assertFalse(initial_thread.is_alive())
        self.assertEqual(initial_error, [])
        self.assertEqual(
            initial_result.get("turnId"),
            "turn:admission:1",
        )
        self.assertEqual(steer["turnId"], "turn:admission:1")
        self.assertEqual(steer["delivery"], "steer")

    def test_persisted_runtime_toggle_is_used_unless_development_env_overrides_it(self) -> None:
        settings = {
            "agent": {
                "pi": {
                    "enabled": True,
                    "idleTimeoutSeconds": 321,
                    "systemProxy": False,
                }
            }
        }
        with patch.dict("os.environ", {}, clear=True), patch(
            "rag_ime.pi.config.getproxies",
            return_value={"https": "http://127.0.0.1:7897"},
        ):
            configured = pi_runtime_config_from_settings(settings)
        self.assertTrue(configured.enabled)
        self.assertEqual(configured.idle_timeout_seconds, 321)
        self.assertNotIn("HTTPS_PROXY", configured.provider_environment)

        with patch.dict(
            "os.environ",
            {
                "RAG_IME_PI_ENABLED": "false",
                "RAG_IME_PI_IDLE_TIMEOUT_SECONDS": "12",
                "RAG_IME_PI_SYSTEM_PROXY": "true",
            },
            clear=True,
        ), patch(
            "rag_ime.pi.config.getproxies",
            return_value={"https": "http://127.0.0.1:7897"},
        ):
            overridden = pi_runtime_config_from_settings(settings)
        self.assertFalse(overridden.enabled)
        self.assertEqual(overridden.idle_timeout_seconds, 12)
        self.assertEqual(
            overridden.provider_environment["HTTPS_PROXY"],
            "http://127.0.0.1:7897",
        )

    def test_debug_text_opt_in_persists_bounded_agent_context_snapshots(self) -> None:
        settings = {
            "agent": {"pi": {"enabled": True}},
            "privacy": {
                "debugIncludeText": True,
                "debugContextDirectory": str(self.root / "external-context"),
                "debugContextMaxGiB": 5,
                "debugContextMaxCallsPerTurn": 128,
            },
        }
        with patch.dict(
            "os.environ",
            {"RAG_IME_APP_SUPPORT_DIR": str(self.root / "support")},
            clear=True,
        ):
            configured = pi_runtime_config_from_settings(settings)
            default_location = pi_runtime_config_from_settings(
                {"privacy": {"debugIncludeText": True}}
            )

        self.assertEqual(
            configured.debug_context_dir,
            self.root / "external-context",
        )
        self.assertEqual(
            configured.debug_context_max_bytes,
            5 * 1024 * 1024 * 1024,
        )
        self.assertEqual(configured.debug_context_max_calls, 128)
        self.assertEqual(
            default_location.debug_context_dir,
            self.root / "support" / "Agent" / "debug-context",
        )
        self.assertEqual(
            default_location.debug_context_max_bytes,
            5 * 1024 * 1024 * 1024,
        )

        with patch.dict(
            "os.environ",
            {
                "RAG_IME_APP_SUPPORT_DIR": str(self.root / "support"),
                "RAG_IME_PI_DEBUG_CONTEXT_DIR": str(self.root / "explicit"),
                "RAG_IME_PI_DEBUG_CONTEXT_MAX_BYTES": "4096",
            },
            clear=True,
        ):
            explicit = pi_runtime_config_from_settings(
                {"privacy": {"debugIncludeText": False}}
            )
        self.assertEqual(explicit.debug_context_dir, self.root / "explicit")
        self.assertEqual(explicit.debug_context_max_bytes, 4096)
        self.assertEqual(explicit.debug_context_max_calls, 128)

    def test_approval_api_lists_and_rejects_but_cannot_fake_an_approval(self) -> None:
        session = self.service.create_session({"title": "审批"})["session"]
        approval = self.service.sessions.create_approval(
            session_id=str(session["id"]),
            tool_name="input",
            operation="apply_settings",
            payload_sha256="a" * 64,
            preview={"summary": "关闭模糊音"},
            risk_level="R1",
        )
        listed = self.service.list_approvals({"sessionId": session["id"]})
        self.assertEqual(listed["items"][0]["approvalId"], approval["approvalId"])

        rejected = self.service.decide_approval(
            str(approval["approvalId"]),
            {"decision": "reject", "payloadSha256": "a" * 64},
        )
        self.assertEqual(rejected["approval"]["state"], "rejected")

        forged = self.service.sessions.create_approval(
            session_id=str(session["id"]),
            tool_name="runtime",
            operation="restart",
            payload_sha256="b" * 64,
            preview={"summary": "重启"},
            risk_level="R2",
        )
        with self.assertRaisesRegex(ValueError, "no longer active in Pi"):
            self.service.decide_approval(
                str(forged["approvalId"]),
                {"decision": "approve", "payloadSha256": "b" * 64},
            )
        self.assertEqual(
            self.service.sessions.get_approval(str(forged["approvalId"]))["state"],
            "pending",
        )

    def test_approval_api_lists_pending_work_across_sessions_for_the_center(self) -> None:
        first = self.service.create_session({"title": "审批一"})["session"]
        second = self.service.create_session({"title": "审批二"})["session"]
        first_approval = self.service.sessions.create_approval(
            session_id=str(first["id"]),
            tool_name="input",
            operation="apply_settings",
            payload_sha256="1" * 64,
            preview={"summary": "第一项"},
            risk_level="R1",
        )
        second_approval = self.service.sessions.create_approval(
            session_id=str(second["id"]),
            tool_name="runtime",
            operation="restart",
            payload_sha256="2" * 64,
            preview={"summary": "第二项"},
            risk_level="R2",
        )

        listed = self.service.list_approvals({})

        self.assertEqual(listed["sessionId"], "")
        self.assertEqual(
            {item["approvalId"] for item in listed["items"]},
            {first_approval["approvalId"], second_approval["approvalId"]},
        )

    def test_expired_or_stale_approval_closes_the_pending_pi_request(self) -> None:
        session = self.service.create_session({"title": "审批终态"})["session"]
        session_id = str(session["id"])
        expired = self.service.sessions.create_approval(
            session_id=session_id,
            tool_name="input",
            operation="apply_settings",
            payload_sha256="e" * 64,
            preview={"summary": "过期变更"},
            risk_level="R1",
            requested_at_ms=100,
            ttl_ms=1_000,
        )
        with (
            patch.object(self.service.runtime, "has_pending_approval", return_value=True),
            patch.object(self.service.runtime, "resolve_approval") as resolve_expired,
        ):
            result = self.service.decide_approval(
                str(expired["approvalId"]),
                {"decision": "approve", "payloadSha256": "e" * 64},
            )
        self.assertEqual(result["approval"]["state"], "expired")
        resolve_expired.assert_called_once_with(
            session_id,
            str(expired["approvalId"]),
            approved=False,
            resolution_state="expired",
        )

        stale = self.service.sessions.create_approval(
            session_id=session_id,
            tool_name="input",
            operation="apply_settings",
            payload_sha256="a" * 64,
            preview={"summary": "哈希已变化"},
            risk_level="R1",
        )
        with (
            patch.object(self.service.runtime, "has_pending_approval", return_value=True),
            patch.object(self.service.runtime, "resolve_approval") as resolve_stale,
        ):
            result = self.service.decide_approval(
                str(stale["approvalId"]),
                {"decision": "approve", "payloadSha256": "b" * 64},
            )
        self.assertEqual(result["approval"]["state"], "stale")
        resolve_stale.assert_called_once_with(
            session_id,
            str(stale["approvalId"]),
            approved=False,
            resolution_state="stale",
        )

    def test_approval_list_reconciles_an_expired_pending_pi_request(self) -> None:
        session = self.service.create_session(
            {"title": "审批轮询恢复"}
        )["session"]
        session_id = str(session["id"])
        approval = self.service.sessions.create_approval(
            session_id=session_id,
            tool_name="workspace_shell",
            operation="run",
            payload_sha256="f" * 64,
            preview={"summary": "已过期的测试命令"},
            risk_level="R2",
            requested_at_ms=100,
            ttl_ms=1_000,
        )
        approval = self.service.sessions.bind_approval_tool_call(
            str(approval["approvalId"]),
            tool_call_id="tool:expired-list-recovery",
        )

        with (
            patch.object(
                self.service.runtime,
                "has_pending_approval",
                return_value=True,
            ),
            patch.object(
                self.service.runtime,
                "resolve_approval",
            ) as resolve_approval,
        ):
            result = self.service.list_approvals({"sessionId": session_id})

        self.assertEqual(result["items"][0]["state"], "expired")
        resolve_approval.assert_called_once_with(
            session_id,
            str(approval["approvalId"]),
            approved=False,
            resolution_state="expired",
        )

    def test_approved_operation_executes_before_pi_is_released_and_returns_receipt(self) -> None:
        session = self.service.create_session({"title": "任务审批"})["session"]
        approval = self.service.sessions.create_approval(
            session_id=str(session["id"]),
            tool_name="planning",
            operation="task_action",
            payload_sha256="c" * 64,
            preview={"summary": "完成接入 Pi"},
            risk_level="R1",
        )
        calls: list[str] = []

        def execute(value):
            calls.append("execute")
            self.assertEqual(value["state"], "approved")
            return {
                "schemaVersion": "rag-ime.agent-operation-receipt.v1",
                "mutationApplied": True,
                "summary": "已完成接入 Pi",
                "auditId": 42,
                "undoAvailable": True,
            }

        self.service.bind_approval_executor(execute)
        with (
            patch.object(self.service.runtime, "has_pending_approval", return_value=True),
            patch.object(self.service.runtime, "resolve_approval") as resolve,
        ):
            result = self.service.decide_approval(
                str(approval["approvalId"]),
                {"decision": "approve", "payloadSha256": "c" * 64},
            )

        self.assertEqual(calls, ["execute"])
        self.assertEqual(result["approval"]["state"], "applied")
        self.assertTrue(result["approval"]["receipt"]["mutationApplied"])
        self.assertTrue(result["runtimeNotified"])
        resolve.assert_called_once_with(
            str(session["id"]),
            str(approval["approvalId"]),
            approved=True,
            resolution_state="applied",
        )
        lookup = self.service.approval_result(
            {"sessionId": session["id"], "approvalId": approval["approvalId"]}
        )
        self.assertEqual(lookup["approval"]["receipt"]["auditId"], 42)

    def test_governed_memory_approval_recovers_from_committed_store_journal(self) -> None:
        session = self.service.create_session({"title": "记忆审批崩溃恢复"})["session"]
        self.service.sessions.set_disclosure_preferences(str(session["id"]), {"tool:memory": "enabled"})
        evidence = self.service.memory_evidence.record_user_message(
            session_id=str(session["id"]),
            pi_entry_id="message:memory-recovery",
            text="用户明确决定验证审批崩溃恢复",
            role_id=str(session["roleId"]),
        )["evidence"]
        gateway = ControlToolGateway(
            sessions=self.service.sessions,
            management=object(),
            core=object(),
            project=self.service.project,
            role_books=self.service.role_books,
        )

        preview = gateway.execute(
            {
                "schemaVersion": "rag-ime.agent-tool-call.v1",
                "sessionId": session["id"],
                "tool": "memory",
                "toolCallId": "tool:memory-recovery:preview",
                "args": {
                    "op": "remember_preview",
                    "text": "当前要验证审批崩溃恢复",
                    "memoryKind": "decision",
                    "evidenceIds": [evidence["evidenceId"]],
                },
            }
        )["result"]
        prepared = gateway.execute(
            {
                "schemaVersion": "rag-ime.agent-tool-call.v1",
                "sessionId": session["id"],
                "tool": "memory",
                "toolCallId": "tool:memory-recovery:apply",
                "args": {
                    "op": "remember_apply",
                    "proposalId": preview["proposalId"],
                },
            }
        )["result"]
        approval = prepared["approval"]
        decided = self.service.sessions.decide_approval(
            str(approval["approvalId"]),
            approved=True,
            payload_sha256=str(approval["payloadSha256"]),
        )

        committed_receipt = gateway.apply_approval(decided)
        self.assertTrue(committed_receipt["mutationApplied"])
        self.assertEqual(
            self.service.sessions.get_approval(str(approval["approvalId"]))["state"],
            "approved",
        )
        self.service.bind_approval_executor(gateway.apply_approval)
        with (
            patch.object(self.service.runtime, "has_pending_approval", return_value=False),
            self.assertRaisesRegex(ValueError, "payload is stale"),
        ):
            self.service.decide_approval(
                str(approval["approvalId"]),
                {"decision": "approve", "payloadSha256": "d" * 64},
            )
        self.assertEqual(
            self.service.sessions.get_approval(str(approval["approvalId"]))["state"],
            "approved",
        )

        with patch.object(
            self.service.runtime,
            "has_pending_approval",
            return_value=False,
        ):
            recovered = self.service.decide_approval(
                str(approval["approvalId"]),
                {
                    "decision": "approve",
                    "payloadSha256": approval["payloadSha256"],
                },
            )

        self.assertEqual(recovered["approval"]["state"], "applied")
        self.assertTrue(recovered["approval"]["receipt"]["mutationApplied"])
        self.assertTrue(recovered["approval"]["receipt"]["idempotentReplay"])
        with sqlite3.connect(self.service.db_path) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM memory_atoms WHERE text = ?",
                    ("当前要验证审批崩溃恢复",),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    """
                    SELECT status
                    FROM memory_governance_proposals
                    WHERE proposal_id = ?
                    """,
                    (preview["proposalId"],),
                ).fetchone()[0],
                "applied",
            )

    def test_memory_review_decision_resumes_the_active_pi_turn(self) -> None:
        session = self.service.create_session({"title": "记忆草案审阅"})["session"]
        session_id = str(session["id"])
        with (
            patch.object(self.service.runtime, "has_pending_review", return_value=True),
            patch.object(self.service.runtime, "resolve_review") as resolve,
        ):
            result = self.service.resolve_review(
                session_id,
                {"runId": "memory-run-1", "decision": "deferred"},
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["decision"], "deferred")
        resolve.assert_called_once_with(
            session_id,
            "memory-run-1",
            reviewed=False,
        )

        with self.assertRaisesRegex(ValueError, "reviewed or deferred"):
            self.service.resolve_review(
                session_id,
                {"runId": "memory-run-1", "decision": "approve"},
            )

    def test_applied_memory_review_retires_the_exact_recovered_turn(self) -> None:
        session = self.service.create_session({"title": "恢复已应用的记忆审阅"})["session"]
        session_id = str(session["id"])
        run_id = "memory-run-applied-recovery"
        recovered_turn_id = "turn:memory-review-recovered"
        with sqlite3.connect(self.service.db_path) as conn:
            conn.execute(
                """
                INSERT INTO memory_cleanup_runs(
                    run_id, created_at_ms, status, summary
                ) VALUES (?, ?, 'applied', ?)
                """,
                (run_id, 1, "记忆草案已应用"),
            )
        self.service.events.publish(
            session_id,
            "user_input_required",
            {
                "requestKind": "memory_review",
                "runId": run_id,
                "method": "confirm",
            },
            turn_id=recovered_turn_id,
        )
        recovered_state = {
            "schemaVersion": "rag-ime.pi-session-control-state.v1",
            "sessionId": session_id,
            "isIdle": True,
            "activeTurn": {"turnId": recovered_turn_id},
        }
        with (
            patch.object(self.service.runtime, "has_pending_review", return_value=False),
            patch.object(
                self.service.runtime,
                "ensure",
                return_value={"state": recovered_state},
            ) as ensure,
            patch.object(
                self.service.runtime,
                "retire_recovered_turn",
                create=True,
                return_value={
                    "schemaVersion": "rag-ime.pi-recovered-turn-retirement.v1",
                    "sessionId": session_id,
                    "turnId": recovered_turn_id,
                    "retired": True,
                },
            ) as retire,
        ):
            result = self.service.resolve_review(
                session_id,
                {"runId": run_id, "decision": "reviewed"},
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["runtimeNotified"])
        self.assertTrue(result["recovered"])
        self.assertEqual(result["recoveryAction"], "retire_recovered_turn")
        ensure.assert_called_once_with(session_id)
        retire.assert_called_once_with(session_id, recovered_turn_id)

    def test_applied_memory_review_never_retires_a_newer_active_turn(self) -> None:
        session = self.service.create_session({"title": "不误伤新回合"})["session"]
        session_id = str(session["id"])
        run_id = "memory-run-applied-newer-turn"
        recovered_turn_id = "turn:memory-review-original"
        with sqlite3.connect(self.service.db_path) as conn:
            conn.execute(
                """
                INSERT INTO memory_cleanup_runs(
                    run_id, created_at_ms, status, summary
                ) VALUES (?, ?, 'applied', ?)
                """,
                (run_id, 1, "记忆草案已应用"),
            )
        self.service.events.publish(
            session_id,
            "user_input_required",
            {"requestKind": "memory_review", "runId": run_id},
            turn_id=recovered_turn_id,
        )
        newer_state = {
            "schemaVersion": "rag-ime.pi-session-control-state.v1",
            "sessionId": session_id,
            "isIdle": True,
            "activeTurn": {"turnId": "turn:newer"},
        }
        with (
            patch.object(self.service.runtime, "has_pending_review", return_value=False),
            patch.object(
                self.service.runtime,
                "ensure",
                return_value={"state": newer_state},
            ),
            patch.object(
                self.service.runtime,
                "retire_recovered_turn",
                create=True,
            ) as retire,
            self.assertRaisesRegex(
                ValueError,
                "does not match the expected recovered turn",
            ),
        ):
            self.service.resolve_review(
                session_id,
                {"runId": run_id, "decision": "reviewed"},
            )

        retire.assert_not_called()

    def test_applied_memory_review_recovers_from_durable_event_after_replay_reset(self) -> None:
        session = self.service.create_session({"title": "从持久事件恢复审阅"})["session"]
        session_id = str(session["id"])
        run_id = "memory-run-applied-durable-recovery"
        recovered_turn_id = "turn:memory-review-durable"
        with sqlite3.connect(self.service.db_path) as conn:
            conn.execute(
                """
                INSERT INTO memory_cleanup_runs(
                    run_id, created_at_ms, status, summary
                ) VALUES (?, ?, 'applied', ?)
                """,
                (run_id, 1, "记忆草案已应用"),
            )
        self.service.events.publish(
            session_id,
            "user_input_required",
            {
                "requestId": "ui-memory-review-durable",
                "requestKind": "memory_review",
                "runId": run_id,
            },
            turn_id=recovered_turn_id,
        )
        self.service.events.invalidate_projection(session_id)
        replayed, gap = self.service.events.replay(session_id)
        self.assertFalse(gap)
        self.assertFalse(
            any(event.event_type == "user_input_required" for event in replayed)
        )
        recovered_state = {
            "schemaVersion": "rag-ime.pi-session-control-state.v1",
            "sessionId": session_id,
            "isIdle": True,
            "activeTurn": {"turnId": recovered_turn_id},
        }
        with (
            patch.object(self.service.runtime, "has_pending_review", return_value=False),
            patch.object(
                self.service.runtime,
                "ensure",
                return_value={"state": recovered_state},
            ),
            patch.object(
                self.service.runtime,
                "retire_recovered_turn",
                create=True,
                return_value={
                    "schemaVersion": "rag-ime.pi-recovered-turn-retirement.v1",
                    "sessionId": session_id,
                    "turnId": recovered_turn_id,
                    "retired": True,
                },
            ) as retire,
        ):
            result = self.service.resolve_review(
                session_id,
                {"runId": run_id, "decision": "reviewed"},
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["recovered"])
        retire.assert_called_once_with(session_id, recovered_turn_id)


    def test_final_user_prompt_creates_private_source_checkpoint_without_activity(self) -> None:
        session = self.service.create_session({"title": "连续记忆"})["session"]
        with patch.object(
            self.service.runtime,
            "prompt",
            return_value={
                "accepted": True,
                "turnId": "turn:memory:1",
                "piEntryId": "pi-entry:user:1",
                "response": {"success": True},
            },
        ):
            result = self.service.prompt(
                str(session["id"]),
                {"message": "记住普通生成和深度检索要分开"},
            )

        self.assertTrue(result["memoryCheckpoint"]["stored"])
        sources = self.service.list_memory_sources({"sessionId": session["id"]})["items"]
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["sourceRole"], "user")
        events, _ = self.service.events.replay(str(session["id"]))
        self.assertNotIn("memory_checkpointed", [event.event_type for event in events])

    def test_input_method_deep_search_reuses_daily_assistant_and_checkpoints_only_question(self) -> None:
        runtime_status = {
            "schemaVersion": "rag-ime.agent-runtime.v1",
            "enabled": True,
            "status": "ready",
            "activeSessionId": None,
            "capabilities": {"rpc": True, "modelConfigured": True},
        }
        prompt_receipt = {
            "accepted": True,
            "turnId": "turn:deep:1",
            "piEntryId": "pi-entry:deep:1",
            "response": {"success": True},
        }
        payload = {
            "query": "最近我在做什么？",
            "context": "前面还讨论了 Pi Session 和双闪电入口。",
            "contextSource": "text_input_client",
            "frontAppBundleId": "com.example.editor",
            "privacyDisposition": "allowed",
            "evidence": [
                {
                    "sourceType": "memory",
                    "title": "Pi 控制中心",
                    "memoryId": "book:pi",
                    "evidencePreview": "控制中心使用连续 Pi Session。",
                }
            ],
        }

        with (
            patch.object(self.service, "runtime_status", return_value=runtime_status),
            patch.object(self.service.runtime, "prompt", return_value=prompt_receipt) as prompt,
        ):
            first = self.service.deep_search(payload)
            second = self.service.deep_search(payload)

        self.assertTrue(first["accepted"])
        self.assertTrue(first["sessionCreated"])
        self.assertFalse(second["sessionCreated"])
        self.assertEqual(first["sessionId"], second["sessionId"])
        self.assertTrue(str(first["session"]["title"]).startswith("记忆检索 "))
        self.assertEqual(first["evidenceCount"], 1)
        runtime_message = str(prompt.call_args_list[0].args[1])
        self.assertTrue(runtime_message.startswith(RUNTIME_PROMPT_ENVELOPE_PREFIX))
        sent = json.loads(
            runtime_message[len(RUNTIME_PROMPT_ENVELOPE_PREFIX):]
        )["message"]
        self.assertIn(
            "<agent-user-query>\n最近我在做什么？\n</agent-user-query>",
            sent,
        )
        self.assertIn("<agent-deep-search-context>", sent)
        self.assertNotIn("输入法深度查找任务", sent)
        self.assertIn("控制中心使用连续 Pi Session", sent)
        self.assertIn("任何写操作仍必须经过原生审批", sent)
        sources = self.service.list_memory_sources({"sessionId": first["sessionId"]})["items"]
        self.assertEqual(
            [item["canonicalTextSha256"] for item in sources],
            [hashlib.sha256("最近我在做什么？".encode()).hexdigest()],
        )

    def test_input_method_deep_search_fails_before_creating_session_when_pi_is_unavailable(self) -> None:
        with self.assertRaisesRegex(ValueError, "disabled"):
            self.service.deep_search(
                {
                    "query": "深度查找",
                    "privacyDisposition": "allowed",
                    "evidence": [],
                }
            )
        self.assertEqual(self.service.list_sessions()["items"], [])


if __name__ == "__main__":
    unittest.main()
