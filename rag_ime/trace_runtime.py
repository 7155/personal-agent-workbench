"""Small, versioned Trace/Eval contracts shared by future Agent applications.

This module deliberately owns only immutable, privacy-safe records.  It does
not persist traces, call a model, or decide whether a product result is good.
Runtime producers (Session, Room, RAG, Memory, and vertical apps) can all emit
the same envelope and attach their own source-specific details in spans and
evidence references.

Ground-truth metrics and AI Judge estimates are separate at construction time;
an estimate can never be labelled as precision, recall, or F1 by this API.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from dataclasses import dataclass, field
from collections.abc import Mapping, Sequence
from .contracts.json_schema import ContractValidationError, validate_contract


TRACE_SCHEMA_VERSION = "rag-ime.trace-envelope.v1"
EVAL_SCHEMA_VERSION = "rag-ime.eval-run.v1"
SANDBOX_SCHEMA_VERSION = "rag-ime.sandbox-run.v1"
_DETERMINISTIC_METRICS = frozenset({"accuracy", "precision", "recall", "f1", "recallAtK", "mrr", "ndcgAtK"})
_JUDGE_METRICS = frozenset({"relevance", "coverage", "groundedness", "contradiction", "confidence"})
_PUBLIC_SPAN_METRICS = frozenset(
    {
        "argumentFieldCount",
        "attachmentCount",
        "blockCount",
        "cacheHitPercent",
        "cacheReadTokens",
        "cacheWriteTokens",
        "candidateCount",
        "characterCount",
        "compactAtTokens",
        "compactionCount",
        "contextChars",
        "contextPercent",
        "contextTokens",
        "contextWindowTokens",
        "count",
        "changeCount",
        "elapsedMs",
        "estimatedTokensAfter",
        "evidenceCount",
        "eventCount",
        "inputTokens",
        "isError",
        "maxItems",
        "messageCount",
        "modelElapsedMs",
        "omittedCount",
        "outputTokens",
        "pendingDraftCount",
        "pendingEventCount",
        "providedEvidenceCount",
        "ragElapsedMs",
        "recentCompleteInputCount",
        "recentConversationCount",
        "remainingTokens",
        "resultFieldCount",
        "selectedChars",
        "selectedCount",
        "tokensBefore",
        "tokensUntilCompact",
        "totalTokens",
        "usedChars",
        "reused",
    }
)
_PUBLIC_SPAN_BOOLEAN_ATTRIBUTES = frozenset(
    {
        "due",
        "embeddingFallback",
        "failureRecorded",
        "invalidResumeToken",
        "isCompacting",
        "modelActiveGeneration",
        "modelCalled",
        "modelStaleDropped",
        "modelTimedOut",
        "modelWaitingForLatest",
        "parentUnavailable",
        "ragActiveGeneration",
        "ragCalled",
        "ragStaleDropped",
        "ragTimedOut",
        "ragWaitingForLatest",
        "rawKnowledgeTextStored",
        "rawMemoryTextStored",
        "rawMessageStored",
        "rawReasoningStored",
        "rawResultStored",
        "rawTextStored",
        "sourceRawTextIncluded",
        "terminalDerivedFromDispatches",
        "terminalDerivedFromMaintenancePhase",
        "willRetry",
    }
)
_PUBLIC_SPAN_INTEGER_ATTRIBUTES = frozenset(
    {
        "frontendRevision",
        "modelPredictionCount",
        "modelSuggestionCount",
        "ragPredictionCount",
        "ragSuggestionCount",
        "requestSeq",
        "resultCount",
        "selectionEpoch",
        "terminalDispatchCount",
        "visibleCandidateCount",
        "workItemRevision",
    }
)
_PUBLIC_SPAN_FINGERPRINT_ATTRIBUTES = frozenset({"answerFingerprint"})
_PUBLIC_FAILURE_REASON_VALUES = frozenset(
    {
        "all_error_recovery_timeout",
        "consecutive_all_error_turns",
        "no_progress",
        "provider_auth_failure",
        "provider_cancelled",
        "provider_contract_failure",
        "provider_failure_unclassified",
        "provider_overloaded",
        "provider_rate_limited",
        "provider_timeout",
        "provider_transport_failure",
        "repeated_failure_signature",
        "tool_activity_observed",
    }
)
_PUBLIC_SPAN_IDENTIFIER_ATTRIBUTES = frozenset(
    {
        "action",
        "activityKind",
        "attemptId",
        "app",
        "commandId",
        "deviceId",
        "evidenceStage",
        "failureKind",
        "frontAppBundleId",
        "intent",
        "intercomKind",
        "knowledgeBaseId",
        "lifecycleAuthority",
        "memoryId",
        "model",
        "panelSessionId",
        "parentUnavailableReason",
        "participantState",
        "phase",
        "postKind",
        "producerKind",
        "provider",
        "project",
        "receiptId",
        "requestKind",
        "retrievalMode",
        "riskLevel",
        "role",
        "roomEventType",
        "runtimeState",
        "sourceEventType",
        "sourceKind",
        "targetParticipantId",
        "terminalPhaseStatus",
        "toolName",
        "trigger",
        "workState",
    }
)
_PUBLIC_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}\Z")
_SHA256_FINGERPRINT_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_PUBLIC_SPAN_FIELDS = 64

# Trace records are metadata-only by contract.  These grammars intentionally
# describe identifiers and labels, rather than attempting to redact arbitrary
# prose after it has entered a dataclass.  Keep the grammar shared by the
# builder, dataclass constructors, and persisted-payload validators so a
# caller cannot bypass the projection by using ``TraceSpan(...)`` or
# ``dataclasses.asdict`` directly.
_SAFE_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")
_SAFE_LABEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .:_-]{0,95}\Z")
_SAFE_SCHEME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9+.-]{0,31}\Z")
_SAFE_AUTHORITY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SAFE_URI_PATH_PATTERN = re.compile(
    r"[A-Za-z0-9_.~!$&'()*+,;=:@%:-]+(?:/[A-Za-z0-9_.~!$&'()*+,;=:@%:-]+)*\Z"
)
_SAFE_MIME_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}\Z"
)
_TRACE_STATUSES = frozenset({"building", "completed", "failed", "cancelled"})
_SPAN_STATUSES = frozenset(
    {"queued", "running", "waiting", "completed", "failed", "cancelled", "info"}
)
_BINDING_KEYS = frozenset(
    {"sessionId", "turnId", "roomId", "runId", "sourceLoopId", "workItemId", "caseId"}
)
_TRACE_LINK_RELATIONS = frozenset({"retry", "related"})
_TRACE_LINK_TARGET_KINDS = frozenset({"trace"})
_TRACE_LINK_KEYS = frozenset({"traceId", "relation", "targetKind"})
_MAX_TRACE_LINKS = 64
_UNAVAILABLE_REASONS = frozenset(
    {"duration_not_recorded", "inconsistent_timing", "judge_not_configured"}
)
_EVIDENCE_DISPOSITIONS = frozenset({"included", "omitted", "filtered", "redacted"})
_EVIDENCE_KEYS = frozenset(
    {
        "evidenceId",
        "sourceKind",
        "sourceRef",
        "sourceLane",
        "evidenceStage",
        "disposition",
        "scores",
        "rankBefore",
        "rankAfter",
        "omissionReason",
    }
)
_ARTIFACT_KEYS = frozenset(
    {"artifactId", "kind", "mediaType", "sha256", "byteSize", "recordCount"}
)
_EVAL_MODES = frozenset({"ground_truth", "ai_judge"})
_EVAL_TRUTH_STATUSES = frozenset({"none", "human", "frozen"})
_EVAL_STATUSES = frozenset({"queued", "running", "completed", "failed"})
_EVALUATOR_KEYS = frozenset({"provider", "model", "thinking", "displayName"})
_EVAL_SUITE_BINDING_KEYS = frozenset({"suiteId", "suiteRevision"})
_EVAL_USAGE_KEYS = frozenset({"input", "output", "cacheRead", "cacheWrite", "totalTokens"})
_EVAL_COST_KEYS = frozenset({"input", "output", "cacheRead", "cacheWrite", "total"})
_EVAL_FAILURE_CODES = frozenset(
    {
        "ai_judge_runtime_unavailable",
        "ai_judge_request_failed",
        "ai_judge_invalid_response",
        "ai_judge_timeout",
    }
)
_SANDBOX_STATUSES = frozenset({"queued", "running", "completed", "failed", "cancelled"})
_SANDBOX_MUTATION_MODES = frozenset({"read_only", "staged"})
_SANDBOX_NETWORK_MODES = frozenset({"blocked", "allowlisted"})
_SANDBOX_POLICY_KEYS = frozenset(
    {
        "workspaceBindingId",
        "workspaceFingerprint",
        "mutationMode",
        "network",
        "productionWriteBlocked",
    }
)
_SANDBOX_REPLAY_COHORT_KEYS = frozenset(
    {
        "suiteId",
        "suiteRevision",
        "caseId",
        "inputFingerprint",
        "environmentFingerprint",
        "configFingerprint",
        "modelProfileFingerprint",
        "toolProfileFingerprint",
        "skillProfileFingerprint",
    }
)
_MAX_EVIDENCE_SCORES = 16


class TraceContractError(ValueError):
    """A producer attempted to emit an unsafe or semantically ambiguous record."""


class _FrozenDict(dict):
    """A JSON-shaped mapping that cannot be mutated after projection.

    ``dataclasses.asdict`` treats dict subclasses as mappings, so this keeps
    the familiar shape while closing the shallow-mutation hole left by a
    frozen dataclass containing an ordinary nested ``dict``.
    """

    __slots__ = ()

    def _immutable(self, *args, **kwargs):
        raise TypeError("projected trace mappings are immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _immutable

    def __ior__(self, other):
        return self._immutable(other)

    def __deepcopy__(self, memo):
        return self


def _now_ms() -> int:
    return int(time.time() * 1000)


def _required(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise TraceContractError(f"{name} is required")
    return text


def _safe_token(value: object, name: str, *, maximum: int = 160) -> str:
    """Validate one opaque identifier without retaining whitespace or paths."""

    if not isinstance(value, str) or not value:
        raise TraceContractError(f"{name} must be a non-empty identifier")
    if len(value) > maximum or value != value.strip() or any(char.isspace() for char in value):
        raise TraceContractError(f"{name} must be a bounded token")
    if "://" in value or not _SAFE_TOKEN_PATTERN.fullmatch(value):
        raise TraceContractError(f"{name} must be a bounded token")
    return value


def _safe_optional_token(value: object, name: str, *, maximum: int = 160) -> str | None:
    if value is None or value == "":
        return None
    return _safe_token(value, name, maximum=maximum)


def _safe_label(value: object, name: str, *, maximum: int = 96) -> str:
    """Validate a short human/provenance label, never arbitrary prose."""

    if not isinstance(value, str) or not value:
        raise TraceContractError(f"{name} must be a non-empty label")
    if len(value) > maximum or value != value.strip() or any(char.isspace() and char != " " for char in value):
        raise TraceContractError(f"{name} must be a bounded label")
    if not _SAFE_LABEL_PATTERN.fullmatch(value) or "://" in value:
        raise TraceContractError(f"{name} must be a bounded label")
    # A display label is allowed to contain a few short words (the current
    # values are ``Human labels``, ``Frozen labels``, and ``Luna Max``), but a
    # sentence, URL, path, or all-caps raw sentinel is not a provenance label.
    if (
        len(value.split(" ")) > 3
        or (" " in value and not value[0].isupper())
        or (value.isupper() and any(char.isalpha() for char in value))
    ):
        raise TraceContractError(f"{name} must be a bounded label")
    return value


def _safe_public_identifier(value: object, name: str) -> str:
    """Validate a whitelisted span attribute that carries an identifier."""

    if not isinstance(value, str) or not value or value != value.strip() or any(char.isspace() for char in value):
        raise TraceContractError(f"{name} must be a bounded identifier")
    if not _PUBLIC_IDENTIFIER_PATTERN.fullmatch(value):
        raise TraceContractError(f"{name} must be a bounded identifier")
    # Keep compatibility with provider/model references such as
    # ``openrouter/claude`` while rejecting URL credentials, URL parameters,
    # absolute/relative filesystem paths, and URI schemes.
    if (
        "://" in value
        or "?" in value
        or "#" in value
        or "@" in value
        or "\\" in value
        or value.startswith(("/", "~"))
        or re.match(r"^[A-Za-z]:/", value)
        or any(segment in {".", ".."} for segment in value.split("/"))
    ):
        raise TraceContractError(f"{name} must be a bounded identifier")
    return value


def _safe_source_ref(value: object, name: str = "sourceRef") -> str:
    """Validate opaque evidence refs and safe scheme-qualified refs.

    Opaque refs (``doc:revenue`` / ``sales-ledger``) use the token grammar.
    Scheme-qualified refs (``knowledge://sales/revenue`` or
    ``fixture://memory/sgg/policy``) may contain a bounded authority/path, but
    may not contain credentials, query strings, fragments, whitespace, or
    filesystem traversal.
    """

    if not isinstance(value, str) or not value or len(value) > 512:
        raise TraceContractError(f"{name} must be a safe evidence reference")
    if value != value.strip() or any(char.isspace() for char in value):
        raise TraceContractError(f"{name} must be a safe evidence reference")
    if any(char in value for char in ("?", "#", "@", "\\")):
        raise TraceContractError(f"{name} must be a safe evidence reference")
    if value.startswith(("/", "~")) or re.match(r"^[A-Za-z]:[/\\]", value):
        raise TraceContractError(f"{name} must be a safe evidence reference")

    if "://" not in value:
        if (
            ":" in value
            and value.split(":", 1)[0].lower() in {"file", "http", "https"}
        ):
            raise TraceContractError(f"{name} must be a safe evidence reference")
        return _safe_token(value, name, maximum=512)

    if value.count("://") != 1:
        raise TraceContractError(f"{name} must be a safe evidence reference")
    scheme, remainder = value.split("://", 1)
    if (
        scheme.lower() not in {"knowledge", "fixture"}
        or not _SAFE_SCHEME_PATTERN.fullmatch(scheme)
        or not remainder
    ):
        raise TraceContractError(f"{name} must be a safe evidence reference")
    authority, separator, path = remainder.partition("/")
    if not _SAFE_AUTHORITY_PATTERN.fullmatch(authority):
        raise TraceContractError(f"{name} must be a safe evidence reference")
    if separator:
        if not path or not _SAFE_URI_PATH_PATTERN.fullmatch(path):
            raise TraceContractError(f"{name} must be a safe evidence reference")
        if any(segment in {".", ".."} for segment in path.split("/")):
            raise TraceContractError(f"{name} must be a safe evidence reference")
    return value


def _safe_non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TraceContractError(f"{name} must be a non-negative integer")
    return value


def _raise_invalid_record(name: str):
    raise TraceContractError(f"trace {name} record is invalid")


def _safe_optional_rank(value: object, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise TraceContractError(f"{name} must be a positive integer or null")
    return value


def _safe_status(value: object, name: str, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise TraceContractError(f"{name} is not a supported status")
    return value


def _safe_unavailable_reason(value: object, *, required: bool) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise TraceContractError("unavailableReason must be a closed reason")
    if not value:
        if required:
            raise TraceContractError("unrecorded span requires unavailable_reason")
        return ""
    if value not in _UNAVAILABLE_REASONS:
        raise TraceContractError("unavailableReason must be a closed reason")
    return value


def _safe_input_normalization(value: object) -> str:
    return _safe_token(value, "input_normalization")


def _validated_binding(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise TraceContractError("trace binding must be a mapping")
    if len(value) > len(_BINDING_KEYS):
        raise TraceContractError("trace binding contains unsupported keys")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or key not in _BINDING_KEYS:
            raise TraceContractError(f"unsupported trace binding key: {key}")
        result[key] = _safe_token(item, f"binding {key}")
    return result


def _project_trace_link_payload(value: Mapping[str, object]) -> dict[str, str]:
    """Project one bounded, typed relationship to another Trace envelope."""

    payload = dict(value)
    if set(payload) != _TRACE_LINK_KEYS:
        raise TraceContractError("trace link contains unsupported fields")
    trace_id = _safe_token(payload.get("traceId"), "trace link traceId")
    relation = payload.get("relation")
    if not isinstance(relation, str) or relation not in _TRACE_LINK_RELATIONS:
        raise TraceContractError("trace link relation is not supported")
    target_kind = payload.get("targetKind")
    if not isinstance(target_kind, str) or target_kind not in _TRACE_LINK_TARGET_KINDS:
        raise TraceContractError("trace link targetKind is not supported")
    return {"traceId": trace_id, "relation": relation, "targetKind": target_kind}


def _project_trace_links(
    value: object,
    *,
    current_trace_id: str | None = None,
) -> tuple[Mapping[str, str], ...]:
    """Validate and stable-deduplicate links without retaining arbitrary JSON."""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TraceContractError("trace links must be a sequence")
    if len(value) > _MAX_TRACE_LINKS:
        raise TraceContractError("trace links exceed the public limit")
    projected: list[Mapping[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in value:
        if isinstance(item, TraceLink):
            payload = item.to_dict()
        elif isinstance(item, Mapping):
            payload = item
        else:
            raise TraceContractError("trace links must contain mappings")
        safe_item = _project_trace_link_payload(payload)
        identity = (
            safe_item["traceId"],
            safe_item["relation"],
            safe_item["targetKind"],
        )
        if current_trace_id is not None and safe_item["traceId"] == current_trace_id:
            raise TraceContractError("trace link cannot target its own trace")
        if identity in seen:
            continue
        seen.add(identity)
        projected.append(safe_item)
    return tuple(projected)


def _validated_scores(value: object) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise TraceContractError("evidence scores must be a mapping")
    if len(value) > _MAX_EVIDENCE_SCORES:
        raise TraceContractError("evidence scores exceed the public field limit")
    result: dict[str, float] = {}
    for key, raw_value in value.items():
        safe_key = _safe_token(key, "evidence score name", maximum=80)
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise TraceContractError(f"evidence score {safe_key} must be numeric")
        score = float(raw_value)
        if not math.isfinite(score):
            raise TraceContractError(f"evidence score {safe_key} must be finite")
        result[safe_key] = score
    return result


def _project_evidence_payload(value: Mapping[str, object]) -> dict[str, object]:
    payload = dict(value)
    payload.setdefault("sourceLane", "")
    payload.setdefault("evidenceStage", "observation_ref")
    payload.setdefault("scores", {})
    payload.setdefault("rankBefore", None)
    payload.setdefault("rankAfter", None)
    payload.setdefault("omissionReason", "")
    if set(payload) != _EVIDENCE_KEYS:
        raise TraceContractError("evidence reference contains unsupported fields")
    safe_disposition = payload.get("disposition")
    if not isinstance(safe_disposition, str) or safe_disposition not in _EVIDENCE_DISPOSITIONS:
        raise TraceContractError("evidence disposition is not supported")
    omission_reason = payload.get("omissionReason")
    if omission_reason == "":
        safe_omission_reason = ""
    else:
        safe_omission_reason = _safe_token(omission_reason, "omissionReason", maximum=160)
    if safe_disposition == "included" and safe_omission_reason:
        raise TraceContractError("included evidence cannot carry omissionReason")
    if safe_disposition != "included" and not safe_omission_reason:
        raise TraceContractError("omitted evidence requires omissionReason")
    source_lane = payload.get("sourceLane")
    safe_source_lane = "" if source_lane == "" else _safe_token(source_lane, "sourceLane")
    safe_stage = _safe_token(payload.get("evidenceStage"), "evidenceStage")
    return {
        "evidenceId": _safe_token(payload.get("evidenceId"), "evidenceId"),
        "sourceKind": _safe_token(payload.get("sourceKind"), "evidence sourceKind"),
        "sourceRef": _safe_source_ref(payload.get("sourceRef")),
        "sourceLane": safe_source_lane,
        "evidenceStage": safe_stage,
        "disposition": safe_disposition,
        "scores": _validated_scores(payload.get("scores")),
        "rankBefore": _safe_optional_rank(payload.get("rankBefore"), "rankBefore"),
        "rankAfter": _safe_optional_rank(payload.get("rankAfter"), "rankAfter"),
        "omissionReason": safe_omission_reason,
    }


def _freeze_evidence_payload(value: Mapping[str, object]) -> Mapping[str, object]:
    """Freeze both the evidence record and its nested score mapping."""

    result = dict(value)
    scores = result.get("scores")
    if not isinstance(scores, Mapping):  # projected evidence always has scores
        raise TraceContractError("evidence scores must be a mapping")
    result["scores"] = _FrozenDict(dict(scores))
    return _FrozenDict(result)


def _project_artifact_payload(value: Mapping[str, object]) -> dict[str, object]:
    payload = dict(value)
    if set(payload) != _ARTIFACT_KEYS:
        raise TraceContractError("artifact reference contains unsupported fields")
    media_type = payload.get("mediaType")
    if not isinstance(media_type, str) or not _SAFE_MIME_PATTERN.fullmatch(media_type):
        raise TraceContractError("artifact mediaType must be a safe MIME type")
    sha256 = payload.get("sha256")
    if not isinstance(sha256, str) or not re.fullmatch(r"[a-f0-9]{64}\Z", sha256):
        raise TraceContractError("artifact sha256 must be a lowercase SHA-256")
    return {
        "artifactId": _safe_token(payload.get("artifactId"), "artifactId"),
        "kind": _safe_token(payload.get("kind"), "artifact kind"),
        "mediaType": media_type,
        "sha256": sha256,
        "byteSize": _safe_non_negative_int(payload.get("byteSize"), "byteSize"),
        "recordCount": _safe_non_negative_int(payload.get("recordCount"), "recordCount"),
    }


def fingerprint_text(value: str) -> str:
    """Return a full SHA-256 fingerprint; raw input is never stored by default."""

    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def project_public_span_metrics(
    value: object,
    *,
    strict: bool,
) -> dict[str, int | float | bool]:
    """Return the closed, numeric public metric projection for one span.

    Direct Trace producers use ``strict=True`` and fail on unknown fields.
    Adapters over older Observation records use ``strict=False`` so legacy
    private metadata is dropped instead of making the whole trace unreadable.
    """

    if not isinstance(value, Mapping):
        if strict:
            raise TraceContractError("span metrics must be a mapping")
        return {}
    if len(value) > _MAX_PUBLIC_SPAN_FIELDS and strict:
        raise TraceContractError("span metrics exceed the public field limit")
    result: dict[str, int | float | bool] = {}
    for raw_key, raw_value in list(value.items())[:_MAX_PUBLIC_SPAN_FIELDS]:
        if not isinstance(raw_key, str) or raw_key not in _PUBLIC_SPAN_METRICS:
            if strict:
                raise TraceContractError(f"unsupported public span metric: {raw_key}")
            continue
        if raw_key in {"isError", "reused"}:
            if not isinstance(raw_value, bool):
                if strict:
                    raise TraceContractError(
                        f"span metric {raw_key} must be boolean"
                    )
                continue
            result[raw_key] = raw_value
            continue
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            if strict:
                raise TraceContractError(f"span metric {raw_key} must be numeric")
            continue
        number = float(raw_value)
        if not math.isfinite(number) or number < 0:
            if strict:
                raise TraceContractError(
                    f"span metric {raw_key} must be finite and non-negative"
                )
            continue
        result[raw_key] = raw_value
    return result


def _public_optimization_execution(value: object) -> Mapping[str, object]:
    """Closed, metadata-only marker emitted by a registered execution owner."""
    expected = {"candidateId", "role", "comparisonContractSha256", "controls", "loadedVersions"}
    if not isinstance(value, Mapping) or not expected <= set(value) or set(value) - expected - {"fixedContextFingerprints"}:
        raise TraceContractError("optimization execution marker is incomplete")
    candidate = _safe_public_identifier(value["candidateId"], "candidateId")
    if value["role"] not in {"baseline", "candidate"}:
        raise TraceContractError("optimization execution role is invalid")
    digest = value["comparisonContractSha256"]
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise TraceContractError("optimization contract fingerprint is invalid")
    controls, versions = value["controls"], value["loadedVersions"]
    required_controls = {"inputState", "evaluator", "qualityPolicy", "permissions", "environment"}
    if not isinstance(controls, Mapping) or not required_controls <= set(controls) or len(controls) > 32:
        raise TraceContractError("optimization controls are incomplete")
    if not isinstance(versions, Mapping) or set(versions) != {"tool", "skill", "prompt", "workflow", "model"}:
        raise TraceContractError("optimization loaded versions are incomplete")
    safe_controls = {_safe_public_identifier(key, "control key"): _safe_public_identifier(item, "control identity") for key, item in controls.items()}
    safe_versions = {key: _safe_public_identifier(item, "loaded version identity") for key, item in versions.items()}
    result = {"candidateId": candidate, "role": value["role"], "comparisonContractSha256": digest, "controls": _FrozenDict(safe_controls), "loadedVersions": _FrozenDict(safe_versions)}
    if "fixedContextFingerprints" in value:
        fixed = value["fixedContextFingerprints"]
        if not isinstance(fixed, Mapping) or len(fixed) > 32:
            raise TraceContractError("optimization fixed context metadata is invalid")
        result["fixedContextFingerprints"] = _FrozenDict({_safe_public_identifier(key, "fixed context key"): _safe_public_identifier(item, "fixed context identity") for key, item in fixed.items()})
    return _FrozenDict(result)


def project_public_span_attributes(
    value: object,
    *,
    strict: bool,
) -> dict[str, object]:
    """Return the closed public metadata projection for one span."""

    if not isinstance(value, Mapping):
        if strict:
            raise TraceContractError("span attributes must be a mapping")
        return {}
    if len(value) > _MAX_PUBLIC_SPAN_FIELDS and strict:
        raise TraceContractError("span attributes exceed the public field limit")
    result: dict[str, object] = {}
    for raw_key, raw_value in list(value.items())[:_MAX_PUBLIC_SPAN_FIELDS]:
        if not isinstance(raw_key, str):
            if strict:
                raise TraceContractError(f"unsupported public span attribute: {raw_key}")
            continue
        if raw_key == "traceOptimization":
            try:
                result[raw_key] = _public_optimization_execution(raw_value)
            except TraceContractError:
                if strict:
                    raise
            continue
        if raw_key in _PUBLIC_SPAN_BOOLEAN_ATTRIBUTES:
            valid = isinstance(raw_value, bool)
        elif raw_key in _PUBLIC_SPAN_INTEGER_ATTRIBUTES:
            valid = (
                isinstance(raw_value, int)
                and not isinstance(raw_value, bool)
                and raw_value >= 0
            )
        elif raw_key in _PUBLIC_SPAN_FINGERPRINT_ATTRIBUTES:
            valid = isinstance(raw_value, str) and bool(
                _SHA256_FINGERPRINT_PATTERN.fullmatch(raw_value)
            )
        elif raw_key == "failureReason":
            valid = (
                isinstance(raw_value, str)
                and raw_value in _PUBLIC_FAILURE_REASON_VALUES
            )
        elif raw_key in _PUBLIC_SPAN_IDENTIFIER_ATTRIBUTES:
            try:
                _safe_public_identifier(raw_value, f"span attribute {raw_key}")
            except TraceContractError:
                valid = False
            else:
                valid = True
        else:
            valid = False
        if not valid:
            if strict:
                raise TraceContractError(
                    f"unsupported public span attribute or value: {raw_key}"
                )
            continue
        result[raw_key] = raw_value
    return result


@dataclass(frozen=True, slots=True)
class TraceSpan:
    span_id: str
    name: str
    parent_span_id: str | None
    status: str
    started_at_ms: int
    ended_at_ms: int | None
    duration_ms: int | None
    recorded: bool
    unavailable_reason: str
    metrics: Mapping[str, object] = field(default_factory=dict)
    attributes: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        safe_span_id = _safe_token(self.span_id, "span_id")
        safe_name = _safe_token(self.name, "span name")
        safe_parent = _safe_optional_token(self.parent_span_id, "parent_span_id")
        safe_status = _safe_status(self.status, "span status", _SPAN_STATUSES)
        safe_started = _safe_non_negative_int(self.started_at_ms, "span start")
        safe_ended = (
            None
            if self.ended_at_ms is None
            else _safe_non_negative_int(self.ended_at_ms, "span end")
        )
        safe_duration = (
            None
            if self.duration_ms is None
            else _safe_non_negative_int(self.duration_ms, "span duration")
        )
        if not isinstance(self.recorded, bool):
            raise TraceContractError("span recorded must be boolean")
        safe_reason = _safe_unavailable_reason(
            self.unavailable_reason,
            required=not self.recorded,
        )
        safe_metrics = project_public_span_metrics(self.metrics, strict=True)
        safe_attributes = project_public_span_attributes(self.attributes, strict=True)
        if self.recorded:
            if safe_ended is None:
                raise TraceContractError("recorded span requires ended_at_ms")
            expected_duration = safe_ended - safe_started
            if expected_duration < 0:
                raise TraceContractError("span end must not precede start")
            if safe_duration != expected_duration:
                raise TraceContractError("recorded span duration must equal end minus start")
            if safe_reason:
                raise TraceContractError("recorded span cannot carry unavailableReason")
        else:
            if safe_ended is not None or safe_duration is not None:
                raise TraceContractError("unrecorded span cannot carry timing")
        object.__setattr__(self, "span_id", safe_span_id)
        object.__setattr__(self, "name", safe_name)
        object.__setattr__(self, "parent_span_id", safe_parent)
        object.__setattr__(self, "status", safe_status)
        object.__setattr__(self, "started_at_ms", safe_started)
        object.__setattr__(self, "ended_at_ms", safe_ended)
        object.__setattr__(self, "duration_ms", safe_duration)
        object.__setattr__(self, "unavailable_reason", safe_reason)
        # Make a construction-time projection.  A shallow copy is deliberate:
        # it keeps the public dataclass/asdict shape JSON-like while ensuring
        # the caller's input mapping cannot be used to inject fields later.
        object.__setattr__(self, "metrics", _FrozenDict(safe_metrics))
        object.__setattr__(self, "attributes", _FrozenDict(safe_attributes))

    def to_dict(self) -> dict[str, object]:
        return {
            "spanId": self.span_id,
            "name": self.name,
            "parentSpanId": self.parent_span_id,
            "status": self.status,
            "startedAtMs": self.started_at_ms,
            "endedAtMs": self.ended_at_ms,
            "durationMs": self.duration_ms,
            "recorded": self.recorded,
            "unavailableReason": self.unavailable_reason,
            "metrics": project_public_span_metrics(self.metrics, strict=True),
            "attributes": project_public_span_attributes(self.attributes, strict=True),
        }


def make_span(
    *,
    span_id: str,
    name: str,
    started_at_ms: int,
    ended_at_ms: int | None = None,
    parent_span_id: str | None = None,
    status: str = "completed",
    recorded: bool = True,
    unavailable_reason: str = "",
    metrics: Mapping[str, object] | None = None,
    attributes: Mapping[str, object] | None = None,
) -> TraceSpan:
    """Create a measured or explicitly unavailable span.

    A missing measurement is not converted to zero.  Conversely, a real
    zero-duration span remains zero, which keeps timing arithmetic truthful.
    """

    safe_id = _safe_token(span_id, "span_id")
    safe_name = _safe_token(name, "span name")
    safe_status = _safe_status(status, "span status", _SPAN_STATUSES)
    safe_parent = _safe_optional_token(parent_span_id, "parent_span_id")
    safe_metrics = project_public_span_metrics(metrics if metrics is not None else {}, strict=True)
    safe_attributes = project_public_span_attributes(
        attributes if attributes is not None else {},
        strict=True,
    )
    if not isinstance(started_at_ms, int) or isinstance(started_at_ms, bool) or started_at_ms < 0:
        raise TraceContractError("span start must be non-negative")
    if recorded:
        if unavailable_reason:
            safe_reason = _safe_unavailable_reason(unavailable_reason, required=False)
            if safe_reason:
                raise TraceContractError("recorded span cannot carry unavailableReason")
        if ended_at_ms is None:
            raise TraceContractError("recorded span requires ended_at_ms")
        if not isinstance(ended_at_ms, int) or isinstance(ended_at_ms, bool):
            raise TraceContractError("span end must be a non-negative integer")
        duration = ended_at_ms - started_at_ms
        if duration < 0:
            raise TraceContractError("span end must not precede start")
        return TraceSpan(
            safe_id,
            safe_name,
            safe_parent,
            safe_status,
            started_at_ms,
            ended_at_ms,
            duration,
            True,
            "",
            safe_metrics,
            safe_attributes,
        )
    if not isinstance(unavailable_reason, str) or not unavailable_reason:
        raise TraceContractError("unrecorded span requires unavailable_reason")
    safe_reason = _safe_unavailable_reason(unavailable_reason, required=True)
    return TraceSpan(
        safe_id,
        safe_name,
        safe_parent,
        safe_status,
        started_at_ms,
        None,
        None,
        False,
        safe_reason,
        safe_metrics,
        safe_attributes,
    )


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    evidence_id: str
    source_kind: str
    source_ref: str
    source_lane: str = ""
    evidence_stage: str = "observation_ref"
    disposition: str = "included"
    scores: Mapping[str, float] = field(default_factory=dict)
    rank_before: int | None = None
    rank_after: int | None = None
    omission_reason: str = ""

    def __post_init__(self) -> None:
        payload = _project_evidence_payload(
            {
                "evidenceId": self.evidence_id,
                "sourceKind": self.source_kind,
                "sourceRef": self.source_ref,
                "sourceLane": self.source_lane,
                "evidenceStage": self.evidence_stage,
                "disposition": self.disposition,
                "scores": self.scores,
                "rankBefore": self.rank_before,
                "rankAfter": self.rank_after,
                "omissionReason": self.omission_reason,
            }
        )
        object.__setattr__(self, "evidence_id", payload["evidenceId"])
        object.__setattr__(self, "source_kind", payload["sourceKind"])
        object.__setattr__(self, "source_ref", payload["sourceRef"])
        object.__setattr__(self, "source_lane", payload["sourceLane"])
        object.__setattr__(self, "evidence_stage", payload["evidenceStage"])
        object.__setattr__(self, "disposition", payload["disposition"])
        object.__setattr__(self, "scores", _FrozenDict(payload["scores"]))
        object.__setattr__(self, "rank_before", payload["rankBefore"])
        object.__setattr__(self, "rank_after", payload["rankAfter"])
        object.__setattr__(self, "omission_reason", payload["omissionReason"])

    def to_dict(self) -> dict[str, object]:
        return {
            "evidenceId": self.evidence_id,
            "sourceKind": self.source_kind,
            "sourceRef": self.source_ref,
            "sourceLane": self.source_lane,
            "evidenceStage": self.evidence_stage,
            "disposition": self.disposition,
            "scores": dict(self.scores),
            "rankBefore": self.rank_before,
            "rankAfter": self.rank_after,
            "omissionReason": self.omission_reason,
        }


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    artifact_id: str
    kind: str
    media_type: str
    sha256: str
    byte_size: int
    record_count: int

    def __post_init__(self) -> None:
        payload = _project_artifact_payload(
            {
                "artifactId": self.artifact_id,
                "kind": self.kind,
                "mediaType": self.media_type,
                "sha256": self.sha256,
                "byteSize": self.byte_size,
                "recordCount": self.record_count,
            }
        )
        object.__setattr__(self, "artifact_id", payload["artifactId"])
        object.__setattr__(self, "kind", payload["kind"])
        object.__setattr__(self, "media_type", payload["mediaType"])
        object.__setattr__(self, "sha256", payload["sha256"])
        object.__setattr__(self, "byte_size", payload["byteSize"])
        object.__setattr__(self, "record_count", payload["recordCount"])

    def to_dict(self) -> dict[str, object]:
        return {
            "artifactId": self.artifact_id,
            "kind": self.kind,
            "mediaType": self.media_type,
            "sha256": self.sha256,
            "byteSize": self.byte_size,
            "recordCount": self.record_count,
        }


@dataclass(frozen=True, slots=True)
class TraceLink:
    """A bounded relationship from one Trace envelope to another Trace."""

    target_trace_id: str
    relation: str
    target_kind: str = "trace"

    def __post_init__(self) -> None:
        payload = _project_trace_link_payload(
            {
                "traceId": self.target_trace_id,
                "relation": self.relation,
                "targetKind": self.target_kind,
            }
        )
        object.__setattr__(self, "target_trace_id", payload["traceId"])
        object.__setattr__(self, "relation", payload["relation"])
        object.__setattr__(self, "target_kind", payload["targetKind"])

    def to_dict(self) -> dict[str, str]:
        return {
            "traceId": self.target_trace_id,
            "relation": self.relation,
            "targetKind": self.target_kind,
        }


@dataclass(frozen=True, slots=True)
class TraceEnvelope:
    trace_id: str
    source_kind: str
    status: str
    binding: Mapping[str, str]
    input_fingerprint: str
    input_content_policy: str
    input_normalization: str
    spans: tuple[TraceSpan, ...]
    evidence: tuple[Mapping[str, object] | EvidenceRef, ...]
    artifacts: tuple[Mapping[str, object] | ArtifactRef, ...]
    created_at_ms: int
    updated_at_ms: int
    parent_trace_id: str | None = None
    links: tuple[Mapping[str, object] | TraceLink, ...] = ()

    def __post_init__(self) -> None:
        safe_trace_id = _safe_token(self.trace_id, "trace_id")
        safe_source_kind = _safe_token(self.source_kind, "source_kind")
        safe_status = _safe_status(self.status, "trace status", _TRACE_STATUSES)
        safe_binding = _validated_binding(self.binding)
        safe_parent_trace_id = _safe_optional_token(self.parent_trace_id, "parent_trace_id")
        if safe_parent_trace_id == safe_trace_id:
            raise TraceContractError("parent trace cannot target its own trace")
        safe_links = _project_trace_links(self.links, current_trace_id=safe_trace_id)
        if not isinstance(self.input_fingerprint, str) or not _SHA256_FINGERPRINT_PATTERN.fullmatch(
            self.input_fingerprint
        ):
            raise TraceContractError("input_fingerprint must be a SHA-256 fingerprint")
        if not isinstance(self.input_content_policy, str) or self.input_content_policy not in {
            "hash_only",
            "redacted",
            "owner_local",
        }:
            raise TraceContractError("input_content_policy is not supported")
        _safe_input_normalization(self.input_normalization)
        if not isinstance(self.spans, Sequence) or isinstance(self.spans, (str, bytes)):
            raise TraceContractError("trace spans must be a sequence")
        safe_spans = tuple(self.spans)
        if len(safe_spans) > 256 or any(not isinstance(span, TraceSpan) for span in safe_spans):
            raise TraceContractError("trace spans must contain TraceSpan records")
        if not isinstance(self.evidence, Sequence) or isinstance(self.evidence, (str, bytes)):
            raise TraceContractError("trace evidence must be a sequence")
        safe_evidence = tuple(
            _project_evidence_payload(item.to_dict() if isinstance(item, EvidenceRef) else item)
            if isinstance(item, (EvidenceRef, Mapping))
            else _raise_invalid_record("evidence")
            for item in self.evidence
        )
        if len(safe_evidence) > 2048:
            raise TraceContractError("trace evidence exceeds the public limit")
        if not isinstance(self.artifacts, Sequence) or isinstance(self.artifacts, (str, bytes)):
            raise TraceContractError("trace artifacts must be a sequence")
        safe_artifacts = tuple(
            _project_artifact_payload(item.to_dict() if isinstance(item, ArtifactRef) else item)
            if isinstance(item, (ArtifactRef, Mapping))
            else _raise_invalid_record("artifact")
            for item in self.artifacts
        )
        if len(safe_artifacts) > 256:
            raise TraceContractError("trace artifacts exceeds the public limit")
        safe_created = _safe_non_negative_int(self.created_at_ms, "trace createdAtMs")
        safe_updated = _safe_non_negative_int(self.updated_at_ms, "trace updatedAtMs")
        if safe_updated < safe_created:
            raise TraceContractError("trace updatedAtMs must not precede createdAtMs")
        object.__setattr__(self, "trace_id", safe_trace_id)
        object.__setattr__(self, "source_kind", safe_source_kind)
        object.__setattr__(self, "status", safe_status)
        object.__setattr__(self, "binding", _FrozenDict(safe_binding))
        object.__setattr__(self, "parent_trace_id", safe_parent_trace_id)
        object.__setattr__(
            self,
            "links",
            tuple(_FrozenDict(item) for item in safe_links),
        )
        object.__setattr__(self, "spans", safe_spans)
        object.__setattr__(
            self,
            "evidence",
            tuple(_freeze_evidence_payload(item) for item in safe_evidence),
        )
        object.__setattr__(self, "artifacts", tuple(_FrozenDict(item) for item in safe_artifacts))
        object.__setattr__(self, "created_at_ms", safe_created)
        object.__setattr__(self, "updated_at_ms", safe_updated)
        validate_trace_envelope(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        payload = {
            "schemaVersion": TRACE_SCHEMA_VERSION,
            "traceId": self.trace_id,
            "sourceKind": self.source_kind,
            "status": self.status,
            "binding": dict(self.binding),
            "input": {
                "fingerprint": self.input_fingerprint,
                "contentPolicy": self.input_content_policy,
                "normalization": self.input_normalization,
            },
            "spans": [span.to_dict() for span in self.spans],
            "evidence": [_as_evidence_dict(item) for item in self.evidence],
            "artifacts": [_as_dict(item) for item in self.artifacts],
            "createdAtMs": self.created_at_ms,
            "updatedAtMs": self.updated_at_ms,
        }
        if self.parent_trace_id is not None:
            payload["parentTraceId"] = self.parent_trace_id
        if self.links:
            payload["links"] = [dict(item) for item in self.links]
        validate_trace_envelope(payload)
        return payload


def build_trace_envelope(
    *,
    trace_id: str,
    source_kind: str,
    input_text: str,
    input_fingerprint: str | None = None,
    binding: Mapping[str, str] | None = None,
    spans: Sequence[TraceSpan] = (),
    evidence: Sequence[Mapping[str, object] | EvidenceRef] = (),
    artifacts: Sequence[Mapping[str, object] | ArtifactRef] = (),
    status: str = "completed",
    input_content_policy: str = "hash_only",
    input_normalization: str = "none",
    now_ms: int | None = None,
    created_at_ms: int | None = None,
    updated_at_ms: int | None = None,
    parent_trace_id: str | None = None,
    links: Sequence[Mapping[str, object] | TraceLink] = (),
) -> TraceEnvelope:
    timestamp = _now_ms() if now_ms is None else int(now_ms)
    created = timestamp if created_at_ms is None else int(created_at_ms)
    updated = timestamp if updated_at_ms is None else int(updated_at_ms)
    if created < 0 or updated < 0 or updated < created:
        raise TraceContractError("trace timestamp must be non-negative")
    return TraceEnvelope(
        trace_id=_safe_token(trace_id, "trace_id"),
        source_kind=_safe_token(source_kind, "source_kind"),
        status=_safe_status(status, "trace status", _TRACE_STATUSES),
        binding=_validated_binding({} if binding is None else binding),
        input_fingerprint=(
            fingerprint_text(input_text)
            if input_fingerprint is None
            else input_fingerprint
        ),
        input_content_policy=input_content_policy,
        input_normalization=_safe_input_normalization(input_normalization),
        spans=tuple(spans),
        evidence=tuple(evidence),
        artifacts=tuple(artifacts),
        created_at_ms=created,
        updated_at_ms=updated,
        parent_trace_id=parent_trace_id,
        links=tuple(links),
    )


def validate_trace_envelope(payload: Mapping[str, object]) -> None:
    try:
        validate_contract(payload, "trace-envelope.v1.json")
    except (ContractValidationError, ValueError) as exc:
        raise TraceContractError(str(exc)) from exc
    if not isinstance(payload, Mapping):
        raise TraceContractError("trace envelope must be a mapping")
    _safe_token(payload.get("traceId"), "traceId")
    _safe_token(payload.get("sourceKind"), "sourceKind")
    _safe_status(payload.get("status"), "trace status", _TRACE_STATUSES)
    _validated_binding(payload.get("binding"))
    if "parentTraceId" in payload:
        parent_trace_id = _safe_optional_token(payload.get("parentTraceId"), "parentTraceId")
        if parent_trace_id == payload.get("traceId"):
            raise TraceContractError("parent trace cannot target its own trace")
    if "links" in payload:
        raw_links = payload.get("links")
        safe_links = _project_trace_links(
            raw_links,
            current_trace_id=payload.get("traceId"),
        )
        if not isinstance(raw_links, Sequence) or len(safe_links) != len(raw_links):
            raise TraceContractError("trace links must be unique")
    input_payload = payload.get("input")
    if not isinstance(input_payload, Mapping):
        raise TraceContractError("trace input must be a mapping")
    fingerprint = input_payload.get("fingerprint")
    if not isinstance(fingerprint, str) or not _SHA256_FINGERPRINT_PATTERN.fullmatch(fingerprint):
        raise TraceContractError("trace input fingerprint must be a SHA-256 fingerprint")
    content_policy = input_payload.get("contentPolicy")
    if content_policy not in {"hash_only", "redacted", "owner_local"}:
        raise TraceContractError("trace input contentPolicy is not supported")
    _safe_input_normalization(input_payload.get("normalization"))
    created_at = payload.get("createdAtMs")
    updated_at = payload.get("updatedAtMs")
    _safe_non_negative_int(created_at, "trace createdAtMs")
    _safe_non_negative_int(updated_at, "trace updatedAtMs")
    if updated_at < created_at:
        raise TraceContractError("trace updatedAtMs must not precede createdAtMs")
    spans = payload.get("spans")
    if not isinstance(spans, Sequence) or isinstance(spans, (str, bytes)):
        raise TraceContractError("trace spans must be a sequence")
    span_ids: list[str] = []
    for item in spans:
        if not isinstance(item, Mapping):
            raise TraceContractError("trace spans must contain mappings")
        span_ids.append(_safe_token(item.get("spanId"), "spanId"))
    if len(span_ids) != len(set(span_ids)):
        raise TraceContractError("trace span IDs must be unique")
    span_id_set = set(span_ids)
    for item in spans:
        _safe_token(item.get("name"), "span name")
        _safe_optional_token(item.get("parentSpanId"), "parentSpanId")
        _safe_status(item.get("status"), "span status", _SPAN_STATUSES)
        started_at = _safe_non_negative_int(item.get("startedAtMs"), "startedAtMs")
        ended_at = (
            None
            if item.get("endedAtMs") is None
            else _safe_non_negative_int(item.get("endedAtMs"), "endedAtMs")
        )
        duration = (
            None
            if item.get("durationMs") is None
            else _safe_non_negative_int(item.get("durationMs"), "durationMs")
        )
        if not isinstance(item.get("recorded"), bool):
            raise TraceContractError("span recorded must be boolean")
        reason = _safe_unavailable_reason(
            item.get("unavailableReason"),
            required=item.get("recorded") is False,
        )
        project_public_span_metrics(item.get("metrics"), strict=True)
        project_public_span_attributes(item.get("attributes"), strict=True)
        parent = item.get("parentSpanId")
        if parent is not None and parent not in span_id_set:
            raise TraceContractError("span parent must reference a span in the same trace")
        recorded = item.get("recorded")
        if recorded:
            if ended_at is None or duration != ended_at - started_at:
                raise TraceContractError("recorded span duration must equal end minus start")
            if reason:
                raise TraceContractError("recorded span cannot carry unavailableReason")
        elif ended_at is not None or duration is not None:
            raise TraceContractError("unrecorded span cannot carry timing")
    evidence = payload.get("evidence")
    if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes)):
        raise TraceContractError("trace evidence must be a sequence")
    evidence_keys: list[tuple[str, str]] = []
    for item in evidence:
        if not isinstance(item, Mapping):
            raise TraceContractError("trace evidence must contain mappings")
        safe_item = _project_evidence_payload(item)
        evidence_keys.append((safe_item["evidenceId"], safe_item["evidenceStage"]))
    if len(evidence_keys) != len(set(evidence_keys)):
        raise TraceContractError(
            "trace evidence identity must be unique within an evidence stage"
        )
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, Sequence) or isinstance(artifacts, (str, bytes)):
        raise TraceContractError("trace artifacts must be a sequence")
    for item in artifacts:
        if not isinstance(item, Mapping):
            raise TraceContractError("trace artifacts must contain mappings")
        _project_artifact_payload(item)


def _as_dict(item: Mapping[str, object] | EvidenceRef | ArtifactRef) -> dict[str, object]:
    if isinstance(item, ArtifactRef):
        return item.to_dict()
    if not isinstance(item, Mapping):
        return _raise_invalid_record("artifact")
    return _project_artifact_payload(item)


def _as_evidence_dict(item: Mapping[str, object] | EvidenceRef) -> dict[str, object]:
    if isinstance(item, EvidenceRef):
        return item.to_dict()
    if not isinstance(item, Mapping):
        return _raise_invalid_record("evidence")
    return _project_evidence_payload(item)


@dataclass(frozen=True, slots=True)
class EvalRun:
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        payload = _copy_eval_payload(self.payload)
        validate_eval_run(payload)
        object.__setattr__(self, "payload", _freeze_eval_payload(payload))

    def to_dict(self) -> dict[str, object]:
        result = _copy_eval_payload(self.payload)
        validate_eval_run(result)
        return result


def _copy_eval_payload(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TraceContractError("EvalRun payload must be a mapping")
    result = dict(value)
    for key in (
        "truth",
        "evaluator",
        "requestedEvaluator",
        "metrics",
        "suiteBinding",
        "usage",
        "cost",
    ):
        nested = result.get(key)
        if isinstance(nested, Mapping):
            result[key] = dict(nested)
    trace_ids = result.get("traceIds")
    if isinstance(trace_ids, Sequence) and not isinstance(trace_ids, (str, bytes)):
        result["traceIds"] = list(trace_ids)
    return result


def _freeze_eval_payload(value: Mapping[str, object]) -> Mapping[str, object]:
    result = dict(value)
    for key in (
        "truth",
        "evaluator",
        "requestedEvaluator",
        "metrics",
        "suiteBinding",
        "usage",
        "cost",
    ):
        nested = result.get(key)
        if isinstance(nested, Mapping):
            result[key] = _FrozenDict(nested)
    trace_ids = result.get("traceIds")
    if isinstance(trace_ids, Sequence) and not isinstance(trace_ids, (str, bytes)):
        result["traceIds"] = tuple(trace_ids)
    return _FrozenDict(result)


def _validated_eval_metrics(
    value: object,
    allowed: frozenset[str],
    *,
    authority_name: str,
) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise TraceContractError("eval metrics must be a mapping")
    result: dict[str, float] = {}
    for key, raw_value in value.items():
        if not isinstance(key, str) or key not in allowed:
            raise TraceContractError(f"{authority_name} metrics contain an unsupported metric")
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise TraceContractError(f"eval metric {key} must be a finite number")
        number = float(raw_value)
        if not math.isfinite(number):
            raise TraceContractError(f"eval metric {key} must be a finite number")
        if not 0 <= number <= 1:
            raise TraceContractError(f"eval metric {key} must be between 0 and 1")
        result[key] = number
    return result


def _validated_eval_fingerprint(value: object, name: str) -> str:
    if not isinstance(value, str) or not _SHA256_FINGERPRINT_PATTERN.fullmatch(value):
        raise TraceContractError(f"{name} must be a SHA-256 fingerprint")
    return value


def _validated_eval_usage(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping) or not value:
        raise TraceContractError("eval usage must contain at least one token count")
    if set(value) - _EVAL_USAGE_KEYS:
        raise TraceContractError("eval usage contains unsupported fields")
    result: dict[str, int] = {}
    for key, raw_value in value.items():
        result[key] = _safe_non_negative_int(raw_value, f"eval usage {key}")
    return result


def _validated_eval_cost(value: object) -> dict[str, float | int]:
    if not isinstance(value, Mapping) or not value:
        raise TraceContractError("eval cost must contain at least one amount")
    if set(value) - _EVAL_COST_KEYS:
        raise TraceContractError("eval cost contains unsupported fields")
    result: dict[str, float | int] = {}
    for key, raw_value in value.items():
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise TraceContractError(f"eval cost {key} must be a finite non-negative number")
        amount = float(raw_value)
        if not math.isfinite(amount) or amount < 0:
            raise TraceContractError(f"eval cost {key} must be a finite non-negative number")
        result[key] = raw_value
    return result


def _validate_eval_provenance(payload: Mapping[str, object], *, mode: str) -> None:
    provenance_keys = {
        "requestedEvaluator",
        "promptVersion",
        "rubricVersion",
        "inputTraceFingerprint",
        "startedAtMs",
        "completedAtMs",
        "elapsedMs",
        "latencyMs",
        "usage",
        "cost",
        "fallbackUsed",
        "failureCode",
        "sourceTraceId",
        "repairTraceId",
        "sourceScope",
        "failureRef",
        "repairReceiptId",
        "changeReceiptId",
        "testEvidenceId",
        "testStatus",
    }
    present = provenance_keys.intersection(payload)
    if mode == "ground_truth":
        judge_only_keys = {
            "requestedEvaluator",
            "promptVersion",
            "rubricVersion",
            "failureCode",
            "sourceTraceId",
            "repairTraceId",
            "sourceScope",
            "failureRef",
            "repairReceiptId",
            "changeReceiptId",
            "testEvidenceId",
            "testStatus",
        }
        if present.intersection(judge_only_keys):
            raise TraceContractError(
                "ground truth eval cannot carry AI Judge repair provenance"
            )
    if "requestedEvaluator" in payload:
        _validated_evaluator(payload.get("requestedEvaluator"))
    for key in ("promptVersion", "rubricVersion"):
        if key in payload:
            _safe_token(payload.get(key), f"eval {key}")
    if (
        "promptVersion" in payload
        and "rubricVersion" in payload
        and payload.get("promptVersion") != payload.get("rubricVersion")
    ):
        raise TraceContractError("eval promptVersion and rubricVersion must agree")
    if "inputTraceFingerprint" in payload:
        _validated_eval_fingerprint(
            payload.get("inputTraceFingerprint"),
            "eval inputTraceFingerprint",
        )
    for key in ("startedAtMs", "completedAtMs", "elapsedMs", "latencyMs"):
        if key in payload:
            _safe_non_negative_int(payload.get(key), f"eval {key}")
    started = payload.get("startedAtMs")
    completed = payload.get("completedAtMs")
    if started is not None and completed is not None and completed < started:
        raise TraceContractError("eval completedAtMs must not precede startedAtMs")
    if "usage" in payload:
        _validated_eval_usage(payload.get("usage"))
    if "cost" in payload:
        _validated_eval_cost(payload.get("cost"))
    if "fallbackUsed" in payload and not isinstance(payload.get("fallbackUsed"), bool):
        raise TraceContractError("eval fallbackUsed must be a boolean")
    if "failureCode" in payload:
        failure_code = payload.get("failureCode")
        if failure_code not in _EVAL_FAILURE_CODES:
            raise TraceContractError("eval failureCode is not supported")
        if payload.get("status") != "failed":
            raise TraceContractError("eval failureCode requires a failed EvalRun")
    repair_keys = {
        "sourceTraceId", "repairTraceId", "sourceScope", "failureRef",
        "repairReceiptId", "changeReceiptId", "testEvidenceId", "testStatus",
    }
    repair_present = repair_keys.intersection(payload)
    if repair_present:
        if mode != "ai_judge":
            raise TraceContractError("repair provenance requires an AI Judge EvalRun")
        if repair_present != repair_keys:
            raise TraceContractError("repair provenance must contain the complete binding")
        for key in (
            "sourceTraceId", "repairTraceId", "sourceScope", "failureRef",
            "repairReceiptId", "changeReceiptId", "testEvidenceId",
        ):
            _safe_token(payload.get(key), f"eval {key}")
        if payload.get("testStatus") != "passed":
            raise TraceContractError("repair provenance requires a passed test")


def build_eval_run(
    *,
    eval_run_id: str,
    trace_ids: Sequence[str],
    mode: str,
    truth_kind: str,
    metrics: Mapping[str, float],
    dataset_id: str = "",
    label_revision: str = "",
    evaluator: Mapping[str, str] | None = None,
    suite_binding: Mapping[str, str] | None = None,
    status: str = "completed",
    now_ms: int | None = None,
    updated_at_ms: int | None = None,
    requested_evaluator: Mapping[str, str] | None = None,
    prompt_version: str | None = None,
    rubric_version: str | None = None,
    input_trace_fingerprint: str | None = None,
    started_at_ms: int | None = None,
    completed_at_ms: int | None = None,
    elapsed_ms: int | None = None,
    latency_ms: int | None = None,
    usage: Mapping[str, int] | None = None,
    cost: Mapping[str, float | int] | None = None,
    fallback_used: bool | None = None,
    failure_code: str | None = None,
    source_trace_id: str | None = None,
    repair_trace_id: str | None = None,
    repair_source_scope: str | None = None,
    repair_failure_ref: str | None = None,
    repair_receipt_id: str | None = None,
    change_receipt_id: str | None = None,
    test_evidence_id: str | None = None,
    test_status: str | None = None,
) -> EvalRun:
    safe_mode = _safe_token(mode, "eval mode")
    if safe_mode not in _EVAL_MODES:
        raise TraceContractError("eval mode must be ground_truth or ai_judge")
    if not isinstance(truth_kind, str) or truth_kind not in _EVAL_TRUTH_STATUSES:
        raise TraceContractError("truth kind is not supported")
    safe_truth = truth_kind
    if not isinstance(trace_ids, Sequence) or isinstance(trace_ids, (str, bytes)):
        raise TraceContractError("trace_ids must be a sequence")
    safe_trace_ids = [_safe_token(value, "trace_id") for value in trace_ids]
    if len(safe_trace_ids) != len(set(safe_trace_ids)):
        raise TraceContractError("trace_ids must be unique")
    if safe_mode == "ground_truth":
        if safe_truth not in {"human", "frozen"}:
            raise TraceContractError("ground truth metrics require human or frozen ground truth")
        if not isinstance(dataset_id, str) or not dataset_id or not isinstance(label_revision, str) or not label_revision:
            raise TraceContractError("ground truth metrics require dataset and label revision")
        safe_dataset_id = _safe_token(dataset_id, "dataset_id")
        safe_label_revision = _safe_token(label_revision, "label_revision")
        safe_metrics = _validated_eval_metrics(
            metrics,
            _DETERMINISTIC_METRICS,
            authority_name="ground truth",
        )
        default_evaluator = {
            "provider": "deterministic",
            "model": "labels",
            "thinking": "none",
            "displayName": (
                "Human labels" if safe_truth == "human" else "Frozen labels"
            ),
        }
        if evaluator is not None and dict(evaluator) != default_evaluator:
            raise TraceContractError(
                "ground truth evaluator provenance is fixed by its label authority"
            )
        safe_evaluator = default_evaluator
        authority = "ground_truth"
    else:
        if safe_truth != "none":
            raise TraceContractError("AI Judge estimates cannot claim ground truth")
        safe_dataset_id = (
            "" if dataset_id == "" else _safe_token(dataset_id, "dataset_id")
        )
        safe_label_revision = (
            "" if label_revision == "" else _safe_token(label_revision, "label_revision")
        )
        safe_metrics = _validated_eval_metrics(
            metrics,
            _JUDGE_METRICS,
            authority_name="AI Judge estimates cannot claim deterministic",
        )
        if evaluator is None:
            raise TraceContractError(
                "AI Judge eval requires explicit evaluator provenance"
            )
        safe_evaluator = _validated_evaluator(evaluator)
        authority = "ai_judge_estimate"
    safe_suite_binding = (
        None if suite_binding is None else _validated_suite_binding(suite_binding)
    )
    timestamp = _now_ms() if now_ms is None else int(now_ms)
    if timestamp < 0:
        raise TraceContractError("eval timestamp must be non-negative")
    updated_timestamp = timestamp if updated_at_ms is None else int(updated_at_ms)
    if updated_timestamp < timestamp:
        raise TraceContractError("eval updated timestamp must not precede created timestamp")
    payload = {
        "schemaVersion": EVAL_SCHEMA_VERSION,
        "evalRunId": _safe_token(eval_run_id, "eval_run_id"),
        "traceIds": safe_trace_ids,
        "mode": safe_mode,
        "metricAuthority": authority,
        "truth": {
            "status": safe_truth,
            "datasetId": safe_dataset_id,
            "labelRevision": safe_label_revision,
        },
        "evaluator": safe_evaluator,
        "metrics": safe_metrics,
        "status": _safe_status(status, "eval status", _EVAL_STATUSES),
        "createdAtMs": timestamp,
        "updatedAtMs": updated_timestamp,
    }
    if safe_suite_binding is not None:
        payload["suiteBinding"] = safe_suite_binding
    optional_provenance = (
        ("requestedEvaluator", None if requested_evaluator is None else _validated_evaluator(requested_evaluator)),
        ("promptVersion", None if prompt_version is None else _safe_token(prompt_version, "eval promptVersion")),
        ("rubricVersion", None if rubric_version is None else _safe_token(rubric_version, "eval rubricVersion")),
        ("inputTraceFingerprint", None if input_trace_fingerprint is None else _validated_eval_fingerprint(input_trace_fingerprint, "eval inputTraceFingerprint")),
        ("startedAtMs", started_at_ms),
        ("completedAtMs", completed_at_ms),
        ("elapsedMs", elapsed_ms),
        ("latencyMs", latency_ms),
        ("usage", None if usage is None else _validated_eval_usage(usage)),
        ("cost", None if cost is None else _validated_eval_cost(cost)),
        ("fallbackUsed", fallback_used),
        ("failureCode", failure_code),
        ("sourceTraceId", None if source_trace_id is None else _safe_token(source_trace_id, "eval sourceTraceId")),
        ("repairTraceId", None if repair_trace_id is None else _safe_token(repair_trace_id, "eval repairTraceId")),
        ("sourceScope", None if repair_source_scope is None else _safe_token(repair_source_scope, "eval sourceScope")),
        ("failureRef", None if repair_failure_ref is None else _safe_token(repair_failure_ref, "eval failureRef")),
        ("repairReceiptId", None if repair_receipt_id is None else _safe_token(repair_receipt_id, "eval repairReceiptId")),
        ("changeReceiptId", None if change_receipt_id is None else _safe_token(change_receipt_id, "eval changeReceiptId")),
        ("testEvidenceId", None if test_evidence_id is None else _safe_token(test_evidence_id, "eval testEvidenceId")),
        ("testStatus", test_status),
    )
    for key, value in optional_provenance:
        if value is not None:
            payload[key] = value
    validate_eval_run(payload)
    return EvalRun(payload)


def validate_eval_run(payload: Mapping[str, object]) -> None:
    try:
        validate_contract(payload, "eval-run.v1.json")
    except (ContractValidationError, ValueError) as exc:
        raise TraceContractError(str(exc)) from exc
    if not isinstance(payload, Mapping):
        raise TraceContractError("EvalRun payload must be a mapping")
    _safe_token(payload.get("evalRunId"), "evalRunId")
    trace_ids = payload.get("traceIds")
    if not isinstance(trace_ids, Sequence) or isinstance(trace_ids, (str, bytes)):
        raise TraceContractError("eval traceIds must be a sequence")
    normalized_trace_ids = [_safe_token(value, "traceId") for value in trace_ids]
    if len(normalized_trace_ids) != len(set(normalized_trace_ids)):
        raise TraceContractError("eval traceIds must be unique")
    mode = payload.get("mode")
    authority = payload.get("metricAuthority")
    truth = payload.get("truth")
    evaluator = payload.get("evaluator")
    metrics = payload.get("metrics")
    if "suiteBinding" in payload:
        _validated_suite_binding(payload.get("suiteBinding"))
    if mode not in _EVAL_MODES:
        raise TraceContractError("eval mode is not supported")
    if payload.get("status") not in _EVAL_STATUSES:
        raise TraceContractError("eval status is not supported")
    _safe_non_negative_int(payload.get("createdAtMs"), "eval createdAtMs")
    _safe_non_negative_int(payload.get("updatedAtMs"), "eval updatedAtMs")
    if payload["updatedAtMs"] < payload["createdAtMs"]:
        raise TraceContractError("eval updatedAtMs must not precede createdAtMs")
    if not isinstance(truth, Mapping):
        raise TraceContractError("eval truth must be a mapping")
    truth_status = truth.get("status")
    if truth_status not in _EVAL_TRUTH_STATUSES:
        raise TraceContractError("eval truth status is not supported")
    for field_name in ("datasetId", "labelRevision"):
        field_value = truth.get(field_name)
        if field_value:
            _safe_token(field_value, f"truth {field_name}")
    if not isinstance(metrics, Mapping):
        raise TraceContractError("eval metrics must be a mapping")
    allowed_metrics = _DETERMINISTIC_METRICS if mode == "ground_truth" else _JUDGE_METRICS
    if set(metrics) - allowed_metrics:
        raise TraceContractError("eval metrics do not match their declared authority")
    for name, value in metrics.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise TraceContractError(f"eval metric {name} must be a finite number")
        if not 0 <= float(value) <= 1:
            raise TraceContractError(f"eval metric {name} must be between 0 and 1")
    if mode == "ground_truth":
        if authority != "ground_truth" or not isinstance(truth, Mapping) or truth.get("status") not in {"human", "frozen"}:
            raise TraceContractError("ground truth eval must carry labelled ground truth authority")
        if not str(truth.get("datasetId") or "").strip() or not str(truth.get("labelRevision") or "").strip():
            raise TraceContractError("ground truth eval requires dataset and label revision")
        expected_evaluator = {
            "provider": "deterministic",
            "model": "labels",
            "thinking": "none",
            "displayName": "Human labels" if truth.get("status") == "human" else "Frozen labels",
        }
        if evaluator != expected_evaluator:
            raise TraceContractError(
                "ground truth evaluator provenance must match its label authority"
            )
    if mode == "ai_judge":
        if authority != "ai_judge_estimate" or not isinstance(truth, Mapping) or truth.get("status") != "none":
            raise TraceContractError("AI Judge eval must carry estimate authority and no ground truth")
        _validated_evaluator(evaluator)
    _validate_eval_provenance(payload, mode=mode)


def _validated_evaluator(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise TraceContractError("evaluator provenance must be a mapping")
    if set(value) != _EVALUATOR_KEYS:
        raise TraceContractError("evaluator provenance must name provider, model, thinking, and displayName")
    return {
        "provider": _safe_token(value.get("provider"), "evaluator provider"),
        "model": _safe_token(value.get("model"), "evaluator model"),
        "thinking": _safe_token(value.get("thinking"), "evaluator thinking"),
        "displayName": _safe_label(value.get("displayName"), "evaluator displayName"),
    }


def _validated_suite_binding(value: object) -> dict[str, str]:
    """Validate the optional immutable identity of a registered Eval suite."""

    if not isinstance(value, Mapping):
        raise TraceContractError("EvalRun suiteBinding must be a mapping")
    if set(value) != _EVAL_SUITE_BINDING_KEYS:
        raise TraceContractError(
            "EvalRun suiteBinding must name suiteId and suiteRevision only"
        )
    return {
        "suiteId": _safe_token(value.get("suiteId"), "suiteBinding suiteId"),
        "suiteRevision": _safe_token(
            value.get("suiteRevision"), "suiteBinding suiteRevision"
        ),
    }


@dataclass(frozen=True, slots=True)
class SandboxRun:
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        payload = _copy_sandbox_payload(self.payload)
        validate_sandbox_run(payload)
        object.__setattr__(self, "payload", _freeze_sandbox_payload(payload))

    def to_dict(self) -> dict[str, object]:
        result = _copy_sandbox_payload(self.payload)
        validate_sandbox_run(result)
        return result


def _copy_sandbox_payload(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TraceContractError("SandboxRun payload must be a mapping")
    result = dict(value)
    policy = result.get("policy")
    if isinstance(policy, Mapping):
        result["policy"] = dict(policy)
    for key in ("traceIds", "evalRunIds"):
        identifiers = result.get(key)
        if isinstance(identifiers, Sequence) and not isinstance(identifiers, (str, bytes)):
            result[key] = list(identifiers)
    return result


def _freeze_sandbox_payload(value: Mapping[str, object]) -> Mapping[str, object]:
    result = dict(value)
    policy = result.get("policy")
    if isinstance(policy, Mapping):
        result["policy"] = _FrozenDict(policy)
    replay_cohort = result.get("replayCohort")
    if isinstance(replay_cohort, Mapping):
        result["replayCohort"] = _FrozenDict(replay_cohort)
    for key in ("traceIds", "evalRunIds"):
        identifiers = result.get(key)
        if isinstance(identifiers, Sequence) and not isinstance(identifiers, (str, bytes)):
            result[key] = tuple(identifiers)
    return _FrozenDict(result)


def build_sandbox_run(
    *,
    sandbox_run_id: str,
    app_id: str,
    workspace_root: str,
    workspace_binding_id: str = "",
    mutation_mode: str = "read_only",
    network: str = "blocked",
    trace_ids: Sequence[str] = (),
    eval_run_ids: Sequence[str] = (),
    replay_cohort: Mapping[str, str] | None = None,
    status: str = "completed",
    now_ms: int | None = None,
) -> SandboxRun:
    timestamp = _now_ms() if now_ms is None else int(now_ms)
    workspace_fingerprint = fingerprint_text(_required(workspace_root, "workspace_root"))
    binding_id = workspace_binding_id or f"workspace-binding:{workspace_fingerprint.removeprefix('sha256:')}"
    payload = {
        "schemaVersion": SANDBOX_SCHEMA_VERSION,
        "sandboxRunId": _required(sandbox_run_id, "sandbox_run_id"),
        "appId": _required(app_id, "app_id"),
        "status": status,
        "policy": {
            "workspaceBindingId": _safe_token(binding_id, "workspace_binding_id"),
            "workspaceFingerprint": workspace_fingerprint,
            "mutationMode": mutation_mode,
            "network": network,
            "productionWriteBlocked": True,
        },
        "traceIds": [_required(value, "trace_id") for value in trace_ids],
        "evalRunIds": [_required(value, "eval_run_id") for value in eval_run_ids],
        "createdAtMs": timestamp,
        "updatedAtMs": timestamp,
    }
    if replay_cohort is not None:
        payload["replayCohort"] = _validated_sandbox_replay_cohort(replay_cohort)
    validate_sandbox_run(payload)
    return SandboxRun(payload)


def validate_sandbox_run(payload: Mapping[str, object]) -> None:
    try:
        validate_contract(payload, "sandbox-run.v1.json")
    except (ContractValidationError, ValueError) as exc:
        raise TraceContractError(str(exc)) from exc
    if not isinstance(payload, Mapping):
        raise TraceContractError("SandboxRun payload must be a mapping")
    _safe_token(payload.get("sandboxRunId"), "sandboxRunId")
    _safe_token(payload.get("appId"), "appId")
    _safe_status(payload.get("status"), "sandbox status", _SANDBOX_STATUSES)
    policy = payload.get("policy")
    if not isinstance(policy, Mapping) or set(policy) != _SANDBOX_POLICY_KEYS:
        raise TraceContractError("sandbox policy contains unsupported fields")
    _safe_token(policy.get("workspaceBindingId"), "workspaceBindingId")
    workspace_fingerprint = policy.get("workspaceFingerprint")
    if not isinstance(workspace_fingerprint, str) or not _SHA256_FINGERPRINT_PATTERN.fullmatch(
        workspace_fingerprint
    ):
        raise TraceContractError("workspaceFingerprint must be a SHA-256 fingerprint")
    _safe_status(
        policy.get("mutationMode"),
        "sandbox mutationMode",
        _SANDBOX_MUTATION_MODES,
    )
    _safe_status(policy.get("network"), "sandbox network", _SANDBOX_NETWORK_MODES)
    if policy.get("productionWriteBlocked") is not True:
        raise TraceContractError("sandbox production writes must remain blocked")
    replay_cohort = payload.get("replayCohort")
    if replay_cohort is not None:
        _validated_sandbox_replay_cohort(replay_cohort)
    for key, label in (("traceIds", "traceId"), ("evalRunIds", "evalRunId")):
        values = payload.get(key)
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            raise TraceContractError(f"sandbox {key} must be a sequence")
        safe_values = [_safe_token(value, label) for value in values]
        if len(safe_values) != len(set(safe_values)):
            raise TraceContractError(f"sandbox {key} must be unique")
    created_at = _safe_non_negative_int(payload.get("createdAtMs"), "sandbox createdAtMs")
    updated_at = _safe_non_negative_int(payload.get("updatedAtMs"), "sandbox updatedAtMs")
    if updated_at < created_at:
        raise TraceContractError("sandbox updatedAtMs must not precede createdAtMs")


def _validated_sandbox_replay_cohort(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != _SANDBOX_REPLAY_COHORT_KEYS:
        raise TraceContractError(
            "sandbox replayCohort must carry the complete frozen cohort"
        )
    result = {
        "suiteId": _safe_token(value.get("suiteId"), "replayCohort suiteId"),
        "suiteRevision": _safe_token(
            value.get("suiteRevision"), "replayCohort suiteRevision"
        ),
        "caseId": _safe_token(value.get("caseId"), "replayCohort caseId"),
    }
    for field_name in (
        "inputFingerprint",
        "environmentFingerprint",
        "configFingerprint",
        "modelProfileFingerprint",
        "toolProfileFingerprint",
        "skillProfileFingerprint",
    ):
        fingerprint = value.get(field_name)
        if (
            not isinstance(fingerprint, str)
            or _SHA256_FINGERPRINT_PATTERN.fullmatch(fingerprint) is None
        ):
            raise TraceContractError(
                f"replayCohort {field_name} must be a SHA-256 fingerprint"
            )
        result[field_name] = fingerprint
    return result
