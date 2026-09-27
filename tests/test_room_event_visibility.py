"""Regression tests for the Room SSE delivery boundary (no Pi/model calls)."""
from __future__ import annotations
import json
import unittest
from rag_ime.rooms.store import AgentRoomEventHub, _room_event_sequence


class EventStore:
    """Deterministic bounded durable-event double; not a database integration test."""
    def __init__(self, count=0, first=1):
        self.events = []
        self.last = 0
        for _ in range(count):
            self.append_event(room_id="r")
        self.events = [e for e in self.events if e["sequence"] >= first]
    def append_event(self, **values):
        self.last += 1
        room_id = values.get("room_id", "r")
        event = dict(schemaVersion="rag-ime.agent-room-event.v1", roomId=room_id,
                     eventId=f"{room_id}:{self.last}", sequence=self.last,
                     resumeToken=f"{room_id}:{self.last}", turnId="root", participantId=None,
                     sourceSessionId="", createdAtMs=self.last, eventType="room_config_changed", payload={})
        self.events.append(event)
        return event
    def event_bounds(self, room_id):
        return (self.events[0]["sequence"] if self.events else 0, self.last)
    def list_events(self, room_id, *, after_sequence=0, limit=2000):
        return [e for e in self.events if e["sequence"] > after_sequence][:limit]


def decode(frame):
    return json.loads(next(line[6:] for line in frame.decode().splitlines() if line.startswith("data: ")))


class RoomEventVisibilityTests(unittest.TestCase):
    def stream(self, store, cursor=""):
        hub = AgentRoomEventHub(store)
        stream = hub.subscribe("r", after_event_id=cursor, heartbeat_seconds=.001)
        self.assertEqual(next(stream), b": connected\n\n")
        self.addCleanup(stream.close)
        return hub, stream

    def test_catchup_exceeding_replay_limit_requests_snapshot(self):
        store = EventStore(2001)
        _, stream = self.stream(store)
        event = decode(next(stream))
        self.assertEqual(event["eventType"], "snapshot_required")
        self.assertEqual(event["resumeToken"], "r:2001")
        self.assertEqual(len(store.events), 2001, "a transient control is not durable history")

    def test_exact_replay_limit_delivers_every_event(self):
        _, stream = self.stream(EventStore(2000))
        self.assertEqual([decode(next(stream))["sequence"] for _ in range(2000)], list(range(1, 2001)))
        self.assertEqual(next(stream), b": heartbeat\n\n")

    def test_retained_prefix_is_disclosed_even_for_zero_cursor(self):
        _, stream = self.stream(EventStore(5, first=4), "r:0")
        self.assertEqual(decode(next(stream))["eventType"], "snapshot_required")

    def test_internal_retention_hole_requests_snapshot(self):
        store = EventStore(4)
        store.events.pop(1)
        _, stream = self.stream(store)
        self.assertEqual(decode(next(stream))["eventType"], "snapshot_required")

    def test_queue_overflow_requests_snapshot_then_uses_durable_highwater(self):
        store = EventStore()
        hub, stream = self.stream(store)
        for _ in range(129):
            hub.publish(room_id="r")
        event = decode(next(stream))
        self.assertEqual((event["eventType"], event["resumeToken"]), ("snapshot_required", "r:129"))
        hub.publish(room_id="r")
        self.assertEqual(decode(next(stream))["sequence"], 130)
        self.assertEqual(len(store.events), 130)

    def test_reordered_publisher_fanout_does_not_silently_skip(self):
        store = EventStore()
        hub, stream = self.stream(store)
        first = store.append_event(room_id="r")
        second = store.append_event(room_id="r")
        hub._fanout(second)
        hub._fanout(first)
        self.assertEqual(decode(next(stream))["eventType"], "snapshot_required")
        hub.publish(room_id="r")
        self.assertEqual(decode(next(stream))["sequence"], 3)

    def test_duplicate_delivery_remains_idempotent(self):
        store = EventStore()
        hub, stream = self.stream(store)
        first = hub.publish(room_id="r")
        hub._fanout(first)
        hub.publish(room_id="r")
        self.assertEqual([decode(next(stream))["sequence"] for _ in range(2)], [1, 2])
        self.assertEqual(next(stream), b": heartbeat\n\n")

    def test_future_cursor_can_resume_after_transient_control(self):
        hub, stream = self.stream(EventStore(2), "r:99")
        self.assertEqual(decode(next(stream))["resumeToken"], "r:2")
        hub.publish(room_id="r")
        self.assertEqual(decode(next(stream))["sequence"], 3)

    def test_foreign_and_negative_cursor_rejected(self):
        for token in ("other:2", "r:-1"):
            with self.subTest(token=token), self.assertRaises(ValueError):
                next(AgentRoomEventHub(EventStore()).subscribe("r", after_event_id=token))
        self.assertIsNone(_room_event_sequence("r", "r:-1"))

    def test_empty_history_and_cleanup(self):
        hub, stream = self.stream(EventStore())
        self.assertEqual(next(stream), b": heartbeat\n\n")
        stream.close()
        self.assertNotIn("r", hub._subscribers)


if __name__ == "__main__":
    unittest.main(verbosity=2)
