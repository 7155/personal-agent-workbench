"""A short, source-linked view of personal cards, never a second fact store."""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping

from .memory_card_mutations import (
    MemoryRevisionConflict, atomic_memory_write, canonical_json, card_revision,
    card_source_refs, correct_memory_card, digest, personal_card_scope,
    record_personal_edit_source, replay_receipt, retract_memory_card, save_receipt,
)
from .sensitive_content import contains_sensitive_content
from .text_utils import compact_whitespace

MAX_PARAGRAPHS = 12
MAX_PARAGRAPH_CHARS = 600
MAX_PROFILE_CHARS = 4000


def read_personal_profile(conn: sqlite3.Connection, *, owner_id: str = "default") -> dict[str, object]:
    rows = conn.execute("""SELECT * FROM memory_atoms
        WHERE owner_kind='user' AND owner_id=? AND scope_kind='user' AND scope_id=?
          AND knowledge_domain IN ('personal_memory','user_profile_preference')
          AND visibility='private' AND scope_mode='authoritative'
          AND COALESCE(scope_project,'')='' AND COALESCE(scope_app,'')=''
          AND privacy_level IN ('local','private')
          AND kind IN ('personal_fact','personal_habit','durable_preference','personal_principle','preference')
          AND binding_id != '' AND authorization_revision != ''
          AND status IN ('active','approved') AND claim_state='current'
        ORDER BY user_edit_revision DESC, updated_at_ms DESC, id LIMIT 97
        """, (owner_id, owner_id)).fetchall()
    paragraphs: list[dict[str, object]] = []
    revisions: list[object] = []
    used = 0
    truncated = len(rows) > 96
    for row in rows[:96]:
        atom = dict(row)
        text = compact_whitespace(str(atom.get("canonical_text") or atom.get("text") or ""))
        if not personal_card_scope(atom, owner_id) or not text or contains_sensitive_content(text):
            continue
        refs = card_source_refs(conn, str(atom["id"]))
        if not refs:
            continue
        support_ids = {ref['id'] for ref in refs}
        refs = [*refs, *(ref for ref in card_source_refs(conn, str(atom["id"]), include_context=True)
                        if ref['id'] not in support_ids)][:32]
        revision = card_revision(atom)
        revisions.append([atom["id"], revision, refs])
        # Omit overlong facts instead of presenting a clipped editable sentence.
        if len(text) > MAX_PARAGRAPH_CHARS or len(paragraphs) >= MAX_PARAGRAPHS or used + len(text) + (2 if paragraphs else 0) > MAX_PROFILE_CHARS:
            truncated = True
            continue
        paragraphs.append({"id": atom["id"], "memoryIds": [atom["id"]], "text": text,
                           "revision": revision, "sourceCount": len(refs), "sourceRefs": refs})
        used += len(text) + (2 if len(paragraphs) > 1 else 0)
    return {"schemaVersion": "paw.personal-profile.v1", "revision": digest(revisions),
            "text": "\n\n".join(str(item["text"]) for item in paragraphs),
            "paragraphs": paragraphs, "truncated": truncated}


def _new_personal_card(conn: sqlite3.Connection, *, text: str, mutation_id: str, timestamp: int) -> dict[str, object]:
    event_id, evidence_id = record_personal_edit_source(conn, text, timestamp=timestamp)
    atom_id = "atom:profile:" + digest(mutation_id)[:32]
    conn.execute("""INSERT INTO memory_atoms(
        id,kind,text,canonical_text,source_event_ids_json,source_memory_ids_json,
        scope_app,scope_project,language,confidence,quality_score,echo_risk,privacy_level,
        status,created_at_ms,updated_at_ms,owner_kind,owner_id,claim_key,lineage_id,
        claim_state,valid_from_ms,knowledge_domain,scope_kind,scope_id,visibility,
        authorization_revision,binding_id,scope_mode,user_edit_revision)
        VALUES (?,'personal_fact',?,?,?,'[]','','','zh',1,1,0,'private','approved',?,?,'user','default',?,?,'current',?,
                'personal_memory','user','default','private','explicit-profile-edit-v1','user:default','authoritative',1)""",
        (atom_id, text, text, canonical_json([event_id]), timestamp, timestamp,
         "profile:" + digest(mutation_id), "lineage:" + digest(mutation_id), timestamp))
    conn.execute("INSERT INTO memory_lifecycle_atom_evidence_links VALUES (?,?,'source',?)", (atom_id, evidence_id, timestamp))
    row = conn.execute("SELECT * FROM memory_atoms WHERE id=?", (atom_id,)).fetchone()
    return {"memoryId": atom_id, "previousMemoryId": "", "revision": card_revision(dict(row))}


