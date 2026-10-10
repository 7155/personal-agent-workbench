from __future__ import annotations

from collections.abc import Callable, Mapping
import json
from typing import Any

from .agent_blocks import bind_block_scope
from .agent_prompt_support import bounded_text
from .agent_protocol import AgentEventEnvelope
from .agent_runtime_failure import provider_auth_failure_message
from rag_ime.pi.public import (
    GROUPED_QUESTIONS_SCHEMA_VERSION,
    grouped_questions_from_wire,
    public_code_tool_activity,
    redact_mapping,
)
from .contracts.json_schema import validate_contract


_TRANSIENT_RUNTIME_EVENT_TYPES = frozenset(
    {
        "text_delta",
        "tool_progress",
    }
)

_ROOM_DELTA_MAX_LATENCY_MS = 100
_ROOM_DELTA_MAX_CHARS = 48
_RUNTIME_CLASSIFICATION_MAX_CHARS = 80


class AgentEventProjectionService:
    """Persist private Agent events and project bounded Room events."""

    def __init__(
        self,
        *,
        sessions: Any,
        rooms: Any,
        agent_blocks: Any,
        observations: Any,
        room_events: Any,
        room_turns: Any,
        append_recent_message: Callable[
            [str, Mapping[str, object]],
            None,
        ],
        record_assistant_evidence: Callable[
            [AgentEventEnvelope],
            Mapping[str, object],
        ],
        notify_intercom: Callable[[], None],
    ) -> None:
        self.sessions = sessions
        self.rooms = rooms
        self.agent_blocks = agent_blocks
        self.observations = observations
        self.room_events = room_events
        self.room_turns = room_turns
        self.append_recent_message = append_recent_message
        self.record_assistant_evidence = (
            record_assistant_evidence
        )
        self.notify_intercom = notify_intercom
        # Room projection is a public UI stream, not the token transport. Pi
        # commonly emits one- or two-character fragments; forwarding every
        # fragment makes the virtual timeline remeasure dozens of times per
        # second and lets deltas evict useful Room facts from the replay window.
        self._room_delta_pending: dict[
            tuple[str, str, str, str, str, int, str],
            dict[str, object],
        ] = {}
        self._room_delta_last_emit_ms: dict[
            tuple[str, str, str, str, str, int, str],
            int,
        ] = {}

    def record(self, event: AgentEventEnvelope) -> None:
        # Token and progress deltas already live in the bounded in-memory SSE
        # replay. Persisting each fragment makes the single Host event thread
        # wait on hundreds of SQLite transactions before agent_settled.
        if event.event_type not in _TRANSIENT_RUNTIME_EVENT_TYPES:
            self.sessions.record_runtime_event(
                event_id=event.event_id,
                session_id=event.session_id,
                turn_id=event.turn_id,
                sequence=event.sequence,
                event_type=event.event_type,
                created_at_ms=event.created_at_ms,
                redacted_summary=_event_summary(event),
                metrics=runtime_event_metrics(event),
            )
        if event.event_type != "message_completed":
            return
        message = event.payload.get("message")
        if (
            isinstance(message, Mapping)
            and self._bind_and_persist_blocks(event, message)
        ):
            self.record_assistant_evidence(event)

    def mirror_to_room(
        self,
        event: AgentEventEnvelope,
        *,
        cancelled_terminal: tuple[str, str] | None = None,
    ) -> None:
        private_intercom_id = (
            self.room_turns.private_intercom_for_event(event)
        )
        if private_intercom_id:
            # The private Session transcript remains authoritative via
            # record(). A notification-only A2A turn must not wake another
            # Intercom delivery or create any public Room projection.
            if event.event_type in {
                "turn_completed",
                "turn_failed",
            }:
                self.room_turns.finish_private_intercom_event(
                    event
                )
            return
        if event.event_type in {
            "turn_completed",
            "turn_failed",
        }:
            self.notify_intercom()
        participant = self.rooms.participant_for_session(
            event.session_id
        )
        if event.event_type not in _TRANSIENT_RUNTIME_EVENT_TYPES:
            self.observations.enqueue_agent_event(
                event,
                room_id=(
                    str(participant.get("roomId") or "")
                    if participant is not None
                    else ""
                ),
            )
        if event.event_type in {
            "provider_request_completed",
            "provider_request_failed",
        }:
            # Provider receipts are Trace telemetry, not a second Room row.
            return
        if participant is None:
            return
        registered_turn_for_event = getattr(
            self.room_turns,
            "registered_turn_for_event",
            None,
        )
        conversation_turn_id = (
            cancelled_terminal[0]
            if cancelled_terminal is not None
            else registered_turn_for_event(event)
            if callable(registered_turn_for_event)
            else ""
        )
        room_turn_id = (
            conversation_turn_id
            or self.room_turns.turn_for_event(event)
        )
        if (
            cancelled_terminal is None
            and self.room_turns.is_cancelled(
                event.session_id,
                room_turn_id,
            )
        ):
            return
        mapped_type, public_data = room_event_projection(
            event
        )
        dispatch_id = self.room_turns.dispatch_for_event(
            event
        )
        work_identity_provider = getattr(
            self.room_turns,
            "work_identity_for_event",
            None,
        )
        work_identity = (
            work_identity_provider(event)
            if callable(work_identity_provider)
            else {}
        )
        if cancelled_terminal is not None:
            public_data = {
                **public_data,
                "status": "aborted",
                "aborted": True,
                "cancellationReceiptId": cancelled_terminal[1],
            }
        child_event = self.room_turns.child_for_event(event)
        if child_event and event.event_type in {
            "turn_completed",
            "turn_failed",
        }:
            status = str(event.payload.get("status") or "")
            phase = (
                "failed"
                if event.event_type == "turn_failed"
                else "aborted"
                if status == "aborted"
                else "completed"
            )
            mapped_type = "participant_activity"
            public_data = {
                **public_data,
                "activityKind": "child",
                "phase": phase,
                "status": status or phase,
            }
        publication: dict[str, object] = dict(
            room_id=str(participant["roomId"]),
            event_type=mapped_type,
            payload={
                "sourceEventId": event.event_id,
                "sourceEventType": event.event_type,
                "data": {
                    **public_data,
                    "rootId": room_turn_id,
                    # The Session turn remains useful for correlation, while
                    # sourceLoopId is the actual card boundary: one Pi
                    # assistant/tool loop per Room card.
                    "sourceTurnId": event.turn_id,
                    **work_identity,
                    **(
                        {
                            "sourceLoopId": bounded_text(
                                event.payload.get("sourceLoopId"),
                                maximum=240,
                            )
                        }
                        if bounded_text(
                            event.payload.get("sourceLoopId"),
                            maximum=240,
                        )
                        else {}
                    ),
                    **(
                        {"dispatchId": dispatch_id}
                        if dispatch_id
                        else {}
                    ),
                },
            },
            turn_id=room_turn_id,
            participant_id=str(participant["id"]),
            source_session_id=event.session_id,
            topic_id=self.room_turns.topic_for_turn(
                room_turn_id
            ),
            created_at_ms=event.created_at_ms,
        )
        if mapped_type == "participant_delta":
            self._publish_room_delta(event, publication)
        else:
            self._flush_room_deltas(str(participant["roomId"]))
            if child_event and event.event_type in {"turn_completed", "turn_failed"}:
                self.room_events.publish_child_terminal(
                    runtime_event_id=event.event_id,
                    dispatch_id=dispatch_id,
                    **publication,
                )
            else:
                self.room_events.publish(**publication)
        if event.event_type in {
            "turn_completed",
            "turn_failed",
        }:
            self.room_turns.finish(
                event.session_id,
                event.turn_id,
                room_turn_id,
            )

    def _publish_room_delta(
        self,
        event: AgentEventEnvelope,
        publication: Mapping[str, object],
    ) -> None:
        payload = publication.get("payload")
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, Mapping):
            self.room_events.publish(**publication)
            return
        room_id = str(publication.get("room_id") or "")
        key = (
            room_id,
            str(publication.get("turn_id") or ""),
            str(publication.get("participant_id") or ""),
            str(data.get("messageId") or ""),
            str(data.get("blockId") or ""),
            int(data.get("contentIndex") or 0),
            str(data.get("dispatchId") or ""),
        )
        # Replacement deltas are state boundaries and must never be folded into
        # preceding append deltas.
        if data.get("replaceBlock") is True or data.get("replaceContent") is True:
            self._flush_room_deltas(room_id)
            self.room_events.publish(**publication)
            self._room_delta_last_emit_ms[key] = event.created_at_ms
            return

        pending_other_key = any(
            candidate[0] == room_id and candidate != key
            for candidate in self._room_delta_pending
        )
        if pending_other_key:
            self._flush_room_deltas(room_id)

        last_emit = self._room_delta_last_emit_ms.get(key)
        if last_emit is None:
            # Preserve immediate first-token feedback, then coalesce the burst.
            self.room_events.publish(**publication)
            self._room_delta_last_emit_ms[key] = event.created_at_ms
            return

        pending = self._room_delta_pending.get(key)
        if pending is None:
            pending = _copy_room_delta_publication(publication)
            self._room_delta_pending[key] = pending
        else:
            pending_payload = pending.get("payload")
            pending_data = (
                pending_payload.get("data")
                if isinstance(pending_payload, dict)
                else None
            )
            if isinstance(pending_data, dict):
                pending_data["delta"] = (
                    str(pending_data.get("delta") or "")
                    + str(data.get("delta") or "")
                )
            if isinstance(pending_payload, dict):
                pending_payload["sourceEventId"] = event.event_id
            pending["created_at_ms"] = event.created_at_ms

        pending_payload = pending.get("payload")
        pending_data = (
            pending_payload.get("data")
            if isinstance(pending_payload, dict)
            else None
        )
        buffered_chars = len(str(pending_data.get("delta") or "")) if isinstance(pending_data, dict) else 0
        if (
            buffered_chars >= _ROOM_DELTA_MAX_CHARS
            or event.created_at_ms - last_emit >= _ROOM_DELTA_MAX_LATENCY_MS
        ):
            self._flush_room_delta_key(key)
            self._room_delta_last_emit_ms[key] = event.created_at_ms

    def _flush_room_delta_key(
        self,
        key: tuple[str, str, str, str, str, int, str],
    ) -> None:
        pending = self._room_delta_pending.pop(key, None)
        if pending is not None:
            self.room_events.publish(**pending)

    def _flush_room_deltas(self, room_id: str) -> None:
        keys = [key for key in self._room_delta_pending if key[0] == room_id]
        for key in keys:
            self._flush_room_delta_key(key)
        for key in [key for key in self._room_delta_last_emit_ms if key[0] == room_id]:
            self._room_delta_last_emit_ms.pop(key, None)

    def _bind_and_persist_blocks(
        self,
        event: AgentEventEnvelope,
        message: Mapping[str, object],
    ) -> bool:
        generation = 0
        native_message_id = event.payload.get("nativeMessageId")
        native_pi_session_id = event.payload.get("nativePiSessionId")
        has_native_identity = isinstance(native_message_id, str) and bool(native_message_id) and isinstance(native_pi_session_id, str) and bool(native_pi_session_id)
        # The live bubble remains Turn-owned. Persist each actual native entry
        # separately: one Turn may complete several assistant messages.
        source_message_id = (
            f"pi:{native_pi_session_id}:{native_message_id}" if has_native_identity
            else str(message.get("id") or event.event_id)
        )
        bound_message = dict(message)
        bound_message["blocks"] = bind_block_scope(
            [
                block
                for block in message.get("blocks", [])
                if isinstance(block, Mapping)
            ],
            session_id=event.session_id,
            message_id=source_message_id,
            generation=generation,
        )
        event.payload["message"] = bound_message
        self.append_recent_message(
            event.session_id,
            bound_message,
        )
        if any(
            isinstance(block, Mapping)
            and block.get("schemaVersion")
            == "rag-ime.agent-block.v1"
            for block in bound_message.get("blocks", [])
        ):
            self.agent_blocks.persist_message(
                {**bound_message, "id": source_message_id},
                root_id="",
                task_id="",
                invocation_id="",
                generation=generation,
                created_at_ms=event.created_at_ms,
                native_message_id=native_message_id if has_native_identity else "",
                native_pi_session_id=native_pi_session_id if has_native_identity else "",
            )
        return True


