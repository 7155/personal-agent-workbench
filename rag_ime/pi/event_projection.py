"""Pure Host event projections; callers retain ordering and terminal authority."""

from __future__ import annotations

import re
from collections.abc import Mapping
from .public import (
    inspectable_tool_result,
    public_durable_tool_outcome,
    public_codemode_nested_calls,
    public_code_tool_activity,
    public_knowledge_tool_activity,
    redact_mapping,
    runtime_tool_result_is_error,
    visible_message_text,
)
from .values import as_integer, as_mapping, redact_runtime_text

__all__ = [
    "codemode_capability",
    "failed_settlement_receipt",
    "runtime_primitive_capabilities",
    "tool_event_payload",
    "text_delta_payload",
]


_CODEMODE_MODES = ("off", "on", "only")


def codemode_capability(value: object) -> dict[str, object]:
    """Normalize the Host's optional native codemode capability disclosure."""

    source = as_mapping(value)
    if isinstance(value, bool):
        available = value
        raw_modes: object = _CODEMODE_MODES if value else []
        raw_default: object = "on"
    else:
        available = source.get("available") is True
        raw_modes = source.get("modes")
        raw_default = source.get("defaultMode")
    modes = [
        mode
        for mode in _CODEMODE_MODES
        if isinstance(raw_modes, (list, tuple)) and mode in raw_modes
    ]
    if available and not modes:
        modes = list(_CODEMODE_MODES)
    default_mode = str(raw_default or "on").strip().lower()
    if default_mode not in modes:
        default_mode = "on" if "on" in modes else (modes[0] if modes else "")
    return {
        "available": available,
        "modes": modes,
        "defaultMode": default_mode,
    }


def failed_settlement_receipt(
    raw: Mapping[str, object],
    *,
    allow_aborted: bool,
) -> tuple[bool, str] | None:
    """Return terminality and error for one failed V2 settlement receipt."""

    receipt = as_mapping(raw.get("receipt"))
    if receipt.get("schemaVersion") != "pi.agent-settled.v2":
        return None
    disposition = str(receipt.get("disposition") or "")
    allowed = {"failed", "aborted"} if allow_aborted else {"failed"}
    if disposition not in allowed:
        return None

    operations = as_mapping(receipt.get("operations"))
    pending_values = []
    if "pendingOperations" in receipt:
        pending_values.append(receipt.get("pendingOperations"))
    if "pending" in operations:
        pending_values.append(operations.get("pending"))
    terminal = bool(pending_values) and all(
        isinstance(value, int) and not isinstance(value, bool) and value == 0
        for value in pending_values
    )
    final_message = as_mapping(receipt.get("finalMessage"))
    error = redact_runtime_text(
        str(
            raw.get("error")
            or final_message.get("errorMessage")
            or receipt.get("stopReason")
            or "Pi settlement failed"
        )
    )
    return terminal, error


def runtime_primitive_capabilities(value: object) -> dict[str, object]:
    source = as_mapping(value)
    operations = as_mapping(source.get("sessionCancelOperations"))
    continuation_envelope = source.get("continuationEnvelope")
    return {
        "sessionExactTurnCancel": source.get("sessionExactTurnCancel") is True,
        "sessionRetiredTurnRecovery": source.get("sessionRetiredTurnRecovery") is True,
        "sessionInterruptedTurnRecovery": source.get("sessionInterruptedTurnRecovery") is True,
        "continuationEnvelope": (
            continuation_envelope if continuation_envelope in {"1", "2"} else ""
        ),
        "cancelScope": (
            str(source.get("cancelScope") or "")
            if source.get("cancelScope") == "1"
            else ""
        ),
        "sessionContinuationQueue": bool(source.get("sessionContinuationQueue")),
        "sessionCancelOperationRegistry": bool(
            source.get("sessionCancelOperationRegistry")
        ),
        "sessionCancelOperations": {
            key: bool(operations.get(key))
            for key in (
                "provider",
                "tool",
                "retrySleep",
                "manualCompaction",
                "autoCompaction",
                "branchSummary",
                "bashProcess",
                "continuationTimer",
            )
        },
    }


