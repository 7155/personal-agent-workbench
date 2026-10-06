from __future__ import annotations

import json
import os
import subprocess
import time
from contextlib import ExitStack
from unittest.mock import patch
import sys
import tempfile
import unittest
from pathlib import Path

from tests.subprocess_startup import StartupCapture, StartupDiagnostics


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


class StartupCaptureTests(unittest.TestCase):
    def test_isolated_file_preserves_flags_argv_output_and_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "server.py"
            source = (
                "import json, sys\n"
                "print(json.dumps({'isolated': sys.flags.isolated, 'no_site': sys.flags.no_site, 'argv': sys.argv}))\n"
                "print('synthetic stderr', file=sys.stderr)\n"
                "raise SystemExit(7)\n"
            )
            target.write_text(source)
            with ExitStack() as resources, StartupCapture(root / "diagnostics", label="isolated-file", isolated=True) as capture:
                resources.callback(capture.close)
                process = capture.popen(
                    [sys.executable, "-I", "-S", str(target), "--value", "fixture"],
                    env={"HOME": str(root), "PATH": os.defpath}, cwd=root,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                stdout, stderr = process.communicate(timeout=5)
                value = json.loads(stdout)
                self.assertEqual(value, {"isolated": 1, "no_site": 1, "argv": [str(target), "--value", "fixture"]})
                self.assertEqual(process.returncode, 7)
                self.assertIn("synthetic stderr", stderr)
                self.assertNotIn("startup:", stderr)
                self.assertNotIn("startup:", stdout)
                self.assertEqual(target.read_text(), source)
                details = capture.failure_details()
                self.assertIn("returncode=7", details)
                self.assertIn("synthetic stderr", details)
                self.assertIn("startup: interpreter diagnostics ready", details)

    def test_isolated_module_preserves_standard_library_module_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.json"
            source.write_text('{"synthetic": true}')
            with ExitStack() as resources, StartupCapture(root / "diagnostics", label="isolated-module", isolated=True) as capture:
                resources.callback(capture.close)
                process = capture.popen(
                    [sys.executable, "-I", "-u", "-m", "json.tool", str(source)],
                    env={"HOME": str(root), "PATH": os.defpath}, cwd=root,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertEqual(json.loads(stdout), {"synthetic": True})

    def test_supplied_environment_and_original_popen_identity_are_retained(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observed = {}
            process = object()
            def factory(command, **kwargs):
                observed.update(command=command, **kwargs)
                return process
            capture = StartupCapture(root, label="supplied-env", popen=factory)
            command = ["/synthetic/venv/python", "-m", "synthetic_worker", "--parent-pid", "123"]
            supplied = {"PATH": "/synthetic/bin", "ALLOWED": "fixture", "PYTHONPATH": "/synthetic/modules"}
            with patch.dict(os.environ, {"SYNTHETIC_SECRET_NOT_FOR_CHILD": "must-not-propagate"}):
                actual = capture.popen(command, env=supplied, cwd=root, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            self.assertIs(actual, process)
            self.assertIs(observed["command"], command)
            self.assertEqual(observed["cwd"], root)
            self.assertTrue(observed["start_new_session"])
            self.assertEqual(observed["stdin"], subprocess.DEVNULL)
            self.assertEqual(observed["env"]["ALLOWED"], "fixture")
            self.assertNotIn("SYNTHETIC_SECRET_NOT_FOR_CHILD", observed["env"])
            self.assertTrue(observed["env"]["PYTHONPATH"].endswith("/synthetic/modules"))
            self.assertEqual(supplied["PYTHONPATH"], "/synthetic/modules")
            self.assertTrue(observed["stdout"].closed)
            self.assertTrue(observed["stderr"].closed)

    def test_inherited_environment_capture_does_not_add_environment_or_wrapper(self):
        with tempfile.TemporaryDirectory() as temporary:
            seen = {}
            process = object()
            def factory(command, **kwargs):
                seen.update(command=command, **kwargs)
                return process
            capture = StartupCapture(Path(temporary), label="inherited", popen=factory)
            command = ["/synthetic/python", "-m", "synthetic_worker", "--parent-pid", "99999999"]
            self.assertIs(capture.popen_inherited(command, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL), process)
            self.assertIs(seen["command"], command)
            self.assertNotIn("env", seen)
            self.assertIn("not injected", capture.children[0]["sampler"])

    def test_tail_reads_are_bounded_and_preserve_invalid_utf8_and_exit_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "output.py"
            target.write_text("import os\nos.write(1, b'A'*20000+b'OUT-END\\xff')\nos.write(2, b'B'*20000+b'ERR-END\\xff')\nraise SystemExit(23)\n")
            with ExitStack() as resources, StartupCapture(root / "diagnostics", label="bounded", tail_bytes=64) as capture:
                resources.callback(capture.close)
                process = capture.popen([sys.executable, str(target)], env={"PATH": os.defpath},
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                process.wait(timeout=5)
                details = capture.failure_details()
                self.assertIn("returncode=23", details)
                self.assertIn("OUT-END\ufffd", details)
                self.assertIn("ERR-END\ufffd", details)
                self.assertNotIn("A" * 65, details)
                self.assertNotIn("B" * 65, details)
                self.assertLess(len(details), 600)

    def test_failure_retains_original_exception_and_stack_before_reaping_child(self):
        class FixtureFailure(RuntimeError):
            pass
        for isolated in (False, True):
            with self.subTest(isolated=isolated), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                target = root / "synthetic_wait.py"
                target.write_text("import time\ntime.sleep(20)\n")
                capture = StartupCapture(root / "diagnostics", label="waiting-fixture",
                    sample_seconds=0.03, isolated=isolated)
                command = [sys.executable, *(["-I", "-S"] if isolated else []), str(target)]
                stream = subprocess.PIPE if isolated else subprocess.DEVNULL
                failure = FixtureFailure("synthetic startup failure")
                with self.assertRaises(FixtureFailure) as raised:
                    with ExitStack() as resources, capture:
                        resources.callback(capture.close)
                        process = capture.popen(command, env={"PATH": os.defpath}, stdout=stream, stderr=stream)
                        deadline = time.monotonic() + 2
                        while "still waiting for readiness" not in capture.failure_details() and time.monotonic() < deadline:
                            time.sleep(0.01)
                        if isolated:
                            import select
                            self.assertEqual(select.select([process.stderr], [], [], 0)[0], [],
                                "diagnostic stacks must not fill the unread product stderr pipe")
                        raise failure
                self.assertIs(raised.exception, failure)
                notes = "\n".join(failure.__notes__)
                self.assertIn("returncode=None", notes)
                self.assertIn("synthetic_wait.py", notes)
                self.assertIn("still waiting for readiness", notes)
                self.assertIsNotNone(process.poll())

    def test_each_child_has_a_fresh_readiness_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            processes = [object(), object()]
            def factory(_command, **_kwargs):
                return processes.pop(0)
            capture = StartupCapture(Path(temporary), label="fresh", popen=factory)
            first = capture.popen(["python", "worker.py"], env={}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            capture.ready(first)
            second = capture.popen(["python", "worker.py"], env={}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.assertIsNot(first, second)
            self.assertTrue(capture.children[0]["startup"].marker.exists())
            self.assertFalse(capture.children[1]["startup"].marker.exists())
