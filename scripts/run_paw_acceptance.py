#!/usr/bin/env python3
"""Portable PAW acceptance entry. Default: offline validation, no network/auth.

Live runs require explicit paths, budget/ledger, model, case and provider guard.
There is no login, installation, credential copying or home-directory discovery.
"""
from __future__ import annotations
import argparse
import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from rag_ime.agent_lab import acceptance_fixtures as fixtures


def readonly_runtime_config(**values):
    from rag_ime.pi.config import PiRuntimeConfig
    class ExistingConfig(PiRuntimeConfig):
        def prepare_agent_config(self):
            # Production preparation owns writable managed settings. This test
            # entry is a read-only consumer of the explicitly supplied config.
            if not self.agent_dir.is_dir() or self.agent_dir.is_symlink() or not (self.agent_dir/"auth.json").is_file():
                raise ValueError("an existing regular authorized agent config is required")
        def child_environment(self, *, session=None):
            environment = super().child_environment(session=session)
            isolated = self.session_dir.parent/"host-support"
            environment.update({
                "HOME":str(isolated/"home"),"CODEX_HOME":str(isolated/"codex"),
                "PI_CODING_AGENT_DIR":str(isolated/"pi-agent"),
                "RAG_IME_APP_SUPPORT_DIR":str(isolated/"app-support"),
                "RAG_IME_PI_PLUGINS_DIR":str(isolated/"plugins"),
                "RAG_IME_PI_PLUGIN_INBOX":str(isolated/"plugin-inbox"),
                "RAG_IME_PI_USER_SKILL_PATHS":str(isolated/"pi-skills"),
                "RAG_IME_CODEX_SKILL_PATHS":str(isolated/"codex-skills"),
                "RAG_IME_PI_AGENT_DIR":str(self.agent_dir),
            })
            return environment
    return ExistingConfig(**values)


def close_and_quiesce(service, server, thread, trace, guard_dir, receipt):
    """Freeze new admissions, close owners, then prove no provider can run."""
    from rag_ime.agent_lab.acceptance_budget import provider_evidence_fingerprint
    receipt.update(hostClosed=False,noPending=False,guardProcessesExited=False)
    errors = []
    client = getattr(service.runtime,"_client",None) if service is not None else None
    process = getattr(client,"_process",None)
    try:
        reservation_path = Path(guard_dir)/"reservation.json"
        value = json.loads(reservation_path.read_text())
        value["maxProviderRequests"] = 0
        temporary = reservation_path.with_name(".closing-"+uuid.uuid4().hex+".json")
        _private_json(temporary,value)
        temporary.replace(reservation_path)
    except BaseException as error:
        errors.append(type(error).__name__)
    for action in ([service.close] if service is not None else []) + ([server.shutdown] if thread is not None else []):
        try:action()
        except BaseException as error:errors.append(type(error).__name__)
    try:
        if thread is not None:
            thread.join(timeout=10)
        server.server_close()
    except BaseException as error:
        errors.append(type(error).__name__)
    deadline = time.monotonic()+5
    while getattr(trace,"active_calls",0) and time.monotonic()<deadline:
        time.sleep(.02)
    try:
        status = service.runtime.runtime_status() if service is not None else {}
        current_client = getattr(service.runtime,"_client",None) if service is not None else None
        host_closed = ((process is None or process.poll() is not None)
                       and not bool(client and client.running)
                       and not bool(current_client and current_client.running))
        no_pending = (not status.get("activeSessionIds") and not status.get("activeCompletionIds")
                      and not status.get("openSessionIds") and not getattr(trace,"active_calls",0)
                      and (thread is None or not thread.is_alive()))
        loaded = Path(guard_dir)/"guard-loaded.jsonl"
        pids = {int(json.loads(line)["pid"]) for line in loaded.read_text().splitlines() if line} if loaded.exists() else set()
        all_exited = bool(pids)
        for pid in pids:
            try:os.kill(pid,0)
            except ProcessLookupError:continue
            except PermissionError:all_exited=False
            else:all_exited=False
        receipt.update(hostClosed=host_closed and not errors,noPending=no_pending and not errors,
                       guardProcessesExited=all_exited and not errors,guardPids=sorted(pids),
                       evidenceFingerprint=provider_evidence_fingerprint(guard_dir),shutdownErrors=errors)
    except BaseException as error:
        errors.append(type(error).__name__)
    finally:
        try:trace.close()
        except BaseException as error:errors.append(type(error).__name__)
    if errors:
        receipt.update(hostClosed=False,noPending=False,guardProcessesExited=False,shutdownErrors=errors)
    if errors or not all(receipt.get(key) is True for key in ("hostClosed","noPending","guardProcessesExited")):
        raise RuntimeError("acceptance shutdown did not establish quiescence; reservation must remain unknown")


