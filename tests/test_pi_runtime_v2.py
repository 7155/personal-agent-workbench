from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from rag_ime.agent_events import AgentEventHub
from rag_ime.agent_runtime_driver import AgentRuntimeError
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.memory_model_executor import (
    MemoryModelTimeout,
    MemoryModelUnavailable,
    build_governed_memory_model_executor,
)
from rag_ime.pi.config import PiRuntimeConfig
from rag_ime.pi.values import PiRuntimeError
from rag_ime.pi.public import (
    pi_message_completes_public_turn,
    pi_message_is_public,
    pi_message_payload,
    public_code_tool_activity,
    public_reasoning_summaries,
    public_usage_evidence,
)
from rag_ime.pi.host_client import PiRuntimeHostClient
from rag_ime.pi.event_projection import runtime_primitive_capabilities
from rag_ime.pi.runtime import (
    PiRuntimeHostManager,
    _HostedSessionState,
)
from rag_ime.pi.values import (
    PiRuntimeCommandAcceptanceUnknown,
    PiRuntimeCommandRejected,
    PiRuntimeSettlementLookupTimeout,
)
from tests.sqlite_fixtures import copy_current_database


FAKE_HOST = r'''#!/usr/bin/env python3
import json
import os
import pathlib
import sys
import time

sessions = {}
sequence = 0
log_path = pathlib.Path(os.environ["RAG_IME_PI_AGENT_DIR"]) / "host-requests.jsonl"
model = {"provider": "gpt", "id": "gpt-5.6-luna", "name": "GPT-5.6 Luna",
         "api": "responses", "reasoning": True,
         "thinkingLevels": ["off", "medium", "xhigh", "max"],
         "input": ["text", "image"], "contextWindow": 1000000, "maxTokens": 128000}
if os.environ.get("TEST_MEMORY_SETTLEMENT_FIXTURE") == "1":
    model["provider"] = "openai-codex"

def write(value):
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")), flush=True)

def result(request, value):
    write({"protocolVersion": "2", "id": request["id"], "ok": True, "result": value})

def event(session_id, turn_id, client_message_id, payload):
    global sequence
    sequence += 1
    write({
        "protocolVersion": "2", "event": "agent.event", "sequence": sequence,
        "sessionId": session_id, "turnId": turn_id,
        "clientMessageId": client_message_id, "payload": payload,
    })

for line in sys.stdin:
    request = json.loads(line)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(request, ensure_ascii=False) + "\n")
    method = request["method"]
    params = request.get("params") or {}
    session_id = params.get("sessionId", "")
    if method == "hello":
        result(request, {"protocol": "rag-ime.pi-runtime-host", "protocolVersion": "2", "hostVersion": "test",
                         "piVersion": "0.80.7", "capabilities": {"multiSession": True, "maxSessions": 4,
                         "settledEvents": True, "dynamicTools": True, "managedPlugins": True,
                         "sessionControlState": True,
                         "sessionBoundAbort": True,
                         "sessionSkillAllowlist": True,
                         "sessionResourceDisclosure": True,
                         "sessionCandidateSkillPaths": True,
                         "sessionPromptSettings": True,
                         "transientContext": True,
                         "statelessCompletion": True,
                         "conversationFork": True,
                         "nativeMcpExecutionPolicy": True,
                         "codemode": {"available": True, "modes": ["off", "on", "only"], "defaultMode": "on"},
                         "runtimePrimitives": {
                             "continuationEnvelope": os.environ.get("TEST_CONTINUATION_ENVELOPE", "2"),
                             "cancelScope": "1",
                             "sessionContinuationQueue": True,
                             "sessionCancelOperationRegistry": True,
                             "sessionCancelOperations": {
                                 "provider": True, "tool": True, "retrySleep": True,
                                 "manualCompaction": True, "autoCompaction": True,
                                 "branchSummary": True, "bashProcess": True,
                                 "continuationTimer": True}}}})
    elif method == "health":
        result(request, {"ok": True, "protocolVersion": "2",
                         "openSessions": len(sessions), "maxSessions": 4, "modelError": ""})
    elif method == "completion.once":
        sequence += 1
        write({
            "protocolVersion": "2", "event": "runtime.notice", "sequence": sequence,
            "sessionId": params["requestId"],
            "payload": {"type": "completion_text_delta", "requestId": params["requestId"],
                        "delta": "one-shot reply", "elapsedMs": 350},
        })
        result(request, {
            "requestId": params["requestId"], "text": "one-shot reply",
            "provider": params["provider"], "modelId": params["modelId"],
            "thinkingLevel": params["thinkingLevel"],
            "firstTokenMs": 350,
            "elapsedMs": 4200,
            "usage": {"totalTokens": 32},
        })
    elif method == "completion.cancel":
        result(request, {"requestId": params["requestId"], "cancelled": True})
    elif method == "session.open":
        session = sessions.setdefault(session_id, {
            "sessionId": session_id, "piSessionId": "pi-" + session_id,
            "sessionFile": params.get("sessionFile") or str(pathlib.Path(os.environ["RAG_IME_PI_SESSION_DIR"]) / (session_id + ".jsonl")),
            "leafId": "", "messages": [], "thinkingLevel": params.get("thinkingLevel", "medium"),
            "codemodeMode": params.get("codemodeMode", "on"),
            "model": model,
            "isIdle": True,
            "messageQueue": {"steering": [], "followUp": [], "steeringMode": "one-at-a-time",
                             "followUpMode": "one-at-a-time"},
        })
        if os.environ.get("TEST_MEMORY_SETTLEMENT_FIXTURE") == "1":
            transcript = pathlib.Path(session["sessionFile"])
            entries = [json.loads(line) for line in transcript.read_text().splitlines()] if transcript.exists() else []
            session["settlements"] = {
                entry["data"]["turnId"]: entry["data"] for entry in entries
                if entry.get("customType") == "rag-ime.pi-turn-settlement"
            }
            bindings = [entry["data"] for entry in entries if entry.get("customType") == "rag-ime.pi-turn-binding"]
            if bindings and bindings[-1]["turnId"] not in session["settlements"]:
                session["activeTurn"] = bindings[-1]
                session["activeTurnId"] = bindings[-1]["turnId"]
                session["activeClientMessageId"] = bindings[-1]["clientMessageId"]
        if os.environ.get("TEST_SESSION_OPEN_MESSAGE_COUNT"):
            session["messageCount"] = int(os.environ["TEST_SESSION_OPEN_MESSAGE_COUNT"])
        recovered_turn_id = os.environ.get("TEST_SESSION_OPEN_RECOVERED_TURN", "").strip()
        if recovered_turn_id:
            session["activeTurnId"] = recovered_turn_id
            session["activeClientMessageId"] = "client-recovered"
            session["activeTurn"] = {
                "turnId": recovered_turn_id,
                "clientMessageId": "client-recovered",
            }
        result(request, {"snapshot": session, "evictedSessionId": None})
    elif method == "tools.sync":
        sessions[session_id]["toolManifest"] = params["tools"]
        result(request, {"tools": params["tools"]})
    elif method == "session.control_state":
        session = sessions[session_id]
        result(request, {
            "schemaVersion": "rag-ime.pi-session-control-state.v1",
            "sessionId": session_id,
            "isIdle": session.get("isIdle", True),
            "isCompacting": False,
            "activeTurn": (
                {
                    "turnId": session["activeTurnId"],
                    "clientMessageId": session.get("activeClientMessageId", ""),
                }
                if session.get("activeTurnId")
                else None
            ),
            "sequence": sequence,
        })
    elif method == "session.snapshot":
        if os.environ.get("TEST_SESSION_SNAPSHOT_HANG") == "1":
            time.sleep(2)
        result(request, sessions[session_id])
    elif method == "session.commands":
        result(request, {"commands": [{"name": "skill:plugin-creator",
                                        "description": "Create and propose a managed plugin", "source": "skill"}]})
    elif method == "session.command.invoke":
        command = params["command"]
        result(request, {
            "schemaVersion": "rag-ime.pi-package-command-invocation.v1",
            "command": command,
            "name": command.split()[0].removeprefix("/"),
            "handled": True,
            "result": {
                "schemaVersion": "rag-ime.pi-package-command-result.v1",
                "packageId": "@paw/pi-session-workflow",
                "command": command.split()[0].removeprefix("/"),
                "message": "Goal [active]: Ship the TUI",
            },
            "leafId": "entry-command-result",
        })
    elif method == "models.list":
        result(request, {"models": [model]})
    elif method == "session.model.set":
        result(request, {**model, "provider": params["provider"], "id": params["modelId"],
                         "maxTokens": params.get("maxTokens", model["maxTokens"])})
    elif method in {"session.settlement.get", "session.await_settled"}:
        if session_id not in sessions:
            write({"protocolVersion": "2", "id": request["id"], "ok": False,
                   "error": {"code": "SESSION_NOT_FOUND", "message": "Session is not open: " + session_id}})
            continue
        settlement = sessions[session_id].get("settlements", {}).get(params["turnId"])
        if settlement and settlement["clientMessageId"] != params["clientMessageId"]:
            write({"protocolVersion": "2", "id": request["id"], "ok": False,
                   "error": {"code": "SETTLED_RECEIPT_MISMATCH", "message": "settlement client identity mismatch"}})
        elif method == "session.settlement.get":
            result(request, {"settlement": settlement})
        elif settlement:
            result(request, settlement)
        else:
            write({"protocolVersion": "2", "id": request["id"], "ok": False,
                   "error": {"code": "SETTLED_TIMEOUT", "message": "turn settlement timed out"}})
    elif method == "session.thinking.set":
        sessions[session_id]["thinkingLevel"] = params["level"]
        result(request, {"level": params["level"]})
    elif method == "session.codemode.set":
        sessions[session_id]["codemodeMode"] = params["mode"]
        result(request, {
            "schemaVersion": "rag-ime.pi-session-codemode.v1",
            "sessionId": session_id,
            "mode": params["mode"],
            "snapshot": sessions[session_id],
        })
    elif method == "session.prompt":
        # Match Pi's fresh UUID per prompt. Reusing a retired identity causes
        # later execution events to be correctly fenced as stale replays.
        prompt_sequence = sessions[session_id].get("promptSequence", 0) + 1
        sessions[session_id]["promptSequence"] = prompt_sequence
        turn_id = "turn-" + session_id + ("-" + str(prompt_sequence) if prompt_sequence > 1 else "")
        sessions[session_id]["activeTurnId"] = turn_id
        client_message_id = params.get("clientMessageId", "")
        sessions[session_id]["activeClientMessageId"] = client_message_id
        if params["message"].startswith("preflight-handled"):
            runtime_id = sessions[session_id]["piSessionId"]
            settlement = {
                "schemaVersion": "rag-ime.pi-turn-settlement.v1", "sessionId": session_id,
                "runtimeSessionId": runtime_id, "turnId": turn_id, "clientMessageId": client_message_id,
                "receipt": {"schemaVersion": "pi.agent-settled.v2", "receiptId": "handled:" + turn_id,
                            "sessionId": runtime_id, "runId": turn_id, "scopeId": runtime_id + ":" + turn_id,
                            "generation": 1, "disposition": "completed", "stopReason": "prompt_handled",
                            "origin": "prompt_preflight", "settledAtMs": 200, "aborted": False,
                            "pendingOperations": 0, "operations": {"pending": 0}, "operationCounts": {}}}
            if params["message"] == "preflight-handled-wrong-client":
                settlement["clientMessageId"] = "another-client"
            elif params["message"] == "preflight-handled-without-origin":
                settlement["receipt"].pop("origin")
            elif params["message"] == "preflight-handled-aborted":
                settlement["receipt"]["disposition"] = "aborted"
                settlement["receipt"]["aborted"] = True
                settlement["receipt"]["stopReason"] = "prompt_preflight_cancelled"
            elif params["message"] == "preflight-handled-failed":
                settlement["receipt"]["disposition"] = "failed"
                settlement["receipt"]["stopReason"] = "fixture_failure"
            sessions[session_id].setdefault("settlements", {})[turn_id] = settlement
            sessions[session_id]["isIdle"] = True
            sessions[session_id]["activeTurnId"] = ""
            sessions[session_id]["activeClientMessageId"] = ""
            if params["message"] == "preflight-handled-before-ack":
                event(session_id, turn_id, client_message_id,
                      {"type": "agent_settled", "receipt": settlement["receipt"]})
            # By default drop the live event: the ACK carries the same
            # durable receipt, not a made-up assistant message or a new run.
            result(request, {"turnId": turn_id, "clientMessageId": client_message_id,
                             "disposition": "handled", "settlement": settlement})
            continue
        user_entry_id = "entry-user-" + str(len(sessions[session_id].get("forkItems", [])) + 1)
        assistant_entry_id = "entry-assistant-" + str(len(sessions[session_id].get("forkItems", [])) + 1)
        user_message = {"id": user_entry_id, "role": "user", "timestamp": 100,
                        "content": [{"type": "text", "text": params["message"]}]}
        assistant = {"id": assistant_entry_id, "role": "assistant", "timestamp": 101,
                     "content": [{"type": "text", "text": "host reply for " + session_id}]}
        if params["message"] == "duplicate-across-native-followup":
            previous_messages = list(sessions[session_id]["messages"])
            previous_assistant = next(
                item for item in reversed(previous_messages)
                if item.get("role") == "assistant"
            )
            assistant = {
                **assistant,
                "content": [{"type": "text", "text": "second host reply for " + session_id}],
            }
            replayed_assistant = {
                **previous_assistant,
                "id": str(previous_assistant["id"]) + "-replayed",
                "timestamp": 102,
            }
            sessions[session_id]["messages"].extend(
                [user_message, replayed_assistant, assistant]
            )
            durable_messages = [*previous_messages, user_message, assistant]
            sessions[session_id]["entries"] = [
                {
                    "type": "message",
                    "id": "durable-" + str(message["id"]),
                    "message": message,
                }
                for message in durable_messages
            ]
        else:
            sessions[session_id]["messages"].extend([user_message, assistant])
        if params["message"] == "duplicate-final-snapshot":
            sessions[session_id]["messages"].append({
                **assistant,
                "id": assistant_entry_id + "-duplicate",
                "timestamp": 102,
            })
        sessions[session_id].setdefault("forkItems", []).extend([
            {"entryId": user_entry_id, "text": params["message"], "role": "user", "createdAtMs": 100},
            {"entryId": assistant_entry_id, "text": "host reply for " + session_id,
             "role": "assistant", "createdAtMs": 101},
        ])
        sessions[session_id]["leafId"] = assistant_entry_id
        transcript = pathlib.Path(sessions[session_id]["sessionFile"])
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(json.dumps({"type": "session", "id": sessions[session_id]["piSessionId"]}) + "\n")
        if os.environ.get("TEST_MEMORY_SETTLEMENT_FIXTURE") == "1":
            runtime_id = sessions[session_id]["piSessionId"]
            binding = {"schemaVersion": "rag-ime.pi-turn-binding.v1", "turnId": turn_id,
                       "clientMessageId": client_message_id}
            settlement = {
                "schemaVersion": "rag-ime.pi-turn-settlement.v1", "sessionId": session_id,
                "runtimeSessionId": runtime_id, "turnId": turn_id, "clientMessageId": client_message_id,
                "receipt": {"schemaVersion": "pi.agent-settled.v2", "receiptId": "pi-settled:" + turn_id,
                            "sessionId": runtime_id, "runId": turn_id, "scopeId": runtime_id + ":" + turn_id,
                            "generation": 1, "disposition": "completed", "stopReason": "stop",
                            "settledAtMs": 200, "aborted": False, "pendingOperations": 0,
                            "operations": {"pending": 0, "pendingByKind": {}, "registeredByKind": {}},
                            "operationCounts": {}, "finalMessage": {"role": "assistant",
                            "content": [{"type": "text", "text": '{"decisions":[]}'}]}}}
            with transcript.open("a") as handle:
                handle.write(json.dumps({"type": "custom", "customType": "rag-ime.pi-turn-binding", "data": binding}) + "\n")
                handle.write(json.dumps({"type": "custom", "customType": "rag-ime.pi-turn-settlement", "data": settlement}) + "\n")
            sessions[session_id]["settlements"] = {turn_id: settlement}
            result(request, {"accepted": True, "turnId": turn_id})
            continue
        result(request, {"accepted": True, "turnId": turn_id})
        event(session_id, turn_id, client_message_id, {"type": "agent_start"})
        if params["message"].startswith("approval:"):
            event(session_id, turn_id, client_message_id, {
                "type": "extension_ui_request",
                "id": "ui-approval-1",
                "method": "confirm",
                "title": "RAG-IME-APPROVAL:" + params["message"],
            })
            continue
        if params["message"] == "grouped-questions":
            event(session_id, turn_id, client_message_id, {
                "type": "extension_ui_request",
                "id": "ui-grouped-1",
                "method": "editor",
                "title": "RAG-IME-QUESTIONS:call-grouped-1",
                "prefill": json.dumps({
                    "schemaVersion": "rag-ime.grouped-questions.v2",
                    "questions": [
                        {
                            "id": "deploy_target",
                            "question": "这次部署到哪里？",
                            "options": [
                                {"label": "预发布环境", "description": "先验证变更。"},
                                {"label": "生产环境", "description": "直接面向用户发布。"},
                            ],
                            "recommended": 0,
                        },
                        {
                            "id": "release_window",
                            "question": "什么时候发布？",
                            "options": [
                                {"label": "现在"},
                                {"label": "今晚"},
                            ],
                        },
                    ],
                }, ensure_ascii=False, separators=(",", ":")),
            })
            continue
        if params["message"] == "hang-without-settled":
            continue
        event(session_id, turn_id, client_message_id, {"type": "message_end", "message": assistant})
        event(session_id, turn_id, client_message_id, {"type": "agent_end", "messages": [assistant]})
        if params["message"] == "end-without-settled":
            # Model a host that reached the idle boundary but whose terminal
            # `agent_settled` notification was lost in transit.  Keeping the
            # active turn here would describe a different state: a genuinely
            # live or suspended turn, which the runtime must not auto-retire.
            sessions[session_id].pop("activeTurnId", None)
            sessions[session_id].pop("activeClientMessageId", None)
            sessions[session_id]["activeTurn"] = None
            continue
        time.sleep(0.15)
        event(session_id, turn_id, client_message_id, {"type": "agent_settled"})
    elif method == "approval.resolve":
        result(request, {
            "resolved": True,
            "approvalId": params.get("approvalId", ""),
            "approved": bool(params.get("approved")),
        })
        turn_id = sessions[session_id]["activeTurnId"]
        client_message_id = sessions[session_id].get("activeClientMessageId", "")
        assistant = sessions[session_id]["messages"][-1]
        event(session_id, turn_id, client_message_id, {"type": "message_end", "message": assistant})
        event(session_id, turn_id, client_message_id, {"type": "agent_end", "messages": [assistant]})
        time.sleep(0.15)
        event(session_id, turn_id, client_message_id, {"type": "agent_settled"})
    elif method == "ui.resolve":
        result(request, {"resolved": True, "requestId": params.get("requestId", "")})
        if params.get("requestId") == "ui-grouped-1":
            turn_id = sessions[session_id]["activeTurnId"]
            client_message_id = sessions[session_id].get("activeClientMessageId", "")
            assistant = {
                "id": "entry-assistant-grouped",
                "role": "assistant",
                "timestamp": 102,
                "content": [{"type": "text", "text": str((params.get("response") or {}).get("value") or "")}],
            }
            event(session_id, turn_id, client_message_id, {"type": "message_end", "message": assistant})
            event(session_id, turn_id, client_message_id, {"type": "agent_end", "messages": [assistant]})
            time.sleep(0.15)
            event(session_id, turn_id, client_message_id, {"type": "agent_settled"})
    elif method in {"session.steer", "session.follow_up"}:
        if params.get("message") == "host-reject-session-idle":
            write({"protocolVersion": "2", "id": request["id"], "ok": False,
                   "error": {"code": "SESSION_IDLE",
                             "message": "Session has no active turn to receive a queued message"}})
            continue
        turn_id = "turn-" + session_id
        queue = sessions[session_id]["messageQueue"]
        key = "steering" if method == "session.steer" else "followUp"
        queue[key].append(params["message"])
        delivery = "steer" if method == "session.steer" else "followUp"
        result(request, {"accepted": True, "queued": True, "delivery": delivery,
                         "turnId": turn_id, "messageQueue": queue})
        event(session_id, turn_id, params.get("clientMessageId", ""),
              {"type": "queue_update", "steering": queue["steering"],
               "followUp": queue["followUp"]})
    elif method == "session.fork.candidates":
        result(request, {"items": sessions[session_id].get("forkItems", [])})
    elif method == "session.fork":
        target_id = params["targetSessionId"]
        candidates = sessions[session_id].get("forkItems", [])
        selected = next((item for item in candidates if item["entryId"] == params["entryId"]), None)
        if selected is None:
            result(request, {})
            continue
        target_file = pathlib.Path(os.environ["RAG_IME_PI_SESSION_DIR"]) / ("fork-" + target_id + ".jsonl")
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text(json.dumps({"type": "session", "id": "pi-fork-" + target_id}) + "\n")
        target = {
            "sessionId": target_id, "piSessionId": "pi-fork-" + target_id,
            "sessionFile": str(target_file), "leafId": "", "messages": [], "forkItems": [],
            "thinkingLevel": sessions[session_id]["thinkingLevel"], "model": model,
        }
        sessions[target_id] = target
        result(request, {
            "sourceSessionId": session_id, "targetSessionId": target_id,
            "entryId": params["entryId"],
            "selectedText": selected["text"] if selected["role"] == "user" else "",
            "branchAnchor": params["entryId"], "snapshot": target, "evictedSessionId": None,
        })
    elif method == "session.debug.context":
        if os.environ.get("TEST_DEBUG_CONTEXT_HANG") == "1":
            time.sleep(2)
        response_bytes = int(os.environ.get("TEST_DEBUG_CONTEXT_RESPONSE_BYTES", "0"))
        result(request, {
            "available": True,
            "context": {"prompt": "x" * response_bytes},
        } if response_bytes else {})
    elif method == "session.compact":
        result(request, {
            "summary": "用户要求角色每天整理主题书。",
            "coverageStartEntryId": "entry-user-1",
            "coverageEndEntryId": "entry-assistant-1",
            "tokensBefore": 1200,
            "estimatedTokensAfter": 320,
        })
    elif method == "session.abort":
        turn_id = sessions[session_id].get("activeTurnId", "turn-" + session_id)
        sessions[session_id].pop("activeTurnId", None)
        sessions[session_id].pop("activeClientMessageId", None)
        sessions[session_id]["activeTurn"] = None
        result(request, {
            "schemaVersion": "rag-ime.pi-session-abort-receipt.v1",
            "sessionId": session_id,
            "turnId": turn_id,
            "cancelledDecisionIds": [],
            "cancelledUIRequestIds": [],
            "lifecycle": {
                "schemaVersion": "pi.agent-abort-receipt.v1",
                "scopeId": session_id,
                "generation": 1,
                "reason": "user_abort",
                "cancelledContinuationIds": [],
                "cancelledOperationIds": ["provider-" + session_id],
                "failedOperationIds": [],
                "operations": [{
                    "operationId": "provider-" + session_id,
                    "kind": "provider",
                }],
                "pendingOperations": [],
                "drained": True,
                "idle": True,
            },
        })
    elif method == "session.close":
        result(request, {"closed": sessions.pop(session_id, None) is not None})
    elif method == "plugins.list":
        result(request, {"plugins": []})
    else:
        result(request, {})
'''


