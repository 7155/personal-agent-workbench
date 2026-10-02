from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from rag_ime.agent_role_book import AgentRoleBookStore
from rag_ime.agent_role_book_control import AgentRoleBookControlService
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.management_work_contract import ManagementWorkContract
from rag_ime.personal_context import AgentMemoryEvidenceStore
from rag_ime.personal_context_observability import PersonalContextObservability
from tests.sqlite_fixtures import copy_current_database


class AgentRoleBookControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-role-book-control-")
        self.db_path = Path(self.tmp.name) / "rag-ime.sqlite3"
        self.addCleanup(self.tmp.cleanup)
        copy_current_database(self.db_path)
        self.roles = AgentRoleBookStore(self.db_path)
        self.roles.initialize()
        self.observability = PersonalContextObservability(
            self.db_path,
            project="rag-ime",
        )
        self.observability.initialize()
        self.control = AgentRoleBookControlService(
            self.db_path,
            project="rag-ime",
            work_contract=ManagementWorkContract(db_path=self.db_path),
            role_books=self.roles,
            observability=self.observability,
        )
        self.evidence = AgentMemoryEvidenceStore(
            self.db_path,
            project="rag-ime",
        )
        self.sessions = AgentSessionStore(self.db_path)
        self.seed = self.roles.ensure_seeded(
            "companion-present-v1",
            "1",
            "澄",
            "陪用户完成项目",
            "persona-v1",
            created_at_ms=100,
        )
        self.old_session = self.sessions.create(
            title="旧会话",
            role_id="companion-present-v1",
            role_version="1",
            role_book_revision_id=str(self.seed["revisionId"]),
            created_at_ms=110,
        )
        self.daily_draft = self._seed_daily_draft()

    def test_daily_proposals_require_r1_preview_and_keep_old_session_pinned(self) -> None:
        catalog = self.control.catalog(
            role_id="companion-present-v1",
            role_version="1",
        )
        self.assertFalse(catalog["activationPolicy"]["agentCanActivate"])
        self.assertEqual(
            catalog["dailyDrafts"][0]["draftId"],
            self.daily_draft["draftId"],
        )
        self.assertEqual(
            catalog["activationPolicy"]["mutableSections"],
            [
                "personality",
                "capabilities",
                "lessonsAndLimits",
                "activeCommitments",
            ],
        )
        self.assertEqual(
            catalog["dailyDrafts"][0]["lessonProposals"][0]["text"],
            "不要把未经验证的推断写成事实",
        )
        self.assertEqual(
            catalog["dailyDrafts"][0]["commitmentProposals"][0]["text"],
            "下一轮先运行聚焦回归测试",
        )
        self.assertEqual(
            catalog["dailyDrafts"][0]["proposalDiagnostics"]["status"],
            "completed",
        )

        selection = {
            "roleId": "companion-present-v1",
            "roleVersion": "1",
            "revisionId": "",
            "draftId": self.daily_draft["draftId"],
            "traitIndexes": [0],
            "capabilityIndexes": [0],
            "lessonIndexes": [0],
            "commitmentIndexes": [0],
        }
        preview = self.control.activation_preview(selection)
        self.assertTrue(preview["ok"])
        self.assertEqual(preview["summary"]["risk"], "R1")
        self.assertIn("采用 1 条经验与边界", preview["summary"]["items"])
        self.assertIn("采用 1 条当前承诺", preview["summary"]["items"])

        applied = self.control.activation_apply(
            {
                **selection,
                "previewToken": preview["previewToken"],
                "payloadSha256": preview["payloadSha256"],
                "confirmText": "apply",
            }
        )
        self.assertTrue(applied["ok"])
        revision = applied["result"]["revision"]
        self.assertEqual(revision["status"], "active")
        expected_text = {
            "personality": "沟通时先给出具体例子",
            "capabilities": "能修复 SQLite 事务恢复问题",
            "lessonsAndLimits": "不要把未经验证的推断写成事实",
            "activeCommitments": "下一轮先运行聚焦回归测试",
        }
        for section, text in expected_text.items():
            self.assertIn(
                text,
                [item["text"] for item in revision["sections"][section]],
            )

        old_after = self.sessions.get(str(self.old_session["id"]))
        self.assertEqual(
            old_after["roleBookRevisionId"],
            self.seed["revisionId"],
        )
        new_session = self.sessions.create(
            title="新会话",
            role_id="companion-present-v1",
            role_version="1",
            role_book_revision_id=str(revision["revisionId"]),
            created_at_ms=200,
        )
        self.assertEqual(
            new_session["roleBookRevisionId"],
            revision["revisionId"],
        )

        snapshot = self.observability.snapshot(role_id="companion-present-v1")
        self.assertEqual(
            snapshot["drafts"]["latestDecisionByOutcome"],
            {"accepted": 1},
        )
        self.assertEqual(
            snapshot["revisions"]["activationEvents"]["activate"],
            1,
        )

        rolled_back = self.control.activation_rollback(
            {
                "receiptId": applied["receiptId"],
                "rollbackToken": applied["rollbackToken"],
                "payloadSha256": applied["payloadSha256"],
                "confirmText": "rollback",
            }
        )
        self.assertTrue(rolled_back["ok"])
        self.assertEqual(
            rolled_back["result"]["revision"]["revisionId"],
            self.seed["revisionId"],
        )
        self.assertEqual(
            self.sessions.get(str(new_session["id"]))["roleBookRevisionId"],
            revision["revisionId"],
        )

    def test_permissions_and_unselected_daily_text_cannot_enter_revision(self) -> None:
        rejected = self.control.activation_preview(
            {
                "roleId": "companion-present-v1",
                "roleVersion": "1",
                "draftId": self.daily_draft["draftId"],
                "traitIndexes": [0],
                "capabilityIndexes": [],
                "lessonIndexes": [],
                "commitmentIndexes": [],
                "permissions": ["admin"],
            }
        )
        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["errorCode"], "invalid_request")

        selection = {
            "roleId": "companion-present-v1",
            "roleVersion": "1",
            "revisionId": "",
            "draftId": self.daily_draft["draftId"],
            "traitIndexes": [],
            "capabilityIndexes": [0],
            "lessonIndexes": [],
            "commitmentIndexes": [],
        }
        preview = self.control.activation_preview(selection)
        applied = self.control.activation_apply(
            {
                **selection,
                "previewToken": preview["previewToken"],
                "payloadSha256": preview["payloadSha256"],
                "confirmText": "apply",
            }
        )
        self.assertTrue(applied["ok"])
        sections = applied["result"]["revision"]["sections"]
        seed_sections = self.seed["sections"]
        self.assertEqual(sections["personality"], seed_sections["personality"])
        self.assertEqual(
            sections["lessonsAndLimits"],
            seed_sections["lessonsAndLimits"],
        )
        self.assertEqual(
            sections["activeCommitments"],
            seed_sections["activeCommitments"],
        )
        self.assertEqual(
            len(sections["capabilities"]),
            len(seed_sections["capabilities"]) + 1,
        )
        self.assertIn(
            "能修复 SQLite 事务恢复问题",
            [item["text"] for item in sections["capabilities"]],
        )
        encoded = json.dumps(applied, ensure_ascii=False)
        self.assertNotIn("不应被采用的另一条特征", encoded)
        self.assertNotIn("不要把未经验证的推断写成事实", encoded)
        self.assertNotIn("下一轮先运行聚焦回归测试", encoded)

    def test_daily_lesson_and_commitment_indexes_are_range_checked(self) -> None:
        for field in ("lessonIndexes", "commitmentIndexes"):
            with self.subTest(field=field):
                selection = {
                    "roleId": "companion-present-v1",
                    "roleVersion": "1",
                    "revisionId": "",
                    "draftId": self.daily_draft["draftId"],
                    "traitIndexes": [],
                    "capabilityIndexes": [],
                    "lessonIndexes": [99] if field == "lessonIndexes" else [],
                    "commitmentIndexes": (
                        [99] if field == "commitmentIndexes" else []
                    ),
                }
                preview = self.control.activation_preview(selection)
                self.assertFalse(preview["ok"])
                self.assertEqual(preview["errorCode"], "invalid_request")

    def test_daily_draft_can_be_deferred_without_changing_active_revision(self) -> None:
        result = self.control.decide_daily_draft(
            {
                "roleId": "companion-present-v1",
                "roleVersion": "1",
                "draftId": self.daily_draft["draftId"],
                "decision": "deferred",
            }
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["decision"]["decision"], "deferred")
        self.assertEqual(
            self.roles.active("companion-present-v1", "1")["revisionId"],
            self.seed["revisionId"],
        )

    def test_empty_daily_draft_can_be_deferred_but_not_activated(self) -> None:
        draft = self._seed_empty_daily_draft()

        result = self.control.decide_daily_draft(
            {
                "roleId": "companion-present-v1",
                "roleVersion": "1",
                "draftId": draft["draftId"],
                "decision": "deferred",
            }
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["decision"]["decision"], "deferred")
        preview = self.control.activation_preview(
            {
                "roleId": "companion-present-v1",
                "roleVersion": "1",
                "revisionId": "",
                "draftId": draft["draftId"],
                "traitIndexes": [],
                "capabilityIndexes": [],
                "lessonIndexes": [],
                "commitmentIndexes": [],
            }
        )
        self.assertFalse(preview["ok"])
        self.assertEqual(preview["errorCode"], "domain_not_applicable")

    def test_agent_revision_preview_shows_item_diff_and_rejects_stale_base(
        self,
    ) -> None:
        evidence_id = str(self.daily_draft["sourceEvidenceIds"][0])
        first = self.roles.propose_revision(
            "companion-present-v1",
            "1",
            {
                "capabilities": [
                    self._role_book_item(
                        "capability:first",
                        "能完成可恢复的角色书迁移",
                        evidence_id,
                    )
                ]
            },
            proposed_by="agent:architect",
            change_summary="补充迁移能力",
            created_at_ms=150,
        )
        selection = {
            "roleId": "companion-present-v1",
            "roleVersion": "1",
            "revisionId": first["revisionId"],
            "draftId": "",
            "traitIndexes": [],
            "capabilityIndexes": [],
        }
        preview = self.control.activation_preview(selection)
        self.assertTrue(preview["ok"])
        self.assertEqual(preview["summary"]["evidenceCount"], 1)
        capabilities_diff = next(
            item
            for item in preview["summary"]["diff"]["sections"]
            if item["section"] == "capabilities"
        )
        self.assertEqual(
            capabilities_diff["added"][0]["text"],
            "能完成可恢复的角色书迁移",
        )

        newer = self.roles.propose_revision(
            "companion-present-v1",
            "1",
            {
                "personality": [
                    self._role_book_item(
                        "personality:newer",
                        "先确认边界再执行",
                        evidence_id,
                    )
                ]
            },
            proposed_by="agent:architect",
            created_at_ms=160,
        )
        self.roles.activate_revision(
            newer["revisionId"],
            activated_at_ms=170,
        )
        stale = self.control.activation_preview(selection)
        self.assertFalse(stale["ok"])
        self.assertEqual(stale["errorCode"], "revision_mismatch")

    def test_agent_revision_requires_real_active_evidence_and_valid_hash(
        self,
    ) -> None:
        fake = self.roles.propose_revision(
            "companion-present-v1",
            "1",
            {
                "recentWork": [
                    self._role_book_item(
                        "recent:fake",
                        "声称完成了未验证工作",
                        "evidence:not-real",
                    )
                ]
            },
            proposed_by="agent:architect",
            created_at_ms=150,
        )
        fake_preview = self.control.activation_preview(
            {
                "roleId": "companion-present-v1",
                "roleVersion": "1",
                "revisionId": fake["revisionId"],
                "draftId": "",
                "traitIndexes": [],
                "capabilityIndexes": [],
            }
        )
        self.assertFalse(fake_preview["ok"])
        self.assertEqual(fake_preview["errorCode"], "revision_mismatch")

        evidence_id = str(self.daily_draft["sourceEvidenceIds"][0])
        real = self.roles.propose_revision(
            "companion-present-v1",
            "1",
            {
                "recentWork": [
                    self._role_book_item(
                        "recent:real",
                        "完成证据哈希校验",
                        evidence_id,
                    )
                ]
            },
            proposed_by="agent:architect",
            created_at_ms=160,
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE agent_memory_evidence
                SET content_text = 'tampered after proposal'
                WHERE evidence_id = ?
                """,
                (evidence_id,),
            )
        tampered = self.control.activation_preview(
            {
                "roleId": "companion-present-v1",
                "roleVersion": "1",
                "revisionId": real["revisionId"],
                "draftId": "",
                "traitIndexes": [],
                "capabilityIndexes": [],
            }
        )
        self.assertFalse(tampered["ok"])
        self.assertEqual(tampered["errorCode"], "revision_mismatch")

    @staticmethod
    def _role_book_item(
        item_id: str,
        text: str,
        evidence_id: str,
    ) -> dict[str, object]:
        return {
            "itemId": item_id,
            "text": text,
            "provenance": {
                "sourceType": "agent_proposal",
                "sourceId": "agent:architect",
                "observedAtMs": 150,
            },
            "evidenceIds": [evidence_id],
        }

    def _seed_daily_draft(self) -> dict[str, object]:
        evidence = self.evidence.record(
            source_kind="work_receipt",
            source_id="work:role-book-review",
            idempotency_key="work:role-book-review",
            text="完成角色书事务恢复验证",
            role_id="companion-present-v1",
            metadata={"accepted": True},
            provenance={
                "sourceType": "work_receipt",
                "sourceId": "work:role-book-review",
                "project": "rag-ime",
                "roleId": "companion-present-v1",
            },
            occurred_at_ms=120,
        )["evidence"]
        evidence_id = str(evidence["evidenceId"])
        draft = {
            "schemaVersion": "rag-ime.role-book-revision-draft.v1",
            "draftId": "role-book-draft:test-control",
            "status": "draft",
            "project": "rag-ime",
            "roleId": "companion-present-v1",
            "baseRoleVersion": "1",
            "sourceDigestId": "digest:test-control",
            "sourceEvidenceIds": [evidence_id],
            "patch": {
                "recentWork": [],
                "traitProposals": [
                    {
                        "text": "沟通时先给出具体例子",
                        "confidence": 0.9,
                        "sourceEvidenceIds": [evidence_id],
                        "reviewRequired": True,
                    },
                    {
                        "text": "不应被采用的另一条特征",
                        "confidence": 0.7,
                        "sourceEvidenceIds": [evidence_id],
                        "reviewRequired": True,
                    },
                ],
                "capabilityProposals": [
                    {
                        "text": "能修复 SQLite 事务恢复问题",
                        "confidence": 0.95,
                        "sourceEvidenceIds": [evidence_id],
                        "reviewRequired": True,
                    }
                ],
                "lessonProposals": [
                    {
                        "text": "不要把未经验证的推断写成事实",
                        "confidence": 0.92,
                        "sourceEvidenceIds": [evidence_id],
                        "reviewRequired": True,
                    }
                ],
                "commitmentProposals": [
                    {
                        "text": "下一轮先运行聚焦回归测试",
                        "confidence": 0.88,
                        "sourceEvidenceIds": [evidence_id],
                        "reviewRequired": True,
                    }
                ],
            },
            "policy": {
                "defaultApply": False,
                "safeAutoApplyFields": ["recentWork"],
                "reviewRequiredFields": [
                    "traits",
                    "capabilities",
                    "lessonsAndLimits",
                    "activeCommitments",
                ],
            },
            "proposalDiagnostics": {
                "status": "completed",
                "provider": "fixture",
                "inputChars": 128,
                "acceptedProposalCount": 4,
                "rejectedProposalCount": 0,
            },
            "createdAtMs": 130,
        }
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO personal_context_consolidation_runs(
                    run_id, project, role_id, role_version, idempotency_key,
                    status, window_start_ms, window_end_ms,
                    source_evidence_ids_json, output_json, created_at_ms,
                    completed_at_ms, updated_at_ms
                ) VALUES (
                    'run:test-control', 'rag-ime', 'companion-present-v1', '1',
                    'daily:test-control', 'succeeded', 100, 140, ?, ?, 140, 140, 140
                )
                """,
                (
                    json.dumps([evidence_id]),
                    json.dumps({"roleBookDraft": draft}, ensure_ascii=False),
                ),
            )
        return draft

    def _seed_empty_daily_draft(self) -> dict[str, object]:
        draft = {
            **self.daily_draft,
            "draftId": "role-book-draft:test-empty",
            "sourceDigestId": "digest:test-empty",
            "patch": {
                "recentWork": [],
                "traitProposals": [],
                "capabilityProposals": [],
                "lessonProposals": [],
                "commitmentProposals": [],
            },
            "proposalDiagnostics": {
                "status": "no_eligible_evidence",
                "provider": "fixture",
                "inputChars": 0,
                "acceptedProposalCount": 0,
                "rejectedProposalCount": 0,
            },
            "createdAtMs": 150,
        }
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO personal_context_consolidation_runs(
                    run_id, project, role_id, role_version, idempotency_key,
                    status, window_start_ms, window_end_ms,
                    source_evidence_ids_json, output_json, created_at_ms,
                    completed_at_ms, updated_at_ms
                ) VALUES (
                    'run:test-empty', 'rag-ime', 'companion-present-v1', '1',
                    'daily:test-empty', 'succeeded', 100, 150, ?, ?, 150, 150, 150
                )
                """,
                (
                    json.dumps(draft["sourceEvidenceIds"]),
                    json.dumps({"roleBookDraft": draft}, ensure_ascii=False),
                ),
            )
        return draft


if __name__ == "__main__":
    unittest.main()
