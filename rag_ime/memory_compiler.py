from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from .local_sqlite_core import LocalSqliteCoreClient
from .memory_cleanup import cleanup_plan_to_payload
from .memory_generator import CoreOptimizationReport, MemoryGenerationError, DeepSeekMemoryGenerator
from .memory_ingest import normalize_text
from .memory_models import CleanupDiffEntry, CleanupRunPlan
from .text_utils import compact_whitespace, now_ms, token_terms, truncate_text

_SECRET_RE = re.compile(
    r"(password|token|api[_ -]?key|bearer|sk-[A-Za-z0-9]{8,}|验证码|身份证|手机号|地址|secret)",
    re.IGNORECASE,
)
_PATH_RE = re.compile(r"(?:(?:/Users|/Volumes)/[^\s\"']+)")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def build_memory_compile_bundle(
    core: LocalSqliteCoreClient,
    *,
    project: str,
    recent_limit: int,
    phrase_limit: int,
    memory_limit: int,
    governance_limit: int,
    allow_private_paths: bool = False,
) -> dict[str, object]:
    snapshot = core.core_optimization_snapshot(
        project=project,
        recent_limit=max(1, recent_limit),
        phrase_limit=max(1, phrase_limit),
    )
    memory_inspect = core.inspect_memory_v2(project=project, limit=max(1, memory_limit))
    governance = core.inspect_memory_governance(limit=max(1, governance_limit), include_inactive=False)
    redaction_stats = {"secret": 0, "path": 0, "email": 0}
    seen_event_keys: set[tuple[str, str]] = set()
    recent_events: list[dict[str, object]] = []
    for item in snapshot.get("recentEvents", []):
        if not isinstance(item, dict):
            continue
        sanitized_text, text_counts = _sanitize_text(str(item.get("text") or ""), allow_private_paths=allow_private_paths, max_chars=220)
        sanitized_context, context_counts = _sanitize_text(str(item.get("recentContext") or ""), allow_private_paths=allow_private_paths, max_chars=220)
        key = (sanitized_text, sanitized_context)
        if key in seen_event_keys:
            continue
        seen_event_keys.add(key)
        _merge_redaction_counts(redaction_stats, text_counts)
        _merge_redaction_counts(redaction_stats, context_counts)
        recent_events.append(
            {
                "eventId": int(item.get("eventId") or 0),
                "source": str(item.get("source") or ""),
                "app": str(item.get("app") or ""),
                "project": str(item.get("project") or ""),
                "text": sanitized_text,
                "recentContext": sanitized_context,
                "tags": list(item.get("tags") or []),
                "acceptedCount": int(item.get("acceptedCount") or 0),
                "skippedCount": int(item.get("skippedCount") or 0),
                "pinned": bool(item.get("pinned")),
                "inputFrequency": int(item.get("inputFrequency") or 0),
            }
        )
    phrases: list[dict[str, object]] = []
    for item in snapshot.get("highFrequencyPhrases", []):
        if not isinstance(item, dict):
            continue
        sanitized_text, counts = _sanitize_text(str(item.get("text") or ""), allow_private_paths=allow_private_paths, max_chars=120)
        _merge_redaction_counts(redaction_stats, counts)
        phrases.append(
            {
                "text": sanitized_text,
                "inputFrequency": int(item.get("inputFrequency") or 0),
                "acceptedCount": int(item.get("acceptedCount") or 0),
                "skippedCount": int(item.get("skippedCount") or 0),
                "pinned": bool(item.get("pinned")),
                "sources": list(item.get("sources") or []),
            }
        )
    memory_items: list[dict[str, object]] = []
    for item in memory_inspect.get("items", []):
        if not isinstance(item, dict):
            continue
        sanitized_text, counts = _sanitize_text(str(item.get("text") or ""), allow_private_paths=allow_private_paths, max_chars=180)
        _merge_redaction_counts(redaction_stats, counts)
        memory_items.append(
            {
                "memoryId": str(item.get("memoryId") or ""),
                "kind": str(item.get("kind") or ""),
                "status": str(item.get("status") or ""),
                "project": str(item.get("project") or ""),
                "app": str(item.get("app") or ""),
                "text": sanitized_text,
                "sourceEventId": int(item.get("sourceEventId") or 0),
                "qualityScore": float(item.get("qualityScore") or 0.0),
                "confidence": float(item.get("confidence") or 0.0),
            }
        )
    suppressions: list[dict[str, object]] = []
    for item in governance.get("suppressions", []):
        if not isinstance(item, dict):
            continue
        value, counts = _sanitize_text(str(item.get("matchValue") or ""), allow_private_paths=allow_private_paths, max_chars=180)
        _merge_redaction_counts(redaction_stats, counts)
        suppressions.append(
            {
                "matchType": str(item.get("matchType") or ""),
                "matchValue": value,
                "action": str(item.get("action") or ""),
                "reason": str(item.get("reason") or ""),
                "active": bool(item.get("active", True)),
            }
        )
    tombstones: list[dict[str, object]] = []
    for item in governance.get("tombstones", []):
        if not isinstance(item, dict):
            continue
        value, counts = _sanitize_text(str(item.get("targetValue") or ""), allow_private_paths=allow_private_paths, max_chars=180)
        _merge_redaction_counts(redaction_stats, counts)
        tombstones.append(
            {
                "targetType": str(item.get("targetType") or ""),
                "targetValue": value,
                "reason": str(item.get("reason") or ""),
                "active": bool(item.get("active", True)),
            }
        )
    return {
        "schemaVersion": "rag-ime.memory-compile-bundle.v1",
        "project": project,
        "exportedAtMs": now_ms(),
        "source": {
            "recentLimit": max(1, recent_limit),
            "phraseLimit": max(1, phrase_limit),
            "memoryLimit": max(1, memory_limit),
            "governanceLimit": max(1, governance_limit),
            "allowPrivatePaths": bool(allow_private_paths),
        },
        "redactionStats": redaction_stats,
        "totals": {
            "recentEvents": len(recent_events),
            "highFrequencyPhrases": len(phrases),
            "memoryItems": len(memory_items),
            "suppressions": len(suppressions),
            "tombstones": len(tombstones),
        },
        "recentEvents": recent_events,
        "highFrequencyPhrases": phrases,
        "memoryItems": memory_items,
        "governance": {
            "suppressions": suppressions,
            "tombstones": tombstones,
        },
    }


