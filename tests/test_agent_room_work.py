from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from rag_ime.rooms.work import AgentRoomWorkStore
from rag_ime.rooms.store import AgentRoomStore
from rag_ime.agent_sessions import AgentSessionStore


class AgentRoomWorkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-room-work-")
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "rag-ime.sqlite"
        self.sessions = AgentSessionStore(self.db_path)
        self.sessions.initialize()
        self.coordinator = self.sessions.create(title="协调者")
        self.worker = self.sessions.create(title="实施者")
        self.researcher = self.sessions.create(title="调研者")
        self.rooms = AgentRoomStore(self.db_path, room_dir=self.root / "rooms")
        self.rooms.initialize()
        self.room = self.rooms.create(
            title="责任协议",
            routing_policy="moderator",
            participants=[
                {
                    "sessionId": self.coordinator["id"],
                    "roleId": "coordinator",
                    "roleVersion": "1",
                    "displayName": "协调者",
                    "collaborationRole": "coordinator",
                },
                {
                    "sessionId": self.worker["id"],
                    "roleId": "worker",
                    "roleVersion": "1",
                    "displayName": "实施者",
                    "collaborationRole": "implementer",
                },
                {
                    "sessionId": self.researcher["id"],
                    "roleId": "researcher",
                    "roleVersion": "1",
                    "displayName": "调研者",
                    "collaborationRole": "researcher",
                },
            ],
        )
        self.coordinator_participant = self.room["participants"][0]
        self.worker_participant = self.room["participants"][1]
        self.researcher_participant = self.room["participants"][2]
        self.work = AgentRoomWorkStore(self.db_path)
        self.work.initialize()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_assignment_changes_owner_only_after_target_turn_is_accepted(self) -> None:
        assigned, created = self.work.assign(
            str(self.coordinator["id"]),
            self._assignment("root-1", self.worker_participant["id"]),
            root_turn_id="room-turn:1",
            topic_id=str(self.room["activeTopicId"]),
            created_at_ms=10,
        )

        self.assertTrue(created)
        self.assertEqual(assigned["state"], "queued")
        self.assertEqual(
            assigned["currentOwnerParticipantId"],
            self.coordinator_participant["id"],
        )
        self.assertEqual(
            assigned["offeredToParticipantId"],
            self.worker_participant["id"],
        )

        active = self.work.accept_assignment(
            str(assigned["id"]),
            target_participant_id=str(self.worker_participant["id"]),
            accepted_turn_id="turn:worker",
            updated_at_ms=20,
        )
        self.assertEqual(active["state"], "active")
        self.assertEqual(
            active["currentOwnerParticipantId"],
            self.worker_participant["id"],
        )

        submitted = self.work.submit(
            str(self.worker["id"]),
            {
                "workId": active["id"],
                "resultSummary": "完成了实现和窄测试。",
                "artifactRefs": ["src/room.py"],
                "evidenceRefs": ["test:test_room"],
            },
            updated_at_ms=30,
        )
        self.assertEqual(submitted["state"], "review")

        completed = self.work.accept(
            str(self.coordinator["id"]),
            {
                "workId": active["id"],
                "expectedRevision": submitted["revision"],
                "operabilityVerdict": "passed",
                "requirementVerdict": "satisfied",
                "evidenceRefs": ["test:test_room", "artifact:src/room.py"],
                "reason": "窄测试与产物核对通过。",
            },
            updated_at_ms=40,
        )
        self.assertEqual(completed["state"], "done")
        self.assertEqual(completed["completedAtMs"], 40)
        self.assertEqual(
            completed["review"],
            {
                "operabilityVerdict": "passed",
                "requirementVerdict": "satisfied",
                "evidenceRefs": ["test:test_room", "artifact:src/room.py"],
                "reason": "窄测试与产物核对通过。",
                "reviewerParticipantId": self.coordinator_participant["id"],
                "reviewedAtMs": 40,
            },
        )
        self.assertEqual(
            self.rooms.get(str(self.room["id"]))["workItems"][0]["state"],
            "done",
        )
        with self.assertRaisesRegex(ValueError, "active or review"):
            self.work.authoritative_owner(
                str(completed["id"]),
                room_id=str(self.room["id"]),
            )
        with sqlite3.connect(self.db_path) as conn:
            events = [
                row[0]
                for row in conn.execute(
                    """
                    SELECT event_type FROM agent_room_work_events
                    WHERE work_id = ? ORDER BY sequence
                    """,
                    (active["id"],),
                )
            ]
        self.assertEqual(
            events,
            ["assigned", "accepted", "submitted", "completed"],
        )

    def test_assignment_is_idempotent_and_rejects_ancestor_bounce(self) -> None:
        payload = self._assignment("root-2", self.worker_participant["id"])
        first, created = self.work.assign(str(self.coordinator["id"]), payload)
        repeated, repeated_created = self.work.assign(
            str(self.coordinator["id"]),
            payload,
        )

        self.assertTrue(created)
        self.assertFalse(repeated_created)
        self.assertEqual(repeated["id"], first["id"])

        active = self.work.accept_assignment(
            str(first["id"]),
            target_participant_id=str(self.worker_participant["id"]),
            accepted_turn_id="turn:worker",
        )
        with self.assertRaisesRegex(ValueError, "ancestor owner"):
            self.work.assign(
                str(self.worker["id"]),
                {
                    **self._assignment(
                        "child-1",
                        self.coordinator_participant["id"],
                    ),
                    "parentWorkId": active["id"],
                },
            )

    def test_revision_budget_requires_escalation_after_two_returns(self) -> None:
        assigned, _ = self.work.assign(
            str(self.coordinator["id"]),
            self._assignment("root-3", self.worker_participant["id"]),
        )
        active = self.work.accept_assignment(
            str(assigned["id"]),
            target_participant_id=str(self.worker_participant["id"]),
            accepted_turn_id="turn:worker",
        )
        for revision in (1, 2):
            self.work.submit(
                str(self.worker["id"]),
                {
                    "workId": active["id"],
                    "resultSummary": f"第 {revision} 版",
                    "evidenceRefs": [f"test:revision-{revision}"],
                },
            )
            active = self.work.return_for_revision(
                str(self.coordinator["id"]),
                {
                    "workId": active["id"],
                    "expectedRevision": active["revision"],
                    "operabilityVerdict": "passed",
                    "requirementVerdict": "not_satisfied",
                    "evidenceRefs": [f"review:revision-{revision}"],
                    "reason": f"第 {revision} 次反馈",
                },
            )
            self.assertEqual(active["revision"], revision)
            self.assertEqual(
                active["review"]["requirementVerdict"],
                "not_satisfied",
            )
            self.assertEqual(
                active["review"]["evidenceRefs"],
                [f"review:revision-{revision}"],
            )

        self.work.submit(
            str(self.worker["id"]),
            {
                "workId": active["id"],
                "resultSummary": "第三版",
                "evidenceRefs": ["test:revision-3"],
            },
        )
        with self.assertRaisesRegex(ValueError, "revision limit"):
            self.work.return_for_revision(
                str(self.coordinator["id"]),
                {
                    "workId": active["id"],
                    "expectedRevision": active["revision"],
                    "operabilityVerdict": "failed",
                    "requirementVerdict": "not_satisfied",
                    "evidenceRefs": ["review:revision-3"],
                    "reason": "仍不通过",
                },
            )

    def test_review_requires_explicit_axes_evidence_and_fresh_revision(self) -> None:
        assigned, _ = self.work.assign(
            str(self.coordinator["id"]),
            self._assignment(
                "root-review-contract",
                self.worker_participant["id"],
            ),
        )
        active = self.work.accept_assignment(
            str(assigned["id"]),
            target_participant_id=str(self.worker_participant["id"]),
            accepted_turn_id="turn:worker-review-contract",
        )
        submitted = self.work.submit(
            str(self.worker["id"]),
            {
                "workId": active["id"],
                "resultSummary": "等待显式双轴验收。",
                "evidenceRefs": ["test:review-contract"],
            },
        )

        required = {
            "expectedRevision": submitted["revision"],
            "operabilityVerdict": "passed",
            "requirementVerdict": "satisfied",
            "evidenceRefs": ["review:test-review-contract"],
            "reason": "双轴验收通过。",
        }
        for missing in required:
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                self.work.accept(
                    str(self.coordinator["id"]),
                    {
                        "workId": submitted["id"],
                        **{
                            key: value
                            for key, value in required.items()
                            if key != missing
                        },
                    },
                )

        with self.assertRaisesRegex(
            ValueError,
            "operability.*passed.*requirement.*satisfied",
        ):
            self.work.accept(
                str(self.coordinator["id"]),
                {
                    "workId": submitted["id"],
                    **required,
                    "requirementVerdict": "not_satisfied",
                },
            )
        with self.assertRaisesRegex(ValueError, "revision changed"):
            self.work.accept(
                str(self.coordinator["id"]),
                {
                    "workId": submitted["id"],
                    **required,
                    "expectedRevision": submitted["revision"] + 1,
                },
            )
        with self.assertRaisesRegex(ValueError, "accept requires a concrete reason"):
            self.work.accept(
                str(self.coordinator["id"]),
                {
                    "workId": submitted["id"],
                    **{key: value for key, value in required.items() if key != "reason"},
                },
            )
        with self.assertRaisesRegex(ValueError, "concrete reason"):
            self.work.return_for_revision(
                str(self.coordinator["id"]),
                {
                    "workId": submitted["id"],
                    **{
                        key: value
                        for key, value in required.items()
                        if key != "reason"
                    },
                    "requirementVerdict": "not_satisfied",
                },
            )
        with self.assertRaisesRegex(ValueError, "must be accepted"):
            self.work.return_for_revision(
                str(self.coordinator["id"]),
                {
                    "workId": submitted["id"],
                    **required,
                    "reason": "不应返修全通过结论",
                },
            )

        unchanged = self.work.get(
            str(submitted["id"]),
            room_id=str(self.room["id"]),
        )
        self.assertEqual(unchanged["state"], "review")
        self.assertEqual(
            unchanged["review"],
            {
                "operabilityVerdict": "",
                "requirementVerdict": "",
                "evidenceRefs": [],
                "reason": "",
                "reviewerParticipantId": "",
                "reviewedAtMs": None,
            },
        )

    def test_failed_work_retries_same_contract_with_revision_fence(self) -> None:
        assigned, _ = self.work.assign(
            str(self.coordinator["id"]),
            self._assignment("failed-retry", self.worker_participant["id"]),
        )
        active = self.work.accept_assignment(
            str(assigned["id"]),
            target_participant_id=str(self.worker_participant["id"]),
            accepted_turn_id="turn:failed-worker",
        )
        failed = self.work.escalate(
            str(self.worker["id"]),
            {
                "workId": active["id"],
                "reason": "原 Partner Tool loop 失败",
                "nextStep": "由空闲伙伴继续同一合同",
            },
        )

        with self.assertRaisesRegex(ValueError, "revision changed"):
            self.work.retry(
                str(failed["id"]),
                actor_participant_id=str(self.coordinator_participant["id"]),
                current_owner_participant_id=str(
                    self.researcher_participant["id"]
                ),
                expected_revision=1,
                reason="陈旧 revision 不得重试",
            )

        retried = self.work.retry(
            str(failed["id"]),
            actor_participant_id=str(self.coordinator_participant["id"]),
            current_owner_participant_id=str(self.researcher_participant["id"]),
            expected_revision=0,
            reason="保留合同并改派空闲伙伴",
            updated_at_ms=50,
        )

        self.assertEqual(retried["id"], failed["id"])
        self.assertEqual(retried["state"], "active")
        self.assertEqual(retried["revision"], 1)
        self.assertEqual(
            retried["currentOwnerParticipantId"],
            self.researcher_participant["id"],
        )
        for key in ("objective", "expectedOutput", "acceptanceCriteria"):
            self.assertEqual(retried[key], active[key])
        self.assertEqual(retried["resultSummary"], "")
        self.assertEqual(retried["evidenceRefs"], [])
        self.assertEqual(retried["blocker"]["retryReason"], "保留合同并改派空闲伙伴")
        self.assertEqual(
            [event["eventType"] for event in self.work.list_events(str(active["id"]))],
            ["assigned", "accepted", "escalated", "resumed"],
        )

    def test_submit_requires_evidence_and_closed_children(self) -> None:
        assigned, _ = self.work.assign(
            str(self.coordinator["id"]),
            self._assignment("root-4", self.worker_participant["id"]),
        )
        active = self.work.accept_assignment(
            str(assigned["id"]),
            target_participant_id=str(self.worker_participant["id"]),
            accepted_turn_id="turn:worker",
        )
        with self.assertRaisesRegex(ValueError, "artifactRefs or evidenceRefs"):
            self.work.submit(
                str(self.worker["id"]),
                {
                    "workId": active["id"],
                    "resultSummary": "只有一句完成声明",
                },
            )

        child, _ = self.work.assign(
            str(self.worker["id"]),
            {
                **self._assignment(
                    "child-2",
                    self.researcher_participant["id"],
                ),
                "parentWorkId": active["id"],
            },
        )
        self.work.accept_assignment(
            str(child["id"]),
            target_participant_id=str(self.researcher_participant["id"]),
            accepted_turn_id="turn:researcher",
        )
        with self.assertRaisesRegex(ValueError, "child WorkItems are open"):
            self.work.submit(
                str(self.worker["id"]),
                {
                    "workId": active["id"],
                    "resultSummary": "子任务尚未闭环",
                    "evidenceRefs": ["test:parent"],
                },
            )

    def test_accept_cannot_upgrade_proposed_failed_or_unverified_submission(
        self,
    ) -> None:
        for root_turn_id in ("", "room-turn:ordinary-without-jev"):
            with self.subTest(root_turn_id=root_turn_id):
                assigned, _ = self.work.assign(
                    str(self.coordinator["id"]),
                    self._assignment(f"root-proposed-gate:{root_turn_id}", self.worker_participant["id"]),
                    root_turn_id=root_turn_id,
                )
                active = self.work.accept_assignment(
                    str(assigned["id"]),
                    target_participant_id=str(self.worker_participant["id"]),
                    accepted_turn_id="turn:proposed-gate",
                )
                submitted = self.work.submit(
                    str(self.worker["id"]),
                    {
                        "workId": active["id"],
                        "resultSummary": "浏览器执行失败，两轴均未验证。",
                        "evidenceRefs": ["review:browser-run-failed"],
                        "proposedOperabilityVerdict": "failed",
                        "proposedRequirementVerdict": "unverified",
                    },
                    updated_at_ms=30,
                )
                self.assertEqual(submitted["proposedOperabilityVerdict"], "failed")
                self.assertEqual(submitted["proposedRequirementVerdict"], "unverified")

                with self.assertRaisesRegex(ValueError, "supersed"):
                    self.work.accept(
                        str(self.coordinator["id"]),
                        {
                            "workId": submitted["id"],
                            "expectedRevision": submitted["revision"],
                            "operabilityVerdict": "passed",
                            "requirementVerdict": "satisfied",
                            "evidenceRefs": ["review:facilitator-claim"],
                            "reason": "试图无视 Partner 的失败结论直接验收。",
                        },
                    )

                unchanged = self.work.get(
                    str(submitted["id"]),
                    room_id=str(self.room["id"]),
                )
                self.assertEqual(unchanged["state"], "review")
                self.assertEqual(unchanged["proposedOperabilityVerdict"], "failed")
                self.assertEqual(unchanged["proposedRequirementVerdict"], "unverified")

    def test_accept_over_failed_proposal_requires_valid_superseding_review(
        self,
    ) -> None:
        assigned, _ = self.work.assign(
            str(self.coordinator["id"]),
            self._assignment("root-supersede", self.worker_participant["id"]),
        )
        active = self.work.accept_assignment(
            str(assigned["id"]),
            target_participant_id=str(self.worker_participant["id"]),
            accepted_turn_id="turn:supersede-worker",
        )
        submitted = self.work.submit(
            str(self.worker["id"]),
            {
                "workId": active["id"],
                "resultSummary": "两轴 UNVERIFIED，遗留 HIGH 风险。",
                "evidenceRefs": ["review:integration-unverified"],
                "proposedOperabilityVerdict": "unverified",
                "proposedRequirementVerdict": "unverified",
            },
            updated_at_ms=30,
        )

        accept_payload = {
            "workId": submitted["id"],
            "expectedRevision": submitted["revision"],
            "operabilityVerdict": "passed",
            "requirementVerdict": "satisfied",
            "evidenceRefs": ["review:superseding-review"],
            "reason": "复核 WorkItem 已通过双轴验证。",
        }
        with self.assertRaisesRegex(ValueError, "superseding review WorkItem"):
            self.work.accept(
                str(self.coordinator["id"]),
                {**accept_payload, "supersededByWorkId": "room-work:missing"},
            )

        stale_review = self.work.create(
            room_id=str(self.room["id"]),
            objective="早于失败提交的复核不算数",
            expected_output="记录",
            current_owner_participant_id=str(self.researcher_participant["id"]),
            created_by_participant_id=str(self.coordinator_participant["id"]),
            client_message_id="stale-review",
            acceptance_criteria=["复核"],
            created_at_ms=10,
        )
        with self.assertRaisesRegex(ValueError, "superseding review WorkItem"):
            self.work.accept(
                str(self.coordinator["id"]),
                {**accept_payload, "supersededByWorkId": stale_review["id"]},
            )

        fresh_review = self.work.create(
            room_id=str(self.room["id"]),
            objective="对失败提交的新证据复核",
            expected_output="双轴复核结论",
            current_owner_participant_id=str(self.researcher_participant["id"]),
            created_by_participant_id=str(self.coordinator_participant["id"]),
            client_message_id="fresh-review",
            acceptance_criteria=["复核"],
            created_at_ms=40,
        )
        with self.assertRaisesRegex(ValueError, "superseding review WorkItem"):
            self.work.accept(
                str(self.coordinator["id"]),
                {**accept_payload, "supersededByWorkId": fresh_review["id"]},
            )

        reviewed = self.work.submit(
            str(self.researcher["id"]),
            {
                "workId": fresh_review["id"],
                "resultSummary": "复跑真实路径，两轴通过。",
                "evidenceRefs": ["review:rerun-passed"],
                "proposedOperabilityVerdict": "passed",
                "proposedRequirementVerdict": "satisfied",
            },
            updated_at_ms=50,
        )
        self.assertEqual(reviewed["state"], "review")

        with self.assertRaisesRegex(ValueError, "direct child"):
            self.work.accept(
                str(self.coordinator["id"]),
                {**accept_payload, "supersededByWorkId": fresh_review["id"]},
                updated_at_ms=55,
            )

        linked_review = self.work.create(
            room_id=str(self.room["id"]),
            objective="对原失败提交进行新证据复核",
            expected_output="双轴复核结论",
            current_owner_participant_id=str(self.researcher_participant["id"]),
            created_by_participant_id=str(self.coordinator_participant["id"]),
            client_message_id="linked-review",
            acceptance_criteria=["复核"],
            parent_work_id=str(submitted["id"]),
            created_at_ms=56,
        )
        linked_review = self.work.submit(
            str(self.researcher["id"]),
            {
                "workId": linked_review["id"],
                "resultSummary": "复跑原任务真实路径，两轴通过。",
                "evidenceRefs": ["review:linked-rerun-passed"],
                "proposedOperabilityVerdict": "passed",
                "proposedRequirementVerdict": "satisfied",
            },
            updated_at_ms=57,
        )
        self.assertEqual(linked_review["parentWorkId"], submitted["id"])

        completed = self.work.accept(
            str(self.coordinator["id"]),
            {**accept_payload, "supersededByWorkId": linked_review["id"]},
            updated_at_ms=60,
        )
        self.assertEqual(completed["state"], "done")
        self.assertEqual(completed["proposedOperabilityVerdict"], "unverified")
        self.assertEqual(completed["proposedRequirementVerdict"], "unverified")
        events = self.work.list_events(str(submitted["id"]))
        self.assertEqual(events[-1]["eventType"], "completed")
        self.assertEqual(
            events[-1]["payload"]["supersededByWorkId"],
            linked_review["id"],
        )

    def test_retry_resets_proposed_verdicts_with_result_fields(self) -> None:
        assigned, _ = self.work.assign(
            str(self.coordinator["id"]),
            self._assignment("root-proposed-retry", self.worker_participant["id"]),
        )
        active = self.work.accept_assignment(
            str(assigned["id"]),
            target_participant_id=str(self.worker_participant["id"]),
            accepted_turn_id="turn:proposed-retry",
        )
        self.work.submit(
            str(self.worker["id"]),
            {
                "workId": active["id"],
                "resultSummary": "失败提交",
                "evidenceRefs": ["review:failed"],
                "proposedOperabilityVerdict": "failed",
                "proposedRequirementVerdict": "not_satisfied",
            },
        )
        returned = self.work.return_for_revision(
            str(self.coordinator["id"]),
            {
                "workId": active["id"],
                "expectedRevision": 0,
                "operabilityVerdict": "failed",
                "requirementVerdict": "not_satisfied",
                "evidenceRefs": ["review:returned"],
                "reason": "按提交结论退回修订。",
            },
        )
        self.assertEqual(returned["state"], "active")
        failed = self.work.escalate(
            str(self.worker["id"]),
            {
                "workId": active["id"],
                "reason": "修订失败",
                "nextStep": "改派空闲伙伴",
            },
        )
        retried = self.work.retry(
            str(failed["id"]),
            actor_participant_id=str(self.coordinator_participant["id"]),
            current_owner_participant_id=str(self.researcher_participant["id"]),
            expected_revision=1,
            reason="改派后重跑同一合同",
        )
        self.assertEqual(retried["proposedOperabilityVerdict"], "")
        self.assertEqual(retried["proposedRequirementVerdict"], "")

    def test_submit_infers_proposed_verdicts_from_failed_summary(self) -> None:
        assigned, _ = self.work.assign(
            str(self.coordinator["id"]),
            self._assignment("root-infer-failed", self.worker_participant["id"]),
        )
        active = self.work.accept_assignment(
            str(assigned["id"]),
            target_participant_id=str(self.worker_participant["id"]),
            accepted_turn_id="turn:infer-failed",
        )
        submitted = self.work.submit(
            str(self.worker["id"]),
            {
                "workId": active["id"],
                "resultSummary": "status: FAILED\n真实路径未跑通。",
                "evidenceRefs": ["review:status-failed"],
            },
        )
        self.assertEqual(submitted["proposedOperabilityVerdict"], "failed")
        self.assertEqual(submitted["proposedRequirementVerdict"], "not_satisfied")
        with self.assertRaisesRegex(ValueError, "supersed"):
            self.work.accept(
                str(self.coordinator["id"]),
                {
                    "workId": submitted["id"],
                    "expectedRevision": submitted["revision"],
                    "operabilityVerdict": "passed",
                    "requirementVerdict": "satisfied",
                    "evidenceRefs": ["review:dishonest"],
                    "reason": "试图把 FAILED 提交写成通过。",
                },
            )

    def test_list_for_root_is_not_truncated_by_room_recent_limit(self) -> None:
        room_id = str(self.room["id"])
        owner_id = str(self.coordinator_participant["id"])
        oldest = self.work.create(
            room_id=room_id,
            objective="最早 Root 的未完成验收项",
            expected_output="必须仍可被终态检查读取",
            current_owner_participant_id=owner_id,
            created_by_participant_id=owner_id,
            client_message_id="oldest-root-item",
            root_turn_id="room-turn:oldest",
            acceptance_criteria=["精确读取"],
            created_at_ms=1,
        )
        for index in range(200):
            self.work.create(
                room_id=room_id,
                objective=f"后续 Room 工作 {index}",
                expected_output="占据普通最近列表",
                current_owner_participant_id=owner_id,
                created_by_participant_id=owner_id,
                client_message_id=f"recent-room-item:{index}",
                root_turn_id=f"room-turn:recent:{index}",
                acceptance_criteria=["保留历史 Root 可寻址性"],
                created_at_ms=10 + index,
            )

        recent_ids = {
            str(item["id"])
            for item in self.work.list(room_id=room_id, limit=200)
        }
        exact = self.work.list_for_root(
            room_id=room_id,
            root_turn_id="room-turn:oldest",
        )

        self.assertNotIn(str(oldest["id"]), recent_ids)
        self.assertEqual([item["id"] for item in exact], [oldest["id"]])

    def _assignment(
        self,
        client_message_id: str,
        target_participant_id: object,
    ) -> dict[str, object]:
        return {
            "targetParticipantId": target_participant_id,
            "clientMessageId": client_message_id,
            "objective": "检查 Room 责任协议",
            "expectedOutput": "实现、证据和风险说明",
            "acceptanceCriteria": [
                "目标回合接受后才转移 owner",
                "交付可被协调者验收",
            ],
        }


if __name__ == "__main__":
    unittest.main()
