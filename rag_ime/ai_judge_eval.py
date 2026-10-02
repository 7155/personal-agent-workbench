"""Small Trace Eval AI-Judge seam.

The evaluator is deliberately kept separate from ``trace_runtime``'s contract
builder.  This module owns the runtime request defaults and parsing boundary;
``build_eval_run`` remains the authority that labels the result as an estimate
and prevents deterministic metrics from being claimed.
"""

from __future__ import annotations

import json
import math
from types import MappingProxyType
from typing import Mapping

from .agent_model_defaults import DEFAULT_AGENT_MODEL_ID
from .text_utils import compact_whitespace


DEFAULT_AI_JUDGE_EVALUATOR = MappingProxyType(
    {
        "provider": "openai-codex",
        "model": DEFAULT_AGENT_MODEL_ID,
        "thinking": "max",
        "displayName": "Luna Max",
    }
)
AI_JUDGE_RUBRIC_VERSION = "trace-eval-ai-judge-v1"
AI_JUDGE_METRIC_KEYS = (
    "relevance",
    "coverage",
    "groundedness",
    "contradiction",
    "confidence",
)


class AiJudgeOutputError(ValueError):
    """The model did not return the bounded score object required by Eval."""


def effective_ai_judge_evaluator(
    requested: Mapping[str, object] | None = None,
) -> dict[str, str]:
    """Resolve an explicit evaluator or the product's Luna Max default."""

    if requested in (None, {}):
        return dict(DEFAULT_AI_JUDGE_EVALUATOR)
    if not isinstance(requested, Mapping):
        raise ValueError("AI Judge evaluator must be an object")
    result = {
        key: str(requested.get(key) or "").strip()
        for key in ("provider", "model", "thinking", "displayName")
    }
    if not all(result.values()):
        raise ValueError("AI Judge evaluator must name provider, model, thinking, and displayName")
    return result


def parse_ai_judge_metrics(value: object) -> dict[str, float]:
    """Parse only the five public estimate scores from a model response."""

    if isinstance(value, Mapping):
        parsed: object = value
    else:
        text = str(value or "").strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1]) if len(lines) >= 3 else ""
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AiJudgeOutputError("AI Judge response is not valid JSON") from exc
    if not isinstance(parsed, Mapping):
        raise AiJudgeOutputError("AI Judge response must be an object")
    if set(parsed) != set(AI_JUDGE_METRIC_KEYS):
        raise AiJudgeOutputError("AI Judge response must contain exactly the public estimate scores")
    result: dict[str, float] = {}
    for key in AI_JUDGE_METRIC_KEYS:
        raw = parsed.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise AiJudgeOutputError(f"AI Judge metric {key} must be numeric")
        number = float(raw)
        if not math.isfinite(number) or not 0 <= number <= 1:
            raise AiJudgeOutputError(f"AI Judge metric {key} must be between 0 and 1")
        result[key] = number
    return result


def build_ai_judge_prompt(trace: Mapping[str, object]) -> str:
    """Build a redacted, reproducible rubric input from public Trace facts."""

    spans = trace.get("spans") if isinstance(trace.get("spans"), list) else []
    safe_spans = [
        {
            "name": compact_whitespace(str(item.get("name") or "")),
            "status": compact_whitespace(str(item.get("status") or "")),
            "recorded": item.get("recorded") is True,
            "durationMs": item.get("durationMs"),
            "unavailableReason": compact_whitespace(str(item.get("unavailableReason") or "")),
        }
        for item in spans
        if isinstance(item, Mapping)
    ][:64]
    evidence = trace.get("evidence") if isinstance(trace.get("evidence"), list) else []
    safe_evidence = [
        {
            "evidenceId": compact_whitespace(str(item.get("evidenceId") or "")),
            "sourceKind": compact_whitespace(str(item.get("sourceKind") or "")),
            "evidenceStage": compact_whitespace(str(item.get("evidenceStage") or "")),
            "disposition": compact_whitespace(str(item.get("disposition") or "")),
        }
        for item in evidence
        if isinstance(item, Mapping)
    ][:64]
    public_trace = {
        "traceId": compact_whitespace(str(trace.get("traceId") or "")),
        "sourceKind": compact_whitespace(str(trace.get("sourceKind") or "")),
        "status": compact_whitespace(str(trace.get("status") or "")),
        "spanCount": len(spans),
        "evidenceCount": len(evidence),
        "spans": safe_spans,
        "evidence": safe_evidence,
    }
    return (
        f"Trace Eval rubric {AI_JUDGE_RUBRIC_VERSION}.\n"
        "Score this redacted Trace only. Return JSON with exactly these numbers "
        "between 0 and 1: relevance, coverage, groundedness, contradiction, confidence. "
        "This is an AI estimate, not ground truth; never return precision, recall, accuracy, or F1.\n"
        + json.dumps(public_trace, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


__all__ = [
    "AI_JUDGE_METRIC_KEYS",
    "AI_JUDGE_RUBRIC_VERSION",
    "AiJudgeOutputError",
    "DEFAULT_AI_JUDGE_EVALUATOR",
    "build_ai_judge_prompt",
    "effective_ai_judge_evaluator",
    "parse_ai_judge_metrics",
]
