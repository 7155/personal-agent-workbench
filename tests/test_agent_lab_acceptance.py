"""Offline control/ledger tests. No authenticated SDK or provider is started."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import multiprocessing
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from rag_ime.agent_lab.acceptance import account_terminal_usage, oracle
from rag_ime.agent_lab.acceptance_budget import AcceptanceBudgetError, AcceptanceBudgetLedger, AcceptanceRunBudget, catalog_reservation


def _compete_for_budget(path, ready, go, results, index):
    ledger = AcceptanceBudgetLedger(path, limit_usd=100)
    ready.put(index)
    go.wait(15)
    try:
        ledger.admit("run-"+str(index), state_root=Path(path).parent/("run-"+str(index)), reserve_usd=60)
        results.put("admitted")
    except AcceptanceBudgetError:
        results.put("denied")


class AcceptancePureControlTests(unittest.TestCase):
    def test_default_cli_is_offline_without_database_network_or_process(self):
        from scripts import run_paw_acceptance
        output = io.StringIO()
        with patch("socket.socket", side_effect=AssertionError("network")), \
             patch("subprocess.run", side_effect=AssertionError("process")), \
             patch("sqlite3.connect", side_effect=AssertionError("database")), redirect_stdout(output):
            self.assertEqual(run_paw_acceptance.main([]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["providerCalls"], 0)
        self.assertEqual(len(result["caseIds"]), 8)

    def test_live_has_no_path_budget_model_or_credentials_defaults(self):
        from scripts import run_paw_acceptance
        args = run_paw_acceptance.parser().parse_args([])
        for name in ("repo","runtime_payload","state_root","agent_dir","model","budget_usd","budget_ledger","max_provider_calls"):
            self.assertIsNone(getattr(args, name), name)
        with patch("subprocess.run", side_effect=AssertionError("process")), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            run_paw_acceptance.main(["--live"])

    def test_live_rejects_state_inside_runtime_payload_before_creating_any_state(self):
        from scripts import run_paw_acceptance
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "synthetic-config"
            payload = root / "payload"
            agent.mkdir(); payload.mkdir()
            (agent / "auth.json").write_text("{}", encoding="utf-8")
            alias = root / "payload-alias"
            alias.symlink_to(payload, target_is_directory=True)
            for state in (payload / "new-run", alias / "new-run"):
                with self.subTest(state=state), redirect_stderr(io.StringIO()) as errors, \
                     patch("os.umask") as umask, \
                     patch("rag_ime.agent_lab.acceptance_budget.AcceptanceBudgetLedger", side_effect=AssertionError("must reject before ledger")) as ledger, \
                     patch("scripts.run_paw_acceptance.prepared_service", side_effect=AssertionError("must not start runtime")) as runtime:
                    with self.assertRaises(SystemExit) as rejected:
                        run_paw_acceptance.main([
                            "--live", "--case", next(iter(run_paw_acceptance.fixtures.CASES)),
                            "--repo", str(run_paw_acceptance.REPO), "--runtime-payload", str(payload),
                            "--state-root", str(state), "--agent-dir", str(agent),
                            "--model", "gpt-6.1-sol", "--budget-usd", "1",
                            "--budget-ledger", str(root / "ledger.json"), "--max-provider-calls", "1",
                            "--enable-provider-guard",
                        ])
                    self.assertEqual(rejected.exception.code, 2)
                    self.assertIn("runtime payload", errors.getvalue())
                    umask.assert_not_called(); ledger.assert_not_called(); runtime.assert_not_called()
                    self.assertFalse(state.exists())
                    self.assertEqual(list(payload.iterdir()), [])

    def test_catalog_reserves_full_limits_and_rejects_unknown_price(self):
        catalog = {"id":"gpt-6.1-sol","contextWindow":272000,"maxTokens":128000,
                   "cost":{"input":2,"output":10,"cacheWrite":2.5,"tiers":[{"input":4,"output":15,"cacheWrite":5}]}}
        bound = catalog_reservation(catalog,1)
        self.assertEqual(bound["reservedUsd"],7.296)
        self.assertEqual(catalog_reservation(catalog,12)["reservedUsd"],87.552)
        with self.assertRaises(AcceptanceBudgetError):catalog_reservation({**catalog,"cost":{}},1)
        with self.assertRaises(AcceptanceBudgetError):catalog_reservation(catalog,13)

    def test_failed_terminal_usage_is_accounted_before_quality_rejection(self):
        recorded=[]
        guard=SimpleNamespace(account_exact_turn=lambda *values:recorded.append(values))
        receipt={"schemaVersion":"rag-ime.pi-turn-settlement.v1","sessionId":"s","turnId":"t",
                 "clientMessageId":"c","runtimeSessionId":"r","receipt":{
                     "schemaVersion":"pi.agent-settled.v2","sessionId":"r","disposition":"failed","pendingOperations":0}}
        account_terminal_usage(receipt,"s","t","c","unused",guard,lambda *_:{"providerCalls":4})
        self.assertEqual(recorded[0][2]["providerCalls"],4)
        receipt["receipt"]["pendingOperations"]=1
        with self.assertRaises(ValueError):account_terminal_usage(receipt,"s","t","c","unused",guard,lambda *_:None)
        self.assertEqual(len(recorded),1)

    def test_missing_oracle_evidence_cannot_pass(self):
        for case in ("classic-handoff","discussion-no-write","ptc-parallel-barrier-v2","ptc-error"):
            self.assertFalse(all(oracle(case,{}, {}, {}, b"", "").values()),case)

    def test_payload_bound_catalog_rejects_changed_host_model(self):
        from scripts.run_paw_acceptance import verify_catalog_binding
        args=SimpleNamespace(runtime_payload="synthetic",agent_dir="synthetic")
        catalog=dict(provider="openai-codex",id="gpt-6.1-sol",api="openai-codex-responses",
                     contextWindow=272000,maxTokens=128000,payloadManifestSha256="same",modelsConfigSha256="same")
        selected=dict(catalog)
        host=SimpleNamespace(send=lambda *a,**kw:{"models":[selected]})
        service=SimpleNamespace(runtime=SimpleNamespace(_host=lambda:host))
        with patch("rag_ime.managed_pi_runtime.snapshot_managed_pi_runtime_payload",return_value=SimpleNamespace(manifest_sha256="same")), \
             patch("scripts.run_paw_acceptance.models_config_digest",return_value="same"):
            verify_catalog_binding(args,catalog,service)
            for key,value in (("api","other"),("contextWindow",999999),("maxTokens",999999),("provider","other")):
                selected[key]=value
                with self.assertRaisesRegex(ValueError,"Host model differs"):verify_catalog_binding(args,catalog,service)
                selected[key]=catalog[key]
        with patch("rag_ime.managed_pi_runtime.snapshot_managed_pi_runtime_payload",return_value=SimpleNamespace(manifest_sha256="changed")):
            with self.assertRaisesRegex(ValueError,"payload or model configuration changed"):
                verify_catalog_binding(args,catalog)


class AcceptanceLedgerTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix="paw-ledger-test-")
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.path=self.root/"budget.sqlite"
        self.ledger=AcceptanceBudgetLedger(self.path,limit_usd=100,initial_known_usd=0,initial_unknown_usd=0)

    def admit(self, run="one", amount=60):
        return self.ledger.admit(run,state_root=self.root/run,reserve_usd=amount)

    def test_two_processes_cannot_admit_competing_runs(self):
        ctx=multiprocessing.get_context("spawn")
        ready,results,go=ctx.Queue(),ctx.Queue(),ctx.Event()
        processes=[ctx.Process(target=_compete_for_budget,args=(str(self.path),ready,go,results,index)) for index in range(2)]
        def cleanup():
            for process in processes:
                if process.is_alive():process.terminate()
                process.join(5)
            ready.close();results.close()
        self.addCleanup(cleanup)
        for process in processes:process.start()
        ready.get(timeout=20);ready.get(timeout=20);go.set()
        outcomes=[results.get(timeout=20),results.get(timeout=20)]
        for process in processes:process.join(10)
        self.assertCountEqual(outcomes,["admitted","denied"])
        self.assertEqual(self.ledger.snapshot()["activeReservedUsd"],60)

    def test_crash_reopen_keeps_active_reservation_and_rejects_new_run(self):
        self.admit()
        reopened=AcceptanceBudgetLedger(self.path,limit_usd=100)
        self.assertEqual(reopened.snapshot()["activeReservedUsd"],60)
        with self.assertRaisesRegex(AcceptanceBudgetError,"active"):
            reopened.admit("two",state_root=self.root/"two",reserve_usd=1)

    def test_timeout_unknown_does_not_release_and_remaining_budget_is_enforced(self):
        self.admit(amount=70)
        self.ledger.mark_unknown("one",reason="TimeoutError")
        self.assertEqual(self.ledger.snapshot()["unknownReservedUsd"],70)
        with self.assertRaisesRegex(AcceptanceBudgetError,"exceed"):
            self.admit("two",40)
        self.admit("bounded-next",20)
        self.assertEqual(self.ledger.snapshot()["remainingUsd"],10)

    def test_mismatched_guard_usage_proof_does_not_release(self):
        self.admit()
        bad={"guardLoaded":True,"exactUsage":True,"httpClaims":2,"accountedProviderCalls":1,"successfulHttpResponses":2}
        with self.assertRaises(AcceptanceBudgetError):self.ledger.settle("one",actual_usd=.1,proof=bad)
        self.assertEqual(self.ledger.snapshot()["activeReservedUsd"],60)
        good={**bad,"accountedProviderCalls":2,"hostClosed":True,"noPending":True,"guardProcessesExited":True}
        self.ledger.settle("one",actual_usd=.1,proof=good)
        self.assertEqual(self.ledger.snapshot()["knownUsd"],.1)
        self.assertEqual(self.ledger.snapshot()["activeReservedUsd"],0)

    def test_new_ledger_requires_opening_balances_and_cannot_reset_prior_round(self):
        path=self.root/"prior-round.sqlite"
        with self.assertRaisesRegex(AcceptanceBudgetError,"explicit"):
            AcceptanceBudgetLedger(path,limit_usd=10)
        ledger=AcceptanceBudgetLedger(path,limit_usd=10,initial_known_usd=.614674,initial_unknown_usd=1)
        self.assertEqual(ledger.snapshot()["knownUsd"],.614674)
        self.assertEqual(ledger.snapshot()["unknownReservedUsd"],1)
        with self.assertRaisesRegex(AcceptanceBudgetError,"immutable"):
            AcceptanceBudgetLedger(path,limit_usd=10,initial_known_usd=0,initial_unknown_usd=0)
        with self.assertRaisesRegex(AcceptanceBudgetError,"exceed"):
            ledger.admit("too-large",state_root=self.root/"large",reserve_usd=9)


class AcceptanceTeardownTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix="paw-teardown-test-")
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.run=self.root/"run";self.run.mkdir()
        reservation={"reservedUsd":60,"priorKnownUsd":0,"maxProviderRequests":12,"maxRequestBytes":1000}
        (self.run/"reservation.json").write_text(json.dumps(reservation))
        (self.run/"guard-loaded.jsonl").write_text('{"pid":999999999}\n')
        self.budget=AcceptanceRunBudget(root=self.run,reservation=reservation)
        self.ledger=AcceptanceBudgetLedger(self.root/"ledger.sqlite",limit_usd=100,initial_known_usd=0,initial_unknown_usd=0)
        self.ledger.admit("one",state_root=self.run,reserve_usd=60)

    def close(self, during_close=lambda:None):
        from scripts.run_paw_acceptance import close_and_quiesce
        process=SimpleNamespace(returncode=None)
        process.poll=lambda:process.returncode
        client=SimpleNamespace(_process=process,running=True)
        runtime=SimpleNamespace(_client=client,runtime_status=lambda:{"activeSessionIds":[],"activeCompletionIds":[],"openSessionIds":[]})
        def stop():
            during_close()
            process.returncode=0;client.running=False
        service=SimpleNamespace(runtime=runtime,close=stop)
        server=SimpleNamespace(shutdown=lambda:None,server_close=lambda:None)
        trace=SimpleNamespace(active_calls=0,close=lambda:None)
        receipt={}
        with patch("os.kill",side_effect=ProcessLookupError):
            close_and_quiesce(service,server,None,trace,self.run,receipt)
        return receipt

    def test_delayed_first_claim_during_close_cannot_release_zero_cost(self):
        provisional=self.budget.finish()
        self.assertFalse(provisional["proof"]["exactUsage"])
        self.assertEqual(provisional["reservationReleasedUsd"],0)
        with self.assertRaises(AcceptanceBudgetError):
            self.ledger.settle("one",actual_usd=0,proof=provisional["proof"])
        receipt=self.close(lambda:(self.run/"provider-call-1.claim").write_text('{"slot":1}'))
        with self.assertRaises(AcceptanceBudgetError):self.budget.finalize(receipt)
        self.ledger.mark_unknown("one",reason="late claim")
        self.assertEqual(self.ledger.snapshot()["unknownReservedUsd"],60)

    def test_shutdown_failure_retains_full_reservation_even_without_claims(self):
        def fail():raise TimeoutError("shutdown blocked")
        with self.assertRaisesRegex(RuntimeError,"quiescence"):self.close(fail)
        with self.assertRaises(AcceptanceBudgetError):self.budget.finalize({})
        self.ledger.mark_unknown("one",reason="shutdown failed")
        self.assertEqual(self.ledger.snapshot()["unknownReservedUsd"],60)

    def test_zero_cost_can_settle_only_after_proven_shutdown(self):
        receipt=self.close()
        final=self.budget.finalize(receipt)
        self.assertTrue(final["proof"]["hostClosed"])
        self.assertTrue(final["proof"]["noPending"])
        self.ledger.settle("one",actual_usd=0,proof=final["proof"])
        self.assertEqual(self.ledger.snapshot()["activeReservedUsd"],0)

    def test_evidence_change_after_close_is_not_settleable(self):
        receipt=self.close()
        (self.run/"provider-call-1.claim").write_text('{"slot":1}')
        with self.assertRaisesRegex(AcceptanceBudgetError,"changed after shutdown"):
            self.budget.finalize(receipt)

    def test_existing_agent_config_is_not_prepared_or_chmodded(self):
        from dataclasses import replace
        from scripts.run_paw_acceptance import readonly_runtime_config
        agent=self.root/"existing-agent";agent.mkdir()
        (agent/"auth.json").write_text('{"synthetic":true}')
        (agent/"settings.json").write_text('{"keep":"unchanged"}')
        before={p.name:p.read_bytes() for p in agent.iterdir()}
        mode=agent.stat().st_mode
        config=readonly_runtime_config(enabled=True,executable=Path("unused"),agent_dir=agent,
                                       session_dir=self.run/"sessions",logs_dir=self.run/"logs")
        with patch("os.chmod",side_effect=AssertionError("external mode changed")):
            config.prepare_agent_config()
            replace(config,tool_gateway_token="synthetic").prepare_agent_config()
        self.assertEqual(before,{p.name:p.read_bytes() for p in agent.iterdir()})
        self.assertEqual(mode,agent.stat().st_mode)
        env=config.child_environment()
        self.assertEqual(env["RAG_IME_PI_AGENT_DIR"],str(agent))
        for key in ("HOME","CODEX_HOME","PI_CODING_AGENT_DIR","RAG_IME_PI_PLUGINS_DIR","RAG_IME_PI_PLUGIN_INBOX"):
            self.assertTrue(Path(env[key]).is_relative_to(self.run),key)


if __name__ == "__main__":unittest.main()
