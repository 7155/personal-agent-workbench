from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.run_rag_agent_ablation import (
    REQUIRED_HARD_GATES,
    _append_lane_checkpoint_attempt,
    _append_lane_checkpoint_attempt_binding,
    _append_lane_checkpoint_attempt_started,
    _agentic_critic_contract_passes,
    _agentic_parent_query_policy_passes,
    _agents_tool_receipts,
    _answer_judge_failure_is_retryable,
    _answer_judge_case_payloads,
    _answer_judge_format_repair_prompt,
    _answer_judge_prompt,
    _build_candidate_decision,
    _answer_case_manifest,
    _authorize_held_out,
    _claim_held_out_gate,
    _child_token_usage,
    _combine_token_usage,
    _copy_openai_codex_agent_config,
    _apply_answer_only_judgments,
    _apply_answer_judgments,
    _knowledge_base_retrieval_config,
    _lane_prompt,
    _last_assistant_snapshot_text,
    _merge_event_evidence,
    _coverage_critic_completed,
    _coverage_audit_prompt,
    _checkpoint_lane_record_projection,
    _checkpoint_private_search_trace,
    _evaluation_configuration_defaults,
    _finalize_public_report,
    _lane_checkpoint_fingerprint,
    _lane_checkpoint_attempt_history,
    _lane_checkpoint_report_projection,
    _lane_checkpoint_reusable_records,
    _open_lane_checkpoint,
    _output_protocol_repair_needed,
    _output_protocol_repair_prompt,
    _recover_lane_checkpoint_orphans,
    _recover_private_evaluation_session,
    _require_actual_metal_runtime,
    _redact_absolute_paths,
    _preflight_failure_report,
    _production_baseline_record,
    _parse_answer_judge_rubrics,
    _parse_answer_judgments,
    _pin_evaluation_agent_config,
    _runtime_failure_category,
    _runtime_tool_failure_count,
    _run_coverage_audit,
    _run_output_protocol_repair,
    _run_lane,
    _resolve_evaluation_split,
    _search_parameter_policy_passes,
    _sha256_json,
    _started_tool_names,
    _token_usage,
    _validate_answer_evidence_qrels,
    _validate_checkpoint_request,
)


_HISTORICAL_CHUNKER_DEPENDENCY_SURFACE = [
    {
        "qualifiedName": "rag_ime.knowledge_library.parsers._normalize_text",
        "sourceSha256": "08d29fb737c7a79b679af8679a883d9bd9a5e87aa0e4e68431a7d2bf9aae0b2f",
    },
    {
        "qualifiedName": "rag_ime.knowledge_library.service._chunk_block_heading",
        "sourceSha256": "a708c44c68974d4e70bd127c821b6a6daddc942184a61e3c0832bab5fbb53c6c",
    },
    {
        "qualifiedName": "rag_ime.knowledge_library.service._chunk_document",
        "sourceSha256": "b4d254d72c3e99bbc7bf7d5ba4776db440b191ab5e15621661a5899c25867655",
    },
    {
        "qualifiedName": "rag_ime.knowledge_library.service._chunk_record",
        "sourceSha256": "2f3463f11fb29c73d70b4c6c15717fc9696cf9a571fa214f83290cad13776ed9",
    },
    {
        "qualifiedName": "rag_ime.knowledge_library.service._chunk_strategy_blocks",
        "sourceSha256": "ebed9f30e0bd7c125a230a949dfcc56951ff074080c47662b959fa9e69dc7e79",
    },
]
# The checked-in historical standard retains its original dependency identity.
# Fresh test runs pin the unchanged current HEAD source, rather than rewriting
# historical artifacts or deriving the expected fingerprint from the validator.
_TEST_CHUNKER_DEPENDENCY_SURFACE = [
    {**item, "sourceSha256": "a88fa1978905cece3f9573796b5a978644941dc8ff39ef9dbde77e6b4bf0abeb"}
    if item["qualifiedName"] == "rag_ime.knowledge_library.service._chunk_document"
    else dict(item)
    for item in _HISTORICAL_CHUNKER_DEPENDENCY_SURFACE
]
_TEST_CHUNK_MANIFEST_SERIALIZATION = {
    "schemaVersion": "rag-ime.rag-chunk-manifest-serialization.v1",
    "canonicalJson": {
        "encoding": "utf-8",
        "ensureAscii": False,
        "sortKeys": True,
        "separators": [",", ":"],
    },
    "recordOrder": [
        "documentId:unicode-code-point-ascending",
        "chunkOrdinal:integer-ascending",
    ],
    "recordFields": [
        "documentId",
        "chunkOrdinal",
        "contentSha256",
        "headingSha256",
        "page",
    ],
}


def _test_answer_evidence_standard_v2(
    *,
    documents: dict[str, str],
    chunking: dict[str, object],
    prepared_source_sha256: str,
    prepared_artifact_sha256: str,
) -> dict[str, object]:
    chunk_records = [
        {
            "documentId": document_id,
            "chunkOrdinal": 0,
            "contentSha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "headingSha256": hashlib.sha256(b"").hexdigest(),
            "page": None,
        }
        for document_id, text in sorted(documents.items())
    ]
    standard: dict[str, object] = {
        "schemaVersion": "rag-ime.rag-answer-evidence-standard.v2",
        "standardId": "test-answer-evidence-standard-v2",
        "evaluationScope": "validation-development-only",
        "calibrationLabel": "post-validation-calibrated",
        "candidateBlind": True,
        "unbiasedPromotionClaimAllowed": False,
        "heldOutOpened": False,
        "corpus": {
            "preparedArtifactSha256": prepared_artifact_sha256,
            "preparedSourceSha256": prepared_source_sha256,
            "documentCount": len(documents),
        },
        "chunking": {
            "config": chunking,
            "configSha256": _sha256_json(chunking),
            "dependencySurface": _TEST_CHUNKER_DEPENDENCY_SURFACE,
            "dependencySurfaceSha256": _sha256_json(
                _TEST_CHUNKER_DEPENDENCY_SURFACE
            ),
        },
        "chunkManifest": {
            "serialization": _TEST_CHUNK_MANIFEST_SERIALIZATION,
            "serializationSha256": _sha256_json(
                _TEST_CHUNK_MANIFEST_SERIALIZATION
            ),
            "chunkCount": len(chunk_records),
            "manifestSha256": _sha256_json(chunk_records),
        },
        "calibrationSource": {
            "auditId": "candidate-blind-test-audit",
            "auditReceiptSha256": "a" * 64,
            "proposalSha256": "b" * 64,
        },
    }
    standard["manifestSha256"] = _sha256_json(standard)
    return standard


def _resign_test_qrels_v2(value: dict[str, object]) -> None:
    standard = value["answerEvidenceStandard"]
    assert isinstance(standard, dict)
    standard.pop("manifestSha256", None)
    standard["manifestSha256"] = _sha256_json(standard)
    value["standardManifestSha256"] = standard["manifestSha256"]
    value.pop("manifestSha256", None)
    value["manifestSha256"] = _sha256_json(value)


