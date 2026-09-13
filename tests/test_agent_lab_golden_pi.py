from __future__ import annotations

import json
import tempfile
import threading
import unittest
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rag_ime.agent_lab.golden_pi import AgentLabGoldenPiExecutor, GoldenPiCallError, normalize_golden_usage
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.pi.values import PiRuntimeSettlementLookupTimeout


class Runtime:
    def __init__(self) -> None:
        self.calls = []
        self.waits = []
        self.fail_admission = False
        self.fail_lookup = False
        self.wrong_identity = False
        self.failed = False
        self.aborts = []

    def set_model(self, session_id, *, provider, model_id):
        return {"selected": {"provider": provider, "id": model_id}}

    def set_thinking_level(self, session_id, *, level):
        return {"thinkingLevel": level}

    def prompt(self, session_id, message, *, client_message_id, images):
        self.calls.append((session_id, message, client_message_id))
        if self.fail_admission:
            raise TimeoutError("unknown admission")
        return {"accepted": True, "turnId": "turn-1"}

    def session_snapshot(self, session_id):
        return {"activeTurn": None}

    def await_turn_settled(self, session_id, turn_id, *, client_message_id, timeout_seconds):
        self.waits.append((session_id, turn_id, client_message_id))
        if self.fail_lookup:
            raise TimeoutError("lookup unavailable")
        return {
            "schemaVersion": "rag-ime.pi-turn-settlement.v1",
            "sessionId": "wrong" if self.wrong_identity else session_id,
            "turnId": turn_id, "clientMessageId": client_message_id,
            "runtimeSessionId": "pi-session",
            "receipt": {
                "schemaVersion": "pi.agent-settled.v2", "sessionId": "pi-session",
                "disposition": "failed" if self.failed else "completed",
                "aborted": False, "pendingOperations": 0,
                "finalMessage": {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "never expose this"},
                    {"type": "text", "text": '{"cases":[]}'},
                ], "usage": {"input": 100, "output": 20}},
            },
        }

    def abort(self, session_id):
        self.aborts.append(session_id)
        return {"ok": True}


class GoldenPiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "golden.sqlite"
        self.sessions = AgentSessionStore(self.path)
        self.sessions.initialize()
        self.addCleanup(self.sessions.close)
        self.runtime = Runtime()
        self.executor = AgentLabGoldenPiExecutor(self.path, sessions=self.sessions, runtime=lambda: self.runtime)
        self.model = {"provider": "openai-codex", "model": "gpt-6-astra", "thinkingLevel": "high", "prompt": ""}

    def complete(self, **kwargs):
        return self.executor.complete(request_id="job:draft", model=self.model, prompt="draft grounded cases", on_session=lambda _sid: None, cancelled=lambda: False, **kwargs)

    def test_owned_pi_session_and_exact_terminal_receipt_are_persisted(self):
        result = self.complete()
        self.assertEqual(result["text"], '{"cases":[]}')
        session = self.sessions.get(result["sessionId"])
        self.assertEqual(session["ownerAppId"], "extension:agent-lab")
        self.assertFalse(session["projectContextEnabled"])
        self.assertEqual(session['allowedTools'], [])
        self.assertEqual(result["usage"], {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120})
        self.assertNotIn("never expose this", json.dumps(result))
        self.assertEqual(self.complete(), result)
        self.assertEqual(len(self.runtime.calls), 1)

    def test_research_app_session_grants_only_its_bound_tool(self):
        self.executor.session_identity = lambda _: {'owner_app_id': 'extension:lab-test',
            'surface_key': 'application.call-one', 'allowed_tools': ['lab_research']}
        result = self.complete()
        session = self.sessions.get(result['sessionId'])
        self.assertEqual(session['allowedTools'], ['lab_research'])
        self.assertEqual(session['toolAllowlistMode'], 'explicit')
        self.assertEqual(session['workspaceRoots'], [])
        self.assertFalse(session['piSkillsEnabled'])

    def test_arbitrary_tool_grants_fail_before_pi_admission(self):
        self.executor.session_identity = lambda _: {'allowed_tools': ['knowledge']}
        with self.assertRaises(GoldenPiCallError):
            self.complete()
        self.assertEqual(self.runtime.calls, [])

    def test_public_research_tool_identity_does_not_copy_source_text_or_budget_claims(self):
        updates = []
        def replay(session_id, *, after_event_id=''):
            if after_event_id: return [], False
            def event(identity, kind, payload, turn='turn-1'):
                return SimpleNamespace(event_id=identity, session_id=session_id, turn_id=turn, event_type=kind, payload=payload)
            payload = {'toolName': 'lab_research', 'toolCallId': 'read-one',
                       'args': {'op': 'open', 'query': 'SECRET_QUERY'},
                       'result': {'details': {'sources': ['SECRET_SOURCE'], 'budget': {'claims': 'SECRET_BUDGET'}}}}
            return [event('1', 'tool_started', payload), event('2', 'tool_finished', payload),
                    event('3', 'tool_finished', {**payload, 'toolCallId': 'read-failed', 'isError': True}),
                    event('4', 'tool_started', {**payload, 'toolCallId': 'foreign'}, turn='foreign')], False
        self.runtime.events = SimpleNamespace(replay=replay)
        result = self.complete(on_progress=updates.append)
        tools = [row for row in updates if row.get('stage') == 'researching']
        self.assertEqual([row['runtime']['status'] for row in tools], ['running', 'completed', 'failed'])
        self.assertEqual(tools[1]['runtime'], {'sessionId': result['sessionId'], 'turnId': 'turn-1',
            'toolCallId': 'read-one', 'toolName': 'lab_research', 'operation': 'open', 'status': 'completed'})
        self.assertNotIn('SECRET_', json.dumps(updates))
        self.assertFalse(any('research' in row for row in updates))

    def test_public_app_progress_follows_exact_turn_without_private_reasoning_or_new_admission(self):
        updates = []
        replayed = []
        def replay(session_id, *, after_event_id=''):
            replayed.append(after_event_id)
            if after_event_id: return [], False
            def event(identity, kind, payload, turn='turn-1'):
                return SimpleNamespace(event_id=identity, session_id=session_id, turn_id=turn, event_type=kind, payload=payload)
            return [event('1','text_delta',{'delta':'foreign output'},'another-turn'),
                    event('2','status_changed',{'phase':'reasoning','private':'never expose this'}),
                    event('3','text_delta',{'blockId':'answer','delta':'First '}),
                    event('4','text_delta',{'blockId':'answer','delta':'answer'})], False
        self.runtime.events = SimpleNamespace(replay=replay)
        result = self.complete(on_progress=updates.append)
        self.assertEqual(updates[0]['stage'],'model_wait')
        self.assertIn({'stage':'thinking'},updates)
        self.assertEqual(updates[-1],{'stage':'answering','text':'First answer'})
        self.assertNotIn('never expose this',json.dumps(updates))
        self.assertNotIn('foreign output',json.dumps(updates))
        self.assertEqual(result['text'],'{"cases":[]}')
        self.assertEqual(replayed,['','4'])
        self.assertEqual(len(self.runtime.calls),1)

    def test_missing_stream_tail_and_broken_feedback_never_settle_or_interrupt_the_turn(self):
        updates = []
        self.runtime.events = SimpleNamespace(replay=lambda *a, **kw:([],True))
        self.assertEqual(self.complete(on_progress=updates.append)['turnId'],'turn-1')
        self.assertIn({'streamPartial':True,'text':''},updates)
        def broken(_): raise OSError('projection unavailable')
        self.assertEqual(self.executor.complete(request_id='second',model=self.model,prompt='second',
            on_session=lambda _:None,cancelled=lambda:False,on_progress=broken)['turnId'],'turn-1')
        self.assertEqual(self.runtime.aborts,[])

    def test_restart_recovers_original_accepted_turn_without_second_prompt(self):
        self.runtime.fail_lookup = True
        with self.assertRaises(GoldenPiCallError):
            self.complete()
        self.runtime.fail_lookup = False
        self.executor = AgentLabGoldenPiExecutor(self.path, sessions=self.sessions, runtime=lambda: self.runtime)
        self.assertEqual(self.complete()["turnId"], "turn-1")
        self.assertEqual(len(self.runtime.calls), 1)
        self.assertEqual(len(self.runtime.waits), 2)

    def test_unknown_admission_is_not_blindly_replayed(self):
        self.runtime.fail_admission = True
        with self.assertRaises(GoldenPiCallError):
            self.complete()
        self.runtime.fail_admission = False
        with self.assertRaises(GoldenPiCallError):
            self.complete()
        self.assertEqual(len(self.runtime.calls), 1)

    def test_request_cannot_be_rebound_to_new_prompt(self):
        self.complete()
        with self.assertRaises(GoldenPiCallError):
            self.executor.complete(request_id="job:draft", model=self.model, prompt="different", on_session=lambda _sid: None, cancelled=lambda: False)
        self.assertEqual(len(self.runtime.calls), 1)

    def test_wrong_or_failed_settlement_never_becomes_success(self):
        for attr in ("wrong_identity", "failed"):
            with self.subTest(attr=attr):
                setattr(self.runtime, attr, True)
                with self.assertRaises(GoldenPiCallError):
                    self.executor.complete(request_id=f"job:{attr}", model=self.model, prompt="answer", on_session=lambda _sid: None, cancelled=lambda: False)
                setattr(self.runtime, attr, False)

    def test_cancelled_before_admission_does_not_call_provider(self):
        with self.assertRaises(GoldenPiCallError):
            self.executor.complete(request_id="cancelled", model=self.model, prompt="answer", on_session=lambda _sid: None, cancelled=lambda: True)
        self.assertEqual(self.runtime.calls, [])

    def test_catalog_cost_is_an_estimate_and_missing_cost_stays_missing(self):
        usage = normalize_golden_usage({"input": 10, "output": 4, "cacheRead": 6, "cost": {"total": 0.003}})
        self.assertEqual(usage["totalTokens"], 20)
        self.assertEqual(usage["estimatedCostUsd"], 0.003)
        self.assertEqual(usage["costBasis"], "model_catalog_estimate")
        self.assertNotIn("costUsd", usage)
        self.assertNotIn("costUsd", normalize_golden_usage({"input": 1}))

    def test_default_zero_catalog_price_is_unknown_but_explicit_reported_zero_is_preserved(self):
        unknown = normalize_golden_usage({'input':20,'output':5,'cost':{'total':0}})
        self.assertNotIn('estimatedCostUsd', unknown)
        self.assertEqual(unknown['costBasis'], 'unavailable')
        self.assertEqual(unknown['totalTokens'], 25)
        reported = normalize_golden_usage({'input':20,'output':5,'costUsd':0})
        self.assertEqual(reported['costUsd'],0)
        self.assertEqual(reported['costBasis'],'runtime_reported')

    def test_failed_settlement_keeps_real_usage_and_replays_failure_receipt(self):
        self.runtime.failed = True
        with self.assertRaises(GoldenPiCallError) as first:
            self.complete()
        completion = first.exception.completion
        self.assertEqual(completion["usage"], {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120})
        self.assertEqual(completion["receipt"]["status"], "failed")
        self.assertEqual(completion["receipt"]["requestId"], "job:draft")
        self.assertEqual(completion["receipt"]["settlementReceiptId"], "")
        self.assertEqual(completion["turnId"], "turn-1")
        self.assertNotIn("never expose this", json.dumps(completion))
        row = self.executor._row("job:draft")
        self.assertEqual(row["state"], "failed")
        self.assertEqual(json.loads(row["receipt_json"]), completion["receipt"])
        with self.assertRaises(GoldenPiCallError) as replay:
            self.complete()
        self.assertEqual(replay.exception.completion, completion)
        self.assertEqual(len(self.runtime.calls), 1)
        self.assertEqual(len(self.runtime.waits), 1)

    def test_wrong_identity_does_not_manufacture_failure_completion(self):
        self.runtime.wrong_identity = True
        with self.assertRaises(GoldenPiCallError) as failed:
            self.complete()
        self.assertIsNone(getattr(failed.exception, "completion", None))
        self.assertEqual(json.loads(self.executor._row("job:draft")["receipt_json"]), {})

    def test_late_observer_timeout_reuses_completed_call_across_executor_instances(self):
        first_wait = threading.Event()
        second_wait = threading.Event()
        first_completed = threading.Event()

        class RacingRuntime(Runtime):
            def __init__(self):
                super().__init__()
                self.lock = threading.Lock()
                self.wait_count = 0

            def await_turn_settled(self, session_id, turn_id, *, client_message_id, timeout_seconds):
                with self.lock:
                    index = self.wait_count
                    self.wait_count += 1
                if index == 0:
                    first_wait.set()
                    if not second_wait.wait(5):
                        raise TimeoutError("second observer did not enter")
                    return super().await_turn_settled(session_id, turn_id, client_message_id=client_message_id, timeout_seconds=timeout_seconds)
                second_wait.set()
                if not first_completed.wait(5):
                    raise TimeoutError("first observer did not complete")
                raise TimeoutError("late observer timeout")

        self.runtime = RacingRuntime()
        duplicate = AgentLabGoldenPiExecutor(self.path, sessions=self.sessions, runtime=lambda: self.runtime)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.complete)
            self.assertTrue(first_wait.wait(5))
            second = pool.submit(duplicate.complete, request_id="job:draft", model=self.model, prompt="draft grounded cases", on_session=lambda _: None, cancelled=lambda: False)
            try:
                result = first.result(5)
                self.assertEqual(self.executor._row("job:draft")["state"], "completed")
            finally:
                first_completed.set()
            self.assertEqual(second.result(5), result)
        self.assertEqual(self.executor._row("job:draft")["state"], "completed")
        self.assertEqual(self.complete(), result)
        self.assertEqual(len(self.runtime.calls), 1)

    def test_short_settlement_wait_rechecks_same_turn_without_another_prompt(self):
        original = self.runtime.await_turn_settled
        waits = []

        def deferred(session_id, turn_id, *, client_message_id, timeout_seconds):
            waits.append((session_id, turn_id, client_message_id, timeout_seconds))
            if len(waits) == 1:
                raise TimeoutError("Pi Session turn settlement timed out")
            return original(session_id, turn_id, client_message_id=client_message_id, timeout_seconds=timeout_seconds)

        self.runtime.await_turn_settled = deferred
        result = self.complete()
        self.assertEqual(len(waits), 2)
        self.assertEqual(waits[0][:3], waits[1][:3])
        self.assertTrue(all(0 < wait[3] <= 5 for wait in waits))
        self.assertEqual(result["turnId"], "turn-1")
        self.assertEqual(len(self.runtime.calls), 1)

    def test_typed_lookup_timeout_continues_the_original_paid_turn_automatically(self):
        original = self.runtime.await_turn_settled
        identities = []

        def delayed(session_id, turn_id, *, client_message_id, timeout_seconds):
            identities.append((session_id, turn_id, client_message_id))
            if len(identities) < 3:
                raise PiRuntimeSettlementLookupTimeout('Pi Session settlement lookup timed out')
            return original(session_id, turn_id, client_message_id=client_message_id, timeout_seconds=timeout_seconds)

        self.runtime.await_turn_settled = delayed
        result = self.complete()
        self.assertEqual(result['turnId'], 'turn-1')
        self.assertEqual(len(identities), 3)
        self.assertEqual(len(set(identities)), 1)
        self.assertEqual(len(self.runtime.calls), 1)
        self.assertEqual(self.runtime.aborts, [])
        self.assertEqual(self.complete(), result)

    def test_lookup_read_recovery_still_honors_stop_without_replaying_the_prompt(self):
        stopped = False

        def delayed(*args, **kwargs):
            nonlocal stopped
            stopped = True
            raise PiRuntimeSettlementLookupTimeout('Pi Session settlement lookup timed out')

        self.runtime.await_turn_settled = delayed
        with self.assertRaises(GoldenPiCallError) as raised:
            self.executor.complete(request_id='stop-after-lookup', model=self.model,
                prompt='original paid request', on_session=lambda _: None, cancelled=lambda: stopped)
        self.assertFalse(raised.exception.interrupted)
        self.assertEqual(len(self.runtime.calls), 1)
        self.assertEqual(len(self.runtime.aborts), 1)
        self.assertEqual(self.executor._row('stop-after-lookup')['state'], 'cancelled')

    def test_completed_result_is_read_only_and_does_not_bind_a_changed_template(self):
        self.assertIsNone(self.executor.completed_result("not-admitted", self.model))
        original = self.complete()
        self.executor.runtime = lambda: self.fail("completed_result must not touch the Runtime")
        self.assertEqual(self.executor.completed_result("job:draft", self.model), original)
        self.assertEqual(len(self.runtime.calls), 1)
        with self.assertRaises(GoldenPiCallError):
            self.executor.completed_result("job:draft", {**self.model, "model": "different-model"})
        # The explicit cache API does not weaken ordinary exact-request replay.
        with self.assertRaises(GoldenPiCallError):
            self.executor.complete(request_id="job:draft", model=self.model, prompt="changed generation template", on_session=lambda _: None, cancelled=lambda: False)

    def test_completed_result_does_not_return_failed_or_unknown_work_as_success(self):
        self.runtime.failed = True
        with self.assertRaises(GoldenPiCallError):
            self.complete()
        self.assertIsNone(self.executor.completed_result("job:draft", self.model))
        self.runtime.failed = False
        self.runtime.fail_lookup = True
        with self.assertRaises(GoldenPiCallError):
            self.executor.complete(request_id="unfinished", model=self.model, prompt="task", on_session=lambda _: None, cancelled=lambda: False)
        self.assertIsNone(self.executor.completed_result("unfinished", self.model))


if __name__ == "__main__":
    unittest.main()
