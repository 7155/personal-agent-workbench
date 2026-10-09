"""Real temporary Gateway/approval producer → actual Management read seams."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest

from rag_ime.contracts.json_schema import validate_contract
from rag_ime.management_service import ManagementService, page_request
from tests import test_agent_governed_memory_tools as governed


class GovernedMemoryReferenceTests(unittest.TestCase):
    def setUp(self):
        self.owner = governed.GovernedMemoryToolTests()
        self.owner.setUp()
        self.addCleanup(self.owner.tearDown)
        self.reader = self.make_reader("wisdom-weasel-rag-ime")
        self.evidence = self.owner.evidence_store.record_user_message(
            session_id=self.owner.session["id"], role_id=self.owner.session["roleId"],
            pi_entry_id="turn:original-user", turn_id="turn:original-user",
            text="请记住演示项目的发布标签是蓝色，不是个人画像。", occurred_at_ms=100,
        )["evidence"]["evidenceId"]
        self.preview = self.owner._execute("memory", "remember_preview", text="演示项目的发布标签是蓝色。",
                                           memoryKind="project_state", evidenceIds=[self.evidence])["result"]
        self.receipt = self.apply("remember_apply", self.preview["proposalId"])
        self.atom = self.receipt["memoryId"]

    def make_reader(self, project):
        reader = ManagementService(db_path=self.owner.db_path, project=project, repo_root=Path(__file__).parents[1],
                                   settings_store=object(), health_provider=lambda: {}, input_source_provider=lambda: {},
                                   predictor_provider=lambda: {}, runtime_config_provider=lambda: SimpleNamespace(settings_revision="fixture-settings", runtime_revision=1))
        self.addCleanup(reader.close)
        return reader

    def apply(self, operation, proposal):
        prepared = self.owner._execute("memory", operation, proposalId=proposal)["result"]
        decided = self.owner.sessions.decide_approval(prepared["approvalId"], approved=True,
                                                    payload_sha256=prepared["approval"]["payloadSha256"])
        receipt = self.owner.gateway.apply_approval(decided)
        self.owner.sessions.complete_approval(prepared["approvalId"], state="applied", receipt=receipt)
        return receipt

    def refs(self, atom=None):
        return self.reader.memory_reference("atom", atom or self.atom)["evidenceRefs"]

    def stored_rows(self):
        with sqlite3.connect(self.owner.db_path) as conn:
            return {table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                    for table in ("memory_atoms", "agent_memory_evidence", "memory_atom_evidence_links",
                                  "memory_governance_proposals", "agent_approvals", "input_events")}

    def assert_unreadable_evidence(self):
        with self.assertRaisesRegex(ValueError, "not found or is not visible"):
            self.reader.memory_reference("evidence", self.evidence)

    def test_original_user_project_fact_links_resolve_without_personal_promotion_or_writes(self):
        with sqlite3.connect(self.owner.db_path) as conn:
            domain = conn.execute("SELECT evidence_domain,origin_kind,scope_mode FROM agent_memory_evidence WHERE evidence_id=?", (self.evidence,)).fetchone()
            arrays = conn.execute("SELECT source_event_ids_json,source_memory_ids_json FROM memory_atoms WHERE id=?", (self.atom,)).fetchone()
        self.assertEqual(domain, ("role_book", "legacy_agent_event", "legacy"))
        self.assertEqual(arrays, ("[]", "[]"))
        before = self.stored_rows()
        reference = self.reader.memory_reference("atom", self.atom)
        self.assertEqual([r["referenceId"] for r in reference["evidenceRefs"]], [self.evidence])
        source = self.reader.memory_reference("evidence", self.evidence)
        validate_contract(source, "memory-reference.v1.json")
        self.assertEqual(source["item"]["sourceKind"], "user_message")
        self.assertEqual(source["item"]["provenance"]["turnId"], "turn:original-user")
        self.assertEqual(source["item"]["text"], "请记住演示项目的发布标签是蓝色，不是个人画像。")
        self.assertEqual(source["evidenceRefs"], [])  # No fabricated input event.
        page = self.reader.memory_page("atoms", page_request({"project": self.reader.project, "limit": 100}))
        item = next(item for item in page["items"] if item["id"] == self.atom)
        self.assertEqual(item["sourceCount"], 1)
        self.assertEqual(item["evidenceRefs"], reference["evidenceRefs"])
        self.assertNotIn(self.evidence, json.dumps(self.reader.memory_page("evidence", page_request({"limit": 100}))))
        with sqlite3.connect(self.owner.db_path) as conn:
            self.assertEqual(conn.execute("SELECT evidence_domain,origin_kind,scope_mode FROM agent_memory_evidence WHERE evidence_id=?", (self.evidence,)).fetchone(), domain)
        self.assertEqual(self.stored_rows(), before)

    def test_binding_corruption_and_withdrawal_fail_closed_on_all_three_readers(self):
        cases = [
            ("agent_memory_evidence", "evidence_id", self.evidence, "project", "foreign-project"),
            ("agent_memory_evidence", "evidence_id", self.evidence, "session_id", "foreign-session"),
            ("agent_memory_evidence", "evidence_id", self.evidence, "source_kind", "assistant_message"),
            ("agent_memory_evidence", "evidence_id", self.evidence, "content_text", "changed body"),
            ("agent_memory_evidence", "evidence_id", self.evidence, "scope_mode", "authoritative"),
            ("agent_memory_evidence", "evidence_id", self.evidence, "owner_id", "other-user"),
            ("agent_memory_evidence", "evidence_id", self.evidence, "role_id", "foreign-role"),
            ("agent_memory_evidence", "evidence_id", self.evidence, "metadata_json", "{\"messageRole\":\"assistant\"}"),
            ("agent_memory_evidence", "evidence_id", self.evidence, "status", "tombstoned"),
            ("agent_memory_evidence", "evidence_id", self.evidence, "admission_state", "forgotten"),
            ("memory_governance_proposals", "proposal_id", self.preview["proposalId"], "status", "preview"),
            ("memory_governance_proposals", "proposal_id", self.preview["proposalId"], "evidence_snapshot_json", "[]"),
            ("memory_governance_proposals", "proposal_id", self.preview["proposalId"], "action_json", "{}"),
            ("memory_governance_proposals", "proposal_id", self.preview["proposalId"], "receipt_json", "{}"),
            ("memory_governance_proposals", "proposal_id", self.preview["proposalId"], "applied_memory_id", "foreign-atom"),
            ("memory_atom_evidence_links", "memory_atom_id", self.atom, "content_sha256", "0" * 64),
            ("memory_atom_evidence_links", "memory_atom_id", self.atom, "provenance_json", "{}"),
            ("memory_atom_evidence_links", "memory_atom_id", self.atom, "relation", "corrects"),
            ("agent_approvals", "approval_id", self.receipt["approvalId"], "state", "approved"),
            ("agent_approvals", "approval_id", self.receipt["approvalId"], "payload_sha256", "0" * 64),
            ("agent_approvals", "approval_id", self.receipt["approvalId"], "preview_json", "{}"),
            ("agent_approvals", "approval_id", self.receipt["approvalId"], "room_bound", 1),
            ("agent_approvals", "approval_id", self.receipt["approvalId"], "receipt_json", "{}"),
            ("memory_atoms", "id", self.atom, "owner_id", "other-user"),
            ("memory_atoms", "id", self.atom, "scope_mode", "authoritative"),
        ]
        for table, key, identifier, field, value in cases:
            with self.subTest(table=table, field=field):
                with sqlite3.connect(self.owner.db_path) as conn:
                    previous = conn.execute(f"SELECT {field} FROM {table} WHERE {key}=?", (identifier,)).fetchone()[0]
                    conn.execute(f"UPDATE {table} SET {field}=? WHERE {key}=?", (value, identifier))
                try:
                    self.assertEqual(self.refs(), [])
                    self.assert_unreadable_evidence()
                    page = self.reader.memory_page("atoms", page_request({"project": self.reader.project, "limit": 100}))
                    item = next((item for item in page["items"] if item["id"] == self.atom), None)
                    self.assertTrue(item is None or not item["evidenceRefs"])
                finally:
                    with sqlite3.connect(self.owner.db_path) as conn:
                        conn.execute(f"UPDATE {table} SET {field}=? WHERE {key}=?", (previous, identifier))

    def test_foreign_project_reader_and_unlinked_audit_stay_private(self):
        foreign = self.make_reader("foreign-project")
        for kind, identifier in (("atom", self.atom), ("evidence", self.evidence)):
            with self.assertRaises(ValueError):
                foreign.memory_reference(kind, identifier)
        with self.assertRaises(ValueError):
            self.reader.memory_reference("evidence", self.owner.evidence_ids["safe"])
        page = self.reader.memory_page("atoms", page_request({"project": self.reader.project,
            "visibleOwners": [{"ownerKind": "agent", "ownerId": "foreign-agent"}], "limit": 100}))
        self.assertEqual(page["items"], [])

    def test_governed_audit_read_does_not_open_unbound_input_context(self):
        # A later raw input link is not part of the approved Agent source
        # snapshot. The fallback must not open it as a personal Event.
        with sqlite3.connect(self.owner.db_path) as conn:
            event = conn.execute("INSERT INTO input_events(created_at_ms,source,committed_text,project) VALUES (1,'manual','unrelated raw context',?)",
                                 (self.reader.project,)).lastrowid
            conn.execute("INSERT INTO memory_evidence_input_event_links(evidence_id,input_event_id,ordinal,relation,content_sha256,created_at_ms) VALUES (?,?,0,'source',?,1)",
                         (self.evidence, event, "0" * 64))
        source = self.reader.memory_reference("evidence", self.evidence)
        self.assertEqual(source["evidenceRefs"], [])
        self.assertNotIn("unrelated raw context", json.dumps(source))

    def test_tombstones_and_capture_exclusions_hide_original_sources(self):
        for kind, identifier in (("memory_id", self.evidence), ("memory_id", self.atom)):
            with self.subTest(kind=kind, identifier=identifier), sqlite3.connect(self.owner.db_path) as conn:
                conn.execute("INSERT INTO memory_tombstones(created_at_ms,target_type,target_value,reason,active) VALUES (1,?,?, 'test',1)", (kind, identifier))
                conn.commit()
                self.assertEqual(self.refs(), [])
                self.assert_unreadable_evidence()
                conn.execute("DELETE FROM memory_tombstones WHERE target_value=?", (identifier,))
        for kind, identifier in (("session", self.owner.session["id"]), ("source", "turn:original-user")):
            with self.subTest(exclusion=kind), sqlite3.connect(self.owner.db_path) as conn:
                conn.execute("INSERT INTO memory_capture_exclusions(project,target_kind,target_id,created_at_ms) VALUES (?,?,?,1)",
                             (self.reader.project, kind, identifier))
                conn.commit()
                self.assertEqual(self.refs(), [])
                self.assert_unreadable_evidence()
                conn.execute("DELETE FROM memory_capture_exclusions WHERE target_id=?", (identifier,))

    def test_unreadable_or_sensitive_atom_cannot_authorize_bare_evidence(self):
        for field, value in (("privacy_level", "sensitive"), ("status", "hidden"), ("status", "tombstoned")):
            with self.subTest(field=field, value=value), sqlite3.connect(self.owner.db_path) as conn:
                original = conn.execute(f"SELECT {field} FROM memory_atoms WHERE id=?", (self.atom,)).fetchone()[0]
                conn.execute(f"UPDATE memory_atoms SET {field}=? WHERE id=?", (value, self.atom))
                conn.commit()
                with self.assertRaises(ValueError):
                    self.reader.memory_reference("atom", self.atom)
                self.assert_unreadable_evidence()
                conn.execute(f"UPDATE memory_atoms SET {field}=? WHERE id=?", (original, self.atom))

    def test_correction_keeps_original_historical_provenance_and_forget_hides_current(self):
        next_evidence = self.owner.evidence_store.record_user_message(session_id=self.owner.session["id"],
            role_id=self.owner.session["roleId"], pi_entry_id="turn:correction", turn_id="turn:correction",
            text="标签现在改成绿色。", occurred_at_ms=200)["evidence"]["evidenceId"]
        preview = self.owner._execute("memory", "correct_preview", targetId=self.atom, text="演示项目的发布标签是绿色。",
                                      reason="用户明确更正", evidenceIds=[next_evidence])["result"]
        correction = self.apply("correct_apply", preview["proposalId"])
        self.assertEqual([ref["referenceId"] for ref in self.refs()], [self.evidence])
        self.assertEqual([ref["referenceId"] for ref in self.refs(correction["memoryId"])], [next_evidence])
        forget = self.owner._execute("memory", "forget_preview", targetId=correction["memoryId"],
                                   reason="用户撤回", evidenceIds=[next_evidence])["result"]
        self.apply("forget_apply", forget["proposalId"])
        with self.assertRaises(ValueError):
            self.reader.memory_reference("atom", correction["memoryId"])
        with self.assertRaises(ValueError):
            self.reader.memory_reference("evidence", next_evidence)

    def test_original_rollback_does_not_leave_a_bare_evidence_read_grant(self):
        self.apply("governance_rollback", self.preview["proposalId"])
        self.assert_unreadable_evidence()
        with self.assertRaises(ValueError):
            self.reader.memory_reference("atom", self.atom)


if __name__ == "__main__":
    unittest.main()
