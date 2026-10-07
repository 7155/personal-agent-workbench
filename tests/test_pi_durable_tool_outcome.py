from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from tests import test_pi_durable_runtime as fixtures


class PiDurableToolOutcomeTests(unittest.TestCase):
    """Actual Store/Runtime/live/cold projections; only native wire is doubled."""

    def setUp(self):
        fixtures.PiDurableRuntimeTests.setUp(self)
        send = self.host.send

        def native_wire(method, params=None, **kwargs):
            result = send(method, params, **kwargs)
            if method == "session.open":
                self.host.snapshot["piSessionId"] = "baab3e0b-8b82-4e7c-9014-86d9f520a197"
                result["snapshot"]["piSessionId"] = self.host.snapshot["piSessionId"]
            return result

        self.host.send = native_wire
        self.runtime.ensure(self.session_id)
        self.runtime._states[self.session_id].source_loop_id = "durable:task:73:assistant"
        self.outcome = {
            "schemaVersion": "rag-ime.pi-durable-tool-outcome.v1",
            "sessionId": self.session_id,
            "runtimeSessionId": self.host.snapshot["piSessionId"],
            "turnId": "original-input", "clientMessageId": "original-client",
            "toolCallId": "original-call", "toolName": "workspace_shell",
            "entryId": "durable:42", "taskId": "durable:task:74",
            "assistantEntryId": "durable:41", "generationTaskId": "durable:task:73",
            "status": "aborted",
        }
        self.runtime._handle_host_event({"protocolVersion": "2", "event": "agent.event",
            "sessionId": self.session_id, "turnId": "original-input", "clientMessageId": "original-client",
            "payload": {"type": "tool_execution_start", "toolCallId": "original-call",
                        "toolName": "workspace_shell", "args": {}}})
        self.result = {"content": [{"type": "text", "text": "Original tool output"}],
                       "details": {"token": "private-fixture-token", "exitCode": 1}}

    def emit(self, outcome=None, *, call="original-call", result=None):
        payload = {"type": "tool_execution_end", "toolCallId": call,
                   "toolName": "workspace_shell", "result": result or self.result, "isError": True}
        if outcome is not None:
            payload["durableToolOutcome"] = outcome
        self.runtime._handle_host_event({"protocolVersion": "2", "event": "agent.event",
            "sessionId": self.session_id, "turnId": "original-input",
            "clientMessageId": "original-client", "payload": payload})
        return [e.payload for e in self.events.replay(self.session_id)[0]
                if e.event_type == "tool_finished"][-1]

    def cold(self, outcome=None, *, result_id="durable:42:0", assistant_id="durable:task:73:assistant", no_host=False):
        result = {"id": result_id, "role": "toolResult", "toolCallId": "original-call",
                  "toolName": "workspace_shell", "isError": True, **copy.deepcopy(self.result)}
        if outcome is not None:
            result["durableToolOutcome"] = outcome
        self.host.snapshot["messages"] = [
            {"id": "original-user", "role": "user", "content": "Original input",
             "_ragImeTurnId": "original-input", "clientMessageId": "original-client"},
            {"id": assistant_id, "role": "assistant", "content": [{"type": "toolCall",
                "id": "original-call", "name": "workspace_shell", "arguments": {}}]}, result]
        snapshot = self.runtime.session_snapshot(self.session_id, _allow_host_open=not no_host)
        return [e["payload"] for e in snapshot["toolHistoryEvents"]
                if e["eventType"] == "tool_finished"][-1]

    def assert_receipt(self, payload):
        self.assertEqual(payload.get("durableToolOutcome"), self.outcome)
        self.assertTrue(payload["isError"])
        self.assertEqual(payload["result"]["content"], self.result["content"])
        self.assertNotIn("private-fixture-token", str(payload))

    def test_exact_native_outcome_survives_live_projection(self):
        self.assert_receipt(self.emit(self.outcome))

    def test_exact_native_outcome_survives_cold_projection(self):
        self.assert_receipt(self.cold(self.outcome))

    def test_resident_cold_projection_does_not_open_host_or_dispatch(self):
        self.host.calls.clear()
        with patch.object(self.runtime, "ensure", side_effect=AssertionError("no Session open")) as ensure, \
             patch.object(self.runtime, "_host", side_effect=AssertionError("no Host start")) as host:
            self.assert_receipt(self.cold(self.outcome, no_host=True))
        ensure.assert_not_called()
        host.assert_not_called()
        self.assertEqual([method for method, _ in self.host.calls], ["session.snapshot"])

    def test_foreign_lineage_is_not_a_cancellation_receipt(self):
        for field, value in {
            "sessionId": "agent:foreign", "runtimeSessionId": "7214cbba-f4cc-4bb7-9dfd-56618d1f5f9e",
            "turnId": "later-turn", "clientMessageId": "later-client",
            "toolCallId": "foreign-call", "toolName": "foreign_tool",
            "generationTaskId": "durable:task:99", "status": "completed",
            "taskId": "durable:task:0", "assistantEntryId": "not-native",
        }.items():
            with self.subTest(field=field):
                marker = {**self.outcome, field: value}
                self.events._tool_terminal_event_cache.clear()
                self.assertNotIn("durableToolOutcome", self.emit(marker))
                self.assertNotIn("durableToolOutcome", self.cold(marker))

    def test_cold_result_entry_and_original_assistant_must_match(self):
        for kwargs in ({"result_id": "durable:99:0"}, {"assistant_id": "durable:task:99:assistant"}):
            self.assertNotIn("durableToolOutcome", self.cold(self.outcome, **kwargs))

    def test_arbitrary_output_and_error_text_do_not_supply_owner_outcome(self):
        self.result["content"][0]["text"] = "Tool workspace_shell was aborted"
        self.result["details"]["durableToolOutcome"] = self.outcome
        for payload in (self.emit(), self.cold()):
            self.assertNotIn("durableToolOutcome", payload)
            self.assertTrue(payload["isError"])
            self.assertIn("aborted", str(payload["result"]))

    def test_malformed_or_credential_bearing_envelopes_are_not_promoted(self):
        variants = [None, "aborted", {**self.outcome, "apiKey": "private-fixture-key"},
                    {**self.outcome, "turnId": "x" * 513},
                    {**self.outcome, "entryId": "durable:0"},
                    {**self.outcome, "runtimeSessionId": "1"},
                    {**self.outcome, "schemaVersion": "unknown"},
                    {**self.outcome, "clientMessageId": None},
                    {k: v for k, v in self.outcome.items() if k != "taskId"}]
        for marker in variants:
            with self.subTest(marker=marker):
                self.events._tool_terminal_event_cache.clear()
                for payload in (self.emit(marker), self.cold(marker)):
                    self.assertNotIn("durableToolOutcome", payload)
                    self.assertNotIn("private-fixture-key", str(payload))
                    self.assertTrue(payload["isError"])

    def test_classic_does_not_promote_even_matching_native_shape(self):
        classic_id = self.classic["id"]
        self.runtime._states[classic_id] = fixtures._HostedSessionState(
            turn_id="original-input", client_message_id="original-client")
        self.runtime._handle_host_event({"protocolVersion": "2", "event": "agent.event",
            "sessionId": classic_id, "turnId": "original-input", "clientMessageId": "original-client",
            "payload": {"type": "tool_execution_end", "toolCallId": "original-call",
                "toolName": "workspace_shell", "result": self.result, "isError": True,
                "durableToolOutcome": {**self.outcome, "sessionId": classic_id}}})
        event = self.events.replay(classic_id)[0][-1]
        self.assertEqual(event.event_type, "tool_finished")
        self.assertNotIn("durableToolOutcome", event.payload)

    def test_cold_missing_or_mismatched_original_user_is_not_promoted(self):
        self.cold(self.outcome)
        for update in ({"clientMessageId": "foreign-client"}, {"_ragImeTurnId": "foreign-turn"}):
            self.host.snapshot["messages"][0].update(update)
            events = self.runtime.session_snapshot(self.session_id)["toolHistoryEvents"]
            finish = [e["payload"] for e in events if e["eventType"] == "tool_finished"][-1]
            self.assertNotIn("durableToolOutcome", finish)

    def test_parallel_calls_keep_their_own_generation_and_failure(self):
        failed = self.emit(call="failed-call", result={"content": [{"type": "text", "text": "Actual failure"}]})
        aborted = self.emit(self.outcome)
        self.assertNotIn("durableToolOutcome", failed)
        self.assertEqual(aborted.get("durableToolOutcome"), self.outcome)
        self.assertTrue(failed["isError"])

    def test_cold_raw_message_bindings_cannot_contradict_owner_envelope(self):
        for index in (1, 2):
            for field in ("_ragImeTurnId", "clientMessageId"):
                self.cold(self.outcome)
                self.host.snapshot["messages"][index][field] = "foreign-original"
                events = self.runtime.session_snapshot(self.session_id)["toolHistoryEvents"]
                finish = [e["payload"] for e in events if e["eventType"] == "tool_finished"][-1]
                self.assertNotIn("durableToolOutcome", finish)

    def test_late_tool_result_uses_original_call_generation(self):
        self.runtime._states[self.session_id].source_loop_id = "durable:task:99:assistant"
        self.assertEqual(self.emit(self.outcome).get("durableToolOutcome"), self.outcome)

    def test_prior_genuine_failure_is_not_rewritten_by_late_stop(self):
        failure = self.emit(result={"content": [{"type": "text", "text": "Actual failure"}]})
        self.emit(self.outcome)
        finishes = [e.payload for e in self.events.replay(self.session_id)[0]
                    if e.event_type == "tool_finished"]
        self.assertEqual(len(finishes), 1)
        self.assertEqual(finishes[0], failure)
        self.assertNotIn("durableToolOutcome", finishes[0])


if __name__ == "__main__":
    unittest.main()
