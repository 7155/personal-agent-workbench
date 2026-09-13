from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from rag_ime.agent_lab.app_research import FrozenResearchReader


class AppResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        chunks = []
        for doc, texts in [('one', ['Title Orion', 'METHODS measured velocity ' + 'a' * 900,
                                    'DISCUSSION single voyage uncertainty', 'REFERENCES other papers']),
                           ('two', ['Title Lyra', 'METHODS periodic domain', 'DISCUSSION uncertain forcing'])]:
            for i, text in enumerate(texts):
                chunks.append({'id': f'{doc}-{i}', 'documentId': doc, 'ordinal': i,
                               'content': text, 'contentHash': hashlib.sha256(text.encode()).hexdigest(),
                               'page': i + 1, 'heading': ''})
        self.snapshot = {'schemaVersion': 'paw.knowledge-search-snapshot.v1',
                         'base': {'id': 'frozen'}, 'chunks': chunks,
                         'documents': [{'id': 'one', 'title': '2016-Orion.md', 'chunkCount': 4},
                                       {'id': 'two', 'title': '2014-Lyra.md', 'chunkCount': 3}],
                         'sources': {key: {'sourceId': 'source-' + key, 'title': title,
                                          'uri': 'knowledge-source:source-' + key}
                                     for key, title in [('one', '2016 Orion'), ('two', '2014 Lyra')]}}
        raw = json.dumps(self.snapshot).encode()
        (self.root / 'snapshot.json').write_bytes(raw)
        self.knowledge = {'snapshotFile': 'snapshot.json', 'snapshotSha256': hashlib.sha256(raw).hexdigest(),
                          'sourceIndexId': 'frozen-index', 'documentCount': 2, 'chunkCount': 7,
                          'queryField': 'question', 'profile': {'mode': 'hybrid', 'topK': 16,
                          'candidateDepth': 40, 'contextChars': 24000, 'threshold': 0, 'rerank': False}}
        self.search = Mock(return_value={'sources': [{'chunkId': 'one-3', 'sourceId': 'source-one'}],
                                         'retrieval': {'candidateLimit': 64, 'effectiveMode': 'hybrid'}})
        self.reader = self.make_reader()
        self.journal = []

    def make_reader(self, **config):
        return FrozenResearchReader(self.root, self.knowledge, {'kind': 'adaptive_research', **config},
                                    call_id='app-call', search=self.search)

    def execute(self, args, reader=None, call_id=None):
        value = (reader or self.reader).execute(args, tool_call_id=call_id or f'tool-{len(self.journal)}', journal=self.journal)
        if value['journalEntry']:
            # This models the App owner's durable append-before-delivery boundary.
            self.journal = json.loads(json.dumps([*self.journal, value['journalEntry']]))
        return value['result']

    def test_discover_find_open_reads_original_methods_without_search_or_dense(self):
        discovered = self.execute({'op': 'discover', 'query': 'Orion 2016'})
        self.assertEqual([x['documentId'] for x in discovered['documents']], ['one'])
        found = self.execute({'op': 'find', 'sourceId': 'source-one', 'patterns': ['METHODS']})
        self.assertEqual(found['matches'][0]['chunkId'], 'one-1')
        opened = self.execute({'op': 'open', 'sourceId': 'source-one', 'chunkId': 'one-1', 'before': 0, 'after': 0})
        self.assertTrue(any('a' * 100 in x['text'] for x in opened['sources']))
        for source in opened['sources']:
            self.assertEqual(source['citation']['page'], 2)
            self.assertEqual(source['citation']['snapshotSha256'], self.knowledge['snapshotSha256'])
        self.search.assert_not_called()
        self.assertFalse((self.root / '.knowledge-cache').exists())

    def test_paged_small_read_of_large_chunk_never_requires_full_chunk_budget(self):
        result = self.execute({'op': 'open', 'sourceId': 'source-one', 'offset': 1, 'maxChars': 80})
        self.assertEqual(sum(len(x['text']) for x in result['sources']), 80)
        self.assertEqual(result['nextCursor']['offset'], 1)
        self.assertEqual(result['nextCursor']['charOffset'], 80)
        next_result = self.execute({'op': 'open', **result['nextCursor'], 'maxChars': 80})
        self.assertEqual(next_result['sources'][0]['citationNumber'], result['sources'][0]['citationNumber'])
        self.assertEqual(next_result['sources'][0]['citation']['charStart'], 80)
        source = self.reader.summarize(self.journal)['sources'][0]
        self.assertEqual(source['text'], self.snapshot['chunks'][1]['content'][:160])

    def test_replay_from_serialized_journal_is_exact_and_does_not_search_again(self):
        args = {'op': 'search', 'query': 'Orion'}
        first = self.execute(args, call_id='same-tool')
        replay = self.make_reader().execute(args, tool_call_id='same-tool', journal=self.journal)
        self.assertEqual(replay['result'], first)
        self.assertIsNone(replay['journalEntry'])
        self.assertTrue(replay['replayed'])
        self.assertEqual(self.search.call_count, 1)
        with self.assertRaises(ValueError):
            self.reader.execute({'op': 'search', 'query': 'changed'}, tool_call_id='same-tool', journal=self.journal)
        altered = copy.deepcopy(self.journal)
        altered[0]['result']['status'] = 'invented'
        with self.assertRaises(ValueError):
            self.reader.execute(args, tool_call_id='same-tool', journal=altered)

    def test_binding_cannot_reuse_journal_from_other_call_or_profile(self):
        self.execute({'op': 'discover', 'query': 'Orion'})
        other = FrozenResearchReader(self.root, self.knowledge, {'kind': 'adaptive_research'}, call_id='other')
        with self.assertRaises(ValueError):
            other.execute({'op': 'discover', 'query': 'Orion'}, tool_call_id='other-tool', journal=self.journal)
        changed = copy.deepcopy(self.knowledge)
        changed['profile']['topK'] = 8
        other = FrozenResearchReader(self.root, changed, {'kind': 'adaptive_research'}, call_id='app-call')
        with self.assertRaises(ValueError):
            other.execute({'op': 'discover', 'query': 'Orion'}, tool_call_id='other-tool', journal=self.journal)

    def test_failed_search_is_receipted_and_replayed_without_inventing_absence(self):
        self.search.side_effect = RuntimeError('encoder unavailable')
        args = {'op': 'search', 'query': 'Orion'}
        failed = self.execute(args, call_id='failed-search')
        self.assertEqual(failed['status'], 'tool_error')
        self.assertEqual(failed['budget']['executedSearchCalls'], 0)
        self.assertEqual(failed['budget']['searchCalls'], 1)
        replay = self.reader.execute(args, tool_call_id='failed-search', journal=self.journal)
        self.assertEqual(replay['result'], failed)
        self.assertEqual(self.search.call_count, 1)

    def test_source_chunk_and_snapshot_identity_must_agree(self):
        for args in ({'op': 'open', 'sourceId': 'foreign'},
                     {'op': 'open', 'sourceId': 'source-one', 'chunkId': 'two-1'},
                     {'op': 'open', 'documentId': 'one', 'sourceId': 'source-two'},
                     {'op': 'open', 'sourceId': 'source-one', 'snapshotSha256': 'foreign'},
                     {'op': 'search', 'query': 'Orion', 'kbId': 'global'}):
            with self.subTest(args=args):
                result = self.execute(args)
                self.assertEqual(result['status'], 'invalid_argument')
                self.assertEqual(result['sources'], [])

    def test_search_does_not_change_profile_ranking_and_preserves_read_reserve(self):
        self.knowledge['profile']['contextChars'] = 1000
        reader = self.make_reader(readReserveChars=900, maxSearchChars=100)
        self.search.return_value['sources'] = [{'chunkId': 'one-1'}, {'chunkId': 'two-1'}]
        result = self.execute({'op': 'search', 'query': 'methods'}, reader=reader)
        self.assertEqual(result['sources'][0]['chunkId'], 'one-1')
        self.assertEqual(result['budget']['discoveryContextChars'], 100)
        self.assertEqual(result['retrieval']['candidateLimit'], 64)
        self.assertEqual(self.search.call_args.args[1]['profile'], self.knowledge['profile'])
        blocked = self.execute({'op': 'search', 'query': 'more'}, reader=reader)
        self.assertEqual(blocked['status'], 'budget_exhausted')
        self.assertEqual(self.search.call_count, 1)
        read = self.execute({'op': 'open', 'sourceId': 'source-one', 'offset': 1, 'maxChars': 300}, reader=reader)
        self.assertGreater(read['budget']['readContextChars'], 0)
        self.assertLessEqual(read['budget']['contextChars'], 1000)

    def test_overlap_reuses_citations_without_double_charging(self):
        args = {'op': 'open', 'chunkId': 'two-1', 'before': 0, 'after': 0}
        first = self.execute(args)
        again = self.execute(args)
        self.assertEqual(again['budget']['contextChars'], first['budget']['contextChars'])
        self.assertEqual(again['sources'], [])
        self.assertEqual(again['existingCitations'], [first['sources'][0]['citationNumber']])

    def test_page_is_original_page_and_not_document_ordinal(self):
        result = self.execute({'op': 'open', 'documentId': 'two', 'page': 3})
        self.assertEqual([s['chunkId'] for s in result['sources']], ['two-2'])
        self.assertEqual(result['sources'][0]['citation']['page'], 3)

    def test_snapshot_hash_and_symlink_are_verified_without_database(self):
        (self.root / 'snapshot.json').write_text('{}')
        with self.assertRaises(ValueError):
            self.make_reader()

    def seeded(self, sources, *, knowledge=None):
        return FrozenResearchReader(self.root, knowledge or self.knowledge, {'kind': 'adaptive_research'},
            call_id='followup', search=self.search, seed_sources=sources,
            seed_context={'sourceCallId': 'lab-app-call-' + 'a' * 32, 'sourceAppVersion': 5})

    def test_seed_preserves_exact_ranges_citations_and_resets_tool_budget(self):
        self.execute({'op': 'open', 'chunkId': 'one-1', 'before': 0, 'after': 0, 'maxChars': 80})
        donor = self.reader.summarize(self.journal)['sources']
        donor[0]['citationNumber'] = 7
        reader = self.seeded(donor)
        initial = reader.summarize([])
        self.assertEqual(initial['sources'][0]['text'], self.snapshot['chunks'][1]['content'][:80])
        self.assertEqual(initial['sources'][0]['citationNumber'], 7)
        self.assertEqual(initial['budget']['toolCalls'], 0)
        self.assertEqual(initial['budget']['remainingToolCalls'], 8)
        self.assertEqual(initial['budget']['contextChars'], 80)
        self.assertEqual(initial['budget']['reusedContextChars'], 80)
        self.assertEqual(initial['budget']['readContextChars'], 0)
        self.assertEqual(initial['evidenceReuse']['windowCount'], 1)
        repeated = reader.execute({'op': 'open', 'chunkId': 'one-1', 'before': 0, 'after': 0, 'maxChars': 80}, tool_call_id='open-more', journal=[])
        # The original range is deduplicated; only the next unread text is charged.
        self.assertEqual(repeated['result']['sources'][0]['citation']['charStart'], 80)
        self.assertEqual(repeated['result']['sources'][0]['citationNumber'], 7)
        new = reader.execute({'op': 'open', 'chunkId': 'two-1', 'before': 0, 'after': 0}, tool_call_id='second-paper', journal=[repeated['journalEntry']])
        self.assertEqual(new['result']['sources'][0]['citationNumber'], 8)
        replay = self.seeded(donor).execute({'op': 'open', 'chunkId': 'one-1', 'before': 0, 'after': 0, 'maxChars': 80}, tool_call_id='open-more', journal=[repeated['journalEntry']])
        self.assertTrue(replay['replayed'])
        self.search.assert_not_called()

    def test_seed_rejects_forged_text_ranges_identity_and_over_budget(self):
        self.execute({'op': 'open', 'chunkId': 'one-1', 'before': 0, 'after': 0})
        source = self.reader.summarize(self.journal)['sources'][0]
        changes = [lambda s: s.update(text='forged'),
                   lambda s: s['segments'][0].update(text='forged'),
                   lambda s: s['citation'].update(page=99),
                   lambda s: s['citation'].update(snapshotSha256='other'),
                   lambda s: s.update(sourceId='source-two'),
                   lambda s: s.update(contentSha256='forged'),
                   lambda s: s['citation'].update(charRanges=[[0, 1]])]
        for change in changes:
            bad = copy.deepcopy(source); change(bad)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.seeded([bad])
        self.execute({'op': 'open', 'sourceId': 'source-two', 'offset': 0})
        self.execute({'op': 'open', 'sourceId': 'source-one', 'offset': 0})
        sources = self.reader.summarize(self.journal)['sources']
        changed = copy.deepcopy(self.knowledge); changed['profile']['contextChars'] = 1000
        with self.assertRaises(ValueError):
            self.seeded(sources, knowledge=changed)


if __name__ == '__main__':
    unittest.main()
