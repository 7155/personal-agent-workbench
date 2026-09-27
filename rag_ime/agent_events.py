from __future__ import annotations

import queue
import threading
import time
from collections import OrderedDict, defaultdict, deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass

from .agent_protocol import AgentEventEnvelope


@dataclass(frozen=True)
class _ProjectionBarrier:
    completed: threading.Event


_PROJECTION_STOP = object()


class AgentTerminalProjectionDuplicate(RuntimeError):
    """A durable authority already owns this terminal projection."""

    def __init__(
        self,
        *,
        event_id: str,
        sequence: int,
        turn_id: str,
        created_at_ms: int,
    ) -> None:
        super().__init__("agent terminal projection is already recorded")
        self.event_id = str(event_id)
        self.sequence = int(sequence)
        self.turn_id = str(turn_id)
        self.created_at_ms = int(created_at_ms)


class AgentEventHub:
    """Per-session durable-before-live event fan-out with bounded replay."""

    def __init__(
        self,
        *,
        replay_limit: int = 512,
        sequence_loader: Callable[[str], int] | None = None,
        event_recorder: Callable[[AgentEventEnvelope], None] | None = None,
        event_observer: Callable[[AgentEventEnvelope], None] | None = None,
        background_projection: bool = False,
        terminal_cache_limit: int = 2048,
    ) -> None:
        self._lock = threading.RLock()
        self._replay_limit = max(32, int(replay_limit))
        self._sequence_loader = sequence_loader or (lambda _session_id: 0)
        self._event_recorder = event_recorder
        self._observers: set[Callable[[AgentEventEnvelope], None]] = set()
        if event_observer is not None:
            self._observers.add(event_observer)
        self._sequences: dict[str, int] = {}
        self._events: dict[str, deque[AgentEventEnvelope]] = defaultdict(
            lambda: deque(maxlen=self._replay_limit)
        )
        self._terminal_cache_limit = max(1, int(terminal_cache_limit))
        self._approval_event_cache: OrderedDict[
            tuple[str, str, str, str],
            AgentEventEnvelope,
        ] = OrderedDict()
        self._tool_terminal_event_cache: OrderedDict[
            tuple[str, str, str],
            AgentEventEnvelope,
        ] = OrderedDict()
        self._subscribers: dict[str, set[queue.Queue[AgentEventEnvelope]]] = defaultdict(set)
        self._projection_queue: queue.Queue[object] | None = None
        self._projection_thread: threading.Thread | None = None
        if background_projection:
            self._projection_queue = queue.Queue()
            self._projection_thread = threading.Thread(
                target=self._run_projection_lane,
                name="rag-ime-agent-event-projection",
                daemon=True,
            )
            self._projection_thread.start()

    def publish(
        self,
        session_id: str,
        event_type: str,
        payload: Mapping[str, object] | None = None,
        *,
        turn_id: str = "",
        created_at_ms: int | None = None,
    ) -> AgentEventEnvelope:
        with self._lock:
            approval_key = _approval_event_key(
                session_id,
                event_type,
                payload,
            )
            if approval_key is not None:
                existing = self._approval_event_cache.get(approval_key)
                if existing is not None:
                    self._approval_event_cache.move_to_end(approval_key)
                    return existing
            tool_terminal_key = _tool_terminal_event_key(
                session_id,
                event_type,
                payload,
                turn_id=turn_id,
            )
            if tool_terminal_key is not None:
                existing = self._tool_terminal_event_cache.get(
                    tool_terminal_key
                )
                if existing is not None:
                    self._tool_terminal_event_cache.move_to_end(
                        tool_terminal_key
                    )
                    return existing
            try:
                envelope = self._build_event_locked(
                    session_id,
                    event_type,
                    payload,
                    turn_id=turn_id,
                    created_at_ms=created_at_ms,
                )
            except AgentTerminalProjectionDuplicate as duplicate:
                envelope = next(
                    (
                        event
                        for event in self._events.get(session_id, ())
                        if event.event_id == duplicate.event_id
                    ),
                    AgentEventEnvelope(
                        event_id=duplicate.event_id,
                        session_id=session_id,
                        turn_id=duplicate.turn_id,
                        sequence=duplicate.sequence,
                        created_at_ms=duplicate.created_at_ms,
                        event_type=event_type,
                        payload=dict(payload or {}),
                        resume_token=duplicate.event_id,
                    ),
                )
                envelope.to_payload()
                if approval_key is not None:
                    self._remember_terminal_event_locked(
                        self._approval_event_cache,
                        approval_key,
                        envelope,
                    )
                if tool_terminal_key is not None:
                    self._remember_terminal_event_locked(
                        self._tool_terminal_event_cache,
                        tool_terminal_key,
                        envelope,
                    )
                # The original event was already delivered by its owning
                # process. Returning its durable identity is an idempotent
                # acknowledgement, not a second replay/live publication.
                return envelope
            if approval_key is not None:
                self._remember_terminal_event_locked(
                    self._approval_event_cache,
                    approval_key,
                    envelope,
                )
            if tool_terminal_key is not None:
                self._remember_terminal_event_locked(
                    self._tool_terminal_event_cache,
                    tool_terminal_key,
                    envelope,
                )
            subscribers = tuple(self._subscribers.get(session_id, ()))
            observers = tuple(self._observers)
            # Sequence allocation, durable recording, replay insertion and
            # live enqueue are one ordered publication. Releasing the lock
            # before enqueue lets another publisher deliver N+1 before N, or
            # append an old event after invalidate_projection cleared the queue.
            # Queue operations are nonblocking; observers remain outside.
            for subscriber in subscribers:
                try:
                    subscriber.put_nowait(envelope)
                except queue.Full:
                    try:
                        subscriber.get_nowait()
                        subscriber.put_nowait(envelope)
                    except (queue.Empty, queue.Full):
                        pass
        self._project(envelope, observers)
        return envelope

    def add_observer(
        self,
        observer: Callable[[AgentEventEnvelope], None],
    ) -> Callable[[], None]:
        """Register a secondary projection and return an idempotent remover."""

        with self._lock:
            self._observers.add(observer)

        def remove() -> None:
            with self._lock:
                self._observers.discard(observer)

        return remove

    def invalidate_projection(
        self,
        session_id: str,
        *,
        reason: str = "session_rewritten",
    ) -> AgentEventEnvelope:
        """Drop stale replay state and make live clients reload the runtime snapshot."""
        with self._lock:
            envelope = self._build_event_locked(
                session_id,
                "snapshot_required",
                {"reason": str(reason or "session_rewritten")},
                turn_id="",
                created_at_ms=None,
            )
            # The invalidation itself is a live control event. Keeping it in
            # replay would let a reconnect apply the old projection before the
            # authoritative snapshot has replaced it.
            self._events[session_id].clear()
            self._drop_terminal_cache_for_session_locked(session_id)
            subscribers = tuple(self._subscribers.get(session_id, ()))
            for subscriber in subscribers:
                while True:
                    try:
                        subscriber.get_nowait()
                    except queue.Empty:
                        break
                try:
                    subscriber.put_nowait(envelope)
                except queue.Full:
                    pass
            observers = tuple(self._observers)
        self._project(envelope, observers)
        return envelope

    def drop_session(self, session_id: str) -> None:
        """Forget bounded live/replay state after a durable Session delete."""

        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            return
        with self._lock:
            self._events.pop(normalized_session_id, None)
            self._sequences.pop(normalized_session_id, None)
            self._drop_terminal_cache_for_session_locked(
                normalized_session_id
            )

    def _drop_terminal_cache_for_session_locked(
        self,
        session_id: str,
    ) -> None:
        for key in tuple(self._approval_event_cache):
            if key[0] == session_id:
                self._approval_event_cache.pop(key, None)
        for key in tuple(self._tool_terminal_event_cache):
            if key[0] == session_id:
                self._tool_terminal_event_cache.pop(key, None)

    def _remember_terminal_event_locked(
        self,
        cache: OrderedDict[tuple[str, ...], AgentEventEnvelope],
        key: tuple[str, ...],
        envelope: AgentEventEnvelope,
    ) -> None:
        cache[key] = envelope
        cache.move_to_end(key)
        while len(cache) > self._terminal_cache_limit:
            cache.popitem(last=False)

    def flush(self, *, timeout: float = 5.0) -> bool:
        """Wait until every previously published secondary projection finishes."""

        projection_queue = self._projection_queue
        projection_thread = self._projection_thread
        if projection_queue is None or projection_thread is None:
            return True
        completed = threading.Event()
        projection_queue.put(_ProjectionBarrier(completed))
        return completed.wait(max(0.0, float(timeout)))

    def close(self, *, timeout: float = 5.0) -> bool:
        """Drain and stop the optional background projection lane."""

        projection_queue = self._projection_queue
        projection_thread = self._projection_thread
        if projection_queue is None or projection_thread is None:
            return True
        drained = self.flush(timeout=timeout)
        projection_queue.put(_PROJECTION_STOP)
        if projection_thread is not threading.current_thread():
            projection_thread.join(max(0.0, float(timeout)))
        stopped = not projection_thread.is_alive()
        if stopped:
            self._projection_queue = None
            self._projection_thread = None
        return drained and stopped

    def subscribe(
        self,
        session_id: str,
        *,
        after_event_id: str = "",
        heartbeat_seconds: float = 10.0,
    ) -> Iterator[bytes]:
        subscriber: queue.Queue[AgentEventEnvelope] = queue.Queue(maxsize=128)
        with self._lock:
            replay, gap = self._replay_locked(session_id, after_event_id)
            if gap:
                replay = [
                    self._snapshot_required_locked(
                        session_id,
                        reason="event_replay_gap",
                        after_event_id=after_event_id,
                    )
                ]
            self._subscribers[session_id].add(subscriber)
        try:
            yield b": connected\n\n"
            for event in replay:
                yield event.sse()
            while True:
                try:
                    yield subscriber.get(timeout=heartbeat_seconds).sse()
                except queue.Empty:
                    yield b": heartbeat\n\n"
        finally:
            with self._lock:
                self._subscribers[session_id].discard(subscriber)
                if not self._subscribers[session_id]:
                    self._subscribers.pop(session_id, None)

    def replay(self, session_id: str, *, after_event_id: str = "") -> tuple[list[AgentEventEnvelope], bool]:
        with self._lock:
            return self._replay_locked(session_id, after_event_id)

    def subscriber_count(self, session_id: str | None = None) -> int:
        with self._lock:
            if session_id is not None:
                return len(self._subscribers.get(session_id, ()))
            return sum(len(items) for items in self._subscribers.values())

    def _build_event_locked(
        self,
        session_id: str,
        event_type: str,
        payload: Mapping[str, object] | None,
        *,
        turn_id: str,
        created_at_ms: int | None,
    ) -> AgentEventEnvelope:
        if not session_id:
            raise ValueError("agent event sessionId must not be empty")
        if session_id not in self._sequences:
            self._sequences[session_id] = max(0, int(self._sequence_loader(session_id)))
        sequence = self._sequences[session_id] + 1
        event_id = f"{session_id}:{sequence}"
        envelope = AgentEventEnvelope(
            event_id=event_id,
            session_id=session_id,
            turn_id=turn_id,
            sequence=sequence,
            created_at_ms=int(created_at_ms if created_at_ms is not None else time.time() * 1000),
            event_type=event_type,
            payload=dict(payload or {}),
            resume_token=event_id,
        )
        envelope.to_payload()
        if self._event_recorder is not None:
            try:
                self._event_recorder(envelope)
            except Exception:
                # A recorder may fail after committing its event row. Reloading
                # the durable high-water mark prevents the next publish from
                # reusing that event ID while still refusing live delivery.
                self._sequences[session_id] = max(
                    self._sequences[session_id],
                    max(0, int(self._sequence_loader(session_id))),
                )
                raise
        self._sequences[session_id] = sequence
        self._events[session_id].append(envelope)
        return envelope

    def _snapshot_required_locked(
        self,
        session_id: str,
        *,
        reason: str,
        after_event_id: str,
    ) -> AgentEventEnvelope:
        """Build a transient recovery control without moving the durable cursor."""

        current_sequence = self._sequences.get(session_id)
        if current_sequence is None:
            current_sequence = max(0, int(self._sequence_loader(session_id)))
            self._sequences[session_id] = current_sequence
        sequence = max(1, current_sequence + 1)
        event_id = f"{session_id}:snapshot-required:{current_sequence}"
        envelope = AgentEventEnvelope(
            event_id=event_id,
            session_id=session_id,
            turn_id="",
            sequence=sequence,
            created_at_ms=int(time.time() * 1000),
            event_type="snapshot_required",
            payload={
                "reason": str(reason),
                "afterEventId": str(after_event_id),
            },
            resume_token=event_id,
        )
        envelope.to_payload()
        return envelope

    def _project(
        self,
        envelope: AgentEventEnvelope,
        observers: tuple[Callable[[AgentEventEnvelope], None], ...],
    ) -> None:
        projection_queue = self._projection_queue
        if projection_queue is not None:
            projection_queue.put((envelope, observers))
            return
        self._apply_projection(envelope, observers)

    def _run_projection_lane(self) -> None:
        projection_queue = self._projection_queue
        if projection_queue is None:
            return
        while True:
            item = projection_queue.get()
            if item is _PROJECTION_STOP:
                return
            if isinstance(item, _ProjectionBarrier):
                item.completed.set()
                continue
            envelope, observers = item
            self._apply_projection(envelope, observers)

    def _apply_projection(
        self,
        envelope: AgentEventEnvelope,
        observers: tuple[Callable[[AgentEventEnvelope], None], ...],
    ) -> None:
        for observer in observers:
            try:
                observer(envelope)
            except Exception:
                # Room/event projections are secondary indexes. A projection
                # failure must never break the participant's primary Pi turn.
                pass

    def _replay_locked(
        self,
        session_id: str,
        after_event_id: str,
    ) -> tuple[list[AgentEventEnvelope], bool]:
        events = list(self._events.get(session_id, ()))
        if not after_event_id:
            return events, False
        after_sequence = _event_sequence(session_id, after_event_id)
        if after_sequence is None:
            return [], True
        current_sequence = self._sequences.get(session_id)
        if current_sequence is None:
            current_sequence = max(
                0,
                int(self._sequence_loader(session_id)),
            )
            self._sequences[session_id] = current_sequence
        if after_sequence > current_sequence:
            return [], True
        if not events:
            return ([], after_sequence < current_sequence)
        first_sequence = events[0].sequence
        if after_sequence < first_sequence - 1:
            return [], True
        return [
            event
            for event in events
            if event.sequence > after_sequence
        ], False


def _approval_event_key(
    session_id: str,
    event_type: str,
    payload: Mapping[str, object] | None,
) -> tuple[str, str, str, str] | None:
    if event_type not in {"approval_required", "approval_resolved"}:
        return None
    source = payload if isinstance(payload, Mapping) else {}
    approval_id = str(source.get("approvalId") or "").strip()
    if not approval_id:
        return None
    state = "pending" if event_type == "approval_required" else "resolved"
    return (str(session_id), event_type, approval_id, state)


def _tool_terminal_event_key(
    session_id: str,
    event_type: str,
    payload: Mapping[str, object] | None,
    *,
    turn_id: str,
) -> tuple[str, str, str] | None:
    if event_type != "tool_finished":
        return None
    source = payload if isinstance(payload, Mapping) else {}
    tool_call_id = str(source.get("toolCallId") or "").strip()
    normalized_turn_id = str(turn_id or "").strip()
    if not tool_call_id:
        return None
    return (str(session_id), normalized_turn_id, tool_call_id)


def _event_sequence(session_id: str, event_id: str) -> int | None:
    prefix = f"{session_id}:"
    if not event_id.startswith(prefix):
        return None
    try:
        return int(event_id[len(prefix) :])
    except ValueError:
        return None
