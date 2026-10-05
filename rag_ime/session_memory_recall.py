from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from .contracts.json_schema import validate_contract
from .db import apply_database_migrations
from .embeddings import EmbeddingProvider, NullEmbeddingProvider
from .hybrid_rag_models import HybridRagQuery
from .knowledge_scope import session_knowledge_caller
from .hybrid_rag_retriever import _active_docs, retrieve_hybrid_rag_candidates
from .memory_card_mutations import card_source_refs
from .input_event_assembly import recent_complete_input_context
from .memory_ownership import agent_visible_memory_owners
from .memory_maintenance_settings import MemoryMaintenanceSettings
from .retrieval_docs import _source_events_retrievable
from .text_utils import compact_whitespace, split_sentences, token_terms, truncate_text
from .timeline_intent import classify_timeline_intent


SESSION_MEMORY_RECALL_SCHEMA_VERSION = "rag-ime.session-memory-recall.v1"
_BOOTSTRAP_DEDUPE_VERSION = "v3"
_REFRESH_DEDUPE_VERSION = "v4"
_RELEVANCE_LANES = frozenset(
    {
        "bm25_raw",
        "bm25_tags",
        "vector_raw",
        "vector_tag_boost",
        "tagmemo",
        "feedback",
    }
)
_RECALL_DETAIL_PROFILES: dict[str, dict[str, int]] = {
    "compact": {
        "maxItems": 9,
        "maxChars": 6_000,
        "bookChars": 480,
        "timelineChars": 650,
        "atomChars": 520,
        "bookItems": 2,
        "atomItems": 6,
    },
    "balanced": {
        "maxItems": 12,
        "maxChars": 10_000,
        "bookChars": 800,
        "timelineChars": 900,
        "atomChars": 720,
        "bookItems": 2,
        "atomItems": 8,
    },
    "detailed": {
        "maxItems": 12,
        "maxChars": 14_000,
        "bookChars": 1_400,
        "timelineChars": 1_400,
        "atomChars": 900,
        "bookItems": 3,
        "atomItems": 10,
    },
}
_PREVERIFIED_RECALL_TABLES = frozenset(
    {
        "agent_memory_sources",
        "candidate_feedback",
        "input_events",
        "management_settings",
        "memory_candidate_suppressions",
        "memory_retrieval_docs",
        "memory_retrieval_docs_fts",
        "memory_state",
        "memory_tombstones",
    }
)


def _visible_memory_owners_for_trigger(
    *,
    project: str,
    role_id: str,
    session_id: str,
    room_ids: Sequence[str],
    trigger: str,
) -> tuple[tuple[str, str], ...]:
    owners = agent_visible_memory_owners(
        project=project,
        role_id=role_id,
        session_id=session_id,
        room_ids=room_ids,
    )
    if trigger not in {"room_task", "subagent_task"}:
        return owners
    # A delegated TaskBrief is already a bounded context envelope. Injecting
    # user/shared/role memory from another workspace dilutes that contract and
    # can make a Partner solve the remembered project instead of its WorkItem.
    return tuple(
        owner
        for owner in owners
        if owner[0] in {"session", "room"}
    )


