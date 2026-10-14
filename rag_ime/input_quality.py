from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from .input_capture_contract import (
    InputCaptureContractError,
    capture_contract_from_metadata,
)

from .text_utils import compact_whitespace


FINALIZED_INPUT_SOURCE = "squirrel_input_segment"
RIME_FRAGMENT_SOURCE = "squirrel_rime_commit_burst"
MEMORY_CONTEXT_OPT_IN_TAG = "memory-context-opt-in"
DISABLED_CONTEXT_SOURCES = frozenset({"codex_history"})

_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+#@-]*")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_WORD_RE = re.compile(r"[A-Za-z0-9]+|[\u3400-\u4dbf\u4e00-\u9fff]+")
_SENTENCE_END_RE = re.compile(r"[。！？!?；;.]$")
_ONLY_SYMBOLS_RE = re.compile(r"^[\W_]+$", re.UNICODE)

MODEL_GENERATED_EVENT_SOURCES = frozenset(
    {
        "api_core_optimizer",
        "api_lexicon_optimizer",
        "squirrel_rime_sidecar",
        "vcp_memory_generator",
    }
)
# Historical source values remain readable; no new event emits the retired name.
INTERNAL_EVENT_SOURCES = MODEL_GENERATED_EVENT_SOURCES | {"ime_demo_fixture"}
_TRANSPORT_TAGS = {
    "group-buffer",
    "rime-commit",
    "rime-sidecar",
    "sidecar-selected",
    "source:model",
    "squirrel",
}
_LOW_SIGNAL_PHRASES = {
    "不是",
    "那个",
    "这个",
    "然后",
    "可以",
    "好的",
    "稳定",
    "弹出",
    "继续",
    "ai",
    "ok",
    "test",
    "测试",
}


@dataclass(frozen=True)
class InputQualityAssessment:
    complete: bool
    injectable: bool
    memory_eligible: bool
    reasons: tuple[str, ...]

    def payload(self) -> dict[str, object]:
        return {
            "complete": self.complete,
            "injectable": self.injectable,
            "memoryEligible": self.memory_eligible,
            "qualityReasons": list(self.reasons),
        }


