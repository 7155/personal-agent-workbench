"""Offline audit fixtures; these tests never launch Pi or call a Provider.

The SQLite rows, transcript Tool results, and request metadata below are explicit
synthetic evidence. They test whether the report rejects incomplete evidence;
they do not establish that a real multi-Agent canary passed.
"""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from rag_ime.jev_tasks.types import Task, digest
from scripts.canary_jev_room import build_canary_evidence, exercise_canary_plan_approval, observe_browser_plan_approval, _numeric_output


class JevCanaryReportTests(unittest.TestCase):
    def test_actual_python_arithmetic_mapping_is_a_numeric_tool_output(self):
        self.assertTrue(_numeric_output("{'A': 15, 'B': 12, 'C=A+B': 27}\n", "C", 27))
        self.assertFalse(_numeric_output("{'A': 15, 'B': 12, 'C=A+B': 28}\n", "C", 27))
        self.assertFalse(_numeric_output("{'claim': 'C=A+B=27'}", "C", 27))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="jev-audit-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "workspace").mkdir()
        (self.root / "sessions").mkdir()
        (self.root / "debug").mkdir()
        self.conn = sqlite3.connect(self.root / "state.sqlite")
        self.addCleanup(self.conn.close)
        schemas = {
            "agent_jev_graphs": "graph_id TEXT, root_work_id TEXT",
            "agent_room_work_items": "id TEXT, state TEXT, revision INTEGER, current_owner_participant_id TEXT, accepted_turn_id TEXT, root_turn_id TEXT",
            "agent_jev_task_requirements": "task_id TEXT, graph_id TEXT, specification_json TEXT",
            "agent_room_work_events": "event_id TEXT, work_id TEXT, event_type TEXT, payload_json TEXT, created_at_ms INTEGER, sequence INTEGER",
            "agent_runtime_bindings": "session_id TEXT, transcript_ref TEXT",
            "agent_runtime_events": "session_id TEXT, turn_id TEXT, event_type TEXT, metrics_json TEXT",
            "agent_command_receipts": "command_scope TEXT, scope_id TEXT, client_message_id TEXT, state TEXT, updated_at_ms INTEGER, response_json TEXT",
            "agent_jev_verifications": "graph_id TEXT, task_id TEXT, task_hash TEXT, dispatch_id TEXT, result_json TEXT",
            "agent_jev_execution_outputs": "dispatch_id TEXT, subject_hash TEXT, source_turn_id TEXT, payload_json TEXT",
            "agent_jev_aux_settlements": "dispatch_id TEXT, result_json TEXT",
            "agent_jev_execution_drains": "dispatch_id TEXT, proof_json TEXT, recorded_at_ms INTEGER",
            "agent_jev_executor_claims": "graph_id TEXT",
            "agent_room_events": "event_id TEXT, event_type TEXT, created_at_ms INTEGER, payload_json TEXT, turn_id TEXT",
            "agent_jev_runtime_effects": "effect_id TEXT, graph_id TEXT, request_json TEXT, operation TEXT, state TEXT, receipt_json TEXT",
        }
        for table, schema in schemas.items():
            self.conn.execute(f"CREATE TABLE {table} ({schema})")
        self.projection = {
            "graphId": "graph", "rootId": "root", "effects": [],
            "final": {"status": "completed", "finalizationId": "final"},
            "edges": [{"prerequisite": label, "dependent": "C", "kind": "requires"} for label in ("A", "B")],
        }
        self.insert("agent_jev_graphs", "graph", "root-work")
        self.insert("agent_room_work_items", "root-work", "done", 0, "owner-0", "synthesize", "root")
        for session in ("session-0", "session-1", "session-2"):
            path = self.root / "sessions" / (session + ".jsonl")
            # The audit must skip non-Tool lines without parsing/using their body.
            path.write_text('{"message":{"role":"assistant","content":[{"type":"thinking","thinking":"DO NOT USE ME"}]}}\n')
            self.insert("agent_runtime_bindings", session, str(path))
        self.dispatch("plan", "plan", "root-work", 0, 1_000, 5_000)
        self.dispatch("worker-A", "execute", "A", 1, 10_000, 20_000)
        self.dispatch("worker-B", "execute", "B", 2, 12_000, 22_000)
        self.dispatch("verify-A", "verify", "A", 0, 24_000, 28_000)
        self.dispatch("verify-B", "verify", "B", 1, 26_000, 31_000)
        self.dispatch("worker-C", "execute", "C", 1, 40_000, 45_000)
        self.dispatch("verify-C", "verify", "C", 0, 47_000, 51_000)
        self.dispatch("synthesize", "synthesize", "root-work", 0, 60_000, 70_000)
        for label, owner, completed in (("A", 1, 30_000), ("B", 2, 32_000), ("C", 1, 52_000)):
            self.insert("agent_room_work_items", label, "done", 0, f"owner-{owner}", "worker-" + label, "root")
            self.insert("agent_jev_task_requirements", label, "graph", json.dumps({"key": label, "difficulty": "simple"}))
            submitted = {
                "id": label, "rootTurnId": "root", "roomId": "room", "state": "review", "revision": 0,
                "currentOwnerParticipantId": f"owner-{owner}", "accountableParticipantId": "owner-0",
                "assignmentKey": "assignment-" + label, "acceptedTurnId": "worker-" + label,
                "objective": "compute " + label, "expectedOutput": "sum", "acceptanceCriteria": ["correct sum"],
                "parentWorkId": "root-work", "artifactRefs": [], "evidenceRefs": ["tool-" + label], "resultSummary": "submitted",
            }
            self.insert("agent_room_work_events", "submitted-" + label, label, "submitted", json.dumps({"work": submitted}), completed - 8_000, completed - 1)
            self.insert("agent_room_work_events", "done-" + label, label, "completed", json.dumps({"work": {**submitted, "state": "done"}}), completed, completed)
            verdict = {"operabilityVerdict": "passed", "requirementVerdict": "satisfied", "reason": "verified", "evidenceRefs": ["tool-" + label]}
            self.insert("agent_jev_verifications", "graph", label, digest(asdict(Task.from_payload(submitted))), "verify-" + label, json.dumps(verdict))
            self.insert("agent_jev_execution_outputs", "verify-" + label, "subject-" + label, "turn-verify-" + label, json.dumps(verdict))
            self.insert("agent_jev_aux_settlements", "verify-" + label, json.dumps({"status": "applied"}))
            if label != "C":
                values = [1, 2, 3, 4, 5] if label == "A" else [2, 4, 6]
                fixture = self.root / "workspace" / (label.lower() + ".json")
                fixture.write_text(json.dumps(values))
                self.tool(label, owner, "read", {"toolName": "workspace_read", "path": str(fixture), "content": json.dumps(values), "truncated": False})
            result = {"A": 15, "B": 12, "C": 27}[label]
            self.tool(label, owner, "bash", {"toolName": "workspace_shell", "receipt": {"exitCode": 0, "output": str(result), "timedOut": False, "outputLimited": False}})
        self.insert("agent_room_events", "final-post", "room_post", 71_000, json.dumps({"post": {
            "postId": "final", "visibility": "room", "kind": "result", "publicationSource": {"kind": "runtime_projection", "ref": "final"},
        }}), "root")
        self.insert("agent_room_events", "root-terminal", "turn_completed", 72_000, "{}", "root")
        self.conn.commit()

    def insert(self, table, *values):
        self.conn.execute(f"INSERT INTO {table} VALUES({','.join('?' for _ in values)})", values)

    def dispatch(self, dispatch, purpose, task, owner, start, end):
        model = "gpt-6-luna" if purpose == "execute" else "gpt-6-sol"
        request = {
            "sessionId": f"session-{owner}", "taskId": task, "ownerId": f"owner-{owner}", "purpose": purpose,
            "contextManifest": {"executionScope": {"modelSelection": {"modelId": model, "provider": "openai-codex", "thinkingLevel": "max"}}},
        }
        if purpose == "verify":
            request["subjectHash"] = "subject-" + task
        effect = {"effectId": dispatch, "operation": "dispatch", "state": "accepted", "request": request, "receipt": {"turnId": "turn-" + dispatch}}
        self.projection["effects"].append(effect)
        self.insert("agent_jev_runtime_effects", dispatch, "graph", json.dumps(request), "dispatch", "accepted", json.dumps(effect["receipt"]))
        self.insert("agent_command_receipts", "session_prompt", request["sessionId"], dispatch, "accepted", start - 1, json.dumps(effect["receipt"]))
        proof = {
            "turnId": "turn-" + dispatch, "status": "drained", "effectsReconciled": True,
            "settlement": {"sessionId": request["sessionId"], "receipt": {
                "runId": "turn-" + dispatch, "pendingOperations": 0,
                "continuations": {"counts": {"pending": 0, "leased": 0}}, "settledAtMs": end,
                "finalMessage": {"model": model, "provider": "openai-codex"},
            }}, "descendantsProof": {"settled": True},
        }
        self.insert("agent_jev_execution_drains", dispatch, json.dumps(proof), end + 1)
        debug = {"sessionId": request["sessionId"], "turnId": "turn-" + dispatch, "clientMessageId": dispatch,
            "model": {"provider": "openai-codex"}, "providerRequests": [{"index": 1, "capturedAtMs": start,
            "payload": {"model": model, "reasoning": {"effort": "max"}}}]}
        (self.root / "debug" / (dispatch + ".json")).write_text(json.dumps(debug))

    def tool(self, label, owner, name, details):
        call = name + "-" + label
        message = {"role": "toolResult", "toolCallId": call, "toolName": name, "details": details, "isError": False, "timestamp": 18_000}
        with (self.root / "sessions" / f"session-{owner}.jsonl").open("a") as stream:
            stream.write(json.dumps({"id": "entry-" + call, "type": "message", "message": message}) + "\n")
        self.insert("agent_runtime_events", f"session-{owner}", "turn-worker-" + label, "tool_finished", json.dumps({"toolIdentity": {"toolCallId": call}}))

    def audit(self):
        self.conn.commit()
        return build_canary_evidence(self.root, self.projection)

    def mutate_json(self, table, field, identity, value, change):
        raw = self.conn.execute(f"SELECT {field} FROM {table} WHERE {identity}=?", (value,)).fetchone()[0]
        parsed = json.loads(raw)
        change(parsed)
        self.conn.execute(f"UPDATE {table} SET {field}=? WHERE {identity}=?", (json.dumps(parsed), value))

    def mutate_debug(self, dispatch, change):
        path = self.root / "debug" / (dispatch + ".json")
        value = json.loads(path.read_text())
        change(value)
        path.write_text(json.dumps(value))

    def test_complete_synthetic_evidence_passes_without_database_writes(self):
        before = (self.root / "state.sqlite").read_bytes()
        result = self.audit()
        self.assertTrue(result["passed"], result["checks"])
        self.assertEqual(result["unavailableEvidence"], [])
        self.assertEqual(before, (self.root / "state.sqlite").read_bytes())
        self.assertNotIn("DO NOT USE ME", json.dumps(result))

    def test_final_and_four_tasks_do_not_replace_tool_evidence(self):
        for path in (self.root / "sessions").glob("*.jsonl"):
            path.write_text("")
        self.tool("A", 1, "room_partner", {"toolName": "room_partner", "resultSummary": "A=15, B=12, C=27"})
        result = self.audit()
        self.assertFalse(result["passed"])
        self.assertEqual(result["checks"]["three_children_done"]["status"], "passed")
        self.assertEqual(result["checks"]["single_public_final_and_root_terminal"]["status"], "passed")
        for label in "ABC":
            self.assertEqual(result["checks"]["tool_result_" + label]["status"], "unverified")

    def test_failed_command_and_unrelated_turn_do_not_count_as_tools(self):
        path = self.root / "sessions" / "session-1.jsonl"
        entries = [json.loads(line) for line in path.read_text().splitlines()]
        for entry in entries:
            if entry.get("message", {}).get("toolCallId") == "bash-A":
                entry["message"]["details"]["receipt"]["exitCode"] = 1
        path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")
        self.conn.execute("UPDATE agent_runtime_events SET turn_id='old-turn' WHERE session_id='session-2'")
        result = self.audit()
        self.assertEqual(result["checks"]["tool_result_A"]["status"], "unverified")
        self.assertEqual(result["checks"]["tool_result_B"]["status"], "unverified")
        self.assertEqual(result["checks"]["tool_result_C"]["status"], "passed")

    def test_distinct_worker_sessions_without_overlap_fail(self):
        self.mutate_debug("worker-B", lambda v: v["providerRequests"][0].update(capturedAtMs=21_000))
        result = self.audit()
        self.assertEqual(result["checks"]["workers_really_overlap"]["status"], "failed")

    def test_self_verification_and_stale_subject_are_rejected(self):
        verifier = next(e for e in self.projection["effects"] if e["effectId"] == "verify-A")
        verifier["request"]["sessionId"] = "session-1"
        self.conn.execute("UPDATE agent_jev_verifications SET task_hash='stale' WHERE task_id='B'")
        self.conn.execute("UPDATE agent_jev_execution_outputs SET source_turn_id='wrong-turn' WHERE dispatch_id='verify-C'")
        result = self.audit()
        check = result["checks"]["independent_bound_verdicts"]
        self.assertEqual(check["status"], "failed")
        self.assertEqual(check["evidence"], [])

    def test_c_actual_admission_before_parent_acceptance_fails(self):
        self.conn.execute("UPDATE agent_command_receipts SET updated_at_ms=31_000 WHERE client_message_id='worker-C'")
        result = self.audit()
        self.assertEqual(result["checks"]["c_admitted_after_parent_acceptance"]["status"], "failed")

    def test_missing_dependency_edge_fails_even_when_timestamps_are_later(self):
        self.projection["edges"].pop()
        self.assertEqual(self.audit()["checks"]["c_admitted_after_parent_acceptance"]["status"], "failed")

    def test_each_accepted_dispatch_must_drain_and_release_claims(self):
        self.mutate_json("agent_jev_execution_drains", "proof_json", "dispatch_id", "worker-A", lambda p: p["settlement"]["receipt"].update(pendingOperations=1))
        self.conn.execute("DELETE FROM agent_jev_execution_drains WHERE dispatch_id='verify-C'")
        self.insert("agent_jev_executor_claims", "graph")
        result = self.audit()
        self.assertEqual(result["checks"]["all_accepted_dispatches_drained"]["status"], "failed")
        self.assertEqual(result["checks"]["zero_executor_claims"]["status"], "failed")

    def test_duplicate_final_or_terminal_fails(self):
        self.insert("agent_room_events", "duplicate-post", "room_post", 73_000, json.dumps({"post": {
            "postId": "other-final", "visibility": "room", "kind": "result", "publicationSource": {"ref": "other-final"},
        }}), "root")
        self.assertEqual(self.audit()["checks"]["single_public_final_and_root_terminal"]["status"], "failed")
        self.conn.execute("DELETE FROM agent_room_events WHERE event_id='duplicate-post'")
        self.insert("agent_room_events", "duplicate-terminal", "turn_failed", 73_000, "{}", "root")
        self.assertEqual(self.audit()["checks"]["single_public_final_and_root_terminal"]["status"], "failed")

    def test_prepared_gpt6_max_cannot_replace_actual_request_model_or_effort(self):
        for field, wrong in (("model", "gpt-5.6-luna"), ("effort", "high")):
            with self.subTest(field=field):
                def wrong_request(value, field=field, wrong=wrong):
                    payload = value["providerRequests"][0]["payload"]
                    payload["model"] = wrong if field == "model" else "gpt-6-luna"
                    payload["reasoning"]["effort"] = wrong if field == "effort" else "max"
                self.mutate_debug("worker-A", wrong_request)
                self.assertEqual(self.audit()["checks"]["actual_gpt6_max_routing"]["status"], "failed")

    def test_legacy_astra_planning_is_rejected_even_with_matching_receipts(self):
        effect = next(e for e in self.projection["effects"] if e["effectId"] == "plan")
        effect["request"]["contextManifest"]["executionScope"]["modelSelection"]["modelId"] = "gpt-6-astra"
        self.mutate_debug("plan", lambda v: v["providerRequests"][0]["payload"].update(model="gpt-6-astra"))
        self.mutate_json("agent_jev_execution_drains", "proof_json", "dispatch_id", "plan",
            lambda p: p["settlement"]["receipt"]["finalMessage"].update(model="gpt-6-astra"))
        self.assertEqual(self.audit()["checks"]["actual_gpt6_max_routing"]["status"], "failed")

    def test_routine_execution_does_not_silently_use_luna(self):
        self.conn.execute("UPDATE agent_jev_task_requirements SET specification_json=? WHERE task_id='A'",
                          (json.dumps({"key": "A", "difficulty": "routine"}),))
        self.assertEqual(self.audit()["checks"]["actual_gpt6_max_routing"]["status"], "failed")

    def test_missing_actual_request_is_unverified(self):
        (self.root / "debug" / "worker-A.json").unlink()
        self.assertEqual(self.audit()["checks"]["actual_gpt6_max_routing"]["status"], "unverified")

    def pending_plan(self):
        self.conn.execute("DELETE FROM agent_room_work_items WHERE id<>'root-work'")
        self.conn.execute("DELETE FROM agent_jev_runtime_effects WHERE effect_id<>'plan'")
        self.conn.commit()
        return {
            "graphId": "graph", "rootId": "root", "phase": "awaiting_approval",
            "planApproval": {"status": "ready", "planHash": "plan-hash", "requirementsRevision": 1},
        }

    def approval_transport(self, view, *, wrong_replay=False):
        """Only the HTTP transport is fake; canonical hold/drain reads use SQLite."""
        operations = []

        def http(path, payload=None):
            operations.append(("get" if payload is None else "post", path, payload))
            if payload is None:
                return view
            count = sum(operation[0] == "post" for operation in operations)
            replay = count == 2 and not wrong_replay
            return {"ok": True, "action": "approve_plan", "graphId": "graph", "rootId": "root",
                "replayed": replay, "idempotentReplay": replay,
                "planApproval": {"status": "approved", "planHash": "plan-hash", "requirementsRevision": 1}}

        def tick():
            operations.append(("tick", None, None))

        return http, tick, operations

    def test_approval_observes_hold_before_two_identical_requests(self):
        view = self.pending_plan()
        http, tick, operations = self.approval_transport(view)
        result = exercise_canary_plan_approval(self.root, http, "/jev", view, tick)
        self.assertEqual(result["status"], "approved", result)
        self.assertTrue(all(check["passed"] is True for check in result["checks"].values()))
        self.assertEqual([operation[0] for operation in operations], ["tick", "get", "tick", "get", "post", "post"])
        self.assertEqual(operations[-1][2], operations[-2][2])
        self.assertEqual(operations[-1][2]["planHash"], "plan-hash")
        samples = result["checks"]["approval_holds_workers_across_ticks"]["evidence"]
        self.assertEqual(len(samples), 3)
        self.assertTrue(all(sample["taskIds"] == ["root-work"] and sample["executionEffectCount"] == 0 for sample in samples))

    def test_browser_approval_waits_without_submitting_then_replays_the_ui_identity(self):
        view = self.pending_plan()
        http, tick, operations = self.approval_transport(view)
        pending = exercise_canary_plan_approval(self.root, http, "/jev", view, tick, submit=False)
        self.assertEqual(pending["status"], "awaiting_browser")
        self.assertFalse(any(operation[0] == "post" for operation in operations))
        self.assertIs(observe_browser_plan_approval(http, "/jev", view, pending), pending)
        view["planApproval"].update(status="approved", lastActionClientMessageId="ui-exact-approval")
        calls = []
        def replay(path, payload):
            calls.append(payload)
            return {"ok": True, "idempotentReplay": True}
        result = observe_browser_plan_approval(replay, "/jev", view, pending)
        self.assertEqual(result["status"], "approved")
        self.assertEqual(calls[0]["clientMessageId"], "ui-exact-approval")
        self.assertEqual(calls[0]["planHash"], "plan-hash")

    def test_browser_approval_rejects_changed_plan_without_posting(self):
        view = self.pending_plan()
        http, tick, operations = self.approval_transport(view)
        pending = exercise_canary_plan_approval(self.root, http, "/jev", view, tick, submit=False)
        view["planApproval"].update(status="approved", planHash="new-plan", lastActionClientMessageId="ui-new")
        result = observe_browser_plan_approval(http, "/jev", view, pending)
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(any(operation[0] == "post" for operation in operations))

    def test_approval_does_not_hide_existing_worker_effects(self):
        view = self.pending_plan()
        self.insert("agent_jev_runtime_effects", "early-worker", "graph", json.dumps({"purpose": "execute"}), "dispatch", "prepared", "{}")
        self.conn.commit()
        http, tick, operations = self.approval_transport(view)
        result = exercise_canary_plan_approval(self.root, http, "/jev", view, tick)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["checks"]["approval_holds_workers_across_ticks"]["status"], "failed")
        self.assertFalse(any(operation[0] == "post" for operation in operations))

    def test_approval_requires_actual_planner_drain(self):
        view = self.pending_plan()
        self.conn.execute("DELETE FROM agent_jev_execution_drains WHERE dispatch_id='plan'")
        self.conn.commit()
        http, tick, operations = self.approval_transport(view)
        result = exercise_canary_plan_approval(self.root, http, "/jev", view, tick)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["checks"]["approval_follows_planner_drain"]["status"], "failed")
        self.assertFalse(any(operation[0] == "post" for operation in operations))

    def test_approval_does_not_approve_a_changed_plan(self):
        initial = self.pending_plan()
        observed = {**initial, "planApproval": {**initial["planApproval"], "planHash": "different-plan"}}
        http, tick, operations = self.approval_transport(observed)
        result = exercise_canary_plan_approval(self.root, http, "/jev", initial, tick)
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(any(operation[0] == "post" for operation in operations))

    def test_approval_second_response_must_be_idempotent(self):
        view = self.pending_plan()
        http, tick, operations = self.approval_transport(view, wrong_replay=True)
        result = exercise_canary_plan_approval(self.root, http, "/jev", view, tick)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["checks"]["plan_approval_http_idempotency"]["status"], "failed")
        self.assertEqual(sum(operation[0] == "post" for operation in operations), 2)


if __name__ == "__main__":
    unittest.main()
