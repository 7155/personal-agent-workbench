from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from typing import cast

from .memory_ingest import normalize_text
from .text_utils import compact_whitespace, now_ms, stable_text_hash


ACTIVE_RETRIEVAL_PROJECTION_VERSION = 1
AUTHORITATIVE_ATOM_STATUSES = frozenset({"active", "approved"})
AUTHORITATIVE_BOOK_STATUSES = frozenset({"active", "approved", "archived"})


def source_type_for_doc(doc_type: str) -> str:
    normalized = compact_whitespace(doc_type).lower()
    return {
        "atom": "atom",
        "book": "book",
        "phrase": "phrase",
        "item": "item",
        "timeline": "timeline",
    }.get(normalized, normalized or "unknown")


def source_revision(
    conn: sqlite3.Connection,
    *,
    source_type: str,
    source_id: str,
    fallback: int = 1,
) -> int:
    normalized_type = compact_whitespace(source_type).lower()
    normalized_id = compact_whitespace(source_id)
    if normalized_type == "timeline":
        row = conn.execute(
            "SELECT updated_at_ms FROM daily_activity_timelines WHERE timeline_id = ?",
            (normalized_id,),
        ).fetchone()
        return max(1, int(row[0] or 0)) if row is not None else max(1, int(fallback))
    row = conn.execute(
        """
        SELECT generation
        FROM memory_source_generations
        WHERE source_type = ? AND source_id = ?
        """,
        (normalized_type, normalized_id),
    ).fetchone()
    return max(1, int(row[0] or 1)) if row is not None else max(1, int(fallback))


def bump_source_revision(
    conn: sqlite3.Connection,
    *,
    source_type: str,
    source_id: str,
    timestamp: int | None = None,
    minimum_revision: int = 2,
) -> int:
    resolved_at = now_ms() if timestamp is None else max(0, int(timestamp))
    minimum = max(1, int(minimum_revision))
    conn.execute(
        """
        INSERT INTO memory_source_generations(
            source_type, source_id, generation, updated_at_ms
        ) VALUES (?, ?, ?, ?)
        ON CONFLICT(source_type, source_id) DO UPDATE SET
            generation = MAX(
                memory_source_generations.generation + 1,
                excluded.generation
            ),
            updated_at_ms = excluded.updated_at_ms
        """,
        (source_type, source_id, minimum, resolved_at),
    )
    return source_revision(
        conn,
        source_type=source_type,
        source_id=source_id,
    )


def authoritative_retrieval_doc(
    conn: sqlite3.Connection,
    doc: Mapping[str, object],
) -> bool:
    """Fail closed unless a projection still matches its authoritative source."""
    return authoritative_retrieval_docs(conn, [doc])[0]