def _copy_room_delta_publication(
    publication: Mapping[str, object],
) -> dict[str, object]:
    copied = dict(publication)
    payload = publication.get("payload")
    if isinstance(payload, Mapping):
        copied_payload = dict(payload)
        data = payload.get("data")
        if isinstance(data, Mapping):
            copied_payload["data"] = dict(data)
        copied["payload"] = copied_payload
    return copied


def room_event_projection(
    event: AgentEventEnvelope,
) -> tuple[str, dict[str, object]]:
    payload = event.payload
    if event.event_type == "text_delta":
        data: dict[str, object] = {
            "messageId": bounded_text(
                payload.get("messageId"),
                maximum=200,
            ),
            "blockId": bounded_text(
                payload.get("blockId"),
                maximum=240,
            ),
            "contentIndex": _signed_integer(
                payload.get("contentIndex"),
                default=0,
            ),
            "delta": str(
                payload.get("delta") or ""
            )[:32_000],
        }
        for flag in ("replaceBlock", "replaceContent"):
            if isinstance(payload.get(flag), bool):
                data[flag] = bool(payload[flag])
        return "participant_delta", data
    if event.event_type == "message_completed":
        message = _public_room_message(
            payload.get("message")
        )
        if message is not None:
            return "participant_message", {
                "message": message
            }
        return "participant_activity", {
            "status": "message_hidden",
            "summary": "已完成一项内部工具步骤",
        }
    if event.event_type == "turn_completed":
        return "turn_completed", _room_scalar_projection(
            payload,
            ("status", "summary"),
        )
    if event.event_type == "turn_failed":
        return "turn_failed", _public_turn_failure(event)
    data = _room_scalar_projection(
        payload,
        (
            "status",
            "summary",
            "message",
            "label",
            "toolName",
            "displayName",
            "toolCallId",
            "parentToolCallId",
            "callId",
            "approvalId",
            "payloadSha256",
            "requestId",
            "requestKind",
            "runId",
            "state",
            "riskLevel",
            "trigger",
            "sourceRole",
            "toolId",
            "operation",
            "decisionMode",
        ),
    )
    for flag in ("ok", "isError", "due", "recoveredExecutionTerminal"):
        if isinstance(payload.get(flag), bool):
            data[flag] = bool(payload[flag])
    if event.event_type == "reasoning_summary":
        source = bounded_text(payload.get("source"), maximum=80)
        if source:
            data["source"] = source
        items = payload.get("items")
        if isinstance(items, list):
            data["items"] = [
                bounded_text(item, maximum=240)
                for item in items[:12]
                if bounded_text(item, maximum=240)
            ]
    if event.event_type in {"approval_required", "approval_resolved"}:
        if isinstance(payload.get("automatic"), bool):
            data["automatic"] = bool(payload["automatic"])
        model_decision = payload.get("approvalModelDecision")
        if isinstance(model_decision, Mapping):
            public_decision = dict(model_decision)
            try:
                validate_contract(
                    public_decision,
                    "agent-approval-model-decision.v1.json",
                )
            except ValueError:
                pass
            else:
                data["approvalModelDecision"] = public_decision
    if event.event_type == "user_input_required":
        data.update(_room_user_input_disclosure(payload))
    if event.event_type in {
        "tool_started",
        "tool_progress",
        "tool_finished",
    }:
        summary, references = _room_tool_summary(payload)
        if summary:
            data["summary"] = summary
        data.update(references)
        data.update(_room_tool_disclosure(payload, event.event_type))
    return "participant_activity", data