class SessionMemoryRecallBuilder:
    """Build one query-aware, role-scoped memory pack for an Agent Session."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        project: str = "",
        embedding_provider: EmbeddingProvider | None = None,
        preverified_schema: bool = False,
    ) -> None:
        self.db_path = Path(db_path)
        self.project = compact_whitespace(project)
        self.embedding_provider = embedding_provider or NullEmbeddingProvider()
        self.preverified_schema = bool(preverified_schema)

    def initialize(self) -> None:
        if self.preverified_schema and not self.db_path.is_file():
            raise FileNotFoundError(
                f"preverified Session recall database does not exist: {self.db_path}"
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            if self.preverified_schema:
                _require_preverified_recall_schema(conn)
            else:
                apply_database_migrations(conn)

    @staticmethod
    def dedupe_key(session_id: str) -> str:
        return f"memory-bootstrap:{compact_whitespace(session_id)}:{_BOOTSTRAP_DEDUPE_VERSION}"

    @staticmethod
    def refresh_dedupe_key(session_id: str, recall_id: str) -> str:
        material = compact_whitespace(recall_id) or "unknown"
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
        return (
            f"memory-bootstrap:{compact_whitespace(session_id)}:"
            f"{_REFRESH_DEDUPE_VERSION}:{digest}"
        )

    def build(
        self,
        session_id: str,
        *,
        role_id: str,
        query_text: str,
        room_ids: Sequence[str] = (),
        trigger: str = "first_user_prompt",
        retrieval_context_text: str = "",
        vector_context_text: str = "",
        vector_context_weight: float = 0.0,
        recent_messages: Sequence[Mapping[str, object]] = (),
        todo_context: Mapping[str, object] | None = None,
        task_context: Mapping[str, object] | None = None,
        compaction_recovery: Mapping[str, object] | None = None,
        max_items: int = 12,
        max_chars: int = 14_000,
        generated_at_ms: int | None = None,
    ) -> dict[str, object]:
        session = compact_whitespace(session_id)
        query = compact_whitespace(query_text)
        role = compact_whitespace(role_id)
        if not session:
            raise ValueError("session_id must not be empty")
        if not query:
            raise ValueError("query_text must not be empty")
        normalized_trigger = compact_whitespace(trigger).lower()
        if normalized_trigger not in {
            "first_user_prompt",
            "turn_start",
            "compaction",
            "room_task",
            "subagent_task",
        }:
            raise ValueError("unsupported Session memory recall trigger")

        visible_owners = _visible_memory_owners_for_trigger(
            project=self.project,
            role_id=role,
            session_id=session,
            room_ids=room_ids,
            trigger=normalized_trigger,
        )
        managed = MemoryMaintenanceSettings.load(
            self.db_path,
            preverified_schema=self.preverified_schema,
        )
        detail_level = managed.recall_detail_level
        detail_profile = _RECALL_DETAIL_PROFILES[detail_level]
        bounded_items = max(
            1,
            min(
                int(max_items),
                detail_profile["maxItems"],
            ),
        )
        bounded_chars = max(
            1_200,
            min(
                int(max_chars),
                detail_profile["maxChars"],
            ),
        )
        generated = int(
            generated_at_ms
            if generated_at_ms is not None
            else time.time() * 1_000
        )

        with self._connect() as conn:
            knowledge_caller = session_knowledge_caller(conn, session)
            recent = recent_complete_input_context(
                conn,
                project=self.project,
                query_text=query,
                baseline_records=6,
                max_records=8,
                token_budget=1_400,
                reserved_tokens=500,
            )
            recent_text = _tail_text(str(recent.get("rendered") or ""), 2_400)
            retrieval_context = _tail_text(retrieval_context_text, 4_000)
            lexical_context = _joined_context(recent_text, retrieval_context, maximum=5_600)
            hybrid_query = HybridRagQuery(
                query_text=query,
                raw_input=lexical_context,
                committed_tail=_tail_text(lexical_context, 800),
                project=self.project,
                input_mode=f"agent_session_{normalized_trigger}",
                top_k=max(24, bounded_items * 4),
                latency_budget_ms=2_500,
                visible_owners=visible_owners,
                knowledge_caller=knowledge_caller,
                vector_context_text=_tail_text(vector_context_text, 6_000),
                vector_context_weight=vector_context_weight,
            )
            requested_embedding = str(
                getattr(self.embedding_provider, "fingerprint", "none") or "none"
            )
            embedding_fallback = False
            try:
                retrieval = retrieve_hybrid_rag_candidates(
                    conn,
                    hybrid_query,
                    self.embedding_provider,
                )
                effective_embedding = requested_embedding
            except Exception:
                # A local/remote vector endpoint must not suppress the lexical,
                # TagMemo, ownership, and lifecycle lanes of Session startup.
                retrieval = retrieve_hybrid_rag_candidates(
                    conn,
                    hybrid_query,
                    NullEmbeddingProvider(),
                )
                effective_embedding = "none"
                embedding_fallback = requested_embedding != "none"

            coverage = _curation_coverage(conn, self.project) if ("user", "default") in visible_owners else None

        selected, omitted = _select_hits(
            retrieval.get("memoryHits"),
            query_text=query,
            max_items=bounded_items,
            max_chars=bounded_chars,
            detail_level=detail_level,
            timeline_allowed=managed.timeline_recall_enabled,
            timeline_max_items=managed.timeline_max_items,
        )
        activated_tags = _selected_activated_tags(retrieval, selected)
        timeline_intent = classify_timeline_intent(
            query,
            enabled=managed.timeline_recall_enabled,
        )
        temporal_intent = timeline_intent.requested
        activity_timeline_included = any(
            item.get("sourceType") == "memory_timeline"
            or (
                item.get("sourceType") == "memory_book"
                and {"daily", "activity-timeline"}.intersection(
                {tag.casefold() for tag in _string_list(item.get("tags"), 16)}
            )
            )
            for item in selected
        )
        source_ids = [str(item["sourceId"]) for item in selected]
        selected_content_sha256 = hashlib.sha256(
            json.dumps(
                selected,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        query_sha256 = hashlib.sha256(query.encode("utf-8")).hexdigest()
        vector_context = _tail_text(vector_context_text, 6_000)
        conversation = _normalized_recent_messages(recent_messages)
        todo = _normalized_todo(todo_context)
        task = _normalized_task(task_context)
        recovery = (
            dict(compaction_recovery)
            if isinstance(compaction_recovery, Mapping)
            else {}
        )
        recall_material = json.dumps(
            {
                "sessionId": session,
                "trigger": normalized_trigger,
                "querySha256": query_sha256,
                "vectorContextSha256": (
                    hashlib.sha256(vector_context.encode("utf-8")).hexdigest()
                    if vector_context
                    else ""
                ),
                "sourceIds": source_ids,
                # A governed Atom can be corrected in place while retaining
                # its source id.  The recall identity therefore includes the
                # selected content, not only the list of source ids, so the
                # Provider context can invalidate superseded text.
                "selectedContentSha256": selected_content_sha256,
                "coverage": coverage,
                "compactionRecovery": recovery,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        payload: dict[str, object] = {
            "schemaVersion": SESSION_MEMORY_RECALL_SCHEMA_VERSION,
            "recallId": (
                "session-memory-recall:"
                + hashlib.sha256(recall_material.encode("utf-8")).hexdigest()[:24]
            ),
            "sessionId": session,
            "project": self.project,
            "roleId": role,
            "generatedAtMs": max(0, generated),
            "trigger": normalized_trigger,
            "query": {
                "preview": truncate_text(query, 240),
                "sha256": query_sha256,
                "recentCompleteInputCount": len(recent.get("records") or []),
                "recentCompleteInputUsedForRetrieval": bool(recent_text),
                "retrievalContextUsed": bool(retrieval_context),
                "recentConversationCount": len(conversation),
                "timelineIntent": timeline_intent.as_dict(),
            },
            "retrieval": {
                "strategy": "vcp_hybrid_book_atom",
                "primaryQuery": truncate_text(
                    str(_mapping(retrieval.get("query")).get("primary") or query),
                    240,
                ),
                "matchedAliases": _string_list(
                    _mapping(retrieval.get("query")).get("matchedAliases"),
                    12,
                ),
                "activatedTags": activated_tags,
                "visibleOwners": [
                    {"ownerKind": kind, "ownerId": owner_id}
                    for kind, owner_id in visible_owners
                ],
                "requestedEmbeddingProvider": requested_embedding,
                "embeddingProvider": effective_embedding,
                "embeddingFallback": embedding_fallback,
                "temporalIntent": temporal_intent,
                "timelineIntent": timeline_intent.as_dict(),
                "activityTimelineIncluded": activity_timeline_included,
                "vectorFusion": dict(
                    _mapping(retrieval.get("query")).get("vectorFusion")
                    if isinstance(
                        _mapping(retrieval.get("query")).get("vectorFusion"),
                        Mapping,
                    )
                    else {}
                ),
            },
            "items": selected,
            "recentConversation": conversation,
            "todo": todo,
            "task": task,
            "sourceIds": source_ids,
            "budget": {
                "maxItems": bounded_items,
                "maxChars": bounded_chars,
                "usedChars": sum(len(str(item.get("text") or "")) for item in selected),
                "omittedCount": omitted,
            },
            "policy": {
                "priority": "developer",
                "lifecycle": "session",
                "evidenceOnly": True,
                "currentUserMessageWins": True,
                "rawRecentInputInjected": False,
                "recentConversationInjected": bool(conversation),
                "detailLevel": detail_level,
            },
        }
        if recovery:
            payload["compactionRecovery"] = recovery
        if coverage is not None:
            payload["coverage"] = coverage
        validate_contract(payload, "session-memory-recall.v1.json")
        return {
            "session_id": session,
            "source_kind": "memory_bootstrap",
            "source_id": str(payload["recallId"]),
            "title": (
                "压缩后任务上下文"
                if normalized_trigger == "compaction"
                else "任务相关个人记忆"
            ),
            "summary": (
                f"当前任务从角色可见 Book/Atom 中召回 {len(selected)} 条证据。"
            ),
            "payload": payload,
            "lane": "fact",
            # Provider calls are stateless: high-priority memory must be
            # re-projected on every turn and restored after Runtime restart.
            "lifecycle": "persistent",
            "dedupe_key": (
                self.dedupe_key(session)
                if normalized_trigger == "first_user_prompt"
                else self.refresh_dedupe_key(session, str(payload["recallId"]))
            ),
        }

    def revalidate_items(
        self, session_id: str, payload: Mapping[str, object],
    ) -> list[dict[str, object]]:
        """Check current sources of a primary Session pack; never run retrieval."""

        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            return []
        items = [dict(item) for item in raw_items[:32] if isinstance(item, Mapping)
                 and item.get("sourceType") in {"memory_atom", "memory_book", "memory_timeline"}
                 and isinstance(item.get("sourceId"), str) and 0 < len(str(item["sourceId"])) <= 240]
        generated = payload.get("generatedAtMs")
        if not items or not isinstance(generated, int) or isinstance(generated, bool) or generated <= 0:
            return []
        query_text = str(_mapping(payload.get("query")).get("preview") or "")
        with self._connect() as conn:
            docs = _active_docs(
                conn, query=HybridRagQuery(
                    query_text=query_text, project=self.project,
                    visible_owners=agent_visible_memory_owners(project=self.project, session_id=session_id),
                    knowledge_caller=session_knowledge_caller(conn, session_id),
                ),
                source_ids=tuple(dict.fromkeys(str(item["sourceId"]) for item in items)),
            )
            current = {(str(doc["doc_type"]), str(doc["source_id"])): doc for doc in docs}
            kept: list[dict[str, object]] = []
            for item in items:
                kind = str(item["sourceType"]).removeprefix("memory_")
                doc = current.get((kind, str(item["sourceId"])))
                if doc is None or int(doc.get("updated_at_ms") or 0) > generated:
                    continue
                if not _recalled_source_unchanged(conn, item, doc, generated_at_ms=generated, query_text=query_text):
                    continue
                kept.append(item)
        return kept

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


def _require_preverified_recall_schema(conn: sqlite3.Connection) -> None:
    """Verify the tables consumed by Session recall without replaying DDL."""

    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    missing = sorted(_PREVERIFIED_RECALL_TABLES - tables)
    if missing:
        raise RuntimeError(
            "preverified Session recall database is missing required tables: "
            + ",".join(missing)
        )


def _curation_coverage(conn: sqlite3.Connection, project: str) -> dict[str, object]:
    """A cheap cursor projection; a no-hit result is never evidence of absence."""
    row = conn.execute("""SELECT last_source_created_at_ms, last_run_ms, status
        FROM memory_curation_cursors
        WHERE owner_kind = 'user' AND owner_id = 'default' AND project = ? AND lane = 'daily'
        """, (project,)).fetchone()
    if row is None:
        return {"status": "unknown", "processedThroughAtMs": 0, "lastRunAtMs": 0}
    return {"status": str(row["status"]),
            "processedThroughAtMs": max(0, int(row["last_source_created_at_ms"] or 0)),
            "lastRunAtMs": max(0, int(row["last_run_ms"] or 0))}


def _recalled_source_unchanged(
    conn: sqlite3.Connection, item: Mapping[str, object], doc: Mapping[str, object], *, generated_at_ms: int,
    query_text: str,
) -> bool:
    kind = str(doc["doc_type"])
    source_id = str(doc["source_id"])
    if kind == "timeline":
        # An old derived timeline must prove current admitted source support
        # before a primary Session reuses it. Unknown historical sets omit.
        event_ids = _mapping(doc.get("metadata")).get("sourceEventIds")
        cached = compact_whitespace(str(item.get("text") or ""))
        return bool(cached and isinstance(event_ids, list) and 0 < len(event_ids) <= 256
                    and _source_events_retrievable(conn, event_ids)
                    and cached == _focused_activity_excerpt(str(doc.get("raw_text") or ""),
                        query_text=query_text, max_chars=len(cached)))
    table, key = ("memory_atoms", "id") if kind == "atom" else ("memory_books", "book_id")
    row = conn.execute(f"SELECT * FROM {table} WHERE {key}=?", (source_id,)).fetchone()
    if row is None:
        return False
    source = dict(row)
    if int(source.get("updated_at_ms") or 0) > generated_at_ms:
        return False
    metadata = _mapping(doc.get("metadata"))
    for snake, camel in (
        ("owner_kind", "ownerKind"), ("owner_id", "ownerId"), ("knowledge_domain", "knowledgeDomain"),
        ("scope_kind", "scopeKind"), ("scope_id", "scopeId"), ("visibility", "visibility"),
        ("authorization_revision", "authorizationRevision"), ("binding_id", "bindingId"), ("scope_mode", "scopeMode"),
    ):
        if str(source.get(snake) or "") != str(metadata.get(camel) or ""):
            return False
    cached = compact_whitespace(str(item.get("text") or ""))
    if not cached:
        return False
    if kind == "atom":
        current_text = str(source.get("canonical_text") or source.get("text") or "")
        if truncate_text(current_text, len(cached)) != cached:
            return False
        atoms = [source]
    else:
        expanded, _ = _book_recall_text(str(doc.get("raw_text") or ""), metadata,
                                       max_chars=len(cached), already_inlined=set())
        if cached not in {expanded, truncate_text(str(source.get("summary") or ""), len(cached))}:
            return False
        atom_ids = json.loads(str(source.get("memory_atom_ids_json") or "[]"))
        if not isinstance(atom_ids, list) or len(atom_ids) > 64:
            return False
        atoms = []
        for atom_id in atom_ids:
            atom = conn.execute("SELECT * FROM memory_atoms WHERE id=?", (str(atom_id),)).fetchone()
            if atom is None:
                return False
            atoms.append(dict(atom))
    for atom in atoms:
        if int(atom.get("updated_at_ms") or 0) > generated_at_ms:
            return False
        if atom.get("scope_mode") == "authoritative" and atom.get("knowledge_domain") in {"personal_memory", "user_profile_preference"}:
            if not card_source_refs(conn, str(atom["id"])):
                return False
    return True


def _select_hits(
    value: object,
    *,
    query_text: str,
    max_items: int,
    max_chars: int,
    detail_level: str = "compact",
    timeline_allowed: bool = True,
    timeline_max_items: int = 2,
) -> tuple[list[dict[str, object]], int]:
    candidates = (
        [dict(item) for item in value if isinstance(item, Mapping)]
        if isinstance(value, list)
        else []
    )
    selected: list[dict[str, object]] = []
    seen_sources: set[str] = set()
    selected_book_text_by_atom: dict[str, list[str]] = {}
    inlined_book_atom_ids: set[str] = set()
    type_counts = {"book": 0, "atom": 0, "timeline": 0}
    used_chars = 0
    eligible_count = 0
    profile = _RECALL_DETAIL_PROFILES.get(
        detail_level,
        _RECALL_DETAIL_PROFILES["compact"],
    )
    timeline_intent = classify_timeline_intent(
        query_text,
        enabled=timeline_allowed,
    )
    activity_timeline_allowed = timeline_intent.requested
    precise_timeline_date = _precise_timeline_date(timeline_intent.range)
    preferred_book_sources = _preferred_book_source_ids(
        candidates,
        query_text=query_text,
        activity_timeline_allowed=activity_timeline_allowed,
        precise_timeline_date=precise_timeline_date,
        limit=2,
    )
    for item in candidates:
        doc_type = compact_whitespace(str(item.get("doc_type") or ""))
        metadata = _mapping(item.get("metadata"))
        lanes = _string_list(metadata.get("lanes"), 16)
        tags = _string_list(item.get("tags"), 16)
        if doc_type not in {"book", "atom", "timeline"}:
            continue
        if bool(metadata.get("archived")):
            continue
        normalized_tags = {tag.casefold() for tag in tags}
        is_activity_timeline = bool(
            doc_type == "timeline"
            or str(metadata.get("derivedArtifactType") or "")
            == "daily_activity_timeline"
            or {"daily", "activity-timeline"}.issubset(normalized_tags)
        )
        if (
            is_activity_timeline
            and not activity_timeline_allowed
        ):
            # Timelines summarize broad activity and only enter a retrieval
            # turn when the user asks a temporal or continuation question.
            continue
        if (
            is_activity_timeline
            and not precise_timeline_date
            and not _activity_matches_subject(item, query_text=query_text)
        ):
            continue
        if not set(lanes).intersection(_RELEVANCE_LANES):
            continue
        eligible_count += 1
        source_id = compact_whitespace(str(item.get("source_id") or ""))
        if doc_type == "book" and source_id not in preferred_book_sources:
            continue
        per_item_chars = {
            "book": profile["bookChars"],
            "timeline": profile["timelineChars"],
            "atom": profile["atomChars"],
        }[doc_type]
        inlined_atom_ids: set[str] = set()
        if is_activity_timeline:
            text = _focused_activity_excerpt(
                str(item.get("text") or ""),
                query_text=query_text,
                max_chars=per_item_chars,
            )
        elif doc_type == "book":
            text, inlined_atom_ids = _book_recall_text(
                str(item.get("text") or ""),
                metadata,
                max_chars=per_item_chars,
                already_inlined=inlined_book_atom_ids,
            )
        else:
            text = truncate_text(str(item.get("text") or ""), per_item_chars)
        if not source_id or not text or source_id in seen_sources:
            continue
        type_limit = {
            "book": profile["bookItems"],
            "timeline": max(1, min(int(timeline_max_items), 4)),
            "atom": profile["atomItems"],
        }[doc_type]
        if type_counts[doc_type] >= type_limit:
            continue
        if selected and used_chars + len(text) > max_chars:
            continue
        raw_scores = {
            str(key): round(float(score), 6)
            for key, score in _mapping(metadata.get("rawScores")).items()
            if isinstance(score, (int, float))
        }
        selected.append(
            {
                "rank": len(selected) + 1,
                "sourceType": f"memory_{doc_type}",
                "sourceId": source_id,
                "title": _human_memory_title(doc_type, metadata),
                "text": text,
                "score": round(float(item.get("score") or 0.0), 6),
                "confidence": round(float(item.get("confidence") or 0.0), 6),
                "lanes": lanes,
                "rawScores": raw_scores,
                "tags": tags,
                "ownerKind": compact_whitespace(
                    str(metadata.get("ownerKind") or "user")
                ),
                "ownerId": compact_whitespace(
                    str(metadata.get("ownerId") or "default")
                ),
                "evidenceEventIds": [
                    int(event_id)
                    for event_id in item.get("evidence_event_ids") or []
                    if isinstance(event_id, int) and event_id > 0
                ][:16],
                **{
                    key: metadata[key]
                    for key in ("project", "claimState", "validFromMs", "validToMs",
                                "supersedesId", "sourceUpdatedAtMs", "sourceStartMs", "sourceEndMs")
                    if key in metadata and metadata[key] is not None
                },
                "corroborationOnly": is_activity_timeline or bool(metadata.get("corroborationOnly")),
                "maySupportFacts": not is_activity_timeline and metadata.get("maySupportFacts") is not False,
            }
        )
        if doc_type == "book":
            for atom_id in _string_list(metadata.get("memoryAtomIds"), 256):
                selected_book_text_by_atom.setdefault(atom_id, []).append(text)
            inlined_book_atom_ids.update(inlined_atom_ids)
        seen_sources.add(source_id)
        type_counts[doc_type] += 1
        used_chars += len(text)
        if len(selected) >= max_items:
            break
    if selected_book_text_by_atom:
        selected = [
            item
            for item in selected
            if not (
                item.get("sourceType") == "memory_atom"
                and _atom_text_is_covered_by_selected_book(
                    item,
                    selected_book_text_by_atom,
                )
            )
        ]
        for rank, item in enumerate(selected, start=1):
            item["rank"] = rank
    return selected, max(0, eligible_count - len(selected))


def _book_recall_text(
    summary_text: str,
    metadata: Mapping[str, object],
    *,
    max_chars: int,
    already_inlined: set[str],
) -> tuple[str, set[str]]:
    """Expand a Book only when every member fits this consumer's budget."""

    summary = truncate_text(summary_text, max_chars)
    atom_ids = _string_list(metadata.get("memoryAtomIds"), 256)
    if not atom_ids or metadata.get("inlineAtomsComplete") is not True:
        return summary, set()
    inline_rows = [
        dict(item)
        for item in metadata.get("inlineAtoms") or []
        if isinstance(item, Mapping)
    ]
    inline_by_id = {
        compact_whitespace(str(item.get("atomId") or "")): compact_whitespace(
            str(item.get("text") or "")
        )
        for item in inline_rows
        if compact_whitespace(str(item.get("atomId") or ""))
        and compact_whitespace(str(item.get("text") or ""))
    }
    if any(atom_id not in inline_by_id for atom_id in atom_ids):
        return summary, set()

    expanded = compact_whitespace(summary_text)
    newly_inlined: set[str] = set()
    for atom_id in atom_ids:
        if atom_id in already_inlined:
            continue
        atom_text = inline_by_id[atom_id]
        newly_inlined.add(atom_id)
        if _coverage_text(atom_text) in _coverage_text(expanded):
            continue
        expanded = compact_whitespace(f"{expanded} 主题事实：{atom_text}")
    if len(expanded) > max_chars:
        return summary, set()
    return expanded, newly_inlined


