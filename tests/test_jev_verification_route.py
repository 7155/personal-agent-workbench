"""Jev may deliver sufficient evidence directly, or request a real Pi check."""
import json
from pathlib import Path
from unittest.mock import patch

from rag_ime.agent_tools import ControlToolGateway
from rag_ime.jev_tasks.decider import JevChoices
from rag_ime.jev_tasks.types import GraphConflict
from rag_ime.jev_tasks.verification_route import accept_existing_evidence
from tests import test_jev_host_application as host
from tests import test_jev_failure_lifecycle as failure


class JevVerificationRouteTests(host.JevHostFixture):
    effects = failure.JevFailureLifecycleTests.effects
    active_effect = failure.JevFailureLifecycleTests.active_effect
    submit = failure.JevFailureLifecycleTests.submit
    finish = failure.JevFailureLifecycleTests.finish

    def setUp(self):
        super().setUp()
        self.gateway = ControlToolGateway(
            sessions=self.service.sessions, management=object(), core=object(),
            project=self.service.project, collaboration=self.service,
            background_jobs=self.service.background_jobs, delegation=self.service.delegation,
            work_documents=self.service.work_documents,
        )
        self.service.bind_tool_manifest_provider(self.gateway.runtime_manifests)
        self.terminals = {}
        original = self.app.execution_terminal
        p = patch.object(self.app, 'execution_terminal', side_effect=lambda effect, **kwargs:
                         self.terminals.get(effect['effectId']) or original(effect, **kwargs))
        p.start()
        self.addCleanup(p.stop)
        self.route_calls = []
        self.route = 'accept_existing_evidence'
        self.on_route = lambda: None
        def choose(state, questions):
            packet = json.loads(state)
            if not packet.get('work', {}).get('context', {}).get('verificationRoute'):
                return failure.choose_valid_progress(state, questions)
            self.route_calls.append(packet['work']['context'])
            self.on_route()
            choice = next(a['id'] for a in packet['actions'] if a['operation'] == self.route)
            return {'model': 'test-jev', 'answers': {'decision': {'type': 'choice', 'choice': choice,
                'confidence': 1.0, 'probabilities': {k: float(k == choice) for k in questions['decision']['criteria']}}}}
        self.app.driver.controller.decider = JevChoices(choose)

    def submitted(self, mode='auto'):
        created = self.app.create(self.room['id'], {'clientMessageId': 'greeting', 'message': 'hi',
            'strategy': 'direct', 'modelRouting': 'participant', 'verificationMode': mode})
        worker = self.active_effect(created, 'execute')
        self.submit(worker, 'result_submit', {'resultSummary': 'Hi! 👋', 'artifactRefs': [], 'evidenceRefs': ['test:result-content']})
        return created, worker

    def test_self_contained_answer_finishes_without_an_extra_pi_turn(self):
        created, worker = self.submitted()
        self.finish(worker)
        for _ in range(4):
            self.app.tick(limit=16)
        view = self.app.projection(self.room['id'], created['graphId'])
        self.assertEqual(view['final'].get('content'), 'Hi! 👋', view)
        self.assertEqual(len(self.effects(created, 'execute')), 1)
        self.assertEqual(self.effects(created, 'verify'), [])
        self.assertEqual(len(self.route_calls), 1)
        self.assertEqual(self.route_calls[0]['task']['result'], 'Hi! 👋')
        self.app.recover()
        self.app.tick(limit=16)
        self.assertEqual(len(self.route_calls), 1)
        self.assertEqual(len(self.calls), 1)
        with self.app.ledger.connection() as conn:
            decision = conn.execute("SELECT result_json FROM agent_jev_commands WHERE operation='verification_route'").fetchone()
        self.assertEqual(json.loads(decision[0])['choice'], 'accept_existing_evidence')

    def test_jev_can_request_an_additional_inspection(self):
        self.route = 'verify'
        created, worker = self.submitted()
        self.finish(worker)
        verifier = self.active_effect(created, 'verify')
        self.assertNotEqual(verifier['request']['ownerId'], worker['request']['ownerId'])
        self.assertEqual(len(self.route_calls), 1)
        self.app.tick(limit=16)
        self.assertEqual(len(self.route_calls), 1)

    def test_explicit_independent_policy_does_not_offer_direct_acceptance(self):
        created, worker = self.submitted('independent')
        self.finish(worker)
        self.active_effect(created, 'verify')
        self.assertEqual(self.route_calls, [])

    def test_worker_submission_without_drain_cannot_finish(self):
        created, _ = self.submitted()
        self.app.tick(limit=16)
        self.assertEqual(self.route_calls, [])
        self.assertFalse(self.app.projection(self.room['id'], created['graphId'])['final'])

    def test_file_changes_while_jev_decides_reject_that_acceptance(self):
        for session in self.sessions:
            self.service.sessions.set_runtime_policy(session['id'], mode='coordinator',
                tool_profile_version='control-center-v1', execution_mode='workspace_managed',
                workspace_roots=[self.tmp.name], allowed_tools=None)
        artifact = Path(self.tmp.name) / 'result.md'
        artifact.write_text('first result')
        created = self.create('file-change')
        worker = self.active_effect(created, 'execute')
        self.submit(worker, 'result_submit', {'resultSummary': 'Saved',
            'artifactRefs': ['workspace:' + str(artifact)], 'evidenceRefs': ['test:readback']})
        self.finish(worker)
        snapshot = self.snapshot(created)
        task = snapshot.task(worker['request']['taskId'])
        fact = self.app.executions(snapshot)[task.id]
        self.on_route = lambda: artifact.write_text('changed during decision')
        with self.assertRaises(GraphConflict):
            accept_existing_evidence(self.app.lifecycle, snapshot, task, fact,
                                     self.app.lifecycle.policy(created['graphId']))
        self.assertEqual(self.snapshot(created).task(task.id).state, 'review')

    def test_stop_while_jev_decides_does_not_accept_the_result(self):
        created, worker = self.submitted()
        self.finish(worker)
        snapshot = self.snapshot(created)
        task = snapshot.task(worker['request']['taskId'])
        fact = self.app.executions(snapshot)[task.id]
        self.on_route = lambda: self.app.stop(self.room['id'], created['rootId'])
        with self.assertRaises(GraphConflict):
            accept_existing_evidence(self.app.lifecycle, snapshot, task, fact,
                                     self.app.lifecycle.policy(created['graphId']))
        self.assertNotEqual(self.snapshot(created).task(task.id).state, 'done')
