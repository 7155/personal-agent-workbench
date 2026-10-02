from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from rag_ime.activity_timeline import (
    DailyActivityTimelineStore,
    StaleActivityTimelineError,
)
from rag_ime.activity_timeline_curation import (
    ACTIVITY_ORGANIZATION_OUTPUT_VERSION,
)
from rag_ime.agent_role_book import AgentRoleBookStore
from rag_ime.local_sqlite_core import LocalSqliteCoreClient
from rag_ime.models import InputEvent
from rag_ime.personal_context import (
    AgentMemoryEvidenceStore,
    MemoryBootstrapBuilder,
    load_activity_timeline_context,
)
from rag_ime.personal_context_maintenance import (
    PersonalContextMaintenanceConfig,
    PersonalContextMaintenanceRunner,
)
from rag_ime.personal_context_observability import PersonalContextObservability
from rag_ime.retrieval_docs import rebuild_retrieval_docs
from tests.sqlite_fixtures import copy_current_database


class DailyActivityTimelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="rag-ime-activity-timeline-"
        )
        self.db_path = Path(self.temporary.name) / "rag-ime.sqlite"
        self.addCleanup(self.temporary.cleanup)
        copy_current_database(self.db_path)
        self.project = "wisdom-weasel-rag-ime"
        self.zone = ZoneInfo("Asia/Shanghai")
        self.core = LocalSqliteCoreClient(self.db_path)
        self.core.initialize()
        self.role_books = AgentRoleBookStore(self.db_path)
        self.role_books.initialize()
        self.role_books.ensure_seeded(
            "architect",
            "role-v1",
            display_name="架构师",
            mission="维护个人上下文",
            created_at_ms=self._ms(8, 0),
        )

    def test_preverified_store_does_not_reapply_migrations(self) -> None:
        store = DailyActivityTimelineStore(
            self.db_path,
            project=self.project,
            timezone_name="Asia/Shanghai",
            preverified_schema=True,
        )
        with patch(
            "rag_ime.activity_timeline.apply_database_migrations",
            side_effect=AssertionError("migration replayed"),
        ):
            store.initialize()

    def test_pending_dates_respect_inclusive_selected_range_and_legacy_history(self) -> None:
        store = DailyActivityTimelineStore(self.db_path, project=self.project, timezone_name="Asia/Shanghai")
        for value in ("2026-07-31T23:59:00", "2026-08-01T00:00:00", "2026-08-12T23:59:00", "2026-08-13T00:00:00"):
            timestamp = int(datetime.fromisoformat(value).replace(tzinfo=self.zone).timestamp() * 1_000)
            self.core.record_event(InputEvent(
                event_id=None, created_at_ms=timestamp, source="voice_final",
                committed_text="验证日记范围", privacy_disposition="allowed", project=self.project,
            ))

        self.assertEqual(
            store.dates_requiring_model_organization("2026-08-12", start_date="2026-08-01"),
            ("2026-08-01", "2026-08-12"),
        )
        self.assertEqual(
            store.dates_requiring_model_organization("2026-08-12"),
            ("2026-07-31", "2026-08-01", "2026-08-12"),
        )

    def test_pending_dates_reject_reversed_range_before_reading_sources(self) -> None:
        store = DailyActivityTimelineStore(self.db_path, project=self.project)
        with patch.object(store, "initialize") as initialize:
            with self.assertRaisesRegex(ValueError, "start.*through"):
                store.dates_requiring_model_organization("2026-08-12", start_date="2026-08-13")
        initialize.assert_not_called()

    def test_calendar_reports_current_and_outdated_daily_coverage(self) -> None:
        store = DailyActivityTimelineStore(
            self.db_path,
            project=self.project,
            timezone_name="Asia/Shanghai",
        )
        self._record(
            9,
            0,
            app="com.openai.codex",
            source="squirrel_input_segment",
            text="实现每日整理日历",
        )
        draft = store.build_draft(
            "2026-07-17",
            generated_at_ms=self._ms(9, 5),
        )["timeline"]
        packet = store.organization_packet(draft["timelineId"])
        store.apply_model_organization(
            draft["timelineId"],
            organization={
                "schemaVersion": ACTIVITY_ORGANIZATION_OUTPUT_VERSION,
                "activities": [
                    {
                        "title": "完善每日整理日历",
                        "summary": "实现月度整理状态与当天活动入口。",
                        "eventRefs": ["e1"],
                        "confidence": 0.96,
                        "boundaryBasis": "当前输入明确描述同一项日历实现工作。",
                    }
                ],
                "unclassified": [],
            },
            receipt={
                "membershipSha256": packet.membership_sha256,
                "verdict": "pass",
            },
            organized_at_ms=self._ms(9, 6),
        )
        store.approve(
            draft["timelineId"],
            expected_source_event_hash=draft["sourceEventHash"],
            approved_by="user:test",
            confirm_text="approve",
            approved_at_ms=self._ms(9, 7),
        )

        current = store.calendar("2026-07")
        current_day = next(
            item for item in current["days"] if item["date"] == "2026-07-17"
        )
        self.assertEqual(current_day["status"], "approved")
        self.assertTrue(current_day["organized"])
        self.assertFalse(current_day["needsRefresh"])
        self.assertEqual(current["summary"]["organizedDayCount"], 1)
        self.assertEqual(current["summary"]["waitingDayCount"], 0)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE daily_activity_timelines SET metadata_json = ? WHERE timeline_id = ?",
                ('{"segmentationMode":"semantic_task_v5"}', draft["timelineId"]),
            )
        heuristic = store.calendar("2026-07")
        heuristic_day = next(
            item for item in heuristic["days"] if item["date"] == "2026-07-17"
        )
        self.assertFalse(heuristic_day["organized"])
        self.assertEqual(heuristic["summary"]["organizedDayCount"], 0)
        self.assertEqual(heuristic["summary"]["waitingDayCount"], 1)

        store.apply_model_organization(
            draft["timelineId"],
            organization={
                "schemaVersion": ACTIVITY_ORGANIZATION_OUTPUT_VERSION,
                "activities": [
                    {
                        "title": "完善每日整理日历",
                        "summary": "实现月度整理状态与当天活动入口。",
                        "eventRefs": ["e1"],
                        "confidence": 0.96,
                        "boundaryBasis": "当前输入明确描述同一项日历实现工作。",
                    }
                ],
                "unclassified": [],
            },
            receipt={
                "membershipSha256": packet.membership_sha256,
                "verdict": "pass",
            },
            organized_at_ms=self._ms(9, 8),
        )

        self._record(
            9,
            10,
            app="com.openai.codex",
            source="squirrel_input_segment",
            text="当天新增一条尚未纳入时间线的输入",
        )
        outdated = store.calendar("2026-07")
        outdated_day = next(
            item for item in outdated["days"] if item["date"] == "2026-07-17"
        )
        self.assertFalse(outdated_day["organized"])
        self.assertTrue(outdated_day["needsRefresh"])
        self.assertEqual(outdated_day["sourceEventCount"], 2)
        self.assertEqual(outdated["summary"]["outdatedDayCount"], 1)
        self.assertEqual(outdated["summary"]["waitingDayCount"], 1)

        with self.assertRaisesRegex(ValueError, "YYYY-MM"):
            store.calendar("2026-7")

    def test_maintenance_draft_review_approve_and_bootstrap_form_closed_loop(
        self,
    ) -> None:
        own_event_ids = [
            self._record(
                9,
                0,
                app="com.openai.chat",
                source="squirrel_commit",
                text="讨论个人记忆重构",
                context_group_id="topic:memory",
            ),
            self._record(
                9,
                5,
                app="com.openai.chat",
                source="squirrel_commit",
                text="明确新 Session 只注入一次上下文",
                context_group_id="topic:memory",
            ),
            self._record(
                9,
                8,
                app="com.google.Chrome",
                source="browser_extension",
                text="查阅 LongMemEval 评测说明",
                context_group_id="tab:benchmark",
            ),
            self._record(
                9,
                14,
                app="com.apple.TextEdit",
                source="squirrel_rime_commit",
                text="实现跨应用活动时间线",
                context_group_id="document:design",
            ),
            self._record(
                9,
                18,
                app="com.apple.TextEdit",
                source="squirrel_rime_commit",
                text="token=secret-value",
                context_group_id="document:design",
            ),
        ]
        self._record(
            9,
            20,
            app="com.google.Chrome",
            source="browser_extension",
            text="另一个项目不可混入",
            project="another-project",
        )
        evidence = AgentMemoryEvidenceStore(self.db_path, project=self.project)
        evidence.record_user_message(
            session_id="session:timeline",
            pi_entry_id="entry:user:timeline",
            role_id="architect",
            text="今天继续把个人上下文闭环做完",
            occurred_at_ms=self._ms(9, 30),
        )

        before = MemoryBootstrapBuilder(
            self.db_path,
            project=self.project,
        ).build(
            "session:before-approval",
            role_id="architect",
            generated_at_ms=self._ms(10, 0),
        )
        self.assertEqual(before["payload"]["sections"]["recentTimeline"], [])

        report = PersonalContextMaintenanceRunner(
            self.db_path,
            config=PersonalContextMaintenanceConfig(
                project=self.project,
                min_interval_ms=0,
                auto_publish_timelines=True,
            ),
            activity_organizer=_TwoBlockActivityOrganizer(),
        ).run_once(
            now_ms=self._ms(10, 0),
            force=True,
        )

        self.assertTrue(report["ok"])
        self.assertEqual(report["activityTimelineSummary"]["draftCount"], 0)
        self.assertEqual(report["activityTimelineSummary"]["approvedCount"], 1)
        built = report["activityTimelines"][0]
        self.assertTrue(built["autoPublished"])
        timeline_id = built["timeline"]["timelineId"]
        timeline = DailyActivityTimelineStore(
            self.db_path,
            project=self.project,
            timezone_name="Asia/Shanghai",
        ).review(timeline_id)
        self.assertEqual(timeline["status"], "approved")
        self.assertEqual(timeline["sourceEventIds"], own_event_ids)
        self.assertEqual(timeline["eventCount"], 5)
        self.assertEqual(timeline["segmentCount"], 2)
        self.assertEqual(
            [segment["apps"] for segment in timeline["segments"]],
            [
                ["com.openai.chat", "com.google.Chrome"],
                ["com.apple.TextEdit"],
            ],
        )
        flattened_ids = [
            event_id
            for segment in timeline["segments"]
            for event_id in segment["sourceEventIds"]
        ]
        self.assertEqual(sorted(flattened_ids), sorted(own_event_ids))
        self.assertEqual(len(flattened_ids), len(set(flattened_ids)))
        self.assertEqual(timeline["segments"][-1]["redactedEventCount"], 1)
        self.assertNotIn("secret-value", json.dumps(timeline, ensure_ascii=False))
        self.assertFalse(timeline["policy"]["longTermFact"])
        self.assertEqual(timeline["ordinaryActivityCount"], 2)
        self.assertEqual(timeline["consolidatedActivityCount"], 0)
        self.assertEqual(timeline["observedStartMs"], self._ms(9, 0))
        self.assertEqual(timeline["observedEndMs"], self._ms(9, 18))
        self.assertEqual(timeline["spanSemantics"], "first_to_last_source_event")
        self.assertTrue(timeline["policy"]["automaticPromotion"])
        self.assertFalse(timeline["policy"]["explicitApprovalRequired"])

        with sqlite3.connect(self.db_path) as conn:
            output = json.loads(
                conn.execute(
                    """
                    SELECT output_json
                    FROM personal_context_consolidation_runs
                    WHERE project = ? AND role_id = 'architect'
                    """,
                    (self.project,),
                ).fetchone()[0]
            )
            self.assertEqual(
                output["digest"]["activityTimelineId"],
                timeline_id,
            )
            self.assertNotIn("segments", output["digest"])
            self.assertEqual(output["userMemoryDraft"]["candidates"], [])
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM memory_atoms").fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM memory_books").fetchone()[0],
                0,
            )

        after = MemoryBootstrapBuilder(
            self.db_path,
            project=self.project,
        ).build(
            "session:after-approval",
            role_id="architect",
            generated_at_ms=self._ms(10, 6),
        )
        recent = after["payload"]["sections"]["recentTimeline"]
        self.assertEqual(recent, [])
        self.assertEqual(timeline["approvedBookId"], "")

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rebuild_retrieval_docs(conn, project=self.project)
            timeline_doc = conn.execute(
                """
                SELECT source_id, metadata_json
                FROM memory_retrieval_docs
                WHERE doc_type = 'timeline'
                """
            ).fetchone()
            outbox = conn.execute(
                """
                SELECT projection_kind, aggregate_type, operation, state
                FROM memory_projection_outbox
                WHERE aggregate_id = ?
                """,
                (timeline_id,),
            ).fetchone()
            book_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM memory_books WHERE book_type = 'daily'"
                ).fetchone()[0]
            )
        self.assertEqual(book_count, 0)
        self.assertEqual(timeline_doc["source_id"], timeline_id)
        self.assertEqual(
            json.loads(timeline_doc["metadata_json"])["sourceEventIds"],
            own_event_ids,
        )
        self.assertEqual(
            tuple(outbox),
            (
                "retrieval_docs",
                "daily_activity_timeline",
                "approve",
                "pending",
            ),
        )

    def test_cas_terminal_and_codex_events_form_one_cross_app_task_without_loss(
        self,
    ) -> None:
        event_ids = [
            self._record(
                8,
                20,
                app="com.mitchellh.ghostty",
                source="squirrel_input_segment",
                text="在终端执行 cas codex switch，切换到工作账号",
                context_group_id="app:ghostty",
            ),
            self._record(
                8,
                31,
                app="com.openai.codex",
                source="codex_history",
                text="验证 Codex 已完成账号切换，可以继续开发",
                context_group_id="app:codex",
            ),
        ]
        store = DailyActivityTimelineStore(
            self.db_path,
            project=self.project,
            timezone_name="Asia/Shanghai",
        )

        timeline = store.build_draft(
            "2026-07-17",
            generated_at_ms=self._ms(8, 40),
        )["timeline"]

        self.assertEqual(timeline["segmentCount"], 1)
        segment = timeline["segments"][0]
        self.assertEqual(segment["title"], "CAS 切换 Codex 账号")
        self.assertEqual(
            segment["apps"],
            ["com.mitchellh.ghostty", "com.openai.codex"],
        )
        self.assertEqual(segment["sourceEventIds"], event_ids)
        self.assertEqual(
            [reference["eventId"] for reference in segment["evidenceRefs"]],
            event_ids,
        )
        self.assertEqual(segment["activityKind"], "ordinary_activity")
        self.assertEqual(segment["spanSemantics"], "first_to_last_source_event")
        self.assertTrue(all("preview" not in ref for ref in segment["evidenceRefs"]))

    def test_short_cas_burst_and_large_followup_have_distinct_task_titles(
        self,
    ) -> None:
        event_ids = [
            self._record(
                6,
                59,
                app="com.mitchellh.ghostty",
                source="squirrel_input_segment",
                text=text,
                context_group_id="app:ghostty:cas",
            )
            for text in ("ex", "cas", "o", "codex", "/", "re")
        ]
        event_ids.extend(
            (
                self._record(
                    7,
                    minute,
                    app="com.openai.codex",
                    source="codex_history",
                    text=text,
                    context_group_id="app:codex:merge",
                )
                for minute, text in (
                    (1, "合并分支"),
                    (5, "记录 git 合并"),
                    (10, "说明合并了哪些分支和功能"),
                )
            )
        )
        followup_texts = [
            "账号切换验证已结束，后续进入记忆系统工作",
            *("优化记忆系统多路召回和 BM25 检索" for _ in range(4)),
            *("优化记忆系统的输入框上下文捕获" for _ in range(4)),
            *("优化记忆系统前端界面和时间线" for _ in range(4)),
        ]
        event_ids.extend(
            self._record(
                8,
                index,
                app="com.openai.codex",
                source="codex_history",
                text=text,
                context_group_id="app:codex:memory-work",
            )
            for index, text in enumerate(followup_texts)
        )
        timeline = DailyActivityTimelineStore(
            self.db_path,
            project=self.project,
            timezone_name="Asia/Shanghai",
        ).build_draft(
            "2026-07-17",
            generated_at_ms=self._ms(9, 0),
        )["timeline"]

        self.assertEqual(timeline["segmentCount"], 3)
        self.assertEqual(
            timeline["segments"][0]["title"],
            "CAS 切换 Codex 账号",
        )
        self.assertEqual(timeline["segments"][1]["title"], "合并分支并记录改动")
        self.assertEqual(
            timeline["segments"][2]["title"],
            "记忆系统、上下文捕获与前端界面协同优化",
        )
        self.assertNotEqual(
            timeline["segments"][2]["title"],
            "CAS 切换 Codex 账号",
        )
        flattened_ids = [
            event_id
            for segment in timeline["segments"]
            for event_id in segment["sourceEventIds"]
        ]
        self.assertEqual(sorted(flattened_ids), sorted(event_ids))
        self.assertEqual(len(flattened_ids), len(set(flattened_ids)))

    def test_same_semantic_task_crossing_noon_is_classified_as_all_day(self) -> None:
        event_ids = [
            self._record(
                9,
                0,
                app="com.openai.codex",
                source="codex_history",
                text="继续修复输入法记忆召回和 BM25 混合检索",
                context_group_id="app:codex:morning",
            ),
            self._record(
                15,
                0,
                app="com.mitchellh.ghostty",
                source="squirrel_input_segment",
                text="继续修复输入法记忆召回和 BM25 混合检索",
                context_group_id="app:ghostty:afternoon",
            ),
        ]
        store = DailyActivityTimelineStore(
            self.db_path,
            project=self.project,
            timezone_name="Asia/Shanghai",
        )

        timeline = store.build_draft(
            "2026-07-17",
            generated_at_ms=self._ms(15, 5),
        )["timeline"]

        self.assertEqual(timeline["segmentCount"], 1)
        segment = timeline["segments"][0]
        self.assertEqual(segment["sourceEventIds"], event_ids)
        self.assertEqual(segment["startMs"], self._ms(9, 0))
        self.assertEqual(segment["endMs"], self._ms(15, 0))
        self.assertEqual(segment["period"], "day")
        self.assertEqual(segment["activityKind"], "consolidated_activity")
        self.assertEqual(segment["spanSemantics"], "first_to_last_source_event")
        self.assertEqual(timeline["ordinaryActivityCount"], 0)
        self.assertEqual(timeline["consolidatedActivityCount"], 1)

    def test_draft_excludes_both_forgotten_event_tombstone_shapes(self) -> None:
        source_tombstoned_id = self._record(
            10,
            0,
            app="com.openai.codex",
            source="squirrel_input_segment",
            text="不应进入时间线的 source_event_id 内容",
        )
        memory_tombstoned_id = self._record(
            10,
            5,
            app="com.openai.codex",
            source="squirrel_input_segment",
            text="不应进入时间线的 memory_id 内容",
        )
        visible_id = self._record(
            10,
            10,
            app="com.openai.codex",
            source="squirrel_input_segment",
            text="仍可进入时间线的可见内容",
        )
        with sqlite3.connect(self.db_path) as conn, conn:
            conn.executemany(
                """
                INSERT INTO memory_tombstones(
                    created_at_ms, target_type, target_value, reason,
                    active, metadata_json
                ) VALUES (?, ?, ?, 'test-forget-source', 1, '{}')
                """,
                (
                    (
                        self._ms(10, 15),
                        "source_event_id",
                        str(source_tombstoned_id),
                    ),
                    (
                        self._ms(10, 15),
                        "memory_id",
                        f"event:{memory_tombstoned_id}",
                    ),
                ),
            )

        timeline = DailyActivityTimelineStore(
            self.db_path,
            project=self.project,
            timezone_name="Asia/Shanghai",
        ).build_draft(
            "2026-07-17",
            generated_at_ms=self._ms(10, 20),
        )["timeline"]

        self.assertEqual(timeline["sourceEventIds"], [visible_id])
        serialized = json.dumps(timeline, ensure_ascii=False)
        self.assertNotIn("source_event_id 内容", serialized)
        self.assertNotIn("memory_id 内容", serialized)
        self.assertIn("仍可进入时间线的可见内容", serialized)

    def test_legacy_draft_is_rebuilt_with_current_semantic_tasks(self) -> None:
        self._record(
            8,
            20,
            app="com.mitchellh.ghostty",
            source="squirrel_input_segment",
            text="在终端执行 cas codex switch，切换到工作账号",
            context_group_id="app:ghostty",
        )
        self._record(
            8,
            31,
            app="com.openai.codex",
            source="codex_history",
            text="验证 Codex 已完成账号切换，可以继续开发",
            context_group_id="app:codex",
        )
        store = DailyActivityTimelineStore(
            self.db_path,
            project=self.project,
            timezone_name="Asia/Shanghai",
        )
        first = store.build_draft(
            "2026-07-17",
            generated_at_ms=self._ms(8, 40),
        )["timeline"]
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE daily_activity_timelines
                SET summary_text = 'legacy app intervals', segment_count = 99,
                    metadata_json = ?
                WHERE timeline_id = ?
                """,
                (
                    json.dumps({"segmentationMode": "app_interval_v1"}),
                    first["timelineId"],
                ),
            )

        legacy = store.review(first["timelineId"])
        self.assertEqual(legacy["segmentationMode"], "legacy_app_interval_v1")

        rebuilt = store.build_draft(
            "2026-07-17",
            generated_at_ms=self._ms(8, 41),
        )

        self.assertTrue(rebuilt["created"])
        self.assertEqual(rebuilt["timeline"]["timelineId"], first["timelineId"])
        self.assertEqual(rebuilt["timeline"]["segmentCount"], 1)
        self.assertNotEqual(rebuilt["timeline"]["summary"], "legacy app intervals")
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT metadata_json FROM daily_activity_timelines WHERE timeline_id = ?",
                (first["timelineId"],),
            ).fetchone()
        self.assertEqual(
            json.loads(str(row[0]))["segmentationMode"],
            "semantic_task_v5",
        )

    def test_one_off_fragment_is_evidence_not_a_standalone_task(self) -> None:
        for minute, text in (
            (0, "优化记忆检索与时间线展示"),
            (5, "修复 Tag 关系图首次打开不完整"),
            (10, "验证记忆整理证据链"),
            (15, "完成时间线前端交互"),
            (20, "检查 RAG 召回与主题书"),
            (25, "测试输入法上下文注入"),
            (30, "补齐 Agent 记忆工具"),
            (35, "整理今日记忆系统进展"),
        ):
            self._record(
                9,
                minute,
                app="com.openai.codex",
                source="squirrel_commit",
                text=text,
            )
        self._record(
            10,
            0,
            app="com.microsoft.edgemac",
            source="squirrel_commit",
            text="ku",
        )
        self._record(
            10,
            20,
            app="com.openai.codex",
            source="squirrel_commit",
            text="LongMemEval 评测与简历指标整理",
        )

        timeline = DailyActivityTimelineStore(
            self.db_path,
            project=self.project,
            timezone_name="Asia/Shanghai",
        ).build_draft(
            "2026-07-17",
            generated_at_ms=self._ms(10, 30),
        )["timeline"]

        self.assertEqual(timeline["segmentationMode"], "semantic_task_v5")
        self.assertNotIn("ku", [item["title"] for item in timeline["segments"]])
        self.assertLessEqual(timeline["segmentCount"], 3)
        self.assertEqual(timeline["eventCount"], 10)

    def test_runtime_context_group_never_forces_unrelated_tasks_together(self) -> None:
        shared_scope = "app:com.openai.codex:window:main"
        event_ids = [
            self._record(
                9,
                0,
                app="com.openai.codex",
                source="codex_history",
                text="修复输入法记忆召回和 BM25 混合检索",
                context_group_id=shared_scope,
            ),
            self._record(
                9,
                8,
                app="com.openai.codex",
                source="codex_history",
                text="整理毕业论文实验图表和参考文献格式",
                context_group_id=shared_scope,
            ),
        ]
        store = DailyActivityTimelineStore(
            self.db_path,
            project=self.project,
            timezone_name="Asia/Shanghai",
        )

        timeline = store.build_draft(
            "2026-07-17",
            generated_at_ms=self._ms(9, 15),
        )["timeline"]

        self.assertEqual(timeline["segmentCount"], 2)
        flattened_ids = [
            event_id
            for segment in timeline["segments"]
            for event_id in segment["sourceEventIds"]
        ]
        self.assertEqual(sorted(flattened_ids), sorted(event_ids))
        self.assertEqual(len(flattened_ids), len(set(flattened_ids)))

    def test_verified_model_organization_replaces_raw_sentence_titles(self) -> None:
        first_id = self._record(
            9,
            0,
            app="com.openai.codex",
            source="voice_final",
            text="这个页面为什么还是把原始句子放在这里",
        )
        second_id = self._record(
            9,
            6,
            app="com.openai.codex",
            source="voice_final",
            text="时间线应该概括我在修复每日语义整理",
        )
        store = DailyActivityTimelineStore(
            self.db_path,
            project=self.project,
            timezone_name="Asia/Shanghai",
        )
        draft = store.build_draft(
            "2026-07-17",
            generated_at_ms=self._ms(9, 10),
        )["timeline"]
        packet = store.organization_packet(draft["timelineId"])

        organized = store.apply_model_organization(
            draft["timelineId"],
            organization={
                "schemaVersion": ACTIVITY_ORGANIZATION_OUTPUT_VERSION,
                "activities": [
                    {
                        "title": "修复时间线语义整理",
                        "summary": "检查每日时间线直接展示原句的问题，并明确改为活动级概括。",
                        "eventRefs": ["e1", "e2"],
                        "confidence": 0.96,
                        "boundaryBasis": "两条输入共同指向同一个时间线语义展示问题。",
                    }
                ],
                "unclassified": [],
            },
            receipt={
                "organizerPromptVersion": "activity-organizer-luna-v3",
                "verifierPromptVersion": "activity-organizer-verifier-luna-v1",
                "membershipSha256": packet.membership_sha256,
                "organizerOutputSha256": "a" * 64,
                "verifierOutputSha256": "b" * 64,
                "verdict": "pass",
                "scores": {"titleSummaryFidelity": 5},
            },
            organized_at_ms=self._ms(9, 11),
        )

        self.assertEqual(organized["segments"][0]["title"], "修复时间线语义整理")
        self.assertEqual(
            organized["segments"][0]["summary"],
            "检查每日时间线直接展示原句的问题，并明确改为活动级概括。",
        )
        self.assertEqual(
            organized["segments"][0]["sourceEventIds"],
            [first_id, second_id],
        )
        self.assertNotIn("这个页面为什么", organized["summary"])
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT metadata_json FROM daily_activity_timelines WHERE timeline_id = ?",
                (draft["timelineId"],),
            ).fetchone()
        metadata = json.loads(str(row[0]))
        self.assertTrue(metadata["modelOrganized"])
        self.assertEqual(metadata["organizationMode"], "luna_activity_v1")
        self.assertEqual(
            metadata["modelOrganization"]["membershipSha256"],
            packet.membership_sha256,
        )

    def test_model_organization_fails_closed_on_missing_event_reference(self) -> None:
        self._record(
            10,
            0,
            app="com.openai.codex",
            source="voice_final",
            text="第一条活动输入",
        )
        self._record(
            10,
            3,
            app="com.openai.codex",
            source="voice_final",
            text="第二条活动输入",
        )
        store = DailyActivityTimelineStore(
            self.db_path,
            project=self.project,
            timezone_name="Asia/Shanghai",
        )
        draft = store.build_draft("2026-07-17")["timeline"]

        with self.assertRaisesRegex(ValueError, "coverage mismatch"):
            store.apply_model_organization(
                draft["timelineId"],
                organization={
                    "schemaVersion": ACTIVITY_ORGANIZATION_OUTPUT_VERSION,
                    "activities": [
                        {
                            "title": "不完整活动",
                            "summary": "只引用了一条来源。",
                            "eventRefs": ["e1"],
                            "confidence": 0.9,
                            "boundaryBasis": "测试缺失引用。",
                        }
                    ],
                    "unclassified": [],
                },
                receipt={},
            )

    def test_changed_input_events_make_reviewed_draft_stale(self) -> None:
        self._record(
            13,
            0,
            app="com.openai.chat",
            source="squirrel_commit",
            text="先生成时间线草案",
        )
        store = DailyActivityTimelineStore(
            self.db_path,
            project=self.project,
            timezone_name="Asia/Shanghai",
        )
        first = store.build_draft(
            "2026-07-17",
            generated_at_ms=self._ms(13, 5),
        )["timeline"]
        self._record(
            13,
            10,
            app="com.google.Chrome",
            source="browser_extension",
            text="草案审核后又新增了活动",
        )

        with self.assertRaisesRegex(
            StaleActivityTimelineError,
            "input events changed",
        ):
            store.approve(
                first["timelineId"],
                expected_source_event_hash=first["sourceEventHash"],
                approved_by="user:test",
                confirm_text="approve",
                approved_at_ms=self._ms(13, 15),
            )

        second = store.build_draft(
            "2026-07-17",
            generated_at_ms=self._ms(13, 16),
        )["timeline"]
        self.assertNotEqual(second["timelineId"], first["timelineId"])
        self.assertEqual(store.review(first["timelineId"])["status"], "superseded")
        self.assertEqual(second["status"], "draft")
        bootstrap = MemoryBootstrapBuilder(
            self.db_path,
            project=self.project,
        ).build(
            "session:stale-draft",
            role_id="architect",
            generated_at_ms=self._ms(13, 17),
        )
        self.assertEqual(bootstrap["payload"]["sections"]["recentTimeline"], [])

    def test_reviewed_or_superseded_exact_hash_is_never_reopened_as_draft(self) -> None:
        first_event_id = self._record(
            13,
            30,
            app="com.apple.TextEdit",
            source="squirrel_commit",
            text="第一版活动证据",
        )
        store = DailyActivityTimelineStore(
            self.db_path,
            project=self.project,
            timezone_name="Asia/Shanghai",
        )
        first = store.build_draft(
            "2026-07-17",
            generated_at_ms=self._ms(13, 31),
        )["timeline"]
        store.reject(
            first["timelineId"],
            reason="用户明确拒绝该证据状态",
            rejected_by="user:test",
            rejected_at_ms=self._ms(13, 32),
        )

        rejected = store.build_draft(
            "2026-07-17",
            generated_at_ms=self._ms(13, 33),
        )

        self.assertFalse(rejected["created"])
        self.assertEqual(rejected["status"], "rejected")
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT status, rejection_reason, approved_by, approved_at_ms
                FROM daily_activity_timelines WHERE timeline_id = ?
                """,
                (first["timelineId"],),
            ).fetchone()
        self.assertEqual(row, ("rejected", "用户明确拒绝该证据状态", "", None))

        # Build a realistic superseded hash: add an event so the first row is
        # superseded, then forget that extra event so the visible set reverts.
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE daily_activity_timelines
                SET status = 'draft', rejection_reason = '', approved_by = '',
                    approved_at_ms = NULL
                WHERE timeline_id = ?
                """,
                (first["timelineId"],),
            )
        second_event_id = self._record(
            13,
            34,
            app="com.google.Chrome",
            source="browser_extension",
            text="第二版新增活动证据",
        )
        store.build_draft(
            "2026-07-17",
            generated_at_ms=self._ms(13, 35),
        )
        self.assertEqual(store.review(first["timelineId"])["status"], "superseded")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE memory_state SET deleted = 1 WHERE event_id = ?",
                (second_event_id,),
            )

        superseded = store.build_draft(
            "2026-07-17",
            generated_at_ms=self._ms(13, 36),
        )

        self.assertFalse(superseded["created"])
        self.assertEqual(superseded["status"], "superseded")
        self.assertEqual(superseded["timeline"]["sourceEventIds"], [first_event_id])
        self.assertEqual(store.review(first["timelineId"])["status"], "superseded")

    def test_daily_digest_uses_evidence_day_timeline_after_midnight(self) -> None:
        self._record(
            22,
            0,
            app="com.apple.TextEdit",
            source="squirrel_commit",
            text="实现输入法每日联合上下文",
        )
        AgentMemoryEvidenceStore(self.db_path, project=self.project).record_user_message(
            session_id="session:late",
            pi_entry_id="entry:late",
            role_id="architect",
            text="今晚继续验证记忆整理",
            occurred_at_ms=self._ms(22, 5),
        )

        next_day = int(
            datetime(2026, 7, 18, 0, 10, tzinfo=self.zone).timestamp() * 1_000
        )
        report = PersonalContextMaintenanceRunner(
            self.db_path,
            config=PersonalContextMaintenanceConfig(
                project=self.project,
                min_interval_ms=0,
            ),
        ).run_once(now_ms=next_day, force=True)

        self.assertTrue(report["ok"])
        with sqlite3.connect(self.db_path) as conn:
            output = json.loads(
                conn.execute(
                    """
                    SELECT output_json FROM personal_context_consolidation_runs
                    WHERE project = ? AND role_id = 'architect'
                    ORDER BY completed_at_ms DESC LIMIT 1
                    """,
                    (self.project,),
                ).fetchone()[0]
            )
        digest = output["digest"]
        self.assertEqual(digest["activityContext"]["date"], "2026-07-17")
        self.assertIn("实现输入法每日联合上下文", digest["activityContext"]["summary"])
        self.assertIn(
            "今晚继续验证记忆整理",
            [item["text"] for item in digest["highlights"]],
        )
        self.assertTrue(digest["activityContext"]["corroborationOnly"])
        self.assertFalse(digest["activityContext"]["maySupportFacts"])

    def test_model_timeline_context_filters_internal_duplicates_and_sensitive_text(
        self,
    ) -> None:
        self._record(
            18,
            0,
            app="RagImeControl",
            source="squirrel_commit",
            text="完成联合上下文测试",
        )
        self._record(
            18,
            1,
            app="RagImeControl",
            source="pi_agent_user",
            text="完成联合上下文测试",
        )
        self._record(
            18,
            2,
            app="RagImeControl",
            source="pi_agent_compaction",
            text="内部压缩摘要不应进入模型",
        )
        self._record(
            18,
            3,
            app="RagImeControl",
            source="pi_agent_tool_receipt",
            text="内部工具回执不应进入模型",
        )
        self._record(
            18,
            4,
            app="RagImeControl",
            source="squirrel_commit",
            text="token=secret-value",
        )
        store = DailyActivityTimelineStore(
            self.db_path,
            project=self.project,
            timezone_name="Asia/Shanghai",
        )
        timeline = store.build_draft(
            "2026-07-17",
            generated_at_ms=self._ms(18, 5),
        )["timeline"]
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            context = load_activity_timeline_context(
                conn,
                project=self.project,
                timeline_date="2026-07-17",
                timeline_id=timeline["timelineId"],
            )

        serialized = json.dumps(context, ensure_ascii=False)
        self.assertIn("完成联合上下文测试", serialized)
        self.assertNotIn("内部压缩摘要", serialized)
        self.assertNotIn("内部工具回执", serialized)
        self.assertNotIn("secret-value", serialized)
        self.assertNotIn("sourceEventIds", serialized)
        self.assertEqual(context["filteredInternalEventCount"], 2)
        self.assertEqual(context["deduplicatedEventCount"], 1)
        self.assertEqual(context["redactedEventCount"], 1)
        self.assertEqual(context["retainedEventCount"], 1)

    def test_model_timeline_context_bounds_long_derived_titles(self) -> None:
        long_text = "记忆整理标题必须在受治理投影边界稳定限长" * 12
        self._record(
            18,
            20,
            app="com.openai.codex",
            source="codex_history",
            text=long_text,
            context_group_id="task:memory-title-boundary",
        )
        store = DailyActivityTimelineStore(
            self.db_path,
            project=self.project,
            timezone_name="Asia/Shanghai",
        )
        timeline = store.build_draft(
            "2026-07-17",
            generated_at_ms=self._ms(18, 21),
        )["timeline"]
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            context = load_activity_timeline_context(
                conn,
                project=self.project,
                timeline_date="2026-07-17",
                timeline_id=timeline["timelineId"],
            )

        self.assertEqual(len(context["segments"]), 1)
        segment = context["segments"][0]
        self.assertEqual(len(segment["title"]), 160)
        self.assertTrue(segment["title"].startswith(long_text[:140]))
        self.assertTrue(segment["title"].endswith("…"))
        self.assertGreater(len(segment["summary"]), len(segment["title"]))
        self.assertEqual(segment["source"]["id"], timeline["timelineId"])

    def test_model_timeline_context_filters_both_event_tombstone_shapes(self) -> None:
        source_event_id = self._record(
            19,
            0,
            app="com.apple.TextEdit",
            source="squirrel_commit",
            text="source_event_id tombstone 不得进入模型上下文",
        )
        memory_id_event_id = self._record(
            19,
            1,
            app="com.google.Chrome",
            source="browser_extension",
            text="memory_id tombstone 不得进入模型上下文",
        )
        store = DailyActivityTimelineStore(
            self.db_path,
            project=self.project,
            timezone_name="Asia/Shanghai",
        )
        timeline = store.build_draft(
            "2026-07-17",
            generated_at_ms=self._ms(19, 2),
        )["timeline"]
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                """
                INSERT INTO memory_tombstones(
                    created_at_ms, target_type, target_value, reason, active
                ) VALUES (?, ?, ?, 'user_forget', 1)
                """,
                (
                    (self._ms(19, 3), "source_event_id", str(source_event_id)),
                    (self._ms(19, 3), "memory_id", f"event:{memory_id_event_id}"),
                ),
            )
            conn.row_factory = sqlite3.Row
            context = load_activity_timeline_context(
                conn,
                project=self.project,
                timeline_date="2026-07-17",
                timeline_id=timeline["timelineId"],
            )

        serialized = json.dumps(context, ensure_ascii=False)
        self.assertNotIn("不得进入模型上下文", serialized)
        self.assertEqual(context["retainedEventCount"], 0)
        self.assertEqual(context["segments"], [])

    def test_approval_and_projection_outbox_share_one_transaction(self) -> None:
        self._record(
            15,
            0,
            app="com.apple.TextEdit",
            source="squirrel_rime_commit",
            text="验证时间线批准事务",
        )
        store = DailyActivityTimelineStore(
            self.db_path,
            project=self.project,
            timezone_name="Asia/Shanghai",
        )
        draft = store.build_draft(
            "2026-07-17",
            generated_at_ms=self._ms(15, 5),
        )["timeline"]

        with patch(
            "rag_ime.activity_timeline.enqueue_memory_projection",
            side_effect=RuntimeError("projection outbox unavailable"),
        ), self.assertRaisesRegex(RuntimeError, "outbox unavailable"):
            store.approve(
                draft["timelineId"],
                expected_source_event_hash=draft["sourceEventHash"],
                approved_by="user:test",
                confirm_text="approve",
                approved_at_ms=self._ms(15, 10),
            )

        self.assertEqual(store.review(draft["timelineId"])["status"], "draft")
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM memory_books").fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM memory_projection_outbox
                    WHERE aggregate_id = ?
                    """,
                    (draft["timelineId"],),
                ).fetchone()[0],
                0,
            )

    def test_decisions_are_recorded_without_raw_rejection_reason(self) -> None:
        observability = PersonalContextObservability(
            self.db_path,
            project=self.project,
        )
        observability.initialize()
        store = DailyActivityTimelineStore(
            self.db_path,
            project=self.project,
            timezone_name="Asia/Shanghai",
            observability=observability,
        )
        self._record(
            16,
            0,
            app="com.apple.TextEdit",
            source="squirrel_rime_commit",
            text="生成可批准的活动时间线",
        )
        first = store.build_draft(
            "2026-07-17",
            generated_at_ms=self._ms(16, 1),
        )["timeline"]
        store.approve(
            first["timelineId"],
            expected_source_event_hash=first["sourceEventHash"],
            approved_by="control-center-user",
            confirm_text="approve",
            approved_at_ms=self._ms(16, 2),
        )

        self._record(
            16,
            5,
            app="com.google.Chrome",
            source="browser_extension",
            text="生成第二份待拒绝的活动时间线",
        )
        second = store.build_draft(
            "2026-07-17",
            generated_at_ms=self._ms(16, 6),
        )["timeline"]
        store.reject(
            second["timelineId"],
            reason="private rejection detail",
            rejected_by="control-center-user",
            rejected_at_ms=self._ms(16, 7),
        )

        snapshot = observability.snapshot(current_ms=self._ms(16, 8))
        self.assertEqual(
            snapshot["drafts"]["activityTimelineByStatus"],
            {"approved": 1, "rejected": 1},
        )
        self.assertEqual(
            snapshot["drafts"]["latestDecisionByOutcome"],
            {"accepted": 1, "rejected": 1},
        )
        self.assertEqual(
            snapshot["drafts"]["generatedByKind"]["activity_timeline"],
            2,
        )
        self.assertNotIn(
            "private rejection detail",
            json.dumps(snapshot, ensure_ascii=False),
        )

    def _record(
        self,
        hour: int,
        minute: int,
        *,
        app: str,
        source: str,
        text: str,
        project: str | None = None,
        context_group_id: str = "",
    ) -> int:
        result = self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=self._ms(hour, minute),
                source=source,
                committed_text=text,
                privacy_disposition="allowed",
                app=app,
                project=self.project if project is None else project,
                context_group_id=context_group_id,
                context_group_level="topic" if context_group_id else "app",
            )
        )
        return int(result.split(":", 1)[1])

    def _ms(self, hour: int, minute: int) -> int:
        return int(
            datetime(
                2026,
                7,
                17,
                hour,
                minute,
                tzinfo=self.zone,
            ).timestamp()
            * 1_000
        )


