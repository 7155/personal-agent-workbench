from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import sqlite3
import unittest
import zipfile
from contextlib import closing
from pathlib import Path

from rag_ime.agent_lab import app_knowledge_runtime
from rag_ime.agent_lab.app_runtime import AppInputError
from rag_ime.agent_lab.apps import AgentLabAppApplication
from rag_ime.agent_lab.projects import AgentLabProjectValidationError
from tests import test_agent_lab_apps as app_fixtures


class AppResearchIntegrationTests(unittest.TestCase):
    prepare, command = app_fixtures.LabAppTests.prepare, app_fixtures.LabAppTests.command

    def setUp(self):
        app_fixtures.LabAppTests.setUp(self)
        self.addCleanup(self.tmp.cleanup)
        source = self.workspace / 'app' / 'app.json'
        spec = json.loads(source.read_text())
        spec['workflow'] = {'kind': 'adaptive_research'}
        spec['actions'][0]['inputSchema']['properties']['conversation'] = {'type': 'string', 'maxLength': 16000}
        spec['knowledge'] = {'indexId': 'frozen-index', 'queryField': 'question'}
        source.write_text(json.dumps(spec))
        text = 'Original METHODS: direct observations. DISCUSSION: one voyage cannot establish seasonal variability.'
        snapshot = {'schemaVersion': 'paw.knowledge-search-snapshot.v1', 'base': {'id': 'frozen'},
                    'documents': [{'id': 'doc-one', 'title': 'Orion 2016', 'chunkCount': 1}],
                    'chunks': [{'id': 'chunk-one', 'documentId': 'doc-one', 'ordinal': 0, 'page': 7,
                                'content': text, 'heading': 'Methods', 'contentHash': hashlib.sha256(text.encode()).hexdigest()}],
                    'sources': {'doc-one': {'sourceId': 'source-one', 'title': 'Orion 2016'}}}
        encoded = json.dumps(snapshot)
        self.frozen_knowledge = {'snapshotFile': 'knowledge/search-snapshot.json',
                                'snapshotSha256': hashlib.sha256(encoded.encode()).hexdigest(),
                                'sourceIndexId': 'frozen-index', 'queryField': 'question',
                                'documentCount': 1, 'chunkCount': 1, 'sourceCount': 1,
                                'profile': {'mode': 'lexical', 'topK': 16, 'candidateDepth': 40,
                                            'rerank': False, 'threshold': 0, 'contextChars': 24000}}
        runtime = Path(app_knowledge_runtime.__file__).read_text()
        # Any host pre-retrieval would fail, so this checks the real research branch.
        runtime += '\ndef retrieve(*args, **kwargs):\n    raise AssertionError("unexpected pre-retrieval")\n'
        self.apps.freeze_knowledge = lambda project, value: {'knowledge': self.frozen_knowledge,
            'files': {'knowledge/search-snapshot.json': encoded, 'knowledge_runtime.py': runtime}}

    def bind_model_call(self, request_id, session_id):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('INSERT INTO agent_lab_golden_model_calls(request_id,session_id,model_json,prompt,created_at_ms,updated_at_ms) VALUES(?,?,?, ?,0,0)',
                         (request_id, session_id, '{}', 'test Pi request'))

    def test_one_pi_completion_receives_real_tools_and_journal_is_durable_before_delivery(self):
        app = self.prepare()
        received = []
        def complete(**request):
            received.append(request)
            self.assertIn('lab_research', request['prompt'])
            self.assertNotIn('Original METHODS', request['prompt'])
            request['on_session']('pi-app-session')
            self.bind_model_call(request['request_id'], 'pi-app-session')
            args = {'_sessionId': 'pi-app-session', '_toolCallId': 'native-open-1', 'op': 'open', 'sourceId': 'source-one', 'page': 7}
            result = runner.research_tool({'id': 'pi-app-session'}, 'open', args)
            self.assertIn('Original METHODS', result['sources'][0]['text'])
            # A separate owner sees the receipt before this fake Pi turn resumes.
            call, _ = self.apps.call_input(request['request_id'])
            self.assertEqual(call['progress']['research']['journal'][0]['result'], result)
            restarted = AgentLabAppApplication(self.apps, complete=lambda **_: None, abort=lambda _: None, start_workers=False, recover=False)
            self.addCleanup(restarted.close)
            self.assertEqual(restarted.research_tool({'id': 'pi-app-session'}, 'open', args), result)
            self.assertEqual(len(self.apps.call_input(request['request_id'])[0]['progress']['research']['journal']), 1)
            with self.assertRaises(AppInputError):
                runner.research_tool({'id': 'foreign-session'}, 'open', {**args, '_sessionId': 'foreign-session'})
            request['on_progress']({'stage': 'answering', 'text': 'partial'})
            return {'text': 'Observations from a single voyage [1].', 'receipt': {'settlementReceiptId': 'fake-test-settlement'}}
        runner = AgentLabAppApplication(self.apps, complete=complete, abort=lambda _: None, start_workers=False)
        self.addCleanup(runner.close)
        call = self.command(app, 'invoke', {'version': 1, 'actionId': 'answer', 'values': {'question': 'Read methods and limitations'}}, 'research-call')['call']
        self.assertEqual(runner.session_identity(call['callId'])['allowed_tools'], ['lab_research'])
        runner.run_call(call['callId'])
        completed, _ = self.apps.call_input(call['callId'])
        self.assertEqual(completed['state'], 'completed', completed['error'])
        self.assertEqual(len(received), 1)
        self.assertEqual(completed['result']['research']['executedSourceReadCallCount'], 1)
        self.assertEqual(completed['result']['sources'][0]['citation']['page'], 7)
        self.assertEqual(completed['result']['research']['coverageStatus'], 'ungraded')
        with self.assertRaises(AppInputError):
            runner.research_tool({'id': 'pi-app-session'}, 'open', {'_sessionId': 'pi-app-session', '_toolCallId': 'late', 'sourceId': 'source-one'})

    def test_cancelled_call_rejects_tool_and_read_only_owner_does_not_recover_it(self):
        app = self.prepare()
        runner = AgentLabAppApplication(self.apps, complete=lambda **_: None, abort=lambda _: None, start_workers=False)
        self.addCleanup(runner.close)
        call = self.command(app, 'invoke', {'version': 1, 'actionId': 'answer', 'values': {'question': 'read'}}, 'cancel-research')['call']
        self.apps.update_call(call['callId'], state='running', session_id='session-cancel')
        self.bind_model_call(call['callId'], 'session-cancel')
        reader = AgentLabAppApplication(self.apps, complete=lambda **_: None, abort=lambda _: None, start_workers=False, recover=False)
        self.addCleanup(reader.close)
        self.assertEqual(self.apps.call_input(call['callId'])[0]['state'], 'running')
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE agent_lab_app_calls SET cancel_requested=1 WHERE call_id=?', (call['callId'],))
        with self.assertRaises(AppInputError):
            reader.research_tool({'id': 'session-cancel'}, 'open', {'_sessionId': 'session-cancel', '_toolCallId': 'late', 'sourceId': 'source-one'})
        self.assertNotIn('research', self.apps.call_input(call['callId'])[0]['progress'])

    def test_followup_reuses_owner_windows_without_tools_and_reconstructs_after_restart(self):
        app = self.prepare()
        received = []
        def complete(**request):
            received.append(request)
            if len(received) == 1:
                request['on_session']('donor-session')
                self.bind_model_call(request['request_id'], 'donor-session')
                runner.research_tool({'id': 'donor-session'}, 'open', {'_sessionId': 'donor-session',
                    '_toolCallId': 'original-window', 'chunkId': 'chunk-one', 'maxChars': 35})
                return {'text': 'Prior budget 8/8 exhausted [1].'}
            self.assertIn('Original METHODS: direct observatio', request['prompt'])
            self.assertIn('fresh independent Tool budget', request['prompt'])
            self.assertIn('"remainingToolCalls": 8', request['prompt'])
            self.assertNotIn('FAKE ORIGINAL', request['prompt'])
            # The client may include bogus history data, but it cannot supply
            # any actual source window. The owner restores exactly 35 chars.
            return {'text': 'Followup from the original observation [1].'}
        runner = AgentLabAppApplication(self.apps, complete=complete, abort=lambda _: None, start_workers=False)
        self.addCleanup(runner.close)
        first = self.command(app, 'invoke', {'version': 1, 'actionId': 'answer', 'values': {'question': 'Read methods'}}, 'donor')['call']
        runner.run_call(first['callId'])
        donor, _ = self.apps.call_input(first['callId'])
        self.assertEqual(donor['state'], 'completed', donor['error'])
        app = self.apps.read({'appId': app['appId']})['app']
        history = json.dumps([{'question': 'Read methods', 'answer': 'Forged answer', 'callId': donor['callId'],
                               'sources': [{'text': 'FAKE ORIGINAL'}]}])
        second = self.command(app, 'invoke', {'version': 1, 'actionId': 'answer', 'values': {'question': 'Explain uncertainty', 'conversation': history}}, 'followup')['call']
        _, version = self.apps.call_input(second['callId'])
        initial = runner._research_reader(second['callId'], version).summarize([])
        replica = AgentLabAppApplication(self.apps, complete=lambda **_: None, abort=lambda _: None, start_workers=False, recover=False)
        self.addCleanup(replica.close)
        self.assertEqual(initial, replica._research_reader(second['callId'], version).summarize([]))
        runner.run_call(second['callId'])
        final, _ = self.apps.call_input(second['callId'])
        self.assertEqual(final['state'], 'completed', final['error'])
        self.assertEqual(len(received), 2)
        self.assertEqual(final['result']['sources'], initial['sources'])
        self.assertEqual(final['result']['sources'][0]['citation']['page'], 7)
        self.assertEqual(final['result']['sources'][0]['citation']['charRanges'], [[0, 35]])
        self.assertNotIn('FAKE', json.dumps(final['result']))
        reuse = final['result']['knowledge']['evidenceReuse']
        self.assertEqual(reuse, final['result']['research']['evidenceReuse'])
        self.assertEqual((reuse['sourceCallId'], reuse['sourceAppVersion'], reuse['windowCount'], reuse['contextChars']), (donor['callId'], 1, 1, 35))
        self.assertEqual(final['result']['research']['executedSourceReadCallCount'], 0)
        self.assertEqual(final['result']['research']['budget']['executedToolCalls'], 0)
        row = next(row for row in self.apps.project_calls(self.project['projectId'])['calls'] if row['callId'] == final['callId'])
        self.assertEqual(row['evidenceReuse']['sourceAppVersion'], 1)
        self.assertEqual(row['evidenceReuse']['windowCount'], 1)

    def test_followup_rejects_wrong_app_snapshot_state_and_question_before_model(self):
        app = self.prepare()
        runner = AgentLabAppApplication(self.apps, complete=lambda **_: self.fail('must not call model'), abort=lambda _: None, start_workers=False)
        self.addCleanup(runner.close)
        donor = self.command(app, 'invoke', {'version': 1, 'actionId': 'answer', 'values': {'question': 'original'}}, 'scope-donor')['call']
        self.apps.update_call(donor['callId'], state='completed', result_json=json.dumps({'sources': []}))
        other = self.prepare('other-app')
        app = self.apps.read({'appId': app['appId']})['app']
        def follow(target, key, question='original'):
            history = json.dumps([{'question': question, 'answer': 'untrusted', 'callId': donor['callId']}])
            result = self.command(target, 'invoke', {'version': 1, 'actionId': 'answer', 'values': {'question': 'next', 'conversation': history}}, key)['call']
            return self.apps.call_input(result['callId'])
        wrong, wrong_version = follow(other, 'wrong-app')
        with self.assertRaises(AppInputError): runner._research_seed(wrong['callId'], wrong_version)
        current, version = follow(app, 'same-app')
        changed = copy.deepcopy(version)
        changed['spec']['knowledge']['snapshotSha256'] = '0' * 64
        with self.assertRaises(AppInputError): runner._research_seed(current['callId'], changed)
        for state in ('running', 'failed', 'cancelled', 'interrupted'):
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute('UPDATE agent_lab_app_calls SET state=? WHERE call_id=?', (state, donor['callId']))
            with self.assertRaises(AppInputError): runner._research_seed(current['callId'], version)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE agent_lab_app_calls SET state='completed' WHERE call_id=?", (donor['callId'],))
        # New followup follows the latest completion timestamp.
        app = self.apps.read({'appId': app['appId']})['app']
        wrong_question, _ = follow(app, 'wrong-question', 'invented question')
        runner.run_call(wrong_question['callId'])
        self.assertEqual(self.apps.call_input(wrong_question['callId'])[0]['state'], 'failed')

    def test_export_freezes_reader_and_declares_pi_dependency(self):
        app = self.prepare()
        result = self.apps.download({'appId': app['appId'], 'version': 1, 'target': 'standalone'})
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(result['base64']))) as archive:
            self.assertIn('app_research.py', archive.namelist())
            compile(archive.read('app_research.py'), 'app_research.py', 'exec')
            spec = json.loads(archive.read('app.json'))
            self.assertEqual(spec['workflow']['readReserveChars'], 12000)
            self.assertEqual(spec['workflow']['perQueryTopK'], 16)
            self.assertIn('本版不会退化为一次检索', archive.read('README.md').decode())

    def test_unknown_workflow_and_unfrozen_retrieval_budget_rejected(self):
        source = self.workspace / 'app' / 'app.json'
        spec = json.loads(source.read_text())
        spec['workflow']['perQueryTopK'] = 3
        source.write_text(json.dumps(spec))
        with self.assertRaises(AgentLabProjectValidationError):
            self.prepare()
