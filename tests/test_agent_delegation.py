from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import stat
import tempfile
import threading
import time
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from rag_ime.agent_artifacts import AgentArtifactStore
from rag_ime.agent_context_runtime import AgentContextRuntime
from rag_ime.agent_delegation import (
    AgentDelegationCoordinator,
    AgentDelegationStore,
    _subagent_failure_class,
    _subagent_prompt,
)
from rag_ime.agent_events import AgentEventHub
from rag_ime.agent_execution_policy import APPROVAL_AUTO, APPROVAL_DENY, approval_strategy
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.agent_templates import AgentTemplateBudget, agent_template as real_agent_template
from rag_ime.pi.config import PiRuntimeConfig

_TASK_CONTRACT = {
    "expectedOutput": "一份可供主持会话核验的有界结果",
    "acceptanceCriteria": ["区分已观察事实、推断与未决风险"],
}


class _CompletingRuntime:
    def __init__(self, *, sessions, events, session_context_provider=None, **_kwargs):
        self.sessions = sessions
        self.events = events
        self.context_provider = session_context_provider
        self.stopped = False

    def prompt(self, session_id, message):
        session = self.sessions.get(session_id)
        context = dict(self.context_provider(session)) if self.context_provider else {}
        template = str(context.get("agentTemplateId") or "delegate")
        turn_id = f"turn:{template}"
        self.events.publish(
            session_id,
            "message_completed",
            {
                "message": _assistant_message(session_id, turn_id, f"{template}: {message.splitlines()[-1]}"),
                "usage": {"totalTokens": 321},
            },
            turn_id=turn_id,
        )
        self.events.publish(
            session_id,
            "turn_completed",
            {"status": "completed"},
            turn_id=turn_id,
        )
        return {"accepted": True, "turnId": turn_id}

    def abort(self, session_id):
        self.events.publish(session_id, "turn_failed", {"error": "aborted"})

    def stop(self):
        self.stopped = True


class AgentTemplateDefaultTests(unittest.TestCase):
    def test_complex_private_agents_have_async_friendly_duration_budgets(self) -> None:
        self.assertEqual(
            real_agent_template("researcher").budget.max_duration_ms,
            600_000,
        )
        self.assertEqual(
            real_agent_template("worker").budget.max_duration_ms,
            900_000,
        )


class _CapturingRuntimeDriverFactory:
    def __init__(self, config):
        self.config = config
        self.contexts = []
        self.session_root = config.session_dir
        self.working_root = config.agent_dir

    def create(self, context, *, purpose, session_context_provider=None):
        self.contexts.append((context, purpose))
        return _CompletingRuntime(
            sessions=context.sessions,
            events=context.events,
            session_context_provider=session_context_provider,
        )

    def reconfigure(self, config):
        self.config = config


class _SharedCompletingRuntime(_CompletingRuntime):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.closed_session_ids: list[str] = []
        self.stop_count = 0

    def close_session(self, session_id):
        self.closed_session_ids.append(session_id)
        return True

    def stop(self):
        self.stop_count += 1
        super().stop()


class _ClaimingWithoutEvidenceRuntime(_CompletingRuntime):
    """A normal model ending is not proof that the parent task succeeded."""

    def prompt(self, session_id, _message):
        turn_id = "turn:unsupported-claim"
        self.events.publish(
            session_id,
            "message_completed",
            {
                "message": _assistant_message(
                    session_id,
                    turn_id,
                    "已修复并通过验收。",
                ),
                "usage": {"totalTokens": 12},
            },
            turn_id=turn_id,
        )
        self.events.publish(
            session_id,
            "turn_completed",
            {"status": "completed"},
            turn_id=turn_id,
        )
        return {"accepted": True, "turnId": turn_id}


class _DuplicateTerminalRuntime(_CompletingRuntime):
    """Publishes contradictory late output after an accepted terminal event."""

    def prompt(self, session_id, _message):
        first_turn_id = "turn:first-terminal"
        self.events.publish(
            session_id,
            "message_completed",
            {
                "message": _assistant_message(
                    session_id,
                    first_turn_id,
                    "首个终态前的唯一结果",
                ),
                "usage": {"totalTokens": 17},
            },
            turn_id=first_turn_id,
        )
        self.events.publish(
            session_id,
            "turn_completed",
            {"status": "completed"},
            turn_id=first_turn_id,
        )
        late_turn_id = "turn:late-contradiction"
        self.events.publish(
            session_id,
            "message_completed",
            {
                "message": _assistant_message(
                    session_id,
                    late_turn_id,
                    "不应进入父会话的迟到结果",
                ),
                "usage": {"totalTokens": 999},
            },
            turn_id=late_turn_id,
        )
        self.events.publish(
            session_id,
            "turn_failed",
            {"error": "late contradictory terminal"},
            turn_id=late_turn_id,
        )
        return {"accepted": True, "turnId": first_turn_id}


class _LateCompletionAfterCancelRuntime(_CompletingRuntime):
    """Ignores cancellation and publishes a late success."""

    def prompt(self, session_id, _message):
        self.session_id = session_id
        self.events.publish(session_id, "text_delta", {"delta": "超" * 300})
        return {"accepted": True, "turnId": "turn:over-budget-late"}

    def abort(self, session_id):
        turn_id = "turn:late-after-cancel"
        self.events.publish(
            session_id,
            "message_completed",
            {
                "message": _assistant_message(
                    session_id,
                    turn_id,
                    "取消后的迟到成功不应被接收",
                ),
                "usage": {"totalTokens": 777},
            },
            turn_id=turn_id,
        )
        self.events.publish(
            session_id,
            "turn_completed",
            {"status": "completed"},
            turn_id=turn_id,
        )


class _HangingRuntime(_CompletingRuntime):
    def prompt(self, session_id, _message):
        self.session_id = session_id
        return {"accepted": True, "turnId": "turn:hanging"}


class _ForkInspectingRuntime(_CompletingRuntime):
    snapshots: list[dict[str, object]] = []

    def prompt(self, session_id, message):
        session = self.sessions.get(session_id)
        path = Path(str(session["sessionFile"]))
        self.__class__.snapshots.append(
            {
                "path": path,
                "mode": stat.S_IMODE(path.stat().st_mode),
                "text": path.read_text(encoding="utf-8"),
            }
        )
        return super().prompt(session_id, message)


class _ControlForkRuntime(_CompletingRuntime):
    def __init__(self, sessions: AgentSessionStore, events: AgentEventHub, root: Path):
        super().__init__(sessions=sessions, events=events)
        self.root = root
        self.fork_calls: list[tuple[str, str, str]] = []

    def fork_candidates(self, session_id: str) -> list[dict[str, object]]:
        return [
            {"entryId": "entry:older", "text": "旧锚点", "role": "user", "createdAtMs": 1},
            {"entryId": "entry:latest", "text": "最新锚点", "role": "user", "createdAtMs": 2},
        ]

    def fork_session(
        self,
        source_session_id: str,
        target_session_id: str,
        *,
        entry_id: str,
    ) -> dict[str, object]:
        self.fork_calls.append((source_session_id, target_session_id, entry_id))
        self.root.mkdir(parents=True, exist_ok=True)
        transcript = self.root / f"{target_session_id.replace(':', '-')}.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "session",
                    "id": f"pi:{target_session_id}",
                    "parentSession": "control-parent.jsonl",
                    "cwd": str(self.root),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.sessions.bind_runtime_session(
            target_session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id=f"pi:{target_session_id}",
            transcript_ref=str(transcript),
            branch_anchor=entry_id,
            binding_state="active",
        )
        return {"targetSessionId": target_session_id, "entryId": entry_id}

    def close_session(self, _session_id: str) -> bool:
        return True


class _SoftBudgetRuntime(_CompletingRuntime):
    def prompt(self, session_id, message):
        self.events.publish(session_id, "text_delta", {"delta": "软" * 205})
        return super().prompt(session_id, message)


class _IgnoringAbortRuntime(_CompletingRuntime):
    instances: list["_IgnoringAbortRuntime"] = []

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.abort_count = 0
        self.stop_count = 0
        self.__class__.instances.append(self)

    def prompt(self, session_id, _message):
        self.events.publish(session_id, "text_delta", {"delta": "硬" * 300})
        return {"accepted": True, "turnId": "turn:over-budget"}

    def abort(self, _session_id):
        self.abort_count += 1

    def stop(self):
        self.stop_count += 1
        self.stopped = True


class _ManyTurnsAndToolsRuntime(_CompletingRuntime):
    def prompt(self, session_id, _message):
        for index in range(20):
            self.events.publish(
                session_id,
                "tool_started",
                {"toolCallId": f"tool:{index}", "toolName": "knowledge"},
                turn_id=f"turn:{index}",
            )
        for index in range(12):
            turn_id = f"turn:{index}"
            self.events.publish(
                session_id,
                "message_completed",
                {
                    "message": _assistant_message(session_id, turn_id, f"阶段 {index + 1}"),
                    "usage": {"totalTokens": 1},
                },
                turn_id=turn_id,
            )
        self.events.publish(
            session_id,
            "turn_completed",
            {"status": "completed"},
            turn_id="turn:11",
        )
        return {"accepted": True, "turnId": "turn:11"}


class _InteractiveRuntime(_CompletingRuntime):
    instances: list["_InteractiveRuntime"] = []

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.deliveries: list[tuple[str, str]] = []
        self.ui_resolutions: list[tuple[str, dict[str, object]]] = []
        self.session_id = ""
        self.__class__.instances.append(self)

    def prompt(self, session_id, message, *, delivery="prompt", **_kwargs):
        self.deliveries.append((delivery, message))
        self.session_id = session_id
        if delivery == "prompt":
            self.events.publish(
                session_id,
                "tool_started",
                {"toolCallId": "tool:1", "toolName": "knowledge"},
                turn_id="turn:interactive",
            )
            self.events.publish(
                session_id,
                "user_input_required",
                {
                    "requestId": "request:choice",
                    "method": "confirm",
                    "title": "选择实现路径",
                    "message": "是否保留兼容层？",
                    "options": ["是", "否"],
                    "defaultValue": "否",
                },
                turn_id="turn:interactive",
            )
        return {"accepted": True, "turnId": "turn:interactive", "delivery": delivery}

    def resolve_ui_request(self, session_id, request_id, *, response):
        self.session_id = session_id
        self.ui_resolutions.append((request_id, dict(response)))
        return {"requestId": request_id, "resolved": True}

    def messages(self, session_id):
        return [
            {
                "schemaVersion": "rag-ime.agent-message.v1",
                "id": "message:task",
                "sessionId": session_id,
                "turnId": "turn:interactive",
                "role": "user",
                "status": "completed",
                "blocks": [
                    {
                        "id": "block:task",
                        "type": "text",
                        "status": "completed",
                        "presentationKind": "markdown",
                        "data": {"text": "核对实现路径"},
                    }
                ],
                "attachments": [],
                "citations": [],
                "createdAtMs": 1,
            }
        ]


class _RejectingUiRuntime(_InteractiveRuntime):
    def resolve_ui_request(self, session_id, request_id, *, response):
        del session_id, request_id, response
        raise RuntimeError("UI resolution ACK failed")


class _ResumeRuntime(_CompletingRuntime):
    prompts: list[str] = []

    def prompt(self, session_id, message, **_kwargs):
        self.__class__.prompts.append(message)
        if len(self.__class__.prompts) == 1:
            self.events.publish(
                session_id,
                "turn_failed",
                {"error": "temporary failure"},
                turn_id="turn:first",
            )
            return {"accepted": True, "turnId": "turn:first"}
        turn_id = "turn:resumed"
        self.events.publish(
            session_id,
            "message_completed",
            {
                "message": _assistant_message(session_id, turn_id, "恢复后完成"),
                "usage": {"totalTokens": 123},
            },
            turn_id=turn_id,
        )
        self.events.publish(
            session_id,
            "turn_completed",
            {"status": "completed"},
            turn_id=turn_id,
        )
        return {"accepted": True, "turnId": turn_id}

    def messages(self, _session_id):
        return []


class _RetryExhaustedRuntime(_CompletingRuntime):
    """A child whose native Pi Provider retry window has already elapsed."""

    def prompt(self, session_id, _message):
        turn_id = "turn:provider-retry-exhausted"
        self.events.publish(
            session_id,
            "turn_failed",
            {
                "error": "fetch failed",
                "failureKind": "network",
                "retryable": True,
                "hadToolActivity": False,
                "retryExhausted": True,
                "providerRetryAttempts": 7,
                "providerRetryMaxAttempts": 7,
                "nextStep": "模型连接在约 5 分钟自动重试后仍未恢复。",
            },
            turn_id=turn_id,
        )
        return {"accepted": True, "turnId": turn_id}

    def messages(self, _session_id):
        return []


class AgentDelegationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-agent-delegation-")
        self._coordinators: list[AgentDelegationCoordinator] = []
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "rag-ime.sqlite"
        self.sessions = AgentSessionStore(self.db_path)
        self.sessions.initialize()
        self.events = AgentEventHub(sequence_loader=self.sessions.max_event_sequence)
        self.context_runtime = AgentContextRuntime(self.db_path)
        self.context_runtime.initialize()
        self.config = PiRuntimeConfig(
            enabled=True,
            executable=self.root / "pi",
            agent_dir=self.root / "config",
            session_dir=self.root / "sessions",
            logs_dir=self.root / "logs",
            tools=("memory", "knowledge", "agents"),
        )
        self.parent = self.sessions.create(
            title="主持会话",
            role_id="companion-firstlight-v1",
            role_version="1",
            model_profile="gpt/test-model",
        )

    def tearDown(self) -> None:
        # A failed assertion skips the explicit close() at the end of a test.
        # Finish every owned writer before deleting its SQLite/Artifact root;
        # production close() is deliberately bounded by cancellation grace.
        threads: list[threading.Thread] = []
        for coordinator in reversed(self._coordinators):
            with coordinator._lock:
                threads.extend(coordinator._threads.values())
            coordinator.close()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive(), "delegation writer outlived test ownership")
        self.tmp.cleanup()

    def coordinator(
        self,
        runtime_factory=_CompletingRuntime,
        *,
        cancellation_grace_ms: int = 50,
        subagent_session_retention_ms: int | None = None,
        subagent_session_gc_interval_ms: int | None = None,
        room_context_provider=None,
        runtime_provider=None,
        model_route_provider=None,
    ) -> AgentDelegationCoordinator:
        coordinator = AgentDelegationCoordinator(
            db_path=self.db_path,
            runtime_config=self.config,
            sessions=self.sessions,
            events=self.events,
            context_runtime=self.context_runtime,
            runtime_factory=runtime_factory,
            runtime_provider=runtime_provider,
            cancellation_grace_ms=cancellation_grace_ms,
            subagent_session_retention_ms=subagent_session_retention_ms,
            subagent_session_gc_interval_ms=subagent_session_gc_interval_ms,
            room_context_provider=room_context_provider,
            model_route_provider=model_route_provider,
        )
        self._coordinators.append(coordinator)
        return coordinator

    def test_child_progress_mirrors_bounded_reads_and_mixed_terminal_results(self) -> None:
        # Synthetic regression based on observed policy/ledger read receipts.
        # It makes no model calls and is not a new live acceptance run.
        class _ReadProgressRuntime(_CompletingRuntime):
            def prompt(inner, session_id, message):
                ledger = "ledger-evidence.json" in message
                name = "ledger-evidence.json" if ledger else "policy-evidence.json"
                turn_id = f"turn:{name}"
                for event_type in ("tool_started", "tool_finished"):
                    inner.events.publish(session_id, event_type, {
                        "toolName": "read", "toolCallId": f"read:{name}",
                        "publicResult": {"fileName": name, "outputPreview": "PRIVATE_OUTPUT"},
                        "args": {"path": "/PRIVATE_PATH", "token": "PRIVATE_TOKEN"},
                        "isError": False,
                    }, turn_id=turn_id)
                if ledger:
                    inner.events.publish(session_id, "tool_started", {
                        "toolName": "read", "toolCallId": "read:late",
                        "publicResult": {"fileName": "late-receipts.json"},
                    }, turn_id=turn_id)
                    inner.events.publish(session_id, "tool_finished", {
                        "toolName": "read", "toolCallId": "read:late", "isError": True,
                        "publicResult": {"fileName": "late-receipts.json"},
                    }, turn_id=turn_id)
                    inner.events.publish(session_id, "turn_failed", {
                        "error": "output budget exceeded",
                    }, turn_id=turn_id)
                else:
                    inner.events.publish(session_id, "message_completed", {
                        "message": _assistant_message(session_id, turn_id, "政策材料已整理"),
                        "usage": {"totalTokens": 10},
                    }, turn_id=turn_id)
                    inner.events.publish(session_id, "turn_completed", {
                        "status": "completed",
                    }, turn_id=turn_id)
                inner.events.publish(session_id, "tool_started", {
                    "toolName": "read", "toolCallId": "read:after-terminal",
                    "publicResult": {"fileName": "after-terminal.json"},
                }, turn_id=turn_id)
                return {"accepted": True, "turnId": turn_id}

        parent_id = str(self.parent["id"])
        self.events.publish(parent_id, "tool_started", {
            "toolName": "agents", "toolCallId": "parent:delegate",
        }, turn_id="parent:turn")
        coordinator = self.coordinator(_ReadProgressRuntime)
        response = coordinator.delegate(parent_id, {
            "_toolCallId": "parent:delegate", "wait": True,
            "tasks": [{"agent": "researcher", "task": f"读取 {name}\nPRIVATE_TASK_PROMPT", **_TASK_CONTRACT}
                      for name in ("ledger-evidence.json", "policy-evidence.json")],
        })

        def receipts():
            events, _ = self.events.replay(parent_id)
            return [event for event in events if "childProgress" in event.payload]

        _wait_until(lambda: sum(event.payload["childProgress"]["phase"] == "terminal"
                               for event in receipts()) == 2)
        progress = receipts()
        self.assertTrue(all(not event.turn_id for event in progress))
        self.assertTrue(all(event.payload["parentToolCallId"] == "parent:delegate"
                            and event.payload["parentTurnId"] == "parent:turn" for event in progress))
        frames = [event.payload["childProgress"] for event in progress]
        reads = [frame for frame in frames if frame["phase"] == "tool_finished"]
        self.assertEqual({(frame["fileName"], frame["isError"]) for frame in reads}, {
            ("ledger-evidence.json", False), ("policy-evidence.json", False),
            ("late-receipts.json", True),
        })
        terminal = {frame["ordinal"]: frame for frame in frames if frame["phase"] == "terminal"}
        self.assertEqual(terminal[0]["state"], "failed")
        self.assertEqual(terminal[0]["failureReason"], "output_budget")
        self.assertEqual(terminal[1]["state"], "completed")
        self.assertEqual(terminal[1]["verificationStatus"], "unverified")
        self.assertEqual(terminal[1]["deliveryStatus"], "returned")
        self.assertEqual(terminal[0]["taskLabel"], "读取 ledger-evidence.json")
        self.assertEqual(terminal[1]["taskLabel"], "读取 policy-evidence.json")
        self.assertTrue(all(len(frame["taskLabel"]) <= 80 for frame in frames))
        self.assertEqual({frame["runId"] for frame in frames},
                         {run["id"] for run in response["batch"]["runs"]})
        serialized = json.dumps(frames)
        self.assertNotIn("PRIVATE_", serialized)
        self.assertNotIn("after-terminal", serialized)
        for run in response["batch"]["runs"]:
            records = coordinator.artifacts.lifecycle_records(owner_kind="subagent_run", owner_id=run["id"])
            self.assertTrue(any(record["eventType"] == "tool_finished" for record in records))
            self.assertNotIn("PRIVATE_", json.dumps(records))

    def test_child_progress_requires_existing_same_session_tool_scope(self) -> None:
        coordinator = self.coordinator()
        parent_id = str(self.parent["id"])
        self.events.publish("foreign:session", "tool_started", {
            "toolName": "agents", "toolCallId": "foreign:call",
        }, turn_id="foreign:turn")
        self.assertIsNone(coordinator._parent_progress_scope(parent_id, {"_toolCallId": "foreign:call"}))
        self.events.publish(parent_id, "tool_started", {
            "toolName": "read", "toolCallId": "wrong:tool",
        }, turn_id="parent:turn")
        self.assertIsNone(coordinator._parent_progress_scope(parent_id, {"_toolCallId": "wrong:tool"}))
        self.assertIsNone(coordinator._parent_progress_scope(parent_id, {"_toolCallId": "lost:call"}))
        self.events.publish(parent_id, "tool_started", {
            "toolName": "agents", "toolCallId": "parent:call",
        }, turn_id="original:turn")
        self.events.publish(parent_id, "status_changed", {"status": "busy"}, turn_id="newer:turn")
        self.assertEqual(coordinator._parent_progress_scope(parent_id, {"_toolCallId": "parent:call"}),
                         ("parent:call", "original:turn"))
        self.events.invalidate_projection(parent_id)
        self.assertIsNone(coordinator._parent_progress_scope(parent_id, {"_toolCallId": "parent:call"}))

    def test_wait_false_ack_and_status_do_not_hydrate_artifact_under_contention(self) -> None:
        class _ArtifactBarrier:
            def __init__(self) -> None:
                self.entered = threading.Event()
                self.release = threading.Event()

            def __enter__(self):
                self.entered.set()
                if not self.release.wait(2):
                    raise AssertionError("artifact worker did not receive release")
                return self

            def __exit__(self, _exc_type, _exc_value, _traceback):
                return False

        coordinator = self.coordinator()
        barrier = _ArtifactBarrier()
        coordinator.artifacts._lock = barrier
        try:
            response = coordinator.delegate(
                str(self.parent["id"]),
                {
                    "wait": False,
                    "agent": "researcher",
                    "task": "验证后台回执",
                    **_TASK_CONTRACT,
                },
            )
            batch = response["batch"]
            run_id = str(batch["runs"][0]["id"])
            self.assertTrue(barrier.entered.wait(2))

            status = coordinator.status(
                str(self.parent["id"]),
                {"runId": run_id},
            )
            status_run = status["batch"]["runs"][0]
            self.assertEqual(run_id, status_run["id"])
            self.assertIn(status_run["state"], {"running", "completed"})
            self.assertNotIn("artifact", status_run)

            barrier.release.set()
            final = coordinator.wait(str(batch["id"]))
            final_run = final["runs"][0]
            self.assertEqual("completed", final_run["state"])
            self.assertIn("artifact", final_run)
        finally:
            barrier.release.set()
            coordinator.close()

    def test_room_permission_policy_controls_inherited_and_narrowed_children(self) -> None:
        room_parent = self.sessions.create(
            title="协作 Room 父会话",
            mode="coordinator",
            workspace_roots=[str(self.root)],
        )
        coordinator = self.coordinator(
            room_context_provider=lambda session_id: (
                {
                    "permissionPolicy": {
                        "schemaVersion": "rag-ime.room-permission-policy.v1",
                        "room": {"executionMode": "full_trust"},
                        "partner": {"executionMode": "inherit"},
                        "toolAgent": {"executionMode": "inherit"},
                    },
                    "permissionPolicySource": "room",
                }
                if session_id == str(room_parent["id"])
                else {}
            )
        )
        try:
            response = coordinator.delegate(
                str(room_parent["id"]),
                {
                    "wait": False,
                    "tasks": [
                        {
                            "agent": "researcher",
                            "task": "执行 Room 默认授权任务",
                            **_TASK_CONTRACT,
                        },
                        {
                            "agent": "researcher",
                            "task": "执行 Room 只读收窄任务",
                            "access": "read_only",
                            **_TASK_CONTRACT,
                        },
                    ],
                },
            )
            inherited_run, narrowed_run = response["batch"]["runs"]
            inherited = self.sessions.get(str(inherited_run["childSessionId"]))
            narrowed = self.sessions.get(str(narrowed_run["childSessionId"]))
            self.assertEqual("coordinator", inherited["mode"])
            self.assertEqual("control-center-auto-approve-v1", inherited["toolProfileVersion"])
            self.assertEqual([str(self.root.resolve()), "/"], inherited["workspaceRoots"])
            self.assertTrue(inherited["projectContextEnabled"])
            self.assertTrue(inherited["piSkillsEnabled"])
            self.assertTrue(inherited["codexSkillsEnabled"])

            self.assertEqual("assistant", narrowed["mode"])
            self.assertEqual("subagent-readonly-v1", narrowed["toolProfileVersion"])
            self.assertEqual("read_only", narrowed["executionMode"])
            self.assertEqual([str(self.root.resolve())], narrowed["workspaceRoots"])
            coordinator.wait(str(response["batch"]["id"]))

            standalone = coordinator.delegate(
                str(self.parent["id"]),
                {
                    "wait": True,
                    "agent": "researcher",
                    "task": "保持独立 Session 默认只读",
                    **_TASK_CONTRACT,
                },
            )
            standalone_child = self.sessions.get(
                str(standalone["batch"]["runs"][0]["childSessionId"])
            )
            self.assertEqual("assistant", standalone_child["mode"])
            self.assertEqual("subagent-readonly-v1", standalone_child["toolProfileVersion"])
            self.assertEqual([], standalone_child["workspaceRoots"])
            invalid_policy_coordinator = self.coordinator(
                room_context_provider=lambda _session_id: {
                    "permissionPolicy": {
                        "schemaVersion": "wrong",
                        "room": {"executionMode": "full_trust"},
                        "partner": {"executionMode": "inherit"},
                        "toolAgent": {"executionMode": "inherit"},
                    }
                }
            )
            try:
                with self.assertRaisesRegex(ValueError, "schemaVersion"):
                    invalid_policy_coordinator.delegate(
                        str(room_parent["id"]),
                        {
                            "wait": False,
                            "agent": "researcher",
                            "task": "拒绝错误策略版本",
                            **_TASK_CONTRACT,
                        },
                    )
            finally:
                invalid_policy_coordinator.close()
        finally:
            coordinator.close()

    def test_nested_room_children_inherit_permissions_without_rebinding_dispatch(self) -> None:
        room_parent = self.sessions.create(
            title="Room 私有委派链",
            mode="coordinator",
            execution_mode="full_trust",
            tool_profile_version="control-center-auto-approve-v1",
            workspace_roots=[str(self.root), "/"],
        )
        room_context = {
            "roomBound": True,
            "roomId": "room:nested",
            "rootId": "room-turn:original",
            "taskId": "",
            "dispatchId": "room-dispatch:original",
            "generation": 1,
            "permissionPolicy": {
                "schemaVersion": "rag-ime.room-permission-policy.v1",
                "room": {"executionMode": "full_trust"},
                "partner": {"executionMode": "inherit"},
                "toolAgent": {"executionMode": "inherit"},
            },
        }
        coordinator = self.coordinator(
            room_context_provider=lambda session_id: (
                room_context
                if session_id == str(room_parent["id"])
                else {
                    "roomBound": False,
                    "roomId": "",
                    "rootId": "",
                    "taskId": "",
                    "dispatchId": "",
                    "generation": 0,
                }
            ),
        )
        try:
            for requested_access in ("inherit", "read_only"):
                with self.subTest(access=requested_access):
                    room_context.update({
                        "rootId": "room-turn:original",
                        "dispatchId": "room-dispatch:original",
                        "generation": 1,
                    })
                    first = coordinator.delegate(
                        str(room_parent["id"]),
                        {
                            "agent": "delegate",
                            "task": "第一层 Tool Agent",
                            "access": requested_access,
                            "wait": True,
                            **_TASK_CONTRACT,
                        },
                    )["batch"]
                    # A later root turn must not replace the private child's
                    # original causal receipt when it delegates again.
                    room_context.update({
                        "rootId": "room-turn:next",
                        "dispatchId": "room-dispatch:next",
                        "generation": 2,
                    })
                    second = coordinator.delegate(
                        str(first["runs"][0]["childSessionId"]),
                        {
                            "agent": "worker",
                            "task": "第二层继续已授予的权限",
                            "wait": True,
                            **_TASK_CONTRACT,
                        },
                    )["batch"]
                    child = self.sessions.get(str(second["runs"][0]["childSessionId"]))
                    expected_mode = "full_trust" if requested_access == "inherit" else "read_only"
                    self.assertEqual(child["executionMode"], expected_mode)
                    self.assertEqual(
                        child["toolProfileVersion"],
                        "control-center-auto-approve-v1"
                        if requested_access == "inherit"
                        else "subagent-readonly-v1",
                    )
                    self.assertEqual(
                        approval_strategy(child, tool="workspace_patch", operation="apply"),
                        APPROVAL_AUTO if requested_access == "inherit" else APPROVAL_DENY,
                    )
                    self.assertEqual(second["causalMetadata"], first["causalMetadata"])
        finally:
            coordinator.close()

    def test_unrestricted_writable_children_inherit_parent_profile_and_root(self) -> None:
        for profile, execution_mode in (
            ("control-center-full-access-v1", "per_action"),
            ("control-center-auto-approve-v1", "full_trust"),
        ):
            parent = self.sessions.create(
                title=f"不受限父会话 {profile}",
                mode="coordinator",
                tool_profile_version=profile,
                execution_mode=execution_mode,
                workspace_roots=["/"],
            )
            coordinator = self.coordinator()
            try:
                response = coordinator.delegate(
                    str(parent["id"]),
                    {
                        "contextMode": "fresh",
                        "wait": True,
                        "tasks": [
                            {
                                "agent": "worker",
                                "task": "在授权工作区执行修复",
                                "access": "write",
                                **_TASK_CONTRACT,
                            }
                        ],
                    },
                )
                child = self.sessions.get(
                    str(response["batch"]["runs"][0]["childSessionId"])
                )
                self.assertEqual(child["toolProfileVersion"], profile)
                self.assertEqual(child["executionMode"], execution_mode)
                self.assertEqual(child["mode"], "coordinator")
                self.assertEqual(child["workspaceRoots"], ["/"])
                self.assertEqual(child["toolAllowlistMode"], "profile")
                self.assertTrue(child["projectContextEnabled"])
                self.assertTrue(child["piSkillsEnabled"])
                self.assertTrue(child["codexSkillsEnabled"])
            finally:
                coordinator.close()

    def test_unrestricted_parent_accepts_existing_descendant_root_and_preserves_read_only_access(
        self,
    ) -> None:
        child_root = self.root / "nested-child"
        child_root.mkdir()
        for profile, execution_mode in (
            ("control-center-full-access-v1", "per_action"),
            ("control-center-auto-approve-v1", "full_trust"),
        ):
            parent = self.sessions.create(
                title=f"全系统父会话 {profile}",
                mode="coordinator",
                tool_profile_version=profile,
                execution_mode=execution_mode,
                workspace_roots=["/"],
            )
            coordinator = self.coordinator()
            try:
                response = coordinator.delegate(
                    str(parent["id"]),
                    {
                        "contextMode": "fresh",
                        "wait": True,
                        "tasks": [
                            {
                                "agent": "worker",
                                "task": "只读检查子目录",
                                "access": "read_only",
                                "workspaceRoots": [str(child_root)],
                                **_TASK_CONTRACT,
                            }
                        ],
                    },
                )
                child = self.sessions.get(
                    str(response["batch"]["runs"][0]["childSessionId"])
                )
                self.assertEqual(child["workspaceRoots"], [str(child_root.resolve())])
                self.assertEqual(child["mode"], "assistant")
                self.assertEqual(child["executionMode"], "read_only")
                self.assertEqual(child["toolProfileVersion"], "subagent-readonly-v1")
            finally:
                coordinator.close()

    def test_delegated_child_roots_still_require_existing_paths(self) -> None:
        parent = self.sessions.create(
            title="全权限父会话",
            mode="coordinator",
            tool_profile_version="control-center-full-access-v1",
            execution_mode="per_action",
            workspace_roots=["/"],
        )
        coordinator = self.coordinator()
        try:
            with self.assertRaisesRegex(ValueError, "no longer exists"):
                coordinator.delegate(
                    str(parent["id"]),
                    {
                        "contextMode": "fresh",
                        "tasks": [
                            {
                                "agent": "worker",
                                "task": "拒绝不存在的子目录",
                                "access": "write",
                                "workspaceRoots": [str(self.root / "missing-child")],
                                **_TASK_CONTRACT,
                            }
                        ],
                    },
                )
        finally:
            coordinator.close()


    def test_fixed_catalog_parallel_results_and_internal_sessions(self) -> None:
        coordinator = self.coordinator()
        catalog = coordinator.catalog()
        self.assertEqual(
            [item["templateId"] for item in catalog["items"]],
            ["researcher", "planner", "worker", "reviewer", "delegate"],
        )
        self.assertTrue(all(item["budget"]["maxTurns"] == 0 for item in catalog["items"]))
        self.assertTrue(all(item["budget"]["maxToolCalls"] == 0 for item in catalog["items"]))
        reviewer = next(item for item in catalog["items"] if item["templateId"] == "reviewer")
        worker = next(item for item in catalog["items"] if item["templateId"] == "worker")
        self.assertEqual(reviewer["defaultAccess"], "read_only")
        self.assertEqual(reviewer["allowedAccess"], ["read_only"])
        self.assertEqual(worker["defaultAccess"], "write")
        self.assertEqual(worker["allowedAccess"], ["read_only", "write"])

        response = coordinator.delegate(
            str(self.parent["id"]),
            {
                "contextMode": "fresh",
                "wait": True,
                "tasks": [
                    {
                        "agent": "researcher",
                        "task": "核对已有证据",
                        **_TASK_CONTRACT,
                        "outputSchema": {
                            "type": "object",
                            "required": ["claims"],
                            "properties": {"claims": {"type": "array"}},
                        },
                    },
                    {"agent": "reviewer", "task": "检查结论是否充分", **_TASK_CONTRACT},
                ],
            },
        )
        batch = response["batch"]
        self.assertEqual(response["acceptanceScope"], "delegation_request")
        self.assertEqual(batch["state"], "completed")
        self.assertEqual(batch["depth"], 1)
        self.assertEqual(batch["resultDeliveryMode"], "inline")
        self.assertEqual(len(batch["runs"]), 2)
        self.assertEqual({run["usage"]["totalTokens"] for run in batch["runs"]}, {321})
        self.assertTrue(
            all(run["expectedOutput"] == _TASK_CONTRACT["expectedOutput"] for run in batch["runs"])
        )
        self.assertTrue(
            all(
                run["acceptanceCriteria"] == _TASK_CONTRACT["acceptanceCriteria"]
                for run in batch["runs"]
            )
        )

        self.assertTrue(all(run["result"]["summary"] for run in batch["runs"]))
        self.assertTrue(
            all(run["result"]["deliveryStatus"] == "returned" for run in batch["runs"])
        )
        self.assertEqual(
            {run["result"]["verificationStatus"] for run in batch["runs"]},
            {"contract_invalid", "unverified"},
        )
        self.assertTrue(
            all(run["result"]["authority"] == "evidence_only" for run in batch["runs"])
        )
        self.assertTrue(all(run["artifact"]["recordCount"] >= 4 for run in batch["runs"]))
        self.assertNotIn(str(self.root), json.dumps(batch["runs"][0]["artifact"]))
        self.assertEqual([item["id"] for item in self.sessions.list()], [self.parent["id"]])
        self.assertEqual(
            [item["id"] for item in self.sessions.list(include_archived=True)],
            [self.parent["id"]],
        )
        internal = self.sessions.list(include_archived=True, include_internal=True)
        self.assertEqual(len(internal), 3)
        children = [item for item in internal if item["id"] != self.parent["id"]]
        self.assertTrue(all(item["sessionKind"] == "subagent_runtime" for item in children))
        self.assertTrue(all(item["status"] != "archived" for item in children))
        run_id = str(batch["runs"][0]["id"])
        _wait_until(
            lambda: "runtime_retained"
            in {
                str(item["eventType"])
                for item in coordinator.artifacts.lifecycle_records(
                    owner_kind="subagent_run", owner_id=run_id
                )
            }
        )
        records = coordinator.artifacts.lifecycle_records(
            owner_kind="subagent_run",
            owner_id=run_id,
        )
        self.assertIn("runtime_retained", {str(item["eventType"]) for item in records})
        projection = coordinator.artifacts.snapshot(
            owner_kind="subagent_run",
            owner_id=run_id,
        )["projection"]
        self.assertEqual(projection["expectedOutput"], _TASK_CONTRACT["expectedOutput"])
        self.assertEqual(
            projection["acceptanceCriteria"],
            _TASK_CONTRACT["acceptanceCriteria"],
        )
        self.assertEqual(projection["outputSchema"]["required"], ["claims"])
        self.assertNotIn("runtime_retired", {str(item["eventType"]) for item in records})
        with self.assertRaisesRegex(ValueError, "unsupported agent template"):
            coordinator.delegate(
                str(self.parent["id"]),
                {"agent": "market-shell-agent", "task": "执行任意命令", **_TASK_CONTRACT},
            )
        coordinator.close()

    def test_delegation_budget_may_narrow_but_never_expand_template_limits(self) -> None:
        coordinator = self.coordinator()
        try:
            response = coordinator.delegate(
                str(self.parent["id"]),
                {
                    "wait": True,
                    "agent": "reviewer",
                    "task": "在有界预算内检查证据覆盖",
                    "budget": {
                        "maxTotalTokens": 8_000,
                        "maxDurationMs": 120_000,
                        "maxOutputChars": 4_000,
                    },
                    **_TASK_CONTRACT,
                },
            )
            self.assertEqual(
                {
                    "maxTurns": 0,
                    "maxToolCalls": 0,
                    "maxTotalTokens": 8_000,
                    "maxDurationMs": 120_000,
                    "maxOutputChars": 4_000,
                },
                response["batch"]["runs"][0]["budget"],
            )
            with self.assertRaisesRegex(ValueError, "budget cannot exceed"):
                coordinator.delegate(
                    str(self.parent["id"]),
                    {
                        "wait": True,
                        "tasks": [
                            {
                                "agent": "reviewer",
                                "task": "拒绝扩大模板预算",
                                "budget": {"maxTotalTokens": 64_000},
                                **_TASK_CONTRACT,
                            }
                        ],
                    },
                )
        finally:
            coordinator.close()

    def test_read_only_child_inherits_parent_workspace_roots_without_write_authority(
        self,
    ) -> None:
        parent_id = str(self.parent["id"])
        self.parent = self.sessions.set_runtime_policy(
            parent_id,
            mode="coordinator",
            tool_profile_version="control-center-v1",
            execution_mode="workspace_managed",
            grant_workspace_scope=True,
            allowed_tools=None,
            pi_skills_enabled=False,
            codex_skills_enabled=False,
            workspace_roots=[str(self.root)],
        )
        coordinator = self.coordinator()
        try:
            batch = coordinator.delegate(
                parent_id,
                {
                    "agent": "researcher",
                    "task": "只读核对父 Session 工作区里的源码",
                    "access": "read_only",
                    "wait": True,
                    **_TASK_CONTRACT,
                },
            )["batch"]
            run = batch["runs"][0]
            child = self.sessions.get(str(run["childSessionId"]))

            self.assertEqual(child["workspaceRoots"], [str(self.root.resolve())])
            self.assertEqual(child["mode"], "assistant")
            self.assertEqual(child["toolProfileVersion"], "subagent-readonly-v1")
            self.assertEqual(child["executionMode"], "read_only")
            self.assertFalse(child["workspaceScopeGranted"])
            self.assertEqual(run["launchDigest"]["workspaceAccess"], "read_only")
            self.assertEqual(run["launchDigest"]["workspaceRootCount"], 1)
        finally:
            coordinator.close()

    def test_control_launch_can_fork_latest_pi_anchor_and_review_stays_read_only(self) -> None:
        fork_runtime = _ControlForkRuntime(
            self.sessions,
            self.events,
            self.config.session_dir,
        )
        coordinator = self.coordinator(runtime_provider=lambda: fork_runtime)
        with self.assertRaisesRegex(ValueError, "reviewer does not allow write"):
            coordinator.delegate(
                str(self.parent["id"]),
                {
                    "agent": "reviewer",
                    "task": "只读审查",
                    "access": "write",
                    **_TASK_CONTRACT,
                },
            )

        response = coordinator.delegate(
            str(self.parent["id"]),
            {
                "agent": "reviewer",
                "task": "沿用当前讨论做独立审查",
                "access": "read_only",
                "contextMode": "fork",
                "forkEntryId": "latest",
                "wait": True,
                **_TASK_CONTRACT,
            },
        )

        run = response["batch"]["runs"][0]
        self.assertEqual(fork_runtime.fork_calls[0][0], self.parent["id"])
        self.assertEqual(fork_runtime.fork_calls[0][2], "entry:latest")
        self.assertEqual(run["launchDigest"]["contextMode"], "fork")
        self.assertEqual(run["launchDigest"]["workspaceAccess"], "read_only")
        self.assertEqual(
            self.sessions.runtime_binding(str(run["childSessionId"]))["branchAnchor"],
            "entry:latest",
        )
        coordinator.close()

    def test_read_only_execution_mode_fences_stale_parent_profile(self) -> None:
        coordinator = self.coordinator()
        parent_id = str(self.parent["id"])
        original_get = self.sessions.get

        def stale_parent_get(session_id: str, *args, **kwargs):
            session = original_get(session_id, *args, **kwargs)
            if str(session_id) == parent_id:
                return {
                    **session,
                    "toolProfileVersion": "control-center-v1",
                    "executionMode": "read_only",
                }
            return session

        try:
            with patch.object(self.sessions, "get", side_effect=stale_parent_get):
                batch = coordinator.delegate(
                    parent_id,
                    {"agent": "worker", "task": "只读子任务", "wait": True, **_TASK_CONTRACT},
                )["batch"]
            child = self.sessions.get(str(batch["runs"][0]["childSessionId"]))
            self.assertEqual(child["toolProfileVersion"], "subagent-readonly-v1")
            self.assertEqual(child["executionMode"], "read_only")
        finally:
            coordinator.close()

    def test_delegated_driver_inherits_exact_gateway_and_manifest_provider(self) -> None:
        factory = _CapturingRuntimeDriverFactory(self.config)

        def manifests(session):
            return [{"name": "rag_benchmark", "sessionId": session["id"]}]

        coordinator = AgentDelegationCoordinator(
            db_path=self.db_path,
            runtime_config=self.config,
            sessions=self.sessions,
            events=self.events,
            context_runtime=self.context_runtime,
            runtime_driver_factory=factory,
            tool_gateway_token="benchmark-token",
            tool_gateway_url="http://127.0.0.1:54321/api/agent/tool/execute",
            tool_manifest_provider=manifests,
        )
        try:
            response = coordinator.delegate(
                str(self.parent["id"]),
                {"agent": "researcher", "task": "核对检索证据", "wait": True, **_TASK_CONTRACT},
            )
            self.assertEqual("completed", response["batch"]["state"])
            context, purpose = factory.contexts[0]
            self.assertEqual("delegated", purpose)
            self.assertEqual("benchmark-token", context.tool_gateway_token)
            self.assertEqual(
                "http://127.0.0.1:54321/api/agent/tool/execute",
                context.tool_gateway_url,
            )
            self.assertIs(manifests, context.tool_manifest_provider)
        finally:
            coordinator.close()

    def test_delegated_child_reuses_shared_runtime_without_stopping_host(self) -> None:
        factory = _CapturingRuntimeDriverFactory(self.config)
        shared = _SharedCompletingRuntime(
            sessions=self.sessions,
            events=self.events,
        )
        coordinator = AgentDelegationCoordinator(
            db_path=self.db_path,
            runtime_config=self.config,
            sessions=self.sessions,
            events=self.events,
            context_runtime=self.context_runtime,
            runtime_driver_factory=factory,
            runtime_provider=lambda: shared,
        )
        try:
            response = coordinator.delegate(
                str(self.parent["id"]),
                {
                    "wait": True,
                    "agent": "researcher",
                    "task": "核对共享 Host",
                    **_TASK_CONTRACT,
                },
            )
            run = response["batch"]["runs"][0]
            child_id = str(run["childSessionId"])
            self.assertEqual("completed", run["state"])
            self.assertEqual([], factory.contexts)
            self.assertEqual([child_id], shared.closed_session_ids)
            self.assertEqual(0, shared.stop_count)
            context = coordinator.runtime_session_context(
                self.sessions.get(child_id)
            )
            self.assertEqual("researcher", context["agentTemplateId"])
            self.assertEqual(1, context["delegationDepth"])
        finally:
            coordinator.close()
        self.assertEqual(0, shared.stop_count)

    def test_room_bound_delegation_records_task_lineage(self) -> None:
        coordinator = self.coordinator(
            room_context_provider=lambda _session_id: {
                "roomBound": True,
                "roomId": "room:delegation",
                "rootId": "root:delegation",
                "taskId": "task:delegation",
                "dispatchId": "dispatch:delegation",
                "generation": 7,
            }
        )

        response = coordinator.delegate(
            str(self.parent["id"]),
            {
                "agent": "researcher",
                "task": "读取任务证据",
                "wait": True,
                **_TASK_CONTRACT,
            },
        )

        self.assertEqual(
            response["batch"]["causalMetadata"],
            {
                "todoId": f"todo:{self.parent['id']}",
                "todoRevision": 0,
                "goalId": "",
                "goalRevision": 0,
                "roomBound": True,
                "roomId": "room:delegation",
                "rootId": "root:delegation",
                "taskId": "task:delegation",
                "dispatchId": "dispatch:delegation",
                "generation": 7,
            },
        )
        coordinator.close()

    def test_session_room_tool_agent_result_is_deliverable_without_kernel_root(self) -> None:
        coordinator = self.coordinator(
            room_context_provider=lambda _session_id: {
                "roomBound": True,
                "roomId": "room:session-composed",
                "rootId": "room-turn:session-composed",
                "taskId": "",
                "dispatchId": "room-dispatch:session-composed",
                "generation": 0,
            }
        )
        try:
            response = coordinator.delegate(
                str(self.parent["id"]),
                {
                    "agent": "researcher",
                    "task": "返回一条事件结果",
                    "wait": True,
                    **_TASK_CONTRACT,
                },
            )
            run = response["batch"]["runs"][0]
            self.assertTrue(
                coordinator.store.room_delivery_allowed(str(run["id"]))
            )
        finally:
            coordinator.close()

    def test_room_bound_child_prompt_routes_handoff_without_private_room_ownership(
        self,
    ) -> None:
        run = {
            "task": "核对 Room 委派上下文",
            "expectedOutput": "有界结果",
            "acceptanceCriteria": ["列出真实依据"],
        }
        fork_batch = {
            "contextMode": "fork",
            "depth": 2,
            "maxDepth": 2,
            "causalMetadata": {"roomBound": True},
        }
        fork_prompt = _subagent_prompt(run, fork_batch)
        self.assertIn("私有、有界助手", fork_prompt)
        self.assertIn("不得发布 Room 进展、委派正式 Room Partner", fork_prompt)
        self.assertIn("不得代替 parent 给用户最终答复", fork_prompt)
        self.assertIn("exact managed Pi transcript prefix", fork_prompt)
        self.assertIn("不承诺 provider cache hit", fork_prompt)
        self.assertIn("只用 status/symbols/hover/definition/references/diagnostics", fork_prompt)
        self.assertIn("hash-bound approval", fork_prompt)
        self.assertIn("导出符号变更先用 references", fork_prompt)

        fresh_prompt = _subagent_prompt(
            run,
            {
                **fork_batch,
                "contextMode": "fresh",
            },
        )
        self.assertIn("fresh 表示独立上下文", fresh_prompt)
        self.assertIn("不引入父会话私有 transcript", fresh_prompt)

        ordinary_prompt = _subagent_prompt(
            run,
            {
                **fork_batch,
                "causalMetadata": {"roomBound": False},
            },
        )
        self.assertNotIn("Room nested handoff", ordinary_prompt)


    def test_delegation_contract_rejects_legacy_unstructured_tasks(self) -> None:
        coordinator = self.coordinator()
        with self.assertRaisesRegex(ValueError, "expectedOutput"):
            coordinator.delegate(
                str(self.parent["id"]),
                {"agent": "researcher", "task": "旧式无结构委派"},
            )
        with self.assertRaisesRegex(ValueError, "one to eight"):
            coordinator.delegate(
                str(self.parent["id"]),
                {
                    "agent": "researcher",
                    "task": "缺少验收条件",
                    "expectedOutput": "一份报告",
                    "acceptanceCriteria": [],
                },
            )
        coordinator.close()

    def test_store_serializes_concurrent_terminal_writers(self) -> None:
        artifacts = AgentArtifactStore(self.db_path)
        store = AgentDelegationStore(self.db_path, artifacts=artifacts)
        store.initialize()
        child = self.sessions.create(title="concurrent terminal child")
        batch = store.create_batch(
            parent_session_id=str(self.parent["id"]),
            parent_run_id="",
            context_mode="fresh",
            depth=1,
            max_depth=2,
            runs=[_run_spec(str(child["id"]), task="并发终态只接收一次")],
        )
        run_id = str(batch["runs"][0]["id"])
        store.start_run(run_id)

        def finish(summary: str) -> dict[str, object]:
            return store.finish_run(
                run_id,
                state="completed",
                result={
                    "summary": summary,
                    "deliveryStatus": "returned",
                    "verificationStatus": "unverified",
                    "authority": "evidence_only",
                },
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            returned = list(pool.map(finish, ("first", "second")))

        self.assertEqual(returned[0]["result"], returned[1]["result"])
        self.assertIn(returned[0]["result"]["summary"], {"first", "second"})
        terminal_events = [
            event["eventType"]
            for event in store.list_events(run_id)
            if event["eventType"] in {"completed", "failed", "aborted", "timed_out"}
        ]
        self.assertEqual(terminal_events, ["completed"])

    def test_store_tracks_stable_attempt_identity_and_structured_contract(self) -> None:
        artifacts = AgentArtifactStore(self.db_path)
        store = AgentDelegationStore(self.db_path, artifacts=artifacts)
        store.initialize()
        child = self.sessions.create(title="structured child")
        spec = {
            **_run_spec(str(child["id"]), task="返回结构化结论"),
            "outputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["claims"],
                "properties": {
                    "claims": {
                        "type": "array",
                        "items": {"type": "string"},
                    }
                },
            },
        }
        batch = store.create_batch(
            parent_session_id=str(self.parent["id"]),
            parent_run_id="",
            context_mode="fresh",
            depth=1,
            max_depth=2,
            runs=[spec],
        )
        run = batch["runs"][0]
        self.assertTrue(str(run["nodeId"]).startswith("subagent-node:"))
        self.assertTrue(str(run["attemptId"]).startswith("subagent-attempt:"))
        self.assertEqual(run["attemptNumber"], 1)
        self.assertEqual(run["predecessorAttemptId"], "")
        self.assertEqual(run["parentRunId"], "")
        self.assertEqual(run["depth"], 1)
        self.assertEqual(run["contract"]["status"], "pending")
        self.assertTrue(run["launchDigest"]["outputContract"]["required"])
        self.assertEqual(len(run["launchDigest"]["outputContract"]["schemaSha256"]), 64)

        run_id = str(run["id"])
        store.start_run(run_id)
        with self.assertRaisesRegex(ValueError, "delivery contract validation failed"):
            store.submit_structured_output(
                child_session_id=str(child["id"]),
                value={"claims": "not-an-array"},
                tool_call_id="tool:invalid",
                validated_at_ms=100,
            )
        invalid = store.get_run(run_id)
        self.assertEqual(invalid["contract"]["status"], "invalid")
        self.assertNotIn("structuredOutput", invalid)

        valid = store.submit_structured_output(
            child_session_id=str(child["id"]),
            value={"claims": ["bounded"]},
            tool_call_id="tool:valid",
            validated_at_ms=200,
        )
        self.assertEqual(valid["contract"]["status"], "valid")
        self.assertEqual(valid["contract"]["toolCallId"], "tool:valid")
        self.assertEqual(valid["structuredOutput"], {"claims": ["bounded"]})
        replay = store.submit_structured_output(
            child_session_id=str(child["id"]),
            value={"claims": ["bounded"]},
            tool_call_id="tool:valid",
            validated_at_ms=300,
        )
        self.assertEqual(replay["structuredOutput"], {"claims": ["bounded"]})
        with self.assertRaisesRegex(ValueError, "already submitted"):
            store.submit_structured_output(
                child_session_id=str(child["id"]),
                value={"claims": ["different"]},
                tool_call_id="tool:different",
            )

    def test_store_rejects_top_level_false_schema_before_persistence(self) -> None:
        store = AgentDelegationStore(self.db_path)
        store.initialize()
        child = self.sessions.create(title="false schema child")
        with self.assertRaisesRegex(ValueError, "outputSchema must be a JSON Schema object"):
            store.create_batch(
                parent_session_id=str(self.parent["id"]),
                parent_run_id="",
                context_mode="fresh",
                depth=1,
                max_depth=2,
                runs=[{
                    **_run_spec(str(child["id"]), task="验证布尔 Schema"),
                    "outputSchema": False,
                }],
            )
        self.assertEqual(store.list_batches(parent_session_id=str(self.parent["id"])), [])

    def test_malformed_persisted_structured_schema_is_an_invalid_recoverable_contract(self) -> None:
        store = AgentDelegationStore(self.db_path)
        store.initialize()
        child = self.sessions.create(title="malformed schema child")
        batch = store.create_batch(
            parent_session_id=str(self.parent["id"]),
            parent_run_id="",
            context_mode="fresh",
            depth=1,
            max_depth=2,
            runs=[{
                **_run_spec(str(child["id"]), task="恢复损坏合同"),
                "outputSchema": {"type": "object"},
            }],
        )
        run_id = str(batch["runs"][0]["id"])
        store.start_run(run_id)
        with store._connect() as conn:
            conn.execute(
                "UPDATE agent_subagent_runs SET output_schema_json = '[' WHERE id = ?",
                (run_id,),
            )

        malformed = store.get_run(run_id)
        self.assertEqual(malformed["contract"]["status"], "invalid")
        self.assertIn("persisted output schema is malformed", malformed["contract"]["error"])
        with self.assertRaisesRegex(ValueError, "persisted output schema is malformed"):
            store.submit_structured_output(
                child_session_id=str(child["id"]),
                value={},
                tool_call_id="tool:malformed",
            )

    def test_missing_structured_output_is_a_local_contract_failure(self) -> None:
        store = AgentDelegationStore(self.db_path)
        store.initialize()
        child = self.sessions.create(title="missing structured child")
        batch = store.create_batch(
            parent_session_id=str(self.parent["id"]),
            parent_run_id="",
            context_mode="fresh",
            depth=1,
            max_depth=2,
            runs=[
                {
                    **_run_spec(str(child["id"]), task="遗漏结构化结论"),
                    "outputSchema": {
                        "type": "object",
                        "required": ["summary"],
                        "properties": {"summary": {"type": "string"}},
                    },
                }
            ],
        )
        run_id = str(batch["runs"][0]["id"])
        store.start_run(run_id)
        store.finish_run(
            run_id,
            state="completed",
            result={
                "summary": "模型已返回，但没有调用终止工具",
                "deliveryStatus": "returned",
                "verificationStatus": "unverified",
                "authority": "evidence_only",
            },
        )
        finalized = store.finalize_structured_output_contract(run_id)
        self.assertEqual(finalized["state"], "completed")
        self.assertEqual(finalized["contract"]["status"], "invalid")
        self.assertIn("was not submitted", finalized["contract"]["error"])

        reopened = store.reopen_run(run_id)
        self.assertEqual(reopened["state"], "queued")
        self.assertEqual(reopened["contract"]["status"], "pending")
        store.start_run(run_id)
        recovered = store.submit_structured_output(
            child_session_id=str(child["id"]),
            value={"summary": "已补交"},
            tool_call_id="tool:recovered",
        )
        self.assertEqual(recovered["contract"]["status"], "valid")

    def test_retry_policy_allows_only_one_receipt_free_runtime_retry(self) -> None:
        coordinator = self.coordinator()
        base = {
            "state": "failed",
            "result": {"failureClass": "transient_runtime"},
            "usage": {"toolCount": 0},
            "attemptNumber": 1,
            "attemptId": "attempt:one",
        }
        self.assertEqual(coordinator._retry_capability(base), (True, ""))
        allowed, reason = coordinator._retry_capability(
            {**base, "usage": {"toolCount": 1}}
        )
        self.assertFalse(allowed)
        self.assertIn("Tool", reason)
        allowed, reason = coordinator._retry_capability(
            {**base, "result": {"failureClass": "logic_error"}}
        )
        self.assertFalse(allowed)
        self.assertIn("修复或改派", reason)
        allowed, reason = coordinator._retry_capability({**base, "attemptNumber": 2})
        self.assertFalse(allowed)
        self.assertIn("唯一一次", reason)
        coordinator.close()

    def test_workspace_boundary_failures_are_tool_errors_not_transient_runtime(self) -> None:
        for message in (
            "path is outside the authorized workspace or does not exist",
            "path does not exist in the authorized workspace; nearby entries: src/main.py",
            "outside the authorized workspace",
            "permission denied while reading workspace file",
        ):
            with self.subTest(message=message):
                self.assertEqual(_subagent_failure_class(message), "tool_error")

    def test_async_terminal_result_is_scheduled_once_without_parent_acceptance(self) -> None:
        coordinator = self.coordinator()
        response = coordinator.delegate(
            str(self.parent["id"]),
            {
                "agent": "reviewer",
                "task": "异步核对证据",
                "expectedOutput": "结构化复核结论",
                "acceptanceCriteria": ["结论带有证据边界"],
                "outputSchema": {
                    "type": "object",
                    "required": ["summary"],
                    "properties": {"summary": {"type": "string"}},
                },
                "wait": False,
            },
        )
        self.assertFalse(response["waited"])
        self.assertEqual(response["batch"]["resultDeliveryMode"], "next_turn")
        run_id = str(response["batch"]["runs"][0]["id"])
        _wait_until(
            lambda: coordinator.store.get_run(run_id, hydrate_artifacts=False)["resultContextScheduledAtMs"]
            is not None
        )
        terminal = coordinator.store.get_run(run_id)
        self.assertEqual(terminal["state"], "completed")
        self.assertEqual(terminal["result"]["deliveryStatus"], "returned")
        self.assertEqual(
            terminal["result"]["verificationStatus"],
            "contract_invalid",
        )
        self.assertEqual(terminal["result"]["authority"], "evidence_only")
        context_items = [
            item
            for item in self.context_runtime.list_items(str(self.parent["id"]))
            if item["sourceKind"] == "subagent_result"
        ]
        self.assertEqual(len(context_items), 1)
        item_id = context_items[0]["itemId"]
        coordinator.close()

        restarted = self.coordinator()
        repeated = [
            item
            for item in self.context_runtime.list_items(str(self.parent["id"]))
            if item["sourceKind"] == "subagent_result"
        ]
        self.assertEqual([item["itemId"] for item in repeated], [item_id])
        materialized = self.context_runtime.materialize(str(self.parent["id"]))
        self.assertEqual(materialized["itemIds"], [item_id])
        payload = materialized["items"][0]["payload"]
        self.assertEqual(payload["deliveryStatus"], "returned")
        self.assertEqual(payload["verificationStatus"], "contract_invalid")
        self.assertEqual(
            payload["outputSchema"]["required"],
            ["summary"],
        )
        self.assertEqual(payload["authority"], "evidence_only")
        self.assertEqual(payload["acceptanceCriteria"], ["结论带有证据边界"])
        self.assertEqual(
            self.sessions.agent_todo(str(self.parent["id"]))["phases"],
            [],
        )
        restarted.close()

    def test_retry_exhaustion_reports_upward_and_returns_control_to_parent(self) -> None:
        coordinator = self.coordinator(_RetryExhaustedRuntime)
        try:
            response = coordinator.delegate(
                str(self.parent["id"]),
                {
                    "agent": "researcher",
                    "task": "等待模型连接后核对证据",
                    **_TASK_CONTRACT,
                    "wait": False,
                },
            )
            run_id = str(response["batch"]["runs"][0]["id"])
            _wait_until(
                lambda: coordinator.store.get_run(run_id)[
                    "resultContextScheduledAtMs"
                ]
                is not None
            )

            run = coordinator.store.get_run(run_id)
            self.assertEqual(run["state"], "failed")
            self.assertTrue(run["result"]["failureReport"]["retryExhausted"])
            self.assertEqual(
                run["result"]["failureReport"]["providerRetryAttempts"],
                7,
            )
            self.assertFalse(run["result"]["retryPolicy"]["automaticScheduled"])
            self.assertTrue(run["result"]["parentDecision"]["required"])
            self.assertTrue(
                run["result"]["parentDecision"]["parentSessionRemainsRunnable"]
            )
            self.assertEqual(
                run["result"]["parentDecision"]["allowedActions"],
                ["continue_parent", "retry", "resume", "switch_model", "redelegate"],
            )

            batches = coordinator.store.list_batches(limit=10)
            self.assertEqual(len(batches), 1, "Pi exhaustion must not trigger a second kernel retry")
            self.assertNotEqual(
                self.sessions.get(str(self.parent["id"]))["status"],
                "faulted",
            )

            materialized = self.context_runtime.materialize(str(self.parent["id"]))
            payload = next(
                item["payload"]
                for item in materialized["items"]
                if item["sourceId"] == run_id
            )
            self.assertEqual(payload["state"], "failed")
            self.assertEqual(payload["deliveryStatus"], "not_returned")
            self.assertTrue(payload["failureReport"]["retryExhausted"])
            self.assertTrue(payload["parentDecision"]["required"])
            self.assertIn("不要把父级标记为失败", payload["instruction"])

            def failed_progress_events() -> list[object]:
                events, _ = self.events.replay(str(self.parent["id"]))
                return [
                    event
                    for event in events
                    if event.event_type == "tool_progress"
                    and event.payload.get("runId") == run_id
                    and event.payload.get("state") == "failed"
                ]

            _wait_until(lambda: len(failed_progress_events()) == 1)
            events, gap = self.events.replay(str(self.parent["id"]))
            self.assertFalse(gap)
            progress = next(
                event.payload
                for event in reversed(events)
                if event.event_type == "tool_progress"
                and event.payload.get("runId") == run_id
                and event.payload.get("state") == "failed"
            )
            self.assertTrue(progress["parentDecisionRequired"])
            self.assertIn("等待上级继续决策或恢复", progress["summary"])
        finally:
            coordinator.close()

    def test_configured_model_routes_apply_by_delegated_runtime_role(self) -> None:
        routes = {
            "toolAgent": {
                "modelProfile": "openai-codex/gpt-5.6-luna",
                "thinkingLevel": "low",
            },
            "subagent": {
                "modelProfile": "openai-codex/gpt-5.6-terra",
                "thinkingLevel": "high",
            },
        }
        coordinator = self.coordinator(
            model_route_provider=lambda route_id: routes.get(route_id)
        )
        try:
            prepared_runs: list[dict[str, object]] = []
            original_create_batch = coordinator.store.create_batch

            def capture_create_batch(*args, **kwargs):
                prepared_runs.extend(kwargs["runs"])
                return original_create_batch(*args, **kwargs)

            self.parent = self.sessions.set_runtime_policy(
                str(self.parent["id"]),
                mode="coordinator",
                tool_profile_version="control-center-v1",
                execution_mode="workspace_managed",
                grant_workspace_scope=True,
                allowed_tools=None,
                workspace_roots=[str(self.root)],
            )
            with patch.object(
                coordinator.store,
                "create_batch",
                side_effect=capture_create_batch,
            ):
                read_only, writable = coordinator.delegate(
                    str(self.parent["id"]),
                    {
                        "wait": True,
                        "tasks": [
                            {
                                "agent": "researcher",
                                "task": "只读研究",
                                "modelProfile": "openai-codex/explicit-read-model",
                                "thinkingLevel": "medium",
                                **_TASK_CONTRACT,
                            },
                            {
                                "agent": "worker",
                                "task": "有界实现",
                                "access": "write",
                                "modelProfile": "openai-codex/explicit-write-model",
                                "thinkingLevel": "medium",
                                **_TASK_CONTRACT,
                            },
                        ],
                    },
                )["batch"]["runs"]
            read_only_child = self.sessions.get(str(read_only["childSessionId"]))
            writable_child = self.sessions.get(str(writable["childSessionId"]))
            self.assertEqual(
                read_only_child["modelProfile"],
                "openai-codex/gpt-5.6-terra",
            )
            self.assertEqual(read_only_child["thinkingLevel"], "high")
            self.assertEqual(
                writable_child["modelProfile"],
                "openai-codex/gpt-5.6-luna",
            )
            self.assertEqual(writable_child["thinkingLevel"], "low")
            self.assertEqual(
                prepared_runs[0]["launchDigest"]["modelRoute"],
                "subagent",
            )
            self.assertEqual(
                prepared_runs[0]["launchDigest"]["modelRouteSource"],
                "configured",
            )
            self.assertEqual(
                prepared_runs[1]["launchDigest"]["modelRoute"],
                "toolAgent",
            )
            self.assertEqual(
                prepared_runs[1]["launchDigest"]["modelRouteSource"],
                "configured",
            )
        finally:
            coordinator.close()

    def test_inherit_model_route_allows_delegated_task_override(self) -> None:
        coordinator = self.coordinator(
            model_route_provider=lambda _route_id: {
                "modelProfile": "inherit",
                "thinkingLevel": "inherit",
            }
        )
        try:
            run = coordinator.delegate(
                str(self.parent["id"]),
                {
                    "agent": "researcher",
                    "task": "使用显式回退路由",
                    "modelProfile": "openai-codex/gpt-5.6-luna",
                    "thinkingLevel": "medium",
                    "wait": True,
                    **_TASK_CONTRACT,
                },
            )["batch"]["runs"][0]
            child = self.sessions.get(str(run["childSessionId"]))
            self.assertEqual(child["modelProfile"], "openai-codex/gpt-5.6-luna")
            self.assertEqual(child["thinkingLevel"], "medium")
        finally:
            coordinator.close()

    def test_first_terminal_result_is_ingested_once_and_late_terminal_is_ignored(self) -> None:
        coordinator = self.coordinator(_DuplicateTerminalRuntime)
        try:
            response = coordinator.delegate(
                str(self.parent["id"]),
                {
                    "agent": "reviewer",
                    "task": "验证首个终态结果",
                    **_TASK_CONTRACT,
                    "wait": False,
                },
            )
            run_id = str(response["batch"]["runs"][0]["id"])

            def terminal_parent_progress() -> list[Mapping[str, object]]:
                events, gap = self.events.replay(str(self.parent["id"]))
                self.assertFalse(gap)
                return [
                    event.payload
                    for event in events
                    if event.event_type == "tool_progress"
                    and event.payload.get("runId") == run_id
                    and event.payload.get("state") == "completed"
                ]

            _wait_until(
                lambda: coordinator.store.get_run(run_id)["resultContextScheduledAtMs"]
                is not None,
                timeout=10.0,
            )
            _wait_until(lambda: len(terminal_parent_progress()) == 1)
            run = coordinator.store.get_run(run_id)
            self.assertEqual(run["state"], "completed")
            self.assertIn("首个终态前的唯一结果", run["result"]["summary"])
            self.assertNotIn("迟到结果", run["result"]["summary"])
            self.assertEqual(run["usage"]["turnCount"], 1)
            self.assertEqual(run["usage"]["totalTokens"], 17)
            terminal_events = [
                event["eventType"]
                for event in coordinator.store.list_events(run_id)
                if event["eventType"] in {"completed", "failed", "aborted", "timed_out"}
            ]
            self.assertEqual(terminal_events, ["completed"])
            context_items = [
                item
                for item in self.context_runtime.list_items(str(self.parent["id"]))
                if item["sourceKind"] == "subagent_result"
                and item["sourceId"] == run_id
            ]
            self.assertEqual(len(context_items), 1)
            materialized = self.context_runtime.materialize(str(self.parent["id"]))
            payload = next(
                item["payload"]
                for item in materialized["items"]
                if item["itemId"] == context_items[0]["itemId"]
            )
            self.assertEqual(payload["verificationStatus"], "unverified")
            snapshot = coordinator.artifacts.snapshot(
                owner_kind="subagent_run",
                owner_id=run_id,
            )
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(
                snapshot["runtimeCheckpoint"]["terminalState"],
                "completed",
            )
            self.assertNotIn("迟到结果", json.dumps(snapshot, ensure_ascii=False))
        finally:
            coordinator.close()

    def test_cancelled_run_rejects_late_success_and_projects_one_failed_result(self) -> None:
        self.events.publish(str(self.parent["id"]), "tool_started", {
            "toolName": "agents", "toolCallId": "parent:cancelled-child",
        }, turn_id="parent:cancel-turn")
        with patch(
            "rag_ime.agent_delegation.agent_template",
            side_effect=_small_budget_template,
        ):
            coordinator = self.coordinator(_LateCompletionAfterCancelRuntime)
            try:
                response = coordinator.delegate(
                    str(self.parent["id"]),
                    {
                        "agent": "worker",
                        "task": "触发取消后迟到成功",
                        "_toolCallId": "parent:cancelled-child",
                        **_TASK_CONTRACT,
                        "wait": False,
                    },
                )
                run_id = str(response["batch"]["runs"][0]["id"])

                def failed_parent_progress() -> list[Mapping[str, object]]:
                    events, gap = self.events.replay(str(self.parent["id"]))
                    self.assertFalse(gap)
                    return [
                        event.payload
                        for event in events
                        if event.event_type == "tool_progress"
                        and event.payload.get("runId") == run_id
                        and event.payload.get("state") == "failed"
                    ]

                _wait_until(
                    lambda: coordinator.store.get_run(run_id, hydrate_artifacts=False)["resultContextScheduledAtMs"]
                    is not None
                )
                _wait_until(lambda: len(failed_parent_progress()) == 1)
                progress = failed_parent_progress()[0]
                self.assertEqual(progress["parentToolCallId"], "parent:cancelled-child")
                self.assertEqual(progress["parentTurnId"], "parent:cancel-turn")
                self.assertEqual(progress["childProgress"]["phase"], "terminal")
                self.assertEqual(progress["childProgress"]["state"], "failed")
                run = coordinator.store.get_run(run_id)
                self.assertEqual(run["state"], "failed")
                self.assertEqual(run["error"], "output budget exceeded")
                self.assertEqual(
                    run["result"]["deliveryStatus"],
                    "not_returned",
                )
                self.assertEqual(
                    run["result"]["verificationStatus"],
                    "not_applicable",
                )
                self.assertEqual(
                    run["result"]["failureClass"],
                    "logic_error",
                )
                self.assertFalse(
                    run["result"]["retryPolicy"]["automaticEligible"]
                )
                self.assertEqual(run["usage"]["turnCount"], 0)
                self.assertEqual(run["usage"]["totalTokens"], 0)
                terminal_events = [
                    event["eventType"]
                    for event in coordinator.store.list_events(run_id)
                    if event["eventType"] in {"completed", "failed", "aborted", "timed_out"}
                ]
                self.assertEqual(terminal_events, ["failed"])
                context_items = [
                    item
                    for item in self.context_runtime.list_items(str(self.parent["id"]))
                    if item["sourceKind"] == "subagent_result"
                    and item["sourceId"] == run_id
                ]
                self.assertEqual(len(context_items), 1)
                materialized = self.context_runtime.materialize(str(self.parent["id"]))
                payload = next(
                    item["payload"]
                    for item in materialized["items"]
                    if item["itemId"] == context_items[0]["itemId"]
                )
                self.assertEqual(payload["state"], "failed")
                self.assertEqual(payload["verificationStatus"], "not_applicable")
                snapshot = coordinator.artifacts.snapshot(
                    owner_kind="subagent_run",
                    owner_id=run_id,
                )
                self.assertIsNotNone(snapshot)
                assert snapshot is not None
                self.assertEqual(
                    snapshot["runtimeCheckpoint"]["terminalState"],
                    "failed",
                )
                self.assertNotIn(
                    "取消后的迟到成功",
                    json.dumps(snapshot, ensure_ascii=False),
                )
            finally:
                coordinator.close()

    def test_todo_backed_delegation_stays_independent_until_explicitly_linked(self) -> None:
        session_id = str(self.parent["id"])
        todo_task = "核对子 Agent 证据"
        self.sessions.mutate_agent_todo(
            session_id,
            {
                "op": "init",
                "list": [{"phase": "验证", "items": [todo_task]}],
            },
        )
        self.sessions.mutate_agent_todo(
            session_id,
            {"op": "start", "task": todo_task},
        )
        coordinator = self.coordinator()
        try:
            independent = coordinator.delegate(
                session_id,
                {"agent": "researcher", "task": "不依赖当前 Todo", "wait": True, **_TASK_CONTRACT},
            )["batch"]["runs"][0]
            self.assertEqual(independent["todoTask"], "")
            self.assertEqual(independent["todoPhase"], "")
            with self.assertRaisesRegex(ValueError, "does not belong"):
                coordinator.delegate(
                    session_id,
                    {
                        "agent": "researcher",
                        "task": "错误 Todo 关联",
                        **_TASK_CONTRACT,
                        "todoTask": "其他不存在的任务",
                    },
                )

            batch = coordinator.delegate(
                session_id,
                {
                    "agent": "researcher",
                    "task": "返回证据供主持会话核验",
                    **_TASK_CONTRACT,
                    "todoTask": todo_task,
                    "wait": True,
                },
            )["batch"]
            run = batch["runs"][0]
            self.assertEqual(run["todoTask"], todo_task)
            self.assertEqual(run["todoPhase"], "验证")
            self.assertEqual(run["state"], "completed")
            self.assertEqual(
                self.sessions.agent_todo(session_id)["phases"][0]["tasks"][0]["status"],
                "in_progress",
            )

            def linked_terminal_progress() -> list[Mapping[str, object]]:
                parent_events, gap = self.events.replay(session_id)
                self.assertFalse(gap)
                return [
                    event.payload
                    for event in parent_events
                    if event.event_type == "tool_progress"
                    and event.payload.get("runId") == run["id"]
                    and event.payload.get("state") == "completed"
                ]

            _wait_until(lambda: bool(linked_terminal_progress()))
            terminal_progress = linked_terminal_progress()
            self.assertEqual(terminal_progress[-1]["todoTask"], todo_task)
            self.assertEqual(terminal_progress[-1]["todoPhase"], "验证")
            self.assertTrue(terminal_progress[-1]["requiresParentTodoUpdate"])
        finally:
            coordinator.close()

    def test_delegation_rejects_provider_invalid_nested_output_schema_before_launch(self) -> None:
        coordinator = self.coordinator()
        try:
            with self.assertRaisesRegex(ValueError, r"outputSchema.*items"):
                coordinator.delegate(
                    str(self.parent["id"]),
                    {
                        "agent": "researcher",
                        "task": "返回数组结果",
                        **_TASK_CONTRACT,
                        "outputSchema": {"type": "array", "items": []},
                    },
                )
            self.assertEqual(
                coordinator.store.list_batches(parent_session_id=str(self.parent["id"])),
                [],
            )
        finally:
            coordinator.close()

    def test_delegation_rejects_non_object_output_schema_before_launch(self) -> None:
        coordinator = self.coordinator()
        try:
            for output_schema in (["buildCommands", "testCommands", "configFiles"], False):
                with self.subTest(output_schema=output_schema):
                    with self.assertRaisesRegex(ValueError, "outputSchema must be a JSON Schema object"):
                        coordinator.delegate(
                            str(self.parent["id"]),
                            {
                                "agent": "researcher",
                                "task": "返回构建信息",
                                **_TASK_CONTRACT,
                                "outputSchema": output_schema,
                            },
                        )
            self.assertEqual(
                coordinator.store.list_batches(parent_session_id=str(self.parent["id"])),
                [],
            )
        finally:
            coordinator.close()

    def test_model_claim_without_receipts_is_returned_but_never_accepted(self) -> None:
        coordinator = self.coordinator(_ClaimingWithoutEvidenceRuntime)
        try:
            response = coordinator.delegate(
                str(self.parent["id"]),
                {
                    "agent": "worker",
                    "task": "声称完成但不提供任何工具证据",
                    **_TASK_CONTRACT,
                    "wait": True,
                },
            )

            self.assertEqual(response["acceptanceScope"], "delegation_request")
            run = response["batch"]["runs"][0]
            self.assertEqual(run["state"], "completed")
            self.assertEqual(run["result"]["deliveryStatus"], "returned")
            self.assertEqual(run["result"]["verificationStatus"], "unverified")
            self.assertEqual(run["result"]["authority"], "evidence_only")
            self.assertIn("已修复并通过验收", run["result"]["summary"])
            self.assertNotIn("evidenceRefs", run["result"])
            self.assertNotIn("artifactRefs", run["result"])

            def progress_summaries() -> list[str]:
                parent_events, gap = self.events.replay(str(self.parent["id"]))
                self.assertFalse(gap)
                return [
                    str(event.payload.get("summary") or "")
                    for event in parent_events
                    if event.event_type == "tool_progress"
                ]

            _wait_until(
                lambda: "子 Agent 已返回结果，待主持会话核验"
                in progress_summaries()
            )
            summaries = progress_summaries()
            self.assertIn("子 Agent 已返回结果，待主持会话核验", summaries)
            self.assertNotIn("子 Agent 已完成", summaries)
        finally:
            coordinator.close()

    def test_paused_or_exhausted_parent_goal_blocks_new_delegation(self) -> None:
        goal = self.sessions.mutate_agent_goal(
            str(self.parent["id"]),
            {
                "action": "confirm_setup",
                "confirmed": True,
                "expectedRevision": 0,
                "objective": "在预算内完成委派",
                "tokenBudget": 5,
            },
        )["workflow"]["goal"]
        paused = self.sessions.mutate_agent_goal(
            str(self.parent["id"]),
            {"action": "pause", "expectedRevision": goal["revision"]},
        )["workflow"]["goal"]
        coordinator = self.coordinator()
        with self.assertRaisesRegex(ValueError, "goal_paused"):
            coordinator.delegate(
                str(self.parent["id"]),
                {"agent": "worker", "task": "不应启动", "contextMode": "fresh", **_TASK_CONTRACT},
            )
        resumed = self.sessions.mutate_agent_goal(
            str(self.parent["id"]),
            {"action": "resume", "expectedRevision": paused["revision"]},
        )["workflow"]["goal"]
        self.sessions.record_agent_goal_usage(
            str(self.parent["id"]),
            idempotency_key="delegate-gate:usage",
            turn_id="turn:delegate-gate",
            event_id="event:delegate-gate",
            token_delta=5,
            elapsed_delta_ms=0,
        )
        with self.assertRaisesRegex(ValueError, "goal_budget_exhausted"):
            coordinator.delegate(
                str(self.parent["id"]),
                {"agent": "worker", "task": "仍不应启动", "contextMode": "fresh", **_TASK_CONTRACT},
            )
        self.assertGreaterEqual(resumed["revision"], goal["revision"])
        coordinator.close()

    def test_default_templates_do_not_stop_on_fixed_turn_or_tool_counts(self) -> None:
        coordinator = self.coordinator(_ManyTurnsAndToolsRuntime)
        batch = coordinator.delegate(
            str(self.parent["id"]),
            {"agent": "researcher", "task": "完成需要多轮检索的任务", "wait": True, **_TASK_CONTRACT},
        )["batch"]

        run = batch["runs"][0]
        self.assertEqual(run["state"], "completed")
        self.assertEqual(run["budget"]["maxTurns"], 0)
        self.assertEqual(run["budget"]["maxToolCalls"], 0)
        self.assertEqual(run["usage"]["turnCount"], 12)
        self.assertEqual(run["usage"]["toolCount"], 20)
        self.assertEqual(run["supervision"]["phase"], "none")
        coordinator.close()

    def test_artifact_inspection_requires_the_owning_parent_session(self) -> None:
        coordinator = self.coordinator()
        batch = coordinator.delegate(
            str(self.parent["id"]),
            {"agent": "reviewer", "task": "检查受控 Artifact", "wait": True, **_TASK_CONTRACT},
        )["batch"]
        artifact_id = str(batch["runs"][0]["artifact"]["artifactId"])
        other = self.sessions.create(title="其他主持会话")

        inspected = coordinator.inspect_artifact(
            str(self.parent["id"]),
            artifact_id,
            limit=3,
        )

        self.assertEqual(inspected["artifact"]["artifactId"], artifact_id)
        self.assertLessEqual(inspected["returnedRecords"], 3)
        with self.assertRaisesRegex(ValueError, "does not belong"):
            coordinator.inspect_artifact(str(other["id"]), artifact_id)
        coordinator.close()

    def test_subagent_permissions_are_intersected_with_explicit_parent_allowlist(self) -> None:
        self.parent = self.sessions.set_runtime_policy(
            str(self.parent["id"]),
            mode="assistant",
            tool_profile_version="control-center-v1",
            allowed_tools=["overview", "memory"],
        )
        coordinator = self.coordinator()
        batch = coordinator.delegate(
            str(self.parent["id"]),
            {"agent": "worker", "task": "只能使用父会话允许的工具", "wait": True, **_TASK_CONTRACT},
        )["batch"]
        child = self.sessions.get(str(batch["runs"][0]["childSessionId"]))

        self.assertEqual(child["toolProfileVersion"], "subagent-worker-v1")
        self.assertEqual(child["toolAllowlistMode"], "explicit")
        self.assertEqual(child["allowedTools"], ["overview", "memory"])
        coordinator.close()

    def test_parent_can_spawn_a_configured_writable_tool_agent_session(self) -> None:
        parent_id = str(self.parent["id"])
        self.parent = self.sessions.set_runtime_policy(
            parent_id,
            mode="coordinator",
            tool_profile_version="control-center-v1",
            execution_mode="workspace_managed",
            grant_workspace_scope=True,
            allowed_tools=None,
            pi_skills_enabled=True,
            codex_skills_enabled=False,
            workspace_roots=[str(self.root)],
        )
        coordinator = self.coordinator()
        try:
            batch = coordinator.delegate(
                parent_id,
                {
                    "agent": "worker",
                    "task": "在共享工作区完成一个有界修复",
                    "modelProfile": "openai-codex/gpt-5.6-luna",
                    "thinkingLevel": "low",
                    "access": "write",
                    "allowedTools": ["workspace_read", "workspace_patch"],
                    "piSkillsEnabled": True,
                    "codexSkillsEnabled": False,
                    "wait": True,
                    **_TASK_CONTRACT,
                },
            )["batch"]
            child = self.sessions.get(str(batch["runs"][0]["childSessionId"]))

            self.assertEqual(child["mode"], "coordinator")
            self.assertEqual(child["modelProfile"], "openai-codex/gpt-5.6-luna")
            self.assertEqual(child["thinkingLevel"], "low")
            self.assertEqual(child["toolProfileVersion"], "subagent-worker-v1")
            self.assertEqual(child["executionMode"], "workspace_managed")
            self.assertEqual(child["workspaceRoots"], [str(self.root.resolve())])
            self.assertEqual(
                child["allowedTools"],
                ["workspace_read", "workspace_patch"],
            )
            self.assertTrue(child["piSkillsEnabled"])
            self.assertFalse(child["codexSkillsEnabled"])
        finally:
            coordinator.close()

    def test_retention_defaults_to_72_hours_and_gc_runs_on_startup(self) -> None:
        self.config.session_dir.mkdir(parents=True)
        child_file = self.config.session_dir / "expired-on-startup.jsonl"
        _write_jsonl(
            child_file,
            _session_records(session_id="expired-child", cwd=self.config.agent_dir),
            mode=0o600,
        )
        child = self.sessions.create(
            title="过期子任务",
            session_kind="subagent_runtime",
        )
        self.sessions.bind_runtime_session(
            str(child["id"]),
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="expired-child",
            transcript_ref=str(child_file),
            binding_state="prepared",
        )
        store = AgentDelegationStore(self.db_path)
        store.initialize()
        batch = store.create_batch(
            parent_session_id=str(self.parent["id"]),
            parent_run_id="",
            context_mode="fresh",
            depth=1,
            max_depth=2,
            runs=[_run_spec(str(child["id"]), task="启动时回收")],
            created_at_ms=1_000,
        )
        run_id = str(batch["runs"][0]["id"])
        store.start_run(run_id, started_at_ms=1_100)
        store.finish_run(
            run_id,
            state="completed",
            result={"summary": "持久结果"},
            completed_at_ms=1_200,
        )

        coordinator = self.coordinator(
            subagent_session_retention_ms=1_000,
            subagent_session_gc_interval_ms=60_000,
        )

        default_db = self.root / "default-retention.sqlite"
        default_sessions = AgentSessionStore(default_db)
        default_sessions.initialize()
        default_context_runtime = AgentContextRuntime(default_db)
        default_context_runtime.initialize()
        with patch.dict(
            "os.environ",
            {"RAG_IME_SUBAGENT_SESSION_RETENTION_HOURS": "72"},
        ):
            default_coordinator = AgentDelegationCoordinator(
                db_path=default_db,
                runtime_config=self.config,
                sessions=default_sessions,
                events=AgentEventHub(),
                context_runtime=default_context_runtime,
                runtime_factory=_CompletingRuntime,
            )
        self.assertEqual(
            72 * 60 * 60 * 1_000,
            default_coordinator._subagent_session_retention_ms,
        )
        default_coordinator.close()
        with self.assertRaises(KeyError):
            self.sessions.get(str(child["id"]))
        self.assertFalse(child_file.exists())
        self.assertEqual(coordinator.store.get_run(run_id)["result"]["summary"], "持久结果")
        coordinator.close()

    def test_nested_delegation_stops_at_depth_two(self) -> None:
        coordinator = self.coordinator()
        first = coordinator.delegate(
            str(self.parent["id"]),
            {"agent": "delegate", "task": "第一层", "wait": True, **_TASK_CONTRACT},
        )["batch"]
        first_child = first["runs"][0]["childSessionId"]
        second = coordinator.delegate(
            str(first_child),
            {"agent": "reviewer", "task": "第二层", "wait": True, **_TASK_CONTRACT},
        )["batch"]
        self.assertEqual(second["depth"], 2)
        tree = coordinator.status(str(self.parent["id"]), {})["tree"]
        self.assertEqual(tree["nodeCount"], 2)
        self.assertEqual(tree["maxDepth"], 2)
        self.assertEqual(tree["roots"][0]["run"]["id"], first["runs"][0]["id"])
        self.assertEqual(
            tree["roots"][0]["children"][0]["run"]["id"],
            second["runs"][0]["id"],
        )
        second_child = second["runs"][0]["childSessionId"]
        with self.assertRaisesRegex(ValueError, "maximum depth is 2"):
            coordinator.delegate(
                str(second_child),
                {"agent": "reviewer", "task": "第三层", **_TASK_CONTRACT},
            )
        coordinator.close()

    def test_live_subagents_can_call_each_other_directly_within_one_tree(self) -> None:
        _InteractiveRuntime.instances.clear()
        coordinator = self.coordinator(_InteractiveRuntime)
        response = coordinator.delegate(
            str(self.parent["id"]),
            {
                "contextMode": "fresh",
                "wait": False,
                "tasks": [
                    {"agent": "researcher", "task": "调查问题", **_TASK_CONTRACT},
                    {"agent": "reviewer", "task": "复核发现", **_TASK_CONTRACT},
                ],
            },
        )
        first, second = response["batch"]["runs"]
        first_run_id = str(first["id"])
        second_run_id = str(second["id"])
        first_session_id = str(first["childSessionId"])
        second_session_id = str(second["childSessionId"])
        _wait_until(lambda: len(coordinator._active_runs) == 2)

        status = coordinator.status(first_session_id, {})
        self.assertIn(
            second_run_id,
            {str(item["runId"]) for item in status["peers"]},
        )
        sent = coordinator.call(
            first_session_id,
            {
                "targetRunId": second_run_id,
                "message": "请立即核对接口边界。",
                "_toolCallId": "tool:peer-a-to-b",
            },
        )
        self.assertEqual(sent["targetSessionId"], second_session_id)
        self.assertEqual(sent["delivery"], "steer")
        target_runtime = next(
            item for item in _InteractiveRuntime.instances if item.session_id == second_session_id
        )
        self.assertIn(
            ("steer", f"[子 Agent {first_run_id} 直接调用]\n请立即核对接口边界。"),
            target_runtime.deliveries,
        )

        replied = coordinator.call(
            second_session_id,
            {
                "targetRunId": first_run_id,
                "message": "已核对，边界成立。",
                "_toolCallId": "tool:peer-b-to-a",
            },
        )
        self.assertEqual(replied["targetSessionId"], first_session_id)
        source_runtime = next(
            item for item in _InteractiveRuntime.instances if item.session_id == first_session_id
        )
        self.assertIn(
            ("steer", f"[子 Agent {second_run_id} 直接调用]\n已核对，边界成立。"),
            source_runtime.deliveries,
        )

        outsider = self.sessions.create(title="另一个委派树")
        with self.assertRaisesRegex(ValueError, "does not belong"):
            coordinator.call(
                str(outsider["id"]),
                {"targetRunId": second_run_id, "message": "越权调用"},
            )
        coordinator.abort(str(self.parent["id"]), {"batchId": response["batch"]["id"]})
        coordinator.close()

    def test_fork_accepts_only_native_0600_session_and_preserves_safe_thinking(self) -> None:
        self.config.session_dir.mkdir(parents=True)
        parent_file = self.config.session_dir / "parent.jsonl"
        parent_records = _session_records(
            session_id="parent-pi",
            cwd=self.config.agent_dir,
            assistant_content=[
                {"type": "thinking", "thinking": "普通思考可以保留"},
                {"type": "text", "text": "公开结论"},
            ],
        )
        _write_jsonl(parent_file, parent_records)
        self.parent = self.sessions.prepare_session_file(
            str(self.parent["id"]),
            pi_session_id="parent-pi",
            session_file=str(parent_file),
        )
        child_file = self.config.session_dir / "native-child.jsonl"
        child_records = [dict(item) for item in parent_records]
        child_records[0] = {
            **child_records[0],
            "id": "child-pi",
            "parentSession": str(parent_file),
        }
        child_records[2] = {
            **child_records[2],
            "message": {
                "role": "assistant",
                "provider": "anthropic",
                "content": [
                    {"type": "thinking", "thinking": "普通思考可以保留"},
                    {"type": "text", "text": "公开结论"},
                ],
            },
        }
        _write_jsonl(child_file, child_records, mode=0o600)
        _ForkInspectingRuntime.snapshots.clear()
        coordinator = self.coordinator(_ForkInspectingRuntime)
        batch = coordinator.delegate(
            str(self.parent["id"]),
            {
                "agent": "planner",
                "task": "继承讨论并规划",
                **_TASK_CONTRACT,
                "contextMode": "fork",
                "wait": True,
                "_runtimeContext": _fork_context(
                    parent_file=parent_file,
                    child_file=child_file,
                    child_session_id="child-pi",
                ),
            },
        )["batch"]
        child = self.sessions.get(str(batch["runs"][0]["childSessionId"]))
        self.assertEqual(child["sessionKind"], "subagent_runtime")
        self.assertNotEqual(child["status"], "archived")
        self.assertEqual(Path(str(child["sessionFile"])).resolve(), child_file.resolve())
        self.assertNotEqual(child_file, parent_file)
        self.assertTrue(child_file.exists())
        self.assertEqual(len(_ForkInspectingRuntime.snapshots), 1)
        snapshot = _ForkInspectingRuntime.snapshots[0]
        self.assertEqual(Path(str(snapshot["path"])).resolve(), child_file.resolve())
        self.assertEqual(snapshot["mode"], 0o600)
        child_text = str(snapshot["text"])
        self.assertIn("已有上下文", child_text)
        self.assertIn("公开结论", child_text)
        self.assertIn("普通思考可以保留", child_text)
        self.assertEqual(json.loads(child_text.splitlines()[0])["id"], "child-pi")
        coordinator.close()

    def test_expired_runtime_session_is_retired_but_result_and_artifact_remain(self) -> None:
        self.config.session_dir.mkdir(parents=True)
        parent_file = self.config.session_dir / "retained-parent.jsonl"
        child_file = self.config.session_dir / "retained-child.jsonl"
        parent_records = _session_records(session_id="retained-parent", cwd=self.config.agent_dir)
        child_records = [dict(item) for item in parent_records]
        child_records[0] = {
            **child_records[0],
            "id": "retained-child",
            "parentSession": str(parent_file),
        }
        _write_jsonl(parent_file, parent_records)
        _write_jsonl(child_file, child_records, mode=0o600)
        self.parent = self.sessions.prepare_session_file(
            str(self.parent["id"]),
            pi_session_id="retained-parent",
            session_file=str(parent_file),
        )
        coordinator = self.coordinator(
            _ForkInspectingRuntime,
            subagent_session_retention_ms=1_000,
            subagent_session_gc_interval_ms=0,
        )
        batch = coordinator.delegate(
            str(self.parent["id"]),
            {
                "agent": "reviewer",
                "task": "保留后清理",
                **_TASK_CONTRACT,
                "contextMode": "fork",
                "wait": True,
                "_runtimeContext": _fork_context(
                    parent_file=parent_file,
                    child_file=child_file,
                    child_session_id="retained-child",
                ),
            },
        )["batch"]
        run = batch["runs"][0]
        child_session_id = str(run["childSessionId"])
        artifact_id = str(run["artifact"]["artifactId"])
        completed_at_ms = int(run["completedAtMs"])

        retained = self.sessions.get(child_session_id)
        self.assertNotEqual(retained["status"], "archived")
        self.assertTrue(child_file.exists())
        self.assertEqual(coordinator.collect_expired_sessions(now_ms=completed_at_ms + 999), 0)

        self.assertEqual(coordinator.collect_expired_sessions(now_ms=completed_at_ms + 1_000), 1)
        with self.assertRaises(KeyError):
            self.sessions.get(child_session_id)
        self.assertNotIn(
            child_session_id,
            {
                str(item["id"])
                for item in self.sessions.list(
                    include_archived=True, include_internal=True
                )
            },
        )
        self.assertFalse(child_file.exists())
        durable_run = coordinator.store.get_run(str(run["id"]))
        self.assertEqual(durable_run["result"]["summary"], run["result"]["summary"])
        inspected = coordinator.inspect_artifact(
            str(self.parent["id"]), artifact_id, limit=100
        )
        self.assertIn(
            "runtime_retired",
            {str(item["eventType"]) for item in inspected["records"]},
        )
        coordinator.close()

    def test_fork_rejects_missing_or_untrusted_runtime_context(self) -> None:
        self.config.session_dir.mkdir(parents=True)
        parent_file = self.config.session_dir / "parent.jsonl"
        parent_records = _session_records(session_id="parent-pi", cwd=self.config.agent_dir)
        _write_jsonl(parent_file, parent_records)
        self.parent = self.sessions.prepare_session_file(
            str(self.parent["id"]),
            pi_session_id="parent-pi",
            session_file=str(parent_file),
        )
        coordinator = self.coordinator()
        with self.assertRaisesRegex(ValueError, "active Pi runtime"):
            coordinator.delegate(
                str(self.parent["id"]),
                {"agent": "planner", "task": "不能手工分叉", "contextMode": "fork", **_TASK_CONTRACT},
            )

        child_file = self.config.session_dir / "forged-child.jsonl"
        child_records = _session_records(
            session_id="child-pi",
            cwd=self.config.agent_dir,
            parent_session=self.config.session_dir / "someone-else.jsonl",
        )
        _write_jsonl(child_file, child_records, mode=0o600)
        with self.assertRaisesRegex(ValueError, "does not reference the active parent"):
            coordinator.delegate(
                str(self.parent["id"]),
                {
                    "agent": "planner",
                    "task": "拒绝伪造分叉",
                    **_TASK_CONTRACT,
                    "contextMode": "fork",
                    "_runtimeContext": _fork_context(
                        parent_file=parent_file,
                        child_file=child_file,
                        child_session_id="child-pi",
                    ),
                },
            )

        unsafe_file = self.config.session_dir / "unsafe-child.jsonl"
        unsafe_records = _session_records(
            session_id="unsafe-pi",
            cwd=self.config.agent_dir,
            parent_session=parent_file,
            assistant_content=[
                {
                    "type": "thinking",
                    "thinking": "不能跨会话复用",
                    "signature": "provider-bound-signature",
                }
            ],
        )
        unsafe_records[2]["message"]["provider"] = "anthropic"
        _write_jsonl(unsafe_file, unsafe_records, mode=0o600)
        with self.assertRaisesRegex(ValueError, "provider-bound thinking"):
            coordinator.delegate(
                str(self.parent["id"]),
                {
                    "agent": "planner",
                    "task": "拒绝不安全思考块",
                    **_TASK_CONTRACT,
                    "contextMode": "fork",
                    "_runtimeContext": _fork_context(
                        parent_file=parent_file,
                        child_file=unsafe_file,
                        child_session_id="unsafe-pi",
                    ),
                },
            )
        coordinator.close()

    def test_global_parallel_limit_and_abort_stop_both_runs(self) -> None:
        coordinator = self.coordinator(_HangingRuntime)
        response = coordinator.delegate(
            str(self.parent["id"]),
            {
                "wait": False,
                "tasks": [
                    {"agent": "researcher", "task": "长任务一", **_TASK_CONTRACT},
                    {"agent": "reviewer", "task": "长任务二", **_TASK_CONTRACT},
                ],
            },
        )
        batch = response["batch"]
        _wait_until(lambda: coordinator.store.active_run_count() == 2)
        self.assertEqual(
            [item["id"] for item in self.sessions.list(include_archived=True)],
            [self.parent["id"]],
        )
        live_internal = self.sessions.list(include_archived=True, include_internal=True)
        self.assertEqual(len(live_internal), 3)
        self.assertTrue(
            all(
                item["sessionKind"] == "subagent_runtime"
                for item in live_internal
                if item["id"] != self.parent["id"]
            )
        )
        with self.assertRaisesRegex(ValueError, "at most two"):
            coordinator.delegate(
                str(self.parent["id"]),
                {"agent": "planner", "task": "第三个并行任务", "wait": False, **_TASK_CONTRACT},
            )
        receipt = coordinator.abort(
            str(self.parent["id"]),
            {"batchId": batch["id"]},
        )
        self.assertEqual(receipt["cancellation"]["state"], "terminated")
        self.assertEqual(receipt["cancellation"]["pendingRunIds"], [])
        final = receipt["batch"]
        self.assertTrue(final["abortRequested"])
        self.assertEqual({run["state"] for run in final["runs"]}, {"aborted"})
        _wait_until(lambda: coordinator.store.active_run_count() == 0)
        retained = self.sessions.list(include_archived=True, include_internal=True)
        self.assertTrue(
            all(
                item["status"] != "archived"
                for item in retained
                if item["id"] != self.parent["id"]
            )
        )
        coordinator.close()

    def test_soft_budget_is_recorded_without_stopping_a_successful_run(self) -> None:
        with patch(
            "rag_ime.agent_delegation.agent_template",
            side_effect=_small_budget_template,
        ):
            coordinator = self.coordinator(_SoftBudgetRuntime)
            batch = coordinator.delegate(
                str(self.parent["id"]),
                {"agent": "reviewer", "task": "接近输出预算但仍完成", "wait": True, **_TASK_CONTRACT},
            )["batch"]

        run = batch["runs"][0]
        self.assertEqual(run["state"], "completed")
        self.assertEqual(run["supervision"]["phase"], "soft")
        records = coordinator.artifacts.lifecycle_records(
            owner_kind="subagent_run",
            owner_id=str(run["id"]),
        )
        self.assertIn("supervision_soft", {str(item["eventType"]) for item in records})
        coordinator.close()

    def test_hard_budget_uses_abort_then_forced_stop_after_grace(self) -> None:
        _IgnoringAbortRuntime.instances.clear()
        with patch(
            "rag_ime.agent_delegation.agent_template",
            side_effect=_small_budget_template,
        ):
            coordinator = self.coordinator(
                _IgnoringAbortRuntime,
                cancellation_grace_ms=20,
            )
            batch = coordinator.delegate(
                str(self.parent["id"]),
                {"agent": "worker", "task": "触发硬输出预算", "wait": True, **_TASK_CONTRACT},
            )["batch"]

        run = batch["runs"][0]
        runtime = _IgnoringAbortRuntime.instances[-1]
        self.assertEqual(run["state"], "failed")
        self.assertEqual(run["error"], "output budget exceeded")
        self.assertEqual(run["supervision"]["phase"], "forced")
        self.assertGreaterEqual(runtime.abort_count, 1)
        self.assertGreaterEqual(runtime.stop_count, 1)
        records = coordinator.artifacts.lifecycle_records(
            owner_kind="subagent_run",
            owner_id=str(run["id"]),
        )
        self.assertIn("supervision_forced", {str(item["eventType"]) for item in records})
        coordinator.close()

    def test_shared_runtime_hard_budget_aborts_child_without_stopping_host(self) -> None:
        shared = _IgnoringAbortRuntime(
            sessions=self.sessions,
            events=self.events,
        )
        factory = _CapturingRuntimeDriverFactory(self.config)
        with patch(
            "rag_ime.agent_delegation.agent_template",
            side_effect=_small_budget_template,
        ):
            coordinator = AgentDelegationCoordinator(
                db_path=self.db_path,
                runtime_config=self.config,
                sessions=self.sessions,
                events=self.events,
                context_runtime=self.context_runtime,
                runtime_driver_factory=factory,
                runtime_provider=lambda: shared,
                cancellation_grace_ms=20,
            )
            batch = coordinator.delegate(
                str(self.parent["id"]),
                {"agent": "worker", "task": "共享 Host 子会话超预算", "wait": True, **_TASK_CONTRACT},
            )["batch"]

        run = batch["runs"][0]
        self.assertEqual("failed", run["state"])
        self.assertEqual("output budget exceeded", run["error"])
        self.assertEqual("forced", run["supervision"]["phase"])
        self.assertGreaterEqual(shared.abort_count, 1)
        self.assertEqual(0, shared.stop_count)
        self.assertEqual([], factory.contexts)
        coordinator.close()
        self.assertEqual(0, shared.stop_count)

    def test_restart_reconciles_terminal_artifact_checkpoint_without_reprompting(self) -> None:
        artifacts = AgentArtifactStore(self.db_path)
        store = AgentDelegationStore(self.db_path, artifacts=artifacts)
        store.initialize()
        child = self.sessions.create(
            title="待恢复子任务",
            role_id="companion-firstlight-v1",
            role_version="1",
            model_profile="gpt/test-model",
            session_kind="subagent_runtime",
        )
        batch = store.create_batch(
            parent_session_id=str(self.parent["id"]),
            parent_run_id="",
            context_mode="fresh",
            depth=1,
            max_depth=2,
            runs=[_run_spec(str(child["id"]), task="恢复完成态")],
        )
        run_id = str(batch["runs"][0]["id"])
        store.start_run(run_id)
        message = _assistant_message(str(child["id"]), "turn:recovered", "恢复后的结果")
        event = self.events.publish(
            str(child["id"]),
            "turn_completed",
            {"status": "completed"},
            turn_id="turn:recovered",
        )
        store.checkpoint_runtime_event(
            run_id,
            event,
            {
                "terminalState": "completed",
                "terminalAtMs": event.created_at_ms,
                "lastMessage": message,
                "usage": {"turnCount": 1, "toolCount": 0, "totalTokens": 42},
            },
        )

        coordinator = self.coordinator(_CompletingRuntime)
        recovered = coordinator.store.get_batch(str(batch["id"]))
        run = recovered["runs"][0]
        self.assertEqual(recovered["state"], "completed")
        self.assertTrue(run["result"]["recovered"])
        self.assertEqual(run["result"]["deliveryStatus"], "returned")
        self.assertEqual(run["result"]["verificationStatus"], "unverified")
        self.assertEqual(run["result"]["authority"], "evidence_only")
        self.assertEqual(run["usage"]["totalTokens"], 42)
        self.assertNotIn(run_id, coordinator._threads)
        retained = self.sessions.get(str(child["id"]))
        self.assertEqual(retained["sessionKind"], "subagent_runtime")
        self.assertNotEqual(retained["status"], "archived")
        coordinator.close()

    def test_restart_relaunches_queued_work_and_can_resume_interrupted_running_work(self) -> None:
        artifacts = AgentArtifactStore(self.db_path)
        store = AgentDelegationStore(self.db_path, artifacts=artifacts)
        store.initialize()
        queued_child = self.sessions.create(title="安全重放 queued")
        queued = store.create_batch(
            parent_session_id=str(self.parent["id"]),
            parent_run_id="",
            context_mode="fresh",
            depth=1,
            max_depth=2,
            runs=[_run_spec(str(queued_child["id"]), task="重放 queued")],
        )
        running_child = self.sessions.create(title="拒绝猜测 running")
        running = store.create_batch(
            parent_session_id=str(self.parent["id"]),
            parent_run_id="",
            context_mode="fresh",
            depth=1,
            max_depth=2,
            runs=[_run_spec(str(running_child["id"]), task="中断 running")],
        )
        store.start_run(str(running["runs"][0]["id"]))

        coordinator = self.coordinator(_CompletingRuntime)
        _wait_until(
            lambda: coordinator.store.get_batch(str(queued["id"]), hydrate_artifacts=False)["state"] == "completed"
        )
        failed = coordinator.store.get_batch(str(running["id"]))
        self.assertEqual(failed["state"], "failed")
        self.assertIn("durable terminal checkpoint", failed["runs"][0]["error"])
        interrupted = failed["runs"][0]
        coordinator.control(
            str(self.parent["id"]),
            str(interrupted["id"]),
            {
                "action": "resume",
                "clientActionId": "action:restart-resume",
                "message": "恢复上下文\n从保留的子 Agent 会话继续，不要重做已有步骤。",
            },
        )
        _wait_until(
            lambda: coordinator.store.get_batch(str(running["id"]), hydrate_artifacts=False)["state"]
            == "completed"
        )
        resumed = coordinator.store.get_batch(str(running["id"]))["runs"][0]
        self.assertEqual(resumed["childSessionId"], interrupted["childSessionId"])
        coordinator.close()

    def test_console_uses_live_runtime_and_controls_are_idempotent(self) -> None:
        _InteractiveRuntime.instances.clear()
        coordinator = self.coordinator(_InteractiveRuntime)
        response = coordinator.delegate(
            str(self.parent["id"]),
            {
                "agent": "worker",
                "task": "核对实现路径",
                **_TASK_CONTRACT,
                "contextMode": "fresh",
                "wait": False,
            },
        )
        run_id = str(response["batch"]["runs"][0]["id"])
        _wait_until(lambda: coordinator.store.get_run(run_id)["state"] == "running")
        _wait_until(lambda: len(coordinator.store.list_inbox(run_id)) == 1)

        console = coordinator.console(str(self.parent["id"]), run_id)
        self.assertEqual(console["conversation"]["source"], "active_runtime")
        self.assertTrue(console["capabilities"]["steer"]["available"])
        self.assertEqual(console["inbox"][0]["kind"], "need_decision")
        self.assertEqual(console["inbox"][0]["request"]["options"], ["是", "否"])
        self.assertEqual(console["inbox"][0]["request"]["defaultValue"], "否")
        self.assertEqual(console["run"]["usage"]["toolCount"], 1)

        first = coordinator.control(
            str(self.parent["id"]),
            run_id,
            {
                "action": "steer",
                "clientActionId": "action:one",
                "message": "保留兼容层",
            },
        )
        replay = coordinator.control(
            str(self.parent["id"]),
            run_id,
            {
                "action": "steer",
                "clientActionId": "action:one",
                "message": "保留兼容层",
            },
        )
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(
            _InteractiveRuntime.instances[0].deliveries.count(("steer", "保留兼容层")),
            1,
        )
        with self.assertRaisesRegex(ValueError, "different action"):
            coordinator.control(
                str(self.parent["id"]),
                run_id,
                {
                    "action": "steer",
                    "clientActionId": "action:one",
                    "message": "删除兼容层",
                },
            )

        inbox_id = str(console["inbox"][0]["id"])
        coordinator.control(
            str(self.parent["id"]),
            run_id,
            {
                "action": "reply",
                "clientActionId": "action:reply",
                "inboxId": inbox_id,
                "message": "是，保留兼容层。",
            },
        )
        self.assertEqual(coordinator.store.list_inbox(run_id)[0]["status"], "replied")
        self.assertEqual(
            _InteractiveRuntime.instances[0].ui_resolutions,
            [("request:choice", {"value": "是，保留兼容层。"})],
        )
        self.assertNotIn(
            ("steer", "是，保留兼容层。"),
            _InteractiveRuntime.instances[0].deliveries,
        )
        coordinator.control(
            str(self.parent["id"]),
            run_id,
            {"action": "abort", "clientActionId": "action:abort"},
        )
        _wait_until(lambda: coordinator.store.get_run(run_id)["state"] == "aborted")
        coordinator.close()

    def test_reply_stays_pending_when_runtime_does_not_ack_ui_resolution(self) -> None:
        coordinator = self.coordinator(_RejectingUiRuntime)
        response = coordinator.delegate(
            str(self.parent["id"]),
            {
                "agent": "worker",
                "task": "核对 ACK 顺序",
                **_TASK_CONTRACT,
                "contextMode": "fresh",
                "wait": False,
            },
        )
        run_id = str(response["batch"]["runs"][0]["id"])
        _wait_until(lambda: len(coordinator.store.list_inbox(run_id)) == 1)
        inbox_id = str(coordinator.store.list_inbox(run_id)[0]["id"])

        with self.assertRaisesRegex(RuntimeError, "ACK failed"):
            coordinator.control(
                str(self.parent["id"]),
                run_id,
                {
                    "action": "reply",
                    "clientActionId": "reply:no-ack",
                    "inboxId": inbox_id,
                    "message": "是",
                },
            )
        self.assertEqual(coordinator.store.list_inbox(run_id)[0]["status"], "pending")
        coordinator.close()

    def test_resume_continues_the_retained_child_session(self) -> None:
        _ResumeRuntime.prompts.clear()
        coordinator = self.coordinator(_ResumeRuntime)
        response = coordinator.delegate(
            str(self.parent["id"]),
            {
                "agent": "reviewer",
                "task": "完成中断恢复验证",
                **_TASK_CONTRACT,
                "contextMode": "fresh",
                "wait": False,
            },
        )
        run_id = str(response["batch"]["runs"][0]["id"])
        child_session_id = str(response["batch"]["runs"][0]["childSessionId"])
        _wait_until(lambda: coordinator.store.get_run(run_id, hydrate_artifacts=False)["state"] == "failed")
        coordinator.control(
            str(self.parent["id"]),
            run_id,
            {
                "action": "resume",
                "clientActionId": "action:resume",
                "message": "从失败位置继续，不要重复已完成步骤。",
            },
        )
        _wait_until(lambda: coordinator.store.get_run(run_id, hydrate_artifacts=False)["state"] == "completed")
        resumed = coordinator.store.get_run(run_id)
        self.assertEqual(resumed["childSessionId"], child_session_id)
        self.assertEqual(_ResumeRuntime.prompts[-1], "从失败位置继续，不要重复已完成步骤。")
        coordinator.close()


