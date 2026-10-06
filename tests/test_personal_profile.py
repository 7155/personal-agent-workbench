from __future__ import annotations

import sqlite3
import unittest
from contextlib import nullcontext
from unittest.mock import Mock

from rag_ime.db import apply_database_migrations
from rag_ime.memory_card_mutations import MemoryRevisionConflict, card_revision, correct_memory_card
from rag_ime.personal_profile import read_personal_profile, save_personal_profile
from rag_ime.query_expansion import _matched_aliases_and_atoms, build_query_expansion


class PersonalProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with sqlite3.connect(':memory:') as conn:
            apply_database_migrations(conn)
            cls.pristine = conn.serialize()

    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.deserialize(self.pristine)
        self.conn.execute('PRAGMA foreign_keys=ON')
        self.addCleanup(self.conn.close)

    def save(self, paragraphs, request='save-1', revision=None):
        return save_personal_profile(self.conn, {
            'expectedRevision': revision or read_personal_profile(self.conn)['revision'],
            'clientRequestId': request, 'paragraphs': paragraphs,
        }, timestamp=100)

    def add(self, text='我喜欢简短直接的回复。'):
        result = self.save([{'id': None, 'memoryIds': [], 'text': text}])
        return result['profile']['paragraphs'][0]

    def test_profile_discloses_evidence_omitted_by_the_reference_cap(self):
        from rag_ime.memory_card_mutations import record_personal_edit_source
        first = self.add()
        for index in range(35):
            _, evidence_id = record_personal_edit_source(self.conn, f'公开测试背景来源 {index}', timestamp=200 + index)
            self.conn.execute('INSERT INTO memory_lifecycle_atom_evidence_links VALUES (?,?,?,?)',
                              (first['id'], evidence_id, 'context', 200 + index))
        paragraph = read_personal_profile(self.conn)['paragraphs'][0]
        self.assertEqual(paragraph['sourceCount'], 36)
        self.assertEqual(len(paragraph['sourceRefs']), 32)
        self.assertTrue(paragraph['sourceRefsTruncated'])

    def test_merge_retains_the_stronger_tag_and_inherited_context(self):
        from rag_ime.memory_card_mutations import card_source_refs, correct_memory_card, merge_memory_cards
        from rag_ime.personal_profile import _new_personal_card
        target = self.add()
        source = _new_personal_card(self.conn, text='公开测试：中文沟通。', mutation_id='context-source', timestamp=110)
        source = correct_memory_card(self.conn, source['memoryId'], text='公开测试：继续使用中文。',
            expected_revision=source['revision'], timestamp=120, mutation_id='source-correction', reason='user_correction', user_edit=True)
        source_refs = card_source_refs(self.conn, source['memoryId'], include_context=True)
        self.assertEqual(len(source_refs), 2)
        tag_id = self.conn.execute("INSERT INTO memory_tags(tag,normalized_tag,created_at_ms,updated_at_ms) VALUES ('语言','语言',100,100)").lastrowid
        self.conn.executemany('INSERT INTO memory_atom_tags VALUES (?,?,?,?)',
            [(target['id'], tag_id, .2, 'offline'), (source['memoryId'], tag_id, .9, 'offline')])
        result = merge_memory_cards(self.conn, source['memoryId'], target['id'],
            expected_revision=source['revision'], expected_target_revision=target['revision'], timestamp=200, mutation_id='context-merge')
        weight, origin = self.conn.execute('SELECT weight,source FROM memory_atom_tags WHERE memory_atom_id=? AND tag_id=?',
                                           (result['memoryId'], tag_id)).fetchone()
        with self.subTest('stronger tag'):
            self.assertEqual((weight, origin), (.9, 'user_merge'))
        with self.subTest('inherited evidence'):
            self.assertTrue({ref['id'] for ref in source_refs}.issubset(ref['id'] for ref in card_source_refs(self.conn, result['memoryId'], include_context=True)))

    def test_profile_add_edit_retry_conflict_and_sources(self):
        first = self.add()
        before = read_personal_profile(self.conn)
        request = {'expectedRevision': before['revision'], 'clientRequestId': 'edit-1',
                   'paragraphs': [{**first, 'text': '我喜欢有解释的回复。'}]}
        result = save_personal_profile(self.conn, request, timestamp=200)
        current = result['profile']['paragraphs'][0]
        self.assertNotEqual(first['id'], current['id'])
        self.assertEqual(current['sourceCount'], 2)
        self.assertEqual(result, save_personal_profile(self.conn, request, timestamp=201))
        with self.assertRaises(MemoryRevisionConflict):
            self.save([{**first, 'text': '过期写入。'}], 'stale', before['revision'])
        self.assertEqual(read_personal_profile(self.conn)['text'], '我喜欢有解释的回复。')

    def test_profile_correction_preserves_alias_lookup_history_and_replay(self):
        first = self.add()
        aliases = [
            ('alias:profile', first['id'], '回复习惯', 'semantic', 'huifu xiguan', 0.8, 50),
            ('alias:profile:short', first['id'], 'hf xg', 'abbreviation', None, 0.3, 60),
        ]
        self.conn.executemany('INSERT INTO memory_aliases VALUES (?,?,?,?,?,?,?)', aliases)
        request = {'expectedRevision': read_personal_profile(self.conn)['revision'],
                   'clientRequestId': 'edit-alias',
                   'paragraphs': [{**first, 'text': '我喜欢有解释的回复。'}]}
        result = save_personal_profile(self.conn, request, timestamp=200)
        current = result['profile']['paragraphs'][0]
        matched, atom_ids = _matched_aliases_and_atoms(self.conn, query_terms=('回复习惯',),
                                                     visible_owners=(('user', 'default'),))
        self.assertEqual(atom_ids, [current['id']])
        self.assertNotEqual(current['id'], first['id'])
        self.assertEqual(matched, ['回复习惯', 'hf xg'])
        expansion = build_query_expansion(self.conn, query_text='回复习惯')
        self.assertEqual(expansion.matched_aliases, ('回复习惯', 'hf xg'))
        historical = self.conn.execute('SELECT * FROM memory_aliases WHERE memory_atom_id=? ORDER BY weight DESC',
                                       (first['id'],)).fetchall()
        self.assertEqual([tuple(row) for row in historical], aliases)
        inherited = self.conn.execute('SELECT * FROM memory_aliases WHERE memory_atom_id=? ORDER BY weight DESC',
                                      (current['id'],)).fetchall()
        self.assertEqual([tuple(row)[2:] for row in inherited], [row[2:] for row in aliases])
        self.assertTrue({row['id'] for row in inherited}.isdisjoint(row[0] for row in aliases))
        before_replay = [tuple(row) for row in self.conn.execute('SELECT * FROM memory_aliases ORDER BY id')]
        self.assertEqual(result, save_personal_profile(self.conn, request, timestamp=201))
        self.assertEqual([tuple(row) for row in self.conn.execute('SELECT * FROM memory_aliases ORDER BY id')], before_replay)

    def test_merge_preserves_source_and_target_aliases_once(self):
        from rag_ime.memory_card_mutations import merge_memory_cards
        from rag_ime.personal_profile import _new_personal_card
        target = self.add()
        source = _new_personal_card(self.conn, text='我使用中文沟通。', mutation_id='merge-source', timestamp=110)
        aliases = [
            ('alias:target', target['id'], '回复习惯', 'semantic', 'huifu xiguan', 0.8, 50),
            ('alias:source', source['memoryId'], '交流语言', 'semantic', None, 0.6, 60),
        ]
        self.conn.executemany('INSERT INTO memory_aliases VALUES (?,?,?,?,?,?,?)', aliases)
        result = merge_memory_cards(self.conn, source['memoryId'], target['id'],
            expected_revision=source['revision'], expected_target_revision=target['revision'],
            timestamp=200, mutation_id='merge-aliases')
        inherited = self.conn.execute('SELECT * FROM memory_aliases WHERE memory_atom_id=? ORDER BY weight DESC',
                                      (result['memoryId'],)).fetchall()
        self.assertEqual([tuple(row)[2:] for row in inherited], [row[2:] for row in aliases])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM memory_aliases').fetchone()[0], 4)
        for alias in aliases:
            self.assertEqual(tuple(self.conn.execute('SELECT * FROM memory_aliases WHERE id=?', (alias[0],)).fetchone()), alias)
            matched, atom_ids = _matched_aliases_and_atoms(self.conn, query_terms=(alias[2],),
                                                         visible_owners=(('user', 'default'),))
            self.assertEqual(atom_ids, [result['memoryId']])
            self.assertEqual(matched, ['回复习惯', '交流语言'])

    def test_rendered_profile_budget_includes_paragraph_separators(self):
        # PR135 / discussion_r4180769539: an accepted profile must remain editable.
        def paragraphs(last):
            return [{'id': None, 'memoryIds': [], 'text': '偏好' * 300} for _ in range(6)] + [
                {'id': None, 'memoryIds': [], 'text': '说明' * last}]
        before = read_personal_profile(self.conn)
        with self.assertRaisesRegex(ValueError, '4000'):
            self.save(paragraphs(200), 'too-long')
        self.assertEqual(read_personal_profile(self.conn), before)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM memory_atoms').fetchone()[0], 0)
        result = self.save(paragraphs(194), 'exact-budget')['profile']
        self.assertEqual(len(result['text']), 4000)
        self.assertEqual(len(result['paragraphs']), 7)
        self.assertFalse(result['truncated'])

    def test_delete_and_source_revocation_remove_projection(self):
        first = self.add()
        result = self.save([{**first, 'text': ''}], 'delete')
        self.assertEqual(result['profile']['text'], '')
        self.assertEqual(self.conn.execute('SELECT status FROM memory_atoms WHERE id=?', (first['id'],)).fetchone()[0], 'tombstoned')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM memory_tombstones').fetchone()[0], 1)

    def test_profile_is_atomic_and_rejects_ambiguous_mapping(self):
        first = self.add()
        before = read_personal_profile(self.conn)
        with self.assertRaises(ValueError):
            self.save([{**first, 'text': '这条改动不应写入。'}, {**first, 'memoryIds': [first['id'], 'other']}], 'bad')
        self.assertEqual(before, read_personal_profile(self.conn))

    def test_revoking_latest_correction_cannot_fall_back_to_old_support(self):
        first = self.add()
        changed = self.save([{**first, 'text': '我现在喜欢详细的回复。'}], 'change')
        current = changed['profile']['paragraphs'][0]
        self.conn.execute("""UPDATE agent_memory_evidence SET admission_state='forgotten'
            WHERE evidence_id IN (SELECT evidence_id FROM memory_lifecycle_atom_evidence_links
                                  WHERE atom_id=? AND relation='source')""", (current['id'],))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM agent_memory_evidence WHERE admission_state='admitted'").fetchone()[0], 1)
        self.assertEqual(read_personal_profile(self.conn)['text'], '')

    def test_authority_tuple_preserved_and_stale_card_rejected(self):
        first = self.add()
        old = dict(self.conn.execute('SELECT * FROM memory_atoms WHERE id=?', (first['id'],)).fetchone())
        changed = correct_memory_card(self.conn, first['id'], text='我更喜欢中文回复。',
                                      expected_revision=card_revision(old), timestamp=200,
                                      mutation_id='test-correct', reason='user edit', user_edit=True)
        new = dict(self.conn.execute('SELECT * FROM memory_atoms WHERE id=?', (changed['memoryId'],)).fetchone())
        for key in ('owner_kind','owner_id','privacy_level','knowledge_domain','scope_kind','scope_id','visibility','authorization_revision','binding_id','scope_mode','scope_project','scope_app'):
            self.assertEqual(old[key], new[key], key)
        with self.assertRaises(MemoryRevisionConflict):
            correct_memory_card(self.conn, first['id'], text='stale', expected_revision=card_revision(old), timestamp=300, mutation_id='stale', reason='stale')

    def test_private_scope_and_admitted_evidence_required(self):
        first = self.add()
        for field, value in [('owner_kind','shared'), ('scope_project','project'), ('visibility','room'), ('scope_mode','quarantined'), ('privacy_level','sensitive')]:
            with self.subTest(field=field):
                old = self.conn.execute(f'SELECT {field} FROM memory_atoms WHERE id=?', (first['id'],)).fetchone()[0]
                self.conn.execute(f'UPDATE memory_atoms SET {field}=? WHERE id=?', (value, first['id']))
                self.assertEqual(read_personal_profile(self.conn)['text'], '')
                self.conn.execute(f'UPDATE memory_atoms SET {field}=? WHERE id=?', (old, first['id']))
        self.conn.execute("UPDATE agent_memory_evidence SET admission_state='forgotten'")
        self.assertEqual(read_personal_profile(self.conn)['text'], '')

    def test_old_background_claim_key_output_cannot_replace_user_edit(self):
        from rag_ime.memory_book_compiler import _apply_memory_atom
        first = self.add()
        old = dict(self.conn.execute('SELECT * FROM memory_atoms WHERE id=?', (first['id'],)).fetchone())
        changed = self.save([{**first, 'text': '用户现在明确偏好有解释的回复。'}], 'user-change')
        payload = {
            'atomId': 'atom:late-background', 'canonicalText': '用户旧的偏好应当覆盖现在的选择。',
            'kind': old['kind'], 'claimKey': old['claim_key'], 'validFromMs': 999999999,
            'project': '', 'app': '', 'ownerKind': old['owner_kind'], 'ownerId': old['owner_id'],
            'knowledgeDomain': old['knowledge_domain'], 'scopeKind': old['scope_kind'],
            'scopeId': old['scope_id'], 'visibility': old['visibility'],
            'authorizationRevision': old['authorization_revision'], 'bindingId': old['binding_id'],
            'scopeMode': old['scope_mode'], 'privacyLevel': old['privacy_level'],
            'expectedCardRevisions': {first['id']: first['revision']},
        }
        with self.assertRaises(MemoryRevisionConflict):
            _apply_memory_atom(self.conn, payload)
        with self.assertRaises(MemoryRevisionConflict):
            _apply_memory_atom(self.conn, {**payload, 'atomId': first['id']})
        self.assertEqual(read_personal_profile(self.conn), changed['profile'])
        self.assertIsNone(self.conn.execute("SELECT 1 FROM memory_atoms WHERE id='atom:late-background'").fetchone())

    def test_request_id_reuse_with_changed_body_is_a_conflict(self):
        first = self.add()
        with self.assertRaises(MemoryRevisionConflict):
            self.save([{**first, 'text': '另一种内容。'}], 'save-1')

    def test_fresh_background_correction_must_preserve_user_card_history(self):
        from rag_ime.memory_book_compiler import _apply_memory_atom
        first = self.add()
        with self.assertRaisesRegex(ValueError, 'create a successor'):
            _apply_memory_atom(self.conn, {'atomId': first['id'], 'canonicalText': '替换用户原话。',
                'expectedCardRevisions': {first['id']: first['revision']}})
        self.assertEqual(read_personal_profile(self.conn)['text'], first['text'])

    def test_background_cannot_recreate_deleted_claim_under_new_id(self):
        from rag_ime.memory_book_compiler import _apply_memory_atom
        first = self.add()
        old = dict(self.conn.execute('SELECT * FROM memory_atoms WHERE id=?', (first['id'],)).fetchone())
        self.save([{**first, 'text': ''}], 'delete')
        payload = {'atomId': 'atom:recreated', 'canonicalText': old['text'], 'kind': old['kind'],
                   'claimKey': old['claim_key'], 'project': '', 'app': '',
                   'ownerKind': 'user', 'ownerId': 'default', 'knowledgeDomain': 'personal_memory',
                   'scopeKind': 'user', 'scopeId': 'default', 'visibility': 'private',
                   'scopeMode': 'authoritative', 'privacyLevel': 'private',
                   'authorizationRevision': old['authorization_revision'], 'bindingId': old['binding_id'],
                   'validFromMs': 999999999}
        with self.assertRaises(MemoryRevisionConflict):
            _apply_memory_atom(self.conn, payload)
        self.assertEqual(read_personal_profile(self.conn)['text'], '')

    def test_governed_correction_preserves_full_nonpersonal_authority(self):
        from rag_ime.agent_governed_memory_tools import MemoryGovernanceProposalStore, _atom_state_sha256
        first = self.add()
        self.conn.execute("""UPDATE memory_atoms SET owner_kind='agent',owner_id='agent-a',
            knowledge_domain='participant_private',scope_kind='participant',scope_id='member-a',
            binding_id='binding-a',authorization_revision='authorization-a' WHERE id=?""", (first['id'],))
        old = dict(self.conn.execute('SELECT * FROM memory_atoms WHERE id=?', (first['id'],)).fetchone())
        alias = ('alias:agent', first['id'], '项目记忆称呼', 'semantic', 'xiangmu jiyi chenghu', 0.75, 90)
        self.conn.execute('INSERT INTO memory_aliases VALUES (?,?,?,?,?,?,?)', alias)
        store = MemoryGovernanceProposalStore(':memory:', project='')
        result = store._apply_correct(self.conn, {
            'target_memory_id': first['id'], 'target_state_sha256': _atom_state_sha256(old),
            'proposal_id': 'proposal:authority-test', 'memory_kind': old['kind'],
            'proposed_text': '这是用户确认后的项目内更正。', 'reason': 'confirmed correction', 'evidence_ids_json': '[]',
        }, evidence_snapshot=[], timestamp=200, approval_id='approval:synthetic')
        new = dict(self.conn.execute('SELECT * FROM memory_atoms WHERE id=?', (result['memoryId'],)).fetchone())
        for key in ('owner_kind','owner_id','privacy_level','knowledge_domain','scope_kind','scope_id','visibility','authorization_revision','binding_id','scope_mode'):
            self.assertEqual(old[key], new[key], key)
        self.assertEqual(read_personal_profile(self.conn)['text'], '')
        matched, atom_ids = _matched_aliases_and_atoms(self.conn, query_terms=(alias[2],),
                                                     visible_owners=(('agent', 'agent-a'),))
        self.assertEqual(atom_ids, [result['memoryId']])
        self.assertEqual(matched, [alias[2]])
        inherited = self.conn.execute('SELECT * FROM memory_aliases WHERE memory_atom_id=?',
                                      (result['memoryId'],)).fetchall()
        self.assertEqual([tuple(row)[2:] for row in inherited], [alias[2:]])
        self.assertEqual(tuple(self.conn.execute('SELECT * FROM memory_aliases WHERE id=?', (alias[0],)).fetchone()), alias)
        for owners in ((('user', 'default'),), (('agent', 'agent-b'),)):
            self.assertEqual(_matched_aliases_and_atoms(self.conn, query_terms=(alias[2],),
                                                       visible_owners=owners), ([], []))
        self.conn.execute("UPDATE memory_atoms SET privacy_level='sensitive' WHERE id=?", (result['memoryId'],))
        self.assertEqual(_matched_aliases_and_atoms(self.conn, query_terms=(alias[2],),
                                                   visible_owners=(('agent', 'agent-a'),)), ([], []))

    def test_background_fence_allows_only_hash_bound_historical_organization(self):
        from rag_ime.memory_card_mutations import assert_background_card_unchanged
        first = self.add()
        self.save([{**first, 'text': '当前明确选择更详细的回复。'}], 'change')
        old = dict(self.conn.execute('SELECT * FROM memory_atoms WHERE id=?', (first['id'],)).fetchone())
        assert_background_card_unchanged(old, card_revision(old), allow_historical_merge=True)
        with self.assertRaises(MemoryRevisionConflict):
            assert_background_card_unchanged(old, first['revision'], allow_historical_merge=True)
        with self.assertRaises(MemoryRevisionConflict):
            assert_background_card_unchanged(old, card_revision(old))

    def test_editor_save_is_not_reextracted_and_delete_forgets_only_unshared_input(self):
        from rag_ime.owner_memory_curation import _build_owner_source_bundle, _build_current_personal_atom_catalog
        first = self.add()
        bundle = _build_owner_source_bundle(self.conn, owner_kind='user', owner_id='default',
            project='', limit=24, canonical_personal=True)
        self.assertEqual(bundle['inputs'], [])
        catalog = _build_current_personal_atom_catalog(self.conn)
        self.assertEqual(catalog['existingMemoryAtoms'][0]['atomId'], first['id'])
        self.save([{**first, 'text': ''}], 'delete')
        self.assertEqual(self.conn.execute("SELECT admission_state FROM agent_memory_evidence").fetchone()[0], 'forgotten')
        self.assertEqual(self.conn.execute("SELECT disposition FROM agent_memory_sources").fetchone()[0], 'not_for_memory')

    def test_deleting_profile_card_does_not_forget_shared_support(self):
        from rag_ime.personal_profile import _new_personal_card
        first = self.add()
        other = _new_personal_card(self.conn, text='我使用中文沟通。', mutation_id='other', timestamp=110)
        evidence_id = self.conn.execute('SELECT evidence_id FROM memory_lifecycle_atom_evidence_links WHERE atom_id=?', (first['id'],)).fetchone()[0]
        self.conn.execute("INSERT INTO memory_lifecycle_atom_evidence_links VALUES (?,?,'source',110)", (other['memoryId'], evidence_id))
        self.save([{**first, 'text': ''}], 'delete')
        self.assertEqual(self.conn.execute('SELECT admission_state FROM agent_memory_evidence WHERE evidence_id=?', (evidence_id,)).fetchone()[0], 'admitted')
        self.assertEqual(read_personal_profile(self.conn)['paragraphs'][0]['id'], other['memoryId'])

    def test_profile_read_is_bounded_and_does_not_clip_editable_text(self):
        from rag_ime.personal_profile import _new_personal_card
        for index in range(15):
            _new_personal_card(self.conn, text=f'第{index}条用户明确保存的个人背景。', mutation_id=f'add-{index}', timestamp=100+index)
        profile = read_personal_profile(self.conn)
        self.assertEqual(len(profile['paragraphs']), 12)
        self.assertTrue(profile['truncated'])
        self.assertLessEqual(len(profile['text']), 4000)

    def test_management_edit_versions_and_replays_exact_receipt(self):
        from rag_ime.management_service import ManagementService, page_request
        first = self.add()
        service = ManagementService.__new__(ManagementService)
        service._connect = lambda: nullcontext(self.conn)
        service.cache_invalidator = None
        service.events = Mock()
        service._bump_runtime_revision = Mock()
        service.project = ''
        request = {'kind': 'atom', 'id': first['id'], 'text': '我希望回答中包含清楚的解释。',
                   'tags': ['表达习惯'], 'expectedRevision': first['revision'], 'clientRequestId': 'ui-edit'}
        result = service.memory_edit(request)
        self.assertNotEqual(result['id'], first['id'])
        self.assertEqual(result, service.memory_edit(request))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM management_audit_log WHERE action='memory_edit'").fetchone()[0], 1)
        items, _ = service._memory_atoms(page_request({'status': 'current'}))
        self.assertEqual(items[0]['revision'], result['revision'])
        self.assertEqual(items[0]['id'], result['id'])
        self.assertEqual(service.personal_profile()['text'], request['text'])
        with self.assertRaises(MemoryRevisionConflict):
            service.memory_edit({**request, 'clientRequestId': 'another-ui-edit'})


if __name__ == '__main__':
    unittest.main()
