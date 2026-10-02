"""A bounded Lab workflow trial: avoid an unnecessary model merge.

The optional deterministic operator validates shape and identity only. It does
not inspect Gold, repair partner values, or replace Pi's Session/Room owners.
"""
from __future__ import annotations

import math
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from ..agent_model_defaults import DEFAULT_AGENT_MODEL_ID
from .micro import canonical, decode, digest, turn_usage, write_private
from .room_comparison import CASES, score

SCHEMAS = {
    "room-identity":{"caseId":"string","turnField":"string","clientField":"string"},
    "quality-state":{"caseId":"string","executionComplete":"boolean","qualityPassed":"boolean"},
    "whole-turn-usage":{"caseId":"string","tokens":"integer","modelCalls":"integer"},
}
TASKS = [{**case,"outputSchema":SCHEMAS[case["caseId"]]} for case in CASES]
INSTRUCTION = "\nInstruction: All facts are supplied. Do not call tools. Return only one JSON array. Each supplied case needs exactly one object matching its outputSchema; integer means one scalar integer, boolean means true or false. Preserve caseId. No extra fields."
BUDGET = {"maxProviderCalls":6,"maxObservedTokens":20000,"maxOutputTokens":768,
          "candidateLimit":1,"armMaxProviderCalls":3,"armMaxObservedTokens":12000}


def merge_disjoint(outputs, contracts):
    """Lossless merge, rejecting overlaps, omissions and malformed rows."""
    schemas={case["caseId"]:case["outputSchema"] for case in contracts}
    rows={}
    types={"string":str,"boolean":bool,"integer":int}
    for output in outputs:
        if not isinstance(output,list):raise ValueError("partner output is not an array")
        for row in output:
            if not isinstance(row,dict) or not isinstance(row.get("caseId"),str):
                raise ValueError("missing case identity")
            case_id=row["caseId"]
            if case_id in rows or case_id not in schemas:raise ValueError("overlapping or unexpected case")
            schema=schemas[case_id]
            if set(row)!=set(schema) or any(type(row[key]) is not types[kind] for key,kind in schema.items()):
                raise ValueError("invalid output shape")
            rows[case_id]=row
    if set(rows)!=set(schemas):raise ValueError("incomplete partner output")
    return [rows[case["caseId"]] for case in contracts]


def _finite(value):
    return type(value) in (int,float) and math.isfinite(value) and value>=0