def _atom_text_is_covered_by_selected_book(
    item: Mapping[str, object],
    book_text_by_atom: Mapping[str, Sequence[str]],
) -> bool:
    source_id = compact_whitespace(str(item.get("sourceId") or ""))
    atom_text = _coverage_text(str(item.get("text") or ""))
    if not source_id or not atom_text:
        return False
    return any(
        atom_text in _coverage_text(book_text)
        for book_text in book_text_by_atom.get(source_id, ())
    )


def _coverage_text(value: str) -> str:
    return re.sub(r"[^\w]+", "", compact_whitespace(value).casefold())


def _human_memory_title(doc_type: str, metadata: Mapping[str, object]) -> str:
    explicit = compact_whitespace(
        str(metadata.get("bookTitle") or metadata.get("timelineTitle") or "")
    )
    if explicit:
        return truncate_text(explicit, 180)
    kind = compact_whitespace(str(metadata.get("kind") or "")).casefold()
    labels = {
        "preference": "用户偏好",
        "durable_preference": "用户偏好",
        "personal_fact": "个人信息",
        "personal_habit": "个人习惯",
        "personal_principle": "个人原则",
        "project_fact": "项目事实",
        "project_requirement": "项目要求",
        "decision": "已确认决定",
        "constraint": "明确约束",
    }
    if kind in labels:
        return labels[kind]
    return {"book": "主题书", "timeline": "近期记录", "atom": "已治理事实"}[doc_type]


