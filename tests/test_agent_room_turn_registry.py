"""RoomTurnRegistry is the sole owner of Room-turn in-memory state.

Phase 3 Batch 1 moved the last direct dict/set mutations out of AgentService
and the legacy dispatch/cancellation services. These tests pin the contract
that migration relied on: reservations are atomic, caller pre-checks run
inside the registry lock, snapshots preserve the pending-before-registered
ordering, and the cancellation-receipt history keeps its exact 2048 bound and
eviction order. A final gate proves the deleted private aliases cannot creep
back into production code unnoticed.
"""

from __future__ import annotations

import ast
import threading
import unittest
from pathlib import Path

from rag_ime.rooms.turn_registry import (
    RoomSessionBusyError,
    RoomTurnRegistry,
)
from rag_ime.agent_protocol import AgentEventEnvelope

ROOT = Path(__file__).resolve().parent.parent

# The private aliases deleted from AgentService. Production code must reach
# this state only through RoomTurnRegistry.
BANNED_ATTRIBUTES = frozenset(
    {
        "_room_turn_lock",
        "_pending_room_turn_by_session",
        "_pending_room_dispatch_by_session",
        "_room_turn_by_session_turn",
        "_room_dispatch_by_session_turn",
        "_room_topic_by_room_turn",
        "_room_user_priority_sessions",
        "_cancelled_room_turns",
        "_cancelled_room_root_by_session",
        "_cancelled_room_turn_by_session_turn",
    }
)


class ExactDispatchFinishTests(unittest.TestCase):
    def test_exact_finish_preserves_newer_pending_dispatch_in_same_root(self):
        registry = RoomTurnRegistry()
        registry.begin("session", "root", dispatch_id="old-dispatch", child=True)
        registry.accept("session", "old-turn", "root")
        registry.begin("session", "root", dispatch_id="new-dispatch", child=True,
                       work_item_id="new-work", work_item_revision=2)
        self.assertTrue(registry.finish_exact_dispatch("session", "old-turn", "root", "old-dispatch"))
        self.assertEqual(registry.active_turn("session"), ("root", "new-dispatch"))
        self.assertEqual(registry.pending_work_by_session["session"]["workItemId"], "new-work")
        self.assertIn("session", registry.pending_child_dispatch_by_session)
        self.assertNotIn("old-dispatch", registry.child_dispatch_ids)
        self.assertFalse(registry.finish_exact_dispatch("session", "old-turn", "root", "old-dispatch"))

    def test_other_root_or_dispatch_cannot_remove_turn(self):
        registry = RoomTurnRegistry()
        registry.begin("session", "root", dispatch_id="dispatch")
        registry.accept("session", "turn", "root")
        self.assertFalse(registry.finish_exact_dispatch("session", "turn", "other-root", "dispatch"))
        self.assertFalse(registry.finish_exact_dispatch("session", "turn", "root", "other-dispatch"))
        self.assertEqual(registry.active_turn("session"), ("root", "dispatch"))


class PriorityReservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = RoomTurnRegistry()

    def _lock_is_held(self) -> bool:
        """True when another thread cannot take the registry lock."""

        acquired: list[bool] = []

        def _try() -> None:
            got = self.registry.lock.acquire(timeout=0.05)
            acquired.append(got)
            if got:
                self.registry.lock.release()

        probe = threading.Thread(target=_try)
        probe.start()
        probe.join()
        return not acquired[0]

    def test_hold_if_idle_reserves_all_or_nothing(self) -> None:
        self.registry.begin("s2", "turn-1")
        with self.assertRaises(RoomSessionBusyError) as caught:
            self.registry.hold_priority_if_idle(["s1", "s2", "s3"])
        self.assertEqual(caught.exception.session_id, "s2")
        # s1 passed its check before s2 failed; nothing may stay reserved.
        self.assertEqual(self.registry.user_priority_sessions, set())

    def test_hold_if_idle_rejects_priority_pending_and_registered(self) -> None:
        self.registry.hold_priority(["p"])
        self.registry.begin("q", "turn-q")
        self.registry.begin("r", "turn-r")
        self.registry.accept("r", "session-turn-r", "turn-r")
        for session_id in ("p", "q", "r"):
            with self.subTest(session_id=session_id):
                with self.assertRaises(RoomSessionBusyError):
                    self.registry.hold_priority_if_idle([session_id])
        self.registry.hold_priority_if_idle(["free"])
        self.assertIn("free", self.registry.user_priority_sessions)

    def test_ensure_available_runs_inside_the_registry_lock(self) -> None:
        held: list[bool] = []

        def _probe(session_id: str) -> None:
            held.append(self._lock_is_held())

        self.registry.hold_priority_if_idle(["s1"], ensure_available=_probe)
        self.assertEqual(held, [True])

    def test_ensure_available_failure_reserves_nothing(self) -> None:
        def _reject(session_id: str) -> None:
            if session_id == "s2":
                raise ValueError("participant is no longer active")

        with self.assertRaises(ValueError):
            self.registry.hold_priority_if_idle(
                ["s1", "s2"],
                ensure_available=_reject,
            )
        self.assertEqual(self.registry.user_priority_sessions, set())

    def test_release_variants_match_previous_set_semantics(self) -> None:
        self.registry.hold_priority(["a", "b", "c"])
        # difference_update semantics: absent members are ignored.
        self.registry.release_priority(["a", "missing"])
        self.assertEqual(self.registry.user_priority_sessions, {"b", "c"})
        # discard semantics: releasing twice is a no-op, not an error.
        self.registry.release_priority_session("b")
        self.registry.release_priority_session("b")
        self.assertEqual(self.registry.user_priority_sessions, {"c"})

    def test_exact_reservation_release_preserves_a_newer_prebinding_claim(self) -> None:
        first = self.registry.hold_priority_if_idle(["session", "peer"])
        self.assertIsNotNone(first)
        self.registry.release_priority_session("session")
        second = self.registry.hold_priority_if_idle(["session"])
        self.assertIsNot(first, second)
        self.registry.release_priority(["session", "peer"], reservation=first)
        self.assertEqual(self.registry.user_priority_sessions, {"session"})
        self.assertEqual(self.registry.priority_reservations(["session", "peer"]), {"session": second})
        self.registry.release_priority_session("session", reservation=second)
        self.assertFalse(self.registry.user_priority_sessions)
        self.assertFalse(self.registry.priority_reservations(["session"]))

    def test_unconditional_hold_rotates_its_reservation_identity(self) -> None:
        first = self.registry.hold_priority(["session"])
        second = self.registry.hold_priority(["session"])
        self.assertIsNotNone(first)
        self.assertIsNot(first, second)
        self.registry.release_priority_session("session", reservation=first)
        self.assertEqual(self.registry.user_priority_sessions, {"session"})
        self.registry.release_priority_session("session", reservation=second)
        self.assertFalse(self.registry.user_priority_sessions)

    def test_session_turn_active_excludes_the_priority_set(self) -> None:
        """The direct-Agent guard never treated a priority hold as busy."""

        self.registry.hold_priority(["held"])
        self.assertFalse(self.registry.session_turn_active("held"))
        self.registry.begin("pending", "turn-p")
        self.assertTrue(self.registry.session_turn_active("pending"))
        self.registry.begin("reg", "turn-g")
        self.registry.accept("reg", "st-1", "turn-g")
        self.assertTrue(self.registry.session_turn_active("reg"))