def authoritative_retrieval_docs(
    conn: sqlite3.Connection,
    docs: Iterable[Mapping[str, object]],
) -> list[bool]:
    """Validate one candidate pack with narrow, set-based canonical reads.

    Results retain input order, including duplicates. No authority is cached:
    forget, supersession, generation and dependency changes are checked on the
    caller's current connection on every pack. This does not discover or trim
    candidates, nor begin/commit a transaction owned by the caller.
    """
    parsed: list[tuple[str, str, int] | None] = []
    ids: dict[str, set[str]] = {}
    for doc in docs:
        kind = compact_whitespace(str(doc.get("doc_type") or "")).lower()
        identity = compact_whitespace(str(doc.get("source_id") or ""))
        try:
            revision = max(1, int(cast(str | int | float, doc.get("source_revision") or 1)))
            version = max(1, int(cast(str | int | float, doc.get("projection_version") or 1)))
        except (TypeError, ValueError):
            parsed.append(None)
            continue
        if not identity or kind not in {"atom", "book", "phrase", "item", "timeline"} or version != ACTIVE_RETRIEVAL_PROJECTION_VERSION:
            parsed.append(None)
            continue
        parsed.append((kind, identity, revision))
        ids.setdefault(kind, set()).add(identity)
    atoms = _authority_rows(conn, "SELECT id, status, claim_state, privacy_level FROM memory_atoms", "id", ids.get("atom", set()))
    books = _authority_rows(conn, "SELECT book_id, status, metadata_json, memory_atom_ids_json FROM memory_books", "book_id", ids.get("book", set()))
    items = _authority_rows(conn, "SELECT memory_id, kind, status, privacy_class FROM memory_items", "memory_id", ids.get("phrase", set()) | ids.get("item", set()))
    timelines = _authority_rows(conn, "SELECT timeline_id, status, updated_at_ms FROM daily_activity_timelines", "timeline_id", ids.get("timeline", set()))
    book_atoms = {identity: _json_list(row[3]) for identity, row in books.items()}
    dependency_ids = {atom_id for members in book_atoms.values() for atom_id in members}
    missing_atoms = dependency_ids - atoms.keys()
    atoms.update(_authority_rows(conn, "SELECT id, status, claim_state, privacy_level FROM memory_atoms", "id", missing_atoms))
    current_atoms = {identity for identity, row in atoms.items() if str(row[1]) in AUTHORITATIVE_ATOM_STATUSES and str(row[2]) == "current" and str(row[3]) != "sensitive"}
    keys = [(source_type_for_doc(kind), identity) for kind, members in ids.items() if kind != "timeline" for identity in sorted(members)]
    generations = {
        (str(row[0]), str(row[1])): row[2]
        for row in conn.execute("""
            SELECT source_type, source_id, generation
            FROM memory_source_generations
            WHERE (source_type, source_id) IN (
                SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]')
                FROM json_each(?)
            )
            """, (json.dumps(keys),)).fetchall()
    } if keys else {}
    phrase_support = {
        str(row[0]): bool(row[1])
        for row in conn.execute("""
            SELECT dependency.dependent_id,
                   MAX(CASE WHEN atom.status IN ('active', 'approved')
                        AND atom.claim_state = 'current'
                        AND atom.privacy_level != 'sensitive'
                        AND generation.generation = dependency.source_revision
                       THEN 1 ELSE 0 END)
            FROM memory_projection_dependencies AS dependency
            LEFT JOIN memory_atoms AS atom ON atom.id = dependency.source_id
            LEFT JOIN memory_source_generations AS generation
              ON generation.source_type = 'atom' AND generation.source_id = atom.id
            WHERE dependency.source_type = 'atom'
              AND dependency.dependent_type = 'phrase'
              AND dependency.dependent_id IN (SELECT value FROM json_each(?))
            GROUP BY dependency.dependent_id
            """, (json.dumps(sorted(ids.get("phrase", set()))),)).fetchall()
    } if ids.get("phrase") else {}
    result: list[bool] = []
    for candidate in parsed:
        if candidate is None:
            result.append(False)
            continue
        kind, identity, revision = candidate
        if kind == "timeline":
            row = timelines.get(identity)
            result.append(bool(row is not None and str(row[1]) == "approved" and revision == max(1, int(row[2] or 0))))
            continue
        if kind == "atom":
            valid = identity in current_atoms
        elif kind == "book":
            row = books.get(identity)
            valid = bool(row is not None and str(row[1]) in AUTHORITATIVE_BOOK_STATUSES
                         and _json_object(row[2]).get("retrievalStale") is not True
                         and set(book_atoms[identity]).issubset(current_atoms))
        else:
            row = items.get(identity)
            valid = bool(row is not None and str(row[2]) in AUTHORITATIVE_ATOM_STATUSES
                         and str(row[3]) != "sensitive"
                         and (kind != "phrase" or str(row[1]) == "phrase")
                         and (kind != "phrase" or phrase_support.get(identity, True)))
        result.append(valid and revision == max(1, int(generations.get((source_type_for_doc(kind), identity)) or 1)))
    return result


def _authority_rows(
    conn: sqlite3.Connection,
    sql: str,
    identity_column: str,
    ids: Iterable[str],
) -> dict[str, sqlite3.Row]:
    identities = sorted(set(ids))
    if not identities:
        return {}
    # JSON1 is already a Memory dependency. One bound pack avoids SQLite's
    # variable limit without one query per document or dependency.
    return {str(row[0]): row for row in conn.execute(
        sql + f" WHERE {identity_column} IN (SELECT value FROM json_each(?))",
        (json.dumps(identities),),
    ).fetchall()}


