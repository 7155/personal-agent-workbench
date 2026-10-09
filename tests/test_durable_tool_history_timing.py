from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from rag_ime.agent_protocol import AgentEventEnvelope
from rag_ime.agent_sessions import AgentSessionStore
from tests import test_agent_service as fixtures


class DurableToolHistoryTimingTests(unittest.TestCase):
    """Actual cold message/SQLite owners, without native process or Provider."""

    def setUp(self):
        fixtures.AgentServiceTests.setUp(self)
        self.addCleanup(self.service.close)
        self.sid = self.service.sessions.create(title="Ordinary durable", runtime_engine="durable")["id"]
        self.turn = "original-turn"
        self.call = "original-call"
        self.name = "workspace_shell"
        self.marker = {"schemaVersion": "rag-ime.pi-durable-tool-outcome.v1", "sessionId": self.sid,
            "runtimeSessionId": "11111111-1111-4111-8111-111111111111", "turnId": self.turn,
            "clientMessageId": "original-client", "toolCallId": self.call, "toolName": self.name,
            "entryId": "durable:105", "taskId": "durable:task:104",
            "assistantEntryId": "durable:103", "generationTaskId": "durable:task:102", "status": "aborted"}
        self.history = []
        for phase, timestamp in (("tool_started", 1791387981264), ("tool_finished", 1791387993365)):
            payload = {"toolCallId": self.call, "toolName": self.name, "args": {},
                       "isError": phase == "tool_finished"}
            if phase == "tool_finished":
                payload.update(result={"content": [{"type": "text", "text": "Original result"}]},
                               durableToolOutcome=self.marker)
            self.history.append(AgentEventEnvelope(event_id=f"{self.sid}:history:{phase}", session_id=self.sid,
                turn_id=self.turn, sequence=len(self.history)+1, created_at_ms=timestamp,
                event_type=phase, payload=payload, resume_token=phase).to_payload())

    def record(self, phase, timestamp, *, turn=None, call=None, name=None, sid=None, sequence=None):
        seq = sequence or (211 if phase == "tool_started" else 213)
        target = sid or self.sid
        self.service.sessions.record_runtime_event(event_id=f"{target}:{seq}", session_id=target,
            turn_id=turn or self.turn, sequence=seq, event_type=phase, created_at_ms=timestamp,
            metrics={"toolIdentity": {"toolCallId": call or self.call, "toolName": name or self.name}})

    def response(self, *, recent=False):
        body = {"messages": [], "toolHistoryEvents": copy.deepcopy(self.history), "runtimeEngine": "durable"}
        with patch.object(self.service.runtime, "session_snapshot", return_value=body), \
             patch.object(self.service.runtime, "recent_session_snapshot", return_value=body), \
             patch.object(self.service.runtime, "ensure", side_effect=AssertionError("no Session open")) as ensure, \
             patch.object(self.service.runtime, "_host", side_effect=AssertionError("no Host open")) as host:
            result = (self.service.message_snapshot.messages(self.sid, view="recent")
                      if recent else self.service.messages(self.sid))
        ensure.assert_not_called()
        host.assert_not_called()
        return [event for event in result["liveEvents"] if event["eventType"] in {"tool_started", "tool_finished"}]

    def test_ordinary_non_room_cold_pair_uses_persisted_gateway_times(self):
        self.record("tool_started", 1791387992606)
        self.record("tool_finished", 1791387993377)
        self.assertEqual(self.service.observations.snapshot({"sessionId": self.sid})["items"], [])
        for recent in (False, True):
            events = self.response(recent=recent)
            self.assertEqual([e["createdAtMs"] for e in events], [1791387992606, 1791387993377])
            self.assertTrue(all(e["payload"].get("toolTimingAvailable") is True for e in events))
            self.assertEqual(events[1]["payload"]["durableToolOutcome"], self.marker)
            self.assertEqual(events[1]["payload"]["result"], self.history[1]["payload"]["result"])
            self.assertTrue(events[1]["payload"]["isError"])

    def assert_unavailable(self):
        events = self.response()
        self.assertTrue(all(e["payload"].get("toolTimingAvailable") is False for e in events))
        self.assertTrue(all(e["payload"].get("toolTimingSource") == "unavailable" for e in events))
        self.assertEqual([e["createdAtMs"] for e in events], [e["createdAtMs"] for e in self.history])
        self.assertEqual(events[1]["payload"]["durableToolOutcome"], self.marker)

    def test_missing_pair_is_unavailable_not_provider_wait_duration(self):
        self.assert_unavailable()

    def test_incomplete_pair_is_unavailable(self):
        for phase in ("tool_started", "tool_finished"):
            with self.subTest(phase=phase):
                self.service.sessions.record_runtime_event(event_id=f"{self.sid}:only:{phase}",
                    session_id=self.sid, turn_id=self.turn, sequence=211, event_type=phase,
                    created_at_ms=1791387992606,
                    metrics={"toolIdentity": {"toolCallId": self.call, "toolName": self.name}})
                self.assert_unavailable()
                with self.service.sessions._connect() as conn:
                    conn.execute("DELETE FROM agent_runtime_events WHERE session_id=?", (self.sid,))

    def test_foreign_turn_call_name_or_session_cannot_supply_timing(self):
        foreign = self.service.sessions.create(title="Foreign", runtime_engine="durable")["id"]
        for kwargs in ({"turn": "foreign-turn"}, {"call": "foreign-call"},
                       {"name": "foreign-tool"}, {"sid": foreign}):
            with self.subTest(kwargs=kwargs):
                self.record("tool_started", 1791387992606, **kwargs)
                self.record("tool_finished", 1791387993377, **kwargs)
                self.assert_unavailable()
                with self.service.sessions._connect() as conn:
                    conn.execute("DELETE FROM agent_runtime_events")

    def test_reused_call_id_in_original_turn_is_ambiguous(self):
        self.record("tool_started", 1791387992606)
        self.record("tool_finished", 1791387993377)
        self.record("tool_started", 1791387994000, sequence=214)
        self.record("tool_finished", 1791387995000, sequence=215)
        self.assert_unavailable()

    def test_reversed_or_invalid_pair_is_unavailable(self):
        self.record("tool_started", 1791387994000)
        self.record("tool_finished", 1791387993377)
        self.assert_unavailable()

    def test_repeated_history_occurrences_cannot_borrow_one_pair(self):
        self.record("tool_started", 1791387992606)
        self.record("tool_finished", 1791387993377)
        self.history.extend(copy.deepcopy(self.history))
        events = self.response()
        self.assertTrue(all(e["payload"].get("toolTimingAvailable") is False for e in events))

    def test_pair_is_readable_by_fresh_store_after_cold_reopen(self):
        self.record("tool_started", 1791387992606)
        self.record("tool_finished", 1791387993377)
        reader = AgentSessionStore(self.root / "rag-ime.sqlite")
        self.addCleanup(reader.close)
        self.assertEqual(reader.runtime_tool_timing_pairs(self.sid, [(self.turn, self.call, self.name)]),
                         {(self.turn, self.call, self.name): (1791387992606, 1791387993377)})

    def observe(self, phase, timestamp, *, turn=None, call=None, name=None, sequence=None):
        seq = sequence or (211 if phase == "tool_started" else 213)
        self.service.observations.observe_agent_event(AgentEventEnvelope(
            event_id=f"{self.sid}:{seq}", session_id=self.sid, turn_id=turn or self.turn,
            sequence=seq, created_at_ms=timestamp, event_type=phase,
            payload={"toolCallId": call or self.call, "toolName": name or self.name,
                     "isError": phase == "tool_finished"}, resume_token=str(seq)), room_id="fixture-room")

    def test_exact_persisted_room_observation_pair_survives_missing_journal(self):
        self.observe("tool_started", 1791387992606)
        self.observe("tool_finished", 1791387993377)
        for recent in (False, True):
            events = self.response(recent=recent)
            self.assertEqual([e["createdAtMs"] for e in events], [1791387992606, 1791387993377])
            self.assertTrue(all(e["payload"].get("toolTimingAvailable") is True for e in events))
            self.assertTrue(all(e["payload"].get("toolTimingSource") == "gateway_observations" for e in events))
            self.assertEqual(events[1]["payload"]["durableToolOutcome"], self.marker)

    def test_foreign_or_incomplete_observation_cannot_supply_missing_journal_pair(self):
        self.observe("tool_started", 1791387992606, turn="foreign-turn")
        self.observe("tool_finished", 1791387993377, turn="foreign-turn")
        self.assert_unavailable()
        self.observe("tool_started", 1791387992606, sequence=214)
        self.assert_unavailable()

    def test_unbound_call_only_observation_does_not_change_original_sort_time(self):
        legacy = [{"phase": phase, "createdAtMs": time,
                   "refs": [{"kind": "tool_call", "id": self.call}]}
                  for phase, time in (("tool_started", 1791387992606), ("tool_finished", 1791387993377))]
        with patch.object(self.service.observations, "snapshot", return_value={"items": legacy}):
            self.assert_unavailable()

    def test_mismatched_original_observation_ref_is_unavailable(self):
        self.observe("tool_started", 1791387992606)
        self.observe("tool_finished", 1791387993377)
        items = self.service.observations.snapshot({"sessionId": self.sid, "category": "tool"})["items"]
        for item in items:
            for ref in item["refs"]:
                if ref["kind"] == "agent_event":
                    ref["id"] = "agent:foreign:211"
        with patch.object(self.service.observations, "snapshot", return_value={"items": items}):
            self.assert_unavailable()

    def test_persisted_journal_pair_is_preferred_over_observation_pair(self):
        self.record("tool_started", 1791387992606)
        self.record("tool_finished", 1791387993377)
        self.observe("tool_started", 1791387992000)
        self.observe("tool_finished", 1791387993000)
        events = self.response()
        self.assertEqual([e["createdAtMs"] for e in events], [1791387992606, 1791387993377])
        self.assertTrue(all(e["payload"].get("toolTimingSource") == "gateway_runtime_events" for e in events))

    def test_classic_history_retains_its_existing_timestamp_contract(self):
        self.sid = self.service.sessions.create(title="Classic control")["id"]
        for event in self.history:
            event["sessionId"] = self.sid
            event["payload"].pop("durableToolOutcome", None)
        self.record("tool_started", 1791387992606)
        self.record("tool_finished", 1791387993377)
        events = self.response()
        self.assertEqual([e["createdAtMs"] for e in events], [e["createdAtMs"] for e in self.history])
        self.assertTrue(all("toolTimingAvailable" not in e["payload"] for e in events))

    def test_original_failure_keeps_result_and_status_after_timing_correction(self):
        self.history[1]["payload"].pop("durableToolOutcome")
        self.record("tool_started", 1791387992606)
        self.record("tool_finished", 1791387993377)
        event = self.response()[1]
        self.assertTrue(event["payload"]["isError"])
        self.assertNotIn("durableToolOutcome", event["payload"])
        self.assertEqual(event["payload"]["result"], self.history[1]["payload"]["result"])


if __name__ == "__main__":
    unittest.main()
