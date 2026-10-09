from __future__ import annotations

import copy
import json
import os
import threading
import unittest
from unittest.mock import Mock, patch

from rag_ime.jev_tasks.decider import ABSTAIN, JevChoices, NativeClassification
from rag_ime.jev_tasks.openai_decisions import MODEL, choice_request, choice_response, post_decision
from rag_ime.jev_tasks.types import Candidate, DecisionUnavailable, GraphError
from tests.test_jev_host_application import JevHostFixture


QUESTIONS = {"route": {"type": "choice", "instructions": "Choose an existing ID using only this evidence.",
    "criteria": {"opaque-a": "Known route A", "opaque-b": "Known route B"}}}


def answer(name="route", selected="opaque-a", values=None):
    values = values or {"opaque-a": 0.9, "opaque-b": 0.1}
    return {"model": MODEL, "answers": [{"type": "choice", "name": name, "choice": selected,
        "confidence": 0.8, "probabilities": [{"value": key, "probability": value} for key, value in values.items()]}]}


class DecisionsContractTests(unittest.TestCase):
    def test_request_preserves_evidence_and_opaque_options(self):
        state = '{"evidence":"原始材料","tool_args":"untrusted source data"}'
        packet = choice_request(state, QUESTIONS, max_request_bytes=96000)
        self.assertEqual(packet, {"model": MODEL, "input": state, "questions": [{"name": "route", "type": "choice",
            "instructions": QUESTIONS["route"]["instructions"], "choices": [
                {"value": "opaque-a", "description": "Known route A"}, {"value": "opaque-b", "description": "Known route B"}]}]})
        self.assertEqual(choice_response(answer(), QUESTIONS)["answers"]["route"]["probabilities"], {"opaque-a": 0.9, "opaque-b": 0.1})

    def test_invalid_provider_answers_never_become_a_choice(self):
        invalid = []
        for mutate in [
            lambda r: r.update(model="other-model"),
            lambda r: r.update(answers=[]),
            lambda r: r["answers"].append(copy.deepcopy(r["answers"][0])),
            lambda r: r["answers"][0].update(name="unknown"),
            lambda r: r["answers"][0].update(name=None),
            lambda r: r["answers"][0].update(type="refusal"),
            lambda r: r["answers"][0].update(type="predicate"),
            lambda r: r["answers"][0].update(choice="invented-action"),
            lambda r: r["answers"][0].update(choice=True),
            lambda r: r["answers"][0].update(choice="opaque-b"),
            lambda r: r["answers"][0].update(confidence=float("nan")),
            lambda r: r["answers"][0].update(confidence=True),
            lambda r: r["answers"][0]["probabilities"].pop(),
            lambda r: r["answers"][0]["probabilities"].append({"value": "opaque-a", "probability": 0.0}),
            lambda r: r["answers"][0]["probabilities"][0].update(value="unknown"),
            lambda r: r["answers"][0]["probabilities"][0].update(probability=0.6),
            lambda r: r["answers"][0]["probabilities"][0].update(probability=float("inf")),
            lambda r: r["answers"][0]["probabilities"][0].update(probability=True),
        ]:
            response = answer(); mutate(response); invalid.append(response)
        for response in invalid:
            with self.subTest(response=response):
                with self.assertRaises(DecisionUnavailable):
                    choice_response(response, QUESTIONS)

    def test_environment_opt_in_reads_only_api_key_and_never_calls_type_safe(self):
        with patch.dict(os.environ, {"RAG_IME_JEV_DECISION_PROVIDER": "openai-decisions", "OPENAI_API_KEY": "offline-placeholder"}), \
                patch("rag_ime.jev.api_key", side_effect=AssertionError("TypeSafe auth forbidden")), \
                patch("rag_ime.jev.evaluate", side_effect=AssertionError("fallback forbidden")), \
                patch("rag_ime.jev_tasks.openai_decisions.post_decision", return_value=answer()) as transport:
            choices = JevChoices.from_paw()
            transport.assert_not_called()
            result = choices.choose({"known": "evidence"}, instructions=QUESTIONS["route"]["instructions"], criteria=QUESTIONS["route"]["criteria"], question_id="route")
            self.assertEqual(result.choice, "opaque-a")
            self.assertEqual(result.model, MODEL)
            self.assertEqual(result.probability, 0.9)
            self.assertEqual(transport.call_count, 1)

    def test_missing_api_key_fails_before_transport_even_with_codex_or_type_safe_auth(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "", "TYPESAFE_API_KEY": "offline-unused"}), \
                patch("rag_ime.jev_tasks.openai_decisions.post_decision") as transport:
            with self.assertRaises(DecisionUnavailable) as caught:
                JevChoices.from_openai_decisions().choose({}, instructions="route", criteria=QUESTIONS["route"]["criteria"])
            self.assertEqual(str(caught.exception.__cause__), "OpenAI Decisions key is not configured")
            transport.assert_not_called()

    def test_thresholds_and_explicit_abstention_remain_application_policy(self):
        candidate = Candidate.make("dispatch", "task", "Known legal action", {})
        values = {candidate.id: 0.55, ABSTAIN: 0.45}
        with patch.dict(os.environ, {"OPENAI_API_KEY": "offline-placeholder"}), \
                patch("rag_ime.jev_tasks.openai_decisions.post_decision", return_value=answer("decision", candidate.id, values)):
            selected, result = JevChoices.from_openai_decisions().choose_action({}, [candidate])
            self.assertIsNone(selected); self.assertEqual(result.choice, candidate.id)
        values = {candidate.id: 0.1, ABSTAIN: 0.9}
        with patch.dict(os.environ, {"OPENAI_API_KEY": "offline-placeholder"}), \
                patch("rag_ime.jev_tasks.openai_decisions.post_decision", return_value=answer("decision", ABSTAIN, values)):
            selected, result = JevChoices.from_openai_decisions().choose_action({}, [candidate], min_probability=0, min_margin=0)
            self.assertIsNone(selected); self.assertEqual(result.choice, ABSTAIN)

    def test_budget_and_pre_cancelled_root_do_not_admit_http(self):
        call = NativeClassification(object(), "owned-call", threading.Event(), Mock())
        call.cancellation_event.set()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "offline-placeholder"}), \
                patch("rag_ime.jev_tasks.openai_decisions.post_decision") as transport:
            with self.assertRaises(DecisionUnavailable):
                JevChoices.from_openai_decisions().choose({}, instructions="route", criteria=QUESTIONS["route"]["criteria"], classification=call)
            with self.assertRaises(DecisionUnavailable):
                JevChoices.from_openai_decisions(max_request_bytes=1024).choose({"source": "long" * 1000}, instructions="route", criteria=QUESTIONS["route"]["criteria"])
            transport.assert_not_called()

    def test_official_http_transport_is_bounded_and_errors_are_redacted(self):
        response = Mock(status=200)
        response.read.return_value = json.dumps(answer()).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock(); opener.open.return_value = response
        with patch("urllib.request.build_opener", return_value=opener):
            self.assertEqual(post_decision({"model": MODEL}, key="offline-placeholder", timeout_seconds=3), answer())
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.openai.com/v1/decisions")
        self.assertEqual(request.method, "POST")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 3)
        with patch("urllib.request.build_opener", side_effect=RuntimeError("provider body and credential should not appear")):
            with self.assertRaises(DecisionUnavailable) as caught:
                post_decision({"model": MODEL}, key="offline-placeholder", timeout_seconds=3)
        self.assertEqual(str(caught.exception), "OpenAI Decisions transport unavailable")
        self.assertIsNone(caught.exception.__cause__)

    def test_invalid_timeout_and_provider_are_rejected_without_calls(self):
        with self.assertRaises(GraphError):
            JevChoices.from_openai_decisions(timeout_seconds=float("nan"))
        with patch.dict(os.environ, {"RAG_IME_JEV_DECISION_PROVIDER": "unsupported"}):
            with self.assertRaises(GraphError):
                JevChoices.from_paw()


