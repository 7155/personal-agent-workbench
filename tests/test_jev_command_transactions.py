"""Real SQLite/WorkItem owners; isolated Runtime transport and bounded race barriers."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from threading import Barrier, Event
from unittest.mock import patch

from rag_ime.jev_tasks.types import GraphConflict, digest
from tests.test_jev_host_application import JevHostFixture


class JevCommandTransactionTests(JevHostFixture):
    def execution(self):
        created = self.create()
        self.app.tick()
        task = self.snapshot(created).tasks[0]
        effect = self.app.effects.get(task.accepted_turn_id)
        return created, task, effect

    def proposal(self):
        return {'resultSummary': 'isolated fixture result', 'artifactRefs': ['fixture:artifact'],
                'evidenceRefs': ['fixture:check']}

    def test_stop_commits_between_submission_validation_and_owner_transaction(self):
        created, task, effect = self.execution()
        entered, resume, stopped, finish_stop = (Event() for _ in range(4))
        apply, cancel = self.app.owner.apply, self.service.room_work.cancel_root
        def delayed_apply(*args, **kwargs):
            entered.set()
            if not resume.wait(10): raise TimeoutError('submission barrier')
            return apply(*args, **kwargs)
        def delayed_cancel(*args, **kwargs):
            # Real stop has already committed stopped=1 and its registry fence.
            stopped.set()
            if not finish_stop.wait(10): raise TimeoutError('stop barrier')
            return cancel(*args, **kwargs)
        with patch.object(self.app.owner, 'apply', side_effect=delayed_apply), \
             patch.object(self.service.room_work, 'cancel_root', side_effect=delayed_cancel), \
             ThreadPoolExecutor(max_workers=2) as pool:
            submission = pool.submit(self.app.submit_execution, effect, self.proposal())
            try:
                self.assertTrue(entered.wait(10))
                stopping = pool.submit(self.app.stop, self.room['id'], created['rootId'])
                self.assertTrue(stopped.wait(10))
                resume.set()
                with self.assertRaises(GraphConflict): submission.result(timeout=10)
                with self.app.ledger.connection() as conn:
                    self.assertIsNone(conn.execute('SELECT 1 FROM agent_jev_commands WHERE command_id=?',
                        ('submit:' + effect['effectId'],)).fetchone())
                self.assertEqual(self.service.room_work.get(task.id)['state'], 'active')
            finally:
                resume.set(); finish_stop.set()
            stopping.result(timeout=10)

    def test_epoch_change_between_validation_and_write_rejects_submission(self):
        created, task, effect = self.execution()
        apply = self.app.owner.apply
        def change_epoch(*args, **kwargs):
            with self.app.ledger.connection(write=True) as conn:
                conn.execute('UPDATE agent_jev_host_roots SET epoch=epoch+1 WHERE graph_id=?', (created['graphId'],))
            return apply(*args, **kwargs)
        with patch.object(self.app.owner, 'apply', side_effect=change_epoch):
            with self.assertRaises(GraphConflict): self.app.submit_execution(effect, self.proposal())
        self.assertEqual(self.service.room_work.get(task.id)['state'], 'active')

    def review_command(self, action):
        created, task, effect = self.execution()
        self.app.submit_execution(effect, self.proposal())
        task = self.snapshot(created).task(task.id)
        return created, {'action': action, 'graphId': created['graphId'], 'clientMessageId': 'review-once',
            'taskId': task.id, 'taskHash': digest(asdict(task)), 'reason': 'fixture review',
            'evidenceRefs': ['fixture:verification'], 'operabilityVerdict': 'passed',
            'requirementVerdict': 'satisfied' if action == 'accept' else 'not_satisfied'}

    def persisted_counts(self):
        with self.app.ledger.connection() as conn:
            return tuple(conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] for table in
                         ('agent_jev_commands', 'agent_room_work_events', 'agent_jev_owner_events'))

    def assert_replay(self, created, payload):
        original = self.app.command(self.room['id'], payload)
        counts = self.persisted_counts()
        replay = self.app.command(self.room['id'], payload)
        self.assertTrue(replay['replayed'])
        self.assertEqual({**original, 'replayed': True}, replay)
        self.assertEqual(self.persisted_counts(), counts)
        for change in ({'reason': 'different'}, {'action': 'advance'}):
            with self.assertRaises(GraphConflict):
                self.app.command(self.room['id'], {**payload, **change})
        self.app.stop(self.room['id'], created['rootId'])
        stopped_counts = self.persisted_counts()
        with patch.object(self.app.owner, 'apply', side_effect=AssertionError('read only replay')), \
             patch.object(self.app.ledger, 'change_edges', side_effect=AssertionError('read only replay')), \
             patch.object(self.app, '_enqueue', side_effect=AssertionError('read only replay')):
            self.assertEqual(self.app.command(self.room['id'], payload), replay)
        self.assertEqual(self.persisted_counts(), stopped_counts)
        with self.assertRaises(GraphConflict):
            self.app.command(self.room['id'], {**payload, 'clientMessageId': 'new-after-stop'})

    def test_accept_request_replays_after_changed_task_and_stop(self):
        created, payload = self.review_command('accept')
        with patch.object(self.app, 'execution_terminal', return_value={'eventId': 'fixture:drained'}):
            self.assert_replay(created, payload)

    def test_return_request_replays_after_changed_revision_and_stop(self):
        created, payload = self.review_command('return')
        with patch.object(self.app, 'execution_terminal', return_value={'eventId': 'fixture:drained'}):
            self.assert_replay(created, payload)

    def edge_command(self):
        created = self.create()
        root = self.snapshot(created).tasks[0]
        children = [self.service.room_work.create(room_id=self.room['id'], objective='child '+str(i),
            expected_output='fixture', acceptance_criteria=['fixture'], current_owner_participant_id=root.owner_id,
            created_by_participant_id=root.owner_id, accountable_participant_id=root.owner_id,
            client_message_id='child:'+str(i), root_turn_id=created['rootId'], parent_work_id=root.id, depth=2)
            for i in range(2)]
        payload = {'action': 'edges', 'graphId': created['graphId'], 'clientMessageId': 'edge-once',
            'add': [{'prerequisite': children[0]['id'], 'dependent': children[1]['id'], 'kind': 'requires'}], 'remove': []}
        return created, payload

    def test_edges_request_replays_after_changed_topology_and_stop(self):
        self.assert_replay(*self.edge_command())

    def test_edges_wakes_only_after_the_durable_command_and_event_commit(self):
        created, payload = self.edge_command()
        def observe_commit():
            with self.app.ledger.connection() as conn:
                self.assertIsNotNone(conn.execute('SELECT 1 FROM agent_jev_commands WHERE command_id=?',
                    (payload['clientMessageId'],)).fetchone())
                self.assertIsNotNone(conn.execute('SELECT 1 FROM agent_jev_owner_events WHERE graph_id=? AND source_id=?',
                    (created['graphId'], payload['clientMessageId'])).fetchone())
        with patch.object(self.service.wake_scheduler, 'wake', side_effect=observe_commit) as wake:
            self.app.command(self.room['id'], payload)
            wake.assert_called_once()
            self.app.command(self.room['id'], payload)
            wake.assert_called_once()

    def test_submission_wins_then_stop_preserves_original_readonly_receipt(self):
        created, task, effect = self.execution()
        original = self.app.submit_execution(effect, self.proposal())
        self.app.stop(self.room['id'], created['rootId'])
        counts = self.persisted_counts()
        with patch.object(self.app.owner, 'apply', side_effect=AssertionError('replay is read only')):
            self.assertEqual(self.app.submit_execution(effect, self.proposal()), {**original, 'replayed': True})
        self.assertEqual(self.persisted_counts(), counts)
        with self.assertRaises(GraphConflict):
            self.app.submit_execution(effect, {**self.proposal(), 'resultSummary': 'different'})

    def test_concurrent_same_review_commits_once(self):
        created, payload = self.review_command('accept')
        start = Barrier(2)
        def review():
            start.wait(timeout=10)
            return self.app.command(self.room['id'], payload)
        with patch.object(self.app, 'execution_terminal', return_value={'eventId': 'fixture:drained'}), \
             ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(review) for _ in range(2)]
            results = [f.result(timeout=10) for f in futures]
        self.assertEqual(sorted(r['replayed'] for r in results), [False, True])
        with self.app.ledger.connection() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM agent_jev_commands WHERE command_id=?',
                (payload['clientMessageId'],)).fetchone()[0], 1)
        self.assertEqual(self.service.room_work.get(payload['taskId'])['state'], 'done')
        self.assertEqual(len(self.calls), 1)

    def test_edges_queue_failure_rolls_back_topology_and_receipt(self):
        created, payload = self.edge_command()
        before = self.snapshot(created)
        counts = self.persisted_counts()
        enqueue = self.app._enqueue
        def fail_after_insert(*args):
            enqueue(*args)
            raise RuntimeError('injected event failure')
        with patch.object(self.app, '_enqueue', side_effect=fail_after_insert):
            with self.assertRaisesRegex(RuntimeError, 'injected event failure'):
                self.app.command(self.room['id'], payload)
        self.assertEqual(self.persisted_counts(), counts)
        self.assertEqual(self.snapshot(created), before)
        self.assertFalse(self.app.command(self.room['id'], payload)['replayed'])
        self.assertEqual(self.snapshot(created).topology_revision, before.topology_revision + 1)