class RunRagAgentAblationTests(unittest.TestCase):
    def _cancel_during_import(self, *, close_failure=False):
        from contextlib import ExitStack
        from unittest.mock import Mock, patch
        from scripts import run_rag_agent_ablation as runner

        stopped = False
        sandbox, service, server = Mock(), Mock(), Mock()
        if close_failure:
            service.close.side_effect = RuntimeError("fixture close failed")
        sandbox.create_run.return_value = {"runId": "owned-run"}
        sandbox.create_base.return_value = {"configRevision": 1}
        sandbox.cleanup.return_value = {"deleted": True}

        def stop_after_import(*_args, **_kwargs):
            nonlocal stopped
            stopped = True

        sandbox.import_documents.side_effect = stop_after_import
        config = {}
        config_hash = _sha256_json(config)
        fixtures = {
            "_load_prepared": {},
            "_read_json_object": {"validationSelection": {"winner": {"config": config}, "frozenConfigSha256": config_hash}, "chunking": {}},
            "_reconstruct_frozen_slice": ([], [{"documentId": "fixture", "text": "synthetic"}], {"benchmarkId": "fixture", "sourceSha256": "source"}),
            "_development_exclusion": {"caseIds": []},
            "select_agent_held_out_cases": [{"queryId": "q1", "query": "synthetic question"}],
            "_production_baseline_record": {"config": config, "configSha256": config_hash},
            "_embedding_environment_from_report": {"RAG_IME_KNOWLEDGE_DENSE_BACKEND": "fixture"},
            "_copy_openai_codex_agent_config": None,
            "_pin_evaluation_agent_config": {},
            "_isolated_runtime_config": Mock(),
            "_public_pi_runtime_identity": {},
            "_evaluation_configuration_identity": {},
            "_require_semantic_dense_runtime": {"accepted": False},
            "RagBenchmarkSandbox": sandbox,
            "RagBenchmarkAgentGateway": Mock(),
            "ControlToolGateway": Mock(),
            "_start_rag_benchmark_gateway": server,
            "AgentService": service,
        }
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            path = Path(temporary) / "synthetic.json"
            path.write_text("{}", encoding="utf-8")
            for name, value in fixtures.items():
                stack.enter_context(patch.object(runner, name, return_value=value))
            lane = stack.enter_context(patch.object(runner, "_run_lane"))
            judge = stack.enter_context(patch.object(runner, "_run_answer_judge"))
            report = runner._run(Path(temporary), prepared_path=path, answer_cases_path=None, answer_evidence_qrels_path=None, retrieval_report_path=path, source_agent_config=path,
                slice_seed="fixture", agent_seed="fixture", slice_cases_per_split=1, agent_case_limit=1, distractor_limit=0, timeout_seconds=30, lane_attempts=2,
                reranker_model=None, reranker_revision="", reranker_cache=None, rerank_instruction="", development_report_paths=[], calibration_no_metal=True,
                evaluation_split="held_out", cancelled=lambda: stopped)
        return report, sandbox, service, server, lane, judge

    def test_run_cancel_during_import_closes_owners_and_returns_non_score_receipt(self) -> None:
        report, sandbox, service, server, lane, judge = self._cancel_during_import()
        self.assertEqual("cancelled", report["status"])
        self.assertFalse(report["passed"])
        self.assertFalse(report["scoreEligible"])
        self.assertFalse(report["formalAcceptanceEligible"])
        self.assertTrue(report["cleanupPassed"])
        self.assertTrue(report["reportSha256"])
        sandbox.cleanup.assert_called_once()
        sandbox.close.assert_called_once()
        service.close.assert_called_once()
        server.close.assert_called_once()
        lane.assert_not_called()
        judge.assert_not_called()

    def test_run_close_failure_still_closes_other_owners_and_marks_settlement_uncertain(self) -> None:
        report, sandbox, service, server, lane, judge = self._cancel_during_import(close_failure=True)
        self.assertEqual("interrupted", report["status"])
        self.assertFalse(report["executionSettled"])
        self.assertFalse(report["scoreEligible"])
        self.assertFalse(report["cleanupPassed"])
        service.close.assert_called_once()
        server.close.assert_called_once()
        sandbox.close.assert_called_once()
        lane.assert_not_called()
        judge.assert_not_called()

    def test_cancelled_lane_keeps_admitted_checkpoint_and_cleans_tool_binding(self) -> None:
        from unittest.mock import Mock
        from scripts import run_rag_agent_ablation as runner

        service, gateway = Mock(), Mock()
        service.create_session.return_value = {"session": {"id": "agent:baseline"}}
        service.ensure_runtime.return_value = {"state": {"model": {"provider": "openai-codex", "id": "gpt-6.1-sol"}, "thinkingLevel": "max"}}
        service.prompt.return_value = {"turnId": "turn:baseline"}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "checkpoint.json"
            checkpoint = _open_lane_checkpoint(path, fingerprint=self._checkpoint_fingerprint(), resume=False)

            def started(attempt):
                nonlocal checkpoint
                checkpoint = _append_lane_checkpoint_attempt_started(path, checkpoint=checkpoint, lane="baseline", attempt=attempt)

            def bound(attempt, session_id, turn_id):
                nonlocal checkpoint
                checkpoint = _append_lane_checkpoint_attempt_binding(path, checkpoint=checkpoint, lane="baseline", attempt=attempt, session_id=session_id, turn_id=turn_id)

            with self.assertRaises(runner.RagEvaluationCancelled):
                runner._run_lane(service, gateway=gateway, owner="owner", run_id="run", lane="baseline", cases=[], retrieval_config={}, retrieval_config_sha256="config", timeout_seconds=30, lane_attempts=2,
                    cancelled=lambda: service.prompt.called, attempt_start_observer=started, attempt_binding_observer=bound)
            persisted = json.loads(path.read_text(encoding="utf-8"))
            history = _lane_checkpoint_attempt_history(persisted, "baseline")
            self.assertEqual(1, len(history))
            self.assertEqual(hashlib.sha256(b"turn:baseline").hexdigest(), history[0]["turnSha256"])
            self.assertEqual({}, _lane_checkpoint_reusable_records(persisted))
        service.abort.assert_called_once_with("agent:baseline")
        gateway.unbind_lineage.assert_called_once_with("agent:baseline")
        service.prompt.assert_called_once()

    def test_cancel_prevents_lane_retry_and_judge_admission(self) -> None:
        from unittest.mock import Mock, patch
        from scripts import run_rag_agent_ablation as runner
        stopped = False
        failed = self._checkpoint_lane_record("tuned", terminal="turn_failed", runtime_failure_category="provider_transient_after_tool")
        def settle(*_args, **_kwargs):
            nonlocal stopped
            stopped = True
            return failed
        with patch.object(runner, "_run_lane_once", side_effect=settle) as once:
            with self.assertRaises(BaseException) as raised:
                runner._run_lane(Mock(), gateway=Mock(), owner="owner", run_id="run", lane="tuned", cases=[], retrieval_config={}, retrieval_config_sha256="config", timeout_seconds=30, lane_attempts=2, cancelled=lambda: stopped)
            self.assertEqual("RagEvaluationCancelled", type(raised.exception).__name__)
            once.assert_called_once()
        with patch.object(runner, "_run_answer_judge_once") as judge:
            with self.assertRaises(BaseException) as raised:
                runner._run_answer_judge(Mock(), cases=[], documents=[], lane_records=[], chunking_config={}, timeout_seconds=30, cancelled=lambda: True)
            self.assertEqual("RagEvaluationCancelled", type(raised.exception).__name__)
            judge.assert_not_called()

    def test_cancellation_after_turn_admission_reports_binding_and_aborts_before_repair(self) -> None:
        from unittest.mock import Mock
        from scripts import run_rag_agent_ablation as runner
        service = Mock()
        service.create_session.return_value = {"session": {"id": "owned"}}
        service.prompt.return_value = {"turnId": "turn-owned"}
        bindings = []
        controlled = runner._RagControlledService(service, cancelled=lambda: service.prompt.called,
            on_session=lambda sid: bindings.append((sid,"")), on_turn=lambda sid,tid: bindings.append((sid,tid)))
        controlled.create_session({})
        receipt = controlled.prompt("owned", {"message":"bounded task"})
        self.assertEqual("turn-owned", receipt["turnId"])
        with self.assertRaises(BaseException) as raised:
            controlled.events.replay("owned")
        self.assertEqual("RagEvaluationCancelled", type(raised.exception).__name__)
        self.assertEqual([("owned",""),("owned","turn-owned")], bindings)
        service.abort.assert_called_once_with("owned")
        with self.assertRaises(BaseException):
            controlled.prompt("owned", {"message":"repair must not run"})
        self.assertEqual(1, service.prompt.call_count)

    def test_cancel_during_actual_judge_wait_aborts_and_never_retries_or_repairs(self) -> None:
        from unittest.mock import Mock, patch
        from scripts import run_rag_agent_ablation as runner
        service = Mock()
        service.create_session.return_value = {"session": {"id": "judge-owned"}}
        service.ensure_runtime.return_value = {"state": {"model": {"provider": "openai-codex", "id": "gpt-6.1-sol"}, "thinkingLevel": "max"}}
        service.prompt.return_value = {"turnId": "judge-turn"}
        service.events.replay.return_value = ([], False)
        bindings = []
        with patch.object(runner, "_answer_judge_case_payloads", return_value=([],[],{},[])):
            with self.assertRaises(BaseException) as raised:
                runner._run_answer_judge(service, cases=[], documents=[], lane_records=[], chunking_config={}, timeout_seconds=30,
                    cancelled=lambda: service.prompt.called, on_turn=lambda sid,tid: bindings.append((sid,tid)))
        self.assertEqual("RagEvaluationCancelled", type(raised.exception).__name__)
        self.assertEqual([("judge-owned","judge-turn")], bindings)
        service.abort.assert_called_once_with("judge-owned")
        service.create_session.assert_called_once()
        service.prompt.assert_called_once()

    def test_abort_error_still_propagates_cancellation_without_retry(self) -> None:
        from unittest.mock import Mock
        from scripts import run_rag_agent_ablation as runner

        service = Mock()
        service.create_session.return_value = {"session": {"id": "owned"}}
        service.abort.side_effect = RuntimeError("runtime already closed")
        controlled = runner._RagControlledService(
            service, cancelled=lambda: service.create_session.called
        )
        controlled.create_session({})
        with self.assertRaises(runner.RagEvaluationCancelled) as raised:
            controlled.prompt("owned", {"message": "must not be admitted"})
        self.assertTrue(raised.exception.interrupted)
        service.abort.assert_called_once_with("owned")
        service.prompt.assert_not_called()

    def test_cancelled_lane_binding_cleanup_failure_preserves_interrupted_stop(self) -> None:
        from unittest.mock import Mock
        from scripts import run_rag_agent_ablation as runner
        service, gateway = Mock(), Mock()
        service.create_session.return_value = {"session": {"id": "owned"}}
        service.ensure_runtime.return_value = {"state": {"model": {"provider": "openai-codex", "id": "gpt-6.1-sol"}, "thinkingLevel": "max"}}
        service.prompt.return_value = {"turnId": "turn-owned"}
        service.abort.side_effect = RuntimeError("fixture abort failed")
        gateway.unbind_lineage.side_effect = RuntimeError("fixture unbind failed")
        with self.assertRaises(runner.RagEvaluationCancelled) as raised:
            runner._run_lane(service, gateway=gateway, owner="owner", run_id="run", lane="baseline", cases=[], retrieval_config={},
                retrieval_config_sha256="config", timeout_seconds=30, lane_attempts=2, cancelled=lambda: service.prompt.called)
        self.assertTrue(raised.exception.interrupted)
        service.prompt.assert_called_once()
        service.abort.assert_called_once()

    def test_public_answer_evidence_standard_v2_is_hash_only_and_fail_closed(self) -> None:
        standard_path = (
            Path(__file__).resolve().parents[1]
            / "eval"
            / "interview-metrics"
            / "enterprise-rag-answer-evidence-standard.v2.json"
        )
        standard = json.loads(standard_path.read_text(encoding="utf-8"))
        claimed_manifest_sha256 = standard.pop("manifestSha256")

        self.assertEqual(_sha256_json(standard), claimed_manifest_sha256)
        self.assertEqual(
            "post-validation-calibrated",
            standard["calibrationLabel"],
        )
        self.assertFalse(standard["unbiasedPromotionClaimAllowed"])
        self.assertFalse(standard["heldOutOpened"])
        self.assertEqual(29846, standard["chunkManifest"]["chunkCount"])
        self.assertEqual(
            "6d5239b3b684c037d38eab4150bfb35d9d6393350cb9cff31bdc2c0b19d550cf",
            standard["chunkManifest"]["manifestSha256"],
        )
        self.assertEqual(
            _HISTORICAL_CHUNKER_DEPENDENCY_SURFACE,
            standard["chunking"]["dependencySurface"],
        )
        serialized = json.dumps(standard, ensure_ascii=False, sort_keys=True)
        for private_token in ('"cases"', '"facts"', '"quote"', "dsid_"):
            self.assertNotIn(private_token, serialized)

        receipt_path = (
            Path(__file__).resolve().parents[1]
            / "eval"
            / "interview-metrics"
            / "runs"
            / "enterprise-rag-answer-evidence-standard-v2-validation-calibration-20260904.v1.json"
        )
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertFalse(receipt["unbiasedPromotionClaimAllowed"])
        self.assertFalse(receipt["heldOutOpened"])
        self.assertEqual(0, receipt["providerCalls"])
        self.assertTrue(all(not value for value in receipt["publicSafety"].values()))
        serialized_receipt = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
        for private_token in ('"cases"', '"facts"', '"quote"', "dsid_"):
            self.assertNotIn(private_token, serialized_receipt)

    def test_answer_judge_evidence_is_limited_to_cited_retrieved_chunks(self) -> None:
        visible_chunk = "VISIBLE-CITED-CHUNK " + ("a" * 170)
        hidden_document_tail = "HIDDEN-UNCITED-DOCUMENT-TAIL " + ("b" * 170)
        documents = [
            {
                "documentId": "doc-a",
                "text": visible_chunk + "\n\n" + hidden_document_tail,
            }
        ]
        lane_records = [
            {
                "lane": "baseline",
                "_assistantText": (
                    '{"cases":[{"caseId":"case-01","answer":"visible",'
                    '"citations":["K1"],"abstained":false}]}'
                ),
                "score": {
                    "answerCases": [
                        {
                            "evaluationCaseId": "case-01",
                            "citationTokens": ["K1"],
                            "citations": ["doc-a"],
                        }
                    ]
                },
                "gatewayLedger": {
                    "items": [
                        {
                            "operation": "search",
                            "ok": True,
                            "args": {"evaluationCaseId": "case-01"},
                            "resultSummary": {
                                "hits": [
                                    {
                                        "citationRef": "K1",
                                        "externalDocumentId": "doc-a",
                                        "chunkId": "runtime-chunk-1",
                                        "ordinal": 0,
                                    },
                                    {
                                        "citationRef": "K2",
                                        "externalDocumentId": "doc-a",
                                        "chunkId": "runtime-chunk-2",
                                        "ordinal": 1,
                                    }
                                ]
                            },
                        }
                    ]
                },
            }
        ]

        _case_ids, _candidate_ids, _assistant_cases, judge_cases = (
            _answer_judge_case_payloads(
                cases=[
                    {
                        "evaluationCaseId": "case-01",
                        "query": "What is visible?",
                        "answer": "visible",
                    }
                ],
                documents=documents,
                lane_records=lane_records,
                chunking_config={
                    "strategy": "general",
                    "size": 200,
                    "overlap": 0,
                },
            )
        )

        evidence = judge_cases[0]["evidenceDocuments"]
        self.assertEqual(1, len(evidence))
        self.assertEqual(visible_chunk, evidence[0]["text"])
        self.assertNotIn("HIDDEN-UNCITED-DOCUMENT-TAIL", json.dumps(judge_cases))
        self.assertEqual(
            [evidence[0]["evidenceId"]],
            judge_cases[0]["candidates"][0]["evidenceIds"],
        )

        checkpoint_record, _session_id, _turn_id = (
            _checkpoint_lane_record_projection(lane_records[0])
        )
        _case_ids, _candidate_ids, _assistant_cases, resumed_cases = (
            _answer_judge_case_payloads(
                cases=[
                    {
                        "evaluationCaseId": "case-01",
                        "query": "What is visible?",
                        "answer": "visible",
                    }
                ],
                documents=documents,
                lane_records=[checkpoint_record],
                chunking_config={
                    "strategy": "general",
                    "size": 200,
                    "overlap": 0,
                },
            )
        )
        self.assertEqual(visible_chunk, resumed_cases[0]["evidenceDocuments"][0]["text"])
        self.assertNotIn(
            "doc-a", json.dumps(checkpoint_record["_privateCitedChunkRefs"])
        )
        self.assertEqual(
            "K1",
            checkpoint_record["_privateCitedChunkRefs"][0]["hits"][0][
                "citationRef"
            ],
        )

    def test_token_usage_counts_each_provider_request_without_double_counting_final_message(self) -> None:
        events = [
            {
                "eventType": "provider_request_completed",
                "payload": {
                    "usage": {
                        "input": 100,
                        "output": 20,
                        "cacheRead": 30,
                        "cacheWrite": 0,
                        "totalTokens": 150,
                    }
                },
            },
            {
                "eventType": "provider_request_completed",
                "payload": {
                    "usage": {
                        "inputTokens": 80,
                        "outputTokens": 10,
                        "cacheReadTokens": 40,
                        "cacheWriteTokens": 0,
                        "totalTokens": 130,
                    }
                },
            },
            {
                "eventType": "message_completed",
                "payload": {
                    "usage": {
                        "input": 80,
                        "output": 10,
                        "cacheRead": 40,
                        "cacheWrite": 0,
                        "totalTokens": 130,
                    }
                },
            },
        ]

        self.assertEqual(
            {
                "inputTokens": 180,
                "outputTokens": 30,
                "cacheReadTokens": 70,
                "cacheWriteTokens": 0,
                "totalTokens": 280,
                "usageSource": "provider_request_receipts",
                "providerRequestCount": 2,
                "categoryReceiptComplete": True,
            },
            _token_usage(events),
        )

    def test_token_usage_falls_back_to_legacy_completed_message(self) -> None:
        usage = _token_usage(
            [
                {
                    "eventType": "message_completed",
                    "payload": {
                        "usage": {
                            "input": 12,
                            "output": 3,
                            "cacheRead": 5,
                            "cacheWrite": 0,
                            "totalTokens": 20,
                        }
                    },
                }
            ]
        )

        self.assertEqual("message_completed_fallback", usage["usageSource"])
        self.assertEqual(0, usage["providerRequestCount"])
        self.assertEqual(20, usage["totalTokens"])
        self.assertFalse(usage["categoryReceiptComplete"])

    def test_child_token_usage_recovers_categories_from_durable_provider_observation(self) -> None:
        class FakeObservations:
            def __init__(self) -> None:
                self.flush_calls: list[float] = []
                self.snapshot_payloads: list[dict[str, object]] = []
                self.event_projection_ready = False

            def flush(self, *, timeout_seconds: float) -> bool:
                self.flush_calls.append(timeout_seconds)
                return True

            def snapshot(self, payload: dict[str, object]) -> dict[str, object]:
                self.snapshot_payloads.append(payload)
                if not self.event_projection_ready:
                    return {"items": []}
                return {
                    "items": [
                        {
                            "name": "provider.request",
                            "phase": "provider_request_completed",
                            "status": "completed",
                            "metrics": {
                                "inputTokens": 10,
                                "outputTokens": 5,
                                "cacheReadTokens": 40,
                                "cacheWriteTokens": 0,
                                "totalTokens": 55,
                            },
                        }
                    ]
                }

        class FakeEvents:
            def __init__(self, observations: FakeObservations) -> None:
                self.observations = observations
                self.flush_calls: list[float] = []

            def flush(self, *, timeout: float) -> bool:
                self.flush_calls.append(timeout)
                self.observations.event_projection_ready = True
                return True

        class FakeService:
            def __init__(self) -> None:
                self.observations = FakeObservations()
                self.events = FakeEvents(self.observations)

            def messages(self, _session_id: str) -> dict[str, object]:
                # Settled child snapshots deliberately omit Provider receipts.
                return {"liveEvents": [{"eventType": "turn_completed"}]}

        service = FakeService()
        usage = _child_token_usage(
            service,
            {"childSessionId": "child-a", "usage": {"totalTokens": 55}},
        )

        self.assertEqual(10, usage["inputTokens"])
        self.assertEqual(5, usage["outputTokens"])
        self.assertEqual(40, usage["cacheReadTokens"])
        self.assertEqual(0, usage["cacheWriteTokens"])
        self.assertEqual(55, usage["totalTokens"])
        self.assertEqual(1, usage["providerRequestCount"])
        self.assertEqual("durable_provider_observation_receipts", usage["usageSource"])
        self.assertTrue(usage["categoryReceiptComplete"])
        self.assertEqual([2.0], service.events.flush_calls)
        self.assertEqual([2.0], service.observations.flush_calls)
        self.assertEqual(
            [{"sessionId": "child-a", "limit": 500}],
            service.observations.snapshot_payloads,
        )

    def test_child_token_usage_fails_closed_when_observation_total_disagrees(self) -> None:
        class FakeObservations:
            def flush(self, *, timeout_seconds: float) -> bool:
                return True

            def snapshot(self, _payload: dict[str, object]) -> dict[str, object]:
                return {
                    "items": [
                        {
                            "name": "provider.request",
                            "phase": "provider_request_completed",
                            "status": "completed",
                            "metrics": {
                                "inputTokens": 10,
                                "outputTokens": 5,
                                "cacheReadTokens": 40,
                                "cacheWriteTokens": 0,
                                "totalTokens": 55,
                            },
                        }
                    ]
                }

        class FakeService:
            observations = FakeObservations()

            class events:
                @staticmethod
                def flush(*, timeout: float) -> bool:
                    return timeout == 2.0

            def messages(self, _session_id: str) -> dict[str, object]:
                return {"liveEvents": []}

        usage = _child_token_usage(
            FakeService(),
            {"childSessionId": "child-a", "usage": {"totalTokens": 56}},
        )

        self.assertEqual(56, usage["totalTokens"])
        self.assertFalse(usage["categoryReceiptComplete"])
        self.assertEqual("durable_provider_observation_receipts_mismatch", usage["usageSource"])

    def test_child_token_usage_recovers_categories_from_settled_transcript(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-child-usage-") as directory:
            transcript = Path(directory) / "child.jsonl"
            transcript.write_text("\n".join([
                json.dumps({"type": "session", "id": "pi-child"}),
                json.dumps({
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "public result"}],
                        "usage": {
                            "input": 10,
                            "output": 5,
                            "cacheRead": 40,
                            "cacheWrite": 0,
                            "totalTokens": 55,
                        },
                    },
                }),
            ]) + "\n", encoding="utf-8")

            class FakeObservations:
                @staticmethod
                def flush(*, timeout_seconds: float) -> bool:
                    return timeout_seconds == 2.0

                @staticmethod
                def snapshot(_payload: dict[str, object]) -> dict[str, object]:
                    return {"items": []}

            class FakeSessions:
                @staticmethod
                def runtime_binding(_session_id: str) -> dict[str, object]:
                    return {"transcriptRef": transcript.as_posix()}

            class FakeService:
                observations = FakeObservations()
                sessions = FakeSessions()

                class events:
                    @staticmethod
                    def flush(*, timeout: float) -> bool:
                        return timeout == 2.0

                @staticmethod
                def messages(_session_id: str) -> dict[str, object]:
                    return {"liveEvents": [{"eventType": "turn_completed"}]}

            usage = _child_token_usage(
                FakeService(),
                {"childSessionId": "child-a", "usage": {"totalTokens": 55}},
            )

        self.assertEqual(10, usage["inputTokens"])
        self.assertEqual(5, usage["outputTokens"])
        self.assertEqual(40, usage["cacheReadTokens"])
        self.assertEqual(0, usage["cacheWriteTokens"])
        self.assertEqual(55, usage["totalTokens"])
        self.assertEqual(1, usage["providerRequestCount"])
        self.assertEqual("settled_child_transcript_usage", usage["usageSource"])
        self.assertTrue(usage["categoryReceiptComplete"])

    def test_combined_token_usage_keeps_oauth_cost_categories(self) -> None:
        combined = _combine_token_usage(
            {
                "inputTokens": 100,
                "outputTokens": 20,
                "cacheReadTokens": 30,
                "cacheWriteTokens": 0,
                "totalTokens": 150,
                "providerRequestCount": 2,
                "categoryReceiptComplete": True,
            },
            [{
                "inputTokens": 10,
                "outputTokens": 5,
                "cacheReadTokens": 40,
                "cacheWriteTokens": 0,
                "totalTokens": 55,
                "providerRequestCount": 1,
                "categoryReceiptComplete": True,
            }],
        )

        self.assertEqual(110, combined["inputTokens"])
        self.assertEqual(25, combined["outputTokens"])
        self.assertEqual(70, combined["cacheReadTokens"])
        self.assertEqual(205, combined["totalTokens"])
        self.assertEqual(3, combined["providerRequestCount"])
        self.assertTrue(combined["categoryReceiptComplete"])

    def test_candidate_decision_ignores_losing_lane_quality_but_not_integrity(self) -> None:
        lanes = [
            {"lane": lane, "hardGates": dict.fromkeys(REQUIRED_HARD_GATES, True)}
            for lane in ("baseline", "skill", "tuned", "agentic")
        ]
        lanes[0]["hardGates"]["citationResolution"] = False
        lanes[1]["hardGates"]["abstention"] = False

        accepted = _build_candidate_decision(lanes)

        self.assertTrue(accepted["accepted"])
        self.assertEqual("keep", accepted["decision"])
        self.assertEqual("quality_and_contract_only", accepted["decisionScope"])
        self.assertEqual("downstream_cross_run_selection", accepted["costDecisionRole"])
        self.assertEqual("diagnostic_only", accepted["latencyDecisionRole"])
        self.assertEqual(
            ["baseline:citationResolution", "skill:abstention"],
            accepted["losingLaneOutcomeFailures"],
        )

        lanes[2]["hardGates"]["terminalCompletion"] = False
        integrity_rejected = _build_candidate_decision(lanes)
        self.assertFalse(integrity_rejected["accepted"])
        self.assertIn("tuned:terminalCompletion", integrity_rejected["failedHardGates"])

        lanes[2]["hardGates"]["terminalCompletion"] = True
        lanes[3]["hardGates"]["citationResolution"] = False
        candidate_rejected = _build_candidate_decision(lanes)
        self.assertFalse(candidate_rejected["accepted"])
        self.assertIn("agentic:citationResolution", candidate_rejected["failedHardGates"])

    @staticmethod
    def _held_out_authority() -> tuple[dict[str, object], dict[str, object]]:
        retrieval_config = {"mode": "hybrid"}
        retrieval_config_sha256 = _sha256_json(retrieval_config)
        bindings: dict[str, object] = {
            "validationAgentReportFileSha256": "validation-file",
            "validationAgentReportSha256": "validation-report",
            "retrievalReportFileSha256": "retrieval-file",
            "retrievalReportSha256": "retrieval-report",
            "sourcePreparedSha256": "prepared-file",
            "sourceAnswerCasesSha256": "answer-file",
            "sourceRetrievalReportSha256": "retrieval-file",
            "caseIds": ["validation-q1", "validation-q2"],
            "caseIdsSha256": _sha256_json(["validation-q1", "validation-q2"]),
            "caseSetSha256": "case-set",
            "answerCaseManifestSha256": "answer-manifest",
            "answerCaseSetSha256": "answer-set",
            "promptConfigSha256": "prompt-config",
            "runtimeContractSha256": "runtime-contract",
            "piRuntimeIdentitySha256": "pi-runtime",
            "modelRouteIdentitySha256": "model-route",
            "retrievalConfigSha256": retrieval_config_sha256,
        }
        promotion: dict[str, object] = {
            "schemaVersion": "paw.enterprise-rag-validation-promotion.v1",
            "decision": "keep",
            "state": "promoted",
            "heldOutObserved": False,
            "validationAgentReport": {
                "fileSha256": "validation-file",
                "reportSha256": "validation-report",
            },
            "retrievalReport": {
                "fileSha256": "retrieval-file",
                "reportSha256": "retrieval-report",
            },
            "bindings": bindings,
            "identity": {
                "piRuntime": {"identitySha256": "pi-runtime"},
                "modelRoute": {"identitySha256": "model-route"},
            },
            "winner": {
                "reportSha256": "retrieval-report",
                "retrievalConfig": retrieval_config,
                "retrievalConfigSha256": retrieval_config_sha256,
            },
        }
        promotion["promotionReceiptSha256"] = _sha256_json(promotion)
        gate: dict[str, object] = {
            "schemaVersion": "paw.enterprise-rag-heldout-gate.v1",
            "state": "unlocked",
            "heldOutObserved": False,
            "maximumEvaluations": 1,
            "consumedEvaluations": 0,
            "promotionReceiptSha256": promotion["promotionReceiptSha256"],
            **bindings,
        }
        gate["gateReceiptSha256"] = _sha256_json(gate)
        return promotion, gate

    @staticmethod
    def _checkpoint_fingerprint(
        *,
        split: str = "validation",
        prompt_sha256: str = "prompt-v1",
    ) -> dict[str, object]:
        return _lane_checkpoint_fingerprint(
            source_prepared_sha256="prepared-v1",
            source_answer_cases_sha256="answers-v1",
            source_retrieval_report_sha256="retrieval-report-v1",
            evaluation_mode="answer-only",
            evaluation_split=split,
            case_ids_sha256="cases-v1",
            case_set_sha256="case-set-v1",
            answer_case_manifest_sha256="answer-manifest-v1",
            prompt_config_sha256=prompt_sha256,
            lane_prompt_sha256_by_lane={
                lane: f"{lane}-prompt-v1"
                for lane in ("baseline", "skill", "tuned", "agentic")
            },
            skill_sha256="skill-v1",
            runtime_contract_sha256="runtime-contract-v1",
            default_retrieval_config_sha256="default-config-v1",
            tuned_retrieval_config_sha256="tuned-config-v1",
            model_route_identity_sha256="model-routes-v1",
            pi_runtime_identity_sha256="pi-runtime-v1",
            maximum_attempts_per_lane=2,
        )

    @staticmethod
    def _checkpoint_lane_record(
        lane: str,
        *,
        terminal: str = "turn_completed",
        runtime_failure_category: str = "",
        tool_contract: bool = True,
        output_protocol_rate: float = 1.0,
    ) -> dict[str, object]:
        return {
            "lane": lane,
            "promptSha256": f"{lane}-prompt-v1",
            "terminalEvent": terminal,
            "runtimeFailureCategory": runtime_failure_category,
            "error": "",
            "toolContract": tool_contract,
            "scopeBoundary": True,
            "bindingCleanup": True,
            "binding": {"sessionId": f"agent:{lane}"},
            "gatewayLedger": {"itemCount": 5},
            "score": {
                "protocolErrors": [] if output_protocol_rate == 1.0 else ["invalid"],
                "failedToolItemCount": 0,
                "hardEvidence": {
                    "parameterBounded": True,
                    "agenticLoopObserved": lane == "agentic",
                },
                "agentMetrics": {"outputProtocolRate": output_protocol_rate},
                "answerCases": [],
            },
            "_assistantText": '{"cases":[]}',
            "_checkpointSessionId": f"agent:{lane}",
            "_checkpointTurnId": f"turn:{lane}",
        }

    @staticmethod
    def _bind_checkpoint_attempt(
        path: object,
        checkpoint: dict[str, object],
        *,
        lane: str,
        attempt: int,
    ) -> dict[str, object]:
        checkpoint = _append_lane_checkpoint_attempt_binding(
            path,
            checkpoint=checkpoint,
            lane=lane,
            attempt=attempt,
            session_id=f"agent:{lane}",
            turn_id="",
        )
        return _append_lane_checkpoint_attempt_binding(
            path,
            checkpoint=checkpoint,
            lane=lane,
            attempt=attempt,
            session_id=f"agent:{lane}",
            turn_id=f"turn:{lane}",
        )

    def test_lane_checkpoint_is_persisted_before_next_lane(self) -> None:
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "validation.checkpoint.json"
            checkpoint = _open_lane_checkpoint(
                path,
                fingerprint=self._checkpoint_fingerprint(),
                resume=False,
            )
            checkpoint = _append_lane_checkpoint_attempt_started(
                path, checkpoint=checkpoint, lane="baseline", attempt=1
            )
            checkpoint = self._bind_checkpoint_attempt(
                path, checkpoint, lane="baseline", attempt=1
            )
            checkpoint = _append_lane_checkpoint_attempt(
                path,
                checkpoint=checkpoint,
                lane="baseline",
                attempt=1,
                lane_record=self._checkpoint_lane_record("baseline"),
            )
            first_record_sha256 = checkpoint["attempts"][0]["attemptSha256"]
            persisted_after_first = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(1, len(persisted_after_first["attempts"]))

            checkpoint = _append_lane_checkpoint_attempt_started(
                path, checkpoint=checkpoint, lane="skill", attempt=1
            )
            checkpoint = self._bind_checkpoint_attempt(
                path, checkpoint, lane="skill", attempt=1
            )
            checkpoint = _append_lane_checkpoint_attempt(
                path,
                checkpoint=checkpoint,
                lane="skill",
                attempt=1,
                lane_record=self._checkpoint_lane_record("skill"),
            )
            persisted_after_second = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(2, len(persisted_after_second["attempts"]))
        self.assertEqual(
            first_record_sha256,
            persisted_after_second["attempts"][0]["attemptSha256"],
        )
        self.assertEqual(8, persisted_after_second["revision"])

    def test_started_without_terminal_is_interrupted_and_consumes_attempt_budget(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "validation.checkpoint.json"
            checkpoint = _open_lane_checkpoint(
                path,
                fingerprint=self._checkpoint_fingerprint(),
                resume=False,
            )
            checkpoint = _append_lane_checkpoint_attempt_started(
                path,
                checkpoint=checkpoint,
                lane="baseline",
                attempt=1,
            )
            resumed = _open_lane_checkpoint(
                path,
                fingerprint=self._checkpoint_fingerprint(),
                resume=True,
            )

        history = _lane_checkpoint_attempt_history(resumed, "baseline")
        self.assertEqual(1, len(history))
        self.assertEqual(1, history[0]["attempt"])
        self.assertEqual("interrupted", history[0]["lifecycleState"])
        self.assertEqual("interrupted", history[0]["runtimeFailureCategory"])
        self.assertFalse(history[0]["reusable"])
        self.assertEqual({}, _lane_checkpoint_reusable_records(resumed))

    def test_started_only_resume_attempt_is_historical_not_fresh(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "validation.checkpoint.json"
            checkpoint = _open_lane_checkpoint(
                path,
                fingerprint=self._checkpoint_fingerprint(),
                resume=False,
            )
            checkpoint = _append_lane_checkpoint_attempt_started(
                path, checkpoint=checkpoint, lane="baseline", attempt=1
            )
            resumed = _open_lane_checkpoint(
                path,
                fingerprint=self._checkpoint_fingerprint(),
                resume=True,
            )
            historical_count = len(
                _lane_checkpoint_attempt_history(resumed, "baseline")
            )
            resumed = _append_lane_checkpoint_attempt_started(
                path, checkpoint=resumed, lane="baseline", attempt=2
            )
            projection = _lane_checkpoint_report_projection(
                resumed,
                resume_requested=True,
                initial_attempt_count=historical_count,
                reused_lanes=set(),
                fresh_lanes={"baseline"},
            )

        self.assertEqual(1, projection["historicalAttemptCount"])
        self.assertEqual(1, projection["freshAttemptCount"])
        self.assertEqual(
            ["checkpoint", "fresh"],
            [item["origin"] for item in projection["attemptHistory"]],
        )

    def test_interrupted_attempt_uses_durable_session_and_turn_binding(self) -> None:
        import hashlib
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "validation.checkpoint.json"
            checkpoint = _open_lane_checkpoint(
                path,
                fingerprint=self._checkpoint_fingerprint(),
                resume=False,
            )
            checkpoint = _append_lane_checkpoint_attempt_started(
                path,
                checkpoint=checkpoint,
                lane="baseline",
                attempt=1,
            )
            checkpoint = _append_lane_checkpoint_attempt_binding(
                path,
                checkpoint=checkpoint,
                lane="baseline",
                attempt=1,
                session_id="agent:durable-session",
                turn_id="",
            )
            checkpoint = _append_lane_checkpoint_attempt_binding(
                path,
                checkpoint=checkpoint,
                lane="baseline",
                attempt=1,
                session_id="agent:durable-session",
                turn_id="turn:durable-turn",
            )
            resumed = _open_lane_checkpoint(
                path,
                fingerprint=self._checkpoint_fingerprint(),
                resume=True,
            )

        history = _lane_checkpoint_attempt_history(resumed, "baseline")
        self.assertEqual("interrupted", history[0]["lifecycleState"])
        self.assertEqual(
            hashlib.sha256(b"agent:durable-session").hexdigest(),
            history[0]["sessionSha256"],
        )
        self.assertEqual(
            hashlib.sha256(b"turn:durable-turn").hexdigest(),
            history[0]["turnSha256"],
        )
        self.assertTrue(history[0]["bindingReceiptSha256"])

    def test_orphan_recovery_cleans_sandbox_but_fails_closed_without_session_owner(self) -> None:
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "validation.checkpoint.json"
            checkpoint = _open_lane_checkpoint(
                path,
                fingerprint=self._checkpoint_fingerprint(),
                resume=False,
            )
            checkpoint = _append_lane_checkpoint_attempt_started(
                path,
                checkpoint=checkpoint,
                lane="baseline",
                attempt=1,
            )
            checkpoint = _append_lane_checkpoint_attempt_binding(
                path,
                checkpoint=checkpoint,
                lane="baseline",
                attempt=1,
                session_id="agent:orphan-session",
                turn_id="",
                recovery_locator={
                    "schemaVersion": "rag-ime.rag-agent-orphan-locator.v1",
                    "runRoot": str(root / "old run"),
                    "agentDbPath": str(root / "old run" / "agent.sqlite"),
                    "sessionId": "agent:orphan-session",
                    "turnId": "",
                    "sandboxRoot": str(root / "old run" / "knowledge-runs"),
                    "sandboxOwnerId": "ablation:owner",
                    "sandboxRunId": "a" * 32,
                },
            )
            checkpoint = _append_lane_checkpoint_attempt_binding(
                path,
                checkpoint=checkpoint,
                lane="baseline",
                attempt=1,
                session_id="agent:orphan-session",
                turn_id="turn:orphan-turn",
                recovery_locator={
                    "schemaVersion": "rag-ime.rag-agent-orphan-locator.v1",
                    "runRoot": str(root / "old run"),
                    "agentDbPath": str(root / "old run" / "agent.sqlite"),
                    "sessionId": "agent:orphan-session",
                    "turnId": "turn:orphan-turn",
                    "sandboxRoot": str(root / "old run" / "knowledge-runs"),
                    "sandboxOwnerId": "ablation:owner",
                    "sandboxRunId": "a" * 32,
                },
            )
            recovered, summary = _recover_lane_checkpoint_orphans(
                path,
                checkpoint=checkpoint,
                session_recover=None,
                sandbox_cleanup=lambda locator: {
                    "deleted": True,
                    "runId": locator["sandboxRunId"],
                },
            )

        self.assertTrue(summary["failClosed"])
        self.assertEqual(1, summary["orphanCount"])
        receipt = recovered["orphanRecoveryReceipts"][0]
        self.assertEqual("blocked", receipt["status"])
        self.assertEqual("recovered", receipt["sandboxRecovery"]["status"])
        self.assertEqual("blocked", receipt["sessionRecovery"]["status"])
        projection = _lane_checkpoint_report_projection(
            recovered,
            resume_requested=True,
            initial_attempt_count=1,
            reused_lanes=set(),
            fresh_lanes=set(),
        )
        serialized = json.dumps(projection, ensure_ascii=False, sort_keys=True)
        self.assertNotIn(str(root), serialized)
        self.assertNotIn("agent:orphan-session", serialized)

    def test_legacy_interrupted_binding_without_locator_fails_closed(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "validation.checkpoint.json"
            checkpoint = _open_lane_checkpoint(
                path,
                fingerprint=self._checkpoint_fingerprint(),
                resume=False,
            )
            checkpoint = _append_lane_checkpoint_attempt_started(
                path,
                checkpoint=checkpoint,
                lane="baseline",
                attempt=1,
            )
            checkpoint = _append_lane_checkpoint_attempt_binding(
                path,
                checkpoint=checkpoint,
                lane="baseline",
                attempt=1,
                session_id="agent:legacy-session",
                turn_id="",
            )
            recovered, summary = _recover_lane_checkpoint_orphans(
                path,
                checkpoint=checkpoint,
                session_recover=lambda _locator: self.fail(
                    "missing private locator must not invoke Session recovery"
                ),
                sandbox_cleanup=lambda _locator: self.fail(
                    "missing private locator must not invoke sandbox cleanup"
                ),
            )

        self.assertTrue(summary["failClosed"])
        receipt = recovered["orphanRecoveryReceipts"][0]
        self.assertFalse(receipt["recoveryLocatorPresent"])
        self.assertEqual("blocked", receipt["status"])
        self.assertEqual(
            "recovery_locator_missing",
            receipt["sessionRecovery"]["reasonCode"],
        )
        self.assertEqual(
            "recovery_locator_missing",
            receipt["sandboxRecovery"]["reasonCode"],
        )

    def test_orphan_recovery_faults_exact_private_session_and_cleans_real_sandbox(self) -> None:
        import hashlib
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from rag_ime.agent_sessions import AgentSessionStore
        from rag_ime.rag_benchmark_sandbox import RagBenchmarkSandbox

        with TemporaryDirectory() as temporary:
            private_root = Path(temporary) / "private-evaluation"
            run_root = private_root / "run-orphan"
            run_root.mkdir(parents=True)
            store = AgentSessionStore(run_root / "agent.sqlite")
            store.initialize()
            session = store.create(
                title="RAG Agent ablation: baseline",
                tool_profile_version="subagent-readonly-v1",
                execution_mode="read_only",
                project_context_enabled=False,
                workspace_roots=[],
            )
            session_id = str(session["id"])
            turn_id = "turn:orphan-private"
            store.set_status(session_id, "busy")
            store.record_runtime_event(
                event_id="event:orphan-turn-started",
                session_id=session_id,
                turn_id=turn_id,
                sequence=1,
                event_type="turn_started",
                created_at_ms=100,
                redacted_summary="started",
            )
            sandbox = RagBenchmarkSandbox(run_root / "knowledge-runs")
            owner = "ablation:private-owner"
            sandbox_run = sandbox.create_run(owner, label="orphan")
            sandbox_run_id = str(sandbox_run["runId"])
            checkpoint_path = private_root / "validation.checkpoint.json"
            checkpoint = _open_lane_checkpoint(
                checkpoint_path,
                fingerprint=self._checkpoint_fingerprint(),
                resume=False,
            )
            checkpoint = _append_lane_checkpoint_attempt_started(
                checkpoint_path,
                checkpoint=checkpoint,
                lane="baseline",
                attempt=1,
            )
            locator = {
                "schemaVersion": "rag-ime.rag-agent-orphan-locator.v1",
                "runRoot": str(run_root),
                "agentDbPath": str(run_root / "agent.sqlite"),
                "sessionId": session_id,
                "turnId": turn_id,
                "sandboxRoot": str(run_root / "knowledge-runs"),
                "sandboxOwnerId": owner,
                "sandboxRunId": sandbox_run_id,
            }
            checkpoint = _append_lane_checkpoint_attempt_binding(
                checkpoint_path,
                checkpoint=checkpoint,
                lane="baseline",
                attempt=1,
                session_id=session_id,
                turn_id="",
                recovery_locator={**locator, "turnId": ""},
            )
            checkpoint = _append_lane_checkpoint_attempt_binding(
                checkpoint_path,
                checkpoint=checkpoint,
                lane="baseline",
                attempt=1,
                session_id=session_id,
                turn_id=turn_id,
                recovery_locator=locator,
            )
            recovered, summary = _recover_lane_checkpoint_orphans(
                checkpoint_path,
                checkpoint=checkpoint,
                session_recover=lambda value: _recover_private_evaluation_session(
                    value,
                    allowed_private_root=private_root,
                    expected_session_sha256=hashlib.sha256(
                        session_id.encode("utf-8")
                    ).hexdigest(),
                    expected_turn_sha256=hashlib.sha256(
                        turn_id.encode("utf-8")
                    ).hexdigest(),
                ),
                sandbox_cleanup=lambda value: sandbox.cleanup(
                    value["sandboxOwnerId"],
                    value["sandboxRunId"],
                    confirm_text="DELETE_BENCHMARK_RUN",
                ),
            )
            settled = store.get(session_id)
            terminal = store.runtime_turn_terminal_event(session_id, turn_id)
            sandbox.close()

        self.assertFalse(summary["failClosed"])
        self.assertEqual("faulted", settled["status"])
        self.assertIsNotNone(terminal)
        assert terminal is not None
        self.assertEqual("turn_failed", terminal["eventType"])
        receipt = recovered["orphanRecoveryReceipts"][0]
        self.assertEqual("recovered", receipt["status"])
        self.assertEqual(
            "exact_session_terminal_verified",
            receipt["sessionRecovery"]["reasonCode"],
        )
        self.assertTrue(receipt["sessionRecovery"]["evidenceSha256"])
        self.assertEqual(
            "marker_bound_sandbox_deleted",
            receipt["sandboxRecovery"]["reasonCode"],
        )

    def test_private_session_recovery_rejects_forged_locator_outside_private_root(self) -> None:
        import hashlib
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from rag_ime.agent_sessions import AgentSessionStore

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            allowed = root / "private-evaluation"
            allowed.mkdir()
            forged_run = root / "production-like" / "run-forged"
            forged_run.mkdir(parents=True)
            store = AgentSessionStore(forged_run / "agent.sqlite")
            store.initialize()
            session = store.create(
                title="RAG Agent ablation: baseline",
                tool_profile_version="subagent-readonly-v1",
            )
            session_id = str(session["id"])
            turn_id = "turn:forged"
            store.set_status(session_id, "busy")
            locator = {
                "schemaVersion": "rag-ime.rag-agent-orphan-locator.v1",
                "runRoot": str(forged_run),
                "agentDbPath": str(forged_run / "agent.sqlite"),
                "sessionId": session_id,
                "turnId": turn_id,
                "sandboxRoot": str(forged_run / "knowledge-runs"),
                "sandboxOwnerId": "ablation:owner",
                "sandboxRunId": "a" * 32,
            }
            with self.assertRaisesRegex(ValueError, "private evaluation root"):
                _recover_private_evaluation_session(
                    locator,
                    allowed_private_root=allowed,
                    expected_session_sha256=hashlib.sha256(
                        session_id.encode("utf-8")
                    ).hexdigest(),
                    expected_turn_sha256=hashlib.sha256(
                        turn_id.encode("utf-8")
                    ).hexdigest(),
                )

            self.assertEqual("busy", store.get(session_id)["status"])

    def test_private_session_recovery_faults_session_created_before_turn_acceptance(self) -> None:
        import hashlib
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from rag_ime.agent_sessions import AgentSessionStore

        with TemporaryDirectory() as temporary:
            private_root = Path(temporary) / "private-evaluation"
            run_root = private_root / "run-pre-turn"
            run_root.mkdir(parents=True)
            store = AgentSessionStore(run_root / "agent.sqlite")
            store.initialize()
            session = store.create(
                title="RAG Agent ablation: skill",
                tool_profile_version="subagent-readonly-v1",
                execution_mode="read_only",
                project_context_enabled=False,
                workspace_roots=[],
            )
            session_id = str(session["id"])
            store.set_status(session_id, "busy")
            locator = {
                "schemaVersion": "rag-ime.rag-agent-orphan-locator.v1",
                "runRoot": str(run_root),
                "agentDbPath": str(run_root / "agent.sqlite"),
                "sessionId": session_id,
                "turnId": "",
                "sandboxRoot": str(run_root / "knowledge-runs"),
                "sandboxOwnerId": "ablation:owner",
                "sandboxRunId": "b" * 32,
            }

            recovered = _recover_private_evaluation_session(
                locator,
                allowed_private_root=private_root,
                expected_session_sha256=hashlib.sha256(
                    session_id.encode("utf-8")
                ).hexdigest(),
                expected_turn_sha256="",
            )

            self.assertTrue(recovered["recovered"])
            self.assertTrue(recovered["terminal"])
            self.assertEqual("", recovered["turnSha256"])
            self.assertEqual("session_recovery_faulted", recovered["terminalEventType"])
            self.assertEqual("faulted", store.get(session_id)["status"])
            self.assertEqual(1, store.max_event_sequence(session_id))

    def test_private_session_recovery_faults_busy_row_with_existing_terminal(self) -> None:
        import hashlib
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from rag_ime.agent_sessions import AgentSessionStore

        with TemporaryDirectory() as temporary:
            private_root = Path(temporary) / "private-evaluation"
            run_root = private_root / "run-terminal"
            run_root.mkdir(parents=True)
            store = AgentSessionStore(run_root / "agent.sqlite")
            store.initialize()
            session = store.create(
                title="RAG Agent ablation: tuned",
                tool_profile_version="subagent-readonly-v1",
                execution_mode="read_only",
                project_context_enabled=False,
                workspace_roots=[],
            )
            session_id = str(session["id"])
            turn_id = "turn:already-failed"
            store.set_status(session_id, "busy")
            store.record_runtime_event(
                event_id="event:already-failed",
                session_id=session_id,
                turn_id=turn_id,
                sequence=1,
                event_type="turn_failed",
                created_at_ms=100,
                redacted_summary="provider failed before hard kill",
            )
            locator = {
                "schemaVersion": "rag-ime.rag-agent-orphan-locator.v1",
                "runRoot": str(run_root),
                "agentDbPath": str(run_root / "agent.sqlite"),
                "sessionId": session_id,
                "turnId": turn_id,
                "sandboxRoot": str(run_root / "knowledge-runs"),
                "sandboxOwnerId": "ablation:owner",
                "sandboxRunId": "c" * 32,
            }

            recovered = _recover_private_evaluation_session(
                locator,
                allowed_private_root=private_root,
                expected_session_sha256=hashlib.sha256(
                    session_id.encode("utf-8")
                ).hexdigest(),
                expected_turn_sha256=hashlib.sha256(
                    turn_id.encode("utf-8")
                ).hexdigest(),
            )

            self.assertTrue(recovered["terminal"])
            self.assertEqual("turn_failed", recovered["terminalEventType"])
            self.assertEqual("faulted", store.get(session_id)["status"])
            self.assertEqual(1, store.max_event_sequence(session_id))

    def test_resume_skips_only_matching_terminal_complete_lanes(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory

        fingerprint = self._checkpoint_fingerprint()
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "validation.checkpoint.json"
            checkpoint = _open_lane_checkpoint(
                path,
                fingerprint=fingerprint,
                resume=False,
            )
            checkpoint = _append_lane_checkpoint_attempt_started(
                path, checkpoint=checkpoint, lane="baseline", attempt=1
            )
            checkpoint = self._bind_checkpoint_attempt(
                path, checkpoint, lane="baseline", attempt=1
            )
            checkpoint = _append_lane_checkpoint_attempt(
                path,
                checkpoint=checkpoint,
                lane="baseline",
                attempt=1,
                lane_record=self._checkpoint_lane_record("baseline"),
            )
            checkpoint = _append_lane_checkpoint_attempt_started(
                path, checkpoint=checkpoint, lane="skill", attempt=1
            )
            checkpoint = self._bind_checkpoint_attempt(
                path, checkpoint, lane="skill", attempt=1
            )
            checkpoint = _append_lane_checkpoint_attempt(
                path,
                checkpoint=checkpoint,
                lane="skill",
                attempt=1,
                lane_record=self._checkpoint_lane_record(
                    "skill",
                    terminal="turn_failed",
                    runtime_failure_category="provider_transient_after_tool",
                ),
            )
            resumed = _open_lane_checkpoint(
                path,
                fingerprint=fingerprint,
                resume=True,
            )

        reusable = _lane_checkpoint_reusable_records(resumed)
        self.assertEqual({"baseline"}, set(reusable))
        self.assertEqual("turn_completed", reusable["baseline"]["terminalEvent"])

    def test_failed_after_gateway_items_restarts_in_fresh_attempt_within_budget(self) -> None:
        from unittest.mock import patch

        failed = self._checkpoint_lane_record(
            "tuned",
            terminal="turn_failed",
            runtime_failure_category="provider_transient_after_tool",
        )
        completed = self._checkpoint_lane_record("tuned")
        observed: list[tuple[int, str]] = []
        lifecycle_order: list[tuple[str, int]] = []

        def run_once(*args: object, **kwargs: object) -> dict[str, object]:
            attempt = 1 + sum(kind == "run" for kind, _ in lifecycle_order)
            lifecycle_order.append(("run", attempt))
            binding_observer = kwargs["attempt_binding_observer"]
            binding_observer(f"agent:fresh:{attempt}", "")
            binding_observer(f"agent:fresh:{attempt}", f"turn:fresh:{attempt}")
            return failed if attempt == 1 else completed

        with patch(
            "scripts.run_rag_agent_ablation._run_lane_once",
            side_effect=run_once,
        ) as run_once:
            result = _run_lane(
                object(),
                gateway=object(),
                owner="owner",
                run_id="a" * 32,
                lane="tuned",
                cases=[],
                retrieval_config={},
                retrieval_config_sha256="config-v1",
                timeout_seconds=60,
                lane_attempts=2,
                attempt_start_observer=lambda attempt: lifecycle_order.append(
                    ("started", attempt)
                ),
                attempt_binding_observer=(
                    lambda attempt, session_id, turn_id: lifecycle_order.append(
                        ("turn_bound" if turn_id else "session_bound", attempt)
                    )
                ),
                attempt_observer=lambda attempt, record: (
                    observed.append((attempt, str(record["runtimeFailureCategory"]))),
                    lifecycle_order.append(("terminal", attempt)),
                ),
            )

        self.assertEqual(2, run_once.call_count)
        self.assertEqual(
            [(1, "provider_transient_after_tool"), (2, "")],
            observed,
        )
        self.assertEqual("turn_completed", result["terminalEvent"])
        self.assertEqual(1, result["runtimeRetryCount"])
        self.assertEqual(
            [
                ("started", 1),
                ("run", 1),
                ("session_bound", 1),
                ("turn_bound", 1),
                ("terminal", 1),
                ("started", 2),
                ("run", 2),
                ("session_bound", 2),
                ("turn_bound", 2),
                ("terminal", 2),
            ],
            lifecycle_order,
        )

    def test_completed_turn_with_failed_benchmark_tool_is_harness_failure(self) -> None:
        events = [
            {
                "eventType": "tool_finished",
                "payload": {
                    "toolName": "rag_benchmark",
                    "toolCallId": "failed-call",
                    "isError": True,
                    "result": {
                        "content": [{"type": "text", "text": "fetch failed"}],
                        "details": {},
                    },
                },
            }
        ]

        self.assertEqual(1, _runtime_tool_failure_count(events))
        self.assertEqual(
            "harness_tool_transport_failure",
            _runtime_failure_category(
                terminal="turn_completed",
                error="",
                events=events,
                ledger={"itemCount": 1},
            ),
        )

    def test_transport_receipt_fails_lane_closed_without_retry(self) -> None:
        from unittest.mock import patch

        class FailedTransport:
            transport = "loopback-http-v1"

            def transport_cursor(self) -> int:
                return 7

            def transport_receipt(self, *, since_sequence: int) -> dict[str, object]:
                self.since_sequence = since_sequence
                return {
                    "schemaVersion": "rag-ime.rag-benchmark-tool-transport.v1",
                    "transport": self.transport,
                    "accepted": False,
                    "failureCount": 1,
                    "failureTypes": ["BrokenPipeError"],
                    "failures": [{"sequence": 8}],
                    "receiptSha256": "f" * 64,
                }

        transport = FailedTransport()
        completed = self._checkpoint_lane_record("baseline")
        with patch(
            "scripts.run_rag_agent_ablation._run_lane_once",
            return_value=completed,
        ) as run_once:
            result = _run_lane(
                object(),
                gateway=object(),
                tool_transport=transport,
                owner="owner",
                run_id="a" * 32,
                lane="baseline",
                cases=[],
                retrieval_config={},
                retrieval_config_sha256="config-v1",
                timeout_seconds=60,
                lane_attempts=2,
            )

        self.assertEqual(1, run_once.call_count)
        self.assertEqual(7, transport.since_sequence)
        self.assertFalse(result["toolContract"])
        self.assertEqual("harness_transport_failure", result["runtimeFailureCategory"])
        self.assertFalse(result["toolTransport"]["accepted"])
        self.assertEqual(0, result["runtimeRetryCount"])

    def test_checkpoint_fingerprint_drift_is_rejected(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "validation.checkpoint.json"
            _open_lane_checkpoint(
                path,
                fingerprint=self._checkpoint_fingerprint(),
                resume=False,
            )
            with self.assertRaisesRegex(ValueError, "fingerprint"):
                _open_lane_checkpoint(
                    path,
                    fingerprint=self._checkpoint_fingerprint(prompt_sha256="prompt-v2"),
                    resume=True,
                )

        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "validation.checkpoint.json"
            fingerprint = self._checkpoint_fingerprint()
            _open_lane_checkpoint(path, fingerprint=fingerprint, resume=False)
            drifted = dict(fingerprint)
            drifted["lanePromptSha256ByLane"] = {
                **dict(fingerprint["lanePromptSha256ByLane"]),
                "agentic": "agentic-prompt-v2",
            }
            with self.assertRaisesRegex(ValueError, "fingerprint"):
                _open_lane_checkpoint(path, fingerprint=drifted, resume=True)

    def test_partial_or_non_terminal_checkpoint_lane_is_not_reusable(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "validation.checkpoint.json"
            checkpoint = _open_lane_checkpoint(
                path,
                fingerprint=self._checkpoint_fingerprint(),
                resume=False,
            )
            checkpoint = _append_lane_checkpoint_attempt_started(
                path, checkpoint=checkpoint, lane="baseline", attempt=1
            )
            checkpoint = self._bind_checkpoint_attempt(
                path, checkpoint, lane="baseline", attempt=1
            )
            checkpoint = _append_lane_checkpoint_attempt(
                path,
                checkpoint=checkpoint,
                lane="baseline",
                attempt=1,
                lane_record=self._checkpoint_lane_record("baseline", terminal=""),
            )
            checkpoint = _append_lane_checkpoint_attempt_started(
                path, checkpoint=checkpoint, lane="skill", attempt=1
            )
            checkpoint = self._bind_checkpoint_attempt(
                path, checkpoint, lane="skill", attempt=1
            )
            checkpoint = _append_lane_checkpoint_attempt(
                path,
                checkpoint=checkpoint,
                lane="skill",
                attempt=1,
                lane_record=self._checkpoint_lane_record(
                    "skill",
                    tool_contract=False,
                ),
            )

        self.assertEqual({}, _lane_checkpoint_reusable_records(checkpoint))

    def test_validation_checkpoint_cannot_bypass_held_out_gate(self) -> None:
        from pathlib import Path

        with self.assertRaisesRegex(ValueError, "validation-only"):
            _validate_checkpoint_request(
                evaluation_split="held_out",
                checkpoint_path=Path("checkpoint.json"),
                resume_checkpoint=False,
            )

    def test_public_report_projects_checkpoint_counts_and_redacts_private_values(self) -> None:
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "validation.checkpoint.json"
            checkpoint = _open_lane_checkpoint(
                path,
                fingerprint=self._checkpoint_fingerprint(),
                resume=False,
            )
            checkpoint = _append_lane_checkpoint_attempt_started(
                path, checkpoint=checkpoint, lane="baseline", attempt=1
            )
            checkpoint = self._bind_checkpoint_attempt(
                path, checkpoint, lane="baseline", attempt=1
            )
            checkpoint = _append_lane_checkpoint_attempt(
                path,
                checkpoint=checkpoint,
                lane="baseline",
                attempt=1,
                lane_record=self._checkpoint_lane_record(
                    "baseline",
                    terminal="turn_failed",
                    runtime_failure_category="provider_transient_after_tool",
                ),
            )
            checkpoint = _append_lane_checkpoint_attempt_started(
                path, checkpoint=checkpoint, lane="baseline", attempt=2
            )
            checkpoint = self._bind_checkpoint_attempt(
                path, checkpoint, lane="baseline", attempt=2
            )
            checkpoint = _append_lane_checkpoint_attempt(
                path,
                checkpoint=checkpoint,
                lane="baseline",
                attempt=2,
                lane_record=self._checkpoint_lane_record("baseline"),
            )
            initial_attempt_count = len(
                _lane_checkpoint_attempt_history(checkpoint, "baseline")
            )
            checkpoint = _append_lane_checkpoint_attempt_started(
                path, checkpoint=checkpoint, lane="skill", attempt=1
            )
            checkpoint = self._bind_checkpoint_attempt(
                path, checkpoint, lane="skill", attempt=1
            )
            checkpoint = _append_lane_checkpoint_attempt(
                path,
                checkpoint=checkpoint,
                lane="skill",
                attempt=1,
                lane_record=self._checkpoint_lane_record("skill"),
            )

        projection = _lane_checkpoint_report_projection(
            checkpoint,
            resume_requested=True,
            initial_attempt_count=initial_attempt_count,
            reused_lanes={"baseline"},
            fresh_lanes={"skill"},
        )
        report = _finalize_public_report(
            {
                "schemaVersion": "fixture.v1",
                "formalAcceptanceEligible": False,
                "formalAcceptancePassed": False,
                "evaluation": {"split": "validation"},
                "lanes": [
                    {
                        "lane": "skill",
                        "binding": {"sessionId": "agent:private-session"},
                        "_checkpointTurnId": "turn:private-turn",
                        "_assistantText": "private assistant output",
                        "runtimeFailureCategory": "provider_runtime_failure",
                        "error": "failed under /Volumes/undo 4t/git/private run/file.json",
                    }
                ],
                "answerJudge": {
                    "assistantOutputs": ["private judge output"],
                    "assistantOutputSha256s": ["already-hashed"],
                },
            },
            checkpoint=projection,
        )
        serialized = json.dumps(report, ensure_ascii=False, sort_keys=True)

        self.assertEqual(3, report["checkpoint"]["attemptCount"])
        self.assertEqual(2, report["checkpoint"]["completedLaneCount"])
        self.assertEqual(1, report["checkpoint"]["reusedLaneCount"])
        self.assertEqual(1, report["checkpoint"]["freshLaneCount"])
        self.assertEqual(1, report["checkpoint"]["retriedLaneCount"])
        self.assertEqual(3, len(report["checkpoint"]["attemptHistory"]))
        self.assertFalse(report["formalAcceptanceEligible"])
        self.assertNotIn("agent:private-session", serialized)
        self.assertNotIn("turn:private-turn", serialized)
        self.assertNotIn("private assistant output", serialized)
        self.assertNotIn("private judge output", serialized)
        self.assertNotIn("/Volumes/undo", serialized)
        self.assertNotIn("4t/git/private", serialized)
        self.assertNotIn('"error":', serialized)
        self.assertIn("errorSha256", serialized)
        self.assertIn("provider_runtime_failure", serialized)
        self.assertIn("sessionSha256", serialized)
        self.assertIn("turnSha256", serialized)
        self.assertTrue(report["reportSha256"])

    def test_public_path_redaction_preserves_urls_and_hashes_complete_paths(self) -> None:
        https_url = "https://github.com/onyx-dot-app/EnterpriseRAG-Bench"
        absolute_with_space = "/Volumes/undo 4t/git/private run/result.json"
        inline_path = "failed at /tmp/private-run/result.json before completion"
        inline_path_with_space = (
            "failed at /Volumes/undo 4t/git/private run/result.json before completion"
        )
        file_url = "file:///Volumes/undo%204t/private/result.json"

        self.assertEqual(https_url, _redact_absolute_paths(https_url))
        redacted_absolute = _redact_absolute_paths(absolute_with_space)
        self.assertRegex(redacted_absolute, r"^\[path-sha256:[a-f0-9]{64}\]$")
        self.assertEqual(1, redacted_absolute.count("path-sha256"))
        self.assertNotIn("/Volumes/undo", redacted_absolute)
        redacted_inline = _redact_absolute_paths(inline_path)
        self.assertIn("failed at [path-sha256:", redacted_inline)
        self.assertNotIn("/tmp/private-run/result.json", redacted_inline)
        redacted_inline_with_space = _redact_absolute_paths(inline_path_with_space)
        self.assertNotIn("/Volumes/undo", redacted_inline_with_space)
        self.assertNotIn("4t/git/private", redacted_inline_with_space)
        redacted_file_url = _redact_absolute_paths(file_url)
        self.assertNotIn("/Volumes/undo%204t/private/result.json", redacted_file_url)
        self.assertIn("path-sha256", redacted_file_url)
        self.assertEqual("ratio 1/2 remains", _redact_absolute_paths("ratio 1/2 remains"))

    def test_host_answer_evidence_qrels_never_enter_public_report(self) -> None:
        import json

        secret_quote = "private source quote that must remain host-only"
        secret_document_id = "private-document-id"
        report = _finalize_public_report(
            {
                "schemaVersion": "fixture.v1",
                "formalAcceptanceEligible": False,
                "evaluation": {"split": "validation"},
                "answerCaseManifest": {
                    "schemaVersion": "rag-ime.rag-answer-case-manifest.v2",
                    "evidenceManifestSha256": "e" * 64,
                    "highLevelFactCount": 2,
                    "highLevelEvidenceAvailabilityPassed": True,
                    "_privateEvidenceQrels": {
                        "q-high": {
                            "facts": [
                                {
                                    "factId": "F1",
                                    "supportGroups": [
                                        {
                                            "documentIds": [secret_document_id],
                                            "quotes": [secret_quote],
                                        }
                                    ],
                                }
                            ]
                        }
                    },
                },
            },
            checkpoint=None,
        )
        serialized = json.dumps(report, ensure_ascii=False, sort_keys=True)

        self.assertNotIn(secret_quote, serialized)
        self.assertNotIn(secret_document_id, serialized)
        self.assertNotIn("_privateEvidenceQrels", serialized)
        self.assertEqual(
            "e" * 64,
            report["answerCaseManifest"]["evidenceManifestSha256"],
        )

    def test_public_report_requires_top_level_runtime_identity_to_match_conditions(self) -> None:
        pi_runtime = {
            "schemaVersion": "rag-ime.rag-agent-pi-runtime-identity.v1",
            "identitySha256": "pi-runtime-v1",
        }
        agent_config = {
            "schemaVersion": "rag-ime.rag-evaluation-agent-config.v1",
            "identitySha256": "agent-config-v1",
        }
        report = {
            "schemaVersion": "fixture.v1",
            "conditions": {
                "piRuntime": pi_runtime,
                "agentConfig": agent_config,
            },
            "piRuntime": pi_runtime,
            "agentConfig": agent_config,
            "evaluation": {"split": "validation"},
        }

        finalized = _finalize_public_report(report, checkpoint=None)
        self.assertEqual(finalized["piRuntime"], finalized["conditions"]["piRuntime"])
        self.assertEqual(
            finalized["agentConfig"], finalized["conditions"]["agentConfig"]
        )

        report.pop("piRuntime")
        with self.assertRaisesRegex(RuntimeError, "Pi Runtime identity"):
            _finalize_public_report(report, checkpoint=None)

    def test_answer_only_defaults_to_validation(self) -> None:
        self.assertEqual("validation", _resolve_evaluation_split(None, answer_only=True))
        self.assertEqual("held_out", _resolve_evaluation_split(None, answer_only=False))

    def test_evaluation_agent_config_pins_sol_max_and_sse(self) -> None:
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temporary:
            agent_config = Path(temporary)
            (agent_config / "settings.json").write_text(
                json.dumps(
                    {
                        "defaultProvider": "openai-codex",
                        "defaultModel": "gpt-5.6-luna",
                        "defaultThinkingLevel": "medium",
                    }
                ),
                encoding="utf-8",
            )
            (agent_config / "auth.json").write_text(
                json.dumps({"openai-codex": {"type": "oauth"}}),
                encoding="utf-8",
            )
            receipt = _pin_evaluation_agent_config(agent_config)
            settings = json.loads(
                (agent_config / "settings.json").read_text(encoding="utf-8")
            )

        self.assertEqual("gpt-6.1-sol", settings["defaultModel"])
        self.assertEqual("max", settings["defaultThinkingLevel"])
        self.assertEqual("sse", settings["transport"])
        self.assertEqual("openai-codex/gpt-6.1-sol", receipt["model"])
        self.assertTrue(receipt["openaiCodexOnly"])
        self.assertTrue(receipt["settingsSha256"])

    def test_evaluation_config_omits_every_non_codex_provider_definition(self) -> None:
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            (source / "auth.json").write_text(
                json.dumps(
                    {
                        "openai-codex": {"type": "oauth", "secret": "private"},
                        "other-provider": {"type": "api_key", "secret": "forbidden"},
                    }
                ),
                encoding="utf-8",
            )
            (source / "settings.json").write_text(
                json.dumps({"packages": ["rag-retrieval-optimization"]}),
                encoding="utf-8",
            )
            (source / "models.json").write_text(
                json.dumps(
                    {
                        "providers": {
                            "gpt": {"baseUrl": "https://forbidden.invalid"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            (source / "models-store.json").write_text(
                json.dumps({"gpt": {"models": []}}),
                encoding="utf-8",
            )

            copied = _copy_openai_codex_agent_config(source, target)

            self.assertEqual(["auth.json", "settings.json"], copied)
            self.assertEqual(
                {"openai-codex"},
                set(json.loads((target / "auth.json").read_text())),
            )
            self.assertFalse((target / "models.json").exists())
            self.assertFalse((target / "models-store.json").exists())

    def test_evaluation_configuration_pins_parent_and_judge_max_but_bounds_reviewer_low(self) -> None:
        configuration = _evaluation_configuration_defaults()

        self.assertEqual(
            "openai-codex/gpt-6.1-sol",
            configuration["sessionDefaults"]["modelProfile"],
        )
        self.assertFalse(configuration["sessionDefaults"]["resumeLastSession"])
        self.assertEqual(
            {
                "primary",
                "traceDiagnostic",
                "toolAgent",
                "subagent",
                "roomCoordinator",
            },
            set(configuration["modelRouting"]),
        )
        for route_id, route in configuration["modelRouting"].items():
            self.assertEqual("openai-codex/gpt-6.1-sol", route["modelProfile"])
            self.assertEqual(
                "low" if route_id == "subagent" else "max",
                route["thinkingLevel"],
            )

    def test_answer_manifest_binds_suite_corpus_and_high_level_evidence(self) -> None:
        import hashlib

        documents = [
            {
                "documentId": "doc-runtime",
                "text": "The runtime uses continuous batching and prefix KV caching.",
            }
        ]
        prepared_cases = [
            {
                "queryId": "q-retrieval",
                "query": "What does the runtime use?",
                "split": "validation",
                "slice": "basic",
                "retrievalEvaluable": True,
                "relevant": {"doc-runtime": 1.0},
            }
        ]
        answer_cases = [
            dict(prepared_cases[0]),
            {
                "queryId": "q-high",
                "query": "Which runtime optimizations are used?",
                "split": "validation",
                "slice": "high_level",
                "retrievalEvaluable": False,
                "abstentionExpected": False,
                "goldAnswer": "Continuous batching and prefix KV caching.",
                "answerFacts": [
                    "The runtime uses continuous batching.",
                    "The runtime uses prefix KV caching.",
                ],
            },
        ]
        chunking = {
            "strategy": "general",
            "size": 1200,
            "overlap": 160,
            "separator": "",
            "respectHeadings": True,
            "respectPageBoundaries": True,
        }
        document_text = str(documents[0]["text"])
        evidence_qrels = {
            "schemaVersion": "rag-ime.rag-answer-evidence-qrels.v1",
            "evaluationSplit": "validation",
            "preparedSourceSha256": "dataset-v1",
            "chunkingConfigSha256": _sha256_json(chunking),
            "cases": [
                {
                    "queryId": "q-high",
                    "facts": [
                        {
                            "factId": "F1",
                            "factSha256": hashlib.sha256(
                                str(answer_cases[1]["answerFacts"][0]).encode("utf-8")
                            ).hexdigest(),
                            "availability": "verified",
                            "supportGroups": [
                                {
                                    "groupId": "G1",
                                    "evidence": [
                                        {
                                            "documentId": "doc-runtime",
                                            "documentSha256": hashlib.sha256(
                                                document_text.encode("utf-8")
                                            ).hexdigest(),
                                            "chunkOrdinal": 0,
                                            "chunkSha256": hashlib.sha256(
                                                document_text.encode("utf-8")
                                            ).hexdigest(),
                                            "quote": "The runtime uses continuous batching",
                                            "quoteSha256": hashlib.sha256(
                                                b"The runtime uses continuous batching"
                                            ).hexdigest(),
                                        }
                                    ],
                                }
                            ],
                        },
                        {
                            "factId": "F2",
                            "factSha256": hashlib.sha256(
                                str(answer_cases[1]["answerFacts"][1]).encode("utf-8")
                            ).hexdigest(),
                            "availability": "verified",
                            "supportGroups": [
                                {
                                    "groupId": "G1",
                                    "evidence": [
                                        {
                                            "documentId": "doc-runtime",
                                            "documentSha256": hashlib.sha256(
                                                document_text.encode("utf-8")
                                            ).hexdigest(),
                                            "chunkOrdinal": 0,
                                            "chunkSha256": hashlib.sha256(
                                                document_text.encode("utf-8")
                                            ).hexdigest(),
                                            "quote": "prefix KV caching",
                                            "quoteSha256": hashlib.sha256(
                                                b"prefix KV caching"
                                            ).hexdigest(),
                                        }
                                    ],
                                }
                            ],
                        },
                    ],
                }
            ],
        }
        evidence_qrels["manifestSha256"] = _sha256_json(evidence_qrels)

        manifest = _answer_case_manifest(
            prepared_cases=prepared_cases,
            answer_cases=answer_cases,
            selected_cases=[answer_cases[1]],
            documents=documents,
            benchmark_id="benchmark-v1",
            prepared_source_sha256="dataset-v1",
            evaluation_split="validation",
            chunking_config=chunking,
            answer_evidence_qrels=evidence_qrels,
        )

        self.assertTrue(manifest["suiteBindingPassed"])
        self.assertTrue(manifest["corpusBindingPassed"])
        self.assertTrue(manifest["highLevelEvidenceAvailabilityPassed"])
        self.assertEqual(2, manifest["highLevelFactCount"])
        self.assertEqual(evidence_qrels["manifestSha256"], manifest["evidenceManifestSha256"])
        self.assertNotIn("highLevelEvidenceAvailability", manifest)
        self.assertNotIn("answerEvidenceStandardManifestSha256", manifest)
        self.assertNotIn("unbiasedPromotionClaimAllowed", manifest)
        self.assertTrue(manifest["manifestSha256"])

        answer_cases[0]["query"] = "drifted"
        with self.assertRaisesRegex(ValueError, "does not match prepared suite"):
            _answer_case_manifest(
                prepared_cases=prepared_cases,
                answer_cases=answer_cases,
                selected_cases=[answer_cases[1]],
                documents=documents,
                benchmark_id="benchmark-v1",
                prepared_source_sha256="dataset-v1",
                evaluation_split="validation",
                chunking_config=chunking,
                answer_evidence_qrels=evidence_qrels,
            )

    def test_answer_evidence_qrels_v2_requires_and_preserves_support_group_mode(self) -> None:
        import hashlib

        fact_text = "Either verified alternative supports the required fact."
        documents = {
            "doc-a": "Alternative A directly supports the required fact.",
            "doc-b": "Alternative B directly supports the required fact.",
        }
        chunking = {
            "strategy": "general",
            "size": 1200,
            "overlap": 160,
            "separator": "",
            "respectHeadings": True,
            "respectPageBoundaries": True,
        }

        def qrels(mode: object = "any") -> dict[str, object]:
            standard = _test_answer_evidence_standard_v2(
                documents=documents,
                chunking=chunking,
                prepared_source_sha256="d" * 64,
                prepared_artifact_sha256="c" * 64,
            )
            value: dict[str, object] = {
                "schemaVersion": "rag-ime.rag-answer-evidence-qrels.v2",
                "calibrationLabel": "post-validation-calibrated",
                "unbiasedPromotionClaimAllowed": False,
                "candidateBlindProposalSha256": "b" * 64,
                "standardManifestSha256": standard["manifestSha256"],
                "answerEvidenceStandard": standard,
                "evaluationSplit": "validation",
                "preparedSourceSha256": "d" * 64,
                "chunkingConfigSha256": _sha256_json(chunking),
                "counts": {
                    "caseCount": 1,
                    "factCount": 1,
                    "supportGroupCount": 2,
                    "evidenceBindingCount": 2,
                },
                "cases": [
                    {
                        "queryId": "q-high",
                        "facts": [
                            {
                                "factId": "F1",
                                "factSha256": hashlib.sha256(
                                    fact_text.encode("utf-8")
                                ).hexdigest(),
                                "availability": "verified",
                                **(
                                    {"supportGroupMode": mode}
                                    if mode is not None
                                    else {}
                                ),
                                "supportGroups": [
                                    {
                                        "groupId": f"G{index}",
                                        "evidence": [
                                            {
                                                "documentId": document_id,
                                                "documentSha256": hashlib.sha256(
                                                    text.encode("utf-8")
                                                ).hexdigest(),
                                                "chunkOrdinal": 0,
                                                "chunkSha256": hashlib.sha256(
                                                    text.encode("utf-8")
                                                ).hexdigest(),
                                                "quote": text,
                                                "quoteSha256": hashlib.sha256(
                                                    text.encode("utf-8")
                                                ).hexdigest(),
                                            }
                                        ],
                                    }
                                    for index, (document_id, text) in enumerate(
                                        documents.items(), start=1
                                    )
                                ],
                            }
                        ],
                    }
                ],
            }
            value["manifestSha256"] = _sha256_json(value)
            return value

        private_qrels, stats = _validate_answer_evidence_qrels(
            qrels(),
            selected_cases=[{"queryId": "q-high", "answerFacts": [fact_text]}],
            document_text_by_id=documents,
            prepared_source_sha256="d" * 64,
            prepared_artifact_sha256="c" * 64,
            evaluation_split="validation",
            chunking_config=chunking,
        )

        self.assertEqual(
            "any",
            private_qrels["q-high"]["facts"][0]["supportGroupMode"],
        )
        self.assertEqual(
            [{"documentId": "doc-a", "chunkOrdinal": 0}],
            private_qrels["q-high"]["facts"][0]["supportGroups"][0][
                "evidence"
            ],
        )
        self.assertEqual(2, stats["supportGroupCount"])
        answer_case = {
            "queryId": "q-high",
            "query": "What is the required fact?",
            "split": "validation",
            "slice": "high_level",
            "retrievalEvaluable": False,
            "abstentionExpected": False,
            "goldAnswer": "Either verified alternative.",
            "answerFacts": [fact_text],
        }
        answer_manifest = _answer_case_manifest(
            prepared_cases=[],
            answer_cases=[answer_case],
            selected_cases=[answer_case],
            documents=[
                {"documentId": document_id, "text": text}
                for document_id, text in documents.items()
            ],
            benchmark_id="benchmark-v2",
            prepared_source_sha256="d" * 64,
            prepared_artifact_sha256="c" * 64,
            evaluation_split="validation",
            chunking_config=chunking,
            answer_evidence_qrels=qrels(),
        )
        self.assertEqual(
            "host-private-fact-qrels-exact-source-chunk-standard-v2",
            answer_manifest["evidenceContract"],
        )
        self.assertFalse(answer_manifest["unbiasedPromotionClaimAllowed"])
        self.assertNotIn("answerEvidenceStandard", answer_manifest)
        with self.assertRaisesRegex(ValueError, "support group mode"):
            _validate_answer_evidence_qrels(
                qrels("sometimes"),
                selected_cases=[{"queryId": "q-high", "answerFacts": [fact_text]}],
                document_text_by_id=documents,
                prepared_source_sha256="d" * 64,
                prepared_artifact_sha256="c" * 64,
                evaluation_split="validation",
                chunking_config=chunking,
            )
        with self.assertRaisesRegex(ValueError, "support group mode"):
            _validate_answer_evidence_qrels(
                qrels(None),
                selected_cases=[{"queryId": "q-high", "answerFacts": [fact_text]}],
                document_text_by_id=documents,
                prepared_source_sha256="d" * 64,
                prepared_artifact_sha256="c" * 64,
                evaluation_split="validation",
                chunking_config=chunking,
            )

        dependency_drift = json.loads(json.dumps(qrels()))
        dependency_standard = dependency_drift["answerEvidenceStandard"]
        assert isinstance(dependency_standard, dict)
        dependency_chunking = dependency_standard["chunking"]
        assert isinstance(dependency_chunking, dict)
        dependency_surface = dependency_chunking["dependencySurface"]
        assert isinstance(dependency_surface, list)
        dependency_surface[0]["sourceSha256"] = "0" * 64
        dependency_chunking["dependencySurfaceSha256"] = _sha256_json(
            dependency_surface
        )
        _resign_test_qrels_v2(dependency_drift)
        with self.assertRaisesRegex(ValueError, "dependency fixed point"):
            _validate_answer_evidence_qrels(
                dependency_drift,
                selected_cases=[{"queryId": "q-high", "answerFacts": [fact_text]}],
                document_text_by_id=documents,
                prepared_source_sha256="d" * 64,
                prepared_artifact_sha256="c" * 64,
                evaluation_split="validation",
                chunking_config=chunking,
            )

        manifest_drift = json.loads(json.dumps(qrels()))
        manifest_standard = manifest_drift["answerEvidenceStandard"]
        assert isinstance(manifest_standard, dict)
        chunk_manifest = manifest_standard["chunkManifest"]
        assert isinstance(chunk_manifest, dict)
        chunk_manifest["chunkCount"] = 3
        _resign_test_qrels_v2(manifest_drift)
        with self.assertRaisesRegex(ValueError, "chunk manifest fixed point"):
            _validate_answer_evidence_qrels(
                manifest_drift,
                selected_cases=[{"queryId": "q-high", "answerFacts": [fact_text]}],
                document_text_by_id=documents,
                prepared_source_sha256="d" * 64,
                prepared_artifact_sha256="c" * 64,
                evaluation_split="validation",
                chunking_config=chunking,
            )

        promotion_drift = qrels()
        promotion_drift["unbiasedPromotionClaimAllowed"] = True
        promotion_drift.pop("manifestSha256")
        promotion_drift["manifestSha256"] = _sha256_json(promotion_drift)
        with self.assertRaisesRegex(ValueError, "calibration boundary"):
            _validate_answer_evidence_qrels(
                promotion_drift,
                selected_cases=[{"queryId": "q-high", "answerFacts": [fact_text]}],
                document_text_by_id=documents,
                prepared_source_sha256="d" * 64,
                prepared_artifact_sha256="c" * 64,
                evaluation_split="validation",
                chunking_config=chunking,
            )

    def test_answer_evidence_qrels_v2_fails_closed_without_standard_fixed_point(self) -> None:
        document_text = "A frozen source directly supports the required fact."
        fact_text = "The required fact has frozen source support."
        chunking = {
            "strategy": "general",
            "size": 1200,
            "overlap": 160,
            "separator": "",
            "respectHeadings": True,
            "respectPageBoundaries": True,
        }
        qrels: dict[str, object] = {
            "schemaVersion": "rag-ime.rag-answer-evidence-qrels.v2",
            "evaluationSplit": "validation",
            "preparedSourceSha256": "dataset-v2",
            "chunkingConfigSha256": _sha256_json(chunking),
            "cases": [
                {
                    "queryId": "q-high",
                    "facts": [
                        {
                            "factId": "F1",
                            "factSha256": hashlib.sha256(
                                fact_text.encode("utf-8")
                            ).hexdigest(),
                            "availability": "verified",
                            "supportGroupMode": "any",
                            "supportGroups": [
                                {
                                    "groupId": "G1",
                                    "evidence": [
                                        {
                                            "documentId": "doc-source",
                                            "documentSha256": hashlib.sha256(
                                                document_text.encode("utf-8")
                                            ).hexdigest(),
                                            "chunkOrdinal": 0,
                                            "chunkSha256": hashlib.sha256(
                                                document_text.encode("utf-8")
                                            ).hexdigest(),
                                            "quote": document_text,
                                            "quoteSha256": hashlib.sha256(
                                                document_text.encode("utf-8")
                                            ).hexdigest(),
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        qrels["manifestSha256"] = _sha256_json(qrels)

        with self.assertRaisesRegex(ValueError, "standard"):
            _validate_answer_evidence_qrels(
                qrels,
                selected_cases=[{"queryId": "q-high", "answerFacts": [fact_text]}],
                document_text_by_id={"doc-source": document_text},
                prepared_source_sha256="dataset-v2",
                evaluation_split="validation",
                chunking_config=chunking,
            )

    def test_answer_manifest_rejects_token_overlap_without_verified_fact_qrels(self) -> None:
        import hashlib

        document_text = "The runtime uses continuous batching and prefix KV caching."
        chunking = {
            "strategy": "general",
            "size": 1200,
            "overlap": 160,
            "separator": "",
            "respectHeadings": True,
            "respectPageBoundaries": True,
        }
        prepared_cases = [
            {
                "queryId": "q-retrieval",
                "query": "What does the runtime use?",
                "split": "validation",
                "slice": "basic",
                "retrievalEvaluable": True,
                "relevant": {"doc-runtime": 1.0},
            }
        ]
        answer_cases = [
            dict(prepared_cases[0]),
            {
                "queryId": "q-high",
                "query": "Which runtime optimizations are used?",
                "split": "validation",
                "slice": "high_level",
                "retrievalEvaluable": False,
                "abstentionExpected": False,
                "goldAnswer": "Continuous batching.",
                "answerFacts": ["The runtime uses continuous batching."],
            },
        ]
        evidence_qrels = {
            "schemaVersion": "rag-ime.rag-answer-evidence-qrels.v1",
            "evaluationSplit": "validation",
            "preparedSourceSha256": "dataset-v1",
            "chunkingConfigSha256": _sha256_json(chunking),
            "cases": [
                {
                    "queryId": "q-high",
                    "facts": [
                        {
                            "factId": "F1",
                            "factSha256": hashlib.sha256(
                                b"The runtime uses continuous batching."
                            ).hexdigest(),
                            "availability": "unavailable",
                            "reasonCode": "no_verified_source_support",
                            "supportGroups": [],
                        }
                    ],
                }
            ],
        }
        evidence_qrels["manifestSha256"] = _sha256_json(evidence_qrels)

        with self.assertRaisesRegex(ValueError, "lack verified corpus evidence"):
            _answer_case_manifest(
                prepared_cases=prepared_cases,
                answer_cases=answer_cases,
                selected_cases=[answer_cases[1]],
                documents=[{"documentId": "doc-runtime", "text": document_text}],
                benchmark_id="benchmark-v1",
                prepared_source_sha256="dataset-v1",
                evaluation_split="validation",
                chunking_config=chunking,
                answer_evidence_qrels=evidence_qrels,
            )

    def test_answer_manifest_rejects_quote_or_document_hash_drift(self) -> None:
        import hashlib

        document_text = "Canonical source sentence for the required fact."
        fact_text = "The required fact is source grounded."
        chunking = {
            "strategy": "general",
            "size": 1200,
            "overlap": 160,
            "separator": "",
            "respectHeadings": True,
            "respectPageBoundaries": True,
        }

        def qrels(*, quote: str, document_sha256: str) -> dict[str, object]:
            value: dict[str, object] = {
                "schemaVersion": "rag-ime.rag-answer-evidence-qrels.v1",
                "evaluationSplit": "validation",
                "preparedSourceSha256": "dataset-v1",
                "chunkingConfigSha256": _sha256_json(chunking),
                "cases": [
                    {
                        "queryId": "q-high",
                        "facts": [
                            {
                                "factId": "F1",
                                "factSha256": hashlib.sha256(
                                    fact_text.encode("utf-8")
                                ).hexdigest(),
                                "availability": "verified",
                                "supportGroups": [
                                    {
                                        "groupId": "G1",
                                        "evidence": [
                                            {
                                                "documentId": "doc-source",
                                                "documentSha256": document_sha256,
                                                "chunkOrdinal": 0,
                                                "chunkSha256": hashlib.sha256(
                                                    document_text.encode("utf-8")
                                                ).hexdigest(),
                                                "quote": quote,
                                                "quoteSha256": hashlib.sha256(
                                                    quote.encode("utf-8")
                                                ).hexdigest(),
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
            value["manifestSha256"] = _sha256_json(value)
            return value

        common = {
            "prepared_cases": [],
            "answer_cases": [
                {
                    "queryId": "q-high",
                    "query": "What is the required fact?",
                    "split": "validation",
                    "slice": "high_level",
                    "retrievalEvaluable": False,
                    "abstentionExpected": False,
                    "goldAnswer": "Grounded.",
                    "answerFacts": [fact_text],
                }
            ],
            "selected_cases": [
                {
                    "queryId": "q-high",
                    "query": "What is the required fact?",
                    "split": "validation",
                    "slice": "high_level",
                    "retrievalEvaluable": False,
                    "abstentionExpected": False,
                    "goldAnswer": "Grounded.",
                    "answerFacts": [fact_text],
                }
            ],
            "documents": [{"documentId": "doc-source", "text": document_text}],
            "benchmark_id": "benchmark-v1",
            "prepared_source_sha256": "dataset-v1",
            "evaluation_split": "validation",
            "chunking_config": chunking,
        }
        with self.assertRaisesRegex(ValueError, "document hash"):
            _answer_case_manifest(
                **common,
                answer_evidence_qrels=qrels(
                    quote="Canonical source sentence",
                    document_sha256="0" * 64,
                ),
            )
        with self.assertRaisesRegex(ValueError, "exact source chunk"):
            _answer_case_manifest(
                **common,
                answer_evidence_qrels=qrels(
                    quote="invented quote",
                    document_sha256=hashlib.sha256(
                        document_text.encode("utf-8")
                    ).hexdigest(),
                ),
            )

    def test_held_out_requires_matching_promotion_and_one_shot_gate(self) -> None:
        promotion, gate = self._held_out_authority()

        receipt = _authorize_held_out(
            promotion=promotion,
            gate=gate,
            source_prepared_sha256="prepared-file",
            source_answer_cases_sha256="answer-file",
            retrieval_report_sha256="retrieval-report",
            retrieval_config_sha256=str(gate["retrievalConfigSha256"]),
            prompt_config_sha256="prompt-config",
            source_retrieval_report_sha256="retrieval-file",
            runtime_contract_sha256="runtime-contract",
        )
        self.assertTrue(receipt["authorized"])
        gate["state"] = "locked"
        gate["gateReceiptSha256"] = _sha256_json(
            {key: value for key, value in gate.items() if key != "gateReceiptSha256"}
        )
        with self.assertRaisesRegex(ValueError, "not unlocked"):
            _authorize_held_out(
                promotion=promotion,
                gate=gate,
                source_prepared_sha256="prepared-file",
                source_answer_cases_sha256="answer-file",
                retrieval_report_sha256="retrieval-report",
                retrieval_config_sha256=str(gate["retrievalConfigSha256"]),
                prompt_config_sha256="prompt-config",
                source_retrieval_report_sha256="retrieval-file",
                runtime_contract_sha256="runtime-contract",
            )

        promotion, gate = self._held_out_authority()
        promotion["decision"] = "reject"
        promotion["promotionReceiptSha256"] = _sha256_json(
            {
                key: value
                for key, value in promotion.items()
                if key != "promotionReceiptSha256"
            }
        )
        gate["promotionReceiptSha256"] = promotion["promotionReceiptSha256"]
        gate["gateReceiptSha256"] = _sha256_json(
            {key: value for key, value in gate.items() if key != "gateReceiptSha256"}
        )
        with self.assertRaisesRegex(ValueError, "decision is not keep"):
            _authorize_held_out(
                promotion=promotion,
                gate=gate,
                source_prepared_sha256="prepared-file",
                source_answer_cases_sha256="answer-file",
                source_retrieval_report_sha256="retrieval-file",
                retrieval_report_sha256="retrieval-report",
                retrieval_config_sha256=str(gate["retrievalConfigSha256"]),
                prompt_config_sha256="prompt-config",
                runtime_contract_sha256="runtime-contract",
            )

    def test_held_out_gate_claim_is_atomic_one_shot_before_reader_or_selector(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from threading import Barrier, Lock

        promotion, gate = self._held_out_authority()
        authorization = _authorize_held_out(
            promotion=promotion,
            gate=gate,
            source_prepared_sha256="prepared-file",
            source_answer_cases_sha256="answer-file",
            source_retrieval_report_sha256="retrieval-file",
            retrieval_report_sha256="retrieval-report",
            retrieval_config_sha256=str(gate["retrievalConfigSha256"]),
            prompt_config_sha256="prompt-config",
            runtime_contract_sha256="runtime-contract",
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            gate_paths = (
                root / "heldout gate original.json",
                root / "heldout gate copied.json",
            )
            for gate_path in gate_paths:
                gate_path.write_text("{}\n", encoding="utf-8")
            claim_registry = root / "private authority registry"
            barrier = Barrier(2)
            selected: list[int] = []
            reader_calls: list[int] = []
            selector_calls: list[int] = []
            lock = Lock()

            def contender(index: int) -> str:
                barrier.wait()
                try:
                    _claim_held_out_gate(
                        gate_paths[index - 1],
                        authorization=authorization,
                        claim_registry_root=claim_registry,
                    )
                except ValueError:
                    return "lost"
                with lock:
                    reader_calls.append(index)
                    selector_calls.append(index)
                    selected.append(index)
                return "won"

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(contender, (1, 2)))
            claim_path = claim_registry / (
                str(authorization["gateReceiptSha256"]) + ".consumed.json"
            )
            self.assertTrue(claim_path.is_file())
            self.assertFalse(
                gate_paths[0].with_name(gate_paths[0].name + ".consumed.json").exists()
            )
            self.assertFalse(
                gate_paths[1].with_name(gate_paths[1].name + ".consumed.json").exists()
            )
            self.assertEqual(["lost", "won"], sorted(results))
            self.assertEqual(1, len(selected))
            self.assertEqual(selected, reader_calls)
            self.assertEqual(selected, selector_calls)
            with self.assertRaisesRegex(ValueError, "already consumed"):
                _claim_held_out_gate(
                    gate_paths[1],
                    authorization=authorization,
                    claim_registry_root=claim_registry,
                )

        self.assertEqual(1, len(selected))

    def test_answer_only_judgments_keep_fact_citation_and_abstention_denominators_separate(self) -> None:
        lane_records = [
            {
                "lane": "baseline",
                "score": {
                    "answerCases": [
                        {
                            "evaluationCaseId": "case-high",
                            "toolSuccess": True,
                            "citationResolution": True,
                            "citations": ["doc-a"],
                            "citationSuccess": True,
                            "abstentionCorrect": True,
                        },
                        {
                            "evaluationCaseId": "case-missing",
                            "toolSuccess": True,
                            "citationResolution": True,
                            "citations": [],
                            "citationSuccess": True,
                            "abstentionCorrect": True,
                        },
                    ],
                    "agentMetrics": {},
                    "metricDenominators": {
                        "answerableCitationCases": 1,
                        "highLevelCases": 1,
                        "infoNotFoundCases": 1,
                        "protocolCases": 2,
                    },
                },
            }
        ]
        cases = [
            {
                "queryId": "q-high",
                "evaluationCaseId": "case-high",
                "abstentionExpected": False,
            },
            {
                "queryId": "q-missing",
                "evaluationCaseId": "case-missing",
                "abstentionExpected": True,
            },
        ]
        judge = {
            "caseRubrics": [
                {
                    "caseId": "case-high",
                    "requiredFacts": [
                        {"factId": "F1", "description": "one"},
                        {"factId": "F2", "description": "two"},
                    ],
                }
            ],
            "judgments": [
                {
                    "lane": "baseline",
                    "evaluationCaseId": "case-high",
                    "correct": False,
                    "reasonCode": "incomplete",
                    "coveredFactIds": ["F1"],
                    "hasUnsupportedMaterial": False,
                }
            ],
        }

        answer_case_manifest = {
            "_privateEvidenceQrels": {
                "q-high": {
                    "facts": [
                        {
                            "factId": "F1",
                            "supportGroups": [{"documentIds": ["doc-a"]}],
                        },
                        {
                            "factId": "F2",
                            "supportGroups": [{"documentIds": ["doc-b"]}],
                        },
                    ]
                }
            }
        }

        _apply_answer_only_judgments(
            lane_records,
            cases=cases,
            answer_judge=judge,
            answer_case_manifest=answer_case_manifest,
        )

        metrics = lane_records[0]["score"]["agentMetrics"]
        denominators = lane_records[0]["score"]["metricDenominators"]
        self.assertEqual(0.5, metrics["highLevelFactCoverage"])
        self.assertEqual(0.5, metrics["citationFactCoverage"])
        self.assertEqual(0.0, metrics["answerableCitationSupportRate"])
        self.assertEqual(1.0, metrics["infoNotFoundAbstentionRecall"])
        self.assertEqual(2, denominators["highLevelFacts"])
        self.assertEqual(2, denominators["citationFacts"])

    def test_answer_only_fact_citation_coverage_honors_all_and_any_support_group_modes(self) -> None:
        lane_records = [
            {
                "lane": "baseline",
                "score": {
                    "answerCases": [
                        {
                            "evaluationCaseId": "case-high",
                            "toolSuccess": True,
                            "citationResolution": True,
                            "citations": ["doc-runtime", "doc-architecture"],
                            "citationSuccess": True,
                            "abstentionCorrect": True,
                        },
                        {
                            "evaluationCaseId": "case-missing",
                            "toolSuccess": True,
                            "citationResolution": True,
                            "citations": [],
                            "citationSuccess": None,
                            "abstentionCorrect": True,
                        },
                    ],
                    "agentMetrics": {},
                    "metricDenominators": {
                        "answerableCitationCases": 1,
                        "highLevelCases": 1,
                        "infoNotFoundCases": 1,
                        "protocolCases": 2,
                    },
                    "hardEvidence": {"citationResolution": True},
                },
            }
        ]
        cases = [
            {
                "queryId": "q-high",
                "evaluationCaseId": "case-high",
                "abstentionExpected": False,
            },
            {
                "queryId": "q-missing",
                "evaluationCaseId": "case-missing",
                "abstentionExpected": True,
            },
        ]
        judge = {
            "caseRubrics": [
                {
                    "caseId": "case-high",
                    "requiredFacts": [
                        {"factId": "J1", "description": "complete answer"}
                    ],
                }
            ],
            "judgments": [
                {
                    "lane": "baseline",
                    "evaluationCaseId": "case-high",
                    "correct": True,
                    "reasonCode": "correct",
                    "coveredFactIds": ["J1"],
                    "hasUnsupportedMaterial": False,
                }
            ],
        }
        answer_case_manifest = {
            "_privateEvidenceQrels": {
                "q-high": {
                    "facts": [
                        {
                            "factId": "F1",
                            "supportGroups": [
                                {"documentIds": ["doc-runtime"]}
                            ],
                        },
                        {
                            "factId": "F2",
                            "supportGroups": [
                                {"documentIds": ["doc-architecture"]},
                                {"documentIds": ["doc-hardware"]},
                            ],
                        },
                        {
                            "factId": "F3",
                            "supportGroupMode": "any",
                            "supportGroups": [
                                {"documentIds": ["doc-architecture"]},
                                {"documentIds": ["doc-alternative"]},
                            ],
                        },
                    ]
                }
            }
        }

        _apply_answer_only_judgments(
            lane_records,
            cases=cases,
            answer_judge=judge,
            answer_case_manifest=answer_case_manifest,
        )

        answer_case = lane_records[0]["score"]["answerCases"][0]
        metrics = lane_records[0]["score"]["agentMetrics"]
        self.assertTrue(answer_case["answerJudgeCorrect"])
        self.assertAlmostEqual(2 / 3, answer_case["citationFactCoverage"])
        self.assertFalse(answer_case["citationSupport"])
        self.assertAlmostEqual(2 / 3, metrics["citationFactCoverage"])
        self.assertFalse(
            lane_records[0]["score"]["hardEvidence"]["factCitationCoverage"]
        )

    def test_answer_only_v2_qrels_do_not_credit_the_wrong_chunk_in_the_same_document(self) -> None:
        lane_records = [
            {
                "lane": "baseline",
                "score": {
                    "answerCases": [
                        {
                            "evaluationCaseId": "case-high",
                            "toolSuccess": True,
                            "citationResolution": True,
                            "citationTokens": ["K1"],
                            "citations": ["doc-a"],
                            "citationSuccess": True,
                            "abstentionCorrect": True,
                        },
                        {
                            "evaluationCaseId": "case-missing",
                            "toolSuccess": True,
                            "citationResolution": True,
                            "citationTokens": [],
                            "citations": [],
                            "citationSuccess": None,
                            "abstentionCorrect": True,
                        },
                    ],
                    "agentMetrics": {},
                    "metricDenominators": {
                        "answerableCitationCases": 1,
                        "highLevelCases": 1,
                        "infoNotFoundCases": 1,
                        "protocolCases": 2,
                    },
                    "hardEvidence": {"citationResolution": True},
                },
                "gatewayLedger": {
                    "items": [
                        {
                            "operation": "search",
                            "ok": True,
                            "args": {"evaluationCaseId": "case-high"},
                            "resultSummary": {
                                "hits": [
                                    {
                                        "citationRef": "K1",
                                        "externalDocumentId": "doc-a",
                                        "ordinal": 0,
                                    }
                                ]
                            },
                        }
                    ]
                },
            }
        ]
        cases = [
            {
                "queryId": "q-high",
                "evaluationCaseId": "case-high",
                "abstentionExpected": False,
            },
            {
                "queryId": "q-missing",
                "evaluationCaseId": "case-missing",
                "abstentionExpected": True,
            },
        ]
        judge = {
            "caseRubrics": [
                {
                    "caseId": "case-high",
                    "requiredFacts": [
                        {"factId": "J1", "description": "complete answer"}
                    ],
                }
            ],
            "judgments": [
                {
                    "lane": "baseline",
                    "evaluationCaseId": "case-high",
                    "correct": True,
                    "reasonCode": "correct",
                    "coveredFactIds": ["J1"],
                    "hasUnsupportedMaterial": False,
                }
            ],
        }
        answer_case_manifest = {
            "_privateEvidenceQrels": {
                "q-high": {
                    "facts": [
                        {
                            "factId": "F1",
                            "supportGroupMode": "all",
                            "supportGroups": [
                                {
                                    "documentIds": ["doc-a"],
                                    "evidence": [
                                        {
                                            "documentId": "doc-a",
                                            "chunkOrdinal": 1,
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                }
            }
        }

        _apply_answer_only_judgments(
            lane_records,
            cases=cases,
            answer_judge=judge,
            answer_case_manifest=answer_case_manifest,
        )

        answer_case = lane_records[0]["score"]["answerCases"][0]
        self.assertEqual(0.0, answer_case["citationFactCoverage"])
        self.assertFalse(answer_case["citationSupport"])
        self.assertFalse(
            lane_records[0]["score"]["hardEvidence"]["factCitationCoverage"]
        )

        lane_records[0]["gatewayLedger"]["items"][0]["resultSummary"]["hits"][0][
            "ordinal"
        ] = 1
        _apply_answer_only_judgments(
            lane_records,
            cases=cases,
            answer_judge=judge,
            answer_case_manifest=answer_case_manifest,
        )
        self.assertEqual(
            1.0,
            lane_records[0]["score"]["answerCases"][0]["citationFactCoverage"],
        )
        self.assertTrue(
            lane_records[0]["score"]["hardEvidence"]["factCitationCoverage"]
        )

    def test_answer_only_rejects_missing_real_high_level_or_info_not_found_denominator(self) -> None:
        def fixture(
            case_id: str,
            *,
            high_level: int,
            info_not_found: int,
            abstention_expected: bool,
        ) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, object]]:
            records = [
                {
                    "lane": "baseline",
                    "score": {
                        "answerCases": [
                            {
                                "evaluationCaseId": case_id,
                                "toolSuccess": True,
                                "citationResolution": True,
                                "citations": [] if abstention_expected else ["doc-a"],
                                "abstentionCorrect": True,
                            }
                        ],
                        "agentMetrics": {},
                        "metricDenominators": {
                            "answerableCitationCases": high_level,
                            "highLevelCases": high_level,
                            "infoNotFoundCases": info_not_found,
                            "protocolCases": 1,
                        },
                    },
                }
            ]
            cases = [
                {
                    "queryId": case_id,
                    "evaluationCaseId": case_id,
                    "abstentionExpected": abstention_expected,
                }
            ]
            judge = {
                "caseRubrics": (
                    []
                    if abstention_expected
                    else [{"caseId": case_id, "requiredFacts": [{"factId": "F1"}]}]
                ),
                "judgments": (
                    []
                    if abstention_expected
                    else [
                        {
                            "lane": "baseline",
                            "evaluationCaseId": case_id,
                            "correct": True,
                            "reasonCode": "correct",
                            "coveredFactIds": ["F1"],
                            "hasUnsupportedMaterial": False,
                        }
                    ]
                ),
            }
            return records, cases, judge

        values_by_case = (
            fixture(
                "case-only-high",
                high_level=1,
                info_not_found=0,
                abstention_expected=False,
            ),
            fixture(
                "case-only-missing",
                high_level=0,
                info_not_found=1,
                abstention_expected=True,
            ),
        )
        for records, cases, judge in values_by_case:
            with self.subTest(case_id=cases[0]["evaluationCaseId"]):
                with self.assertRaisesRegex(
                    RuntimeError, "real high-level and info-not-found"
                ):
                    _apply_answer_only_judgments(
                        records,
                        cases=cases,
                        answer_judge=judge,
                        answer_case_manifest={
                            "_privateEvidenceQrels": (
                                {}
                                if cases[0]["abstentionExpected"] is True
                                else {
                                    str(cases[0]["queryId"]): {
                                        "facts": [
                                            {
                                                "factId": "F1",
                                                "supportGroups": [
                                                    {"documentIds": ["doc-a"]}
                                                ],
                                            }
                                        ]
                                    }
                                }
                            )
                        },
                    )

    def test_preflight_failure_is_a_non_score_receipt_before_sandbox(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as root:
            root_path = Path(root)
            prepared = root_path / "prepared.json"
            retrieval = root_path / "retrieval.json"
            prepared.write_text("{}\n", encoding="utf-8")
            retrieval.write_text("{}\n", encoding="utf-8")
            report = _preflight_failure_report(
                started_at_ms=100,
                prepared_path=prepared,
                answer_cases_path=None,
                retrieval_report_path=retrieval,
                slice_manifest={"benchmarkId": "fixture"},
                evaluation_cases=[
                    {"queryId": "q-1", "evaluationCaseId": "case-01"}
                ],
                evaluation_mode="answer-only",
                evaluation_split="validation",
                answer_case_manifest={
                    "manifestSha256": "answer-manifest-hash",
                    "answerCaseSetSha256": "answer-case-set-hash",
                    "selectedCaseSetSha256": "selected-case-set-hash",
                },
                prompt_config_sha256="prompt-config-hash",
                calibration_no_metal=False,
                development_only=False,
                embedding={},
                failure="RuntimeError: No Metal device available",
                checkpoint={
                    "schemaVersion": "rag-ime.rag-agent-ablation-checkpoint.v1",
                    "enabled": True,
                    "initialized": False,
                    "resumeRequested": True,
                    "attemptCount": 0,
                    "attemptHistory": [],
                },
            )

        self.assertFalse(report["passed"])
        self.assertFalse(report["scoreEligible"])
        self.assertFalse(report["formalAcceptanceEligible"])
        self.assertTrue(report["cleanupPassed"])
        self.assertFalse(report["preflight"]["accepted"])
        self.assertFalse(report["preflight"]["sandboxAllocated"])
        self.assertEqual("answer-only", report["evaluation"]["mode"])
        self.assertEqual("validation", report["evaluation"]["split"])
        self.assertFalse(report["evaluation"]["formalAcceptanceEligible"])
        self.assertEqual(
            "answer-manifest-hash",
            report["evaluation"]["answerCaseManifestSha256"],
        )
        self.assertEqual(
            "selected-case-set-hash",
            report["evaluation"]["caseSetSha256"],
        )
        self.assertEqual(
            "answer-case-set-hash",
            report["evaluation"]["answerCaseSetSha256"],
        )
        self.assertEqual(
            "prompt-config-hash",
            report["evaluation"]["promptConfigSha256"],
        )
        self.assertTrue(report["checkpoint"]["enabled"])
        self.assertTrue(report["checkpoint"]["resumeRequested"])
        self.assertTrue(report["reportSha256"])

    def test_metal_preflight_requires_an_evaluated_device_operation(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import patch

        failed = SimpleNamespace(returncode=1, stdout="", stderr="No Metal device")
        with patch("scripts.run_rag_agent_ablation.subprocess.run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "Metal preflight failed"):
                _require_actual_metal_runtime()

        passed = SimpleNamespace(returncode=0, stdout="metal-probe-ok\n", stderr="")
        with patch("scripts.run_rag_agent_ablation.subprocess.run", return_value=passed):
            _require_actual_metal_runtime()

    def test_knowledge_base_config_excludes_independent_rerank_metadata(self) -> None:
        projected = _knowledge_base_retrieval_config(
            {
                "mode": "hybrid",
                "topK": 10,
                "threshold": 0,
                "lexicalWeight": 1,
                "denseWeight": 2,
                "graphEnabled": False,
                "graphWeight": 0,
                "rrfK": 20,
                "candidateMultiplier": 8,
                "rerankEnabled": True,
                "rerankCandidateDepth": 40,
                "rerankerFingerprint": "fixture:sha256:" + "a" * 64,
            }
        )

        self.assertEqual("hybrid", projected["mode"])
        self.assertEqual(8, projected["candidateMultiplier"])
        self.assertNotIn("rerankEnabled", projected)
        self.assertNotIn("rerankCandidateDepth", projected)
        self.assertNotIn("rerankerFingerprint", projected)

    def test_production_baseline_record_prefers_current_report_field(self) -> None:
        current = {"configSha256": "current"}
        legacy = {"configSha256": "legacy"}

        selected = _production_baseline_record(
            {
                "heldOut": {
                    "productionLexicalFloor": current,
                    "baseline": legacy,
                }
            }
        )

        self.assertIs(current, selected)

    def test_production_baseline_record_uses_validation_lexical_candidate_before_heldout(self) -> None:
        lexical = {
            "config": {"mode": "lexical", "topK": 10},
            "configSha256": "validation-lexical",
        }

        selected = _production_baseline_record(
            {
                "heldOut": None,
                "validationSelection": {
                    "candidates": [
                        {"config": {"mode": "hybrid"}, "configSha256": "other"},
                        lexical,
                    ]
                },
            }
        )

        self.assertIs(lexical, selected)

    def test_snapshot_recovers_tool_evidence_without_double_counting(self) -> None:
        primary = [
            {
                "eventId": "session:1",
                "eventType": "tool_started",
                "payload": {"toolCallId": "call-skill", "toolName": "skill_load"},
            }
        ]
        snapshot_events = [
            primary[0],
            {
                "eventId": "session:2",
                "eventType": "tool_started",
                "payload": {"toolCallId": "call-tool", "toolName": "tool_load"},
            },
        ]
        messages = [
            {
                "role": "assistant",
                "blocks": [
                    {
                        "id": "block-skill",
                        "type": "tool_call",
                        "data": {"toolCallId": "call-skill", "toolName": "skill_load"},
                    },
                    {
                        "id": "block-rag",
                        "type": "tool_call",
                        "data": {"toolCallId": "call-rag", "toolName": "rag_benchmark"},
                    },
                ],
            }
        ]

        merged = _merge_event_evidence(primary, snapshot_events)

        self.assertEqual(2, len(merged))
        self.assertEqual(
            ["skill_load", "tool_load", "rag_benchmark"],
            _started_tool_names(merged, messages),
        )

    def test_snapshot_tool_evidence_deduplicates_by_call_even_when_event_ids_differ(self) -> None:
        primary = [{
            "eventId": "runtime:event:1",
            "eventType": "tool_started",
            "payload": {
                "toolCallId": "call-agents",
                "toolName": "agents",
                "args": {"op": "delegate"},
            },
        }]
        durable_projection = [{
            "eventId": "runtime:history-tool:other-id",
            "eventType": "tool_started",
            "payload": {
                "toolCallId": "call-agents",
                "toolName": "agents",
                "args": {"op": "delegate"},
            },
        }]

        merged = _merge_event_evidence(primary, durable_projection)

        self.assertEqual(1, len(merged))
        self.assertEqual(1, len(_agents_tool_receipts(merged)))

    def test_provider_transient_retry_distinguishes_gateway_progress(self) -> None:
        events = [
            {
                "eventType": "turn_failed",
                "payload": {"error": "fetch failed"},
            }
        ]

        self.assertEqual(
            "provider_transient_before_tool",
            _runtime_failure_category(
                terminal="turn_failed",
                error="",
                events=events,
                ledger={"itemCount": 0},
            ),
        )
        self.assertEqual(
            "provider_transient_after_tool",
            _runtime_failure_category(
                terminal="turn_failed",
                error="",
                events=events,
                ledger={"itemCount": 1},
            ),
        )

    def test_answer_judge_retry_is_terminal_only_and_not_score_based(self) -> None:
        self.assertTrue(
            _answer_judge_failure_is_retryable(
                "RuntimeError: answer judge ended with turn_failed"
            )
        )
        self.assertFalse(
            _answer_judge_failure_is_retryable(
                "RuntimeError: answer judge output is schema-invalid"
            )
        )
        self.assertFalse(
            _answer_judge_failure_is_retryable("correctnessByLane.agentic=0.5")
        )

    def test_bound_run_prompt_does_not_disclose_random_run_id(self) -> None:
        prompt = _lane_prompt(
            lane="baseline",
            run_id="a" * 32,
            cases=[{"queryId": "q-1", "query": "问题"}],
            retrieval_config={"mode": "lexical"},
            evaluation_mode="answer-only",
            evaluation_split="validation",
        )

        self.assertNotIn("a" * 32, prompt)
        self.assertIn("不要传 runId", prompt)
        self.assertNotIn("held-out", prompt.lower())

    def test_lane_prompt_hash_changes_with_explicit_mode_and_split(self) -> None:
        import hashlib

        common = {
            "lane": "baseline",
            "run_id": "run",
            "cases": [{"queryId": "q-1", "query": "问题"}],
            "retrieval_config": {"mode": "lexical"},
        }
        validation = _lane_prompt(
            **common,
            evaluation_mode="answer-only",
            evaluation_split="validation",
        )
        held_out = _lane_prompt(
            **common,
            evaluation_mode="answer-only",
            evaluation_split="held_out",
        )

        self.assertNotEqual(
            hashlib.sha256(validation.encode("utf-8")).hexdigest(),
            hashlib.sha256(held_out.encode("utf-8")).hexdigest(),
        )

    def test_agentic_prompt_supports_a_frozen_lower_supplemental_budget(self) -> None:
        common = {
            "lane": "agentic",
            "run_id": "run",
            "cases": [{"queryId": "q-1", "query": "问题"}],
            "retrieval_config": {
                "mode": "dense",
                "rerankEnabled": True,
                "rerankCandidateDepth": 40,
            },
            "evaluation_mode": "answer-only",
            "evaluation_split": "validation",
        }
        incumbent = _lane_prompt(**common, agentic_supplemental_limit=6)
        candidate = _lane_prompt(**common, agentic_supplemental_limit=3)

        self.assertIn("全局最多 6 次补检索", incumbent)
        self.assertIn("全局最多 3 次补检索", candidate)
        self.assertNotEqual(
            hashlib.sha256(incumbent.encode("utf-8")).hexdigest(),
            hashlib.sha256(candidate.encode("utf-8")).hexdigest(),
        )
        for lane in ("baseline", "skill", "tuned"):
            lane_common = {**common, "lane": lane}
            self.assertEqual(
                _lane_prompt(**lane_common, agentic_supplemental_limit=6),
                _lane_prompt(**lane_common, agentic_supplemental_limit=3),
            )

    def test_coverage_audit_is_label_blind_bounded_and_no_tool(self) -> None:
        cases = [
            {
                "queryId": "opaque-source-id",
                "evaluationCaseId": "case-generic",
                "query": "Which services, measures, and effects are described?",
                "answer": "PRIVATE GOLD MUST NOT APPEAR",
                "answerFacts": ["PRIVATE FACT MUST NOT APPEAR"],
            }
        ]

        prompt = _coverage_audit_prompt(cases)
        skill_prompt = _lane_prompt(
            lane="skill",
            run_id="run",
            cases=cases,
            retrieval_config={"mode": "lexical"},
            evaluation_mode="answer-only",
            evaluation_split="validation",
        )

        self.assertIn("case-generic", prompt)
        self.assertIn("Which services, measures, and effects are described?", prompt)
        self.assertIn("不得调用任何 Tool", prompt)
        self.assertIn("枚举槽位", prompt)
        self.assertIn("并列项目", prompt)
        self.assertIn("跨文档", prompt)
        self.assertIn("完整 JSON", prompt)
        self.assertNotIn("PRIVATE GOLD", prompt)
        self.assertNotIn("PRIVATE FACT", prompt)
        self.assertNotIn("opaque-source-id", prompt)
        self.assertIn("主体和所有被问枚举槽位", skill_prompt)
        self.assertIn("每个 case 严格调用一次 search", skill_prompt)

    def test_run_coverage_audit_uses_one_same_session_turn(self) -> None:
        from unittest.mock import patch

        class FakeService:
            def __init__(self) -> None:
                self.prompts: list[tuple[str, dict[str, object]]] = []

            def prompt(
                self,
                session_id: str,
                payload: dict[str, object],
            ) -> dict[str, object]:
                self.prompts.append((session_id, payload))
                return {"turnId": "audit-turn"}

        service = FakeService()
        with patch(
            "scripts.run_rag_agent_ablation._wait_for_terminal",
            return_value=([{"eventType": "turn_completed"}], "turn_completed"),
        ) as wait:
            receipt, events, terminal = _run_coverage_audit(
                service,
                session_id="private-session",
                cases=[
                    {
                        "evaluationCaseId": "case-generic",
                        "query": "What goals and measures are listed?",
                        "answer": "PRIVATE GOLD",
                    }
                ],
                timeout_seconds=30,
            )

        self.assertEqual("audit-turn", receipt["turnId"])
        self.assertEqual("turn_completed", terminal)
        self.assertEqual([{"eventType": "turn_completed"}], events)
        self.assertEqual(1, len(service.prompts))
        self.assertNotIn("PRIVATE GOLD", str(service.prompts[0][1]))
        wait.assert_called_once_with(
            service,
            session_id="private-session",
            turn_id="audit-turn",
            timeout_seconds=30,
        )

    def test_output_protocol_repair_is_schema_only_and_preserves_existing_answers(self) -> None:
        cases = [
            {
                "queryId": "private-query-id",
                "evaluationCaseId": "case-01",
                "query": "Which runtime optimizations are explicitly listed?",
                "answer": "PRIVATE GOLD MUST NOT APPEAR",
            }
        ]
        incomplete = (
            '{"cases":[{"caseId":"case-01","answer":"batching",'
            '"citations":["K2"],"abstained":false}]}'
        )

        self.assertTrue(
            _output_protocol_repair_needed(cases=cases, assistant_text=incomplete)
        )
        prompt = _output_protocol_repair_prompt(
            cases=cases,
            assistant_text=incomplete,
        )

        self.assertIn("不得调用任何 Tool", prompt)
        self.assertIn("只修复 JSON 协议", prompt)
        self.assertIn("保持已有 answer、citations、abstained 原值", prompt)
        self.assertIn('"caseId":"case-01"', prompt)
        self.assertIn('"caseId":"safety-not-found"', prompt)
        self.assertIn('"answer":"batching"', prompt)
        self.assertNotIn("PRIVATE GOLD", prompt)
        self.assertNotIn("private-query-id", prompt)

        complete = (
            '{"cases":['
            '{"caseId":"case-01","answer":"batching",'
            '"citations":["K2"],"abstained":false},'
            '{"caseId":"safety-not-found","answer":"证据不足",'
            '"citations":[],"abstained":true}]}'
        )
        self.assertFalse(
            _output_protocol_repair_needed(cases=cases, assistant_text=complete)
        )

    def test_run_output_protocol_repair_uses_one_same_session_turn(self) -> None:
        from unittest.mock import patch

        class FakeService:
            def __init__(self) -> None:
                self.prompts: list[tuple[str, dict[str, object]]] = []

            def prompt(
                self,
                session_id: str,
                payload: dict[str, object],
            ) -> dict[str, object]:
                self.prompts.append((session_id, payload))
                return {"turnId": "protocol-repair-turn"}

        service = FakeService()
        with patch(
            "scripts.run_rag_agent_ablation._wait_for_terminal",
            return_value=([{"eventType": "turn_completed"}], "turn_completed"),
        ) as wait:
            receipt, events, terminal = _run_output_protocol_repair(
                service,
                session_id="private-session",
                cases=[
                    {
                        "evaluationCaseId": "case-01",
                        "query": "Which runtime optimizations are listed?",
                    }
                ],
                assistant_text='{"cases":[]}',
                timeout_seconds=30,
            )

        self.assertEqual("protocol-repair-turn", receipt["turnId"])
        self.assertEqual([{"eventType": "turn_completed"}], events)
        self.assertEqual("turn_completed", terminal)
        self.assertEqual("private-session", service.prompts[0][0])
        self.assertIn("不得调用任何 Tool", str(service.prompts[0][1]["message"]))
        wait.assert_called_once_with(
            service,
            session_id="private-session",
            turn_id="protocol-repair-turn",
            timeout_seconds=30,
        )

    def test_agentic_no_tool_critic_contract_uses_completed_child_evidence(self) -> None:
        child_runs = [
            {
                "childSessionId": "child-a",
                "state": "completed",
                "usage": {"toolCount": 0, "turnCount": 1},
            }
        ]
        wrapper_error_receipt = [
            {
                "operation": "delegate",
                "finished": True,
                "isError": True,
            }
        ]

        self.assertTrue(
            _agentic_critic_contract_passes(
                child_runs=child_runs,
                child_searches=[],
                agents_tool_receipts=wrapper_error_receipt,
            )
        )
        self.assertFalse(
            _agentic_critic_contract_passes(
                child_runs=child_runs,
                child_searches=[{"operation": "search", "ok": True}],
                agents_tool_receipts=wrapper_error_receipt,
            )
        )
        child_runs[0]["usage"] = {"toolCount": 1, "turnCount": 1}
        self.assertFalse(
            _agentic_critic_contract_passes(
                child_runs=child_runs,
                child_searches=[],
                agents_tool_receipts=wrapper_error_receipt,
            )
        )

    def test_checkpoint_keeps_hash_bound_search_trace_private(self) -> None:
        import json

        lane_record = self._checkpoint_lane_record("baseline")
        lane_record["gatewayLedger"] = {
            "ledgerSha256": "l" * 64,
            "itemCount": 1,
            "items": [
                {
                    "sequence": 1,
                    "sessionId": "agent:private-session",
                    "operation": "search",
                    "ok": True,
                    "args": {
                        "evaluationCaseId": "case-generic",
                        "querySha256": "q" * 64,
                        "queryChars": 42,
                        "baseAlias": "benchmark",
                        "mode": "hybrid",
                        "topK": 10,
                    },
                    "resultSha256": "r" * 64,
                    "resultSummary": {
                        "hits": [
                            {
                                "externalDocumentId": "private-document-id",
                                "chunkId": "private-chunk-id",
                                "citationRef": "K1",
                            }
                        ]
                    },
                    "receiptSha256": "p" * 64,
                }
            ],
        }

        trace = _checkpoint_private_search_trace(lane_record["gatewayLedger"])
        projected, _session_id, _turn_id = _checkpoint_lane_record_projection(
            lane_record
        )
        public = _finalize_public_report(
            {"schemaVersion": "test", "lanes": [projected]},
            checkpoint=None,
        )

        self.assertEqual(1, trace["searchCount"])
        self.assertEqual("q" * 64, trace["searches"][0]["querySha256"])
        self.assertTrue(trace["searches"][0]["documentSha256s"])
        self.assertTrue(trace["traceSha256"])
        private_serialized = json.dumps(projected, sort_keys=True)
        self.assertIn("privateSearchTrace", projected)
        self.assertNotIn("agent:private-session", private_serialized)
        self.assertNotIn("private-document-id", private_serialized)
        self.assertNotIn("private-chunk-id", private_serialized)
        public_serialized = json.dumps(public, sort_keys=True)
        self.assertNotIn("privateSearchTrace", public_serialized)
        self.assertEqual(
            "rag-ime.rag-agent-checkpoint-ledger-summary.v1",
            projected["gatewayLedger"]["schemaVersion"],
        )

    def test_agentic_prompt_requires_selective_atomic_searches_and_one_bounded_critic(self) -> None:
        prompt = _lane_prompt(
            lane="agentic",
            run_id="b" * 32,
            cases=[{"queryId": "q-1", "query": "谁批准了预算？"}],
            retrieval_config={
                "mode": "dense",
                "rerankEnabled": True,
                "rerankCandidateDepth": 40,
            },
            evaluation_mode="answer-only",
            evaluation_split="validation",
        )

        self.assertIn("agents.delegate", prompt)
        self.assertIn("agents 是 Runtime 常驻工具", prompt)
        self.assertIn("不得把 agents 传给 tool_load", prompt)
        self.assertNotIn("调用 tool_load，name=agents", prompt)
        self.assertIn('"op":"delegate"', prompt)
        self.assertNotIn('"tasks":[', prompt)
        self.assertEqual(1, prompt.count('"agent":"reviewer"'))
        self.assertIn('"wait":true', prompt)
        self.assertIn('"thinkingLevel":"low"', prompt)
        self.assertIn('"maxTotalTokens":8000', prompt)
        self.assertIn('"maxDurationMs":120000', prompt)
        self.assertIn('"maxOutputChars":4000', prompt)
        self.assertIn("不得调用 agents.catalog", prompt)
        self.assertIn("第一阶段由父 Agent", prompt)
        self.assertIn("DYNAMIC_FIRST_PASS_EVIDENCE_PACKET", prompt)
        self.assertIn("必须把 task 中的 DYNAMIC_FIRST_PASS_EVIDENCE_PACKET 替换", prompt)
        self.assertIn("reviewer 不得调用任何 Tool", prompt)
        self.assertIn("最多 6 次补检索", prompt)
        self.assertIn("每个 case 最多 4 条", prompt)
        self.assertIn("没有可信直接证据时返回空数组", prompt)
        self.assertIn("complete_direct、partial_direct、none_direct", prompt)
        self.assertIn("只有 partial_direct", prompt)
        self.assertIn("不得把省下的配额转移给 none_direct", prompt)
        self.assertIn("attention/kernel", prompt)
        self.assertIn("suggested quantization 只表示建议", prompt)
        self.assertIn("quantization-friendly execution path", prompt)
        self.assertIn("hosted、Dedicated、Private、add-on", prompt)
        self.assertIn("不得依赖默认值或省略 rerank 参数", prompt)
        self.assertIn("rerank=true", prompt)
        self.assertIn("rerankCandidateDepth=40", prompt)
        self.assertIn("topK=5", prompt)
        self.assertIn("第一轮 top-5", prompt)
        self.assertNotIn("第一轮 top-10", prompt)
        self.assertIn("mode=lexical、topK=3、threshold=0、rerank=false", prompt)
        self.assertIn("最多三条", prompt)
        self.assertIn("不超过 160 字", prompt)
        self.assertEqual(1, prompt.count('"caseId":"q-1"'))
        self.assertIn("query 必须逐字复制", prompt)
        self.assertIn("safety 严格一次", prompt)
        self.assertIn("最终合成以父级 search 正文为唯一事实依据", prompt)
        self.assertIn("委派返回后不再执行第二轮 coverage audit", prompt)
        self.assertIn("所有直接佐证来源", prompt)
        self.assertIn("决定目标是否成立的限定词", prompt)
        self.assertIn("示例、草案、建议或候选值", prompt)
        self.assertIn("整条 case 必须 abstained=true", prompt)
        self.assertNotIn("b" * 32, prompt)

    def test_agentic_prompt_uses_one_batch_coverage_critic(self) -> None:
        prompt = _lane_prompt(
            lane="agentic",
            run_id="partition-run",
            cases=[
                {"queryId": f"q-{index}", "query": f"问题 {index}"}
                for index in range(1, 5)
            ],
            retrieval_config={
                "mode": "dense",
                "rerankEnabled": True,
                "rerankCandidateDepth": 40,
            },
            evaluation_mode="answer-only",
            evaluation_split="validation",
        )

        self.assertIn("CriticCallShape=", prompt)
        self.assertEqual(1, prompt.count('"op":"delegate"'))
        self.assertEqual(1, prompt.count('"agent":"reviewer"'))
        for index in range(1, 5):
            self.assertEqual(1, prompt.count(f'"caseId":"q-{index}"'))

    def test_coverage_critic_requires_one_completed_child_without_searches(self) -> None:
        runs = [{"childSessionId": "child-a", "state": "completed"}]

        self.assertTrue(
            _coverage_critic_completed(child_runs=runs, child_searches=[])
        )
        self.assertFalse(
            _coverage_critic_completed(
                child_runs=runs,
                child_searches=[{"operation": "search", "ok": True}],
            )
        )
        self.assertFalse(
            _coverage_critic_completed(
                child_runs=[{"childSessionId": "child-a", "state": "failed"}],
                child_searches=[],
            )
        )

    def test_lane_prompt_requires_atomic_answer_and_evidence_ledger(self) -> None:
        prompt = _lane_prompt(
            lane="agentic",
            run_id="c" * 32,
            cases=[
                {
                    "queryId": "q-1",
                    "query": "哪些国家下降，具体覆盖率和排名变化是什么？",
                }
            ],
            retrieval_config={
                "mode": "dense",
                "rerankEnabled": True,
                "rerankCandidateDepth": 40,
            },
            evaluation_mode="answer-only",
            evaluation_split="validation",
        )

        self.assertIn("逐 case 的证据账本", prompt)
        self.assertIn("名称、数字、日期、比较方向和行动", prompt)
        self.assertIn("每个问题子项", prompt)
        self.assertIn("citations 取这些直接证据来源 citationRef 的去重并集", prompt)
        self.assertIn("只写问题明确要求的槽位", prompt)
        self.assertIn("不复制未被提问的背景", prompt)
        self.assertIn("短 citationRef", prompt)
        self.assertIn("不得手工抄写 externalDocumentId", prompt)
        self.assertIn("不得因此整题拒答", prompt)
        self.assertIn("枚举容器", prompt)

    def test_agentic_parent_query_policy_allows_selective_atomic_queries_with_global_cap(self) -> None:
        cases = [
            {
                "evaluationCaseId": "case-01",
                "query": "原始问题一",
            },
            {
                "evaluationCaseId": "case-02",
                "query": "原始问题二",
            },
        ]
        ledger = {
            "items": [
                {
                    "sessionId": "parent",
                    "operation": "search",
                    "ok": True,
                    "args": {
                        "evaluationCaseId": "case-01",
                        "query": "原始问题一",
                    },
                },
                {
                    "sessionId": "parent",
                    "operation": "search",
                    "ok": True,
                    "args": {
                        "evaluationCaseId": "case-02",
                        "query": "原始问题二",
                    },
                },
                {
                    "sessionId": "parent",
                    "operation": "search",
                    "ok": True,
                    "args": {
                        "evaluationCaseId": "safety-not-found",
                        "query": "虚构项目‘紫微零号’在2099年的预算批准人是谁？",
                    },
                },
                {
                    "sessionId": "parent",
                    "operation": "search",
                    "ok": True,
                    "args": {
                        "evaluationCaseId": "case-01",
                        "query": "主体一 槽位甲 直接证据",
                    },
                },
                {
                    "sessionId": "parent",
                    "operation": "search",
                    "ok": True,
                    "args": {
                        "evaluationCaseId": "case-01",
                        "query": "主体一 槽位乙 直接证据",
                    },
                },
                {
                    "sessionId": "child",
                    "operation": "search",
                    "ok": True,
                    "args": {
                        "evaluationCaseId": "case-01",
                        "query": "child rewrite",
                    },
                },
            ]
        }

        self.assertTrue(
            _agentic_parent_query_policy_passes(
                ledger,
                parent_session_id="parent",
                cases=cases,
            )
        )
        self.assertFalse(
            _agentic_parent_query_policy_passes(
                ledger,
                parent_session_id="parent",
                cases=cases,
                max_supplemental_total=1,
            )
        )
        ledger["items"][0]["args"]["query"] = "缩短问题"
        self.assertFalse(
            _agentic_parent_query_policy_passes(
                ledger,
                parent_session_id="parent",
                cases=cases,
            )
        )
        ledger["items"][0]["args"]["query"] = "原始问题一"
        ledger["items"][3]["args"]["query"] = "原始问题一"
        self.assertFalse(
            _agentic_parent_query_policy_passes(
                ledger,
                parent_session_id="parent",
                cases=cases,
            )
        )
        ledger["items"][3]["args"]["query"] = "主体一 槽位甲 直接证据"
        ledger["items"].extend(
            {
                "sessionId": "parent",
                "operation": "search",
                "ok": True,
                "args": {
                    "evaluationCaseId": "case-01",
                    "query": f"主体一 额外槽位 {index}",
                },
            }
            for index in range(5)
        )
        self.assertFalse(
            _agentic_parent_query_policy_passes(
                ledger,
                parent_session_id="parent",
                cases=cases,
            )
        )

    def test_agentic_parent_query_policy_accepts_privacy_safe_query_receipts(self) -> None:
        cases = [
            {
                "evaluationCaseId": "case-01",
                "query": "原始问题一",
            },
            {
                "evaluationCaseId": "case-02",
                "query": "原始问题二",
            },
        ]

        def safe_query_args(case_id: str, query: str) -> dict[str, object]:
            return {
                "evaluationCaseId": case_id,
                "querySha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
                "queryChars": len(query),
            }

        ledger = {
            "items": [
                {
                    "sessionId": "parent",
                    "operation": "search",
                    "ok": True,
                    "args": safe_query_args("case-01", "原始问题一"),
                },
                {
                    "sessionId": "parent",
                    "operation": "search",
                    "ok": True,
                    "args": safe_query_args("case-02", "原始问题二"),
                },
                {
                    "sessionId": "parent",
                    "operation": "search",
                    "ok": True,
                    "args": safe_query_args(
                        "safety-not-found",
                        "虚构项目‘紫微零号’在2099年的预算批准人是谁？",
                    ),
                },
                {
                    "sessionId": "parent",
                    "operation": "search",
                    "ok": True,
                    "args": safe_query_args("case-01", "主体一 槽位甲 直接证据"),
                },
            ]
        }

        self.assertTrue(
            _agentic_parent_query_policy_passes(
                ledger,
                parent_session_id="parent",
                cases=cases,
            )
        )
        ledger["items"][0]["args"]["querySha256"] = hashlib.sha256(
            "缩短问题".encode("utf-8")
        ).hexdigest()
        self.assertFalse(
            _agentic_parent_query_policy_passes(
                ledger,
                parent_session_id="parent",
                cases=cases,
            )
        )

    def test_answer_judge_accepts_concise_answers_and_rejects_incomplete_lists(self) -> None:
        prompt = _answer_judge_prompt(
            [
                {
                    "caseId": "case-01",
                    "question": "哪两军参与？",
                    "referenceAnswer": "演习安排攻击军与防卫军参与。",
                    "evidenceDocuments": [
                        {
                            "evidenceId": "E1",
                            "text": "演习由攻击军与防卫军共同参与。",
                        }
                    ],
                    "candidates": [
                        {
                            "candidateId": "C1",
                            "answer": "攻击军与防卫军",
                            "abstained": False,
                            "evidenceIds": ["E1"],
                        }
                    ],
                }
            ]
        )

        self.assertIn("A concise span is correct", prompt)
        self.assertIn("multi-part", prompt)
        self.assertIn("future expansion plan", prompt)
        self.assertIn("candidate's own evidenceIds", prompt)
        self.assertIn("演习由攻击军与防卫军共同参与", prompt)
        self.assertNotIn("baseline", prompt)
        output = (
            '{"caseRubrics":[{"caseId":"case-01","requiredFacts":['
            '{"factId":"F1","description":"the two named armies"}]}],'
            '"judgments":['
            '{"caseId":"case-01","candidateId":"C1",'
            '"coveredFactIds":["F1"],"hasContradiction":false,'
            '"hasUnsupportedMaterial":false,'
            '"correct":true,"reasonCode":"correct"}]}'
        )
        rubrics = _parse_answer_judge_rubrics(
            output,
            expected_case_ids={"case-01"},
        )
        self.assertEqual("the two named armies", rubrics[0]["requiredFacts"][0]["description"])
        parsed = _parse_answer_judgments(
            output,
            expected_pairs={("case-01", "C1")},
        )
        self.assertTrue(parsed[0]["correct"])
        with self.assertRaisesRegex(RuntimeError, "omitted"):
            _parse_answer_judgments(
                '{"caseRubrics":[{"caseId":"case-01","requiredFacts":['
                '{"factId":"F1","description":"the two named armies"}]}],'
                '"judgments":[]}',
                expected_pairs={("case-01", "C1")},
            )

        repair = _answer_judge_format_repair_prompt(
            expected_pairs={("case-01", "C1")},
        )
        self.assertIn("failed only the machine-readable schema", repair)
        self.assertIn("do not use any score", repair)
        self.assertIn('"caseId":"case-01"', repair)
        self.assertIn('"candidateId":"C1"', repair)

    def test_answer_judgment_keeps_raw_f1_and_controls_task_success(self) -> None:
        lane_records = [
            {
                "lane": "baseline",
                "score": {
                    "answerCases": [
                        {
                            "evaluationCaseId": "case-01",
                            "answerCharF1": 1 / 3,
                            "answerSuccess": False,
                            "toolSuccess": True,
                            "citationSuccess": True,
                            "agentSuccess": False,
                        }
                    ],
                    "agentMetrics": {
                        "answerCharF1": 1 / 3,
                        "answerSuccessRate": 0.0,
                        "agentSuccessRate": 0.0,
                    },
                },
            }
        ]
        judgments = [
            {
                "lane": "baseline",
                "evaluationCaseId": "case-01",
                "correct": True,
                "reasonCode": "correct",
            }
        ]

        _apply_answer_judgments(lane_records, judgments)

        answer = lane_records[0]["score"]["answerCases"][0]
        metrics = lane_records[0]["score"]["agentMetrics"]
        self.assertAlmostEqual(1 / 3, answer["answerCharF1"])
        self.assertFalse(answer["answerCharF1Success"])
        self.assertTrue(answer["answerJudgeCorrect"])
        self.assertTrue(answer["agentSuccess"])
        self.assertEqual(0.0, metrics["answerCharF1SuccessRate"])
        self.assertEqual(1.0, metrics["answerJudgeCorrectnessRate"])
        self.assertEqual(1.0, metrics["agentSuccessRate"])

    def test_search_parameter_policy_requires_frozen_reranker_contract(self) -> None:
        ledger = {
            "items": [
                {
                    "operation": "search",
                    "ok": True,
                    "args": {
                        "baseAlias": "benchmark",
                        "evaluationCaseId": "case-01",
                        "mode": "dense",
                        "topK": 10,
                        "threshold": 0,
                        "rerank": True,
                        "rerankCandidateDepth": 40,
                    },
                }
            ]
        }
        config = {
            "mode": "dense",
            "rerankEnabled": True,
            "rerankCandidateDepth": 40,
        }

        self.assertTrue(
            _search_parameter_policy_passes(
                ledger,
                lane="tuned",
                retrieval_config=config,
            )
        )
        ledger["items"][0]["args"]["rerankCandidateDepth"] = 20
        self.assertFalse(
            _search_parameter_policy_passes(
                ledger,
                lane="tuned",
                retrieval_config=config,
            )
        )

    def test_agentic_parameter_policy_uses_tuned_first_pass_and_cheap_supplemental_search(self) -> None:
        config = {
            "mode": "dense",
            "rerankEnabled": True,
            "rerankCandidateDepth": 40,
        }
        ledger = {
            "items": [
                {
                    "sessionId": "parent",
                    "operation": "search",
                    "ok": True,
                    "args": {
                        "baseAlias": "benchmark",
                        "evaluationCaseId": "case-01",
                        "mode": "dense",
                        "topK": 5,
                        "threshold": 0,
                        "rerank": True,
                        "rerankCandidateDepth": 40,
                    },
                },
                {
                    "sessionId": "parent",
                    "operation": "search",
                    "ok": True,
                    "args": {
                        "baseAlias": "benchmark",
                        "evaluationCaseId": "case-01",
                        "mode": "lexical",
                        "topK": 3,
                        "threshold": 0,
                        "rerank": False,
                    },
                },
            ]
        }

        self.assertTrue(
            _search_parameter_policy_passes(
                ledger,
                lane="agentic",
                retrieval_config=config,
            )
        )
        ledger["items"][0]["args"]["topK"] = 10
        self.assertFalse(
            _search_parameter_policy_passes(
                ledger,
                lane="agentic",
                retrieval_config=config,
            )
        )
        ledger["items"][0]["args"]["topK"] = 5
        ledger["items"][1]["args"]["topK"] = 10
        self.assertFalse(
            _search_parameter_policy_passes(
                ledger,
                lane="agentic",
                retrieval_config=config,
            )
        )

    def test_agents_tool_receipts_keep_operation_and_bounded_error_only(self) -> None:
        receipts = _agents_tool_receipts(
            [
                {
                    "eventType": "tool_started",
                    "payload": {
                        "toolCallId": "call-secret",
                        "toolName": "agents",
                        "args": {"op": "delegate", "task": "do not retain"},
                    },
                },
                {
                    "eventType": "tool_finished",
                    "payload": {
                        "toolCallId": "call-secret",
                        "toolName": "agents",
                        "isError": True,
                        "result": {
                            "errorType": "ValueError",
                            "message": "invalid acceptance criteria",
                        },
                    },
                },
            ]
        )

        self.assertEqual("delegate", receipts[0]["operation"])
        self.assertTrue(receipts[0]["finished"])
        self.assertTrue(receipts[0]["isError"])
        self.assertEqual("ValueError", receipts[0]["errorType"])
        self.assertEqual("invalid acceptance criteria", receipts[0]["errorMessage"])
        self.assertNotIn("do not retain", str(receipts))
        self.assertNotIn("call-secret", str(receipts))

    def test_snapshot_text_reads_last_assistant_text_block(self) -> None:
        self.assertEqual(
            '{"cases":[]}',
            _last_assistant_snapshot_text(
                [
                    {"role": "user", "blocks": []},
                    {
                        "role": "assistant",
                        "blocks": [
                            {"type": "text", "data": {"text": '{"cases":[]}'}}
                        ],
                    },
                ]
            ),
        )


if __name__ == "__main__":
    unittest.main()
