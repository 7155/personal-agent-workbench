from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeAlias

from .agent_blocks import MAX_BLOCKS_PER_TURN, normalize_trusted_agent_blocks
from .agent_protocol import AgentEventEnvelope
from .agent_tool_artifacts import managed_file_block
from .contracts.json_schema import validate_contract

RoomPublicMessageProvider: TypeAlias = Callable[
    [str],
    Mapping[str, object] | None,
]

_ROOM_CONTEXT_OPEN = "<room-context>"
_ROOM_CONTEXT_CLOSE = "</room-context>"
_TRANSIENT_CONTEXT_PREFIX = "RAG_IME_TRANSIENT_CONTEXT_V1\n"
_RECENT_LIVE_EVENT_LIMIT = 256
_RECENT_ROOM_MESSAGE_LIMIT = 12
_ROOM_USER_TRANSCRIPT_MATCH_WINDOW_MS = 30_000
_HISTORICAL_HTML_LINK = re.compile(
    r"\[[^\]\n]{0,240}\]\((?:\./)?(?P<name>[^/\\?#)\s]{1,240}\.html?)(?:#[^)\s]*)?\)",
    re.IGNORECASE,
)


def _recover_managed_html_links(
    session_id: str,
    messages: Sequence[Mapping[str, object]],
    *,
    media: Any,
) -> list[dict[str, object]]:
    """Restore old relative HTML links from Session-owned managed receipts.

    Older Pi replies sometimes named a generated HTML file using a relative
    Markdown link but predated the managed-file block projection. Recovery is
    deliberately narrow: assistant Markdown, a basename-only `.htm[l]` target,
    and an exact same-Session `tool_result` receipt must all agree.
    """

    copied = [dict(message) for message in messages]
    requested_names: set[str] = set()
    names_by_message: dict[str, list[str]] = {}
    for message in copied:
        if str(message.get("role") or "") != "assistant":
            continue
        message_id = str(message.get("id") or "").strip()
        if not message_id:
            continue
        names: list[str] = []
        for block in message.get("blocks", []):
            if not isinstance(block, Mapping) or block.get("type") != "text":
                continue
            data = block.get("data")
            if not isinstance(data, Mapping):
                continue
            value = str(data.get("text") or data.get("markdown") or "")
            for match in _HISTORICAL_HTML_LINK.finditer(value):
                name = match.group("name")
                if name not in names:
                    names.append(name)
        if names:
            names_by_message[message_id] = names
            requested_names.update(names)
    if not requested_names:
        return copied

    try:
        receipts = media.list_for_session(session_id, limit=500)
    except Exception:
        return copied
    receipts_by_name: dict[str, Mapping[str, object]] = {}
    for receipt in receipts:
        if not isinstance(receipt, Mapping):
            continue
        file_name = str(receipt.get("fileName") or "")
        if (
            file_name in requested_names
            and receipt.get("mimeType") == "text/html"
            and receipt.get("origin") == "tool_result"
        ):
            receipts_by_name.setdefault(file_name, receipt)

    for index, message in enumerate(copied):
        message_id = str(message.get("id") or "").strip()
        names = names_by_message.get(message_id, [])
        if not names:
            continue
        blocks = [
            dict(block)
            for block in message.get("blocks", [])
            if isinstance(block, Mapping)
        ]
        existing_media_ids = {
            str(data.get("mediaId") or "")
            for block in blocks
            for data in [block.get("data")]
            if isinstance(data, Mapping)
        }
        remaining = max(0, MAX_BLOCKS_PER_TURN - len(blocks))
        if remaining == 0:
            continue
        raw_blocks: list[dict[str, object]] = []
        for name in names:
            receipt = receipts_by_name.get(name)
            if receipt is None:
                continue
            media_id = str(receipt.get("mediaId") or "")
            if not media_id or media_id in existing_media_ids:
                continue
            raw_blocks.append(managed_file_block(receipt))
            existing_media_ids.add(media_id)
            if len(raw_blocks) >= remaining:
                break
        if not raw_blocks:
            continue
        recovered = normalize_trusted_agent_blocks(
            raw_blocks,
            source_kind="managed_media_recovery",
            source_ref=message_id,
        )
        updated = dict(message)
        updated["blocks"] = [*blocks, *recovered]
        copied[index] = updated
    return copied


