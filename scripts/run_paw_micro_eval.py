#!/usr/bin/env python3
"""Run small, fixed PAW trials; private artifacts must be outside the repo.

--live uses installed Pi/OAuth through source AgentService and Lab owners.
No installation, production DB writes, commits, or automatic candidate retries.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rag_ime.agent_model_defaults import DEFAULT_AGENT_MODEL_ID
from rag_ime.agent_lab.micro import MicroAdapter, SUITE, canonical, digest, write_private, turn_usage
from rag_ime.agent_lab.trial_execution import AgentLabTrialApplication
from rag_ime.agent_lab.trials import AgentLabTrialStore

FAULT_TESTS = {
    "concurrent-click": "tests.test_agent_lab_trial_execution.AgentLabTrialExecutionTests.test_double_click_and_concurrent_run_execute_once_without_implicit_quality_pass",
    "queued-cancel": "tests.test_agent_lab_trial_execution.AgentLabTrialExecutionTests.test_queued_cancel_does_not_execute",
    "running-cancel-cleanup": "tests.test_agent_lab_trial_execution.AgentLabTrialExecutionTests.test_running_cancel_calls_abort_and_waits_for_cleanup_before_terminal",
    "late-session-bind": "tests.test_agent_lab_trial_execution.AgentLabTrialExecutionTests.test_session_bound_after_cancel_is_aborted_exactly_once",
    "restart-no-paid-replay": "tests.test_agent_lab_trial_execution.AgentLabTrialExecutionTests.test_restart_interrupts_and_read_or_replay_never_starts_paid_work",
    "failed-abort": "tests.test_agent_lab_trial_execution.AgentLabTrialExecutionTests.test_failed_abort_or_cleanup_timeout_stays_interrupted_not_cancelled",
    "lost-settlement": "tests.test_agent_lab_golden_pi.GoldenPiTests.test_restart_recovers_original_accepted_turn_without_second_prompt",
    "unknown-admission": "tests.test_agent_lab_golden_pi.GoldenPiTests.test_unknown_admission_is_not_blindly_replayed",
    "wrong-terminal-identity": "tests.test_agent_lab_golden_pi.GoldenPiTests.test_wrong_or_failed_settlement_never_becomes_success",
    "metadata-write-failure": "tests.test_agent_room_send_admission.RoomSendAdmissionTests.test_delivery_metadata_failure_keeps_accepted_pi_turn_and_replay_identity",
    "worker-crash": "tests.test_agent_room_send_admission.RoomSendAdmissionTests.test_worker_exception_closes_room_turn_and_releases_admission",
    "recovery-duplicate-terminal": "tests.test_agent_room_partner_restart_recovery.AgentRoomPartnerRestartRecoveryTest.test_live_and_recovered_child_terminal_publish_once_in_either_order",
}


def room_turn_identity(admission):
    dispatch = admission["dispatches"][0]
    if not dispatch.get("accepted") or not dispatch.get("sessionTurnId") or not dispatch.get("dispatchId"):
        raise RuntimeError("Room admission has no bound Pi turn")
    # User request -> Room dispatch -> Pi client message are distinct identities.
    return dispatch["sessionTurnId"], dispatch["dispatchId"]


def reliability(root, repeats):
    rows=[]
    for case_id, test_id in FAULT_TESTS.items():
        observations=[]
        for repeat in range(repeats):
            stream=io.StringIO()
            test=unittest.defaultTestLoader.loadTestsFromName(test_id)
            started=time.monotonic()
            result=unittest.TextTestRunner(stream=stream,verbosity=2).run(test)
            observations.append({"repeat":repeat+1,"passed":result.wasSuccessful() and result.testsRun==1 and not result.skipped,
                                 "testsRun":result.testsRun,"elapsedMs":round((time.monotonic()-started)*1000,3)})
            (root/f"fault-{case_id}-{repeat+1}.log").write_text(stream.getvalue())
        rows.append({"caseId":case_id,"testId":test_id,"observations":observations,
                     "passed":all(r["passed"] for r in observations)})
        print("fault",case_id,"pass" if rows[-1]["passed"] else "FAIL",flush=True)
    report={"evidenceLevel":"source integration with simulated failure injection", "providerCalls":0,
            "uniqueScenarios":len(rows),"observations":len(rows)*repeats,"passedScenarios":sum(r["passed"] for r in rows),
            "rows":rows,"nativeAcceptance":False,"soakTest":False,
            "timingBoundary":"test harness elapsed time; not cancellation or recovery latency"}
    write_private(root/"reliability.json",report)
    return report


def skill_inventory():
    names=["alignment-and-decision","implementation-planning","systematic-debugging","test-driven-implementation",
           "independent-review","orchestrate-session","facilitate-room"]
    rows=[]
    for name in names:
        source=ROOT/"integrations/pi/skills"/name/"SKILL.md"
        local=Path.home()/".codex/skills"/name/"SKILL.md"
        a=source.read_bytes();b=local.read_bytes() if local.exists() else None
        rows.append({"name":name,"sourceSha256":hashlib.sha256(a).hexdigest(),
                     "codexSha256":hashlib.sha256(b).hexdigest() if b else None,"same":a==b,"sourceBytes":len(a),"codexBytes":len(b) if b else None})
    return {"rows":rows,"sameCount":sum(r["same"] for r in rows),"total":len(rows),
            "boundary":"disk inventory only; difference does not prove a runtime behavior defect"}


def live_trials(root, tasks, baseline_run=None):
    from scripts.pi_canary_support import installed_agent_config_dir,stage_openai_codex_oauth,launch_environment
    from rag_ime.managed_pi_runtime import snapshot_managed_pi_runtime
    from rag_ime.pi.config import PiRuntimeConfig
    from rag_ime.agent_service import AgentService
    from rag_ime.agent_lab.golden_pi import AgentLabGoldenPiExecutor, _settled_output
    plist=Path.home()/"Library/LaunchAgents/com.rag-ime.agent-gateway.plist"
    source=installed_agent_config_dir(plist)
    for key,value in launch_environment(plist).items():
        os.environ.setdefault(key,value)
    stage_openai_codex_oauth(source,root/"config")
    service=None
    app=None
    try:
        inst=snapshot_managed_pi_runtime(source.parent.parent)
        cfg=replace(PiRuntimeConfig.from_environment(enabled_default=True),enabled=True,
                    executable=inst.executable,extension_path=inst.extension_path,node_executable=inst.node_executable,
                    tools=inst.tools,pi_version=inst.pi_version,installation_error="",agent_dir=root/"config",
                    session_dir=root/"sessions",logs_dir=root/"logs",debug_context_dir=root/"debug-context",
                    protocol_version="2",idle_timeout_seconds=0,max_sessions=4)
        service=AgentService(db_path=root/"agent.sqlite",runtime_config=cfg,project="paw-micro-selfboot",
                             background_job_execution_owner=False,startup_recovery_enabled=False)
        class BoundedRuntime:
            def __getattr__(self,name):
                return getattr(service.runtime,name)
            def set_model(self,session_id,*,provider,model_id):
                return service.runtime.set_model(session_id,provider=provider,model_id=model_id,max_tokens=768)
        executor=AgentLabGoldenPiExecutor(root/"agent.sqlite",sessions=service.sessions,runtime=lambda:BoundedRuntime(),timeout_seconds=90)
        def account(result):
            transcript=service.sessions.get(result["sessionId"])["sessionFile"]
            try:
                aggregate=turn_usage(transcript,result["turnId"])
            except (OSError,ValueError,KeyError):
                return {**result,"lastMessageUsage":result.get("usage"),"usage":{}}
            return {**result,"lastMessageUsage":result.get("usage"),**aggregate,
                    "receipt":{**result["receipt"],"usage":aggregate["usage"],"usageAuthority":"pi_transcript_exact_turn"}}
        def complete(**kwargs):
            return account(executor.complete(**kwargs))
        rooms={}
        def room_complete(*,request_id,model,prompt,on_session,cancelled,room_role,room_group="default",release_after=False):
            room=rooms.get(room_group)
            if room is None:
                room=service.create_room({"title":"Lab micro · contract handoff", "workspaceRoots":[], "executionMode":"read_only",
                    "participants":[{"roleId":"companion-present-v1","roleVersion":"1","displayName":"Request"},
                                    {"roleId":"companion-future-v1","roleVersion":"1","displayName":"Response"},
                                    {"roleId":"companion-firstlight-v1","roleVersion":"1","displayName":"Integrator"}]})["room"]
                rooms[room_group]=room
                binding_name="room-binding.json" if room_group=="default" else "room-binding-"+digest(room_group)[:12]+".json"
                write_private(root/binding_name,{"group":room_group,"roomId":room["id"],"participants":room["participants"]})
            target=room["participants"][("request","response","integrate").index(room_role)]
            sid=target["sessionId"]
            # Participant authority is owned by Room, never by Session updates.
            service.runtime.set_model(sid,provider=model["provider"],model_id=model["model"],max_tokens=768)
            service.runtime.set_thinking_level(sid,level=model["thinkingLevel"])
            on_session(sid)
            if cancelled():
                raise RuntimeError("cancelled before Room admission")
            started=time.monotonic()
            accepted=service.post_room_message(room["id"],{"message":prompt,"clientMessageId":request_id,"participantIds":[target["id"]]})
            write_private(root/(digest(request_id)+"-admission.json"),accepted)
            tid, pi_client_id = room_turn_identity(accepted)
            result=service.runtime.await_turn_settled(sid,tid,client_message_id=pi_client_id,timeout_seconds=90)
            text,usage,receipt_id=_settled_output(result,sid,tid,pi_client_id)
            service.events.flush()
            receipt={"requestId":request_id,"piClientMessageId":pi_client_id,"sessionId":sid,"turnId":tid,"roomId":room["id"],"roomTurnId":accepted["roomTurnId"],
                     "settlementReceiptId":receipt_id,"elapsedMs":round((time.monotonic()-started)*1000),"usage":usage,"model":model}
            accounted=account({"text":text,"sessionId":sid,"turnId":tid,"usage":usage,"receipt":receipt})
            if release_after:service.runtime.close_session(sid)
            return accounted
        adapter=MicroAdapter(root/"trials",complete=complete,room_complete=room_complete,abort_session=service.runtime.abort)
        from rag_ime.agent_lab.room_comparison import RoomComparisonAdapter
        comparison=RoomComparisonAdapter(root/"comparison",complete=complete,room_complete=room_complete,abort_session=service.runtime.abort)
        class ContextContinuationAdapter:
            def prepare(self,spec,job_id):
                if spec!={"taskId":"context-continuation"}:
                    raise ValueError("fixed continuation suite only")
                public={**spec,"model":DEFAULT_AGENT_MODEL_ID,"thinkingLevel":"low","maxProviderCalls":4,"maxObservedTokens":12000,"maxOutputTokens":768}
                return {"publicSpec":public,"privateInput":{**public,"jobId":job_id}}
            def execute(self,private,observer,cancelled):
                from scripts.eval_paw_context_continuation import run_evaluation
                measured={"providerCalls":0,"tokens":0,"withinBudget":True}
                def consumer(*,prompt,fixture_id):
                    if cancelled():raise InterruptedError("cancelled")
                    if not measured["withinBudget"] or measured["providerCalls"]>=4 or measured["tokens"]>=12000:
                        raise RuntimeError("continuation_budget_exhausted")
                    result=complete(request_id=private["jobId"]+":"+fixture_id,
                        model={"provider":"openai-codex","model":private["model"],"thinkingLevel":private["thinkingLevel"]},
                        prompt=prompt+"\n所有可用依据都已提供。不要调用工具。",
                        on_session=lambda sid:observer.bind_session(sid,cancel=lambda:service.runtime.abort(sid)),cancelled=cancelled)
                    write_private(root/(fixture_id+"-model-receipt.json"),result)
                    # Fixtures are independent. Release each settled Session
                    # before the next one to keep local memory use bounded.
                    service.runtime.close_session(result["sessionId"])
                    calls=result.get("providerCalls")
                    tokens=result.get("usage",{}).get("totalTokens")
                    if not isinstance(calls,int) or not isinstance(tokens,(int,float)):
                        measured["withinBudget"]=False
                        raise RuntimeError("exact_turn_usage_unavailable")
                    measured["providerCalls"]+=calls
                    measured["tokens"]+=tokens
                    measured["withinBudget"]=measured["providerCalls"]<=4 and measured["tokens"]<=12000
                    # The executor returns only after validating a successful
                    # terminal settlement for this exact Session/turn.
                    return {**result,"receipt":{**result["receipt"],"status":"completed",
                        "statusAuthority":"AgentLabGoldenPiExecutor verified terminal settlement"}}
                report=run_evaluation(consumer=consumer)
                report["labExecution"]={"jobId":private["jobId"],"owner":"AgentLabTrialApplication",**measured}
                passed=report["metrics"]["modelContinuationSuccess"]["passed"]
                report.update(status="completed",qualityVerdict="keep" if passed==4 and measured["withinBudget"] else "reject")
                write_private(root/"context-continuation-report.json",report)
                return report
        from rag_ime.agent_lab.room_merge import RoomMergeTrialAdapter, RoomMergeConfirmationAdapter
        room_merge=RoomMergeTrialAdapter(root/"room-merge",complete=complete,room_complete=room_complete,abort_session=service.runtime.abort)
        room_merge_confirm=RoomMergeConfirmationAdapter(root/"room-merge-confirm",baseline_root=baseline_run,room_complete=room_complete,abort_session=service.runtime.abort)
        scene_adapters={"micro-selfboot":adapter,"room-comparison":comparison,"context-continuation":ContextContinuationAdapter(),"room-merge":room_merge,"room-merge-confirm":room_merge_confirm}
        app=AgentLabTrialApplication(AgentLabTrialStore(root/"agent.sqlite"),scene_adapters,start_workers=False)
        reports=[]
        for task in tasks:
            scene=task if task in {"room-comparison","context-continuation","room-merge","room-merge-confirm"} else "micro-selfboot"
            admission=app.start("micro:"+root.name+":"+task,scene,{"taskId":task})
            job=app.run_job(admission["job"]["jobId"])["job"]
            reports.append(job)
            write_private(root/(task+"-job.json"),job)
            print("live",task,job["state"],canonical(job.get("result",{}).get("checks",[])),flush=True)
        write_private(root/"runtime-identity.json",{"piVersion":inst.pi_version,"manifestSha256":inst.manifest_sha256,
                      "execution":"isolated source AgentService + Lab TrialApplication + installed Pi binary"})
        return reports
    finally:
        if app is not None:app.close()
        if service is not None:service.close()
        (root/"config/auth.json").unlink(missing_ok=True)


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root",type=Path,required=True)
    parser.add_argument("--live",action="store_true")
    parser.add_argument("--tasks",nargs="+",choices=["contract","handoff","update","optimization","room-comparison","context-continuation","room-merge","room-merge-confirm"],default=["handoff","update","optimization","contract"])
    parser.add_argument("--baseline-run",type=Path,help="retained Room merge run for candidate confirmation")
    parser.add_argument("--fault-repeats",type=int,default=1)
    parser.add_argument("--skip-faults",action="store_true")
    args=parser.parse_args()
    root=args.output_root.expanduser().resolve()
    if root.is_relative_to(ROOT) or root.exists():
        parser.error("output-root must be new and outside the repository")
    if not 1<=args.fault_repeats<=3:
        parser.error("fault-repeats must be 1..3")
    if len(args.tasks)!=len(set(args.tasks)):
        parser.error("duplicate tasks are not allowed")
    root.mkdir(parents=True,mode=0o700)
    frozen={"suite":json.loads(SUITE.read_text()),"sourceHashes":{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
         for p in [Path(__file__),ROOT/"scripts/eval_paw_context_continuation.py",ROOT/"rag_ime/agent_lab/micro.py",ROOT/"rag_ime/agent_lab/room_comparison.py",ROOT/"rag_ime/agent_lab/room_merge.py",ROOT/"rag_ime/agent_lab/trial_execution.py",ROOT/"rag_ime/agent_lab/golden_pi.py"]}}
    write_private(root/"frozen.json",frozen)
    write_private(root/"skill-inventory.json",skill_inventory())
    result={"schemaVersion":"paw.micro-selfboot-run.v1","artifactRoot":str(root),"suiteSha256":digest(frozen["suite"])}
    if not args.skip_faults:
        result["reliability"]=reliability(root,args.fault_repeats)
    if args.live:
        try:
            result["jobs"]=live_trials(root,args.tasks,args.baseline_run)
        except Exception as error:
            result["liveFailureType"]=type(error).__name__
            # Private traceback for diagnosis; never put credentials into the public report.
            import traceback
            (root/"failure.log").write_text(traceback.format_exc())
    write_private(root/"summary.json",result)
    print("receipt",root/"summary.json",flush=True)
    failed=result.get("liveFailureType") or any(j["state"]!="completed" or j.get("result",{}).get("qualityVerdict")!="keep" for j in result.get("jobs",[]))
    return 1 if failed or ("reliability" in result and result["reliability"]["passedScenarios"]!=len(FAULT_TESTS)) else 0


if __name__=="__main__":
    raise SystemExit(main())
