from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import subprocess
import sys
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch

from rag_ime.team.broker import TeamModelConfig
from rag_ime.team.coordinator import TeamExecutionCoordinator
from rag_ime.team.errors import TeamError
from rag_ime.team.execution import ExecutionReceipt, TeamExecutionError
from rag_ime.team.gateway import TeamApplication
from rag_ime.team.grants import execution_authority


class Process:
    def __init__(self):
        self.code = None

    def poll(self):
        return self.code


class Launcher:
    def __init__(self):
        self.started, self.stopped = [], []

    def start(self, spec, command, environment):
        self.started.append(spec.container_id)
        return Process()

    def stop(self, spec, process):
        self.stopped.append(spec.container_id)
        process.code = 0
        return ExecutionReceipt(spec.attempt_id, spec.session_id, spec.container_id, 'stop', 'absent', True)


class UnknownStartLauncher(Launcher):
    """Synthetic launcher whose start outcome is unknown to the coordinator."""

    def __init__(self):
        super().__init__()
        self.specs = []
        self.inspect_missing = False
        self.commands = []

    def start(self, spec, command, environment):
        self.specs.append(spec)
        self.started.append(spec.container_id)
        raise TeamExecutionError('synthetic launcher failure after registration')

    def _run_docker(self, command, *, allow_failure):
        self.commands.append(tuple(command))
        if command[1] == 'rm':
            return subprocess.CompletedProcess(command, 0, '', '')
        if self.inspect_missing:
            return subprocess.CompletedProcess(command, 1, '', 'Error: No such object')
        return subprocess.CompletedProcess(command, 0, '[{"Id":"still-present"}]', '')

    @staticmethod
    def _is_missing_container(result):
        text = ((result.stdout or '') + (result.stderr or '')).lower()
        return result.returncode != 0 and 'no such object' in text


class TeamExecutionCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix='paw-team-coordinator-')
        root = Path(self.tmp.name)
        self.coordinator = TeamExecutionCoordinator(
            image='fixture-image', command=('fixture',),
            model=TeamModelConfig(base_url='https://model.example.invalid/v1', model='fixture', api_key='upstream-secret-fixture'),
        )
        self.launcher = Launcher()
        self.coordinator.launcher = self.launcher
        self.app = TeamApplication(root / 'data', root / 'web', execution=self.coordinator)
        self.admin = self.app.identity.bootstrap_admin('admin', 'administrator-password')
        self.space = self.app.identity.list_spaces(self.admin['id'])[0]
        self.workspace = root / 'workspace'
        self.workspace.mkdir()
        self.app.grants.bind_session(self.admin['id'], self.space['id'], 'agent:fixture', self.workspace)
        self.app.shared_resources.store.capture_session(self.admin['id'], self.space['id'], 'agent:fixture')
        self.session = {'id': 'agent:fixture', 'workspaceRoots': [str(self.workspace)], 'executionMode': 'workspace_managed'}

    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()

    def binding(self):
        return self.coordinator.binding_for(self.space['id'], self.session)

    def test_attempt_only_carries_broker_credentials_and_reuses_its_owned_scope(self):
        binding = self.binding()
        self.assertIs(self.binding(), binding)
        self.assertNotIn('upstream-secret-fixture', repr(binding.model_providers))
        self.assertEqual(binding.spec.network_mode, 'none')
        self.assertEqual(binding.spec.workspace_root, self.workspace.resolve())

    def test_quiescence_stops_container_and_fences_an_old_launch(self):
        old = self.binding()
        old.launcher.start(old.spec, ('fixture',), {})
        with self.coordinator.quiesce('agent:fixture'):
            self.assertEqual(self.launcher.stopped, [old.spec.container_id])
            with self.assertRaises(TeamError):
                old.launcher.start(old.spec, ('fixture',), {})
        new = self.binding()
        self.assertGreater(new.generation, old.generation)
        self.assertEqual(new.spec.session_dir, old.spec.session_dir)
        self.assertNotEqual(new.spec.home_dir, old.spec.home_dir)
        self.assertNotEqual(new.spec.agent_dir, old.spec.agent_dir)
        with self.assertRaises(TeamError):
            self.app.grants.resolve_token(old.gateway_token, 'agent:fixture', self.space['id'])
        self.assertTrue((old.spec.scope_root / ('stop-' + old.spec.attempt_id + '.json')).exists())

    def test_member_revocation_stops_tracked_process_without_restoring_grant(self):
        binding = self.binding()
        binding.launcher.start(binding.spec, ('fixture',), {})
        self.app.revoke_user(self.admin['id'])
        self.assertEqual(self.launcher.stopped, [binding.spec.container_id])
        with self.assertRaises(TeamError):
            self.binding()

    def test_disabling_runtime_does_not_treat_an_existing_worker_as_quiescent(self):
        binding = self.binding()
        binding.launcher.start(binding.spec, ('fixture',), {})
        self.app.execution = None
        try:
            with self.assertRaises(TeamError):
                with self.app.quiesce_session('agent:fixture'):
                    self.fail('An unverified previous worker cannot be snapshotted')
        finally:
            self.app.execution = self.coordinator

    def payload(self):
        return {
            'sessionId': 'agent:fixture', 'spaceId': self.space['id'], 'args': {},
            # TeamRemoteWorkspaceHarness projects this server-owned Session
            # policy into the worker request.  The coordinator must derive
            # the mount mode from this projection, never from a caller flag.
            'session': dict(self.session),
        }

    def _recover_after_test(self, spec, launcher):
        launcher.inspect_missing = True
        self.coordinator._recover_container(spec.scope_root)
        self.coordinator._recovery_pending.discard(spec.scope_root)

    def test_one_shot_mount_mode_comes_from_session_policy_projection(self):
        launcher = UnknownStartLauncher()
        self.coordinator.launcher = launcher
        payload = self.payload()
        payload['session']['executionMode'] = 'read_only'
        payload['workspaceReadOnly'] = False
        with patch.object(self.coordinator, '_new_operation_launcher', return_value=launcher):
            with self.assertRaises(TeamExecutionError):
                self.coordinator.run_isolated(self.workspace, ('fixture',), payload=payload)
        self.assertEqual(len(launcher.specs), 1)
        self.assertTrue(launcher.specs[0].workspace_read_only)
        self.assertIn(launcher.specs[0].scope_root, self.coordinator._recovery_pending)
        self.assertTrue((launcher.specs[0].scope_root / 'active-container.json').exists())
        self._recover_after_test(launcher.specs[0], launcher)

    def test_one_shot_requires_server_session_policy_projection(self):
        payload = self.payload()
        payload.pop('session')
        with patch.object(self.coordinator, '_new_operation_launcher', return_value=Launcher()):
            with self.assertRaises(TeamError):
                self.coordinator.run_isolated(self.workspace, ('fixture',), payload=payload)

    def test_launcher_start_failure_keeps_marker_until_container_absence_is_verified(self):
        launcher = UnknownStartLauncher()
        self.coordinator.launcher = launcher
        binding = self.binding()
        with self.assertRaises(TeamExecutionError):
            binding.launcher.start(binding.spec, ('fixture',), {})
        marker = binding.spec.scope_root / 'active-container.json'
        self.assertTrue(marker.exists())
        self.assertIn(binding.spec.scope_root, self.coordinator._recovery_pending)
        self.assertNotIn('agent:fixture', self.coordinator._processes)

        with self.assertRaises(TeamExecutionError):
            self.coordinator._recover_container(binding.spec.scope_root)
        self.assertTrue(marker.exists())
        self.assertFalse((binding.spec.scope_root / ('recovered-' + binding.spec.attempt_id + '.json')).exists())

        self._recover_after_test(binding.spec, launcher)
        self.assertFalse(marker.exists())
        receipt = binding.spec.scope_root / ('recovered-' + binding.spec.attempt_id + '.json')
        self.assertTrue(receipt.exists())
        self.assertIn('"verified": true', receipt.read_text())

    def test_workspace_worker_rejects_another_checkout_and_revoked_session(self):
        other = self.workspace.parent / 'other'
        other.mkdir()
        with self.assertRaises(TeamError):
            self.coordinator.run_isolated(other, ('fixture',), payload=self.payload())
        self.app.grants.revoke_user(self.admin['id'])
        with self.assertRaises(TeamError):
            self.coordinator.run_isolated(self.workspace, ('fixture',), payload=self.payload())

    def test_revocation_stops_an_already_running_workspace_worker(self):
        launcher = PipeLauncher()
        failures = []
        def execute():
            try:
                self.coordinator.run_isolated(self.workspace, ('fixture',), payload=self.payload())
            except (TeamError, TeamExecutionError) as exc:
                failures.append(exc)
        with patch.object(self.coordinator, '_new_operation_launcher', return_value=launcher):
            worker = Thread(target=execute)
            worker.start()
            self.assertTrue(launcher.started.wait(5))
            self.app.revoke_user(self.admin['id'])
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertIsNotNone(launcher.process.poll())
        self.assertTrue(failures, 'A revoked operation must not return a successful result')

    def test_quiesce_blocks_a_new_workspace_worker_until_publication_finishes(self):
        with self.coordinator.quiesce('agent:fixture'):
            with self.assertRaises(TeamError):
                self.coordinator.run_isolated(self.workspace, ('fixture',), payload=self.payload())

    def test_worker_stdin_is_covered_by_the_deadline(self):
        launcher = PipeLauncher()
        payload = {**self.payload(), 'args': {'content': 'x' * 1_000_000}}
        started = time.monotonic()
        with patch.object(self.coordinator, '_new_operation_launcher', return_value=launcher):
            with self.assertRaisesRegex(TeamExecutionError, 'deadline'):
                self.coordinator.run_isolated(self.workspace, ('fixture',), payload=payload, timeout=0.2)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIsNotNone(launcher.process.poll())

    def test_previously_admitted_tool_cannot_launch_after_its_attempt_was_retired(self):
        old = self.binding()
        check = lambda: self.app.grants.resolve_token(old.gateway_token, 'agent:fixture', self.space['id'])
        with execution_authority(check):
            with self.coordinator.quiesce('agent:fixture'):
                pass
            with self.assertRaises(TeamError):
                self.coordinator.run_isolated(self.workspace, ('fixture',), payload=self.payload())

    def test_orphan_scan_retires_old_capability_and_removes_container(self):
        old = self.binding()
        old.launcher.start(old.spec, ('fixture',), {})
        self.coordinator._bindings.clear()
        self.coordinator._processes.clear()
        removed = subprocess.CompletedProcess([], 0, '', '')
        absent = subprocess.CompletedProcess([], 1, '', 'Error: No such object: ' + old.spec.container_id)
        with patch.object(self.launcher, '_run_docker', side_effect=[removed, absent], create=True), \
             patch.object(self.launcher, '_is_missing_container', return_value=True, create=True):
            self.coordinator._scan_orphans()
        self.assertFalse((old.spec.scope_root / 'active-container.json').exists())
        with self.assertRaises(TeamError):
            self.app.grants.resolve_token(old.gateway_token, 'agent:fixture', self.space['id'])

    def test_default_broker_root_keeps_tmp_compatibility(self):
        self.assertIsNone(self.coordinator._broker_socket_root_config)
        self.assertEqual(self.coordinator._socket_root.parent, Path('/tmp'))