class PiRuntimeV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-pi-v2-")
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        copy_current_database(self.root / "rag-ime.sqlite")
        self.fake_host = self.root / "fake-host"
        self.fake_host.write_text(FAKE_HOST, encoding="utf-8")
        self.fake_host.chmod(0o755)
        self.store = AgentSessionStore(self.root / "rag-ime.sqlite")
        self.store.initialize()
        self.first = self.store.create(title="first", created_at_ms=100)
        self.second = self.store.create(title="second", created_at_ms=101)
        self.events = AgentEventHub(
            sequence_loader=self.store.max_event_sequence,
            event_recorder=self._record_event,
        )
        self.compactions: list[tuple[str, dict[str, object], str]] = []
        self.runtime = PiRuntimeHostManager(
            config=PiRuntimeConfig(
                enabled=True,
                executable=self.fake_host,
                agent_dir=self.root / "agent",
                session_dir=self.root / "sessions",
                logs_dir=self.root / "logs",
                idle_timeout_seconds=0,
                command_timeout_seconds=5,
                provider="gpt",
                model="gpt-5.6-luna",
                pi_version="0.80.7",
                protocol_version="2",
                max_sessions=4,
            ),
            sessions=self.store,
            events=self.events,
            tool_manifest_provider=lambda _session: [{
                "name": "memory",
                "description": "memory operations",
                "parameters": {"type": "object", "properties": {"op": {"type": "string"}}},
            }],
            compaction_observer=self._observe_compaction,
        )

    def tearDown(self) -> None:
        self.runtime.stop()
        self.tmp.cleanup()

    def _observe_compaction(
        self,
        session_id: str,
        result: dict[str, object],
        trigger: str,
    ) -> dict[str, object]:
        self.compactions.append((session_id, dict(result), trigger))
        return {
            "schemaVersion": "rag-ime.agent-memory-checkpoint.v1",
            "ok": True,
            "stored": True,
            "status": "checkpointed",
        }

    def test_turn_tool_evidence_reads_exact_durable_binding_without_starting_host(self):
        session_id = self.first["id"]
        transcript = self.runtime.config.session_dir / "bound-evidence.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        script = "console.log(" + json.dumps("公开合法操作" * 900) + ")"
        entries = [{"type": "session", "id": "physical-evidence"},
            {"type": "custom", "id": "binding", "parentId": "physical-evidence",
                "customType": "rag-ime.pi-turn-binding", "data": {
                    "schemaVersion": "rag-ime.pi-turn-binding.v1", "turnId": "bound-turn"}},
            {"type": "message", "id": "user", "parentId": "binding",
                "message": {"role": "user", "content": "private task"}},
            {"type": "message", "id": "call", "parentId": "user",
                "message": {"role": "assistant", "content": [{"type": "toolCall", "name": "browser",
                    "id": "browser-call", "arguments": {"op": "run", "script": script}}]}},
            {"type": "message", "id": "result", "parentId": "call",
                "message": {"role": "toolResult", "toolCallId": "browser-call", "toolName": "browser",
                    "details": {"schemaVersion": "rag-ime.browser-control.v1", "status": "completed"}}}]
        transcript.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
        self.store.bind_runtime_session(session_id, driver_id="managed-pi", runtime_kind="pi_rpc",
            external_session_id="physical-evidence", transcript_ref=str(transcript), branch_anchor="result",
            binding_state="active", metadata={"protocolVersion": "2"}, message_count=3)
        with patch.object(self.runtime, "_host_locked", side_effect=AssertionError("Host started")):
            evidence = self.runtime.session_tool_evidence(session_id, turn_id="bound-turn")
            self.assertEqual(len(evidence["toolHistoryEvents"]), 2)
            self.assertEqual(evidence["toolHistoryEvents"][0]["payload"]["args"]["script"], script)
            self.assertEqual(self.runtime.session_tool_evidence(session_id, turn_id="other-turn")["toolHistoryEvents"], [])
            with self.assertRaises(AgentRuntimeError):
                self.runtime.session_tool_evidence(self.second["id"], turn_id="bound-turn")
        self.assertNotIn("private task", json.dumps(evidence))
        self.assertIsNone(self.runtime._client)

    def test_runtime_status_is_a_local_projection_and_does_not_start_host(
        self,
    ) -> None:
        self.assertIsNone(self.runtime._client)
        with patch.object(
            self.runtime,
            "_host_locked",
            side_effect=AssertionError(
                "runtime_status must not start or contact the Pi Host"
            ),
        ) as start_host:
            status = self.runtime.runtime_status()

        start_host.assert_not_called()
        self.assertIsNone(self.runtime._client)
        self.assertEqual(status["activeSessionIds"], [])

    def test_opening_runtime_does_not_replace_public_message_count_with_provider_entries(
        self,
    ) -> None:
        session_id = str(self.first["id"])
        self.store.set_status(
            session_id,
            "idle",
            message_count=3,
            last_message_preview="公开助手回复",
        )

        self.runtime.config = replace(
            self.runtime.config,
            provider_environment={"TEST_SESSION_OPEN_MESSAGE_COUNT": "61"},
        )
        self.runtime.ensure(session_id)

        session = self.store.get(session_id)
        self.assertEqual(session["messageCount"], 3)
        self.assertEqual(session["lastMessagePreview"], "公开助手回复")

    def test_usage_evidence_distinguishes_cache_report_from_missing_fields(
        self,
    ) -> None:
        self.assertEqual(
            public_usage_evidence({"role": "assistant"}),
            {
                "usageReported": False,
                "cacheUsageReported": False,
            },
        )
        self.assertEqual(
            public_usage_evidence({
                "usage": {"input": 120, "output": 30},
            }),
            {
                "usageReported": True,
                "cacheUsageReported": False,
            },
        )
        self.assertEqual(
            public_usage_evidence({
                "usage": {
                    "input": 120,
                    "output": 30,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                },
            }),
            {
                "usageReported": True,
                "cacheUsageReported": True,
            },
        )

    def test_managed_approval_events_keep_tool_call_and_causal_turn(self) -> None:
        session_id = str(self.first["id"])
        turn_id = f"turn-{session_id}"
        approval = self.store.create_approval(
            session_id=session_id,
            tool_name="workspace_shell",
            operation="run",
            payload_sha256="a" * 64,
            preview={"summary": "运行受控命令"},
            risk_level="R3",
            causal_metadata={"turnId": turn_id},
        )
        approval = self.store.bind_approval_tool_call(
            str(approval["approvalId"]),
            tool_call_id="tool:managed-approval",
        )
        approval_id = str(approval["approvalId"])

        self.runtime.prompt(
            session_id,
            approval_id,
            client_message_id="client:managed-approval",
        )
        _wait_until(
            lambda: self.runtime.has_pending_approval(
                session_id,
                approval_id,
            )
        )
        # The control flag precedes asynchronous public event publication.
        # Wait for the receipt this test inspects, rather than racing its writer.
        _wait_until(lambda: any(
            event.event_type == "approval_required"
            and event.payload.get("approvalId") == approval_id
            for event in self.events.replay(session_id)[0]
        ))
        events, _ = self.events.replay(session_id)
        required = next(
            event
            for event in events
            if event.event_type == "approval_required"
        )
        self.assertEqual(required.payload["toolCallId"], "tool:managed-approval")
        self.assertEqual(required.turn_id, turn_id)

        self.store.decide_approval(
            approval_id,
            approved=True,
            payload_sha256="a" * 64,
        )
        self.runtime.resolve_approval(
            session_id,
            approval_id,
            approved=True,
            resolution_state="external_pending",
        )
        _wait_until(lambda: self.store.get(session_id)["status"] == "idle")
        events, _ = self.events.replay(session_id)
        resolved = next(
            event
            for event in events
            if event.event_type == "approval_resolved"
        )
        self.assertEqual(resolved.payload["toolCallId"], "tool:managed-approval")
        self.assertEqual(resolved.turn_id, turn_id)

    def test_grouped_questions_project_once_and_validate_one_answer_map(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.prompt(
            session_id,
            "grouped-questions",
            client_message_id="client:grouped-questions",
        )
        _wait_until(lambda: bool(self.runtime.pending_ui_requests(session_id)))

        pending = self.runtime.pending_ui_requests(session_id)
        self.assertEqual(len(pending), 1)
        request = pending[0]
        self.assertEqual(request["requestKind"], "grouped_questions")
        self.assertEqual(request["method"], "editor")
        self.assertEqual(
            request["questions"],
            [
                {
                    "id": "deploy_target",
                    "question": "这次部署到哪里？",
                    "options": [
                        {"label": "预发布环境", "description": "先验证变更。"},
                        {"label": "生产环境", "description": "直接面向用户发布。"},
                    ],
                    "recommended": 0,
                },
                {
                    "id": "release_window",
                    "question": "什么时候发布？",
                    "options": [{"label": "现在"}, {"label": "今晚"}],
                },
            ],
        )
        self.assertNotIn("prefill", request)
        self.assertNotIn("RAG-IME-QUESTIONS", str(request["title"]))

        with self.assertRaisesRegex(PiRuntimeError, "当前问题或可选项不一致"):
            self.runtime.resolve_ui_request(
                session_id,
                str(request["requestId"]),
                response={
                    "value": json.dumps(
                        {
                            "answers": {
                                "deploy_target": {"selected": ["不存在的环境"]},
                                "release_window": {"selected": ["今晚"]},
                            }
                        },
                        ensure_ascii=False,
                    ),
                    "resolutionSource": "direct_user",
                },
            )
        with self.assertRaisesRegex(PiRuntimeError, "当前问题或可选项不一致"):
            self.runtime.resolve_ui_request(
                session_id,
                str(request["requestId"]),
                response={
                    "value": json.dumps(
                        {"answers": {"deploy_target": {"selected": ["预发布环境"]}}},
                        ensure_ascii=False,
                    ),
                    "resolutionSource": "direct_user",
                },
            )
        self.assertEqual(len(self.runtime.pending_ui_requests(session_id)), 1)

        self.runtime.resolve_ui_request(
            session_id,
            str(request["requestId"]),
            response={
                "value": json.dumps(
                    {
                        "answers": {
                            "deploy_target": {"selected": ["预发布环境"]},
                            "release_window": {"selected": ["今晚"]},
                        }
                    },
                    ensure_ascii=False,
                ),
                "resolutionSource": "direct_user",
            },
        )
        _wait_until(
            lambda: any(
                event.event_type == "turn_completed"
                for event in self.events.replay(session_id)[0]
            )
        )
        self.assertEqual(self.store.get(session_id)["status"], "idle")

        events, _ = self.events.replay(session_id)
        required = next(
            event
            for event in events
            if event.event_type == "user_input_required"
            and event.payload.get("requestKind") == "grouped_questions"
        )
        resolved = next(
            event
            for event in events
            if event.event_type == "user_input_required"
            and event.payload.get("resolutionState") == "resolved"
        )
        with self.assertRaisesRegex(PiRuntimeError, "no longer pending"):
            self.runtime.resolve_ui_request(
                session_id,
                str(request["requestId"]),
                response={
                    "value": json.dumps(
                        {
                            "answers": {
                                "deploy_target": {"selected": ["预发布环境"]},
                                "release_window": {"selected": ["今晚"]},
                            }
                        },
                        ensure_ascii=False,
                    ),
                    "resolutionSource": "direct_user",
                },
            )
        completed_message = next(
            event for event in events if event.event_type == "message_completed"
        )
        self.assertEqual(
            completed_message.payload["message"]["blocks"][0]["data"]["text"],
            '{"answers":{"deploy_target":{"selected":["预发布环境"]},"release_window":{"selected":["今晚"]}}}',
        )
        completed = next(event for event in events if event.event_type == "turn_completed")
        self.assertEqual(
            {required.turn_id, resolved.turn_id, completed.turn_id},
            {required.turn_id},
        )

    def test_stateless_completion_does_not_open_or_persist_a_session(self) -> None:
        deltas: list[str] = []
        result = self.runtime.complete_once(
            request_id="surface-one-shot-1",
            provider="deepseek",
            model_id="deepseek-v4-flash",
            thinking_level="high",
            message="只回复这一次",
            on_text_delta=deltas.append,
            timeout_seconds=15,
        )

        self.assertEqual(result["text"], "one-shot reply")
        self.assertEqual(result["elapsedMs"], 4200)
        self.assertEqual(result["firstTokenMs"], 350)
        self.assertEqual(deltas, ["one-shot reply"])
        self.assertEqual(list((self.root / "sessions").glob("*.jsonl")), [])
        requests = [
            json.loads(line)
            for line in (self.root / "agent" / "host-requests.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        methods = [request["method"] for request in requests]
        self.assertEqual(methods, ["hello", "completion.once"])
        params = requests[-1]["params"]
        self.assertEqual(params["provider"], "deepseek")
        self.assertEqual(params["modelId"], "deepseek-v4-flash")
        self.assertEqual(params["thinkingLevel"], "high")
        self.assertNotIn("sessionId", params)
        self.assertNotIn("images", params)
        self.assertTrue(self.runtime.runtime_status()["capabilities"]["statelessCompletion"])

    def test_stateless_response_reconciles_delayed_deltas_without_replaying_them(self) -> None:
        client = self.runtime._host()
        for early in ("", "one-shot "):
            with self.subTest(early=early):
                deltas = []
                captured = []

                def response_before_projection(method, params, *, timeout=None, early=early, captured=captured):
                    self.assertEqual(method, "completion.once")
                    sink = self.runtime._completion_sinks[params["requestId"]]
                    captured.append(sink)
                    if early:
                        sink(early)
                    return {"text": "one-shot reply"}

                with patch.object(client, "send", side_effect=response_before_projection):
                    self.runtime.complete_once(
                        request_id="delayed-stream", provider="deepseek",
                        model_id="deepseek-v4-flash", thinking_level="high",
                        message="reply", on_text_delta=deltas.append,
                    )
                self.assertEqual("".join(deltas), "one-shot reply")
                before = list(deltas)
                captured[0]("one-shot reply")
                self.assertEqual(deltas, before)

    def test_model_selection_forwards_a_bounded_output_budget(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        selected = self.runtime.available_models()[0]
        client = self.runtime._require_client()
        original_send = client.send
        captured: dict[str, object] = {}

        def send(
            method: str,
            params: dict[str, object] | None = None,
            *,
            timeout: float | None = None,
            before_write=None,
        ) -> dict[str, object]:
            if method == "session.model.set":
                captured.update(dict(params or {}))
                return {**selected, "maxTokens": int(params["maxTokens"])}
            return original_send(
                method,
                params,
                timeout=timeout,
                before_write=before_write,
            )

        with patch.object(client, "send", side_effect=send):
            receipt = self.runtime.set_model(
                session_id,
                provider=str(selected["provider"]),
                model_id=str(selected["id"]),
                max_tokens=16_384,
            )

        self.assertEqual(captured["sessionId"], session_id)
        self.assertEqual(captured["maxTokens"], 16_384)
        self.assertEqual(receipt["selected"]["maxTokens"], 16_384)

    def test_stateless_completion_timeout_cancels_only_that_request(self) -> None:
        first_client = self.runtime._host()
        first_host_identity = first_client.host_identity
        original_send = first_client.send

        def timeout_completion(
            method: str,
            params: dict[str, object] | None = None,
            *,
            timeout: float | None = None,
        ) -> dict[str, object]:
            if method == "completion.once":
                raise PiRuntimeError(
                    "Pi Runtime Host command timed out: completion.once"
                )
            return original_send(method, params, timeout=timeout)

        with patch.object(first_client, "send", side_effect=timeout_completion):
            with self.assertRaisesRegex(
                PiRuntimeError,
                "command timed out: completion.once",
            ):
                self.runtime.complete_once(
                    request_id="surface-timeout-1",
                    provider="deepseek",
                    model_id="deepseek-v4-flash",
                    thinking_level="high",
                    message="这一次模拟无响应",
                    timeout_seconds=15,
                )

        timed_out_status = self.runtime.runtime_status()
        receipt = timed_out_status["runtimeHostKillGate"]["lastKillReceipt"]
        self.assertIsNone(receipt)
        self.assertEqual(timed_out_status["status"], "ready")
        self.assertTrue(first_client.running)

        retried = self.runtime.complete_once(
            request_id="surface-timeout-retry",
            provider="deepseek",
            model_id="deepseek-v4-flash",
            thinking_level="high",
            message="新 Host 应该正常完成",
            timeout_seconds=15,
        )

        self.assertEqual(retried["text"], "one-shot reply")
        self.assertIs(self.runtime._client, first_client)
        self.assertEqual(self.runtime._client.host_identity, first_host_identity)
        requests = [
            json.loads(line)
            for line in (self.root / "agent" / "host-requests.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertIn("completion.cancel", [request["method"] for request in requests])

    def test_plugin_mutations_use_a_dedicated_approval_capability(self) -> None:
        self.runtime.stop()
        self.runtime = PiRuntimeHostManager(
            config=replace(
                self.runtime.config,
                tool_gateway_token="tool-gateway-only",
                plugin_approval_token="plugin-approval-only",
            ),
            sessions=self.store,
            events=self.events,
            tool_manifest_provider=lambda _session: [],
        )

        self.runtime.plugin_preview_install(
            {
                "sourcePath": "/tmp/plugin",
                "expectedDigest": "d" * 64,
                "enable": False,
            }
        )
        self.runtime.plugin_install({"sourcePath": "/tmp/plugin"})
        self.runtime.plugin_enable(
            "plugin:test",
            enabled=True,
            expected_active_digest="a" * 64,
            expected_enabled=False,
        )
        self.runtime.plugin_rollback(
            "plugin:test",
            expected_active_digest="b" * 64,
            target_digest="c" * 64,
        )
        self.runtime.plugin_uninstall(
            "plugin:test",
            expected_active_digest="b" * 64,
            expected_enabled=True,
        )

        requests = [
            json.loads(line)
            for line in (self.root / "agent" / "host-requests.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        mutations = [
            request
            for request in requests
            if request["method"]
            in {
                "plugins.install",
                "plugins.enable",
                "plugins.rollback",
                "plugins.uninstall",
            }
        ]
        self.assertEqual(len(mutations), 4)
        self.assertTrue(
            all(
                request["params"]["approvalToken"] == "plugin-approval-only"
                for request in mutations
            )
        )
        self.assertNotIn("tool-gateway-only", json.dumps(mutations))

    def test_plugin_catalog_uses_the_native_runtime_method(self) -> None:
        catalog = self.runtime.plugin_catalog()

        self.assertEqual(catalog, [])
        requests = [
            json.loads(line)
            for line in (self.root / "agent" / "host-requests.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(requests[-1]["method"], "plugins.catalog")

    def test_pi_package_draft_and_prepare_use_native_host_methods_without_approval(self) -> None:
        self.runtime.plugin_create_package(
            {
                "draftId": "context-helper",
                "packageJson": {
                    "name": "@paw/context-helper",
                    "version": "1.0.0",
                    "pi": {"skills": ["skills/context-helper/SKILL.md"]},
                },
                "files": {"skills/context-helper/SKILL.md": "skill body"},
            }
        )
        self.runtime.plugin_prepare_package("npm:@paw/context-helper@1.0.0")

        requests = [
            json.loads(line)
            for line in (self.root / "agent" / "host-requests.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        package_requests = [
            request
            for request in requests
            if request["method"] in {"plugins.package.create", "plugins.package.prepare"}
        ]
        self.assertEqual(
            [request["method"] for request in package_requests],
            ["plugins.package.create", "plugins.package.prepare"],
        )
        self.assertEqual(
            package_requests[1]["params"],
            {"source": "npm:@paw/context-helper@1.0.0"},
        )
        self.assertTrue(
            all("approvalToken" not in request["params"] for request in package_requests)
        )

    def test_reopens_physical_transcript_through_managed_directory_alias(self) -> None:
        physical_root = self.root / "external-sessions"
        physical_root.mkdir()
        self.runtime.config.session_dir.symlink_to(physical_root, target_is_directory=True)
        session_id = str(self.first["id"])
        # An empty fork may not have a transcript file until its first prompt.
        transcript = physical_root.resolve() / "empty-fork.jsonl"
        self.store.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-empty-fork",
            transcript_ref=str(transcript),
            branch_anchor="first-user",
            binding_state="active",
            metadata={"protocolVersion": "2"},
            message_count=0,
        )

        self.runtime.ensure(session_id)

        requests = [json.loads(line) for line in
                    (self.root / "agent" / "host-requests.jsonl").read_text().splitlines()]
        opened = next(row["params"] for row in requests if row["method"] == "session.open")
        self.assertEqual(opened["sessionFile"], str(self.runtime.config.session_dir / transcript.name))
        self.assertEqual(Path(opened["sessionFile"]).resolve(), transcript)

    def test_session_context_resource_settings_reach_pi_session_open(self) -> None:
        session_id = str(self.first["id"])
        self.store.set_runtime_policy(
            session_id,
            mode="assistant",
            tool_profile_version="control-center-v1",
            allowed_tools=None,
            project_context_enabled=False,
            pi_skills_enabled=True,
            codex_skills_enabled=True,
        )

        self.runtime.ensure(session_id)

        requests = [
            json.loads(line)
            for line in (self.root / "agent" / "host-requests.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        opened = next(request for request in requests if request["method"] == "session.open")
        self.assertTrue(opened["params"]["noContextFiles"])
        self.assertTrue(opened["params"]["piSkillsEnabled"])
        self.assertTrue(opened["params"]["codexSkillsEnabled"])

    def test_candidate_skill_paths_reach_host_only_from_frozen_owner_policy(self) -> None:
        workspace = self.root / "evaluation"
        skill = workspace / ".pi" / "skills" / "candidate"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: candidate\n---\nUse the frozen method.")
        session_id = str(self.first["id"])
        self.store.set_runtime_policy(session_id, mode="coordinator", tool_profile_version="subagent-readonly-v1", execution_mode="read_only", allowed_tools=[], workspace_roots=[str(workspace)], project_context_enabled=False, pi_skills_enabled=True, codex_skills_enabled=False)
        self.runtime._skill_allowlist_provider = lambda _: ["candidate"]
        self.runtime._candidate_skill_paths_provider = lambda _: [str(skill)]
        opened = self.runtime.ensure(session_id)
        self.assertEqual(opened["resourceSnapshot"]["candidateSkillPaths"], [str(skill.resolve())])
        self.runtime.close_session(session_id)
        self.runtime._candidate_skill_paths_provider = lambda _: [str(self.root / "outside")]
        self.runtime.ensure(session_id)
        requests = [json.loads(line) for line in (self.root / "agent" / "host-requests.jsonl").read_text().splitlines()]
        opens = [request["params"] for request in requests if request["method"] == "session.open"]
        self.assertTrue(all(item["candidateSkillPaths"] == [str(skill.resolve())] for item in opens))
        self.assertEqual(opens[0]["provider"], "openai-codex")

    def test_candidate_skill_paths_reject_workspace_escape_and_unsupported_host(self) -> None:
        workspace = self.root / "evaluation"
        workspace.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "SKILL.md").write_text("outside")
        with self.assertRaisesRegex(PiRuntimeError, "isolated Session"):
            self.runtime._candidate_skill_paths([str(outside)], str(workspace))
        linked = workspace / "linked"
        linked.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(PiRuntimeError, "isolated Session"):
            self.runtime._candidate_skill_paths([str(linked)], str(workspace))
        session_id = str(self.first["id"])
        (workspace / "SKILL.md").write_text("not a scoped resource")
        self.runtime._session_context_provider = lambda _: {"candidateSkillPaths": [str(outside)]}
        self.runtime.ensure(session_id)
        requests = [json.loads(line) for line in (self.root / "agent" / "host-requests.jsonl").read_text().splitlines()]
        self.assertNotIn("candidateSkillPaths", next(row["params"] for row in requests if row["method"] == "session.open"))
        isolated = workspace / "skills" / "candidate"
        isolated.mkdir(parents=True)
        (isolated / "SKILL.md").write_text("candidate")
        third = self.store.create(title="candidate", mode="coordinator", tool_profile_version="subagent-readonly-v1", execution_mode="read_only", workspace_roots=[str(workspace)], pi_skills_enabled=True)
        self.runtime._skill_allowlist_provider = lambda _: ["candidate"]
        self.runtime._candidate_skill_paths_provider = lambda _: [str(isolated)]
        self.runtime._host_capabilities["sessionCandidateSkillPaths"] = False
        with self.assertRaisesRegex(PiRuntimeError, "does not support isolated"):
            self.runtime.ensure(str(third["id"]))

    def test_resource_switches_reach_pi_even_when_base_skill_refs_are_frozen(self) -> None:
        session_id = str(self.first["id"])
        self.runtime._skill_allowlist_provider = lambda _: ["systematic-debugging"]
        opened = self.runtime.ensure(session_id)
        self.runtime.close_session(session_id)
        policy = {"disabledSkillNames": ["systematic-debugging"], "disabledPluginIds": ["@paw/example"]}
        self.runtime._session_context_provider = lambda _: {"resourceDisclosurePolicy": policy}
        reopened = self.runtime.ensure(session_id)
        self.assertEqual(opened["resourceSnapshot"], reopened["resourceSnapshot"])
        requests = [json.loads(line) for line in (self.root / "agent" / "host-requests.jsonl").read_text().splitlines()]
        params = [request["params"] for request in requests if request["method"] == "session.open"][-1]
        self.assertEqual(params["resourceDisclosurePolicy"], policy)
        self.assertEqual(params["skillAllowlist"], ["systematic-debugging"])

    def test_session_skill_allowlist_reaches_pi_session_open(self) -> None:
        session_id = str(self.first["id"])
        self.runtime._skill_allowlist_provider = (
            lambda _session: [
                "systematic-debugging",
                "test-driven-implementation",
            ]
        )

        self.runtime.ensure(session_id)

        requests = [
            json.loads(line)
            for line in (self.root / "agent" / "host-requests.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        opened = next(request for request in requests if request["method"] == "session.open")
        self.assertEqual(
            opened["params"]["skillAllowlist"],
            ["systematic-debugging", "test-driven-implementation"],
        )

    def test_session_open_receipt_freezes_skill_refs_across_reuse(self) -> None:
        session_id = str(self.first["id"])
        effective = ["systematic-debugging", "trace-agent-diagnostics"]
        self.runtime._skill_allowlist_provider = lambda _session: list(effective)

        opened = self.runtime.ensure(session_id)
        self.assertEqual(
            opened["resourceSnapshot"],
            {
                "schemaVersion": "rag-ime.pi-session-resource-snapshot.v1",
                "skillPolicy": "allowlist",
                "skillRefs": [
                    "systematic-debugging",
                    "trace-agent-diagnostics",
                ],
            },
        )
        binding = self.store.runtime_binding(session_id)
        assert binding is not None
        self.assertEqual(
            binding["metadata"]["resourceSnapshot"],
            opened["resourceSnapshot"],
        )

        effective[:] = ["systematic-debugging", "facilitate-room"]
        reused = self.runtime.ensure(session_id)

        self.assertTrue(reused["reused"])
        self.assertEqual(reused["resourceSnapshot"], opened["resourceSnapshot"])
        requests = [
            json.loads(line)
            for line in (self.root / "agent" / "host-requests.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(
            [request["method"] for request in requests].count("session.open"),
            1,
        )

        self.runtime.stop()
        reopened = self.runtime.ensure(session_id)
        self.assertFalse(reopened["reused"])
        self.assertEqual(reopened["resourceSnapshot"], opened["resourceSnapshot"])
        requests = [
            json.loads(line)
            for line in (self.root / "agent" / "host-requests.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        session_opens = [
            request for request in requests if request["method"] == "session.open"
        ]
        self.assertEqual(len(session_opens), 2)
        self.assertEqual(
            session_opens[-1]["params"]["skillAllowlist"],
            ["systematic-debugging", "trace-agent-diagnostics"],
        )

    def test_prompt_settings_reach_native_open_and_remain_frozen_after_restart(self) -> None:
        session_id = str(self.first["id"])
        settings = {"systemInstructions": "保持这份系统补充。", "compactionInstructions": "保留未落盘变化。"}
        self.runtime._prompt_settings_provider = lambda _session: dict(settings)
        original = dict(settings)
        opened = self.runtime.ensure(session_id)
        self.assertEqual(opened["resourceSnapshot"]["promptSettings"], original)
        settings.update(systemInstructions="新的系统补充。", compactionInstructions="新的压缩补充。")
        self.runtime.stop()
        reopened = self.runtime.ensure(session_id)
        self.assertEqual(reopened["resourceSnapshot"]["promptSettings"], original)
        requests = [json.loads(line) for line in (self.root / "agent" / "host-requests.jsonl").read_text().splitlines()]
        opens = [request["params"] for request in requests if request["method"] == "session.open"]
        self.assertEqual(len(opens), 2)
        for params in opens:
            self.assertIn(original["systemInstructions"], params["systemPrompt"])
            self.assertNotIn(settings["systemInstructions"], params["systemPrompt"])
            self.assertEqual(params["compactionInstructions"], original["compactionInstructions"])

    def test_prompt_settings_require_a_capable_host_and_preserve_empty_override(self) -> None:
        session_id = str(self.first["id"])
        self.runtime._prompt_settings_provider = lambda _session: {"systemInstructions": "", "compactionInstructions": ""}
        self.runtime._host()
        self.runtime._host_capabilities.pop("sessionPromptSettings", None)
        with self.assertRaisesRegex(PiRuntimeError, "does not support prompt settings"):
            self.runtime.ensure(session_id)
        self.runtime._host_capabilities["sessionPromptSettings"] = True
        self.runtime.ensure(session_id)
        requests = [json.loads(line) for line in (self.root / "agent" / "host-requests.jsonl").read_text().splitlines()]
        opens = [request["params"] for request in requests if request["method"] == "session.open"]
        self.assertEqual(len(opens), 1)
        self.assertIn("compactionInstructions", opens[0])
        self.assertEqual(opens[0]["compactionInstructions"], "")

    def test_existing_binding_does_not_adopt_new_prompt_defaults(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        self.runtime._prompt_settings_provider = lambda _session: {"systemInstructions": "不要混入旧会话", "compactionInstructions": "新默认"}
        self.runtime.stop()
        self.runtime.ensure(session_id)
        requests = [json.loads(line) for line in (self.root / "agent" / "host-requests.jsonl").read_text().splitlines()]
        params = [request["params"] for request in requests if request["method"] == "session.open"][-1]
        self.assertNotIn("不要混入旧会话", params["systemPrompt"])
        self.assertNotIn("compactionInstructions", params)

    def test_session_skill_allowlist_fails_closed_on_an_old_host(self) -> None:
        session_id = str(self.first["id"])
        self.store.set_runtime_policy(
            session_id,
            mode="assistant",
            tool_profile_version="control-center-v1",
            allowed_tools=None,
            project_context_enabled=False,
            pi_skills_enabled=True,
            codex_skills_enabled=False,
        )
        self.runtime._skill_allowlist_provider = (
            lambda _session: ["systematic-debugging"]
        )
        self.runtime._host()
        self.runtime._host_capabilities.pop("sessionSkillAllowlist", None)

        with self.assertRaisesRegex(
            PiRuntimeError,
            "does not support per-Session Skill allowlists",
        ):
            self.runtime.ensure(session_id)

    def test_disabled_skill_systems_omit_the_allowlist_for_an_old_host(self) -> None:
        session_id = str(self.first["id"])
        self.runtime._skill_allowlist_provider = (
            lambda _session: ["systematic-debugging"]
        )
        self.runtime._host()
        self.runtime._host_capabilities.pop("sessionSkillAllowlist", None)

        ensured = self.runtime.ensure(session_id)

        self.assertFalse(ensured["reused"])
        requests = [
            json.loads(line)
            for line in (self.root / "agent" / "host-requests.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        opened = next(request for request in requests if request["method"] == "session.open")
        self.assertNotIn("skillAllowlist", opened["params"])

    def test_ensure_retires_an_idle_turn_restored_after_host_failure(
        self,
    ) -> None:
        session_id = str(self.first["id"])
        recovered_turn_id = "turn-interrupted-before-ack"
        self.store.set_status(
            session_id,
            "faulted",
            last_message_preview="Pi Runtime Host exited",
        )
        self.runtime.config = replace(
            self.runtime.config,
            provider_environment={
                "TEST_SESSION_OPEN_RECOVERED_TURN": recovered_turn_id,
            },
        )

        ensured = self.runtime.ensure(session_id)

        self.assertIsNone(ensured["state"]["activeTurn"])
        self.assertEqual(self.store.get(session_id)["status"], "idle")
        requests = [
            json.loads(line)
            for line in (self.root / "agent" / "host-requests.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        session_methods = [
            request["method"]
            for request in requests
            if request["method"].startswith("session.")
        ]
        self.assertEqual(
            session_methods,
            [
                "session.open",
                "session.control_state",
                "session.abort",
                "session.control_state",
            ],
        )

    def test_warm_ensure_retires_idle_binding_preserved_by_preflight(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.config = replace(self.runtime.config, provider_environment={
            "TEST_SESSION_OPEN_RECOVERED_TURN": "old-idle-turn",
        })
        opened = self.runtime.ensure(session_id, retire_recovered_turn=False)
        self.assertEqual(opened["state"]["activeTurn"]["turnId"], "old-idle-turn")
        preserved = self.runtime.ensure(session_id, retire_recovered_turn=False)
        self.assertEqual(preserved["state"]["activeTurn"]["turnId"], "old-idle-turn")
        recovered = self.runtime.ensure(session_id)
        self.assertTrue(recovered["reused"])
        self.assertTrue(recovered["recoveredTurnRetirement"]["retired"])
        self.assertIsNone(recovered["state"]["activeTurn"])
        self.assertTrue(self.runtime.prompt(session_id, "new task")["accepted"])

    def test_warm_ensure_does_not_retire_a_running_turn(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        client = self.runtime._require_client()
        control = {"schemaVersion": "rag-ime.pi-session-control-state.v1",
                   "sessionId": session_id, "isIdle": False,
                   "activeTurn": {"turnId": "running-turn"}}
        with patch.object(client, "send", return_value=control), patch.object(
            self.runtime, "retire_recovered_turn"
        ) as retire:
            result = self.runtime.ensure(session_id)
        retire.assert_not_called()
        self.assertEqual(result["state"]["activeTurn"]["turnId"], "running-turn")

    def test_retire_recovered_turn_uses_exact_idle_turn_and_confirms_clear(
        self,
    ) -> None:
        session_id = str(self.first["id"])
        recovered_turn_id = "turn-recovered-resident"
        self.runtime.ensure(session_id)
        client = self.runtime._require_client()
        methods: list[str] = []
        stale = True

        def recovered_send(
            method: str,
            params: dict[str, object] | None = None,
            *,
            timeout: float | None = None,
            before_write=None,
        ) -> dict[str, object]:
            nonlocal stale
            methods.append(method)
            if method == "session.control_state":
                return {
                    "schemaVersion": "rag-ime.pi-session-control-state.v1",
                    "sessionId": session_id,
                    "isIdle": True,
                    "isCompacting": False,
                    "activeTurn": (
                        {"turnId": recovered_turn_id} if stale else None
                    ),
                    "sequence": 1,
                }
            if method == "session.abort":
                self.assertEqual(params, {"sessionId": session_id})
                stale = False
                return {
                    "schemaVersion": "rag-ime.pi-session-abort-receipt.v1",
                    "sessionId": session_id,
                    "turnId": recovered_turn_id,
                    "cancelledDecisionIds": [],
                    "cancelledUIRequestIds": [],
                    "lifecycle": {
                        "schemaVersion": "pi.agent-abort-receipt.v1",
                        "scopeId": session_id,
                        "generation": 1,
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
            raise AssertionError(f"unexpected Host method: {method}")

        with patch.object(client, "send", side_effect=recovered_send):
            receipt = self.runtime.retire_recovered_turn(
                session_id,
                recovered_turn_id,
            )

        self.assertEqual(
            methods,
            ["session.control_state", "session.abort", "session.control_state"],
        )
        self.assertEqual(receipt["turnId"], recovered_turn_id)
        self.assertTrue(receipt["retired"])
        self.assertIsNone(receipt["state"]["activeTurn"])
        self.assertEqual(self.store.get(session_id)["status"], "idle")

    def test_retire_recovered_turn_rejects_a_different_active_turn(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        client = self.runtime._require_client()
        methods: list[str] = []

        def different_turn(
            method: str,
            params: dict[str, object] | None = None,
            *,
            timeout: float | None = None,
            before_write=None,
        ) -> dict[str, object]:
            methods.append(method)
            if method == "session.control_state":
                return {
                    "schemaVersion": "rag-ime.pi-session-control-state.v1",
                    "sessionId": session_id,
                    "isIdle": True,
                    "isCompacting": False,
                    "activeTurn": {"turnId": "turn-newer"},
                    "sequence": 1,
                }
            raise AssertionError(f"unexpected Host method: {method}")

        with patch.object(client, "send", side_effect=different_turn):
            with self.assertRaisesRegex(
                PiRuntimeError,
                "does not match the expected recovered turn",
            ):
                self.runtime.retire_recovered_turn(
                    session_id,
                    "turn-original-fault",
                )

        self.assertEqual(methods, ["session.control_state"])

    def test_retire_recovered_turn_rejects_unsettled_abort_receipt(self) -> None:
        session_id = str(self.first["id"])
        recovered_turn_id = "turn-recovered-unsettled"
        self.runtime.ensure(session_id)
        client = self.runtime._require_client()

        def unsettled_abort(
            method: str,
            params: dict[str, object] | None = None,
            *,
            timeout: float | None = None,
            before_write=None,
        ) -> dict[str, object]:
            if method == "session.control_state":
                return {
                    "schemaVersion": "rag-ime.pi-session-control-state.v1",
                    "sessionId": session_id,
                    "isIdle": True,
                    "isCompacting": False,
                    "activeTurn": {"turnId": recovered_turn_id},
                    "sequence": 1,
                }
            if method == "session.abort":
                return {
                    "schemaVersion": "rag-ime.pi-session-abort-receipt.v1",
                    "sessionId": session_id,
                    "turnId": recovered_turn_id,
                    "lifecycle": {
                        "schemaVersion": "pi.agent-abort-receipt.v1",
                        "scopeId": session_id,
                        "generation": 1,
                        "reason": "user_abort",
                        "cancelledContinuationIds": [],
                        "cancelledOperationIds": [],
                        "failedOperationIds": [],
                        "operations": [],
                        "pendingOperations": ["provider"],
                        "drained": False,
                        "idle": False,
                    },
                }
            raise AssertionError(f"unexpected Host method: {method}")

        with patch.object(client, "send", side_effect=unsettled_abort):
            with self.assertRaisesRegex(
                PiRuntimeError,
                "invalid recovered Session abort receipt",
            ):
                self.runtime.retire_recovered_turn(
                    session_id,
                    recovered_turn_id,
                )

    def test_coding_tool_projection_keeps_useful_request_and_bounded_output(self) -> None:
        result = public_code_tool_activity(
            "grep",
            {
                "path": "/Users/private/project/rag_ime",
                "pattern": "rime_lexicon_review",
                "glob": "*.py",
                "limit": 100,
                "context": 2,
            },
            {
                "content": [{
                    "type": "text",
                    "text": (
                        "/Users/private/project/rag_ime/agent_tools.py:41:"
                        " def rime_lexicon_review(): pass\n"
                        "OPENAI_API_KEY=sk-never-render-this"
                    ),
                }],
            },
        )

        self.assertEqual(result["path"], "…/project/rag_ime")
        self.assertEqual(result["pattern"], "rime_lexicon_review")
        self.assertEqual(result["glob"], "*.py")
        self.assertEqual(result["limit"], 100)
        self.assertEqual(result["context"], 2)
        self.assertIn("~/project/rag_ime/agent_tools.py:41", result["outputPreview"])
        self.assertIn("OPENAI_API_KEY=[REDACTED_SECRET]", result["outputPreview"])
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("/Users/private", serialized)
        self.assertNotIn("sk-never-render-this", serialized)

    def test_canonical_workspace_tools_keep_meaningful_bounded_details(self) -> None:
        lsp = public_code_tool_activity(
            "workspace_lsp",
            {
                "operation": "diagnostics",
                "root": "/Users/private/project",
                "path": "src/main.py",
                "server": "pyright",
                "line": 12,
                "column": 4,
            },
            {"content": [{"type": "text", "text": "2 diagnostics"}]},
        )
        job = public_code_tool_activity(
            "workspace_job",
            {
                "operation": "logs",
                "jobId": "bg_123",
                "cwd": "/Volumes/private/project",
                "command": "python -m unittest",
                "cursor": 64,
                "limitBytes": 4096,
            },
            {"content": [{"type": "text", "text": "tests complete"}]},
        )
        write = public_code_tool_activity(
            "workspace_write",
            {
                "operation": "apply",
                "path": "src/new_file.py",
                "content": "first\nsecond\n",
            },
        )
        edit = public_code_tool_activity(
            "workspace_edit",
            {
                "operation": "apply",
                "path": "src/main.py",
                "edits": [{
                    "oldText": "first\nsecond\n",
                    "newText": "replacement\n",
                }],
            },
        )

        self.assertEqual(
            {
                key: lsp[key]
                for key in ("path", "root", "operation", "server", "line", "column")
            },
            {
                "path": "src/main.py",
                "root": "…/project",
                "operation": "diagnostics",
                "server": "pyright",
                "line": 12,
                "column": 4,
            },
        )
        self.assertEqual(lsp["outputPreview"], "2 diagnostics")
        self.assertEqual(job["jobId"], "bg_123")
        self.assertEqual(job["cwd"], "…/project")
        self.assertEqual(job["cursor"], 64)
        self.assertEqual(job["limitBytes"], 4096)
        self.assertEqual(job["outputPreview"], "tests complete")
        self.assertEqual(write["summary"], "new_file.py +2")
        self.assertNotIn("content", write)
        self.assertEqual(edit["additions"], 1)
        self.assertEqual(edit["deletions"], 2)
        serialized_edit = json.dumps(edit)
        self.assertNotIn("edits", serialized_edit)
        self.assertNotIn("oldText", serialized_edit)
        self.assertNotIn("newText", serialized_edit)

    def test_coding_tool_projection_unwraps_managed_evidence_receipts(self) -> None:
        evidence_summary = (
            "wisdom-weasel-rag-ime/rag_ime/agent_tools.py:41: "
            "def rime_lexicon_review(): pass"
        )
        result = public_code_tool_activity(
            "grep",
            {},
            {
                "content": [{
                    "type": "text",
                    "text": json.dumps(
                        {
                            "evidenceHandle": "internal-handle-must-not-render",
                            "evidenceRequest": {
                                "path": "/Volumes/private/project/rag_ime",
                                "pattern": "rime_lexicon_review",
                                "limit": 100,
                                "context": 2,
                            },
                            "evidenceSummary": evidence_summary,
                            "evidenceBytes": len(evidence_summary.encode("utf-8")) + 500,
                            "evidenceSha256": "a" * 64,
                        },
                        ensure_ascii=False,
                    ),
                }],
            },
        )

        self.assertEqual(result["path"], "…/project/rag_ime")
        self.assertEqual(result["pattern"], "rime_lexicon_review")
        self.assertEqual(result["limit"], 100)
        self.assertEqual(result["context"], 2)
        self.assertEqual(result["outputPreview"], evidence_summary)
        self.assertTrue(result["outputTruncated"])
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("evidenceHandle", serialized)
        self.assertNotIn("internal-handle", serialized)
        self.assertNotIn("evidenceSha256", serialized)

    def test_coding_tool_projection_redacts_commands_and_never_previews_secret_files(self) -> None:
        command = public_code_tool_activity(
            "bash",
            {
                "command": (
                    "OPENAI_API_KEY=plain-secret python scripts/probe.py "
                    "--token bearer-secret"
                ),
                "timeout": 30,
            },
            {"content": [{"type": "text", "text": "probe complete"}]},
        )
        secret_file = public_code_tool_activity(
            "read",
            {"path": "/Users/private/project/.env", "limit": 100},
            {"content": [{"type": "text", "text": "DATABASE_PASSWORD=do-not-show"}]},
        )

        self.assertEqual(command["timeout"], 30)
        self.assertEqual(
            command["command"],
            (
                "OPENAI_API_KEY=[REDACTED_SECRET] python scripts/probe.py "
                "--token [REDACTED_SECRET]"
            ),
        )
        self.assertEqual(command["outputPreview"], "probe complete")
        self.assertNotIn("outputPreview", secret_file)
        self.assertNotIn("do-not-show", json.dumps(secret_file))

    def test_host_coding_tool_event_keeps_public_projection_and_inspectable_result(self) -> None:
        session_id = str(self.first["id"])
        turn_id = "turn-grep-projection"
        self.runtime._handle_host_event(  # noqa: SLF001 - protocol boundary
            {
                "protocolVersion": "2",
                "event": "agent.event",
                "sessionId": session_id,
                "turnId": turn_id,
                "payload": {
                    "type": "tool_execution_update",
                    "toolCallId": "call-grep-progress-projection",
                    "toolName": "grep",
                    "args": {"path": "rag_ime", "pattern": "provider_payload"},
                    "partialResult": {
                        "content": [{
                            "type": "text",
                            "text": "rag_ime/pi_runtime.py:90:provider_payload pending",
                        }],
                    },
                    "isError": False,
                },
            }
        )
        self.runtime._handle_host_event(  # noqa: SLF001 - protocol boundary
            {
                "protocolVersion": "2",
                "event": "agent.event",
                "sessionId": session_id,
                "turnId": turn_id,
                "payload": {
                    "type": "tool_execution_end",
                    "toolCallId": "call-grep-projection",
                    "toolName": "grep",
                    "args": {
                        "path": "/Users/private/project/rag_ime",
                        "pattern": "provider_payload",
                        "limit": 100,
                    },
                    "result": {
                        "content": [{
                            "type": "text",
                            "text": "rag_ime/pi_runtime.py:100:def provider_payload():",
                        }],
                        "details": {
                            "sourcePath": "/Users/private/project/rag_ime/pi_runtime.py",
                            "accessToken": "do-not-render-this-token",
                        },
                    },
                    "isError": False,
                },
            }
        )

        events, gap = self.events.replay(session_id)
        self.assertFalse(gap)
        progress_event = next(
            item
            for item in events
            if item.event_type == "tool_progress"
            and item.payload.get("toolCallId") == "call-grep-progress-projection"
        )
        self.assertEqual(
            progress_event.payload["publicResult"]["outputPreview"],
            "rag_ime/pi_runtime.py:90:provider_payload pending",
        )
        self.assertNotIn("partialResult", progress_event.payload)
        event = next(
            item
            for item in events
            if item.event_type == "tool_finished"
            and item.payload.get("toolCallId") == "call-grep-projection"
        )
        self.assertEqual(
            event.payload["publicResult"]["outputPreview"],
            "rag_ime/pi_runtime.py:100:def provider_payload():",
        )
        self.assertEqual(
            event.payload["result"]["content"][0]["text"],
            "rag_ime/pi_runtime.py:100:def provider_payload():",
        )
        self.assertEqual(
            event.payload["result"]["details"]["sourcePath"],
            "/Users/private/project/rag_ime/pi_runtime.py",
        )
        self.assertEqual(
            event.payload["result"]["details"]["accessToken"],
            "[REDACTED_SECRET]",
        )

    def test_host_bash_receipt_exit_status_is_the_authoritative_tool_result(self) -> None:
        session_id = str(self.first["id"])
        turn_id = "turn-bash-exit-status"

        cases = (
            ("success", {"exitCode": 0, "timedOut": False}, False),
            ("failed", {"exitCode": 1, "timedOut": False}, True),
            ("timeout", {"exitCode": -15, "timedOut": True}, True),
        )
        for label, receipt, expected_error in cases:
            self.runtime._handle_host_event(  # noqa: SLF001 - protocol boundary
                {
                    "protocolVersion": "2",
                    "event": "agent.event",
                    "sessionId": session_id,
                    "turnId": turn_id,
                    "payload": {
                        "type": "tool_execution_end",
                        "toolCallId": f"call-bash-{label}",
                        "toolName": "bash",
                        "args": {},
                        "result": {
                            "content": [{
                                "type": "text",
                                "text": (
                                    "FAILED is ordinary command output"
                                    if label == "success"
                                    else f"command {label}"
                                ),
                            }],
                            "details": {"receipt": receipt},
                        },
                        # Backend coding tools currently return a normal Pi
                        # ToolResult even when their structured command receipt
                        # records a non-zero exit. The receipt, not output text,
                        # is the command's terminal authority.
                        "isError": False,
                    },
                }
            )

        # Pi-originated interruption already carries isError and must remain a
        # failure even when there is no command receipt to inspect.
        self.runtime._handle_host_event(  # noqa: SLF001 - protocol boundary
            {
                "protocolVersion": "2",
                "event": "agent.event",
                "sessionId": session_id,
                "turnId": turn_id,
                "payload": {
                    "type": "tool_execution_end",
                    "toolCallId": "call-bash-aborted",
                    "toolName": "bash",
                    "args": {},
                    "result": {
                        "content": [{"type": "text", "text": "Command aborted"}],
                    },
                    "isError": True,
                },
            }
        )

        events = {
            str(item.payload.get("toolCallId")): item
            for item in self.events.replay(session_id)[0]
            if item.event_type == "tool_finished"
            and item.turn_id == turn_id
        }
        self.assertFalse(events["call-bash-success"].payload["isError"])
        for label in ("failed", "timeout", "aborted"):
            with self.subTest(label=label):
                payload = events[f"call-bash-{label}"].payload
                self.assertTrue(payload["isError"])
                self.assertEqual(
                    payload["publicResult"]["error"],
                    payload["publicResult"]["outputPreview"],
                )

    def test_host_projects_codex_thinking_end_as_public_work_summary(self) -> None:
        session_id = str(self.first["id"])
        turn_id = "turn-codex-reasoning"
        self.runtime._handle_host_event(  # noqa: SLF001 - protocol boundary
            {
                "protocolVersion": "2",
                "event": "agent.event",
                "sessionId": session_id,
                "turnId": turn_id,
                "payload": {
                    "type": "message_update",
                    "assistantMessageEvent": {
                        "type": "thinking_end",
                        "contentIndex": 0,
                    },
                    "message": {
                        "id": "assistant-codex-reasoning",
                        "role": "assistant",
                        "api": "openai-codex-responses",
                        "content": [{
                            "type": "thinking",
                            "thinking": (
                                "**Inspecting /Users/private/project/runtime.py**\n\n"
                                "**Planning the visible fix**"
                            ),
                            "thinkingSignature": "private-provider-signature",
                        }],
                    },
                },
            }
        )

        events, gap = self.events.replay(session_id)
        self.assertFalse(gap)
        reasoning = [
            event for event in events
            if event.event_type == "reasoning_summary"
            and event.turn_id == turn_id
        ]
        self.assertEqual(len(reasoning), 1)
        self.assertEqual(reasoning[0].payload["summary"], "Planning the visible fix")
        self.assertEqual(
            reasoning[0].payload["items"],
            ["Inspecting [REDACTED_PATH]", "Planning the visible fix"],
        )
        self.assertEqual(
            reasoning[0].payload["source"],
            "provider_reasoning_summary",
        )
        self.assertNotIn(
            "private-provider-signature",
            json.dumps(reasoning[0].payload, ensure_ascii=False),
        )

    def test_cold_history_reads_managed_jsonl_without_opening_provider_context(self) -> None:
        session_id = str(self.first["id"])
        transcript = self.root / "sessions" / "cold-history.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        entries = [
            {"type": "session", "id": "pi-cold-history"},
            {
                "type": "message",
                "id": "cold-user",
                "parentId": "",
                "timestamp": "1970-01-01T00:00:00.100Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "冷启动历史问题"}],
                },
            },
            {
                "type": "message",
                "id": "cold-assistant",
                "parentId": "cold-user",
                "timestamp": "1970-01-01T00:00:00.200Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "冷启动历史回答"}],
                },
            },
        ]
        transcript.write_text(
            "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
            encoding="utf-8",
        )
        self.store.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-cold-history",
            transcript_ref=transcript.as_posix(),
            branch_anchor="cold-assistant",
            binding_state="active",
            metadata={"protocolVersion": "2"},
            message_count=2,
        )
        context_calls: list[str] = []
        self.runtime._session_context_provider = (  # noqa: SLF001 - latency contract
            lambda session: context_calls.append(str(session["id"])) or {}
        )

        with patch.object(
            self.runtime,
            "_host",
            side_effect=AssertionError("history must not start the Runtime Host"),
        ):
            snapshot = self.runtime.session_snapshot(session_id)

        self.assertEqual(
            [
                block["data"]["text"]
                for message in snapshot["messages"]
                for block in message["blocks"]
                if block["type"] == "text"
            ],
            ["冷启动历史问题", "冷启动历史回答"],
        )
        self.assertEqual(context_calls, [])
        self.assertFalse((self.root / "agent" / "host-requests.jsonl").exists())

    def test_recent_snapshot_returns_last_complete_selected_branch_turns_without_host_or_tools(
        self,
    ) -> None:
        session_id = str(self.first["id"])
        transcript = self.root / "sessions" / "recent-selected-branch.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        entries: list[dict[str, object]] = [
            {"type": "session", "id": "pi-recent-selected"},
        ]
        parent_id = "pi-recent-selected"
        for index in range(8):
            user_id = f"recent-user-{index + 1}"
            answer_id = f"recent-answer-{index + 1}"
            entries.extend([
                {
                    "type": "message",
                    "id": user_id,
                    "parentId": parent_id,
                    "timestamp": 100 + index * 10,
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": f"问题 {index + 1}"}],
                    },
                },
                {
                    "type": "message",
                    "id": answer_id,
                    "parentId": user_id,
                    "timestamp": 101 + index * 10,
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": f"回答 {index + 1}"}],
                    },
                },
            ])
            parent_id = answer_id
        entries.extend([
            {
                "type": "message",
                "id": "abandoned-answer",
                "parentId": "recent-user-2",
                "timestamp": 999,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "未选择的分支"}],
                },
            },
            {
                "type": "message",
                "id": "recent-dangling-user",
                "parentId": parent_id,
                "timestamp": 1_000,
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "尚未完成的问题"}],
                },
            },
        ])
        transcript.write_text(
            "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
            encoding="utf-8",
        )
        self.store.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-recent-selected",
            transcript_ref=transcript.as_posix(),
            branch_anchor="recent-dangling-user",
            binding_state="active",
            metadata={
                "protocolVersion": "2",
                "codemodeAvailable": True,
                "codemodeMode": "only",
            },
            message_count=17,
        )

        with (
            patch.object(
                self.runtime,
                "_host",
                side_effect=AssertionError("recent snapshot must not start the Host"),
            ),
            patch.object(
                self.runtime,
                "session_snapshot",
                side_effect=AssertionError("recent snapshot must not use full history"),
            ),
        ):
            snapshot = self.runtime.recent_session_snapshot(session_id)

        texts = [
            block["data"]["text"]
            for message in snapshot["messages"]
            for block in message["blocks"]
            if block["type"] == "text"
        ]
        self.assertEqual(
            texts,
            [
                "问题 1", "回答 1", "问题 2", "回答 2",
                "问题 3", "回答 3", "问题 4", "回答 4",
                "问题 5", "回答 5", "问题 6", "回答 6",
                "问题 7", "回答 7", "问题 8", "回答 8",
                "尚未完成的问题",
            ],
        )
        self.assertEqual(snapshot["toolHistoryEvents"], [])
        self.assertEqual(snapshot["codemodeMode"], "only")
        self.assertNotIn("未选择的分支", json.dumps(snapshot, ensure_ascii=False))

    def test_recent_snapshot_parses_only_a_bounded_tail_of_a_large_linear_transcript(
        self,
    ) -> None:
        session_id = str(self.first["id"])
        transcript = self.root / "sessions" / "recent-large-linear.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        entries: list[dict[str, object]] = [
            {"type": "session", "id": "pi-recent-large-linear"},
        ]
        parent_id = "pi-recent-large-linear"
        for index in range(25_000):
            entry_id = f"large-context-{index + 1}"
            entries.append({
                "type": "custom",
                "id": entry_id,
                "parentId": parent_id,
                "payload": "x" * 256,
            })
            parent_id = entry_id
        entries.append({
            "type": "compaction",
            "id": "large-compaction-boundary",
            "parentId": parent_id,
            "summary": "older context compacted",
        })
        parent_id = "large-compaction-boundary"
        for index in range(8):
            user_id = f"large-user-{index + 1}"
            answer_id = f"large-answer-{index + 1}"
            entries.extend([
                {
                    "type": "message",
                    "id": user_id,
                    "parentId": parent_id,
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": f"大对话问题 {index + 1}"}],
                    },
                },
                {
                    "type": "message",
                    "id": answer_id,
                    "parentId": user_id,
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": f"大对话回答 {index + 1}"}],
                    },
                },
            ])
            parent_id = answer_id
        transcript.write_text(
            "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
            encoding="utf-8",
        )
        self.store.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-recent-large-linear",
            transcript_ref=transcript.as_posix(),
            branch_anchor=parent_id,
            binding_state="active",
            metadata={"protocolVersion": "2"},
            message_count=16,
        )
        real_loads = json.loads
        parsed_line_count = 0

        def counted_loads(value, *args, **kwargs):
            nonlocal parsed_line_count
            parsed_line_count += 1
            return real_loads(value, *args, **kwargs)

        with patch("rag_ime.pi.runtime.json.loads", side_effect=counted_loads):
            snapshot = self.runtime.recent_session_snapshot(session_id)

        texts = [
            block["data"]["text"]
            for message in snapshot["messages"]
            for block in message["blocks"]
            if block["type"] == "text"
        ]
        self.assertEqual(
            texts,
            [
                "大对话问题 1", "大对话回答 1",
                "大对话问题 2", "大对话回答 2",
                "大对话问题 3", "大对话回答 3",
                "大对话问题 4", "大对话回答 4",
                "大对话问题 5", "大对话回答 5",
                "大对话问题 6", "大对话回答 6",
                "大对话问题 7", "大对话回答 7",
                "大对话问题 8", "大对话回答 8",
            ],
        )
        # The authorized 24-turn window reads a larger bounded tail than the
        # former six-turn contract while remaining far below a full scan.
        self.assertLess(parsed_line_count, 15_000)

    def test_recent_snapshot_falls_back_when_selected_branch_anchor_is_outside_tail(
        self,
    ) -> None:
        session_id = str(self.first["id"])
        transcript = self.root / "sessions" / "recent-anchor-fallback.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        entries: list[dict[str, object]] = [
            {"type": "session", "id": "pi-recent-anchor-fallback"},
            {
                "type": "message",
                "id": "selected-user",
                "parentId": "pi-recent-anchor-fallback",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "保留的分支问题"}],
                },
            },
            {
                "type": "message",
                "id": "selected-answer",
                "parentId": "selected-user",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "保留的分支回答"}],
                },
            },
            {
                "type": "message",
                "id": "abandoned-user",
                "parentId": "pi-recent-anchor-fallback",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "未选择的分支问题"}],
                },
            },
        ]
        parent_id = "abandoned-user"
        for index in range(20):
            entry_id = f"abandoned-context-{index + 1}"
            entries.append({
                "type": "custom",
                "id": entry_id,
                "parentId": parent_id,
                "payload": "x" * 256,
            })
            parent_id = entry_id
        transcript.write_text(
            "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
            encoding="utf-8",
        )
        self.store.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-recent-anchor-fallback",
            transcript_ref=transcript.as_posix(),
            branch_anchor="selected-answer",
            binding_state="active",
            metadata={"protocolVersion": "2"},
            message_count=2,
        )

        with patch(
            "rag_ime.pi.transcript_io._RECENT_SESSION_TAIL_SCAN_BYTES",
            512,
        ):
            snapshot = self.runtime.recent_session_snapshot(session_id)

        texts = [
            block["data"]["text"]
            for message in snapshot["messages"]
            for block in message["blocks"]
            if block["type"] == "text"
        ]
        self.assertEqual(texts, ["保留的分支问题", "保留的分支回答"])
        self.assertNotIn("未选择的分支问题", texts)

    def test_recent_snapshot_repairs_outside_tail_anchor_once_then_reuses_projection(
        self,
    ) -> None:
        session_id = str(self.first["id"])
        transcript = self.root / "sessions" / "recent-anchor-projection.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        entries: list[dict[str, object]] = [
            {"type": "session", "id": "pi-recent-anchor-projection"},
            {
                "type": "message",
                "id": "projected-user",
                "parentId": "pi-recent-anchor-projection",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "投影保留的问题"}],
                },
            },
            {
                "type": "message",
                "id": "projected-answer",
                "parentId": "projected-user",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "投影保留的回答"}],
                },
            },
        ]
        parent_id = "pi-recent-anchor-projection"
        for index in range(20):
            entry_id = f"projection-abandoned-{index + 1}"
            entries.append({
                "type": "custom",
                "id": entry_id,
                "parentId": parent_id,
                "payload": "x" * 256,
            })
            parent_id = entry_id
        transcript.write_text(
            "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
            encoding="utf-8",
        )
        self.store.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-recent-anchor-projection",
            transcript_ref=transcript.as_posix(),
            branch_anchor="projected-answer",
            binding_state="active",
            metadata={"protocolVersion": "2"},
            message_count=2,
        )

        with (
            patch(
                "rag_ime.pi.transcript_io._RECENT_SESSION_TAIL_SCAN_BYTES",
                512,
            ),
            patch.object(
                self.runtime,
                "_durable_history_snapshot",
                wraps=self.runtime._durable_history_snapshot,
            ) as full_snapshot,
        ):
            first = self.runtime.recent_session_snapshot(session_id)
            with patch(
                "rag_ime.pi.runtime.read_recent_transcript_tail",
                side_effect=AssertionError(
                    "persisted projection must avoid a second JSONL scan"
                ),
            ):
                second = self.runtime.recent_session_snapshot(session_id)

        self.assertEqual(first, second)
        self.assertEqual(full_snapshot.call_count, 1)

    def test_terminal_settlement_advances_branch_cursor_before_recent_read(self) -> None:
        session_id = str(self.first["id"])
        turn_id = "turn-terminal-recent-refresh"
        transcript = self.root / "sessions" / "terminal-recent-refresh.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        entries: list[dict[str, object]] = [
            {"type": "session", "id": "pi-terminal-recent-refresh"},
            {
                "type": "message",
                "id": "old-user",
                "parentId": "pi-terminal-recent-refresh",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "上一轮问题"}],
                },
            },
            {
                "type": "message",
                "id": "old-answer",
                "parentId": "old-user",
                "message": {
                    "role": "assistant",
                    "stopReason": "stop",
                    "content": [{"type": "text", "text": "上一轮回答"}],
                },
            },
            {
                "type": "custom",
                "id": "old-settlement",
                "parentId": "old-answer",
                "customType": "rag-ime.pi-turn-settlement",
                "data": {
                    "schemaVersion": "rag-ime.pi-turn-settlement.v1",
                    "turnId": "turn-old",
                },
            },
        ]
        transcript.write_text(
            "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
            encoding="utf-8",
        )
        self.store.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-terminal-recent-refresh",
            transcript_ref=transcript.as_posix(),
            branch_anchor="old-settlement",
            binding_state="active",
            metadata={"protocolVersion": "2"},
            message_count=2,
        )
        initial = self.runtime.recent_session_snapshot(session_id)
        self.assertEqual(
            [
                block["data"]["text"]
                for message in initial["messages"]
                for block in message["blocks"]
                if block["type"] == "text"
            ],
            ["上一轮问题", "上一轮回答"],
        )

        new_entries = [
            {
                "type": "custom",
                "id": "new-binding",
                "parentId": "old-settlement",
                "customType": "rag-ime.pi-turn-binding",
                "data": {
                    "schemaVersion": "rag-ime.pi-turn-binding.v1",
                    "turnId": turn_id,
                    "clientMessageId": "client-terminal-recent-refresh",
                },
            },
            {
                "type": "message",
                "id": "new-user",
                "parentId": "new-binding",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "最新截图验收"}],
                },
            },
            {
                "type": "message",
                "id": "new-answer",
                "parentId": "new-user",
                "message": {
                    "role": "assistant",
                    "stopReason": "stop",
                    "content": [{"type": "text", "text": "screenshot HTTP500"}],
                },
            },
            {
                "type": "custom",
                "id": "new-settlement",
                "parentId": "new-answer",
                "customType": "rag-ime.pi-turn-settlement",
                "data": {
                    "schemaVersion": "rag-ime.pi-turn-settlement.v1",
                    "turnId": turn_id,
                },
            },
        ]
        with transcript.open("a", encoding="utf-8") as target:
            target.write(
                "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in new_entries)
            )
        assistant = new_entries[2]["message"]
        self.runtime._states[session_id] = _HostedSessionState(
            turn_id=turn_id,
            last_agent_messages=[assistant],
        )

        self.runtime._handle_host_event({
            "protocolVersion": "2",
            "event": "agent.event",
            "sessionId": session_id,
            "turnId": turn_id,
            "payload": {"type": "agent_settled"},
        })

        binding = self.store.runtime_binding(session_id)
        assert binding is not None
        self.assertEqual(binding["branchAnchor"], "new-settlement")
        recent = self.runtime.recent_session_snapshot(session_id)
        texts = [
            block["data"]["text"]
            for message in recent["messages"]
            for block in message["blocks"]
            if block["type"] == "text"
        ]
        self.assertEqual(texts[-2:], ["最新截图验收", "screenshot HTTP500"])
        self.assertTrue(recent["projectionCurrent"])
        projection = self.store.recent_message_projection(session_id)
        assert projection is not None
        self.assertEqual(projection["branchAnchor"], "new-settlement")
        self.assertEqual(
            [
                block["data"]["text"]
                for message in projection["messages"]
                for block in message["blocks"]
                if block["type"] == "text"
            ][-2:],
            ["最新截图验收", "screenshot HTTP500"],
        )

    def test_cold_recent_read_recovers_terminal_branch_without_physical_last_row(
        self,
    ) -> None:
        session_id = str(self.first["id"])
        transcript = self.root / "sessions" / "cold-terminal-branch.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        entries: list[dict[str, object]] = [
            {"type": "session", "id": "pi-cold-terminal-branch"},
            {
                "type": "message",
                "id": "old-user",
                "parentId": "pi-cold-terminal-branch",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "旧问题"}],
                },
            },
            {
                "type": "message",
                "id": "old-answer",
                "parentId": "old-user",
                "message": {
                    "role": "assistant",
                    "stopReason": "stop",
                    "content": [{"type": "text", "text": "旧回答"}],
                },
            },
            {
                "type": "custom",
                "id": "old-settlement",
                "parentId": "old-answer",
                "customType": "rag-ime.pi-turn-settlement",
                "data": {
                    "schemaVersion": "rag-ime.pi-turn-settlement.v1",
                    "turnId": "old-turn",
                },
            },
        ]
        transcript.write_text(
            "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
            encoding="utf-8",
        )
        self.store.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-cold-terminal-branch",
            transcript_ref=transcript.as_posix(),
            branch_anchor="old-settlement",
            binding_state="active",
            message_count=2,
            updated_at_ms=100,
        )
        initial = self.runtime.recent_session_snapshot(session_id)
        new_entries = [
            {
                "type": "custom",
                "id": "new-binding",
                "parentId": "old-settlement",
                "customType": "rag-ime.pi-turn-binding",
                "data": {
                    "schemaVersion": "rag-ime.pi-turn-binding.v1",
                    "turnId": "new-turn",
                    "clientMessageId": "client-new-turn",
                },
            },
            {
                "type": "message",
                "id": "new-user",
                "parentId": "new-binding",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "最新问题"}],
                },
            },
            {
                "type": "message",
                "id": "new-answer",
                "parentId": "new-user",
                "message": {
                    "role": "assistant",
                    "stopReason": "stop",
                    "content": [{"type": "text", "text": "最新回答"}],
                },
            },
            {
                "type": "custom",
                "id": "new-settlement",
                "parentId": "new-answer",
                "customType": "rag-ime.pi-turn-settlement",
                "data": {
                    "schemaVersion": "rag-ime.pi-turn-settlement.v1",
                    "turnId": "new-turn",
                },
            },
            {
                "type": "message",
                "id": "fork-user",
                "parentId": "old-settlement",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "分叉问题"}],
                },
            },
            {
                "type": "message",
                "id": "fork-answer",
                "parentId": "fork-user",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "分叉回答"}],
                },
            },
        ]
        with transcript.open("a", encoding="utf-8") as target:
            target.write(
                "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in new_entries)
            )

        # Simulate a prior repair that saved the old branch against the new
        # file identity.  A fresh runtime must consult the durable settlement
        # instead of returning this exact-looking stale cache.
        identity = self.runtime._recent_projection_identity(session_id)
        assert identity is not None
        self.store.save_recent_message_projection(
            session_id,
            transcript_ref=str(identity["transcriptRef"]),
            external_session_id=str(identity["externalSessionId"]),
            branch_anchor="old-settlement",
            transcript_device=int(identity["transcriptDevice"]),
            transcript_inode=int(identity["transcriptInode"]),
            transcript_size=int(identity["transcriptSize"]),
            transcript_mtime_ns=int(identity["transcriptMtimeNs"]),
            transcript_boundary_sha256=str(identity["transcriptBoundarySha256"]),
            messages=list(initial["messages"]),
            tool_history_events=list(initial.get("toolHistoryEvents") or []),
        )
        generation = int(self.store.runtime_binding(session_id)["generation"])
        config = self.runtime.config
        self.runtime.stop()
        self.runtime = PiRuntimeHostManager(
            config=config,
            sessions=self.store,
            events=self.events,
        )

        with patch.object(
            self.runtime,
            "_host",
            side_effect=AssertionError("cold read must not start the Host"),
        ):
            recovered = self.runtime.recent_session_snapshot(session_id)

        texts = [
            block["data"]["text"]
            for message in recovered["messages"]
            for block in message["blocks"]
            if block["type"] == "text"
        ]
        self.assertEqual(texts, ["旧问题", "旧回答", "最新问题", "最新回答"])
        self.assertNotIn("分叉回答", texts)
        self.assertTrue(recovered["projectionCurrent"])
        binding = self.store.runtime_binding(session_id)
        assert binding is not None
        self.assertEqual(binding["branchAnchor"], "new-settlement")
        self.assertEqual(binding["generation"], generation)
        projection = self.store.recent_message_projection(session_id)
        assert projection is not None
        self.assertEqual(projection["branchAnchor"], "new-settlement")

    def test_large_append_returns_projection_while_one_background_repair_runs(
        self,
    ) -> None:
        session_id = str(self.first["id"])
        transcript = self.root / "sessions" / "recent-large-append-projection.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        entries: list[dict[str, object]] = [
            {"type": "session", "id": "pi-recent-large-append"},
        ]
        parent_id = "pi-recent-large-append"
        for index in range(8):
            user_id = f"initial-user-{index + 1}"
            answer_id = f"initial-answer-{index + 1}"
            entries.extend([
                {
                    "type": "message",
                    "id": user_id,
                    "parentId": parent_id,
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": f"初始问题 {index + 1}"}],
                    },
                },
                {
                    "type": "message",
                    "id": answer_id,
                    "parentId": user_id,
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": f"初始回答 {index + 1}"}],
                    },
                },
            ])
            parent_id = answer_id
        transcript.write_text(
            "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
            encoding="utf-8",
        )
        self.store.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-recent-large-append",
            transcript_ref=transcript.as_posix(),
            branch_anchor=parent_id,
            binding_state="active",
            metadata={"protocolVersion": "2"},
            message_count=16,
        )
        initial = self.runtime.recent_session_snapshot(session_id)

        appended: list[dict[str, object]] = []
        for index in range(2_300):
            entry_id = f"large-append-tool-{index + 1}"
            appended.append({
                "type": "custom",
                "id": entry_id,
                "parentId": parent_id,
                "payload": "x" * 1_024,
            })
            parent_id = entry_id
        appended.extend([
            {
                "type": "message",
                "id": "large-append-user",
                "parentId": parent_id,
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "大追加问题"}],
                },
            },
            {
                "type": "message",
                "id": "large-append-answer",
                "parentId": "large-append-user",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "大追加回答"}],
                },
            },
        ])
        with transcript.open("a", encoding="utf-8") as target:
            target.write(
                "".join(
                    json.dumps(entry, ensure_ascii=False) + "\n"
                    for entry in appended
                )
            )
        self.assertGreater(transcript.stat().st_size, 2 * 1024 * 1024)

        real_full_snapshot = self.runtime._durable_history_snapshot
        real_projection_save = self.runtime._save_recent_message_projection
        full_started = threading.Event()
        release_full = threading.Event()
        projection_saved = threading.Event()
        caller_returned = threading.Event()
        caller_result: list[dict[str, object]] = []
        errors: list[BaseException] = []
        full_thread_ids: list[int] = []

        def blocked_full_snapshot(target_session_id: str):
            full_thread_ids.append(threading.get_ident())
            full_started.set()
            if not release_full.wait(timeout=5):
                raise TimeoutError("test did not release projection repair")
            return real_full_snapshot(target_session_id)

        def observed_projection_save(*args, **kwargs):
            saved = real_projection_save(*args, **kwargs)
            projection_saved.set()
            return saved

        def read_recent() -> None:
            try:
                caller_result.append(
                    self.runtime.recent_session_snapshot(session_id)
                )
            except BaseException as exc:
                errors.append(exc)
            finally:
                caller_returned.set()

        with (
            patch.object(
                self.runtime,
                "_durable_history_snapshot",
                side_effect=blocked_full_snapshot,
            ),
            patch.object(
                self.runtime,
                "_save_recent_message_projection",
                side_effect=observed_projection_save,
            ),
        ):
            caller = threading.Thread(target=read_recent)
            caller.start()
            self.assertTrue(full_started.wait(timeout=2))
            returned_while_repair_blocked = caller_returned.wait(timeout=0.5)
            release_full.set()
            caller.join(timeout=5)
            self.assertTrue(projection_saved.wait(timeout=5))

        self.assertTrue(returned_while_repair_blocked)
        self.assertFalse(caller.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(caller_result, [{**initial, "projectionCurrent": False}])
        self.assertNotEqual(full_thread_ids, [caller.ident])

        repaired = self.runtime.recent_session_snapshot(session_id)
        repaired_texts = [
            block["data"]["text"]
            for message in repaired["messages"]
            for block in message["blocks"]
            if block["type"] == "text"
        ]
        self.assertEqual(repaired_texts[-2:], ["大追加问题", "大追加回答"])
        replayed, _gap = self.events.replay(session_id)
        self.assertTrue(any(
            event.event_type == "snapshot_required"
            and event.payload.get("reason") == "recent_projection_refreshed"
            for event in replayed
        ))

    def test_first_recent_read_returns_proven_suffix_while_legacy_repair_runs(
        self,
    ) -> None:
        session_id = str(self.first["id"])
        transcript = self.root / "sessions" / "recent-first-provisional.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        entries: list[dict[str, object]] = [
            {"type": "session", "id": "pi-recent-first-provisional"},
        ]
        parent_id = "pi-recent-first-provisional"
        for index in range(7):
            user_id = f"legacy-user-{index + 1}"
            answer_id = f"legacy-answer-{index + 1}"
            entries.extend([
                {
                    "type": "message",
                    "id": user_id,
                    "parentId": parent_id,
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": f"旧问题 {index + 1}"}],
                    },
                },
                {
                    "type": "message",
                    "id": answer_id,
                    "parentId": user_id,
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": f"旧回答 {index + 1}"}],
                    },
                },
            ])
            parent_id = answer_id
        for index in range(2_300):
            entry_id = f"legacy-tool-{index + 1}"
            entries.append({
                "type": "custom",
                "id": entry_id,
                "parentId": parent_id,
                "payload": "x" * 1_024,
            })
            parent_id = entry_id
        entries.extend([
            {
                "type": "message",
                "id": "provisional-user",
                "parentId": parent_id,
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "立即可见的问题"}],
                },
            },
            {
                "type": "message",
                "id": "provisional-answer",
                "parentId": "provisional-user",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "立即可见的回答"}],
                },
            },
        ])
        transcript.write_text(
            "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
            encoding="utf-8",
        )
        self.store.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-recent-first-provisional",
            transcript_ref=transcript.as_posix(),
            branch_anchor="provisional-answer",
            binding_state="active",
            metadata={"protocolVersion": "2"},
            message_count=16,
        )

        real_full_snapshot = self.runtime._durable_history_snapshot
        real_projection_save = self.runtime._save_recent_message_projection
        full_started = threading.Event()
        release_full = threading.Event()
        projection_saved = threading.Event()
        caller_returned = threading.Event()
        caller_result: list[dict[str, object]] = []

        def blocked_full_snapshot(target_session_id: str):
            full_started.set()
            if not release_full.wait(timeout=5):
                raise TimeoutError("test did not release first projection repair")
            return real_full_snapshot(target_session_id)

        def observed_projection_save(*args, **kwargs):
            saved = real_projection_save(*args, **kwargs)
            projection_saved.set()
            return saved

        def read_recent() -> None:
            caller_result.append(self.runtime.recent_session_snapshot(session_id))
            caller_returned.set()

        with (
            patch.object(
                self.runtime,
                "_durable_history_snapshot",
                side_effect=blocked_full_snapshot,
            ),
            patch.object(
                self.runtime,
                "_save_recent_message_projection",
                side_effect=observed_projection_save,
            ),
        ):
            caller = threading.Thread(target=read_recent)
            caller.start()
            self.assertTrue(full_started.wait(timeout=2))
            returned_while_repair_blocked = caller_returned.wait(timeout=0.5)
            release_full.set()
            caller.join(timeout=5)
            self.assertTrue(projection_saved.wait(timeout=5))

        self.assertTrue(returned_while_repair_blocked)
        provisional_texts = [
            block["data"]["text"]
            for message in caller_result[0]["messages"]
            for block in message["blocks"]
            if block["type"] == "text"
        ]
        self.assertEqual(
            provisional_texts,
            ["立即可见的问题", "立即可见的回答"],
        )

        repaired = self.runtime.recent_session_snapshot(session_id)
        repaired_texts = [
            block["data"]["text"]
            for message in repaired["messages"]
            for block in message["blocks"]
            if block["type"] == "text"
        ]
        self.assertEqual(
            repaired_texts,
            [
                "旧问题 1", "旧回答 1", "旧问题 2", "旧回答 2",
                "旧问题 3", "旧回答 3", "旧问题 4", "旧回答 4",
                "旧问题 5", "旧回答 5", "旧问题 6", "旧回答 6",
                "旧问题 7", "旧回答 7",
                "立即可见的问题", "立即可见的回答",
            ],
        )

    def test_recent_snapshot_projection_invalidates_for_append_and_branch_rewrite(
        self,
    ) -> None:
        session_id = str(self.first["id"])
        transcript = self.root / "sessions" / "recent-projection-invalidation.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        entries: list[dict[str, object]] = [
            {"type": "session", "id": "pi-recent-projection-invalidation"},
            {
                "type": "message",
                "id": "base-user",
                "parentId": "pi-recent-projection-invalidation",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "基础问题"}],
                },
            },
            {
                "type": "message",
                "id": "base-answer",
                "parentId": "base-user",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "基础回答"}],
                },
            },
        ]
        transcript.write_text(
            "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
            encoding="utf-8",
        )
        self.store.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-recent-projection-invalidation",
            transcript_ref=transcript.as_posix(),
            branch_anchor="base-answer",
            binding_state="active",
            metadata={"protocolVersion": "2"},
            message_count=2,
        )
        self.runtime.recent_session_snapshot(session_id)

        appended = [
            {
                "type": "compaction",
                "id": "append-compaction",
                "parentId": "base-answer",
                "summary": "older context compacted",
            },
            {
                "type": "message",
                "id": "append-user",
                "parentId": "append-compaction",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "追加问题"}],
                },
            },
            {
                "type": "message",
                "id": "append-answer",
                "parentId": "append-user",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "追加回答"}],
                },
            },
        ]
        with transcript.open("a", encoding="utf-8") as target:
            target.write(
                "".join(
                    json.dumps(entry, ensure_ascii=False) + "\n"
                    for entry in appended
                )
            )

        projection_refreshed = threading.Event()
        remove_observer = self.events.add_observer(
            lambda event: projection_refreshed.set()
            if event.session_id == session_id
            and event.event_type == "snapshot_required"
            and event.payload.get("reason") == "recent_projection_refreshed"
            else None
        )
        stale_append = self.runtime.recent_session_snapshot(session_id)
        self.assertFalse(stale_append["projectionCurrent"])
        stale_texts = [
            block["data"]["text"]
            for message in stale_append["messages"]
            for block in message["blocks"]
            if block["type"] == "text"
        ]
        self.assertEqual(stale_texts, ["基础问题", "基础回答"])
        self.assertTrue(projection_refreshed.wait(timeout=5))
        remove_observer()
        compacted_append = self.runtime.recent_session_snapshot(session_id)
        compacted_append_repeat = self.runtime.recent_session_snapshot(session_id)
        self.assertTrue(compacted_append_repeat["projectionCurrent"])
        append_texts = [
            block["data"]["text"]
            for message in compacted_append["messages"]
            for block in message["blocks"]
            if block["type"] == "text"
        ]
        self.assertEqual(compacted_append, compacted_append_repeat)
        self.assertEqual(
            append_texts,
            ["基础问题", "基础回答", "追加问题", "追加回答"],
        )

        rewritten_entries = [
            {
                "type": "message",
                "id": "rewrite-user",
                "parentId": "base-user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "改写问题"}],
                },
            },
            {
                "type": "message",
                "id": "rewrite-answer",
                "parentId": "rewrite-user",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "改写回答"}],
                },
            },
        ]
        with transcript.open("a", encoding="utf-8") as target:
            target.write(
                "".join(
                    json.dumps(entry, ensure_ascii=False) + "\n"
                    for entry in rewritten_entries
                )
            )
        self.store.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-recent-projection-invalidation",
            transcript_ref=transcript.as_posix(),
            branch_anchor="rewrite-answer",
            binding_state="active",
            metadata={"protocolVersion": "2"},
            message_count=4,
        )

        rewritten = self.runtime.recent_session_snapshot(session_id)
        repeated = self.runtime.recent_session_snapshot(session_id)
        texts = [
            block["data"]["text"]
            for message in rewritten["messages"]
            for block in message["blocks"]
            if block["type"] == "text"
        ]

        self.assertEqual(rewritten, repeated)
        self.assertEqual(texts, ["改写问题", "改写回答"])
        self.assertNotIn("追加回答", texts)

    def test_recent_snapshot_preserves_legacy_flat_transcript_append_order(
        self,
    ) -> None:
        session_id = str(self.first["id"])
        transcript = self.root / "sessions" / "recent-legacy-flat.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        entries: list[dict[str, object]] = [
            {"type": "session", "id": "pi-recent-legacy-flat"},
        ]
        for index in range(8):
            entries.extend([
                {
                    "type": "message",
                    "id": f"flat-user-{index + 1}",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": f"旧问题 {index + 1}"}],
                    },
                },
                {
                    "type": "message",
                    "id": f"flat-answer-{index + 1}",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": f"旧回答 {index + 1}"}],
                    },
                },
            ])
        transcript.write_text(
            "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
            encoding="utf-8",
        )
        self.store.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-recent-legacy-flat",
            transcript_ref=transcript.as_posix(),
            branch_anchor="flat-answer-8",
            binding_state="active",
            metadata={"protocolVersion": "1"},
            message_count=16,
        )

        snapshot = self.runtime.recent_session_snapshot(session_id)

        texts = [
            block["data"]["text"]
            for message in snapshot["messages"]
            for block in message["blocks"]
            if block["type"] == "text"
        ]
        self.assertEqual(
            texts,
            [
                "旧问题 1", "旧回答 1", "旧问题 2", "旧回答 2",
                "旧问题 3", "旧回答 3", "旧问题 4", "旧回答 4",
                "旧问题 5", "旧回答 5", "旧问题 6", "旧回答 6",
                "旧问题 7", "旧回答 7", "旧问题 8", "旧回答 8",
            ],
        )

    def test_recent_snapshot_response_is_bounded_and_oversize_transcripts_fail_closed(
        self,
    ) -> None:
        session_id = str(self.first["id"])
        transcript = self.root / "sessions" / "recent-bounded.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        entries: list[dict[str, object]] = [
            {"type": "session", "id": "pi-recent-bounded"},
        ]
        parent_id = "pi-recent-bounded"
        for index in range(8):
            user_id = f"bounded-user-{index + 1}"
            answer_id = f"bounded-answer-{index + 1}"
            entries.extend([
                {
                    "type": "message",
                    "id": user_id,
                    "parentId": parent_id,
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": f"问题 {index + 1}"}],
                    },
                },
                {
                    "type": "message",
                    "id": answer_id,
                    "parentId": user_id,
                    "message": {
                        "role": "assistant",
                        "content": [{
                            "type": "text",
                            "text": f"回答 {index + 1}:" + (
                                "甲" * (30_000 if index == 7 else 12_000)
                            ),
                        }],
                    },
                },
            ])
            parent_id = answer_id
        transcript.write_text(
            "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
            encoding="utf-8",
        )
        self.store.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-recent-bounded",
            transcript_ref=transcript.as_posix(),
            branch_anchor=parent_id,
            binding_state="active",
            metadata={"protocolVersion": "2"},
            message_count=16,
        )

        snapshot = self.runtime.recent_session_snapshot(session_id)
        serialized = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")

        self.assertLessEqual(len(serialized), 256 * 1024)
        self.assertIn("回答 8:", serialized.decode("utf-8"))
        self.assertNotIn("近期快照已截断", serialized.decode("utf-8"))
        self.assertNotIn("回答 1:", serialized.decode("utf-8"))

        transcript.write_text(
            json.dumps({"type": "session", "id": "pi-recent-bounded"})
            + "\n"
            + ("x" * (8 * 1024 * 1024 + 1))
            + "\n",
            encoding="utf-8",
        )
        self.assertEqual(
            self.runtime.recent_session_snapshot(session_id),
            {"messages": [], "projectionCurrent": False},
        )

    def test_host_snapshot_failure_recovers_from_durable_history_instead_of_empty_success(self) -> None:
        session_id = str(self.first["id"])
        transcript = self.root / "sessions" / "host-snapshot-failure.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        entries = [
            {"type": "session", "id": "pi-host-snapshot-failure"},
            {
                "type": "message",
                "id": "failure-user",
                "parentId": "",
                "timestamp": "1970-01-01T00:00:00.100Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "历史仍然存在"}],
                },
            },
            {
                "type": "message",
                "id": "failure-assistant",
                "parentId": "failure-user",
                "timestamp": "1970-01-01T00:00:00.200Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Host 暂时不可用，但历史不能消失"}],
                },
            },
        ]
        transcript.write_text(
            "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
            encoding="utf-8",
        )
        self.store.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-host-snapshot-failure",
            transcript_ref=transcript.as_posix(),
            branch_anchor="failure-assistant",
            binding_state="active",
            metadata={"protocolVersion": "2"},
            message_count=2,
        )

        with patch.object(
            self.runtime,
            "_inspection_snapshot",
            side_effect=AgentRuntimeError("Host snapshot unavailable"),
        ):
            snapshot = self.runtime.session_snapshot(session_id)

        self.assertEqual(
            [
                block["data"]["text"]
                for message in snapshot["messages"]
                for block in message["blocks"]
                if block["type"] == "text"
            ],
            ["历史仍然存在", "Host 暂时不可用，但历史不能消失"],
        )

    def test_evaluation_snapshot_reads_managed_transcript_without_opening_runtime_host(self) -> None:
        session = self.store.create(
            title="EnterpriseOps CSM · Task 1 · 通过",
            model_profile="openai-codex/gpt-5.6-sol",
            thinking_level="high",
            tool_profile_version="subagent-readonly-v1",
            execution_mode="read_only",
            evaluation_snapshot=True,
        )
        session_id = str(session["id"])
        transcript = self.root / "sessions" / "evaluation-snapshot.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(
            "".join(
                json.dumps(entry, ensure_ascii=False) + "\n"
                for entry in (
                    {"type": "session", "id": "pi-evaluation-snapshot"},
                    {
                        "type": "message",
                        "id": "snapshot-user",
                        "parentId": "",
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": "真实评测任务"}],
                        },
                    },
                    {
                        "type": "message",
                        "id": "snapshot-answer",
                        "parentId": "snapshot-user",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "真实评测结果"}],
                        },
                    },
                )
            ),
            encoding="utf-8",
        )
        self.store.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-evaluation-snapshot",
            transcript_ref=transcript.as_posix(),
            branch_anchor="snapshot-answer",
            binding_state="prepared",
            metadata={"protocolVersion": "2"},
            message_count=2,
        )

        with patch.object(
            self.runtime,
            "_inspection_snapshot",
            side_effect=AssertionError("evaluation snapshot must not open Runtime Host"),
        ):
            snapshot = self.runtime.session_snapshot(session_id)

        self.assertEqual(
            [
                block["data"]["text"]
                for message in snapshot["messages"]
                for block in message["blocks"]
                if block["type"] == "text"
            ],
            ["真实评测任务", "真实评测结果"],
        )

    def test_durable_snapshot_keeps_product_prompt_identity(self) -> None:
        session_id = str(self.first["id"])
        transcript = self.root / "sessions" / "product-prompt-identity.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        entries = [
            {"type": "session", "id": "pi-product-prompt-identity"},
            {
                "type": "custom",
                "customType": "rag-ime.pi-turn-binding",
                "id": "binding-first-prompt",
                "parentId": "pi-product-prompt-identity",
                "data": {
                    "schemaVersion": "rag-ime.pi-turn-binding.v1",
                    "turnId": "turn-first-prompt",
                    "clientMessageId": "client-first-prompt",
                },
            },
            {
                "type": "message",
                "id": "product-user",
                "parentId": "binding-first-prompt",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "hi"}],
                },
            },
            {
                "type": "message",
                "id": "product-assistant",
                "parentId": "product-user",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hi! How can I help?"}],
                },
            },
        ]
        transcript.write_text(
            "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
            encoding="utf-8",
        )
        self.store.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-product-prompt-identity",
            transcript_ref=transcript.as_posix(),
            branch_anchor="product-assistant",
            binding_state="active",
            metadata={"protocolVersion": "2"},
            message_count=2,
        )

        snapshot = self.runtime.session_snapshot(session_id)
        recent = self.runtime.recent_session_snapshot(session_id)

        self.assertEqual(
            [message["turnId"] for message in snapshot["messages"]],
            ["turn-first-prompt", "turn-first-prompt"],
        )
        self.assertEqual(
            snapshot["messages"][0]["clientMessageId"],
            "client-first-prompt",
        )
        self.assertEqual(
            [message["turnId"] for message in recent["messages"]],
            ["turn-first-prompt", "turn-first-prompt"],
        )
        self.assertEqual(
            recent["messages"][0]["clientMessageId"],
            "client-first-prompt",
        )

    def test_durable_snapshot_uses_transcript_append_time_for_timeline_order(self) -> None:
        session_id = str(self.first["id"])
        transcript = self.root / "sessions" / "timeline-order.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        entries = [
            {"type": "session", "id": "pi-timeline-order"},
            {
                "type": "message",
                "id": "timeline-user-1",
                "parentId": "",
                "timestamp": "1970-01-01T00:00:00.100Z",
                "message": {
                    "role": "user",
                    "timestamp": 900,
                    "content": [{"type": "text", "text": "第一条"}],
                },
            },
            {
                "type": "message",
                "id": "timeline-user-2",
                "parentId": "timeline-user-1",
                "timestamp": "1970-01-01T00:00:00.200Z",
                "message": {
                    "role": "user",
                    "timestamp": 100,
                    "content": [{"type": "text", "text": "第二条"}],
                },
            },
        ]
        transcript.write_text(
            "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
            encoding="utf-8",
        )
        self.store.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-timeline-order",
            transcript_ref=transcript.as_posix(),
            branch_anchor="timeline-user-2",
            binding_state="active",
            metadata={"protocolVersion": "2"},
            message_count=2,
        )

        snapshot = self.runtime.session_snapshot(session_id)
        recent = self.runtime.recent_session_snapshot(session_id)

        self.assertEqual(
            [message["createdAtMs"] for message in snapshot["messages"]],
            [100, 200],
        )
        self.assertEqual(
            [message["createdAtMs"] for message in recent["messages"]],
            [200],
        )
        self.assertEqual(
            [message["timelineSequence"] for message in recent["messages"]],
            [2.0],
        )

    def test_recent_snapshot_keeps_bounded_reasoning_and_tool_progress(self) -> None:
        session_id = str(self.first["id"])
        transcript = self.root / "sessions" / "recent-tool-progress.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        entries = [
            {"type": "session", "id": "pi-recent-tool-progress"},
            {
                "type": "message", "id": "recent-tool-user",
                "parentId": "pi-recent-tool-progress", "timestamp": 100,
                "message": {"role": "user", "timestamp": 900, "content": [{"type": "text", "text": "检查文件"}]},
            },
            {
                "type": "message", "id": "recent-tool-call", "parentId": "recent-tool-user", "timestamp": 200,
                "message": {
                    "role": "assistant", "timestamp": 901,
                    "content": [{"type": "toolCall", "id": "tool-recent", "name": "workspace_read", "arguments": {"path": "README.md"}}],
                },
            },
            {
                "type": "message", "id": "recent-tool-result", "parentId": "recent-tool-call", "timestamp": 300,
                "message": {
                    "role": "toolResult", "timestamp": 902, "toolCallId": "tool-recent", "toolName": "workspace_read",
                    "isError": False, "details": {"summary": "读取完成"},
                },
            },
            {
                "type": "message", "id": "recent-tool-answer", "parentId": "recent-tool-result", "timestamp": 400,
                "message": {
                    "role": "assistant", "api": "openai-codex-responses", "timestamp": 903,
                    "content": [{"type": "thinking", "thinking": "核对读取结果"}, {"type": "text", "text": "检查完成"}],
                },
            },
        ]
        transcript.write_text(
            "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
            encoding="utf-8",
        )
        self.store.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-recent-tool-progress",
            transcript_ref=transcript.as_posix(),
            branch_anchor="recent-tool-answer",
            binding_state="active",
            metadata={"protocolVersion": "2"},
            message_count=4,
        )

        first = self.runtime.recent_session_snapshot(session_id)
        with patch(
            "rag_ime.pi.runtime.read_recent_transcript_tail",
            side_effect=AssertionError("exact recent cache must include progress"),
        ):
            second = self.runtime.recent_session_snapshot(session_id)

        self.assertEqual(first, second)
        self.assertEqual(
            [event["eventType"] for event in first["toolHistoryEvents"]],
            ["tool_started", "tool_finished", "reasoning_summary"],
        )
        final_message = next(message for message in first["messages"] if message["role"] == "assistant")
        self.assertEqual(final_message["timelineSequence"], 4.9)

    def test_durable_reasoning_precedes_final_inside_one_append_entry(self) -> None:
        session_id = str(self.first["id"])
        transcript = self.root / "sessions" / "reasoning-before-final.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        entries = [
            {"type": "session", "id": "pi-reasoning-order"},
            {
                "type": "message",
                "id": "reasoning-user",
                "parentId": "pi-reasoning-order",
                "timestamp": "1970-01-01T00:00:00.100Z",
                "message": {
                    "role": "user",
                    "timestamp": 100,
                    "content": [{"type": "text", "text": "hi"}],
                },
            },
            {
                "type": "message",
                "id": "reasoning-assistant",
                "parentId": "reasoning-user",
                "timestamp": "1970-01-01T00:00:00.200Z",
                "message": {
                    "role": "assistant",
                    "api": "openai-codex-responses",
                    "timestamp": 200,
                    "content": [
                        {"type": "thinking", "thinking": "Drafting greeting"},
                        {"type": "text", "text": "Hi!"},
                    ],
                },
            },
        ]
        transcript.write_text(
            "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
            encoding="utf-8",
        )
        self.store.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-reasoning-order",
            transcript_ref=transcript.as_posix(),
            branch_anchor="reasoning-assistant",
            binding_state="active",
            metadata={"protocolVersion": "2"},
            message_count=2,
        )

        snapshot = self.runtime.session_snapshot(session_id)
        final_message = next(
            message for message in snapshot["messages"]
            if message["role"] == "assistant"
        )
        reasoning = next(
            event for event in snapshot["toolHistoryEvents"]
            if event["eventType"] == "reasoning_summary"
        )

        self.assertEqual(final_message["timelineSequence"], 2.9)
        self.assertEqual(reasoning["timelineSequence"], 2.1)
        self.assertLess(
            reasoning["timelineSequence"],
            final_message["timelineSequence"],
        )

    def test_durable_snapshot_keeps_more_than_256_tool_activities_after_refresh(
        self,
    ) -> None:
        session_id = str(self.first["id"])
        transcript = self.root / "sessions" / "long-tool-history.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        entries: list[dict[str, object]] = [
            {"type": "session", "id": "pi-long-tool-history"},
            {
                "type": "message",
                "id": "long-tool-user",
                "parentId": "pi-long-tool-history",
                "timestamp": "1970-01-01T00:00:00.100Z",
                "message": {
                    "role": "user",
                    "timestamp": 100,
                    "content": [{"type": "text", "text": "保留完整工具时间线"}],
                },
            },
        ]
        parent_id = "long-tool-user"
        for index in range(300):
            tool_call_id = f"long-tool-{index + 1}"
            assistant_id = f"long-assistant-{index + 1}"
            result_id = f"long-result-{index + 1}"
            entries.extend([
                {
                    "type": "message",
                    "id": assistant_id,
                    "parentId": parent_id,
                    "timestamp": 1_000 + index * 2,
                    "message": {
                        "role": "assistant",
                        "timestamp": 200 + index * 2,
                        "content": [{
                            "type": "toolCall",
                            "id": tool_call_id,
                            "name": "workspace_read",
                            "arguments": {"path": f"docs/item-{index + 1}.md"},
                        }],
                    },
                },
                {
                    "type": "message",
                    "id": result_id,
                    "parentId": assistant_id,
                    "timestamp": 1_001 + index * 2,
                    "message": {
                        "role": "toolResult",
                        "timestamp": 201 + index * 2,
                        "toolCallId": tool_call_id,
                        "toolName": "workspace_read",
                        "isError": False,
                        "details": {"summary": f"read item {index + 1}"},
                    },
                },
            ])
            parent_id = result_id
        transcript.write_text(
            "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
            encoding="utf-8",
        )
        self.store.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-long-tool-history",
            transcript_ref=transcript.as_posix(),
            branch_anchor=parent_id,
            binding_state="active",
            metadata={"protocolVersion": "2"},
            message_count=601,
        )

        snapshot = self.runtime.session_snapshot(session_id)
        tool_events = [
            event
            for event in snapshot["toolHistoryEvents"]
            if event["eventType"] in {"tool_started", "tool_finished"}
        ]

        self.assertEqual(len(tool_events), 600)
        self.assertEqual(tool_events[0]["payload"]["toolCallId"], "long-tool-1")
        self.assertEqual(tool_events[-1]["payload"]["toolCallId"], "long-tool-300")

    def test_resident_history_and_command_reads_skip_context_reassembly(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        context_calls: list[str] = []
        self.runtime._session_context_provider = (  # noqa: SLF001 - latency contract
            lambda session: context_calls.append(str(session["id"])) or {}
        )

        with patch.object(
            self.store,
            "set_status",
            wraps=self.store.set_status,
        ) as set_status:
            snapshot = self.runtime.session_snapshot(session_id)
            commands = self.runtime.command_catalog(session_id)

        self.assertTrue(snapshot["messages"] == [])
        self.assertTrue(commands)
        self.assertEqual(context_calls, [])
        set_status.assert_not_called()

    def test_resident_idle_history_prefers_populated_durable_branch_without_live_snapshot(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        binding = self.store.runtime_binding(session_id)
        transcript = Path(binding["transcriptRef"])
        root_id = str(binding["externalSessionId"])
        time.sleep(0.01)
        entries = [
            {"type": "session", "id": root_id},
            {
                "type": "message",
                "id": "resident-user",
                "parentId": root_id,
                "timestamp": "1970-01-01T00:00:00.100Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "常驻历史问题"}],
                },
            },
            {
                "type": "message",
                "id": "resident-answer",
                "parentId": "resident-user",
                "timestamp": "1970-01-01T00:00:00.200Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "常驻历史回答"}],
                },
            },
        ]
        transcript.write_text(
            "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
            encoding="utf-8",
        )
        request_log = self.root / "agent" / "host-requests.jsonl"
        before = [
            json.loads(line)
            for line in request_log.read_text(encoding="utf-8").splitlines()
        ]

        snapshot = self.runtime.session_snapshot(session_id)

        self.assertEqual(
            [
                block["data"]["text"]
                for message in snapshot["messages"]
                for block in message["blocks"]
                if block["type"] == "text"
            ],
            ["常驻历史问题", "常驻历史回答"],
        )
        after = [
            json.loads(line)
            for line in request_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [item["method"] for item in after].count("session.snapshot"),
            [item["method"] for item in before].count("session.snapshot"),
        )

    def test_resident_idle_history_keeps_body_across_repeated_reads(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.prompt(session_id, "resident history")
        _wait_until(lambda: self.store.get(session_id)["status"] == "idle")

        transcript = Path(
            self.store.runtime_binding(session_id)["transcriptRef"]
        )
        self.assertEqual(
            [json.loads(line)["type"] for line in transcript.read_text().splitlines()],
            ["session"],
        )

        for _ in range(2):
            snapshot = self.runtime.session_snapshot(session_id)
            self.assertEqual(
                [
                    block["data"]["text"]
                    for message in snapshot["messages"]
                    for block in message["blocks"]
                    if block["type"] == "text"
                ],
                ["resident history", f"host reply for {session_id}"],
            )

        requests = [
            json.loads(line)
            for line in (
                self.root / "agent" / "host-requests.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [item["method"] for item in requests].count("session.open"),
            1,
        )
        self.assertEqual(
            [item["method"] for item in requests].count("session.snapshot"),
            2,
        )

    def test_snapshot_keeps_transient_steer_in_the_active_history_turn(self) -> None:
        session_id = str(self.first["id"])
        transient = (
            "RAG_IME_TRANSIENT_CONTEXT_V1\n"
            + json.dumps(
                {
                    "schemaVersion": "rag-ime.runtime-prompt.v1",
                    "message": "查看详细",
                    "sessionContext": "private-session-context",
                    "transientContext": "private-steer-context",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        messages = [
            {
                "id": "user-continue",
                "role": "user",
                "timestamp": 100,
                "content": [{"type": "text", "text": "继续"}],
            },
            {
                "id": "assistant-browser",
                "role": "assistant",
                "timestamp": 101,
                "content": [{
                    "type": "toolCall",
                    "id": "browser-call",
                    "name": "browser",
                    "arguments": {"action": "snapshot"},
                }],
            },
            {
                "id": "browser-result",
                "role": "toolResult",
                "timestamp": 102,
                "toolCallId": "browser-call",
                "toolName": "browser",
                "content": [{"type": "text", "text": "page ready"}],
            },
            {
                "id": "user-steer",
                "role": "user",
                "timestamp": 103,
                "content": [{"type": "text", "text": transient}],
            },
            {
                "id": "assistant-final",
                "role": "assistant",
                "timestamp": 104,
                "content": [{"type": "text", "text": "这是详细结果"}],
            },
            {
                "id": "user-next",
                "role": "user",
                "timestamp": 105,
                "content": [{"type": "text", "text": "开始下一轮"}],
            },
            {
                "id": "assistant-next",
                "role": "assistant",
                "timestamp": 106,
                "content": [{"type": "text", "text": "下一轮结果"}],
            },
        ]
        entries = [
            {
                "type": "message",
                "id": str(message["id"]),
                "parentId": str(messages[index - 1]["id"]) if index else "root",
                "message": message,
            }
            for index, message in enumerate(messages)
        ]

        with patch.object(
            self.runtime,
            "_inspection_snapshot",
            return_value={
                "messages": messages,
                "entries": entries,
                "leafId": "assistant-next",
                "messageQueue": {},
            },
        ):
            snapshot = self.runtime.session_snapshot(session_id)

        self.assertEqual(
            [message["turnId"] for message in snapshot["messages"]],
            [
                "history:user-continue",
                "history:user-continue",
                "history:user-continue",
                "history:user-next",
                "history:user-next",
            ],
        )
        self.assertEqual(
            [
                block["data"]["text"]
                for message in snapshot["messages"]
                for block in message["blocks"]
                if block["type"] == "text"
            ],
            ["继续", "查看详细", "这是详细结果", "开始下一轮", "下一轮结果"],
        )
        self.assertEqual(
            {
                event["turnId"]
                for event in snapshot["toolHistoryEvents"]
            },
            {"history:user-continue"},
        )
        serialized = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn("RAG_IME_TRANSIENT_CONTEXT_V1", serialized)
        self.assertNotIn("private-session-context", serialized)
        self.assertNotIn("private-steer-context", serialized)

    def test_mixed_text_and_tool_message_keeps_text_but_not_tool_protocol(self) -> None:
        mixed = {
            "role": "assistant",
            "timestamp": 101,
            "content": [
                {"type": "text", "text": "我先检查项目入口。"},
                {
                    "type": "toolCall",
                    "id": "call-read",
                    "name": "workspace_read",
                    "arguments": {
                        "path": "/Users/private/project/README.md",
                        "apiKey": "top-secret",
                    },
                },
            ],
        }
        tool_only = {
            **mixed,
            "content": [mixed["content"][1]],
        }

        self.assertTrue(pi_message_is_public(mixed))
        self.assertFalse(pi_message_completes_public_turn(mixed))
        self.assertFalse(pi_message_is_public(tool_only))
        payload = pi_message_payload(
            mixed,
            session_id="session-1",
            turn_id="turn-1",
        ).to_payload()
        self.assertEqual(
            [block["type"] for block in payload["blocks"]],
            ["text"],
        )
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertIn("我先检查项目入口", serialized)
        self.assertNotIn("workspace_read", serialized)
        self.assertNotIn("top-secret", serialized)
        self.assertNotIn("/Users/private", serialized)

    def test_thinking_text_wrappers_stay_out_of_history_and_live_text(self) -> None:
        from rag_ime.pi.public import visible_message_text, last_assistant_preview

        for text, expected in [
            ("<thinking>private notes</thinking>Visible answer", "Visible answer"),
            ("<thinking>unfinished internal notes", ""),
            ("<think", ""),
            ("<thinking>one</thinking>\n<thinking>two</thinking>Answer", "Answer"),
            ("```xml\n<thinking>example</thinking>\n```", "```xml\n<thinking>example</thinking>\n```"),
            ("Use `<thinking>` as a literal tag.", "Use `<thinking>` as a literal tag."),
        ]:
            with self.subTest(text=text):
                self.assertEqual(visible_message_text("assistant", text), expected)
                self.assertEqual(visible_message_text("user", text), text)
        hidden = {"role": "assistant", "content": [{"type": "text", "text": "<thinking>private notes"}]}
        self.assertFalse(pi_message_is_public(hidden))
        self.assertEqual(last_assistant_preview([hidden]), "")
        self.assertNotIn("private notes", json.dumps(pi_message_payload(hidden, session_id="s", turn_id="t").to_payload()))
        session_id = str(self.first["id"])
        cumulative = ""
        for chunk in ["<thi", "nking>private notes", "</thinking>", "Visible answer"]:
            cumulative += chunk
            self.runtime._handle_host_event({
                "protocolVersion": "2", "event": "agent.event", "sessionId": session_id, "turnId": "wrapped-stream",
                "payload": {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": chunk},
                    "message": {"id": "wrapped-text", "role": "assistant", "content": [{"type": "text", "text": cumulative}]}},
            })
        deltas = [event.payload for event in self.events.replay(session_id)[0] if event.event_type == "text_delta" and event.turn_id == "wrapped-stream"]
        self.assertEqual(deltas[-1]["delta"], "Visible answer")
        self.assertTrue(deltas[-1]["replaceContent"])
        self.assertNotIn("private notes", json.dumps(deltas))
        self.assertNotIn("<thi", json.dumps(deltas))

    def test_public_summary_removes_thinking_wrappers_without_widening_provider_access(self) -> None:
        raw = {"api": "openai-responses", "content": [
            {"type": "thinking", "thinking": "**<thinking>Preparing app</thinking>**\n\n<THINKING>Checking revision</THINKING>"},
            {"type": "redacted_thinking", "thinking": "hidden"},
        ]}
        self.assertEqual(public_reasoning_summaries(raw), ["Preparing app"])
        raw["content"] = [{"type": "thinking", "thinking": "<thinking>\nChecking revision\n</thinking>"}]
        self.assertEqual(public_reasoning_summaries(raw), ["Checking revision"])
        self.assertEqual(public_reasoning_summaries({**raw, "api": "anthropic-messages"}), [])

    def test_only_openai_response_family_summaries_are_public(self) -> None:
        openai = {
            "api": "openai-responses",
            "content": [{
                "type": "thinking",
                "thinking": (
                    "**Analyzing session history**\n\n"
                    "**Inspecting /Users/private/project/runtime.py**"
                ),
                "thinkingSignature": "opaque-provider-signature",
            }],
        }
        codex = {**openai, "api": "openai-codex-responses"}
        anthropic = {**openai, "api": "anthropic-messages"}

        expected = ["Analyzing session history", "Inspecting [REDACTED_PATH]"]
        self.assertEqual(public_reasoning_summaries(openai), expected)
        self.assertEqual(public_reasoning_summaries(codex), expected)
        self.assertEqual(public_reasoning_summaries(anthropic), [])

    def test_host_tool_artifact_is_carried_to_the_final_assistant_message(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        turn_id = "turn-host-file-artifact"
        file_block = {
            "id": "tool-artifact:file:0123456789abcdef",
            "type": "file",
            "data": {
                "mediaId": "media_abcdefghijklmnop",
                "sessionId": session_id,
                "fileName": "result.diff",
                "mimeType": "text/x-diff",
                "byteSize": 80,
                "sha256": "b" * 64,
                "receiptUrl": (
                    "/api/agent/media/media_abcdefghijklmnop/content"
                    f"?sessionId={session_id}"
                ),
            },
        }

        def send(payload: dict[str, object]) -> None:
            self.runtime._handle_host_event(  # noqa: SLF001 - protocol boundary
                {
                    "protocolVersion": "2",
                    "event": "agent.event",
                    "sessionId": session_id,
                    "turnId": turn_id,
                    "payload": payload,
                }
            )

        send({
            "type": "message_start",
            "message": {"role": "assistant", "timestamp": 101, "content": []},
        })
        send({
            "type": "tool_execution_end",
            "toolCallId": "call-patch",
            "toolName": "workspace_patch",
            "result": {"details": {"agentBlocks": [file_block]}},
            "isError": False,
        })
        send({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "timestamp": 101,
                "content": [
                    {"type": "text", "text": "我已经找到主要结构，继续核对最后一项。"},
                    {"type": "toolCall", "id": "call-next", "name": "todo"},
                ],
            },
        })
        send({
            "type": "message_start",
            "message": {"role": "assistant", "timestamp": 102, "content": []},
        })
        send({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "timestamp": 102,
                "content": [{"type": "text", "text": "变更已经交付。"}],
                "usage": {"input": 100, "output": 20, "totalTokens": 120},
            },
        })

        events, gap = self.events.replay(session_id)
        self.assertFalse(gap)
        completed = [event.payload["message"] for event in events if event.event_type == "message_completed"]
        progress = [event.payload for event in events if event.event_type == "text_delta"]
        self.assertEqual(
            [
                (item["delta"], item.get("replaceContent"), item.get("sourceLoopId"))
                for item in progress
            ],
            [("我已经找到主要结构，继续核对最后一项。", True, "pi:message:assistant:101")],
        )
        self.assertEqual(len(completed), 1)
        completed_event = next(
            event
            for event in events
            if event.event_type == "message_completed"
        )
        self.assertIs(completed_event.payload["usageReported"], True)
        self.assertIs(completed_event.payload["cacheUsageReported"], False)
        self.assertEqual(
            completed_event.payload.get("sourceLoopId"),
            "pi:message:assistant:102",
        )
        self.assertEqual([block["type"] for block in completed[-1]["blocks"]], ["file", "text"])
        self.assertEqual(completed[-1]["blocks"][0]["data"]["mimeType"], "text/x-diff")

    def test_host_projects_one_source_loop_id_per_assistant_tool_loop(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        turn_id = "turn-two-assistant-loops"

        def send(payload: dict[str, object]) -> None:
            self.runtime._handle_host_event(  # noqa: SLF001 - protocol boundary
                {
                    "protocolVersion": "2",
                    "event": "agent.event",
                    "sessionId": session_id,
                    "turnId": turn_id,
                    "payload": payload,
                }
            )

        send({
            "type": "message_start",
            "message": {"role": "assistant", "timestamp": 101, "content": []},
        })
        send({
            "type": "tool_execution_start",
            "toolCallId": "call-open",
            "toolName": "room_partner",
            "args": {"op": "delegate_batch"},
        })
        send({
            "type": "message_start",
            "message": {"role": "assistant", "timestamp": 202, "content": []},
        })
        send({
            "type": "tool_execution_start",
            "toolCallId": "call-final",
            "toolName": "room_partner",
            "args": {"op": "post", "kind": "result"},
        })

        events, gap = self.events.replay(session_id)
        self.assertFalse(gap)
        tool_events = [
            event for event in events
            if event.event_type == "tool_started"
        ]
        self.assertEqual(
            [event.payload.get("sourceLoopId") for event in tool_events],
            [
                "pi:message:assistant:101",
                "pi:message:assistant:202",
            ],
        )

    def test_manual_and_automatic_compaction_notify_memory_checkpoint_observer(self) -> None:
        session_id = str(self.first["id"])

        manual = self.runtime.compact(session_id, "保留长期决定")
        self.runtime._handle_host_event(  # noqa: SLF001 - protocol integration boundary
            {
                "protocolVersion": "2",
                "event": "agent.event",
                "sessionId": session_id,
                "turnId": "turn:auto-compact",
                "payload": {
                    "type": "compaction_end",
                    "trigger": "automatic",
                    "result": {
                        "summary": "自动压缩保留了桌面语义操控约束。",
                        "coverageStartEntryId": "entry-user-2",
                        "coverageEndEntryId": "entry-assistant-2",
                    },
                },
            }
        )

        self.assertTrue(manual["memoryCheckpoint"]["stored"])
        self.assertEqual(
            [(item[0], item[2]) for item in self.compactions],
            [(session_id, "manual"), (session_id, "automatic")],
        )
        self.assertEqual(
            self.compactions[1][1]["coverageEndEntryId"],
            "entry-assistant-2",
        )

    def test_one_host_keeps_multiple_sessions_open_and_syncs_dynamic_tools(self) -> None:
        first_id = str(self.first["id"])
        second_id = str(self.second["id"])
        self.store.set_thinking_level(first_id, "max")
        self.runtime.ensure(first_id)
        self.runtime.ensure(second_id)

        status = self.runtime.runtime_status()
        self.assertEqual(status["openSessionIds"], sorted([first_id, second_id]))
        self.assertTrue(status["capabilities"]["multiSession"])
        self.assertEqual(
            status["capabilities"]["runtimePrimitives"]["continuationEnvelope"],
            "2",
        )
        self.assertTrue(
            status["capabilities"]["runtimePrimitives"][
                "sessionCancelOperationRegistry"
            ]
        )
        self.assertTrue(
            status["capabilities"]["runtimePrimitives"]["sessionContinuationQueue"]
        )
        self.assertTrue(
            status["capabilities"]["runtimePrimitives"][
                "sessionCancelOperations"
            ]["manualCompaction"]
        )
        self.assertTrue(
            status["capabilities"]["runtimePrimitives"][
                "sessionCancelOperations"
            ]["bashProcess"]
        )
        self.assertTrue(
            status["capabilities"]["runtimePrimitives"][
                "sessionCancelOperations"
            ]["branchSummary"]
        )
        self.assertTrue(
            status["capabilities"]["runtimePrimitives"][
                "sessionCancelOperations"
            ]["continuationTimer"]
        )
        self.assertNotIn("roomTypes", status["capabilities"]["runtimePrimitives"])
        requests = [json.loads(line) for line in (self.root / "agent" / "host-requests.jsonl").read_text().splitlines()]
        opened = [row for row in requests if row["method"] == "session.open"]
        self.assertEqual(len(opened), 2)
        self.assertEqual(opened[0]["params"]["toolManifest"][0]["name"], "memory")
        self.assertEqual(opened[0]["params"]["thinkingLevel"], "max")

    def test_session_open_forwards_effective_codemode_mode_and_runtime_advertises_support(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.config = replace(self.runtime.config, codemode_mode="only")
        self.runtime.ensure(session_id)

        requests = [
            json.loads(line)
            for line in (self.root / "agent" / "host-requests.jsonl").read_text().splitlines()
        ]
        opened = next(request for request in requests if request["method"] == "session.open")
        self.assertEqual(opened["params"]["codemodeMode"], "only")
        self.assertEqual(
            self.runtime.runtime_status()["capabilities"]["codemode"]["modes"],
            ["off", "on", "only"],
        )

    def test_idle_codemode_setter_persists_effective_mode_without_rotating_binding(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        before = self.store.runtime_binding(session_id)
        assert before is not None

        selected = self.runtime.set_codemode_mode(session_id, mode="only")

        self.assertEqual(selected["codemodeMode"], "only")
        after = self.store.runtime_binding(session_id)
        assert after is not None
        self.assertEqual(after["generation"], before["generation"])
        self.assertTrue(after["metadata"]["codemodeAvailable"])
        self.assertEqual(after["metadata"]["codemodeMode"], "only")
        self.assertEqual(
            self.runtime.session_snapshot(session_id)["codemodeMode"],
            "only",
        )
        self.assertEqual(self.store.get(session_id)["codemodeMode"], "only")
        self.assertTrue(self.runtime.close_session(session_id))
        reopened = self.runtime.ensure(session_id)
        self.assertEqual(reopened["codemodeMode"], "only")
        self.assertEqual(self.store.get(session_id)["codemodeMode"], "only")
        requests = [
            json.loads(line)
            for line in (self.root / "agent" / "host-requests.jsonl").read_text().splitlines()
        ]
        self.assertEqual(
            [request["method"] for request in requests].count("session.codemode.set"),
            1,
        )

    def test_unsupported_host_never_projects_configured_or_snapshot_codemode(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        binding = self.store.runtime_binding(session_id)
        assert binding is not None
        self.runtime._host_capabilities["codemode"] = {
            "available": False,
            "modes": [],
            "defaultMode": "",
        }
        self.store.update_runtime_binding_metadata(
            session_id,
            {"codemodeAvailable": False, "codemodeMode": "on"},
            expected_generation=int(binding["generation"]),
            expected_external_session_id=str(binding["externalSessionId"]),
            expected_transcript_ref=str(binding["transcriptRef"]),
            expected_branch_anchor=str(binding["branchAnchor"]),
        )

        self.assertNotIn("codemodeMode", self.store.get(session_id))
        self.assertNotIn("codemodeMode", self.store.list()[0])
        self.assertNotIn(
            "codemodeMode",
            self.store.list_page(projection_only=True)["items"][0],
        )
        with patch.object(
            self.runtime,
            "_inspection_snapshot",
            return_value={"messages": [], "codemodeMode": "on"},
        ):
            self.assertNotIn(
                "codemodeMode",
                self.runtime.session_snapshot(session_id),
            )

    def test_warm_ensure_applies_changed_codemode_preference_only_when_idle(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        self.runtime._session_context_provider = lambda _session: {
            "codemodeMode": "only",
        }

        prepared = self.runtime.ensure(session_id)

        self.assertEqual(prepared["codemodeMode"], "only")
        binding = self.store.runtime_binding(session_id)
        assert binding is not None
        self.assertEqual(binding["metadata"]["codemodeMode"], "only")
        requests = [
            json.loads(line)
            for line in (self.root / "agent" / "host-requests.jsonl").read_text().splitlines()
        ]
        self.assertEqual(
            [request["method"] for request in requests].count("session.codemode.set"),
            1,
        )

    def test_runtime_primitives_accept_only_supported_continuation_envelopes(self) -> None:
        self.assertEqual(
            runtime_primitive_capabilities({"continuationEnvelope": "1"})[
                "continuationEnvelope"
            ],
            "1",
        )
        self.assertEqual(
            runtime_primitive_capabilities({"continuationEnvelope": "2"})[
                "continuationEnvelope"
            ],
            "2",
        )
        for unsupported in ("3", 2, True, None):
            with self.subTest(unsupported=unsupported):
                self.assertEqual(
                    runtime_primitive_capabilities(
                        {"continuationEnvelope": unsupported}
                    )["continuationEnvelope"],
                    "",
                )

    def test_live_peer_runtime_manager_cannot_kill_the_active_host(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        client = self.runtime._client
        self.assertIsNotNone(client)
        assert client is not None
        active_host_identity = client.host_identity

        peer = PiRuntimeHostManager(
            config=self.runtime.config,
            sessions=self.store,
            events=self.events,
            tool_manifest_provider=lambda _session: [],
        )
        try:
            self.assertEqual(
                peer.runtime_status()["runtimeHostKillGate"][
                    "orphanReconcileReceipts"
                ],
                [],
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "unreconciled Runtime Host",
            ):
                peer.available_models()

            self.assertTrue(client.running)
            self.assertEqual(
                self.runtime.ensure(session_id)["state"][
                    "sessionId"
                ],
                session_id,
            )
            self.assertEqual(
                self.runtime._kill_gate.process(
                    active_host_identity
                )["state"],
                "running",
            )
        finally:
            peer.stop()

    def test_concurrent_catalog_reads_admit_exactly_one_runtime_host(self) -> None:
        workers = 6
        start_barrier = threading.Barrier(workers)
        state_lock = threading.Lock()
        active_starts = 0
        maximum_active_starts = 0
        errors: list[BaseException] = []
        catalogs: list[list[dict[str, object]]] = []
        original_start = PiRuntimeHostClient.start

        def delayed_start(client: PiRuntimeHostClient) -> dict[str, object]:
            nonlocal active_starts, maximum_active_starts
            with state_lock:
                active_starts += 1
                maximum_active_starts = max(
                    maximum_active_starts,
                    active_starts,
                )
            try:
                # Widen the admission window enough that the old unlocked
                # implementation deterministically started competing Hosts.
                time.sleep(0.05)
                return original_start(client)
            finally:
                with state_lock:
                    active_starts -= 1

        def read_catalog() -> None:
            start_barrier.wait()
            try:
                catalog = self.runtime.available_models()
                with state_lock:
                    catalogs.append(catalog)
            except BaseException as exc:
                with state_lock:
                    errors.append(exc)

        with patch.object(
            PiRuntimeHostClient,
            "start",
            autospec=True,
            side_effect=delayed_start,
        ):
            threads = [
                threading.Thread(target=read_catalog)
                for _ in range(workers)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(catalogs), workers)
        self.assertEqual(maximum_active_starts, 1)
        self.assertTrue(all(catalog for catalog in catalogs))
        requests = [
            json.loads(line)
            for line in (
                self.root / "agent" / "host-requests.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            sum(request["method"] == "hello" for request in requests),
            1,
        )
        self.assertEqual(
            sum(request["method"] == "models.list" for request in requests),
            1,
        )

    def test_blocked_model_catalog_does_not_hold_session_lifecycle_lock(self) -> None:
        self.runtime._host()  # noqa: SLF001 - establish only the shared Host
        client = self.runtime._require_client()
        original_send = client.send
        catalog_entered = threading.Event()
        release_catalog = threading.Event()
        ensure_done = threading.Event()
        errors: list[BaseException] = []

        def blocking_models(
            method: str,
            params: dict[str, object] | None = None,
            *,
            timeout: float | None = None,
            before_write=None,
        ) -> dict[str, object]:
            if method == "models.list":
                catalog_entered.set()
                if not release_catalog.wait(timeout=5):
                    raise TimeoutError("test did not release models.list")
                return {
                    "models": [{
                        "provider": "gpt",
                        "id": "gpt-5.6-luna",
                        "name": "GPT-5.6 Luna",
                        "api": "responses",
                        "reasoning": True,
                        "thinkingLevels": ["off", "medium"],
                        "input": ["text", "image"],
                        "contextWindow": 1_000_000,
                        "maxTokens": 128_000,
                    }],
                }
            return original_send(
                method,
                params,
                timeout=timeout,
                before_write=before_write,
            )

        def read_catalog() -> None:
            try:
                self.runtime.available_models()
            except BaseException as exc:
                errors.append(exc)

        def ensure_session() -> None:
            try:
                self.runtime.ensure(str(self.second["id"]))
            except BaseException as exc:
                errors.append(exc)
            finally:
                ensure_done.set()

        with patch.object(client, "send", side_effect=blocking_models):
            catalog_thread = threading.Thread(target=read_catalog)
            catalog_thread.start()
            self.assertTrue(catalog_entered.wait(timeout=2))
            ensure_thread = threading.Thread(target=ensure_session)
            ensure_thread.start()
            lifecycle_completed_while_catalog_blocked = ensure_done.wait(timeout=0.5)
            release_catalog.set()
            catalog_thread.join(timeout=5)
            ensure_thread.join(timeout=5)

        self.assertTrue(lifecycle_completed_while_catalog_blocked)
        self.assertFalse(catalog_thread.is_alive())
        self.assertFalse(ensure_thread.is_alive())
        self.assertEqual(errors, [])

    def test_close_session_keeps_other_hosted_sessions_ready(self) -> None:
        first_id = str(self.first["id"])
        second_id = str(self.second["id"])
        self.runtime.ensure(first_id)
        self.runtime.ensure(second_id)

        self.assertTrue(self.runtime.close_session(first_id))

        status = self.runtime.runtime_status()
        self.assertEqual(status["openSessionIds"], [second_id])
        self.assertEqual(self.store.get(first_id)["status"], "idle")
        requests = [
            json.loads(line)
            for line in (self.root / "agent" / "host-requests.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        closed = [
            row["params"]["sessionId"]
            for row in requests
            if row["method"] == "session.close"
        ]
        self.assertEqual(closed, [first_id])
        self.assertTrue(self.runtime._client and self.runtime._client.running)  # noqa: SLF001

    def test_surface_profile_disables_project_context_files(self) -> None:
        surface = self.store.create(
            title="输入法联想 · test",
            session_kind="subagent_runtime",
            tool_profile_version="ime-surface-v1",
        )
        self.store.set_runtime_policy(
            str(surface["id"]),
            mode="assistant",
            tool_profile_version="ime-surface-v1",
            allowed_tools=[],
            workspace_roots=[],
        )

        self.runtime.ensure(str(surface["id"]))

        requests = [json.loads(line) for line in (self.root / "agent" / "host-requests.jsonl").read_text().splitlines()]
        opened = [row for row in requests if row["method"] == "session.open"]
        self.assertTrue(opened[-1]["params"]["noContextFiles"])
        self.assertIn("只输出可以直接插入光标", opened[-1]["params"]["systemPrompt"])

    def test_memory_curation_profile_opens_without_tools_context_or_skills(self) -> None:
        memory_session = self.store.create(
            title="memory curation",
            session_kind="subagent_runtime",
            tool_profile_version="memory-curation-v1",
            project_context_enabled=True,
            pi_skills_enabled=True,
            codex_skills_enabled=True,
        )
        self.store.set_runtime_policy(
            str(memory_session["id"]),
            mode="assistant",
            tool_profile_version="memory-curation-v1",
            allowed_tools=["memory"],
            project_context_enabled=True,
            pi_skills_enabled=True,
            codex_skills_enabled=True,
            workspace_roots=[str(self.root)],
        )

        self.runtime.ensure(str(memory_session["id"]))

        requests = [
            json.loads(line)
            for line in (self.root / "agent" / "host-requests.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        opened = [row for row in requests if row["method"] == "session.open"][-1]
        self.assertEqual(opened["params"]["toolManifest"], [])
        self.assertTrue(opened["params"]["noContextFiles"])
        self.assertFalse(opened["params"]["piSkillsEnabled"])
        self.assertFalse(opened["params"]["codexSkillsEnabled"])
        self.assertEqual(opened["params"]["skillAllowlist"], [])
        self.assertIn(
            "governed personal-memory curation engine",
            opened["params"]["systemPrompt"],
        )

    def test_agent_end_is_not_terminal_and_max_comes_from_host_catalog(self) -> None:
        session_id = str(self.first["id"])
        catalog = self.runtime.model_catalog(session_id)
        self.assertEqual(catalog["models"][0]["thinkingLevels"], ["off", "medium", "xhigh", "max"])
        requests = [
            json.loads(line)
            for line in (
                self.root / "agent" / "host-requests.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [request["method"] for request in requests],
            ["hello", "models.list"],
        )
        self.assertEqual(self.runtime.runtime_status()["openSessionIds"], [])
        self.runtime.set_thinking_level(session_id, level="max")

        accepted = self.runtime.prompt(session_id, "hello", client_message_id="client-1")
        self.assertEqual(accepted["clientMessageId"], "client-1")
        time.sleep(0.05)
        self.assertEqual(self.store.get(session_id)["status"], "busy")
        self.assertFalse(any(item.event_type == "turn_completed" for item in self.events.replay(session_id)[0]))

        _wait_until(
            lambda: any(
                item.event_type == "turn_completed"
                for item in self.events.replay(session_id)[0]
            )
        )
        completed = [item for item in self.events.replay(session_id)[0] if item.event_type == "turn_completed"]
        self.assertEqual(completed[-1].payload["terminalEvent"], "agent_settled")
        messages = self.runtime.messages(session_id)
        self.assertEqual([item["role"] for item in messages], ["user", "assistant"])

    def test_prompt_ack_loss_after_host_acceptance_does_not_publish_unscoped_failure(
        self,
    ) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        client = self.runtime._require_client()
        original_send = client.send

        def lose_prompt_ack(
            method: str,
            params: dict[str, object] | None = None,
            *,
            timeout: float | None = None,
            before_write=None,
        ) -> dict[str, object]:
            result = original_send(
                method,
                params,
                timeout=timeout,
                before_write=before_write,
            )
            if method == "session.prompt":
                raise PiRuntimeError(
                    "Pi Runtime Host command timed out: session.prompt"
                )
            return result

        with patch.object(client, "send", side_effect=lose_prompt_ack):
            with self.assertRaisesRegex(
                PiRuntimeCommandAcceptanceUnknown,
                "command timed out: session.prompt",
            ):
                self.runtime.prompt(
                    session_id,
                    "host accepts but caller loses the acknowledgement",
                    client_message_id="client:lost-prompt-ack",
                )

        _wait_until(
            lambda: any(
                item.event_type == "turn_completed"
                for item in self.events.replay(session_id)[0]
            ),
        )
        events = self.events.replay(session_id)[0]
        completed = [
            item for item in events
            if item.event_type == "turn_completed"
        ]
        self.assertEqual(len(completed), 1)
        turn_id = completed[0].turn_id
        self.assertTrue(turn_id)
        self.assertTrue(any(
            item.event_type == "message_completed"
            and item.turn_id == turn_id
            for item in events
        ))
        self.assertFalse(any(
            item.event_type == "turn_failed"
            for item in events
        ))
        self.assertEqual(self.store.get(session_id)["status"], "idle")

    def test_provider_retry_events_are_projected_without_raw_diagnostics(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        turn_id = "turn-provider-retry"
        with self.runtime._lock:
            self.runtime._states[session_id].turn_id = turn_id

        self.runtime._handle_host_event({
            "protocolVersion": "2",
            "event": "agent.event",
            "sessionId": session_id,
            "turnId": turn_id,
            "payload": {
                "type": "auto_retry_start",
                "attempt": 2,
                "maxAttempts": 3,
                "delayMs": 4_000,
                "errorMessage": "private upstream diagnostic",
            },
        })
        self.runtime._handle_host_event({
            "protocolVersion": "2",
            "event": "agent.event",
            "sessionId": session_id,
            "turnId": turn_id,
            "payload": {
                "type": "agent_end",
                "willRetry": True,
                "messages": [{
                    "role": "assistant",
                    "stopReason": "error",
                    "errorMessage": "private upstream diagnostic",
                    "content": [],
                }],
            },
        })

        events = self.events.replay(session_id)[0]
        statuses = [
            event for event in events
            if event.event_type == "status_changed"
            and event.payload.get("phase") == "provider_retry"
        ]
        self.assertEqual(statuses[-1].payload["status"], "retrying")
        self.assertEqual(statuses[-1].payload["attempt"], 2)
        self.assertEqual(statuses[-1].payload["maxAttempts"], 3)
        self.assertFalse(any(event.event_type == "turn_failed" for event in events))
        self.assertNotIn(
            "private upstream diagnostic",
            json.dumps(statuses[-1].payload, ensure_ascii=False),
        )

        self.runtime._handle_host_event({
            "protocolVersion": "2",
            "event": "agent.event",
            "sessionId": session_id,
            "turnId": turn_id,
            "payload": {
                "type": "auto_retry_end",
                "attempt": 2,
                "success": False,
                "finalError": "must not escape",
            },
        })
        statuses = [
            event for event in self.events.replay(session_id)[0]
            if event.event_type == "status_changed"
            and event.payload.get("phase") == "provider_retry"
        ]
        self.assertEqual(statuses[-1].payload["status"], "working")
        self.assertEqual(statuses[-1].payload["activityState"], "failed")
        self.assertNotIn(
            "must not escape",
            json.dumps(statuses[-1].payload, ensure_ascii=False),
        )

    def test_exhausted_provider_retries_are_reported_on_terminal_failure(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        turn_id = "turn-provider-retry-exhausted"
        with self.runtime._lock:
            self.runtime._states[session_id].turn_id = turn_id

        def host_event(payload: dict[str, object]) -> None:
            self.runtime._handle_host_event({
                "protocolVersion": "2",
                "event": "agent.event",
                "sessionId": session_id,
                "turnId": turn_id,
                "payload": payload,
            })

        host_event({
            "type": "auto_retry_start",
            "attempt": 6,
            "maxAttempts": 6,
            "delayMs": 64_000,
            "errorMessage": "private upstream diagnostic",
        })
        host_event({
            "type": "agent_end",
            "willRetry": False,
            "messages": [{
                "role": "assistant",
                "stopReason": "error",
                "errorMessage": "fetch failed",
                "content": [],
            }],
        })
        host_event({
            "type": "auto_retry_end",
            "attempt": 6,
            "success": False,
            "finalError": "private upstream diagnostic",
        })
        host_event({"type": "agent_settled"})

        failed = [
            event for event in self.events.replay(session_id)[0]
            if event.event_type == "turn_failed" and event.turn_id == turn_id
        ]
        self.assertEqual(len(failed), 1)
        self.assertTrue(failed[0].payload["retryExhausted"])
        self.assertEqual(failed[0].payload["providerRetryAttempts"], 6)
        self.assertEqual(failed[0].payload["providerRetryMaxAttempts"], 6)
        self.assertIn("自动重试", failed[0].payload["nextStep"])
        self.assertNotIn(
            "private upstream diagnostic",
            json.dumps(failed[0].payload, ensure_ascii=False),
        )

    def test_nonfatal_extension_error_does_not_terminalize_active_turn(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        turn_id = "turn-extension-warning"
        with self.runtime._lock:
            self.runtime._states[session_id].turn_id = turn_id

        self.runtime._handle_host_event({
            "protocolVersion": "2",
            "event": "agent.event",
            "sessionId": session_id,
            "turnId": turn_id,
            "payload": {
                "type": "extension_error",
                "extensionPath": "session-context-refresh.ts",
                "event": "before_agent_start",
                "error": "optional context refresh failed",
            },
        })

        events = self.events.replay(session_id)[0]
        warnings = [
            event for event in events
            if event.event_type == "status_changed"
            and event.payload.get("phase") == "extension_warning"
        ]
        self.assertEqual(warnings[-1].payload["status"], "working")
        self.assertEqual(
            warnings[-1].payload["extensionEvent"],
            "before_agent_start",
        )
        self.assertFalse(
            any(event.event_type == "turn_failed" for event in events)
        )
        with self.runtime._lock:
            self.assertEqual(
                self.runtime._states[session_id].turn_id,
                turn_id,
            )

        self.runtime._handle_host_event({
            "protocolVersion": "2",
            "event": "agent.event",
            "sessionId": session_id,
            "turnId": turn_id,
            "payload": {
                "type": "agent_end",
                "messages": [{
                    "role": "assistant",
                    "content": [{"type": "text", "text": "继续并完成"}],
                }],
            },
        })
        self.runtime._handle_host_event({
            "protocolVersion": "2",
            "event": "agent.event",
            "sessionId": session_id,
            "turnId": turn_id,
            "payload": {"type": "agent_settled"},
        })

        events = self.events.replay(session_id)[0]
        self.assertFalse(
            any(event.event_type == "turn_failed" for event in events)
        )
        completed = [
            event for event in events
            if event.event_type == "turn_completed"
        ]
        self.assertEqual(
            completed[-1].payload["terminalEvent"],
            "agent_settled",
        )

    def test_tool_loop_no_progress_becomes_one_public_failed_terminal(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        turn_id = "turn-tool-loop-no-progress"
        with self.runtime._lock:
            state = self.runtime._states[session_id]
            state.turn_id = turn_id
            state.had_tool_activity = True

        self.runtime._handle_host_event({
            "protocolVersion": "2",
            "event": "agent.event",
            "sessionId": session_id,
            "turnId": turn_id,
            "payload": {
                "type": "tool_loop_no_progress",
                "message": "Tool Loop 连续未产生成功结果，已按受管无进展策略停止。",
                "reason": "repeated_failure_signature",
                "consecutiveAllErrorTurns": 3,
                "repeatedFailureSignature": 3,
                "toolNames": ["read"],
            },
        })
        self.runtime._handle_host_event({
            "protocolVersion": "2",
            "event": "agent.event",
            "sessionId": session_id,
            "turnId": turn_id,
            "payload": {
                "type": "agent_end",
                "messages": [{
                    "role": "assistant",
                    "stopReason": "toolUse",
                    "content": [{
                        "type": "toolCall",
                        "id": "call-3",
                        "name": "read",
                        "arguments": {"path": "/missing"},
                    }],
                }],
            },
        })
        self.runtime._handle_host_event({
            "protocolVersion": "2",
            "event": "agent.event",
            "sessionId": session_id,
            "turnId": turn_id,
            "payload": {"type": "agent_settled"},
        })

        events = self.events.replay(session_id)[0]
        guarded = [
            event for event in events
            if event.event_type == "status_changed"
            and event.payload.get("phase") == "tool_loop_no_progress"
        ]
        self.assertEqual(guarded[-1].payload["reason"], "repeated_failure_signature")
        self.assertEqual(guarded[-1].payload["toolNames"], ["read"])
        self.assertIn("工具 read", guarded[-1].payload["nextStep"])
        failed = [
            event for event in events
            if event.event_type == "turn_failed"
            and event.turn_id == turn_id
        ]
        self.assertEqual(len(failed), 1)
        self.assertIn("受管无进展策略停止", failed[0].payload["error"])
        self.assertEqual(failed[0].payload["reason"], "repeated_failure_signature")
        self.assertEqual(failed[0].payload["toolNames"], ["read"])
        self.assertIn("当前任务上重试", failed[0].payload["nextStep"])
        self.assertFalse(failed[0].payload["retryable"])
        self.assertEqual(self.store.get(session_id)["status"], "faulted")

    def test_snapshot_collapses_only_adjacent_duplicate_assistant_projection(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.prompt(session_id, "duplicate-final-snapshot")
        _wait_until(lambda: self.store.get(session_id)["status"] == "idle")

        messages = self.runtime.messages(session_id)

        self.assertEqual([item["role"] for item in messages], ["user", "assistant"])
        self.assertEqual(
            messages[-1]["blocks"][0]["data"]["text"],
            f"host reply for {session_id}",
        )

    def test_snapshot_uses_durable_entries_to_remove_cross_turn_assistant_replay(self) -> None:
        session_id = str(self.first["id"])
        first = self.runtime.prompt(session_id, "first turn")
        _wait_until(lambda: self.store.get(session_id)["status"] == "idle")
        second = self.runtime.prompt(session_id, "duplicate-across-native-followup")
        _wait_until(lambda: self.store.get(session_id)["status"] == "idle")
        _wait_until(lambda: any(
            event.event_type == "turn_completed"
            and event.turn_id == second["turnId"]
            for event in self.events.replay(session_id)[0]
        ))
        self.assertNotEqual(first["turnId"], second["turnId"])
        completed = [event.turn_id for event in self.events.replay(session_id)[0]
                     if event.event_type == "turn_completed"]
        self.assertEqual(completed, [first["turnId"], second["turnId"]])

        messages = self.runtime.messages(session_id)
        assistant_texts = [
            item["blocks"][0]["data"]["text"]
            for item in messages
            if item["role"] == "assistant"
        ]

        self.assertEqual(
            assistant_texts,
            [
                f"host reply for {session_id}",
                f"second host reply for {session_id}",
            ],
        )

    def test_native_capabilities_read_resident_owner_without_prompt_or_configuration_payload(self) -> None:
        session_id = str(self.first["id"])
        native = {"schemaVersion": "rag-ime.pi-native-capabilities.v1", "codemodeMode": "on",
                  "mcp": {"available": True, "active": True, "configErrorCount": 0,
                          "servers": [{"name": "docs", "namespace": "mcp__docs", "scope": "global",
                                      "enabled": True, "state": "connected", "exposure": "codemode",
                                      "toolCount": 1, "url": "private-transport", "env": {"SECRET": "private"}}]},
                  "tools": [{"name": "mcp__docs__search", "namespace": {"name": "mcp__docs", "instructions": "not-in-catalog"},
                             "exposure": "codemode", "active": False, "routable": True,
                             "description": "Search docs", "parameters": {"type": "object"}}]}
        with patch.object(self.runtime, "_inspection_snapshot") as inspect, patch.object(self.runtime, "_require_client") as client:
            client.return_value.send.return_value = {"nativeCapabilities": native}
            response = self.runtime.native_capabilities(session_id)
        inspect.assert_called_once_with(session_id, durable_fallback=False)
        client.return_value.send.assert_called_once_with("tools.list", {"sessionId": session_id})
        self.assertEqual(response["sessionId"], session_id)
        self.assertEqual(response["tools"][0]["namespace"], {"name": "mcp__docs"})
        self.assertFalse(response["tools"][0]["active"])
        self.assertTrue(response["tools"][0]["routable"])
        self.assertNotIn("private", json.dumps(response))
        self.assertNotIn("not-in-catalog", json.dumps(response))

    def test_native_capabilities_do_not_promote_an_unsupported_or_malformed_owner_to_empty_success(self) -> None:
        session_id = str(self.first["id"])
        for raw in ({}, {"schemaVersion": "rag-ime.pi-native-capabilities.v1", "mcp": {"available": False}},
                    {"schemaVersion": "rag-ime.pi-native-capabilities.v1", "mcp": {"available": True, "servers": "invalid"}, "tools": []}):
            with self.subTest(raw=raw), patch.object(self.runtime, "_inspection_snapshot"), patch.object(self.runtime, "_require_client") as client:
                client.return_value.send.return_value = {"nativeCapabilities": raw}
                with self.assertRaises(PiRuntimeError):
                    self.runtime.native_capabilities(session_id)

    def test_v2_exposes_managed_skill_commands_to_the_composer(self) -> None:
        session_id = str(self.first["id"])

        self.assertEqual(
            self.runtime.command_catalog(session_id),
            [
                {
                    "name": "skill:plugin-creator",
                    "invocation": "/skill:plugin-creator",
                    "description": "Create and propose a managed plugin",
                    "source": "skill",
                }
            ],
        )

    def test_v2_skill_catalog_reads_actual_session_commands_without_snapshot_or_turn(self) -> None:
        session_id = str(self.first["id"])
        before = (self.root / "agent" / "host-requests.jsonl")
        original = before.read_text(encoding="utf-8").splitlines() if before.exists() else []
        self.assertEqual(self.runtime.skill_catalog(session_id), [
            {"name": "skill:plugin-creator", "source": "skill"},
        ])
        self.assertEqual(self.runtime.skill_catalog(session_id), [
            {"name": "skill:plugin-creator", "source": "skill"},
        ])
        requests = [json.loads(line) for line in before.read_text(encoding="utf-8").splitlines()[len(original):]]
        self.assertEqual(requests[-1]["method"], "session.commands")
        self.assertNotIn("session.snapshot", [item["method"] for item in requests])
        self.assertNotIn("session.prompt", [item["method"] for item in requests])

    def test_v2_invokes_pi_package_command_without_starting_model_turn(self) -> None:
        session_id = str(self.first["id"])
        receipt = self.runtime.invoke_command(session_id, "/workflow")
        self.assertEqual(receipt["name"], "workflow")
        self.assertTrue(receipt["handled"])
        self.assertEqual(
            receipt["result"]["message"],
            "Goal [active]: Ship the TUI",
        )
        requests = [
            json.loads(line)
            for line in (
                self.root / "agent" / "host-requests.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(requests[-1]["method"], "session.command.invoke")
        self.assertNotIn(
            "session.prompt",
            [item["method"] for item in requests[-2:]],
        )

    def test_v2_fork_uses_host_owned_anchor_and_binds_a_distinct_target(self) -> None:
        first_id = str(self.first["id"])
        second_id = str(self.second["id"])
        self.assertTrue(self.runtime.runtime_status()["capabilities"]["conversationFork"])
        self.runtime.prompt(first_id, "从这条消息建立分支")
        _wait_until(lambda: self.store.get(first_id)["status"] == "idle")
        self.assertTrue(self.runtime.runtime_status()["capabilities"]["transientContext"])
        source_binding = self.store.runtime_binding(first_id)

        self.assertEqual(
            self.runtime.fork_candidates(first_id),
            [
                {
                    "entryId": "entry-user-1",
                    "text": "从这条消息建立分支",
                    "role": "user",
                    "createdAtMs": 100,
                },
                {
                    "entryId": "entry-assistant-1",
                    "text": f"host reply for {first_id}",
                    "role": "assistant",
                    "createdAtMs": 101,
                },
            ],
        )
        forked = self.runtime.fork_session(first_id, second_id, entry_id="entry-user-1")

        self.assertTrue(self.runtime.runtime_status()["capabilities"]["conversationFork"])
        self.assertEqual(self.store.runtime_binding(first_id), source_binding)
        target_binding = self.store.runtime_binding(second_id)
        self.assertIsNotNone(target_binding)
        assert source_binding is not None and target_binding is not None
        self.assertNotEqual(target_binding["externalSessionId"], source_binding["externalSessionId"])
        self.assertNotEqual(target_binding["transcriptRef"], source_binding["transcriptRef"])
        self.assertEqual(target_binding["branchAnchor"], "entry-user-1")
        self.assertEqual(target_binding["metadata"]["forkedFromSessionId"], first_id)
        self.assertEqual(forked["selectedText"], "从这条消息建立分支")
        self.assertEqual(self.store.get(first_id)["status"], "idle")
        self.assertEqual(self.store.get(second_id)["status"], "idle")

        self.runtime.prompt(second_id, "只发送到新分支")
        _wait_until(lambda: self.store.get(second_id)["status"] == "idle")
        self.assertEqual(self.runtime.messages(second_id)[0]["blocks"][0]["data"]["text"], "只发送到新分支")

    def test_v2_fork_after_assistant_keeps_answer_context_without_restoring_draft(self) -> None:
        first_id = str(self.first["id"])
        second_id = str(self.second["id"])
        self.runtime.prompt(first_id, "先回答这个问题")
        _wait_until(lambda: self.store.get(first_id)["status"] == "idle")

        forked = self.runtime.fork_session(
            first_id,
            second_id,
            entry_id="entry-assistant-1",
        )

        self.assertEqual(forked["selectedText"], "")
        target_binding = self.store.runtime_binding(second_id)
        self.assertIsNotNone(target_binding)
        assert target_binding is not None
        self.assertEqual(target_binding["branchAnchor"], "entry-assistant-1")

    def test_v2_fork_catalog_exposes_only_the_public_deep_search_question(self) -> None:
        first_id = str(self.first["id"])
        self.runtime.prompt(
            first_id,
            "<agent-deep-search-context>private evidence</agent-deep-search-context>\n"
            "<agent-user-query>最近做了什么？</agent-user-query>\n"
            "本地时间：2026-07-16",
        )
        _wait_until(lambda: self.store.get(first_id)["status"] == "idle")

        candidates = self.runtime.fork_candidates(first_id)

        self.assertEqual(
            candidates,
            [
                {
                    "entryId": "entry-user-1",
                    "text": "最近做了什么？",
                    "role": "user",
                    "createdAtMs": 100,
                },
                {
                    "entryId": "entry-assistant-1",
                    "text": f"host reply for {first_id}",
                    "role": "assistant",
                    "createdAtMs": 101,
                },
            ],
        )
        self.assertNotIn("private evidence", json.dumps(candidates, ensure_ascii=False))

    def test_v2_fork_rejects_unknown_anchor_without_binding_target(self) -> None:
        first_id = str(self.first["id"])
        second_id = str(self.second["id"])
        self.runtime.prompt(first_id, "原始消息")
        _wait_until(lambda: self.store.get(first_id)["status"] == "idle")
        source_binding = self.store.runtime_binding(first_id)

        with self.assertRaisesRegex(PiRuntimeError, "not available"):
            self.runtime.fork_session(first_id, second_id, entry_id="entry-missing")

        self.assertEqual(self.store.runtime_binding(first_id), source_binding)
        self.assertIsNone(self.store.runtime_binding(second_id))

    def test_handled_prompt_recovers_a_lost_settled_event_without_fabricating_output(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.prompt(session_id, "older ordinary reply", client_message_id="earlier-client")
        _wait_until(lambda: self.store.get(session_id)["status"] == "idle")
        handled = self.runtime.prompt(session_id, "preflight-handled", client_message_id="handled-client")
        self.assertEqual(handled["disposition"], "handled")
        self.assertTrue(handled["accepted"])
        self.assertNotIn("abortRequested", handled)
        self.assertEqual(self.store.get(session_id)["status"], "idle")
        settlement = self.runtime.await_turn_settled(session_id, str(handled["turnId"]),
            client_message_id="handled-client", timeout_seconds=1)
        self.assertEqual(settlement["receipt"]["stopReason"], "prompt_handled")
        self.assertNotIn("finalMessage", settlement["receipt"])
        events = [event for event in self.events.replay(session_id)[0] if event.turn_id == handled["turnId"]]
        self.assertEqual(sum(event.event_type == "turn_completed" for event in events), 1)
        self.assertFalse(any(event.event_type == "message_completed" for event in events))
        next_prompt = self.runtime.prompt(session_id, "next ordinary task", client_message_id="next-client")
        self.assertTrue(next_prompt["accepted"])
        _wait_until(lambda: self.store.get(session_id)["status"] == "idle")
        self.assertEqual(sum(item["method"] == "session.prompt" for item in self._host_request_log()), 3)

    def test_handled_ack_preserves_the_settlement_abort_outcome(self) -> None:
        session_id = str(self.first["id"])
        handled = self.runtime.prompt(session_id, "preflight-handled-aborted", client_message_id="handled-aborted")
        self.assertEqual(handled["disposition"], "handled")
        self.assertTrue(handled["abortRequested"])
        self.assertEqual(self.store.get(session_id)["status"], "idle")
        self.assertEqual(self.store.get(session_id)["lastMessagePreview"], "已停止。")
        terminal = [event for event in self.events.replay(session_id)[0] if event.event_type == "turn_completed"]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0].turn_id, handled["turnId"])
        self.assertTrue(terminal[0].payload["aborted"])

    def test_late_handled_ack_preserves_a_newer_prompt_reservation(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        client = self.runtime._require_client()
        original_send = client.send
        ack_received = threading.Event()
        release_ack = threading.Event()
        results: list[dict[str, object]] = []
        errors: list[Exception] = []

        def hold_ack(method, params=None, **kwargs):
            result = original_send(method, params, **kwargs)
            if method == "session.prompt":
                ack_received.set()
                if not release_ack.wait(5):
                    raise TimeoutError("test did not release the captured ACK")
            return result

        def submit():
            try:
                results.append(self.runtime.prompt(session_id, "preflight-handled-before-ack", client_message_id="handled-original"))
            except Exception as error:
                errors.append(error)

        with patch.object(client, "send", side_effect=hold_ack):
            worker = threading.Thread(target=submit)
            worker.start()
            try:
                self.assertTrue(ack_received.wait(5))
                _wait_until(lambda: self.store.get(session_id)["status"] == "idle")
                self.runtime.reserve_prompt_admission(session_id, client_message_id="newer-client")
            finally:
                release_ack.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[0]["disposition"], "handled")
        self.assertNotIn("abortRequested", results[0])
        self.runtime.require_prompt_admission_active(session_id, client_message_id="newer-client")
        self.assertEqual(self.store.get(session_id)["status"], "busy")

    def test_terminal_projection_cannot_overwrite_a_new_prompt_reservation(self) -> None:
        self._assert_terminal_projection_preserves_successor("preflight-handled", "idle")

    def test_failed_terminal_projection_cannot_fault_a_new_prompt_reservation(self) -> None:
        self._assert_terminal_projection_preserves_successor("preflight-handled-failed", "faulted")

    def _assert_terminal_projection_preserves_successor(self, message: str, terminal_status: str) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        terminal_write_entered = threading.Event()
        reservation_attempted = threading.Event()
        reservation_finished = threading.Event()
        release_terminal = threading.Event()
        original_set_status = self.store.set_status
        errors: list[Exception] = []
        results: list[dict[str, object]] = []

        def hold_terminal_write(sid, status, **kwargs):
            if sid == session_id and status == terminal_status and threading.current_thread() is terminal_worker:
                terminal_write_entered.set()
                if not release_terminal.wait(5):
                    raise TimeoutError("test did not release the terminal store write")
            return original_set_status(sid, status, **kwargs)

        def finish_old_prompt():
            try:
                results.append(self.runtime.prompt(session_id, message, client_message_id="old-owner"))
            except Exception as error:
                errors.append(error)

        def reserve_new_prompt():
            reservation_attempted.set()
            try:
                self.runtime.reserve_prompt_admission(session_id, client_message_id="new-owner")
            except Exception as error:
                errors.append(error)
            finally:
                reservation_finished.set()

        terminal_worker = threading.Thread(target=finish_old_prompt)
        reservation_worker = threading.Thread(target=reserve_new_prompt)
        with patch.object(self.store, "set_status", side_effect=hold_terminal_write):
            terminal_worker.start()
            try:
                self.assertTrue(terminal_write_entered.wait(5))
                reservation_worker.start()
                self.assertTrue(reservation_attempted.wait(5))
                # Exercise the interleaving if admission can finish before the
                # old store write. Correct serialization keeps it pending; the
                # deadline only releases that barrier, not a timing assertion.
                reservation_finished.wait(0.5)
            finally:
                release_terminal.set()
                terminal_worker.join(5)
                if reservation_worker.ident is not None:
                    reservation_worker.join(5)
        self.assertFalse(terminal_worker.is_alive())
        self.assertFalse(reservation_worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[0]["disposition"], "handled")
        self.runtime.require_prompt_admission_active(session_id, client_message_id="new-owner")
        self.assertEqual(self.store.get(session_id)["status"], "busy")

    def test_handled_prompt_requires_an_exact_authoritative_no_run_receipt(self) -> None:
        for message in ("preflight-handled-wrong-client", "preflight-handled-without-origin"):
            with self.subTest(message=message):
                session = self.store.create(title=message)
                session_id = str(session["id"])
                with self.assertRaises(PiRuntimeCommandAcceptanceUnknown):
                    self.runtime.prompt(session_id, message, client_message_id=message)
                self.assertEqual(self.store.get(session_id)["status"], "busy")
                self.assertFalse(any(event.event_type == "turn_completed" for event in self.events.replay(session_id)[0]))
                with self.assertRaises(PiRuntimeError):
                    self.runtime.prompt(session_id, "must not replay the handled effect")

    def test_prompt_rejects_a_second_turn_until_the_active_turn_settles(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        with self.runtime._lock:
            self.runtime._states[session_id].turn_id = "turn-still-aborting"

        with self.assertRaisesRegex(PiRuntimeError, "上一轮") as conflict:
            self.runtime.prompt(session_id, "不要覆盖旧回合")
        self.assertEqual(
            conflict.exception.error_code,
            "AGENT_TURN_CONFLICT",
        )

    def test_prompt_uses_turn_timeout_instead_of_short_control_timeout(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        client = self.runtime._require_client()
        original_send = client.send
        prompt_timeouts: list[float | None] = []

        def record_send(
            method: str,
            params: dict[str, object] | None = None,
            *,
            timeout: float | None = None,
            before_write=None,
        ) -> dict[str, object]:
            if method == "session.prompt":
                prompt_timeouts.append(timeout)
            return original_send(
                method,
                params,
                timeout=timeout,
                before_write=before_write,
            )

        with patch.object(client, "send", side_effect=record_send):
            self.runtime.prompt(session_id, "允许一轮长时间使用工具")

        self.assertEqual(prompt_timeouts, [3_600.0])
        _wait_until(lambda: self.store.get(session_id)["status"] == "idle")

    def test_prompt_reuses_resident_session_without_control_state_round_trip(
        self,
    ) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        client = self.runtime._require_client()
        original_send = client.send
        methods: list[str] = []

        def record_send(
            method: str,
            params: dict[str, object] | None = None,
            *,
            timeout: float | None = None,
            before_write=None,
        ) -> dict[str, object]:
            methods.append(method)
            return original_send(
                method,
                params,
                timeout=timeout,
                before_write=before_write,
            )

        with patch.object(client, "send", side_effect=record_send):
            self.runtime.prompt(session_id, "复用已驻留的会话")

        self.assertIn("session.prompt", methods)
        self.assertNotIn("session.control_state", methods)
        _wait_until(lambda: self.store.get(session_id)["status"] == "idle")

    def test_pending_prompt_admission_prevents_idle_shutdown(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.config = replace(
            self.runtime.config,
            idle_timeout_seconds=60,
        )
        self.runtime.ensure(session_id)
        self.runtime.reserve_prompt_admission(
            session_id,
            client_message_id="memory-curation-admission",
        )

        # An unrelated idle projection may try to reschedule the shared Host
        # while Pi is still admitting this prompt and has not returned a turn
        # id. That admission is active work and must fence idle shutdown.
        with self.runtime._lock:
            self.runtime._schedule_idle_locked()

        self.assertIsNone(self.runtime._idle_timer)
        self.assertTrue(
            self.runtime.release_prompt_admission(
                session_id,
                client_message_id="memory-curation-admission",
            )
        )

    def test_queue_update_cannot_reopen_an_idle_historical_turn(self) -> None:
        session_id = str(self.first["id"])
        self.runtime._handle_host_event({
            "protocolVersion": "2", "event": "agent.event",
            "sessionId": session_id, "turnId": "historical-failed-turn",
            "clientMessageId": "historical-client",
            "payload": {"type": "queue_update", "steering": [], "followUp": []},
        })
        self.assertEqual(self.runtime.runtime_status()["activeSessionIds"], [])
        self.assertNotIn(session_id, self.runtime._states)
        self.assertIsNone(self.runtime._client)
        self.assertEqual(self.store.get(session_id)["status"], "idle")
        queued = self.events.replay(session_id)[0][-1]
        self.assertEqual(queued.event_type, "message_queue_updated")
        self.assertEqual(queued.turn_id, "historical-failed-turn")
        self.assertEqual(queued.payload, {"steering": [], "followUp": []})

    def test_queue_update_preserves_a_newer_live_turn_and_client_identity(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.reserve_prompt_admission(session_id, client_message_id="new-client")
        state = self.runtime._states[session_id]
        with self.runtime._lock:
            state.turn_id = "new-turn"
            state.client_message_id = "new-client"
        self.runtime._handle_host_event({
            "protocolVersion": "2", "event": "agent.event",
            "sessionId": session_id, "turnId": "old-turn",
            "clientMessageId": "old-client",
            "payload": {"type": "queue_update", "steering": ["queued"], "followUp": []},
        })
        self.assertEqual(state.turn_id, "new-turn")
        self.assertEqual(state.client_message_id, "new-client")
        self.assertTrue(state.prompt_admission_in_flight)
        self.assertEqual(self.events.replay(session_id)[0][-1].turn_id, "old-turn")

    def test_queue_update_cannot_acknowledge_a_reserved_prompt_admission(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.reserve_prompt_admission(session_id, client_message_id="reserved-client")
        state = self.runtime._states[session_id]
        self.runtime._handle_host_event({
            "protocolVersion": "2", "event": "agent.event",
            "sessionId": session_id, "turnId": "queue-correlation",
            "clientMessageId": "reserved-client",
            "payload": {"type": "queue_update", "steering": [], "followUp": []},
        })
        self.assertEqual(state.turn_id, "")
        self.assertTrue(state.prompt_admission_in_flight)
        self.assertEqual(state.admission_client_message_id, "reserved-client")

    def test_busy_turn_accepts_native_steer_and_follow_up_messages(self) -> None:
        session_id = str(self.first["id"])
        active = self.runtime.prompt(session_id, "hang-without-settled", client_message_id="initial")

        steered = self.runtime.prompt(
            session_id,
            "先不要修改配置",
            client_message_id="steer-1",
            delivery="steer",
        )
        followed = self.runtime.prompt(
            session_id,
            "完成后再给我测试结果",
            client_message_id="follow-1",
            delivery="followUp",
        )

        self.assertEqual(steered["turnId"], active["turnId"])
        self.assertEqual(followed["turnId"], active["turnId"])
        self.assertTrue(steered["queued"])
        self.assertEqual(followed["delivery"], "followUp")
        _wait_until(
            lambda: len([
                item for item in self.events.replay(session_id)[0]
                if item.event_type == "message_queue_updated"
            ]) >= 2
        )
        queue_events = [
            item for item in self.events.replay(session_id)[0]
            if item.event_type == "message_queue_updated"
        ]
        self.assertEqual(queue_events[-1].payload["steering"], ["先不要修改配置"])
        self.assertEqual(queue_events[-1].payload["followUp"], ["完成后再给我测试结果"])
        snapshot = self.runtime.session_snapshot(session_id)
        self.assertEqual(snapshot["messageQueue"]["steering"], ["先不要修改配置"])
        self.assertEqual(snapshot["messageQueue"]["followUp"], ["完成后再给我测试结果"])
        requests = [json.loads(line) for line in (self.root / "agent" / "host-requests.jsonl").read_text().splitlines()]
        self.assertIn("session.steer", [row["method"] for row in requests])
        self.assertIn("session.follow_up", [row["method"] for row in requests])

    def test_steer_waits_for_reserved_prompt_to_reach_host(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        client = self.runtime._require_client()
        self.runtime.reserve_prompt_admission(
            session_id,
            client_message_id="initial-race",
        )
        original_write = client._write_record
        prompt_write_entered = threading.Event()
        release_prompt_write = threading.Event()
        results: dict[str, object] = {}
        failures: list[BaseException] = []

        def delayed_write(process, request, *, before_write=None) -> None:
            if request.get("method") == "session.prompt":
                prompt_write_entered.set()
                if not release_prompt_write.wait(timeout=2):
                    raise TimeoutError("test did not release prompt write")
            original_write(
                process,
                request,
                before_write=before_write,
            )

        def run_prompt() -> None:
            try:
                results["prompt"] = self.runtime.prompt(
                    session_id,
                    "hang-without-settled",
                    client_message_id="initial-race",
                )
            except BaseException as exc:
                failures.append(exc)

        def run_steer() -> None:
            try:
                results["steer"] = self.runtime.prompt(
                    session_id,
                    "立即改变当前执行",
                    client_message_id="steer-during-admission",
                    delivery="steer",
                )
            except BaseException as exc:
                failures.append(exc)

        with patch.object(
            client,
            "_write_record",
            side_effect=delayed_write,
        ):
            prompt_thread = threading.Thread(target=run_prompt)
            steer_thread = threading.Thread(target=run_steer)
            prompt_thread.start()
            self.assertTrue(prompt_write_entered.wait(timeout=1))
            steer_thread.start()
            self.assertFalse(
                self.runtime._states[
                    session_id
                ].prompt_dispatch_signal.is_set()
            )
            release_prompt_write.set()
            prompt_thread.join(timeout=2)
            steer_thread.join(timeout=2)

        self.assertFalse(prompt_thread.is_alive())
        self.assertFalse(steer_thread.is_alive())
        self.assertEqual(failures, [])
        prompt = results["prompt"]
        steer = results["steer"]
        self.assertEqual(steer["turnId"], prompt["turnId"])
        self.assertTrue(steer["queued"])
        requests = [
            json.loads(line)
            for line in (
                self.root / "agent" / "host-requests.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        ]
        methods = [row["method"] for row in requests]
        self.assertLess(
            methods.index("session.prompt"),
            methods.index("session.steer"),
        )

    def test_host_rejection_preserves_code_and_ends_as_known_failure(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.prompt(
            session_id,
            "hang-without-settled",
            client_message_id="initial-for-rejection",
        )

        with self.assertRaises(PiRuntimeCommandRejected) as rejected:
            self.runtime.prompt(
                session_id,
                "host-reject-session-idle",
                client_message_id="stale-steer",
                delivery="steer",
            )

        self.assertEqual(
            rejected.exception.error_code,
            "PI_RUNTIME_COMMAND_REJECTED",
        )
        self.assertEqual(
            rejected.exception.host_error_code,
            "SESSION_IDLE",
        )

    def test_debug_context_reads_a_resident_busy_session_without_snapshot(
        self,
    ) -> None:
        session_id = str(self.first["id"])
        self.runtime.prompt(
            session_id,
            "hang-without-settled",
            client_message_id="debug-active",
        )

        self.assertEqual(self.runtime.debug_context(session_id), {})

        requests = [
            json.loads(line)
            for line in (
                self.root / "agent" / "host-requests.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        ]
        methods = [item["method"] for item in requests]
        self.assertEqual(methods[-1], "session.debug.context")
        self.assertNotIn("session.snapshot", methods)

    def test_debug_context_does_not_restore_a_nonresident_session(self) -> None:
        session_id = str(self.first["id"])

        response = self.runtime.debug_context(session_id)

        self.assertEqual(
            response,
            {
                "schemaVersion": "rag-ime.pi-debug-context-response.v1",
                "sessionId": session_id,
                "turnId": "",
                "available": False,
                "transient": True,
                "context": None,
                "telemetry": None,
                "reason": "session_not_resident",
            },
        )
        self.assertFalse((self.root / "agent" / "host-requests.jsonl").exists())

    def test_debug_context_timeout_is_bounded_and_does_not_fail_the_session(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.config = replace(
            self.runtime.config,
            provider_environment={"TEST_DEBUG_CONTEXT_HANG": "1"},
        )
        self.runtime.ensure(session_id)
        started = time.monotonic()
        response = self.runtime.debug_context(session_id)

        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(
            response,
            {
                "schemaVersion": "rag-ime.pi-debug-context-response.v1",
                "sessionId": session_id,
                "turnId": "",
                "available": False,
                "transient": True,
                "context": None,
                "telemetry": None,
                "reason": "runtime_unresponsive",
            },
        )

    def test_large_debug_context_reply_preserves_content_within_existing_deadline(self) -> None:
        session_id = str(self.first["id"])
        response_bytes = 12 * 1024 * 1024
        self.runtime.config = replace(
            self.runtime.config,
            provider_environment={"TEST_DEBUG_CONTEXT_RESPONSE_BYTES": str(response_bytes)},
        )
        self.runtime.ensure(session_id)

        response = self.runtime.debug_context(session_id)

        self.assertTrue(response.get("available"), response.get("reason"))
        self.assertEqual(len(response["context"]["prompt"]), response_bytes)
        control = self.runtime._require_client().send(
            "session.control_state", {"sessionId": session_id}, timeout=1.0
        )
        self.assertTrue(control["isIdle"])

    def test_idle_control_probe_closes_tool_loop_when_agent_settled_is_lost(self) -> None:
        session_id = str(self.first["id"])
        accepted = self.runtime.prompt(
            session_id,
            "end-without-settled",
            client_message_id="lost-settled",
        )
        turn_id = str(accepted["turnId"])

        _wait_until(
            lambda: any(
                item.event_type == "turn_completed"
                and item.turn_id == turn_id
                for item in self.events.replay(session_id)[0]
            ),
            timeout=2.5,
        )
        completed = [
            item
            for item in self.events.replay(session_id)[0]
            if item.event_type == "turn_completed"
            and item.turn_id == turn_id
        ]
        self.assertEqual(
            completed[-1].payload["terminalEvent"],
            "idle_control_reconciliation",
        )
        self.assertEqual(completed[-1].payload["status"], "completed")
        self.assertEqual(self.store.get(session_id)["status"], "idle")
        self.assertEqual(self.runtime._states[session_id].turn_id, "")

        continued = self.runtime.prompt(
            session_id,
            "继续发送",
            client_message_id="after-lost-settled",
        )
        self.assertTrue(continued["accepted"])

    def test_authoritative_settlement_reconciles_a_lost_terminal_event(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        turn_id = "turn-memory-settlement"
        client_message_id = "memory-request:exact"
        runtime_session_id = f"pi-{session_id}"
        with self.runtime._lock:
            state = self.runtime._states[session_id]
            state.turn_id = turn_id
            state.client_message_id = client_message_id
        self.store.set_status(session_id, "busy")
        client = self.runtime._require_client()
        original_send = client.send
        settlement_methods: list[str] = []
        await_attempts = 0

        def settlement_send(
            method: str,
            params: dict[str, object] | None = None,
            *,
            timeout: float | None = None,
            before_write=None,
        ) -> dict[str, object]:
            nonlocal await_attempts
            if method in {"session.settlement.get", "session.await_settled"}:
                settlement_methods.append(method)
            if method == "session.settlement.get":
                return {
                    "settlement": {
                        "schemaVersion": "rag-ime.pi-turn-settlement.v1",
                        "sessionId": session_id,
                        "runtimeSessionId": runtime_session_id,
                        "turnId": turn_id,
                        "clientMessageId": client_message_id,
                        "receipt": {
                            "schemaVersion": "pi.agent-settled.v2",
                            "receiptId": "pi-settled:memory-suspended",
                            "sessionId": runtime_session_id,
                            "runId": turn_id,
                            "scopeId": f"{runtime_session_id}:{turn_id}",
                            "generation": 1,
                            "disposition": "suspended",
                            "stopReason": "extension_work_pending",
                            "settledAtMs": 199,
                            "aborted": False,
                            "pendingOperations": 1,
                            "operations": {
                                "pending": 1,
                                "pendingByKind": {"extension": 1},
                                "registeredByKind": {"extension": 1},
                            },
                            "operationCounts": {"extension": 1},
                        },
                    }
                }
            if method == "session.await_settled":
                await_attempts += 1
                if await_attempts == 1:
                    raise PiRuntimeError(
                        "Pi Runtime Host command timed out: session.await_settled"
                    )
                assert params is not None
                self.assertEqual(params["sessionId"], session_id)
                self.assertEqual(params["turnId"], turn_id)
                self.assertEqual(params["clientMessageId"], client_message_id)
                self.assertIs(params["allowSuspended"], False)
                self.assertGreaterEqual(int(params["timeoutMs"]), 1_000)
                self.assertLessEqual(int(params["timeoutMs"]), 2_000)
                return {
                    "schemaVersion": "rag-ime.pi-turn-settlement.v1",
                    "sessionId": session_id,
                    "runtimeSessionId": runtime_session_id,
                    "turnId": turn_id,
                    "clientMessageId": client_message_id,
                    "receipt": {
                        "schemaVersion": "pi.agent-settled.v2",
                        "receiptId": "pi-settled:memory-exact",
                        "sessionId": runtime_session_id,
                        "runId": turn_id,
                        "scopeId": f"{runtime_session_id}:{turn_id}",
                        "generation": 1,
                        "disposition": "completed",
                        "stopReason": "stop",
                        "settledAtMs": 200,
                        "aborted": False,
                        "pendingOperations": 0,
                        "operations": {
                            "pending": 0,
                            "pendingByKind": {},
                            "registeredByKind": {},
                        },
                        "operationCounts": {},
                        "finalMessage": {
                            "role": "assistant",
                            "content": [
                                {"type": "text", "text": '{"decisions":[]}'},
                            ],
                            "usage": {"input": 10, "output": 4},
                        },
                    },
                }
            return original_send(
                method,
                params,
                timeout=timeout,
                before_write=before_write,
            )

        with patch.object(client, "send", side_effect=settlement_send):
            settlement = self.runtime.await_turn_settled(
                session_id,
                turn_id,
                client_message_id=client_message_id,
                timeout_seconds=2.0,
            )

        self.assertEqual(settlement["turnId"], turn_id)
        self.assertEqual(
            settlement_methods,
            [
                "session.settlement.get",
                "session.await_settled",
                "session.settlement.get",
                "session.await_settled",
            ],
        )
        self.assertEqual(self.runtime._states[session_id].turn_id, "")
        self.assertEqual(self.store.get(session_id)["status"], "idle")
        events = self.events.replay(session_id)[0]
        self.assertTrue(
            any(
                event.event_type == "message_completed" and event.turn_id == turn_id
                for event in events
            )
        )
        completed = [
            event
            for event in events
            if event.event_type == "turn_completed" and event.turn_id == turn_id
        ]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].payload["terminalEvent"], "agent_settled")

        final_message = dict(settlement["receipt"]["finalMessage"])
        for payload in (
            {"type": "message_end", "message": final_message},
            {"type": "agent_end", "messages": [final_message]},
            {"type": "agent_settled", "receipt": settlement["receipt"]},
        ):
            self.runtime._handle_host_event(
                {
                    "protocolVersion": "2",
                    "event": "agent.event",
                    "sessionId": session_id,
                    "turnId": turn_id,
                    "clientMessageId": client_message_id,
                    "payload": payload,
                }
            )
            self.assertEqual(self.runtime._states[session_id].turn_id, "")

        replayed = self.events.replay(session_id)[0]
        self.assertEqual(
            len(
                [
                    event
                    for event in replayed
                    if event.event_type == "message_completed"
                    and event.turn_id == turn_id
                ]
            ),
            1,
        )
        self.assertEqual(
            len(
                [
                    event
                    for event in replayed
                    if event.event_type == "turn_completed"
                    and event.turn_id == turn_id
                ]
            ),
            1,
        )

    def _restart_accepted_memory_request(self, *, durable_settlement: bool = True):
        self.runtime.config = replace(
            self.runtime.config,
            provider="openai-codex",
            provider_environment={"TEST_MEMORY_SETTLEMENT_FIXTURE": "1"},
        )
        executor = build_governed_memory_model_executor(
            self.runtime,
            "openai-codex/gpt-5.6-luna",
            "max",
            timeout_seconds=1.0,
            db_path=self.root / "rag-ime.sqlite",
        )
        run = executor.begin_run("memory_durable_cold_recovery")
        messages = [{"role": "user", "content": '{"fixture":true}'}]
        # Pi accepts and persists the result, while the first caller loses all
        # settlement responses (including the bounded late-result lookup).
        with patch.object(
            self.runtime, "await_turn_settled", side_effect=TimeoutError("lost result")
        ):
            with self.assertRaises(MemoryModelTimeout):
                executor.complete(messages=messages)
        request = executor.run_status(run["runId"])["requests"][0]
        transcript = Path(self.store.get(run["sessionId"])["sessionFile"])
        entries = [json.loads(line) for line in transcript.read_text().splitlines()]
        self.assertTrue(any(entry.get("customType") == "rag-ime.pi-turn-settlement" for entry in entries))
        if not durable_settlement:
            transcript.write_text("".join(
                json.dumps(entry) + "\n" for entry in entries
                if entry.get("customType") != "rag-ime.pi-turn-settlement"
            ))
        config = self.runtime.config
        self.runtime.stop()
        self.runtime = PiRuntimeHostManager(config=config, sessions=self.store, events=self.events)
        recovered = build_governed_memory_model_executor(
            self.runtime,
            "openai-codex/gpt-5.6-luna",
            "max",
            timeout_seconds=1.0,
            db_path=self.root / "rag-ime.sqlite",
        )
        recovered.begin_run(run["runId"])
        self.assertNotIn(run["sessionId"], self.runtime._open_sessions)
        return recovered, run, request, messages

    def _host_request_log(self):
        return [json.loads(line) for line in (
            self.root / "agent" / "host-requests.jsonl"
        ).read_text().splitlines()]

    def test_memory_recovers_nonresident_durable_turn_without_reprompting(self) -> None:
        executor, run, original, messages = self._restart_accepted_memory_request()
        recovered = executor.complete(messages=messages)
        self.assertEqual(recovered["turnId"], original["turnId"])
        self.assertEqual(recovered["receipt"]["sessionId"], run["sessionId"])
        self.assertTrue(recovered["receipt"]["recoveredSettlement"])
        self.assertEqual(recovered["choices"][0]["message"]["content"], '{"decisions":[]}')
        request = executor.run_status(run["runId"])["requests"][0]
        self.assertEqual(request["state"], "completed")
        self.assertEqual(request["attemptCount"], 1)
        self.assertEqual(executor.complete(messages=messages), recovered)
        requests = self._host_request_log()
        self.assertEqual(sum(item["method"] == "session.prompt" for item in requests), 1)
        opens = [item for item in requests if item["method"] == "session.open"]
        self.assertEqual(len(opens), 2)
        self.assertEqual(opens[-1]["params"]["sessionId"], run["sessionId"])
        self.assertEqual(opens[-1]["params"]["sessionFile"], self.store.get(run["sessionId"])["sessionFile"])

    def test_memory_missing_durable_settlement_remains_unresolved_without_retirement(self) -> None:
        executor, run, original, messages = self._restart_accepted_memory_request(durable_settlement=False)
        aborts_before = sum(item["method"] == "session.abort" for item in self._host_request_log())
        with self.assertRaisesRegex(MemoryModelTimeout, "remains unresolved.*not replayed"):
            executor.complete(messages=messages)
        request = executor.run_status(run["runId"])["requests"][0]
        self.assertEqual(request["state"], "resumable")
        self.assertEqual(request["turnId"], original["turnId"])
        self.assertEqual(request["attemptCount"], 1)
        requests = self._host_request_log()
        self.assertEqual(sum(item["method"] == "session.prompt" for item in requests), 1)
        self.assertEqual(sum(item["method"] == "session.abort" for item in requests), aborts_before)
        self.assertIn(run["sessionId"], self.runtime._open_sessions)

    def test_memory_recovery_does_not_require_or_replace_a_live_product_turn(self) -> None:
        executor, run, original, messages = self._restart_accepted_memory_request()
        self.runtime.ensure(run["sessionId"])
        with self.runtime._lock:
            state = self.runtime._states[run["sessionId"]]
            self.assertEqual(state.turn_id, "")
            state.turn_id = "newer-live-turn"
            state.client_message_id = "newer-live-request"
        self.store.set_status(run["sessionId"], "busy")
        recovered = executor.complete(messages=messages)
        self.assertEqual(recovered["turnId"], original["turnId"])
        self.assertEqual(self.store.get(run["sessionId"])["status"], "busy")
        with self.runtime._lock:
            self.assertEqual(state.turn_id, "newer-live-turn")
            self.assertEqual(state.client_message_id, "newer-live-request")
        self.assertEqual(sum(item["method"] == "session.prompt" for item in self._host_request_log()), 1)

    def test_memory_lookup_timeout_preserves_accepted_turn_without_aborting(self) -> None:
        executor, run, original, messages = self._restart_accepted_memory_request()
        client = self.runtime._require_client()
        original_send = client.send

        def lookup_timeout(method, params=None, **kwargs):
            if method == "session.settlement.get":
                raise PiRuntimeError("Pi Runtime Host command timed out: session.settlement.get")
            return original_send(method, params, **kwargs)

        aborts_before = sum(item["method"] == "session.abort" for item in self._host_request_log())
        with patch.object(client, "send", side_effect=lookup_timeout):
            with self.assertRaisesRegex(MemoryModelUnavailable, "settlement lookup timed out.*not replayed") as caught:
                executor.complete(messages=messages)
        self.assertNotIsInstance(caught.exception, MemoryModelTimeout)
        request = executor.run_status(run["runId"])["requests"][0]
        self.assertEqual(request["state"], "resumable")
        self.assertEqual(request["turnId"], original["turnId"])
        self.assertEqual(sum(item["method"] == "session.abort" for item in self._host_request_log()), aborts_before)
        self.assertEqual(sum(item["method"] == "session.prompt" for item in self._host_request_log()), 1)

    def test_memory_cold_recovery_rejects_a_different_request_identity(self) -> None:
        executor, run, original, messages = self._restart_accepted_memory_request()
        transcript = Path(self.store.get(run["sessionId"])["sessionFile"])
        entries = [json.loads(line) for line in transcript.read_text().splitlines()]
        for entry in entries:
            if entry.get("customType") == "rag-ime.pi-turn-settlement":
                entry["data"]["clientMessageId"] = "another-memory-request"
        transcript.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
        with self.assertRaisesRegex(MemoryModelUnavailable, "not replayed.*identity mismatch"):
            executor.complete(messages=messages)
        request = executor.run_status(run["runId"])["requests"][0]
        self.assertEqual(request["state"], "resumable")
        self.assertEqual(request["turnId"], original["turnId"])
        self.assertEqual(request["attemptCount"], 1)
        self.assertEqual(sum(item["method"] == "session.prompt" for item in self._host_request_log()), 1)

    def test_settlement_transport_timeout_is_distinct_from_confirmed_wait_timeout(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        client = self.runtime._require_client()
        identity = {"sessionId": session_id, "turnId": "turn-timeout-kind", "clientMessageId": "request-timeout-kind"}
        for transport_failure in (True, False):
            with self.subTest(transport_failure=transport_failure):
                clock = [0.0]
                calls = []

                def wait_timeout(method, params=None, calls=calls, clock=clock, transport_failure=transport_failure, **kwargs):
                    calls.append((method, dict(params)))
                    if method == "session.settlement.get":
                        return {"settlement": None}
                    self.assertEqual(method, "session.await_settled")
                    clock[0] = 2.0
                    if transport_failure:
                        raise PiRuntimeError("Pi Runtime Host command timed out: session.await_settled")
                    raise PiRuntimeCommandRejected("turn timed out", host_error_code="SETTLED_TIMEOUT")

                with patch("rag_ime.pi.runtime.time.monotonic", side_effect=lambda clock=clock: clock[0]):
                    with patch.object(client, "send", side_effect=wait_timeout):
                        with self.assertRaises(TimeoutError) as caught:
                            self.runtime.await_turn_settled(session_id, identity["turnId"], client_message_id=identity["clientMessageId"], timeout_seconds=2.0)
                self.assertEqual(isinstance(caught.exception, PiRuntimeSettlementLookupTimeout), transport_failure)
                self.assertEqual(calls[0], ("session.settlement.get", identity))
                self.assertEqual({key: calls[1][1][key] for key in identity}, identity)
                self.assertEqual(len(calls), 2)

    def test_settlement_lookup_retries_one_timeout_with_the_same_turn_identity(
        self,
    ) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        turn_id = "turn-settlement-lookup-retry"
        client_message_id = "memory-request:lookup-retry"
        runtime_session_id = f"pi-{session_id}"
        with self.runtime._lock:
            state = self.runtime._states[session_id]
            state.turn_id = turn_id
            state.client_message_id = client_message_id
        self.store.set_status(session_id, "busy")
        settlement = {
            "schemaVersion": "rag-ime.pi-turn-settlement.v1",
            "sessionId": session_id,
            "runtimeSessionId": runtime_session_id,
            "turnId": turn_id,
            "clientMessageId": client_message_id,
            "receipt": {
                "schemaVersion": "pi.agent-settled.v2",
                "receiptId": "pi-settled:lookup-retry",
                "sessionId": runtime_session_id,
                "runId": turn_id,
                "scopeId": f"{runtime_session_id}:{turn_id}",
                "generation": 1,
                "disposition": "completed",
                "stopReason": "stop",
                "settledAtMs": 200,
                "aborted": False,
                "pendingOperations": 0,
                "operations": {
                    "pending": 0,
                    "pendingByKind": {},
                    "registeredByKind": {},
                },
                "operationCounts": {},
                "finalMessage": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": '{"decisions":[]}'}],
                },
            },
        }
        client = self.runtime._require_client()
        original_send = client.send
        lookup_params: list[dict[str, object]] = []

        def transient_lookup(
            method: str,
            params: dict[str, object] | None = None,
            *,
            timeout: float | None = None,
            before_write=None,
        ) -> dict[str, object]:
            if method == "session.settlement.get":
                lookup_params.append(dict(params or {}))
                if len(lookup_params) == 1:
                    raise PiRuntimeError(
                        "Pi Runtime Host command timed out: session.settlement.get"
                    )
                return {"settlement": settlement}
            return original_send(
                method,
                params,
                timeout=timeout,
                before_write=before_write,
            )

        with patch.object(client, "send", side_effect=transient_lookup):
            result = self.runtime.await_turn_settled(
                session_id,
                turn_id,
                client_message_id=client_message_id,
                timeout_seconds=2.0,
            )

        identity = {
            "sessionId": session_id,
            "turnId": turn_id,
            "clientMessageId": client_message_id,
        }
        self.assertEqual(lookup_params, [identity, identity])
        self.assertEqual(result["receipt"]["receiptId"], "pi-settled:lookup-retry")

    def test_settlement_lookup_stops_after_one_timeout_retry(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        turn_id = "turn-settlement-lookup-timeout"
        client_message_id = "memory-request:lookup-timeout"
        client = self.runtime._require_client()
        calls: list[dict[str, object]] = []

        def timed_out_lookup(
            method: str,
            params: dict[str, object] | None = None,
            *,
            timeout: float | None = None,
            before_write=None,
        ) -> dict[str, object]:
            self.assertEqual(method, "session.settlement.get")
            calls.append(dict(params or {}))
            raise PiRuntimeError(
                "Pi Runtime Host command timed out: session.settlement.get"
            )

        with patch.object(client, "send", side_effect=timed_out_lookup):
            with self.assertRaisesRegex(
                PiRuntimeSettlementLookupTimeout,
                "settlement lookup timed out",
            ):
                self.runtime.await_turn_settled(
                    session_id,
                    turn_id,
                    client_message_id=client_message_id,
                    timeout_seconds=2.0,
                )

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])

    def test_authoritative_aborted_settlement_projects_idle_abort_once(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        turn_id = "turn-memory-aborted"
        client_message_id = "memory-request:aborted"
        runtime_session_id = f"pi-{session_id}"
        with self.runtime._lock:
            state = self.runtime._states[session_id]
            state.turn_id = turn_id
            state.client_message_id = client_message_id
        self.store.set_status(session_id, "busy")
        settlement = {
            "schemaVersion": "rag-ime.pi-turn-settlement.v1",
            "sessionId": session_id,
            "runtimeSessionId": runtime_session_id,
            "turnId": turn_id,
            "clientMessageId": client_message_id,
            "receipt": {
                "schemaVersion": "pi.agent-settled.v2",
                "receiptId": "pi-settled:memory-aborted",
                "sessionId": runtime_session_id,
                "runId": turn_id,
                "scopeId": f"{runtime_session_id}:{turn_id}",
                "generation": 1,
                "disposition": "aborted",
                "stopReason": "aborted",
                "settledAtMs": 201,
                "aborted": True,
                "pendingOperations": 0,
                "operations": {
                    "pending": 0,
                    "pendingByKind": {},
                    "registeredByKind": {},
                },
                "operationCounts": {},
                "finalMessage": {
                    "role": "assistant",
                    "stopReason": "aborted",
                    "content": [],
                },
            },
        }
        client = self.runtime._require_client()
        original_send = client.send

        def settled_get(
            method: str,
            params: dict[str, object] | None = None,
            *,
            timeout: float | None = None,
            before_write=None,
        ) -> dict[str, object]:
            if method == "session.settlement.get":
                return {"settlement": settlement}
            return original_send(
                method,
                params,
                timeout=timeout,
                before_write=before_write,
            )

        with patch.object(client, "send", side_effect=settled_get):
            result = self.runtime.await_turn_settled(
                session_id,
                turn_id,
                client_message_id=client_message_id,
                timeout_seconds=2.0,
            )

        self.assertEqual(result["receipt"]["disposition"], "aborted")
        self.assertEqual(self.runtime._states[session_id].turn_id, "")
        self.assertEqual(self.store.get(session_id)["status"], "idle")
        events = self.events.replay(session_id)[0]
        self.assertFalse(any(event.event_type == "turn_failed" for event in events))
        completed = [
            event
            for event in events
            if event.event_type == "turn_completed" and event.turn_id == turn_id
        ]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].payload["status"], "aborted")

    def test_abort_during_authoritative_settlement_projection_is_idempotent(
        self,
    ) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        turn_id = "turn-settlement-projecting"
        with self.runtime._lock:
            state = self.runtime._states[session_id]
            state.turn_id = turn_id
            self.runtime._fence_retired_turn_locked(state, session_id, turn_id)
        client = self.runtime._require_client()

        with patch.object(
            client,
            "send",
            side_effect=AssertionError("settled turn must not receive session.abort"),
        ):
            receipt = self.runtime.abort(session_id)

        self.assertIs(receipt["alreadySettled"], True)
        self.assertEqual(receipt["lifecycle"]["reason"], "already_settled")
        self.assertEqual(self.runtime._states[session_id].turn_id, turn_id)
        self.assertEqual(
            self.runtime._states[session_id].abort_requested_turn_id,
            "",
        )


    def test_settled_extension_error_retires_stale_host_turn(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        turn_id = "turn-settled-extension-error"
        assistant = {
            "role": "assistant",
            "content": [{"type": "text", "text": "review complete"}],
        }
        with self.runtime._lock:
            self.runtime._states[session_id].turn_id = turn_id
        self.store.set_status(session_id, "busy")
        self.runtime._handle_host_event({
            "protocolVersion": "2",
            "event": "agent.event",
            "sessionId": session_id,
            "turnId": turn_id,
            "payload": {
                "type": "agent_end",
                "messages": [assistant],
            },
        })
        client = self.runtime._require_client()
        original_send = client.send

        def stale_control_state(
            method: str,
            params: dict[str, object] | None = None,
            *,
            timeout: float | None = None,
        ) -> dict[str, object]:
            if method == "session.control_state":
                return {
                    "isIdle": True,
                    "activeTurn": {"turnId": turn_id},
                }
            return original_send(method, params, timeout=timeout)

        with patch.object(
            client,
            "send",
            side_effect=stale_control_state,
        ):
            self.runtime._settle_fallback_probe(session_id, turn_id)
            self.assertEqual(
                self.runtime._states[session_id].turn_id,
                turn_id,
            )
            self.runtime._handle_host_event({
                "protocolVersion": "2",
                "event": "agent.event",
                "sessionId": session_id,
                "payload": {
                    "type": "extension_error",
                    "event": "agent_settled",
                    "error": "Optional settled hook rejected the final message",
                },
            })
            self.runtime._settle_fallback_probe(session_id, turn_id)

        completed = [
            item
            for item in self.events.replay(session_id)[0]
            if item.event_type == "turn_completed"
            and item.turn_id == turn_id
        ]
        self.assertEqual(
            completed[-1].payload["terminalEvent"],
            "idle_control_reconciliation",
        )
        self.assertEqual(self.store.get(session_id)["status"], "idle")
        self.assertEqual(self.runtime._states[session_id].turn_id, "")

    def test_agent_settle_failed_retires_stale_session_turn(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        turn_id = "turn-session-settle-failed"
        with self.runtime._lock:
            state = self.runtime._states[session_id]
            state.turn_id = turn_id
            state.last_agent_messages = [{
                "role": "assistant",
                "content": [{"type": "text", "text": "review complete"}],
            }]
        self.store.set_status(session_id, "busy")
        client = self.runtime._require_client()
        original_send = client.send

        def stale_control_state(
            method: str,
            params: dict[str, object] | None = None,
            *,
            timeout: float | None = None,
        ) -> dict[str, object]:
            if method == "session.control_state":
                return {
                    "isIdle": True,
                    "activeTurn": {"turnId": turn_id},
                }
            return original_send(method, params, timeout=timeout)

        with patch.object(
            client,
            "send",
            side_effect=stale_control_state,
        ):
            self.runtime._handle_host_event({
                "protocolVersion": "2",
                "event": "agent.event",
                "sessionId": session_id,
                "payload": {
                    "type": "agent_settle_failed",
                    "error": "Session settlement request returned HTTP 400",
                },
            })
            self.runtime._settle_fallback_probe(session_id, turn_id)

        failed = [
            item
            for item in self.events.replay(session_id)[0]
            if item.event_type == "turn_failed"
            and item.turn_id == turn_id
        ]
        self.assertEqual(
            failed[-1].payload["terminalEvent"],
            "idle_control_reconciliation",
        )
        self.assertIn("HTTP 400", failed[-1].payload["error"])
        warnings = [
            item
            for item in self.events.replay(session_id)[0]
            if item.event_type == "status_changed"
            and item.payload.get("phase") == "settlement_warning"
        ]
        self.assertIn("HTTP 400", warnings[-1].payload["warning"])
        self.assertEqual(self.store.get(session_id)["status"], "faulted")
        self.assertEqual(self.runtime._states[session_id].turn_id, "")

    def test_terminal_agent_settle_failed_receipt_fails_the_turn_once(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        turn_id = "turn-terminal-settle-failed"
        with self.runtime._lock:
            state = self.runtime._states[session_id]
            state.turn_id = turn_id
            state.had_tool_activity = True
        self.store.set_status(session_id, "busy")
        envelope = {
            "protocolVersion": "2",
            "event": "agent.event",
            "sessionId": session_id,
            "turnId": turn_id,
            "payload": {
                "type": "agent_settle_failed",
                "error": "Agent finalization failed after tool execution",
                "receipt": {
                    "schemaVersion": "pi.agent-settled.v2",
                    "receiptId": "pi-settled:terminal-failure",
                    "disposition": "failed",
                    "stopReason": "settlement_rejected",
                    "pendingOperations": 0,
                    "operations": {"pending": 0},
                    "finalMessage": {
                        "errorMessage": (
                            "Agent finalization failed after tool execution"
                        ),
                    },
                },
            },
        }

        self.runtime._handle_host_event(envelope)
        self.runtime._handle_host_event(envelope)

        failed = [
            item
            for item in self.events.replay(session_id)[0]
            if item.event_type == "turn_failed" and item.turn_id == turn_id
        ]
        self.assertEqual(len(failed), 1)
        self.assertIn("finalization failed", failed[0].payload["error"])
        self.assertTrue(failed[0].payload["hadToolActivity"])
        self.assertEqual(self.store.get(session_id)["status"], "faulted")
        self.assertEqual(self.runtime._states[session_id].turn_id, "")

    def test_failed_agent_settled_receipt_is_never_completed(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        turn_id = "turn-failed-settled-receipt"
        with self.runtime._lock:
            state = self.runtime._states[session_id]
            state.turn_id = turn_id
            state.last_agent_messages = [{
                "role": "assistant",
                "content": [{"type": "text", "text": "review complete"}],
            }]
        self.store.set_status(session_id, "busy")

        self.runtime._handle_host_event({
            "protocolVersion": "2",
            "event": "agent.event",
            "sessionId": session_id,
            "turnId": turn_id,
            "payload": {
                "type": "agent_settled",
                "receipt": {
                    "schemaVersion": "pi.agent-settled.v2",
                    "receiptId": "pi-settled:failed-agent-settled",
                    "disposition": "failed",
                    "stopReason": "settlement_rejected",
                    "pendingOperations": 0,
                    "operations": {"pending": 0},
                    "finalMessage": {
                        "errorMessage": "Session settlement was rejected",
                    },
                },
            },
        })

        events = self.events.replay(session_id)[0]
        failed = [
            item
            for item in events
            if item.event_type == "turn_failed" and item.turn_id == turn_id
        ]
        completed = [
            item
            for item in events
            if item.event_type == "turn_completed" and item.turn_id == turn_id
        ]
        self.assertEqual(len(failed), 1)
        self.assertEqual(completed, [])
        self.assertIn("settlement was rejected", failed[0].payload["error"])
        self.assertEqual(self.store.get(session_id)["status"], "faulted")

    def test_terminal_aborted_agent_settle_failed_receipt_is_turn_failed(self) -> None:
        session_id = str(self.second["id"])
        self.runtime.ensure(session_id)
        turn_id = "turn-terminal-settle-aborted"
        with self.runtime._lock:
            self.runtime._states[session_id].turn_id = turn_id
        self.store.set_status(session_id, "busy")

        self.runtime._handle_host_event({
            "protocolVersion": "2",
            "event": "agent.event",
            "sessionId": session_id,
            "turnId": turn_id,
            "payload": {
                "type": "agent_settle_failed",
                "error": "Session settlement was aborted",
                "receipt": {
                    "schemaVersion": "pi.agent-settled.v2",
                    "receiptId": "pi-settled:aborted-settlement",
                    "disposition": "aborted",
                    "stopReason": "cancelled",
                    "pendingOperations": 0,
                    "operations": {"pending": 0},
                },
            },
        })

        failed = [
            item
            for item in self.events.replay(session_id)[0]
            if item.event_type == "turn_failed" and item.turn_id == turn_id
        ]
        self.assertEqual(len(failed), 1)
        self.assertEqual(self.store.get(session_id)["status"], "faulted")

    def test_abort_timeout_on_responsive_shared_host_is_isolated_to_session(self) -> None:
        session_id = str(self.first["id"])
        accepted = self.runtime.prompt(session_id, "hang-without-settled")
        turn_id = str(accepted["turnId"])

        self.runtime.abort(session_id)

        _wait_until(
            lambda: any(
                item.event_type == "turn_completed" and item.payload.get("status") == "aborted"
                for item in self.events.replay(session_id)[0]
            ),
            timeout=2.5,
        )
        completed = [item for item in self.events.replay(session_id)[0] if item.event_type == "turn_completed"]
        self.assertEqual(completed[-1].turn_id, turn_id)
        self.assertEqual(completed[-1].payload["status"], "aborted")
        self.assertEqual(
            completed[-1].payload["terminalEvent"],
            "abort_timeout_isolated",
        )
        _wait_until(
            lambda: any(
                item.event_type == "status_changed"
                and item.payload.get("cancellationPending") is True
                for item in self.events.replay(session_id)[0]
            ),
            timeout=2.5,
        )
        escalations = [
            item
            for item in self.events.replay(session_id)[0]
            if item.event_type == "status_changed"
            and item.payload.get("cancellationPending") is True
        ]
        self.assertEqual(escalations[-1].payload["status"], "idle")
        self.assertEqual(escalations[-1].payload["runtimeStatus"], "ready")
        self.assertFalse(escalations[-1].payload["escalated"])
        runtime_status = self.runtime.runtime_status()
        self.assertEqual(runtime_status["status"], "ready")
        self.assertIsNone(
            runtime_status["runtimeHostKillGate"]["lastKillReceipt"]
        )
        client = self.runtime._require_client()
        self.assertTrue(client.running)

        other_session_id = str(self.second["id"])
        self.runtime.prompt(other_session_id, "other-session-survives")
        _wait_until(lambda: self.store.get(other_session_id)["status"] == "idle")

        completed_count = len(completed)
        self.runtime._handle_host_event({
            "protocolVersion": "2",
            "event": "agent.event",
            "sessionId": session_id,
            "turnId": turn_id,
            "payload": {"type": "agent_settled"},
        })
        self.assertEqual(
            len([item for item in self.events.replay(session_id)[0] if item.event_type == "turn_completed"]),
            completed_count,
        )
        self.assertEqual(self.runtime._states[session_id].turn_id, "")

    def test_abort_timeout_kills_host_only_when_health_lane_is_unresponsive(self) -> None:
        session_id = str(self.first["id"])
        accepted = self.runtime.prompt(session_id, "hang-without-settled")
        turn_id = str(accepted["turnId"])
        client = self.runtime._require_client()
        original_send = client.send

        def fail_health(method, params=None, *, timeout=None, before_write=None):
            if method == "health":
                raise PiRuntimeError("Pi Runtime Host command timed out: health")
            return original_send(
                method,
                params,
                timeout=timeout,
                before_write=before_write,
            )

        with patch.object(client, "send", side_effect=fail_health):
            self.runtime.abort(session_id)
            _wait_until(
                lambda: self.runtime.runtime_status()["runtimeHostKillGate"]["lastKillReceipt"]
                is not None,
                timeout=2.5,
            )

        completed = [
            item
            for item in self.events.replay(session_id)[0]
            if item.event_type == "turn_completed" and item.turn_id == turn_id
        ]
        self.assertEqual(completed[-1].payload["terminalEvent"], "abort_timeout_kill")
        kill_gate = self.runtime.runtime_status()["runtimeHostKillGate"]
        self.assertEqual(kill_gate["lastKillReceipt"]["requestKind"], "cancel_timeout")

    def test_abort_timeout_never_kills_a_shared_host_with_an_active_peer(self) -> None:
        session_id = str(self.first["id"])
        peer_session_id = str(self.second["id"])
        accepted = self.runtime.prompt(session_id, "hang-without-settled")
        turn_id = str(accepted["turnId"])
        peer = self.runtime.prompt(peer_session_id, "hang-without-settled")
        peer_turn_id = str(peer["turnId"])
        client = self.runtime._require_client()
        original_send = client.send

        def fail_health(method, params=None, *, timeout=None, before_write=None):
            if method == "health":
                raise PiRuntimeError("Pi Runtime Host command timed out: health")
            return original_send(
                method,
                params,
                timeout=timeout,
                before_write=before_write,
            )

        with patch.object(client, "send", side_effect=fail_health):
            self.runtime.abort(session_id)
            _wait_until(
                lambda: any(
                    item.event_type == "turn_completed"
                    and item.turn_id == turn_id
                    and item.payload.get("status") == "aborted"
                    for item in self.events.replay(session_id)[0]
                ),
                timeout=2.5,
            )

        _wait_until(
            lambda: any(
                item.event_type == "status_changed"
                and (
                    item.payload.get("cancellationPending") is True
                    or item.payload.get("escalated") is True
                )
                for item in self.events.replay(session_id)[0]
            )
            or self.store.get(peer_session_id)["status"] == "faulted",
            timeout=2.5,
        )

        self.assertIsNone(
            self.runtime.runtime_status()["runtimeHostKillGate"][
                "lastKillReceipt"
            ]
        )
        self.assertTrue(client.running)
        self.assertEqual(self.runtime.runtime_status()["status"], "busy")
        self.assertEqual(self.store.get(peer_session_id)["status"], "busy")
        target_events = self.events.replay(session_id)[0]
        completed = [
            item
            for item in target_events
            if item.event_type == "turn_completed" and item.turn_id == turn_id
        ]
        self.assertEqual(
            completed[-1].payload["terminalEvent"],
            "abort_timeout_isolated",
        )
        isolated = [
            item
            for item in target_events
            if item.event_type == "status_changed"
            and item.payload.get("cancellationPending") is True
        ]
        self.assertFalse(isolated[-1].payload["hostHealthConfirmed"])
        self.assertTrue(isolated[-1].payload["sharedHostProtected"])

        self.runtime._handle_host_event({
            "protocolVersion": "2",
            "event": "agent.event",
            "sessionId": peer_session_id,
            "turnId": peer_turn_id,
            "payload": {"type": "agent_settled"},
        })
        self.assertEqual(self.store.get(peer_session_id)["status"], "idle")

    def test_abort_accepts_host_idle_receipt_after_turn_already_settled(self) -> None:
        session_id = str(self.first["id"])
        accepted = self.runtime.prompt(session_id, "host-settled-before-abort-ack")
        turn_id = str(accepted["turnId"])
        client = self.runtime._require_client()
        original_send = client.send

        def idle_abort_receipt(
            method: str,
            params: dict[str, object] | None = None,
            *,
            timeout: float | None = None,
        ) -> dict[str, object]:
            if method != "session.abort":
                return original_send(method, params, timeout=timeout)
            return {
                "schemaVersion": "rag-ime.pi-session-abort-receipt.v1",
                "sessionId": session_id,
                "turnId": "",
                "cancelledDecisionIds": [],
                "cancelledUIRequestIds": [],
                "lifecycle": {
                    "schemaVersion": "pi.agent-abort-receipt.v1",
                    "scopeId": session_id,
                    "generation": 2,
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

        with patch.object(client, "send", side_effect=idle_abort_receipt):
            receipt = self.runtime.abort(session_id)

        self.assertEqual(receipt["turnId"], turn_id)
        self.assertEqual(self.store.get(session_id)["status"], "idle")
        self.assertEqual(self.runtime._states[session_id].turn_id, "")
        completed = [
            item
            for item in self.events.replay(session_id)[0]
            if item.event_type == "turn_completed" and item.turn_id == turn_id
        ]
        self.assertEqual(completed[-1].payload["status"], "aborted")
        self.assertEqual(
            completed[-1].payload["terminalEvent"],
            "idle_abort_receipt",
        )
        time.sleep(1.1)
        self.assertIsNone(
            self.runtime.runtime_status()["runtimeHostKillGate"]["lastKillReceipt"]
        )

    def test_abort_settled_error_is_terminal_aborted_not_faulted(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        turn_id = f"turn-{session_id}"
        with self.runtime._lock:
            self.runtime._states[session_id].turn_id = turn_id
        self.store.set_status(session_id, "busy")

        self.runtime.abort(session_id)
        self.runtime._handle_host_event({
            "protocolVersion": "2",
            "event": "agent.event",
            "sessionId": session_id,
            "turnId": turn_id,
            "payload": {
                "type": "agent_end",
                "messages": [{
                    "role": "assistant",
                    "stopReason": "error",
                    "errorMessage": "This operation was aborted",
                    "content": [],
                }],
            },
        })
        self.runtime._handle_host_event({
            "protocolVersion": "2",
            "event": "agent.event",
            "sessionId": session_id,
            "turnId": turn_id,
            "payload": {"type": "agent_settled"},
        })

        self.assertEqual(self.store.get(session_id)["status"], "idle")
        events = self.events.replay(session_id)[0]
        self.assertFalse(any(item.event_type == "turn_failed" for item in events))
        completed = [item for item in events if item.event_type == "turn_completed"]
        self.assertEqual(completed[-1].payload["status"], "aborted")
        self.assertEqual(completed[-1].payload["terminalEvent"], "agent_settled")

    def test_abort_racing_provider_failure_remains_terminal_aborted(self) -> None:
        """A late Provider error must not overwrite an accepted user Stop."""

        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        turn_id = f"turn-{session_id}"
        with self.runtime._lock:
            self.runtime._states[session_id].turn_id = turn_id
        self.store.set_status(session_id, "busy")

        self.runtime.abort(session_id)
        self.runtime._turn_failed_once(
            session_id,
            turn_id,
            RuntimeError("fetch failed after user abort"),
        )

        events = self.events.replay(session_id)[0]
        self.assertFalse(
            any(
                item.event_type == "turn_failed" and item.turn_id == turn_id
                for item in events
            )
        )
        completed = [
            item
            for item in events
            if item.event_type == "turn_completed" and item.turn_id == turn_id
        ]
        self.assertEqual(completed[-1].payload["status"], "aborted")
        self.assertEqual(
            completed[-1].payload["terminalEvent"],
            "abort_failure_race",
        )
        self.assertEqual(self.store.get(session_id)["status"], "idle")

    def test_abort_publishes_stopping_state_before_waiting_for_host_ack(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        turn_id = f"turn-{session_id}"
        with self.runtime._lock:
            self.runtime._states[session_id].turn_id = turn_id
        client = self.runtime._client
        self.assertIsNotNone(client)
        assert client is not None
        original_send = client.send

        def observe_send(method, params, *, timeout=None):
            if method == "session.abort":
                statuses = [
                    event.payload.get("status")
                    for event in self.events.replay(session_id)[0]
                    if event.event_type == "status_changed"
                    and event.turn_id == turn_id
                ]
                self.assertEqual(statuses[-1], "aborting")
            return original_send(method, params, timeout=timeout)

        with patch.object(client, "send", side_effect=observe_send):
            self.runtime.abort(session_id)

    def test_cancelled_admission_ack_never_reopens_gateway_before_exact_abort(self) -> None:
        self._assert_cancelled_admission_ack_stays_fenced("")

    def test_cancelled_admission_ack_fence_failure_still_delivers_exact_abort(self) -> None:
        self._assert_cancelled_admission_ack_stays_fenced("client:pending", fail_fence=True)

    def _assert_cancelled_admission_ack_stays_fenced(self, client_id: str, *, fail_fence: bool = False) -> None:
        session_id = str(self.first["id"])
        turn_id = "turn:late-ack"
        self.runtime.ensure(session_id)
        client = self.runtime._require_client()
        original_send, original_abort = client.send, self.runtime.abort
        observed, targets = [], []

        def send(method, params=None, *, timeout=None, before_write=None):
            if method == "session.prompt":
                if before_write:
                    before_write()
                self.runtime.abort_with_approval_fence(session_id,
                    lambda identity: self.store.cancel_pending_approvals(session_id,
                        turn_id=identity["turnId"], client_message_id=identity["clientMessageId"]))
                return {"accepted": True, "turnId": turn_id}
            if method == "session.abort":
                targets.append(dict(params))
                raise PiRuntimeCommandRejected("target retired", host_error_code="ABORT_TARGET_MISMATCH")
            return original_send(method, params, timeout=timeout, before_write=before_write)

        def inspect_before_abort(sid, **kwargs):
            if "_expected_identity" in kwargs:
                observed.append(self.runtime.is_gateway_turn_active(sid, turn_id, client_message_id=client_id))
            return original_abort(sid, **kwargs)

        original_cancel = self.store.cancel_pending_approvals
        def cancel(sid, **kwargs):
            if fail_fence and kwargs.get("turn_id") == turn_id:
                raise OSError("isolated fence storage failure")
            return original_cancel(sid, **kwargs)

        original_set_status = self.store.set_status
        def set_status(sid, status, **kwargs):
            if fail_fence and self.runtime._states[sid].abort_requested_turn_id == turn_id:
                raise OSError("isolated status storage failure")
            return original_set_status(sid, status, **kwargs)

        original_publish = self.events.publish
        def publish(sid, *args, **kwargs):
            if fail_fence and self.runtime._states[sid].abort_requested_turn_id == turn_id:
                raise OSError("isolated event storage failure")
            return original_publish(sid, *args, **kwargs)

        with patch.object(client, "send", side_effect=send), \
             patch.object(self.runtime, "abort", side_effect=inspect_before_abort), \
             patch.object(self.store, "cancel_pending_approvals", side_effect=cancel), \
             patch.object(self.store, "set_status", side_effect=set_status), \
             patch.object(self.events, "publish", side_effect=publish), \
             patch.object(self.runtime, "_deliver_pending_admission_abort", return_value=None):
            if fail_fence:
                with self.assertRaises(PiRuntimeCommandAcceptanceUnknown):
                    self.runtime.prompt(session_id, "stop during admission", client_message_id=client_id)
            else:
                self.assertTrue(self.runtime.prompt(session_id, "stop during admission", client_message_id=client_id)["abortRequested"])
        self.assertEqual(observed, [False])
        self.assertEqual(targets, [{"sessionId": session_id, "expectedTurnId": turn_id, "clientMessageId": client_id}])
        self.assertFalse(self.runtime.is_gateway_turn_active(session_id, turn_id, client_message_id=client_id))
        if not fail_fence:
            with self.store._read_connect() as conn:
                self.assertIsNotNone(conn.execute("SELECT 1 FROM agent_gateway_cancelled_turns WHERE session_id = ? AND turn_id = ?",
                    (session_id, turn_id)).fetchone())

    def test_abort_during_prompt_admission_reaches_host_before_prompt_ack(
        self,
    ) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        client = self.runtime._require_client()
        original_send = client.send
        prompt_entered = threading.Event()
        release_prompt = threading.Event()
        abort_delivered = threading.Event()
        abort_settled = threading.Event()
        turn_id = f"turn-admission-{session_id}"

        def delayed_send(
            method: str,
            params: dict[str, object] | None = None,
            *,
            timeout: float | None = None,
            before_write=None,
        ) -> dict[str, object]:
            if method == "session.prompt":
                if before_write is not None:
                    before_write()
                prompt_entered.set()
                self.assertTrue(release_prompt.wait(2.0))
                return {"accepted": True, "turnId": turn_id}
            if method == "session.abort":
                abort_delivered.set()
                self.runtime._handle_host_event({
                    "protocolVersion": "2",
                    "event": "agent.event",
                    "sessionId": session_id,
                    "turnId": turn_id,
                    "payload": {"type": "agent_settled"},
                })
                abort_settled.set()
                return {
                    "schemaVersion": "rag-ime.pi-session-abort-receipt.v1",
                    "sessionId": session_id,
                    "turnId": turn_id,
                    "cancelledDecisionIds": [],
                    "cancelledUIRequestIds": [],
                    "lifecycle": {
                        "schemaVersion": "pi.agent-abort-receipt.v1",
                        "scopeId": session_id,
                        "generation": 1,
                        "reason": "user_abort",
                        "cancelledContinuationIds": [],
                        "cancelledOperationIds": ["provider"],
                        "failedOperationIds": [],
                        "operations": [],
                        "pendingOperations": [],
                        "drained": True,
                        "idle": True,
                    },
                }
            return original_send(method, params, timeout=timeout)

        prompt_result: dict[str, object] = {}
        prompt_error: list[BaseException] = []

        def run_prompt() -> None:
            try:
                prompt_result.update(
                    self.runtime.prompt(
                        session_id,
                        "stop while admission is pending",
                        client_message_id="client:admission-race",
                    )
                )
            except BaseException as exc:  # pragma: no cover - assertion below
                prompt_error.append(exc)

        with patch.object(client, "send", side_effect=delayed_send):
            prompt_thread = threading.Thread(target=run_prompt)
            prompt_thread.start()
            try:
                self.assertTrue(prompt_entered.wait(1.0))
                early_receipt = self.runtime.abort(session_id)
                self.assertTrue(early_receipt["pendingAdmission"])
                self.assertTrue(abort_delivered.wait(1.0))
                self.assertTrue(abort_settled.wait(1.0))
                # Cancellation reaches and settles at the Host while the
                # original prompt ACK is still held by the explicit barrier.
                self.assertFalse(release_prompt.is_set())
                self.assertTrue(prompt_thread.is_alive())
                self.assertEqual(prompt_result, {})
                self.assertEqual(self.store.get(session_id)["status"], "idle")
            finally:
                release_prompt.set()
                prompt_thread.join(timeout=2.0)

        self.assertFalse(prompt_thread.is_alive())
        self.assertEqual(prompt_error, [])
        self.assertTrue(abort_delivered.is_set())
        self.assertTrue(prompt_result["abortRequested"])
        self.assertEqual(self.store.get(session_id)["status"], "idle")
        completed = [
            event
            for event in self.events.replay(session_id)[0]
            if event.event_type == "turn_completed"
            and event.turn_id == turn_id
        ]
        self.assertEqual(completed[-1].payload["status"], "aborted")

    def test_reserved_prompt_admission_stops_before_host_dispatch(
        self,
    ) -> None:
        session_id = str(self.first["id"])
        client_message_id = "client:application-admission-race"

        reserved = self.runtime.reserve_prompt_admission(
            session_id,
            client_message_id=client_message_id,
        )
        self.assertTrue(reserved["reserved"])
        early_receipt = self.runtime.abort(session_id)
        self.assertTrue(early_receipt["pendingAdmission"])
        self.assertTrue(early_receipt["admissionCancelled"])
        self.assertTrue(early_receipt["lifecycle"]["drained"])
        self.assertTrue(early_receipt["lifecycle"]["idle"])
        self.assertEqual(
            early_receipt["lifecycle"]["pendingOperations"],
            [],
        )
        self.assertEqual(self.store.get(session_id)["status"], "idle")
        admission_statuses = [
            event.payload.get("status")
            for event in self.events.replay(session_id)[0]
            if event.event_type == "status_changed"
        ]
        self.assertEqual(admission_statuses[-1], "aborting")
        with self.assertRaises(PiRuntimeCommandRejected) as cancelled:
            self.runtime.prompt(
                session_id,
                "stop before context preparation finishes",
                client_message_id=client_message_id,
            )
        self.assertEqual(
            cancelled.exception.host_error_code,
            "PROMPT_ADMISSION_CANCELLED",
        )
        self.assertEqual(self.store.get(session_id)["status"], "idle")
        self.assertEqual(self.runtime.runtime_status()["activeSessionIds"], [])

    def test_open_idle_session_can_list_fork_candidates(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        self.assertEqual(self.store.get(session_id)["status"], "idle")

        self.assertEqual(self.runtime.fork_candidates(session_id), [])
        self.assertEqual(self.store.get(session_id)["status"], "idle")

    def test_turn_failure_uses_supported_faulted_status_and_publishes_event(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)

        self.runtime._turn_failed(session_id, "turn-provider-error", RuntimeError("provider failed"))

        self.assertEqual(self.store.get(session_id)["status"], "faulted")
        failed = [item for item in self.events.replay(session_id)[0] if item.event_type == "turn_failed"]
        self.assertEqual(failed[-1].turn_id, "turn-provider-error")
        self.assertEqual(failed[-1].payload["error"], "provider failed")
        self.assertEqual(
            failed[-1].payload["reasonCode"],
            "provider_failure_unclassified",
        )
        self.assertFalse(failed[-1].payload["retryable"])
        self.assertFalse(failed[-1].payload["hadToolActivity"])

    def test_transport_failure_without_tool_activity_is_retryable(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)

        self.runtime._turn_failed(
            session_id,
            "turn-transport-error",
            RuntimeError("fetch failed"),
        )

        failed = [
            item
            for item in self.events.replay(session_id)[0]
            if item.event_type == "turn_failed"
        ][-1]
        self.assertEqual(
            failed.payload["failureKind"],
            "transient_provider_failure",
        )
        self.assertEqual(
            failed.payload["reasonCode"],
            "provider_transport_failure",
        )
        self.assertTrue(failed.payload["retryable"])
        self.assertFalse(failed.payload["hadToolActivity"])

    def test_websocket_failure_is_classified_as_transport_interruption(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)

        self.runtime._turn_failed(
            session_id,
            "turn-websocket-error",
            RuntimeError("WebSocket error"),
        )

        failed = [
            item
            for item in self.events.replay(session_id)[0]
            if item.event_type == "turn_failed"
        ][-1]
        self.assertEqual(
            failed.payload["reasonCode"],
            "provider_transport_failure",
        )
        self.assertTrue(failed.payload["retryable"])

    def test_transport_failure_after_tool_activity_is_not_retryable(
        self,
    ) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        with self.runtime._lock:
            self.runtime._states[session_id].had_tool_activity = True

        self.runtime._turn_failed(
            session_id,
            "turn-tool-transport-error",
            RuntimeError("fetch failed"),
        )

        failed = [
            item
            for item in self.events.replay(session_id)[0]
            if item.event_type == "turn_failed"
        ][-1]
        self.assertEqual(
            failed.payload["reasonCode"],
            "tool_activity_observed",
        )
        self.assertFalse(failed.payload["retryable"])
        self.assertTrue(failed.payload["hadToolActivity"])

    def test_host_exit_is_classified_separately_from_provider_failure(self) -> None:
        session_id = str(self.first["id"])
        self.runtime.ensure(session_id)
        client = self.runtime._client
        self.assertIsNotNone(client)
        with self.runtime._lock:
            self.runtime._states[session_id].turn_id = (
                "turn-runtime-host-exit"
            )

        try:
            self.runtime._handle_host_exit(-9, "")

            failed = [
                item
                for item in self.events.replay(session_id)[0]
                if item.event_type == "turn_failed"
            ]
            self.assertEqual(
                failed[-1].payload["failureKind"],
                "runtime_host_exit",
            )
            self.assertEqual(failed[-1].payload["exitCode"], -9)
            self.assertEqual(
                failed[-1].payload["error"],
                "Pi Runtime Host exited with code -9",
            )
        finally:
            assert client is not None
            client.stop()

    def _record_event(self, event) -> None:
        self.store.record_runtime_event(
            event_id=event.event_id,
            session_id=event.session_id,
            turn_id=event.turn_id,
            sequence=event.sequence,
            event_type=event.event_type,
            created_at_ms=event.created_at_ms,
            redacted_summary=str(event.payload.get("status") or event.payload.get("error") or ""),
        )


def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before timeout")


if __name__ == "__main__":
    unittest.main()