def _preferred_book_source_ids(
    candidates: Sequence[Mapping[str, object]],
    *,
    query_text: str,
    activity_timeline_allowed: bool,
    precise_timeline_date: bool,
    limit: int,
) -> set[str]:
    books: list[Mapping[str, object]] = []
    for item in candidates:
        if compact_whitespace(str(item.get("doc_type") or "")) != "book":
            continue
        metadata = _mapping(item.get("metadata"))
        if bool(metadata.get("archived")):
            continue
        if not set(_string_list(metadata.get("lanes"), 16)).intersection(
            _RELEVANCE_LANES
        ):
            continue
        tags = {tag.casefold() for tag in _string_list(item.get("tags"), 16)}
        if {"daily", "activity-timeline"}.intersection(tags):
            if not activity_timeline_allowed:
                continue
            if (
                not precise_timeline_date
                and not _activity_matches_subject(item, query_text=query_text)
            ):
                continue
        books.append(item)
    ranked = sorted(
        books,
        key=lambda item: _book_relevance_key(
            item,
            query_text=query_text,
            activity_timeline_allowed=activity_timeline_allowed,
            precise_timeline_date=precise_timeline_date,
        ),
        reverse=True,
    )
    return {
        compact_whitespace(str(item.get("source_id") or ""))
        for item in ranked[: max(0, int(limit))]
        if compact_whitespace(str(item.get("source_id") or ""))
    }


