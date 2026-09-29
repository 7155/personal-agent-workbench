from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from rag_ime.agent_approval_model import ApprovalModelArbiter, requires_jev_approval
from rag_ime.jev_tasks.policy import model_cards, model_role_guidance, normalize_policy, select_model, tool_approval_mode
from rag_ime.jev_tasks.ledger import GraphLedger
from rag_ime.jev_tasks.types import GraphConflict, GraphError
from tests import test_agent_approval_model as approval_fixtures
from tests import test_agent_approval_application as application_fixtures


def catalog(provider="configured-provider"):
    return [{"provider": provider, "id": "gpt-6-" + name, "thinkingLevels": ["high", "max"]}
            for name in ("astra", "sol", "luna")]


class JevModelPolicyTests(unittest.TestCase):
    def test_role_cards_separate_sources_and_use_current_family(self):
        cards = model_cards()
        self.assertEqual({card["modelId"] for card in cards}, {"gpt-6-astra", "gpt-6-sol", "gpt-6-luna"})
        for card in cards:
            self.assertEqual({e["sourceKind"] for e in card["evidence"]}, {"official", "community", "user_policy"})
            self.assertTrue(card["limitations"])
            self.assertEqual(card["defaultThinkingLevel"], "max")
            self.assertTrue(next(e["url"] for e in card["evidence"] if e["sourceKind"] == "community").startswith("https://www.reddit.com/"))
        cards[0]["strengths"].clear()
        self.assertTrue(model_cards()[0]["strengths"])
        self.assertIn("简单任务", model_role_guidance("gpt-6-luna", "execute"))
        self.assertIn("不授予新权限", model_role_guidance("gpt-6-sol", "verify"))
        self.assertIn("优先使用 Sol max", next(e for e in cards[0]["evidence"] if e["sourceKind"] == "user_policy")["summary"])
        self.assertEqual(model_role_guidance("other", "execute"), "")

    def test_default_and_old_root_policy_are_distinct(self):
        self.assertEqual(normalize_policy({}), {"modelRouting": "balanced", "toolApprovalMode": "dispatch", "verificationMode": "auto"})
        self.assertEqual(normalize_policy({}, legacy=True)["modelRouting"], "participant")
        self.assertEqual(normalize_policy({}, stored=True)["verificationMode"], "independent")
        self.assertEqual(normalize_policy({"modelRouting": "balanced"}, stored=True)["verificationMode"], "independent")
        self.assertEqual(normalize_policy({"verificationMode": "auto"}, stored=True)["verificationMode"], "auto")
        self.assertEqual(normalize_policy({"verificationMode": "independent"})["verificationMode"], "independent")
        self.assertEqual(normalize_policy({"toolApprovalMode": "jev_dangerous"})["toolApprovalMode"], "jev_dangerous")
        for payload in ({"modelRouting": "cheap"}, {"toolApprovalMode": True}, {"toolApprovalMode": "skip_scope"}, {"verificationMode": "skip"}, {"verificationMode": True}):
            with self.assertRaises(GraphError):
                normalize_policy(payload)

    def test_exact_model_tiers_and_planning(self):
        self.assertEqual(select_model(catalog(), purpose="execute")["modelId"], "gpt-6-sol")
        for purpose, difficulty, model in (
            ("plan", "simple", "sol"), ("plan", "routine", "sol"),
            ("plan", "complex", "sol"), ("plan", "critical", "sol"), ("execute", "simple", "luna"),
            ("execute", "routine", "sol"), ("execute", "complex", "sol"), ("execute", "critical", "sol"),
            ("verify", "simple", "sol"), ("verify", "routine", "sol"),
            ("verify", "complex", "sol"), ("verify", "critical", "sol"),
            ("synthesize", "simple", "luna"), ("synthesize", "routine", "sol"),
        ):
            with self.subTest(purpose=purpose, difficulty=difficulty):
                chosen = select_model(catalog(), purpose=purpose, difficulty=difficulty)
                self.assertEqual(chosen["modelProfile"], "configured-provider/gpt-6-" + model)
                self.assertEqual(chosen["thinkingLevel"], "max")

    def test_no_alias_provider_or_reasoning_substitution(self):
        with self.assertRaisesRegex(GraphConflict, "MODEL_UNAVAILABLE"):
            select_model([{"provider": "configured-provider", "id": "gpt-5.6-sol", "thinkingLevels": ["max"]}], purpose="execute")
        with self.assertRaisesRegex(GraphConflict, "MODEL_UNAVAILABLE"):
            select_model(catalog(), purpose="execute", preferred_provider="different-provider")
        with self.assertRaisesRegex(GraphConflict, "PROVIDER_AMBIGUOUS"):
            select_model(catalog("a") + catalog("b"), purpose="execute")
        with self.assertRaisesRegex(GraphConflict, "REASONING_UNAVAILABLE"):
            select_model([{"provider": "a", "id": "gpt-6-luna", "thinkingLevels": ["high"]}], purpose="execute", difficulty="simple")
        with self.assertRaisesRegex(GraphConflict, "MODEL_UNAVAILABLE"):
            select_model([{**catalog()[2], "available": False}], purpose="execute", difficulty="simple")
        with self.assertRaisesRegex(GraphConflict, "API_UNSUPPORTED"):
            select_model([{**catalog()[2], "api": "openai-completions"}], purpose="execute", difficulty="simple")

    def test_planning_does_not_upgrade_to_astra_when_sol_is_missing(self):
        with self.assertRaisesRegex(GraphConflict, "MODEL_UNAVAILABLE"):
            select_model([row for row in catalog() if row["id"] != "gpt-6-sol"], purpose="plan")
        selected = select_model(catalog(), purpose="plan", difficulty="routine",
                                locked_profile="configured-provider/gpt-6-astra")
        self.assertEqual(selected["modelId"], "gpt-6-astra")
        self.assertEqual(selected["reason"], "user_model_lock")

    def test_explicit_lock_and_provider_are_preserved(self):
        selected = select_model(catalog("a") + catalog("b"), purpose="execute", preferred_provider="b")
        self.assertEqual(selected["provider"], "b")
        selected = select_model(catalog(), purpose="execute", difficulty="critical",
                                locked_profile="configured-provider/gpt-6-sol")
        self.assertEqual(selected["modelId"], "gpt-6-sol")
        self.assertEqual(selected["reason"], "user_model_lock")


class JevApprovalBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="jev-policy-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "binding.sqlite"
        self.request = {"sessionId": "session", "rootId": "root", "roomId": "room", "dispatchId": "dispatch", "rootEpoch": 1}
        self.causal = {"roomId": "room", "rootId": "root", "dispatchId": "dispatch", "turnId": "pi-turn"}
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.executescript("""
                CREATE TABLE agent_jev_graphs(graph_id TEXT, room_id TEXT, root_turn_id TEXT);
                CREATE TABLE agent_jev_host_roots(graph_id TEXT, policy_json TEXT, stopped INTEGER, epoch INTEGER);
                CREATE TABLE agent_jev_runtime_effects(graph_id TEXT,effect_id TEXT,operation TEXT,state TEXT,request_json TEXT,receipt_json TEXT);
                INSERT INTO agent_jev_graphs VALUES('graph','room','root');
            """)
            conn.execute("INSERT INTO agent_jev_host_roots VALUES('graph',?,0,1)", (json.dumps({"toolApprovalMode": "jev_dangerous"}),))
            conn.execute("INSERT INTO agent_jev_runtime_effects VALUES('graph','dispatch','dispatch','accepted',?,?)",
                         (json.dumps(self.request), json.dumps({"turnId": "pi-turn"})))

    def mode(self, **kwargs):
        return tool_approval_mode(self.path, session_id="session", causal=self.causal,
                                  bound_turn_id=kwargs.get("bound_turn_id", "pi-turn"))

    def test_only_exact_live_root_dispatch_and_pi_turn_uses_optional_mode(self):
        self.assertEqual(self.mode(), "jev_dangerous")
        self.assertIsNone(tool_approval_mode(self.path, session_id="session", causal={**self.causal, "rootId": "ordinary"}, bound_turn_id="pi-turn"))
        for field, value in (("dispatchId", "stale"), ("turnId", "other")):
            with self.subTest(field=field), self.assertRaises(GraphConflict):
                tool_approval_mode(self.path, session_id="session", causal={**self.causal, field: value}, bound_turn_id="pi-turn")
        with self.assertRaises(GraphConflict):
            self.mode(bound_turn_id="")
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE agent_jev_host_roots SET stopped=1")
        with self.assertRaises(GraphConflict):
            self.mode()

    def test_sending_requires_original_owner_pi_turn_and_unknown_is_not_permission(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE agent_jev_runtime_effects SET state='sending',receipt_json='{}'")
        self.assertEqual(self.mode(), "jev_dangerous")
        with self.assertRaises(GraphConflict):
            self.mode(bound_turn_id="")
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE agent_jev_runtime_effects SET state='unknown'")
        with self.assertRaises(GraphConflict):
            self.mode()

    def test_default_dispatch_keeps_existing_owner_boundary(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE agent_jev_host_roots SET policy_json='{}'")
        self.assertEqual(self.mode(bound_turn_id=""), "dispatch")


class JevOnlyArbiterTests(unittest.TestCase):
    setUp = approval_fixtures.ApprovalModelArbiterTests.setUp
    tearDown = approval_fixtures.ApprovalModelArbiterTests.tearDown
    approval = approval_fixtures.ApprovalModelArbiterTests.approval

    def arbiter(self):
        self.runtime = approval_fixtures.FakeCompletionRuntime({"text": "must not run"})
        return ApprovalModelArbiter(self.db_path, runtime_provider=lambda: self.runtime)

    def test_unconfigured_jev_does_not_silently_fall_back(self):
        receipt = self.arbiter().decide(self.approval(), self.session, jev_only=True)
        self.assertEqual(receipt["status"], "failed_closed")
        self.assertEqual(receipt["decision"], "deny")
        self.assertEqual(receipt["modelProvider"], "typesafe")
        self.assertEqual(receipt["failureCode"], "JEV_NOT_CONFIGURED")
        self.assertEqual(self.runtime.requests, [])

    def test_jev_transport_failure_remains_jev_failed_closed(self):
        arbiter = self.arbiter()
        with patch("rag_ime.agent_approval_model._jev_api_key", return_value="test"), patch(
            "rag_ime.agent_approval_model._jev_decide", side_effect=TimeoutError("timeout")
        ):
            receipt = arbiter.decide(self.approval(), self.session, jev_only=True)
        self.assertEqual(receipt["status"], "failed_closed")
        self.assertEqual(receipt["modelProvider"], "typesafe")
        self.assertEqual(receipt["failureCode"], "MODEL_TIMEOUT")
        self.assertEqual(self.runtime.requests, [])

    def test_jev_finite_approval_uses_existing_hash_bound_receipt(self):
        arbiter = self.arbiter()
        approval = self.approval()
        with patch("rag_ime.agent_approval_model._jev_api_key", return_value="test"), patch(
            "rag_ime.agent_approval_model._jev_decide", return_value=("approve", ["authorized_scope"], "Jev approved")
        ) as decide:
            receipt = arbiter.decide(approval, self.session, jev_only=True)
            self.assertEqual(arbiter.decide(approval, self.session, jev_only=True), receipt)
            decide.assert_called_once()
        self.assertEqual(receipt["payloadSha256"], approval["payloadSha256"])
        self.assertEqual(receipt["decision"], "approve")
        self.assertEqual(self.runtime.requests, [])

    def test_danger_classification_uses_prepared_operation(self):
        self.assertTrue(requires_jev_approval({"toolId": "configuration", "operation": "restore_apply", "riskLevel": "R3"}, self.session))
        ordinary = {"toolId": "workspace_write", "operation": "apply", "riskLevel": "R2",
                    "preview": {"actionPayload": {"path": "/project/README.md", "content": "Example: rm -rf cache"}}}
        self.assertFalse(requires_jev_approval(ordinary, self.session))
        ordinary["preview"]["actionPayload"]["path"] = "/project/.env"
        self.assertTrue(requires_jev_approval(ordinary, self.session))
        self.assertFalse(requires_jev_approval({"toolId": "planning", "operation": "task_action", "riskLevel": "R1"}, self.session))

    def test_scope_evidence_does_not_compare_filesystem_identity_to_authorized_paths(self):
        arbiter = self.arbiter()
        approval = self.approval(preview={"baseState": {
            "workspaceScopeSha256": self.session["workspaceScopeSha256"], "workspaceRootsSha256": "b" * 64,
        }})
        with patch("rag_ime.agent_approval_model._jev_api_key", return_value="test"), patch(
            "rag_ime.agent_approval_model._jev_decide", return_value=("deny", ["policy_boundary"], "Denied")
        ) as judge:
            arbiter.decide(approval, self.session, jev_only=True)
        evidence = judge.call_args.args[0]
        self.assertNotIn("cross_workspace", evidence["authority"]["riskSignals"])


class JevApprovalApplicationTests(unittest.TestCase):
    tearDown = application_fixtures.ApprovalApplicationTests.tearDown

    def setUp(self):
        application_fixtures.ApprovalApplicationTests.setUp(self)
        self.sid = self.worker["id"]
        self.sessions.set_runtime_policy(self.sid, mode="coordinator", tool_profile_version="control-center-full-access-v1",
                                         execution_mode="per_action", workspace_roots=["/"], allowed_tools=None)
        self.sessions.bind_runtime_session(self.sid, driver_id="pi", runtime_kind="pi", external_session_id="pi-session")
        self.sessions.record_runtime_event(event_id="jev-tool-start", session_id=self.sid, turn_id="pi-turn",
                                          sequence=1, event_type="tool_started", created_at_ms=1)
        root = self.work.create(room_id=self.room["id"], objective="Test exact prepared action", expected_output="Tool receipt",
                                current_owner_participant_id=self.worker_participant["id"],
                                created_by_participant_id=self.worker_participant["id"],
                                accountable_participant_id=self.worker_participant["id"],
                                client_message_id="jev-policy-test", root_turn_id="jev-root",
                                acceptance_criteria=["Exact authorized tool effect only"])
        self.ledger = GraphLedger(self.db_path)
        self.ledger.register_created_root(graph_id="jev-graph", room_id=self.room["id"], root_id="jev-root", root_work_id=root["id"],
                                          controller_id="controller", participant_id=self.worker_participant["id"], session_id=self.sid)
        request = {"sessionId": self.sid, "roomId": self.room["id"], "rootId": "jev-root", "dispatchId": "jev-dispatch", "rootEpoch": 1}
        with self.ledger.connection(write=True) as conn:
            conn.execute("INSERT INTO agent_jev_host_roots(graph_id,request_hash,policy_json) VALUES('jev-graph','hash',?)",
                         (json.dumps({"toolApprovalMode": "jev_dangerous"}),))
            conn.execute("INSERT INTO agent_jev_commands VALUES('command','jev-graph','hash','dispatch',?,'{}',1)", (root["id"],))
            conn.execute("INSERT INTO agent_jev_runtime_effects VALUES('jev-dispatch','jev-graph','command','dispatch',?,'accepted',?,1)",
                         (json.dumps(request), json.dumps({"turnId": "pi-turn"})))
        self.app.room_turns.begin(self.sid, "jev-root", dispatch_id="jev-dispatch")
        self.app.room_turns.accept(self.sid, "pi-turn", "jev-root")
        self.live = {"roomId": self.room["id"], "rootId": "jev-root", "dispatchId": "jev-dispatch", "generation": 1}
        self.app._active_room_dispatch_context = lambda _: self.live if self.app.room_turns.active_turn(self.sid)[0] else None
        self.executor = Mock(return_value={"mutationApplied": True, "summary": "Prepared action applied"})
        self.app._executor_provider = lambda: self.executor

    def approval(self, risk="R3"):
        with self.sessions.approval_creation_scope(session_id=self.sid, tool_call_id="tool-call", room_context=self.live):
            return self.sessions.create_approval(session_id=self.sid, tool_name="planning", operation="task_action",
                payload_sha256="a" * 64, preview={"actionPayload": {"taskId": "task"}}, risk_level=risk, ttl_ms=120000)

    def test_optional_jev_approval_reaches_original_execution_owner(self):
        with patch("rag_ime.agent_approval_model._jev_api_key", return_value="test"), patch(
            "rag_ime.agent_approval_model._jev_decide", return_value=("approve", ["authorized_scope"], "Jev approved")
        ) as judge:
            result = self.app.auto_approve_pending(self.approval())
        judge.assert_called_once()
        self.executor.assert_called_once()
        self.assertTrue(result["autoApproved"])
        self.assertEqual(result["approval"]["state"], "applied")
        self.assertEqual(result["approvalModelDecision"]["modelProvider"], "typesafe")

    def test_default_and_ordinary_operations_skip_jev(self):
        with patch.object(self.app.approval_model, "decide") as judge:
            ordinary = self.app.auto_approve_pending(self.approval("R1"))
            with self.ledger.connection(write=True) as conn:
                conn.execute("UPDATE agent_jev_host_roots SET policy_json='{}'")
            dangerous = self.app.auto_approve_pending(self.approval())
        judge.assert_not_called()
        self.assertTrue(ordinary["autoApproved"])
        self.assertTrue(dangerous["autoApproved"])
        self.assertEqual(self.executor.call_count, 2)

    def test_unavailable_jev_is_recoverable_without_effect_or_fallback(self):
        result = self.app.auto_approve_pending(self.approval())
        self.executor.assert_not_called()
        self.assertTrue(result["blocked"])
        self.assertTrue(result["retryable"])
        self.assertFalse(result["autoApproved"])
        self.assertFalse(result["approvalRequired"])
        self.assertEqual(result["approval"]["state"], "rejected")
        self.assertEqual(result["failureCode"], "JEV_NOT_CONFIGURED")

    def test_stop_while_judging_still_fences_original_executor(self):
        def stop_then_approve(*args, **kwargs):
            self.app.room_turns.cancel(self.sid, "jev-root")
            return "approve", ["authorized_scope"], "Jev approved"
        with patch("rag_ime.agent_approval_model._jev_api_key", return_value="test"), patch(
            "rag_ime.agent_approval_model._jev_decide", side_effect=stop_then_approve
        ):
            result = self.app.auto_approve_pending(self.approval())
        self.executor.assert_not_called()
        self.assertFalse(result["autoApproved"])
        self.assertEqual(result["approval"]["state"], "stale")


if __name__ == "__main__":
    unittest.main()
