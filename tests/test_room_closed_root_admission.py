from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
import unittest
from unittest.mock import patch

from rag_ime.rooms.work import AgentRoomWorkStore
from tests import test_room_root_disposition as fixtures


class RoomClosedRootAdmissionTests(unittest.TestCase):
    setUp = fixtures.RoomRootDispositionTests.setUp
    command = fixtures.RoomRootDispositionTests.command
    room = fixtures.RoomRootDispositionTests.room
    dispatch = fixtures.RoomRootDispositionTests.dispatch
    result = fixtures.RoomRootDispositionTests.result
    post_result = fixtures.RoomRootDispositionTests.post_result

    def worker(self, room):
        return next(p for p in room['participants'] if p['id'] != self.facilitator['id'])

    def delegate(self, room, *, client='fixture-child'):
        return self.service.execute_room_partner_tool(self.facilitator['sessionId'], {
            'op': 'delegate', 'targetParticipantId': self.worker(room)['id'],
            'task': 'Original bounded work', 'expectedOutput': 'Original result',
            'acceptanceCriteria': ['Original result checked'],
        }, tool_call_id=client)

    def create(self, room, *, client='fixture-work', root=None):
        return self.service.room_work.create(room_id=room['id'], objective='Original work', expected_output='Original result',
            current_owner_participant_id=self.worker(room)['id'], created_by_participant_id=self.facilitator['id'],
            accountable_participant_id=self.facilitator['id'], client_message_id=client,
            root_turn_id=root or self.root_id, acceptance_criteria=['Original result checked'])

    def test_closed_original_root_rejects_delegate_batch_retry_and_work_creation(self):
        room = self.command('create_room', clientRequestId='fixture-batch-room', input={
            'task': 'Original bounded batch', 'routingPolicy': 'manual_mentions',
            'participants': [
                {'roleId': 'companion-present-v1', 'collaborationRole': 'coordinator'},
                {'roleId': 'companion-future-v1', 'collaborationRole': 'reviewer'},
                {'roleId': 'companion-firstlight-v1', 'collaborationRole': 'implementer'}]})['target']
        self.dispatch(room)
        formal = self.post_result(room)
        with patch.object(self.service, 'prompt', side_effect=AssertionError('closed Root must not dispatch')):
            with self.assertRaisesRegex(ValueError, 'Root.*(closed|result|terminal)'):
                self.delegate(room)
            with self.assertRaisesRegex(ValueError, 'Root.*(closed|result|terminal)'):
                self.service.execute_room_partner_tool(self.facilitator['sessionId'], {
                    'op': 'delegate_batch', 'phase': 'closed-root-work', 'tasks': [{
                        'targetParticipantId': self.worker(room)['id'], 'task': 'Late work',
                        'expectedOutput': 'Late result', 'acceptanceCriteria': ['Late result checked']}, {
                        'targetParticipantId': next(p['id'] for p in room['participants']
                            if p['id'] not in {self.facilitator['id'], self.worker(room)['id']}),
                        'task': 'Second late work',
                        'expectedOutput': 'Second late result', 'acceptanceCriteria': ['Checked']}],
                }, tool_call_id='fixture-closed-batch')
            with self.assertRaisesRegex(ValueError, 'Root.*(closed|result|terminal)'):
                self.service.execute_room_partner_tool(self.facilitator['sessionId'], {
                    'op': 'retry', 'workItemId': 'fixture-missing-work', 'expectedRevision': 0, 'reason': 'Late retry'},
                    tool_call_id='fixture-closed-retry')
            with self.assertRaisesRegex(ValueError, 'Root.*(closed|result|terminal)'):
                self.create(room)
        self.assertEqual(self.result(room)['state'], 'completed')
        self.assertEqual(self.post_result(room)['postId'], formal['postId'])
        self.assertEqual(self.service.room_work.list_for_root(room_id=room['id'], root_turn_id=self.root_id), [])

    def test_result_wins_preflight_race_and_prevents_child_admission(self):
        room = self.room()
        self.dispatch(room)
        entered, resume = Event(), Event()
        original = self.service.room_partner_application.room_target_idle
        def idle(*args, **kwargs):
            result = original(*args, **kwargs)
            entered.set()
            self.assertTrue(resume.wait(5))
            return result
        with patch.object(self.service.room_partner_application, 'room_target_idle', side_effect=idle), patch.object(self.service, 'prompt') as prompt, ThreadPoolExecutor(max_workers=1) as executor:
            child = executor.submit(self.delegate, room)
            try:
                self.assertTrue(entered.wait(5))
                self.post_result(room)
            finally:
                resume.set()
            with self.assertRaisesRegex(ValueError, 'Root.*(closed|result|terminal)'):
                child.result(timeout=5)
            prompt.assert_not_called()
        self.assertEqual(self.service.room_work.list_for_root(room_id=room['id'], root_turn_id=self.root_id), [])

    def test_child_admission_wins_and_publication_waits_without_holding_native_prompt(self):
        room = self.room()
        self.dispatch(room)
        entered, resume = Event(), Event()
        def prompt(*args, **kwargs):
            entered.set()
            self.assertTrue(resume.wait(5))
            return {'accepted': True, 'turnId': 'fixture-child-turn'}
        with patch.object(self.service, 'prompt', side_effect=prompt), ThreadPoolExecutor(max_workers=1) as executor:
            child = executor.submit(self.delegate, room)
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaisesRegex(ValueError, 'explicitly accepted'):
                    self.post_result(room)
                self.assertEqual(self.result(room)['state'], 'unknown')
            finally:
                resume.set()
            self.assertEqual(child.result(timeout=5)['status'], 'accepted')

    def test_discussion_unknown_and_fresh_root_still_allow_original_work(self):
        room = self.room()
        self.dispatch(room)
        self.service.execute_room_partner_tool(self.facilitator['sessionId'],
            {'op': 'post', 'kind': 'progress', 'content': 'Discussion remains open'}, tool_call_id='fixture-discussion')
        work = self.create(room)
        self.assertEqual(work['state'], 'active')
        self.assertEqual(self.result(room)['state'], 'unknown')

    def test_closed_prior_root_does_not_block_a_fresh_original_root(self):
        room = self.room()
        self.dispatch(room)
        original_root = self.root_id
        self.post_result(room)
        self.service.room_turns.finish(self.facilitator['sessionId'], 'pi-original', original_root)
        self.service.room_turns.begin(self.facilitator['sessionId'], 'fixture-new-root', dispatch_id='fixture-new-dispatch')
        self.service.room_turns.accept(self.facilitator['sessionId'], 'fixture-new-pi-turn', 'fixture-new-root')
        with patch.object(self.service, 'prompt', return_value={'accepted': True, 'turnId': 'fixture-new-child'}):
            delegated = self.delegate(room, client='fixture-new-root-child')
        self.assertEqual(delegated['status'], 'accepted')
        self.assertEqual(delegated['rootId'], 'fixture-new-root')
        self.assertEqual(self.result(room, original_root)['state'], 'completed')
        self.assertEqual(self.result(room, 'fixture-new-root')['state'], 'unknown')

    def test_store_and_public_work_entry_reject_closed_root_but_keep_unbound_work(self):
        room = self.room()
        self.dispatch(room)
        self.post_result(room)
        payload = {'objective': 'Late public work', 'expectedOutput': 'Late output',
            'currentOwnerParticipantId': self.worker(room)['id'],
            'createdByParticipantId': self.facilitator['id'], 'clientMessageId': 'fixture-public-late',
            'rootTurnId': self.root_id, 'acceptanceCriteria': ['Late checked']}
        with self.assertRaisesRegex(ValueError, 'Root.*closed'):
            self.service.create_room_work_item(room['id'], payload)
        with self.assertRaisesRegex(ValueError, 'Root.*closed'):
            self.service.assign_room_work(self.facilitator['sessionId'], {
                'targetParticipantId': self.worker(room)['id'], 'objective': 'Late assignment',
                'expectedOutput': 'Late output', 'acceptanceCriteria': ['Checked'],
                'clientMessageId': 'fixture-late-assignment'})
        payload['rootTurnId'] = ''
        payload['clientMessageId'] = 'fixture-unbound-work'
        self.assertTrue(self.service.create_room_work_item(room['id'], payload)['ok'])

    def test_stop_wins_before_direct_work_admission(self):
        room = self.room()
        self.dispatch(room)
        with patch.object(self.service, 'abort', return_value={'ok': True, 'sessionId': self.facilitator['sessionId']}):
            self.service.abort_room_turn(room['id'], {'roomTurnId': self.root_id, 'clientRequestId': 'fixture-stop'})
        with self.assertRaisesRegex(ValueError, 'Root.*stopped'):
            self.create(room)
        self.assertEqual(self.result(room)['state'], 'unknown')

    def test_durable_result_rejects_cold_store_and_caller_transaction_admission(self):
        room = self.room()
        self.dispatch(room)
        self.post_result(room)
        cold = AgentRoomWorkStore(self.service.room_work.db_path)
        with patch.object(self.service, 'room_work', cold):
            with self.assertRaisesRegex(ValueError, 'Root.*closed'):
                self.create(room)
        values = dict(room_id=room['id'], objective='Original work', expected_output='Original result',
            current_owner_participant_id=self.worker(room)['id'], created_by_participant_id=self.facilitator['id'],
            client_message_id='fixture-transaction-work', root_turn_id=self.root_id,
            acceptance_criteria=['Checked'])
        # Enlist in the original SQLite owner; do not acquire registry after
        # opening a caller-owned writer transaction (Jev's existing boundary).
        with cold._connect(immediate=True) as conn:
            with self.assertRaisesRegex(ValueError, 'Root.*closed'):
                cold.create(**values, _connection=conn)
            with self.assertRaisesRegex(ValueError, 'Root.*closed'):
                cold.create_root_in_transaction(conn, **values,
                    work_id='fixture-transaction-root', created_at_ms=100)
        self.assertEqual(cold.list_for_root(room_id=room['id'], root_turn_id=self.root_id), [])

    def test_caller_writer_wins_after_result_review_before_publication(self):
        room = self.room()
        self.dispatch(room)
        reviewed, publish = Event(), Event()
        original = self.service.room_events.publish_projection
        def paused(**values):
            reviewed.set()
            self.assertTrue(publish.wait(5))
            return original(**values)
        with patch.object(self.service.room_events, 'publish_projection', side_effect=paused), ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(self.post_result, room)
            try:
                self.assertTrue(reviewed.wait(5))
                cold = AgentRoomWorkStore(self.service.room_work.db_path)
                with cold._connect(immediate=True) as conn:
                    cold.create(room_id=room['id'], objective='Concurrent original work', expected_output='Checked output',
                        current_owner_participant_id=self.worker(room)['id'], created_by_participant_id=self.facilitator['id'],
                        client_message_id='fixture-caller-wins', root_turn_id=self.root_id,
                        acceptance_criteria=['Checked'], _connection=conn)
            finally:
                publish.set()
            with self.assertRaisesRegex(ValueError, 'explicitly accepted'):
                result.result(timeout=5)
        self.assertEqual(self.result(room)['state'], 'unknown')

    def test_result_writer_wins_and_caller_waits_then_rejects_admission(self):
        room = self.room()
        self.dispatch(room)
        guarded, release, caller_attempt = Event(), Event(), Event()
        owner = self.service.room_partner_application
        original = owner._require_accepted_root_work
        def pause_guard(**values):
            original(**values)
            guarded.set()
            self.assertTrue(release.wait(5))
        def caller():
            cold = AgentRoomWorkStore(self.service.room_work.db_path)
            caller_attempt.set()
            with cold._connect(immediate=True) as conn:
                return cold.create(room_id=room['id'], objective='Late original work', expected_output='Late output',
                    current_owner_participant_id=self.worker(room)['id'], created_by_participant_id=self.facilitator['id'],
                    client_message_id='fixture-result-writer-wins', root_turn_id=self.root_id,
                    acceptance_criteria=['Checked'], _connection=conn)
        with patch.object(owner, '_require_accepted_root_work', side_effect=pause_guard), ThreadPoolExecutor(max_workers=2) as executor:
            result = executor.submit(self.post_result, room)
            try:
                self.assertTrue(guarded.wait(5))
                work = executor.submit(caller)
                self.assertTrue(caller_attempt.wait(5))
            finally:
                release.set()
            published = result.result(timeout=5)
            with self.assertRaisesRegex(ValueError, 'Root.*closed'):
                work.result(timeout=5)
        self.assertEqual(self.result(room)['state'], 'completed')
        self.assertEqual(self.post_result(room)['postId'], published['postId'])
        self.assertEqual(self.service.room_work.list_for_root(room_id=room['id'], root_turn_id=self.root_id), [])
