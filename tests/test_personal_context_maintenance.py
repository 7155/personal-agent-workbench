from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from rag_ime.activity_timeline import DailyActivityTimelineStore
from rag_ime.activity_timeline_curation import (
    ACTIVITY_ORGANIZATION_OUTPUT_VERSION,
)
from rag_ime.agent_role_book import AgentRoleBookStore
from rag_ime.deepseek_memory_organizer import ActivitySemanticVerificationError
from rag_ime.local_sqlite_core import LocalSqliteCoreClient
from rag_ime.models import InputEvent
from rag_ime.personal_context import (
    AgentMemoryEvidenceStore,
    PersonalContextConsolidator,
)
from rag_ime.personal_context_maintenance import (
    PersonalContextMaintenanceConfig,
    PersonalContextMaintenanceRunner,
    write_personal_context_maintenance_report,
)


class PersonalContextMaintenanceRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="rag-ime-personal-context-maintenance-"
        )
        self.db_path = Path(self.temporary.name) / "rag-ime.sqlite"
        self.role_books = AgentRoleBookStore(self.db_path)
        self.role_books.initialize()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_run_is_per_project_and_role_draft_only_and_idempotent_by_default(
        self,
    ) -> None:
        self._seed_role("architect", "role-v1", created_at_ms=10)
        self._seed_role("reviewer", "role-v2", created_at_ms=20)
        self._record_work(
            "project-a",
            "architect",
            "receipt:a",
            "完成个人上下文游标",
            occurred_at_ms=100,
        )
        self._record_work(
            "project-b",
            "reviewer",
            "receipt:b",
            "完成角色书草案审阅",
            occurred_at_ms=110,
        )
        runner = PersonalContextMaintenanceRunner(
            self.db_path,
            config=PersonalContextMaintenanceConfig(min_interval_ms=86_400_000),
        )

        first = runner.run_once(now_ms=1_000)
        second = runner.run_once(now_ms=1_001)
        status = runner.status(now_ms=1_001)

        self.assertTrue(first["ok"])
        self.assertTrue(first["draftOnly"])
        self.assertFalse(first["applySafeRecentWork"])
        self.assertEqual(first["summary"]["targetCount"], 2)
        self.assertEqual(first["summary"]["succeededCount"], 2)
        self.assertEqual(
            {(item["project"], item["roleId"]) for item in first["targets"]},
            {("project-a", "architect"), ("project-b", "reviewer")},
        )
        for target in first["targets"]:
            self.assertTrue(target["artifacts"]["digestId"])
            self.assertTrue(target["artifacts"]["userMemoryDraftId"])
            self.assertTrue(target["artifacts"]["roleBookDraftId"])
            self.assertEqual(target["artifacts"]["appliedRoleBookRevisionId"], "")
        self.assertEqual(second["summary"]["succeededCount"], 0)
        self.assertEqual(second["summary"]["notDueCount"], 2)
        self.assertEqual(status["summary"]["targetCount"], 2)
        for target in status["targets"]:
            self.assertEqual(target["dueReason"], "no_evidence")
            self.assertEqual(target["lastRunStatus"], "succeeded")
            self.assertEqual(target["lastSucceededAtMs"], 1_000)
            self.assertEqual(target["lastRunError"], "")

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT project, role_id, status, output_json
                FROM personal_context_consolidation_runs
                ORDER BY project, role_id
                """
            ).fetchall()
        self.assertEqual(len(rows), 2)
        for _, _, run_status, output_json in rows:
            output = json.loads(output_json)
            self.assertEqual(run_status, "succeeded")
            self.assertTrue(output["digest"])
            self.assertTrue(output["userMemoryDraft"])
            self.assertTrue(output["roleBookDraft"])
            self.assertEqual(output["appliedRoleBookRevisionId"], "")
        self.assertEqual(
            self.role_books.active("architect", "role-v1")["revisionNumber"],
            1,
        )
        self.assertEqual(
            self.role_books.active("reviewer", "role-v2")["revisionNumber"],
            1,
        )

    def test_safe_recent_work_is_applied_only_with_explicit_switch(self) -> None:
        self._seed_role("architect", "role-v1", created_at_ms=10)
        self._record_work(
            "project-a",
            "architect",
            "receipt:apply",
            "完成可验证的每日整理任务",
            occurred_at_ms=100,
        )
        config = PersonalContextMaintenanceConfig(
            project="project-a",
            apply_safe_recent_work=True,
            min_interval_ms=86_400_000,
        )

        report = PersonalContextMaintenanceRunner(
            self.db_path,
            config=config,
        ).run_once(now_ms=1_000)

        self.assertTrue(report["ok"])
        self.assertFalse(report["draftOnly"])
        self.assertTrue(report["applySafeRecentWork"])
        target = report["targets"][0]
        self.assertEqual(target["runStatus"], "succeeded")
        self.assertTrue(target["artifacts"]["appliedRoleBookRevisionId"])
        active = self.role_books.active("architect", "role-v1")
        self.assertEqual(active["revisionNumber"], 2)
        self.assertEqual(
            [item["text"] for item in active["sections"]["recentWork"]],
            ["已验收工作项 work:receipt:apply"],
        )

    def test_periodic_runner_persists_model_role_review_without_activation(
        self,
    ) -> None:
        seed = self._seed_role("architect", "role-v1", created_at_ms=10)
        evidence = AgentMemoryEvidenceStore(self.db_path, project="project-a")
        digest = evidence.record_session_digest(
            session_id="session:periodic",
            digest_id="digest:periodic",
            role_id="architect",
            text="任务完成：修正时间线证据边界；后续继续维护角色书中的当前承诺。",
            occurred_at_ms=100,
            metadata={"trigger": "idle_batch"},
        )["evidence"]
        receipt = evidence.record_work_receipt(
            work_item_id="work:receipt:periodic",
            receipt_id="receipt:periodic",
            session_id="session:periodic",
            role_id="architect",
            text="完成周期性角色书整理",
            accepted=True,
            occurred_at_ms=110,
        )["evidence"]
        organizer = _MaintenanceRoleBookOrganizer(
            [digest["evidenceId"], receipt["evidenceId"]]
        )

        report = PersonalContextMaintenanceRunner(
            self.db_path,
            config=PersonalContextMaintenanceConfig(
                project="project-a",
                min_interval_ms=0,
                apply_safe_recent_work=True,
            ),
            role_book_organizer=organizer,
        ).run_once(now_ms=1_000, force=True)

        self.assertTrue(report["ok"])
        artifact = report["targets"][0]["artifacts"]
        self.assertTrue(artifact["proposedRoleBookRevisionId"])
        self.assertTrue(artifact["appliedRoleBookRevisionId"])
        proposed = self.role_books.get_revision(
            artifact["proposedRoleBookRevisionId"]
        )
        self.assertEqual(proposed["status"], "draft")
        self.assertEqual(
            proposed["sourceRevisionId"],
            artifact["appliedRoleBookRevisionId"],
        )
        self.assertTrue(proposed["sections"]["lessonsAndLimits"])
        self.assertTrue(proposed["sections"]["activeCommitments"])
        self.assertEqual(
            self.role_books.active("architect", "role-v1")["revisionId"],
            artifact["appliedRoleBookRevisionId"],
        )
        self.assertNotEqual(artifact["appliedRoleBookRevisionId"], seed["revisionId"])
        self.assertEqual(len(organizer.calls), 1)

    def test_one_target_failure_is_reported_without_stopping_other_targets(
        self,
    ) -> None:
        self._seed_role("architect", "role-v1", created_at_ms=10)
        self._seed_role("reviewer", "role-v2", created_at_ms=20)
        self._record_work(
            "broken-project",
            "architect",
            "receipt:broken",
            "这一条会触发测试故障",
            occurred_at_ms=100,
        )
        self._record_work(
            "healthy-project",
            "reviewer",
            "receipt:healthy",
            "这一条仍应成功整理",
            occurred_at_ms=110,
        )

        def factory(
            db_path: str | Path,
            *,
            project: str,
            role_book_applier: object | None,
        ) -> PersonalContextConsolidator:
            if project == "broken-project":
                return _ExplodingConsolidator()  # type: ignore[return-value]
            return PersonalContextConsolidator(
                db_path,
                project=project,
                role_book_applier=role_book_applier,
            )

        report = PersonalContextMaintenanceRunner(
            self.db_path,
            config=PersonalContextMaintenanceConfig(min_interval_ms=0),
            consolidator_factory=factory,
        ).run_once(now_ms=1_000)

        by_project = {item["project"]: item for item in report["targets"]}
        self.assertFalse(report["ok"])
        self.assertEqual(report["summary"]["failedCount"], 1)
        self.assertEqual(by_project["broken-project"]["runStatus"], "failed")
        self.assertIn(
            "synthetic background failure",
            by_project["broken-project"]["error"],
        )
        self.assertEqual(by_project["healthy-project"]["runStatus"], "succeeded")
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT project, status
                FROM personal_context_consolidation_runs
                ORDER BY project
                """
            ).fetchall()
        self.assertEqual(rows, [("healthy-project", "succeeded")])

    def test_failed_persisted_run_exposes_retry_due_error_and_last_run(self) -> None:
        self._seed_role("architect", "role-v1", created_at_ms=10)
        self._record_work(
            "project-a",
            "architect",
            "receipt:failed-apply",
            "这一条会进入 recentWork 的安全应用边界",
            occurred_at_ms=100,
        )

        def failing_applier(request: object) -> object:
            del request
            raise RuntimeError("role book apply unavailable")

        runner = PersonalContextMaintenanceRunner(
            self.db_path,
            config=PersonalContextMaintenanceConfig(
                project="project-a",
                apply_safe_recent_work=True,
                min_interval_ms=86_400_000,
            ),
            role_book_applier=failing_applier,
        )
        run = runner.run_once(now_ms=1_000)
        status = runner.status(now_ms=1_001)

        self.assertFalse(run["ok"])
        self.assertEqual(run["targets"][0]["runStatus"], "failed")
        target = status["targets"][0]
        self.assertFalse(status["ok"])
        self.assertTrue(target["due"])
        self.assertEqual(target["dueReason"], "retry_failed")
        self.assertEqual(target["status"], "failed")
        self.assertEqual(target["lastRunStatus"], "failed")
        self.assertEqual(target["lastRunAtMs"], 1_000)
        self.assertEqual(target["lastSucceededAtMs"], 0)
        self.assertIn("role book apply unavailable", target["error"])

    def test_successful_retry_clears_the_previous_run_error_from_the_report(self) -> None:
        self._seed_role("architect", "role-v1", created_at_ms=10)
        self._record_work(
            "project-a",
            "architect",
            "receipt:retry-success",
            "这一条先失败，再由后续维护重试成功",
            occurred_at_ms=100,
        )

        def failing_applier(request: object) -> object:
            del request
            raise RuntimeError("temporary role book apply failure")

        config = PersonalContextMaintenanceConfig(
            project="project-a",
            apply_safe_recent_work=True,
            min_interval_ms=86_400_000,
        )
        failed = PersonalContextMaintenanceRunner(
            self.db_path,
            config=config,
            role_book_applier=failing_applier,
        ).run_once(now_ms=1_000)
        recovered = PersonalContextMaintenanceRunner(
            self.db_path,
            config=config,
        ).run_once(now_ms=1_001)

        self.assertEqual(failed["targets"][0]["runStatus"], "failed")
        self.assertTrue(recovered["ok"])
        target = recovered["targets"][0]
        self.assertEqual(target["runStatus"], "succeeded")
        self.assertEqual(target["lastRunStatus"], "succeeded")
        self.assertEqual(target["status"], "idle")
        self.assertEqual(target["error"], "")
        self.assertTrue(target["artifacts"]["userMemoryDraftId"])
        self.assertTrue(target["artifacts"]["roleBookDraftId"])

    def test_configuration_defaults_to_enabled_draft_only_and_report_is_atomic(
        self,
    ) -> None:
        default = PersonalContextMaintenanceConfig.from_environ({})
        explicit = PersonalContextMaintenanceConfig.from_environ(
            {
                "RAG_IME_PERSONAL_CONTEXT_MAINTENANCE_ENABLED": "true",
                "RAG_IME_PERSONAL_CONTEXT_APPLY_SAFE_RECENT_WORK": "1",
                "RAG_IME_PERSONAL_CONTEXT_INTERVAL_SECONDS": "900",
                "RAG_IME_PERSONAL_CONTEXT_BATCH_LIMIT": "25",
            },
            project="project-a",
            role_id="architect",
            role_version="role-v1",
        )
        report_path = Path(self.temporary.name) / "runs" / "latest.json"
        write_personal_context_maintenance_report(
            report_path,
            {"schemaVersion": "test.v1", "ok": True},
        )

        self.assertTrue(default.enabled)
        self.assertFalse(default.apply_safe_recent_work)
        self.assertEqual(default.min_interval_ms, 86_400_000)
        self.assertTrue(explicit.apply_safe_recent_work)
        self.assertEqual(explicit.min_interval_ms, 900_000)
        self.assertEqual(explicit.batch_limit, 25)
        self.assertEqual(
            json.loads(report_path.read_text(encoding="utf-8")),
            {"schemaVersion": "test.v1", "ok": True},
        )
        self.assertEqual(
            list(report_path.parent.glob(f".{report_path.name}.*.tmp")),
            [],
        )

    def test_activity_maintenance_uses_verified_semantics_before_publish(self) -> None:
        timestamp = int(
            datetime(
                2026,
                8,
                12,
                10,
                0,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ).timestamp()
            * 1_000
        )
        LocalSqliteCoreClient(self.db_path).record_event(
            InputEvent(
                event_id=None,
                created_at_ms=timestamp,
                source="voice_final",
                committed_text="这个日记不要再直接显示原始句子",
                recent_context="正在检查每日时间线的语义整理结果",
                privacy_disposition="allowed",
                app="RagImeControl",
                project="project-a",
            )
        )
        organizer = _MaintenanceActivityOrganizer()
        runner = PersonalContextMaintenanceRunner(
            self.db_path,
            config=PersonalContextMaintenanceConfig(
                project="project-a",
                consolidate_roles=False,
                build_timelines=True,
                auto_publish_timelines=True,
            ),
            activity_organizer=organizer,
        )

        result = runner.build_activity_timeline(
            "2026-08-12",
            now_ms=timestamp + 60_000,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["semanticOrganization"]["status"], "completed")
        self.assertEqual(
            result["timeline"]["segments"][0]["title"],
            "修复每日日记语义展示",
        )
        self.assertNotIn("不要再直接显示", result["timeline"]["summary"])
        self.assertEqual(organizer.lifecycle, ["begin", "organize", "finish"])
        calendar = DailyActivityTimelineStore(
            self.db_path,
            project="project-a",
        ).calendar("2026-08")
        self.assertTrue(calendar["days"][0]["organized"])

    def test_activity_maintenance_fails_closed_when_semantics_fail(self) -> None:
        timestamp = int(
            datetime(
                2026,
                8,
                12,
                11,
                0,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ).timestamp()
            * 1_000
        )
        LocalSqliteCoreClient(self.db_path).record_event(
            InputEvent(
                event_id=None,
                created_at_ms=timestamp,
                source="voice_final",
                committed_text="需要整理这一条活动",
                privacy_disposition="allowed",
                app="RagImeControl",
                project="project-a",
            )
        )
        organizer = _MaintenanceActivityOrganizer(fail=True)
        result = PersonalContextMaintenanceRunner(
            self.db_path,
            config=PersonalContextMaintenanceConfig(
                project="project-a",
                consolidate_roles=False,
                build_timelines=True,
                auto_publish_timelines=True,
            ),
            activity_organizer=organizer,
        ).build_activity_timeline("2026-08-12", now_ms=timestamp + 60_000)

        self.assertFalse(result["ok"])
        self.assertEqual(result["semanticOrganization"]["status"], "failed")
        self.assertEqual(organizer.lifecycle, ["begin", "organize", "fail"])
        latest = DailyActivityTimelineStore(
            self.db_path,
            project="project-a",
        ).latest("2026-08-12")
        self.assertEqual(latest["status"], "draft")

    def test_activity_semantic_verification_failure_is_a_review_warning(self) -> None:
        timestamp = int(
            datetime(
                2026,
                8,
                12,
                11,
                30,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ).timestamp()
            * 1_000
        )
        LocalSqliteCoreClient(self.db_path).record_event(
            InputEvent(
                event_id=None,
                created_at_ms=timestamp,
                source="voice_final",
                committed_text="需要保留这份待检查的活动草稿",
                privacy_disposition="allowed",
                app="RagImeControl",
                project="project-a",
            )
        )
        organizer = _MaintenanceActivityOrganizer(semantic_fail=True)
        result = PersonalContextMaintenanceRunner(
            self.db_path,
            config=PersonalContextMaintenanceConfig(
                project="project-a",
                consolidate_roles=False,
                build_timelines=True,
                auto_publish_timelines=True,
            ),
            activity_organizer=organizer,
        ).build_activity_timeline("2026-08-12", now_ms=timestamp + 60_000)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "draft")
        self.assertFalse(result["autoPublished"])
        semantic = result["semanticOrganization"]
        self.assertEqual(semantic["status"], "warning")
        self.assertTrue(semantic["reviewRequired"])
        self.assertEqual(
            semantic["error"],
            "Activity organization did not pass independent semantic verification",
        )
        self.assertEqual(semantic["verification"]["verdict"], "reject")
        self.assertEqual(
            semantic["receipt"]["verifierRequest"]["traceId"],
            "trace:activity-repair-verifier",
        )
        self.assertEqual(organizer.lifecycle, ["begin", "organize", "finish"])
        latest = DailyActivityTimelineStore(
            self.db_path,
            project="project-a",
        ).latest("2026-08-12")
        self.assertEqual(latest["status"], "draft")

    def test_activity_catch_up_serially_organizes_every_pending_day(self) -> None:
        for day, hour in ((10, 9), (11, 14)):
            timestamp = int(
                datetime(
                    2026,
                    8,
                    day,
                    hour,
                    0,
                    tzinfo=ZoneInfo("Asia/Shanghai"),
                ).timestamp()
                * 1_000
            )
            LocalSqliteCoreClient(self.db_path).record_event(
                InputEvent(
                    event_id=None,
                    created_at_ms=timestamp,
                    source="voice_final",
                    committed_text=f"整理 8 月 {day} 日的实际工作",
                    recent_context="正在回补历史每日日记",
                    privacy_disposition="allowed",
                    app="RagImeControl",
                    project="project-a",
                )
            )
        organizer = _MaintenanceActivityOrganizer()
        progress: list[dict[str, object]] = []
        runner = PersonalContextMaintenanceRunner(
            self.db_path,
            config=PersonalContextMaintenanceConfig(
                project="project-a",
                consolidate_roles=False,
                build_timelines=True,
                auto_publish_timelines=True,
            ),
            activity_organizer=organizer,
        )

        report = runner.build_activity_timelines_through(
            "2026-08-12",
            progress=lambda value: progress.append(dict(value)),
        )

        self.assertTrue(report["ok"])
        self.assertEqual(report["pendingDayCount"], 2)
        self.assertEqual(report["completedDayCount"], 2)
        self.assertEqual(report["remainingDayCount"], 0)
        self.assertEqual(
            [item["date"] for item in report["activityTimelines"]],
            ["2026-08-10", "2026-08-11"],
        )
        self.assertEqual(
            organizer.lifecycle,
            ["begin", "organize", "finish", "begin", "organize", "finish"],
        )
        self.assertEqual(progress[-1]["completedDayCount"], 2)
        self.assertEqual(
            DailyActivityTimelineStore(
                self.db_path,
                project="project-a",
            ).dates_requiring_model_organization("2026-08-12"),
            (),
        )

    def test_activity_catch_up_does_not_count_a_review_warning_as_organized(self) -> None:
        timestamp = int(
            datetime(2026, 8, 12, 11, 30, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
            * 1_000
        )
        LocalSqliteCoreClient(self.db_path).record_event(
            InputEvent(
                event_id=None,
                created_at_ms=timestamp,
                source="voice_final",
                committed_text="这条活动仍需独立复核",
                privacy_disposition="allowed",
                app="RagImeControl",
                project="project-a",
            )
        )
        runner = PersonalContextMaintenanceRunner(
            self.db_path,
            config=PersonalContextMaintenanceConfig(
                project="project-a",
                consolidate_roles=False,
                build_timelines=True,
                auto_publish_timelines=True,
            ),
            activity_organizer=_MaintenanceActivityOrganizer(semantic_fail=True),
        )

        report = runner.build_activity_timelines_through("2026-08-12")

        self.assertFalse(report["ok"])
        self.assertEqual(report["completedDayCount"], 0)
        self.assertEqual(report["remainingDayCount"], 1)
        self.assertEqual(report["warningCount"], 1)
        self.assertEqual(report["failedDate"], "2026-08-12")
        self.assertEqual(
            DailyActivityTimelineStore(self.db_path, project="project-a")
            .dates_requiring_model_organization("2026-08-12"),
            ("2026-08-12",),
        )

    def test_activity_catch_up_continues_past_review_warning_without_claiming_success(self) -> None:
        for day in (11, 12):
            timestamp = int(
                datetime(2026, 8, day, 11, 30, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
                * 1_000
            )
            LocalSqliteCoreClient(self.db_path).record_event(
                InputEvent(
                    event_id=None,
                    created_at_ms=timestamp,
                    source="voice_final",
                    committed_text=f"第 {day} 天的活动来源",
                    privacy_disposition="allowed",
                    app="RagImeControl",
                    project="project-a",
                )
            )

        class FirstDayNeedsReview(_MaintenanceActivityOrganizer):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            def organize_activity_timeline(self, *, packet: object) -> dict[str, object]:
                self.calls += 1
                self.semantic_fail = self.calls == 1
                return super().organize_activity_timeline(packet=packet)

        runner = PersonalContextMaintenanceRunner(
            self.db_path,
            config=PersonalContextMaintenanceConfig(
                project="project-a",
                consolidate_roles=False,
                build_timelines=True,
                auto_publish_timelines=True,
            ),
            activity_organizer=FirstDayNeedsReview(),
        )
        progress: list[dict[str, object]] = []

        report = runner.build_activity_timelines_through(
            "2026-08-12",
            progress=lambda value: progress.append(dict(value)),
        )

        self.assertFalse(report["ok"])
        self.assertEqual(report["completedDayCount"], 1)
        self.assertEqual(report["remainingDayCount"], 1)
        self.assertEqual(report["failedDate"], "2026-08-11")
        self.assertEqual(report["warningCount"], 1)
        self.assertEqual([item["ok"] for item in report["activityTimelines"]], [False, True])
        self.assertEqual(progress[-1]["completedDayCount"], 1)
        self.assertEqual(
            DailyActivityTimelineStore(self.db_path, project="project-a")
            .dates_requiring_model_organization("2026-08-12"),
            ("2026-08-11",),
        )

    def test_activity_catch_up_excludes_earlier_month_and_preserves_its_backlog(self) -> None:
        for value in ("2026-07-31T09:00:00", "2026-08-01T09:00:00", "2026-08-12T09:00:00"):
            timestamp = int(datetime.fromisoformat(value).replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1_000)
            LocalSqliteCoreClient(self.db_path).record_event(InputEvent(
                event_id=None, created_at_ms=timestamp, source="voice_final",
                committed_text="整理当前日期实际工作", privacy_disposition="allowed", project="project-a",
            ))
        organizer = _MaintenanceActivityOrganizer()
        runner = PersonalContextMaintenanceRunner(
            self.db_path,
            config=PersonalContextMaintenanceConfig(
                project="project-a", consolidate_roles=False,
                build_timelines=True, auto_publish_timelines=True,
            ),
            activity_organizer=organizer,
        )

        report = runner.build_activity_timelines_through("2026-08-12", start_date="2026-08-01")

        self.assertTrue(report["ok"])
        self.assertEqual(report["pendingDayCount"], 2)
        self.assertEqual(report["completedDayCount"], 2)
        self.assertEqual([item["date"] for item in report["activityTimelines"]], ["2026-08-01", "2026-08-12"])
        self.assertEqual(organizer.lifecycle, ["begin", "organize", "finish"] * 2)
        store = DailyActivityTimelineStore(self.db_path, project="project-a")
        self.assertIsNone(store.latest("2026-07-31"))
        self.assertEqual(store.dates_requiring_model_organization("2026-08-12"), ("2026-07-31",))

    def test_periodic_maintenance_backfills_one_historical_day_per_run(self) -> None:
        for day in (10, 11):
            timestamp = int(
                datetime(
                    2026,
                    8,
                    day,
                    9,
                    0,
                    tzinfo=ZoneInfo("Asia/Shanghai"),
                ).timestamp()
                * 1_000
            )
            LocalSqliteCoreClient(self.db_path).record_event(
                InputEvent(
                    event_id=None,
                    created_at_ms=timestamp,
                    source="voice_final",
                    committed_text=f"自动补齐 8 月 {day} 日的活动",
                    privacy_disposition="allowed",
                    app="RagImeControl",
                    project="project-a",
                )
            )
        organizer = _MaintenanceActivityOrganizer()
        runner = PersonalContextMaintenanceRunner(
            self.db_path,
            config=PersonalContextMaintenanceConfig(
                project="project-a",
                consolidate_roles=False,
                build_timelines=True,
                auto_publish_timelines=True,
                timeline_catch_up_limit=1,
            ),
            activity_organizer=organizer,
        )
        now_ms = int(
            datetime(
                2026,
                8,
                12,
                10,
                0,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ).timestamp()
            * 1_000
        )

        report = runner.run_once(now_ms=now_ms)

        catch_up = report["activityTimelineCatchUp"]
        self.assertTrue(catch_up["ok"])
        self.assertEqual(catch_up["pendingDayCount"], 2)
        self.assertEqual(catch_up["completedDayCount"], 1)
        self.assertEqual(catch_up["remainingDayCount"], 1)
        self.assertEqual(
            [item["date"] for item in catch_up["activityTimelines"]],
            ["2026-08-10"],
        )
        self.assertEqual(
            DailyActivityTimelineStore(
                self.db_path,
                project="project-a",
            ).dates_requiring_model_organization("2026-08-11"),
            ("2026-08-11",),
        )

    def test_cli_run_and_status_expose_the_background_maintenance_state(
        self,
    ) -> None:
        self._seed_role("architect", "role-v1", created_at_ms=10)
        self._record_work(
            "project-a",
            "architect",
            "receipt:cli",
            "通过真实 CLI 入口整理个人上下文",
            occurred_at_ms=100,
        )
        root = Path(__file__).resolve().parents[1]
        report_path = Path(self.temporary.name) / "runs" / "cli-run.json"
        common = [
            sys.executable,
            "-m",
            "rag_ime.cli",
            "--core-mode",
            "local",
            "--db-path",
            str(self.db_path),
        ]

        run = subprocess.run(
            [
                *common,
                "personal-context-maintenance-run",
                "--project",
                "project-a",
                "--interval-seconds",
                "86400",
                "--report-path",
                str(report_path),
            ],
            cwd=root,
            env={
                **os.environ,
                "RAG_IME_PERSONAL_CONTEXT_APPLY_SAFE_RECENT_WORK": "0",
            },
            text=True,
            capture_output=True,
            check=True,
        )
        status = subprocess.run(
            [
                *common,
                "personal-context-maintenance-status",
                "--project",
                "project-a",
            ],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        )

        run_payload = json.loads(run.stdout)
        status_payload = json.loads(status.stdout)
        self.assertTrue(run_payload["ok"])
        self.assertTrue(run_payload["draftOnly"])
        self.assertEqual(run_payload["targets"][0]["runStatus"], "succeeded")
        self.assertEqual(
            json.loads(report_path.read_text(encoding="utf-8")),
            run_payload,
        )
        self.assertEqual(
            status_payload["targets"][0]["lastRunStatus"],
            "succeeded",
        )
        self.assertEqual(status_payload["targets"][0]["dueReason"], "no_evidence")

    def _seed_role(
        self,
        role_id: str,
        role_version: str,
        *,
        created_at_ms: int,
    ) -> dict[str, object]:
        return self.role_books.ensure_seeded(
            role_id,
            role_version,
            display_name=role_id,
            mission="维护个人上下文",
            created_at_ms=created_at_ms,
        )

    def _record_work(
        self,
        project: str,
        role_id: str,
        receipt_id: str,
        text: str,
        *,
        occurred_at_ms: int,
    ) -> None:
        store = AgentMemoryEvidenceStore(self.db_path, project=project)
        store.initialize()
        store.record_work_receipt(
            work_item_id=f"work:{receipt_id}",
            receipt_id=receipt_id,
            role_id=role_id,
            text=text,
            occurred_at_ms=occurred_at_ms,
        )


class _ExplodingConsolidator:
    def due(
        self,
        role_id: str,
        *,
        now_ms: int,
        min_interval_ms: int,
    ) -> dict[str, object]:
        del role_id, now_ms, min_interval_ms
        return {
            "due": True,
            "reason": "new_evidence",
            "cursor": {},
            "runId": "",
        }

    def run(self, *args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        raise RuntimeError("synthetic background failure")


class _MaintenanceRoleBookOrganizer:
    provider_name = "fake-maintenance-role-organizer"

    def __init__(self, evidence_ids: list[str]) -> None:
        self.evidence_ids = evidence_ids
        self.calls: list[dict[str, object]] = []

    def curate_role_book(
        self,
        *,
        bundle: dict[str, object],
        project: str,
        role_id: str,
        role_version: str,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "bundle": bundle,
                "project": project,
                "roleId": role_id,
                "roleVersion": role_version,
            }
        )
        digest_id, receipt_id = self.evidence_ids
        return {
            "traitProposals": [],
            "capabilityProposals": [],
            "lessonProposals": [
                {
                    "text": "时间线只能辅助判断，角色经验必须引用对话证据",
                    "confidence": 0.95,
                    "sourceEvidenceIds": [receipt_id],
                }
            ],
            "commitmentProposals": [
                {
                    "text": "继续维护角色书中的当前承诺",
                    "confidence": 0.85,
                    "sourceEvidenceIds": [digest_id],
                }
            ],
        }


class _MaintenanceActivityOrganizer:
    provider_name = "fake-maintenance-activity-organizer"

    def __init__(self, *, fail: bool = False, semantic_fail: bool = False) -> None:
        self.fail = fail
        self.semantic_fail = semantic_fail
        self.lifecycle: list[str] = []

    def begin_run(self, run_id: str, *, frozen_input_sha256: str = "") -> dict[str, object]:
        del run_id, frozen_input_sha256
        self.lifecycle.append("begin")
        return {}

    def organize_activity_timeline(self, *, packet: object) -> dict[str, object]:
        self.lifecycle.append("organize")
        if self.fail:
            raise RuntimeError("synthetic Activity verifier failure")
        if self.semantic_fail:
            raise ActivitySemanticVerificationError(
                "Activity organization did not pass independent semantic verification",
                verification={
                    "schemaVersion": "rag-ime.activity-organization-verdict.v1",
                    "verdict": "reject",
                    "scores": {},
                    "issues": [],
                    "strengths": [],
                },
                receipt={
                    "verifierRequest": {"traceId": "trace:activity-repair-verifier"},
                },
            )
        refs = list(getattr(packet, "event_refs"))
        return {
            "organization": {
                "schemaVersion": ACTIVITY_ORGANIZATION_OUTPUT_VERSION,
                "activities": [
                    {
                        "title": "修复每日日记语义展示",
                        "summary": "检查时间线的原句展示问题，并改为活动级语义概括。",
                        "eventRefs": refs,
                        "confidence": 0.96,
                        "boundaryBasis": "所有来源都在讨论同一项日记语义修复。",
                    }
                ],
                "unclassified": [],
            },
            "receipt": {
                "membershipSha256": getattr(packet, "membership_sha256"),
                "verdict": "pass",
            },
        }

    def finish_run(self) -> dict[str, object]:
        self.lifecycle.append("finish")
        return {}

    def fail_run(self, error: BaseException) -> dict[str, object]:
        del error
        self.lifecycle.append("fail")
        return {}


if __name__ == "__main__":
    unittest.main()
