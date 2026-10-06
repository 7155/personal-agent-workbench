from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shlex
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from rag_ime.agent_service import AgentService
from rag_ime.debug_server import DebugImeService, DebugServerConfig
from rag_ime.pi.config import PiRuntimeConfig
from rag_ime.agent_workspace import WorkspaceHarness, WorkspaceHarnessError
from rag_ime.agent_workspace_commands import WorkspaceCommandOwner
from rag_ime.rooms.cancellation_proofs import _session_abort_surfaces


class WorkspaceCommandCancellationTests(unittest.TestCase):
    def test_stop_fences_a_waiting_call_and_preserves_another_session_and_later_call(self):
        effects = []
        harness = WorkspaceHarness(executor=lambda prepared: effects.append(prepared.command) or {'exitCode': 0})
        owner = WorkspaceCommandOwner(harness)
        with tempfile.TemporaryDirectory() as temporary:
            prepared = harness.prepare_command({'mode': 'coordinator', 'workspaceRoots': [temporary]}, {'command': 'echo fixture'})
            entered, release = threading.Event(), threading.Event()
            def old_call():
                with owner.call_scope('A'):
                    entered.set()
                    self.assertTrue(release.wait(3))
                    with self.assertRaises(WorkspaceHarnessError):
                        owner.execute('A', prepared)
            with ThreadPoolExecutor(max_workers=1) as pool:
                old = pool.submit(old_call)
                self.assertTrue(entered.wait(3))
                stopped = owner.request_cancel('A')
                self.assertEqual(owner.execute('B', prepared)['exitCode'], 0)
                self.assertEqual(owner.execute('A', prepared)['exitCode'], 0)
                release.set()
                old.result(timeout=3)
                self.assertTrue(stopped()['drained'])
                self.assertEqual(effects, ['echo fixture', 'echo fixture'])

    def test_host_idle_does_not_hide_a_pending_gateway_process(self):
        receipt = {'sessionId': 'A', 'runtimeReceipt': {
            'schemaVersion': 'rag-ime.pi-session-abort-receipt.v1',
            'lifecycle': {'schemaVersion': 'pi.agent-abort-receipt.v1', 'drained': True, 'idle': True}},
            'workspaceCommands': {'drained': False, 'operationIds': ['command:A'], 'pendingOperationIds': ['command:A']}}
        self.assertEqual(_session_abort_surfaces(receipt)['shell']['state'], 'requested')

    def test_session_stop_drains_the_gateway_command_process(self):
        with tempfile.TemporaryDirectory(prefix='paw-command-cancel-') as temporary, \
                patch.dict(os.environ, {'RAG_IME_APP_SUPPORT_DIR': str(Path(temporary) / 'support')}):
            root = Path(temporary)
            service = AgentService(db_path=root / 'state.sqlite', runtime_config=PiRuntimeConfig(enabled=False,
                                   executable=None, agent_dir=root / 'config', session_dir=root / 'sessions', logs_dir=root / 'logs'),
                                   startup_recovery_enabled=False, wake_scheduler_enabled=False)
            app = DebugImeService(DebugServerConfig(db_path=root / 'state.sqlite', agent_service=service,
                                 seed_if_empty=False, memory_projection_worker_enabled=False,
                                 rime_user_dir=root / 'Rime', rime_lexicon_backup_root=root / 'RimeBackups'))
            session = service.sessions.create(title='cancel fixture', mode='coordinator',
                                             execution_mode='full_trust', tool_profile_version='control-center-auto-approve-v1',
                                             workspace_roots=[str(root)])
            session_id = str(session['id'])
            script = 'import os,time,pathlib; pathlib.Path("pid").write_text(str(os.getpid())); time.sleep(30)'
            pending = app.agent_tools._prepare_workspace_command(session_id=session_id,
                        args={'command': f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}',
                              'cwd': str(root), 'timeoutSeconds': 60}, risk_level='R2')
            approval = service.sessions.decide_approval(pending['approvalId'], approved=True,
                                                       payload_sha256=pending['approval']['payloadSha256'])
            receipt = {'schemaVersion': 'rag-ime.pi-session-abort-receipt.v1', 'sessionId': session_id,
                       'turnId': 'fixture-turn', 'lifecycle': {'schemaVersion': 'pi.agent-abort-receipt.v1',
                       'drained': True, 'idle': True, 'operations': [], 'pendingOperations': [], 'failedOperationIds': []}}
            pid = None
            try:
                with ThreadPoolExecutor(max_workers=1) as pool, patch.object(service.runtime, 'abort', return_value=receipt):
                    running = pool.submit(app.agent_tools._apply_workspace_command, approval)
                    deadline = time.monotonic() + 5
                    while not (root / 'pid').exists() and time.monotonic() < deadline and not running.done():
                        time.sleep(.01)
                    self.assertTrue((root / 'pid').exists(), running.result() if running.done() else 'process did not start')
                    pid = int((root / 'pid').read_text())
                    os.kill(pid, 0)
                    stopped = service.abort(session_id)
                    try:
                        with self.assertRaises(ProcessLookupError):
                            os.kill(pid, 0)
                        self.assertTrue(stopped['workspaceCommands']['drained'])
                        self.assertNotEqual(running.result(timeout=2)['exitCode'], 0)
                    finally:
                        try: os.killpg(pid, signal.SIGKILL)
                        except ProcessLookupError: pass
            finally:
                app.close()


if __name__ == '__main__':
    unittest.main()