def _book_relevance_key(
    item: Mapping[str, object],
    *,
    query_text: str,
    activity_timeline_allowed: bool,
    precise_timeline_date: bool,
) -> tuple[float, int, float, float]:
    tags = _string_list(item.get("tags"), 16)
    normalized_query = compact_whitespace(query_text).casefold()
    tag_matches = sum(
        1
        for tag in tags
        if compact_whitespace(tag).casefold()
        and compact_whitespace(tag).casefold() in normalized_query
    )
    normalized_tags = {tag.casefold() for tag in tags}
    temporal_priority = (
        1.0
        if activity_timeline_allowed
        and {"daily", "activity-timeline"}.intersection(normalized_tags)
        and (
            precise_timeline_date
            or _activity_matches_subject(item, query_text=query_text)
        )
        else 0.0
    )
    raw_scores = _mapping(_mapping(item.get("metadata")).get("rawScores"))
    vector_relevance = sum(
        float(raw_scores.get(lane) or 0.0)
        for lane in ("vector_raw", "vector_tag_boost")
    )
    return (
        temporal_priority,
        tag_matches,
        vector_relevance,
        float(item.get("score") or 0.0),
    )


def _focused_activity_excerpt(
    value: str,
    *,
    query_text: str,
    max_chars: int,
) -> str:
    text = compact_whitespace(value)
    segments = split_sentences(text)
    if not segments:
        return truncate_text(text, max_chars)
    terms = _subject_query_terms(query_text)
    if not terms:
        return truncate_text(text, max_chars)
    scored: list[tuple[float, int]] = []
    for index, segment in enumerate(segments):
        normalized = segment.casefold()
        matched = {term for term in terms if _subject_term_hits(term, normalized)}
        if not matched:
            continue
        term_score = sum(2.0 if len(term) >= 3 else 1.0 for term in matched)
        recency_tiebreaker = index / max(1, len(segments))
        scored.append((term_score + recency_tiebreaker, index))
    if scored:
        chosen = sorted(
            index for _, index in sorted(scored, reverse=True)[:5]
        )
    else:
        chosen = list(range(max(0, len(segments) - 4), len(segments)))
    excerpt = "；".join(
        compact_whitespace(segments[index]).rstrip("；;。")
        for index in chosen
        if compact_whitespace(segments[index]).rstrip("；;。")
    )
    return truncate_text(f"相关时间线片段：{excerpt}", max_chars)