class _TwoBlockActivityOrganizer:
    provider_name = "fake-two-block-activity-organizer"

    def begin_run(self, run_id: str, *, frozen_input_sha256: str = "") -> dict[str, object]:
        del run_id, frozen_input_sha256
        return {}

    def organize_activity_timeline(self, *, packet: object) -> dict[str, object]:
        refs = list(getattr(packet, "event_refs"))
        return {
            "organization": {
                "schemaVersion": ACTIVITY_ORGANIZATION_OUTPUT_VERSION,
                "activities": [
                    {
                        "title": "梳理个人记忆与评测方案",
                        "summary": "讨论个人记忆重构、上下文注入与 LongMemEval 评测依据。",
                        "eventRefs": refs[:3],
                        "confidence": 0.95,
                        "boundaryBasis": "前三条来源共同围绕个人记忆方案与评测依据。",
                    },
                    {
                        "title": "实现跨应用活动时间线",
                        "summary": "实现跨应用活动时间线，并保留一条已脱敏来源。",
                        "eventRefs": refs[3:],
                        "confidence": 0.94,
                        "boundaryBasis": "后两条来源来自同一文档实现活动。",
                    },
                ],
                "unclassified": [],
            },
            "receipt": {
                "membershipSha256": getattr(packet, "membership_sha256"),
                "verdict": "pass",
            },
        }

    def finish_run(self) -> dict[str, object]:
        return {}

    def fail_run(self, error: BaseException) -> dict[str, object]:
        del error
        return {}


if __name__ == "__main__":
    unittest.main()
