"""Real Jev stores consume exact Host recovery and causal-owner drain proofs.

Pi admission and the Host recovery RPC are explicit doubles; no live turn or
installed database is touched. WorkItem/claim transitions use their real owners.
"""
from __future__ import annotations

import json
from copy import deepcopy
from unittest.mock import patch

from tests import test_jev_host_application as host


class JevRecoveryFixture(host.JevHostFixture):
    def setUp(self):
        super().setUp()
        self.created = self.create()
        self.app.tick()
        self.effect = next(item for item in self.app.projection(
            self.room["id"], self.created["graphId"])["effects"] if item["operation"] == "dispatch")
        request, admitted = self.effect["request"], self.effect["receipt"]
        self.recovered = False
        self.settlement = {"schemaVersion": "rag-ime.pi-turn-settlement.v1",
            "sessionId": request["sessionId"], "turnId": admitted["turnId"],
            "clientMessageId": self.effect["effectId"], "runtimeSessionId": "native-session",
            "receipt": {"schemaVersion": "pi.agent-settled.v2", "sessionId": "native-session",
                "runId": admitted["turnId"], "scopeId": "native-session:" + admitted["turnId"],
                "receiptId": "recovered-proof", "disposition": "aborted", "aborted": True,
                "pendingOperations": 0, "operations": {"pending": 0},
                "continuations": {"pendingIds": [], "readyIds": [], "scheduledIds": [],
                    "leasedIds": [], "counts": {"pending": 0, "leased": 0}}}}
        self.receipt = {"schemaVersion": "rag-ime.pi-exact-turn-cancel.v1",
            "sessionId": request["sessionId"], "turnId": admitted["turnId"],
            "clientMessageId": self.effect["effectId"],
            "cancelId": "jev-retired-recovery:" + self.effect["effectId"],
            "receiptId": "exact-recovery-proof", "state": "accepted", "phase": "settled",
            "runtimeReceipt": {"schemaVersion": "rag-ime.pi-session-abort-receipt.v1",
                "sessionId": request["sessionId"], "turnId": admitted["turnId"], "lifecycle": {
                    "schemaVersion": "pi.agent-abort-receipt.v1", "reason": "retired_turn_recovery",
                    "idle": True, "drained": True, "operations": [], "pendingOperations": [],
                    "failedOperationIds": [], "cancelledContinuationIds": []}}}

        def lookup(*args, **kwargs):
            if not self.recovered:
                raise TimeoutError("retired turn has no settlement yet")
            return self.settlement

        def recover(*args, **kwargs):
            self.assertEqual(args, (request["sessionId"], admitted["turnId"]))
            self.assertEqual(kwargs, {"client_message_id": self.effect["effectId"],
                "cancel_id": self.receipt["cancelId"], "recover_retired_only": True})
            self.recovered = True
            return self.receipt

        for name, kwargs in (("is_turn_active", {"return_value": False}),
                             ("await_turn_settled", {"side_effect": lookup}),
                             ("abort_turn", {"side_effect": recover}),
                             ("abort", {"side_effect": AssertionError("broad abort forbidden")})):
            p = patch.object(self.service.runtime, name, **kwargs)
            setattr(self, name, p.start())
            self.addCleanup(p.stop)

    def claims(self):
        with self.app.ledger.connection() as conn:
            return conn.execute("SELECT effect_id FROM agent_jev_executor_claims WHERE graph_id=?",
                                (self.created["graphId"],)).fetchall()

    def reconcile(self):
        return self.app.command(self.room["id"], {"action": "reconcile",
            "graphId": self.created["graphId"], "clientMessageId": "test-exact-recovery"})