def sync_projection_dependencies(
    conn: sqlite3.Connection,
    docs: Iterable[Mapping[str, object]],
    *,
    timestamp: int | None = None,
) -> None:
    resolved_at = now_ms() if timestamp is None else max(0, int(timestamp))
    for doc in docs:
        doc_id = compact_whitespace(str(doc.get("doc_id") or ""))
        source_id = compact_whitespace(str(doc.get("source_id") or ""))
        doc_type = compact_whitespace(str(doc.get("doc_type") or ""))
        if not doc_id or not source_id or not doc_type:
            continue
        revision = max(1, int(doc.get("source_revision") or 1))
        source_type = source_type_for_doc(doc_type)
        conn.execute(
            """
            INSERT INTO memory_projection_dependencies(
                source_type, source_id, dependent_type, dependent_id,
                source_revision, created_at_ms, updated_at_ms
            ) VALUES (?, ?, 'retrieval_doc', ?, ?, ?, ?)
            ON CONFLICT(source_type, source_id, dependent_type, dependent_id)
            DO UPDATE SET
                source_revision = excluded.source_revision,
                updated_at_ms = excluded.updated_at_ms
            """,
            (source_type, source_id, doc_id, revision, resolved_at, resolved_at),
        )
        if doc_type == "book":
            metadata = doc.get("metadata")
            atom_ids = (
                _string_list(metadata.get("memoryAtomIds"))
                if isinstance(metadata, Mapping)
                else []
            )
            conn.execute(
                """
                DELETE FROM memory_projection_dependencies
                WHERE dependent_type = 'book' AND dependent_id = ?
                """,
                (source_id,),
            )
            for atom_id in atom_ids:
                conn.execute(
                    """
                    INSERT INTO memory_projection_dependencies(
                        source_type, source_id, dependent_type, dependent_id,
                        source_revision, created_at_ms, updated_at_ms
                    ) VALUES ('atom', ?, 'book', ?, ?, ?, ?)
                    ON CONFLICT(source_type, source_id, dependent_type, dependent_id)
                    DO UPDATE SET
                        source_revision = excluded.source_revision,
                        updated_at_ms = excluded.updated_at_ms
                    """,
                    (
                        atom_id,
                        source_id,
                        source_revision(
                            conn,
                            source_type="atom",
                            source_id=atom_id,
                        ),
                        resolved_at,
                        resolved_at,
                    ),
                )
        elif doc_type == "phrase":
            metadata = doc.get("metadata")
            phrase_event_ids = (
                _positive_ints(metadata.get("sourceEventIds"))
                if isinstance(metadata, Mapping)
                else []
            )
            if isinstance(metadata, Mapping):
                phrase_event_ids.extend(
                    _positive_ints([metadata.get("sourceEventId")])
                )
            _sync_phrase_atom_dependencies(
                conn,
                phrase_id=source_id,
                source_event_ids=phrase_event_ids,
                timestamp=resolved_at,
            )


def delete_retrieval_docs(
    conn: sqlite3.Connection,
    doc_ids: Iterable[str],
) -> list[str]:
    ids = tuple(dict.fromkeys(compact_whitespace(value) for value in doc_ids if compact_whitespace(value)))
    if not ids:
        return []
    deleted: list[str] = []
    for offset in range(0, len(ids), 400):
        chunk = ids[offset : offset + 400]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT rowid, doc_id FROM memory_retrieval_docs WHERE doc_id IN ({placeholders})",
            chunk,
        ).fetchall()
        rowids = tuple(int(row[0]) for row in rows)
        deleted.extend(str(row[1]) for row in rows)
        if rowids:
            row_placeholders = ",".join("?" for _ in rowids)
            conn.execute(
                f"DELETE FROM memory_retrieval_docs_fts WHERE rowid IN ({row_placeholders})",
                rowids,
            )
        conn.execute(
            f"DELETE FROM memory_retrieval_doc_vectors WHERE doc_id IN ({placeholders})",
            chunk,
        )
        conn.execute(
            f"DELETE FROM memory_retrieval_docs WHERE doc_id IN ({placeholders})",
            chunk,
        )
        conn.execute(
            f"""DELETE FROM memory_projection_dependencies
                WHERE dependent_type = 'retrieval_doc'
                  AND dependent_id IN ({placeholders})""",
            chunk,
        )
    return sorted(set(deleted))