def _public_turn_failure(
    event: AgentEventEnvelope,
    *,
    dispatch_id: str = "",
) -> dict[str, object]:
    reason = bounded_text(
        str(event.payload.get("reason") or ""),
        maximum=120,
    )
    next_step = bounded_text(
        str(event.payload.get("nextStep") or ""),
        maximum=300,
    )
    tool_names = [
        bounded_text(str(name), maximum=80)
        for name in (
            event.payload.get("toolNames")
            if isinstance(event.payload.get("toolNames"), list)
            else []
        )
        if isinstance(name, str)
    ][:16]
    runtime_host_exit = (
        str(event.payload.get("failureKind") or "")
        == "runtime_host_exit"
    )
    no_progress = reason in {
        "all_error_recovery_timeout",
        "consecutive_all_error_turns",
        "repeated_failure_signature",
        "no_progress",
    }
    kind = "runtime" if runtime_host_exit else "provider"
    auth_guidance = provider_auth_failure_message(event.payload.get("error"))
    if auth_guidance:
        reason = "provider_auth_failure"
        next_step = auth_guidance
    summary = auth_guidance or (
        "工具连续失败，已停止本轮以避免继续空转"
        if no_progress
        else "Agent 运行时中断，任务已暂停等待恢复"
        if runtime_host_exit
        else "模型响应失败，任务已暂停等待恢复"
    )
    return {
        "status": (
            "runtime_error"
            if runtime_host_exit
            else (
                "provider_error"
                if dispatch_id
                else "failed"
            )
        ),
        "summary": summary,
        "error": summary,
        **({"reason": reason} if reason else {}),
        **({"nextStep": next_step} if next_step else {}),
        **({"toolNames": tool_names} if tool_names else {}),
        **(
            {"requestId": f"{dispatch_id}:{kind}"}
            if dispatch_id
            else {}
        ),
        "isError": True,
    }




