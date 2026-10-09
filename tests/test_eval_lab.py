from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from unittest.mock import patch
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.contracts.json_schema import ContractValidationError, validate_contract
from rag_ime.agent_lab.experiments import AgentLabExperimentStore
from rag_ime.eval_lab import EvalLabProjection

from tests.test_agent_lab_experiment_store import _experiment


class EvalLabProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-eval-lab-")
        self.db_path = Path(self.tmp.name) / "paw.sqlite"
        self.store = AgentSessionStore(self.db_path)
        self.store.initialize()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _snapshot(self, *, index: int, succeeded: bool, passed: int, total: int) -> str:
        session = self.store.create(
            title=f"EnterpriseOps CSM · Task {index}",
            model_profile="openai-codex/gpt-5.6-sol",
            thinking_level="high",
            tool_profile_version="subagent-readonly-v1",
            execution_mode="read_only",
            evaluation_snapshot=True,
            created_at_ms=100 + index,
        )
        session_id = str(session["id"])
        self.store.bind_runtime_session(
            session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id=f"private-pi-{index}",
            transcript_ref=f"/private/machine/session-{index}.jsonl",
            binding_state="prepared",
            metadata={
                "evaluationSnapshot": {
                    "schemaVersion": "rag-ime.evaluation-snapshot.v1",
                    "runId": "enterpriseops-validation-v1",
                    "suiteId": "enterpriseops-csm",
                    "split": "validation",
                    "workflowProfile": "baseline-v1",
                    "sourceDatabaseSha256": "a" * 64,
                    "sourceReportSha256": "b" * 64,
                    "sourceTranscriptSha256": str(index) * 64,
                    "taskAlias": f"Task {index}",
                    "taskIndex": index,
                    "taskIdSha256": "c" * 64,
                    "taskSucceeded": succeeded,
                    "terminalEvent": "turn_completed",
                    "verifier": {
                        "passed": passed,
                        "total": total,
                        "passRate": passed / total,
                    },
                    "toolCalls": 4 + index,
                    "failedToolCalls": 0,
                    "latencyMs": 500.0 + index,
                }
            },
            updated_at_ms=200 + index,
        )
        self.store.set_status(session_id, "idle", updated_at_ms=300 + index)
        return session_id

    def _clone_sessions(self, original: str, count: int, *, evaluation: bool, distinct_runs: bool = False) -> list[str]:
        """Bulk scratch fixtures keep the production reader and real schema."""
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            session = dict(conn.execute("SELECT * FROM agent_sessions WHERE id=?", (original,)).fetchone())
            binding = dict(conn.execute("SELECT * FROM agent_runtime_bindings WHERE session_id=?", (original,)).fetchone())
            ids = []
            for index in range(count):
                identity = f"agent:{'evaluation' if evaluation else 'ordinary'}-{index:04d}"
                row = {**session, "id": identity, "evaluation_snapshot": int(evaluation),
                       "created_at_ms": 10_000, "updated_at_ms": 10_000}
                columns = list(row)
                conn.execute(f"INSERT INTO agent_sessions({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", [row[key] for key in columns])
                if evaluation:
                    metadata = json.loads(binding["metadata_json"])
                    metadata["evaluationSnapshot"]["taskIndex"] = index + 2
                    if distinct_runs:
                        metadata["evaluationSnapshot"]["runId"] = f"evaluation-run-{index:04d}"
                    runtime = {**binding, "session_id": identity, "external_session_id": identity, "metadata_json": json.dumps(metadata)}
                    columns = list(runtime)
                    conn.execute(f"INSERT INTO agent_runtime_bindings({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", [runtime[key] for key in columns])
                ids.append(identity)
            return ids

    def test_ordinary_sessions_cannot_hide_an_older_evaluation(self) -> None:
        for count in (500, 1000):
            with self.subTest(ordinary_count=count):
                original = self._snapshot(index=1, succeeded=True, passed=1, total=1)
                ids = self._clone_sessions(original, count, evaluation=False)
                try:
                    projection = EvalLabProjection(self.db_path)
                    with patch.object(projection.sessions, "runtime_binding", side_effect=AssertionError("evaluation must use joined binding")):
                        result = projection.list_runs()
                    self.assertEqual(result["total"], 1)
                    self.assertEqual(result["items"][0]["tasks"][0]["sessionId"], original)
                    self.assertNotIn("truncation", result)
                finally:
                    with closing(sqlite3.connect(self.db_path)) as conn, conn:
                        conn.executemany("DELETE FROM agent_sessions WHERE id=?", [(identity,) for identity in ids])
                        conn.execute("DELETE FROM agent_runtime_bindings WHERE session_id=?", (original,))
                        conn.execute("DELETE FROM agent_sessions WHERE id=?", (original,))

    def test_evaluation_keyset_pages_include_every_same_millisecond_identity_once(self) -> None:
        original = self._snapshot(index=1, succeeded=True, passed=1, total=1)
        ids = self._clone_sessions(original, 501, evaluation=True)
        self._clone_sessions(original, 1000, evaluation=False)
        cursor = {}
        seen = []
        pages = []
        while True:
            page = self.store.list_evaluation_page(limit=37, **cursor)
            pages.append(page)
            seen.extend(str(record["session"]["id"]) for record in page["items"])
            for record in page["items"]:
                self.assertEqual(record["snapshot"]["runId"], "enterpriseops-validation-v1")
                self.assertNotIn("metadata", record["session"]["runtimeBinding"])
            if not page["hasMore"]:
                break
            cursor = {"before_updated_at_ms": page["nextCursor"]["beforeUpdatedAtMs"],
                      "before_id": page["nextCursor"]["beforeId"]}
        self.assertEqual(seen, sorted(ids, reverse=True) + [original])
        self.assertEqual(len(seen), len(set(seen)))
        self.assertIsNone(pages[-1]["nextCursor"])
        bounded = self.store.list_evaluation_page(limit=1000)
        self.assertEqual(len(bounded["items"]), 500)
        self.assertTrue(bounded["hasMore"])

    def test_evaluation_pages_preserve_archive_internal_and_surface_filters(self) -> None:
        original = self._snapshot(index=1, succeeded=True, passed=1, total=1)
        ids = self._clone_sessions(original, 3, evaluation=True)
        self.store.archive(ids[0], updated_at_ms=10_000)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE agent_sessions SET session_kind='subagent_runtime' WHERE id=?", (ids[1],))
            conn.execute("UPDATE agent_sessions SET surface_kind='extension_app',owner_app_id='extension:eval-test',surface_key='original' WHERE id=?", (ids[2],))
        visible = lambda **filters: {record["session"]["id"] for record in self.store.list_evaluation_page(**filters)["items"]}
        self.assertEqual(visible(), {original, ids[2]})
        self.assertEqual(visible(include_archived=True), {original, ids[0], ids[2]})
        self.assertEqual(visible(include_internal=True), {original, ids[1], ids[2]})
        self.assertEqual(visible(include_archived=True, include_internal=True), {original, *ids})
        self.assertEqual(visible(surface_kind="extension_app", owner_app_id="extension:eval-test", surface_key="original"), {ids[2]})
        with self.assertRaisesRegex(ValueError, "surface filters require"):
            self.store.list_evaluation_page(owner_app_id="extension:eval-test")

    def test_full_evaluation_scan_preserves_run_statistics_before_task_display_limit(self) -> None:
        original = self._snapshot(index=1, succeeded=True, passed=1, total=1)
        ids = self._clone_sessions(original, 501, evaluation=True)
        payload = EvalLabProjection(self.db_path).list_runs()
        validate_contract(payload, "eval-lab-run-list.v1.json")
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["items"][0]["taskCount"], 502)
        self.assertEqual(payload["items"][0]["taskSuccessCount"], 502)
        self.assertEqual(len(payload["items"][0]["tasks"]), 500)
        self.assertEqual(payload["truncation"], {"runLimit": 500, "taskLimit": 500, "omittedRunCount": 0, "omittedTaskCount": 2})
        self.assertIn(original, {task["sessionId"] for task in payload["items"][0]["tasks"]})
        self.assertEqual(len(ids), 501)

    def test_run_display_limit_counts_runs_not_sessions_and_is_explicit(self) -> None:
        original = self._snapshot(index=1, succeeded=True, passed=1, total=1)
        self._clone_sessions(original, 501, evaluation=True, distinct_runs=True)
        payload = EvalLabProjection(self.db_path).list_runs()
        validate_contract(payload, "eval-lab-run-list.v1.json")
        self.assertEqual(payload["total"], 502)
        self.assertEqual(len(payload["items"]), 500)
        self.assertEqual([run["runId"] for run in payload["items"]], [f"evaluation-run-{index:04d}" for index in range(500)])
        self.assertEqual(payload["truncation"], {"runLimit": 500, "taskLimit": 500, "omittedRunCount": 2, "omittedTaskCount": 0})
        invalid = deepcopy(payload)
        invalid["truncation"]["omittedTaskCount"] = -1
        with self.assertRaises(ContractValidationError):
            validate_contract(invalid, "eval-lab-run-list.v1.json")

    def test_groups_snapshot_sessions_into_sanitized_runs(self) -> None:
        first = self._snapshot(index=1, succeeded=True, passed=11, total=11)
        second = self._snapshot(index=2, succeeded=False, passed=3, total=5)
        ordinary = self.store.create(title="普通会话")

        payload = EvalLabProjection(self.db_path).list_runs()

        validate_contract(payload, "eval-lab-run-list.v1.json")
        self.assertEqual(payload["total"], 1)
        run = payload["items"][0]
        self.assertEqual(run["runId"], "enterpriseops-validation-v1")
        self.assertEqual(run["taskCount"], 2)
        self.assertEqual(run["taskSuccessCount"], 1)
        self.assertEqual(run["verifierPassCount"], 14)
        self.assertEqual(run["verifierCount"], 16)
        self.assertEqual(run["toolCalls"], 11)
        self.assertEqual(
            {item["sessionId"] for item in run["tasks"]},
            {first, second},
        )
        self.assertNotIn(str(ordinary["id"]), str(payload))
        self.assertNotIn("private-pi", str(payload))
        self.assertNotIn("/private/machine", str(payload))
        self.assertNotIn("taskIdSha256", str(payload))

    def test_explanation_is_strictly_validated_and_rejects_unknown_fields(self) -> None:
        self._snapshot(index=1, succeeded=True, passed=1, total=1)
        payload = EvalLabProjection(self.db_path).list_runs()
        task = payload["items"][0]["tasks"][0]
        task["explanation"] = {
            "caseId": "case-opaque",
            "businessRequest": {"normalizedText": "请更新客户案例。"},
            "agentOutcome": {"normalizedSummary": "已完成请求。"},
            "acceptance": {
                "passed": 1,
                "total": 1,
                "items": [
                    {
                        "id": "verifier-1",
                        "label": "Verifier 1",
                        "status": "pass",
                        "failureOwner": None,
                        "explanation": "验收项通过。",
                    }
                ],
            },
        }
        validate_contract(payload, "eval-lab-run-list.v1.json")

        invalid = deepcopy(payload)
        invalid["items"][0]["tasks"][0]["explanation"]["rawTranscript"] = "must not pass"
        with self.assertRaises(ContractValidationError):
            validate_contract(invalid, "eval-lab-run-list.v1.json")

    def test_includes_latest_versioned_experiments_without_manufacturing_sessions(self) -> None:
        self._snapshot(index=1, succeeded=True, passed=1, total=1)
        AgentLabExperimentStore(self.db_path).persist(_experiment())

        payload = EvalLabProjection(self.db_path).list_runs()

        validate_contract(payload, "eval-lab-run-list.v1.json")
        self.assertEqual(payload["experimentTotal"], 1)
        self.assertEqual(
            payload["experiments"][0]["experimentId"],
            "enterprise-rag.retrieval-selection.v1",
        )
        self.assertEqual(payload["experiments"][0]["evaluationKind"], "rag_retrieval")
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["items"][0]["taskCount"], 1)

    def test_optional_source_ledger_read_through_keeps_external_db_current(self) -> None:
        ledger_path = (
            Path(__file__).resolve().parents[1]
            / "eval/interview-metrics/agent-experiments.v1.json"
        )
        payload = EvalLabProjection(
            self.db_path,
            source_ledger_path=ledger_path,
        ).list_runs()
        validate_contract(payload, "eval-lab-run-list.v1.json")
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["experimentTotal"], len(ledger["experiments"]))
        experiment_ids = {item["experimentId"] for item in payload["experiments"]}
        self.assertIn("agent-lab.model-cost.luna-max-validation.v1", experiment_ids)
        self.assertIn("trace-agent.closed-loop-historical-replay.v1", experiment_ids)
        memory = next(item for item in payload["experiments"]
                      if item["experimentId"] == "memory.maintenance-pi-model-only-20260905-r3.v1")
        self.assertEqual(memory["dataset"]["split"], "synthetic_validation")
        self.assertEqual(memory["candidate"]["metrics"]["apiCostUsd"], 0.0071846)
        self.assertEqual(memory["candidate"]["metrics"]["requestCount"], 2)
        self.assertEqual(memory["baseline"]["metrics"]["apiCostUsd"], 0.163425)
        self.assertIn("memory.maintenance-luna-model-only-r1.v1", experiment_ids)

    def test_projects_optimal_path_receipt_without_exposing_raw_evidence(self) -> None:
        payload = EvalLabProjection(
            self.db_path,
            source_ledger_path=Path(__file__).resolve().parents[1]
            / "eval/interview-metrics/agent-experiments.v1.json",
        ).list_runs()
        validate_contract(payload, "eval-lab-run-list.v1.json")
        self.assertEqual(payload["pathSearchTotal"], 4)
        search = payload["pathSearches"][0]
        self.assertEqual(
            search["selectedNodeId"], "cloudops-luna-owner-mechanism-r5"
        )
        self.assertEqual(search["claimStatus"], "best_known")
        self.assertEqual(len(search["candidates"]), 3)
        self.assertEqual(search["candidates"][0]["changedFactor"], "baseline")
        self.assertEqual(
            search["candidates"][0]["nodeId"], "cloudops-sol-alert-first-r7"
        )
        self.assertEqual(search["candidates"][1]["status"], "rejected")
        self.assertEqual(
            search["candidates"][1]["nodeId"],
            "cloudops-luna-model-only-r1",
        )
        self.assertEqual(search["candidates"][2]["status"], "eligible")
        self.assertEqual(
            search["candidates"][2]["nodeId"], search["selectedNodeId"]
        )
        self.assertIn("质量与可靠性硬门失败", search["candidates"][1]["reason"])
        enterprise = payload["pathSearches"][1]
        self.assertEqual(
            enterprise["selectedNodeId"], "enterpriseops-luna-prompt-r7"
        )
        self.assertEqual(enterprise["claimStatus"], "best_known")
        previous = payload["pathSearches"][2]
        self.assertEqual(previous["selectedNodeId"], "sol-max-controlled-20260903")
        self.assertEqual(previous["claimStatus"], "insufficient_evidence")
        historical = payload["pathSearches"][3]
        self.assertEqual(historical["selectedNodeId"], "sol-state-contract")
        self.assertEqual(historical["claimStatus"], "best_known")
        self.assertNotIn("evidenceRefs", str(search))


if __name__ == "__main__":
    unittest.main()