def invalidate_superseded_atom_dependencies(
    conn: sqlite3.Connection,
    old_atom_ids: Iterable[str],
    *,
    new_atom_id: str = "",
    timestamp: int | None = None,
) -> dict[str, object]:
    """Invalidate projections derived from superseded or revised Atom text.

    Books remain as auditable records, but are marked retrieval-stale and leave
    retrieval immediately. Semantic-group membership may move to a verified
    replacement Atom; the old Atom can never keep boosting current retrieval.
    An in-place revision passes its own ID as the replacement and keeps its
    existing group memberships while refreshing the dependent projections.
    """

    resolved_at = now_ms() if timestamp is None else max(0, int(timestamp))
    old_ids = tuple(
        dict.fromkeys(
            compact_whitespace(value)
            for value in old_atom_ids
            if compact_whitespace(value)
        )
    )
    replacement = compact_whitespace(new_atom_id)
    if replacement and not _all_current_atoms(conn, [replacement]):
        replacement = ""
    if not old_ids:
        return {
            "schemaVersion": "rag-ime.memory-projection-invalidation.v1",
            "oldAtomIds": [],
            "newAtomId": replacement,
            "staleBookIds": [],
            "suppressedPhraseIds": [],
            "removedDocIds": [],
            "movedGroupMemberships": 0,
            "previousBooks": [],
            "previousGroupMembers": [],
            "previousPhrases": [],
            "previousPhraseGroupMembers": [],
            "previousPhraseSuppressions": [],
        }

    placeholders = ",".join("?" for _ in old_ids)
    book_rows = conn.execute(
        f"""
        SELECT DISTINCT book.*
        FROM memory_books AS book
        JOIN json_each(book.memory_atom_ids_json) AS member
          ON CAST(member.value AS TEXT) IN ({placeholders})
        WHERE json_valid(book.memory_atom_ids_json)
        """,
        old_ids,
    ).fetchall()
    previous_books: list[dict[str, object]] = []
    stale_book_ids: list[str] = []
    rebuilt_book_ids: list[str] = []
    for row in book_rows:
        book_id = str(row["book_id"])
        metadata = _json_object(row["metadata_json"])
        stale_book_ids.append(book_id)
        status = str(row["status"] or "")
        member_ids = _json_list(row["memory_atom_ids_json"])
        if replacement and status in {"active", "approved"}:
            next_ids = list(
                dict.fromkeys(
                    replacement if atom_id in old_ids else atom_id
                    for atom_id in member_ids
                )
            )
            if next_ids:
                atom_placeholders = ",".join("?" for _ in next_ids)
                atom_rows = conn.execute(
                    f"""
                    SELECT id, canonical_text, text, source_event_ids_json
                    FROM memory_atoms
                    WHERE id IN ({atom_placeholders})
                      AND status IN ('active', 'approved')
                      AND claim_state = 'current'
                    """,
                    tuple(next_ids),
                ).fetchall()
            else:
                atom_rows = []
            text_by_id = {
                str(atom_row["id"]): compact_whitespace(
                    str(atom_row["canonical_text"] or atom_row["text"] or "")
                )
                for atom_row in atom_rows
            }
            current_ids = [atom_id for atom_id in next_ids if atom_id in text_by_id]
            source_event_ids = sorted({
                event_id
                for atom_row in atom_rows
                for event_id in _positive_ints(_json_list(atom_row["source_event_ids_json"]))
            })
            summary = "；".join(
                text_by_id[atom_id]
                for atom_id in current_ids
                if text_by_id[atom_id]
            )[:2400]
            previous_books.append({"bookId": book_id, "row": dict(row)})
            metadata.pop("retrievalStale", None)
            metadata.pop("retrievalStaleReason", None)
            metadata.pop("retrievalStaleAtMs", None)
            metadata.update(
                {
                    "projectionRebuiltForSupersession": True,
                    "projectionRebuiltAtMs": resolved_at,
                }
            )
            conn.execute(
                """
                UPDATE memory_books
                SET summary = ?, normalized_text = ?, memory_atom_ids_json = ?,
                    source_event_ids_json = ?,
                    tags_json = '[]', surface_hints_json = '[]',
                    query_expansions_json = '[]', metadata_json = ?,
                    updated_at_ms = ?, last_active_at_ms = ?
                WHERE book_id = ?
                """,
                (
                    summary,
                    normalize_text(f"{row['title']} {summary}"),
                    json.dumps(current_ids, ensure_ascii=False),
                    json.dumps(source_event_ids),
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    resolved_at,
                    resolved_at,
                    book_id,
                ),
            )
            bump_source_revision(
                conn,
                source_type="book",
                source_id=book_id,
                timestamp=resolved_at,
            )
            rebuilt_book_ids.append(book_id)
            continue
        if metadata.get("retrievalStale") is True:
            continue
        previous_books.append({"bookId": book_id, "row": dict(row)})
        metadata.update(
            {
                "retrievalStale": True,
                "retrievalStaleReason": "superseded_atom_dependency",
                "retrievalStaleAtMs": resolved_at,
            }
        )
        conn.execute(
            "UPDATE memory_books SET metadata_json = ? WHERE book_id = ?",
            (json.dumps(metadata, ensure_ascii=False, sort_keys=True), book_id),
        )
        bump_source_revision(
            conn,
            source_type="book",
            source_id=book_id,
            timestamp=resolved_at,
        )

    phrase_rows = conn.execute(
        f"""
        SELECT DISTINCT item.*
        FROM memory_projection_dependencies AS dependency
        JOIN memory_items AS item
          ON item.memory_id = dependency.dependent_id
        WHERE dependency.source_type = 'atom'
          AND dependency.source_id IN ({placeholders})
          AND dependency.dependent_type = 'phrase'
          AND item.kind = 'phrase'
          AND item.status IN ('active', 'approved')
        """,
        old_ids,
    ).fetchall()
    previous_phrases: list[dict[str, object]] = []
    previous_phrase_groups: list[dict[str, object]] = []
    previous_phrase_suppressions: list[dict[str, object]] = []
    suppressed_phrase_ids: list[str] = []
    for row in phrase_rows:
        phrase_id = str(row["memory_id"])
        current_support = conn.execute(
            f"""
            SELECT 1
            FROM memory_projection_dependencies AS dependency
            JOIN memory_atoms AS atom ON atom.id = dependency.source_id
            WHERE dependency.source_type = 'atom'
              AND dependency.dependent_type = 'phrase'
              AND dependency.dependent_id = ?
              AND dependency.source_id NOT IN ({placeholders})
              AND atom.status IN ('active', 'approved')
              AND atom.claim_state = 'current'
            LIMIT 1
            """,
            (phrase_id, *old_ids),
        ).fetchone()
        if current_support is not None:
            continue
        previous_phrases.append({"phraseId": phrase_id, "row": dict(row)})
        phrase_group_rows = conn.execute(
            """
            SELECT group_id, member_type, member_id, weight, source, updated_at_ms
            FROM memory_semantic_group_members
            WHERE member_type = 'phrase' AND member_id = ?
            """,
            (phrase_id,),
        ).fetchall()
        previous_phrase_groups.extend(dict(group_row) for group_row in phrase_group_rows)
        metadata = _json_object(row["metadata_json"])
        metadata.update(
            {
                "supersessionSuppressed": True,
                "supersessionSuppressedAtMs": resolved_at,
                "supersessionSuppressedReason": "superseded_atom_dependency",
            }
        )
        conn.execute(
            """
            UPDATE memory_items
            SET status = 'hidden', metadata_json = ?, updated_at_ms = ?
            WHERE memory_id = ? AND kind = 'phrase'
            """,
            (
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                resolved_at,
                phrase_id,
            ),
        )
        conn.execute(
            """
            DELETE FROM memory_semantic_group_members
            WHERE member_type = 'phrase' AND member_id = ?
            """,
            (phrase_id,),
        )
        suppression_token = stable_text_hash(phrase_id).removeprefix("sha256:")[:24]
        for suppression_id, match_type, match_value in (
            (f"supersession:phrase-id:{suppression_token}", "memory_id", phrase_id),
            (
                f"supersession:phrase-text:{suppression_token}",
                "text",
                compact_whitespace(str(row["text"] or "")),
            ),
        ):
            if not match_value:
                continue
            existing_suppression = conn.execute(
                "SELECT * FROM memory_candidate_suppressions WHERE id = ?",
                (suppression_id,),
            ).fetchone()
            previous_phrase_suppressions.append(
                {
                    "suppressionId": suppression_id,
                    "row": dict(existing_suppression) if existing_suppression is not None else {},
                }
            )
            conn.execute(
                """
                INSERT INTO memory_candidate_suppressions(
                    id, match_type, match_value, action, reason, strength,
                    expires_at_ms, created_at_ms
                ) VALUES (?, ?, ?, 'block', 'superseded_atom_dependency',
                          1.0, NULL, ?)
                ON CONFLICT(id) DO UPDATE SET
                    match_type = excluded.match_type,
                    match_value = excluded.match_value,
                    action = 'block',
                    reason = excluded.reason,
                    strength = 1.0,
                    expires_at_ms = NULL,
                    created_at_ms = excluded.created_at_ms
                """,
                (suppression_id, match_type, match_value, resolved_at),
            )
        suppressed_phrase_ids.append(phrase_id)

    group_rows = conn.execute(
        f"""
        SELECT group_id, member_type, member_id, weight, source, updated_at_ms
        FROM memory_semantic_group_members
        WHERE member_type = 'atom' AND member_id IN ({placeholders})
        """,
        old_ids,
    ).fetchall()
    group_ids = tuple(dict.fromkeys(str(row[0]) for row in group_rows))
    previous_group_members: list[dict[str, object]] = []
    if group_ids:
        group_placeholders = ",".join("?" for _ in group_ids)
        tracked_ids = (*old_ids, *((replacement,) if replacement else ()))
        tracked_placeholders = ",".join("?" for _ in tracked_ids)
        previous_group_members = [
            {
                "groupId": str(row[0]),
                "memberType": str(row[1]),
                "memberId": str(row[2]),
                "weight": float(row[3]),
                "source": str(row[4]),
                "updatedAtMs": int(row[5]),
            }
            for row in conn.execute(
                f"""
                SELECT group_id, member_type, member_id, weight, source, updated_at_ms
                FROM memory_semantic_group_members
                WHERE group_id IN ({group_placeholders})
                  AND member_type = 'atom'
                  AND member_id IN ({tracked_placeholders})
                """,
                (*group_ids, *tracked_ids),
            ).fetchall()
        ]
    moved = 0
    if replacement:
        for row in group_rows:
            if str(row[2]) == replacement:
                continue
            conn.execute(
                """
                INSERT INTO memory_semantic_group_members(
                    group_id, member_type, member_id, weight, source, updated_at_ms
                ) VALUES (?, 'atom', ?, ?, 'supersession', ?)
                ON CONFLICT(group_id, member_type, member_id) DO UPDATE SET
                    weight = MAX(memory_semantic_group_members.weight, excluded.weight),
                    updated_at_ms = excluded.updated_at_ms
                """,
                (str(row[0]), replacement, float(row[3]), resolved_at),
            )
            moved += 1
    conn.execute(
        f"""
        DELETE FROM memory_semantic_group_members
        WHERE member_type = 'atom' AND member_id IN ({placeholders})
          AND member_id <> ?
        """,
        (*old_ids, replacement),
    )

    dependent_doc_ids = [f"atom:{atom_id}" for atom_id in old_ids]
    dependent_doc_ids.extend(f"book:{book_id}" for book_id in stale_book_ids)
    dependent_doc_ids.extend(f"phrase:{phrase_id}" for phrase_id in suppressed_phrase_ids)
    dependency_rows = conn.execute(
        f"""
        SELECT dependent_id
        FROM memory_projection_dependencies
        WHERE source_type = 'atom'
          AND source_id IN ({placeholders})
          AND dependent_type = 'retrieval_doc'
        """,
        old_ids,
    ).fetchall()
    dependent_doc_ids.extend(str(row[0]) for row in dependency_rows)
    removed_docs = delete_retrieval_docs(conn, dependent_doc_ids)
    conn.execute(
        f"""
        DELETE FROM memory_projection_dependencies
        WHERE source_type = 'atom' AND source_id IN ({placeholders})
        """,
        old_ids,
    )
    return {
        "schemaVersion": "rag-ime.memory-projection-invalidation.v1",
        "oldAtomIds": list(old_ids),
        "newAtomId": replacement,
        "staleBookIds": sorted(stale_book_ids),
        "rebuiltBookIds": sorted(rebuilt_book_ids),
        "suppressedPhraseIds": sorted(suppressed_phrase_ids),
        "removedDocIds": removed_docs,
        "movedGroupMemberships": moved,
        "previousBooks": previous_books,
        "previousGroupMembers": previous_group_members,
        "previousPhrases": previous_phrases,
        "previousPhraseGroupMembers": previous_phrase_groups,
        "previousPhraseSuppressions": previous_phrase_suppressions,
        "affectedGroupIds": list(group_ids),
    }