def _activity_matches_subject(
    item: Mapping[str, object],
    *,
    query_text: str,
) -> bool:
    terms = _subject_query_terms(query_text)
    if not terms:
        return True
    metadata = _mapping(item.get("metadata"))
    haystack = " ".join(
        (
            str(item.get("text") or ""),
            str(metadata.get("bookTitle") or ""),
            str(metadata.get("timelineTitle") or ""),
            " ".join(_string_list(item.get("tags"), 16)),
        )
    ).casefold()
    return any(_subject_term_hits(term, haystack) for term in terms)


_SUBJECT_QUERY_NOISE_RE = re.compile(
    r"(?:今天|今日|昨天|昨日|前天|最近(?:几天)?|近期|这几天|近几天|"
    r"过去(?:几|[1-9]\d?)天|本周|这周|这个星期|上周|上个星期|"
    r"本月|这个月|上月|上个月|时间线|活动记录|工作记录|最近工作|"
    r"daily\s*book|timeline|activity\s*(?:log|history)|recent\s+work|"
    r"帮我|看一下|看看|查看|我|我们|项目|工作|活动|"
    r"做到哪里了?|做了什么|做什么|进展(?:如何|怎么样)?|"
    r"什么|怎么|怎样|如何|哪里|哪儿|主要|当前|现在)",
    re.IGNORECASE,
)


