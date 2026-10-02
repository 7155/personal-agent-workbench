"""Equal-ceiling, small paired Session/Room evaluation over three PAW contracts."""
from __future__ import annotations

import json
import math
from pathlib import Path
import time

from ..agent_model_defaults import DEFAULT_AGENT_MODEL_ID
from .micro import canonical, decode, digest, write_private

CASES = [
    {"caseId":"room-identity", "source":"agent_room_session_dispatch.py public receipt",
     "task":"Choose the accepted Pi turn field and its Pi client identity field from dispatch; userClientId belongs to the Room command.",
     "input":{"dispatch":{"sessionTurnId":"turn-7","dispatchId":"dispatch-9"},"userClientId":"user-3"},
     "outputKeys":["caseId","turnField","clientField"]},
    {"caseId":"quality-state", "source":"agent_lab_trial_execution.py quality/terminal separation",
     "task":"Report whether execution is complete and whether quality passed. An execution can complete with rejected quality.",
     "input":{"state":"completed","result":{"qualityVerdict":"reject"}},
     "outputKeys":["caseId","executionComplete","qualityPassed"]},
    {"caseId":"whole-turn-usage", "source":"agent_lab_golden_pi.py vs full Pi transcript accounting",
     "task":"Sum all input, output and cacheRead tokens in the entire turn; report the number of assistant model calls. These input numbers exclude cache reads.",
     "input":{"assistantCalls":[{"input":100,"output":10,"cacheRead":0},{"input":120,"output":20,"cacheRead":30}]},
     "outputKeys":["caseId","tokens","modelCalls"]},
]
EXPECTED = {
    "room-identity":{"caseId":"room-identity","turnField":"sessionTurnId","clientField":"dispatchId"},
    "quality-state":{"caseId":"quality-state","executionComplete":True,"qualityPassed":False},
    "whole-turn-usage":{"caseId":"whole-turn-usage","tokens":280,"modelCalls":2},
}
BUDGET = {"maxProviderCalls":4,"maxObservedTokens":12000,"maxOutputTokens":768,"maxSeconds":90}


def score(answer):
    rows = answer if isinstance(answer,list) else []
    counts = {key:sum(isinstance(r,dict) and r.get("caseId")==key for r in rows) for key in EXPECTED}
    valid = {r.get("caseId"):r for r in rows if isinstance(r,dict) and isinstance(r.get("caseId"),str)}
    cases = [{"caseId":key,"passed":counts[key]==1 and valid.get(key)==expected} for key,expected in EXPECTED.items()]
    extra = sum(not isinstance(r,dict) or not isinstance(r.get("caseId"),str) or r["caseId"] not in EXPECTED for r in rows)
    return {"cases":cases,"passed":sum(c["passed"] for c in cases),"total":len(cases),
            "extraRows":extra,"allPassed":all(c["passed"] for c in cases) and extra==0}