@contextmanager
def prepared_service(*, repo, runtime_payload, state_root, agent_dir, provider_environment, trace, quiescence=None):
    """Use explicit paths only; no auth copying, discovery, login or installation."""
    from rag_ime.agent_service import AgentService
    from rag_ime.debug_server import DebugImeService, DebugRequestHandler, DebugServerConfig, QuietThreadingHTTPServer
    from rag_ime.managed_pi_runtime import snapshot_managed_pi_runtime_payload
    repo, state, agent_dir = Path(repo).resolve(), Path(state_root).resolve(), Path(agent_dir).resolve()
    if not agent_dir.is_dir() or state.is_relative_to(repo) or state.is_relative_to(agent_dir):
        raise ValueError("state must be fresh, outside repository and existing explicit agent config")
    state.mkdir(parents=True, exist_ok=False)
    installed = snapshot_managed_pi_runtime_payload(runtime_payload)
    class Handler(DebugRequestHandler):
        def log_message(self, *_args):
            pass
    server = QuietThreadingHTTPServer(("127.0.0.1",0),Handler)
    tool_url = f"http://127.0.0.1:{server.server_port}/api/agent/tool/execute"
    guard_dir = provider_environment.get("PAW_CALL_GUARD_DIR")
    if not guard_dir:
        raise ValueError("an explicit provider guard directory is required")
    config = readonly_runtime_config(enabled=True, executable=installed.executable,
        extension_path=installed.extension_path, node_executable=installed.node_executable,
        tools=installed.tools, pi_version=installed.pi_version, protocol_version="2",
        provider="openai-codex", model="gpt-6.1-sol", model_configured=True,
        agent_dir=agent_dir, provider_environment=dict(provider_environment),
        session_dir=state/"sessions",logs_dir=state/"logs",debug_context_dir=state/"debug",
        tool_gateway_url=tool_url,idle_timeout_seconds=0,max_sessions=4,command_timeout_seconds=60)
    service = None
    thread = None
    try:
        service = AgentService(db_path=state/"state.sqlite",runtime_config=config,
            startup_recovery_enabled=False,wake_scheduler_enabled=False,tool_gateway_url=tool_url)
        Handler.service = DebugImeService(DebugServerConfig(db_path=state/"state.sqlite",agent_service=service,
            seed_if_empty=False,memory_projection_worker_enabled=False,rime_user_dir=state/"Rime",
            rime_lexicon_backup_root=state/"RimeBackups"))
        trace.install(service,Handler.service.agent_tools)
        Handler.static_dir = repo/"debug"
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        yield service
    finally:
        close_and_quiesce(service,server,thread,trace,guard_dir,quiescence if quiescence is not None else {})


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--live", action="store_true", help="Explicitly allow guarded provider requests")
    result.add_argument("--case", choices=sorted(fixtures.CASES))
    result.add_argument("--repo", type=Path)
    result.add_argument("--runtime-payload", type=Path)
    result.add_argument("--state-root", type=Path)
    result.add_argument("--agent-dir", type=Path, help="Existing authorized Pi config; no login or auth copy")
    result.add_argument("--model", choices=["gpt-6.1-sol"])
    result.add_argument("--budget-usd", type=float)
    result.add_argument("--budget-ledger", type=Path)
    result.add_argument("--initial-known-usd", type=float, help="Required for a NEW ledger; include earlier spend in this round")
    result.add_argument("--initial-unknown-usd", type=float, help="Required for a NEW ledger; include all outstanding prior reservations")
    result.add_argument("--max-provider-calls", type=int)
    result.add_argument("--max-tool-calls", type=int, default=32)
    result.add_argument("--enable-provider-guard", action="store_true")
    result.add_argument("--inherit-proxy-env", action="store_true", help="Explicitly pass proxy variables and NODE_USE_ENV_PROXY=1")
    return result