def _subject_query_terms(value: str) -> list[str]:
    subject = compact_whitespace(_SUBJECT_QUERY_NOISE_RE.sub(" ", value))
    return [
        term.casefold()
        for term in token_terms(subject, max_terms=32)
        if len(term) >= 2
    ]


def _precise_timeline_date(range_name: str) -> bool:
    return range_name in {"today", "yesterday", "day_before_yesterday"} or (
        re.fullmatch(r"(?:20\d{2}-)?\d{1,2}-\d{1,2}", range_name) is not None
    )


def _subject_term_hits(term: str, text: str) -> bool:
    if re.fullmatch(r"[a-z0-9_+#.\-]+", term):
        return (
            re.search(
                rf"(?<![a-z0-9_]){re.escape(term)}(?![a-z0-9_])",
                text,
            )
            is not None
        )
    return term in text


def _selected_activated_tags(
    retrieval: Mapping[str, object],
    selected: Sequence[Mapping[str, object]],
) -> list[str]:
    selected_tags = {
        tag.casefold()
        for item in selected
        for tag in _string_list(item.get("tags"), 16)
    }
    return [
        tag
        for tag in _string_list(
            _mapping(retrieval.get("query")).get("activatedTags"),
            32,
        )
        if tag.casefold() in selected_tags
    ][:16]