def save_personal_profile(conn: sqlite3.Connection, payload: Mapping[str, object], *, timestamp: int) -> dict[str, object]:
    expected = payload.get("expectedRevision")
    if not isinstance(expected, str) or len(expected) != 64:
        raise ValueError("expectedRevision must be the profile revision from the current read")
    paragraphs = payload.get("paragraphs")
    if not isinstance(paragraphs, list) or len(paragraphs) > MAX_PARAGRAPHS:
        raise ValueError("profile supports at most 12 edited paragraphs")
    request_id = payload.get("clientRequestId")
    with atomic_memory_write(conn):
        replay = replay_receipt(conn, request_id, dict(payload))
        if replay is not None:
            return replay
        current = read_personal_profile(conn)
        if current["revision"] != expected:
            raise MemoryRevisionConflict("memory_profile_revision_conflict", current)
        by_id = {str(item["id"]): item for item in current["paragraphs"]}
        seen: set[str] = set()
        validated: list[tuple[str, str, str]] = []
        total = 0
        for item in paragraphs:
            if not isinstance(item, Mapping) or not isinstance(item.get("text"), str):
                raise ValueError("profile paragraph text must be a string")
            text = compact_whitespace(item["text"])
            total += len(text)
            if len(text) > MAX_PARAGRAPH_CHARS or total > MAX_PROFILE_CHARS or contains_sensitive_content(text):
                raise ValueError("profile paragraph is sensitive or exceeds the 600/4000 character limits")
            atom_id = item.get("id")
            ids = item.get("memoryIds")
            if atom_id is None:
                if ids != [] or not text:
                    raise ValueError("new profile paragraphs require text and empty memoryIds")
                validated.append(("", text, ""))
                continue
            if not isinstance(atom_id, str) or atom_id not in by_id or ids != [atom_id] or atom_id in seen:
                raise ValueError("ambiguous profile paragraph mapping; reload before saving")
            if item.get("revision") != by_id[atom_id]["revision"]:
                raise MemoryRevisionConflict("memory_profile_revision_conflict", current)
            seen.add(atom_id)
            validated.append((atom_id, text, str(item["revision"])))
        changes: list[dict[str, object]] = []
        for index, (atom_id, text, revision) in enumerate(validated):
            mutation_id = f"profile:{request_id}:{index}"
            if not atom_id:
                change = _new_personal_card(conn, text=text, mutation_id=mutation_id, timestamp=timestamp)
            elif not text:
                change = retract_memory_card(conn, atom_id, expected_revision=revision, timestamp=timestamp, mutation_id=mutation_id)
            elif text == by_id[atom_id]["text"]:
                continue
            else:
                change = correct_memory_card(conn, atom_id, text=text, expected_revision=revision,
                    timestamp=timestamp, mutation_id=mutation_id, reason="explicit_personal_profile_edit", user_edit=True)
            changes.append({key: change[key] for key in ("memoryId", "previousMemoryId", "revision")})
        result = {"ok": True, "profile": read_personal_profile(conn), "changes": changes}
        save_receipt(conn, str(request_id), dict(payload), result, timestamp)
        return result