def _assistant_message(session_id: str, turn_id: str, text: str) -> dict[str, object]:
    return {
        "schemaVersion": "rag-ime.agent-message.v1",
        "id": f"message:{turn_id}",
        "sessionId": session_id,
        "turnId": turn_id,
        "role": "assistant",
        "status": "completed",
        "blocks": [
            {
                "id": f"text:{turn_id}",
                "type": "text",
                "status": "completed",
                "presentationKind": "markdown",
                "data": {"text": text},
            }
        ],
        "attachments": [],
        "citations": [],
        "createdAtMs": 10,
        "completedAtMs": 11,
    }


def _small_budget_template(template_id: object, version: object = "1"):
    template = real_agent_template(template_id, version)
    return replace(
        template,
        budget=AgentTemplateBudget(
            max_depth=2,
            max_turns=8,
            max_tool_calls=12,
            max_total_tokens=10_000,
            max_duration_ms=5_000,
            max_output_chars=256,
        ),
    )


def _run_spec(child_session_id: str, *, task: str) -> dict[str, object]:
    return {
        "childSessionId": child_session_id,
        "templateId": "reviewer",
        "templateVersion": "1",
        "task": task,
        **_TASK_CONTRACT,
        "maxTurns": 8,
        "maxToolCalls": 12,
        "maxTotalTokens": 10_000,
        "maxDurationMs": 5_000,
        "maxOutputChars": 1_000,
    }


