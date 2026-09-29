"""Manual JEV reassignment through real task/receipt owners; no installed Runtime."""
from dataclasses import asdict
from unittest.mock import patch

from rag_ime.jev_tasks.types import GraphConflict, digest
from tests.test_jev_host_application import JevHostFixture


class JevManualReassignTests(JevHostFixture):
    def setUp(self):
        super().setUp()
        self.created = self.create()
        self.before = self.snapshot(self.created)
        self.task = self.before.task(self.before.root_work_id)
        self.target = next(p for p in self.room['participants'] if p['id'] != self.task.owner_id)
        self.payload = {'action': 'reassign', 'graphId': self.created['graphId'],
            'taskId': self.task.id, 'taskHash': digest(asdict(self.task)),
            'targetParticipantId': self.target['id'], 'reason': 'Independent final review',
            'clientMessageId': 'manual-reassign:1'}

    def command(self, **changes):
        return self.app.command(self.room['id'], {**self.payload, **changes})

    def assigned_events(self):
        with self.app.ledger.connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM agent_room_work_events WHERE work_id=? AND event_type='assigned'",
                                (self.task.id,)).fetchone()[0]

    def test_ready_reassign_persists_once_and_next_normal_tick_uses_new_owner(self):
        count = self.assigned_events()
        result = self.command()
        self.assertEqual(result['status'], 'applied')
        changed = self.snapshot(self.created).task(self.task.id)
        self.assertEqual(changed.owner_id, self.target['id'])
        self.assertNotEqual(changed.assignment_key, self.task.assignment_key)
        self.assertEqual(self.assigned_events(), count + 1)
        self.assertEqual(self.calls, [])  # This command is not a second scheduler.
        self.assertTrue(self.command()['replayed'])
        self.assertEqual(self.assigned_events(), count + 1)
        self.app.tick()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], self.target['sessionId'])
        self.assertTrue(self.command()['replayed'])
        self.assertEqual(len(self.calls), 1)

    def test_native_control_accepts_target_field(self):
        from rag_ime.control_api import ControlAccessContext, ControlPathId, ControlRequest, default_route_policy
        default_route_policy().authorize(ControlRequest(request_id='reassign',
            path_id=ControlPathId.AGENT_JEV_COMMAND.value, params={'roomId': self.room['id']},
            body=self.payload), ControlAccessContext.native())

    def test_wrong_hash_and_non_room_target_do_not_mutate_or_dispatch(self):
        for changes in ({'taskHash': 'stale'}, {'targetParticipantId': 'other-room-participant'}):
            with self.subTest(changes=changes), self.assertRaises(GraphConflict):
                self.command(**changes)
        self.assertEqual(self.snapshot(self.created).task(self.task.id), self.task)
        self.assertEqual(self.calls, [])

    def test_busy_target_can_own_future_work_without_starting_a_second_execution(self):
        with patch.object(self.service, '_room_target_idle', return_value=False):
            self.assertEqual(self.command()['status'], 'applied')
        self.assertEqual(self.calls, [])

    def test_client_id_conflict_and_replay_after_root_stop(self):
        self.command()
        count = self.assigned_events()
        for change in ({'reason': 'changed'}, {'targetParticipantId': self.task.owner_id}, {'taskHash': 'new-hash'}):
            with self.subTest(change=change), self.assertRaisesRegex(GraphConflict, 'different intent'):
                self.command(**change)
        self.app.stop(self.room['id'], self.created['rootId'])
        with patch.object(self.app.owner, 'apply', side_effect=AssertionError('replay must not mutate')), \
             patch.object(self.app, '_enqueue', side_effect=AssertionError('stopped replay must not schedule')):
            self.assertTrue(self.command()['replayed'])
        self.assertEqual(self.assigned_events(), count)
        with self.assertRaisesRegex(GraphConflict, 'stopped'):
            self.command(clientMessageId='fresh-after-stop')
        self.assertEqual(self.calls, [])

    def test_running_unknown_and_pending_attempts_are_not_reassigned(self):
        self.app.tick()
        task = self.snapshot(self.created).task(self.task.id)
        self.payload['taskHash'] = digest(asdict(task))
        calls = len(self.calls)
        for active in (True, False):
            with self.subTest(active=active), patch.object(self.service.runtime, 'is_turn_active', return_value=active):
                with self.assertRaisesRegex(GraphConflict, 'live or unknown'):
                    self.command()
        for state in ('pending', 'sending', 'unknown'):
            with self.subTest(state=state):
                with self.app.ledger.connection(write=True) as conn:
                    conn.execute('UPDATE agent_jev_runtime_effects SET state=? WHERE effect_id=?', (state, task.accepted_turn_id))
                with self.assertRaisesRegex(GraphConflict, 'pending/unknown'):
                    self.command()
        self.assertEqual(self.snapshot(self.created).task(task.id).owner_id, task.owner_id)
        self.assertEqual(len(self.calls), calls)

    def specification(self, **changes):
        import json
        with self.app.ledger.connection(write=True) as conn:
            row = conn.execute('SELECT specification_json FROM agent_jev_task_requirements WHERE task_id=?', (self.task.id,)).fetchone()
            specification = {**json.loads(row[0] if row else '{}'), **changes}
            conn.execute('INSERT INTO agent_jev_task_requirements VALUES(?,?,?) ON CONFLICT(task_id) DO UPDATE SET specification_json=excluded.specification_json',
                         (self.task.id, self.created['graphId'], json.dumps(specification)))

    def test_owner_lock_capability_and_write_scope_are_enforced(self):
        self.specification(ownerParticipantId=self.task.owner_id)
        with self.assertRaisesRegex(GraphConflict, 'capability/scope or owner lock'):
            self.command()
        self.specification(ownerParticipantId='', requiredCapabilities=['browser'])
        with patch.object(self.service, '_runtime_tool_manifest', return_value=[]):
            with self.assertRaises(GraphConflict):
                self.command()
        self.specification(requiredCapabilities=[], writeTargets=['/outside-granted-workspace/file'])
        with self.assertRaises(GraphConflict):
            self.command()
        self.assertEqual(self.snapshot(self.created).task(self.task.id), self.task)
        self.assertEqual(self.calls, [])

    def test_legacy_guard_is_unchanged(self):
        with self.assertRaises(GraphConflict):
            self.app.guard_legacy_work(task_id=self.task.id)
        self.assertEqual(self.command()['status'], 'applied')


    def test_enqueue_failure_rolls_back_assignment_receipt_and_wakeup_together(self):
        count = self.assigned_events()
        enqueue = self.app._enqueue
        def enqueue_then_fail(*args):
            enqueue(*args)
            raise RuntimeError('injected queue failure')
        with patch.object(self.app, '_enqueue', side_effect=enqueue_then_fail):
            with self.assertRaisesRegex(RuntimeError, 'injected queue failure'):
                self.command()
        self.assertEqual(self.snapshot(self.created).task(self.task.id), self.task)
        self.assertEqual(self.assigned_events(), count)
        with self.app.ledger.connection() as conn:
            self.assertIsNone(conn.execute('SELECT 1 FROM agent_jev_commands WHERE command_id=?',
                                          (self.payload['clientMessageId'],)).fetchone())
            self.assertIsNone(conn.execute('SELECT 1 FROM agent_jev_owner_events WHERE source_id=?',
                                          ('reassign:' + self.payload['clientMessageId'],)).fetchone())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.command()['status'], 'applied')

    def test_wake_failure_keeps_durable_event_and_replay_does_not_need_a_second_write(self):
        # Consume create's original event through its owner without dispatch;
        # the later tick must have this manual command's own durable wakeup.
        event = self.app.queue.claim_next()
        self.assertIsNotNone(event)
        self.assertTrue(self.app.queue.finish(event, {'status': 'wait'}))
        with patch.object(self.service.wake_scheduler, 'wake', side_effect=RuntimeError('injected wake failure')):
            with self.assertRaisesRegex(RuntimeError, 'injected wake failure'):
                self.command()
        self.assertEqual(self.snapshot(self.created).task(self.task.id).owner_id, self.target['id'])
        with self.app.ledger.connection() as conn:
            row = conn.execute('SELECT state FROM agent_jev_owner_events WHERE source_id=?',
                               ('reassign:' + self.payload['clientMessageId'],)).fetchone()
            self.assertEqual(row[0], 'pending')
        with patch.object(self.app, '_enqueue', side_effect=AssertionError('replay must not enqueue')):
            self.assertTrue(self.command()['replayed'])
        self.app.tick()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], self.target['sessionId'])


