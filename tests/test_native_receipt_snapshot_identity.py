from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from rag_ime.agent_blocks import normalize_trusted_agent_blocks
from tests import test_agent_service as fixtures


class NativeReceiptSnapshotIdentityTests(unittest.TestCase):
    setUp = fixtures.AgentServiceTests.setUp
    tearDown = fixtures.AgentServiceTests.tearDown

    def prepare(self):
        self.sid = self.service.create_session({'title': 'Original receipt fixture', 'runtimeEngine': 'classic'})['session']['id']
        self.service.runtime.config.session_dir.mkdir(parents=True, exist_ok=True)
        for scope, media in [('original', 'media_public_old'), ('replacement', 'media_public_new')]:
            source_id = f'pi:{scope}:same8hex'
            blocks = normalize_trusted_agent_blocks([{'id': 'receipt', 'type': 'file', 'data': {
                'fileName': 'result.txt', 'mediaId': media, 'sessionId': self.sid, 'sha256': 'a' * 64, 'byteSize': 4}}],
                source_kind='pi_runtime_event', source_ref=source_id)
            self.service.agent_blocks.persist_message({'id': source_id, 'sessionId': self.sid, 'turnId': 'turn:public',
                'role': 'assistant', 'status': 'completed', 'blocks': list(blocks), 'attachments': [],
                'citations': [], 'createdAtMs': 101}, native_pi_session_id=scope, native_message_id='same8hex')
        self.bind('original')

    def bind(self, scope, *, rewrite=True):
        path = self.service.runtime.config.session_dir / f'{scope}.jsonl'
        if rewrite:
            path.write_text(json.dumps({'type': 'session', 'id': scope, 'timestamp': '2026-10-09T00:00:00Z'}) + '\n' +
                json.dumps({'type': 'message', 'id': 'same8hex', 'parentId': None, 'timestamp': '2026-10-09T00:00:01Z',
                    'message': {'role': 'assistant', 'timestamp': 101, 'content': [{'type': 'text', 'text': scope + ' native text'}],
                                'stopReason': 'stop'}}) + '\n')
        self.service.sessions.bind_runtime_session(self.sid, driver_id=self.service.runtime.driver_id,
            runtime_kind=self.service.runtime.runtime_kind, external_session_id=scope,
            transcript_ref=str(path), branch_anchor='same8hex')

    def files(self, message):
        return [block['data']['mediaId'] for block in message['blocks'] if block['type'] == 'file']

    def test_recent_snapshot_keeps_original_identity_when_binding_changes_after_native_read(self):
        self.prepare()
        original = self.service.runtime.recent_session_snapshot
        def rebind_after_read(sid):
            result = original(sid)
            self.bind('replacement')
            return result
        with patch('subprocess.Popen', side_effect=AssertionError('No Host permitted')), \
             patch.object(self.service.runtime, 'recent_session_snapshot', side_effect=rebind_after_read):
            result = self.service.message_snapshot.messages(self.sid, view='recent')['items']
        self.assertEqual(result[0]['id'], 'same8hex')
        self.assertEqual(self.files(result[0]), ['media_public_old'])
        self.assertEqual(self.service.sessions.runtime_binding(self.sid)['externalSessionId'], 'replacement')

    def test_full_snapshot_keeps_native_identity_and_recovers_other_pi_with_its_own_id(self):
        self.prepare()
        original = self.service.runtime.session_snapshot
        def rebind_after_read(sid):
            result = original(sid, _allow_host_open=False)
            self.bind('replacement')
            return result
        with patch('subprocess.Popen', side_effect=AssertionError('No Host permitted')), \
             patch.object(self.service.runtime, 'session_snapshot', side_effect=rebind_after_read):
            result = self.service.message_snapshot.messages(self.sid, view='full')['items']
        rows = {message['id']: self.files(message) for message in result}
        self.assertEqual(rows['same8hex'], ['media_public_old'])
        self.assertEqual(rows['pi:replacement:same8hex'], ['media_public_new'])
        self.assertEqual(len(result), 2)

    def test_full_recovers_original_pi_receipt_with_own_id_without_borrowing_current_entry(self):
        self.prepare()
        self.bind('replacement')
        with patch('subprocess.Popen', side_effect=AssertionError('No Host permitted')):
            result = self.service.message_snapshot.messages(self.sid, view='full')['items']
        rows = {message['id']: self.files(message) for message in result}
        self.assertEqual(rows['same8hex'], ['media_public_new'])
        self.assertEqual(rows['pi:original:same8hex'], ['media_public_old'])
        self.assertEqual(len(result), 2)

    def test_legacy_recent_without_snapshot_identity_is_plain_on_binding_change(self):
        self.prepare()
        original = self.service.runtime.recent_session_snapshot
        def legacy_after_read(sid):
            result = original(sid)
            result.pop('nativePiSessionId', None)
            result.pop('piSessionId', None)
            self.bind('replacement')
            return result
        with patch.object(self.service.runtime, 'recent_session_snapshot', side_effect=legacy_after_read):
            result = self.service.message_snapshot.messages(self.sid, view='recent')['items']
        self.assertEqual(self.files(result[0]), [])

    def test_same_pi_rebind_and_legacy_stable_binding_keep_original_receipts(self):
        self.prepare()
        self.bind('original')
        original = self.service.runtime.recent_session_snapshot
        def legacy_stable(sid):
            result = original(sid)
            result.pop('nativePiSessionId', None)
            result.pop('piSessionId', None)
            return result
        with patch.object(self.service.runtime, 'recent_session_snapshot', side_effect=legacy_stable):
            result = self.service.message_snapshot.messages(self.sid, view='recent')['items']
        self.assertEqual(self.files(result[0]), ['media_public_old'])

    def test_internal_recent_rebind_is_unproven_and_does_not_poison_new_cache(self):
        self.prepare()
        # A complete native turn takes the canonical cache-write path.
        path = self.service.runtime.config.session_dir / 'original.jsonl'
        entries = [json.loads(line) for line in path.read_text().splitlines()]
        entries[1]['parentId'] = 'user8hex'
        entries.insert(1, {'type': 'message', 'id': 'user8hex', 'parentId': None,
            'timestamp': '2026-10-09T00:00:00Z', 'message': {'role': 'user', 'content': 'original input', 'timestamp': 100}})
        path.write_text(''.join(json.dumps(entry) + '\n' for entry in entries))
        original = self.service.runtime._recent_durable_history_messages
        rebound = False
        def rebind_inside_reader(sid):
            nonlocal rebound
            result = original(sid)
            if not rebound:
                rebound = True
                self.bind('replacement')
            return result
        with patch('subprocess.Popen', side_effect=AssertionError('No Host permitted')), \
             patch.object(self.service.runtime, '_recent_durable_history_messages', side_effect=rebind_inside_reader):
            first = self.service.runtime.recent_session_snapshot(self.sid)
        self.assertIsNone(first['nativePiSessionId'])
        self.assertTrue(rebound)
        with patch('subprocess.Popen', side_effect=AssertionError('No Host permitted')):
            result = self.service.message_snapshot.messages(self.sid, view='recent')['items']
        current = next(message for message in result if message['id'] == 'same8hex')
        self.assertEqual(self.files(current), ['media_public_new'])
        self.assertEqual([b['data']['text'] for b in current['blocks'] if b['type'] == 'text'], ['replacement native text'])

    def test_legacy_full_binding_change_recovers_owned_rows_without_attaching_to_unknown_native(self):
        self.prepare()
        original = self.service.runtime.session_snapshot
        def legacy_after_read(sid):
            result = original(sid, _allow_host_open=False)
            result.pop('nativePiSessionId', None)
            result.pop('piSessionId', None)
            self.bind('replacement')
            return result
        with patch('subprocess.Popen', side_effect=AssertionError('No Host permitted')), \
             patch.object(self.service.runtime, 'session_snapshot', side_effect=legacy_after_read):
            result = self.service.message_snapshot.messages(self.sid, view='full')['items']
        rows = {message['id']: self.files(message) for message in result}
        self.assertEqual(rows['same8hex'], [])
        self.assertEqual(rows['pi:original:same8hex'], ['media_public_old'])
        self.assertEqual(rows['pi:replacement:same8hex'], ['media_public_new'])
        self.assertEqual(len(result), 3)

    def test_background_cache_rejects_binding_aba_without_relabeling_native_text(self):
        self.prepare()
        self.bind('replacement')
        self.bind('original', rewrite=False)
        identity = self.service.runtime._recent_projection_identity(self.sid)
        original = self.service.runtime._recent_durable_history_messages
        def aba_read(sid):
            self.bind('replacement', rewrite=False)
            result = original(sid)
            self.bind('original', rewrite=False)
            return result
        with patch('subprocess.Popen', side_effect=AssertionError('No Host permitted')), \
             patch.object(self.service.runtime, '_recent_durable_history_messages', side_effect=aba_read):
            self.service.runtime._refresh_recent_message_projection(self.sid, identity)
        with patch('subprocess.Popen', side_effect=AssertionError('No Host permitted')):
            result = self.service.message_snapshot.messages(self.sid, view='recent')['items']
        self.assertEqual([b['data']['text'] for b in result[0]['blocks'] if b['type'] == 'text'], ['original native text'])
        self.assertEqual(self.files(result[0]), ['media_public_old'])