def restore_dependency_invalidation(
    conn: sqlite3.Connection,
    rollback: Mapping[str, object],
    *,
    timestamp: int | None = None,
) -> None:
    resolved_at = now_ms() if timestamp is None else max(0, int(timestamp))
    for item in rollback.get("previousBooks") or []:
        if not isinstance(item, Mapping):
            continue
        book_id = compact_whitespace(str(item.get("bookId") or ""))
        if not book_id:
            continue
        previous_row = item.get("row")
        if isinstance(previous_row, Mapping) and previous_row:
            columns = list(previous_row)
            conn.execute(
                f"""INSERT OR REPLACE INTO memory_books(
                    {', '.join(columns)}
                ) VALUES ({', '.join('?' for _ in columns)})""",
                [previous_row[column] for column in columns],
            )
        else:
            conn.execute(
                "UPDATE memory_books SET metadata_json = ? WHERE book_id = ?",
                (str(item.get("metadataJson") or "{}"), book_id),
            )
        bump_source_revision(
            conn,
            source_type="book",
            source_id=book_id,
            timestamp=resolved_at,
        )
    for item in rollback.get("previousPhrases") or []:
        if not isinstance(item, Mapping):
            continue
        previous_row = item.get("row")
        if not isinstance(previous_row, Mapping) or not previous_row:
            continue
        columns = list(previous_row)
        conn.execute(
            f"""INSERT OR REPLACE INTO memory_items(
                {', '.join(columns)}
            ) VALUES ({', '.join('?' for _ in columns)})""",
            [previous_row[column] for column in columns],
        )
    for item in rollback.get("previousPhraseSuppressions") or []:
        if not isinstance(item, Mapping):
            continue
        suppression_id = compact_whitespace(str(item.get("suppressionId") or ""))
        previous_row = item.get("row")
        if isinstance(previous_row, Mapping) and previous_row:
            columns = list(previous_row)
            conn.execute(
                f"""INSERT OR REPLACE INTO memory_candidate_suppressions(
                    {', '.join(columns)}
                ) VALUES ({', '.join('?' for _ in columns)})""",
                [previous_row[column] for column in columns],
            )
        elif suppression_id:
            conn.execute(
                "DELETE FROM memory_candidate_suppressions WHERE id = ?",
                (suppression_id,),
            )
    group_ids = _string_list(rollback.get("affectedGroupIds"))
    atom_ids = _string_list(rollback.get("oldAtomIds"))
    replacement = compact_whitespace(str(rollback.get("newAtomId") or ""))
    if replacement:
        atom_ids.append(replacement)
    if group_ids and atom_ids:
        group_placeholders = ",".join("?" for _ in group_ids)
        atom_placeholders = ",".join("?" for _ in atom_ids)
        conn.execute(
            f"""
            DELETE FROM memory_semantic_group_members
            WHERE group_id IN ({group_placeholders})
              AND member_type = 'atom'
              AND member_id IN ({atom_placeholders})
            """,
            (*group_ids, *atom_ids),
        )
    for item in rollback.get("previousGroupMembers") or []:
        if not isinstance(item, Mapping):
            continue
        conn.execute(
            """
            INSERT OR REPLACE INTO memory_semantic_group_members(
                group_id, member_type, member_id, weight, source, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(item.get("groupId") or ""),
                str(item.get("memberType") or "atom"),
                str(item.get("memberId") or ""),
                float(item.get("weight") or 0.8),
                str(item.get("source") or "rollback"),
                int(item.get("updatedAtMs") or resolved_at),
            ),
        )
    for item in rollback.get("previousPhraseGroupMembers") or []:
        if not isinstance(item, Mapping):
            continue
        conn.execute(
            """
            INSERT OR REPLACE INTO memory_semantic_group_members(
                group_id, member_type, member_id, weight, source, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(item.get("group_id") or ""),
                str(item.get("member_type") or "phrase"),
                str(item.get("member_id") or ""),
                float(item.get("weight") or 0.8),
                str(item.get("source") or "rollback"),
                int(item.get("updated_at_ms") or resolved_at),
            ),
        )


