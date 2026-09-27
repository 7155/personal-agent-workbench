from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .rag_benchmark import compute_retrieval_metrics


RETRIEVAL_EXPERIMENT_SCHEMA_VERSION = "rag-ime.rag-retrieval-experiment.v1"

RetrievalCallback = Callable[[dict[str, object], dict[str, Any]], list[str]]
QueryPlanner = Callable[[dict[str, object]], Sequence[str]]


def evaluate_retrieval_configuration(
    *,
    cases: list[Mapping[str, object]],
    config: Mapping[str, object],
    retrieve: RetrievalCallback,
    k_values: tuple[int, ...] = (1, 3, 5, 10),
    query_planner: QueryPlanner | None = None,
    max_queries_per_case: int = 1,
    fusion_rrf_k: int = 60,
) -> dict[str, Any]:
    """Evaluate one frozen config without disclosing qrels to retrieval code.

    Retrieval callbacks receive only query ID, system and query text. An
    optional planner receives the same narrow payload. This makes the offline
    execution layer usable for either a deterministic multi-query diagnostic or
    a separately metered model planner while keeping labels outside both.
    """

    if not isinstance(cases, list) or not cases:
        raise ValueError("cases must be a non-empty array")
    normalized_config = _json_object(config, "config")
    if (
        isinstance(max_queries_per_case, bool)
        or not isinstance(max_queries_per_case, int)
        or not 1 <= max_queries_per_case <= 8
    ):
        raise ValueError("max_queries_per_case must be an integer from 1 to 8")
    if (
        isinstance(fusion_rrf_k, bool)
        or not isinstance(fusion_rrf_k, int)
        or not 1 <= fusion_rrf_k <= 1_000
    ):
        raise ValueError("fusion_rrf_k must be an integer from 1 to 1000")

    metric_cases: list[dict[str, object]] = []
    private_cases: list[dict[str, object]] = []
    slice_cases: dict[str, list[dict[str, object]]] = defaultdict(list)
    call_count = 0
    planner_call_count = 0
    latency_ms = 0.0
    observed_splits: set[str] = set()
    seen_query_ids: set[str] = set()
    for raw_case in cases:
        if not isinstance(raw_case, Mapping):
            raise ValueError("retrieval cases must be objects")
        if raw_case.get("retrievalEvaluable") is False:
            continue
        query_id = _identifier(raw_case.get("queryId"), "queryId")
        if query_id in seen_query_ids:
            raise ValueError(f"duplicate queryId: {query_id}")
        seen_query_ids.add(query_id)
        system = str(raw_case.get("system") or "").strip().lower()
        if system not in {"knowledge", "memory"}:
            raise ValueError("case system must be knowledge or memory")
        split = str(raw_case.get("split") or "").strip()
        if split not in {"train", "validation", "held_out"}:
            raise ValueError("case split must be train, validation, or held_out")
        observed_splits.add(split)
        query_text = _text(
            raw_case.get("query") or raw_case.get("question"),
            f"query {query_id}",
            maximum=100_000,
        )
        relevant = raw_case.get("relevant")
        if not isinstance(relevant, Mapping) or not relevant:
            raise ValueError(f"query {query_id} relevant must be a non-empty object")
        query_payload: dict[str, object] = {
            "queryId": query_id,
            "system": system,
            "text": query_text,
        }
        if query_planner is None:
            planned_queries = [query_text]
        else:
            planner_call_count += 1
            planned_queries = _planned_queries(
                query_planner(json.loads(_canonical_json(query_payload))),
                original=query_text,
                limit=max_queries_per_case,
            )

        rankings: list[list[str]] = []
        per_call: list[dict[str, object]] = []
        for planned_query in planned_queries[:max_queries_per_case]:
            retrieval_payload = {
                **query_payload,
                "text": planned_query,
            }
            started = time.perf_counter()
            ranking = retrieve(
                json.loads(_canonical_json(retrieval_payload)),
                json.loads(_canonical_json(normalized_config)),
            )
            elapsed_ms = (time.perf_counter() - started) * 1_000
            latency_ms += elapsed_ms
            call_count += 1
            normalized_ranking = _unique_ranking(ranking, query_id=query_id)
            rankings.append(normalized_ranking)
            per_call.append(
                {
                    "querySha256": hashlib.sha256(
                        planned_query.encode("utf-8")
                    ).hexdigest(),
                    "retrieved": normalized_ranking,
                    "latencyMs": round(elapsed_ms, 3),
                }
            )
        fused = (
            rankings[0]
            if len(rankings) == 1
            else reciprocal_rank_fusion(
                rankings,
                rrf_k=fusion_rrf_k,
                limit=max(max(k_values), max(len(item) for item in rankings)),
            )
        )
        metric_case = {
            "queryId": query_id,
            "relevant": dict(relevant),
            "retrieved": fused,
        }
        metric_cases.append(metric_case)
        slice_name = _slice_name(raw_case.get("slice") or raw_case.get("questionType"))
        slice_cases[slice_name].append(metric_case)
        private_cases.append(
            {
                "queryId": query_id,
                "split": split,
                "slice": slice_name,
                "plannedQueryCount": len(rankings),
                "calls": per_call,
                "fusedRetrieved": fused,
            }
        )
    if not metric_cases:
        raise ValueError("cases contain no retrieval-evaluable queries")

    aggregate = compute_retrieval_metrics(metric_cases, k_values=k_values)
    public_metrics = {
        "queryCount": aggregate["queryCount"],
        "kValues": aggregate["kValues"],
        "metrics": aggregate["metrics"],
    }
    per_slice = {
        name: {
            "queryCount": metrics["queryCount"],
            "metrics": metrics["metrics"],
        }
        for name, items in sorted(slice_cases.items())
        for metrics in [compute_retrieval_metrics(items, k_values=k_values)]
    }
    result: dict[str, Any] = {
        "schemaVersion": RETRIEVAL_EXPERIMENT_SCHEMA_VERSION,
        "config": normalized_config,
        "configSha256": _sha256_json(normalized_config),
        "splits": sorted(observed_splits),
        "queryPlanner": "none" if query_planner is None else "external",
        "maxQueriesPerCase": max_queries_per_case,
        "fusionRrfK": fusion_rrf_k,
        "metrics": public_metrics,
        "perSlice": per_slice,
        "costs": {
            "retrievalCalls": call_count,
            "plannerCalls": planner_call_count,
            "retrievalLatencyMs": round(latency_ms, 3),
            "meanRetrievalLatencyMs": round(latency_ms / call_count, 3),
        },
        "privateCases": private_cases,
    }
    result["receiptSha256"] = _sha256_json(
        {key: value for key, value in result.items() if key != "privateCases"}
    )
    return result


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[str]],
    *,
    rrf_k: int = 60,
    limit: int = 10,
) -> list[str]:
    if not rankings:
        raise ValueError("rankings must not be empty")
    if isinstance(rrf_k, bool) or not isinstance(rrf_k, int) or rrf_k < 1:
        raise ValueError("rrf_k must be a positive integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    scores: dict[str, float] = defaultdict(float)
    best_rank: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    sequence = 0
    for ranking in rankings:
        normalized = _unique_ranking(list(ranking), query_id="fusion")
        for rank, item_id in enumerate(normalized, start=1):
            scores[item_id] += 1.0 / (rrf_k + rank)
            best_rank[item_id] = min(best_rank.get(item_id, rank), rank)
            if item_id not in first_seen:
                first_seen[item_id] = sequence
                sequence += 1
    return sorted(
        scores,
        key=lambda item_id: (
            -scores[item_id],
            best_rank[item_id],
            first_seen[item_id],
            item_id,
        ),
    )[:limit]


def deterministic_query_plan(query: Mapping[str, object]) -> list[str]:
    """Bounded label-free fallback used only as an offline diagnostic.

    The final Agentic lane may replace this with Luna, but its planner receives
    the same narrow payload and is metered separately.
    """

    text = _text(query.get("text"), "query.text", maximum=100_000)
    separators = "。！？!?；;\n"
    parts = [text]
    current = ""
    for character in text:
        if character in separators:
            compact = " ".join(current.split()).strip(" ,，。！？!?；;")
            if len(compact) >= 8:
                parts.append(compact)
            current = ""
        else:
            current += character
    compact = " ".join(current.split()).strip(" ,，。！？!?；;")
    if len(compact) >= 8:
        parts.append(compact)
    unique = list(dict.fromkeys(parts))
    longest = sorted(unique[1:], key=lambda item: (-len(item), item))[:2]
    return [text, *longest]


def _planned_queries(
    value: Sequence[str],
    *,
    original: str,
    limit: int,
) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError("query planner must return a sequence of strings")
    result = [original]
    for raw_item in value:
        item = _text(raw_item, "planned query", maximum=100_000)
        if item not in result:
            result.append(item)
        if len(result) >= limit:
            break
    return result


def _unique_ranking(value: object, *, query_id: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"retriever for {query_id} must return an array")
    result: list[str] = []
    seen: set[str] = set()
    for raw_item in value:
        item_id = _identifier(raw_item, f"retrieved item for {query_id}")
        if item_id in seen:
            continue
        seen.add(item_id)
        result.append(item_id)
    return result


def _slice_name(value: object) -> str:
    text = str(value or "unspecified").strip().lower().replace(" ", "_")
    if not text or len(text) > 100:
        return "unspecified"
    return text


def _identifier(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 300 or any(ord(character) < 32 for character in text):
        raise ValueError(f"{field} must be a stable identifier")
    return text


def _text(value: object, field: str, *, maximum: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum:
        raise ValueError(f"{field} must be non-empty and at most {maximum} characters")
    return text


def _json_object(value: Mapping[str, object], field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    try:
        normalized = json.loads(_canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must contain JSON values") from exc
    if not isinstance(normalized, dict):
        raise ValueError(f"{field} must be an object")
    return normalized


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
