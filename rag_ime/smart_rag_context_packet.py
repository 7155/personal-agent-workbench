from __future__ import annotations

import uuid
from typing import Iterable, Mapping, Sequence

from .active_rag_models import ActiveRagEvidence
from .daily_planner import estimate_tokens
from .input_event_assembly import tail_for_token_budget
from .text_utils import compact_whitespace, now_ms, stable_text_hash, truncate_text
from .window_context import project_window_context_for_generation


SMART_RAG_CONTEXT_PACKET_SCHEMA_VERSION = "rag-ime.smart-context-packet.v1"


def build_post_commit_context_packet(
    *,
    current_input: str,
    context_group_id: str,
    app: str,
    project: str,
    group_events: Sequence[Mapping[str, object]] = (),
    memory_books: Sequence[Mapping[str, object]] = (),
    tag_hints: Sequence[str] = (),
    surface_hints: Sequence[str] = (),
    negative_signals: Sequence[str] = (),
) -> dict[str, object]:
    """Build the only automatic online task: short post-commit suffix completion."""

    group_id = compact_whitespace(context_group_id)
    safe_group_events: list[dict[str, object]] = []
    for event in reversed(tuple(group_events)):
        event_group = compact_whitespace(str(event.get("contextGroupId") or group_id))
        if group_id and event_group and event_group != group_id:
            continue
        if bool(event.get("deleted")):
            continue
        text = truncate_text(compact_whitespace(str(event.get("text") or "")), 80)
        if not text:
            continue
        safe_group_events.append(
            {
                "text": text,
                "ageMs": max(0, int(event.get("ageMs") or 0)),
                "accepted": bool(event.get("accepted")),
            }
        )
        if len(safe_group_events) >= 2:
            break
    safe_books: list[dict[str, object]] = []
    for book in memory_books:
        safe_books.append(
            {
                "bookId": compact_whitespace(str(book.get("bookId") or book.get("id") or "")),
                "title": truncate_text(compact_whitespace(str(book.get("title") or "")), 60),
                "summary": truncate_text(compact_whitespace(str(book.get("summary") or "")), 140),
                "surfaceHints": _bounded_strings(book.get("surfaceHints"), limit=4, max_chars=18),
                "tags": _bounded_strings(book.get("tags"), limit=8, max_chars=24),
                "sourceEventIds": [
                    int(value)
                    for value in book.get("sourceEventIds", [])
                    if isinstance(value, int) and value > 0
                ][:8],
                "directCandidateAllowed": False,
            }
        )
        if len(safe_books) >= 2:
            break
    return {
        "schemaVersion": "rag-ime.post-commit-context-packet.v1",
        "currentInput": {
            "contextGroupId": group_id,
            "committedTail": truncate_text(compact_whitespace(current_input), 500),
            "app": compact_whitespace(app),
            "project": compact_whitespace(project),
        },
        "groupBuffer": list(reversed(safe_group_events)),
        "memoryBook": safe_books,
        "tagHints": _bounded_strings(tag_hints, limit=8, max_chars=24),
        "surfaceHints": _bounded_strings(surface_hints, limit=4, max_chars=18),
        "negativeSignals": _bounded_strings(negative_signals, limit=8, max_chars=24),
        "outputContract": {
            "task": "post_commit_suffix",
            "suffixOnly": True,
            "maxChars": 18,
            "candidateCount": 3,
            "noExplanation": True,
            "noContextEcho": True,
        },
    }


