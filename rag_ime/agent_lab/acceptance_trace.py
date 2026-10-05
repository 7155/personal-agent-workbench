"""Actual PAW gateway observations plus controlled live read fixtures.

Wraps the existing gateway; never manufactures model messages or settlements.
Only registered synthetic reads receive delays/fault injection. All normal tool
calls execute their original production implementation. No model calls here.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import threading
import time

READS = {"read", "workspace_read"}
WRITES = {"write", "workspace_write", "edit", "workspace_edit", "workspace_patch"}
PROCESSES = {"bash", "workspace_shell", "workspace_job"}
DELEGATIONS = {"delegate", "delegate_tasks", "subagent", "agent", "agents"}
READONLY = READS | {"ls", "find", "grep", "workspace_list", "workspace_search"}
# These known Host-owned discovery/read operations do not traverse the PAW
# execute gateway. They still require exact-turn durable start+finish evidence.
HOST_META = {"codemode", "tool_search", "tool_load", "skill_search", "skill_load"}

def hashes(root):
    return {str(p.relative_to(root)):("SYMLINK" if p.is_symlink() else hashlib.sha256(p.read_bytes()).hexdigest())
            for p in sorted(root.rglob("*")) if p.is_symlink() or p.is_file()}

class GatewayTrace:
    def __init__(self):
        self.rows = []
        self.lock = threading.Lock()
        self.local = threading.local()
        self.configs = {}
        self.installed = False
        self.active_calls = 0

    def install(self, service, gateway):
        if self.installed:
            raise ValueError("trace already installed")
        self.service = service
        execute, read = gateway.execute, gateway.workspace_harness.read
        self.original_execute, self.original_read = execute, read
        self.gateway = gateway
        def traced_execute(payload):
            sid = str(payload.get("sessionId", ""))
            config = self.configs.get(sid)
            if config is None:
                return execute(payload)
            row = {"sessionId":sid,"toolCallId":payload.get("toolCallId"),
                   "binding":dict(payload.get("executionBinding") or {}),
                   "toolName":payload.get("tool"),"args":dict(payload.get("args") or {}),
                   "startedNs":time.monotonic_ns(),"before":hashes(config["workspace"])}
            self.local.row = row
            try:
                result = execute(payload)
                row["ok"] = result.get("ok") is True
                row["result"] = result
                return result
            except BaseException as error:
                row["ok"] = False
                row["exceptionType"] = type(error).__name__
                # Only synthetic known marker is kept, never arbitrary secrets.
                if "FIXTURE_E_TRANSIENT" in str(error):
                    row["fixtureErrorCode"] = "FIXTURE_E_TRANSIENT"
                raise
            finally:
                row["endedNs"] = time.monotonic_ns()
                row["after"] = hashes(config["workspace"])
                with self.lock:
                    self.rows.append(row)
                self.local.row = None
        def fixture_read(session, args):
            row = getattr(self.local, "row", None)
            config = self.configs.get(str(session.get("id", "")))
            if row is not None and config:
                requested = Path(str(args.get("path", "")))
                path = requested if requested.is_absolute() else config["workspace"]/requested
                fixture = config["reads"].get(str(path.resolve()))
                if fixture:
                    row["fixtureIndex"] = fixture["index"]
                    if config.get("barrier") is not None:
                        row["barrierArrivalNs"] = time.monotonic_ns()
                        with self.lock:
                            if fixture["index"] in config["barrierIndices"]:
                                raise ValueError("DUPLICATE_FIXTURE_BARRIER_ARRIVAL")
                            config["barrierIndices"].add(fixture["index"])
                        config["barrier"].wait()
                        row["barrierReleased"] = True
                    row["fixtureStartedNs"] = time.monotonic_ns()
                    try:
                        time.sleep(fixture.get("delayMs", 0)/1000)
                        if fixture.get("errorCode"):
                            raise ValueError(fixture["errorCode"])
                        result = read(session, args)
                        row["fixtureValue"] = fixture["value"]
                        return result
                    finally:
                        row["fixtureEndedNs"] = time.monotonic_ns()
            return read(session, args)
        def tracked_execute(payload):
            with self.lock:
                self.active_calls += 1
            try:
                return traced_execute(payload)
            finally:
                with self.lock:
                    self.active_calls -= 1
        gateway.execute = tracked_execute
        gateway.workspace_harness.read = fixture_read
        self.installed = True

    def configure(self, task, session_id, workspace, authorization=None):
        if not self.installed:
            raise ValueError("gateway trace is not installed")
        config = {"workspace":Path(workspace).resolve(),"reads":{},"authorizedNs":time.monotonic_ns() if authorization else None}
        self.configs[session_id] = config
        cid = task["id"]
        if cid in {"ptc-parallel", "ptc-parallel-barrier-v2"}:
            if cid == "ptc-parallel-barrier-v2":
                barrier = task["fixture"]["barrier"]
                config["barrier"] = threading.Barrier(barrier["parties"],timeout=barrier["timeoutSeconds"])
                config["barrierIndices"] = set()
            for index, (value, delay) in enumerate(zip(task["fixture"]["values"], task["fixture"]["delaysMs"])):
                path = config["workspace"]/f"fixture-{index}.json"
                path.write_text(json.dumps({"value":value})+"\n")
                config["reads"][str(path)] = {"index":index,"value":value,"delayMs":delay}
            return "Use ONE codemode program to call the actual read tool concurrently for fixture-0.json, fixture-1.json, fixture-2.json with Promise.all. Do not invoke bash or write. Return only JSON {\"values\":[...],\"sum\":...}. Discover the actual read tool schema if necessary."
        if cid == "ptc-error":
            for index in (0,1):
                path = config["workspace"]/f"fixture-{index}.json"
                path.write_text(json.dumps({"value":7})+"\n")
                config["reads"][str(path)] = {"index":index,"value":7,"delayMs":100,**({"errorCode":"FIXTURE_E_TRANSIENT"} if index else {})}
            return "Use ONE codemode program to concurrently read fixture-0.json and fixture-1.json using the actual read tool. Handle failure separately, do not retry or write. Return only JSON {\"value\":7,\"error\":\"the exact error code\"}. Do not substitute a result for the failed read."
        if cid == "classic-handoff":
            return task["input"]+" 请使用实际 read、write、read 工具按顺序完成；不要用 bash 或 codemode 替代。"
        return task["input"]

    def __call__(self, *, task, session_id, admissions, receipts, authorization, compaction, workspace, final_text=""):
        events = []
        exact = bool(receipts)
        for receipt in receipts:
            evidence = self.service.runtime.session_tool_evidence(session_id,
                turn_id=receipt["turnId"], client_message_id=receipt["clientMessageId"])
            exact = exact and evidence.get("sessionId") == session_id and evidence.get("turnId") == receipt["turnId"]
            events.extend(evidence["toolHistoryEvents"])
        turn_ids = {r["turnId"] for r in receipts}
        rows = [r for r in self.rows if r["sessionId"] == session_id and r["binding"].get("turnId") in turn_ids]
        starts = [e for e in events if e["eventType"] == "tool_started"]
        finishes = {e["payload"]["toolCallId"]:e["payload"] for e in events if e["eventType"] == "tool_finished"}
        # A transcript tool draft alone cannot prove execution. Every non-code
        # tool result must also have an observed gateway receipt, including errors.
        observed = {r["toolCallId"] for r in rows if r["toolName"] not in HOST_META}
        required = {e["payload"]["toolCallId"] for e in starts if e["payload"].get("toolName") not in HOST_META}
        complete = self.installed and exact and required == observed and all(e["payload"]["toolCallId"] in finishes for e in starts)
        facts = {"complete":complete,"exactIdentity":exact,"gatewayCalls":rows,
                 "durableToolEvents":events,"codemodeCalls":sum(e["payload"].get("toolName") == "codemode" for e in starts)}
        # Unknown tool semantics cannot establish a negative no-mutation claim.
        mutating = [r for r in rows if r["toolName"] not in READONLY or r["before"] != r["after"]]
        facts.update(mutatingAdmissions=len(mutating), processStarts=sum(r["toolName"] in PROCESSES for r in rows),
                     taskDispatches=sum(r["toolName"] in DELEGATIONS for r in rows))
        if task["id"] == "classic-handoff":
            goal = self.service.sessions.agent_goal(session_id)
            audit = goal.get("completionAudit") or {}
            facts["goalCompletionReceipt"] = (goal.get("sessionId") == session_id and goal.get("configured") is True
                                              and goal.get("status") == "completed" and bool(audit.get("auditId")))
            facts["goalReceipt"] = {"goalId":goal.get("goalId"),"revision":goal.get("revision"),
                                    "status":goal.get("status"),"auditId":audit.get("auditId")}
            ordered = sorted(rows, key=lambda r:r["startedNs"])
            successful = [r for r in ordered if r["ok"]]
            reads = [r for r in successful if r["toolName"] in READS]
            writes = [r for r in successful if r["toolName"] in WRITES]
            output_reads = [r for r in reads if Path(str(r["args"].get("path", ""))).name == "result.txt"]
            input_reads = [r for r in reads if Path(str(r["args"].get("path", ""))).name == "input.txt"]
            facts["readWriteRead"] = bool(input_reads and writes and output_reads and
                input_reads[0]["endedNs"] <= writes[0]["startedNs"] and writes[0]["endedNs"] <= output_reads[-1]["startedNs"])
            facts["outputContentMutations"] = sum(r["before"].get("result.txt") != r["after"].get("result.txt") for r in rows)
            authorized = self.configs[session_id]["authorizedNs"]
            facts["authorizationBeforeMutation"] = bool(authorization and authorized and all(authorized < r["startedNs"] for r in mutating))
        try:
            answer = json.loads(final_text.strip())
        except (ValueError, TypeError):
            answer = {}
        fixture_rows = [r for r in rows if "fixtureIndex" in r]
        if task["id"] in {"ptc-parallel", "ptc-parallel-barrier-v2"}:
            ordered = sorted(fixture_rows, key=lambda r:r["fixtureIndex"])
            facts.update(nestedReadCalls=len(fixture_rows), nestedStarts=[r["fixtureStartedNs"] for r in ordered if "fixtureStartedNs" in r],
                         nestedEnds=[r["fixtureEndedNs"] for r in ordered if "fixtureEndedNs" in r], orderedValues=[r.get("fixtureValue") for r in ordered],
                         finalStructuredSum=answer.get("sum") if isinstance(answer,dict) and answer.get("values") == [3,5,8] else None)
            if task["id"] == "ptc-parallel-barrier-v2":
                facts["barrierArrivals"] = sorted(r["fixtureIndex"] for r in ordered if "barrierArrivalNs" in r)
                facts["barrierReleases"] = sum(r.get("barrierReleased") is True for r in ordered)
            nested = {e["payload"]["toolCallId"] for e in starts if e["payload"].get("parentToolCallId")}
            facts["complete"] = facts["complete"] and {r["toolCallId"] for r in fixture_rows} <= nested
        elif task["id"] == "ptc-error":
            good = [r for r in fixture_rows if r["ok"]]
            bad = [r for r in fixture_rows if not r["ok"]]
            facts.update(nestedSuccessCalls=len(good), nestedErrorCalls=len(bad), successValue=good[0].get("fixtureValue") if good else None,
                         errorCode=bad[0].get("fixtureErrorCode") if bad else None, retryCalls=max(0,len(fixture_rows)-2),
                         finalDistinguishesFailure=isinstance(answer,dict) and answer.get("value") == 7 and answer.get("error") == "FIXTURE_E_TRANSIENT")
        return facts

    def close(self):
        if self.installed:
            self.gateway.execute = self.original_execute
            self.gateway.workspace_harness.read = self.original_read
            self.installed = False
