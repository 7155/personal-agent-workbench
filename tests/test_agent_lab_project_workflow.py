from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from rag_ime.agent_lab.project_application import AgentLabProjectApplication


class ProjectWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.project = {
            'projectId': 'project-one', 'bindings': [], 'artifacts': [], 'applications': [],
            'materialSetId': 'materials-one', 'materialSet': {'version': 1, 'materials': []},
        }

    def project_read(self, *, knowledge=None, golden=None, app_calls=None):
        app = AgentLabProjectApplication(Path(tempfile.gettempdir()) / 'unused-workflow.sqlite',
            session_application=Mock(), current_model=lambda: {}, knowledge=knowledge, read_golden=golden)
        app.store = Mock()
        app.store.read.return_value = {'project': self.project}
        app.apps = Mock()
        app.apps.project_calls.return_value = app_calls or {'calls': [], 'counts': {}, 'totalCount': 0, 'truncated': False}
        return app.read({'projectId': 'project-one'})

    def test_project_read_includes_actual_app_calls_without_quality_inference(self):
        calls = {'projectId': 'project-one', 'calls': [
            {'callId': 'app-running', 'appId': 'app-one', 'version': 3, 'title': 'Frozen Polar', 'actionId': 'research',
             'actionTitle': '研究', 'state': 'running', 'sessionId': 'pi-one', 'updatedAtMs': 15,
             'progress': {'stage': 'researching', 'executedToolCalls': 3, 'executedSourceReadCalls': 2},
             'input': 'SECRET_QUESTION', 'result': 'SECRET_ANSWER'},
            {'callId': 'app-finished', 'appId': 'app-one', 'version': 2, 'title': 'Older Polar', 'actionId': 'research',
             'actionTitle': '研究', 'state': 'completed', 'progress': {}, 'decision': 'improved'}],
            'counts': {'running': 1, 'completed': 1}, 'totalCount': 2, 'truncated': False}
        result = self.project_read(app_calls=calls)['project']['workflow']
        nodes = {n['id']: n for n in result['nodes']}
        running = nodes['app-running']
        self.assertEqual(running['ref'], {'kind': 'application_call', 'id': 'app-running', 'version': 3})
        self.assertIn({'kind': 'application', 'id': 'app-one', 'version': 3}, running['evidenceRefs'])
        self.assertEqual(running['status'], 'running')
        self.assertIn('3', running['summary'])
        self.assertEqual(result['counts']['running'], 1)
        self.assertEqual(result['counts']['completed'], 1)
        self.assertEqual(result['currentNodeId'], 'app-running')
        self.assertNotIn('decision', nodes['app-finished'])
        self.assertIn('不代表', nodes['app-finished']['summary'])
        self.assertNotIn('SECRET_', json.dumps(result))

    def test_app_call_count_denominators_survive_bounded_history(self):
        from rag_ime.agent_lab.project_workflow import project_workflow
        flow = project_workflow(self.project, app_calls={'calls': [
            {'callId': 'one', 'appId': 'app', 'version': 1, 'state': 'running'}],
            'counts': {'running': 3, 'queued': 2, 'completed': 40, 'failed': 4, 'cancelled': 5, 'interrupted': 6},
            'totalCount': 60, 'truncated': True})
        self.assertEqual(flow['counts'], {'running': 3, 'queued': 2, 'completed': 40, 'failed': 4})
        self.assertEqual(flow['applicationCalls']['counts']['terminal'], 49)
        self.assertFalse(flow['complete'])

    def test_followup_reuses_completed_call_as_dependency_without_new_tool_claim(self):
        calls = {'calls': [
            {'callId': 'followup', 'appId': 'app-one', 'version': 7, 'state': 'completed',
             'progress': {'executedToolCalls': 0, 'executedSourceReadCalls': 0},
             'evidenceReuse': {'sourceCallId': 'original', 'sourceAppVersion': 5, 'windowCount': 34}},
            {'callId': 'original', 'appId': 'app-one', 'version': 5, 'state': 'completed'}]}
        flow = self.project_read(app_calls=calls)['project']['workflow']
        node = next(row for row in flow['nodes'] if row['id'] == 'followup')
        self.assertIn({'source': 'original', 'target': 'followup'}, flow['edges'])
        self.assertIn('复用前次 34 个原文窗口', node['summary'])
        self.assertIn('已执行 0 次知识工具', node['summary'])
        self.assertIn({'kind': 'application_call', 'id': 'original', 'version': 5}, node['evidenceRefs'])
        self.assertNotIn('decision', node)

    def test_followup_does_not_link_a_conflicting_visible_application(self):
        calls = {'calls': [
            {'callId': 'followup', 'appId': 'app-one', 'version': 7, 'state': 'completed',
             'evidenceReuse': {'sourceCallId': 'foreign', 'sourceAppVersion': 5, 'windowCount': 34}},
            {'callId': 'foreign', 'appId': 'app-two', 'version': 5, 'state': 'completed'}]}
        flow = self.project_read(app_calls=calls)['project']['workflow']
        node = next(row for row in flow['nodes'] if row['id'] == 'followup')
        self.assertNotIn('复用', node['summary'])
        self.assertNotIn('foreign', node['dependencies'])
        self.assertFalse(any(ref['id'] == 'foreign' for ref in node['evidenceRefs']))

    def test_followup_preserves_exact_source_receipt_outside_bounded_history(self):
        calls = {'calls': [
            {'callId': 'followup', 'appId': 'app-one', 'version': 7, 'state': 'completed',
             'evidenceReuse': {'sourceCallId': 'old-call', 'sourceAppVersion': 5, 'windowCount': 34}}],
            'truncated': True}
        flow = self.project_read(app_calls=calls)['project']['workflow']
        node = next(row for row in flow['nodes'] if row['id'] == 'followup')
        self.assertIn({'kind': 'application_call', 'id': 'old-call', 'version': 5}, node['evidenceRefs'])
        self.assertNotIn('old-call', node['dependencies'])

    def test_selected_read_contains_workflow_without_starting_workers(self):
        result = self.project_read()
        self.assertEqual(result['project']['workflow']['schemaVersion'], 'paw.lab-project-workflow.v1')
        self.assertEqual(result['project']['workflow']['counts']['running'], 0)

    def test_parallel_jobs_and_completed_dependency_survive_refresh(self):
        from rag_ime.agent_lab.project_workflow import project_workflow
        knowledge = {'jobs': [
            {'jobId': 'corpus-one', 'state': 'completed', 'publicSpec': {'operation': 'import_corpus'}},
            {'jobId': 'index-one', 'state': 'running', 'publicSpec': {'operation': 'index', 'corpusId': 'corpus-one'}},
            {'jobId': 'index-two', 'state': 'running', 'publicSpec': {'operation': 'index', 'corpusId': 'corpus-one'}},
        ], 'corpora': [{'jobId': 'corpus-one', 'documentCount': 211}]}
        first = project_workflow(self.project, knowledge=knowledge, executions={}, observed_at_ms=10)
        second = project_workflow(self.project, knowledge=knowledge, executions={}, observed_at_ms=20)
        self.assertEqual(first['nodes'], second['nodes'])
        self.assertEqual(first['counts'], {'running': 2, 'queued': 0, 'completed': 1, 'failed': 0})
        self.assertIn({'source': 'corpus-one', 'target': 'index-two'}, first['edges'])
        self.assertEqual(len([n for n in first['nodes'] if n['id'] == 'corpus-one']), 1)

    def test_draft_success_does_not_become_experiment_or_publish_decision(self):
        from rag_ime.agent_lab.project_workflow import project_workflow
        self.project['bindings'] = [{'bindingId': 'binding-one', 'ownerRef': {'kind': 'golden_suite', 'id': 'suite-one'}}]
        flow = project_workflow(self.project, executions={'binding-one': {'suite': {'jobs': [
            {'jobId': 'draft-one', 'kind': 'draft', 'state': 'completed',
             'result': {'comparison': {'decision': 'improved'}}}]} }})
        node = next(n for n in flow['nodes'] if n['id'] == 'draft-one')
        self.assertEqual(node['kind'], 'dataset')
        self.assertNotIn('decision', node)
        self.assertFalse(any(n['kind'] == 'experiment' and n['status'] == 'completed' for n in flow['nodes']))

    def test_retrieval_result_has_single_metrics_and_real_query_denominator(self):
        from rag_ime.agent_lab.project_workflow import project_workflow
        result = {'jobId': 'eval-one', 'kind': 'evaluation', 'evaluatedCount': 37, 'plannedCount': 40,
                  'profile': {'mode': 'hybrid', 'topK': 16, 'rerank': False, 'prompt': 'SECRET_PROFILE'},
                  'report': {'privateCases': ['SECRET_CASE'], 'metrics': {'queryCount': 37, 'metrics': {
                      'mrr': .47, 'recallAtK': {'1': .28, '16': .82}, 'ndcgAtK': {'16': .53}}}}}
        flow = project_workflow(self.project, knowledge={'jobs': [
            {'jobId': 'eval-one', 'state': 'completed', 'publicSpec': {'operation': 'evaluate'}, 'result': result}],
            'evaluations': [result]})
        node = next(n for n in flow['nodes'] if n['id'] == 'eval-one')
        self.assertEqual(node['title'], '检索评测')
        self.assertEqual({metric['label']: metric['value'] for metric in node['metrics']},
                         {'MRR': .47, 'Recall@1': .28, 'Recall@16': .82, 'nDCG@16': .53})
        for metric in node['metrics']:
            self.assertIsNone(metric['baseline'])
            self.assertIsNone(metric['candidate'])
            self.assertEqual(metric['sampleCount'], 37)
        self.assertIn('37 / 40', node['summary'])
        self.assertEqual(node['factors'][0], {'name': 'mode', 'before': '', 'after': 'hybrid', 'reason': '本次检索配置'})
        self.assertNotIn('SECRET_', json.dumps(flow))
        self.assertNotIn('decision', node)

    def test_retrieval_metrics_do_not_override_failed_owner_or_invent_denominator(self):
        from rag_ime.agent_lab.project_workflow import project_workflow
        for state in ('cancelled', 'failed', 'completed'):
            for count in (None, True, -1, 3.5):
                with self.subTest(state=state, count=count):
                    result = {'jobId': 'eval-one', 'kind': 'evaluation', 'plannedCount': 40,
                              'report': {'metrics': {'queryCount': count, 'metrics': {
                                  'mrr': .5, 'recallAtK': {'1': float('nan'), 'secret': .9}}}}}
                    flow = project_workflow(self.project, knowledge={'jobs': [
                        {'jobId': 'eval-one', 'state': state, 'publicSpec': {'operation': 'evaluate'}, 'result': result}],
                        'evaluations': [result]})
                    node = next(n for n in flow['nodes'] if n['id'] == 'eval-one')
                    self.assertEqual(node['status'], state)
                    if state == 'completed':
                        self.assertEqual(node['metrics'], [{'label': 'MRR', 'value': .5, 'baseline': None,
                                                           'candidate': None, 'unit': 'ratio'}])
                    else:
                        self.assertNotIn('metrics', node)

    def test_restored_index_keeps_runtime_identity_and_kind(self):
        from rag_ime.agent_lab.project_workflow import project_workflow
        flow = project_workflow(self.project, knowledge={'jobs': [
            {'jobId': 'restored-index', 'state': 'completed', 'publicSpec': {'operation': 'restore_index'}}]})
        node = next(n for n in flow['nodes'] if n['id'] == 'restored-index')
        self.assertEqual(node['kind'], 'index')
        self.assertEqual(node['title'], '恢复已有索引')
        self.assertEqual(node['ref'], {'kind': 'knowledge_job', 'id': 'restored-index'})

    def test_agent_review_is_dataset_work_with_original_child_receipts(self):
        from rag_ime.agent_lab.project_workflow import project_workflow
        self.project['bindings'] = [{'bindingId': 'binding-one', 'ownerRef': {'kind': 'golden_suite', 'id': 'suite-one'}}]
        for state in ('running', 'completed', 'cancelled'):
            with self.subTest(state=state):
                flow = project_workflow(self.project, executions={'binding-one': {'suite': {'jobs': [
                    {'jobId': 'review-one', 'kind': 'review', 'state': state, 'result': {
                        'reviewedCount': 12, 'approvedCount': 10, 'reviews': ['SECRET_LABELS'],
                        'comparison': {'decision': 'improved'}, 'receipts': [
                            {'requestId': 'review-request', 'stage': 'review', 'receipt': {'status': 'succeeded'}}]}}]}}})
                nodes = {n['id']: n for n in flow['nodes']}
                node = nodes['review-one']
                self.assertEqual((node['kind'], node['title'], node['status'], node['source']),
                                 ('dataset', 'Agent 核对题集', state, 'runtime'))
                self.assertEqual(nodes['review-request']['title'], '题目核对调用')
                self.assertEqual(node['children'], ['review-request'])
                self.assertEqual(flow['counts']['completed'], int(state == 'completed'))
                self.assertNotIn('decision', node)
                self.assertNotIn('SECRET_', json.dumps(flow))

    def test_receipts_are_children_without_raw_answers_or_duplicate_job_counts(self):
        from rag_ime.agent_lab.project_workflow import project_workflow
        self.project['bindings'] = [{'bindingId': 'binding-one', 'ownerRef': {'kind': 'golden_suite', 'id': 'suite-one'}}]
        result = {'comparison': {'decision': 'improved'}, 'baseline': {'prompt': 'SECRET_PROMPT'},
                  'holdout': {'cases': [{'answer': 'SECRET_ANSWER', 'references': ['SECRET_REFERENCE']}],
                              'baselineMetrics': {'passRate': .5}, 'candidateMetrics': {'passRate': 1}},
                  'receipts': [{'requestId': 'request-one', 'stage': 'answer', 'output': 'SECRET_OUTPUT',
                                'receipt': {'status': 'completed'}}]}
        job = {'jobId': 'experiment-one', 'kind': 'experiment', 'state': 'completed', 'result': result}
        flow = project_workflow(self.project, executions={'binding-one': {'suite': {'jobs': [job, job]}}})
        nodes = {n['id']: n for n in flow['nodes']}
        self.assertEqual(nodes['request-one']['parentId'], 'experiment-one')
        self.assertEqual(nodes['experiment-one']['children'], ['request-one'])
        self.assertEqual(flow['counts']['completed'], 1)
        self.assertEqual(nodes['experiment-one']['decision'], 'improved')
        self.assertNotIn('SECRET_', json.dumps(flow))

    def test_failed_owner_read_remains_unavailable_not_empty_success(self):
        self.project['bindings'] = [{'bindingId': 'binding-one', 'adapterId': 'golden.knowledge_qa',
                                    'ownerRef': {'kind': 'golden_suite', 'id': 'suite-one'}}]
        knowledge = Mock()
        knowledge.read.side_effect = OSError('SECRET_SOURCE_PATH')
        result = self.project_read(knowledge=knowledge, golden=Mock(side_effect=OSError('SECRET_PROVIDER')))
        flow = result['project']['workflow']
        self.assertFalse(flow['complete'])
        self.assertTrue(any(n['id'] == 'binding-one' and n['status'] == 'unavailable' for n in flow['nodes']))
        self.assertIn('knowledge', flow['unavailableOwners'])
        self.assertNotIn('SECRET_', json.dumps(flow))

    def test_missing_dependency_has_original_reference_and_unavailable_state(self):
        from rag_ime.agent_lab.project_workflow import project_workflow
        flow = project_workflow(self.project, knowledge={'jobs': [
            {'jobId': 'index-one', 'state': 'interrupted', 'publicSpec': {'operation': 'index', 'corpusId': 'missing-corpus'}}]})
        nodes = {n['id']: n for n in flow['nodes']}
        self.assertEqual(nodes['missing-corpus']['status'], 'unavailable')
        self.assertEqual(nodes['index-one']['status'], 'interrupted')
        self.assertEqual(nodes['index-one']['dependencies'], ['missing-corpus'])

    def test_completed_resource_does_not_override_cancelled_owner_job(self):
        from rag_ime.agent_lab.project_workflow import project_workflow
        flow = project_workflow(self.project, knowledge={
            'jobs': [{'jobId': 'corpus-one', 'state': 'cancelled', 'publicSpec': {'operation': 'import_corpus'}}],
            'corpora': [{'jobId': 'corpus-one', 'documentCount': 211}]})
        node = next(n for n in flow['nodes'] if n['id'] == 'corpus-one')
        self.assertEqual(node['status'], 'cancelled')

    def test_golden_owner_is_read_once_per_suite_and_only_projection_is_returned(self):
        self.project['bindings'] = [
            {'bindingId': f'binding-{n}', 'adapterId': 'golden.knowledge_qa',
             'ownerRef': {'kind': 'golden_suite', 'id': 'suite-one'}} for n in (1, 2)]
        golden = Mock(return_value={'suite': {'suiteId': 'suite-one', 'cases': [{'answer': 'SECRET_HOLDOUT'}],
            'jobs': [{'jobId': 'job-one', 'kind': 'experiment', 'state': 'running',
                      'result': {'cases': [{'answer': 'SECRET_HOLDOUT'}]}}]}})
        result = self.project_read(golden=golden)
        self.assertEqual(golden.call_count, 1)
        self.assertEqual(result['project']['workflow']['counts']['running'], 1)
        self.assertNotIn('SECRET_HOLDOUT', json.dumps(result))

    def test_project_specific_steps_use_declared_edges_and_runtime_overrides_claim(self):
        from rag_ime.agent_lab.project_workflow import project_workflow
        artifact = {'artifactId': 'plan-one', 'revision': 2, 'content': {
            'schemaVersion': 'paw.lab-project-progress.v1', 'steps': [
                {'id': 'inspect-grid', 'title': '核对网格候选点', 'state': 'completed', 'summary': '已发布地图'},
                {'id': 'compare-grid', 'title': '对比网格方案', 'state': 'completed', 'summary': '声称已完成',
                 'dependsOn': ['inspect-grid'], 'jobId': 'job-grid'},
                {'id': 'code-fix', 'title': '修正地图交互', 'state': 'pending', 'dependsOn': ['compare-grid']},
        ]}}
        knowledge = {'jobs': [{'jobId': 'job-grid', 'state': 'running', 'publicSpec': {'operation': 'evaluate'}}]}
        flow = project_workflow(self.project, knowledge=knowledge, progress_artifact=artifact)
        nodes = {node['id']: node for node in flow['nodes']}
        self.assertEqual(nodes['inspect-grid']['source'], 'artifact')
        self.assertEqual(nodes['inspect-grid']['kind'], 'step')
        self.assertEqual(nodes['job-grid']['status'], 'running')
        self.assertEqual(nodes['job-grid']['source'], 'runtime')
        self.assertNotIn('compare-grid', nodes)
        self.assertEqual(nodes['job-grid']['dependencies'], ['inspect-grid'])
        self.assertEqual(nodes['code-fix']['dependencies'], ['job-grid'])
        self.assertEqual(flow['counts']['running'], 1)
        self.assertNotIn('声称已完成', nodes['job-grid']['summary'])

    def test_explicit_current_plan_focus_keeps_historical_failures_inspectable(self):
        from rag_ime.agent_lab.project_workflow import project_workflow
        knowledge = {'jobs': [
            {'jobId': 'old-failed', 'state': 'failed', 'publicSpec': {'operation': 'evaluate'}},
            {'jobId': 'old-interrupted', 'state': 'interrupted', 'publicSpec': {'operation': 'evaluate'}},
            {'jobId': 'current-result', 'state': 'completed', 'publicSpec': {'operation': 'evaluate'}},
        ]}
        original = project_workflow(self.project, knowledge=knowledge)
        self.assertEqual(original['currentNodeId'], 'old-interrupted')
        for reference in ({'jobId': 'current-result'},
                          {'evidenceRefs': [{'kind': 'knowledge_job', 'id': 'current-result'}]}):
            with self.subTest(reference=reference):
                plan = {'content': {'schemaVersion': 'paw.lab-project-progress.v1', 'steps': [
                    {'id': 'current-step', 'state': 'completed', **reference}]}}
                flow = project_workflow(self.project, knowledge=knowledge, progress_artifact=plan)
                self.assertEqual(flow['currentNodeId'], 'current-result')
                self.assertEqual(flow['counts'], original['counts'])
                before = {node['id']: node for node in original['nodes']}
                after = {node['id']: node for node in flow['nodes']}
                for identifier in ('old-failed', 'old-interrupted'):
                    self.assertEqual(after[identifier], before[identifier])

    def test_real_active_work_outside_plan_precedes_planned_focus(self):
        from rag_ime.agent_lab.project_workflow import project_workflow
        plan = {'content': {'schemaVersion': 'paw.lab-project-progress.v1', 'steps': [
            {'id': 'planned-active', 'state': 'active'}]}}
        for state in ('running', 'queued'):
            with self.subTest(state=state):
                flow = project_workflow(self.project, progress_artifact=plan, knowledge={'jobs': [
                    {'jobId': 'outside-plan', 'state': state, 'publicSpec': {'operation': 'evaluate'}}]})
                self.assertEqual(flow['currentNodeId'], 'outside-plan')
                self.assertEqual(flow['counts'][state], 1)
        flow = project_workflow(self.project, progress_artifact=plan)
        self.assertEqual(flow['currentNodeId'], 'planned-active')
        self.assertEqual(flow['counts']['running'], 0)

    def test_absent_or_invalid_plan_preserves_existing_failure_focus(self):
        from rag_ime.agent_lab.project_workflow import project_workflow
        for plan in (None, {'content': {'schemaVersion': 'unknown', 'steps': [
                {'id': 'next', 'state': 'active'}]}},
                {'content': {'schemaVersion': 'paw.lab-project-progress.v1', 'steps': []}},
                {'content': {'schemaVersion': 'paw.lab-project-progress.v1', 'steps': [
                    {'id': '', 'state': 'active'}]}}):
            with self.subTest(plan=plan):
                flow = project_workflow(self.project, progress_artifact=plan, knowledge={'jobs': [
                    {'jobId': 'old-failed', 'state': 'failed', 'publicSpec': {'operation': 'evaluate'}},
                    {'jobId': 'new-result', 'state': 'completed', 'publicSpec': {'operation': 'evaluate'}}]})
                self.assertEqual(flow['currentNodeId'], 'old-failed')

    def test_historical_experiment_chain_preserves_accepted_factors_without_new_runs(self):
        from rag_ime.agent_lab.project_workflow import project_workflow
        history = {'artifactId': 'history-one', 'revision': 3, 'content': {
            'schemaVersion': 'paw.lab-imported-experiments.v1', 'experiments': [
                {'experimentId': 'first', 'title': '模型对照', 'supersededBy': 'second',
                 'comparison': {'decision': 'reject', 'decisionReason': '质量不达标'}},
                {'experimentId': 'second', 'title': '修正枚举约束',
                 'comparison': {'decision': 'keep', 'decisionReason': '质量达标'},
                 'factors': [{'name': '枚举约束', 'before': '自由文本', 'after': '明确枚举', 'reason': '纠正非法取值'}],
                 'baseline': {'metrics': {'passRate': .5}, 'evidenceRefs': ['baseline.json']},
                 'candidate': {'metrics': {'passRate': 1}, 'evidenceRefs': ['candidate.json']}}]}}
        flow = project_workflow(self.project, history_artifact=history)
        node = next(n for n in flow['nodes'] if n['id'] == 'second')
        self.assertEqual(node['dependencies'], ['first'])
        self.assertEqual(node['source'], 'artifact')
        self.assertEqual(node['decision'], 'keep')
        self.assertEqual(node['factors'][0]['reason'], '纠正非法取值')
        self.assertIn({'kind': 'artifact', 'id': 'history-one', 'version': 3}, node['evidenceRefs'])
        self.assertEqual(flow['counts']['completed'], 0)

    def test_partial_result_cannot_publish_completed_experiment_metrics(self):
        from rag_ime.agent_lab.project_workflow import project_workflow
        self.project['bindings'] = [{'bindingId': 'binding-one', 'ownerRef': {'kind': 'golden_suite', 'id': 'suite-one'}}]
        flow = project_workflow(self.project, executions={'binding-one': {'suite': {'jobs': [
            {'jobId': 'job-partial', 'kind': 'experiment', 'state': 'completed',
             'result': {'partial': True, 'comparison': {'decision': 'improved'}}}]}}})
        node = next(n for n in flow['nodes'] if n['id'] == 'job-partial')
        self.assertEqual(node['status'], 'unavailable')
        self.assertNotIn('decision', node)

    def test_latest_persisted_progress_is_restored_by_a_new_application_instance(self):
        from rag_ime.agent_lab.projects import AgentLabProjectStore
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'workflow.sqlite'
            app = AgentLabProjectApplication(db, session_application=Mock(), current_model=lambda: {})
            app.store = AgentLabProjectStore(db)
            project = app.command({'action': 'create', 'projectId': '', 'expectedRevision': 0,
                'clientRequestId': 'create', 'input': {'description': '修复地图筛选后比较网格方案'}})['project']
            for number in (1, 2):
                project = app.command({'action': 'publish_artifact', 'projectId': project['projectId'],
                    'expectedRevision': project['revision'], 'clientRequestId': f'publish-{number}',
                    'input': {'title': f'项目步骤 {number}', 'view': 'json', 'content': {
                        'schemaVersion': 'paw.lab-project-progress.v1', 'steps': [
                            {'id': f'grid-step-{number}', 'title': '修复地图筛选', 'state': 'completed', 'summary': '交互已修复'}]}}})['project']
            before = app.read({'projectId': project['projectId']})['project']['workflow']
            reopened = AgentLabProjectApplication(db, session_application=Mock(), current_model=lambda: {})
            after = reopened.read({'projectId': project['projectId']})['project']['workflow']
            self.assertEqual(before['nodes'], after['nodes'])
            ids = {node['id'] for node in after['nodes']}
            self.assertIn('grid-step-2', ids)
            self.assertNotIn('grid-step-1', ids)

    def test_projection_limit_and_dependency_cycle_are_reported(self):
        from rag_ime.agent_lab.project_workflow import project_workflow
        flow = project_workflow(self.project, progress_artifact={'content': {
            'schemaVersion': 'paw.lab-project-progress.v1', 'steps': [
                {'id': 'a', 'state': 'pending', 'dependsOn': ['b']},
                {'id': 'b', 'state': 'pending', 'dependsOn': ['a']}]}})
        self.assertFalse(flow['complete'])
        self.assertIn('workflow:cycle', flow['unavailableOwners'])
        self.assertEqual(len(flow['edges']), 1)
        flow = project_workflow(self.project, knowledge={'jobs': [
            {'jobId': f'job-{n}', 'state': 'queued'} for n in range(1100)]})
        self.assertLessEqual(len(flow['nodes']), 1000)
        self.assertFalse(flow['complete'])
        self.assertIn('workflow:limit', flow['unavailableOwners'])

    def test_skill_change_uses_frozen_comparison_and_keeps_exact_suite_reference(self):
        from rag_ime.agent_lab.project_workflow import project_workflow
        self.project['bindings'] = [{'bindingId': 'binding-one', 'ownerRef': {'kind': 'golden_suite', 'id': 'suite-one'}}]
        comparison = {'scope': 'application_skill_body', 'changed': False,
            'baseline': {'kind': 'application_skill', 'title': '旧名称', 'sha256': 'same', 'body': 'DO_NOT_COPY', 'source': {'kind': 'inline'}},
            'candidate': {'kind': 'application_skill', 'title': '新名称', 'sha256': 'same', 'source': {'kind': 'inline'}}, 'diff': ''}
        flow = project_workflow(self.project, executions={'binding-one': {'suite': {'jobs': [
            {'jobId': 'job-skill', 'kind': 'experiment', 'state': 'completed',
             'result': {'optimizationScope': 'skill', 'applicationMethodComparison': comparison}}]}}})
        node = next(n for n in flow['nodes'] if n['id'] == 'job-skill')
        self.assertFalse(node['applicationMethodComparison']['changed'])
        self.assertEqual(node['optimization']['scope'], 'skill')
        self.assertIn({'kind': 'golden_suite', 'id': 'suite-one'}, node['evidenceRefs'])
        self.assertNotIn('DO_NOT_COPY', json.dumps(node))


if __name__ == '__main__':
    unittest.main()
