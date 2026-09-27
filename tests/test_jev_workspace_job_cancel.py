"""Real JEV/approval/job owners with isolated listening processes; no live Host."""
from __future__ import annotations

import json
import os
import shlex
import socket
import time
from pathlib import Path
from unittest.mock import patch

from rag_ime.agent_tools import ControlToolGateway
from rag_ime.agent_background_jobs import AgentBackgroundJobError
from tests.test_jev_host_application import JevHostFixture


class JevWorkspaceJobCancelTests(JevHostFixture):
    def setUp(self):
        super().setUp()
        self.root = Path(self.tmp.name)
        for session in self.sessions:
            self.service.sessions.set_runtime_policy(session['id'], mode='coordinator',
                tool_profile_version='control-center-full-access-v1', execution_mode='per_action',
                workspace_roots=[str(self.root)], allowed_tools=None)
            self.service.sessions.bind_runtime_session(session['id'], driver_id='pi', runtime_kind='pi', external_session_id='native:' + session['id'])
        self.created = self.create()
        self.app.tick()
        self.effect = next(e for e in self.app.projection(self.room['id'], self.created['graphId'])['effects'] if e['operation'] == 'dispatch')
        self.sid = self.effect['request']['sessionId']
        self.turn = self.effect['receipt']['turnId']
        self.service.events.publish(self.sid, 'tool_started', {'toolName': 'workspace_job', 'toolCallId': 'fixture-start'}, turn_id=self.turn)
        p = patch.object(self.service.runtime, 'is_turn_active', return_value=True)
        self.active = p.start()
        self.addCleanup(p.stop)
        self.gateway = ControlToolGateway(sessions=self.service.sessions, management=object(), core=object(),
            project=self.service.project, collaboration=self.service, background_jobs=self.service.background_jobs,
            workspace_harness=self.service.background_jobs.workspace_harness, work_documents=self.service.work_documents)
        self.service.bind_approval_executor(self.gateway.apply_approval)
        self.gateway.bind_auto_approval_executor(self.service.auto_approve_pending)
        self.sequence = 0
        self.program = self.root / 'listener.py'
        self.program.write_text("import socket,time\ns=socket.socket();s.bind(('127.0.0.1',0));s.listen()\nprint('PORT='+str(s.getsockname()[1]),flush=True)\ntime.sleep(30)\n")

    def tool(self, op, **args):
        self.sequence += 1
        # Native Runtime publishes the causal tool event before calling Gateway.
        self.service.events.publish(self.sid, 'tool_started',
            {'toolName': 'workspace_job', 'toolCallId': f'fixture-job:{self.sequence}'}, turn_id=self.turn)
        return self.gateway.execute({'schemaVersion': 'rag-ime.agent-tool-call.v1', 'sessionId': self.sid,
            'tool': 'workspace_job', 'toolCallId': f'fixture-job:{self.sequence}', 'args': {'op': op, **args}})['result']

    def start_job(self):
        result = self.tool('start', command='python3 ' + shlex.quote(str(self.program)), cwd=str(self.root),
                           timeoutSeconds=30, allowNetwork=True, label='isolated listener')
        self.assertFalse(result.get('approvalRequired'), result)
        receipt = result['receipt']
        self.assertTrue(receipt['ok'], receipt)
        self.job = receipt['job']
        job_id = self.job['jobId']
        # Always stop this test-owned process through its existing owner, even
        # when an intentional negative authorization assertion fails.
        self.addCleanup(lambda: self.service.background_jobs._cancel_owned(self.sid, job_id,
                        reason='fixture_cleanup', room_owner=True))
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            output = self.service.background_jobs.logs(self.sid, job_id)['text']
            if 'PORT=' in output:
                self.port = int(output.split('PORT=', 1)[1].splitlines()[0])
                with socket.create_connection(('127.0.0.1', self.port), timeout=1):
                    pass
                return job_id
            time.sleep(.03)
        self.fail('isolated listener did not start')

    def assert_stopped(self, job_id):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            job = self.service.background_jobs.status(self.sid, job_id)['job']
            if job['status'] == 'cancelled':
                break
            time.sleep(.03)
        self.assertEqual(job['status'], 'cancelled')
        with self.assertRaises(OSError):
            socket.create_connection(('127.0.0.1', self.port), timeout=.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(self.job['pid'], 0)

    def test_current_dispatch_tool_cancels_its_real_listener_and_terminal_replay_is_idempotent(self):
        job_id = self.start_job()
        self.assertEqual(self.job['causalMetadata']['turnId'], self.turn)
        self.assertEqual(self.job['roomLineage']['rootId'], self.created['rootId'])
        self.assertNotEqual(self.turn, self.created['rootId'])
        cancelled = self.tool('cancel', jobId=job_id)
        self.assertTrue(cancelled['receipt'].get('ok'), cancelled['receipt'])
        self.assert_stopped(job_id)
        repeated = self.tool('cancel', jobId=job_id)
        self.assertTrue(repeated['receipt']['alreadyTerminal'])
        self.assertFalse(repeated.get('approvalRequired'))

    def test_root_owner_uses_root_column_not_pi_turn_for_fanout(self):
        job_id = self.start_job()
        with self.assertRaises(AgentBackgroundJobError):
            self.service.background_jobs.cancel_room_owned(self.sid, job_id, room_turn_id=self.turn)
        receipts = self.service.background_jobs.cancel_room_root(self.sid, room_turn_id=self.created['rootId'])
        self.assertEqual(len(receipts), 1)
        self.assert_stopped(job_id)

    def causal(self):
        return dict(self.job['roomLineage'], turnId=self.turn, roomBound=True)

    def test_verifier_claim_shape_cancels_only_its_dispatch(self):
        # Use the auxiliary owner's persisted claim shape with the same isolated
        # admitted Runtime; actual Pi admission remains the fixture transport double.
        from rag_ime.jev_tasks.types import canonical
        request = dict(self.effect['request'], purpose='verify', subjectHash='fixture-subject')
        with self.app.ledger.connection(write=True) as conn:
            conn.execute('UPDATE agent_jev_runtime_effects SET request_json=? WHERE effect_id=?',
                         (json.dumps(request), self.effect['effectId']))
            conn.execute('UPDATE agent_jev_executor_claims SET binding_json=? WHERE effect_id=?',
                         (canonical({'purpose': 'verify', 'subjectHash': request['subjectHash'],
                                     'dispatchId': self.effect['effectId']}), self.effect['effectId']))
        job_id = self.start_job()
        result = self.tool('cancel', jobId=job_id)
        self.assertTrue(result['receipt'].get('ok'), result)
        self.assert_stopped(job_id)

    def test_result_submission_keeps_authority_to_clean_up_before_turn_settles(self):
        job_id = self.start_job()
        result = self.app.tool_operation(self.sid, {'op': 'result_submit', 'proposal': {
            'resultSummary': 'isolated result', 'evidenceRefs': ['test:job'], 'artifactRefs': []}},
            tool_call_id='fixture-submit')
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(self.snapshot(self.created).task(self.effect['request']['taskId']).result,
                         'isolated result')
        cancelled = self.tool('cancel', jobId=job_id)
        self.assertTrue(cancelled['receipt'].get('ok'), cancelled)
        self.assert_stopped(job_id)

    def test_job_owner_rejects_each_mismatching_immutable_lineage(self):
        job_id = self.start_job()
        for field in ('roomId', 'rootId', 'dispatchId', 'turnId', 'generation'):
            with self.subTest(field=field):
                wrong = dict(self.causal(), **{field: 999 if field == 'generation' else 'wrong'})
                with self.assertRaises(AgentBackgroundJobError):
                    self.service.background_jobs.cancel_room_dispatch_owned(self.sid, job_id, context=wrong)
        other = next(s['id'] for s in self.sessions if s['id'] != self.sid)
        with self.assertRaises((AgentBackgroundJobError, KeyError)):
            self.service.background_jobs.cancel_room_dispatch_owned(other, job_id, context=self.causal())
        self.assertEqual(self.service.background_jobs.status(self.sid, job_id)['job']['status'], 'running')

    def test_jev_owner_rejects_stale_identity_missing_claim_and_unconfirmed_runtime(self):
        from rag_ime.jev_tasks.types import GraphConflict
        job_id = self.start_job()
        for field in ('roomId', 'rootId', 'turnId', 'generation'):
            with self.subTest(field=field):
                wrong = dict(self.causal(), **{field: 999 if field == 'generation' else 'wrong'})
                with self.assertRaises(GraphConflict):
                    self.app.cancel_workspace_job(self.sid, job_id, causal=wrong)
        for active in (False, None):
            self.active.return_value = active
            with self.assertRaises(GraphConflict):
                self.app.cancel_workspace_job(self.sid, job_id, causal=self.causal())
        self.active.return_value = True
        with self.app.ledger.connection(write=True) as conn:
            conn.execute('DELETE FROM agent_jev_executor_claims WHERE effect_id=?', (self.effect['effectId'],))
        with self.assertRaises(GraphConflict):
            self.app.cancel_workspace_job(self.sid, job_id, causal=self.causal())
        self.assertEqual(self.service.background_jobs.status(self.sid, job_id)['job']['status'], 'running')

    def test_newer_room_dispatch_cannot_cancel_previous_job(self):
        from rag_ime.jev_tasks.types import GraphConflict
        job_id = self.start_job()
        self.service.room_turns.finish(self.sid, self.turn, self.created['rootId'])
        self.service.room_turns.begin(self.sid, self.created['rootId'], dispatch_id='newer-dispatch')
        self.service.room_turns.accept(self.sid, 'newer-turn', self.created['rootId'])
        with self.assertRaises(GraphConflict):
            self.app.cancel_workspace_job(self.sid, job_id, causal=self.causal())
        self.assertEqual(self.service.background_jobs.status(self.sid, job_id)['job']['status'], 'running')

    def test_no_jev_effect_keeps_existing_room_boundary(self):
        job_id = self.start_job()
        with patch.object(self.app.lifecycle, 'effect_for_dispatch', return_value=None):
            result = self.tool('cancel', jobId=job_id)
        self.assertFalse(result['receipt']['mutationApplied'])
        self.assertIn('Room-bound background jobs', result['receipt']['error'])
        self.assertEqual(self.service.background_jobs.status(self.sid, job_id)['job']['status'], 'running')

    def test_job_execution_owner_is_still_required(self):
        job_id = self.start_job()
        with patch.object(self.service.background_jobs, 'execution_owner', False):
            result = self.tool('cancel', jobId=job_id)
        self.assertFalse(result['receipt']['mutationApplied'])
        self.assertEqual(self.service.background_jobs.status(self.sid, job_id)['job']['status'], 'running')

    def test_root_fanout_all_sessions_matches_only_explicit_root(self):
        job_id = self.start_job()
        self.assertEqual(self.service.background_jobs.cancel_room_root_all_sessions(room_turn_id=self.turn), [])
        receipts = self.service.background_jobs.cancel_room_root_all_sessions(room_turn_id=self.created['rootId'])
        self.assertEqual([r['job']['jobId'] for r in receipts], [job_id])
        self.assert_stopped(job_id)

    def test_stopped_root_and_mismatching_claim_reject_without_signalling(self):
        from rag_ime.jev_tasks.types import GraphConflict
        job_id = self.start_job()
        other = next(s['id'] for s in self.sessions if s['id'] != self.sid)
        with self.assertRaises(GraphConflict):
            self.app.cancel_workspace_job(other, job_id, causal=self.causal())
        with self.app.ledger.connection(write=True) as conn:
            claim = conn.execute('SELECT binding_json FROM agent_jev_executor_claims WHERE effect_id=?',
                                 (self.effect['effectId'],)).fetchone()[0]
            conn.execute("UPDATE agent_jev_executor_claims SET binding_json='[]' WHERE effect_id=?", (self.effect['effectId'],))
        with self.assertRaises(GraphConflict):
            self.app.cancel_workspace_job(self.sid, job_id, causal=self.causal())
        with self.app.ledger.connection(write=True) as conn:
            conn.execute('UPDATE agent_jev_executor_claims SET binding_json=? WHERE effect_id=?', (claim, self.effect['effectId']))
            conn.execute('UPDATE agent_jev_host_roots SET stopped=1 WHERE graph_id=?', (self.created['graphId'],))
        with self.assertRaises(GraphConflict):
            self.app.cancel_workspace_job(self.sid, job_id, causal=self.causal())
        self.assertEqual(self.service.background_jobs.status(self.sid, job_id)['job']['status'], 'running')
        with socket.create_connection(('127.0.0.1', self.port), timeout=1):
            pass

    def test_pending_termination_returns_pending_receipt_until_owner_observes_exit(self):
        job_id = self.start_job()
        with patch.object(self.service.background_jobs, '_terminate_live') as terminate:
            result = self.tool('cancel', jobId=job_id)
            terminate.assert_called()  # The existing monitor may also retry termination.
        self.assertTrue(result['receipt']['ok'])
        self.assertEqual(result['receipt']['job']['status'], 'cancelling')
        with socket.create_connection(('127.0.0.1', self.port), timeout=1):
            pass  # A cancellation request does not invent physical drain.
        self.service.background_jobs.cancel_room_owned(self.sid, job_id, room_turn_id=self.created['rootId'])
        self.assert_stopped(job_id)
