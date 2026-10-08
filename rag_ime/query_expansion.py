from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from difflib import SequenceMatcher

from .memory_ingest import normalize_text
from .memory_ownership import resolve_visible_memory_owners, sql_memory_owner_predicate
from .memory_projection_consistency import authoritative_retrieval_docs
from .text_utils import compact_whitespace, token_terms


@dataclass(frozen=True)
class QueryExpansion:
    primary_query: str
    lexical_terms: tuple[str, ...]
    matched_aliases: tuple[str, ...]
    activated_tags: tuple[str, ...]
    negative_tags: tuple[str, ...]
    expansion_terms: tuple[str, ...]


_NEGATIVE_FEEDBACK_ACTIONS = {
    "backspace_after_accept",
    "edited_after_accept",
    "ignored_repeatedly",
    "session_invalidated",
    "skipped",
    "skip",
    "rejected",
}


def build_query_expansion(
    conn: sqlite3.Connection,
    *,
    query_text: str,
    raw_input: str = "",
    preedit: str = "",
    rime_candidates: tuple[str, ...] = (),
    committed_tail: str = "",
    project: str = "",
    app: str = "",
    visible_owners: tuple[tuple[str, str], ...] = (),
) -> QueryExpansion:
    resolved_owners = resolve_visible_memory_owners(visible_owners, project=project)
    primary = _primary_query(
        query_text=query_text,
        raw_input=raw_input,
        preedit=preedit,
        rime_candidates=rime_candidates,
        committed_tail=committed_tail,
    )
    lexical_terms = _unique(
        token_terms(" ".join([primary, raw_input, preedit, committed_tail, *rime_candidates]), max_terms=32)
    )
    matched_aliases, atom_ids = _matched_aliases_and_atoms(
        conn,
        query_terms=(primary, *lexical_terms, *rime_candidates),
        visible_owners=resolved_owners,
    )
    activated_tags = _activated_tags(
        conn,
        query_terms=(primary, *lexical_terms, *rime_candidates, *matched_aliases),
        atom_ids=atom_ids,
        project=project,
        app=app,
        visible_owners=resolved_owners,
    )
    negative_tags = _negative_feedback_tags(
        conn,
        project=project,
        app=app,
        visible_owners=resolved_owners,
    )
    expansion_terms = _expansion_terms(
        conn,
        query_terms=(primary, *lexical_terms, *rime_candidates),
        atom_ids=atom_ids,
        activated_tags=activated_tags,
        negative_tags=negative_tags,
        project=project,
        app=app,
        visible_owners=resolved_owners,
    )
    return QueryExpansion(
        primary_query=primary,
        lexical_terms=tuple(lexical_terms),
        matched_aliases=tuple(matched_aliases),
        activated_tags=tuple(tag for tag in activated_tags if tag not in set(negative_tags)),
        negative_tags=tuple(negative_tags),
        expansion_terms=tuple(term for term in expansion_terms if term not in set(negative_tags)),
    )


def _primary_query(
    *,
    query_text: str,
    raw_input: str,
    preedit: str,
    rime_candidates: tuple[str, ...],
    committed_tail: str,
) -> str:
    for value in (query_text, rime_candidates[0] if rime_candidates else "", preedit, raw_input, committed_tail):
        compact = compact_whitespace(value)
        if compact:
            return compact
    return ""


def _matched_aliases_and_atoms(
    conn: sqlite3.Connection,
    *,
    query_terms: tuple[str, ...],
    visible_owners: tuple[tuple[str, str], ...],
) -> tuple[list[str], list[str]]:
    owner_clause, owner_params = sql_memory_owner_predicate(
        visible_owners,
        table_alias="ma",
    )
    aliases = conn.execute(
        f"""
        SELECT a.memory_atom_id, a.alias, a.alias_type
        FROM memory_aliases AS a
        JOIN memory_atoms AS ma ON ma.id = a.memory_atom_id
        WHERE ma.status IN ('active', 'approved')
          AND ma.claim_state = 'current'
          AND ma.privacy_level != 'sensitive'
          AND {owner_clause}
        ORDER BY a.weight DESC, a.created_at_ms ASC
        """,
        owner_params,
    ).fetchall()
    matched_atom_ids: list[str] = []
    matched_aliases: list[str] = []
    for row in aliases:
        alias = compact_whitespace(str(row["alias"] or ""))
        if not alias:
            continue
        if any(_alias_matches_query(alias, term) for term in query_terms if compact_whitespace(term)):
            atom_id = str(row["memory_atom_id"] or "")
            if atom_id and atom_id not in matched_atom_ids:
                matched_atom_ids.append(atom_id)
    if matched_atom_ids:
        placeholders = ", ".join("?" for _ in matched_atom_ids)
        rows = conn.execute(
            f"""
            SELECT alias
            FROM memory_aliases
            WHERE memory_atom_id IN ({placeholders})
            ORDER BY weight DESC, created_at_ms ASC
            """,
            tuple(matched_atom_ids),
        ).fetchall()
        matched_aliases = _unique(str(row["alias"]) for row in rows if compact_whitespace(str(row["alias"])))
    return matched_aliases, matched_atom_ids