class RoomMergeTrialAdapter:
    def __init__(self,artifact_root,*,complete,room_complete,abort_session):
        self.root=Path(artifact_root)
        self.complete=complete
        self.room_complete=room_complete
        self.abort_session=abort_session

    def prepare(self,spec,job_id):
        if spec!={"taskId":"room-merge"}:raise ValueError("fixed workflow trial only")
        public={**spec,"target":"workflow","commonPromptRevision":"typed-contract-v2",
                "taskSha256":digest(TASKS),"budget":dict(BUDGET),"model":DEFAULT_AGENT_MODEL_ID,"thinkingLevel":"low",
                "allowedOperator":"deterministic_disjoint_merge","synthetic":True}
        return {"publicSpec":public,"privateInput":{**public,"jobId":job_id}}

    def execute(self,private,observer,cancelled):
        limits=private["budget"]
        folder=self.root/digest(private["jobId"])
        folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        write_private(folder/"frozen.json",{"spec":private,"tasks":TASKS,"instruction":INSTRUCTION,
            "qualityGate":"all three exact case outputs, with schema and identity validity",
            "primaryObjective":"lower estimated cost per successful complete case group",
            "candidateDiff":"replace third model merge with lossless shape-checked merge; worker prompts unchanged",
            "baseline":"two worker Room Sessions plus a model integrator; fresh Room per arm"})
        observed={"providerCalls":0,"tokens":0,"costUsd":0.0,"costComplete":True,"budgetExceeded":False}
        arms={name:{"calls":[],"providerCalls":0,"tokens":0,"costUsd":0.0} for name in ("baseline","candidate")}
        diagnosis=None
        error=None

        def call(arm,label,packet,role=None):
            state=arms.get(arm)
            if cancelled():raise InterruptedError("cancelled")
            if observed["budgetExceeded"] or not observed["costComplete"] or observed["providerCalls"]>=limits["maxProviderCalls"] or observed["tokens"]>=limits["maxObservedTokens"]:
                raise RuntimeError("trial_budget_exhausted_or_unknown")
            if state and (state["providerCalls"]>=limits["armMaxProviderCalls"] or state["tokens"]>=limits["armMaxObservedTokens"]):raise RuntimeError("arm_budget_exhausted")
            kwargs={"request_id":private["jobId"]+":"+label,"model":{"provider":"openai-codex","model":private["model"],"thinkingLevel":private["thinkingLevel"]},
                    "prompt":canonical(packet)+(INSTRUCTION if role else "\nInstruction: Return JSON with cause and operator. Use only the supplied trace. No tools."),
                    "on_session":lambda sid:observer.bind_session(sid,cancel=lambda:self.abort_session(sid)),"cancelled":cancelled}
            if role:kwargs.update(room_role=role,room_group=private["jobId"]+":"+arm,release_after=True)
            started=time.monotonic()
            observer.progress("Room workflow trial: "+label)
            try:
                result=(self.room_complete if role else self.complete)(**kwargs)
            except Exception:
                observed["costComplete"]=False
                raise
            write_private(folder/(label+".json"),result)
            usage=result.get("usage",{})
            count=result.get("providerCalls")
            tokens=usage.get("totalTokens")
            cost=usage.get("estimatedCostUsd")
            if type(count) is not int or count<1 or not _finite(tokens) or not _finite(cost):
                observed["costComplete"]=False
                raise RuntimeError("complete_turn_usage_unavailable")
            observed["providerCalls"]+=count;observed["tokens"]+=tokens;observed["costUsd"]+=cost
            entry={"label":label,"sessionId":result["sessionId"],"turnId":result["turnId"],"usage":usage,"providerCalls":count,
                   "wallMs":round((time.monotonic()-started)*1000,3),"receipt":result["receipt"]}
            if state is not None:
                state["calls"].append(entry);state["providerCalls"]+=count;state["tokens"]+=tokens;state["costUsd"]+=cost
            if observed["providerCalls"]>limits["maxProviderCalls"] or observed["tokens"]>limits["maxObservedTokens"] or (state and (state["providerCalls"]>limits["armMaxProviderCalls"] or state["tokens"]>limits["armMaxObservedTokens"])):
                # Preserve a completed answer for free host-side grading.
                # The next paid call is blocked above; budget is not quality.
                observed["budgetExceeded"]=True
            return decode(result["text"]),entry

        def workers(arm):
            first,_=call(arm,arm+"-partner-a",{"cases":TASKS[:2]},"request")
            second,_=call(arm,arm+"-partner-b",{"cases":TASKS[2:]},"response")
            return [first,second]

        try:
            started=time.monotonic()
            baseline_workers=workers("baseline")
            baseline,_=call("baseline","baseline-integrator",{"cases":TASKS,"partnerEvidence":baseline_workers},"integrate")
            arms["baseline"].update(answer=baseline,score=score(merge_disjoint([baseline],TASKS)),wallMs=round((time.monotonic()-started)*1000,3))
            diagnosis,diagnosis_receipt=call("diagnosis","diagnosis",{
                "trace":{"workerOutputs":baseline_workers,"integratedOutput":baseline,"measurement":arms["baseline"]},
                "objective":"preserve every structured finding while reducing paid model calls and cost",
                "allowedOperators":["deterministic_disjoint_merge","keep_baseline"],
                "operatorContract":"deterministic_disjoint_merge checks case identity and field types, rejects overlap/omission and preserves values. It does not use Gold or repair answers."})
            if not isinstance(diagnosis,dict) or diagnosis.get("operator")!="deterministic_disjoint_merge":
                raise ValueError("diagnosis_did_not_select_available_operator")
            write_private(folder/"candidate.json",{"diagnosis":diagnosis,"receipt":diagnosis_receipt,"target":"workflow",
                "workerPromptUnchanged":True,"operator":"deterministic_disjoint_merge"})
            started=time.monotonic()
            candidate_workers=workers("candidate")
            merged=merge_disjoint(candidate_workers,TASKS)
            arms["candidate"].update(answer=merged,score=score(merged),wallMs=round((time.monotonic()-started)*1000,3))
        except Exception as exc:
            error=type(exc).__name__+":"+str(exc)
        quality=not error and all(arm.get("score",{}).get("allPassed") for arm in arms.values())
        saving=arms["baseline"]["costUsd"]-arms["candidate"]["costUsd"] if quality and observed["costComplete"] else None
        improved=saving is not None and saving>0
        for arm in arms.values():
            arm["costPerSuccessfulGroupUsd"]=arm["costUsd"] if arm.get("score",{}).get("allPassed") else None
        report={"schemaVersion":"paw.room-workflow-optimization.v1","status":"failed" if error else "completed",
                "qualityVerdict":"keep" if quality else "reject","optimizationVerdict":"budget_exhausted" if observed["budgetExceeded"] else "improved" if improved else "failed" if error else "no_improvement",
                "error":error,"arms":arms,"diagnosis":diagnosis,"observed":observed,"budget":limits,
                "comparison":{"sameWorkerInputs":True,"sameModel":True,"sameArmBudget":True,"freshRoomPerArm":True,
                    "estimatedSavingPerGroupUsd":saving,"estimatedCostReductionPct":100*saving/arms["baseline"]["costUsd"] if improved else None,
                    "experimentCostUsd":observed["costUsd"] if observed["costComplete"] else None,
                    "breakEvenGroupsIncludingEntireExperiment":math.ceil(observed["costUsd"]/saving) if improved else None},
                "boundary":"one paired synthetic workflow trial; typed prompt frozen identically before both arms; does not compare Room against a single Session, prove generic delegation gains, or include engineering labor in payback"}
        write_private(folder/"report.json",report)
        return report