def compiler_generator_from_env(
    *,
    env_path: str | Path | None,
    provider: str = "",
    model: str = "",
) -> DeepSeekMemoryGenerator:
    generator = DeepSeekMemoryGenerator.from_env_path(env_path)
    requested_provider = _compiler_provider_alias(provider)
    configured_provider = _compiler_provider_alias(generator.provider_name)
    if requested_provider and configured_provider != requested_provider:
        raise MemoryGenerationError(
            f"configured provider is {generator.provider_name}, not {requested_provider}; check --provider or env file"
        )
    requested_model = compact_whitespace(model)
    if requested_model and requested_model != generator.config.model:
        generator = DeepSeekMemoryGenerator(replace(generator.config, model=requested_model))
    return generator


def cleanup_plan_from_compiler_report(
    *,
    project: str,
    bundle: dict[str, object],
    report: CoreOptimizationReport,
) -> CleanupRunPlan:
    diffs: list[CleanupDiffEntry] = []
    seen_targets: set[tuple[str, str]] = set()
    evidence_index = _build_cleanup_evidence_index(bundle)
    for item in report.memories:
        text = compact_whitespace(item.text)
        if not text:
            continue
        normalized = normalize_text(text)
        target = f"stable:{normalized}"
        key = ("add_stable_memory", target)
        if key in seen_targets:
            continue
        seen_targets.add(key)
        evidence_event_ids, source_stats = _resolve_compiler_evidence(
            provided=item.evidence_event_ids,
            text=text,
            tags=item.tags,
            reason=item.reason,
            evidence_index=evidence_index,
        )
        diffs.append(
            CleanupDiffEntry(
                op="add_stable_memory",
                target_memory_id=target,
                payload={
                    "memoryId": target,
                    "text": text,
                    "project": project,
                    "confidence": max(0.55, min(0.99, float(item.importance))),
                    "reason": compact_whitespace(item.reason),
                    "source": compact_whitespace(item.source),
                    "tags": list(_unique_strings(("compiled-memory", *item.tags))),
                    "evidenceEventIds": evidence_event_ids,
                    "sourceStats": source_stats,
                },
            )
        )
    for item in report.lexicon_phrases:
        text = compact_whitespace(item.text)
        if not text:
            continue
        normalized = normalize_text(text)
        target = f"phrase:{normalized}"
        key = ("add_phrase", target)
        if key in seen_targets:
            continue
        seen_targets.add(key)
        evidence_event_ids, source_stats = _resolve_compiler_evidence(
            provided=item.evidence_event_ids,
            text=text,
            tags=item.tags,
            reason=item.reason,
            evidence_index=evidence_index,
        )
        diffs.append(
            CleanupDiffEntry(
                op="add_phrase",
                target_memory_id=target,
                payload={
                    "memoryId": target,
                    "text": text,
                    "project": project,
                    "weight": max(0.05, min(1.0, float(item.weight))),
                    "reason": compact_whitespace(item.reason),
                    "tags": list(_unique_strings(("compiled-phrase", *item.tags))),
                    "evidenceEventIds": evidence_event_ids,
                    "sourceStats": source_stats,
                },
            )
        )
    for item in report.hide_events:
        if int(item.event_id or 0) <= 0:
            continue
        target = f"event:{int(item.event_id)}"
        key = ("tombstone", target)
        if key in seen_targets:
            continue
        seen_targets.add(key)
        diffs.append(
            CleanupDiffEntry(
                op="tombstone",
                target_memory_id=target,
                payload={
                    "targetType": "source_event_id",
                    "targetValue": str(int(item.event_id)),
                    "reason": compact_whitespace(item.reason) or "compiler:hide_event",
                },
            )
        )
    counts = {
        "stable": sum(1 for item in diffs if item.op == "add_stable_memory"),
        "phrase": sum(1 for item in diffs if item.op == "add_phrase"),
        "tombstone": sum(1 for item in diffs if item.op == "tombstone"),
    }
    return CleanupRunPlan(
        run_id=f"cleanup_{now_ms()}",
        provider=report.provider,
        model=report.model,
        summary=f"stable={counts['stable']} phrase={counts['phrase']} tombstone={counts['tombstone']}",
        metadata={
            "bundleSchemaVersion": str(bundle.get("schemaVersion") or ""),
            "bundleTotals": dict(bundle.get("totals") or {}),
            "redactionStats": dict(bundle.get("redactionStats") or {}),
            "generatorMetadata": dict(report.metadata or {}),
        },
        diffs=tuple(diffs),
    )


