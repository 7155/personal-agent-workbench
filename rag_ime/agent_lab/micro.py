"""Small synthetic trials through Lab's existing job and Pi completion owners.

No Agent loop, background retry, model judge, or production memory claim.
The host supplies completion/Room callbacks; expected values stay host-side.
"""
from __future__ import annotations

from ..agent_model_defaults import DEFAULT_AGENT_MODEL_ID

import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import zipfile

ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / "eval/micro-selfboot/tasks.v1.json"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def turn_usage(path, turn_id):
    """Sum all assistant calls within an exact persisted Pi turn binding.

    Final-message usage omits tool-search/retry calls. Never substitute it for
    the total when this exact transcript evidence is unavailable.
    """
    from .golden_pi import normalize_golden_usage
    raw = Path(path).read_bytes()
    active = False
    rows = []
    seen = set()
    for line in raw.splitlines():
        entry = json.loads(line)
        if entry.get("customType") == "rag-ime.pi-turn-binding":
            active = entry.get("data", {}).get("turnId") == turn_id
        if active and entry.get("type") == "message" and entry.get("message", {}).get("role") == "assistant":
            identity = entry.get("id")
            if not identity or identity in seen:
                raise ValueError("duplicate or missing transcript identity")
            seen.add(identity)
            rows.append(normalize_golden_usage(entry["message"].get("usage")))
    if not rows or any("totalTokens" not in row for row in rows):
        raise ValueError("exact turn usage unavailable")
    usage = {key:sum(row.get(key, 0) for row in rows) for key in ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "totalTokens")}
    costs = [row.get("estimatedCostUsd", row.get("costUsd")) for row in rows]
    if all(type(c) in {int,float} and math.isfinite(c) and c >= 0 for c in costs):
        usage.update(estimatedCostUsd=sum(costs),costBasis="pi_transcript_catalog_estimate")
    return {"usage":usage,"providerCalls":len(rows),"transcriptSha256":hashlib.sha256(raw).hexdigest()}


def write_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(canonical(value) + "\n")


def decode(text):
    # Equivalent JSON fencing is formatting; arbitrary prose is not repaired.
    value = text.strip()
    if value.startswith("```json\n") and value.endswith("\n```"):
        value = value[8:-4]
    return json.loads(value)


POLICY_APP = '''import json, pathlib, sys
def decide(config, expense):
    if expense['currency'] != config['currency']:
        return config['unknownCurrency']
    if config['receiptRequired'] and not expense['receipt']:
        return 'rejected'
    return 'approved' if expense['amount'] <= config['limit'] else 'rejected'
if __name__ == '__main__':
    config = json.loads(pathlib.Path(__file__).with_name('config.json').read_text())
    print(json.dumps({'decision': decide(config, json.load(sys.stdin))}))
'''


def verify_policy(config):
    cases = next(t for t in json.loads(SUITE.read_text())["tasks"] if t["id"] == "export")["cases"]
    valid = (isinstance(config, dict) and set(config) == {"currency", "limit", "receiptRequired", "unknownCurrency"}
             and isinstance(config["currency"], str) and type(config["limit"]) is int
             and type(config["receiptRequired"]) is bool and config["unknownCurrency"] in {"manual", "approved", "rejected"})
    namespace = {"__name__": "policy_verifier"}
    exec(POLICY_APP, namespace)  # Fixed host source only; never generated code.
    rows = []
    for case in cases:
        actual = namespace["decide"](config, case) if valid else None
        rows.append({"input": {k:v for k,v in case.items() if k != "expected"},
                     "actual": actual, "expected": case["expected"], "passed": actual == case["expected"]})
    passed = sum(row["passed"] for row in rows)
    return {"passed": passed, "total": len(rows), "allPassed": passed == len(rows), "cases": rows}


