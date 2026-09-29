"""Two Roots in one Room share a real admission reservation; Pi terminal is explicit."""
from unittest.mock import patch

from tests.test_jev_host_application import JevHostFixture
from rag_ime.jev_tasks.types import canonical


class JevSharedSessionWakeupTests(JevHostFixture):
    def test_other_root_waits_until_exact_drain_then_dispatches_once(self):
        first = self.create('first-root')
        self.app.tick()
        first_task = self.snapshot(first).tasks[0]
        effect = self.app.effects.get(first_task.accepted_turn_id)
        sid = effect['request']['sessionId']
        second = self.create('second-root')
        owner = next(p['id'] for p in self.room['participants'] if p['sessionId'] == sid)
        with self.app.ledger.connection(write=True) as conn:
            conn.execute('INSERT INTO agent_jev_task_requirements VALUES(?,?,?)',
                         (second['workItemId'], second['graphId'], canonical({'ownerParticipantId': owner})))
        self.app.tick()
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.snapshot(second).tasks[0].accepted_turn_id)

        self.app.stop(effect['request']['roomId'], first['rootId'])
        self.app.tick()
        self.assertEqual(len(self.calls), 1, 'Stop acceptance alone is not resource release')
        terminal = {'eventId': 'exact-shared-drain', 'eventType': 'turn_completed', 'status': 'aborted'}
        self.service.runtime.release_prompt_admission(sid, client_message_id=effect['effectId'])
        with patch.object(self.app, 'execution_terminal', side_effect=lambda item, **kw:
                          terminal if item['effectId'] == effect['effectId'] else None):
            self.app.reconcile_graph(self.app.binding_by_graph(first['graphId']))
            for _ in range(4):
                self.app.tick()
                if len(self.calls) == 2:
                    break
            self.assertEqual(len(self.calls), 2)
            current = self.snapshot(second).tasks[0]
            self.assertEqual(current.state, 'active')
            self.assertEqual(self.calls[1][0], sid)
            self.assertEqual(self.service.room_turns.active_turn(sid)[0], second['rootId'])
            self.app.reconcile_graph(self.app.binding_by_graph(first['graphId']))
            self.app.tick()
            self.assertEqual(len(self.calls), 2, 'Repeated old drain must not dispatch again')
            self.assertEqual(self.service.room_turns.active_turn(sid)[0], second['rootId'])
