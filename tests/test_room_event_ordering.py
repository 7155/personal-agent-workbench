"""Concurrent event admission tests. No Pi, network or production database."""
from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from rag_ime.rooms.store import AgentRoomEventHub
from test_room_event_visibility import EventStore, decode


class ProjectedStore(EventStore):
    def __init__(self):
        super().__init__()
        self.keys = {}

    def append_projected_event(self, *, projection_key, **values):
        if projection_key in self.keys:
            return self.keys[projection_key], False
        event = self.append_event(**values)
        self.keys[projection_key] = event
        return event, True

    def append_child_terminal_projection(self, **values):
        return self.append_projected_event(projection_key=values.pop('projection_key'), **values)


class RoomEventOrderingTests(unittest.TestCase):
    def setup_stream(self, hub):
        stream = hub.subscribe('r', heartbeat_seconds=.001)
        self.assertEqual(next(stream), b': connected\n\n')
        self.addCleanup(stream.close)
        return stream

    def publish(self, hub, method, key):
        if method == 'publish':
            return hub.publish(room_id='r')
        return getattr(hub, method)(room_id='r', projection_key=key)

    def check_delayed_fanout(self, method):
        entered, release = threading.Event(), threading.Event()

        class DelayedHub(AgentRoomEventHub):
            def pause(self, event):
                if event['sequence'] == 1:
                    entered.set()
                    if not release.wait(3):
                        raise AssertionError('test publisher was not released')

            def _fanout(self, event):  # ZIP1 delays before enqueue; ZIP2 does not use this path.
                self.pause(event)
                super()._fanout(event)

            def _notify_observers(self, event):  # ZIP2 already queued under the append lock.
                self.pause(event)
                super()._notify_observers(event)

        store = ProjectedStore()
        hub = DelayedHub(store)
        stream = self.setup_stream(hub)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.publish, hub, method, 'first')
            try:
                self.assertTrue(entered.wait(2))
                second = pool.submit(self.publish, hub, method, 'second')
                self.assertEqual(second.result(timeout=2)['sequence'], 2)
                # We can drain SSE even though the first observer is still slow.
                received = [decode(next(stream))]
                self.assertEqual(received[0]['sequence'], 1)
                received.append(decode(next(stream)))
                self.assertEqual([e['sequence'] for e in received], [1, 2])
                self.assertNotIn('snapshot_required', [e['eventType'] for e in received])
            finally:
                release.set()
                first.result(timeout=2)

    def test_normal_publish_enqueues_before_slow_observer(self):
        self.check_delayed_fanout('publish')

    def test_projected_publish_enqueues_before_slow_observer(self):
        self.check_delayed_fanout('publish_projection')

    def test_child_terminal_enqueues_before_slow_observer(self):
        self.check_delayed_fanout('publish_child_terminal')

    def test_parallel_publish_delivers_contiguous_stream(self):
        hub = AgentRoomEventHub(ProjectedStore())
        stream = self.setup_stream(hub)
        with ThreadPoolExecutor(max_workers=8) as pool:
            returned = list(pool.map(lambda i: hub.publish(room_id='r'), range(96)))
        self.assertEqual(sorted(e['sequence'] for e in returned), list(range(1, 97)))
        self.assertEqual([decode(next(stream))['sequence'] for _ in range(96)], list(range(1, 97)))

    def test_idempotent_projection_does_not_enqueue_twice(self):
        hub = AgentRoomEventHub(ProjectedStore())
        stream = self.setup_stream(hub)
        with ThreadPoolExecutor(max_workers=8) as pool:
            events = list(pool.map(lambda _: hub.publish_projection(room_id='r', projection_key='same'), range(32)))
        self.assertEqual({e['eventId'] for e in events}, {'r:1'})
        self.assertEqual(decode(next(stream))['sequence'], 1)
        self.assertEqual(next(stream), b': heartbeat\n\n')

    def test_observer_reentrant_publish_preserves_subscriber_order(self):
        hub = AgentRoomEventHub(ProjectedStore())
        stream = self.setup_stream(hub)
        def observer(event):
            if event['sequence'] == 1:
                hub.publish(room_id='r')
        hub.add_observer(observer)
        hub.publish(room_id='r')
        self.assertEqual([decode(next(stream))['sequence'] for _ in range(2)], [1, 2])

    def test_failing_observer_does_not_block_other_observers_or_sse(self):
        hub = AgentRoomEventHub(ProjectedStore())
        stream = self.setup_stream(hub)
        observed = []
        def broken(_event):
            raise ValueError('local projection failed')
        hub.add_observer(broken)
        hub.add_observer(observed.append)
        hub.publish(room_id='r')
        self.assertEqual(decode(next(stream))['sequence'], 1)
        self.assertEqual(len(observed), 1)

    def test_subscriber_attaching_during_slow_observer_gets_no_duplicate(self):
        hub = AgentRoomEventHub(ProjectedStore())
        entered, release = threading.Event(), threading.Event()
        def observer(_event):
            entered.set()
            release.wait(3)
        hub.add_observer(observer)
        with ThreadPoolExecutor(max_workers=1) as pool:
            published = pool.submit(hub.publish, room_id='r')
            try:
                self.assertTrue(entered.wait(2))
                stream = self.setup_stream(hub)
                self.assertEqual(decode(next(stream))['sequence'], 1)
            finally:
                release.set()
                published.result(timeout=2)
        self.assertEqual(next(stream), b': heartbeat\n\n')


if __name__ == '__main__':
    unittest.main(verbosity=2)