def reconcile_completed_trial(source_root):
    """Regrade retained outputs and reconcile every exact Pi turn, no calls."""
    root=Path(source_root)
    job_file=root/"room-merge-job.json"
    job=json.loads(job_file.read_text())
    if job["publicSpec"]["taskSha256"]!=digest(TASKS):raise ValueError("frozen task changed")
    folder=root/"room-merge"/digest(job["jobId"])
    frozen=json.loads((folder/"frozen.json").read_text())
    if frozen["tasks"]!=TASKS or frozen["instruction"]!=INSTRUCTION:raise ValueError("frozen worker prompt changed")
    measured={}
    with sqlite3.connect(root/"agent.sqlite") as conn:
        for label in ("baseline-partner-a","baseline-partner-b","baseline-integrator","diagnosis","candidate-partner-a","candidate-partner-b"):
            receipt=json.loads((folder/(label+".json")).read_text())
            if receipt["receipt"]["requestId"]!=job["jobId"]+":"+label or receipt["receipt"]["model"]!={"provider":"openai-codex","model":DEFAULT_AGENT_MODEL_ID,"thinkingLevel":"low"}:
                raise ValueError("model or request identity changed")
            path=conn.execute("SELECT session_file FROM agent_sessions WHERE id=?",(receipt["sessionId"],)).fetchone()[0]
            actual=turn_usage(path,receipt["turnId"])
            if actual["usage"]!=receipt["usage"] or actual["transcriptSha256"]!=receipt["transcriptSha256"] or actual["providerCalls"]!=receipt["providerCalls"]:
                raise ValueError("exact transcript receipt mismatch")
            measured[label]={**actual,"answer":decode(receipt["text"]),"receipt":receipt["receipt"]}
    arms={}
    for name in ("baseline","candidate"):
        labels=[name+"-partner-a",name+"-partner-b"]+(["baseline-integrator"] if name=="baseline" else [])
        answer=merge_disjoint([measured["baseline-integrator"]["answer"]],TASKS) if name=="baseline" else merge_disjoint([measured[label]["answer"] for label in labels],TASKS)
        arms[name]={"score":score(answer),"answer":answer,"costUsd":sum(measured[label]["usage"]["estimatedCostUsd"] for label in labels),
                    "tokens":sum(measured[label]["usage"]["totalTokens"] for label in labels),"providerCalls":sum(measured[label]["providerCalls"] for label in labels)}
    total_tokens=sum(row["usage"]["totalTokens"] for row in measured.values())
    return {"schemaVersion":"paw.room-merge-reconciliation.v1","sourceJobSha256":hashlib.sha256(job_file.read_bytes()).hexdigest(),
            "sourceOriginalStatus":job["result"]["optimizationVerdict"],"arms":arms,"newProviderCalls":0,"verifiedTranscripts":len(measured),
            "experimentTokens":total_tokens,"tokenThreshold":20000,"tokensOverThreshold":max(0,total_tokens-20000),
            "experimentCostUsd":sum(row["usage"]["estimatedCostUsd"] for row in measured.values()),
            "diagnosis":measured["diagnosis"]["answer"],"boundary":"original budget-failure receipt retained; outputs graded without new model calls or Gold changes"}


