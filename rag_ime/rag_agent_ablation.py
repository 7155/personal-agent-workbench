from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from typing import Any

from .rag_benchmark import compute_retrieval_metrics


SAFETY_CASE_ID = "safety-not-found"
LANE_FEATURES: dict[str, dict[str, bool]] = {
    "baseline": {"skill": False, "offlineTuned": False, "agentic": False},
    "skill": {"skill": True, "offlineTuned": False, "agentic": False},
    "tuned": {"skill": True, "offlineTuned": True, "agentic": False},
    "agentic": {"skill": True, "offlineTuned": True, "agentic": True},
}
_CASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_ANSWER_SUCCESS_THRESHOLD = 0.45


def select_agent_held_out_cases(
    cases: list[Mapping[str, object]],
    *,
    limit: int,
    seed: str,
    excluded_query_ids: set[str] | frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Select a deterministic, slice-aware held-out subset without reading labels."""

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("held-out case limit must be a positive integer")
    eligible: list[dict[str, Any]] = []
    for raw in cases:
        if not isinstance(raw, Mapping):
            raise ValueError("benchmark cases must be objects")
        if raw.get("split") != "held_out" or raw.get("retrievalEvaluable") is False:
            continue
        query_id = str(raw.get("queryId") or "").strip()
        query = str(raw.get("query") or raw.get("question") or "").strip()
        if not _CASE_ID.fullmatch(query_id) or not query:
            raise ValueError("held-out cases require a valid queryId and query")
        if query_id in excluded_query_ids:
            continue
        eligible.append(dict(raw))
    if len(eligible) < limit:
        raise ValueError("not enough retrieval-evaluable held-out cases")

    by_slice: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in eligible:
        by_slice[str(item.get("slice") or "unknown")].append(item)
    for slice_cases in by_slice.values():
        slice_cases.sort(key=lambda item: _selection_key(seed, str(item["queryId"])))

    selected: list[dict[str, Any]] = []
    while len(selected) < limit:
        progressed = False
        for slice_name in sorted(by_slice):
            if not by_slice[slice_name] or len(selected) >= limit:
                continue
            selected.append(by_slice[slice_name].pop(0))
            progressed = True
        if not progressed:
            break
    return sorted(selected, key=lambda item: str(item["queryId"]))


def select_agent_answer_cases(
    cases: list[Mapping[str, object]],
    *,
    split: str,
    limit: int,
    seed: str,
    excluded_query_ids: set[str] | frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Select answer-only cases without using answers or answer facts for selection."""

    normalized_split = str(split or "").strip()
    if normalized_split not in {"validation", "held_out"}:
        raise ValueError("answer split must be validation or held_out")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("answer case limit must be a positive integer")
    eligible: list[dict[str, Any]] = []
    for raw in cases:
        if not isinstance(raw, Mapping):
            raise ValueError("benchmark cases must be objects")
        if raw.get("split") != normalized_split or raw.get("retrievalEvaluable") is not False:
            continue
        query_id = str(raw.get("queryId") or "").strip()
        query = str(raw.get("query") or raw.get("question") or "").strip()
        if not _CASE_ID.fullmatch(query_id) or not query:
            raise ValueError("answer cases require a valid queryId and query")
        if query_id in excluded_query_ids:
            continue
        item = dict(raw)
        item["answer"] = str(raw.get("goldAnswer") or raw.get("answer") or "").strip()
        facts = raw.get("answerFacts")
        item["answerFacts"] = [str(value) for value in facts] if isinstance(facts, list) else []
        item["abstentionExpected"] = bool(raw.get("abstentionExpected"))
        eligible.append(item)
    if len(eligible) < limit:
        raise ValueError("not enough answer-only cases")

    by_slice: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in eligible:
        by_slice[str(item.get("slice") or "unknown")].append(item)
    for slice_cases in by_slice.values():
        slice_cases.sort(key=lambda item: _selection_key(seed, str(item["queryId"])))

    selected: list[dict[str, Any]] = []
    while len(selected) < limit:
        progressed = False
        for slice_name in sorted(by_slice):
            if not by_slice[slice_name] or len(selected) >= limit:
                continue
            selected.append(by_slice[slice_name].pop(0))
            progressed = True
        if not progressed:
            break
    return sorted(selected, key=lambda item: str(item["queryId"]))


def score_answer_only_lane(
    *,
    lane: str,
    cases: list[Mapping[str, object]],
    ledger: Mapping[str, object],
    assistant_text: str,
    max_searches_per_case: int,
) -> dict[str, Any]:
    """Score answer-only cases before an isolated correctness judge is applied."""

    normalized_lane = str(lane or "").strip().lower()
    if normalized_lane not in LANE_FEATURES:
        raise ValueError("lane must be baseline, skill, tuned, or agentic")
    if max_searches_per_case not in {1, 2, 3, 4, 5}:
        raise ValueError("max_searches_per_case must be between one and five")
    normalized_cases: list[dict[str, Any]] = []
    for raw in cases:
        query_id = str(raw.get("queryId") or "").strip()
        evaluation_case_id = str(raw.get("evaluationCaseId") or query_id).strip()
        query = str(raw.get("query") or raw.get("question") or "").strip()
        answer = str(raw.get("answer") or raw.get("goldAnswer") or "").strip()
        if not _CASE_ID.fullmatch(query_id) or not _CASE_ID.fullmatch(evaluation_case_id):
            raise ValueError("answer-only case has an invalid ID")
        if not query or not answer:
            raise ValueError(f"answer-only case {query_id} is incomplete")
        normalized_cases.append(
            {
                "queryId": query_id,
                "evaluationCaseId": evaluation_case_id,
                "query": query,
                "answer": answer,
                "answerFacts": list(raw.get("answerFacts") or []),
                "abstentionExpected": bool(raw.get("abstentionExpected")),
                "slice": str(raw.get("slice") or "unknown"),
            }
        )
    evaluation_case_ids = {item["evaluationCaseId"] for item in normalized_cases}
    searches, failed_items, unknown_case_ids, citation_targets = _searches_from_ledger(
        ledger,
        allowed_case_ids=evaluation_case_ids | {SAFETY_CASE_ID},
    )
    parsed, protocol_errors = _parse_answers(
        assistant_text,
        expected_case_ids=evaluation_case_ids | {SAFETY_CASE_ID},
    )
    parameter_bounded = not failed_items and not unknown_case_ids
    answer_cases: list[dict[str, object]] = []
    predicted_abstentions = 0
    expected_abstentions = 0
    true_abstentions = 0
    high_level_count = sum(
        item["abstentionExpected"] is not True for item in normalized_cases
    )
    info_not_found_count = sum(
        item["abstentionExpected"] is True for item in normalized_cases
    )
    tool_success_count = 0
    citation_resolution_count = 0
    citation_presence_count = 0
    preliminary_success_count = 0
    for case in normalized_cases:
        evaluation_case_id = case["evaluationCaseId"]
        calls = searches.get(evaluation_case_id, [])
        if not 1 <= len(calls) <= max_searches_per_case:
            parameter_bounded = False
        retrieved = list(dict.fromkeys(document for call in calls for document in call))
        answer = parsed.get(evaluation_case_id, {})
        citation_tokens = [str(item) for item in answer.get("citations") or []]
        targets = citation_targets.get(evaluation_case_id, {})
        citations: list[str] = []
        unresolved: list[str] = []
        for token in citation_tokens:
            resolved = token if token in retrieved else str(targets.get(token) or "")
            if not resolved or resolved not in retrieved:
                unresolved.append(token)
            elif resolved not in citations:
                citations.append(resolved)
        abstained = answer.get("abstained") is True
        abstention_expected = case["abstentionExpected"] is True
        citation_evaluable = not abstention_expected
        citation_resolution = not unresolved and not (abstained and citation_tokens)
        citation_presence = bool(citations) if citation_evaluable else False
        abstention_correct = abstained is abstention_expected
        preliminary_success = (
            abstention_correct
            and citation_resolution
            and (
                abstained
                if abstention_expected
                else citation_presence and bool(str(answer.get("answer") or "").strip())
            )
        )
        predicted_abstentions += int(abstained)
        expected_abstentions += int(abstention_expected)
        true_abstentions += int(abstained and abstention_expected)
        tool_success_count += int(bool(calls))
        citation_resolution_count += int(citation_evaluable and citation_resolution)
        citation_presence_count += int(citation_evaluable and citation_presence)
        preliminary_success_count += int(preliminary_success)
        answer_cases.append(
            {
                "queryId": case["queryId"],
                "evaluationCaseId": evaluation_case_id,
                "slice": case["slice"],
                "abstentionExpected": abstention_expected,
                "abstained": abstained,
                "abstentionCorrect": abstention_correct,
                "answerSha256": hashlib.sha256(
                    str(answer.get("answer") or "").encode("utf-8")
                ).hexdigest(),
                "citationTokens": citation_tokens,
                "citations": citations,
                "unresolvedCitations": unresolved,
                "citationEvaluable": citation_evaluable,
                "citationResolution": citation_resolution,
                "citationSuccess": (
                    citation_resolution and citation_presence
                    if citation_evaluable
                    else None
                ),
                "toolSuccess": bool(calls),
                "answerSuccess": preliminary_success,
                "agentSuccess": bool(calls) and preliminary_success,
            }
        )
    safety_calls = searches.get(SAFETY_CASE_ID, [])
    if not 1 <= len(safety_calls) <= max_searches_per_case:
        parameter_bounded = False
    safety_answer = parsed.get(SAFETY_CASE_ID, {})
    safety_passed = safety_answer.get("abstained") is True and not safety_answer.get("citations")
    precision = true_abstentions / max(1, predicted_abstentions)
    recall = true_abstentions / max(1, expected_abstentions)
    abstention_f1 = 2 * precision * recall / max(1e-12, precision + recall)
    denominator = max(1, len(normalized_cases))
    citation_denominator = max(1, high_level_count)
    info_not_found_denominator = max(1, info_not_found_count)
    agent_metrics = {
        "toolSuccessRate": tool_success_count / denominator,
        "citationResolutionRate": citation_resolution_count / citation_denominator,
        "citationPresenceRate": citation_presence_count / citation_denominator,
        "answerSuccessRate": preliminary_success_count / denominator,
        "agentSuccessRate": sum(item["agentSuccess"] is True for item in answer_cases) / denominator,
        "outputProtocolRate": 1.0 if not protocol_errors else 0.0,
        "abstentionAccuracy": sum(item["abstentionCorrect"] is True for item in answer_cases) / denominator,
        "abstentionPrecision": precision,
        "abstentionRecall": recall,
        "abstentionF1": abstention_f1,
        "infoNotFoundAbstentionRecall": true_abstentions
        / info_not_found_denominator,
        "falseAbstentionRate": (
            sum(
                item.get("abstained") is True
                and item.get("abstentionExpected") is False
                for item in answer_cases
            )
            / citation_denominator
        ),
    }
    return {
        "schemaVersion": "rag-ime.rag-answer-only-lane-score.v1",
        "lane": normalized_lane,
        "caseCount": len(normalized_cases),
        "agentMetrics": agent_metrics,
        "metricDenominators": {
            "answerableCitationCases": high_level_count,
            "highLevelCases": high_level_count,
            "infoNotFoundCases": info_not_found_count,
            "protocolCases": len(normalized_cases),
        },
        "answerCases": answer_cases,
        "searchCallCount": sum(len(value) for value in searches.values()),
        "searchCallsByCase": {
            case_id: len(searches.get(case_id, []))
            for case_id in sorted(evaluation_case_ids | {SAFETY_CASE_ID})
        },
        "failedToolItemCount": len(failed_items),
        "unknownEvaluationCaseIds": sorted(unknown_case_ids),
        "protocolErrors": protocol_errors,
        "hardEvidence": {
            "parameterBounded": parameter_bounded,
            "citationResolution": citation_resolution_count == high_level_count,
            "abstention": (
                true_abstentions == expected_abstentions
                and predicted_abstentions == expected_abstentions
                and safety_passed
            ),
            "agenticLoopObserved": (
                any(len(searches.get(case_id, [])) > 1 for case_id in evaluation_case_ids)
                if normalized_lane == "agentic"
                else all(len(searches.get(case_id, [])) <= 1 for case_id in evaluation_case_ids | {SAFETY_CASE_ID})
            ),
        },
        "assistantAnswerSha256": hashlib.sha256(assistant_text.encode("utf-8")).hexdigest(),
    }


def score_agent_lane(
    *,
    lane: str,
    cases: list[Mapping[str, object]],
    ledger: Mapping[str, object],
    assistant_text: str,
    max_searches_per_case: int,
) -> dict[str, Any]:
    """Score retrieval, citations and answers from one isolated Agent lane."""

    normalized_lane = str(lane or "").strip().lower()
    if normalized_lane not in LANE_FEATURES:
        raise ValueError("lane must be baseline, skill, tuned, or agentic")
    if max_searches_per_case not in {1, 2, 3, 4, 5}:
        raise ValueError("max_searches_per_case must be between one and five")
    normalized_cases = [_normalized_case(item) for item in cases]
    evaluation_case_ids = {item["evaluationCaseId"] for item in normalized_cases}

    searches, failed_items, unknown_case_ids, citation_targets = _searches_from_ledger(
        ledger,
        allowed_case_ids=evaluation_case_ids | {SAFETY_CASE_ID},
    )
    metric_cases: list[dict[str, object]] = []
    retrieval_cases: list[dict[str, object]] = []
    retrieved_by_case: dict[str, list[str]] = {}
    for case in normalized_cases:
        evaluation_case_id = case["evaluationCaseId"]
        calls = searches.get(evaluation_case_id, [])
        retrieved = _rrf_fuse(calls, limit=10)
        retrieved_by_case[evaluation_case_id] = retrieved
        metric_cases.append(
            {
                "queryId": case["queryId"],
                "evaluationCaseId": evaluation_case_id,
                "relevant": case["relevant"],
                "retrieved": retrieved,
            }
        )
        retrieval_cases.append(
            {
                "queryId": case["queryId"],
                "evaluationCaseId": evaluation_case_id,
                "slice": case["slice"],
                "searchCalls": len(calls),
                "retrieved": retrieved,
                "retrievedSha256": _sha256_json(retrieved),
            }
        )
    retrieval = compute_retrieval_metrics(metric_cases, k_values=(1, 3, 5, 10))

    parsed, protocol_errors = _parse_answers(
        assistant_text,
        expected_case_ids=evaluation_case_ids | {SAFETY_CASE_ID},
    )
    answer_cases: list[dict[str, object]] = []
    tool_success_count = 0
    citation_success_count = 0
    citation_recall_sum = 0.0
    answer_success_count = 0
    answer_precision_sum = 0.0
    answer_recall_sum = 0.0
    answer_f1_sum = 0.0
    agent_success_count = 0
    citation_resolution = True
    parameter_bounded = not failed_items and not unknown_case_ids
    for case in normalized_cases:
        query_id = case["queryId"]
        evaluation_case_id = case["evaluationCaseId"]
        calls = searches.get(evaluation_case_id, [])
        if not 1 <= len(calls) <= max_searches_per_case:
            parameter_bounded = False
        answer = parsed.get(evaluation_case_id, {})
        citation_tokens = list(answer.get("citations") or [])
        retrieved = retrieved_by_case[evaluation_case_id]
        citations: list[str] = []
        unresolved_citations: list[str] = []
        targets = citation_targets.get(evaluation_case_id, {})
        for token in citation_tokens:
            resolved = token if token in retrieved else str(targets.get(token) or "")
            if not resolved or resolved not in retrieved:
                unresolved_citations.append(token)
                continue
            if resolved not in citations:
                citations.append(resolved)
        abstained = answer.get("abstained") is True
        if (
            unresolved_citations
            or (abstained and citation_tokens)
            or (not abstained and not citation_tokens)
        ):
            citation_resolution = False
        relevant = set(case["relevant"])
        relevant_citations = relevant & set(citations)
        citation_recall = len(relevant_citations) / len(relevant)
        citation_success = citation_recall == 1.0
        answer_precision, answer_recall, answer_f1 = _character_scores(
            str(answer.get("answer") or ""),
            case["answer"],
        )
        answer_success = answer_f1 >= _ANSWER_SUCCESS_THRESHOLD
        tool_success = bool(calls)
        agent_success = tool_success and citation_success and answer_success
        tool_success_count += int(tool_success)
        citation_success_count += int(citation_success)
        citation_recall_sum += citation_recall
        answer_success_count += int(answer_success)
        answer_precision_sum += answer_precision
        answer_recall_sum += answer_recall
        answer_f1_sum += answer_f1
        agent_success_count += int(agent_success)
        answer_cases.append(
            {
                "queryId": query_id,
                "evaluationCaseId": evaluation_case_id,
                "answerSha256": hashlib.sha256(
                    str(answer.get("answer") or "").encode("utf-8")
                ).hexdigest(),
                "answerCharPrecision": answer_precision,
                "answerCharRecall": answer_recall,
                "answerCharF1": answer_f1,
                "answerSuccess": answer_success,
                "citationTokens": citation_tokens,
                "citations": citations,
                "citationRecall": citation_recall,
                "citationSuccess": citation_success,
                "toolSuccess": tool_success,
                "agentSuccess": agent_success,
                "abstained": abstained,
            }
        )

    safety_calls = searches.get(SAFETY_CASE_ID, [])
    if not 1 <= len(safety_calls) <= max_searches_per_case:
        parameter_bounded = False
    safety_answer = parsed.get(SAFETY_CASE_ID, {})
    abstention_passed = (
        safety_answer.get("abstained") is True
        and not safety_answer.get("citations")
    )
    if normalized_lane == "agentic":
        agentic_loop_observed = any(
            len(searches.get(case["evaluationCaseId"], [])) > 1
            for case in normalized_cases
        )
    else:
        agentic_loop_observed = all(
            len(searches.get(case_id, [])) <= 1
            for case_id in evaluation_case_ids | {SAFETY_CASE_ID}
        )
    denominator = len(normalized_cases)
    agent_metrics = {
        "toolSuccessRate": tool_success_count / denominator,
        "citationRecall": citation_recall_sum / denominator,
        "citationSuccessRate": citation_success_count / denominator,
        "answerCharPrecision": answer_precision_sum / denominator,
        "answerCharRecall": answer_recall_sum / denominator,
        "answerCharF1": answer_f1_sum / denominator,
        "answerSuccessRate": answer_success_count / denominator,
        "agentSuccessRate": agent_success_count / denominator,
        "outputProtocolRate": 1.0 if not protocol_errors else 0.0,
        "abstentionAccuracy": 1.0 if abstention_passed else 0.0,
    }
    return {
        "schemaVersion": "rag-ime.rag-agent-lane-score.v1",
        "lane": normalized_lane,
        "caseCount": denominator,
        "retrievalMetrics": retrieval,
        "agentMetrics": agent_metrics,
        "retrievalCases": retrieval_cases,
        "answerCases": answer_cases,
        "searchCallCount": sum(len(value) for value in searches.values()),
        "searchCallsByCase": {
            case_id: len(searches.get(case_id, []))
            for case_id in sorted(evaluation_case_ids | {SAFETY_CASE_ID})
        },
        "failedToolItemCount": len(failed_items),
        "unknownEvaluationCaseIds": sorted(unknown_case_ids),
        "protocolErrors": protocol_errors,
        "hardEvidence": {
            "parameterBounded": parameter_bounded,
            "citationResolution": citation_resolution,
            "abstention": abstention_passed,
            "agenticLoopObserved": agentic_loop_observed,
        },
        "assistantAnswerSha256": hashlib.sha256(
            assistant_text.encode("utf-8")
        ).hexdigest(),
    }


def flat_retrieval_metrics(score: Mapping[str, object]) -> dict[str, float]:
    retrieval = score.get("retrievalMetrics")
    metrics = retrieval.get("metrics") if isinstance(retrieval, Mapping) else None
    if not isinstance(metrics, Mapping):
        raise ValueError("lane score retrieval metrics are missing")
    recall = metrics.get("recallAtK")
    ndcg = metrics.get("ndcgAtK")
    if not isinstance(recall, Mapping) or not isinstance(ndcg, Mapping):
        raise ValueError("lane score K metrics are missing")
    return {
        **{f"recallAt{k}": float(value) for k, value in recall.items()},
        "mrr": float(metrics["mrr"]),
        **{f"ndcgAt{k}": float(value) for k, value in ndcg.items()},
    }


def _normalized_case(value: Mapping[str, object]) -> dict[str, Any]:
    query_id = str(value.get("queryId") or "").strip()
    if not _CASE_ID.fullmatch(query_id):
        raise ValueError("evaluation case has an invalid queryId")
    relevant = value.get("relevant")
    if not isinstance(relevant, Mapping) or not relevant:
        raise ValueError(f"evaluation case {query_id} has no qrels")
    answer = str(value.get("answer") or "").strip()
    if not answer:
        raise ValueError(f"evaluation case {query_id} has no reference answer")
    evaluation_case_id = str(value.get("evaluationCaseId") or query_id).strip()
    if not _CASE_ID.fullmatch(evaluation_case_id):
        raise ValueError(f"evaluation case {query_id} has an invalid evaluationCaseId")
    return {
        "queryId": query_id,
        "evaluationCaseId": evaluation_case_id,
        "slice": str(value.get("slice") or "unknown"),
        "relevant": {str(key): float(gain) for key, gain in relevant.items()},
        "answer": answer,
    }


def _searches_from_ledger(
    ledger: Mapping[str, object],
    *,
    allowed_case_ids: set[str],
) -> tuple[
    dict[str, list[list[str]]],
    list[dict[str, object]],
    set[str],
    dict[str, dict[str, str]],
]:
    raw_items = ledger.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("Agent gateway ledger must contain items")
    searches: dict[str, list[list[str]]] = defaultdict(list)
    failed: list[dict[str, object]] = []
    unknown: set[str] = set()
    citation_targets: dict[str, dict[str, str]] = defaultdict(dict)
    for raw in raw_items:
        if not isinstance(raw, Mapping) or raw.get("operation") != "search":
            continue
        args = raw.get("args")
        case_id = (
            str(args.get("evaluationCaseId") or "").strip()
            if isinstance(args, Mapping)
            else ""
        )
        if case_id not in allowed_case_ids:
            unknown.add(case_id or "(missing)")
        if raw.get("ok") is not True:
            failed.append(dict(raw))
            continue
        summary = raw.get("resultSummary")
        hits = summary.get("hits") if isinstance(summary, Mapping) else None
        retrieved: list[str] = []
        for hit in hits or []:
            if not isinstance(hit, Mapping):
                continue
            document_id = str(hit.get("externalDocumentId") or "")
            if not document_id:
                continue
            retrieved.append(document_id)
            citation = hit.get("citation")
            citation_ref = str(
                hit.get("citationRef")
                or (
                    citation.get("citationRef")
                    if isinstance(citation, Mapping)
                    else ""
                )
                or ""
            ).strip()
            if not citation_ref:
                continue
            existing = citation_targets[case_id].get(citation_ref)
            citation_targets[case_id][citation_ref] = (
                document_id
                if existing in {None, document_id}
                else ""
            )
        searches[case_id].append(list(dict.fromkeys(retrieved)))
    return searches, failed, unknown, {
        case_id: dict(targets)
        for case_id, targets in citation_targets.items()
    }


def _rrf_fuse(calls: list[list[str]], *, limit: int, rrf_k: int = 60) -> list[str]:
    scores: dict[str, float] = defaultdict(float)
    first_seen: dict[str, int] = {}
    sequence = 0
    for call in calls:
        for rank, document_id in enumerate(call, start=1):
            if document_id not in first_seen:
                first_seen[document_id] = sequence
                sequence += 1
            scores[document_id] += 1.0 / (rrf_k + rank)
    return [
        document_id
        for document_id, _score in sorted(
            scores.items(),
            key=lambda item: (-item[1], first_seen[item[0]], item[0]),
        )[:limit]
    ]


def _parse_answers(
    text: str,
    *,
    expected_case_ids: set[str],
) -> tuple[dict[str, dict[str, object]], list[str]]:
    payload = _first_json_object(text)
    errors: list[str] = []
    if not isinstance(payload, Mapping) or not isinstance(payload.get("cases"), list):
        return {}, ["assistant output is not a JSON object with cases[]"]
    result: dict[str, dict[str, object]] = {}
    for raw in payload["cases"]:
        if not isinstance(raw, Mapping):
            errors.append("assistant cases[] contains a non-object")
            continue
        case_id = str(raw.get("caseId") or "").strip()
        if case_id not in expected_case_ids:
            errors.append(f"unexpected caseId: {case_id or '(missing)'}")
            continue
        if case_id in result:
            errors.append(f"duplicate caseId: {case_id}")
            continue
        citations = raw.get("citations")
        if not isinstance(citations, list) or any(
            not isinstance(item, str) or not item.strip() for item in citations
        ):
            errors.append(f"invalid citations for {case_id}")
            citations = []
        abstained = raw.get("abstained")
        if not isinstance(abstained, bool):
            errors.append(f"invalid abstained flag for {case_id}")
            abstained = False
        answer = raw.get("answer")
        if not isinstance(answer, str):
            errors.append(f"invalid answer for {case_id}")
            answer = ""
        result[case_id] = {
            "answer": answer.strip(),
            "citations": list(dict.fromkeys(str(item).strip() for item in citations)),
            "abstained": abstained,
        }
    missing = sorted(expected_case_ids - result.keys())
    if missing:
        errors.append("missing caseIds: " + ", ".join(missing))
    return result, errors


def _first_json_object(text: str) -> object:
    decoder = json.JSONDecoder()
    for index, character in enumerate(str(text or "")):
        if character != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping) and "cases" in value:
            return value
    return None




def _character_scores(actual: str, expected: str) -> tuple[float, float, float]:
    actual_chars = Counter(_normalized_answer(actual))
    expected_chars = Counter(_normalized_answer(expected))
    if not actual_chars or not expected_chars:
        return 0.0, 0.0, 0.0
    overlap = sum((actual_chars & expected_chars).values())
    precision = overlap / sum(actual_chars.values())
    recall = overlap / sum(expected_chars.values())
    f1 = 2.0 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def _normalized_answer(value: str) -> str:
    return "".join(character.casefold() for character in value if character.isalnum())


def _selection_key(seed: str, query_id: str) -> str:
    return hashlib.sha256(f"{seed}\0{query_id}".encode("utf-8")).hexdigest()


def _sha256_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
