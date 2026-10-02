from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from pathlib import Path
from urllib.request import Request, urlopen

from rag_ime.debug_server import DebugImeService, DebugRequestHandler, DebugServerConfig
from rag_ime.contracts.json_schema import validate_contract
from rag_ime.local_sqlite_core import LocalSqliteCoreClient
from rag_ime.management_service import ManagementService, page_request
from rag_ime.management_work_contract import (
    ManagementWorkContract,
    ManagementWorkError,
    WorkExecution,
)
from rag_ime.memory_book_compiler import (
    memory_book_plan_from_compile_output,
    store_memory_book_plan,
)
from rag_ime.models import InputEvent
from rag_ime.model_registry import ModelDeployment, ModelRegistry
from rag_ime.retrieval_docs import rebuild_retrieval_docs
from rag_ime.runtime_config import RuntimeConfigResolver
from rag_ime.settings_store import ManagementSettingsStore


ROOT = Path(__file__).resolve().parents[1]


class ManagementWorkContractUnitTests(unittest.TestCase):
    def test_domain_mutations_enforce_foreign_key_cascades(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-work-foreign-keys-") as tmp:
            db_path = Path(tmp) / "work.sqlite"
            ManagementSettingsStore(db_path).initialize()
            with sqlite3.connect(db_path) as conn:
                conn.executescript(
                    """
                    CREATE TABLE work_contract_parent(id TEXT PRIMARY KEY);
                    CREATE TABLE work_contract_child(
                        parent_id TEXT NOT NULL
                            REFERENCES work_contract_parent(id) ON DELETE CASCADE
                    );
                    INSERT INTO work_contract_parent(id) VALUES ('parent:one');
                    INSERT INTO work_contract_child(parent_id) VALUES ('parent:one');
                    """
                )

            contract = ManagementWorkContract(db_path=db_path)
            domain = {"parentId": "parent:one"}
            revision = {"runtimeRevision": 1, "subjectRevision": "sha256:before"}
            preview = contract.create_preview(
                path_id="test.parent.delete",
                payload=domain,
                expected_revision=revision,
                required_confirm="delete",
                summary={"title": "删除父记录", "items": [], "risk": "R2"},
            )

            def delete_parent(conn: sqlite3.Connection) -> WorkExecution:
                conn.execute(
                    "DELETE FROM work_contract_parent WHERE id = ?",
                    (domain["parentId"],),
                )
                return WorkExecution(
                    result={"ok": True},
                    audit_action="test_parent_delete",
                    target_type="test_parent",
                    target_id=str(domain["parentId"]),
                )

            contract.execute_apply(
                path_id="test.parent.delete",
                payload=domain,
                preview_token=str(preview["previewToken"]),
                payload_sha256=str(preview["payloadSha256"]),
                confirm_text="delete",
                current_revision=lambda _conn: revision,
                executor=delete_parent,
            )

            with sqlite3.connect(db_path) as conn:
                child_count = int(
                    conn.execute("SELECT COUNT(*) FROM work_contract_child").fetchone()[0]
                )
                violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            self.assertEqual(child_count, 0)
            self.assertEqual(violations, [])

    def test_preview_apply_and_rollback_are_hash_revision_and_token_bound(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-work-contract-") as tmp:
            db_path = Path(tmp) / "work.sqlite"
            ManagementSettingsStore(db_path).initialize()
            contract = ManagementWorkContract(db_path=db_path)
            domain = {"taskId": "task:one", "action": "complete"}
            revision = {"runtimeRevision": 3, "subjectRevision": "sha256:before"}
            preview = contract.create_preview(
                path_id="planning.task.action",
                payload=domain,
                expected_revision=revision,
                required_confirm="apply",
                summary={"title": "更新任务", "items": ["完成 task:one"], "risk": "R1"},
            )
            validate_contract(preview, "management-work-preview.v1.json")

            with self.assertRaisesRegex(ManagementWorkError, "canonical SHA-256"):
                contract.execute_apply(
                    path_id="planning.task.action",
                    payload=domain,
                    preview_token=str(preview["previewToken"]),
                    payload_sha256="sha256:" + "0" * 64,
                    confirm_text="apply",
                    current_revision=lambda _conn: revision,
                    executor=lambda _conn: self.fail("hash mismatch must not execute"),
                )

            applied = contract.execute_apply(
                path_id="planning.task.action",
                payload=domain,
                preview_token=str(preview["previewToken"]),
                payload_sha256=str(preview["payloadSha256"]),
                confirm_text="apply",
                current_revision=lambda _conn: revision,
                executor=lambda _conn: WorkExecution(
                    result={"ok": True, "eventId": "event:one"},
                    audit_action="planning_task_complete",
                    target_type="task",
                    target_id="task:one",
                    rollback_available=True,
                    rollback_path_id="planning.taskEvent.undo",
                    rollback_confirm="undo",
                    rollback_authority={"eventId": "event:one"},
                    rollback_data={"afterRevision": "sha256:after"},
                ),
            )

            self.assertTrue(applied["ok"])
            validate_contract(applied, "management-work-receipt.v1.json")
            self.assertTrue(applied["rollbackAvailable"])
            self.assertEqual(applied["pathId"], "planning.task.action")
            with sqlite3.connect(db_path) as conn:
                preview_row = conn.execute(
                    "SELECT preview_token_sha256, status FROM management_work_previews"
                ).fetchone()
                receipt_row = conn.execute(
                    "SELECT rollback_token_sha256 FROM management_work_receipts WHERE receipt_id = ?",
                    (applied["receiptId"],),
                ).fetchone()
            self.assertNotEqual(preview_row[0], preview["previewToken"])
            self.assertNotEqual(receipt_row[0], applied["rollbackToken"])
            self.assertEqual(preview_row[1], "applied")

            rolled_back = contract.execute_rollback(
                path_id="planning.taskEvent.undo",
                receipt_id=str(applied["receiptId"]),
                rollback_token=str(applied["rollbackToken"]),
                payload_sha256=str(applied["payloadSha256"]),
                confirm_text="undo",
                expected_apply_path_id="planning.task.action",
                executor=lambda _conn, receipt: WorkExecution(
                    result={"ok": True, "eventId": receipt.rollback_authority["eventId"]},
                    audit_action="planning_task_event_undo",
                    target_type="task",
                    target_id="task:one",
                ),
            )

            self.assertFalse(rolled_back["rollbackAvailable"])
            validate_contract(rolled_back, "management-work-receipt.v1.json")
            self.assertEqual(rolled_back["rollbackOfReceiptId"], applied["receiptId"])
            with self.assertRaisesRegex(ManagementWorkError, "already rolled back"):
                contract.execute_rollback(
                    path_id="planning.taskEvent.undo",
                    receipt_id=str(applied["receiptId"]),
                    rollback_token=str(applied["rollbackToken"]),
                    payload_sha256=str(applied["payloadSha256"]),
                    confirm_text="undo",
                    expected_apply_path_id="planning.task.action",
                    executor=lambda _conn, _receipt: self.fail("receipt reuse must not execute"),
                )

    def test_preview_fails_closed_after_expiry_or_revision_change(self) -> None:
        clock = [1_000]
        with tempfile.TemporaryDirectory(prefix="rag-ime-work-expiry-") as tmp:
            db_path = Path(tmp) / "work.sqlite"
            ManagementSettingsStore(db_path).initialize()
            contract = ManagementWorkContract(
                db_path=db_path,
                preview_ttl_ms=1_000,
                clock_ms=lambda: clock[0],
            )
            preview = contract.create_preview(
                path_id="planning.task.action",
                payload={"taskId": "task:one", "action": "complete"},
                expected_revision={"runtimeRevision": 1, "subjectRevision": "one"},
                required_confirm="apply",
                summary={"title": "更新任务", "items": [], "risk": "R1"},
            )
            clock[0] = 2_001
            with self.assertRaisesRegex(ManagementWorkError, "expired"):
                contract.execute_apply(
                    path_id="planning.task.action",
                    payload={"taskId": "task:one", "action": "complete"},
                    preview_token=str(preview["previewToken"]),
                    payload_sha256=str(preview["payloadSha256"]),
                    confirm_text="apply",
                    current_revision=lambda _conn: {"runtimeRevision": 1, "subjectRevision": "two"},
                    executor=lambda _conn: self.fail("expired preview must not execute"),
                )


class PlanningWorkContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-planning-work-")
        self.db_path = Path(self.tmp.name) / "planning.sqlite"
        self.settings = ManagementSettingsStore(self.db_path)
        self.settings.initialize()
        resolver = RuntimeConfigResolver(self.settings, environ={})
        self.management = ManagementService(
            db_path=self.db_path,
            project="wisdom-weasel-rag-ime",
            repo_root=ROOT,
            settings_store=self.settings,
            health_provider=lambda: {"ok": True},
            input_source_provider=lambda: {},
            predictor_provider=lambda: {},
            runtime_config_provider=resolver.resolve,
        )

    def tearDown(self) -> None:
        self.management.close()
        self.tmp.cleanup()

    def _preview(self, kind: str, payload: dict[str, object]) -> dict[str, object]:
        return self.management.planning_mutation_preview(
            {
                "kind": kind,
                "payload": payload,
                "expectedRuntimeRevision": self.management.revision().runtime_revision,
            }
        )

    def test_task_save_action_and_undo_return_real_receipts(self) -> None:
        task_payload = {
            "taskId": "task:contract",
            "date": "2026-07-14",
            "title": "补齐 WorkContract",
            "priority": 2,
        }
        preview = self._preview("task.save", task_payload)
        saved = self.management.planning_apply_task_save(
            {
                **task_payload,
                "expectedRuntimeRevision": preview["expectedRevision"]["runtimeRevision"],
                "previewToken": preview["previewToken"],
                "payloadSha256": preview["payloadSha256"],
                "confirmText": "apply",
            }
        )

        self.assertTrue(saved["ok"], saved)
        self.assertEqual(saved["pathId"], "planning.task.save")
        self.assertEqual(saved["task"]["id"], "task:contract")
        self.assertTrue(saved["rollbackAvailable"])

        action_preview = self._preview(
            "task.action",
            {"taskId": "task:contract", "action": "complete"},
        )
        applied = self.management.planning_apply_task_action(
            {
                "taskId": "task:contract",
                "action": "complete",
                "expectedRuntimeRevision": action_preview["expectedRevision"]["runtimeRevision"],
                "previewToken": action_preview["previewToken"],
                "payloadSha256": action_preview["payloadSha256"],
                "confirmText": "apply",
            }
        )
        denied = self.management.planning_undo_task_event_contract(
            {
                "eventId": applied["eventId"],
                "receiptId": applied["receiptId"],
                "rollbackToken": applied["rollbackToken"],
                "payloadSha256": applied["payloadSha256"],
                "confirmText": "rollback",
            }
        )
        undone = self.management.planning_undo_task_event_contract(
            {
                "eventId": applied["eventId"],
                "receiptId": applied["receiptId"],
                "rollbackToken": applied["rollbackToken"],
                "payloadSha256": applied["payloadSha256"],
                "confirmText": "undo",
            }
        )

        self.assertEqual(denied["errorCode"], "confirmation_mismatch")
        self.assertTrue(undone["ok"])
        self.assertEqual(undone["task"]["status"], "todo")
        self.assertEqual(undone["rollbackOfReceiptId"], applied["receiptId"])

    def test_new_task_save_can_rollback_only_while_snapshot_is_current(self) -> None:
        task_payload = {
            "taskId": "task:rollback",
            "date": "2026-07-14",
            "title": "可回滚保存",
        }
        preview = self._preview("task.save", task_payload)
        saved = self.management.planning_apply_task_save(
            {
                **task_payload,
                "expectedRuntimeRevision": preview["expectedRevision"]["runtimeRevision"],
                "previewToken": preview["previewToken"],
                "payloadSha256": preview["payloadSha256"],
                "confirmText": "apply",
            }
        )
        rollback = self.management.planning_mutation_rollback(
            {
                "receiptId": saved["receiptId"],
                "rollbackToken": saved["rollbackToken"],
                "payloadSha256": saved["payloadSha256"],
                "confirmText": "rollback",
            }
        )

        self.assertTrue(rollback["ok"], rollback)
        self.assertTrue(rollback["deleted"])
        dashboard = self.management.planning_dashboard(plan_date="2026-07-14")
        self.assertEqual(dashboard["tasks"], [])

    def test_goal_save_update_and_rollback_use_real_goal_snapshots(self) -> None:
        goal_payload = {
            "goalId": "goal:contract",
            "title": "完成控制中心迁移",
            "detail": "先接通真实规划写入",
            "horizon": "long_term",
            "status": "active",
            "priority": 2,
            "targetDate": "2026-07-31",
            "project": "wisdom-weasel-rag-ime",
        }
        create_preview = self._preview("goal.save", goal_payload)
        self.assertIn("时间范围: 长期目标", create_preview["summary"]["items"])
        self.assertIn("状态: 进行中", create_preview["summary"]["items"])
        self.assertNotIn("long_term", " ".join(create_preview["summary"]["items"]))
        created = self.management.planning_apply_goal_save(
            {
                **goal_payload,
                "expectedRuntimeRevision": create_preview["expectedRevision"]["runtimeRevision"],
                "previewToken": create_preview["previewToken"],
                "payloadSha256": create_preview["payloadSha256"],
                "confirmText": "apply",
            }
        )

        self.assertTrue(created["ok"], created)
        self.assertEqual(created["pathId"], "planning.goal.save")
        self.assertEqual(created["goal"]["id"], "goal:contract")
        self.assertEqual(created["goal"]["targetDate"], "2026-07-31")
        self.assertTrue(created["rollbackAvailable"])

        updated_payload = {
            **goal_payload,
            "title": "完成真实控制中心切换",
            "horizon": "medium_term",
            "priority": 3,
        }
        update_preview = self._preview("goal.save", updated_payload)
        updated = self.management.planning_apply_goal_save(
            {
                **updated_payload,
                "expectedRuntimeRevision": update_preview["expectedRevision"]["runtimeRevision"],
                "previewToken": update_preview["previewToken"],
                "payloadSha256": update_preview["payloadSha256"],
                "confirmText": "apply",
            }
        )
        restored = self.management.planning_mutation_rollback(
            {
                "receiptId": updated["receiptId"],
                "rollbackToken": updated["rollbackToken"],
                "payloadSha256": updated["payloadSha256"],
                "confirmText": "rollback",
            }
        )

        self.assertTrue(restored["ok"], restored)
        self.assertTrue(restored["restored"])
        dashboard = self.management.planning_dashboard(plan_date="2026-07-14")
        self.assertEqual(dashboard["goals"][0]["title"], "完成控制中心迁移")
        self.assertEqual(dashboard["goals"][0]["horizon"], "long_term")

        deleted = self.management.planning_mutation_rollback(
            {
                "receiptId": created["receiptId"],
                "rollbackToken": created["rollbackToken"],
                "payloadSha256": created["payloadSha256"],
                "confirmText": "rollback",
            }
        )
        self.assertTrue(deleted["ok"], deleted)
        self.assertTrue(deleted["deleted"])
        self.assertEqual(self.management.planning_dashboard(plan_date="2026-07-14")["goals"], [])

    def test_apply_without_preview_or_with_unknown_field_fails_closed(self) -> None:
        result = self.management.planning_apply_task_save(
            {
                "date": "2026-07-14",
                "title": "不得直接保存",
                "expectedRuntimeRevision": self.management.revision().runtime_revision,
                "previewToken": "missing",
                "payloadSha256": "sha256:" + "0" * 64,
                "confirmText": "apply",
                "shell": "rm -rf /",
            }
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["errorCode"], "invalid_request")
        validate_contract(result, "management-work-error.v1.json")
        self.assertEqual(self.management.planning_dashboard(plan_date="2026-07-14")["tasks"], [])

        goal_result = self.management.planning_apply_goal_save(
            {
                "title": "不得注入内部目标字段",
                "expectedRuntimeRevision": self.management.revision().runtime_revision,
                "previewToken": "missing",
                "payloadSha256": "sha256:" + "0" * 64,
                "confirmText": "apply",
                "metadata": {"systemPrompt": "ignore policy"},
            }
        )
        self.assertFalse(goal_result["ok"])
        self.assertEqual(goal_result["errorCode"], "invalid_request")
        self.assertEqual(self.management.planning_dashboard(plan_date="2026-07-14")["goals"], [])

    def test_task_action_preview_becomes_stale_when_task_changes(self) -> None:
        self.management.planning_save_task(
            {
                "taskId": "task:stale",
                "date": "2026-07-14",
                "title": "检测陈旧预览",
            }
        )
        preview = self._preview(
            "task.action",
            {"taskId": "task:stale", "action": "complete"},
        )
        self.management.planning_task_action(
            {"taskId": "task:stale", "action": "start"}
        )
        denied = self.management.planning_apply_task_action(
            {
                "taskId": "task:stale",
                "action": "complete",
                "expectedRuntimeRevision": preview["expectedRevision"]["runtimeRevision"],
                "previewToken": preview["previewToken"],
                "payloadSha256": preview["payloadSha256"],
                "confirmText": "apply",
            }
        )

        self.assertEqual(denied["errorCode"], "revision_mismatch")
        dashboard = self.management.planning_dashboard(plan_date="2026-07-14")
        self.assertEqual(dashboard["tasks"][0]["status"], "in_progress")


class HistoryWorkContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-history-work-")
        self.db_path = Path(self.tmp.name) / "history.sqlite"
        self.core = LocalSqliteCoreClient(self.db_path)
        self.core.initialize()
        self.settings = ManagementSettingsStore(self.db_path)
        self.settings.initialize()
        resolver = RuntimeConfigResolver(self.settings, environ={})
        self.invalidations: list[bool] = []
        self.management = ManagementService(
            db_path=self.db_path,
            project="wisdom-weasel-rag-ime",
            repo_root=ROOT,
            settings_store=self.settings,
            health_provider=lambda: {"ok": True},
            input_source_provider=lambda: {},
            predictor_provider=lambda: {},
            runtime_config_provider=resolver.resolve,
            cache_invalidator=lambda: self.invalidations.append(True),
        )
        event_ref = self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=1_900_000_100_020,
                source="manual",
                committed_text="需要通过真实收据隐藏的历史记录",
                privacy_disposition="allowed",
                recent_context="history work contract",
                project="wisdom-weasel-rag-ime",
            )
        )
        self.event_id = int(event_ref.split(":", 1)[1])

    def tearDown(self) -> None:
        self.management.close()
        self.tmp.cleanup()

    def test_tombstone_apply_and_rollback_change_the_real_history_projection(self) -> None:
        before = self.management.history_page(page_request({"limit": 20}))
        preview = self.management.history_tombstone_preview(
            {
                "eventId": self.event_id,
                "reason": "user-requested-hide",
                "expectedRuntimeRevision": self.management.revision().runtime_revision,
            }
        )
        applied = self.management.history_tombstone_apply(
            {
                "eventId": self.event_id,
                "reason": "user-requested-hide",
                "expectedRuntimeRevision": preview["expectedRevision"]["runtimeRevision"],
                "previewToken": preview["previewToken"],
                "payloadSha256": preview["payloadSha256"],
                "confirmText": "apply",
            }
        )
        hidden = self.management.history_page(page_request({"limit": 20}))
        rolled_back = self.management.history_tombstone_rollback(
            {
                "receiptId": applied["receiptId"],
                "rollbackToken": applied["rollbackToken"],
                "payloadSha256": applied["payloadSha256"],
                "confirmText": "rollback",
            }
        )
        restored = self.management.history_page(page_request({"limit": 20}))

        self.assertEqual([item["id"] for item in before["items"]], [self.event_id])
        self.assertTrue(applied["ok"], applied)
        self.assertEqual(applied["pathId"], "history.tombstone.apply")
        self.assertTrue(applied["rollbackAvailable"])
        self.assertEqual(hidden["items"], [])
        self.assertTrue(rolled_back["ok"], rolled_back)
        self.assertEqual(rolled_back["pathId"], "history.tombstone.rollback")
        self.assertEqual([item["id"] for item in restored["items"]], [self.event_id])
        self.assertEqual(len(self.invalidations), 2)

    def test_tombstone_apply_fails_closed_without_bound_preview(self) -> None:
        denied = self.management.history_tombstone_apply(
            {
                "eventId": self.event_id,
                "reason": "unbound",
                "expectedRuntimeRevision": self.management.revision().runtime_revision,
                "previewToken": "missing",
                "payloadSha256": "sha256:" + "0" * 64,
                "confirmText": "apply",
                "shell": "rm -rf /",
            }
        )

        self.assertFalse(denied["ok"])
        self.assertEqual(denied["errorCode"], "invalid_request")
        current = self.management.history_page(page_request({"limit": 20}))
        self.assertEqual([item["id"] for item in current["items"]], [self.event_id])


class MemoryBookArchiveWorkContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-memory-book-work-")
        self.db_path = Path(self.tmp.name) / "memory.sqlite"
        self.core = LocalSqliteCoreClient(self.db_path)
        self.core.initialize()
        self.settings = ManagementSettingsStore(self.db_path)
        self.settings.initialize()
        resolver = RuntimeConfigResolver(self.settings, environ={})
        self.invalidations: list[bool] = []
        self.management = ManagementService(
            db_path=self.db_path,
            project="wisdom-weasel-rag-ime",
            repo_root=ROOT,
            settings_store=self.settings,
            health_provider=lambda: {"ok": True},
            input_source_provider=lambda: {},
            predictor_provider=lambda: {},
            runtime_config_provider=resolver.resolve,
            cache_invalidator=lambda: self.invalidations.append(True),
        )
        self.book_id = "book:control-center"
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            conn.execute(
                """
                INSERT INTO memory_books(
                    book_id, book_type, book_key, title, summary, normalized_text,
                    project, status, confidence, quality_score, created_at_ms,
                    updated_at_ms, last_active_at_ms, metadata_json
                ) VALUES (?, 'topic', 'control-center', '控制中心迁移', '真实归档与恢复',
                          '控制中心迁移 真实归档与恢复', ?, 'approved', 0.9, 0.9,
                          100, 100, 100, '{}')
                """,
                (self.book_id, "wisdom-weasel-rag-ime"),
            )
            rebuild_retrieval_docs(conn, project="")

    def tearDown(self) -> None:
        self.management.close()
        self.tmp.cleanup()

    def test_archive_apply_and_rollback_update_the_real_retrieval_projection(self) -> None:
        preview = self.management.memory_book_archive_preview(
            {
                "bookId": self.book_id,
                "archived": True,
                "reason": "user-requested-archive",
                "expectedRuntimeRevision": self.management.revision().runtime_revision,
            }
        )
        applied = self.management.memory_book_archive_apply(
            {
                "bookId": self.book_id,
                "archived": True,
                "reason": "user-requested-archive",
                "expectedRuntimeRevision": preview["expectedRevision"]["runtimeRevision"],
                "previewToken": preview["previewToken"],
                "payloadSha256": preview["payloadSha256"],
                "confirmText": "apply",
            }
        )
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            archived_status = conn.execute(
                "SELECT status FROM memory_books WHERE book_id = ?",
                (self.book_id,),
            ).fetchone()[0]
            archived_doc = conn.execute(
                "SELECT metadata_json FROM memory_retrieval_docs WHERE source_id = ?",
                (self.book_id,),
            ).fetchone()
        rolled_back = self.management.memory_book_archive_rollback(
            {
                "receiptId": applied["receiptId"],
                "rollbackToken": applied["rollbackToken"],
                "payloadSha256": applied["payloadSha256"],
                "confirmText": "rollback",
            }
        )
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            restored_status = conn.execute(
                "SELECT status FROM memory_books WHERE book_id = ?",
                (self.book_id,),
            ).fetchone()[0]
            restored_doc_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM memory_retrieval_docs WHERE source_id = ?",
                    (self.book_id,),
                ).fetchone()[0]
            )

        self.assertTrue(preview["ok"], preview)
        self.assertNotIn(self.book_id, preview["summary"]["items"])
        self.assertTrue(applied["ok"], applied)
        self.assertEqual(applied["pathId"], "memory.book.archive.apply")
        self.assertTrue(applied["rollbackAvailable"])
        self.assertEqual(archived_status, "archived")
        self.assertTrue(json.loads(archived_doc[0])["archived"])
        self.assertTrue(rolled_back["ok"], rolled_back)
        self.assertEqual(rolled_back["pathId"], "memory.book.archive.rollback")
        self.assertEqual(restored_status, "approved")
        self.assertEqual(restored_doc_count, 1)
        self.assertEqual(len(self.invalidations), 2)

    def test_archive_apply_fails_closed_after_the_book_changes(self) -> None:
        preview = self.management.memory_book_archive_preview(
            {
                "bookId": self.book_id,
                "archived": True,
                "expectedRuntimeRevision": self.management.revision().runtime_revision,
            }
        )
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            conn.execute(
                "UPDATE memory_books SET title = '已被其他操作更新', updated_at_ms = 200 WHERE book_id = ?",
                (self.book_id,),
            )
        denied = self.management.memory_book_archive_apply(
            {
                "bookId": self.book_id,
                "archived": True,
                "reason": "control_center_archive",
                "expectedRuntimeRevision": preview["expectedRevision"]["runtimeRevision"],
                "previewToken": preview["previewToken"],
                "payloadSha256": preview["payloadSha256"],
                "confirmText": "apply",
            }
        )

        self.assertFalse(denied["ok"])
        self.assertEqual(denied["errorCode"], "revision_mismatch")
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            status = conn.execute(
                "SELECT status FROM memory_books WHERE book_id = ?",
                (self.book_id,),
            ).fetchone()[0]
        self.assertEqual(status, "approved")


class KnowledgeDatabaseWorkContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-knowledge-work-")
        self.db_path = Path(self.tmp.name) / "knowledge.sqlite"
        self.core = LocalSqliteCoreClient(self.db_path)
        self.core.initialize()
        self.service = DebugImeService(DebugServerConfig(db_path=self.db_path, seed_if_empty=False))

    def tearDown(self) -> None:
        self.service.agent.close()
        self.service.management.close()
        self.tmp.cleanup()

    def _draft(self) -> dict[str, object]:
        event_ref = self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=1_900_000_100_045,
                source="manual",
                committed_text="WorkContract 知识库草案",
                privacy_disposition="allowed",
                recent_context="Memory Book",
                project="wisdom-weasel-rag-ime",
            )
        )
        event_id = int(event_ref.split(":", 1)[1])
        plan = memory_book_plan_from_compile_output(
            {
                "dailyBooks": [
                    {
                        "bookKey": "2026-07-14",
                        "title": "WorkContract",
                        "summary": "显式预览后应用。",
                        "sourceEventIds": [event_id],
                    }
                ]
            },
            project="wisdom-weasel-rag-ime",
            provider="deepseek",
            model="deepseek-test",
        )
        with self.core._connect() as conn:  # type: ignore[attr-defined]
            store_memory_book_plan(conn, plan)
        return plan

    def test_database_apply_and_rollback_are_bound_to_run_receipt_and_hash(self) -> None:
        plan = self._draft()
        preview = self.service.knowledge_workbench_database_apply_preview(
            {"runId": plan["runId"]}
        )
        applied = self.service.knowledge_workbench_database_apply_contract(
            {
                "runId": plan["runId"],
                "confirm": "apply",
                "previewToken": preview["previewToken"],
                "payloadSha256": preview["payloadSha256"],
                "expectedRuntimeRevision": preview["expectedRevision"]["runtimeRevision"],
            }
        )
        wrong_run = self.service.knowledge_workbench_database_rollback_contract(
            {
                "runId": "memory_book_wrong",
                "confirm": "rollback",
                "receiptId": applied["receiptId"],
                "rollbackToken": applied["rollbackToken"],
                "payloadSha256": applied["payloadSha256"],
            }
        )
        rolled_back = self.service.knowledge_workbench_database_rollback_contract(
            {
                "runId": plan["runId"],
                "confirm": "rollback",
                "receiptId": applied["receiptId"],
                "rollbackToken": applied["rollbackToken"],
                "payloadSha256": applied["payloadSha256"],
            }
        )

        self.assertTrue(preview["ok"])
        self.assertEqual(preview["pathId"], "knowledge.database.apply")
        self.assertTrue(applied["ok"])
        self.assertEqual(applied["run"]["status"], "applied")
        self.assertEqual(applied["rollbackAuthority"], {"runId": plan["runId"]})
        self.assertEqual(wrong_run["errorCode"], "rollback_authority_mismatch")
        self.assertTrue(rolled_back["ok"])
        self.assertEqual(rolled_back["run"]["status"], "rolled_back")

    def test_database_apply_rejects_wrong_hash_without_mutating_run(self) -> None:
        plan = self._draft()
        preview = self.service.knowledge_workbench_database_apply_preview(
            {"runId": plan["runId"]}
        )
        denied = self.service.knowledge_workbench_database_apply_contract(
            {
                "runId": plan["runId"],
                "confirm": "apply",
                "previewToken": preview["previewToken"],
                "payloadSha256": "sha256:" + "0" * 64,
                "expectedRuntimeRevision": preview["expectedRevision"]["runtimeRevision"],
            }
        )
        review = self.service.agent_memory_maintenance_run(
            {"runId": plan["runId"], "project": "wisdom-weasel-rag-ime"}
        )

        self.assertEqual(denied["errorCode"], "payload_hash_mismatch")
        self.assertTrue(review["canApply"])
        self.assertEqual(review["run"]["status"], "draft")

    def test_database_apply_preview_becomes_stale_after_draft_edit(self) -> None:
        plan = self._draft()
        preview = self.service.knowledge_workbench_database_apply_preview(
            {"runId": plan["runId"]}
        )
        review = self.service.agent_memory_maintenance_run(
            {"runId": plan["runId"], "project": "wisdom-weasel-rag-ime"}
        )
        diff_id = review["run"]["changes"][0]["diffId"]
        self.service.knowledge_workbench_database_draft_edit(
            {"runId": plan["runId"], "diffId": diff_id, "selected": False}
        )
        denied = self.service.knowledge_workbench_database_apply_contract(
            {
                "runId": plan["runId"],
                "confirm": "apply",
                "previewToken": preview["previewToken"],
                "payloadSha256": preview["payloadSha256"],
                "expectedRuntimeRevision": preview["expectedRevision"]["runtimeRevision"],
            }
        )

        self.assertEqual(denied["errorCode"], "revision_mismatch")
        current = self.service.agent_memory_maintenance_run(
            {"runId": plan["runId"], "project": "wisdom-weasel-rag-ime"}
        )
        self.assertEqual(current["run"]["status"], "draft")

    def test_http_routes_enforce_preview_before_knowledge_apply(self) -> None:
        plan = self._draft()

        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = ROOT / "debug"
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            denied = _post_json(
                server.server_port,
                "/api/knowledge/database/apply",
                {"runId": plan["runId"], "confirm": "apply"},
            )
            preview = _post_json(
                server.server_port,
                "/api/knowledge/database/apply-preview",
                {"runId": plan["runId"]},
            )
            applied = _post_json(
                server.server_port,
                "/api/knowledge/database/apply",
                {
                    "runId": plan["runId"],
                    "confirm": "apply",
                    "previewToken": preview["previewToken"],
                    "payloadSha256": preview["payloadSha256"],
                    "expectedRuntimeRevision": preview["expectedRevision"]["runtimeRevision"],
                },
            )
            rolled_back = _post_json(
                server.server_port,
                "/api/knowledge/database/rollback",
                {
                    "runId": plan["runId"],
                    "confirm": "rollback",
                    "receiptId": applied["receiptId"],
                    "rollbackToken": applied["rollbackToken"],
                    "payloadSha256": applied["payloadSha256"],
                },
            )
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(denied["errorCode"], "invalid_request")
        self.assertTrue(applied["ok"])
        self.assertTrue(rolled_back["ok"])


class ConfigurationSettingsWorkContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-configuration-work-")
        self.db_path = Path(self.tmp.name) / "configuration.sqlite"
        self.service = DebugImeService(
            DebugServerConfig(db_path=self.db_path, seed_if_empty=False)
        )

    def tearDown(self) -> None:
        self.service.agent.close()
        self.service.management.close()
        self.tmp.cleanup()

    def _width_change(self) -> tuple[int, int]:
        before = int(self.service.settings()["settings"]["display"]["maxWidth"])
        after = before + 20 if before <= 740 else before - 20
        return before, after

    def test_settings_apply_and_rollback_use_bound_receipts_and_real_store(self) -> None:
        before, after = self._width_change()
        current_font_size = self.service.settings_store.get_settings()["display"][
            "candidateFontSize"
        ]
        changes = {
            "display.candidateFontSize": current_font_size,
            "display.maxWidth": after,
        }
        preview = self.service.configuration_settings_preview(
            {
                "changes": changes,
                "expectedRuntimeRevision": self.service.management.revision().runtime_revision,
            }
        )
        applied = self.service.configuration_settings_apply(
            {
                "changes": changes,
                "expectedRuntimeRevision": preview["expectedRevision"]["runtimeRevision"],
                "previewToken": preview["previewToken"],
                "payloadSha256": preview["payloadSha256"],
                "confirmText": "apply",
            }
        )
        persisted = self.service.settings_store.get_settings()
        rolled_back = self.service.configuration_settings_rollback(
            {
                "receiptId": applied["receiptId"],
                "rollbackToken": applied["rollbackToken"],
                "payloadSha256": applied["payloadSha256"],
                "confirmText": "rollback",
            }
        )
        restored = self.service.settings_store.get_settings()

        validate_contract(preview, "management-work-preview.v1.json")
        validate_contract(applied, "management-work-receipt.v1.json")
        validate_contract(rolled_back, "management-work-receipt.v1.json")
        self.assertEqual(preview["pathId"], "configuration.settings.apply")
        self.assertEqual(persisted["display"]["maxWidth"], after)
        self.assertEqual(applied["rollbackAuthority"], {"settingKeys": ["display.maxWidth"]})
        self.assertEqual(applied["restartComponents"], ["squirrel"])
        self.assertEqual(restored["display"]["maxWidth"], before)
        with sqlite3.connect(self.db_path) as conn:
            stored_audit_id = conn.execute(
                "SELECT audit_id FROM management_settings WHERE key = 'display'"
            ).fetchone()[0]
            work_audits = conn.execute(
                """
                SELECT action
                FROM management_audit_log
                WHERE action IN (
                    'configuration_settings_apply',
                    'configuration_settings_rollback'
                )
                ORDER BY id
                """
            ).fetchall()
        self.assertEqual(stored_audit_id, rolled_back["auditId"])
        self.assertEqual(
            [row[0] for row in work_audits],
            ["configuration_settings_apply", "configuration_settings_rollback"],
        )

    def test_agent_defaults_apply_and_reopen_from_the_sqlite_authority(self) -> None:
        initial_defaults = ManagementSettingsStore(self.db_path).get_settings()["agent"]["defaults"]
        changes = {"agent.defaults.executionMode": "workspace_managed"}
        preview = self.service.configuration_settings_preview(
            {
                "changes": changes,
                "expectedRuntimeRevision": self.service.management.revision().runtime_revision,
            }
        )
        applied = self.service.configuration_settings_apply(
            {
                "changes": changes,
                "expectedRuntimeRevision": preview["expectedRevision"]["runtimeRevision"],
                "previewToken": preview["previewToken"],
                "payloadSha256": preview["payloadSha256"],
                "confirmText": "apply",
            }
        )

        reopened = ManagementSettingsStore(self.db_path).get_settings()

        validate_contract(applied, "management-work-receipt.v1.json")
        self.assertEqual(
            reopened["agent"]["defaults"],
            {**initial_defaults, "executionMode": "workspace_managed"},
        )

    def test_settings_apply_rejects_a_preview_staled_by_another_setting_change(self) -> None:
        _before, after = self._width_change()
        preview = self.service.configuration_settings_preview(
            {
                "changes": {"display.maxWidth": after},
                "expectedRuntimeRevision": self.service.management.revision().runtime_revision,
            }
        )
        self.service.settings_store.update_settings(
            {"display.candidateFontSize": 17},
            updated_by="concurrent-test",
        )
        denied = self.service.configuration_settings_apply(
            {
                "changes": {"display.maxWidth": after},
                "expectedRuntimeRevision": preview["expectedRevision"]["runtimeRevision"],
                "previewToken": preview["previewToken"],
                "payloadSha256": preview["payloadSha256"],
                "confirmText": "apply",
            }
        )

        validate_contract(denied, "management-work-error.v1.json")
        self.assertEqual(denied["errorCode"], "revision_mismatch")
        self.assertNotEqual(
            denied["currentRevision"]["runtimeRevision"],
            preview["expectedRevision"]["runtimeRevision"],
        )
        self.assertNotEqual(
            self.service.settings_store.get_settings()["display"]["maxWidth"],
            after,
        )

    def test_predictor_settings_preview_validates_registered_artifact_and_cross_field_contract(self) -> None:
        model_path = Path(self.tmp.name) / "minimind-ime-v2"
        model_path.mkdir()
        (model_path / "config.json").write_text(
            '{"model_type":"minimind"}',
            encoding="utf-8",
        )
        (model_path / "tokenizer.json").write_text("{}", encoding="utf-8")
        registry_path = Path(self.tmp.name) / "models.json"
        registry = ModelRegistry(registry_path)
        registry.register(
            ModelDeployment(
                model_id="minimind-ime-v2",
                path=str(model_path),
                format="mlx",
                fingerprint="",
                profile="minimind_ime_v2",
                runtime="mlx",
                prompt_mode="base-completion",
            ),
            activate=True,
        )
        revision = self.service.management.revision().runtime_revision

        with patch.dict(
            os.environ,
            {"RAG_IME_MODEL_REGISTRY": str(registry_path)},
        ):
            preview = self.service.configuration_settings_preview(
                {
                    "changes": {
                        "models.modelId": "minimind-ime-v2",
                        "models.path": str(model_path),
                        "models.maxTokens": 12,
                        "models.temperature": 0.2,
                        "models.topP": 0.9,
                    },
                    "expectedRuntimeRevision": revision,
                }
            )
            denied = self.service.configuration_settings_preview(
                {
                    "changes": {"models.promptMode": "chat-json"},
                    "expectedRuntimeRevision": revision,
                }
            )

        validate_contract(preview, "management-work-preview.v1.json")
        validate_contract(denied, "management-work-error.v1.json")
        self.assertIn("需要重载: predictor", preview["summary"]["items"])
        self.assertEqual(denied["errorCode"], "invalid_request")
        self.assertIn("base-completion", json.dumps(denied, ensure_ascii=False))

    def test_settings_preview_rejects_secret_values_without_echoing_them(self) -> None:
        secret = "must-never-appear-in-a-receipt"
        denied = self.service.configuration_settings_preview(
            {
                "changes": {"managementSecurity.token": secret},
                "expectedRuntimeRevision": self.service.management.revision().runtime_revision,
            }
        )

        validate_contract(denied, "management-work-error.v1.json")
        self.assertEqual(denied["errorCode"], "unsupported_mutation")
        self.assertNotIn(secret, json.dumps(denied, ensure_ascii=False))
        persisted = self.service.settings_store.get_settings(include_sensitive=True)
        self.assertNotEqual(persisted["managementSecurity"].get("token"), secret)

    def test_settings_preview_rejects_transport_metadata_hidden_inside_changes(self) -> None:
        _before, after = self._width_change()
        denied = self.service.configuration_settings_preview(
            {
                "changes": {
                    "settings": {"display.maxWidth": after},
                    "updatedBy": "spoofed-audit-authority",
                },
                "expectedRuntimeRevision": self.service.management.revision().runtime_revision,
            }
        )

        validate_contract(denied, "management-work-error.v1.json")
        self.assertEqual(denied["errorCode"], "invalid_request")
        self.assertNotEqual(
            self.service.settings_store.get_settings()["display"]["maxWidth"],
            after,
        )

    def test_http_routes_expose_the_settings_work_contract(self) -> None:
        before, after = self._width_change()

        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = ROOT / "debug"
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            preview = _post_json(
                server.server_port,
                "/api/settings/preview",
                {
                    "changes": {"display.maxWidth": after},
                    "expectedRuntimeRevision": self.service.management.revision().runtime_revision,
                },
            )
            applied = _post_json(
                server.server_port,
                "/api/settings/apply",
                {
                    "changes": {"display.maxWidth": after},
                    "expectedRuntimeRevision": preview["expectedRevision"]["runtimeRevision"],
                    "previewToken": preview["previewToken"],
                    "payloadSha256": preview["payloadSha256"],
                    "confirmText": "apply",
                },
            )
            rolled_back = _post_json(
                server.server_port,
                "/api/settings/rollback",
                {
                    "receiptId": applied["receiptId"],
                    "rollbackToken": applied["rollbackToken"],
                    "payloadSha256": applied["payloadSha256"],
                    "confirmText": "rollback",
                },
            )
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertTrue(applied["ok"], applied)
        self.assertTrue(rolled_back["ok"], rolled_back)
        self.assertEqual(
            self.service.settings_store.get_settings()["display"]["maxWidth"],
            before,
        )


def _post_json(port: int, path: str, payload: dict[str, object]) -> dict[str, object]:
    request = Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        parsed = json.loads(response.read().decode("utf-8"))
    if not isinstance(parsed, dict):
        raise AssertionError("management route returned a non-object response")
    return parsed


if __name__ == "__main__":
    unittest.main()
