"""Second-brain control records in PAW's existing portable backup archive.

Restoration carries history, not authority: scopes pause, remote processing is
off, pairings are revoked and old approvals must be reviewed anew.
"""

import json
from pathlib import Path
from .store import KnowledgeStore
from .vault import MarkdownVault

TABLES = (
    "knowledge_vaults",
    "knowledge_vault_notes",
    "knowledge_vault_policy",
    "knowledge_vault_materials",
    "knowledge_note_proposals",
    "knowledge_note_applications",
    "knowledge_vault_days",
    "knowledge_vault_memory_links",
)


def export_records(database: Path):
    if not database.is_file():
        return None
    import sqlite3

    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        present = {
            r[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "knowledge_vaults" not in present:
            return None
        tables = {
            name: [dict(r) for r in db.execute("SELECT * FROM " + name)]
            for name in TABLES
            if name in present
        }
    return {
        "schemaVersion": "paw.vault-control-backup.v1",
        "tables": tables,
        "restoresAuthority": False,
    }


def restore_records(database: Path, packet):
    if (
        not isinstance(packet, dict)
        or packet.get("schemaVersion") != "paw.vault-control-backup.v1"
    ):
        raise ValueError("invalid vault control backup")
    tables = packet.get("tables")
    if not isinstance(tables, dict) or set(tables) - set(TABLES):
        raise ValueError("invalid vault control collections")
    store = KnowledgeStore(database)
    MarkdownVault(store)
    with store.connection() as db:
        db.execute("DELETE FROM knowledge_editor_pairings")
        for name in reversed(TABLES):
            db.execute("DELETE FROM " + name)
        for name in TABLES:
            columns = [r[1] for r in db.execute("PRAGMA table_info(" + name + ")")]
            rows = tables.get(name, [])
            if not isinstance(rows, list) or len(rows) > 100_000:
                raise ValueError("vault control collection too large")
            for original in rows:
                if not isinstance(original, dict) or set(original) != set(columns):
                    raise ValueError("vault control row does not match schema")
                row = dict(original)
                if name == "knowledge_vaults":
                    row["paused"] = 1
                elif name == "knowledge_vault_policy":
                    policy = json.loads(row["policy_json"])
                    policy.update(
                        remoteProcessing=False,
                        jevEnabled=False,
                        inbox="",
                        personalDiary="",
                    )
                    row["policy_json"] = json.dumps(policy)
                elif name == "knowledge_note_proposals" and row["state"] in {
                    "waiting_editor",
                    "applying",
                }:
                    row["state"] = "prepared"
                elif name == "knowledge_vault_memory_links":
                    row["state"] = "needs_review"
                db.execute(
                    f"INSERT INTO {name} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                    [row[c] for c in columns],
                )
