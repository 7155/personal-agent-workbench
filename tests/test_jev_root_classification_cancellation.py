"""Public Root lifecycle against the real Runtime; only its private pipe is fake."""
from __future__ import annotations

import json
import threading
from unittest.mock import Mock, patch

from rag_ime.jev_tasks.decider import JevChoices
from rag_ime.jev_tasks.types import Candidate, DecisionUnavailable
from rag_ime.pi.values import PiRuntimeCommandAcceptanceUnknown
from tests.test_jev_host_application import JevHostFixture, choose_execution
from tests import test_agent_room_reservation_cancellation as cancellation_fixtures


class RootClassificationStop(JevHostFixture):
    def setUp(self):
        super().setUp()
        self.runtime = self.service.runtime
        self.client = Mock(running=True)
        self.runtime._client = self.client
        self.runtime._host_capabilities = {"statelessClassification": True}
        self.app.driver.controller.decider = JevChoices.from_paw(runtime_provider=lambda: self.service.runtime)
        self.requests = []
        self.aborts = []
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.entered = threading.Event()
        self.errors = []
        self.client.send.side_effect = self.send
        key = patch("rag_ime.jev.api_key", return_value="offline-dummy-key")
        key.start()
        self.addCleanup(key.stop)
        fallback = patch("rag_ime.jev.evaluate", side_effect=AssertionError("legacy fallback forbidden"))
        fallback.start()
        self.addCleanup(fallback.stop)

    def send(self, method, params, **kwargs):
        if method == "classification.once":
            kwargs["before_write"]()
            self.requests.append(dict(params))
            self.entered.set()
            if not self.release.wait(10):
                raise TimeoutError("offline classification barrier")
            return self.answer(params)
        if method == "classification.abort":
            self.aborts.append(dict(params))
            return {**params, "aborted": True, "active": True, "drained": False}
        raise AssertionError("unexpected private RPC " + method)

    @staticmethod
    def answer(params):
        return {**choose_execution(json.dumps(params["state"]), params["questions"]),
                "requestId": params["requestId"], "dispatchId": params["dispatchId"], "stopReason": "stop"}

    def root(self, key="classification-stop-root", strategy="direct"):
        return self.service.jev_command(self.room["id"], {
            "action": "create", "clientMessageId": key, "message": "完成这项后端测试",
            "strategy": strategy, "modelRouting": "participant",
            "controllerParticipantId": self.room["participants"][0]["id"],
        })

    def worker(self, action):
        def run():
            try:
                action()
            except BaseException as error:
                self.errors.append(error)
        worker = threading.Thread(target=run, name="offline-root-classification")
        worker.start()
        return worker

    def finish(self, worker):
        self.release.set()
        worker.join(10)
        self.assertFalse(worker.is_alive(), "offline worker failed to drain")

    def exact_abort(self, request):
        return {"requestId": request["requestId"], "dispatchId": request["dispatchId"]}

    def check_public_stop(self, strategy):
        created = self.root(strategy=strategy)
        worker = self.worker(lambda: self.app.tick(limit=1))
        try:
            self.assertTrue(self.entered.wait(10))
            receipt = self.app.stop(self.room["id"], created["rootId"])
            self.assertTrue(self.app.projection(self.room["id"], created["graphId"])["stopped"])
            self.assertEqual(self.aborts, [self.exact_abort(self.requests[0])])
            self.assertEqual(receipt["pendingClassifications"], [{
                "requestId": self.requests[0]["requestId"], "graphId": created["graphId"],
                "status": "cancellation_requested",
            }])
            self.assertEqual(receipt["status"], "cancellation_pending")
            self.assertFalse(receipt["ok"])
            self.assertIn("classification", receipt["pendingTargets"])
            self.assertEqual(self.runtime.runtime_status()["status"], "busy")
        finally:
            self.finish(worker)
        self.assertEqual(self.errors, [])
        self.assertEqual(self.snapshot(created).tasks[0].state, "cancelled")
        self.prompt.assert_not_called()
        self.assertEqual(self.app._classifications, {})
        self.assertEqual(self.runtime.runtime_status()["status"], "ready")
        self.assertEqual(self.app.stop(self.room["id"], created["rootId"])["pendingClassifications"], [])

    def test_controller_root_stop_cancels_inflight_native_classification(self):
        self.check_public_stop("direct")

    def test_auto_route_stop_cancels_inflight_native_classification(self):
        self.check_public_stop("auto")

    def choice(self, created):
        return self.app.choose_action(created["graphId"], {}, [
            Candidate.make("wait", created["rootId"], "wait", {}),
            Candidate.make("plan", created["rootId"], "plan", {}),
        ])

    def test_stopped_root_cannot_register_a_later_classification(self):
        created = self.root()
        self.app.stop(self.room["id"], created["rootId"])
        with self.assertRaises(DecisionUnavailable):
            self.choice(created)
        self.client.send.assert_not_called()
        self.assertEqual(self.app._classifications, {})

    def test_stop_between_root_registration_and_runtime_admission_prevents_dispatch(self):
        created = self.root()
        native = self.runtime.classify_once
        def before_admission(**kwargs):
            self.entered.set()
            if not self.release.wait(10):
                raise TimeoutError("before Runtime admission")
            return native(**kwargs)
        with patch.object(self.runtime, "classify_once", side_effect=before_admission):
            worker = self.worker(lambda: self.choice(created))
            try:
                self.assertTrue(self.entered.wait(10))
                receipt = self.app.stop(self.room["id"], created["rootId"])
                self.assertEqual(len(receipt["pendingClassifications"]), 1)
                self.client.send.assert_not_called()
            finally:
                self.finish(worker)
        self.assertEqual(len(self.errors), 1)
        self.assertIsInstance(self.errors[0], DecisionUnavailable)
        self.client.send.assert_not_called()
        self.assertEqual(self.app._classifications, {})
        self.assertEqual(self.runtime._classifications, {})

    def test_stop_after_runtime_admission_before_pipe_write_prevents_dispatch(self):
        created = self.root()
        def before_pipe(method, params, **kwargs):
            self.assertEqual(method, "classification.once")
            self.entered.set()
            if not self.release.wait(10):
                raise TimeoutError("before pipe write")
            kwargs["before_write"]()
            self.fail("cancelled request crossed pipe admission")
        self.client.send.side_effect = before_pipe
        worker = self.worker(lambda: self.choice(created))
        try:
            self.assertTrue(self.entered.wait(10))
            self.app.stop(self.room["id"], created["rootId"])
            self.assertEqual(self.aborts, [])
        finally:
            self.finish(worker)
        self.assertEqual(len(self.errors), 1)
        self.assertIsInstance(self.errors[0], DecisionUnavailable)
        self.assertEqual(self.app._classifications, {})
        self.assertEqual(self.runtime._classifications, {})

    def unknown_choice(self):
        def unknown(method, params, **kwargs):
            if method == "classification.once":
                kwargs["before_write"]()
                self.requests.append(dict(params))
                raise PiRuntimeCommandAcceptanceUnknown("offline reply lost")
            return self.send(method, params, **kwargs)
        self.client.send.side_effect = unknown
        created = self.root()
        with self.assertRaises(DecisionUnavailable):
            self.choice(created)
        self.assertEqual(len(self.app._classifications), 1)
        return created

    def settled_notice(self, request, client=None):
        self.runtime._handle_host_event({"protocolVersion": "2", "event": "runtime.notice", "payload": {
            "type": "classification_settled", **self.exact_abort(request), "stopReason": "aborted",
        }}, source_client=client or self.client)

    def test_unknown_undrained_choice_remains_root_cancellable_until_actual_settlement(self):
        created = self.unknown_choice()
        self.assertEqual(len(self.app.projection(self.room["id"], created["graphId"])["pendingClassifications"]), 1)
        self.assertEqual(len(self.aborts), 1)  # original Runtime unknown cleanup signal
        receipt = self.app.stop(self.room["id"], created["rootId"])
        self.assertEqual(self.aborts, [self.exact_abort(self.requests[0])] * 2)
        self.assertEqual(len(receipt["pendingClassifications"]), 1)
        self.settled_notice({**self.requests[0], "dispatchId": "unrelated-dispatch"})
        self.assertEqual(len(self.app._classifications), 1)
        self.settled_notice(self.requests[0])
        self.assertEqual(self.app._classifications, {})  # no subsequent Stop needed
        self.assertEqual(self.runtime._classifications, {})
        projection = self.app.projection(self.room["id"], created["graphId"])
        self.assertTrue(projection["stopped"])
        self.assertEqual(projection["pendingClassifications"], [])
        events = self.service.rooms.list_events_for_turn(self.room["id"], created["rootId"])
        self.assertTrue(any(event["payload"].get("classificationSettled") is True for event in events))

    def test_active_session_plus_unknown_classifier_never_publishes_false_drain(self):
        created = self.unknown_choice()
        session_id = self.room["participants"][1]["sessionId"]
        self.service.room_turns.begin(session_id, created["rootId"], dispatch_id="execution-before-stop")
        self.service.room_turns.accept(session_id, "turn:before-stop", created["rootId"])
        before = len(self.service.rooms.list_events_for_turn(self.room["id"], created["rootId"]))
        with patch.object(self.service, "abort", side_effect=cancellation_fixtures.RoomReservationCancellationTests.typed_abort):
            receipt = self.app.stop(self.room["id"], created["rootId"])
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["status"], "cancellation_pending")
        self.assertEqual(receipt["pendingTargets"], ["classification"])
        self.assertEqual(self.service.room_turns.active_turn(session_id), ("", ""))
        self.service.room_turns.begin(session_id, "different-root-after-drained-session")
        self.service.room_turns.accept(session_id, "different-turn", "different-root-after-drained-session")
        events = self.service.rooms.list_events_for_turn(self.room["id"], created["rootId"])[before:]
        self.assertFalse(any(event["eventType"] == "turn_completed" for event in events))
        self.assertFalse(any(event["payload"].get("status") == "cancellation_applied" for event in events))
        self.settled_notice(self.requests[0])
        events = self.service.rooms.list_events_for_turn(self.room["id"], created["rootId"])[before:]
        self.assertFalse(any(event["eventType"] == "turn_completed" for event in events))
        self.assertTrue(any(event["payload"].get("classificationSettled") is True for event in events))
        self.assertEqual(self.service.room_turns.active_turn(session_id), ("different-root-after-drained-session", ""))

    def test_unsupported_host_fallback_stays_pending_until_direct_adapter_returns(self):
        self.runtime._host_capabilities = {}
        created = self.root()
        def legacy(state, questions, **_kwargs):
            self.entered.set()
            if not self.release.wait(10):
                raise TimeoutError("offline legacy barrier")
            return choose_execution(state, questions)
        with patch("rag_ime.jev.evaluate", side_effect=legacy):
            worker = self.worker(lambda: self.app.tick(limit=1))
            try:
                self.assertTrue(self.entered.wait(10))
                receipt = self.app.stop(self.room["id"], created["rootId"])
                self.assertFalse(receipt["ok"])
                self.assertEqual(receipt["status"], "cancellation_pending")
                self.assertEqual(len(receipt["pendingClassifications"]), 1)
                self.client.send.assert_not_called()
                self.assertEqual(len(self.app._classifications), 1)
            finally:
                self.finish(worker)
        self.assertEqual(self.errors, [])
        self.assertEqual(self.snapshot(created).tasks[0].state, "cancelled")
        self.prompt.assert_not_called()
        self.assertEqual(self.app._classifications, {})

    def test_unsupported_host_cancel_before_fallback_does_not_start_direct_http(self):
        self.runtime._host_capabilities = {}
        created = self.root()
        native = self.runtime.classify_once
        def stop_at_unsupported(**kwargs):
            response = native(**kwargs)
            self.app.stop(self.room["id"], created["rootId"])
            return response
        with patch.object(self.runtime, "classify_once", side_effect=stop_at_unsupported):
            with self.assertRaises(DecisionUnavailable):
                self.choice(created)
        self.client.send.assert_not_called()
        self.assertEqual(self.app._classifications, {})

    def test_original_host_exit_reclaims_unknown_handle_without_another_root_stop(self):
        self.unknown_choice()
        self.client.running = False
        self.runtime._handle_host_exit(1, "offline exit", source_client=self.client)
        self.assertEqual(self.app._classifications, {})

    def test_runtime_stop_reclaims_unknown_handle_after_host_stop(self):
        self.unknown_choice()
        self.client.stop.side_effect = lambda: setattr(self.client, "running", False)
        self.runtime.stop()
        self.assertEqual(self.app._classifications, {})
        self.client.stop.assert_called_once()

    def test_stopping_old_root_does_not_cancel_successor_classification(self):
        old = self.unknown_choice()
        new = self.root(key="successor-classification")
        self.entered.clear()
        self.client.send.side_effect = self.send
        worker = self.worker(lambda: self.choice(new))
        try:
            self.assertTrue(self.entered.wait(10))
            self.app.stop(self.room["id"], old["rootId"])
            self.assertEqual(self.aborts, [self.exact_abort(self.requests[0])] * 2)
            self.settled_notice(self.requests[0])
            self.assertEqual(len(self.app._classifications), 1)
            self.assertEqual(self.runtime.runtime_status()["status"], "busy")
        finally:
            self.finish(worker)
        self.assertEqual(self.errors, [])
        self.assertEqual(self.app._classifications, {})

    def test_root_stop_uses_original_runtime_after_service_runtime_replacement(self):
        created = self.unknown_choice()
        replacement = Mock()
        with patch.object(self.service, "runtime", replacement):
            self.app.stop(self.room["id"], created["rootId"])
        replacement.cancel_classification.assert_not_called()
        self.assertEqual(self.aborts, [self.exact_abort(self.requests[0])] * 2)
        self.settled_notice(self.requests[0])

    def test_cancel_rpc_failure_keeps_truthful_pending_resource_and_original_identity(self):
        created = self.unknown_choice()
        with patch.object(self.runtime, "cancel_classification", side_effect=OSError("offline pipe failed")):
            receipt = self.app.stop(self.room["id"], created["rootId"])
        self.assertEqual(len(receipt["pendingClassifications"]), 1)
        self.assertEqual(receipt["pendingClassifications"][0]["requestId"], self.requests[0]["requestId"])
        self.assertEqual(len(self.app._classifications), 1)
        self.settled_notice(self.requests[0])

    def test_settlement_callback_failure_does_not_break_native_runtime_release(self):
        self.release.set()
        def failed_projection():
            raise OSError("offline observer failed")
        self.client.send.side_effect = lambda _method, params, **kwargs: (kwargs["before_write"]() or {
            **params, "stopReason": "stop", "answers": {},
        })
        result = self.runtime.classify_once(request_id="standalone", state={}, questions={
            "decision": {"type": "choice", "criteria": {"yes": "yes", "no": "no"}, "instructions": "choose"},
        }, on_settled=failed_projection)
        self.assertEqual(result["stopReason"], "stop")
        self.assertEqual(self.runtime._classifications, {})
