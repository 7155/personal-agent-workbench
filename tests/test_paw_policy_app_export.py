import tempfile
import os
from unittest.mock import patch
import unittest
import zipfile
from pathlib import Path
from contextlib import ExitStack

from tests.subprocess_startup import StartupCapture

from scripts.export_paw_policy_app import export_app, verify_app


class PolicyAppExportTests(unittest.TestCase):
    def test_clean_http_app_runs_the_frozen_business_cases(self):
        config = {"currency": "CNY", "limit": 800, "receiptRequired": True, "unknownCurrency": "manual"}
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as resources:
            capture = StartupCapture(Path(temporary) / "startup-diagnostics", label="policy-export", isolated=True,
                match=lambda command: isinstance(command, (list, tuple)) and len(command) > 3
                    and list(command[1:3]) == ["-I", "-S"] and Path(command[3]).name == "server.py")
            resources.callback(capture.close)
            archive = Path(temporary) / "app.zip"
            export_app(config, archive, provenance={"dataClass": "test"})
            with capture, patch("scripts.export_paw_policy_app.subprocess.Popen", side_effect=capture.popen):
                result = verify_app(archive, config, repeats=1)
            self.assertEqual(result["startupPasses"], 1)
            self.assertEqual(result["businessPasses"], 4)
            self.assertTrue(result["parity"])
            self.assertTrue(result["stdlibOnly"])
            with zipfile.ZipFile(archive) as zipped:
                self.assertEqual(set(zipped.namelist()), {"policy.py", "server.py", "index.html", "config.json", "export.json", "README.txt"})
                self.assertNotIn("expected", zipped.read("config.json").decode())

    @unittest.skipUnless(os.name == "posix", "high pipe descriptors use POSIX duplication")
    def test_export_verifier_accepts_high_numbered_stdout_descriptor(self):
        import fcntl

        def high_descriptor_process(*args, **kwargs):
            process = capture.popen(*args, **kwargs)
            duplicate = fcntl.fcntl(process.stdout.fileno(), fcntl.F_DUPFD, 2048)
            process.stdout.close()
            process.stdout = os.fdopen(duplicate, "r")
            return process

        config = {"currency": "CNY", "limit": 800, "receiptRequired": True, "unknownCurrency": "manual"}
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as resources:
            capture = StartupCapture(Path(temporary) / "startup-diagnostics", label="policy-export", isolated=True,
                match=lambda command: isinstance(command, (list, tuple)) and len(command) > 3
                    and list(command[1:3]) == ["-I", "-S"] and Path(command[3]).name == "server.py")
            resources.callback(capture.close)
            archive = Path(temporary) / "app.zip"
            export_app(config, archive, provenance={"dataClass": "test"})
            with capture, patch("scripts.export_paw_policy_app.subprocess.Popen", side_effect=high_descriptor_process):
                result = verify_app(archive, config, repeats=1)
            self.assertEqual(result["startupPasses"], 1)
            self.assertEqual(result["businessPasses"], 4)

    def test_wrong_candidate_cannot_be_exported_as_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "quality"):
                export_app({"currency": "CNY", "limit": 500, "receiptRequired": True, "unknownCurrency": "manual"}, Path(temporary) / "app.zip", provenance={})


if __name__ == "__main__":
    unittest.main()
