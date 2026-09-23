from __future__ import annotations

import queue
import threading
import unittest

from rag_ime.agent_events import AgentEventHub


class PausedFirstQueue(queue.Queue):
    """Force a scheduling boundary between event allocation and delivery."""

    def __init__(self) -> None:
        super().__init__(maxsize=128)
        self.entered = threading.Event()
        self.release = threading.Event()

    def put_nowait(self, item):
        if item.sequence == 1:
            self.entered.set()
            if not self.release.wait(2):
                raise TimeoutError("test did not release the first publication")
        return super().put_nowait(item)


def drain(subscriber):
    events = []
    while True:
        try:
            events.append(subscriber.get_nowait())
        except queue.Empty:
            return events


class AgentEventPublicationOrderTests(unittest.TestCase):
    def interleave(self, second_action):
        hub = AgentEventHub()
        subscriber = PausedFirstQueue()
        hub._subscribers["session-order"].add(subscriber)
        errors = []
        second_started = threading.Event()
        second_done = threading.Event()

        def first():
            try:
                hub.publish("session-order", "text_delta", {"delta": "a"}, turn_id="turn-1")
            except BaseException as error:
                errors.append(error)

        def second():
            second_started.set()
            try:
                second_action(hub)
            except BaseException as error:
                errors.append(error)
            finally:
                second_done.set()

        first_thread = threading.Thread(target=first)
        second_thread = threading.Thread(target=second)
        first_thread.start()
        try:
            self.assertTrue(subscriber.entered.wait(1))
            second_thread.start()
            self.assertTrue(second_started.wait(1))
            # On the old implementation, the second publisher completes here
            # and overtakes the paused first one. With the fix it waits for the
            # publication lock. The deadline only bounds that negative wait.
            second_done.wait(0.15)
        finally:
            subscriber.release.set()
            first_thread.join(2)
            if second_thread.ident is not None:
                second_thread.join(2)
        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(errors, [])
        return hub, drain(subscriber)

    def test_concurrent_publishers_deliver_in_sequence_order(self):
        hub, events = self.interleave(
            lambda hub: hub.publish("session-order", "turn_completed", {}, turn_id="turn-1")
        )
        self.assertEqual([event.sequence for event in events], [1, 2])
        replay, gap = hub.replay("session-order")
        self.assertFalse(gap)
        self.assertEqual([event.event_id for event in events], [event.event_id for event in replay])

    def test_invalidation_cannot_be_followed_by_an_old_publication(self):
        _, events = self.interleave(
            lambda hub: hub.invalidate_projection("session-order")
        )
        self.assertEqual([event.event_type for event in events], ["snapshot_required"])

    def test_observer_runs_outside_the_publication_lock(self):
        hub = AgentEventHub()
        checks = []

        def observer(_event):
            completed = threading.Event()
            worker = threading.Thread(target=lambda: (hub.replay("s"), completed.set()))
            worker.start()
            checks.append(completed.wait(1))
            worker.join(1)

        hub.add_observer(observer)
        hub.publish("s", "status_changed", {"status": "busy"})
        self.assertEqual(checks, [True])

    def test_failed_durable_record_is_not_published(self):
        def reject(_event):
            raise OSError("storage unavailable")

        hub = AgentEventHub(event_recorder=reject)
        subscriber = queue.Queue(maxsize=2)
        hub._subscribers["s"].add(subscriber)
        with self.assertRaises(OSError):
            hub.publish("s", "status_changed", {"status": "busy"})
        self.assertTrue(subscriber.empty())

    def test_full_subscriber_queue_remains_bounded_and_ordered(self):
        hub = AgentEventHub()
        subscriber = queue.Queue(maxsize=2)
        hub._subscribers["s"].add(subscriber)
        for index in range(5):
            hub.publish("s", "text_delta", {"delta": str(index)}, turn_id="turn-1")
        self.assertEqual([event.sequence for event in drain(subscriber)], [4, 5])


if __name__ == "__main__":
    unittest.main()
