from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rag_ime.contracts.json_schema import validate_contract
from rag_ime.eval_run_store import EvalRunStore
from rag_ime.trace_runtime import EvidenceRef, build_trace_envelope, make_span
from rag_ime.trace_store import TraceStore
from rag_ime.vertical_agent_harness import VerticalHarnessError, load_builtin_manifests
from rag_ime.vertical_agent_suite import (
    BuiltinVerticalSuiteError,
    evaluate_vertical_agent_case_trace,
    run_builtin_vertical_agent_eval,
    run_vertical_agent_self_test_suite,
)


class VerticalAgentSelfTestSuiteTests(unittest.TestCase):
    def test_runs_all_public_examples_and_writes_only_bounded_receipts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vertical-suite-parent-") as tmp:
            root = Path(tmp) / "suite"
            report = run_vertical_agent_self_test_suite(root)

            self.assertEqual(report["status"], "completed")
            self.assertEqual(report["totalCount"], 3)
            self.assertEqual(report["passedCount"], 3)
            self.assertEqual(report["failedCount"], 0)
            self.assertEqual(
                {item["appId"] for item in report["results"]},
                {"earth-research", "sgg", "zhanggui-wenshu"},
            )
            self.assertTrue(all(item["providerCalls"] == 0 for item in report["results"]))
            self.assertTrue(all(item["productionWriteBlocked"] for item in report["results"]))
            persisted = json.loads((root / "suite-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted, report)
            validate_contract(report, "vertical-agent-self-test-suite.v1.json")
            self.assertNotIn("query", str(report).lower())
            self.assertNotIn("sales ledger", str(report).lower())

    def test_stable_trace_and_eval_identities_survive_distinct_sandbox_runs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vertical-suite-parent-") as tmp:
            first = run_vertical_agent_self_test_suite(Path(tmp) / "first", app_ids=["sgg"])
            second = run_vertical_agent_self_test_suite(Path(tmp) / "second", app_ids=["sgg"])

            first_result = first["results"][0]
            second_result = second["results"][0]
            self.assertEqual(first_result["traceId"], second_result["traceId"])
            self.assertEqual(first_result["evalRunId"], second_result["evalRunId"])
            self.assertEqual(first_result["metrics"], second_result["metrics"])

    def test_rejects_unknown_apps_and_nonempty_output_before_running(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vertical-suite-parent-") as tmp:
            unknown_root = Path(tmp) / "unknown"
            with self.assertRaisesRegex(VerticalHarnessError, "unknown vertical app"):
                run_vertical_agent_self_test_suite(unknown_root, app_ids=["missing"])
            self.assertFalse(unknown_root.exists())

            occupied = Path(tmp) / "occupied"
            occupied.mkdir()
            (occupied / "keep.txt").write_text("owner data", encoding="utf-8")
            with self.assertRaisesRegex(VerticalHarnessError, "must be empty"):
                run_vertical_agent_self_test_suite(occupied, app_ids=["sgg"])
            self.assertEqual((occupied / "keep.txt").read_text(encoding="utf-8"), "owner data")

    def test_builtin_eval_requires_exact_allowlisted_suite_and_revision(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vertical-suite-parent-") as tmp:
            root = Path(tmp)
            store = EvalRunStore(root / "runtime-eval.sqlite")
            result = run_builtin_vertical_agent_eval(
                "sgg",
                "fixture-v2",
                root / "sgg-run",
                eval_store=store,
            )
            self.assertEqual(result, {"evalRunId": result["evalRunId"]})
            self.assertIsNotNone(store.get(str(result["evalRunId"])))

            for suite_id, revision, expected_code in (
                ("unknown", "fixture-v2", "unknown_suite"),
                ("sgg", "fixture-v1", "suite_revision_mismatch"),
                ("sgg", "", "suite_revision_required"),
            ):
                with self.subTest(suite_id=suite_id, revision=revision):
                    with self.assertRaises(BuiltinVerticalSuiteError) as raised:
                        run_builtin_vertical_agent_eval(
                            suite_id,
                            revision,
                            root / f"bad-{suite_id or 'empty'}-{revision or 'none'}",
                            eval_store=store,
                        )
                    self.assertEqual(raised.exception.code, expected_code)

    def test_evaluates_external_trace_without_running_fixture_runner(self) -> None:
        manifest = load_builtin_manifests()["sgg"]
        trace = build_trace_envelope(
            trace_id="trace:vertical:external-case",
            source_kind="vertical_agent",
            input_text="public fixture query",
            spans=(
                make_span(
                    span_id="span:input",
                    name="agent.input",
                    started_at_ms=100,
                    ended_at_ms=101,
                ),
                make_span(
                    span_id="span:retrieve",
                    name="rag.retrieve",
                    parent_span_id="span:input",
                    started_at_ms=101,
                    ended_at_ms=102,
                ),
                make_span(
                    span_id="span:memory",
                    name="memory.recall",
                    parent_span_id="span:input",
                    started_at_ms=102,
                    ended_at_ms=103,
                ),
                make_span(
                    span_id="span:tool",
                    name="tool.execute",
                    parent_span_id="span:input",
                    started_at_ms=103,
                    ended_at_ms=104,
                    attributes={"toolName": "vertical.lookup"},
                ),
                make_span(
                    span_id="span:answer",
                    name="agent.answer",
                    parent_span_id="span:input",
                    started_at_ms=104,
                    ended_at_ms=105,
                ),
            ),
            evidence=(
                EvidenceRef(
                    "knowledge:sales-ledger",
                    "knowledge",
                    "K-08fe67dcbc",
                    source_lane="sandbox_knowledge",
                    evidence_stage="retrieval_output",
                ),
                EvidenceRef(
                    "memory:fixture:sgg:pricing-policy",
                    "memory",
                    "fixture://memory/sgg/pricing-policy",
                    source_lane="fixture_memory",
                    evidence_stage="memory_recall",
                ),
            ),
            now_ms=100,
        ).to_dict()

        with tempfile.TemporaryDirectory(prefix="vertical-external-trace-") as tmp:
            root = Path(tmp)
            eval_store = EvalRunStore(root / "eval.sqlite")
            trace_store = TraceStore(root / "trace.sqlite")
            with patch(
                "rag_ime.vertical_agent_suite.run_vertical_agent_self_test",
                side_effect=AssertionError("external Trace path must not run fixtures"),
            ):
                result = evaluate_vertical_agent_case_trace(
                    manifest,
                    trace,
                    eval_store=eval_store,
                    trace_store=trace_store,
                    fixture_id="sales-ledger-answer",
                    now_ms=200,
                )

            self.assertEqual(result["traceId"], trace["traceId"])
            self.assertEqual(
                result["verification"]["truth"]["requiredEvidenceIds"],
                ["knowledge:sales-ledger", "memory:fixture:sgg:pricing-policy"],
            )
            self.assertEqual(trace_store.get(str(result["traceId"])), trace)
            eval_run = eval_store.get(str(result["evalRunId"]))
            self.assertIsNotNone(eval_run)
            assert eval_run is not None
            self.assertEqual(eval_run["traceIds"], [trace["traceId"]])
            self.assertEqual(
                eval_run["suiteBinding"],
                {"suiteId": "sgg", "suiteRevision": "fixture-v2"},
            )
            self.assertEqual(
                set(result),
                {"traceId", "evalRunId", "verification"},
            )


if __name__ == "__main__":
    unittest.main()
