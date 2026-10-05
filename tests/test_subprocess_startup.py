from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.subprocess_startup import StartupDiagnostics


class StartupDiagnosticsTests(unittest.TestCase):
    def test_samples_waiting_child_and_stops_after_readiness(self) -> None:
        with tempfile.TemporaryDirectory(prefix="paw-startup-diagnostics-") as temporary:
            diagnostics = StartupDiagnostics(Path(temporary), sample_seconds=0.03)
            command = [sys.executable, "-c", "import time; time.sleep(0.2)"]
            waiting = subprocess.run(command, env=diagnostics.environment(),
                                     capture_output=True, text=True, timeout=5)
            self.assertEqual(waiting.returncode, 0, waiting.stderr)
            self.assertIn("startup: interpreter diagnostics ready", waiting.stderr)
            self.assertIn("startup: still waiting for readiness", waiting.stderr)
            self.assertIn('File "<string>"', waiting.stderr)
            diagnostics.ready()
            ready = subprocess.run(command, env=diagnostics.environment(),
                                   capture_output=True, text=True, timeout=5)
            self.assertEqual(ready.returncode, 0, ready.stderr)
            self.assertIn("startup: interpreter diagnostics ready", ready.stderr)
            self.assertNotIn("startup: still waiting for readiness", ready.stderr)

    def test_restarted_child_gets_a_new_readiness_marker_and_keeps_environment(self) -> None:
        with tempfile.TemporaryDirectory(prefix="paw-startup-diagnostics-") as temporary:
            directory = Path(temporary)
            first = StartupDiagnostics(directory)
            first.ready()
            restarted = StartupDiagnostics(directory)
            self.assertFalse(restarted.marker.exists())
            environment = restarted.environment({"PYTHONPATH": "existing", "PRESERVE": "yes"})
            self.assertEqual(environment["PRESERVE"], "yes")
            self.assertTrue(environment["PYTHONPATH"].endswith("existing"))
