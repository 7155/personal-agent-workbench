from __future__ import annotations

import copy
import tempfile
import unittest
import io
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from rag_ime.agent_configuration import default_agent_configuration
from rag_ime.agent_capability_catalog import build_capability_catalog, capability_disclosure_enabled
from rag_ime.agent_events import AgentEventHub
from rag_ime.agent_gateway_requests import GatewayRequestStore
from rag_ime.agent_message_snapshot import AgentMessageSnapshotService
from rag_ime.control_api import ControlAccessContext, ControlApiError, ControlRequest, default_route_policy
from rag_ime.agent_session_application import AgentSessionApplicationService
from rag_ime.agent_role_application import AgentRoleApplicationService
from rag_ime.agent_session_policy import AgentSessionPolicyService
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.agent_routes import agent_session_route
from rag_ime.agent_service import AgentService
from rag_ime.debug_server import DebugRequestHandler
from rag_ime.agent_tools import ControlToolGateway
from rag_ime.pi.config import PiRuntimeConfig
from rag_ime.pi.runtime import PiRuntimeHostManager, _HostedSessionState
from rag_ime.pi.values import PiRuntimeError, PiRuntimeCommandAcceptanceUnknown
from tests.sqlite_fixtures import copy_current_database


ENGINE_CAPABILITIES = {"gatewayTools": True, "compaction": True, "resume": True, "exactAbort": True,
    "nativeMcp": False, "codemode": False, "managedPlugins": False, "conversationFork": False,
    "conversationRewrite": False, "commandCatalog": False, "images": False}
PUBLIC_MODEL = {"provider": "test", "id": "model", "name": "Offline model", "api": "test",
    "reasoning": False, "thinkingLevels": ["off"], "supportsImages": True, "contextWindow": 1000, "maxTokens": 100}


class DurableHostDouble:
    """Only the native wire is doubled; Store, Manager and projection are real."""

    running = True

    def __init__(self):
        self.calls = []
        self.snapshot = None
        self.next_error = None
        self.settlement = None
        self.initial_active = True

    def send(self, method, params=None, *, before_write=None, **kwargs):
        if before_write is not None:
            before_write()
        self.calls.append((method, copy.deepcopy(params or {})))
        if self.next_error is not None:
            error, self.next_error = self.next_error, None
            raise error
        if method == "session.open":
            self.snapshot = self.snapshot or {
                "sessionId": params["sessionId"], "piSessionId": "native-conversation",
                "runtimeEngine": "durable", "durableStoreRef": params["durableStoreRef"],
                "durableConversationId": "1", "paused": True,
                "projectionCurrent": True,
                "engineCapabilities": dict(ENGINE_CAPABILITIES),
                "isIdle": False, "recoverable": True,
                "activeTurn": {"turnId": "original-input", "clientMessageId": "original-client"},
                "messages": [{"id": "user-original", "role": "user", "content": "原任务",
                              "_ragImeTurnId": "original-input", "clientMessageId": "original-client"}],
                "model": {"provider": "test", "id": "model"},
            }
            if not self.initial_active:
                self.snapshot.update(isIdle=True, recoverable=False, activeTurn=None, messages=[])
            return {"snapshot": copy.deepcopy(self.snapshot)}
        if method in {"session.control_state", "session.snapshot"}:
            value = copy.deepcopy(self.snapshot)
            if method == "session.control_state":
                value["schemaVersion"] = "rag-ime.pi-session-control-state.v1"
            return value
        if method == "session.resume":
            self.snapshot["recoverable"] = False
            self.snapshot["paused"] = False
            state = {**copy.deepcopy(self.snapshot), "schemaVersion": "rag-ime.pi-session-control-state.v1"}
            return {"schemaVersion": "rag-ime.pi-session-resume.v1", "accepted": True,
                    "runtimeEngine": "durable", "sessionId": params["sessionId"], "turnId": params["turnId"],
                    "clientMessageId": params["clientMessageId"], "resumed": True, "state": state}
        if method == "tools.sync":
            return {"tools": params["tools"]}
        if method == "session.prompt":
            return {"accepted": True, "turnId": "new-input", "piEntryId": "new-entry",
                    "clientMessageId": params["clientMessageId"]}
        if method in {"session.steer", "session.follow_up"}:
            return {"accepted": True, "queued": True, "turnId": "queued-input",
                    "clientMessageId": params["clientMessageId"], "parentTurnId": "original-input"}
        if method == "session.abort":
            return {"schemaVersion": "rag-ime.pi-session-abort-receipt.v1",
                    "sessionId": params["sessionId"], "turnId": params.get("expectedTurnId", ""),
                    "lifecycle": {"schemaVersion": "pi.agent-abort-receipt.v1",
                                  "drained": False, "idle": False, "pendingOperations": ["native-run"]}}
        if method == "session.settlement.get":
            return {"settlement": self.settlement}
        if method == "session.close":
            return {"closed": True}
        if method == "health":
            return {"ok": True}
        return {}

    def stop(self):
        self.running = False


class PiDurableRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-durable-adapter-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        copy_current_database(self.root / "product.sqlite")
        self.store = AgentSessionStore(self.root / "product.sqlite")
        self.addCleanup(self.store.close)
        self.store.initialize()
        self.session = self.store.create(title="新的持续Session", runtime_engine="durable")
        self.classic = self.store.create(title="原Session")
        executable = self.root / "host"
        executable.touch()
        self.events = AgentEventHub()
        self.runtime = PiRuntimeHostManager(config=PiRuntimeConfig(
            enabled=True, executable=executable, agent_dir=self.root / "agent",
            session_dir=self.root / "sessions", logs_dir=self.root / "logs",
            idle_timeout_seconds=0, provider="test", model="model", protocol_version="2"),
            sessions=self.store, events=self.events, tool_manifest_provider=lambda _: [])
        self.addCleanup(self.runtime.stop)
        self.host = DurableHostDouble()
        self.runtime._client = self.host
        self.runtime._host = Mock(return_value=self.host)
        self.runtime._host_capabilities = {"sessionControlState": True, "sessionBoundAbort": True,
            "sessionEngines": {"classic": {"available": True},
                               "durable": {"available": True, "experimental": True, "version": "1"}}}
        self.session_id = self.session["id"]

    def open(self):
        return self.runtime.ensure(self.session_id)

    def test_open_uses_isolated_store_and_preserves_recoverable_original_input(self):
        self.open()
        request = self.host.calls[0]
        self.assertEqual(request[0], "session.open")
        self.assertEqual(request[1]["runtimeEngine"], "durable")
        self.assertNotIn("sessionFile", request[1])
        self.assertNotIn("codemodeMode", request[1])
        self.assertFalse(request[1].get("nativeMcpExecutionAllowed", False))
        self.assertTrue(Path(request[1]["durableStoreRef"]).is_relative_to((self.runtime.session_root / "durable").resolve()))
        binding = self.store.runtime_binding(self.session_id)
        self.assertEqual(binding["runtimeKind"], "pi_durable")
        self.assertEqual(self.runtime._states[self.session_id].turn_id, "original-input")
        self.assertFalse(any(method == "session.abort" for method, _ in self.host.calls))

    def test_missing_native_engine_capability_rejects_before_open_or_binding_change(self):
        self.runtime._host_capabilities = {}
        with self.assertRaisesRegex(PiRuntimeError, "Durable"):
            self.open()
        self.assertEqual(self.host.calls, [])
        self.assertEqual(self.store.runtime_binding(self.session_id)["state"], "prepared")

    def test_repeated_passive_open_never_resumes_or_retires_input(self):
        self.open()
        self.open()
        self.assertEqual([method for method, _ in self.host.calls], ["session.open", "session.control_state"])
        self.assertEqual(self.store.get(self.session_id)["status"], "busy")

    def test_durable_open_eviction_reopens_idle_classic_peer_without_retiring_paused_peer(self):
        self.runtime._host_capabilities["nativeMcpExecutionPolicy"] = True
        classic_id = self.classic["id"]
        paused_peer = self.store.create(title="Paused peer", runtime_engine="durable")["id"]
        abort_timer, settle_timer = Mock(), Mock()
        self.runtime._open_sessions.update((classic_id, paused_peer))
        self.runtime._states[classic_id] = _HostedSessionState(
            abort_timer=abort_timer, settle_timer=settle_timer)
        paused_state = _HostedSessionState(runtime_engine="durable", recoverable=True,
            turn_id="peer-original", client_message_id="peer-client")
        self.runtime._states[paused_peer] = paused_state
        send = self.host.send

        def pooled_send(method, params=None, **kwargs):
            if params.get("sessionId") == classic_id:
                self.assertEqual(method, "session.open", "evicted peer must reopen, not query lost resident state")
                self.host.calls.append((method, copy.deepcopy(params)))
                return {"snapshot": {"sessionId": classic_id, "piSessionId": "classic-native",
                    "sessionFile": str(self.root / "sessions" / "classic.jsonl"),
                    "isIdle": True, "messages": [], "leafId": ""}}
            result = send(method, params, **kwargs)
            if method == "session.open":
                result["evictedSessionId"] = classic_id
            return result

        self.host.send = pooled_send
        opened = self.open()
        self.assertNotIn(classic_id, self.runtime._open_sessions)
        self.assertNotIn(classic_id, self.runtime._states)
        self.assertEqual(opened["evictedSessionId"], classic_id)
        abort_timer.cancel.assert_called_once_with()
        settle_timer.cancel.assert_called_once_with()
        self.assertIs(self.runtime._states[paused_peer], paused_state)
        self.assertIn(paused_peer, self.runtime._open_sessions)
        reopened = self.runtime.ensure(classic_id)
        self.assertFalse(reopened["reused"])
        self.assertIn(classic_id, self.runtime._open_sessions)
        self.assertFalse(any(method == "session.abort" for method, _ in self.host.calls))

    def test_snapshot_and_recent_never_read_classic_jsonl_or_tail_cache(self):
        with patch.object(self.runtime, "_durable_history_snapshot", side_effect=AssertionError("JSONL read")), \
                patch.object(self.runtime, "_recent_projection_identity", side_effect=AssertionError("file-tail cache")):
            full = self.runtime.session_snapshot(self.session_id)
            recent = self.runtime.recent_session_snapshot(self.session_id)
        self.assertEqual(full["messages"][0]["turnId"], "original-input")
        self.assertEqual(recent["messages"][0]["turnId"], "original-input")
        self.assertFalse(any(method == "session.resume" for method, _ in self.host.calls))

    def test_native_history_failure_does_not_fall_back_to_jsonl_or_empty_success(self):
        self.open()
        self.host.next_error = PiRuntimeError("native storage unavailable")
        with patch.object(self.runtime, "_durable_history_snapshot", side_effect=AssertionError("JSONL fallback")):
            with self.assertRaisesRegex(PiRuntimeError, "native storage unavailable"):
                self.runtime.session_snapshot(self.session_id)

    def test_cold_recent_status_does_not_infer_idle_from_absent_memory_state(self):
        self.store.set_status(self.session_id, "busy")
        projection = object.__new__(AgentMessageSnapshotService)
        projection.sessions = self.store
        projection._runtime_provider = lambda: self.runtime
        result = projection._reconcile_session_status(self.session_id, self.store.get(self.session_id))
        self.assertEqual(result["status"], "busy")
        self.assertEqual(self.host.calls, [])

    def test_explicit_resume_uses_original_identity_without_new_prompt(self):
        self.open()
        result = self.runtime.resume_session(self.session_id, turn_id="original-input", client_message_id="original-client")
        self.assertTrue(result["resumed"])
        request = next(params for method, params in self.host.calls if method == "session.resume")
        self.assertEqual(request, {"sessionId": self.session_id, "turnId": "original-input", "clientMessageId": "original-client"})
        self.assertFalse(any(method == "session.prompt" for method, _ in self.host.calls))

    def test_resume_rejects_a_successor_instead_of_rebinding_old_input(self):
        self.open()
        self.host.snapshot["activeTurn"] = {"turnId": "successor", "clientMessageId": "new-client"}
        with self.assertRaises(PiRuntimeError):
            self.runtime.resume_session(self.session_id, turn_id="original-input", client_message_id="original-client")
        self.assertFalse(any(method == "session.resume" for method, _ in self.host.calls))

    def test_host_exit_preserves_recoverable_durable_but_faults_classic(self):
        self.open()
        self.runtime._states[self.classic["id"]] = _HostedSessionState(turn_id="classic-turn")
        self.runtime._handle_host_exit(1, "interrupted", source_client=self.host)
        self.assertEqual(self.store.get(self.session_id)["status"], "busy")
        self.assertEqual(self.runtime._states[self.session_id].turn_id, "original-input")
        self.assertEqual(self.store.get(self.classic["id"])["status"], "faulted")
        self.assertFalse(any(event.event_type == "turn_failed" for event in self.events.replay(self.session_id)[0]))

    def test_explicit_manager_stop_does_not_forge_idle_for_recoverable_input(self):
        self.open()
        self.runtime.stop()
        self.assertEqual(self.store.get(self.session_id)["status"], "busy")

    def test_idle_settlement_probe_cannot_infer_a_durable_terminal_outcome(self):
        self.open()
        self.host.snapshot.update(isIdle=True, activeTurn=None)
        self.runtime._settle_fallback_probe(self.session_id, "original-input")
        self.assertEqual(self.runtime._states[self.session_id].turn_id, "original-input")
        self.assertFalse(any(event.event_type == "turn_completed" for event in self.events.replay(self.session_id)[0]))

    def test_unsupported_controls_refuse_without_opening_native_session(self):
        operations = [lambda: self.runtime.set_codemode_mode(self.session_id, mode="on"),
                      lambda: self.runtime.fork_candidates(self.session_id),
                      lambda: self.runtime.rewind_session(self.session_id, entry_id="old-entry"),
                      lambda: self.runtime.invoke_command(self.session_id, "/skill:test")]
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(PiRuntimeError, "Durable"):
                    operation()
        self.assertEqual(self.host.calls, [])

    def test_images_reject_before_prompt_admission_or_rpc(self):
        with self.assertRaisesRegex(PiRuntimeError, "Durable"):
            self.runtime.prompt(self.session_id, "看图", client_message_id="image", images=[{"data": "dummy"}])
        self.assertEqual(self.host.calls, [])

    def test_resume_unknown_stays_bound_to_original_input(self):
        self.open()
        original_send = self.host.send
        def send(method, *args, **kwargs):
            if method == "session.resume":
                raise PiRuntimeCommandAcceptanceUnknown("lost resume ACK")
            return original_send(method, *args, **kwargs)
        self.host.send = send
        with self.assertRaises(PiRuntimeCommandAcceptanceUnknown):
            self.runtime.resume_session(self.session_id, turn_id="original-input", client_message_id="original-client")
        self.assertEqual(self.runtime._states[self.session_id].turn_id, "original-input")

    def application(self):
        app = object.__new__(AgentSessionApplicationService)
        app.sessions = self.store
        app._runtime_provider = lambda: self.runtime
        app.configuration_store = Mock()
        app.configuration_store.snapshot.return_value = {"configuration": default_agent_configuration()}
        app.pending_memory_bootstrap = lambda _: {}
        return app

    def test_application_opt_in_is_explicit_and_old_create_stays_classic(self):
        app = self.application()
        self.assertEqual(app.create_session({"title": "old"})["session"]["runtimeEngine"], "classic")
        result = app.create_session({"title": "new", "runtimeEngine": "durable"})
        self.assertEqual(result["session"]["runtimeEngine"], "durable")
        self.assertFalse(result["session"]["piSkillsEnabled"])
        self.assertFalse(result["session"]["codexSkillsEnabled"])

    def test_application_unsupported_engine_creates_no_new_session(self):
        app = self.application()
        before = len(self.store.list())
        self.runtime._host_capabilities = {}
        with self.assertRaisesRegex(PiRuntimeError, "Durable"):
            app.create_session({"title": "unsupported", "runtimeEngine": "durable"})
        self.assertEqual(len(self.store.list()), before)

    def test_role_catalog_reuses_negotiated_capabilities_without_an_extra_host_request(self):
        app = object.__new__(AgentRoleApplicationService)
        app._runtime_provider = lambda: self.runtime
        app._default_model_profile_provider = lambda: "test/model"
        self.runtime.available_models = Mock(return_value=[{"provider": "test", "id": "model"}])
        result = app.model_catalog()
        self.assertTrue(result["sessionEngines"]["durable"]["available"])
        self.assertEqual(self.host.calls, [])
        self.runtime._client = None
        self.assertFalse(app.model_catalog()["sessionEngines"]["durable"]["available"])

    def test_session_model_catalog_exposes_the_native_engine_capabilities(self):
        self.runtime.available_models = Mock(return_value=[dict(PUBLIC_MODEL)])
        policy = object.__new__(AgentSessionPolicyService)
        policy.sessions = self.store
        policy._runtime_provider = lambda: self.runtime
        result = policy.model_catalog(self.session_id)
        self.assertEqual(result["runtimeEngine"], "durable")
        self.assertEqual(result["engineCapabilities"], ENGINE_CAPABILITIES)
        self.assertFalse(any(method == "session.resume" for method, _ in self.host.calls))

    def test_application_open_is_passive_and_explicit_resume_keeps_original_input(self):
        app = self.application()
        app.runtime_status = self.runtime.runtime_status
        app.probe_memory_maintenance = Mock(side_effect=AssertionError("background classic maintenance"))
        result = app.ensure_runtime({"sessionId": self.session_id})
        self.assertTrue(result["state"]["recoverable"])
        self.assertFalse(any(method == "session.resume" for method, _ in self.host.calls))
        result = app.resume_session(self.session_id, {"turnId": "original-input", "clientMessageId": "original-client"})
        self.assertTrue(result["runtimeReceipt"]["resumed"])
        self.assertEqual(sum(method == "session.resume" for method, _ in self.host.calls), 1)
        self.assertFalse(any(method == "session.prompt" for method, _ in self.host.calls))

    def test_recent_uses_native_bounded_view_and_keeps_partial_cursor(self):
        self.open()
        self.host.snapshot.update(partial=True, historyCursor="older-page", projectionCurrent=True)
        result = self.runtime.recent_session_snapshot(self.session_id)
        self.assertEqual(self.host.calls[-1], ("session.snapshot", {"sessionId": self.session_id, "view": "recent"}))
        self.assertTrue(result["partial"])
        self.assertEqual(result["historyCursor"], "older-page")

    def terminal_receipt(self):
        return {"schemaVersion": "rag-ime.pi-turn-settlement.v1", "sessionId": self.session_id,
            "turnId": "original-input", "clientMessageId": "original-client", "runtimeSessionId": "native-conversation",
            "receipt": {"schemaVersion": "pi.agent-settled.v2", "sessionId": "native-conversation",
                "runId": "original-input", "scopeId": "native-conversation:original-input", "receiptId": "committed-outcome",
                "disposition": "completed", "aborted": False, "pendingOperations": 0, "operations": {"pending": 0},
                "finalMessage": {"role": "assistant", "id": "final-message", "content": [{"type": "text", "text": "原生已完成"}]}}}

    def test_recovery_awaits_original_persisted_settlement_without_reprompt_or_idle_retirement(self):
        self.host.settlement = self.terminal_receipt()
        receipt = self.runtime.await_turn_settled(self.session_id, "original-input", client_message_id="original-client", timeout_seconds=2)
        self.assertEqual(receipt["receipt"]["receiptId"], "committed-outcome")
        self.assertEqual(self.store.get(self.session_id)["status"], "idle")
        self.assertFalse(any(method in {"session.prompt", "session.resume", "session.abort"} for method, _ in self.host.calls))
        self.assertEqual(sum(event.event_type == "turn_completed" for event in self.events.replay(self.session_id)[0]), 1)

    def test_incomplete_settlement_cannot_clear_original_input(self):
        self.open()
        invalid = self.terminal_receipt()
        invalid["receipt"]["pendingOperations"] = 1
        invalid["receipt"]["operations"]["pending"] = 1
        self.host.settlement = invalid
        with self.assertRaisesRegex(PiRuntimeError, "pending operations"):
            self.runtime.await_turn_settled(self.session_id, "original-input", client_message_id="original-client", timeout_seconds=2)
        self.assertEqual(self.runtime._states[self.session_id].turn_id, "original-input")
        self.assertEqual(self.store.get(self.session_id)["status"], "busy")

    def test_delayed_original_terminal_receipt_does_not_retire_successor(self):
        self.open()
        state = self.runtime._states[self.session_id]
        state.turn_id, state.client_message_id = "successor", "successor-client"
        self.host.settlement = self.terminal_receipt()
        receipt = self.runtime.await_turn_settled(self.session_id, "original-input", client_message_id="original-client", timeout_seconds=2)
        self.assertEqual(receipt["turnId"], "original-input")
        self.assertEqual(state.turn_id, "successor")
        self.assertEqual(self.store.get(self.session_id)["status"], "busy")

    def test_pending_abort_is_exact_and_does_not_infer_drain_or_kill_shared_host(self):
        self.open()
        result = self.runtime.abort(self.session_id)
        self.assertFalse(result["lifecycle"]["drained"])
        self.assertIn(("session.abort", {"sessionId": self.session_id, "expectedTurnId": "original-input",
            "clientMessageId": "original-client"}), self.host.calls)
        self.runtime._abort_fallback_expired(self.session_id, "original-input")
        self.assertTrue(self.host.running)
        self.assertEqual(self.runtime._states[self.session_id].turn_id, "original-input")
        self.assertFalse(any(event.event_type == "turn_completed" for event in self.events.replay(self.session_id)[0]))

    def test_stop_before_native_prompt_write_preserves_existing_admission_fence(self):
        self.open()
        state = self.runtime._states[self.session_id]
        state.turn_id = ""
        state.client_message_id = ""
        state.recoverable = False
        self.host.snapshot.update(isIdle=True, activeTurn=None, recoverable=False, paused=False)
        self.runtime.reserve_prompt_admission(self.session_id, client_message_id="new-command")
        self.runtime.abort(self.session_id)
        with self.assertRaises(PiRuntimeError):
            self.runtime.prompt(self.session_id, "新请求", client_message_id="new-command")
        self.assertFalse(any(method == "session.prompt" for method, _ in self.host.calls))

    def test_unfinished_paused_input_cannot_authorize_gateway_execution(self):
        self.open()
        self.assertFalse(self.runtime.is_turn_active(self.session_id, "original-input", client_message_id="original-client"))
        self.assertFalse(self.runtime.is_gateway_turn_active(self.session_id, "original-input", client_message_id="original-client"))

    def test_application_rejects_internal_surface_and_explicit_unsupported_skills(self):
        app = self.application()
        before = len(self.store.list())
        for extra in ({"_modelRoute": "roomCoordinator"}, {"piSkillsEnabled": True}, {"runtimeEngine": ["durable"]}):
            with self.subTest(extra=extra):
                with self.assertRaises(ValueError):
                    app.create_session({"title": "拒绝", "runtimeEngine": "durable", **extra})
        self.assertEqual(len(self.store.list()), before)

    def test_follow_up_admits_distinct_input_without_stealing_current_gateway_owner(self):
        self.open()
        self.runtime._states[self.session_id].recoverable = False
        result = self.runtime.prompt(self.session_id, "之后继续", client_message_id="queued-client", delivery="followUp")
        self.assertEqual(result["turnId"], "queued-input")
        self.assertEqual(result["clientMessageId"], "queued-client")
        self.assertEqual(self.runtime._states[self.session_id].turn_id, "original-input")
        self.assertEqual(self.runtime._states[self.session_id].client_message_id, "original-client")

    def test_host_cannot_swap_storage_or_conversation_identity_on_snapshot(self):
        self.open()
        self.host.snapshot["durableConversationId"] = "other-conversation"
        with self.assertRaisesRegex(PiRuntimeError, "another storage binding"):
            self.runtime.session_snapshot(self.session_id)

    def test_host_capability_mismatch_does_not_activate_prepared_binding(self):
        original_send = self.host.send
        def send(method, *args, **kwargs):
            result = original_send(method, *args, **kwargs)
            if method == "session.open":
                result["snapshot"]["engineCapabilities"]["codemode"] = True
            return result
        self.host.send = send
        with self.assertRaisesRegex(PiRuntimeError, "incompatible Durable engine capabilities"):
            self.open()
        self.assertEqual(self.store.runtime_binding(self.session_id)["state"], "prepared")

    def test_durable_directory_symlink_is_rejected_before_host_open(self):
        self.runtime.session_root.mkdir()
        (self.root / "outside").mkdir()
        (self.runtime.session_root / "durable").symlink_to(self.root / "outside", target_is_directory=True)
        with self.assertRaisesRegex(PiRuntimeError, "symlink"):
            self.open()
        self.assertEqual(self.host.calls, [])

    def test_governance_catalog_never_advertises_native_mcp_or_codemode(self):
        result = self.runtime.native_capabilities(self.session_id)
        self.assertFalse(result["mcp"]["available"])
        self.assertIsNone(result["codemodeMode"])
        self.assertEqual(self.runtime.command_catalog(self.session_id), [])
        self.assertEqual(self.runtime.skill_catalog(self.session_id), [])
        self.assertEqual(self.host.calls, [])

    def test_capability_catalog_hides_native_skills_and_plugins_but_keeps_gateway_tools(self):
        skills = Mock()
        skills.governance_catalog.return_value = [{"skillId": "native-test", "name": "Native Test"}]
        extensions = Mock()
        extensions.catalog.return_value = {"items": [{"id": "native-plugin", "enabled": True}]}
        result = build_capability_catalog(tool_manifests=[], session=self.session,
            configuration_store=None, governed_skills=skills, extensions=extensions)
        for item in result["items"]:
            if item["kind"] == "tool":
                continue
            self.assertEqual(item["authorization"]["state"], "denied")
            self.assertEqual(item["authorization"]["reason"], "durable_engine_unsupported")
            self.assertEqual(item["disclosure"]["effective"], "disabled")
        self.assertFalse(capability_disclosure_enabled("skill:native-test", session=self.session, configuration_store=None))
        self.assertTrue(capability_disclosure_enabled("tool:browser", session=self.session, configuration_store=None))
        self.assertTrue(capability_disclosure_enabled("extension:native-plugin", session=self.classic, configuration_store=None))

    def test_durable_plugin_direct_and_approved_paths_cannot_mutate_global_packages(self):
        gateway = object.__new__(ControlToolGateway)
        gateway.sessions = self.store
        gateway.extensions = Mock()
        with self.assertRaisesRegex(ValueError, "Durable"):
            gateway._plugins("propose_install", {"_sessionId": self.session_id, "packageSource": "dummy"})
        with self.assertRaisesRegex(ValueError, "Durable"):
            gateway._apply_approved_operation({"toolId": "plugins", "operation": "apply", "sessionId": self.session_id})
        gateway.extensions.assert_not_called()
        gateway.extensions.preview.assert_not_called()
        gateway.extensions.apply.assert_not_called()
        gateway.extensions.list.return_value = {"items": []}
        self.assertEqual(gateway._plugins("list", {"_sessionId": self.classic["id"]}), {"items": []})

    def test_durable_public_plugin_admission_rejects_before_native_effect_or_approval(self):
        gateway = object.__new__(ControlToolGateway)
        gateway.sessions = self.store
        gateway._require_request_owner = Mock()
        gateway._session_with_scenario_context = lambda session: session
        gateway.extensions = Mock()
        gateway.gateway_requests = GatewayRequestStore(self.store)
        request = {"schemaVersion": "rag-ime.agent-tool-call.v1", "toolCallId": "plugin-attempt",
            "sessionId": self.session_id, "tool": "plugins", "args": {"op": "propose_install", "packageSource": "dummy"}}
        with self.assertRaisesRegex(ValueError, "Durable"):
            gateway.execute(request)
        self.assertEqual(self.store.pending_approval_ids(self.session_id), ())
        gateway.extensions.preview.assert_not_called()

    def test_cold_stop_observes_original_recoverable_input_before_cancellation(self):
        self.store.set_status(self.session_id, "busy")
        result = self.runtime.abort(self.session_id)
        self.assertEqual(result["turnId"], "original-input")
        self.assertFalse(result["lifecycle"]["drained"])
        self.assertEqual([method for method, _ in self.host.calls], ["session.open", "session.abort"])

    def test_fresh_prompt_keeps_original_ack_and_governed_manifest_fence(self):
        self.host.initial_active = False
        result = self.runtime.prompt(self.session_id, "新任务", client_message_id="new-command")
        self.assertEqual(result["turnId"], "new-input")
        self.assertEqual(self.runtime._states[self.session_id].client_message_id, "new-command")
        manifest = next(params for method, params in self.host.calls if method == "tools.sync")
        self.assertFalse(manifest["nativeMcpExecutionAllowed"])

    def test_lost_prompt_ack_quarantines_original_admission_instead_of_resubmitting(self):
        self.host.initial_active = False
        original_send = self.host.send
        def send(method, *args, **kwargs):
            value = original_send(method, *args, **kwargs)
            if method == "session.prompt":
                raise PiRuntimeCommandAcceptanceUnknown("lost prompt ACK")
            return value
        self.host.send = send
        with self.assertRaises(PiRuntimeCommandAcceptanceUnknown):
            self.runtime.prompt(self.session_id, "新任务", client_message_id="original-command")
        with self.assertRaises(PiRuntimeCommandAcceptanceUnknown):
            self.runtime.prompt(self.session_id, "新任务", client_message_id="original-command")
        self.assertEqual(sum(method == "session.prompt" for method, _ in self.host.calls), 1)
        self.assertTrue(self.runtime._states[self.session_id].prompt_dispatched)
        self.assertEqual(self.runtime._states[self.session_id].admission_client_message_id, "original-command")

    def test_recovered_history_is_passive_when_model_configuration_is_unavailable(self):
        self.runtime.config = replace(self.runtime.config, model_configured=False)
        result = self.runtime.session_snapshot(self.session_id)
        self.assertEqual(result["messages"][0]["turnId"], "original-input")
        self.assertFalse(any(method in {"session.prompt", "session.resume"} for method, _ in self.host.calls))

    def test_public_full_and_recent_snapshot_preserve_authoritative_recovery_identity(self):
        blocks = Mock()
        blocks.hydrate_messages.side_effect = lambda _id, messages: messages
        projection = AgentMessageSnapshotService(sessions=self.store, runtime_provider=lambda: self.runtime,
            workflow_projector=lambda _: {"todo": {}, "goal": {}, "actGate": {}}, agent_blocks=blocks,
            media=Mock(), observations=Mock(snapshot=Mock(return_value={"items": []})), events=self.events,
            background_jobs=Mock(list=Mock(return_value={"items": []})),
            room_public_messages=lambda _: None, room_recent_public_messages=lambda _: None)
        for view in ("", "recent"):
            with self.subTest(view=view):
                result = projection.messages(self.session_id, view=view)
                self.assertEqual(result["runtimeEngine"], "durable")
                self.assertTrue(result["paused"])
                self.assertTrue(result["recoverable"])
                self.assertTrue(result["projectionCurrent"])
                self.assertEqual(result["activeTurn"], {"turnId": "original-input", "clientMessageId": "original-client"})
        self.assertFalse(any(method in {"session.prompt", "session.resume"} for method, _ in self.host.calls))

    def test_completed_idle_host_reopen_projects_no_recovery_and_admits_distinct_prompt(self):
        # The native wire is doubled with the corrected Host metadata. Real
        # SQLite/Harness reopen behavior belongs to Pi's native regression.
        original_send = self.host.send

        def send(method, *args, **kwargs):
            result = original_send(method, *args, **kwargs)
            if method == "session.open":
                self.host.snapshot.update(paused=False, recoverable=False, isIdle=True, activeTurn=None)
                self.host.snapshot["messages"].append({
                    "id": "assistant-completed", "role": "assistant", "content": "原任务已经完成",
                    "_ragImeTurnId": "original-input", "stopReason": "stop",
                })
                result["snapshot"] = copy.deepcopy(self.host.snapshot)
            return result

        self.host.send = send
        blocks = Mock()
        blocks.hydrate_messages.side_effect = lambda _id, messages: messages
        projection = AgentMessageSnapshotService(sessions=self.store, runtime_provider=lambda: self.runtime,
            workflow_projector=lambda _: {"todo": {}, "goal": {}, "actGate": {}}, agent_blocks=blocks,
            media=Mock(), observations=Mock(snapshot=Mock(return_value={"items": []})), events=self.events,
            background_jobs=Mock(list=Mock(return_value={"items": []})),
            room_public_messages=lambda _: None, room_recent_public_messages=lambda _: None)
        for view in ("", "recent"):
            with self.subTest(view=view):
                result = projection.messages(self.session_id, view=view)
                self.assertEqual(result["runtimeEngine"], "durable")
                self.assertFalse(result["paused"])
                self.assertFalse(result["recoverable"])
                self.assertTrue(result["projectionCurrent"])
                self.assertIsNone(result["activeTurn"])
                self.assertEqual(result["status"], "idle")
                self.assertEqual([message["turnId"] for message in result["items"]],
                                 ["original-input", "original-input"])
        self.assertFalse(any(method in {"session.prompt", "session.resume", "session.abort"}
                             for method, _ in self.host.calls))
        result = self.runtime.prompt(self.session_id, "不同的新任务", client_message_id="new-distinct-client")
        self.assertEqual(result["turnId"], "new-input")
        self.assertEqual(result["clientMessageId"], "new-distinct-client")
        prompts = [params for method, params in self.host.calls if method == "session.prompt"]
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]["clientMessageId"], "new-distinct-client")
        self.assertFalse(any(method in {"session.resume", "session.abort"} for method, _ in self.host.calls))

    def test_resume_route_requires_exact_body_and_is_not_remote_executable(self):
        policy = default_route_policy()
        valid = ControlRequest(request_id="resume-request", path_id="agent.session.resume",
            params={"sessionId": self.session_id}, body={"turnId": "original-input", "clientMessageId": "original-client"})
        policy.authorize(valid, ControlAccessContext.native())
        with self.assertRaises(ControlApiError):
            policy.authorize(ControlRequest(request_id="missing-identity", path_id="agent.session.resume",
                params={"sessionId": self.session_id}, body={"turnId": "original-input"}), ControlAccessContext.native())
        with self.assertRaises(ControlApiError):
            policy.authorize(valid, ControlAccessContext.remote(device_id="phone", scopes={"agent.write"}))
        self.assertEqual(agent_session_route(f"/api/agent/sessions/{self.session_id}/resume"), (self.session_id, "resume"))

    def test_resume_application_cannot_resume_classic_or_a_different_original_input(self):
        app = self.application()
        with self.assertRaisesRegex(ValueError, "Durable"):
            app.resume_session(self.classic["id"], {"turnId": "original-input", "clientMessageId": "original-client"})
        with self.assertRaises(PiRuntimeError):
            app.resume_session(self.session_id, {"turnId": "wrong-input", "clientMessageId": "original-client"})
        self.assertFalse(any(method == "session.resume" for method, _ in self.host.calls))

    def test_service_resume_delegates_original_identity_through_existing_mutability_guard(self):
        service = object.__new__(AgentService)
        service._require_mutable_session = Mock()
        service.session_application = self.application()
        result = service.resume_session(self.session_id, {"turnId": "original-input", "clientMessageId": "original-client"})
        self.assertTrue(result["runtimeReceipt"]["resumed"])
        service._require_mutable_session.assert_called_once_with(self.session_id)
        self.assertFalse(any(method == "session.prompt" for method, _ in self.host.calls))

    def test_real_http_handler_resumes_exact_input_after_execution_owner_guard(self):
        agent = object.__new__(AgentService)
        agent._require_mutable_session = Mock()
        agent.session_application = self.application()
        handler = object.__new__(DebugRequestHandler)
        handler.service = Mock(agent=agent)
        handler.path = f"/api/agent/sessions/{self.session_id}/resume"
        body = json.dumps({"turnId": "original-input", "clientMessageId": "original-client"}).encode()
        handler.headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler._authorize_gateway_request = Mock(return_value=True)
        handler._management_post_security_error = Mock(return_value=None)
        handler._write_json = Mock()
        handler.do_POST()
        handler.service.require_agent_runtime_execution_owner.assert_called_once()
        code, response = handler._write_json.call_args.args
        self.assertEqual(int(code), 202)
        self.assertEqual(response["turnId"], "original-input")
        self.assertEqual(response["clientMessageId"], "original-client")
        self.assertTrue(response["runtimeReceipt"]["resumed"])
        self.assertFalse(any(method == "session.prompt" for method, _ in self.host.calls))

    def test_retired_timer_does_not_query_closed_store(self):
        self.runtime._states.clear()
        with patch.object(self.store, "get", side_effect=AssertionError("retired timer queried closed store")):
            self.runtime._settle_fallback_probe(self.session_id, "original-input")
            self.runtime._abort_fallback_expired(self.session_id, "original-input")

    def test_partial_native_history_does_not_rewrite_total_message_count(self):
        self.open()
        self.store.set_status(self.session_id, "busy", message_count=64)
        self.host.snapshot.update(partial=True, historyCursor="older")
        blocks = Mock()
        blocks.hydrate_messages.side_effect = lambda _id, messages: messages
        projection = AgentMessageSnapshotService(sessions=self.store, runtime_provider=lambda: self.runtime,
            workflow_projector=lambda _: {"todo": {}, "goal": {}, "actGate": {}}, agent_blocks=blocks,
            media=Mock(), observations=Mock(snapshot=Mock(return_value={"items": []})), events=self.events,
            background_jobs=Mock(list=Mock(return_value={"items": []})),
            room_public_messages=lambda _: None, room_recent_public_messages=lambda _: None)
        for view in ("", "recent"):
            result = projection.messages(self.session_id, view=view)
            self.assertTrue(result["partial"])
            self.assertEqual(self.store.get(self.session_id)["messageCount"], 64)

    def test_final_message_settlement_does_not_replace_durable_history_count_with_context_count(self):
        self.open()
        self.store.set_status(self.session_id, "busy", message_count=64)
        self.host.settlement = self.terminal_receipt()
        self.runtime.await_turn_settled(self.session_id, "original-input", client_message_id="original-client", timeout_seconds=2)
        self.assertEqual(self.store.get(self.session_id)["messageCount"], 64)


if __name__ == "__main__":
    unittest.main()