def tool_event_payload(
    raw: Mapping[str, object], *, event_type: str, source_loop_id: str,
    durable_context: Mapping[str, str] | None = None,
) -> tuple[str, dict[str, object]]:
    mapped_type = {
        "tool_execution_start": "tool_started",
        "tool_execution_update": "tool_progress",
        "tool_execution_end": "tool_finished",
    }[event_type]
    raw_args = as_mapping(raw.get("args"))
    tool_name = str(raw.get("toolName") or "")
    payload: dict[str, object] = {
        "toolCallId": str(raw.get("toolCallId") or ""),
        "toolName": tool_name,
        "args": redact_mapping(raw_args),
        "isError": bool(raw.get("isError")),
        **({"sourceLoopId": source_loop_id} if source_loop_id else {}),
    }
    parent_tool_call_id = str(raw.get("parentToolCallId") or "").strip()
    if parent_tool_call_id:
        payload["parentToolCallId"] = parent_tool_call_id[:512]
    # Preserve a Host-measured end-to-end duration when available;
    # Observation/Trace must continue to represent missing timing as
    # unavailable rather than deriving it from unrelated timestamps.
    if raw.get("durationMs") is not None:
        payload["durationMs"] = as_integer(raw.get("durationMs"))
    result_key = "partialResult" if event_type == "tool_execution_update" else "result"
    raw_result = raw.get(result_key)
    result_is_error = runtime_tool_result_is_error(
        tool_name,
        raw_result,
        reported_is_error=bool(raw.get("isError")),
    )
    payload["isError"] = result_is_error
    public_result = public_code_tool_activity(
        tool_name,
        raw_args,
        raw_result,
    )
    public_result.update(
        public_knowledge_tool_activity(
            tool_name,
            raw_args,
            raw_result,
        )
    )
    if result_is_error and public_result.get("outputPreview"):
        public_result["error"] = public_result["outputPreview"]
    if public_result:
        payload["publicResult"] = public_result
    if raw_result is not None:
        if event_type == "tool_execution_end":
            payload[result_key] = inspectable_tool_result(raw_result)
            nested_calls = public_codemode_nested_calls(raw_result)
            if nested_calls:
                payload["nestedCalls"] = nested_calls
                nested_receipt = as_mapping(raw_result).get("nestedCalls")
                if isinstance(nested_receipt, Mapping) and isinstance(
                    nested_receipt.get("complete"), bool
                ):
                    payload["nestedCallsComplete"] = nested_receipt["complete"]
        elif not public_result:
            payload[result_key] = redact_mapping(as_mapping(raw_result))
    if event_type == "tool_execution_end" and durable_context:
        outcome = public_durable_tool_outcome(
            raw.get("durableToolOutcome"), **durable_context,
            tool_call_id=str(raw.get("toolCallId") or ""), tool_name=tool_name,
            assistant_message_id=source_loop_id,
        )
        if outcome is not None:
            payload["durableToolOutcome"] = outcome
    return mapped_type, payload


def text_delta_payload(
    raw_message: Mapping[str, object],
    update: Mapping[str, object],
    *,
    turn_id: str,
    replace_block: bool,
    source_loop_id: str,
) -> dict[str, object]:
    delta = str(update.get("delta") or "")
    content_index = as_integer(update.get("contentIndex"))
    content = raw_message.get("content")
    current_text = (
        str(as_mapping(content[content_index]).get("text") or "")
        if isinstance(content, list) and 0 <= content_index < len(content)
        else content
        if isinstance(content, str)
        else ""
    )
    # Pi supplies the cumulative text with every delta. Reproject
    # raw-tag-prefixed blocks, including split opening tags, so an
    # internal preamble cannot flash before message_end filters it.
    replace_content = current_text.lstrip().startswith("<")
    if replace_content:
        delta = visible_message_text("assistant", current_text)
    # Durable assigns one immutable identity per native assistant task. Its
    # completion/transcript use this id too; a turn-wide live alias would leave
    # a second message beside the final receipt, including incomplete streams.
    # Classic retains its turn alias and segment behavior around Tool calls.
    message_id = (
        source_loop_id
        if re.fullmatch(r"durable:task:[1-9][0-9]*:assistant", source_loop_id)
        else f"{turn_id}:assistant"
    )
    return {
        "messageId": message_id,
        "blockId": f"{message_id}:text",
        "contentIndex": content_index,
        "delta": delta,
        **({"replaceContent": True} if replace_content else {}),
        "replaceBlock": replace_block,
        **({"sourceLoopId": source_loop_id} if source_loop_id else {}),
    }