class RuntimeTurnBindingTests(unittest.TestCase):
    @staticmethod
    def _event(turn_id: str, event_type: str) -> AgentEventEnvelope:
        return AgentEventEnvelope(
            event_id=f"event:{turn_id}:{event_type}",
            session_id="session:target",
            turn_id=turn_id,
            sequence=1,
            created_at_ms=1,
            event_type=event_type,
            payload={},
            resume_token=f"event:{turn_id}:{event_type}",
        )

    def test_pending_root_binds_only_the_runtime_turn_passed_to_accept(
        self,
    ) -> None:
        registry = RoomTurnRegistry()
        prior = self._event("runtime-turn:prior", "turn_completed")
        current = self._event("runtime-turn:current", "text_delta")
        registry.begin(
            "session:target",
            "room-root:current",
            dispatch_id="dispatch:current",
        )

        self.assertFalse(registry.allows_room_event(prior))
        self.assertEqual(registry.registered_turn_for_event(prior), "")
        self.assertEqual(registry.dispatch_for_event(prior), "")
        self.assertEqual(
            registry.pending_turn_by_session.get("session:target"),
            "room-root:current",
        )
        self.assertNotIn(
            ("session:target", "runtime-turn:prior"),
            registry.turn_by_session_turn,
        )

        self.assertEqual(
            registry.accept(
                "session:target",
                "runtime-turn:current",
                "room-root:current",
            ),
            (),
        )

        self.assertFalse(registry.allows_room_event(prior))
        self.assertTrue(registry.allows_room_event(current))
        self.assertEqual(
            registry.registered_turn_for_event(current),
            "room-root:current",
        )
        self.assertEqual(
            registry.dispatch_for_event(current),
            "dispatch:current",
        )

    def test_work_identity_is_fenced_to_the_accepted_runtime_turn(self) -> None:
        registry = RoomTurnRegistry()
        current = self._event("runtime-turn:current", "tool_started")
        registry.begin(
            "session:target",
            "room-root:current",
            dispatch_id="dispatch:current",
            child=True,
            work_item_id="work:1",
            work_item_revision=3,
            attempt_id="attempt:3",
        )

        self.assertEqual(registry.work_identity_for_event(current), {})
        registry.accept(
            "session:target",
            "runtime-turn:current",
            "room-root:current",
        )
        self.assertEqual(
            registry.work_identity_for_event(current),
            {
                "workItemId": "work:1",
                "workItemRevision": 3,
                "attemptId": "attempt:3",
            },
        )

        registry.finish(
            "session:target",
            "runtime-turn:current",
            "room-root:current",
        )
        self.assertEqual(registry.work_identity_for_event(current), {})

    def test_unregistered_participant_session_turn_is_not_a_room_event(
        self,
    ) -> None:
        registry = RoomTurnRegistry()

        self.assertFalse(
            registry.allows_room_event(
                self._event("runtime-turn:direct", "text_delta")
            )
        )

    def test_accept_replays_current_terminal_buffered_before_runtime_ack(
        self,
    ) -> None:
        registry = RoomTurnRegistry()
        delta = self._event("runtime-turn:current", "text_delta")
        terminal = self._event(
            "runtime-turn:current",
            "turn_completed",
        )
        registry.begin(
            "session:target",
            "room-root:current",
            dispatch_id="dispatch:current",
        )

        self.assertFalse(registry.allows_room_event(delta))
        self.assertFalse(registry.allows_room_event(terminal))
        self.assertTrue(registry.session_turn_active("session:target"))

        buffered = registry.accept(
            "session:target",
            "runtime-turn:current",
            "room-root:current",
        )

        self.assertEqual(buffered, (delta, terminal))
        self.assertEqual(registry.pending_events_by_session_turn, {})
        for event in buffered:
            self.assertEqual(
                registry.registered_turn_for_event(event),
                "room-root:current",
            )
            if event.event_type == "turn_completed":
                registry.finish(
                    event.session_id,
                    event.turn_id,
                    "room-root:current",
                )
        self.assertFalse(registry.session_turn_active("session:target"))

    def test_cancelled_turn_rejects_late_events_and_allows_next_turn(
        self,
    ) -> None:
        registry = RoomTurnRegistry()
        old = self._event("runtime-turn:old", "text_delta")
        current = self._event("runtime-turn:current", "text_delta")
        registry.begin(
            "session:target",
            "room-root:old",
            dispatch_id="dispatch:old",
        )
        registry.accept(
            "session:target",
            "runtime-turn:old",
            "room-root:old",
        )

        registry.record_cancellation(
            "room-root:old",
            "cancel:old",
        )
        registry.cancel("session:target", "room-root:old")

        self.assertFalse(registry.allows_room_event(old))
        self.assertEqual(
            registry.registered_turn_for_event(old),
            "room-root:old",
        )
        self.assertFalse(registry.session_turn_active("session:target"))

        registry.begin(
            "session:target",
            "room-root:current",
            dispatch_id="dispatch:current",
        )
        self.assertFalse(registry.allows_room_event(current))
        registry.accept(
            "session:target",
            "runtime-turn:current",
            "room-root:current",
        )
        self.assertTrue(registry.allows_room_event(current))
        self.assertFalse(registry.allows_room_event(old))
        self.assertTrue(registry.session_turn_active("session:target"))

    def test_cancelled_abort_terminal_can_be_claimed_exactly_once(
        self,
    ) -> None:
        registry = RoomTurnRegistry()
        registry.begin(
            "session:target",
            "room-root:cancelled",
            dispatch_id="dispatch:cancelled",
        )
        registry.accept(
            "session:target",
            "runtime-turn:cancelled",
            "room-root:cancelled",
        )
        registry.record_cancellation(
            "room-root:cancelled",
            "cancel:cancelled",
        )
        aborted = AgentEventEnvelope(
            event_id="event:cancelled:aborted",
            session_id="session:target",
            turn_id="runtime-turn:cancelled",
            sequence=2,
            created_at_ms=2,
            event_type="turn_completed",
            payload={"status": "aborted", "aborted": True},
            resume_token="event:cancelled:aborted",
        )

        self.assertEqual(
            registry.claim_cancelled_terminal(aborted),
            ("room-root:cancelled", "cancel:cancelled"),
        )
        self.assertIsNone(
            registry.claim_cancelled_terminal(aborted)
        )
        self.assertFalse(
            registry.mark_cancelled_terminal(
                "session:target",
                "room-root:cancelled",
            )
        )

        late_completed = AgentEventEnvelope(
            event_id="event:cancelled:completed",
            session_id="session:target",
            turn_id="runtime-turn:cancelled",
            sequence=3,
            created_at_ms=3,
            event_type="turn_completed",
            payload={"status": "completed"},
            resume_token="event:cancelled:completed",
        )
        self.assertIsNone(
            registry.claim_cancelled_terminal(late_completed)
        )

    def test_synthetic_abort_terminal_reservation_is_exactly_once(self) -> None:
        registry = RoomTurnRegistry()
        registry.begin("session:target", "room-root:cancelled")
        registry.record_cancellation("room-root:cancelled", "cancel:cancelled")

        self.assertTrue(
            registry.mark_cancelled_terminal(
                "session:target",
                "room-root:cancelled",
            )
        )
        self.assertFalse(
            registry.mark_cancelled_terminal(
                "session:target",
                "room-root:cancelled",
            )
        )

    def test_cancelled_root_cannot_begin_a_late_wake_turn(self) -> None:
        registry = RoomTurnRegistry()
        registry.record_cancellation(
            "room-root:cancelled-before-wake",
            "cancel:before-wake",
        )

        with self.assertRaisesRegex(ValueError, "cancelled"):
            registry.begin(
                "session:facilitator",
                "room-root:cancelled-before-wake",
                dispatch_id="room-wake:late",
            )

        self.assertEqual(
            registry.cancelled_root_by_session["session:facilitator"],
            "room-root:cancelled-before-wake",
        )


class TurnTargetSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = RoomTurnRegistry()

    def test_pending_come_first_with_dispatch_then_registered_without(self) -> None:
        self.registry.begin("s-pending", "turn-1", dispatch_id="d-1")
        self.registry.begin("s-reg", "turn-1", dispatch_id="d-2")
        self.registry.accept("s-reg", "st-9", "turn-1")
        self.registry.begin("s-other", "turn-2", dispatch_id="d-3")

        rows = self.registry.turn_targets(
            "turn-1",
            resolve=lambda session_id: f"participant:{session_id}",
        )
        self.assertEqual(
            rows,
            (
                ("participant:s-pending", "s-pending", "d-1"),
                # Registered session-turns historically reported no dispatch
                # id in this traversal, even though one exists.
                ("participant:s-reg", "s-reg", ""),
            ),
        )

    def test_resolve_none_drops_the_entry(self) -> None:
        self.registry.begin("gone", "turn-1")
        self.registry.begin("kept", "turn-1")
        rows = self.registry.turn_targets(
            "turn-1",
            resolve=lambda session_id: None if session_id == "gone" else session_id,
        )
        self.assertEqual(rows, (("kept", "kept", ""),))

    def test_resolve_runs_inside_the_registry_lock(self) -> None:
        self.registry.begin("s1", "turn-1")
        held: list[bool] = []

        def _resolve(session_id: str) -> str:
            got = self.registry.lock.acquire(blocking=False)
            if got:
                # RLock: re-acquiring from the owning thread succeeds, which
                # is itself the proof the lock is already held by us.
                self.registry.lock.release()
            held.append(got)
            return session_id

        self.registry.turn_targets("turn-1", resolve=_resolve)
        self.assertEqual(held, [True])


class PrivateIntercomTurnTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = RoomTurnRegistry()

    @staticmethod
    def _event(event_type: str) -> AgentEventEnvelope:
        return AgentEventEnvelope(
            event_id=f"event:{event_type}",
            session_id="session:target",
            turn_id="turn:private-notice",
            sequence=1,
            created_at_ms=1,
            event_type=event_type,
            payload={},
            resume_token=f"event:{event_type}",
        )

    def test_private_notice_binds_first_event_and_releases_on_terminal(
        self,
    ) -> None:
        self.registry.begin_private_intercom(
            "session:target",
            "intercom:notice",
        )
        self.assertTrue(
            self.registry.session_turn_active("session:target")
        )

        first = self._event("text_delta")
        self.assertTrue(self.registry.allows_room_event(first))
        self.assertEqual(
            self.registry.private_intercom_for_event(first),
            "intercom:notice",
        )
        self.registry.accept_private_intercom(
            "session:target",
            "turn:private-notice",
            "intercom:notice",
        )
        self.assertEqual(
            self.registry.private_intercom_for_event(
                self._event("turn_completed")
            ),
            "intercom:notice",
        )
        self.assertTrue(
            self.registry.allows_room_event(
                self._event("turn_completed")
            )
        )
        self.registry.finish_private_intercom_event(
            self._event("turn_completed")
        )
        self.assertFalse(
            self.registry.session_turn_active("session:target")
        )

    def test_failed_delivery_releases_pending_private_notice(self) -> None:
        self.registry.begin_private_intercom(
            "session:target",
            "intercom:failed",
        )
        self.registry.abandon_private_intercom(
            "session:target",
            "intercom:failed",
        )
        self.assertFalse(
            self.registry.session_turn_active("session:target")
        )
        self.assertEqual(
            self.registry.private_intercom_for_event(
                self._event("turn_completed")
            ),
            "",
        )


class CancellationReceiptHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = RoomTurnRegistry()

    def test_evicts_the_oldest_only_after_2048_entries(self) -> None:
        for index in range(2048):
            self.registry.record_cancellation(f"turn-{index}", f"receipt-{index}")
        self.assertEqual(len(self.registry.cancelled_turns), 2048)
        self.assertIn("turn-0", self.registry.cancelled_turns)

        self.registry.record_cancellation("turn-2048", "receipt-2048")
        self.assertEqual(len(self.registry.cancelled_turns), 2048)
        self.assertNotIn("turn-0", self.registry.cancelled_turns)
        self.assertIn("turn-1", self.registry.cancelled_turns)
        self.assertEqual(
            self.registry.cancelled_turns["turn-2048"],
            "receipt-2048",
        )

    def test_recancelling_overwrites_without_refreshing_position(self) -> None:
        """dict assignment keeps first-insert order; the inline code relied
        on that, so a re-cancelled turn is still evicted at its original
        seniority."""

        self.registry.record_cancellation("turn-a", "receipt-1")
        self.registry.record_cancellation("turn-b", "receipt-2")
        self.registry.record_cancellation("turn-a", "receipt-3")
        self.assertEqual(
            list(self.registry.cancelled_turns),
            ["turn-a", "turn-b"],
        )
        self.assertEqual(self.registry.cancelled_turns["turn-a"], "receipt-3")


class AliasReintroductionGateTests(unittest.TestCase):
    def test_production_code_never_touches_the_deleted_aliases(self) -> None:
        """No attribute access on any of the ten deleted alias names.

        AST-based rather than text-based so a mention in a comment or
        docstring does not fail, while `self._room_turn_lock` or
        `host._cancelled_room_turns` anywhere in rag_ime/ does.
        """

        offenders: list[str] = []
        for path in sorted((ROOT / "rag_ime").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr in BANNED_ATTRIBUTES
                ):
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{node.lineno} .{node.attr}"
                    )
        self.assertEqual(
            offenders,
            [],
            "RoomTurnRegistry owns this state; go through its methods",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
