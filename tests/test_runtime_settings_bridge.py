from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from rag_ime.debug_server import DebugImeService, DebugServerConfig


class RuntimeSettingsBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-runtime-bridge-")
        self.service = DebugImeService(
            DebugServerConfig(db_path=Path(self.tmp.name) / "rag-ime.sqlite", seed_if_empty=False)
        )

    def tearDown(self) -> None:
        self.service.close()
        self.tmp.cleanup()

    def test_live_setting_changes_runtime_revision(self) -> None:
        before = self.service.management.revision()
        result = self.service.settings_update({"interaction.postCommit.maxCallsPer10s": 1})

        self.assertGreater(result["runtimeRevision"], before.runtime_revision)
        self.assertEqual(result["settingsRevision"], self.service.management.revision().settings_revision)
        self.assertGreater(result["auditId"], 0)

    def test_runtime_action_is_an_async_job(self) -> None:
        self.service.management._command_for_action = lambda _action: ["/usr/bin/true"]  # type: ignore[method-assign]
        response = self.service.management.start_runtime_action({"action": "restart_predictor"})
        job_id = response["jobId"]

        self.assertIn(response["job"]["status"], {"queued", "running", "succeeded"})
        for _ in range(50):
            status = self.service.management.runtime_job(job_id)
            if status["job"]["status"] != "queued" and status["job"]["status"] != "running":
                break
            time.sleep(0.01)
        self.assertEqual(status["job"]["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
