from __future__ import annotations

import json
import copy
import hashlib
import unittest
from unittest.mock import patch

from rag_ime.agent_blocks import normalize_trusted_agent_blocks
from tests import test_agent_service as fixtures


class DurableExactReceiptSnapshotTests(unittest.TestCase):
    """Real Store/projector; the native wire is explicitly a deterministic double."""
    setUp = fixtures.AgentServiceTests.setUp
    tearDown = fixtures.AgentServiceTests.tearDown

    def prepare(self):
        self.sid = self.service.sessions.create(title='Public Durable receipts', runtime_engine='durable')['id']
        self.native = 'public-durable-native'
        self.message_id = 'durable:task:47:assistant'
        self.turn = 'public-original-turn'
        self.client = 'public-original-client'
        self.bind()
        seed = b'PAW paired receipt fixture bf336d6e.\nOriginal seed must stay unchanged.\n'
        final = seed + b'PAW_DURABLE_BF336_durable_bf336_run_1\n'
        # The public seed/final bytes match the real native witness; diff bodies
        # are sanitized public data. Each version retains its own managed ID/SHA.
        data = [('result.txt', seed), ('result.txt', final), ('result.txt.diff', b'public original diff\n'), ('result.txt.diff', b'public final diff\n')]
        raw = [{'id': f'file:{i}', 'type': 'file', 'data': {
            'fileName': name, 'mediaId': f'media_public_receipt_{i:016d}', 'sessionId': self.sid,
            'sha256': hashlib.sha256(content).hexdigest(), 'byteSize': len(content)}} for i, (name, content) in enumerate(data)]
        self.blocks = list(normalize_trusted_agent_blocks(raw, source_kind='pi_runtime_event', source_ref=self.message_id))
        self.message = {'id': self.message_id, 'sessionId': self.sid, 'turnId': self.turn,
            'clientMessageId': self.client, 'role': 'assistant', 'status': 'completed',
            'blocks': [{'type': 'text', 'data': {'text': 'Original native answer'}}, *self.blocks],
            'attachments': [], 'citations': [], 'createdAtMs': 101}
        self.service.agent_blocks.persist_message(self.message, generation=0)
        self.snapshot = {'runtimeEngine': 'durable', 'nativePiSessionId': self.native,
            'projectionCurrent': True, 'paused': False, 'recoverable': False,
            'messages': [{**self.message, 'blocks': [self.message['blocks'][0]]}]}

    def bind(self, native=None):
        self.service.sessions.bind_runtime_session(self.sid, driver_id='managed-pi', runtime_kind='pi_durable',
            external_session_id=native or self.native, transcript_ref=str(self.root / 'public-durable-store'),
            metadata={'runtimeEngine': 'durable', 'durableConversationId': '1'})

    def read(self, *, view='recent', after_read=None):
        def snapshot(_sid):
            result = copy.deepcopy(self.snapshot)
            if after_read: after_read()
            return result
        owner = 'recent_session_snapshot' if view == 'recent' else 'session_snapshot'
        with patch('subprocess.Popen', side_effect=AssertionError('No Host/Provider permitted')), \
             patch.object(self.service.runtime, owner, side_effect=snapshot):
            return self.service.message_snapshot.messages(self.sid, view=view)['items']

    def test_original_durable_recent_exact_message_retains_all_four_versions_without_alias(self):
        self.prepare()
        actual = self.read()
        self.assertEqual(len(actual), 1)
        self.assertEqual(actual[0]['id'], self.message_id)
        self.assertCountEqual([b['data'] for b in actual[0]['blocks'] if b['type'] == 'file'], [b['data'] for b in self.blocks])
        self.assertEqual(actual[0]['blocks'][0], self.message['blocks'][0])
        with self.service.agent_blocks._connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM agent_block_native_aliases WHERE session_id=?', (self.sid,)).fetchone()[0], 0)

    def test_durable_rebind_and_binding_epoch_aba_cannot_attach_old_synthetic_entry(self):
        self.prepare()
        for change in (lambda: self.bind('replacement'), lambda: (self.bind('replacement'), self.bind())):
            with self.subTest(change=change):
                self.bind()
                actual = self.read(after_read=change)
                self.assertFalse(any(b['type'] == 'file' for b in actual[0]['blocks']))

    def test_unknown_or_mixed_or_non_durable_native_snapshot_has_no_exact_fallback(self):
        self.prepare()
        original = copy.deepcopy(self.snapshot)
        for change in ('classic', 'missingidentity', 'mismatchidentity', 'notcurrent', 'mixed', 'foreignsession', 'unknownentry', 'wrongturn', 'wrongclient'):
            with self.subTest(change=change):
                self.snapshot = copy.deepcopy(original)
                if change == 'classic': self.snapshot['runtimeEngine'] = 'classic'
                elif change == 'missingidentity': self.snapshot['nativePiSessionId'] = None
                elif change == 'mismatchidentity': self.snapshot['nativePiSessionId'] = 'other'
                elif change == 'notcurrent': self.snapshot['projectionCurrent'] = False
                elif change == 'mixed': self.snapshot['messages'].append({**self.snapshot['messages'][0], 'id': 'pi:other:47'})
                elif change == 'foreignsession': self.snapshot['messages'][0]['sessionId'] = 'foreign'
                elif change == 'unknownentry': self.snapshot['messages'][0]['id'] = 'durable:task:999:assistant'
                elif change == 'wrongturn': self.snapshot['messages'][0]['turnId'] = 'other-turn'
                else: self.snapshot['messages'][0]['clientMessageId'] = 'other-client'
                for view in ('recent', 'full'):
                    self.assertFalse(any(b['type'] == 'file' for m in self.read(view=view) for b in m['blocks']))

    def test_generation_revoke_and_adjacent_receipts_do_not_borrow_or_restore(self):
        self.prepare()
        adjacent = {**self.message, 'id': 'durable:task:99:assistant'}
        self.service.agent_blocks.persist_message(adjacent, generation=0)
        actual = self.read()
        self.assertEqual([m['id'] for m in actual], [self.message_id])
        ref = self.service.agent_blocks.blocks_for_message(self.sid, self.message_id, generation=0)[0]['ref']
        self.service.agent_blocks.revoke(ref, root_id=f'session:{self.sid}', session_id=self.sid)
        self.assertEqual(len([b for b in self.read()[0]['blocks'] if b['type'] == 'file']), 3)
        newer = {**self.message, 'turnId': 'different-generation-turn', 'clientMessageId': 'different-generation-client'}
        self.service.agent_blocks.persist_message(newer, generation=1)
        actual = self.read()
        self.assertEqual(len([b for b in actual[0]['blocks'] if b['type'] == 'file']), 3)

    def test_original_durable_full_attaches_visible_receipts_without_inventing_history(self):
        self.prepare()
        self.service.agent_blocks.persist_message({**self.message, 'id': 'durable:task:99:assistant'}, generation=0)
        actual = self.read(view='full')
        self.assertEqual([m['id'] for m in actual], [self.message_id])
        self.assertEqual(len([b for b in actual[0]['blocks'] if b['type'] == 'file']), 4)

    def test_unverified_binding_metadata_and_epoch_never_grant_exact_hydration(self):
        self.prepare()
        original = self.service.sessions.runtime_binding(self.sid)
        for key, value in (('state', 'stale'), ('runtimeKind', 'pi'), ('generation', None),
                           ('generation', True), ('metadata', {}), ('sessionId', 'foreign')):
            with self.subTest(key=key, value=value), patch.object(self.service.sessions, 'runtime_binding', return_value={**original, key: value}):
                for view in ('recent', 'full'):
                    self.assertFalse(any(b['type'] == 'file' for m in self.read(view=view) for b in m['blocks']))


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