def assess_input_text(
    text: str,
    *,
    source: str,
    source_count: int = 1,
    finalized: bool | None = None,
    reconstructed: bool = False,
    tags: Iterable[str] = (),
    capture_metadata: Mapping[str, object] | None = None,
    app: str = "",
) -> InputQualityAssessment:
    """Classify input before it can enter model context or long-term memory.

    The lightning Active-RAG action intentionally bypasses this projection and
    reads Squirrel's process-local editable buffer. Every persisted/model-facing
    path must pass this gate instead.
    """

    value = compact_whitespace(text)
    normalized_source = compact_whitespace(source).lower()
    normalized_tags = {compact_whitespace(tag).lower() for tag in tags if compact_whitespace(tag)}
    explicit_finalized = (
        normalized_source == FINALIZED_INPUT_SOURCE
        or "finalized" in normalized_tags
        or "complete-input" in normalized_tags
    )
    capture_contract = None
    capture_contract_invalid = False
    try:
        capture_contract = capture_contract_from_metadata(
            capture_metadata or {},
            text=value,
            source=normalized_source,
            app=app or str((capture_metadata or {}).get("appBundleId") or ""),
        )
    except InputCaptureContractError:
        capture_contract_invalid = True
    trusted_capture_boundary = bool(
        capture_contract is not None and capture_contract.is_strong_final
    )
    boundary_finalized = (
        explicit_finalized if finalized is None else bool(finalized)
    ) or trusted_capture_boundary
    reasons: list[str] = []

    if not source_context_enabled(normalized_source, tags=normalized_tags):
        reasons.append("source_not_enabled")
    if normalized_source in INTERNAL_EVENT_SOURCES:
        reasons.append("transport_or_generated_source")
    if not value:
        reasons.append("empty")
    elif _ONLY_SYMBOLS_RE.fullmatch(value):
        reasons.append("symbols_only")
    if _looks_repeated(value):
        reasons.append("repeated_noise")

    lowered = value.casefold()
    if lowered in _LOW_SIGNAL_PHRASES:
        reasons.append("known_low_signal_fragment")

    cjk_count = len(_CJK_RE.findall(value))
    word_tokens = _WORD_RE.findall(value)
    ascii_only_token = bool(_ASCII_TOKEN_RE.fullmatch(value))
    sentence_ended = bool(_SENTENCE_END_RE.search(value))

    if ascii_only_token:
        reasons.append("isolated_ascii_token")
    if cjk_count and cjk_count <= 5 and not sentence_ended and len(word_tokens) <= 2:
        reasons.append("short_cjk_fragment")
    if not cjk_count and len(word_tokens) < 2 and not sentence_ended:
        reasons.append("single_word")

    if normalized_source == FINALIZED_INPUT_SOURCE and not boundary_finalized:
        reasons.append("segment_not_finalized")
    if normalized_source == RIME_FRAGMENT_SOURCE and not reconstructed:
        reasons.append("raw_rime_fragment")
    if normalized_source == RIME_FRAGMENT_SOURCE and reconstructed and not boundary_finalized:
        reasons.append("missing_finalized_boundary")
    if capture_contract_invalid:
        reasons.append("invalid_capture_v2")

    substantial = sentence_ended or cjk_count >= 6 or len(word_tokens) >= 3
    complete = bool(
        value
        and substantial
        and (
            boundary_finalized
            or normalized_source not in {FINALIZED_INPUT_SOURCE, RIME_FRAGMENT_SOURCE}
        )
    )
    hard_failures = {
        "source_not_enabled",
        "transport_or_generated_source",
        "empty",
        "symbols_only",
        "repeated_noise",
        "known_low_signal_fragment",
        "isolated_ascii_token",
        "short_cjk_fragment",
        "single_word",
        "segment_not_finalized",
        "raw_rime_fragment",
        "missing_finalized_boundary",
    }
    injectable = complete and not hard_failures.intersection(reasons)

    # Long-term memory is intentionally stricter than recent Agent context.
    # Punctuation can close an utterance, but it cannot turn a short word or
    # fragment into durable memory.  AX/local context may help interpret a
    # complete input later; it must never supply the missing durable claim.
    durable_signal = (
        cjk_count >= 8
        or len(word_tokens) >= 4
        or (sentence_ended and cjk_count >= 6)
        or (sentence_ended and not cjk_count and len(word_tokens) >= 3)
    )
    explicit_memory_boundary = normalized_source == "squirrel_assistant_remember"
    memory_boundary_trusted = trusted_capture_boundary or explicit_memory_boundary
    memory_eligible = injectable and durable_signal and memory_boundary_trusted
    if injectable and not durable_signal:
        reasons.append("insufficient_durable_signal")
    if injectable and not memory_boundary_trusted:
        reasons.append("untrusted_memory_boundary")

    if not complete and not reasons:
        reasons.append("incomplete_expression")
    if normalized_tags.intersection(_TRANSPORT_TAGS):
        reasons.append("transport_tags_ignored")

    return InputQualityAssessment(
        complete=complete,
        injectable=injectable,
        memory_eligible=memory_eligible,
        reasons=tuple(dict.fromkeys(reasons)),
    )


def source_context_enabled(source: str, *, tags: Iterable[str] = ()) -> bool:
    """Keep disabled bulk-history sources out unless a caller opts in explicitly."""

    normalized_source = compact_whitespace(source).lower()
    normalized_tags = {
        compact_whitespace(tag).lower()
        for tag in tags
        if compact_whitespace(tag)
    }
    return (
        normalized_source not in DISABLED_CONTEXT_SOURCES
        or MEMORY_CONTEXT_OPT_IN_TAG in normalized_tags
    )


def _looks_repeated(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    if len(compact) < 3:
        return False
    if len(set(compact)) == 1:
        return True
    for width in range(1, min(8, len(compact) // 2) + 1):
        unit = compact[:width]
        if unit * (len(compact) // width) == compact and len(compact) % width == 0:
            return True
    return False
