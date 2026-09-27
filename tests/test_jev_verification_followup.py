"""Missing evidence gets bounded independent verification, never code rework.

Real owner/store transitions; finite model choice and Pi terminal are fixtures.
"""
import json
from unittest.mock import patch

from rag_ime.agent_tools import ControlToolGateway
from rag_ime.jev_tasks.decider import JevChoices
from tests import test_jev_failure_lifecycle as failure
from tests import test_jev_host_application as host


class JevVerificationFollowupTests(host.JevHostFixture):
    effects = failure.JevFailureLifecycleTests.effects
    active_effect = failure.JevFailureLifecycleTests.active_effect
    submit = failure.JevFailureLifecycleTests.submit
    finish = failure.JevFailureLifecycleTests.finish
    assert_failed_final = failure.JevFailureLifecycleTests.assert_failed_final
    result = staticmethod(failure.JevFailureLifecycleTests.result)
    verdict = staticmethod(failure.JevFailureLifecycleTests.verdict)

    def setUp(self):
        super().setUp()
        self.gateway = ControlToolGateway(sessions=self.service.sessions, management=object(), core=object(),
            project=self.service.project, collaboration=self.service, background_jobs=self.service.background_jobs,
            delegation=self.service.delegation, work_documents=self.service.work_documents)
        self.service.bind_tool_manifest_provider(self.gateway.runtime_manifests)
        self.app.driver.controller.decider = JevChoices(failure.choose_valid_progress)
        self.terminals = {}
        original = self.app.execution_terminal
        p = patch.object(self.app, 'execution_terminal', side_effect=lambda effect, **kw:
            self.terminals.get(effect['effectId']) or original(effect, **kw))
        p.start(); self.addCleanup(p.stop)

    def submitted_worker(self):
        created = self.create()
        worker = self.active_effect(created, 'execute')
        self.submit(worker, 'result_submit', self.result('fixed-artifact'))
        self.finish(worker)
        return created, worker

    @staticmethod
    def missing():
        return {'operabilityVerdict': 'passed', 'requirementVerdict': 'unverified',
                'reason': 'Only the existing saved input trace remains unread.',
                'evidenceRefs': ['fixture:existing-input-trace']}

    def test_followup_preserves_work_version_and_accepts_only_new_independent_evidence(self):
        created, worker = self.submitted_worker()
        initial = self.snapshot(created).task(worker['request']['taskId'])
        verifier = self.active_effect(created, 'verify')
        self.submit(verifier, 'verification_submit', self.missing())
        self.app.tick(limit=16)
        self.assertEqual(len(self.effects(created, 'verify')), 1, 'ACK is not drain')
        self.finish(verifier)
        followup = self.active_effect(created, 'verify')
        current = self.snapshot(created).task(initial.id)
        self.assertEqual(current, initial)
        self.assertEqual(len(self.effects(created, 'execute')), 1)
        self.assertEqual(followup['request']['subjectHash'], verifier['request']['subjectHash'])
        self.assertNotEqual(followup['request']['ownerId'], worker['request']['ownerId'])
        self.assertIn('existing saved input trace', followup['request']['taskBrief']['objective'])
        self.assertIn('fixture:existing-input-trace', followup['request']['taskBrief']['objective'])
        # Restart/repeated wakes retain the same in-flight verification.
        self.app.recover()
        self.app.tick(limit=16)
        self.assertEqual(len(self.effects(created, 'verify')), 2)
        self.submit(followup, 'verification_submit', self.verdict())
        self.finish(followup)
        for _ in range(4): self.app.tick(limit=16)
        final = self.snapshot(created).task(initial.id)
        self.assertEqual(final.state, 'done')
        self.assertEqual(final.revision, initial.revision)
        self.assertEqual(final.accepted_turn_id, initial.accepted_turn_id)
        with self.app.ledger.connection() as conn:
            first = json.loads(conn.execute('SELECT payload_json FROM agent_jev_execution_outputs WHERE dispatch_id=?',
                (verifier['effectId'],)).fetchone()[0])
            second = json.loads(conn.execute('SELECT payload_json FROM agent_jev_execution_outputs WHERE dispatch_id=?',
                (followup['effectId'],)).fetchone()[0])
            self.assertEqual(first, self.missing())
            self.assertEqual(second, self.verdict())
            current_dispatch = conn.execute('SELECT dispatch_id FROM agent_jev_verifications WHERE task_id=?',
                (initial.id,)).fetchone()[0]
            self.assertEqual(current_dispatch, followup['effectId'])

    def test_three_unverified_attempts_close_unresolved_without_execution_revisions(self):
        created, worker = self.submitted_worker()
        task_id = worker['request']['taskId']
        for attempt in range(3):
            verifier = self.active_effect(created, 'verify')
            self.assertEqual(len(self.effects(created, 'verify')), attempt + 1)
            self.submit(verifier, 'verification_submit', self.missing())
            self.finish(verifier)
        view = self.assert_failed_final(created)
        self.assertIn('补证', view['final']['content'])
        task = self.snapshot(created).task(task_id)
        self.assertEqual(task.revision, 0)
        self.assertEqual(len(self.effects(created, 'execute')), 1)
        self.assertEqual(len(self.effects(created, 'verify')), 3)
        blocker = self.service.room_work.get(task_id)['blocker']
        self.assertIn('补证', blocker['reason'])
        self.assertTrue(blocker['terminal'])

    def test_proven_failure_after_followup_revises_original_task_once(self):
        created, worker = self.submitted_worker()
        first = self.active_effect(created, 'verify')
        self.submit(first, 'verification_submit', self.missing())
        self.finish(first)
        followup = self.active_effect(created, 'verify')
        self.submit(followup, 'verification_submit', self.verdict(passed=False))
        self.finish(followup)
        repair = self.active_effect(created, 'execute')
        self.assertEqual(repair['request']['taskId'], worker['request']['taskId'])
        self.assertEqual(repair['request']['taskRevision'], 1)
        self.assertEqual(len(self.effects(created, 'execute')), 2)

    def test_evidence_retry_is_still_available_after_real_revision_budget_is_used(self):
        created, worker = self.submitted_worker()
        for _ in range(2):
            verifier = self.active_effect(created, 'verify')
            self.submit(verifier, 'verification_submit', self.verdict(passed=False))
            self.finish(verifier)
            worker = self.active_effect(created, 'execute')
            self.submit(worker, 'result_submit', self.result('actual-repair'))
            self.finish(worker)
        self.assertEqual(self.snapshot(created).task(worker['request']['taskId']).revision, 2)
        verifier = self.active_effect(created, 'verify')
        self.submit(verifier, 'verification_submit', self.missing())
        self.finish(verifier)
        followup = self.active_effect(created, 'verify')
        self.assertEqual(len(self.effects(created, 'execute')), 3)
        self.submit(followup, 'verification_submit', self.verdict())
        self.finish(followup)
        for _ in range(4): self.app.tick(limit=16)
        task = self.snapshot(created).task(worker['request']['taskId'])
        self.assertEqual((task.state, task.revision), ('done', 2))

    def test_stop_fences_followup_output_and_never_creates_another_verifier(self):
        created, worker = self.submitted_worker()
        first = self.active_effect(created, 'verify')
        self.submit(first, 'verification_submit', self.missing())
        self.finish(first)
        followup = self.active_effect(created, 'verify')
        self.app.stop(self.room['id'], created['rootId'])
        # Stop retires the public turn binding; its stale Tool operation is
        # rejected before any new structured output can reach the JEV owner.
        with self.assertRaises(ValueError):
            self.submit(followup, 'verification_submit', self.verdict())
        self.finish(followup, failed=True)
        self.app.recover()
        self.app.tick(limit=16)
        self.assertEqual(len(self.effects(created, 'execute')), 1)
        self.assertEqual(len(self.effects(created, 'verify')), 2)
        with self.app.ledger.connection() as conn:
            self.assertIsNone(conn.execute('SELECT 1 FROM agent_jev_execution_outputs WHERE dispatch_id=?',
                (followup['effectId'],)).fetchone())
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM agent_jev_executor_claims WHERE graph_id=?',
                (created['graphId'],)).fetchone()[0], 0)