class TeamBrokerSocketRootTests(unittest.TestCase):
    def _coordinator(self, broker_socket_root=None):
        return TeamExecutionCoordinator(
            image='fixture-image', command=('fixture',),
            model=TeamModelConfig(
                base_url='https://model.example.invalid/v1',
                model='fixture',
                api_key='upstream-secret-fixture',
            ),
            broker_socket_root=broker_socket_root,
        )

    def _app_with_binding(self, root, coordinator, username):
        web = root / 'web'
        web.mkdir(parents=True)
        (web / 'index.html').write_text('<html></html>')
        app = TeamApplication(root / 'data', web, execution=coordinator)
        user = app.identity.bootstrap_admin(username, 'administrator-password')
        space = app.identity.list_spaces(user['id'])[0]
        workspace = root / 'workspace'
        workspace.mkdir()
        session_id = 'agent:' + username
        app.grants.bind_session(user['id'], space['id'], session_id, workspace)
        app.shared_resources.store.capture_session(user['id'], space['id'], session_id)
        session = {
            'id': session_id,
            'workspaceRoots': [str(workspace)],
            'executionMode': 'workspace_managed',
        }
        binding = coordinator.binding_for(space['id'], session)
        return app, space, binding

    def test_custom_root_is_private_and_actual_broker_socket_binds(self):
        with TemporaryDirectory(prefix='r-', dir='/tmp') as temporary:
            root = Path(temporary).resolve()
            broker_root = root / 'daemon-brokers'
            broker_root.mkdir(mode=0o700)
            coordinator = self._coordinator(broker_root)
            app = None
            try:
                app, _, binding = self._app_with_binding(root / 'app', coordinator, 'custom')
                private_root = coordinator._socket_root
                self.assertIsNotNone(private_root)
                self.assertEqual(private_root.parent, broker_root)
                self.assertEqual(stat.S_IMODE(private_root.stat().st_mode), 0o700)
                self.assertTrue(binding.spec.broker_socket.is_socket())
                self.assertLessEqual(len(str(binding.spec.broker_socket).encode()), 100)
            finally:
                if app is not None:
                    app.close()
                else:
                    coordinator.close()
            self.assertTrue(broker_root.is_dir())
            self.assertEqual(list(broker_root.iterdir()), [])

    def test_two_controllers_have_separate_children_and_close_does_not_remove_peer(self):
        with TemporaryDirectory(prefix='r-', dir='/tmp') as temporary:
            root = Path(temporary).resolve()
            broker_root = root / 'daemon-brokers'
            broker_root.mkdir(mode=0o700)
            coordinator_a = self._coordinator(broker_root)
            coordinator_b = self._coordinator(broker_root)
            app_a = app_b = None
            try:
                app_a, _, binding_a = self._app_with_binding(root / 'a', coordinator_a, 'controller-a')
                app_b, _, binding_b = self._app_with_binding(root / 'b', coordinator_b, 'controller-b')
                child_a = coordinator_a._socket_root
                child_b = coordinator_b._socket_root
                self.assertNotEqual(child_a, child_b)
                self.assertTrue(binding_a.spec.broker_socket.exists())
                self.assertTrue(binding_b.spec.broker_socket.exists())

                app_a.close()
                app_a = None
                self.assertTrue(broker_root.is_dir())
                self.assertFalse(child_a.exists())
                self.assertTrue(child_b.is_dir())
                self.assertTrue(binding_b.spec.broker_socket.exists())
            finally:
                if app_a is not None:
                    app_a.close()
                if app_b is not None:
                    app_b.close()
                else:
                    coordinator_b.close()
            self.assertTrue(broker_root.is_dir())
            self.assertEqual(list(broker_root.iterdir()), [])

    def test_bad_or_overlong_custom_root_is_rejected_before_execution(self):
        with TemporaryDirectory(prefix='r-', dir='/tmp') as temporary:
            root = Path(temporary).resolve()
            missing = root / 'missing'
            file_path = root / 'file'
            file_path.write_text('not a directory')
            real = root / 'real'
            real.mkdir()
            symlink = root / 'link'
            symlink.symlink_to(real, target_is_directory=True)
            symlink_parent = root / 'parent-link'
            symlink_parent.symlink_to(root, target_is_directory=True)
            nested = symlink_parent / 'nested'
            nested.mkdir()
            overlong = root / ('x' * 100)
            overlong.mkdir()
            writable = root / 'writable'
            writable.mkdir(mode=0o777)
            writable.chmod(0o777)

            for invalid in ('', 'relative', missing, file_path, symlink, nested, overlong, writable, Path('/')):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    self._coordinator(invalid)

            owned = root / 'owned'
            owned.mkdir(mode=0o700)
            with patch('rag_ime.team.coordinator.os.getuid', return_value=owned.stat().st_uid + 1):
                with self.assertRaises(ValueError):
                    self._coordinator(owned)


class PipeLauncher:
    """Local child fixture exercises pipe cancellation; it is not OCI evidence."""
    def __init__(self):
        self.started = Event()
        self.process = None

    def start(self, spec, command, environment):
        self.process = subprocess.Popen(
            [sys.executable, '-c', 'import time; time.sleep(60)'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
        )
        self.started.set()
        return self.process

    def stop(self, spec, process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        return ExecutionReceipt(spec.attempt_id, spec.session_id, spec.container_id, 'stop', 'absent', True)