def _session_records(
    *,
    session_id: str,
    cwd: Path,
    parent_session: Path | None = None,
    assistant_content: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    header: dict[str, object] = {
        "type": "session",
        "version": 3,
        "id": session_id,
        "timestamp": "2026-07-14T00:00:00.000Z",
        "cwd": str(cwd),
    }
    if parent_session is not None:
        header["parentSession"] = str(parent_session)
    return [
        header,
        {
            "type": "message",
            "id": "m1",
            "parentId": None,
            "message": {"role": "user", "content": [{"type": "text", "text": "已有上下文"}]},
        },
        {
            "type": "message",
            "id": "m2",
            "parentId": "m1",
            "message": {
                "role": "assistant",
                "content": assistant_content or [{"type": "text", "text": "公开结论"}],
            },
        },
    ]


def _write_jsonl(path: Path, records: list[dict[str, object]], *, mode: int | None = None) -> None:
    path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in records) + "\n",
        encoding="utf-8",
    )
    if mode is not None:
        path.chmod(mode)


def _fork_context(
    *,
    parent_file: Path,
    child_file: Path,
    child_session_id: str,
) -> dict[str, object]:
    return {
        "schemaVersion": "rag-ime.agent-runtime-context.v1",
        "forkSessions": [
            {
                "sessionId": child_session_id,
                "sessionFile": str(child_file),
                "parentSessionFile": str(parent_file),
                "parentLeafId": "m2",
                "thinkingOverride": "off",
            }
        ],
    }


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


if __name__ == "__main__":
    unittest.main()
