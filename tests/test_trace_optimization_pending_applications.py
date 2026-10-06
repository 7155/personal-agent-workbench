from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from rag_ime.trace_optimization_versions import digest
from tests import test_trace_optimization_application as fixture_module
from tests import test_trace_optimization as comparison_fixture


class PendingApplicationProjectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.TraceOptimizationApplicationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.candidate = self.fixture.prepare()
        self.report_id = self.fixture.report["reportId"]
        # Projection/receipt tests consume synthetic Host evidence. Actual
        # managed command execution is covered by the separate native test.
        comparison = self.record_fixture_validation()
        self.assertTrue(comparison["comparable"], comparison)
        self.assertEqual(comparison["decision"], "kept", comparison)
        self.action = {"operation": "candidate_action", "candidateId": self.candidate["candidateId"],
            "clientRequestId": "apply-once", "action": "apply"}

    def record_fixture_validation(self, scores=(1, 1), suffix="", candidate_version=None):
        helper = comparison_fixture.TraceOptimizationComparisonTests()
        helper.path = self.fixture.db
        helper.trials = self.fixture.trials
        versions = {kind: "not-used:local-command" for kind in ("tool", "skill", "prompt", "workflow", "model")}
        before_versions = {**versions, "tool": self.candidate["parentVersionRef"]}
        after_versions = {**versions, "tool": candidate_version or self.candidate["candidateVersionRef"]}
        with patch("rag_ime.agent_workspace.subprocess.Popen",
                   side_effect=AssertionError("projection fixture must not execute a command")) as process:
            before = helper.trial(self.candidate, "baseline", scores=(0, 0), cost=None,
                                  suffix=suffix, loaded_versions=before_versions)
            after = helper.trial(self.candidate, "candidate", scores=scores, cost=None,
                                 suffix=suffix, loaded_versions=after_versions)
            result = self.fixture.candidates.record_validation(self.candidate["candidateId"],
                client_request_id="projection-validation" + suffix,
                baseline_trial_id=before, candidate_trial_id=after)
            process.assert_not_called()
        return result

    def test_failed_quality_fixture_does_not_enable_application(self):
        result = self.record_fixture_validation(scores=(0, 0), suffix="-failed-quality")
        self.assertEqual(result["decision"], "rejected", result)
        with self.assertRaisesRegex(ValueError, "no validated owning action"):
            self.fixture.app.command(self.report_id, self.action)

    def test_wrong_loaded_version_fixture_does_not_enable_application(self):
        result = self.record_fixture_validation(suffix="-wrong-version", candidate_version="tool:unregistered")
        self.assertFalse(result["comparable"], result)
        with self.assertRaisesRegex(ValueError, "no validated owning action"):
            self.fixture.app.command(self.report_id, self.action)

    def test_lost_settlement_projects_original_reservation_and_disables_repeat_actions(self):
        with patch.object(self.fixture.versions, "finish_application", side_effect=sqlite3.OperationalError("lost settlement")):
            with self.assertRaises(sqlite3.OperationalError):
                self.fixture.app.command(self.report_id, self.action)
        self.assertIn(".upper()", (self.fixture.root / "baseline" / "tool.py").read_text())
        projected = self.fixture.reports.get(self.report_id)["optimization"]
        self.assertEqual(len(projected.get("pendingApplications", [])), 1)
        pending = projected["pendingApplications"][0]
        self.assertEqual(pending["status"], "applying")
        self.assertEqual(pending["candidateId"], self.candidate["candidateId"])
        self.assertEqual(pending["versionRef"], self.candidate["candidateVersionRef"])
        self.assertEqual(projected["applications"], [])
        self.assertEqual(projected["candidates"][0]["availableActions"], [])
        self.assertNotIn("details", pending)
        self.assertNotIn("destinationPath", pending)
        with patch.object(self.fixture.versions, "apply_file", side_effect=AssertionError("must not execute again")):
            replay = self.fixture.app.command(self.report_id, self.action)["optimization"]
        self.assertEqual(replay["pendingApplications"], projected["pendingApplications"])
        with self.assertRaises(ValueError):
            self.fixture.app.command(self.report_id, {**self.action, "clientRequestId": "another-apply"})
        with self.assertRaises(ValueError):
            self.fixture.app.command(self.report_id, {**self.action, "action": "keep_original", "clientRequestId": "pretend-kept"})
        with self.assertRaises(ValueError):
            self.fixture.app.command(self.report_id, {"operation": "run_candidate", "candidateId": self.candidate["candidateId"], "clientRequestId": "repeat-execution"})

    def test_interrupted_receipt_binds_once_without_duplicate_pending_display(self):
        with patch.object(self.fixture.versions, "apply_file", side_effect=TimeoutError("owner outcome unknown")):
            report = self.fixture.app.command(self.report_id, self.action)
        projected = report["optimization"]
        self.assertEqual(len(projected["applications"]), 1)
        self.assertEqual(projected["applications"][0]["status"], "interrupted")
        self.assertEqual(projected["pendingApplications"], [])
        self.assertEqual(projected["candidates"][0]["availableActions"], [])
        with patch.object(self.fixture.versions, "apply_file", side_effect=AssertionError("must not execute again")):
            replay = self.fixture.app.command(self.report_id, self.action)["optimization"]
        self.assertEqual(replay["applications"], projected["applications"])

    def test_other_report_pending_receipt_does_not_leak_or_disable_this_candidate(self):
        comparison = self.fixture.candidates.read(self.report_id)["comparisons"][0]
        request = {"candidateId": "another-candidate", "reportId": "another-report", "comparisonId": comparison["comparisonId"],
            "targetKind": self.candidate["targetKind"], "targetRef": self.candidate["targetRef"],
            "versionRef": "another-version", "action": "apply"}
        receipt, _ = self.fixture.versions.begin_application("foreign-report", request)
        result = self.fixture.reports.get(self.report_id)["optimization"]
        self.assertEqual(result["pendingApplications"], [])
        self.assertIn("apply", result["candidates"][0]["availableActions"])
        self.assertNotIn(receipt["receiptRef"], str(result))

    def test_pending_receipt_tampering_is_not_projected_as_trusted_context(self):
        comparison = self.fixture.candidates.read(self.report_id)["comparisons"][0]
        request = {"candidateId": self.candidate["candidateId"], "reportId": self.report_id, "comparisonId": comparison["comparisonId"],
            "targetKind": self.candidate["targetKind"], "targetRef": self.candidate["targetRef"],
            "versionRef": self.candidate["candidateVersionRef"], "action": "apply"}
        receipt, _ = self.fixture.versions.begin_application("tamper-test", request)
        with sqlite3.connect(self.fixture.db) as conn:
            conn.execute("UPDATE trace_optimization_application_receipts SET request_hash=? WHERE receipt_ref=?",
                (digest({"forged": True}), receipt["receiptRef"]))
        with self.assertRaisesRegex(ValueError, "Pending application receipt"):
            self.fixture.reports.get(self.report_id)


if __name__ == "__main__":
    unittest.main()
