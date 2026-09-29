"""JEV Root compatibility with the existing Room steer/receipt owners."""
from unittest.mock import patch

from tests.test_jev_host_application import JevHostFixture


class JevRoomSteerTests(JevHostFixture):
    def setUp(self):
        super().setUp()
        self.created = self.create()
        self.app.tick()
        self.effect = next(e for e in self.app.projection(self.room['id'], self.created['graphId'])['effects']
                           if e['operation'] == 'dispatch')
        self.request = self.effect['request']
        self.turn = self.effect['receipt']['turnId']
        self.sid = self.request['sessionId']
        self.payload = {'action': 'steer_participant', 'rootId': self.created['rootId'],
            'participantId': self.request['ownerId'], 'message': 'Keep the bounded preview alive for verification',
            'clientActionId': 'steer:one'}
        self.prompt.reset_mock()
        self.prompt.side_effect = lambda *args: {'turnId': self.turn, 'queued': True}
        active = patch.object(self.service.runtime, 'is_turn_active', return_value=True)
        self.active = active.start()
        self.addCleanup(active.stop)

    def steer(self, **changes):
        return self.service.steer_room_participant(self.room['id'], {**self.payload, **changes})

    def test_projected_root_steers_same_turn_once_without_new_dispatch(self):
        for index in range(105):
            self.service.rooms.append_event(room_id=self.room['id'], turn_id=self.created['rootId'],
                event_type='participant_activity', participant_id=self.request['ownerId'],
                source_session_id=self.sid, payload={'summary': str(index)}, retain_per_room=100)
        self.assertFalse(any(e['eventType'] == 'user_message' for e in
                             self.service.rooms.control_events_for_turn(self.room['id'], self.created['rootId'])))
        before = self.snapshot(self.created)
        result = self.steer()
        self.assertEqual(result['turnId'], self.turn)
        self.assertTrue(result['accepted'])
        self.assertEqual(result['delivery'], 'steer')
        self.prompt.assert_called_once()
        args = self.prompt.call_args.args
        self.assertEqual(args[0], self.sid)
        self.assertEqual(args[1]['delivery'], 'steer')
        self.assertEqual(self.snapshot(self.created).tasks, before.tasks)
        self.assertEqual(len(self.app.projection(self.room['id'], self.created['graphId'])['effects']), 1)
        self.assertTrue(self.steer()['idempotentReplay'])
        self.prompt.assert_called_once()

    def test_second_steer_still_needs_claim_despite_new_legacy_user_event(self):
        self.steer()
        self.assertTrue(any(e['eventType'] == 'user_message' for e in
                            self.service.rooms.control_events_for_turn(self.room['id'], self.created['rootId'])))
        with self.app.ledger.connection(write=True) as conn:
            conn.execute('DELETE FROM agent_jev_executor_claims WHERE effect_id=?', (self.effect['effectId'],))
        with self.assertRaisesRegex(ValueError, 'current JEV claim'):
            self.steer(clientActionId='steer:two')
        self.prompt.assert_called_once()
        self.assertTrue(self.steer()['idempotentReplay'])  # Only the completed receipt is replayed.
        self.prompt.assert_called_once()

    def test_stopped_root_refuses_new_steer_but_keeps_completed_receipt(self):
        self.steer()
        with patch.object(self.service.room_cancellation, 'abort_turn', return_value={'status': 'cancel_requested'}):
            self.app.stop(self.room['id'], self.created['rootId'])
        with self.assertRaisesRegex(ValueError, 'stopped'):
            self.steer(clientActionId='steer:after-stop')
        self.assertTrue(self.steer()['idempotentReplay'])
        self.prompt.assert_called_once()

    def test_wrong_room_and_non_claimed_participant_are_rejected(self):
        people = [self.service.sessions.create(title=name) for name in ('other-a', 'other-b')]
        other = self.service.rooms.create(title='Other Room', routing_policy='moderator', participants=[
            {'sessionId': s['id'], 'roleId': role, 'roleVersion': '1', 'displayName': s['title'], 'collaborationRole': role}
            for s, role in zip(people, ('coordinator', 'implementer'))])
        with self.assertRaisesRegex(ValueError, 'does not belong'):
            self.service.steer_room_participant(other['id'], self.payload)
        target = next(p for p in self.room['participants'] if p['id'] != self.request['ownerId'])
        with self.assertRaisesRegex(ValueError, 'current JEV claim'):
            self.steer(participantId=target['id'], clientActionId='steer:no-claim')
        self.prompt.assert_not_called()

    def test_runtime_unconfirmed_exact_turn_cannot_be_steered(self):
        for active in (False, None):
            with self.subTest(active=active):
                self.active.return_value = active
                with self.assertRaisesRegex(ValueError, 'exact active JEV turn'):
                    self.steer(clientActionId='steer:runtime:' + str(active))
        self.active.assert_called_with(self.sid, self.turn, client_message_id=self.effect['effectId'])
        self.prompt.assert_not_called()

    def test_newer_dispatch_and_finished_binding_cannot_receive_old_root_steer(self):
        self.service.room_turns.finish(self.sid, self.turn, self.created['rootId'])
        with self.assertRaisesRegex(ValueError, 'exact active JEV turn'):
            self.steer(clientActionId='steer:finished')
        self.service.room_turns.begin(self.sid, self.created['rootId'], dispatch_id='new-dispatch')
        self.service.room_turns.accept(self.sid, 'new-turn', self.created['rootId'])
        with self.assertRaisesRegex(ValueError, 'exact active JEV turn'):
            self.steer(clientActionId='steer:newer')
        self.prompt.assert_not_called()

    def test_inactive_room_is_rejected(self):
        self.service.rooms.archive(self.room['id'], archived=True)
        with self.assertRaisesRegex(ValueError, 'inactive'):
            self.steer(clientActionId='steer:archived')
        self.prompt.assert_not_called()

    def test_pending_or_unknown_effect_and_corrupt_claim_are_rejected(self):
        for state in ('pending', 'unknown'):
            with self.subTest(state=state):
                with self.app.ledger.connection(write=True) as conn:
                    conn.execute('UPDATE agent_jev_runtime_effects SET state=? WHERE effect_id=?', (state, self.effect['effectId']))
                with self.assertRaisesRegex(ValueError, 'exact active JEV turn'):
                    self.steer(clientActionId='steer:' + state)
        with self.app.ledger.connection(write=True) as conn:
            conn.execute("UPDATE agent_jev_runtime_effects SET state='accepted' WHERE effect_id=?", (self.effect['effectId'],))
            conn.execute("UPDATE agent_jev_executor_claims SET binding_json='[]' WHERE effect_id=?", (self.effect['effectId'],))
        with self.assertRaisesRegex(ValueError, 'exact active JEV turn'):
            self.steer(clientActionId='steer:bad-claim')
        self.prompt.assert_not_called()

    def test_reusing_action_id_with_different_message_does_not_send_again(self):
        self.steer()
        with self.assertRaises(ValueError):
            self.steer(message='different message')
        self.prompt.assert_called_once()

    def test_verifier_uses_its_auxiliary_claim_and_original_turn(self):
        import json
        from rag_ime.jev_tasks.types import canonical
        request = dict(self.request, purpose='verify', subjectHash='isolated-subject')
        with self.app.ledger.connection(write=True) as conn:
            conn.execute('UPDATE agent_jev_runtime_effects SET request_json=? WHERE effect_id=?', (json.dumps(request), self.effect['effectId']))
            conn.execute('UPDATE agent_jev_executor_claims SET binding_json=? WHERE effect_id=?',
                (canonical({'purpose': 'verify', 'subjectHash': request['subjectHash'], 'dispatchId': self.effect['effectId']}), self.effect['effectId']))
        self.assertEqual(self.steer()['turnId'], self.turn)
        self.prompt.assert_called_once()
