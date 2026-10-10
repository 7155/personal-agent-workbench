from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from rag_ime.agent_event_projection import runtime_event_metrics
from rag_ime.agent_protocol import AgentEventEnvelope
from rag_ime.agent_service import AgentService
from rag_ime.pi.config import PiRuntimeConfig
from rag_ime.pi.runtime import _HostedSessionState
from rag_ime.contracts.json_schema import validate_contract, ContractValidationError


class AgentDirectoryFactsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-directory-facts-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.service = AgentService(db_path=root / "test.sqlite", runtime_config=PiRuntimeConfig(
            enabled=False, executable=None, agent_dir=root / "agent", session_dir=root / "sessions",
            logs_dir=root / "logs", idle_timeout_seconds=0,
        ))
        self.addCleanup(self.service.close)
        self.sid = str(self.service.sessions.create(title="public directory fixture")["id"])

    def event(self, kind="turn_completed", payload=None, sequence=1, turn="turn:one"):
        return AgentEventEnvelope(event_id=f"{self.sid}:{sequence}", session_id=self.sid,
            turn_id=turn, sequence=sequence, created_at_ms=sequence, event_type=kind,
            payload=payload or {}, resume_token=f"{self.sid}:{sequence}")

    def record(self, event):
        self.service.sessions.record_runtime_event(event_id=event.event_id, session_id=event.session_id,
            turn_id=event.turn_id, sequence=event.sequence, event_type=event.event_type,
            created_at_ms=event.created_at_ms, metrics=runtime_event_metrics(event))

    def item(self):
        return next(row for row in self.service.list_sessions({"projectionOnly": True})["items"] if row["id"] == self.sid)

    def test_directory_projects_exact_waiting_without_request_content_or_host(self):
        state = _HostedSessionState(turn_id="turn:one")
        state.pending_ui_requests["request:one"] = {"requestId": "request:one", "requestKind": "grouped_questions",
            "_turnId": "turn:one", "message": "private question", "questions": ["private"]}
        self.service.runtime._states[self.sid] = state
        with patch.object(self.service.runtime, "ensure", side_effect=AssertionError("must not open Host")):
            facts = self.item()["presentationFacts"]
        self.assertEqual(facts, {"activeTurnId": "turn:one", "waiting": [
            {"turnId": "turn:one", "requestId": "request:one", "kind": "input"}]})
        self.assertNotIn("private", json.dumps(facts))

    def test_directory_retains_typed_terminal_exact_identity(self):
        self.record(self.event(payload={"status": "completed"}))
        self.assertEqual(self.item()["presentationFacts"]["terminal"], {
            "eventId": f"{self.sid}:1", "turnId": "turn:one", "sequence": 1, "outcome": "completed"})
        self.assertNotIn("presentationFacts", self.service.list_sessions()["items"][0])

    def test_legacy_empty_and_latest_failure_cannot_be_done(self):
        self.record(self.event(payload={"status": "completed"}))
        self.record(self.event(kind="turn_failed", payload={"error": "private"}, sequence=2))
        self.assertEqual(self.item()["presentationFacts"]["terminal"]["outcome"], "failed")
        self.record(self.event(sequence=3, turn="turn:legacy"))
        self.assertNotIn("terminal", self.item().get("presentationFacts", {}))

    def test_stop_is_typed_aborted_even_with_conflicting_success(self):
        self.record(self.event(payload={"status": "completed", "aborted": True}))
        self.assertEqual(self.item()["presentationFacts"]["terminal"]["outcome"], "aborted")

    def test_normal_agent_settled_producer_supplies_success_type(self):
        self.service.runtime._states[self.sid] = _HostedSessionState(turn_id="turn:one")
        with patch.object(self.service.runtime, "_refresh_terminal_recent_projection"), \
             patch.object(self.service.runtime, "_schedule_idle_locked"):
            self.service.runtime._handle_host_event({"protocolVersion": "2", "event": "agent.event",
                "sessionId": self.sid, "turnId": "turn:one", "payload": {"type": "agent_settled"}})
        terminal = self.service.events.replay(self.sid)[0][-1]
        self.assertEqual(terminal.payload.get("status"), "completed")
        self.assertEqual(runtime_event_metrics(terminal)["terminalOutcome"], "completed")

    def test_resolved_cancelled_stale_foreign_or_resolving_request_is_not_waiting(self):
        state = _HostedSessionState(turn_id="turn:current")
        self.service.runtime._states[self.sid] = state
        for index, extra in enumerate([
            {"resolutionState": "resolved"}, {"resolutionState": "cancelled"},
            {"_turnId": "turn:old"}, {"sessionId": "foreign"}, {"_resolving": True},
            {"requestId": "different"},
        ]):
            request_id = f"request:{index}"
            state.pending_ui_requests[request_id] = {"requestId": request_id, "_turnId": "turn:current", **extra}
        self.assertEqual(self.item()["presentationFacts"]["waiting"], [])
        state.pending_ui_requests.clear()
        state.pending_ui_requests["request:live"] = {"requestId": "request:live", "_turnId": "turn:current"}
        self.assertEqual(len(self.item()["presentationFacts"]["waiting"]), 1)
        with patch.object(state, "abort_requested_turn_id", "turn:current"):
            self.assertEqual(self.item()["presentationFacts"]["waiting"], [])
        with patch.object(state, "recoverable", True):
            self.assertNotIn("waiting", self.item().get("presentationFacts", {}))
        state.retired_turn_ids.add("turn:current")
        self.assertNotIn("waiting", self.item().get("presentationFacts", {}))

    def test_current_approval_review_are_content_free_and_turn_clearing_removes_waiting(self):
        state = _HostedSessionState(turn_id="turn:one")
        state.pending_approvals["approval:one"] = "request:approval"
        state.pending_reviews["review:one"] = "request:review"
        self.service.runtime._states[self.sid] = state
        self.assertEqual(self.item()["presentationFacts"]["waiting"], [
            {"turnId": "turn:one", "requestId": "request:review", "kind": "review"},
            {"turnId": "turn:one", "requestId": "request:approval", "kind": "approval"}])
        state.turn_id = ""
        self.assertEqual(self.item()["presentationFacts"]["waiting"], [])

    def test_legacy_runtime_and_missing_resident_state_remain_unknown(self):
        self.assertNotIn("presentationFacts", self.item())
        class LegacyRuntime:
            pass
        with patch.object(self.service.session_application, "_runtime_provider", return_value=LegacyRuntime()):
            self.assertNotIn("presentationFacts", self.item())
        self.record(self.event(payload={"status": "completed"}))
        with patch.object(self.service.session_application, "_runtime_provider", return_value=LegacyRuntime()):
            self.assertEqual(self.item()["presentationFacts"]["terminal"]["outcome"], "completed")
            self.assertNotIn("waiting", self.item()["presentationFacts"])

    def test_terminal_batch_is_single_scoped_query_and_never_uses_older_success(self):
        admitted = [self.sid]
        for index in range(100):
            admitted.append(str(self.service.sessions.create(title=f"public {index}")["id"]))
        for index, sid in enumerate(admitted):
            self.service.sessions.record_runtime_event(event_id=f"event:{index}", session_id=sid,
                turn_id="turn:one", sequence=1, event_type="turn_completed", created_at_ms=1,
                metrics={"terminalOutcome": "completed"})
        sql = []
        read = self.service.sessions._read_connect
        @contextmanager
        def traced():
            with read() as conn:
                conn.set_trace_callback(sql.append)
                try:
                    yield conn
                finally:
                    conn.set_trace_callback(None)
        with patch.object(self.service.sessions, "_read_connect", side_effect=traced):
            facts = self.service.sessions.directory_terminal_facts(admitted)
        self.assertEqual(set(facts), set(admitted[:100]))
        self.assertEqual(len([statement for statement in sql if "agent_runtime_events" in statement]), 1)
        self.assertNotIn(admitted[-1], next(statement for statement in sql if "agent_runtime_events" in statement))
        self.record(self.event(sequence=2))
        self.assertNotIn(self.sid, self.service.sessions.directory_terminal_facts([self.sid]))

    def test_runtime_batch_caps_admitted_ids_and_visible_requests_without_borrowing(self):
        admitted = [f"public:session:{index}" for index in range(101)]
        for sid in admitted:
            state = _HostedSessionState(turn_id=f"turn:{sid}")
            for index in range(10):
                request_id = f"request:{index}"
                state.pending_ui_requests[request_id] = {"requestId": request_id, "_turnId": state.turn_id}
            self.service.runtime._states[sid] = state
        facts = self.service.runtime.directory_waiting_facts(admitted)
        self.assertEqual(set(facts), set(admitted[:100]))
        self.assertEqual(len(facts[admitted[0]]["waiting"]), 8)
        self.assertTrue(all(request["turnId"] == f"turn:{admitted[0]}" for request in facts[admitted[0]]["waiting"]))

    def test_corrupt_or_incompatible_terminal_metadata_is_not_a_fact(self):
        for sequence, metrics in enumerate([
            {"terminalOutcome": "invented"}, {"terminalOutcome": []},
            {"terminalOutcome": "failed"}, {}, {"private": "completed"},
        ], start=1):
            with self.subTest(metrics=metrics):
                self.service.sessions.record_runtime_event(event_id=f"bad:{sequence}", session_id=self.sid,
                    turn_id="turn:one", sequence=sequence, event_type="turn_completed", created_at_ms=1,
                    metrics=metrics)
                self.assertNotIn(self.sid, self.service.sessions.directory_terminal_facts([self.sid]))
        for payload in [{}, {"status": "idle"}, {"status": "completed", "aborted": "false"},
                        {"status": "completed", "aborted": 0}]:
            self.assertNotIn("terminalOutcome", runtime_event_metrics(self.event(payload=payload)))

    def test_agent_settled_stop_and_failure_never_normalize_to_success(self):
        for suffix, extra, expected in [("stop", {"abort_requested_turn_id": "turn:stop"}, "aborted"),
                                        ("error", {"final_error": "public failure"}, "failed")]:
            turn_id = f"turn:{suffix}"
            state = _HostedSessionState(turn_id=turn_id, **extra)
            self.service.runtime._states[self.sid] = state
            with patch.object(self.service.runtime, "_refresh_terminal_recent_projection"), \
                 patch.object(self.service.runtime, "_schedule_idle_locked"):
                self.service.runtime._handle_host_event({"protocolVersion": "2", "event": "agent.event",
                    "sessionId": self.sid, "turnId": turn_id, "payload": {"type": "agent_settled"}})
            terminal = self.service.events.replay(self.sid)[0][-1]
            self.assertEqual(runtime_event_metrics(terminal)["terminalOutcome"], expected)

    def test_optional_api_contract_preserves_legacy_and_rejects_content_or_bad_shape(self):
        session = self.service.sessions.get(self.sid)
        validate_contract(session, "agent-session.v1.json")
        valid = {"activeTurnId": "turn:one", "waiting": [
            {"turnId": "turn:one", "requestId": "request:one", "kind": "input"}],
            "terminal": {"eventId": "event:one", "turnId": "turn:previous", "sequence": 1, "outcome": "completed"}}
        validate_contract({**session, "presentationFacts": valid}, "agent-session.v1.json")
        for invalid in [{**valid, "prompt": "private"},
                        {"waiting": [{"turnId": "turn:one", "requestId": "request:one", "kind": "attention"}]},
                        {"terminal": {"eventId": "event:one", "turnId": "turn:one", "sequence": 0, "outcome": "completed"}},
                        {"waiting": valid["waiting"] * 9}]:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ContractValidationError):
                    validate_contract({**session, "presentationFacts": invalid}, "agent-session.v1.json")

    def test_actual_ui_resolution_ack_removes_waiting_and_does_not_read_host(self):
        state = _HostedSessionState(turn_id="turn:one")
        state.pending_ui_requests["request:one"] = {
            "requestId": "request:one", "_turnId": "turn:one", "method": "input"}
        self.service.runtime._states[self.sid] = state
        self.assertEqual(len(self.item()["presentationFacts"]["waiting"]), 1)
        from unittest.mock import Mock
        transport = Mock()
        def acknowledge(*args, **kwargs):
            self.assertEqual(self.item()["presentationFacts"]["waiting"], [])
            return {}
        transport.send.side_effect = acknowledge
        with patch.object(self.service.runtime, "_require_client", return_value=transport):
            self.service.runtime.resolve_ui_request(self.sid, "request:one", response={"value": "public answer"})
        transport.send.assert_called_once_with("ui.resolve", {
            "sessionId": self.sid, "requestId": "request:one", "response": {"value": "public answer"}})
        self.assertEqual(self.item()["presentationFacts"]["waiting"], [])
        self.assertNotIn("request:one", state.pending_ui_requests)
        self.assertEqual(self.service.events.replay(self.sid)[0][-1].payload["resolutionState"], "resolved")
