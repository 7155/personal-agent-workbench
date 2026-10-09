"""Read only original user audit sources of an applied governed Atom.

This is not personal Evidence admission and never grants general Agent audit
access. Both an Atom edge and a bare evidence reference use the same proof.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping

from .agent_governed_memory_tools import (
    _input_event_ids_from_provenance,
    _input_events_are_memory_eligible,
)
from .memory_lifecycle.common import excluded
from .sensitive_content import contains_sensitive_content
from .memory_ingest import looks_sensitive


def governed_atom_evidence(
    conn: sqlite3.Connection, *, project: str, atom_id: str = "", evidence_id: str = "",
) -> list[dict[str, object]]:
    """Return a bounded, deduplicated source set after its original R1 proof.

    Atom visibility matches the existing reference reader, including historical
    superseded rows. Source admission/withdrawal remains independently fenced.
    """
    if bool(atom_id) == bool(evidence_id):
        return []
    rows = conn.execute(
        """
        SELECT evidence.*, link.memory_atom_id AS linked_atom_id,
               link.content_sha256 AS linked_hash, link.provenance_json AS linked_provenance,
               proposal.proposal_id, proposal.operation AS proposal_operation,
               proposal.session_id AS proposal_session, proposal.project AS proposal_project,
               proposal.payload_sha256 AS proposal_hash, proposal.action_json,
               proposal.evidence_ids_json, proposal.evidence_snapshot_json,
               proposal.receipt_json AS proposal_receipt
        FROM memory_atom_evidence_links AS link
        JOIN memory_atoms AS atom ON atom.id = link.memory_atom_id
        JOIN agent_memory_evidence AS evidence ON evidence.evidence_id = link.evidence_id
        JOIN memory_governance_proposals AS proposal ON proposal.proposal_id = link.proposal_id
        WHERE ((? != '' AND atom.id = ?) OR (? != '' AND evidence.evidence_id = ?))
          AND atom.owner_kind = 'user' AND atom.owner_id = 'default'
          AND atom.privacy_level != 'sensitive' AND atom.status NOT IN ('hidden', 'tombstoned')
          AND atom.scope_mode = 'legacy'
          AND (? = '' OR COALESCE(atom.scope_project, '') IN ('', ?))
          AND proposal.status = 'applied' AND proposal.applied_memory_id = atom.id
          AND proposal.operation IN ('remember_preview', 'correct_preview')
          AND proposal.project = COALESCE(atom.scope_project, '')
          AND evidence.project = proposal.project AND evidence.session_id = proposal.session_id
          AND evidence.owner_kind = 'user' AND evidence.owner_id = 'default'
          AND evidence.status = 'active' AND evidence.admission_state = 'admitted'
          AND evidence.scope_mode = 'legacy' AND evidence.source_kind = 'user_message'
          AND ((proposal.operation = 'remember_preview' AND link.relation = 'supports')
               OR (proposal.operation = 'correct_preview' AND link.relation = 'corrects'))
          AND NOT EXISTS (
              SELECT 1 FROM memory_tombstones AS tombstone
              WHERE tombstone.active = 1 AND tombstone.target_type = 'memory_id'
                AND tombstone.target_value IN (atom.id, evidence.evidence_id)
          )
        ORDER BY evidence.occurred_at_ms DESC, evidence.evidence_id DESC
        LIMIT 80
        """,
        (atom_id, atom_id, evidence_id, evidence_id, project, project),
    ).fetchall()
    sources: dict[str, dict[str, object]] = {}
    for raw in rows:
        row = dict(raw)
        if _original_governed_source(conn, row):
            sources.setdefault(str(row["evidence_id"]), row)
    return list(sources.values())


def _original_governed_source(conn: sqlite3.Connection, row: Mapping[str, object]) -> bool:
    try:
        provenance = json.loads(str(row["provenance_json"]))
        metadata = json.loads(str(row["metadata_json"]))
        receipt = json.loads(str(row["proposal_receipt"]))
        action = json.loads(str(row["action_json"]))
        snapshot = json.loads(str(row["evidence_snapshot_json"]))
        if not all(isinstance(value, dict) for value in (provenance, metadata, receipt, action)) or not isinstance(snapshot, list):
            return False
        evidence_id = str(row["evidence_id"])
        selected = [value for value in snapshot if isinstance(value, dict) and value.get("evidenceId") == evidence_id]
        if len(selected) != 1 or evidence_id not in json.loads(str(row["evidence_ids_json"])):
            return False
        source = selected[0]
        body = str(row["content_text"])
        if len(body) > 32_000 or looks_sensitive(body) or contains_sensitive_content(body):
            return False
        if (row["linked_hash"] != row["content_sha256"] or _hash_text(body) != row["content_sha256"]
                or source.get("contentSha256") != row["content_sha256"]
                or source.get("sourceKind") != "user_message" or source.get("sourceId") != row["source_id"]
                or source.get("provenance") != provenance or json.loads(str(row["linked_provenance"])) != provenance
                or metadata.get("messageRole") != "user" or provenance.get("sourceType") != "user_message"
                or provenance.get("sourceId") != row["source_id"]
                or provenance.get("sessionId") != row["proposal_session"]
                or provenance.get("project") != row["proposal_project"]
                or provenance.get("roleId") != row["role_id"]):
            return False
        if excluded(conn, project=str(row["project"]), session_id=str(row["session_id"]), source_id=str(row["source_id"])):
            return False
        if not _input_events_are_memory_eligible(conn, _input_event_ids_from_provenance(provenance)):
            return False
        if _hash_json(action) != row["proposal_hash"] or action.get("evidenceSnapshotSha256") != _hash_json(snapshot):
            return False
        operation = str(row["proposal_operation"]).replace("_preview", "_apply")
        approval_id = receipt.get("approvalId")
        if not isinstance(approval_id, str) or not approval_id:
            return False
        approval = conn.execute("SELECT * FROM agent_approvals WHERE approval_id = ?", (approval_id,)).fetchone()
        if approval is None or (approval["state"], approval["tool_name"], approval["operation"], approval["session_id"], approval["room_bound"]) != ("applied", "memory", operation, row["proposal_session"], 0):
            return False
        approved_preview = json.loads(str(approval["preview_json"]))
        approved_receipt = json.loads(str(approval["receipt_json"]))
        if not isinstance(approved_preview, dict) or not isinstance(approved_receipt, dict):
            return False
        action_payload = approved_preview.get("actionPayload")
        base_state = approved_preview.get("baseState")
        if (action_payload != {"proposalId": row["proposal_id"], "payloadSha256": row["proposal_hash"]}
                or not isinstance(base_state, dict) or base_state.get("evidenceStateSha256") != _hash_json(snapshot)):
            return False
        # Re-read the exact existing approved-operation material; do not trust
        # only a matching identifier or a generic admitted flag.
        approval_material = {"schemaVersion": "rag-ime.agent-approved-operation.v1", "sessionId": row["proposal_session"],
                             "tool": "memory", "operation": operation, "actionPayload": action_payload, "baseState": base_state}
        if _hash_json(approval_material) != approval["payload_sha256"]:
            return False
        expected = {"schemaVersion": "rag-ime.agent-operation-receipt.v1", "toolId": "memory", "operation": operation,
                    "proposalId": row["proposal_id"], "memoryId": row["linked_atom_id"], "approvalId": approval_id,
                    "status": "applied", "mutationApplied": True}
        return (all(receipt.get(key) == value and approved_receipt.get(key) == value for key, value in expected.items())
                and receipt.get("evidenceIds") == json.loads(str(row["evidence_ids_json"]))
                and approved_receipt.get("evidenceIds") == receipt.get("evidenceIds"))
    except (ValueError, TypeError, KeyError):
        return False


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hash_json(value: object) -> str:
    return _hash_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