def _private_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)+"\n", encoding="utf-8")


def _base_environment(args, guard_dir):
    # Do not inherit arbitrary NODE_OPTIONS or provider credentials. The chosen
    # agent-dir is the sole credential source used by the authorized Pi SDK.
    env = {key: os.environ[key] for key in ("PATH","TMPDIR","TMP","TEMP","LANG","LC_ALL","SYSTEMROOT","WINDIR") if key in os.environ}
    guard = REPO/"scripts/paw_acceptance_provider_guard.mjs"
    env.update(NODE_OPTIONS="--import="+guard.as_uri(), PAW_CALL_GUARD_DIR=str(guard_dir))
    if args.inherit_proxy_env:
        env.update({key:value for key,value in os.environ.items() if key in {
            "HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","NO_PROXY","http_proxy","https_proxy","all_proxy","no_proxy"}})
        env["NODE_USE_ENV_PROXY"] = "1"
    return env


def read_local_catalog(args, root):
    from rag_ime.managed_pi_runtime import snapshot_managed_pi_runtime_payload
    installed = snapshot_managed_pi_runtime_payload(args.runtime_payload)
    bridge = installed.executable.parent/"provider-bridge.mjs"
    if not bridge.is_file() or bridge.is_symlink():
        raise RuntimeError("verified payload has no regular provider bridge")
    catalog_guard = root/"catalog-preflight"
    catalog_guard.mkdir()
    _private_json(catalog_guard/"reservation.json", {"reservedUsd":0,"priorKnownUsd":0,"maxProviderRequests":0,"maxRequestBytes":1_000_000})
    environment = _base_environment(args,catalog_guard)
    environment.update(HOME=str(catalog_guard/"home"),CODEX_HOME=str(catalog_guard/"codex"),
                       PI_CODING_AGENT_DIR=str(catalog_guard/"pi-agent"))
    config_digest = models_config_digest(args.agent_dir)
    result = subprocess.run([installed.node_executable,str(bridge)],
                            input=json.dumps({"action":"catalog","agentDir":str(args.agent_dir.resolve())}),
                            env=environment,capture_output=True,text=True,timeout=30,check=False)
    if result.returncode:
        raise RuntimeError("local model catalog unavailable; no login or network fallback was attempted")
    try:
        rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        answer = next(row for row in rows if row.get("event") == "result" and row.get("ok") is True)
        if answer.get("catalogError"):
            raise ValueError("catalog error")
        provider = next(row for row in answer["providers"] if row.get("id") == "openai-codex")
        model = next(row for row in provider["availableModels"] if row.get("id") == args.model)
        if not isinstance(model.get("cost"),dict) or model.get("provider") != "openai-codex":
            raise ValueError("payload bridge lacks acceptance model metadata")
        catalog = {key:model[key] for key in ("provider","id","api","contextWindow","maxTokens","cost")}
        catalog.update(payloadManifestSha256=installed.manifest_sha256,modelsConfigSha256=config_digest)
        verify_catalog_binding(args,catalog)
        return catalog
    except (ValueError,KeyError,StopIteration) as error:
        raise RuntimeError("local model catalog did not return valid metadata") from error


def models_config_digest(agent_dir):
    path = Path(agent_dir)/"models.json"
    if path.is_symlink():
        raise ValueError("model config must not be a symlink")
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "absent"


def verify_catalog_binding(args, catalog, service=None):
    from rag_ime.managed_pi_runtime import snapshot_managed_pi_runtime_payload
    installed = snapshot_managed_pi_runtime_payload(args.runtime_payload)
    if (installed.manifest_sha256 != catalog.get("payloadManifestSha256")
            or models_config_digest(args.agent_dir) != catalog.get("modelsConfigSha256")):
        raise ValueError("runtime payload or model configuration changed after catalog admission")
    if service is not None:
        rows = service.runtime._host().send("models.list",timeout=30).get("models",[])
        selected = next((row for row in rows if row.get("provider") == catalog["provider"] and row.get("id") == catalog["id"]),{})
        if any(selected.get(key) != catalog.get(key) for key in ("provider","id","api","contextWindow","maxTokens")):
            raise ValueError("executing Host model differs from the payload-bound reservation catalog")


