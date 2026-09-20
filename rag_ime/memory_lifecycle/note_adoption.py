"""Explicit user review of a version-bound note statement.

Portable imports alone still cannot admit Evidence. This separate surface
records the user's actual capture/adoption intent before invoking the existing
admission and Atom mutation owners; it never invents a Pi Session.
"""

from .common import canonical_json
from ..memory_evidence_admission import transition_evidence_admission
from ..memory_actions import mutate_memory_action


def approve_note_statement(
    conn, *, evidence_id, atom_id, statement, note_id, revision, timestamp, confirmed
):
    if confirmed is not True:
        raise ValueError("explicit_note_adoption_required")
    evidence = conn.execute(
        "SELECT source_id,content_text,project FROM agent_memory_evidence WHERE evidence_id=?",
        (evidence_id,),
    ).fetchone()
    if (
        not evidence
        or evidence["content_text"] != statement
        or not evidence["project"]
        or not note_id
        or len(revision) != 64
    ):
        raise ValueError("note_statement_adoption_mismatch")
    hint_id = "note-adoption:" + evidence_id
    exists = conn.execute(
        "SELECT hint_id FROM memory_capture_hints WHERE hint_id=?", (hint_id,)
    ).fetchone()
    if not exists:
        conn.execute(
            """INSERT INTO memory_capture_hints(hint_id,source_id,kind,normalized_claim,scope,reason,basis,future_use,evidence_ids_json,captured_by_session_id,captured_by_role_id,status,created_at_ms,updated_at_ms)
                     VALUES(?,?,'decision',?,'project','explicit_note_adoption','explicit_user_request',?,?,'','','active',?,?)""",
            (
                hint_id,
                evidence["source_id"],
                statement,
                "项目内复用用户明确采纳的陈述",
                canonical_json([evidence_id]),
                timestamp,
                timestamp,
            ),
        )

    transition_evidence_admission(
        conn,
        evidence_id,
        new_state="admitted",
        reason_code="user_adopted_note_statement",
        actor_kind="user",
        created_at_ms=timestamp,
        metadata={
            "noteId": note_id,
            "revision": revision,
            "surface": "note_review",
            "explicitConfirmation": True,
        },
    )
    return mutate_memory_action(
        conn,
        {
            "memoryId": atom_id,
            "itemType": "atom",
            "action": "pin",
            "reason": "用户明确采纳当前笔记版本中的陈述",
        },
        changed_at_ms=timestamp,
    )