def runtime_event_metrics(
    event: AgentEventEnvelope,
) -> dict[str, object]:
    payload = event.payload
    message = payload.get("message")
    message = message if isinstance(message, Mapping) else {}
    usage = message.get("usage")
    if not isinstance(usage, Mapping):
        usage = payload.get("usage")
    usage = usage if isinstance(usage, Mapping) else {}

    def metric(*keys: str) -> int:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                return max(0, int(value))
        return 0

    metrics: dict[str, object] = {}
    # Append typed outcome to existing metrics; old absent values stay unknown.
    if event.turn_id:
        if event.event_type == "turn_failed":
            metrics["terminalOutcome"] = "failed"
        elif event.event_type == "turn_completed":
            if payload.get("aborted") is True or payload.get("status") == "aborted":
                metrics["terminalOutcome"] = "aborted"
            elif payload.get("status") == "completed" and (payload.get("aborted") is None or payload.get("aborted") is False):
                metrics["terminalOutcome"] = "completed"
    normalized_usage = {
        "inputTokens": metric("inputTokens", "input"),
        "outputTokens": metric("outputTokens", "output"),
        "cacheReadTokens": metric(
            "cacheReadTokens",
            "cacheRead",
        ),
        "cacheWriteTokens": metric(
            "cacheWriteTokens",
            "cacheWrite",
        ),
        "totalTokens": metric("totalTokens", "total"),
    }
    if not normalized_usage["totalTokens"]:
        normalized_usage["totalTokens"] = (
            normalized_usage["inputTokens"]
            + normalized_usage["outputTokens"]
        )
    if any(normalized_usage.values()):
        metrics["usage"] = normalized_usage
    if event.event_type in {"tool_started", "tool_progress", "tool_finished"}:
        if event.event_type == "tool_started":
            metrics["toolCalls"] = 1
        # Persist only opaque Tool identity. Restart reconciliation needs to
        # distinguish a completed effect from another Tool in the same turn;
        # arguments and results remain outside this bounded metrics index.
        tool_call_id = str(payload.get("toolCallId") or "").strip()[:512]
        tool_name = str(payload.get("toolName") or "").strip()[:120]
        if tool_call_id:
            tool_identity: dict[str, object] = {
                "toolCallId": tool_call_id,
            }
            if tool_name:
                tool_identity["toolName"] = tool_name
            parent_tool_call_id = str(
                payload.get("parentToolCallId") or ""
            ).strip()[:512]
            if parent_tool_call_id:
                tool_identity["parentToolCallId"] = parent_tool_call_id
            metrics["toolIdentity"] = tool_identity
    if event.event_type == "user_input_required":
        # Keep only opaque request identity so a later process can reconcile
        # a review whose in-memory pending map was lost. The event payload is
        # still the live UI contract; this bounded index is not user content.
        ui_request: dict[str, object] = {}
        for key in ("requestId", "requestKind", "runId"):
            value = str(payload.get(key) or "").strip()[:240]
            if value:
                ui_request[key] = value
        if ui_request:
            metrics["uiRequest"] = ui_request
    if event.event_type in {"approval_required", "approval_resolved"}:
        # The full approval payload remains live-only, but restart recovery
        # needs one durable opaque identity to prove that an exact terminal
        # was already published.  Without it a process can either duplicate a
        # terminal event or leave a recovered Tool card running forever.
        approval_id = str(payload.get("approvalId") or "").strip()[:240]
        if approval_id:
            approval_identity: dict[str, object] = {
                "approvalId": approval_id,
            }
            state = str(payload.get("state") or "").strip()[:80]
            if state:
                approval_identity["state"] = state
            metrics["approvalIdentity"] = approval_identity
    if event.event_type == "turn_failed":
        failure_kind = _bounded_runtime_classification(
            payload.get("failureKind")
        )
        if failure_kind:
            metrics["failureKind"] = failure_kind
        reason_code = _bounded_runtime_classification(
            payload.get("reasonCode")
        )
        if reason_code:
            metrics["reasonCode"] = reason_code
        for key in ("hadToolActivity", "retryable"):
            value = payload.get(key)
            if isinstance(value, bool):
                metrics[key] = value
        exit_code = payload.get("exitCode")
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            metrics["exitCode"] = max(
                -2_147_483_648,
                min(2_147_483_647, exit_code),
            )
    duration = payload.get("durationMs")
    if (
        isinstance(duration, (int, float))
        and not isinstance(duration, bool)
    ):
        metrics["durationMs"] = max(0, int(duration))
    if (
        event.event_type == "message_completed"
        and str(message.get("role") or "") == "user"
    ):
        client_message_id = str(
            payload.get("clientMessageId")
            or message.get("clientMessageId")
            or ""
        ).strip()[:200]
        if client_message_id:
            acceptance: dict[str, object] = {
                "clientMessageId": client_message_id,
                "messageId": str(
                    message.get("id") or ""
                ).strip()[:200],
                "turnId": str(event.turn_id).strip()[:200],
            }
            retry_of_client_message_id = str(
                payload.get("retryOfClientMessageId")
                or message.get("retryOfClientMessageId")
                or ""
            ).strip()[:200]
            if retry_of_client_message_id:
                acceptance["retryOfClientMessageId"] = (
                    retry_of_client_message_id
                )
            # This is deliberately content-free acceptance evidence. A
            # process can die after Pi accepted a prompt but before the
            # command receipt becomes terminal; these IDs let the next
            # process reconcile that receipt without executing it again.
            metrics["promptAcceptance"] = acceptance
    return metrics