def _joined_context(*values: str, maximum: int) -> str:
    joined = "\n".join(
        compact_whitespace(value)
        for value in values
        if compact_whitespace(value)
    )
    return _tail_text(joined, maximum)


def _normalized_recent_messages(
    values: Sequence[Mapping[str, object]],
    *,
    limit: int = 8,
    max_chars: int = 5_000,
) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    used = 0
    for value in reversed(list(values)):
        role = compact_whitespace(str(value.get("role") or "")).lower()
        if role not in {"user", "assistant"}:
            continue
        text = _message_text(value)
        if not text:
            continue
        text = truncate_text(text, min(1_200, max_chars))
        if normalized and used + len(text) > max_chars:
            break
        normalized.append({"role": role, "text": text})
        used += len(text)
        if len(normalized) >= max(1, int(limit)):
            break
    normalized.reverse()
    return normalized


def _message_text(value: Mapping[str, object]) -> str:
    direct = compact_whitespace(str(value.get("text") or ""))
    if direct:
        return direct
    content = value.get("content")
    if isinstance(content, str):
        return compact_whitespace(content)
    if not isinstance(content, (list, tuple)):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, Mapping):
            continue
        if str(block.get("type") or "").lower() not in {"text", "output_text"}:
            continue
        text = compact_whitespace(str(block.get("text") or ""))
        if text:
            parts.append(text)
    return compact_whitespace("\n".join(parts))


def _normalized_todo(value: Mapping[str, object] | None) -> list[dict[str, str]]:
    if not isinstance(value, Mapping):
        return []
    items = value.get("items")
    if not isinstance(items, (list, tuple)):
        return []
    result: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        status = compact_whitespace(str(item.get("status") or "pending")).lower()
        if status not in {"pending", "in_progress", "blocked"}:
            continue
        content = truncate_text(
            str(item.get("content") or item.get("title") or ""),
            240,
        )
        if content:
            result.append({"status": status, "content": content})
        if len(result) >= 8:
            break
    return result


def _normalized_task(value: Mapping[str, object] | None) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, object] = {}
    for source_key, target_key, maximum in (
        ("kind", "kind", 40),
        ("objective", "objective", 1_200),
        ("expectedOutput", "expectedOutput", 800),
        ("state", "state", 40),
    ):
        text = truncate_text(str(value.get(source_key) or ""), maximum)
        if text:
            result[target_key] = text
    criteria = _string_list(value.get("acceptanceCriteria"), 8)
    if criteria:
        result["acceptanceCriteria"] = [truncate_text(item, 300) for item in criteria]
    original_requirements = _string_list(value.get("originalRequirements"), 4)
    if original_requirements:
        result["originalRequirements"] = [
            truncate_text(item, 2_000) for item in original_requirements
        ]
    return result


def _tail_text(value: str, maximum: int) -> str:
    text = str(value or "").strip()
    if len(text) <= maximum:
        return text
    return "…" + text[-max(1, maximum - 1) :]


def _mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _string_list(value: object, limit: int) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value:
        text = compact_whitespace(str(item or ""))
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result
