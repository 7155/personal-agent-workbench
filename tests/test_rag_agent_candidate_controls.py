from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import run_rag_agent_ablation as runner


class RagAgentCandidateControlsTests(unittest.TestCase):
    def test_candidate_instructions_reach_answer_prompt_without_replacing_task(self) -> None:
        common = dict(lane="agentic", run_id="run", cases=[{"queryId": "q1", "query": "Which release?"}],
                      retrieval_config={"mode": "lexical"}, evaluation_mode="answer-only", evaluation_split="validation")
        baseline = runner._lane_prompt(**common)
        candidate = runner._lane_prompt(**common, candidate_prompt=SimpleNamespace(text="Check every named release against its source."))
        self.assertIn(baseline, candidate)
        self.assertIn("Check every named release against its source.", candidate)
        self.assertIn("Which release?", candidate)
        with self.assertRaisesRegex(ValueError, "Validation"):
            runner._lane_prompt(**{**common, "evaluation_split": "held_out"},
                                candidate_prompt=SimpleNamespace(text="candidate"))

    def test_candidate_model_does_not_change_default_judge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("prepared", "retrieval", "cases", "qrels"):
                (root / f"{name}.json").write_text("{}", encoding="utf-8")
            (root / "config").mkdir()
            arguments = [
                "--prepared", str(root / "prepared.json"),
                "--retrieval-report", str(root / "retrieval.json"),
                "--answer-cases", str(root / "cases.json"),
                "--answer-evidence-qrels", str(root / "qrels.json"),
                "--source-agent-config", str(root / "config"),
                "--output", str(root / "report.json"),
                "--private-root", str(root / "runs"),
                "--answer-only", "--development-only",
                "--model-override", "gpt-5.6-luna",
            ]
            with patch.object(runner, "_run", return_value={"passed": True}) as run, redirect_stdout(io.StringIO()):
                self.assertEqual(runner.main(arguments), 0)
            self.assertEqual(run.call_args.kwargs["evaluation_model"], "gpt-5.6-luna")
            self.assertEqual(run.call_args.kwargs["judge_model"], "gpt-6.1-sol")
            with patch.object(runner, "_run", return_value={"passed": True}) as run, redirect_stdout(io.StringIO()):
                self.assertEqual(runner.main([*arguments, "--judge-model", "gpt-5.6-luna"]), 0)
            self.assertEqual(run.call_args.kwargs["judge_model"], "gpt-5.6-luna")
            prompt_path = root / "candidate.md"
            prompt_path.write_text("Check the source for each release.", encoding="utf-8")
            with patch.object(runner, "_run", return_value={"passed": True}) as run, redirect_stdout(io.StringIO()):
                runner.main([*arguments, "--candidate-prompt-file", str(prompt_path)])
            self.assertEqual(run.call_args.kwargs["candidate_prompt"].text, "Check the source for each release.")
            prompt_path.write_text("A later candidate.", encoding="utf-8")
            self.assertEqual(run.call_args.kwargs["candidate_prompt"].text, "Check the source for each release.")

            pricing = root / "prices.json"
            pricing.write_text("{}", encoding="utf-8")
            def executed(run_root: Path, **kwargs: object) -> dict:
                (run_root / "agent.sqlite").touch()
                return {"passed": True}
            for judge, model_flags in (("gpt-5.6-sol", ["--all-models"]), ("gpt-5.6-luna", ["--model", "gpt-5.6-luna"])):
                with self.subTest(judge=judge), patch.object(runner, "_run", side_effect=executed), patch(
                    "scripts.build_agent_lab_cost_receipt_from_runtime_db.main", return_value=0
                ) as export, redirect_stdout(io.StringIO()):
                    runner.main([*arguments, "--judge-model", judge, "--pricing-config", str(pricing),
                                 "--pricing-published-date", "2026-09-05", "--cost-receipt-output", str(root / "cost.json")])
                export_args = export.call_args.args[0]
                self.assertIn(model_flags[0], export_args)
                if judge == "gpt-5.6-sol":
                    self.assertNotIn("--model", export_args)
            cost_arguments = [*arguments, "--pricing-config", str(pricing), "--pricing-published-date", "2026-09-05",
                              "--cost-receipt-output", str(root / "cost.json")]
            retained = []
            def failed_cost_source(run_root: Path, **kwargs: object) -> dict:
                retained.append(run_root)
                return executed(run_root, **kwargs)
            with patch.object(runner, "_run", side_effect=failed_cost_source), patch(
                "scripts.build_agent_lab_cost_receipt_from_runtime_db.main", side_effect=SystemExit("price missing")
            ), redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    runner.main(cost_arguments)
            self.assertTrue((retained[0] / "agent.sqlite").is_file(), "failed cost export must retain its Runtime evidence")
            with patch.object(runner, "_run") as run, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    runner.main([*cost_arguments, "--resume-checkpoint", str(root / "checkpoint.json")])
            run.assert_not_called()

    def test_judge_session_pins_model_before_runtime_admission(self) -> None:
        service = Mock()
        service.create_session.return_value = {"session": {"id": "judge-session"}}
        service.ensure_runtime.side_effect = RuntimeError("stop before Provider")
        with patch.object(runner, "_answer_judge_case_payloads", return_value=([], {}, [], [])):
            with self.assertRaisesRegex(RuntimeError, "stop before Provider"):
                runner._run_answer_judge_once(
                    service, cases=[], documents=[], lane_records=[], chunking_config={},
                    timeout_seconds=1, evaluation_model="gpt-5.6-sol",
                )
        created = service.create_session.call_args.args[0]
        self.assertEqual(created.get("modelProfile"), "openai-codex/gpt-5.6-sol")
        self.assertEqual(created.get("thinkingLevel"), "max")
        service.prompt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
