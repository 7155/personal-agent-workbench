from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rag_ime.deepseek_config import load_deepseek_config
from rag_ime.deepseek_memory_organizer import (
    ActivitySemanticVerificationError,
    DEFAULT_MEMORY_ORGANIZATION_INSTRUCTION,
    DeepSeekMemoryOrganizer,
    DeepSeekMemoryOrganizerError,
    ManagedPiMemoryOrganizer,
    _bind_atom_first_canonical_evidence,
    _bind_curation_local_create_references,
    _curation_repair_preserves_unflagged_actions,
    _owner_memory_model_bundle,
)
from rag_ime.activity_timeline_curation import (
    ACTIVITY_ORGANIZATION_OUTPUT_VERSION,
    ACTIVITY_ORGANIZATION_SCORE_FIELDS,
    ACTIVITY_ORGANIZATION_VERDICT_VERSION,
    build_activity_organization_packet,
)


class DeepSeekMemoryOrganizerTests(unittest.TestCase):
    def test_managed_luna_activity_organization_requires_independent_pass(self) -> None:
        phases: list[str] = []
        test_case = self

        class FakeExecutor:
            provider = "openai-codex"
            model_id = "gpt-5.6-luna"

            def complete(
                self,
                *,
                messages,
                max_tokens=None,
                phase="model-call",
                isolated=False,
            ):
                del messages, max_tokens
                phases.append(phase)
                if phase == "activity-verifier":
                    test_case.assertTrue(isolated)
                    payload = {
                        "schemaVersion": ACTIVITY_ORGANIZATION_VERDICT_VERSION,
                        "verdict": "pass",
                        "scores": {
                            field: 5 for field in ACTIVITY_ORGANIZATION_SCORE_FIELDS
                        },
                        "issues": [],
                        "strengths": ["标题与来源一致。"],
                    }
                else:
                    payload = {
                        "schemaVersion": ACTIVITY_ORGANIZATION_OUTPUT_VERSION,
                        "activities": [
                            {
                                "title": "修复日记语义整理",
                                "summary": "检查并修复每日日记直接展示原句的问题。",
                                "eventRefs": ["e1", "e2"],
                                "confidence": 0.96,
                                "boundaryBasis": "两条输入指向同一项日记语义修复。",
                            }
                        ],
                        "unclassified": [],
                    }
                return {
                    "choices": [{"message": {"content": json.dumps(payload)}}],
                    "requestId": f"request:{phase}",
                    "turnId": f"turn:{phase}",
                    "provider": self.provider,
                    "model": self.model_id,
                    "usage": {},
                    "receipt": {},
                }

        packet = build_activity_organization_packet(
            [
                {
                    "id": 1,
                    "created_at_ms": 1_786_500_000_000,
                    "source": "voice_final",
                    "committed_text": "这个日记不要显示原句",
                    "recent_context": "正在检查每日日记语义",
                    "app": "RagImeControl",
                    "project": "project-a",
                    "context_group_id": "session:memory",
                },
                {
                    "id": 2,
                    "created_at_ms": 1_786_500_060_000,
                    "source": "voice_final",
                    "committed_text": "改成活动级概括",
                    "recent_context": "这个日记不要显示原句，改成活动级概括",
                    "app": "RagImeControl",
                    "project": "project-a",
                    "context_group_id": "session:memory",
                },
            ],
            timeline_id="activity-timeline:test",
            project="project-a",
            timeline_date="2026-08-12",
            timezone_name="Asia/Shanghai",
        )

        result = ManagedPiMemoryOrganizer(FakeExecutor()).organize_activity_timeline(
            packet=packet
        )

        self.assertEqual(phases, ["activity-organizer", "activity-verifier"])
        self.assertEqual(result["receipt"]["verdict"], "pass")
        self.assertEqual(
            result["organization"]["activities"][0]["title"],
            "修复日记语义整理",
        )
        self.assertNotIn(
            "activityId",
            result["organization"]["activities"][0],
        )

    def test_semantic_repair_repairs_missing_ledger_ref_before_reverification(self) -> None:
        phases: list[str] = []

        class FakeExecutor:
            provider = "openai-codex"
            model_id = "gpt-6-luna"

            def complete(self, *, messages, max_tokens=None, phase="model-call", isolated=False):
                del max_tokens
                self_test.assertTrue(isolated)
                phases.append(phase)
                if phase == "activity-semantic-contract-repair":
                    self_test.assertIn("missing=['e2']", messages[1]["content"])
                if phase in {
                    "activity-verifier",
                    "activity-repair-verifier",
                    "activity-repair-verifier-2",
                }:
                    passed = phase == "activity-repair-verifier-2"
                    payload = {
                        "schemaVersion": ACTIVITY_ORGANIZATION_VERDICT_VERSION,
                        "verdict": "pass" if passed else "iterate",
                        "scores": {
                            field: 5 if passed else 3
                            for field in ACTIVITY_ORGANIZATION_SCORE_FIELDS
                        },
                        "issues": [],
                        "strengths": [],
                    }
                else:
                    refs = ["e1"] if phase == "activity-semantic-repair" else ["e1", "e2"]
                    payload = {
                        "schemaVersion": ACTIVITY_ORGANIZATION_OUTPUT_VERSION,
                        "activities": [{
                            "title": "整理日记时间线",
                            "summary": "核对并整理当天的两条来源。",
                            "eventRefs": refs,
                            "confidence": 0.9,
                            "boundaryBasis": "两条来源描述同一次整理工作。",
                        }],
                        "unclassified": [],
                    }
                return {
                    "choices": [{"message": {"content": json.dumps(payload)}}],
                    "receipt": {"requestId": f"request:{phase}"},
                }

        self_test = self
        packet = build_activity_organization_packet(
            [
                {
                    "id": index,
                    "created_at_ms": 1_786_500_000_000 + index * 60_000,
                    "source": "voice_final",
                    "committed_text": f"第 {index} 条整理来源",
                    "recent_context": "整理当天时间线",
                    "app": "RagImeControl",
                    "project": "project-a",
                    "context_group_id": "session:memory",
                }
                for index in (1, 2)
            ],
            timeline_id="activity-timeline:semantic-contract-repair",
            project="project-a",
            timeline_date="2026-08-12",
            timezone_name="Asia/Shanghai",
        )

        result = ManagedPiMemoryOrganizer(FakeExecutor()).organize_activity_timeline(packet=packet)

        self.assertEqual(
            phases,
            [
                "activity-organizer",
                "activity-verifier",
                "activity-semantic-repair",
                "activity-semantic-contract-repair",
                "activity-repair-verifier",
                "activity-semantic-repair-2",
                "activity-repair-verifier-2",
            ],
        )
        self.assertEqual(result["organization"]["activities"][0]["eventRefs"], ["e1", "e2"])
        self.assertEqual(result["receipt"]["verdict"], "pass")
        self.assertTrue(result["receipt"]["semanticContractRepaired"])
        self.assertEqual(result["receipt"]["semanticRepairRounds"], 2)

    def test_activity_semantic_review_has_five_bounded_repair_rounds(self) -> None:
        phases: list[str] = []

        class AlwaysIterateExecutor:
            provider = "openai-codex"
            model_id = "gpt-6-luna"

            def complete(self, *, messages, max_tokens=None, phase="model-call", isolated=False):
                del messages, max_tokens
                assert isolated
                phases.append(phase)
                if "verifier" in phase:
                    payload = {
                        "schemaVersion": ACTIVITY_ORGANIZATION_VERDICT_VERSION,
                        "verdict": "iterate",
                        "scores": {field: 3 for field in ACTIVITY_ORGANIZATION_SCORE_FIELDS},
                        "issues": [],
                        "strengths": [],
                    }
                else:
                    payload = {
                        "schemaVersion": ACTIVITY_ORGANIZATION_OUTPUT_VERSION,
                        "activities": [{
                            "title": "整理时间线",
                            "summary": "核对当天来源并整理活动。",
                            "eventRefs": ["e1"],
                            "confidence": 0.9,
                            "boundaryBasis": "来源描述同一次整理工作。",
                        }],
                        "unclassified": [],
                    }
                return {"choices": [{"message": {"content": json.dumps(payload)}}]}

        packet = build_activity_organization_packet(
            [{
                "id": 1,
                "created_at_ms": 1_786_500_000_000,
                "source": "voice_final",
                "committed_text": "整理时间线",
                "recent_context": "",
                "app": "RagImeControl",
                "project": "project-a",
                "context_group_id": "session:memory",
            }],
            timeline_id="activity-timeline:five-repairs",
            project="project-a",
            timeline_date="2026-08-12",
            timezone_name="Asia/Shanghai",
        )

        with self.assertRaises(ActivitySemanticVerificationError) as caught:
            ManagedPiMemoryOrganizer(AlwaysIterateExecutor()).organize_activity_timeline(packet=packet)

        self.assertEqual(caught.exception.receipt["semanticRepairRounds"], 5)
        self.assertEqual(phases.count("activity-organizer"), 1)
        self.assertEqual(len([phase for phase in phases if phase.startswith("activity-semantic-repair")]), 5)
        self.assertEqual(len([phase for phase in phases if "verifier" in phase]), 6)

    def test_atom_first_binding_preserves_composite_reconstruction_evidence(self) -> None:
        result = _bind_atom_first_canonical_evidence(
            {
                "sourceDecisions": [
                    {
                        "sourceRef": "S1",
                        "disposition": "remember",
                        "confidence": 0.98,
                    }
                ],
                "memoryAtoms": [
                    {
                        "canonicalText": "历史输入先重建成完整表达再整理。",
                        "sourceEventIds": [7, 8],
                    }
                ],
            },
            bundle={
                "inputs": [
                    {
                        "sourceRef": "S1",
                        "sourceEventIds": [7, 8],
                        "evidenceIds": ["evidence:input:7", "evidence:input:8"],
                    }
                ]
            },
        )

        self.assertEqual(
            result["sourceDecisions"][0]["evidenceIds"],
            ["evidence:input:7", "evidence:input:8"],
        )
        self.assertEqual(result["sourceDecisions"][0]["evidenceId"], "")
        self.assertEqual(
            result["memoryAtoms"][0]["evidenceIds"],
            ["evidence:input:7", "evidence:input:8"],
        )

    def test_semantic_repair_may_update_all_actions_for_flagged_evidence_only(self) -> None:
        previous = {
            "attach": [["E1", "P1"], ["E1", "P2"], ["E2", "P3"]],
            "create": [{"e": "E2", "p": "", "text": "preserved fact"}],
            "update": [],
            "supersede": [],
            "merge": [],
            "retract": [],
            "ignore": [],
            "tagMerges": [],
        }
        repaired = {
            **previous,
            "attach": [["E2", "P3"]],
            "create": [
                *previous["create"],
                {"e": "E1", "p": "", "text": "independent fact"},
            ],
        }
        findings = [
            {
                "code": "attach_target_mismatch",
                "actionType": "attach",
                "actionIndex": 0,
                "evidenceRefs": ["E1"],
            }
        ]

        self.assertTrue(
            _curation_repair_preserves_unflagged_actions(
                previous,
                repaired,
                findings,
            )
        )
        repaired["attach"] = []
        self.assertFalse(
            _curation_repair_preserves_unflagged_actions(
                previous,
                repaired,
                findings,
            )
        )

    def test_local_new_atom_refs_fold_attach_evidence_into_create(self) -> None:
        normalized, receipts = _bind_curation_local_create_references(
            {
                "attach": [["E23", "P22"], ["E23", "P23"]],
                "create": [
                    {"e": "E26", "p": "P22", "text": "first"},
                    {"e": "E26", "p": "P23", "text": "second"},
                    {"e": "E17", "p": "P20", "text": "third"},
                ],
            },
            model_bundle={
                "inputs": [{"ref": "E17"}, {"ref": "E23"}, {"ref": "E26"}],
                "existingAtoms": [{"ref": "P1"}],
            },
        )

        self.assertEqual(normalized["attach"], [])
        self.assertEqual(normalized["create"][0]["e"], ["E26", "E23"])
        self.assertEqual(normalized["create"][1]["e"], ["E26", "E23"])
        self.assertEqual(normalized["create"][2]["e"], "E17")
        self.assertTrue(all(item["p"] == "" for item in normalized["create"]))
        self.assertEqual(
            [item["localRef"] for item in receipts],
            ["P20", "P22", "P23"],
        )

    def test_managed_luna_uses_the_atom_first_topic_book_pipeline(self) -> None:
        captured: list[dict[str, object]] = []

        class FakeExecutor:
            provider = "openai-codex"
            model_id = "gpt-5.6-luna"

            def complete(
                self,
                *,
                messages,
                max_tokens=None,
                phase="model-call",
                isolated=False,
            ):
                captured.append(
                    {
                        "messages": messages,
                        "maxTokens": max_tokens,
                        "phase": phase,
                        "isolated": isolated,
                    }
                )
                if phase == "atom-first-verifier":
                    packet = json.loads(messages[1]["content"])
                    return {
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "v": 1,
                                            "ok": 1,
                                            "coveredEvidenceRefs": ["E1"],
                                            "checkedActionCount": packet[
                                                "expectedActionCount"
                                            ],
                                            "decisionDigest": packet[
                                                "decisionDigest"
                                            ],
                                            "findings": [],
                                            "errors": [],
                                        }
                                    )
                                },
                            }
                        ]
                    }
                return {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": json.dumps(
                                    {
                                        "create": [
                                            {
                                                "e": "E1",
                                                "text": "Rime 候选不能被模型候选重排。",
                                                "kind": "project_constraint",
                                                "g": "new:input-method",
                                                "topicTitle": "输入法",
                                                "confidence": 0.98,
                                            },
                                            {
                                                "e": "E1",
                                                "text": "应用切换后必须让旧模型候选失效。",
                                                "kind": "project_requirement",
                                                "topicRefs": [
                                                    "new:input-method",
                                                    "new:context-stability",
                                                ],
                                                "topicTitle": "上下文稳定性",
                                                "confidence": 0.97,
                                            },
                                        ],
                                        "attach": [],
                                        "update": [],
                                        "supersede": [],
                                        "merge": [],
                                        "ignore": [],
                                        "tagMerges": [],
                                        "warnings": [],
                                    },
                                    ensure_ascii=False,
                                )
                            },
                        }
                    ]
                }

        organizer = ManagedPiMemoryOrganizer(FakeExecutor())
        result = organizer.curate_owner_memory(
            bundle={
                "project": "ime",
                "inputs": [
                    {
                        "sourceRef": "S1",
                        "sourceEventIds": [7],
                        "evidenceId": "evidence:input:7",
                        "evidenceIds": ["evidence:input:7"],
                    }
                ],
                "recentEvents": [
                    {
                        "eventId": 7,
                        "sourceEventIds": [7],
                        "sourceRef": "S1",
                        "createdAtMs": 1,
                        "text": (
                            "Rime 候选不能被模型候选重排；"
                            "应用切换后必须让旧模型候选失效。"
                        ),
                        "finalized": True,
                        "memoryEligible": True,
                    }
                ],
            },
            project="ime",
            owner_kind="user",
            owner_id="default",
        )

        self.assertEqual(organizer.curation_protocol_version, "atom-first-v1")
        self.assertEqual(result["curationArchitecture"], "atom-first-v1")
        self.assertEqual(len(result["memoryAtoms"]), 2)
        self.assertEqual(result["sourceDecisions"][0]["disposition"], "remember")
        self.assertEqual(
            result["sourceDecisions"][0]["evidenceId"],
            "evidence:input:7",
        )
        self.assertEqual(
            result["sourceDecisions"][0]["evidenceAdmissionState"],
            "admitted",
        )
        self.assertTrue(
            all(
                item["evidenceIds"] == ["evidence:input:7"]
                for item in result["memoryAtoms"]
            )
        )
        self.assertEqual(
            result["personalCurationV2"]["curationArchitecture"],
            "atom-first-v1",
        )
        self.assertEqual({item["title"] for item in result["topicBooks"]}, {
            "输入法",
            "上下文稳定性",
        })
        self.assertIn("唯一的 Evidence -> Atom -> Book", captured[0]["messages"][0]["content"])
        self.assertIn("输出前在同一轮静默逐项自检", captured[0]["messages"][0]["content"])
        self.assertIn("短片段”按语义自足性判断", captured[0]["messages"][0]["content"])
        self.assertIn("禁止拼接、投票或概括成长期 Atom", captured[0]["messages"][0]["content"])
        self.assertIn("独立变化测试", captured[0]["messages"][0]["content"])
        self.assertEqual(
            [item["phase"] for item in captured],
            ["atom-first-curation", "atom-first-verifier"],
        )
        self.assertFalse(captured[0]["isolated"])
        self.assertTrue(captured[1]["isolated"])
        self.assertTrue(result["modelDiagnostics"]["independentVerification"]["passed"])
        curation_packet = json.loads(captured[0]["messages"][1]["content"])
        verifier_packet = json.loads(captured[1]["messages"][1]["content"])
        for packet in (curation_packet, verifier_packet):
            semantic_input = packet["snapshot"]["inputs"][0]
            self.assertEqual(semantic_input["ref"], "E1")
            self.assertIn("Rime 候选", semantic_input["text"])
            self.assertNotIn("eventIds", semantic_input)
            self.assertNotIn("sourceRef", semantic_input)
            self.assertNotIn("contextGroupId", semantic_input)

    def test_all_memory_phases_allow_automatic_topic_aggregation(self) -> None:
        from rag_ime import deepseek_memory_organizer as prompts

        names = (
            "_memory_curation_system_prompt",
            "_memory_curation_recovery_prompt",
            "_memory_curation_semantic_repair_prompt",
            "_memory_curation_verifier_prompt",
            "_memory_catalog_consolidation_system_prompt",
            "_memory_catalog_consolidation_recovery_prompt",
            "_memory_catalog_consolidation_repair_prompt",
            "_memory_catalog_consolidation_verifier_prompt",
        )
        for name in names:
            with self.subTest(phase=name):
                prompt = getattr(prompts, name)()
                self.assertIn("自动聚合", prompt)
                self.assertIn("不同子问题", prompt)
                self.assertIn("Atom 独立", prompt)
                self.assertIn("来源", prompt)
                self.assertNotIn("同一问题/决策轴", prompt)
                self.assertNotIn("窄标题", prompt)

    def test_recovery_requests_keep_topic_aggregation_and_scope_rules(self) -> None:
        calls: list[dict[str, object]] = []

        def response(payload: dict[str, object]) -> dict[str, object]:
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(payload)},
                    }
                ]
            }

        def curation_payload() -> dict[str, object]:
            return {
                "create": [
                    {
                        "e": "E1",
                        "text": "用户偏好简洁回答",
                        "kind": "durable_preference",
                        "g": "new:answer-style",
                        "topicTitle": "回答风格",
                        "confidence": 0.9,
                    }
                ],
                "attach": [],
                "update": [],
                "supersede": [],
                "merge": [],
                "retract": [],
                "ignore": [],
                "tagMerges": [],
                "warnings": [],
            }

        class RecoveryExecutor:
            provider = "openai-codex"
            model_id = "gpt-5.6-luna"

            def complete(
                self,
                *,
                messages,
                max_tokens=None,
                phase="model-call",
                isolated=False,
            ):
                del max_tokens
                calls.append(
                    {
                        "messages": messages,
                        "phase": phase,
                        "isolated": isolated,
                    }
                )
                if phase in {
                    "atom-first-curation",
                    "memory-catalog-consolidation",
                }:
                    return {
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {"content": "not-json"},
                            }
                        ]
                    }
                if phase == "atom-first-repair":
                    return response(curation_payload())
                if phase == "memory-catalog-consolidation-repair":
                    return response(
                        {
                            "merge": [],
                            "tagMerges": [],
                            "bookMerges": [],
                            "warnings": [],
                        }
                    )
                if phase in {
                    "atom-first-verifier",
                    "memory-catalog-consolidation-verifier",
                }:
                    packet = json.loads(messages[1]["content"])
                    return response(
                        {
                            "v": 1,
                            "ok": 1,
                            "coveredEvidenceRefs": packet.get(
                                "expectedEvidenceRefs", []
                            ),
                            "checkedActionCount": packet["expectedActionCount"],
                            "decisionDigest": packet["decisionDigest"],
                            "findings": [],
                            "errors": [],
                        }
                    )
                raise AssertionError(f"unexpected phase: {phase}")

        organizer = ManagedPiMemoryOrganizer(RecoveryExecutor())
        organizer.compile_memory_curation(
            bundle={
                "schemaVersion": "rag-ime.memory-book-source-bundle.v1",
                "project": "ime",
                "recentEvents": [
                    {
                        "eventId": 1,
                        "sourceEventIds": [1],
                        "text": "用户偏好简洁回答",
                        "createdAtMs": 1,
                        "finalized": True,
                        "memoryEligible": True,
                    }
                ],
                "existingMemoryAtoms": [],
                "existingSemanticGroups": [],
                "existingSemanticTags": [],
                "existingMemoryBooks": [],
                "existingTagEdges": [],
            },
            project="ime",
        )
        organizer.compile_memory_curation(
            bundle={
                "schemaVersion": "rag-ime.memory-book-source-bundle.v1",
                "project": "ime",
                "curationScope": "global",
                "catalogAudit": True,
                "catalogOnly": True,
                "catalogComplete": True,
                "catalogTruncated": {},
                "recentEvents": [],
                "existingMemoryAtoms": [],
                "existingSemanticGroups": [],
                "existingSemanticTags": [],
                "existingMemoryBooks": [],
                "existingTagEdges": [],
            },
            project="ime",
        )

        recovery_calls = {
            str(call["phase"]): call
            for call in calls
            if str(call["phase"])
            in {
                "atom-first-repair",
                "memory-catalog-consolidation-repair",
            }
        }
        common_rules = (
            "自动聚合",
            "Atom 独立",
            "不同子问题",
            "自行选择主题粒度",
            "项目名",
            "App",
            "日期",
            "共现",
            "相似词",
        )
        for phase, call in recovery_calls.items():
            prompt = str(call["messages"][0]["content"])
            for rule in common_rules:
                self.assertIn(rule, prompt, phase)
        incremental_prompt = str(
            recovery_calls["atom-first-repair"]["messages"][0]["content"]
        )
        self.assertIn(
            "scope、project、binding 的空字符串是快照中的明确值",
            incremental_prompt,
        )
        self.assertIn("不能视为缺失", incremental_prompt)

        self.assertEqual(
            [str(call["phase"]) for call in calls],
            [
                "atom-first-curation",
                "atom-first-repair",
                "atom-first-verifier",
                "memory-catalog-consolidation",
                "memory-catalog-consolidation-repair",
                "memory-catalog-consolidation-verifier",
            ],
        )
        self.assertTrue(calls[2]["isolated"])
        self.assertTrue(calls[5]["isolated"])

    def test_verifier_contract_repair_corrects_digest_without_semantic_repair(self) -> None:
        phases: list[str] = []

        def response(payload: dict[str, object]) -> dict[str, object]:
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(payload)},
                    }
                ]
            }

        class DigestRepairExecutor:
            provider = "openai-codex"
            model_id = "gpt-5.6-luna"
            contract_repairs = 0

            def complete(
                self,
                *,
                messages,
                max_tokens=None,
                phase="model-call",
                isolated=False,
            ):
                del max_tokens, isolated
                phases.append(phase)
                if phase == "atom-first-curation":
                    return response(
                        {
                            "create": [
                                {
                                    "e": "E1",
                                    "text": "用户偏好简洁回答",
                                    "kind": "durable_preference",
                                    "g": "new:answer-style",
                                    "topicTitle": "回答风格",
                                    "confidence": 0.9,
                                }
                            ],
                            "attach": [],
                            "update": [],
                            "supersede": [],
                            "merge": [],
                            "retract": [],
                            "ignore": [],
                            "tagMerges": [],
                            "warnings": [],
                        }
                    )
                if phase == "atom-first-verifier":
                    packet = json.loads(messages[1]["content"])
                    digest = packet["decisionDigest"]
                    wrong_digest = digest[:-1] + ("0" if digest[-1] != "0" else "1")
                    return response(
                        {
                            "v": 1,
                            "ok": 1,
                            "coveredEvidenceRefs": ["E1"],
                            "checkedActionCount": packet["expectedActionCount"],
                            "decisionDigest": wrong_digest,
                            "findings": [],
                            "errors": [],
                        }
                    )
                if phase == "atom-first-verifier-contract-repair":
                    self.contract_repairs += 1
                    packet = json.loads(messages[1]["content"])
                    return response(
                        {
                            "v": 1,
                            "ok": 1,
                            "coveredEvidenceRefs": packet["expectedEvidenceRefs"],
                            "checkedActionCount": packet["expectedActionCount"],
                            "decisionDigest": packet["decisionDigest"],
                            "findings": [],
                            "errors": [],
                        }
                    )
                raise AssertionError(f"unexpected phase: {phase}")

        executor = DigestRepairExecutor()
        result = ManagedPiMemoryOrganizer(executor).compile_memory_curation(
            bundle={
                "schemaVersion": "rag-ime.memory-book-source-bundle.v1",
                "project": "ime",
                "recentEvents": [
                    {
                        "eventId": 1,
                        "sourceEventIds": [1],
                        "text": "用户偏好简洁回答",
                        "createdAtMs": 1,
                        "finalized": True,
                        "memoryEligible": True,
                    }
                ],
                "existingMemoryAtoms": [],
                "existingSemanticGroups": [],
                "existingSemanticTags": [],
                "existingMemoryBooks": [],
                "existingTagEdges": [],
            },
            project="ime",
        )
        self.assertTrue(result["independentlyVerified"])
        self.assertEqual(executor.contract_repairs, 1)
        self.assertEqual(
            phases,
            [
                "atom-first-curation",
                "atom-first-verifier",
                "atom-first-verifier-contract-repair",
            ],
        )
        self.assertTrue(
            result["modelDiagnostics"]["independentVerification"]["contractRepair"]["passed"]
        )

    def test_verifier_contract_repair_still_rejects_a_persistent_digest_error(self) -> None:
        phases: list[str] = []

        def response(payload: dict[str, object]) -> dict[str, object]:
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(payload)},
                    }
                ]
            }

        class PersistentDigestErrorExecutor:
            provider = "openai-codex"
            model_id = "gpt-5.6-luna"
            contract_repairs = 0

            def complete(
                self,
                *,
                messages,
                max_tokens=None,
                phase="model-call",
                isolated=False,
            ):
                del max_tokens, isolated
                phases.append(phase)
                if phase == "atom-first-curation":
                    return response(
                        {
                            "create": [
                                {
                                    "e": "E1",
                                    "text": "用户偏好简洁回答",
                                    "kind": "durable_preference",
                                    "g": "new:answer-style",
                                    "topicTitle": "回答风格",
                                    "confidence": 0.9,
                                }
                            ],
                            "attach": [],
                            "update": [],
                            "supersede": [],
                            "merge": [],
                            "retract": [],
                            "ignore": [],
                            "tagMerges": [],
                            "warnings": [],
                        }
                    )
                if phase in {
                    "atom-first-verifier",
                    "atom-first-verifier-contract-repair",
                }:
                    if phase == "atom-first-verifier-contract-repair":
                        self.contract_repairs += 1
                    packet = json.loads(messages[1]["content"])
                    digest = packet["decisionDigest"]
                    wrong_digest = digest[:-1] + ("0" if digest[-1] != "0" else "1")
                    return response(
                        {
                            "v": 1,
                            "ok": 1,
                            "coveredEvidenceRefs": packet.get(
                                "expectedEvidenceRefs", ["E1"]
                            ),
                            "checkedActionCount": packet["expectedActionCount"],
                            "decisionDigest": wrong_digest,
                            "findings": [],
                            "errors": [],
                        }
                    )
                raise AssertionError(f"unexpected phase: {phase}")

        executor = PersistentDigestErrorExecutor()
        with self.assertRaisesRegex(
            DeepSeekMemoryOrganizerError,
            "response-contract repair",
        ):
            ManagedPiMemoryOrganizer(executor).compile_memory_curation(
                bundle={
                    "schemaVersion": "rag-ime.memory-book-source-bundle.v1",
                    "project": "ime",
                    "recentEvents": [
                        {
                            "eventId": 1,
                            "sourceEventIds": [1],
                            "text": "用户偏好简洁回答",
                            "createdAtMs": 1,
                            "finalized": True,
                            "memoryEligible": True,
                        }
                    ],
                    "existingMemoryAtoms": [],
                    "existingSemanticGroups": [],
                    "existingSemanticTags": [],
                    "existingMemoryBooks": [],
                    "existingTagEdges": [],
                },
                project="ime",
            )
        self.assertEqual(executor.contract_repairs, 1)
        self.assertEqual(
            phases,
            [
                "atom-first-curation",
                "atom-first-verifier",
                "atom-first-verifier-contract-repair",
            ],
        )
        self.assertNotIn("atom-first-repair", phases)

    def test_unparsed_verifier_response_uses_semantic_repair_not_metadata_repair(self) -> None:
        phases: list[str] = []
        verifier_calls = 0

        def response(payload: dict[str, object]) -> dict[str, object]:
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(payload)},
                    }
                ]
            }

        class UnparsedVerifierExecutor:
            provider = "openai-codex"
            model_id = "gpt-5.6-luna"

            def complete(
                self,
                *,
                messages,
                max_tokens=None,
                phase="model-call",
                isolated=False,
            ):
                del max_tokens, isolated
                nonlocal verifier_calls
                phases.append(phase)
                if phase == "atom-first-curation":
                    return response(
                        {
                            "create": [
                                {
                                    "e": "E1",
                                    "text": "用户偏好简洁回答",
                                    "kind": "durable_preference",
                                    "g": "new:answer-style",
                                    "topicTitle": "回答风格",
                                    "confidence": 0.9,
                                }
                            ],
                            "attach": [],
                            "update": [],
                            "supersede": [],
                            "merge": [],
                            "retract": [],
                            "ignore": [],
                            "tagMerges": [],
                            "warnings": [],
                        }
                    )
                if phase == "atom-first-verifier":
                    verifier_calls += 1
                    if verifier_calls == 1:
                        return {
                            "choices": [
                                {
                                    "finish_reason": "stop",
                                    "message": {"content": "not-json"},
                                }
                            ]
                        }
                    packet = json.loads(messages[1]["content"])
                    return response(
                        {
                            "v": 1,
                            "ok": 1,
                            "coveredEvidenceRefs": ["E1"],
                            "checkedActionCount": packet["expectedActionCount"],
                            "decisionDigest": packet["decisionDigest"],
                            "findings": [],
                            "errors": [],
                        }
                    )
                if phase == "atom-first-repair":
                    return response(
                        {
                            "create": [
                                {
                                    "e": "E1",
                                    "text": "用户偏好简洁回答",
                                    "kind": "durable_preference",
                                    "g": "new:answer-style",
                                    "topicTitle": "回答风格",
                                    "confidence": 0.9,
                                }
                            ],
                            "attach": [],
                            "update": [],
                            "supersede": [],
                            "merge": [],
                            "retract": [],
                            "ignore": [],
                            "tagMerges": [],
                            "warnings": [],
                        }
                    )
                raise AssertionError(f"unexpected phase: {phase}")

        executor = UnparsedVerifierExecutor()
        with self.assertRaisesRegex(
            DeepSeekMemoryOrganizerError,
            "independent memory curation verifier rejected",
        ):
            ManagedPiMemoryOrganizer(
                executor,
                max_semantic_repair_rounds=1,
            ).compile_memory_curation(
                bundle={
                    "schemaVersion": "rag-ime.memory-book-source-bundle.v1",
                    "project": "ime",
                    "recentEvents": [
                        {
                            "eventId": 1,
                            "sourceEventIds": [1],
                            "text": "用户偏好简洁回答",
                            "createdAtMs": 1,
                            "finalized": True,
                            "memoryEligible": True,
                        }
                    ],
                    "existingMemoryAtoms": [],
                    "existingSemanticGroups": [],
                    "existingSemanticTags": [],
                    "existingMemoryBooks": [],
                    "existingTagEdges": [],
                },
                project="ime",
            )
        self.assertEqual(
            phases,
            [
                "atom-first-curation",
                "atom-first-verifier",
                "atom-first-repair",
            ],
        )
        self.assertNotIn("atom-first-verifier-contract-repair", phases)

    def test_managed_luna_repairs_one_semantic_verifier_rejection(self) -> None:
        phases: list[str] = []
        verifier_calls = 0

        class FakeExecutor:
            provider = "openai-codex"
            model_id = "gpt-5.6-luna"

            def complete(
                inner_self,
                *,
                messages,
                max_tokens=None,
                phase="model-call",
                isolated=False,
            ):
                del inner_self, max_tokens, isolated
                nonlocal verifier_calls
                phases.append(phase)
                if phase == "atom-first-verifier":
                    verifier_calls += 1
                    packet = json.loads(messages[1]["content"])
                    return {
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "v": 1,
                                            "ok": 0 if verifier_calls == 1 else 1,
                                            "coveredEvidenceRefs": ["E1"],
                                            "checkedActionCount": packet[
                                                "expectedActionCount"
                                            ],
                                            "decisionDigest": packet[
                                                "decisionDigest"
                                            ],
                                            "findings": (
                                                [
                                                    {
                                                        "code": "compound_atom",
                                                        "actionType": "create",
                                                        "actionIndex": 0,
                                                        "evidenceRefs": ["E1"],
                                                    }
                                                ]
                                                if verifier_calls == 1
                                                else []
                                            ),
                                            "errors": (
                                                ["compound_atom"]
                                                if verifier_calls == 1
                                                else []
                                            ),
                                        }
                                    )
                                }
                            }
                        ]
                    }
                if phase == "atom-first-repair":
                    packet = json.loads(messages[1]["content"])
                    self.assertEqual(packet["verifierErrors"], ["compound_atom"])
                    return {
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "attach": [],
                                            "create": [
                                                {
                                                    "e": "E1",
                                                    "text": "Rime 保留原生候选排序。",
                                                    "kind": "project_constraint",
                                                    "g": "new:input-method",
                                                    "topicTitle": "输入法",
                                                    "confidence": 0.98,
                                                },
                                                {
                                                    "e": "E1",
                                                    "text": "应用切换使旧模型候选失效。",
                                                    "kind": "project_requirement",
                                                    "g": "new:context-stability",
                                                    "topicTitle": "上下文稳定性",
                                                    "confidence": 0.97,
                                                },
                                            ],
                                            "update": [],
                                            "supersede": [],
                                            "merge": [],
                                            "retract": [],
                                            "ignore": [],
                                            "tagMerges": [],
                                            "warnings": [],
                                        }
                                    )
                                }
                            }
                        ]
                    }
                return {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "attach": [],
                                        "create": [
                                            {
                                                "e": "E1",
                                                "text": (
                                                    "Rime 保留原生候选排序，并且应用切换"
                                                    "使旧模型候选失效。"
                                                ),
                                                "kind": "project_constraint",
                                                "g": "new:input-method",
                                                "topicTitle": "输入法",
                                                "confidence": 0.98,
                                            }
                                        ],
                                        "update": [],
                                        "supersede": [],
                                        "merge": [],
                                        "retract": [],
                                        "ignore": [],
                                        "tagMerges": [],
                                        "warnings": [],
                                    }
                                )
                            }
                        }
                    ]
                }

        result = ManagedPiMemoryOrganizer(
            FakeExecutor(),
            max_semantic_repair_rounds=5,
        ).curate_owner_memory(
            bundle={
                "project": "ime",
                "inputs": [
                    {
                        "sourceRef": "S1",
                        "sourceEventIds": [7],
                        "evidenceId": "evidence:input:7",
                        "evidenceIds": ["evidence:input:7"],
                    }
                ],
                "recentEvents": [
                    {
                        "eventId": 7,
                        "sourceEventIds": [7],
                        "sourceRef": "S1",
                        "createdAtMs": 1,
                        "text": (
                            "Rime 保留原生候选排序；应用切换后必须让旧模型候选失效。"
                        ),
                        "finalized": True,
                        "memoryEligible": True,
                    }
                ],
            },
            project="ime",
            owner_kind="user",
            owner_id="default",
        )

        self.assertEqual(
            phases,
            [
                "atom-first-curation",
                "atom-first-verifier",
                "atom-first-repair",
                "atom-first-verifier",
            ],
        )
        self.assertNotIn("atom-first-verifier-contract-repair", phases)
        self.assertEqual(len(result["memoryAtoms"]), 2)
        self.assertTrue(
            result["modelDiagnostics"]["semanticRepair"]["passed"]
        )
        self.assertEqual(
            result["modelDiagnostics"]["semanticRepair"]["maxAttempts"],
            5,
        )

    def test_candidate_migration_can_defer_verifier_until_final_catalog_audit(self) -> None:
        phases: list[tuple[str, bool]] = []

        class FakeExecutor:
            provider = "openai-codex"
            model_id = "gpt-5.6-luna"

            def complete(
                self,
                *,
                messages,
                max_tokens=None,
                phase="model-call",
                isolated=False,
            ):
                phases.append((phase, isolated))
                return {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": json.dumps(
                                    {
                                        "attach": [["E1", "P1"]],
                                        "create": [],
                                        "update": [],
                                        "supersede": [],
                                        "merge": [],
                                        "retract": [],
                                        "ignore": ["E2"],
                                        "tagMerges": [],
                                        "warnings": [],
                                    }
                                )
                            },
                        }
                    ]
                }

        result = ManagedPiMemoryOrganizer(
            FakeExecutor(),
            require_independent_verification=False,
        ).compile_memory_curation(
            bundle={
                "project": "ime",
                "recentEvents": [
                    {
                        "eventId": 101,
                        "sourceEventIds": [101],
                        "createdAtMs": 1,
                        "text": "输入法碎片不能直接注入 Agent 上下文。",
                        "finalized": True,
                        "memoryEligible": True,
                    },
                    {
                        "eventId": 102,
                        "sourceEventIds": [102],
                        "createdAtMs": 2,
                        "text": "这是一条不需要长期保存的临时测试。",
                        "finalized": True,
                        "memoryEligible": True,
                    },
                ],
                "existingMemoryAtoms": [
                    {
                        "atomId": "atom:context-boundary",
                        "canonicalText": "禁止把输入法碎片注入普通 Agent 上下文。",
                        "kind": "project_constraint",
                        "sourceEventIds": [7],
                    }
                ],
            },
            project="ime",
        )

        self.assertEqual(phases, [("atom-first-curation", False)])
        self.assertFalse(result["independentlyVerified"])
        self.assertTrue(result["verificationDeferred"])
        self.assertTrue(
            result["modelDiagnostics"]["independentVerification"]["deferred"]
        )

    def test_managed_luna_repairs_new_findings_exposed_by_reverification(self) -> None:
        phases: list[str] = []
        verifier_calls = 0
        repair_calls = 0

        class FakeExecutor:
            provider = "openai-codex"
            model_id = "gpt-5.6-luna"

            def complete(
                inner_self,
                *,
                messages,
                max_tokens=None,
                phase="model-call",
                isolated=False,
            ):
                del inner_self, max_tokens, isolated
                nonlocal verifier_calls, repair_calls
                phases.append(phase)
                if phase == "atom-first-verifier":
                    verifier_calls += 1
                    packet = json.loads(messages[1]["content"])
                    findings_by_call = {
                        1: [
                            {
                                "code": "compound_atom",
                                "actionType": "create",
                                "actionIndex": 0,
                                "evidenceRefs": ["E1"],
                            }
                        ],
                        2: [
                            {
                                "code": "inferred_requirement",
                                "actionType": "create",
                                "actionIndex": 1,
                                "evidenceRefs": ["E2"],
                            }
                        ],
                        3: [
                            {
                                "code": "durable_evidence_ignored",
                                "actionType": "ignore",
                                "actionIndex": 0,
                                "evidenceRefs": ["E2"],
                            }
                        ],
                    }
                    findings = findings_by_call.get(verifier_calls, [])
                    return {
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "v": 1,
                                            "ok": 0 if findings else 1,
                                            "coveredEvidenceRefs": ["E1", "E2"],
                                            "checkedActionCount": packet[
                                                "expectedActionCount"
                                            ],
                                            "decisionDigest": packet[
                                                "decisionDigest"
                                            ],
                                            "findings": findings,
                                            "errors": [
                                                item["code"] for item in findings
                                            ],
                                        }
                                    )
                                }
                            }
                        ]
                    }
                if phase == "atom-first-repair":
                    repair_calls += 1
                    packet = json.loads(messages[1]["content"])
                    expected_error = {
                        1: "compound_atom",
                        2: "inferred_requirement",
                        3: "durable_evidence_ignored",
                    }[repair_calls]
                    self.assertEqual(packet["verifierErrors"], [expected_error])
                    return {
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "attach": [],
                                            "create": [
                                                {
                                                    "e": "E1",
                                                    "text": "Rime 保留原生候选排序。",
                                                    "kind": "project_constraint",
                                                    "g": "new:input-method",
                                                    "topicTitle": "输入法",
                                                    "confidence": 0.98,
                                                },
                                                *(
                                                    [
                                                        {
                                                            "e": "E2",
                                                            "text": (
                                                                "项目必须采用某种界面。"
                                                                if repair_calls == 1
                                                                else "项目界面保持简洁。"
                                                            ),
                                                            "kind": "project_requirement",
                                                            "g": "new:interface",
                                                            "topicTitle": "界面",
                                                            "confidence": 0.8,
                                                        }
                                                    ]
                                                    if repair_calls != 2
                                                    else []
                                                ),
                                            ],
                                            "update": [],
                                            "supersede": [],
                                            "merge": [],
                                            "retract": [],
                                            "ignore": (
                                                ["E2"] if repair_calls == 2 else []
                                            ),
                                            "tagMerges": [],
                                            "warnings": [],
                                        }
                                    )
                                }
                            }
                        ]
                    }
                return {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "attach": [],
                                        "create": [
                                            {
                                                "e": "E1",
                                                "text": (
                                                    "Rime 保留原生候选排序，同时应用切换"
                                                    "后让旧候选失效。"
                                                ),
                                                "kind": "project_constraint",
                                                "g": "new:input-method",
                                                "topicTitle": "输入法",
                                                "confidence": 0.98,
                                            },
                                            {
                                                "e": "E2",
                                                "text": "项目必须采用某种界面。",
                                                "kind": "project_requirement",
                                                "g": "new:interface",
                                                "topicTitle": "界面",
                                                "confidence": 0.8,
                                            },
                                        ],
                                        "update": [],
                                        "supersede": [],
                                        "merge": [],
                                        "retract": [],
                                        "ignore": [],
                                        "tagMerges": [],
                                        "warnings": [],
                                    }
                                )
                            }
                        }
                    ]
                }

        result = ManagedPiMemoryOrganizer(FakeExecutor()).curate_owner_memory(
            bundle={
                "project": "ime",
                "inputs": [
                    {
                        "sourceRef": "S1",
                        "sourceEventIds": [7],
                        "evidenceId": "evidence:input:7",
                        "evidenceIds": ["evidence:input:7"],
                    },
                    {
                        "sourceRef": "S2",
                        "sourceEventIds": [8],
                        "evidenceId": "evidence:input:8",
                        "evidenceIds": ["evidence:input:8"],
                    },
                ],
                "recentEvents": [
                    {
                        "eventId": 7,
                        "sourceEventIds": [7],
                        "sourceRef": "S1",
                        "createdAtMs": 1,
                        "text": (
                            "Rime 保留原生候选排序；应用切换后必须让旧模型候选失效。"
                        ),
                        "finalized": True,
                        "memoryEligible": True,
                    },
                    {
                        "eventId": 8,
                        "sourceEventIds": [8],
                        "sourceRef": "S2",
                        "createdAtMs": 2,
                        "text": "项目界面保持简洁。",
                        "finalized": True,
                        "memoryEligible": True,
                    },
                ],
            },
            project="ime",
            owner_kind="user",
            owner_id="default",
        )

        self.assertEqual(
            phases,
            [
                "atom-first-curation",
                "atom-first-verifier",
                "atom-first-repair",
                "atom-first-verifier",
                "atom-first-repair",
                "atom-first-verifier",
                "atom-first-repair",
                "atom-first-verifier",
            ],
        )
        self.assertEqual(len(result["memoryAtoms"]), 2)
        self.assertEqual(
            result["sourceDecisions"][1]["disposition"],
            "remember",
        )
        semantic_repair = result["modelDiagnostics"]["semanticRepair"]
        self.assertEqual(semantic_repair["attemptCount"], 3)
        self.assertEqual(semantic_repair["maxAttempts"], 3)
        self.assertTrue(semantic_repair["passed"])

    def test_managed_luna_retries_a_repair_that_changes_unflagged_actions(self) -> None:
        phases: list[str] = []
        repair_calls = 0

        class FakeExecutor:
            provider = "openai-codex"
            model_id = "gpt-5.6-luna"

            def complete(
                inner_self,
                *,
                messages,
                max_tokens=None,
                phase="model-call",
                isolated=False,
            ):
                del inner_self, max_tokens, isolated
                nonlocal repair_calls
                phases.append(phase)
                if phase == "atom-first-verifier":
                    packet = json.loads(messages[1]["content"])
                    return {
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "v": 1,
                                            "ok": 0 if repair_calls == 0 else 1,
                                            "coveredEvidenceRefs": ["E1", "E2"],
                                            "checkedActionCount": packet[
                                                "expectedActionCount"
                                            ],
                                            "decisionDigest": packet[
                                                "decisionDigest"
                                            ],
                                            "findings": (
                                                [
                                                    {
                                                        "code": "compound_atom",
                                                        "actionType": "create",
                                                        "actionIndex": 0,
                                                        "evidenceRefs": ["E1"],
                                                    }
                                                ]
                                                if repair_calls == 0
                                                else []
                                            ),
                                            "errors": (
                                                ["compound_atom"]
                                                if repair_calls == 0
                                                else []
                                            ),
                                        }
                                    )
                                }
                            }
                        ]
                    }
                if phase == "atom-first-repair":
                    repair_calls += 1
                    packet = json.loads(messages[1]["content"])
                    if repair_calls == 1:
                        self.assertNotIn("repairRetryFeedback", packet)
                    else:
                        self.assertEqual(
                            packet["repairRetryFeedback"]["reason"],
                            "changed_unflagged_actions",
                        )
                    return {
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "attach": [],
                                            "create": [
                                                {
                                                    "e": "E1",
                                                    "text": "Rime 保留原生候选排序。",
                                                    "kind": "project_constraint",
                                                    "g": "new:input-method",
                                                    "topicTitle": "输入法",
                                                    "confidence": 0.98,
                                                },
                                                *(
                                                    [
                                                        {
                                                            "e": "E2",
                                                            "text": "临时工作内容。",
                                                            "kind": "project_fact",
                                                            "g": "new:temporary",
                                                            "topicTitle": "临时",
                                                            "confidence": 0.7,
                                                        }
                                                    ]
                                                    if repair_calls == 1
                                                    else []
                                                ),
                                            ],
                                            "update": [],
                                            "supersede": [],
                                            "merge": [],
                                            "retract": [],
                                            "ignore": (
                                                [] if repair_calls == 1 else ["E2"]
                                            ),
                                            "tagMerges": [],
                                            "warnings": [],
                                        }
                                    )
                                }
                            }
                        ]
                    }
                return {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "attach": [],
                                        "create": [
                                            {
                                                "e": "E1",
                                                "text": (
                                                    "Rime 保留原生候选排序，并保持候选"
                                                    "上下文一致。"
                                                ),
                                                "kind": "project_constraint",
                                                "g": "new:input-method",
                                                "topicTitle": "输入法",
                                                "confidence": 0.98,
                                            }
                                        ],
                                        "update": [],
                                        "supersede": [],
                                        "merge": [],
                                        "retract": [],
                                        "ignore": ["E2"],
                                        "tagMerges": [],
                                        "warnings": [],
                                    }
                                )
                            }
                        }
                    ]
                }

        result = ManagedPiMemoryOrganizer(FakeExecutor()).curate_owner_memory(
            bundle={
                "project": "ime",
                "inputs": [
                    {
                        "sourceRef": "S1",
                        "sourceEventIds": [7],
                        "evidenceId": "evidence:input:7",
                        "evidenceIds": ["evidence:input:7"],
                    },
                    {
                        "sourceRef": "S2",
                        "sourceEventIds": [8],
                        "evidenceId": "evidence:input:8",
                        "evidenceIds": ["evidence:input:8"],
                    },
                ],
                "recentEvents": [
                    {
                        "eventId": 7,
                        "sourceEventIds": [7],
                        "sourceRef": "S1",
                        "createdAtMs": 1,
                        "text": "Rime 保留原生候选排序，并保持候选上下文一致。",
                        "finalized": True,
                        "memoryEligible": True,
                    },
                    {
                        "eventId": 8,
                        "sourceEventIds": [8],
                        "sourceRef": "S2",
                        "createdAtMs": 2,
                        "text": "这只是一次临时工作内容。",
                        "finalized": True,
                        "memoryEligible": True,
                    },
                ],
            },
            project="ime",
            owner_kind="user",
            owner_id="default",
        )

        self.assertEqual(
            phases,
            [
                "atom-first-curation",
                "atom-first-verifier",
                "atom-first-repair",
                "atom-first-repair",
                "atom-first-verifier",
            ],
        )
        semantic_repair = result["modelDiagnostics"]["semanticRepair"]
        self.assertEqual(semantic_repair["attemptCount"], 2)
        self.assertFalse(semantic_repair["attempts"][0]["reverified"])
        self.assertTrue(semantic_repair["attempts"][1]["reverified"])
        self.assertTrue(semantic_repair["passed"])

    def test_role_book_curation_is_review_only_and_preserves_allowed_ids(self) -> None:
        config = load_deepseek_config(
            env={
                "DEEPSEEK_API_KEY": "secret",
                "RAG_IME_DEEPSEEK_BASE_URL": "https://api.deepseek.com",
                "RAG_IME_DEEPSEEK_MODEL": "deepseek-v4-flash",
            }
        )
        captured: dict[str, object] = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                del exc_type, exc, tb
                return False

            def read(self):
                return json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "traitProposals": [],
                                            "capabilityProposals": [],
                                            "lessonProposals": [],
                                            "commitmentProposals": [],
                                            "warnings": [],
                                        },
                                        ensure_ascii=False,
                                    )
                                }
                            }
                        ]
                    }
                ).encode("utf-8")

        def fake_urlopen(request, timeout):
            del timeout
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse()

        bundle = {
            "schemaVersion": "rag-ime.role-book-curation-input.v2",
            "curationEvidence": [
                {
                    "evidenceId": "evidence:digest:1",
                    "sourceKind": "session_digest",
                    "text": "已完成并验证角色书维护边界",
                }
            ],
            "activityContext": {
                "corroborationOnly": True,
                "maySupportRoleProposals": False,
            },
            "policy": {
                "allowedEvidenceIds": ["evidence:digest:1"],
                "rawConversationMaySupplyEvidence": False,
                "autoActivation": False,
            },
        }
        result = DeepSeekMemoryOrganizer(
            config,
            urlopen=fake_urlopen,
        ).curate_role_book(
            bundle=bundle,
            project="rag-ime",
            role_id="architect",
            role_version="role-v1",
        )

        self.assertEqual(result["schemaVersion"], "rag-ime.role-book-curation.v1")
        self.assertEqual(result["provider"], "deepseek")
        request_payload = captured["payload"]
        system_prompt = request_payload["messages"][0]["content"]
        self.assertIn("review-only", system_prompt)
        self.assertIn("原始 user/assistant 消息", system_prompt)
        self.assertIn("不能单独", system_prompt)
        self.assertIn("数分钟内", system_prompt)
        self.assertIn("ephemeral activity", system_prompt)
        self.assertIn("durable consolidation", system_prompt)
        self.assertIn("sourceEvidenceIds 必须只列出实际支持", system_prompt)
        self.assertIn("必须 abstain", system_prompt)
        self.assertIn("activeRoleBook 中已有语义等价项时不得重复提案", system_prompt)
        model_input = json.loads(request_payload["messages"][1]["content"])
        self.assertEqual(
            model_input["bundle"]["policy"]["allowedEvidenceIds"],
            ["evidence:digest:1"],
        )
        self.assertFalse(model_input["bundle"]["policy"]["autoActivation"])

    def test_managed_luna_routes_role_book_curation_through_its_bounded_phase(self) -> None:
        captured: list[dict[str, object]] = []

        class FakeExecutor:
            provider = "openai-codex"
            model_id = "gpt-5.6-luna"

            def complete(
                self,
                *,
                messages,
                max_tokens=None,
                phase="model-call",
                isolated=False,
            ):
                captured.append(
                    {
                        "messages": messages,
                        "maxTokens": max_tokens,
                        "phase": phase,
                        "isolated": isolated,
                    }
                )
                return {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "traitProposals": [],
                                        "capabilityProposals": [],
                                        "lessonProposals": [],
                                        "commitmentProposals": [],
                                        "warnings": ["insufficient durable evidence"],
                                    }
                                )
                            }
                        }
                    ]
                }

        organizer = ManagedPiMemoryOrganizer(FakeExecutor())
        result = organizer.curate_role_book(
            bundle={
                "curationEvidence": [
                    {
                        "evidenceId": "evidence:digest:1",
                        "sourceKind": "session_digest",
                        "text": "一次短期工作摘要",
                    }
                ],
                "policy": {
                    "allowedEvidenceIds": ["evidence:digest:1"],
                    "autoActivation": False,
                },
            },
            project="rag-ime",
            role_id="architect",
            role_version="role-v1",
        )

        self.assertEqual(result["schemaVersion"], "rag-ime.role-book-curation.v1")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["phase"], "role-book-curation")
        self.assertFalse(captured[0]["isolated"])

    def test_owner_model_bundle_keeps_bounded_context_corroboration_only(self) -> None:
        projected = _owner_memory_model_bundle(
            {
                "inputs": [
                    {
                        "sourceRef": "S1",
                        "sourceKind": "user_final",
                        "trustClass": "user_claim",
                        "createdAtMs": 1,
                        "sourceEventIds": [11],
                        "text": "以后默认先给结论。",
                        "recentContext": "前一轮正在讨论输入法历史记录",
                        "app": "com.openai.codex",
                        "contextGroupId": "app:codex",
                        "captureHints": [
                            {
                                "kind": "preference",
                                "claim": "用户偏好先看结论。",
                                "scope": "user",
                                "basis": "explicit_user_statement",
                                "futureUse": "未来回答默认先给结论。",
                                "authoritative": False,
                            }
                        ],
                    }
                ],
                "activityContext": {
                    "available": True,
                    "summary": "当天主要在 TextEdit 实现时间线",
                    "segments": [{"summary": "实现时间线联合上下文"}],
                },
                "agentConversationContext": {
                    "available": True,
                    "messages": [
                        {
                            "role": "assistant",
                            "text": "已完成有界上下文审计",
                            "occurredAtMs": 4,
                        }
                    ],
                },
            }
        )

        self.assertTrue(projected["activityContext"]["available"])
        self.assertEqual(
            projected["activityContext"]["summary"],
            "当天主要在 TextEdit 实现时间线",
        )
        self.assertEqual(
            projected["activityContext"]["segments"][0]["summary"],
            "实现时间线联合上下文",
        )
        self.assertTrue(projected["agentConversationContext"]["available"])
        self.assertEqual(
            projected["agentConversationContext"]["messages"][0]["text"],
            "已完成有界上下文审计",
        )
        context_json = json.dumps(
            {
                "activity": projected["activityContext"],
                "conversation": projected["agentConversationContext"],
            },
            ensure_ascii=False,
        )
        self.assertNotIn("sourceEventIds", context_json)
        self.assertNotIn("evidenceId", context_json)
        self.assertTrue(projected["activityContext"]["corroborationOnly"])
        self.assertFalse(projected["activityContext"]["maySupportFacts"])
        self.assertTrue(projected["agentConversationContext"]["corroborationOnly"])
        self.assertFalse(projected["agentConversationContext"]["maySupportFacts"])
        self.assertNotIn("localContext", projected["inputs"][0])
        self.assertNotIn("app", projected["inputs"][0])
        self.assertEqual(
            projected["inputs"][0]["captureHints"][0]["claim"],
            "用户偏好先看结论。",
        )

    def test_owner_bundle_samples_long_fragment_provenance_across_full_range(self) -> None:
        projected = _owner_memory_model_bundle(
            {
                "inputs": [
                    {
                        "sourceRef": "S1",
                        "sourceKind": "user_final",
                        "trustClass": "user_claim",
                        "createdAtMs": 1,
                        "sourceEventIds": list(range(1, 5_001)),
                        "text": "重建后的完整输入",
                    }
                ]
            }
        )

        source_ids = projected["inputs"][0]["sourceEventIds"]
        self.assertEqual(len(source_ids), 64)
        self.assertEqual(source_ids[0], 1)
        self.assertEqual(source_ids[-1], 5_000)
        self.assertTrue(all(left < right for left, right in zip(source_ids, source_ids[1:])))

    def test_external_owner_bundle_drops_unrelated_daily_context_and_caps_atoms(
        self,
    ) -> None:
        projected = _owner_memory_model_bundle(
            {
                "inputs": [
                    {
                        "sourceRef": "S1",
                        "sourceKind": "session_digest",
                        "trustClass": "session_summary",
                        "externalProvider": "codex",
                        "createdAtMs": 2,
                        "sourceOccurredAtMs": 1,
                        "sourceEventIds": [11],
                        "text": "Codex 已整理的 Session 摘要",
                    }
                ],
                "activityContext": {
                    "available": True,
                    "summary": "今天正在处理无关的前台窗口。",
                    "segments": [
                        {
                            "segmentId": "segment:1",
                            "app": "TextEdit",
                            "summary": "无关活动",
                        }
                    ],
                },
                "agentConversationContext": {
                    "available": True,
                    "messages": [
                        {
                            "role": "assistant",
                            "text": "无关对话尾窗",
                        }
                    ],
                },
                "existingMemoryAtoms": [
                    {
                        "atomId": f"atom:{index}",
                        "canonicalText": f"已有事实 {index}",
                    }
                    for index in range(40)
                ],
            }
        )

        self.assertFalse(projected["activityContext"]["available"])
        self.assertFalse(projected["agentConversationContext"]["available"])
        self.assertEqual(len(projected["existingMemoryAtoms"]), 20)

    def test_deepseek_config_reads_dedicated_env_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "deepseek.env"
            env_path.write_text(
                "\n".join(
                    [
                        "DEEPSEEK_API_KEY=secret",
                        "RAG_IME_DEEPSEEK_BASE_URL=https://api.deepseek.com",
                        "RAG_IME_DEEPSEEK_MODEL=deepseek-v4-flash",
                        "RAG_IME_DEEPSEEK_JSON=1",
                        "RAG_IME_DEEPSEEK_ACTIVE_RAG_MAX_TOKENS=1280",
                        "RAG_IME_DEEPSEEK_MEMORY_BOOK_MAX_TOKENS=1536",
                    ]
                ),
                encoding="utf-8",
            )

            config = load_deepseek_config(env_path)

        self.assertEqual(config.api_base_url, "https://api.deepseek.com/v1")
        self.assertEqual(config.api_key, "secret")
        self.assertEqual(config.model, "deepseek-v4-flash")
        self.assertTrue(config.json_mode)
        self.assertEqual(config.thinking, "disabled")
        self.assertEqual(config.active_rag_max_tokens, 1280)
        self.assertEqual(config.memory_book_max_tokens, 1536)

    def test_deepseek_config_reads_process_environment_by_default(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "RAG_IME_DEEPSEEK_API_KEY": "env-secret",
                "RAG_IME_DEEPSEEK_BASE_URL": "https://api.kukuit.com",
                "RAG_IME_DEEPSEEK_MODEL": "deepseek-v4-flash",
            },
            clear=False,
        ):
            config = load_deepseek_config()

        self.assertEqual(config.api_base_url, "https://api.kukuit.com/v1")
        self.assertEqual(config.api_key, "env-secret")
        self.assertEqual(config.model, "deepseek-v4-flash")

    def test_deepseek_config_rejects_non_v4_model_for_high_intelligence_routes(self) -> None:
        with self.assertRaisesRegex(ValueError, "DeepSeek V4"):
            load_deepseek_config(
                env={
                    "DEEPSEEK_API_KEY": "secret",
                    "RAG_IME_DEEPSEEK_MODEL": "deepseek-chat",
                }
            )

    def test_deepseek_config_reads_default_env_file_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "deepseek.env"
            env_path.write_text(
                "\n".join(
                    [
                        "DEEPSEEK_API_KEY=file-secret",
                        "RAG_IME_DEEPSEEK_BASE_URL=https://api.kukuit.com",
                        "RAG_IME_DEEPSEEK_MODEL=deepseek-v4-flash",
                    ]
                ),
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"RAG_IME_DEEPSEEK_ENV": str(env_path)}, clear=False):
                config = load_deepseek_config()

        self.assertEqual(config.api_base_url, "https://api.kukuit.com/v1")
        self.assertEqual(config.api_key, "file-secret")
        self.assertEqual(config.env_path, env_path)

    def test_memory_organizer_builds_json_chat_completion_request(self) -> None:
        config = load_deepseek_config(
            env={
                "DEEPSEEK_API_KEY": "secret",
                "RAG_IME_DEEPSEEK_BASE_URL": "https://api.deepseek.com",
                "RAG_IME_DEEPSEEK_MODEL": "deepseek-v4-flash",
            }
        )
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                content = {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "schemaVersion": "rag-ime.memory-book-compile.v1",
                                        "dailyBooks": [],
                                        "topicBooks": [],
                                        "memoryAtoms": [],
                                        "tagEdges": [],
                                        "phraseCandidates": [],
                                        "warnings": [],
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                }
                return json.dumps(content).encode("utf-8")

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["authorization"] = request.get_header("Authorization")
            captured["userAgent"] = request.get_header("User-agent")
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse()

        organizer = DeepSeekMemoryOrganizer(config, urlopen=fake_urlopen)
        payload = organizer.compile_memory_book(
            bundle={"recentEvents": [{"eventId": 1, "text": "RAG 输入法"}]},
            project="wisdom-weasel-rag-ime",
            instruction="合并输入法分组，修正语音错字，标签不要太碎",
        )

        self.assertEqual(payload["schemaVersion"], "rag-ime.memory-book-compile.v1")
        self.assertEqual(payload["topicBooks"], [])
        self.assertEqual(payload["semanticGroups"], [])
        self.assertEqual(payload["semanticTags"], [])
        self.assertEqual(payload["provider"], "deepseek")
        self.assertEqual(payload["model"], "deepseek-v4-flash")
        self.assertEqual(payload["instruction"], "合并输入法分组，修正语音错字，标签不要太碎")
        self.assertEqual(captured["url"], "https://api.deepseek.com/v1/chat/completions")
        self.assertEqual(captured["authorization"], "Bearer secret")
        self.assertEqual(captured["userAgent"], "rag-ime/1.0 curl-compatible")
        self.assertEqual(captured["payload"]["response_format"], {"type": "json_object"})
        self.assertEqual(captured["payload"]["thinking"], {"type": "enabled"})
        self.assertEqual(captured["payload"]["reasoning_effort"], "low")
        self.assertEqual(captured["payload"]["max_tokens"], 3072)
        self.assertFalse(captured["payload"]["stream"])
        system_prompt = captured["payload"]["messages"][0]["content"]
        self.assertIn("topicBooks", system_prompt)
        self.assertIn('bookType="topic"', system_prompt)
        self.assertIn("更新摘要而不是按日期新建重复主题", system_prompt)
        self.assertIn("semanticGroups", system_prompt)
        self.assertIn("禁止照抄成语义标签", system_prompt)
        self.assertIn("rimeRankFeedback", system_prompt)
        self.assertIn("合并输入法分组，修正语音错字，标签不要太碎", captured["payload"]["messages"][1]["content"])

    def test_memory_organizer_uses_project_default_when_instruction_is_empty(self) -> None:
        config = load_deepseek_config(
            env={
                "DEEPSEEK_API_KEY": "secret",
                "RAG_IME_DEEPSEEK_BASE_URL": "https://api.deepseek.com",
                "RAG_IME_DEEPSEEK_MODEL": "deepseek-v4-flash",
            }
        )
        captured: dict[str, object] = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                body = {"choices": [{"message": {"content": '{"memoryAtoms": []}'}}]}
                return json.dumps(body).encode("utf-8")

        def fake_urlopen(request, timeout):
            request_payload = json.loads(request.data.decode("utf-8"))
            captured.setdefault("payloads", []).append(request_payload)
            captured.setdefault("payload", request_payload)
            return FakeResponse()

        payload = DeepSeekMemoryOrganizer(config, urlopen=fake_urlopen).compile_memory_book(
            bundle={"recentEvents": [{"eventId": 1, "text": "输入法记忆整理"}]},
            project="wisdom-weasel-rag-ime",
        )

        self.assertEqual(payload["instruction"], DEFAULT_MEMORY_ORGANIZATION_INSTRUCTION)
        request_payload = captured["payload"]
        self.assertIn(DEFAULT_MEMORY_ORGANIZATION_INSTRUCTION, request_payload["messages"][1]["content"])
        self.assertIn("不直接写入正式记忆", DEFAULT_MEMORY_ORGANIZATION_INSTRUCTION)

    def test_owner_curator_requires_source_coverage_and_defines_not_for_memory_examples(self) -> None:
        config = load_deepseek_config(
            env={
                "DEEPSEEK_API_KEY": "secret",
                "RAG_IME_DEEPSEEK_BASE_URL": "https://api.deepseek.com",
                "RAG_IME_DEEPSEEK_MODEL": "deepseek-v4-flash",
            }
        )
        captured: dict[str, object] = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                body = {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "sourceDecisions": [
                                            {
                                                "sourceRef": "S1",
                                                "disposition": "remember",
                                                "reasonCode": "durable_constraint",
                                                "confidence": 0.95,
                                            }
                                        ],
                                        "memoryAtoms": [],
                                        "phraseCandidates": [{"text": "不应写入词库"}],
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                }
                return json.dumps(body, ensure_ascii=False).encode("utf-8")

        def fake_urlopen(request, timeout):
            request_payload = json.loads(request.data.decode("utf-8"))
            captured.setdefault("payloads", []).append(request_payload)
            captured.setdefault("payload", request_payload)
            return FakeResponse()

        payload = DeepSeekMemoryOrganizer(config, urlopen=fake_urlopen).curate_owner_memory(
            bundle={
                "inputs": [
                    {
                        "sourceRef": "S1",
                        "sourceKind": "user_final",
                        "trustClass": "user_claim",
                        "createdAtMs": 1,
                        "sourceEventIds": [11],
                        "text": "不要截图",
                    },
                    {
                        "sourceRef": "S2",
                        "sourceKind": "user_final",
                        "trustClass": "user_claim",
                        "createdAtMs": 2,
                        "sourceEventIds": [12],
                        "text": "嗯嗯那个这个",
                    },
                ]
            },
            project="wisdom-weasel-rag-ime",
            owner_kind="user",
            owner_id="default",
        )

        self.assertEqual(
            [(item["sourceRef"], item["disposition"]) for item in payload["sourceDecisions"]],
            [("S1", "remember"), ("S2", "needs_review")],
        )
        self.assertEqual(payload["phraseCandidates"], [])
        self.assertEqual(len(captured["payloads"]), 2)
        self.assertIn("retry", payload["modelDiagnostics"])
        request_payload = captured["payload"]
        system_prompt = request_payload["messages"][0]["content"]
        self.assertIn("not_for_memory", system_prompt)
        self.assertIn("captureHints", system_prompt)
        self.assertIn("助手回答", system_prompt)
        self.assertIn("工具回执", system_prompt)
        self.assertIn("测试/构建/安装结果", system_prompt)
        self.assertIn("当前进度", system_prompt)
        self.assertIn("会话摘要", system_prompt)
        self.assertIn("一次性请求", system_prompt)
        self.assertIn(
            "kind 只使用 personal_fact、personal_habit、durable_preference 或 personal_principle",
            system_prompt,
        )
        self.assertIn("不能把项目需求改写成个人原则", system_prompt)
        self.assertIn("每个 bundle.inputs.sourceRef 恰好输出", system_prompt)
        self.assertIn("必须 needs_review", system_prompt)
        recovery_prompt = captured["payloads"][1]["messages"][0]["content"]
        self.assertIn("项目功能、架构选择、文件改动、命令", recovery_prompt)
        self.assertIn("任务进度、会话摘要、临时计划", recovery_prompt)
        self.assertIn("证据不足或冲突时 needs_review 并 abstain", recovery_prompt)

    def test_owner_curator_rejects_non_personal_atom_kinds_at_provider_boundary(self) -> None:
        config = load_deepseek_config(
            env={
                "DEEPSEEK_API_KEY": "secret",
                "RAG_IME_DEEPSEEK_BASE_URL": "https://api.deepseek.com",
                "RAG_IME_DEEPSEEK_MODEL": "deepseek-v4-flash",
            }
        )

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                body = {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "sourceDecisions": [
                                            {
                                                "sourceRef": "S1",
                                                "disposition": "remember",
                                                "reasonCode": "project_requirement",
                                                "confidence": 0.98,
                                            }
                                        ],
                                        "memoryAtoms": [
                                            {
                                                "canonicalText": "项目要增加一个控制面板。",
                                                "summary": "增加控制面板",
                                                "kind": "project_requirement",
                                                "sourceEventIds": [11],
                                                "confidence": 0.98,
                                                "qualityScore": 0.95,
                                                "directCandidateAllowed": False,
                                            }
                                        ],
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                }
                return json.dumps(body, ensure_ascii=False).encode("utf-8")

        payload = DeepSeekMemoryOrganizer(
            config,
            urlopen=lambda request, timeout: FakeResponse(),
        ).curate_owner_memory(
            bundle={
                "inputs": [
                    {
                        "sourceRef": "S1",
                        "sourceKind": "user_final",
                        "trustClass": "user_claim",
                        "createdAtMs": 1,
                        "sourceEventIds": [11],
                        "text": "项目要增加一个控制面板。",
                        "captureHints": [
                            {
                                "kind": "decision",
                                "claim": "项目要增加一个控制面板。",
                                "confidence": 0.98,
                            }
                        ],
                    }
                ]
            },
            project="wisdom-weasel-rag-ime",
            owner_kind="user",
            owner_id="default",
        )

        self.assertEqual(payload["memoryAtoms"], [])
        self.assertEqual(payload["sourceDecisions"][0]["disposition"], "not_for_memory")
        self.assertEqual(
            payload["sourceDecisions"][0]["reasonCode"],
            "non_personal_memory_kind",
        )
        self.assertIn("rejected_non_personal_memory_atoms:1", payload["warnings"])

    def test_owner_curator_recovers_truncated_output_with_compact_retry(self) -> None:
        config = load_deepseek_config(
            env={
                "DEEPSEEK_API_KEY": "secret",
                "RAG_IME_DEEPSEEK_BASE_URL": "https://api.deepseek.com",
                "RAG_IME_DEEPSEEK_MODEL": "deepseek-v4-flash",
            }
        )
        requests: list[dict[str, object]] = []
        responses = [
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {
                            "content": json.dumps(
                                {
                                    "sourceDecisions": [
                                        {
                                            "sourceRef": "S1",
                                            "disposition": "remember",
                                            "reasonCode": "durable_preference",
                                            "confidence": 0.95,
                                        }
                                    ]
                                }
                            )
                        },
                    }
                ]
            },
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "sourceDecisions": [
                                        {
                                            "sourceRef": "S1",
                                            "disposition": "remember",
                                            "reasonCode": "durable_preference",
                                            "confidence": 0.95,
                                        },
                                        {
                                            "sourceRef": "S2",
                                            "disposition": "not_for_memory",
                                            "reasonCode": "input_noise_filler",
                                            "confidence": 0.98,
                                        },
                                    ],
                                    "memoryAtoms": [
                                        {
                                            "canonicalText": "用户要求记忆按需召回。",
                                            "summary": "按需召回",
                                            "kind": "durable_preference",
                                            "sourceEventIds": [11],
                                        }
                                    ],
                                },
                                ensure_ascii=False,
                            )
                        },
                    }
                ]
            },
        ]

        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")

        def fake_urlopen(request, timeout):
            requests.append(json.loads(request.data.decode("utf-8")))
            return FakeResponse(responses[len(requests) - 1])

        result = DeepSeekMemoryOrganizer(
            config,
            urlopen=fake_urlopen,
        ).curate_owner_memory(
            bundle={
                "inputs": [
                    {
                        "sourceRef": "S1",
                        "sourceKind": "user_final",
                        "trustClass": "user_claim",
                        "createdAtMs": 1,
                        "sourceEventIds": [11],
                        "text": "记忆只按当前问题召回。",
                    },
                    {
                        "sourceRef": "S2",
                        "sourceKind": "user_final",
                        "trustClass": "user_claim",
                        "createdAtMs": 2,
                        "sourceEventIds": [12],
                        "text": "嗯嗯那个这个",
                    },
                ],
                "existingMemoryAtoms": [
                    {
                        "atomId": f"atom:{index}",
                        "canonicalText": f"已有事实 {index}",
                    }
                    for index in range(40)
                ],
            },
            project="wisdom-weasel-rag-ime",
            owner_kind="user",
            owner_id="default",
        )

        self.assertEqual(len(requests), 2)
        self.assertEqual(result["modelDiagnostics"]["finishReason"], "length")
        self.assertEqual(result["modelDiagnostics"]["retry"]["finishReason"], "stop")
        self.assertIn(
            "owner_curation_recovered_with_compact_retry",
            result["warnings"],
        )
        self.assertEqual(len(result["memoryAtoms"]), 1)
        retry_bundle = json.loads(requests[1]["messages"][1]["content"])["bundle"]
        self.assertLessEqual(len(retry_bundle["existingMemoryAtoms"]), 8)

    def test_memory_organizer_repairs_missing_phrase_pinyin_with_bounded_second_request(self) -> None:
        config = load_deepseek_config(
            env={
                "DEEPSEEK_API_KEY": "secret",
                "RAG_IME_DEEPSEEK_BASE_URL": "https://api.deepseek.com",
                "RAG_IME_DEEPSEEK_MODEL": "deepseek-v4-flash",
            }
        )
        response_payloads = [
            {
                "schemaVersion": "rag-ime.memory-book-compile.v1",
                "dailyBooks": [],
                "topicBooks": [],
                "memoryAtoms": [],
                "tagEdges": [],
                "phraseCandidates": [
                    {
                        "text": "表情包",
                        "pinyin": "",
                        "tags": ["词库"],
                        "weight": 0.8,
                        "sourceEventIds": [1, 2],
                    },
                    {
                        "text": "输入法",
                        "pinyin": "shu ru fa",
                        "tags": ["项目"],
                        "weight": 0.7,
                        "sourceEventIds": [2],
                    },
                ],
                "warnings": [],
            },
            {"items": [{"text": "表情包", "pinyin": "biao qing bao"}]},
        ]
        requests = []

        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                body = {
                    "choices": [
                        {"message": {"content": json.dumps(self.payload, ensure_ascii=False)}}
                    ]
                }
                return json.dumps(body, ensure_ascii=False).encode("utf-8")

        def fake_urlopen(request, timeout):
            requests.append(json.loads(request.data.decode("utf-8")))
            return FakeResponse(response_payloads[len(requests) - 1])

        organizer = DeepSeekMemoryOrganizer(config, urlopen=fake_urlopen)
        payload = organizer.compile_memory_book(
            bundle={"recentEvents": [{"eventId": 1, "text": "使用表情包"}]},
            project="wisdom-weasel-rag-ime",
        )

        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1]["max_tokens"], 512)
        self.assertEqual(requests[1]["messages"][1]["content"], '{"project": "wisdom-weasel-rag-ime", "texts": ["表情包"]}')
        self.assertEqual(payload["phraseCandidates"][0]["pinyin"], "biao qing bao")
        self.assertEqual(payload["phraseCandidates"][1]["pinyin"], "shu ru fa")
        self.assertIn("phrase_pinyin_repaired:1", payload["warnings"])

    def test_memory_organizer_keeps_unrepaired_phrase_out_of_dsv4_lexicon_contract(self) -> None:
        config = load_deepseek_config(
            env={
                "DEEPSEEK_API_KEY": "secret",
                "RAG_IME_DEEPSEEK_BASE_URL": "https://api.deepseek.com",
                "RAG_IME_DEEPSEEK_MODEL": "deepseek-v4-flash",
            }
        )
        response_payloads = [
            {"phraseCandidates": [{"text": "未知词", "sourceEventIds": [1]}]},
            {"items": []},
        ]

        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                body = {"choices": [{"message": {"content": json.dumps(self.payload, ensure_ascii=False)}}]}
                return json.dumps(body, ensure_ascii=False).encode("utf-8")

        calls = 0

        def fake_urlopen(request, timeout):
            nonlocal calls
            response = FakeResponse(response_payloads[calls])
            calls += 1
            return response

        payload = DeepSeekMemoryOrganizer(config, urlopen=fake_urlopen).compile_memory_book(
            bundle={"recentEvents": [{"eventId": 1, "text": "未知词"}]},
            project="wisdom-weasel-rag-ime",
        )

        self.assertEqual(calls, 2)
        self.assertNotIn("pinyin", payload["phraseCandidates"][0])
        self.assertIn("phrase_pinyin_missing:1", payload["warnings"])

    def test_empty_primary_response_recovers_with_compact_governed_retry(self) -> None:
        config = load_deepseek_config(
            env={
                "DEEPSEEK_API_KEY": "secret",
                "RAG_IME_DEEPSEEK_BASE_URL": "https://api.deepseek.com",
                "RAG_IME_DEEPSEEK_MODEL": "deepseek-v4-flash",
            }
        )
        payloads = [
            {"semanticGroups": [], "semanticTags": [], "memoryAtoms": []},
            {
                "semanticGroups": [
                    {
                        "groupId": "group:input-method",
                        "title": "输入法",
                        "description": "输入法与个人记忆",
                        "sourceEventIds": [1, 2],
                    }
                ],
                "semanticTags": [
                    {
                        "name": "记忆清洗",
                        "description": "模型清洗输入历史后再建立索引",
                        "semanticGroupIds": ["group:input-method"],
                        "sourceEventIds": [1, 2],
                    }
                ],
                "memoryAtoms": [
                    {
                        "canonicalText": "原始输入需经模型清洗后再索引。",
                        "sourceEventIds": [1, 2],
                        "semanticGroupIds": ["group:input-method"],
                    }
                ],
            },
        ]
        requests = []

        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(
                    {
                        "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(self.payload, ensure_ascii=False)}}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 80},
                    },
                    ensure_ascii=False,
                ).encode("utf-8")

        def fake_urlopen(request, timeout):
            requests.append(json.loads(request.data.decode("utf-8")))
            return FakeResponse(payloads[len(requests) - 1])

        result = DeepSeekMemoryOrganizer(config, urlopen=fake_urlopen).compile_memory_book(
            bundle={
                "project": "wisdom-weasel-rag-ime",
                "recentEvents": [
                    {"eventId": 1, "sourceEventIds": [1], "text": "输入法需要整理记忆"},
                    {"eventId": 2, "sourceEventIds": [2], "text": "历史要清洗后再索引"},
                ],
            },
            project="wisdom-weasel-rag-ime",
        )

        self.assertEqual(len(requests), 2)
        self.assertEqual(result["semanticGroups"][0]["groupId"], "group:input-method")
        self.assertIn("organizer_recovered_with_compact_retry", result["warnings"])
        self.assertEqual(result["modelDiagnostics"]["retry"]["finishReason"], "stop")
        self.assertLess(result["modelBundleStats"]["chars"], 2_000)


if __name__ == "__main__":
    unittest.main()
