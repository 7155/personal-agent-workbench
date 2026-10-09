from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.control_api import ControlPathId, default_route_policy
from rag_ime.control_api.route_table import find_route
from rag_ime.eval_lab_evidence import EvalLabEvidenceProjection


class EvalLabEvidenceProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-eval-evidence-")
        self.root = Path(self.tmp.name) / "archive"
        self.run = self.root / "runs" / "enterpriseops-test-run"
        (self.run / "agent" / "sessions").mkdir(parents=True)
        self.db = self.run / "paw.sqlite"
        transcript = self.run / "agent" / "sessions" / "task.jsonl"
        lines = [
            {"type": "session", "version": 3, "id": "pi-test"},
            {"type": "model_change", "provider": "openai-codex", "modelId": "gpt-5.6-sol"},
            {"type": "thinking_level_change", "thinkingLevel": "max"},
            {"type": "message", "id": "u1", "timestamp": "2026-09-01T00:00:00Z", "message": {"role": "user", "content": [{"type": "text", "text": "Customer request: create a case for alice@example.com\nWork only through the provided tools."}]}},
            {"type": "message", "id": "a1", "timestamp": "2026-09-01T00:00:01Z", "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "private reasoning"}, {"type": "toolCall", "name": "create_case", "arguments": {"query": "SELECT secret FROM hidden_gold;", "account_id": 7}}], "usage": {"input": 10, "output": 4}}},
            {"type": "message", "id": "t1", "timestamp": "2026-09-01T00:00:02Z", "message": {"role": "toolResult", "toolName": "create_case", "isError": False, "content": [{"type": "text", "text": "{\"case_id\":\"CS-0001\",\"email\":\"alice@example.com\"}"}]}},
            {"type": "message", "id": "a2", "timestamp": "2026-09-01T00:00:03Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "已创建工单。"}]}},
        ]
        transcript.write_text("\n".join(json.dumps(item) for item in lines) + "\n", encoding="utf-8")
        store = AgentSessionStore(self.db)
        store.initialize()
        session = store.create(title="EnterpriseOps CSM task_test", model_profile="openai-codex/gpt-5.6-sol", thinking_level="high", execution_mode="per_action", created_at_ms=100)
        store.bind_pi_session(str(session["id"]), pi_session_id="pi-test", session_file=transcript.as_posix(), message_count=4, updated_at_ms=200)
        store.set_status(str(session["id"]), "idle", message_count=4, updated_at_ms=200)
        self.report = self.run / "report.json"
        self.report.write_text(json.dumps({"schemaVersion": "paw.enterpriseops-csm-eval.v1", "split": "validation", "evaluationContract": {"provider": "openai-codex", "model": "gpt-5.6-sol", "thinking": "high", "workflowProfile": "baseline-v1", "transport": "loopback-http-v1", "timeoutSeconds": 900}, "runtimeIdentity": {"piVersion": "0.84.2", "runtimeVersion": "test"}, "lane": {"workflowProfile": "baseline-v1", "taskCount": 1, "taskSuccessCount": 1, "taskSuccessRate": 1, "verifierCount": 2, "verifierPassCount": 2, "verifierPassRate": 1, "toolCalls": 1, "failedToolCalls": 0, "latencyMs": 100, "tasks": [{"taskId": "task_test", "taskSucceeded": True, "terminalEvent": "turn_completed", "toolCalls": 1, "failedToolCalls": 0, "latencyMs": 100, "verifier": {"passed": 2, "total": 2}}]}}, ensure_ascii=False), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_catalog_finds_all_sessions_and_detail_is_public_only(self) -> None:
        projection = EvalLabEvidenceProjection(self.root)
        catalog = projection.read()
        self.assertTrue(catalog["source"]["available"])
        self.assertEqual(catalog["source"]["runCount"], 1)
        self.assertEqual(catalog["source"]["transcriptCount"], 1)
        self.assertEqual(len(catalog["sources"]), 1)
        run = catalog["runs"][0]
        self.assertEqual(run["sessionCount"], 1)
        self.assertEqual(run["metrics"]["verifierPassCount"], 2)
        detail = projection.read({"runId": "enterpriseops-test-run", "taskIndex": "1"})["detail"]
        self.assertEqual(detail["status"], "available")
        text = json.dumps(detail, ensure_ascii=False)
        self.assertIn("create_case", text)
        self.assertIn("[邮箱已隐藏]", text)
        self.assertNotIn("private reasoning", text)
        self.assertNotIn("SELECT secret", text)
        self.assertNotIn("hidden_gold", text)
        self.assertNotIn(str(self.root), text)
        self.assertFalse(detail["protected"]["thinkingShown"])
        self.assertFalse(detail["protected"]["rawSqlShown"])

        report_detail = projection.read({"runId": "enterpriseops-test-run", "taskIndex": "0"})["detail"]
        self.assertEqual(report_detail["status"], "report_available")
        self.assertEqual(report_detail["turns"], [])
        self.assertEqual(report_detail["report"]["schemaVersion"], "paw.enterpriseops-csm-eval.v1")

    def test_unreadable_runs_keep_empty_metrics_and_visible_evidence_gaps(self) -> None:
        for index in range(9):
            run = self.root / "runs" / f"public-unreadable-run-{index}"
            run.mkdir()
            (run / "paw.sqlite").write_bytes(b"Unreadable public test fixture")
        database_before = self.db.read_bytes()
        catalog = EvalLabEvidenceProjection(self.root).read()
        self.assertEqual(catalog["total"], 10)
        self.assertEqual(len(catalog["runs"]), 10)
        unavailable = [run for run in catalog["runs"] if run["status"] == "evidence_unavailable"]
        self.assertEqual(len(unavailable), 9)
        for run in unavailable:
            self.assertEqual(run["evidenceKind"], "unavailable")
            self.assertEqual(run["metrics"], {})
            self.assertEqual(run["tasks"], [])
        readable = next(run for run in catalog["runs"] if run["runId"] == "enterpriseops-test-run")
        self.assertEqual(readable["metrics"]["taskSuccessCount"], 1)
        self.assertEqual(readable["metrics"]["verifierPassCount"], 2)
        self.assertEqual(self.db.read_bytes(), database_before)

    def test_transcript_usage_receipt_wins_over_zero_report_placeholder(self) -> None:
        report = json.loads(self.report.read_text(encoding="utf-8"))
        report["usage"] = {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 0,
        }
        self.report.write_text(json.dumps(report), encoding="utf-8")

        run = EvalLabEvidenceProjection(self.root).read()["runs"][0]

        self.assertEqual(run["environment"]["pricingUsage"], "已从 transcript 汇总 Provider usage")
        self.assertEqual(run["environment"]["usageReceipt"], {
            "source": "transcript",
            "input": 10,
            "output": 4,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 14,
        })

    def test_report_projects_runtime_reconciled_cost_authority(self) -> None:
        run_root = self.root / "runs" / "runtime-reconciled-cost"
        run_root.mkdir(parents=True)
        runtime_source_sha256 = "a" * 64
        (run_root / "report.json").write_text(json.dumps({
            "schemaVersion": "rag-ime.agent-lab-cost-receipt.v1",
            "status": "completed",
            "authority": "runtime_cost_reconciled",
            "usage": {
                "available": True,
                "uncachedInputTokens": 100,
                "cachedInputTokens": 200,
                "outputTokens": 50,
                "sourceRef": "runtime-cost:runtime-reconciled-cost",
                "sourceSha256": runtime_source_sha256,
            },
            "estimate": {
                "uncachedInputCostUsd": "0.0005",
                "cachedInputCostUsd": "0.0001",
                "outputCostUsd": "0.0015",
                "totalCostUsd": "0.0021",
            },
            "pricingIdentity": {
                "pricingId": f"pricing:sha256:{'b' * 64}",
                "provider": "openai-codex",
                "model": "gpt-5.6-sol",
                "currency": "USD",
                "unit": "per_million_tokens",
                "rates": {
                    "uncachedInputUsd": "5",
                    "cachedInputUsd": "0.5",
                    "outputUsd": "30",
                },
                "publishedDate": "2026-09-01",
                "sourceUrl": "https://example.invalid/pricing.json",
                "sourceSha256": "c" * 64,
            },
            "runtimeCostReceipt": {
                "requestCount": 2,
                "runtimeDbSha256": "d" * 64,
                "transcriptSha256s": ["e" * 64],
                "databaseUsage": {
                    "uncachedInputTokens": 100,
                    "cachedInputTokens": 200,
                    "outputTokens": 50,
                },
                "reportedCostUsd": {
                    "input": "0.0005",
                    "cacheRead": "0.0001",
                    "output": "0.0015",
                    "total": "0.0021",
                },
                "sourceSha256": runtime_source_sha256,
            },
            "billing": {"status": "not_provided"},
        }), encoding="utf-8")

        projected = next(
            item
            for item in EvalLabEvidenceProjection(self.root).read()["runs"]
            if item["runId"] == "runtime-reconciled-cost"
        )

        self.assertEqual(
            projected["environment"]["costAuthority"],
            "runtime_cost_reconciled",
        )
        self.assertEqual(
            projected["environment"]["runtimeCostReceipt"]["sourceSha256"],
            runtime_source_sha256,
        )
        self.assertEqual(
            projected["environment"]["runtimeCostReceipt"]["requestCount"],
            2,
        )

    def test_missing_archive_is_a_truthful_empty_projection(self) -> None:
        projection = EvalLabEvidenceProjection(Path(self.tmp.name) / "missing")
        payload = projection.read()
        self.assertFalse(payload["source"]["available"])
        self.assertEqual(payload["runs"], [])

    def test_report_only_receipt_is_visible_without_inventing_a_session(self) -> None:
        report_run = self.root / "runs" / "ledger-receipt"
        report_run.mkdir(parents=True)
        (report_run / "report.json").write_text(json.dumps({
            "schemaVersion": "paw.example-receipt.v1",
            "status": "rejected",
            "decision": "reject",
            "metrics": {"quality": 0.5},
            "provenanceGaps": ["没有公开 transcript"],
        }, ensure_ascii=False), encoding="utf-8")
        projection = EvalLabEvidenceProjection(self.root)
        catalog = projection.read()
        run = next(item for item in catalog["runs"] if item["runId"] == "ledger-receipt")
        self.assertEqual(run["sessionCount"], 0)
        self.assertEqual(run["status"], "rejected")
        self.assertEqual(run["evidenceKind"], "report_only")
        detail = projection.read({"runId": "ledger-receipt"})["detail"]
        self.assertEqual(detail["status"], "report_only")
        self.assertEqual(detail["turns"], [])
        self.assertEqual(detail["report"]["decision"], "reject")
        explicit_report = projection.read({"runId": "ledger-receipt", "taskIndex": "0"})["detail"]
        self.assertEqual(explicit_report["status"], "report_only")

    def test_checked_in_retained_cost_receipts_project_exact_price_evidence(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        product_root = Path(self.tmp.name) / "product"
        runs_root = product_root / "eval" / "interview-metrics" / "runs"
        runs_root.mkdir(parents=True)
        expected = {
            "agent-lab-cost-enterpriseops-sol-max-compact-baseline-20260904.v1.json": "2.258372",
            "agent-lab-cost-enterpriseops-sol-max-preloaded-20260904.v1.json": "1.565709",
            "agent-lab-cost-memory-sol-max-full-json-baseline-20260904.r2.v1.json": "0.265205",
            "agent-lab-cost-memory-sol-max-concise-contract-20260904.r3.v1.json": "0.24769",
        }
        for filename in expected:
            shutil.copy2(
                repo_root / "eval" / "interview-metrics" / "runs" / filename,
                runs_root / filename,
            )

        with (
            patch("rag_ime.eval_lab_evidence._project_root", return_value=product_root),
            patch("rag_ime.eval_lab_evidence._safe_source_root", return_value=None),
        ):
            catalog = EvalLabEvidenceProjection().read()

        by_id = {item["runId"]: item for item in catalog["runs"]}
        for filename, total_usd in expected.items():
            with self.subTest(filename=filename):
                run = by_id[f"ledger--{Path(filename).stem}"]
                self.assertTrue(run["reportAvailable"])
                self.assertEqual(run["environment"]["costEstimate"]["totalCostUsd"], total_usd)
                if filename.startswith("agent-lab-cost-enterpriseops"):
                    self.assertEqual(
                        run["environment"]["pricingIdentity"]["pricingId"],
                        "pricing:sha256:80067d14fbe8bca408a20e095bb0b4323b390bb14031157fb83bc7eb6490a04e",
                    )
                    self.assertEqual(
                        run["environment"]["costAuthority"],
                        "runtime_cost_reconciled",
                    )
                else:
                    self.assertEqual(
                        run["environment"]["pricingIdentity"]["pricingId"],
                        "openai-api-gpt-5.6-sol-2026-09-01",
                    )
                self.assertEqual(run["environment"]["billing"], {"status": "not_provided"})

    def test_default_catalog_discovers_source_local_enterpriseops_sessions(self) -> None:
        repo_root = Path(self.tmp.name) / "product"
        run = repo_root / ".rag-ime-data" / "enterpriseops-agent-eval" / "enterpriseops-csm-luna-validation-v2"
        sessions = run / "agent" / "sessions"
        sessions.mkdir(parents=True)
        transcript = sessions / "task.jsonl"
        transcript.write_text("\n".join([
            json.dumps({"type": "session", "version": 3, "id": "pi-local"}),
            json.dumps({"type": "model_change", "provider": "openai-codex", "modelId": "gpt-5.6-luna"}),
            json.dumps({
                "type": "message",
                "message": {"role": "user", "content": [{"type": "text", "text": "Customer request: update the account.\nWork only through the provided tools."}]},
            }),
            json.dumps({
                "type": "message",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "已完成更新。"}]},
            }),
        ]) + "\n", encoding="utf-8")
        store = AgentSessionStore(run / "paw.sqlite")
        store.initialize()
        session = store.create(
            title="EnterpriseOps CSM local task",
            model_profile="openai-codex/gpt-5.6-luna",
            thinking_level="max",
            execution_mode="per_action",
            created_at_ms=100,
        )
        store.bind_pi_session(
            str(session["id"]),
            pi_session_id="pi-local",
            session_file=transcript.as_posix(),
            message_count=2,
            updated_at_ms=200,
        )

        with patch("rag_ime.eval_lab_evidence._project_root", return_value=repo_root):
            projection = EvalLabEvidenceProjection()
            catalog = projection.read()

        local_run = next(item for item in catalog["runs"] if item["runId"] == "enterpriseops-local--enterpriseops-csm-luna-validation-v2")
        self.assertEqual(local_run["sourceId"], "paw-local-enterpriseops")
        self.assertEqual(local_run["sessionCount"], 1)
        self.assertEqual(local_run["transcriptCount"], 1)
        self.assertEqual(local_run["family"], "EnterpriseOps CSM")

    def test_report_task_rows_keep_per_task_failure_without_fake_transcript(self) -> None:
        report_run = self.root / "runs" / "report-with-tasks"
        report_run.mkdir(parents=True)
        (report_run / "report.json").write_text(json.dumps({
            "schemaVersion": "paw.example-receipt.v1",
            "status": "rejected",
            "tasks": [{
                "taskAlias": "Task 1",
                "taskSucceeded": False,
                "verifierPassed": 1,
                "verifierTotal": 2,
                "failedVerifierIndexes": [2],
                "provisionalFirstOwner": "prompt_context",
                "usage": {
                    "input": 10,
                    "output": 3,
                    "cacheRead": 20,
                    "cacheWrite": 0,
                    "totalTokens": 33,
                },
            }],
        }, ensure_ascii=False), encoding="utf-8")
        projection = EvalLabEvidenceProjection(self.root)
        run = next(item for item in projection.read()["runs"] if item["runId"] == "report-with-tasks")
        self.assertEqual(run["sessionCount"], 0)
        self.assertEqual(len(run["tasks"]), 1)
        self.assertEqual(run["tasks"][0]["title"], "Task 1")
        self.assertEqual(run["tasks"][0]["evidenceStatus"], "report_only")
        self.assertEqual(run["tasks"][0]["failureOwner"], "prompt_context")
        self.assertFalse(run["tasks"][0]["toolReceiptComplete"])
        self.assertEqual(run["tasks"][0]["inputTokens"], 10)
        self.assertEqual(run["tasks"][0]["cacheReadTokens"], 20)
        self.assertEqual(run["tasks"][0]["outputTokens"], 3)
        self.assertEqual(run["environment"]["usageReceipt"], {
            "source": "report_task_usage",
            "input": 10,
            "output": 3,
            "cacheRead": 20,
            "cacheWrite": 0,
            "totalTokens": 33,
            "categoryReceiptComplete": True,
        })
        task_detail = projection.read({"runId": "report-with-tasks", "taskIndex": "1"})["detail"]
        self.assertEqual(task_detail["status"], "report_only")
        self.assertEqual(task_detail["task"]["failedVerifierIndexes"], [2])

    def test_report_batch_derives_recovered_tool_failures_from_authoritative_counts(self) -> None:
        report_run = self.root / "runs" / "cloudops-batches"
        report_run.mkdir(parents=True)
        (report_run / "report.json").write_text(json.dumps({
            "schemaVersion": "paw.cloudops-agent-eval-run.v1",
            "status": "completed",
            "batches": [{
                "batchId": "batch-2",
                "answerCount": 4,
                "terminalEvent": "turn_completed",
                "toolCalls": 32,
                "successfulToolCalls": 31,
                "usage": {
                    "input": 84,
                    "output": 13,
                    "cacheRead": 1261,
                    "cacheWrite": 0,
                },
            }],
        }), encoding="utf-8")

        projection = EvalLabEvidenceProjection(self.root)
        run = next(
            item for item in projection.read()["runs"]
            if item["runId"] == "cloudops-batches"
        )

        self.assertTrue(run["tasks"][0]["taskSucceeded"])
        self.assertEqual(run["tasks"][0]["toolCalls"], 32)
        self.assertEqual(run["tasks"][0]["toolFailures"], 1)
        self.assertTrue(run["tasks"][0]["toolReceiptComplete"])

    def test_rag_lane_list_projects_agentic_cases_without_hidden_answers(self) -> None:
        report_run = self.root / "runs" / "rag-answer-run"
        report_run.mkdir(parents=True)
        (report_run / "report.json").write_text(json.dumps({
            "schemaVersion": "rag-ime.rag-agent-ablation-run.v1",
            "status": "completed",
            "evaluation": {"split": "validation"},
            "lanes": [
                {
                    "lane": "baseline",
                    "terminalEvent": "turn_completed",
                    "score": {"answerCases": [{
                        "evaluationCaseId": "case-01",
                        "agentSuccess": False,
                        "answerSuccess": False,
                        "citationSupport": False,
                        "abstentionCorrect": True,
                        "toolSuccess": True,
                        "answer": "must not be projected",
                    }]},
                },
                {
                    "lane": "agentic",
                    "terminalEvent": "turn_completed",
                    "score": {"answerCases": [
                        {
                            "evaluationCaseId": "case-01",
                            "agentSuccess": False,
                            "answerSuccess": True,
                            "citationSupport": False,
                            "citationFactCoverage": 0.4,
                            "abstentionCorrect": True,
                            "toolSuccess": True,
                            "answer": "private candidate answer",
                            "citations": ["hidden-document-id"],
                        },
                        {
                            "evaluationCaseId": "case-02",
                            "agentSuccess": True,
                            "answerSuccess": True,
                            "citationSupport": True,
                            "citationFactCoverage": 1.0,
                            "abstentionCorrect": True,
                            "toolSuccess": True,
                        },
                    ]},
                },
            ],
        }, ensure_ascii=False), encoding="utf-8")

        projection = EvalLabEvidenceProjection(self.root)
        run = next(
            item for item in projection.read()["runs"]
            if item["runId"] == "rag-answer-run"
        )

        self.assertEqual([task["title"] for task in run["tasks"]], ["case-01", "case-02"])
        self.assertFalse(run["tasks"][0]["taskSucceeded"])
        self.assertEqual(run["tasks"][0]["verifierPassed"], 3)
        self.assertEqual(run["tasks"][0]["verifierTotal"], 4)
        self.assertEqual(run["tasks"][0]["failedVerifierNames"], ["citationSupport"])
        self.assertEqual(run["tasks"][0]["citationFactCoverage"], 0.4)
        self.assertTrue(run["tasks"][1]["taskSucceeded"])
        rendered = json.dumps(run, ensure_ascii=False)
        self.assertNotIn("private candidate answer", rendered)
        self.assertNotIn("hidden-document-id", rendered)
        self.assertNotIn("must not be projected", rendered)

    def test_report_projects_public_trace_receipt_ids(self) -> None:
        report_run = self.root / "runs" / "trace-receipts"
        report_run.mkdir(parents=True)
        (report_run / "report.json").write_text(json.dumps({
            "schemaVersion": "paw.example-receipt.v1",
            "status": "completed",
            "traceIds": ["trace:case:one"],
            "receipts": {
                "aggregateTraceId": "trace:aggregate:one",
                "traceId": "trace:case:two",
                "notTrace": "secret-value",
            },
        }, ensure_ascii=False), encoding="utf-8")

        projection = EvalLabEvidenceProjection(self.root)
        run = next(
            item for item in projection.read()["runs"]
            if item["runId"] == "trace-receipts"
        )

        self.assertEqual(run["environment"]["traceCount"], 3)
        self.assertEqual(
            run["environment"]["traceIds"],
            ["trace:case:one", "trace:aggregate:one", "trace:case:two"],
        )
        self.assertNotIn("secret-value", json.dumps(run, ensure_ascii=False))

    def test_report_allowlist_sanitizes_scalar_and_credential_fields(self) -> None:
        report_run = self.root / "runs" / "unsafe-report"
        report_run.mkdir(parents=True)
        (report_run / "report.json").write_text(json.dumps({
            "schemaVersion": "paw.example-receipt.v1",
            "status": "/Users/private/report SELECT value FROM hidden_table;",
            "decision": "password=short-pass",
            "result": "apiKey:short-key",
            "runtime": "/Volumes/private/runtime",
            "environment": {
                "password": "p@ss",
                "apiKey": "key-value",
                "reasoning": "private reasoning",
                "safeLabel": "Darwin",
            },
            "metrics": {"/Users/private/metric": 1},
        }, ensure_ascii=False), encoding="utf-8")

        projection = EvalLabEvidenceProjection(self.root)
        detail = projection.read({"runId": "unsafe-report", "taskIndex": "0"})["detail"]
        rendered = json.dumps(detail, ensure_ascii=False)

        for forbidden in (
            "/Users/private/report",
            "SELECT value FROM hidden_table",
            "password=short-pass",
            "apiKey:short-key",
            "/Volumes/private/runtime",
            "p@ss",
            "key-value",
            "private reasoning",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, rendered)
        self.assertIn("safeLabel", rendered)

    def test_transcript_only_run_is_not_reported_as_completed(self) -> None:
        transcript_run = self.root / "runs" / "transcript-only"
        sessions = transcript_run / "agent" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "task.jsonl").write_text(json.dumps({
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "完成了公开步骤。"}],
            },
        }, ensure_ascii=False) + "\n", encoding="utf-8")

        projection = EvalLabEvidenceProjection(self.root)
        run = next(item for item in projection.read()["runs"] if item["runId"] == "transcript-only")
        self.assertEqual(run["status"], "transcript_only")
        self.assertEqual(run["evidenceKind"], "transcript_only")
        self.assertFalse(run["reportAvailable"])

    def test_evidence_route_is_local_read_only_with_bounded_query_keys(self) -> None:
        policy = default_route_policy()
        route = policy.resolve(ControlPathId.AGENT_EVAL_LAB_EVIDENCE)
        descriptor = find_route("GET", "/api/agent/eval-lab/evidence")

        self.assertEqual(route.local_8766_path, "/api/agent/eval-lab/evidence")
        self.assertIsNone(route.gateway_8768_path)
        self.assertFalse(route.remote_safe)
        self.assertEqual(route.query, frozenset({"runId", "taskIndex"}))
        self.assertIsNotNone(descriptor)
        self.assertEqual(descriptor.handler, "agent.eval_lab_evidence_read")
        self.assertEqual(descriptor.query_args, ("runId", "taskIndex"))
        self.assertTrue(descriptor.takes_arguments)


if __name__ == "__main__":
    unittest.main()