class AgentMessageSnapshotService:
    """Project one Session snapshot from Pi, durable rows, and live events."""

    def __init__(
        self,
        *,
        sessions: Any,
        runtime_provider: Callable[[], Any],
        workflow_projector: Callable[[str], Mapping[str, object]],
        agent_blocks: Any,
        media: Any,
        observations: Any,
        events: Any,
        background_jobs: Any,
        room_public_messages: RoomPublicMessageProvider,
        room_recent_public_messages: RoomPublicMessageProvider,
    ) -> None:
        self.sessions = sessions
        self._runtime_provider = runtime_provider
        self._workflow_projector = workflow_projector
        self.agent_blocks = agent_blocks
        self.media = media
        self.observations = observations
        self.events = events
        self.background_jobs = background_jobs
        self._room_public_messages = room_public_messages
        self._room_recent_public_messages = room_recent_public_messages

    @property
    def runtime(self) -> Any:
        """Resolve the current interactive Runtime after policy replacement."""

        return self._runtime_provider()

    def messages(
        self,
        session_id: str,
        *,
        view: str = "",
    ) -> dict[str, object]:
        if view == "recent":
            room_projection = self._room_recent_public_messages(session_id)
            if room_projection is not None:
                return self._recent_room_messages(
                    session_id,
                    room_projection=room_projection,
                )
            return self._recent_session_messages(session_id)
        session = self.sessions.get(session_id)
        last_sequence = self.sessions.max_event_sequence(session_id)
        snapshot_provider = getattr(
            self.runtime,
            "session_snapshot",
            None,
        )
        runtime_snapshot = (
            snapshot_provider(session_id)
            if callable(snapshot_provider)
            else None
        )
        # Inspecting Pi can reconcile an open-but-idle transcript back to idle.
        # Refetch after that boundary so snapshot replay never resurrects a
        # completed turn from the bounded event journal.
        session = self._reconcile_session_status(
            session_id,
            self.sessions.get(session_id),
        )
        messages = (
            list(runtime_snapshot.get("messages") or [])
            if isinstance(runtime_snapshot, Mapping)
            else self.runtime.messages(session_id)
        )
        messages = self.agent_blocks.hydrate_messages(
            session_id,
            [
                message
                for message in messages
                if isinstance(message, Mapping)
            ],
        )
        messages = _recover_managed_html_links(
            session_id,
            messages,
            media=self.media,
        )
        room_projection = self._room_public_messages(session_id)
        if room_projection is not None:
            messages = _project_room_public_messages(
                session_id=session_id,
                private_messages=messages,
                projection=room_projection,
            )
        telemetry = _mapping_field(runtime_snapshot, "telemetry")
        message_queue = _mapping_field(
            runtime_snapshot,
            "messageQueue",
        )
        tool_history_events = (
            list(runtime_snapshot.get("toolHistoryEvents") or [])
            if isinstance(runtime_snapshot, Mapping)
            else []
        )
        try:
            observation_snapshot = self.observations.snapshot(
                {
                    "sessionId": session_id,
                    "category": "tool",
                    "limit": 500,
                }
            )
        except Exception:
            observed_tool_events: list[object] = []
        else:
            observed_tool_events = list(
                observation_snapshot.get("items") or []
            )
        tool_history_events = _apply_observed_times(
            tool_history_events,
            observed_tool_events,
        )
        replayed, _gap = self.events.replay(session_id)
        # Streaming deltas are deliberately not persisted one-by-one, but they
        # still own the live replay cursor while this process is running. Using
        # only SQLite's durable high-water mark dropped every active delta from
        # a refresh until a later terminal event happened to advance it.
        last_sequence = max(
            last_sequence,
            max((event.sequence for event in replayed), default=0),
        )
        replay_events = [
            event.to_payload()
            for event in replayed
            if event.sequence <= last_sequence
        ]
        live_events = _snapshot_live_events(
            tool_history_events,
            replay_events,
            session_active=str(session.get("status") or "idle")
            in {"active", "busy"},
        )
        self._append_pending_approvals(
            session_id=session_id,
            live_events=live_events,
            last_sequence=last_sequence,
        )
        self._append_pending_ui_requests(
            session_id=session_id,
            live_events=live_events,
            last_sequence=last_sequence,
        )
        # Runtime Host message counts describe Provider-loop entries (including
        # tool calls), not the human transcript.  Reconcile only after the
        # visible private and Room-public branches have been projected, so a
        # Session cannot open as an apparently empty conversation.
        if int(session.get("messageCount") or 0) != len(messages):
            session = self.sessions.set_status(
                session_id,
                str(session.get("status") or "idle"),
                message_count=len(messages),
            )
        workflow = self._workflow_projector(session_id)
        background_jobs = self.background_jobs.list(
            session_id,
            limit=100,
        )
        lifecycle_cancellation_audits = (
            self.sessions.lifecycle_cancellation_audits(
                session_id,
                limit=20,
            )
        )
        return {
            "schemaVersion": "rag-ime.agent-message-list.v1",
            "ok": True,
            "sessionId": session_id,
            "items": messages,
            "status": str(session.get("status") or "idle"),
            "liveEvents": live_events,
            "lastSequence": last_sequence,
            "resumeToken": (
                f"{session_id}:{last_sequence}"
                if last_sequence
                else ""
            ),
            "telemetry": telemetry,
            "messageQueue": message_queue,
            "todo": workflow["todo"],
            "goal": workflow["goal"],
            "actGate": workflow["actGate"],
            "backgroundJobs": list(background_jobs.get("items") or []),
            "lifecycleCancellationAudits": lifecycle_cancellation_audits,
        }

    def _recent_room_messages(
        self,
        session_id: str,
        *,
        room_projection: Mapping[str, object],
    ) -> dict[str, object]:
        """Return the durable recent window without restoring Pi Runtime."""

        session = self.sessions.get(session_id)
        last_sequence = self.sessions.max_event_sequence(session_id)
        replayed, _gap = self.events.replay(session_id)
        # Streaming text deltas are intentionally in-memory only.  They still
        # advance the live cursor and must survive a recent-view refresh while
        # Pi is producing the final response.
        last_sequence = max(
            last_sequence,
            max((event.sequence for event in replayed), default=0),
        )
        live_events = _compact_text_delta_events([
            event.to_payload()
            for event in replayed
            if event.sequence <= last_sequence
        ])[-_RECENT_LIVE_EVENT_LIMIT:]
        self._append_pending_approvals(
            session_id=session_id,
            live_events=live_events,
            last_sequence=last_sequence,
        )
        live_events = live_events[-_RECENT_LIVE_EVENT_LIMIT:]
        messages = _recent_room_public_messages(
            _project_room_public_messages(
                session_id=session_id,
                private_messages=[],
                projection=room_projection,
            )
        )
        workflow = self._workflow_projector(session_id)
        background_jobs = self.background_jobs.list(
            session_id,
            limit=100,
        )
        lifecycle_cancellation_audits = (
            self.sessions.lifecycle_cancellation_audits(
                session_id,
                limit=20,
            )
        )
        recent_from_sequence = min(
            (
                int(event.get("sequence") or 0)
                for event in live_events
                if int(event.get("sequence") or 0) > 0
            ),
            default=last_sequence,
        )
        return {
            "schemaVersion": "rag-ime.agent-message-list.v1",
            "ok": True,
            "sessionId": session_id,
            "items": messages,
            "status": str(session.get("status") or "idle"),
            "liveEvents": live_events,
            "lastSequence": last_sequence,
            "resumeToken": (
                f"{session_id}:{last_sequence}"
                if last_sequence
                else ""
            ),
            "telemetry": None,
            "messageQueue": None,
            "todo": workflow["todo"],
            "goal": workflow["goal"],
            "actGate": workflow["actGate"],
            "backgroundJobs": list(background_jobs.get("items") or []),
            "lifecycleCancellationAudits": lifecycle_cancellation_audits,
            "snapshotScope": "recent",
            "partial": True,
            "recentFromSequence": recent_from_sequence,
        }

    def _recent_session_messages(self, session_id: str) -> dict[str, object]:
        """Return a bounded first paint without restoring the Pi transcript."""

        # The recent path deliberately avoids ``session_snapshot`` because a
        # large Pi transcript can make first paint take minutes. Runtime status
        # is only PAW's in-memory control projection, so it is safe to use here
        # to retire a database ``busy`` row after the Host has already become
        # idle. Without this reconciliation a refresh can recreate a permanent
        # running turn even though cancellation already completed.
        session = self._reconcile_session_status(
            session_id,
            self.sessions.get(session_id),
        )
        snapshot_provider = getattr(
            self.runtime,
            "recent_session_snapshot",
            None,
        )
        runtime_snapshot = (
            snapshot_provider(session_id)
            if callable(snapshot_provider)
            else None
        )
        messages = (
            [
                dict(message)
                for message in runtime_snapshot.get("messages") or []
                if isinstance(message, Mapping)
            ]
            if isinstance(runtime_snapshot, Mapping)
            else []
        )
        last_sequence = self.sessions.max_event_sequence(session_id)
        replayed, _gap = self.events.replay(session_id)
        # The durable event journal omits streaming text deltas by design.
        # Use the in-memory replay high-water mark as well, otherwise recent
        # snapshots advertise the old cursor and erase Pi's active output.
        last_sequence = max(
            last_sequence,
            max((event.sequence for event in replayed), default=0),
        )
        replay_events = [
            event.to_payload()
            for event in replayed
            if event.sequence <= last_sequence
        ]
        tool_history_events = (
            [
                dict(event)
                for event in runtime_snapshot.get("toolHistoryEvents") or []
                if isinstance(event, Mapping)
            ]
            if isinstance(runtime_snapshot, Mapping)
            else []
        )
        live_events = (
            _snapshot_live_events(
                tool_history_events,
                replay_events,
                session_active=str(session.get("status") or "idle")
                in {"active", "busy"},
            )
            if tool_history_events
            else _compact_text_delta_events(replay_events)
        )
        self._append_pending_approvals(
            session_id=session_id,
            live_events=live_events,
            last_sequence=last_sequence,
        )
        live_events = live_events[-_RECENT_LIVE_EVENT_LIMIT:]
        workflow = self._workflow_projector(session_id)
        background_jobs = self.background_jobs.list(
            session_id,
            limit=100,
        )
        lifecycle_cancellation_audits = (
            self.sessions.lifecycle_cancellation_audits(
                session_id,
                limit=20,
            )
        )
        recent_from_sequence = min(
            (
                int(event.get("sequence") or 0)
                for event in live_events
                if int(event.get("sequence") or 0) > 0
            ),
            default=last_sequence,
        )
        return {
            "schemaVersion": "rag-ime.agent-message-list.v1",
            "ok": True,
            "sessionId": session_id,
            "items": messages,
            "status": str(session.get("status") or "idle"),
            "liveEvents": live_events,
            "lastSequence": last_sequence,
            "resumeToken": (
                f"{session_id}:{last_sequence}"
                if last_sequence
                else ""
            ),
            "telemetry": None,
            "messageQueue": None,
            "todo": workflow["todo"],
            "goal": workflow["goal"],
            "actGate": workflow["actGate"],
            "backgroundJobs": list(background_jobs.get("items") or []),
            "lifecycleCancellationAudits": lifecycle_cancellation_audits,
            "snapshotScope": "recent",
            "partial": True,
            "runtimeQuiescent": str(session.get("status") or "idle")
            not in {"active", "busy"},
            "recentFromSequence": recent_from_sequence,
        }

    def _append_pending_approvals(
        self,
        *,
        session_id: str,
        live_events: list[dict[str, object]],
        last_sequence: int,
    ) -> None:
        visible_ids = {
            str(event.get("payload", {}).get("approvalId") or "")
            for event in live_events
            if event.get("eventType") == "approval_required"
            and isinstance(event.get("payload"), Mapping)
        }
        approvals = self.sessions.list_approvals(
            session_id=session_id,
            state="pending",
            limit=100,
        )
        for approval in reversed(approvals):
            approval_id = str(approval.get("approvalId") or "")
            if not approval_id or approval_id in visible_ids:
                continue
            event_id = f"{session_id}:snapshot:{approval_id}"
            causal = approval.get("causalMetadata")
            causal_turn_id = (
                str(causal.get("turnId") or "").strip()
                if isinstance(causal, Mapping)
                else ""
            )
            live_events.append(
                AgentEventEnvelope(
                    event_id=event_id,
                    session_id=session_id,
                    turn_id=causal_turn_id or f"approval:{approval_id}",
                    sequence=max(1, last_sequence),
                    created_at_ms=int(
                        approval.get("requestedAtMs") or 0
                    ),
                    event_type="approval_required",
                    payload={
                        **approval,
                        "toolName": str(
                            approval.get("toolId") or ""
                        ),
                        "summary": str(
                            approval.get("operation")
                            or "需要批准的工具操作"
                        ),
                    },
                    resume_token=event_id,
                ).to_payload()
            )

    def _append_pending_ui_requests(
        self,
        *,
        session_id: str,
        live_events: list[dict[str, object]],
        last_sequence: int,
    ) -> None:
        pending_provider = getattr(self.runtime, "pending_ui_requests", None)
        if not callable(pending_provider):
            return
        visible_ids = {
            str(event.get("payload", {}).get("requestId") or "")
            for event in live_events
            if event.get("eventType") == "user_input_required"
            and isinstance(event.get("payload"), Mapping)
        }
        for pending in pending_provider(session_id):
            if not isinstance(pending, Mapping):
                continue
            request_id = str(pending.get("requestId") or "")
            if not request_id or request_id in visible_ids:
                continue
            turn_id = str(pending.get("turnId") or f"ui:{request_id}")
            created_at_ms = int(pending.get("createdAtMs") or 0)
            payload = {
                key: value
                for key, value in pending.items()
                if key not in {"turnId", "createdAtMs"}
            }
            event_id = f"{session_id}:snapshot:ui:{request_id}"
            live_events.append(
                AgentEventEnvelope(
                    event_id=event_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    sequence=max(1, last_sequence),
                    created_at_ms=created_at_ms,
                    event_type="user_input_required",
                    payload=payload,
                    resume_token=event_id,
                ).to_payload()
            )

    def _reconcile_session_status(
        self,
        session_id: str,
        session: Mapping[str, object],
    ) -> dict[str, object]:
        persisted_status = str(session.get("status") or "idle")
        if persisted_status not in {"active", "busy"}:
            return dict(session)
        runtime_status = self.runtime.runtime_status()
        busy_session_ids = {
            str(value)
            for value in runtime_status.get("activeSessionIds") or []
            if str(value)
        }
        active_session_id = str(
            runtime_status.get("activeSessionId") or ""
        )
        if (
            runtime_status.get("status") == "busy"
            and active_session_id
        ):
            busy_session_ids.add(active_session_id)
        effective_status = (
            "busy"
            if session_id in busy_session_ids
            else "idle"
        )
        if effective_status == persisted_status:
            return dict(session)
        return self.sessions.set_status(
            session_id,
            effective_status,
        )