def _event_summary(event: AgentEventEnvelope) -> str:
    return str(
        event.payload.get("status")
        or event.payload.get("error")
        or event.payload.get("toolName")
        or event.payload.get("label")
        or ""
    )


def _bounded_runtime_classification(value: object) -> str:
    """Keep one typed, opaque runtime classification in durable metrics."""

    if not isinstance(value, str):
        return ""
    value = value.strip()
    if (
        not value
        or len(value) > _RUNTIME_CLASSIFICATION_MAX_CHARS
        or not value.isascii()
        or not all(
            character.isalnum() or character in "_-"
            for character in value
        )
    ):
        return ""
    return value


def _public_room_message(
    value: object,
) -> dict[str, object] | None:
    if (
        not isinstance(value, Mapping)
        or str(value.get("role") or "") != "assistant"
    ):
        return None
    raw_blocks = value.get("blocks")
    if not isinstance(raw_blocks, list):
        return None
    allowed = {
        "text",
        "code",
        "reasoning_summary",
        "progress",
        "citation",
        "image",
        "audio",
        "file",
        "sticker",
        "task_plan",
        "diff",
        "approval",
        "error",
        "card",
        "checklist",
        "table",
        "artifact",
        "reference",
        "status",
    }
    blocks = [
        dict(item)
        for item in raw_blocks
        if isinstance(item, Mapping)
        and item.get("type") in allowed
        and (
            item.get("schemaVersion")
            != "rag-ime.agent-block.v1"
            or str(item.get("visibility") or "")
            in {"room_post", "root_post"}
        )
    ]
    if not blocks:
        return None
    public = dict(value)
    public["blocks"] = blocks
    public["attachments"] = [
        str(item)[:240]
        for item in value.get("attachments", [])
        if isinstance(item, str)
    ][:16]
    public["citations"] = [
        str(item)[:240]
        for item in value.get("citations", [])
        if isinstance(item, str)
    ][:32]
    return public




