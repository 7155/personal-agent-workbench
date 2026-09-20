"""Explicit note-statement adoption through the existing Memory lifecycle owners."""

import sqlite3

from .db import sqlite_connection
from .memory_lifecycle.common import digest, text_digest, transaction
from .memory_lifecycle.portability import records_bundle, import_bundle, object_id
from .memory_lifecycle.note_adoption import approve_note_statement
from .memory_actions import mutate_memory_action
from .knowledge_library.models import KnowledgeLibraryError


def adopt(facade, payload):
    if payload.get("confirm") is not True:
        raise KnowledgeLibraryError(
            "采纳项目事项需要明确确认，不能从笔记保存推断。",
            code="confirmation_required",
        )
    row = facade.worker.management_call(
        "management_vault", {**payload, "action": "memory_prepare"}
    )
    namespace = "paw-vault:" + row["vault_id"]
    record = {
        "id": row["id"],
        "text": row["statement"],
        "occurredAtMs": row["created_ms"],
        "metadata": {
            "noteId": row["note_id"],
            "revision": row["revision"],
            "adoption": "explicit_user",
        },
    }
    packet = records_bundle(
        records=[record], namespace=namespace, project=row["project"]
    )
    origin = {"namespace": namespace, "id": row["id"]}
    atom_id = object_id("atom", origin)
    packet["atoms"] = [
        {
            "id": atom_id,
            "origin": origin,
            "kind": "decision",
            "text": row["statement"],
            "contentSha256": text_digest(row["statement"]),
            "createdAtMs": row["created_ms"],
            "updatedAtMs": row["created_ms"],
            "validFromMs": row["created_ms"],
            "validToMs": None,
            "claimState": "current",
            "properties": {
                "confidence": 1.0,
                "qualityScore": 1.0,
                "echoRisk": 0.0,
                "language": "auto",
                "scopeApp": "",
            },
        }
    ]
    packet["references"].append(
        {"from": atom_id, "to": packet["evidence"][0]["id"], "relation": "source"}
    )
    packet["sha256"] = digest({k: v for k, v in packet.items() if k != "sha256"})
    # Import, explicit admission and atom approval are ONE existing-core transaction.
    with sqlite_connection(facade.work_contract.db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        with transaction(conn):
            result = import_bundle(
                conn, packet, target_project=row["project"], dry_run=False
            )
            evidence_id = result["idMap"][packet["evidence"][0]["id"]]
            local_atom = result["idMap"][atom_id]
            approve_note_statement(
                conn,
                evidence_id=evidence_id,
                atom_id=local_atom,
                statement=row["statement"],
                note_id=row["note_id"],
                revision=row["revision"],
                timestamp=row["created_ms"],
                confirmed=payload.get("confirm"),
            )
    facade.worker.management_call(
        "management_vault",
        {
            "action": "memory_receipt",
            "vaultId": payload["vaultId"],
            "linkId": row["id"],
            "atomId": local_atom,
            "state": "adopted",
        },
    )
    return {
        "adopted": True,
        "atomId": local_atom,
        "project": row["project"],
        "noteRevision": row["revision"],
        "notice": "该陈述已独立采纳为项目事项，笔记其他内容未采纳。",
    }


def reconcile(facade, payload):
    result = facade.worker.management_call(
        "management_vault", {**payload, "action": "memory_links"}
    )
    for row in result["items"]:
        if row["needsReview"] and row["state"] == "adopted" and row["atom_id"]:
            with sqlite_connection(facade.work_contract.db_path) as conn:
                conn.row_factory = sqlite3.Row
                mutate_memory_action(
                    conn,
                    {
                        "memoryId": row["atom_id"],
                        "itemType": "atom",
                        "action": "disable",
                        "reason": "所采纳的笔记依据发生实质变化，等待用户复核",
                    },
                )
            facade.worker.management_call(
                "management_vault",
                {
                    "action": "memory_receipt",
                    "vaultId": payload["vaultId"],
                    "linkId": row["id"],
                    "atomId": row["atom_id"],
                    "state": "needs_review",
                },
            )
            row["state"] = "needs_review"
    return result