def _alias_matches_query(alias: str, query: str) -> bool:
    alias_norm = normalize_text(alias)
    query_norm = normalize_text(query)
    if not alias_norm or not query_norm:
        return False
    if alias_norm in query_norm or query_norm in alias_norm:
        return True
    if len(alias_norm) >= 3 and len(query_norm) >= 3:
        return SequenceMatcher(None, alias_norm, query_norm).ratio() >= 0.66
    return False


def _activated_tags(
    conn: sqlite3.Connection,
    *,
    query_terms: tuple[str, ...],
    atom_ids: list[str],
    project: str,
    app: str,
    visible_owners: tuple[tuple[str, str], ...],
) -> list[str]:
    tag_ids: list[int] = []
    tags: list[str] = []
    for tag_id, tag in _direct_tag_matches(
        conn,
        query_terms=query_terms,
        visible_owners=visible_owners,
    ):
        if tag_id not in tag_ids:
            tag_ids.append(tag_id)
        if tag not in tags:
            tags.append(tag)
    for tag_id, tag in _atom_tags(conn, atom_ids=atom_ids):
        if tag_id not in tag_ids:
            tag_ids.append(tag_id)
        if tag not in tags:
            tags.append(tag)
    for tag in _retrieval_doc_tags(
        conn,
        query_terms=query_terms,
        project=project,
        app=app,
        visible_owners=visible_owners,
    ):
        if tag not in tags:
            tags.append(tag)
    for tag_id, tag in _neighbor_tags(
        conn,
        seed_tag_ids=tag_ids,
        visible_owners=visible_owners,
    ):
        if tag not in tags:
            tags.append(tag)
    return tags[:32]


def _direct_tag_matches(
    conn: sqlite3.Connection,
    *,
    query_terms: tuple[str, ...],
    visible_owners: tuple[tuple[str, str], ...],
) -> list[tuple[int, str]]:
    matches: list[tuple[int, str]] = []
    item_owner_clause, item_owner_params = sql_memory_owner_predicate(
        visible_owners,
        table_alias="mi",
    )
    atom_owner_clause, atom_owner_params = sql_memory_owner_predicate(
        visible_owners,
        table_alias="ma",
    )
    rows = conn.execute(
        f"""
        SELECT id, tag, normalized_tag
        FROM memory_tags AS t
        WHERE t.status = 'active' AND t.source IN ('dsv4', 'user')
          AND (
              EXISTS (
                  SELECT 1
                  FROM memory_item_tags AS mit
                  JOIN memory_items AS mi ON mi.id = mit.memory_item_id
                  WHERE mit.tag_id = t.id
                    AND mi.status IN ('active', 'approved')
                    AND {item_owner_clause}
              )
              OR EXISTS (
                  SELECT 1
                  FROM memory_atom_tags AS mat
                  JOIN memory_atoms AS ma ON ma.id = mat.memory_atom_id
                  WHERE CAST(mat.tag_id AS TEXT) = CAST(t.id AS TEXT)
                    AND ma.status IN ('active', 'approved')
                    AND ma.claim_state = 'current'
                    AND ma.privacy_level != 'sensitive'
                    AND {atom_owner_clause}
              )
          )
        ORDER BY quality_score DESC, id ASC
        """,
        (*item_owner_params, *atom_owner_params),
    ).fetchall()
    for row in rows:
        tag = str(row["tag"] or "")
        normalized = str(row["normalized_tag"] or normalize_text(tag))
        for term in query_terms:
            term_norm = normalize_text(term)
            if not term_norm:
                continue
            if normalized == term_norm or normalized in term_norm or term_norm in normalized:
                matches.append((int(row["id"]), tag))
                break
    return matches


def _atom_tags(conn: sqlite3.Connection, *, atom_ids: list[str]) -> list[tuple[int, str]]:
    if not atom_ids:
        return []
    placeholders = ", ".join("?" for _ in atom_ids)
    return [
        (int(row["id"]), str(row["tag"]))
        for row in conn.execute(
            f"""
            SELECT t.id, t.tag
            FROM memory_atom_tags at
            JOIN memory_tags t ON CAST(t.id AS TEXT) = CAST(at.tag_id AS TEXT)
            WHERE at.memory_atom_id IN ({placeholders})
              AND t.status = 'active'
              AND t.source IN ('dsv4', 'user')
            ORDER BY at.weight DESC, t.quality_score DESC
            """,
            tuple(atom_ids),
        ).fetchall()
    ]


