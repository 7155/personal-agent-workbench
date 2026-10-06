from __future__ import annotations

import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from rag_ime.agent_background_jobs import AgentBackgroundJobService
from rag_ime.agent_gateway_requests import GatewayRequestConflict, GatewayRequestUnresolved
from rag_ime.agent_service import AgentService
from rag_ime.agent_session_application import AgentSessionApplicationService
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.agent_tools import ControlToolGateway
from rag_ime.agent_workspace import WorkspaceHarness
from rag_ime.debug_server import DebugRequestHandler
from rag_ime.pi.runtime import PiRuntimeHostManager, _HostedSessionState
from rag_ime.pi.values import PiRuntimeCommandRejected
from tests.sqlite_fixtures import copy_current_database
from tests.test_agent_tools import _Core, _Facade, _Management


class _Runtime:
    def __init__(self):
        self.turns = {}
        self.admissions = {}

    def is_gateway_turn_active(self, session_id, turn_id, *, client_message_id):
        return self.turns.get(session_id) == (turn_id, client_message_id)

    def abort(self, session_id):
        self.admissions.pop(session_id, None)
        turn_id, _ = self.turns.pop(session_id, ("", ""))
        return {"turnId": turn_id, "lifecycle": {"drained": True}}

    def abort_with_approval_fence(self, session_id, before_abort):
        turn_id, client_id = self.turns.get(session_id, ("", ""))
        pending = session_id in self.admissions
        before_abort({"turnId": "" if pending else turn_id,
            "clientMessageId": self.admissions.get(session_id, client_id), "pendingAdmission": pending})
        return self.abort(session_id)


class GatewayExecutionIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"RAG_IME_APP_SUPPORT_DIR": str(Path(self.tmp.name) / "support")})
        self.env.start()
        self.addCleanup(self.env.stop)
        db = Path(self.tmp.name) / "identity.sqlite"
        copy_current_database(db)
        self.store = AgentSessionStore(db)
        self.store.initialize()
        self.workspace = Path(self.tmp.name) / "work"
        self.workspace.mkdir()
        self.session = self.new_session()
        self.runtime = _Runtime()
        self.runtime.turns[self.session["id"]] = ("turn:old", "message:old")
        self.context = {"roomId": "room:old", "rootId": "root:old", "dispatchId": "dispatch:old", "generation": 1}
        self.effects = []
        self.gateway = self.new_gateway()
        self.service = object.__new__(AgentService)
        self.service._require_mutable_session = lambda session_id: self.store.get(session_id)
        self.service._workspace_command_cancellation = self.gateway.workspace_commands.request_cancel
        self.service.background_jobs = AgentBackgroundJobService(
            db, events=lambda *args, **kwargs: None, sessions=self.store)
        self.service.background_jobs.initialize()
        self.addCleanup(self.service.background_jobs.close)
        app = object.__new__(AgentSessionApplicationService)
        app.sessions = self.store
        app._runtime_provider = lambda: self.runtime
        app.cancel_pending_approvals = self.store.cancel_pending_approvals
        self.service.session_application = app

    def new_session(self):
        session = self.store.create(title="identity fixture")
        session = self.store.set_runtime_policy(session["id"], mode="coordinator", execution_mode="full_trust",
            tool_profile_version="control-center-v1", allowed_tools=None, workspace_roots=[str(self.workspace)],
            grant_workspace_scope=True)
        self.store.bind_runtime_session(session["id"], driver_id="managed-pi", runtime_kind="pi_rpc",
            external_session_id="isolated:" + session["id"])
        return session

    def new_gateway(self):
        owner = self
        class Room:
            runtime = owner.runtime
            def _active_room_dispatch_context(self, session_id):
                return dict(owner.context)
        def effect(prepared):
            self.effects.append(prepared.command)
            return {"schemaVersion": "rag-ime.workspace-command-receipt.v1", "mutationApplied": True,
                "summary": "fixture effect", "exitCode": 0}
        gateway = ControlToolGateway(sessions=self.store, management=_Management(), core=_Core(),
            facade=_Facade(), project="wisdom-weasel-rag-ime", collaboration=Room(),
            workspace_harness=WorkspaceHarness(executor=effect))
        def auto(approval):
            decided = self.store.decide_approval(approval["approvalId"], approved=True,
                payload_sha256=approval["payloadSha256"], decided_by="execution-policy:room_unrestricted")
            claimed = self.store.claim_approval_execution(approval["approvalId"], room_context=self.context)
            receipt = gateway.apply_approval(claimed)
            self.store.complete_approval(approval["approvalId"], state="applied", receipt=receipt)
            return {"summary": receipt["summary"], "approvalRequired": False, "autoApproved": True,
                "approvalId": approval["approvalId"], "receipt": receipt}
        gateway.bind_auto_approval_executor(auto)
        return gateway

    def request(self, *, call_id="call:one", session_id=None, client_id="message:old", turn_id="turn:old"):
        return {"schemaVersion": "rag-ime.agent-tool-call.v1", "sessionId": session_id or self.session["id"],
            "tool": "bash", "toolCallId": call_id, "sourceLoopId": "pi:message:assistant:1",
            "executionBinding": {"turnId": turn_id, "clientMessageId": client_id},
            "roomCapability": dict(self.context),
            "args": {"command": "echo effect", "cwd": str(self.workspace), "timeout": 5}}

    def reopen(self):
        self.store = AgentSessionStore(self.store.db_path)
        self.store.initialize()
        return self.new_gateway()

    def test_lost_http_response_replays_original_after_reopen_and_stop(self):
        request = self.request()
        first = self.gateway.execute(request)
        self.service.abort(self.session["id"])
        replay = self.reopen().execute(request)
        self.assertEqual(replay, first)
        self.assertEqual(len(self.effects), 1)
        self.assertEqual(len(self.store.find_tool_call_approvals(self.session["id"], "call:one", "workspace_shell")), 1)

    def test_same_identity_different_args_conflicts(self):
        request = self.request()
        self.gateway.execute(request)
        request["args"]["command"] = "echo changed"
        with self.assertRaises(GatewayRequestConflict):
            self.gateway.execute(request)
        self.assertEqual(len(self.effects), 1)

    def test_concurrent_duplicate_has_one_effect(self):
        entered, finish = threading.Event(), threading.Event()
        original = self.gateway._auto_approval_executor
        def blocked(approval):
            entered.set()
            self.assertTrue(finish.wait(5))
            return original(approval)
        self.gateway.bind_auto_approval_executor(blocked)
        errors = []
        def first():
            try:
                self.gateway.execute(self.request())
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=first)
        thread.start()
        self.assertTrue(entered.wait(5))
        try:
            with self.assertRaises(GatewayRequestUnresolved):
                self.new_gateway().execute(self.request())
        finally:
            finish.set()
            thread.join(5)
        self.assertFalse(errors)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(self.effects), 1)

    def test_effect_with_failed_result_storage_is_unknown_after_reopen(self):
        with patch.object(self.gateway.gateway_requests, "complete", side_effect=OSError("receipt disk failure")):
            with self.assertRaises(OSError):
                self.gateway.execute(self.request())
        with self.assertRaises(GatewayRequestUnresolved):
            self.reopen().execute(self.request())
        self.assertEqual(len(self.effects), 1)

    def test_claimed_effect_with_failed_approval_storage_is_unknown_after_reopen(self):
        with patch.object(self.store, "complete_approval", side_effect=OSError("approval disk failure")):
            with self.assertRaises(OSError):
                self.gateway.execute(self.request())
        with self.assertRaises(GatewayRequestUnresolved):
            self.reopen().execute(self.request())
        self.assertEqual(len(self.effects), 1)

    def test_effect_with_all_receipt_writes_failed_retains_unfinished_admission(self):
        with patch.object(self.gateway.gateway_requests, "complete", side_effect=OSError("disk failure")), \
            patch.object(self.gateway.gateway_requests, "unknown", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.gateway.execute(self.request())
        with self.assertRaises(GatewayRequestUnresolved):
            self.reopen().execute(self.request())
        self.assertEqual(len(self.effects), 1)

    def test_legacy_unbound_call_still_executes_and_alias_replay_is_canonical(self):
        request = self.request()
        request.pop("executionBinding")
        request.pop("roomCapability")
        first = self.gateway.execute(request)
        request["tool"] = "workspace_shell"
        request["args"]["op"] = "run"
        request["args"]["timeoutSeconds"] = request["args"].pop("timeout")
        self.assertEqual(self.gateway.execute(request), first)
        self.assertEqual(len(self.effects), 1)

    def test_historical_approval_without_ledger_never_reexecutes(self):
        self.gateway.execute(self.request())
        original = self.store.find_tool_call_approvals(self.session["id"], "call:one", "workspace_shell")[0]
        with self.store.approval_creation_scope(session_id=self.session["id"], tool_call_id="call:one"):
            self.store.create_approval(session_id=self.session["id"], tool_name="workspace_shell", operation="run",
                payload_sha256=original["payloadSha256"], preview=original["preview"], risk_level="R2")
        historical_ids = [approval["approvalId"] for approval in
            self.store.find_tool_call_approvals(self.session["id"], "call:one", "workspace_shell")]
        with self.store._connect() as conn:
            conn.execute("DELETE FROM agent_gateway_requests")
        with self.assertRaisesRegex(GatewayRequestUnresolved, "Historical"):
            self.new_gateway().execute(self.request())
        self.assertEqual(len(self.effects), 1)
        self.assertEqual(historical_ids, [approval["approvalId"] for approval in
            self.store.find_tool_call_approvals(self.session["id"], "call:one", "workspace_shell")])

    def test_stale_room_capability_never_rebinds_to_new_root(self):
        request = self.request()
        self.context.update(rootId="root:new", dispatchId="dispatch:new")
        with self.assertRaisesRegex(ValueError, "stale Room"):
            self.gateway.execute(request)
        self.assertFalse(self.effects)
        self.assertFalse(self.store.find_tool_call_approvals(self.session["id"], "call:one", "workspace_shell"))

    def test_room_rotation_between_owner_check_and_context_capture_creates_no_new_approval(self):
        request = self.request()
        old = dict(self.context)
        new = {**old, "rootId": "root:new", "dispatchId": "dispatch:new"}
        with patch.object(self.gateway, "_room_dispatch_context", side_effect=[old, old, old, new]):
            with self.assertRaisesRegex(ValueError, "stale Room"):
                self.gateway.execute(request)
        self.assertFalse(self.effects)
        self.assertFalse(self.store.find_tool_call_approvals(self.session["id"], "call:one", "workspace_shell"))

    def test_full_session_abort_blocks_late_call_and_new_turn_is_unaffected(self):
        request = self.request()
        receipt = self.service.abort(self.session["id"])
        self.assertTrue(receipt["workspaceCommands"]["drained"])
        with self.assertRaisesRegex(ValueError, "inactive Runtime"):
            self.gateway.execute(request)
        self.runtime.turns[self.session["id"]] = ("turn:new", "message:new")
        with self.assertRaisesRegex(ValueError, "inactive Runtime"):
            self.gateway.execute(self.request(call_id="late:new-id"))
        self.gateway.execute(self.request(call_id="call:new", turn_id="turn:new", client_id="message:new"))
        other = self.new_session()
        self.runtime.turns[other["id"]] = ("turn:other", "message:other")
        self.gateway.execute(self.request(call_id="call:one", session_id=other["id"], turn_id="turn:other", client_id="message:other"))
        self.assertEqual(len(self.effects), 2)

    def test_room_empty_client_identity_is_exact_and_late_call_is_blocked(self):
        self.runtime.turns[self.session["id"]] = ("turn:room", "")
        self.gateway.execute(self.request(call_id="room:active", turn_id="turn:room", client_id=""))
        request = self.request(call_id="room:late", turn_id="turn:room", client_id="")
        request.pop("roomCapability")
        self.service.abort(self.session["id"])
        with self.assertRaisesRegex(ValueError, "inactive Runtime"):
            self.gateway.execute(request)
        self.assertEqual(len(self.effects), 1)

    def test_stop_between_owner_check_and_approval_insert_has_no_effect(self):
        original = self.gateway._prepare_approval
        def after_stop(**kwargs):
            self.service.abort(self.session["id"])
            return original(**kwargs)
        with patch.object(self.gateway, "_prepare_approval", side_effect=after_stop):
            with self.assertRaisesRegex(ValueError, "inactive Runtime"):
                self.gateway.execute(self.request())
        approval = self.store.find_tool_call_approvals(self.session["id"], "call:one", "workspace_shell")[0]
        approved = self.store.decide_approval(approval["approvalId"], approved=True,
            payload_sha256=approval["payloadSha256"], decided_by="execution-policy:room_unrestricted")
        claimed = self.store.claim_approval_execution(approved["approvalId"], room_context=self.context)
        self.assertEqual(claimed["state"], "stale")
        self.assertFalse(self.effects)

    def test_stop_after_approval_insert_before_claim_has_no_effect(self):
        def stopped(approval):
            self.service.abort(self.session["id"])
            return self.store.claim_approval_execution(approval["approvalId"], room_context=self.context)
        self.gateway.bind_auto_approval_executor(stopped)
        with self.assertRaises(ValueError):
            self.gateway.execute(self.request())
        self.assertFalse(self.effects)

    def test_abort_failure_fences_late_approval_insert_before_rpc(self):
        request = self.request()
        request.pop("roomCapability")
        original = self.gateway._prepare_approval
        def failed_abort(session_id):
            self.runtime.turns.pop(session_id, None)
            raise TimeoutError("Host abort ACK lost")
        def insert_after_failed_stop(**kwargs):
            with self.assertRaises(TimeoutError):
                self.service.abort(self.session["id"])
            return original(**kwargs)
        with patch.object(self.gateway, "_room_dispatch_context", return_value=None), \
            patch.object(self.runtime, "abort", side_effect=failed_abort), \
            patch.object(self.gateway, "_prepare_approval", side_effect=insert_after_failed_stop):
            with self.assertRaisesRegex(ValueError, "inactive Runtime"):
                self.gateway.execute(request)
        approval = self.store.find_tool_call_approvals(self.session["id"], "call:one", "workspace_shell")[0]
        approved = self.store.decide_approval(approval["approvalId"], approved=True,
            payload_sha256=approval["payloadSha256"], decided_by="user")
        self.assertEqual(self.store.claim_approval_execution(approved["approvalId"])["state"], "stale")
        self.assertFalse(self.effects)

    def test_old_abort_ack_never_cancels_new_turn_approval(self):
        original_abort = self.runtime.abort
        created = []
        def settle_old_then_start_new(session_id):
            receipt = original_abort(session_id)
            self.runtime.turns[session_id] = ("turn:new", "message:new")
            with self.store.approval_creation_scope(session_id=session_id, tool_call_id="call:new", turn_id="turn:new"):
                created.append(self.store.create_approval(session_id=session_id, tool_name="workspace_write",
                    operation="apply", payload_sha256="1" * 64, preview={}, risk_level="R1"))
            return receipt
        with patch.object(self.runtime, "abort", side_effect=settle_old_then_start_new):
            self.service.abort(self.session["id"])
        self.assertEqual(self.store.get_approval(created[0]["approvalId"])["state"], "pending")

    def test_pending_admission_stop_fences_ledger_turn_even_when_ack_fails(self):
        for client_id in ("message:old", ""):
            session_id = self.session["id"]
            self.runtime.turns[session_id] = ("turn:old", client_id)
            self.runtime.admissions[session_id] = client_id
            request = self.request(call_id="pending:" + client_id, client_id=client_id)
            original = self.gateway._prepare_approval
            def stop_before_insert(_stop_session_id=session_id, _original=original, **kwargs):
                with patch.object(self.runtime, "abort", side_effect=TimeoutError("ACK lost")):
                    with self.assertRaises(TimeoutError):
                        self.service.abort(_stop_session_id)
                self.runtime.turns.pop(_stop_session_id, None)
                return _original(**kwargs)
            with patch.object(self.gateway, "_prepare_approval", side_effect=stop_before_insert):
                with self.assertRaisesRegex(ValueError, "inactive Runtime"):
                    self.gateway.execute(request)
            approval = self.store.find_tool_call_approvals(session_id, request["toolCallId"], "workspace_shell")[0]
            approved = self.store.decide_approval(approval["approvalId"], approved=True,
                payload_sha256=approval["payloadSha256"], decided_by="user")
            self.assertEqual(self.store.claim_approval_execution(approved["approvalId"])["state"], "stale")
        self.assertFalse(self.effects)

    def test_exact_stop_preserves_other_bound_turn_and_limits_legacy_to_captured_ids(self):
        session_id = self.session["id"]
        def prepare(turn_id, call_id):
            with self.store.approval_creation_scope(session_id=session_id, tool_call_id=call_id, turn_id=turn_id):
                return self.store.create_approval(session_id=session_id, tool_name="workspace_write", operation="apply",
                    payload_sha256="1" * 64, preview={}, risk_level="R1")
        old = prepare("turn:old", "old")
        legacy = prepare("", "legacy")
        new = prepare("turn:new", "new")
        self.store.cancel_pending_approvals(session_id, turn_id="turn:old")
        self.assertEqual(self.store.get_approval(old["approvalId"])["state"], "stale")
        self.assertEqual(self.store.get_approval(legacy["approvalId"])["state"], "stale")
        self.assertEqual(self.store.get_approval(new["approvalId"])["state"], "pending")
        captured = self.store.pending_approval_ids(session_id)
        after = prepare("", "legacy-after")
        self.store.cancel_pending_approvals(session_id, approval_ids=captured)
        self.assertEqual(self.store.get_approval(after["approvalId"])["state"], "pending")

    def test_pending_admission_stop_cancels_approval_after_http_reply_was_completed(self):
        session_id = self.session["id"]
        self.runtime.admissions[session_id] = "message:old"
        self.gateway.bind_auto_approval_executor(lambda approval: {
            "summary": "pending fixture approval", "approvalRequired": True, "approval": dict(approval)})
        response = self.gateway.execute(self.request())
        approval_id = response["result"]["approval"]["approvalId"]
        self.service.abort(session_id)
        self.assertEqual(self.store.get_approval(approval_id)["state"], "stale")
        with self.store._read_connect() as conn:
            self.assertIsNotNone(conn.execute("SELECT 1 FROM agent_gateway_cancelled_turns WHERE session_id = ? AND turn_id = 'turn:old'",
                (session_id,)).fetchone())
        self.assertFalse(self.effects)

    def test_cancelled_pending_http_reply_still_identifies_empty_room_turn_for_stop(self):
        session_id = self.session["id"]
        self.runtime.turns[session_id] = ("turn:old", "")
        self.runtime.admissions[session_id] = ""
        self.gateway.bind_auto_approval_executor(lambda approval: {
            "summary": "pending fixture approval", "approvalRequired": True, "approval": dict(approval)})
        self.gateway.execute(self.request(client_id=""))
        self.service.abort(session_id)
        self.assertEqual(self.store.cancelled_gateway_admission_turn(session_id, ""), "turn:old")

    def test_legacy_driver_stop_limits_ack_cleanup_to_the_captured_approval_set(self):
        session_id = self.session["id"]
        old = self.store.create_approval(session_id=session_id, tool_name="workspace_write", operation="apply",
            payload_sha256="1" * 64, preview={}, risk_level="R1")
        created = []
        def legacy_abort(_session_id):
            created.append(self.store.create_approval(session_id=_session_id, tool_name="workspace_write",
                operation="apply", payload_sha256="2" * 64, preview={}, risk_level="R1"))
            return {"turnId": "turn:old", "lifecycle": {"drained": True}}
        self.service.session_application._runtime_provider = lambda: SimpleNamespace(abort=legacy_abort)
        receipt = self.service.abort(session_id)
        self.assertEqual(receipt["approvalCancellation"]["cancelledApprovalIds"], [old["approvalId"]])
        self.assertEqual(self.store.get_approval(created[0]["approvalId"])["state"], "pending")

    def test_pre_rpc_fence_storage_failure_never_dispatches_an_unfenced_abort(self):
        with patch.object(self.service.session_application, "cancel_pending_approvals", side_effect=OSError("fence unavailable")), \
            patch.object(self.runtime, "abort") as abort:
            with self.assertRaisesRegex(OSError, "fence unavailable"):
                self.service.abort(self.session["id"])
            abort.assert_not_called()


class GatewayTurnObserverTests(unittest.TestCase):
    def test_bound_abort_rejection_disarms_its_fallback_without_cancelling_new_turn(self):
        manager = object.__new__(PiRuntimeHostManager)
        manager._lock = threading.RLock()
        state = _HostedSessionState(turn_id="turn:old", client_message_id="message:old")
        manager._states = {"session:test": state}
        manager._retired_host_turns = set()
        manager._host_capabilities = {"sessionBoundAbort": True}
        manager.events = SimpleNamespace(publish=lambda *args, **kwargs: None)
        manager._abort_fallback_expired = lambda *args: None
        class Timer:
            started = False
            cancelled = False
            def start(self): self.started = True
            def cancel(self): self.cancelled = True
        timer = Timer()
        class Client:
            def send(self, *args, **kwargs):
                raise PiRuntimeCommandRejected("target changed", host_error_code="ABORT_TARGET_MISMATCH")
        manager._require_client = lambda: Client()
        with patch("rag_ime.pi.runtime.threading.Timer", return_value=timer):
            with self.assertRaises(PiRuntimeCommandRejected):
                manager.abort("session:test")
        self.assertTrue(timer.cancelled)
        self.assertIsNone(state.abort_timer)

    def test_pending_abort_worker_preserves_new_owner_after_rejection_or_unknown_error(self):
        for error in (PiRuntimeCommandRejected("target changed", host_error_code="ABORT_TARGET_MISMATCH"),
            TimeoutError("ACK lost")):
            manager = object.__new__(PiRuntimeHostManager)
            manager._lock = threading.RLock()
            state = _HostedSessionState(turn_id="turn:new", client_message_id="message:new")
            manager._states = {"session:test": state}
            manager._host_capabilities = {"sessionBoundAbort": True}
            published, requests = [], []
            manager.events = SimpleNamespace(publish=lambda *args, _published=published, **kwargs: _published.append(args))
            class Client:
                def send(self, method, params, _requests=requests, _error=error, **kwargs):
                    _requests.append((method, dict(params)))
                    raise _error
            manager._require_client = lambda client_type=Client: client_type()
            manager._deliver_pending_admission_abort("session:test", "message:old")
            self.assertEqual(requests, [("session.abort", {"sessionId": "session:test", "expectedClientMessageId": "message:old"})])
            self.assertFalse(published)
            self.assertEqual(state.turn_id, "turn:new")

    def test_python_abort_carries_original_target_when_next_turn_starts_before_rpc(self):
        for client_id in ("message:old", ""):
            manager = object.__new__(PiRuntimeHostManager)
            manager._lock = threading.RLock()
            state = _HostedSessionState(turn_id="turn:old", client_message_id=client_id)
            manager._states = {"session:test": state}
            manager._retired_host_turns = set()
            manager._host_capabilities = {"sessionBoundAbort": True}
            manager.events = SimpleNamespace(publish=lambda *args, **kwargs: None)
            manager._abort_fallback_expired = lambda *args: None
            requests = []
            class Client:
                def send(self, method, params, _requests=requests, **kwargs):
                    _requests.append((method, dict(params)))
                    return {"schemaVersion": "rag-ime.pi-session-abort-receipt.v1",
                        "sessionId": "session:test", "turnId": params["expectedTurnId"],
                        "lifecycle": {"schemaVersion": "pi.agent-abort-receipt.v1",
                            "idle": False, "drained": False, "pendingOperations": []}}
            def replacement_before_rpc(state=state, client_type=Client):
                state.turn_id = "turn:new"
                state.client_message_id = "message:new"
                return client_type()
            manager._require_client = replacement_before_rpc
            try:
                manager.abort("session:test")
            finally:
                if state.abort_timer is not None:
                    state.abort_timer.cancel()
            self.assertEqual(requests, [("session.abort", {"sessionId": "session:test",
                "expectedTurnId": "turn:old", "clientMessageId": client_id})])
            self.assertEqual(state.turn_id, "turn:new")

    def test_http_preserves_unknown_and_conflict_without_claiming_no_effect(self):
        for error in (GatewayRequestUnresolved("outcome unknown"), GatewayRequestConflict("identity conflict")):
            handler = object.__new__(DebugRequestHandler)
            handler.path = "/api/agent/tool/execute"
            handler.headers = {"X-RAG-IME-Agent-Token": "isolated-test-token"}
            handler._authorize_gateway_request = lambda *args: True
            handler._read_json = lambda: {}
            written = []
            handler._write_json = lambda status, payload, written=written: written.append((status, payload))
            def execute(_request, error=error):
                raise error
            handler.service = SimpleNamespace(agent=SimpleNamespace(tool_token="isolated-test-token"),
                agent_tools=SimpleNamespace(execute=execute))
            handler.do_POST()
            status, response = written[0]
            self.assertEqual(status, 409)
            self.assertEqual(response["errorCode"], error.error_code)
            self.assertFalse(response["retryable"])
            self.assertNotIn("mutationApplied", response)

    def manager(self, client_id="message:pending"):
        manager = object.__new__(PiRuntimeHostManager)
        manager._lock = threading.RLock()
        manager._open_sessions = {"session:test"}
        manager._host_capabilities = {"sessionControlState": True}
        manager._retired_host_turns = set()
        state = _HostedSessionState()
        state.prompt_admission_in_flight = True
        state.prompt_dispatched = True
        state.admission_client_message_id = client_id
        manager._states = {"session:test": state}
        class Client:
            running = True
            calls = 0
            def send(self, method, params, *, timeout):
                self.calls += 1
                return {"sessionId": "session:test", "isIdle": False,
                    "activeTurn": {"turnId": "turn:pending", "clientMessageId": client_id}}
        manager._client = Client()
        return manager, state

    def test_http_before_event_and_ack_uses_existing_host_read(self):
        manager, state = self.manager()
        self.assertTrue(manager.is_gateway_turn_active("session:test", "turn:pending", client_message_id="message:pending"))
        self.assertEqual(manager._client.calls, 1)
        self.assertEqual(state.turn_id, "")

    def test_empty_room_client_is_exact_not_wildcard(self):
        manager, state = self.manager("")
        self.assertTrue(manager.is_gateway_turn_active("session:test", "turn:pending", client_message_id=""))
        self.assertFalse(manager.is_gateway_turn_active("session:test", "turn:pending", client_message_id="other"))

    def test_abort_retired_and_different_turn_do_not_query_host(self):
        for kind in ("abort", "retired", "different", "idle"):
            manager, state = self.manager()
            if kind == "abort": state.abort_pending_admission = True
            elif kind == "retired": state.retired_turn_ids.add("turn:pending")
            elif kind == "different": state.turn_id = "turn:other"
            else: state.prompt_admission_in_flight = False
            self.assertFalse(manager.is_gateway_turn_active("session:test", "turn:pending", client_message_id="message:pending"))
            self.assertEqual(manager._client.calls, 0)

    def test_abort_racing_control_state_read_is_rechecked(self):
        manager, state = self.manager()
        original = manager._client.send
        def aborting(*args, **kwargs):
            observed = original(*args, **kwargs)
            state.abort_pending_admission = True
            return observed
        manager._client.send = aborting
        self.assertFalse(manager.is_gateway_turn_active("session:test", "turn:pending", client_message_id="message:pending"))
