from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


class AgentLabRagTrialTests(unittest.TestCase):
    def setUp(self):
        from rag_ime.agent_lab.rag_trial import AgentLabRagTrialAdapter, AgentLabRagTrialAssets
        self.adapter_type, self.assets_type = AgentLabRagTrialAdapter, AgentLabRagTrialAssets
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.host = self.root / "private-host"
        self.host.mkdir()
        for name in ("prepared", "answers", "qrels", "retrieval"):
            (self.host / f"{name}.json").write_text(json.dumps({"private": "GOLD-DOCUMENT-ANSWER-SENTINEL"}))
        config = self.host / "agent-config"
        config.mkdir()
        (config / "auth.json").write_text(json.dumps({"openai-codex": {"access": "SECRET-AUTH"}}))
        payload = self.host / "pi-payload"
        payload.mkdir()
        (payload / "manifest.json").write_text('{}')
        self.pricing = self.host / "pricing.json"
        self.pricing.write_text(json.dumps({"gpt": {"models": [
            {"id": "gpt-5.6-luna", "provider": "openai-codex", "cost": {"input": 0.2, "cacheRead": 0.02, "cacheWrite": 0, "output": 1.2}},
            {"id": "gpt-5.6-sol", "provider": "openai-codex", "cost": {"input": 5, "cacheRead": 0.5, "cacheWrite": 0, "output": 30}},
        ]}}))
        self.assets = AgentLabRagTrialAssets(
            prepared_path=self.host / "prepared.json", answer_cases_path=self.host / "answers.json",
            answer_evidence_qrels_path=self.host / "qrels.json", retrieval_report_path=self.host / "retrieval.json",
            source_agent_config=config, pi_runtime_payload=payload,
            pricing_config=self.pricing, pricing_published_date="2026-09-01", judge_model="gpt-5.6-sol")
        self.adapter = AgentLabRagTrialAdapter(self.root / "artifacts", assets=self.assets)
        runtime = patch("rag_ime.managed_pi_runtime.snapshot_managed_pi_runtime_payload", side_effect=lambda path, **_: SimpleNamespace(
            manifest_sha256=hashlib.sha256((Path(path) / "manifest.json").read_bytes()).hexdigest()))
        runtime.start()
        self.addCleanup(runtime.stop)
        self.spec = {"provider": "openai-codex", "model": "gpt-5.6-luna", "thinking": "max",
            "evaluationSplit": "validation", "candidatePromptText": "PRIVATE-PROMPT", "promptProfile": "incumbent", "agenticSupplementalLimit": 3}

    def prepared(self, job_id="lab-trial:fixture"):
        return self.adapter.prepare(self.spec, job_id)

    def raw_report(self):
        return {"schemaVersion": "rag-ime.rag-agent-ablation-run.v1", "passed": False,
            "acceptanceStatus": "validation-accepted-not-formal", "cleanupPassed": True,
            "candidateDecision": {"accepted": True, "decision": "keep"},
            "evaluation": {"caseCount": 4, "caseIds": ["SECRET-CASE"], "caseSetSha256": "a" * 64},
            "answerJudge": {"accepted": True, "evidenceCases": [{"answer": "GOLD-DOCUMENT-ANSWER-SENTINEL"}]},
            "lanes": [{"lane": lane, "score": {"agentMetrics": {"highLevelAnswerCorrectnessRate": 1.0, "citationFactCoverage": 1.0,
                "rawAnswer": "GOLD-DOCUMENT-ANSWER-SENTINEL", "unknownNumber": 999}, "metricDenominators": {"highLevelCases": 4, "citationFacts": 9},
                "answerCases": ["GOLD-DOCUMENT-ANSWER-SENTINEL"]}} for lane in ("baseline", "skill", "tuned", "agentic")],
            "privatePath": str(self.host), "failure": "SECRET-ERROR", "reportSha256": "b" * 64}

    def runtime_cost_fixture(self, run_root):
        usage = {"input": 1000, "cacheRead": 2000, "cacheWrite": 0, "output": 500}
        with closing(sqlite3.connect(run_root / "agent.sqlite")) as conn:
            conn.execute("CREATE TABLE agent_sessions (model_profile TEXT, session_file TEXT)")
            conn.execute("CREATE TABLE agent_runtime_events (event_type TEXT, metrics_json TEXT)")
            for model, rates, failed in (("gpt-5.6-luna", (0.2, 0.02, 1.2), False), ("gpt-5.6-sol", (5, 0.5, 30), True)):
                cost = {"input": usage["input"] * rates[0] / 1e6, "cacheRead": usage["cacheRead"] * rates[1] / 1e6,
                    "cacheWrite": 0, "output": usage["output"] * rates[2] / 1e6}
                cost["total"] = sum(cost.values())
                transcript = run_root / f"{model}.jsonl"
                transcript.write_text(json.dumps({"type": "message", "message": {"role": "assistant", "provider": "openai-codex", "model": model,
                    "content": "PRIVATE-TRANSCRIPT", "usage": {**usage, "cost": cost}}}) + "\n")
                conn.execute("INSERT INTO agent_sessions VALUES (?,?)", (f"openai-codex/{model}", str(transcript)))
                conn.execute("INSERT INTO agent_runtime_events VALUES (?,?)", ("provider_request_failed" if failed else "provider_request_completed",
                    json.dumps({"provider": "openai-codex", "model": model, "usage": usage})))
            conn.commit()

    def test_new_default_controls_and_judge_freeze_current_model_without_running(self):
        from dataclasses import replace
        from scripts import run_rag_agent_ablation as runner
        assets = replace(self.assets, judge_model=self.assets_type.__dataclass_fields__["judge_model"].default)
        adapter = self.adapter_type(self.root / "latest", assets=assets)
        spec = {key:value for key,value in self.spec.items() if key != "model"}
        with patch.object(runner, "_run", side_effect=AssertionError("prepare must not run")) as run:
            frozen = adapter.prepare(spec, "latest-default")
        run.assert_not_called()
        self.assertEqual(frozen["publicSpec"]["model"], "gpt-6.1-sol")
        self.assertEqual(frozen["publicSpec"]["judgeModel"], "gpt-6.1-sol")

    def test_prepare_is_read_only_and_freezes_bounded_controls_without_public_secrets(self):
        from scripts import run_rag_agent_ablation as runner
        with patch.object(runner, "_run") as run:
            frozen = self.prepared()
        run.assert_not_called()
        self.assertTrue(all(path.is_file() for path in runner._RUNTIME_CONTRACT_PATHS))
        self.assertIn(runner.ROOT / "rag_ime/pi/host_client.py", runner._RUNTIME_CONTRACT_PATHS)
        self.assertFalse((self.root / "artifacts").exists())
        public = json.dumps(frozen["publicSpec"])
        for secret in (str(self.host), "PRIVATE-PROMPT", "GOLD-DOCUMENT-ANSWER-SENTINEL", "SECRET-AUTH"):
            self.assertNotIn(secret, public)
        self.assertEqual("gpt-5.6-sol", frozen["publicSpec"]["judgeModel"])
        self.assertEqual("gpt-5.6-luna", frozen["publicSpec"]["model"])
        self.assertFalse(frozen["publicSpec"]["formalAcceptanceEligible"])

    def test_missing_assets_are_unavailable_and_frontend_cannot_supply_paths_or_gold(self):
        with self.assertRaisesRegex(ValueError, "assets.*configured"):
            self.adapter_type(self.root / "unconfigured").prepare({}, "lab-trial:empty")
        for bad in ({"gold": "secret"}, {"preparedPath": "/tmp/arbitrary"}, {"evaluationSplit": "held_out"},
                    {"provider": "foreign"}, {"thinking": "high"}, {"model": "invented"}, {"agenticSupplementalLimit": True},
                    {"candidatePromptText": "x" * 16001}, {"judgeModel": "gpt-5.6-luna"}):
            with self.subTest(bad=tuple(bad)), self.assertRaises(ValueError):
                self.adapter.prepare({**self.spec, **bad}, "lab-trial:invalid")

    def test_queued_asset_drift_prevents_any_runner_admission(self):
        from scripts import run_rag_agent_ablation as runner
        for filename in ("qrels.json", "pi-payload/manifest.json"):
            with self.subTest(filename=filename):
                frozen = self.prepared()
                path = self.host / filename
                before = path.read_bytes()
                path.write_bytes(before + b" ")
                with patch.object(runner, "_run") as run, self.assertRaisesRegex(ValueError, "changed"):
                    self.adapter.execute(frozen["privateInput"], Mock(), lambda: False)
                run.assert_not_called()
                path.write_bytes(before)

    def test_pre_execution_cancellation_does_not_create_an_execution_or_call_runner(self):
        from scripts import run_rag_agent_ablation as runner
        frozen = self.prepared()
        with patch.object(runner, "_run") as run, self.assertRaises(runner.RagEvaluationCancelled):
            self.adapter.execute(frozen["privateInput"], Mock(), lambda: True)
        run.assert_not_called()
        self.assertFalse(Path(frozen["privateInput"]["runRoot"]).exists())

    def test_execute_calls_existing_runner_once_uses_frozen_input_and_retains_private_evidence(self):
        from scripts import run_rag_agent_ablation as runner
        frozen, observer = self.prepared(), Mock()
        def execute(run_root, **kwargs):
            self.assertFalse(kwargs["calibration_no_metal"])
            self.assertTrue(kwargs["development_only"])
            self.assertTrue(kwargs["answer_only"])
            self.assertFalse(kwargs["resume_checkpoint"])
            self.assertEqual("gpt-5.6-sol", kwargs["judge_model"])
            self.assertEqual("PRIVATE-PROMPT", kwargs["candidate_prompt"].text)
            self.assertNotEqual(self.host / "qrels.json", kwargs["answer_evidence_qrels_path"])
            self.assertEqual((self.host / "qrels.json").read_bytes(), kwargs["answer_evidence_qrels_path"].read_bytes())
            kwargs["on_session"]("actual-session")
            kwargs["on_turn"]("actual-session", "actual-turn")
            self.runtime_cost_fixture(run_root)
            return self.raw_report()
        with patch.object(runner, "_run", side_effect=execute) as run:
            result = self.adapter.execute(frozen["privateInput"], observer, lambda: False)
        run.assert_called_once()
        self.assertEqual("rag-ime.agent-lab-trial-result.v1", result["schemaVersion"])
        self.assertEqual("completed", result["status"])
        self.assertEqual("keep", result["signals"]["qualityVerdict"])
        self.assertEqual(4, result["caseCount"])
        self.assertEqual(1.0, result["metrics"]["agentic"]["citationFactCoverage"])
        self.assertEqual("0.02184", result["cost"]["totalCostUsd"])
        self.assertEqual(1, result["cost"]["failedRequestCount"])
        self.assertFalse(result["cost"]["providerBillAvailable"])
        observer.bind_session.assert_any_call("actual-session", "")
        observer.bind_session.assert_any_call("actual-session", "actual-turn")
        public = json.dumps(result)
        for secret in (str(self.host), "GOLD-DOCUMENT-ANSWER-SENTINEL", "SECRET-CASE", "SECRET-ERROR", "PRIVATE-PROMPT", "PRIVATE-TRANSCRIPT", "unknownNumber"):
            self.assertNotIn(secret, public)
        root = Path(frozen["privateInput"]["runRoot"])
        self.assertTrue((root / "agent.sqlite").is_file())
        self.assertEqual(self.raw_report(), json.loads((root / "report.json").read_text()))
        self.assertTrue((root / "cost-receipt.json").is_file())
        self.assertIn("actual-turn", (root / "bindings.jsonl").read_text())
        for name in ("report.json", "cost-receipt.json", "bindings.jsonl"):
            self.assertEqual(0, (root / name).stat().st_mode & 0o077)

    def test_incomplete_cost_stays_unavailable_without_discarding_report_or_retry(self):
        from scripts import run_rag_agent_ablation as runner
        frozen = self.prepared()
        with patch.object(runner, "_run", return_value=self.raw_report()) as run:
            result = self.adapter.execute(frozen["privateInput"], Mock(), lambda: False)
        self.assertFalse(result["cost"]["available"])
        self.assertNotIn("totalCostUsd", result["cost"])
        self.assertTrue((Path(frozen["privateInput"]["runRoot"]) / "report.json").is_file())
        run.assert_called_once()

    def test_missing_measured_metrics_cannot_become_keep_from_a_claimed_decision(self):
        from scripts import run_rag_agent_ablation as runner
        frozen, report = self.prepared(), self.raw_report()
        report["lanes"][0]["score"]["agentMetrics"] = {"untrusted": "claimed perfect score"}
        with patch.object(runner, "_run", return_value=report):
            result = self.adapter.execute(frozen["privateInput"], Mock(), lambda: False)
        self.assertFalse(result["signals"]["scoreEligible"])
        self.assertEqual("unavailable", result["signals"]["qualityVerdict"])

    def test_actual_runner_lane_observes_pi_binding_aborts_and_cleans_before_app_result(self):
        from scripts import run_rag_agent_ablation as runner
        frozen, observer = self.prepared(), Mock()
        sandbox, service, server, gateway = Mock(), Mock(), Mock(), Mock()
        server.transport_cursor.return_value = 0
        sandbox.create_run.return_value = {"runId": "a" * 32}
        sandbox.root = Path(frozen["privateInput"]["runRoot"]) / "knowledge-runs"
        sandbox.create_base.return_value = {"configRevision": 1}
        sandbox.cleanup.return_value = {"deleted": True}
        service.create_session.return_value = {"session": {"id": "pi-lane-session"}}
        service.ensure_runtime.return_value = {"state": {"model": {"provider": "openai-codex", "id": "gpt-5.6-luna"}, "thinkingLevel": "max"}}
        service.prompt.return_value = {"turnId": "pi-lane-turn"}
        config_hash = runner._sha256_json({})
        fixtures = {
            "_load_prepared": {"cases": []},
            "_read_json_object": {"cases": [], "validationSelection": {"winner": {"config": {}}, "frozenConfigSha256": config_hash}, "chunking": {}},
            "_reconstruct_frozen_slice": ([], [{"documentId": "doc", "text": "fixture"}], {"benchmarkId": "fixture", "sourceSha256": "c" * 64}),
            "_development_exclusion": {"caseIds": []},
            "select_agent_answer_cases": [{"queryId": "q1", "query": "fixture question"}],
            "_answer_case_manifest": {"manifestSha256": "d" * 64, "selectedCaseSetSha256": "e" * 64, "answerCaseSetSha256": "f" * 64},
            "_production_baseline_record": {"config": {}, "configSha256": config_hash},
            "_lane_prompt": "fixture prompt",
            "_embedding_environment_from_report": {"RAG_IME_KNOWLEDGE_DENSE_BACKEND": "fixture"},
            "_require_actual_metal_runtime": None, "embedding_provider_from_env": Mock(), "embedding_provider_info": {"provider": "fixture"},
            "_frozen_reranker": (None, {"required": False, "accepted": True}),
            "_copy_openai_codex_agent_config": None, "_pin_evaluation_agent_config": {}, "_isolated_runtime_config": Mock(),
            "_public_pi_runtime_identity": {"identitySha256": "a" * 64}, "_evaluation_configuration_identity": {"identitySha256": "b" * 64},
            "_require_semantic_dense_runtime": {"accepted": True}, "RagBenchmarkSandbox": sandbox,
            "RagBenchmarkAgentGateway": gateway, "ControlToolGateway": Mock(), "_start_rag_benchmark_gateway": server, "AgentService": service,
        }
        read_json = runner._read_json_object
        with ExitStack() as stack:
            for name, value in fixtures.items():
                if name == "_read_json_object":
                    stack.enter_context(patch.object(runner, name, side_effect=lambda path, fixture=value: read_json(path) if Path(path).name == "lane-checkpoint.json" else fixture))
                else:
                    stack.enter_context(patch.object(runner, name, return_value=value))
            judge = stack.enter_context(patch.object(runner, "_run_answer_judge"))
            result = self.adapter.execute(frozen["privateInput"], observer, lambda: service.prompt.called)
        self.assertEqual("cancelled", result["status"])
        self.assertEqual("unavailable", result["signals"]["qualityVerdict"])
        observer.bind_session.assert_any_call("pi-lane-session", "")
        observer.bind_session.assert_any_call("pi-lane-session", "pi-lane-turn")
        service.create_session.assert_called_once()
        service.prompt.assert_called_once()
        service.abort.assert_called_once_with("pi-lane-session")
        service.close.assert_called_once()
        gateway.unbind_lineage.assert_called_once_with("pi-lane-session")
        sandbox.cleanup.assert_called_once()
        sandbox.close.assert_called_once()
        server.close.assert_called_once()
        judge.assert_not_called()
        checkpoint = json.loads((Path(frozen["privateInput"]["runRoot"]) / "lane-checkpoint.json").read_text())
        history = runner._lane_checkpoint_attempt_history(checkpoint, "baseline")
        self.assertEqual(hashlib.sha256(b"pi-lane-turn").hexdigest(), history[0]["turnSha256"])

    def test_repeat_execute_does_not_admit_paid_retry(self):
        from scripts import run_rag_agent_ablation as runner
        from rag_ime.agent_lab.trial_execution import AgentLabTrialExecutionInterrupted
        frozen = self.prepared()
        with patch.object(runner, "_run", return_value=self.raw_report()) as run:
            self.adapter.execute(frozen["privateInput"], Mock(), lambda: False)
            with self.assertRaises(AgentLabTrialExecutionInterrupted):
                self.adapter.execute(frozen["privateInput"], Mock(), lambda: False)
        run.assert_called_once()

    def test_application_cancel_stays_pending_until_runner_cleanup_and_retains_partial_evidence(self):
        from scripts import run_rag_agent_ablation as runner
        from rag_ime.agent_lab.trial_execution import AgentLabTrialApplication
        from rag_ime.agent_lab.trials import AgentLabTrialStore
        entered, cleanup = threading.Event(), threading.Event()
        observed_cancel = threading.Event()
        app = AgentLabTrialApplication(AgentLabTrialStore(self.root / "trials.sqlite"), {self.adapter.scene_id: self.adapter}, start_workers=False)
        self.addCleanup(app.close)
        def execute(run_root, **kwargs):
            kwargs["on_session"]("pi-owned")
            kwargs["on_turn"]("pi-owned", "pi-turn")
            entered.set()
            self.assertTrue(cleanup.wait(3))
            if kwargs["cancelled"]():
                observed_cancel.set()
            self.runtime_cost_fixture(run_root)
            return {**self.raw_report(), "status": "cancelled", "scoreEligible": False}
        with patch.object(runner, "_run", side_effect=execute) as run:
            job_id = app.start("click", self.adapter.scene_id, self.spec)["job"]["jobId"]
            thread = threading.Thread(target=app.run_job, args=(job_id,))
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual("cancelling", app.cancel(job_id)["job"]["state"])
                self.assertEqual("cancelling", app.read(job_id)["job"]["state"])
            finally:
                cleanup.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertTrue(observed_cancel.is_set())
            self.assertEqual("cancelled", app.read(job_id)["job"]["state"])
            retained = app.read(job_id)["job"]["result"]
            self.assertEqual("cancelled", retained["status"])
            self.assertEqual("unavailable", retained["signals"]["qualityVerdict"])
            self.assertFalse(retained["signals"]["scoreEligible"])
            self.assertTrue(retained["cost"]["available"])
            self.assertEqual("0.02184", retained["cost"]["totalCostUsd"])
            private = app.store.job_input(job_id)["privateInput"]
            self.assertTrue((Path(private["runRoot"]) / "cost-receipt.json").is_file())
            app.run_job(job_id)
        run.assert_called_once()

    def test_uncertain_cleanup_is_interrupted_and_preserves_report_without_reading_live_cost(self):
        from scripts import run_rag_agent_ablation as runner
        from rag_ime.agent_lab.trial_execution import AgentLabTrialExecutionInterrupted
        frozen = self.prepared()
        raw = {**self.raw_report(), "status": "interrupted", "executionSettled": False}
        with patch.object(runner, "_run", return_value=raw), patch("scripts.build_agent_lab_cost_receipt_from_runtime_db.build_multi_model_cost_receipt") as costs:
            with self.assertRaises(AgentLabTrialExecutionInterrupted):
                self.adapter.execute(frozen["privateInput"], Mock(), lambda: False)
        costs.assert_not_called()
        self.assertEqual(raw, json.loads((Path(frozen["privateInput"]["runRoot"]) / "report.json").read_text()))


if __name__ == "__main__":
    unittest.main()
