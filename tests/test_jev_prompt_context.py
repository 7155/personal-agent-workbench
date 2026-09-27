"""Jev purposes override legacy collaboration roles without changing Room chat."""
import unittest

from rag_ime.rooms.prompt_context import room_participant_prompt


class JevPromptContextTests(unittest.TestCase):
    def test_every_role_can_receive_each_exact_purpose(self):
        for role in ("coordinator", "implementer", "reviewer"):
            for purpose, operation in (("plan", "plan_submit"), ("execute", "result_submit"),
                                       ("verify", "verification_submit"), ("synthesize", "final_submit")):
                with self.subTest(role=role, purpose=purpose):
                    target = {"id": "actor", "status": "active", "collaborationRole": role}
                    room = {"id": "room", "roomKind": "collaboration", "participants": [target],
                            "_jevExecutionPurpose": purpose}
                    value = room_participant_prompt(room, target, "")
                    self.assertIn("op=" + operation, value)
                    self.assertIn("主要对话和规划者也可承担已分派任务", value)
                    self.assertNotIn("skill_load 加载 facilitate-room", value)
                    self.assertNotIn("没有结构化 WorkItem 时", value)
                    self.assertNotIn("提交 typed work_result", value)

    def test_regular_room_keeps_facilitator_and_partner_guidance(self):
        for role, expected in (("coordinator", "当前职责：Room Facilitator"),
                               ("implementer", "当前职责：Room Partner")):
            target = {"id": "actor", "status": "active", "collaborationRole": role}
            value = room_participant_prompt({"participants": [target]}, target, "")
            self.assertIn(expected, value)
            self.assertNotIn("当前 Jev 执行目的", value)

    def test_jev_revision_feedback_does_not_instruct_legacy_retry(self):
        target = {"id": "actor", "status": "active", "collaborationRole": "coordinator"}
        work = {"id": "work", "state": "active", "revision": 1,
                "currentOwnerParticipantId": "actor", "accountableParticipantId": "actor",
                "blocker": {"reviewFeedback": "修复结果"}, "recommendedOperation": "retry"}
        value = room_participant_prompt({"participants": [target], "workItems": [work],
                                        "_jevExecutionPurpose": "execute"}, target, "", work_item=work)
        self.assertIn("revision=1", value)
        self.assertNotIn("op=retry", value)
        self.assertIn("op=result_submit", value)