def _project_room_public_messages(
    *,
    session_id: str,
    private_messages: Sequence[Mapping[str, object]],
    projection: Mapping[str, object],
) -> list[dict[str, object]]:
    participant_id = str(projection.get("participantId") or "")
    projected: list[
        tuple[int, int, int, str, dict[str, object]]
    ] = []
    private_user_copies: list[tuple[int, str, int, str, tuple[str, ...]]] = []
    for index, message in enumerate(private_messages):
        if _is_managed_room_bootstrap(message):
            continue
        value = dict(message)
        if str(value.get("role") or "") == "user":
            text = _message_text(value)
            created_at_ms = _message_created_at_ms(value)
            if text and created_at_ms > 0:
                private_user_copies.append(
                    (
                        index,
                        text,
                        created_at_ms,
                        str(value.get("clientMessageId") or ""),
                        _message_attachment_ids(value),
                    )
                )
        projected.append(
            (
                _message_created_at_ms(value),
                1,
                index,
                str(value.get("id") or ""),
                value,
            )
        )

    raw_events = projection.get("events")
    events = (
        raw_events
        if isinstance(raw_events, Sequence)
        and not isinstance(raw_events, (str, bytes))
        else ()
    )

    # A Room user event is a fast public mirror.  Once the target Session has
    # been admitted, the route decision gives us the exact Pi admission id.
    # Pair that binding with the visible input before suppressing the Room
    # mirror: one root may fan out to planning/execute child prompts whose
    # text must remain visible as a separate Pi row. The private transcript
    # keeps the dispatch id, while an exact pair carries the Room alias so a
    # later full snapshot can replace the mirror instead of appending it.
    route_dispatches_by_root: dict[str, set[str]] = {}
    for event in events:
        if not isinstance(event, Mapping):
            continue
        if str(event.get("eventType") or "") != "route_decision":
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        target_session_id = str(
            payload.get("targetSessionId")
            or event.get("sourceSessionId")
            or ""
        )
        target_participant_id = str(
            payload.get("targetParticipantId")
            or event.get("participantId")
            or ""
        )
        dispatch_id = str(payload.get("dispatchId") or "")
        if (
            not dispatch_id
            or target_session_id != session_id
            or (
                target_participant_id
                and participant_id
                and target_participant_id != participant_id
            )
        ):
            continue
        roots = {
            str(event.get("turnId") or ""),
            str(payload.get("rootId") or ""),
        }
        for root in roots:
            if root:
                route_dispatches_by_root.setdefault(root, set()).add(dispatch_id)

    seen_event_ids: set[str] = set()
    seen_post_ids: set[str] = set()
    matched_private_user_indexes: set[int] = set()
    ordered_events = sorted(
        (
            event
            for event in events
            if isinstance(event, Mapping)
        ),
        key=_room_event_order,
    )

    def _dispatch_ids_for_user_event(
        event: Mapping[str, object],
        payload: Mapping[str, object],
    ) -> set[str]:
        dispatch_ids: set[str] = set()
        direct_dispatch_id = str(payload.get("dispatchId") or "")
        if direct_dispatch_id:
            dispatch_ids.add(direct_dispatch_id)
        raw_dispatches = payload.get("dispatches")
        if isinstance(raw_dispatches, Sequence) and not isinstance(
            raw_dispatches,
            (str, bytes),
        ):
            candidates = [
                item
                for item in raw_dispatches
                if isinstance(item, Mapping)
                and (
                    not str(item.get("participantId") or item.get("targetParticipantId") or "")
                    or str(item.get("participantId") or item.get("targetParticipantId") or "")
                    == participant_id
                )
            ]
            for item in candidates:
                dispatch_id = str(item.get("dispatchId") or "")
                if dispatch_id:
                    dispatch_ids.add(dispatch_id)
        for root in (
            str(event.get("turnId") or ""),
            str(payload.get("rootId") or ""),
        ):
            dispatch_ids.update(route_dispatches_by_root.get(root, set()))
        return dispatch_ids

    def _room_private_match(
        *,
        event: Mapping[str, object],
        payload: Mapping[str, object],
        text: str,
        created_at_ms: int,
    ) -> tuple[int | None, str]:
        dispatch_ids = _dispatch_ids_for_user_event(event, payload)
        identity_candidates = [
            (private_index, client_message_id)
            for (
                private_index,
                private_text,
                _private_created_at_ms,
                client_message_id,
                private_attachment_ids,
            )
            in private_user_copies
            if (
                private_index not in matched_private_user_indexes
                and private_text == text
                and _room_attachments_match(payload, private_attachment_ids)
                and client_message_id
                and client_message_id in dispatch_ids
            )
        ]
        if len(identity_candidates) == 1:
            return identity_candidates[0]
        # A route binding disables the legacy text/time fallback.  The exact
        # dispatch still needs the same visible input above: a Room root can
        # fan out to planning/execute child prompts with different text.
        # Preserve both rows until an exact content-and-dispatch pair exists.
        if dispatch_ids:
            return None, ""

        # Legacy Room events may have no route metadata.  Keep the old bounded
        # fallback, but require one unique nearest candidate so a later equal
        # request is never deleted by an earlier Pi row.
        fallback_candidates = sorted(
            (
                (
                    private_index,
                    abs(private_created_at_ms - created_at_ms),
                )
                for (
                    private_index,
                    private_text,
                    private_created_at_ms,
                    _client_message_id,
                    private_attachment_ids,
                )
                in private_user_copies
                if (
                    private_index not in matched_private_user_indexes
                    and private_text == text
                    and _room_attachments_match(payload, private_attachment_ids)
                    and 0 <= private_created_at_ms - created_at_ms
                    <= _ROOM_USER_TRANSCRIPT_MATCH_WINDOW_MS
                )
            ),
            key=lambda candidate: (candidate[1], candidate[0]),
        )
        if not fallback_candidates:
            return None, ""
        if len(fallback_candidates) > 1 and (
            fallback_candidates[0][1] == fallback_candidates[1][1]
        ):
            return None, ""
        private_index = fallback_candidates[0][0]
        client_message_id = next(
            (
                candidate_client_message_id
                for (
                    candidate_index,
                    _candidate_text,
                    _candidate_created_at_ms,
                    candidate_client_message_id,
                    _candidate_attachment_ids,
                )
                in private_user_copies
                if candidate_index == private_index
            ),
            "",
        )
        return private_index, client_message_id

    for event in ordered_events:
        event_type = str(event.get("eventType") or "")
        event_id = str(event.get("eventId") or "")
        payload = event.get("payload")
        if not event_id or not isinstance(payload, Mapping):
            continue
        created_at_ms = _message_created_at_ms(event)
        sequence = _integer(
            event.get("sequence"),
            default=0,
            minimum=0,
            maximum=9_223_372_036_854_775_807,
        )
        turn_id = str(event.get("turnId") or "")
        if event_type == "user_message":
            if event_id in seen_event_ids:
                continue
            text = str(payload.get("text") or "").strip()
            if not text:
                continue
            seen_event_ids.add(event_id)
            matching_private_index, bound_client_message_id = _room_private_match(
                event=event,
                payload=payload,
                text=text,
                created_at_ms=created_at_ms,
            )
            if matching_private_index is not None:
                # The Room event is the fast public mirror of the same input
                # that Pi persists in its transcript a few milliseconds later.
                # Keep Pi's copy in a full snapshot because it owns the
                # history turn shared by the assistant response. Recent Room
                # snapshots still use the event while Pi is being loaded.
                matched_private_user_indexes.add(
                    matching_private_index
                )
                room_client_message_id = str(
                    payload.get("clientMessageId") or ""
                )
                if room_client_message_id or event_id:
                    for projected_index, item in enumerate(projected):
                        if item[2] != matching_private_index:
                            continue
                        projected[projected_index] = (
                            *item[:4],
                            _attach_room_message_alias(
                                item[4],
                                room_client_message_id=room_client_message_id,
                                room_event_id=event_id,
                            ),
                        )
                        break
                continue
            room_client_message_id = str(payload.get("clientMessageId") or "")
            message = _room_text_message(
                session_id=session_id,
                message_id=f"room-event:{event_id}",
                turn_id=turn_id,
                role="user",
                text=text,
                created_at_ms=created_at_ms,
                source_kind="room_event",
                source_ref=event_id,
                client_message_id=bound_client_message_id or room_client_message_id,
                room_client_message_id=room_client_message_id,
                room_event_id=event_id,
            )
            projected.append(
                (
                    created_at_ms,
                    0,
                    sequence,
                    event_id,
                    message,
                )
            )
            continue
        if (
            event_type != "room_post"
            or str(event.get("participantId") or "") != participant_id
        ):
            continue
        post = payload.get("post")
        if not isinstance(post, Mapping):
            continue
        post_id = str(post.get("postId") or "")
        text = str(post.get("content") or "").strip()
        if not post_id or not text or post_id in seen_post_ids:
            continue
        seen_post_ids.add(post_id)
        message = _room_text_message(
            session_id=session_id,
            message_id=f"room-post:{post_id}",
            turn_id=turn_id,
            role="assistant",
            text=text,
            created_at_ms=created_at_ms,
            source_kind="room_post",
            source_ref=post_id,
        )
        projected.append(
            (
                created_at_ms,
                2,
                sequence,
                post_id,
                message,
            )
        )
    projected.sort(key=lambda item: item[:4])
    return [item[4] for item in projected]


