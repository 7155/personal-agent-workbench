from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from rag_ime.cli import main
from rag_ime.hybrid_rag_eval import load_hybrid_rag_eval_cases, run_hybrid_rag_eval


class HybridRagEvalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.cases_file = self.root / "eval" / "hybrid_rag_core_cases.jsonl"

    def test_load_hybrid_rag_eval_cases(self) -> None:
        cases = load_hybrid_rag_eval_cases(self.cases_file, default_project="wisdom-weasel-rag-ime")

        self.assertGreaterEqual(len(cases), 4)
        self.assertEqual(cases[0].case_id, "hybrid-multilane-rag")
        self.assertIn("bm25_tags", cases[0].must_have_lane)

    def test_run_hybrid_rag_eval_passes_correctness_gate(self) -> None:
        report = run_hybrid_rag_eval(
            cases_file=self.cases_file,
            project="wisdom-weasel-rag-ime",
            repeat=1,
            top_k=5,
            latency_budget_ms=25,
            enforce_latency=False,
        )

        self.assertTrue(report["gatePassed"])
        self.assertEqual(report["failedCases"], 0)
        self.assertEqual(report["metrics"]["rawSentenceLeakRate"], 0.0)
        self.assertEqual(report["metrics"]["tombstoneLeakRate"], 0.0)
        self.assertFalse(report["latencyGateEnforced"])

    def test_run_hybrid_rag_eval_strict_latency_gate_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-hybrid-rag-latency-test-") as tmp:
            path = Path(tmp) / "cases.jsonl"
            case = json.loads(self.cases_file.read_text(encoding="utf-8").splitlines()[0])
            case["maxLatencyMs"] = -1
            path.write_text(json.dumps(case, ensure_ascii=False) + "\n", encoding="utf-8")

            report = run_hybrid_rag_eval(
                cases_file=path,
                project="wisdom-weasel-rag-ime",
                repeat=1,
                top_k=5,
                latency_budget_ms=25,
            )

        self.assertTrue(report["latencyGateEnforced"])
        self.assertFalse(report["gatePassed"])
        self.assertTrue(any(str(item).startswith("latency:") for item in report["cases"][0]["failures"]))

    def test_run_hybrid_rag_eval_reports_failed_case(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-hybrid-rag-eval-test-") as tmp:
            path = Path(tmp) / "cases.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "id": "expected-failure",
                        "query": "不存在的候选",
                        "memoryState": {"phraseCandidates": [{"text": "别的候选", "sourceEventIds": [1]}]},
                        "mustContainAny": ["必须出现的候选"],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            report = run_hybrid_rag_eval(
                cases_file=path,
                project="wisdom-weasel-rag-ime",
                repeat=1,
                top_k=5,
                latency_budget_ms=25,
            )

        self.assertFalse(report["gatePassed"])
        self.assertEqual(report["failedCases"], 1)
        self.assertIn("missing_top3_any", report["cases"][0]["failures"][0])

    def test_cli_eval_hybrid_rag_core_outputs_json_report(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main(
                [
                    "eval-hybrid-rag-core",
                    "--cases-file",
                    str(self.cases_file),
                    "--project",
                    "wisdom-weasel-rag-ime",
                    "--repeat",
                    "1",
                    "--skip-latency-check",
                    "--summary-only",
                ]
            )
        payload = json.loads(stdout.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual(payload["schemaVersion"], "rag-ime.hybrid-rag-eval.v1")
        self.assertTrue(payload["gatePassed"])
        self.assertFalse(payload["latencyGateEnforced"])
        self.assertEqual(payload["cases"], [])

    def test_hybrid_rag_gate_script_runs_hybrid_eval(self) -> None:
        script = (self.root / "scripts" / "run_hybrid_rag_gate.sh").read_text(encoding="utf-8")

        self.assertIn("eval-hybrid-rag-core", script)
        self.assertIn("eval/hybrid_rag_core_cases.jsonl", script)
        self.assertIn("eval/v1_post_commit_memory_cases.jsonl", script)
        self.assertIn("RAG_IME_HYBRID_RAG_CORE=1", script)
        self.assertIn("RAG_IME_AI_AFTER_COMMIT_ONLY=1", script)
        self.assertNotIn("--skip-latency-check", script)
        self.assertNotIn("eval/memory_optimizer_cases.jsonl", script)

        algorithm_script = (self.root / "scripts" / "run_memory_optimizer_algorithm_gate.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("eval/memory_optimizer_cases.jsonl", algorithm_script)
        self.assertIn("RAG_IME_AI_AFTER_COMMIT_ONLY=0", algorithm_script)


if __name__ == "__main__":
    unittest.main()
