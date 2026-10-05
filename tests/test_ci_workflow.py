from __future__ import annotations

import shlex
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


class CIWorkflowTests(unittest.TestCase):
    def test_provenance_checks_have_full_git_history(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

        jobs = yaml.safe_load(workflow)["jobs"]
        for job_name in ("python-plan", "python-unit", "python", "macos-plan", "macos-unit", "macos"):
            with self.subTest(job=job_name):
                steps = jobs[job_name]["steps"]
                checkout = next(step for step in steps if step.get("uses") == "actions/checkout@v6")
                self.assertEqual(checkout["with"]["fetch-depth"], 0)
                self.assertTrue(any(step.get("uses") == "actions/setup-python@v6" for step in steps))

    def test_squirrel_build_uses_a_swift_6_runner(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        patch = (ROOT / "squirrel-patches/0001-add-rag-ime-sidecar.patch").read_text(encoding="utf-8")

        self.assertIn("throws(ReservedPropertyError)", patch)
        self.assertIn("runs-on: macos-15", workflow)

    def test_each_platform_plans_full_discovery_and_runs_all_twelve_shards(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        jobs = yaml.safe_load(workflow)["jobs"]
        for platform in ("python", "macos"):
            with self.subTest(platform=platform):
                plan = jobs[f"{platform}-plan"]
                step = next(s for s in plan["steps"] if s.get("name") == "Plan the complete unit suite")
                self.assertEqual(shlex.split(step["run"]), [
                    "python", "scripts/run_unit_tests.py", "--pattern", "test*.py",
                    "--write-shard-plan", "$RUNNER_TEMP/plan.json", "--shard-count", "12",
                ])
                self.assertEqual(step["env"]["PYTHONWARNINGS"], "error::ResourceWarning")
                worker = jobs[f"{platform}-unit"]
                self.assertEqual(worker["needs"], f"{platform}-plan")
                self.assertEqual(worker["timeout-minutes"], 30)
                self.assertEqual(worker["strategy"]["matrix"]["shard"], list(range(12)))
                self.assertFalse(worker["strategy"]["fail-fast"])
                self.assertNotIn("continue-on-error", worker)
                run = next(s for s in worker["steps"] if s.get("name") == "Run assigned unit suite")
                self.assertEqual(run["env"]["PYTHONWARNINGS"], "error::ResourceWarning")
                self.assertEqual(shlex.split(run["run"].splitlines()[1]), [
                    "python", "scripts/run_unit_tests.py", "--shard-plan", "$RUNNER_TEMP/paw-unit-plan/plan.json",
                    "--shard-index", "$SHARD_INDEX", "--shard-receipt", "$RUNNER_TEMP/paw-unit-output/shard-$SHARD_INDEX.json",
                    "--timing-jsonl", "$RUNNER_TEMP/paw-unit-output/timing-$SHARD_INDEX.jsonl",
                ])
                upload = next(s for s in worker["steps"] if s.get("uses") == "actions/upload-artifact@v4")
                self.assertEqual(upload["if"], "always()")
                self.assertEqual(upload["with"]["if-no-files-found"], "error")
                self.assertIn("${{ matrix.shard }}", upload["with"]["name"])
                self.assertIn("${{ github.run_id }}", upload["with"]["name"])
                self.assertTrue(all("continue-on-error" not in s for s in worker["steps"]))

    def test_original_gates_require_complete_receipts_even_for_failed_or_skipped_workers(self) -> None:
        jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))["jobs"]
        for platform, name in (("python", "Python, schemas, and contracts"), ("macos", "Native control and Squirrel release")):
            with self.subTest(platform=platform):
                gate = jobs[platform]
                self.assertEqual(gate["name"], name)
                self.assertEqual(gate["if"], "always()")
                self.assertEqual(gate["needs"], [f"{platform}-plan", f"{platform}-unit"])
                self.assertNotIn("continue-on-error", gate)
                verify = next(s for s in gate["steps"] if s.get("name") == "Verify complete unit-suite receipts")
                self.assertEqual(verify["env"]["UNIT_RESULT"], "${{ needs['" + platform + "-unit'].result }}")
                self.assertEqual(verify["env"]["PLAN_RESULT"], "${{ needs['" + platform + "-plan'].result }}")
                self.assertEqual(shlex.split(verify["run"].splitlines()[0]), ["test", "$PLAN_RESULT", "=", "success"])
                self.assertEqual(shlex.split(verify["run"].splitlines()[1]), [
                    "python", "scripts/run_unit_tests.py", "--verify-shards", "$RUNNER_TEMP/paw-unit-plan/plan.json",
                    "--shard-receipts", "$RUNNER_TEMP/paw-unit-receipts", "--workflow-result", "$UNIT_RESULT",
                ])
                receipts = next(s for s in gate["steps"] if "pattern" in s.get("with", {}))
                self.assertFalse(receipts["with"]["merge-multiple"])
                self.assertEqual(receipts["with"]["pattern"], f"unit-receipt-{platform}-" + "${{ github.run_id }}-*")
                self.assertTrue(all("continue-on-error" not in s for s in gate["steps"]))
        steps = jobs["macos"]["steps"]
        verify_index = next(i for i, s in enumerate(steps) if s.get("name") == "Verify complete unit-suite receipts")
        for name in ("Build native control center", "Build headless voice agent", "Build patched Squirrel Release", "Verify native signatures"):
            index = next(i for i, s in enumerate(steps) if s.get("name") == name)
            self.assertGreater(index, verify_index)
            self.assertNotIn("if", steps[index])

    def test_linux_mac_only_tests_remain_explicitly_skipped(self) -> None:
        mac_only_modules = (
            "test_check_macos_input_source.py",
            "test_memory_book_maintenance_scripts.py",
            "test_prepare_squirrel_workspace.py",
            "test_product_readiness_gate.py",
            "test_setup_xcode_for_squirrel.py",
            "test_doctor_squirrel_integration.py",
        )

        for name in mac_only_modules:
            source = (ROOT / "tests" / name).read_text(encoding="utf-8")
            self.assertIn('@unittest.skipUnless(sys.platform == "darwin"', source, name)


if __name__ == "__main__":
    unittest.main()
