from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rag_ime.agent_lab.trial_execution import AgentLabTrialApplication
from rag_ime.agent_lab.trials import AgentLabTrialStore
from rag_ime.agent_workspace import WorkspaceHarness
from rag_ime.agent_execution_policy import DANGEROUS_AUTO_APPROVE_TOOL_PROFILE, FULL_TRUST_EXECUTION_MODE
from rag_ime.eval_run_store import EvalRunStore
from rag_ime.trace_diagnostics import TraceDiagnosticReportStore
from rag_ime.trace_optimization import TraceOptimizationStore
from rag_ime.trace_optimization_application import TraceOptimizationApplication
from rag_ime.trace_optimization_versions import TraceOptimizationVersionStore
from rag_ime.trace_store import TraceStore
from tests.test_trace_diagnostics import _inspection_fixture


class TraceOptimizationApplicationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.root / "test.sqlite"
        self.versions = TraceOptimizationVersionStore(self.db)
        self.candidates = TraceOptimizationStore(self.db,
            version_reader=self.versions.resolve, application_reader=self.versions.application_receipt)
        self.reports = TraceDiagnosticReportStore(self.db, optimization_store=self.candidates)
        self.trials = AgentLabTrialStore(self.db)
        agent = SimpleNamespace(sessions=SimpleNamespace(db_path=self.db),
            trace_optimization_versions=self.versions, trace_optimizations=self.candidates,
            trace_diagnostic_reports=self.reports, trace_store=TraceStore(self.db), eval_runs=EvalRunStore(self.db),
            background_jobs=SimpleNamespace(workspace_harness=WorkspaceHarness()),
            _eval_lab_trial_adapters={}, _eval_lab_trial_application=None,
            _trace_optimization_extensions=None, _trace_optimization_capability_reader=lambda: [],
            _trace_diagnostic_project_roots=lambda targets: [str(self.root)], _trial_store=lambda: self.trials)
        self.app = TraceOptimizationApplication(agent)
        self.runner = AgentLabTrialApplication(self.trials, agent._eval_lab_trial_adapters, start_workers=False)
        self.addCleanup(self.runner.close)
        agent.eval_lab_trial_start = lambda value: self.runner.start(value["clientRequestId"], value["sceneId"], value["spec"])
        agent.eval_lab_trial_cancel = lambda value: self.runner.cancel(value["jobId"])
        self.project_id = self.app.ensure_project([str(self.root)])
        packet = _inspection_fixture()
        self.report = self.reports.create(diagnostic_session_id="diagnostic:local", title="真实工具用例",
            targets=packet["targets"], inspection=packet, optimization_project_id=self.project_id)

    def register(self, body, role):
        path = self.root / role / "tool.py"
        path.parent.mkdir()
        path.write_text(body)
        return self.app.command(self.report["reportId"], {"operation": "register_version", "clientRequestId": role,
            "input": {"targetKind": "tool", "targetRef": "tool:capitalize", "sourcePath": str(path),
                "destinationPath": str(self.root / "baseline" / "tool.py")}})

    def prepare(self):
        before = self.register("import json,os\nprint(json.loads(os.environ['PAW_TRACE_INPUT'])['word'])\n", "baseline")
        after = self.register("import json,os\nprint(json.loads(os.environ['PAW_TRACE_INPUT'])['word'].upper())\n", "candidate")
        plan_path = self.root / "cases.json"
        plan_path.write_text(json.dumps({"cases": [
            {"caseId": "hello", "command": ["/usr/bin/python3", "{version}/tool.py"], "input": {"word": "hello"}, "expectedStdout": "HELLO"},
            {"caseId": "world", "command": ["/usr/bin/python3", "{version}/tool.py"], "input": {"word": "world"}, "expectedStdout": "WORLD"}],
            "timeoutSeconds": 5}))
        plan = self.app.command(self.report["reportId"], {"operation": "register_plan", "clientRequestId": "plan", "input": {"sourcePath": str(plan_path)}})
        return self.app.command(self.report["reportId"], {"operation": "prepare_candidate", "clientRequestId": "candidate-one",
            "input": {"parentVersionRef": before["versionRef"], "candidateVersionRef": after["versionRef"],
                "planRef": plan["planRef"], "evidenceIds": ["observation:observation:session:a"],
                "summary": "读取实际输入后统一大写", "expectedEffect": "通过两个冻结用例"}})

    @unittest.skipUnless(sys.platform == "darwin" and os.access("/usr/bin/sandbox-exec", os.X_OK),
                         "actual network-blocked fixture execution requires the macOS command harness")
    def test_real_registered_versions_execute_compare_and_persist_knowledge(self):
        candidate = self.prepare()
        self.app.command(self.report["reportId"], {"operation": "run_candidate", "candidateId": candidate["candidateId"], "clientRequestId": "run-one"})
        pair = self.app.jobs(self.report["reportId"])[0]
        for role in ("baseline", "candidate"):
            self.runner.run_job(pair[role]["jobId"])
            self.assertEqual(self.trials.read(pair[role]["jobId"])["job"]["state"], "completed")
        self.app.reconcile(self.report["reportId"])
        result = self.reports.get(self.report["reportId"])["optimization"]
        comparison = result["comparisons"][0]
        self.assertTrue(comparison["comparable"], comparison)
        self.assertEqual(comparison["decision"], "kept", comparison)
        self.assertEqual(comparison["validationScope"], "frozen_local_task_fixture")
        self.assertIn("apply", result["candidates"][0]["availableActions"])
        self.assertEqual(len(self.app.library({"projectId": self.project_id})["attempts"]), 1)
        # Retrying the exact admission only returns the two original jobs.
        self.app.command(self.report["reportId"], {"operation": "run_candidate", "candidateId": candidate["candidateId"], "clientRequestId": "run-one"})
        self.assertEqual(len(self.app.jobs(self.report["reportId"])), 1)
        # The last user action traverses the application owner to real files.
        action = {"operation": "candidate_action", "candidateId": candidate["candidateId"], "clientRequestId": "apply-one", "action": "apply"}
        applied = self.app.command(self.report["reportId"], action)
        self.assertEqual(applied["optimization"]["applications"][0]["status"], "applied")
        self.assertIn(".upper()", (self.root / "baseline" / "tool.py").read_text())
        replayed = self.app.command(self.report["reportId"], action)
        self.assertEqual(len(replayed["optimization"]["applications"]), 1)

    def test_missing_managed_harness_cannot_produce_an_improvement(self):
        self.app.agent.background_jobs.workspace_harness.sandbox_executable = self.root / "missing-sandbox-exec"
        candidate = self.prepare()
        self.app.command(self.report["reportId"], {"operation": "run_candidate",
            "candidateId": candidate["candidateId"], "clientRequestId": "unavailable-harness"})
        pair = self.app.jobs(self.report["reportId"])[0]
        workspace = self.app.agent.background_jobs.workspace_harness
        execute = workspace.execute_cancellable
        execution_errors = []

        def record_execution_error(*args, **kwargs):
            try:
                return execute(*args, **kwargs)
            except Exception as error:
                execution_errors.append(str(error))
                raise

        with patch("rag_ime.agent_workspace.subprocess.Popen",
                   side_effect=AssertionError("managed fixture must not execute without its sandbox")) as popen, \
                patch.object(workspace, "execute_cancellable", side_effect=record_execution_error):
            for role in ("baseline", "candidate"):
                self.runner.run_job(pair[role]["jobId"])
                job = self.trials.read(pair[role]["jobId"])["job"]
                self.assertEqual(job["state"], "failed")
                self.assertEqual(job["error"], "Scene execution failed; inspect the original execution records.")
            popen.assert_not_called()
        self.assertEqual(len(execution_errors), 2)
        for error in execution_errors:
            self.assertIn("macOS command harness is unavailable", error)
        self.app.reconcile(self.report["reportId"])
        result = self.reports.get(self.report["reportId"])["optimization"]
        self.assertFalse(result["comparisons"][0]["comparable"])
        self.assertEqual(result["comparisons"][0]["executionStatus"], "failed")
        self.assertEqual(result["comparisons"][0]["decision"], "rejected")
        self.assertNotIn("apply", result["candidates"][0]["availableActions"])

    def test_cancel_before_execution_cannot_produce_an_improvement(self):
        candidate = self.prepare()
        self.app.command(self.report["reportId"], {"operation": "run_candidate", "candidateId": candidate["candidateId"], "clientRequestId": "cancel-run"})
        self.app.command(self.report["reportId"], {"operation": "cancel_candidate", "candidateId": candidate["candidateId"], "clientRequestId": "cancel-click"})
        comparison = self.reports.get(self.report["reportId"])["optimization"]["comparisons"][0]
        self.assertFalse(comparison["comparable"])
        self.assertEqual(comparison["executionStatus"], "cancelled")

    def test_registry_rejects_changed_snapshots_and_outside_project(self):
        version = self.register("print('original')\n", "baseline")
        row = self.versions.get(version["versionRef"])
        (Path(row["snapshotPath"]) / row["entryPath"]).write_text("print('tampered')\n")
        with self.assertRaisesRegex(ValueError, "snapshot has changed"):
            self.versions.get(version["versionRef"])
        with self.assertRaisesRegex(ValueError, "outside"):
            self.versions.register(self.report["reportId"], {"targetKind": "tool", "targetRef": "bad", "sourcePath": "/usr/bin/true"}, roots=[str(self.root)])

    def test_plan_cannot_claim_component_loading_from_an_echoed_path(self):
        path = self.root / "invalid-plan.json"
        for argv in (["echo", "{version}/tool.py"], ["python3", "{version}/../outside.py"]):
            path.write_text(json.dumps({"cases": [{"caseId": "false-loading", "command": argv, "input": {}, "expectedStdout": "pass"}]}))
            with self.assertRaisesRegex(ValueError, "execute a registered file"):
                self.versions.register_plan(self.report["reportId"], {"sourcePath": str(path)}, roots=[str(self.root)])

    def test_full_trust_fixture_stop_interrupts_the_running_process(self):
        harness = WorkspaceHarness()
        prepared = harness.prepare_command({"mode": "coordinator", "workspaceRoots": [str(self.root)],
            "executionMode": FULL_TRUST_EXECUTION_MODE, "toolProfileVersion": DANGEROUS_AUTO_APPROVE_TOOL_PROFILE},
            {"command": "/usr/bin/python3 -c 'import time; time.sleep(20)'", "cwd": str(self.root), "timeoutSeconds": 25, "allowNetwork": False})
        self.assertTrue(prepared.unrestricted)  # Process cancellation, not sandbox isolation.
        stop = threading.Event()
        timer = threading.Timer(0.3, stop.set)
        timer.start()
        started = time.monotonic()
        try:
            receipt = harness.execute_cancellable(prepared, stop.is_set)
        finally:
            timer.cancel()
        self.assertTrue(stop.is_set())
        self.assertLess(time.monotonic() - started, 5)
        self.assertNotEqual(receipt["exitCode"], 0)

    def test_same_workspace_reopens_same_knowledge_project(self):
        self.assertEqual(self.app.ensure_project([str(self.root)]), self.project_id)
        library = self.app.library({})
        self.assertEqual(len(library["projects"]), 1)
        self.assertEqual(library["projects"][0]["projectId"], self.project_id)

    def test_distillation_is_persisted_in_report_and_reusable_knowledge(self):
        packet = _inspection_fixture()
        packet["intent"] = {"mode": "distill", "scopeMode": "selected", "focusAreas": ["skill"], "objective": "提炼恢复方法"}
        report = self.reports.create(diagnostic_session_id="diagnostic:distill", title="工作记录提炼",
            targets=packet["targets"], inspection=packet, optimization_project_id=self.project_id)
        self.app.agent._trace_optimization_extensions = SimpleNamespace(skills_list=lambda: {"runtimeAvailable": True, "items": []})
        prepared = self.app.command(report["reportId"], {"operation": "prepare_distillation", "clientRequestId": "distill-read"})
        ref = next(row["evidenceId"] for source in prepared["sources"] for row in source["records"])
        proposed = {"suggestionId": "method:one", "outcome": "new_skill", "title": "有边界的恢复方法",
            "reason": "选定记录说明恢复步骤需要沉淀", "evidenceIds": [ref], "capabilityNeeds": ["bounded-recovery"],
            "existingCapabilityIds": [], "proposedChange": "明确输入、终止条件和失败恢复。"}
        saved = self.app.command(report["reportId"], {"operation": "save_distillation", "clientRequestId": "distill-save", "input": {"proposals": [proposed]}})
        self.assertEqual(saved["distillation"]["items"][0]["outcome"], "new_skill")
        self.assertEqual(saved["distillation"]["items"][0]["validationStatus"], "candidate_draft")
        library = self.app.library({"projectId": self.project_id})
        self.assertEqual(len(library["patterns"]), 1)
        self.assertEqual(library["patterns"][0]["title"], "有边界的恢复方法")

    def test_unavailable_skill_catalog_does_not_authorize_a_claim_of_missing_capability(self):
        packet = _inspection_fixture()
        packet["intent"] = {"mode": "distill", "scopeMode": "selected", "focusAreas": ["skill"], "objective": ""}
        report = self.reports.create(diagnostic_session_id="diagnostic:incomplete", title="目录缺失",
            targets=packet["targets"], inspection=packet, optimization_project_id=self.project_id)
        prepared = self.app.command(report["reportId"], {"operation": "prepare_distillation", "clientRequestId": "incomplete-read"})
        self.assertFalse(prepared["inventoryComplete"])
        ref = next(row["evidenceId"] for source in prepared["sources"] for row in source["records"])
        saved = self.app.command(report["reportId"], {"operation": "save_distillation", "clientRequestId": "incomplete-save", "input": {"proposals": [{"suggestionId": "new", "outcome": "new_skill", "title": "待核对方法", "reason": "尚未对照完整目录", "evidenceIds": [ref], "capabilityNeeds": ["recovery"], "existingCapabilityIds": [], "proposedChange": "恢复说明"}]}})
        self.assertEqual(saved["distillation"]["items"][0]["outcome"], "experience_only")

    def test_pi_plan_is_dispatched_with_actual_session_binding_and_compared_by_host(self):
        from rag_ime.agent_sessions import AgentSessionStore
        from tests.test_trace_optimization_pi import Runtime

        sessions = AgentSessionStore(self.db)
        sessions.initialize()
        runtime = Runtime(sessions)
        runtime.adapter = self.app.pi_adapter
        self.app.agent.runtime = runtime
        self.app.pi_adapter.sessions = sessions
        original_settlement = runtime.await_turn_settled
        def settlement(session_id, turn_id, **kwargs):
            result = original_settlement(session_id, turn_id, **kwargs)
            policy = self.app.session_policy(sessions.get(session_id))
            if policy["promptSettings"]["systemInstructions"] == "original instructions":
                result["receipt"]["finalMessage"]["content"][0]["text"] = "different answer"
            return result
        runtime.await_turn_settled = settlement
        refs = []
        for name, body in (("original", "original instructions"), ("updated", "updated instructions")):
            path = self.root / (name + ".md")
            path.write_text(body)
            refs.append(self.versions.register(self.report["reportId"], {"targetKind": "prompt", "targetRef": "prompt:example",
                "sourcePath": str(path), "destinationPath": str(self.root / "original.md")}, roots=[str(self.root)])["versionRef"])
        source = self.root / "pi-plan.json"
        source.write_text(json.dumps({"executionKind": "pi_session", "model": {"provider": "fixture", "model": "fixture", "thinkingLevel": "high"},
            "allowedTools": [], "cases": [{"caseId": "one", "input": "Solve this task", "expectedText": "host-only expected answer"}],
            "timeoutSeconds": 5, "maxTaskTurns": 2}))
        plan = self.app.command(self.report["reportId"], {"operation": "register_plan", "clientRequestId": "pi-plan", "input": {"sourcePath": str(source)}})
        candidate = self.app.command(self.report["reportId"], {"operation": "prepare_candidate", "clientRequestId": "pi-candidate",
            "input": {"parentVersionRef": refs[0], "candidateVersionRef": refs[1], "planRef": plan["planRef"],
                "summary": "Bound prompt comparison", "expectedEffect": "Exact task answer", "evidenceIds": ["observation:observation:session:a"]}})
        self.app.command(self.report["reportId"], {"operation": "run_candidate", "clientRequestId": "pi-run", "candidateId": candidate["candidateId"]})
        for role in ("baseline", "candidate"):
            job = self.app.jobs(self.report["reportId"])[0][role]
            self.assertEqual(job["sceneId"], "trace-optimization-pi")
            self.runner.run_job(job["jobId"])
        self.app.reconcile(self.report["reportId"])
        comparison = self.reports.get(self.report["reportId"])["optimization"]["comparisons"][0]
        self.assertTrue(comparison["comparable"], comparison)
        self.assertEqual(comparison["decision"], "kept", comparison)
        self.assertEqual(len(runtime.calls), 2)
        self.assertTrue(all("host-only expected answer" not in call["prompt"] for call in runtime.calls))


if __name__ == "__main__":
    unittest.main()
