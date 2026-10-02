from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import ANY, Mock, patch

from rag_ime.agent_events import AgentEventHub
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.jev_tasks.decider import JevChoices
from rag_ime.jev_tasks.types import DecisionUnavailable
from rag_ime.pi.config import PiRuntimeConfig
from rag_ime.pi.runtime import PiRuntimeHostManager
from rag_ime.pi.values import PiRuntimeCommandAcceptanceUnknown, PiRuntimeError
from tests.sqlite_fixtures import copy_current_database


QUESTIONS = {"decision": {"type": "choice", "criteria": {"yes": "Yes", "no": "No"}, "instructions": "Choose"}}
ANSWER = {"type": "choice", "choice": "yes", "probabilities": {"yes": .9, "no": .1}, "confidence": .9}
RESULT = {"requestId": "decision-1", "dispatchId": "test-dispatch-id", "provider": "typesafe", "model": "jev-latest", "stopReason": "stop", "answers": {"decision": ANSWER}}


class NativeClassificationRuntimeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="paw-native-classifier-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        db = root / "state.sqlite"
        copy_current_database(db)
        store = AgentSessionStore(db)
        self.runtime = PiRuntimeHostManager(config=PiRuntimeConfig(
            enabled=True, executable=Path(__file__), agent_dir=root / "agent",
            session_dir=root / "sessions", logs_dir=root / "logs", idle_timeout_seconds=0,
            provider="openai-codex", model="gpt-6.1-sol", pi_version="1.0.0",
        ), sessions=store, events=AgentEventHub())
        self.addCleanup(self.runtime.stop)
        identity = patch("rag_ime.pi.runtime.uuid.uuid4", return_value="test-dispatch-id")
        identity.start()
        self.addCleanup(identity.stop)
        self.client = Mock(running=True)
        def send(_method, _params, **kwargs):
            if callback := kwargs.get("before_write"):
                callback()
            return RESULT
        self.client.send.side_effect = send
        self.runtime._client = self.client
        self.runtime._host_capabilities = {"statelessClassification": True}

    def set_outcomes(self, outcomes):
        outcomes = iter(outcomes)
        def send(_method, _params, **kwargs):
            if callback := kwargs.get("before_write"):
                callback()
            result = next(outcomes)
            if isinstance(result, BaseException):
                raise result
            return result
        self.client.send.side_effect = send

    def classify(self, **overrides):
        return self.runtime.classify_once(**{
            "request_id": "decision-1", "state": {"task": "inspect"}, "questions": QUESTIONS,
            "api_key": "dummy-private-key", "endpoint": "https://example.invalid/custom/classify",
            "timeout_seconds": 12, **overrides,
        })

    def test_native_request_preserves_context_and_private_transport_without_a_prompt(self):
        self.assertEqual(self.classify(), RESULT)
        self.client.send.assert_called_once_with("classification.once", {
            "requestId": "decision-1", "dispatchId": "test-dispatch-id", "state": {"task": "inspect"}, "questions": QUESTIONS,
            "apiKey": "dummy-private-key", "endpoint": "https://example.invalid/custom/classify", "timeoutMs": 12000,
        }, timeout=17.0, before_write=ANY)
        self.assertNotIn("dummy-private-key", json.dumps(self.runtime.runtime_status()))

    def test_unsupported_host_returns_none_before_classification_is_sent(self):
        self.runtime._host_capabilities = {}
        self.assertIsNone(self.classify())
        self.client.send.assert_not_called()

    def test_unknown_native_answer_aborts_only_original_request_and_remains_unknown(self):
        unknown = PiRuntimeCommandAcceptanceUnknown("Pi Runtime Host command timed out: classification.once")
        outcomes = iter([unknown, {"requestId": "decision-1", "dispatchId": "test-dispatch-id", "aborted": True, "active": True, "drained": True}])
        self.set_outcomes(outcomes)
        with self.assertRaises(PiRuntimeCommandAcceptanceUnknown):
            self.classify()
        self.assertEqual([call.args[0] for call in self.client.send.call_args_list], ["classification.once", "classification.abort"])
        self.assertEqual(self.client.send.call_args_list[1].args[1], {"requestId": "decision-1", "dispatchId": "test-dispatch-id"})
        self.client.stop.assert_not_called()
        self.assertIs(self.runtime._client, self.client)

    def test_pending_classification_prevents_idle_shutdown_and_duplicate_dispatch(self):
        entered, release = threading.Event(), threading.Event()
        results = []
        def send(method, params, **_kwargs):
            if method == "classification.once":
                _kwargs["before_write"]()
                entered.set()
                self.assertTrue(release.wait(5))
                return RESULT
            return {"requestId": "decision-1", "dispatchId": "test-dispatch-id", "aborted": True, "active": True, "drained": True}
        self.client.send.side_effect = send
        self.runtime.config = replace(self.runtime.config, idle_timeout_seconds=1)
        worker = threading.Thread(target=lambda: results.append(self.classify()))
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            self.assertEqual(self.runtime.runtime_status()["status"], "busy")
            with self.runtime._lock:
                self.runtime._schedule_idle_locked()
            self.assertIsNone(self.runtime._idle_timer)
            with self.assertRaisesRegex(PiRuntimeError, "already active"):
                self.classify()
            self.assertTrue(self.runtime.cancel_classification("decision-1"))
            self.assertFalse(self.runtime.cancel_classification("unrelated"))
            self.assertEqual(self.client.send.call_args_list[-1].args, ("classification.abort", {"requestId": "decision-1", "dispatchId": "test-dispatch-id"}))
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [RESULT])
        self.assertEqual(sum(call.args[0] == "classification.once" for call in self.client.send.call_args_list), 1)

    def test_lost_answer_keeps_host_busy_until_exact_abort_has_drained(self):
        self.runtime.config = replace(self.runtime.config, idle_timeout_seconds=30)
        self.set_outcomes([
            PiRuntimeCommandAcceptanceUnknown("lost answer"),
            {"requestId": "decision-1", "dispatchId": "test-dispatch-id", "aborted": True, "active": True, "drained": False},
            {"requestId": "decision-1", "dispatchId": "test-dispatch-id", "aborted": True, "active": False, "drained": True},
        ])
        with self.assertRaises(PiRuntimeCommandAcceptanceUnknown):
            self.classify()
        self.assertEqual(self.runtime.runtime_status()["status"], "busy")
        self.assertIsNone(self.runtime._idle_timer)
        self.assertTrue(self.runtime.cancel_classification("decision-1"))
        self.assertEqual(self.runtime.runtime_status()["status"], "ready")
        self.assertEqual(sum(call.args[0] == "classification.once" for call in self.client.send.call_args_list), 1)

    def test_abort_for_another_request_does_not_release_unknown_classification(self):
        self.set_outcomes([
            PiRuntimeCommandAcceptanceUnknown("lost answer"),
            {"requestId": "other", "aborted": True, "active": False, "drained": True},
        ])
        with self.assertRaises(PiRuntimeCommandAcceptanceUnknown):
            self.classify()
        self.assertEqual(self.runtime.runtime_status()["status"], "busy")

    def test_cancel_before_pipe_write_prevents_classification_admission(self):
        dispatched = []
        def send(method, params, **kwargs):
            if method == "classification.once":
                self.assertTrue(self.runtime.cancel_classification("decision-1"))
                kwargs["before_write"]()
                dispatched.append(method)
                return RESULT
            self.fail("An unsubmitted classification must not send an abort ahead of admission")
        self.client.send.side_effect = send
        with self.assertRaisesRegex(PiRuntimeError, "cancelled before dispatch"):
            self.classify()
        self.assertEqual(dispatched, [])
        self.assertEqual(self.runtime.runtime_status()["status"], "ready")

    def test_late_settlement_requires_original_host_and_dispatch_identity(self):
        self.set_outcomes([
            PiRuntimeCommandAcceptanceUnknown("lost answer"),
            {"requestId": "decision-1", "dispatchId": "test-dispatch-id", "aborted": True, "drained": False},
        ])
        with self.assertRaises(PiRuntimeCommandAcceptanceUnknown):
            self.classify()
        event = {"protocolVersion": "2", "event": "runtime.notice", "payload": {
            "type": "classification_settled", "requestId": "decision-1", "dispatchId": "test-dispatch-id",
        }}
        self.runtime._handle_host_event(event, source_client=Mock(running=True))
        self.assertEqual(self.runtime.runtime_status()["status"], "busy")
        self.runtime._handle_host_event({**event, "payload": {**event["payload"], "dispatchId": "old-dispatch"}},
                                        source_client=self.client)
        self.assertEqual(self.runtime.runtime_status()["status"], "busy")
        self.runtime._handle_host_event(event, source_client=self.client)
        self.assertEqual(self.runtime.runtime_status()["status"], "ready")

    def test_dead_origin_host_releases_pending_but_old_exit_cannot_clear_new_host(self):
        self.set_outcomes([
            PiRuntimeCommandAcceptanceUnknown("lost answer"),
            {"requestId": "decision-1", "dispatchId": "test-dispatch-id", "aborted": True, "drained": False},
        ])
        with self.assertRaises(PiRuntimeCommandAcceptanceUnknown):
            self.classify()
        self.runtime._handle_host_exit(1, "old unrelated exit", source_client=Mock(running=False))
        self.assertEqual(self.runtime.runtime_status()["status"], "busy")
        self.client.running = False
        self.runtime._handle_host_exit(1, "owned host exited", source_client=self.client)
        self.assertEqual(self.runtime.runtime_status()["status"], "faulted")
        self.assertFalse(self.runtime.cancel_classification("decision-1"))

    def test_delayed_old_exit_retires_only_old_host_classification(self):
        self.set_outcomes([
            PiRuntimeCommandAcceptanceUnknown("lost answer"),
            {"requestId": "decision-1", "dispatchId": "test-dispatch-id", "aborted": True, "drained": False},
        ])
        with self.assertRaises(PiRuntimeCommandAcceptanceUnknown):
            self.classify()
        old_client = self.client
        old_client.running = False
        new_client = Mock(running=True)
        self.runtime._client = new_client
        self.runtime._status = "ready"
        self.runtime._handle_host_exit(1, "old process exited", source_client=old_client)
        self.assertIs(self.runtime._client, new_client)
        self.assertEqual(self.runtime.runtime_status()["status"], "ready")
        self.assertFalse(self.runtime.cancel_classification("decision-1"))

    def test_invalid_timeout_or_non_object_context_is_rejected_before_host(self):
        for overrides in ({"timeout_seconds": float("nan")}, {"timeout_seconds": -1}, {"state": []}, {"questions": {}}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.classify(**overrides)
        self.client.send.assert_not_called()


class JevNativeClassificationAdapterTests(unittest.TestCase):
    def choose(self, runtime):
        return JevChoices.from_paw(runtime_provider=lambda: runtime).choose(
            {"task": "inspect"}, instructions="Choose", criteria=QUESTIONS["decision"]["criteria"],
        )

    def test_room_decider_uses_existing_runtime_and_preserves_choice_validation(self):
        runtime = Mock()
        runtime.classify_once.return_value = RESULT
        with patch("rag_ime.jev.api_key", return_value="dummy-private-key"), patch("rag_ime.jev.evaluate") as legacy:
            choice = self.choose(runtime)
        self.assertEqual(choice.choice, "yes")
        params = runtime.classify_once.call_args.kwargs
        self.assertEqual(params["state"], {"task": "inspect"})
        self.assertEqual(params["questions"], QUESTIONS)
        self.assertEqual(params["api_key"], "dummy-private-key")
        legacy.assert_not_called()

    def test_only_unsupported_runtime_uses_one_legacy_call(self):
        runtime = Mock()
        runtime.classify_once.return_value = None
        with patch("rag_ime.jev.api_key", return_value="dummy-private-key"), patch("rag_ime.jev.evaluate", return_value=RESULT) as legacy:
            self.assertEqual(self.choose(runtime).choice, "yes")
        legacy.assert_called_once()

    def test_revoked_classifier_key_does_not_use_stale_host_credentials(self):
        runtime = Mock()
        with patch("rag_ime.jev.api_key", return_value=""), patch("rag_ime.jev.evaluate") as legacy:
            with self.assertRaises(DecisionUnavailable):
                self.choose(runtime)
        runtime.classify_once.assert_not_called()
        legacy.assert_not_called()

    def test_native_error_abort_or_lost_answer_never_falls_back(self):
        for outcome in ({**RESULT, "stopReason": "error"}, {**RESULT, "stopReason": "aborted"},
                        PiRuntimeCommandAcceptanceUnknown("classification answer missing")):
            runtime = Mock()
            if isinstance(outcome, Exception):
                runtime.classify_once.side_effect = outcome
            else:
                runtime.classify_once.return_value = outcome
            with self.subTest(outcome=outcome), patch("rag_ime.jev.api_key", return_value="dummy-private-key"), patch("rag_ime.jev.evaluate") as legacy:
                with self.assertRaises(DecisionUnavailable):
                    self.choose(runtime)
                legacy.assert_not_called()

    def test_native_answer_still_requires_host_built_candidates_and_probabilities(self):
        runtime = Mock()
        runtime.classify_once.return_value = {**RESULT, "answers": {"decision": {**ANSWER, "choice": "invented"}}}
        with patch("rag_ime.jev.api_key", return_value="dummy-private-key"), patch("rag_ime.jev.evaluate") as legacy:
            with self.assertRaisesRegex(DecisionUnavailable, "unknown choice"):
                self.choose(runtime)
            legacy.assert_not_called()


class NativeClassificationConfigTests(unittest.TestCase):
    def config(self, **kwargs):
        return PiRuntimeConfig(enabled=True, executable=Path("/fake-host"), agent_dir=Path("/fake/agent"),
                               session_dir=Path("/fake/sessions"), logs_dir=Path("/fake/logs"), **kwargs)

    def test_classifier_credentials_are_resolved_fresh_for_each_owned_host(self):
        resolver = Mock(side_effect=[
            {"TYPESAFE_API_KEY": "dummy-first", "RAG_IME_PI_TYPESAFE_ENDPOINT": "https://example.invalid/custom"},
            {"TYPESAFE_API_KEY": "dummy-second", "RAG_IME_PI_TYPESAFE_ENDPOINT": "https://example.invalid/changed"},
            {},
        ])
        config = self.config(typesafe_environment_resolver=resolver,
                             provider_environment={"TYPESAFE_API_KEY": "stale-dummy", "KEEP": "other-provider"})
        resolver.assert_not_called()
        first, second, revoked = [config.child_environment() for _ in range(3)]
        self.assertEqual(first["TYPESAFE_API_KEY"], "dummy-first")
        self.assertEqual(second["TYPESAFE_API_KEY"], "dummy-second")
        self.assertEqual(second["RAG_IME_PI_TYPESAFE_ENDPOINT"], "https://example.invalid/changed")
        self.assertNotIn("TYPESAFE_API_KEY", revoked)
        self.assertNotIn("RAG_IME_PI_TYPESAFE_ENDPOINT", revoked)
        self.assertEqual(revoked["KEEP"], "other-provider")
        self.assertNotIn("stale-dummy", repr(config))

    def test_direct_config_does_not_read_user_keychain_or_ambient_classifier_key(self):
        with patch("rag_ime.jev.api_key") as key, patch.dict(os.environ, {"TYPESAFE_API_KEY": "ambient-dummy"}):
            self.assertNotIn("TYPESAFE_API_KEY", self.config().child_environment())
        key.assert_not_called()

    def test_production_config_binds_lazy_existing_credential_owner_and_full_endpoint(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "RAG_IME_APP_SUPPORT_DIR": directory, "RAG_IME_PI_EXECUTABLE": "/fake-host",
            "RAG_IME_PI_PROVIDER": "openai-codex", "RAG_IME_PI_MODEL": "gpt-6.1-sol",
            "TYPESAFE_API_URL": "https://example.invalid/full/systemone?version=1",
        }, clear=True), patch("rag_ime.jev.api_key", return_value="production-dummy") as key:
            config = PiRuntimeConfig.from_environment(system_proxy_default=False)
            key.assert_not_called()
            environment = config.child_environment()
            key.assert_called_once_with()
        self.assertEqual(environment["TYPESAFE_API_KEY"], "production-dummy")
        self.assertEqual(environment["RAG_IME_PI_TYPESAFE_ENDPOINT"], "https://example.invalid/full/systemone?version=1")
