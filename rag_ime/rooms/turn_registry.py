from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Mapping
from threading import RLock

from rag_ime.agent_protocol import AgentEventEnvelope


# Cancelled-turn receipts kept for late event stamping. The bound lives next to
# the dict it bounds; it is a memory cap, not a cancellation policy.
_CANCELLED_TURN_RECEIPT_LIMIT = 2048
_CANCELLED_TERMINAL_LIMIT = 4096
_PRIVATE_INTERCOM_TURN_LIMIT = 2048
_PENDING_ROOM_EVENT_LIMIT = 2048


class RoomSessionBusyError(RuntimeError):
    """One target Session already has priority, a pending turn or a live turn.

    Raised inside the registry lock so the reservation that failed leaves no
    partial state. Carries the Session id; the caller owns the user-facing
    message, which names a Room participant rather than a Session.
    """

    def __init__(self, session_id: str) -> None:
        super().__init__(f"session busy: {session_id}")
        self.session_id = session_id


class RoomTurnRegistry:
    """Own the mapping from private Session turns to public Room roots."""

    def __init__(self) -> None:
        self.lock = RLock()
        self.pending_turn_by_session: dict[str, str] = {}
        self.pending_dispatch_by_session: dict[str, str] = {}
        self.pending_child_dispatch_by_session: set[str] = set()
        self.pending_work_by_session: dict[str, dict[str, object]] = {}
        self.turn_by_session_turn: dict[
            tuple[str, str],
            str,
        ] = {}
        self.dispatch_by_session_turn: dict[
            tuple[str, str],
            str,
        ] = {}
        self.child_dispatch_ids: set[str] = set()
        self.work_by_session_turn: dict[
            tuple[str, str],
            dict[str, object],
        ] = {}
        self.topic_by_room_turn: dict[str, str] = {}
        self.user_priority_sessions: set[str] = set()
        self._priority_reservations: dict[str, object] = {}
        self.cancelled_turns: dict[str, str] = {}
        self.cancelled_root_by_session: dict[str, str] = {}
        self.cancelled_turn_by_session_turn: dict[
            tuple[str, str],
            str,
        ] = {}
        self.cancelled_terminal_by_session_root: dict[
            tuple[str, str],
            str,
        ] = {}
        self.private_intercom_pending_by_session: dict[
            str,
            str,
        ] = {}
        self.private_intercom_by_session_turn: dict[
            tuple[str, str],
            str,
        ] = {}
        self.pending_events_by_session_turn: dict[
            tuple[str, str],
            deque[AgentEventEnvelope],
        ] = {}
        self._pending_event_order: deque[tuple[str, str]] = deque()

    def begin(
        self,
        session_id: str,
        room_turn_id: str,
        topic_id: str = "",
        *,
        dispatch_id: str = "",
        child: bool = False,
        work_item_id: str = "",
        work_item_revision: int = 0,
        attempt_id: str = "",
    ) -> None:
        with self.lock:
            if room_turn_id in self.cancelled_turns:
                self.cancelled_root_by_session[session_id] = room_turn_id
                raise ValueError("Room turn is cancelled")
            self._discard_pending_events_locked(session_id)
            self.cancelled_root_by_session.pop(
                session_id,
                None,
            )
            for key in tuple(
                self.cancelled_turn_by_session_turn
            ):
                if key[0] == session_id:
                    self.cancelled_turn_by_session_turn.pop(
                        key,
                        None,
                    )
            self.pending_turn_by_session[session_id] = (
                room_turn_id
            )
            if dispatch_id:
                self.pending_dispatch_by_session[
                    session_id
                ] = dispatch_id
            if child:
                self.pending_child_dispatch_by_session.add(session_id)
            else:
                self.pending_child_dispatch_by_session.discard(session_id)
            normalized_work_id = str(work_item_id or "").strip()
            if normalized_work_id:
                self.pending_work_by_session[session_id] = {
                    "workItemId": normalized_work_id,
                    "workItemRevision": max(0, int(work_item_revision)),
                    "attemptId": str(attempt_id or dispatch_id or "").strip(),
                }
            else:
                self.pending_work_by_session.pop(session_id, None)
            self.topic_by_room_turn[room_turn_id] = topic_id

    def accept(
        self,
        session_id: str,
        session_turn_id: str,
        room_turn_id: str,
    ) -> tuple[AgentEventEnvelope, ...]:
        if not session_turn_id:
            return ()
        with self.lock:
            if (
                self.pending_turn_by_session.get(session_id)
                != room_turn_id
            ):
                return ()
            self.pending_turn_by_session.pop(session_id, None)
            key = (session_id, session_turn_id)
            self.turn_by_session_turn[key] = room_turn_id
            dispatch_id = (
                self.pending_dispatch_by_session.pop(
                    session_id,
                    "",
                )
            )
            if dispatch_id:
                self.dispatch_by_session_turn[
                    key
                ] = dispatch_id
                if session_id in self.pending_child_dispatch_by_session:
                    self.child_dispatch_ids.add(dispatch_id)
            work_identity = self.pending_work_by_session.pop(session_id, None)
            if work_identity:
                self.work_by_session_turn[key] = work_identity
            self.pending_child_dispatch_by_session.discard(session_id)
            return self._discard_pending_events_locked(
                session_id,
                accepted_turn_id=session_turn_id,
            )

    def cancel(
        self,
        session_id: str,
        room_turn_id: str,
    ) -> None:
        with self.lock:
            if room_turn_id in self.cancelled_turns:
                self.cancelled_root_by_session[
                    session_id
                ] = room_turn_id
            if (
                self.pending_turn_by_session.get(session_id)
                == room_turn_id
            ):
                self.pending_turn_by_session.pop(
                    session_id,
                    None,
                )
                self.pending_dispatch_by_session.pop(
                    session_id,
                    None,
                )
                self.pending_child_dispatch_by_session.discard(session_id)
                self.pending_work_by_session.pop(session_id, None)
                self._discard_pending_events_locked(session_id)
            for key, value in tuple(
                self.turn_by_session_turn.items()
            ):
                if (
                    key[0] == session_id
                    and value == room_turn_id
                ):
                    if room_turn_id in self.cancelled_turns:
                        self.cancelled_turn_by_session_turn[
                            key
                        ] = room_turn_id
                    self.turn_by_session_turn.pop(key, None)
                    removed_dispatch = self.dispatch_by_session_turn.pop(
                        key,
                        None,
                    )
                    if removed_dispatch:
                        self.child_dispatch_ids.discard(removed_dispatch)
                    self.work_by_session_turn.pop(key, None)
            while (
                len(self.cancelled_turn_by_session_turn)
                > 4_096
            ):
                self.cancelled_turn_by_session_turn.pop(
                    next(
                        iter(
                            self.cancelled_turn_by_session_turn
                        )
                    )
                )
            self._drop_topic_if_idle(room_turn_id)

    def hold_priority(
        self,
        session_ids: Iterable[str],
    ) -> object:
        """Mark Sessions user-priority unconditionally.

        The direct-Agent entry path proves availability separately before
        claiming, so this hold does not re-check; use
        `hold_priority_if_idle` when the check and the hold must be one
        critical section.
        """

        ordered = list(session_ids)
        reservation = object()
        with self.lock:
            self.user_priority_sessions.update(ordered)
            self._priority_reservations.update({session_id: reservation for session_id in ordered})
        return reservation

    def hold_priority_if_idle(
        self,
        session_ids: Iterable[str],
        *,
        ensure_available: Callable[[str], None] | None = None,
    ) -> object:
        """Atomically reserve Sessions that are not already engaged.

        For each Session, in order: run `ensure_available` (inside the lock,
        so caller pre-checks stay atomic with the reservation), then reject
        with `RoomSessionBusyError` if the Session holds priority, a pending
        turn or a registered turn. Only after every Session passes is the
        whole set marked priority -- a failure reserves nothing. The returned
        opaque identity lets delayed cleanup release only this acquisition.
        """

        ordered = list(session_ids)
        reservation = object()
        with self.lock:
            for session_id in ordered:
                if ensure_available is not None:
                    ensure_available(session_id)
                if (
                    session_id in self.user_priority_sessions
                    or session_id in self.pending_turn_by_session
                    or any(
                        key[0] == session_id
                        for key in self.turn_by_session_turn
                    )
                ):
                    raise RoomSessionBusyError(session_id)
            self.user_priority_sessions.update(ordered)
            self._priority_reservations.update({session_id: reservation for session_id in ordered})
        return reservation

    def priority_reservations(self, session_ids: Iterable[str]) -> dict[str, object]:
        """Snapshot claims before cancellation performs fallible/async work."""
        with self.lock:
            return {session_id: self._priority_reservations[session_id] for session_id in session_ids
                    if session_id in self._priority_reservations}

    def release_priority(
        self,
        session_ids: Iterable[str],
        *,
        reservation: object | None = None,
    ) -> None:
        with self.lock:
            for session_id in session_ids:
                if reservation is not None and self._priority_reservations.get(session_id) is not reservation:
                    continue
                self.user_priority_sessions.discard(session_id)
                self._priority_reservations.pop(session_id, None)

    def release_priority_session(
        self,
        session_id: str,
        *,
        reservation: object | None = None,
    ) -> None:
        self.release_priority((session_id,), reservation=reservation)

    def session_turn_active(self, session_id: str) -> bool:
        """Whether a Session has a pending or registered Room turn.

        This is the direct-Agent guard's decision. It deliberately excludes
        the priority set: a Session held for an imminent dispatch has no turn
        yet, and the guard treated it as available.
        """

        with self.lock:
            return (
                session_id in self.pending_turn_by_session
                or session_id
                in self.private_intercom_pending_by_session
                or any(
                    key[0] == session_id
                    for key in self.turn_by_session_turn
                )
                or any(
                    key[0] == session_id
                    for key in self.private_intercom_by_session_turn
                )
            )

    def active_turn(self, session_id: str) -> tuple[str, str]:
        """Return the public Room root and optional dispatch for one Session.

        Pi owns the private turn.  This registry only keeps the small causal
        link needed by Room projection, cancellation and nested Tool Agents.
        """

        with self.lock:
            pending = self.pending_turn_by_session.get(session_id, "")
            if pending:
                return (
                    pending,
                    self.pending_dispatch_by_session.get(session_id, ""),
                )
            for key, room_turn_id in self.turn_by_session_turn.items():
                if key[0] == session_id:
                    return (
                        room_turn_id,
                        self.dispatch_by_session_turn.get(key, ""),
                    )
        return "", ""

    def begin_private_intercom(
        self,
        session_id: str,
        message_id: str,
    ) -> None:
        """Fence one notification-only Provider turn from Room projection."""

        with self.lock:
            self.private_intercom_pending_by_session[
                session_id
            ] = message_id

    def accept_private_intercom(
        self,
        session_id: str,
        session_turn_id: str,
        message_id: str,
    ) -> None:
        """Bind the accepted Pi turn unless its terminal event already arrived."""

        if not session_turn_id:
            return
        key = (session_id, session_turn_id)
        with self.lock:
            if (
                self.private_intercom_by_session_turn.get(key)
                == message_id
            ):
                return
            if (
                self.private_intercom_pending_by_session.get(
                    session_id
                )
                != message_id
            ):
                return
            self.private_intercom_pending_by_session.pop(
                session_id,
                None,
            )
            self.private_intercom_by_session_turn[key] = (
                message_id
            )
            self._bound_private_intercom_turns()

    def abandon_private_intercom(
        self,
        session_id: str,
        message_id: str,
    ) -> None:
        with self.lock:
            if (
                self.private_intercom_pending_by_session.get(
                    session_id
                )
                == message_id
            ):
                self.private_intercom_pending_by_session.pop(
                    session_id,
                    None,
                )

    def private_intercom_for_event(
        self,
        event: AgentEventEnvelope,
    ) -> str:
        """Resolve and bind an event to a notification-only private turn."""

        if not event.turn_id:
            return ""
        key = (event.session_id, event.turn_id)
        with self.lock:
            message_id = (
                self.private_intercom_by_session_turn.get(key)
            )
            if message_id:
                return message_id
            message_id = (
                self.private_intercom_pending_by_session.pop(
                    event.session_id,
                    "",
                )
            )
            if not message_id:
                return ""
            self.private_intercom_by_session_turn[key] = (
                message_id
            )
            self._bound_private_intercom_turns()
            return message_id

    def finish_private_intercom_event(
        self,
        event: AgentEventEnvelope,
    ) -> None:
        if not event.turn_id:
            return
        with self.lock:
            self.private_intercom_by_session_turn.pop(
                (event.session_id, event.turn_id),
                None,
            )

    def _bound_private_intercom_turns(self) -> None:
        while (
            len(self.private_intercom_by_session_turn)
            > _PRIVATE_INTERCOM_TURN_LIMIT
        ):
            self.private_intercom_by_session_turn.pop(
                next(iter(self.private_intercom_by_session_turn))
            )

    def turn_targets(
        self,
        room_turn_id: str,
        *,
        resolve: Callable[[str], str | None],
    ) -> tuple[tuple[str, str, str], ...]:
        """(resolved id, session id, dispatch id) for Sessions bound to a turn.

        Pending Sessions come first, each with its pending dispatch id;
        registered session-turns follow with an empty dispatch id, matching
        the traversal this replaces. `resolve` runs inside the registry lock
        so participant resolution and map traversal remain one critical
        section, exactly as the caller's inline loops were; returning None
        drops the entry.
        """

        results: list[tuple[str, str, str]] = []
        with self.lock:
            for session_id, pending_turn_id in (
                self.pending_turn_by_session.items()
            ):
                if pending_turn_id != room_turn_id:
                    continue
                resolved = resolve(session_id)
                if resolved is None:
                    continue
                results.append(
                    (
                        resolved,
                        session_id,
                        self.pending_dispatch_by_session.get(
                            session_id,
                            "",
                        ),
                    )
                )
            for (session_id, _session_turn_id), bound_turn_id in (
                self.turn_by_session_turn.items()
            ):
                if bound_turn_id != room_turn_id:
                    continue
                resolved = resolve(session_id)
                if resolved is None:
                    continue
                results.append((resolved, session_id, ""))
        return tuple(results)

    def record_cancellation(
        self,
        room_turn_id: str,
        cancellation_receipt_id: str,
    ) -> None:
        """Remember a turn's cancellation receipt, evicting oldest past 2048.

        Same dict semantics as the inline code this replaces: re-cancelling a
        turn overwrites the receipt in place without refreshing its insertion
        position, and eviction starts only once the size exceeds the limit.
        """

        with self.lock:
            self.cancelled_turns[room_turn_id] = (
                cancellation_receipt_id
            )
            while (
                len(self.cancelled_turns)
                > _CANCELLED_TURN_RECEIPT_LIMIT
            ):
                self.cancelled_turns.pop(
                    next(iter(self.cancelled_turns))
                )

    def mark_cancelled_terminal(
        self,
        session_id: str,
        room_turn_id: str,
    ) -> bool:
        """Reserve Room's one public abort terminal.

        Return ``True`` only when the caller won the reservation. Pi may settle
        while the synchronous abort request is still collecting cancellation
        receipts; in that race the runtime event has already claimed and
        published the terminal, so the Room cancellation path must not publish
        a second synthetic terminal.
        """

        with self.lock:
            receipt_id = self.cancelled_turns.get(
                room_turn_id,
                "",
            )
            if not receipt_id:
                return False
            terminal_key = (session_id, room_turn_id)
            if terminal_key in self.cancelled_terminal_by_session_root:
                return False
            self._remember_cancelled_terminal_locked(
                session_id,
                room_turn_id,
                receipt_id,
            )
            return True

    def claim_cancelled_terminal(
        self,
        event: AgentEventEnvelope,
    ) -> tuple[str, str] | None:
        """Claim the one late abort proof allowed through cancellation.

        Cancellation still fences every delta, Tool event, failure and late
        successful completion. The sole exception is Pi's authoritative
        aborted terminal after an earlier Room abort returned pending. That
        event closes the public Room exactly once.
        """

        if (
            event.event_type != "turn_completed"
            or str(event.payload.get("status") or "")
            != "aborted"
            or not event.turn_id
        ):
            return None
        key = (event.session_id, event.turn_id)
        with self.lock:
            room_turn_id = (
                self.turn_by_session_turn.get(key)
                or self.cancelled_turn_by_session_turn.get(key)
                or self.cancelled_root_by_session.get(
                    event.session_id,
                    "",
                )
            )
            if not room_turn_id:
                return None
            receipt_id = self.cancelled_turns.get(
                room_turn_id,
                "",
            )
            if not receipt_id:
                return None
            terminal_key = (
                event.session_id,
                room_turn_id,
            )
            if (
                terminal_key
                in self.cancelled_terminal_by_session_root
            ):
                return None
            self._remember_cancelled_terminal_locked(
                event.session_id,
                room_turn_id,
                receipt_id,
            )
            return room_turn_id, receipt_id

    def _remember_cancelled_terminal_locked(
        self,
        session_id: str,
        room_turn_id: str,
        receipt_id: str,
    ) -> None:
        self.cancelled_terminal_by_session_root[
            (session_id, room_turn_id)
        ] = receipt_id
        while (
            len(self.cancelled_terminal_by_session_root)
            > _CANCELLED_TERMINAL_LIMIT
        ):
            self.cancelled_terminal_by_session_root.pop(
                next(
                    iter(
                        self.cancelled_terminal_by_session_root
                    )
                )
            )

    def allows_room_event(
        self,
        event: AgentEventEnvelope,
    ) -> bool:
        """Route only an explicit Room turn or private intercom turn.

        Runtime events can reach the background projection lane before
        prompt() returns its turn id. While a Root is pending, events are
        retained by runtime turn id instead of guessing which turn owns the
        Root. accept() returns only the accepted turn's buffered sequence and
        discards every older candidate for that Session. An ordinary direct
        Session turn is never a Room event merely because that Session also
        belongs to a Room participant.
        """

        if not event.turn_id:
            return False
        key = (event.session_id, event.turn_id)
        with self.lock:
            # `mirror_to_room()` owns private-intercom suppression and terminal
            # cleanup. Let those events reach it without making them public.
            if (
                key in self.private_intercom_by_session_turn
                or event.session_id
                in self.private_intercom_pending_by_session
            ):
                return True
            if key in self.cancelled_turn_by_session_turn:
                return False
            if self.cancelled_root_by_session.get(event.session_id):
                return False
            if key in self.turn_by_session_turn:
                return True
            if event.session_id in self.pending_turn_by_session:
                self._buffer_pending_event_locked(key, event)
                return False
            return False

    def turn_for_event(
        self,
        event: AgentEventEnvelope,
    ) -> str:
        registered = self.registered_turn_for_event(event)
        return registered or event.turn_id

    def registered_turn_for_event(
        self,
        event: AgentEventEnvelope,
    ) -> str:
        """Return only an explicitly registered public Room conversation."""

        if not event.turn_id:
            return ""
        key = (event.session_id, event.turn_id)
        with self.lock:
            room_turn_id = self.turn_by_session_turn.get(key)
            if room_turn_id:
                return room_turn_id
            cancelled = (
                self.cancelled_turn_by_session_turn.get(key)
            )
            if cancelled:
                return cancelled
            # A pending Root has no authoritative runtime turn id yet. Binding
            # the first observed event would let a queued event from the prior
            # Session turn claim the new Root and let its terminal clear it.
            # accept() is the sole operation that establishes this mapping.
            cancelled_pending = (
                self.cancelled_root_by_session.get(
                    event.session_id
                )
            )
            if cancelled_pending:
                self.cancelled_turn_by_session_turn[
                    key
                ] = cancelled_pending
                return cancelled_pending
        return ""

    def dispatch_for_event(
        self,
        event: AgentEventEnvelope,
    ) -> str:
        if not event.turn_id:
            return ""
        key = (event.session_id, event.turn_id)
        with self.lock:
            dispatch_id = self.dispatch_by_session_turn.get(key)
            if dispatch_id:
                return dispatch_id
            return ""

    def work_identity_for_event(
        self,
        event: AgentEventEnvelope,
    ) -> dict[str, object]:
        """Return the immutable WorkItem fence captured for this Pi turn."""

        if not event.turn_id:
            return {}
        key = (event.session_id, event.turn_id)
        with self.lock:
            value = self.work_by_session_turn.get(key)
            return dict(value) if isinstance(value, Mapping) else {}

    def child_for_event(self, event: AgentEventEnvelope) -> bool:
        dispatch_id = self.dispatch_for_event(event)
        with self.lock:
            return bool(
                dispatch_id
                and dispatch_id in self.child_dispatch_ids
            )

    def finish_exact_dispatch(
        self, session_id: str, session_turn_id: str, room_turn_id: str, dispatch_id: str,
    ) -> bool:
        """Retire a proven drained projection without touching later Session work."""
        key = (session_id, session_turn_id)
        with self.lock:
            if (self.turn_by_session_turn.get(key) != room_turn_id
                or self.dispatch_by_session_turn.get(key) != dispatch_id):
                return False
            self.turn_by_session_turn.pop(key, None)
            self.dispatch_by_session_turn.pop(key, None)
            self.work_by_session_turn.pop(key, None)
            self.child_dispatch_ids.discard(dispatch_id)
            if (self.pending_turn_by_session.get(session_id) == room_turn_id
                and self.pending_dispatch_by_session.get(session_id) == dispatch_id):
                self.pending_turn_by_session.pop(session_id, None)
                self.pending_dispatch_by_session.pop(session_id, None)
                self.pending_child_dispatch_by_session.discard(session_id)
                self.pending_work_by_session.pop(session_id, None)
            self.pending_events_by_session_turn.pop(key, None)
            self._pending_event_order = deque(item for item in self._pending_event_order if item != key)
            self._drop_topic_if_idle(room_turn_id)
            return True

    def finish(
        self,
        session_id: str,
        session_turn_id: str,
        room_turn_id: str,
    ) -> None:
        with self.lock:
            key = (session_id, session_turn_id)
            self.turn_by_session_turn.pop(key, None)
            removed_dispatch = self.dispatch_by_session_turn.pop(key, None)
            self.work_by_session_turn.pop(key, None)
            if removed_dispatch:
                self.child_dispatch_ids.discard(removed_dispatch)
            if (
                self.pending_turn_by_session.get(session_id)
                == room_turn_id
            ):
                self.pending_turn_by_session.pop(
                    session_id,
                    None,
                )
                self.pending_dispatch_by_session.pop(
                    session_id,
                    None,
                )
                self.pending_child_dispatch_by_session.discard(session_id)
                self.pending_work_by_session.pop(session_id, None)
            self._discard_pending_events_locked(session_id)
            self._drop_topic_if_idle(room_turn_id)

    def is_cancelled(
        self,
        session_id: str,
        room_turn_id: str,
    ) -> bool:
        with self.lock:
            return (
                room_turn_id in self.cancelled_turns
                or self.cancelled_root_by_session.get(
                    session_id
                )
                == room_turn_id
            )

    def topic_for_turn(self, room_turn_id: str) -> str:
        with self.lock:
            return self.topic_by_room_turn.get(
                room_turn_id,
                "",
            )

    def drop_topic_if_idle(self, room_turn_id: str) -> None:
        with self.lock:
            self._drop_topic_if_idle(room_turn_id)

    def _buffer_pending_event_locked(
        self,
        key: tuple[str, str],
        event: AgentEventEnvelope,
    ) -> None:
        bucket = self.pending_events_by_session_turn.setdefault(
            key,
            deque(),
        )
        bucket.append(event)
        self._pending_event_order.append(key)
        while len(self._pending_event_order) > _PENDING_ROOM_EVENT_LIMIT:
            oldest_key = self._pending_event_order.popleft()
            oldest_bucket = self.pending_events_by_session_turn.get(
                oldest_key
            )
            if not oldest_bucket:
                continue
            oldest_bucket.popleft()
            if not oldest_bucket:
                self.pending_events_by_session_turn.pop(
                    oldest_key,
                    None,
                )

    def _discard_pending_events_locked(
        self,
        session_id: str,
        *,
        accepted_turn_id: str = "",
    ) -> tuple[AgentEventEnvelope, ...]:
        accepted = tuple(
            self.pending_events_by_session_turn.get(
                (session_id, accepted_turn_id),
                (),
            )
        )
        for key in tuple(self.pending_events_by_session_turn):
            if key[0] == session_id:
                self.pending_events_by_session_turn.pop(key, None)
        self._pending_event_order = deque(
            key
            for key in self._pending_event_order
            if key[0] != session_id
        )
        return accepted

    def _drop_topic_if_idle(
        self,
        room_turn_id: str,
    ) -> None:
        if (
            room_turn_id
            in self.pending_turn_by_session.values()
        ):
            return
        if (
            room_turn_id
            in self.turn_by_session_turn.values()
        ):
            return
        self.topic_by_room_turn.pop(room_turn_id, None)