def _build_cleanup_evidence_index(bundle: dict[str, object]) -> list[dict[str, object]]:
    rows_by_event_id: dict[int, dict[str, object]] = {}
    for item in bundle.get("recentEvents", []):
        if not isinstance(item, dict):
            continue
        event_id = int(item.get("eventId") or 0)
        if event_id <= 0:
            continue
        rows_by_event_id[event_id] = {
            "eventId": event_id,
            "text": compact_whitespace(str(item.get("text") or "")),
            "recentContext": compact_whitespace(str(item.get("recentContext") or "")),
            "tags": tuple(_unique_strings(item.get("tags") or [])),
            "acceptedCount": int(item.get("acceptedCount") or 0),
            "inputFrequency": int(item.get("inputFrequency") or 0),
        }
    for item in bundle.get("memoryItems", []):
        if not isinstance(item, dict):
            continue
        event_id = int(item.get("sourceEventId") or 0)
        if event_id <= 0:
            continue
        existing = rows_by_event_id.get(event_id, {})
        rows_by_event_id[event_id] = {
            "eventId": event_id,
            "text": compact_whitespace(str(item.get("text") or existing.get("text") or "")),
            "recentContext": compact_whitespace(str(existing.get("recentContext") or "")),
            "tags": tuple(_unique_strings(existing.get("tags") or [])),
            "acceptedCount": int(existing.get("acceptedCount") or 0),
            "inputFrequency": int(existing.get("inputFrequency") or 0),
        }
    return list(rows_by_event_id.values())


