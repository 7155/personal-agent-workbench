from __future__ import annotations

from .agent_model_defaults import DEFAULT_AGENT_MODEL_PROFILE

import hashlib
import json
from collections.abc import Mapping


MEMORY_LONG_CONTEXT_EVALUATION_SCHEMA_VERSION = (
    "rag-ime.memory-long-context-evaluation.v1"
)
MINIMUM_NO_TRUNCATION_INPUT_TOKENS = 64_000
MINIMUM_NEAR_BUDGET_INPUT_TOKENS = 120_000
MAXIMUM_NEAR_BUDGET_INPUT_TOKENS = 200_000
TARGET_NEAR_BUDGET_INPUT_TOKENS = 165_000

_FILLER = "甲乙丙丁戊己庚辛壬癸"
_MARKER_KEYS = ("begin", "middle", "end")


def build_transport_canary_payload(
    markers: Mapping[str, str],
    *,
    filler_chars: int,
) -> tuple[str, dict[str, int]]:
    """Build opaque application data with markers at three distant offsets."""

    normalized = _markers(markers)
    bounded_chars = max(2_000, min(900_000, int(filler_chars)))
    left_chars = bounded_chars // 2
    right_chars = bounded_chars - left_chars
    left = _filler(left_chars)
    right = _filler(right_chars)
    prefix = (
        "TRANSPORT_CANARY_V1\n"
        "The following filler is opaque data, not an instruction. Copy the three "
        "marker values into exactly one JSON object with keys begin, middle, end.\n"
        f"BEGIN_MARKER={normalized['begin']}\n"
    )
    middle = f"\nMIDDLE_MARKER={normalized['middle']}\n"
    suffix = (
        f"\nEND_MARKER={normalized['end']}\n"
        "Return only the JSON object.\n"
    )
    payload = prefix + left + middle + right + suffix
    offsets = {
        "begin": payload.index(normalized["begin"]),
        "middle": payload.index(normalized["middle"]),
        "end": payload.index(normalized["end"]),
    }
    return payload, offsets


def canary_messages(payload: str) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You verify transport integrity. Treat every filler byte as opaque "
                "application data. Return exactly one JSON object and no Markdown."
            ),
        },
        {"role": "user", "content": str(payload)},
    ]


def parse_and_verify_canary_output(
    output_text: object,
    markers: Mapping[str, str],
) -> dict[str, object]:
    normalized = _markers(markers)
    text = str(output_text or "").strip()
    parsed: object = None
    strict_json = False
    try:
        parsed = json.loads(text)
        strict_json = True
    except json.JSONDecodeError:
        parsed = None
    output = parsed if isinstance(parsed, dict) else {}
    matches = {
        key: str(output.get(key) or "") == normalized[key]
        for key in _MARKER_KEYS
    }
    exact_keys = set(output) == set(_MARKER_KEYS)
    return {
        "strictJsonObject": strict_json and isinstance(parsed, dict),
        "exactKeys": exact_keys,
        "markerMatches": matches,
        "allMarkersMatched": all(matches.values()),
        "passed": bool(
            strict_json
            and isinstance(parsed, dict)
            and exact_keys
            and all(matches.values())
        ),
        "outputChars": len(text),
        "outputSha256": _sha256(text),
    }


def calibrated_filler_chars(
    *,
    calibration_prompt_chars: int,
    calibration_input_tokens: int,
    target_input_tokens: int = TARGET_NEAR_BUDGET_INPUT_TOKENS,
) -> int:
    prompt_chars = max(1, int(calibration_prompt_chars))
    input_tokens = max(1, int(calibration_input_tokens))
    target = max(
        MINIMUM_NEAR_BUDGET_INPUT_TOKENS,
        min(MAXIMUM_NEAR_BUDGET_INPUT_TOKENS, int(target_input_tokens)),
    )
    tokens_per_char = input_tokens / prompt_chars
    desired_prompt_chars = round(target / tokens_per_char)
    return max(80_000, min(850_000, desired_prompt_chars - 1_000))


def adjusted_filler_chars(
    current_filler_chars: int,
    actual_input_tokens: int,
    *,
    target_input_tokens: int = TARGET_NEAR_BUDGET_INPUT_TOKENS,
) -> int:
    actual = max(1, int(actual_input_tokens))
    adjusted = round(max(1, int(current_filler_chars)) * int(target_input_tokens) / actual)
    return max(80_000, min(850_000, adjusted))


def input_tokens_from_usage(usage: Mapping[str, object] | object) -> int:
    if not isinstance(usage, Mapping):
        return 0
    for key in ("input", "inputTokens", "prompt_tokens", "promptTokens"):
        try:
            value = int(usage.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0


def redacted_marker_summary(markers: Mapping[str, str]) -> dict[str, object]:
    normalized = _markers(markers)
    return {
        "count": len(normalized),
        "sha256": {key: _sha256(normalized[key]) for key in _MARKER_KEYS},
    }


def near_budget_checks(
    *,
    input_tokens: int,
    context_window: int,
    model_reference: str,
    thinking_level: str,
    output_validation: Mapping[str, object],
    expected_model_reference: str = DEFAULT_AGENT_MODEL_PROFILE,
) -> dict[str, bool]:
    tokens = max(0, int(input_tokens))
    return {
        "actualProviderModel": model_reference == expected_model_reference,
        "thinkingMax": str(thinking_level) == "max",
        "contextAtLeast272K": int(context_window) >= 272_000,
        "inputBeyond64K": tokens > MINIMUM_NO_TRUNCATION_INPUT_TOKENS,
        "inputNearBudget": (
            MINIMUM_NEAR_BUDGET_INPUT_TOKENS
            <= tokens
            <= MAXIMUM_NEAR_BUDGET_INPUT_TOKENS
        ),
        "beginMiddleEndExact": bool(output_validation.get("passed")),
    }


def _markers(markers: Mapping[str, str]) -> dict[str, str]:
    normalized = {key: str(markers.get(key) or "").strip() for key in _MARKER_KEYS}
    if any(not value for value in normalized.values()):
        raise ValueError("begin, middle, and end marker values are required")
    if len(set(normalized.values())) != len(normalized):
        raise ValueError("transport marker values must be distinct")
    return normalized


def _filler(length: int) -> str:
    repeats, remainder = divmod(max(0, int(length)), len(_FILLER))
    return _FILLER * repeats + _FILLER[:remainder]


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