def repair_superseded_memory_residuals(
    conn: sqlite3.Connection,
    *,
    dry_run: bool = False,
    timestamp: int | None = None,
) -> dict[str, object]:
    """Clean legacy Book/Group projections that still depend on old Atoms."""

    rows = conn.execute(
        """
        SELECT id
        FROM memory_atoms
        WHERE status NOT IN ('active', 'approved') OR claim_state != 'current'
        ORDER BY id
        """
    ).fetchall()
    old_ids = [str(row[0]) for row in rows]
    if not old_ids:
        return {
            "schemaVersion": "rag-ime.memory-projection-repair.v1",
            "noncurrentAtoms": 0,
            "staleBooks": 0,
            "removedDocs": 0,
            "removedGroupMemberships": 0,
            "movedGroupMemberships": 0,
            "suppressedPhrases": 0,
            "dryRun": bool(dry_run),
        }
    placeholders = ",".join("?" for _ in old_ids)
    stale_books = int(
        conn.execute(
            f"""
            SELECT COUNT(DISTINCT book.book_id)
            FROM memory_books AS book
            JOIN json_each(book.memory_atom_ids_json) AS member
              ON CAST(member.value AS TEXT) IN ({placeholders})
            WHERE json_valid(book.memory_atom_ids_json)
            """,
            old_ids,
        ).fetchone()[0]
    )
    group_members = int(
        conn.execute(
            f"""
            SELECT COUNT(*)
            FROM memory_semantic_group_members
            WHERE member_type = 'atom' AND member_id IN ({placeholders})
            """,
            old_ids,
        ).fetchone()[0]
    )
    if dry_run:
        return {
            "schemaVersion": "rag-ime.memory-projection-repair.v1",
            "noncurrentAtoms": len(old_ids),
            "staleBooks": stale_books,
            "removedDocs": 0,
            "removedGroupMemberships": group_members,
            "movedGroupMemberships": 0,
            "suppressedPhrases": 0,
            "dryRun": True,
        }

    replacement_map = {
        old_id: _latest_current_replacement(conn, old_id)
        for old_id in old_ids
    }
    aggregate_removed: set[str] = set()
    moved = 0
    suppressed_phrases: set[str] = set()
    for replacement in sorted(set(replacement_map.values())):
        grouped = [old_id for old_id, new_id in replacement_map.items() if new_id == replacement]
        result = invalidate_superseded_atom_dependencies(
            conn,
            grouped,
            new_atom_id=replacement,
            timestamp=timestamp,
        )
        aggregate_removed.update(_string_list(result.get("removedDocIds")))
        moved += int(result.get("movedGroupMemberships") or 0)
        suppressed_phrases.update(_string_list(result.get("suppressedPhraseIds")))
    return {
        "schemaVersion": "rag-ime.memory-projection-repair.v1",
        "noncurrentAtoms": len(old_ids),
        "staleBooks": stale_books,
        "removedDocs": len(aggregate_removed),
        "removedGroupMemberships": group_members,
        "movedGroupMemberships": moved,
        "suppressedPhrases": len(suppressed_phrases),
        "dryRun": False,
    }