def _resolve_compiler_evidence(
    *,
    provided: tuple[int, ...],
    text: str,
    tags: tuple[str, ...],
    reason: str,
    evidence_index: list[dict[str, object]],
) -> tuple[list[int], dict[str, object]]:
    provided_ids = [int(event_id) for event_id in provided if int(event_id) > 0]
    if provided_ids:
        selected_ids = list(dict.fromkeys(provided_ids))
        known_ids = {int(row.get("eventId") or 0) for row in evidence_index}
        known_selected_ids = [event_id for event_id in selected_ids if event_id in known_ids]
        return selected_ids, {
            "strategy": "model-provided",
            "totalEventCount": len(evidence_index),
            "matchedEventCount": len(known_selected_ids) or len(selected_ids),
            "selectedEventIds": selected_ids,
            "bestScore": 10.0 if selected_ids else 0.0,
            "minAcceptedScore": 3.0,
            "matchDetails": [
                {
                    "eventId": event_id,
                    "score": 10.0,
                    "matchKind": "model-provided",
                    "knownInBundle": event_id in known_ids,
                }
                for event_id in selected_ids[:3]
            ],
        }
    normalized = normalize_text(text)
    query_terms = set(token_terms(f"{text} {reason} {' '.join(tags)}", max_terms=24))
    scored: list[tuple[float, int, dict[str, object]]] = []
    for row in evidence_index:
        event_id = int(row.get("eventId") or 0)
        if event_id <= 0:
            continue
        event_text = compact_whitespace(str(row.get("text") or ""))
        event_context = compact_whitespace(str(row.get("recentContext") or ""))
        event_tags = {compact_whitespace(str(tag)).lower() for tag in row.get("tags") or [] if compact_whitespace(str(tag))}
        score = 0.0
        normalized_event = normalize_text(event_text)
        if normalized and normalized_event and normalized == normalized_event:
            score += 10.0
        elif normalized and normalized_event and (normalized in normalized_event or normalized_event in normalized):
            score += 6.0
        haystack = " ".join(part for part in (event_text, event_context, " ".join(sorted(event_tags))) if part)
        overlaps = query_terms.intersection(token_terms(haystack, max_terms=32))
        score += len(overlaps) * 1.5
        score += len({tag.lower() for tag in tags}.intersection(event_tags)) * 0.75
        score += min(1.0, int(row.get("acceptedCount") or 0) * 0.2)
        score += min(1.0, int(row.get("inputFrequency") or 0) * 0.1)
        if score >= 3.0:
            match_kind = "token-overlap"
            if normalized and normalize_text(event_text) == normalized:
                match_kind = "exact-text"
            elif normalized and normalize_text(event_text) and (
                normalized in normalize_text(event_text) or normalize_text(event_text) in normalized
            ):
                match_kind = "text-substring"
            scored.append(
                (
                    score,
                    event_id,
                    {
                        "eventId": event_id,
                        "score": round(score, 3),
                        "matchKind": match_kind,
                        "overlapTerms": sorted(overlaps)[:8],
                        "acceptedCount": int(row.get("acceptedCount") or 0),
                        "inputFrequency": int(row.get("inputFrequency") or 0),
                    },
                )
            )
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    selected = scored[:3]
    selected_ids = [event_id for _score, event_id, _details in selected]
    return selected_ids, {
        "strategy": "auto-backfill",
        "totalEventCount": len(evidence_index),
        "matchedEventCount": len(scored),
        "selectedEventIds": selected_ids,
        "bestScore": round(scored[0][0], 3) if scored else 0.0,
        "minAcceptedScore": 3.0,
        "matchDetails": [details for _score, _event_id, details in selected],
    }


def memory_compile_report_payload(
    *,
    project: str,
    bundle: dict[str, object],
    report: CoreOptimizationReport,
    plan: CleanupRunPlan,
    output_path: str = "",
) -> dict[str, object]:
    return {
        "schemaVersion": "rag-ime.memory-compile.v1",
        "ok": True,
        "dryRun": True,
        "project": project,
        "provider": report.provider,
        "model": report.model,
        "elapsedMs": report.elapsed_ms,
        "bundle": {
            "schemaVersion": str(bundle.get("schemaVersion") or ""),
            "totals": dict(bundle.get("totals") or {}),
            "redactionStats": dict(bundle.get("redactionStats") or {}),
        },
        "diffCount": len(plan.diffs),
        "run": cleanup_plan_to_payload(plan),
        "outputPath": output_path,
        "rawPreview": truncate_text(compact_whitespace(report.raw_text), 400),
        "metadata": dict(report.metadata or {}),
    }


def _sanitize_text(text: str, *, allow_private_paths: bool, max_chars: int) -> tuple[str, dict[str, int]]:
    value = compact_whitespace(text)
    counts = {"secret": 0, "path": 0, "email": 0}
    if not value:
        return "", counts
    value, secret_count = _SECRET_RE.subn("[REDACTED_SECRET]", value)
    value, email_count = _EMAIL_RE.subn("[REDACTED_EMAIL]", value)
    counts["secret"] += secret_count
    counts["email"] += email_count
    if not allow_private_paths:
        value, path_count = _PATH_RE.subn("[REDACTED_PATH]", value)
        counts["path"] += path_count
    return truncate_text(value, max_chars), counts


def _merge_redaction_counts(total: dict[str, int], delta: dict[str, int]) -> None:
    for key in ("secret", "path", "email"):
        total[key] = int(total.get(key, 0)) + int(delta.get(key, 0))


def _unique_strings(items: tuple[str, ...] | list[str] | tuple[object, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        text = compact_whitespace(str(item))
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return tuple(result)


def _compiler_provider_alias(provider: str) -> str:
    normalized = compact_whitespace(provider).lower()
    if normalized in {"deepseek", "deepseek-v4", "dsv4"}:
        return "deepseek-v4"
    return normalized
