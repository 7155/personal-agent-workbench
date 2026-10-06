from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable

from .context_group import ContextGroup, context_group_compatibility
from .embeddings import EmbeddingProvider, cosine_similarity, embed_query, normalize_vector
from .hybrid_rag_models import HybridRagCandidate, HybridRagHit, HybridRagQuery, MemoryHit
from .hybrid_rag_ranker import rank_hybrid_hits_to_memory_hits
from .knowledge_scope import KnowledgeCallerContext, scope_sql_predicate
from .memory_ingest import normalize_text
from .memory_ownership import resolve_visible_memory_owners, sql_memory_owner_predicate
from .memory_projectors import ImeMemoryProjector
from .memory_projection_consistency import authoritative_retrieval_doc
from .query_expansion import build_query_expansion
from .retrieval_vector_index import load_retrieval_doc_vectors
from .text_utils import compact_whitespace, token_terms
from .timeline_intent import (
    TimelineIntent,
    classify_timeline_intent,
    timeline_date_bounds,
)


HYBRID_RAG_RETRIEVAL_SCHEMA_VERSION = "rag-ime.hybrid-rag-retrieval.v1"
_EXPLICIT_HISTORY_RE = re.compile(
    r"(?:之前|以前|最初|初版|旧版|旧项目|归档|历史|当时|过去|去年|上周|上月|"
    r"\d{4}[年./-]\d{1,2}(?:[月./-]\d{1,2}日?)?)",
    re.IGNORECASE,
)
def retrieve_hybrid_rag_candidates(
    conn: sqlite3.Connection,
    query: HybridRagQuery,
    embedding_provider: EmbeddingProvider | None = None,
) -> dict[str, object]:
    started = time.perf_counter()
    visible_owners = resolve_visible_memory_owners(query.visible_owners, project=query.project)
    expansion = build_query_expansion(
        conn,
        query_text=query.query_text,
        raw_input=query.raw_input,
        preedit=query.preedit,
        rime_candidates=query.rime_candidates,
        committed_tail=query.committed_tail,
        project=query.project,
        app=query.app,
        visible_owners=visible_owners,
    )
    timeline_intent = _timeline_intent_for_query(
        query,
        enabled=_managed_timeline_recall_enabled(conn),
    )
    timeline_requested = timeline_intent.requested
    retrieval_now_ms = int(time.time() * 1_000)
    docs = _active_docs(
        conn,
        query=query,
        timeline_intent=timeline_intent,
        now_ms=retrieval_now_ms,
    )
    blocked = _blocked_sets(conn)
    vector_available = bool(embedding_provider and embedding_provider.fingerprint != "none")
    enabled_lanes = _resolved_lane_enabled(query.enabled_lanes, vector_available=vector_available)
    # Daily timelines are a derived, short-lived view. They must not compete
    # with stable Atoms and topic Books for an ordinary semantic question.
    # A temporal query opts into both the documents and their dedicated lane.
    enabled_lanes["time"] = enabled_lanes["time"] and timeline_requested
    lane_weights = _resolved_lane_weights(query.lane_weights)
    # Keep the recall pool independent of a small presentation top_k. With a
    # top_k-derived pool, asking for 5 results could entirely omit the exact
    # fact that appears when asking for 12, making ranking non-monotonic.
    lane_limit = max(64, query.top_k * 4)
    vectors = (
        load_retrieval_doc_vectors(conn, embedding_provider.fingerprint, (str(doc["doc_id"]) for doc in docs))
        if vector_available and embedding_provider is not None else {}
    )
    query_vector, vector_fusion = (
        _semantic_query_vector(
            embedding_provider,
            expansion.primary_query,
            context_text=query.vector_context_text,
            context_weight=query.vector_context_weight,
        )
        if vectors and embedding_provider
        else ([], {"applied": False, "queryWeight": 1.0, "contextWeight": 0.0})
    )
    # SQLite connections are thread-affine by default. Complete SQL-backed
    # lanes here; CPU-only scoring lanes then run in parallel.
    lexical_lane_meta: dict[str, str] = {}
    bm25_raw_hits = []
    if enabled_lanes["bm25_raw"]:
        bm25_raw_hits, lexical_lane_meta["bm25_raw"] = _rank_fts5_docs(
            conn,
            docs=docs,
            lane="bm25_raw",
            terms=(expansion.primary_query, *expansion.lexical_terms),
            fields=("raw_text",),
            blocked=blocked,
            project=query.project,
            app=query.app,
            visible_owners=visible_owners,
            knowledge_caller=query.knowledge_caller,
            limit=lane_limit,
        )
    bm25_tags_hits = []
    if enabled_lanes["bm25_tags"]:
        bm25_tags_hits, lexical_lane_meta["bm25_tags"] = _rank_fts5_docs(
            conn,
            docs=docs,
            lane="bm25_tags",
            terms=(*expansion.matched_aliases, *expansion.activated_tags, *expansion.expansion_terms),
            fields=("tags_text", "aliases_text", "surface_hints_text", "query_expansions_text"),
            blocked=blocked,
            project=query.project,
            app=query.app,
            visible_owners=visible_owners,
            knowledge_caller=query.knowledge_caller,
            limit=lane_limit,
        )
    tagmemo_hits = []
    if enabled_lanes["tagmemo"]:
        tagmemo_hits, lexical_lane_meta["tagmemo"] = _rank_fts5_docs(
            conn,
            docs=docs,
            lane="tagmemo",
            terms=expansion.activated_tags,
            fields=("tags_text", "aliases_text", "query_expansions_text"),
            blocked=blocked,
            project=query.project,
            app=query.app,
            visible_owners=visible_owners,
            knowledge_caller=query.knowledge_caller,
            limit=lane_limit,
        )
    feedback_hits = (
        _feedback_hits(
            conn,
            docs=docs,
            query=query,
            terms=(*expansion.lexical_terms, *expansion.matched_aliases, *expansion.activated_tags),
            blocked=blocked,
            limit=lane_limit,
        )
        if enabled_lanes["feedback"] else []
    )
    tasks = {
        "vector_raw": lambda: _rank_vector_docs(
            docs, vectors=vectors, query_vector=query_vector, lane="vector_raw", vector_index=0,
            blocked=blocked, limit=lane_limit,
        ) if enabled_lanes["vector_raw"] else [],
        "vector_tag_boost": lambda: _rank_vector_docs(
            docs, vectors=vectors, query_vector=query_vector, lane="vector_tag_boost", vector_index=1,
            blocked=blocked, limit=lane_limit, include_group=True,
        ) if enabled_lanes["vector_tag_boost"] else [],
        "time": lambda: (
            _rank_time_docs(
                docs,
                expansion_terms=(*expansion.expansion_terms, *expansion.activated_tags),
                blocked=blocked,
                limit=lane_limit,
                now_ms=retrieval_now_ms,
            )
            if enabled_lanes["time"]
            else []
        ),
        "feedback": lambda: feedback_hits,
    }
    with ThreadPoolExecutor(max_workers=len(tasks), thread_name_prefix="rag-lane") as executor:
        futures = {name: executor.submit(task) for name, task in tasks.items()}
        lane_hits = {name: future.result() for name, future in futures.items()}
    lane_hits.update({
        "bm25_raw": bm25_raw_hits,
        "bm25_tags": bm25_tags_hits,
        "tagmemo": tagmemo_hits,
    })
    hits = [hit for lane in lane_hits.values() for hit in lane]
    memory_hits = rank_hybrid_hits_to_memory_hits(
        hits,
        query_text=query.query_text,
        lane_weights=lane_weights,
        decay_settings=_memory_decay_settings(conn),
    )
    if _recent_timeline_requested(query, intent=timeline_intent):
        memory_hits.sort(key=_recent_timeline_sort_key, reverse=True)
    historical_book_ids = _historical_books_for_explicit_history(
        hits=hits,
        query_text=" ".join(
            item
            for item in (query.query_text, query.raw_input, query.committed_tail)
            if compact_whitespace(item)
        ),
    )
    candidates = ImeMemoryProjector().project(
        memory_hits,
        query_text=query.query_text,
        committed_tail=query.committed_tail,
        top_k=query.top_k,
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return {
        "schemaVersion": HYBRID_RAG_RETRIEVAL_SCHEMA_VERSION,
        "query": {
            "primary": expansion.primary_query,
            "lexicalTerms": list(expansion.lexical_terms),
            "matchedAliases": list(expansion.matched_aliases),
            "activatedTags": list(expansion.activated_tags),
            "negativeTags": list(expansion.negative_tags),
            "expansionTerms": list(expansion.expansion_terms),
            "visibleOwners": [
                {"ownerKind": kind, "ownerId": identity}
                for kind, identity in visible_owners
            ],
            "timelineRequested": timeline_requested,
            "recentTimelineRequested": _recent_timeline_requested(
                query,
                intent=timeline_intent,
            ),
            "timelineIntent": timeline_intent.as_dict(),
            "vectorFusion": vector_fusion,
        },
        "lanes": {
            name: {
                "enabled": enabled_lanes[name],
                "available": vector_available if name in {"vector_raw", "vector_tag_boost"} else True,
                "implementation": _lane_implementation(
                    name,
                    lexical_lane_meta=lexical_lane_meta,
                    vector_available=vector_available,
                ),
                "lexicalFallback": _lane_implementation(
                    name,
                    lexical_lane_meta=lexical_lane_meta,
                    vector_available=vector_available,
                ) == "lexical_substring_fallback",
                "fts5Bm25": _lane_implementation(
                    name,
                    lexical_lane_meta=lexical_lane_meta,
                    vector_available=vector_available,
                ) == "sqlite_fts5_bm25",
                "skippedReason": (
                    ("embedding_provider_not_wired" if not vector_available else "vector_index_empty")
                    if name in {"vector_raw", "vector_tag_boost"} and not values
                    else (
                        "not_requested_by_query"
                        if name == "time" and not timeline_requested
                        else ("disabled_by_effective_runtime_config" if not enabled_lanes[name] else "")
                    )
                ),
                "weight": lane_weights[name],
                "count": len(values),
                "docIds": [hit.doc_id for hit in values[:5]],
            }
            for name, values in lane_hits.items()
        },
        "elapsedMs": elapsed_ms,
        "parallelExecution": True,
        "vectorIndexDocuments": len(vectors),
        "overBudget": elapsed_ms > max(1, int(query.latency_budget_ms)),
        "hits": [hit.__dict__ for hit in hits],
        "memoryHits": [hit.__dict__ for hit in memory_hits],
        "candidates": [candidate.__dict__ for candidate in candidates],
        # Compatibility field retained while lifecycle mutation moves to an
        # explicit command. A query is now strictly read-only.
        "reactivatedBookIds": [],
        "historicalBookIds": historical_book_ids,
    }


def _semantic_query_vector(
    embedding_provider: EmbeddingProvider,
    query_text: str,
    *,
    context_text: str = "",
    context_weight: float = 0.0,
) -> tuple[list[float], dict[str, object]]:
    """Return a normalized query/context blend for semantic lanes only."""

    primary = embed_query(embedding_provider, query_text)
    bounded_context_weight = (
        min(0.5, max(0.0, float(context_weight)))
        if math.isfinite(float(context_weight))
        else 0.0
    )
    normalized_context = compact_whitespace(context_text)
    if not primary or not normalized_context or bounded_context_weight <= 0.0:
        return list(primary), {
            "applied": False,
            "queryWeight": 1.0,
            "contextWeight": 0.0,
        }
    context = embed_query(embedding_provider, normalized_context)
    if not context or len(context) != len(primary):
        return list(primary), {
            "applied": False,
            "queryWeight": 1.0,
            "contextWeight": 0.0,
        }
    query_weight = 1.0 - bounded_context_weight
    blended = normalize_vector(
        [
            query_weight * float(query_value)
            + bounded_context_weight * float(context_value)
            for query_value, context_value in zip(primary, context, strict=True)
        ]
    )
    if not blended:
        return list(primary), {
            "applied": False,
            "queryWeight": 1.0,
            "contextWeight": 0.0,
        }
    return blended, {
        "applied": True,
        "queryWeight": query_weight,
        "contextWeight": bounded_context_weight,
    }


_DEFAULT_LANE_WEIGHTS = {
    "bm25_raw": 1.00,
    "bm25_tags": 1.15,
    "vector_raw": 1.05,
    "vector_tag_boost": 1.05,
    "tagmemo": 1.10,
    "time": 0.90,
    "feedback": 1.20,
}


def _resolved_lane_enabled(values: tuple[tuple[str, bool], ...], *, vector_available: bool = False) -> dict[str, bool]:
    configured = {str(key): bool(value) for key, value in values}
    resolved = {lane: configured.get(lane, True) for lane in _DEFAULT_LANE_WEIGHTS}
    resolved["vector_raw"] = vector_available and resolved["vector_raw"]
    resolved["vector_tag_boost"] = vector_available and resolved["vector_tag_boost"]
    return resolved


def _resolved_lane_weights(values: tuple[tuple[str, float], ...]) -> dict[str, float]:
    configured = {str(key): max(0.0, float(value)) for key, value in values}
    return {lane: configured.get(lane, default) for lane, default in _DEFAULT_LANE_WEIGHTS.items()}


def _historical_books_for_explicit_history(
    *,
    hits: list[HybridRagHit],
    query_text: str,
) -> list[str]:
    """Report relevant archived Books without changing their lifecycle."""

    if _EXPLICIT_HISTORY_RE.search(compact_whitespace(query_text)) is None:
        return []
    eligible_book_ids: list[str] = []
    for hit in hits:
        if hit.doc_type != "book" or not bool(hit.metadata.get("archived")):
            continue
        # Time-only recall can contain unrelated old books. A lexical, vector,
        # tag, or feedback lane is the evidence that this archived topic is
        # actually related to the user's explicit historical request.
        if hit.source_lane == "time" or hit.source_id in eligible_book_ids:
            continue
        eligible_book_ids.append(hit.source_id)
        if len(eligible_book_ids) >= 2:
            break
    return eligible_book_ids


def _lane_implementation(
    lane: str,
    *,
    lexical_lane_meta: dict[str, str],
    vector_available: bool,
) -> str:
    if lane in {"bm25_raw", "bm25_tags", "tagmemo"}:
        return lexical_lane_meta.get(lane, "disabled")
    if lane in {"vector_raw", "vector_tag_boost"}:
        return "precomputed_cosine" if vector_available else "not_wired"
    if lane == "time":
        return "recency_term_scoring"
    if lane == "feedback":
        return "accepted_feedback_relevance"
    return "native"


def retrieve_hybrid_rag_candidate_objects(
    conn: sqlite3.Connection,
    query: HybridRagQuery,
    embedding_provider: EmbeddingProvider | None = None,
) -> list[HybridRagCandidate]:
    payload = retrieve_hybrid_rag_candidates(conn, query, embedding_provider)
    candidates: list[HybridRagCandidate] = []
    for item in payload.get("candidates", []):
        if isinstance(item, HybridRagCandidate):
            candidates.append(item)
        elif isinstance(item, dict):
            candidates.append(
                HybridRagCandidate(
                    candidate_id=str(item["candidate_id"]),
                    text=str(item["text"]),
                    insert_text=str(item["insert_text"]),
                    source_type=str(item["source_type"]),
                    source_lane=str(item["source_lane"]),
                    score=float(item["score"]),
                    confidence=float(item["confidence"]),
                    tags=tuple(item.get("tags") or ()),
                    memory_ids=tuple(item.get("memory_ids") or ()),
                    atom_ids=tuple(item.get("atom_ids") or ()),
                    book_ids=tuple(item.get("book_ids") or ()),
                    evidence_event_ids=tuple(int(value) for value in item.get("evidence_event_ids") or ()),
                    evidence_preview=str(item.get("evidence_preview") or ""),
                    debug_features=dict(item.get("debug_features") or {}),
                    metadata=dict(item.get("metadata") or {}),
                )
            )
    return candidates


def retrieve_hybrid_rag_memory_hit_objects(
    conn: sqlite3.Connection,
    query: HybridRagQuery,
    embedding_provider: EmbeddingProvider | None = None,
) -> list[MemoryHit]:
    payload = retrieve_hybrid_rag_candidates(conn, query, embedding_provider)
    hits: list[MemoryHit] = []
    for item in payload.get("memoryHits", []):
        if isinstance(item, MemoryHit):
            hits.append(item)
            continue
        if not isinstance(item, dict):
            continue
        hits.append(
            MemoryHit(
                hit_id=str(item["hit_id"]),
                doc_id=str(item["doc_id"]),
                doc_type=str(item["doc_type"]),
                source_id=str(item["source_id"]),
                text=str(item["text"]),
                surface_hints=tuple(item.get("surface_hints") or ()),
                source_type=str(item["source_type"]),
                source_lane=str(item["source_lane"]),
                score=float(item["score"]),
                confidence=float(item["confidence"]),
                tags=tuple(item.get("tags") or ()),
                memory_ids=tuple(item.get("memory_ids") or ()),
                atom_ids=tuple(item.get("atom_ids") or ()),
                book_ids=tuple(item.get("book_ids") or ()),
                evidence_event_ids=tuple(
                    int(value) for value in item.get("evidence_event_ids") or ()
                ),
                evidence_preview=str(item.get("evidence_preview") or ""),
                debug_features={
                    str(key): float(value)
                    for key, value in dict(item.get("debug_features") or {}).items()
                },
                metadata=dict(item.get("metadata") or {}),
            )
        )
    return hits[: max(1, int(query.top_k))]


def _active_docs(
    conn: sqlite3.Connection,
    *,
    query: HybridRagQuery,
    timeline_intent: TimelineIntent | None = None,
    now_ms: int | None = None,
    source_ids: tuple[str, ...] | None = None,
) -> list[dict[str, object]]:
    # The primary Session boundary revalidates a bounded existing pack with
    # the same source/scope owner, without searching or loading the corpus.
    if source_ids is not None and not source_ids:
        return []
    source_clause = (
        " AND source_id IN (" + ",".join("?" for _ in source_ids) + ")"
        if source_ids is not None else ""
    )
    visible_owners = resolve_visible_memory_owners(query.visible_owners, project=query.project)
    owner_clause, owner_params = sql_memory_owner_predicate(
        visible_owners,
        table_alias="memory_retrieval_docs",
    )
    scope_clause, scope_params = scope_sql_predicate(
        query.knowledge_caller,
        table_alias="memory_retrieval_docs",
    )
    rows = conn.execute(
        f"""
        SELECT doc_id, doc_type, source_id, raw_text, tags_text, aliases_text, surface_hints_text,
               query_expansions_text, time_key, project, app, owner_kind, owner_id,
               knowledge_domain, scope_kind, scope_id, visibility,
               authorization_revision, binding_id, scope_mode,
               source_revision, projection_version, updated_at_ms, metadata_json
        FROM memory_retrieval_docs
        WHERE status = 'active'
          AND doc_type != 'item'
          AND (? = '' OR project = ? OR project = '')
          AND (? = '' OR app = ? OR app = '')
          AND {owner_clause}
          AND {scope_clause}
          {source_clause}
        """,
        (query.project, query.project, query.app, query.app, *owner_params, *scope_params,
         *(source_ids or ())),
    ).fetchall()
    current_group = ContextGroup(
        context_group_id=compact_whitespace(query.context_group_id),
        context_group_level=query.context_group_level if query.context_group_level in {"document", "project", "app", "global"} else "app",
        confidence=1.0 if query.context_group_id else 0.0,
        parent_group_ids=tuple(query.context_group_parent_ids),
        app_bundle_id=compact_whitespace(query.app),
        project=compact_whitespace(query.project),
    )
    parsed_rows = [(row, _metadata(row["metadata_json"])) for row in rows]
    governed_visible_event_ids = _governed_visible_event_ids(
        conn,
        {
            event_id
            for _, metadata in parsed_rows
            for event_id in _metadata_source_event_ids(metadata)
        },
    )
    docs: list[dict[str, object]] = []
    resolved_timeline_intent = (
        _timeline_intent_for_query(query)
        if timeline_intent is None
        else timeline_intent
    )
    timeline_requested = resolved_timeline_intent.requested
    resolved_now = datetime.fromtimestamp(
        (
            int(now_ms)
            if now_ms is not None
            else int(time.time() * 1_000)
        )
        / 1_000,
        tz=timezone.utc,
    ).astimezone()
    timeline_bounds = timeline_date_bounds(
        resolved_timeline_intent,
        now=resolved_now,
    )
    for row, metadata in parsed_rows:
        if not authoritative_retrieval_doc(
            conn,
            {
                "doc_type": row["doc_type"],
                "source_id": row["source_id"],
                "source_revision": row["source_revision"],
                "projection_version": row["projection_version"],
            },
        ):
            continue
        source_event_ids = _metadata_source_event_ids(metadata)
        if source_event_ids and not set(source_event_ids).issubset(
            governed_visible_event_ids
        ):
            continue
        if _is_daily_timeline_doc(metadata) and not timeline_requested:
            continue
        if (
            _is_daily_timeline_doc(metadata)
            and timeline_bounds is not None
            and not _timeline_doc_in_bounds(
                metadata,
                time_key=str(row["time_key"] or ""),
                bounds=timeline_bounds,
            )
        ):
            continue
        # A timeline is cross-app by design. Once an explicit temporal query
        # admits it, score it at project scope instead of requiring the exact
        # document/app context group used by ordinary short-term snippets.
        short_term = bool(
            metadata.get("shortTerm") or metadata.get("short_term")
        ) and not _is_daily_timeline_doc(metadata)
        compatibility = context_group_compatibility(
            current_group,
            candidate_group_id=str(metadata.get("contextGroupId") or metadata.get("context_group_id") or ""),
            candidate_project=str(row["project"] or ""),
            candidate_app=str(row["app"] or ""),
            short_term=short_term,
        )
        if compatibility <= 0.0:
            continue
        metadata["groupCompatibility"] = compatibility
        metadata.update(
            {
                "ownerKind": str(row["owner_kind"] or ""),
                "ownerId": str(row["owner_id"] or ""),
                "knowledgeDomain": str(row["knowledge_domain"] or ""),
                "scopeKind": str(row["scope_kind"] or ""),
                "scopeId": str(row["scope_id"] or ""),
                "visibility": str(row["visibility"] or ""),
                "authorizationRevision": str(row["authorization_revision"] or ""),
                "bindingId": str(row["binding_id"] or ""),
                "scopeMode": str(row["scope_mode"] or "legacy"),
            }
        )
        docs.append({
            "doc_id": str(row["doc_id"]),
            "doc_type": str(row["doc_type"]),
            "source_id": str(row["source_id"]),
            "raw_text": str(row["raw_text"] or ""),
            "tags_text": str(row["tags_text"] or ""),
            "aliases_text": str(row["aliases_text"] or ""),
            "surface_hints_text": str(row["surface_hints_text"] or ""),
            "query_expansions_text": str(row["query_expansions_text"] or ""),
            "time_key": str(row["time_key"] or ""),
            "project": str(row["project"] or ""),
            "app": str(row["app"] or ""),
            "owner_kind": str(row["owner_kind"] or ""),
            "owner_id": str(row["owner_id"] or ""),
            "source_revision": int(row["source_revision"] or 1),
            "projection_version": int(row["projection_version"] or 1),
            "updated_at_ms": int(row["updated_at_ms"] or 0),
            "metadata": metadata,
        })
    docs.sort(key=lambda item: float(dict(item.get("metadata") or {}).get("groupCompatibility") or 0.0), reverse=True)
    return docs


def _metadata_source_event_ids(metadata: dict[str, object]) -> list[int]:
    values = metadata.get("sourceEventIds")
    raw_values = list(values) if isinstance(values, (list, tuple)) else []
    raw_values.append(metadata.get("sourceEventId"))
    result: list[int] = []
    for value in raw_values:
        try:
            event_id = int(value)
        except (TypeError, ValueError):
            continue
        if event_id > 0 and event_id not in result:
            result.append(event_id)
    return result


def _governed_visible_event_ids(
    conn: sqlite3.Connection,
    event_ids: set[int],
) -> set[int]:
    if not event_ids:
        return set()
    visible: set[int] = set()
    values = sorted(event_ids)
    for offset in range(0, len(values), 500):
        chunk = values[offset : offset + 500]
        placeholders = ",".join("?" for _ in chunk)
        visible.update(
            int(row[0])
            for row in conn.execute(
                f"""SELECT event.id
                    FROM input_events AS event
                    WHERE event.id IN ({placeholders})
                      AND NOT EXISTS (
                          SELECT 1 FROM memory_tombstones AS tombstone
                          WHERE tombstone.active = 1
                            AND (
                                (tombstone.target_type = 'source_event_id'
                                 AND tombstone.target_value = CAST(event.id AS TEXT))
                                OR
                                (tombstone.target_type = 'memory_id'
                                 AND tombstone.target_value = ('event:' || event.id))
                            )
                      )""",
                tuple(chunk),
            ).fetchall()
        )
        governed_rows = conn.execute(
            f"""SELECT input_event_id,
                       SUM(CASE WHEN status = 'active'
                                     AND disposition NOT IN ('not_for_memory', 'expired')
                                THEN 1 ELSE 0 END)
                FROM agent_memory_sources
                WHERE input_event_id IN ({placeholders})
                GROUP BY input_event_id""",
            tuple(chunk),
        ).fetchall()
        visible.difference_update(
            int(row[0]) for row in governed_rows if int(row[1] or 0) == 0
        )
    return visible


def _timeline_requested(query: HybridRagQuery) -> bool:
    return _timeline_intent_for_query(query).requested


def _recent_timeline_requested(
    query: HybridRagQuery,
    *,
    intent: TimelineIntent | None = None,
) -> bool:
    resolved = intent or _timeline_intent_for_query(query)
    return resolved.range in {
        "today",
        "yesterday",
        "day_before_yesterday",
        "recent_days",
        "current_week",
    }


def _timeline_intent_for_query(
    query: HybridRagQuery,
    *,
    enabled: bool = True,
) -> TimelineIntent:
    return classify_timeline_intent(
        query.query_text,
        raw_input=query.raw_input,
        committed_tail=query.committed_tail,
        enabled=enabled,
    )


def _managed_timeline_recall_enabled(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT value_json FROM management_settings WHERE key = 'memory'"
        ).fetchone()
    except sqlite3.OperationalError:
        return True
    if row is None:
        return True
    try:
        payload = json.loads(str(row[0] or "{}"))
    except (TypeError, json.JSONDecodeError):
        return True
    if not isinstance(payload, dict):
        return True
    recall = payload.get("recall")
    return (
        bool(recall.get("timelineEnabled", True))
        if isinstance(recall, dict)
        else True
    )


def _recent_timeline_sort_key(hit: MemoryHit) -> tuple[int, str, float]:
    metadata = hit.metadata
    daily = _is_daily_timeline_doc(metadata)
    date_key = compact_whitespace(
        str(metadata.get("timelineDate") or metadata.get("bookKey") or "")
    )
    return (1 if daily else 0, date_key if daily else "", hit.score)


def _is_daily_timeline_doc(metadata: dict[str, object]) -> bool:
    return (
        compact_whitespace(str(metadata.get("bookType") or "")).lower() == "daily"
        or compact_whitespace(str(metadata.get("derivedArtifactType") or "")).lower()
        == "daily_activity_timeline"
    )


def _timeline_doc_in_bounds(
    metadata: dict[str, object],
    *,
    time_key: str,
    bounds: tuple[str, str],
) -> bool:
    timeline_date = compact_whitespace(
        str(metadata.get("timelineDate") or metadata.get("bookKey") or "")
    )
    match = re.search(
        r"(?<!\d)(20\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01]))(?!\d)",
        f"{timeline_date} {time_key}",
    )
    if match is None:
        return False
    return bounds[0] <= match.group(1) <= bounds[1]


def _rank_docs(
    docs: list[dict[str, object]],
    *,
    lane: str,
    terms: Iterable[str],
    fields: tuple[str, ...],
    blocked: dict[str, set[str]],
    limit: int,
) -> list[HybridRagHit]:
    weighted: list[tuple[float, dict[str, object]]] = []
    normalized_terms = [normalize_text(term) for term in terms if compact_whitespace(str(term))]
    normalized_terms.extend(token_terms(" ".join(str(term) for term in terms), max_terms=24))
    normalized_terms = _unique(normalized_terms)
    if not normalized_terms:
        return []
    for doc in docs:
        if _doc_blocked(doc, blocked):
            continue
        haystack = normalize_text(" ".join(str(doc.get(field) or "") for field in fields))
        if not haystack:
            continue
        score = sum(1.0 + min(len(term), 8) * 0.02 for term in normalized_terms if term and term in haystack)
        if score <= 0:
            continue
        weighted.append((score, doc))
    weighted.sort(key=lambda item: item[0], reverse=True)
    return [_hit_from_doc(doc, lane=lane, rank=index, raw_score=score) for index, (score, doc) in enumerate(weighted[:limit], start=1)]


def _rank_fts5_docs(
    conn: sqlite3.Connection,
    *,
    docs: list[dict[str, object]],
    lane: str,
    terms: Iterable[str],
    fields: tuple[str, ...],
    blocked: dict[str, set[str]],
    project: str,
    app: str,
    visible_owners: tuple[tuple[str, str], ...],
    knowledge_caller: KnowledgeCallerContext | None = None,
    limit: int,
) -> tuple[list[HybridRagHit], str]:
    """Run a column-scoped FTS5 BM25 query, with an explicit safe fallback.

    The retrieval-doc builder maintains ``memory_retrieval_docs_fts`` with the
    same rowids as the normalized document table.  Keeping the fallback local
    makes a partially migrated database usable, but diagnostics must report it
    as such rather than calling it BM25.
    """

    match_query = _fts5_match_query(terms, fields=fields)
    if not match_query:
        return [], "sqlite_fts5_bm25"
    docs_by_id = {str(doc["doc_id"]): doc for doc in docs}
    if not docs_by_id:
        return [], "sqlite_fts5_bm25"
    owner_clause, owner_params = sql_memory_owner_predicate(
        visible_owners,
        table_alias="d",
    )
    scope_clause, scope_params = scope_sql_predicate(knowledge_caller, table_alias="d")
    try:
        rows = conn.execute(
            f"""
            SELECT d.doc_id, bm25(memory_retrieval_docs_fts) AS bm25_score
            FROM memory_retrieval_docs_fts
            JOIN memory_retrieval_docs AS d
              ON d.rowid = memory_retrieval_docs_fts.rowid
            WHERE memory_retrieval_docs_fts MATCH ?
              AND d.status = 'active'
              AND (? = '' OR d.project = ? OR d.project = '')
              AND (? = '' OR d.app = ? OR d.app = '')
              AND {owner_clause}
              AND {scope_clause}
            ORDER BY bm25(memory_retrieval_docs_fts) ASC, d.updated_at_ms DESC
            LIMIT ?
            """,
            (
                match_query,
                project,
                project,
                app,
                app,
                *owner_params,
                *scope_params,
                max(1, limit * 8),
            ),
        ).fetchall()
    except sqlite3.OperationalError:
        return (
            _rank_docs(docs, lane=lane, terms=terms, fields=fields, blocked=blocked, limit=limit),
            "lexical_substring_fallback",
        )
    hits: list[HybridRagHit] = []
    for row in rows:
        doc = docs_by_id.get(str(row["doc_id"]))
        if doc is None or _doc_blocked(doc, blocked):
            continue
        hits.append(
            _hit_from_doc(
                doc,
                lane=lane,
                rank=len(hits) + 1,
                raw_score=float(row["bm25_score"]),
            )
        )
        if len(hits) >= limit:
            break
    if not hits:
        fallback_hits = _rank_docs(
            docs,
            lane=lane,
            terms=terms,
            fields=fields,
            blocked=blocked,
            limit=limit,
        )
        if fallback_hits:
            return fallback_hits, "lexical_substring_fallback"
    return hits, "sqlite_fts5_bm25"


def _fts5_match_query(terms: Iterable[str], *, fields: tuple[str, ...]) -> str:
    normalized: list[str] = []
    for value in terms:
        normalized.extend(token_terms(str(value), max_terms=24))
    unique_terms = _unique(normalized)
    if not unique_terms or not fields:
        return ""
    escaped = [f'"{term.replace(chr(34), chr(34) + chr(34))}"' for term in unique_terms]
    return f"{{{' '.join(fields)}}} : ({' OR '.join(escaped)})"


def _rank_vector_docs(
    docs: list[dict[str, object]],
    *,
    vectors: dict[str, tuple[list[float], list[float], list[float]]],
    query_vector: list[float],
    lane: str,
    vector_index: int,
    blocked: dict[str, set[str]],
    limit: int,
    include_group: bool = False,
) -> list[HybridRagHit]:
    if not query_vector:
        return []
    eligible: list[tuple[dict[str, object], list[float], list[float]]] = []
    for doc in docs:
        if _doc_blocked(doc, blocked):
            continue
        doc_vectors = vectors.get(str(doc["doc_id"]))
        if doc_vectors and doc_vectors[vector_index]:
            eligible.append((doc, doc_vectors[vector_index], doc_vectors[2]))
    try:
        import numpy as np

        if eligible:
            query_array = np.asarray(query_vector, dtype=np.float32)
            matrix = np.asarray([item[1] for item in eligible], dtype=np.float32)
            scores = matrix @ query_array
            if include_group:
                group_matrix = np.asarray(
                    [item[2] if item[2] else [0.0] * len(query_vector) for item in eligible],
                    dtype=np.float32,
                )
                group_scores = np.maximum(0.0, group_matrix @ query_array)
                compatibility = np.asarray([
                    float(dict(item[0].get("metadata") or {}).get("groupCompatibility") or 0.0)
                    for item in eligible
                ], dtype=np.float32)
                scores = scores * 0.75 + group_scores * 0.15 + compatibility * 0.10
            weighted = [
                (float(score), eligible[index][0])
                for index, score in enumerate(scores)
                if float(score) > 0.0
            ]
            weighted.sort(key=lambda item: item[0], reverse=True)
            return [
                _hit_from_doc(doc, lane=lane, rank=index, raw_score=score)
                for index, (score, doc) in enumerate(weighted[:limit], start=1)
            ]
    except (ImportError, ValueError):
        pass
    weighted: list[tuple[float, dict[str, object]]] = []
    for doc, doc_vector, group_vector in eligible:
        score = cosine_similarity(query_vector, doc_vector)
        if include_group and group_vector:
            group_score = max(0.0, cosine_similarity(query_vector, group_vector))
            compatibility = float(dict(doc.get("metadata") or {}).get("groupCompatibility") or 0.0)
            score = score * 0.75 + group_score * 0.15 + compatibility * 0.10
        if score > 0.0:
            weighted.append((score, doc))
    weighted.sort(key=lambda item: item[0], reverse=True)
    return [
        _hit_from_doc(doc, lane=lane, rank=index, raw_score=score)
        for index, (score, doc) in enumerate(weighted[:limit], start=1)
    ]


def _rank_time_docs(
    docs: list[dict[str, object]],
    *,
    expansion_terms: Iterable[str],
    blocked: dict[str, set[str]],
    limit: int,
    now_ms: int | None = None,
) -> list[HybridRagHit]:
    terms = [normalize_text(term) for term in expansion_terms if compact_whitespace(str(term))]
    weighted: list[tuple[float, dict[str, object]]] = []
    resolved_now_ms = (
        int(now_ms) if now_ms is not None else int(time.time() * 1_000)
    )
    for doc in docs:
        if _doc_blocked(doc, blocked):
            continue
        time_key = str(doc.get("time_key") or "")
        if not time_key:
            continue
        haystack = normalize_text(" ".join([str(doc.get("raw_text") or ""), str(doc.get("tags_text") or ""), time_key]))
        matched_terms = {term for term in terms if term and term in haystack}
        lexical_ratio = len(matched_terms) / max(1, len(set(terms)))
        recency = _time_recency_score(
            time_key,
            updated_at_ms=int(doc.get("updated_at_ms") or 0),
            now_ms=resolved_now_ms,
        )
        # The Time lane is deliberately an explicit temporal prior, not an
        # unbounded "all Daily Books" fallback. Query overlap keeps an old but
        # relevant book competitive while the half-life prevents it from
        # permanently outranking recent evidence.
        score = 0.20 + 0.55 * recency + 0.65 * lexical_ratio
        weighted.append((score, doc))
    weighted.sort(key=lambda item: item[0], reverse=True)
    return [_hit_from_doc(doc, lane="time", rank=index, raw_score=score) for index, (score, doc) in enumerate(weighted[:limit], start=1)]


def _feedback_hits(
    conn: sqlite3.Connection,
    *,
    docs: list[dict[str, object]],
    query: HybridRagQuery,
    terms: Iterable[str],
    blocked: dict[str, set[str]],
    limit: int,
) -> list[HybridRagHit]:
    source_ids = {str(doc["source_id"]): doc for doc in docs if not _doc_blocked(doc, blocked)}
    if not source_ids:
        return []
    rows = conn.execute(
        """
        SELECT memory_id, COUNT(*) AS accepted_count
        FROM candidate_feedback
        WHERE action = 'accepted'
          AND (? = '' OR project = ? OR project = '')
          AND (? = '' OR app = ? OR app = '')
        GROUP BY memory_id
        ORDER BY accepted_count DESC
        LIMIT ?
        """,
        (query.project, query.project, query.app, query.app, max(1, limit)),
    ).fetchall()
    hits: list[HybridRagHit] = []
    normalized_terms = _unique(token_terms(" ".join(str(item) for item in terms), max_terms=32))
    for index, row in enumerate(rows, start=1):
        doc = source_ids.get(str(row["memory_id"] or ""))
        if doc is None:
            continue
        relevance = _feedback_relevance(doc, normalized_terms)
        if relevance <= 0.0:
            continue
        metadata = dict(doc.get("metadata") or {})
        metadata["feedbackAccepted"] = True
        metadata["acceptedCount"] = int(row["accepted_count"] or 0)
        metadata["feedbackRelevance"] = relevance
        hits.append(
            _hit_from_doc(
                {**doc, "metadata": metadata},
                lane="feedback",
                rank=len(hits) + 1,
                raw_score=float(row["accepted_count"] or 0) * (1.0 + relevance),
            )
        )
        if len(hits) >= limit:
            break
    return hits


def _feedback_relevance(doc: dict[str, object], terms: list[str]) -> float:
    if not terms:
        return 0.0
    haystack = normalize_text(
        " ".join(
            str(doc.get(field) or "")
            for field in ("raw_text", "tags_text", "aliases_text", "surface_hints_text", "query_expansions_text")
        )
    )
    if not haystack:
        return 0.0
    matched = {term for term in terms if term and term in haystack}
    return len(matched) / max(1, len(terms))


_TIME_KEY_DATE_RE = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2}|\d{8})(?!\d)")


