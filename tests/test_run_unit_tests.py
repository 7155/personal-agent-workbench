from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DiagnosticUnitRunnerTests(unittest.TestCase):
    def run_shard_pipeline(self, body: str):
        with tempfile.TemporaryDirectory(prefix="paw-runner-shard-cli-") as temporary:
            root = Path(temporary)
            (root / "tests").mkdir()
            (root / "tests/test_worker_cli.py").write_text(
                "import time,unittest\nclass Fixture(unittest.TestCase):\n" + body,
                encoding="utf-8",
            )
            entry = "\n".join([
                "import sys", "from pathlib import Path",
                f"sys.path.insert(0, {str(ROOT)!r})",
                "from scripts import run_unit_tests as runner, unit_test_shards as shards",
                "runner.ROOT = Path(sys.argv.pop(1))",
                "shards.repository_tree = lambda root: 'fixture-tree'",
                "runner.repository_tree = shards.repository_tree",
                "raise SystemExit(runner.main())",
            ])
            command = [sys.executable, "-B", "-c", entry, str(root)]
            plan, receipts, timing = root / "plan.json", root / "receipts", root / "timing.jsonl"
            planned = subprocess.run(
                [*command, "--write-shard-plan", str(plan), "--shard-count", "1"],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(planned.returncode, 0, planned.stdout + planned.stderr)
            worker = subprocess.run(
                [*command, "--shard-plan", str(plan), "--shard-index", "0",
                 "--shard-receipt", str(receipts / "shard-0.json"),
                 "--timing-jsonl", str(timing), "--watchdog-seconds", "0.05"],
                capture_output=True, text=True, timeout=10,
            )
            gate = subprocess.run(
                [*command, "--verify-shards", str(plan), "--shard-receipts", str(receipts),
                 "--workflow-result", "success"],
                capture_output=True, text=True, timeout=10,
            )
            receipt = json.loads((receipts / "shard-0.json").read_text(encoding="utf-8"))
            events = [json.loads(line) for line in timing.read_text(encoding="utf-8").splitlines()]
            return worker, gate, receipt, events

    def test_shard_cli_retains_watchdog_timing_skip_and_successful_terminal_gate(self) -> None:
        worker, gate, receipt, events = self.run_shard_pipeline(
            "    def test_slow(self): time.sleep(0.15)\n"
            "    @unittest.skip('normal unit skip')\n    def test_skip(self): pass\n"
        )
        self.assertEqual(worker.returncode, 0, worker.stderr)
        self.assertEqual(gate.returncode, 0, gate.stderr)
        self.assertIn("Diagnostic stack watchdog", worker.stderr)
        self.assertIn("Module time", worker.stderr)
        self.assertEqual([receipt["testsRun"], receipt["skipped"]], [2, 1])
        self.assertEqual(receipt["status"], "completed")
        self.assertTrue(receipt["successful"])
        self.assertEqual(events[-1]["tests"], 2)
        self.assertFalse(events[-1]["interrupted"])
        self.assertEqual(json.loads(gate.stdout)["shards"], 1)

    def test_shard_cli_flushes_failure_before_next_test_and_gate_stays_red(self) -> None:
        worker, gate, receipt, events = self.run_shard_pipeline(
            "    def test_a_failure(self): self.fail('shard failure sentinel')\n"
            "    def test_z_next(self): pass\n"
        )
        self.assertEqual(worker.returncode, 1)
        self.assertEqual(gate.returncode, 1)
        self.assertLess(worker.stderr.index("shard failure sentinel"), worker.stderr.index("test_z_next ("))
        self.assertFalse(receipt["successful"])
        self.assertEqual(receipt["failures"], 1)
        self.assertEqual(events[-1]["failures"], 1)

    def test_partial_shard_cli_options_are_rejected_before_loading_tests(self) -> None:
        for options in (
            ["--shard-plan", "missing.json"],
            ["--shard-index", "0"],
            ["--shard-count", "12"],
            ["--verify-shards", "missing.json", "--shard-receipts", "missing"],
            ["--write-shard-plan", "unused.json", "named.test"],
        ):
            with self.subTest(options=options):
                completed = subprocess.run([sys.executable, str(ROOT / "scripts/run_unit_tests.py"), *options],
                                           capture_output=True, text=True, timeout=10)
                self.assertEqual(completed.returncode, 2, completed.stderr)

    def run_fixture(self, body: str, *, watchdog: float = 60) -> tuple[subprocess.CompletedProcess[str], list[dict]]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "diagnostic_fixture.py").write_text(
                "import time,unittest\nclass Fixture(unittest.TestCase):\n" + body,
                encoding="utf-8",
            )
            timing = root / "timing.jsonl"
            environment = {**os.environ, "PYTHONPATH": str(root)}
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts/run_unit_tests.py"),
                 "--watchdog-seconds", str(watchdog), "--timing-jsonl", str(timing),
                 "diagnostic_fixture"],
                env=environment, capture_output=True, text=True, timeout=10,
            )
            events = [json.loads(line) for line in timing.read_text(encoding="utf-8").splitlines()]
            return result, events

    def test_failure_remains_failure_with_timing_and_module_receipt(self) -> None:
        result, events = self.run_fixture("    def test_failure(self): self.fail('sentinel failure')\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("sentinel failure", result.stderr)
        self.assertIn("Module time", result.stderr)
        self.assertEqual([event["event"] for event in events], ["start", "stop", "result"])
        self.assertEqual(events[-1]["failures"], 1)

    def test_watchdog_dumps_stack_without_terminating_or_skipping_slow_test(self) -> None:
        result, events = self.run_fixture("    def test_first_slow(self): time.sleep(0.15)\n    def test_next(self): pass\n", watchdog=0.05)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Diagnostic stack watchdog", result.stderr)
        self.assertIn("diagnostic_fixture.py", result.stderr)
        self.assertEqual(events[-1]["tests"], 2)
        self.assertEqual(events[-1]["skipped"], 0)

    def test_tracebacks_are_printed_before_next_test_without_changing_results(self) -> None:
        result, events = self.run_fixture(
            "    def test_a_failure(self): self.fail('early failure sentinel')\n"
            "    def test_b_error(self): raise RuntimeError('early error sentinel')\n"
            "    def test_c_subtests(self):\n"
            "        with self.subTest(kind='failure'): self.fail('early subtest failure sentinel')\n"
            "        with self.subTest(kind='error'): raise ValueError('early subtest error sentinel')\n"
            "        with self.subTest(kind='success'): pass\n"
            "    def test_z_next(self): pass\n"
        )
        self.assertEqual(result.returncode, 1)
        next_test = result.stderr.index('test_z_next (')
        for message in ('early failure sentinel', 'early error sentinel',
                        'early subtest failure sentinel', 'early subtest error sentinel'):
            self.assertLess(result.stderr.index(message), next_test, result.stderr)
        self.assertEqual(events[-1]['tests'], 4)
        self.assertEqual(events[-1]['failures'], 2)
        self.assertEqual(events[-1]['errors'], 2)
        self.assertFalse(events[-1]['interrupted'])

    def test_success_exits_normally_and_releases_its_diagnostic_thread(self) -> None:
        result, events = self.run_fixture("    def test_success(self): pass\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(events[-1]["tests"], 1)
        self.assertFalse(events[-1]["interrupted"])

    def test_watchdog_exit_never_waits_for_asynchronous_native_cancellation(self) -> None:
        result, events = self.run_fixture(
            "    def test_guard(self):\n"
            "        import faulthandler\n"
            "        def forbidden(*args, **kwargs): raise AssertionError('native asynchronous watchdog used')\n"
            "        faulthandler.dump_traceback_later = forbidden\n"
            "        faulthandler.cancel_dump_traceback_later = forbidden\n"
            "        time.sleep(0.15)\n",
            watchdog=0.05,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Diagnostic stack watchdog", result.stderr)
        self.assertEqual(events[-1]["failures"], 0)

    @unittest.skipUnless(os.name == "posix", "requires POSIX SIGINT delivery")
    def test_interrupt_finishes_current_test_and_reports_incomplete_suite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "interrupt_fixture.py").write_text(
                "import time,unittest\nclass Fixture(unittest.TestCase):\n"
                "    def test_first(self): time.sleep(0.3)\n"
                "    def test_second(self): pass\n", encoding="utf-8",
            )
            timing = root / "timing.jsonl"
            process = subprocess.Popen(
                [sys.executable, str(ROOT / "scripts/run_unit_tests.py"),
                 "--timing-jsonl", str(timing), "interrupt_fixture"],
                env={**os.environ, "PYTHONPATH": str(root)},
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                deadline = time.monotonic() + 3
                while not timing.exists() or not timing.read_text(encoding="utf-8"):
                    if time.monotonic() >= deadline:
                        self.fail("runner did not start fixture")
                    time.sleep(0.01)
                process.send_signal(signal.SIGINT)
                _, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 130, stderr)
                result = json.loads(timing.read_text(encoding="utf-8").splitlines()[-1])
                self.assertTrue(result["interrupted"])
                self.assertEqual(result["tests"], 1)
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate()