class JevRetiredRecoveryTests(JevRecoveryFixture):
    def test_public_reconcile_recovers_exact_retired_attempt_without_stopping_root(self):
        self.app.projection(self.room["id"], self.created["graphId"])
        self.abort_turn.assert_not_called()  # GET remains read-only.
        self.assertEqual(len(self.claims()), 1)
        self.reconcile()
        self.abort_turn.assert_called_once()
        self.assertEqual(self.claims(), [])
        task = self.snapshot(self.created).task(self.effect["request"]["taskId"])
        self.assertEqual(task.state, "failed")  # An interrupted result is never accepted.
        policy = self.app.lifecycle.policy(self.created["graphId"])
        self.assertFalse(policy["stopped"])
        self.assertEqual(json.loads(policy["final_json"]), {})
        self.assertEqual(self.app.execution_status(self.effect), "drained")
        self.assertEqual(self.service.room_turns.active_turn(self.effect["request"]["sessionId"]), ("", ""))
        self.reconcile()
        self.abort_turn.assert_called_once()  # Consume persisted drain on replay.

    def test_new_turn_or_unavailable_recovery_keeps_claim_and_work(self):
        before = self.snapshot(self.created).task(self.effect["request"]["taskId"])
        self.abort_turn.side_effect = None
        self.abort_turn.return_value = {"state": "rejected", "reason": "newer_turn"}
        self.reconcile()
        self.assertEqual(len(self.claims()), 1)
        self.assertEqual(self.snapshot(self.created).task(before.id), before)
        self.abort.assert_not_called()

    def test_recovered_settlement_still_requires_causal_descendant_drain(self):
        before = self.snapshot(self.created).task(self.effect["request"]["taskId"])
        with patch("rag_ime.jev_tasks.runtime_adapter.causal_descendants_proof",
                   return_value={"settled": False, "pending": ["background_job:still-running"]}):
            self.reconcile()
        self.assertTrue(self.recovered)
        self.assertEqual(len(self.claims()), 1)
        self.assertEqual(self.snapshot(self.created).task(before.id), before)
        self.assertIsNone(self.app.execution_terminal(self.effect, lookup=False))

    def test_known_live_turn_is_never_sent_to_retired_recovery(self):
        self.is_turn_active.return_value = True
        self.reconcile()
        self.abort_turn.assert_not_called()
        self.assertEqual(len(self.claims()), 1)

    def test_old_settlement_removes_only_its_room_mapping_when_session_has_new_turn(self):
        self.recovered = True  # Already persisted: lookup needs no recovery RPC.
        session = self.effect["request"]["sessionId"]
        self.service.room_turns.begin(session, "new-root", dispatch_id="new-dispatch")
        self.service.room_turns.accept(session, "new-turn", "new-root")
        self.reconcile()
        self.assertEqual(self.service.room_turns.active_turn(session), ("new-root", "new-dispatch"))
        self.assertEqual(self.service.room_turns.turn_by_session_turn[(session, "new-turn")], "new-root")
        self.assertNotIn((session, self.effect["receipt"]["turnId"]), self.service.room_turns.turn_by_session_turn)
        self.abort_turn.assert_not_called()