def _time_recency_score(time_key: str, *, updated_at_ms: int, now_ms: int) -> float:
    reference_ms = _time_key_ms(time_key) or max(0, int(updated_at_ms))
    if reference_ms <= 0:
        return 0.0
    age_days = max(0.0, (max(0, now_ms - reference_ms)) / 86_400_000.0)
    return math.exp(-math.log(2.0) * age_days / 30.0)


def _time_key_ms(time_key: str) -> int:
    match = _TIME_KEY_DATE_RE.search(str(time_key or ""))
    if match is None:
        return 0
    value = match.group(1)
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d" if "-" in value else "%Y%m%d")
    except ValueError:
        return 0
    return int(parsed.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _hit_from_doc(doc: dict[str, object], *, lane: str, rank: int, raw_score: float) -> HybridRagHit:
    metadata = dict(doc.get("metadata") or {})
    metadata["projectScope"] = bool(doc.get("project"))
    metadata["appScope"] = bool(doc.get("app"))
    metadata["ownerKind"] = str(doc.get("owner_kind") or "")
    metadata["ownerId"] = str(doc.get("owner_id") or "")
    if doc.get("doc_type") == "book" and not compact_whitespace(str(metadata.get("bookTitle") or "")):
        metadata["bookTitle"] = _book_title(str(doc.get("raw_text") or ""))
    return HybridRagHit(
        doc_id=str(doc["doc_id"]),
        doc_type=str(doc["doc_type"]),
        source_id=str(doc["source_id"]),
        text=compact_whitespace(str(doc.get("raw_text") or "")),
        surface_hints=tuple(compact_whitespace(str(doc.get("surface_hints_text") or "")).split()),
        tags=tuple(compact_whitespace(str(doc.get("tags_text") or "")).split()),
        source_lane=lane,
        rank=rank,
        raw_score=float(raw_score),
        metadata=metadata,
    )


def _blocked_sets(conn: sqlite3.Connection) -> dict[str, set[str]]:
    blocked = {"memory_id": set(), "normalized_text": set(), "text": set(), "suppressed_memory_id": set(), "suppressed_text": set()}
    for row in conn.execute("SELECT target_type, target_value FROM memory_tombstones WHERE active = 1").fetchall():
        target_type = str(row["target_type"] or "")
        target_value = compact_whitespace(str(row["target_value"] or ""))
        if target_type in blocked and target_value:
            blocked[target_type].update(_memory_id_variants(target_value) if target_type == "memory_id" else {target_value})
    for row in conn.execute("SELECT match_type, match_value FROM memory_candidate_suppressions").fetchall():
        match_type = str(row["match_type"] or "")
        match_value = compact_whitespace(str(row["match_value"] or ""))
        if match_type == "memory_id" and match_value:
            blocked["suppressed_memory_id"].update(_memory_id_variants(match_value))
        elif match_value:
            blocked["suppressed_text"].add(match_value)
    return blocked


def _doc_blocked(doc: dict[str, object], blocked: dict[str, set[str]]) -> bool:
    source_id = str(doc.get("source_id") or "")
    text = compact_whitespace(str(doc.get("raw_text") or ""))
    normalized = normalize_text(text)
    return (
        source_id in blocked["memory_id"]
        or source_id in blocked["suppressed_memory_id"]
        or f"phrase:{normalized}" in blocked["memory_id"]
        or text in blocked["text"]
        or normalized in blocked["normalized_text"]
        or normalized in blocked["suppressed_text"]
    )


def _memory_id_variants(value: str) -> set[str]:
    text = compact_whitespace(value)
    variants = {text}
    if text.startswith("phrase:"):
        variants.add(f"phrase:{normalize_text(text.split(':', 1)[1])}")
    return {item for item in variants if item}


def _metadata(raw: object) -> dict[str, object]:
    try:
        parsed = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _memory_decay_settings(conn: sqlite3.Connection) -> dict[str, object]:
    try:
        row = conn.execute(
            "SELECT value_json FROM management_settings WHERE key = 'memory' LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return {}
    if row is None:
        return {}
    payload = _metadata(row[0])
    value = payload.get("timeDecay")
    return dict(value) if isinstance(value, dict) else {}


def _book_title(raw_text: str) -> str:
    text = compact_whitespace(raw_text)
    if not text:
        return ""
    return text.split("。", 1)[0].split("，", 1)[0][:24]


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = compact_whitespace(str(value))
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result