def _latest_current_replacement(conn: sqlite3.Connection, old_atom_id: str) -> str:
    row = conn.execute(
        """
        SELECT edge.new_memory_id
        FROM memory_supersessions AS edge
        JOIN memory_atoms AS atom ON atom.id = edge.new_memory_id
        WHERE edge.old_memory_id = ?
          AND edge.status = 'active'
          AND atom.status IN ('active', 'approved')
          AND atom.claim_state = 'current'
        ORDER BY edge.created_at_ms DESC
        LIMIT 1
        """,
        (old_atom_id,),
    ).fetchone()
    return str(row[0]) if row is not None else ""


def _all_current_atoms(conn: sqlite3.Connection, atom_ids: Iterable[str]) -> bool:
    ids = tuple(dict.fromkeys(compact_whitespace(value) for value in atom_ids if compact_whitespace(value)))
    if not ids:
        return True
    placeholders = ",".join("?" for _ in ids)
    count = int(
        conn.execute(
            f"""
            SELECT COUNT(*)
            FROM memory_atoms
            WHERE id IN ({placeholders})
              AND status IN ('active', 'approved')
              AND claim_state = 'current'
              AND privacy_level != 'sensitive'
            """,
            ids,
        ).fetchone()[0]
    )
    return count == len(ids)


