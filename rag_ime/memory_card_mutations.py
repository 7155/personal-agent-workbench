"""Versioned card writes shared by explicit edits and governed corrections.

Callers own authorization and the outer transaction. This owner preserves the
complete stored authority tuple and never rewrites a historical card's fact.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from .memory_evidence_admission import admitted_personal_evidence_sql, transition_evidence_admission
from .memory_evidence_ledger import checkpoint_input_event_evidence
from .memory_projection_consistency import invalidate_superseded_atom_dependencies
from .sensitive_content import contains_sensitive_content
from .text_utils import compact_whitespace


class MemoryRevisionConflict(ValueError):
    def __init__(self, code: str, current: Mapping[str, object] | None = None):
        super().__init__(f"{code}: memory changed; reload before saving")
        self.code = code
        self.current = dict(current or {})


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def card_revision(row: Mapping[str, object]) -> str:
    # Retrieval usage and maintenance touches are not edits. Authority, content, lineage and source
    # fields are covered, including fields added by future append-only migrations.
    return digest({key: value for key, value in dict(row).items() if key not in {"last_used_at_ms", "updated_at_ms"}})


@contextmanager
def atomic_memory_write(conn: sqlite3.Connection) -> Iterator[None]:
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    else:
        conn.execute("UPDATE memory_card_mutation_receipts SET created_at_ms=created_at_ms WHERE 0")
    conn.execute("SAVEPOINT memory_card_write")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK TO memory_card_write")
        conn.execute("RELEASE memory_card_write")
        raise
    else:
        conn.execute("RELEASE memory_card_write")


def replay_receipt(conn: sqlite3.Connection, request_id: object, request: object) -> dict[str, object] | None:
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 160:
        raise ValueError("clientRequestId is required (maximum 160 characters)")
    row = conn.execute("SELECT * FROM memory_card_mutation_receipts WHERE client_request_id=?", (request_id,)).fetchone()
    if row is None:
        return None
    if row["request_sha256"] != digest(request):
        raise MemoryRevisionConflict("memory_request_conflict")
    return json.loads(row["result_json"])


def save_receipt(conn: sqlite3.Connection, request_id: str, request: object, result: object, timestamp: int) -> None:
    conn.execute("INSERT INTO memory_card_mutation_receipts VALUES (?, ?, ?, ?)",
                 (request_id, digest(request), canonical_json(result), timestamp))


def require_current_card(conn: sqlite3.Connection, atom_id: str, expected_revision: object) -> dict[str, object]:
    if not isinstance(expected_revision, str) or len(expected_revision) != 64:
        raise ValueError("expectedRevision must be the card revision from the current Memory read")
    row = conn.execute("SELECT * FROM memory_atoms WHERE id=?", (atom_id,)).fetchone()
    atom = dict(row) if row else {}
    if not atom or atom.get("claim_state") != "current" or atom.get("status") not in {"active", "approved"} or card_revision(atom) != expected_revision:
        current = {"id": atom_id, "revision": card_revision(atom)} if atom else {}
        raise MemoryRevisionConflict("memory_card_revision_conflict", current)
    return atom


def personal_card_scope(atom: Mapping[str, object], owner_id: str = "default") -> bool:
    return (
        atom.get("owner_kind") == "user" and atom.get("owner_id") == owner_id
        and atom.get("knowledge_domain") in {"personal_memory", "user_profile_preference"}
        and atom.get("scope_kind") == "user" and atom.get("scope_id") == owner_id
        and atom.get("visibility") == "private" and atom.get("scope_mode") == "authoritative"
        and bool(atom.get("binding_id")) and bool(atom.get("authorization_revision"))
        and not atom.get("scope_project") and not atom.get("scope_app")
        and atom.get("privacy_level") in {"local", "private"}
        and atom.get("kind") in {"personal_fact", "personal_habit", "durable_preference", "personal_principle", "preference"}
    )


def _card_source_query(personal_only: bool) -> str:
    predicate = admitted_personal_evidence_sql("evidence") if personal_only else "evidence.status='active' AND evidence.admission_state != 'forgotten'"
    return f"""
        SELECT DISTINCT evidence.evidence_id FROM agent_memory_evidence evidence
        WHERE ({predicate}) AND evidence.evidence_id IN (
            SELECT evidence_id FROM memory_atom_evidence_links WHERE memory_atom_id=? AND relation IN ('supports','corrects')
            UNION SELECT evidence_id FROM memory_lifecycle_atom_evidence_links WHERE atom_id=? AND (relation='source' OR ?)
            UNION SELECT source.evidence_id FROM memory_evidence_input_event_links source
              JOIN memory_atoms atom ON atom.id=?
              JOIN json_each(atom.source_event_ids_json) event ON CAST(event.value AS INTEGER)=source.input_event_id
              WHERE source.relation='source'
                AND NOT EXISTS (SELECT 1 FROM memory_atom_evidence_links WHERE memory_atom_id=atom.id)
                AND NOT EXISTS (SELECT 1 FROM memory_lifecycle_atom_evidence_links WHERE atom_id=atom.id)
        )"""


def card_source_count(conn: sqlite3.Connection, atom_id: str, *, personal_only: bool = True,
                      include_context: bool = False) -> int:
    query = _card_source_query(personal_only)
    return int(conn.execute(f'SELECT COUNT(*) FROM ({query})',
               (atom_id, atom_id, int(include_context), atom_id)).fetchone()[0])


def card_source_refs(conn: sqlite3.Connection, atom_id: str, *, personal_only: bool = True,
                     include_context: bool = False, limit: int | None = 32) -> list[dict[str, str]]:
    query = _card_source_query(personal_only)
    rows = conn.execute(f'{query} ORDER BY evidence.evidence_id LIMIT ?',
        (atom_id, atom_id, int(include_context), atom_id, -1 if limit is None else max(1, int(limit)))).fetchall()
    return [{"kind": "evidence", "id": str(row[0])} for row in rows]


def record_personal_edit_source(conn: sqlite3.Connection, text: str, *, timestamp: int) -> tuple[int, str]:
    """An editor Save is explicit user evidence, through the existing ledger."""
    cursor = conn.execute("""INSERT INTO input_events(created_at_ms,source,committed_text,app,project)
                             VALUES (?,'personal_profile_editor',?,'','')""", (timestamp, text))
    event_id = int(cursor.lastrowid)
    evidence = checkpoint_input_event_evidence(conn, event_id=event_id, created_at_ms=timestamp,
        source="personal_profile_editor", committed_text=text, project="")
    source_id = str(evidence["sourceId"])
    conn.execute("""INSERT INTO memory_capture_hints(
        hint_id,source_id,kind,normalized_claim,scope,reason,basis,future_use,evidence_ids_json,
        captured_by_session_id,captured_by_role_id,status,created_at_ms,updated_at_ms)
        VALUES (?,?,'fact',?,'user','personal_profile_editor','explicit_user_request',
                '用户明确保存的个人背景',?,'','','active',?,?)""",
        (f"profile-edit:{event_id}", source_id, text, canonical_json([evidence["evidenceId"]]), timestamp, timestamp))
    transition_evidence_admission(conn, str(evidence["evidenceId"]), new_state="admitted",
        reason_code="explicit_personal_profile_edit", actor_kind="user", created_at_ms=timestamp)
    return event_id, str(evidence["evidenceId"])


def _insert_row(conn: sqlite3.Connection, row: Mapping[str, object]) -> None:
    columns = list(row)
    conn.execute(f"INSERT INTO memory_atoms ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", tuple(row.values()))


def correct_memory_card(conn: sqlite3.Connection, atom_id: str, *, text: str,
        expected_revision: str, timestamp: int, mutation_id: str, reason: str,
        user_edit: bool = False, new_id: str = "", supersession_id: str = "",
        memory_kind: str = "", metadata: Mapping[str, object] | None = None) -> dict[str, object]:
    old = require_current_card(conn, atom_id, expected_revision)
    text = compact_whitespace(text)
    if not text or len(text) > 12000:
        raise ValueError("memory card text must contain 1–12000 characters")
    if old.get("privacy_level") == "sensitive" or contains_sensitive_content(text):
        raise ValueError("sensitive memory cannot be edited in the control center")
    new_id = new_id or "atom:edit:" + digest(mutation_id)[:32]
    supersession_id = supersession_id or "supersession:edit:" + digest(mutation_id)[:32]
    new = dict(old)
    new.update(id=new_id, text=text, canonical_text=text, status="approved", claim_state="current",
               created_at_ms=timestamp, updated_at_ms=timestamp, last_used_at_ms=None,
               valid_from_ms=timestamp, valid_to_ms=None, supersedes_id=atom_id,
               user_edit_revision=int(old.get("user_edit_revision") or 0) + int(user_edit))
    if memory_kind:
        new["kind"] = memory_kind
    new["claim_key"] = old.get("claim_key") or "claim:" + digest(atom_id)[:32]
    new["lineage_id"] = old.get("lineage_id") or "lineage:" + digest(atom_id)[:32]
    new["source_memory_ids_json"] = canonical_json(list(dict.fromkeys([*json.loads(str(old["source_memory_ids_json"])), atom_id])))
    source = record_personal_edit_source(conn, text, timestamp=timestamp) if user_edit and personal_card_scope(old) else None
    if source:
        new["source_event_ids_json"] = canonical_json(list(dict.fromkeys([*json.loads(str(old["source_event_ids_json"])), source[0]])))
    conn.execute("UPDATE memory_atoms SET status='superseded',claim_state='superseded',valid_to_ms=?,updated_at_ms=? WHERE id=?", (timestamp, timestamp, atom_id))
    _insert_row(conn, new)
    conn.execute("""INSERT INTO memory_lifecycle_atom_evidence_links(atom_id,evidence_id,relation,created_at_ms)
        SELECT ?, evidence_id, 'context', ? FROM (
            SELECT evidence_id FROM memory_atom_evidence_links WHERE memory_atom_id=? AND relation IN ('supports','corrects')
            UNION SELECT evidence_id FROM memory_lifecycle_atom_evidence_links WHERE atom_id=?)""",
        (new_id, timestamp, atom_id, atom_id))
    if source:
        conn.execute("INSERT OR IGNORE INTO memory_lifecycle_atom_evidence_links VALUES (?,?,'source',?)", (new_id, source[1], timestamp))
    conn.execute("INSERT INTO memory_atom_tags SELECT ?,tag_id,weight,source FROM memory_atom_tags WHERE memory_atom_id=?", (new_id, atom_id))
    for alias in conn.execute('SELECT * FROM memory_aliases WHERE memory_atom_id=?', (atom_id,)).fetchall():
        conn.execute('INSERT INTO memory_aliases VALUES (?,?,?,?,?,?,?)',
                     ('alias:edit:' + digest([new_id, alias['id']])[:32], new_id,
                      alias['alias'], alias['alias_type'], alias['pinyin'], alias['weight'], alias['created_at_ms']))
    conn.execute("""INSERT INTO memory_supersessions(supersession_id,old_memory_id,new_memory_id,reason,source_event_ids_json,status,created_at_ms,metadata_json)
        VALUES (?,?,?,?,?,'active',?,?)""", (supersession_id, atom_id, new_id, reason,
        new["source_event_ids_json"], timestamp, canonical_json(dict(metadata or {"source": "memory_card_editor", "clientRequestId": mutation_id}))))
    invalidation = invalidate_superseded_atom_dependencies(conn, [atom_id], new_atom_id=new_id, timestamp=timestamp)
    return {"memoryId": new_id, "previousMemoryId": atom_id, "revision": card_revision(new),
            "supersessionId": supersession_id, "dependencyInvalidation": invalidation}


def retract_memory_card(conn: sqlite3.Connection, atom_id: str, *, expected_revision: str,
        timestamp: int, mutation_id: str, reason: str = "explicit_personal_profile_remove") -> dict[str, object]:
    require_current_card(conn, atom_id, expected_revision)
    conn.execute("UPDATE memory_atoms SET status='tombstoned',claim_state='retracted',valid_to_ms=?,updated_at_ms=?,user_edit_revision=user_edit_revision+1 WHERE id=?", (timestamp, timestamp, atom_id))
    conn.execute("""INSERT INTO memory_tombstones(created_at_ms,target_type,target_value,reason,active,metadata_json)
        VALUES (?,'memory_id',?,?,1,?)""", (timestamp, atom_id, reason, canonical_json({"clientRequestId": mutation_id})))
    invalidate_superseded_atom_dependencies(conn, [atom_id], timestamp=timestamp)
    if reason == "explicit_personal_profile_remove":
        _forget_unshared_editor_evidence(conn, atom_id, timestamp)
    row = conn.execute("SELECT * FROM memory_atoms WHERE id=?", (atom_id,)).fetchone()
    return {"memoryId": atom_id, "previousMemoryId": atom_id, "revision": card_revision(dict(row)), "deleted": True}


def _forget_unshared_editor_evidence(conn: sqlite3.Connection, atom_id: str, timestamp: int) -> None:
    # Editor evidence is a single explicit fact, not a multi-fact transcript.
    # Forget its replay input only when no other current card uses it as support.
    candidates = conn.execute("""
        SELECT DISTINCT evidence.evidence_id, source.input_event_id
        FROM memory_lifecycle_atom_evidence_links link
        JOIN agent_memory_evidence evidence ON evidence.evidence_id=link.evidence_id
        JOIN memory_evidence_input_event_links source ON source.evidence_id=evidence.evidence_id AND source.relation='source'
        JOIN input_events event ON event.id=source.input_event_id
        WHERE link.atom_id=? AND event.source='personal_profile_editor'
          AND evidence.admission_state != 'forgotten'
        """, (atom_id,)).fetchall()
    for evidence in candidates:
        shared = conn.execute("""
            SELECT 1 FROM memory_atoms atom
            WHERE atom.status IN ('active','approved') AND atom.claim_state='current'
              AND (EXISTS (SELECT 1 FROM memory_atom_evidence_links l WHERE l.memory_atom_id=atom.id
                           AND l.evidence_id=? AND l.relation IN ('supports','corrects'))
                   OR EXISTS (SELECT 1 FROM memory_lifecycle_atom_evidence_links l WHERE l.atom_id=atom.id
                              AND l.evidence_id=? AND l.relation='source')) LIMIT 1
            """, (evidence['evidence_id'], evidence['evidence_id'])).fetchone()
        if shared is None:
            transition_evidence_admission(conn, str(evidence['evidence_id']), new_state='forgotten',
                reason_code='explicit_personal_profile_remove', actor_kind='user', created_at_ms=timestamp)


def assert_background_card_unchanged(atom: Mapping[str, object], expected_revision: object = None,
                                     *, allow_historical_merge: bool = False) -> None:
    """Old curation packets cannot revive history or replace a user correction."""
    if atom and allow_historical_merge and atom.get('claim_state') == 'superseded' and atom.get('status') == 'superseded' and expected_revision == card_revision(atom):
        return
    if atom and (atom.get("claim_state") != "current" or atom.get("status") not in {"active", "approved"}
                 or ((expected_revision or int(atom.get("user_edit_revision") or 0)) and expected_revision != card_revision(atom))):
        raise MemoryRevisionConflict("memory_card_revision_conflict", {"id": atom["id"], "revision": card_revision(atom)})


def merge_memory_cards(conn: sqlite3.Connection, source_id: str, target_id: str, *,
        expected_revision: str, expected_target_revision: str, timestamp: int,
        mutation_id: str) -> dict[str, object]:
    if source_id == target_id:
        raise ValueError("memory atom cannot merge into itself")
    source = require_current_card(conn, source_id, expected_revision)
    target = require_current_card(conn, target_id, expected_target_revision)
    fields = ('owner_kind','owner_id','privacy_level','knowledge_domain','scope_kind',
              'scope_id','visibility','authorization_revision','binding_id','scope_mode','scope_project','scope_app')
    if any(source.get(key) != target.get(key) for key in fields):
        raise ValueError("memory cards cannot merge across ownership or authority boundaries")
    mutation = correct_memory_card(conn, target_id, text=str(target['canonical_text'] or target['text']),
        expected_revision=expected_target_revision, timestamp=timestamp,
        mutation_id=mutation_id, reason=f"user_merge:{source_id}", user_edit=True)
    new_id = str(mutation['memoryId'])
    new = dict(conn.execute('SELECT * FROM memory_atoms WHERE id=?', (new_id,)).fetchone())
    events = list(dict.fromkeys([*json.loads(str(new['source_event_ids_json'])), *json.loads(str(source['source_event_ids_json']))]))
    memories = list(dict.fromkeys([*json.loads(str(new['source_memory_ids_json'])), *json.loads(str(source['source_memory_ids_json'])), source_id]))
    conn.execute('UPDATE memory_atoms SET source_event_ids_json=?,source_memory_ids_json=? WHERE id=?',
                 (canonical_json(events), canonical_json(memories), new_id))
    conn.execute("""INSERT OR IGNORE INTO memory_lifecycle_atom_evidence_links(atom_id,evidence_id,relation,created_at_ms)
        SELECT ?,evidence_id,'context',? FROM (
            SELECT evidence_id FROM memory_atom_evidence_links WHERE memory_atom_id=? AND relation IN ('supports','corrects')
            UNION SELECT evidence_id FROM memory_lifecycle_atom_evidence_links WHERE atom_id=? AND relation IN ('source','context'))""", (new_id,timestamp,source_id,source_id))
    conn.execute("""INSERT INTO memory_atom_tags SELECT ?,tag_id,weight,'user_merge' FROM memory_atom_tags WHERE memory_atom_id=?
        ON CONFLICT(memory_atom_id,tag_id) DO UPDATE SET weight=MAX(memory_atom_tags.weight,excluded.weight),source='user_merge'""", (new_id,source_id))
    for alias in conn.execute('SELECT * FROM memory_aliases WHERE memory_atom_id=?', (source_id,)).fetchall():
        conn.execute('INSERT INTO memory_aliases VALUES (?,?,?,?,?,?,?)',
                     ('alias:merge:' + digest([new_id, alias['id']])[:32], new_id,
                      alias['alias'], alias['alias_type'], alias['pinyin'], alias['weight'], alias['created_at_ms']))
    retract_memory_card(conn, source_id, expected_revision=expected_revision, timestamp=timestamp,
                        mutation_id=mutation_id, reason=f'user_merge:{new_id}')
    invalidate_superseded_atom_dependencies(conn, [source_id], new_atom_id=new_id, timestamp=timestamp)
    mutation['revision'] = card_revision(dict(conn.execute('SELECT * FROM memory_atoms WHERE id=?', (new_id,)).fetchone()))
    return {**mutation, 'merged': True, 'mergedIntoId': new_id, 'sourceStatus': 'tombstoned', 'sourceEventCount': len(events)}