def _room_scalar_projection(
    payload: Mapping[str, object],
    keys: tuple[str, ...],
) -> dict[str, object]:
    projected: dict[str, object] = {}
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        projected[key] = bounded_text(value, maximum=500)
    return projected


_ROOM_TOOL_REQUEST_KEYS = (
    "fileName",
    "path",
    "root",
    "cwd",
    "op",
    "operation",
    "mode",
    "patternKind",
    "server",
    "label",
    "jobId",
    "status",
    "reason",
    "query",
    "pattern",
    "glob",
    "title",
    "newName",
    "offset",
    "limit",
    "context",
    "timeout",
    "line",
    "column",
    "timeoutMs",
    "timeoutSeconds",
    "cursor",
    "limitBytes",
    "command",
)

_ROOM_TOOL_RESULT_KEYS = (
    "outputPreview",
    "outputTruncated",
    "summary",
    "status",
    "message",
    "lineCount",
    "additions",
    "deletions",
    "decisionMode",
    "approvalModelDecision",
    "automatic",
)

_ROOM_TOOL_NAMES = frozenset(
    {
        "room_partner",
    }
)

_ROOM_REQUEST_TEXT_LIMITS = {
    "op": 40,
    "targetParticipantId": 240,
    "task": 1_200,
    "expectedOutput": 1_200,
    "content": 1_200,
}

_ROOM_REQUEST_LIST_KEYS = frozenset(
    {
        "acceptanceCriteria",
    }
)

_ROOM_RESULT_KEYS_BY_TOOL = {
    "room_partner": (
        "operation",
        "roomId",
        "rootId",
        "dispatchId",
        "childDispatchId",
        "participantId",
        "displayName",
        "status",
        "result",
        "partners",
        "published",
        "postId",
        "idempotentReplay",
    ),
}

_ROOM_RESULT_BOOLEAN_KEYS = frozenset(
    {
        "published",
        "idempotentReplay",
    }
)

_ROOM_RESULT_TEXT_LIMITS = {
    "operation": 40,
    "roomId": 240,
    "rootId": 240,
    "dispatchId": 240,
    "childDispatchId": 240,
    "participantId": 240,
    "displayName": 160,
    "status": 160,
    "result": 2_000,
    "postId": 240,
}


def _room_tool_request_projection(
    raw_args: Mapping[str, object],
) -> dict[str, object]:
    projected: dict[str, object] = {}
    for key, maximum in _ROOM_REQUEST_TEXT_LIMITS.items():
        value = raw_args.get(key)
        if not isinstance(value, str):
            continue
        text = _redacted_room_text(value, maximum=maximum)
        if text:
            projected[key] = text
    for key in _ROOM_REQUEST_LIST_KEYS:
        raw_values = raw_args.get(key)
        if not isinstance(raw_values, list):
            continue
        values = [
            text
            for value in raw_values[:32]
            if isinstance(value, str)
            and (text := _redacted_room_text(value, maximum=240))
        ]
        if values:
            projected[key] = values
    timeout_seconds = raw_args.get("timeoutSeconds")
    if isinstance(timeout_seconds, int) and not isinstance(timeout_seconds, bool):
        projected["timeoutSeconds"] = max(5, min(300, timeout_seconds))
    return projected


def _room_tool_result_layers(
    value: object,
) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Mapping):
        return ()
    redacted = redact_mapping(value)
    layers: list[Mapping[str, object]] = []

    def visit(layer: Mapping[str, object], depth: int) -> None:
        if depth > 3:
            return
        layers.append(layer)
        for key in ("details", "result"):
            child = layer.get(key)
            if isinstance(child, Mapping):
                visit(child, depth + 1)

    visit(redacted, 0)
    return tuple(layers)


def _room_tool_result_projection(
    tool_name: str,
    raw_result: object,
) -> dict[str, object]:
    keys = _ROOM_RESULT_KEYS_BY_TOOL.get(
        str(tool_name or "").strip().lower()
    )
    if not keys:
        return {}
    layers = _room_tool_result_layers(raw_result)
    if not layers:
        return {}
    projected: dict[str, object] = {}
    for key in keys:
        values = [layer[key] for layer in layers if key in layer]
        if key == "partners":
            value = next((item for item in values if isinstance(item, list)), None)
            if not isinstance(value, list):
                continue
            projected[key] = [
                {
                    field: bounded
                    for field in (
                        "participantId",
                        "displayName",
                        "collaborationRole",
                        "modelProfile",
                        "thinkingLevel",
                        "status",
                    )
                    if (
                        bounded := _redacted_room_text(
                            item.get(field), maximum=240
                        )
                    )
                }
                for item in value[:16]
                if isinstance(item, Mapping)
            ]
            continue
        if key in _ROOM_RESULT_BOOLEAN_KEYS:
            value = next((item for item in values if isinstance(item, bool)), None)
            if isinstance(value, bool):
                projected[key] = value
            continue
        maximum = _ROOM_RESULT_TEXT_LIMITS.get(key)
        value = next((item for item in values if isinstance(item, str)), None)
        if maximum is None or not isinstance(value, str):
            continue
        text = _redacted_room_text(value, maximum=maximum)
        if text:
            projected[key] = text
    return projected


