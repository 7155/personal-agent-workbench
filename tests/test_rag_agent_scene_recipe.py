from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import run_rag_agent_ablation as runner


def scene_binding(*, candidate: bool = True, revision: int = 1) -> dict[str, object]:
    return {
        "schemaVersion": "rag-ime.agent-lab-scene-recipe-binding.v1",
        "sceneId": "agent-lab.enterprise-rag.validation",
        "revision": revision,
        "versionId": (
            "enterprise-rag.validation.luna-prompt-v4-r6.v1" if candidate
            else "enterprise-rag.validation.incumbent.v1"
        ),
        "recipe": {
            "provider": "openai-codex", "model": "gpt-5.6-luna" if candidate else "gpt-5.6-sol",
            "thinkingLevel": "max", "promptProfile": "coverage-balanced-evidence-gate-v4" if candidate else "incumbent",
            "promptContractVersion": "rag-agent-evidence-state-budget-routing-v19",
            "agenticSupplementalLimit": 6, "answerOnly": True, "developmentOnly": True,
            "split": "validation", "candidateAware": True, "unbiasedPromotionClaimAllowed": False,
        },
        "effectScope": "future_validation_runs",
    }


class RagAgentSceneRecipeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-rag-scene-binding-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ("prepared", "retrieval", "cases", "qrels"):
            (self.root / f"{name}.json").write_text("{}", encoding="utf-8")
        (self.root / "config").mkdir()

    def arguments(self, binding: dict[str, object]) -> list[str]:
        path = self.root / "recipe.json"
        path.write_text(json.dumps(binding), encoding="utf-8")
        return [
            "--scene-recipe", str(path),
            "--prepared", str(self.root / "prepared.json"),
            "--retrieval-report", str(self.root / "retrieval.json"),
            "--answer-cases", str(self.root / "cases.json"),
            "--answer-evidence-qrels", str(self.root / "qrels.json"),
            "--source-agent-config", str(self.root / "config"),
            "--output", str(self.root / "result.json"),
            "--private-root", str(self.root / "private-runs"),
        ]

    def test_main_uses_candidate_snapshot_without_extra_model_flags(self) -> None:
        binding = scene_binding()
        with patch.object(runner, "_run", return_value={"passed": True}) as run, redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(self.arguments(binding)), 0)
        args = run.call_args.kwargs
        self.assertEqual(args["evaluation_model"], "gpt-5.6-luna")
        self.assertEqual(args["prompt_profile"], "coverage-balanced-evidence-gate-v4")
        self.assertEqual(args["agentic_supplemental_limit"], 6)
        self.assertTrue(args["answer_only"])
        self.assertTrue(args["development_only"])
        self.assertEqual(args["evaluation_split"], "validation")
        self.assertEqual(args["scene_recipe"], binding)

    def test_prompt_candidate_can_derive_from_recipe_without_rewriting_parent(self) -> None:
        from scripts.agent_eval_candidate_prompt import CandidatePrompt
        binding = scene_binding()
        candidate = CandidatePrompt("Check every required fact against its citation.")
        path = self.root / "candidate.md"
        path.write_text(candidate.text, encoding="utf-8")
        with patch.object(runner, "_run", return_value={"passed": True}) as run, redirect_stdout(io.StringIO()):
            runner.main([*self.arguments(binding), "--candidate-prompt-file", str(path)])
        self.assertEqual(run.call_args.kwargs["scene_recipe"], binding)
        self.assertEqual(run.call_args.kwargs["candidate_prompt"], candidate)
        report = runner._finalize_public_report({"conditions": {"sceneRecipe": binding}}, checkpoint=None,
                                               scene_recipe=binding, candidate_prompt=candidate, judge_model="gpt-5.6-sol")
        self.assertNotIn("sceneRecipe", report["conditions"])
        self.assertEqual(report["conditions"]["parentSceneRecipe"], binding)
        self.assertTrue(report["conditions"]["candidatePrompt"]["enabled"])

    def test_later_incumbent_run_does_not_rewrite_previous_candidate_binding(self) -> None:
        candidate = scene_binding()
        incumbent = scene_binding(candidate=False, revision=2)
        with patch.object(runner, "_run", return_value={"passed": True}) as run, redirect_stdout(io.StringIO()):
            runner.main(self.arguments(candidate))
            first = run.call_args.kwargs
            runner.main(self.arguments(incumbent))
            second = run.call_args.kwargs
        self.assertEqual(first["scene_recipe"], candidate)
        self.assertEqual(first["evaluation_model"], "gpt-5.6-luna")
        self.assertEqual(second["scene_recipe"], incumbent)
        self.assertEqual(second["evaluation_model"], "gpt-5.6-sol")
        self.assertEqual(second["prompt_profile"], "incumbent")

    def test_explicit_conflicts_fail_before_private_directory_or_runtime(self) -> None:
        for extra in (
            ["--evaluation-split", "held_out"],
            ["--model-override", "gpt-5.6-sol"],
            ["--prompt-profile", "incumbent"],
            ["--agentic-supplemental-limit", "3"],
            ["--calibration-no-metal"],
            ["--promotion-receipt", "unused.json"],
            ["--heldout-gate", "unused.json"],
        ):
            with self.subTest(extra=extra), patch.object(runner, "_run") as run, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    runner.main([*self.arguments(scene_binding()), *extra])
                self.assertEqual(caught.exception.code, 2)
                run.assert_not_called()
                self.assertFalse((self.root / "private-runs").exists())

    def test_explicit_matching_flags_are_accepted(self) -> None:
        with patch.object(runner, "_run", return_value={"passed": True}) as run, redirect_stdout(io.StringIO()):
            runner.main([
                *self.arguments(scene_binding()), "--model-override=gpt-5.6-luna",
                "--prompt-profile", "coverage-balanced-evidence-gate-v4",
                "--evaluation-split", "validation", "--agentic-supplemental-limit", "6",
                "--answer-only", "--development-only",
            ])
        self.assertEqual(run.call_args.kwargs["scene_recipe"], scene_binding())

    def test_invalid_binding_is_rejected_before_any_private_run_is_created(self) -> None:
        changes = (
            {"schemaVersion": "unknown"}, {"sceneId": "another-scene"},
            {"versionId": "unregistered-version"}, {"revision": True}, {"revision": -1},
        )
        recipe_changes = (
            {"provider": "another-provider"}, {"model": "unknown-model"},
            {"thinkingLevel": "low"}, {"promptContractVersion": "unknown-contract"},
            {"promptProfile": "incumbent"}, {"agenticSupplementalLimit": 3},
            {"answerOnly": False}, {"developmentOnly": False}, {"split": "held_out"},
            {"candidateAware": False}, {"unbiasedPromotionClaimAllowed": True},
            {"answerOnly": 1}, {"developmentOnly": 1}, {"agenticSupplementalLimit": 6.0},
        )
        values = [{**scene_binding(), **change} for change in changes]
        values += [{**scene_binding(), "recipe": {**scene_binding()["recipe"], **change}} for change in recipe_changes]
        for value in values:
            with self.subTest(value=value), patch.object(runner, "_run") as run, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    runner.main(self.arguments(value))
                run.assert_not_called()
                self.assertFalse((self.root / "private-runs").exists())

    def test_without_scene_recipe_keeps_existing_runner_defaults(self) -> None:
        args = self.arguments(scene_binding())
        args = args[2:]
        qrels_index = args.index("--answer-evidence-qrels")
        del args[qrels_index:qrels_index + 2]
        with patch.object(runner, "_run", return_value={"passed": True}) as run, redirect_stdout(io.StringIO()):
            runner.main(args)
        actual = run.call_args.kwargs
        self.assertEqual(actual["evaluation_model"], "gpt-6.1-sol")
        self.assertEqual(actual["prompt_profile"], "incumbent")
        self.assertEqual(actual["evaluation_split"], "held_out")
        self.assertFalse(actual["answer_only"])
        self.assertFalse(actual["development_only"])
        self.assertIsNone(actual.get("scene_recipe"))

    def run_until_preflight(self, binding: dict[str, object], *, judge_model: str = "gpt-5.6-sol") -> dict[str, object]:
        empty_hash = runner._sha256_json({})
        retrieval = {"validationSelection": {"winner": {"config": {}}, "frozenConfigSha256": empty_hash}, "chunking": {}}
        def read(path: Path) -> dict[str, object]:
            return retrieval if path.name == "retrieval.json" else {"cases": []}
        with patch.multiple(
            runner,
            _load_prepared=Mock(return_value={}),
            _read_json_object=Mock(side_effect=read),
            _reconstruct_frozen_slice=Mock(return_value=([], [], {"benchmarkId": "fixture", "sourceSha256": "a" * 64})),
            _development_exclusion=Mock(return_value={"caseIds": []}),
            _production_baseline_record=Mock(return_value={"config": {}, "configSha256": empty_hash}),
            select_agent_answer_cases=Mock(return_value=[]),
            _answer_case_manifest=Mock(return_value={}),
            _lane_prompt=Mock(return_value="fixture lane"),
            _embedding_environment_from_report=Mock(return_value={}),
            _file_sha256=Mock(return_value="a" * 64),
            _require_actual_metal_runtime=Mock(side_effect=RuntimeError("isolated preflight stop")),
            embedding_provider_from_env=Mock(side_effect=AssertionError("no Provider in this test")),
        ):
            return runner._run(
                self.root / "unused-run", prepared_path=self.root / "prepared.json",
                answer_cases_path=self.root / "cases.json", answer_evidence_qrels_path=self.root / "qrels.json",
                retrieval_report_path=self.root / "retrieval.json", source_agent_config=self.root / "config",
                slice_seed="fixture", agent_seed="fixture", slice_cases_per_split=4,
                agent_case_limit=4, distractor_limit=0, timeout_seconds=60, lane_attempts=1,
                reranker_model=None, reranker_revision="", reranker_cache=None,
                rerank_instruction="", development_report_paths=[], development_only=True,
                evaluation_split="validation", answer_only=True, pi_runtime_payload=self.root,
                evaluation_model=binding["recipe"]["model"], prompt_profile=binding["recipe"]["promptProfile"],
                scene_recipe=binding,
                judge_model=judge_model,
            )

    def test_checkpoint_identity_includes_the_independent_judge(self) -> None:
        binding = scene_binding()
        fixed_judge = self.run_until_preflight(binding)
        changed_judge = self.run_until_preflight(binding, judge_model="gpt-5.6-luna")
        self.assertEqual(fixed_judge["conditions"]["answerJudgeModel"], "openai-codex/gpt-5.6-sol")
        self.assertNotEqual(fixed_judge["evaluation"]["promptConfigSha256"], changed_judge["evaluation"]["promptConfigSha256"])

    def test_real_run_records_binding_before_provider_and_fingerprints_its_revision(self) -> None:
        first = scene_binding()
        report = self.run_until_preflight(first)
        same = self.run_until_preflight(copy.deepcopy(first))
        later = self.run_until_preflight({**first, "revision": 3})
        self.assertFalse(report["passed"])
        self.assertEqual(report["conditions"]["sceneRecipe"], first)
        self.assertEqual(report["evaluation"]["promptConfigSha256"], same["evaluation"]["promptConfigSha256"])
        self.assertNotEqual(report["evaluation"]["promptConfigSha256"], later["evaluation"]["promptConfigSha256"])
        fingerprint = {"evaluationSplit": "validation", "maximumAttemptsPerLane": 1,
                       "promptConfigSha256": report["evaluation"]["promptConfigSha256"]}
        checkpoint = self.root / "checkpoint.json"
        runner._open_lane_checkpoint(checkpoint, fingerprint=fingerprint, resume=False)
        runner._open_lane_checkpoint(checkpoint, fingerprint=fingerprint, resume=True)
        with self.assertRaisesRegex(ValueError, "fingerprint drift"):
            runner._open_lane_checkpoint(checkpoint, fingerprint={**fingerprint, "promptConfigSha256": later["evaluation"]["promptConfigSha256"]}, resume=True)


if __name__ == "__main__":
    unittest.main()
