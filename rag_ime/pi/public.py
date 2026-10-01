"""Pi message shaping and public projection for the Host and transcript readers.

Message identity, visibility, reasoning summaries, redaction and public payloads
have one owner. Callers supply media URL resolution explicitly. The Pi-family
import gate enforces the exported contract in __all__.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Mapping
from typing import cast
from urllib.parse import quote

from rag_ime.agent_blocks import (
    extract_completed_agent_blocks,
    normalize_trusted_agent_blocks,
)
from rag_ime.agent_protocol import AgentBlock, AgentMessage, normalize_agent_block
from rag_ime.agent_runtime_failure import provider_auth_failure_message
from rag_ime.contracts.json_schema import validate_contract

from rag_ime.pi.values import (
    PiRuntimeError,
    as_integer,
    as_mapping,
    redact_runtime_text,
)

__all__ = [
    "APPROVAL_TITLE_PREFIX",
    "GROUPED_QUESTIONS_SCHEMA_VERSION",
    "GROUPED_QUESTIONS_TITLE_PREFIX",
    "REVIEW_TITLE_PREFIX",
    "canonical_grouped_answers",
    "grouped_questions_from_wire",
    "inspectable_tool_result",
    "last_assistant_error",
    "last_assistant_preview",
    "managed_media_content_url",
    "pi_message_id",
    "pi_message_completes_public_turn",
    "pi_message_continues_public_turn",
    "pi_message_is_public",
    "pi_message_payload",
    "provider_request_receipt",
    "provider_retry_status",
    "public_code_tool_activity",
    "public_codemode_nested_calls",
    "public_knowledge_tool_activity",
    "public_reasoning_summaries",
    "public_file_name",
    "public_fork_candidate_text",
    "public_pi_model",
    "public_usage",
    "public_usage_evidence",
    "redact_mapping",
    "runtime_tool_result_is_error",
    "safe_scalar",
    "supported_thinking_levels",
    "ui_confirmation_value",
    "visible_message_text",
]


APPROVAL_TITLE_PREFIX = "RAG-IME-APPROVAL:"


REVIEW_TITLE_PREFIX = "RAG-IME-REVIEW:"

GROUPED_QUESTIONS_TITLE_PREFIX = "RAG-IME-QUESTIONS:"


GROUPED_QUESTIONS_SCHEMA_VERSION = "rag-ime.grouped-questions.v2"


_GROUPED_QUESTION_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,79}$")
_GROUPED_CUSTOM_OPTION_LABELS = frozenset({"other", "其他", "其它", "自定义"})
_GROUPED_QUESTION_KEYS = frozenset(
    {"id", "question", "header", "options", "multi", "recommended"}
)
_GROUPED_OPTION_KEYS = frozenset({"label", "description", "preview"})
_GROUPED_ANSWER_KEYS = frozenset({"selected", "custom"})


def _bounded_grouped_string(
    value: object,
    *,
    field: str,
    maximum: int,
    required: bool = False,
) -> str:
    if not isinstance(value, str):
        if required:
            raise ValueError(f"grouped {field} must be a string")
        return ""
    normalized = value.strip()
    if required and not normalized:
        raise ValueError(f"grouped {field} must not be empty")
    if len(normalized) > maximum:
        raise ValueError(f"grouped {field} is too long")
    return normalized


def _grouped_option_label_is_custom(label: str) -> bool:
    return label.casefold() in _GROUPED_CUSTOM_OPTION_LABELS


def _canonical_grouped_question(
    value: object,
    *,
    seen_ids: set[str],
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("grouped question must be an object")
    if set(value) - _GROUPED_QUESTION_KEYS:
        raise ValueError("grouped question contains unsupported fields")
    question_id = _bounded_grouped_string(
        value.get("id"),
        field="question id",
        maximum=80,
        required=True,
    )
    if _GROUPED_QUESTION_ID_RE.fullmatch(question_id) is None:
        raise ValueError("grouped question id is invalid")
    if question_id in seen_ids:
        raise ValueError("grouped question ids must be unique")
    question = _bounded_grouped_string(
        value.get("question"),
        field="question text",
        maximum=160,
        required=True,
    )
    raw_header = value.get("header")
    header = (
        _bounded_grouped_string(
            raw_header,
            field="question header",
            maximum=80,
            required=True,
        )
        if raw_header is not None
        else ""
    )
    raw_options = value.get("options")
    if not isinstance(raw_options, list) or not 2 <= len(raw_options) <= 5:
        raise ValueError("grouped question must contain two to five options")
    options: list[dict[str, object]] = []
    seen_labels: set[str] = set()
    for raw_option in raw_options:
        if not isinstance(raw_option, Mapping):
            raise ValueError("grouped question option must be an object")
        if set(raw_option) - _GROUPED_OPTION_KEYS:
            raise ValueError("grouped question option contains unsupported fields")
        label = _bounded_grouped_string(
            raw_option.get("label"),
            field="option label",
            maximum=240,
            required=True,
        )
        if _grouped_option_label_is_custom(label):
            raise ValueError("grouped question must not add an Other option")
        if label in seen_labels:
            raise ValueError("grouped question option labels must be unique")
        option: dict[str, object] = {"label": label}
        for field in ("description", "preview"):
            raw_value = raw_option.get(field)
            if raw_value is None:
                continue
            normalized = _bounded_grouped_string(
                raw_value,
                field=f"option {field}",
                maximum=500,
                required=True,
            )
            option[field] = normalized
        seen_labels.add(label)
        options.append(option)
    raw_multi = value.get("multi")
    if raw_multi is not None and not isinstance(raw_multi, bool):
        raise ValueError("grouped question multi must be a boolean")
    multi = bool(raw_multi) if raw_multi is not None else False
    raw_recommended = value.get("recommended")
    recommended: int | None = None
    if raw_recommended is not None:
        if (
            isinstance(raw_recommended, bool)
            or not isinstance(raw_recommended, int)
            or not 0 <= raw_recommended < len(options)
        ):
            raise ValueError("grouped question recommendation index is invalid")
        recommended = raw_recommended
    seen_ids.add(question_id)
    canonical: dict[str, object] = {
        "id": question_id,
        "question": question,
        "options": options,
    }
    if header:
        canonical["header"] = header
    if raw_multi is not None:
        canonical["multi"] = multi
    if recommended is not None:
        canonical["recommended"] = recommended
    return canonical


def grouped_questions_from_wire(value: object) -> list[dict[str, object]]:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 12_000:
        raise ValueError("grouped question payload is missing or too large")
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("grouped question payload is not valid JSON") from exc
    if not isinstance(decoded, Mapping):
        raise ValueError("grouped question payload must be an object")
    if set(decoded) != {"schemaVersion", "questions"}:
        raise ValueError("grouped question payload contains unsupported fields")
    if decoded.get("schemaVersion") != GROUPED_QUESTIONS_SCHEMA_VERSION:
        raise ValueError("unsupported grouped question schema")
    raw_questions = decoded.get("questions")
    if not isinstance(raw_questions, list) or not 1 <= len(raw_questions) <= 4:
        raise ValueError("grouped question payload must contain one to four questions")
    seen_ids: set[str] = set()
    return [
        _canonical_grouped_question(item, seen_ids=seen_ids)
        for item in raw_questions
    ]


def canonical_grouped_answers(
    value: object,
    questions: object,
) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 12_000:
        raise ValueError("grouped answer payload is missing or too large")
    if not isinstance(questions, list) or not 1 <= len(questions) <= 4:
        raise ValueError("grouped question state is invalid")
    try:
        canonical_questions: list[dict[str, object]] = []
        seen_ids: set[str] = set()
        for question in questions:
            canonical_questions.append(
                _canonical_grouped_question(question, seen_ids=seen_ids)
            )
    except ValueError as exc:
        raise ValueError("grouped question state is invalid") from exc
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("grouped answer payload is not valid JSON") from exc
    if not isinstance(decoded, Mapping) or set(decoded) != {"answers"}:
        raise ValueError("grouped answer payload must contain only answers")
    raw_answers = decoded.get("answers")
    if not isinstance(raw_answers, Mapping):
        raise ValueError("grouped answers must be an object")
    expected_ids = [str(question["id"]) for question in canonical_questions]
    if set(raw_answers) != set(expected_ids):
        raise ValueError("grouped answers must cover every offered question")
    answers: dict[str, dict[str, object]] = {}
    for question in canonical_questions:
        question_id = str(question["id"])
        raw_answer = raw_answers.get(question_id)
        if not isinstance(raw_answer, Mapping):
            raise ValueError("grouped answer must be an object")
        if set(raw_answer) - _GROUPED_ANSWER_KEYS or "selected" not in raw_answer:
            raise ValueError("grouped answer shape is invalid")
        raw_selected = raw_answer.get("selected")
        if (
            not isinstance(raw_selected, list)
            or any(not isinstance(item, str) for item in raw_selected)
        ):
            raise ValueError("grouped answer selections must be strings")
        selected = [item.strip() for item in raw_selected]
        if any(not item for item in selected) or len(set(selected)) != len(selected):
            raise ValueError("grouped answer selections must be non-empty and unique")
        # _canonical_grouped_question always emits an options list.
        canonical_options = cast("list[object]", question["options"])
        option_labels = {
            str(option["label"])
            for option in canonical_options
            if isinstance(option, Mapping)
        }
        if any(item not in option_labels for item in selected):
            raise ValueError("grouped answer is not one of the offered options")
        multi = question.get("multi") is True
        if not multi and len(selected) > 1:
            raise ValueError("single-choice grouped answers allow one selection")
        raw_custom = raw_answer.get("custom")
        custom = ""
        if raw_custom is not None:
            custom = _bounded_grouped_string(
                raw_custom,
                field="custom answer",
                maximum=1_000,
                required=True,
            )
        if not selected and not custom:
            raise ValueError("grouped answer must select an option or provide custom text")
        if selected and custom and not multi:
            raise ValueError("single-choice grouped answers cannot combine custom text")
        answer: dict[str, object] = {"selected": selected}
        if custom:
            answer["custom"] = custom
        answers[question_id] = answer
    return json.dumps(
        {"answers": answers},
        ensure_ascii=False,
        separators=(",", ":"),
    )


_TRANSIENT_CONTEXT_PREFIX = "RAG_IME_TRANSIENT_CONTEXT_V1\n"
_TRANSIENT_CONTEXT_SCHEMA = "rag-ime.runtime-prompt.v1"


def _transient_context_message(text: str) -> str | None:
    if not text.startswith(_TRANSIENT_CONTEXT_PREFIX):
        return None
    try:
        envelope = json.loads(text[len(_TRANSIENT_CONTEXT_PREFIX) :])
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if (
        not isinstance(envelope, Mapping)
        or envelope.get("schemaVersion") != _TRANSIENT_CONTEXT_SCHEMA
        or not isinstance(envelope.get("message"), str)
    ):
        return None
    return str(envelope["message"]).strip()


def visible_message_text(role: str, text: str) -> str:
    if role == "assistant":
        # Some adapters emit internal preambles as text instead of a thinking
        # carrier. Never promote them to public summaries. Only a leading raw
        # wrapper is protocol; quoted/fenced examples and user text stay intact.
        remaining = text
        while True:
            stripped = remaining.lstrip()
            lowered = stripped.lower()
            if lowered and "<thinking>".startswith(lowered):
                return ""
            opening = re.match(r"<thinking\s*>", stripped, re.IGNORECASE)
            if not opening:
                return remaining
            closing = re.search(r"</thinking\s*>", stripped[opening.end():], re.IGNORECASE)
            if not closing:
                return ""
            remaining = stripped[opening.end() + closing.end():].lstrip()
    if role != "user":
        return text
    if text.startswith(_TRANSIENT_CONTEXT_PREFIX):
        return _transient_context_message(text) or ""
    tagged = re.search(
        r"<(?:agent|rag-ime)-user-query>\s*(.*?)\s*</(?:agent|rag-ime)-user-query>",
        text,
        flags=re.DOTALL,
    )
    if tagged:
        return tagged.group(1).strip()
    legacy_prefix = "请在当前连续会话中处理这个输入法深度查找任务。"
    marker = "\n用户问题：\n"
    if text.startswith(legacy_prefix) and marker in text:
        question = text.split(marker, 1)[1].split("\n\n本地时间：", 1)[0].strip()
        if question:
            return question
    return text


def pi_message_continues_public_turn(raw: Mapping[str, object]) -> bool:
    """Whether a durable Pi user entry belongs to the active PAW turn.

    Pi decodes the provider-only context envelope for ``session.prompt`` before
    writing the initial user entry. Native Steer/follow-up entries are appended
    while that PAW turn is already active and retain the envelope in Pi's
    transcript. The versioned schema marker is therefore the durable, non-temporal
    boundary: keep its public ``message`` in history, but do not open another
    top-level conversation turn for it.
    """

    if str(raw.get("role") or "").strip().lower() != "user":
        return False
    content = raw.get("content")
    texts: list[str]
    if isinstance(content, str):
        texts = [content]
    elif isinstance(content, list):
        texts = [
            str(item.get("text") or "")
            for item in content
            if isinstance(item, Mapping)
            and str(item.get("type") or "") == "text"
        ]
    else:
        return False
    return any(
        text.startswith(_TRANSIENT_CONTEXT_PREFIX)
        and _transient_context_message(text) is not None
        for text in texts
    )


def public_file_name(value: str) -> str:
    normalized = str(value or "").replace("\\", "/").rstrip("/")
    file_name = normalized.rsplit("/", 1)[-1].strip()
    if not file_name or len(file_name) > 240 or any(ord(character) < 32 for character in file_name):
        return ""
    if re.search(r"token|secret|password|api.?key|authorization|cookie", file_name, re.IGNORECASE):
        return ""
    return file_name


def supported_thinking_levels(raw: Mapping[str, object]) -> list[str]:
    if not bool(raw.get("reasoning")):
        return ["off"]
    explicit = raw.get("thinkingLevels")
    if isinstance(explicit, list):
        allowed = {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
        explicit_levels = [str(level) for level in explicit if str(level) in allowed]
        return list(dict.fromkeys(explicit_levels)) or ["off"]
    mapping = as_mapping(raw.get("thinkingLevelMap"))
    levels: list[str] = []
    for level in ("off", "minimal", "low", "medium", "high", "xhigh", "max"):
        mapped = mapping.get(level)
        if mapped is None and level in mapping:
            continue
        if level in {"xhigh", "max"} and level not in mapping:
            continue
        levels.append(level)
    return levels or ["off"]


def safe_scalar(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_runtime_text(str(value))[:2000]


def last_assistant_error(messages: list[object]) -> str:
    for item in reversed(messages):
        message = as_mapping(item)
        if str(message.get("role") or "") != "assistant":
            continue
        if str(message.get("stopReason") or "").lower() != "error" and not message.get("errorMessage"):
            return ""
        return redact_runtime_text(str(message.get("errorMessage") or "模型请求失败，请重试"))
    return ""


def last_assistant_preview(messages: list[object]) -> str:
    for item in reversed(messages):
        message = as_mapping(item)
        if str(message.get("role") or "") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return " ".join(visible_message_text("assistant", content).split())[:240]
        if isinstance(content, list):
            text = " ".join(
                visible_message_text("assistant", str(as_mapping(block).get("text") or ""))
                for block in content
                if str(as_mapping(block).get("type") or "") == "text"
            )
            return " ".join(text.split())[:240]
    return ""


def pi_message_id(raw: Mapping[str, object], turn_id: str) -> str:
    value = str(raw.get("id") or "").strip()
    if value:
        return value
    timestamp = as_integer(raw.get("timestamp"))
    role = str(raw.get("role") or "assistant").lower()
    if timestamp:
        return f"pi:message:{role}:{timestamp}"
    serialized = json.dumps(raw.get("content"), ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:20]
    return f"pi:message:{role}:{digest}"


def provider_request_receipt(
    raw: Mapping[str, object],
    *,
    turn_id: str,
    provider: str = "",
    model: str = "",
    status: str = "completed",
    completed_at_ms: int | None = None,
) -> dict[str, object]:
    """Return a bounded, content-free receipt for one Provider request.

    Pi emits an assistant message for every request, including assistant
    messages whose only purpose is to invoke a Tool.  The existing Pi message
    identity is carried as a bounded request identity; no prompt, completion,
    or new identity derivation crosses the boundary.  Usage and
    timing are copied only when Pi actually reported them; prompt, completion,
    and error text never enter this projection.
    """

    normalized_status = str(status or "completed").strip().lower()
    if normalized_status not in {"completed", "failed"}:
        normalized_status = "completed"
    message_id = re.sub(
        r"[^A-Za-z0-9_.:-]",
        "_",
        pi_message_id(raw, turn_id),
    )[:160]
    if not message_id:
        message_id = "response"
    safe_turn_id = re.sub(r"[^A-Za-z0-9_.:-]", "_", str(turn_id))[:160]
    if not safe_turn_id:
        safe_turn_id = "turn"
    receipt: dict[str, object] = {
        "requestId": f"{safe_turn_id}:provider:{message_id}",
        "status": normalized_status,
    }
    provider_value = str(raw.get("provider") or provider or "").strip()
    model_value = str(
        raw.get("responseModel") or raw.get("model") or model or ""
    ).strip()
    if provider_value:
        receipt["provider"] = provider_value[:80]
    if model_value:
        receipt["model"] = model_value[:160]
    started_at_ms = as_integer(raw.get("timestamp"))
    if started_at_ms:
        receipt["startedAtMs"] = started_at_ms
        # Pi's timestamp is the request start.  Only expose a measured
        # duration when it is an epoch-millisecond timestamp; tiny fixture
        # timestamps and malformed values are not latency evidence.
        if (
            completed_at_ms is not None
            and started_at_ms >= 1_000_000_000_000
            and completed_at_ms >= started_at_ms
        ):
            receipt["durationMs"] = max(0, int(completed_at_ms - started_at_ms))
    usage = public_usage(raw)
    if public_usage_evidence(raw)["usageReported"]:
        receipt["usage"] = usage
        receipt.update(public_usage_evidence(raw))
    return receipt


def pi_message_is_public(raw: Mapping[str, object]) -> bool:
    """Keep Pi's loop protocol out while retaining user-visible assistant text.

    Providers commonly attach a public progress paragraph and a Tool call to
    the same assistant message.  The Tool protocol is projected separately as
    a redacted activity timeline, but that must not erase the paragraph from
    durable history.  Tool-only messages remain absent from the human
    transcript.
    """

    role = str(raw.get("role") or "assistant").lower()
    if role == "user":
        return True
    if role != "assistant":
        return False
    content = raw.get("content")
    if not isinstance(content, list):
        return bool(visible_message_text(role, str(content or "")).strip()) or bool(raw.get("errorMessage"))
    return any(
        (
            str(as_mapping(item).get("type") or "") == "text"
            and bool(visible_message_text(role, str(as_mapping(item).get("text") or "")).strip())
        )
        or str(as_mapping(item).get("type") or "") == "image"
        for item in content
    ) or bool(raw.get("errorMessage"))


def pi_message_completes_public_turn(raw: Mapping[str, object]) -> bool:
    """Whether a Pi message is the durable assistant result for this turn.

    A Provider may stream useful progress text and a Tool call in the same
    assistant message.  That text is public, but the message is not terminal:
    the Tool result will be followed by another assistant message.  Publishing
    both as ``message_completed`` creates duplicate transcript rows and makes
    the first, provisional row look like a finished answer.
    """

    if not pi_message_is_public(raw):
        return False
    content = raw.get("content")
    if not isinstance(content, list):
        return True
    return not any(
        str(as_mapping(item).get("type") or "") in {"toolCall", "tool_call"}
        for item in content
    )


def public_reasoning_summaries(
    raw: Mapping[str, object],
    *,
    maximum_items: int = 8,
    completed_content_index: int | None = None,
) -> list[str]:
    """Project only Provider-authored reasoning *summaries*.

    Pi's generic ``thinking`` carrier also transports private chain-of-thought
    for some Providers.  Only the OpenAI Responses API and the Codex Responses
    adapter define this carrier as a user-visible reasoning summary, so every
    other API (and every ``redacted_thinking`` block) stays private.  The
    projection is bounded, path/secret redacted, and contains no signatures or
    raw Provider metadata.
    """

    if str(raw.get("api") or "").strip().lower() not in {
        "openai-responses",
        "openai-codex-responses",
    }:
        return []
    content = raw.get("content")
    if not isinstance(content, list):
        return []
    if completed_content_index is not None:
        # An empty/signature-only or private block ending is not a new public
        # summary. Do not replay the previous block's text for that event.
        if not 0 <= completed_content_index < len(content):
            return []
        if not public_reasoning_summaries(
            {**raw, "content": [content[completed_content_index]]},
            maximum_items=maximum_items,
        ):
            return []
    limit = max(1, min(int(maximum_items), 12))
    result: list[str] = []
    for raw_block in content:
        block = as_mapping(raw_block)
        if str(block.get("type") or "") != "thinking":
            continue
        value = str(block.get("thinking") or block.get("text") or "").strip()
        if not value:
            continue
        # These wrappers may occur inside already-public Provider summaries.
        value = re.sub(r"</?thinking\s*>", "", value, flags=re.IGNORECASE).strip()
        headings = re.findall(r"\*\*([^*\n]{1,300})\*\*", value)
        candidates = headings or re.split(r"(?:\r?\n){2,}|\r?\n", value)
        for candidate in candidates:
            normalized = re.sub(
                r"^(?:[-*+]\s+|#{1,6}\s+)",
                "",
                str(candidate).strip(),
            )
            normalized = normalized.strip("*_` ")
            if not normalized:
                continue
            # Non-heading summaries can be paragraphs. Keep the first bounded
            # sentence rather than exposing an entire reasoning transcript.
            if not headings:
                sentence = re.split(r"(?<=[。！？.!?])\s+", normalized, maxsplit=1)[0]
                normalized = sentence or normalized
            safe = redact_runtime_text(normalized)[:240].strip()
            if safe and safe not in result:
                result.append(safe)
            # A long Responses message can contain many thinking blocks. Keep
            # the recent bounded tail so live updates do not freeze at item 8.
            if len(result) > limit:
                result.pop(0)
    return result


def public_usage(value: object) -> dict[str, int]:
    message = as_mapping(value)
    usage = as_mapping(message.get("usage"))
    input_tokens = as_integer(usage.get("input"))
    output_tokens = as_integer(usage.get("output"))
    cache_read = as_integer(usage.get("cacheRead"))
    cache_write = as_integer(usage.get("cacheWrite"))
    total = as_integer(usage.get("totalTokens"))
    if total <= 0:
        total = input_tokens + output_tokens + cache_read + cache_write
    return {
        "input": input_tokens,
        "output": output_tokens,
        "cacheRead": cache_read,
        "cacheWrite": cache_write,
        "totalTokens": total,
    }


def public_usage_evidence(value: object) -> dict[str, bool]:
    """Describe which usage fields the Provider response actually reported."""

    usage_value = as_mapping(value).get("usage")
    if not isinstance(usage_value, Mapping):
        return {
            "usageReported": False,
            "cacheUsageReported": False,
        }
    usage = dict(usage_value)

    def reported(key: str) -> bool:
        metric = usage.get(key)
        return (
            isinstance(metric, (int, float))
            and not isinstance(metric, bool)
        )

    return {
        "usageReported": any(
            reported(key)
            for key in (
                "input",
                "output",
                "cacheRead",
                "cacheWrite",
                "totalTokens",
            )
        ),
        "cacheUsageReported": any(
            reported(key)
            for key in ("cacheRead", "cacheWrite")
        ),
    }


def provider_retry_status(
    payload: Mapping[str, object],
    *,
    started: bool,
) -> dict[str, object]:
    """Project retry progress without leaking raw Provider diagnostics."""

    attempt = max(1, as_integer(payload.get("attempt")))
    maximum = max(attempt, as_integer(payload.get("maxAttempts")))
    if started:
        return {
            "status": "retrying",
            "phase": "provider_retry",
            "activityState": "running",
            "summary": f"模型连接暂时失败，正在自动重试（{attempt}/{maximum}）",
            "attempt": attempt,
            "maxAttempts": maximum,
            "delayMs": max(0, as_integer(payload.get("delayMs"))),
        }
    success = payload.get("success") is True
    return {
        "status": "analyzing" if success else "working",
        "phase": "provider_retry",
        "activityState": "completed" if success else "failed",
        "summary": (
            "模型连接已恢复，继续处理"
            if success
            else "自动重试未恢复，正在结束本轮"
        ),
        "attempt": attempt,
        "maxAttempts": maximum,
        "success": success,
    }


def ui_confirmation_value(value: object) -> bool:
    normalized = str(value or "").strip().lower()
    affirmative = (
        "yes",
        "y",
        "true",
        "confirm",
        "confirmed",
        "allow",
        "approve",
        "是",
        "确认",
        "同意",
        "允许",
        "批准",
        "保留",
    )
    negative = (
        "no",
        "n",
        "false",
        "cancel",
        "deny",
        "reject",
        "否",
        "取消",
        "不同意",
        "拒绝",
        "不允许",
        "删除",
    )
    if any(normalized == item or normalized.startswith(f"{item}，") or normalized.startswith(f"{item},") for item in affirmative):
        return True
    if any(normalized == item or normalized.startswith(f"{item}，") or normalized.startswith(f"{item},") for item in negative):
        return False
    raise PiRuntimeError("confirm UI response must explicitly approve or reject the request")


def public_fork_candidate_text(value: object, *, role: str = "user") -> str:
    """Return a public transcript preview without leaking injected context."""

    normalized = " ".join(str(value or "").split())[:8000]
    visible = (
        " ".join(visible_message_text("user", normalized).split())[:8000]
        if role == "user"
        else normalized
    )
    if any(
        marker in visible
        for marker in (
            "<agent-deep-search-context",
            "<agent-user-query",
            "<rag-ime-deep-search-context",
            "<rag-ime-user-query",
        )
    ):
        return ""
    return visible


def public_code_tool_activity(
    tool_name: str,
    args: Mapping[str, object],
    raw_result: object = None,
) -> dict[str, object]:
    """Project a bounded, useful coding-tool receipt for the private timeline.

    Tool arguments in the generic event envelope are intentionally redacted,
    but reducing every path, query and command to a generic label made the
    timeline useless for debugging. This projection keeps the operational
    nouns a user needs to audit (target, pattern, command, range) while
    redacting credentials and bounding returned output. Mutation bodies are
    never copied into the event.
    """

    normalized_tool = str(tool_name or "").strip().lower()
    file_tools = {
        "read", "read_file", "workspace_read",
        "write", "write_file", "workspace_write_file", "workspace_write",
        "edit", "edit_file", "workspace_edit_file", "workspace_edit",
        "workspace_patch",
    }
    search_tools = {"grep", "workspace_search"}
    list_tools = {"find", "ls", "workspace_list"}
    command_tools = {"bash", "workspace_shell", "workspace_job"}
    semantic_tools = {"workspace_lsp"}
    coding_tools = (
        file_tools | search_tools | list_tools | command_tools | semantic_tools
    )
    if normalized_tool not in coding_tools:
        return {}
    evidence = _public_tool_evidence_envelope(raw_result)
    evidence_request = as_mapping(evidence.get("evidenceRequest"))
    effective_args = {**evidence_request, **dict(args)}
    raw_path = str(
        effective_args.get("relativePath")
        or effective_args.get("fileName")
        or effective_args.get("file_path")
        or effective_args.get("path")
        or ""
    )
    workspace_path = _public_workspace_path(raw_path)
    file_name = public_file_name(raw_path)
    result: dict[str, object] = {}
    if file_name:
        result["fileName"] = file_name
    if workspace_path:
        result["path"] = workspace_path
    for key in ("root", "cwd"):
        visible_path = _public_workspace_path(effective_args.get(key))
        if visible_path:
            result[key] = visible_path

    for key in (
        "op",
        "operation",
        "mode",
        "patternKind",
        "server",
        "label",
        "jobId",
        "status",
        "reason",
    ):
        value = _public_tool_text(effective_args.get(key), maximum=240)
        if value:
            result[key] = value
    for key in ("query", "pattern", "glob", "title", "newName"):
        value = _public_tool_text(effective_args.get(key), maximum=500)
        if value:
            result[key] = value
    for key in (
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
    ):
        numeric_value = effective_args.get(key)
        if isinstance(numeric_value, (int, float)) and not isinstance(
            numeric_value,
            bool,
        ):
            result[key] = numeric_value

    if normalized_tool in command_tools:
        command = _public_tool_text(effective_args.get("command"), maximum=2_000)
        if command:
            result["command"] = command

    if normalized_tool in {"write", "write_file", "workspace_write_file", "workspace_write"}:
        content = effective_args.get("content")
        if isinstance(content, str) and content:
            normalized = content.replace("\r\n", "\n").replace("\r", "\n")
            lines = normalized.split("\n")
            while lines and not lines[-1]:
                lines.pop()
            line_count = max(1, len(lines))
            result.update({"lineCount": line_count, "additions": line_count})
            if file_name:
                result["summary"] = f"{file_name} +{line_count}"
    if normalized_tool in {
        "edit", "edit_file", "workspace_edit_file", "workspace_edit",
        "workspace_patch",
    }:
        additions, deletions = _public_mutation_line_counts(effective_args)
        if additions is not None:
            result["additions"] = additions
        if deletions is not None:
            result["deletions"] = deletions
    preview_tools = (
        file_tools
        - {
            "write", "write_file", "workspace_write_file", "workspace_write",
            "edit", "edit_file", "workspace_edit_file", "workspace_edit",
            "workspace_patch",
        }
        | search_tools
        | list_tools
        | command_tools
        | semantic_tools
    )
    if (
        normalized_tool in preview_tools
        and _public_tool_output_allowed(normalized_tool, raw_path)
    ):
        preview, truncated = _public_tool_output_preview(
            raw_result,
            evidence=evidence,
        )
        if preview:
            result["outputPreview"] = preview
            result["outputTruncated"] = truncated
    model_decision = _public_approval_model_decision(raw_result)
    if model_decision:
        result["decisionMode"] = "model"
        result["approvalModelDecision"] = model_decision
        result["automatic"] = True

    return result


_PUBLIC_KNOWLEDGE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_PUBLIC_KNOWLEDGE_MODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}\Z")


def _public_knowledge_id(value: object) -> str:
    candidate = str(value or "").strip()
    return candidate if _PUBLIC_KNOWLEDGE_ID_RE.fullmatch(candidate) else ""


def _public_knowledge_mode(value: object) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if _PUBLIC_KNOWLEDGE_MODE_RE.fullmatch(candidate) else ""


def public_knowledge_tool_activity(
    tool_name: str,
    args: Mapping[str, object],
    raw_result: object = None,
) -> dict[str, object]:
    """Project one Knowledge search into privacy-safe retrieval metadata.

    Search queries, document names, citations, content and local paths are
    intentionally absent.  The projection keeps only opaque Knowledge ids,
    measured result counts, retrieval mode and evidence receipts needed by
    Trace/Eval.  It is shared by both Pi runtime protocols so a vertical Agent
    produces the same trace regardless of the host transport in use.
    """

    if str(tool_name or "").strip().lower() != "knowledge":
        return {}
    operation = str(args.get("operation") or args.get("op") or "").strip().lower()
    if operation != "search":
        return {}

    result = as_mapping(raw_result)
    retrieval = as_mapping(result.get("retrieval"))
    kb_id = _public_knowledge_id(args.get("kbId") or args.get("baseId"))
    mode = _public_knowledge_mode(
        retrieval.get("effectiveMode")
        or retrieval.get("mode")
        or args.get("searchMode")
        or args.get("mode")
    )
    raw_items = result.get("items") or result.get("hits")
    items = (
        list(raw_items[:32])
        if isinstance(raw_items, list)
        else list(raw_items[:32])
        if isinstance(raw_items, tuple)
        else []
    )
    evidence: list[dict[str, object]] = []
    for rank, raw_item in enumerate(items, start=1):
        item = as_mapping(raw_item)
        item_kb_id = _public_knowledge_id(
            item.get("kbId") or item.get("baseId") or kb_id
        )
        chunk_id = _public_knowledge_id(item.get("chunkId"))
        if not item_kb_id or not chunk_id:
            continue
        source_ref = f"knowledge://{item_kb_id}/{chunk_id}"
        readable_evidence_id = f"knowledge:{item_kb_id}:{chunk_id}"
        evidence_id = (
            readable_evidence_id
            if len(readable_evidence_id) <= 160
            else f"knowledge:sha256:{hashlib.sha256(source_ref.encode('utf-8')).hexdigest()}"
        )
        scores: dict[str, float] = {}
        raw_score = item.get("score")
        if (
            isinstance(raw_score, (int, float))
            and not isinstance(raw_score, bool)
            and math.isfinite(float(raw_score))
        ):
            scores["score"] = round(float(raw_score), 6)
        evidence.append(
            {
                "evidenceId": evidence_id,
                "sourceKind": "knowledge",
                "sourceRef": source_ref,
                "sourceLane": mode,
                "disposition": "included",
                "scores": scores,
                "rankBefore": None,
                "rankAfter": rank,
                "omissionReason": "",
            }
        )

    raw_total = result.get("total")
    evidence_count = (
        int(raw_total)
        if isinstance(raw_total, int)
        and not isinstance(raw_total, bool)
        and 0 <= raw_total <= 1_000_000
        else len(items)
    )
    projection: dict[str, object] = {
        "activityKind": "knowledge_retrieval",
        "operation": "search",
        "evidenceStage": "retrieval_output",
        "evidenceCount": evidence_count,
        "traceEvidence": evidence,
    }
    if kb_id:
        projection["kbId"] = kb_id
    if mode:
        projection["retrievalMode"] = mode
    return projection


def _public_mutation_line_counts(
    args: Mapping[str, object],
) -> tuple[int | None, int | None]:
    pairs: list[tuple[str, str]] = []
    edits = args.get("edits")
    if isinstance(edits, list):
        for value in edits[:64]:
            edit = as_mapping(value)
            old_text = edit.get("oldText")
            new_text = edit.get("newText")
            if isinstance(old_text, str) and isinstance(new_text, str):
                pairs.append((old_text, new_text))
    else:
        old_text = args.get("oldText")
        new_text = args.get("newText")
        if isinstance(old_text, str) and isinstance(new_text, str):
            pairs.append((old_text, new_text))
    if not pairs:
        return None, None
    return (
        sum(_public_text_line_count(new_text) for _, new_text in pairs),
        sum(_public_text_line_count(old_text) for old_text, _ in pairs),
    )


def _public_text_line_count(value: str) -> int:
    if not value:
        return 0
    lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[-1]:
        lines.pop()
    return max(1, len(lines))


def runtime_tool_result_is_error(
    tool_name: str,
    raw_result: object,
    *,
    reported_is_error: bool = False,
) -> bool:
    """Return the authoritative terminal error state for a managed Tool.

    Pi marks a Tool invocation as failed when its implementation raises. The
    PAW backend command bridge instead returns a normal ToolResult containing
    the durable workspace receipt, including ``exitCode`` and ``timedOut``.
    For command aliases and Browser, that structured receipt is therefore more
    authoritative than Pi's transport-level ``isError`` bit. Output text is
    deliberately ignored: tests and programs may print words such as
    ``FAILED`` while still exiting successfully.
    """

    if reported_is_error:
        return True
    normalized_tool = str(tool_name or "").strip().lower()
    root = as_mapping(raw_result)
    details = as_mapping(root.get("details"))
    if normalized_tool == "browser":
        # Inspect only the Tool's own receipt. Page text and historical entries
        # returned by browser.trace must never change this invocation's status.
        return any(receipt.get("ok") is False for receipt in (details, root))
    if normalized_tool not in {
        "bash",
        "workspace_shell",
    }:
        return False

    approval = as_mapping(details.get("approval"))
    candidates = (
        as_mapping(details.get("receipt")),
        as_mapping(approval.get("receipt")),
        as_mapping(root.get("receipt")),
        details,
        root,
    )
    for receipt in candidates:
        if any(
            receipt.get(key) is True
            for key in ("timedOut", "cancelled", "aborted")
        ):
            return True
        exit_code = receipt.get("exitCode")
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            return exit_code != 0
    return False


def _public_approval_model_decision(
    raw_result: object,
) -> dict[str, object]:
    root = as_mapping(raw_result)
    nested_result = as_mapping(root.get("result"))
    nested_details = as_mapping(root.get("details"))
    nested_approval = as_mapping(
        root.get("approval")
        or nested_result.get("approval")
        or nested_details.get("approval")
    )
    for candidate in (
        root.get("approvalModelDecision"),
        nested_result.get("approvalModelDecision"),
        nested_details.get("approvalModelDecision"),
        nested_approval.get("approvalModelDecision"),
    ):
        if not isinstance(candidate, Mapping):
            continue
        public = dict(candidate)
        try:
            validate_contract(
                public,
                "agent-approval-model-decision.v1.json",
            )
        except ValueError:
            continue
        return public
    return {}



def _public_workspace_path(value: object) -> str:
    normalized = str(value or "").replace("\\", "/").strip()
    normalized = re.sub(r"/{2,}", "/", normalized)
    if (
        not normalized
        or len(normalized) > 1_000
        or any(ord(character) < 32 for character in normalized)
        or re.search(
            r"token|secret|password|api.?key|authorization|cookie",
            normalized,
            re.IGNORECASE,
        )
    ):
        return ""
    parts = [part for part in normalized.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        return ""
    absolute = normalized.startswith("/") or normalized.startswith("~/")
    if not absolute:
        return "/".join(parts) or normalized
    visible_parts = parts
    if normalized.startswith("/Users/") and len(parts) >= 2:
        visible_parts = parts[2:]
    elif normalized.startswith("/Volumes/") and len(parts) >= 2:
        visible_parts = parts[2:]
    elif normalized.startswith("/private/var/") and len(parts) >= 2:
        visible_parts = parts[2:]
    # Keep enough suffix to identify a concrete target without persisting a
    # home-directory account name or external-volume label.
    suffix = "/".join(visible_parts[-4:])
    return f"…/{suffix}" if suffix else ""


def _public_tool_text(value: object, *, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{6,}\b", "[REDACTED_SECRET]", text)
    text = re.sub(
        r"(?i)\b([a-z0-9_]*(?:api[_-]?key|access[_-]?token|password|secret|authorization))"
        r"(\s*(?:=|:)\s*)([^\s'\";]+|\"[^\"]*\"|'[^']*')",
        r"\1\2[REDACTED_SECRET]",
        text,
    )
    text = re.sub(
        r"(?i)(--(?:api[_-]?key|token|password|secret)\s+)"
        r"([^\s'\";]+|\"[^\"]*\"|'[^']*')",
        r"\1[REDACTED_SECRET]",
        text,
    )
    # Keep useful path suffixes in commands and grep output, but never persist
    # the local account or external-volume label. Prefix replacement works for
    # quoted paths containing spaces, unlike a whitespace-delimited path regex.
    text = re.sub(r"/Users/[^/\s]+/", "~/", text)
    text = re.sub(r"/Volumes/[^/]+/", "/…/", text)
    text = text.replace("/private/var/", "/…/var/")
    text = text.replace("/var/folders/", "/…/var/folders/")
    return text[:maximum]


def _public_tool_output_allowed(tool_name: str, raw_path: str) -> bool:
    if tool_name in {"bash", "workspace_shell"}:
        return True
    normalized = raw_path.replace("\\", "/").lower()
    basename = normalized.rsplit("/", 1)[-1]
    return not (
        basename in {
            ".env", ".env.local", ".env.production", ".npmrc", ".pypirc",
            ".netrc", "auth.json", "credentials", "credentials.json",
            "id_rsa", "id_ed25519",
        }
        or basename.endswith((".pem", ".key", ".p12", ".pfx"))
    )


def _public_tool_evidence_envelope(raw_result: object) -> Mapping[str, object]:
    """Recover a managed coding-tool receipt before generic truncation.

    The runtime bridge deliberately returns a small JSON evidence envelope
    rather than placing raw workspace output in the public event. Treating
    that envelope as ordinary text exposed internal handles and JSON syntax
    while hiding the useful request/summary. Parse only the bounded envelope
    shapes owned by the bridge; malformed or oversized values fall back to
    the generic safe preview path.
    """

    result = as_mapping(raw_result)
    candidates: list[object] = []
    content = result.get("content")
    if isinstance(content, list):
        candidates.extend(
            as_mapping(item).get("text")
            for item in content[:16]
        )
    candidates.extend(result.get(key) for key in ("stdout", "output", "text"))
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate:
            continue
        # Large enough for the bridge's intentionally bounded summaries, but
        # refuse to JSON-decode arbitrary multi-megabyte tool output.
        if len(candidate) > 4_000_000 or not candidate.lstrip().startswith("{"):
            continue
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        envelope = as_mapping(parsed)
        if not envelope.get("evidenceHandle"):
            continue
        if not any(
            key in envelope
            for key in ("evidenceSummary", "previewHead", "evidenceRequest")
        ):
            continue
        return envelope
    return {}


def _public_tool_output_preview(
    raw_result: object,
    *,
    evidence: Mapping[str, object] | None = None,
) -> tuple[str, bool]:
    evidence = evidence or _public_tool_evidence_envelope(raw_result)
    if evidence:
        raw_summary = evidence.get("evidenceSummary") or evidence.get("previewHead")
        source = str(raw_summary or "")
        text = _public_tool_text(source, maximum=6_000)
        if text:
            lines = text.splitlines()
            preview = "\n".join(lines[:40])[:6_000]
            evidence_bytes = as_integer(evidence.get("evidenceBytes"))
            truncated = (
                len(source) > 6_000
                or len(lines) > 40
                or evidence_bytes > len(source.encode("utf-8"))
                or bool(evidence.get("continuation"))
            )
            return preview, truncated

    result = as_mapping(raw_result)
    content = result.get("content")
    chunks: list[str] = []
    source_truncated = False
    if isinstance(content, list):
        source_truncated = len(content) > 16
        for item in content[:16]:
            raw_text = as_mapping(item).get("text")
            if isinstance(raw_text, str) and len(raw_text) > 6_000:
                source_truncated = True
            text = _public_tool_text(raw_text, maximum=6_000)
            if text:
                chunks.append(text)
    if not chunks:
        for key in ("stdout", "output", "text"):
            raw_text = result.get(key)
            if isinstance(raw_text, str) and len(raw_text) > 6_000:
                source_truncated = True
            text = _public_tool_text(raw_text, maximum=6_000)
            if text:
                chunks.append(text)
                break
    if not chunks:
        return "", False
    joined = "\n".join(chunks)
    lines = joined.splitlines()
    truncated = source_truncated or len(joined) > 6_000 or len(lines) > 40
    preview = "\n".join(lines[:40])[:6_000]
    return preview, truncated


def public_pi_model(raw: Mapping[str, object]) -> dict[str, object]:
    provider = str(raw.get("provider") or "").strip()
    model_id = str(raw.get("id") or "").strip()
    if not provider or not model_id:
        return {}
    raw_inputs = raw.get("input")
    inputs = raw_inputs if isinstance(raw_inputs, list) else []
    return {
        "provider": provider[:80],
        "id": model_id[:160],
        "name": str(raw.get("name") or model_id).strip()[:160] or model_id[:160],
        "api": str(raw.get("api") or "").strip()[:80],
        "reasoning": bool(raw.get("reasoning")),
        "thinkingLevels": supported_thinking_levels(raw),
        "supportsImages": "image" in {str(item) for item in inputs},
        "contextWindow": as_integer(raw.get("contextWindow")),
        "maxTokens": as_integer(raw.get("maxTokens")),
    }


def redact_mapping(value: Mapping[str, object], *, depth: int = 0) -> dict[str, object]:
    if depth >= 4:
        return {"truncated": True}
    result: dict[str, object] = {}
    for raw_key, raw_value in list(value.items())[:64]:
        key = str(raw_key)[:120]
        if re.search(r"token|secret|password|api.?key|authorization|cookie", key, re.IGNORECASE):
            result[key] = "[REDACTED_SECRET]"
        elif isinstance(raw_value, Mapping):
            result[key] = redact_mapping(raw_value, depth=depth + 1)
        elif isinstance(raw_value, list):
            result[key] = [
                redact_mapping(item, depth=depth + 1) if isinstance(item, Mapping) else safe_scalar(item)
                for item in raw_value[:64]
            ]
        else:
            result[key] = safe_scalar(raw_value)
    return result


_TOOL_RESULT_SECRET_KEY = re.compile(
    r"token|secret|password|api.?key|authorization|cookie",
    re.IGNORECASE,
)


def inspectable_tool_result(value: object) -> object:
    """Keep the complete JSON-shaped Tool receipt available to local UI.

    Unlike ``redact_mapping`` this projection intentionally preserves paths,
    nested fields, long text and list contents so the result inspector can be
    used for debugging. Credential values remain masked because a local Tool
    result can still contain provider or browser authentication material.
    """

    if isinstance(value, Mapping):
        return {
            str(raw_key): (
                "[REDACTED_SECRET]"
                if _TOOL_RESULT_SECRET_KEY.search(str(raw_key))
                else inspectable_tool_result(raw_value)
            )
            for raw_key, raw_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [inspectable_tool_result(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_tool_result_credentials(str(value))


def public_codemode_nested_calls(
    raw_result: object,
) -> list[dict[str, object]]:
    """Project Pi's bounded nested-call receipt for a public timeline.

    Pi 0.99 stores two related views on the outer ToolResult: the generic
    ``nestedCalls`` record keeps real argument objects, while codemode's own
    ``details.calls`` record keeps compact previews, cancellation and model
    cost fields. Prefer the generic record and enrich it by id from details so
    a replay has one child identity per call. Nested results are intentionally
    absent here: live ``tool_execution_*`` events carry those real results and
    a cold transcript does not contain them. A missing terminal status remains
    running instead of being promoted to success.
    """

    root = as_mapping(raw_result)
    nested = as_mapping(root.get("nestedCalls"))
    details = as_mapping(root.get("details"))
    detail_calls = _nested_call_items(details.get("calls"))
    nested_calls = _nested_call_items(nested.get("calls"))
    source = nested_calls or detail_calls
    if not source:
        return []
    by_id = {
        str(item.get("id") or ""): item
        for item in detail_calls
        if str(item.get("id") or "")
    }
    result: list[dict[str, object]] = []
    for raw in source[:256]:
        detail = by_id.get(str(raw.get("id") or ""), {})
        call_id = str(raw.get("id") or detail.get("id") or "").strip()[:512]
        name = str(raw.get("name") or detail.get("name") or "").strip()[:120]
        if not call_id or not name:
            continue
        detail = by_id.get(call_id, detail)
        projected: dict[str, object] = {
            "id": call_id,
            "name": name,
            "status": _public_nested_call_status(
                raw.get("status") or detail.get("status")
            ),
        }
        arguments = _public_nested_call_arguments(
            raw.get("arguments")
            if raw.get("arguments") is not None
            else raw.get("args")
            if raw.get("args") is not None
            else detail.get("arguments")
            if detail.get("arguments") is not None
            else detail.get("args")
        )
        if arguments:
            projected["args"] = arguments
        arguments_bytes = raw.get("argumentsBytes")
        if not isinstance(arguments_bytes, int) or isinstance(arguments_bytes, bool):
            arguments_bytes = detail.get("argumentsBytes")
        if isinstance(arguments_bytes, int) and not isinstance(arguments_bytes, bool):
            projected["argumentsBytes"] = max(0, min(arguments_bytes, 1_000_000))
        duration = raw.get("durationMs")
        if not isinstance(duration, (int, float)) or isinstance(duration, bool):
            duration = detail.get("durationMs")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            projected["durationMs"] = max(0, min(int(duration), 2_147_483_647))
        error = raw.get("error") or detail.get("error")
        if error:
            error_text = _public_tool_text(error, maximum=500)
            if error_text:
                projected["error"] = error_text
        cost = raw.get("cost")
        if not isinstance(cost, (int, float)) or isinstance(cost, bool):
            cost = detail.get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            try:
                finite_cost = math.isfinite(float(cost))
            except (OverflowError, ValueError):
                finite_cost = False
            if finite_cost:
                projected["cost"] = max(0.0, min(float(cost), 1_000_000.0))
        result.append(projected)
    return result


def _nested_call_items(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value[:256] if isinstance(item, Mapping)]


def _public_nested_call_status(value: object) -> str:
    normalized = str(value or "").strip().lower()
    return {
        "unfinished": "running",
        "running": "running",
        "ok": "ok",
        "error": "error",
        "cancelled": "cancelled",
    }.get(normalized, "running")


def _public_nested_call_arguments(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return redact_mapping(value)
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = None
        if isinstance(decoded, Mapping):
            return redact_mapping(decoded)
        text = _public_tool_text(value, maximum=500)
        return {"value": text} if text else {}
    return {}


def _redact_tool_result_credentials(value: str) -> str:
    redacted = re.sub(
        r"\bsk-[A-Za-z0-9_-]{6,}\b",
        "[REDACTED_SECRET]",
        value,
    )
    redacted = re.sub(
        r"\b([A-Za-z0-9_]*(?:api[_-]?key|access[_-]?token|password|secret|authorization))"
        r"(\s*(?:=|:)\s*)([^\s;'\"\\]+|\"[^\"]*\"|'[^']*')",
        r"\1\2[REDACTED_SECRET]",
        redacted,
        flags=re.IGNORECASE,
    )
    return re.sub(
        r"(--(?:api[_-]?key|token|password|secret)\s+)"
        r"([^\s;'\"\\]+|\"[^\"]*\"|'[^']*')",
        r"\1[REDACTED_SECRET]",
        redacted,
        flags=re.IGNORECASE,
    )


def managed_media_content_url(session_id: str, media_id: str) -> str:
    return (
        f"/api/agent/media/{quote(str(media_id), safe='')}/content"
        f"?sessionId={quote(str(session_id), safe='')}"
    )


def pi_message_payload(
    raw: Mapping[str, object],
    *,
    session_id: str,
    turn_id: str,
    media_resolver: Callable[[str, str, str], str] | None = None,
    message_id: str | None = None,
    trusted_blocks: object = None,
) -> AgentMessage:
    role = str(raw.get("role") or "assistant")
    if role not in {"user", "assistant", "tool", "system"}:
        role = "tool" if role.lower().startswith("tool") else "assistant"
    content = raw.get("content")
    resolved_message_id = message_id or pi_message_id(raw, turn_id)
    fallback_blocks_enabled = trusted_blocks is None
    blocks: list[AgentBlock] = []
    blocks.extend(
        AgentBlock.from_payload(item)
        for item in normalize_trusted_agent_blocks(
            trusted_blocks,
            source_kind="pi_runtime_event",
            source_ref=f"{session_id}:{resolved_message_id}",
        )
    )
    attachments: list[str] = []
    if isinstance(content, str):
        visible_content = visible_message_text(role, content)
        extracted = (
            extract_completed_agent_blocks(
                visible_content,
                source_kind="pi_session_message",
                source_ref=f"{session_id}:{resolved_message_id}",
            )
            if role == "assistant"
            else None
        )
        if extracted is not None:
            visible_content = extracted.text
            if fallback_blocks_enabled:
                blocks.extend(AgentBlock.from_payload(item) for item in extracted.blocks)
        if visible_content:
            blocks.append(
                normalize_agent_block(
                    {
                        "id": f"{turn_id}:text:0",
                        "type": "text",
                        "status": "completed",
                        "presentationKind": "markdown",
                        "data": {"text": visible_content},
                    }
                )
            )
    elif isinstance(content, list):
        for index, item in enumerate(content):
            value = as_mapping(item)
            content_type = str(value.get("type") or "unknown")
            if content_type == "text":
                visible_content = visible_message_text(role, str(value.get("text") or ""))
                extracted = (
                    extract_completed_agent_blocks(
                        visible_content,
                        source_kind="pi_session_message",
                        source_ref=f"{session_id}:{resolved_message_id}",
                    )
                    if role == "assistant"
                    else None
                )
                if extracted is not None:
                    visible_content = extracted.text
                    if fallback_blocks_enabled:
                        blocks.extend(AgentBlock.from_payload(item) for item in extracted.blocks)
                if visible_content:
                    blocks.append(
                        normalize_agent_block(
                            {
                                "id": f"{turn_id}:text:{index}",
                                "type": "text",
                                "status": "completed",
                                "presentationKind": "markdown",
                                "data": {"text": visible_content},
                            }
                        )
                    )
            elif content_type == "image" and media_resolver is not None:
                media_id = media_resolver(
                    session_id,
                    str(value.get("mimeType") or ""),
                    str(value.get("data") or ""),
                )
                if media_id:
                    attachments.append(media_id)
                    receipt_url = managed_media_content_url(session_id, media_id)
                    blocks.append(
                        normalize_agent_block(
                            {
                                "id": f"{turn_id}:image:{index}",
                                "type": "image",
                                "status": "completed",
                                "presentationKind": "image",
                                "data": {
                                    "mediaId": media_id,
                                    "receiptUrl": receipt_url,
                                },
                            }
                        )
                    )
            elif content_type in {"thinking", "redacted_thinking"}:
                continue
            elif content_type in {"toolCall", "tool_call"}:
                # Tool lifecycle events own the public execution timeline.
                # Do not duplicate Pi protocol blocks or their arguments in
                # the human transcript message.
                continue
    stop_reason = str(raw.get("stopReason") or "").lower()
    aborted = stop_reason == "aborted"
    error_message = redact_runtime_text(str(raw.get("errorMessage") or "").strip())
    failed = not aborted and (stop_reason == "error" or bool(error_message))
    if failed:
        if role == "assistant" and not any(block.block_type == "text" for block in blocks):
            retained_results = any(
                block.block_type in {"artifact", "diff", "file"}
                for block in blocks
            )
            blocks.insert(
                0,
                normalize_agent_block(
                    {
                        "id": f"{turn_id}:failure-text:0",
                        "type": "text",
                        "status": "failed",
                        "presentationKind": "markdown",
                        "data": {
                            "text": provider_auth_failure_message(error_message) or (
                                "模型服务未能生成最终回复。"
                                + (
                                    "已完成的工具与文件结果已保留；"
                                    if retained_results
                                    else ""
                                )
                                + "请继续当前对话，或切换模型后继续。"
                            )
                        },
                    }
                ),
            )
        blocks.append(
            normalize_agent_block(
                {
                    "id": f"{turn_id}:error:0",
                    "type": "error",
                    "status": "failed",
                    "presentationKind": "error",
                    "data": {"message": error_message or "模型请求失败，请重试"},
                }
            )
        )
    if aborted and not blocks:
        blocks.append(
            normalize_agent_block(
                {
                    "id": f"{turn_id}:aborted:0",
                    "type": "text",
                    "status": "aborted",
                    "presentationKind": "markdown",
                    "data": {"text": "已停止。"},
                }
            )
        )
    if not blocks:
        blocks.append(
            normalize_agent_block(
                {
                    "id": f"{turn_id}:progress:0",
                    "type": "progress",
                    "status": "completed",
                    "presentationKind": "progress",
                    "data": {"label": "本轮没有可展示正文"},
                }
            )
        )
    created_at = as_integer(raw.get("timestamp")) or int(time.time() * 1000)
    return AgentMessage(
        message_id=resolved_message_id,
        session_id=session_id,
        turn_id=turn_id,
        role=role,
        status="aborted" if aborted else "failed" if failed else "completed",
        blocks=tuple(blocks),
        attachments=tuple(dict.fromkeys(attachments)),
        created_at_ms=created_at,
        completed_at_ms=created_at,
        client_message_id=str(raw.get("clientMessageId") or ""),
        provider=str(raw.get("provider") or "").strip()[:80],
        model=str(raw.get("responseModel") or raw.get("model") or "").strip()[:160],
        usage=public_usage(raw) if role == "assistant" else None,
    )