def _sync_phrase_atom_dependencies(
    conn: sqlite3.Connection,
    *,
    phrase_id: str,
    source_event_ids: Iterable[int],
    timestamp: int,
) -> None:
    conn.execute(
        """
        DELETE FROM memory_projection_dependencies
        WHERE source_type = 'atom'
          AND dependent_type = 'phrase'
          AND dependent_id = ?
        """,
        (phrase_id,),
    )
    event_ids = tuple(dict.fromkeys(int(value) for value in source_event_ids if int(value) > 0))
    if not event_ids:
        return
    placeholders = ",".join("?" for _ in event_ids)
    atom_rows = conn.execute(
        f"""
        SELECT DISTINCT atom.id
        FROM memory_atoms AS atom
        JOIN json_each(
            CASE
                WHEN json_valid(atom.source_event_ids_json)
                THEN atom.source_event_ids_json
                ELSE '[]'
            END
        ) AS event
        WHERE CAST(event.value AS INTEGER) IN ({placeholders})
        """,
        event_ids,
    ).fetchall()
    for row in atom_rows:
        atom_id = str(row[0])
        conn.execute(
            """
            INSERT INTO memory_projection_dependencies(
                source_type, source_id, dependent_type, dependent_id,
                source_revision, created_at_ms, updated_at_ms
            ) VALUES ('atom', ?, 'phrase', ?, ?, ?, ?)
            ON CONFLICT(source_type, source_id, dependent_type, dependent_id)
            DO UPDATE SET
                source_revision = excluded.source_revision,
                updated_at_ms = excluded.updated_at_ms
            """,
            (
                atom_id,
                phrase_id,
                source_revision(conn, source_type="atom", source_id=atom_id),
                timestamp,
                timestamp,
            ),
        )


def _json_object(raw: object) -> dict[str, object]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _json_list(raw: object) -> list[str]:
    try:
        value = json.loads(str(raw or "[]"))
    except (TypeError, json.JSONDecodeError):
        return []
    return _string_list(value)


def _string_list(raw: object) -> list[str]:
    if not isinstance(raw, (list, tuple, set)):
        return []
    return list(
        dict.fromkeys(
            compact_whitespace(str(value))
            for value in raw
            if compact_whitespace(str(value))
        )
    )


def _positive_ints(raw: object) -> list[int]:
    if not isinstance(raw, (list, tuple, set)):
        return []
    values: list[int] = []
    for item in raw:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in values:
            values.append(number)
    return values
