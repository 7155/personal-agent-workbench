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
        for job_name in ("python", "macos"):
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

    def test_macos_job_runs_full_suite_and_linux_mac_tests_are_explicitly_skipped(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        mac_only_modules = (
            "test_check_macos_input_source.py",
            "test_memory_book_maintenance_scripts.py",
            "test_prepare_squirrel_workspace.py",
            "test_product_readiness_gate.py",
            "test_setup_xcode_for_squirrel.py",
            "test_doctor_squirrel_integration.py",
        )

        jobs = yaml.safe_load(workflow)["jobs"]
        for job_name, step_name, minutes in (
            ("python", "Run unit suite", 30),
            ("macos", "Run full macOS unit suite", 60),
        ):
            with self.subTest(job=job_name):
                job = jobs[job_name]
                step = next(step for step in job["steps"] if step.get("name") == step_name)
                self.assertEqual(shlex.split(step["run"]), [
                    "python", "scripts/run_unit_tests.py", "--pattern", "test*.py",
                    "--timing-jsonl", "$RUNNER_TEMP/paw-unit-timing.jsonl",
                ])
                self.assertEqual(step["env"]["PYTHONWARNINGS"], "error::ResourceWarning")
                self.assertEqual(job["timeout-minutes"], minutes)
        self.assertIn("Run full macOS unit suite", workflow)
        for name in mac_only_modules:
            source = (ROOT / "tests" / name).read_text(encoding="utf-8")
            self.assertIn('@unittest.skipUnless(sys.platform == "darwin"', source, name)


if __name__ == "__main__":
    unittest.main()