def verify_export(config, destination):
    """Run the exact exported runtime in a fresh cwd, empty HOME and -I Python.

    This proves a dependency-free CLI artifact, not native Extension App or a
    clean OS installation. The expected decisions never enter the ZIP.
    """
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / "expense-policy-app.zip"
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("app.py", POLICY_APP)
        z.writestr("config.json", canonical(config))
        z.writestr("README.txt", 'Requires Python 3 standard library only. Run python3 -I app.py and send one JSON expense on stdin: {"amount":600,"currency":"CNY","receipt":true}. No PAW installation or credentials required.\n')
    rows = []
    with tempfile.TemporaryDirectory(prefix="paw-export-clean-") as temporary:
        clean = Path(temporary)
        with zipfile.ZipFile(archive) as z:
            z.extractall(clean)
        for row in verify_policy(config)["cases"]:
            started = time.monotonic()
            result = subprocess.run([sys.executable, "-I", str(clean / "app.py")],
                input=canonical(row["input"]), text=True, capture_output=True, cwd=clean,
                env={"HOME": str(clean), "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"}, timeout=10)
            try:
                actual = json.loads(result.stdout)["decision"]
            except (ValueError, KeyError, TypeError):
                actual = None
            rows.append({**row, "actual": actual, "passed": result.returncode == 0 and actual == row["expected"],
                         "elapsedMs": round((time.monotonic()-started)*1000, 3), "exitCode": result.returncode})
    passed = sum(row["passed"] for row in rows)
    return {"passed": passed, "total": len(rows), "allPassed": passed == len(rows), "cases": rows,
            "archiveSha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "isolatedPython": True, "cleanMachineVerified": False, "nativeAppVerified": False,
            "artifactKind": "standalone_policy_cli"}


class BudgetExceeded(RuntimeError):
    pass