class DecisionsRootLifecycleTests(JevHostFixture):
    def setUp(self):
        super().setUp()
        self.app.driver.controller.decider = JevChoices.from_openai_decisions()
        env = patch.dict(os.environ, {"OPENAI_API_KEY": "offline-placeholder"})
        env.start(); self.addCleanup(env.stop)

    def test_missing_key_is_configuration_missing_and_never_dispatches(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}), patch("rag_ime.jev_tasks.openai_decisions.post_decision") as transport:
            created = self.create("decisions-missing-key")
            self.app.tick(limit=1)
            view = self.app.projection(self.room["id"], created["graphId"])
            self.assertIn("configuration_missing", str(view["events"]))
            self.prompt.assert_not_called(); transport.assert_not_called()
            self.assertEqual(self.app._classifications, {})

    def test_stop_during_direct_decision_retains_pending_until_actual_return(self):
        entered, release = threading.Event(), threading.Event()
        errors = []
        created = self.create("decisions-stop")

        def evaluate(packet, **kwargs):
            entered.set()
            if not release.wait(10):
                raise TimeoutError("offline Decisions barrier")
            question = packet["questions"][0]
            selected = question["choices"][0]["value"]
            return answer(question["name"], selected, {choice["value"]: 1.0 if choice["value"] == selected else 0.0 for choice in question["choices"]})

        def run():
            try:
                self.app.tick(limit=1)
            except Exception as exc:
                errors.append(exc)

        with patch("rag_ime.jev_tasks.openai_decisions.post_decision", side_effect=evaluate) as transport, \
                patch("rag_ime.jev.evaluate", side_effect=AssertionError("fallback forbidden")):
            worker = threading.Thread(target=run, name="offline-decisions-stop")
            worker.start()
            try:
                self.assertTrue(entered.wait(10))
                stopped = self.app.stop(self.room["id"], created["rootId"])
                self.assertEqual(stopped["status"], "cancellation_pending")
                self.assertEqual(len(stopped["pendingClassifications"]), 1)
                self.prompt.assert_not_called()
            finally:
                release.set(); worker.join(10)
            self.assertFalse(worker.is_alive())
            self.assertEqual(transport.call_count, 1)
        self.assertEqual(errors, [])
        self.assertEqual(self.app._classifications, {})
        self.assertTrue(self.app.projection(self.room["id"], created["graphId"])["stopped"])
        self.prompt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
