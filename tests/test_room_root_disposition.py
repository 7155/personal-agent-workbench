from __future__ import annotations

import unittest
from unittest.mock import patch

from rag_ime.agent_protocol import AgentEventEnvelope
from tests import test_coordinator_read_result as fixtures


class RoomRootDispositionTests(unittest.TestCase):
    setUp = fixtures.CoordinatorReadResultTests.setUp
    command = fixtures.CoordinatorReadResultTests.command

    def room(self):
        return self.command('create_room', clientRequestId='original-room', input={
            'task': 'Return original Room work', 'routingPolicy': 'manual_mentions',
            'participants': [{'roleId': 'companion-present-v1', 'collaborationRole': 'coordinator'},
                             {'roleId': 'companion-future-v1', 'collaborationRole': 'reviewer'}]})['target']

    def dispatch(self, room):
        self.facilitator = next(p for p in room['participants'] if p['id'] == room['moderatorParticipantId'])
        # Only the Pi transport-side prompt result is a fixture. The real
        # ingress, receipt, Room dispatch, registry and event owners run.
        with patch.object(self.service, 'prompt', return_value={'accepted': True, 'turnId': 'pi-original'}) as prompt:
            accepted = self.service.post_room_message(room['id'], {'message': 'Original work',
                'clientMessageId': 'client-original', 'participantIds': [self.facilitator['id']]})
        self.assertTrue(accepted['accepted'])
        self.assertEqual(accepted['executionOwner'], 'session')
        self.assertEqual(prompt.call_count, 1)
        self.root_id = accepted['roomTurnId']
        return accepted

    def result(self, room, root=None):
        with patch.object(self.service.runtime, '_host', side_effect=AssertionError('read must not open Host')):
            return self.command('read_result', targetId=room['id'], input={'roomTurnId': root or self.root_id})

    def post_result(self, room, content='Original reviewed Root report', tool='final-original'):
        return self.service.execute_room_partner_tool(self.facilitator['sessionId'],
            {'op': 'post', 'kind': 'result', 'content': content}, tool_call_id=tool)

    def test_native_ingress_and_facilitator_root_report_are_read_without_jev_policy(self):
        room = self.room()
        accepted = self.dispatch(room)
        self.assertEqual(self.result(room)['state'], 'unknown')
        published = self.post_result(room)
        result = self.result(room)
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['execution'], {'roomId': room['id'], 'roomTurnId': self.root_id})
        self.assertEqual(result['acceptanceRef']['eventId'], accepted['timelineEvents'][0]['eventId'])
        self.assertEqual(result['finalMessages'][0]['messageId'], published['postId'])
        ref = next(ref for ref in result['terminalRefs'] if ref.get('rootDisposition') == 'result')
        self.assertEqual(ref['eventType'], 'room_post')
        self.assertEqual(ref['participantId'], self.facilitator['id'])
        self.assertEqual(ref['sourceSessionId'], self.facilitator['sessionId'])
        self.assertEqual(ref['postId'], published['postId'])
        self.post_result(room, content='Replay must not replace original content')
        self.assertEqual(self.result(room)['finalMessages'][0]['text'], 'Original reviewed Root report')
        self.assertEqual(self.result(room)['terminalRefs'], result['terminalRefs'])

    def test_member_terminal_and_review_pending_do_not_complete_root(self):
        room = self.room()
        self.dispatch(room)
        worker = next(p for p in room['participants'] if p['id'] != self.facilitator['id'])
        work = self.service.room_work.create(room_id=room['id'], objective='Original task', expected_output='Actual result',
            current_owner_participant_id=worker['id'], accountable_participant_id=self.facilitator['id'],
            created_by_participant_id=self.facilitator['id'], client_message_id='work-original', root_turn_id=self.root_id, acceptance_criteria=['Original output works'])
        with self.assertRaisesRegex(ValueError, 'explicitly accepted'):
            self.post_result(room)
        submitted = self.service.room_work.submit(worker['sessionId'], {'workId': work['id'],
            'resultSummary': 'Original child result', 'evidenceRefs': ['original-check']})
        with self.assertRaisesRegex(ValueError, 'explicitly accepted'):
            self.post_result(room)
        self.assertEqual(self.result(room)['state'], 'unknown')
        self.service.room_work.accept(self.facilitator['sessionId'], {'workId': work['id'],
            'expectedRevision': submitted['revision'], 'operabilityVerdict': 'passed',
            'requirementVerdict': 'satisfied', 'evidenceRefs': ['original-check'], 'reason': 'Checked original work'})
        self.post_result(room)
        self.assertEqual(self.result(room)['state'], 'completed')

    def test_actual_member_event_projection_remains_unknown_and_foreign_root_cannot_finish_original(self):
        room = self.room()
        self.dispatch(room)
        event = AgentEventEnvelope(event_id='pi-original-ended', session_id=self.facilitator['sessionId'],
            turn_id='pi-original', sequence=1, event_type='turn_completed', created_at_ms=100,
            resume_token='pi-original-ended', payload={'status': 'completed'})
        self.service.event_projection_application.mirror_to_room(event)
        result = self.result(room)
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(result['terminalRefs'][0]['participantId'], self.facilitator['id'])
        # The native registry is still used: publishing an explicit result
        # for a different original Root does not settle this one.
        self.service.room_turns.begin(self.facilitator['sessionId'], 'foreign-root', dispatch_id='foreign-dispatch')
        self.service.room_turns.accept(self.facilitator['sessionId'], 'pi-foreign', 'foreign-root')
        self.post_result(room)
        self.assertEqual(self.result(room)['state'], 'unknown')
        self.assertEqual(self.result(room, 'foreign-root')['state'], 'completed')

    def test_stop_and_late_participant_terminal_do_not_invent_root_result(self):
        room = self.room()
        self.dispatch(room)
        # Unknown Stop acknowledgement cannot prove native drain. The real
        # Room cancellation/fan-out owner runs, with only Pi abort as a fixture.
        with patch.object(self.service, 'abort', return_value={'ok': True, 'sessionId': self.facilitator['sessionId']}) as abort:
            receipt = self.service.abort_room_turn(room['id'], {'roomTurnId': self.root_id, 'clientRequestId': 'stop-original'})
        self.assertEqual(abort.call_count, 1)
        self.assertNotEqual(receipt['status'], 'terminated')
        self.assertEqual(self.result(room)['state'], 'unknown')
        with self.assertRaisesRegex(ValueError, 'stopped before result'):
            self.post_result(room)
        event = AgentEventEnvelope(event_id='late-original', session_id=self.facilitator['sessionId'],
            turn_id='pi-original', sequence=1, event_type='turn_completed', created_at_ms=100,
            resume_token='late-original', payload={'status': 'completed'})
        self.service.event_projection_application.mirror_to_room(event)
        self.assertEqual(self.result(room)['state'], 'unknown')

    def test_unreceipted_result_and_corrupted_projection_are_unknown(self):
        room = self.room()
        self.dispatch(room)
        post = {'schemaVersion': 'wisdom-weasel.room-post.v2', 'postId': 'untrusted-result',
            'roomId': room['id'], 'rootId': self.root_id, 'generation': 0, 'dispatchId': 'untrusted',
            'authorActorRef': self.facilitator['id'], 'kind': 'result', 'visibility': 'room',
            'content': 'Label alone is not formal Root disposition', 'idempotencyKey': 'untrusted',
            'publicationSource': {'kind': 'room_post', 'ref': 'untrusted'}, 'createdAtMs': 100}
        self.service.rooms.append_event(room_id=room['id'], event_type='room_post', payload={'post': post},
            turn_id=self.root_id, participant_id=self.facilitator['id'], source_session_id=self.facilitator['sessionId'])
        self.assertEqual(self.result(room)['state'], 'unknown')
        # Deliberately malformed owned test data, never a user database.
        with self.service.rooms._connect() as conn:
            conn.execute('INSERT INTO agent_room_public_projection_receipts(projection_key,room_id,event_id,payload_hash,created_at_ms) VALUES (?,?,?,?,?)',
                (f"room-terminal-result:{room['id']}:{self.root_id}", room['id'], f"{room['id']}:4", '0' * 64, 100))
        self.assertEqual(self.result(room)['state'], 'unknown')

    def test_original_result_recovers_after_display_pruning_without_republishing(self):
        room = self.room()
        self.dispatch(room)
        original_watermark = self.service.rooms.get(room['id'])['lastEventSequence']
        self.post_result(room)
        self.assertIsNone(self.service.rooms.root_result_for_turn(room['id'], self.root_id, through_sequence=original_watermark))
        for index in range(110):
            self.service.rooms.append_event(room_id=room['id'], event_type='participant_activity',
                turn_id='later-root', payload={'index': index}, retain_per_room=100)
        original_snapshot = self.service.rooms.get(room['id'])['lastEventSequence']
        # New reader instance shares only the original durable Room data.
        from rag_ime.rooms.store import AgentRoomStore
        reopened = AgentRoomStore(self.service.sessions.db_path)
        self.addCleanup(reopened.close)
        formal = reopened.root_result_for_turn(room['id'], self.root_id, through_sequence=original_snapshot)
        self.assertIsNotNone(formal)
        result = self.result(room)
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['finalMessages'][0]['text'], 'Original reviewed Root report')
        self.assertEqual(self.service.rooms.get(room['id'])['lastEventSequence'], original_snapshot)

    def test_stop_winning_during_review_read_blocks_late_original_result(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        room = self.room()
        self.dispatch(room)
        read_entered, finish_read = Event(), Event()
        original = self.service.room_partner_application._accepted_root_work
        first = True
        def read(*args, **kwargs):
            nonlocal first
            result = original(*args, **kwargs)
            if first:
                first = False
                read_entered.set()
                self.assertTrue(finish_read.wait(5))
            return result
        with patch.object(self.service.room_partner_application, '_accepted_root_work', side_effect=read), ThreadPoolExecutor(max_workers=1) as executor:
            publication = executor.submit(self.post_result, room)
            try:
                self.assertTrue(read_entered.wait(5))
                with patch.object(self.service, 'abort', return_value={'ok': True}):
                    self.service.abort_room_turn(room['id'], {'roomTurnId': self.root_id, 'clientRequestId': 'latch-stop'})
            finally:
                finish_read.set()
            with self.assertRaisesRegex(ValueError, 'stopped before result'):
                publication.result(timeout=5)
        self.assertFalse(self.service.room_events.has_projection(f"room-terminal-result:{room['id']}:{self.root_id}"))
        self.assertEqual(self.result(room)['state'], 'unknown')

    def test_unsettled_child_blocks_root_even_after_work_is_reviewed(self):
        room = self.room()
        accepted = self.dispatch(room)
        worker = next(p for p in room['participants'] if p['id'] != self.facilitator['id'])
        work = self.service.room_work.create(room_id=room['id'], objective='Original task', expected_output='Actual result',
            current_owner_participant_id=worker['id'], accountable_participant_id=self.facilitator['id'],
            created_by_participant_id=self.facilitator['id'], client_message_id='pending-child-work', root_turn_id=self.root_id,
            acceptance_criteria=['Original output works'])
        submitted = self.service.room_work.submit(worker['sessionId'], {'workId': work['id'],
            'resultSummary': 'Original child result', 'evidenceRefs': ['original-check']})
        self.service.room_work.accept(self.facilitator['sessionId'], {'workId': work['id'],
            'expectedRevision': submitted['revision'], 'operabilityVerdict': 'passed', 'requirementVerdict': 'satisfied',
            'evidenceRefs': ['original-check'], 'reason': 'Checked original work'})
        self.service.room_partner_dispatches.register(child_dispatch_id='original-child', room_id=room['id'],
            root_id=self.root_id, parent_dispatch_id=accepted['dispatches'][0]['dispatchId'], tool_call_id='delegate-original',
            source_participant_id=self.facilitator['id'], source_session_id=self.facilitator['sessionId'],
            target_participant_id=worker['id'], target_session_id=worker['sessionId'], work_item_id=work['id'])
        with self.assertRaisesRegex(ValueError, 'explicitly accepted'):
            self.post_result(room)
        self.assertEqual(self.result(room)['state'], 'unknown')

    def test_existing_accepted_work_recovery_publishes_readable_original_root(self):
        room = self.room()
        accepted = self.dispatch(room)
        worker = next(p for p in room['participants'] if p['id'] != self.facilitator['id'])
        work = self.service.room_work.create(room_id=room['id'], objective='Original task', expected_output='Actual result',
            current_owner_participant_id=worker['id'], accountable_participant_id=self.facilitator['id'],
            created_by_participant_id=self.facilitator['id'], client_message_id='recovered-work', root_turn_id=self.root_id,
            acceptance_criteria=['Original output works'])
        submitted = self.service.room_work.submit(worker['sessionId'], {'workId': work['id'],
            'resultSummary': 'Recovered original child result', 'evidenceRefs': ['original-check']})
        self.service.room_work.accept(self.facilitator['sessionId'], {'workId': work['id'],
            'expectedRevision': submitted['revision'], 'operabilityVerdict': 'passed', 'requirementVerdict': 'satisfied',
            'evidenceRefs': ['original-check'], 'reason': 'Checked original work'})
        record = {'roomId': room['id'], 'rootId': self.root_id, 'sourceParticipantId': self.facilitator['id'],
            'parentDispatchId': accepted['dispatches'][0]['dispatchId'], 'wake': {'generation': 4}}
        schedule = {'id': 'original-finalization-wake', 'latestRun': {'runId': 'original-finalization-run', 'finishedAtMs': 200}}
        owner = self.service.room_partner_application
        self.assertTrue(owner._project_terminal_result_from_accepted_work(record, schedule,
            session_id=self.facilitator['sessionId'], turn_id='pi-original-finalization'))
        self.assertEqual(self.result(room)['state'], 'completed')
        self.assertIn('Recovered original child result', self.result(room)['finalMessages'][0]['text'])
        self.assertFalse(owner._project_terminal_result_from_accepted_work(record, schedule,
            session_id=self.facilitator['sessionId'], turn_id='pi-original-finalization'))