class MicroAdapter:
    """Trial adapter: one bounded task, with a maximum of one candidate."""
    def __init__(self, artifact_root, *, complete, room_complete=None, abort_session=None):
        self.artifact_root = Path(artifact_root)
        self.complete = complete
        self.room_complete = room_complete
        self.abort_session = abort_session

    def prepare(self, spec, job_id):
        if not isinstance(spec, dict) or set(spec) - {"taskId", "model", "thinkingLevel"}:
            raise ValueError("Micro trial accepts only taskId, model and thinkingLevel")
        suite = json.loads(SUITE.read_text())
        task_id = spec.get("taskId")
        if task_id not in {"contract", "handoff", "update", "optimization"}:
            raise ValueError("Unsupported micro task; reliability is a source test run and export follows optimization")
        model = spec.get("model", DEFAULT_AGENT_MODEL_ID)
        thinking = spec.get("thinkingLevel", "low")
        if model not in {DEFAULT_AGENT_MODEL_ID, "gpt-5.6-luna", "gpt-5.6-sol"} or thinking not in {"low", "medium"}:
            raise ValueError("Unsupported micro model controls")
        public = {"taskId":task_id, "suiteRevision":suite["revision"], "suiteSha256":digest(suite),
                  "model":model, "thinkingLevel":thinking, "budget":suite["budget"], "synthetic":True}
        private = {**public, "jobId":job_id, "task":next(t for t in suite["tasks"] if t["id"]==task_id)}
        return {"publicSpec":public, "privateInput":private}

    def execute(self, private, observer, cancelled):
        folder = self.artifact_root / digest(private["jobId"])
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_private(folder / "input.json", private)
        receipts, checks = [], []
        used = 0
        provider_calls = 0
        budget = private["budget"]
        task = private["task"]
        started = time.monotonic()
        status, reason = "completed", "finished"
        artifacts = {}

        def call(label, prompt, *, room_role=None):
            nonlocal used, provider_calls
            if cancelled():
                raise BudgetExceeded("cancelled")
            if len(receipts) >= budget["maxCallsPerTrial"] or provider_calls >= budget["maxCallsPerTrial"]:
                raise BudgetExceeded("call_budget_exceeded")
            if used is None or used >= budget["maxObservedTokensPerTrial"]:
                raise BudgetExceeded("usage_unavailable" if used is None else "token_budget_exceeded")
            observer.progress("Micro trial: " + label)
            callback = self.room_complete if room_role else self.complete
            if callback is None:
                raise BudgetExceeded("room_adapter_unavailable")
            def bind(session_id):
                if self.abort_session is None:
                    observer.bind_session(session_id)
                else:
                    observer.bind_session(session_id, cancel=lambda:self.abort_session(session_id))
            kwargs = {"request_id": private["jobId"]+":"+label,
                      "model":{"provider":"openai-codex", "model":private["model"], "thinkingLevel":private["thinkingLevel"]},
                      "prompt":prompt, "on_session":bind, "cancelled":cancelled}
            if room_role:
                kwargs["room_role"] = room_role
            try:
                result = callback(**kwargs)
            except Exception as error:
                import traceback
                (folder / f"failure-{len(receipts)+1}.log").write_text(traceback.format_exc())
                completion = getattr(error, "completion", None)
                receipts.append(dict(completion or {"receipt":{"status":"interrupted"}, "usage":{}}))
                write_private(folder / f"call-{len(receipts)}.json", receipts[-1])
                used = None
                raise BudgetExceeded("model_call_failed_or_uncertain") from error
            receipts.append(result)
            write_private(folder / f"call-{len(receipts)}.json", result)
            observer.bind_session(result.get("sessionId", ""), result.get("turnId", ""))
            tokens = result.get("usage", {}).get("totalTokens")
            provider_calls += result.get("providerCalls", 1)
            if type(tokens) not in {int, float} or not math.isfinite(tokens) or tokens < 0:
                used = None
                raise BudgetExceeded("usage_unavailable")
            used += tokens
            if used > budget["maxObservedTokensPerTrial"]:
                raise BudgetExceeded("token_budget_exceeded")
            if provider_calls > budget["maxCallsPerTrial"]:
                raise BudgetExceeded("call_budget_exceeded")
            return decode(result["text"])

        try:
            if task["id"] == "update":
                # An explicit controlled context fixture, not production memory retrieval.
                instruction = " These supplied decisions are the complete authoritative synthetic fixture. Current means as of its last recorded entry, not today's wall clock. Return JSON only."
                baseline = call("old-context", canonical({"history":task["input"]["history"][:1], "question":task["input"]["question"]})+instruction)
                current = call("updated-context", canonical(task["input"])+instruction)
                checks = [{"name":"old-context-control", "passed":baseline == {"currentLimit":500,"sourceDate":"2026-09-01"}},
                          {"name":"latest-decision", "passed":current == task["expected"]}]
            elif task["id"] == "handoff":
                handoff = call("predecessor", task["input"]["earlier"]+' Return a compact JSON handoff with a constraints array; do not implement.')
                answer = call("successor", canonical({"handoff":handoff,"task":task["input"]["next"]})+' Return JSON with path, idempotencyField, priorityRequired; no other keys.')
                checks = [{"name":key,"passed":isinstance(answer,dict) and answer.get(key)==expected} for key,expected in task["expected"].items()]
            elif task["id"] == "contract":
                instructions = ' List consumer contract mismatches as a JSON array of strings: request:<required API field> or response:<required API field>. No prose.'
                solo = call("solo",canonical(task["input"])+instructions)
                parts=[]
                for field in ("request","response"):
                    context={side:{field:task["input"][side][field]} for side in ("api","consumer")}
                    parts.append(call(field,canonical(context)+instructions,room_role=field))
                combined=call("integrate",canonical({"partnerFindings":parts})+' Merge these findings as one deduplicated JSON array of strings. Do not omit either partner.',room_role="integrate")
                checks=[{"name":"solo","passed":isinstance(solo,list) and sorted(solo)==sorted(task["expected"])},
                        {"name":"room-handoff","passed":isinstance(combined,list) and sorted(combined)==sorted(task["expected"])}]
                if not checks[-1]["passed"]:
                    diagnosis=call("room-diagnosis",canonical({"task":task["input"],"partnerFindings":parts,"merged":combined})+
                        ' Diagnose this contract audit. Return JSON with cause and candidatePrompt. The findings must name missing required API fields, not obsolete consumer names. Propose one bounded correction to the integrator; never invent findings.')
                    if not isinstance(diagnosis,dict) or not isinstance(diagnosis.get("candidatePrompt"),str) or not 1<=len(diagnosis["candidatePrompt"])<=4000:
                        raise ValueError("Invalid Room candidate")
                    repaired=call("integrate-candidate",canonical({"originalContract":task["input"],"partnerFindings":parts})+
                                  diagnosis["candidatePrompt"]+instructions,room_role="integrate")
                    repaired_ok=isinstance(repaired,list) and sorted(repaired)==sorted(task["expected"])
                    artifacts["roomRepair"]={"baselinePassed":False,"candidatePassed":repaired_ok,
                        "decision":"keep" if repaired_ok else "reject", "candidateCount":1,
                        "changedFactor":"integrator receives original contract plus one Lab diagnostic instruction",
                        "scope":"same-case development repair; not an unbiased Room performance gain"}
                    checks.append({"name":"room-candidate","passed":repaired_ok})
            elif task["id"] == "optimization":
                schema='Return JSON only: currency (string), limit (integer), receiptRequired (boolean), unknownCurrency (manual/approved/rejected). Use current policy.'
                baseline_prompt=canonical(task["input"])+"\n"+schema
                baseline=call("baseline",baseline_prompt)
                base_score=verify_policy(baseline)
                # One real Lab diagnosis call; no gold labels or expected config supplied.
                diagnosis=call("diagnosis",canonical({"request":task["input"],"actual":baseline,
                    "checksPassed":base_score["passed"],"checksTotal":base_score["total"]})+
                    ' Produce JSON {"cause":"...","candidatePrompt":"..."}. Improve or shorten the instruction once. Preserve every current policy rule. Omit irrelevant notes. Include output keys currency,limit,receiptRequired,unknownCurrency. Do not add facts.')
                if not isinstance(diagnosis,dict) or not isinstance(diagnosis.get("candidatePrompt"),str) or not 1 <= len(diagnosis["candidatePrompt"]) <= 4000:
                    raise ValueError("Invalid candidate")
                # Keep the application's output contract identical in both arms.
                # A generated Prompt may change business phrasing, not enum types.
                candidate=call("candidate",diagnosis["candidatePrompt"]+"\n"+schema)
                score=verify_policy(candidate)
                checks=[{"name":"baseline-quality","passed":base_score["allPassed"]},
                        {"name":"candidate-quality","passed":score["allPassed"]}]
                base_cost=receipts[0].get("usage",{}).get("estimatedCostUsd")
                candidate_cost=receipts[2].get("usage",{}).get("estimatedCostUsd")
                cost_known=all(type(c) in {int,float} and math.isfinite(c) and c>=0 for c in (base_cost,candidate_cost))
                decision=("reject" if not score["allPassed"] else "cost_unavailable" if not cost_known
                          else "keep" if candidate_cost<base_cost else "no_improvement")
                artifacts["comparison"]={"baseline":base_score,"candidate":score,
                    "baselinePromptCharacters":len(baseline_prompt),"candidatePromptCharacters":len(diagnosis["candidatePrompt"]),
                    "decision":decision, "decisionBasis":"quality first, then observed per-call catalog cost; one paired observation",
                    "baselineEstimatedCostUsd":base_cost,"candidateEstimatedCostUsd":candidate_cost,
                    "generalization":"candidate-aware synthetic development validation; not held-out"}
                if score["allPassed"]:
                    artifacts["export"]=verify_export(candidate,folder)
                    checks.append({"name":"export-functional-parity","passed":artifacts["export"]["allPassed"]})
        except BudgetExceeded as error:
            reason=str(error)
            status="cancelled" if reason=="cancelled" else "failed"
        except (ValueError,KeyError,TypeError):
            status,reason="failed","invalid_model_output"
        if cancelled():
            status,reason="cancelled","cancelled"
        public_receipts=[{"sessionId":r.get("sessionId"),"turnId":r.get("turnId"),
                          "usage":r.get("usage",{}),"receipt":r.get("receipt",{})} for r in receipts]
        costs=[r.get("usage",{}).get("estimatedCostUsd",r.get("usage",{}).get("costUsd")) for r in receipts]
        costs_known=bool(costs) and all(type(c) in {int,float} and math.isfinite(c) and c>=0 for c in costs)
        acceptance=[c for c in checks if c["name"] not in ({"room-handoff"} if "roomRepair" in artifacts else set())]
        report={"status":status,"taskId":task["id"],"stopReason":reason,"synthetic":True,
                "qualityVerdict":"keep" if status=="completed" and acceptance and all(c["passed"] for c in acceptance) else "reject",
                "checks":checks,"completionAttempts":len(receipts),"modelCalls":provider_calls if used is not None else None,"totalTokens":used,
                "elapsedMs":round((time.monotonic()-started)*1000,3),"receipts":public_receipts,
                "estimatedCostUsd":sum(costs) if costs_known else None,"providerBill":False,
                "artifacts":artifacts,"artifactKey":folder.name,
                "boundaries":["source Lab adapter over real Pi; not installed frontend acceptance",
                    "handoff/update are controlled context fixtures, not native memory retrieval or compaction",
                    "Room is externally scripted dispatch; not autonomous decomposition",
                    "20k is a settlement-time stop threshold; in-flight input tokens are not hard-capped"]}
        write_private(folder/"report.json",report)
        return report
