"""Task-detail controls use current host facts, without advancing execution on reads."""
from dataclasses import asdict
from unittest.mock import patch

from rag_ime.jev_tasks.types import digest
from tests.test_jev_host_application import JevHostFixture


class JevAssignmentControlsTests(JevHostFixture):
    def options(self, created, task_id=None):
        return self.app.command(self.room['id'], {'action': 'assignment_options',
            'graphId': created['graphId'], 'taskId': task_id or created['workItemId'],
            'clientMessageId': 'read-options'})

    def test_idle_options_bind_exact_task_and_never_dispatch_or_wake(self):
        created = self.create()
        task = self.snapshot(created).task(created['workItemId'])
        with patch.object(self.service.wake_scheduler, 'wake') as wake:
            result = self.options(created)
            wake.assert_not_called()
        self.assertEqual(result['taskHash'], digest(asdict(task)))
        self.assertEqual(result['action'], 'reassign')
        self.assertEqual(result['targetParticipantIds'], [p['id'] for p in self.room['participants'] if p['id'] != task.owner_id])
        self.assertEqual(self.calls, [])
        with self.app.ledger.connection() as conn:
            self.assertIsNone(conn.execute('SELECT 1 FROM agent_jev_commands WHERE command_id=?', ('read-options',)).fetchone())

    def test_running_options_offer_reclaim_but_unknown_and_stop_offer_no_action(self):
        created = self.create(); self.app.tick()
        with patch.object(self.service.runtime, 'is_turn_active', return_value=True):
            result = self.options(created)
        self.assertEqual(result['action'], 'request_reclaim')
        with patch.object(self.service.runtime, 'is_turn_active', return_value=False):
            result = self.options(created)
        self.assertEqual((result['action'], result['unavailableReason']), ('', 'execution_unknown'))
        self.app.stop(self.room['id'], created['rootId'])
        result = self.options(created)
        self.assertEqual((result['action'], result['unavailableReason']), ('', 'root_inactive'))
        self.assertEqual(len(self.calls), 1)

    def test_options_keep_owner_lock_and_capability_scope(self):
        import json
        created = self.create(); task = self.snapshot(created).task(created['workItemId'])
        for specification, reason in [({'ownerParticipantId': task.owner_id}, 'owner_locked'),
                                      ({'requiredCapabilities': ['not-available']}, 'no_eligible_partner'),
                                      ({'writeTargets': ['/ungranted/file']}, 'no_eligible_partner')]:
            with self.app.ledger.connection(write=True) as conn:
                conn.execute('INSERT INTO agent_jev_task_requirements VALUES(?,?,?) ON CONFLICT(task_id) DO UPDATE SET specification_json=excluded.specification_json',
                             (task.id, created['graphId'], json.dumps(specification)))
            result = self.options(created)
            self.assertEqual((result['action'], result['unavailableReason'], result['targetParticipantIds']), ('', reason, []))

    def test_projection_exposes_reclaim_until_real_drain_and_keeps_old_owner(self):
        created = self.create(); self.app.tick()
        task = self.snapshot(created).task(created['workItemId'])
        target = next(p['id'] for p in self.room['participants'] if p['id'] != task.owner_id)
        with patch.object(self.service.runtime, 'is_turn_active', return_value=True):
            self.app.command(self.room['id'], {'action': 'request_reclaim', 'graphId': created['graphId'],
                'taskId': task.id, 'taskHash': digest(asdict(task)), 'targetParticipantId': target,
                'reason': 'adjust responsibility', 'clientMessageId': 'control-reclaim'})
            with patch.object(self.service.wake_scheduler, 'wake') as wake:
                result = self.app.projection(self.room['id'], created['graphId'])
                options = self.options(created)
                wake.assert_not_called()
        self.assertEqual(options['unavailableReason'], 'reclaim_pending')
        self.assertEqual(result['reclaims'], [{'reclaimId': 'control-reclaim', 'taskId': task.id,
            'taskRevision': task.revision, 'dispatchId': task.accepted_turn_id,
            'targetParticipantId': target, 'stage': 'awaiting_stop'}])
        self.assertEqual(result['tasks'][0]['owner_id'], task.owner_id)
        effect = self.app.effects.get(task.accepted_turn_id)
        proof = {'eventId': 'drain', 'eventType': 'turn_completed', 'status': 'aborted'}
        with patch.object(self.app, 'execution_terminal', side_effect=lambda e, **kw: proof if e['effectId'] == effect['effectId'] else None):
            self.assertEqual(self.app.projection(self.room['id'], created['graphId'])['reclaims'][0]['stage'], 'awaiting_assignment')
            self.app.reconcile_graph(self.app.binding_by_graph(created['graphId']))
        self.assertEqual(self.app.projection(self.room['id'], created['graphId'])['reclaims'], [])
        self.assertEqual(self.snapshot(created).task(task.id).owner_id, target)