def _retrieval_doc_tags(
    conn: sqlite3.Connection,
    *,
    query_terms: tuple[str, ...],
    project: str,
    app: str,
    visible_owners: tuple[tuple[str, str], ...],
) -> list[str]:
    owner_clause, owner_params = sql_memory_owner_predicate(
        visible_owners,
        table_alias="memory_retrieval_docs",
    )
    rows = conn.execute(
        f"""
        SELECT doc_type, source_id, source_revision, projection_version,
               raw_text, tags_text, aliases_text, surface_hints_text,
               query_expansions_text, project, app
        FROM memory_retrieval_docs
        WHERE status = 'active'
          AND (? = '' OR project = ? OR project = '')
          AND (? = '' OR app = ? OR app = '')
          AND {owner_clause}
        LIMIT 200
        """,
        (project, project, app, app, *owner_params),
    ).fetchall()
    tags: list[str] = []
    matching = []
    for row in rows:
        haystack = " ".join(
            str(row[key] or "")
            for key in ("raw_text", "tags_text", "aliases_text", "surface_hints_text", "query_expansions_text")
        )
        if not any(_term_hits_text(term, haystack) for term in query_terms):
            continue
        matching.append(row)
    authority = authoritative_retrieval_docs(conn, (dict(row) for row in matching))
    for row, valid in zip(matching, authority):
        if not valid:
            continue
        for tag in compact_whitespace(str(row["tags_text"] or "")).split():
            if tag and tag not in tags:
                tags.append(tag)
    return tags


def _neighbor_tags(
    conn: sqlite3.Connection,
    *,
    seed_tag_ids: list[int],
    visible_owners: tuple[tuple[str, str], ...],
) -> list[tuple[int, str]]:
    if not seed_tag_ids:
        return []
    placeholders = ", ".join("?" for _ in seed_tag_ids)
    item_owner_clause, item_owner_params = sql_memory_owner_predicate(
        visible_owners,
        table_alias="mi",
    )
    atom_owner_clause, atom_owner_params = sql_memory_owner_predicate(
        visible_owners,
        table_alias="ma",
    )
    return [
        (int(row["id"]), str(row["tag"]))
        for row in conn.execute(
            f"""
            SELECT t.id, t.tag, MAX(e.weight) AS weight
            FROM memory_tag_edges e
            JOIN memory_tags t ON t.id = e.dst_tag_id
            WHERE e.src_tag_id IN ({placeholders})
              AND t.status = 'active'
              AND t.source IN ('dsv4', 'user')
              AND (
                  EXISTS (
                      SELECT 1
                      FROM memory_item_tags AS mit
                      JOIN memory_items AS mi ON mi.id = mit.memory_item_id
                      WHERE mit.tag_id = t.id
                        AND mi.status IN ('active', 'approved')
                        AND {item_owner_clause}
                  )
                  OR EXISTS (
                      SELECT 1
                      FROM memory_atom_tags AS mat
                      JOIN memory_atoms AS ma ON ma.id = mat.memory_atom_id
                      WHERE CAST(mat.tag_id AS TEXT) = CAST(t.id AS TEXT)
                        AND ma.status IN ('active', 'approved')
                        AND ma.claim_state = 'current'
                        AND ma.privacy_level != 'sensitive'
                        AND {atom_owner_clause}
                  )
              )
            GROUP BY t.id, t.tag
            ORDER BY weight DESC, t.quality_score DESC
            LIMIT 16
            """,
            (*seed_tag_ids, *item_owner_params, *atom_owner_params),
        ).fetchall()
    ]