def _room_user_input_disclosure(
    payload: Mapping[str, object],
) -> dict[str, object]:
    """Expose only the bounded public question contract owned by the runtime."""

    method = bounded_text(payload.get("method"), maximum=40)
    if method not in {"select", "confirm", "input", "editor"}:
        method = ""
    request_kind = bounded_text(
        payload.get("requestKind"),
        maximum=120,
    ) or "user_input_required"
    disclosure: dict[str, object] = {"requestKind": request_kind}
    if method:
        disclosure["method"] = method
    for field, maximum in (
        ("title", 160),
        ("message", 500),
        ("placeholder", 500),
        ("prefill", 4_000),
        ("defaultValue", 4_000),
    ):
        if payload.get(field) is not None:
            disclosure[field] = bounded_text(
                payload.get(field),
                maximum=maximum,
            )
    if request_kind == "grouped_questions":
        raw_questions = payload.get("questions")
        if isinstance(raw_questions, list):
            try:
                questions = grouped_questions_from_wire(
                    json.dumps(
                        {
                            "schemaVersion": GROUPED_QUESTIONS_SCHEMA_VERSION,
                            "questions": raw_questions,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
            except (TypeError, ValueError):
                questions = []
            if questions:
                disclosure["schemaVersion"] = GROUPED_QUESTIONS_SCHEMA_VERSION
                disclosure["questions"] = questions
    else:
        raw_options = payload.get("options")
        if isinstance(raw_options, list):
            disclosure["options"] = [
                option
                for value in raw_options[:100]
                if (
                    option := bounded_text(
                        value,
                        maximum=240,
                    )
                )
            ]
    timeout = payload.get("timeout")
    if (
        isinstance(timeout, (int, float))
        and not isinstance(timeout, bool)
    ):
        disclosure["timeout"] = max(0, int(timeout))
    for field, allowed in (
        (
            "resolutionState",
            frozenset({"resolved", "cancelled"}),
        ),
        (
            "resolutionSource",
            frozenset(
                {
                    "direct_user",
                    "user_cancelled",
                    "timeout",
                    "runtime_cancelled",
                }
            ),
        ),
    ):
        value = bounded_text(
            payload.get(field),
            maximum=40,
        )
        if value in allowed:
            disclosure[field] = value
    return disclosure


def _redacted_room_text(
    value: object,
    *,
    maximum: int,
) -> str:
    redacted = redact_mapping({"value": value}).get("value")
    return bounded_text(redacted, maximum=maximum)


def _room_tool_disclosure(
    payload: Mapping[str, object],
    event_type: str,
) -> dict[str, object]:
    raw_args = (
        payload.get("args")
        if isinstance(payload.get("args"), Mapping)
        else {}
    )
    raw_result = (
        payload.get("partialResult")
        if event_type == "tool_progress"
        else payload.get("result")
    )
    tool_name = str(payload.get("toolName") or "").strip().lower()
    is_room_tool = tool_name in _ROOM_TOOL_NAMES
    public_result = (
        dict(payload["publicResult"])
        if isinstance(payload.get("publicResult"), Mapping)
        else public_code_tool_activity(
            tool_name,
            raw_args,
            raw_result,
        )
    )
    if is_room_tool:
        arguments = _room_tool_request_projection(raw_args)
    elif public_result:
        arguments = {
            key: public_result[key]
            for key in _ROOM_TOOL_REQUEST_KEYS
            if key in public_result
        }
    else:
        redacted_args = redact_mapping(raw_args)
        arguments = {
            key: redacted_args[key]
            for key in _ROOM_TOOL_REQUEST_KEYS
            if key in redacted_args and key != "context"
        }

    disclosure: dict[str, object] = {}
    bounded_arguments = _bounded_room_tool_value(arguments)
    if isinstance(bounded_arguments, Mapping) and bounded_arguments:
        disclosure["arguments"] = bounded_arguments

    if event_type in {"tool_progress", "tool_finished"}:
        if is_room_tool:
            result_source = _room_tool_result_projection(
                tool_name,
                raw_result,
            )
            if not result_source and public_result:
                result_source = _room_tool_result_projection(
                    tool_name,
                    public_result,
                )
        elif public_result:
            result_source = {
                key: public_result[key]
                for key in _ROOM_TOOL_RESULT_KEYS
                if key in public_result
            }
        else:
            redacted_result = (
                redact_mapping(raw_result)
                if isinstance(raw_result, Mapping)
                else {}
            )
            result_source = {
                key: redacted_result[key]
                for key in _ROOM_TOOL_RESULT_KEYS
                if key in redacted_result
            }
        if event_type == "tool_finished":
            outcome = _room_gateway_outcome(raw_result)
            # Keep execution certainty ahead of optional display fields when
            # the bounded Room projection reaches its field-count limit.
            result_source = {
                **outcome,
                **{key: value for key, value in result_source.items() if key not in outcome},
            }
        bounded_result = _bounded_room_tool_value(result_source)
        if isinstance(bounded_result, Mapping) and bounded_result:
            disclosure["result"] = bounded_result
        if bool(payload.get("isError")):
            error = (
                _room_tool_error(public_result)
                or _room_tool_error(
                    redact_mapping(raw_result)
                    if isinstance(raw_result, Mapping)
                    else {},
                )
                or _redacted_room_text(
                    payload.get("error"),
                    maximum=1_000,
                )
            )
            if error:
                disclosure["error"] = error
    return disclosure


def _room_gateway_outcome(raw_result: object) -> dict[str, object]:
    """Carry the bridge's bounded certainty fields, never infer them from output."""

    if not isinstance(raw_result, Mapping):
        return {}
    details = raw_result.get("details")
    source = details if isinstance(details, Mapping) else raw_result
    outcome = source.get("executionOutcome")
    phase = source.get("gatewayRequestPhase")
    if not isinstance(outcome, str) or not isinstance(phase, str) or (outcome, phase) not in {
        ("unknown", "sent"), ("not_started", "queued"), ("applied", "sent"),
    }:
        return {}
    result: dict[str, object] = {"executionOutcome": outcome, "gatewayRequestPhase": phase}
    for key in ("replayAllowed", "retryable", "mutationApplied"):
        if isinstance(source.get(key), bool):
            result[key] = source[key]
    for key in ("errorCode", "gatewayError", "error"):
        if isinstance(source.get(key), str):
            result[key] = _redacted_room_text(source[key], maximum=500)
    timeout = source.get("gatewayTimeoutMs")
    if isinstance(timeout, int) and not isinstance(timeout, bool) and timeout > 0:
        result["gatewayTimeoutMs"] = timeout
    recovery = source.get("gatewayRecovery")
    if isinstance(recovery, Mapping) and isinstance(recovery.get("state"), str) and recovery.get("state") in {
        "recovered", "pending", "not_found", "ambiguous", "unavailable",
    }:
        result["gatewayRecovery"] = {
            "state": recovery["state"],
            **{
                key: _redacted_room_text(recovery[key], maximum=500)
                for key in ("approvalId", "error") if isinstance(recovery.get(key), str)
            },
        }
    return result


def _bounded_room_tool_value(
    value: object,
    *,
    depth: int = 0,
) -> object:
    if depth >= 4:
        return "[TRUNCATED]"
    if isinstance(value, Mapping):
        return {
            bounded_text(key, maximum=120): _bounded_room_tool_value(
                child,
                depth=depth + 1,
            )
            for key, child in list(value.items())[:32]
            if bounded_text(key, maximum=120)
        }
    if isinstance(value, list):
        return [
            _bounded_room_tool_value(item, depth=depth + 1)
            for item in value[:32]
        ]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return bounded_text(value, maximum=2_000)


def _room_tool_error(value: object) -> str:
    if not isinstance(value, Mapping):
        return ""
    for key in ("error", "errorMessage", "reason"):
        error = bounded_text(value.get(key), maximum=1_000)
        if error:
            return error
    content = value.get("content")
    if isinstance(content, list):
        for item in content[:8]:
            if not isinstance(item, Mapping):
                continue
            if bounded_text(item.get("type"), maximum=40) != "text":
                continue
            error = bounded_text(item.get("text"), maximum=1_000)
            if error:
                return error
    for child in value.values():
        error = _room_tool_error(child)
        if error:
            return error
    return ""


def _room_tool_summary(
    payload: Mapping[str, object],
) -> tuple[str, dict[str, object]]:
    raw_result = (
        payload.get("result")
        or payload.get("partialResult")
    )
    result = (
        raw_result
        if isinstance(raw_result, Mapping)
        else {}
    )
    details = (
        result.get("details")
        if isinstance(result.get("details"), Mapping)
        else {}
    )
    domain = (
        details.get("result")
        if isinstance(details.get("result"), Mapping)
        else {}
    )
    summary = bounded_text(
        domain.get("summary")
        or details.get("summary")
        or result.get("summary"),
        maximum=500,
    )
    references: dict[str, list[str]] = {
        "books": [],
        "groups": [],
        "tags": [],
        "recent": [],
    }
    items = (
        domain.get("items")
        if isinstance(domain.get("items"), list)
        else []
    )
    key_for_kind = {
        "book": "books",
        "group": "groups",
        "tag": "tags",
        "source": "recent",
    }
    for item in items[:24]:
        if not isinstance(item, Mapping):
            continue
        key = key_for_kind.get(
            str(item.get("kind") or "")
        )
        if not key:
            continue
        label = bounded_text(
            item.get("title")
            or item.get("label")
            or item.get("text"),
            maximum=80,
        )
        if label and label not in references[key]:
            references[key].append(label)
    return summary, {
        key: values[:6]
        for key, values in references.items()
        if values
    }


def _signed_integer(
    value: object,
    *,
    default: int,
) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
