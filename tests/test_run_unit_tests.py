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
