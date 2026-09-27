#!/usr/bin/env python3
"""Bounded live JEV -> ordinary Pi -> verification canary in private temp state.

Uses the actual HTTP/Tool/SSE owners and an explicitly selected staged Runtime.
Never installs a Runtime or touches existing Room/Session rows. Network model
calls are real; run explicitly, not as part of ordinary unit discovery.
"""

from __future__ import annotations

import argparse
import ast
from collections.abc import Mapping
from dataclasses import asdict, replace
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import threading
import time
from urllib.parse import quote
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rag_ime.agent_service import AgentService
from rag_ime.debug_server import (
    DebugImeService,
    DebugRequestHandler,
    DebugServerConfig,
    QuietThreadingHTTPServer,
)
from rag_ime.managed_pi_runtime import snapshot_managed_pi_runtime_payload
from rag_ime.pi.config import PiRuntimeConfig
from rag_ime.jev_tasks.types import Task, digest
from scripts.pi_canary_support import stage_openai_codex_oauth


def _object(value):
    return value if isinstance(value, Mapping) else {}


def _check(passed, reason, evidence=None):
    return {"status": "unverified" if passed is None else "passed" if passed else "failed",
            "passed": passed, "reason": reason, "evidence": evidence or []}


def _timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


def _json_value(value):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _numeric_output(output, label, expected):
    """Read only a command's real output, never a submitted model summary."""
    for line in [output.strip(), *output.splitlines()]:
        try:
            value = ast.literal_eval(line.strip())
        except (ValueError, SyntaxError):
            value = None
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value == expected:
            return True
        if isinstance(value, dict) and any(
            str(key).casefold() in ({label.casefold(), "sum", "total", "result"}
                                    | ({"c=a+b"} if label == "C" else set()))
            and isinstance(number, (int, float)) and not isinstance(number, bool) and number == expected
            for key, number in value.items()
        ):
            return True
        pattern = rf"^\s*(?:{re.escape(label)}|sum|total|result|总和|结果)\s*[:=].*?(?<![\d.]){expected}(?:\.0+)?\s*$"
        if re.match(pattern, line, re.IGNORECASE):
            return True
        if label == "C" and re.fullmatch(r"\s*15\s*\+\s*12\s*=\s*27\s*", line):
            return True
    return False


def _fixture_values(output, expected):
    for raw in re.findall(r"\[[^\[\]\n]{1,100}\]", output):
        parsed = _json_value(raw)
        if parsed == expected and all(type(value) is int for value in parsed):
            return True
    return False


def _load_tool_results(root, bindings, tool_events):
    """Select JSONL toolResult entries; assistant/reasoning entries are skipped."""
    turns = {}
    for event in tool_events:
        key = (event["session_id"], event["tool_call_id"])
        turns.setdefault(key, set()).add(event["turn_id"])
    results, unavailable = [], []
    for binding in bindings:
        path = Path(binding["transcript_ref"])
        if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            unavailable.append({"sessionId": binding["session_id"], "reason": "private transcript unavailable"})
            continue
        with path.open() as stream:
            for line in stream:
                if not re.search(r'"role"\s*:\s*"toolResult"', line):
                    continue
                entry = _object(_json_value(line))
                message = _object(entry.get("message"))
                if message.get("role") != "toolResult":
                    continue
                call_id = str(message.get("toolCallId") or "")
                matched = turns.get((binding["session_id"], call_id), set())
                if len(matched) != 1:
                    continue
                details = _object(message.get("details"))
                for _ in range(3):
                    if not isinstance(details.get("result"), Mapping):
                        break
                    details = details["result"]
                name = str(details.get("toolName") or message.get("toolName") or "")
                if name not in {"read", "workspace_read", "bash", "workspace_shell"}:
                    continue  # result_submit/post are worker claims, not evidence.
                approval = _object(details.get("approval"))
                receipt = _object(details.get("receipt") or approval.get("receipt"))
                causal = _object(approval.get("causalMetadata"))
                turn_id = next(iter(matched))
                if causal.get("turnId") and causal["turnId"] != turn_id:
                    continue
                action = _object(_object(approval.get("preview")).get("actionPayload"))
                results.append({"sessionId": binding["session_id"], "turnId": turn_id,
                    "toolCallId": call_id, "entryId": str(entry.get("id") or ""), "tool": name,
                    "source": str(path.relative_to(root)), "timestampMs": _timestamp(message.get("timestamp") or entry.get("timestamp")),
                    "isError": message.get("isError") is True, "path": str(details.get("path") or ""),
                    "content": str(details.get("content") or ""), "truncated": details.get("truncated") is True,
                    "exitCode": receipt.get("exitCode"), "timedOut": receipt.get("timedOut") is True,
                    "outputLimited": receipt.get("outputLimited") is True, "output": str(receipt.get("output") or ""),
                    "command": str(action.get("command") or ""), "cwd": str(action.get("cwd") or receipt.get("cwd") or "")})
    return results, unavailable