class RoomComparisonAdapter:
    def __init__(self, artifact_root, *, complete, room_complete, abort_session):
        self.root=Path(artifact_root)
        self.complete=complete
        self.room_complete=room_complete
        self.abort_session=abort_session

    def prepare(self,spec,job_id):
        if spec != {"taskId":"room-comparison"}:
            raise ValueError("Room comparison uses one fixed task and model contract")
        public={"taskId":"room-comparison","caseCount":3,"model":DEFAULT_AGENT_MODEL_ID,"thinkingLevel":"low",
                "armBudget":BUDGET,"caseSha256":digest(CASES),"synthetic":True}
        return {"publicSpec":public,"privateInput":{**public,"jobId":job_id}}

    def execute(self,private,observer,cancelled):
        folder=self.root/digest(private["jobId"])
        folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        write_private(folder/"frozen.json",{"request":private,"cases":CASES,"expected":EXPECTED})
        arms={key:{"calls":[],"tokens":0,"providerCalls":0,"cost":0.0,"costKnown":True} for key in ("solo","room")}
        instruction=' All information is supplied. Do not call tools. Return only a JSON array, one object per supplied case using its outputKeys, with native JSON booleans/numbers. No extra fields.'
        def run(arm,label,cases,role=None,context=None):
            state=arms[arm]
            if cancelled():raise InterruptedError("cancelled")
            if state["providerCalls"]>=BUDGET["maxProviderCalls"] or state["tokens"]>=BUDGET["maxObservedTokens"]:
                raise RuntimeError("arm_budget_exhausted")
            packet={"cases":cases}
            if context is not None:packet["partnerEvidence"]=context
            kwargs={"request_id":private["jobId"]+":"+label,
                    "model":{"provider":"openai-codex","model":private["model"],"thinkingLevel":private["thinkingLevel"]},
                    "prompt":canonical(packet)+instruction,"cancelled":cancelled,
                    "on_session":lambda sid:observer.bind_session(sid,cancel=lambda:self.abort_session(sid))}
            if role:kwargs["room_role"]=role
            start=time.monotonic()
            try:
                result=(self.room_complete if role else self.complete)(**kwargs)
            except Exception:
                # An accepted call can fail before returning its receipt. The
                # already observed spend remains a lower bound, never zero.
                state["costKnown"]=False
                raise
            write_private(folder/(label+".json"),result)
            usage=result.get("usage",{})
            if not isinstance(usage.get("totalTokens"),(int,float)) or isinstance(usage.get("totalTokens"),bool) or not math.isfinite(usage["totalTokens"]) or usage["totalTokens"]<0 or not isinstance(result.get("providerCalls"),int) or isinstance(result.get("providerCalls"),bool) or result["providerCalls"]<1:
                state["costKnown"]=False
                raise RuntimeError("exact_turn_usage_unavailable")
            state["tokens"]+=usage["totalTokens"]
            state["providerCalls"]+=result["providerCalls"]
            cost=usage.get("estimatedCostUsd")
            cost_valid=isinstance(cost,(int,float)) and not isinstance(cost,bool) and math.isfinite(cost) and cost>=0
            state["costKnown"] &= cost_valid
            if cost_valid:state["cost"]+=cost
            answer=decode(result["text"])
            state["calls"].append({"label":label,"sessionId":result["sessionId"],"turnId":result["turnId"],
                                   "elapsedMs":round((time.monotonic()-start)*1000,3),"usage":usage,
                                   "answer":answer,"receipt":result["receipt"]})
            if state["providerCalls"]>BUDGET["maxProviderCalls"] or state["tokens"]>BUDGET["maxObservedTokens"]:
                raise RuntimeError("arm_budget_exhausted")
            return answer
        error=None
        try:
            started=time.monotonic();solo=run("solo","solo",CASES)
            arms["solo"].update(score=score(solo),wallMs=round((time.monotonic()-started)*1000,3))
            started=time.monotonic()
            first=run("room","partner-a",CASES[:2],role="request")
            second=run("room","partner-b",CASES[2:],role="response")
            merged=run("room","integrator",CASES,role="integrate",context=[first,second])
            worker_rows=(first if isinstance(first,list) else [])+(second if isinstance(second,list) else [])
            known_correct={r["caseId"] for r in worker_rows if isinstance(r,dict) and isinstance(r.get("caseId"),str) and r==EXPECTED.get(r["caseId"])}
            kept={r["caseId"] for r in merged if isinstance(r,dict) and isinstance(r.get("caseId"),str) and r==EXPECTED.get(r["caseId"])} if isinstance(merged,list) else set()
            duplicate_rows=len(worker_rows)-len({canonical(row) for row in worker_rows})
            arms["room"].update(score=score(merged),wallMs=round((time.monotonic()-started)*1000,3),
                correctHandoffFacts=len(known_correct),omittedCorrectHandoffFacts=len(known_correct-kept),
                duplicateFindingRows=duplicate_rows,assignedCaseOverlap=0)
        except Exception as exc:
            error=type(exc).__name__+":"+str(exc)
        for state in arms.values():
            count=state.get("score",{}).get("passed",0)
            state["costPerSuccessfulCaseUsd"]=state["cost"]/count if count and state["costKnown"] else None
        report={"schemaVersion":"paw.room-comparison.v1","status":"failed" if error else "completed",
                "qualityVerdict":"reject" if error or not all(a.get("score",{}).get("allPassed") for a in arms.values()) else "keep",
                "error":error,"arms":arms,"sameModel":True,"sameCaseInputs":True,"sameBudgetCeiling":True,
                "armBudget":BUDGET,"uniqueCases":3,"repetitions":1,
                "boundary":"scripted Room decomposition, sequential partner dispatch; same-case budget ceiling, not autonomous scheduling, latency generalization or production success rate"}
        write_private(folder/"report.json",report)
        return report