def _recent_room_public_messages(
    messages: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    recent = [
        dict(message)
        for message in messages[-_RECENT_ROOM_MESSAGE_LIMIT:]
    ]
    if any(str(message.get("role") or "") == "user" for message in recent):
        return recent
    latest_user = next(
        (
            dict(message)
            for message in reversed(messages)
            if str(message.get("role") or "") == "user"
        ),
        None,
    )
    if latest_user is None:
        return recent
    return [latest_user, *recent[-(_RECENT_ROOM_MESSAGE_LIMIT - 1):]]


def _room_event_order(
    event: Mapping[str, object],
) -> tuple[int, int, str]:
    return (
        _integer(
            event.get("sequence"),
            default=0,
            minimum=0,
            maximum=9_223_372_036_854_775_807,
        ),
        _message_created_at_ms(event),
        str(event.get("eventId") or ""),
    )


def _message_created_at_ms(value: Mapping[str, object]) -> int:
    return _integer(
        value.get("createdAtMs"),
        default=0,
        minimum=0,
        maximum=9_223_372_036_854_775_807,
    )


def _message_text(value: Mapping[str, object]) -> str:
    blocks = value.get("blocks")
    if not isinstance(blocks, Sequence) or isinstance(
        blocks,
        (str, bytes),
    ):
        return ""
    parts: list[str] = []
    for block in blocks:
        if (
            not isinstance(block, Mapping)
            or str(block.get("type") or "") != "text"
        ):
            continue
        data = block.get("data")
        if not isinstance(data, Mapping):
            continue
        text = str(data.get("text") or "").strip()
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _message_attachment_ids(value: Mapping[str, object]) -> tuple[str, ...]:
    raw_attachments = value.get("attachments")
    if not isinstance(raw_attachments, Sequence) or isinstance(
        raw_attachments,
        (str, bytes),
    ):
        return ()
    return tuple(str(item).strip() for item in raw_attachments if str(item).strip())


def _room_attachment_ids(payload: Mapping[str, object]) -> tuple[str, ...] | None:
    if "attachmentReceipts" in payload:
        raw_attachments = payload.get("attachmentReceipts")
    elif "attachmentIds" in payload:
        raw_attachments = payload.get("attachmentIds")
    else:
        return None
    if not isinstance(raw_attachments, Sequence) or isinstance(
        raw_attachments,
        (str, bytes),
    ):
        return ()
    values: list[str] = []
    for item in raw_attachments:
        if isinstance(item, Mapping):
            value = str(item.get("mediaId") or item.get("id") or "").strip()
        else:
            value = str(item).strip()
        if value:
            values.append(value)
    return tuple(values)


def _room_attachments_match(
    payload: Mapping[str, object],
    private_attachment_ids: tuple[str, ...],
) -> bool:
    room_attachment_ids = _room_attachment_ids(payload)
    # A Room event without attachment metadata cannot safely absorb a Pi row
    # carrying attachments: preserving both rows is safer than losing input.
    if room_attachment_ids is None:
        return not private_attachment_ids
    return room_attachment_ids == private_attachment_ids


def _is_managed_room_bootstrap(
    message: Mapping[str, object],
) -> bool:
    if str(message.get("role") or "") != "user":
        return False
    text = _message_text(message)
    return (
        text.startswith(_ROOM_CONTEXT_OPEN)
        and text.endswith(_ROOM_CONTEXT_CLOSE)
    ) or (
        text.startswith(_TRANSIENT_CONTEXT_PREFIX)
        and _ROOM_CONTEXT_OPEN in text
        and _ROOM_CONTEXT_CLOSE in text
    )


def _room_text_message(
    *,
    session_id: str,
    message_id: str,
    turn_id: str,
    role: str,
    text: str,
    created_at_ms: int,
    source_kind: str,
    source_ref: str,
    client_message_id: str = "",
    room_client_message_id: str = "",
    room_event_id: str = "",
) -> dict[str, object]:
    text_data: dict[str, object] = {"text": text}
    if room_client_message_id and room_client_message_id != client_message_id:
        text_data["roomClientMessageId"] = room_client_message_id
    if room_event_id:
        text_data["roomEventId"] = room_event_id
    message: dict[str, object] = {
        "schemaVersion": "rag-ime.agent-message.v1",
        "id": message_id,
        "sessionId": session_id,
        "turnId": turn_id,
        "role": role,
        "status": "completed",
        "blocks": [
            {
                "id": f"{message_id}:text",
                "type": "text",
                "status": "completed",
                "presentationKind": "markdown",
                "data": text_data,
                "source": {
                    "kind": source_kind,
                    "ref": source_ref,
                },
                "visibility": "room_post",
            }
        ],
        "attachments": [],
        "citations": [],
        "createdAtMs": created_at_ms,
        "completedAtMs": created_at_ms,
    }
    if client_message_id:
        message["clientMessageId"] = client_message_id
    validate_contract(message, "agent-message.v1.json")
    return message


def _attach_room_message_alias(
    message: Mapping[str, object],
    *,
    room_client_message_id: str,
    room_event_id: str,
) -> dict[str, object]:
    """Keep the fast Room identity on Pi's canonical transcript row.

    ``clientMessageId`` remains the Session dispatch identity.  The original
    Room id is a display/reconciliation alias and lives in text-block data so
    the frontend can carry it from recent snapshots through full history and
    SSE without changing the public message contract.
    """

    if not room_client_message_id and not room_event_id:
        return dict(message)
    updated = dict(message)
    blocks = [
        dict(block)
        for block in message.get("blocks", [])
        if isinstance(block, Mapping)
    ]
    for index, block in enumerate(blocks):
        if str(block.get("type") or "") != "text":
            continue
        data = block.get("data")
        if not isinstance(data, Mapping):
            data = {}
        updated_data = dict(data)
        if room_client_message_id:
            updated_data["roomClientMessageId"] = room_client_message_id
        if room_event_id:
            updated_data["roomEventId"] = room_event_id
        block["data"] = updated_data
        blocks[index] = block
        break
    if blocks:
        updated["blocks"] = blocks
    return updated


def _mapping_field(
    value: object,
    key: str,
) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    field = value.get(key)
    return dict(field) if isinstance(field, Mapping) else None


def _merge_tool_events(
    tool_history_events: Sequence[object],
    replay_events: Sequence[object],
) -> list[dict[str, object]]:
    history = [
        dict(item)
        for item in tool_history_events
        if isinstance(item, Mapping)
    ]
    replay = [
        dict(item)
        for item in replay_events
        if isinstance(item, Mapping)
    ]
    history_types = _tool_event_types(history)
    replay_types = _tool_event_types(replay)
    stale_replay_ids = {
        tool_call_id
        for tool_call_id, event_types in history_types.items()
        if "tool_finished" in event_types
        and "tool_finished"
        not in replay_types.get(tool_call_id, set())
    }
    replay_reasoning_ids = {
        identity
        for event in replay
        for identity in [_reasoning_event_identity(event)]
        if identity
    }
    merged: list[dict[str, object]] = []
    for event in history:
        reasoning_id = _reasoning_event_identity(event)
        if reasoning_id and reasoning_id in replay_reasoning_ids:
            continue
        tool_call_id, event_type = _tool_event_identity(event)
        if (
            tool_call_id
            and tool_call_id not in stale_replay_ids
            and event_type
            in replay_types.get(tool_call_id, set())
        ):
            continue
        merged.append(event)
    for event in replay:
        tool_call_id, _event_type = _tool_event_identity(event)
        if tool_call_id and tool_call_id in stale_replay_ids:
            continue
        merged.append(event)
    return merged


def _snapshot_live_events(
    tool_history_events: Sequence[object],
    replay_events: Sequence[object],
    *,
    session_active: bool,
) -> list[dict[str, object]]:
    """Restore durable activities plus only the genuinely live event tail.

    Pi's durable transcript owns completed messages, reasoning summaries and
    Tool receipts. Replaying the old streaming journal as well used to send
    hundreds of settled ``text_delta`` records back to the browser, duplicate
    turns whose live ids differ from history ids, and revive stale spinners.
    """

    replay = [
        dict(item)
        for item in replay_events
        if isinstance(item, Mapping)
    ]
    terminal_turn_ids = {
        str(event.get("turnId") or "")
        for event in replay
        if str(event.get("eventType") or "")
        in {"turn_completed", "turn_failed"}
        and str(event.get("turnId") or "")
    }
    live_tail = [
        event
        for event in replay
        if str(event.get("eventType") or "")
        in {"approval_required", "user_input_required"}
        or (
            session_active
            and (
                not str(event.get("turnId") or "")
                or str(event.get("turnId") or "")
                not in terminal_turn_ids
            )
        )
    ]
    return _compact_text_delta_events(
        _merge_tool_events(tool_history_events, live_tail)
    )


def _compact_text_delta_events(
    events: Sequence[object],
) -> list[dict[str, object]]:
    """Coalesce one active streaming run without changing its final text."""

    compacted: list[dict[str, object]] = []
    for value in events:
        if not isinstance(value, Mapping):
            continue
        event = dict(value)
        if str(event.get("eventType") or "") != "text_delta":
            compacted.append(event)
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            compacted.append(event)
            continue
        previous = compacted[-1] if compacted else None
        previous_payload = (
            previous.get("payload")
            if isinstance(previous, Mapping)
            else None
        )
        same_stream = (
            isinstance(previous, Mapping)
            and str(previous.get("eventType") or "") == "text_delta"
            and isinstance(previous_payload, Mapping)
            and str(previous.get("turnId") or "")
            == str(event.get("turnId") or "")
            and str(previous_payload.get("messageId") or "")
            == str(payload.get("messageId") or "")
            and str(previous_payload.get("blockId") or "")
            == str(payload.get("blockId") or "")
            and previous_payload.get("contentIndex")
            == payload.get("contentIndex")
            and payload.get("replaceBlock") is not True
        )
        if not same_stream:
            compacted.append(event)
            continue
        merged_payload = dict(previous_payload)
        merged_payload["delta"] = (
            str(previous_payload.get("delta") or "")
            + str(payload.get("delta") or "")
        )
        compacted[-1] = {
            **dict(previous),
            "eventId": event.get("eventId"),
            "sequence": event.get("sequence"),
            "createdAtMs": event.get("createdAtMs"),
            "resumeToken": event.get("resumeToken"),
            "payload": merged_payload,
        }
    return compacted


def _apply_observed_times(
    tool_history_events: Sequence[object],
    observations: Sequence[object],
) -> list[dict[str, object]]:
    observed_times: dict[tuple[str, str], list[int]] = {}
    for value in observations:
        observation = (
            value if isinstance(value, Mapping) else {}
        )
        event_type = str(
            observation.get("phase")
            or observation.get("name")
            or ""
        )
        if event_type not in {
            "tool_started",
            "tool_progress",
            "tool_finished",
        }:
            continue
        tool_call_id = _observation_tool_call_id(observation)
        created_at_ms = _integer(
            observation.get("createdAtMs"),
            default=0,
            minimum=0,
            maximum=9_223_372_036_854_775_807,
        )
        if not tool_call_id or created_at_ms <= 0:
            continue
        observed_times.setdefault(
            (tool_call_id, event_type),
            [],
        ).append(created_at_ms)
    queues = {
        key: deque(sorted(values))
        for key, values in observed_times.items()
    }
    corrected: list[dict[str, object]] = []
    for value in tool_history_events:
        event = dict(value) if isinstance(value, Mapping) else {}
        tool_call_id, event_type = _tool_event_identity(event)
        timestamps = queues.get((tool_call_id, event_type))
        if timestamps:
            event["createdAtMs"] = timestamps.popleft()
        corrected.append(event)
    return corrected


def _observation_tool_call_id(
    observation: Mapping[str, object],
) -> str:
    refs = observation.get("refs")
    if not isinstance(refs, list):
        return ""
    for value in refs:
        ref = value if isinstance(value, Mapping) else {}
        if ref.get("kind") == "tool_call":
            return str(ref.get("id") or "")
    return ""


def _tool_event_types(
    events: Sequence[Mapping[str, object]],
) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for event in events:
        tool_call_id, event_type = _tool_event_identity(event)
        if tool_call_id and event_type:
            result.setdefault(tool_call_id, set()).add(event_type)
    return result


def _reasoning_event_identity(event: Mapping[str, object]) -> str:
    if str(event.get("eventType") or "") != "reasoning_summary":
        return ""
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return ""
    return str(payload.get("requestId") or "")


def _tool_event_identity(
    event: Mapping[str, object],
) -> tuple[str, str]:
    event_type = str(event.get("eventType") or "")
    if event_type not in {
        "tool_started",
        "tool_progress",
        "tool_finished",
    }:
        return "", ""
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return "", ""
    return str(payload.get("toolCallId") or ""), event_type


def _integer(
    value: object,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))