class JevDependentReassignTests(JevHostFixture):
    def test_dependency_blocked_unexecuted_child_can_be_explicitly_reassigned(self):
        created = self.app.create(self.room['id'], {'clientMessageId': 'planned', 'message': 'Build then independent review',
            'strategy': 'plan', 'modelRouting': 'participant'})
        self.app.tick()
        effect = next(e for e in self.app.projection(self.room['id'], created['graphId'])['effects']
                      if e['request'].get('purpose') == 'plan')
        result = self.app.tool_operation(effect['request']['sessionId'], {'op': 'plan_submit', 'proposal': {
            'requirementsRevision': 1, 'topologyRevision': 0, 'tasks': [
                {'key': 'build', 'objective': 'Build', 'expectedOutput': 'code', 'acceptanceCriteria': ['works'], 'dependsOn': []},
                {'key': 'review', 'objective': 'Independent review', 'expectedOutput': 'report', 'acceptanceCriteria': ['checked'], 'dependsOn': ['build']},
            ]}}, tool_call_id='submit-plan')
        self.assertTrue(result['ok'])
        snapshot = self.snapshot(created)
        task = next(t for t in snapshot.tasks if t.objective == 'Independent review')
        self.assertFalse(task.accepted_turn_id)
        self.assertNotIn(task.id, snapshot.graph().frontier(self.app.executions(snapshot)).ready)
        target = next(p for p in self.room['participants'] if p['id'] != task.owner_id)
        calls = len(self.calls)
        receipt = self.app.command(self.room['id'], {'action': 'reassign', 'graphId': created['graphId'],
            'clientMessageId': 'review-owner', 'taskId': task.id, 'taskHash': digest(asdict(task)),
            'targetParticipantId': target['id'], 'reason': 'non-implementer review'})
        self.assertEqual(receipt['status'], 'applied')
        self.assertEqual(self.snapshot(created).task(task.id).owner_id, target['id'])
        self.assertEqual(len(self.calls), calls)