class JevInterruptedRecoveryTests(JevRecoveryFixture):
    def setUp(self):
        super().setUp()
        self.cold = True
        self.retirement_phase = "settled"
        self.recovery_calls = []
        def recover(session_id, turn_id, **kwargs):
            self.assertEqual((session_id, turn_id), (self.receipt['sessionId'], self.receipt['turnId']))
            self.assertEqual(kwargs['client_message_id'], self.effect['effectId'])
            self.recovery_calls.append(dict(kwargs))
            if kwargs.get('recover_interrupted_only'):
                receipt = deepcopy(self.receipt)
                receipt['cancelId'] = 'jev-interrupted-recovery:' + self.effect['effectId']
                self.assertEqual(kwargs['cancel_id'], receipt['cancelId'])
                receipt['phase'] = self.retirement_phase
                receipt['runtimeReceipt']['lifecycle']['reason'] = 'interrupted_turn_recovery'
                receipt['runtimeReceipt']['lifecycle']['cancelledOperationIds'] = []
                if self.retirement_phase == 'settled':
                    self.cold = False
                return receipt
            self.assertTrue(kwargs.get('recover_retired_only'))
            if self.cold:
                return {'state': 'rejected', 'reason': 'requested_turn_is_not_the_latest_retired_binding'}
            self.recovered = True
            return self.receipt
        self.abort_turn.side_effect = recover

    def failure(self, *, session=None, turn=None):
        with self.app.ledger.connection(write=True) as conn:
            session = session or self.effect['request']['sessionId']
            sequence = conn.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM agent_runtime_events WHERE session_id=?', (session,)).fetchone()[0]
            conn.execute('INSERT INTO agent_runtime_events(event_id,session_id,turn_id,sequence,event_type,created_at_ms) VALUES(?,?,?,?,?,?)',
                         ('interrupted-fixture:' + session + ':' + str(sequence), session, turn or self.effect['receipt']['turnId'], sequence, 'turn_failed', 1))

    def as_verifier(self):
        # Same isolated admitted Pi identity, represented by the auxiliary
        # owner's exact subject/claim shape instead of an execute task binding.
        snapshot = self.snapshot(self.created)
        request = dict(self.effect['request'], purpose='verify')
        request['subjectHash'] = self.app.lifecycle.subject(snapshot, snapshot.task(request['taskId']), 'verify')
        with self.app.ledger.connection(write=True) as conn:
            conn.execute('UPDATE agent_jev_runtime_effects SET request_json=? WHERE effect_id=?', (json.dumps(request), self.effect['effectId']))
            from rag_ime.jev_tasks.types import canonical
            conn.execute('UPDATE agent_jev_executor_claims SET binding_json=? WHERE effect_id=?',
                         (canonical({'purpose': 'verify', 'subjectHash': request['subjectHash'], 'dispatchId': self.effect['effectId']}), self.effect['effectId']))
        self.effect = self.app.effects.get(self.effect['effectId'])

    def test_failed_execute_retires_then_recovers_once_and_keeps_root_goal(self):
        self.failure()
        self.reconcile()
        self.assertEqual(self.claims(), [])
        self.assertTrue(self.recovered)
        self.assertEqual([bool(c.get('recover_interrupted_only')) for c in self.recovery_calls], [False, True, False])
        self.assertFalse(self.app.lifecycle.policy(self.created['graphId'])['stopped'])
        calls = len(self.recovery_calls)
        self.reconcile()
        self.assertEqual(len(self.recovery_calls), calls)

    def test_failed_verifier_uses_same_recovery_and_records_missing_output_not_success(self):
        self.as_verifier()
        self.failure()
        self.reconcile()
        self.assertEqual(self.claims(), [])
        self.assertTrue(self.recovered)
        with self.app.ledger.connection() as conn:
            row = conn.execute('SELECT result_json FROM agent_jev_aux_settlements WHERE dispatch_id=?', (self.effect['effectId'],)).fetchone()
        self.assertEqual(json.loads(row[0])['status'], 'missing_output')
        self.assertEqual(self.service.room_turns.active_turn(self.effect['request']['sessionId']), ('', ''))
        calls = len(self.recovery_calls)
        self.reconcile()
        self.assertEqual(len(self.recovery_calls), calls)

    def test_absent_or_other_turn_failure_never_requests_interrupted_retirement(self):
        self.failure(turn='another-turn')
        self.failure(session=next(s['id'] for s in self.sessions if s['id'] != self.effect['request']['sessionId']))
        self.reconcile()
        self.assertTrue(self.claims())
        self.assertFalse(any(c.get('recover_interrupted_only') for c in self.recovery_calls))

    def test_known_live_identity_is_not_retired_even_with_old_failure_evidence(self):
        self.failure()
        self.is_turn_active.return_value = True
        self.reconcile()
        self.abort_turn.assert_not_called()
        self.assertTrue(self.claims())

    def test_pending_or_failed_retirement_is_bounded_and_never_forges_settlement(self):
        self.failure()
        for phase in ('requested', 'failed'):
            with self.subTest(phase=phase):
                self.retirement_phase = phase
                self.recovery_calls.clear()
                self.reconcile()
                self.assertTrue(self.claims())
                self.assertFalse(self.recovered)
                self.assertLessEqual(len(self.recovery_calls), 3)
                if phase == 'requested':
                    self.assertTrue(self.recovery_calls[-1]['lookup_only'])

    def test_recovered_verifier_still_waits_for_causal_descendants(self):
        self.as_verifier()
        self.failure()
        with patch('rag_ime.jev_tasks.runtime_adapter.causal_descendants_proof', return_value={'settled': False, 'pending': ['job']}):
            self.reconcile()
        self.assertTrue(self.recovered)
        self.assertTrue(self.claims())
        self.assertIsNone(self.app.execution_terminal(self.effect, lookup=False))

    def test_newer_host_turn_rejection_keeps_exact_old_claim(self):
        self.failure()
        self.abort_turn.side_effect = None
        self.abort_turn.return_value = {'state': 'rejected', 'reason': 'requested_turn_is_not_the_cold_recovered_binding'}
        self.reconcile()
        self.assertTrue(self.claims())
        self.assertFalse(self.recovered)
        self.abort.assert_not_called()

    def test_auxiliary_drain_preserves_a_newer_room_dispatch_mapping(self):
        self.as_verifier()
        self.recovered = True
        session = self.effect['request']['sessionId']
        self.service.room_turns.begin(session, 'new-root', dispatch_id='new-dispatch')
        self.service.room_turns.accept(session, 'new-turn', 'new-root')
        self.reconcile()
        self.assertEqual(self.claims(), [])
        self.assertEqual(self.service.room_turns.active_turn(session), ('new-root', 'new-dispatch'))
        self.abort_turn.assert_not_called()