def prove_zero_request_guard(args, root, catalog):
    """Exercise the actual configured Host with a permanently zero-call guard.

    Proof state is separate from paid state; a late proof retry can never gain
    access to the paid reservation. No provider HTTP request is permitted here.
    """
    from rag_ime.agent_lab.acceptance_trace import GatewayTrace
    proof = root/"host-guard-proof"
    proof.mkdir()
    _private_json(proof/"reservation.json", {"reservedUsd":0,"priorKnownUsd":0,"maxProviderRequests":0,"maxRequestBytes":1_000_000})
    caught = False
    quiescence = {}
    try:
        with prepared_service(repo=args.repo,runtime_payload=args.runtime_payload,state_root=proof/"service",
                agent_dir=args.agent_dir,provider_environment=_base_environment(args,proof),trace=GatewayTrace(),quiescence=quiescence) as service:
            verify_catalog_binding(args,catalog,service)
            sid = service.ensure_primary_assistant({})["session"]["id"]
            service.runtime.set_model(sid,provider="openai-codex",model_id=args.model)
            service.runtime.set_thinking_level(sid,level="xhigh")
            service.select_codemode_mode(sid,{"mode":"off"})
            client = "acceptance-zero-network-proof"
            accepted = service.prompt(sid,{"message":"Transport guard preflight only. Do not call tools. Reply OK.","clientMessageId":client})
            tid = accepted.get("turnId")
            if tid:
                service.runtime.await_turn_settled(sid,tid,client_message_id=client,timeout_seconds=30)
    except Exception:
        caught = True
    denied = [json.loads(line) for line in (proof/"denied.jsonl").read_text().splitlines()] if (proof/"denied.jsonl").exists() else []
    confirmed = ((proof/"guard-loaded.jsonl").is_file() and not list(proof.glob("provider-call-*.claim"))
                 and any(row.get("event") == "budget_zero_rejected_before_network" for row in denied)
                 and all(quiescence.get(key) is True for key in ("hostClosed","noPending","guardProcessesExited")))
    # Guard tests pin the precise event name; fail closed if the runtime never
    # reached the model fetch path or changed transport underneath this hook.
    _private_json(proof/"proof.json", {"passed":confirmed,"providerCalls":0,"exceptionObserved":caught,"quiescence":quiescence})
    if not confirmed:
        raise RuntimeError("actual Host guard proof failed; no paid case was admitted")


