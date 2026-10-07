from __future__ import annotations

import json
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from rag_ime.agent_protocol import AgentEventEnvelope
from tests.test_coordinator_read_result import CoordinatorReadResultTests


class CoordinatorRoomResultTests(unittest.TestCase):
    setUp = CoordinatorReadResultTests.setUp
    command = CoordinatorReadResultTests.command

    def room(self):
        return self.command('create_room', clientRequestId='original-room-create', input={
            'task': 'Read original evidence', 'routingPolicy': 'manual_mentions',
            'participants': [{'roleId': 'companion-present-v1', 'collaborationRole': 'coordinator'},
                             {'roleId': 'companion-future-v1', 'collaborationRole': 'reviewer'}]})['target']

    def result(self, room, root='original-root'):
        with ExitStack() as guards:
            for name in ('_host', 'prompt', 'resume_session'):
                guards.enter_context(patch.object(self.service.runtime, name, side_effect=AssertionError('read must not execute')))
            guards.enter_context(patch.object(self.service, 'room_artifacts', side_effect=AssertionError('whole Room assets are not original turn evidence')))
            return self.command('read_result', targetId=room['id'], input={'roomTurnId': root})

    def mirror(self, room, *, root='original-root', text='Original public partner answer', role='assistant', status='completed'):
        participant = room['participants'][0]
        session = participant['sessionId']
        turn = root + '-participant'
        self.service.room_turns.begin(session, root, dispatch_id=root + '-dispatch')
        self.service.room_turns.accept(session, turn, root)
        message = AgentEventEnvelope(event_id=root + '-message-event', session_id=session, turn_id=turn,
            sequence=1, event_type='message_completed', created_at_ms=123, resume_token=root + '-message-event',
            payload={'message': {'id': root + '-message', 'role': role, 'blocks': [{'type': 'text', 'data': {'text': text}}]}})
        self.service.event_projection_application.mirror_to_room(message)
        # Exact outer terminal format from mirror_to_room; invoking the live
        # terminal hook itself would additionally notify unrelated owners.
        self.service.rooms.append_event(room_id=room['id'], event_type='turn_completed', turn_id=root,
            participant_id=participant['id'], source_session_id=session,
            payload={'sourceEventId': root + '-terminal-event', 'sourceEventType': 'turn_completed',
                     'data': {'status': status, 'rootId': root, 'sourceTurnId': turn, 'dispatchId': root + '-dispatch'}})

    def publish_final(self, room, *, root='original-root', status='completed', content='Original sole Root report'):
        value = {'finalizationId': 'jev-final:' + root, 'content': content, 'status': status, 'createdAtMs': 456}
        snapshot = SimpleNamespace(graph_id=root, room_id=room['id'], root_id=root,
                                   participant_id=room['participants'][0]['id'])
        lifecycle = self.service.jev_application.lifecycle
        # Real publication owner into real SQLite, with an already-finalized
        # policy fixture. This does not execute or simulate Root settlement.
        with patch.object(lifecycle, 'policy', return_value={'final_json': json.dumps(value), 'stopped': 0, 'epoch': 1}):
            lifecycle.publish_final(snapshot)

    def test_actual_mirror_payload_preserves_reply_and_known_participant_status_without_root_completion(self):
        room = self.room()
        self.mirror(room)
        result = self.result(room)
        self.assertEqual(result['finalMessages'][0]['messageId'], 'original-root-message')
        self.assertEqual(result['finalMessages'][0]['text'], 'Original public partner answer')
        self.assertEqual(result['terminalRefs'][0]['status'], 'completed')
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(result['artifacts'], [])

    def test_actual_root_publication_retains_report_and_explicit_original_root_terminal(self):
        room = self.room()
        self.publish_final(room)
        self.publish_final(room, root='new-unrelated-root', content='Other report')
        result = self.result(room)
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['finalMessages'][0]['messageId'], 'jev-final:original-root')
        self.assertEqual(result['finalMessages'][0]['text'], 'Original sole Root report')
        self.assertEqual(result['terminalRefs'][0]['status'], 'completed')
        self.assertEqual(len(result['terminalRefs']), 1)
        self.assertTrue(result['evidenceOnly'])
        self.assertEqual(result['artifacts'], [])

    def test_late_root_report_and_terminal_survive_more_than_100_stream_events(self):
        room = self.room()
        self.service.rooms.append_event(room_id=room['id'], event_type='user_message', turn_id='original-root',
                                       payload={'text': 'Original task', 'clientMessageId': 'original-input'})
        for index in range(105):
            self.service.rooms.append_event(room_id=room['id'], event_type='participant_activity',
                turn_id='original-root', payload={'status': 'running', 'index': index})
        self.publish_final(room)
        result = self.result(room)
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['finalMessages'][0]['text'], 'Original sole Root report')
        self.assertEqual(len(result['terminalRefs']), 1)
        self.assertIsNotNone(result['acceptanceRef'])

    def test_only_explicit_retained_root_disposition_is_known(self):
        room = self.room()
        for status in ('', 'future-status', 'failed', 'aborted'):
            root = 'root-' + (status or 'missing')
            self.service.rooms.append_event(room_id=room['id'], event_type='turn_completed', turn_id=root,
                payload={'rootId': root, 'finalizationId': 'jev-final:' + root, 'status': status})
            result = self.result(room, root)
            self.assertEqual(result['state'], status if status in {'failed', 'aborted'} else 'unknown')
            self.assertEqual(result['terminalRefs'][0]['status'], status)
            self.assertNotIn('user_stopped', json.dumps(result))
        self.service.rooms.append_event(room_id=room['id'], event_type='turn_completed', turn_id='no-root-proof', payload={'status': 'completed'})
        self.assertEqual(self.result(room, 'no-root-proof')['state'], 'unknown')
        self.service.rooms.append_event(room_id=room['id'], event_type='turn_completed', turn_id='wrong-root-proof',
            payload={'status': 'completed', 'rootId': 'another', 'finalizationId': 'jev-final:another'})
        self.assertEqual(self.result(room, 'wrong-root-proof')['state'], 'unknown')

    def test_actual_and_legacy_messages_preserve_latest_bounded_text_without_other_turn(self):
        room = self.room()
        for index in range(110):
            self.service.rooms.append_event(room_id=room['id'], event_type='participant_message', turn_id='original-root',
                payload={'text': f'Original legacy {index}'})
        self.mirror(room, text='p' * 6000)
        self.publish_final(room, content='r' * 6000)
        self.mirror(room, root='new-root', text='New unrelated answer')
        result = self.result(room)
        self.assertEqual(result['state'], 'completed')
        self.assertLessEqual(len(result['finalMessages']), 8)
        self.assertEqual(sum(len(message['text']) for message in result['finalMessages']), 8000)
        self.assertEqual(result['finalMessages'][-1]['text'], 'r' * 6000)
        self.assertTrue(result['truncated'])
        self.assertNotIn('New unrelated answer', json.dumps(result))

    def test_nonassistant_mirror_does_not_become_original_final_answer(self):
        room = self.room()
        self.mirror(room, role='user', text='User input is not a final reply')
        result = self.result(room)
        self.assertEqual(result['finalMessages'], [])
        self.assertEqual(result['state'], 'unknown')

    def test_root_terminal_reference_is_not_hidden_by_many_participant_terminals(self):
        room = self.room()
        for late in (False, True):
            root = 'late-partners' if late else 'early-partners'
            if late:
                self.publish_final(room, root=root)
            for index in range(20):
                self.service.rooms.append_event(room_id=room['id'], event_type='turn_completed', turn_id=root,
                    participant_id=room['participants'][0]['id'], payload={'status': 'completed', 'sourceTurnId': f'partner-{index}'})
            if not late:
                self.publish_final(room, root=root)
            result = self.result(room, root)
            self.assertEqual(result['state'], 'completed')
            self.assertLessEqual(len(result['terminalRefs']), 16)
            roots = [ref for ref in result['terminalRefs'] if ref.get('finalizationId') == 'jev-final:' + root]
            self.assertEqual(len(roots), 1)
            self.assertIsNone(roots[0]['participantId'])
            self.assertTrue(result['truncated'])

    def test_public_root_report_keeps_a_budgeted_slot_after_more_than_eight_late_partner_messages(self):
        room = self.room()
        report = 'Original Root report ' * 100
        self.publish_final(room, content=report)
        for index in range(12):
            self.service.rooms.append_event(room_id=room['id'], event_type='participant_message', turn_id='original-root',
                payload={'text': f'Late partner {index} ' + 'p' * 1500})
        result = self.result(room)
        roots = [message for message in result['finalMessages'] if message['messageId'] == 'jev-final:original-root']
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0]['text'], report)
        self.assertLessEqual(len(result['finalMessages']), 8)
        self.assertEqual(sum(len(message['text']) for message in result['finalMessages']), 8000)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['state'], 'completed')

    def test_message_pages_have_one_fixed_read_boundary_while_same_turn_keeps_growing(self):
        room = self.room()
        self.publish_final(room)
        for index in range(104):
            self.service.rooms.append_event(room_id=room['id'], event_type='participant_message', turn_id='original-root',
                payload={'text': f'Before snapshot {index}'})
        boundary = self.service.rooms.get(room['id'])['lastEventSequence']
        original = self.service.rooms.list_events_for_turn
        message_pages = []
        def growing(room_id, turn_id, **options):
            if options.get('event_types') != ('participant_message', 'room_post'):
                return original(room_id, turn_id, **options)
            message_pages.append(options.get('through_sequence'))
            if len(message_pages) > 4:
                raise AssertionError('read keeps chasing newly appended original-turn messages')
            page = original(room_id, turn_id, **options)
            for index in range(100):
                self.service.rooms.append_event(room_id=room_id, event_type='participant_message', turn_id=turn_id,
                    payload={'text': f'After snapshot {len(message_pages)}-{index}'})
            return page
        with patch.object(self.service.rooms, 'list_events_for_turn', side_effect=growing):
            result = self.result(room)
        self.assertEqual(message_pages, [boundary, boundary])
        self.assertNotIn('After snapshot', json.dumps(result))
        self.assertEqual(result['state'], 'completed')
        self.assertTrue(any(message['messageId'] == 'jev-final:original-root' for message in result['finalMessages']))

    def test_root_control_and_report_published_after_read_start_wait_for_next_read(self):
        room = self.room()
        original = self.service.rooms.control_events_for_turn
        boundary = self.service.rooms.get(room['id'])['lastEventSequence']
        boundaries = []
        def publish_after_snapshot(room_id, turn_id, **options):
            boundaries.append(options.get('through_sequence'))
            self.publish_final(room)
            return original(room_id, turn_id, **options)
        with patch.object(self.service.rooms, 'control_events_for_turn', side_effect=publish_after_snapshot):
            result = self.result(room)
        self.assertEqual(boundaries, [boundary])
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(result['terminalRefs'], [])
        self.assertEqual(result['finalMessages'], [])
        later = self.result(room)
        self.assertEqual(later['state'], 'completed')
        self.assertEqual(later['finalMessages'][0]['text'], 'Original sole Root report')