def _negative_feedback_tags(
    conn: sqlite3.Connection,
    *,
    project: str,
    app: str,
    visible_owners: tuple[tuple[str, str], ...],
) -> list[str]:
    rows = conn.execute(
        """
        SELECT cf.memory_id, cf.candidate_text
        FROM candidate_feedback cf
        WHERE cf.action IN ({})
          AND (? = '' OR cf.project = ? OR cf.project = '')
          AND (? = '' OR cf.app = ? OR cf.app = '')
        ORDER BY cf.created_at_ms DESC
        LIMIT 50
        """.format(", ".join("?" for _ in _NEGATIVE_FEEDBACK_ACTIONS)),
        (*sorted(_NEGATIVE_FEEDBACK_ACTIONS), project, project, app, app),
    ).fetchall()
    tags: list[str] = []
    item_owner_clause, item_owner_params = sql_memory_owner_predicate(
        visible_owners,
        table_alias="mi",
    )
    atom_owner_clause, atom_owner_params = sql_memory_owner_predicate(
        visible_owners,
        table_alias="ma",
    )
    for row in rows:
        memory_id = compact_whitespace(str(row["memory_id"] or ""))
        if not memory_id:
            continue
        tag_rows = conn.execute(
            f"""
            SELECT tag
            FROM (
                SELECT t.tag AS tag, mit.weight AS weight
                FROM memory_items mi
                JOIN memory_item_tags mit ON mit.memory_item_id = mi.id
                JOIN memory_tags t ON t.id = mit.tag_id
                WHERE mi.memory_id = ?
                  AND mi.status IN ('active', 'approved')
                  AND {item_owner_clause}
                  AND t.status = 'active'
                  AND t.source IN ('dsv4', 'user')

                UNION ALL

                SELECT t.tag AS tag, mat.weight AS weight
                FROM memory_atom_tags mat
                JOIN memory_atoms ma ON ma.id = mat.memory_atom_id
                JOIN memory_tags t ON CAST(t.id AS TEXT) = CAST(mat.tag_id AS TEXT)
                WHERE mat.memory_atom_id = ?
                  AND ma.status IN ('active', 'approved')
                  AND ma.claim_state = 'current'
                  AND ma.privacy_level != 'sensitive'
                  AND {atom_owner_clause}
                  AND t.status = 'active'
                  AND t.source IN ('dsv4', 'user')
            ) governed_tags
            ORDER BY weight DESC
            """,
            (
                memory_id,
                *item_owner_params,
                memory_id,
                *atom_owner_params,
            ),
        ).fetchall()
        for tag_row in tag_rows:
            tag = str(tag_row["tag"] or "")
            if tag and tag not in tags:
                tags.append(tag)
    return tags


def _expansion_terms(
    conn: sqlite3.Connection,
    *,
    query_terms: tuple[str, ...],
    atom_ids: list[str],
    activated_tags: list[str],
    negative_tags: list[str],
    project: str,
    app: str,
    visible_owners: tuple[tuple[str, str], ...],
) -> list[str]:
    terms: list[str] = []
    negative = set(negative_tags)
    if atom_ids:
        placeholders = ", ".join("?" for _ in atom_ids)
        rows = conn.execute(
            f"""
            SELECT alias, alias_type
            FROM memory_aliases
            WHERE memory_atom_id IN ({placeholders})
              AND alias_type IN ('alias', 'query_expansion', 'surface_hint')
            ORDER BY weight DESC, created_at_ms ASC
            """,
            tuple(atom_ids),
        ).fetchall()
        for row in rows:
            alias = str(row["alias"] or "")
            if alias and alias not in negative:
                terms.append(alias)
    for tag in activated_tags:
        if tag not in negative:
            terms.append(tag)
    for tag in _retrieval_doc_expansions(
        conn,
        query_terms=query_terms,
        project=project,
        app=app,
        visible_owners=visible_owners,
    ):
        if tag not in negative:
            terms.append(tag)
    return _unique(terms)[:32]


def _retrieval_doc_expansions(
    conn: sqlite3.Connection,
    *,
    query_terms: tuple[str, ...],
    project: str,
    app: str,
    visible_owners: tuple[tuple[str, str], ...],
) -> list[str]:
    owner_clause, owner_params = sql_memory_owner_predicate(
        visible_owners,
        table_alias="memory_retrieval_docs",
    )
    rows = conn.execute(
        f"""
        SELECT doc_type, source_id, source_revision, projection_version,
               query_expansions_text, surface_hints_text
        FROM memory_retrieval_docs
        WHERE status = 'active'
          AND (? = '' OR project = ? OR project = '')
          AND (? = '' OR app = ? OR app = '')
          AND {owner_clause}
        LIMIT 200
        """,
        (project, project, app, app, *owner_params),
    ).fetchall()
    result: list[str] = []
    matching = []
    for row in rows:
        haystack = f"{row['query_expansions_text'] or ''} {row['surface_hints_text'] or ''}"
        if not any(_term_hits_text(term, haystack) for term in query_terms):
            continue
        matching.append(row)
    authority = authoritative_retrieval_docs(conn, (dict(row) for row in matching))
    for row, valid in zip(matching, authority):
        if not valid:
            continue
        haystack = f"{row['query_expansions_text'] or ''} {row['surface_hints_text'] or ''}"
        result.extend(compact_whitespace(haystack).split())
    return result


def _term_hits_text(term: str, text: str) -> bool:
    term_norm = normalize_text(term)
    text_norm = normalize_text(text)
    return bool(term_norm and (term_norm in text_norm or text_norm in term_norm))


def _unique(values) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = compact_whitespace(str(value))
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