def main(argv=None):
    parse = parser()
    args = parse.parse_args(argv)
    if not args.live:
        manifest = fixtures.load_manifest()
        result = fixtures.invoice_self_test()
        print(json.dumps({"status":"offline_passed","caseIds":[row["id"] for row in manifest["cases"]],
                          "fixtureOracleNegatives":result["negativeCases"],"providerCalls":0,"nativeUI":False},ensure_ascii=False))
        return 0
    required = ("case","repo","runtime_payload","state_root","agent_dir","model","budget_usd","budget_ledger","max_provider_calls")
    missing = [name.replace("_","-") for name in required if getattr(args,name) is None]
    if missing or not args.enable_provider_guard:
        parse.error("live requires explicit "+", ".join(missing+["enable-provider-guard"] if not args.enable_provider_guard else missing))
    if args.repo.resolve() != REPO:
        parse.error("run the CLI from the explicitly selected repository checkout")
    if not 1 <= args.max_provider_calls <= 12 or not 1 <= args.max_tool_calls <= 64:
        parse.error("provider calls must be 1..12 and tool calls 1..64")
    if not args.agent_dir.is_dir() or not (args.agent_dir/"auth.json").is_file() or not args.runtime_payload.is_dir():
        parse.error("explicit existing agent config and runtime payload are required")
    root = args.state_root.resolve()
    if (root.exists() or root.is_relative_to(REPO) or root.is_relative_to(args.agent_dir.resolve())
        or root.is_relative_to(args.runtime_payload.resolve())):
        parse.error("state-root must be NEW and outside the repository, runtime payload and agent config")
    if (args.budget_ledger.resolve().is_relative_to(root) or args.budget_ledger.resolve().is_relative_to(REPO)
        or args.budget_ledger.resolve().is_relative_to(args.agent_dir.resolve()) or args.budget_ledger.resolve().is_relative_to(args.runtime_payload.resolve())):
        parse.error("budget-ledger must persist outside this run, repository, runtime payload and agent config")
    os.umask(0o077)
    from rag_ime.agent_lab.acceptance_budget import AcceptanceBudgetLedger, AcceptanceRunBudget, catalog_reservation
    from rag_ime.agent_lab.acceptance import run_case
    from rag_ime.agent_lab.acceptance_trace import GatewayTrace
    ledger = AcceptanceBudgetLedger(args.budget_ledger,limit_usd=args.budget_usd,
                                   initial_known_usd=args.initial_known_usd,initial_unknown_usd=args.initial_unknown_usd)
    if ledger.snapshot()["activeRunIds"]:
        raise RuntimeError("another run is active; inspect/reconcile the same ledger first")
    root.mkdir(parents=True,mode=0o700)
    run_id = "acceptance:"+uuid.uuid4().hex
    report = {"runId":run_id,"caseId":args.case,"status":"preflight","nativeAcceptance":False,"providerCalls":0}
    admitted = False
    budget = None
    quiescence = {}
    try:
        catalog = read_local_catalog(args,root)
        reservation = catalog_reservation(catalog,args.max_provider_calls)
        prove_zero_request_guard(args,root,catalog)
        admission = ledger.admit(run_id,state_root=root,reserve_usd=reservation["reservedUsd"])
        admitted = True
        reservation.update(admission,provider="openai-codex",model=args.model,reasoningEffort="xhigh")
        _private_json(root/"catalog.json",catalog)
        _private_json(root/"reservation.json",reservation)
        _private_json(root/"task.json",fixtures.CASES[args.case])
        budget = AcceptanceRunBudget(root=root,reservation=reservation,max_tools=args.max_tool_calls)
        class BoundedTrace(GatewayTrace):
            def install(self,service,gateway):
                super().install(service,gateway)
                original = gateway.execute
                def execute(payload):
                    budget.check_tool_admission()
                    return original(payload)
                gateway.execute = execute
        trace = BoundedTrace()
        with prepared_service(repo=args.repo,runtime_payload=args.runtime_payload,state_root=root/"service",
                agent_dir=args.agent_dir,provider_environment=_base_environment(args,root),trace=trace,quiescence=quiescence) as service:
            budget.service = service
            verify_catalog_binding(args,catalog,service)  # boots Host without a prompt
            budget.require_loaded()
            trial = run_case(service=service,root=root/"trial",case_id=args.case,budget=budget,trace=trace)
            report["trial"] = trial
            job = trial["job"]
            report["status"] = "passed" if job["state"] == "completed" and (job.get("result") or {}).get("qualityVerdict") == "keep" else "failed"
        # The complete Service/Host/gateway teardown must succeed first. Never
        # release even a zero-claim reservation while a delayed request can run.
        reconciled = budget.finalize(quiescence)
        ledger.settle(run_id,actual_usd=reconciled["estimatedUsd"],proof=reconciled["proof"])
        report.update(reconciled)
    except BaseException as error:
        report.update(status="blocked_or_failed",errorType=type(error).__name__)
        if admitted:
            try:
                reconciled = budget.finalize(quiescence) if budget is not None else None
                if reconciled is None:
                    raise RuntimeError("no receipt")
                ledger.settle(run_id,actual_usd=reconciled["estimatedUsd"],proof=reconciled["proof"])
                report.update(reconciled)
            except Exception:
                ledger.mark_unknown(run_id,reason=type(error).__name__)
                report["usageUnknown"] = True
        if isinstance(error,(KeyboardInterrupt,SystemExit)):
            report["interrupted"] = True
    report["ledger"] = ledger.snapshot()
    report["quiescence"] = quiescence
    _private_json(root/"result.json",report)
    print(json.dumps(report,ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