def _bounded_strings(values: object, *, limit: int, max_chars: int) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    result: list[str] = []
    for value in values:
        text = truncate_text(compact_whitespace(str(value)), max_chars)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _take_budgeted_window_context(
    context: Mapping[str, object],
    *,
    remaining_tokens: int,
    maximum_tokens: int,
    max_nodes: int = 160,
    max_chars: int = 12000,
) -> tuple[dict[str, object], int]:
    projected = project_window_context_for_generation(context)
    if not projected or remaining_tokens <= 0:
        return {}, max(0, remaining_tokens)
    budget = max(0, min(int(remaining_tokens), int(maximum_tokens)))
    requested_nodes = max(1, min(160, int(max_nodes)))
    requested_chars = max(1, min(12000, int(max_chars)))
    compact: dict[str, object] = {
        "schemaVersion": str(projected.get("schemaVersion") or ""),
        "captureMode": str(projected.get("captureMode") or "accessibility_semantics"),
        "projection": str(projected.get("projection") or ""),
        "capturedAtMs": max(0, int(projected.get("capturedAtMs") or 0)),
        "sourceNodeCount": max(0, int(projected.get("sourceNodeCount") or 0)),
        "nodes": [],
        "truncated": bool(projected.get("truncated")),
        "trust": {
            "maySupportIntent": True,
            "maySupportFacts": False,
            "mustNotOverrideCurrentInput": True,
        },
    }
    application = projected.get("application")
    if isinstance(application, Mapping):
        compact["application"] = {
            key: text
            for key in ("name", "windowTitle")
            if (text := compact_whitespace(str(application.get(key) or "")))
        }
    application_semantics = _take_budgeted_application_semantics(
        projected.get("applicationSemantics"),
        budget=max(0, budget - _window_context_estimated_tokens(compact)),
    )
    if application_semantics:
        compact["applicationSemantics"] = application_semantics
    header_tokens = _window_context_estimated_tokens(compact)
    header_tokens = min(budget, header_tokens)
    consumed = header_tokens
    raw_nodes = projected.get("nodes") if isinstance(projected.get("nodes"), list) else []
    actual_node_count = len(raw_nodes)
    actual_char_count = sum(
        len(str(node.get("label") or "")) + len(str(node.get("value") or ""))
        for node in raw_nodes
        if isinstance(node, Mapping)
    )
    selected_nodes: list[dict[str, object]] = []
    selected_char_count = 0
    for raw in raw_nodes[:requested_nodes]:
        if not isinstance(raw, Mapping):
            continue
        node: dict[str, object] = {
            key: raw[key]
            for key in (
                "role",
                "subrole",
                "label",
                "value",
                "focused",
                "selected",
            )
            if key in raw and raw[key] not in ("", None, [], False)
        }
        node_tokens = max(
            1,
            estimate_tokens(
                " ".join(
                    str(node.get(key) or "")
                    for key in ("role", "subrole", "label", "value")
                )
            ),
        )
        node_chars = len(str(node.get("label") or "")) + len(str(node.get("value") or ""))
        remaining_chars = requested_chars - selected_char_count
        if remaining_chars <= 0:
            compact["truncated"] = True
            break
        if node_chars > remaining_chars:
            compact["truncated"] = True
            label = str(node.get("label") or "")
            value = str(node.get("value") or "")
            if len(label) >= remaining_chars:
                node["label"] = label[:remaining_chars]
                node.pop("value", None)
            else:
                node["label"] = label
                node["value"] = value[: max(0, remaining_chars - len(label))]
            node_chars = len(str(node.get("label") or "")) + len(str(node.get("value") or ""))
            if node_chars <= 0:
                continue
        if consumed + node_tokens > budget:
            compact["truncated"] = True
            value = str(node.get("value") or "")
            base_node = {key: item for key, item in node.items() if key != "value"}
            base_tokens = max(
                1,
                estimate_tokens(
                    " ".join(
                        str(base_node.get(key) or "")
                        for key in ("role", "subrole", "label")
                    )
                ),
            )
            value_budget = budget - consumed - base_tokens
            if value_budget <= 0:
                continue
            bounded_value = tail_for_token_budget(value, value_budget)
            if not bounded_value:
                continue
            node = {**base_node, "value": bounded_value}
            node_tokens = base_tokens + estimate_tokens(bounded_value)
            node_chars = len(str(node.get("label") or "")) + len(str(node.get("value") or ""))
            if consumed + node_tokens > budget:
                continue
        selected_nodes.append(node)
        consumed += node_tokens
        selected_char_count += node_chars
        if len(selected_nodes) >= requested_nodes:
            break
    compact["nodes"] = selected_nodes
    compact["nodeCount"] = len(selected_nodes)
    compact["requestedNodeCount"] = requested_nodes
    compact["effectiveNodeCount"] = len(selected_nodes)
    compact["actualNodeCount"] = actual_node_count
    compact["requestedCharCount"] = requested_chars
    compact["effectiveCharCount"] = selected_char_count
    compact["actualCharCount"] = actual_char_count
    compact["unavailableReason"] = "no_accessibility_nodes" if actual_node_count == 0 else ""
    if actual_node_count > len(selected_nodes) or actual_char_count > selected_char_count:
        compact["truncated"] = True
    return compact, max(0, remaining_tokens - consumed)


def _window_context_estimated_tokens(context: Mapping[str, object]) -> int:
    if not context:
        return 0
    projected = (
        context
        if context.get("projection") == "generation_text"
        else project_window_context_for_generation(context)
    )
    if not projected:
        return 0
    text_parts: list[str] = []
    application = projected.get("application")
    if isinstance(application, Mapping):
        text_parts.extend(
            str(application.get(key) or "")
            for key in ("name", "windowTitle")
        )
    application_semantics = projected.get("applicationSemantics")
    if isinstance(application_semantics, Mapping):
        text_parts.extend(
            str(application_semantics.get(key) or "")
            for key in (
                "projectName",
                "activeFile",
                "editorExcerpt",
                "contentOrigin",
            )
        )
        entries = application_semantics.get("projectEntries")
        if isinstance(entries, list):
            text_parts.extend(str(entry) for entry in entries[:48])
    nodes = projected.get("nodes") if isinstance(projected.get("nodes"), list) else []
    for node in nodes[:48]:
        if not isinstance(node, Mapping):
            continue
        text_parts.extend(
            str(node.get(key) or "")
            for key in ("role", "subrole", "label", "value")
        )
    return estimate_tokens(" ".join(text_parts))


