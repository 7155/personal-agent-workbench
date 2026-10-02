"""Credential projection keeps exact ordinary Tool output, including JSON strings."""
from __future__ import annotations

import json
import unittest

from rag_ime.pi.public import inspectable_tool_result


class InspectableToolResultSanitizerTests(unittest.TestCase):
    def test_structures_and_serialized_json_redact_the_same_credentials(self):
        source = {'observed': 42, 'rows': [{'password': 'synthetic-password', 'access_token': 'synthetic-access'}],
                  'headers': {'Authorization': 'Bearer synthetic-bearer', 'Cookie': 'session=synthetic-cookie'}}
        expected = {'observed': 42, 'rows': [{'password': '[REDACTED_SECRET]', 'access_token': '[REDACTED_SECRET]'}],
                    'headers': {'Authorization': '[REDACTED_SECRET]', 'Cookie': '[REDACTED_SECRET]'}}
        self.assertEqual(inspectable_tool_result(source), expected)
        for text in (json.dumps(source), json.dumps([source], indent=2),
                     json.dumps(json.dumps(source)),
                     json.dumps('{"pass\\u0077ord":"synthetic-password","observed":42}'),
                     '{"pass\\u0077ord":"synthetic-password","observed":42}'):
            with self.subTest(text=text):
                result = inspectable_tool_result(text)
                self.assertNotIn('synthetic-', result)
                self.assertEqual(inspectable_tool_result(result), result)
                self.assertIsNotNone(json.loads(result))

    def test_headers_and_quoted_assignments_mask_complete_values(self):
        for text in ('Authorization: Bearer synthetic-bearer\nobserved=42',
                     '[debug] Authorization: Bearer synthetic-bearer\nobserved=42',
                     'request Cookie: session=synthetic-cookie; sid=synthetic-id\nobserved=42',
                     'Cookie: session=synthetic-cookie; auth=synthetic-auth\nobserved=42',
                     'Set-Cookie: session=synthetic-cookie; Secure; HttpOnly\nobserved=42',
                     'result: {"password": "synthetic-quoted", "observed":42}',
                     "password='synthetic quoted value' observed=42",
                     'OPENAI_API_KEY=synthetic-key --token synthetic-token observed=42'):
            with self.subTest(text=text):
                result = inspectable_tool_result(text)
                self.assertNotIn('synthetic', result)
                self.assertIn('42', result)
                self.assertEqual(inspectable_tool_result(result), result)

    def test_deep_serialized_credentials_are_bounded_without_throwing_or_leaking(self):
        for depth in (100, 2000):
            source = '[' * depth + '{"password":"synthetic-secret"}' + ']' * depth
            with self.subTest(depth=depth):
                result = inspectable_tool_result(source)
                self.assertNotIn('synthetic-secret', result)
                self.assertRegex(result, r'\[REDACTED_(?:SECRET|NESTING_LIMIT)\]')

    def test_secret_json_preserves_all_other_bytes_numbers_duplicate_keys_and_escapes(self):
        source = (' {"password" : "synthetic-secret", "decimal":0.12345678901234567890123456789,'
                  '"scientific":1e1000,"observed":1,"observed":2,"text":"\\u4e2d\\/line\\n"}  ')
        self.assertEqual(inspectable_tool_result(source), source.replace('synthetic-secret', '[REDACTED_SECRET]'))
        duplicate = '{"password":"synthetic-one","password":"synthetic-two","observed":42}'
        self.assertEqual(inspectable_tool_result(duplicate),
                         duplicate.replace('synthetic-one', '[REDACTED_SECRET]').replace('synthetic-two', '[REDACTED_SECRET]'))
        nested_value = '{"secret":{"password":"synthetic","observed":1},"safe":"\\u4e2d"}'
        self.assertEqual(inspectable_tool_result(nested_value),
                         '{"secret":"[REDACTED_SECRET]","safe":"\\u4e2d"}')

    def test_nonsecret_results_keep_exact_types_text_and_formatting(self):
        for value in (' { "observed" : 42, "rows": [1,2,3], "path": "/tmp/原样.json" }\n',
                      '42\n[exit code: 0]', 'document.cookie is unavailable',
                      {'stdout': '  [1,2,3]\n', 'stderr': '', 'exitCode': 0, 'timedOut': False},
                      ['first', {'content': '原样\n' * 5000}], None, True, 42, 1.25):
            with self.subTest(value=str(value)[:80]):
                self.assertEqual(inspectable_tool_result(value), value)
