from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from rag_ime.agent_lab.golden import AgentLabGoldenStore, AgentLabGoldenConflict, AgentLabGoldenValidationError
from rag_ime.agent_lab.golden_execution import AgentLabGoldenApplication
from rag_ime.agent_lab.project_application import AgentLabProjectApplication
from rag_ime.agent_lab.projects import AgentLabProjectValidationError
from tests.test_agent_lab_golden import MODEL, SOURCE, case
from tests.test_agent_lab_golden_execution import response, task_data


class AgentReviewTests(unittest.TestCase):
    def test_migration_preserves_original_jobs_and_frozen_input_guard(self):
        migrations = Path(__file__).resolve().parents[1] / 'rag_ime/db/migrations'
        with sqlite3.connect(':memory:') as conn:
            conn.execute('PRAGMA foreign_keys=ON')
            conn.executescript((migrations / '0190_agent_lab_golden.sql').read_text())
            conn.execute("INSERT INTO agent_lab_golden_suites VALUES('suite',1,'{}',1,1)")
            for number, state in enumerate(('queued', 'running', 'completed', 'failed', 'cancelled', 'interrupted')):
                conn.execute('INSERT INTO agent_lab_golden_jobs VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (f'job-{number}', 'suite', 'experiment', state, '{"receipt":"unchanged"}',
                     '{"frozen":"original"}', number % 2, 1, number, number + 1))
            before = conn.execute('SELECT rowid,* FROM agent_lab_golden_jobs ORDER BY rowid').fetchall()
            conn.executescript((migrations / '0203_agent_lab_agent_review.sql').read_text())
            self.assertEqual(conn.execute('SELECT rowid,* FROM agent_lab_golden_jobs ORDER BY rowid').fetchall(), before)
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
            conn.execute("INSERT INTO agent_lab_golden_jobs VALUES('review','suite','review','queued','{}','{}',0,0,8,8)")
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'immutable'):
                conn.execute("UPDATE agent_lab_golden_jobs SET input_json='changed' WHERE job_id='job-0'")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.store = AgentLabGoldenStore(self.root / 'lab.sqlite', default_model=MODEL)
        self.serial = 0
        self.suite = self.store.command({'action': 'create', 'expectedRevision': 0, 'clientRequestId': 'create',
            'input': {'title': '标准核对', 'scenario': '任务规则', 'sources': [SOURCE], 'targetCount': 2}})['suite']
        job = self.command('draft')['job']
        self.store.update_job(job['jobId'], {'state': 'running'})
        self.store.finish_job(job['jobId'], {'cases': [case('dev'), case('hold', 'holdout')]})
        self.refresh()

    def refresh(self):
        self.suite = self.store.read(self.suite['suiteId'])['suite']

    def command(self, action, value=None):
        self.serial += 1
        result = self.store.command({'action': action, 'suiteId': self.suite['suiteId'],
            'expectedRevision': self.suite['revision'], 'clientRequestId': f'command-{self.serial}', 'input': value or {}})
        self.refresh()
        return result

    def review_result(self, inputs):
        return {'reviews': [{'caseId': item['caseId'], 'verdict': 'approved', 'note': '独立核对',
            'samples': [{'sampleId': sample['sampleId'], 'verdict': decision, 'note': '依据原文'}
                for sample, decision in zip(item['samples'], ('pass', 'fail', 'uncertain'))]}
                for item in inputs['suite']['cases'] if item['caseId'] in inputs['input']['caseIds']]}

    def test_review_uses_real_pi_receipts_and_never_calls_a_completed_job_again(self):
        calls = []
        def complete(**request):
            data = task_data(request['prompt'])
            calls.append(data)
            self.assertNotIn('category', json.dumps(data))
            self.assertNotIn('humanVerdict', json.dumps(data))
            request['on_session']('pi-review-session')
            return response(json.dumps({'verdict': 'approved', 'note': '有来源', 'samples': [
                {'sampleId': sample['sampleId'], 'verdict': verdict, 'note': '逐项核对'}
                for sample, verdict in zip(data['samples'], ('pass', 'fail', 'uncertain'))]}))
        app = AgentLabGoldenApplication(store=self.store, complete=complete, abort=lambda _: None, start_workers=False)
        self.addCleanup(app.close)
        job = self.command('review')['job']
        app.run_job(job['jobId'])
        app.run_job(job['jobId'])
        self.refresh()
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.suite['jobs'][0]['state'], 'completed', self.suite['jobs'][0]['error'])
        self.assertEqual(self.suite['jobs'][0]['result']['usageByScope']['reviewing']['calls'], 2)
        for item in self.suite['cases']:
            self.assertEqual(item['review']['author'], 'agent')
            self.assertEqual(item['review']['jobId'], job['jobId'])
            self.assertEqual({sample['labelAuthor'] for sample in item['samples']}, {'agent'})
        with self.assertRaisesRegex(AgentLabGoldenValidationError, '没有待核对'):
            self.command('review')

    def test_review_keeps_human_work_and_cannot_settle_after_standard_changed(self):
        self.command('review_case', {**self.suite['cases'][0], 'verdict': 'approved', 'note': '用户已核对'})
        job = self.command('review')['job']
        inputs = self.store.job_input(job['jobId'])
        self.assertEqual(inputs['input']['caseIds'], ['hold'])
        self.store.update_job(job['jobId'], {'state': 'running'})
        self.command('review_case', {**self.suite['cases'][1], 'verdict': 'rejected', 'note': '用户新决定'})
        with self.assertRaises(AgentLabGoldenConflict):
            self.store.finish_job(job['jobId'], self.review_result(inputs))
        self.refresh()
        self.assertEqual(self.suite['cases'][0]['review']['author'], 'human')
        self.assertEqual(self.suite['cases'][1]['review']['status'], 'rejected')

    def test_review_rejects_missing_or_substituted_samples(self):
        job = self.command('review')['job']
        inputs = self.store.job_input(job['jobId'])
        self.store.update_job(job['jobId'], {'state': 'running'})
        result = self.review_result(inputs)
        result['reviews'][0]['samples'][0]['sampleId'] = 'foreign'
        with self.assertRaises(AgentLabGoldenValidationError):
            self.store.finish_job(job['jobId'], result)
        self.refresh()
        self.assertEqual(self.suite['cases'][0]['review']['status'], 'pending')

    def project_application(self):
        return AgentLabProjectApplication(self.root / 'project.sqlite', session_application=Mock(), current_model=lambda: MODEL,
            read_golden=lambda _: self.store.read(self.suite['suiteId']), command_golden=self.store.command)

    def test_guide_commands_and_reads_keep_holdout_private_but_dev_results_usable(self):
        app = self.project_application()
        binding = {'bindingId': 'binding', 'adapterId': 'golden.knowledge_qa',
                   'ownerRef': {'kind': 'golden_suite', 'id': self.suite['suiteId']}}
        project = {'bindings': [binding]}
        result = app._execution(project, 'execution_command', {'bindingId': 'binding', 'action': 'review',
            'expectedRevision': self.suite['revision'], 'clientRequestId': 'guide-review', 'input': {}})
        self.assertEqual([item['caseId'] for item in result['suite']['cases']], ['dev'])
        self.assertNotIn('失败任务需要谁来确认', json.dumps(result, ensure_ascii=False))
        for inputs in ({**self.suite['cases'][1], 'verdict': 'approved'},
                       {**self.suite['cases'][0], 'verdict': 'approved', 'reviewAuthor': 'human'},
                       {**self.suite['cases'][0], 'verdict': 'approved', 'split': 'holdout'}):
            with self.assertRaises(AgentLabProjectValidationError):
                app._execution(project, 'execution_command', {'bindingId': 'binding', 'action': 'review_case',
                    'expectedRevision': self.suite['revision'], 'clientRequestId': 'invalid-edit', 'input': inputs})
        safe = app._guide_job({'kind': 'experiment', 'result': {
            'development': {'cases': [{'answer': 'development detail'}]},
            'holdout': {'cases': [{'answer': 'HOLDOUT-SECRET'}], 'baselineMetrics': {'passRate': 0.5}},
            'receipts': [{'text': 'HOLDOUT-SECRET'}], 'comparison': {'decision': 'inconclusive'}}})
        self.assertIn('development detail', str(safe))
        self.assertNotIn('HOLDOUT-SECRET', str(safe))
        self.assertEqual(safe['result']['holdout']['baselineMetrics']['passRate'], 0.5)

    def test_guide_knowledge_is_project_scoped_and_rejects_path_escape(self):
        app = self.project_application()
        project = {'projectId': 'project-one', 'guideSessionId': 'guide'}
        app.store.read = Mock(return_value={'project': project})
        app.knowledge = Mock()
        app.knowledge._jobs.return_value = [{'jobId': 'owned'}]
        app.knowledge.read.return_value = {'jobs': [{'jobId': 'owned', 'result': {
            'split': 'holdout', 'report': {'metrics': {'mrr': 0.5}, 'cases': ['HOLDOUT-SECRET']}}}]}
        app._knowledge_command = Mock(return_value={'ok': True})
        session = {'id': 'guide', 'surfaceKind': 'extension_app', 'ownerAppId': 'extension:agent-lab',
                   'surfaceKey': 'project.project-one.guide', 'workspaceRoots': [str(self.root)]}
        observed = app.tool(session, 'knowledge_read', {})
        self.assertNotIn('HOLDOUT-SECRET', str(observed))
        self.assertIn('mrr', str(observed))
        with self.assertRaises(AgentLabProjectValidationError):
            app.tool(session, 'knowledge_read', {'jobId': 'foreign'})
        command = {'expectedRevision': 1, 'clientRequestId': 'import',
                   'input': {'operation': 'import_corpus', 'path': str(self.root / 'corpus.jsonl')}}
        app.tool(session, 'knowledge_command', command)
        app._knowledge_command.assert_called_once_with({'action': 'knowledge', 'projectId': 'project-one', **command})
        for path in ('../outside', str(self.root.parent / 'outside')):
            with self.assertRaises(AgentLabProjectValidationError):
                app.tool(session, 'knowledge_command', {**command, 'input': {'operation': 'import_corpus', 'path': path}})

    def test_guide_app_call_uses_existing_owner_after_project_binding_check(self):
        app = self.project_application()
        app.store.read = Mock(return_value={'project': {'projectId': 'one', 'guideSessionId': 'guide'}})
        app.apps.read = Mock(return_value={'app': {'appId': 'app-one'}})
        app.command_app = Mock(return_value={'ok': True, 'call': {'callId': 'original'}})
        session = {'id': 'guide', 'surfaceKind': 'extension_app', 'ownerAppId': 'extension:agent-lab',
                   'surfaceKey': 'project.one.guide'}
        command = {'appId': 'app-one', 'action': 'invoke', 'expectedRevision': 2, 'clientRequestId': 'invoke-original',
                   'input': {'version': 1, 'actionId': 'research', 'values': {'question': 'Inspect actual sources'}}}
        self.assertEqual(app.tool(session, 'app_command', command)['call']['callId'], 'original')
        app.apps.read.assert_called_once_with({'projectId': 'one', 'appId': 'app-one'})
        app.command_app.assert_called_once_with(command)
        app.apps.read.side_effect = AgentLabProjectValidationError('foreign app')
        with self.assertRaises(AgentLabProjectValidationError):
            app.tool(session, 'app_command', {**command, 'appId': 'foreign'})
        app.command_app.assert_called_once()