def _take_budgeted_application_semantics(
    value: object,
    *,
    budget: int,
) -> dict[str, object]:
    if not isinstance(value, Mapping) or budget <= 0:
        return {}
    compact: dict[str, object] = {
        "source": str(value.get("source") or ""),
        "freshness": str(value.get("freshness") or ""),
        "projectName": compact_whitespace(str(value.get("projectName") or "")),
        "activeFile": compact_whitespace(str(value.get("activeFile") or "")),
        "editorExcerptStartLine": max(
            1,
            int(value.get("editorExcerptStartLine") or 1),
        ),
        "projectEntries": [],
        "projectEntriesScope": str(value.get("projectEntriesScope") or ""),
        "contentOrigin": str(value.get("contentOrigin") or ""),
        "trust": {
            "maySupportIntent": True,
            "maySupportFacts": False,
            "mayLagUnsavedChanges": bool(
                isinstance(value.get("trust"), Mapping)
                and value["trust"].get("mayLagUnsavedChanges") is True
            ),
            "mustNotOverrideCurrentInput": True,
        },
    }
    consumed = estimate_tokens(
        " ".join(
            str(compact.get(key) or "")
            for key in (
                "source",
                "freshness",
                "projectName",
                "activeFile",
                "contentOrigin",
            )
        )
    )
    raw_entries = value.get("projectEntries")
    entry_reserve = 0
    if isinstance(raw_entries, list):
        entry_reserve = min(
            160,
            max(0, budget // 5),
            sum(
                estimate_tokens(compact_whitespace(str(entry or "")))
                for entry in raw_entries[:48]
            ),
        )
    excerpt_budget = max(0, budget - consumed - entry_reserve)
    excerpt = _head_for_token_budget(
        str(value.get("editorExcerpt") or ""),
        excerpt_budget,
    )
    if excerpt:
        compact["editorExcerpt"] = excerpt
        consumed += estimate_tokens(excerpt)
    if isinstance(raw_entries, list):
        selected_entries: list[str] = []
        for raw_entry in raw_entries[:48]:
            entry = compact_whitespace(str(raw_entry or ""))
            entry_tokens = estimate_tokens(entry)
            if not entry or consumed + entry_tokens > budget:
                continue
            selected_entries.append(entry)
            consumed += entry_tokens
        compact["projectEntries"] = selected_entries
    return compact


def _head_for_token_budget(text: str, budget: int) -> str:
    if not text or budget <= 0:
        return ""
    if estimate_tokens(text) <= budget:
        return text
    lower = 0
    upper = len(text)
    while lower < upper:
        midpoint = (lower + upper + 1) // 2
        if estimate_tokens(text[:midpoint]) <= max(0, budget - 1):
            lower = midpoint
        else:
            upper = midpoint - 1
    return text[:lower].rstrip() + ("…" if lower > 0 else "")


def build_active_rag_context_packet(
    *,
    scene: str,
    current_context: str,
    selected_text: str,
    selected_text_hash: str,
    frontend_revision: int,
    selection_epoch: int,
    panel_session_id: str,
    project: str,
    app: str,
    evidence: Iterable[ActiveRagEvidence],
    recent_input_history: Iterable[ActiveRagEvidence] = (),
    intent: str = "complete",
    placement: str = "insert_after_selection",
    max_candidates: int = 1,
    max_chars: int = 120,
    latency_budget_ms: int = 2500,
    remote_model_allowed: bool = True,
    context_token_budget: int = 4096,
    reserved_output_tokens: int = 1024,
    recent_input_baseline: int = 4,
    recent_input_maximum: int = 4,
    recent_input_char_maximum: int = 12000,
    ax_node_maximum: int = 160,
    ax_char_maximum: int = 12000,
    window_context: Mapping[str, object] | None = None,
) -> dict[str, object]:
    raw_context = compact_whitespace(current_context)
    selected = compact_whitespace(selected_text)
    token_budget = max(2048, int(context_token_budget))
    reserved_tokens = max(256, min(token_budget - 256, int(reserved_output_tokens)))
    available_tokens = max(256, token_budget - reserved_tokens)
    raw_window_context = dict(window_context) if isinstance(window_context, Mapping) else {}
    desired_window_tokens = min(900, _window_context_estimated_tokens(raw_window_context))
    current_input_budget = max(256, available_tokens - desired_window_tokens)
    context = tail_for_token_budget(raw_context or selected, current_input_budget)
    evidence_items = tuple(evidence)
    history_items = tuple(recent_input_history)
    raw_window_projection = project_window_context_for_generation(raw_window_context)
    raw_window_nodes = (
        raw_window_projection.get("nodes")
        if isinstance(raw_window_projection.get("nodes"), list)
        else []
    )
    raw_ax_actual_node_count = len(raw_window_nodes)
    raw_ax_actual_char_count = sum(
        len(str(node.get("label") or "")) + len(str(node.get("value") or ""))
        for node in raw_window_nodes
        if isinstance(node, Mapping)
    )
    raw_ax_captured_at_ms = max(0, int(raw_window_projection.get("capturedAtMs") or 0))
    raw_planning_items = [_planning_item(item) for item in evidence_items if _is_planning_evidence(item)]
    raw_activity_items = [
        _activity_timeline_item(item)
        for item in evidence_items
        if _is_activity_timeline_evidence(item)
    ]
    raw_notebook_items = [
        _notebook_item(item)
        for item in evidence_items
        if _is_notebook_evidence(item)
        and not _is_planning_evidence(item)
        and not _is_activity_timeline_evidence(item)
    ]
    raw_timeline_items = [
        _timeline_item(item)
        for item in evidence_items
        if _is_timeline_evidence(item) and not _is_activity_timeline_evidence(item)
    ]
    raw_recent_items = _recent_input_items(
        (*evidence_items, *history_items),
        current_context=context,
        selected_text=selected,
    )
    remaining_tokens = max(0, available_tokens - estimate_tokens(context))
    planning_items, remaining_tokens = _take_budgeted_items(
        raw_planning_items,
        remaining_tokens=remaining_tokens,
        text_keys=("title", "detail"),
        limit=6,
    )
    compact_window_context, remaining_tokens = _take_budgeted_window_context(
        raw_window_context,
        remaining_tokens=remaining_tokens,
        maximum_tokens=900,
        max_nodes=ax_node_maximum,
        max_chars=ax_char_maximum,
    )
    window_context_tokens = _window_context_estimated_tokens(compact_window_context)
    activity_items, remaining_tokens = _take_budgeted_items(
        raw_activity_items,
        remaining_tokens=remaining_tokens,
        text_keys=("title", "summary"),
        limit=2,
    )
    recent_items, remaining_tokens = _take_budgeted_recent_items(
        raw_recent_items,
        remaining_tokens=remaining_tokens,
        limit=max(1, min(200, int(recent_input_maximum))),
        max_chars=recent_input_char_maximum,
    )
    requested_recent_count = max(1, min(200, int(recent_input_maximum)))
    requested_recent_chars = max(1, min(100000, int(recent_input_char_maximum)))
    actual_recent_count = len(raw_recent_items)
    actual_recent_chars = sum(
        len(str(item.get("textPreview") or ""))
        for item in raw_recent_items
    )
    effective_recent_chars = sum(
        len(str(item.get("textPreview") or ""))
        for item in recent_items
    )
    recent_input_policy = {
        "requestedCount": requested_recent_count,
        "requestedChars": requested_recent_chars,
        "effectiveCount": len(recent_items),
        "effectiveChars": effective_recent_chars,
        "actualCount": actual_recent_count,
        "actualChars": actual_recent_chars,
        "truncated": bool(
            actual_recent_count > len(recent_items)
            or actual_recent_chars > effective_recent_chars
            or any(bool(item.get("truncated")) for item in recent_items)
        ),
        "unavailableReason": (
            "no_recent_input"
            if actual_recent_count == 0
            else "budget_exhausted"
            if actual_recent_count > 0 and not recent_items
            else ""
        ),
    }
    compact_ax_actual_node_count = int(compact_window_context.get("actualNodeCount") or 0)
    compact_ax_actual_char_count = int(compact_window_context.get("actualCharCount") or 0)
    ax_actual_node_count = compact_ax_actual_node_count or raw_ax_actual_node_count
    ax_actual_char_count = compact_ax_actual_char_count or raw_ax_actual_char_count
    ax_effective_node_count = int(compact_window_context.get("effectiveNodeCount") or 0)
    ax_effective_char_count = int(compact_window_context.get("effectiveCharCount") or 0)
    ax_has_raw_nodes = raw_ax_actual_node_count > 0
    ax_truncated = bool(compact_window_context.get("truncated")) or (
        ax_actual_node_count > ax_effective_node_count
        or ax_actual_char_count > ax_effective_char_count
    )
    ax_unavailable_reason = str(compact_window_context.get("unavailableReason") or "")
    if not ax_unavailable_reason:
        ax_unavailable_reason = (
            "no_accessibility_nodes"
            if not ax_has_raw_nodes
            else "budget_exhausted"
            if not compact_window_context
            else ""
        )
    ax_policy = {
        "requestedNodeCount": max(1, min(160, int(ax_node_maximum))),
        "requestedCharCount": max(1, min(12000, int(ax_char_maximum))),
        "effectiveNodeCount": ax_effective_node_count,
        "effectiveCharCount": ax_effective_char_count,
        "actualNodeCount": ax_actual_node_count,
        "actualCharCount": ax_actual_char_count,
        "truncated": ax_truncated,
        "unavailableReason": ax_unavailable_reason,
        "capturedAtMs": int(compact_window_context.get("capturedAtMs") or raw_ax_captured_at_ms),
    }
    grounding_items = _structured_grounding_evidence(evidence_items, limit=6)
    rag_hints, remaining_tokens = _take_budgeted_items(
        _rag_evidence_hints_from_grounding(grounding_items),
        remaining_tokens=remaining_tokens,
        text_keys=("text",),
        limit=6,
    )
    selected_rag_ids = {str(item.get("evidenceId") or "") for item in rag_hints}
    selected_grounding_items = [
        item
        for item in grounding_items
        if str(item.get("evidenceId") or "") in selected_rag_ids
    ]
    notebook_items = [item for item in raw_notebook_items if str(item.get("id") or "") in selected_rag_ids]
    timeline_items = [item for item in raw_timeline_items if str(item.get("id") or "") in selected_rag_ids]
    rag_tokens = sum(estimate_tokens(str(item.get("text") or "")) for item in rag_hints)
    source_tokens = {
        "currentInput": estimate_tokens(context or selected),
        "windowContext": window_context_tokens,
        "recentCompleteInputs": sum(
            estimate_tokens(str(item.get("textPreview") or "")) for item in recent_items
        ),
        "activityTimeline": sum(
            estimate_tokens(
                compact_whitespace(
                    f"{item.get('title') or ''} {item.get('summary') or ''}"
                )
            )
            for item in activity_items
        ),
        "plansAndTodos": sum(
            estimate_tokens(str(item.get("title") or item.get("notes") or ""))
            for item in planning_items
        ),
        "timeline": sum(
            estimate_tokens(str(item.get("text") or ""))
            for item in rag_hints
            if str(item.get("category") or "") == "timeline"
        ),
        "notebook": sum(
            estimate_tokens(str(item.get("text") or ""))
            for item in rag_hints
            if str(item.get("category") or "") == "notebook"
        ),
        "ragEvidence": rag_tokens,
    }
    packet_id = _packet_id(
        scene=scene,
        current_context=context,
        selected_text_hash=selected_text_hash,
        frontend_revision=frontend_revision,
        selection_epoch=selection_epoch,
        panel_session_id=panel_session_id,
    )
    return {
        "schemaVersion": SMART_RAG_CONTEXT_PACKET_SCHEMA_VERSION,
        "packetId": packet_id,
        "scene": scene,
        "priority": [
            "currentInput",
            "planning",
            "windowContext",
            "activityTimeline",
            "recentCompleteInputs",
            "groundingEvidence",
            "notebook",
        ],
        "currentInput": {
            "mode": scene,
            "committedTail": context,
            "selectedText": truncate_text(selected, 600) if scene == "active_rag" else "",
            "selectedTextHash": selected_text_hash,
            "intent": compact_whitespace(intent),
            "placement": compact_whitespace(placement),
            "rawInput": "",
            "preedit": "",
            "app": compact_whitespace(app),
            "project": compact_whitespace(project),
            "frontendRevision": int(frontend_revision),
            "selectionEpoch": int(selection_epoch),
            "panelSessionId": compact_whitespace(panel_session_id),
            "deleteState": {"recentDeletedTextHashes": []},
            "recentCompleteInputs": recent_items,
            "recentInputPolicy": recent_input_policy,
        },
        "windowContext": compact_window_context,
        "oneRing": {
            "role": "continuity_context",
            "maySupportIntent": True,
            "maySupportFacts": False,
            "baselineEvents": max(1, min(200, int(recent_input_baseline))),
            "maxEvents": max(1, min(200, int(recent_input_maximum))),
            "events": recent_items,
            "negativeSignals": [],
        },
        "planning": {
            "role": "work_intent_context",
            "maySupportIntent": True,
            "maySupportFacts": False,
            "items": planning_items[:6],
            "openTasks": [item for item in planning_items if item.get("kind") == "todo"][:6],
            "longTermGoals": [item for item in planning_items if item.get("kind") == "goal"][:6],
        },
        "activityTimeline": {
            "role": "continuity_context",
            "maySupportIntent": True,
            "maySupportFacts": False,
            "items": activity_items[:2],
        },
        "notebook": {
            "scope": {"project": compact_whitespace(project), "app": compact_whitespace(app)},
            "items": notebook_items[:12],
        },
        "timeline": {
            "project": compact_whitespace(project),
            "app": compact_whitespace(app),
            "timeWindow": "7d",
            "currentPhase": _current_phase(timeline_items),
            "recentDecisions": timeline_items[:5],
            "openTasks": [item for item in planning_items if item.get("kind") == "todo"][:6],
            "expiresAtMs": now_ms() + 7 * 24 * 60 * 60 * 1000,
        },
        "groundingEvidence": selected_grounding_items,
        "ragEvidenceHints": rag_hints,
        "outputContract": {
            "maxCandidates": max(1, int(max_candidates)),
            "minCandidateChars": 40 if int(max_chars) == 0 or int(max_chars) >= 80 else 2,
            "maxCandidateChars": max(0, int(max_chars)),
            "outputFormat": "candidate_document" if int(max_chars) == 0 else "candidate_json",
            "intent": compact_whitespace(intent),
            "placement": compact_whitespace(placement),
            "allowedPlacements": ["replace_selection", "insert_after_selection", "append_at_cursor", "show_only"],
            "requireSourceRefs": True,
            "requireConfidence": False,
        },
        "constraints": {
            "forbiddenTerms": ["下一步", "接下来", "根据上述", "可以进行", "可以继续"],
            "forbiddenHashes": [selected_text_hash] if selected_text_hash else [],
            "recentDeletedHashes": [],
            "tombstonedMemoryIds": [],
            "suppressedCandidateIds": [],
            "privacyMode": "redacted",
            "latencyBudgetMs": int(latency_budget_ms),
            "remoteModelAllowed": bool(remote_model_allowed),
        },
        "trace": {
            "packetId": packet_id,
            "evidenceCount": len(evidence_items),
            "notebookCount": len(notebook_items),
            "timelineCount": len(timeline_items),
            "oneRingEventCount": len(recent_items),
            "planningCount": len(planning_items),
            "activityTimelineCount": len(activity_items),
            "recentInputPolicy": recent_input_policy,
            "axPolicy": ax_policy,
            "tokenBudget": token_budget,
            "reservedOutputTokens": reserved_tokens,
            "availableContextTokens": available_tokens,
            "estimatedContextTokens": sum(
                source_tokens[key]
                for key in (
                    "currentInput",
                    "windowContext",
                    "plansAndTodos",
                    "activityTimeline",
                    "recentCompleteInputs",
                    "ragEvidence",
                )
            ),
            "remainingContextTokens": remaining_tokens,
            "withinSoftBudget": remaining_tokens >= 0,
            "contextSourceCounts": {
                "recentInputs": len(recent_items),
                "windowNodes": len(compact_window_context.get("nodes", []))
                if isinstance(compact_window_context.get("nodes"), list)
                else 0,
                "plansAndTodos": len(planning_items),
                "activityTimeline": len(activity_items),
                "timeline": len(timeline_items),
                "notebook": len(notebook_items),
                "ragEvidence": len(rag_hints),
            },
            "contextSourceTokens": source_tokens,
            "trimmedSourceCounts": {
                "recentInputs": max(0, len(raw_recent_items) - len(recent_items)),
                "windowNodes": max(
                    0,
                    len(raw_window_context.get("nodes", []))
                    - len(compact_window_context.get("nodes", [])),
                )
                if isinstance(raw_window_context.get("nodes"), list)
                and isinstance(compact_window_context.get("nodes"), list)
                else 0,
                "plansAndTodos": max(0, len(raw_planning_items) - len(planning_items)),
                "activityTimeline": max(0, len(raw_activity_items) - len(activity_items)),
                "ragEvidence": max(0, len(grounding_items) - len(rag_hints)),
            },
        },
    }


def _take_budgeted_recent_items(
    items: Sequence[dict[str, object]],
    *,
    remaining_tokens: int,
    limit: int,
    max_chars: int = 12000,
) -> tuple[list[dict[str, object]], int]:
    selected_reversed: list[dict[str, object]] = []
    remaining = max(0, int(remaining_tokens))
    remaining_chars = max(1, min(100000, int(max_chars)))
    for item in reversed(items):
        text = compact_whitespace(str(item.get("textPreview") or ""))
        if not text or remaining_chars <= 0:
            break
        truncated = len(text) > remaining_chars
        if truncated and selected_reversed:
            break
        if truncated:
            text = text[:remaining_chars].rstrip()
        cost = estimate_tokens(text) + 2
        if not text or cost > remaining:
            continue
        selected_item = item
        if truncated:
            selected_item = {**item, "textPreview": text, "truncated": True}
        selected_reversed.append(selected_item)
        remaining -= cost
        remaining_chars -= len(text)
        if len(selected_reversed) >= max(0, int(limit)):
            break
    selected_reversed.reverse()
    return selected_reversed, remaining


def _take_budgeted_items(
    items: Sequence[dict[str, object]],
    *,
    remaining_tokens: int,
    text_keys: Sequence[str],
    limit: int,
) -> tuple[list[dict[str, object]], int]:
    selected: list[dict[str, object]] = []
    remaining = max(0, int(remaining_tokens))
    for item in items:
        text = compact_whitespace(" ".join(str(item.get(key) or "") for key in text_keys))
        cost = estimate_tokens(text) + 2
        if not text or cost > remaining:
            continue
        selected.append(item)
        remaining -= cost
        if len(selected) >= max(0, int(limit)):
            break
    return selected, remaining


def _structured_grounding_evidence(
    evidence: Sequence[ActiveRagEvidence],
    *,
    limit: int,
) -> list[dict[str, object]]:
    """Keep typed facts small and deduplicate the same fact across retrieval lanes."""

    result: list[dict[str, object]] = []
    semantic_keys: list[str] = []
    for item in evidence:
        if (
            _is_recent_input_evidence(item)
            or _is_planning_evidence(item)
            or _is_activity_timeline_evidence(item)
            or item.source_type in {"phrase", "surface_phrase"}
        ):
            continue
        metadata = item.metadata if isinstance(item.metadata, dict) else {}
        if bool(metadata.get("contextOnly")) or metadata.get("maySupportFacts") is False:
            continue
        title = truncate_text(
            compact_whitespace(
                str(metadata.get("title") or metadata.get("bookTitle") or "")
            ),
            100,
        )
        preview = _canonical_evidence_text(item, metadata=metadata)
        semantic_key = _semantic_fact_key(preview or title)
        if not semantic_key or any(
            _semantic_facts_overlap(semantic_key, existing)
            for existing in semantic_keys
        ):
            continue
        semantic_keys.append(semantic_key)
        source_type = _typed_grounding_source_type(item, metadata=metadata)
        result.append(
            {
                "evidenceId": item.evidence_id,
                "sourceType": source_type,
                "sourceLane": item.source_lane,
                "category": "notebook" if _is_notebook_evidence(item) else "rag",
                "title": title,
                "preview": preview,
                "ref": _grounding_reference(item, source_type=source_type),
                "maySupportFacts": True,
            }
        )
    return _diversify_grounding_items(result, limit=limit)


def _diversify_grounding_items(
    items: Sequence[dict[str, object]],
    *,
    limit: int,
) -> list[dict[str, object]]:
    """Keep rank order while reserving room for both Atom and Topic Book facts."""

    bounded_limit = max(0, int(limit))
    if bounded_limit == 0:
        return []
    selected = [dict(item) for item in items[:bounded_limit]]
    required_types = {
        source_type
        for source_type in ("memory_atom", "memory_book")
        if any(str(item.get("sourceType") or "") == source_type for item in items)
    }
    for required_type in ("memory_atom", "memory_book"):
        if required_type not in required_types or any(
            str(item.get("sourceType") or "") == required_type
            for item in selected
        ):
            continue
        replacement = next(
            (
                dict(item)
                for item in items[bounded_limit:]
                if str(item.get("sourceType") or "") == required_type
            ),
            None,
        )
        if replacement is None:
            continue
        type_counts = {
            source_type: sum(
                1
                for item in selected
                if str(item.get("sourceType") or "") == source_type
            )
            for source_type in required_types
        }
        replacement_index = next(
            (
                index
                for index in range(len(selected) - 1, -1, -1)
                if (
                    str(selected[index].get("sourceType") or "") not in required_types
                    or type_counts.get(str(selected[index].get("sourceType") or ""), 0) > 1
                )
            ),
            -1,
        )
        if replacement_index >= 0:
            selected[replacement_index] = replacement
    return selected


def _rag_evidence_hints_from_grounding(
    grounding: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    return [
        {
            "evidenceId": str(item.get("evidenceId") or ""),
            "sourceType": str(item.get("sourceType") or ""),
            "sourceLane": str(item.get("sourceLane") or ""),
            "category": str(item.get("category") or "rag"),
            # One canonical field only. Do not concatenate title, summary,
            # preview and text into a repeated sentence.
            "text": compact_whitespace(
                str(item.get("preview") or item.get("title") or "")
            ),
        }
        for item in grounding
        if compact_whitespace(str(item.get("preview") or item.get("title") or ""))
    ]




def _canonical_evidence_text(
    item: ActiveRagEvidence,
    *,
    metadata: Mapping[str, object],
) -> str:
    for value in (
        item.preview,
        metadata.get("summary"),
        item.text,
        metadata.get("title"),
    ):
        text = truncate_text(
            _deduplicate_adjacent_evidence_text(str(value or "")),
            240,
        )
        if text:
            return text
    surface_hints = metadata.get("surfaceHints")
    if isinstance(surface_hints, list):
        for value in surface_hints:
            text = truncate_text(compact_whitespace(str(value or "")), 240)
            if text:
                return text
    return ""


def _deduplicate_adjacent_evidence_text(value: str) -> str:
    """Collapse the legacy ``memory_items`` text+summary exact duplicate."""

    text = compact_whitespace(value)
    if not text:
        return ""
    for separator_chars in (0, 1):
        repeated_chars = len(text) - separator_chars
        if repeated_chars <= 0 or repeated_chars % 2:
            continue
        half = repeated_chars // 2
        left = text[:half]
        right = text[half + separator_chars :]
        separator = text[half : half + separator_chars]
        if left and left == right and (separator_chars == 0 or separator == " "):
            return left
    return text


def _semantic_fact_key(value: str) -> str:
    return "".join(
        character.lower()
        for character in compact_whitespace(value)
        if character.isalnum() or "\u3400" <= character <= "\u9fff"
    )


def _semantic_facts_overlap(left: str, right: str) -> bool:
    if left == right:
        return True
    shorter, longer = sorted((left, right), key=len)
    return len(shorter) >= 8 and shorter in longer


def _grounding_reference(
    item: ActiveRagEvidence,
    *,
    source_type: str,
) -> dict[str, str]:
    if item.atom_ids:
        return {"kind": "atom", "id": str(item.atom_ids[0])}
    if item.book_ids:
        return {"kind": "book", "id": str(item.book_ids[0])}
    evidence_id = compact_whitespace(item.evidence_id)
    lowered = evidence_id.lower()
    if lowered.startswith("atom:") or source_type in {"atom", "memory_atom"}:
        kind = "atom"
    elif lowered.startswith("book:") or source_type in {"book", "memory_book"}:
        kind = "book"
    else:
        kind = "evidence"
    return {"kind": kind, "id": evidence_id}


def _typed_grounding_source_type(
    item: ActiveRagEvidence,
    *,
    metadata: Mapping[str, object],
) -> str:
    explicit = compact_whitespace(str(metadata.get("sourceType") or ""))
    if explicit and explicit not in {"memory", "rag"}:
        return explicit
    if item.atom_ids:
        return "memory_atom"
    if item.book_ids:
        return "memory_book"
    return explicit or compact_whitespace(item.source_type) or "rag"


def _packet_id(
    *,
    scene: str,
    current_context: str,
    selected_text_hash: str,
    frontend_revision: int,
    selection_epoch: int,
    panel_session_id: str,
) -> str:
    base = "|".join(
        (
            compact_whitespace(scene),
            stable_text_hash(current_context),
            selected_text_hash,
            str(int(frontend_revision)),
            str(int(selection_epoch)),
            compact_whitespace(panel_session_id),
        )
    )
    return f"pkt_{stable_text_hash(base).split(':', 1)[-1][:16]}_{uuid.uuid4().hex[:6]}"


def _is_notebook_evidence(item: ActiveRagEvidence) -> bool:
    return item.source_type in {"memory", "phrase"} or item.source_lane in {
        "bm25_tags",
        "tag_memo",
        "timeline_daily_book",
        "timeline_memory_book",
    }


def _is_planning_evidence(item: ActiveRagEvidence) -> bool:
    return item.source_type in {"daily_plan", "todo", "goal"} or item.source_lane.startswith("planning_")


def _is_timeline_evidence(item: ActiveRagEvidence) -> bool:
    metadata = item.metadata if isinstance(item.metadata, dict) else {}
    return bool(metadata.get("timelineContext")) or item.source_lane.startswith("timeline_")


def _is_activity_timeline_evidence(item: ActiveRagEvidence) -> bool:
    return (
        item.source_type == "activity_timeline"
        or item.source_lane == "timeline_approved_activity"
    )


def _is_recent_input_evidence(item: ActiveRagEvidence) -> bool:
    return item.source_type == "recent_input_context" or item.source_lane == "timeline_recent_input"


def _recent_input_items(
    evidence: Sequence[ActiveRagEvidence],
    *,
    current_context: str,
    selected_text: str,
) -> list[dict[str, object]]:
    """Keep recent complete inputs separate from grounding evidence.

    The editable field remains the primary request. Recent inputs only restore
    continuity when IMK exposes a short cursor tail; they never increase the
    RAG evidence count or authorize factual claims.
    """

    current_values = {
        compact_whitespace(value).lower()
        for value in (current_context, selected_text)
        if compact_whitespace(value)
    }
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in evidence:
        if not _is_recent_input_evidence(item):
            continue
        text = truncate_text(compact_whitespace(item.text), 480)
        normalized = text.lower()
        if not text or normalized in current_values or normalized in seen:
            continue
        seen.add(normalized)
        result.append(_one_ring_like_event(item, text=text))
    return result[-4:]


def _notebook_item(item: ActiveRagEvidence) -> dict[str, object]:
    metadata = item.metadata if isinstance(item.metadata, dict) else {}
    title = compact_whitespace(str(metadata.get("title") or metadata.get("bookTitle") or item.text))
    summary = compact_whitespace(str(metadata.get("summary") or item.preview or item.text))
    surface_hints = metadata.get("surfaceHints") if isinstance(metadata.get("surfaceHints"), list) else []
    return {
        "id": item.evidence_id,
        "kind": item.source_lane or item.source_type,
        "title": truncate_text(title, 80),
        "summary": truncate_text(summary, 100),
        "tags": list(item.tags[:8]),
        "surfaceHints": [truncate_text(compact_whitespace(str(value)), 32) for value in surface_hints[:6]],
        "sourceEventIds": list(item.evidence_event_ids[:8]),
        "confidence": item.confidence,
        "directCandidateAllowed": False,
    }


def _timeline_item(item: ActiveRagEvidence) -> dict[str, object]:
    metadata = item.metadata if isinstance(item.metadata, dict) else {}
    text = compact_whitespace(str(metadata.get("summary") or metadata.get("title") or item.preview or item.text))
    return {
        "id": item.evidence_id,
        "text": truncate_text(text, 100),
        "source": item.source_lane or item.source_type,
        "tags": list(item.tags[:6]),
        "sourceEventIds": list(item.evidence_event_ids[:8]),
    }


def _activity_timeline_item(item: ActiveRagEvidence) -> dict[str, object]:
    metadata = item.metadata if isinstance(item.metadata, dict) else {}
    title = truncate_text(
        compact_whitespace(str(metadata.get("title") or item.text or item.preview)),
        120,
    )
    summary = truncate_text(
        compact_whitespace(str(metadata.get("summary") or item.preview or item.text)),
        320,
    )
    ref = metadata.get("ref") if isinstance(metadata.get("ref"), dict) else {}
    return {
        "id": item.evidence_id,
        "title": title,
        "summary": summary,
        "date": compact_whitespace(str(metadata.get("date") or "")),
        "period": compact_whitespace(str(metadata.get("period") or "")),
        "startMs": _nonnegative_int(metadata.get("startMs")),
        "endMs": _nonnegative_int(metadata.get("endMs")),
        "apps": [
            truncate_text(compact_whitespace(str(value)), 80)
            for value in metadata.get("apps", [])
            if compact_whitespace(str(value))
        ][:8] if isinstance(metadata.get("apps"), list) else [],
        "evidenceCount": _nonnegative_int(metadata.get("evidenceCount")),
        "redactedEventCount": _nonnegative_int(metadata.get("redactedEventCount")),
        "ref": {
            "kind": compact_whitespace(str(ref.get("kind") or "timeline")),
            "id": compact_whitespace(str(ref.get("id") or item.evidence_id)),
        },
        "maySupportFacts": False,
    }


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _one_ring_like_event(item: ActiveRagEvidence, *, text: str | None = None) -> dict[str, object]:
    metadata = item.metadata if isinstance(item.metadata, dict) else {}
    preview = truncate_text(compact_whitespace(text if text is not None else item.text), 480)
    return {
        "type": "recent_input_context",
        "textHash": stable_text_hash(preview),
        "textPreview": preview,
        "source": item.source_lane or item.source_type,
        "sourceEventIds": list(item.evidence_event_ids[:32]),
        "atMs": int(metadata.get("createdAtMs") or now_ms()),
    }


def _planning_item(item: ActiveRagEvidence) -> dict[str, object]:
    metadata = item.metadata if isinstance(item.metadata, dict) else {}
    title = compact_whitespace(str(metadata.get("title") or item.text or item.preview))
    return {
        "id": item.evidence_id,
        "kind": "todo" if item.source_type == "todo" else ("goal" if item.source_type == "goal" else "daily_plan"),
        "title": truncate_text(title, 240),
        "detail": truncate_text(compact_whitespace(str(metadata.get("summary") or item.preview)), 600),
        "status": compact_whitespace(str(metadata.get("status") or "")),
        "priority": int(metadata.get("priority") or 0),
        "source": item.source_lane or item.source_type,
    }


def _current_phase(items: list[dict[str, object]]) -> str:
    for item in items:
        text = compact_whitespace(str(item.get("text") or ""))
        if text:
            return truncate_text(text, 80)
    return ""
