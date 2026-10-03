from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shlex
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from rag_ime.agent_background_jobs import AgentBackgroundJobError
from rag_ime.agent_gateway_requests import GatewayRequestStore
from rag_ime.agent_service import AgentService
from rag_ime.db import sqlite_connection
from rag_ime.pi.config import PiRuntimeConfig


class WorkspaceJobTurnCancellationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='paw-job-stop-')
        self.root = Path(self.temporary.name)
        self.service = AgentService(db_path=self.root / 'state.sqlite',
            runtime_config=PiRuntimeConfig(enabled=False, executable=None,
                agent_dir=self.root / 'config', session_dir=self.root / 'sessions', logs_dir=self.root / 'logs'),
            startup_recovery_enabled=False, wake_scheduler_enabled=False)
        self.jobs = self.service.background_jobs
        self.session = self.service.sessions.create(title='fixture', mode='coordinator',
            execution_mode='full_trust', tool_profile_version='control-center-auto-approve-v1',
            workspace_roots=[str(self.root)])
        self.session_id = str(self.session['id'])
        self.job_ids = []

    def tearDown(self):
        try:
            for session_id, job_id in self.job_ids:
                self.jobs.cancel(session_id, job_id)
        finally:
            self.service.close()
            self.temporary.cleanup()

    def prepared(self, name):
        script = f'import os,time,pathlib; pathlib.Path({name + ".pid"!r}).write_text(str(os.getpid())); time.sleep(2); pathlib.Path({name + ".late"!r}).write_text("late"); time.sleep(30)'
        return self.jobs.workspace_harness.prepare_background_command(self.session,
            {'command': f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}', 'cwd': str(self.root), 'timeoutSeconds': 60})

    def start(self, name, turn_id, session_id=None):
        session_id = session_id or self.session_id
        result = self.jobs.start(session_id, self.prepared(name), causal_metadata={'turnId': turn_id})
        self.job_ids.append((session_id, result['job']['jobId']))
        return result['job']['jobId']

    def abort(self, turn_id='original', pending_admission=False):
        def native_abort(session_id, before_abort):
            before_abort({'turnId': turn_id, 'clientMessageId': 'original-client', 'pendingAdmission': pending_admission})
            return {'sessionId': session_id, 'turnId': turn_id, 'lifecycle': {'drained': True, 'idle': True}}
        with patch.object(self.service.runtime, 'abort_with_approval_fence', side_effect=native_abort):
            return self.service.abort(self.session_id)

    def wait_file(self, name):
        deadline = time.monotonic() + 5
        while not (self.root / name).exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue((self.root / name).exists())

    def test_stop_physically_drains_original_job_without_stopping_other_turns(self):
        original = self.start('original', 'original')
        old = self.start('old', 'old')
        unbound = self.start('unbound', '')
        other_session = self.service.sessions.create(title='other', mode='coordinator', workspace_roots=[str(self.root)])
        other = self.start('other', 'original', str(other_session['id']))
        self.wait_file('original.pid')
        pid = int((self.root / 'original.pid').read_text())
        result = self.abort()
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertTrue(result['backgroundJobs']['drained'])
        self.assertEqual(result['backgroundJobs']['jobIds'], [original])
        for session_id, job_id in [(self.session_id, old), (self.session_id, unbound), (str(other_session['id']), other)]:
            self.assertEqual(self.jobs.status(session_id, job_id)['job']['status'], 'running')
        successor = self.start('successor', 'successor')
        self.assertEqual(self.jobs.status(self.session_id, successor)['job']['status'], 'running')
        time.sleep(2.1)
        self.assertFalse((self.root / 'original.late').exists())

    def test_stop_rejects_a_late_start_for_only_the_cancelled_turn(self):
        self.abort()
        with self.assertRaisesRegex(AgentBackgroundJobError, 'cancelled'):
            self.start('late', 'original')
        self.assertFalse((self.root / 'late.pid').exists())
        self.start('next', 'next')

    def test_stop_during_spawn_does_not_release_user_command(self):
        entered, release = threading.Event(), threading.Event()
        spawn = self.jobs.workspace_harness.spawn_background
        def held_spawn(*args, **kwargs):
            launched = spawn(*args, **kwargs)
            entered.set()
            if not release.wait(5):
                raise TimeoutError('fixture launch was not released')
            return launched
        try:
            with ThreadPoolExecutor(max_workers=1) as pool, patch.object(self.jobs.workspace_harness, 'spawn_background', side_effect=held_spawn):
                starting = pool.submit(self.start, 'racing', 'original')
                self.assertTrue(entered.wait(5))
                try:
                    result = self.abort()
                    self.assertFalse(result['backgroundJobs']['drained'])
                finally:
                    release.set()
                with self.assertRaisesRegex(AgentBackgroundJobError, 'cancelled'):
                    starting.result(timeout=5)
            self.assertFalse((self.root / 'racing.pid').exists())
            self.assertFalse((self.root / 'racing.late').exists())
        finally:
            release.set()

    def test_unknown_pid_is_not_reported_as_physically_drained(self):
        job_id = self.start('unknown', 'original')
        live = self.jobs._live.pop(job_id)
        live.detach_requested.set()
        live.thread.join(3)
        try:
            with sqlite_connection(self.jobs.db_path) as conn:
                conn.execute('UPDATE agent_background_jobs SET pid=NULL, process_group_id=NULL, process_birth_token="" WHERE job_id=?', (job_id,))
            result = self.abort()
            self.assertFalse(result['ok'])
            self.assertFalse(result['backgroundJobs']['drained'])
            self.assertEqual(result['backgroundJobs']['pendingJobIds'], [job_id])
        finally:
            self.jobs._terminate_and_wait(live)

    def test_stop_before_release_gate_prevents_command_execution(self):
        entered, release = threading.Event(), threading.Event()
        release_launch = self.jobs._release_launch
        def held_release(job_id):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('fixture release gate was not released')
            return release_launch(job_id)
        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(self.jobs, '_release_launch', side_effect=held_release):
            starting = pool.submit(self.start, 'release-race', 'original')
            self.assertTrue(entered.wait(5))
            try:
                stopped = self.abort()
                self.assertEqual(len(stopped['backgroundJobs']['jobIds']), 1)
            finally:
                release.set()
            with self.assertRaisesRegex(AgentBackgroundJobError, 'cancelled'):
                starting.result(timeout=5)
        self.assertFalse((self.root / 'release-race.pid').exists())
        self.assertFalse((self.root / 'release-race.late').exists())

    def test_room_owned_job_is_left_to_the_room_cancel_owner(self):
        job_id = self.start('room', 'original')
        try:
            with sqlite_connection(self.jobs.db_path) as conn:
                conn.execute('UPDATE agent_background_jobs SET room_bound=1 WHERE job_id=?', (job_id,))
            stopped = self.abort()
            self.assertEqual(stopped['backgroundJobs']['jobIds'], [])
            self.assertEqual(self.jobs.status(self.session_id, job_id)['job']['status'], 'running')
        finally:
            with sqlite_connection(self.jobs.db_path) as conn:
                conn.execute('UPDATE agent_background_jobs SET room_bound=0 WHERE job_id=?', (job_id,))

    def test_pending_admission_stop_finds_a_running_job_after_its_http_receipt_completed(self):
        job_id = self.start('completed-http', 'original')
        self.wait_file('completed-http.pid')
        requests = GatewayRequestStore(self.service.sessions)
        request = {'sessionId': self.session_id, 'toolCallId': 'completed-job-start',
            'executionBinding': {'turnId': 'original', 'clientMessageId': 'original-client'}}
        requests.admit(request)
        requests.complete(request, {'ok': True, 'jobId': job_id})
        stopped = self.abort('', pending_admission=True)
        self.assertEqual(stopped['backgroundJobs']['jobIds'], [job_id])
        self.assertTrue(stopped['backgroundJobs']['drained'])
        self.assertEqual(stopped['backgroundJobs']['turnIds'], ['original'])
        with self.assertRaisesRegex(AgentBackgroundJobError, 'cancelled'):
            self.start('late-completed-http', 'original')

    def test_ambiguous_pending_client_does_not_cancel_a_successor_job(self):
        requests = GatewayRequestStore(self.service.sessions)
        for turn_id in ('original', 'successor'):
            self.start('ambiguous-' + turn_id, turn_id)
            request = {'sessionId': self.session_id, 'toolCallId': 'ambiguous-' + turn_id,
                'executionBinding': {'turnId': turn_id, 'clientMessageId': 'original-client'}}
            requests.admit(request)
            requests.complete(request, {'ok': True})
        stopped = self.abort('', pending_admission=True)
        self.assertFalse(stopped['ok'])
        self.assertFalse(stopped['backgroundJobs']['drained'])
        self.assertEqual(stopped['backgroundJobs']['jobIds'], [])
        self.assertEqual(len(stopped['backgroundJobs']['pendingJobIds']), 2)
        for session_id, job_id in self.job_ids:
            self.assertEqual(self.jobs.status(session_id, job_id)['job']['status'], 'running')

    def test_native_abort_error_still_physically_cancels_captured_jobs(self):
        self.start('native-error', 'original')
        self.wait_file('native-error.pid')
        pid = int((self.root / 'native-error.pid').read_text())
        def native_abort(session_id, before_abort):
            before_abort({'turnId': 'original', 'clientMessageId': 'original-client'})
            raise RuntimeError('fixture native acknowledgement lost')
        with patch.object(self.service.runtime, 'abort_with_approval_fence', side_effect=native_abort):
            with self.assertRaisesRegex(RuntimeError, 'acknowledgement lost'):
                self.service.abort(self.session_id)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)


if __name__ == '__main__':
    unittest.main()