def _load_request_metadata(root, accepted):
    """Only request model/effort/timing are retained; no prompts or reasoning."""
    expected = {(e["request"]["sessionId"], e["receipt"].get("turnId")) for e in accepted}
    result = []
    for path in sorted((root / "debug").rglob("*.json")):
        if not path.resolve().is_relative_to(root.resolve()):
            continue
        value = _object(_json_value(path.read_text()))
        identity = (value.get("sessionId"), value.get("turnId"))
        if identity not in expected:
            continue
        for request in value.get("providerRequests", []):
            request = _object(request)
            payload = _object(request.get("payload"))
            result.append({"sessionId": identity[0], "turnId": identity[1],
                "clientMessageId": value.get("clientMessageId"), "source": str(path.relative_to(root)),
                "index": request.get("index"), "capturedAtMs": request.get("capturedAtMs"),
                "provider": _object(value.get("model")).get("provider"), "model": payload.get("model"),
                "reasoningEffort": _object(payload.get("reasoning")).get("effort")})
    return result


def build_canary_evidence(private_root, projection):
    """Offline, read-only acceptance audit. Missing evidence never means pass."""
    root = Path(private_root)
    graph_id = projection.get("graphId")
    root_id = projection.get("rootId")
    effects = [effect for effect in projection.get("effects", []) if effect.get("operation") == "dispatch"]
    accepted = [effect for effect in effects if effect.get("state") == "accepted"]
    by_dispatch = {effect["effectId"]: effect for effect in accepted}
    unavailable = []
    conn = sqlite3.connect((root / "state.sqlite").resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    def rows(sql, parameters=()):
        try:
            return [dict(row) for row in conn.execute(sql, parameters)]
        except sqlite3.Error as exc:
            unavailable.append({"source": "state.sqlite", "reason": type(exc).__name__})
            return []

    try:
        graph = rows("SELECT root_work_id FROM agent_jev_graphs WHERE graph_id=?", (graph_id,))
        root_work = graph[0]["root_work_id"] if len(graph) == 1 else ""
        tasks = rows("SELECT id,state,revision,current_owner_participant_id,accepted_turn_id FROM agent_room_work_items WHERE root_turn_id=? AND id<>?", (root_id, root_work))
        specs = rows("SELECT task_id,specification_json FROM agent_jev_task_requirements WHERE graph_id=?", (graph_id,))
        work_events = rows("SELECT event_id,work_id,event_type,payload_json,created_at_ms FROM agent_room_work_events WHERE work_id IN (SELECT id FROM agent_room_work_items WHERE root_turn_id=?) ORDER BY sequence", (root_id,))
        bindings = rows("SELECT session_id,transcript_ref FROM agent_runtime_bindings WHERE session_id IN (SELECT json_extract(request_json,'$.sessionId') FROM agent_jev_runtime_effects WHERE graph_id=?)", (graph_id,))
        tool_events = rows("SELECT session_id,turn_id,json_extract(metrics_json,'$.toolIdentity.toolCallId') AS tool_call_id FROM agent_runtime_events WHERE event_type='tool_finished'")
        commands = rows("SELECT scope_id,client_message_id,state,updated_at_ms,json_extract(response_json,'$.turnId') AS turn_id FROM agent_command_receipts WHERE command_scope='session_prompt'")
        verifications = rows("SELECT * FROM agent_jev_verifications WHERE graph_id=?", (graph_id,))
        outputs = rows("SELECT dispatch_id,subject_hash,source_turn_id,payload_json FROM agent_jev_execution_outputs WHERE dispatch_id IN (SELECT effect_id FROM agent_jev_runtime_effects WHERE graph_id=?)", (graph_id,))
        settlements = rows("SELECT dispatch_id,result_json FROM agent_jev_aux_settlements WHERE dispatch_id IN (SELECT effect_id FROM agent_jev_runtime_effects WHERE graph_id=?)", (graph_id,))
        drains = rows("""SELECT dispatch_id,recorded_at_ms,
            json_extract(proof_json,'$.turnId') AS turn_id,json_extract(proof_json,'$.status') AS status,
            json_extract(proof_json,'$.effectsReconciled') AS reconciled,
            json_extract(proof_json,'$.settlement.sessionId') AS session_id,
            json_extract(proof_json,'$.settlement.receipt.runId') AS run_id,
            json_extract(proof_json,'$.settlement.receipt.pendingOperations') AS pending_operations,
            json_extract(proof_json,'$.settlement.receipt.continuations.counts.pending') AS pending_continuations,
            json_extract(proof_json,'$.settlement.receipt.continuations.counts.leased') AS leased_continuations,
            json_extract(proof_json,'$.settlement.receipt.settledAtMs') AS settled_at_ms,
            json_extract(proof_json,'$.settlement.receipt.finalMessage.model') AS actual_model,
            json_extract(proof_json,'$.settlement.receipt.finalMessage.provider') AS actual_provider,
            json_extract(proof_json,'$.descendantsProof.settled') AS descendants_settled
            FROM agent_jev_execution_drains WHERE dispatch_id IN (SELECT effect_id FROM agent_jev_runtime_effects WHERE graph_id=?)""", (graph_id,))
        claims = rows("SELECT COUNT(*) AS count FROM agent_jev_executor_claims WHERE graph_id=?", (graph_id,))
        public_events = rows("""SELECT event_id,event_type,created_at_ms,
            json_extract(payload_json,'$.post.postId') AS post_id,
            json_extract(payload_json,'$.post.visibility') AS visibility,
            json_extract(payload_json,'$.post.kind') AS kind,
            json_extract(payload_json,'$.post.publicationSource.ref') AS source_ref
            FROM agent_room_events WHERE turn_id=? AND event_type IN ('room_post','turn_completed','turn_failed')""", (root_id,))
    finally:
        conn.close()
    tools, missing_transcripts = _load_tool_results(root, bindings, tool_events)
    requests = _load_request_metadata(root, accepted)
    unavailable.extend(missing_transcripts)
    spec_by_task = {row["task_id"]: _object(_json_value(row["specification_json"])) for row in specs}
    labels = {}
    for task in tasks:
        label = str(spec_by_task.get(task["id"], {}).get("key", "")).upper()
        if label in {"A", "B", "C"}:
            labels.setdefault(label, []).append(task)
    mapped = {key: value[0] for key, value in labels.items() if len(value) == 1}
    checks = {"three_children_done": _check(bool(root_work and len(tasks) == 3 and len(mapped) == 3 and all(t["state"] == "done" for t in tasks)),
        "Require exactly A, B, C as three real completed child WorkItems.", tasks)}
    tool_proofs = []
    for label, expected in (("A", 15), ("B", 12), ("C", 27)):
        task = mapped.get(label)
        effect = by_dispatch.get(task["accepted_turn_id"]) if task else None
        matching = [tool for tool in tools if effect and tool["sessionId"] == effect["request"]["sessionId"]
                    and tool["turnId"] == effect["receipt"].get("turnId") and not tool["isError"]]
        commands_ok = [tool for tool in matching if tool["tool"] in {"bash", "workspace_shell"}
                       and tool["exitCode"] == 0 and not tool["timedOut"] and not tool["outputLimited"]]
        calculations = [tool for tool in commands_ok if _numeric_output(tool["output"], label, expected)]
        reads = []
        if label != "C":
            fixture = (root / "workspace" / (label.lower() + ".json")).resolve()
            numbers = [1, 2, 3, 4, 5] if label == "A" else [2, 4, 6]
            for tool in matching:
                if tool["path"] and Path(tool["path"]).resolve() == fixture and not tool["truncated"] and _fixture_values(tool["content"], numbers):
                    reads.append(tool)
            for tool in commands_ok:
                source_named = str(fixture) in tool["command"] or str(root / "workspace" / (label.lower() + ".json")) in tool["command"]
                if source_named and _fixture_values(tool["output"], numbers):
                    reads.append(tool)
        evidence = [{key: tool[key] for key in ("sessionId", "turnId", "toolCallId", "entryId", "source", "tool", "timestampMs", "output", "content")}
                    for tool in {tool["toolCallId"]: tool for tool in reads + calculations}.values()]
        tool_proofs.extend(evidence)
        checks["tool_result_" + label] = _check(True if calculations and (label == "C" or reads) else None,
            f"Require actual successful Tool output {label}={expected}" + (" and matching fixture read." if label != "C" else "; model submissions do not count."), evidence)

    drain_by_id = {row["dispatch_id"]: row for row in drains}
    request_by_turn = {}
    for request in requests:
        request_by_turn.setdefault((request["sessionId"], request["turnId"]), []).append(request)
    intervals = []
    for effect in accepted:
        req, receipt = effect["request"], effect["receipt"]
        calls = request_by_turn.get((req["sessionId"], receipt.get("turnId")), [])
        drain = drain_by_id.get(effect["effectId"], {})
        starts = [call["capturedAtMs"] for call in calls if isinstance(call.get("capturedAtMs"), int)]
        if req.get("purpose", "execute") == "execute" and starts and drain.get("settled_at_ms"):
            intervals.append({"dispatchId": effect["effectId"], "taskId": req["taskId"], "sessionId": req["sessionId"],
                              "startMs": min(starts), "endMs": drain["settled_at_ms"]})
    overlaps = [{"first": left, "second": right, "overlapMs": min(left["endMs"], right["endMs"]) - max(left["startMs"], right["startMs"])}
                for index, left in enumerate(intervals) for right in intervals[index + 1:]
                if left["sessionId"] != right["sessionId"] and left["taskId"] != right["taskId"]
                and min(left["endMs"], right["endMs"]) > max(left["startMs"], right["startMs"])]
    checks["workers_really_overlap"] = _check(bool(overlaps) if len(intervals) >= 2 else None,
        "Two distinct worker Sessions must have overlapping real request-to-settlement intervals.", overlaps or intervals)

    output_by_id = {row["dispatch_id"]: row for row in outputs}
    settlement_by_id = {row["dispatch_id"]: _object(_json_value(row["result_json"])) for row in settlements}
    verification_proofs = []
    for task in tasks:
        passed = []
        submissions = [_object(_object(_json_value(event["payload_json"])).get("work")) for event in work_events
                       if event["work_id"] == task["id"] and event["event_type"] == "submitted"]
        for verification in verifications:
            if verification["task_id"] != task["id"]:
                continue
            verifier = by_dispatch.get(verification["dispatch_id"])
            output = output_by_id.get(verification["dispatch_id"], {})
            verdict = _object(_json_value(verification["result_json"]))
            for submitted in submissions:
                try:
                    submitted_hash = digest(asdict(Task.from_payload(submitted)))
                except (TypeError, ValueError):
                    continue
                worker = by_dispatch.get(submitted.get("acceptedTurnId"))
                if (verifier and worker and submitted_hash == verification["task_hash"]
                    and submitted.get("revision") == task["revision"] and submitted.get("acceptedTurnId") == task["accepted_turn_id"]
                    and verifier["request"].get("purpose") == "verify" and verifier["request"].get("taskId") == task["id"]
                    and verifier["request"]["sessionId"] != worker["request"]["sessionId"]
                    and verifier["request"]["ownerId"] != worker["request"]["ownerId"]
                    and output.get("source_turn_id") == verifier["receipt"].get("turnId")
                    and output.get("subject_hash") == verifier["request"].get("subjectHash")
                    and _json_value(output.get("payload_json")) == verdict
                    and settlement_by_id.get(verification["dispatch_id"], {}).get("status") == "applied"
                    and verdict.get("operabilityVerdict") == "passed" and verdict.get("requirementVerdict") == "satisfied"
                    and verdict.get("evidenceRefs")):
                    passed.append({"taskId": task["id"], "workerDispatchId": worker["effectId"],
                        "verifierDispatchId": verifier["effectId"], "workerSessionId": worker["request"]["sessionId"],
                        "verifierSessionId": verifier["request"]["sessionId"], "taskHash": submitted_hash,
                        "operabilityVerdict": "passed", "requirementVerdict": "satisfied"})
        verification_proofs.extend(passed)
    checks["independent_bound_verdicts"] = _check(len({p["taskId"] for p in verification_proofs}) == 3 if len(tasks) == 3 else None,
        "Every accepted child must have an applied independent verdict bound to its exact submitted revision and Pi turn.", verification_proofs)

    accepted_commands = {(row["scope_id"], row["client_message_id"]): row for row in commands if row["state"] == "accepted"}
    completed = {}
    for event in work_events:
        work = _object(_object(_json_value(event["payload_json"])).get("work"))
        if event["event_type"] == "completed" and work.get("state") == "done":
            completed[event["work_id"]] = {"eventId": event["event_id"], "acceptedAtMs": event["created_at_ms"]}
    dependency_proofs = []
    if all(label in mapped for label in ("A", "B", "C")) and all(mapped[label]["id"] in completed for label in ("A", "B")):
        threshold = max(completed[mapped[label]["id"]]["acceptedAtMs"] for label in ("A", "B"))
        for effect in accepted:
            req = effect["request"]
            if req.get("purpose", "execute") != "execute" or req["taskId"] != mapped["C"]["id"]:
                continue
            command = accepted_commands.get((req["sessionId"], effect["effectId"]), {})
            calls = request_by_turn.get((req["sessionId"], effect["receipt"].get("turnId")), [])
            start = min((c["capturedAtMs"] for c in calls if isinstance(c.get("capturedAtMs"), int)), default=0)
            dependency_proofs.append({"dispatchId": effect["effectId"], "parentsAcceptedAtMs": threshold,
                "cAcceptedAtMs": command.get("updated_at_ms"), "firstRequestAtMs": start,
                "passed": bool(command.get("turn_id") == effect["receipt"].get("turnId") and command.get("updated_at_ms", 0) >= threshold and start >= threshold)})
    expected_edges = {(mapped[label]["id"], mapped["C"]["id"]) for label in ("A", "B")} if len(mapped) == 3 else set()
    actual_edges = {(edge.get("prerequisite"), edge.get("dependent")) for edge in projection.get("edges", []) if edge.get("kind", "requires") == "requires"}
    checks["c_admitted_after_parent_acceptance"] = _check(all(p["passed"] for p in dependency_proofs) and expected_edges <= actual_edges if dependency_proofs else None,
        "C needs both dependency edges and actual admission/request timestamps after A and B acceptance.", dependency_proofs)

    drain_proofs = []
    for effect in accepted:
        proof = drain_by_id.get(effect["effectId"], {})
        valid = (proof.get("status") == "drained" and proof.get("reconciled") == 1 and proof.get("descendants_settled") == 1
            and proof.get("session_id") == effect["request"]["sessionId"]
            and proof.get("turn_id") == effect["receipt"].get("turnId") == proof.get("run_id")
            and proof.get("pending_operations") == proof.get("pending_continuations") == proof.get("leased_continuations") == 0)
        drain_proofs.append({"dispatchId": effect["effectId"], "passed": valid, **proof})
    checks["all_accepted_dispatches_drained"] = _check(all(p["passed"] for p in drain_proofs) if accepted else None,
        "Every accepted dispatch requires an exact drained settlement and no pending operations/continuations/descendants.", drain_proofs)
    checks["zero_executor_claims"] = _check(claims[0]["count"] == 0 if claims else None, "The canonical executor claim table must be empty for this graph.", claims)
    final = _object(projection.get("final"))
    finals = [event for event in public_events if event["event_type"] == "room_post" and event["visibility"] == "room"
              and event["kind"] in {"result", "blocked"}]
    terminals = [event for event in public_events if event["event_type"] in {"turn_completed", "turn_failed"}]
    checks["single_public_final_and_root_terminal"] = _check(
        final.get("status") == "completed" and len(finals) == 1 and finals[0]["post_id"] == final.get("finalizationId")
        and finals[0]["source_ref"] == final.get("finalizationId")
        and len(terminals) == 1 and terminals[0]["event_type"] == "turn_completed",
        "Require one completed public final post and one root terminal event, with no duplicates.", finals + terminals)

    model_proofs = []
    for effect in accepted:
        req, receipt = effect["request"], effect["receipt"]
        selected = _object(_object(_object(req.get("contextManifest")).get("executionScope")).get("modelSelection"))
        difficulty = spec_by_task.get(req["taskId"], {}).get("difficulty", "routine")
        expected_model = "gpt-6-astra" if req.get("purpose") == "plan" or difficulty == "critical" else "gpt-6-sol" if difficulty == "complex" else "gpt-6-luna"
        calls = request_by_turn.get((req["sessionId"], receipt.get("turnId")), [])
        proof = drain_by_id.get(effect["effectId"], {})
        valid = (selected.get("modelId") == expected_model and selected.get("thinkingLevel") == "max"
                 and proof.get("actual_model") == expected_model and proof.get("actual_provider") == selected.get("provider")
                 and bool(calls) and all(call["model"] == expected_model and call["reasoningEffort"] == "max"
                     and call["provider"] == selected.get("provider") and call["clientMessageId"] == effect["effectId"] for call in calls))
        model_proofs.append({"dispatchId": effect["effectId"], "purpose": req.get("purpose", "execute"), "expectedModel": expected_model,
            "selectedModel": selected.get("modelId"), "actualModel": proof.get("actual_model"), "passed": valid if calls else None,
            "requests": calls})
    model_passed = False if any(p["passed"] is False for p in model_proofs) else None if not model_proofs or any(p["passed"] is None for p in model_proofs) else True
    checks["actual_gpt6_max_routing"] = _check(model_passed,
        "Compare prepared routing with actual request model/max effort and the settled response model; configuration alone is insufficient.", model_proofs)
    return {"checks": checks, "passed": all(check["passed"] is True for check in checks.values()),
            "evidenceVersion": "paw.jev-live-canary-evidence.v2", "unavailableEvidence": unavailable,
            "toolEvidence": tool_proofs, "evidenceBoundary": "Only Tool results, request model/effort metadata and canonical receipts are checked; no assistant reasoning, credentials or worker self-reports are used as completion proof."}


def _pending_plan_sample(private_root, view):
    """Observe the canonical hold, including the planner's real drain receipt."""
    sample = {
        "phase": view.get("phase"), "graphId": view.get("graphId"), "rootId": view.get("rootId"),
        "planHash": _object(view.get("planApproval")).get("planHash"),
        "requirementsRevision": _object(view.get("planApproval")).get("requirementsRevision"),
    }
    conn = sqlite3.connect((Path(private_root) / "state.sqlite").resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        graph = conn.execute("SELECT root_work_id FROM agent_jev_graphs WHERE graph_id=?", (view.get("graphId"),)).fetchone()
        sample["rootWorkId"] = graph[0] if graph else None
        sample["taskIds"] = [row[0] for row in conn.execute("SELECT id FROM agent_room_work_items WHERE root_turn_id=? ORDER BY id", (view.get("rootId"),))]
        sample["executionEffectCount"] = conn.execute("""SELECT COUNT(*) FROM agent_jev_runtime_effects
            WHERE graph_id=? AND operation='dispatch' AND COALESCE(json_extract(request_json,'$.purpose'),'execute')='execute'""", (view.get("graphId"),)).fetchone()[0]
        sample["planDrains"] = [dict(row) for row in conn.execute("""SELECT e.effect_id,
            json_extract(e.request_json,'$.sessionId') AS requested_session,
            json_extract(e.receipt_json,'$.turnId') AS accepted_turn,
            json_extract(d.proof_json,'$.status') AS status,
            json_extract(d.proof_json,'$.effectsReconciled') AS reconciled,
            json_extract(d.proof_json,'$.turnId') AS proof_turn,
            json_extract(d.proof_json,'$.settlement.sessionId') AS settled_session,
            json_extract(d.proof_json,'$.settlement.receipt.runId') AS settled_turn,
            json_extract(d.proof_json,'$.settlement.receipt.pendingOperations') AS pending_operations,
            json_extract(d.proof_json,'$.settlement.receipt.continuations.counts.pending') AS pending_continuations,
            json_extract(d.proof_json,'$.settlement.receipt.continuations.counts.leased') AS leased_continuations,
            json_extract(d.proof_json,'$.descendantsProof.settled') AS descendants_settled
            FROM agent_jev_runtime_effects e LEFT JOIN agent_jev_execution_drains d ON d.dispatch_id=e.effect_id
            WHERE e.graph_id=? AND e.operation='dispatch' AND e.state='accepted' AND json_extract(e.request_json,'$.purpose')='plan'""", (view.get("graphId"),))]
    except sqlite3.Error as exc:
        sample["unavailable"] = type(exc).__name__
    finally:
        conn.close()
    return sample


def exercise_canary_plan_approval(private_root, http, path, initial_view, tick, *, submit=True):
    """Approve only the explicitly requested synthetic fixture after proving hold.

    This is invoked only by --approve-plan. The same immutable request is sent
    twice to exercise HTTP idempotency, never to approve an arbitrary user plan.
    """
    graph_id, root_id = initial_view.get("graphId"), initial_view.get("rootId")
    approval = _object(initial_view.get("planApproval"))
    plan_hash = approval.get("planHash")
    samples = [_pending_plan_sample(private_root, initial_view)]
    for _ in range(2):
        tick()
        observed = http(path + "?graphId=" + quote(graph_id, safe=""))
        samples.append(_pending_plan_sample(private_root, observed))
    held = bool(plan_hash) and all(
        not sample.get("unavailable") and sample.get("phase") == "awaiting_approval"
        and sample.get("graphId") == graph_id and sample.get("rootId") == root_id
        and sample.get("planHash") == plan_hash and sample.get("requirementsRevision") == approval.get("requirementsRevision")
        and sample.get("rootWorkId") and sample.get("taskIds") == [sample["rootWorkId"]]
        and sample.get("executionEffectCount") == 0 for sample in samples)
    drained = all(sample.get("planDrains") and all(
        row.get("status") == "drained" and row.get("reconciled") == row.get("descendants_settled") == 1
        and row.get("accepted_turn") and row.get("accepted_turn") == row.get("proof_turn") == row.get("settled_turn")
        and row.get("requested_session") == row.get("settled_session")
        and row.get("pending_operations") == row.get("pending_continuations") == row.get("leased_continuations") == 0
        for row in sample["planDrains"]) for sample in samples)
    checks = {
        "approval_holds_workers_across_ticks": _check(bool(held), "Three canonical observations and two explicit ticks must retain only the Root and zero worker effects for the same pending plan.", samples),
        "approval_follows_planner_drain": _check(bool(drained), "The accepted planner must have an exact reconciled drain before approving the plan.", [row for sample in samples for row in sample.get("planDrains", [])]),
    }
    result = {"requested": True, "status": "blocked", "checks": checks, "responses": []}
    if not held or not drained:
        return result
    if not submit:
        result.update(status="awaiting_browser", planHash=plan_hash,
                      requirementsRevision=approval.get("requirementsRevision"))
        return result
    payload = {"action": "approve_plan", "graphId": graph_id, "rootId": root_id,
               "planHash": plan_hash, "clientMessageId": "live-plan-approve-1"}
    for _ in range(2):
        response = http(path, payload)
        result["responses"].append({key: response.get(key) for key in ("ok", "action", "graphId", "rootId", "replayed", "idempotentReplay")}
            | {"planApproval": {key: _object(response.get("planApproval")).get(key) for key in ("status", "planHash", "requirementsRevision")}})
    first, second = result["responses"]
    approved = all(response.get("ok") is True and response.get("action") == "approve_plan"
        and response.get("graphId") == graph_id and response.get("rootId") == root_id
        and response["planApproval"].get("status") == "approved" and response["planApproval"].get("planHash") == plan_hash
        and response["planApproval"].get("requirementsRevision") == approval.get("requirementsRevision") for response in result["responses"])
    idempotent = approved and first.get("replayed") is False and first.get("idempotentReplay") is False and second.get("replayed") is True and second.get("idempotentReplay") is True
    checks["plan_approval_http_idempotency"] = _check(bool(idempotent), "An exact hash-bound synthetic plan approval must apply once and replay the second identical HTTP request.", result["responses"])
    result["status"] = "approved" if idempotent else "blocked"
    return result


def observe_browser_plan_approval(http, path, view, pending):
    """Observe a real UI command and replay its exact identity, never click for it."""
    approval = _object(view.get("planApproval"))
    if approval.get("status") != "approved":
        return pending
    client_id = approval.get("lastActionClientMessageId")
    matches = bool(client_id) and approval.get("planHash") == pending.get("planHash") \
        and approval.get("requirementsRevision") == pending.get("requirementsRevision")
    response = http(path, {"action": "approve_plan", "graphId": view["graphId"],
        "rootId": view["rootId"], "planHash": approval.get("planHash"), "clientMessageId": client_id}) if matches else {}
    replayed = matches and response.get("ok") is True and response.get("idempotentReplay") is True
    checks = dict(pending["checks"])
    checks["plan_approval_http_idempotency"] = _check(bool(replayed),
        "The browser-approved immutable plan must replay its existing HTTP command without another application.",
        [{"clientMessageId": client_id, "replayed": response.get("idempotentReplay")}])
    return {**pending, "status": "approved" if replayed else "blocked", "checks": checks,
            "approvalSource": "browser", "responses": [response]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-payload", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--approve-plan", action="store_true", help="Exercise the pending-plan hold, then approve this synthetic fixture twice to check idempotency.")
    parser.add_argument("--browser-approval", action="store_true", help="Hold the synthetic plan for approval from the real frontend instead of submitting automatically.")
    parser.add_argument("--browser-create", action="store_true", help="Wait for the real frontend to send the synthetic request as well as approve its plan.")
    parser.add_argument("--linger", type=int, default=0, help="Keep the real HTTP service available briefly after writing the final report for browser verification.")
    args = parser.parse_args()
    args.browser_approval = args.browser_approval or args.browser_create
    args.approve_plan = args.approve_plan or args.browser_approval
    os.umask(0o077)
    installation = snapshot_managed_pi_runtime_payload(args.runtime_payload)
    discovered = PiRuntimeConfig.from_environment(enabled_default=True)
    run = Path(tempfile.mkdtemp(prefix="paw-jev-live-"))
    config_dir = run / "config"
    stage_openai_codex_oauth(discovered.agent_dir, config_dir)
    workspace = run / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text(
        "Synthetic canary. Only read these fixtures; do not contact anyone or change external state.\n"
    )
    (workspace / "a.json").write_text("[1,2,3,4,5]\n")
    (workspace / "b.json").write_text("[2,4,6]\n")

    class Handler(DebugRequestHandler):
        def log_message(self, *_args):
            pass

    server = QuietThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}"
    config = replace(
        discovered,
        enabled=True,
        executable=installation.executable,
        extension_path=installation.extension_path,
        node_executable=installation.node_executable,
        tools=installation.tools,
        pi_version=installation.pi_version,
        protocol_version="2",
        installation_error="",
        model_configuration_error="",
        model_configured=True,
        provider="openai-codex",
        model="gpt-6-sol",
        agent_dir=config_dir,
        session_dir=run / "sessions",
        logs_dir=run / "logs",
        debug_context_dir=run / "debug",
        tool_gateway_url=url + "/api/agent/tool/execute",
        idle_timeout_seconds=0,
        max_sessions=4,
        command_timeout_seconds=60,
    )
    agent = AgentService(
        db_path=run / "state.sqlite",
        runtime_config=config,
        startup_recovery_enabled=False,
        tool_gateway_url=url + "/api/agent/tool/execute",
        wake_scheduler_enabled=False,
    )
    app = DebugImeService(
        DebugServerConfig(
            db_path=run / "state.sqlite",
            agent_service=agent,
            seed_if_empty=False,
            memory_projection_worker_enabled=False,
            rime_user_dir=run / "Rime",
            rime_lexicon_backup_root=run / "RimeBackups",
        )
    )
    Handler.service = app
    Handler.static_dir = ROOT / "debug"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def http(path, payload=None):
        req = Request(
            url + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json"},
        )
        with urlopen(req, timeout=90) as response:
            return json.load(response)

    report = {
        "schemaVersion": "paw.jev-live-canary.v1",
        "privateRoot": str(run),
        "runtimeVersion": installation.runtime_version,
        "providerCalls": "live",
        "installed": False,
        "approvalCanary": {"requested": args.approve_plan, "status": "waiting" if args.approve_plan else "not_requested"},
    }
    created = None
    try:
        participants = []
        for index, role in enumerate(("coordinator", "implementer", "reviewer")):
            session = agent.sessions.create(
                title="Jev canary " + role,
                model_profile="openai-codex/gpt-6-sol",
                mode="coordinator",
                workspace_roots=[str(workspace)],
                project_context_enabled=False,
                execution_mode="full_trust",
                room_execution_mode="room_unrestricted",
            )
            participants.append(
                {
                    "sessionId": session["id"],
                    "roleId": role,
                    "roleVersion": "1",
                    "displayName": role,
                    "collaborationRole": role,
                }
            )
        room = agent.rooms.create(
            title="Jev synthetic live canary",
            routing_policy="moderator",
            participants=participants,
        )
        path = "/api/agent/rooms/" + quote(room["id"], safe="") + "/jev"
        message = (
            f"在授权的合成测试目录 {workspace} 完成三项最小责任：A 实际读取 a.json 计算总和；"
            "B 实际读取 b.json 计算总和；A、B 相互独立可并行。C 依赖 A 和 B 的已验收结果，将两个总和相加。"
            "使用这三项任务，不新建额外任务、不写文件、不调用任何外部业务接口。每项提供真实读取或计算证据，"
            "独立核验后汇总 A、B、C 数值。任务都是简单且有明确验收的合成计算。"
        )
        if args.approve_plan:
            message += (
                "严格工具验收：A、B 的执行 worker 各自必须实际读取对应 JSON 文件，随后执行只读计算命令，在 stdout 输出其总和；"
                "C 的执行 worker 也必须实际执行只读计算命令，在 stdout 输出 A+B 的数值。"
                "这三项工具证据必须来自各自执行 worker 的真实 Tool 结果，不能仅心算、写自述或由 verifier 的复算代替。"
            )
        if args.browser_create:
            print(json.dumps({"stage": "awaiting_browser_request", "root": str(run),
                              "apiUrl": url, "roomId": room["id"], "message": message}, ensure_ascii=False), flush=True)
            request_deadline = time.monotonic() + min(args.timeout, 600)
            while time.monotonic() < request_deadline:
                roots = http(path).get("items", [])
                if roots:
                    if len(roots) != 1 or roots[0].get("objective") != message:
                        raise RuntimeError("Browser must submit exactly the printed synthetic request once")
                    created = {"roomId": room["id"], "graphId": roots[0]["graph_id"],
                               "rootId": roots[0]["root_turn_id"]}
                    report["requestSource"] = "browser"
                    break
                time.sleep(0.5)
            if created is None:
                raise RuntimeError("Browser request was not received before the canary deadline")
        else:
            created = http(
                path,
                {
                    "action": "create",
                    "clientMessageId": "live-1",
                    "strategy": "plan",
                    "message": message,
                    "modelRouting": "balanced",
                    "toolApprovalMode": "dispatch",
                    "executionApproval": args.approve_plan,
                },
            )
        graph_id = created["graphId"]
        report["graphId"] = graph_id
        print(
            json.dumps({"stage": "created", "root": str(run), "graphId": graph_id,
                        "roomId": room["id"], "apiUrl": url}),
            flush=True,
        )
        deadline = time.monotonic() + args.timeout
        last = ""
        last_progress = time.monotonic()
        while time.monotonic() < deadline:
            agent.jev_application.tick(limit=3)
            view = http(path + "?graphId=" + quote(graph_id, safe=""))
            if args.approve_plan and report["approvalCanary"]["status"] == "waiting":
                if view.get("phase") == "awaiting_input":
                    report["approvalCanary"].update(status="awaiting_input", reason="The proposed plan requires user input; no approval was sent.")
                    break
                if view.get("phase") == "awaiting_approval":
                    report["approvalCanary"] = exercise_canary_plan_approval(run, http, path, view, lambda: agent.jev_application.tick(limit=3), submit=not args.browser_approval)
                    print(json.dumps({"stage": "plan_approval", "status": report["approvalCanary"]["status"]}), flush=True)
                    if report["approvalCanary"]["status"] not in {"approved", "awaiting_browser"}:
                        break
                    view = http(path + "?graphId=" + quote(graph_id, safe=""))
            if args.browser_approval and report["approvalCanary"]["status"] == "awaiting_browser":
                report["approvalCanary"] = observe_browser_plan_approval(http, path, view, report["approvalCanary"])
                if report["approvalCanary"]["status"] == "blocked":
                    break
            states = [(t["id"], t["state"], t["revision"]) for t in view["tasks"]]
            state = json.dumps(
                [
                    view["phase"],
                    states,
                    [
                        (e["request"].get("purpose", "execute"), e["state"])
                        for e in view["effects"]
                    ],
                ]
            )
            if state != last:
                last_progress = time.monotonic()
                print(
                    json.dumps(
                        {
                            "stage": view["phase"],
                            "tasks": [
                                (t["objective"][:30], t["state"]) for t in view["tasks"]
                            ],
                            "effects": len(view["effects"]),
                            "eventResults": [
                                {
                                    k: v
                                    for k, v in json.loads(e["result_json"]).items()
                                    if k in {"status", "errorType", "missing"}
                                }
                                for e in view["events"][:2]
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                last = state
            if view.get("final"):
                break
            all_drained = bool(view["effects"]) and all(
                effect.get("executionStatus") == "drained" for effect in view["effects"]
                if effect.get("operation") == "dispatch")
            if (all_drained and time.monotonic() - last_progress > 240
                    and report["approvalCanary"]["status"] != "awaiting_browser"):
                report["stalled"] = "All accepted execution turns drained without another task transition for 240 seconds."
                break
            if len(view["effects"]) > 20:
                raise RuntimeError("bounded live attempt budget exceeded")
            time.sleep(0.5)
        report["projection"] = view
        report["events"] = agent.rooms.list_events(room["id"], limit=2000)
        report.update(build_canary_evidence(run, view))
        if args.approve_plan:
            approval = report["approvalCanary"]
            report["checks"].update(approval.get("checks", {}))
            report["checks"]["required_plan_approval_exercised"] = _check(True if approval["status"] == "approved" else None,
                "The explicit approval canary must observe the hold and approve the current plan through its HTTP owner.", [approval["status"]])
            report["passed"] = all(check["passed"] is True for check in report["checks"].values())
        report["effectModels"] = [
            e["request"]
            .get("contextManifest", {})
            .get("executionScope", {})
            .get("modelSelection")
            for e in view["effects"]
        ]
        print(
            json.dumps(
                {
                    "stage": "finished",
                    "passed": report["passed"],
                    "output": str(args.output),
                }
            ),
            flush=True,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        if args.linger:
            time.sleep(max(0, min(args.linger, 600)))
    except Exception as exc:
        report["passed"] = False
        report["errorType"] = type(exc).__name__
        report["error"] = str(exc)[:500]
        print(
            json.dumps(
                {
                    "stage": "error",
                    "errorType": type(exc).__name__,
                    "error": str(exc)[:500],
                }
            ),
            flush=True,
        )
    finally:
        if created and not report.get("passed"):
            try:
                agent.jev_application.stop(created["roomId"], created["rootId"])
            except Exception:
                pass
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        app.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        (config_dir / "auth.json").unlink(missing_ok=True)
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
