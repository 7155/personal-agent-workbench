"""Public reclaim uses real stores/outbox; exact Pi cancellation/drain are fixtures."""
import json
from dataclasses import asdict
from unittest.mock import patch

from rag_ime.jev_tasks.types import GraphConflict, digest
from tests.test_jev_host_application import JevHostFixture


class JevManualReclaimTests(JevHostFixture):
    def setUp(self):
        super().setUp()
        self.terminals = {}
        for owner, name, changes in (
            (self.service.runtime, 'is_turn_active', {'return_value': True}),
            (self.app, 'execution_terminal', {'side_effect': lambda effect, **kw: self.terminals.get(effect['effectId'])}),
        ):
            p = patch.object(owner, name, **changes)
            p.start(); self.addCleanup(p.stop)
        self.created = self.create()
        self.app.tick()
        self.task = self.current()
        self.worker = self.app.effects.get(self.task.accepted_turn_id)
        self.target = next(p for p in self.room['participants'] if p['id'] != self.task.owner_id)
        self.payload = {'action': 'request_reclaim', 'graphId': self.created['graphId'],
            'taskId': self.task.id, 'taskHash': digest(asdict(self.task)),
            'targetParticipantId': self.target['id'], 'reason': 'Move this responsibility to an available partner',
            'clientMessageId': 'manual-reclaim:1'}
        p = patch.object(self.service.runtime, 'abort_turn', side_effect=self.cancel_receipt)
        self.abort = p.start(); self.addCleanup(p.stop)

    def current(self):
        return self.snapshot(self.created).task(self.created['workItemId'])

    def command(self, **changes):
        return self.app.command(self.room['id'], {**self.payload, **changes})

    def cancel_receipt(self, session_id, turn_id, *, client_message_id, cancel_id, lookup_only=False):
        return {'schemaVersion': 'rag-ime.pi-exact-turn-cancel.v1', 'sessionId': session_id,
            'turnId': turn_id, 'clientMessageId': client_message_id, 'cancelId': cancel_id,
            'receiptId': 'cancel-receipt:' + cancel_id, 'state': 'accepted'}

    def claims(self):
        with self.app.ledger.connection() as conn:
            return conn.execute('SELECT COUNT(*) FROM agent_jev_executor_claims WHERE graph_id=?',
                (self.created['graphId'],)).fetchone()[0]

    def drain(self):
        self.terminals[self.worker['effectId']] = {
            'eventId': 'exact-drain:' + self.worker['effectId'], 'eventType': 'turn_completed', 'status': 'aborted'}
        self.app.reconcile_graph(self.app.binding_by_graph(self.created['graphId']))

    def specification(self, **values):
        with self.app.ledger.connection(write=True) as conn:
            conn.execute('INSERT INTO agent_jev_task_requirements VALUES(?,?,?) ON CONFLICT(task_id) DO UPDATE SET specification_json=excluded.specification_json',
                (self.task.id, self.created['graphId'], json.dumps(values)))

    def test_exact_cancel_acceptance_does_not_reassign_until_real_drain(self):
        receipt = self.command()
        self.assertEqual(receipt['status'], 'requested')
        self.assertTrue(receipt['mustDrainBeforeReassign'])
        self.abort.assert_not_called()  # The existing queue delivers the committed intent.
        self.assertEqual(self.current(), self.task)
        self.app.tick()
        self.abort.assert_called_once_with(self.worker['request']['sessionId'], self.worker['receipt']['turnId'],
            client_message_id=self.worker['effectId'], cancel_id=self.payload['clientMessageId'], lookup_only=False)
        self.assertEqual(self.current(), self.task)
        self.assertEqual(self.claims(), 1)
        self.assertEqual(len(self.calls), 1)
        with self.assertRaisesRegex(GraphConflict, 'reclaimed'):
            self.app.submit_execution(self.worker, {'resultSummary': 'late result', 'evidenceRefs': ['fixture:late'], 'artifactRefs': []})
        self.drain()
        task = self.current()
        self.assertEqual((task.id, task.revision, task.owner_id), (self.task.id, self.task.revision, self.target['id']))
        self.assertEqual(self.claims(), 0)
        self.app.tick()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[-1][0], self.target['sessionId'])
        self.assertEqual(self.abort.call_count, 1)
        self.assertTrue(self.command()['replayed'])

    def test_native_control_accepts_the_exact_reclaim_request(self):
        from rag_ime.control_api import ControlAccessContext, ControlPathId, ControlRequest, default_route_policy
        default_route_policy().authorize(ControlRequest(request_id='reclaim',
            path_id=ControlPathId.AGENT_JEV_COMMAND.value, params={'roomId': self.room['id']}, body=self.payload),
            ControlAccessContext.native())

    def test_already_drained_attempt_is_not_cancelled_after_session_reuse(self):
        self.command()
        self.drain()  # Physical execution ended before the cancellation queue ran.
        self.app.tick()
        self.abort.assert_not_called()
        self.assertEqual(self.current().owner_id, self.target['id'])
        self.assertEqual(self.app.effects.get('cancel:' + self.payload['clientMessageId'])['state'], 'not_sent')

    def test_existing_kernel_reclaim_hash_still_replays_without_a_manual_reason(self):
        snapshot = self.snapshot(self.created)
        execution = self.app.executions(snapshot)[self.task.id]
        old_hash = digest(['reclaim', snapshot.fingerprint, self.task.id, self.target['id'], asdict(execution)])
        self.app.driver.controller.reclaims.request(snapshot, command_id='existing-kernel', task_id=self.task.id,
            target_participant_id=self.target['id'], execution=execution)
        with self.app.ledger.connection() as conn:
            self.assertTrue(self.app.ledger.prior(conn, 'existing-kernel', self.created['graphId'], old_hash)['replayed'])

    def test_unknown_attempt_stale_task_and_ineligible_target_do_not_enqueue_cancel(self):
        with patch.object(self.service.runtime, 'is_turn_active', return_value=False):
            with self.assertRaises(ValueError):
                self.command()
        for changes in ({'taskHash': 'old'}, {'targetParticipantId': 'outside-room'},
                        {'targetParticipantId': self.task.owner_id}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.command(**changes)
        self.specification(ownerParticipantId=self.task.owner_id)
        with self.assertRaises(GraphConflict):
            self.command()
        self.assertEqual(self.current(), self.task)
        self.abort.assert_not_called()
        with self.app.ledger.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_jev_runtime_effects WHERE operation='cancel'").fetchone()[0], 0)

    def test_replay_is_bound_to_original_intent_even_after_root_stop(self):
        self.command(); self.app.tick()
        for changes in ({'reason': 'different'}, {'targetParticipantId': self.task.owner_id}, {'action': 'reassign'}):
            with self.subTest(changes=changes), self.assertRaisesRegex(GraphConflict, 'different intent'):
                self.command(**changes)
        self.app.stop(self.room['id'], self.created['rootId'])
        with patch.object(self.app, '_enqueue', side_effect=AssertionError('replay must not enqueue')):
            self.assertTrue(self.command()['replayed'])
        with self.assertRaisesRegex(GraphConflict, 'stopped'):
            self.command(clientMessageId='after-stop')
        self.drain(); self.app.tick()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.current().owner_id, self.task.owner_id)

    def test_unknown_cancel_receipt_is_lookup_only_and_retains_old_claim(self):
        self.abort.side_effect = lambda *a, **kw: None if not kw.get('lookup_only') else self.cancel_receipt(*a, **kw)
        self.command(); self.app.tick()
        self.app.recover(); self.app.tick()
        self.assertEqual(sum(not call.kwargs['lookup_only'] for call in self.abort.call_args_list), 1)
        self.assertTrue(any(call.kwargs['lookup_only'] for call in self.abort.call_args_list))
        self.assertEqual(self.claims(), 1)
        self.assertEqual(self.current(), self.task)
        self.drain()
        self.assertEqual(self.current().owner_id, self.target['id'])

    def test_event_failure_rolls_back_reclaim_receipt_and_cancel_intent(self):
        original = self.app._enqueue
        def fail(*args):
            original(*args)
            raise RuntimeError('queue unavailable')
        with patch.object(self.app, '_enqueue', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'queue unavailable'):
                self.command()
        with self.app.ledger.connection() as conn:
            for table in ('agent_jev_reclaims', 'agent_jev_commands'):
                key = 'reclaim_id' if table.endswith('reclaims') else 'command_id'
                self.assertIsNone(conn.execute(f'SELECT 1 FROM {table} WHERE {key}=?', (self.payload['clientMessageId'],)).fetchone())
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_jev_runtime_effects WHERE operation='cancel'").fetchone()[0], 0)
        self.assertEqual(self.current(), self.task)
        self.abort.assert_not_called()

    def test_postcommit_wake_failure_recovers_the_existing_cancel_without_replay(self):
        with patch.object(self.service.wake_scheduler, 'wake', side_effect=RuntimeError('wake unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'wake unavailable'):
                self.command()
        self.assertTrue(self.command()['replayed'])
        self.app.recover(); self.app.tick(); self.app.recover(); self.app.tick()
        self.assertEqual(sum(not call.kwargs['lookup_only'] for call in self.abort.call_args_list), 1)
        self.assertEqual(self.claims(), 1)

    def test_target_is_rechecked_after_drain_without_holding_the_old_execution_slot(self):
        self.command(); self.app.tick()
        self.specification(ownerParticipantId=self.task.owner_id)
        self.drain()
        self.assertEqual(self.current(), self.task)
        self.assertEqual(self.claims(), 0)
        self.assertEqual(len(self.calls), 1)
        self.specification(ownerParticipantId='')
        self.app.reconcile_graph(self.app.binding_by_graph(self.created['graphId']))
        self.assertEqual(self.current().owner_id, self.target['id'])
        self.assertEqual(self.abort.call_count, 1)
