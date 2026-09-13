from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rag_ime.agent_lab import app_knowledge_runtime as runtime


class AppKnowledgeConversationTests(unittest.TestCase):
    def retrieve(self, question, conversation=None):
        store = Mock()
        store.search.return_value = []
        knowledge = {'queryField': 'question', 'profile': {'mode': 'lexical', 'rerank': False,
            'topK': 4, 'threshold': 0, 'contextChars': 4000}, 'snapshotSha256': 'snapshot',
            'sourceIndexId': 'index', 'documentCount': 209}
        values = {'question': question}
        if conversation is not None:
            values['conversation'] = conversation
        with patch.object(runtime, '_open', return_value=(store, {'base': {'id': 'base'}, 'sources': {}}, None)), \
             patch.object(runtime.importlib, 'import_module', return_value=SimpleNamespace(
                 rank_retrieval_hits=lambda *args, **kwargs: ([], 'lexical'))):
            result = runtime.retrieve(Path('/unused'), knowledge, values)
        self.assertEqual(store.search.call_args.args[0], result['query'])
        self.assertEqual(result['profile'], knowledge['profile'])
        self.assertEqual(result['snapshotSha256'], 'snapshot')
        self.assertEqual(result['modelCalls'], 0)
        return result['query']

    def test_followup_retrieves_user_topic_without_assistant_claims_or_citations(self):
        topic = 'Compare Orion 2016 observations and Lyra 2014 simulations.'
        history = json.dumps([{'question': topic, 'answer': 'UNVERIFIED_ASSISTANT_CLAIM [917]'}])
        current = 'Which limitations of these two papers have direct evidence?'
        query = self.retrieve(current, history)
        self.assertTrue(query.startswith(current))
        self.assertIn(topic, query)
        self.assertNotIn('UNVERIFIED_ASSISTANT_CLAIM', query)
        self.assertNotIn('[917]', query)

    def test_only_earliest_and_latest_available_user_questions_are_used(self):
        history = json.dumps([{'question': text, 'answer': ''} for text in
                              ('EARLIEST_TOPIC', 'MIDDLE_ONE', 'MIDDLE_TWO', 'LATEST_CONTEXT')])
        query = self.retrieve('CURRENT_EXPLICIT_QUESTION', history)
        self.assertTrue(query.startswith('CURRENT_EXPLICIT_QUESTION'))
        self.assertIn('EARLIEST_TOPIC', query)
        self.assertIn('LATEST_CONTEXT', query)
        self.assertNotIn('MIDDLE_', query)

    def test_new_conversation_and_invalid_history_leave_query_exactly_unchanged(self):
        for history in (None, '', '[]', '{}', 'null', '[1]', '[{"question":"topic"}]',
                        '[{"question":42,"answer":"old"}]', '[{"question":" ","answer":"old"}]',
                        [{'question': 'topic', 'answer': 'old'}], '[' * 2000, 'x' * 16001):
            with self.subTest(history=str(history)[:60]):
                self.assertEqual(self.retrieve('  New explicit topic  ', history), '  New explicit topic  ')

    def test_context_is_bounded_deduplicated_and_never_truncates_current_question(self):
        history = json.dumps([{'question': 'H' * 6000, 'answer': ''}])
        query = self.retrieve('Q' * 19500, history)
        self.assertTrue(query.startswith('Q' * 19500))
        self.assertLessEqual(len(query), 20000)
        self.assertEqual(self.retrieve('Q' * 20000, history), 'Q' * 20000)
        self.assertLessEqual(len(self.retrieve('short', history)), 3006)
        repeated = json.dumps([{'question': 'same', 'answer': ''}] * 4)
        self.assertEqual(self.retrieve('same', repeated), 'same')

    def test_original_question_validation_still_rejects_empty_or_oversized_input(self):
        for question in ('', '  ', None, 'q' * 20001):
            with self.subTest(question=str(question)[:20]), self.assertRaises(ValueError):
                self.retrieve(question, '[{"question":"valid topic","answer":"old"}]')