class RoomMergeConfirmationAdapter:
    """Repeat only the already selected candidate, with a two-call budget."""
    def __init__(self,artifact_root,*,baseline_root,room_complete,abort_session):
        self.root=Path(artifact_root)
        self.baseline_root=baseline_root
        self.room_complete=room_complete
        self.abort_session=abort_session

    def prepare(self,spec,job_id):
        if spec!={"taskId":"room-merge-confirm"} or self.baseline_root is None:raise ValueError("explicit retained baseline required")
        reconciled=reconcile_completed_trial(self.baseline_root)
        if not reconciled["arms"]["baseline"]["score"]["allPassed"] or reconciled["diagnosis"].get("operator")!="deterministic_disjoint_merge":
            raise ValueError("baseline quality or operator selection unavailable")
        public={**spec,"taskSha256":digest(TASKS),"model":DEFAULT_AGENT_MODEL_ID,"thinkingLevel":"low",
                "maxProviderCalls":2,"maxObservedTokens":10000,"baselineReceiptSha256":reconciled["sourceJobSha256"],"candidateFrozenBeforeConfirmation":True}
        return {"publicSpec":public,"privateInput":{**public,"jobId":job_id,"source":reconciled}}

    def execute(self,private,observer,cancelled):
        folder=self.root/digest(private["jobId"])
        folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        write_private(folder/"frozen.json",private)
        observed={"providerCalls":0,"tokens":0,"costUsd":0.0,"costComplete":True}
        outputs=[]
        error=None
        started=time.monotonic()
        try:
            for label,cases,role in (("partner-a",TASKS[:2],"request"),("partner-b",TASKS[2:],"response")):
                if cancelled():raise InterruptedError("cancelled")
                if observed["providerCalls"]>=private["maxProviderCalls"] or observed["tokens"]>=private["maxObservedTokens"]:raise RuntimeError("confirmation_budget_exhausted")
                result=self.room_complete(request_id=private["jobId"]+":"+label,
                    model={"provider":"openai-codex","model":private["model"],"thinkingLevel":private["thinkingLevel"]},
                    prompt=canonical({"cases":cases})+INSTRUCTION,room_role=role,room_group=private["jobId"],release_after=True,
                    on_session=lambda sid:observer.bind_session(sid,cancel=lambda:self.abort_session(sid)),cancelled=cancelled)
                write_private(folder/(label+".json"),result)
                usage=result.get("usage",{})
                if type(result.get("providerCalls")) is not int or result["providerCalls"]<1 or not _finite(usage.get("totalTokens")) or not _finite(usage.get("estimatedCostUsd")):
                    raise ValueError("complete_turn_usage_unavailable")
                observed["providerCalls"]+=result["providerCalls"];observed["tokens"]+=usage["totalTokens"];observed["costUsd"]+=usage["estimatedCostUsd"]
                outputs.append(decode(result["text"]))
            answer=merge_disjoint(outputs,TASKS)
            quality=score(answer)
        except Exception as exc:
            error=type(exc).__name__+":"+str(exc)
            observed["costComplete"]=False
            answer=None;quality=None
        baseline=private["source"]["arms"]["baseline"]
        within=observed["providerCalls"]<=private["maxProviderCalls"] and observed["tokens"]<=private["maxObservedTokens"] and not error
        passed=bool(quality and quality["allPassed"])
        saving=baseline["costUsd"]-observed["costUsd"] if passed and observed["costComplete"] else None
        report={"schemaVersion":"paw.room-merge-confirmation.v1","status":"failed" if error else "completed","error":error,
                "qualityVerdict":"keep" if passed else "reject","optimizationVerdict":"improved" if saving is not None and saving>0 and within else "no_improvement",
                "baseline":baseline,"candidate":{**observed,"score":quality,"answer":answer,"wallMs":round((time.monotonic()-started)*1000,3)},
                "withinBudget":within,"comparison":{"estimatedCostReductionPct":100*saving/baseline["costUsd"] if saving is not None else None,
                    "tokenReductionPct":100*(baseline["tokens"]-observed["tokens"])/baseline["tokens"],"providerCallReductionPct":100*(baseline["providerCalls"]-observed["providerCalls"])/baseline["providerCalls"],
                    "baselineIsRetained":True,"sameTasksModelWorkerPrompt":True,"candidateWasFrozenBeforeRun":True},
                "priorExperiment":private["source"],"boundary":"one fresh candidate confirmation against the retained matched baseline; not a new paired baseline trial or production population claim"}
        write_private(folder/"report.json",report)
        return report
